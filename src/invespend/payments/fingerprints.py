"""Keyed fingerprints for beneficiaries and extracted account numbers (S11). Stdlib only.

``Beneficiary.from_api`` drops ``accountNumber``, so fingerprints are built from the RAW beneficiary dicts of the API:
an HMAC-SHA256 (key ``PAYMENTS_FINGERPRINT_KEY``) over name | account number (digits) | branch code. The number itself
is never stored, logged or emailed; the digest is not reversible without the key.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable, Mapping

_SEP = "\x1f"


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _mac(key: str, message: str) -> str:
    if not key:
        raise ValueError("fingerprint key is required")
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def beneficiary_fingerprint(raw: Mapping, key: str) -> str:
    name = " ".join(str(raw.get("beneficiaryName") or raw.get("name") or "").split()).casefold()
    code = str(raw.get("code") or raw.get("branchCode") or "").strip()
    return _mac(key, _SEP.join(["beneficiary", name, _digits(raw.get("accountNumber")), code]))


def account_hmac(number: object, key: str) -> str:
    digits = _digits(number)
    if not digits:
        raise ValueError("an account number is required")
    return _mac(key, _SEP.join(["account", digits]))


def raw_by_id(raw_beneficiaries: Iterable[Mapping] | None) -> dict[str, Mapping]:
    out: dict[str, Mapping] = {}
    for item in raw_beneficiaries or ():
        if isinstance(item, Mapping) and item.get("beneficiaryId"):
            out.setdefault(str(item["beneficiaryId"]).strip(), item)
    return out
