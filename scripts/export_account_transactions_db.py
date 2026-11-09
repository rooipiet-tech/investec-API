"""Export one account's transactions from the Postgres database to CSV (read-only).

Complements export_account_transactions.py (live API): closed accounts vanish from
the API, but transactions ingested while they were open remain in the database.

Usage (from repo root, with DATABASE_URL set):
    python scripts/export_account_transactions_db.py ACCOUNT_NUMBER OUTPUT.csv

Only SELECTs are run, inside a read-only session. The log shows counts and the
date range only (never full account numbers, names or transaction details).
"""
from __future__ import annotations

import csv
import os
import sys

import psycopg

COLUMNS = [
    "transaction_date", "posting_date", "value_date", "action_date", "type",
    "transaction_type", "status", "description", "card_number", "amount",
    "running_balance", "category", "account_id", "transaction_hash",
]

# Normalise both sides: strip non-alphanumerics and leading zeros.
NORM = "ltrim(regexp_replace(coalesce({0}, ''), '[^A-Za-z0-9]', '', 'g'), '0')"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    number, out_path = argv[1].strip(), argv[2]
    url = os.environ["DATABASE_URL"].strip()

    with psycopg.connect(url, connect_timeout=15, prepare_threshold=None) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("select count(*) from accounts")
            total_accounts = cur.fetchone()[0]
            cur.execute(
                f"select account_id from accounts where {NORM.format('account_number')} "
                f"= {NORM.format('%s')}",
                (number,),
            )
            ids = [r[0] for r in cur.fetchall()]
            if not ids:
                print(
                    f"Account {number} not found in the database's accounts table "
                    f"({total_accounts} account(s) stored). Nothing to export.",
                    file=sys.stderr,
                )
                return 1
            print(f"Found {len(ids)} matching account record(s) in the database.")
            cur.execute(
                f"select {', '.join(COLUMNS)} from transactions "
                "where account_id = any(%s) "
                "order by coalesce(transaction_date, posting_date) desc, posting_date desc",
                (ids,),
            )
            rows = cur.fetchall()

    if not rows:
        print("Account exists in the database but has 0 stored transactions.", file=sys.stderr)
        return 1

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        w.writerows(rows)
    dates = [r[1] for r in rows if r[1]]
    rng = f"{min(dates)} to {max(dates)}" if dates else "n/a"
    print(f"Wrote {len(rows)} transactions (posting dates {rng}) to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
