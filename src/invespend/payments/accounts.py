"""Source-account selection from the email's last-3 digits (F3).

The last-3 digits in an inbound email select WHICH of the user's own accounts to
debit (the source account). They are matched EXACTLY against the user's real
Investec accounts (``get_accounts``); zero or more-than-one match -> fail-closed.

The last-3 is a WEAK SELECTOR only — it is NEVER an authorization factor.
Execution still requires the valid bound HMAC token (F2). This module returns
the resolved account dict (or ``None``); it performs no authorization.
"""
from __future__ import annotations

import re


def resolve_source_account(accounts: list[dict], last3: str) -> dict | None:
    """Return the single account whose number ends with ``last3``.

    Zero or >1 matches -> ``None`` (fail-closed, F3). ``last3`` must be exactly
    three digits; anything else fails closed.
    """
    if not last3 or len(last3) != 3 or not last3.isdigit():
        return None
    matches = [
        a
        for a in accounts
        if str(a.get("accountNumber") or a.get("account_number") or "").endswith(last3)
    ]
    if len(matches) == 1:
        return matches[0]
    return None  # 0 or >1 -> fail closed


def source_account_id(account: dict) -> str:
    return str(account.get("accountId") or account.get("account_id") or "")


def source_account_last3(account: dict) -> str:
    number = str(account.get("accountNumber") or account.get("account_number") or "")
    return number[-3:] if len(number) >= 3 else ""


def source_profile_id(account: dict) -> str:
    return str(account.get("profileId") or account.get("profile_id") or "")


def resolve_source_unique(accounts: list[dict], last3: str) -> tuple[dict | None, str]:
    """Strict source-account resolution across ALL accounts the key sees (every
    profile). Returns ``(account, reason)``; reason in ``ok | no_match | ambiguous
    | bad_last3``. ``last3`` must be exactly three ASCII digits. The returned
    account carries ``accountId`` and ``profileId`` (see ``source_account_id`` /
    ``source_profile_id``). The last-3 is a weak selector, never authorisation."""
    if not isinstance(last3, str) or not re.fullmatch(r"[0-9]{3}", last3):
        return None, "bad_last3"
    matches = [
        a for a in accounts or []
        if str(a.get("accountNumber") or a.get("account_number") or "").endswith(last3)
    ]
    if len(matches) == 1:
        return matches[0], "ok"
    if not matches:
        return None, "no_match"
    return None, "ambiguous"
