"""Build in-memory test mail the way the cycle sees it (parsed with compat32)."""
from __future__ import annotations

import email
import email.policy
from email.message import EmailMessage, Message


def make_msg(
    *,
    plain: str | None = None,
    html: str | None = None,
    subject: str = "Hello",
    headers: dict[str, str] | None = None,
    attachments: tuple[tuple[str, str, str, bytes], ...] = (),   # (filename, maintype, subtype, data)
    inline: tuple[tuple[str, str, str, bytes], ...] = (),
    rfc822: tuple[Message, ...] = (),
) -> Message:
    m = EmailMessage()
    m["From"] = "Piet <piet@example.com>"
    m["To"] = "bot@example.com"
    m["Subject"] = subject
    for k, v in (headers or {}).items():
        m[k] = v
    if plain is not None and html is not None:
        m.set_content(plain)
        m.add_alternative(html, subtype="html")
    elif html is not None:
        m.set_content(html, subtype="html")
    else:
        m.set_content(plain if plain is not None else "")
    for fn, mt, st, data in attachments:
        m.add_attachment(data, maintype=mt, subtype=st, filename=fn)
    for fn, mt, st, data in inline:
        m.add_attachment(data, maintype=mt, subtype=st, filename=fn, disposition="inline")
    for inner in rfc822:
        m.add_attachment(inner)
    return reparse(m)


def reparse(m: EmailMessage) -> Message:
    return email.message_from_bytes(m.as_bytes(), policy=email.policy.compat32)
