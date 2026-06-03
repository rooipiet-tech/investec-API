import pandas as pd

from invespend.report import build_spend_summary, write_workbook


def _sample_df():
    return pd.DataFrame(
        {
            "posting_date": pd.to_datetime(
                ["2026-05-25", "2026-05-26", "2026-05-26", "2026-05-27"]
            ),
            "account_number": ["10010000001", "10010000001", "10010000001", "10010000002"],
            "account_name": ["Acct One", "Acct One", "Acct One", "Acct Two"],
            "type": ["DEBIT", "DEBIT", "CREDIT", "DEBIT"],
            "transaction_type": ["CardPurchases"] * 4,
            "description": ["Woolworths", "Uber", "Salary", "Netflix"],
            "amount": [-200.0, -85.5, 15000.0, -199.0],
            "category": ["Groceries", "Transport", "Income", "Subscriptions"],
        }
    )


def test_summary_totals():
    sheets = build_spend_summary(_sample_df())
    summary = sheets["Summary"].set_index("Metric")["Value"]
    assert summary["Money out (excl. transfers)"] == 484.5      # 200 + 85.5 + 199
    assert summary["Money in (excl. transfers)"] == 15000.0
    assert summary["Net"] == 15000.0 - 484.5


def test_transfers_excluded_from_spend_and_income():
    df = _sample_df()
    df.loc[len(df)] = {
        "posting_date": pd.Timestamp("2026-05-28"),
        "account_number": "10010000001", "account_name": "Acct One",
        "type": "DEBIT", "transaction_type": "Transfer",
        "description": "Transfer to savings", "amount": -1000.0,
        "category": "Transfers",
    }
    summary = build_spend_summary(df)["Summary"].set_index("Metric")["Value"]
    # The transfer is excluded from money out and reported separately.
    assert summary["Money out (excl. transfers)"] == 484.5
    assert summary["Transfers out"] == 1000.0
    # ...and it does not appear as a spend category.
    by_cat = build_spend_summary(df)["By Category"]
    assert "Transfers" not in set(by_cat["category"])


def test_by_category_sorted_desc():
    by_cat = build_spend_summary(_sample_df())["By Category"]
    assert list(by_cat["category"])[0] == "Groceries"  # 200 is the largest spend
    assert "Income" not in set(by_cat["category"])      # income is not spend


def test_empty_df_produces_all_sheets():
    sheets = build_spend_summary(pd.DataFrame())
    assert set(sheets) == {
        "Summary", "By Category", "By Account",
        "Top Merchants", "Daily Trend", "Transactions",
    }


def test_by_account_uses_account_number(tmp_path):
    by_acct = build_spend_summary(_sample_df())["By Account"]
    assert "account_number" in by_acct.columns
    assert "account_id" not in by_acct.columns
    # Acct One has the most spend (200 + 85.5) and sorts first.
    assert list(by_acct["account_number"])[0] == "10010000001"


def test_write_workbook(tmp_path):
    sheets = build_spend_summary(_sample_df())
    out = write_workbook(sheets, tmp_path / "report.xlsx")
    assert out.exists() and out.stat().st_size > 0
