"""Daily ingestion job: Investec API → Postgres."""
from __future__ import annotations

import logging
from datetime import date, timedelta

from . import db
from .categorize import categorize
from .config import Settings
from .investec_client import InvestecClient

log = logging.getLogger(__name__)


def run_ingest(
    settings: Settings,
    window_days: int | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
) -> dict:
    """Pull transactions and upsert them.

    By default pulls a rolling window ending today. Pass explicit ``from_date`` /
    ``to_date`` for a backfill. Returns a small summary dict for observability.
    """
    to_date = to_date or date.today()
    if from_date is None:
        window = window_days or settings.ingest_window_days
        from_date = to_date - timedelta(days=window)

    client = InvestecClient(
        client_id=settings.investec_client_id,
        client_secret=settings.investec_client_secret,
        api_key=settings.investec_api_key,
        base_url=settings.investec_base_url,
    )

    accounts_synced = 0
    tx_upserted = 0

    with db.connect(settings.database_url) as conn:
        db.init_db(conn)
        run_id = db.start_sync_run(conn, from_date, to_date)
        try:
            accounts = client.get_accounts()
            log.info("Found %d account(s)", len(accounts))
            for account in accounts:
                db.upsert_account(conn, account)
                accounts_synced += 1
                account_id = account["accountId"]

                transactions = client.get_transactions(account_id, from_date, to_date)
                for tx in transactions:
                    category = categorize(tx.get("description"), tx.get("transactionType"))
                    if db.upsert_transaction(conn, account_id, tx, category):
                        tx_upserted += 1
                log.info(
                    "Account %s: %d transactions pulled (%d new)",
                    account.get("accountNumber", account_id),
                    len(transactions),
                    tx_upserted,
                )

            db.finish_sync_run(
                conn, run_id,
                accounts=accounts_synced,
                transactions=tx_upserted,
                status="success",
            )
        except Exception as exc:  # noqa: BLE001 - record then re-raise
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
    }
    log.info("Ingest complete: %s", summary)
    return summary
