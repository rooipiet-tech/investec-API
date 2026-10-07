from __future__ import annotations

from email.message import EmailMessage
from email.utils import make_msgid

import pytest

from invespend.payments import loopguard as lg

SENTINEL = lg.NOTICE_FIRST_LINE


def check(**kw):
    base = dict(message_id="<x@a.com>", auto_submitted="", notification_header="")
    base.update(kw)
    return lg.is_own_notification(**base)


def test_clean_message_is_not_own():
    assert check() is None


@pytest.mark.parametrize("value", ["auto-generated", "auto-replied", "yes"])
def test_auto_submitted_alone_triggers(value):
    assert check(auto_submitted=value) == "auto_submitted"


def test_auto_submitted_no_is_not_automatic():
    assert check(auto_submitted="no") is None


def test_notification_header_alone_triggers():
    assert check(notification_header="1") == "x_invespend"


def test_own_message_id_alone_triggers_with_and_without_ref():
    assert check(message_id="<1.2.3.invespend-notification@a.com>") == "own_message_id"
    assert check(message_id="<1.2.3.invespend-notification.B-1006-3fa9@a.com>") == "own_message_id"
    assert check(message_id="<1.2.3.invespend-notification.0123456789ab@a.com>") == "own_message_id"


def test_real_make_msgid_output_is_own_message_id_with_and_without_ref():
    for idstring in ("invespend-notification", "invespend-notification.B-1006-3fa9",
                     "invespend-notification.0123456789ab"):
        mid = make_msgid(idstring=idstring, domain="a.com")
        assert check(message_id=mid) == "own_message_id", mid
    for ref in (None, "B-1006-3fa9"):
        mid = lg.loop_guard_headers("me@a.com", ref)["Message-ID"]
        assert check(message_id=mid) == "own_message_id"


def test_look_alike_message_id_not_own():
    for mid in ("<invespend-notification@a.com>", "<1.2.3.invespend-notificationX@a.com>",
                "<1.2.3.xinvespend-notification@a.com>", "<1.2.3.invespend_notification@a.com>"):
        assert check(message_id=mid) is None, mid


def test_loop_guard_headers_shape():
    h = lg.loop_guard_headers("me@a.com", "B-1006-3fa9")
    assert h["Auto-Submitted"] == "auto-generated"
    assert h["X-Invespend-Notification"] == "1"
    assert h["Message-ID"].endswith("@a.com>")
    assert "invespend-notification.B-1006-3fa9@a.com" in h["Message-ID"]


def test_apply_loop_guard_sets_all_three_headers_once():
    msg = EmailMessage()
    msg["Auto-Submitted"] = "no"
    lg.apply_loop_guard(msg, "me@a.com", None)
    lg.apply_loop_guard(msg, "me@a.com", None)
    assert msg.get_all("Auto-Submitted") == ["auto-generated"]
    assert len(msg.get_all("Message-ID")) == 1
    assert msg["X-Invespend-Notification"] == "1"


def test_reply_with_references_to_our_msgid_is_not_ignored():
    # The guard takes no References / In-Reply-To argument at all.
    import inspect
    params = inspect.signature(lg.is_own_notification).parameters
    assert "references" not in params and "in_reply_to" not in params
    assert check(message_id="<human@a.com>", subject="Re: [BATCH B-1006-3fa9] x",
                 raw_head_lines=["approve", "", ">" + SENTINEL]) is None


def test_all_three_headers_stripped_body_sentinel_still_ignored():
    assert check(raw_head_lines=[SENTINEL, "x"], subject="Payment batch") == "own_body"


def test_human_reply_quoting_our_notice_with_re_subject_not_ignored():
    assert check(raw_head_lines=["approve", "On Tue wrote:", SENTINEL], subject="Re: batch") is None


def test_sentinel_with_re_prefix_not_ignored():
    for pref in ("Re: x", "RE: x", "Fwd: x", "  fw: x", "Doorst.: x", "AW: x"):
        assert check(raw_head_lines=[SENTINEL], subject=pref) is None, pref


def test_sentinel_on_second_or_third_line_after_banner_still_ignored():
    assert check(raw_head_lines=["External sender", SENTINEL]) == "own_body"
    assert check(raw_head_lines=["External sender", "Careful", SENTINEL]) == "own_body"


def test_sentinel_on_fourth_line_not_matched():
    assert check(raw_head_lines=["a", "b", "c", SENTINEL]) is None


def test_sentinel_must_equal_a_whole_line():
    assert check(raw_head_lines=[SENTINEL + " more"]) is None
    assert check(raw_head_lines=["  " + SENTINEL + "  "]) == "own_body"


@pytest.mark.parametrize("value", ["bulk", "junk", "list", "auto_reply", " BULK ", "Auto_Reply"])
def test_precedence_bulk_junk_list_auto_reply_ignored(value):
    assert check(auto_headers={"precedence": value}) == "bulk_precedence"


@pytest.mark.parametrize("auto", [None, {}, {"precedence": ""}, {"precedence": "normal"},
                                  {"precedence": "first-class"}])
def test_precedence_normal_or_absent_not_ignored(auto):
    assert check(auto_headers=auto) is None


@pytest.mark.parametrize("key", ["x_autoreply", "x_autorespond", "x_auto_response_suppress"])
def test_x_autoreply_x_autorespond_x_auto_response_suppress_ignored(key):
    assert check(auto_headers={key: "All"}) == "auto_reply_header"
    assert check(auto_headers={key: "  "}) is None


def test_auto_reply_header_reason_codes():
    assert check(auto_headers={"x_autoreply": "yes"}) == "auto_reply_header"
    assert check(auto_headers={"precedence": "bulk"}) == "bulk_precedence"


def test_subject_prefix_tuple_is_lowercase():
    assert all(p == p.lower() for p in lg.SUBJECT_REPLY_PREFIXES)
    assert "re:" in lg.SUBJECT_REPLY_PREFIXES and "doorst.:" in lg.SUBJECT_REPLY_PREFIXES
