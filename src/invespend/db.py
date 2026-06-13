"""Postgres persistence layer (works against Supabase free tier).

Connections use TLS. All writes are idempotent upserts so re-running ingest is
safe.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import psycopg

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"


def _money(value: object) -> Decimal | None:
    """Parse a monetary value into an exact Decimal (None if absent/unparseable).

    Money is kept as Decimal, never float: the amount/running_balance columns are
    numeric(18,2) and reconciliation compares them to the cent, so binary
    floating point must never enter the pipeline. Parsing via ``str`` keeps the
    exact decimal digits Investec sent (``Decimal(0.1)`` would not)."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _scalar(cur: psycopg.Cursor):
    """First column of the next row; raises if the query returned nothing.

    Used for queries that always yield exactly one row (aggregates, ``RETURNING``,
    existence checks) — it makes that assumption explicit instead of indexing a
    possibly-``None`` ``fetchone()`` result."""
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("query returned no rows where one was expected")
    return row[0]


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
    """Apply pending migrations exactly once, tracked in ``schema_migrations``.

    Previously every migration re-ran on every ingest, and the DDL they contain
    (``alter table … enable row level security``, ``create or replace view``)
    takes AccessExclusive locks — the root cause of the recurring lock-wait
    failures. Tracking applied filenames makes the nightly init a no-op in the
    steady state (one SELECT, no locks) and also lets future migrations be
    non-idempotent (data fixes, column renames) safely.

    On a database created before tracking existed, the first run re-applies
    every migration once (they are all idempotent) and records them.

    A short ``lock_timeout`` is still set so that when a migration *does* run,
    a conflicting lock (e.g. a leaked ``pg_dump`` left idle in transaction)
    fails fast with a clear error instead of hanging for minutes. SET LOCAL
    keeps it scoped to this transaction (and works through transaction pooling).
    """
    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '15s'")
        cur.execute(
            """
            create table if not exists schema_migrations (
                filename   text primary key,
                applied_at timestamptz not null default now()
            );
            """
        )
        # Lock it down like every other table (anon/authenticated see nothing).
        # Guarded so the ALTER (and its AccessExclusive lock) runs only once.
        cur.execute(
            "select coalesce((select relrowsecurity from pg_class "
            "where relname = 'schema_migrations' "
            "and relnamespace = 'public'::regnamespace), false);"
        )
        if not _scalar(cur):
            cur.execute("alter table schema_migrations enable row level security;")

        cur.execute("select filename from schema_migrations;")
        applied = {row[0] for row in cur.fetchall()}
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if migration.name in applied:
                continue
            log.info("Applying migration %s", migration.name)
            cur.execute(migration.read_text())
            cur.execute(
                "insert into schema_migrations (filename) values (%s);",
                (migration.name,),
            )

        # Keep the read-only reporting role's grants in sync with the build
        # layer (no-op until the operator creates the role — see db/roles.sql).
        grants = MIGRATIONS_DIR.parent / "grants.sql"
        if grants.exists():
            cur.execute(grants.read_text())


def oldest_posting_date(conn: psycopg.Connection) -> date | None:
    """The earliest posting_date stored, or None if there are no transactions."""
    with conn.cursor() as cur:
        cur.execute("select min(posting_date) from transactions;")
        return _scalar(cur)


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


_INSERT_TRANSACTION_SQL = """
    insert into transactions
        (transaction_hash, account_id, type, transaction_type, status,
         description, card_number, posting_date, value_date, action_date,
         transaction_date, amount, running_balance, category, day_seq, raw)
    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    on conflict (transaction_hash) do nothing;
"""


def _transaction_row(account_id: str, tx: dict, category: str, day_seq: int) -> tuple:
    signed_amount = _money(tx.get("amount")) or Decimal("0")
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
        _money(tx.get("runningBalance")),
        category,
        day_seq,
        json.dumps(tx),
    )


def upsert_transactions(
    conn: psycopg.Connection, account_id: str, rows: list[tuple[dict, str, int]]
) -> int:
    """Bulk-insert transactions; returns how many NEW rows were written.

    ``rows`` is ``[(tx, category, day_seq), ...]``. One ``executemany`` batch
    per call (psycopg pipelines it) instead of a round trip per row — which is
    what dominates a backfill over a TLS pooler connection. Idempotency is
    unchanged: ``ON CONFLICT DO NOTHING`` on the deterministic hash, and
    ``rowcount`` aggregates only the rows actually inserted.
    """
    if not rows:
        return 0
    params = [_transaction_row(account_id, tx, category, day_seq)
              for tx, category, day_seq in rows]
    with conn.cursor() as cur:
        cur.executemany(_INSERT_TRANSACTION_SQL, params)
        return max(cur.rowcount, 0)


def extract_balance_fields(balance: dict) -> dict:
    """Map an Investec balance payload to our column values.

    Pure (no I/O) so it is unit tested without a database.
    """
    return {
        "current_balance": _money(balance.get("currentBalance")),
        "available_balance": _money(balance.get("availableBalance")),
        "budget_balance": _money(balance.get("budgetBalance")),
        "straight_balance": _money(balance.get("straightBalance")),
        "cash_balance": _money(balance.get("cashBalance")),
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
        return _scalar(cur)


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
            for (old_hash, _), (tx, day_seq) in zip(
                items, assign_day_seq(account_id, txs), strict=True
            ):
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
