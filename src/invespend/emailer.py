"""Send the report workbook(s) via SMTP (Gmail app password by default)."""
from __future__ import annotations

import logging
import mimetypes
import smtplib
from email.message import EmailMessage
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)


def _attach(msg: EmailMessage, attachment: Path) -> None:
    ctype, _ = mimetypes.guess_type(attachment.name)
    maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
    msg.add_attachment(
        attachment.read_bytes(),
        maintype=maintype,
        subtype=subtype,
        filename=attachment.name,
    )


def send_email(
    settings: Settings,
    attachments: Path | list[Path],
    subject: str,
    body: str,
) -> None:
    """Send one email with one or more file attachments.

    ``attachments`` may be a single ``Path`` or a list — the weekly per-account
    statements ride along as separate attachments on a single email.
    """
    settings.require_email()
    paths = [attachments] if isinstance(attachments, Path) else list(attachments)
    if not paths:
        raise ValueError("send_email called with no attachments")

    msg = EmailMessage()
    msg["From"] = settings.report_sender
    msg["To"] = ", ".join(settings.report_recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    for path in paths:
        _attach(msg, path)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        server.starttls()
        server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
    log.info("Emailed %d file(s) to %s", len(paths), msg["To"])


def send_report(settings: Settings, attachment: Path, subject: str, body: str) -> None:
    """Back-compat single-attachment wrapper around :func:`send_email`."""
    send_email(settings, attachment, subject, body)
