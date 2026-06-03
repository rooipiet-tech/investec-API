"""Per-account bank-statement workbooks.

The weekly report (``report.py``) is an *analytical* roll-up of all accounts in
one workbook. This module is the complementary *statement* view: one Excel file
**per account**, each a plain line-by-line transaction listing with a running
balance — exactly like the statement your bank emails you.

The aggregation logic is pure (DataFrame in → statement DataFrame out) so it is
unit tested without a database, mirroring ``report.py``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import db
from .config import Settings

log = logging.getLogger(__name__)

# Order matters: this is the on-statement column layout.
STATEMENT_COLUMNS = ["Date", "Description", "Category", "Debit", "Credit", "Balance"]
_MONEY_COLS = ("Debit", "Credit", "Balance")


@dataclass(frozen=True)
class Account:
    account_id: str
    account_number: str
    account_name: str
    currency: str = "ZAR"


def load_accounts(settings: Settings) -> list[Account]:
    """List every known account, with its latest snapshot currency."""
    query = """
        select a.account_id,
               coalesce(a.account_number, a.account_id) as account_number,
               coalesce(a.account_name, '')             as account_name,
               coalesce(
                   (select b.currency from balances b
                     where b.account_id = a.account_id
                     order by b.as_of_date desc, b.captured_at desc
                     limit 1),
                   'ZAR'
               )                                        as currency
        from accounts a
        order by account_number;
    """
    with db.connect(settings.reporting_db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
    return [Account(*row) for row in rows]


def load_account_transactions(
    settings: Settings, account_id: str, start: date, end: date
) -> pd.DataFrame:
    """Read one account's transactions in [start, end], in statement order.

    Ordered by posting_date then ``day_seq`` so same-day postings keep the API's
    stable order — which is also the order the running balance was computed in.
    """
    query = """
        select t.posting_date, t.description, t.transaction_type, t.type,
               t.amount, t.running_balance, t.category, t.day_seq
        from transactions t
        where t.account_id = %s and t.posting_date between %s and %s
        order by t.posting_date, t.day_seq, t.ingested_at;
    """
    cols = ["posting_date", "description", "transaction_type", "type",
            "amount", "running_balance", "category", "day_seq"]
    with db.connect(settings.reporting_db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (account_id, start, end))
            rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=cols)
    if not df.empty:
        df["amount"] = df["amount"].astype(float)
        df["running_balance"] = df["running_balance"].astype(float)
        df["posting_date"] = pd.to_datetime(df["posting_date"])
    return df


def load_opening_balance(settings: Settings, account_id: str, start: date) -> float | None:
    """The running balance on the last transaction *before* the period.

    This is the statement's opening balance — the carried-forward figure a bank
    prints above the first line. ``None`` when there is no prior balance to
    anchor to (the period contains the account's earliest data).
    """
    query = """
        select t.running_balance
        from transactions t
        where t.account_id = %s and t.posting_date < %s
          and t.running_balance is not null
        order by t.posting_date desc, t.day_seq desc, t.ingested_at desc
        limit 1;
    """
    with db.connect(settings.reporting_db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (account_id, start))
            row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def build_account_statement(
    df: pd.DataFrame, opening_balance: float | None = None
) -> dict:
    """Turn one account's transactions into a bank-statement DataFrame + totals.

    Debits (outflows) and credits (inflows) are split into their own columns and
    a running ``Balance`` is carried down each row. The bank's own
    ``running_balance`` is authoritative and used as-is when present; any gaps
    are filled arithmetically (previous balance + signed amount) so the column is
    always complete and internally consistent.

    Pure (no I/O) so it is unit tested without a database.
    """
    if df.empty:
        return {
            "statement": pd.DataFrame(columns=STATEMENT_COLUMNS),
            "opening_balance": opening_balance,
            "closing_balance": opening_balance,
            "total_debits": 0.0,
            "total_credits": 0.0,
            "count": 0,
        }

    sort_cols = [c for c in ("posting_date", "day_seq") if c in df.columns]
    df = df.sort_values(sort_cols).reset_index(drop=True)

    balances: list[float] = []
    prev = opening_balance if opening_balance is not None else 0.0
    for amount, running in zip(df["amount"], df["running_balance"]):
        if running is not None and not pd.isna(running):
            balance = float(running)
        else:
            balance = round(prev + float(amount), 2)
        balances.append(round(balance, 2))
        prev = balance

    statement = pd.DataFrame(
        {
            "Date": df["posting_date"].dt.date,
            "Description": df["description"].fillna("").astype(str).str.strip(),
            "Category": df.get("category", pd.Series([""] * len(df))).fillna(""),
            "Debit": [round(-a, 2) if a < 0 else None for a in df["amount"]],
            "Credit": [round(a, 2) if a > 0 else None for a in df["amount"]],
            "Balance": balances,
        }
    )

    # If we were not handed an opening balance, infer it by stepping back off the
    # first row so the header reconciles with the first printed line.
    if opening_balance is None:
        opening_balance = round(balances[0] - float(df["amount"].iloc[0]), 2)

    total_debits = round(float(df.loc[df["amount"] < 0, "amount"].abs().sum()), 2)
    total_credits = round(float(df.loc[df["amount"] > 0, "amount"].sum()), 2)

    return {
        "statement": statement,
        "opening_balance": opening_balance,
        "closing_balance": balances[-1],
        "total_debits": total_debits,
        "total_credits": total_credits,
        "count": len(statement),
    }


def _safe_filename(text: str) -> str:
    """Make a string safe for use as a filename component."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip())
    return cleaned.strip("-") or "account"


def write_statement_workbook(
    statement: dict, account: Account, start: date, end: date, path: Path
) -> Path:
    """Write a single-account, bank-statement-style .xlsx.

    Header block (account, period, opening/closing balance, totals) sits above
    the line-by-line transaction table, the way a printed statement reads.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    frame: pd.DataFrame = statement["statement"]

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        sheet_name = "Statement"
        header_rows = 9  # account/period/balances block before the table
        body = frame if not frame.empty else pd.DataFrame(
            {"Date": [], "Description": ["No transactions for this period"],
             "Category": [], "Debit": [], "Credit": [], "Balance": []}
        )
        body = body.reindex(columns=STATEMENT_COLUMNS)
        body.to_excel(writer, sheet_name=sheet_name, index=False, startrow=header_rows)
        ws = writer.sheets[sheet_name]

        ccy = account.currency or "ZAR"
        meta = [
            ("Account statement", ""),
            ("Account name", account.account_name),
            ("Account number", account.account_number),
            ("Period", f"{start.isoformat()} to {end.isoformat()}"),
            ("Currency", ccy),
            ("Opening balance", statement["opening_balance"]),
            ("Money in (credits)", statement["total_credits"]),
            ("Money out (debits)", statement["total_debits"]),
            ("Closing balance", statement["closing_balance"]),
        ]
        for i, (label, value) in enumerate(meta, start=1):
            label_cell = ws.cell(row=i, column=1, value=label)
            value_cell = ws.cell(row=i, column=2, value=value)
            label_cell.font = Font(bold=True)
            if isinstance(value, (int, float)):
                value_cell.number_format = "#,##0.00"
        ws.cell(row=1, column=1).font = Font(bold=True, size=14)

        # Style the transaction-table header row (1-based; +1 for the header itself).
        head_row = header_rows + 1
        fill = PatternFill("solid", fgColor="DDEBF7")
        for col_idx, name in enumerate(STATEMENT_COLUMNS, start=1):
            cell = ws.cell(row=head_row, column=col_idx)
            cell.font = Font(bold=True)
            cell.fill = fill
            cell.alignment = Alignment(horizontal="center")

        # Money formatting + sensible column widths.
        money_cols = {STATEMENT_COLUMNS.index(c) + 1 for c in _MONEY_COLS}
        first_data_row = head_row + 1
        last_data_row = head_row + max(len(body), 1)
        date_col = STATEMENT_COLUMNS.index("Date") + 1
        for col_idx in range(1, len(STATEMENT_COLUMNS) + 1):
            letter = get_column_letter(col_idx)
            fmt = "#,##0.00" if col_idx in money_cols else (
                "yyyy-mm-dd" if col_idx == date_col else None
            )
            if fmt:
                for r in range(first_data_row, last_data_row + 1):
                    ws.cell(row=r, column=col_idx).number_format = fmt
            widths = {1: 12, 2: 42, 3: 16, 4: 14, 5: 14, 6: 16}
            ws.column_dimensions[letter].width = widths.get(col_idx, 14)

        ws.freeze_panes = ws.cell(row=first_data_row, column=1)

    return path


def generate_account_statements(
    settings: Settings,
    end: date | None = None,
    out_dir: Path | None = None,
    days: int = 7,
) -> tuple[list[Path], dict]:
    """Build one bank-statement workbook per account for the trailing window.

    Returns ``(paths, info)``. Accounts with no transactions in the window are
    skipped so the weekly email only carries statements that have activity.
    """
    end = end or date.today()
    start = end - timedelta(days=days - 1)
    out_dir = out_dir or Path("reports")

    paths: list[Path] = []
    per_account: list[dict] = []
    for account in load_accounts(settings):
        df = load_account_transactions(settings, account.account_id, start, end)
        if df.empty:
            log.info("No transactions for %s in window; skipping statement",
                     account.account_number)
            continue
        opening = load_opening_balance(settings, account.account_id, start)
        statement = build_account_statement(df, opening)
        filename = (
            f"statement_{_safe_filename(account.account_number)}_"
            f"{start.isoformat()}_to_{end.isoformat()}.xlsx"
        )
        path = write_statement_workbook(statement, account, start, end, out_dir / filename)
        paths.append(path)
        per_account.append(
            {
                "account_number": account.account_number,
                "account_name": account.account_name,
                "transactions": statement["count"],
                "closing_balance": statement["closing_balance"],
            }
        )
        log.info("Wrote statement %s (%d transactions)", path, statement["count"])

    info = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "accounts": len(paths),
        "per_account": per_account,
    }
    return paths, info
