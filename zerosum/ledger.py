"""A double-entry ledger on SQLite.

Money is never stored as a single number that gets edited. A transfer is a
set of legs, each one an entry against an account, and the legs of a
transfer must sum to zero. An account's balance is the sum of its entries.

The rules that matter live in the schema, where no code path can skip
them, including ones that don't exist yet. The tables are STRICT, so an
amount can't be stored as text or as a float. A CHECK constraint refuses
any update that would take an account below zero unless it is allowed to
go negative. Triggers make the journal append-only, so fixing a mistake
means posting another transfer.

Each account also carries its current balance in a column, because summing
the journal on every overdraft check would get slower forever. That column
is a cache of the journal and the audit module checks that the two agree.

Every transfer carries an idempotency key chosen by the client. A client
that sends a transfer and never hears back can't tell whether it was posted,
so it sends it again with the same key, and the ledger hands back the
original instead of posting it twice.

Amounts are integers in the currency's minor unit (cents for AUD). Nothing
here does floating-point arithmetic on money.
"""

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager

# The largest integer a JavaScript client can parse from JSON without
# silently rounding it. Above this, the number a client sends and the
# number it reads back could differ.
MAX_AMOUNT = 2**53 - 1
MAX_LEGS = 100
BUSY_TIMEOUT = 30  # seconds a writer waits for the lock before giving up

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    currency TEXT NOT NULL,
    allow_negative INTEGER NOT NULL DEFAULT 0,
    balance INTEGER NOT NULL DEFAULT 0,
    CHECK (allow_negative OR balance >= 0)
) STRICT;

CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    memo TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) STRICT;

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    transfer_id INTEGER NOT NULL REFERENCES transfers(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    amount INTEGER NOT NULL CHECK (amount != 0)
) STRICT;

CREATE INDEX IF NOT EXISTS entries_by_transfer ON entries(transfer_id);
CREATE INDEX IF NOT EXISTS entries_by_account ON entries(account_id);

CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries
BEGIN SELECT RAISE(ABORT, 'entries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries
BEGIN SELECT RAISE(ABORT, 'entries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS transfers_no_update BEFORE UPDATE ON transfers
BEGIN SELECT RAISE(ABORT, 'transfers are append-only'); END;
CREATE TRIGGER IF NOT EXISTS transfers_no_delete BEFORE DELETE ON transfers
BEGIN SELECT RAISE(ABORT, 'transfers are append-only'); END;
"""


class Rejected(Exception):
    """The request is well-formed but the ledger won't post it."""


class Conflict(Exception):
    """The request clashes with something already in the ledger."""


class NotFound(Exception):
    """No such account."""


class Ledger:
    """A ledger stored in one SQLite file.

    Every call opens its own connection, so one Ledger can be shared by any
    number of threads. SQLite allows one writer at a time, so writers queue
    behind each other for up to BUSY_TIMEOUT seconds.
    """

    def __init__(self, path: str):
        self.path = path
        with closing(self._connect()) as db:
            # WAL lets the audit and balance reads run while a transfer is
            # being written, instead of waiting for it.
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None turns off the sqlite3 module's habit of
        # opening transactions behind your back, so every transaction here
        # is one written out explicitly.
        db = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    @contextmanager
    def _write(self):
        # BEGIN IMMEDIATE takes the write lock before the first read. A plain
        # BEGIN starts as a reader and upgrades on its first write, and in
        # WAL mode that upgrade fails at once, without waiting out the busy
        # timeout, if another transfer committed in between. Under 50
        # concurrent withdrawals, 45 failed that way.
        # ponytail: one writer at a time for the whole ledger, and one WAL
        # sync per transfer. Group commits, or Postgres with row locks, once
        # that ceiling matters.
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def open_account(self, account_id: str, currency: str, allow_negative: bool = False) -> tuple[dict, bool]:
        """Open an account, returning (account, created).

        Opening an account that already exists with the same settings is not
        an error, so a client can safely retry after a lost response. The
        same id with different settings raises Conflict.
        """
        if not isinstance(account_id, str) or not account_id or len(account_id) > 64:
            raise Rejected("account id must be a string of 1 to 64 characters")
        if not isinstance(currency, str) or len(currency) != 3 or not currency.isupper():
            raise Rejected("currency must be a three-letter code such as AUD")
        if not isinstance(allow_negative, bool):
            raise Rejected("allow_negative must be true or false")

        with self._write() as db:
            existing = db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if existing:
                if (existing["currency"], bool(existing["allow_negative"])) != (currency, allow_negative):
                    raise Conflict(f"account {account_id} already exists with different settings")
                return _account(existing), False
            db.execute(
                "INSERT INTO accounts (id, currency, allow_negative) VALUES (?, ?, ?)",
                (account_id, currency, int(allow_negative)),
            )
            return _account(db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()), True

    def account(self, account_id: str) -> dict:
        """Return an account and its balance. Raises NotFound."""
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise NotFound(f"no account {account_id}")
        return _account(row)

    def transfer(self, key: str, legs: list, memo: str = "") -> tuple[dict, bool]:
        """Post a transfer, returning (transfer, created).

        `legs` is a list of {"account": id, "amount": minor_units} with at
        least two entries that sum to zero, all in one currency, each account
        appearing once. Negative amounts are debits.

        If `key` has been used before for the same legs and memo, nothing is
        posted and the original transfer comes back with created=False. That
        holds even if the accounts could no longer afford it, because it
        already happened. The same key with a different request is Rejected.

        A rejected transfer doesn't use up its key. Retrying it is asking
        again, and the answer can change once the account has the money.

        Raises Rejected if the legs are invalid, an account doesn't exist, or
        an account would go below zero. Nothing is written in that case.
        """
        if not isinstance(key, str) or not key or len(key) > 255:
            raise Rejected("idempotency key must be a string of 1 to 255 characters")
        parsed = _parse_legs(legs)
        if not isinstance(memo, str) or len(memo) > 200:
            raise Rejected("memo must be a string of at most 200 characters")
        # Legs are hashed after sorting, so the same transfer with its legs
        # in a different order still counts as the same request.
        request_hash = hashlib.sha256(json.dumps([parsed, memo]).encode()).hexdigest()

        with self._write() as db:
            seen = db.execute(
                "SELECT id, request_hash FROM transfers WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if seen:
                if seen["request_hash"] != request_hash:
                    raise Rejected(f"idempotency key {key} was already used for a different transfer")
                return _load_transfer(db, seen["id"]), False

            names = [account for account, _ in parsed]
            rows = db.execute(
                f"SELECT id, currency FROM accounts WHERE id IN ({','.join('?' * len(names))})", names
            ).fetchall()
            missing = set(names) - {row["id"] for row in rows}
            if missing:
                raise Rejected(f"no account {', '.join(sorted(missing))}")
            if len({row["currency"] for row in rows}) > 1:
                raise Rejected("all legs of a transfer must be in the same currency")

            transfer_id = db.execute(
                "INSERT INTO transfers (idempotency_key, request_hash, memo) VALUES (?, ?, ?)",
                (key, request_hash, memo),
            ).lastrowid
            for account, amount in parsed:
                db.execute(
                    "INSERT INTO entries (transfer_id, account_id, amount) VALUES (?, ?, ?)",
                    (transfer_id, account, amount),
                )
                _apply(db, account, amount)
            return _load_transfer(db, transfer_id), True


def _parse_legs(legs) -> list[tuple[str, int]]:
    """Check the shape of a transfer's legs and return them as sorted
    (account, amount) pairs. Raises Rejected."""
    if not isinstance(legs, list) or not 2 <= len(legs) <= MAX_LEGS:
        raise Rejected(f"a transfer needs between 2 and {MAX_LEGS} legs")
    parsed = []
    for leg in legs:
        if not isinstance(leg, dict) or set(leg) != {"account", "amount"}:
            raise Rejected("each leg must be an object with exactly an account and an amount")
        account, amount = leg["account"], leg["amount"]
        if not isinstance(account, str) or not account:
            raise Rejected("a leg's account must be a non-empty string")
        # type() rather than isinstance(), because bool is a subclass of int
        # and True would otherwise post as one cent.
        if type(amount) is not int or amount == 0 or abs(amount) > MAX_AMOUNT:
            raise Rejected(f"a leg's amount must be a non-zero whole number of minor units up to {MAX_AMOUNT}")
        parsed.append((account, amount))
    if len({account for account, _ in parsed}) != len(parsed):
        raise Rejected("an account can appear only once in a transfer")
    if sum(amount for _, amount in parsed) != 0:
        raise Rejected("the legs of a transfer must sum to zero")
    return sorted(parsed)


def _apply(db: sqlite3.Connection, account: str, amount: int) -> None:
    # The overdraft rule is the CHECK constraint on accounts. Updating one
    # account at a time means that when it fires, we know whose it was.
    try:
        db.execute("UPDATE accounts SET balance = balance + ? WHERE id = ?", (amount, account))
    except sqlite3.IntegrityError as e:
        if e.sqlite_errorname == "SQLITE_CONSTRAINT_CHECK":
            raise Rejected(f"insufficient funds in {account}") from None
        raise Rejected(f"{account} can't take this amount: {e}") from None


def _account(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "currency": row["currency"],
        "allow_negative": bool(row["allow_negative"]),
        "balance": row["balance"],
    }


def _load_transfer(db: sqlite3.Connection, transfer_id: int) -> dict:
    transfer = db.execute("SELECT * FROM transfers WHERE id = ?", (transfer_id,)).fetchone()
    legs = db.execute(
        "SELECT account_id, amount FROM entries WHERE transfer_id = ? ORDER BY id", (transfer_id,)
    ).fetchall()
    return {
        "id": transfer["id"],
        "idempotency_key": transfer["idempotency_key"],
        "memo": transfer["memo"],
        "created_at": transfer["created_at"],
        "legs": [{"account": leg["account_id"], "amount": leg["amount"]} for leg in legs],
    }
