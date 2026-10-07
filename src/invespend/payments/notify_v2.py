"""v2 notification builders (S10). Stdlib only; builders RETURN ``EmailMessage`` objects.

NOTHING in this module sends mail: no SMTP, no socket, no import of the legacy notifier.

Every builder applies the loop guard (``Auto-Submitted``, ``X-Invespend-Notification``, its
own ``Message-ID`` carrying the instruction or batch ref) and starts the body with the
content sentinel ``loopguard.NOTICE_FIRST_LINE``. Subjects are ``<words> [INV-<ref>]`` or
``<words> [BATCH <batch_ref>]`` and never start with a reply/forward prefix. ONLY the
paste-details email contains a full account number (to the verified sender). Third-party
values (payee, reference, ...) are flattened to one line, capped and have long digit runs
masked, so they cannot add lines, fake numbered items or smuggle a command.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage

from .bankdetails import BankDetails
from .loopguard import NOTICE_FIRST_LINE, SUBJECT_REPLY_PREFIXES, apply_loop_guard
from .outcome import sanitize_provider_message

BENEFICIARY_FIELD_ORDER = (
    "Beneficiary name", "Bank", "Account number", "Amount", "Their reference",
    "My reference", "Payment notification", "Beneficiary email address", "Beneficiary mobile number",
)
REFERENCE_ONLY_FIELDS = ("Branch code",)   # rendered as a separate line AFTER the ordered list
CANCEL_CAVEAT = (
    "Items you have already approved WILL still run in the next cycle unless you cancel them with a recognised cancel reply "
    "(cancel, or cancel followed by item numbers, alone on the first line)."
)
NOT_PROVIDED = "(not provided)"

_SAST = timezone(timedelta(hours=2))
# 9+ digits with up to three separator chars (space - . / _) between digits: masked in third-party fields
_LONG_NUMBER = re.compile(r"\d(?:[ \-./_]{0,3}\d){8,}")
_CODE = re.compile(r"[a-z0-9_]{1,40}")
_FIGURE_SOURCES = ("typed", "attachment", "image")
_PROBLEM_KINDS = ("failed", "needs_review", "parked", "needs_authorisation", "resend", "cycle_failed")


@dataclass(frozen=True)
class BatchItem:
    item_no: int
    payee_name: str
    amount: Decimal
    currency: str
    source_last3: str
    their_reference: str
    figures_source: str      # typed | attachment | image


@dataclass(frozen=True)
class ExpiredItem:
    batch_ref: str | None
    item_no: int | None
    payee_name: str
    amount: Decimal
    currency: str
    kind: str                # unapproved | approved_not_executed | awaiting_beneficiary | held


@dataclass(frozen=True)
class PendingItem:
    batch_ref: str
    item_no: int
    payee_name: str
    amount: Decimal
    currency: str
    expires_at: datetime


# ------------------------------------------------------------------ helpers
def _clean(value: object, limit: int = 100, *, mask: bool = True, pipes: bool = True) -> str:
    text = "" if value is None else str(value)[: limit * 4]
    text = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in text)
    text = " ".join(text.split())[:limit].strip()
    if pipes:
        text = text.replace("|", "/")
    if mask:
        text = _LONG_NUMBER.sub("[number hidden]", text)
    return text or NOT_PROVIDED


def _money(amount: Decimal) -> str:
    try:
        value = Decimal(amount)
        if not value.is_finite():
            raise ValueError
        return format(value.quantize(Decimal("0.01")), "f")
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("amount must be a finite decimal") from None


def _last3(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-3:] if len(digits) >= 3 else "(unknown)"


def _sast(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return f"{moment.astimezone(_SAST):%Y-%m-%d %H:%M} SAST"


def _code(value: object) -> str:
    text = str(value or "")
    return text if _CODE.fullmatch(text) else "unspecified"


def _nos(numbers: Sequence[int]) -> str:
    return ", ".join(str(int(n)) for n in sorted(set(numbers)))


def _build(sender: str, to: str, subject: str, ref: str | None, lines: Sequence[str]) -> EmailMessage:
    lowered = subject.strip().lower()
    if any(lowered.startswith(prefix) for prefix in SUBJECT_REPLY_PREFIXES):
        raise ValueError("subject must not look like a reply or forward")
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content("\n".join([NOTICE_FIRST_LINE, "", *lines]) + "\n")
    apply_loop_guard(msg, sender, ref)
    return msg


def _inv(words: str, ref: str) -> str:
    return f"{words} [INV-{ref}]"


def _batch_tag(words: str, batch_ref: str | None) -> str:
    return f"{words} [BATCH {batch_ref}]" if batch_ref else words


# --------------------------------------------------------------- paste email
def build_paste_details_email(
    sender: str, to: str, *, ref: str, details: BankDetails, amount: Decimal, their_reference: str,
    my_reference: str, payment_notification: str = "", beneficiary_email: str = "", beneficiary_mobile: str = "",
) -> EmailMessage:
    def raw(value: object) -> str:   # the verified user's own details: not masked
        return _clean(value, mask=False, pipes=False)

    values = {
        "Beneficiary name": raw(details.payee_name),
        "Bank": raw(details.bank),
        "Account number": raw(details.account_number),
        "Amount": _money(amount),
        "Their reference": raw(their_reference),
        "My reference": raw(my_reference),
        "Payment notification": raw(payment_notification),
        "Beneficiary email address": raw(beneficiary_email),
        "Beneficiary mobile number": raw(beneficiary_mobile),
    }
    lines = [
        "This payee is not in your Investec beneficiary list and the API cannot create beneficiaries.",
        "Add the beneficiary in Investec Online or the mobile app, entering these fields in this order:",
        "",
        *[f"{name}: {values[name]}" for name in BENEFICIARY_FIELD_ORDER],
        "",
        f"Branch code (reference only, not one of the fields above): {raw(details.branch_code)}",
        "",
        "Once the payee appears in your Investec beneficiary list the instruction is offered to you in a batch "
        "for approval. Nothing has been paid.",
    ]
    return _build(sender, to, _inv("Add this beneficiary in Investec", ref), ref, lines)


# --------------------------------------------------------------- batch email
def build_batch_approval_email(
    sender: str, to: str, *, batch_ref: str, items: Sequence[BatchItem], pending: Sequence[PendingItem],
    expires_at: datetime,
) -> EmailMessage:
    if not items:
        raise ValueError("a batch email needs at least one item")
    lines = [f"Batch: {batch_ref}", f"{len(items)} payment(s) awaiting your approval.", ""]
    totals: dict[str, Decimal] = {}
    for item in items:
        source = item.figures_source if item.figures_source in _FIGURE_SOURCES else "unknown"
        currency = _clean(item.currency, 8, mask=False)
        lines.append(
            f"{int(item.item_no)}. {_clean(item.payee_name)} | {currency} {_money(item.amount)} | "
            f"from account ending {_last3(item.source_last3)} | "
            f"reference: {_clean(item.their_reference)} | figures from: {source}"
        )
        if source == "image":
            lines.append("   (read from an image; check the amount and payee)")
        totals[currency] = totals.get(currency, Decimal(0)) + Decimal(item.amount)
    lines += ["", "Batch total: " + "; ".join(f"{cur} {_money(total)}" for cur, total in totals.items()), ""]
    lines += [
        "How to reply: reply to THIS email, keeping the subject, with ONLY the command on the first line, above any quoted text:",
        "- `approve` (all items)",
        "- `approve 1 3` (the listed items)",
        "- `cancel` (every item of this batch that has not run yet, approved ones included)",
        "- `cancel 2` (drops the listed items; several numbers allowed)",
        "Anything else changes nothing. " + CANCEL_CAVEAT,
        f"Items you do not mention stay pending until {_sast(expires_at)} and are then expired. Approved items run in the "
        "next cycle (about 15 minutes), still subject to dry-run/live gating and final checks.",
    ]
    if pending:
        lines += ["", "Still pending from earlier batches:"]
        for p in pending:
            lines.append(
                f"{p.batch_ref} #{int(p.item_no)} | {_clean(p.payee_name)} | {_clean(p.currency, 8, mask=False)} "
                f"{_money(p.amount)} | expires {_sast(p.expires_at)}"
            )
        lines.append("To act on these, reply to the original email of that batch.")
    return _build(sender, to, _batch_tag("Payments awaiting approval", batch_ref), batch_ref, lines)


# ------------------------------------------------------------ outcome emails
def build_outcome_email(
    sender: str, to: str, *, ref: str, payee_name: str, amount: Decimal, currency: str, source_last3: str, outcome: object,
) -> EmailMessage:
    status = str(getattr(outcome, "status", "") or "")
    if getattr(outcome, "dry_run", False):
        status = "dry_run"
    summary = [
        f"Payee: {_clean(payee_name)}",
        f"Amount: {_clean(currency, 8, mask=False)} {_money(amount)}",
        f"From account ending {_last3(source_last3)}",
    ]
    if status == "success":
        words, head = "Payment executed", ["The approved payment was executed."]
    elif status == "dry_run":
        words = "Payment dry-run"
        head = ["DRY-RUN: no money moved. The approved payment was checked and would have been sent in live mode."]
    elif status == "failed":
        words = "Payment failed"
        head = ["The payment was not completed.", f"Investec message: {sanitize_provider_message(getattr(outcome, 'message', ''))}",
                "It will not be resent automatically."]
    elif status == "needs_authorisation":
        words = "Payment needs authorisation in Investec"
        head = ["Investec needs you to authorise this payment in Investec Online or the app."]
    else:
        words = "Payment outcome unknown"
        head = ["The outcome of this payment is unknown and it MAY HAVE BEEN PAID.",
                "Check the account before sending it again. It will not be resent automatically."]
    return _build(sender, to, _inv(words, ref), ref, [*head, "", *summary])


def build_beneficiary_observed_email(
    sender: str, to: str, *, ref: str, payee_name: str, amount: Decimal, currency: str, source_last3: str,
    hold_hours: int, eligible_at: datetime,
) -> EmailMessage:
    hours = max(int(hold_hours), 0)
    lines = [
        f"The payee {_clean(payee_name)} ({_clean(currency, 8, mask=False)} {_money(amount)}, from account ending "
        f"{_last3(source_last3)}) is now in your Investec beneficiary list.",
        f"Hold: {hours} hours.",
    ]
    if hours:
        lines.append(f"Eligible from {_sast(eligible_at)}; it will appear in the next batch approval email after that.")
    else:
        lines.append("No hold applies: it will appear in the next batch approval email.")
    lines.append("Nothing is paid until you approve it in a batch.")
    return _build(sender, to, _inv("Payee now registered", ref), ref, lines)


_ACK_WORDS = {
    "expired": "expired", "not_applied": "reply predates batch", "already_approved": "already approved",
    "already_executed": "already executed", "already_cancelled": "already cancelled", "too_late": "too late",
    "not_in_batch": "not in this batch", "parked": "parked",
}


def build_command_ack_email(
    sender: str, to: str, *, batch_ref: str, approved: Sequence[int], cancelled: Sequence[int],
    skipped: Sequence[tuple[int, str]],
) -> EmailMessage:
    parts = []
    if approved:
        parts.append(f"approved: {_nos(approved)} (will run in the next cycle)")
    if cancelled:
        parts.append(f"cancelled: {_nos(cancelled)}")
    if skipped:
        parts.append("not applied: " + ", ".join(
            f"{int(n)} ({_ACK_WORDS.get(code, 'not applied')})" for n, code in sorted(skipped, key=lambda s: s[0])))
    text = "; ".join(parts) if parts else "nothing was changed"
    return _build(sender, to, _batch_tag("Reply received", batch_ref), batch_ref,
                  [f"Batch {batch_ref}: {text}"])


def build_expiry_email(sender: str, to: str, *, items: Sequence[ExpiredItem]) -> EmailMessage:
    phrases = {
        "unapproved": "expired unapproved",
        "approved_not_executed": "approved but not executed in time (expired)",
        "awaiting_beneficiary": "awaiting beneficiary expired",
        "held": "held item expired",
    }
    lines = ["These items expired this cycle and will not be executed:"]
    for item in items:
        where = [item.batch_ref or "(no batch)"]
        if item.item_no is not None:
            where.append(f"#{int(item.item_no)}")
        lines.append(" ".join([*where, _clean(item.payee_name), _clean(item.currency, 8, mask=False),
                               _money(item.amount), phrases.get(item.kind, "expired")]))
    return _build(sender, to, "Payment items expired", None, lines)


_PROBLEM_SUBJECTS = {
    "failed": "Payment failed",
    "needs_review": "Payment outcome unknown",
    "parked": "Payment instruction not actioned",
    "needs_authorisation": "Payment needs authorisation in Investec",
    "resend": "Please resend your payment instruction",
}


def build_problem_email(
    sender: str, to: str, *, ref: str | None, kind: str, detail_code: str, provider_message: str | None = None,
) -> EmailMessage:
    if kind not in _PROBLEM_KINDS:
        raise ValueError(f"unknown problem kind: {kind!r}")
    if kind == "cycle_failed":
        return _build(sender, to, "Payment cycle failed", ref, ["The payment cycle failed. Check the logs."])
    code = f"Reason code: {_code(detail_code)}"
    if kind == "failed":
        lines = ["The payment was not completed.", code,
                 f"Investec message: {sanitize_provider_message(provider_message)}",
                 "It will not be resent automatically. A new attempt needs a fresh instruction and a new approval."]
    elif kind == "needs_review":
        lines = ["The outcome of this payment is unknown and it MAY HAVE BEEN PAID.", code]
        shown = sanitize_provider_message(provider_message)
        if shown:
            lines.append(f"Investec returned an error: {shown}. It may not have been paid. "
                         "Check Investec Online before sending again.")
        lines += ["Check the account before sending it again. It will not be resent automatically."]
    elif kind == "parked":
        lines = ["Your instruction was not turned into a payment and nothing was paid.", code,
                 "Send a new, corrected instruction if you still want it processed."]
    elif kind == "needs_authorisation":
        lines = ["Investec needs you to authorise this payment in Investec Online or the app.", code,
                 "Nothing further is sent automatically."]
    else:   # resend
        lines = ["Your earlier message was not fully processed. Please send it again."]
    assert ref is not None
    return _build(sender, to, _inv(_PROBLEM_SUBJECTS[kind], ref), ref, lines)


_NOT_UNDERSTOOD = {
    "bad_syntax": "the command line was not in an accepted form",
    "duplicate_number": "an item number was repeated",
    "both_verbs_in_text": "the reply contained both approve and cancel",
    "out_of_range": "an item number is not in this batch",
    "quote_stripped": "no command was found above the quoted text",
}


def build_not_understood_email(
    sender: str, to: str, *, batch_ref: str | None, reason_code: str, valid_item_nos: Sequence[int],
) -> EmailMessage:
    reason = _NOT_UNDERSTOOD.get(reason_code, "the reply was not recognised")
    lines = ["Reply not understood; nothing was approved or cancelled.", f"Reason: {reason}."]
    numbers = sorted(set(int(n) for n in valid_item_nos))
    if numbers:
        contiguous = numbers == list(range(1, len(numbers) + 1))
        lines.append("Valid item numbers: " + (f"1..{len(numbers)}" if contiguous else _nos(numbers)))
    lines += [
        "Accepted commands, alone on the first line above any quoted text:",
        "- approve", "- approve 1 3", "- cancel", "- cancel 2",
        CANCEL_CAVEAT,
    ]
    return _build(sender, to, _batch_tag("Reply not understood", batch_ref), batch_ref, lines)


def build_batch_not_matched_email(sender: str, to: str) -> EmailMessage:
    lines = [
        "Reply not matched: no batch matched; reply to the batch email itself (keep the subject, first line only "
        "the command). Nothing was approved or cancelled.",
        CANCEL_CAVEAT,
    ]
    return _build(sender, to, "Reply not matched to a payment batch", None, lines)
