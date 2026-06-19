"""Detect new Investec accounts and alert the user when any are unclaimed by a group.

Called from the daily ingest. Compares the accounts returned by the API with
the groups defined in ``groups.py``; any account not matched by any group
triggers an email so the user can assign it.
"""
from __future__ import annotations

import logging

from . import groups as _groups
from .config import Settings
from .groups import Group

log = logging.getLogger(__name__)

# Keywords hinting that an account is for personal use.
_PERSONAL_HINTS = {
    "private", "premier", "gold", "platinum", "cheque", "current",
    "savings", "individual", "personal",
}
# Keywords hinting at a business/entity account.
_BUSINESS_HINTS = {
    "pty", "ltd", "inc", "llc", "corp", "corporation", "enterprise",
    "consulting", "services", "business", "trading", "holdings",
    "investments", "technologies", "solutions",
}


def suggest_group(account: dict) -> str | None:
    """Return the most likely group name for an unclaimed account, or None.

    Scores each group by how many of its pattern words appear in the account
    name/reference/product, then falls back to product-name heuristics.
    """
    name = (account.get("account_name") or "").lower()
    ref = (account.get("reference_name") or "").lower()
    product = (account.get("product_name") or "").lower()
    combined = f"{name} {ref} {product}"

    best: Group | None = None
    best_score = 0
    for group in _groups.GROUPS:
        score = 0
        for pattern in group.name_patterns:
            for token in pattern.lower().split():
                if len(token) >= 3 and token in combined:
                    score += 1
        if score > best_score:
            best_score = score
            best = group
    if best:
        return best.name

    words = set(combined.split())
    if words & _BUSINESS_HINTS:
        return "business"
    if words & _PERSONAL_HINTS:
        return "personal"
    return None


def find_unclaimed(accounts: list[dict], settings: Settings) -> list[dict]:
    """Return accounts not matched by any group in groups.py (and not excluded)."""
    unclaimed = []
    for account in accounts:
        name = account.get("account_name") or ""
        number = account.get("account_number") or ""
        if any(p.lower() in name.lower() for p in _groups.EXCLUDE_ACCOUNTS):
            continue
        if not any(g.matches(name, number) for g in _groups.GROUPS):
            unclaimed.append(account)
    return unclaimed


def build_alert_email(new_accounts: list[dict], settings: Settings) -> tuple[str, str]:
    """Build (subject, body) for the new-account alert email."""
    unclaimed = find_unclaimed(new_accounts, settings)
    count = len(new_accounts)
    subject = (
        f"[invespend] {count} new Investec account(s) detected"
        + (" — group assignment needed" if unclaimed else " — all claimed")
    )

    lines = [
        f"invespend detected {count} new Investec account(s) during today's sync.",
        "",
    ]

    unclaimed_ids = {u["account_id"] for u in unclaimed}

    for acc in new_accounts:
        name = acc.get("account_name") or "Unknown"
        number = acc.get("account_number") or "n/a"
        product = acc.get("product_name") or "n/a"
        ref = acc.get("reference_name") or ""
        is_unclaimed = acc["account_id"] in unclaimed_ids
        suggested = suggest_group(acc) if is_unclaimed else None

        lines.append(f"  Account:  {name}")
        if ref and ref.lower() != name.lower():
            lines.append(f"  Ref:      {ref}")
        lines.append(f"  Number:   {number}")
        lines.append(f"  Product:  {product}")
        if is_unclaimed:
            if suggested:
                lines.append(f"  Status:   UNCLAIMED — suggested group: '{suggested}'")
            else:
                lines.append("  Status:   UNCLAIMED — no group suggestion available")
        else:
            lines.append("  Status:   already claimed by a configured group")
        lines.append("")

    if unclaimed:
        lines += [
            "─" * 60,
            "HOW TO ASSIGN AN ACCOUNT TO A GROUP",
            "",
            "Edit src/invespend/groups.py and add the account to an existing group",
            "or create a new Group entry:",
            "",
        ]
        for group in _groups.GROUPS:
            current_names = ", ".join(f'"{p}"' for p in group.name_patterns)
            current_nums = ", ".join(f'"{n}"' for n in group.account_numbers)
            lines.append(f"  Group '{group.name}':")
            if current_names:
                lines.append(f"    name_patterns: [{current_names}]")
            if current_nums:
                lines.append(f"    account_numbers: [{current_nums}]")
            lines.append("")
        lines += [
            "To add a new group, append a Group(...) entry to the GROUPS list.",
            "",
        ]

    lines.append("— invespend")
    return subject, "\n".join(lines)
