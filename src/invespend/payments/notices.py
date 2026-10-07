"""Small shared helpers for v2 notices (S11). Stdlib only; nothing here builds or sends a real SMTP connection."""
from __future__ import annotations

from email.message import EmailMessage


def sender_of(settings: object) -> str:
    return str(getattr(settings, "report_sender", "") or getattr(settings, "smtp_user", "") or "")


def safe_send(smtp_send, msg: EmailMessage, audit, step: str = "notice_send_failed") -> bool:
    """Send one notice; a failure is audited by class name only and never raises (a lost notice must not break the
    state change that was already committed)."""
    try:
        smtp_send(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        audit.append(step, {"reason": type(exc).__name__})
        return False
