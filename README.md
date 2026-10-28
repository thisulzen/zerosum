# zerosum

A double-entry ledger with an HTTP API, written in Python using only the standard library. It stays correct when clients retry, when requests land at the same moment, and when the server is killed halfway through a write.

```bash
python -m zerosum serve ledger.db --port 8000
```

```bash
curl -X POST localhost:8000/accounts -d '{"id": "funding", "currency": "AUD", "allow_negative": true}'
curl -X POST localhost:8000/accounts -d '{"id": "alice", "currency": "AUD"}'
curl -X POST localhost:8000/transfers -H 'Idempotency-Key: open-alice' \
     -d '{"legs": [{"account": "funding", "amount": -5000}, {"account": "alice", "amount": 5000}]}'
curl localhost:8000/accounts/alice
# {"id": "alice", "currency": "AUD", "allow_negative": false, "balance": 5000}
```

Amounts are whole cents. Send that transfer again with the same key and you get the original back with a 200. Nothing is posted twice.

## Status

Done: accounts, balanced transfers of up to 100 legs, overdraft protection, idempotency keys, an audit command, the HTTP API, and a crash test that kills the server under load. It's all tested, and CI runs the tests and the full crash test on every push.

Not done: most of what you'd need before putting it on a network. The list is at the end.

## Design

### Double entry

A transfer is a set of legs. Each leg is an entry against one account, and the legs of a transfer have to sum to zero. An account's balance is the sum of its entries. A card payment with a fee is three legs: the customer pays 1,000, the merchant receives 980 and the fee account receives 20.

Money can move but it can't appear or vanish, so the balances of every account in a currency add up to zero. That's where the name comes from. Money comes in from outside through an account that's allowed to go negative (`funding` above), which stands in for the rest of the world.

It costs at least two rows every time money moves. What that buys is that every cent can be traced to the transfer that moved it, and a bug that credits one account and forgets to debit the other shows up straight away as a transfer that doesn't sum to zero.

### The database enforces the rules

The rules live in the SQLite schema, where no Python code path can skip them:

- the tables are `STRICT`, so an amount can't be stored as text or as a float;
- a `CHECK` constraint refuses any update that would take an account below zero, unless the account is allowed to go negative;
- triggers make transfers and entries append-only. To fix a mistake you post another transfer.

That covers code nobody has written yet, too. The cost is that someone reading only the Python won't see the rules, and SQLite's errors need translating. To say whose account would have been overdrawn, the ledger updates one account at a time, so it knows which update the constraint stopped.

Each account also stores its current balance in a column, so an overdraft check doesn't have to add up the whole journal again. The journal is the truth and the column is a cache of it, which the audit checks.

### Idempotency keys

When a client sends a transfer and the connection drops, it can't know whether the transfer was posted. If it gives up, a payment might be lost. If it retries blindly, the payment might go through twice.

So every transfer carries an `Idempotency-Key` header chosen by the client, and the ledger stores the key with a hash of the request.

- Same key, same request: nothing is posted, and the original comes back with a 200 instead of a 201. That holds even if the account could no longer afford it, because the transfer has already happened.
- Same key, different request: refused with a 422.
- A refused transfer doesn't use up its key. Retrying it is asking again, and the answer can change once the account has the money.

Opening an account works the same way: the same id with the same settings returns the existing account.

This follows the IETF draft [The Idempotency-Key HTTP Header Field](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/) and the way Stripe's API handles retries. Stripe expires its keys. These are kept forever, so the transfers table only grows.

### One writer at a time

Every write runs in `BEGIN IMMEDIATE`, which takes SQLite's write lock before the first read. "Has this key been used?" and "post the transfer" become one step, so two copies of the same request can't both decide the key is new.

SQLite allows one writer at a time, and that caps throughput. On a 2018 laptop (Intel i5-8259U, macOS 15.7), the crash test with no kills gets about 375 transfers a second through one client and about 735 through sixteen, HTTP included. Past that I'd move to a database with row-level locks, such as Postgres.

### The audit doesn't trust the ledger

`python -m zerosum audit ledger.db` reads the file directly and imports nothing from the ledger code. Its job is to catch that code being wrong, so it can't lean on it. It opens the file read-only and runs every check inside one read transaction, so a live server can't change the data halfway through. It checks that:

- every transfer's legs sum to zero;
- every transfer has at least two legs, all in one currency;
- every entry belongs to an account that exists;
- every account's stored balance equals the sum of its entries;
- no account that may not go negative has entries summing below zero.

It exits 1 if it finds anything, so cron or CI can run it.

## The crash test

`python -m zerosum.crashtest` starts the server as a separate process and sends it 2,000 random transfers between 20 customers from 16 clients. The clients act the way a real client has to: when a connection drops, they wait and send the same request with the same key until they get an answer. Meanwhile the server is killed with `SIGKILL` five times and restarted on the same file. A kill can land before a transfer commits, after it commits but before the response goes out, or between requests.

Once every transfer has an answer, it checks that:

- the audit finds nothing wrong;
- the ledger holds exactly the transfers the clients were told succeeded, each under the id the client was given;
- every balance equals what the clients work out for themselves from the transfers they were told succeeded, without asking the server.

A typical run:

```
2000 transfers from 16 clients in 4.1s, server killed 5 times
  1705 accepted, 295 refused for insufficient funds
  55 needed at least one retry after the connection dropped
  2 had been posted before their response was lost; the retry got the original back
  every check passed
```

The counts differ between runs, because the order transfers arrive in decides which of them can be afforded. Every run has to pass the same checks.

`SIGKILL` only covers the process dying. Surviving a power cut depends on the disk honouring `fsync`, and this test can't check that.

## What testing turned up

Most of these came from breaking something on purpose to see whether a test would notice.

- I swapped `BEGIN IMMEDIATE` for a plain `BEGIN` to see whether it was earning its place, and 45 of 50 concurrent withdrawals failed with `database is locked`. A plain `BEGIN` starts as a reader and upgrades on its first write, and in WAL mode that upgrade fails at once, without waiting out the busy timeout, if another writer committed in between. Overdrafts were still impossible because the `CHECK` constraint held, but most clients got a 500.
- `http.server` listens with a backlog of 5. With 50 clients connecting at once, the extra connection attempts were dropped and had to wait for a retry, and the concurrency test took 3.1 seconds instead of 1.0. The server uses 128 now.
- SQLite quietly turns integer arithmetic that overflows 64 bits into a floating-point number. The test that pushes a balance past the limit shows the `STRICT` table refusing the update, where a normal table would have stored a rounded balance.
- To check the crash test wasn't passing by luck, I broke idempotency by giving every attempt a fresh key. It failed, with three customers' balances off from what the clients expected.

## Running it

Python 3.11 or later, and SQLite 3.37 or later for `STRICT` tables. Tested on Python 3.12, locally and in CI.

```bash
pip install pytest
python -m pytest
python -m zerosum.crashtest --transfers 5000 --kills 10
```

## What's tested

- `tests/test_ledger.py` covers the posting rules: balanced and unbalanced transfers, amounts that aren't safe integers (floats, strings, booleans, 2^53 or more), mixed currencies, overdrafts that have to leave nothing behind, the 64-bit overflow, the append-only triggers, and idempotent retries, including the retry of a transfer the account can no longer afford.
- `tests/test_audit.py` tampers with the database in raw SQL, past the triggers and constraints, once for each check the audit makes. It also checks that the audit leaves the file untouched, and that a mistyped path is an error. Otherwise it would quietly audit an empty new database and pass.
- `tests/test_server.py` covers the HTTP API, bad bodies, 50 concurrent withdrawals that can't all be paid, and one key sent 20 times at once, which has to post exactly once.
- `tests/test_crash.py` runs a smaller crash test, with 400 transfers and three kills.

## Not done

- No authentication and no TLS. Anyone who can reach the port can move money.
- One currency per transfer, and no exchange between currencies.
- No statement or history endpoint. The entries are stored, but nothing serves them yet.
- No holds or pending authorisations, where money is reserved first and settled later.
- Idempotency keys never expire.
- Durability through a power cut is untested.

## Synthetic data only

Every account, name and amount in the tests and the crash test is made up.

## Layout

```
zerosum/
    ledger.py       Schema, posting rules and idempotency
    audit.py        Read-only consistency checks, independent of ledger.py
    server.py       HTTP API over http.server
    crashtest.py    Kills the server under load and checks the result
    __main__.py     python -m zerosum serve | audit
tests/
```

## References

- Kleppmann, *Designing Data-Intensive Applications*, Chapter 7 (Transactions)
- [The Idempotency-Key HTTP Header Field](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/), IETF HTTPAPI working group draft
- SQLite: [STRICT tables](https://www.sqlite.org/stricttables.html), [transactions](https://www.sqlite.org/lang_transaction.html), [write-ahead logging](https://www.sqlite.org/wal.html)
