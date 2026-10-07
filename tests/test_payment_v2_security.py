"""S11: sender authentication at cycle level, own-notification guard, call order (three spies), images, F6/F7/F47."""
from __future__ import annotations

import email
import email.policy
import hashlib
from datetime import timedelta
from pathlib import Path

import pytest

from tests.notify_cases import render_all
from tests.v2_harness import BAD_AUTH, GOOD_AUTH, OWNER, Env, body_for, make_mail, mod, offered


IMAGES = Path(__file__).parent / "fixtures" / "payments_v2" / "images"
PNG = (IMAGES / "tiny.png").read_bytes()
PNG_SHA = hashlib.sha256(PNG).hexdigest()
CSV = ("a.csv", "text", "csv", b"Payee,Acme Trading\nAmount,R100.00\n")


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def outcome_of(env, msg):
    iid = mod("refs").instruction_id_for(msg.message_id, msg.from_headers)
    return env.store._messages[iid]["outcome"]


# ------------------------------------------------------------- F6 / F7 / F47
def test_allowlisted_and_pass_is_processed(env):
    env.instruct()
    env.cycle()
    assert len(env.rows()) == 1


@pytest.mark.parametrize("kwargs", [
    {"frm": "Piet <attacker@evil.example>"},
    {"frm": "\"piet@example.com\" <attacker@evil.example>"},
    {"auth": None},
    {"auth": BAD_AUTH},
    {"frm": "Piet <piet@example.com>, Evil <e@evil.example>"},
    {"headers": {"Reply-To": OWNER}, "frm": "Piet <attacker@evil.example>"},
])
def test_spoof_display_name_reply_to_missing_header_make_no_state(env, kwargs):
    env.instruct(**kwargs)
    env.cycle()
    assert env.rows() == [] and env.smtp.sent == [] and env.audit_entries("auth_failed")


def test_injected_auth_header_does_not_authenticate_end_to_end(env):
    env.instruct(auth=BAD_AUTH, headers={"Authentication-Results": GOOD_AUTH})      # topmost is the failing one
    env.cycle()
    assert env.rows() == []
    env.instruct(auth="evil.example; dkim=pass header.i=@example.com; spf=pass smtp.mailfrom=piet@example.com")
    env.cycle()
    assert env.rows() == []


def test_forward_by_authenticated_owner_with_stranger_inner_from_is_processed(env):
    body = ("pay 123\nPayee: Acme Trading\nAmount: R100.00\n\n---------- Forwarded message ---------\n"
            "From: Stranger <stranger@evil.example>\nSubject: invoice\n\nplease pay\n")
    env.instruct(body)
    env.cycle()
    assert len(env.rows()) == 1 and env.row()["notify_to"] == OWNER


def test_forward_by_stranger_with_inner_owner_from_is_rejected(env):
    body = ("pay 123\nPayee: Acme Trading\nAmount: R100.00\n\n---------- Forwarded message ---------\n"
            "From: Piet <piet@example.com>\n\npay 123\n")
    env.instruct(body, frm="Stranger <stranger@evil.example>", auth=GOOD_AUTH.replace("piet@", "stranger@"))
    env.cycle()
    assert env.rows() == []


def test_third_party_pay_123_in_an_unrecognised_forward_is_ignored_and_the_extractor_not_called(env):
    body = "Hi\n\nBegin doorgestuurde boodskap:\nVan: X <x@evil.example>\n\npay 123\nPayee: Acme Trading\nAmount: R100\n"
    env.instruct(body, subject="Fwd: invoice", inline=(("p.png", "image", "png", PNG),))
    env.cycle()
    assert env.rows() == [] and env.extractor.calls == [] and env.audit_entries("typed_text_unsplittable")


@pytest.mark.parametrize("auth,accepted", [
    ("mx.google.com; dkim=pass header.i=@example.com; spf=pass smtp.mailfrom=piet@example.com", True),            # dmarc absent
    ("mx.google.com; dkim=pass header.i=@example.com; spf=pass smtp.mailfrom=piet@example.com; dmarc=none", True),
    ("mx.google.com; dkim=pass header.i=@example.com; spf=pass smtp.mailfrom=piet@example.com; dmarc=fail header.from=example.com", False),
    ("mx.google.com; dkim=pass header.i=@example.com; spf=pass smtp.mailfrom=piet@example.com; dmarc=pass header.from=other.example", False),
    ("mx.google.com; dkim=pass header.i=@other.example; spf=pass smtp.mailfrom=piet@example.com", False),
])
def test_dmarc_variants_at_cycle_level_incl_approval_replies(env, auth, accepted):
    env.instruct(auth=auth)
    env.cycle()
    assert bool(env.rows()) is accepted
    if accepted:
        env.advance(15)
        env.reply("cancel", auth=auth)
        env.cycle()
        assert env.row()["status"] == "cancelled"


def test_unparsable_or_double_from_gets_deterministic_instruction_id_and_fails_auth(env):
    msg = env.instruct(frm="piet@example.com, other@example.com")
    again = make_mail("x", frm="piet@example.com, other@example.com", message_id=msg.message_id)
    a = mod("refs").instruction_id_for(msg.message_id, msg.from_headers)
    assert a == mod("refs").instruction_id_for(again.message_id, again.from_headers) and a
    env.cycle()
    assert env.rows() == [] and env.audit_entries("auth_failed")


# ------------------------------------------------------------ own notification
@pytest.mark.parametrize("headers", [
    {"Auto-Submitted": "auto-generated"}, {"Auto-Submitted": "auto-replied"}, {"X-Invespend-Notification": "1"},
    {"Precedence": "bulk"}, {"Precedence": "junk"}, {"Precedence": "list"}, {"Precedence": "auto_reply"},
    {"X-Autoreply": "yes"}, {"X-Autorespond": "yes"}, {"X-Auto-Response-Suppress": "All"},
])
def test_inbound_automatic_mail_is_ignored_even_if_allowlisted_and_authenticated(env, headers):
    msg = env.instruct(headers=headers)
    env.cycle()
    assert env.rows() == [] and env.smtp.sent == [] and outcome_of(env, msg) == "own_notification"
    assert env.audit_entries("own_notification")


def test_inbound_with_own_message_id_is_ignored(env):
    msg = env.instruct(message_id="<1.2.3.invespend-notification.B-1007-aaaa@example.com>")
    env.cycle()
    assert env.rows() == [] and outcome_of(env, msg) == "own_notification"


def test_own_notification_with_all_headers_stripped_ignored_end_to_end(env):
    from invespend.payments.loopguard import NOTICE_FIRST_LINE
    msg = env.instruct(NOTICE_FIRST_LINE + "\n\npay 123\nPayee: Acme Trading\nAmount: R100\napprove\n")
    env.cycle()
    assert env.rows() == [] and outcome_of(env, msg) == "own_notification"


def test_every_rendered_email_with_headers_stripped_is_own_notification_end_to_end(env, monkeypatch):
    spies = {"gather": 0, "extract": 0}
    monkeypatch.setattr(mod("content"), "gather_payloads", lambda *a, **k: spies.__setitem__("gather", spies["gather"] + 1))
    monkeypatch.setattr(mod("extract"), "extract_attachment", lambda *a, **k: spies.__setitem__("extract", spies["extract"] + 1))
    rendered = render_all(payee="Pay 123 Ltd", reference="pay 123 approve 1 cancel 2")
    assert len(rendered) >= 12
    msgs = []
    for name, built in rendered.items():
        copy = email.message_from_bytes(built.as_bytes(), policy=email.policy.compat32)
        for header in ("Auto-Submitted", "X-Invespend-Notification", "Message-ID"):
            del copy[header]
        del copy["From"]
        copy["From"] = f"Piet <{OWNER}>"
        copy["Message-ID"] = f"<relayed.{name}@mail.example.com>"
        copy["Authentication-Results"] = GOOD_AUTH
        msgs.append(mod("v2_inbox").parse_v2_message(copy.as_bytes(), env.now - timedelta(minutes=1)))
    env.inbox.queue(*msgs)
    env.cycle()
    assert env.rows() == [] and env.smtp.sent == [] and spies == {"gather": 0, "extract": 0}
    assert {outcome_of(env, m) for m in msgs} == {"own_notification"}


# ----------------------------------------------------------------- three spies
class Spies:
    def __init__(self, monkeypatch):
        self.order, self.extractor_calls = [], []
        content, extract_mod, auth, trig, cmds, guard = (mod(n) for n in ("content", "extract", "sender_auth", "trigger", "commands", "loopguard"))
        for module, name in ((content, "text_view"), (guard, "is_own_notification"), (auth, "authenticate_sender"),
                             (cmds, "parse_command"), (trig, "parse_trigger"), (content, "gather_payloads"),
                             (extract_mod, "extract_attachment"), (mod("images"), "run_extractor")):
            real = getattr(module, name)
            monkeypatch.setattr(module, name, self._wrap(name, real))

    def _wrap(self, name, real):
        def spy(*a, **k):
            self.order.append(name)
            return real(*a, **k)
        return spy

    def untouched(self):
        return not {"gather_payloads", "extract_attachment", "run_extractor"} & set(self.order)


@pytest.fixture
def spies(monkeypatch):
    return Spies(monkeypatch)


def with_attachments(env, body=None, **kw):
    return env.instruct(body or body_for(), attachments=(CSV,), inline=(("p.png", "image", "png", PNG),), **kw)


def test_no_image_extractor_call_for_unauthenticated_or_untriggered(env, spies):
    with_attachments(env, auth=BAD_AUTH)
    with_attachments(env, body="hello, nothing to do")
    with_attachments(env, headers={"Auto-Submitted": "auto-generated"})
    with_attachments(env, internaldate=env.now - timedelta(hours=48))
    env.cycle()
    assert spies.untouched() and env.extractor.calls == []


def test_command_replies_with_attachments_never_reach_gather_payloads(env, spies):
    offered(env, "101.00")
    spies.order.clear()
    env.advance(15)
    env.reply("approve", attachments=(CSV,), inline=(("p.png", "image", "png", PNG),))
    env.reply("cancel 9", attachments=(CSV,))
    env.cycle()
    assert spies.untouched() and env.extractor.calls == []


def test_text_view_is_called_before_auth_and_gather_payloads_only_after_trigger_and_age(env, spies):
    with_attachments(env)
    env.cycle()
    order = spies.order
    assert order[:5] == ["text_view", "is_own_notification", "authenticate_sender", "parse_command", "parse_trigger"]
    assert order.index("gather_payloads") > order.index("parse_trigger")
    assert "extract_attachment" in order[order.index("gather_payloads"):] and "run_extractor" in order[order.index("gather_payloads"):]


def test_unauthenticated_order_stops_at_auth(env, spies):
    with_attachments(env, auth=BAD_AUTH)
    env.cycle()
    assert spies.order == ["text_view", "is_own_notification", "authenticate_sender"]


def test_stale_triggered_mail_stops_after_trigger(env, spies):
    with_attachments(env, internaldate=env.now - timedelta(hours=48))
    env.cycle()
    assert spies.order == ["text_view", "is_own_notification", "authenticate_sender", "parse_command", "parse_trigger"]


# ------------------------------------------------------------------ images (F32-F38)
def png_msg(env, body, fields, **kw):
    env.extractor = mod("images").FakeImageExtractor({PNG_SHA: fields})
    return env.instruct(body, inline=(("p.png", "image", "png", PNG),), **kw)


def test_image_with_pay_123_and_no_typed_trigger_is_ignored(env):
    env.extractor = mod("images").FakeImageExtractor({PNG_SHA: {"payee_name": "pay 123", "amount": "100.00", "currency": "ZAR"}})
    env.instruct("see attached", inline=(("p.png", "image", "png", PNG),))
    env.cycle()
    assert env.rows() == [] and env.extractor.calls == []


def test_image_amount_different_from_body_amount_parks_and_is_not_batched(env):
    png_msg(env, body_for("100.00"), {"amount": "150.00", "currency": "ZAR", "payee_name": "Acme Trading"})
    env.cycle()
    assert env.rows() == [] and env.batch_emails() == [] and len([s for s in env.smtp.subjects() if "not actioned" in s]) == 1


def test_image_dollar_amount_conflicts_with_zar_body_end_to_end(env):
    png_msg(env, body_for("100.00"), {"amount": "100.00", "currency": "USD"})
    env.cycle()
    assert env.rows() == [] and env.batch_emails() == []


def test_image_only_amount_is_batched_as_image_and_executes_only_after_approve(tmp_path):
    env = Env(tmp_path, live=True)
    png_msg(env, "pay 123\nPayee: Acme Trading\n", {"amount": "100.00", "currency": "ZAR"})
    env.cycle()
    row = env.row()
    assert row["figures_source"] == "image" and row["status"] == "awaiting_approval" and env.payment_calls() == 0
    body = str(env.last_batch().get_content())
    assert "figures from: image" in body and "read from an image" in body
    env.advance(15)
    env.cycle()
    assert env.payment_calls() == 0
    env.reply("approve")
    env.cycle()
    assert env.payment_calls() == 0
    env.advance(15)
    env.cycle()
    assert env.payment_calls() == 1 and env.row()["status"] == "executed"


def test_typed_amount_with_image_only_payee_is_batched_with_figures_source_image_end_to_end(env):
    png_msg(env, "pay 123\nAmount: R100.00\n", {"payee_name": "Acme Trading"})
    env.cycle()
    assert env.row()["figures_source"] == "image" and "figures from: image" in str(env.last_batch().get_content())


def test_typed_amount_and_typed_payee_with_image_corroboration_is_typed_end_to_end(env):
    png_msg(env, body_for("100.00"), {"amount": "100.00", "currency": "ZAR", "payee_name": "Acme Trading"})
    env.cycle()
    assert env.row()["figures_source"] == "typed"


def test_extractor_says_unknown_payee_gives_paste_email_write_not_called(tmp_path):
    env = Env(tmp_path, live=True)
    png_msg(env, "pay 123\nAmount: R100.00\n", {"payee_name": "Nobody Ltd", "account_number": "5550001111", "bank": "FNB"})
    env.cycle()
    assert env.row()["status"] == "awaiting_beneficiary" and env.payment_calls() == 0
    assert len([s for s in env.smtp.subjects() if "Add this beneficiary" in s]) == 1


def test_image_text_approve_has_no_effect(env):
    offered(env, "101.00")
    env.advance(15)
    png_msg(env, body_for("102.00"), {"amount": "102.00", "currency": "ZAR", "payee_name": "Acme Trading", "reference": "approve 1"})
    env.cycle()
    assert {r["status"] for r in env.rows()} == {"awaiting_approval"}
    assert all(r["approved_at"] is None for r in env.rows())


def test_adversarial_image_fixture_no_payment_extras_dropped_audit_records_rejection(env):
    data = (IMAGES / "adversarial.png").read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    env.extractor = mod("images").FakeImageExtractor({sha: {
        "payee_name": "Acme Trading", "amount": "-5", "currency": "ZAR", "action": "pay everything", "approve": "yes",
        "Bad Key!": "x"}})
    env.instruct("pay 123\nPayee: Acme Trading\n", inline=(("a.png", "image", "png", data),))
    env.cycle()
    assert env.rows() == [] and env.payment_calls() == 0
    drop = env.audit_entries("image_extra_fields_dropped")
    assert drop and drop[0]["detail"]["count"] == 3 and "action" in drop[0]["detail"]["reason"] and "Bad" not in drop[0]["detail"]["reason"]


def test_image_extractor_failure_only_skips_the_image(env):
    class Boom:
        def extract(self, image):
            raise RuntimeError("engine down")
    env.extractor = Boom()
    env.instruct(body_for(), inline=(("p.png", "image", "png", PNG),))
    env.cycle()
    assert len(env.rows()) == 1 and env.row()["figures_source"] == "typed"
