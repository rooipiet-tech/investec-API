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
# RS3A-2 / RS3AF-3 / RS3AF3-3: any non-ZAR currency token in a text region means the figure may not be rand (a DENY-list
# kept deliberately: a rand figure may be followed by ordinary words). The text is first NFKC-folded (full-width
# letters/digits, NBSP), format/zero-width characters are dropped and common Cyrillic/Greek look-alike letters are
# mapped to Latin. Then, in one linear pass each:
#   (a) any currency SYMBOL (Unicode category Sc: $ euro pound yen rupee rouble won shekel lira naira bitcoin ...);
#   (b) curated codes / words, case-insensitive whole words (USD, EUR, GBP, ..., dollars, euros, kroner, rouble, ...);
#   (c) ANY other ISO 4217 alphabetic code (``ISO_4217_CODES``, every active code except ZAR) as an UPPER-CASE
#       standalone 3-letter token (``Amount: R100 SEK``, ``SEK100``, ``100SEK``) or, lower-case, glued to a digit
#       (``100sek``). Lower-case words are NOT matched (``try``, ``all``, ``top``, ``sun`` are ordinary words), and an
#       upper-case non-code (``SUN CO``, ``PTY``) is not a currency. Documented false positive (only ever PARKS, never
#       pays): an all-capitals word that is also a code (``TRY AGAIN``, ``PAY ALL``, ``BOB``);
#   (d) ambiguous currency words (real, won, franc(s), lira, ...) only directly after a number (``100 won``);
#   (e) runs of single letters split by up to 3 separators of any kind incl. newlines ("U S D", "E-U-R", "U\nS\nD"):
#       curated codes match anywhere in the joined run, other ISO codes only when the run equals the code.
# Linear (literal alternations, one pass, runs consumed once). ``US`` is matched upper-case only (the pronoun "us").
ISO_4217_CODES = frozenset((
    "AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE "
    "CHF CHW CLF CLP CNY COP COU CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD "
    "HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD "
    "MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR "
    "RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS "
    "UAH UGX USD USN UYI UYU UZS VED VES VND VUV WST XAF XAG XAU XBA XBB XBC XBD XCD XDR XOF XPD XPF XPT XSU XTS XUA XXX "
    "YER ZMW ZWG ZWL").split()) | frozenset({"USDT", "USDC", "BTC", "ETH", "RMB"})
_LETTER = r"[^\W\d_]"
_FOREIGN_WORD = re.compile(
    r"(?<!" + _LETTER + r")(?:(?-i:US)|USDT|USDC|USD|BTC|ETH|JPY|yen|CNY|rmb|yuan|INR|rupees?|MXN|pesos?|d[o\u00f3]lar(?:es)?|"
    r"dollars?|euros?|pounds?|GBP|EUR|AUD|CAD|NZD|CHF|SGD|HKD|AED|"
    r"kron(?:er|e|or|a|ur)|reais|rou?bles?|rubles?|dirhams?|riyals?|shekels?|sheqels?|baht|ringgit|rupiah|zlot(?:y|ych|ys)|"
    r"forints?|korun[ay]?|dinars?|naira|bitcoin|quid|sterling|shillings?|hryvnia|cedis?|birr|kwacha|kwanza|metical|taka|"
    r"tenge|manat|denar|\u0631\u06cc\u0627\u0644)(?!" + _LETTER + r")",
    re.IGNORECASE,
)
_AMBIGUOUS_WORD = re.compile(
    r"\d[ \t]{0,2}(?:real|won|francs?|lir[ae]|rials?|dong|lev|leu|lek|kip|riel|pula|dram|som)(?!" + _LETTER + r")",
    re.IGNORECASE,
)
_UPPER_CODE = re.compile(r"(?<!" + _LETTER + r")[A-Z]{3}(?!" + _LETTER + r")")
_GLUED_CODE = re.compile(r"(?<=\d)[A-Za-z]{3}(?!" + _LETTER + r")|(?<!" + _LETTER + r")[A-Za-z]{3}(?=\d)")
# RS3AF4-2: a lower / mixed-case ISO code IMMEDIATELY after a labelled or marked figure (same line, at most 2 blanks:
# ``Amount: 100 sek``, ``R100 usd``, ``amount 100 nok``). Ordinary lower-case words elsewhere do not park; a word that is
# also a code stays unparked in lower / mixed case (``_CODE_WORD_COLLISIONS``).
_FIGURE_THEN_CODE = re.compile(
    r"(?:(?<![A-Za-z0-9])amount[\s:=]{0,3}(?:(?:R|ZAR)[ \t]?)?|(?<![A-Za-z0-9\-/#_.])(?:R|ZAR)[ \t]?)"
    + _NUM + r"[ \t]{0,2}(?P<code>[A-Za-z]{3})(?![A-Za-z])", re.ASCII | re.IGNORECASE)
# ISO codes that are also ordinary English words / common first names: NOT parked by the adjacent lower-case rule
# (``Amount: R100 try again``, ``R100 all good``, ``R100 Bob``); their UPPER-CASE spelling still parks (rule above).
_CODE_WORD_COLLISIONS = frozenset(("ALL", "TOP", "TRY", "BOB", "PEN", "COP", "MAD", "CUP", "SOS", "RON", "RUB", "MOP", "GEL",
                                   "BAM", "DOP"))
_LETTER_RUN = re.compile(r"(?<!" + _LETTER + r")" + _LETTER + r"(?:[\s.\-_/]{1,3}" + _LETTER + r")+")
_SPLIT_TOKENS = ("US", "USDT", "USDC", "USD", "BTC", "ETH", "JPY", "CNY", "RMB", "INR", "MXN", "GBP", "EUR", "AUD",
                 "CAD", "NZD", "CHF", "SGD", "HKD", "AED", "YEN")
_LOOKALIKES = str.maketrans({
    "\u0410": "A", "\u0412": "B", "\u0415": "E", "\u041a": "K", "\u041c": "M", "\u041d": "H", "\u041e": "O", "\u0420": "P",
    "\u0421": "C", "\u0422": "T", "\u0425": "X", "\u0423": "Y", "\u0406": "I", "\u0405": "S", "\u0408": "J",
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0445": "x", "\u0443": "y", "\u0456": "i",
    "\u0455": "s", "\u0458": "j", "\u043a": "k",
    "\u0391": "A", "\u0392": "B", "\u0395": "E", "\u0396": "Z", "\u0397": "H", "\u0399": "I", "\u039a": "K", "\u039c": "M",
    "\u039d": "N", "\u039f": "O", "\u03a1": "P", "\u03a4": "T", "\u03a5": "Y", "\u03a7": "X",
    "\u03bf": "o", "\u03b9": "i",
})


def has_foreign_currency_token(text: str) -> bool:
    """True when ``text`` carries a non-ZAR currency token (symbol, code, word; also full-width, zero-width,
    look-alike-letter or split by spaces/newlines; module comment above for the policy). Fails closed (True) on input
    longer than ``MAX_TYPED_CHARS``."""
    text = text or ""
    if len(text) > MAX_TYPED_CHARS:
        return True
    folded = "".join(ch for ch in unicodedata.normalize("NFKC", text) if unicodedata.category(ch) != "Cf")
    folded = folded.translate(_LOOKALIKES)
    if any(unicodedata.category(ch) == "Sc" for ch in folded):
        return True
    if _FOREIGN_WORD.search(folded) or _AMBIGUOUS_WORD.search(folded):
        return True
    if any(m.group() in ISO_4217_CODES for m in _UPPER_CODE.finditer(folded)):
        return True
    if any(m.group().upper() in ISO_4217_CODES for m in _GLUED_CODE.finditer(folded)):
        return True
    if any(m.group("code").upper() in ISO_4217_CODES and m.group("code").upper() not in _CODE_WORD_COLLISIONS
           for m in _FIGURE_THEN_CODE.finditer(folded)):
        return True
    for run in _LETTER_RUN.finditer(folded):
        letters = "".join(ch for ch in run.group() if ch.isalpha()).upper()
        if letters in ISO_4217_CODES or any(token in letters for token in _SPLIT_TOKENS):
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
