from __future__ import annotations

import pytest

from invespend.payments.accounts import (
    resolve_source_account,
    resolve_source_unique,
    source_account_id,
    source_profile_id,
)
from invespend.payments.beneficiaries import (
    Beneficiary,
    resolve_beneficiary,
    resolve_beneficiary_strict,
)


def B(i, name, email="", match_names=()):
    return Beneficiary(beneficiary_id=i, name=name, email=email, match_names=list(match_names))


def test_ok_none_ambiguous():
    acme, bolt = B("1", "Acme Ltd"), B("2", "Bolt")
    assert resolve_beneficiary_strict("acme ltd", [acme, bolt]) == (acme, "ok")
    assert resolve_beneficiary_strict("Nobody", [acme, bolt]) == (None, "none")
    assert resolve_beneficiary_strict("Acme", [B("1", "Acme"), B("3", "ACME")]) == (None, "ambiguous")
    assert resolve_beneficiary_strict("", [acme]) == (None, "none")
    assert resolve_beneficiary_strict("   ", [acme]) == (None, "none")
    assert resolve_beneficiary_strict("acme ltd", []) == (None, "none")


def test_normalisation_parity_with_resolve_beneficiary_on_exact_names():
    entries = [B("1", " Acme Ltd "), B("2", "Bolt"), B("3", "Nut & Bolt")]
    for name in ["acme ltd", "ACME LTD", "  Bolt ", "nut & bolt", "Nut & Bolt"]:
        got, reason = resolve_beneficiary_strict(name, entries)
        assert reason == "ok"
        assert got == resolve_beneficiary(name, entries)


def test_strict_does_not_match_substring_or_partial_name():
    entries = [B("1", "Acme Ltd")]
    for needle in ("acme", "Acme Ltd Pty", "ltd", "cme l"):
        assert resolve_beneficiary_strict(needle, entries) == (None, "none"), needle


def test_strict_ignores_static_match_names():
    entries = [B("1", "Acme Ltd", match_names=["acme"])]
    assert resolve_beneficiary(" pay acme please", entries) is not None   # the old resolver substring-matches
    assert resolve_beneficiary_strict(" pay acme please", entries) == (None, "none")
    assert resolve_beneficiary_strict("acme", entries) == (None, "none")


def test_strict_exact_email_match_ok():
    entries = [B("1", "Acme", email="Pay@Acme.Test"), B("2", "Bolt")]
    got, reason = resolve_beneficiary_strict("pay@acme.test", entries)
    assert reason == "ok" and got.beneficiary_id == "1"


def test_strict_two_exact_emails_ambiguous():
    entries = [B("1", "Acme", email="x@a.test"), B("2", "Other", email="x@a.test")]
    assert resolve_beneficiary_strict("x@a.test", entries) == (None, "ambiguous")


def test_name_and_email_of_one_beneficiary_count_once():
    entry = B("1", "x@a.test", email="x@a.test")
    assert resolve_beneficiary_strict("x@a.test", [entry]) == (entry, "ok")


def test_old_resolver_cannot_tell_none_from_ambiguous_strict_can():
    two = [B("1", "Acme"), B("2", "Acme")]
    assert resolve_beneficiary("Acme", two) is None
    assert resolve_beneficiary("Zed", two) is None
    assert resolve_beneficiary_strict("Acme", two)[1] == "ambiguous"
    assert resolve_beneficiary_strict("Zed", two)[1] == "none"


# ---- source accounts ---------------------------------------------------------------------------
ACCOUNTS = [
    {"accountId": "A1", "accountNumber": "10011234567", "profileId": "P1"},
    {"accountId": "A2", "accountNumber": "10019999999", "profileId": "P1"},
    {"accountId": "A3", "accountNumber": "10018888567", "profileId": "P2"},
]


def test_unique_match_carries_account_id_and_profile_id():
    acc, reason = resolve_source_unique(ACCOUNTS, "999")
    assert reason == "ok"
    assert source_account_id(acc) == "A2" and source_profile_id(acc) == "P1"


def test_no_match():
    assert resolve_source_unique(ACCOUNTS, "000") == (None, "no_match")
    assert resolve_source_unique([], "123") == (None, "no_match")


def test_two_matches_in_two_profiles_is_ambiguous():
    assert resolve_source_unique(ACCOUNTS, "567") == (None, "ambiguous")


@pytest.mark.parametrize("bad", ["", "12", "1234", "abc", "12a", "١٢٣", " 123", None, 123])
def test_bad_last3(bad):
    assert resolve_source_unique(ACCOUNTS, bad) == (None, "bad_last3")


def test_snake_case_keys_and_profile_helper_default():
    acc = {"account_id": "S1", "account_number": "5550123"}
    assert resolve_source_unique([acc], "123") == (acc, "ok")
    assert source_profile_id(acc) == ""
    assert source_profile_id({"profile_id": 7}) == "7"


def test_existing_resolve_source_account_is_unchanged():
    assert resolve_source_account(ACCOUNTS, "999")["accountId"] == "A2"
    assert resolve_source_account(ACCOUNTS, "567") is None
