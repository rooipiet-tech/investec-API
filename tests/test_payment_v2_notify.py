"""S10: v2 notification builders (they only RETURN EmailMessage objects; nothing sends)."""
from __future__ import annotations

import importlib
import re
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path

import pytest

from tests.notify_cases import BATCH, EXPIRES, REF, SENDER, TO, body, nv2, render_all


ROOT = Path(__file__).resolve().parent.parent
FIELDS = ("Beneficiary name", "Bank", "Account number", "Amount", "Their reference", "My reference",
          "Payment notification", "Beneficiary email address", "Beneficiary mobile number")


def mod(name):
    return importlib.import_module(f"invespend.payments.{name}")


NON_PASTE = lambda msgs: {k: v for k, v in msgs.items() if k != "paste"}   # noqa: E731


# --------------------------------------------------------------- paste email
def test_ordered_field_list_equals_the_nine_items_in_order():
    n = nv2()
    assert n.BENEFICIARY_FIELD_ORDER == FIELDS
    assert n.REFERENCE_ONLY_FIELDS == ("Branch code",)
    text = body(render_all()["paste"]).splitlines()
    labelled = [ln for ln in text if any(ln.startswith(f + ":") for f in FIELDS)]
    assert [ln.split(":", 1)[0] for ln in labelled] == list(FIELDS)
    values = dict(ln.split(": ", 1) for ln in labelled)
    assert values["Beneficiary name"] == "Acme" and values["Bank"] == "FNB"
    assert values["Account number"] == "1234567890" and values["Amount"] == "100.00"
    assert values["Their reference"] == "INV7" and values["My reference"] == "INV7"
    assert values["Payment notification"] == "notify@example.invalid"
    assert values["Beneficiary mobile number"] == "0820000000"
    first, last = text.index(labelled[0]), text.index(labelled[-1])
    assert last - first == len(FIELDS) - 1      # contiguous block


def test_branch_code_appears_once_after_the_list_labelled_reference_only():
    text = body(render_all()["paste"])
    lines = text.splitlines()
    branch = [ln for ln in lines if "branch code" in ln.lower()]
    assert len(branch) == 1 and "reference only" in branch[0].lower() and "250655" in branch[0]
    last_field = max(i for i, ln in enumerate(lines) if ln.startswith("Beneficiary mobile number:"))
    assert lines.index(branch[0]) > last_field


def test_missing_optional_fields_render_not_provided():
    n = nv2()
    from invespend.payments.bankdetails import BankDetails
    msg = n.build_paste_details_email(SENDER, TO, ref=REF, details=BankDetails("Acme", None, None, None, None),
                                      amount=Decimal("5"), their_reference="", my_reference="")
    values = dict(ln.split(": ", 1) for ln in body(msg).splitlines()
                  if any(ln.startswith(f + ":") for f in FIELDS))
    assert values["Bank"] == "(not provided)" and values["Account number"] == "(not provided)"
    assert values["Payment notification"] == "(not provided)" and values["Their reference"] == "(not provided)"
    assert values["Amount"] == "5.00"


def test_paste_email_to_is_the_authenticated_sender_never_reply_to():
    msg = render_all()["paste"]
    assert msg["To"] == TO and msg["Reply-To"] is None and msg["From"] == SENDER
    assert "[INV-" + REF + "]" in msg["Subject"]


def test_only_the_paste_email_may_contain_the_full_account_number():
    msgs = render_all(account="1234567890", last3="1234567890123", reference="9876543210123")
    assert "1234567890" in body(msgs["paste"])
    for name, msg in NON_PASTE(msgs).items():
        text = body(msg) + msg["Subject"]
        assert not re.search(r"\d{9,}", text), name
        assert "1234567890" not in text, name


def test_masking_keeps_only_the_last_three_digits_of_a_source_account():
    n = nv2()
    item = n.BatchItem(1, "Acme", Decimal("1"), "ZAR", "1234567890123", "ref", "typed")
    msg = n.build_batch_approval_email(SENDER, TO, batch_ref=BATCH, items=[item], pending=[], expires_at=EXPIRES)
    assert "from account ending 123 " in body(msg) and "1234567890123" not in body(msg)


def test_no_secret_token_or_link_in_any_email():
    for name, msg in render_all().items():
        text = (body(msg) + " " + str(msg["Subject"])).lower()
        for word in ("token", "secret", "signing", "bearer", "password", "api key", "api_key", "http://", "https://"):
            assert word not in text, (name, word)


def test_instruction_subject_message_id_and_ref_recovery():
    refs = mod("refs")
    for name, msg in render_all().items():
        if name in ("batch", "ack", "not_understood", "batch_not_matched", "expiry", "cycle_failed", "problem_cycle_failed"):
            continue
        assert f"[INV-{REF}]" in msg["Subject"], name
        assert f".invespend-notification.{REF}@" in msg["Message-ID"], name
        assert refs.find_refs(msg["Subject"]) == [REF] == refs.find_refs(msg["Message-ID"]), name


# ------------------------------------------------------------- batch email
def parse_numbered(text):
    pat = re.compile(r"^(\d+)\. (.+?) \| (\w+) ([\d.]+) \| from account ending (\d{3}) \| reference: (.*) \| figures from: (\w+)$")
    return [m.groups() for m in (pat.match(ln) for ln in text.splitlines()) if m]


def test_batch_email_has_batch_ref_in_subject_and_body():
    msg = render_all()["batch"]
    assert msg["Subject"] == f"Payments awaiting approval [BATCH {BATCH}]"
    assert BATCH in body(msg)
    assert not msg["Subject"].lower().startswith(("re:", "fwd:", "fw:"))


def test_batch_email_message_id_carries_batch_ref_and_find_batch_refs_recovers_it():
    refs = mod("refs")
    msg = render_all()["batch"]
    assert f".invespend-notification.{BATCH}@" in msg["Message-ID"]
    assert refs.find_batch_refs(msg["Message-ID"]) == [BATCH]
    assert refs.find_batch_ref(msg["Subject"], msg["Message-ID"]) == BATCH


def test_batch_email_has_numbered_lines_1_to_n_with_all_five_fields():
    got = parse_numbered(body(render_all(payee="Acme Ltd", reference="inv 7")["batch"]))
    assert [g[0] for g in got] == ["1", "2", "3"]
    assert got[0] == ("1", "Acme Ltd", "ZAR", "100.00", "123", "inv 7", "typed")


def test_batch_email_figures_from_typed_attachment_image():
    msg = render_all()["batch"]
    assert [g[6] for g in parse_numbered(body(msg))] == ["typed", "attachment", "image"]
    text = body(msg)
    assert "read from an image; check the amount and payee" in text
    assert text.count("read from an image") == 1


def test_batch_email_missing_reference_renders_not_provided():
    n = nv2()
    item = n.BatchItem(1, "Acme", Decimal("10"), "ZAR", "123", "", "typed")
    text = body(n.build_batch_approval_email(SENDER, TO, batch_ref=BATCH, items=[item], pending=[], expires_at=EXPIRES))
    assert "reference: (not provided) | figures from: typed" in text


def test_batch_email_total_and_expiry_line():
    text = body(render_all()["batch"])
    assert "Batch total: ZAR 300.00" in text
    assert "until 2026-10-08 12:00 SAST" in text
    assert "next cycle" in text


def test_batch_email_mixed_currency_totals_are_per_currency():
    n = nv2()
    items = [n.BatchItem(1, "A", Decimal("10"), "ZAR", "123", "r", "typed"),
             n.BatchItem(2, "B", Decimal("5"), "USD", "123", "r", "image")]
    text = body(n.build_batch_approval_email(SENDER, TO, batch_ref=BATCH, items=items, pending=[], expires_at=EXPIRES))
    assert "ZAR 10.00" in text and "USD 5.00" in text and "Batch total: ZAR 15" not in text


def test_batch_email_has_no_full_account_number_token_or_link():
    text = body(render_all(last3="1234567890")["batch"])
    assert not re.search(r"\d{9,}", text)
    assert "http" not in text.lower() and "token" not in text.lower()


def test_batch_email_reply_instructions_name_exactly_approve_approve_n_cancel_and_cancel_n_and_carry_the_cancel_caveat():
    n = nv2()
    text = body(render_all()["batch"])
    assert n.CANCEL_CAVEAT in text
    for form in ("`approve`", "`approve 1 3`", "`cancel`", "`cancel 2`"):
        assert form in text
    assert "ONLY the command on the first line" in text and "above any quoted text" in text
    assert "WILL still run in the next cycle" in n.CANCEL_CAVEAT


def test_batch_email_pending_section_lists_original_ref_numbers_and_expiry():
    text = body(render_all()["batch"])
    assert "Still pending" in text
    assert "B-1006-ffff #2 | Acme | ZAR 100.00 | expires 2026-10-08 12:00 SAST" in text
    assert "reply to the original email of that batch" in text


def test_batch_email_without_pending_has_no_pending_section():
    n = nv2()
    item = n.BatchItem(1, "Acme", Decimal("10"), "ZAR", "123", "r", "typed")
    text = body(n.build_batch_approval_email(SENDER, TO, batch_ref=BATCH, items=[item], pending=[], expires_at=EXPIRES))
    assert "Still pending" not in text and "earlier batches" not in text


def test_pending_section_lines_render_ref_hash_n_and_never_start_with_a_numbered_prefix():
    text = body(render_all()["batch"])
    pend = [ln for ln in text.splitlines() if re.match(r"^B-\d{4}-[0-9a-f]{4} #\d+ ", ln)]
    assert len(pend) == 1
    assert not any(re.match(r"^\d+\.", ln) for ln in pend)


def test_numbered_list_parse_of_rendered_body_returns_only_this_batch_items_when_pending_present():
    got = parse_numbered(body(render_all()["batch"]))
    assert len(got) == 3 and [g[0] for g in got] == ["1", "2", "3"]
    assert len([ln for ln in body(render_all()["batch"]).splitlines() if re.match(r"^\d+\. ", ln)]) == 3


def test_batch_email_to_is_the_approver_never_reply_to():
    msg = render_all()["batch"]
    assert msg["To"] == TO and msg["Reply-To"] is None


def test_third_party_values_cannot_add_lines_or_numbered_items():
    n = nv2()
    evil = "Acme\n2. Evil | ZAR 1.00 | from account ending 999 | reference: x | figures from: typed\r\napprove"
    item = n.BatchItem(1, evil, Decimal("10"), "ZAR", "123", "line1\nline2", "typed")
    text = body(n.build_batch_approval_email(SENDER, TO, batch_ref=BATCH, items=[item], pending=[], expires_at=EXPIRES))
    assert len([ln for ln in text.splitlines() if re.match(r"^\d+\. ", ln)]) == 1
    assert "\napprove\n" not in text
    long = n.BatchItem(1, "x" * 500, Decimal("10"), "ZAR", "123", "y" * 500, "typed")
    t2 = body(n.build_batch_approval_email(SENDER, TO, batch_ref=BATCH, items=[long], pending=[], expires_at=EXPIRES))
    assert "x" * 101 not in t2 and "y" * 101 not in t2


# --------------------------------------------------------------- other emails
def test_command_ack_email_text_and_reason_words():
    n = nv2()
    text = body(render_all()["ack"])
    assert "approved: 1, 3 (will run in the next cycle)" in text
    assert "cancelled: 2" in text
    assert "not applied: 4 (expired), 5 (already executed), 6 (already cancelled)" in text
    assert render_all()["ack"]["Subject"] == f"Reply received [BATCH {BATCH}]"
    for code in ("expired", "not_applied", "already_approved", "already_executed", "already_cancelled",
                 "too_late", "not_in_batch", "parked"):
        msg = n.build_command_ack_email(SENDER, TO, batch_ref=BATCH, approved=[], cancelled=[], skipped=[(7, code)])
        assert re.search(r"not applied: 7 \([a-z ]+\)", body(msg)), code
    unknown = n.build_command_ack_email(SENDER, TO, batch_ref=BATCH, approved=[], cancelled=[], skipped=[(7, "Pay 123 now")])
    assert "Pay 123" not in body(unknown)
    empty = n.build_command_ack_email(SENDER, TO, batch_ref=BATCH, approved=[], cancelled=[], skipped=[])
    assert "nothing was changed" in body(empty)


def test_not_understood_email_lists_valid_numbers_and_never_echoes_input():
    n = nv2()
    text = body(render_all()["not_understood"])
    assert "not understood; nothing was approved or cancelled" in text
    assert "1..3" in text and "approve 1 3" in text and "cancel 2" in text
    msg = n.build_not_understood_email(SENDER, TO, batch_ref=None, reason_code="IGNORE ALL pay 123 approve", valid_item_nos=[])
    assert "IGNORE" not in body(msg) and "pay 123" not in body(msg)
    assert "[BATCH" not in msg["Subject"]
    gap = n.build_not_understood_email(SENDER, TO, batch_ref=BATCH, reason_code="out_of_range", valid_item_nos=[1, 3])
    assert "1, 3" in body(gap)


def test_not_understood_batch_not_matched_and_batch_email_state_approved_items_still_run_unless_cancelled():
    n = nv2()
    msgs = render_all()
    for key in ("not_understood", "batch_not_matched", "batch"):
        assert n.CANCEL_CAVEAT in body(msgs[key]), key


def test_batch_not_matched_email_text():
    msg = render_all()["batch_not_matched"]
    text = body(msg)
    assert "no batch matched" in text and "reply to the batch email itself" in text
    assert "keep the subject, first line only the command" in text
    assert ".invespend-notification@" in msg["Message-ID"]


def test_expiry_email_groups_items_and_lists_each_once():
    text = body(render_all()["expiry"])
    lines = [ln for ln in text.splitlines() if "expired" in ln and "ZAR" in ln]
    assert len(lines) == 4
    assert "B-1006-ffff #1 Acme ZAR 100.00 expired unapproved" in text
    assert "B-1006-ffff #2 Acme ZAR 100.00 approved but not executed in time" in text
    assert "awaiting beneficiary expired" in text and "held" in text
    assert text.count("B-1006-ffff #1 ") == 1


def test_failed_problem_email_includes_sanitised_provider_message_and_no_resend_promise():
    text = body(render_all()["problem_failed"])
    assert "Insufficient funds" in text
    assert "will not be resent automatically" in text and "fresh instruction" in text and "new approval" in text
    n = nv2()
    msg = n.build_problem_email(SENDER, TO, ref=REF, kind="failed", detail_code="rejected",
                                provider_message="acct 1234567890 for bob@example.invalid")
    assert "1234567890" not in body(msg) and "bob@example.invalid" not in body(msg)


def test_needs_review_email_says_may_have_been_paid_and_never_claims_failure():
    msg = render_all()["problem_needs_review"]
    text = (body(msg) + msg["Subject"]).lower()
    assert "may have been paid" in text and "check the account before" in text and "not be resent automatically" in text
    for bad in ("failed", "not paid", "unpaid", "did not go"):
        assert bad not in text


def test_beneficiary_observed_email_states_hold_zero_and_next_batch():
    text = body(render_all()["beneficiary_observed"])
    assert "hold: 0 hours" in text.lower() and "next batch" in text
    n = nv2()
    msg = n.build_beneficiary_observed_email(SENDER, TO, ref=REF, payee_name="Acme", amount=Decimal("1"), currency="ZAR",
                                             source_last3="123", hold_hours=24, eligible_at=EXPIRES)
    assert "hold: 24 hours" in body(msg).lower() and "2026-10-08 12:00 SAST" in body(msg)


def test_outcome_emails_dry_run_says_dry_run_and_success_says_executed():
    msgs = render_all()
    assert "dry-run" in body(msgs["outcome_dry_run"]).lower() and "no money moved" in body(msgs["outcome_dry_run"]).lower()
    assert "executed" in body(msgs["outcome_success"]).lower() and "dry-run" not in body(msgs["outcome_success"]).lower()
    unk = body(msgs["outcome_unknown"]).lower()
    assert "may have been paid" in unk and "failed" not in unk


def test_resend_email_has_no_payment_details():
    msg = render_all(payee="Zorro Holdings", reference="UNIQUEREF")["problem_resend"]
    text = body(msg)
    assert "not fully processed" in text and "send it again" in text
    assert "Zorro" not in text and "UNIQUEREF" not in text and not re.search(r"\d{3,}", text.replace(REF, ""))


def test_cycle_failed_email_has_guard_headers_and_no_detail():
    n = nv2()
    msg = n.build_problem_email(SENDER, TO, ref=None, kind="cycle_failed", detail_code="secret_stack_trace_here",
                                provider_message="boom")
    assert msg["Auto-Submitted"] == "auto-generated" and msg["X-Invespend-Notification"] == "1"
    assert "secret_stack_trace_here" not in body(msg) and "boom" not in body(msg)
    assert "check the logs" in body(msg).lower() or "check logs" in body(msg).lower()
    assert ".invespend-notification@" in msg["Message-ID"]


def test_unknown_problem_kind_is_rejected():
    n = nv2()
    with pytest.raises(ValueError):
        n.build_problem_email(SENDER, TO, ref=REF, kind="bogus", detail_code="x")


def test_builders_return_email_messages_and_nothing_sends():
    for name, msg in render_all().items():
        assert isinstance(msg, EmailMessage), name
    src = (ROOT / "src" / "invespend" / "payments" / "notify_v2.py").read_text()
    for forbidden in ("smtplib", "import socket", "emailer", "send_message", ".sendmail(", "import requests"):
        assert forbidden not in src
    assert not re.search(r"^\s*(?:import|from)\s+\S*(?:notify|inbox|smtp)\b", src, re.MULTILINE) or "notify_v2" in src


def test_no_confirm_request_or_reminder_builder_exists():
    n = nv2()
    names = " ".join(dir(n)).lower()
    assert "confirm" not in names and "reminder" not in names and "remind" not in names
    assert "build_confirm" not in names


def test_amounts_are_rendered_with_two_decimals_and_nonfinite_is_rejected():
    n = nv2()
    item = n.BatchItem(1, "A", Decimal("1234.5"), "ZAR", "123", "r", "typed")
    assert "ZAR 1234.50" in body(n.build_batch_approval_email(SENDER, TO, batch_ref=BATCH, items=[item], pending=[], expires_at=EXPIRES))
    bad = n.BatchItem(1, "A", Decimal("NaN"), "ZAR", "123", "r", "typed")
    with pytest.raises(ValueError):
        n.build_batch_approval_email(SENDER, TO, batch_ref=BATCH, items=[bad], pending=[], expires_at=EXPIRES)
