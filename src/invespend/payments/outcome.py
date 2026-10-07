"""Payment outcome model and exceptions for the hardened write path (S3, F17/F40).

Stdlib only. Imported lazily by ``investec_client`` to avoid a circular import.

Mapping of a decoded HTTP 200 body (``parse_payment_response``): an ALLOW-LIST design that fails toward
``needs_review``. DECISION (until the G2 sandbox run shows real Investec failure bodies): a 200 response is NEVER
``failed``. Money may have moved on any 200, so only a strict reference can make ``success``, an explicit
authorisation flag makes ``needs_authorisation``, and EVERYTHING ELSE is ``unknown`` (needs_review: reservation
kept, never resent, never released). ``failed`` (reservation released, re-instructable, message shown) is produced
ONLY by a definite HTTP 4xx ``PaymentRejected`` (see ``investec_client._post_once`` and ``execute``): 408, 429 and
5xx stay unknown. Free-text ``Status`` wording is NEVER used to infer authorisation or failure (the documented
success status is "No authorisation necessary ..."; "Processed - no errors" and "Not declined" contain fail words).

* a *strict reference* (the only thing that can make ``success``) is a non-empty STRING entry-level
  ``PaymentReferenceNumber`` that is not a placeholder. Placeholders are compared after NFKC, dropping Unicode format
  characters (Cf), casefold and stripping leading/trailing punctuation: none/null/n/a/na/nil/0/false/ok ("N/A.", "OK!").
  A *reference-like* value is ANY key (data, entry, nested dicts/lists, any depth) whose folded name contains ``ref`` with a
  present value of ANY type (text that is not blank/punctuation-only/placeholder, nonzero number, non-empty list/dict).
* an *error text* is a non-empty non-placeholder STRING ``ErrorMessage`` (data or entry level); placeholders: none/null/
  n/a/na/nil/0/false/true/ok/no error(s)/success(ful). It only decides whether the owner's needs_review email carries
  the (sanitised) provider message; it never decides the outcome.

1. body or ``data`` not a dict                           -> ``unknown`` (``unrecognised_shape``)
2. ``AuthorisationRequired`` true (bool or "true") at data or entry level -> ``needs_authorisation``
   (reservation KEPT: it may be authorised later in Investec Online and then moves money)
3. any key containing ``authori`` (any depth) with a value not clearly false, or a body beyond 200k nodes / depth 64
   -> ``unknown`` (``authorisation_unclear`` / ``body_too_large``)
4. at least one strict reference: with an error -> ``unknown`` (``ref_and_error``); several entries and not all with a
   reference -> ``unknown`` (``mixed_entries``); an entry ``Status`` EQUAL (strip/casefold, not substring) to
   failed/declined/rejected/unsuccessful/"unsuccessful payment" -> ``unknown`` (``status_conflict``); else ``success``
5. reference-like content but no strict reference -> ``unknown`` (``ref_and_error`` / ``reference_unrecognised``)
6. anything else -> ``unknown`` (``error_message`` when an error text exists, carrying the sanitised message;
   else ``no_reference`` / ``unrecognised_shape``)

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


def _loose(word: str) -> str:
    """A keyword that may be split by up to two whitespace characters between letters (a newline inside "key")."""
    return r"\s{0,2}".join(word)


# RS3AF-4 / RS3AF3-4: underscore-aware ("api_key", "client_secret", "x-api-key"): the keyword may carry a <= 20
# letter/underscore prefix and may be split by a newline/space; the keyword is kept, its value goes. Up to three filler
# tokens (":", "=", is, was, are, equals) between the keyword and the value are skipped ("password is hunter22").
_SECRET_KEYWORDS = "|".join(_loose(w) for w in ("key", "token", "secret", "password", "passwd", "pwd", "authorization",
                                                "bearer", "passphrase"))
SECRET_WORD = re.compile(
    r"(?P<lead>^|[^A-Za-z])(?P<word>[a-z_]{0,20}(?:" + _SECRET_KEYWORDS + r")s?)"
    r"(?:\s{0,3}(?:[:=]|\b(?:is|was|are|equals?)\b)){0,3}\s{0,3}\S+", re.IGNORECASE)
# linear: a JWT must start at a token boundary, so a run of "eyJeyJeyJ..." has ONE start (no restart per "eyJ")
_JWT = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_AKIA = re.compile(r"AKIA[0-9A-Z]{16}")
_VENDOR = re.compile(
    r"(?<![A-Za-z0-9])(?:(?:xox[abprs]-|xapp-|rk_live_|rk_test_|sk_live_|sk_test_|whsec_|glpat-|npm_|github_pat_)[A-Za-z0-9_\-]{4,}"
    r"|(?-i:ASIA[0-9A-Z]{16})|(?-i:SG)\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)", re.IGNORECASE)
_SECRET_LIKE = re.compile(
    r"(?<![A-Za-z0-9])(?:(?:sk|pk|tok|token|key|secret|api)[-_]|gh[pousr]_)[A-Za-z0-9_\-]{4,}", re.IGNORECASE)
_WS = re.compile(r"\s+")
_MAX = 200
REDACTED = "[redacted]"
_MIN_SECRET = 4
_MAX_SECRETS = 64


SCRUB_CUT = 5000          # callers bound untrusted text before redaction (the tail is dropped, never emitted)
_REDACT_CUT = 20_000


def fold_text(text: str) -> str:
    """NFKC fold with format characters (category Cf: zero-width, joiners, BOM, RTL marks) dropped."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text) if unicodedata.category(ch) != "Cf")


def redact_secret_words(text: str) -> str:
    """The bearer / secret-word / JWT / AKIA / vendor-prefix / token-like rules (no digit or address rules). Linear
    (input beyond 20,000 characters is dropped)."""
    text = text[:_REDACT_CUT]
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)           # the keyword stays, the value goes
    text = SECRET_WORD.sub(lambda m: f"{m.group('lead')}{m.group('word')} {REDACTED}", text)
    text = _JWT.sub(REDACTED, text)
    text = _AKIA.sub(REDACTED, text)
    text = _VENDOR.sub(REDACTED, text)
    return _SECRET_LIKE.sub(REDACTED, text)


def secret_value_pattern(values) -> re.Pattern | None:
    """One case-insensitive pattern for exact secret values; optional whitespace may sit between any two characters
    (a value split across a newline is still caught). Values shorter than 4 characters are ignored (they would
    redact ordinary text); longer than 200 are matched on their first 200 characters. Linear: no nested quantifier."""
    parts = []
    for value in list(values or ())[:_MAX_SECRETS]:
        if not isinstance(value, str):
            continue
        chars = "".join(fold_text(value).split())[:200]
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
    message: str           # F40: sanitised provider text (error_message / needs_authorisation), "" otherwise


_REF_PLACEHOLDERS = frozenset({"none", "null", "n/a", "na", "nil", "0", "false", "ok"})
_ERROR_PLACEHOLDERS = frozenset({"none", "null", "n/a", "na", "nil", "0", "false", "true", "ok", "no error", "no errors",
                                 "success", "successful"})
_FAILURE_STATUSES = frozenset({"failed", "declined", "rejected", "unsuccessful", "unsuccessful payment"})
_SHORT = 64            # no placeholder or failure status is longer than this
_FOLD_CUT = 5000       # untrusted text is cut to this before any fold; a longer value that folds to nothing is "present"
_MAX_NODES = 200_000   # body walk bounds (linear): beyond either the body is ambiguous
_MAX_DEPTH = 64


def _is_strip(ch: str) -> bool:
    return unicodedata.category(ch)[0] in "PSZC"       # punctuation, symbols, separators, controls / format


def _folded(value: str) -> str | None:
    """NFKC, Cf dropped, casefolded, stripped of leading/trailing punctuation, symbols and whitespace. ``None`` when the
    value is too long to compare cheaply and folds to nothing (treated as present by the callers)."""
    text = unicodedata.normalize("NFKC", value[:_FOLD_CUT])
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf").casefold()
    lo, hi = 0, len(text)
    while lo < hi and _is_strip(text[lo]):
        lo += 1
    while hi > lo and _is_strip(text[hi - 1]):
        hi -= 1
    text = text[lo:hi]
    return None if (not text and len(value) > _FOLD_CUT) else text


def _meaningful_text(value: str, placeholders: frozenset, *, long_blank: bool = True) -> bool:
    """``long_blank``: the answer for a value longer than the fold cut that folds to nothing (True for reference-like
    values: present is the safe reading; False for errors: a blank error is never a definite failure)."""
    folded = _folded(value)
    if folded is None:
        return long_blank
    return bool(folded) and (len(folded) > _SHORT or folded not in placeholders)


def _truthy(value: object) -> bool:
    return value is True or (isinstance(value, str) and len(value) <= _SHORT and value.strip().lower() == "true")


def _falsey(value: object) -> bool:
    if value is None or value is False or (isinstance(value, (int, float)) and value == 0):
        return True
    if isinstance(value, (list, dict)):
        return not value
    return isinstance(value, str) and (_folded(value) or "") in ("", "false", "0", "no", "none", "null", "n/a", "na", "nil")


def _reference(entry: dict) -> str | None:
    """A non-empty, non-placeholder STRING PaymentReferenceNumber (stripped), else None."""
    raw = entry.get("PaymentReferenceNumber")
    if not isinstance(raw, str) or not _meaningful_text(raw, _REF_PLACEHOLDERS):
        return None
    return raw.strip()


def _error(holder: dict) -> str | None:
    """A non-empty, non-placeholder STRING ErrorMessage, else None (non-string values are ignored)."""
    raw = holder.get("ErrorMessage")
    if not isinstance(raw, str) or not _meaningful_text(raw, _ERROR_PLACEHOLDERS, long_blank=False):
        return None
    return raw


def _value_present(value: object) -> bool:
    """A reference-like value of ANY type: non-empty non-placeholder text, nonzero number, non-empty list/dict."""
    if isinstance(value, str):
        return _meaningful_text(value, _REF_PLACEHOLDERS)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value) if isinstance(value, (list, dict)) else False


def _scan(root: object) -> tuple[bool, bool, bool]:
    """(reference_like, authorisation_like, overflow) over the whole body, recursively through dicts and lists.

    reference_like: ANY key whose folded name contains ``ref`` with a present value; authorisation_like: any key
    containing ``authori`` whose value is not clearly false; overflow: too many nodes or too deep (ambiguous)."""
    ref = auth = False
    stack = [(root, 0)]
    nodes = 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_NODES or depth > _MAX_DEPTH:
            return ref, auth, True
        if isinstance(node, dict):
            for key, value in node.items():
                name = fold_text(str(key)[:_SHORT * 4]).casefold()
                if "ref" in name and _value_present(value):
                    ref = True
                if "authori" in name and not _falsey(value):
                    auth = True
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
        elif isinstance(node, list):
            stack.extend((value, depth + 1) for value in node if isinstance(value, (dict, list)))
    return ref, auth, False


def _status_text(entry: dict) -> str:
    raw = entry.get("Status")
    return sanitize_provider_message(raw) if isinstance(raw, str) and raw.strip() else ""


def parse_payment_response(body: object, *, strict: bool = True, secrets=()) -> PaymentOutcome:
    """Interpret a decoded 200 body (module docstring for the allow-list). ``strict`` is accepted for symmetry only.

    Anything not positively recognised is ``unknown`` (never ``failed``): the POST was already sent. ``message`` is the
    sanitised provider text for ``unknown``/``error_message`` and ``needs_authorisation``."""
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
    ref_like, auth_like, overflow = _scan(data)
    if auth_like or overflow:                      # an unclear authorisation flag / unbounded body: never decide
        return PaymentOutcome("unknown", None, "authorisation_unclear" if auth_like else "body_too_large", "")
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
    if ref_like:                                   # a reference-like value we do not recognise: money may have moved
        return PaymentOutcome("unknown", None, "ref_and_error" if errors else "reference_unrecognised", "")
    if errors:                                     # a 200 is never "failed": carry the message for the owner's email
        return PaymentOutcome("unknown", None, "error_message", sanitize_provider_message(errors[0], secrets))
    return PaymentOutcome("unknown", None, "no_reference" if entries else "unrecognised_shape", "")
