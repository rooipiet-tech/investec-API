"""Command-line entry point: invespend init-db | ingest | report."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from . import db
from .backup import run_backup
from .config import Settings
from .emailer import send_report
from .ingest import run_ingest
from .report import generate_weekly_report


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
        settings, window_days=args.days, from_date=from_date, to_date=to_date
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


def cmd_backup(_args: argparse.Namespace) -> int:
    settings = Settings.load()
    path = run_backup(settings)
    print(f"Backup written: {path}")
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
    p_ingest.set_defaults(func=cmd_ingest)

    p_report = sub.add_parser("report", help="Build the weekly Excel report")
    p_report.add_argument("--send", action="store_true", help="Email the report")
    p_report.set_defaults(func=cmd_report)

    sub.add_parser("backup", help="pg_dump the database to a (gzipped/encrypted) artifact") \
        .set_defaults(func=cmd_backup)

    return parser


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
