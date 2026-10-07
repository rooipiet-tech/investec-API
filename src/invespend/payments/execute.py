"""Execution of ONE approved instruction (S11): the only module that reaches the write endpoint.

Called ONLY from the cycle's execution step, with rows that were in the cycle-start snapshot of ``accepted`` rows,
so an item approved in this cycle can never run in it (F45). Order (F15, no gap between the last check and the POST
beyond the call): approval guard, offer digest, live gate, fresh beneficiary list + fingerprint, source account,
balance, caps, the claim CAS, then the POST OUTSIDE any database transaction. Dry-run never POSTs.

Outcome mapping is POSITIVE about "not sent": only ``PaymentNotSent`` (the client's pre-send token fetch failed)
releases the reservation and parks; every other exception after the claim is ``needs_review`` with the reservation
kept and the payment NEVER resent. Every exit from ``submitting`` is ONE ``store.finalize`` call.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from . import accounts as accounts_mod
from . import beneficiaries as bene
from . import fingerprints, instructions, notify_v2
from .caps import check_per_payment
from .mode import v2_settings
from .notices import safe_send, sender_of
from .outcome import PaymentNotSent, PaymentRejected, parse_payment_response
from .refs import request_ref


def reverify_beneficiary(record: dict, beneficiaries: list | None, raw_beneficiaries: list | None, key: str) -> str | None:
    """None when the stored beneficiary is still valid, else a park reason code. Shared with the pre-offer check."""
    if beneficiaries is None or raw_beneficiaries is None:
        return "beneficiary_list_unavailable"
    if not key:
        return "fingerprint_key_missing"
    bid = record.get("beneficiary_id")
    if not bid:
        return "beneficiary_missing"
    if not any(b.beneficiary_id == bid for b in beneficiaries):
        return "beneficiary_removed"
    found, status = bene.resolve_beneficiary_strict(str(record.get("payee_name_norm") or ""), beneficiaries)
    if status == "ambiguous":
        return "beneficiary_ambiguous"
    if found is None or found.beneficiary_id != bid:
        return "beneficiary_changed"
    raw = fingerprints.raw_by_id(raw_beneficiaries).get(str(bid))
    if raw is None:
        return "beneficiary_removed"
    stored = record.get("beneficiary_fingerprint")
    if not stored or fingerprints.beneficiary_fingerprint(raw, key) != stored:
        return "beneficiary_changed"
    return None


def _decimal(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _payee_name(row: dict, beneficiaries: list | None) -> str:
    for b in beneficiaries or ():
        if b.beneficiary_id == row.get("beneficiary_id") and b.name:
            return b.name
    return str(row.get("payee_name_norm") or "")


def _problem(settings, smtp_send, audit, row: dict, kind: str, code: str, provider_message: str | None = None) -> None:
    msg = notify_v2.build_problem_email(sender_of(settings), row["notify_to"], ref=request_ref(row["instruction_id"]),
                                        kind=kind, detail_code=code, provider_message=provider_message)
    safe_send(smtp_send, msg, audit)


def _park_accepted(settings, store, audit, smtp_send, row: dict, reason: str, now: datetime) -> str:
    """accepted -> parked; the problem email goes out ONLY when this call won the CAS."""
    ref = request_ref(row["instruction_id"])
    if store.cas_status(row["instruction_id"], {"accepted"}, "parked", now=now, outcome_code=reason):
        audit.append("parked", {"ref": ref, "reason": reason})
        _problem(settings, smtp_send, audit, row, "parked", reason)
        return f"parked:{reason}"
    audit.append("execute_cas_lost", {"ref": ref, "reason": reason})
    return "cas_lost"


def _finalize(store, audit, row: dict, new_status: str, *, release: bool, now: datetime, code: str | None = None,
              message: str | None = None) -> bool | None:
    """ONE call; if it raises (e.g. LockNotAvailable) the row stays ``submitting`` for the stale sweep."""
    try:
        return store.finalize(row["instruction_id"], "submitting", new_status, release=release, now=now,
                              outcome_code=code, outcome_message=message)
    except Exception as exc:  # noqa: BLE001
        audit.append("finalize_error", {"ref": request_ref(row["instruction_id"]), "reason": type(exc).__name__})
        return None


def execute_instruction(settings, store, audit, client, record: dict, *, mode: str, now: datetime, smtp_send) -> str:
    cfg = v2_settings(settings)
    iid = record.get("instruction_id") if isinstance(record, dict) else None
    row = store.get(iid) if iid else None
    ref = request_ref(iid) if iid else ""

    # (0) approval guard: the STORED row decides, never the dict the caller passed
    if (row is None or row.get("status") != "accepted" or row.get("approved_at") is None
            or row["expires_at"] <= now or record.get("status") != "accepted"):
        audit.append("execute_refused_not_approved", {"ref": ref})
        return "refused_not_approved"
    if (not row.get("batch_ref") or row.get("item_no") is None or not row.get("offer_digest")
            or instructions.offer_digest(row, row["batch_ref"], row["item_no"]) != row["offer_digest"]):
        return _park_accepted(settings, store, audit, smtp_send, row, "approval_content_changed", now)

    # (1) live gate evaluated now; dry-run (the default) never POSTs
    live = mode == "live" and bool(settings.live_enabled())
    effective_mode = "live" if live else "dry-run"

    # (2) fresh beneficiary list and fingerprint
    try:
        raw = client.get_beneficiaries()
        if not isinstance(raw, list):
            raise TypeError("beneficiary list")
        beneficiaries = bene.from_api(raw)
    except Exception:  # noqa: BLE001
        return _park_accepted(settings, store, audit, smtp_send, row, "beneficiary_list_unavailable", now)
    reason = reverify_beneficiary(row, beneficiaries, raw, cfg.fingerprint_key)
    if reason:
        return _park_accepted(settings, store, audit, smtp_send, row, reason, now)

    # (3) the source account is still exactly one account
    try:
        accounts = client.get_accounts()
    except Exception:  # noqa: BLE001
        return _park_accepted(settings, store, audit, smtp_send, row, "accounts_unavailable", now)
    account, status = accounts_mod.resolve_source_unique(accounts, str(row["source_account_last3"]))
    if status != "ok" or account is None or accounts_mod.source_account_id(account) != row["source_account_id"]:
        return _park_accepted(settings, store, audit, smtp_send, row, "source_account_changed", now)

    # (4) balance
    amount = _decimal(row["amount"])
    try:
        balance_data = client.get_balance(row["source_account_id"])
        balance = _decimal(balance_data.get("availableBalance", balance_data.get("currentBalance")))
    except Exception:  # noqa: BLE001
        balance = None
    if balance is None or amount is None:
        return _park_accepted(settings, store, audit, smtp_send, row, "balance_unavailable", now)
    if balance < amount:
        return _park_accepted(settings, store, audit, smtp_send, row, "insufficient_balance", now)

    # (5) per-payment cap (fail closed when unset)
    if not check_per_payment(amount, cfg.per_payment_cap).ok:
        return _park_accepted(settings, store, audit, smtp_send, row, "over_per_payment_cap", now)

    # (6) the claim: daily cap + not expired + CAS to submitting + reservation, one transaction
    try:
        claim = store.claim_for_execution(iid, amount, daily_cap=Decimal(str(cfg.daily_cap)),
                                          execution_mode=effective_mode, now=now)
    except Exception as exc:  # noqa: BLE001 - no POST was made: the row stays accepted (or submitting -> stale sweep)
        audit.append("claim_error", {"ref": ref, "reason": type(exc).__name__})
        return "claim_error"
    if not claim.committed:
        if claim.reason == "daily_cap":
            return _park_accepted(settings, store, audit, smtp_send, row, "daily_cap", now)
        if claim.reason == "stale":
            if store.cas_status(iid, {"accepted"}, "expired", now=now, outcome_code="expired"):
                audit.append("expired", {"ref": ref, "count": 1})
                item = notify_v2.ExpiredItem(row["batch_ref"], row["item_no"], _payee_name(row, beneficiaries), amount,
                                             row["currency"], "approved_not_executed")
                safe_send(smtp_send, notify_v2.build_expiry_email(sender_of(settings), row["notify_to"], items=[item]), audit)
                return "expired"
            return "cas_lost"
        audit.append("claim_cas_lost", {"ref": ref})
        return "cas_lost"

    audit.append("execute", {"ref": ref, "execution_mode": effective_mode})
    payee = _payee_name(row, beneficiaries)
    amount_text = format(amount.quantize(Decimal("0.01")), "f")

    def outcome_email(status: str) -> None:
        msg = notify_v2.build_outcome_email(
            sender_of(settings), row["notify_to"], ref=ref, payee_name=payee, amount=amount, currency=row["currency"],
            source_last3=row["source_account_last3"], outcome=SimpleNamespace(status=status, message=""))
        safe_send(smtp_send, msg, audit)

    # (7) dry-run: nothing is sent
    if not live:
        if _finalize(store, audit, row, "executed", release=False, now=now, code="dry-run"):
            audit.append("executed", {"ref": ref, "execution_mode": "dry-run", "result": "dry-run"})
            outcome_email("dry_run")
        return "executed:dry-run"

    # (7) live: the client fetches a FRESH token itself; the POST is outside any DB transaction
    audit.append("live_execute", {"ref": ref, "execution_mode": "live"})
    try:
        body = client.create_payment(row["source_account_id"], row["beneficiary_id"], amount_text,
                                     row.get("their_reference") or "", row.get("my_reference") or "", fresh_token=True)
        outcome = parse_payment_response(body)
    except PaymentNotSent:
        if _finalize(store, audit, row, "parked", release=True, now=now, code="token_fetch_failed"):
            audit.append("parked", {"ref": ref, "reason": "token_fetch_failed"})
            _problem(settings, smtp_send, audit, row, "parked", "token_fetch_failed")
        return "parked:token_fetch_failed"
    except PaymentRejected as exc:
        return _failed(settings, store, audit, smtp_send, row, now, getattr(exc, "message", ""), "rejected")
    except Exception as exc:  # noqa: BLE001 - unknown outcome: money may have moved, never resent
        return _needs_review(settings, store, audit, smtp_send, row, now, type(exc).__name__)

    if outcome.status == "success":
        if _finalize(store, audit, row, "executed", release=False, now=now, code="live"):
            audit.append("executed", {"ref": ref, "execution_mode": "live", "result": "success"})
            outcome_email("success")
        return "executed:live"
    if outcome.status == "failed":
        return _failed(settings, store, audit, smtp_send, row, now, outcome.message, outcome.reason)
    if outcome.status == "needs_authorisation":
        if _finalize(store, audit, row, "needs_authorisation", release=True, now=now, code="authorisation_required",
                     message=outcome.message):
            audit.append("needs_authorisation", {"ref": ref})
            _problem(settings, smtp_send, audit, row, "needs_authorisation", "authorisation_required")
        return "needs_authorisation"
    return _needs_review(settings, store, audit, smtp_send, row, now, outcome.reason or "unknown")


def _failed(settings, store, audit, smtp_send, row, now, message, code) -> str:
    safe_code = code if isinstance(code, str) and code else "rejected"
    if _finalize(store, audit, row, "failed", release=True, now=now, code=safe_code[:40], message=message):
        audit.append("failed", {"ref": request_ref(row["instruction_id"])})
        _problem(settings, smtp_send, audit, row, "failed", "rejected", provider_message=message)
    return "failed"


def _needs_review(settings, store, audit, smtp_send, row, now, code) -> str:
    safe = "".join(ch for ch in str(code).lower() if ch.isalnum() or ch == "_")[:40] or "unknown"
    if _finalize(store, audit, row, "needs_review", release=False, now=now, code=safe):
        audit.append("needs_review", {"ref": request_ref(row["instruction_id"]), "reason": safe})
        _problem(settings, smtp_send, audit, row, "needs_review", safe)
    return "needs_review"
