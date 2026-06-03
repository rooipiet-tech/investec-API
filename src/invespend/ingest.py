"""Daily ingestion + historical backfill: Investec API → Postgres.

Resilience model (ARCHITECTURE §5): the API and the database are never coupled
inside one long transaction. We fetch a chunk of data from Investec *with no DB
transaction open*, then write it in a short, self-contained transaction. This
means:

* no connection ever sits ``idle in transaction`` waiting on a slow API call,
  so it cannot hold a table lock (or leak one) for more than a few milliseconds;
* a wide backfill is split into bounded date windows, each committed on its own,
  so progress is durable and a failure costs at most one chunk, not everything.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Iterator

from . import db
from .categorize import categorize
from .config import Settings
from .investec_client import InvestecClient

log = logging.getLogger(__name__)

# Window size for a single transactions API call. Narrow enough that the API
# responds quickly even over long histories, wide enough to keep call counts low.
CHUNK_DAYS = 90
# When backfilling, stop after this many consecutive empty windows — i.e. we have
# walked back past everything Investec still retains.
MAX_EMPTY_CHUNKS = 3


def _date_chunks(from_date: date, to_date: date, chunk_days: int) -> Iterator[tuple[date, date]]:
    """Yield (start, end) windows covering [from_date, to_date], newest first."""
    end = to_date
    while end >= from_date:
        start = max(from_date, end - timedelta(days=chunk_days - 1))
        yield start, end
        end = start - timedelta(days=1)


def run_ingest(
    settings: Settings,
    window_days: int | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    chunk_days: int = CHUNK_DAYS,
    resume: bool = False,
) -> dict:
    """Pull transactions and upsert them.

    By default pulls a rolling window ending today. Pass explicit ``from_date`` /
    ``to_date`` for a backfill. With ``resume=True`` a backfill that was cut short
    (e.g. by a CI time limit) continues *older* than the data already stored,
    instead of re-pulling the recent windows every run. Returns a small summary
    dict for observability.
    """
    to_date = to_date or date.today()
    if from_date is None:
        window = window_days or settings.ingest_window_days
        from_date = to_date - timedelta(days=window)

    url = settings.database_url

    if resume:
        # Continue from where a previous backfill stopped: end this run just after
        # the oldest stored transaction (small overlap to heal the boundary) so the
        # windows march further back into history each run.
        with db.connect(url) as conn:
            oldest = db.oldest_posting_date(conn)
        if oldest:
            to_date = oldest + timedelta(days=2)
            log.info("Resume: continuing backfill older than %s (to_date=%s)",
                     oldest, to_date)
    client = InvestecClient(
        client_id=settings.investec_client_id,
        client_secret=settings.investec_client_secret,
        api_key=settings.investec_api_key,
        base_url=settings.investec_base_url,
    )

    accounts_synced = 0
    tx_upserted = 0
    balances_captured = 0

    # Schema first, in its own short transaction (DDL never shares a transaction
    # with the data load, so it can't lock the tables for the whole run).
    with db.connect(url) as conn:
        db.init_db(conn)
    with db.connect(url) as conn:
        run_id = db.start_sync_run(conn, from_date, to_date)

    try:
        accounts = client.get_accounts()  # API call — no DB transaction open
        log.info("Found %d account(s)", len(accounts))
        with db.connect(url) as conn:
            for account in accounts:
                db.upsert_account(conn, account)
                accounts_synced += 1

        # Balance snapshots: fetch (API) then write (DB) separately, per account.
        for account in accounts:
            account_id = account["accountId"]
            try:
                balance = client.get_balance(account_id)  # API
                with db.connect(url) as conn:             # DB
                    db.upsert_balance(conn, account_id, balance)
                balances_captured += 1
            except Exception as exc:  # noqa: BLE001 - non-fatal
                log.warning("Balance fetch failed for %s: %s", account_id, exc)

        # Transactions: walk date windows newest→oldest. Fetch each window with no
        # transaction open, then write it in a short one.
        is_backfill = (to_date - from_date).days > chunk_days
        empty_streak = 0
        for chunk_start, chunk_end in _date_chunks(from_date, to_date, chunk_days):
            chunk_total = 0
            for account in accounts:
                account_id = account["accountId"]
                transactions = client.get_transactions(account_id, chunk_start, chunk_end)
                chunk_total += len(transactions)
                if not transactions:
                    continue
                rows = db.assign_day_seq(account_id, transactions)
                with db.connect(url) as conn:
                    for tx, day_seq in rows:
                        category = categorize(tx.get("description"), tx.get("transactionType"))
                        if db.upsert_transaction(conn, account_id, tx, category, day_seq):
                            tx_upserted += 1
            log.info(
                "Window %s..%s: %d transactions (%d new so far)",
                chunk_start, chunk_end, chunk_total, tx_upserted,
            )
            if is_backfill:
                empty_streak = empty_streak + 1 if chunk_total == 0 else 0
                if empty_streak >= MAX_EMPTY_CHUNKS:
                    log.info("Reached end of available history; stopping backfill.")
                    break

        with db.connect(url) as conn:
            db.finish_sync_run(
                conn, run_id,
                accounts=accounts_synced,
                transactions=tx_upserted,
                status="success",
            )
    except Exception as exc:  # noqa: BLE001 - record then re-raise
        # Record the failure in its own connection so it persists.
        with db.connect(url) as conn:
            db.finish_sync_run(
                conn, run_id,
                accounts=accounts_synced,
                transactions=tx_upserted,
                status="error",
                error=str(exc),
            )
        raise

    summary = {
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "accounts": accounts_synced,
        "transactions_upserted": tx_upserted,
        "balances_captured": balances_captured,
    }
    log.info("Ingest complete: %s", summary)
    return summary
