"""Slice 2 fix round R3: account numbers in third-party fields are masked whatever the separator."""
from __future__ import annotations

import re

import pytest

from tests.notify_cases import body, render_all
from tests.timing_helper import assert_fast


FORMS = ["1234/5678/9012", "1234.5678.9012", "1234_5678_9012", "1234 5678 9012", "acct 1234567890",
         "1234-5678-9012", "1234 - 5678 - 9012", "1234. 5678. 9012", "12/34/56/78/90", "1 2 3 4 5 6 7 8 9",
         "1234567890123456", "a1234567890b", "1234/ 5678 /9012"]


def digits_of(form):
    return re.sub(r"\D", "", form)


def leaks(text, form):
    """True when a 9+ digit run (after dropping separators) of the form still appears."""
    stripped = re.sub(r"[ \-./_]", "", text)
    return digits_of(form)[:9] in stripped


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("field", ["payee", "reference"])
def test_third_party_field_is_masked_in_every_non_paste_email(form, field):
    kw = {field: f"Acme {form}"} if field == "payee" else {field: f"INV {form}"}
    for name, msg in render_all(**kw).items():
        if name == "paste":
            continue
        text = body(msg) + "\n" + str(msg["Subject"])
        assert not leaks(text, form), (name, form)


@pytest.mark.parametrize("form", FORMS)
def test_batch_shows_hidden_marker(form):
    assert "[number hidden]" in body(render_all(payee=f"Acme {form}")["batch"])
    assert "[number hidden]" in body(render_all(reference=f"INV {form}")["batch"])


@pytest.mark.parametrize("form", FORMS)
def test_executed_ack_and_failed_emails_mask_the_payee(form):
    msgs = render_all(payee=f"Acme {form}")
    for name in ("outcome_success", "outcome_dry_run", "outcome_unknown", "beneficiary_observed", "expiry"):
        assert "[number hidden]" in body(msgs[name]), name
        assert not leaks(body(msgs[name]), form), name
    for name in msgs:
        if name.startswith("problem_"):
            assert not leaks(body(msgs[name]), form), name


def test_paste_details_email_still_shows_the_full_account_number():
    for account in ("1234567890", "1234 5678 9012", "1234-5678-9012"):
        text = body(render_all(account=account)["paste"])
        assert f"Account number: {account}" in text


def test_paste_details_email_does_not_mask_third_party_reference_either():
    text = body(render_all(reference="INV 1234/5678/9012")["paste"])
    assert "1234/5678/9012" in text   # the verified user's own details


@pytest.mark.parametrize("text", ["Acme Ltd", "INV 2026", "2026-10-07", "R 100", "invoice 12/34", "Unit 12 Main Rd 5"])
def test_short_digit_strings_are_not_masked(text):
    assert "[number hidden]" not in body(render_all(payee=text, reference=text)["batch"])


@pytest.mark.parametrize("data", ["'1 ' * 100000", "'1.' * 100000", "'1/' * 100000", "'1' + ' ' * 200000 + 'x'",
                                  "'1 x' * 70000", "'1.. ' * 50000", "'-' * 200000", "'1_' * 99999 + 'x'"])
def test_mask_is_linear(data):
    assert_fast(
        f"from invespend.payments import notify_v2\ndata = {data}",
        "notify_v2._clean(data, 400000)",
    )
