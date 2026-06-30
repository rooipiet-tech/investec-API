"""Inbox reading + pure message parsing (F1, F3, F16).

The pipeline depends on the small :class:`InboxReader` protocol so tests inject a
fake. ``ImapInbox`` is the real stdlib (``imaplib`` + ``email``) implementation.

The pure helpers (``extract_last3``, ``extract_token``, ``parse_message``) are
the only place inbound email is interpreted. CRITICAL (F1): nothing here treats
the From-address, subject, body keywords ('app'/'approve'/'yes'), or any
plaintext as an authenticator. ``extract_token`` only extracts a candidate token
string; whether it is VALID is decided solely by ``token.verify_token`` against
the trusted pending record.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import Message

# last-3 selector, e.g. "...123" or "account ending 123" or "acc 123"
_LAST3_RE = re.compile(r"(?:\.{2,}|ending|acc(?:ount)?\s*(?:no\.?|number)?\s*[:#]?\s*\D*)(\d{3})\b", re.IGNORECASE)
# Token candidate: nonce:expiry:mac (mac is 64 hex chars).
_TOKEN_RE = re.compile(r"\b([A-Za-z0-9_\-]{8,}:\d{8,}:[0-9a-f]{64})\b")


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    from_addr: str
    subject: str
    body: str
    token: str | None
    last3: str | None
    attachments: list[tuple[str, bytes]] = field(default_factory=list)


def extract_last3(text: str) -> str | None:
    """Pull the last-3 source-account selector. Weak selector only — NEVER auth."""
    if not text:
        return None
    m = _LAST3_RE.search(text)
    if m:
        return m.group(1)
    # Fallback: a bare 3-digit group after a hash/ellipsis is already covered;
    # otherwise no confident selector -> None (fail-closed selection upstream).
    return None


def extract_token(text: str) -> str | None:
    """Extract a candidate approval-token STRING. Validity is decided elsewhere
    via HMAC verification — extraction alone never authorizes anything (F1)."""
    if not text:
        return None
    m = _TOKEN_RE.search(text)
    return m.group(1) if m else None


def _body_text(msg: Message) -> str:
    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True)
                if payload:
                    parts.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            parts.append(payload.decode(msg.get_content_charset() or "utf-8", "replace"))
    return "\n".join(parts)


def _attachments(msg: Message) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    if not msg.is_multipart():
        return out
    for part in msg.walk():
        filename = part.get_filename()
        if filename:
            payload = part.get_payload(decode=True)
            if payload is not None:
                out.append((filename, payload))
    return out


def parse_message(raw: bytes) -> InboundMessage:
    """Parse a raw RFC822 message into an :class:`InboundMessage`."""
    msg = message_from_bytes(raw)
    body = _body_text(msg)
    subject = str(msg.get("Subject", ""))
    search_space = f"{subject}\n{body}"
    return InboundMessage(
        message_id=str(msg.get("Message-ID", "")),
        from_addr=str(msg.get("From", "")),
        subject=subject,
        body=body,
        token=extract_token(search_space),
        last3=extract_last3(search_space),
        attachments=_attachments(msg),
    )


class InboxReader:
    """Protocol: ``fetch_messages()`` returns a list of :class:`InboundMessage`."""

    def fetch_messages(self) -> list[InboundMessage]:  # pragma: no cover - interface
        raise NotImplementedError


class ImapInbox(InboxReader):
    """Real stdlib IMAP reader. Not exercised by offline tests (mocked instead)."""

    def __init__(self, settings) -> None:
        self._settings = settings

    def fetch_messages(self) -> list[InboundMessage]:  # pragma: no cover - network
        import imaplib

        s = self._settings
        out: list[InboundMessage] = []
        with imaplib.IMAP4_SSL(s.imap_host, s.imap_port) as conn:
            conn.login(s.imap_user, s.imap_password)
            conn.select(s.imap_mailbox)
            typ, data = conn.search(None, "UNSEEN")
            if typ != "OK":
                return out
            for num in data[0].split():
                typ, msg_data = conn.fetch(num, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                out.append(parse_message(msg_data[0][1]))
        return out
