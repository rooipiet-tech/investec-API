"""Integration tests against a real Postgres.

These are the coverage the unit tests cannot give: that the migrations actually
apply (and apply *once*), that the upsert dedupes against a live unique index,
and that the SQL build-layer views — the most intricate, least-unit-testable
logic in the project — classify and aggregate as intended.

Skipped unless ``TEST_DATABASE_URL`` points at a throwaway Postgres; CI provides
one via a service container (see .github/workflows/tests.yml). Never point it at
the production database — the data-loading tests truncate tables.
"""
from __future__ import annotations

import os
from datetime import date

import pytest

from invespend import db

TEST_DB = os.getenv("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB integration tests"),
]


@pytest.fixture(scope="module")
def initialized_db() -> str:
    with db.connect(TEST_DB) as conn:
        db.init_db(conn)
    return TEST_DB


@pytest.fixture()
def clean_db(initialized_db: str) -> str:
    with db.connect(initialized_db) as conn, conn.cursor() as cur:
        cur.execute("truncate transactions, balances, sync_runs, accounts restart identity cascade;")
    return initialized_db


def _tx(type_, amount, desc, ttype="CardPurchases", d="2026-06-03"):
    return {
        "type": type_, "transactionType": ttype, "status": "POSTED",
        "description": desc, "amount": amount,
        "postingDate": d, "valueDate": d, "actionDate": d, "transactionDate": d,
    }


def test_migrations_apply_exactly_once(initialized_db: str):
    # A second init_db must be a no-op: the tracking table count does not grow
    # and no migration re-runs (idempotency of the whole apply path).
    with db.connect(initialized_db) as conn, conn.cursor() as cur:
        cur.execute("select count(*) from schema_migrations;")
        first = cur.fetchone()[0]
    with db.connect(initialized_db) as conn:
        db.init_db(conn)
    with db.connect(initialized_db) as conn, conn.cursor() as cur:
        cur.execute("select count(*) from schema_migrations;")
        second = cur.fetchone()[0]
        cur.execute("select count(*) from pg_matviews;")  # smoke: catalog reachable
    assert first == second
    # Every migration file is recorded.
    expected = {p.name for p in db.MIGRATIONS_DIR.glob("*.sql")}
    with db.connect(initialized_db) as conn, conn.cursor() as cur:
        cur.execute("select filename from schema_migrations;")
        recorded = {r[0] for r in cur.fetchall()}
    assert expected <= recorded


def test_upsert_dedupes_against_live_index(clean_db: str):
    with db.connect(clean_db) as conn:
        db.upsert_account(conn, {"accountId": "A1", "accountNumber": "10010900709"})
    txs = [_tx("DEBIT", "45.00", "COFFEE SHOP"), _tx("DEBIT", "45.00", "COFFEE SHOP")]
    batch = [(tx, "Dining", seq) for tx, seq in db.assign_day_seq("A1", txs)]
    with db.connect(clean_db) as conn:
        first = db.upsert_transactions(conn, "A1", batch)
    with db.connect(clean_db) as conn:
        second = db.upsert_transactions(conn, "A1", batch)
    assert first == 2  # two identical coffees kept distinct via day_seq
    assert second == 0  # re-pull is idempotent


def test_amounts_round_trip_as_exact_decimal(clean_db: str):
    from decimal import Decimal
    with db.connect(clean_db) as conn:
        db.upsert_account(conn, {"accountId": "A1", "accountNumber": "10010900709"})
        db.upsert_transactions(
            conn, "A1", [(_tx("DEBIT", "1234.56", "BIG SPEND"), "Other", 0)]
        )
    with db.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute("select amount from transactions where description = 'BIG SPEND';")
        amount = cur.fetchone()[0]
    assert amount == Decimal("-1234.56")
    assert isinstance(amount, Decimal)


def test_flow_view_classifies_salary_external_and_transfer_internal(clean_db: str):
    with db.connect(clean_db) as conn:
        db.upsert_account(conn, {"accountId": "A1", "accountNumber": "10010900709",
                                 "accountName": "Mr J P Van Zyl"})
        db.upsert_account(conn, {"accountId": "A2", "accountNumber": "10011234567",
                                 "accountName": "Mr J P Van Zyl"})
        rows = [
            (_tx("CREDIT", "50000.00", "SALARY JUNE MR J P VAN ZYL", "Deposits", "2026-06-01"), "Income", 0),
            (_tx("DEBIT", "1000.00", "Transfer to Savings Mr J P Van Zyl", "Transfers", "2026-06-02"), "Transfers", 0),
        ]
        db.upsert_transactions(conn, "A1", rows)

    with db.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute("select flow_type from transactions_flow where description like 'SALARY%';")
        assert cur.fetchone()[0] == "external_inflow"  # the Tier-B fix
        cur.execute("select flow_type, flow_signal from transactions_flow where description like 'Transfer%';")
        assert cur.fetchone() == ("internal_transfer", "counterparty_name")


def test_sync_health_view_reports_runs(clean_db: str):
    with db.connect(clean_db) as conn:
        rid = db.start_sync_run(conn, date(2026, 6, 1), date(2026, 6, 13))
        db.finish_sync_run(conn, rid, accounts=1, transactions=3, status="success")
    with db.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute("select status, transactions_upserted from sync_health;")
        assert cur.fetchone() == ("success", 3)
