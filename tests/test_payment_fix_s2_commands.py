"""Slice 2 fix round R1: command lines split on '\\n' ONLY; control chars never widen a command."""
from __future__ import annotations

import email

import pytest

from tests.test_payment_commands import C, PC, V, content, parse
from tests.timing_helper import assert_fast


BAD = C("invalid", (), "bad_syntax")


@pytest.mark.parametrize("typed", [
    "approve\x0b1", "approve\x0c1 2", "cancel\x0b2", "cancel\x0c2", "approve\x1c1", "approve\x1d1",
    "approve\x1e1", "approve\x1f1", "approve\x001", "approve\x7f1", "approve\x0b", "approve\x0c",
    "cancel\x0b", "\x0bapprove", "approve 1\x0b", "approve\x1c", "cancel 2\x0c3",
])
def test_ascii_control_chars_are_bad_syntax_never_widened(typed):
    assert parse(typed) == BAD


@pytest.mark.parametrize("typed", [
    "approve\x851", "approve 1", "approve 1", "approve 1", "approve​1", "approve​",
    "approve  1", "approve 1", "approve﻿1", "cancel\x852", "cancel 2", "cancel 2",
    "approve⁠", "approve　 1",
])
def test_non_ascii_whitespace_variants_never_become_a_command(typed):
    cmd = parse(typed)
    assert cmd is None or cmd[0] == "invalid"
    assert cmd is None or cmd == BAD


def test_line_separator_after_verb_does_not_split_lines():
    assert PC(V("approve\x0bcancel")) == BAD
    assert PC(V("Thanks\x0capprove")) is None


def test_unicode_separators_in_raw_cancel_line_never_widen():
    for raw in ("cancel\x0b2", "cancel\x0c2", "cancel 2"):
        got = PC(V("", confident=False, raw=(raw,)))
        assert got is None or got[0] == "invalid"


@pytest.mark.parametrize("typed,expected", [
    ("approve", C("approve_all")), ("approve 1 3", C("approve", (1, 3))), ("cancel", C("cancel_all")),
    ("cancel 2", C("cancel", (2,))), ("Approve.", C("approve_all")), ("APPROVE 2!", C("approve", (2,))),
    ("approve\t1\t3", C("approve", (1, 3))), ("approve 1,3;", C("approve", (1, 3))),
    ("approve\r\nthanks", C("approve_all")), ("\r\n\r\napprove 1\r\n", C("approve", (1,))),
    ("  cancel  ", C("cancel_all")), ("\t approve \t", C("approve_all")),
])
def test_legitimate_grammar_still_green(typed, expected):
    assert parse(typed) == expected


def test_text_view_and_parser_agree_on_line_splitting():
    raw = "Subject: x\nContent-Type: text/plain\n\napprove\x0b1\nthanks\n"
    view = content.text_view(email.message_from_string(raw))
    assert "approve\x0b1" in view.typed_text.split("\n")
    assert PC(view) == BAD


@pytest.mark.parametrize("data", ["'approve\\x0b' * 25000", "'\\x0c' * 200000", "'approve\\x1c1\\n' * 25000"])
def test_parse_command_with_control_chars_is_linear(data):
    assert_fast(
        "from invespend.payments import content, commands\n"
        f"data = {data}\n"
        "view = content.TextView(typed_text=data, forwarded_text='', quoted_text='', raw_head_lines=(), "
        "confident=True, indicators=())",
        "commands.parse_command(view)",
    )
