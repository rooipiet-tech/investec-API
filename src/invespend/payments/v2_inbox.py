"""v2 inbound message model (S2). Stdlib only, no legacy import.

The legacy ``inbox.InboundMessage`` drops the raw message and INTERNALDATE, so
v2 has its own frozen model. ``raw`` is the RFC822 bytes: the cycle parses it
ONCE and hands the ``email.message.Message`` to the text/attachment readers;
it is never logged or audited (``repr=False``).
"""
from __future__ import annotations

import email
import email.policy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol


@dataclass(frozen=True)
class V2Message:
    message_id: str                        # "" if missing (rejected at the identity step)
    subject: str
    from_headers: tuple[str, ...]          # msg.get_all("From")
    auth_results: tuple[str, ...]          # ALL Authentication-Results values, index 0 = topmost
    in_reply_to: str
    references: str
    auto_submitted: str
    x_invespend_notification: str
    precedence: str                        # raw values; "" when absent
    x_autoreply: str
    x_autorespond: str
    x_auto_response_suppress: str
    internaldate: datetime | None          # IMAP INTERNALDATE (trusted)
    received_header_time: datetime | None  # topmost Received header timestamp
    raw: bytes = field(repr=False)         # RFC822 bytes; never logged/audited


def _hdr(msg, name: str) -> str:
    value = msg.get(name)
    return "" if value is None else str(value)


def _all(msg, name: str) -> tuple[str, ...]:
    return tuple(str(v) for v in (msg.get_all(name) or ()))


def _received_time(msg) -> datetime | None:
    values = msg.get_all("Received") or ()
    if not values:
        return None
    topmost = str(values[0])
    if ";" not in topmost:
        return None
    try:
        parsed = parsedate_to_datetime(topmost.rsplit(";", 1)[1].strip())
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_v2_message(raw: bytes, internaldate: datetime | None = None) -> V2Message:
    # compat32 keeps header values as the raw (possibly folded) strings, which
    # is what the auth tokenizer and the identity hash want to see.
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    return V2Message(
        message_id=_hdr(msg, "Message-ID").strip(),
        subject=_hdr(msg, "Subject"),
        from_headers=_all(msg, "From"),
        auth_results=_all(msg, "Authentication-Results"),
        in_reply_to=_hdr(msg, "In-Reply-To"),
        references=_hdr(msg, "References"),
        auto_submitted=_hdr(msg, "Auto-Submitted"),
        x_invespend_notification=_hdr(msg, "X-Invespend-Notification"),
        precedence=_hdr(msg, "Precedence"),
        x_autoreply=_hdr(msg, "X-Autoreply"),
        x_autorespond=_hdr(msg, "X-Autorespond"),
        x_auto_response_suppress=_hdr(msg, "X-Auto-Response-Suppress"),
        internaldate=internaldate,
        received_header_time=_received_time(msg),
        raw=raw,
    )


class V2InboxReader(Protocol):
    def fetch_messages(self) -> list[V2Message]: ...


class V2ImapInbox:  # pragma: no cover - network
    """Real IMAP reader: same host/credentials/mailbox settings and RFC822 fetch
    semantics as the legacy reader, plus INTERNALDATE. No PEEK change."""

    def __init__(self, settings) -> None:
        self._settings = settings

    def fetch_messages(self) -> list[V2Message]:
        import imaplib
        import time

        s = self._settings
        out: list[V2Message] = []
        with imaplib.IMAP4_SSL(s.imap_host, s.imap_port) as conn:
            conn.login(s.imap_user, s.imap_password)
            conn.select(s.imap_mailbox)
            typ, data = conn.search(None, "UNSEEN")
            if typ != "OK":
                return out
            for num in data[0].split():
                typ, msg_data = conn.fetch(num, "(INTERNALDATE RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                internal = None
                parsed = imaplib.Internaldate2tuple(msg_data[0][0])
                if parsed:
                    internal = datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
                out.append(parse_v2_message(msg_data[0][1], internal))
        return out
