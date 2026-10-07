"""Slice 3A fix round 4: broad reference detection, success-sounding errors, linear scrub, complete currency gate,
secret-word / vendor-prefix redaction, mailbox wildcard / UTF-7 refusal.

Offline; fakes only. 'failed' (reservation released, re-instructable) is reserved for a DEFINITE rejection; every
ambiguous 200 is 'unknown' (needs_review, reservation kept). Investec's 200 shape is unverified until the G2 gate."""
from __future__ import annotations

import random
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.timing_helper import assert_fast
from tests.v2_harness import Env, mod


def _o():
    return mod("outcome")


def _cls(body):
    return _o().parse_payment_response(body).status


# ------------------------------------------------------------------ RS3AF3-1 broad reference detection
REF_LIKE_KEYS = ["PaymentReferenceNumber", "paymentReference", "paymentreference", "ref", "REF", "reference_no", "Reference",
                 "payment_ref_id", "ＰaymentReference", "RefNo", "my-reference"]
REF_VALUES = ["P123", 123456, 1.5, ["x"], {"a": 1}, "ＡＢＣ12", "abc", True, "  R-77 "]


@pytest.mark.parametrize("key", REF_LIKE_KEYS)
@pytest.mark.parametrize("value", REF_VALUES)
@pytest.mark.parametrize("where", ["data", "entry", "nested", "list_in_data"])
def test_reference_like_content_with_error_is_never_failed(key, value, where):
    holder = {key: value, "ErrorMessage": "Insufficient funds"}
    if where == "data":
        body = {"data": {"TransferResponses": [], **holder}}
    elif where == "entry":
        body = {"data": {"TransferResponses": [holder]}}
    elif where == "nested":
        body = {"data": {"TransferResponses": [], "ErrorMessage": "Insufficient funds", "detail": {"x": [{"y": {key: value}}]}}}
    else:
        body = {"data": {"TransferResponses": [], "ErrorMessage": "Insufficient funds", "items": [{key: value}]}}
    out = _o().parse_payment_response(body)
    assert out.status == "unknown", (key, value, where)


@pytest.mark.parametrize("value", ["", "   ", None, False, 0, 0.0, [], {}, "None", "null", "N/A", "N/A.", "None.", "OK!", "ok.", "false",
                                   "-", "--", "...", "!!!", "​", "​‍﻿", "‏", "N​/A", "ＮＯＮＥ", " nil. "])
def test_placeholder_or_empty_reference_with_error_is_failed(value):
    body = {"data": {"TransferResponses": [{"PaymentReferenceNumber": value, "ErrorMessage": "Insufficient funds"}],
                     "reference": value}}
    out = _o().parse_payment_response(body)
    assert out.status == "failed" and out.message == "Insufficient funds", value


@pytest.mark.parametrize("msg", ["No error.", "Success.", "Payment processed successfully", "successful payment", "Payment completed",
                                 "Approved", "Request accepted", "Paid", "All done", "OK, no errors found", "Processed without errors",
                                 "no errors occurred", "Successfully submitted", "SUCCESS!", "Completed OK", "without error"])
def test_success_sounding_error_without_reference_is_unknown(msg):
    body = {"data": {"TransferResponses": [{"Status": "x", "ErrorMessage": msg}]}}
    assert _cls(body) == "unknown", msg
    assert _cls({"data": {"TransferResponses": [], "ErrorMessage": msg}}) == "unknown", msg


@pytest.mark.parametrize("msg", ["Insufficient funds", "Invalid beneficiary", "Daily limit exceeded", "Beneficiary not found",
                                 "Unsuccessful payment: account blocked", "Payment declined by bank"])
def test_real_failure_text_without_reference_is_failed(msg):
    out = _o().parse_payment_response({"data": {"TransferResponses": [], "ErrorMessage": msg}})
    assert out.status == "failed" and out.message == msg


def test_needs_authorisation_only_from_true():
    for flag in (True, "true", "TRUE", " true "):
        assert _cls({"data": {"AuthorisationRequired": flag, "TransferResponses": []}}) == "needs_authorisation"
    for flag in (1, "yes", "pending", ["x"]):
        out = _o().parse_payment_response({"data": {"AuthorisationRequired": flag, "ErrorMessage": "Insufficient funds"}})
        assert out.status == "unknown"
    assert _cls({"data": {"AuthorisationRequired": False, "ErrorMessage": "Insufficient funds"}}) == "failed"


_KEYS = ["PaymentReferenceNumber", "paymentReference", "ref", "reference_no", "ErrorMessage", "Status", "AuthorisationRequired",
         "TransferResponses", "data", "x", "Message", "authorizationRequired", "id", "Ref"]
_VALS = ["P1", "", "none", "N/A.", "Insufficient funds", "No error.", "Processed", "true", "false", True, False, 0, 1, 123, 2.5,
         None, "​", "!!!", "OK!", "Paid", "Invalid beneficiary", [], {}, ["a"], "TRUE"]


def _gen(rng, depth=0):
    if depth > 3 or rng.random() < 0.35:
        return rng.choice(_VALS)
    if rng.random() < 0.5:
        return [_gen(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return {rng.choice(_KEYS): _gen(rng, depth + 1) for _ in range(rng.randint(0, 5))}


def _meaningful(v):
    if isinstance(v, str):
        t = "".join(ch for ch in v if ch.isalnum()).casefold()
        return bool(t) and t not in {"none", "null", "na", "nil", "0", "false", "ok"}
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return bool(v)


def _has_ref_like(node):
    if isinstance(node, dict):
        return any(("ref" in str(k).casefold() and _meaningful(v)) or _has_ref_like(v) for k, v in node.items())
    if isinstance(node, list):
        return any(_has_ref_like(v) for v in node)
    return False


def _auth_false(v):
    if isinstance(v, str):
        return "".join(ch for ch in v if ch.isalnum()).casefold() in {"", "false", "0", "no", "none", "null", "na", "nil"}
    return v in (False, 0, None, [], {})


def _has_auth_key(node):
    if isinstance(node, dict):
        return any("authori" in str(k).casefold() and not _auth_false(v) or _has_auth_key(v) for k, v in node.items())
    if isinstance(node, list):
        return any(_has_auth_key(v) for v in node)
    return False


_TAME_KEYS = ["PaymentReferenceNumber", "ErrorMessage", "Status", "id", "x", "ref"]
_TAME_VALS = ["P1", "", "none", "Insufficient funds", "No error.", "Processed", "Invalid beneficiary", None, "OK!", 0, 12, "\u200b"]


def _tame_corpus(n=3000):
    rng = random.Random(77)
    for _ in range(n):
        ents = [{k: rng.choice(_TAME_VALS) for k in rng.sample(_TAME_KEYS, rng.randint(0, 3)) if k != "ref" or rng.random() < 0.1}
                for _ in range(rng.randint(0, 2))]
        data = {"TransferResponses": ents}
        if rng.random() < 0.4:
            data["ErrorMessage"] = rng.choice(_TAME_VALS)
        yield {"data": data}


def _corpus(n=4000):
    yield from _tame_corpus()
    rng = random.Random(20260507)
    for _ in range(n):
        data = {rng.choice(_KEYS): _gen(rng, 1) for _ in range(rng.randint(0, 6))}
        if rng.random() < 0.7:
            data["TransferResponses"] = [{rng.choice(_KEYS): _gen(rng, 2) for _ in range(rng.randint(0, 4))}
                                         for _ in range(rng.randint(0, 3))]
        yield {"data": data} if rng.random() < 0.95 else _gen(rng)


def test_corpus_invariants_i1_to_i3():
    seen = {"failed": 0, "success": 0, "unknown": 0, "needs_authorisation": 0}
    for body in _corpus():
        out = _o().parse_payment_response(body)               # I3: never raises
        seen[out.status] += 1
        data = body.get("data") if isinstance(body, dict) else None
        if out.status == "failed":                            # I1
            assert not _has_ref_like(data) and not _has_auth_key(data), body
        if out.status == "success":                           # I2
            assert any(isinstance(e, dict) and isinstance(e.get("PaymentReferenceNumber"), str)
                       and _meaningful(e["PaymentReferenceNumber"]) for e in data["TransferResponses"]), body
    assert seen["failed"] > 50 and seen["success"] > 50 and seen["unknown"] > 100, seen


def test_classifier_is_linear_on_large_bodies():                # I4
    assert_fast("from invespend.payments.outcome import parse_payment_response as f\n"
                "deep = {}\nn = deep\nfor _ in range(5000):\n    n['k'] = {}\n    n = n['k']\n"
                "wide = {'data': {'TransferResponses': [{'x%d' % i: ['a', {'ref': ''}] for i in range(20)} for _ in range(2000)], "
                "'ErrorMessage': 'Insufficient funds'}}\n"
                "big = {'data': {'ErrorMessage': 'a ' * 500000, 'PaymentReferenceNumber': '\\u200b' * 500000}}\n"
                "nested = {'data': {'ErrorMessage': 'x', 'd': deep}}",
                "f(deep); f(wide); f(big); f(nested)")


def _live(tmp_path, responder):
    env = Env(tmp_path, live=True)
    env.client.responder = responder
    env.instruct()
    env.cycle()
    env.advance(15)
    env.reply("approve")
    env.cycle()
    env.advance(15)
    env.cycle()
    return env


@pytest.mark.parametrize("body", [
    {"data": {"TransferResponses": [{"PaymentReferenceNumber": 123456, "ErrorMessage": "warn"}]}},
    {"data": {"paymentreference": "P9", "ErrorMessage": "Insufficient funds", "TransferResponses": []}},
    {"data": {"TransferResponses": [], "ErrorMessage": "No error."}},
    {"data": {"TransferResponses": [], "ErrorMessage": "Payment processed successfully"}},
    {"data": {"TransferResponses": [{"PaymentReferenceNumber": ["R1"], "ErrorMessage": "Insufficient funds"}]}},
])
def test_e2e_ambiguous_200_is_needs_review_reservation_kept(tmp_path, body):
    env = _live(tmp_path, lambda: body)
    row = env.row()
    assert row["status"] == "needs_review" and row["daily_reserved"] is True
    assert env.store.daily_total(env.now) == Decimal("100.00") and env.payment_calls() == 1
    env.advance(15)
    env.instruct()
    env.cycle()
    assert sorted(r["status"] for r in env.rows()) == ["needs_review", "parked"]
    assert env.payment_calls() == 1


def test_e2e_insufficient_funds_no_reference_is_failed_released_reinstructable(tmp_path):
    env = _live(tmp_path, lambda: {"data": {"TransferResponses": [], "ErrorMessage": "Insufficient funds"}})
    row = env.row()
    assert row["status"] == "failed" and row["daily_reserved"] is False
    assert env.store.daily_total(env.now) == Decimal("0.00")
    env.advance(15)
    env.instruct()
    env.cycle()
    assert sorted(r["status"] for r in env.rows()) == ["awaiting_approval", "failed"]


# ------------------------------------------------------------------ RS3AF3-2 linear scrub / redaction
@pytest.mark.parametrize("data", ["'eyJ' * 70000", "'_' * 200000", "'eyJabcde.' * 25000", "'SG.' * 70000", "'xoxb-' * 50000",
                                  "'password is ' * 20000", "'ke\\n' * 60000"])
def test_redactors_finish_fast(data):
    assert_fast("from invespend.payments.outcome import redact_secret_words as r, sanitize_provider_message as s\n"
                "from invespend.payments.v2_cli import scrub_message as m\nfrom types import SimpleNamespace\n"
                f"st = SimpleNamespace(api_key='abcdefgh')\ndata = {data}", "m(data, st); r(data); s(data); s(data, ['abcd'])")


# ------------------------------------------------------------------ RS3AF3-3 currency gate
ISO_SAMPLE = ["SEK", "NOK", "DKK", "BRL", "RUB", "KRW", "TRY", "AED", "SAR", "ILS", "THB", "MYR", "IDR", "PLN", "HUF", "CZK", "CHF",
              "NGN", "KES", "ISK", "EGP", "PHP", "VND", "UAH", "LSL", "NAD", "SZL", "BWP", "MUR", "ZMW", "XOF", "XAU", "TWD", "ARS"]
WORDS = ["kroner", "krone", "krona", "kronor", "reais", "rouble", "roubles", "ruble", "rubles", "dirham",
         "riyal", "shekel", "shekels", "baht", "ringgit", "rupiah", "zloty", "forint", "koruna", "dinar", "dinars", "naira", "bitcoin",
         "quid", "sterling", "shilling", "hryvnia"]


def _gate(text):
    return mod("amounts").has_foreign_currency_token(text)


@pytest.mark.parametrize("code", ISO_SAMPLE)
@pytest.mark.parametrize("fmt", ["Amount: R100 {c}", "Amount: {c} 100", "Amount: 100{c}", "Amount: {c}100", "pay 100 {c} please",
                                 "Amount: R100 ({c})"])
def test_iso_codes_park(code, fmt):
    assert _gate(fmt.format(c=code))


@pytest.mark.parametrize("code", ["sek", "nok", "dkk", "brl", "pln"])
def test_lowercase_codes_glued_to_amount_park(code):
    assert _gate(f"Amount: 100{code}") and _gate(f"Amount: {code}100")


@pytest.mark.parametrize("word", WORDS)
def test_currency_words_park(word):
    assert _gate(f"Amount: 100 {word}") and _gate(f"Amount: {word.upper()} 100")


@pytest.mark.parametrize("text", ["Amount: 100 real", "Amount: 100 won", "Amount: 100 lira", "Amount: 100 francs", "Amount: 5 franc", "Amount: R100 Franc Botha"])
def test_ambiguous_words_park_only_after_a_number(text):
    assert _gate(text)


@pytest.mark.parametrize("text", ["Amount: R100 U\nS\nD", "Amount: R100 U S D", "Amount: R100 E-U-R", "Amount: R100 S\nE\nK",
                                  "Amount: R100 s e k", "Amount: R100 K​R​W", "Amount: R100 ＳＥＫ", "Amount: R100 ЕUR",
                                  "Amount: R100 ₽", "Amount: R100 ₩", "Amount: R100 ₪", "Amount: R100 ₺",
                                  "Amount: R100 ₦", "Amount: R100 ₿", "Amount: R100 ฿", "Amount: R100 ﷼",
                                  "Amount: R100 ₱", "Amount: R100 ₫", "Amount: R100 ₴", "Amount: R100 ¢"])
def test_split_and_symbol_variants_park(text):
    assert _gate(text)


@pytest.mark.parametrize("text", ["Amount: R500 for rent", "Amount: R100 per month", "R100 Sun Co", "Amount: R100 Sunday lunch",
                                  "Amount: ZAR 100", "Amount: R100 rand", "Amount: R100.00", "Amount: R100 for the user manual",
                                  "Amount: R100 a b c", "pay Real Estate Agents Amount: R100", "we won the tender, Amount: R100",
                                  "Payee: Sunbird Trading Amount: R100", "Amount: R100 try again later",
                                  "Amount: R100 all good", "Amount: R100 Bob", "Amount: R100 top up", "ask us about it Amount: R100",
                                  "Amount: R100 SUN CO", "Amount: R100 ABC PTY LTD", "Amount: R100 INV7781", "Amount: R100 KFC"])
def test_no_false_positive_table(text):
    assert not _gate(text), text


def test_iso_list_is_complete_data_without_zar():
    amounts = mod("amounts")
    codes = amounts.ISO_4217_CODES
    assert len(codes) >= 150 and "ZAR" not in codes
    for c in ISO_SAMPLE + ["USD", "EUR", "GBP", "JPY", "CNY", "INR", "AUD", "CAD", "NZD", "HKD", "SGD", "MXN"]:
        assert c in codes


# ------------------------------------------------------------------ RS3AF3-4 scrub / sanitiser
@pytest.mark.parametrize("text,leak", [
    ("ke\ny=ZZtop9", "ZZtop9"), ("sec​ret=ZZtop9", "ZZtop9"), ("ｔｏｋｅｎ=ZZtop9", "ZZtop9"), ("pass\nword: ZZtop9", "ZZtop9"),
    ("password is hunter22", "hunter22"), ("password was hunter22", "hunter22"), ("token = : hunter22", "hunter22"),
    ("secret equals hunter22", "hunter22"), ("api key is: hunter22", "hunter22"), ("passwords are hunter22", "hunter22"),
    ("xoxb-1234-abcdefgh", "abcdefgh"), ("xoxp-1234-abcdefgh", "abcdefgh"), ("xapp-1-A0B-abcdefgh", "abcdefgh"),
    ("ASIAABCDEFGHIJKLMNOP", "ASIAABCDEFGHIJKLMNOP"), ("rk_live_abcd1234", "abcd1234"), ("sk_live_abcd1234", "abcd1234"),
    ("whsec_abcd1234", "abcd1234"), ("glpat-abcd1234efgh", "abcd1234efgh"), ("SG.abcd1234.efgh5678", "abcd1234"),
    ("npm_abcd1234efgh", "abcd1234efgh"), ("github_pat_abcd1234efgh", "abcd1234efgh"),
])
def test_sanitiser_and_scrub_redact(text, leak):
    assert leak not in _o().sanitize_provider_message(text), text
    assert leak not in mod("v2_cli").scrub_message(f"err {text} end", SimpleNamespace()), text


def test_scrub_folds_configured_secret_with_zero_width():
    s = SimpleNamespace(backup_passphrase="hunter2-pass")
    out = mod("v2_cli").scrub_message("bad hunter2-​pass here", s)
    assert "hunter2" not in out


@pytest.mark.parametrize("text", ["Insufficient funds", "Beneficiary must be paid once online first", "Payment declined by bank",
                                  "Error 42 on line 7 retry in 30 s", "Invalid beneficiary", "Daily limit exceeded",
                                  "The payment was rejected because it was too large"])
def test_sanitiser_keeps_normal_messages_intact(text):
    assert _o().sanitize_provider_message(text) == text


# ------------------------------------------------------------------ RS3AF3-5 mailbox preflight
@pytest.mark.parametrize("name", ["INBOX\\", "\\INBOX", "INB\\OX", "*", "%", "INBOX.*", "INBOX.%", "Pay*", "Pay%ments", "&AEkATgBCAE8AWA-",
                                  "&AEkATgBCAE8AWA-.", "&AEk-NBOX", "&AEkATgBCAE8AWA-/&AEkATgBCAE8AWA-"])
def test_mailbox_wildcards_backslash_and_utf7_inbox_refused(tmp_path, name):
    env = Env(tmp_path, imap_mailbox=name)
    with pytest.raises(mod("cycle").PreflightError) as exc:
        env.cycle()
    assert "mailbox_not_dedicated" in str(exc.value) and env.inbox.fetch_calls == 0


@pytest.mark.parametrize("name", ["Payments", "INBOX.Payments", "[Gmail]/Payments", "&AMk-t&AOk-", "Pay&-ments"])
def test_mailbox_sub_labels_and_other_utf7_allowed(tmp_path, name):
    env = Env(tmp_path, imap_mailbox=name)
    env.cycle()
    assert env.inbox.fetch_calls == 1
