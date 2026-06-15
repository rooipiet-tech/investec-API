"""Tests for --dry-run behaviour across ingest, report and statements commands."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from invespend.cli import build_parser
from invespend.ingest import run_ingest


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------

def test_ingest_dry_run_flag():
    args = build_parser().parse_args(["ingest", "--dry-run"])
    assert args.dry_run is True


def test_ingest_dry_run_default_false():
    args = build_parser().parse_args(["ingest"])
    assert args.dry_run is False


def test_report_dry_run_flag():
    args = build_parser().parse_args(["report", "--dry-run"])
    assert args.dry_run is True


def test_report_dry_run_default_false():
    args = build_parser().parse_args(["report"])
    assert args.dry_run is False


def test_statements_dry_run_flag():
    args = build_parser().parse_args(["statements", "--dry-run"])
    assert args.dry_run is True


def test_statements_dry_run_default_false():
    args = build_parser().parse_args(["statements"])
    assert args.dry_run is False


def test_ingest_dry_run_with_other_flags():
    args = build_parser().parse_args(
        ["ingest", "--dry-run", "--from", "2024-01-01", "--to", "2024-01-31"]
    )
    assert args.dry_run is True
    assert args.from_date == "2024-01-01"


# ---------------------------------------------------------------------------
# run_ingest dry-run skips all DB writes
# ---------------------------------------------------------------------------

def _make_fake_account(account_id="acc1"):
    return {
        "accountId": account_id,
        "accountName": "Cheque",
        "accountNumber": "10000000001",
        "referenceName": "ref",
        "productName": "Private Bank Account",
        "kycCompliant": True,
        "profileId": "p1",
    }


def _make_fake_balance():
    return {
        "currentBalance": 5000.0,
        "availableBalance": 4800.0,
        "budgetBalance": None,
        "straightBalance": None,
        "cashBalance": None,
        "currency": "ZAR",
    }


def _make_fake_transactions():
    return [
        {
            "type": "DEBIT",
            "transactionType": "OnlinePurchase",
            "status": "POSTED",
            "description": "WOOLWORTHS",
            "amount": 250.0,
            "postingDate": "2024-01-15",
            "valueDate": "2024-01-15",
            "actionDate": "2024-01-15",
            "transactionDate": "2024-01-15",
            "runningBalance": 4750.0,
            "cardNumber": "",
        }
    ]


@patch("invespend.ingest.db")
@patch("invespend.ingest.InvestecClient")
def test_dry_run_makes_no_db_calls(mock_client_cls, mock_db):
    """With dry_run=True, no db.connect / write functions are called."""
    txns = _make_fake_transactions()
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.get_accounts.return_value = [_make_fake_account()]
    mock_client.get_balance.return_value = _make_fake_balance()
    mock_client.get_transactions.return_value = txns

    # assign_day_seq and extract_balance_fields are pure helpers on the db module;
    # configure them so the dry-run path can use their return values.
    mock_db.assign_day_seq.return_value = [(txns[0], 0)]
    mock_db.extract_balance_fields.return_value = {
        "current_balance": 5000.0, "currency": "ZAR"
    }

    settings = MagicMock()
    settings.database_url = "postgresql://fake"
    settings.ingest_window_days = 7
    settings.investec_client_id = "id"
    settings.investec_client_secret = "secret"
    settings.investec_api_key = "key"
    settings.investec_base_url = "https://openapi.investec.com"

    summary = run_ingest(
        settings,
        from_date=date(2024, 1, 1),
        to_date=date(2024, 1, 31),
        dry_run=True,
    )

    # No DB connections opened
    mock_db.connect.assert_not_called()
    # DB write helpers not called
    mock_db.init_db.assert_not_called()
    mock_db.upsert_account.assert_not_called()
    mock_db.upsert_balance.assert_not_called()
    mock_db.upsert_transactions.assert_not_called()
    mock_db.start_sync_run.assert_not_called()
    mock_db.finish_sync_run.assert_not_called()

    # API calls still made
    mock_client.get_accounts.assert_called_once()
    mock_client.get_balance.assert_called_once()
    mock_client.get_transactions.assert_called()

    assert summary["dry_run"] is True
    assert summary["accounts"] == 1
    assert summary["balances_captured"] == 1
    assert summary["transactions_upserted"] == 1  # counted but not written


@patch("invespend.ingest.db")
@patch("invespend.ingest.InvestecClient")
def test_dry_run_summary_keys(mock_client_cls, mock_db):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.get_accounts.return_value = []
    mock_client.get_transactions.return_value = []

    settings = MagicMock()
    settings.database_url = "postgresql://fake"
    settings.ingest_window_days = 7
    settings.investec_client_id = "id"
    settings.investec_client_secret = "secret"
    settings.investec_api_key = "key"
    settings.investec_base_url = "https://openapi.investec.com"

    summary = run_ingest(
        settings,
        from_date=date(2024, 1, 1),
        to_date=date(2024, 1, 7),
        dry_run=True,
    )

    assert "dry_run" in summary
    assert "from_date" in summary
    assert "to_date" in summary
    assert "accounts" in summary
    assert "transactions_upserted" in summary
    assert "balances_captured" in summary


# ---------------------------------------------------------------------------
# cmd_report and cmd_statements suppress email in dry-run mode
# ---------------------------------------------------------------------------

@patch("invespend.cli.send_report")
@patch("invespend.cli.generate_weekly_report")
@patch("invespend.cli.Settings")
def test_cmd_report_dry_run_suppresses_email(mock_settings_cls, mock_gen, mock_send, capsys):
    from pathlib import Path
    from invespend.cli import cmd_report
    mock_gen.return_value = (
        Path("/tmp/report.xlsx"),
        {"start": "2024-01-01", "end": "2024-01-07", "rows": 5},
    )

    args = build_parser().parse_args(["report", "--dry-run"])
    cmd_report(args)

    mock_gen.assert_called_once()
    mock_send.assert_not_called()

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "Weekly spend analysis" in out   # subject printed
    assert "report.xlsx" in out             # attachment name printed
    assert "5 transactions" in out          # body content printed


@patch("invespend.cli.send_report")
@patch("invespend.cli.generate_weekly_report")
@patch("invespend.cli.Settings")
def test_cmd_report_dry_run_with_send_still_suppresses(mock_settings_cls, mock_gen, mock_send):
    """--dry-run takes precedence over --send."""
    from pathlib import Path
    from invespend.cli import cmd_report
    mock_gen.return_value = (
        Path("/tmp/report.xlsx"),
        {"start": "2024-01-01", "end": "2024-01-07", "rows": 3},
    )

    args = build_parser().parse_args(["report", "--dry-run", "--send"])
    cmd_report(args)

    mock_send.assert_not_called()


@patch("invespend.cli.send_email")
@patch("invespend.cli.generate_account_statements")
@patch("invespend.cli.Settings")
def test_cmd_statements_dry_run_suppresses_email(mock_settings_cls, mock_gen, mock_send, capsys):
    from pathlib import Path
    from invespend.cli import cmd_statements
    mock_gen.return_value = (
        [Path("/tmp/acc1.xlsx")],
        {
            "full_history": False,
            "start": "2024-01-01",
            "end": "2024-01-31",
            "accounts": 1,
            "per_account": [
                {"account_number": "10000000001", "account_name": "Cheque",
                 "transactions": 5, "start": "2024-01-01", "closing_balance": 5000.0}
            ],
        },
    )

    args = build_parser().parse_args(["statements", "--dry-run"])
    cmd_statements(args)

    mock_gen.assert_called_once()
    mock_send.assert_not_called()

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "Account statements" in out      # subject printed
    assert "acc1.xlsx" in out              # attachment name printed
    assert "10000000001" in out            # body content printed
