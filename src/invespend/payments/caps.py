"""Spending caps — pure decision functions, enforced BEFORE execution (F6).

Two independent gates:
  * per-payment cap — a single payment may not exceed ``per_payment_cap``.
  * daily-aggregate cap — the running total already executed today plus this
    payment may not exceed ``daily_aggregate_cap``.

Both are pure here; the *persistence* of the daily total (and its atomic,
re-read-pre-execute increment) lives in ``store.py`` (F6/F7, PR3). A cap of 0 or
negative is treated as "no payment may pass" (fail-closed).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class CapDecision:
    ok: bool
    reason: str = ""


def _d(value) -> Decimal:
    return Decimal(str(value))


def _cap(value) -> Decimal:
    """Convert a CAP argument only: unparsable, NaN, infinite or negative -> 0
    (blocked, the safe direction). Never used for amounts."""
    try:
        cap = Decimal(str(value))
    except Exception:
        return Decimal(0)
    if not cap.is_finite() or cap < 0:
        return Decimal(0)
    return cap


def _amount(value) -> Decimal | None:
    """Convert an amount / running total via ``_d``; None when unparsable or
    non-finite. Never mapped to 0 (that would read as "non-positive" or, for a
    day total, as an empty day, i.e. a cap bypass)."""
    try:
        amt = _d(value)
    except Exception:
        return None
    return amt if amt.is_finite() else None


def check_per_payment(amount, per_payment_cap) -> CapDecision:
    cap = _cap(per_payment_cap)
    if cap <= 0:
        return CapDecision(False, "per-payment cap not configured (fail-closed)")
    amt = _amount(amount)
    if amt is None:
        return CapDecision(False, "invalid amount")
    if amt <= 0:
        return CapDecision(False, "non-positive amount")
    if amt > cap:
        return CapDecision(False, f"amount {amt} exceeds per-payment cap {cap}")
    return CapDecision(True, "")


def check_daily_aggregate(amount, today_total, daily_aggregate_cap) -> CapDecision:
    cap = _cap(daily_aggregate_cap)
    if cap <= 0:
        return CapDecision(False, "daily-aggregate cap not configured (fail-closed)")
    amt = _amount(amount)
    total = _amount(today_total)
    if amt is None or total is None:
        return CapDecision(False, "invalid amount")
    if total + amt > cap:
        return CapDecision(
            False,
            f"daily total {total} + {amt} would exceed aggregate cap {cap}",
        )
    return CapDecision(True, "")
