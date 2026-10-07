"""S7: the ONE batch-command parser (pure function of a TextView)."""
from __future__ import annotations

import importlib

import pytest

pytestmark = pytest.mark.xfail(strict=False, reason="S7 red: payments/commands.py + content.py not built yet")


class _Lazy:
    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        return getattr(importlib.import_module(self._name), attr)


content = _Lazy("invespend.payments.content")
commands = _Lazy("invespend.payments.commands")


def V(typed="", *, confident=True, raw=(), forwarded="", quoted=""):
    return content.TextView(
        typed_text=typed, forwarded_text=forwarded, quoted_text=quoted,
        raw_head_lines=tuple(raw), confident=confident, indicators=(),
    )


def C(kind, numbers=(), reason="", source="typed"):
    """Expected value as a plain tuple (parametrize runs at collection, before the module exists)."""
    return (kind, tuple(numbers), reason, source)


def T(cmd):
    return None if cmd is None else (cmd.kind, cmd.numbers, cmd.reason, cmd.source)


def PC(view):
    return T(commands.parse_command(view))


def parse(typed, **kw):
    return PC(V(typed, **kw))


@pytest.mark.parametrize("typed,expected", [
    ("approve", C("approve_all")),
    ("Approve.", C("approve_all")),
    ("APPROVE", C("approve_all")),
    ("  approve  ", C("approve_all")),
    ("approve!", C("approve_all")),
    ("approve 1 3", C("approve", (1, 3))),
    ("Approve 2", C("approve", (2,))),
    ("Approve 1 3.", C("approve", (1, 3))),
    ("approve 1,3", C("approve", (1, 3))),
    ("approve 1, 3", C("approve", (1, 3))),
    ("approve 1 ,3", C("approve", (1, 3))),
    ("approve\t1\t3", C("approve", (1, 3))),
    ("approve 0", C("approve", (0,))),
    ("approve 9", C("approve", (9,))),
    ("cancel 2", C("cancel", (2,))),
    ("cancel 2, 3!", C("cancel", (2, 3))),
    ("cancel", C("cancel_all")),
    ("Cancel.", C("cancel_all")),
    ("\n\n  approve\n", C("approve_all")),
    ("approve\nThanks\nPiet", C("approve_all")),
    ("approve\n-- \ncancel 2", C("approve_all")),
    ("approve\n--\ncancel", C("approve_all")),
])
def test_command_grammar_table(typed, expected):
    assert parse(typed) == expected


@pytest.mark.parametrize("typed,reason", [
    ("approve x", "bad_syntax"), ("approve cancel", "bad_syntax"), ("approve 1 cancel 2", "bad_syntax"),
    ("approve please", "bad_syntax"), ("cancel please", "bad_syntax"), ("cancel 1 x", "bad_syntax"),
    ("approve 1-3", "bad_syntax"), ("approve 01", "bad_syntax"), ("approve 1;3", "bad_syntax"),
    ("approve 1 and 3", "bad_syntax"), ("approve all", "bad_syntax"), ("approve #1", "bad_syntax"),
    ("approve ,1", "bad_syntax"), ("approve 1,,3", "bad_syntax"), ("approve-1", "bad_syntax"),
    ("approve:", "bad_syntax"),
])
def test_bad_syntax_is_invalid(typed, reason):
    assert parse(typed) == C("invalid", (), reason)


def test_one_run_of_trailing_punctuation_is_not_a_separator():
    assert parse("approve 1;") == C("approve", (1,))
    assert parse("approve 1 3 ...") == C("approve", (1, 3))


def test_duplicates_and_ordering_normalised():
    assert parse("approve 3 1") == C("approve", (1, 3))
    assert parse("approve 1 1") == C("invalid", (), "duplicate_number")
    assert parse("cancel 2, 2") == C("invalid", (), "duplicate_number")
    assert parse("approve 1 x 1") == C("invalid", (), "bad_syntax")


@pytest.mark.parametrize("typed,expected", [
    ("approve 0", C("approve", (0,))), ("approve 999", C("approve", (999,))),
    ("approve 1000", C("invalid", (), "bad_syntax")), ("approve 01", C("invalid", (), "bad_syntax")),
    ("approve -1", C("invalid", (), "bad_syntax")), ("approve 1.5", C("invalid", (), "bad_syntax")),
    ("approve +1", C("invalid", (), "bad_syntax")),
    ("approve ١", None),   # Arabic-Indic digit: non-ASCII line is never a command
])
def test_item_number_table(typed, expected):
    assert parse(typed) == expected


@pytest.mark.parametrize("typed", [
    "thanks", "please approve", "do not approve", "do not approve, the amount is wrong",
    "I approve", "i will not approve", "approved", "approve_all", "disapprove", "cancelled",
    "", "   \n \n", "> approve", "аpprove", "apprоve", "approve 1", "approve ",
    "ok approve 1", "recancel",
])
def test_none_table(typed):
    assert parse(typed) is None


def test_approve_on_second_line_ignored():
    assert parse("Thanks\napprove") is None
    assert parse("\nThanks\n\napprove 1") is None


def test_do_not_approve_is_not_approve():
    assert parse("do not approve, the amount is wrong") is None
    assert parse("i will not approve") is None
    assert parse("don't approve") is None


def test_text_view_has_no_raw_first_line_and_unconfident_view_never_approves():
    assert not hasattr(V(""), "raw_first_line")
    for raw in ("approve", "approve 1", "approve 1 3", "Approve."):
        assert PC(V("", confident=False, raw=(raw,))) is None
    # even if a hand-built unconfident view carries typed text, it is not an approve source
    assert PC(V("approve", confident=False, raw=("approve",))) is None


def test_approve_in_quoted_text_ignored():
    assert PC(V("", quoted="approve", forwarded="approve 1")) is None
    assert PC(V("thanks", quoted="approve")) is None


def test_bare_cancel_is_cancel_all():
    assert parse("cancel") == C("cancel_all")
    assert parse("cancel\n") == C("cancel_all")


def test_bare_cancel_with_cancel_numbers_or_junk_is_bad_syntax():
    assert parse("cancel please") == C("invalid", (), "bad_syntax")
    assert parse("cancel 1 x") == C("invalid", (), "bad_syntax")


def test_approve_with_cancel_on_later_line_is_invalid():
    both = C("invalid", (), "both_verbs_in_text")
    assert parse("approve\ncancel 2") == both
    assert parse("cancel\napprove") == both
    assert parse("approve 1\nI might cancel 2 later") == both
    assert parse("cancel 2\nand approve 1") == both
    assert parse("approve\nplease do not cancel") == both
    # same verb below is fine; substrings are not whole words
    assert parse("approve\nI approve") == C("approve_all")
    assert parse("approve\nthe cancellation policy") == C("approve_all")


def test_both_verbs_scan_stops_at_signature_delimiter():
    assert parse("approve\n-- \ncancel 2") == C("approve_all")
    assert parse("approve\n--\ncancel") == C("approve_all")
    assert parse("approve\ncancel 2\n-- ") == C("invalid", (), "both_verbs_in_text")
    assert parse("cancel 2\n-- \nIf you approve this disclaimer") == C("cancel", (2,))
    # a dashed line that is not exactly the delimiter does not stop the scan
    assert parse("approve\n---\ncancel") == C("invalid", (), "both_verbs_in_text")


def test_unconfident_view_raw_first_line_cancel_is_honoured():
    got = PC(V("", confident=False, raw=("cancel 2",)))
    assert got == C("cancel", (2,), "", "raw_first_line")


def test_unconfident_view_raw_first_line_bare_cancel_is_cancel_all():
    got = PC(V("", confident=False, raw=("Cancel.",)))
    assert got == C("cancel_all", (), "", "raw_first_line")


def test_unconfident_view_raw_first_line_approve_is_none():
    assert PC(V("", confident=False, raw=("approve",))) is None
    assert PC(V("", confident=False, raw=("approve x",))) is None
    assert PC(V("", confident=False, raw=())) is None


def test_unconfident_raw_cancel_with_approve_on_later_raw_head_line_is_invalid():
    got = PC(V("", confident=False, raw=("cancel", "approve", "x")))
    assert got == C("invalid", (), "both_verbs_in_text", "raw_first_line")
    ok = PC(V("", confident=False, raw=("cancel", "thanks", "bye")))
    assert ok == C("cancel_all", (), "", "raw_first_line")


def test_unconfident_raw_cancel_bad_syntax_is_invalid_with_raw_source():
    got = PC(V("", confident=False, raw=("cancel please",)))
    assert got == C("invalid", (), "bad_syntax", "raw_first_line")


def test_confident_view_ignores_raw_head_lines_for_cancel():
    got = PC(V("cancel 1", confident=True, raw=("approve", "x")))
    assert got == C("cancel", (1,), "", "typed")
    assert PC(V("thanks", confident=True, raw=("cancel",))) is None


def test_image_text_never_reaches_parse_command():
    from tests.mail_helpers import make_msg
    from invespend.payments.images import FakeImageExtractor
    from invespend.payments import images as imgmod
    png = (__import__("pathlib").Path(__file__).parent / "fixtures" / "payments_v2" / "images" / "tiny.png").read_bytes()
    msg = make_msg(plain="see attached", inline=(("scan.png", "image", "png", png),))
    view = content.text_view(msg)
    ref, _ = imgmod.image_ref_from_bytes(png)
    fake = FakeImageExtractor({ref.sha256: {"payee_name": "approve", "reference": "approve 1", "action": "approve"}})
    imgmod.run_extractor(fake, ref)
    assert commands.parse_command(view) is None


def test_parse_command_is_pure_function_of_the_view():
    v = V("approve 1 3")
    assert PC(v) == PC(v) == C("approve", (1, 3))
    assert commands.parse_command(v) == commands.parse_command(v)
    import inspect
    assert list(inspect.signature(commands.parse_command).parameters) == ["view"]


def test_command_is_frozen_dataclass_with_documented_fields():
    import dataclasses
    assert [f.name for f in dataclasses.fields(commands.Command)] == ["kind", "numbers", "reason", "source"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        commands.Command("approve_all", ()).kind = "x"
    assert commands.Command("approve_all", ()) == commands.Command("approve_all", (), "", "typed")


def test_very_long_lines_are_invalid_not_slow():
    got = parse("approve " + "1 " * 5000)
    assert got.kind == "invalid"
    assert parse("approve" + " " * 50_000 + "x").kind == "invalid"
