"""Text view and payload gathering for v2 mail (S7). Pure, stdlib only.

Two functions with DIFFERENT safety contracts:

* ``text_view(msg)`` reads ONLY ``text/plain`` and ``text/html`` leaf parts. It never
  opens an attachment, never unwraps ``message/rfc822`` (such a part is an indicator
  by content type only), never touches an image part, and never calls ``extract`` or an
  image extractor. It is safe to run on UNAUTHENTICATED mail and is size capped.
* ``gather_payloads(msg, view)`` parses attachments and collects images. It must only be
  called after the sender is authenticated, the trigger is ok and the age gate passed.

Typed text is the text BEFORE the first quote/forward boundary in document order; every
boundary is non-typed regardless of nesting (no depth counter). When any reply/forward
indicator exists but no boundary can be located, typed text is "" (fail closed).
"""
from __future__ import annotations

import email.message
import re
from dataclasses import dataclass
from email.header import decode_header, make_header
from html.parser import HTMLParser

from . import extract
from .images import ImageRef, image_ref_from_bytes
from .loopguard import SUBJECT_REPLY_PREFIXES

MAX_TEXT_BYTES = 1_000_000          # per text part; a larger part is truncated, never raises
MAX_PARTS = 200                     # MIME parts visited per message
MAX_MIME_DEPTH = 20
MAX_HTML_STACK = 500
MAX_LINE = 2000                     # a line is cut to this before any marker regex
MAX_HEAD_LINE = 1000
MAX_ATTACHMENTS = 20
MAX_IMAGES = 10

_RFC822 = ("message/rfc822", "message/global")
_TEXT_TYPES = ("text/plain", "text/html")
_ATTACHMENT_EXTS = (".pdf", ".xlsx", ".xls", ".csv")
_ATTACHMENT_CTYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "text/csv": ".csv",
}
_SIGNATURE_CTYPES = ("application/pgp-signature", "application/pkcs7-signature", "application/x-pkcs7-signature")


# --------------------------------------------------------------------- types
@dataclass(frozen=True)
class TextView:                      # from text_view(); safe on UNAUTHENTICATED mail
    typed_text: str
    forwarded_text: str
    quoted_text: str
    raw_head_lines: tuple[str, ...]  # first (up to) 3 non-empty RAW body lines, before any split
    confident: bool
    indicators: tuple[str, ...]


@dataclass(frozen=True)
class Region:
    kind: str                        # "typed" | "forwarded" | "quoted" | "attachment" | "image" | "subject"
    text: str
    source: str                      # filename / "body" / "rfc822:N"; audit provenance only


@dataclass(frozen=True)
class Payloads:                      # from gather_payloads(); ONLY after auth ok + trigger ok + age gate
    regions: tuple[Region, ...]
    attachments: tuple[tuple[str, bytes], ...]
    images: tuple[ImageRef, ...]
    extractions: tuple[tuple[str, extract.ExtractResult], ...] = ()   # per attachment, same order
    notes: tuple[str, ...] = ()      # reason codes only (never names, never bytes)


# ------------------------------------------------------------- MIME walking
def _walk(root: email.message.Message):
    """Yield ("leaf"|"rfc822"|"limit", part). NEVER descends into message/rfc822."""
    stack = [(root, 0)]
    count = 0
    while stack:
        part, depth = stack.pop()
        count += 1
        if count > MAX_PARTS:
            yield "limit", part
            return
        try:
            ctype = part.get_content_type()
        except Exception:  # noqa: BLE001 - hostile headers must not raise
            ctype = "text/plain"
        if ctype in _RFC822:
            yield "rfc822", part
            continue
        if part.is_multipart():
            if depth >= MAX_MIME_DEPTH:
                yield "limit", part
                continue
            children = part.get_payload()
            if isinstance(children, list):
                for child in reversed(children):
                    stack.append((child, depth + 1))
            continue
        yield "leaf", part


def _filename(part: email.message.Message) -> str:
    try:
        raw = part.get_filename()
    except Exception:  # noqa: BLE001
        return ""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(str(raw))))
    except Exception:  # noqa: BLE001
        return str(raw)


def _is_attachment(part: email.message.Message) -> bool:
    try:
        disposition = part.get_content_disposition()
    except Exception:  # noqa: BLE001
        disposition = None
    return disposition == "attachment" or bool(_filename(part))


def _decoded_bytes(part: email.message.Message) -> bytes | None:
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001
        return None
    return bytes(payload) if isinstance(payload, (bytes, bytearray)) else None


def _decode_text(part: email.message.Message) -> str | None:
    data = _decoded_bytes(part)
    if data is None:
        return None
    data = data[:MAX_TEXT_BYTES]
    charset = part.get_content_charset() or "utf-8"
    try:
        text = data.decode(charset, errors="replace")
    except (LookupError, ValueError):
        text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _collect_text(msg: email.message.Message) -> tuple[list[str], list[str], bool, bool]:
    """(plain parts, html parts, saw_rfc822, hit_limit); text/plain|html leaves only."""
    plain: list[str] = []
    html: list[str] = []
    rfc822 = limit = False
    for kind, part in _walk(msg):
        if kind == "rfc822":
            rfc822 = True
        elif kind == "limit":
            limit = True
        else:
            ctype = part.get_content_type()
            if ctype in _TEXT_TYPES and not _is_attachment(part):
                text = _decode_text(part)
                if text is not None:
                    (plain if ctype == "text/plain" else html).append(text)
    return plain, html, rfc822, limit


# ---------------------------------------------------------------- HTML scan
_WS = re.compile(r"\s+")
_VOID = frozenset("area base br col embed hr img input link meta source track wbr".split())
_BLOCK = frozenset(
    "p div br li tr h1 h2 h3 h4 h5 h6 ul ol table hr blockquote pre section article header footer "
    "dl dt dd center form address tbody thead".split()
)
_SKIP_TAGS = frozenset({"script", "style", "head", "title", "template"})
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|mso-hide\s*:\s*all", re.IGNORECASE)
_CLASS_KINDS = {
    "gmail_quote": ("html_gmail_quote", False),
    "gmail_attr": ("html_gmail_attr", False),
    "divrplyfwdmsg": ("html_divRplyFwdMsg", True),
    "moz-forward-container": ("html_moz_forward_container", True),
    "moz-cite-prefix": ("html_moz_cite_prefix", False),
    "yahoo_quoted": ("html_yahoo_quoted", False),
    "protonmail_quote": ("html_protonmail_quote", False),
}


def _container_kind(tag: str, attrs) -> tuple[str, bool] | None:
    if tag == "blockquote":
        return "html_blockquote", False
    for name, value in attrs:
        if value is None:
            continue
        lname = name.lower()
        if lname == "class":
            for token in value[:500].lower().split():
                if token in _CLASS_KINDS:
                    return _CLASS_KINDS[token]
        elif lname == "id":
            token = value.strip().lower()
            if token in _CLASS_KINDS:
                return _CLASS_KINDS[token]
        elif lname == "type" and value.strip().lower() == "cite":
            return "html_type_cite", False
    return None


class _Scanner(HTMLParser):
    """One linear pass: visible text, the first quote/forward boundary, indicator codes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.length = 0
        self._last_nl = True
        self.boundary_pos: int | None = None
        self.boundary_forward = False
        self.codes: list[str] = []
        self._stack: list[tuple[str, bool]] = []
        self._counts: dict[str, int] = {}
        self._skip = 0

    def _code(self, code: str) -> None:
        if code not in self.codes:
            self.codes.append(code)

    def _nl(self) -> None:
        if not self._last_nl:
            self.out.append("\n")
            self.length += 1
            self._last_nl = True

    def _is_hidden(self, tag: str, attrs) -> bool:
        if tag in _SKIP_TAGS:
            return True
        for name, value in attrs:
            lname = name.lower()
            if lname == "hidden":
                return True
            if lname == "style" and value and _HIDDEN_STYLE.search(value[:1000]):
                return True
        return False

    def handle_starttag(self, tag, attrs):
        kind = _container_kind(tag, attrs) if tag not in _VOID else None
        if kind is not None:
            self._code(kind[0])
            if self.boundary_pos is None:
                self.boundary_pos = self.length
                self.boundary_forward = kind[1]
        if tag in _VOID:
            if tag in _BLOCK:
                self._nl()
            return
        if tag in _BLOCK:
            self._nl()
        elif tag in ("td", "th") and not self._skip and not self._last_nl:
            self.out.append(" ")
            self.length += 1
        hidden = self._is_hidden(tag, attrs)
        if len(self._stack) < MAX_HTML_STACK:
            self._stack.append((tag, hidden))
            self._counts[tag] = self._counts.get(tag, 0) + 1
            if hidden:
                self._skip += 1

    def handle_endtag(self, tag):
        if tag == "blockquote" and self.boundary_pos is None:
            # no container is open before the first boundary, so this end tag is unmatched
            self._code("html_stray_blockquote_end")
            self.boundary_pos = self.length
        if tag == "br":
            self._nl()
            return
        if self._counts.get(tag, 0) > 0:
            while self._stack:
                name, hidden = self._stack.pop()
                self._counts[name] -= 1
                if hidden:
                    self._skip -= 1
                if name == tag:
                    break
        if tag in _BLOCK:
            self._nl()

    def handle_data(self, data):
        if self._skip:
            return
        text = _WS.sub(" ", data)
        if self._last_nl:
            text = text.lstrip(" ")
        if not text:
            return
        self.out.append(text)
        self.length += len(text)
        self._last_nl = text.endswith("\n")

    def text(self) -> str:
        return "".join(self.out)


def _scan_html(html: str) -> _Scanner:
    scanner = _Scanner()
    try:
        scanner.feed(html[:MAX_TEXT_BYTES])
        scanner.close()
    except Exception:  # noqa: BLE001 - a hostile document never raises out of text_view
        pass
    return scanner


def _tidy(text: str) -> str:
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def html_to_text(html: str) -> str:
    """stdlib html.parser; drops script/style/comments/hidden elements."""
    return _tidy(_scan_html(html or "").text())


# ------------------------------------------------------------ plain markers
_CANDIDATE = re.compile(
    r"^[ \t ]*(?:>|-{2,}|begin forwarded|(?:on|op|le|el|am|den|il)[ \t]|(?:from|van|von|de|da|od)[ \t]*:)",
    re.IGNORECASE | re.MULTILINE,
)
_SEPARATOR = re.compile(
    r"[ \t]*-{2,}[ \t]*(?P<what>original message|forwarded message|oorspronkelijk bericht|origineel bericht|"
    r"oorspronklike boodskap|ursprüngliche nachricht|weitergeleitete nachricht|message d'origine|"
    r"message transféré|message transfere)[ \t]*-{2,}[ \t]*",
    re.IGNORECASE,
)
_BEGIN_FWD = re.compile(r"[ \t]*begin forwarded message[ \t]*:?[ \t]*", re.IGNORECASE)
_FROM_LINE = re.compile(r"[ \t]*(?:from|van|von|de|da|od)[ \t]*:", re.IGNORECASE)
_HEADER_KEY = re.compile(
    r"[ \t]*(?:sent|date|verzonden|datum|gesendet|envoyé|enviado|to|aan|an|subject|onderwerp|betreff|cc)[ \t]*:",
    re.IGNORECASE,
)
_SENT_INLINE = re.compile(r"\b(?:sent|date|verzonden|datum|gesendet)[ \t]*:", re.IGNORECASE)
_WROTE_ENDINGS = ("wrote:", "wrote :", "schreef:", "schrieb:", "a écrit :", "a écrit:", "escribió:", "skrev:")


def _lines_from(text: str, start: int, count: int) -> list[str]:
    lines: list[str] = []
    pos = start
    while len(lines) < count and pos <= len(text):
        end = text.find("\n", pos)
        end = len(text) if end < 0 else end
        lines.append(text[pos:min(end, pos + MAX_LINE)])
        pos = end + 1
        if end >= len(text):
            break
    return lines


def _marker_at(text: str, start: int, first: str) -> tuple[str, bool] | None:
    stripped = first.lstrip(" \t ")
    if stripped.startswith(">"):
        return "marker_quote_line", False
    sep = _SEPARATOR.fullmatch(first)
    if sep:
        kind = sep.group("what").lower()
        if "forward" in kind or "transf" in kind or "weitergeleitete" in kind:
            return "marker_forwarded", True
        return "marker_original_message", False
    if _BEGIN_FWD.fullmatch(first):
        return "marker_forwarded", True
    if _FROM_LINE.match(first):
        if _SENT_INLINE.search(first):
            return "marker_header_block", False
        for nxt in _lines_from(text, start, 7)[1:]:
            if _HEADER_KEY.match(nxt):
                return "marker_header_block", False
        return None
    lowered = first.lstrip().lower()
    if lowered[:3] in ("on ", "op ", "le ", "el ", "am ", "il ") or lowered.startswith("den "):
        joined = ""
        for line in _lines_from(text, start, 3):
            joined = (joined + " " + line.strip()).strip()
            if joined.lower().endswith(_WROTE_ENDINGS):
                return "marker_wrote", False
    return None


def _find_marker(text: str) -> tuple[int, str, bool] | None:
    """(offset of the first boundary line, code, forward) in document order."""
    for m in _CANDIDATE.finditer(text):
        start = m.start()
        end = text.find("\n", start)
        first = text[start:(len(text) if end < 0 else end)][:MAX_LINE]
        found = _marker_at(text, start, first)
        if found is not None:
            return start, found[0], found[1]
    return None


# ---------------------------------------------------------------- splitting
@dataclass
class _Split:
    typed: str
    rest: str
    full: str
    boundary: bool
    forward: bool
    codes: list[str]


def _split_plain(text: str) -> _Split:
    marker = _find_marker(text)
    if marker is None:
        full = _tidy(text)
        return _Split(full, "", full, False, False, [])
    start, code, forward = marker
    return _Split(_tidy(text[:start]), _tidy(text[start:]), _tidy(text), True, forward, [code])


def _split_html(html: str) -> _Split:
    scanner = _scan_html(html)
    text = scanner.text()
    pos = scanner.boundary_pos
    codes = list(scanner.codes)
    forward = scanner.boundary_forward
    marker = _find_marker(text[:pos] if pos is not None else text)
    if marker is not None:
        pos, code, forward = marker
        codes.append(code)
    if pos is None:
        full = _tidy(text)
        return _Split(full, "", full, False, False, codes)
    return _Split(_tidy(text[:pos]), _tidy(text[pos:]), _tidy(text), True, forward, codes)


def _dedupe(items) -> tuple[str, ...]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return tuple(seen)


def split_typed_and_quoted(
    text: str, html: str | None, indicators: tuple[str, ...]
) -> tuple[str, str, str, bool]:
    """(typed, forwarded, quoted, confident). HTML wins when given; fails closed."""
    split = _split_html(html) if html is not None else _split_plain(text or "")
    if indicators and not split.boundary:
        return "", "", split.full, False
    forwarded, quoted = (split.rest, "") if split.forward else ("", split.rest)
    return split.typed, forwarded, quoted, True


def _header_indicators(msg: email.message.Message, rfc822: bool, limit: bool) -> list[str]:
    codes: list[str] = []
    subject = str(msg.get("Subject") or "").strip().lower()
    if any(subject.startswith(p) for p in SUBJECT_REPLY_PREFIXES):
        codes.append("subject_prefix")
    if str(msg.get("In-Reply-To") or "").strip():
        codes.append("in_reply_to")
    if str(msg.get("References") or "").strip():
        codes.append("references")
    if rfc822:
        codes.append("message_rfc822")
    if limit:
        codes.append("mime_limit")
    return codes


def forward_or_reply_indicators(msg: email.message.Message, html: str | None) -> tuple[str, ...]:
    """Reason codes, empty = none. Headers, content types and HTML containers only."""
    _, _, rfc822, limit = _collect_text(msg)
    codes = _header_indicators(msg, rfc822, limit)
    if html is not None:
        codes.extend(_scan_html(html).codes)
    return _dedupe(codes)


def _head_lines(raw_text: str) -> tuple[str, ...]:
    lines: list[str] = []
    for line in raw_text.split("\n"):
        stripped = line.strip()
        if stripped:
            lines.append(stripped[:MAX_HEAD_LINE])
            if len(lines) == 3:
                break
    return tuple(lines)


def text_view(msg: email.message.Message) -> TextView:
    """Typed/forwarded/quoted text from text/plain and text/html leaves ONLY."""
    plain_parts, html_parts, rfc822, limit = _collect_text(msg)
    plain = "\n".join(plain_parts)[:MAX_TEXT_BYTES] if plain_parts else None
    html = "\n".join(html_parts)[:MAX_TEXT_BYTES] if html_parts else None
    splits: list[_Split] = []
    if html is not None:
        splits.append(_split_html(html))
    if plain is not None:
        splits.append(_split_plain(plain))
    indicators = _dedupe(_header_indicators(msg, rfc822, limit) + [c for s in splits for c in s.codes])
    if plain is not None:
        raw_text = plain
    elif splits:
        raw_text = splits[0].full
    else:
        raw_text = ""
    head = _head_lines(raw_text)
    if not splits:
        return TextView("", "", "", head, not indicators, indicators)
    if indicators and any(not s.boundary for s in splits):
        full = (splits[-1] if plain is not None else splits[0]).full
        return TextView("", "", full, head, False, indicators)
    chosen = min(splits, key=lambda s: len(s.typed))
    forwarded, quoted = (chosen.rest, "") if chosen.forward else ("", chosen.rest)
    return TextView(chosen.typed, forwarded, quoted, head, True, indicators)


# ------------------------------------------------------------------ payloads
def _ext(name: str) -> str:
    lowered = name.lower()
    return lowered[lowered.rfind("."):] if "." in lowered else ""


def _render_attachment(result: extract.ExtractResult) -> str:
    if result.status != "ok" or not result.amount:
        return ""
    lines = [f"amount: {result.currency or 'ZAR'} {result.amount}"]
    if result.payee:
        lines.append(f"payee: {result.payee}")
    return "\n".join(lines)


def _body_text_of(msg: email.message.Message) -> str:
    plain, html, _, _ = _collect_text(msg)
    if plain:
        return _tidy("\n".join(plain)[:MAX_TEXT_BYTES])
    if html:
        return html_to_text("\n".join(html)[:MAX_TEXT_BYTES])
    return ""


def gather_payloads(msg: email.message.Message, view: TextView, *, max_depth: int = 2) -> Payloads:
    """Attachments (pdf/xlsx/csv), images and unwrapped message/rfc822 text.

    ONLY call after authenticate_sender ok AND parse_trigger ok AND the age gate. Typed,
    forwarded and quoted regions come from ``view`` (no re-parse). Inner From/headers of an
    unwrapped message are never read.
    """
    regions: list[Region] = []
    if view.typed_text:
        regions.append(Region("typed", view.typed_text, "body"))
    if view.forwarded_text:
        regions.append(Region("forwarded", view.forwarded_text, "body"))
    if view.quoted_text:
        regions.append(Region("quoted", view.quoted_text, "body"))
    subject = str(msg.get("Subject") or "").strip()
    if subject:
        regions.append(Region("subject", subject, "subject"))   # audit only, never searched

    attachments: list[tuple[str, bytes]] = []
    extractions: list[tuple[str, extract.ExtractResult]] = []
    images: list[ImageRef] = []
    notes: list[str] = []
    rfc822_count = 0

    def note(code: str) -> None:
        if code not in notes:
            notes.append(code)

    stack: list[tuple[email.message.Message, int]] = [(msg, 0)]
    visited = 0
    while stack:
        root, depth = stack.pop()
        for kind, part in _walk(root):
            visited += 1
            if visited > MAX_PARTS:
                note("part_limit")
                stack.clear()
                break
            if kind == "limit":
                note("part_limit")
                continue
            if kind == "rfc822":
                if depth + 1 > max_depth:
                    note("rfc822_depth_exceeded")
                    continue
                payload = part.get_payload()
                inner = payload[0] if isinstance(payload, list) and payload else None
                if inner is None:
                    continue
                rfc822_count += 1
                text = _body_text_of(inner)
                if text:
                    regions.append(Region("forwarded", text, f"rfc822:{rfc822_count}"))
                stack.append((inner, depth + 1))
                continue
            ctype = part.get_content_type()
            if ctype in _TEXT_TYPES and not _is_attachment(part):
                continue                      # body text: already in the view
            if ctype in _SIGNATURE_CTYPES:
                continue
            name = _filename(part)
            if ctype.startswith("image/") or _ext(name) in extract._IMAGE_EXTS:
                data = _decoded_bytes(part) or b""
                ref, reason = image_ref_from_bytes(data)
                if ref is None:
                    note(f"image_skipped_{reason}")
                elif len(images) >= MAX_IMAGES:
                    note("images_limit")
                else:
                    images.append(ref)
                continue
            ext = _ext(name) if _ext(name) in _ATTACHMENT_EXTS else _ATTACHMENT_CTYPES.get(ctype, "")
            if not ext:
                note("attachment_skipped_unsupported")
                continue
            if len(attachments) >= MAX_ATTACHMENTS:
                note("attachments_limit")
                continue
            data = _decoded_bytes(part)
            if data is None:
                note("attachment_skipped_undecodable")
                continue
            fname = name if _ext(name) in _ATTACHMENT_EXTS else (name or "attachment") + ext
            try:
                result = extract.extract_attachment(fname, data)
            except Exception:  # noqa: BLE001 - a parser failure parks the item, never crashes
                result = extract.ExtractResult("needs_review", reason="attachment extractor error")
            attachments.append((fname, data))
            extractions.append((fname, result))
            regions.append(Region("attachment", _render_attachment(result), fname))
    return Payloads(tuple(regions), tuple(attachments), tuple(images), tuple(extractions), tuple(notes))
