"""S10 (R4-B3): the guarantee is the LOOP GUARD, not parse_trigger. Every builder, rendered
with worst-case third-party values, is still our own mail with its headers stripped."""
from __future__ import annotations

import importlib
import re


from tests.mail_helpers import reparse
from tests.notify_cases import body, render_all


WORST = dict(payee="Pay 123 Ltd", reference="pay 123 approve 1 cancel 2")
STRIP = ("Auto-Submitted", "X-Invespend-Notification", "Message-ID")


def mod(name):
    return importlib.import_module(f"invespend.payments.{name}")


def stripped(msg):
    for header in STRIP:
        del msg[header]
    return reparse(msg)


def test_every_builder_output_with_headers_stripped_is_still_own_body():
    content, lg = mod("content"), mod("loopguard")
    for name, msg in render_all(**WORST).items():
        subject = str(msg["Subject"])
        plain = stripped(msg)
        for header in STRIP:
            assert plain[header] is None
        view = content.text_view(plain)
        assert lg.is_own_notification("", "", "", view.raw_head_lines, subject) == "own_body", name


def test_every_builder_sets_loop_guard_headers():
    lg = mod("loopguard")
    for name, msg in render_all(**WORST).items():
        assert msg["Auto-Submitted"] == "auto-generated", name
        assert msg["X-Invespend-Notification"] == "1", name
        assert lg.OWN_MSGID_RE.search(msg["Message-ID"]), name
        assert lg.is_own_notification(msg["Message-ID"], "", "") == "own_message_id", name


def test_every_builder_body_starts_with_the_sentinel_within_first_three_non_empty_lines():
    lg = mod("loopguard")
    for name, msg in render_all(**WORST).items():
        non_empty = [ln.strip() for ln in body(msg).splitlines() if ln.strip()]
        assert non_empty[0] == lg.NOTICE_FIRST_LINE, name
        assert lg.NOTICE_FIRST_LINE in non_empty[:3]


def test_every_builder_subject_avoids_every_reply_forward_prefix():
    lg = mod("loopguard")
    for case in (dict(), WORST, dict(payee="Re: Fwd: x", reference="FW: y")):
        for name, msg in render_all(**case).items():
            subject = str(msg["Subject"]).strip().lower()
            for prefix in lg.SUBJECT_REPLY_PREFIXES:
                assert not subject.startswith(prefix), (name, prefix)


def test_parse_trigger_none_on_fixed_template_text_with_benign_values():
    trig = mod("trigger")
    for name, msg in render_all().items():
        assert trig.parse_trigger(body(msg)).status == "none", name
        assert trig.parse_trigger(str(msg["Subject"])).status == "none", name


def test_parse_command_is_none_on_every_rendered_template():
    commands, content = mod("commands"), mod("content")
    for name, msg in render_all(**WORST).items():
        view = content.text_view(stripped(msg))
        assert commands.parse_command(view) is None, name
        forced = content.TextView("", "", view.quoted_text, view.raw_head_lines, False, ("forced",))
        assert commands.parse_command(forced) is None, name
        assert forced.raw_head_lines[0] == mod("loopguard").NOTICE_FIRST_LINE


def test_reply_quoting_our_batch_email_still_needs_a_typed_command_on_the_first_line():
    from tests.mail_helpers import make_msg
    commands, content = mod("commands"), mod("content")
    quoted = "\n".join("> " + ln for ln in body(render_all()["batch"]).splitlines())
    reply = make_msg(plain="thanks\n\nOn Tue, Bot wrote:\n" + quoted, subject="Re: Payments awaiting approval [BATCH B-1007-ab12]")
    view = content.text_view(reply)
    assert view.typed_text == "thanks" and view.confident
    assert commands.parse_command(view) is None
    ok = make_msg(plain="approve 1 3\n\nOn Tue, Bot wrote:\n" + quoted, subject="Re: Payments awaiting approval [BATCH B-1007-ab12]")
    got = commands.parse_command(content.text_view(ok))
    assert (got.kind, got.numbers) == ("approve", (1, 3))


def test_reply_with_references_to_our_msgid_is_not_ignored_by_loop_guard():
    from tests.mail_helpers import make_msg
    lg, content = mod("loopguard"), mod("content")
    our = render_all()["batch"]
    reply = make_msg(plain="approve\n\n> quoted", subject="Re: Payments awaiting approval [BATCH B-1007-ab12]",
                     headers={"In-Reply-To": our["Message-ID"], "References": our["Message-ID"]})
    view = content.text_view(reply)
    got = lg.is_own_notification(str(reply.get("Message-ID") or ""), "", "", view.raw_head_lines, str(reply["Subject"]))
    assert got is None
    assert lg.is_own_notification(our["Message-ID"], "", "") == "own_message_id"


def test_inbound_with_auto_submitted_ignored_even_if_allowlisted_and_authenticated():
    from tests.mail_helpers import make_msg
    lg = mod("loopguard")
    auth = ("mx.google.com; dkim=pass header.i=@example.com; spf=pass smtp.mailfrom=piet@example.com; dmarc=pass header.from=example.com")
    msg = make_msg(plain="approve", headers={"Auto-Submitted": "auto-replied", "Authentication-Results": auth})
    assert lg.is_own_notification("", str(msg["Auto-Submitted"]), "") == "auto_submitted"


def test_no_builder_text_contains_a_full_second_copy_of_third_party_lines():
    for name, msg in render_all(**WORST).items():
        if name == "paste":
            continue
        assert not re.search(r"(?m)^(?:approve|cancel)\b", body(msg)), name
