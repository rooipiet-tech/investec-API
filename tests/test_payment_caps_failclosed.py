"""S4: spending caps fail closed and are env-driven (F18).

Defect reproduced first: ``PER_PAYMENT_CAP=abc`` made ``Settings.load()`` raise
ValueError for EVERY subcommand (an ingest outage) and ``inf`` was accepted as a
cap that nothing could exceed. Malformed caps now mean 0 (blocked), payments only.
"""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest

from invespend.config import Settings
from invespend.payments import caps
from invespend.payments.caps import check_daily_aggregate, check_per_payment

ROOT = Path(__file__).resolve().parent.parent


BAD = ["", "0", "-5", "abc", "nan", "inf", "-inf", "NaN", "Infinity", "1e999", " "]


def _load(monkeypatch, **env):
    for k, v in {"INVESTEC_CLIENT_ID": "a", "INVESTEC_CLIENT_SECRET": "b",
                 "INVESTEC_API_KEY": "c", "DATABASE_URL": "postgres://x"}.items():
        monkeypatch.setenv(k, v)
    for k in ("PER_PAYMENT_CAP", "DAILY_AGGREGATE_CAP"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings.load()


def test_unset_caps_block_everything(monkeypatch):
    s = _load(monkeypatch)
    assert s.per_payment_cap == 0.0 and s.daily_aggregate_cap == 0.0
    assert check_per_payment(100, s.per_payment_cap).ok is False
    assert check_daily_aggregate(100, 0, s.daily_aggregate_cap).ok is False


@pytest.mark.parametrize("value", BAD)
def test_bad_per_payment_cap_loads_and_blocks(monkeypatch, value):
    s = _load(monkeypatch, PER_PAYMENT_CAP=value)
    assert s.per_payment_cap == 0.0
    assert check_per_payment(100, s.per_payment_cap).ok is False


@pytest.mark.parametrize("value", BAD)
def test_bad_daily_cap_loads_and_blocks(monkeypatch, value):
    s = _load(monkeypatch, DAILY_AGGREGATE_CAP=value)
    assert s.daily_aggregate_cap == 0.0
    assert check_daily_aggregate(100, 0, s.daily_aggregate_cap).ok is False


def test_good_caps_parse(monkeypatch):
    s = _load(monkeypatch, PER_PAYMENT_CAP="20000", DAILY_AGGREGATE_CAP=" 50000.50 ")
    assert s.per_payment_cap == 20000.0 and s.daily_aggregate_cap == 50000.5


def test_other_settings_still_parse_as_before(monkeypatch):
    with pytest.raises(ValueError):
        _load(monkeypatch, IMAP_PORT="abc")


def test_per_payment_boundary_20000():
    assert check_per_payment("20000", 20000.0).ok is True
    assert check_per_payment("20000.00", 20000.0).ok is True
    assert check_per_payment("20000.01", 20000.0).ok is False


def test_daily_aggregate_boundary_50000():
    assert check_daily_aggregate("20000", "30000", 50000.0).ok is True
    assert check_daily_aggregate("20000.01", "30000", 50000.0).ok is False
    assert check_daily_aggregate("0.01", "50000", 50000.0).ok is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "abc", "NaN", "Infinity", None, ""])
def test_non_finite_amount_is_blocked_not_mapped_to_zero(bad):
    r = check_per_payment(bad, 20000.0)
    assert r.ok is False and r.reason == "invalid amount"
    r = check_daily_aggregate(bad, 0, 50000.0)
    assert r.ok is False and r.reason == "invalid amount"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "abc", None, ""])
def test_non_finite_today_total_is_blocked(bad):
    r = check_daily_aggregate(100, bad, 50000.0)
    assert r.ok is False and r.reason == "invalid amount"


def test_cap_helper_maps_bad_caps_to_zero_and_leaves_amount_conversion_alone():
    for bad in ("abc", float("nan"), float("inf"), float("-inf"), -1, "-0.01", None, ""):
        assert caps._cap(bad) == Decimal(0), bad
    assert caps._cap(100) == Decimal(100)
    assert caps._cap("20000.50") == Decimal("20000.50")
    # _d is untouched: it still converts blindly (and still raises on junk)
    assert caps._d("5") == Decimal("5")
    with pytest.raises(InvalidOperation):
        caps._d("abc")
    assert math.isnan(float(caps._d("nan")))
    # _amount: None for unparsable or non-finite, never 0
    for bad in ("abc", float("nan"), float("inf"), None, ""):
        assert caps._amount(bad) is None, bad
    assert caps._amount("1.5") == Decimal("1.5")
    assert caps._amount(0) == Decimal(0)


def test_finite_results_unchanged():
    assert check_per_payment(0, 100).reason == "non-positive amount"
    assert check_per_payment(-1, 100).reason == "non-positive amount"
    assert check_per_payment(101, 100).reason == "amount 101 exceeds per-payment cap 100"
    assert check_per_payment(1, 0).reason == "per-payment cap not configured (fail-closed)"
    assert check_daily_aggregate(1, 0, 0).reason == "daily-aggregate cap not configured (fail-closed)"
    assert check_daily_aggregate(60, 50, 100).reason == "daily total 50 + 60 would exceed aggregate cap 100"


def test_env_example_documents_the_caps_and_the_r20000_caveat():
    text = (ROOT / ".env.example").read_text()
    assert "PER_PAYMENT_CAP=20000" in text
    assert "DAILY_AGGREGATE_CAP=50000" in text
    assert "R20,000" in text and "unverified" in text.lower()


def test_code_has_no_non_zero_cap_fallback():
    src = (ROOT / "src" / "invespend" / "config.py").read_text()
    assert "20000" not in src and "50000" not in src
    assert "_opt_cap" in src
