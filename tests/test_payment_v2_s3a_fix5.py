"""Slice 3A fix round 5: a 200 is NEVER 'failed' (until the G2 sandbox run shows real Investec failure bodies);
'failed' only from a definite 4xx PaymentRejected. Plus: adjacent lower-case ISO code gate, control-character
mailbox refusal, scrub_message long-token / address rules.

Offline; fakes only."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.v2_harness import Env, mod, offered


def _o():
    return mod("outcome")


def _accepted(env, amount="100.00"):
    offered(env, amount)
    env.advance(15)
    env.reply("approve")
    env.cycle()
    env.advance(15)
    assert {r["status"] for r in env.rows()} == {"accepted"}


def _live(tmp_path, responder):
    env = Env(tmp_path, live=True)
    env.client.responder = responder
    _accepted(env)
    env.cycle()
    return env


def _emails(env, subject):
    return [m for m in env.smtp.sent if subject in str(m["Subject"])]


# ------------------------------------------------------------------ the classifier through execute()
REVIEW_BODIES = [
    {"data": {"ErrorMessage": "Payment made"}},
    {"data": {"ErrorMessage": "Pending"}},
    {"data": {"ErrorMessage": "Pending approval"}},
    {"data": {"ErrorMessage": "Gateway timeout"}},
    {"data": {"ErrorMessage": "Duplicate payment detected"}},
    {"data": {"ErrorMessage": "Succeeded"}},
    {"data": {"ErrorMessage": "Error: none"}},
    {"data": {"ErrorMessage": "Insufficient funds"}},
    {"data": {"TransferResponses": [{"TransactionId": "T1", "Status": "Processed"}]}},
    {"data": {"TransferResponses": [{"paymentId": "P1"}], "ErrorMessage": "Pending"}},
    {"data": {"TransferResponses": [{"confirmationNumber": "C1"}]}},
    {"data": {"TransferResponses": [{"id": 7}]}},
    {"data": {"TransferResponses": [{"Status": "Processed"}]}},
    {"data": {}}, {"data": []}, {"data": "ok"}, {"ok": True}, {},
]


@pytest.mark.parametrize("body", REVIEW_BODIES)
def test_e2e_every_unrecognised_200_is_needs_review_reserved_never_resent(tmp_path, body):
    env = _live(tmp_path, lambda: body)
    row = env.row()
    assert row["status"] == "needs_review" and row["daily_reserved"] is True
    assert env.store.daily_total(env.now) == Decimal("100.00")
    env.run_cycles(3)
    assert env.payment_calls() == 1
    env.advance(15)
    env.instruct()
    env.cycle()
    assert sorted(r["status"] for r in env.rows()) == ["needs_review", "parked"]
    assert [r for r in env.rows() if r["status"] == "parked"][0]["outcome_code"] == "possible_duplicate"
    assert env.payment_calls() == 1


def test_200_error_message_reaches_the_owner_email_sanitised_with_may_have_been_paid_wording(tmp_path):
    env = _live(tmp_path, lambda: {"data": {"ErrorMessage": "Insufficient funds for 1234567890 a@b.com api_key=ZZTOP99"}})
    mails = _emails(env, "Payment outcome unknown")
    assert len(mails) == 1 and not _emails(env, "Payment failed")
    text = str(mails[0].get_content())
    assert "Investec returned an error: Insufficient funds for" in text
    assert "It may not have been paid. Check Investec Online before sending again." in text
    assert "MAY HAVE BEEN PAID" in text
    for leak in ("1234567890", "a@b.com", "ZZTOP99"):
        assert leak not in env.everything_text()


def test_needs_review_email_without_a_provider_message_is_unchanged(tmp_path):
    env = _live(tmp_path, lambda: {"data": {"TransferResponses": [{"TransactionId": "T1"}]}})
    text = str(_emails(env, "Payment outcome unknown")[0].get_content())
    assert "Investec returned an error" not in text and "MAY HAVE BEEN PAID" in text


def test_200_with_exact_true_authorisation_flag_is_needs_authorisation_reserved(tmp_path):
    env = _live(tmp_path, lambda: {"data": {"AuthorisationRequired": "true", "ErrorMessage": "Pending"}})
    assert env.row()["status"] == "needs_authorisation" and env.row()["daily_reserved"] is True


@pytest.mark.parametrize("flag", [1, "yes", "True!"])
def test_200_with_unclear_authorisation_flag_is_needs_review(tmp_path, flag):
    env = _live(tmp_path, lambda: {"data": {"AuthorisationRequired": flag,
                                           "TransferResponses": [{"PaymentReferenceNumber": "PR1"}]}})
    assert env.row()["status"] == "needs_review" and env.row()["daily_reserved"] is True


def test_200_with_a_real_reference_is_executed(tmp_path):
    env = _live(tmp_path, lambda: {"data": {"TransferResponses": [{"PaymentReferenceNumber": "PR1", "Status": "Processed"}]}})
    assert env.row()["status"] == "executed" and env.payment_calls() == 1


@pytest.mark.parametrize("body", REVIEW_BODIES)
def test_parse_never_returns_failed_for_a_200(body):
    assert _o().parse_payment_response(body).status in ("unknown", "needs_authorisation", "success")
    assert _o().parse_payment_response(body).status == "unknown"


# ------------------------------------------------------------------ 4xx is the ONLY failed path (F40)
@pytest.mark.parametrize("msg", ["Insufficient funds", "Invalid beneficiary", "Daily limit exceeded"])
def test_e2e_4xx_rejection_is_failed_released_message_shown_no_resend(tmp_path, msg):
    def responder():
        raise _o().PaymentRejected(400, msg)
    env = _live(tmp_path, responder)
    row = env.row()
    assert row["status"] == "failed" and row["daily_reserved"] is False and row["outcome_message"] == msg
    assert env.store.daily_total(env.now) == Decimal("0.00")
    mails = _emails(env, "Payment failed")
    assert len(mails) == 1 and f"Investec message: {msg}" in str(mails[0].get_content())
    env.run_cycles(3)
    assert env.payment_calls() == 1
    env.advance(15)
    env.instruct()
    env.cycle()
    assert sorted(r["status"] for r in env.rows()) == ["awaiting_approval", "failed"]


def test_e2e_4xx_rejection_message_is_sanitised(tmp_path):
    def responder():
        raise _o().PaymentRejected(400, "Account 1234567890 rejected Bearer abc.def.ghi a@b.com api_key=ZZTOP99")
    env = _live(tmp_path, responder)
    assert env.row()["status"] == "failed"
    for leak in ("1234567890", "abc.def.ghi", "a@b.com", "ZZTOP99"):
        assert leak not in env.everything_text()


@pytest.mark.parametrize("exc", [lambda: _o().PaymentUnknownOutcome("HTTP 500"), lambda: TimeoutError("t"),
                                 lambda: _o().PaymentUnknownOutcome("HTTP 408"), lambda: _o().PaymentUnknownOutcome("HTTP 429")])
def test_e2e_500_timeout_408_429_stay_needs_review(tmp_path, exc):
    def responder():
        raise exc()
    env = _live(tmp_path, responder)
    assert env.row()["status"] == "needs_review" and env.row()["daily_reserved"] is True
    assert env.payment_calls() == 1


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_client_408_429_5xx_are_unknown_outcome_not_rejected(monkeypatch, status):
    from tests.test_payment_client_hardening import Rig, mk_resp
    rig = Rig(monkeypatch, write=lambda: mk_resp(status, {"message": "Insufficient funds"}))
    with pytest.raises(_o().PaymentUnknownOutcome):
        rig.pay()
    assert len(rig.write_calls) == 1


# ------------------------------------------------------------------ nit 1: adjacent lower / mixed-case ISO code
@pytest.mark.parametrize("line", ["Amount: 100 sek", "R100 usd", "amount 100 nok", "Amount: 100 Sek", "Amount: R100 brl",
                                  "Amount: 100sek", "Amount: 100  nok"])
def test_adjacent_lower_case_iso_code_is_foreign(line):
    assert mod("amounts").has_foreign_currency_token(line) is True


@pytest.mark.parametrize("line", ["Amount: R500 for rent", "R100 per month", "Amount: R100 rand", "Amount: 100 for the user",
                                  "Amount: 100\nsek", "Amount: 100 and then sek", "Reference: INV7 top floor", "Amount: R100 all in",
                                  "R200 try again", "Amount: R100 Bob", "Amount: R100 top up"])
def test_ordinary_lower_case_words_still_pass(line):
    assert mod("amounts").has_foreign_currency_token(line) is False


@pytest.mark.parametrize("line", ["Amount: 100 sek", "R100 usd", "amount 100 nok"])
def test_e2e_adjacent_lower_case_iso_code_parks_currency_conflict(tmp_path, line):
    env = Env(tmp_path)
    env.instruct("pay 123\nPayee: Acme Trading\n" + line + "\n")
    summary = env.cycle()
    assert summary["results"].get("parked_currency_conflict") == 1, summary["results"]
    assert env.rows() == [] and env.payment_calls() == 0


# ------------------------------------------------------------------ nit 2: control characters in the mailbox name
@pytest.mark.parametrize("name", ["INBOX\x00", "Payments\x00", "Pay\x01ments", "\x1fPayments", "Pay\nments", "Pay\tments",
                                  "Payments\x7f", "INBOX\x00Payments", "Pay\rments"])
def test_mailbox_with_control_characters_is_refused(tmp_path, name):
    assert mod("cycle")._is_shared_inbox(name) is True
    env = Env(tmp_path, imap_mailbox=name)
    with pytest.raises(mod("cycle").PreflightError) as exc:
        env.cycle()
    assert "mailbox_not_dedicated" in str(exc.value) and env.inbox.fetch_calls == 0


# ------------------------------------------------------------------ nit 3: scrub_message long-token / address rules
@pytest.mark.parametrize("text", ["AIzaSyA-abcdefghijklmnopqrstuvwxyz12345", "ya29.a0AfH6SMBxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                                  "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=", "da39a3ee5e6b4b0d3255bfef95601890afd80709"])
def test_scrub_message_redacts_long_tokens(text):
    out = mod("v2_cli").scrub_message(f"boom {text} end", SimpleNamespace())
    assert out.startswith("boom ") and out.endswith(" end") and "[redacted]" in out
    assert text[8:30] not in out


def test_scrub_message_redacts_addresses_and_grouped_digits_but_keeps_ordinary_text():
    out = mod("v2_cli").scrub_message("boom bob@example.com card 5169 1234 5678 9012 failed at step 12345", SimpleNamespace())
    assert "bob@example.com" not in out and "5169" not in out and "9012" not in out
    assert "boom" in out and "failed at step 12345" in out
