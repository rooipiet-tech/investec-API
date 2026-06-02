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
    assert fields["current_balance"] == 12345.67
    assert fields["available_balance"] == 12000.00
    assert fields["currency"] == "ZAR"


def test_missing_fields_become_none_and_default_currency():
    fields = extract_balance_fields({"currentBalance": 100.0})
    assert fields["current_balance"] == 100.0
    assert fields["available_balance"] is None
    assert fields["cash_balance"] is None
    assert fields["currency"] == "ZAR"  # default when absent
