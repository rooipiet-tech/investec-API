"""Reference helpers for v2 (S2). Pure, stdlib only.

Two disjoint ref families:
  * INSTRUCTION ref ``[INV-<12 hex>]``: informational, never authorises anything;
  * BATCH ref ``B-<MMDD>-<4 hex>``: the approval linkage (F44), read ONLY from
    Subject / In-Reply-To / References, never from the body.
The ref confers no authority; authentication is the sender checks only.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses

# SAST = UTC+02:00, no DST.
_SAST = timezone(timedelta(hours=2))

BATCH_REF_RE = r"B-[0-9]{4}-[0-9a-f]{4}"

_INSTR_TAG = re.compile(r"\[INV-([0-9a-f]{12})\]")
# R7-T3: UNANCHORED (make_msgid yields <timeval.pid.rand.X@domain>), leading "\."
_INSTR_MSGID = re.compile(r"\.invespend-notification\.([0-9a-f]{12})@")
_BATCH_TAG = re.compile(r"\[BATCH (" + BATCH_REF_RE + r")\]")
_BATCH_MSGID = re.compile(r"\.invespend-notification\.(" + BATCH_REF_RE + r")@")


def request_ref(instruction_id: str) -> str:
    return instruction_id[:12]


def _distinct_in_order(patterns: Sequence[re.Pattern], texts: Sequence[str]) -> list[str]:
    hits: list[tuple[int, int, str]] = []
    for ti, text in enumerate(texts):
        for pat in patterns:
            for m in pat.finditer(text or ""):
                hits.append((ti, m.start(), m.group(1)))
    hits.sort(key=lambda h: (h[0], h[1]))
    seen: list[str] = []
    for _, _, ref in hits:
        if ref not in seen:
            seen.append(ref)
    return seen


def find_refs(*texts: str) -> list[str]:
    return _distinct_in_order((_INSTR_TAG, _INSTR_MSGID), texts)


def find_ref(*texts: str) -> str | None:
    refs = find_refs(*texts)
    return refs[0] if len(refs) == 1 else None


def new_batch_ref(now: datetime, rand4: str) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return f"B-{now.astimezone(_SAST):%m%d}-{rand4}"


def find_batch_refs(*texts: str) -> list[str]:
    return _distinct_in_order((_BATCH_TAG, _BATCH_MSGID), texts)


def find_batch_ref(*texts: str) -> str | None:
    refs = find_batch_refs(*texts)
    return refs[0] if len(refs) == 1 else None


def _normalise(value: str) -> str:
    return (value or "").strip().strip("<>").strip().lower()


def instruction_id_for(message_id: str, from_headers: Sequence[str]) -> str | None:
    mid = _normalise(message_id)
    if not mid:
        return None
    from_key: str | None = None
    if len(from_headers) == 1:
        addrs = getaddresses([from_headers[0]])
        if len(addrs) == 1 and "@" in addrs[0][1]:
            from_key = addrs[0][1].strip().lower()
    if from_key is None:
        joined = "\n".join(_normalise(h) for h in from_headers)
        from_key = "rawfrom:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return hashlib.sha256(f"{mid}|{from_key}".encode("utf-8")).hexdigest()
