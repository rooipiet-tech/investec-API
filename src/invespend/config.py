"""Environment-driven settings.

Secrets are never hard-coded: locally they come from a git-ignored ``.env``
file, in CI they come from GitHub Actions encrypted secrets injected as env vars.

Account groupings and exclusions live in ``groups.py`` — edit that file instead
of setting secrets.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()  # no-op in CI where vars are already in the environment

# The .env.example placeholder value for APPROVAL_SIGNING_SECRET. A user who
# copies .env.example verbatim must NOT be able to issue/verify real tokens.
_SIGNING_SECRET_PLACEHOLDER = "change_me_long_random_secret"
# Minimum acceptable signing-secret length (rejects trivially-short secrets).
_SIGNING_SECRET_MIN_LEN = 16


def signing_secret_is_valid(secret: str | None) -> bool:
    """A signing secret is usable only when it is non-empty, not the example
    placeholder, and at least the minimum length (F2/F15)."""
    if not secret:
        return False
    secret = secret.strip()
    if not secret or secret == _SIGNING_SECRET_PLACEHOLDER:
        return False
    return len(secret) >= _SIGNING_SECRET_MIN_LEN


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


def _opt_bool(name: str, default: bool = False) -> bool:
    """Read an optional boolean env var (1/true/yes/on -> True)."""
    raw = _opt(name)
    if not raw:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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
    # Default recipients for accounts not claimed by any group in groups.py.
    report_recipients: list[str] = field(default_factory=list)

    # Behaviour
    ingest_window_days: int = 7

    # Optional read-only connection for reporting / Power BI (least privilege).
    # Falls back to database_url when unset.
    report_database_url: str = ""

    # Backup (optional): passphrase to encrypt the weekly pg_dump at rest.
    backup_passphrase: str = ""

    # ── Email-payment-approval (NEW, all optional with safe defaults) ─────────
    # HMAC key that signs approval tokens. Env only, never logged/emailed (F2/F15).
    approval_signing_secret: str = ""
    # Spending caps (default 0 => fail-closed: no payment passes until configured).
    per_payment_cap: float = 0.0
    daily_aggregate_cap: float = 0.0
    # DRY-RUN is the default; live needs BOTH the flag AND a write credential (PR1/F8).
    payments_dry_run: bool = True
    payments_live_enable: bool = False
    # Write-scoped Investec credential (absent by default -> stays dry-run).
    investec_write_client_id: str = ""
    investec_write_client_secret: str = ""
    investec_write_api_key: str = ""
    # The user may declare the MAIN Investec credential is payment-capable (single
    # key for reads + payments). Off by default: a read key never silently pays.
    investec_payments_enabled: bool = False
    # Source the beneficiary allowlist from the Investec API instead of static code.
    payments_beneficiaries_from_api: bool = False
    # IMAP inbox (read; tests inject a fake reader).
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_mailbox: str = "INBOX"
    # POPIA retention window (days) for pending/audit cleanup (F20).
    retention_days: int = 90
    # Where pending/daily-total/audit JSON state is persisted (F17).
    state_dir: str = ".invespend_state"

    def require_signing_secret(self) -> str:
        """Return the approval signing secret only when it is valid; otherwise
        fail CLOSED (F2/F15) so a placeholder/short secret can never mint or
        verify a real token."""
        if not signing_secret_is_valid(self.approval_signing_secret):
            raise RuntimeError(
                "APPROVAL_SIGNING_SECRET is missing, the .env.example placeholder, "
                "or too short; set a strong random secret (>= "
                f"{_SIGNING_SECRET_MIN_LEN} chars) before issuing approval tokens."
            )
        return self.approval_signing_secret.strip()

    def _has_write_trio(self) -> bool:
        """True iff a full separate write-scoped Investec credential is configured."""
        return bool(
            self.investec_write_client_id
            and self.investec_write_client_secret
            and self.investec_write_api_key
        )

    def has_write_credential(self) -> bool:
        """True iff money can be moved: EITHER a full separate write-scoped trio is
        configured, OR the user explicitly declared the main credential is
        payment-capable (investec_payments_enabled). A read key never pays unless
        one of these is true (PR1/F8)."""
        return self._has_write_trio() or self.investec_payments_enabled

    def payment_credentials(self) -> tuple[str, str, str]:
        """The (client_id, secret, api_key) the LIVE client must use to pay: the
        separate write trio when fully present, else the main Investec trio (the
        user-declared payment-capable key)."""
        if self._has_write_trio():
            return (
                self.investec_write_client_id,
                self.investec_write_client_secret,
                self.investec_write_api_key,
            )
        return (
            self.investec_client_id,
            self.investec_client_secret,
            self.investec_api_key,
        )

    def live_enabled(self) -> bool:
        """Live money movement is permitted ONLY when the enable flag is set AND a
        payment-capable credential is present (PR1/F8). Flag-only stays dry-run."""
        return bool(self.payments_live_enable and self.has_write_credential())

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
            # ── Email-payment-approval (all optional) ─────────────────────────
            approval_signing_secret=_opt("APPROVAL_SIGNING_SECRET"),
            per_payment_cap=float(_opt("PER_PAYMENT_CAP", "0")),
            daily_aggregate_cap=float(_opt("DAILY_AGGREGATE_CAP", "0")),
            payments_dry_run=_opt_bool("PAYMENTS_DRY_RUN", True),
            payments_live_enable=_opt_bool("PAYMENTS_LIVE_ENABLE", False),
            investec_write_client_id=_opt("INVESTEC_WRITE_CLIENT_ID"),
            investec_write_client_secret=_opt("INVESTEC_WRITE_CLIENT_SECRET"),
            investec_write_api_key=_opt("INVESTEC_WRITE_API_KEY"),
            investec_payments_enabled=_opt_bool("INVESTEC_PAYMENTS_ENABLED", False),
            payments_beneficiaries_from_api=_opt_bool("PAYMENTS_BENEFICIARIES_FROM_API", False),
            imap_host=_opt("IMAP_HOST"),
            imap_port=int(_opt("IMAP_PORT", "993")),
            imap_user=_opt("IMAP_USER"),
            imap_password=_opt("IMAP_PASSWORD").replace(" ", ""),
            imap_mailbox=_opt("IMAP_MAILBOX", "INBOX"),
            retention_days=int(_opt("RETENTION_DAYS", "90")),
            state_dir=_opt("PAYMENTS_STATE_DIR", ".invespend_state"),
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
        # Groups in groups.py carry their own recipients; a default list is only
        # required when no groups are defined there.
        from . import groups as _groups  # late import avoids any circular risk
        if not self.report_recipients and not _groups.GROUPS:
            raise RuntimeError("REPORT_RECIPIENTS is empty; nowhere to send the report.")
