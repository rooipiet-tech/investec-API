"""F22: the pre-existing CLI surface is frozen (S0).

Renders ``--help`` for the top level and every pre-existing subcommand and
compares byte-for-byte with the captures under ``tests/fixtures/cli_help/``.
The captures were taken at base 2eee10c under CPython 3.12 (argparse help text
changes between minors), so the comparison runs on 3.12 only. A new subcommand
or flag fails this test. Regenerate (3.12, ``COLUMNS=80``) only when a future
spec intentionally changes the CLI surface.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from invespend import cli

FIXTURES = Path(__file__).parent / "fixtures" / "cli_help"
SUBCOMMANDS = (
    "init-db", "ingest", "report", "statements", "backfill-hashes",
    "backup", "approve-payments", "pay-selftest",
)

needs_312 = pytest.mark.skipif(
    sys.version_info[:2] != (3, 12),
    reason="help fixtures are valid for CPython 3.12 only",
)


def _subparsers(parser):
    for action in parser._actions:
        if getattr(action, "choices", None) and hasattr(action, "_name_parser_map"):
            return action.choices
    raise AssertionError("no subparsers")


def _render(monkeypatch, name: str | None) -> str:
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    parser = cli.build_parser()
    if name is None:
        return parser.format_help()
    return _subparsers(parser)[name].format_help()


def _fixture(name: str | None) -> str:
    fname = "help__top.txt" if name is None else f"help_{name}.txt"
    return (FIXTURES / fname).read_text()


def test_subcommand_set_is_exactly_the_frozen_one():
    assert set(_subparsers(cli.build_parser())) == set(SUBCOMMANDS)


def test_all_nine_fixtures_exist():
    for name in (None, *SUBCOMMANDS):
        assert (FIXTURES / ("help__top.txt" if name is None else f"help_{name}.txt")).is_file()


@needs_312
@pytest.mark.parametrize("name", [None, *SUBCOMMANDS])
def test_help_matches_base_fixture(monkeypatch, name):
    assert _render(monkeypatch, name) == _fixture(name)


@needs_312
def test_help_fixtures_independent_of_shell_columns(monkeypatch):
    # The width is set INSIDE the render; a wide invoking shell changes nothing.
    monkeypatch.setenv("COLUMNS", "200")
    for name in (None, *SUBCOMMANDS):
        assert _render(monkeypatch, name) == _fixture(name)
