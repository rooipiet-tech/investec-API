from __future__ import annotations

import pytest

from invespend.payments.amounts import labelled_payee_candidates, marked_amount_candidates
from invespend.payments.trigger import strip_trigger


def amounts(text):
    return marked_amount_candidates(strip_trigger(text))


def test_pay_123_payee_acme_ref_INV7781_no_instruction():
    assert amounts("pay 123\nPayee: Acme\nRef INV7781") == []


def test_payee_acme_R500_ref_INV2026_gives_500_00():
    text = "pay 123\nPayee: Acme\nR500\nRef INV2026"
    assert amounts(text) == ["500.00"]
    assert labelled_payee_candidates(text) == ["Acme"]


@pytest.mark.parametrize("text", ["unit 14", "invoice 512", "Flat 7B", "see 2026 note", "500", "123.45", "No. 99"])
def test_unit_14_and_invoice_512_never_candidates(text):
    assert marked_amount_candidates(text) == []


def test_amount_label_counts():
    assert marked_amount_candidates("Amount: 500") == ["500.00"]
    assert marked_amount_candidates("AMOUNT = 1 234,50") == ["1234.50"]
    assert marked_amount_candidates("amount: R750") == ["750.00"]
    assert marked_amount_candidates("the amount is 500") == []
    assert marked_amount_candidates("misamount: 500") == []


def test_ZAR_suffix_counts():
    assert marked_amount_candidates("500 ZAR") == ["500.00"]
    assert marked_amount_candidates("500.00ZAR") == ["500.00"]
    assert marked_amount_candidates("ZAR 500") == ["500.00"]
    assert marked_amount_candidates("500 ZARX") == []
    assert marked_amount_candidates("INV500 ZAR") == []
    assert marked_amount_candidates("INV-500 ZAR") == []


def test_alphanumeric_token_digits_never_count():
    for text in ("INV7781", "PAYR500", "xR500", "ABCZAR500", "R500x", "R500abc", "1R500"):
        assert marked_amount_candidates(text) == [], text


def test_thousand_separators_and_decimals_normalised():
    assert marked_amount_candidates("R1 234.56") == ["1234.56"]
    assert marked_amount_candidates("R1,234.56") == ["1234.56"]
    assert marked_amount_candidates("R1234.56") == ["1234.56"]
    assert marked_amount_candidates("R20 000") == ["20000.00"]
    assert marked_amount_candidates("R 100") == ["100.00"]
    assert marked_amount_candidates("r100") == ["100.00"]
    assert marked_amount_candidates("zar 100,50") == ["100.50"]
    assert marked_amount_candidates("R500, thanks") == ["500.00"]
    assert marked_amount_candidates("R500.") == ["500.00"]
    assert marked_amount_candidates("R500\nthanks") == ["500.00"]


def test_distinct_candidates_in_first_seen_order_duplicates_collapsed():
    assert marked_amount_candidates("R500 and again R500.00 then ZAR 20") == ["500.00", "20.00"]
    assert marked_amount_candidates("Amount: R500") == ["500.00"]


# ---- TR3-11 --------------------------------------------------------------------------------
def test_ref_R20261_is_not_an_amount():
    assert marked_amount_candidates("Ref: R20261") == []
    assert marked_amount_candidates("my reference R20261") == []
    assert marked_amount_candidates("Reference # R20261") == []
    assert marked_amount_candidates("Ref R20261 amount R500") == ["500.00"]


def test_R1234_dash_INV_is_not_an_amount():
    assert marked_amount_candidates("R1234-INV") == []
    assert marked_amount_candidates("R1234-") == []


def test_R2026_slash_is_not_an_amount():
    assert marked_amount_candidates("R2026/05") == []
    assert marked_amount_candidates("R2026/") == []


def test_six_digit_run_without_separators_not_an_amount():
    assert marked_amount_candidates("R123456") == []
    assert marked_amount_candidates("R1234567.50") == []
    assert marked_amount_candidates("Amount: 123456") == []
    assert marked_amount_candidates("123456 ZAR") == []
    assert marked_amount_candidates("R12345") == ["12345.00"]   # 5 digits is still a plain amount
    assert marked_amount_candidates("R123 456") == ["123456.00"]  # thousand separator: fine


def test_odd_decimal_tails_never_count():
    assert marked_amount_candidates("R500.5") == []
    assert marked_amount_candidates("R500,5x") == []


# ---- TR3-12 ----------------------------------------------------------------------------------
def test_payee_only_from_labelled_lines():
    text = "Hi Bob,\nplease pay Acme Corp R500\nBeneficiary: Widgets Ltd\nRegards Piet"
    assert labelled_payee_candidates(text) == ["Widgets Ltd"]
    assert labelled_payee_candidates("pay Acme R500") == []
    assert labelled_payee_candidates("Payee: Acme\nPayee: Acme\nbeneficiary = Zed") == ["Acme", "Zed"]
    assert labelled_payee_candidates("") == []
