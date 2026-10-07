"""Fix round (slice 1): outcome mapping, 408, sanitiser redaction and bounds."""
from __future__ import annotations

import pytest
import requests

from invespend.payments import outcome as o
from tests.test_payment_client_hardening import Rig, mk_resp
from tests.timing_helper import assert_fast



def _body(entry, **data):
    return {"data": {"TransferResponses": [entry], "ErrorMessage": None, **data}}


# ---- review R1: success needs a reference and a clean per-entry status ---------
@pytest.mark.parametrize("entry", [
    {},
    {"PaymentReferenceNumber": None},
    {"PaymentReferenceNumber": ""},
    {"PaymentReferenceNumber": "   "},
    {"AuthorisationRequired": False},
])
def test_no_reference_is_unknown_never_success(entry):
    out = o.parse_payment_response(_body(entry))
    assert out.status == "unknown" and out.reference is None and out.reason == "no_reference"


@pytest.mark.parametrize("status", ["Failed", "REJECTED", "Declined", "Unsuccessful", "Error", "Insufficient funds"])
def test_failed_entry_status_is_failed_even_with_reference(status):
    out = o.parse_payment_response(_body({"PaymentReferenceNumber": "REF1", "Status": status}))
    assert out.status == "failed" and out.reason == "entry_status"
    assert out.message == status


def test_failed_entry_status_without_reference_is_failed_not_unknown():
    out = o.parse_payment_response(_body({"Status": "Failed"}))
    assert out.status == "failed" and out.reason == "entry_status"


def test_entry_error_message_is_failed():
    out = o.parse_payment_response(_body({"PaymentReferenceNumber": "REF1", "ErrorMessage": "Beneficiary blocked"}))
    assert out.status == "failed" and out.reason == "entry_error_message"
    assert out.message == "Beneficiary blocked"


@pytest.mark.parametrize("status", ["Awaiting authorisation", "Authorization required", "Pending authorisation"])
def test_entry_status_authorisation_wording_is_needs_authorisation(status):
    out = o.parse_payment_response(_body({"PaymentReferenceNumber": "REF1", "Status": status}))
    assert out.status == "needs_authorisation" and out.reference == "REF1"


@pytest.mark.parametrize("status", [None, "", "Successful", "Processed", "Submitted"])
def test_reference_with_benign_or_absent_status_is_success(status):
    entry = {"PaymentReferenceNumber": "REF1", "AuthorisationRequired": False}
    if status is not None:
        entry["Status"] = status
    out = o.parse_payment_response(_body(entry))
    assert (out.status, out.reference, out.reason) == ("success", "REF1", "ok")


def test_any_bad_entry_prevents_success_in_multi_entry_response():
    body = {"data": {"ErrorMessage": None, "TransferResponses": [
        {"PaymentReferenceNumber": "A"}, {"Status": "Failed"}]}}
    assert o.parse_payment_response(body).status == "failed"
    body["data"]["TransferResponses"] = [{"PaymentReferenceNumber": "A"}, {}]
    assert o.parse_payment_response(body).status == "unknown"


def test_unknown_outcome_is_documented_in_module_docstring():
    assert "unknown" in (o.__doc__ or "") and "never" in (o.__doc__ or "").lower()


# ---- review R2: 408 is ambiguous -----------------------------------------------
def test_http_408_response_is_unknown_outcome(monkeypatch):
    rig = Rig(monkeypatch, write=lambda: mk_resp(408, {"message": "timeout"}))
    with pytest.raises(o.PaymentUnknownOutcome):
        rig.pay()
    assert len(rig.write_calls) == 1


def test_http_408_httperror_is_unknown_outcome(monkeypatch):
    err = requests.exceptions.HTTPError("x", response=mk_resp(408, {}))
    rig = Rig(monkeypatch, write=lambda: err)
    with pytest.raises(o.PaymentUnknownOutcome):
        rig.pay()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_other_4xx_still_definite_rejection(monkeypatch, status):
    rig = Rig(monkeypatch, write=lambda: mk_resp(status, {"message": "no"}))
    with pytest.raises(o.PaymentRejected):
        rig.pay()


# ---- review R3: grouped digits -------------------------------------------------
@pytest.mark.parametrize("text", [
    "Acct 1000-2000-3000 closed", "Acct 1000 2000 3000 closed", "1000 - 2000", "10-00-20-00-30",
    "a 1 2 3 4 5 6 b",
])
def test_grouped_digit_sequences_redacted(text):
    out = o.sanitize_provider_message(text)
    assert "1000" not in out and "2000" not in out and o.REDACTED in out


def test_short_numbers_and_small_groups_survive():
    assert o.sanitize_provider_message("Error 42 on line 7 retry in 30 s") == "Error 42 on line 7 retry in 30 s"
    assert o.sanitize_provider_message("code 12345 only") == "code 12345 only"


# ---- risk R2: bound before regex ------------------------------------------------
def test_sanitiser_is_linear_on_200kb_token_like_input():
    assert_fast("from invespend.payments.outcome import sanitize_provider_message as f\n"
                "data = 'a' * 200000 + '!'", "f(data)")


@pytest.mark.parametrize("data", ["'1 ' * 100000", "'x@' * 100000", "'1-' * 100000", "'a1' * 100000 + '!'"])
def test_sanitiser_is_linear_on_other_hostile_inputs(data):
    assert_fast("from invespend.payments.outcome import sanitize_provider_message as f\n"
                f"data = {data}", "f(data)")


def test_long_message_still_cut_to_200():
    out = o.sanitize_provider_message("word " * 5000)
    assert len(out) == 200
