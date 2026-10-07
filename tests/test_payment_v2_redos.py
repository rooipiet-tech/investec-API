"""Fix round (slice 1): every public parser stays linear on ~200KB hostile input.

Each case runs in a killed-on-timeout subprocess (tests/timing_helper.py), so a
regression fails in seconds instead of stalling the suite.
"""
from __future__ import annotations

import pytest

from tests.timing_helper import assert_fast

XFAIL_F6 = pytest.mark.xfail(reason="red: risk R3", strict=False)

N = 200_000
HOSTILE = [
    "'a' * 200000 + '!'",
    "'R1 ' * 70000",
    "'ref R1 ' * 30000",
    "'.invespend-notification.' * 10000",
    "'1 ' * 100000",
    "'(' * 100000",
    "'\"' * 100000",
    "'<' * 100000",
    "'a=b;' * 50000",
    "'dkim=pass ' * 20000",
    "'pay 12 ' * 30000",
    "'\\n' * 200000",
    "'x@' * 100000",
]


@XFAIL_F6
def test_loopguard_message_id_is_linear():
    assert_fast(
        "from invespend.payments.loopguard import is_own_notification as f\n"
        "data = '.invespend-notification.' * 10000",
        "f(data, '', '')",
    )


@XFAIL_F6
def test_loopguard_message_id_cut_to_rfc_line_limit():
    from invespend.payments import loopguard as lg

    assert lg.MAX_MSGID_CHARS == 998
    assert lg.is_own_notification("<x" + "a" * 1000 + ".invespend-notification.r@d>", "", "") is None


def test_loopguard_still_matches_real_make_msgid_output():
    from invespend.payments import loopguard as lg

    h = lg.loop_guard_headers("me@example.com", "abc123")
    assert lg.is_own_notification(h["Message-ID"], "", "") == "own_message_id"
    h = lg.loop_guard_headers("me@example.com", None)
    assert lg.is_own_notification(h["Message-ID"], "", "") == "own_message_id"


@pytest.mark.parametrize("data", [
    pytest.param(d, marks=XFAIL_F6) if "invespend" in d else d for d in HOSTILE
])
def test_loopguard_hostile(data):
    assert_fast(
        "from invespend.payments.loopguard import is_own_notification as f\n"
        f"data = {data}",
        "f(data, '', ''); f('', '', '', [data] * 3, data, {'precedence': data})",
    )


@pytest.mark.parametrize("data", HOSTILE)
def test_amounts_and_trigger_hostile(data):
    assert_fast(
        "from invespend.payments.amounts import marked_amount_candidates as a, labelled_payee_candidates as p\n"
        "from invespend.payments.trigger import parse_trigger as t, strip_trigger as s\n"
        f"data = {data}",
        "a(data); p(data); t(data); s(data)",
    )


@pytest.mark.parametrize("data", HOSTILE)
def test_refs_hostile(data):
    assert_fast(
        "from invespend.payments import refs\n"
        f"data = {data}",
        "refs.find_refs(data); refs.find_ref(data); refs.find_batch_refs(data); "
        "refs.find_batch_ref(data); refs.instruction_id_for(data, [data])",
    )


@pytest.mark.parametrize("data", HOSTILE)
def test_outcome_hostile(data):
    assert_fast(
        "from invespend.payments import outcome as o\n"
        f"data = {data}",
        "o.sanitize_provider_message(data); "
        "o.provider_message_from_body({'ErrorMessage': data}); "
        "o.parse_payment_response({'data': {'TransferResponses': [{'Status': data, 'ErrorMessage': data}]}})",
    )


@pytest.mark.parametrize("data", HOSTILE)
def test_sender_auth_hostile(data):
    assert_fast(
        "from invespend.payments import sender_auth as sa\n"
        f"data = {data}",
        "try:\n"
        "    sa.tokenize_auth_results('mx.example.com; ' + data)\n"
        "except sa.AuthParseError:\n"
        "    pass\n"
        "sa.authenticate_sender([data], ['mx.example.com; ' + data], frozenset({'a@example.com'}), frozenset({'mx.example.com'}))\n"
        "sa.authenticate_sender(['a@example.com'], ['mx.example.com; ' + data], frozenset({'a@example.com'}), frozenset({'mx.example.com'}))\n"
        "sa._has_group_syntax(data)",
    )


@pytest.mark.parametrize("data", HOSTILE)
def test_v2_inbox_parse_hostile(data):
    assert_fast(
        "from invespend.payments.v2_inbox import parse_v2_message as f\n"
        f"data = {data}",
        "f(('Message-ID: <' + data + '>\\r\\nFrom: ' + data + '\\r\\nSubject: ' + data + '\\r\\n"
        "Authentication-Results: ' + data + '\\r\\nReceived: ' + data + '\\r\\n\\r\\n' + data).encode('utf-8', 'replace'))",
    )
