import pandas as pd

from invespend.report import (
    build_spend_summary,
    summarize_sync_health,
    write_workbook,
)


def _sample_df():
    return pd.DataFrame(
        {
            "effective_date": pd.to_datetime(
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
        "Summary", "By Category", "By Account", "Top Merchants",
        "Daily Trend", "Internal Transfers", "Transactions",
    }


def _flow_df():
    """Two external rows plus a matched internal-transfer pair (debit + credit)."""
    return pd.DataFrame(
        {
            "effective_date": pd.to_datetime(
                ["2026-05-25", "2026-05-26", "2026-05-27", "2026-05-27"]
            ),
            "account_number": ["10010000001"] * 3 + ["10010000002"],
            "account_name": ["Acct One"] * 3 + ["Acct Two"],
            "type": ["DEBIT", "CREDIT", "DEBIT", "CREDIT"],
            "transaction_type": ["CardPurchases"] * 4,
            "description": ["Woolworths", "Salary", "Transfer to Acct Two", "Transfer from Acct One"],
            "amount": [-200.0, 15000.0, -5000.0, 5000.0],
            "category": ["Groceries", "Income", "Transfers", "Transfers"],
            "flow_type": ["external_outflow", "external_inflow",
                          "internal_transfer", "internal_transfer"],
        }
    )


def test_internal_transfers_excluded_from_spend_and_income():
    sheets = build_spend_summary(_flow_df())
    summary = sheets["Summary"].set_index("Metric")["Value"]
    # The R5000 internal transfer legs count toward neither spend nor income.
    assert summary["Total spend"] == 200.0
    assert summary["Total income"] == 15000.0
    assert summary["Internal transfers (excluded)"] == 2
    # ...and they are surfaced on their own sheet, not in By Category.
    assert len(sheets["Internal Transfers"]) == 2
    assert "Transfers" not in set(sheets["By Category"]["category"])


def test_by_account_uses_account_number(tmp_path):
    by_acct = build_spend_summary(_sample_df())["By Account"]
    assert "account_number" in by_acct.columns
    assert "account_id" not in by_acct.columns
    # Acct One has the most spend (200 + 85.5) and sorts first.
    assert list(by_acct["account_number"])[0] == "10010000001"


def test_sync_health_summary_flags_staleness():
    now = pd.Timestamp.now(tz="UTC")
    df = pd.DataFrame({
        "started_at": [now, now - pd.Timedelta(days=3), now - pd.Timedelta(days=4)],
        "status": ["error", "error", "success"],
        "accounts_synced": [0, 0, 2],
        "transactions_upserted": [0, 0, 10],
        "error": ["boom", "boom", None],
    })
    summary = summarize_sync_health(df).set_index("Metric")["Value"]
    # Latest run failed; last success was 4 days ago; two failures in the window.
    assert summary["Last sync status"] == "error"
    assert summary["Days since last success"] == 4
    assert summary["Failed runs (recent window)"] == 2


def test_sync_health_summary_handles_no_runs():
    summary = summarize_sync_health(pd.DataFrame())
    assert summary.iloc[0]["Value"] == "none recorded"


def test_sync_health_summary_never_succeeded():
    now = pd.Timestamp.now(tz="UTC")
    df = pd.DataFrame({
        "started_at": [now], "status": ["error"],
        "accounts_synced": [0], "transactions_upserted": [0], "error": ["x"],
    })
    summary = summarize_sync_health(df).set_index("Metric")["Value"]
    assert summary["Last successful sync (UTC)"] == "never"
    assert summary["Days since last success"] == "n/a"


def test_write_workbook(tmp_path):
    sheets = build_spend_summary(_sample_df())
    out = write_workbook(sheets, tmp_path / "report.xlsx")
    assert out.exists() and out.stat().st_size > 0
