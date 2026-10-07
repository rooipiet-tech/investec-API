"""Slice 3A fix round 3: the allow-list outcome classifier (F17/F40), currency gate, sanitiser, scrub, mailbox.

Offline; fakes only. 'failed' (reservation released, re-instructable) is reserved for a DEFINITE rejection."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.timing_helper import assert_fast
from tests.v2_harness import Env, mod, offered

pytestmark = pytest.mark.xfail(reason="fix3 red", strict=False)


def _o():
    return mod("outcome")


def _body(entries=None, **data):
    if entries is None:
        entries = [{"PaymentReferenceNumber": "PR1", "Status": "Processed"}]
    return {"data": {"TransferResponses": entries, **data}}


def _ent(**kw):
    return {"PaymentReferenceNumber": "PR1", "Status": "Processed", **kw}


def _cls(body):
    out = _o().parse_payment_response(body)
    return out.status


# ------------------------------------------------------------------ classifier table
IGNORED_ERRORS = [False, 0, [], {}, 0.0, None, True, "null", "None", "N/A", "OK", "no error", "No Errors", "false", "true", "nil",
                  "na", "0", "-", "Success", "successful", "", "  ", " NULL "]


@pytest.mark.parametrize("err", IGNORED_ERRORS)
def test_placeholder_or_non_string_error_with_reference_is_success_data_level(err):
    assert _cls(_body(ErrorMessage=err)) == "success"


@pytest.mark.parametrize("err", IGNORED_ERRORS)
def test_placeholder_or_non_string_error_with_reference_is_success_entry_level(err):
    assert _cls(_body([_ent(ErrorMessage=err)])) == "success"


@pytest.mark.parametrize("err", [False, 0, [], {}, 0.0, "null", "none", "n/a", "ok", "false", "no error", None])
def test_placeholder_error_without_reference_is_unknown_not_failed(err):
    out = _o().parse_payment_response({"data": {"TransferResponses": [{"Status": "Processed"}], "ErrorMessage": err}})
    assert out.status == "unknown" and out.reason == "no_reference"


@pytest.mark.parametrize("body", [
    {"data": {"TransferResponses": [], "ErrorMessage": "Insufficient funds"}},
    {"data": {"ErrorMessage": "Insufficient funds"}},
    {"data": {"TransferResponses": [{"Status": "x", "ErrorMessage": "Insufficient funds"}]}},
    {"data": {"TransferResponses": [{"PaymentReferenceNumber": "None", "ErrorMessage": "Insufficient funds"}]}},
    {"data": {"TransferResponses": [{"PaymentReferenceNumber": "  ", "ErrorMessage": "Insufficient funds"}]}},
    {"data": {"TransferResponses": [{"PaymentReferenceNumber": 12345, "ErrorMessage": "Insufficient funds"}]}},
])
def test_real_message_without_reference_is_failed(body):
    out = _o().parse_payment_response(body)
    assert (out.status, out.message) == ("failed", "Insufficient funds")
    assert out.reason in ("error_message", "entry_error_message")


@pytest.mark.parametrize("body", [
    _body(ErrorMessage="Insufficient funds"),
    _body([_ent(ErrorMessage="Insufficient funds")]),
    _body([_ent(), {"ErrorMessage": "Insufficient funds"}]),
])
def test_message_with_reference_is_unknown_ref_and_error(body):
    out = _o().parse_payment_response(body)
    assert out.status == "unknown"


@pytest.mark.parametrize("status", [
    "Processed - no errors", "processed without error", "Not declined", "No authorisation necessary for this payment",
    "No authorisation necessary - payment effective date 2026-10-07", "Payment effective date 2026-10-07", "Pending",
    "awaiting settlement", "Held", "Awaiting authorisation", "Authorization required", "Pending authorisation",
    "Failed payments are retried; this one succeeded", "unsuccessfully", "declined later?", "", None, 5, ["failed"], {"a": 1},
])
def test_status_wording_is_ignored_when_reference_present(status):
    entry = {"PaymentReferenceNumber": "PR1"}
    if status is not None:
        entry["Status"] = status
    out = _o().parse_payment_response(_body([entry]))
    assert (out.status, out.reference) == ("success", "PR1")


@pytest.mark.parametrize("status", ["failed", "FAILED", " Declined ", "rejected", "Unsuccessful", "unsuccessful payment",
                                    "Unsuccessful Payment"])
def test_exact_failure_status_with_reference_is_unknown_never_failed(status):
    out = _o().parse_payment_response(_body([_ent(Status=status)]))
    assert (out.status, out.reason) == ("unknown", "status_conflict")


@pytest.mark.parametrize("status", ["Pending", "awaiting settlement", "Pending authorisation", "Awaiting authorisation", "Failed",
                                    "Processed - no errors", "Not declined", "", None])
def test_status_without_reference_is_unknown_never_authorisation_or_failed(status):
    entry = {} if status is None else {"Status": status}
    out = _o().parse_payment_response(_body([entry]))
    assert (out.status, out.reason) == ("unknown", "no_reference")


@pytest.mark.parametrize("entries", [
    [_ent(), {"PaymentReferenceNumber": "PR2", "Status": "Failed"}],
    [_ent(), {"Status": "Processed"}],
    [_ent(), {}],
    [{}, _ent()],
    [_ent(), None],
    [_ent(), "x"],
])
def test_mixed_entries_are_unknown(entries):
    assert _cls(_body(entries)) == "unknown"


def test_all_entries_with_references_is_success():
    out = _o().parse_payment_response(_body([_ent(), {"PaymentReferenceNumber": "PR2"}]))
    assert (out.status, out.reference) == ("success", "PR1")


@pytest.mark.parametrize("ref", [None, "", "  ", "none", "NULL", "N/A", "na", "nil", "0", "false", "-", "OK", " ok ", 0, False, 123, [], {}])
def test_placeholder_or_non_string_reference_is_no_reference(ref):
    out = _o().parse_payment_response(_body([{"PaymentReferenceNumber": ref, "Status": "Processed"}]))
    assert (out.status, out.reason) == ("unknown", "no_reference")


@pytest.mark.parametrize("flag", [True, "true", "True", " TRUE "])
@pytest.mark.parametrize("where", ["data", "entry"])
def test_authorisation_required_true_is_needs_authorisation(flag, where):
    body = _body([_ent(AuthorisationRequired=flag)] if where == "entry" else None, **({"AuthorisationRequired": flag} if where == "data" else {}))
    out = _o().parse_payment_response(body)
    assert (out.status, out.reason) == ("needs_authorisation", "authorisation_required")


@pytest.mark.parametrize("flag", [False, "false", 0, 1, None, "yes", [], "True!"])
def test_authorisation_required_not_true_is_not_authorisation(flag):
    assert _cls(_body([_ent(AuthorisationRequired=flag)], AuthorisationRequired=flag)) == "success"


def test_authorisation_required_with_error_is_authorisation_kept():
    assert _cls(_body([{"AuthorisationRequired": True}], ErrorMessage="bad")) == "needs_authorisation"


@pytest.mark.parametrize("body", [
    None, "x", [], 5, [{"data": {}}], {}, {"data": None}, {"data": []}, {"data": "x"}, {"data": {}}, {"data": {"TransferResponses": {}}},
    {"data": {"TransferResponses": "x"}}, {"data": {"TransferResponses": None}}, {"data": {"TransferResponses": [None, 1, "x"]}},
    {"data": {"TransferResponses": []}}, {"data": {"ErrorMessage": ""}}, {"data": {"ErrorMessage": False}},
    {"data": {"TransferResponses": ({"PaymentReferenceNumber": "PR1"},)}}, {"error": "x"},
])
def test_weird_shapes_are_unknown_without_exceptions(body):
    out = _o().parse_payment_response(body)
    assert out.status == "unknown" and out.reason in ("unrecognised_shape", "no_reference")


def test_giant_bodies_are_unknown_or_bounded_without_exceptions():
    big = "x" * 300_000
    assert _cls({"data": {"TransferResponses": [{"PaymentReferenceNumber": big, "Status": big}]}}) == "success"
    assert _cls({"data": {"TransferResponses": [{"Status": big, "ErrorMessage": " " * 300_000}]}}) == "unknown"
    out = _o().parse_payment_response({"data": {"ErrorMessage": big}})
    assert out.status == "failed" and len(out.message) <= 200
    assert _cls({"data": {"TransferResponses": [{"PaymentReferenceNumber": "R"}] * 50_000}}) == "success"
    assert _cls({"data": {"TransferResponses": [{}] * 50_000}}) == "unknown"
    assert _cls({"data": {"TransferResponses": [{"PaymentReferenceNumber": "R"}] * 3 + [{}]}}) == "unknown"


def test_failed_message_is_sanitised():
    out = _o().parse_payment_response({"data": {"ErrorMessage": "Acct 123456789012 refused for a@b.com api_key=SECRET123"}})
    assert out.status == "failed" and "123456789012" not in out.message and "a@b.com" not in out.message and "SECRET123" not in out.message


def test_the_classifier_never_returns_failed_when_a_reference_exists():
    import itertools
    errs = ["Insufficient funds", "x", False, None, "ok"]
    stats = ["Failed", "Rejected", "Processed", None, "Pending"]
    for err, st, auth in itertools.product(errs, stats, [None, False]):
        entry = {"PaymentReferenceNumber": "PR1", "ErrorMessage": err, "AuthorisationRequired": auth}
        if st is not None:
            entry["Status"] = st
        assert _cls(_body([entry], ErrorMessage=err)) != "failed"


# ------------------------------------------------------------------ end to end
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


def test_e2e_rs3a1_repro_executed_one_post_total_retained(tmp_path):
    env = _live(tmp_path, lambda: {"data": {"TransferResponses": [{"PaymentReferenceNumber": "PR1", "Status": "Processed"}], "ErrorMessage": ""}})
    assert env.row()["status"] == "executed" and env.payment_calls() == 1
    assert env.store.daily_total(env.now) == Decimal("100.00")
    env.run_cycles(3)
    assert env.payment_calls() == 1


@pytest.mark.parametrize("err", [False, 0, [], {}, "null", "OK", "no error", "false"])
def test_e2e_placeholder_error_with_reference_is_executed(tmp_path, err):
    env = _live(tmp_path, lambda: {"data": {"TransferResponses": [{"PaymentReferenceNumber": "PR1", "Status": "Processed"}], "ErrorMessage": err}})
    assert env.row()["status"] == "executed" and env.row()["daily_reserved"] is True and env.payment_calls() == 1


@pytest.mark.parametrize("body", [
    {"data": {"AuthorisationRequired": True, "TransferResponses": [{"PaymentReferenceNumber": "R", "Status": "x"}]}},
    {"data": {"TransferResponses": [{"PaymentReferenceNumber": "R", "AuthorisationRequired": "true"}]}},
])
def test_e2e_needs_authorisation_keeps_reservation_and_blocks_reinstruction(tmp_path, body):
    env = _live(tmp_path, lambda: body)
    row = env.row()
    assert row["status"] == "needs_authorisation" and row["daily_reserved"] is True
    assert env.store.daily_total(env.now) == Decimal("100.00") and env.payment_calls() == 1
    env.run_cycles(3)
    assert env.payment_calls() == 1
    env.advance(15)
    env.instruct()
    env.cycle()
    assert sorted(r["status"] for r in env.rows()) == ["needs_authorisation", "parked"]
    assert env.payment_calls() == 1 and env.store.daily_total(env.now) == Decimal("100.00")


@pytest.mark.parametrize("body", [
    {"data": {"TransferResponses": [{"Status": "Pending"}]}},
    {"data": {"TransferResponses": [{"PaymentReferenceNumber": "R", "Status": "Failed"}]}},
    {"data": {"TransferResponses": [{"PaymentReferenceNumber": "R"}], "ErrorMessage": "Insufficient funds"}},
    {"data": {"TransferResponses": [{"PaymentReferenceNumber": "R"}, {}]}},
    {}, None, "oops",
])
def test_e2e_unknown_is_needs_review_reservation_kept_never_resent_blocks_reinstruction(tmp_path, body):
    env = _live(tmp_path, lambda: body)
    row = env.row()
    assert row["status"] == "needs_review" and row["daily_reserved"] is True
    assert env.store.daily_total(env.now) == Decimal("100.00") and env.payment_calls() == 1
    env.run_cycles(3)
    assert env.payment_calls() == 1
    env.advance(15)
    env.instruct()
    env.cycle()
    assert sorted(r["status"] for r in env.rows()) == ["needs_review", "parked"]
    assert [r for r in env.rows() if r["status"] == "parked"][0]["outcome_code"] == "possible_duplicate"
    assert env.payment_calls() == 1


def test_e2e_identical_reinstruction_of_executed_payment_is_blocked(tmp_path):
    env = _live(tmp_path, lambda: {"data": {"TransferResponses": [{"PaymentReferenceNumber": "PR1"}]}})
    assert env.row()["status"] == "executed"
    env.advance(15)
    env.instruct()
    env.cycle()
    assert sorted(r["status"] for r in env.rows()) == ["executed", "parked"]
    assert [r for r in env.rows() if r["status"] == "parked"][0]["outcome_code"] == "possible_duplicate"
    assert env.payment_calls() == 1


def test_e2e_definite_failure_is_failed_released_and_reinstruction_allowed(tmp_path):
    env = _live(tmp_path, lambda: {"data": {"TransferResponses": [], "ErrorMessage": "Insufficient funds"}})
    row = env.row()
    assert row["status"] == "failed" and row["daily_reserved"] is False and row["outcome_message"] == "Insufficient funds"
    assert env.store.daily_total(env.now) == Decimal("0.00")
    env.advance(15)
    env.instruct()
    env.cycle()
    assert sorted(r["status"] for r in env.rows()) == ["awaiting_approval", "failed"]


def test_e2e_provider_secrets_do_not_reach_row_or_email(tmp_path):
    env = _live(tmp_path, lambda: {"data": {"TransferResponses": [], "ErrorMessage": "Account 1234567890 rejected Bearer abc.def.ghi key-77777 api_key=ZZTOP99"}})
    assert env.row()["status"] == "failed"
    text = env.everything_text()
    for leak in ("abc.def.ghi", "key-77777", "1234567890", "ZZTOP99"):
        assert leak not in text


def test_e2e_configured_secret_echoed_by_provider_is_redacted(tmp_path):
    env = Env(tmp_path, live=True)
    env.settings.investec_api_key = secret = "hunter2-pass-9"
    env.client.responder = lambda: {"data": {"TransferResponses": [], "ErrorMessage": f"rejected for {secret.upper()}"}}
    _accepted(env)
    env.cycle()
    assert env.row()["status"] == "failed" and secret not in env.everything_text().lower()


# ------------------------------------------------------------------ RS3AF-3 currency gate
@pytest.mark.parametrize("amount_line", [
    "Amount: R100 US", "Amount: 100 U S D", "Amount: 100 ＵＳＤ", "Amount: R100 USDT", "Amount: R100 USDC", "Amount: R100 BTC",
    "Amount: R100 ETH", "Amount: R100 yen", "Amount: R100 JPY", "Amount: R100 CNY", "Amount: R100 rmb", "Amount: R100 yuan",
    "Amount: R100 INR", "Amount: 100 rupees", "Amount: 100 rupee", "Amount: R100 MXN", "Amount: 100 pesos", "Amount: 100 peso",
    "Amount: 100 dólares", "Amount: 100 dolares", "Amount: 100 dolar", "Amount: 100 dólar", "Amount: 100 dollars",
    "Amount: 100 euros", "Amount: 100 pounds sterling", "Amount: R100 GBP", "Amount: R100 AUD", "Amount: R100 CAD",
    "Amount: R100 NZD", "Amount: R100 CHF", "Amount: R100 SGD", "Amount: R100 HKD", "Amount: R100 AED", "Amount: $100",
    "Amount: R100 €", "Amount: R100 £", "Amount: R100 ¥", "Amount: R100 ₹", "Amount: 100 U​S​D",
    "Amount: 100 USD", "Amount: 100 U  S  D", "Amount: R100 U.S.D", "Amount: R100 u s d", "Amount: R100 ＵＳ",
    "Amount: R100 U⁠SD", "Amount: R100 ＥＵＲ", "Amount: R100\nU S D 5",
])
def test_foreign_currency_variants_park_currency_conflict(tmp_path, amount_line):
    env = Env(tmp_path)
    env.instruct("pay 123\nPayee: Acme Trading\n" + amount_line + "\n")
    summary = env.cycle()
    assert summary["results"].get("parked_currency_conflict") == 1, summary["results"]
    assert env.rows() == [] and env.payment_calls() == 0


@pytest.mark.parametrize("amount_line", ["Amount: R100 per month", "Amount: R100 rand", "Amount: ZAR 100", "Amount: R100.00",
                                         "Amount: R100 for the user manual", "Amount: R100 a b c", "Amount: R100 for the use"])
def test_zar_lines_stay_fine(tmp_path, amount_line):
    env = Env(tmp_path)
    env.instruct("pay 123\nPayee: Acme Trading\n" + amount_line + "\n")
    summary = env.cycle()
    assert "parked_currency_conflict" not in summary["results"]
    assert env.row()["status"] == "awaiting_approval" and env.row()["amount"] == Decimal("100.00")


def test_foreign_gate_is_linear_200kb_hostile():
    for data in ("'U ' * 100000", "'a ' * 100000", "'\\u200b' * 200000", "'R100 ' * 40000", "'U-' * 100000"):
        assert_fast("from invespend.payments.amounts import has_foreign_currency_token as f\n"
                    f"data = {data}", "f(data[:20000]); f(data)")


# ------------------------------------------------------------------ RS3AF-4 sanitiser
@pytest.mark.parametrize("text,leak", [
    ("api_key=SECRET123 x", "SECRET123"), ("apikey=SECRET123", "SECRET123"), ("client_secret: s3cr3tv", "s3cr3tv"),
    ("passwd=hunter2 bad", "hunter2"), ("pwd: hunter2 bad", "hunter2"), ("access_token Abc123xyz", "Abc123xyz"),
    ("refresh_token=Rtok777 x", "Rtok777"), ("secret\nvalue9", "value9"), ("X-API-KEY: kkk999", "kkk999"),
    ("eyJhbGciOi.eyJzdWIiOiIx.SflKxwRJSMe", "eyJhbGciOi"), ("AKIAABCDEFGHIJKLMNOP is bad", "AKIAABCDEFGHIJKLMNOP"),
    ("ghp_abcdEFGH1234 oops", "ghp_abcdEFGH1234"), ("gho_abcdEFGH1234", "gho_abcdEFGH1234"), ("sk-abcdefgh12 x", "sk-abcdefgh12"),
    ("tok_ABCDEF12 x", "tok_ABCDEF12"), ("ｔｏｋｅｎ=FWtok1", "FWtok1"), ("Authorization: Bearer zzz.yyy", "zzz.yyy"),
    ("PASSWORD : p4ss", "p4ss"), ("my_secret_key=abc123", "abc123"),
])
def test_sanitize_redacts(text, leak):
    assert leak not in _o().sanitize_provider_message(text), text


def test_sanitize_configured_secret_values_any_case_and_split():
    out = _o().sanitize_provider_message("bad HUNTER2-pass and hunter2-\npass again", ["hunter2-pass"])
    assert "hunter2" not in out.lower()


@pytest.mark.parametrize("text", ["Insufficient funds", "Beneficiary must be paid once online first", "Payment declined by bank",
                                  "Error 42 on line 7 retry in 30 s"])
def test_sanitize_keeps_normal_messages(text):
    assert _o().sanitize_provider_message(text) == text


@pytest.mark.parametrize("data", ["'key ' * 100000", "'_' * 200000", "'a_' * 100000", "'token' * 40000", "'eyJ' * 70000",
                                  "'eyJabcde.' * 25000", "'AKIA' * 50000", "'secret' + ' ' * 200000 + 'x'", "'passw' * 40000",
                                  "'ghp_' * 50000", "'\\n' * 200000 + 'key'", "'sk-' * 70000"])
def test_sanitize_is_linear_on_hostile_200kb(data):
    assert_fast("from invespend.payments.outcome import sanitize_provider_message as f\n"
                f"data = {data}", "f(data); f(data, ['abcd', 'x' * 50])")


# ------------------------------------------------------------------ RS3AF-5 scrub_message
def test_scrub_message_folds_case_and_whitespace_and_bearer_rules():
    s = SimpleNamespace(backup_passphrase="hunter2-pass", investec_api_key="Key Value 77")
    cases = ["hunter2-\npass", "HUNTER2-PASS", "hunter2- pass", "key\tvalue  77", "KEY VALUE 77", "Bearer abc.def", "secret: topvalue"]
    for text in cases:
        out = mod("v2_cli").scrub_message(f"err {text} end", s)
        for leak in ("hunter2", "abc.def", "topvalue", "value", "VALUE"):
            assert leak.lower() not in out.lower().replace("key [redacted]", ""), (text, out)


def test_scrub_message_linear():
    assert_fast("from invespend.payments.v2_cli import scrub_message as f\nfrom types import SimpleNamespace\n"
                "s = SimpleNamespace(api_key='abcdefgh')\ndata = 'key ' * 50000 + '_' * 100000", "f(data, s)")


# ------------------------------------------------------------------ RS3AF-6 mailbox preflight
@pytest.mark.parametrize("name", ["ＩＮＢＯＸ", "IN​BOX", "INBOX​", "﻿INBOX", "INBOX:", "inbox:",
                                  "INBOX.INBOX", "INBOX/INBOX", "inbox.inbox.", "INBOX:INBOX", "I⁠NBOX.", "​INBOX."])
def test_inbox_lookalikes_refused_before_any_fetch(tmp_path, name):
    env = Env(tmp_path, imap_mailbox=name)
    with pytest.raises(mod("cycle").PreflightError) as exc:
        env.cycle()
    assert "mailbox_not_dedicated" in str(exc.value) and env.inbox.fetch_calls == 0


@pytest.mark.parametrize("name", ["INBOX.Payments", "Payments", "[Gmail]/Payments", "inbox-payments", "INBOX.INBOX.Payments", "Inbox/Pay"])
def test_sub_labels_still_allowed(tmp_path, name):
    env = Env(tmp_path, imap_mailbox=name)
    env.cycle()
    assert env.inbox.fetch_calls == 1
