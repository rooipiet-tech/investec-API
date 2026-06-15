"""Command-line entry point: invespend init-db | ingest | report | statements."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from . import db
from .backup import run_backup
from .config import AccountGroup, Settings
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


def _report_body(info: dict) -> str:
    return (
        "Hi,\n\nAttached is your Investec weekly spend analysis for "
        f"{info['start']} to {info['end']} ({info['rows']} transactions).\n\n"
        "— invespend"
    )


def _statements_body(info: dict) -> str:
    lines = [
        f"  • {a['account_number']} ({a['account_name']}): "
        f"{a['transactions']} txns from {a['start']}, "
        f"closing balance {_money(a['closing_balance'])}"
        for a in info["per_account"]
    ]
    return (
        "Hi,\n\nAttached are your Investec per-account statements — one Excel "
        "file per account, each a full transaction listing (newest first) with "
        f"a running balance, as at {info['end']}:\n\n"
        + "\n".join(lines)
        + "\n\n— invespend"
    )


def _send_group_report(
    settings: Settings,
    group: AccountGroup | None,
    exclude: list[str],
    send: bool,
    extra_exclude: list[str] | None = None,
) -> None:
    """Generate and optionally email a weekly report for one account group.

    ``group=None`` means the "default" bucket (all accounts not claimed by a
    named group and not in the exclude list).
    """
    include = group.account_patterns if group else None
    excl = list(exclude) + (extra_exclude or [])
    label = group.name if group else "default"
    path, info = generate_weekly_report(
        settings,
        include_patterns=include,
        exclude_patterns=excl,
        label=label,
    )
    tag = f"({label})" if group else "(default)"
    print(f"Report {tag} written: {path} ({info})")
    if not send:
        return
    recipients = group.recipients if group else settings.report_recipients
    if not recipients:
        logging.getLogger(__name__).warning(
            "No recipients for %s report; skipping email", label
        )
        return
    subject = f"Weekly spend analysis: {info['start']} to {info['end']}"
    send_report(settings, path, subject, _report_body(info), recipients=recipients)
    print(f"Report {tag} emailed.")


def cmd_report(args: argparse.Namespace) -> int:
    settings = Settings.load()
    groups = settings.report_account_groups
    exclude = settings.report_exclude_accounts

    if groups:
        all_group_patterns = [p for g in groups for p in g.account_patterns]
        for group in groups:
            _send_group_report(settings, group, exclude, args.send)
        # Default bucket: accounts not in any named group
        _send_group_report(settings, None, exclude, args.send,
                           extra_exclude=all_group_patterns)
    else:
        _send_group_report(settings, None, exclude, args.send)
    return 0


def _money(value: float | None) -> str:
    # Space-grouped thousands to match the workbooks' Accounting format
    # (en-ZA style): 15 515.50, not 15,515.50.
    return f"{value:,.2f}".replace(",", " ") if value is not None else "n/a"


def _send_group_statements(
    settings: Settings,
    group: AccountGroup | None,
    exclude: list[str],
    days: int | None,
    send: bool,
    extra_exclude: list[str] | None = None,
) -> None:
    include = group.account_patterns if group else None
    excl = list(exclude) + (extra_exclude or [])
    label = group.name if group else "default"
    paths, info = generate_account_statements(
        settings,
        days=days,
        include_patterns=include,
        exclude_patterns=excl,
    )
    tag = f"({label})" if group else "(default)"
    print(f"Statements {tag}: {len(paths)} file(s)")
    if not paths:
        return
    if not send:
        return
    recipients = group.recipients if group else settings.report_recipients
    if not recipients:
        logging.getLogger(__name__).warning(
            "No recipients for %s statements; skipping email", label
        )
        return
    span = "full history" if info["full_history"] else f"{info['start']} to {info['end']}"
    subject = (
        f"Account statements ({span}) as at {info['end']} "
        f"— {info['accounts']} account(s)"
    )
    send_email(settings, paths, subject, _statements_body(info), recipients=recipients)
    print(f"Statements {tag} emailed.")


def cmd_statements(args: argparse.Namespace) -> int:
    settings = Settings.load()
    groups = settings.report_account_groups
    exclude = settings.report_exclude_accounts

    if groups:
        all_group_patterns = [p for g in groups for p in g.account_patterns]
        for group in groups:
            _send_group_statements(settings, group, exclude, args.days, args.send)
        _send_group_statements(settings, None, exclude, args.days, args.send,
                               extra_exclude=all_group_patterns)
    else:
        _send_group_statements(settings, None, exclude, args.days, args.send)
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
    p_stmts.add_argument("--days", type=int, default=None,
                         help="Trailing window in days; omit for full history "
                              "(account's first transaction → today, the default)")
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
