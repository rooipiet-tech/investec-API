"""The ``pay NNN`` trigger (S6). Pure, stdlib only; v2 grammar (not the legacy one).

``NNN`` are the last three digits of the SOURCE account to debit: a weak
selector, never an authentication factor. The input is the typed region ONLY
(produced by the text view); never quoted/forwarded text, attachments, images
or the subject.

Grammar (binding):
  * the separator between ``pay`` and the digits is ``[ \\t<NBSP>]?`` (NOT ``\\s?``,
    so ``pay\\n123`` is ``none``); digits are ASCII ``[0-9]`` only;
  * a following letter or digit rejects; a following ``.`` or ``,`` rejects ONLY
    when it is immediately followed by a letter/digit, so sentence punctuation is
    fine (``Please pay 123.`` ok, ``pay 123,000`` and ``pay 123.45`` none);
  * ``pay`` must not be preceded by a letter/digit (``repay 123`` is none);
  * two distinct values -> ``ambiguous``; the same value twice -> ok;
  * ``pay 12`` / ``pay 1234`` are ``malformed`` (never guessed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_RIGHT = r"(?![0-9A-Za-z])(?![.,][0-9A-Za-z])"
_SEP = "[ \t ]?"
PAY_TRIGGER_RE = re.compile(
    r"(?<![A-Za-z0-9])pay" + _SEP + r"([0-9]{3})" + _RIGHT, re.IGNORECASE | re.ASCII
)
_MALFORMED_RE = re.compile(
    r"(?<![A-Za-z0-9])pay" + _SEP + r"(?:[0-9]{1,2}|[0-9]{4,})" + _RIGHT,
    re.IGNORECASE | re.ASCII,
)


@dataclass(frozen=True)
class TriggerResult:
    status: str                  # "ok" | "none" | "malformed" | "ambiguous"
    last3: str | None


def parse_trigger(typed_text: str) -> TriggerResult:
    text = typed_text or ""
    values = []
    for m in PAY_TRIGGER_RE.finditer(text):
        if m.group(1) not in values:
            values.append(m.group(1))
    malformed = _MALFORMED_RE.search(text) is not None
    if len(values) > 1:
        return TriggerResult("ambiguous", None)
    if values and malformed:
        # a valid trigger next to a malformed one: never guess which was meant
        return TriggerResult("ambiguous", None)
    if values:
        return TriggerResult("ok", values[0])
    if malformed:
        return TriggerResult("malformed", None)
    return TriggerResult("none", None)


def strip_trigger(typed_text: str) -> str:
    """Remove the trigger and malformed-trigger spans, replacing each with one
    space, so the trigger digits can never become an amount candidate."""
    text = PAY_TRIGGER_RE.sub(" ", typed_text or "")
    return _MALFORMED_RE.sub(" ", text)
