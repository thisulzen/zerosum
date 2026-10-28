"""Check a ledger file against the rules it is supposed to keep.

The audit reads the database directly and imports nothing from ledger.py.
It is meant to catch the ledger code being wrong, so it can't rely on that
code, and it opens the file read-only so it can't make anything worse.

Every check runs inside one read transaction, so a server posting
transfers at the same time can't make the audit compare a balance from one
moment with entries from the next.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

CHECKS = {
    "transfer {} has legs summing to {}, not zero": """
        SELECT transfer_id, SUM(amount) FROM entries
        GROUP BY transfer_id HAVING SUM(amount) != 0
    """,
    "transfer {} has {} legs; a transfer needs at least two": """
        SELECT t.id, COUNT(e.id) FROM transfers t LEFT JOIN entries e ON e.transfer_id = t.id
        GROUP BY t.id HAVING COUNT(e.id) < 2
    """,
    "transfer {} mixes currencies: {}": """
        SELECT e.transfer_id, GROUP_CONCAT(DISTINCT a.currency)
        FROM entries e JOIN accounts a ON a.id = e.account_id
        GROUP BY e.transfer_id HAVING COUNT(DISTINCT a.currency) > 1
    """,
    "entry {} belongs to account {}, which doesn't exist": """
        SELECT e.id, e.account_id FROM entries e LEFT JOIN accounts a ON a.id = e.account_id
        WHERE a.id IS NULL
    """,
    "account {} has a stored balance of {} but its entries sum to {}": """
        SELECT a.id, a.balance, COALESCE(SUM(e.amount), 0)
        FROM accounts a LEFT JOIN entries e ON e.account_id = a.id
        GROUP BY a.id HAVING a.balance != COALESCE(SUM(e.amount), 0)
    """,
    # Checked against the journal, not the stored balance, so an account the
    # cache says is fine still fails if its entries say otherwise.
    "account {} may not go negative but its entries sum to {}": """
        SELECT a.id, SUM(e.amount) FROM accounts a JOIN entries e ON e.account_id = a.id
        WHERE NOT a.allow_negative GROUP BY a.id HAVING SUM(e.amount) < 0
    """,
}


def audit(path: str) -> list[str]:
    """Return a description of every problem found. An empty list means the
    ledger is consistent. Raises FileNotFoundError if there is no ledger."""
    if not Path(path).is_file():
        raise FileNotFoundError(path)
    # mode=ro has to go through a URI, and as_uri() escapes any characters
    # in the path that would otherwise be read as part of the URI.
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as db:
        db.execute("BEGIN")
        problems = [message.format(*row) for message, query in CHECKS.items() for row in db.execute(query)]
        db.execute("COMMIT")
    return problems
