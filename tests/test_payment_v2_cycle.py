"""S11: the v2 cycle end to end (identity, age gate, routing, duplicates, hold, sweeps, emails, audit). Offline."""
from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.v2_harness import ACME_RAW, KEY, OWNER, T0, Env, body_for, mod, offered



@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def subjects(env, fragment):
    return [s for s in env.smtp.subjects() if fragment in s]


def msg_outcome(env, msg):
    iid = mod("refs").instruction_id_for(msg.message_id, msg.from_headers)
    return env.store._messages[iid]["outcome"]


NOBODY = {"beneficiaryId": "ben-nobody", "beneficiaryName": "Nobody Ltd", "accountNumber": "5550001111", "code": "250655"}


# ---------------------------------------------------------- B2/B3/B4/B5, NB5
def test_pay_123_alone_never_creates_instruction(env):
    msg = env.instruct("pay 123")
    env.cycle()
    assert env.rows() == [] and len(subjects(env, "not actioned")) == 1 and msg_outcome(env, msg).startswith("parked_")


def test_trigger_digits_not_in_amount_candidates(env):
    env.instruct("pay 123\nPayee: Acme Trading\n")
    env.cycle()
    assert env.rows() == []


@pytest.mark.parametrize("body", [
    "pay 123 payee Acme Ltd ref INV7781", "pay 123\nPayee: Acme Trading\nRef INV7781", "pay 123\nPayee: Acme Trading\nunit 14\nPO 20260",
])
def test_cycle_level_no_marked_amount_is_none(env, body):
    env.instruct(body)
    env.cycle()
    assert env.rows() == [] and env.batch_emails() == []


def test_marked_amount_with_reference_digits_gives_the_marked_amount(env):
    env.instruct("pay 123\nPayee: Acme Trading\nAmount: R500\nRef INV2026\n")
    env.cycle()
    assert env.row()["amount"] == Decimal("500.00")


def test_same_message_processed_twice_with_different_extraction_one_payment(env):
    msg = env.instruct()
    env.cycle()
    env.advance(15)
    env.extractor = mod("images").FakeImageExtractor({"x": {"amount": "999.00"}})
    env.inbox.queue(msg)
    env.cycle()
    assert len(env.rows()) == 1 and env.row()["amount"] == Decimal("100.00")


def test_empty_message_id_rejected(env):
    msg = env.instruct(message_id="")
    env.cycle()
    assert env.rows() == [] and env.smtp.sent == [] and env.audit_entries("no_message_id")
    assert msg.message_id == ""


def test_ignored_message_not_reprocessed(env):
    msg = env.instruct("hello there, no trigger")
    env.cycle()
    assert env.rows() == [] and env.smtp.sent == []
    spy = []
    real = mod("content").text_view
    mod("content").text_view = lambda m: spy.append(1) or real(m)
    try:
        env.inbox.queue(msg)
        env.cycle()
    finally:
        mod("content").text_view = real
    assert spy == []


def test_created_false_never_notifies_or_executes(env):
    msg = env.instruct(body_for(payee="Nobody Ltd"))
    env.cycle()
    assert len(subjects(env, "Add this beneficiary")) == 1
    env.store._messages.clear()                                         # message-seen row lost (e.g. cleanup)
    env.inbox.queue(msg)
    env.advance(15)
    env.cycle()
    assert len(env.rows()) == 1 and len(subjects(env, "Add this beneficiary")) == 1 and env.payment_calls() == 0


def test_unread_backlog_older_than_window_is_expired_not_paid(env):
    env.instruct(internaldate=env.now - timedelta(hours=48))
    env.cycle()
    assert env.rows() == [] and len(subjects(env, "not actioned")) == 1 and "expired_age" in env.smtp.body_text()


def test_re_marked_unread_old_mail_not_paid(env):
    msg = env.instruct(internaldate=env.now - timedelta(hours=26))
    env.cycle()
    env.store._messages.clear()
    env.inbox.queue(msg)
    env.advance(15)
    env.cycle()
    assert env.rows() == [] and env.payment_calls() == 0


def test_reprocessing_after_retention_cleanup_not_paid(env):
    msg = env.instruct()
    env.cycle()
    env.advance(26 * 60)
    env.store._messages.clear()
    env.inbox.queue(msg)
    env.cycle()
    assert len(env.rows()) == 1 and env.row()["status"] == "expired" and env.payment_calls() == 0
    assert "expired_age" in env.smtp.body_text()


def test_date_header_spoof_does_not_make_old_mail_fresh(env):
    env.instruct(internaldate=env.now - timedelta(hours=48), headers={"Date": "Wed, 07 Oct 2026 10:00:00 +0000"})
    env.cycle()
    assert env.rows() == []


def test_missing_received_time_fails_closed(env):
    env.instruct(internaldate=None)
    env.cycle()
    assert env.rows() == [] and "expired_age" in env.smtp.body_text()


def test_received_at_older_of_internaldate_and_received_header(env):
    env.instruct(internaldate=env.now - timedelta(minutes=1), received_header="Mon, 05 Oct 2026 08:00:00 +0000")
    env.cycle()
    assert env.rows() == []                                             # the OLDER (header) time is used
    env.instruct(body_for("200.00"), internaldate=env.now - timedelta(hours=40), received_header="Wed, 07 Oct 2026 10:00:00 +0000")
    env.cycle()
    assert env.rows() == []
    env.instruct(body_for("300.00"), internaldate=env.now - timedelta(minutes=1), received_header="Wed, 07 Oct 2026 09:50:00 +0000")
    env.cycle()
    assert len(env.rows()) == 1 and env.row()["received_at"] == T0 - timedelta(minutes=10)


def test_expired_age_mail_from_unauthenticated_sender_gets_no_email(env):
    env.instruct(internaldate=env.now - timedelta(hours=48), auth=None)
    env.cycle()
    assert env.smtp.sent == [] and env.rows() == []


def test_old_mail_with_no_trigger_is_ignored_without_any_email(env):
    env.instruct("just chatting", internaldate=env.now - timedelta(hours=48))
    env.cycle()
    assert env.smtp.sent == []


def test_retention_shorter_than_window_skips_cleanup(tmp_path):
    env = Env(tmp_path, retention_days=1)
    env.store.create({"instruction_id": "old", "status": "parked", "path": "registered", "amount": "1.00", "source_account_id": "a",
                      "source_account_last3": "123", "figures_source": "typed", "message_id_hash": "h", "notify_to": OWNER,
                      "received_at": T0 - timedelta(days=30), "expires_at": T0, "updated_at": T0 - timedelta(days=30), "outcome_code": "x"})
    env.cycle()
    assert env.store.get("old") is not None and env.audit_entries("retention_too_short")


def test_cleanup_runs_when_retention_is_valid(env):
    env.store.create({"instruction_id": "old", "status": "parked", "path": "registered", "amount": "1.00", "source_account_id": "a",
                      "source_account_last3": "123", "figures_source": "typed", "message_id_hash": "h", "notify_to": OWNER,
                      "received_at": T0 - timedelta(days=300), "expires_at": T0, "updated_at": T0 - timedelta(days=300), "outcome_code": "x"})
    env.cycle()
    assert env.store.get("old") is None


# -------------------------------------------------------------- TR3-3, TF
def test_all_messages_get_processing_rows_before_any_is_processed(env, monkeypatch):
    seen = []
    real = mod("content").text_view
    monkeypatch.setattr(mod("content"), "text_view", lambda m: seen.append(len(env.store._messages)) or real(m))
    for amount in ("101.00", "102.00", "103.00"):
        env.instruct(body_for(amount))
    env.cycle()
    assert seen and seen[0] == 3


def test_exception_in_message_sets_outcome_error_and_cycle_continues(env, monkeypatch):
    first = env.instruct(body_for("101.00"))
    env.instruct(body_for("102.00"))
    calls = {"n": 0}
    real = mod("accounts").resolve_source_unique

    def flaky(accounts, last3):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(accounts, last3)
    monkeypatch.setattr(mod("accounts"), "resolve_source_unique", flaky)
    env.cycle()
    assert msg_outcome(env, first) == "error" and len(env.rows()) == 1 and env.row()["amount"] == Decimal("102.00")
    assert env.audit_entries("message_error")[0]["detail"]["reason"] == "RuntimeError"


def test_message1_raises_message2_approval_still_processed_message1_reported_by_outcome_error(env, monkeypatch):
    offered(env, "101.00")
    env.advance(15)
    bad = env.instruct(body_for("102.00"))
    env.reply("approve")
    monkeypatch.setattr(mod("accounts"), "resolve_source_unique", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    env.cycle()
    assert msg_outcome(env, bad) == "error" and env.row(0)["status"] == "accepted"


def test_stuck_processing_message_gets_resend_notice_only_if_authenticated_and_once(env):
    store = env.store
    store.mark_message_seen("m-auth", env.now - timedelta(minutes=40))
    store.set_message_outcome("m-auth", "processing", auth_from=OWNER)
    store.mark_message_seen("m-anon", env.now - timedelta(minutes=40))
    env.cycle()
    resend = subjects(env, "Please resend")
    assert len(resend) == 1 and str(env.smtp.sent[-1]["To"]) == OWNER
    env.advance(15)
    env.cycle()
    assert len(subjects(env, "Please resend")) == 1


# ---------------------------------------------------------- routing at cycle level
def test_registered_payee_exact_match_passes_the_registered_id_to_the_write(tmp_path):
    env = Env(tmp_path, live=True)
    env.instruct()
    env.cycle()
    env.advance(15)
    env.reply("approve")
    env.cycle()
    env.advance(15)
    env.cycle()
    assert env.client.payment_calls[0][1] == "ben-acme"


def test_near_miss_name_is_new_payee_with_paste_email(env):
    env.instruct(body_for(payee="Acme Trade"))
    env.cycle()
    assert env.row()["status"] == "awaiting_beneficiary" and len(subjects(env, "Add this beneficiary")) == 1 and env.payment_calls() == 0


def test_paste_email_contains_the_bank_details_only_for_the_verified_sender(env):
    env.instruct("pay 123\nPayee: Nobody Ltd\nAmount: R50.00\nBank: FNB\nAccount number: 5550001111\nBranch code: 250655\nReference: INV7\n")
    env.cycle()
    paste = [m for m in env.smtp.sent if "Add this beneficiary" in str(m["Subject"])][0]
    text = str(paste.get_content())
    assert "5550001111" in text and "Nobody Ltd" in text and str(paste["To"]) == OWNER
    row = env.row()
    assert row["account_hmac"] and "5550001111" not in str(row)


def test_two_same_name_beneficiaries_parks_not_new_payee(env):
    env.client.beneficiaries.append({**ACME_RAW, "beneficiaryId": "ben-acme-dup"})
    env.instruct()
    env.cycle()
    assert env.row()["status"] == "parked" and env.row()["outcome_code"] == "beneficiary_ambiguous" and env.row()["path"] == "new_payee"
    assert subjects(env, "Add this beneficiary") == []


def test_awaiting_instruction_with_two_exact_name_beneficiaries_parks_never_held_write_not_called(env):
    env.instruct(body_for(payee="Nobody Ltd"))
    env.cycle()
    env.client.beneficiaries += [NOBODY, {**NOBODY, "beneficiaryId": "ben-nobody-2"}]
    env.advance(15)
    env.cycle()
    assert env.row()["status"] == "parked" and env.row()["outcome_code"] == "beneficiary_ambiguous" and env.payment_calls() == 0


def test_registered_name_match_number_mismatch_parks(env):
    env.instruct("pay 123\nPayee: Acme Trading\nAmount: R100.00\nAccount number: 1111111111\n")
    env.cycle()
    assert env.row()["status"] == "parked" and env.row()["outcome_code"] == "account_number_mismatch" and env.batch_emails() == []


def test_registered_name_match_with_matching_number_is_batched(env):
    env.instruct("pay 123\nPayee: Acme Trading\nAmount: R100.00\nAccount number: 1234567890\n")
    env.cycle()
    assert env.row()["status"] == "awaiting_approval" and env.row()["account_hmac"]


def test_no_number_extracted_name_only_ok(env):
    env.instruct()
    env.cycle()
    assert env.row()["status"] == "awaiting_approval" and env.row()["account_hmac"] is None


def test_held_requires_hmac_match_when_number_extracted(env):
    env.instruct("pay 123\nPayee: Nobody Ltd\nAmount: R50.00\nAccount number: 9990001111\n")
    env.cycle()
    env.client.beneficiaries.append(NOBODY)                                  # listed number differs from the one in the mail
    env.advance(15)
    env.cycle()
    assert env.row()["status"] == "parked" and env.row()["outcome_code"] == "account_number_mismatch"


def test_beneficiary_list_unavailable_new_instruction_parks_never_new_payee(env):
    env.client.fail_beneficiaries = RuntimeError("down")
    env.instruct()
    env.cycle()
    assert env.row()["status"] == "parked" and env.row()["outcome_code"] == "beneficiary_list_unavailable"
    assert subjects(env, "Add this beneficiary") == [] and env.row()["path"] == "new_payee"


@pytest.mark.parametrize("case", ["possible_duplicate", "account_number_mismatch", "beneficiary_list_unavailable"])
def test_park_created_before_route_has_valid_path(env, case):
    if case == "possible_duplicate":
        env.instruct()
        env.cycle()
        env.advance(15)
        env.instruct()
        env.cycle()
        row = [r for r in env.rows() if r["status"] == "parked"][0]
        assert row["path"] == "registered" and row["outcome_code"] == "possible_duplicate"
    elif case == "account_number_mismatch":
        env.instruct("pay 123\nPayee: Acme Trading\nAmount: R100.00\nAccount number: 1111111111\n")
        env.cycle()
        row = env.row()
        assert row["path"] == "registered" and row["status"] == "parked"
    else:
        env.client.fail_beneficiaries = RuntimeError("down")
        env.instruct()
        env.cycle()
        row = env.row()
        assert row["path"] == "new_payee" and row["status"] == "parked"
    assert row["path"] in ("registered", "new_payee")


def test_park_route_creates_row_with_status_parked_and_outcome_code_one_email(env):
    env.instruct(body_for("20000.01"))
    env.cycle()
    assert env.row()["status"] == "parked" and env.row()["outcome_code"] == "over_per_payment_cap"
    assert len(subjects(env, "not actioned")) == 1


def test_parked_creation_rerun_sends_no_second_email(env):
    msg = env.instruct(body_for("20000.01"))
    env.cycle()
    env.store._messages.clear()
    env.inbox.queue(msg)
    env.advance(15)
    env.cycle()
    assert len(env.rows()) == 1 and len(subjects(env, "not actioned")) == 1


def test_pre_record_stop_creates_no_row_and_sets_message_outcome(env):
    msg = env.instruct("pay 123\nPayee: Acme Trading\nAmount: R100.00\nAmount: R200.00\n")
    env.cycle()
    assert env.rows() == [] and msg_outcome(env, msg) == "parked_amount_conflict" and len(subjects(env, "not actioned")) == 1


def test_source_account_unresolved_or_ambiguous_is_a_pre_record_stop(env):
    msg = env.instruct(body_for(last3="999"))
    env.cycle()
    assert env.rows() == [] and msg_outcome(env, msg) == "parked_source_no_match"
    env.client.accounts.append({"accountId": "acc-2", "accountNumber": "99912345123"})
    msg2 = env.instruct(body_for(last3="123"))
    env.cycle()
    assert env.rows() == [] and msg_outcome(env, msg2) == "parked_source_ambiguous"


def test_every_created_record_carries_notify_to_from_auth_from_never_reply_to(env):
    env.instruct(headers={"Reply-To": "attacker@evil.example"})
    env.cycle()
    assert env.row()["notify_to"] == OWNER
    assert all(OWNER in str(m["To"]) and "evil" not in str(m["To"]) for m in env.smtp.sent)


def test_sweep_notifies_stored_notify_to_after_message_seen_cleanup(env):
    offered(env, "101.00")
    env.store._messages.clear()
    env.advance(25 * 60)
    env.cycle()
    assert env.row()["status"] == "expired"
    expiry = [m for m in env.smtp.sent if "expired" in str(m["Subject"])]
    assert len(expiry) == 1 and str(expiry[0]["To"]) == OWNER


# ---------------------------------------------------------------- duplicates
def test_same_source_payee_amount_within_window_parks_possible_duplicate(env):
    env.instruct()
    env.cycle()
    env.advance(15)
    env.instruct()
    env.cycle()
    rows = {r["status"] for r in env.rows()}
    assert rows == {"awaiting_approval", "parked"} and [r for r in env.rows() if r["status"] == "parked"][0]["outcome_code"] == "possible_duplicate"
    assert len(env.batch_emails()) == 1


def test_same_with_different_amount_not_duplicate(env):
    env.instruct()
    env.cycle()
    env.advance(15)
    env.instruct(body_for("101.00"))
    env.cycle()
    assert [r["status"] for r in env.rows()] == ["awaiting_approval", "awaiting_approval"]


def test_parked_then_resend_is_processed(env):
    env.client.fail_beneficiaries = RuntimeError("down")
    env.instruct()
    env.cycle()
    assert env.row()["status"] == "parked"
    env.client.fail_beneficiaries = None
    env.advance(15)
    env.instruct()
    env.cycle()
    assert sorted(r["status"] for r in env.rows()) == ["awaiting_approval", "parked"]


def test_failed_then_resend_is_processed(tmp_path):
    env = Env(tmp_path, live=True)

    def reject():
        raise mod("outcome").PaymentRejected(400, "no")
    env.client.responder = reject
    offered(env, "100.00")
    env.advance(15)
    env.reply("approve")
    env.cycle()
    env.advance(15)
    env.cycle()
    assert env.row()["status"] == "failed"
    env.advance(15)
    env.instruct()
    env.cycle()
    assert sorted(r["status"] for r in env.rows()) == ["awaiting_approval", "failed"]


@pytest.mark.parametrize("terminal", ["cancelled", "expired"])
def test_cancelled_or_expired_then_resend_is_processed(env, terminal):
    offered(env, "100.00")
    if terminal == "cancelled":
        env.advance(15)
        env.reply("cancel")
        env.cycle()
    else:
        env.advance(25 * 60)
        env.cycle()
    assert env.row()["status"] == terminal
    env.advance(15)
    env.instruct()
    env.cycle()
    assert sorted(r["status"] for r in env.rows()) == sorted(["awaiting_approval", terminal])


def test_executed_then_resend_parks_possible_duplicate(env):
    offered(env, "100.00")
    env.advance(15)
    env.reply("approve")
    env.cycle()
    env.advance(15)
    env.cycle()
    assert env.row()["status"] == "executed"
    env.advance(15)
    env.instruct()
    env.cycle()
    assert sorted(r["status"] for r in env.rows()) == ["executed", "parked"]


# ----------------------------------------------------------- hold / T12 / TA
def beneficiary_new(env, hold):
    env.settings.payments_hold_hours = float(hold)


def test_hold_zero_makes_registered_recent_flag_inert_at_cycle_level(tmp_path):
    env = Env(tmp_path, bootstrap=False)
    env.cycle()                                                     # bootstrap: Acme + Beta established
    env.client.beneficiaries.append(NOBODY)
    env.advance(15)
    env.cycle()                                                     # Nobody is new, not established
    env.instruct(body_for(payee="Nobody Ltd"))
    env.cycle()
    assert env.row()["status"] == "awaiting_approval" and env.row()["recent_beneficiary"] is False


def test_recent_registered_beneficiary_held_when_hold_nonzero(tmp_path):
    env = Env(tmp_path, bootstrap=False, payments_hold_hours=2.0)
    env.cycle()
    env.client.beneficiaries.append(NOBODY)
    env.advance(15)
    env.cycle()
    seen = env.now
    env.instruct(body_for(payee="Nobody Ltd"))
    env.cycle()
    row = env.row()
    assert row["status"] == "held" and row["recent_beneficiary"] is True and row["eligible_at"] == seen + timedelta(hours=2)
    assert row["first_seen_at"] == seen and env.batch_emails() == []


def test_bootstrap_beneficiaries_batchable_immediately(tmp_path):
    env = Env(tmp_path, bootstrap=False, payments_hold_hours=24.0)
    env.cycle()                                                     # first run with v2: existing beneficiaries are established
    env.instruct()
    env.cycle()
    assert env.row()["status"] == "awaiting_approval" and env.store.bootstrap_done()


def test_bootstrap_with_empty_list_then_first_added_beneficiary_is_recent_held_when_hold_nonzero(tmp_path):
    env = Env(tmp_path, bootstrap=False, payments_hold_hours=2.0)
    env.client.beneficiaries = []
    env.cycle()
    assert env.store.bootstrap_done()
    env.client.beneficiaries = [dict(ACME_RAW)]
    env.advance(15)
    env.cycle()
    env.instruct()
    env.cycle()
    assert env.row()["status"] == "held"


def test_fingerprint_change_on_established_beneficiary_is_recent_held_first_seen_unchanged(tmp_path):
    env = Env(tmp_path, payments_hold_hours=2.0)
    env.cycle()
    env.client.beneficiaries[0]["accountNumber"] = "1111111111"
    env.advance(15)
    env.cycle()
    changed = env.now
    env.instruct()
    env.cycle()
    assert env.row()["status"] == "held" and env.row()["eligible_at"] == changed + timedelta(hours=2)
    assert env.store.observe_beneficiary("ben-acme", env.now, fingerprint=None).first_seen_at == T0 - timedelta(days=1)


def test_unset_hold_means_zero_and_audit_note_for_fallback(tmp_path):
    env = Env(tmp_path, payments_hold_hours=24.0, payments_hold_hours_fallback=True)
    env.cycle()
    assert env.audit_entries("hold_setting_fallback")


def test_first_seen_at_set_once_later_cycles_and_store_reload_do_not_change_it(env):
    env.instruct(body_for(payee="Nobody Ltd"))
    env.cycle()
    env.client.beneficiaries.append(NOBODY)
    env.advance(15)
    env.cycle()
    first = env.store.get(env.row()["instruction_id"])["first_seen_at"]
    env.run_cycles(3)
    assert env.store.get(env.row()["instruction_id"])["first_seen_at"] == first


# --------------------------------------------------------------- sweeps
def test_sweep_sends_missing_set_once_notice_after_crash_then_marks_once(env):
    env.store.create({"instruction_id": "h1", "status": "held", "path": "new_payee", "amount": "100.00", "source_account_id": "acc-1",
                      "source_account_last3": "123", "payee_name_norm": "acme trading", "beneficiary_id": "ben-acme",
                      "beneficiary_fingerprint": "x", "figures_source": "typed", "message_id_hash": "h", "notify_to": OWNER,
                      "received_at": env.now, "first_seen_at": env.now, "eligible_at": env.now + timedelta(hours=5),
                      "expires_at": env.now + timedelta(hours=29), "updated_at": env.now})
    env.advance(31)
    env.cycle()
    assert len(subjects(env, "Payee now registered")) == 1 and env.store.get("h1")["hold_notified_at"] == env.now
    env.advance(15)
    env.cycle()
    assert len(subjects(env, "Payee now registered")) == 1


def test_held_row_with_eligible_at_in_the_future_is_not_made_ready(env):
    env.store.create({"instruction_id": "h1", "status": "held", "path": "registered", "amount": "100.00", "source_account_id": "acc-1",
                      "source_account_last3": "123", "payee_name_norm": "acme trading", "beneficiary_id": "ben-acme",
                      "beneficiary_fingerprint": mod("fingerprints").beneficiary_fingerprint(ACME_RAW, KEY),
                      "figures_source": "typed", "message_id_hash": "h", "notify_to": OWNER, "received_at": env.now,
                      "first_seen_at": env.now, "eligible_at": env.now + timedelta(hours=5), "expires_at": env.now + timedelta(hours=29),
                      "updated_at": env.now, "hold_notified_at": None})
    env.cycle()
    assert env.store.get("h1")["status"] == "held"
    env.advance(5 * 60)
    env.cycle()
    assert env.store.get("h1")["status"] == "awaiting_approval" and len(env.batch_emails()) == 1


def test_expiry_windows_apply_per_state(env):
    env.instruct(body_for(payee="Nobody Ltd"))
    env.cycle()
    env.advance(6 * 24 * 60)
    env.cycle()
    assert env.row()["status"] == "awaiting_beneficiary"
    env.advance(24 * 60 + 15)
    env.cycle()
    assert env.row()["status"] == "expired"


def test_expiry_notice_is_grouped_one_email_for_many_items(env):
    for amount in ("101.00", "102.00", "103.00"):
        env.instruct(body_for(amount))
    env.cycle()
    env.advance(25 * 60)
    env.cycle()
    expiry = [m for m in env.smtp.sent if "expired" in str(m["Subject"])]
    assert len(expiry) == 1 and str(expiry[0].get_content()).count("expired unapproved") == 3


# ------------------------------------------------------------------ emails, audit
def test_all_captured_emails_have_no_token_secret_or_full_account_number_except_paste(tmp_path):
    env = Env(tmp_path, live=True, anthropic_api_key="sk-ant-api03-AAAAAAAAAAAAAAAA")
    env.instruct("pay 123\nPayee: Acme Trading\nAmount: R100.00\nAccount number: 1234567890\nReference: INV7\n")
    env.instruct("pay 123\nPayee: Nobody Ltd\nAmount: R55.00\nAccount number: 5550001111\n")
    env.cycle()
    env.advance(15)
    env.reply("approve")
    env.cycle()
    env.advance(15)
    env.cycle()
    for m in env.smtp.sent:
        text = str(m.get_content()) + str(m["Subject"])
        if "Add this beneficiary" in str(m["Subject"]):
            continue
        assert "1234567890" not in text and "5550001111" not in text and "sk-ant" not in text and "test-fingerprint-key" not in text
        # the informational instruction tag [INV-<12 random hex>] is a hash-derived reference, not an account
        # number: 9+ consecutive decimal digits occur in it by chance (~1 run in 8), so mask it before the scan
        scan = re.sub(r"\[INV-[0-9a-f]{12}\]", "[INV-REF]", text)
        assert not re.search(r"\d{9,}", scan)


def test_no_reminder_or_confirm_request_email_is_ever_sent(env):
    offered(env, "100.00")
    env.run_cycles(20)
    assert not [s for s in env.smtp.subjects() if re.search(r"remind|confirm", s, re.I)]


def test_audit_lifecycle_is_ordered_and_append_only(tmp_path):
    env = Env(tmp_path, live=True)
    env.instruct()
    env.cycle()
    env.advance(15)
    env.reply("approve")
    env.cycle()
    env.advance(15)
    env.cycle()
    steps = env.audit_steps()
    wanted = ["cycle_start", "message_received", "auth_ok", "instruction_created", "batch_offered", "approve", "execute", "executed"]
    positions = [steps.index(s) for s in wanted]
    assert positions == sorted(positions), steps
    first = env.audit.read_entries()[:3]
    env.cycle()
    assert env.audit.read_entries()[:3] == first


def test_no_digit_runs_secrets_body_or_image_bytes_anywhere(tmp_path, caplog):
    png = open("tests/fixtures/payments_v2/images/tiny.png", "rb").read()
    env = Env(tmp_path, live=True, anthropic_api_key="sk-ant-api03-ZZZZZZZZZZZZZZZZ")
    caplog.set_level("DEBUG")
    env.instruct("pay 123\nPayee: Acme Trading\nAmount: R100.00\nAccount number: 1234567890\nsecret-body-marker\n",
                 inline=(("p.png", "image", "png", png),))
    env.cycle()
    env.advance(15)
    env.reply("approve")
    env.cycle()
    env.advance(15)
    env.cycle()
    blob = env.everything_text() + caplog.text
    scan = re.sub(r"[0-9a-f]{40,}", "", blob)
    assert "sk-ant" not in blob and "secret-body-marker" not in blob and "iVBOR" not in blob
    assert "1234567890" not in scan
    assert not re.search(r"(?<![\w.-])\d{9,}(?![\w.-])", scan)


def test_summary_has_v2_keys_and_counts(env):
    env.instruct()
    summary = env.cycle()
    for key in ("mode", "live_enabled", "messages", "offered", "batches_sent", "approved", "cancelled", "executed", "expired",
                "parked", "results"):
        assert key in summary
    assert summary["mode"] == "dry-run" and summary["messages"] == 1 and summary["offered"] == 1


def test_failure_after_fetch_sends_generic_notice(env, monkeypatch):
    env.instruct()
    monkeypatch.setattr(mod("routing"), "beneficiary_path", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret detail 123456789")))
    monkeypatch.setattr(env.store, "list_by_status", lambda *a, **k: [] if not env.inbox.fetch_calls else (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(Exception):
        env.cycle()
    cycle_failed = [m for m in env.smtp.sent if "cycle failed" in str(m["Subject"])]
    assert len(cycle_failed) == 1 and "secret detail" not in str(cycle_failed[0].get_content())
