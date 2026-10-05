"""Beneficiary snapshot sync + match persistence (called from ``ingest`` when enabled).

Same resilience model as ingest: the Investec API call happens with NO database
connection open; DB work happens in short, self-contained transactions. Any
failure here is non-fatal to ingest (the caller wraps it), and a failed or
malformed fetch never wipes the existing snapshot.

Logs counts only, never beneficiary names or account numbers.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from . import db
from .beneficiary_match import (
    PAYMENT_TRANSACTION_TYPES,
    STATUS_AMBIGUOUS,
    STATUS_MATCHED,
    STATUS_NO_CANDIDATE,
    match_all,
    parse_beneficiaries,
)

log = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sync_and_match(
    client,
    database_url: str,
    *,
    now: Callable[[], datetime] = _utc_now,
) -> dict:
    """Fetch beneficiaries, refresh the snapshot, match open payments, persist.

    Returns a small dict of counts ({} when the fetch was skipped). The result
    is for logging only and is never merged into the ingest summary.
    """
    data = client.get_beneficiaries()  # API call, no DB connection open
    records = parse_beneficiaries(data)
    if records is None:
        log.warning("Beneficiary fetch returned a malformed payload; snapshot kept")
        return {}
    if not records:
        log.warning("Beneficiary fetch returned no beneficiaries; snapshot kept")
        return {}

    synced_at = now()
    with db.connect(database_url) as conn:
        db.upsert_beneficiaries(conn, records, synced_at)
        retired = db.retire_missing_beneficiaries(
            conn, [r.beneficiary_id for r in records])
        own = db.load_own_account_numbers(conn)
        benes = db.load_active_beneficiaries(conn)
        txs = db.load_match_candidates(conn, sorted(PAYMENT_TRANSACTION_TYPES))

    results = match_all(txs, benes, own)  # pure, no connection open

    with db.connect(database_url) as conn:
        db.upsert_beneficiary_matches(conn, results, synced_at)

    counts = {
        "beneficiaries": len(records),
        "retired": retired,
        "candidates": len(results),
        STATUS_MATCHED: sum(r.status == STATUS_MATCHED for r in results),
        STATUS_AMBIGUOUS: sum(r.status == STATUS_AMBIGUOUS for r in results),
        STATUS_NO_CANDIDATE: sum(r.status == STATUS_NO_CANDIDATE for r in results),
    }
    log.info(
        "Beneficiary matching: beneficiaries=%d retired=%d candidates=%d "
        "matched=%d ambiguous=%d no_candidate=%d",
        counts["beneficiaries"], counts["retired"], counts["candidates"],
        counts[STATUS_MATCHED], counts[STATUS_AMBIGUOUS], counts[STATUS_NO_CANDIDATE],
    )
    return counts
