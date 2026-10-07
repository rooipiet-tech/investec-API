"""The approval / cancel handler (S11, F14/F16/F43/F44/F45). Called by cycle step 4 for an AUTHENTICATED sender.

* The batch is ONE ref from Subject/In-Reply-To/References (``refs.find_batch_refs``) and applies only to rows of
  that batch whose ``notify_to`` is the replying address. Zero or several refs, or no rows -> batch-not-matched.
* ``cmd`` comes from the ONE parser (``commands.parse_command``). Numbers are range-checked HERE against the batch
  (0, beyond the batch size or a non-member make the whole command a no-op with a not-understood notice).
* approve needs a fresh reply (message age window) that does not predate the batch; otherwise nothing changes and
  the authenticated sender gets ONE acknowledgement saying ``expired`` or ``not_applied`` (R7-T11).
* cancel (bare = every row of the batch, or the listed numbers) is never age-gated, works for approved-but-unclaimed
  (``accepted``) rows and is also honoured from the raw first line (the parser decides, R7-T1).
* This handler NEVER executes anything: approval only moves rows to ``accepted``; they run in a later cycle (F45).
"""
from __future__ import annotations

from collections.abc import MutableMapping, Sequence
from datetime import datetime

from . import notify_v2
from .commands import Command
from .mode import v2_settings
from .notices import safe_send, sender_of

CANCELLABLE = {"awaiting_approval", "accepted"}


def _not_matched(settings, audit, smtp_send, auth_from, now, notice_counter, ref) -> str:
    audit.append("approval_noop", {"ref": ref or "none", "reason": "batch_not_matched"})
    if _allow_notice(audit, auth_from, notice_counter):
        safe_send(smtp_send, notify_v2.build_batch_not_matched_email(sender_of(settings), auth_from), audit)
    return "approval_noop"


def _allow_notice(audit, auth_from: str, counter: MutableMapping | None) -> bool:
    """R7-T5: not-understood / batch-not-matched notices go out at most ONCE per address per cycle."""
    if counter is None:
        return True
    seen = counter.get(auth_from, 0)
    counter[auth_from] = seen + 1
    if seen == 0:
        return True
    suppressed = counter.get(("suppressed", auth_from), 0) + 1
    counter[("suppressed", auth_from)] = suppressed
    audit.append("notice_suppressed", {"count": suppressed})
    return False


def _skip_code_for_approve(row: dict, reason: str) -> str:
    status = row["status"]
    if reason == "wrong_batch":
        return "not_in_batch"
    if reason == "expired" or status == "expired":
        return "expired"
    return {"accepted": "already_approved", "executed": "already_executed", "cancelled": "already_cancelled",
            "parked": "parked", "submitting": "too_late"}.get(status, "too_late")


def _skip_code_for_cancel(row: dict) -> str:
    return {"cancelled": "already_cancelled", "expired": "expired", "parked": "parked"}.get(row["status"], "too_late")


def handle_approval_message(
    settings, store, audit, *, auth_from: str, batch_refs: Sequence[str], cmd: Command | None,
    received_at: datetime | None, now: datetime, smtp_send, notice_counter: MutableMapping | None = None,
    counters: MutableMapping | None = None,
) -> str:
    cfg = v2_settings(settings)
    sender = sender_of(settings)
    refs = list(dict.fromkeys(batch_refs))
    if len(refs) != 1:
        return _not_matched(settings, audit, smtp_send, auth_from, now, notice_counter, None)
    ref = refs[0]
    rows = store.list_batch(ref, auth_from)
    if not rows:
        return _not_matched(settings, audit, smtp_send, auth_from, now, notice_counter, ref)
    by_no = {r["item_no"]: r for r in rows}
    valid = sorted(by_no)

    def not_understood(reason_code: str) -> str:
        audit.append("approval_noop", {"ref": ref, "reason": reason_code})
        if _allow_notice(audit, auth_from, notice_counter):
            safe_send(smtp_send, notify_v2.build_not_understood_email(
                sender, auth_from, batch_ref=ref, reason_code=reason_code, valid_item_nos=valid), audit)
        return "approval_noop"

    if cmd is None:
        return not_understood("not_a_command")
    if cmd.kind == "invalid":
        return not_understood(cmd.reason or "bad_syntax")
    if any(n not in by_no for n in cmd.numbers):
        return not_understood("out_of_range")

    is_approve = cmd.kind in ("approve_all", "approve")
    targets = valid if not cmd.numbers else list(cmd.numbers)
    approved: list[int] = []
    cancelled: list[int] = []
    skipped: list[tuple[int, str]] = []

    if is_approve:
        fresh = received_at is not None and now - received_at <= cfg.max_age
        if not fresh:
            audit.append("expired_age", {"ref": ref, "reason": "approve_reply_stale"})
            skipped = [(n, "expired") for n in targets]
        else:
            for n in targets:
                row = by_no[n]
                if row.get("offered_at") is not None and received_at < row["offered_at"]:
                    skipped.append((n, "not_applied"))
                    continue
                result = store.approve_item(row["instruction_id"], batch_ref=ref, item_no=n, notify_to=auth_from,
                                            now=now, grace=cfg.grace)
                if result.approved:
                    approved.append(n)
                else:
                    current = store.get(row["instruction_id"]) or row
                    skipped.append((n, _skip_code_for_approve(current, result.reason)))
            if any(code == "not_applied" for _, code in skipped):
                audit.append("approval_predates_batch", {"ref": ref, "count": sum(1 for _, c in skipped if c == "not_applied")})
    else:
        if cmd.source == "raw_first_line":
            audit.append("cancel_from_raw_first_line", {"ref": ref})
        for n in targets:
            row = by_no[n]
            if row["status"] in CANCELLABLE and store.cas_status(
                    row["instruction_id"], CANCELLABLE, "cancelled", now=now, outcome_code="cancelled"):
                cancelled.append(n)
            else:
                current = store.get(row["instruction_id"]) or row
                skipped.append((n, _skip_code_for_cancel(current)))

    if approved:
        audit.append("approve", {"ref": ref, "count": len(approved)})
    if cancelled:
        audit.append("cancel", {"ref": ref, "count": len(cancelled)})
    if not approved and not cancelled:
        audit.append("approval_noop", {"ref": ref, "reason": "nothing_applied"})
    if counters is not None:
        counters["approved"] = counters.get("approved", 0) + len(approved)
        counters["cancelled"] = counters.get("cancelled", 0) + len(cancelled)
    safe_send(smtp_send, notify_v2.build_command_ack_email(
        sender, auth_from, batch_ref=ref, approved=approved, cancelled=cancelled, skipped=skipped), audit)
    return "approval_applied" if (approved or cancelled) else "approval_noop"
