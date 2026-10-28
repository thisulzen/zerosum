import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from zerosum.crashtest import send
from zerosum.ledger import Ledger, move
from zerosum.server import LedgerServer


@pytest.fixture
def base(tmp_path):
    server = LedgerServer(Ledger(str(tmp_path / "ledger.db")), ("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    address = f"127.0.0.1:{server.server_port}"
    send(address, "POST", "/accounts", {"id": "funding", "currency": "AUD", "allow_negative": True})
    send(address, "POST", "/accounts", {"id": "alice", "currency": "AUD"})
    send(address, "POST", "/accounts", {"id": "bob", "currency": "AUD"})
    send(address, "POST", "/transfers", {"legs": move("funding", "alice", 1_000)}, key="opening")
    yield address
    server.shutdown()
    server.server_close()
    thread.join()


def balance(address: str, account: str) -> int:
    return send(address, "GET", f"/accounts/{account}")[1]["balance"]


def test_account_shows_its_balance(base):
    assert send(base, "GET", "/accounts/alice") == (
        200,
        {"id": "alice", "currency": "AUD", "allow_negative": False, "balance": 1_000},
    )


def test_new_account_is_201_and_retry_is_200(base):
    assert send(base, "POST", "/accounts", {"id": "carol", "currency": "AUD"})[0] == 201
    assert send(base, "POST", "/accounts", {"id": "carol", "currency": "AUD"})[0] == 200
    assert send(base, "POST", "/accounts", {"id": "carol", "currency": "NZD"})[0] == 409


def test_transfer_and_its_retry(base):
    status, first = send(base, "POST", "/transfers", {"legs": move("alice", "bob", 300)}, key="k1")
    assert status == 201
    status, again = send(base, "POST", "/transfers", {"legs": move("alice", "bob", 300)}, key="k1")
    assert (status, again) == (200, first)
    assert balance(base, "bob") == 300


def test_transfer_without_key_is_refused(base):
    status, body = send(base, "POST", "/transfers", {"legs": move("alice", "bob", 300)})
    assert status == 400
    assert "Idempotency-Key" in body["error"]


def test_key_reused_for_a_different_transfer(base):
    send(base, "POST", "/transfers", {"legs": move("alice", "bob", 300)}, key="k1")
    assert send(base, "POST", "/transfers", {"legs": move("alice", "bob", 400)}, key="k1")[0] == 422


def test_insufficient_funds(base):
    assert send(base, "POST", "/transfers", {"legs": move("alice", "bob", 1_001)}, key="k1") == (
        422,
        {"error": "insufficient funds in alice"},
    )


@pytest.mark.parametrize(
    "path, status",
    [("/accounts/carol", 404), ("/accounts/", 404), ("/nowhere", 404), ("/accounts/alice/entries", 404)],
)
def test_unknown_paths(base, path, status):
    assert send(base, "GET", path)[0] == status


@pytest.mark.parametrize(
    "raw, status",
    [(b"{not json", 400), (b"[1, 2]", 400), (b"\xff\xfe", 400), (b" " * 70_000, 413)],
)
def test_bad_bodies(base, raw, status):
    assert send(base, "POST", "/transfers", raw, key="k1")[0] == status


def test_account_ids_are_url_decoded(base):
    assert send(base, "POST", "/accounts", {"id": "savings/joint", "currency": "AUD"})[0] == 201
    assert send(base, "GET", "/accounts/savings%2Fjoint")[1]["id"] == "savings/joint"


def test_concurrent_withdrawals_never_overdraw(base):
    # 50 withdrawals of 30 against a balance of 1,000: exactly 33 can be
    # paid, and the other 17 must be refused rather than overdraw.
    with ThreadPoolExecutor(max_workers=50) as pool:
        statuses = list(pool.map(
            lambda i: send(base, "POST", "/transfers", {"legs": move("alice", "bob", 30)}, key=f"w{i}")[0], range(50)
        ))
    assert (statuses.count(201), statuses.count(422)) == (33, 17)
    assert (balance(base, "alice"), balance(base, "bob")) == (10, 990)


def test_same_key_sent_concurrently_posts_once(base):
    # A client that retries aggressively can have several copies of one
    # request in flight at once. Exactly one of them may post it.
    with ThreadPoolExecutor(max_workers=20) as pool:
        responses = list(pool.map(
            lambda _: send(base, "POST", "/transfers", {"legs": move("alice", "bob", 100)}, key="once"), range(20)
        ))
    assert sorted(status for status, _ in responses) == [200] * 19 + [201]
    assert len({body["id"] for _, body in responses}) == 1
    assert balance(base, "bob") == 100
