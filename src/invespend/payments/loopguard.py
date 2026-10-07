"""Own-notification guard for v2 (S2). Pure, stdlib only.

A notice we send must never be processed as inbound mail. Matching uses ONLY
the message's own Message-ID, its automatic-mail headers and a content
sentinel. It NEVER looks at References / In-Reply-To (a human reply to our
email carries our id there, and ignoring it would drop approvals and cancels).
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from email.message import EmailMessage
from email.utils import make_msgid

AUTO_SUBMITTED_VALUE = "auto-generated"
NOTIFICATION_HEADER = "X-Invespend-Notification"
# make_msgid(idstring=X) renders '<timeval.pid.rand.X@domain>', so every matcher
# is an UNANCHORED search with a leading "\." (R7-T3).
MSGID_IDSTRING = "invespend-notification"
NOTICE_FIRST_LINE = "Invespend automated notice. Do not reply with payment instructions."
SUBJECT_REPLY_PREFIXES = (
    "re:", "fwd:", "fw:", "wg:", "tr:", "rv:", "i:", "enc:", "doorst.:",
    "vs:", "aw:", "sv:", "antw:", "odp:",
)
OWN_MSGID_RE = re.compile(r"\.invespend-notification(\.[^@]*)?@")
# RFC 5322 line limit: the (attacker-controlled) id is cut before the unanchored
# search so matching is linear in a bounded string.
MAX_MSGID_CHARS = 998
AUTO_PRECEDENCE = frozenset({"bulk", "junk", "list", "auto_reply"})


def _domain_of(sender: str) -> str | None:
    if "@" in sender:
        return sender.rsplit("@", 1)[1].strip().strip(">") or None
    return None


def loop_guard_headers(sender: str, ref: str | None) -> dict[str, str]:
    idstring = MSGID_IDSTRING + ("." + ref if ref else "")
    return {
        "Auto-Submitted": AUTO_SUBMITTED_VALUE,
        NOTIFICATION_HEADER: "1",
        "Message-ID": make_msgid(idstring=idstring, domain=_domain_of(sender)),
    }


def apply_loop_guard(msg: EmailMessage, sender: str, ref: str | None) -> None:
    for name, value in loop_guard_headers(sender, ref).items():
        if name in msg:
            del msg[name]
        msg[name] = value


def is_own_notification(
    message_id: str,
    auto_submitted: str,
    notification_header: str,
    raw_head_lines: Sequence[str] = (),
    subject: str = "",
    auto_headers: Mapping[str, str] | None = None,
) -> str | None:
    """Reason code when the message is automatic/our own, else None.

    Codes: "auto_submitted" | "x_invespend" | "own_message_id" | "own_body"
    | "bulk_precedence" | "auto_reply_header" | None
    """
    auto = (auto_submitted or "").strip().lower()
    if auto and auto != "no":
        return "auto_submitted"
    if (notification_header or "").strip():
        return "x_invespend"
    if OWN_MSGID_RE.search((message_id or "")[:MAX_MSGID_CHARS]):
        return "own_message_id"
    headers = auto_headers or {}
    if (headers.get("precedence") or "").strip().lower() in AUTO_PRECEDENCE:
        return "bulk_precedence"
    for key in ("x_autoreply", "x_autorespond", "x_auto_response_suppress"):
        if (headers.get(key) or "").strip():
            return "auto_reply_header"
    lowered = (subject or "").strip().lower()
    is_reply_subject = any(lowered.startswith(p) for p in SUBJECT_REPLY_PREFIXES)
    if not is_reply_subject:
        non_empty = [ln.strip() for ln in raw_head_lines if ln and ln.strip()][:3]
        if NOTICE_FIRST_LINE in non_empty:
            return "own_body"
    return None
