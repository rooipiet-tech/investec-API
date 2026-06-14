from decimal import Decimal

from invespend.db import extract_balance_fields


def test_maps_investec_balance_payload():
    payload = {
        "accountId": "ACC1",
        "currentBalance": 12345.67,
        "availableBalance": 12000.00,
        "budgetBalance": 0,
        "straightBalance": 12345.67,
        "cashBalance": 0,
        "currency": "ZAR",
    }
    fields = extract_balance_fields(payload)
    # Money is parsed to exact Decimal, not float (numeric columns, cent-exact
    # reconciliation), so the values carry the digits Investec sent.
    assert fields["current_balance"] == Decimal("12345.67")
    assert isinstance(fields["current_balance"], Decimal)
    assert fields["available_balance"] == Decimal("12000.00")
    assert fields["currency"] == "ZAR"


def test_string_amounts_parse_exactly():
    # The API sometimes sends amounts as strings; they must round-trip exactly.
    fields = extract_balance_fields({"currentBalance": "12345.67"})
    assert fields["current_balance"] == Decimal("12345.67")


def test_missing_fields_become_none_and_default_currency():
    fields = extract_balance_fields({"currentBalance": 100.0})
    assert fields["current_balance"] == Decimal("100.0")
    assert fields["available_balance"] is None
    assert fields["cash_balance"] is None
    assert fields["currency"] == "ZAR"  # default when absent
