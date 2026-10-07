"""Slice 2: every new parser stays linear on ~200KB hostile input (killed-subprocess timing)."""
from __future__ import annotations

import pytest

from tests.timing_helper import assert_fast


TEXT = [
    "'a' * 200000", "'On ' * 70000", "'On x\\n' * 60000", "'From: ' * 40000", "'from: a\\n' * 25000",
    "'> ' * 100000", "'\\n' * 200000", "'-' * 200000", "'wrote:\\n' * 30000", "'approve ' * 25000",
    "'-----Original Message-----\\n' * 8000", "'   ' * 70000 + 'x'", "'\\t,' * 100000",
]
HTML = [
    "'<' * 100000", "'<a ' * 70000", "'<!--' * 50000", "'&' * 200000", "'<b>' * 60000", "'</b>' * 50000",
    "'<div class=\"' * 20000", "'<blockquote>' * 15000", "'</blockquote>' * 15000", "'<p>x' * 60000",
    "'<script>' * 20000", "'<div style=\"display:none\">' * 8000", "'&#' * 100000", "'<br>' * 50000",
]
PRELUDE = "import email\nfrom invespend.payments import content\n"


@pytest.mark.parametrize("data", TEXT)
def test_text_view_plain_is_linear(data):
    assert_fast(
        PRELUDE + f"data = {data}\nraw = 'Subject: Re: x\\nContent-Type: text/plain\\n\\n' + data\n"
        "msg = email.message_from_string(raw)",
        "content.text_view(msg)",
    )


@pytest.mark.parametrize("data", HTML)
def test_text_view_html_is_linear(data):
    assert_fast(
        PRELUDE + f"data = {data}\nraw = 'Subject: Re: x\\nContent-Type: text/html\\n\\n' + data\n"
        "msg = email.message_from_string(raw)",
        "content.text_view(msg)",
    )


@pytest.mark.parametrize("data", HTML)
def test_html_to_text_is_linear(data):
    assert_fast(PRELUDE + f"data = {data}", "content.html_to_text(data)")


@pytest.mark.parametrize("data", TEXT + HTML)
def test_split_typed_and_quoted_is_linear(data):
    assert_fast(PRELUDE + f"data = {data}", "content.split_typed_and_quoted(data, None, ('x',)); content.split_typed_and_quoted('', data, ('x',))")


@pytest.mark.parametrize("data", TEXT)
def test_parse_command_is_linear(data):
    assert_fast(
        "from invespend.payments import commands, content\n"
        f"data = {data}\n"
        "v = content.TextView(data, data, data, (data, data, data), True, ())\n"
        "u = content.TextView('', '', data, (data, data, data), False, ())",
        "commands.parse_command(v); commands.parse_command(u); commands.parse_command(content.TextView('approve\\n' + data, '', '', (), True, ()))",
    )


@pytest.mark.parametrize("data", TEXT)
def test_extract_bank_details_is_linear(data):
    assert_fast(
        "from invespend.payments import bankdetails\n" f"data = {data}",
        "bankdetails.extract_bank_details(data); bankdetails.extract_bank_details('Bank: ' + data[:19000])",
    )


@pytest.mark.parametrize("data", TEXT + HTML)
def test_image_validation_is_linear(data):
    assert_fast(
        "from invespend.payments import images\n" f"data = {data}",
        "images.validate_extraction({'payee_name': data, 'amount': data, 'account_number': data, 'currency': data, data: data}); "
        "images.sniff_mime(data.encode()); images.image_ref_from_bytes(data.encode())",
    )


@pytest.mark.parametrize("data", TEXT + HTML)
def test_notify_third_party_value_cleaning_is_linear(data):
    assert_fast(
        "from decimal import Decimal\nfrom invespend.payments import notify_v2 as n\n"
        f"data = {data}\n"
        "item = n.BatchItem(1, data, Decimal('1'), 'ZAR', data, data, 'typed')",
        "n.build_batch_approval_email('a@example.com', 'b@example.com', batch_ref='B-1007-ab12', items=[item], "
        "pending=[], expires_at=__import__('datetime').datetime(2026, 10, 8))",
    )
