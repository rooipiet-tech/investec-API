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


@dataclass(frozen=True)
class Settings:
    # Investec Open API
    investec_client_id: str
    investec_client_secret: str
    investec_api_key: str
    investec_base_url: str

    # Database
    database_url: str

    # Email — Gmail API (preferred) or SMTP fallback
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    report_sender: str
    report_recipients: list[str] = field(default_factory=list)
    # Gmail OAuth2 credentials. When all three are set the Gmail API is used
    # instead of SMTP (no app-password required, no "less-secure app" toggle).
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""

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
            ingest_window_days=int(_opt("INGEST_WINDOW_DAYS", "7")),
            backup_passphrase=_opt("BACKUP_PASSPHRASE"),
            gmail_client_id=_opt("GMAIL_CLIENT_ID"),
            gmail_client_secret=_opt("GMAIL_CLIENT_SECRET"),
            gmail_refresh_token=_opt("GMAIL_REFRESH_TOKEN"),
        )

    @property
    def reporting_db_url(self) -> str:
        """Connection the report uses — the read-only role if configured."""
        return self.report_database_url or self.database_url

    @property
    def use_gmail_api(self) -> bool:
        """True when Gmail API credentials are fully configured."""
        return bool(self.gmail_client_id and self.gmail_client_secret
                    and self.gmail_refresh_token)

    def require_email(self) -> None:
        """Validate email settings only when we actually intend to send."""
        if not self.report_sender:
            raise RuntimeError("Missing email setting: REPORT_SENDER")
        if not self.report_recipients:
            raise RuntimeError("REPORT_RECIPIENTS is empty; nowhere to send the report.")
        if self.use_gmail_api:
            return  # OAuth credentials already validated by use_gmail_api
        missing = [
            n for n, v in (
                ("SMTP_USER", self.smtp_user),
                ("SMTP_PASSWORD", self.smtp_password),
            ) if not v
        ]
        if missing:
            raise RuntimeError(f"Missing SMTP settings: {', '.join(missing)}")
