"""Marked-only amount candidates for v2 text regions (S6, NB5 / TR3-11 / TR3-12).

``extract._AMOUNT_RE`` takes ANY digit run, so a reference such as ``INV7781`` or
``unit 14`` would become an amount. v2 therefore never calls ``extract_from_text``
on text regions: a text candidate counts ONLY when it is

  * currency-marked: ``R`` / ``ZAR`` immediately before (word-bounded) or ``ZAR``
    immediately after the number, or
  * labelled ``amount:`` / ``amount=`` (case-insensitive).

Left boundary of a currency marker: not preceded by a letter, digit, ``-``, ``/``,
``#``, ``_`` or ``.`` (``INV-R500``, ``PO/R500``, ``Order#R500``, ``ref.R500`` are
reference-like tokens, not amounts), and only a space or tab (never a newline) may
sit between ``R``/``ZAR`` and the number.

Linear time: input longer than ``MAX_TYPED_CHARS`` (20,000 characters) FAILS CLOSED,
i.e. yields no candidates at all (the caller then reconciles "none" and parks); it is
never truncated, so a payee-relevant region can not be silently cut off. The
``ref`` look-behind inspects only the preceding 40 characters.

Right boundary of every candidate: no following letter, digit, ``-`` or ``/``, and
no ``.``/``,`` followed by a digit; a run of 6 or more digits without thousand
separators never counts; a currency-marked number directly after a ``ref`` /
``reference`` label never counts (``Ref: R20261`` is a reference, not an amount).
Digit runs inside alphanumeric tokens never count. No marked amount -> no
candidates (the caller reconciles that as "none"). ``extract.py`` is not edited;
only its private number normaliser is reused so formats stay identical.
"""
from __future__ import annotations

import re

from .extract import _PAYEE_RE, _norm_amount

MAX_TYPED_CHARS = 20_000
_REF_WINDOW = 40

_NUM = r"(?P<num>\d+(?:[.,]\d{2})|\d{1,3}(?:[ ,]\d{3})+(?:[.,]\d{2})?|\d+)"
_RIGHT = r"(?![0-9A-Za-z\-/])(?![.,][0-9])"

_PREFIXED = re.compile(
    r"(?<![A-Za-z0-9\-/#_.])(?:R|ZAR)[ \t]?" + _NUM + _RIGHT, re.IGNORECASE | re.ASCII
)
_SUFFIXED = re.compile(
    r"(?<![A-Za-z0-9\-/.,])" + _NUM + r"\s?ZAR(?![A-Za-z0-9])", re.IGNORECASE | re.ASCII
)
_LABELLED = re.compile(
    r"(?<![A-Za-z0-9])amount\s*[:=]\s*(?:(?:R|ZAR)[ \t]?)?" + _NUM + _RIGHT,
    re.IGNORECASE | re.ASCII,
)
_AFTER_REF_LABEL = re.compile(r"\bref(?:erence)?\b[\s:#=.\-]*$", re.IGNORECASE | re.ASCII)
_LONG_RUN = re.compile(r"\d{6,}")


def marked_amount_candidates(text: str) -> list[str]:
    """Normalised ``"123.45"`` strings in first-seen order, marked amounts only."""
    text = text or ""
    if len(text) > MAX_TYPED_CHARS:
        return []  # fail closed: never truncate a payee-relevant region
    found: list[tuple[int, str]] = []
    for pattern, guard_ref in ((_PREFIXED, True), (_SUFFIXED, True), (_LABELLED, False)):
        for m in pattern.finditer(text):
            num = m.group("num")
            if _LONG_RUN.match(num):
                continue
            if guard_ref and _AFTER_REF_LABEL.search(text[max(0, m.start() - _REF_WINDOW): m.start()]):
                continue
            norm = _norm_amount(num)
            if norm is not None:
                found.append((m.start(), norm))
    found.sort(key=lambda item: item[0])
    out: list[str] = []
    for _, norm in found:
        if norm not in out:
            out.append(norm)
    return out


def labelled_payee_candidates(text: str) -> list[str]:
    """Payee names ONLY from labelled ``payee:`` / ``beneficiary:`` lines (TR3-12)."""
    out: list[str] = []
    if len(text or "") > MAX_TYPED_CHARS:
        return out  # fail closed, same cap as marked_amount_candidates
    for m in _PAYEE_RE.finditer(text or ""):
        name = m.group("payee").strip()
        if name and name not in out:
            out.append(name)
    return out
