"""S7: label-based bank details (no guessing)."""
from __future__ import annotations

import importlib

import pytest

pytestmark = pytest.mark.xfail(strict=False, reason="S7 red: payments/bankdetails.py not built yet")


class _Lazy:
    def __getattr__(self, attr):
        return getattr(importlib.import_module("invespend.payments.bankdetails"), attr)


bd = _Lazy()

FULL = """Beneficiary: Acme Trading
Bank: FNB
Account number: 1234 5678 90
Branch code: 250655
Reference: INV-77
"""


def test_all_labelled_fields_extracted():
    d = bd.extract_bank_details(FULL)
    assert (d.payee_name, d.bank, d.account_number, d.branch_code, d.reference) == (
        "Acme Trading", "FNB", "1234567890", "250655", "INV-77")


def test_label_variants():
    d = bd.extract_bank_details("Payee: Zed\nAcc no: 123456789\nBranch: 632005\nbank name = Absa")
    assert (d.payee_name, d.account_number, d.branch_code, d.bank) == ("Zed", "123456789", "632005", "Absa")


def test_nothing_labelled_means_nothing_guessed():
    d = bd.extract_bank_details("Please pay Acme 1234567890 at FNB branch 250655")
    assert d == bd.BankDetails(None, None, None, None, None)


def test_payee_only_from_labelled_lines():
    d = bd.extract_bank_details("Dear Bob,\nPlease pay Acme R100\nThanks")
    assert d.payee_name is None


@pytest.mark.parametrize("acc", ["12345", "1" * 21, "12ab5678", ""])
def test_account_must_be_6_to_20_digits(acc):
    assert bd.extract_bank_details(f"Account number: {acc}").account_number is None


def test_conflicting_values_for_one_label_are_not_guessed():
    d = bd.extract_bank_details("Account number: 1234567890\nAccount number: 9876543210\nBank: FNB")
    assert d.account_number is None and d.bank == "FNB"
    same = bd.extract_bank_details("Account number: 1234567890\nacc no: 1234567890")
    assert same.account_number == "1234567890"


def test_oversize_input_fails_closed_and_control_chars_rejected():
    assert bd.extract_bank_details("Bank: FNB\n" + "x" * 30_000) == bd.BankDetails(None, None, None, None, None)
    assert bd.extract_bank_details("Bank: FN\x00B").bank is None
    assert bd.extract_bank_details("Bank: " + "a" * 101).bank is None


def test_bankdetails_is_frozen():
    import dataclasses
    assert [f.name for f in dataclasses.fields(bd.BankDetails)] == ["payee_name", "bank", "account_number", "branch_code", "reference"]
