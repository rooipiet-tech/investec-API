#!/usr/bin/env python3
"""Send a one-off test email to verify SMTP settings end to end.

Exercises the real send path (STARTTLS → login → attachment → delivery) using
the same emailer and Settings the weekly report uses, but without touching the
database, Investec, or the report builder — so a failure here is unambiguously
an email/credentials problem, not a data one.

Usage:
    python scripts/send_test_email.py                 # send to REPORT_SENDER (yourself)
    python scripts/send_test_email.py you@example.com  # send to a specific address

Requires the same SMTP_* / REPORT_SENDER env (or .env) the report uses. Because
Settings.load() also validates the Investec/DATABASE_URL vars, run it from the
environment that already has your full .env — nothing is read from those beyond
loading; no DB connection is opened.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from pathlib import Path

from invespend.config import Settings
from invespend.emailer import send_email


def main() -> int:
    settings = Settings.load()
    settings.require_email()  # fail fast with a clear message if SMTP vars are missing

    recipient = sys.argv[1] if len(sys.argv) > 1 else settings.report_sender
    stamp = datetime.now().isoformat(timespec="seconds")

    with tempfile.TemporaryDirectory() as tmp:
        attachment = Path(tmp) / "invespend-test.txt"
        attachment.write_text(f"invespend SMTP test attachment generated at {stamp}\n")
        send_email(
            settings,
            attachment,
            subject=f"invespend SMTP test — {stamp}",
            body=(
                "This is a test email from invespend's send_test_email.py.\n\n"
                "If you received it (with the attached .txt), your SMTP host, "
                "port, credentials, sender, and TLS are all working.\n"
            ),
            recipients=[recipient],
        )

    print(f"Test email sent to {recipient} via {settings.smtp_host}:{settings.smtp_port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
