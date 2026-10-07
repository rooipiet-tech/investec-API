"""S5: sender authentication (pure function of headers; real .eml fixtures, no network)."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from invespend.payments import sender_auth as sa
from invespend.payments.sender_auth import (
    AuthParseError,
    authenticate_sender,
    tokenize_auth_results,
    trusted_auth_results,
)
from invespend.payments.v2_inbox import parse_v2_message

FIX = Path(__file__).parent / "fixtures" / "payments_v2" / "auth"
ALLOW = frozenset({"piet@example.com"})
TRUSTED = frozenset({"mx.google.com"})

needs_strict = pytest.mark.skipif(
    not sa.strict_address_parser_available(),
    reason="interpreter lacks the strict address parser (the module fails closed there)",
)


def verdict(name, allow=ALLOW, trusted=TRUSTED):
    msg = parse_v2_message((FIX / f"{name}.eml").read_bytes())
    return authenticate_sender(msg.from_headers, msg.auth_results, allow, trusted)


def assert_rejected(name, reason):
    v = verdict(name)
    assert (v.ok, v.reason) == (False, reason), name


@needs_strict
@pytest.mark.parametrize("name", ["ok_gmail_header_i", "ok_header_d", "ok_dmarc_absent",
                                  "ok_dmarc_none", "upper_case_from"])
def test_allowlisted_and_aligned_pass_is_ok(name):
    v = verdict(name)
    assert (v.ok, v.reason, v.from_addr) == (True, "ok", "piet@example.com")


@needs_strict
def test_real_gmail_header_with_header_i_accepted():
    msg = parse_v2_message((FIX / "ok_gmail_header_i.eml").read_bytes())
    assert "header.i=@example.com" in msg.auth_results[0]
    assert authenticate_sender(msg.from_headers, msg.auth_results, ALLOW, TRUSTED).ok


@needs_strict
def test_dkim_fail():
    assert_rejected("dkim_fail", "dkim_not_pass")


@needs_strict
def test_spf_fail_and_none():
    assert_rejected("spf_fail", "spf_not_pass")
    assert_rejected("spf_none", "spf_not_pass")


@needs_strict
def test_both_headers_missing():
    assert_rejected("no_auth_headers", "no_trusted_auth_results")


@needs_strict
def test_display_name_spoof_rejected():
    # The allowlisted address only appears as a display name; the real address is foreign.
    assert_rejected("display_name_spoof", "not_allowlisted")


@needs_strict
def test_reply_to_never_authenticates():
    assert_rejected("replyto_allowlisted_from_foreign", "not_allowlisted")


@needs_strict
def test_untrusted_authserv_id_with_pass_rejected():
    assert_rejected("untrusted_authserv", "no_trusted_auth_results")


@needs_strict
def test_authserv_id_mismatch_rejected():
    msg = parse_v2_message((FIX / "ok_gmail_header_i.eml").read_bytes())
    v = authenticate_sender(msg.from_headers, msg.auth_results, ALLOW, frozenset({"other.example"}))
    assert (v.ok, v.reason) == (False, "no_trusted_auth_results")
    ok = authenticate_sender(msg.from_headers, msg.auth_results, ALLOW, frozenset({"MX.Google.COM"}))
    assert ok.ok  # authserv-id comparison is case-insensitive


@needs_strict
def test_arc_pass_with_dkim_fail_rejected():
    assert_rejected("arc_pass_dkim_fail", "dkim_not_pass")


@needs_strict
def test_injected_trusted_id_header_below_real_one_ignored():
    assert_rejected("injected_trusted_id_below", "dkim_not_pass")
    msg = parse_v2_message((FIX / "injected_trusted_id_below.eml").read_bytes())
    assert len(msg.auth_results) == 2
    assert trusted_auth_results(msg.auth_results[1:], TRUSTED)  # the lower one WOULD pass alone


@needs_strict
def test_injected_untrusted_header_above_real_one_rejects():
    # Topmost is not ours: nothing below it is consulted.
    assert_rejected("injected_untrusted_above", "no_trusted_auth_results")


@needs_strict
@pytest.mark.parametrize("name", ["comment_injection", "comment_injection_semicolon", "quoted_injection"])
def test_comment_injection_fixture_rejected(name):
    assert_rejected(name, "dkim_not_pass")


@needs_strict
def test_two_from_headers_rejected():
    assert_rejected("two_from_headers", "multiple_from")


@needs_strict
def test_group_or_two_addresses_in_from_rejected():
    assert_rejected("group_two_addresses", "multiple_from")
    assert_rejected("group_syntax", "multiple_from")


@needs_strict
def test_no_from_rejected():
    assert_rejected("no_from", "no_from")


@needs_strict
@pytest.mark.parametrize("name", ["non_ascii_from", "non_ascii_display_name"])
def test_non_ascii_from_rejected(name):
    assert_rejected(name, "non_ascii_from")


@needs_strict
def test_subdomain_dkim_not_aligned_rejected():
    assert_rejected("subdomain_dkim", "misaligned")


@needs_strict
def test_subdomain_spf_not_aligned_rejected():
    assert_rejected("subdomain_spf", "misaligned")


@needs_strict
def test_case_insensitive_address_and_plus_address_not_equal():
    assert verdict("upper_case_from").ok
    assert_rejected("plus_address", "not_allowlisted")
    assert verdict("ok_gmail_header_i", allow=frozenset({"PIET@Example.COM"})).ok


@needs_strict
def test_not_allowlisted_when_allowlist_empty():
    assert_rejected_with = verdict("ok_gmail_header_i", allow=frozenset())
    assert (assert_rejected_with.ok, assert_rejected_with.reason) == (False, "not_allowlisted")


# ---- TD / F47 -----------------------------------------------------------------------
@needs_strict
def test_dmarc_fail_rejected():
    assert_rejected("dmarc_fail", "dmarc_fail")


@needs_strict
def test_dmarc_pass_header_from_mismatch_rejected():
    assert_rejected("dmarc_pass_header_from_mismatch", "dmarc_header_from_mismatch")


@needs_strict
def test_dmarc_absent_with_aligned_dkim_spf_accepted():
    assert verdict("ok_dmarc_absent").ok


@needs_strict
def test_dmarc_none_with_aligned_dkim_spf_accepted():
    assert verdict("ok_dmarc_none").ok


@needs_strict
def test_misaligned_dkim_spf_with_dmarc_pass_rejected():
    assert_rejected("misaligned_with_dmarc_pass", "misaligned")


@needs_strict
def test_unknown_dmarc_result_fails_closed():
    h = ("mx.google.com; dkim=pass header.i=@example.com; spf=pass smtp.mailfrom=piet@example.com; "
         "dmarc=weird header.from=example.com")
    assert authenticate_sender(["piet@example.com"], [h], ALLOW, TRUSTED).reason == "dmarc_fail"


# ---- F48 -------------------------------------------------------------------------------
def test_strict_parser_unavailable_fails_closed_with_code(monkeypatch):
    monkeypatch.setattr(sa, "strict_address_parser_available", lambda: False)
    msg = parse_v2_message((FIX / "ok_gmail_header_i.eml").read_bytes())
    v = authenticate_sender(msg.from_headers, msg.auth_results, ALLOW, TRUSTED)
    assert (v.ok, v.reason, v.from_addr) == (False, "strict_parser_unavailable", "")


@pytest.mark.skipif(os.environ.get("CI") != "true",
                    reason="TR3-8: only enforced in CI; the v2 preflight fails closed anyway")
def test_strict_parser_available_on_this_interpreter():
    assert sa.strict_address_parser_available() is True


def test_strict_parser_detection_reads_the_getaddresses_signature(monkeypatch):
    import email.utils as eu
    assert sa.strict_address_parser_available() == ("strict" in __import__("inspect").signature(eu.getaddresses).parameters)

    def old_getaddresses(fieldvalues):
        return []
    monkeypatch.setattr(sa, "getaddresses", old_getaddresses)
    assert sa.strict_address_parser_available() is False


# ---- tokenizer ---------------------------------------------------------------------------
def test_tokenizer_basic_gmail_shape():
    value = (FIX / "ok_gmail_header_i.eml").read_text().split("Authentication-Results: ")[1].split("From:")[0]
    value = re.sub(r"\r\n\s+", " ", value).strip()
    ident, results = tokenize_auth_results(value)
    assert ident == "mx.google.com"
    methods = [m for m, _ in results]
    assert methods == ["dkim", "spf", "dmarc"]
    assert results[0][1]["result"] == "pass" and results[0][1]["header.i"] == "@example.com"
    assert results[1][1]["smtp.mailfrom"] == "piet@example.com"
    assert results[2][1]["header.from"] == "example.com"


def test_tokenizer_strips_nested_comments_with_escapes():
    ident, res = tokenize_auth_results(r"mx.example 1; dkim=fail (a (nested; dkim=pass) \) still; comment) header.d=a.com")
    assert ident == "mx.example"
    assert res == [("dkim", {"result": "fail", "header.d": "a.com"})]


def test_tokenizer_quoted_strings_hide_semicolons():
    _, res = tokenize_auth_results('mx; dkim=fail header.d="a.com; dkim=pass"')
    assert res == [("dkim", {"result": "fail", "header.d": "a.com; dkim=pass"})]


def test_tokenizer_none_and_empty_segments():
    assert tokenize_auth_results("mx.example; none") == ("mx.example", [])
    assert tokenize_auth_results("mx.example;") == ("mx.example", [])


def test_tokenizer_method_version_and_case():
    _, res = tokenize_auth_results("MX.Example; DKIM/1=PASS Header.D=A.com")
    assert res == [("dkim", {"result": "pass", "header.d": "A.com"})]


@pytest.mark.parametrize("bad", ["", "   ", "mx; dkim=pass (unclosed", 'mx; dkim=pass header.d="open',
                                 "mx; garbage", "mx; =pass", "mx; dkim=", "dkim=pass; spf=pass"])
def test_tokenizer_malformed_raises(bad):
    with pytest.raises(AuthParseError):
        tokenize_auth_results(bad)


def test_malformed_topmost_header_is_no_trusted_results():
    assert trusted_auth_results(["mx.google.com; dkim=pass (unclosed"], TRUSTED) is None
    assert trusted_auth_results([], TRUSTED) is None


def test_trusted_results_reads_only_the_topmost_header():
    good = "mx.google.com; dkim=pass header.d=a.com"
    assert trusted_auth_results(["evil.example; dkim=pass header.d=a.com", good], TRUSTED) is None
    assert trusted_auth_results([good, "evil.example; x=y"], TRUSTED) == [("dkim", {"result": "pass", "header.d": "a.com"})]


# ---- structure -----------------------------------------------------------------------------
def test_module_never_reads_reply_to_or_arc():
    src = Path(sa.__file__).read_text().lower()
    assert "reply" not in src
    assert not re.search(r"\barc\b", src)
    assert "arc-" not in src


def test_authenticate_sender_is_a_pure_function_of_headers():
    import inspect
    params = list(inspect.signature(authenticate_sender).parameters)
    assert params == ["from_headers", "auth_results_headers", "allowlist", "trusted_authserv_ids"]
    src = Path(sa.__file__).read_text()
    for forbidden in ("attachment", "get_payload", "image", "import requests", "socket", "open("):
        assert forbidden not in src, forbidden
