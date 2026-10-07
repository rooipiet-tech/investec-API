"""``PAYMENTS_MODE`` and the v2 settings view (S11). Stdlib only.

Every v2 setting is read through ``getattr`` with a safe default, so a stubbed ``Settings`` (the existing CLI tests
build minimal ones) never needs the new fields and a missing field can only make v2 stricter, never looser.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

KNOWN_MODES = ("legacy", "v2")
MAX_HOLD_HOURS = 24 * 365          # RS3A-4: also keeps timedelta(hours=...) from overflowing


def payments_mode(settings: object) -> str:
    """Normalised mode: trimmed and lower-cased; missing/empty/None means ``legacy``."""
    raw = getattr(settings, "payments_mode", None)
    text = str(raw).strip().lower() if raw is not None else ""
    return text or "legacy"


def _window(value: object, default: float) -> float:
    """Age/expiry windows: missing -> default; unparsable, non-finite or <= 0 -> 0 (already expired, fail closed)."""
    if value is None:
        return float(default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def _count(value: object, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _strings(value: object) -> frozenset[str]:
    if isinstance(value, str):
        value = value.split(",")
    try:
        return frozenset(str(v).strip().lower() for v in (value or ()) if str(v).strip())
    except TypeError:
        return frozenset()


@dataclass(frozen=True)
class V2Settings:
    allowed_senders: frozenset[str]
    authserv_ids: frozenset[str]
    notify_recipients: tuple[str, ...]
    sender: str
    mailbox: str
    max_age_hours: float
    approval_expiry_hours: float
    grace_hours: float
    held_expiry_hours: float
    awaiting_expiry_days: float
    hold_hours: float
    hold_fallback: bool
    hold_registered_recent: bool
    max_batch_items: int
    stale_minutes: int
    stuck_minutes: int
    duplicate_days: int
    fingerprint_key: str
    retention_days: int
    per_payment_cap: float
    daily_cap: float

    @property
    def max_age(self) -> timedelta:
        return timedelta(hours=self.max_age_hours)

    @property
    def approval_window(self) -> timedelta:
        return timedelta(hours=self.approval_expiry_hours)

    @property
    def grace(self) -> timedelta:
        return timedelta(hours=self.grace_hours)

    @property
    def held_expiry(self) -> timedelta:
        return timedelta(hours=self.held_expiry_hours)

    @property
    def awaiting_expiry(self) -> timedelta:
        return timedelta(days=self.awaiting_expiry_days)

    @property
    def hold(self) -> timedelta:
        return timedelta(hours=self.hold_hours)

    @property
    def stale(self) -> timedelta:
        return timedelta(minutes=self.stale_minutes)

    @property
    def stuck(self) -> timedelta:
        return timedelta(minutes=self.stuck_minutes)

    @property
    def duplicate_window(self) -> timedelta:
        return timedelta(days=self.duplicate_days)


def v2_settings(settings: object) -> V2Settings:
    g = lambda name, default=None: getattr(settings, name, default)   # noqa: E731
    authserv = set(_strings(g("payments_authserv_ids")))
    authserv |= _strings(g("payments_authserv_id"))
    allowed = _strings(g("payments_allowed_senders"))
    notify = tuple(sorted(_strings(g("payments_notify_recipients")))) or tuple(sorted(allowed))
    hold = g("payments_hold_hours", 0.0)
    try:
        hold_hours = float(hold)
    except (TypeError, ValueError):
        hold_hours = 24.0
    if not math.isfinite(hold_hours) or hold_hours < 0 or hold_hours > MAX_HOLD_HOURS:
        hold_hours = 24.0
    return V2Settings(
        allowed_senders=allowed,
        authserv_ids=frozenset(authserv),
        notify_recipients=notify,
        sender=str(g("report_sender") or g("smtp_user") or ""),
        mailbox=str(g("imap_mailbox", "INBOX") or ""),
        max_age_hours=_window(g("payments_max_message_age_hours"), 24),
        approval_expiry_hours=_window(g("payments_approval_expiry_hours"), 24),
        grace_hours=_window(g("payments_approved_grace_hours"), 24),
        held_expiry_hours=_window(g("payments_held_expiry_hours"), 24),
        awaiting_expiry_days=_window(g("payments_awaiting_expiry_days"), 7),
        hold_hours=hold_hours,
        hold_fallback=bool(g("payments_hold_hours_fallback", False)),
        hold_registered_recent=bool(g("payments_hold_registered_recent", True)),
        max_batch_items=_count(g("payments_max_batch_items"), 50),
        stale_minutes=_count(g("payments_submitting_stale_minutes"), 30),
        stuck_minutes=_count(g("payments_stuck_message_minutes"), 30),
        duplicate_days=_count(g("payments_duplicate_window_days"), 7),
        fingerprint_key=str(g("payments_fingerprint_key") or ""),
        retention_days=_count(g("retention_days"), 90),
        per_payment_cap=_window(g("per_payment_cap"), 0),
        daily_cap=_window(g("daily_aggregate_cap"), 0),
    )


def caps_configured(cfg: V2Settings) -> bool:
    """BOTH the per-payment and the daily cap are set (> 0 and finite); ``v2_settings`` already maps unset,
    unparsable, non-finite or non-positive values to 0. The daily cap is still re-checked at the claim."""
    return cfg.per_payment_cap > 0 and cfg.daily_cap > 0
