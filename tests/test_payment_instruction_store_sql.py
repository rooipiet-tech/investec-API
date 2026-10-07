"""S9 (R4-T2): SQL-shape tests for EVERY mutating PgInstructionStore method, offline.

A fake psycopg connection records the SQL; ``script`` supplies one result per executed statement, in order.
The helper classes are copied from tests/test_payment_pg_store.py (never imported from or edited there)."""
from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal

import pytest

from invespend import db
from tests.instr_helpers import GRACE, T0, WINDOW, ins, rec

pytestmark = pytest.mark.xfail(strict=False, reason="S9 red: pg_instructions.py not built yet")


class _Cursor:
    def __init__(self, conn):
        self._c = conn
        self._rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        c = self._c
        c.sql.append((" ".join(sql.lower().split()), params))
        if c.fail_at is not None and len(c.sql) == c.fail_at:
            raise RuntimeError("boom")
        item = c.script.pop(0) if c.script else {}
        if not isinstance(item, dict):
            item = {"rows": item}
        self._rows = list(item.get("rows", []))
        self.rowcount = item.get("rowcount", len(self._rows))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows


class _Conn:
    def __init__(self, script=(), fail_at=None):
        self.sql, self.script, self.fail_at = [], list(script), fail_at
        self.commits = self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@pytest.fixture
def pg(monkeypatch):
    from invespend.payments import pg_instructions
    holder = {"opened": 0, "conn": None}

    def install(conn):
        def _open(url, attempts=4):
            holder["opened"] += 1
            return conn
        monkeypatch.setattr(db, "_open", _open)
        holder["conn"] = conn
        return pg_instructions.PgInstructionStore("postgresql://x")

    holder["install"] = install
    return holder


def _text(conn):
    return [s for s, _ in conn.sql]


def _joined(conn):
    return " | ".join(_text(conn))


def _row_tuple(**over):
    base = rec(1, **over)
    out = {c: base.get(c) for c in ins().INSTRUCTION_COLUMNS}
    out["recent_beneficiary"] = bool(out["recent_beneficiary"])
    out["daily_reserved"] = bool(out["daily_reserved"])
    return tuple(out[c] for c in ins().INSTRUCTION_COLUMNS)


def _one_txn(pg, conn):
    assert pg["opened"] == 1 and conn.commits == 1 and conn.rollbacks == 0 and conn.closed


def test_create_is_one_transaction_insert_on_conflict_do_nothing_returning(pg):
    conn = _Conn([{"rows": [("id0001",)]}, {"rows": [_row_tuple()]}])
    store = pg["install"](conn)
    row, created = store.create(rec(1))
    assert created is True and row["instruction_id"] == "id0001"
    sql = _text(conn)
    assert sql[0].startswith("insert into payment_instruction") and "on conflict (instruction_id) do nothing returning instruction_id" in sql[0]
    assert sql[1].startswith("select") and "from payment_instruction where instruction_id = %s" in sql[1]
    _one_txn(pg, conn)
    # conflict path: nothing returned -> created False
    conn2 = _Conn([{"rows": []}, {"rows": [_row_tuple()]}])
    store2 = pg["install"](conn2)
    assert store2.create(rec(1))[1] is False


def test_create_validation_happens_before_any_connection(pg):
    conn = _Conn()
    store = pg["install"](conn)
    for bad in (rec(1, status="accepted"), rec(1, path="x"), rec(1, notify_to=""), rec(1, figures_source="x"), rec(1, batch_ref="B-1")):
        with pytest.raises(ValueError):
            store.create(bad)
    assert pg["opened"] == 0


def test_cas_status_single_update_where_status_any_returning(pg):
    conn = _Conn([{"rows": [("id0001",)]}])
    store = pg["install"](conn)
    assert store.cas_status("id0001", {"awaiting_approval", "accepted"}, "cancelled", now=T0, outcome_code="c") is True
    (sql, params), = conn.sql
    assert sql.startswith("update payment_instruction set status = %s") and "updated_at = %s" in sql
    assert "where instruction_id = %s and status = any(%s)" in sql and sql.endswith("returning instruction_id")
    assert "outcome_code = coalesce(%s, outcome_code)" in sql
    assert sorted(params[-1]) == ["accepted", "awaiting_approval"] and "cancelled" in params
    _one_txn(pg, conn)


def test_cas_status_returns_false_when_no_row_matched(pg):
    conn = _Conn([{"rows": []}])
    assert pg["install"](conn).cas_status("id0001", {"accepted"}, "cancelled", now=T0) is False


def test_approve_item_one_update_with_every_guard(pg):
    conn = _Conn([{"rows": [("id0001",)]}])
    store = pg["install"](conn)
    res = store.approve_item("id0001", batch_ref="B-1007-aaaa", item_no=1, notify_to="piet@example.com", now=T0, grace=GRACE)
    assert res.approved is True and res.reason == "ok"
    assert len(conn.sql) == 1
    sql = conn.sql[0][0]
    for part in ("update payment_instruction set status = 'accepted'", "approved_at = %s", "expires_at = %s",
                 "updated_at = %s", "where instruction_id = %s and status = 'awaiting_approval' and batch_ref = %s and item_no = %s",
                 "and notify_to = %s", "and offer_digest is not null", "and expires_at > %s", "returning instruction_id"):
        assert part in sql, part
    _one_txn(pg, conn)


def test_approve_item_follow_up_read_picks_the_reason(pg):
    def run(row):
        conn = _Conn([{"rows": []}, {"rows": [row] if row else []}])
        res = pg["install"](conn).approve_item("id0001", batch_ref="B-1007-aaaa", item_no=1, notify_to="p@x", now=T0, grace=GRACE)
        assert len(conn.sql) == 2 and conn.sql[1][0].startswith("select")
        return res.reason
    exp = T0 + timedelta(hours=1)
    assert run(None) == "wrong_batch"
    assert run(("accepted", "B-1007-aaaa", 1, "p@x", exp, "d")) == "not_awaiting"
    assert run(("awaiting_approval", "B-1007-aaaa", 1, "p@x", T0 - timedelta(hours=1), "d")) == "expired"
    assert run(("awaiting_approval", "B-1007-zzzz", 1, "p@x", exp, "d")) == "wrong_batch"
    assert run(("awaiting_approval", "B-1007-aaaa", 1, "other", exp, "d")) == "wrong_batch"


def test_offer_batch_locks_numbers_and_commits_once(pg):
    selected = [_row_tuple(), _row_tuple(instruction_id="id0002")]
    updated1 = _row_tuple(batch_ref="B-1007-0001", item_no=1, offered_at=T0, offer_digest="d1")
    updated2 = _row_tuple(instruction_id="id0002", batch_ref="B-1007-0001", item_no=2, offered_at=T0, offer_digest="d2")
    conn = _Conn([{"rows": selected}, {"rows": []}, {"rows": [updated1]}, {"rows": [updated2]}])
    store = pg["install"](conn)
    rows = store.offer_batch("piet@example.com", now=T0, approval_window=WINDOW, max_items=50, new_ref=lambda: "B-1007-0001")
    assert [r["item_no"] for r in rows] == [1, 2]
    sql = _text(conn)
    assert "for update" in sql[0] and "status = 'awaiting_approval'" in sql[0] and "batch_ref is null" in sql[0]
    assert "order by received_at, instruction_id" in sql[0] and "limit %s" in sql[0] and "expires_at > %s" in sql[0]
    assert sql[1].startswith("select 1 from payment_instruction where batch_ref = %s")
    for update in sql[2:]:
        assert update.startswith("update payment_instruction set batch_ref = %s, item_no = %s, offered_at = %s, offer_digest = %s")
        assert "where instruction_id = %s and status = 'awaiting_approval' and batch_ref is null" in update
    _one_txn(pg, conn)


def test_offer_batch_nothing_unoffered_issues_one_select_and_no_ref(pg):
    conn = _Conn([{"rows": []}])
    calls = []
    assert pg["install"](conn).offer_batch("p@x", now=T0, approval_window=WINDOW, max_items=3,
                                           new_ref=lambda: calls.append(1) or "B-1007-0001") == []
    assert calls == [] and len(conn.sql) == 1


def test_offer_batch_unique_violation_rolls_back_and_retries_with_a_new_ref(pg, monkeypatch):
    import psycopg
    conns = [_Conn([{"rows": [_row_tuple()]}, {"rows": []}]), None]
    conns[1] = _Conn([{"rows": [_row_tuple()]}, {"rows": []}, {"rows": [_row_tuple(batch_ref="B-1007-0002", item_no=1, offered_at=T0, offer_digest="d")]}])
    seq = iter(conns)
    monkeypatch.setattr(db, "_open", lambda url, attempts=4: next(seq))
    from invespend.payments import pg_instructions
    store = pg_instructions.PgInstructionStore("postgresql://x")

    original = _Cursor.execute

    def execute(self, sql, params=None):
        if self._c is conns[0] and len(self._c.sql) == 2:
            self._c.sql.append((" ".join(sql.lower().split()), params))
            raise psycopg.errors.UniqueViolation("clash")
        return original(self, sql, params)

    monkeypatch.setattr(_Cursor, "execute", execute)
    refs = iter(["B-1007-0001", "B-1007-0002"])
    rows = store.offer_batch("p@x", now=T0, approval_window=WINDOW, max_items=3, new_ref=lambda: next(refs))
    assert rows[0]["batch_ref"] == "B-1007-0002"
    assert conns[0].rollbacks == 1 and conns[0].commits == 0 and conns[1].commits == 1


def test_unoffer_batch_clears_only_unnotified_awaiting_rows(pg):
    conn = _Conn([{"rowcount": 2}])
    assert pg["install"](conn).unoffer_batch("B-1007-0001", now=T0) == 2
    (sql, _), = conn.sql
    assert "batch_ref = null, item_no = null, offered_at = null, offer_digest = null" in sql
    assert "where batch_ref = %s and batch_notified_at is null and status = 'awaiting_approval'" in sql
    _one_txn(pg, conn)


def test_mark_batch_notified_is_set_once(pg):
    conn = _Conn([{"rowcount": 3}])
    assert pg["install"](conn).mark_batch_notified("B-1007-0001", T0) == 3
    (sql, _), = conn.sql
    assert "set batch_notified_at = %s" in sql and "where batch_ref = %s and batch_notified_at is null" in sql
    _one_txn(pg, conn)


def test_claim_inserts_day_row_then_locks_then_updates_and_commits_once(pg):
    conn = _Conn([
        {}, {"rows": [(Decimal("0"),)]},
        {"rows": [("accepted", T0, T0 + timedelta(hours=5), Decimal("100.00"))]},
        {"rows": [("id0001",)]}, {"rowcount": 1},
    ])
    store = pg["install"](conn)
    res = store.claim_for_execution("id0001", Decimal("100.00"), daily_cap=Decimal("50000"), execution_mode="dry-run", now=T0)
    assert (res.committed, res.reason, res.daily_total) == (True, "ok", Decimal("100.00"))
    sql = _text(conn)
    assert sql[0].startswith("insert into payment_daily_total (day, total) values (%s, 0) on conflict") and "do nothing" in sql[0]
    assert sql[1].startswith("select total from payment_daily_total where day = %s for update")
    assert sql[2].startswith("select") and "from payment_instruction where instruction_id = %s for update" in sql[2]
    upd = sql[3]
    assert upd.startswith("update payment_instruction set status = 'submitting'")
    for part in ("daily_reserved = true", "reserved_day = %s", "execution_mode = %s", "updated_at = %s",
                 "where instruction_id = %s and status = 'accepted' and approved_at is not null and expires_at > %s"):
        assert part in upd, part
    assert sql[4].startswith("update payment_daily_total set total = total + %s where day = %s")
    _one_txn(pg, conn)
    assert str(conn.sql[0][1][0]) == "2026-10-07"


def test_claim_daily_cap_reserves_nothing(pg):
    conn = _Conn([{}, {"rows": [(Decimal("49950"),)]}, {"rows": [("accepted", T0, T0 + timedelta(hours=5), Decimal("100.00"))]}])
    res = pg["install"](conn).claim_for_execution("id0001", Decimal("100.00"), daily_cap=Decimal("50000"),
                                                  execution_mode="dry-run", now=T0)
    assert (res.committed, res.reason, res.daily_total) == (False, "daily_cap", Decimal("49950"))
    assert not any(s.startswith("update") for s in _text(conn))


def test_claim_stale_and_cas_lost_reserve_nothing(pg):
    conn = _Conn([{}, {"rows": [(Decimal("0"),)]}, {"rows": [("accepted", T0, T0 - timedelta(hours=1), Decimal("100.00"))]}])
    assert pg["install"](conn).claim_for_execution("id0001", Decimal("100.00"), daily_cap=Decimal("50000"),
                                                   execution_mode="dry-run", now=T0).reason == "stale"
    assert not any(s.startswith("update") for s in _text(conn))
    conn = _Conn([{}, {"rows": [(Decimal("0"),)]}, {"rows": [("cancelled", T0, T0 + timedelta(hours=1), Decimal("100.00"))]}])
    assert pg["install"](conn).claim_for_execution("id0001", Decimal("100.00"), daily_cap=Decimal("50000"),
                                                   execution_mode="dry-run", now=T0).reason == "cas_lost"
    conn = _Conn([{}, {"rows": [(Decimal("0"),)]}, {"rows": [("accepted", T0, T0 + timedelta(hours=1), Decimal("100.00"))]}, {"rows": []}])
    assert pg["install"](conn).claim_for_execution("id0001", Decimal("100.00"), daily_cap=Decimal("50000"),
                                                   execution_mode="dry-run", now=T0).reason == "cas_lost"
    assert not any(s.startswith("update payment_daily_total") for s in _text(conn))


def test_claim_post_happens_outside_a_transaction_connection_counter(pg):
    conn = _Conn([{}, {"rows": [(Decimal("0"),)]}, {"rows": [("accepted", T0, T0 + timedelta(hours=5), Decimal("100.00"))]}, {"rows": [("id0001",)]}, {}])
    store = pg["install"](conn)
    store.claim_for_execution("id0001", Decimal("100.00"), daily_cap=Decimal("50000"), execution_mode="live", now=T0)
    assert conn.closed and pg["opened"] == 1          # the connection is closed before the caller can POST


def test_finalize_status_update_returning_then_release_bound_to_returned_reserved_day(pg):
    returned_day = T0.date() - timedelta(days=1)
    conn = _Conn([{"rows": [(Decimal("100.00"), returned_day, True)]}, {}, {}])
    store = pg["install"](conn)
    assert store.finalize("id0001", "submitting", "failed", release=True, now=T0, outcome_code="rejected", outcome_message="nope") is True
    sql = _text(conn)
    assert sql[0].startswith("update payment_instruction set status = %s") and "where instruction_id = %s and status = %s" in sql[0]
    assert sql[0].endswith("returning amount, reserved_day, daily_reserved")
    assert "outcome_message" in sql[0] and "executed_at" in sql[0]
    assert sql[1].startswith("update payment_instruction set daily_reserved = false")
    assert sql[2].startswith("update payment_daily_total set total = greatest(total - %s, 0) where day = %s")
    assert conn.sql[2][1] == (Decimal("100.00"), returned_day)       # the RETURNED day and amount, never the day of now
    _one_txn(pg, conn)


def test_finalize_no_row_matched_changes_nothing_else(pg):
    conn = _Conn([{"rows": []}])
    assert pg["install"](conn).finalize("id0001", "submitting", "failed", release=True, now=T0) is False
    assert len(conn.sql) == 1
    _one_txn(pg, conn)


def test_finalize_without_release_or_unreserved_row_touches_no_total(pg):
    conn = _Conn([{"rows": [(Decimal("100.00"), T0.date(), True)]}])
    assert pg["install"](conn).finalize("id0001", "submitting", "executed", release=False, now=T0) is True
    assert len(conn.sql) == 1 and "payment_daily_total" not in _joined(conn)
    conn = _Conn([{"rows": [(Decimal("100.00"), None, False)]}])
    assert pg["install"](conn).finalize("id0001", "submitting", "failed", release=True, now=T0) is True
    assert len(conn.sql) == 1


def test_finalize_validation_before_any_connection(pg):
    conn = _Conn()
    store = pg["install"](conn)
    with pytest.raises(ValueError):
        store.finalize("id0001", "submitting", "failed", release=False, now=T0)
    with pytest.raises(ValueError):
        store.finalize("id0001", "accepted", "failed", release=True, now=T0)
    assert pg["opened"] == 0


def test_set_held_single_statement_set_once_guard(pg):
    conn = _Conn([{"rows": [("id0001",)]}])
    store = pg["install"](conn)
    assert store.set_held("id0001", beneficiary_id="b", fingerprint="f", first_seen_at=T0, eligible_at=T0,
                          expires_at=T0 + WINDOW, now=T0) is True
    (sql, _), = conn.sql
    assert "set status = 'held'" in sql and "expires_at = %s" in sql and "updated_at = %s" in sql
    assert "first_seen_at = coalesce(first_seen_at, %s)" in sql and "eligible_at = coalesce(eligible_at, %s)" in sql
    assert "where instruction_id = %s and status = 'awaiting_beneficiary' and eligible_at is null and expires_at > %s" in sql
    assert sql.endswith("returning instruction_id")
    _one_txn(pg, conn)


def test_make_ready_cas_guarded_by_eligible_at_and_expiry(pg):
    conn = _Conn([{"rows": [("id0001",)]}])
    assert pg["install"](conn).make_ready("id0001", now=T0, approval_window=WINDOW) is True
    (sql, _), = conn.sql
    assert "set status = 'awaiting_approval'" in sql and "expires_at = %s" in sql
    assert "where instruction_id = %s and status = 'held' and eligible_at <= %s and expires_at > %s" in sql
    _one_txn(pg, conn)


def test_mark_notified_set_once_and_field_whitelist(pg):
    conn = _Conn([{"rows": [("id0001",)]}])
    store = pg["install"](conn)
    assert store.mark_notified("id0001", "paste", T0) is True
    (sql, _), = conn.sql
    assert "set paste_notified_at = %s" in sql and "paste_notified_at is null" in sql and "updated_at = %s" in sql
    with pytest.raises(ValueError):
        store.mark_notified("id0001", "paste_notified_at; drop table x", T0)
    conn = _Conn([{"rows": [("id0001",)]}])
    pg["install"](conn).mark_notified("id0001", "hold", T0)
    assert "hold_notified_at is null" in conn.sql[0][0]


def test_recover_stale_submitting_single_update_by_updated_at(pg):
    conn = _Conn([{"rows": [("id0001",), ("id0002",)]}])
    assert pg["install"](conn).recover_stale_submitting(older_than=timedelta(minutes=30), now=T0) == ["id0001", "id0002"]
    (sql, params), = conn.sql
    assert "set status = 'needs_review'" in sql and "outcome_code = 'stale_submitting'" in sql
    assert "where status = 'submitting' and updated_at < %s" in sql and sql.endswith("returning instruction_id")
    assert T0 - timedelta(minutes=30) in params
    assert "daily_reserved" not in sql                      # the reservation is KEPT
    _one_txn(pg, conn)


def test_message_seen_and_resend_stamps(pg):
    conn = _Conn([{"rows": [("m1",)]}])
    assert pg["install"](conn).mark_message_seen("m1", T0) is True
    assert "insert into payment_message_seen" in conn.sql[0][0] and "on conflict (instruction_id) do nothing returning" in conn.sql[0][0]
    conn = _Conn([{"rows": []}])
    assert pg["install"](conn).mark_message_seen("m1", T0) is False
    conn = _Conn([{"rows": [("m1",)]}])
    assert pg["install"](conn).mark_resend_notified("m1", T0) is True
    assert "resend_notified_at is null" in conn.sql[0][0] and "set resend_notified_at = %s" in conn.sql[0][0]
    conn = _Conn()
    pg["install"](conn).set_message_outcome("m1", "instruction", auth_from="p@x")
    assert "auth_from = coalesce(%s, auth_from)" in conn.sql[0][0]


def test_sweep_stuck_messages_updates_then_selects_in_one_transaction(pg):
    conn = _Conn([{}, {"rows": [("m1", "stuck", T0, "p@x", None)]}])
    rows = pg["install"](conn).sweep_stuck_messages(older_than=timedelta(minutes=30), now=T0 + timedelta(hours=1))
    assert rows[0]["instruction_id"] == "m1" and rows[0]["auth_from"] == "p@x"
    sql = _text(conn)
    assert "set outcome = 'stuck' where outcome = 'processing' and seen_at < %s" in sql[0]
    assert "auth_from is not null and resend_notified_at is null" in sql[1]
    _one_txn(pg, conn)


def test_observe_beneficiary_inserts_locks_and_updates_conditionally(pg):
    conn = _Conn([{}, {"rows": [(T0, False, "fpA", None)]}, {}])
    obs = pg["install"](conn).observe_beneficiary("b1", T0 + timedelta(hours=1), fingerprint="fpB")
    sql = _text(conn)
    assert sql[0].startswith("insert into payment_beneficiary_seen") and "on conflict (beneficiary_id) do nothing" in sql[0]
    assert "for update" in sql[1]
    assert sql[2].startswith("update payment_beneficiary_seen set last_fingerprint = %s, fingerprint_changed_at = %s")
    assert "first_seen_at" not in sql[2]                       # never reset
    assert obs.first_seen_at == T0 and obs.fingerprint_changed_at == T0 + timedelta(hours=1) and obs.last_fingerprint == "fpB"
    _one_txn(pg, conn)
    conn = _Conn([{}, {"rows": [(T0, False, None, None)]}, {}])
    obs = pg["install"](conn).observe_beneficiary("b1", T0, fingerprint="fpA")
    assert obs.fingerprint_changed_at is None and "fingerprint_changed_at = %s" not in conn.sql[2][0]
    conn = _Conn([{}, {"rows": [(T0, False, "fpA", None)]}])
    pg["install"](conn).observe_beneficiary("b1", T0, fingerprint="fpA")
    assert len(conn.sql) == 2


def test_meta_set_upsert_and_bootstrap_marker(pg):
    conn = _Conn()
    store = pg["install"](conn)
    store.meta_set("k", "v", T0)
    assert "insert into payment_v2_meta" in conn.sql[0][0] and "on conflict (key) do update" in conn.sql[0][0]
    conn = _Conn()
    pg["install"](conn).mark_bootstrap_done(T0)
    assert conn.sql[0][1][0] == "beneficiary_bootstrap"


def test_cleanup_deletes_only_terminal_instructions_and_old_message_rows(pg):
    conn = _Conn([{"rowcount": 4}, {"rowcount": 2}])
    assert pg["install"](conn).cleanup(90, T0) == 4
    sql = _joined(conn)
    assert "delete from payment_instruction where status in (" in sql and "delete from payment_message_seen where seen_at < %s" in sql
    assert "payment_beneficiary_seen" not in sql and "payment_v2_meta" not in sql and "'awaiting_approval'" not in sql.split("payment_message_seen")[0]
    _one_txn(pg, conn)


def test_every_mutating_method_rolls_back_without_commit_on_exception(pg):
    calls = {
        "create": lambda s: s.create(rec(1)),
        "mark_message_seen": lambda s: s.mark_message_seen("m", T0),
        "set_message_outcome": lambda s: s.set_message_outcome("m", "x"),
        "sweep_stuck_messages": lambda s: s.sweep_stuck_messages(older_than=timedelta(minutes=1), now=T0),
        "mark_resend_notified": lambda s: s.mark_resend_notified("m", T0),
        "mark_bootstrap_done": lambda s: s.mark_bootstrap_done(T0),
        "observe_beneficiary": lambda s: s.observe_beneficiary("b", T0, fingerprint="f"),
        "meta_set": lambda s: s.meta_set("k", "v", T0),
        "set_held": lambda s: s.set_held("i", beneficiary_id="b", fingerprint="f", first_seen_at=T0, eligible_at=T0, expires_at=T0, now=T0),
        "make_ready": lambda s: s.make_ready("i", now=T0, approval_window=WINDOW),
        "offer_batch": lambda s: s.offer_batch("p@x", now=T0, approval_window=WINDOW, max_items=3, new_ref=lambda: "B-1007-0001"),
        "unoffer_batch": lambda s: s.unoffer_batch("B-1007-0001", now=T0),
        "mark_batch_notified": lambda s: s.mark_batch_notified("B-1007-0001", T0),
        "approve_item": lambda s: s.approve_item("i", batch_ref="B-1007-0001", item_no=1, notify_to="p@x", now=T0, grace=GRACE),
        "cas_status": lambda s: s.cas_status("i", {"accepted"}, "cancelled", now=T0),
        "claim_for_execution": lambda s: s.claim_for_execution("i", Decimal("1"), daily_cap=Decimal("10"), execution_mode="dry-run", now=T0),
        "finalize": lambda s: s.finalize("i", "submitting", "failed", release=True, now=T0),
        "recover_stale_submitting": lambda s: s.recover_stale_submitting(older_than=timedelta(minutes=1), now=T0),
        "mark_notified": lambda s: s.mark_notified("i", "paste", T0),
        "cleanup": lambda s: s.cleanup(1, T0),
    }
    for name, call in calls.items():
        pg["opened"] = 0
        conn = _Conn(fail_at=1)
        store = pg["install"](conn)
        with pytest.raises(RuntimeError):
            call(store)
        assert conn.commits == 0 and conn.rollbacks == 1 and pg["opened"] == 1, name


def test_every_mutating_instruction_update_sets_updated_at(pg):
    for sql_start, call in (
        ("cas_status", lambda s: s.cas_status("i", {"accepted"}, "cancelled", now=T0)),
        ("mark_notified", lambda s: s.mark_notified("i", "paste", T0)),
        ("unoffer_batch", lambda s: s.unoffer_batch("B-1007-0001", now=T0)),
        ("mark_batch_notified", lambda s: s.mark_batch_notified("B-1007-0001", T0)),
        ("make_ready", lambda s: s.make_ready("i", now=T0, approval_window=WINDOW)),
        ("recover", lambda s: s.recover_stale_submitting(older_than=timedelta(minutes=1), now=T0)),
    ):
        conn = _Conn([{"rows": [("i",)], "rowcount": 1}])
        call(pg["install"](conn))
        assert "updated_at = %s" in conn.sql[0][0], sql_start


def test_pg_store_never_issues_ddl_or_account_number_columns(pg):
    import inspect
    from invespend.payments import pg_instructions
    source = inspect.getsource(pg_instructions).lower()
    for banned in ("create table", "alter table", "drop table", "truncate", "account_number"):
        assert banned not in source, banned
