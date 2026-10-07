"""S9 state table (plan 2a): (a)(b)(c)(g)(h)(j)(k)(o)(p) at store level. (d)(e)(f)(i)(l)(m)(n) need the cycle and are
in tests/test_payment_state_table_cycle.py (S11)."""
from __future__ import annotations

import itertools
import re
from datetime import timedelta
from decimal import Decimal

import pytest

from invespend import db
from tests.instr_helpers import GRACE, H, STORE_PARAMS, T0, WINDOW, approve, ins, make_store, offer, rec, ref_gen


STATUSES = ["awaiting_approval", "awaiting_beneficiary", "held", "accepted", "submitting", "executed", "failed",
            "needs_review", "needs_authorisation", "cancelled", "expired", "parked"]
TERMINAL = {"executed", "failed", "needs_review", "needs_authorisation", "cancelled", "expired", "parked"}
CAP = Decimal("50000")


@pytest.fixture(params=STORE_PARAMS)
def store(request):
    return make_store(request.param)


_n = itertools.count(100)


def reach(store, status: str) -> str:
    """Drive a fresh row into ``status`` using only legitimate methods; returns its id."""
    n = next(_n)
    iid = f"id{n:04d}"
    notify = f"u{n}@example.com"                      # own approver so batches never mix
    if status == "awaiting_beneficiary":
        store.create(rec(n, notify_to=notify, status="awaiting_beneficiary", path="new_payee", beneficiary_id=None,
                         beneficiary_fingerprint=None, expires_at=T0 + timedelta(days=7)))
        return iid
    if status == "held":
        store.create(rec(n, notify_to=notify, status="held", first_seen_at=T0, eligible_at=T0 + H, expires_at=T0 + 25 * H))
        return iid
    if status == "parked":
        store.create(rec(n, notify_to=notify))
        assert store.cas_status(iid, {"awaiting_approval"}, "parked", now=T0, outcome_code="x")
        return iid
    store.create(rec(n, notify_to=notify))
    if status == "awaiting_approval":
        offer_rows = store.offer_batch(notify, now=T0, approval_window=WINDOW, max_items=5, new_ref=ref_gen())
        assert offer_rows
        return iid
    if status in ("cancelled", "expired"):
        assert store.cas_status(iid, {"awaiting_approval"}, status, now=T0)
        return iid
    row = store.offer_batch(notify, now=T0, approval_window=WINDOW, max_items=5, new_ref=ref_gen())[0]
    assert store.approve_item(iid, batch_ref=row["batch_ref"], item_no=row["item_no"], notify_to=notify, now=T0 + H, grace=GRACE).approved
    if status == "accepted":
        return iid
    assert store.claim_for_execution(iid, Decimal("100.00"), daily_cap=CAP, execution_mode="dry-run", now=T0 + 2 * H).committed
    if status == "submitting":
        return iid
    assert store.finalize(iid, "submitting", status, release=ins().FINALIZE_RELEASES[status], now=T0 + 3 * H)
    return iid


def attempt(store, iid: str, to: str) -> None:
    """Try the edge ->to through the API that owns it."""
    row = store.get(iid)
    if to == "accepted":
        store.approve_item(iid, batch_ref=row["batch_ref"] or "B-0000-0000", item_no=row["item_no"] or 1,
                           notify_to=row["notify_to"], now=T0 + H, grace=GRACE)
    elif to == "submitting":
        store.claim_for_execution(iid, Decimal("100.00"), daily_cap=CAP, execution_mode="dry-run", now=T0 + 2 * H)
    elif to in ("executed", "failed", "needs_review", "needs_authorisation") or (to == "parked" and row["status"] == "submitting"):
        store.finalize(iid, row["status"], to, release=ins().FINALIZE_RELEASES[to], now=T0 + 3 * H)
    elif to == "awaiting_approval" and row["status"] == "held":
        store.make_ready(iid, now=T0 + H, approval_window=WINDOW)
    elif to == "held" and row["status"] == "awaiting_beneficiary":
        store.set_held(iid, beneficiary_id="b", fingerprint="f", first_seen_at=T0, eligible_at=T0, expires_at=T0 + 25 * H, now=T0)
    else:
        store.cas_status(iid, {row["status"]}, to, now=T0 + H)


def invariant(store):
    for row in store.list_by_status(STATUSES):
        if row["status"] in ins().POST_APPROVAL_STATUSES:
            assert row["approved_at"] is not None and row["batch_ref"] is not None, row


def test_transitions_keys_equal_the_sql_status_list():                                     # (a)
    sql = " ".join(ln.split("--", 1)[0] for ln in db.MIGRATIONS_DIR.joinpath("0013_payment_instructions.sql").read_text().splitlines()).lower()
    m = re.search(r"status\s+text not null check \(status in \((.*?)\)\)", sql, re.S)
    assert set(re.findall(r"'([a-z_]+)'", m.group(1))) == set(ins().TRANSITIONS) == set(STATUSES)


def test_every_nonterminal_has_an_edge_and_every_terminal_none():                         # (b) (g)
    t = ins().TRANSITIONS
    for status in STATUSES:
        assert isinstance(t[status], frozenset)
        assert bool(t[status]) == (status not in TERMINAL), status
    assert t["parked"] == frozenset()
    assert t["awaiting_approval"] == {"accepted", "cancelled", "expired", "parked"}
    assert t["awaiting_beneficiary"] == {"held", "expired", "parked"}
    assert t["held"] == {"awaiting_approval", "expired", "parked"}
    assert t["accepted"] == {"submitting", "cancelled", "expired", "parked"}
    assert t["submitting"] == {"executed", "failed", "needs_review", "needs_authorisation", "parked"}
    assert all(dest in STATUSES for dests in t.values() for dest in dests)


def test_property_walk_all_from_to_pairs_allowed_iff_in_table(store):                      # (c) (p)
    t = ins().TRANSITIONS
    for frm, to in itertools.product(STATUSES, STATUSES):
        iid = reach(store, frm)
        assert store.get(iid)["status"] == frm
        invariant(store)
        before = store.get(iid)
        try:
            attempt(store, iid, to)
        except ValueError:
            pass
        invariant(store)
        after = store.get(iid)["status"]
        if to in t[frm]:
            assert after == to, (frm, to, after)
        else:
            assert after == frm, (frm, to, after)
            assert store.get(iid)["approved_at"] == before["approved_at"]


def test_accepted_reachable_only_through_approve_item(store):                              # (h)
    with pytest.raises(ValueError):
        store.create(rec(1, status="accepted"))
    iid = reach(store, "awaiting_approval")
    with pytest.raises(ValueError):
        store.cas_status(iid, {"awaiting_approval"}, "accepted", now=T0)
    assert store.get(iid)["status"] == "awaiting_approval"
    with pytest.raises(ValueError):
        store.cas_status(reach(store, "awaiting_beneficiary"), {"awaiting_beneficiary"}, "held", now=T0)
    assert ("parked" in ins().TRANSITIONS["submitting"])


def test_finalize_releases_on_failed_needs_authorisation_parked_keeps_on_executed_needs_review(store):   # (j)
    for status, released in (("executed", False), ("needs_review", False), ("failed", True),
                             ("needs_authorisation", True), ("parked", True)):
        s = store
        iid = reach(s, "submitting")
        before = s.daily_total(T0 + 2 * H)
        assert s.finalize(iid, "submitting", status, release=released, now=T0 + 3 * H)
        assert s.daily_total(T0 + 3 * H) == (before if not released else before - Decimal("100.00"))
        with pytest.raises(ValueError):
            s.finalize(iid, "submitting", status, release=not released, now=T0 + 3 * H)


def test_release_after_midnight_restores_reserved_day(store):                              # (k)
    from datetime import datetime, timezone
    sast = timezone(timedelta(hours=2))
    claim_at, final_at = datetime(2026, 10, 7, 23, 59, tzinfo=sast), datetime(2026, 10, 8, 0, 1, tzinfo=sast)
    store.create(rec(1, notify_to="m@example.com"))
    row = store.offer_batch("m@example.com", now=claim_at - H, approval_window=WINDOW, max_items=5, new_ref=ref_gen())[0]
    assert store.approve_item("id0001", batch_ref=row["batch_ref"], item_no=1, notify_to="m@example.com",
                              now=claim_at - timedelta(minutes=30), grace=GRACE).approved
    assert store.claim_for_execution("id0001", Decimal("100.00"), daily_cap=CAP, execution_mode="dry-run", now=claim_at).committed
    assert store.daily_total(claim_at) == Decimal("100.00") and store.daily_total(final_at) == Decimal("0")
    assert store.finalize("id0001", "submitting", "failed", release=True, now=final_at)
    assert store.daily_total(claim_at) == Decimal("0") and store.daily_total(final_at) == Decimal("0")
    assert store.finalize("id0001", "submitting", "failed", release=True, now=final_at) is False


def test_make_ready_never_before_eligible_at(store):                                       # (o)
    iid = reach(store, "held")
    assert store.make_ready(iid, now=T0, approval_window=WINDOW) is False
    assert store.get(iid)["status"] == "held"
    assert store.make_ready(iid, now=T0 + H, approval_window=WINDOW) is True


def test_make_ready_requires_held_and_not_expired(store):                                  # (o)
    iid = reach(store, "held")
    assert store.make_ready(iid, now=T0 + 26 * H, approval_window=WINDOW) is False
    assert store.make_ready(reach(store, "awaiting_approval"), now=T0 + H, approval_window=WINDOW) is False
