"""Environment-driven settings.

Secrets are never hard-coded: locally they come from a git-ignored ``.env``
file, in CI they come from GitHub Actions encrypted secrets injected as env vars.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()  # no-op in CI where vars are already in the environment


def _require(name: str) -> str:
    value = os.getenv(name)
    if value is not None:
        value = value.strip()  # tolerate trailing newlines/spaces from pasted secrets
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env (local) or set it as a GitHub secret (CI)."
        )
    return value


def _opt(name: str, default: str = "") -> str:
    """Read an optional env var, trimmed of surrounding whitespace."""
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


@dataclass
class AccountGroup:
    """A named set of accounts that gets its own report email.

    ``account_patterns`` are case-insensitive substrings matched against
    ``account_name``; any match claims the account for this group.
    Configured via REPORT_GROUP_{N}_ACCOUNTS / REPORT_GROUP_{N}_RECIPIENTS.
    """
    name: str
    account_patterns: list[str]
    recipients: list[str]


def account_matches(account_name: str, patterns: list[str]) -> bool:
    """Return True if account_name contains any pattern (case-insensitive)."""
    name_lower = account_name.lower()
    return any(p.lower() in name_lower for p in patterns)


def _load_account_groups() -> list[AccountGroup]:
    """Scan REPORT_GROUP_{1..19}_ACCOUNTS / _RECIPIENTS env vars into groups."""
    groups = []
    for i in range(1, 20):
        accts_env = _opt(f"REPORT_GROUP_{i}_ACCOUNTS")
        if not accts_env:
            continue
        recips_env = _opt(f"REPORT_GROUP_{i}_RECIPIENTS")
        groups.append(AccountGroup(
            name=f"group_{i}",
            account_patterns=[p.strip() for p in accts_env.split(",") if p.strip()],
            recipients=[r.strip() for r in recips_env.split(",") if r.strip()],
        ))
    return groups


@dataclass(frozen=True)
class Settings:
    # Investec Open API
    investec_client_id: str
    investec_client_secret: str
    investec_api_key: str
    investec_base_url: str

    # Database
    database_url: str

    # Email
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    report_sender: str
    report_recipients: list[str] = field(default_factory=list)

    # Account filtering / grouping
    report_exclude_accounts: list[str] = field(default_factory=list)
    report_account_groups: list[AccountGroup] = field(default_factory=list)

    # Behaviour
    ingest_window_days: int = 7

    # Optional read-only connection for reporting / Power BI (least privilege).
    # Falls back to database_url when unset.
    report_database_url: str = ""

    # Backup (optional): passphrase to encrypt the weekly pg_dump at rest.
    backup_passphrase: str = ""

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            investec_client_id=_require("INVESTEC_CLIENT_ID"),
            investec_client_secret=_require("INVESTEC_CLIENT_SECRET"),
            investec_api_key=_require("INVESTEC_API_KEY"),
            investec_base_url=_opt("INVESTEC_BASE_URL", "https://openapi.investec.com"),
            database_url=_require("DATABASE_URL"),
            report_database_url=_opt("REPORT_DATABASE_URL"),
            smtp_host=_opt("SMTP_HOST", "smtp.gmail.com"),
            smtp_port=int(_opt("SMTP_PORT", "587")),
            smtp_user=_opt("SMTP_USER"),
            # Gmail shows app passwords as "abcd efgh ijkl mnop"; the real value
            # has no spaces, so drop all whitespace rather than just trimming.
            smtp_password=_opt("SMTP_PASSWORD").replace(" ", ""),
            report_sender=_opt("REPORT_SENDER", _opt("SMTP_USER")),
            report_recipients=[
                r.strip() for r in os.getenv("REPORT_RECIPIENTS", "").split(",") if r.strip()
            ],
            report_exclude_accounts=[
                p.strip() for p in _opt("REPORT_EXCLUDE_ACCOUNTS").split(",") if p.strip()
            ],
            report_account_groups=_load_account_groups(),
            ingest_window_days=int(_opt("INGEST_WINDOW_DAYS", "7")),
            backup_passphrase=_opt("BACKUP_PASSPHRASE"),
        )

    @property
    def reporting_db_url(self) -> str:
        """Connection the report uses — the read-only role if configured."""
        return self.report_database_url or self.database_url

    def require_email(self) -> None:
        """Validate email settings only when we actually intend to send."""
        missing = [
            n for n, v in (
                ("SMTP_USER", self.smtp_user),
                ("SMTP_PASSWORD", self.smtp_password),
                ("REPORT_SENDER", self.report_sender),
            ) if not v
        ]
        if missing:
            raise RuntimeError(f"Missing email settings: {', '.join(missing)}")
        # When account groups are configured each group carries its own recipients,
        # so a default REPORT_RECIPIENTS list is only required when no groups exist.
        if not self.report_recipients and not self.report_account_groups:
            raise RuntimeError("REPORT_RECIPIENTS is empty; nowhere to send the report.")
