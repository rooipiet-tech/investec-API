"""S11 (F45): no code path executes an item that was not approved by an authenticated reply."""
from __future__ import annotations

from datetime import timedelta

import pytest

from tests.v2_harness import BAD_AUTH, Env, instruct_many, mod, offered

pytestmark = pytest.mark.xfail(strict=False, reason="S11 red: cycle not built yet")


def scenario_no_reply(env):
    offered(env, "101.00")


def scenario_expired(env):
    offered(env, "101.00")
    env.advance(25 * 60)


def scenario_cancelled(env):
    offered(env, "101.00")
    env.advance(15)
    env.reply("cancel")
    env.cycle()


def scenario_noop_ambiguous_reply(env):
    offered(env, "101.00")
    env.advance(15)
    env.reply("approve 1 1")
    env.reply("do not approve")


def scenario_unauthenticated_approve(env):
    offered(env, "101.00")
    env.advance(15)
    env.reply("approve", auth=BAD_AUTH)
    env.reply("approve", auth=None)


def scenario_approve_for_a_different_batch(env):
    offered(env, "101.00")                                   # batch A, item 1 (the item under test is in batch B)
    env.advance(15)
    first = env.last_batch()
    instruct_many(env, "102.00")
    env.cycle()
    env.advance(15)
    second_row = [r for r in env.rows() if r["amount"] == 102][0]
    # an approve that links ONLY batch A cannot touch batch B's item
    env.reply("approve 1", batch=first)
    env.cycle()
    return second_row["instruction_id"]


SCENARIOS = [scenario_no_reply, scenario_expired, scenario_cancelled, scenario_noop_ambiguous_reply,
             scenario_unauthenticated_approve]


@pytest.mark.parametrize("live", [False, True], ids=["dry-run", "live-mocked"])
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__)
def test_unapproved_item_never_reaches_the_write_endpoint(tmp_path, scenario, live):
    env = Env(tmp_path, live=live)
    scenario(env)
    seen = set()
    for _ in range(6):
        env.cycle()
        seen |= {r["status"] for r in env.rows()}
        env.advance(15)
    assert env.payment_calls() == 0
    assert not seen & {"accepted", "submitting", "executed"}, seen


@pytest.mark.parametrize("live", [False, True], ids=["dry-run", "live-mocked"])
def test_approve_in_a_reply_to_a_different_batch_never_runs_the_other_batchs_item(tmp_path, live):
    env = Env(tmp_path, live=live)
    other = scenario_approve_for_a_different_batch(env)
    for _ in range(4):
        env.cycle()
        env.advance(15)
    target = env.store.get(other)
    assert target["status"] == "awaiting_approval" and target["approved_at"] is None
    assert env.payment_calls() == (1 if live else 0)          # only batch A's own item (approved) ran


@pytest.mark.parametrize("live", [False, True], ids=["dry-run", "live-mocked"])
def test_positive_control_authenticated_approve_executes_exactly_once_in_the_following_cycle(tmp_path, live):
    env = Env(tmp_path, live=live)
    offered(env, "101.00")
    env.advance(15)
    env.reply("approve")
    env.cycle()
    assert env.payment_calls() == 0 and env.row()["status"] == "accepted"
    env.advance(15)
    env.cycle()
    assert env.row()["status"] == "executed" and env.payment_calls() == (1 if live else 0)
    for _ in range(3):
        env.advance(15)
        env.cycle()
    assert env.payment_calls() == (1 if live else 0)


def test_create_cannot_produce_accepted_in_the_cycle_path(tmp_path):
    env = Env(tmp_path)
    created = []
    real = env.store.create
    env.store.create = lambda record: created.append(record["status"]) or real(record)
    env.instruct()
    env.cycle()
    assert created and "accepted" not in created and set(created) <= mod("instructions").INITIAL_STATUSES


def test_item_approved_in_this_cycle_is_not_in_the_snapshot(tmp_path):
    env = Env(tmp_path, live=True)
    offered(env, "101.00")
    env.advance(15)
    env.reply("approve")
    summary = env.cycle()
    assert summary["approved"] == 1 and summary["executed"] == 0 and env.payment_calls() == 0
