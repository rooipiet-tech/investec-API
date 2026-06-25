"""F6/F7: deterministic dedup key + pure cap decisions."""
from invespend.payments.caps import check_daily_aggregate, check_per_payment
from invespend.payments.dedup import dedup_key


def test_dedup_key_deterministic():
    a = dedup_key("msg-1", "100.00", "BEN1", "ZAR")
    b = dedup_key("msg-1", "100.00", "BEN1", "ZAR")
    assert a == b


def test_dedup_key_amount_canonicalized():
    assert dedup_key("m", "100", "B", "ZAR") == dedup_key("m", "100.00", "B", "ZAR")


def test_dedup_key_differs_per_payment():
    base = dedup_key("m", "100.00", "BEN1", "ZAR")
    assert base != dedup_key("m2", "100.00", "BEN1", "ZAR")
    assert base != dedup_key("m", "200.00", "BEN1", "ZAR")
    assert base != dedup_key("m", "100.00", "BEN2", "ZAR")
    assert base != dedup_key("m", "100.00", "BEN1", "USD")


def test_per_payment_cap_blocks_over():
    assert check_per_payment("1500", "1000").ok is False
    assert check_per_payment("999", "1000").ok is True


def test_per_payment_zero_cap_fails_closed():
    assert check_per_payment("1", "0").ok is False


def test_per_payment_non_positive_amount_blocked():
    assert check_per_payment("0", "1000").ok is False
    assert check_per_payment("-5", "1000").ok is False


def test_daily_aggregate_under_cap_pushing_over_blocked():
    # 800 already today + 300 = 1100 > 1000 -> blocked even though 300 < cap.
    assert check_daily_aggregate("300", "800", "1000").ok is False


def test_daily_aggregate_within_cap_ok():
    assert check_daily_aggregate("300", "600", "1000").ok is True


def test_daily_aggregate_zero_cap_fails_closed():
    assert check_daily_aggregate("1", "0", "0").ok is False
