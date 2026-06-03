"""Command-line entry point: invespend init-db | ingest | report."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from . import db
from .backup import run_backup
from .config import Settings
from .emailer import send_email, send_report
from .ingest import run_ingest
from .report import generate_weekly_report
from .statements import generate_account_statements


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def cmd_init_db(_args: argparse.Namespace) -> int:
    settings = Settings.load()
    with db.connect(settings.database_url) as conn:
        db.init_db(conn)
    print("Schema applied.")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    settings = Settings.load()
    from_date = date.fromisoformat(args.from_date) if args.from_date else None
    to_date = date.fromisoformat(args.to_date) if args.to_date else None
    summary = run_ingest(
        settings, window_days=args.days, from_date=from_date, to_date=to_date,
        resume=args.resume, full=args.full,
    )
    print(f"Ingest: {summary}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    settings = Settings.load()
    path, info = generate_weekly_report(settings)
    print(f"Report written: {path} ({info})")
    if args.send:
        subject = f"Weekly spend analysis: {info['start']} to {info['end']}"
        body = (
            "Hi,\n\nAttached is your Investec weekly spend analysis for "
            f"{info['start']} to {info['end']} ({info['rows']} transactions).\n\n"
            "— invespend"
        )
        send_report(settings, path, subject, body)
        print("Report emailed.")
    return 0


def cmd_statements(args: argparse.Namespace) -> int:
    settings = Settings.load()
    paths, info = generate_account_statements(settings, days=args.days)
    print(f"Statements written: {len(paths)} file(s) ({info})")
    if not paths:
        print("No accounts had transactions in the window; nothing to email.")
        return 0
    if args.send:
        subject = (
            f"Weekly account statements: {info['start']} to {info['end']} "
            f"({info['accounts']} account(s))"
        )
        lines = [
            f"  • {a['account_number']} ({a['account_name']}): "
            f"{a['transactions']} txns, closing balance {a['closing_balance']}"
            for a in info["per_account"]
        ]
        body = (
            "Hi,\n\nAttached are your Investec per-account statements for "
            f"{info['start']} to {info['end']} — one Excel file per account, "
            "each a full transaction listing with running balance:\n\n"
            + "\n".join(lines)
            + "\n\n— invespend"
        )
        send_email(settings, paths, subject, body)
        print("Statements emailed.")
    return 0


def cmd_backup(_args: argparse.Namespace) -> int:
    settings = Settings.load()
    path = run_backup(settings)
    print(f"Backup written: {path}")
    return 0


def cmd_backfill_hashes(_args: argparse.Namespace) -> int:
    settings = Settings.load()
    with db.connect(settings.database_url) as conn:
        summary = db.backfill_transaction_hashes(conn)
    print(f"Backfill: {summary}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invespend", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Apply the database schema").set_defaults(func=cmd_init_db)

    p_ingest = sub.add_parser("ingest", help="Pull transactions into the database")
    p_ingest.add_argument("--days", type=int, default=None,
                          help="Days of history to pull (default: INGEST_WINDOW_DAYS)")
    p_ingest.add_argument("--from", dest="from_date", default=None,
                          help="Backfill start date YYYY-MM-DD (overrides --days)")
    p_ingest.add_argument("--to", dest="to_date", default=None,
                          help="End date YYYY-MM-DD (default: today)")
    p_ingest.add_argument("--resume", action="store_true",
                          help="Backfill: continue older than the oldest stored "
                               "transaction instead of re-pulling recent windows")
    p_ingest.add_argument("--full", action="store_true",
                          help="Backfill: walk every window down to --from without "
                               "the empty-window early-stop (exhaustive scan)")
    p_ingest.set_defaults(func=cmd_ingest)

    p_report = sub.add_parser("report", help="Build the weekly Excel report")
    p_report.add_argument("--send", action="store_true", help="Email the report")
    p_report.set_defaults(func=cmd_report)

    p_stmts = sub.add_parser(
        "statements",
        help="Build a separate bank-statement Excel file per account (running balance)",
    )
    p_stmts.add_argument("--send", action="store_true",
                         help="Email the statements (one attachment per account)")
    p_stmts.add_argument("--days", type=int, default=7,
                         help="Length of the statement window in days (default: 7)")
    p_stmts.set_defaults(func=cmd_statements)

    sub.add_parser("backup", help="pg_dump the database to a (gzipped/encrypted) artifact") \
        .set_defaults(func=cmd_backup)

    sub.add_parser(
        "backfill-hashes",
        help="One-off: re-key existing rows to the day_seq-aware hash (idempotent)",
    ).set_defaults(func=cmd_backfill_hashes)

    return parser


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
