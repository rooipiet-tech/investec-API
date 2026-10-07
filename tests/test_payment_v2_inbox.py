from __future__ import annotations

from datetime import datetime, timezone

from invespend.payments.v2_inbox import V2Message, parse_v2_message

RAW = (
    b"Authentication-Results: mx.google.com; dkim=pass header.i=@a.com; spf=pass smtp.mailfrom=a.com\r\n"
    b"Authentication-Results: evil.example; dkim=pass header.d=a.com\r\n"
    b"Received: from x by y; Tue, 06 Oct 2026 10:00:00 +0200\r\n"
    b"Received: from p by q; Tue, 06 Oct 2026 09:00:00 +0200\r\n"
    b"From: Piet <piet@a.com>\r\n"
    b"Message-ID: <abc@a.com>\r\n"
    b"Subject: pay 123\r\n"
    b"In-Reply-To: <irt@x>\r\n"
    b"References: <r1@x> <r2@x>\r\n"
    b"Auto-Submitted: auto-replied\r\n"
    b"X-Invespend-Notification: 1\r\n"
    b"Precedence: bulk\r\n"
    b"X-Autoreply: yes\r\n"
    b"X-Autorespond: yes\r\n"
    b"X-Auto-Response-Suppress: All\r\n"
    b"\r\nbody\r\n"
)


def test_headers_filled():
    m = parse_v2_message(RAW)
    assert m.message_id == "<abc@a.com>"
    assert m.subject == "pay 123"
    assert m.from_headers == ("Piet <piet@a.com>",)
    assert m.in_reply_to == "<irt@x>"
    assert m.references == "<r1@x> <r2@x>"
    assert m.auto_submitted == "auto-replied"
    assert m.x_invespend_notification == "1"
    assert m.precedence == "bulk"
    assert m.x_autoreply == "yes"
    assert m.x_autorespond == "yes"
    assert m.x_auto_response_suppress == "All"
    assert m.internaldate is None


def test_absent_headers_are_empty_strings_and_tuples():
    m = parse_v2_message(b"Subject: x\r\n\r\nhi")
    assert m.message_id == "" and m.from_headers == () and m.auth_results == ()
    assert m.precedence == m.x_autoreply == m.x_autorespond == m.x_auto_response_suppress == ""
    assert m.received_header_time is None


def test_multiple_authentication_results_order_preserved():
    m = parse_v2_message(RAW)
    assert len(m.auth_results) == 2
    assert m.auth_results[0].startswith("mx.google.com;")
    assert m.auth_results[1].startswith("evil.example;")


def test_two_from_headers_kept_separately():
    m = parse_v2_message(b"From: a@x.com\r\nFrom: b@x.com\r\n\r\n")
    assert m.from_headers == ("a@x.com", "b@x.com")


def test_internaldate_and_topmost_received_parsed():
    when = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
    m = parse_v2_message(RAW, when)
    assert m.internaldate == when
    assert m.received_header_time == datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)


def test_unparsable_received_gives_none():
    m = parse_v2_message(b"Received: from x; not a date\r\n\r\n")
    assert m.received_header_time is None


def test_raw_kept_but_excluded_from_repr():
    m = parse_v2_message(RAW)
    assert m.raw == RAW
    assert "raw=" not in repr(m)
    assert isinstance(m, V2Message)
