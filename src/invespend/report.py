"""Weekly Excel spend-analysis builder.

The aggregation logic is pure (DataFrame in → DataFrames out) so it is unit
tested without a database.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from openpyxl.styles import Font

from . import MONEY_FORMAT, db
from .config import Settings

log = logging.getLogger(__name__)

COLUMNS = ["effective_date", "account_number", "account_name", "type",
           "transaction_type", "description", "amount", "category", "flow_type"]


def load_transactions(settings: Settings, start: date, end: date) -> pd.DataFrame:
    """Read transactions in [start, end] into a DataFrame.

    Windows on ``effective_date`` (when the transaction economically happened —
    transaction/action/value date, falling back to posting date) rather than the
    bank's posting date, which can lag by months and would otherwise sweep a
    backlog of late-posted items into a single week. Reads the build-layer
    ``transactions_flow`` view so each row carries its ``flow_type``, and joins
    accounts for the human-readable account number (and name).
    """
    query = """
        select f.effective_date,
               coalesce(a.account_number, f.account_id) as account_number,
               coalesce(a.account_name, '')             as account_name,
               f.type, f.transaction_type, f.description, f.amount,
               f.category, f.flow_type
        from transactions_flow f
        left join accounts a on a.account_id = f.account_id
        where f.effective_date between %s and %s
        order by f.effective_date;
    """
    with db.connect(settings.reporting_db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (start, end))
            rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=COLUMNS)
    if not df.empty:
        df["amount"] = df["amount"].astype(float)
        df["effective_date"] = pd.to_datetime(df["effective_date"])
    return df


def load_monthly_movement(settings: Settings, months: int = 6) -> pd.DataFrame:
    """Read the month-over-month movement bridge from the build-layer view.

    Sourced from `monthly_movement` (not the 7-day window) so the report can show
    a real month-over-month trend per category.
    """
    query = """
        select month, category, total_spend, prev_month_spend, movement
        from monthly_movement
        where month >= (date_trunc('month', current_date) - %s::interval)
        order by month desc, total_spend desc;
    """
    with db.connect(settings.reporting_db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (f"{months} months",))
            rows = cur.fetchall()
    cols = ["month", "category", "total_spend", "prev_month_spend", "movement"]
    df = pd.DataFrame(rows, columns=cols)
    for col in ("total_spend", "prev_month_spend", "movement"):
        if not df.empty:
            df[col] = df[col].astype(float)
    return df


def load_reconciliation(settings: Settings) -> pd.DataFrame:
    """Read the bank-balance-vs-transactions reconciliation from the view."""
    query = """
        select account_id, as_of_date, api_current_balance,
               latest_txn_date, latest_txn_running_balance, difference, reconciled
        from balance_reconciliation
        order by account_id;
    """
    cols = ["account_id", "as_of_date", "api_current_balance", "latest_txn_date",
            "latest_txn_running_balance", "difference", "reconciled"]
    with db.connect(settings.reporting_db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def build_spend_summary(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Turn a transaction DataFrame into the report's sheets.

    Spend = external outflows (negative amounts). Income = external inflows
    (positive amounts). Internal transfers — money moving between your own
    accounts — are excluded from every spend/income figure so they are not
    double-counted as both an outflow and an inflow; they get their own sheet.
    """
    if df.empty:
        empty = pd.DataFrame()
        return {"Summary": empty, "By Category": empty, "By Account": empty,
                "Top Merchants": empty, "Daily Trend": empty,
                "Internal Transfers": empty, "Transactions": empty}

    # External flows only drive the spend/income analysis. Tolerate a frame
    # without flow_type (e.g. legacy callers/tests) by treating all as external.
    if "flow_type" in df.columns:
        external = df[df["flow_type"] != "internal_transfer"]
        internal = df[df["flow_type"] == "internal_transfer"]
    else:
        external = df
        internal = df.iloc[0:0]

    spend = external[external["amount"] < 0].copy()
    spend["spend"] = spend["amount"].abs()
    income = external[external["amount"] > 0]["amount"].sum()

    summary = pd.DataFrame(
        {
            "Metric": [
                "Total spend", "Total income", "Net", "Transactions", "Categories",
                "Internal transfers (excluded)", "Internal transfer volume",
            ],
            "Value": [
                round(spend["spend"].sum(), 2),
                round(float(income), 2),
                round(float(external["amount"].sum()), 2),
                len(df),
                spend["category"].nunique(),
                len(internal),
                round(float(internal[internal["amount"] > 0]["amount"].sum()), 2),
            ],
        }
    )

    by_category = (
        spend.groupby("category")["spend"].agg(["sum", "count"])
        .rename(columns={"sum": "total_spend", "count": "transactions"})
        .sort_values("total_spend", ascending=False)
        .reset_index()
    )

    by_account = (
        spend.groupby(["account_number", "account_name"])["spend"].sum()
        .rename("total_spend").sort_values(ascending=False).reset_index()
    )

    top_merchants = (
        spend.groupby("description")["spend"].sum()
        .rename("total_spend").sort_values(ascending=False).head(15).reset_index()
    )

    daily = (
        spend.groupby(spend["effective_date"].dt.date)["spend"].sum()
        .rename("total_spend").reset_index().rename(columns={"effective_date": "date"})
    )

    internal_sheet = internal.sort_values("effective_date").reset_index(drop=True)
    transactions = df.sort_values("effective_date").reset_index(drop=True)

    return {
        "Summary": summary,
        "By Category": by_category,
        "By Account": by_account,
        "Top Merchants": top_merchants,
        "Daily Trend": daily,
        "Internal Transfers": internal_sheet,
        "Transactions": transactions,
    }


def write_workbook(sheets: dict[str, pd.DataFrame], path: Path) -> Path:
    """Write the sheets to a formatted .xlsx workbook."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            out = frame if not frame.empty else pd.DataFrame({"info": ["No data for this period"]})
            out.to_excel(writer, sheet_name=name[:31], index=False)
            ws = writer.sheets[name[:31]]
            # Bold header + auto-ish column widths.
            for col_cells in ws.columns:
                width = max((len(str(c.value)) for c in col_cells if c.value is not None), default=10)
                ws.column_dimensions[col_cells[0].column_letter].width = min(width + 2, 50)
            for cell in ws[1]:
                cell.font = Font(bold=True)
            # Accounting format on every numeric cell (skip the header row and
            # booleans like the reconciliation flag, which would show as 1.00).
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                        cell.number_format = MONEY_FORMAT
    return path


def generate_weekly_report(settings: Settings, end: date | None = None,
                           out_dir: Path | None = None) -> tuple[Path, dict]:
    """Build the workbook for the 7 days ending on ``end`` (default: today)."""
    end = end or date.today()
    start = end - timedelta(days=6)
    out_dir = out_dir or Path("reports")

    df = load_transactions(settings, start, end)
    sheets = build_spend_summary(df)
    # Month-over-month trend comes from the build-layer view, which spans more
    # than the 7-day window the rest of the sheets are built from.
    try:
        sheets["Monthly Movement"] = load_monthly_movement(settings)
    except Exception as exc:  # noqa: BLE001 - report still useful without MoM
        log.warning("Skipping Monthly Movement sheet: %s", exc)
    try:
        sheets["Reconciliation"] = load_reconciliation(settings)
    except Exception as exc:  # noqa: BLE001 - report still useful without it
        log.warning("Skipping Reconciliation sheet: %s", exc)
    filename = f"spend-analysis_{start.isoformat()}_to_{end.isoformat()}.xlsx"
    path = write_workbook(sheets, out_dir / filename)
    log.info("Wrote report %s (%d transactions)", path, len(df))
    return path, {"start": start.isoformat(), "end": end.isoformat(), "rows": len(df)}
