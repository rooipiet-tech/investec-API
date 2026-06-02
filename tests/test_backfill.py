import json

from invespend.db import (
    assign_day_seq,
    backfill_transaction_hashes,
    transaction_hash,
)


class _FakeCursor:
    """Minimal psycopg-cursor stand-in for exercising the backfill logic."""

    def __init__(self, store):
        self._store = store
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        head = sql.strip().lower()
        if head.startswith("select"):
            self._store["fetch"] = list(self._store["rows"])
        elif head.startswith("update"):
            new_hash, day_seq, old_hash = params
            # Apply the update to our in-memory rows, like Postgres would.
            hit = 0
            for row in self._store["rows"]:
                if row[1] == old_hash:
                    self._store["rows"][self._store["rows"].index(row)] = (
                        row[0], new_hash, row[2]
                    )
                    hit = 1
            self._store["updates"].append((old_hash, new_hash, day_seq))
            self.rowcount = hit

    def fetchall(self):
        return self._store["fetch"]


class _FakeConn:
    def __init__(self, rows):
        self.store = {"rows": list(rows), "updates": [], "fetch": []}

    def cursor(self):
        return _FakeCursor(self.store)


def _raw(amount, desc, value_date="2026-05-26", action_date="2026-05-26"):
    return json.dumps({
        "valueDate": value_date,
        "actionDate": action_date,
        "amount": amount,
        "description": desc,
    })


def test_backfill_rekeys_identical_same_day_rows():
    # Two identical R45 coffees stored under arbitrary OLD-style hashes.
    rows = [
        ("ACC1", "old_hash_a", _raw("45.00", "COFFEE SHOP")),
        ("ACC1", "old_hash_b", _raw("45.00", "COFFEE SHOP")),
        ("ACC1", "old_hash_c", _raw("120.00", "LUNCH")),
    ]
    conn = _FakeConn(rows)
    summary = backfill_transaction_hashes(conn)

    assert summary["scanned"] == 3
    assert summary["updated"] == 3  # all three were on old keys

    # The resulting keys are exactly what a fresh ingest would compute.
    txs = [json.loads(r[2]) for r in rows]
    expected = {
        transaction_hash("ACC1", tx, seq)
        for tx, seq in assign_day_seq("ACC1", txs)
    }
    assert {r[1] for r in conn.store["rows"]} == expected
    assert len(expected) == 3  # identical coffees kept distinct via day_seq


def test_backfill_is_idempotent():
    rows = [
        ("ACC1", "old_a", _raw("45.00", "COFFEE SHOP")),
        ("ACC1", "old_b", _raw("45.00", "COFFEE SHOP")),
    ]
    conn = _FakeConn(rows)
    backfill_transaction_hashes(conn)
    # Second pass over the now-rekeyed rows changes nothing.
    summary2 = backfill_transaction_hashes(conn)
    assert summary2["updated"] == 0
