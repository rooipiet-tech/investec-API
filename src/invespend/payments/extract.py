"""Attachment / body extraction (F5, F11, F18).

Produces a single unambiguous payment candidate from clean input, and fails
CLOSED on any ambiguity:
  * multiple distinct amounts            -> needs_review
  * multiple distinct payees             -> needs_review
  * a currency that is not the expected  -> needs_review
  * a corrupt / unreadable PDF           -> needs_review
  * an image attachment                  -> skipped (OCR out of scope, NO cloud
    OCR, NO network egress) (F11)

``status`` is one of: ``ok`` | ``needs_review`` | ``skipped``. Only ``ok`` may
proceed to pending; everything else is parked and never executes.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass

# Amount like "R 1 234.56", "1234.56", "ZAR 100", "100,00".
# Order matters: try the plain run-of-digits-with-decimals form FIRST so
# "1234.56" is not split into "1 234" + "4.56" by the grouped alternative.
_AMOUNT_RE = re.compile(
    r"(?:(?P<cur>R|ZAR|USD|EUR|GBP)\s*)?"
    r"(?P<num>\d+(?:[.,]\d{2})|\d{1,3}(?:[ ,]\d{3})+(?:[.,]\d{2})?|\d+)",
    re.IGNORECASE,
)
# Payee captured up to end-of-line or the start of a currency/amount token, so a
# single "Pay: Acme  R 100" line still yields a clean payee.
_PAYEE_RE = re.compile(
    r"(?:payee|beneficiary)\s*[:=]\s*(?P<payee>[^\n\r]+?)\s*(?:(?:R|ZAR|USD|EUR|GBP)\s*\d|$)",
    re.IGNORECASE | re.MULTILINE,
)
_CURRENCY_WORDS = {"R": "ZAR", "ZAR": "ZAR", "USD": "USD", "EUR": "EUR", "GBP": "GBP"}

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp", ".heic"}

# Hard cap on attachment payload size. Anything larger fails CLOSED to
# needs_review WITHOUT being handed to pypdf/pandas (decompression-bomb / OOM
# defence — never trust an oversized payload to a parser) (F11/F18).
MAX_ATTACHMENT_BYTES = 10_000_000


@dataclass(frozen=True)
class ExtractResult:
    status: str  # "ok" | "needs_review" | "skipped"
    amount: str | None = None
    currency: str | None = None
    payee: str | None = None
    reason: str = ""


def _norm_amount(raw: str) -> str | None:
    s = raw.strip().replace(" ", "")
    # Treat a trailing ",dd" or ".dd" as decimals; strip thousands separators.
    if re.search(r"[.,]\d{2}$", s):
        sep = s[-3]
        intpart = s[:-3].replace(",", "").replace(".", "")
        dec = s[-2:]
        s = f"{intpart}.{dec}"
    else:
        s = s.replace(",", "").replace(".", "")
    if not re.fullmatch(r"\d+(?:\.\d{2})?", s):
        return None
    return f"{float(s):.2f}"


def extract_from_text(text: str, expected_currency: str = "ZAR") -> ExtractResult:
    """Parse a plaintext body. Ambiguity -> needs_review (fail-closed)."""
    if not text or not text.strip():
        return ExtractResult("needs_review", reason="empty body")

    amounts: list[str] = []
    currencies: set[str] = set()
    for m in _AMOUNT_RE.finditer(text):
        norm = _norm_amount(m.group("num"))
        if norm is None:
            continue
        cur_raw = (m.group("cur") or "").upper()
        if cur_raw:
            currencies.add(_CURRENCY_WORDS.get(cur_raw, cur_raw))
        if norm not in amounts:
            amounts.append(norm)

    payees = []
    for m in _PAYEE_RE.finditer(text):
        p = m.group("payee").strip()
        if p and p not in payees:
            payees.append(p)

    if len(amounts) == 0:
        return ExtractResult("needs_review", reason="no amount found")
    if len(amounts) > 1:
        return ExtractResult("needs_review", reason="multiple amounts")
    if len(payees) > 1:
        return ExtractResult("needs_review", reason="multiple payees")

    currency = expected_currency
    if currencies:
        if len(currencies) > 1:
            return ExtractResult("needs_review", reason="multiple currencies")
        found = next(iter(currencies))
        if found != expected_currency:
            return ExtractResult(
                "needs_review",
                reason=f"currency mismatch: {found} != {expected_currency}",
            )
        currency = found

    payee = payees[0] if payees else None
    return ExtractResult("ok", amount=amounts[0], currency=currency, payee=payee)


def extract_from_csv(data: bytes | str, expected_currency: str = "ZAR") -> ExtractResult:
    """Flatten CSV cells to text and reuse the deterministic text extractor."""
    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return ExtractResult("needs_review", reason="undecodable csv")
    else:
        text = data
    # Preserve key/value structure: "Payee,Acme" -> "Payee: Acme" so the label
    # regexes still fire after flattening.
    lines = []
    for raw in text.splitlines():
        cells = re.split(r"[;,]", raw)
        if len(cells) >= 2 and re.fullmatch(r"\s*(payee|beneficiary|amount)\s*", cells[0], re.IGNORECASE):
            lines.append(f"{cells[0].strip()}: {' '.join(c.strip() for c in cells[1:])}")
        else:
            lines.append(" ".join(c.strip() for c in cells))
    return extract_from_text("\n".join(lines), expected_currency=expected_currency)


def extract_from_xlsx(data: bytes, expected_currency: str = "ZAR") -> ExtractResult:
    """Read an xlsx via pandas; flatten to text. Corrupt -> needs_review."""
    try:
        import pandas as pd  # local import: pandas may be absent in minimal envs
    except Exception:  # noqa: BLE001
        return ExtractResult("needs_review", reason="xlsx reader unavailable")
    try:
        frames = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None)
    except Exception:  # noqa: BLE001 — corrupt workbook -> fail closed
        return ExtractResult("needs_review", reason="corrupt xlsx")
    cells: list[str] = []
    for frame in frames.values():
        for value in frame.to_numpy().ravel().tolist():
            if value is not None and str(value).strip() and str(value) != "nan":
                cells.append(str(value))
    return extract_from_text(" ".join(cells), expected_currency=expected_currency)


def extract_from_pdf(data: bytes, expected_currency: str = "ZAR") -> ExtractResult:
    """Read a PDF via pypdf; corrupt/unreadable -> needs_review (fail-closed)."""
    try:
        import pypdf  # local import: keep dep isolated
    except BaseException:  # noqa: BLE001 — import/env failure -> fail closed
        return ExtractResult("needs_review", reason="pdf reader unavailable")
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except BaseException:  # noqa: BLE001 — corrupt pdf -> fail closed
        return ExtractResult("needs_review", reason="corrupt pdf")
    if not text.strip():
        return ExtractResult("needs_review", reason="empty pdf text")
    return extract_from_text(text, expected_currency=expected_currency)


def extract_from_image(filename: str = "") -> ExtractResult:
    """Image attachments are out of scope: graceful skip, NO cloud OCR, NO egress."""
    return ExtractResult("skipped", reason="image attachment (OCR out of scope)")


def extract_attachment(
    filename: str, data: bytes, expected_currency: str = "ZAR"
) -> ExtractResult:
    """Dispatch by extension. Images skip with no network call (F11)."""
    name = (filename or "").lower()
    ext = name[name.rfind(".") :] if "." in name else ""
    if ext in _IMAGE_EXTS:
        return extract_from_image(filename)
    # Size guard: never hand an oversized payload to pypdf/pandas (fail-closed).
    if isinstance(data, (bytes, bytearray)) and len(data) > MAX_ATTACHMENT_BYTES:
        return ExtractResult("needs_review", reason="attachment too large")
    if ext == ".pdf":
        return extract_from_pdf(data, expected_currency=expected_currency)
    if ext in (".xlsx", ".xls"):
        return extract_from_xlsx(data, expected_currency=expected_currency)
    if ext == ".csv":
        return extract_from_csv(data, expected_currency=expected_currency)
    # Unknown type: try as text, fail closed if undecodable.
    try:
        return extract_from_text(
            data.decode("utf-8") if isinstance(data, bytes) else str(data),
            expected_currency=expected_currency,
        )
    except UnicodeDecodeError:
        return ExtractResult("skipped", reason="unsupported binary attachment")
