"""S11: the state-table tests of plan 2a that need the cycle: (d)(e)(f)(i)(l)(m)(n)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.instr_helpers import rec
from tests.v2_harness import Env, mod, offered, body_for

pytestmark = pytest.mark.xfail(strict=False, reason="S11 red: cycle not built yet")


def to_accepted(env):
    offered(env, "100.00")
    env.advance(15)
    env.reply("approve")
    env.cycle()
    env.advance(15)


def test_d_accepted_row_past_grace_at_cycle_start_becomes_expired_write_not_called(tmp_path):
    env = Env(tmp_path, live=True)
    to_accepted(env)
    env.advance(24 * 60)                                       # grace (24h from approved_at) has passed
    env.cycle()
    assert env.row()["status"] == "expired" and env.payment_calls() == 0


def test_d_fresh_accepted_row_from_the_snapshot_executes_once(tmp_path):
    env = Env(tmp_path, live=True)
    to_accepted(env)
    env.cycle()
    env.advance(15)
    env.cycle()
    assert env.row()["status"] == "executed" and env.payment_calls() == 1


def test_e_awaiting_approval_row_offered_more_than_the_window_ago_expires_with_one_notice(tmp_path):
    env = Env(tmp_path, live=True)
    offered(env, "100.00")
    env.advance(24 * 60 + 15)
    env.cycle()
    assert env.row()["status"] == "expired" and env.payment_calls() == 0
    assert len([s for s in env.smtp.subjects() if "expired" in s]) == 1
    env.advance(15)
    env.cycle()
    assert len([s for s in env.smtp.subjects() if "expired" in s]) == 1        # notified once (CAS winner only)


def test_f_stale_submitting_becomes_needs_review_and_fresh_submitting_is_untouched(tmp_path):
    env = Env(tmp_path, live=True)
    for n, minute in ((1, 0), (2, 25)):
        env.store.create(rec(n, status="awaiting_approval", notify_to="piet@example.com", expires_at=env.now + timedelta(days=2)))
    rows = env.store.offer_batch("piet@example.com", now=env.now, approval_window=timedelta(hours=48), max_items=5, new_ref=lambda: "B-1007-aaaa")
    for row in rows:
        env.store.approve_item(row["instruction_id"], batch_ref="B-1007-aaaa", item_no=row["item_no"], notify_to="piet@example.com",
                               now=env.now, grace=timedelta(hours=48))
    env.store.claim_for_execution("id0001", Decimal("100.00"), daily_cap=Decimal("50000"), execution_mode="live", now=env.now)
    env.store.claim_for_execution("id0002", Decimal("100.00"), daily_cap=Decimal("50000"), execution_mode="live", now=env.now + timedelta(minutes=25))
    env.advance(35)
    env.cycle()
    assert env.store.get("id0001")["status"] == "needs_review" and env.store.get("id0002")["status"] == "submitting"
    assert env.payment_calls() == 0


def test_i_expiry_first_a_held_row_past_both_eligible_and_held_expiry_is_expired(tmp_path):
    env = Env(tmp_path, live=True)
    env.store.create(rec(1, status="held", first_seen_at=env.now - timedelta(days=3), eligible_at=env.now - timedelta(days=3),
                         expires_at=env.now - timedelta(days=2), notify_to="piet@example.com"))
    env.cycle()
    assert env.store.get("id0001")["status"] == "expired" and env.payment_calls() == 0 and env.batch_emails() == []


def test_l_nb_r3_3_see_execute_tests_every_exit_is_finalize():
    assert mod("instructions").FINALIZE_RELEASES["parked"] is True


def test_m_claim_exception_before_commit_row_stays_accepted_write_not_called(tmp_path, monkeypatch):
    env = Env(tmp_path, live=True)
    to_accepted(env)
    monkeypatch.setattr(env.store, "claim_for_execution", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    env.cycle()
    assert env.row()["status"] == "accepted" and env.payment_calls() == 0 and env.audit_entries("claim_error")


def test_m_claim_exception_after_commit_row_submitting_then_stale_needs_review_write_not_called(tmp_path, monkeypatch):
    env = Env(tmp_path, live=True)
    to_accepted(env)
    real = env.store.claim_for_execution

    def commit_then_raise(*a, **k):
        real(*a, **k)
        raise RuntimeError("connection lost after commit")
    monkeypatch.setattr(env.store, "claim_for_execution", commit_then_raise)
    env.cycle()
    assert env.row()["status"] == "submitting" and env.payment_calls() == 0 and env.audit_entries("claim_error")
    monkeypatch.setattr(env.store, "claim_for_execution", real)
    env.advance(31)
    env.cycle()
    assert env.row()["status"] == "needs_review" and env.payment_calls() == 0
    env.run_cycles(3)
    assert env.payment_calls() == 0


def test_m_finalize_failure_after_post_leaves_submitting_then_stale_needs_review(tmp_path, monkeypatch):
    env = Env(tmp_path, live=True)
    to_accepted(env)
    real = env.store.finalize
    monkeypatch.setattr(env.store, "finalize", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("lock")))
    env.cycle()
    assert env.row()["status"] == "submitting" and env.payment_calls() == 1
    monkeypatch.setattr(env.store, "finalize", real)
    env.advance(31)
    env.cycle()
    assert env.row()["status"] == "needs_review" and env.payment_calls() == 1


def test_n_item_approved_in_this_cycle_is_not_in_the_snapshot_and_executes_next_cycle(tmp_path):
    env = Env(tmp_path, live=True)
    offered(env, "100.00")
    env.advance(15)
    env.reply("approve")
    env.cycle()                                                # cycle N: approve
    assert env.payment_calls() == 0
    env.advance(15)
    env.cycle()                                                # cycle N+1: exactly one call
    assert env.payment_calls() == 1
    env.advance(15)
    env.cycle()                                                # cycle N+2: none
    assert env.payment_calls() == 1
