"""Operator-initiated single-payment self-test (additive, isolated).

Lets the operator make ONE explicit payment from the command line — e.g. a R1
payment — to validate the live wiring WITHOUT waiting for an inbound email. It
REUSES every existing guardrail and bypasses none:

  * source account resolved via ``accounts.resolve_source_account`` (or the sole
    account when ``--source`` is omitted); 0/>1 candidates -> fail closed.
  * beneficiary must be a REAL registered payee — confirmed against the live
    ``get_beneficiaries`` list (API) or the static ALLOWLIST by beneficiary_id.
    An unregistered id never pays (pays strictly by beneficiaryId, never a PAN).
  * per-payment cap (``caps.check_per_payment``) AND the persisted daily-aggregate
    cap via the SAME ``store.execute_and_commit`` path the pipeline uses — so the
    self-test counts toward the daily total and cannot exceed it.
  * deterministic dedup key + ``add_pending`` make a repeated identical self-test
    in the same instant idempotent and auditable.
  * execute gate identical to the pipeline: DRY-RUN by default (NO write endpoint),
    live ONLY when ``settings.live_enabled()``; the live enablement is audited.

The ``now``/``ts`` timestamp is injected (never ``datetime.now`` in library code)
so the dedup key and audit are deterministic under test. Returns a machine-
readable dict; the CLI prints it as JSON and maps it to an exit code.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import accounts as acct
from . import beneficiaries as bene
from . import caps as caps_mod
from . import dedup as dedup_mod
from . import token as token_mod
from .audit import AuditLog
from .store import PaymentStore


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _registered_beneficiary(client, settings, allowlist, beneficiary_id: str):
    """Confirm ``beneficiary_id`` is a REAL registered beneficiary and return its
    :class:`beneficiaries.Beneficiary`, else ``None`` (fail closed).

    When ``payments_beneficiaries_from_api`` is set, the id must appear in the
    live ``get_beneficiaries`` list (exact match on beneficiaryId). Otherwise it
    must appear in the static ALLOWLIST (or an explicitly injected ``allowlist``)
    by beneficiary_id. An id not present in the chosen source NEVER pays.
    """
    if allowlist is None and getattr(settings, "payments_beneficiaries_from_api", False):
        source = "api"
        try:
            entries = bene.from_api(client.get_beneficiaries())
        except Exception:  # noqa: BLE001 — fail closed, never bypass safety
            entries = []
    else:
        source = "static"
        entries = bene.ALLOWLIST if allowlist is None else allowlist
    for b in entries:
        if b.beneficiary_id == beneficiary_id:
            return b, source
    return None, source


def run_selftest(
    settings,
    *,
    client,
    beneficiary_id: str,
    amount,
    source_last3: str | None = None,
    reference: str = "",
    expected_currency: str = "ZAR",
    allowlist=None,
    now: str | None = None,
    store=None,
    audit=None,
):
    """Perform one operator self-test payment. ``client`` exposes ``get_accounts``,
    ``get_beneficiaries`` and (live only) ``create_payment``. ``store``/``audit``
    may be injected to select a backend; when omitted, the file-backed defaults
    under ``state_dir`` are used. ``now`` is an injected ISO timestamp (the dedup
    seed); never ``datetime.now`` here.

    Returns a dict with ``committed`` and ``reason``; ``committed`` is False on any
    fail-closed path and NO write endpoint is called in that case."""
    if store is None:
        store = PaymentStore(settings.state_dir)
    if audit is None:
        audit = AuditLog(settings.state_dir)
    now = now or _utcnow_iso()
    live = settings.live_enabled()
    beneficiary_source = (
        "api" if (allowlist is None
                  and getattr(settings, "payments_beneficiaries_from_api", False))
        else "static"
    )

    summary = {
        "self_test": True,
        "execution_mode": "live" if live else "dry-run",
        "live_enabled": live,
        "beneficiary_source": beneficiary_source,
        "requested_beneficiary": beneficiary_id,
        "amount": f"{amount}",
        "source_account_last3": None,
        "mode": "live" if live else "dry-run",
        "committed": False,
        "reason": "",
        "daily_total": str(store.daily_total()),
    }
    audit.append("selftest_start", {
        "execution_mode": summary["execution_mode"], "live_enabled": live,
        "beneficiary_id": beneficiary_id, "amount": summary["amount"],
    }, ts=now)

    # 1. SOURCE account: explicit last-3 (exact; 0/>1 -> fail closed) OR the sole
    #    account when --source is omitted; 0 or >1 candidates -> fail closed.
    accounts = client.get_accounts()
    if source_last3:
        source = acct.resolve_source_account(accounts, source_last3)
    elif len(accounts) == 1:
        source = accounts[0]
    else:
        source = None
    if source is None:
        audit.append("selftest_source_unresolved",
                     {"reason": "0 or >1 source candidates"}, ts=now)
        summary["reason"] = "source_unresolved"
        return summary
    source_id = acct.source_account_id(source)
    source_last3_resolved = acct.source_account_last3(source)
    summary["source_account_last3"] = source_last3_resolved

    # 2. BENEFICIARY verification: must be a REAL registered beneficiary (live API
    #    list or static ALLOWLIST) — never pay an unregistered id (F4).
    beneficiary, _src = _registered_beneficiary(client, settings, allowlist, beneficiary_id)
    if beneficiary is None:
        audit.append("selftest_beneficiary_unregistered",
                     {"reason": "beneficiaryId not in registered list",
                      "beneficiary_id": beneficiary_id}, ts=now)
        summary["reason"] = "beneficiary_unregistered"
        return summary

    # 3. Per-payment cap (F6) — re-use the pipeline's pure gate.
    per = caps_mod.check_per_payment(amount, settings.per_payment_cap)
    if not per.ok:
        audit.append("selftest_cap_blocked",
                     {"reason": per.reason, "amount": summary["amount"]}, ts=now)
        summary["reason"] = "cap_blocked:" + per.reason
        return summary

    # 4. Dedup + idempotent pending. The synthetic message id embeds the injected
    #    timestamp so a repeated identical self-test in the SAME instant is
    #    idempotent and the record is auditable.
    synthetic_mid = f"selftest:{now}:{beneficiary.beneficiary_id}:{summary['amount']}"
    key = dedup_mod.dedup_key(
        synthetic_mid, amount, beneficiary.beneficiary_id, expected_currency
    )
    summary["dedup_key"] = key
    if store.is_executed(key):
        audit.append("selftest_already_executed", {"dedup_key": key}, ts=now)
        summary["reason"] = "already_executed"
        summary["committed"] = False
        summary["daily_total"] = str(store.daily_total())
        return summary

    nonce = token_mod.new_nonce()
    record = store.add_pending(
        dedup_key=key,
        amount=amount,
        currency=expected_currency,
        beneficiary_id=beneficiary.beneficiary_id,
        source_account_id=source_id,
        source_account_last3=source_last3_resolved,
        message_id=synthetic_mid,
        nonce=nonce,
    )
    audit.append("selftest_pending_recorded", {
        "dedup_key": key, "amount": record["amount"], "currency": record["currency"],
        "beneficiary_id": beneficiary.beneficiary_id,
        "source_account_last3": source_last3_resolved,
    }, ts=now)

    # 5. PRE-EXECUTE: re-read daily total, re-check + atomically commit the
    #    aggregate, mark executed, burn nonce — the SAME store path the pipeline
    #    uses, so the self-test counts toward the daily cap and cannot exceed it.
    commit = store.execute_and_commit(
        key, record["amount"],
        daily_aggregate_cap=settings.daily_aggregate_cap,
        execution_mode="live" if live else "dry-run",
    )
    summary["daily_total"] = commit["daily_total"]
    if not commit["committed"]:
        audit.append("selftest_aggregate_blocked",
                     {"reason": commit["reason"], "dedup_key": key}, ts=now)
        summary["reason"] = "aggregate_blocked:" + commit["reason"]
        summary["committed"] = False
        return summary

    # 6. EXECUTE GATE: identical to the pipeline. DRY-RUN calls NO write endpoint;
    #    live ONLY when live_enabled() (flag AND payment-capable cred), audited.
    if live:
        audit.append("selftest_live_execute", {
            "dedup_key": key, "beneficiary_id": record["beneficiary_id"],
            "execution_mode": "live", "live_enabled": True,
        }, ts=now)
        client.create_payment(
            record["source_account_id"], record["beneficiary_id"],
            record["amount"], reference,
        )
        audit.append("selftest_executed",
                     {"dedup_key": key, "execution_mode": "live",
                      "daily_total": commit["daily_total"]}, ts=now)
        summary["committed"] = True
        summary["reason"] = "executed:live"
        return summary

    audit.append("selftest_executed",
                 {"dedup_key": key, "execution_mode": "dry-run",
                  "daily_total": commit["daily_total"]}, ts=now)
    summary["committed"] = True
    summary["reason"] = "executed:dry-run"
    return summary
