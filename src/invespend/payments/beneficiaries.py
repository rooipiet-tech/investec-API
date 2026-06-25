"""Beneficiary allowlist — the ONLY thing that can be paid (F4).

Mirrors the ``groups.py`` code-config pattern: the allowlist lives in code, not
in env/secrets. Execution targets an exact pre-registered ``beneficiary_id``; a
parsed account number from an attachment is NEVER paid directly and is never
returned from resolution.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Beneficiary:
    """A pre-registered payee. ``beneficiary_id`` is the Investec-side id that is
    the ONLY value ever passed to execution. ``match_names`` are case-insensitive
    substrings used to map an extracted payee name to this allowlist entry."""

    beneficiary_id: str
    name: str
    match_names: list[str] = field(default_factory=list)
    # last-3 of the beneficiary account, for audit/display only — never the full PAN.
    account_last3: str = ""


# Edit this list to register payees. Empty by default: with no allowlist every
# payment fails closed (F4/F5).
ALLOWLIST: list[Beneficiary] = []


def resolve_beneficiary(
    payee_name: str, allowlist: list[Beneficiary] | None = None
) -> Beneficiary | None:
    """Resolve an extracted payee name to exactly one allowlisted beneficiary.

    Zero or more-than-one match -> ``None`` (fail-closed, F4/F5). Returns the
    registered :class:`Beneficiary`; the caller pays ``.beneficiary_id`` and
    NEVER a parsed account number.
    """
    if not payee_name:
        return None
    entries = ALLOWLIST if allowlist is None else allowlist
    needle = payee_name.strip().lower()
    if not needle:
        return None
    matches = [
        b
        for b in entries
        if any(m.strip().lower() and m.strip().lower() in needle for m in b.match_names)
    ]
    if len(matches) == 1:
        return matches[0]
    return None  # 0 or >1 -> fail closed
