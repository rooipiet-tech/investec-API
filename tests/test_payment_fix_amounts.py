"""Fix round (slice 1): amounts boundary tightening and linear-time bounds."""
from __future__ import annotations

import pytest

from invespend.payments import amounts as a
from invespend.payments.amounts import labelled_payee_candidates, marked_amount_candidates
from tests.timing_helper import assert_fast

XFAIL_F4 = pytest.mark.xfail(reason="red: risk R1", strict=False)
XFAIL_F7 = pytest.mark.xfail(reason="red: risk R4", strict=False)


# ---- risk R4: reference-like left boundaries ------------------------------------
@XFAIL_F7
@pytest.mark.parametrize("text", [
    "INV-R500", "PO/R500", "Order#R500", "ref.R500", "my_R500", "ABC-ZAR500", "a/R 500",
    "R\n500", "R\r\n500", "Amount: R\n500",
])
def test_reference_like_tokens_are_not_marked_amounts(text):
    assert marked_amount_candidates(text) == []


@pytest.mark.parametrize("text, expected", [
    ("Amount: R500", ["500.00"]),
    ("R 1,250.50", ["1250.50"]),
    ("ZAR 500", ["500.00"]),
    ("payee: Acme R500", ["500.00"]),
    ("please pay R500.", ["500.00"]),
    ("(R500)", ["500.00"]),
    ("total:R500", ["500.00"]),
    ("R\t500", ["500.00"]),
    ("500 ZAR", ["500.00"]),
    ("Amount: ZAR 99.95", ["99.95"]),
    ("Pay Acme\nR500", ["500.00"]),
])
def test_legitimate_marked_amounts_still_count(text, expected):
    assert marked_amount_candidates(text) == expected


# ---- risk R1: cap + bounded window ------------------------------------------------
@XFAIL_F4
def test_cap_constant_documented():
    assert a.MAX_TYPED_CHARS == 20_000
    assert "20" in (a.__doc__ or "") and "fail" in (a.__doc__ or "").lower()


@XFAIL_F4
def test_input_over_cap_fails_closed_no_candidates_at_all():
    text = "R500 " + "x " * (a.MAX_TYPED_CHARS // 2)
    assert len(text) > a.MAX_TYPED_CHARS
    assert marked_amount_candidates(text) == []
    # a payee-relevant region beyond the cap is never silently truncated away
    assert marked_amount_candidates("x " * (a.MAX_TYPED_CHARS // 2) + " R500") == []


@XFAIL_F4
def test_input_at_cap_is_still_parsed():
    text = "R500 " + "x" * (a.MAX_TYPED_CHARS - 5)
    assert len(text) == a.MAX_TYPED_CHARS
    assert marked_amount_candidates(text) == ["500.00"]


@XFAIL_F4
@pytest.mark.parametrize("data", ["'R1 ' * 70000", "'ref R1 ' * 30000", "'ZAR 1 ' * 40000",
                                  "'amount: R1 ' * 20000", "'1 ZAR' * 40000", "'payee: ' * 30000"])
def test_marked_amounts_and_payee_are_linear_on_200kb(data):
    assert_fast(
        "from invespend.payments.amounts import marked_amount_candidates as f, labelled_payee_candidates as g\n"
        f"data = {data}",
        "f(data); g(data)",
    )


@XFAIL_F4
def test_payee_candidates_over_cap_fail_closed():
    assert labelled_payee_candidates("Payee: Acme\n" + "x" * a.MAX_TYPED_CHARS) == []


def test_ref_label_guard_still_works_with_bounded_window():
    assert marked_amount_candidates("Ref: R20261") == []
    assert marked_amount_candidates("Reference R500") == []
    assert marked_amount_candidates("see the reference:   R500") == []
    assert marked_amount_candidates("ref R500 and then R700") == ["700.00"]
