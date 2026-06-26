"""Approval-cycle orchestration (F1-F12, F19, F20).

``run_approval_cycle`` wires every helper, fully injectable so offline tests can
mock the inbox, SMTP, and Investec client. The security invariants it enforces:

  * F1: inbound From/subject/body/keywords NEVER authorize — only a valid bound
    HMAC token (verified against the trusted pending record) executes.
  * F3: last-3 selects the source account (exact match vs real get_accounts;
    0/>1 -> fail-closed) and is NEVER an auth factor.
  * F4: pays only an exact allowlisted beneficiary_id.
  * F5/F11: needs_review/skipped extractions never execute.
  * F6/F7 (PR3): per-payment + persisted daily-aggregate caps re-checked and
    committed atomically pre-execute; deterministic dedup prevents double-exec.
  * F8 (PR1): DRY-RUN default; live only when live_enabled() (flag AND cred),
    audited; in dry-run NO write endpoint is called.
  * F9/F20 (PR2): every step audited via minimal-field allowlist; secrets/PANs
    never recorded.
  * F12: progress email only when an explicit sender contact is present.

Returns a machine-readable dict (F16).
"""
from __future__ import annotations

from . import accounts as acct
from . import beneficiaries as bene
from . import caps as caps_mod
from . import dedup as dedup_mod
from . import extract as extract_mod
from . import notify
from . import token as token_mod
from .audit import AuditLog
from .store import PaymentStore


def _extract_candidate(message, expected_currency: str):
    """Prefer attachment extraction; fall back to body. Returns ExtractResult."""
    for filename, data in getattr(message, "attachments", []) or []:
        result = extract_mod.extract_attachment(filename, data, expected_currency)
        if result.status in ("ok", "needs_review"):
            return result
        # status == "skipped" (image): keep looking; an image alone fails closed.
    body = getattr(message, "body", "") or ""
    token = getattr(message, "token", None)
    if token:
        # The approval token carries long digit runs (expiry + 64-hex MAC); strip
        # it so it is never mis-parsed as a second amount (ambiguity, F5).
        body = body.replace(token, " ")
    if body.strip():
        return extract_mod.extract_from_text(body, expected_currency)
    return extract_mod.ExtractResult("needs_review", reason="no extractable content")


def run_approval_cycle(
    settings,
    *,
    inbox,
    client,
    smtp_send=None,
    allowlist=None,
    expected_currency: str = "ZAR",
    sender_contact: str | None = None,
    approval_recipients=None,
    now=None,
):
    """Run one cycle. ``client`` exposes ``get_accounts`` and (live only)
    ``create_payment``. ``smtp_send`` overrides the SMTP transport for tests."""
    store = PaymentStore(settings.state_dir)
    audit = AuditLog(settings.state_dir)
    live = settings.live_enabled()
    audit.append("cycle_start", {"execution_mode": "live" if live else "dry-run",
                                 "live_enabled": live})

    # Allowlist source: explicit param wins; else API (when enabled) else static
    # code config. A failed/empty API fetch -> empty allowlist -> everything fails
    # closed (no pay) — the fetch never raises past this guard.
    beneficiary_source = "static"
    if allowlist is None and getattr(settings, "payments_beneficiaries_from_api", False):
        beneficiary_source = "api"
        try:
            allowlist = bene.from_api(client.get_beneficiaries())
        except Exception:  # noqa: BLE001 — fail closed, never bypass safety
            allowlist = []
        audit.append("beneficiaries_loaded",
                     {"source": "api", "count": len(allowlist)})

    real_accounts = client.get_accounts()
    messages = inbox.fetch_messages()

    summary = {
        "execution_mode": "live" if live else "dry-run",
        "live_enabled": live,
        "beneficiary_source": beneficiary_source,
        "processed": 0,
        "pending": 0,
        "executed": 0,
        "parked": 0,
        "skipped": 0,
        "results": [],
    }

    for message in messages:
        summary["processed"] += 1
        outcome = _process_message(
            message, settings, store, audit, client, real_accounts,
            allowlist, expected_currency, live, now,
        )
        summary[outcome["bucket"]] += 1
        summary["results"].append({"dedup_key": outcome.get("dedup_key"),
                                   "result": outcome["result"]})

    # ── consolidated approval email: all still-pending payments (F19) ─────────
    pendings = store.list_records(status="pending")
    if pendings and approval_recipients:
        email_items = _approval_items(settings, pendings, allowlist, real_accounts)
        if email_items:
            msg = notify.build_approval_email(
                settings.report_sender, list(approval_recipients), email_items
            )
            (smtp_send or _default_smtp)(settings, msg)
            audit.append("approval_email_sent", {"count": len(email_items)})

    # ── progress email: ONLY when an explicit sender contact is present (F12) ──
    if sender_contact and str(sender_contact).strip():
        pmsg = notify.build_progress_email(
            settings.report_sender, sender_contact,
            summary["executed"] + summary["pending"], summary["parked"],
        )
        (smtp_send or _default_smtp)(settings, pmsg)
        audit.append("progress_email_sent", {"count": summary["processed"]})

    # ── retention cleanup (F20) ───────────────────────────────────────────────
    removed = store.cleanup(settings.retention_days)
    if removed:
        audit.append("cleanup", {"count": removed})

    audit.append("cycle_end", {"result": "ok"})
    return summary


def _default_smtp(settings, msg):
    from ..emailer import _smtp_send

    _smtp_send(settings, msg)


def _process_message(
    message, settings, store, audit, client, real_accounts,
    allowlist, expected_currency, live, now,
):
    audit.append("message_received", {"message_id_hash": _hash(message.message_id)})

    # 1. Source account selection from last-3 (weak selector; 0/>1 -> fail-closed).
    source = acct.resolve_source_account(real_accounts, message.last3 or "")
    if source is None:
        audit.append("source_unresolved", {"reason": "last3 0 or >1 match"})
        return {"bucket": "parked", "result": "source_unresolved"}
    source_id = acct.source_account_id(source)
    source_last3 = acct.source_account_last3(source)

    # 2. Extract a candidate (fail-closed on ambiguity / image-only).
    result = _extract_candidate(message, expected_currency)
    if result.status == "skipped":
        audit.append("extract_skipped", {"reason": result.reason})
        return {"bucket": "skipped", "result": "skipped:" + result.reason}
    if result.status != "ok":
        audit.append("extract_needs_review", {"reason": result.reason})
        return {"bucket": "parked", "result": "needs_review:" + result.reason}

    # 3. Resolve beneficiary by allowlist (exact; 0/>1 -> fail-closed, F4).
    beneficiary = bene.resolve_beneficiary(result.payee or "", allowlist)
    if beneficiary is None:
        audit.append("beneficiary_unresolved", {"reason": "no exact allowlist match"})
        return {"bucket": "parked", "result": "beneficiary_unresolved"}

    # 4. Per-payment cap (F6).
    per = caps_mod.check_per_payment(result.amount, settings.per_payment_cap)
    if not per.ok:
        audit.append("cap_blocked", {"reason": per.reason, "amount": result.amount})
        return {"bucket": "parked", "result": "cap_blocked:" + per.reason}

    # 5. Deterministic dedup key (F7) + idempotent pending.
    key = dedup_mod.dedup_key(
        message.message_id, result.amount, beneficiary.beneficiary_id, result.currency
    )
    if store.is_executed(key):
        audit.append("already_executed", {"dedup_key": key})
        return {"bucket": "executed", "result": "already_executed", "dedup_key": key}

    nonce = token_mod.new_nonce()
    record = store.add_pending(
        dedup_key=key,
        amount=result.amount,
        currency=result.currency,
        beneficiary_id=beneficiary.beneficiary_id,
        source_account_id=source_id,
        source_account_last3=source_last3,
        message_id=message.message_id,
        nonce=nonce,
    )
    audit.append("pending_recorded", {
        "dedup_key": key, "amount": record["amount"], "currency": record["currency"],
        "beneficiary_id": beneficiary.beneficiary_id, "source_account_last3": source_last3,
    })

    # 6. Authorization: ONLY a valid, bound, single-use, unexpired HMAC token (F1/F2/F10).
    if not message.token:
        # No token => inbound text alone never authorizes. Stays pending.
        audit.append("awaiting_token", {"dedup_key": key})
        return {"bucket": "pending", "result": "pending", "dedup_key": key}

    claim = token_mod.verify_token(
        settings.require_signing_secret(),
        message.token,
        source_account_id=record["source_account_id"],
        amount=record["amount"],
        beneficiary_id=record["beneficiary_id"],
        dedup_key=key,
    )
    if claim is None or claim.nonce != record.get("nonce"):
        # Forged/expired/altered/wrong-secret/reused token -> never executes (F1/F2).
        audit.append("token_rejected", {"dedup_key": key, "reason": "invalid or non-matching token"})
        return {"bucket": "pending", "result": "token_rejected", "dedup_key": key}

    # 7. PRE-EXECUTE: re-read daily total, re-check + atomically commit aggregate,
    #    mark executed, burn nonce — all before any write endpoint (PR3/F6).
    commit = store.execute_and_commit(
        key, record["amount"],
        daily_aggregate_cap=settings.daily_aggregate_cap,
        execution_mode="live" if live else "dry-run",
    )
    if not commit["committed"]:
        audit.append("aggregate_blocked", {"reason": commit["reason"], "dedup_key": key})
        return {"bucket": "parked", "result": "aggregate_blocked:" + commit["reason"], "dedup_key": key}

    # 8. Execute. DRY-RUN by default: NO write endpoint called. Live only when
    #    live_enabled() (flag AND write cred), and that fact is audited (F8/PR1).
    if live:
        audit.append("live_execute", {
            "dedup_key": key, "beneficiary_id": record["beneficiary_id"],
            "execution_mode": "live", "live_enabled": True,
        })
        client.create_payment(
            record["source_account_id"], record["beneficiary_id"], record["amount"],
        )
        audit.append("executed", {"dedup_key": key, "execution_mode": "live",
                                  "daily_total": commit["daily_total"]})
        return {"bucket": "executed", "result": "executed:live", "dedup_key": key}

    audit.append("executed", {"dedup_key": key, "execution_mode": "dry-run",
                              "daily_total": commit["daily_total"]})
    return {"bucket": "executed", "result": "executed:dry-run", "dedup_key": key}


def _approval_items(settings, pendings, allowlist, real_accounts):
    items = []
    name_by_id = {b.beneficiary_id: b.name for b in (allowlist if allowlist is not None
                                                      else bene.ALLOWLIST)}
    for rec in pendings:
        token, _ = token_mod.issue_token(
            settings.require_signing_secret(),
            source_account_id=rec["source_account_id"],
            amount=rec["amount"],
            beneficiary_id=rec["beneficiary_id"],
            dedup_key=rec["dedup_key"],
            nonce=rec.get("nonce") or token_mod.new_nonce(),
        )
        items.append({
            "amount": rec["amount"],
            "currency": rec["currency"],
            "source_account_last3": rec.get("source_account_last3", ""),
            "beneficiary_name": name_by_id.get(rec["beneficiary_id"], rec["beneficiary_id"]),
            "token": token,
        })
    return items


def _hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(str(value or "").encode()).hexdigest()
