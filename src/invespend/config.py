"""Environment-driven settings.

Secrets are never hard-coded: locally they come from a git-ignored ``.env``
file, in CI they come from GitHub Actions encrypted secrets injected as env vars.

Account groupings and exclusions live in ``groups.py`` — edit that file instead
of setting secrets.
"""
from __future__ import annotations

import math
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


def _opt_cap(name: str) -> float:
    """Spending cap from the environment, FAIL-CLOSED (F18): unset, empty,
    unparsable, NaN, infinite or non-positive all mean 0.0 ("no payment may
    pass"). Never raises, so a malformed cap cannot break non-payment commands."""
    raw = os.getenv(name)
    if raw is None:
        return 0.0
    try:
        value = float(raw.strip())
    except ValueError:
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


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


def _csv_lower(name: str) -> tuple[str, ...]:
    """Comma-separated list, trimmed and lower-cased; empty items dropped."""
    return tuple(p.strip().lower() for p in os.getenv(name, "").split(",") if p.strip())


def _window(name: str, default: float) -> float:
    """Age/expiry window in hours or days (v2): unset/empty -> ``default``; unparsable, non-finite or
    non-positive -> 0.0, i.e. "already expired" (fail closed, parsed like the caps)."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return float(default)
    try:
        value = float(raw.strip())
    except ValueError:
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def _positive_int(name: str, default: int) -> int:
    """Count-like v2 setting: unset, unparsable or non-positive -> ``default`` (never crashes)."""
    raw = os.getenv(name)
    try:
        value = int(raw.strip()) if raw is not None and raw.strip() else default
    except ValueError:
        return default
    return value if value > 0 else default


_MAX_HOLD_HOURS = 24 * 365       # RS3A-4: a larger value (1e308 overflows timedelta) falls back to 24 hours


def _hold_hours(name: str) -> tuple[float, bool]:
    """New-beneficiary hold (F39, C5): unset/empty -> 0; a SET value that is unparsable, negative or
    non-finite falls back to 24 hours (the safe direction). Returns (hours, fell_back)."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return 0.0, False
    try:
        value = float(raw.strip())
    except ValueError:
        return 24.0, True
    if not math.isfinite(value) or value < 0 or value > _MAX_HOLD_HOURS:
        return 24.0, True
    return value, False


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
    # Payment-state backend (OQ2 opt-in): "file" (default) keeps state under
    # state_dir as JSON; "postgres" stores it in the DB (reuses database_url) so a
    # Railway deploy needs no persistent volume. File stays the default behaviour.
    payments_state_backend: str = "file"
    # Beneficiary matching (off by default): after a successful ingest, label
    # outgoing payments with the registered Investec beneficiary. Separate from
    # payments_beneficiaries_from_api; never moves money.
    beneficiary_matching_enabled: bool = False

    # ── Email-payment v2 (batch approval; ALL optional, safe defaults, read via getattr elsewhere) ──
    # PAYMENTS_MODE: "legacy" (default, the retired token flow) or "v2". Unknown values are an error envelope.
    payments_mode: str = "legacy"
    # Senders allowed to instruct AND to approve (exact addresses, lower-cased). Empty = nobody (fail closed).
    payments_allowed_senders: tuple[str, ...] = ()
    # Owner-only failure notices; falls back to the allowed senders.
    payments_notify_recipients: tuple[str, ...] = ()
    # Authentication-Results authserv-id(s) of OUR receiving MTA (e.g. mx.google.com). Empty = nothing authenticates.
    payments_authserv_id: str = ""
    payments_authserv_ids: tuple[str, ...] = ()
    # Windows (hours/days). Unparsable or <= 0 means 0 = already expired (fail closed).
    payments_max_message_age_hours: float = 24.0
    payments_approval_expiry_hours: float = 24.0
    payments_approved_grace_hours: float = 24.0
    payments_held_expiry_hours: float = 24.0
    payments_awaiting_expiry_days: float = 7.0
    payments_max_batch_items: int = 50
    payments_submitting_stale_minutes: int = 30
    payments_stuck_message_minutes: int = 30
    payments_duplicate_window_days: int = 7
    # HMAC key for beneficiary fingerprints / account hashes. Secret, never logged. Unset: offers and executions park.
    payments_fingerprint_key: str = field(default="", repr=False)
    payments_hold_registered_recent: bool = True
    # New-beneficiary hold (F39): default 0 hours. NOTE: the 24h rule is NOT in the official Investec swagger; the
    # community FAQ's "a beneficiary must be paid once in Investec Online first" may still block a brand-new
    # beneficiary even at hold 0 (the payment then ends `failed` with Investec's message).
    payments_hold_hours: float = 0.0
    payments_hold_hours_fallback: bool = False
    # Image reading (S14 adapter): NO default model id; disabled until key AND model are both set.
    image_extractor_model: str = ""
    anthropic_api_key: str = field(default="", repr=False)
    payments_max_image_bytes: int = 5_000_000
    image_extractor_retries: int = 0

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
            per_payment_cap=_opt_cap("PER_PAYMENT_CAP"),
            daily_aggregate_cap=_opt_cap("DAILY_AGGREGATE_CAP"),
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
            payments_state_backend=_opt("PAYMENTS_STATE_BACKEND", "file").lower(),
            beneficiary_matching_enabled=_opt_bool("BENEFICIARY_MATCHING_ENABLED", False),
            # ── Email-payment v2 ──────────────────────────────────────────────
            payments_mode=_opt("PAYMENTS_MODE", "legacy").lower(),
            payments_allowed_senders=_csv_lower("PAYMENTS_ALLOWED_SENDERS"),
            payments_notify_recipients=_csv_lower("PAYMENTS_NOTIFY_RECIPIENTS"),
            payments_authserv_id=_opt("PAYMENTS_AUTHSERV_ID").lower(),
            payments_authserv_ids=_csv_lower("PAYMENTS_AUTHSERV_IDS"),
            payments_max_message_age_hours=_window("PAYMENTS_MAX_MESSAGE_AGE_HOURS", 24),
            payments_approval_expiry_hours=_window("PAYMENTS_APPROVAL_EXPIRY_HOURS", 24),
            payments_approved_grace_hours=_window("PAYMENTS_APPROVED_GRACE_HOURS", 24),
            payments_held_expiry_hours=_window("PAYMENTS_HELD_EXPIRY_HOURS", 24),
            payments_awaiting_expiry_days=_window("PAYMENTS_AWAITING_EXPIRY_DAYS", 7),
            payments_max_batch_items=_positive_int("PAYMENTS_MAX_BATCH_ITEMS", 50),
            payments_submitting_stale_minutes=_positive_int("PAYMENTS_SUBMITTING_STALE_MINUTES", 30),
            payments_stuck_message_minutes=_positive_int("PAYMENTS_STUCK_MESSAGE_MINUTES", 30),
            payments_duplicate_window_days=_positive_int("PAYMENTS_DUPLICATE_WINDOW_DAYS", 7),
            payments_fingerprint_key=_opt("PAYMENTS_FINGERPRINT_KEY"),
            payments_hold_registered_recent=_opt_bool("PAYMENTS_HOLD_REGISTERED_RECENT", True),
            payments_hold_hours=_hold_hours("PAYMENTS_HOLD_HOURS")[0],
            payments_hold_hours_fallback=_hold_hours("PAYMENTS_HOLD_HOURS")[1],
            image_extractor_model=_opt("IMAGE_EXTRACTOR_MODEL"),
            anthropic_api_key=_opt("ANTHROPIC_API_KEY"),
            payments_max_image_bytes=_positive_int("PAYMENTS_MAX_IMAGE_BYTES", 5_000_000),
            image_extractor_retries=int(_window("IMAGE_EXTRACTOR_RETRIES", 0)),
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
