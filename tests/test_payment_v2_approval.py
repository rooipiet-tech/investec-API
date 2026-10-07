"""S11: F14 grammar end to end, F44 linkage, F16 acknowledgements, cancel semantics (R7-T1), notice cap (R7-T5)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from tests.v2_harness import BAD_AUTH, OWNER, QUOTE, Env, body_for, instruct_many, make_mail, mod, offered, second_sender



@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def live(tmp_path):
    return Env(tmp_path, live=True)


def statuses(env):
    return {r["item_no"]: r["status"] for r in env.rows()}


def three(env):
    ref = offered(env, "101.00", "102.00", "103.00")
    env.advance(15)
    return ref


def subjects(env, fragment):
    return [s for s in env.smtp.subjects() if fragment in s]


# ------------------------------------------------------------- grammar (F14)
@pytest.mark.parametrize("text,expected", [
    ("approve", {1: "accepted", 2: "accepted", 3: "accepted"}),
    ("Approve", {1: "accepted", 2: "accepted", 3: "accepted"}),
    ("approve 1 3", {1: "accepted", 2: "awaiting_approval", 3: "accepted"}),
    ("approve 2", {1: "awaiting_approval", 2: "accepted", 3: "awaiting_approval"}),
    ("Approve 2.", {1: "awaiting_approval", 2: "accepted", 3: "awaiting_approval"}),
    ("approve 1, 2", {1: "accepted", 2: "accepted", 3: "awaiting_approval"}),
    ("cancel 2", {1: "awaiting_approval", 2: "cancelled", 3: "awaiting_approval"}),
    ("cancel 1 3", {1: "cancelled", 2: "awaiting_approval", 3: "cancelled"}),
    ("cancel", {1: "cancelled", 2: "cancelled", 3: "cancelled"}),
])
def test_valid_commands_apply(env, text, expected):
    three(env)
    env.reply(text)
    env.cycle()
    assert statuses(env) == expected
    assert len(subjects(env, "Reply received")) == 1                       # one acknowledgement per processed reply


@pytest.mark.parametrize("text", [
    "approve 9", "approve 0", "approve x", "approve cancel", "please approve", "approve 1 1", "",
    "do not approve, the amount is wrong", "hello\napprove", "approve 1 9", "cancel 9", "cancel please",
    "approve\ncancel",
])
def test_invalid_replies_change_nothing_and_get_a_not_understood_notice(env, text):
    three(env)
    env.reply(text)
    env.cycle()
    assert set(statuses(env).values()) == {"awaiting_approval"} and env.payment_calls() == 0
    assert len(subjects(env, "Reply not understood")) == 1
    env.advance(15)
    env.cycle()
    assert set(statuses(env).values()) == {"awaiting_approval"}


def test_approve_only_in_a_quoted_block_is_not_applied(env):
    three(env)
    env.reply("Thanks, will do.", quote=False)
    env.inbox.pending.clear()
    batch = env.last_batch()
    env.inbox.queue(make_mail("Thanks\n\nOn Wed, 7 Oct 2026 at 10:00, Invespend <bot@example.com> wrote:\n> approve\n",
                              subject=f"Re: {batch['Subject']}", in_reply_to=str(batch["Message-ID"]),
                              internaldate=env.now - timedelta(minutes=1)))
    env.cycle()
    assert set(statuses(env).values()) == {"awaiting_approval"} and len(subjects(env, "Reply not understood")) == 1


def test_do_not_approve_reply_never_approves_end_to_end(env):
    three(env)
    env.reply("do not approve, the amount is wrong")
    env.cycle()
    env.advance(15)
    env.cycle()
    assert set(statuses(env).values()) == {"awaiting_approval"} and env.payment_calls() == 0


def test_quote_stripped_reply_gets_not_understood_and_changes_nothing(env):
    three(env)
    env.reply("approve", quote=False)                   # In-Reply-To present, no quote boundary: layout unconfident
    env.cycle()
    assert set(statuses(env).values()) == {"awaiting_approval"}
    body = "\n".join(str(m.get_content()) for m in env.smtp.sent if "not understood" in str(m["Subject"]))
    assert mod("notify_v2").CANCEL_CAVEAT in body


def test_quote_stripped_approve_is_not_applied_and_gets_not_understood(env):
    three(env)
    env.reply("approve 1", quote=False)
    env.cycle()
    assert set(statuses(env).values()) == {"awaiting_approval"} and len(subjects(env, "Reply not understood")) == 1


def test_bare_cancel_cancels_every_unclaimed_item_of_the_batch(live):
    env = live
    ref = three(env)
    env.reply("approve 3")
    env.cycle()                                                       # item 3 accepted
    env.advance(15)
    env.cycle()                                                       # item 3 executed (earlier cycle)
    env.advance(15)
    env.reply("approve 2")
    env.cycle()                                                       # item 2 accepted, NOT yet claimed
    assert statuses(env) == {1: "awaiting_approval", 2: "accepted", 3: "executed"}
    before = env.payment_calls()
    env.advance(5)
    env.reply("cancel")
    env.cycle()
    assert statuses(env) == {1: "cancelled", 2: "cancelled", 3: "executed"}
    ack = [m for m in env.smtp.sent if "Reply received" in str(m["Subject"])][-1]
    text = str(ack.get_content())
    assert "cancelled: 1, 2" in text and "3 (too late)" in text
    env.advance(15)
    env.cycle()
    assert env.payment_calls() == before == 1 and ref in str(ack["Subject"])


def test_bare_cancel_of_accepted_item_before_next_cycle_never_executes(live):
    env = live
    three(env)
    env.reply("approve")
    env.cycle()
    assert set(statuses(env).values()) == {"accepted"}
    env.advance(15)
    env.reply("cancel")
    env.cycle()                                                       # cancel handled BEFORE execution in the same cycle
    assert set(statuses(env).values()) == {"cancelled"} and env.payment_calls() == 0
    env.advance(15)
    env.cycle()
    assert env.payment_calls() == 0


def test_cancel_won_during_message_handling_item_in_snapshot_not_executed(live):
    env = live
    offered(env, "101.00")
    env.advance(15)
    env.reply("approve")
    env.cycle()
    env.advance(15)
    env.reply("cancel 1")
    summary = env.cycle()                                             # row is in this cycle's snapshot but no longer accepted
    assert env.row()["status"] == "cancelled" and env.payment_calls() == 0 and summary["executed"] == 0


def test_quote_stripped_bare_cancel_is_honoured_from_the_raw_first_line(env):
    three(env)
    env.reply("approve")
    env.cycle()
    env.advance(15)
    env.reply("cancel", quote=False)
    env.cycle()
    assert set(statuses(env).values()) == {"cancelled"}
    assert env.audit_entries("cancel_from_raw_first_line")


def test_quote_stripped_cancel_n_is_honoured_from_the_raw_first_line(env):
    three(env)
    env.reply("cancel 2", quote=False)
    env.cycle()
    assert statuses(env) == {1: "awaiting_approval", 2: "cancelled", 3: "awaiting_approval"}


def test_unconfident_raw_cancel_with_approve_on_a_later_raw_line_is_a_noop(env):
    three(env)
    env.reply("cancel\napprove", quote=False)
    env.cycle()
    assert set(statuses(env).values()) == {"awaiting_approval"} and len(subjects(env, "Reply not understood")) == 1


def test_bare_cancel_without_batch_ref_gets_batch_not_matched_and_changes_nothing(env):
    three(env)
    env.reply("cancel", subject_ref=False, in_reply_to=False)
    env.cycle()
    assert set(statuses(env).values()) == {"awaiting_approval"} and len(subjects(env, "not matched")) == 1


def test_bare_cancel_with_two_distinct_batch_refs_is_noop(env):
    first = three(env)
    instruct_many(env, "104.00")
    env.cycle()
    second = env.last_batch()
    first_batch = env.batch_emails()[0]
    env.advance(15)
    env.inbox.queue(make_mail("cancel" + QUOTE, subject=f"Re: {first_batch['Subject']}", in_reply_to=str(second["Message-ID"]),
                              internaldate=env.now - timedelta(minutes=1)))
    env.cycle()
    assert "cancelled" not in statuses(env).values() and len(subjects(env, "not matched")) == 1 and first != env.batch_ref(second)


def test_bare_cancel_from_other_allowlisted_sender_or_unauthenticated_cancels_nothing(env):
    kw = second_sender(env)
    three(env)
    env.reply("cancel", **kw)                                          # other allowlisted sender: not the approver
    env.reply("cancel", auth=BAD_AUTH)                                 # unauthenticated
    before = len(env.smtp.sent)
    env.cycle()
    assert set(statuses(env).values()) == {"awaiting_approval"}
    new = env.smtp.sent[before:]
    assert [str(m["To"]) for m in new] == ["anna@example.com"] and "not matched" in str(new[0]["Subject"])


def test_bare_cancel_mail_older_than_age_window_still_cancels(env):
    three(env)
    env.reply("cancel", internaldate=env.now - timedelta(hours=48))
    env.cycle()
    assert set(statuses(env).values()) == {"cancelled"}


def test_cancel_mail_older_than_age_window_still_cancels_cancel_never_age_gated(env):
    three(env)
    env.reply("cancel 1", internaldate=None, received_header=None)     # no trusted receipt time at all
    env.cycle()
    assert statuses(env)[1] == "cancelled"


def test_not_understood_notice_for_cancel_forms_states_approved_items_still_run(live):
    env = live
    offered(env, "101.00")
    env.advance(15)
    env.reply("approve")
    env.cycle()
    for text in ("cancel please", "cancel 9"):
        env.advance(1)
        env.reply(text)
    env.cycle()
    assert env.row()["status"] in ("accepted", "executed")
    caveat = mod("notify_v2").CANCEL_CAVEAT
    notices = [str(m.get_content()) for m in env.smtp.sent if "Reply not understood" in str(m["Subject"])]
    assert notices and all(caveat in n for n in notices)
    env.advance(15)
    env.cycle()
    assert env.row()["status"] == "executed" and env.payment_calls() == 1


def test_cancel_drops_listed_items_multiple_numbers_allowed(env):
    three(env)
    env.reply("cancel 1 3")
    env.cycle()
    assert statuses(env) == {1: "cancelled", 2: "awaiting_approval", 3: "cancelled"}


def test_partial_approval_leaves_unmentioned_items_pending_until_expiry_and_relisted_next_batch(env):
    ref = three(env)
    env.reply("approve 1")
    env.cycle()
    assert statuses(env) == {1: "accepted", 2: "awaiting_approval", 3: "awaiting_approval"}
    env.advance(15)
    instruct_many(env, "104.00")
    env.cycle()
    body = str(env.last_batch().get_content())
    assert f"{ref} #2" in body and f"{ref} #3" in body and f"{ref} #1" not in body


# ------------------------------------------------------------ linkage (F44)
def test_reply_with_re_subject_matches_the_batch(env):
    three(env)
    env.reply("approve 1", in_reply_to=False)
    env.cycle()
    assert statuses(env)[1] == "accepted"


def test_reply_with_in_reply_to_only_matches_the_batch(env):
    three(env)
    env.reply("approve 2", subject_ref=False)
    env.cycle()
    assert statuses(env)[2] == "accepted"


def test_reply_with_different_message_id_still_matches_the_batch(env):
    three(env)
    env.reply("approve 3")
    env.cycle()
    assert statuses(env)[3] == "accepted"


def test_reply_with_two_distinct_batch_refs_is_noop_with_not_matched_notice(env):
    three(env)
    instruct_many(env, "104.00")
    env.cycle()
    a, b = env.batch_emails()
    env.advance(15)
    env.inbox.queue(make_mail("approve" + QUOTE, subject=str(a["Subject"]).replace("Payments", "Re: Payments"),
                              in_reply_to=str(b["Message-ID"]), internaldate=env.now - timedelta(minutes=1)))
    env.cycle()
    assert "accepted" not in statuses(env).values() and len(subjects(env, "not matched")) == 1


def test_batch_ref_typed_in_body_does_not_link(env):
    ref = three(env)
    env.reply(f"approve\n{ref}", subject_ref=False, in_reply_to=False)
    env.cycle()
    assert set(statuses(env).values()) == {"awaiting_approval"} and len(subjects(env, "not matched")) == 1


def test_unauthenticated_sender_knowing_the_ref_approves_nothing_and_gets_no_reply(env):
    three(env)
    before = len(env.smtp.sent)
    env.reply("approve", auth=BAD_AUTH)
    env.reply("approve", auth=None)
    env.cycle()
    assert set(statuses(env).values()) == {"awaiting_approval"} and len(env.smtp.sent) == before
    assert env.audit_entries("auth_failed")


def test_approval_from_other_allowlisted_sender_does_not_act_on_the_batch(env):
    kw = second_sender(env)
    three(env)
    env.reply("approve", **kw)
    env.cycle()
    assert set(statuses(env).values()) == {"awaiting_approval"} and subjects(env, "not matched")


def test_item_numbers_unique_and_stable_across_reruns(env):
    three(env)
    before = sorted((r["batch_ref"], r["item_no"]) for r in env.rows())
    env.run_cycles(3)
    assert sorted((r["batch_ref"], r["item_no"]) for r in env.rows()) == before


# ------------------------------------------- stale approvals and acknowledgements
def test_stale_approve_from_unauthenticated_sender_gets_no_email(env):
    three(env)
    before = len(env.smtp.sent)
    env.reply("approve", auth=BAD_AUTH, internaldate=env.now - timedelta(hours=48))
    env.cycle()
    assert len(env.smtp.sent) == before and set(statuses(env).values()) == {"awaiting_approval"}


def test_approve_mail_older_than_age_window_gets_ack_expired_and_changes_nothing(env):
    three(env)
    env.reply("approve", internaldate=env.now - timedelta(hours=30))
    env.cycle()
    assert set(statuses(env).values()) == {"awaiting_approval"}
    ack = str([m for m in env.smtp.sent if "Reply received" in str(m["Subject"])][-1].get_content())
    assert "1 (expired)" in ack and "2 (expired)" in ack


def test_approve_with_no_trusted_receipt_time_fails_closed_with_ack_expired(env):
    three(env)
    env.reply("approve", internaldate=None)
    env.cycle()
    assert set(statuses(env).values()) == {"awaiting_approval"} and subjects(env, "Reply received")


def test_approval_predating_the_batch_gets_ack_not_applied_and_changes_nothing(env):
    three(env)
    offered_at = env.row()["offered_at"]
    env.reply("approve", internaldate=offered_at - timedelta(minutes=1))
    env.cycle()
    assert set(statuses(env).values()) == {"awaiting_approval"}
    ack = str([m for m in env.smtp.sent if "Reply received" in str(m["Subject"])][-1].get_content())
    assert "reply predates batch" in ack


def test_approve_after_expiry_is_noop_and_ack_says_expired(env):
    three(env)
    env.advance(25 * 60)
    env.reply("approve")
    env.cycle()                                                        # sweep expires the rows AFTER message handling
    rows = statuses(env)
    assert set(rows.values()) <= {"awaiting_approval", "expired"} and "accepted" not in rows.values()
    ack = str([m for m in env.smtp.sent if "Reply received" in str(m["Subject"])][-1].get_content())
    assert "expired" in ack
    env.advance(15)
    env.reply("approve 1")
    env.cycle()
    assert set(statuses(env).values()) == {"expired"} and env.payment_calls() == 0


def test_approve_at_offer_plus_23h59_executes_next_cycle(live):
    env = live
    offered(env, "101.00")
    env.advance(23 * 60 + 40)
    env.reply("approve")
    env.cycle()
    assert env.row()["status"] == "accepted"
    env.advance(15)
    env.cycle()
    assert env.row()["status"] == "executed" and env.payment_calls() == 1


def test_reapprove_of_executed_item_is_noop_and_ack_says_already_executed(env):
    offered(env, "101.00")
    env.advance(15)
    env.reply("approve")
    env.cycle()
    env.advance(15)
    env.cycle()
    assert env.row()["status"] == "executed"
    env.advance(15)
    env.reply("approve 1")
    env.cycle()
    ack = str([m for m in env.smtp.sent if "Reply received" in str(m["Subject"])][-1].get_content())
    assert "1 (already executed)" in ack and env.row()["status"] == "executed"


def test_same_approval_reply_processed_twice_does_not_double_execute(live):
    env = live
    offered(env, "101.00")
    env.advance(15)
    reply = env.reply("approve")
    env.cycle()
    env.inbox.queue(reply)                                              # same Message-ID again: skipped by message_seen
    env.advance(15)
    env.cycle()
    env.advance(15)
    env.reply("approve")                                                # a DIFFERENT mail also saying approve
    env.cycle()
    env.advance(15)
    env.cycle()
    assert env.payment_calls() == 1 and env.row()["status"] == "executed"


def test_ack_email_sent_once_per_processed_reply(env):
    three(env)
    reply = env.reply("approve 1")
    env.cycle()
    env.inbox.queue(reply)
    env.advance(15)
    env.cycle()
    assert len(subjects(env, "Reply received")) == 1


def test_reply_to_batch_with_typed_pay_instruction_is_not_a_new_instruction(env):
    three(env)
    rows = len(env.rows())
    env.reply(body_for("500.00"))
    env.cycle()
    assert len(env.rows()) == rows and len(subjects(env, "Reply not understood")) == 1


def test_approve_with_no_linkage_typed_gets_batch_not_matched_and_is_never_guessed(env):
    three(env)
    env.reply("approve", subject_ref=False, in_reply_to=False)
    env.cycle()
    assert set(statuses(env).values()) == {"awaiting_approval"} and len(subjects(env, "not matched")) == 1


def test_not_understood_and_batch_not_matched_notices_capped_at_one_per_address_per_cycle(env):
    three(env)
    env.reply("please approve")
    env.reply("do not approve")
    env.reply("approve 9")
    before = len(env.smtp.sent)
    env.cycle()
    assert len(env.smtp.sent) - before == 1
    suppressed = env.audit_entries("notice_suppressed")
    assert max(e["detail"]["count"] for e in suppressed) == 2
    env.advance(15)
    env.reply("approve 1")                                             # an ack for a valid command is never suppressed
    env.reply("approve 2")
    env.reply("junk words")
    env.cycle()
    assert len(subjects(env, "Reply received")) == 2 and len(subjects(env, "Reply not understood")) == 2


def test_approval_handler_never_executes_and_direct_unit(env):
    three(env)
    approval = mod("approval")
    cmd = mod("commands").Command("approve_all", (), "", "typed")
    ref = env.batch_ref()
    out = approval.handle_approval_message(
        env.settings, env.store, env.audit, auth_from=OWNER, batch_refs=[ref], cmd=cmd, received_at=env.now,
        now=env.now, smtp_send=env.smtp)
    assert out == "approval_applied" and set(statuses(env).values()) == {"accepted"} and env.payment_calls() == 0
    out2 = approval.handle_approval_message(
        env.settings, env.store, env.audit, auth_from=OWNER, batch_refs=[ref, "B-0000-0000"], cmd=cmd, received_at=env.now,
        now=env.now, smtp_send=env.smtp)
    assert out2 == "approval_noop"


@pytest.mark.parametrize("numbers", [(0,), (4,), (1, 4), (999,)])
def test_handler_range_checks_numbers_against_the_batch(env, numbers):
    three(env)
    approval = mod("approval")
    for kind in ("approve", "cancel"):
        cmd = mod("commands").Command(kind, numbers, "", "typed")
        out = approval.handle_approval_message(
            env.settings, env.store, env.audit, auth_from=OWNER, batch_refs=[env.batch_ref()], cmd=cmd,
            received_at=env.now, now=env.now, smtp_send=env.smtp)
        assert out == "approval_noop"
    assert set(statuses(env).values()) == {"awaiting_approval"}


def test_age_window_zero_means_nothing_is_fresh_even_a_future_dated_reply(tmp_path):
    env = Env(tmp_path)
    ref = offered(env, "101.00")
    env.settings.payments_max_message_age_hours = 0.0
    env.advance(15)
    env.reply("approve", internaldate=env.now + timedelta(hours=1))
    env.cycle()
    assert env.row()["status"] == "awaiting_approval" and ref
    assert "1 (expired)" in str([m for m in env.smtp.sent if "Reply received" in str(m["Subject"])][-1].get_content())
