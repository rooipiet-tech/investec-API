"""Offline tests for beneficiary snapshot sync + match persistence.

No live DB: a fake connection records executed SQL and serves canned rows for
the read queries. ``db._open`` is patched so ``db.connect()`` runs against the
fake. Synthetic data only.
"""
import logging
import re
from datetime import datetime, timezone

import pytest

from invespend import beneficiary_sync, db
from invespend.beneficiary_match import MatchResult

FIXED_NOW = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
FAKE_ACCT = "9999 0000 1234"
FAKE_ACCT_DIGITS = "999900001234"
OWN_ACCT = "99990000555"

API_BENES = [
    {"beneficiaryId": "ben-acme", "beneficiaryName": "Acme Trading", "bank": "Test Bank",
     "accountNumber": FAKE_ACCT, "cellNo": "0000000000",
     "emailAddress": "nobody@example.invalid"},
    {"beneficiaryId": "ben-dup1", "beneficiaryName": "Dup Payee"},
    {"beneficiaryId": "ben-dup2", "beneficiaryName": "Dup Payee"},
    {"beneficiaryId": "ben-own", "beneficiaryName": "My Savings",
     "accountNumber": "9999 0000 555"},
]

TX_ROWS = [
    ("h1", "DEBIT", "OnlineBankingPayments", "PAYMENT TO ACME TRADING", -100, "external_outflow"),
    ("h2", "DEBIT", "FasterPay", "DUP PAYEE", -20, "external_outflow"),
    ("h3", "DEBIT", "OnlineBankingTransfers", "Nobody Known", -5, "external_outflow"),
    ("h4", "DEBIT", "OnlineBankingTransfers", "Transfer to 9999 0000 555", -7, "external_outflow"),
]


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.events.append("sql")
        self._conn.sql.append((sql, params))
        low = " ".join(sql.lower().split())
        self._rows = []
        if low.startswith("select account_number from accounts"):
            self._rows = [(n,) for n in self._conn.state["own"]]
        elif low.startswith("select beneficiary_id,") and "from beneficiaries" in low:
            self._rows = list(self._conn.state["benes"])
        elif "from transactions_flow" in low:
            self._rows = list(self._conn.state["txs"])
        elif low.startswith("insert"):
            self.rowcount = low.count("),") + 1
        elif low.startswith("update"):
            self.rowcount = self._conn.state.get("retired", 0)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, state, events, sql):
        self.state = state
        self.events = events
        self.sql = sql

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")

    def close(self):
        self.events.append("close")


class _Client:
    def __init__(self, data, events, raises=None):
        self._data = data
        self._events = events
        self._raises = raises
        self.calls = 0

    def get_beneficiaries(self):
        self.calls += 1
        self._events.append("api")
        if self._raises:
            raise self._raises
        return self._data


def _bene_rows(api):
    from invespend.beneficiary_match import parse_beneficiaries
    return [(r.beneficiary_id, r.beneficiary_name, r.name, r.reference_name, r.bank,
             r.branch_code, r.account_number) for r in parse_beneficiaries(api)]


@pytest.fixture
def fake_db(monkeypatch):
    events, sql = [], []
    state = {"own": [OWN_ACCT], "benes": _bene_rows(API_BENES), "txs": list(TX_ROWS)}

    def _open(url, attempts=4):
        events.append("open")
        return _FakeConn(state, events, sql)

    monkeypatch.setattr(db, "_open", _open)
    return {"events": events, "sql": sql, "state": state}


def _run(fake_db, data=API_BENES, raises=None):
    client = _Client(data, fake_db["events"], raises)
    out = beneficiary_sync.sync_and_match(client, "postgresql://fake", now=lambda: FIXED_NOW)
    return client, out


def _writes(sql_log):
    return [(s, p) for s, p in sql_log
            if s.strip().lower().startswith(("insert", "update", "delete"))]


def _target(sql):
    m = re.match(r"\s*(?:insert into|update|delete from)\s+(\w+)", sql, re.I)
    return m.group(1)


def test_sync_api_before_connect(fake_db):
    _run(fake_db)
    ev = fake_db["events"]
    assert ev[0] == "api"
    assert ev.index("api") < ev.index("open")
    assert ev.count("api") == 1


@pytest.mark.parametrize("data, raises", [
    (None, RuntimeError("boom")),
    ({"data": []}, None),
    (None, None),
    ("junk", None),
    ([], None),
    (["junk", {"name": "no id"}], None),
])
def test_sync_fetch_raises_or_malformed_no_writes(fake_db, caplog, data, raises):
    caplog.set_level(logging.WARNING)
    if raises:
        with pytest.raises(RuntimeError):
            _run(fake_db, data, raises)
    else:
        assert _run(fake_db, data)[1] == {}
        assert "snapshot kept" in caplog.text
    assert fake_db["sql"] == []
    assert "open" not in fake_db["events"]


def test_sync_writes_snapshot_and_matches(fake_db):
    client, out = _run(fake_db)
    assert client.calls == 1
    targets = [_target(s) for s, _ in _writes(fake_db["sql"])]
    assert targets == ["beneficiaries", "beneficiaries", "beneficiary_matches"]
    assert out["matched"] == 1 and out["ambiguous"] == 1 and out["no_candidate"] == 2
    # Match rows: h1 matched by name, h2 ambiguous, h3 none, h4 own account -> none.
    params = _writes(fake_db["sql"])[-1][1]
    rows = [tuple(params[i:i + 7]) for i in range(0, len(params), 7)]
    assert rows == [
        ("h1", "matched", "ben-acme", "beneficiary_name_exact", "acme trading", 1, FIXED_NOW),
        ("h2", "ambiguous", None, "beneficiary_name_exact", "dup payee", 2, FIXED_NOW),
        ("h3", "no_candidate", None, "none", "", 0, FIXED_NOW),
        ("h4", "no_candidate", None, "none", "", 0, FIXED_NOW),
    ]


def test_snapshot_insert_is_allowlisted(fake_db):
    _run(fake_db)
    ins_sql, ins_params = _writes(fake_db["sql"])[0]
    assert "cell" not in ins_sql.lower() and "email" not in ins_sql.lower()
    assert "0000000000" not in ins_params and "nobody@example.invalid" not in ins_params
    assert FAKE_ACCT in ins_params  # Q1: full number persisted in the new table


def test_sync_soft_retire(fake_db):
    _run(fake_db)
    first_retire = [(s, p) for s, p in _writes(fake_db["sql"]) if s.lower().startswith("update")]
    fake_db["sql"].clear()
    _run(fake_db, [b for b in API_BENES if b["beneficiaryId"] != "ben-dup2"])
    retire = [(s, p) for s, p in _writes(fake_db["sql"]) if s.lower().startswith("update")]
    assert len(first_retire) == 1 and len(retire) == 1
    sql, params = retire[0]
    assert "set active = false" in sql and "not (beneficiary_id = any(%s))" in sql
    assert params == (["ben-acme", "ben-dup1", "ben-own"],)
    for s, _ in fake_db["sql"]:
        assert "delete" not in s.lower()


def test_match_upsert_freeze_sql(fake_db):
    _run(fake_db)
    upsert = [s for s, _ in fake_db["sql"] if "insert into beneficiary_matches" in s][0]
    assert "where beneficiary_matches.status <> 'matched'" in upsert
    assert "is distinct from" in upsert
    cand = [s for s, _ in fake_db["sql"] if "from transactions_flow" in s][0]
    assert "m.status <> 'matched'" in cand
    assert "flow_type is distinct from 'internal_transfer'" in cand


def test_idempotent_rerun(fake_db):
    _run(fake_db)
    first = list(fake_db["sql"])
    fake_db["sql"].clear()
    _run(fake_db)
    assert fake_db["sql"] == first


def test_status_values_persisted(fake_db):
    _run(fake_db)
    params = _writes(fake_db["sql"])[-1][1]
    statuses = {params[i + 1] for i in range(0, len(params), 7)}
    assert statuses == {"matched", "ambiguous", "no_candidate"}


def test_only_new_tables_written(fake_db):
    _run(fake_db)
    for s, _ in _writes(fake_db["sql"]):
        assert _target(s) in ("beneficiaries", "beneficiary_matches")
    for s, _ in fake_db["sql"]:
        low = s.lower()
        for t in ("transactions", "accounts", "balances", "sync_runs", "category_map"):
            assert not re.search(rf"\b(insert into|update|delete from)\s+{t}\b", low)


def test_no_account_digits_logged(fake_db, caplog):
    caplog.set_level(logging.DEBUG)
    _run(fake_db)
    text = caplog.text
    assert "Beneficiary matching:" in text
    for secret in (FAKE_ACCT, FAKE_ACCT_DIGITS, "9999 0000 555", "99990000555",
                   "Acme Trading", "Dup Payee"):
        assert secret not in text


def test_match_upsert_batches_under_param_limit():
    class _Cur:
        def __init__(self, log):
            self.log = log
            self.rowcount = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params):
            self.log.append(params)
            self.rowcount = len(params) // 7

    class _Conn:
        def __init__(self):
            self.log = []

        def cursor(self):
            return _Cur(self.log)

    conn = _Conn()
    results = [MatchResult(f"h{i:05d}", "no_candidate", None, "none", "", 0)
               for i in range(1201)]
    assert db.upsert_beneficiary_matches(conn, results, FIXED_NOW) == 1201
    assert [len(p) // 7 for p in conn.log] == [500, 500, 201]
    assert max(len(p) for p in conn.log) < 65535
