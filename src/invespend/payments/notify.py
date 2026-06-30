"""Approval + progress emails via an ISOLATED send path (F12, F14, F19).

These functions build attachment-less ``EmailMessage`` objects and send them by
reusing ``emailer._smtp_send`` DIRECTLY. They never call ``emailer.send_email``
(whose zero-attachment guard must keep raising for its existing callers, F14)
and never route through it.

F12: the progress email is sent ONLY when an explicit sender contact is present,
and its body/headers contain NONE of {beneficiary_id, full account, balance,
caps, token}.

F19: the approval email consolidates all currently-unapproved pendings, each
shown with its OWN bound token; later-arriving items are simply included on the
next refreshed email.
"""
from __future__ import annotations

from email.message import EmailMessage

from ..emailer import _smtp_send

# Fields that must never appear in a progress email (F12).
PROGRESS_FORBIDDEN_KEYS = ("beneficiary_id", "full account", "balance", "caps", "token")


def build_approval_email(
    sender: str,
    recipients: list[str],
    pendings: list[dict],
) -> EmailMessage:
    """Consolidated approval email: each pending listed with its own token (F19).

    ``pendings`` items are dicts: ``amount``, ``currency``, ``source_account_last3``,
    ``beneficiary_name``, ``token``. Reply with the token to approve THAT payment.
    """
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"Payment approval required: {len(pendings)} pending"
    lines = [
        "The following payment(s) are pending approval. To approve a payment, "
        "reply with its approval token exactly. Inbound text alone never "
        "authorizes a payment — only a valid token does.",
        "",
    ]
    for i, p in enumerate(pendings, 1):
        lines.append(
            f"{i}. {p.get('currency', '')} {p.get('amount', '')} "
            f"to {p.get('beneficiary_name', '')} "
            f"from account ...{p.get('source_account_last3', '')}"
        )
        lines.append(f"   token: {p.get('token', '')}")
        lines.append("")
    msg.set_content("\n".join(lines))
    return msg


def send_approval_email(settings, recipients: list[str], pendings: list[dict]) -> EmailMessage:
    msg = build_approval_email(settings.report_sender, recipients, pendings)
    _smtp_send(settings, msg)
    return msg


def build_progress_email(sender: str, recipient: str, processed: int, parked: int) -> EmailMessage:
    """Minimised status email (F12): only aggregate counts; NO beneficiary id,
    NO account number, NO balance, NO caps, NO token."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = "Payment request received"
    msg.set_content(
        "Your payment request has been received and is being processed by the "
        "account owner. You will not receive further account details here.\n\n"
        f"Items in this update: {processed + parked}."
    )
    return msg


def send_progress_email(settings, sender_contact: str, processed: int, parked: int):
    """Send a minimised progress email — ONLY when an explicit contact exists (F12).

    Returns the sent message, or ``None`` when no contact was provided (send mock
    must stay not-called in that case)."""
    if not sender_contact or not str(sender_contact).strip():
        return None
    msg = build_progress_email(settings.report_sender, sender_contact, processed, parked)
    _smtp_send(settings, msg)
    return msg
