"""Source-account selection from the email's last-3 digits (F3).

The last-3 digits in an inbound email select WHICH of the user's own accounts to
debit (the source account). They are matched EXACTLY against the user's real
Investec accounts (``get_accounts``); zero or more-than-one match -> fail-closed.

The last-3 is a WEAK SELECTOR only — it is NEVER an authorization factor.
Execution still requires the valid bound HMAC token (F2). This module returns
the resolved account dict (or ``None``); it performs no authorization.
"""
from __future__ import annotations


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
