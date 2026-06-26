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
    substrings used to map an extracted payee name to this allowlist entry.
    ``email`` (when present, e.g. from the API) supports exact-email matching."""

    beneficiary_id: str
    name: str
    match_names: list[str] = field(default_factory=list)
    # last-3 of the beneficiary account, for audit/display only — never the full PAN.
    account_last3: str = ""
    # Lower-cased beneficiary email (API-sourced), for exact-email matching only.
    email: str = ""


# Edit this list to register payees. Empty by default: with no allowlist every
# payment fails closed (F4/F5).
ALLOWLIST: list[Beneficiary] = []


def from_api(raw: list[dict]) -> list[Beneficiary]:
    """Build the allowlist from the live Investec beneficiaries list.

    Maps each Investec beneficiary dict to a :class:`Beneficiary`. Entries with
    no ``beneficiaryId`` are skipped (cannot be paid -> fail-closed). A failed or
    empty fetch yields an empty allowlist, which fails every resolution closed.
    """
    out: list[Beneficiary] = []
    for r in raw or []:
        bid = r.get("beneficiaryId")
        if not bid:
            continue  # un-payable -> never allowlisted (fail-closed)
        name = r.get("beneficiaryName") or r.get("name") or ""
        digits = "".join(ch for ch in str(r.get("accountNumber") or "") if ch.isdigit())
        out.append(
            Beneficiary(
                beneficiary_id=str(bid),
                name=name,
                account_last3=digits[-3:] if digits else "",
                email=(r.get("emailAddress") or "").lower(),
            )
        )
    return out


def resolve_beneficiary(
    payee_name: str, allowlist: list[Beneficiary] | None = None
) -> Beneficiary | None:
    """Resolve an extracted payee/sender to exactly one allowlisted beneficiary.

    Matching is EXACT only: case-insensitive name equality, exact email equality,
    or (for static code-config entries) a case-insensitive ``match_names``
    substring. Zero or more-than-one candidate -> ``None`` (fail-closed, F4/F5).
    Never fuzzy. Returns the registered :class:`Beneficiary`; the caller pays
    ``.beneficiary_id`` and NEVER a parsed account number.
    """
    if not payee_name:
        return None
    entries = ALLOWLIST if allowlist is None else allowlist
    needle = payee_name.strip().lower()
    if not needle:
        return None
    matches = [b for b in entries if _matches(b, needle)]
    if len(matches) == 1:
        return matches[0]
    return None  # 0 or >1 -> fail closed


def _matches(b: Beneficiary, needle: str) -> bool:
    """Exact name equality OR exact email equality OR a static match_names
    substring. Never substring/fuzzy on the name itself."""
    if b.name and b.name.strip().lower() == needle:
        return True
    if b.email and b.email.strip().lower() == needle:
        return True
    return any(
        m.strip().lower() and m.strip().lower() in needle for m in b.match_names
    )
