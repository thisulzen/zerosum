"""python -m zerosum serve LEDGER [--host H] [--port P]
python -m zerosum audit LEDGER
"""

import argparse
import sys

from zerosum.audit import audit
from zerosum.ledger import Ledger
from zerosum.server import LedgerServer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zerosum", description="A double-entry ledger over HTTP.")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the HTTP API over a ledger file, creating it if needed")
    serve.add_argument("ledger")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000, help="0 picks a free port")
    check = commands.add_parser("audit", help="check a ledger file; exits 1 if anything is wrong")
    check.add_argument("ledger")
    args = parser.parse_args(argv)

    if args.command == "serve":
        server = LedgerServer(Ledger(args.ledger), (args.host, args.port))
        # The crash test reads this line to know the server is listening.
        print(f"serving {args.ledger} on http://{args.host}:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    try:
        problems = audit(args.ledger)
    except FileNotFoundError:
        print(f"no ledger at {args.ledger}", file=sys.stderr)
        return 2
    for problem in problems:
        print(problem)
    print(f"{args.ledger}: {len(problems)} problems" if problems else f"{args.ledger}: consistent")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
