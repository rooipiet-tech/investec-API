"""Deterministic dedup key for idempotency (F7).

A payment is identified by sha256(message_id | amount | beneficiary_id | currency).
The same inbound email therefore yields the same key on every cycle, preventing
double-pending and double-execution; genuinely different payments yield different
keys. Mirrors the ``db.transaction_hash`` idiom (sha256 of joined parts).
"""
from __future__ import annotations

import hashlib
from decimal import Decimal


def _amount_str(amount) -> str:
    try:
        return str(Decimal(str(amount)).quantize(Decimal("0.01")))
    except Exception:  # noqa: BLE001 — fall back to raw string form
        return str(amount)


def dedup_key(message_id: str, amount, beneficiary_id: str, currency: str) -> str:
    parts = [
        str(message_id or ""),
        _amount_str(amount),
        str(beneficiary_id or ""),
        str(currency or "").upper(),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()
