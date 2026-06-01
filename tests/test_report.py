import pandas as pd

from invespend.report import build_spend_summary, write_workbook


def _sample_df():
    return pd.DataFrame(
        {
            "posting_date": pd.to_datetime(
                ["2026-05-25", "2026-05-26", "2026-05-26", "2026-05-27"]
            ),
            "account_id": ["A1", "A1", "A1", "A2"],
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
    assert summary["Total spend"] == 484.5      # 200 + 85.5 + 199
    assert summary["Total income"] == 15000.0
    assert summary["Net"] == 15000.0 - 484.5


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


def test_write_workbook(tmp_path):
    sheets = build_spend_summary(_sample_df())
    out = write_workbook(sheets, tmp_path / "report.xlsx")
    assert out.exists() and out.stat().st_size > 0
