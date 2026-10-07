from __future__ import annotations

import pytest

from invespend.payments.trigger import (
    PAY_TRIGGER_RE,
    TriggerResult,
    parse_trigger,
    strip_trigger,
)
from invespend.payments.amounts import marked_amount_candidates


@pytest.mark.parametrize("text,last3", [
    ("Pay123", "123"), ("PAY 123", "123"), ("pay 123", "123"), ("pay\t123", "123"),
    ("pay 123", "123"), ("hello\npay 123\nthanks", "123"), ("(pay 123)", "123"),
    # TG: sentence punctuation after the digits is fine
    ("Please pay 123.", "123"), ("pay 123, R500", "123"), ("pay 123.\nthanks", "123"),
    ("pay 123,", "123"), ("pay 123!", "123"), ("pay 123 pay 123", "123"),
])
def test_ok_table(text, last3):
    assert parse_trigger(text) == TriggerResult("ok", last3)


@pytest.mark.parametrize("text", [
    "pay\n123", "pay123abc", "pay 123x", "pay 123,000", "pay 123.45", "pay٣٢١",
    "pay abc", "payment 123", "repay 123", "xpay 123", "pay  123", "pay -123", "", "no trigger here",
    "Pay\r\n123", "pay 123a",
])
def test_none_table(text):
    assert parse_trigger(text) == TriggerResult("none", None)


@pytest.mark.parametrize("text", [
    "pay 12", "pay 1", "pay 1234", "pay 1234.", "pay 12345", "pay 12,", "Pay12", "pay 12_3",
])
def test_malformed_table(text):
    assert parse_trigger(text) == TriggerResult("malformed", None)


@pytest.mark.parametrize("text", ["pay 1234,000", "pay 1234.56", "pay 12x", "pay 1234x"])
def test_malformed_like_but_followed_by_alnum_is_none(text):
    assert parse_trigger(text) == TriggerResult("none", None)


def test_two_distinct_values_are_ambiguous_same_value_is_ok():
    assert parse_trigger("pay 123 and pay 456") == TriggerResult("ambiguous", None)
    assert parse_trigger("pay 123\npay 123") == TriggerResult("ok", "123")


def test_valid_trigger_next_to_malformed_is_ambiguous_never_guessed():
    assert parse_trigger("pay 123 or pay 1234") == TriggerResult("ambiguous", None)
    assert parse_trigger("pay 12 pay 123") == TriggerResult("ambiguous", None)


def test_digits_are_ascii_only_and_separator_is_not_newline():
    assert PAY_TRIGGER_RE.flags & __import__("re").ASCII
    assert parse_trigger("pay٣٢١").status == "none"
    assert parse_trigger("pay\n123").status == "none"


def test_subject_like_or_quoted_text_is_the_callers_concern_only_typed_text_is_parsed():
    # parse_trigger has a single text argument: nothing else can reach it.
    import inspect
    assert list(inspect.signature(parse_trigger).parameters) == ["typed_text"]


# ---- B2: strip_trigger ------------------------------------------------------------------
def test_strip_trigger_removes_only_the_span():
    assert strip_trigger("pay 123") == " "
    assert strip_trigger("please pay 123 R500 thanks") == "please   R500 thanks"
    assert strip_trigger("Pay123.") == " ."
    assert strip_trigger("nothing") == "nothing"


def test_strip_trigger_also_removes_malformed_spans():
    out = strip_trigger("a pay 12 b pay 1234 c")
    assert "12" not in out and "1234" not in out
    assert out.split() == ["a", "b", "c"]


def test_strip_trigger_leaves_none_cases_untouched():
    for text in ("pay 123,000", "repay 123", "pay\n123"):
        assert strip_trigger(text) == text


def test_trigger_digits_never_become_amount_candidate():
    for text in ("pay 123", "R500 pay 123", "pay 123 pay 1234", "amount: pay 123"):
        assert "123.00" not in marked_amount_candidates(strip_trigger(text))
        assert "1234.00" not in marked_amount_candidates(strip_trigger(text))
    assert marked_amount_candidates(strip_trigger("R500 pay 123")) == ["500.00"]


def test_pay_123_with_payee_in_quote_and_no_other_amount_does_not_produce_123():
    from invespend.payments.extract import extract_from_text
    typed = "pay 123"
    # the raw typed text WOULD yield R123.00 through the legacy extractor ...
    assert extract_from_text(typed).amount == "123.00"
    # ... which v2 never uses: stripped, then marked-only, gives nothing.
    assert marked_amount_candidates(strip_trigger(typed)) == []
