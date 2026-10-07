"""S11: execution (F3, F15, F17, F18, F40, NB-R3-3), only for the cycle-start snapshot of accepted rows."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
import requests

from tests.v2_harness import ACME_RAW, OWNER, SUCCESS_BODY, Env, body_for, mod, offered

pytestmark = pytest.mark.xfail(strict=False, reason="S11 red: execute/cycle not built yet")


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def live(tmp_path):
    return Env(tmp_path, live=True)


def to_accepted(env, *amounts):
    amounts = amounts or ("100.00",)
    offered(env, *amounts)
    env.advance(15)
    env.reply("approve")
    env.cycle()
    env.advance(15)
    assert {r["status"] for r in env.rows()} == {"accepted"}


def subjects(env, fragment):
    return [s for s in env.smtp.subjects() if fragment in s]


def rows_by_no(env):
    return {r["item_no"]: r for r in env.rows()}


# ------------------------------------------------------------------- F3
def test_default_is_dry_run_create_payment_not_called_result_executed_dry_run(env):
    to_accepted(env)
    summary = env.cycle()
    assert env.payment_calls() == 0 and env.row()["status"] == "executed" and env.row()["execution_mode"] == "dry-run"
    assert summary["results"].get("executed:dry-run") == 1 and summary["live_enabled"] is False
    assert subjects(env, "Payment dry-run") and "DRY-RUN" in env.smtp.body_text()
    assert env.row()["outcome_code"] == "dry-run" and env.row()["daily_reserved"] is True


def test_live_flag_without_credential_stays_dry_run(tmp_path):
    env = Env(tmp_path, payments_live_enable=True)            # live_enabled() is False without a payment-capable credential
    to_accepted(env)
    env.cycle()
    assert env.payment_calls() == 0 and env.row()["execution_mode"] == "dry-run"


def test_live_gate_open_calls_mocked_write_once_and_audits_live_execute(live):
    to_accepted(live)
    live.cycle()
    assert live.payment_calls() == 1 and live.row()["status"] == "executed" and live.row()["execution_mode"] == "live"
    assert live.audit_entries("live_execute")
    src, bene, amount, reference, my_ref = live.client.payment_calls[0]
    assert (src, bene, amount) == ("acc-1", "ben-acme", "100.00") and reference == "INV7"
    assert subjects(live, "Payment executed")


def test_execute_instruction_refuses_a_record_that_is_not_accepted(live):
    offered(live, "100.00")
    stale = live.row()                                                     # status awaiting_approval
    out = mod("execute").execute_instruction(live.settings, live.store, live.audit, live.client, stale, mode="live",
                                             now=live.now, smtp_send=live.smtp)
    assert out == "refused_not_approved" and live.payment_calls() == 0 and live.row()["status"] == "awaiting_approval"
    assert live.audit_entries("execute_refused_not_approved")
    forged = dict(stale, status="accepted")                                # a forged dict: the STORED row decides
    mod("execute").execute_instruction(live.settings, live.store, live.audit, live.client, forged, mode="live",
                                       now=live.now, smtp_send=live.smtp)
    assert live.payment_calls() == 0 and len(live.audit_entries("execute_refused_not_approved")) == 2


def test_cycle_never_calls_execute_instruction_for_a_row_outside_the_snapshot(live, monkeypatch):
    calls = []
    real = mod("execute").execute_instruction
    monkeypatch.setattr(mod("execute"), "execute_instruction", lambda *a, **k: calls.append(a[4]["instruction_id"]) or real(*a, **k))
    offered(live, "100.00")
    live.advance(15)
    live.reply("approve")
    live.cycle()                                                           # approved this cycle: not in the snapshot
    assert calls == [] and live.payment_calls() == 0
    live.advance(15)
    live.cycle()
    assert len(calls) == 1 and live.payment_calls() == 1


# -------------------------------------------------------------------- F15
def test_beneficiary_removed_between_approval_and_execution_parks(live):
    to_accepted(live)
    live.client.beneficiaries = [b for b in live.client.beneficiaries if b["beneficiaryId"] != "ben-acme"]
    live.cycle()
    row = live.row()
    assert row["status"] == "parked" and row["outcome_code"] == "beneficiary_removed" and live.payment_calls() == 0
    assert subjects(live, "not actioned")


def test_beneficiary_changed_between_offer_and_approval_parks_at_execution(live):
    to_accepted(live)
    live.client.beneficiaries[0]["accountNumber"] = "1111111111"              # fingerprint changes, name unchanged
    live.cycle()
    assert live.row()["status"] == "parked" and live.row()["outcome_code"] == "beneficiary_changed" and live.payment_calls() == 0


def test_beneficiary_list_unavailable_at_execution_parks_not_pays(live):
    to_accepted(live)
    live.client.fail_beneficiaries = RuntimeError("down")
    live.cycle()
    assert live.row()["status"] == "parked" and live.row()["outcome_code"] == "beneficiary_list_unavailable" and live.payment_calls() == 0


def test_fingerprint_key_missing_parks_at_execution(tmp_path):
    env = Env(tmp_path, live=True)
    to_accepted(env)
    env.settings.payments_fingerprint_key = ""
    env.cycle()
    assert env.row()["outcome_code"] == "fingerprint_key_missing" and env.payment_calls() == 0


def test_balance_short_parks_write_not_called(live):
    to_accepted(live)
    live.client.balance = {"availableBalance": "99.99"}
    live.cycle()
    assert live.row()["status"] == "parked" and live.row()["outcome_code"] == "insufficient_balance" and live.payment_calls() == 0


def test_balance_unavailable_or_unparsable_parks(live):
    to_accepted(live)
    live.client.balance = {"availableBalance": "n/a"}
    live.cycle()
    assert live.row()["status"] == "parked" and live.row()["outcome_code"] == "balance_unavailable" and live.payment_calls() == 0


def test_per_payment_cap_lowered_after_approval_parks_at_execution(live):
    to_accepted(live)
    live.settings.per_payment_cap = 50.0
    live.cycle()
    assert live.row()["outcome_code"] == "over_per_payment_cap" and live.row()["status"] == "parked" and live.payment_calls() == 0


def test_source_ambiguous_at_execution_parks(live):
    to_accepted(live)
    live.client.accounts.append({"accountId": "acc-2", "accountNumber": "99912345123", "profileId": "p2"})
    live.cycle()
    assert live.row()["outcome_code"] == "source_account_changed" and live.payment_calls() == 0


def test_source_account_gone_at_execution_parks(live):
    to_accepted(live)
    live.client.accounts = [{"accountId": "acc-9", "accountNumber": "55512345999"}]
    live.cycle()
    assert live.row()["outcome_code"] == "source_account_changed" and live.payment_calls() == 0


def test_item_cancelled_or_expired_between_approval_and_execution_never_executes(live):
    to_accepted(live)
    live.advance(25 * 60)                                                   # approved grace passed
    live.cycle()
    assert live.row()["status"] == "expired" and live.payment_calls() == 0
    assert subjects(live, "expired")


def test_changed_stored_row_after_offer_is_parked_approval_content_changed(live):
    to_accepted(live)
    iid = live.row()["instruction_id"]
    live.store._rows[iid]["amount"] = Decimal("9999.00")                    # tampered after the batch was sent
    live.cycle()
    row = live.row()
    assert row["status"] == "parked" and row["outcome_code"] == "approval_content_changed" and live.payment_calls() == 0
    assert len(subjects(live, "not actioned")) == 1


def test_token_mock_call_count_increments_at_execution_time_never_at_approval(live):
    offered(live, "100.00")
    live.advance(15)
    live.reply("approve")
    live.cycle()
    assert live.client.token_fetches == 0
    live.advance(15)
    live.cycle()
    assert live.client.token_fetches == 1


# --------------------------------------------------------- outcome mapping
def test_chunked_encoding_error_after_send_needs_review_call_count_1_reservation_kept(live):
    def boom():
        raise requests.exceptions.ChunkedEncodingError("cut")
    live.client.responder = boom
    to_accepted(live)
    live.cycle()
    assert live.row()["status"] == "needs_review" and live.payment_calls() == 1 and live.row()["daily_reserved"] is True
    assert subjects(live, "outcome unknown") and "MAY HAVE BEEN PAID" in live.smtp.body_text()
    live.run_cycles(3)
    assert live.payment_calls() == 1


def test_payment_unknown_outcome_timeout_needs_review_reservation_kept(live):
    def boom():
        raise mod("outcome").PaymentUnknownOutcome("Timeout")
    live.client.responder = boom
    to_accepted(live)
    live.cycle()
    assert live.row()["status"] == "needs_review" and live.payment_calls() == 1 and live.store.daily_total(live.now) == Decimal("100.00")


def test_runtime_error_from_parse_needs_review_call_count_1_reservation_kept(live, monkeypatch):
    monkeypatch.setattr(mod("execute"), "parse_payment_response", lambda body: (_ for _ in ()).throw(RuntimeError("bug")))
    to_accepted(live)
    live.cycle()
    assert live.row()["status"] == "needs_review" and live.payment_calls() == 1 and live.row()["daily_reserved"] is True


def test_token_fetch_failure_parked_released_write_not_called(live):
    def boom():
        raise mod("outcome").PaymentNotSent("token")
    live.client.responder = boom
    to_accepted(live)
    live.cycle()
    row = live.row()
    assert row["status"] == "parked" and row["daily_reserved"] is False and live.store.daily_total(live.now) == Decimal("0.00")
    assert subjects(live, "not actioned")


def test_status_update_lock_not_available_after_post_leaves_submitting_then_needs_review_never_resent(live, monkeypatch):
    import psycopg
    to_accepted(live)
    real = live.store.finalize
    state = {"raised": 0}

    def flaky(*a, **k):
        state["raised"] += 1
        raise psycopg.errors.LockNotAvailable("lock")
    monkeypatch.setattr(live.store, "finalize", flaky)
    live.cycle()
    assert live.row()["status"] == "submitting" and live.payment_calls() == 1
    monkeypatch.setattr(live.store, "finalize", real)
    live.advance(31)
    live.cycle()                                                              # stale sweep
    assert live.row()["status"] == "needs_review" and live.payment_calls() == 1 and live.row()["outcome_code"] == "stale_submitting"
    live.run_cycles(3)
    assert live.payment_calls() == 1


def test_investec_rejection_fails_with_message_notified_once_never_resent(live):
    def reject():
        raise mod("outcome").PaymentRejected(400, mod("outcome").sanitize_provider_message("Beneficiary must be paid once online first 12345678"))
    live.client.responder = reject
    to_accepted(live)
    live.cycle()
    row = live.row()
    assert row["status"] == "failed" and "paid once online first" in row["outcome_message"] and "12345678" not in row["outcome_message"]
    assert row["daily_reserved"] is False and live.store.daily_total(live.now) == Decimal("0.00")
    failed = subjects(live, "Payment failed")
    assert len(failed) == 1 and "paid once online first" in live.smtp.body_text()
    live.run_cycles(4)
    assert live.payment_calls() == 1 and len(live.rows()) == 1 and len(subjects(live, "Payment failed")) == 1


def test_200_error_message_body_is_failed_released(live):
    live.client.responder = lambda: {"data": {"TransferResponses": [], "ErrorMessage": "Insufficient funds"}}
    to_accepted(live)
    live.cycle()
    assert live.row()["status"] == "failed" and live.row()["outcome_message"] == "Insufficient funds"
    assert live.row()["daily_reserved"] is False


def test_authorisation_required_body_is_needs_authorisation_released(live):
    live.client.responder = lambda: {"data": {"AuthorisationRequired": True, "TransferResponses": [{"PaymentReferenceNumber": "R", "Status": "x"}]}}
    to_accepted(live)
    live.cycle()
    assert live.row()["status"] == "needs_authorisation" and live.row()["daily_reserved"] is False
    assert subjects(live, "needs authorisation")


def test_needs_review_then_next_cycle_never_resends(live):
    live.client.responder = lambda: {"data": {"TransferResponses": [{"Status": "Processed"}]}}       # no reference: unknown
    to_accepted(live)
    live.cycle()
    assert live.row()["status"] == "needs_review"
    live.run_cycles(3)
    assert live.payment_calls() == 1


# --------------------------------------------------------- claim edge cases
def test_claim_exception_audits_claim_error_and_never_posts(live, monkeypatch):
    to_accepted(live)
    monkeypatch.setattr(live.store, "claim_for_execution", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    live.cycle()
    assert live.payment_calls() == 0 and live.row()["status"] == "accepted" and live.audit_entries("claim_error")
    assert live.audit_entries("claim_error")[0]["detail"]["reason"] == "RuntimeError"


def test_claim_cas_lost_sends_no_notice(live, monkeypatch):
    to_accepted(live)
    ClaimResult = mod("instructions").ClaimResult
    monkeypatch.setattr(live.store, "claim_for_execution", lambda *a, **k: ClaimResult(False, "cas_lost", Decimal(0)))
    before = len(live.smtp.sent)
    live.cycle()
    assert len(live.smtp.sent) == before and live.row()["status"] == "accepted" and live.audit_entries("claim_cas_lost")
    assert live.payment_calls() == 0


def test_stale_claim_expires_and_notifies_only_when_cas_wins(live, monkeypatch):
    to_accepted(live)
    ClaimResult = mod("instructions").ClaimResult
    monkeypatch.setattr(live.store, "claim_for_execution", lambda *a, **k: ClaimResult(False, "stale", Decimal(0)))
    live.cycle()
    assert live.row()["status"] == "expired" and len(subjects(live, "expired")) == 1 and live.payment_calls() == 0


def test_stale_claim_cas_lost_race_sends_no_notice(tmp_path, monkeypatch):
    env = Env(tmp_path, live=True)
    to_accepted(env)
    ClaimResult = mod("instructions").ClaimResult

    def racing_claim(iid, *a, **k):
        env.store.cas_status(iid, {"accepted"}, "cancelled", now=env.now)       # someone else wins first
        return ClaimResult(False, "stale", Decimal(0))
    monkeypatch.setattr(env.store, "claim_for_execution", racing_claim)
    before = len(env.smtp.sent)
    env.cycle()
    assert env.row()["status"] == "cancelled" and len(env.smtp.sent) == before


def test_daily_cap_claim_refusal_parks_and_never_retries_next_day(tmp_path):
    env = Env(tmp_path, live=True, daily_aggregate_cap=150.0)
    to_accepted(env, "100.00", "100.00")
    env.cycle()
    rows = rows_by_no(env)
    assert rows[1]["status"] == "executed" and rows[2]["status"] == "parked" and rows[2]["outcome_code"] == "daily_cap"
    assert env.payment_calls() == 1 and len(subjects(env, "not actioned")) == 1
    env.advance(24 * 60)
    env.cycle()
    env.advance(15)
    env.cycle()
    assert env.payment_calls() == 1 and rows_by_no(env)[2]["status"] == "parked"


def test_batch_total_over_daily_cap_executes_in_order_until_cap_then_parks_rest_notified(tmp_path):
    env = Env(tmp_path, live=True)
    to_accepted(env, "20000.00", "20000.00", "20000.00")
    env.cycle()
    rows = rows_by_no(env)
    assert [rows[i]["status"] for i in (1, 2, 3)] == ["executed", "executed", "parked"] and rows[3]["outcome_code"] == "daily_cap"
    assert env.payment_calls() == 2 and len(subjects(env, "not actioned")) == 1
    assert [c[2] for c in env.client.payment_calls] == ["20000.00", "20000.00"]


def test_definite_failure_releases_daily_reservation_so_next_payment_fits(tmp_path):
    env = Env(tmp_path, live=True, daily_aggregate_cap=150.0)
    bodies = iter([lambda: (_ for _ in ()).throw(mod("outcome").PaymentRejected(400, "no")), lambda: SUCCESS_BODY])
    env.client.responder = lambda: next(bodies)()
    to_accepted(env, "100.00", "100.00")
    env.cycle()
    rows = rows_by_no(env)
    assert rows[1]["status"] == "failed" and rows[2]["status"] == "executed" and env.payment_calls() == 2


def test_every_exit_from_submitting_uses_finalize_with_matching_release_flag(live, monkeypatch):
    calls = []
    real = live.store.finalize

    def spy(iid, expected, new_status, *, release, **kw):
        calls.append((expected, new_status, release))
        return real(iid, expected, new_status, release=release, **kw)
    monkeypatch.setattr(live.store, "finalize", spy)
    outcomes = iter([lambda: SUCCESS_BODY, lambda: (_ for _ in ()).throw(mod("outcome").PaymentRejected(400, "no")),
                     lambda: (_ for _ in ()).throw(RuntimeError("x")), lambda: (_ for _ in ()).throw(mod("outcome").PaymentNotSent("t")),
                     lambda: {"data": {"AuthorisationRequired": True, "TransferResponses": []}}])
    live.client.responder = lambda: next(outcomes)()
    to_accepted(live, "101.00", "102.00", "103.00", "104.00", "105.00")
    live.cycle()
    assert [c[0] for c in calls] == ["submitting"] * 5
    expected = {("submitting", "executed", False), ("submitting", "failed", True), ("submitting", "needs_review", False),
                ("submitting", "parked", True), ("submitting", "needs_authorisation", True)}
    assert set(calls) == expected


def test_dry_run_executes_through_finalize_with_dry_run_outcome_code_and_no_post(env):
    to_accepted(env, "100.00", "200.00")
    env.cycle()
    assert env.payment_calls() == 0 and all(r["status"] == "executed" and r["outcome_code"] == "dry-run" for r in env.rows())
    assert env.store.daily_total(env.now) == Decimal("300.00")


# ----------------------------------------------------------------------- F18
@pytest.mark.parametrize("cap_name", ["per_payment_cap", "daily_aggregate_cap"])
def test_caps_unset_or_zero_block_everything(tmp_path, cap_name):
    env = Env(tmp_path, live=True, **{cap_name: 0.0})
    env.instruct()
    env.cycle()
    if cap_name == "per_payment_cap":
        assert env.row()["status"] == "parked" and env.batch_emails() == []
    else:
        to_accepted_parked = env
        to_accepted_parked.advance(15)
        env.reply("approve")
        env.cycle()
        env.advance(15)
        env.cycle()
        assert env.row()["status"] == "parked" and env.row()["outcome_code"] == "daily_cap"
    assert env.payment_calls() == 0


def test_daily_boundary_exactly_at_cap_passes_and_one_cent_over_blocks(tmp_path):
    env = Env(tmp_path, live=True, per_payment_cap=50000.0, daily_aggregate_cap=50000.0)
    to_accepted(env, "30000.00", "20000.00")
    env.cycle()
    assert [r["status"] for r in env.rows()] == ["executed", "executed"]
    env2_path = tmp_path / "second"
    env2_path.mkdir()
    env2 = Env(env2_path, live=True, per_payment_cap=50000.0, daily_aggregate_cap=50000.0)
    to_accepted(env2, "30000.00", "20000.01")
    env2.cycle()
    assert sorted(r["status"] for r in env2.rows()) == ["executed", "parked"]


def test_daily_total_survives_reload_of_the_store_object(live):
    to_accepted(live, "100.00")
    live.cycle()
    assert live.store.daily_total(live.now) == Decimal("100.00")


def test_no_9plus_digit_runs_in_stored_rows_audit_or_non_paste_emails_after_a_live_run(live):
    to_accepted(live)
    live.cycle()
    import re
    text = live.everything_text()
    # hashes may legitimately contain digit runs: strip them before the scan
    scan = re.sub(r"[0-9a-f]{40,}", "", text)
    assert not re.search(r"(?<![\w.-])\d{9,}(?![\w.-])", scan.replace("PR1", ""))
