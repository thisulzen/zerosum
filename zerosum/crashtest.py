"""Kill the server partway through a stream of transfers, several times
over, and check that nothing was lost or posted twice.

    python -m zerosum.crashtest [--transfers 2000] [--clients 16] [--kills 5]

The clients behave the way a real client has to. When a connection drops
they can't know whether their transfer was posted, so they wait and send
the same request with the same idempotency key until they get an answer.
Meanwhile the server is killed with SIGKILL, which gives it no chance to
clean up, and restarted on the same file. A kill can land before a transfer
commits, after it commits but before the response goes out, or between
requests, and the clients can't tell which.

Once every transfer has an answer, three things must hold:

- the audit finds nothing wrong;
- the ledger holds exactly the transfers the clients were told succeeded,
  each under the id the client was given;
- every balance equals what the clients work out for themselves from the
  transfers they were told succeeded, without asking the server.

SIGKILL tests a process dying, not the machine losing power. Surviving a
power cut depends on the disk honouring fsync, which a test like this can't
check.
"""

import argparse
import http.client
import json
import random
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from zerosum.audit import audit

CUSTOMERS = [f"customer-{i:02}" for i in range(20)]
OPENING_BALANCE = 50_000
RETRY_FOR = 30  # seconds a client keeps retrying one request before giving up
ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Report:
    transfers: int
    clients: int
    accepted: int = 0
    refused: int = 0
    retried: int = 0
    lost_responses: int = 0
    kills: int = 0
    seconds: float = 0.0
    failures: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        lines = [
            f"{self.transfers} transfers from {self.clients} clients in {self.seconds:.1f}s, "
            f"server killed {self.kills} times",
            f"  {self.accepted} accepted, {self.refused} refused for insufficient funds",
            f"  {self.retried} needed at least one retry after the connection dropped",
            f"  {self.lost_responses} had been posted before their response was lost; "
            "the retry got the original back",
        ]
        lines += [f"  FAILED: {failure}" for failure in self.failures] or ["  every check passed"]
        return "\n".join(lines)


class Server:
    """The server in its own process, so that killing it is real."""

    def __init__(self, ledger: str, log):
        self.ledger = ledger
        self.log = log
        self.port = 0

    def start(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "zerosum", "serve", self.ledger, "--port", str(self.port)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=self.log, text=True,
        )
        # The first start picks a free port; restarts reuse it, so clients
        # keep the same address throughout.
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"the server didn't start; see {self.log.name}")
        self.port = int(line.rsplit(":", 1)[1])

    def kill(self) -> None:
        self.process.kill()
        self.process.wait()
        self.process.stdout.close()

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.port}"


def send(address: str, method: str, path: str, body=None, key: str | None = None) -> tuple[int, dict]:
    """Make one request and return (status, parsed body). A bytes body is
    sent as it is; anything else is sent as JSON. Raises OSError or
    http.client.HTTPException if the connection fails."""
    if body is not None and not isinstance(body, bytes):
        body = json.dumps(body).encode()
    connection = http.client.HTTPConnection(address, timeout=10)
    try:
        connection.request(method, path, body, {"Idempotency-Key": key} if key else {})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def answer(server: Server, method: str, path: str, body=None, key: str | None = None) -> tuple[int, dict, int]:
    """Send a request until it gets a response that isn't a server error,
    returning (status, body, failed attempts first)."""
    deadline = time.monotonic() + RETRY_FOR
    failed = 0
    while True:
        try:
            status, response = send(server.address, method, path, body, key)
            if status < 500:
                return status, response, failed
        except (OSError, http.client.HTTPException):
            pass
        failed += 1
        if time.monotonic() > deadline:
            raise TimeoutError(f"no answer to {method} {path} for {RETRY_FOR}s")
        time.sleep(0.02)


def move(source: str, destination: str, amount: int, memo: str = "") -> dict:
    return {"legs": [{"account": source, "amount": -amount}, {"account": destination, "amount": amount}], "memo": memo}


def workload(transfers: int, seed: int) -> list[tuple[str, dict]]:
    """Random transfers between customers, as (key, request) pairs. Amounts
    are large enough that some will be refused for insufficient funds."""
    rng = random.Random(seed)
    jobs = []
    for i in range(transfers):
        source, destination = rng.sample(CUSTOMERS, 2)
        jobs.append((f"transfer-{i}", move(source, destination, rng.randint(1, 20_000), f"transfer {i}")))
    return jobs


def run(workdir: str, transfers: int = 2000, clients: int = 16, kills: int = 5, seed: int = 1) -> Report:
    report = Report(transfers, clients)
    ledger = str(Path(workdir) / "crashtest.db")
    jobs = workload(transfers, seed)
    results = {}  # key -> (status, body, failed attempts)
    started = time.monotonic()

    with open(Path(workdir) / "server.log", "w") as log:
        server = Server(ledger, log)
        server.start()
        try:
            answer(server, "POST", "/accounts", {"id": "funding", "currency": "AUD", "allow_negative": True})
            for customer in CUSTOMERS:
                answer(server, "POST", "/accounts", {"id": customer, "currency": "AUD"})
                answer(server, "POST", "/transfers", move("funding", customer, OPENING_BALANCE), f"open-{customer}")

            def client(job: tuple[str, dict]) -> None:
                key, body = job
                results[key] = answer(server, "POST", "/transfers", body, key)

            def killer(stop: threading.Event) -> None:
                # Spread the kills evenly through the run, by progress
                # rather than by time, so they always land under load.
                for n in range(1, kills + 1):
                    while len(results) < transfers * n // (kills + 1):
                        if stop.wait(0.001):
                            return
                    server.kill()
                    server.start()
                    report.kills += 1

            stop = threading.Event()
            killing = threading.Thread(target=killer, args=(stop,))
            killing.start()
            try:
                with ThreadPoolExecutor(clients) as pool:
                    list(pool.map(client, jobs))
            finally:
                stop.set()
                killing.join()
        finally:
            server.kill()

    report.seconds = time.monotonic() - started
    check(report, ledger, jobs, results)
    return report


def check(report: Report, ledger: str, jobs: list[tuple[str, dict]], results: dict) -> None:
    expected = {customer: OPENING_BALANCE for customer in CUSTOMERS}
    expected["funding"] = -OPENING_BALANCE * len(CUSTOMERS)
    acknowledged = {}
    for key, body in jobs:
        status, response, failed = results[key]
        report.retried += failed > 0
        if status in (200, 201):
            report.accepted += 1
            # Each key belongs to one client, so a 200 can only mean an
            # earlier attempt was posted and its response never arrived.
            report.lost_responses += status == 200
            acknowledged[key] = response["id"]
            for leg in body["legs"]:
                expected[leg["account"]] += leg["amount"]
        elif status == 422 and response["error"].startswith("insufficient funds"):
            report.refused += 1
        else:
            report.failures.append(f"{key} got an unexpected {status}: {response}")

    report.failures += [f"audit: {problem}" for problem in audit(ledger)]

    uri = Path(ledger).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        posted = dict(db.execute("SELECT idempotency_key, id FROM transfers WHERE idempotency_key LIKE 'transfer-%'"))
        balances = dict(db.execute("SELECT id, balance FROM accounts"))

    if lost := sorted(acknowledged.keys() - posted.keys()):
        report.failures.append(f"acknowledged but not in the ledger: {lost}")
    if extra := sorted(posted.keys() - acknowledged.keys()):
        report.failures.append(f"in the ledger but never acknowledged: {extra}")
    if moved := sorted(k for k in acknowledged.keys() & posted.keys() if acknowledged[k] != posted[k]):
        report.failures.append(f"acknowledged under a different id than the ledger has: {moved}")
    for account, balance in expected.items():
        if balances.get(account) != balance:
            report.failures.append(f"{account}: the ledger says {balances.get(account)}, the clients expect {balance}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m zerosum.crashtest", description=__doc__.split("\n\n")[0])
    parser.add_argument("--transfers", type=int, default=2000)
    parser.add_argument("--clients", type=int, default=16)
    parser.add_argument("--kills", type=int, default=5)
    parser.add_argument("--seed", type=int, default=1, help="changes which transfers are sent, not when the kills land")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as workdir:
        report = run(workdir, args.transfers, args.clients, args.kills, args.seed)
    print(report)
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
