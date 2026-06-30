"""Postgres-backed pending + daily-aggregate state (OQ2 opt-in backend).

Mirrors the public interface of ``store.PaymentStore`` exactly so the pipeline and
CLI can swap backends without behaviour change. File/JSON stays the DEFAULT; this
backend exists so a Railway deploy keeps the daily-cap + audit trail across
restarts WITHOUT a persistent volume.

The same minimal-field allowlist applies: records are built via
``store._minimal_record`` (PR2/F20) — a raw/parsed account number never enters the
store, only last-3 or a hash; no secret is ever stored. The schema (migration
0009) carries no PAN / secret columns.

PR3/F6/F7: ``execute_and_commit`` runs in ONE transaction — it locks/re-reads the
day's total, re-checks the aggregate via ``caps.check_daily_aggregate`` and, only
if ok, marks the record executed + burns the nonce + increments the day total
atomically. This is STRONGER than the file version (true ACID) while keeping the
same guarantees: never double-pay, never exceed the aggregate, nonce burned
atomically with execution, already-executed -> idempotent no-op.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from .. import db
from .store import _PENDING_ALLOWED, _minimal_record, _utcnow

# Column order used for inserting/selecting a pending record. Matches the
# allowlist in store._PENDING_ALLOWED 1:1 (asserted below at import time).
_PENDING_COLUMNS = (
    "dedup_key",
    "status",
    "amount",
    "currency",
    "beneficiary_id",
    "source_account_id",
    "source_account_last3",
    "message_id_hash",
    "nonce",
    "created_at",
    "executed_at",
    "execution_mode",
)
assert set(_PENDING_COLUMNS) == _PENDING_ALLOWED


def _row_to_record(row) -> dict | None:
    """Map a payment_pending row (column order = _PENDING_COLUMNS) to the same
    dict shape the file store returns: amount as a quantised string, timestamps as
    ISO strings, nonce '' when burned/null."""
    if row is None:
        return None
    rec = dict(zip(_PENDING_COLUMNS, row))
    if rec.get("amount") is not None:
        rec["amount"] = f"{Decimal(str(rec['amount'])).quantize(Decimal('0.01'))}"
    for ts_key in ("created_at", "executed_at"):
        val = rec.get(ts_key)
        if isinstance(val, datetime):
            rec[ts_key] = val.isoformat()
    if rec.get("nonce") is None:
        rec["nonce"] = ""
    return rec


class PgPaymentStore:
    def __init__(self, database_url: str | os.PathLike) -> None:
        self._database_url = str(database_url)

    # ── pending ────────────────────────────────────────────────────────────
    def add_pending(
        self,
        *,
        dedup_key: str,
        amount,
        currency: str,
        beneficiary_id: str,
        source_account_id: str,
        source_account_last3: str,
        message_id: str,
        nonce: str,
    ) -> dict:
        """Idempotently add a pending record keyed by dedup key (F7).

        ``insert ... on conflict (dedup_key) do nothing`` then re-select — one
        pending per dedup key; re-presenting the same email never duplicates.
        """
        record = _minimal_record(
            dedup_key=dedup_key,
            amount=amount,
            currency=currency,
            beneficiary_id=beneficiary_id,
            source_account_id=source_account_id,
            source_account_last3=source_account_last3,
            message_id=message_id,
            nonce=nonce,
        )
        values = [record.get(col) for col in _PENDING_COLUMNS]
        with db.connect(self._database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"insert into payment_pending ({', '.join(_PENDING_COLUMNS)}) "
                    f"values ({', '.join(['%s'] * len(_PENDING_COLUMNS))}) "
                    "on conflict (dedup_key) do nothing;",
                    values,
                )
                cur.execute(
                    f"select {', '.join(_PENDING_COLUMNS)} from payment_pending "
                    "where dedup_key = %s;",
                    (dedup_key,),
                )
                return _row_to_record(cur.fetchone())

    def get(self, dedup_key: str) -> dict | None:
        with db.connect(self._database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"select {', '.join(_PENDING_COLUMNS)} from payment_pending "
                    "where dedup_key = %s;",
                    (dedup_key,),
                )
                return _row_to_record(cur.fetchone())

    def list_records(self, status: str | None = None) -> list[dict]:
        with db.connect(self._database_url) as conn:
            with conn.cursor() as cur:
                if status is not None:
                    cur.execute(
                        f"select {', '.join(_PENDING_COLUMNS)} from payment_pending "
                        "where status = %s;",
                        (status,),
                    )
                else:
                    cur.execute(
                        f"select {', '.join(_PENDING_COLUMNS)} from payment_pending;"
                    )
                return [_row_to_record(row) for row in cur.fetchall()]

    def is_executed(self, dedup_key: str) -> bool:
        rec = self.get(dedup_key)
        return bool(rec and rec.get("status") == "executed")

    # ── daily aggregate ──────────────────────────────────────────────────────
    def daily_total(self, day: str | None = None) -> Decimal:
        day = day or date.today().isoformat()
        with db.connect(self._database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select total from payment_daily_total where day = %s;", (day,)
                )
                row = cur.fetchone()
        return Decimal(str(row[0])) if row and row[0] is not None else Decimal("0")

    # ── atomic execute commit (PR3) ─────────────────────────────────────────
    def execute_and_commit(
        self,
        dedup_key: str,
        amount,
        *,
        daily_aggregate_cap,
        execution_mode: str,
        day: str | None = None,
    ) -> dict:
        """Re-read the day's total under a row lock, re-check the aggregate, then
        in ONE transaction mark executed + burn the nonce + increment the total.

        Returns ``{"committed": bool, "reason": str, "daily_total": str}`` — the
        same shape as the file store. ACID: if the aggregate would be exceeded or
        the record is already executed, NOTHING is mutated.
        """
        from .caps import check_daily_aggregate

        day = day or date.today().isoformat()
        amt = Decimal(str(amount)).quantize(Decimal("0.01"))

        with db.connect(self._database_url) as conn:
            with conn.cursor() as cur:
                # Lock the day's total row (or absence of it) for the txn so two
                # concurrent commits cannot jointly exceed the aggregate.
                cur.execute(
                    "select total from payment_daily_total where day = %s for update;",
                    (day,),
                )
                row = cur.fetchone()
                current = Decimal(str(row[0])) if row and row[0] is not None else Decimal("0")

                decision = check_daily_aggregate(amt, current, daily_aggregate_cap)
                if not decision.ok:
                    return {"committed": False, "reason": decision.reason,
                            "daily_total": str(current)}

                cur.execute(
                    "select status from payment_pending where dedup_key = %s for update;",
                    (dedup_key,),
                )
                prow = cur.fetchone()
                if prow is None:
                    return {"committed": False, "reason": "unknown dedup key",
                            "daily_total": str(current)}
                if prow[0] == "executed":
                    # already executed -> idempotent no-op, do NOT double-count.
                    return {"committed": False, "reason": "already executed",
                            "daily_total": str(current)}

                # Mark executed + burn the single-use nonce (status guard prevents
                # a double-count even under a lost-update race).
                cur.execute(
                    "update payment_pending set status = 'executed', executed_at = %s, "
                    "execution_mode = %s, nonce = '' "
                    "where dedup_key = %s and status <> 'executed';",
                    (_utcnow(), execution_mode, dedup_key),
                )
                # Increment the day total atomically with the execution.
                cur.execute(
                    "insert into payment_daily_total (day, total) values (%s, %s) "
                    "on conflict (day) do update set "
                    "total = payment_daily_total.total + excluded.total;",
                    (day, amt),
                )
                new_total = current + amt
        return {"committed": True, "reason": "", "daily_total": str(new_total)}

    def mark_parked(self, dedup_key: str, reason: str = "") -> None:
        with db.connect(self._database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "update payment_pending set status = 'parked' where dedup_key = %s;",
                    (dedup_key,),
                )

    # ── retention / cleanup (F20) ────────────────────────────────────────────
    def cleanup(self, retention_days: int, now: datetime | None = None) -> int:
        """Remove pending/executed records older than ``retention_days`` and prune
        daily totals past the window. Returns the number of pending rows removed."""
        now = now or _utcnow()
        cutoff = now - timedelta(days=retention_days)
        cutoff_day = cutoff.date()
        with db.connect(self._database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "delete from payment_pending "
                    "where coalesce(executed_at, created_at) < %s;",
                    (cutoff,),
                )
                removed = cur.rowcount
                cur.execute(
                    "delete from payment_daily_total where day < %s;", (cutoff_day,)
                )
        return removed
