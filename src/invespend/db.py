"""Postgres persistence layer (works against Supabase free tier).

Connections use TLS. All writes are idempotent upserts so re-running ingest is
safe.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

import psycopg

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"


def _open(database_url: str, attempts: int = 4) -> psycopg.Connection:
    """Open a connection, retrying transient pooler timeouts with backoff."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return psycopg.connect(
                database_url,
                autocommit=False,
                connect_timeout=15,
                # Required for Supabase's transaction pooler (PgBouncer, 6543):
                # psycopg3's server-side prepared statements collide on shared
                # pooled connections ("prepared statement _pg3_0 already exists").
                prepare_threshold=None,
                options="-c lock_timeout=15000 -c idle_in_transaction_session_timeout=120000",
            )
        except psycopg.OperationalError as exc:  # incl. ConnectionTimeout
            last = exc
            wait = 2 ** i
            log.warning("DB connect attempt %d/%d failed (%s); retrying in %ds",
                        i + 1, attempts, exc, wait)
            time.sleep(wait)
    raise last  # type: ignore[misc]


@contextmanager
def connect(database_url: str) -> Iterator[psycopg.Connection]:
    # Session guards so a stuck job can never wedge the database:
    #  - lock_timeout: fail fast instead of waiting minutes on a held lock
    #  - idle_in_transaction_session_timeout: the server kills any connection
    #    left idle mid-transaction, so an abruptly-killed job self-heals.
    # Transient pooler connection timeouts are retried (see _open).
    conn = _open(database_url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(conn: psycopg.Connection) -> None:
    """Apply every migration in order (each is idempotent).

    Migrations include DDL (``alter table … enable row level security``,
    ``create or replace view``) that needs an AccessExclusive lock. If another
    session holds a conflicting lock — e.g. a leaked ``pg_dump`` left idle in
    transaction — the default behaviour is to wait until the server statement
    timeout (~2 min) and then fail the whole ingest. A short ``lock_timeout``
    makes that fail fast with a clear "lock timeout" error instead of hanging,
    so a transient lock costs seconds and the next run recovers. SET LOCAL keeps
    it scoped to this transaction (and works through transaction pooling).
    """
    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '15s'")
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            cur.execute(migration.read_text())


def oldest_posting_date(conn: psycopg.Connection) -> date | None:
    """The earliest posting_date stored, or None if there are no transactions."""
    with conn.cursor() as cur:
        cur.execute("select min(posting_date) from transactions;")
        return cur.fetchone()[0]


def _group_key(account_id: str, tx: dict) -> tuple:
    """The identifying fields of a transaction, minus the within-day counter."""
    return (
        account_id,
        str(tx.get("valueDate", "")),
        str(tx.get("actionDate", "")),
        str(tx.get("amount", "")),
        str(tx.get("description", "")),
    )


def assign_day_seq(account_id: str, transactions: list[dict]) -> list[tuple[dict, int]]:
    """Pair each transaction with a within-day sequence counter.

    The counter increments across otherwise-identical same-day transactions in
    API order. Because the API returns transactions in a stable order, the same
    rolling-window re-pull yields the same counters — keeping the hash, and thus
    the upsert, idempotent (ARCHITECTURE §5.2).
    """
    counters: dict[tuple, int] = {}
    out: list[tuple[dict, int]] = []
    for tx in transactions:
        key = _group_key(account_id, tx)
        seq = counters.get(key, 0)
        counters[key] = seq + 1
        out.append((tx, seq))
    return out


def transaction_hash(account_id: str, tx: dict, day_seq: int) -> str:
    """Deterministic dedup key — the public API has no stable transaction id.

    sha256(account_id | value_date | action_date | amount | description | day_seq)
    """
    parts = [
        account_id,
        str(tx.get("valueDate", "")),
        str(tx.get("actionDate", "")),
        str(tx.get("amount", "")),
        str(tx.get("description", "")),
        str(day_seq),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def upsert_account(conn: psycopg.Connection, account: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into accounts
                (account_id, account_number, account_name, reference_name,
                 product_name, kyc_compliant, profile_id, last_synced_at)
            values (%s, %s, %s, %s, %s, %s, %s, now())
            on conflict (account_id) do update set
                account_number = excluded.account_number,
                account_name   = excluded.account_name,
                reference_name = excluded.reference_name,
                product_name   = excluded.product_name,
                kyc_compliant  = excluded.kyc_compliant,
                profile_id     = excluded.profile_id,
                last_synced_at = now();
            """,
            (
                account["accountId"],
                account.get("accountNumber"),
                account.get("accountName"),
                account.get("referenceName"),
                account.get("productName"),
                account.get("kycCompliant"),
                account.get("profileId"),
            ),
        )


_TX_COLUMNS = (
    "transaction_hash, account_id, type, transaction_type, status, description, "
    "card_number, posting_date, value_date, action_date, transaction_date, amount, "
    "running_balance, category, day_seq, raw"
)


def _tx_params(account_id: str, tx: dict, category: str, day_seq: int) -> tuple:
    """The 16 column values for one transaction row (order matches _TX_COLUMNS)."""
    signed_amount = float(tx.get("amount", 0))
    if str(tx.get("type", "")).upper() == "DEBIT":
        signed_amount = -abs(signed_amount)
    else:
        signed_amount = abs(signed_amount)
    return (
        transaction_hash(account_id, tx, day_seq),
        account_id,
        tx.get("type"),
        tx.get("transactionType"),
        tx.get("status"),
        tx.get("description"),
        tx.get("cardNumber"),
        _parse_date(tx.get("postingDate")),
        _parse_date(tx.get("valueDate")),
        _parse_date(tx.get("actionDate")),
        _parse_date(tx.get("transactionDate")),
        signed_amount,
        tx.get("runningBalance"),
        category,
        day_seq,
        json.dumps(tx),
    )


def upsert_transaction(
    conn: psycopg.Connection, account_id: str, tx: dict, category: str, day_seq: int = 0
) -> bool:
    """Insert a transaction if new. Returns True when a new row was written."""
    with conn.cursor() as cur:
        cur.execute(
            f"insert into transactions ({_TX_COLUMNS}) "
            f"values ({', '.join(['%s'] * 16)}) "
            "on conflict (transaction_hash) do nothing;",
            _tx_params(account_id, tx, category, day_seq),
        )
        return cur.rowcount > 0


def upsert_transactions(
    conn: psycopg.Connection,
    account_id: str,
    items: list[tuple[dict, str, int]],
    batch_size: int = 500,
) -> int:
    """Batch-insert ``(tx, category, day_seq)`` rows; returns new rows written.

    One multi-row INSERT per batch instead of a round-trip per row — the
    difference between a backfill finishing in minutes vs hours when re-walking
    large histories. Idempotent via ``on conflict do nothing``; ``cur.rowcount``
    counts only the rows that were actually inserted. Batched to stay well under
    Postgres's parameter limit (500 × 16 = 8000 params).
    """
    new_rows = 0
    row_ph = "(" + ", ".join(["%s"] * 16) + ")"
    for i in range(0, len(items), batch_size):
        chunk = items[i:i + batch_size]
        flat = [v for tx, category, day_seq in chunk
                for v in _tx_params(account_id, tx, category, day_seq)]
        with conn.cursor() as cur:
            cur.execute(
                f"insert into transactions ({_TX_COLUMNS}) values "
                + ", ".join([row_ph] * len(chunk))
                + " on conflict (transaction_hash) do nothing;",
                flat,
            )
            new_rows += cur.rowcount
    return new_rows


def extract_balance_fields(balance: dict) -> dict:
    """Map an Investec balance payload to our column values.

    Pure (no I/O) so it is unit tested without a database.
    """
    def num(key: str):
        value = balance.get(key)
        return float(value) if value is not None else None

    return {
        "current_balance": num("currentBalance"),
        "available_balance": num("availableBalance"),
        "budget_balance": num("budgetBalance"),
        "straight_balance": num("straightBalance"),
        "cash_balance": num("cashBalance"),
        "currency": balance.get("currency", "ZAR"),
    }


def upsert_balance(conn: psycopg.Connection, account_id: str, balance: dict) -> None:
    """Record today's balance snapshot (one per account per day; refreshed on re-run)."""
    fields = extract_balance_fields(balance)
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into balances
                (account_id, current_balance, available_balance, budget_balance,
                 straight_balance, cash_balance, currency, raw)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (account_id, as_of_date) do update set
                captured_at       = now(),
                current_balance   = excluded.current_balance,
                available_balance = excluded.available_balance,
                budget_balance    = excluded.budget_balance,
                straight_balance  = excluded.straight_balance,
                cash_balance      = excluded.cash_balance,
                currency          = excluded.currency,
                raw               = excluded.raw;
            """,
            (
                account_id,
                fields["current_balance"],
                fields["available_balance"],
                fields["budget_balance"],
                fields["straight_balance"],
                fields["cash_balance"],
                fields["currency"],
                json.dumps(balance),
            ),
        )


def start_sync_run(conn: psycopg.Connection, from_date: date, to_date: date) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "insert into sync_runs (from_date, to_date, status) "
            "values (%s, %s, 'running') returning id;",
            (from_date, to_date),
        )
        return cur.fetchone()[0]


def finish_sync_run(
    conn: psycopg.Connection,
    run_id: int,
    *,
    accounts: int,
    transactions: int,
    status: str,
    error: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update sync_runs set
                finished_at = now(),
                accounts_synced = %s,
                transactions_upserted = %s,
                status = %s,
                error = %s
            where id = %s;
            """,
            (accounts, transactions, status, error, run_id),
        )


def backfill_transaction_hashes(conn: psycopg.Connection) -> dict:
    """Re-key existing rows to the day_seq-aware hash (see migration 0003).

    Older rows were keyed by the previous hash formula. This recomputes the key
    using the *same* Python functions live ingest uses (``assign_day_seq`` +
    ``transaction_hash``) on each row's stored ``raw`` payload, so the backfilled
    keys are byte-identical to what a future re-pull would produce — the
    rolling-window overlap then dedupes cleanly instead of creating duplicates.

    Idempotent: rows already on the new key are skipped, so it is safe to run
    repeatedly. Returns a small summary dict.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select account_id, transaction_hash, raw "
            "from transactions "
            "order by account_id, ingested_at, transaction_hash;"
        )
        rows = cur.fetchall()

    # Group rows per account, preserving the deterministic fetch order.
    by_account: dict[str, list[tuple[str, dict]]] = {}
    for account_id, old_hash, raw in rows:
        tx = json.loads(raw) if isinstance(raw, str) else raw
        by_account.setdefault(account_id, []).append((old_hash, tx))

    scanned = 0
    updated = 0
    with conn.cursor() as cur:
        for account_id, items in by_account.items():
            txs = [tx for _, tx in items]
            for (old_hash, _), (tx, day_seq) in zip(items, assign_day_seq(account_id, txs)):
                scanned += 1
                new_hash = transaction_hash(account_id, tx, day_seq)
                if new_hash != old_hash:
                    cur.execute(
                        "update transactions "
                        "set transaction_hash = %s, day_seq = %s "
                        "where transaction_hash = %s;",
                        (new_hash, day_seq, old_hash),
                    )
                    updated += cur.rowcount
    return {"scanned": scanned, "updated": updated}
