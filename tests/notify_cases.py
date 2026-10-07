"""Render every v2 builder with chosen third-party values (used by the S10 tests)."""
from __future__ import annotations

import importlib
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

SENDER = "bot@example.com"
TO = "piet@example.com"
REF = "a1b2c3d4e5f6"
BATCH = "B-1007-ab12"
EXPIRES = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)      # 12:00 SAST


def nv2():
    return importlib.import_module("invespend.payments.notify_v2")


def render_all(payee="Acme", reference="INV7", last3="123", account="1234567890", amount="100.00"):
    """name -> EmailMessage for EVERY builder (problem kinds expanded)."""
    n = nv2()
    from invespend.payments.bankdetails import BankDetails

    amt = Decimal(amount)
    items = [
        n.BatchItem(1, payee, amt, "ZAR", last3, reference, "typed"),
        n.BatchItem(2, payee, amt, "ZAR", last3, reference, "attachment"),
        n.BatchItem(3, payee, amt, "ZAR", last3, reference, "image"),
    ]
    pending = [n.PendingItem("B-1006-ffff", 2, payee, amt, "ZAR", EXPIRES)]
    expired = [
        n.ExpiredItem("B-1006-ffff", 1, payee, amt, "ZAR", "unapproved"),
        n.ExpiredItem("B-1006-ffff", 2, payee, amt, "ZAR", "approved_not_executed"),
        n.ExpiredItem(None, None, payee, amt, "ZAR", "awaiting_beneficiary"),
        n.ExpiredItem(None, None, payee, amt, "ZAR", "held"),
    ]
    out = {
        "paste": n.build_paste_details_email(
            SENDER, TO, ref=REF, details=BankDetails(payee, "FNB", account, "250655", reference),
            amount=amt, their_reference=reference, my_reference=reference,
            payment_notification="notify@example.invalid", beneficiary_email="bene@example.invalid",
            beneficiary_mobile="0820000000"),
        "batch": n.build_batch_approval_email(SENDER, TO, batch_ref=BATCH, items=items, pending=pending, expires_at=EXPIRES),
        "outcome_success": n.build_outcome_email(SENDER, TO, ref=REF, payee_name=payee, amount=amt, currency="ZAR",
                                                 source_last3=last3, outcome=SimpleNamespace(status="success", message="")),
        "outcome_dry_run": n.build_outcome_email(SENDER, TO, ref=REF, payee_name=payee, amount=amt, currency="ZAR",
                                                 source_last3=last3, outcome=SimpleNamespace(status="dry_run", message="")),
        "outcome_unknown": n.build_outcome_email(SENDER, TO, ref=REF, payee_name=payee, amount=amt, currency="ZAR",
                                                 source_last3=last3, outcome=SimpleNamespace(status="unknown", message="")),
        "beneficiary_observed": n.build_beneficiary_observed_email(
            SENDER, TO, ref=REF, payee_name=payee, amount=amt, currency="ZAR", source_last3=last3,
            hold_hours=0, eligible_at=EXPIRES),
        "ack": n.build_command_ack_email(SENDER, TO, batch_ref=BATCH, approved=[1, 3], cancelled=[2],
                                         skipped=[(4, "expired"), (5, "already_executed"), (6, "already_cancelled")]),
        "expiry": n.build_expiry_email(SENDER, TO, items=expired),
        "not_understood": n.build_not_understood_email(SENDER, TO, batch_ref=BATCH, reason_code="out_of_range",
                                                       valid_item_nos=[1, 2, 3]),
        "batch_not_matched": n.build_batch_not_matched_email(SENDER, TO),
    }
    for kind in ("failed", "needs_review", "parked", "needs_authorisation", "resend", "cycle_failed"):
        out[f"problem_{kind}"] = n.build_problem_email(
            SENDER, TO, ref=REF, kind=kind, detail_code="amount_conflict",
            provider_message="Insufficient funds" if kind == "failed" else None)
    return out


def body(msg) -> str:
    return msg.get_content()
