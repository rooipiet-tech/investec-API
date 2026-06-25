"""Append-only audit trail (F9, F15, F20).

Every pipeline step appends one timestamped JSON line to ``audit.log`` under the
state dir. Entries are never mutated. PR2/F20: each entry is built from an
EXPLICIT minimal-field allowlist (``_safe_fields``) at write time — never
raw-then-redact — and a defensive scrub drops anything that looks like a full
account number (>=6 consecutive digits) or carries a secret-shaped key. The
signing secret is never passed in and never recorded.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

# Keys allowed to appear verbatim in an audit detail object.
_DETAIL_ALLOWED = {
    "dedup_key",
    "amount",
    "currency",
    "beneficiary_id",
    "source_account_last3",
    "source_account_id",
    "message_id_hash",
    "reason",
    "status",
    "execution_mode",
    "live_enabled",
    "daily_total",
    "result",
    "count",
}
# Keys whose names suggest a secret/PAN — never recorded even if allow-listed.
_FORBIDDEN_KEY = re.compile(
    r"(secret|password|token|pan|account_number|accountnumber|full_account)", re.IGNORECASE
)
_LONG_DIGITS = re.compile(r"\d{6,}")
# Numeric/amount keys whose values are legitimately long runs of digits (e.g.
# a daily_total of '100000.00'). These are exempt from the long-digit scrub so
# the amounts stay readable; every OTHER (free-text) field is still scrubbed and
# a full account number is never emitted.
_NUMERIC_KEYS = {"amount", "daily_total", "per_payment_cap", "daily_aggregate_cap"}


def _scrub_value(value):
    if isinstance(value, str):
        # Never let a full account-number-shaped string through.
        return _LONG_DIGITS.sub("[redacted]", value)
    return value


def _safe_fields(detail: dict | None) -> dict:
    if not detail:
        return {}
    out = {}
    for key, value in detail.items():
        if _FORBIDDEN_KEY.search(str(key)):
            continue
        if key not in _DETAIL_ALLOWED:
            continue
        # Allowlisted numeric/amount keys keep their value verbatim; everything
        # else is scrubbed for account-number-shaped digit runs.
        out[key] = value if key in _NUMERIC_KEYS else _scrub_value(value)
    return out


class AuditLog:
    def __init__(self, state_dir: str | os.PathLike) -> None:
        self._path = Path(state_dir) / "audit.log"

    def append(self, step: str, detail: dict | None = None, *, ts: str | None = None) -> dict:
        entry = {
            "ts": ts or datetime.now(timezone.utc).isoformat(),
            "step": step,
            "detail": _safe_fields(detail),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry

    def read_entries(self) -> list[dict]:
        if not self._path.exists():
            return []
        entries = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
        return entries
