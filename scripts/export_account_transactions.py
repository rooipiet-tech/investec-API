"""Export every transaction for ONE account straight from the Investec API to CSV.

Read-only: uses the existing client's account/transaction reads; nothing is
written to the database and no application code is changed.

Usage (from repo root, with INVESTEC_* env vars set):
    python scripts/export_account_transactions.py ACCOUNT_NUMBER OUTPUT.csv [FROM_DATE]

FROM_DATE (YYYY-MM-DD) defaults to 25 years ago (i.e. full history). Windows are
walked newest-first in 90-day chunks, stopping after 3 consecutive empty windows,
mirroring the ingest backfill.
"""
from __future__ import annotations

import csv
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from invespend.investec_client import InvestecClient

CHUNK_DAYS = 90
MAX_EMPTY_CHUNKS = 3


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    account_number, out_path = argv[1].strip(), argv[2]
    today = date.today()
    from_date = date.fromisoformat(argv[3]) if len(argv) > 3 and argv[3] else today - timedelta(days=365 * 25)

    client = InvestecClient(
        client_id=os.environ["INVESTEC_CLIENT_ID"].strip(),
        client_secret=os.environ["INVESTEC_CLIENT_SECRET"].strip(),
        api_key=os.environ["INVESTEC_API_KEY"].strip(),
        base_url=(os.environ.get("INVESTEC_BASE_URL") or "https://openapi.investec.com").strip(),
    )

    matches = [a for a in client.get_accounts() if str(a.get("accountNumber", "")).strip() == account_number]
    if not matches:
        print(f"Account {account_number} not found among the credential's accounts.", file=sys.stderr)
        return 1
    account = matches[0]
    account_id = account["accountId"]
    print(f"Found account {account_number} ({account.get('accountName')}, {account.get('productName')})")

    rows: dict[tuple, dict] = {}
    empty = 0
    end = today
    while end >= from_date:
        start = max(from_date, end - timedelta(days=CHUNK_DAYS - 1))
        txs = client.get_transactions(account_id, start, end)
        print(f"{start}..{end}: {len(txs)} transactions")
        empty = 0 if txs else empty + 1
        for t in txs:
            key = tuple(sorted((k, str(v)) for k, v in t.items()))
            rows[key] = t
        if empty >= MAX_EMPTY_CHUNKS:
            break
        end = start - timedelta(days=1)

    ordered = sorted(
        rows.values(),
        key=lambda t: (t.get("transactionDate") or "", t.get("postingDate") or ""),
        reverse=True,
    )
    fields: list[str] = []
    for t in ordered:
        for k in t:
            if k not in fields:
                fields.append(k)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(ordered)
    print(f"Wrote {len(ordered)} transactions to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
