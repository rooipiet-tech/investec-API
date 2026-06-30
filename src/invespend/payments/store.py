"""Persistent pending + daily-aggregate state (F6, F7, F17, F20).

File/JSON only — NO schema change, NO db/ migration (OQ2 default). Two pieces of
state live under ``state_dir``:
  * ``pending.json``      — pending/executed payment records, keyed by dedup key.
  * ``daily_totals.json`` — executed-amount total per ISO date (survives a fresh
    process, F6).

PR2/F20: records are built via an EXPLICIT minimal-field allowlist at write time
(``_minimal_record``) — never raw-then-redact. A raw/parsed account number never
enters the store; only last-3 or a hash. No secret is ever stored.

PR3/F6/F7: ``execute_and_commit`` re-reads the persisted daily total, re-checks
the aggregate cap, and atomically increments the total together with marking the
record executed and burning the token nonce — under one lock-ordered write — so
two pendings in one cycle cannot jointly exceed the aggregate.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

# Fields permitted in a persisted pending record. Anything not here is dropped.
_PENDING_ALLOWED = {
    "dedup_key",
    "status",  # pending | executed | burned | parked
    "amount",
    "currency",
    "beneficiary_id",
    "source_account_id",
    "source_account_last3",
    "message_id_hash",
    "nonce",
    "created_at",
    "executed_at",
    "execution_mode",  # dry-run | live
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _minimal_record(
    *,
    dedup_key: str,
    amount,
    currency: str,
    beneficiary_id: str,
    source_account_id: str,
    source_account_last3: str,
    message_id: str,
    nonce: str,
) -> dict:
    """Build a pending record from an EXPLICIT allowlist (PR2/F20).

    Note: a raw/parsed account number is NEVER an input here — only the resolved
    source ``account_id`` (an opaque Investec id, not a PAN) and its last-3.
    The inbound ``message_id`` is stored only as a hash.
    """
    import hashlib

    record = {
        "dedup_key": dedup_key,
        "status": "pending",
        "amount": f"{Decimal(str(amount)).quantize(Decimal('0.01'))}",
        "currency": str(currency or "").upper(),
        "beneficiary_id": beneficiary_id,
        "source_account_id": source_account_id,
        "source_account_last3": source_account_last3,
        "message_id_hash": hashlib.sha256(str(message_id or "").encode()).hexdigest(),
        "nonce": nonce,
        "created_at": _utcnow().isoformat(),
        "executed_at": None,
        "execution_mode": None,
    }
    return {k: v for k, v in record.items() if k in _PENDING_ALLOWED}


class PaymentStore:
    def __init__(self, state_dir: str | os.PathLike) -> None:
        self._dir = Path(state_dir)
        self._pending_path = self._dir / "pending.json"
        self._totals_path = self._dir / "daily_totals.json"

    # ── pending ────────────────────────────────────────────────────────────
    def _load_pending(self) -> dict:
        if not self._pending_path.exists():
            return {}
        try:
            return json.loads(self._pending_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_pending(self, data: dict) -> None:
        _atomic_write_json(self._pending_path, data)

    def add_pending(
        self,
        *,
        dedup_key: str,
        amount,
        currency: str,
        beneficiary_id: str,
        source_account_id: str,
        source_account_last3: str,
        message_id: str,
        nonce: str,
    ) -> dict:
        """Idempotently add a pending record keyed by dedup key (F7).

        Re-presenting the same email returns the existing record unchanged —
        one pending per dedup key, never a duplicate.
        """
        data = self._load_pending()
        if dedup_key in data:
            return data[dedup_key]
        record = _minimal_record(
            dedup_key=dedup_key,
            amount=amount,
            currency=currency,
            beneficiary_id=beneficiary_id,
            source_account_id=source_account_id,
            source_account_last3=source_account_last3,
            message_id=message_id,
            nonce=nonce,
        )
        data[dedup_key] = record
        self._save_pending(data)
        return record

    def get(self, dedup_key: str) -> dict | None:
        return self._load_pending().get(dedup_key)

    def list_records(self, status: str | None = None) -> list[dict]:
        records = list(self._load_pending().values())
        if status is not None:
            records = [r for r in records if r.get("status") == status]
        return records

    def is_executed(self, dedup_key: str) -> bool:
        rec = self.get(dedup_key)
        return bool(rec and rec.get("status") == "executed")

    # ── daily aggregate ──────────────────────────────────────────────────────
    def _load_totals(self) -> dict:
        if not self._totals_path.exists():
            return {}
        try:
            return json.loads(self._totals_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def daily_total(self, day: str | None = None) -> Decimal:
        day = day or date.today().isoformat()
        return Decimal(str(self._load_totals().get(day, "0")))

    # ── atomic execute commit (PR3) ─────────────────────────────────────────
    def execute_and_commit(
        self,
        dedup_key: str,
        amount,
        *,
        daily_aggregate_cap,
        execution_mode: str,
        day: str | None = None,
    ) -> dict:
        """Re-read daily total, re-check aggregate, then atomically increment the
        total + mark executed + burn the nonce — all under one ordered write.

        Returns ``{"committed": bool, "reason": str, "daily_total": str}``. When
        the re-read aggregate would be exceeded, NOTHING is mutated (the caller
        must not have called the write endpoint — pipeline re-checks first).
        """
        from .caps import check_daily_aggregate

        day = day or date.today().isoformat()
        amt = Decimal(str(amount)).quantize(Decimal("0.01"))

        totals = self._load_totals()
        current = Decimal(str(totals.get(day, "0")))
        decision = check_daily_aggregate(amt, current, daily_aggregate_cap)
        if not decision.ok:
            return {"committed": False, "reason": decision.reason, "daily_total": str(current)}

        pending = self._load_pending()
        record = pending.get(dedup_key)
        if record is None:
            return {"committed": False, "reason": "unknown dedup key", "daily_total": str(current)}
        if record.get("status") == "executed":
            # already executed -> idempotent no-op, do NOT double-count.
            return {
                "committed": False,
                "reason": "already executed",
                "daily_total": str(current),
            }

        new_total = current + amt
        totals[day] = str(new_total)
        record["status"] = "executed"
        record["executed_at"] = _utcnow().isoformat()
        record["execution_mode"] = execution_mode
        record["nonce"] = ""  # burn the single-use nonce

        # PR3/F6/F7: persist the executed+burned pending state FIRST, then the
        # daily total. A crash between the two leaves the record already
        # executed with its nonce burned (a re-presented token cannot re-pay)
        # and at worst an UN-incremented day — conservative (never double-pays,
        # never lets a stale token execute). The reverse order could leave the
        # day incremented while the record is still pending/un-burned, allowing
        # a re-presented token to double-pay.
        self._save_pending(pending)
        _atomic_write_json(self._totals_path, totals)
        return {"committed": True, "reason": "", "daily_total": str(new_total)}

    def mark_parked(self, dedup_key: str, reason: str = "") -> None:
        data = self._load_pending()
        rec = data.get(dedup_key)
        if rec is not None:
            rec["status"] = "parked"
            self._save_pending(data)

    # ── retention / cleanup (F20) ────────────────────────────────────────────
    def cleanup(self, retention_days: int, now: datetime | None = None) -> int:
        """Remove pending/executed records older than ``retention_days`` and
        prune daily totals past the window. Returns the number of records removed."""
        now = now or _utcnow()
        cutoff = now - timedelta(days=retention_days)

        data = self._load_pending()
        keep = {}
        removed = 0
        for key, rec in data.items():
            ts = rec.get("executed_at") or rec.get("created_at")
            try:
                when = datetime.fromisoformat(ts) if ts else now
            except (TypeError, ValueError):
                when = now
            if when < cutoff:
                removed += 1
            else:
                keep[key] = rec
        if removed:
            self._save_pending(keep)

        totals = self._load_totals()
        cutoff_day = cutoff.date()
        kept_totals = {}
        for day, total in totals.items():
            try:
                if date.fromisoformat(day) >= cutoff_day:
                    kept_totals[day] = total
            except ValueError:
                kept_totals[day] = total
        if kept_totals != totals:
            _atomic_write_json(self._totals_path, kept_totals)
        return removed
