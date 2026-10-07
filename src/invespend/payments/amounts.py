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
import unicodedata

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
# RS3A-2 / RS3AF-3: any non-ZAR currency token in a text region means the figure may not be rand. The text is first
# NFKC-folded (full-width letters/digits, NBSP) and format/zero-width characters are dropped; tokens are then looked
# for (a) as whole words and (b) inside runs of single letters split by one separator ("U S D", "U.S.D"). Linear
# (alternation of literals, one pass, runs consumed once); false positives (a payee called "Euro ...") only park,
# never pay. ``US`` is matched upper-case only (the pronoun "us" is common in prose).
_FOREIGN_WORD = re.compile(
    r"(?<![^\W\d_])(?:(?-i:US)|USDT|USDC|USD|BTC|ETH|JPY|yen|CNY|rmb|yuan|INR|rupees?|MXN|pesos?|d[o\u00f3]lar(?:es)?|"
    r"dollars?|euros?|pounds?|GBP|EUR|AUD|CAD|NZD|CHF|SGD|HKD|AED)(?![^\W\d_])",
    re.IGNORECASE,
)
_FOREIGN_SYMBOL = re.compile(r"[$\u20ac\u00a3\u00a5\u20b9]")
_LETTER_RUN = re.compile(r"(?<![^\W\d_])[^\W\d_](?:[ \t.\-_/]{1,3}[^\W\d_])+")
_SPLIT_TOKENS = ("US", "USDT", "USDC", "USD", "BTC", "ETH", "JPY", "CNY", "RMB", "INR", "MXN", "GBP", "EUR", "AUD",
                 "CAD", "NZD", "CHF", "SGD", "HKD", "AED", "YEN")


def has_foreign_currency_token(text: str) -> bool:
    """True when ``text`` carries a non-ZAR currency token (code, word or symbol, also full-width, zero-width or
    space-split). Fails closed (True) on input longer than ``MAX_TYPED_CHARS``."""
    text = text or ""
    if len(text) > MAX_TYPED_CHARS:
        return True
    folded = "".join(ch for ch in unicodedata.normalize("NFKC", text) if unicodedata.category(ch) != "Cf")
    if _FOREIGN_SYMBOL.search(folded) or _FOREIGN_WORD.search(folded):
        return True
    for run in _LETTER_RUN.finditer(folded):
        letters = "".join(ch for ch in run.group() if ch.isalpha()).upper()
        if any(token in letters for token in _SPLIT_TOKENS):
            return True
    return False


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
