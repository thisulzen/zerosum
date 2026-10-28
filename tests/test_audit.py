import sqlite3
import uuid

import pytest

from zerosum.audit import audit
from zerosum.ledger import Ledger, move


@pytest.fixture
def path(tmp_path):
    path = tmp_path / "ledger has spaces & symbols.db"
    ledger = Ledger(str(path))
    ledger.open_account("funding", "AUD", allow_negative=True)
    ledger.open_account("alice", "AUD")
    ledger.open_account("bob", "AUD")
    ledger.open_account("nz", "NZD")
    for amount in (10_000, 2_000, 350):
        ledger.transfer(str(uuid.uuid4()), move("funding", "alice", amount))
    ledger.transfer(str(uuid.uuid4()), move("alice", "bob", 5_000))
    return str(path)


def tamper(path: str, *statements: str) -> None:
    """Change the file the way a bug or a person with a SQL prompt could:
    straight past the ledger code, with the safety triggers dropped."""
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA ignore_check_constraints = ON")
    for trigger in ("entries_no_update", "entries_no_delete", "transfers_no_update", "transfers_no_delete"):
        db.execute(f"DROP TRIGGER {trigger}")
    for statement in statements:
        db.execute(statement)
    db.close()


def test_consistent_ledger_has_no_problems(path):
    assert audit(path) == []


def test_empty_ledger_has_no_problems(tmp_path):
    path = str(tmp_path / "empty.db")
    Ledger(path)
    assert audit(path) == []


def test_drifted_balance_is_reported(path):
    tamper(path, "UPDATE accounts SET balance = balance + 1 WHERE id = 'bob'")
    assert audit(path) == ["account bob has a stored balance of 5001 but its entries sum to 5000"]


def test_edited_entry_is_reported(path):
    # Editing one leg breaks two things at once: the transfer no longer
    # balances, and the account's cached balance no longer matches.
    tamper(path, "UPDATE entries SET amount = 4000 WHERE account_id = 'bob'")
    assert audit(path) == [
        "transfer 4 has legs summing to -1000, not zero",
        "account bob has a stored balance of 5000 but its entries sum to 4000",
    ]


def test_deleted_leg_is_reported(path):
    tamper(path, "DELETE FROM entries WHERE account_id = 'bob'", "UPDATE accounts SET balance = 0 WHERE id = 'bob'")
    assert "transfer 4 has 1 legs; a transfer needs at least two" in audit(path)


def test_mixed_currency_transfer_is_reported(path):
    tamper(path, "UPDATE entries SET account_id = 'nz' WHERE account_id = 'bob'")
    assert "transfer 4 mixes currencies: AUD,NZD" in audit(path)


def test_entry_for_missing_account_is_reported(path):
    tamper(path, "UPDATE entries SET account_id = 'ghost' WHERE account_id = 'bob'")
    assert "entry 8 belongs to account ghost, which doesn't exist" in audit(path)


def test_overdrawn_account_is_reported_even_if_its_cache_looks_fine(path):
    tamper(path, "UPDATE entries SET amount = -20000 WHERE account_id = 'alice' AND amount = -5000")
    problems = audit(path)
    assert "account alice may not go negative but its entries sum to -7650" in problems


def test_audit_does_not_change_the_file(path):
    with open(path, "rb") as f:
        before = f.read()
    audit(path)
    with open(path, "rb") as f:
        assert f.read() == before


def test_missing_ledger_is_an_error_not_an_empty_audit(tmp_path):
    # Opening a missing file would create an empty database, which would
    # pass every check. A typo in the path must not look like a clean audit.
    with pytest.raises(FileNotFoundError):
        audit(str(tmp_path / "typo.db"))
