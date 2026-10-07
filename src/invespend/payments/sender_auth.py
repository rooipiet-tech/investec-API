"""Sender authentication for v2 (S5). Pure, stdlib only, no wiring.

Primary control for inbound payment mail. The inputs are the raw header values
of ONE message (``V2Message.from_headers`` / ``V2Message.auth_results``); no
message content is ever an input. Any failure -> ``ok=False`` with a
reason CODE only (never header text).

Rules (all must hold):
  1. exactly one From header holding exactly one plain ASCII address (no group
     syntax), parsed with the STRICT address parser; if this interpreter lacks
     it the function FAILS CLOSED (``strict_parser_unavailable``); the address
     must be in the allowlist exactly (case-insensitive, no plus/dot equivalence);
  2. only the TOPMOST Authentication-Results header is read (the one our
     receiving MTA added last) and its authserv-id must equal a configured id;
  3. dkim=pass (domain from header.d, else header.i) and spf=pass (smtp.mailfrom);
  4. STRICT alignment: dkim domain and smtp.mailfrom domain equal the From domain
     exactly (no parent-organisation relaxation); every other dkim/spf result
     (fail, softfail, none, temperror, permerror) rejects;
  5. DMARC: a result other than pass/none rejects; a dmarc=pass must carry
     header.from equal to the From domain; an absent or dmarc=none result is
     acceptable because dkim AND spf already passed strictly aligned (F47).
Only the authentication results listed above are consulted; results for any
other method are ignored, and no other header is read.
"""
from __future__ import annotations

import inspect
from collections.abc import Sequence
from dataclasses import dataclass
from email.utils import getaddresses


@dataclass(frozen=True)
class AuthVerdict:
    ok: bool
    reason: str      # "ok" | "no_from" | "multiple_from" | "non_ascii_from" | "not_allowlisted"
                     # | "no_trusted_auth_results" | "dkim_not_pass" | "spf_not_pass" | "misaligned"
                     # | "dmarc_fail" | "dmarc_header_from_mismatch" | "strict_parser_unavailable"
    from_addr: str   # normalised outer address ("" if unparsable)


class AuthParseError(ValueError):
    pass


# ---- RFC 8601 tokenizer ------------------------------------------------------
def _segments(value: str) -> list[list[str]]:
    """Split on ';' outside comments and quoted strings; each segment is a list of
    whitespace-separated tokens (quoted strings keep their quotes, comments are
    removed). Raises AuthParseError on unbalanced comments/quotes."""
    segments: list[list[str]] = [[]]
    token: list[str] = []
    depth = 0           # comment nesting
    in_quote = False
    i, n = 0, len(value)

    def flush() -> None:
        if token:
            segments[-1].append("".join(token))
            token.clear()

    while i < n:
        ch = value[i]
        if depth:
            if ch == "\\":
                i += 2
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
            if depth == 0:
                flush()   # a comment separates tokens
            continue
        if in_quote:
            if ch == "\\" and i + 1 < n:
                token.append(value[i + 1])
                i += 2
                continue
            if ch == '"':
                in_quote = False
                token.append('"')
            else:
                token.append(ch)
            i += 1
            continue
        if ch == "(":
            depth = 1
            flush()
        elif ch == '"':
            in_quote = True
            token.append('"')
        elif ch == ";":
            flush()
            segments.append([])
        elif ch in " \t\r\n":
            flush()
        else:
            token.append(ch)
        i += 1
    if depth or in_quote:
        raise AuthParseError("unbalanced comment or quoted string")
    flush()
    return segments


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def tokenize_auth_results(header_value: str) -> tuple[str, list[tuple[str, dict[str, str]]]]:
    """Returns (authserv_id, [(method, {"result": ..., "header.d": ..., ...}), ...]).

    Methods and property names are lower-cased; ``result`` is lower-cased; values
    keep their case (unquoted). Malformed input raises AuthParseError.
    """
    segments = _segments(header_value or "")
    head = segments[0]
    if not head:
        raise AuthParseError("missing authserv-id")
    authserv_id = _unquote(head[0]).strip().lower()
    if not authserv_id or "=" in authserv_id:
        raise AuthParseError("bad authserv-id")
    results: list[tuple[str, dict[str, str]]] = []
    for seg in segments[1:]:
        if not seg:
            continue                      # empty segment (trailing ';')
        if seg == ["none"]:
            continue                      # "no authentication performed"
        first = seg[0]
        if "=" not in first:
            raise AuthParseError("malformed resinfo")
        method, result = first.split("=", 1)
        method = method.split("/", 1)[0].strip().lower()
        result = _unquote(result).strip().lower()
        if not method or not result:
            raise AuthParseError("malformed resinfo")
        props: dict[str, str] = {"result": result}
        for tok in seg[1:]:
            if "=" not in tok:
                continue                  # stray word (e.g. part of a reason); never a property
            key, val = tok.split("=", 1)
            key = key.strip().lower()
            if "." in key and key not in props:
                props[key] = _unquote(val)
        results.append((method, props))
    return authserv_id, results


def trusted_auth_results(
    headers: Sequence[str], trusted_authserv_ids: frozenset[str]
) -> list[tuple[str, dict[str, str]]] | None:
    """ONLY ``headers[0]`` (the topmost header, added last by our receiving MTA).
    None unless its authserv-id equals a configured id exactly (case-insensitive);
    lower headers are never read."""
    if not headers:
        return None
    try:
        authserv_id, results = tokenize_auth_results(headers[0])
    except AuthParseError:
        return None
    trusted = {t.strip().lower() for t in trusted_authserv_ids if t and t.strip()}
    if authserv_id not in trusted:
        return None
    return results


def strict_address_parser_available() -> bool:
    """Feature-detects the ``strict`` keyword of ``email.utils.getaddresses``
    (added by a CPython security patch release; absent on early 3.12 patch
    releases). The v2 cycle preflight fails closed when this is False."""
    try:
        return "strict" in inspect.signature(getaddresses).parameters
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return False


# ---- From parsing ---------------------------------------------------------------
def _has_group_syntax(header: str) -> bool:
    """True when ':' or ';' occurs outside quoted strings, comments and angle
    brackets (RFC 5322 group syntax: ``name: a@x, b@y;``)."""
    in_quote = False
    depth = 0
    in_angle = False
    i = 0
    while i < len(header):
        ch = header[i]
        if in_quote:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_quote = False
        elif depth:
            if ch == "\\":
                i += 1
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
        elif ch == '"':
            in_quote = True
        elif ch == "(":
            depth = 1
        elif ch == "<":
            in_angle = True
        elif ch == ">":
            in_angle = False
        elif ch in ":;" and not in_angle:
            return True
        i += 1
    return False


def _parse_single_from(from_headers: Sequence[str]) -> tuple[str, str]:
    """(reason, address). reason "ok" with the lower-cased address, else a code."""
    if len(from_headers) == 0:
        return "no_from", ""
    if len(from_headers) > 1:
        return "multiple_from", ""
    header = from_headers[0]
    if not header.isascii():
        return "non_ascii_from", ""
    header = header.replace("\r", " ").replace("\n", " ")
    if _has_group_syntax(header):
        return "multiple_from", ""
    parsed = getaddresses([header], strict=True)
    if len(parsed) == 0:
        return "no_from", ""
    if len(parsed) > 1:
        return "multiple_from", ""
    addr = parsed[0][1].strip()
    if not addr or addr.count("@") != 1:
        return "no_from", ""
    local, domain = addr.split("@")
    if not local or not domain or any(c in addr for c in " \t<>(),;:\"\\"):
        return "no_from", ""
    if not addr.isascii():
        return "non_ascii_from", ""
    return "ok", addr.lower()


def _domain(value: str) -> str:
    """Domain of ``user@domain``, ``@domain`` or a bare domain, lower-cased."""
    value = (value or "").strip()
    return (value.rsplit("@", 1)[1] if "@" in value else value).strip().lower()


def authenticate_sender(
    from_headers: Sequence[str],
    auth_results_headers: Sequence[str],
    allowlist: frozenset[str],
    trusted_authserv_ids: frozenset[str],
) -> AuthVerdict:
    if not strict_address_parser_available():
        return AuthVerdict(False, "strict_parser_unavailable", "")
    reason, addr = _parse_single_from(from_headers)
    if reason != "ok":
        return AuthVerdict(False, reason, "")
    allowed = {a.strip().lower() for a in allowlist if a and a.strip()}
    if addr not in allowed:
        return AuthVerdict(False, "not_allowlisted", addr)

    results = trusted_auth_results(auth_results_headers, trusted_authserv_ids)
    if results is None:
        return AuthVerdict(False, "no_trusted_auth_results", addr)

    from_domain = addr.rsplit("@", 1)[1]

    dkim = [p for m, p in results if m == "dkim"]
    if not dkim or any(p["result"] != "pass" for p in dkim):
        return AuthVerdict(False, "dkim_not_pass", addr)
    spf = [p for m, p in results if m == "spf"]
    if not spf or any(p["result"] != "pass" for p in spf):
        return AuthVerdict(False, "spf_not_pass", addr)

    dkim_domains = [_domain(p.get("header.d") or p.get("header.i") or "") for p in dkim]
    if from_domain not in dkim_domains:
        return AuthVerdict(False, "misaligned", addr)
    if any(_domain(p.get("smtp.mailfrom", "")) != from_domain for p in spf):
        return AuthVerdict(False, "misaligned", addr)

    for method, props in results:
        if method != "dmarc":
            continue
        if props["result"] == "pass":
            if _domain(props.get("header.from", "")) != from_domain:
                return AuthVerdict(False, "dmarc_header_from_mismatch", addr)
        elif props["result"] != "none":
            return AuthVerdict(False, "dmarc_fail", addr)

    return AuthVerdict(True, "ok", addr)
