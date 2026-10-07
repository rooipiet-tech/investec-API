"""Slice 2 fix round R2: validate_extraction is total and amounts are in (0, 1_000_000.00]."""
from __future__ import annotations

import importlib

import pytest

from tests.timing_helper import assert_fast

pytestmark = pytest.mark.xfail(strict=False, reason="S2 fix R2 red: images.py amount bounds not built yet")


def images():
    return importlib.import_module("invespend.payments.images")


def amount_of(value):
    fields, _, _ = images().validate_extraction({"payee_name": "Acme", "amount": value})
    return None if fields is None else fields.amount


HUGE = [10**5000, -(10**5000), 10**13, 10**12, 1e30, 1e300, float("inf"), float("-inf"), float("nan"),
        "9" * 5000, "1" * 40, "1000000.01", "1 000 000.01", "0", "0.00", "0,00", "00", "-5", -5, -5.0, 0, 0.0,
        -0.0, True, False, 1000001, 1000000.01, "9999999999999.00", "1e30", "NaN", "inf", ""]


@pytest.mark.parametrize("value", HUGE, ids=[f"v{i}" for i in range(len(HUGE))])
def test_hostile_or_out_of_range_amount_is_dropped_never_raises(value):
    assert amount_of(value) is None


@pytest.mark.parametrize("value", HUGE, ids=[f"v{i}" for i in range(len(HUGE))])
def test_run_extractor_never_raises(value):
    im = images()

    class Engine:
        def extract(self, image):
            return {"payee_name": "Acme", "amount": value, "account_number": "1234567890"}

    ref, reason = im.image_ref_from_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 20)
    assert reason == ""
    out = im.run_extractor(Engine(), ref)
    assert out.fields is not None and out.fields.amount is None and out.fields.payee_name == "Acme"


@pytest.mark.parametrize("value,expected", [
    ("100.00", "100.00"), (100, "100.00"), (100.0, "100.00"), ("1 234.50", "1234.50"), ("1,234.50", "1234.50"),
    ("0.01", "0.01"), (1, "1.00"), (1000000, "1000000.00"), ("1000000.00", "1000000.00"),
    (999999.99, "999999.99"), (10**6, "1000000.00"),
])
def test_in_range_amounts_still_accepted(value, expected):
    assert amount_of(value) == expected


@pytest.mark.parametrize("value", ["100.5", "1.234", "1e3", "١٠٠.٠٠"])
def test_amount_shape_rejection_kept(value):
    assert amount_of(value) is None


def test_zar_and_currency_normalisation_kept():
    im = images()
    for cur in ("R", "zar", "Rand", "ZAR"):
        f, _, _ = im.validate_extraction({"amount": "10.00", "currency": cur})
        assert f.currency == "ZAR" and f.amount == "10.00"
    f, _, _ = im.validate_extraction({"amount": "10.00", "currency": "usd"})
    assert f.currency == "USD"


def test_validate_extraction_total_on_odd_mappings():
    im = images()

    class Boom(dict):
        def get(self, *a, **k):
            raise RuntimeError("boom")

    for raw in (Boom(), {"amount": object()}, {"amount": [1]}, {"amount": {"a": 1}}, {"amount": b"100.00"}):
        got = im.validate_extraction(raw)
        assert isinstance(got, tuple) and len(got) == 3


def test_amount_cap_is_documented():
    assert importlib.import_module("invespend.payments.images").MAX_AMOUNT == 1_000_000


@pytest.mark.parametrize("data", ["'9' * 200000", "'1 ' * 100000", "'1,' * 100000", "'0' * 200000 + '.00'"])
def test_amount_validation_is_linear(data):
    assert_fast(
        "from invespend.payments import images\n" f"data = {data}",
        "images.validate_extraction({'amount': data})",
    )
