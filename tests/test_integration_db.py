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


def test_daily_repull_with_advancing_action_date_does_not_duplicate(clean_db: str):
    # Regression for the production bug: Investec returns actionDate as the fetch
    # date, so the rolling daily window re-fetched each transaction with a new
    # actionDate and re-inserted it every day. Keyed on postedOrder it dedupes.
    with db.connect(clean_db) as conn:
        db.upsert_account(conn, {"accountId": "A1", "accountNumber": "10010900709"})
    day1 = {**_tx("DEBIT", "2400.00", "BUSSIE UB40", d="2026-06-08"), "postedOrder": 10540}
    day2 = {**day1, "actionDate": "2026-06-14"}  # next day's pull, actionDate advanced
    with db.connect(clean_db) as conn:
        first = db.upsert_transactions(conn, "A1", [(day1, "Transport", 0)])
    with db.connect(clean_db) as conn:
        second = db.upsert_transactions(conn, "A1", [(day2, "Transport", 0)])
    assert first == 1 and second == 0
    with db.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute("select count(*) from transactions where description = 'BUSSIE UB40';")
        assert cur.fetchone()[0] == 1


def test_distinct_posted_orders_same_day_are_all_kept(clean_db: str):
    # The legitimate month-end fees: same day/amount/description, but each a real
    # separate transaction with its own postedOrder — none must be lost.
    with db.connect(clean_db) as conn:
        db.upsert_account(conn, {"accountId": "A1", "accountNumber": "100"})
        rows = [
            ({**_tx("DEBIT", "6.00", "ELECTRONIC DEBIT FEE", d="2022-10-31"),
              "postedOrder": po}, "Fees", 0)
            for po in (9623, 9621, 9620)
        ]
        inserted = db.upsert_transactions(conn, "A1", rows)
    assert inserted == 3


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


def test_category_map_is_synced_from_python_rules(initialized_db: str):
    from invespend.categorize import category_map_rows
    expected = {(kw, cat) for kw, cat, _ in category_map_rows()}
    with db.connect(initialized_db) as conn, conn.cursor() as cur:
        cur.execute("select keyword, category from category_map;")
        actual = set(cur.fetchall())
    assert actual == expected  # table is an exact projection of the rules


def test_init_db_prunes_stray_category_keywords(initialized_db: str):
    # A keyword not in the Python rules must be removed on the next init_db,
    # so the table can never drift from the single source of truth.
    with db.connect(initialized_db) as conn, conn.cursor() as cur:
        cur.execute(
            "insert into category_map (keyword, category, priority) "
            "values ('zzz-not-a-rule', 'Bogus', 9999) on conflict do nothing;"
        )
    with db.connect(initialized_db) as conn:
        db.init_db(conn)
    with db.connect(initialized_db) as conn, conn.cursor() as cur:
        cur.execute("select count(*) from category_map where keyword = 'zzz-not-a-rule';")
        assert cur.fetchone()[0] == 0


def test_categorized_view_matches_python(clean_db: str):
    from invespend.categorize import categorize
    with db.connect(clean_db) as conn:
        db.upsert_account(conn, {"accountId": "A1", "accountNumber": "10010900709"})
        db.upsert_transactions(conn, "A1", [
            (_tx("DEBIT", "200.00", "WOOLWORTHS SANDTON"), "ignored-at-ingest", 0),
            (_tx("DEBIT", "85.00", "Uber trip"), "ignored-at-ingest", 0),
        ])
    with db.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute("select description, category from transactions_categorized order by description;")
        for desc, category in cur.fetchall():
            assert category == categorize(desc)  # view agrees with the engine


def test_sync_health_view_reports_runs(clean_db: str):
    with db.connect(clean_db) as conn:
        rid = db.start_sync_run(conn, date(2026, 6, 1), date(2026, 6, 13))
        db.finish_sync_run(conn, rid, accounts=1, transactions=3, status="success")
    with db.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute("select status, transactions_upserted from sync_health;")
        assert cur.fetchone() == ("success", 3)
