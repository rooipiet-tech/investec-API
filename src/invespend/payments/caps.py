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


def check_per_payment(amount, per_payment_cap) -> CapDecision:
    amt = _d(amount)
    cap = _d(per_payment_cap)
    if cap <= 0:
        return CapDecision(False, "per-payment cap not configured (fail-closed)")
    if amt <= 0:
        return CapDecision(False, "non-positive amount")
    if amt > cap:
        return CapDecision(False, f"amount {amt} exceeds per-payment cap {cap}")
    return CapDecision(True, "")


def check_daily_aggregate(amount, today_total, daily_aggregate_cap) -> CapDecision:
    amt = _d(amount)
    total = _d(today_total)
    cap = _d(daily_aggregate_cap)
    if cap <= 0:
        return CapDecision(False, "daily-aggregate cap not configured (fail-closed)")
    if total + amt > cap:
        return CapDecision(
            False,
            f"daily total {total} + {amt} would exceed aggregate cap {cap}",
        )
    return CapDecision(True, "")
