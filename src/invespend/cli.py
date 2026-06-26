"""Command-line entry point: invespend init-db | ingest | report | statements."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from . import db
from .account_alert import build_alert_email, find_unclaimed
from .backup import run_backup
from .config import Settings
from .emailer import _smtp_send, send_email, send_report
from .groups import EXCLUDE_ACCOUNTS, GROUPS, Group
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
    _alert_new_accounts(settings, summary.get("new_accounts", []))
    return 0


def _alert_new_accounts(settings: Settings, new_accounts: list[dict]) -> None:
    """Email the user about new accounts that aren't assigned to any group.

    Non-fatal: if email isn't configured (e.g. during a bare ingest run that
    omits SMTP secrets), we log a warning and continue.
    """
    from email.message import EmailMessage
    log = logging.getLogger(__name__)

    if not new_accounts:
        return

    unclaimed = find_unclaimed(new_accounts, settings)
    subject, body = build_alert_email(new_accounts, settings)
    print(f"New account(s) detected: {[a['account_name'] for a in new_accounts]}")
    if unclaimed:
        print(
            f"Unclaimed account(s) needing group assignment: "
            f"{[a['account_name'] for a in unclaimed]}"
        )

    # Collect all configured recipients (group recipients as fallback).
    recipients = settings.report_recipients or [
        r for g in GROUPS for r in g.recipients
    ]
    if not recipients:
        log.warning("New accounts detected but no recipients configured; skipping alert email.")
        return

    try:
        settings.require_email()
    except RuntimeError as exc:
        log.warning("Cannot send new-account alert: %s", exc)
        return

    msg = EmailMessage()
    msg["From"] = settings.report_sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        _smtp_send(settings, msg)
        log.info("New-account alert emailed to %s", msg["To"])
        print(f"Alert email sent to: {msg['To']}")
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to send new-account alert email: %s", exc)


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


def _resolve_group_recipients(
    settings: Settings,
    group: Group | None,
    label: str,
    kind: str,
) -> list[str]:
    """Recipients for a group delivery, or [] (with a warning) if none."""
    recipients = group.recipients if group else settings.report_recipients
    if not recipients:
        logging.getLogger(__name__).warning(
            "No recipients for %s %s; skipping email", label, kind
        )
    return recipients


def _send_group_report(
    settings: Settings,
    group: Group | None,
    send: bool,
    extra_exclude_names: list[str] | None = None,
    extra_exclude_numbers: list[str] | None = None,
) -> None:
    """Generate and optionally email a weekly report for one account group.

    ``group=None`` means the "default" bucket (accounts not claimed by any
    named group and not in EXCLUDE_ACCOUNTS).
    """
    label = group.name if group else "default"
    path, info = generate_weekly_report(
        settings,
        include_patterns=group.name_patterns if group else None,
        include_account_numbers=group.account_numbers if group else None,
        exclude_patterns=list(EXCLUDE_ACCOUNTS) + (extra_exclude_names or []),
        exclude_account_numbers=extra_exclude_numbers or [],
        label=label,
    )
    tag = f"({label})" if group else "(default)"
    print(f"Report {tag} written: {path} ({info})")
    if not send:
        return
    recipients = _resolve_group_recipients(settings, group, label, "report")
    if not recipients:
        return
    subject = f"Weekly spend analysis: {info['start']} to {info['end']}"
    send_report(settings, path, subject, _report_body(info), recipients=recipients)
    print(f"Report {tag} emailed.")


def cmd_report(args: argparse.Namespace) -> int:
    settings = Settings.load()

    if GROUPS:
        all_names = [p for g in GROUPS for p in g.name_patterns]
        all_numbers = [n for g in GROUPS for n in g.account_numbers]
        for group in GROUPS:
            _send_group_report(settings, group, args.send)
        # Default bucket: accounts not claimed by any named group
        _send_group_report(settings, None, args.send,
                           extra_exclude_names=all_names,
                           extra_exclude_numbers=all_numbers)
    else:
        _send_group_report(settings, None, args.send)
    return 0


def _money(value: float | None) -> str:
    # Space-grouped thousands to match the workbooks' Accounting format
    # (en-ZA style): 15 515.50, not 15,515.50.
    return f"{value:,.2f}".replace(",", " ") if value is not None else "n/a"


def _send_group_statements(
    settings: Settings,
    group: Group | None,
    days: int | None,
    send: bool,
    extra_exclude_names: list[str] | None = None,
    extra_exclude_numbers: list[str] | None = None,
) -> None:
    label = group.name if group else "default"
    paths, info = generate_account_statements(
        settings,
        days=days,
        include_patterns=group.name_patterns if group else None,
        include_account_numbers=group.account_numbers if group else None,
        exclude_patterns=list(EXCLUDE_ACCOUNTS) + (extra_exclude_names or []),
        exclude_account_numbers=extra_exclude_numbers or [],
    )
    tag = f"({label})" if group else "(default)"
    print(f"Statements {tag}: {len(paths)} file(s)")
    if not paths:
        return
    if not send:
        return
    recipients = _resolve_group_recipients(settings, group, label, "statements")
    if not recipients:
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

    if GROUPS:
        all_names = [p for g in GROUPS for p in g.name_patterns]
        all_numbers = [n for g in GROUPS for n in g.account_numbers]
        for group in GROUPS:
            _send_group_statements(settings, group, args.days, args.send)
        _send_group_statements(settings, None, args.days, args.send,
                               extra_exclude_names=all_names,
                               extra_exclude_numbers=all_numbers)
    else:
        _send_group_statements(settings, None, args.days, args.send)
    return 0


def cmd_backup(_args: argparse.Namespace) -> int:
    settings = Settings.load()
    path = run_backup(settings)
    print(f"Backup written: {path}")
    return 0


def cmd_backfill_hashes(_args: argparse.Namespace) -> int:
    import os
    # Only DATABASE_URL is required — no Investec API creds needed.
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        database_url = Settings.load().database_url
    with db.connect(database_url) as conn:
        summary = db.backfill_transaction_hashes(conn)
    print(f"Backfill: {summary}")
    return 0


def cmd_approve_payments(args: argparse.Namespace) -> int:
    """Run one email-payment-approval cycle. Headless, machine-readable JSON,
    deterministic exit code (F16). DRY-RUN unless live is configured (F8)."""
    import json

    # --live is advisory only: live still requires live_enabled() (flag AND cred).
    requested_mode = "live" if getattr(args, "live", False) else "dry-run"
    try:
        settings = Settings.load()
        from .investec_client import InvestecClient
        from .payments.inbox import ImapInbox
        from .payments.pipeline import run_approval_cycle

        live = settings.live_enabled()
        # F8/PR1: live mode uses the payment-capable credential (separate write
        # trio if configured, else the user-declared payment-capable main key);
        # dry-run uses the read credential.
        if live:
            cid, csec, akey = settings.payment_credentials()
            client = InvestecClient(cid, csec, akey, settings.investec_base_url)
        else:
            client = InvestecClient(
                settings.investec_client_id,
                settings.investec_client_secret,
                settings.investec_api_key,
                settings.investec_base_url,
            )
        inbox = ImapInbox(settings)
        # OQ2: select the payment-state backend. "file" (default) keeps the
        # existing JSON-under-state_dir behaviour; "postgres" stores pending /
        # daily-total / audit in the DB (reuses database_url) so a Railway deploy
        # needs no persistent volume.
        backend = getattr(settings, "payments_state_backend", "file")
        if backend == "postgres":
            from .payments.audit import PgAuditLog
            from .payments.pg_store import PgPaymentStore

            store = PgPaymentStore(settings.database_url)
            audit = PgAuditLog(settings.database_url)
            summary = run_approval_cycle(
                settings, inbox=inbox, client=client, store=store, audit=audit
            )
        else:
            summary = run_approval_cycle(settings, inbox=inbox, client=client)
    except Exception as exc:  # noqa: BLE001 — surface as machine-readable envelope (F16)
        print(json.dumps(
            {"error": type(exc).__name__, "message": str(exc),
             "requested_mode": requested_mode},
            sort_keys=True,
        ))
        return 2
    # F8/F16: surface the advisory flag and the effective gate so the flag's
    # effect (or inertness) is explicit and assertable. credential_set names which
    # creds the live client used (write trio vs the payment-capable main key);
    # beneficiary_source (set by the pipeline) shows api vs static allowlist.
    summary["requested_mode"] = requested_mode
    summary["live_enabled"] = live
    summary["state_backend"] = getattr(settings, "payments_state_backend", "file")
    if live:
        summary["credential_set"] = (
            "write" if settings._has_write_trio() else "main"
        )
    else:
        summary["credential_set"] = "read"
    print(json.dumps(summary, sort_keys=True))
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

    p_pay = sub.add_parser(
        "approve-payments",
        help="Run one email-payment-approval cycle (DRY-RUN default; outputs JSON)",
    )
    pay_mode = p_pay.add_mutually_exclusive_group()
    pay_mode.add_argument("--dry-run", dest="live", action="store_false",
                          help="Never call the write endpoint (default)")
    pay_mode.add_argument("--live", dest="live", action="store_true",
                          help="Permit live execution (still needs flag+write cred)")
    p_pay.set_defaults(live=False)
    p_pay.add_argument("--once", action="store_true",
                       help="Run a single cycle and exit (default)")
    p_pay.set_defaults(func=cmd_approve_payments)

    return parser


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
