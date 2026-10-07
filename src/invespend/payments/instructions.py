"""v2 instruction state (S9): the status table, the canonical offer digest and the in-memory store.

``TRANSITIONS`` is the single source (plan 2a) for the 0013 status CHECK list, both stores and the v2
cycle. ``accepted`` is reachable ONLY through ``InstructionStore.approve_item`` (an authenticated approval
reply); ``create`` refuses it, ``cas_status`` refuses it, so no code path can reach the write endpoint for an
unapproved item. ``PgInstructionStore`` lives in ``pg_instructions`` and is re-exported lazily from here
(``instructions.PgInstructionStore``) so tests and the CLI can look it up as a module attribute at call time.
"""
from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol

from .caps import check_daily_aggregate
from .excerpt import sanitise_excerpt
from .outcome import sanitize_provider_message

# ---------------------------------------------------------------- constants
STATUSES = (
    "awaiting_approval", "awaiting_beneficiary", "held", "accepted", "submitting",
    "executed", "failed", "needs_review", "needs_authorisation", "cancelled", "expired", "parked",
)
PENDING_APPROVAL = "awaiting_approval"   # R7-T14: alias for the F12 verify wording; behaviour identical

TRANSITIONS: dict[str, frozenset[str]] = {
    "awaiting_approval": frozenset({"accepted", "cancelled", "expired", "parked"}),
    "awaiting_beneficiary": frozenset({"held", "expired", "parked"}),
    "held": frozenset({"awaiting_approval", "expired", "parked"}),
    "accepted": frozenset({"submitting", "cancelled", "expired", "parked"}),
    "submitting": frozenset({"executed", "failed", "needs_review", "needs_authorisation", "parked"}),
    "executed": frozenset(),
    "failed": frozenset(),
    "needs_review": frozenset(),
    "needs_authorisation": frozenset(),
    "cancelled": frozenset(),
    "expired": frozenset(),
    "parked": frozenset(),
}
TERMINAL_STATUSES = frozenset(s for s, dest in TRANSITIONS.items() if not dest)
ACTIVE_STATUSES = frozenset(TRANSITIONS) - TERMINAL_STATUSES
INITIAL_STATUSES = frozenset({"awaiting_approval", "awaiting_beneficiary", "held", "parked"})   # NEVER "accepted"
POST_APPROVAL_STATUSES = frozenset({"accepted", "submitting", "executed", "failed", "needs_review", "needs_authorisation"})
DUPLICATE_GUARD_STATUSES = frozenset({
    "awaiting_approval", "awaiting_beneficiary", "held", "accepted", "submitting", "executed",
    "needs_review", "needs_authorisation",
})
FINALIZE_RELEASES: Mapping[str, bool] = {
    "executed": False, "needs_review": False, "failed": True, "needs_authorisation": False, "parked": True,
}
CLAIM_REASONS = ("ok", "daily_cap", "stale", "cas_lost")
APPROVE_REASONS = ("ok", "not_awaiting", "expired", "wrong_batch")
PATHS = frozenset({"registered", "new_payee"})
FIGURES_SOURCES = frozenset({"typed", "attachment", "image"})
NOTIFIED_FIELDS = {"paste": "paste_notified_at", "hold": "hold_notified_at"}
BOOTSTRAP_KEY = "beneficiary_bootstrap"
EXECUTION_MODES = frozenset({"dry-run", "live"})

INSTRUCTION_COLUMNS = (
    "instruction_id", "status", "path", "amount", "currency", "source_account_id", "source_profile_id",
    "source_account_last3", "payee_name_norm", "beneficiary_id", "beneficiary_fingerprint", "account_hmac",
    "recent_beneficiary", "daily_reserved", "reserved_day", "my_reference", "their_reference", "figures_source",
    "figures_excerpt", "message_id_hash", "notify_to", "received_at", "first_seen_at", "eligible_at", "batch_ref", "item_no",
    "offered_at", "batch_notified_at", "offer_digest", "approved_at", "expires_at", "paste_notified_at",
    "hold_notified_at", "executed_at", "execution_mode", "outcome_code", "outcome_message", "updated_at",
)
BENEFICIARY_SEEN_COLUMNS = ("beneficiary_id", "first_seen_at", "established", "last_fingerprint", "fingerprint_changed_at")
MESSAGE_SEEN_COLUMNS = ("instruction_id", "outcome", "seen_at", "auth_from", "resend_notified_at")
META_COLUMNS = ("key", "value", "set_at")
# Columns a create() record may carry (the create-owned ones of plan 2b).
CREATE_COLUMNS = frozenset({
    "instruction_id", "status", "path", "amount", "currency", "source_account_id", "source_profile_id",
    "source_account_last3", "payee_name_norm", "beneficiary_id", "beneficiary_fingerprint", "account_hmac",
    "recent_beneficiary", "my_reference", "their_reference", "figures_source", "figures_excerpt", "message_id_hash",
    "notify_to", "received_at", "first_seen_at", "eligible_at", "expires_at", "outcome_code", "updated_at",
})
_REQUIRED_CREATE = ("instruction_id", "status", "path", "amount", "source_account_id", "source_account_last3",
                    "figures_source", "message_id_hash", "notify_to", "received_at", "expires_at")

_SAST = timezone(timedelta(hours=2))   # UTC+02:00, no DST


def _owners(*names: str) -> tuple[str, ...]:
    return names


def _build_column_owners() -> dict[tuple[str, str], tuple[str, ...]]:
    owners: dict[tuple[str, str], tuple[str, ...]] = {}
    t = "payment_instruction"
    create_only = _owners("create")
    plain = {
        "instruction_id": create_only, "path": create_only, "amount": create_only, "currency": create_only,
        "source_account_id": create_only, "source_profile_id": create_only, "source_account_last3": create_only,
        "payee_name_norm": create_only, "my_reference": create_only, "their_reference": create_only,
        "recent_beneficiary": create_only, "message_id_hash": create_only, "figures_source": create_only, "figures_excerpt": create_only,
        "notify_to": create_only, "account_hmac": create_only, "received_at": create_only,
        "beneficiary_id": _owners("create", "set_held"),
        "beneficiary_fingerprint": _owners("create", "set_held"),
        "first_seen_at": _owners("create", "set_held"),
        "eligible_at": _owners("create", "set_held"),
        "status": _owners("create", "cas_status", "approve_item", "make_ready", "set_held", "claim_for_execution",
                          "finalize", "recover_stale_submitting"),
        "batch_ref": _owners("offer_batch", "unoffer_batch"),
        "item_no": _owners("offer_batch", "unoffer_batch"),
        "offered_at": _owners("offer_batch", "unoffer_batch"),
        "offer_digest": _owners("offer_batch", "unoffer_batch"),
        "batch_notified_at": _owners("mark_batch_notified"),
        "approved_at": _owners("approve_item"),
        "expires_at": _owners("create", "set_held", "make_ready", "offer_batch", "approve_item"),
        "daily_reserved": _owners("claim_for_execution", "finalize"),
        "reserved_day": _owners("claim_for_execution"),
        "paste_notified_at": _owners("mark_notified"),
        "hold_notified_at": _owners("mark_notified"),
        "executed_at": _owners("finalize"),
        "execution_mode": _owners("claim_for_execution"),
        "outcome_code": _owners("create", "cas_status", "finalize", "recover_stale_submitting"),
        "outcome_message": _owners("finalize"),
        "updated_at": _owners("create", "cas_status", "approve_item", "make_ready", "set_held", "offer_batch",
                              "unoffer_batch", "mark_batch_notified", "claim_for_execution", "finalize",
                              "recover_stale_submitting", "mark_notified"),
    }
    for col in INSTRUCTION_COLUMNS:
        owners[(t, col)] = plain[col]
    for col in BENEFICIARY_SEEN_COLUMNS:
        owners[("payment_beneficiary_seen", col)] = _owners("observe_beneficiary")
    owners[("payment_message_seen", "instruction_id")] = _owners("mark_message_seen")
    owners[("payment_message_seen", "outcome")] = _owners("mark_message_seen", "set_message_outcome", "sweep_stuck_messages")
    owners[("payment_message_seen", "seen_at")] = _owners("mark_message_seen")
    owners[("payment_message_seen", "auth_from")] = _owners("set_message_outcome")
    owners[("payment_message_seen", "resend_notified_at")] = _owners("mark_resend_notified")
    for col in META_COLUMNS:
        owners[("payment_v2_meta", col)] = _owners("meta_set")
    return owners


COLUMN_OWNERS: dict[tuple[str, str], tuple[str, ...]] = _build_column_owners()


# ------------------------------------------------------------------ results
@dataclass(frozen=True)
class BeneficiaryObservation:
    first_seen_at: datetime
    established: bool
    last_fingerprint: str | None
    fingerprint_changed_at: datetime | None


@dataclass(frozen=True)
class ClaimResult:
    committed: bool
    reason: str            # exactly one of CLAIM_REASONS
    daily_total: Decimal


@dataclass(frozen=True)
class ApproveResult:
    approved: bool
    reason: str            # exactly one of APPROVE_REASONS


class StoreNotInitialised(RuntimeError):
    """The four 0013 tables are missing (``init-db`` must have run)."""


# ------------------------------------------------------------------- helpers
def _amount(value: object) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("amount must be a decimal number") from None
    if not amount.is_finite():
        raise ValueError("amount must be finite")
    return amount.quantize(Decimal("0.01"))


def offer_digest(row: Mapping, batch_ref: str, item_no: int) -> str:
    """R7-T7: the ONE canonical serialisation binding what the batch email showed to what is executed.

    Fields in this exact order: batch_ref, item_no, amount (2 decimal places), currency, source_account_id,
    payee_name_norm, beneficiary_id, beneficiary_fingerprint; ``None`` is the empty string; joined with
    ``\\x1f``; UTF-8; sha256 hex digest. ``figures_excerpt`` is deliberately NOT bound (RS3AF5-1): it is display text
    set once at creation, never used to decide anything, and binding it would change every pinned digest.
    """
    def text(value: object) -> str:
        return "" if value is None else str(value)

    fields = [
        text(batch_ref), str(int(item_no)), format(_amount(row["amount"]), "f"), text(row.get("currency")),
        text(row.get("source_account_id")), text(row.get("payee_name_norm")), text(row.get("beneficiary_id")),
        text(row.get("beneficiary_fingerprint")),
    ]
    return hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()


def sast_day(moment: datetime) -> date:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_SAST).date()


def validate_create_record(record: Mapping) -> dict:
    """Shared by both stores: everything that can be refused WITHOUT touching a database."""
    unknown = {k for k in record if k not in CREATE_COLUMNS and record[k] is not None}
    if unknown:
        raise ValueError(f"unknown record keys: {sorted(unknown)}")
    for key in _REQUIRED_CREATE:
        if record.get(key) in (None, ""):
            raise ValueError(f"{key} is required")
    if not str(record["notify_to"]).strip():
        raise ValueError("notify_to is required")
    if record["status"] not in INITIAL_STATUSES:
        raise ValueError(f"status {record['status']!r} is not an initial status")
    if record["path"] not in PATHS:
        raise ValueError(f"path {record['path']!r} is not registered|new_payee")
    if record["figures_source"] not in FIGURES_SOURCES:
        raise ValueError(f"figures_source {record['figures_source']!r} is not typed|attachment|image")
    out = {col: None for col in INSTRUCTION_COLUMNS}
    out.update({
        "currency": "ZAR", "recent_beneficiary": False, "daily_reserved": False,
    })
    out.update({k: v for k, v in record.items() if k in CREATE_COLUMNS and v is not None})
    out["currency"] = record.get("currency") or "ZAR"
    out["notify_to"] = str(record["notify_to"]).strip()
    out["amount"] = _amount(record["amount"])
    out["figures_excerpt"] = sanitise_excerpt(record.get("figures_excerpt")) or None     # RS3AF5-1: never stored raw
    out["recent_beneficiary"] = bool(out.get("recent_beneficiary"))
    if out.get("updated_at") is None:
        out["updated_at"] = datetime.now(timezone.utc)
    return out


def _check_cas_edge(expected: Collection[str], new: str) -> list[str]:
    exp = list(expected)
    if not exp:
        raise ValueError("expected statuses must not be empty")
    if new == "submitting":
        raise ValueError("use claim_for_execution to enter submitting")
    if new == "accepted":
        raise ValueError("use approve_item to enter accepted")
    if "submitting" in exp:
        raise ValueError("use finalize to leave submitting")
    if new == "awaiting_approval" and "held" in exp:
        raise ValueError("use make_ready for held -> awaiting_approval")
    if new == "held" and "awaiting_beneficiary" in exp:
        raise ValueError("use set_held for awaiting_beneficiary -> held")
    for status in exp:
        if status not in TRANSITIONS or new not in TRANSITIONS[status]:
            raise ValueError(f"transition {status!r} -> {new!r} is not in TRANSITIONS")
    return exp


def check_finalize(expected: str, new_status: str, release: bool) -> None:
    if expected != "submitting":
        raise ValueError("finalize expects the row to be in 'submitting'")
    if new_status not in TRANSITIONS["submitting"]:
        raise ValueError(f"transition 'submitting' -> {new_status!r} is not in TRANSITIONS")
    if FINALIZE_RELEASES[new_status] != bool(release):
        raise ValueError(f"release must be {FINALIZE_RELEASES[new_status]} for {new_status!r}")


def message_for(new_status: str, outcome_message: str | None) -> str | None:
    """F40: only failed / needs_authorisation keep the (sanitised, <= 200 chars) provider message."""
    if new_status not in ("failed", "needs_authorisation") or outcome_message is None:
        return None
    return sanitize_provider_message(outcome_message) or None


def check_notified_field(field: str) -> str:
    if field not in NOTIFIED_FIELDS:
        raise ValueError("field must be 'paste' or 'hold'")
    return NOTIFIED_FIELDS[field]


# ----------------------------------------------------------------- protocol
class InstructionStore(Protocol):
    durable: bool

    def ping(self) -> None: ...
    def get(self, instruction_id: str) -> dict | None: ...
    def create(self, record: dict) -> tuple[dict, bool]: ...
    def mark_message_seen(self, instruction_id: str, now: datetime) -> bool: ...
    def set_message_outcome(self, instruction_id: str, outcome: str, *, auth_from: str | None = None) -> None: ...
    def sweep_stuck_messages(self, *, older_than: timedelta, now: datetime) -> list[dict]: ...
    def mark_resend_notified(self, instruction_id: str, now: datetime) -> bool: ...
    def list_active(self) -> list[dict]: ...
    def list_by_status(self, statuses: Collection[str]) -> list[dict]: ...
    def find_recent_similar(self, source_account_id: str, payee_name_norm: str, amount: Decimal,
                            since: datetime) -> dict | None: ...
    def bootstrap_done(self) -> bool: ...
    def mark_bootstrap_done(self, now: datetime) -> None: ...
    def observe_beneficiary(self, beneficiary_id: str, now: datetime, *, fingerprint: str | None,
                            established: bool = False) -> BeneficiaryObservation: ...
    def meta_get(self, key: str) -> str | None: ...
    def meta_set(self, key: str, value: str, now: datetime) -> None: ...
    def set_held(self, instruction_id: str, *, beneficiary_id: str, fingerprint: str, first_seen_at: datetime,
                 eligible_at: datetime, expires_at: datetime, now: datetime) -> bool: ...
    def make_ready(self, instruction_id: str, *, now: datetime, approval_window: timedelta) -> bool: ...
    def offer_batch(self, notify_to: str, *, now: datetime, approval_window: timedelta, max_items: int,
                    new_ref: Callable[[], str]) -> list[dict]: ...
    def unoffer_batch(self, batch_ref: str, *, now: datetime) -> int: ...
    def mark_batch_notified(self, batch_ref: str, now: datetime) -> int: ...
    def list_batch(self, batch_ref: str, notify_to: str) -> list[dict]: ...
    def list_pending_offered(self, notify_to: str, *, now: datetime, exclude_batch_ref: str | None = None) -> list[dict]: ...
    def list_unnotified_batches(self, *, older_than: timedelta, now: datetime) -> list[str]: ...
    def approve_item(self, instruction_id: str, *, batch_ref: str, item_no: int, notify_to: str, now: datetime,
                     grace: timedelta) -> ApproveResult: ...
    def cas_status(self, instruction_id: str, expected: Collection[str], new: str, *, now: datetime,
                   outcome_code: str | None = None) -> bool: ...
    def claim_for_execution(self, instruction_id: str, amount: Decimal, *, daily_cap: Decimal, execution_mode: str,
                            now: datetime) -> ClaimResult: ...
    def finalize(self, instruction_id: str, expected: str, new_status: str, *, release: bool, now: datetime,
                 outcome_code: str | None = None, outcome_message: str | None = None) -> bool: ...
    def recover_stale_submitting(self, *, older_than: timedelta, now: datetime) -> list[str]: ...
    def mark_notified(self, instruction_id: str, field: str, now: datetime) -> bool: ...
    def cleanup(self, retention_days: int, now: datetime) -> int: ...
    def daily_total(self, when: datetime) -> Decimal: ...


# ----------------------------------------------------------- memory store
class MemoryInstructionStore:
    """TESTS ONLY (``durable = False``): same semantics as ``PgInstructionStore`` with a thread-lock CAS.

    Every mutated row is validated against the two 0013 table CHECKs (batch columns all-or-none; a
    post-approval status requires ``approved_at`` and ``batch_ref``) and raises ``ValueError``. The CLI never
    builds one: a v2 cycle needs a durable store (``v2_requires_durable_store``).
    """

    durable = False

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[str, dict] = {}
        self._messages: dict[str, dict] = {}
        self._bene: dict[str, dict] = {}
        self._meta: dict[str, dict] = {}
        self._daily: dict[date, Decimal] = {}

    # -- checks (the two table CHECKs) ------------------------------------
    def _validate_row(self, row: Mapping) -> None:
        if row["status"] not in TRANSITIONS:
            raise ValueError("status outside the CHECK list")
        batch = [row.get("batch_ref"), row.get("item_no"), row.get("offered_at"), row.get("offer_digest")]
        if len({b is None for b in batch}) != 1:
            raise ValueError("batch columns must be all null or all set")
        if row.get("item_no") is not None and not 1 <= int(row["item_no"]) <= 999:
            raise ValueError("item_no out of range")
        if row["status"] in POST_APPROVAL_STATUSES and (row.get("approved_at") is None or row.get("batch_ref") is None):
            raise ValueError("post-approval status requires approved_at and batch_ref")

    def _put(self, row: dict) -> None:
        self._validate_row(row)
        self._rows[row["instruction_id"]] = row

    def _update(self, row: dict, **changes) -> None:
        candidate = dict(row, **changes)
        self._validate_row(candidate)
        row.update(changes)

    @staticmethod
    def _copy(row: dict | None) -> dict | None:
        return None if row is None else dict(row)

    # -- basics ----------------------------------------------------------
    def ping(self) -> None:
        return None

    def get(self, instruction_id: str) -> dict | None:
        with self._lock:
            return self._copy(self._rows.get(instruction_id))

    def create(self, record: dict) -> tuple[dict, bool]:
        row = validate_create_record(record)
        with self._lock:
            existing = self._rows.get(row["instruction_id"])
            if existing is not None:
                return dict(existing), False
            self._put(row)
            return dict(row), True

    def list_active(self) -> list[dict]:
        return self.list_by_status(ACTIVE_STATUSES)

    def list_by_status(self, statuses: Collection[str]) -> list[dict]:
        wanted = set(statuses)
        with self._lock:
            rows = [dict(r) for r in self._rows.values() if r["status"] in wanted]
        return sorted(rows, key=lambda r: (r["received_at"], r["instruction_id"]))

    def find_recent_similar(self, source_account_id: str, payee_name_norm: str, amount: Decimal,
                            since: datetime) -> dict | None:
        """Duplicate guard: matches rows in DUPLICATE_GUARD_STATUSES only; ``since`` bounds ``received_at``
        (the trusted receipt time), NEVER ``updated_at`` (R6-T5)."""
        wanted = _amount(amount)
        with self._lock:
            hits = [r for r in self._rows.values()
                    if r["status"] in DUPLICATE_GUARD_STATUSES and r["source_account_id"] == source_account_id
                    and r["payee_name_norm"] == payee_name_norm and _amount(r["amount"]) == wanted
                    and r["received_at"] >= since]
            if not hits:
                return None
            return dict(max(hits, key=lambda r: (r["received_at"], r["instruction_id"])))

    # -- message identity -------------------------------------------------
    def mark_message_seen(self, instruction_id: str, now: datetime) -> bool:
        with self._lock:
            if instruction_id in self._messages:
                return False
            self._messages[instruction_id] = {"instruction_id": instruction_id, "outcome": "processing",
                                              "seen_at": now, "auth_from": None, "resend_notified_at": None}
            return True

    def set_message_outcome(self, instruction_id: str, outcome: str, *, auth_from: str | None = None) -> None:
        with self._lock:
            row = self._messages.get(instruction_id)
            if row is None:
                return
            row["outcome"] = outcome
            if auth_from is not None:
                row["auth_from"] = auth_from

    def sweep_stuck_messages(self, *, older_than: timedelta, now: datetime) -> list[dict]:
        with self._lock:
            for row in self._messages.values():
                if row["outcome"] == "processing" and row["seen_at"] < now - older_than:
                    row["outcome"] = "stuck"
            hits = [dict(r) for r in self._messages.values()
                    if r["outcome"] == "stuck" and r["auth_from"] is not None and r["resend_notified_at"] is None]
        return sorted(hits, key=lambda r: (r["seen_at"], r["instruction_id"]))

    def mark_resend_notified(self, instruction_id: str, now: datetime) -> bool:
        with self._lock:
            row = self._messages.get(instruction_id)
            if row is None or row["resend_notified_at"] is not None:
                return False
            row["resend_notified_at"] = now
            return True

    # -- beneficiary memory and meta --------------------------------------
    def observe_beneficiary(self, beneficiary_id: str, now: datetime, *, fingerprint: str | None,
                            established: bool = False) -> BeneficiaryObservation:
        with self._lock:
            row = self._bene.get(beneficiary_id)
            if row is None:
                row = {"first_seen_at": now, "established": bool(established), "last_fingerprint": fingerprint,
                       "fingerprint_changed_at": None}
                self._bene[beneficiary_id] = row
            elif fingerprint is not None:
                previous = row["last_fingerprint"]
                if previous is None:
                    row["last_fingerprint"] = fingerprint            # baseline, not a change
                elif previous != fingerprint:
                    row["last_fingerprint"] = fingerprint
                    row["fingerprint_changed_at"] = now
            return BeneficiaryObservation(row["first_seen_at"], row["established"], row["last_fingerprint"],
                                          row["fingerprint_changed_at"])

    def meta_get(self, key: str) -> str | None:
        with self._lock:
            row = self._meta.get(key)
            return None if row is None else row["value"]

    def meta_set(self, key: str, value: str, now: datetime) -> None:
        with self._lock:
            self._meta[key] = {"value": value, "set_at": now}

    def bootstrap_done(self) -> bool:
        return self.meta_get(BOOTSTRAP_KEY) is not None

    def mark_bootstrap_done(self, now: datetime) -> None:
        self.meta_set(BOOTSTRAP_KEY, "done", now)

    # -- hold / ready ------------------------------------------------------
    def set_held(self, instruction_id: str, *, beneficiary_id: str, fingerprint: str, first_seen_at: datetime,
                 eligible_at: datetime, expires_at: datetime, now: datetime) -> bool:
        with self._lock:
            row = self._rows.get(instruction_id)
            if (row is None or row["status"] != "awaiting_beneficiary" or row["eligible_at"] is not None
                    or row["expires_at"] <= now):
                return False
            self._update(row, status="held", beneficiary_id=beneficiary_id, beneficiary_fingerprint=fingerprint,
                         first_seen_at=row["first_seen_at"] or first_seen_at, eligible_at=eligible_at,
                         expires_at=expires_at, updated_at=now)
            return True

    def make_ready(self, instruction_id: str, *, now: datetime, approval_window: timedelta) -> bool:
        with self._lock:
            row = self._rows.get(instruction_id)
            if (row is None or row["status"] != "held" or row["eligible_at"] is None
                    or not row["eligible_at"] <= now < row["expires_at"]):
                return False
            self._update(row, status="awaiting_approval", expires_at=now + approval_window, updated_at=now)
            return True

    # -- batches -----------------------------------------------------------
    def offer_batch(self, notify_to: str, *, now: datetime, approval_window: timedelta, max_items: int,
                    new_ref: Callable[[], str]) -> list[dict]:
        with self._lock:
            candidates = sorted(
                (r for r in self._rows.values()
                 if r["status"] == "awaiting_approval" and r["notify_to"] == notify_to and r["batch_ref"] is None
                 and r["expires_at"] > now),
                key=lambda r: (r["received_at"], r["instruction_id"]))[:max(int(max_items), 0)]
            if not candidates:
                return []
            used = {r["batch_ref"] for r in self._rows.values() if r["batch_ref"]}
            ref = None
            for _ in range(5):
                attempt = new_ref()
                if attempt not in used:
                    ref = attempt
                    break
            if ref is None:
                raise RuntimeError("could not allocate a unique batch ref")
            offered = []
            for number, row in enumerate(candidates, start=1):
                self._update(row, batch_ref=ref, item_no=number, offered_at=now,
                             offer_digest=offer_digest(row, ref, number), expires_at=now + approval_window,
                             updated_at=now)
                offered.append(dict(row))
            return offered

    def unoffer_batch(self, batch_ref: str, *, now: datetime) -> int:
        with self._lock:
            count = 0
            for row in self._rows.values():
                if (row["batch_ref"] == batch_ref and row["batch_notified_at"] is None
                        and row["status"] == "awaiting_approval"):
                    self._update(row, batch_ref=None, item_no=None, offered_at=None, offer_digest=None, updated_at=now)
                    count += 1
            return count

    def mark_batch_notified(self, batch_ref: str, now: datetime) -> int:
        with self._lock:
            count = 0
            for row in self._rows.values():
                if row["batch_ref"] == batch_ref and row["batch_notified_at"] is None:
                    self._update(row, batch_notified_at=now, updated_at=now)
                    count += 1
            return count

    def list_batch(self, batch_ref: str, notify_to: str) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._rows.values() if r["batch_ref"] == batch_ref and r["notify_to"] == notify_to]
        return sorted(rows, key=lambda r: r["item_no"])

    def list_pending_offered(self, notify_to: str, *, now: datetime, exclude_batch_ref: str | None = None) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._rows.values()
                    if r["status"] == "awaiting_approval" and r["batch_ref"] is not None and r["notify_to"] == notify_to
                    and r["expires_at"] > now and r["batch_ref"] != exclude_batch_ref]
        return sorted(rows, key=lambda r: (r["offered_at"], r["batch_ref"], r["item_no"]))

    def list_unnotified_batches(self, *, older_than: timedelta, now: datetime) -> list[str]:
        with self._lock:
            refs = {r["batch_ref"] for r in self._rows.values()
                    if r["batch_ref"] is not None and r["batch_notified_at"] is None
                    and r["status"] == "awaiting_approval" and r["expires_at"] > now
                    and r["offered_at"] < now - older_than}
        return sorted(refs)

    # -- approval and CAS --------------------------------------------------
    def approve_item(self, instruction_id: str, *, batch_ref: str, item_no: int, notify_to: str, now: datetime,
                     grace: timedelta) -> ApproveResult:
        with self._lock:
            row = self._rows.get(instruction_id)
            if (row is None or row["batch_ref"] != batch_ref or row["item_no"] != item_no
                    or row["notify_to"] != notify_to or row["offer_digest"] is None):
                return ApproveResult(False, "wrong_batch")
            if row["status"] != "awaiting_approval":
                return ApproveResult(False, "not_awaiting")
            if row["expires_at"] <= now:
                return ApproveResult(False, "expired")
            self._update(row, status="accepted", approved_at=now, expires_at=now + grace, updated_at=now)
            return ApproveResult(True, "ok")

    def cas_status(self, instruction_id: str, expected: Collection[str], new: str, *, now: datetime,
                   outcome_code: str | None = None) -> bool:
        exp = _check_cas_edge(expected, new)
        with self._lock:
            row = self._rows.get(instruction_id)
            if row is None or row["status"] not in exp:
                return False
            self._update(row, status=new, updated_at=now,
                         outcome_code=outcome_code if outcome_code is not None else row["outcome_code"])
            return True

    def daily_total(self, when: datetime) -> Decimal:
        with self._lock:
            return self._daily.get(sast_day(when), Decimal("0.00"))

    def claim_for_execution(self, instruction_id: str, amount: Decimal, *, daily_cap: Decimal, execution_mode: str,
                            now: datetime) -> ClaimResult:
        if execution_mode not in EXECUTION_MODES:
            raise ValueError("execution_mode must be dry-run or live")
        day = sast_day(now)
        with self._lock:
            row = self._rows.get(instruction_id)
            current = self._daily.get(day, Decimal("0.00"))
            if row is None or row["status"] != "accepted" or row["approved_at"] is None:
                return ClaimResult(False, "cas_lost", current)
            if _amount(amount) != _amount(row["amount"]):
                raise ValueError("amount does not match the stored row")
            if row["expires_at"] <= now:
                return ClaimResult(False, "stale", current)
            if not check_daily_aggregate(_amount(amount), current, daily_cap).ok:
                return ClaimResult(False, "daily_cap", current)
            total = current + _amount(amount)
            self._update(row, status="submitting", daily_reserved=True, reserved_day=day,
                         execution_mode=execution_mode, updated_at=now)
            self._daily[day] = total
            return ClaimResult(True, "ok", total)

    def finalize(self, instruction_id: str, expected: str, new_status: str, *, release: bool, now: datetime,
                 outcome_code: str | None = None, outcome_message: str | None = None) -> bool:
        check_finalize(expected, new_status, release)
        with self._lock:
            row = self._rows.get(instruction_id)
            if row is None or row["status"] != expected:
                return False
            reserved, reserved_day, amount = row["daily_reserved"], row["reserved_day"], _amount(row["amount"])
            self._update(
                row, status=new_status, updated_at=now,
                executed_at=now if new_status == "executed" else row["executed_at"],
                outcome_code=outcome_code if outcome_code is not None else row["outcome_code"],
                outcome_message=message_for(new_status, outcome_message) or row["outcome_message"])
            if release and reserved:
                row["daily_reserved"] = False
                if reserved_day is not None:
                    self._daily[reserved_day] = max(self._daily.get(reserved_day, Decimal("0.00")) - amount, Decimal("0.00"))
            return True

    def recover_stale_submitting(self, *, older_than: timedelta, now: datetime) -> list[str]:
        with self._lock:
            ids = sorted(r["instruction_id"] for r in self._rows.values()
                         if r["status"] == "submitting" and r["updated_at"] < now - older_than)
            for iid in ids:
                self._update(self._rows[iid], status="needs_review", outcome_code="stale_submitting", updated_at=now)
            return ids

    def mark_notified(self, instruction_id: str, field: str, now: datetime) -> bool:
        column = check_notified_field(field)
        with self._lock:
            row = self._rows.get(instruction_id)
            if row is None or row[column] is not None:
                return False
            self._update(row, **{column: now, "updated_at": now})
            return True

    def cleanup(self, retention_days: int, now: datetime) -> int:
        cutoff = now - timedelta(days=retention_days)
        with self._lock:
            gone = [i for i, r in self._rows.items() if r["status"] in TERMINAL_STATUSES and r["updated_at"] < cutoff]
            for iid in gone:
                del self._rows[iid]
            for mid in [m for m, r in self._messages.items() if r["seen_at"] < cutoff]:
                del self._messages[mid]
            return len(gone)


def __getattr__(name: str):
    """Lazy re-export (PEP 562): ``instructions.PgInstructionStore`` without a circular import."""
    if name == "PgInstructionStore":
        from .pg_instructions import PgInstructionStore
        return PgInstructionStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
