"""Postgres persistence layer (works against Supabase free tier).

Connections use TLS. All writes are idempotent upserts so re-running ingest is
safe.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

import psycopg

MIGRATION = Path(__file__).resolve().parents[2] / "db" / "migrations" / "0001_init.sql"


@contextmanager
def connect(database_url: str) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(database_url, autocommit=False)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(conn: psycopg.Connection) -> None:
    """Apply the schema migration (idempotent)."""
    sql = MIGRATION.read_text()
    with conn.cursor() as cur:
        cur.execute(sql)


def transaction_hash(account_id: str, tx: dict) -> str:
    """Deterministic dedup key — the public API has no stable transaction id."""
    parts = [
        account_id,
        str(tx.get("postingDate", "")),
        str(tx.get("valueDate", "")),
        str(tx.get("amount", "")),
        str(tx.get("type", "")),
        str(tx.get("transactionType", "")),
        str(tx.get("description", "")),
        str(tx.get("runningBalance", "")),
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


def upsert_transaction(
    conn: psycopg.Connection, account_id: str, tx: dict, category: str
) -> bool:
    """Insert a transaction if new. Returns True when a new row was written."""
    signed_amount = float(tx.get("amount", 0))
    if str(tx.get("type", "")).upper() == "DEBIT":
        signed_amount = -abs(signed_amount)
    else:
        signed_amount = abs(signed_amount)

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into transactions
                (transaction_hash, account_id, type, transaction_type, status,
                 description, card_number, posting_date, value_date, action_date,
                 transaction_date, amount, running_balance, category, raw)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (transaction_hash) do nothing;
            """,
            (
                transaction_hash(account_id, tx),
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
                json.dumps(tx),
            ),
        )
        return cur.rowcount > 0


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
