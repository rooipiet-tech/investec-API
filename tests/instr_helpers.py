"""Shared helpers for the v2 instruction-store tests (S9/S11). No monkeypatching of existing code.

``store`` fixtures run against MemoryInstructionStore always and PgInstructionStore only when
``INVESPEND_TEST_DATABASE_URL`` is set (a scratch database; its payment_* tables are truncated).
"""
from __future__ import annotations

import importlib
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

T0 = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)
PG_URL = os.environ.get("INVESPEND_TEST_DATABASE_URL", "")
STORE_PARAMS = ["memory"] + (["pg"] if PG_URL else [])
WINDOW = timedelta(hours=24)
GRACE = timedelta(hours=24)


def ins():
    return importlib.import_module("invespend.payments.instructions")


def make_store(kind: str):
    mod = ins()
    if kind == "memory":
        return mod.MemoryInstructionStore()
    from invespend import db
    with db.connect(PG_URL) as conn:
        db.init_db(conn)
        with conn.cursor() as cur:
            cur.execute("truncate payment_instruction, payment_beneficiary_seen, payment_message_seen, "
                        "payment_v2_meta, payment_daily_total")
    return mod.PgInstructionStore(PG_URL)


def rec(n: int = 1, **over) -> dict:
    row = {
        "instruction_id": f"id{n:04d}",
        "status": "awaiting_approval",
        "path": "registered",
        "amount": Decimal("100.00"),
        "currency": "ZAR",
        "source_account_id": "acc-1",
        "source_profile_id": "prof-1",
        "source_account_last3": "123",
        "payee_name_norm": "acme ltd",
        "beneficiary_id": "ben-1",
        "beneficiary_fingerprint": "fp-1",
        "account_hmac": None,
        "recent_beneficiary": False,
        "my_reference": "inv7",
        "their_reference": "inv7",
        "figures_source": "typed",
        "message_id_hash": f"hash{n:04d}",
        "notify_to": "piet@example.com",
        "received_at": T0,
        "expires_at": T0 + WINDOW,
        "updated_at": T0,
    }
    row.update(over)
    return row


_ref_counter = {"n": 0}


def ref_gen(prefix: str = "B-1007-"):
    def new_ref() -> str:
        _ref_counter["n"] += 1
        return f"{prefix}{_ref_counter['n']:04x}"
    return new_ref


def offer(store, *ns, notify_to: str = "piet@example.com", now: datetime = T0, **over):
    """Create awaiting_approval rows for ns (ids id000n) and offer them as one batch."""
    for n in ns:
        store.create(rec(n, notify_to=notify_to, **over))
    return store.offer_batch(notify_to, now=now, approval_window=WINDOW, max_items=50, new_ref=ref_gen())


def approve(store, row, *, now: datetime = T0 + H):
    return store.approve_item(row["instruction_id"], batch_ref=row["batch_ref"], item_no=row["item_no"],
                              notify_to=row["notify_to"], now=now, grace=GRACE)


def accepted(store, n: int = 1, **over):
    rows = offer(store, n, **over)
    res = approve(store, rows[0])
    assert res.approved
    return store.get(rows[0]["instruction_id"])
