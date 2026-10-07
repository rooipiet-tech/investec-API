"""Label-based bank details from text (S7). Pure, stdlib only, no guessing.

A value is read ONLY from a labelled line. If one label appears with different values
the field is None (never a guess). Input over MAX_CHARS fails closed to all-None.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_CHARS = 20_000
MAX_VALUE = 100

_LABELS: dict[str, re.Pattern[str]] = {
    "payee_name": re.compile(r"[ \t]*(?:beneficiary|payee)(?:[ \t]+name)?[ \t]*[:=][ \t]*(.*)", re.IGNORECASE),
    "bank": re.compile(r"[ \t]*bank(?:[ \t]+name)?[ \t]*[:=][ \t]*(.*)", re.IGNORECASE),
    "account_number": re.compile(
        r"[ \t]*(?:account|acc)\.?[ \t]*(?:number|no\.?|nr\.?|#)[ \t]*[:=]?[ \t]*(.*)", re.IGNORECASE
    ),
    "branch_code": re.compile(r"[ \t]*branch(?:[ \t]+code)?[ \t]*[:=][ \t]*(.*)", re.IGNORECASE),
    "reference": re.compile(
        r"[ \t]*(?:(?:their|beneficiary)[ \t]+)?reference[ \t]*[:=][ \t]*(.*)", re.IGNORECASE
    ),
}


@dataclass(frozen=True)
class BankDetails:
    payee_name: str | None = None
    bank: str | None = None
    account_number: str | None = None
    branch_code: str | None = None
    reference: str | None = None


def _text(value: str) -> str | None:
    value = value.strip()
    if not value or len(value) > MAX_VALUE:
        return None
    if any(unicodedata.category(ch).startswith("C") for ch in value):
        return None
    return value


def _account(value: str) -> str | None:
    value = value.strip()
    if not re.fullmatch(r"[0-9 \-]{1,60}", value):
        return None
    digits = re.sub(r"[ \-]", "", value)
    return digits if 6 <= len(digits) <= 20 else None


def _branch(value: str) -> str | None:
    value = value.strip()
    return value if re.fullmatch(r"[0-9]{4,8}", value) else None


_CLEAN = {"account_number": _account, "branch_code": _branch}


def extract_bank_details(text: str) -> BankDetails:
    if not text or len(text) > MAX_CHARS:
        return BankDetails()
    found: dict[str, list[str | None]] = {name: [] for name in _LABELS}
    for line in text.splitlines():
        line = line[:400]
        for name, pattern in _LABELS.items():
            m = pattern.fullmatch(line)
            if m:
                found[name].append(_CLEAN.get(name, _text)(m.group(1)))
                break
    values: dict[str, str | None] = {}
    for name, items in found.items():
        distinct = set(items)
        values[name] = distinct.pop() if len(distinct) == 1 else None
    return BankDetails(**values)
