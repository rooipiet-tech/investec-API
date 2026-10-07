from __future__ import annotations

import inspect
import re
from datetime import datetime, timedelta, timezone

from invespend.payments import loopguard, refs

HEX12 = "0123456789ab"


def test_subject_tag():
    assert refs.find_refs(f"Re: [INV-{HEX12}] pay") == [HEX12]
    assert refs.find_ref(f"[INV-{HEX12}]") == HEX12


def test_ref_in_in_reply_to_message_id_form():
    assert refs.find_refs("subject", f"<1.2.3.invespend-notification.{HEX12}@a.com>") == [HEX12]


def test_subject_edited_away_but_in_reply_to_carries_ref():
    assert refs.find_ref("Totally different", f"<1.2.3.invespend-notification.{HEX12}@a.com>") == HEX12


def test_two_distinct_refs_give_find_ref_none():
    other = "ba9876543210"
    assert refs.find_ref(f"[INV-{HEX12}]", f"<1.2.invespend-notification.{other}@a.com>") is None
    assert refs.find_refs(f"[INV-{HEX12}] [INV-{other}]") == [HEX12, other]


def test_lookalike_11_13_hex_ignored():
    assert refs.find_refs("[INV-0123456789a]", "[INV-0123456789abc]") == []
    assert refs.find_refs("<1.2.invespend-notification.0123456789a@a.com>") == []


def test_instruction_id_unparsable_from_hashes_raw_header():
    a = refs.instruction_id_for("<m@x>", ["not an address"])
    b = refs.instruction_id_for("<m@x>", ["other garbage"])
    assert a and b and a != b
    assert a == refs.instruction_id_for("<m@x>", ["  NOT AN ADDRESS "])  # normalised


def test_instruction_id_two_from_headers_hashes_raw_headers():
    a = refs.instruction_id_for("<m@x>", ["a@x.com", "b@x.com"])
    assert a != refs.instruction_id_for("<m@x>", ["a@x.com"])
    assert a != refs.instruction_id_for("<m@x>", ["b@x.com", "a@x.com"])


def test_instruction_id_stable_and_independent_of_body():
    sig = inspect.signature(refs.instruction_id_for)
    assert list(sig.parameters) == ["message_id", "from_headers"]
    one = refs.instruction_id_for("<M@X>", ["Piet <Piet@A.com>"])
    assert one == refs.instruction_id_for("m@x", ["piet@a.com"])
    assert re.fullmatch(r"[0-9a-f]{64}", one)


def test_instruction_id_none_for_empty_message_id():
    assert refs.instruction_id_for("", ["a@x.com"]) is None
    assert refs.instruction_id_for(" <> ", ["a@x.com"]) is None


def test_request_ref_is_first_12_hex():
    assert refs.request_ref("f" * 64) == "f" * 12


# ---- batch refs (F44) -------------------------------------------------------
B = "B-1006-3fa9"


def test_batch_ref_from_subject_tag():
    assert refs.find_batch_refs(f"Re: [BATCH {B}] approval") == [B]


def test_batch_ref_from_in_reply_to_message_id_only():
    assert refs.find_batch_ref("Re: nothing", f"<1.2.3.invespend-notification.{B}@a.com>") == B


def test_batch_ref_from_references_only():
    assert refs.find_batch_ref("x", "", f"<r.1@x> <1.2.3.invespend-notification.{B}@a.com>") == B


def test_two_distinct_batch_refs_give_none():
    assert refs.find_batch_ref(f"[BATCH {B}]", "<1.2.invespend-notification.B-1006-aaaa@a.com>") is None
    assert refs.find_batch_refs(f"[BATCH {B}] [BATCH B-1007-bbbb]") == [B, "B-1007-bbbb"]


def test_same_batch_ref_in_subject_and_in_reply_to_is_one_ref():
    assert refs.find_batch_ref(f"[BATCH {B}]", f"<1.2.invespend-notification.{B}@a.com>") == B


def test_batch_ref_in_body_or_quoted_text_never_counts():
    for fn in (refs.find_batch_refs, refs.find_batch_ref):
        sig = inspect.signature(fn)
        assert [p.kind for p in sig.parameters.values()] == [inspect.Parameter.VAR_POSITIONAL]
        assert "body" not in sig.parameters
    assert not any("body" in n for n in inspect.signature(refs.find_batch_refs).parameters)


def test_lookalike_batch_refs_ignored():
    for text in ("[BATCH B-106-3fa9]", "[BATCH B-1006-3fa]", "[BATCH B-1006-3FA9]",
                 f"{B}", f"see {B} here", f"[BATCH {B}x]", f"BATCH {B}"):
        assert refs.find_batch_refs(text) == [], text
    assert refs.find_batch_refs("<1.2.invespend-notification.B-1006-3FA9@a.com>") == []


def test_instruction_and_batch_ref_families_are_disjoint():
    assert refs.find_batch_refs(f"[INV-{HEX12}]", f"<1.2.invespend-notification.{HEX12}@a.com>") == []
    assert refs.find_refs(f"[BATCH {B}]", f"<1.2.invespend-notification.{B}@a.com>") == []


def test_new_batch_ref_has_no_six_digit_run_and_matches_regex():
    ref = refs.new_batch_ref(datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc), "3fa9")
    assert ref == "B-1006-3fa9"
    assert re.fullmatch(refs.BATCH_REF_RE, ref)
    assert not re.search(r"\d{6,}", ref)


def test_new_batch_ref_uses_sast_date_at_midnight_boundary():
    sast = timezone(timedelta(hours=2))
    just_before = datetime(2026, 10, 6, 23, 59, tzinfo=sast)
    just_after = datetime(2026, 10, 7, 0, 1, tzinfo=sast)
    assert refs.new_batch_ref(just_before.astimezone(timezone.utc), "aaaa") == "B-1006-aaaa"
    assert refs.new_batch_ref(just_after.astimezone(timezone.utc), "aaaa") == "B-1007-aaaa"
    # 22:30 UTC on the 6th is already the 7th in SAST
    assert refs.new_batch_ref(datetime(2026, 10, 6, 22, 30, tzinfo=timezone.utc), "aaaa") == "B-1007-aaaa"


def test_batch_ref_recovered_from_real_make_msgid_output():
    mid = loopguard.loop_guard_headers("me@a.com", B)["Message-ID"]
    assert refs.find_batch_ref("unrelated subject", mid) == B


def test_instruction_ref_recovered_from_real_make_msgid_output():
    mid = loopguard.loop_guard_headers("me@a.com", HEX12)["Message-ID"]
    assert refs.find_ref("unrelated subject", mid) == HEX12
    assert refs.find_batch_refs(mid) == []


def test_msgid_regexes_are_unanchored_and_reject_lookalike_suffixes():
    # unanchored: extra leading make_msgid components do not matter
    assert refs.find_batch_ref("<anything.at.all.invespend-notification." + B + "@a.com>") == B
    for mid in (f"<1.2.invespend-notification.{B}x@a.com>", f"<1.2.invespend-notification.{B}>",
                f"<1.2.xinvespend-notification.{B}@a.com>", f"<1.2.invespend-notification-{B}@a.com>"):
        assert refs.find_batch_refs(mid) == [], mid
    for mid in (f"<1.2.invespend-notification.{HEX12}a@a.com>", f"<1.2.invespend-notification.{HEX12[:-1]}@a.com>"):
        assert refs.find_refs(mid) == [], mid
