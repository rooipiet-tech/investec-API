"""Send the report workbook via SMTP (Gmail app password by default)."""
from __future__ import annotations

import logging
import mimetypes
import smtplib
from email.message import EmailMessage
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)


def send_report(settings: Settings, attachment: Path, subject: str, body: str) -> None:
    settings.require_email()

    msg = EmailMessage()
    msg["From"] = settings.report_sender
    msg["To"] = ", ".join(settings.report_recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    ctype, _ = mimetypes.guess_type(attachment.name)
    maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
    msg.add_attachment(
        attachment.read_bytes(),
        maintype=maintype,
        subtype=subtype,
        filename=attachment.name,
    )

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        server.starttls()
        server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
    log.info("Emailed %s to %s", attachment.name, msg["To"])
