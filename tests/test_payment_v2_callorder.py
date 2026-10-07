"""S7 (R4-B2/T16): attachment and image handling is unreachable for unauthenticated,
untriggered, stale, own or command mail. The harness composes the REAL pieces that exist
in the plan's step order (text_view, loop guard, sender auth, trigger, age gate, then
gather_payloads and the image extractor) around three spies; S11 repeats this against the
real per-message handler."""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.mail_helpers import make_msg


IMAGES = Path(__file__).parent / "fixtures" / "payments_v2" / "images"
ALLOW = frozenset({"piet@example.com"})
TRUSTED = frozenset({"mx.google.com"})
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
GOOD_AUTH = ("mx.google.com; dkim=pass header.i=@example.com; spf=pass smtp.mailfrom=piet@example.com; "
             "dmarc=pass header.from=example.com")
BAD_AUTH = "mx.google.com; dkim=fail header.i=@example.com; spf=pass smtp.mailfrom=piet@example.com"


def _mod(name):
    return importlib.import_module(f"invespend.payments.{name}")


def build(*, body="pay 123\nR100", auth=GOOD_AUTH, headers=None):
    png = (IMAGES / "tiny.png").read_bytes()
    h = {"Authentication-Results": auth}
    h.update(headers or {})
    return make_msg(
        plain=body, headers=h,
        attachments=(("a.csv", "text", "csv", b"Payee,Acme\nAmount,R100.00\n"),),
        inline=(("p.png", "image", "png", png),),
    )


def run(msg, spies, *, internaldate=NOW - timedelta(minutes=5), max_age_hours=24):
    """The plan's step order; every step appends to ``spies.order``."""
    content, loopguard, sender_auth = _mod("content"), _mod("loopguard"), _mod("sender_auth")
    trig, commands, images = _mod("trigger"), _mod("commands"), _mod("images")
    spies.order.append("text_view")
    view = content.text_view(msg)
    spies.order.append("loop_guard")
    if loopguard.is_own_notification(
        str(msg.get("Message-ID") or ""), str(msg.get("Auto-Submitted") or ""),
        str(msg.get("X-Invespend-Notification") or ""), view.raw_head_lines, str(msg.get("Subject") or ""),
    ):
        return "own_notification"
    spies.order.append("auth")
    verdict = sender_auth.authenticate_sender(
        [str(v) for v in msg.get_all("From") or ()], [str(v) for v in msg.get_all("Authentication-Results") or ()],
        ALLOW, TRUSTED,
    )
    if not verdict.ok:
        return "unauthenticated"
    spies.order.append("command")
    if commands.parse_command(view) is not None:
        return "command"
    spies.order.append("trigger")
    if trig.parse_trigger(view.typed_text).status != "ok":
        return "untriggered"
    spies.order.append("age")
    if NOW - internaldate > timedelta(hours=max_age_hours):
        return "expired_age"
    payloads = content.gather_payloads(msg, view)
    for ref in payloads.images:
        spies.order.append("image_extractor")
        images.run_extractor(spies.fake, ref)
    return "processed"


class Spies:
    def __init__(self, monkeypatch):
        content, extract, images = _mod("content"), _mod("extract"), _mod("images")
        self.order, self.extract_calls, self.gather_calls = [], [], []
        self.fake = images.FakeImageExtractor({})
        real_extract, real_gather = extract.extract_attachment, content.gather_payloads

        def spy_extract(*a, **k):
            self.order.append("extract_attachment")
            self.extract_calls.append(a)
            return real_extract(*a, **k)

        def spy_gather(*a, **k):
            self.order.append("gather_payloads")
            self.gather_calls.append(a)
            return real_gather(*a, **k)

        monkeypatch.setattr(extract, "extract_attachment", spy_extract)
        monkeypatch.setattr(content, "gather_payloads", spy_gather)

    def untouched(self):
        return self.extract_calls == [] and self.gather_calls == [] and self.fake.calls == []


@pytest.fixture
def spies(monkeypatch):
    return Spies(monkeypatch)


def test_no_image_extractor_call_for_unauthenticated_or_untriggered(spies):
    assert run(build(auth=BAD_AUTH), spies) == "unauthenticated"
    assert run(build(body="hello, no trigger here"), spies) == "untriggered"
    assert spies.untouched()


def test_over_age_message_never_reaches_attachments_or_images(spies):
    assert run(build(), spies, internaldate=NOW - timedelta(hours=25)) == "expired_age"
    assert spies.untouched()


def test_own_notification_and_command_never_reach_attachments_or_images(spies):
    assert run(build(headers={"Auto-Submitted": "auto-generated"}), spies) == "own_notification"
    assert run(build(body="approve 1"), spies) == "command"
    assert spies.untouched()


def test_text_view_is_called_before_auth_and_gather_payloads_only_after_trigger_and_age(spies):
    assert run(build(), spies) == "processed"
    assert spies.order[:6] == ["text_view", "loop_guard", "auth", "command", "trigger", "age"]
    assert spies.order[6] == "gather_payloads"
    assert "extract_attachment" in spies.order[7:] and "image_extractor" in spies.order[7:]
    assert spies.order.index("gather_payloads") > spies.order.index("age")
    assert len(spies.fake.calls) == 1 and len(spies.extract_calls) == 1


def test_unauthenticated_order_stops_at_auth(spies):
    run(build(auth=BAD_AUTH), spies)
    assert spies.order == ["text_view", "loop_guard", "auth"]
