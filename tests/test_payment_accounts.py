"""F3: last-3 selects the source account exactly; 0/>1 -> fail-closed."""
from invespend.payments.accounts import (
    resolve_source_account,
    source_account_id,
    source_account_last3,
)

ACCOUNTS = [
    {"accountId": "ACC1", "accountNumber": "10010900709"},
    {"accountId": "ACC2", "accountNumber": "10010900123"},
]


def test_unique_last3_resolves():
    acc = resolve_source_account(ACCOUNTS, "709")
    assert acc is not None
    assert source_account_id(acc) == "ACC1"
    assert source_account_last3(acc) == "709"


def test_zero_match_fails_closed():
    assert resolve_source_account(ACCOUNTS, "999") is None


def test_multiple_match_fails_closed():
    accs = [
        {"accountId": "A", "accountNumber": "111709"},
        {"accountId": "B", "accountNumber": "222709"},
    ]
    assert resolve_source_account(accs, "709") is None


def test_non_three_digit_fails_closed():
    assert resolve_source_account(ACCOUNTS, "70") is None
    assert resolve_source_account(ACCOUNTS, "7090") is None
    assert resolve_source_account(ACCOUNTS, "abc") is None
    assert resolve_source_account(ACCOUNTS, "") is None


def test_snake_case_keys_supported():
    accs = [{"account_id": "X", "account_number": "555000"}]
    acc = resolve_source_account(accs, "000")
    assert acc is not None
    assert source_account_id(acc) == "X"
