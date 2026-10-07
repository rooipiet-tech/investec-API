"""S11: F12 routing, F43 cadence, batch offering (offline, injected clock)."""
from __future__ import annotations

import smtplib
import socket
from datetime import timedelta

import pytest

from tests.v2_harness import ACME_RAW, INSTRUCTION, OWNER, Env, body_for, instruct_many, mod, offered, second_sender

pytestmark = pytest.mark.xfail(strict=False, reason="S11 red: cycle/batch not built yet")

PENDING = "awaiting_approval"


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_fixture_preconditions_make_registered_payee_batchable(env):
    env.instruct()
    summary = env.cycle()
    assert env.row()["status"] == mod("instructions").PENDING_APPROVAL and env.row()["batch_ref"]
    assert summary["batches_sent"] == 1 and summary["offered"] == 1


# ------------------------------------------------------------------ F12
def test_registered_payee_becomes_awaiting_approval_and_is_offered_write_not_called_that_cycle(env):
    env.instruct()
    env.cycle()
    row = env.row()
    assert row["status"] == PENDING and row["item_no"] == 1 and row["offered_at"] == env.now and row["path"] == "registered"
    assert len(env.batch_emails()) == 1 and env.payment_calls() == 0


@pytest.mark.parametrize("live", [False, True])
def test_no_reply_never_executes_over_many_cycles(tmp_path, live):
    env = Env(tmp_path, live=live)
    env.instruct()
    env.run_cycles(12)
    assert env.payment_calls() == 0 and env.row()["status"] == PENDING and len(env.batch_emails()) == 1


@pytest.mark.parametrize("live", [False, True])
def test_authenticated_approve_executes_exactly_once_in_the_next_cycle_live_mocked_and_dry_run(tmp_path, live):
    env = Env(tmp_path, live=live)
    env.instruct()
    env.cycle()
    env.advance(15)
    env.reply("approve")
    env.cycle()                                                        # approval cycle: nothing runs
    assert env.row()["status"] == "accepted" and env.payment_calls() == 0
    env.advance(15)
    result = env.cycle()
    assert env.row()["status"] == "executed" and env.payment_calls() == (1 if live else 0)
    assert env.row()["execution_mode"] == ("live" if live else "dry-run")
    env.advance(15)
    env.cycle()
    env.advance(15)
    env.cycle()
    assert env.payment_calls() == (1 if live else 0) and result["executed"] == 1


def test_unregistered_payee_parks_awaiting_beneficiary_not_in_batch_write_not_called(env):
    env.instruct(body_for(payee="Nobody Ltd"))
    env.cycle()
    row = env.row()
    assert row["status"] == "awaiting_beneficiary" and row["path"] == "new_payee" and row["batch_ref"] is None
    assert env.batch_emails() == [] and env.payment_calls() == 0
    assert [s for s in env.smtp.subjects() if "Add this beneficiary" in s] and row["paste_notified_at"] == env.now


def test_beneficiary_appears_at_t0_default_hold_zero_joins_the_next_batch_not_before_it_appears(env):
    env.instruct(body_for(payee="Nobody Ltd"))
    env.cycle()
    env.run_cycles(1)
    assert env.batch_emails() == [] and env.row()["status"] == "awaiting_beneficiary"      # not listed yet: never batched
    env.client.beneficiaries.append({"beneficiaryId": "ben-nobody", "beneficiaryName": "Nobody Ltd", "accountNumber": "5550001111", "code": "250655"})
    appear = env.now
    env.cycle()
    row = env.row()
    assert row["status"] == PENDING and row["first_seen_at"] == appear and row["eligible_at"] == appear
    assert len(env.batch_emails()) == 1 and row["batch_ref"]
    assert [s for s in env.smtp.subjects() if "Payee now registered" in s]
    assert "0 hours" in env.smtp.body_text()
    assert env.payment_calls() == 0


def test_hold_n_hours_not_batched_before_first_seen_plus_n(tmp_path):
    env = Env(tmp_path, payments_hold_hours=2.0)
    env.instruct(body_for(payee="Nobody Ltd"))
    env.cycle()
    env.client.beneficiaries.append({"beneficiaryId": "ben-nobody", "beneficiaryName": "Nobody Ltd", "accountNumber": "5550001111", "code": "250655"})
    env.advance(15)
    seen = env.now
    env.cycle()
    assert env.row()["status"] == "held" and env.row()["eligible_at"] == seen + timedelta(hours=2)
    for _ in range(7):
        env.advance(15)
        env.cycle()
        assert env.row()["status"] == "held" and env.batch_emails() == []
    env.advance(15)                                                    # first_seen + 2h exactly
    assert env.now == seen + timedelta(hours=2)
    env.cycle()
    assert env.row()["status"] == PENDING and len(env.batch_emails()) == 1


def test_hold_never_replaces_approval(tmp_path):
    env = Env(tmp_path, payments_hold_hours=1.0)
    env.instruct(body_for(payee="Nobody Ltd"))
    env.cycle()
    env.client.beneficiaries.append({"beneficiaryId": "ben-nobody", "beneficiaryName": "Nobody Ltd", "accountNumber": "5550001111", "code": "250655"})
    env.run_cycles(10)
    assert env.row()["status"] in (PENDING, "held") and env.payment_calls() == 0


def test_static_scan_write_endpoint_reachable_only_from_execute_instruction_and_only_for_accepted():
    import ast
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "src" / "invespend"
    callers, executors, accepted_setters = [], [], []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text())
        rel = path.relative_to(src).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "create_payment":
                    callers.append(rel)
                if node.func.attr == "execute_instruction":
                    executors.append(rel)
                if node.func.attr == "cas_status":
                    args = list(node.args[2:3]) + [k.value for k in node.keywords if k.arg == "new"]
                    if any(isinstance(a, ast.Constant) and a.value == "accepted" for a in args):
                        accepted_setters.append(rel)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "execute_instruction":
                executors.append(rel)
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "status" and isinstance(v, ast.Constant) and v.value == "accepted":
                        accepted_setters.append(rel)
    legacy_callers = {"payments/pipeline.py", "payments/selftest.py"}
    assert set(callers) - legacy_callers == {"payments/execute.py"}, callers
    assert set(executors) <= {"payments/cycle.py"} and executors, executors
    assert accepted_setters == [] or set(accepted_setters) <= {"payments/instructions.py"}, accepted_setters


# ------------------------------------------------------------------ F43
def test_cycle_with_zero_new_items_sends_no_email(env):
    assert env.cycle()["batches_sent"] == 0 and env.smtp.sent == []


def test_cycle_with_three_new_items_sends_exactly_one_email_listing_three_numbered_items(env):
    instruct_many(env, "101.00", "102.00", "103.00")
    env.cycle()
    batches = env.batch_emails()
    assert len(batches) == 1 and len(env.smtp.sent) == 1
    body = str(batches[0].get_content())
    for n, amount in ((1, "101.00"), (2, "102.00"), (3, "103.00")):
        assert f"{n}. Acme Trading | ZAR {amount}" in body
    assert "Batch total: ZAR 306.00" in body and "figures from: typed" in body
    assert str(batches[0]["To"]) == OWNER and str(batches[0]["From"]) == "bot@example.com"


def test_next_cycle_one_new_two_pending_one_email_with_still_pending_section_citing_the_earlier_ref(env):
    first = offered(env, "101.00", "102.00")
    env.advance(15)
    instruct_many(env, "103.00")
    env.cycle()
    assert len(env.batch_emails()) == 2
    second = env.last_batch()
    body = str(second.get_content())
    assert "Still pending from earlier batches:" in body and f"{first} #1" in body and f"{first} #2" in body
    assert env.batch_ref(second) != first and "1. Acme Trading | ZAR 103.00" in body


def test_pending_items_are_never_reoffered_or_duplicated_and_keep_ref_and_numbers(env):
    first = offered(env, "101.00", "102.00")
    before = {r["instruction_id"]: (r["batch_ref"], r["item_no"]) for r in env.rows()}
    env.advance(15)
    instruct_many(env, "103.00")
    env.cycle()
    after = {r["instruction_id"]: (r["batch_ref"], r["item_no"]) for r in env.rows() if r["instruction_id"] in before}
    assert after == before and all(ref == first for ref, _ in after.values())
    assert sorted(r["item_no"] for r in env.rows() if r["batch_ref"] != first) == [1]


def test_rerun_same_cycle_sends_no_duplicate_batch_email(env):
    instruct_many(env, "101.00")
    env.cycle()
    env.cycle()
    assert len(env.batch_emails()) == 1


def test_expired_cancelled_approved_items_absent_from_still_pending(env):
    first = offered(env, "101.00", "102.00", "103.00")
    env.advance(15)
    env.reply("cancel 1")
    env.cycle()
    env.advance(15)
    env.reply("approve 2")
    env.cycle()
    env.advance(15)
    instruct_many(env, "104.00")
    env.cycle()
    body = str(env.last_batch().get_content())
    assert f"{first} #3" in body and f"{first} #1" not in body and f"{first} #2" not in body


def test_pending_items_alone_do_not_trigger_an_email(env):
    offered(env, "101.00")
    env.run_cycles(6)
    assert len(env.batch_emails()) == 1


PRE_DATA = [
    smtplib.SMTPConnectError(421, b"no"), smtplib.SMTPAuthenticationError(535, b"no"),
    smtplib.SMTPSenderRefused(550, b"no", "a@b.c"), smtplib.SMTPRecipientsRefused({"a@b.c": (550, b"no")}),
    ConnectionRefusedError(), socket.gaierror(-2, "no"),
]


@pytest.mark.parametrize("error", PRE_DATA, ids=lambda e: type(e).__name__)
def test_batch_email_pre_data_send_failure_unoffers_and_next_cycle_offers_under_new_ref(env, error):
    env.instruct()
    env.smtp.fail_with, env.smtp.fail_times = error, -1
    env.cycle()
    row = env.row()
    assert row["batch_ref"] is None and row["item_no"] is None and row["status"] == PENDING and env.batch_emails() == []
    failed = env.audit_entries("batch_send_failed")
    assert len(failed) == 1 and failed[0]["detail"]["reason"] == type(error).__name__
    env.smtp.fail_with = None
    env.advance(15)
    env.cycle()
    assert len(env.batch_emails()) == 1 and env.row()["batch_ref"]
    assert env.row()["batch_notified_at"] == env.now


POST_DATA = [TimeoutError("t"), smtplib.SMTPServerDisconnected("x"), smtplib.SMTPDataError(554, b"no"), RuntimeError("unknown")]


@pytest.mark.parametrize("error", POST_DATA, ids=lambda e: type(e).__name__)
def test_batch_email_send_timeout_after_data_leaves_batch_offered_and_resend_delivers_same_ref(env, error):
    env.instruct()
    env.smtp.fail_with, env.smtp.fail_times = error, 1
    env.cycle()
    row = env.row()
    ref, number = row["batch_ref"], row["item_no"]
    assert ref and row["batch_notified_at"] is None and env.batch_emails() == []
    assert env.audit_entries("batch_send_unknown")[0]["detail"]["reason"] == type(error).__name__
    assert env.audit_entries("batch_send_failed") == []
    env.advance(20)
    env.cycle()
    assert env.batch_emails() == []                                 # inside the stuck window: nothing yet
    env.advance(20)
    env.cycle()                                                     # 40 minutes after the offer: resend of the SAME batch
    assert len(env.batch_emails()) == 1 and env.batch_ref() == ref
    assert f"1. Acme Trading" in str(env.last_batch().get_content()) and number == 1
    assert env.row()["batch_ref"] == ref and env.row()["batch_notified_at"] == env.now
    # a reply to that copy still links and approves
    env.advance(15)
    env.reply("approve")
    env.cycle()
    assert env.row()["status"] == "accepted"


def test_pre_data_error_tuple_is_exactly_the_documented_classes():
    assert set(mod("batch").PRE_DATA_SMTP_ERRORS) == {
        smtplib.SMTPConnectError, smtplib.SMTPHeloError, smtplib.SMTPAuthenticationError, smtplib.SMTPSenderRefused,
        smtplib.SMTPRecipientsRefused, ConnectionRefusedError, socket.gaierror}


def test_batch_email_resent_by_notice_sweep_when_stamp_missing(env):
    store = env.store
    env.instruct()
    # simulate the crash window: offered, never emailed, never stamped
    env.smtp.fail_with, env.smtp.fail_times = RuntimeError("crash"), 1
    env.cycle()
    ref = env.row()["batch_ref"]
    assert ref and store.get(env.row()["instruction_id"])["batch_notified_at"] is None
    env.advance(31)
    env.cycle()
    assert len(env.batch_emails()) == 1 and env.batch_ref() == ref
    env.advance(15)
    env.cycle()
    assert len(env.batch_emails()) == 1                              # stamped: no further copies


def test_two_approvers_get_one_batch_each_single_approver_exactly_one(env):
    kw = second_sender(env)
    env.instruct(body_for("101.00"))
    env.instruct(body_for("102.00"), **kw)
    env.cycle()
    batches = env.batch_emails()
    assert sorted(str(b["To"]) for b in batches) == ["anna@example.com", OWNER] and len(env.smtp.sent) == 2
    refs = {env.batch_ref(b) for b in batches}
    assert len(refs) == 2
    for b in batches:
        assert "ZAR 101.00" in str(b.get_content()) or "ZAR 102.00" in str(b.get_content())
        assert not ("ZAR 101.00" in str(b.get_content()) and "ZAR 102.00" in str(b.get_content()))


def test_batch_capped_at_max_items_surplus_offered_next_cycle(tmp_path):
    env = Env(tmp_path, payments_max_batch_items=2)
    instruct_many(env, "101.00", "102.00", "103.00")
    env.cycle()
    assert [r["item_no"] for r in env.rows() if r["batch_ref"]] == [1, 2] and len([r for r in env.rows() if not r["batch_ref"]]) == 1
    env.advance(15)
    env.cycle()
    assert len(env.batch_emails()) == 2 and sorted(r["item_no"] for r in env.rows()) == [1, 1, 2]


def test_batch_deferred_when_beneficiary_list_unavailable(env):
    env.store.create({
        "instruction_id": "id-manual", "status": PENDING, "path": "registered", "amount": "100.00", "source_account_id": "acc-1",
        "source_account_last3": "123", "payee_name_norm": "acme trading", "beneficiary_id": "ben-acme", "beneficiary_fingerprint": "x",
        "figures_source": "typed", "message_id_hash": "h", "notify_to": OWNER, "received_at": env.now,
        "expires_at": env.now + timedelta(hours=24), "updated_at": env.now})
    env.client.fail_beneficiaries = RuntimeError("down")
    summary = env.cycle()
    assert env.batch_emails() == [] and env.row()["batch_ref"] is None and summary["batches_sent"] == 0
    assert env.audit_entries("offer_deferred_list_unavailable")


def test_beneficiary_removed_before_offer_parks_and_is_not_batched(env):
    env.store.create({
        "instruction_id": "id-manual", "status": PENDING, "path": "registered", "amount": "100.00", "source_account_id": "acc-1",
        "source_account_last3": "123", "payee_name_norm": "acme trading", "beneficiary_id": "ben-gone",
        "beneficiary_fingerprint": "x", "figures_source": "typed", "message_id_hash": "h", "notify_to": OWNER,
        "received_at": env.now, "expires_at": env.now + timedelta(hours=24), "updated_at": env.now})
    env.cycle()
    row = env.row()
    assert row["status"] == "parked" and row["outcome_code"] == "beneficiary_removed" and env.batch_emails() == []
    assert [s for s in env.smtp.subjects() if "not actioned" in s]


def test_over_per_payment_cap_item_is_parked_and_never_offered(env):
    env.instruct(body_for("20000.01"))
    env.cycle()
    row = env.row()
    assert row["status"] == "parked" and row["outcome_code"] == "over_per_payment_cap" and env.batch_emails() == []
    assert env.payment_calls() == 0 and len([s for s in env.smtp.subjects() if "not actioned" in s]) == 1


def test_pre_offer_cap_check_also_covers_rows_that_became_ready_later(env):
    env.store.create({
        "instruction_id": "id-manual", "status": PENDING, "path": "registered", "amount": "30000.00", "source_account_id": "acc-1",
        "source_account_last3": "123", "payee_name_norm": "acme trading", "beneficiary_id": "ben-acme",
        "beneficiary_fingerprint": mod("fingerprints").beneficiary_fingerprint(ACME_RAW, "test-fingerprint-key"),
        "figures_source": "typed", "message_id_hash": "h", "notify_to": OWNER, "received_at": env.now,
        "expires_at": env.now + timedelta(hours=24), "updated_at": env.now})
    env.cycle()
    assert env.row()["status"] == "parked" and env.row()["outcome_code"] == "over_per_payment_cap"
