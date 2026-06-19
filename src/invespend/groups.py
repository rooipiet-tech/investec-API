"""Account group definitions — the single place to configure report groupings.

Edit EXCLUDE_ACCOUNTS and GROUPS below. No secrets or env vars required.

Each Group matches accounts by:
  account_numbers — exact Investec account number strings (e.g. "10010900709")
  name_patterns   — case-insensitive substrings of account_name

Either field can be empty; a match in either list claims the account for the group.
Groups are checked in order; the first match wins. Accounts not claimed by any group
(and not in EXCLUDE_ACCOUNTS) land in the default report sent to report_recipients.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Group:
    name: str
    recipients: list[str]
    account_numbers: list[str] = field(default_factory=list)
    name_patterns: list[str] = field(default_factory=list)

    def matches(self, account_name: str, account_number: str = "") -> bool:
        if account_number and account_number in self.account_numbers:
            return True
        return any(p.lower() in account_name.lower() for p in self.name_patterns)


# Excluded from ALL reports and statements (case-insensitive substring of account_name).
EXCLUDE_ACCOUNTS: list[str] = [
    "fin5",
]

GROUPS: list[Group] = [
    Group(
        name="personal",
        recipients=["rooipiet@gmail.com"],
        name_patterns=["BARC", "Ella", "JP van Zyl"],
    ),
    Group(
        name="canvas",
        recipients=["canvas@example.com"],  # TODO: update to actual canvas recipients
        name_patterns=["Canvas Intelligence", "Canvas Kopano", "Canvas Support"],
    ),
]
