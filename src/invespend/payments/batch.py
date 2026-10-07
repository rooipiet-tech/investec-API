"""The batch offer step (S11, F43/F44): numbers ready items, assigns ONE ref per approver and emails ONE batch.

For every approver (``notify_to``) with UNOFFERED ``awaiting_approval`` rows: offering is deferred when the
beneficiary list could not be fetched; each row is re-verified (beneficiary, fingerprint key, per-payment cap) and
parked when it fails; then ``store.offer_batch`` (one transaction) numbers the rest and a single email goes out. An
empty offer sends NOTHING. The email carries a "still pending" section for earlier batches (never re-offered).

Send failures (R7-T4): ONLY a positive PRE-DATA failure (the message provably never reached DATA) calls
``unoffer_batch``; any other error leaves the batch offered with ``batch_notified_at`` null and the unnotified-batch
resend delivers the SAME ref and numbers, so the user can never hold a copy whose ref the store has discarded.
"""
from __future__ import annotations

import smtplib
import socket
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from . import beneficiaries as bene
from . import execute, notify_v2
from .caps import check_per_payment
from .mode import v2_settings
from .notices import safe_send, sender_of
from .refs import request_ref

PRE_DATA_SMTP_ERRORS = (
    smtplib.SMTPConnectError, smtplib.SMTPHeloError, smtplib.SMTPAuthenticationError, smtplib.SMTPSenderRefused,
    smtplib.SMTPRecipientsRefused, ConnectionRefusedError, socket.gaierror,
)


def _payee_name(row: dict, beneficiaries: list | None) -> str:
    for b in beneficiaries or ():
        if b.beneficiary_id == row.get("beneficiary_id") and b.name:
            return b.name
    return str(row.get("payee_name_norm") or "")


def _email(settings, store, to: str, ref: str, rows: list[dict], beneficiaries, now: datetime):
    items = [
        notify_v2.BatchItem(r["item_no"], _payee_name(r, beneficiaries), Decimal(str(r["amount"])), r["currency"],
                            r["source_account_last3"], r.get("their_reference") or "", r["figures_source"])
        for r in sorted(rows, key=lambda r: r["item_no"])
    ]
    pending = [
        notify_v2.PendingItem(p["batch_ref"], p["item_no"], _payee_name(p, beneficiaries), Decimal(str(p["amount"])),
                              p["currency"], p["expires_at"])
        for p in store.list_pending_offered(to, now=now, exclude_batch_ref=ref)
    ]
    expires_at = min(r["expires_at"] for r in rows)
    return notify_v2.build_batch_approval_email(sender_of(settings), to, batch_ref=ref, items=items, pending=pending,
                                                expires_at=expires_at)


def _send_batch(settings, store, audit, smtp_send, to: str, ref: str, rows: list[dict], beneficiaries, now: datetime,
                *, can_unoffer: bool) -> bool:
    msg = _email(settings, store, to, ref, rows, beneficiaries, now)
    try:
        smtp_send(msg)
    except PRE_DATA_SMTP_ERRORS as exc:
        if can_unoffer:
            store.unoffer_batch(ref, now=now)
        audit.append("batch_send_failed", {"ref": ref, "reason": type(exc).__name__})
        return False
    except Exception as exc:  # noqa: BLE001 - the server MAY have accepted the message: keep the batch offered
        audit.append("batch_send_unknown", {"ref": ref, "reason": type(exc).__name__})
        return False
    store.mark_batch_notified(ref, now)
    audit.append("batch_offered", {"ref": ref, "count": len(rows)})
    return True


def offer_batches(settings, store, audit, *, beneficiaries, raw_beneficiaries, now: datetime, smtp_send,
                  new_ref: Callable[[], str]) -> dict:
    cfg = v2_settings(settings)
    summary = {"batches_sent": 0, "items_offered": 0, "pre_offer_parked": 0, "deferred": 0}
    unoffered = [r for r in store.list_by_status({"awaiting_approval"}) if r["batch_ref"] is None and r["expires_at"] > now]
    if not unoffered:
        return summary
    if beneficiaries is None:
        audit.append("offer_deferred_list_unavailable", {"count": len(unoffered)})
        summary["deferred"] = len(unoffered)
        return summary
    for to in sorted({r["notify_to"] for r in unoffered}):
        for row in [r for r in unoffered if r["notify_to"] == to]:
            reason = execute.reverify_beneficiary(row, beneficiaries, raw_beneficiaries, cfg.fingerprint_key)
            if reason is None and not check_per_payment(row["amount"], cfg.per_payment_cap).ok:
                reason = "over_per_payment_cap"
            if reason is None:
                continue
            if store.cas_status(row["instruction_id"], {"awaiting_approval"}, "parked", now=now, outcome_code=reason):
                summary["pre_offer_parked"] += 1
                audit.append("parked", {"ref": request_ref(row["instruction_id"]), "reason": reason})
                safe_send(smtp_send, notify_v2.build_problem_email(
                    sender_of(settings), row["notify_to"], ref=request_ref(row["instruction_id"]), kind="parked",
                    detail_code=reason), audit)
        rows = store.offer_batch(to, now=now, approval_window=cfg.approval_window, max_items=cfg.max_batch_items,
                                 new_ref=new_ref)
        if not rows:
            continue
        ref = rows[0]["batch_ref"]
        summary["items_offered"] += len(rows)
        if _send_batch(settings, store, audit, smtp_send, to, ref, rows, beneficiaries, now, can_unoffer=True):
            summary["batches_sent"] += 1
    return summary


def resend_unnotified(settings, store, audit, *, beneficiaries, now: datetime, smtp_send) -> int:
    """Notice sweep: a batch whose ``batch_notified_at`` is still null after the stuck window (the crash window
    between ``offer_batch`` and the send) is emailed again with the SAME ref and numbers; no state changes."""
    cfg = v2_settings(settings)
    sent = 0
    for ref in store.list_unnotified_batches(older_than=cfg.stuck, now=now):
        rows = [r for r in store.list_by_status({"awaiting_approval"}) if r["batch_ref"] == ref]
        if not rows:
            continue
        to = rows[0]["notify_to"]
        if _send_batch(settings, store, audit, smtp_send, to, ref, [r for r in rows if r["notify_to"] == to], beneficiaries,
                       now, can_unoffer=False):
            sent += 1
    return sent
