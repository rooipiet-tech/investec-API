"""Postgres-backed ``InstructionStore`` (S9). One connection per call, same pattern as ``pg_store``.

EVERY mutating method is ONE transaction (one ``db.connect`` block: commit on success, rollback on any
exception) built from CAS ``WHERE`` clauses and ``RETURNING``. ``claim_for_execution`` inserts the day row,
locks it, then locks the instruction row; the HTTP POST happens AFTER the claim returned (its connection is
already closed) and OUTSIDE any transaction. No DDL here: the schema is migration 0013.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Collection
from datetime import datetime, timedelta
from decimal import Decimal

import psycopg

from .. import db
from .caps import check_daily_aggregate
from .instructions import (
    ACTIVE_STATUSES, BOOTSTRAP_KEY, DUPLICATE_GUARD_STATUSES, EXECUTION_MODES,
    INSTRUCTION_COLUMNS, MESSAGE_SEEN_COLUMNS, TERMINAL_STATUSES, ApproveResult, BeneficiaryObservation, ClaimResult,
    StoreNotInitialised, _amount, _check_cas_edge, check_finalize, check_notified_field, message_for, offer_digest,
    sast_day, validate_create_record,
)

_COLS = ", ".join(INSTRUCTION_COLUMNS)
_TERMINAL_SQL = ", ".join(f"'{s}'" for s in sorted(TERMINAL_STATUSES))
_TABLES = ("payment_instruction", "payment_beneficiary_seen", "payment_message_seen", "payment_v2_meta")
_OFFER_ATTEMPTS = 3


def _row(values) -> dict | None:
    if values is None:
        return None
    return dict(zip(INSTRUCTION_COLUMNS, values))


class PgInstructionStore:
    durable = True

    def __init__(self, database_url: str | os.PathLike) -> None:
        self._database_url = str(database_url)

    def _connect(self):
        return db.connect(self._database_url)

    # -- reads --------------------------------------------------------------
    def ping(self) -> None:
        """Raises when the database is unreachable; ``StoreNotInitialised`` when a 0013 table is missing."""
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    for table in _TABLES:
                        cur.execute(f"select 1 from {table} limit 1")
        except psycopg.errors.UndefinedTable:
            raise StoreNotInitialised("the payment v2 tables are missing; run init-db") from None

    def get(self, instruction_id: str) -> dict | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"select {_COLS} from payment_instruction where instruction_id = %s", (instruction_id,))
                return _row(cur.fetchone())

    def _list(self, where: str, params: tuple, order: str) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"select {_COLS} from payment_instruction where {where} order by {order}", params)
                return [_row(r) for r in cur.fetchall()]

    def list_active(self) -> list[dict]:
        return self.list_by_status(ACTIVE_STATUSES)

    def list_by_status(self, statuses: Collection[str]) -> list[dict]:
        return self._list("status = any(%s)", (sorted(statuses),), "received_at, instruction_id")

    def find_recent_similar(self, source_account_id: str, payee_name_norm: str, amount: Decimal,
                            since: datetime) -> dict | None:
        """Duplicate guard: only rows in DUPLICATE_GUARD_STATUSES; ``since`` bounds ``received_at`` (the trusted
        receipt time), NEVER ``updated_at`` (R6-T5)."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"select {_COLS} from payment_instruction where status = any(%s) and source_account_id = %s "
                    "and payee_name_norm = %s and amount = %s and received_at >= %s "
                    "order by received_at desc, instruction_id limit 1",
                    (sorted(DUPLICATE_GUARD_STATUSES), source_account_id, payee_name_norm, _amount(amount), since))
                return _row(cur.fetchone())

    def list_batch(self, batch_ref: str, notify_to: str) -> list[dict]:
        return self._list("batch_ref = %s and notify_to = %s", (batch_ref, notify_to), "item_no")

    def list_pending_offered(self, notify_to: str, *, now: datetime, exclude_batch_ref: str | None = None) -> list[dict]:
        return self._list(
            "status = 'awaiting_approval' and batch_ref is not null and notify_to = %s and expires_at > %s "
            "and batch_ref is distinct from %s", (notify_to, now, exclude_batch_ref), "offered_at, batch_ref, item_no")

    def list_unnotified_batches(self, *, older_than: timedelta, now: datetime) -> list[str]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select distinct batch_ref from payment_instruction where batch_ref is not null "
                    "and batch_notified_at is null and status = 'awaiting_approval' and expires_at > %s "
                    "and offered_at < %s order by batch_ref", (now, now - older_than))
                return [r[0] for r in cur.fetchall()]

    def daily_total(self, when: datetime) -> Decimal:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select total from payment_daily_total where day = %s", (sast_day(when),))
                row = cur.fetchone()
        return Decimal(str(row[0])) if row and row[0] is not None else Decimal("0.00")

    # -- create ----------------------------------------------------------------
    def create(self, record: dict) -> tuple[dict, bool]:
        row = validate_create_record(record)           # ValueError BEFORE any connection
        values = [row[c] for c in INSTRUCTION_COLUMNS]
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"insert into payment_instruction ({_COLS}) values ({', '.join(['%s'] * len(values))}) "
                    "on conflict (instruction_id) do nothing returning instruction_id", values)
                created = cur.fetchone() is not None
                cur.execute(f"select {_COLS} from payment_instruction where instruction_id = %s", (row["instruction_id"],))
                return _row(cur.fetchone()), created

    # -- message identity ----------------------------------------------------------
    def mark_message_seen(self, instruction_id: str, now: datetime) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "insert into payment_message_seen (instruction_id, outcome, seen_at) values (%s, 'processing', %s) "
                    "on conflict (instruction_id) do nothing returning instruction_id", (instruction_id, now))
                return cur.fetchone() is not None

    def set_message_outcome(self, instruction_id: str, outcome: str, *, auth_from: str | None = None) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "update payment_message_seen set outcome = %s, auth_from = coalesce(%s, auth_from) "
                    "where instruction_id = %s", (outcome, auth_from, instruction_id))

    def sweep_stuck_messages(self, *, older_than: timedelta, now: datetime) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("update payment_message_seen set outcome = 'stuck' where outcome = 'processing' "
                            "and seen_at < %s", (now - older_than,))
                cur.execute(
                    f"select {', '.join(MESSAGE_SEEN_COLUMNS)} from payment_message_seen where outcome = 'stuck' "
                    "and auth_from is not null and resend_notified_at is null order by seen_at, instruction_id")
                return [dict(zip(MESSAGE_SEEN_COLUMNS, r)) for r in cur.fetchall()]

    def mark_resend_notified(self, instruction_id: str, now: datetime) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "update payment_message_seen set resend_notified_at = %s where instruction_id = %s "
                    "and resend_notified_at is null returning instruction_id", (now, instruction_id))
                return cur.fetchone() is not None

    # -- beneficiary memory and meta ---------------------------------------------------
    def observe_beneficiary(self, beneficiary_id: str, now: datetime, *, fingerprint: str | None,
                            established: bool = False) -> BeneficiaryObservation:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "insert into payment_beneficiary_seen (beneficiary_id, first_seen_at, established, last_fingerprint) "
                    "values (%s, %s, %s, %s) on conflict (beneficiary_id) do nothing",
                    (beneficiary_id, now, bool(established), fingerprint))
                cur.execute(
                    "select first_seen_at, established, last_fingerprint, fingerprint_changed_at "
                    "from payment_beneficiary_seen where beneficiary_id = %s for update", (beneficiary_id,))
                first_seen, est, last, changed = cur.fetchone()
                if fingerprint is not None and last is None:
                    cur.execute("update payment_beneficiary_seen set last_fingerprint = %s where beneficiary_id = %s",
                                (fingerprint, beneficiary_id))
                    last = fingerprint
                elif fingerprint is not None and last != fingerprint:
                    cur.execute("update payment_beneficiary_seen set last_fingerprint = %s, fingerprint_changed_at = %s "
                                "where beneficiary_id = %s", (fingerprint, now, beneficiary_id))
                    last, changed = fingerprint, now
                return BeneficiaryObservation(first_seen, bool(est), last, changed)

    def meta_get(self, key: str) -> str | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select value from payment_v2_meta where key = %s", (key,))
                row = cur.fetchone()
                return None if row is None else row[0]

    def meta_set(self, key: str, value: str, now: datetime) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "insert into payment_v2_meta (key, value, set_at) values (%s, %s, %s) "
                    "on conflict (key) do update set value = excluded.value, set_at = excluded.set_at",
                    (key, value, now))

    def bootstrap_done(self) -> bool:
        return self.meta_get(BOOTSTRAP_KEY) is not None

    def mark_bootstrap_done(self, now: datetime) -> None:
        self.meta_set(BOOTSTRAP_KEY, "done", now)

    # -- hold / ready ---------------------------------------------------------------
    def set_held(self, instruction_id: str, *, beneficiary_id: str, fingerprint: str, first_seen_at: datetime,
                 eligible_at: datetime, expires_at: datetime, now: datetime) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "update payment_instruction set status = 'held', beneficiary_id = %s, beneficiary_fingerprint = %s, "
                    "first_seen_at = coalesce(first_seen_at, %s), eligible_at = coalesce(eligible_at, %s), "
                    "expires_at = %s, updated_at = %s "
                    "where instruction_id = %s and status = 'awaiting_beneficiary' and eligible_at is null "
                    "and expires_at > %s returning instruction_id",
                    (beneficiary_id, fingerprint, first_seen_at, eligible_at, expires_at, now, instruction_id, now))
                return cur.fetchone() is not None

    def make_ready(self, instruction_id: str, *, now: datetime, approval_window: timedelta) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "update payment_instruction set status = 'awaiting_approval', expires_at = %s, updated_at = %s "
                    "where instruction_id = %s and status = 'held' and eligible_at <= %s and expires_at > %s "
                    "returning instruction_id", (now + approval_window, now, instruction_id, now, now))
                return cur.fetchone() is not None

    # -- batches ----------------------------------------------------------------------
    def offer_batch(self, notify_to: str, *, now: datetime, approval_window: timedelta, max_items: int,
                    new_ref: Callable[[], str]) -> list[dict]:
        for attempt in range(_OFFER_ATTEMPTS):
            try:
                return self._offer_once(notify_to, now, approval_window, max_items, new_ref)
            except psycopg.errors.UniqueViolation:
                if attempt == _OFFER_ATTEMPTS - 1:
                    raise
        return []   # pragma: no cover

    def _offer_once(self, notify_to: str, now: datetime, approval_window: timedelta, max_items: int,
                    new_ref: Callable[[], str]) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"select {_COLS} from payment_instruction where status = 'awaiting_approval' and notify_to = %s "
                    "and batch_ref is null and expires_at > %s order by received_at, instruction_id limit %s for update",
                    (notify_to, now, max(int(max_items), 0)))
                rows = [_row(r) for r in cur.fetchall()]
                if not rows:
                    return []
                ref = None
                for _ in range(5):
                    candidate = new_ref()
                    cur.execute("select 1 from payment_instruction where batch_ref = %s limit 1", (candidate,))
                    if cur.fetchone() is None:
                        ref = candidate
                        break
                if ref is None:
                    raise RuntimeError("could not allocate a unique batch ref")
                offered = []
                for number, row in enumerate(rows, start=1):
                    cur.execute(
                        "update payment_instruction set batch_ref = %s, item_no = %s, offered_at = %s, offer_digest = %s, "
                        "expires_at = %s, updated_at = %s "
                        "where instruction_id = %s and status = 'awaiting_approval' and batch_ref is null "
                        f"returning {_COLS}",
                        (ref, number, now, offer_digest(row, ref, number), now + approval_window, now,
                         row["instruction_id"]))
                    updated = _row(cur.fetchone())
                    if updated is not None:
                        offered.append(updated)
                return offered

    def unoffer_batch(self, batch_ref: str, *, now: datetime) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "update payment_instruction set batch_ref = null, item_no = null, offered_at = null, "
                    "offer_digest = null, updated_at = %s where batch_ref = %s and batch_notified_at is null "
                    "and status = 'awaiting_approval'", (now, batch_ref))
                return cur.rowcount

    def mark_batch_notified(self, batch_ref: str, now: datetime) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "update payment_instruction set batch_notified_at = %s, updated_at = %s "
                    "where batch_ref = %s and batch_notified_at is null", (now, now, batch_ref))
                return cur.rowcount

    # -- approval and CAS -----------------------------------------------------------------
    def approve_item(self, instruction_id: str, *, batch_ref: str, item_no: int, notify_to: str, now: datetime,
                     grace: timedelta) -> ApproveResult:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "update payment_instruction set status = 'accepted', approved_at = %s, expires_at = %s, "
                    "updated_at = %s where instruction_id = %s and status = 'awaiting_approval' and batch_ref = %s "
                    "and item_no = %s and notify_to = %s and offer_digest is not null and expires_at > %s "
                    "returning instruction_id",
                    (now, now + grace, now, instruction_id, batch_ref, item_no, notify_to, now))
                if cur.fetchone() is not None:
                    return ApproveResult(True, "ok")
                cur.execute(
                    "select status, batch_ref, item_no, notify_to, expires_at, offer_digest from payment_instruction "
                    "where instruction_id = %s", (instruction_id,))
                row = cur.fetchone()
        if row is None:
            return ApproveResult(False, "wrong_batch")
        status, row_ref, row_no, row_to, expires_at, digest = row
        if row_ref != batch_ref or row_no != item_no or row_to != notify_to or digest is None:
            return ApproveResult(False, "wrong_batch")
        if status != "awaiting_approval":
            return ApproveResult(False, "not_awaiting")
        if expires_at <= now:
            return ApproveResult(False, "expired")
        return ApproveResult(False, "wrong_batch")      # lost a race with a concurrent change

    def cas_status(self, instruction_id: str, expected: Collection[str], new: str, *, now: datetime,
                   outcome_code: str | None = None) -> bool:
        exp = _check_cas_edge(expected, new)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "update payment_instruction set status = %s, updated_at = %s, "
                    "outcome_code = coalesce(%s, outcome_code) "
                    "where instruction_id = %s and status = any(%s) returning instruction_id",
                    (new, now, outcome_code, instruction_id, exp))
                return cur.fetchone() is not None

    def claim_for_execution(self, instruction_id: str, amount: Decimal, *, daily_cap: Decimal, execution_mode: str,
                            now: datetime) -> ClaimResult:
        if execution_mode not in EXECUTION_MODES:
            raise ValueError("execution_mode must be dry-run or live")
        day = sast_day(now)
        wanted = _amount(amount)
        with self._connect() as conn:
            with conn.cursor() as cur:
                # Insert the day row first so the lock below always has a row to take (no gap lock race).
                cur.execute("insert into payment_daily_total (day, total) values (%s, 0) on conflict (day) do nothing", (day,))
                cur.execute("select total from payment_daily_total where day = %s for update", (day,))
                total_row = cur.fetchone()
                current = Decimal(str(total_row[0])) if total_row and total_row[0] is not None else Decimal("0.00")
                cur.execute("select status, approved_at, expires_at, amount from payment_instruction "
                            "where instruction_id = %s for update", (instruction_id,))
                row = cur.fetchone()
                if row is None or row[0] != "accepted" or row[1] is None:
                    return ClaimResult(False, "cas_lost", current)
                if _amount(row[3]) != wanted:
                    raise ValueError("amount does not match the stored row")
                if row[2] <= now:
                    return ClaimResult(False, "stale", current)
                if not check_daily_aggregate(wanted, current, daily_cap).ok:
                    return ClaimResult(False, "daily_cap", current)
                cur.execute(
                    "update payment_instruction set status = 'submitting', daily_reserved = true, reserved_day = %s, "
                    "execution_mode = %s, updated_at = %s "
                    "where instruction_id = %s and status = 'accepted' and approved_at is not null and expires_at > %s "
                    "returning instruction_id", (day, execution_mode, now, instruction_id, now))
                if cur.fetchone() is None:
                    return ClaimResult(False, "cas_lost", current)
                cur.execute("update payment_daily_total set total = total + %s where day = %s", (wanted, day))
                return ClaimResult(True, "ok", current + wanted)

    def finalize(self, instruction_id: str, expected: str, new_status: str, *, release: bool, now: datetime,
                 outcome_code: str | None = None, outcome_message: str | None = None) -> bool:
        check_finalize(expected, new_status, release)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "update payment_instruction set status = %s, updated_at = %s, "
                    "executed_at = coalesce(%s, executed_at), outcome_code = coalesce(%s, outcome_code), "
                    "outcome_message = coalesce(%s, outcome_message) "
                    "where instruction_id = %s and status = %s returning amount, reserved_day, daily_reserved",
                    (new_status, now, now if new_status == "executed" else None, outcome_code,
                     message_for(new_status, outcome_message), instruction_id, expected))
                row = cur.fetchone()
                if row is None:
                    return False
                amount, reserved_day, reserved = row
                if release and reserved:
                    cur.execute("update payment_instruction set daily_reserved = false where instruction_id = %s",
                                (instruction_id,))
                    if reserved_day is not None:
                        # the RETURNED day and amount, never the day of ``now`` (a claim at 23:59 SAST, finalize at 00:01)
                        cur.execute("update payment_daily_total set total = greatest(total - %s, 0) where day = %s",
                                    (amount, reserved_day))
                return True

    def recover_stale_submitting(self, *, older_than: timedelta, now: datetime) -> list[str]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "update payment_instruction set status = 'needs_review', outcome_code = 'stale_submitting', "
                    "updated_at = %s where status = 'submitting' and updated_at < %s returning instruction_id",
                    (now, now - older_than))
                return sorted(r[0] for r in cur.fetchall())

    def mark_notified(self, instruction_id: str, field: str, now: datetime) -> bool:
        column = check_notified_field(field)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"update payment_instruction set {column} = %s, updated_at = %s "
                    f"where instruction_id = %s and {column} is null returning instruction_id",
                    (now, now, instruction_id))
                return cur.fetchone() is not None

    def cleanup(self, retention_days: int, now: datetime) -> int:
        cutoff = now - timedelta(days=retention_days)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"delete from payment_instruction where status in ({_TERMINAL_SQL}) and updated_at < %s",
                            (cutoff,))
                removed = cur.rowcount
                cur.execute("delete from payment_message_seen where seen_at < %s", (cutoff,))
                return removed
