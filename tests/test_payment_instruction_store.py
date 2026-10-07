"""S9: InstructionStore behaviour. Memory always; Pg only with INVESPEND_TEST_DATABASE_URL."""
from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tests.instr_helpers import (
    GRACE, H, STORE_PARAMS, T0, WINDOW, accepted, approve, ins, make_store, offer, rec, ref_gen,
)



@pytest.fixture(params=STORE_PARAMS)
def store(request):
    return make_store(request.param)


# ------------------------------------------------------------------ create
def test_create_idempotent_returns_created_flag(store):
    row, created = store.create(rec(1))
    assert created is True and row["status"] == "awaiting_approval"
    again, created2 = store.create(rec(1, amount=Decimal("999.00")))
    assert created2 is False and again["amount"] == Decimal("100.00")


def test_create_roundtrip_every_create_owned_column(store):
    held = rec(2, status="held", first_seen_at=T0, eligible_at=T0 + H, account_hmac="hm", recent_beneficiary=True,
               their_reference="their", my_reference="mine", figures_source="image", outcome_code=None)
    row, _ = store.create(held)
    for key, value in held.items():
        assert row[key] == value, key
    assert len(ins().INSTRUCTION_COLUMNS) == 37
    assert set(row) == set(ins().INSTRUCTION_COLUMNS)
    assert row["daily_reserved"] is False and row["batch_ref"] is None and row["approved_at"] is None


def test_create_rejects_accepted_status(store):
    with pytest.raises(ValueError):
        store.create(rec(1, status="accepted"))


def test_create_rejects_status_outside_initial_statuses(store):
    for status in ("submitting", "executed", "failed", "cancelled", "expired", "needs_review", "bogus"):
        with pytest.raises(ValueError):
            store.create(rec(1, status=status))
    assert ins().INITIAL_STATUSES == frozenset({"awaiting_approval", "awaiting_beneficiary", "held", "parked"})


def test_create_accepts_parked_with_outcome_code(store):
    row, created = store.create(rec(1, status="parked", outcome_code="possible_duplicate", expires_at=T0))
    assert created and row["status"] == "parked" and row["outcome_code"] == "possible_duplicate"


def test_create_rejects_invalid_path(store):
    for bad in (None, "", "other", "REGISTERED"):
        with pytest.raises(ValueError):
            store.create(rec(1, path=bad))


def test_create_rejects_invalid_figures_source(store):
    for bad in (None, "", "ocr", "Typed"):
        with pytest.raises(ValueError):
            store.create(rec(1, figures_source=bad))


def test_create_without_notify_to_raises(store):
    for bad in (None, "", "  "):
        with pytest.raises(ValueError):
            store.create(rec(1, notify_to=bad))
    row = rec(1)
    del row["notify_to"]
    with pytest.raises(ValueError):
        store.create(row)


def test_create_rejects_batch_columns_and_unknown_keys(store):
    for key, value in (("batch_ref", "B-1007-aaaa"), ("item_no", 1), ("offered_at", T0), ("offer_digest", "x"),
                       ("approved_at", T0), ("account_number", "1234567890")):
        with pytest.raises(ValueError):
            store.create(rec(1, **{key: value}))


def test_expires_at_per_state_at_create(store):
    awaiting, _ = store.create(rec(1, status="awaiting_beneficiary", path="new_payee", beneficiary_id=None,
                                   beneficiary_fingerprint=None, expires_at=T0 + timedelta(days=7)))
    parked, _ = store.create(rec(2, status="parked", outcome_code="x", expires_at=T0))
    assert awaiting["expires_at"] == T0 + timedelta(days=7) and parked["expires_at"] == T0


def test_instruction_id_independent_of_extraction():
    from invespend.payments import refs
    a = refs.instruction_id_for("<m1@x>", ["Piet <piet@example.com>"])
    assert a == refs.instruction_id_for("<m1@x>", ["piet@example.com"]) and a and len(a) == 64


def test_missing_message_id_rejected():
    from invespend.payments import refs
    assert refs.instruction_id_for("", ["piet@example.com"]) is None


def test_row_has_no_9plus_digit_runs(store):
    row, _ = store.create(rec(1, message_id_hash="abc"))
    for key, value in row.items():
        if isinstance(value, str) and key not in ("instruction_id",):
            assert not re.search(r"\d{9,}", value), key
    assert "account_number" not in row


# ------------------------------------------------------------ message seen
def test_mark_message_seen_second_call_false(store):
    assert store.mark_message_seen("m1", T0) is True
    assert store.mark_message_seen("m1", T0 + H) is False


def test_set_message_outcome_and_auth_from(store):
    store.mark_message_seen("m1", T0)
    store.set_message_outcome("m1", "instruction", auth_from="piet@example.com")
    store.set_message_outcome("m1", "approval_applied")        # auth_from kept
    assert store.sweep_stuck_messages(older_than=timedelta(minutes=30), now=T0 + H) == []


def test_sweep_stuck_messages_returns_only_authenticated_unnotified(store):
    for mid in ("a", "b", "c", "d"):
        store.mark_message_seen(mid, T0)
    store.set_message_outcome("a", "processing", auth_from="piet@example.com")   # authenticated, stuck
    store.set_message_outcome("c", "ignored_no_trigger")
    store.mark_message_seen("fresh", T0 + H)
    rows = store.sweep_stuck_messages(older_than=timedelta(minutes=30), now=T0 + H)
    assert [r["instruction_id"] for r in rows] == ["a"]                          # b, d: never authenticated
    assert rows[0]["auth_from"] == "piet@example.com"
    assert store.mark_resend_notified("a", T0 + H) is True
    assert store.mark_resend_notified("a", T0 + 2 * H) is False
    assert store.sweep_stuck_messages(older_than=timedelta(minutes=30), now=T0 + 2 * H) == []


# -------------------------------------------------- beneficiary observation
def test_first_observation_sets_last_fingerprint_only(store):
    obs = store.observe_beneficiary("b1", T0, fingerprint="fpA")
    assert obs.first_seen_at == T0 and obs.last_fingerprint == "fpA" and obs.fingerprint_changed_at is None
    assert obs.established is False


def test_observe_never_resets_first_seen(store):
    store.observe_beneficiary("b1", T0, fingerprint="fpA", established=True)
    obs = store.observe_beneficiary("b1", T0 + H, fingerprint="fpA")
    assert obs.first_seen_at == T0 and obs.established is True


def test_fingerprint_change_sets_changed_at_not_first_seen(store):
    store.observe_beneficiary("b1", T0, fingerprint="fpA")
    obs = store.observe_beneficiary("b1", T0 + H, fingerprint="fpB")
    assert obs.first_seen_at == T0 and obs.last_fingerprint == "fpB" and obs.fingerprint_changed_at == T0 + H


def test_fingerprint_null_to_value_is_baseline_no_change(store):
    store.observe_beneficiary("b1", T0, fingerprint=None)
    obs = store.observe_beneficiary("b1", T0 + H, fingerprint="fpA")
    assert obs.last_fingerprint == "fpA" and obs.fingerprint_changed_at is None
    again = store.observe_beneficiary("b1", T0 + 2 * H, fingerprint=None)       # key removed: nothing changes
    assert again.last_fingerprint == "fpA" and again.fingerprint_changed_at is None


def test_bootstrap_marker_persisted_for_empty_list(store):
    assert store.bootstrap_done() is False
    store.mark_bootstrap_done(T0)
    assert store.bootstrap_done() is True and store.meta_get("beneficiary_bootstrap") is not None


def test_bootstrap_runs_once_then_new_beneficiary_not_established(store):
    store.observe_beneficiary("old", T0, fingerprint="f", established=True)
    store.mark_bootstrap_done(T0)
    new = store.observe_beneficiary("new", T0 + H, fingerprint="g")
    assert new.established is False and store.observe_beneficiary("old", T0 + H, fingerprint="f").established


def test_meta_roundtrip(store):
    assert store.meta_get("k") is None
    store.meta_set("k", "v1", T0)
    store.meta_set("k", "v2", T0 + H)
    assert store.meta_get("k") == "v2"


# ------------------------------------------------------------- find_recent_similar
def test_find_recent_similar_ignores_parked_failed_cancelled_expired_includes_awaiting_approval_executed_and_needs_review(store):
    args = ("acc-1", "acme ltd", Decimal("100.00"))
    since = T0 - H
    store.create(rec(1, status="parked", outcome_code="x"))
    assert store.find_recent_similar(*args, since) is None
    store.create(rec(2))
    assert store.find_recent_similar(*args, since)["instruction_id"] == "id0002"
    assert store.find_recent_similar("acc-2", "acme ltd", Decimal("100.00"), since) is None
    assert store.find_recent_similar(*args[:2], Decimal("100.01"), since) is None
    assert store.find_recent_similar("acc-1", "other", Decimal("100.00"), since) is None
    assert ins().DUPLICATE_GUARD_STATUSES == frozenset({
        "awaiting_approval", "awaiting_beneficiary", "held", "accepted", "submitting", "executed",
        "needs_review", "needs_authorisation"})


def test_find_recent_similar_excludes_terminal_non_guard_statuses(store):
    row = accepted(store, 1)
    store.cas_status(row["instruction_id"], {"accepted"}, "cancelled", now=T0 + H)
    assert store.find_recent_similar("acc-1", "acme ltd", Decimal("100.00"), T0 - H) is None


def test_find_recent_similar_includes_executed_and_needs_review(store):
    row = accepted(store, 1)
    assert store.claim_for_execution(row["instruction_id"], Decimal("100.00"), daily_cap=Decimal("1000"),
                                     execution_mode="dry-run", now=T0 + 2 * H).committed
    assert store.find_recent_similar("acc-1", "acme ltd", Decimal("100.00"), T0 - H)    # submitting counts
    assert store.finalize(row["instruction_id"], "submitting", "needs_review", release=False, now=T0 + 3 * H)
    assert store.find_recent_similar("acc-1", "acme ltd", Decimal("100.00"), T0 - H)


def test_find_recent_similar_uses_received_at_not_updated_at(store):
    store.create(rec(1, received_at=T0 - timedelta(days=10), updated_at=T0))
    assert store.find_recent_similar("acc-1", "acme ltd", Decimal("100.00"), T0 - timedelta(days=7)) is None
    assert store.find_recent_similar("acc-1", "acme ltd", Decimal("100.00"), T0 - timedelta(days=11))


# ------------------------------------------------------------ held / ready
def _awaiting_beneficiary(store, n=1, **over):
    return store.create(rec(n, status="awaiting_beneficiary", path="new_payee", beneficiary_id=None,
                            beneficiary_fingerprint=None, expires_at=T0 + timedelta(days=7), **over))[0]


def test_set_held_sets_once_incl_expires_at(store):
    _awaiting_beneficiary(store)
    kw = dict(beneficiary_id="b9", fingerprint="fp9", first_seen_at=T0 + H, eligible_at=T0 + H,
              expires_at=T0 + 25 * H, now=T0 + H)
    assert store.set_held("id0001", **kw) is True
    row = store.get("id0001")
    assert row["status"] == "held" and row["expires_at"] == T0 + 25 * H and row["eligible_at"] == T0 + H
    assert store.set_held("id0001", **{**kw, "expires_at": T0 + 99 * H}) is False
    assert store.get("id0001")["expires_at"] == T0 + 25 * H


def test_set_held_sets_beneficiary_fields_once(store):
    _awaiting_beneficiary(store)
    store.set_held("id0001", beneficiary_id="b9", fingerprint="fp9", first_seen_at=T0, eligible_at=T0,
                   expires_at=T0 + H, now=T0)
    row = store.get("id0001")
    assert row["beneficiary_id"] == "b9" and row["beneficiary_fingerprint"] == "fp9" and row["first_seen_at"] == T0
    assert store.set_held("id0001", beneficiary_id="other", fingerprint="x", first_seen_at=T0 + H, eligible_at=T0 + H,
                          expires_at=T0 + H, now=T0 + H) is False
    assert store.get("id0001")["beneficiary_id"] == "b9"


def test_set_held_refuses_non_awaiting_beneficiary_or_expired(store):
    store.create(rec(1))
    kw = dict(beneficiary_id="b", fingerprint="f", first_seen_at=T0, eligible_at=T0, expires_at=T0 + H, now=T0)
    assert store.set_held("id0001", **kw) is False
    _awaiting_beneficiary(store, 2)
    assert store.set_held("id0002", **{**kw, "now": T0 + timedelta(days=8)}) is False


def test_hold_zero_makes_eligible_at_equal_first_seen_at(store):
    _awaiting_beneficiary(store)
    store.set_held("id0001", beneficiary_id="b", fingerprint="f", first_seen_at=T0, eligible_at=T0, expires_at=T0 + 24 * H, now=T0)
    row = store.get("id0001")
    assert row["eligible_at"] == row["first_seen_at"]


def test_hold_n_hours_eligible_at_is_first_seen_plus_n(store):
    _awaiting_beneficiary(store)
    store.set_held("id0001", beneficiary_id="b", fingerprint="f", first_seen_at=T0, eligible_at=T0 + 5 * H,
                   expires_at=T0 + 29 * H, now=T0)
    assert store.get("id0001")["eligible_at"] - store.get("id0001")["first_seen_at"] == 5 * H


def test_make_ready_never_before_eligible_at(store):
    _awaiting_beneficiary(store)
    store.set_held("id0001", beneficiary_id="b", fingerprint="f", first_seen_at=T0, eligible_at=T0 + 5 * H,
                   expires_at=T0 + 29 * H, now=T0)
    assert store.make_ready("id0001", now=T0 + 4 * H, approval_window=WINDOW) is False
    assert store.get("id0001")["status"] == "held"
    assert store.make_ready("id0001", now=T0 + 5 * H, approval_window=WINDOW) is True
    row = store.get("id0001")
    assert row["status"] == "awaiting_approval" and row["expires_at"] == T0 + 5 * H + WINDOW and row["batch_ref"] is None


def test_make_ready_requires_held_and_not_expired(store):
    store.create(rec(1))
    assert store.make_ready("id0001", now=T0, approval_window=WINDOW) is False
    store.create(rec(2, status="held", first_seen_at=T0, eligible_at=T0, expires_at=T0 + 2 * H))
    assert store.make_ready("id0002", now=T0 + 3 * H, approval_window=WINDOW) is False
    assert store.make_ready("id0002", now=T0 + H, approval_window=WINDOW) is True


# --------------------------------------------------------------- batches
def test_offer_batch_numbers_items_1_to_n_and_sets_ref_offered_at_digest(store):
    rows = offer(store, 1, 2, 3)
    assert [r["item_no"] for r in rows] == [1, 2, 3]
    assert len({r["batch_ref"] for r in rows}) == 1 and rows[0]["batch_ref"].startswith("B-1007-")
    for r in rows:
        assert r["offered_at"] == T0 and r["expires_at"] == T0 + WINDOW and r["status"] == "awaiting_approval"
        assert r["offer_digest"] == ins().offer_digest(store.get(r["instruction_id"]), r["batch_ref"], r["item_no"])


def test_offer_batch_orders_by_received_at_then_id(store):
    store.create(rec(2, received_at=T0 - H))
    store.create(rec(1, received_at=T0))
    store.create(rec(3, received_at=T0))
    rows = store.offer_batch("piet@example.com", now=T0, approval_window=WINDOW, max_items=50, new_ref=ref_gen())
    assert [r["instruction_id"] for r in rows] == ["id0002", "id0001", "id0003"]


def test_offer_batch_returns_empty_when_nothing_unoffered_no_ref_consumed(store):
    calls = []
    assert store.offer_batch("piet@example.com", now=T0, approval_window=WINDOW, max_items=50,
                             new_ref=lambda: calls.append(1) or "B-1007-aaaa") == []
    assert calls == []


def test_offer_batch_is_per_notify_to(store):
    store.create(rec(1))
    store.create(rec(2, notify_to="other@example.com"))
    a = store.offer_batch("piet@example.com", now=T0, approval_window=WINDOW, max_items=50, new_ref=ref_gen())
    b = store.offer_batch("other@example.com", now=T0, approval_window=WINDOW, max_items=50, new_ref=ref_gen())
    assert [r["instruction_id"] for r in a] == ["id0001"] and [r["instruction_id"] for r in b] == ["id0002"]
    assert a[0]["batch_ref"] != b[0]["batch_ref"] and a[0]["item_no"] == b[0]["item_no"] == 1


def test_offer_batch_respects_max_items_and_leaves_surplus_unoffered(store):
    for n in (1, 2, 3):
        store.create(rec(n))
    rows = store.offer_batch("piet@example.com", now=T0, approval_window=WINDOW, max_items=2, new_ref=ref_gen())
    assert len(rows) == 2 and store.get("id0003")["batch_ref"] is None
    nxt = store.offer_batch("piet@example.com", now=T0 + H, approval_window=WINDOW, max_items=2, new_ref=ref_gen())
    assert [r["instruction_id"] for r in nxt] == ["id0003"] and nxt[0]["item_no"] == 1


def test_offer_batch_ignores_expired_and_already_offered_rows(store):
    store.create(rec(1, expires_at=T0 - H))
    store.create(rec(2, status="held", first_seen_at=T0, eligible_at=T0, expires_at=T0 + H))
    first = offer(store, 3)
    assert [r["instruction_id"] for r in first] == ["id0003"]
    assert store.offer_batch("piet@example.com", now=T0, approval_window=WINDOW, max_items=50, new_ref=ref_gen()) == []


def test_item_numbers_unique_and_stable_across_reruns(store):
    rows = offer(store, 1, 2)
    again = store.offer_batch("piet@example.com", now=T0 + H, approval_window=WINDOW, max_items=50, new_ref=ref_gen())
    assert again == []
    assert [(store.get(r["instruction_id"])["batch_ref"], store.get(r["instruction_id"])["item_no"]) for r in rows] == \
        [(r["batch_ref"], r["item_no"]) for r in rows]


def test_ref_clash_retries_with_new_ref(store):
    first = offer(store, 1)
    refs = iter([first[0]["batch_ref"], first[0]["batch_ref"], "B-1007-ffff"])
    store.create(rec(2))
    rows = store.offer_batch("piet@example.com", now=T0 + H, approval_window=WINDOW, max_items=50, new_ref=lambda: next(refs))
    assert rows[0]["batch_ref"] == "B-1007-ffff"


def test_unoffer_batch_only_before_notified(store):
    rows = offer(store, 1, 2)
    ref = rows[0]["batch_ref"]
    assert store.unoffer_batch(ref, now=T0) == 2
    row = store.get("id0001")
    assert row["batch_ref"] is None and row["item_no"] is None and row["offered_at"] is None and row["offer_digest"] is None
    rows = store.offer_batch("piet@example.com", now=T0, approval_window=WINDOW, max_items=50, new_ref=ref_gen())
    assert store.mark_batch_notified(rows[0]["batch_ref"], T0) == 2
    assert store.unoffer_batch(rows[0]["batch_ref"], now=T0) == 0 and store.get("id0001")["batch_ref"]


def test_mark_batch_notified_set_once(store):
    rows = offer(store, 1, 2)
    ref = rows[0]["batch_ref"]
    assert store.mark_batch_notified(ref, T0 + H) == 2
    assert store.mark_batch_notified(ref, T0 + 2 * H) == 0
    assert store.get("id0001")["batch_notified_at"] == T0 + H


def test_list_batch_per_approver_ordered_any_status(store):
    rows = offer(store, 2, 1)
    ref = rows[0]["batch_ref"]
    approve(store, rows[0])
    listed = store.list_batch(ref, "piet@example.com")
    assert [r["item_no"] for r in listed] == [1, 2] and {r["status"] for r in listed} == {"accepted", "awaiting_approval"}
    assert store.list_batch(ref, "other@example.com") == []
    assert store.list_batch("B-1007-zzzz", "piet@example.com") == []


def test_list_pending_offered_excludes_expired_cancelled_approved_and_the_new_batch(store):
    old = offer(store, 1, 2, 3)
    store.create(rec(4, expires_at=T0 + H))
    store.create(rec(5))
    old4 = store.offer_batch("piet@example.com", now=T0, approval_window=WINDOW, max_items=50, new_ref=ref_gen())
    approve(store, old[0])
    store.cas_status("id0002", {"awaiting_approval"}, "cancelled", now=T0)
    ids = {r["instruction_id"] for r in store.list_pending_offered("piet@example.com", now=T0 + 2 * H)}
    assert "id0001" not in ids and "id0002" not in ids and "id0003" in ids
    excl = store.list_pending_offered("piet@example.com", now=T0 + 2 * H, exclude_batch_ref=old4[0]["batch_ref"])
    assert all(r["batch_ref"] != old4[0]["batch_ref"] for r in excl)
    assert store.list_pending_offered("other@example.com", now=T0) == []


def test_list_unnotified_batches_threshold(store):
    rows = offer(store, 1)
    ref = rows[0]["batch_ref"]
    m30 = timedelta(minutes=30)
    assert store.list_unnotified_batches(older_than=m30, now=T0 + timedelta(minutes=10)) == []
    assert store.list_unnotified_batches(older_than=m30, now=T0 + timedelta(minutes=31)) == [ref]
    store.mark_batch_notified(ref, T0)
    assert store.list_unnotified_batches(older_than=m30, now=T0 + timedelta(minutes=31)) == []


# ------------------------------------------------------------- approval
def test_approve_item_sets_approved_at_and_grace_expiry_once(store):
    row = offer(store, 1)[0]
    res = approve(store, row, now=T0 + H)
    assert res.approved is True and res.reason == "ok"
    got = store.get("id0001")
    assert got["status"] == "accepted" and got["approved_at"] == T0 + H and got["expires_at"] == T0 + H + GRACE
    again = approve(store, row, now=T0 + 2 * H)
    assert again.approved is False and again.reason == "not_awaiting"
    assert store.get("id0001")["approved_at"] == T0 + H


def test_approve_item_refuses_expired_unoffered_or_wrong_batch_or_wrong_notify_to(store):
    row = offer(store, 1)[0]
    base = dict(batch_ref=row["batch_ref"], item_no=row["item_no"], notify_to=row["notify_to"], now=T0 + H, grace=GRACE)
    for override, reason in (({"batch_ref": "B-1007-zzzz"}, "wrong_batch"), ({"item_no": 9}, "wrong_batch"),
                             ({"notify_to": "other@example.com"}, "wrong_batch"),
                             ({"now": T0 + 25 * H}, "expired")):
        res = store.approve_item("id0001", **{**base, **override})
        assert (res.approved, res.reason) == (False, reason), override
    store.create(rec(2))                                  # unoffered
    res = store.approve_item("id0002", **{**base, "batch_ref": row["batch_ref"], "item_no": 2})
    assert (res.approved, res.reason) == (False, "wrong_batch")
    res = store.approve_item("missing", **base)
    assert (res.approved, res.reason) == (False, "wrong_batch")
    assert store.get("id0001")["status"] == "awaiting_approval"


def test_approve_item_twice_second_is_not_awaiting_no_change(store):
    row = offer(store, 1)[0]
    assert approve(store, row).approved
    snapshot = store.get("id0001")
    assert approve(store, row, now=T0 + 3 * H).reason == "not_awaiting"
    assert store.get("id0001") == snapshot


def test_approve_item_reason_enumeration(store):
    row = offer(store, 1, 2, 3)
    store.cas_status("id0003", {"awaiting_approval"}, "cancelled", now=T0)
    seen = {approve(store, row[0]).reason, approve(store, row[0]).reason, approve(store, row[2]).reason,
            store.approve_item("id0002", batch_ref="B-1007-zzzz", item_no=2, notify_to="piet@example.com",
                               now=T0, grace=GRACE).reason,
            store.approve_item("id0002", batch_ref=row[1]["batch_ref"], item_no=2, notify_to="piet@example.com",
                               now=T0 + 30 * H, grace=GRACE).reason}
    assert seen == {"ok", "not_awaiting", "wrong_batch", "expired"}


def test_cas_status_refuses_new_accepted_submitting_and_held_to_awaiting_approval(store):
    store.create(rec(1))
    store.create(rec(2, status="held", first_seen_at=T0, eligible_at=T0, expires_at=T0 + H))
    with pytest.raises(ValueError):
        store.cas_status("id0001", {"awaiting_approval"}, "accepted", now=T0)
    with pytest.raises(ValueError):
        store.cas_status("id0001", {"awaiting_approval"}, "submitting", now=T0)
    with pytest.raises(ValueError):
        store.cas_status("id0002", {"held"}, "awaiting_approval", now=T0)
    with pytest.raises(ValueError):
        store.cas_status("id0001", {"submitting"}, "executed", now=T0)
    with pytest.raises(ValueError):
        store.cas_status("id0001", {"awaiting_approval"}, "executed", now=T0)       # not a TRANSITIONS edge
    with pytest.raises(ValueError):
        store.cas_status("id0001", set(), "cancelled", now=T0)


def test_cas_status_refuses_awaiting_beneficiary_to_held_set_held_performs_it(store):
    _awaiting_beneficiary(store)
    with pytest.raises(ValueError):
        store.cas_status("id0001", {"awaiting_beneficiary"}, "held", now=T0)
    assert store.set_held("id0001", beneficiary_id="b", fingerprint="f", first_seen_at=T0, eligible_at=T0,
                          expires_at=T0 + H, now=T0) is True
    assert store.get("id0001")["status"] == "held"


def test_outcome_code_written_by_each_owner(store):
    store.create(rec(1, status="parked", outcome_code="possible_duplicate"))
    assert store.get("id0001")["outcome_code"] == "possible_duplicate"
    store.create(rec(2))
    assert store.cas_status("id0002", {"awaiting_approval"}, "parked", now=T0, outcome_code="over_cap")
    assert store.get("id0002")["outcome_code"] == "over_cap" and store.get("id0002")["updated_at"] == T0
    row = accepted(store, 3)
    store.claim_for_execution(row["instruction_id"], Decimal("100.00"), daily_cap=Decimal("1000"),
                              execution_mode="dry-run", now=T0 + 2 * H)
    assert store.finalize(row["instruction_id"], "submitting", "executed", release=False, now=T0 + 3 * H, outcome_code="dry-run")
    assert store.get(row["instruction_id"])["outcome_code"] == "dry-run"
    row = accepted(store, 4)
    store.claim_for_execution(row["instruction_id"], Decimal("100.00"), daily_cap=Decimal("1000"),
                              execution_mode="dry-run", now=T0 + 2 * H)
    assert store.recover_stale_submitting(older_than=timedelta(minutes=30), now=T0 + 4 * H) == ["id0004"]
    assert store.get("id0004")["outcome_code"] == "stale_submitting"


def test_create_cannot_produce_accepted(store):
    with pytest.raises(ValueError):
        store.create(rec(1, status="accepted", approved_at=T0))


# ---------------------------------------------------------- claim / finalize
CAP = Decimal("50000")


def _claim(store, iid="id0001", *, amount="100.00", cap=CAP, mode="dry-run", now=T0 + 2 * H):
    return store.claim_for_execution(iid, Decimal(amount), daily_cap=cap, execution_mode=mode, now=now)


def test_claim_requires_accepted_with_approved_at_and_unexpired(store):
    store.create(rec(1))
    assert _claim(store).reason == "cas_lost"                                   # awaiting_approval is never claimable
    row = next(r for r in offer(store, 2) if r["instruction_id"] == "id0002")
    assert _claim(store, "id0002").reason == "cas_lost"
    approve(store, row)
    res = _claim(store, "id0002", now=T0 + H + GRACE + H)
    assert res.committed is False and res.reason == "stale"
    assert store.get("id0002")["status"] == "accepted" and store.get("id0002")["daily_reserved"] is False


def test_claim_rejects_expired_accepted_row_atomically(store):
    accepted(store, 1)
    res = _claim(store, now=T0 + H + GRACE)                                      # expires_at <= now
    assert (res.committed, res.reason) == (False, "stale")
    assert store.get("id0001")["status"] == "accepted"
    # nothing reserved on any day
    assert _claim_total(store, T0 + H + GRACE) == Decimal("0")


def _claim_total(store, now):
    # probe the total through a claim of a throwaway row would change state; use the store-specific reader
    return store.daily_total(now)


def test_claim_sets_reserved_day(store):
    accepted(store, 1)
    res = _claim(store)
    assert res.committed and res.reason == "ok" and res.daily_total == Decimal("100.00")
    row = store.get("id0001")
    assert row["status"] == "submitting" and row["daily_reserved"] is True
    assert str(row["reserved_day"]) == "2026-10-07" and row["updated_at"] == T0 + 2 * H


def test_claim_sets_execution_mode(store):
    accepted(store, 1)
    _claim(store, mode="live")
    assert store.get("id0001")["execution_mode"] == "live"


def test_claim_reason_enumeration(store):
    accepted(store, 1)
    accepted(store, 2)
    store.create(rec(3))
    reasons = {
        _claim(store, "id0001").reason,
        _claim(store, "id0001").reason,                                          # now submitting -> cas_lost
        _claim(store, "id0002", cap=Decimal("50")).reason,                       # daily cap
        _claim(store, "id0002", now=T0 + H + GRACE).reason,                      # stale
    }
    assert reasons == {"ok", "cas_lost", "daily_cap", "stale"}
    assert set(ins().CLAIM_REASONS) == {"ok", "daily_cap", "stale", "cas_lost"}


def test_claim_blocks_on_cap_and_leaves_status_unchanged(store):
    accepted(store, 1, amount=Decimal("30000.00"))
    accepted(store, 2, amount=Decimal("30000.00"))
    assert _claim(store, "id0001", amount="30000.00").committed
    res = _claim(store, "id0002", amount="30000.00")
    assert (res.committed, res.reason) == (False, "daily_cap") and res.daily_total == Decimal("30000.00")
    assert store.get("id0002")["status"] == "accepted" and store.get("id0002")["daily_reserved"] is False


def test_claim_cap_fail_closed_when_unset(store):
    accepted(store, 1)
    assert _claim(store, cap=Decimal("0")).reason == "daily_cap"


def test_claim_amount_must_match_stored_row(store):
    accepted(store, 1)
    with pytest.raises(ValueError):
        _claim(store, amount="1.00")


def test_claim_two_threads_exactly_one_wins(store):
    accepted(store, 1)
    results = []
    barrier = threading.Barrier(2)

    def run():
        barrier.wait()
        results.append(_claim(store).reason)

    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["cas_lost", "ok"]


def test_cancel_vs_claim_cas_exactly_one_winner(store):
    accepted(store, 1)
    out = {}
    barrier = threading.Barrier(2)

    def cancel():
        barrier.wait()
        out["cancel"] = store.cas_status("id0001", {"awaiting_approval", "accepted"}, "cancelled", now=T0 + 2 * H)

    def claim():
        barrier.wait()
        out["claim"] = _claim(store).committed

    threads = [threading.Thread(target=cancel), threading.Thread(target=claim)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert out["cancel"] != out["claim"]
    assert store.get("id0001")["status"] == ("cancelled" if out["cancel"] else "submitting")


def test_approve_vs_cancel_cas_exactly_one_winner(store):
    row = offer(store, 1)[0]
    out = {}
    barrier = threading.Barrier(2)

    def a():
        barrier.wait()
        out["approve"] = approve(store, row).approved

    def c():
        barrier.wait()
        out["cancel"] = store.cas_status("id0001", {"awaiting_approval", "accepted"}, "cancelled", now=T0 + H)

    threads = [threading.Thread(target=a), threading.Thread(target=c)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    # approve then cancel: both legitimately win in sequence; cancel then approve: approve loses
    status = store.get("id0001")["status"]
    assert out["cancel"] is True and status == "cancelled"
    if not out["approve"]:
        assert store.get("id0001")["approved_at"] is None


def test_finalize_release_idempotent_and_never_negative(store):
    accepted(store, 1)
    _claim(store)
    assert store.finalize("id0001", "submitting", "failed", release=True, now=T0 + 3 * H,
                          outcome_code="rejected", outcome_message="insufficient funds")
    assert store.daily_total(T0 + 3 * H) == Decimal("0")
    assert store.finalize("id0001", "submitting", "failed", release=True, now=T0 + 4 * H) is False
    assert store.daily_total(T0 + 3 * H) == Decimal("0")
    assert store.get("id0001")["daily_reserved"] is False


def test_finalize_keeps_reservation_on_executed_and_needs_review(store):
    accepted(store, 1)
    accepted(store, 2)
    _claim(store, "id0001")
    _claim(store, "id0002")
    assert store.finalize("id0001", "submitting", "executed", release=False, now=T0 + 3 * H)
    assert store.finalize("id0002", "submitting", "needs_review", release=False, now=T0 + 3 * H)
    assert store.daily_total(T0 + 3 * H) == Decimal("200.00")
    assert store.get("id0001")["daily_reserved"] is True


def test_finalize_rejects_release_flag_mismatch(store):
    accepted(store, 1)
    _claim(store)
    for new, wrong in (("executed", True), ("needs_review", True), ("failed", False), ("needs_authorisation", False),
                       ("parked", False)):
        with pytest.raises(ValueError):
            store.finalize("id0001", "submitting", new, release=wrong, now=T0 + 3 * H)
    with pytest.raises(ValueError):
        store.finalize("id0001", "accepted", "failed", release=True, now=T0 + 3 * H)
    with pytest.raises(ValueError):
        store.finalize("id0001", "submitting", "cancelled", release=False, now=T0 + 3 * H)
    assert store.get("id0001")["status"] == "submitting"
    assert dict(ins().FINALIZE_RELEASES) == {"executed": False, "needs_review": False, "failed": True,
                                             "needs_authorisation": True, "parked": True}


def test_finalize_uses_stored_reserved_day_not_now(store):
    SAST = timezone(timedelta(hours=2))
    t_claim = datetime(2026, 10, 7, 23, 59, tzinfo=SAST)
    t_final = datetime(2026, 10, 8, 0, 1, tzinfo=SAST)
    row = offer(store, 1, now=t_claim - H)[0]
    approve(store, row, now=t_claim - timedelta(minutes=30))
    assert store.claim_for_execution("id0001", Decimal("100.00"), daily_cap=CAP, execution_mode="dry-run", now=t_claim).committed
    accepted(store, 2)    # a second row so the new day also has a reservation to compare
    assert store.finalize("id0001", "submitting", "failed", release=True, now=t_final)
    assert store.daily_total(t_claim) == Decimal("0")                      # previous day restored
    assert store.daily_total(t_final) == Decimal("0")                      # new day untouched


def test_finalize_sets_executed_at_only_for_executed(store):
    accepted(store, 1)
    accepted(store, 2)
    _claim(store, "id0001")
    _claim(store, "id0002")
    store.finalize("id0001", "submitting", "executed", release=False, now=T0 + 3 * H)
    store.finalize("id0002", "submitting", "needs_review", release=False, now=T0 + 3 * H)
    assert store.get("id0001")["executed_at"] == T0 + 3 * H and store.get("id0002")["executed_at"] is None


def test_finalize_failed_stores_sanitised_provider_message(store):
    accepted(store, 1)
    _claim(store)
    store.finalize("id0001", "submitting", "failed", release=True, now=T0 + 3 * H, outcome_code="rejected",
                   outcome_message="Beneficiary cooling off for acct 123456789012 mail a@b.co " + "x" * 400)
    msg = store.get("id0001")["outcome_message"]
    assert len(msg) <= 200 and not re.search(r"\d{6,}", msg) and "@" not in msg


def test_outcome_message_has_no_9plus_digit_runs(store):
    accepted(store, 1)
    _claim(store)
    store.finalize("id0001", "submitting", "needs_authorisation", release=True, now=T0 + 3 * H,
                   outcome_message="ref 1234567890123 pending")
    assert not re.search(r"\d{9,}", store.get("id0001")["outcome_message"] or "")


def test_finalize_message_ignored_for_other_statuses(store):
    accepted(store, 1)
    _claim(store)
    store.finalize("id0001", "submitting", "executed", release=False, now=T0 + 3 * H, outcome_message="should not be kept")
    assert store.get("id0001")["outcome_message"] is None


def test_recover_stale_submitting_respects_threshold(store):
    accepted(store, 1)
    accepted(store, 2)
    _claim(store, "id0001", now=T0 + 2 * H)
    _claim(store, "id0002", now=T0 + 2 * H + timedelta(minutes=25))
    got = store.recover_stale_submitting(older_than=timedelta(minutes=30), now=T0 + 2 * H + timedelta(minutes=35))
    assert got == ["id0001"]
    assert store.get("id0001")["status"] == "needs_review" and store.get("id0001")["daily_reserved"] is True
    assert store.get("id0002")["status"] == "submitting"


# ----------------------------------------------------------- notices, cleanup
def test_mark_notified_set_once(store):
    _awaiting_beneficiary(store)
    assert store.mark_notified("id0001", "paste", T0 + H) is True
    assert store.mark_notified("id0001", "paste", T0 + 2 * H) is False
    assert store.get("id0001")["paste_notified_at"] == T0 + H
    assert store.mark_notified("id0001", "hold", T0 + 3 * H) is True
    with pytest.raises(ValueError):
        store.mark_notified("id0001", "batch", T0)


def test_list_active_and_list_by_status(store):
    store.create(rec(1))
    store.create(rec(2, status="parked", outcome_code="x"))
    row = accepted(store, 3)
    assert {r["instruction_id"] for r in store.list_active()} == {"id0001", "id0003"}
    assert [r["instruction_id"] for r in store.list_by_status({"accepted"})] == ["id0003"]
    assert {r["instruction_id"] for r in store.list_by_status(["parked", "awaiting_approval"])} == {"id0001", "id0002"}
    assert row["status"] == "accepted"


def test_cleanup_removes_terminal_older_than_window_and_keeps_active(store):
    store.create(rec(1, status="parked", outcome_code="x", updated_at=T0 - timedelta(days=100), expires_at=T0))
    store.create(rec(2, updated_at=T0 - timedelta(days=100)))
    store.create(rec(3, status="parked", outcome_code="x", updated_at=T0 - timedelta(days=1), expires_at=T0))
    store.mark_message_seen("old", T0 - timedelta(days=100))
    store.mark_message_seen("new", T0)
    assert store.cleanup(90, T0) == 1
    assert store.get("id0001") is None and store.get("id0002") and store.get("id0003")
    assert store.mark_message_seen("old", T0) is True         # row gone
    assert store.mark_message_seen("new", T0) is False


def test_cleanup_never_touches_beneficiary_seen_or_meta(store):
    store.observe_beneficiary("b1", T0 - timedelta(days=500), fingerprint="f")
    store.mark_bootstrap_done(T0 - timedelta(days=500))
    store.cleanup(1, T0)
    assert store.bootstrap_done() is True
    assert store.observe_beneficiary("b1", T0, fingerprint="f").first_seen_at == T0 - timedelta(days=500)


def test_memory_store_not_durable():
    assert ins().MemoryInstructionStore.durable is False
    assert "TESTS ONLY" in (ins().MemoryInstructionStore.__doc__ or "")


# ----------------------------------------------------------- post-approval CHECK
def test_post_approval_statuses_require_approved_at_and_batch_ref_in_both_stores(store):
    row = accepted(store, 1)
    assert row["approved_at"] and row["batch_ref"]
    store.create(rec(2))
    if hasattr(store, "_validate_row"):
        bad = dict(store.get("id0002"), status="executed")
        with pytest.raises(ValueError):
            store._validate_row(bad)
        bad = dict(row, approved_at=None)
        with pytest.raises(ValueError):
            store._validate_row(bad)
        bad = dict(row, batch_ref=None, item_no=None, offered_at=None, offer_digest=None)
        with pytest.raises(ValueError):
            store._validate_row(bad)
        with pytest.raises(ValueError):
            store._validate_row(dict(row, item_no=None))              # all-or-none
    else:                                                              # Pg: the table CHECK itself
        import psycopg
        from invespend import db
        from tests.instr_helpers import PG_URL
        with pytest.raises(psycopg.errors.CheckViolation):
            with db.connect(PG_URL) as conn, conn.cursor() as cur:
                cur.execute("update payment_instruction set status = 'executed' where instruction_id = 'id0002'")
        with pytest.raises(psycopg.errors.CheckViolation):
            with db.connect(PG_URL) as conn, conn.cursor() as cur:
                cur.execute("update payment_instruction set item_no = null where instruction_id = 'id0001'")


# ------------------------------------------------------------------ digest
DIGEST_ROW = {"amount": Decimal("100.00"), "currency": "ZAR", "source_account_id": "acc-1",
              "payee_name_norm": "acme ltd", "beneficiary_id": "ben-1", "beneficiary_fingerprint": "fp-1"}


def test_offer_digest_canonical_vector():
    import hashlib
    expected = hashlib.sha256("\x1f".join(["B-1007-aaaa", "2", "100.00", "ZAR", "acc-1", "acme ltd", "ben-1", "fp-1"]).encode()).hexdigest()
    assert ins().offer_digest(DIGEST_ROW, "B-1007-aaaa", 2) == expected
    assert ins().offer_digest(DIGEST_ROW, "B-1007-aaaa", 2) == PINNED_DIGEST


PINNED_DIGEST = "3603ea5f3e9db7537f1aefedba00063b638e588a12fa2a9e24e6509b1b7a9b4c"


def test_offer_digest_amount_quantised_and_none_as_empty():
    base = ins().offer_digest(DIGEST_ROW, "B-1007-aaaa", 1)
    for amount in (Decimal("100"), Decimal("100.0"), "100", 100, Decimal("100.001").quantize(Decimal("0.01"))):
        assert ins().offer_digest({**DIGEST_ROW, "amount": amount}, "B-1007-aaaa", 1) == base
    none = ins().offer_digest({**DIGEST_ROW, "beneficiary_id": None, "beneficiary_fingerprint": None}, "B-1007-aaaa", 1)
    empty = ins().offer_digest({**DIGEST_ROW, "beneficiary_id": "", "beneficiary_fingerprint": ""}, "B-1007-aaaa", 1)
    assert none == empty != base


def test_offer_digest_changes_when_any_field_changes():
    base = ins().offer_digest(DIGEST_ROW, "B-1007-aaaa", 1)
    variants = [("B-1007-bbbb", 1, {}), ("B-1007-aaaa", 2, {})] + [
        ("B-1007-aaaa", 1, {k: v}) for k, v in (
            ("amount", Decimal("100.01")), ("currency", "USD"), ("source_account_id", "acc-2"),
            ("payee_name_norm", "acme"), ("beneficiary_id", "ben-2"), ("beneficiary_fingerprint", "fp-2"))]
    digests = {ins().offer_digest({**DIGEST_ROW, **extra}, ref, no) for ref, no, extra in variants}
    assert base not in digests and len(digests) == len(variants)


def test_offer_digest_memory_and_pg_stores_agree(store):
    store.create(rec(1, amount=Decimal("100")))
    rows = store.offer_batch("piet@example.com", now=T0, approval_window=WINDOW, max_items=5, new_ref=ref_gen())
    read_back = store.get("id0001")
    assert rows[0]["offer_digest"] == ins().offer_digest(read_back, rows[0]["batch_ref"], rows[0]["item_no"])
    assert rows[0]["offer_digest"] == ins().offer_digest({**DIGEST_ROW, "amount": Decimal("100")}, rows[0]["batch_ref"], 1)


def test_pending_approval_alias_equals_awaiting_approval():
    assert ins().PENDING_APPROVAL == "awaiting_approval"
    assert ins().POST_APPROVAL_STATUSES == frozenset(
        {"accepted", "submitting", "executed", "failed", "needs_review", "needs_authorisation"})
