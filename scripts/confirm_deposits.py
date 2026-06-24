"""Confirm a list of expected deposits against the ingested transactions table.

Usage (from repo root, with DATABASE_URL set):
    python scripts/confirm_deposits.py

This is a read-only lookup. For each expected deposit it searches the
`transactions` table for a CREDIT whose amount matches exactly AND whose
description looks like a Discovery health payment, in the account whose number
ends in the configured suffix. It prints MATCH / NO MATCH per deposit and lists
the matching rows (with their dates) so you can eyeball timing. It changes
nothing in the database.

The default expected set below is the Discovery Life "Health Integrator PayBack"
confirmation for account ...0709. Edit ACCOUNT_SUFFIX / EXPECTED / DESC_TERMS to
reuse it.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

# Make sure the invespend package is importable when run from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from invespend.db import connect

# Match transactions in the account whose account_number ends with this suffix.
ACCOUNT_SUFFIX = "0709"

# Description must contain at least one of these (case-insensitive) to count as a
# Discovery health payment. Kept broad so wording variants still match.
DESC_TERMS = ["discovery", "health integrator", "payback", "integrator"]

# (label, amount in Rands, payment release date — date is informational only)
EXPECTED = [
    ("Health Integrator PayBack", Decimal("10832.90"), date(2025, 7, 1)),
    ("Health Integrator PayBack", Decimal("9232.79"), date(2024, 7, 1)),
    ("Health Integrator PayBack", Decimal("7774.35"), date(2023, 7, 3)),
    ("Health Integrator PayBack", Decimal("6376.71"), date(2022, 7, 1)),
]

# Amount match is exact; description must match any Discovery term. Date is not
# gated — it's selected only so the output can show it for a sanity check.
SQL = """
    select t.transaction_date, t.posting_date, t.value_date,
           t.amount, t.type, t.description, t.running_balance
    from transactions t
    join accounts a on a.account_id = t.account_id
    where a.account_number like %(suffix)s
      and t.amount = %(amount)s
      and t.description ilike any(%(terms)s)
    order by coalesce(t.value_date, t.posting_date, t.transaction_date)
"""


def main() -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set. Export it (or load your .env) first.")
        return 2

    # Wrap each term in %…% so ILIKE ANY does a substring match.
    terms = [f"%{t}%" for t in DESC_TERMS]

    found = 0
    with connect(database_url) as conn:
        for label, amount, when in EXPECTED:
            with conn.cursor() as cur:
                cur.execute(
                    SQL,
                    {
                        "suffix": f"%{ACCOUNT_SUFFIX}",
                        # Deposits are CREDITs and stored as positive amounts.
                        "amount": amount,
                        "terms": terms,
                    },
                )
                rows = cur.fetchall()

            header = f"R{amount:,.2f}  (expected {when:%d/%m/%Y}, {label})"
            if rows:
                found += 1
                print(f"[MATCH]    {header}")
                for d, p, v, amt, typ, desc, bal in rows:
                    eff = v or p or d
                    print(f"             ↳ {eff}  {typ}  R{amt:,.2f}  {desc or ''}".rstrip())
            else:
                print(f"[NO MATCH] {header}")

    print(
        f"\n{found}/{len(EXPECTED)} expected deposits found in account "
        f"...{ACCOUNT_SUFFIX} (matched on amount + Discovery description)."
    )
    return 0 if found == len(EXPECTED) else 1


if __name__ == "__main__":
    raise SystemExit(main())
