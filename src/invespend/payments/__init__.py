"""Isolated email-payment-approval subpackage.

This package is fully additive: it does not import from, mutate, or alter the
behaviour of any existing invespend module (ingest/report/statements/db/backup),
the existing CLI subcommands, db/ migrations, or emailer.send_email.

Security posture (see .loop/spec.json F1-F20):
  * Inbound email content NEVER authorizes a payment. Only a valid, single-use,
    expiring, bound HMAC token does.
  * Execution targets only an exact pre-registered beneficiary id.
  * DRY-RUN is the default; live money movement requires both an explicit enable
    flag AND a write-scoped credential, and is audited.
"""
