"""Send the report workbook(s) via the Gmail API (preferred) or SMTP fallback."""
from __future__ import annotations

import base64
import logging
import mimetypes
import smtplib
from email.message import EmailMessage
from pathlib import Path

import requests

from .config import Settings

log = logging.getLogger(__name__)

_GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


def _attach(msg: EmailMessage, attachment: Path) -> None:
    ctype, _ = mimetypes.guess_type(attachment.name)
    maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
    msg.add_attachment(
        attachment.read_bytes(),
        maintype=maintype,
        subtype=subtype,
        filename=attachment.name,
    )


def _build_message(settings: Settings, attachments: list[Path],
                   subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.report_sender
    msg["To"] = ", ".join(settings.report_recipients)
    msg["Subject"] = subject
    msg.set_content(body)
    for path in attachments:
        _attach(msg, path)
    return msg


def _refresh_access_token(settings: Settings) -> str:
    """Exchange the stored refresh token for a short-lived access token."""
    resp = requests.post(
        _GMAIL_TOKEN_URL,
        data={
            "client_id": settings.gmail_client_id,
            "client_secret": settings.gmail_client_secret,
            "refresh_token": settings.gmail_refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _send_via_gmail_api(settings: Settings, msg: EmailMessage) -> None:
    """Send a pre-built EmailMessage via the Gmail REST API."""
    access_token = _refresh_access_token(settings)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    resp = requests.post(
        _GMAIL_SEND_URL,
        json={"raw": raw},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    log.info("Gmail API: sent message id=%s to %s", resp.json().get("id"), msg["To"])


def _send_via_smtp(settings: Settings, msg: EmailMessage) -> None:
    """Send via Gmail SMTP (app-password fallback)."""
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        server.starttls()
        server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
    log.info("SMTP: emailed %d file(s) to %s", len(msg.get_payload()), msg["To"])


def send_email(
    settings: Settings,
    attachments: Path | list[Path],
    subject: str,
    body: str,
) -> None:
    """Send one email with one or more file attachments.

    Uses the Gmail API when GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET /
    GMAIL_REFRESH_TOKEN are all configured; falls back to SMTP otherwise.
    ``attachments`` may be a single ``Path`` or a list.
    """
    settings.require_email()
    paths = [attachments] if isinstance(attachments, Path) else list(attachments)
    if not paths:
        raise ValueError("send_email called with no attachments")

    msg = _build_message(settings, paths, subject, body)

    if settings.use_gmail_api:
        _send_via_gmail_api(settings, msg)
    else:
        _send_via_smtp(settings, msg)


def send_report(settings: Settings, attachment: Path, subject: str, body: str) -> None:
    """Back-compat single-attachment wrapper around :func:`send_email`."""
    send_email(settings, attachment, subject, body)
