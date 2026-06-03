import pandas as pd
from openpyxl import load_workbook

from invespend.statements import (
    Account,
    build_account_statement,
    write_statement_workbook,
    _safe_filename,
)


def _sample_df():
    # Same-day pair carries running_balance; one row has a NULL balance to
    # exercise the arithmetic gap-fill.
    return pd.DataFrame(
        {
            "posting_date": pd.to_datetime(
                ["2026-05-25", "2026-05-26", "2026-05-26", "2026-05-27"]
            ),
            "description": ["Woolworths", "Uber", "Salary", "Netflix"],
            "transaction_type": ["CardPurchases"] * 4,
            "type": ["DEBIT", "DEBIT", "CREDIT", "DEBIT"],
            "amount": [-200.0, -85.5, 15000.0, -199.0],
            "running_balance": [800.0, 714.5, 15714.5, None],
            "category": ["Groceries", "Transport", "Income", "Subscriptions"],
            "day_seq": [0, 0, 1, 0],
        }
    )


def test_debit_credit_split():
    stmt = build_account_statement(_sample_df(), opening_balance=1000.0)["statement"]
    # Debits populate Debit (positive), leave Credit empty, and vice versa.
    assert stmt.loc[0, "Debit"] == 200.0 and pd.isna(stmt.loc[0, "Credit"])
    assert stmt.loc[2, "Credit"] == 15000.0 and pd.isna(stmt.loc[2, "Debit"])


def test_uses_bank_running_balance_and_fills_gaps():
    result = build_account_statement(_sample_df(), opening_balance=1000.0)
    stmt = result["statement"]
    # Stored running balances are used verbatim where present...
    assert list(stmt["Balance"])[:3] == [800.0, 714.5, 15714.5]
    # ...and the NULL final balance is filled: 15714.5 + (-199.0).
    assert stmt.loc[3, "Balance"] == 15515.5
    assert result["closing_balance"] == 15515.5


def test_totals_and_opening_passthrough():
    result = build_account_statement(_sample_df(), opening_balance=1000.0)
    assert result["opening_balance"] == 1000.0
    assert result["total_debits"] == 484.5      # 200 + 85.5 + 199
    assert result["total_credits"] == 15000.0
    assert result["count"] == 4


def test_opening_balance_inferred_when_absent():
    # No opening handed in: infer it by stepping back off the first row.
    result = build_account_statement(_sample_df(), opening_balance=None)
    # First stored balance 800 minus the first amount (-200) => 1000.
    assert result["opening_balance"] == 1000.0


def test_empty_df_is_safe():
    result = build_account_statement(pd.DataFrame())
    assert result["count"] == 0
    assert result["total_debits"] == 0.0
    assert list(result["statement"].columns) == [
        "Date", "Description", "Category", "Debit", "Credit", "Balance",
    ]


def test_write_statement_workbook(tmp_path):
    import datetime as dt

    result = build_account_statement(_sample_df(), opening_balance=1000.0)
    account = Account("acc-1", "10010000001", "Primary Cheque", "ZAR")
    out = write_statement_workbook(
        result, account, dt.date(2026, 5, 25), dt.date(2026, 5, 31),
        tmp_path / "stmt.xlsx",
    )
    assert out.exists() and out.stat().st_size > 0

    wb = load_workbook(out)
    ws = wb["Statement"]
    # Header block carries the account number and the closing balance.
    header = {ws.cell(row=r, column=1).value: ws.cell(row=r, column=2).value
              for r in range(1, 10)}
    assert header["Account number"] == "10010000001"
    assert header["Closing balance"] == 15515.5
    # The transaction table header is present below the metadata block.
    assert ws.cell(row=10, column=1).value == "Date"
    assert ws.cell(row=10, column=6).value == "Balance"


def test_safe_filename():
    assert _safe_filename("1001 0000/001") == "1001-0000-001"
    assert _safe_filename("   ") == "account"
