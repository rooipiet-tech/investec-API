"""Confirm a list of expected deposits against the ingested transactions table.

Usage (from repo root, with DATABASE_URL set):
    python scripts/confirm_deposits.py

This is a read-only lookup. It searches the `transactions` table for each
expected deposit (a CREDIT of a given amount, on/around a given date, into the
account whose number ends in the configured suffix) and prints MATCH / NO MATCH
for each. It changes nothing in the database.

The default expected set below is the Discovery Life "Health Integrator PayBack"
confirmation for account ...0709. Edit ACCOUNT_SUFFIX / EXPECTED to reuse it.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

# Make sure the invespend package is importable when run from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from invespend.db import connect

# Match transactions in the account whose account_number ends with this suffix.
ACCOUNT_SUFFIX = "0709"

# How many days either side of the stated release date to accept (postings can
# settle a day or two late, and the confirmation shows a "release" date).
DATE_TOLERANCE_DAYS = 4

# (label, amount in Rands, payment release date)
EXPECTED = [
    ("Health Integrator PayBack", Decimal("10832.90"), date(2025, 7, 1)),
    ("Health Integrator PayBack", Decimal("9232.79"), date(2024, 7, 1)),
    ("Health Integrator PayBack", Decimal("7774.35"), date(2023, 7, 3)),
    ("Health Integrator PayBack", Decimal("6376.71"), date(2022, 7, 1)),
]

SQL = """
    select t.transaction_date, t.posting_date, t.value_date,
           t.amount, t.type, t.description, t.running_balance
    from transactions t
    join accounts a on a.account_id = t.account_id
    where a.account_number like %(suffix)s
      and t.amount = %(amount)s
      and coalesce(t.value_date, t.posting_date, t.transaction_date)
          between %(lo)s and %(hi)s
    order by coalesce(t.value_date, t.posting_date, t.transaction_date)
"""


def main() -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set. Export it (or load your .env) first.")
        return 2

    found = 0
    with connect(database_url) as conn:
        for label, amount, when in EXPECTED:
            lo = when - timedelta(days=DATE_TOLERANCE_DAYS)
            hi = when + timedelta(days=DATE_TOLERANCE_DAYS)
            with conn.cursor() as cur:
                cur.execute(
                    SQL,
                    {
                        "suffix": f"%{ACCOUNT_SUFFIX}",
                        # Deposits are CREDITs and stored as positive amounts.
                        "amount": amount,
                        "lo": lo,
                        "hi": hi,
                    },
                )
                rows = cur.fetchall()

            header = f"R{amount:,.2f} on {when:%d/%m/%Y}  ({label})"
            if rows:
                found += 1
                print(f"[MATCH]    {header}")
                for d, p, v, amt, typ, desc, bal in rows:
                    eff = v or p or d
                    print(f"             ↳ {eff}  {typ}  R{amt:,.2f}  {desc or ''}".rstrip())
            else:
                print(f"[NO MATCH] {header}")

    print(f"\n{found}/{len(EXPECTED)} expected deposits found in account ...{ACCOUNT_SUFFIX}.")
    return 0 if found == len(EXPECTED) else 1


if __name__ == "__main__":
    raise SystemExit(main())
