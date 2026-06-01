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
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env (local) or set it as a GitHub secret (CI)."
        )
    return value


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

    # Behaviour
    ingest_window_days: int = 7

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            investec_client_id=_require("INVESTEC_CLIENT_ID"),
            investec_client_secret=_require("INVESTEC_CLIENT_SECRET"),
            investec_api_key=_require("INVESTEC_API_KEY"),
            investec_base_url=os.getenv("INVESTEC_BASE_URL", "https://openapi.investec.com"),
            database_url=_require("DATABASE_URL"),
            smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com"),
            smtp_port=int(os.getenv("SMTP_PORT", "587")),
            smtp_user=os.getenv("SMTP_USER", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            report_sender=os.getenv("REPORT_SENDER", os.getenv("SMTP_USER", "")),
            report_recipients=[
                r.strip() for r in os.getenv("REPORT_RECIPIENTS", "").split(",") if r.strip()
            ],
            ingest_window_days=int(os.getenv("INGEST_WINDOW_DAYS", "7")),
        )

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
        if not self.report_recipients:
            raise RuntimeError("REPORT_RECIPIENTS is empty; nowhere to send the report.")
