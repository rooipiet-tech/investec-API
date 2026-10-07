"""Payment outcome model and exceptions for the hardened write path (S3, F17/F40).

Stdlib only. Imported lazily by ``investec_client`` to avoid a circular import.
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
_DIGITS = re.compile(r"\d{6,}")
_TOKEN = re.compile(r"[A-Za-z0-9_\-]{24,}")
_WS = re.compile(r"\s+")
_MAX = 200
REDACTED = "[redacted]"


def sanitize_provider_message(text: object) -> str:
    """F40: the provider text made safe to store, email and audit. Never raises.

    str-coerce, collapse whitespace, drop other control characters, redact
    addresses, 6+ digit runs and token-like strings, truncate to 200 characters.
    """
    try:
        if text is None:
            return ""
        s = str(text)
        s = _WS.sub(" ", s)
        s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")
        s = _ADDRESS.sub(REDACTED, s)
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
    status: str            # "success" | "failed" | "needs_authorisation"
    reference: str | None  # PaymentReferenceNumber when present
    reason: str            # short code, never raw body
    message: str           # F40: sanitised provider text for failed/needs_authorisation, "" otherwise


def _truthy(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def parse_payment_response(body: dict | None, *, strict: bool = True) -> PaymentOutcome:
    """Interpret a decoded 200 body. ``strict=False`` exists for symmetry only (unused by v2)."""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return PaymentOutcome("failed", None, "unrecognised_shape", "")
    error = data.get("ErrorMessage")
    if error is not None:
        return PaymentOutcome("failed", None, "error_message", sanitize_provider_message(error))
    raw_entries = data.get("TransferResponses")
    entries = [e for e in raw_entries if isinstance(e, dict)] if isinstance(raw_entries, list) else []
    if _truthy(data.get("AuthorisationRequired")):
        return PaymentOutcome("needs_authorisation", None, "authorisation_required", "")
    for entry in entries:
        if _truthy(entry.get("AuthorisationRequired")):
            ref = entry.get("PaymentReferenceNumber")
            return PaymentOutcome(
                "needs_authorisation", str(ref) if ref else None, "authorisation_required",
                sanitize_provider_message(entry.get("Status")),
            )
    if not entries:
        if strict:
            return PaymentOutcome("failed", None, "unrecognised_shape", "")
        return PaymentOutcome("success", None, "ok", "")
    ref = entries[0].get("PaymentReferenceNumber")
    return PaymentOutcome("success", str(ref) if ref else None, "ok", "")
