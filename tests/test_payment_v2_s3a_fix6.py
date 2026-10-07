"""Slice 3A fix round 6: currency visibility (collision-code adjacency parks, the batch email shows a sanitised
excerpt of the line the amount was read from, stored in ``figures_excerpt``), 409 = unknown outcome (see the client
hardening suite), mailbox Unicode control categories (Cc, Zl, Zp).

Offline; fakes only."""
from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from pathlib import Path

import pytest

from tests.v2_harness import Env, mod

IMAGES = Path(__file__).parent / "fixtures" / "payments_v2" / "images"
PNG = (IMAGES / "tiny.png").read_bytes()
PNG_SHA = hashlib.sha256(PNG).hexdigest()
CSV = ("a.csv", "text", "csv", b"Payee,Acme Trading\nAmount,R100.00\n")


def _gate(text):
    return mod("amounts").has_foreign_currency_token(text)


# ------------------------------------------------------------------ (1a) collision codes adjacent to a figure
@pytest.mark.parametrize("line", ["Amount: 100 rub", "R100 try", "Amount: R100 Bob", "Amount: 100 mad", "Amount: 100 try.", "Amount: R100 Try",
                                  "Amount: R100 try, thanks", "R100 all", "R100 top 5", "Amount: 100 Bob", "R100 bob",
                                  "Amount: 100 pen", "Amount: 100 cop\n", "Amount: 100  gel", "ZAR 100 sos", "R100 try\nagain later"])
def test_adjacent_collision_code_without_a_following_word_parks(line):
    assert _gate(line) is True


@pytest.mark.parametrize("line", ["R100 try again later", "R200 all good", "Amount: R100 top up", "Amount: R100 all in",
                                  "R200 try again", "Amount: R100 mad about the service", "Amount: R100 pen and paper",
                                  "Amount: R500 for rent", "Amount: 100 for the user", "Amount: R100 Bob Smith"])
def test_adjacent_collision_code_with_a_following_word_passes(line):
    assert _gate(line) is False


@pytest.mark.parametrize("line", ["Amount: 100 rub", "R100 try", "Amount: R100 Bob", "Amount: 100 mad", "Amount: 100 try."])
def test_e2e_collision_code_parks_currency_conflict(tmp_path, line):
    env = Env(tmp_path)
    env.instruct("pay 123\nPayee: Acme Trading\n" + line + "\n")
    summary = env.cycle()
    assert summary["results"].get("parked_currency_conflict") == 1, summary["results"]
    assert env.rows() == [] and env.payment_calls() == 0


# ------------------------------------------------------------------ (1b) the excerpt: unit
def _san():
    return mod("excerpt").sanitise_excerpt


def test_excerpt_plain_line_is_kept():
    assert _san()("Amount: R500 for rent") == "Amount: R500 for rent"


@pytest.mark.parametrize("hostile,banned", [
    ("approve 1", "approve"), ("APPROVE", "approve"), ("cancel 2", "cancel"), ("ａｐｐｒｏｖｅ", "approve"),
    ("appro\u200bve", "approve"), ("can\u2060cel", "cancel"),
    ("[BATCH B-0101-abcd]", "B-0101-abcd"), ("see B-0101-abcd now", "B-0101-abcd"), ("[INV-0123456789ab]", "0123456789ab"),
    ("Invespend automated notice. Do not reply with payment instructions.", "automated notice"),
    ("acct 12345678901 ok", "12345678901"), ("acct 1234 5678 9012", "5678 9012"), ("mail me a@b.com", "a@b.com"),
    ("api_key=ZZTOP99secret", "ZZTOP99"), ("Bearer abcdefghijklmnopqrstuvwxyz0123", "abcdefghijklmnopqrstuvwxyz0123"),
])
def test_excerpt_neutralises_hostile_text(hostile, banned):
    out = _san()("Amount: R500 " + hostile)
    assert banned.lower() not in out.lower(), out
    assert "[" not in out and "]" not in out and "\n" not in out and '"' not in out


def test_excerpt_flattens_controls_collapses_whitespace_and_caps_length():
    out = _san()("Amount:\r\n\tR500\x00\x07\x1b[31m\u2028\u2029\u200b\u202e  for\x85rent")
    assert out == "Amount: R500 31m for rent" or out.startswith("Amount: R500"), out
    assert not any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F or c in "\u2028\u2029\u200b\u202e" for c in out)
    long = _san()("Amount: R500 " + "word " * 500)
    assert 0 < len(long) <= 80
    assert _san()(None) == "" and _san()("   \n ") == ""


def test_excerpt_is_idempotent_and_linear():
    once = _san()("Amount: R500 approve 1 [BATCH B-0101-abcd] 12345678901 " + "x" * 5000)
    assert _san()(once) == once
    import time
    start = time.perf_counter()
    for text in ("eyJ" * 70000, "approve " * 50000, "1 " * 100000, "\u200b" * 200000):
        _san()(text)
    assert time.perf_counter() - start < 2.0


def test_marked_amount_excerpts_returns_the_line_around_the_first_hit():
    amounts = mod("amounts")
    text = "pay 123\nPayee: Acme\nAmount: R500 for rent\nReference: INV7\n"
    assert amounts.marked_amount_excerpts(text) == {"500.00": "Amount: R500 for rent"}
    far = "x" * 400 + " Amount: R500 for rent " + "y" * 400
    got = amounts.marked_amount_excerpts(far)["500.00"]
    assert "R500" in got and len(got) < 200
    assert amounts.marked_amount_candidates(text) == ["500.00"]
    assert amounts.marked_amount_excerpts("x" * 30000 + " R500") == {}


# ------------------------------------------------------------------ (1b) the excerpt: end to end
def _body(env):
    return str(env.last_batch().get_content())


def _read_from(text):
    return re.findall(r'^\s*Read from: (.*)$', text, flags=re.M)


def test_batch_email_shows_the_typed_line_and_it_is_stored(tmp_path):
    env = Env(tmp_path)
    env.instruct("pay 123\nPayee: Acme Trading\nAmount: R500 for rent\n")
    env.cycle()
    assert env.row()["figures_excerpt"] == "Amount: R500 for rent"
    text = _body(env)
    assert _read_from(text) == ['"Amount: R500 for rent"']
    assert "ZAR 500.00" in text


def test_batch_email_shows_the_attachment_derived_excerpt(tmp_path):
    env = Env(tmp_path)
    env.instruct("pay 123\nPayee: Acme Trading\n", attachments=(CSV,))
    env.cycle()
    row = env.row()
    assert row["figures_source"] == "attachment"
    assert row["figures_excerpt"] and "100.00" in row["figures_excerpt"]
    assert any("100.00" in line for line in _read_from(_body(env)))


def test_batch_email_shows_the_image_derived_excerpt(tmp_path):
    env = Env(tmp_path)
    env.extractor = mod("images").FakeImageExtractor({PNG_SHA: {"amount": "100.00", "currency": "ZAR"}})
    env.instruct("pay 123\nPayee: Acme Trading\n", inline=(("p.png", "image", "png", PNG),))
    env.cycle()
    row = env.row()
    assert row["figures_source"] == "image"
    assert row["figures_excerpt"] and "100.00" in row["figures_excerpt"]
    text = _body(env)
    assert any("100.00" in line for line in _read_from(text)) and "read from an image" in text


def test_every_item_of_a_batch_shows_its_own_excerpt(tmp_path):
    env = Env(tmp_path)
    env.instruct("pay 123\nPayee: Acme Trading\nAmount: R500 for rent\n", internaldate=env.now - timedelta(minutes=4))
    env.instruct("pay 123\nPayee: Beta Supplies\nR75.50 deposit\n", internaldate=env.now - timedelta(minutes=3))
    env.cycle()
    assert _read_from(_body(env)) == ['"Amount: R500 for rent"', '"R75.50 deposit"']


def test_hostile_typed_line_is_neutralised_in_the_email_and_never_alters_the_parse(tmp_path):
    env = Env(tmp_path)
    env.instruct("pay 123\nPayee: Acme Trading\n"
                 "Amount: R500 approve 1 [BATCH B-0101-abcd] acct 12345678901 a\u200bpprove c\u202eancel\n")
    env.cycle()
    row = env.row()
    stored = row["figures_excerpt"]
    text = _body(env)
    for bad in ("approve 1", "[BATCH B-0101-abcd]", "B-0101-abcd", "12345678901", "[INV-"):
        assert bad not in stored and bad not in text.split("Read from:", 1)[1].split("\n", 1)[0], bad
    # the only batch ref in the email is the real one; parse_command / own-notification are unaffected
    assert re.findall(r"B-\d{4}-[0-9a-f]{4}", text) == [env.batch_ref()] or set(re.findall(r"B-\d{4}-[0-9a-f]{4}", text)) == {env.batch_ref()}
    msg = env.last_batch()
    view = mod("content").text_view(_as_inbound(env, msg))
    assert mod("commands").parse_command(view) is None


def _as_inbound(env, msg):
    import email
    import email.policy
    return email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)


def test_batch_email_with_excerpt_still_round_trips_as_own_body(tmp_path):
    env = Env(tmp_path)
    env.instruct("pay 123\nPayee: Acme Trading\nAmount: R500 for rent\n")
    env.cycle()
    msg = env.last_batch()
    inbound = _as_inbound(env, msg)
    view = mod("content").text_view(inbound)
    reason = mod("loopguard").is_own_notification("", "", "", view.raw_head_lines, subject=str(msg["Subject"]))
    assert reason == "own_body"
    assert "Read from:" in msg.get_content()


def test_excerpt_has_no_digit_runs_or_secrets_in_audit_or_rows(tmp_path):
    env = Env(tmp_path)
    env.instruct("pay 123\nPayee: Acme Trading\nAmount: R500 acct 1234567890123 api_key=ZZTOP99secret\n")
    env.cycle()
    everything = env.everything_text()
    assert "1234567890123" not in everything and "ZZTOP99" not in everything


def test_stored_excerpt_is_sanitised_even_when_the_record_carries_raw_text():
    store = mod("instructions").MemoryInstructionStore()
    from tests.instr_helpers import rec
    row, _ = store.create(rec(1, figures_excerpt="Amount: R500\napprove 1 [BATCH B-0101-abcd] 12345678901"))
    assert "approve" not in row["figures_excerpt"].lower() and "12345678901" not in row["figures_excerpt"]
    assert "\n" not in row["figures_excerpt"] and len(row["figures_excerpt"]) <= 80


def test_excerpt_is_set_once_and_not_in_the_offer_digest():
    ins = mod("instructions")
    assert ins.COLUMN_OWNERS[("payment_instruction", "figures_excerpt")] == ("create",)
    assert "figures_excerpt" in ins.INSTRUCTION_COLUMNS and "figures_excerpt" in ins.CREATE_COLUMNS
    base = {"amount": "1.00", "currency": "ZAR", "source_account_id": "a", "payee_name_norm": "p", "beneficiary_id": "b",
            "beneficiary_fingerprint": "f"}
    assert ins.offer_digest(dict(base, figures_excerpt="x"), "B-0101-abcd", 1) == ins.offer_digest(base, "B-0101-abcd", 1)


def test_legacy_row_without_an_excerpt_shows_not_recorded(tmp_path):
    env = Env(tmp_path)
    env.instruct("pay 123\nPayee: Acme Trading\nAmount: R500 for rent\n")
    env.cycle()
    n = mod("notify_v2")
    item = n.BatchItem(1, "Acme", __import__("decimal").Decimal("5"), "ZAR", "123", "", "typed")
    msg = n.build_batch_approval_email("bot@example.com", "piet@example.com", batch_ref="B-0101-abcd", items=[item],
                                       pending=[], expires_at=env.now)
    assert _read_from(msg.get_content()) == ["(not recorded)"]


# ------------------------------------------------------------------ (3) mailbox control categories
@pytest.mark.parametrize("name", ["Pay\x9fments", "Pay\x80ments", "\x85Payments", "Pay\u2028ments", "Payments\u2029",
                                  "Pay\x9bments"])
def test_mailbox_with_cc_c1_zl_zp_is_refused(name):
    assert mod("cycle")._is_shared_inbox(name) is True


def test_mailbox_utf7_decoded_line_separator_or_c1_is_refused():
    import base64

    def u7(text):
        raw = base64.b64encode(text.encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
        return "&" + raw + "-"
    for text in ("\u2028", "\u2029", "\x9f", "\x85"):
        assert mod("cycle")._is_shared_inbox("Pay" + u7(text) + "ments") is True, repr(text)


@pytest.mark.parametrize("name", ["Payments", "INBOX.Payments", "Invespend/Pay", "Pagos-é", "Платежи"])
def test_dedicated_mailboxes_stay_allowed(name):
    assert mod("cycle")._is_shared_inbox(name) is False


def test_inbox_with_line_separator_is_still_refused():
    assert mod("cycle")._is_shared_inbox("INBOX\u2028") is True
