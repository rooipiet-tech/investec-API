"""A tiny YAML-subset loader for the repo's workflow files (PyYAML is not a dependency).

Supports what GitHub workflow files here use: block mappings, block sequences, plain / single- /
double-quoted scalars, ``key: |`` and ``>`` block scalars, ``{}`` / ``[]`` / simple flow lists,
comments. Everything is a string except ``{}`` and ``[...]``; booleans stay strings on purpose so
a literal ``true`` is detectable. The key ``on`` stays ``"on"`` (PyYAML would make it ``True``).
"""
from __future__ import annotations

import re

_KEY = re.compile(r"""^("[^"]*"|'[^']*'|[^\s'"#][^:#]*?)\s*:(?:\s+(.*))?$""")


def _strip_comment(text: str) -> str:
    out, quote = [], ""
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"" and (i == 0 or text[i - 1] in " \t[{,:"):
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _scalar(text: str):
    text = text.strip()
    if text == "{}":
        return {}
    if text == "[]":
        return []
    if text.startswith("[") and text.endswith("]"):
        return [_scalar(p) for p in text[1:-1].split(",") if p.strip()]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def _key(raw: str) -> str:
    raw = raw.strip()
    return raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"" else raw


def load(text: str):
    lines = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            lines.append(None)
        else:
            lines.append(raw)
    pos = [0]

    def indent(s: str) -> int:
        return len(s) - len(s.lstrip(" "))

    def peek():
        while pos[0] < len(lines) and lines[pos[0]] is None:
            pos[0] += 1
        return lines[pos[0]] if pos[0] < len(lines) else None

    def block_scalar(parent_indent: int, folded: bool) -> str:
        body = []
        while pos[0] < len(lines):
            raw = lines[pos[0]]
            if raw is None:
                body.append("")
                pos[0] += 1
                continue
            if indent(raw) <= parent_indent:
                break
            body.append(raw.strip())
            pos[0] += 1
        return (" " if folded else "\n").join(body).strip("\n") + ("" if folded else "\n")

    def parse(min_indent: int):
        raw = peek()
        if raw is None or indent(raw) < min_indent:
            return None
        base = indent(raw)
        if raw.strip().startswith("- ") or raw.strip() == "-":
            return parse_seq(base)
        return parse_map(base)

    def parse_seq(base: int) -> list:
        items = []
        while True:
            raw = peek()
            if raw is None or indent(raw) != base or not (raw.strip().startswith("- ") or raw.strip() == "-"):
                return items
            rest = raw.strip()[1:].lstrip()
            if not rest:
                pos[0] += 1
                items.append(parse(base + 1))
            elif _KEY.match(_strip_comment(rest)) and not rest.startswith(("'", '"', "{", "[")) or \
                    (rest[0] in "'\"" and _KEY.match(_strip_comment(rest))):
                lines[pos[0]] = " " * (base + 2) + rest        # re-read as a mapping inside the item
                items.append(parse_map(base + 2))
            else:
                pos[0] += 1
                items.append(_scalar(_strip_comment(rest)))

    def parse_map(base: int) -> dict:
        out: dict = {}
        while True:
            raw = peek()
            if raw is None or indent(raw) != base:
                return out
            m = _KEY.match(_strip_comment(raw.strip()))
            if not m:
                raise ValueError("yaml_lite: cannot parse line %r" % raw)
            key, val = _key(m.group(1)), (m.group(2) or "").strip()
            pos[0] += 1
            if val in ("|", ">", "|-", ">-", "|+", ">+"):
                out[key] = block_scalar(base, val.startswith(">"))
            elif val:
                out[key] = _scalar(val)
            else:
                nxt = peek()
                if nxt is not None and (indent(nxt) > base or (indent(nxt) == base and nxt.strip().startswith("- "))):
                    out[key] = parse(indent(nxt))
                else:
                    out[key] = ""

    return parse(0)
