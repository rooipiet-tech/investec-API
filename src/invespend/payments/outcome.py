"""Payment outcome model and exceptions for the hardened write path (S3, F17/F40).

Stdlib only. Imported lazily by ``investec_client`` to avoid a circular import.

Mapping of a decoded HTTP 200 body (``parse_payment_response``). Money may have
moved on any 200, so SUCCESS is the narrowest outcome and "unknown" is never
auto-resent (a caller must treat it like ``PaymentUnknownOutcome``):

* data-level ``ErrorMessage`` that is NON-EMPTY after strip      -> ``failed`` (``None``, ``""`` and
  whitespace mean "no error", exactly as for an entry)
* data-level or per-entry ``AuthorisationRequired`` true, or an
  entry ``Status`` that says authori[sz]ation is awaited/required -> ``needs_authorisation``
* entry ``ErrorMessage`` non-empty, or an entry ``Status`` that positively says
  fail/reject/decline/deny/invalid/insufficient/error/cancel/expire -> ``failed``
* an entry without a non-empty ``PaymentReferenceNumber`` and no positive
  rejection                                                  -> ``unknown`` (``no_reference``)
* every entry has a reference and a clean/absent status       -> ``success``
* no entries (strict) / not a ``data`` object                 -> ``unknown`` (``unrecognised_shape``): after the
  POST was sent an unrecognised 200 is NOT proof that no money moved, so it keeps the reservation and is never resent

Across several entries the precedence is needs_authorisation, failed, unknown,
success. Response field names are UNVERIFIED until the G2 sandbox run.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


class PaymentNotSent(Exception):
    """POSITIVE proof that no write request left the process (pre-send token fetch failed)."""


class PaymentUnknownOutcome(Exception):
    """The write request may have been received: money may have moved. Never resent."""


class PaymentRejected(Exception):
    """Investec answered with a definite 4xx rejection (other than 429)."""

    def __init__(self, status_code: int, message: str = "") -> None:
        self.status_code = status_code
        self.message = message
        text = f"payment rejected: HTTP {status_code}"
        if message:
            text += f": {message}"
        super().__init__(text)


_ADDRESS = re.compile(r"\S+@\S+")
# 6+ digits, optionally grouped by spaces or hyphens (up to 3 between digits) (1000-2000-3000); linear:
# the group continues only while it keeps matching, no nested quantifier.
_DIGITS = re.compile(r"\d(?:[ \-]{0,3}\d){5,}")
_PRE_CUT = 2000  # bound the regex input first (provider text is untrusted), cut to _MAX last
_TOKEN = re.compile(r"[A-Za-z0-9_\-]{24,}")
# RS3A-3: credentials echoed by a provider. Each pattern is a single left-to-right pass over input already cut to
# 2000 chars with whitespace collapsed; no nested quantifiers.
_BEARER = re.compile(r"\b(bearer|basic)[ ]+\S+", re.IGNORECASE)
_SECRET_WORD = re.compile(
    r"\b(key|token|secret|password|passphrase|authorization)\b[ ]{0,3}[:=]?[ ]{0,3}\S+", re.IGNORECASE)
_SECRET_LIKE = re.compile(r"\b(?:sk|pk|tok|token|key|secret|api)[-_][A-Za-z0-9_\-]{4,}", re.IGNORECASE)
_WS = re.compile(r"\s+")
_MAX = 200
REDACTED = "[redacted]"


def sanitize_provider_message(text: object) -> str:
    """F40: the provider text made safe to store, email and audit. Never raises.

    str-coerce, collapse whitespace, drop other control characters, redact
    addresses, 6+ digit runs (also space/hyphen grouped) and token-like strings,
    truncate to 200 characters. The input is first bounded to 2000 characters so
    every regex runs on a small string.
    """
    try:
        if text is None:
            return ""
        s = str(text)[:_PRE_CUT]
        s = _WS.sub(" ", s)
        s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")
        s = _ADDRESS.sub(REDACTED, s)
        s = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", s)          # the keyword stays, the value goes
        s = _SECRET_WORD.sub(lambda m: f"{m.group(1)} {REDACTED}", s)
        s = _SECRET_LIKE.sub(REDACTED, s)
        s = _TOKEN.sub(REDACTED, s)
        s = _DIGITS.sub(REDACTED, s)
        return s.strip()[:_MAX]
    except Exception:  # pragma: no cover - defensive, the contract is "never raises"
        return ""


# Investec rejection body field names are UNVERIFIED until the G2 sandbox run:
# the first present (non-empty) value of this documented guess order wins.
PROVIDER_MESSAGE_FIELDS = ("ErrorMessage", "message", "error_description", "error")


def provider_message_from_body(body: object) -> str:
    """Sanitised provider message from a decoded JSON body ("" when none)."""
    if not isinstance(body, dict):
        return ""
    candidates = [body]
    if isinstance(body.get("data"), dict):
        candidates.append(body["data"])
    for source in candidates:
        for key in PROVIDER_MESSAGE_FIELDS:
            value = source.get(key)
            if value is not None and value != "":
                return sanitize_provider_message(value)
    return ""


@dataclass(frozen=True)
class PaymentOutcome:
    status: str            # "success" | "failed" | "needs_authorisation" | "unknown"
    reference: str | None  # PaymentReferenceNumber when present
    reason: str            # short code, never raw body
    message: str           # F40: sanitised provider text for failed/needs_authorisation, "" otherwise


def _truthy(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _non_blank(value: object) -> bool:
    return value is not None and str(value).strip() != ""


def parse_payment_response(body: dict | None, *, strict: bool = True) -> PaymentOutcome:
    """Interpret a decoded 200 body. ``strict=False`` exists for symmetry only (unused by v2).

    A 200 that is not the expected shape is ``unknown`` (never ``failed``): the POST was already sent."""
    shape_status = "unknown"
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return PaymentOutcome(shape_status, None, "unrecognised_shape", "")
    error = data.get("ErrorMessage")
    if _non_blank(error):
        return PaymentOutcome("failed", None, "error_message", sanitize_provider_message(error))
    raw_entries = data.get("TransferResponses")
    entries = [e for e in raw_entries if isinstance(e, dict)] if isinstance(raw_entries, list) else []
    if _truthy(data.get("AuthorisationRequired")):
        return PaymentOutcome("needs_authorisation", None, "authorisation_required", "")
    if not entries:
        if strict:
            return PaymentOutcome(shape_status, None, "unrecognised_shape", "")
        return PaymentOutcome("success", None, "ok", "")
    results = [_entry_outcome(e) for e in entries]
    for wanted in ("needs_authorisation", "failed", "unknown"):
        for r in results:
            if r.status == wanted:
                return r
    return results[0]


_AUTH_WORDS = re.compile(r"authori[sz]|awaiting|pending", re.IGNORECASE)
_FAIL_WORDS = re.compile(
    r"fail|reject|declin|denied|deny|invalid|insufficient|error|cancel|expire|unsuccess",
    re.IGNORECASE,
)


def _entry_outcome(entry: dict) -> PaymentOutcome:
    raw_ref = entry.get("PaymentReferenceNumber")
    ref = str(raw_ref).strip() if raw_ref is not None else ""
    status_text = entry.get("Status")
    status = status_text.strip() if isinstance(status_text, str) else ""
    msg = sanitize_provider_message(status_text) if status else ""
    if _truthy(entry.get("AuthorisationRequired")):
        return PaymentOutcome("needs_authorisation", ref or None, "authorisation_required", msg)
    error = entry.get("ErrorMessage")
    if _non_blank(error):
        return PaymentOutcome("failed", ref or None, "entry_error_message", sanitize_provider_message(error))
    if status and _FAIL_WORDS.search(status):
        return PaymentOutcome("failed", ref or None, "entry_status", msg)
    if status and _AUTH_WORDS.search(status):
        return PaymentOutcome("needs_authorisation", ref or None, "authorisation_required", msg)
    if not ref:
        return PaymentOutcome("unknown", None, "no_reference", msg)
    return PaymentOutcome("success", ref, "ok", "")
