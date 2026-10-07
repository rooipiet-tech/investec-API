"""Payment outcome model and exceptions for the hardened write path (S3, F17/F40).

Stdlib only. Imported lazily by ``investec_client`` to avoid a circular import.

Mapping of a decoded HTTP 200 body (``parse_payment_response``): an ALLOW-LIST design that fails toward
``needs_review``. Money may have moved on any 200, so ``failed`` (reservation released, re-instructable) is reserved
for a DEFINITE rejection and ``unknown`` is never auto-resent (a caller treats it like ``PaymentUnknownOutcome``).
Free-text ``Status`` wording is NEVER used to infer authorisation or failure (the documented success status is
"No authorisation necessary ..."; "Processed - no errors" and "Not declined" contain fail words).

* a *reference* is a non-empty STRING ``PaymentReferenceNumber`` (after strip) that is not a placeholder
  (none/null/n/a/na/nil/0/false/-/ok, case-insensitive); an *error* is a non-empty STRING ``ErrorMessage`` (data or
  entry level) that is not a placeholder (none/null/n/a/na/nil/0/false/true/ok/no error(s)/success(ful)/-); non-string
  ``ErrorMessage`` values (False, 0, [], {}, None) are ignored.

1. body or ``data`` not a dict                           -> ``unknown`` (``unrecognised_shape``)
2. ``AuthorisationRequired`` true (bool or "true") at data or entry level -> ``needs_authorisation``
   (reservation KEPT: it may be authorised later in Investec Online and then moves money)
3. at least one reference: with an error -> ``unknown`` (``ref_and_error``); several entries and not all with a
   reference -> ``unknown`` (``mixed_entries``); an entry ``Status`` EQUAL (strip/casefold, not substring) to
   failed/declined/rejected/unsuccessful/"unsuccessful payment" -> ``unknown`` (``status_conflict``); else ``success``
4. no reference: a (non-placeholder string) error -> ``failed`` (``error_message``, sanitised); else ``unknown``
   (``no_reference`` / ``unrecognised_shape``)

Response field names are UNVERIFIED until the G2 sandbox run.
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
# Credentials echoed by a provider. Each pattern is a single left-to-right pass over input already cut to 2000
# chars with whitespace collapsed; every quantifier is bounded, no nested quantifiers (linear).
_BEARER = re.compile(r"\b(bearer|basic)[ ]+\S+", re.IGNORECASE)
# RS3AF-4: underscore-aware ("api_key", "client_secret", "x-api-key"): the keyword may carry a <= 20 letter/underscore
# prefix; the keyword is kept, its value goes.
SECRET_WORD = re.compile(
    r"(?P<lead>^|[^A-Za-z])(?P<word>[a-z_]{0,20}(?:key|token|secret|passw(?:or)?d|pwd|authorization|bearer|passphrase))"
    r"\s{0,3}[:=]?\s{0,3}\S+", re.IGNORECASE)
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_AKIA = re.compile(r"AKIA[0-9A-Z]{16}")
_SECRET_LIKE = re.compile(
    r"(?<![A-Za-z0-9])(?:(?:sk|pk|tok|token|key|secret|api)[-_]|gh[pousr]_)[A-Za-z0-9_\-]{4,}", re.IGNORECASE)
_WS = re.compile(r"\s+")
_MAX = 200
REDACTED = "[redacted]"
_MIN_SECRET = 4
_MAX_SECRETS = 64


def redact_secret_words(text: str) -> str:
    """The bearer / secret-word / JWT / AKIA / token-like rules (no digit or address rules). Linear."""
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)           # the keyword stays, the value goes
    text = SECRET_WORD.sub(lambda m: f"{m.group('lead')}{m.group('word')} {REDACTED}", text)
    text = _JWT.sub(REDACTED, text)
    text = _AKIA.sub(REDACTED, text)
    return _SECRET_LIKE.sub(REDACTED, text)


def secret_value_pattern(values) -> re.Pattern | None:
    """One case-insensitive pattern for exact secret values; optional whitespace may sit between any two characters
    (a value split across a newline is still caught). Values shorter than 4 characters are ignored (they would
    redact ordinary text); longer than 200 are matched on their first 200 characters. Linear: no nested quantifier."""
    parts = []
    for value in list(values or ())[:_MAX_SECRETS]:
        if not isinstance(value, str):
            continue
        chars = "".join(unicodedata.normalize("NFKC", value).split())[:200]
        if len(chars) < _MIN_SECRET:
            continue
        parts.append(r"\s*".join(re.escape(c) for c in chars))
    if not parts:
        return None
    parts.sort(key=len, reverse=True)
    return re.compile("|".join(parts), re.IGNORECASE)


def sanitize_provider_message(text: object, secrets=()) -> str:
    """F40: the provider text made safe to store, email and audit. Never raises.

    str-coerce, NFKC fold, collapse whitespace, drop control/format characters, redact the exact configured
    ``secrets`` (any case), addresses, bearer/secret-word values, JWTs, AKIA keys, token-like strings, 6+ digit runs
    (also space/hyphen grouped) and long tokens, truncate to 200 characters. The input is first bounded to 2000
    characters so every regex runs on a small string.
    """
    try:
        if text is None:
            return ""
        s = unicodedata.normalize("NFKC", str(text)[:_PRE_CUT])
        s = _WS.sub(" ", s)
        s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")
        pattern = secret_value_pattern(secrets)
        if pattern is not None:
            s = pattern.sub(REDACTED, s)
        s = _ADDRESS.sub(REDACTED, s)
        s = redact_secret_words(s)
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


_REF_PLACEHOLDERS = frozenset({"none", "null", "n/a", "na", "nil", "0", "false", "-", "ok"})
_ERROR_PLACEHOLDERS = frozenset({"none", "null", "n/a", "na", "nil", "0", "false", "true", "ok", "no error", "no errors",
                                 "success", "successful", "-"})
_FAILURE_STATUSES = frozenset({"failed", "declined", "rejected", "unsuccessful", "unsuccessful payment"})
_SHORT = 64  # no placeholder or failure status is longer than this: longer text is never folded/compared


def _folded(value: str) -> str:
    return value.strip().casefold() if len(value) <= _SHORT + 64 else ""


def _truthy(value: object) -> bool:
    return value is True or (isinstance(value, str) and len(value) <= _SHORT and value.strip().lower() == "true")


def _reference(entry: dict) -> str | None:
    """A non-empty, non-placeholder STRING PaymentReferenceNumber (stripped), else None."""
    raw = entry.get("PaymentReferenceNumber")
    if not isinstance(raw, str):
        return None
    ref = raw.strip()
    if not ref or _folded(ref) in _REF_PLACEHOLDERS:
        return None
    return ref


def _error(holder: dict) -> str | None:
    """A non-empty, non-placeholder STRING ErrorMessage, else None (non-string values are ignored)."""
    raw = holder.get("ErrorMessage")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or _folded(text) in _ERROR_PLACEHOLDERS:
        return None
    return raw


def _status_text(entry: dict) -> str:
    raw = entry.get("Status")
    return sanitize_provider_message(raw) if isinstance(raw, str) and raw.strip() else ""


def parse_payment_response(body: object, *, strict: bool = True, secrets=()) -> PaymentOutcome:
    """Interpret a decoded 200 body (module docstring for the allow-list). ``strict`` is accepted for symmetry only.

    Anything not positively recognised is ``unknown`` (never ``failed``): the POST was already sent."""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return PaymentOutcome("unknown", None, "unrecognised_shape", "")
    raw_entries = data.get("TransferResponses")
    items = raw_entries if isinstance(raw_entries, list) else []
    entries = [e for e in items if isinstance(e, dict)]
    clean = len(entries) == len(items)             # a non-dict item beside real entries is a mixed response

    # authorisation: never inferred from Status wording, only the explicit flag
    if _truthy(data.get("AuthorisationRequired")):
        return PaymentOutcome("needs_authorisation", None, "authorisation_required", "")
    for e in entries:
        if _truthy(e.get("AuthorisationRequired")):
            return PaymentOutcome("needs_authorisation", _reference(e), "authorisation_required", _status_text(e))

    refs = [_reference(e) for e in entries]
    errors = [x for x in [_error(data)] + [_error(e) for e in entries] if x is not None]
    if any(refs):
        if errors:
            return PaymentOutcome("unknown", None, "ref_and_error", "")
        if (len(items) > 1 and not (clean and all(refs))):
            return PaymentOutcome("unknown", None, "mixed_entries", "")
        for e in entries:
            status = e.get("Status")
            if isinstance(status, str) and _folded(status) in _FAILURE_STATUSES:
                return PaymentOutcome("unknown", None, "status_conflict", "")
        return PaymentOutcome("success", next(r for r in refs if r), "ok", "")
    if errors:
        return PaymentOutcome("failed", None, "error_message", sanitize_provider_message(errors[0], secrets))
    return PaymentOutcome("unknown", None, "no_reference" if entries else "unrecognised_shape", "")
