"""S11: the whole v2 lifecycle against the REAL PgInstructionStore (only with INVESPEND_TEST_DATABASE_URL; scratch DB)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from tests.instr_helpers import PG_URL, make_store
from tests.v2_harness import Env, instruct_many, offered

pytestmark = pytest.mark.skipif(not PG_URL, reason="no INVESPEND_TEST_DATABASE_URL")


def test_full_lifecycle_live_mocked_on_postgres(tmp_path):
    env = Env(tmp_path, live=True, store=make_store("pg"))
    instruct_many(env, "101.00", "102.00")
    env.cycle(allow_non_durable=False)                                   # the real store is durable: preflight passes
    assert len(env.batch_emails()) == 1 and env.payment_calls() == 0
    env.advance(15)
    env.reply("approve 1")
    env.cycle(allow_non_durable=False)
    env.advance(15)
    env.cycle(allow_non_durable=False)
    rows = {r["item_no"]: r for r in env.rows()}
    assert rows[1]["status"] == "executed" and rows[2]["status"] == "awaiting_approval" and env.payment_calls() == 1
    assert env.store.daily_total(env.now) == Decimal("101.00")
    env.advance(15)
    env.reply("cancel")
    env.cycle(allow_non_durable=False)
    assert {r["item_no"]: r["status"] for r in env.rows()} == {1: "executed", 2: "cancelled"}


def test_stale_submitting_and_restart_on_postgres(tmp_path):
    env = Env(tmp_path, live=True, store=make_store("pg"))
    offered(env, "100.00")
    env.advance(15)
    env.reply("approve")
    env.cycle(allow_non_durable=False)
    env.advance(15)
    from invespend.payments import instructions
    real = env.store.finalize
    env.store.finalize = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("lock"))
    env.cycle(allow_non_durable=False)
    assert env.row()["status"] == "submitting" and env.payment_calls() == 1
    env.store.finalize = real
    env.advance(31)
    env.cycle(allow_non_durable=False)
    assert env.row()["status"] == "needs_review" and env.payment_calls() == 1
    # a fresh store object over the same database sees the same state (durable)
    again = instructions.PgInstructionStore(PG_URL)
    assert again.get(env.row()["instruction_id"])["status"] == "needs_review"
    assert again.daily_total(env.now) == Decimal("100.00")
