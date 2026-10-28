import sqlite3
import uuid

import pytest

from zerosum.ledger import MAX_AMOUNT, Conflict, Ledger, NotFound, Rejected, move


def post(ledger: Ledger, legs: list, memo: str = "") -> dict:
    """Post a transfer under a fresh key, for tests that aren't about keys."""
    return ledger.transfer(str(uuid.uuid4()), legs, memo)[0]


@pytest.fixture
def ledger(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    ledger.open_account("funding", "AUD", allow_negative=True)
    ledger.open_account("alice", "AUD")
    ledger.open_account("bob", "AUD")
    post(ledger, move("funding", "alice", 10_000))
    return ledger


def balance(ledger: Ledger, account: str) -> int:
    return ledger.account(account)["balance"]


def test_transfer_moves_money_between_accounts(ledger):
    post(ledger, move("alice", "bob", 2_500))
    assert balance(ledger, "alice") == 7_500
    assert balance(ledger, "bob") == 2_500


def test_transfer_returns_its_legs(ledger):
    transfer = post(ledger, move("alice", "bob", 100), memo="rent")
    assert transfer["memo"] == "rent"
    assert sorted((leg["account"], leg["amount"]) for leg in transfer["legs"]) == [("alice", -100), ("bob", 100)]


def test_three_leg_transfer(ledger):
    # A card payment with a fee: the customer pays 1000, the merchant gets
    # 980 and the fee account gets 20. Double entry doesn't mean two legs.
    ledger.open_account("fees", "AUD")
    post(ledger, [
        {"account": "alice", "amount": -1_000},
        {"account": "bob", "amount": 980},
        {"account": "fees", "amount": 20},
    ])
    assert [balance(ledger, a) for a in ("alice", "bob", "fees")] == [9_000, 980, 20]


def test_unbalanced_transfer_is_rejected(ledger):
    with pytest.raises(Rejected, match="sum to zero"):
        post(ledger, [{"account": "alice", "amount": -100}, {"account": "bob", "amount": 99}])


def test_single_leg_is_rejected(ledger):
    with pytest.raises(Rejected, match="between 2 and"):
        post(ledger, [{"account": "alice", "amount": 0}])


def test_too_many_legs_is_rejected(ledger):
    legs = [{"account": "alice", "amount": -100}] + [{"account": f"x{i}", "amount": 1} for i in range(100)]
    with pytest.raises(Rejected, match="between 2 and 100"):
        post(ledger, legs)


def test_zero_amount_leg_is_rejected(ledger):
    with pytest.raises(Rejected, match="non-zero"):
        post(ledger, [
            {"account": "alice", "amount": -100},
            {"account": "bob", "amount": 100},
            {"account": "funding", "amount": 0},
        ])


@pytest.mark.parametrize("amount", [1.5, "100", True, None, MAX_AMOUNT + 1])
def test_amount_must_be_a_safe_integer(ledger, amount):
    with pytest.raises(Rejected, match="whole number"):
        post(ledger, [{"account": "alice", "amount": amount}, {"account": "bob", "amount": amount}])


def test_same_account_twice_is_rejected(ledger):
    with pytest.raises(Rejected, match="only once"):
        post(ledger, [
            {"account": "alice", "amount": -100},
            {"account": "alice", "amount": 50},
            {"account": "bob", "amount": 50},
        ])


def test_unknown_account_is_rejected(ledger):
    with pytest.raises(Rejected, match="no account carol"):
        post(ledger, move("alice", "carol", 100))


def test_mixed_currencies_are_rejected(ledger):
    ledger.open_account("nz", "NZD")
    with pytest.raises(Rejected, match="same currency"):
        post(ledger, move("alice", "nz", 100))


def test_overdraft_is_rejected_and_nothing_is_written(ledger):
    # The credit to bob is applied before the debit to alice fails. The
    # rollback has to undo it, or bob would get money from nowhere.
    ledger.open_account("aaron", "AUD")
    with pytest.raises(Rejected, match="insufficient funds in alice"):
        post(ledger, [{"account": "aaron", "amount": 20_000}, {"account": "alice", "amount": -20_000}])
    assert balance(ledger, "alice") == 10_000
    assert balance(ledger, "aaron") == 0


def test_account_can_be_emptied_exactly(ledger):
    post(ledger, move("alice", "bob", 10_000))
    assert balance(ledger, "alice") == 0


def test_account_allowed_negative_can_go_negative(ledger):
    assert balance(ledger, "funding") == -10_000


def test_balance_overflow_is_refused_not_rounded(ledger):
    # SQLite turns an integer that overflows 64 bits into a float. On a
    # STRICT table that is refused instead of being stored as a rounded
    # balance. Alice started 10,000 ahead, so she hits the limit first.
    for _ in range(1_025):
        try:
            post(ledger, move("funding", "alice", MAX_AMOUNT))
        except Rejected as e:
            assert "alice can't take this amount" in str(e)
            break
    else:
        pytest.fail("no balance ever hit the 64-bit limit")
    assert type(balance(ledger, "alice")) is int
    assert balance(ledger, "alice") + balance(ledger, "funding") == 0


def test_entries_are_append_only(ledger):
    db = sqlite3.connect(ledger.path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE entries SET amount = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM entries")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM transfers")


def test_reopening_an_account_with_the_same_settings_is_not_an_error(ledger):
    account, created = ledger.open_account("alice", "AUD")
    assert not created
    assert account["balance"] == 10_000


def test_reopening_an_account_with_different_settings_conflicts(ledger):
    with pytest.raises(Conflict):
        ledger.open_account("alice", "AUD", allow_negative=True)


@pytest.mark.parametrize("currency", ["aud", "AU", "AUDD", 36])
def test_currency_must_be_a_three_letter_code(ledger, currency):
    with pytest.raises(Rejected, match="currency"):
        ledger.open_account("carol", currency)


def test_unknown_account_lookup(ledger):
    with pytest.raises(NotFound):
        ledger.account("carol")


def test_ledger_survives_reopening(ledger):
    assert balance(Ledger(ledger.path), "alice") == 10_000


def test_retry_with_the_same_key_posts_once(ledger):
    first, created = ledger.transfer("k1", move("alice", "bob", 100))
    again, created_again = ledger.transfer("k1", move("alice", "bob", 100))
    assert (created, created_again) == (True, False)
    assert again == first
    assert balance(ledger, "bob") == 100


def test_retry_replays_even_when_it_could_no_longer_be_afforded(ledger):
    # The retry is a question about a transfer that already happened, not a
    # new request, so it must not be judged against today's balance.
    ledger.transfer("all-of-it", move("alice", "bob", 10_000))
    _, created = ledger.transfer("all-of-it", move("alice", "bob", 10_000))
    assert not created
    assert balance(ledger, "alice") == 0


def test_same_key_with_different_amount_is_rejected(ledger):
    ledger.transfer("k1", move("alice", "bob", 100))
    with pytest.raises(Rejected, match="already used"):
        ledger.transfer("k1", move("alice", "bob", 101))
    assert balance(ledger, "bob") == 100


def test_same_key_with_different_memo_is_rejected(ledger):
    ledger.transfer("k1", move("alice", "bob", 100), memo="rent")
    with pytest.raises(Rejected, match="already used"):
        ledger.transfer("k1", move("alice", "bob", 100), memo="bond")


def test_leg_order_does_not_change_the_request(ledger):
    ledger.transfer("k1", move("alice", "bob", 100))
    _, created = ledger.transfer("k1", list(reversed(move("alice", "bob", 100))))
    assert not created


def test_rejected_transfer_does_not_use_up_its_key(ledger):
    with pytest.raises(Rejected, match="insufficient funds"):
        ledger.transfer("k1", move("bob", "alice", 500))
    post(ledger, move("alice", "bob", 500))
    _, created = ledger.transfer("k1", move("bob", "alice", 500))
    assert created


@pytest.mark.parametrize("key", ["", None, "k" * 256])
def test_key_must_be_a_reasonable_string(ledger, key):
    with pytest.raises(Rejected, match="idempotency key"):
        ledger.transfer(key, move("alice", "bob", 100))
