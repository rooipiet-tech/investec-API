"""Slice 3A fix round: RS3A-1 .. RS3A-7 plus the early caps check. Offline; fakes only."""
from __future__ import annotations

import math
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.v2_harness import Env, body_for, mod, offered


def _o():
    return mod("outcome")


def _entry(**kw):
    return {"PaymentReferenceNumber": "PR1", "Status": "Processed", **kw}


# ------------------------------------------------------------------ RS3A-1: outcome mapping
@pytest.mark.parametrize("blank", ["", " ",
                                   "  \t ", None, "MISSING"])
def test_blank_data_error_message_is_no_error(blank):
    data = {"TransferResponses": [_entry()]}
    if blank != "MISSING":
        data["ErrorMessage"] = blank
    out = _o().parse_payment_response({"data": data})
    assert (out.status, out.reference, out.reason) == ("success", "PR1", "ok")


@pytest.mark.parametrize("blank", ["", " ", None, "MISSING"])
def test_blank_entry_error_message_is_no_error(blank):
    e = _entry()
    if blank != "MISSING":
        e["ErrorMessage"] = blank
    out = _o().parse_payment_response({"data": {"TransferResponses": [e], "ErrorMessage": None}})
    assert out.status == "success"


def test_non_empty_error_message_is_unknown_never_failed():
    out = _o().parse_payment_response({"data": {"TransferResponses": [], "ErrorMessage": "Insufficient funds"}})
    assert (out.status, out.reason, out.message) == ("unknown", "error_message", "Insufficient funds")
    out = _o().parse_payment_response({"data": {"TransferResponses": [{"ErrorMessage": "Insufficient funds"}]}})
    assert out.status == "unknown"
    # fix round 3: with a payment reference an error is a conflict, never a definite failure
    out = _o().parse_payment_response({"data": {"TransferResponses": [_entry(ErrorMessage="Insufficient funds")]}})
    assert out.status == "unknown"


@pytest.mark.parametrize("body", [{}, {"data": {}}, {"data": {"TransferResponses": []}}, {"data": None}, None, [], "x",
                                  {"data": "x"}, {"error": "x"}, {"data": {"ErrorMessage": ""}}, {"data": {"ErrorMessage": "  "}}])
def test_unrecognised_shape_is_unknown_not_failed(body):
    out = _o().parse_payment_response(body)
    assert (out.status, out.reason) == ("unknown", "unrecognised_shape")


def test_entry_without_reference_stays_unknown_and_auth_unchanged():
    out = _o().parse_payment_response({"data": {"TransferResponses": [{"Status": "Processed"}]}})
    assert (out.status, out.reason) == ("unknown", "no_reference")
    out = _o().parse_payment_response({"data": {"AuthorisationRequired": True, "TransferResponses": []}})
    assert out.status == "needs_authorisation"


def _accepted(env):
    offered(env, "100.00")
    env.advance(15)
    env.reply("approve")
    env.cycle()
    env.advance(15)
    assert {r["status"] for r in env.rows()} == {"accepted"}


@pytest.mark.parametrize("blank", ["", " ", None])
def test_e2e_blank_error_message_200_is_executed_reservation_kept(tmp_path, blank):
    env = Env(tmp_path, live=True)
    env.client.responder = lambda: {"data": {"TransferResponses": [{"PaymentReferenceNumber": "PR1", "Status": "Processed"}],
                                             "ErrorMessage": blank}}
    _accepted(env)
    env.cycle()
    assert env.row()["status"] == "executed" and env.payment_calls() == 1
    assert env.store.daily_total(env.now) == Decimal("100.00") and env.row()["daily_reserved"] is True
    env.run_cycles(3)
    assert env.payment_calls() == 1


@pytest.mark.parametrize("body", [None, {}, {"error": "x"}, {"data": {}}, {"data": {"TransferResponses": []}}, "oops"])
def test_e2e_unrecognised_200_is_needs_review_kept_never_resent_blocks_reinstruction(tmp_path, body):
    env = Env(tmp_path, live=True)
    env.client.responder = lambda: body
    _accepted(env)
    env.cycle()
    row = env.row()
    assert row["status"] == "needs_review" and row["outcome_code"] == "unrecognised_shape"
    assert row["daily_reserved"] is True and env.store.daily_total(env.now) == Decimal("100.00")
    assert env.payment_calls() == 1
    env.run_cycles(3)
    assert env.payment_calls() == 1
    env.advance(15)
    env.instruct()                                         # the same payment again: the duplicate guard holds
    env.cycle()
    assert sorted(r["status"] for r in env.rows()) == ["needs_review", "parked"]
    assert [r for r in env.rows() if r["status"] == "parked"][0]["outcome_code"] == "possible_duplicate"
    assert env.payment_calls() == 1 and env.store.daily_total(env.now) == Decimal("100.00")


def test_e2e_non_empty_error_message_is_needs_review_reservation_kept(tmp_path):
    env = Env(tmp_path, live=True)
    env.client.responder = lambda: {"data": {"TransferResponses": [], "ErrorMessage": "Insufficient funds"}}
    _accepted(env)
    env.cycle()
    assert env.row()["status"] == "needs_review" and env.store.daily_total(env.now) == Decimal("100.00")


# ------------------------------------------------------------------ RS3A-2: currency words
FOREIGN_BODIES = [
    "Amount: 100 USD", "Amount: R100 EUR", "Amount: R100 per month USD", "Amount: R100\nUSD 50", "Amount: 100 dollars",
    "Amount: 100 euros", "Amount: 100 pounds", "Amount: 100 usd", "Amount: $100", "Amount: R100 €", "Amount: £100",
    "Amount: R100 GBP", "Amount: R100 AUD", "Amount: R100 CAD", "Amount: R100 NZD", "Amount: R100 CHF", "Amount: R100 JPY",
    "Amount: R100 CNY", "R100 (about 6 Dollars)", "Amount: R100\nnote\nUSD 50",
]


@pytest.mark.parametrize("amount_line", FOREIGN_BODIES)
def test_foreign_currency_token_parks_currency_conflict(tmp_path, amount_line):
    env = Env(tmp_path)
    env.instruct("pay 123\nPayee: Acme Trading\n" + amount_line + "\n")
    summary = env.cycle()
    assert summary["results"].get("parked_currency_conflict") == 1, summary["results"]
    assert env.rows() == [] and env.batch_emails() == [] and env.payment_calls() == 0


@pytest.mark.parametrize("amount_line", ["Amount: R100 per month", "Amount: 100", "Amount: ZAR 100", "R100 rand", "Amount: R100.00"])
def test_zar_only_text_is_not_a_conflict(tmp_path, amount_line):
    env = Env(tmp_path)
    env.instruct("pay 123\nPayee: Acme Trading\n" + amount_line + "\n")
    summary = env.cycle()
    assert "parked_currency_conflict" not in summary["results"]
    assert env.row()["status"] == "awaiting_approval" and env.row()["amount"] == Decimal("100.00")


# ------------------------------------------------------------------ RS3A-3: sanitiser
@pytest.mark.parametrize("text,leaks", [
    ("Account 1234567890 rejected Bearer abc.def.ghi key-77777", ["abc.def.ghi", "key-77777", "1234567890"]),
    ("denied: tok_SECRET99", ["tok_SECRET99", "SECRET99"]),
    ("sk-abcdefghijk1234 failed", ["sk-abcdefghijk1234"]),
    ("password=hunter2 rejected", ["hunter2"]),
    ("passphrase: correct-horse bad", ["correct-horse"]),
    ("secret : s3cr3t!", ["s3cr3t"]),
    ("token=abc123 x", ["abc123"]),
    ("Authorization: Basic dXNlcjpwdw== no", ["dXNlcjpwdw"]),
    ("api key 99887 bad", ["99887"]),
    ("BEARER xyz.123", ["xyz.123"]),
])
def test_sanitize_redacts_credentials(text, leaks):
    out = _o().sanitize_provider_message(text)
    for leak in leaks:
        assert leak not in out, (text, out)


def test_sanitize_keeps_plain_messages_and_is_linear():
    assert _o().sanitize_provider_message("Insufficient funds") == "Insufficient funds"
    assert _o().sanitize_provider_message("Beneficiary must be paid once online first") == "Beneficiary must be paid once online first"
    import time
    for nasty in ("key " * 600, "token" + " " * 1900 + "x", "Bearer " * 300, "password:" + " " * 1900, "sk-" * 700):
        start = time.perf_counter()
        _o().sanitize_provider_message(nasty)
        assert time.perf_counter() - start < 0.5


def test_e2e_provider_message_secrets_do_not_reach_row_or_email(tmp_path):
    env = Env(tmp_path, live=True)
    env.client.responder = lambda: {"data": {"TransferResponses": [],
                                             "ErrorMessage": "Account 1234567890 rejected Bearer abc.def.ghi key-77777"}}
    _accepted(env)
    env.cycle()
    assert env.row()["status"] == "needs_review"
    text = env.everything_text()
    for leak in ("abc.def.ghi", "key-77777", "1234567890"):
        assert leak not in text


# ------------------------------------------------------------------ RS3A-4: hold hours clamp
HOLD_CASES = ["1e308", "1e10", "-1", "nan", "inf", "abc", "8761", "0", "24", "8760", "1.5"]


def _expect_hold(raw):
    try:
        v = float(raw)
    except ValueError:
        return 24.0
    if not math.isfinite(v) or v < 0 or v > 24 * 365:
        return 24.0
    return v


_BIG = {"1e308", "1e10", "8761"}


def _hold_params(cases):
    return [pytest.param(c) if str(c) in _BIG or (isinstance(c, float) and math.isfinite(c) and c > 24 * 365) else c for c in cases]


@pytest.mark.parametrize("raw", _hold_params(HOLD_CASES))
def test_config_hold_hours_clamped(monkeypatch, raw):
    cfg = mod("mode").__name__ and __import__("invespend.config", fromlist=["x"])
    monkeypatch.setenv("PAYMENTS_HOLD_HOURS", raw)
    hours, fell_back = cfg._hold_hours("PAYMENTS_HOLD_HOURS")
    assert hours == _expect_hold(raw) and fell_back == (hours == 24.0 and raw not in ("24",))


def test_config_hold_hours_unset_and_blank_are_zero(monkeypatch):
    cfg = __import__("invespend.config", fromlist=["x"])
    monkeypatch.delenv("PAYMENTS_HOLD_HOURS", raising=False)
    assert cfg._hold_hours("PAYMENTS_HOLD_HOURS") == (0.0, False)
    monkeypatch.setenv("PAYMENTS_HOLD_HOURS", "")
    assert cfg._hold_hours("PAYMENTS_HOLD_HOURS") == (0.0, False)


@pytest.mark.parametrize("raw", _hold_params(HOLD_CASES + [1e308, 1e10, float("nan"), float("inf"), -1.0]))
def test_v2_settings_hold_hours_clamped(raw):
    value = mod("mode").v2_settings(SimpleNamespace(payments_hold_hours=raw)).hold_hours
    assert value == _expect_hold(str(raw))
    mod("mode").v2_settings(SimpleNamespace(payments_hold_hours=raw)).hold          # never OverflowError


def test_v2_settings_hold_blank_string_and_missing():
    assert mod("mode").v2_settings(SimpleNamespace()).hold_hours == 0.0
    assert mod("mode").v2_settings(SimpleNamespace(payments_hold_hours="")).hold_hours == 24.0


def test_huge_hold_does_not_abort_the_cycle(tmp_path):
    env = Env(tmp_path, payments_hold_hours=1e308)
    env.instruct()
    summary = env.cycle()
    assert summary["errors"] == 0 and "error" not in summary["results"]


# ------------------------------------------------------------------ RS3A-5: mailbox preflight
@pytest.mark.parametrize("name", [pytest.param(n) for n in ("INBOX.", "INBOX/", '"INBOX"', "'inbox'", '"INBOX."', "Inbox//")]
                         + [" inbox ", "inbox", "INBOX", ""])
def test_inbox_variants_refused_before_any_fetch(tmp_path, name):
    env = Env(tmp_path, imap_mailbox=name)
    with pytest.raises(mod("cycle").PreflightError) as exc:
        env.cycle()
    assert "mailbox_not_dedicated" in str(exc.value) and env.inbox.fetch_calls == 0 and env.client.beneficiary_calls == 0


@pytest.mark.parametrize("name", ["INBOX.Payments", "Payments", "[Gmail]/Payments", "inbox-payments"])
def test_dedicated_labels_allowed(tmp_path, name):
    env = Env(tmp_path, imap_mailbox=name)
    env.cycle()
    assert env.inbox.fetch_calls == 1


# ------------------------------------------------------------------ RS3A-6: empty beneficiary list
def test_empty_beneficiary_list_parks_like_unavailable_without_paste_email(tmp_path):
    env = Env(tmp_path, bootstrap=False)
    env.client.beneficiaries = []
    env.instruct()
    env.cycle()
    # bootstrap is still marked done on an empty list: the existing (unmodifiable) test
    # test_bootstrap_with_empty_list_then_first_added_beneficiary_is_recent_held... pins that behaviour
    assert [r["outcome_code"] for r in env.rows()] == ["beneficiary_list_unavailable"] and env.batch_emails() == []
    assert not [s for s in env.smtp.subjects() if "new payee" in s.lower()]


def test_non_empty_first_list_marks_bootstrap(tmp_path):
    env = Env(tmp_path, bootstrap=False)
    env.cycle()
    assert env.store.bootstrap_done() is True


# ------------------------------------------------------------------ RS3A-7: scrub_message names
def test_scrub_message_covers_passphrase_and_mail_users():
    s = SimpleNamespace(backup_passphrase="correct horse staple", imap_user="reader@example.org", smtp_user="mailer@example.org",
                        imap_password="pw-imap-1", unrelated="harmless text")
    out = mod("v2_cli").scrub_message("x correct horse staple reader@example.org mailer@example.org pw-imap-1 harmless text", s)
    for leak in ("correct horse staple", "reader@example.org", "mailer@example.org", "pw-imap-1"):
        assert leak not in out
    assert "harmless text" in out


# ------------------------------------------------------------------ item 8: caps checked early
@pytest.mark.parametrize("over", [{"per_payment_cap": 0.0}, {"per_payment_cap": None}, {"per_payment_cap": float("nan")},
                                  {"per_payment_cap": 0.0, "daily_aggregate_cap": 0.0}, {"per_payment_cap": -1.0},
                                  {"per_payment_cap": 0.0, "daily_aggregate_cap": None}])
def test_unset_cap_parks_caps_not_configured_nothing_offered_one_notice(tmp_path, over):
    env = Env(tmp_path, **over)
    env.instruct()
    env.cycle()
    row = env.row()
    assert row["status"] == "parked" and row["outcome_code"] == "caps_not_configured"
    assert env.batch_emails() == [] and env.payment_calls() == 0
    assert len([s for s in env.smtp.subjects() if "not actioned" in s]) == 1
    env.run_cycles(2)
    assert len([s for s in env.smtp.subjects() if "not actioned" in s]) == 1 and env.batch_emails() == []


def test_row_already_awaiting_is_parked_at_offer_time_when_caps_become_unset(tmp_path):
    env = Env(tmp_path)
    env.instruct()
    env.settings.per_payment_cap = 0.0
    env.cycle()
    assert env.row()["outcome_code"] == "caps_not_configured" and env.batch_emails() == []


def test_both_caps_set_offers(tmp_path):
    env = Env(tmp_path)
    env.instruct()
    env.cycle()
    assert env.row()["status"] == "awaiting_approval" and len(env.batch_emails()) == 1


def test_per_payment_boundary_unchanged(tmp_path):
    env = Env(tmp_path)
    env.instruct(body_for("20000.00"))
    env.cycle()
    assert env.row()["status"] == "awaiting_approval"
    env2 = Env(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path)
    env2.instruct(body_for("20000.01"))
    env2.cycle()
    assert env2.row()["outcome_code"] == "over_per_payment_cap" and env2.batch_emails() == []


@pytest.mark.parametrize("over", [{"daily_aggregate_cap": 0.0}, {"daily_aggregate_cap": None}])
def test_daily_cap_unset_parks_at_offer_time(tmp_path, over):
    env = Env(tmp_path, **over)
    env.instruct()
    env.cycle()
    assert env.row()["outcome_code"] == "caps_not_configured" and env.batch_emails() == []


def test_daily_cap_unset_after_offer_is_still_fail_closed_at_claim_nothing_posts(tmp_path):
    env = Env(tmp_path, live=True)
    env.instruct()
    env.cycle()
    assert env.row()["status"] == "awaiting_approval"
    env.settings.daily_aggregate_cap = 0.0   # claim-time re-check: cap removed after the offer
    env.advance(15)
    env.reply("approve")
    env.cycle()
    env.advance(15)
    env.cycle()
    assert env.row()["outcome_code"] == "daily_cap" and env.payment_calls() == 0
