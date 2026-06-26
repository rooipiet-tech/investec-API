"""Offline tests for the Postgres payment-state backend (OQ2 opt-in).

No live DB: a fake psycopg connection/cursor records executed SQL and returns
canned rows, mirroring the fake-connection style in tests/test_backfill.py. We
patch ``db._open`` so ``db.connect()``'s commit/rollback/close run against the
fake.
"""
from decimal import Decimal

import pytest

from invespend import db
from invespend.config import Settings
from invespend.payments import pg_store
from invespend.payments.pg_store import PgPaymentStore


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.sql.append((sql, params))
        head = sql.strip().lower()
        if head.startswith("select"):
            # The conn supplies queued result rows in FIFO order.
            self._conn.last_row = (
                self._conn.results.pop(0) if self._conn.results else None
            )
        elif head.startswith(("insert", "update", "delete")):
            self.rowcount = self._conn.rowcounts.pop(0) if self._conn.rowcounts else 0

    def fetchone(self):
        return self._conn.last_row

    def fetchall(self):
        return self._conn.fetchall_rows.pop(0) if self._conn.fetchall_rows else []

    rowcount = 0


class _FakeConn:
    def __init__(self, results=None, rowcounts=None, fetchall_rows=None):
        self.sql = []
        self.results = list(results or [])
        self.rowcounts = list(rowcounts or [])
        self.fetchall_rows = list(fetchall_rows or [])
        self.last_row = None
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.txn_entered = False

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


@pytest.fixture
def patch_conn(monkeypatch):
    def _install(conn):
        monkeypatch.setattr(db, "_open", lambda url, attempts=4: conn)
        return conn
    return _install


def _sql_text(conn):
    return " ".join(s.lower() for s, _ in conn.sql)


# ── add_pending ────────────────────────────────────────────────────────────
def test_add_pending_on_conflict_do_nothing_then_selects(patch_conn):
    selected = (
        "k1", "pending", Decimal("45.00"), "ZAR", "ben1", "acc1", "001",
        "hash", "nonce1", None, None, None,
    )
    conn = _FakeConn(results=[selected], rowcounts=[1])
    patch_conn(conn)
    store = PgPaymentStore("postgresql://x")
    rec = store.add_pending(
        dedup_key="k1", amount="45.00", currency="ZAR", beneficiary_id="ben1",
        source_account_id="acc1", source_account_last3="001",
        message_id="mid", nonce="nonce1",
    )
    text = _sql_text(conn)
    assert "insert into payment_pending" in text
    assert "on conflict (dedup_key) do nothing" in text
    assert "select" in text  # re-selects the row after the no-op insert
    assert rec["dedup_key"] == "k1"
    assert rec["amount"] == "45.00"
    assert rec["status"] == "pending"


def test_add_pending_is_idempotent_returns_existing(patch_conn):
    # Conflict path: insert hits an existing row (rowcount 0) but select still
    # returns the original pending -> caller sees one stable record.
    existing = (
        "k1", "pending", Decimal("45.00"), "ZAR", "ben1", "acc1", "001",
        "hash", "nonce1", None, None, None,
    )
    conn = _FakeConn(results=[existing], rowcounts=[0])
    patch_conn(conn)
    store = PgPaymentStore("postgresql://x")
    rec = store.add_pending(
        dedup_key="k1", amount="45.00", currency="ZAR", beneficiary_id="ben1",
        source_account_id="acc1", source_account_last3="001",
        message_id="mid", nonce="nonceDIFFERENT",
    )
    assert "on conflict (dedup_key) do nothing" in _sql_text(conn)
    assert rec["nonce"] == "nonce1"  # the stored nonce, not the re-presented one


# ── execute_and_commit ──────────────────────────────────────────────────────
def test_execute_and_commit_blocked_when_over_cap_no_update(patch_conn):
    # Day total already 90; cap 100; amount 20 -> would exceed -> blocked.
    conn = _FakeConn(results=[(Decimal("90.00"),)])
    patch_conn(conn)
    store = PgPaymentStore("postgresql://x")
    out = store.execute_and_commit(
        "k1", "20.00", daily_aggregate_cap=100, execution_mode="dry-run",
        day="2026-06-26",
    )
    assert out["committed"] is False
    assert "exceed" in out["reason"]
    text = _sql_text(conn)
    assert "for update" in text  # locked the day total
    assert "update payment_pending" not in text  # nothing mutated
    assert "insert into payment_daily_total" not in text


def test_execute_and_commit_success_updates_pending_and_total(patch_conn):
    # for-update on day total returns 10; cap 100; status select returns pending.
    conn = _FakeConn(
        results=[(Decimal("10.00"),), ("pending",)],
        rowcounts=[1, 1],  # update pending, upsert total
    )
    patch_conn(conn)
    store = PgPaymentStore("postgresql://x")
    out = store.execute_and_commit(
        "k1", "20.00", daily_aggregate_cap=100, execution_mode="live",
        day="2026-06-26",
    )
    assert out["committed"] is True
    assert out["daily_total"] == "30.00"
    text = _sql_text(conn)
    assert "update payment_pending set status = 'executed'" in text
    assert "nonce = ''" in text  # nonce burned atomically
    assert "status <> 'executed'" in text  # double-count guard
    assert "insert into payment_daily_total" in text
    assert "do update set total = payment_daily_total.total + excluded.total" in text
    assert conn.committed is True


def test_execute_and_commit_already_executed_no_double_count(patch_conn):
    conn = _FakeConn(results=[(Decimal("10.00"),), ("executed",)])
    patch_conn(conn)
    store = PgPaymentStore("postgresql://x")
    out = store.execute_and_commit(
        "k1", "20.00", daily_aggregate_cap=100, execution_mode="dry-run",
        day="2026-06-26",
    )
    assert out["committed"] is False
    assert out["reason"] == "already executed"
    text = _sql_text(conn)
    assert "update payment_pending" not in text
    assert "insert into payment_daily_total" not in text


def test_execute_and_commit_unknown_key(patch_conn):
    conn = _FakeConn(results=[(Decimal("10.00"),), None])
    patch_conn(conn)
    store = PgPaymentStore("postgresql://x")
    out = store.execute_and_commit(
        "missing", "20.00", daily_aggregate_cap=100, execution_mode="dry-run",
        day="2026-06-26",
    )
    assert out["committed"] is False
    assert out["reason"] == "unknown dedup key"


# ── cleanup ──────────────────────────────────────────────────────────────────
def test_cleanup_issues_deletes(patch_conn):
    conn = _FakeConn(rowcounts=[3, 1])
    patch_conn(conn)
    store = PgPaymentStore("postgresql://x")
    removed = store.cleanup(retention_days=90)
    text = _sql_text(conn)
    assert "delete from payment_pending" in text
    assert "coalesce(executed_at, created_at)" in text
    assert "delete from payment_daily_total" in text
    assert removed == 3  # the pending-table rowcount


# ── single-transaction guarantee ────────────────────────────────────────────
def test_execute_and_commit_runs_in_one_connection_transaction(patch_conn):
    # All SQL for a commit happens on ONE connection that is committed once at the
    # end (db.connect commits on clean exit) — i.e. one atomic transaction.
    conn = _FakeConn(
        results=[(Decimal("0.00"),), ("pending",)], rowcounts=[1, 1]
    )
    patch_conn(conn)
    store = PgPaymentStore("postgresql://x")
    store.execute_and_commit(
        "k1", "5.00", daily_aggregate_cap=100, execution_mode="dry-run",
        day="2026-06-26",
    )
    assert conn.committed is True
    assert conn.rolled_back is False
    assert conn.closed is True


# ── config backend selection ────────────────────────────────────────────────
def _base_env(monkeypatch):
    for name in (
        "INVESTEC_CLIENT_ID", "INVESTEC_CLIENT_SECRET", "INVESTEC_API_KEY",
        "DATABASE_URL",
    ):
        monkeypatch.setenv(name, "x")


def test_payments_state_backend_defaults_to_file(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.delenv("PAYMENTS_STATE_BACKEND", raising=False)
    assert Settings.load().payments_state_backend == "file"


def test_payments_state_backend_reads_postgres(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("PAYMENTS_STATE_BACKEND", "postgres")
    assert Settings.load().payments_state_backend == "postgres"
