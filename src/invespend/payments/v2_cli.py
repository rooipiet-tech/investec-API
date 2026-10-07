"""``approve-payments`` in v2 mode (S11): wiring only; the logic lives in ``cycle``. Dry-run is the default.

``run_v2`` is reached from ONE inserted branch in ``cli.cmd_approve_payments`` when ``PAYMENTS_MODE`` is not
``legacy``. It builds the v2 inbox, the client (live uses ``payment_credentials()``, everything else the read
credential), the default ``smtp_send`` (the existing, unmodified ``emailer._smtp_send``), the image extractor, and,
for the postgres backend ONLY, ``PgInstructionStore`` and ``PgAuditLog`` (never ``PgPaymentStore``), each looked up as
a module attribute at call time so tests can monkeypatch them. Exit codes: 0 ok, 2 error envelope. No new flag.
"""
from __future__ import annotations

import functools
import json
import re

from .mode import KNOWN_MODES, payments_mode

_SECRET_NAME = re.compile(r"secret|password|pass|token|key|database_url|api_key|imap_user|smtp_user", re.IGNORECASE)
_LONG_DIGITS = re.compile(r"\d{6,}")


def secret_values(settings: object) -> list[str]:
    """Every non-empty (>= 4 chars) string value of a secret-looking settings attribute, longest first. Never raises."""
    try:
        values: set[str] = set()
        names = set(getattr(settings, "__dict__", {})) | {n for n in dir(settings) if not n.startswith("_")}
        for name in names:
            if not _SECRET_NAME.search(name):
                continue
            try:
                value = getattr(settings, name)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(value, str) and len(value) >= 4:
                values.add(value)
        return sorted(values, key=len, reverse=True)
    except Exception:  # noqa: BLE001
        return []


def scrub_message(text: str, settings: object) -> str:
    """Replace every secret-looking settings value (matched case-insensitively, whitespace runs equivalent, so a value
    split across a newline or upper-cased is still caught), then bearer / secret-word / JWT / token-like values, then
    every 6+ digit run (T15, R4-T5). The text is cut to 5000 characters and NFKC-folded (format characters dropped)
    before redaction. Used for the error envelope."""
    from .outcome import REDACTED, SCRUB_CUT, fold_text, redact_secret_words, secret_value_pattern

    out = fold_text(str(text)[:SCRUB_CUT])      # bound first (linear), then NFKC + drop zero-width / format characters
    pattern = secret_value_pattern(secret_values(settings))
    if pattern is not None:
        out = pattern.sub(REDACTED, out)
    out = redact_secret_words(out)
    return _LONG_DIGITS.sub(REDACTED, out)


def _envelope(exc_name: str, message: str, requested_mode: str) -> dict:
    return {"error": exc_name, "message": message, "requested_mode": requested_mode, "payments_mode": "v2"}


def run_v2(args, settings, requested_mode: str) -> int:
    try:
        mode = payments_mode(settings)
        if mode not in KNOWN_MODES:
            print(json.dumps(_envelope("UnknownPaymentsMode", f"payments_mode {mode!r} is not one of {list(KNOWN_MODES)}",
                                       requested_mode), sort_keys=True))
            return 2
        backend = getattr(settings, "payments_state_backend", "file")
        if backend != "postgres":
            print(json.dumps(_envelope("PreflightError", "v2_requires_durable_store", requested_mode), sort_keys=True))
            return 2

        from .. import emailer, investec_client
        from . import audit as audit_mod
        from . import cycle, images, instructions, v2_inbox

        live = bool(settings.live_enabled())
        if live:
            cid, csec, akey = settings.payment_credentials()
        else:
            cid, csec, akey = settings.investec_client_id, settings.investec_client_secret, settings.investec_api_key
        client = investec_client.InvestecClient(cid, csec, akey, settings.investec_base_url)
        inbox = v2_inbox.V2ImapInbox(settings)
        store = instructions.PgInstructionStore(settings.database_url)
        audit = audit_mod.PgAuditLog(settings.database_url)
        summary = cycle.run_instruction_cycle(
            settings, inbox=inbox, client=client, store=store, audit=audit,
            smtp_send=functools.partial(emailer._smtp_send, settings), extractor=images.get_extractor(settings))
    except Exception as exc:  # noqa: BLE001 - machine-readable envelope, scrubbed (T15)
        print(json.dumps(_envelope(type(exc).__name__, scrub_message(str(exc), settings), requested_mode), sort_keys=True))
        return 2
    summary["requested_mode"] = requested_mode
    summary["live_enabled"] = live
    summary["state_backend"] = getattr(settings, "payments_state_backend", "file")
    if live:
        summary["credential_set"] = "write" if settings._has_write_trio() else "main"
    else:
        summary["credential_set"] = "read"
    print(json.dumps(summary, sort_keys=True))
    return 0
