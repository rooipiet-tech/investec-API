"""The ONE batch-command parser (S7, F14). Pure, stdlib only.

``parse_command(view)`` reads a ``TextView`` and nothing else.

* APPROVE comes ONLY from the first non-empty line of CONFIDENT typed text. There is no
  raw-first-line fallback: an unconfident view never approves.
* CANCEL (``cancel_all`` / ``cancel N..``) uses the same line when the view is confident
  and falls back to ``raw_head_lines[0]`` otherwise (cancel can only reduce payments).
* The line must be pure ASCII, is casefolded and loses ONE run of trailing ``. ! , ;``.
* Accepted: ``approve``, ``approve N M`` / ``N, M``, ``cancel``, ``cancel N M`` / ``N, M``
  (N is 0 or 1-3 digits, no leading zero). A recognised verb with any other shape is
  ``invalid/bad_syntax``; a repeated number is ``invalid/duplicate_number``; the OTHER verb
  as a whole word in the rest of the text is ``invalid/both_verbs_in_text`` (the scan stops
  at the RFC 3676 signature delimiter ``-- ``). Quoted, forwarded, subject, attachment and
  image text are never inspected. Range checking is the caller's job (it knows the batch).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .content import TextView

MAX_COMMAND_LINE = 1000

_VERB = re.compile(r"(approve|cancel)\b", re.ASCII)
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")   # every ASCII control char except TAB (a line has no \n)
_VERB_START = re.compile(r"(approve|cancel)", re.ASCII)
_NUMBER = re.compile(r"0|[1-9][0-9]{0,2}", re.ASCII)
_SEPARATOR = re.compile(r"[ \t]*,[ \t]*|[ \t]+")
_TRAILING = re.compile(r"[.!,;]+\Z")
_WORD = {"approve": re.compile(r"\bapprove\b", re.ASCII | re.IGNORECASE),
         "cancel": re.compile(r"\bcancel\b", re.ASCII | re.IGNORECASE)}


@dataclass(frozen=True)
class Command:
    kind: str                  # "approve_all" | "approve" | "cancel_all" | "cancel" | "invalid"
    numbers: tuple[int, ...]   # () for approve_all, cancel_all and invalid; ascending, unique
    reason: str = ""           # "" unless invalid: "bad_syntax" | "duplicate_number" | "both_verbs_in_text"
    source: str = "typed"      # "typed" | "raw_first_line" (only cancel kinds can come from the raw line)


def _first_line_and_rest(view: TextView) -> tuple[str, list[str], str, bool] | None:
    if view.confident:
        lines = view.typed_text.split("\n")   # '\n' ONLY: never str.splitlines (it splits on \x0b, \x0c, \x85 ...)
        for i, line in enumerate(lines):
            line = line.rstrip("\r")
            if line.strip(" \t"):
                return line, lines[i + 1:], "typed", True
        return None
    raw = list(view.raw_head_lines)
    if not raw:
        return None
    return raw[0], raw[1:], "raw_first_line", False


def _other_verb_below(rest: list[str], other: str) -> bool:
    pattern = _WORD[other]
    for line in rest:
        if line.rstrip() == "--":
            return False
        if pattern.search(line):
            return True
    return False


def parse_command(view: TextView) -> Command | None:
    picked = _first_line_and_rest(view)
    if picked is None:
        return None
    first, rest, source, may_approve = picked
    if not first.isascii():
        return None
    if _CONTROL.search(first):   # only space/tab may separate; never lets a verb widen to approve_all/cancel_all
        return Command("invalid", (), "bad_syntax", source) if _VERB_START.match(first.lstrip(" \t").casefold()) \
            or _VERB_START.match(_CONTROL.sub(" ", first).strip().casefold()) else None
    norm = _TRAILING.sub("", first.casefold().strip()).strip()
    m = _VERB.match(norm)
    if m is None:
        return None
    verb = m.group(1)
    if verb == "approve" and not may_approve:
        return None
    tail = norm[m.end():]

    def invalid(reason: str) -> Command:
        return Command("invalid", (), reason, source)

    numbers: tuple[int, ...] = ()
    if tail:
        if len(norm) > MAX_COMMAND_LINE or tail[0] not in " \t":
            return invalid("bad_syntax")
        tokens = _SEPARATOR.split(tail.strip(" \t"))
        if not all(_NUMBER.fullmatch(t) for t in tokens):
            return invalid("bad_syntax")
        values = [int(t) for t in tokens]
        if len(set(values)) != len(values):
            return invalid("duplicate_number")
        numbers = tuple(sorted(values))
    other = "cancel" if verb == "approve" else "approve"
    if _other_verb_below(rest, other):
        return invalid("both_verbs_in_text")
    if verb == "approve":
        return Command("approve" if numbers else "approve_all", numbers, "", source)
    return Command("cancel" if numbers else "cancel_all", numbers, "", source)
