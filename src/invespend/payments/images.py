"""Image payload interface for v2 (S8). Pure, stdlib only, no engine, no network.

An engine (the production adapter is a later stage) may return ONLY structured
data: a mapping that ``validate_extraction`` reduces to ``ImageFields``. There is
no action/decision field, so text inside an image can never trigger a payment,
select an account or approve anything; the values are data for a human to check.
Image bytes live only in ``ImageRef.data`` (``repr=False``) and are never logged
or audited; only reason codes and counts leave this module.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import unicodedata
from collections.abc import Mapping
from decimal import Decimal
from dataclasses import dataclass, field
from typing import Protocol

from .extract import _norm_amount

SUPPORTED_MIME = ("image/jpeg", "image/png", "image/gif", "image/webp")
DEFAULT_MAX_IMAGE_BYTES = 5_000_000
MAX_FIELD_CHARS = 100
ALLOWED_KEYS = ("payee_name", "bank", "account_number", "amount", "currency", "reference")
_SAFE_NAME = re.compile(r"[a-z_]{1,32}")
_STRICT_AMOUNT = re.compile(r"\d+(?:[.,]\d{2})?|\d{1,3}(?:[ ,]\d{3})+(?:[.,]\d{2})?", re.ASCII)
_ACCOUNT_RAW = re.compile(r"[0-9 \-]{6,60}", re.ASCII)
# An image amount must be > 0 and <= MAX_AMOUNT (R 1,000,000.00): anything else is a misread or hostile value
# and is dropped (None), never guessed. The text is length-capped BEFORE any int/float conversion.
MAX_AMOUNT = 1_000_000
_MAX_AMOUNT_CHARS = 24
_MAX_INT_DIGITS = 12
_ZAR_WORDS = frozenset({"r", "zar", "rand"})


@dataclass(frozen=True)
class ImageRef:
    mime: str
    size: int
    sha256: str
    data: bytes = field(repr=False)   # never in audit/log


@dataclass(frozen=True)
class ImageFields:
    """The ONLY thing an engine may return: structured data, no action field."""

    payee_name: str | None = None
    bank: str | None = None
    account_number: str | None = None
    amount: str | None = None
    currency: str | None = None
    reference: str | None = None


class ImageExtractor(Protocol):
    def extract(self, image: ImageRef) -> Mapping[str, object] | None: ...


def sniff_mime(data: bytes) -> str | None:
    """Magic bytes, never the file name or the declared content type."""
    head = bytes(data[:12])
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def max_image_bytes() -> int:
    """PAYMENTS_MAX_IMAGE_BYTES; unset, unparsable or <= 0 means the 5 MB default."""
    raw = os.environ.get("PAYMENTS_MAX_IMAGE_BYTES", "")
    try:
        value = int(raw.strip())
    except ValueError:
        return DEFAULT_MAX_IMAGE_BYTES
    return value if value > 0 else DEFAULT_MAX_IMAGE_BYTES


def image_ref_from_bytes(data: bytes, *, max_bytes: int | None = None) -> tuple[ImageRef | None, str]:
    """(ref, "") or (None, reason) with reason in empty | oversize | unsupported."""
    if not data:
        return None, "empty"
    limit = max_image_bytes() if max_bytes is None else max_bytes
    if len(data) > limit:
        return None, "oversize"
    mime = sniff_mime(data)
    if mime is None or mime not in SUPPORTED_MIME:
        return None, "unsupported"
    blob = bytes(data)
    return ImageRef(mime, len(blob), hashlib.sha256(blob).hexdigest(), blob), ""


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_FIELD_CHARS:
        return None
    if any(unicodedata.category(ch).startswith("C") for ch in text):
        return None
    return text


def _clean_amount(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if not 0 < value <= MAX_AMOUNT:   # bound first: str() of a huge int raises ValueError
            return None
        text = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value) or not 0 < value <= MAX_AMOUNT:
            return None
        text = f"{value:.2f}"
    elif isinstance(value, str):
        if len(value) > 4 * _MAX_AMOUNT_CHARS:
            return None
        text = value.strip()
        if len(text) > _MAX_AMOUNT_CHARS:
            return None
    else:
        return None
    # strict shapes only: "100.5" would be read as 1005 by the shared normaliser
    if not _STRICT_AMOUNT.fullmatch(text):
        return None
    norm = _norm_amount(text)
    if norm is None or not Decimal(0) < Decimal(norm) <= MAX_AMOUNT:
        return None
    return norm


def _clean_account(value: object) -> str | None:
    if not isinstance(value, str) or not _ACCOUNT_RAW.fullmatch(value.strip()):
        return None
    digits = re.sub(r"[ \-]", "", value.strip())
    return digits if 6 <= len(digits) <= 20 else None


def _clean_currency(value: object) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    if text.casefold() in _ZAR_WORDS:
        return "ZAR"
    return text.upper() if text.isalpha() else text


_CLEANERS = (
    ("payee_name", _clean_text), ("bank", _clean_text), ("account_number", _clean_account),
    ("amount", _clean_amount), ("currency", _clean_currency), ("reference", _clean_text),
)


def _safe(cleaner, raw: Mapping[str, object], name: str) -> str | None:
    """validate_extraction is total: any failure while cleaning one field drops that field."""
    try:
        return cleaner(raw.get(name))
    except Exception:  # noqa: BLE001
        return None


def validate_extraction(
    raw: Mapping[str, object] | None,
) -> tuple[ImageFields | None, tuple[str, ...], int]:
    """(fields, dropped_names, dropped_count).

    Allowed keys only. EVERY other key is dropped and counted, but only keys that
    fully match ``[a-z_]{1,32}`` are returned by name; a hostile key is counted,
    never named. A bad value for an allowed key becomes None (never guessed). When
    nothing usable remains ``fields`` is None.
    """
    if not isinstance(raw, Mapping):
        return None, (), 0
    names: list[str] = []
    count = 0
    for key in raw:
        if key in ALLOWED_KEYS:
            continue
        count += 1
        if isinstance(key, str) and _SAFE_NAME.fullmatch(key) and key not in names:
            names.append(key)
    values = {name: _safe(cleaner, raw, name) for name, cleaner in _CLEANERS}
    fields = ImageFields(**values) if any(v is not None for v in values.values()) else None
    return fields, tuple(sorted(names)), count


def is_zar(fields: ImageFields | None) -> bool:
    """Payments are ZAR only; a missing currency is not ZAR (never assumed)."""
    return fields is not None and fields.currency == "ZAR"


class NullImageExtractor:
    """Default: no engine configured, images are skipped."""

    def __init__(self, note: str = "image_engine_disabled") -> None:
        self.note = note

    def extract(self, image: ImageRef) -> Mapping[str, object] | None:
        return None


class FakeImageExtractor:
    """Deterministic ``{sha256_hex: raw_dict}``; used by all tests."""

    def __init__(self, mapping: Mapping[str, Mapping[str, object]]) -> None:
        self._mapping = dict(mapping)
        self.calls: list[str] = []

    def extract(self, image: ImageRef) -> Mapping[str, object] | None:
        self.calls.append(image.sha256)
        raw = self._mapping.get(image.sha256)
        return None if raw is None else dict(raw)


@dataclass(frozen=True)
class ExtractionOutcome:
    fields: ImageFields | None
    dropped_names: tuple[str, ...] = ()
    dropped_count: int = 0
    note: str = ""   # reason code only; never engine text


def run_extractor(extractor: ImageExtractor, image: ImageRef) -> ExtractionOutcome:
    """Call the engine, validate, never raise, never carry engine text."""
    try:
        raw = extractor.extract(image)
    except Exception:  # noqa: BLE001 - an engine failure only skips the image
        return ExtractionOutcome(None, note="image_extractor_error")
    fields, names, count = validate_extraction(raw)
    if fields is None:
        note = getattr(extractor, "note", "") if raw is None else ""
        return ExtractionOutcome(None, names, count, note if isinstance(note, str) and note else "image_no_fields")
    return ExtractionOutcome(fields, names, count)


def get_extractor(settings: object) -> ImageExtractor:
    """Enabled ONLY when both the API key and the model setting are non-empty."""
    key = getattr(settings, "anthropic_api_key", None)
    model = getattr(settings, "image_extractor_model", None)
    if not (isinstance(key, str) and key.strip() and isinstance(model, str) and model.strip()):
        return NullImageExtractor()
    try:
        from .images_claude import ClaudeVisionExtractor  # type: ignore[import-not-found]
    except ImportError:
        return NullImageExtractor("image_adapter_unavailable")
    return ClaudeVisionExtractor(api_key=key, model=model)
