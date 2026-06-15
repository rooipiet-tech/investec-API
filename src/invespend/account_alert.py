"""Detect new Investec accounts and alert the user when any are unclaimed by a group.

Called from the daily ingest. Compares the accounts returned by the API with
the group patterns in Settings; any account not matched by any group triggers
an email so the user can assign it.
"""
from __future__ import annotations

import logging

from .config import AccountGroup, Settings, account_matches

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


def suggest_group(account: dict, settings: Settings) -> str | None:
    """Return the most likely group name for an unclaimed account, or None.

    Tries each configured group (looking for shared words/tokens between the
    account and the group's patterns), then falls back to product-name
    heuristics so the user gets a concrete suggestion even when there are no
    groups configured yet.
    """
    name = (account.get("account_name") or "").lower()
    ref = (account.get("reference_name") or "").lower()
    product = (account.get("product_name") or "").lower()
    combined = f"{name} {ref} {product}"

    # Score each existing group by how many of its pattern *words* appear in
    # the combined text (weaker than a substring match, catches partial names).
    best: AccountGroup | None = None
    best_score = 0
    for group in settings.report_account_groups:
        score = 0
        for pattern in group.account_patterns:
            for token in pattern.lower().split():
                if len(token) >= 3 and token in combined:
                    score += 1
        if score > best_score:
            best_score = score
            best = group
    if best:
        return best.name

    # Heuristic fallback when no groups are configured or no words matched.
    words = set(combined.split())
    if words & _BUSINESS_HINTS:
        return "business"
    if words & _PERSONAL_HINTS:
        return "personal"
    return None


def find_unclaimed(accounts: list[dict], settings: Settings) -> list[dict]:
    """Return accounts not matched by any configured group pattern (and not excluded)."""
    exclude = settings.report_exclude_accounts
    unclaimed = []
    for account in accounts:
        name = account.get("account_name") or ""
        if account_matches(name, exclude):
            continue
        claimed = any(
            account_matches(name, g.account_patterns)
            for g in settings.report_account_groups
        )
        if not claimed:
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
        suggested = suggest_group(acc, settings) if is_unclaimed else None

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
        n_groups = len(settings.report_account_groups)
        next_n = n_groups + 1
        lines += [
            "─" * 60,
            "HOW TO ASSIGN AN ACCOUNT TO A GROUP",
            "",
            "Option A — add a new group (GitHub Actions → Settings → Secrets):",
            "",
            f"  REPORT_GROUP_{next_n}_NAME=<group-name>",
            f"  REPORT_GROUP_{next_n}_ACCOUNTS=<comma-separated substrings matching the account name>",
            f"  REPORT_GROUP_{next_n}_RECIPIENTS=<comma-separated email addresses>",
            "",
        ]
        if settings.report_account_groups:
            lines.append("Option B — add the account to an existing group:")
            lines.append("")
            for i, group in enumerate(settings.report_account_groups, 1):
                lines.append(f"  REPORT_GROUP_{i}_NAME={group.name}")
                current = ",".join(group.account_patterns)
                lines.append(f"  REPORT_GROUP_{i}_ACCOUNTS={current},<new pattern>")
                lines.append("")

    lines.append("— invespend")
    return subject, "\n".join(lines)
