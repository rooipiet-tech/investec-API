"""Pure beneficiary matcher: label outgoing payments with a registered beneficiary.

Input: transactions (``TxCandidate``) and a beneficiary snapshot
(``BeneficiaryRecord``). Output: one ``MatchResult`` per candidate transaction.
This module is pure and deterministic: no I/O, no logging, no clock, no
randomness. The result does not depend on input ordering, dict/set iteration,
database row order or locale (``str.casefold`` and NFKC are locale-independent).

Candidates (everything else is ignored, i.e. never matched):

* ``type`` is ``DEBIT`` (case-insensitive) AND ``amount < 0``;
* ``transaction_type`` is in ``PAYMENT_TRANSACTION_TYPES`` (provisional
  allowlist; NULL or unknown types fail closed, so card purchases, fees, ATM
  withdrawals and debit orders never match);
* ``flow_type`` is not ``internal_transfer``.

Beneficiaries whose digit-normalised account number (>= 8 digits) equals one of
our own account numbers are never candidates (own-account transfers).

Normalisation: NFKC, casefold, every non-alphanumeric character becomes a
space, whitespace collapsed and stripped. A description is compared through a
fixed set of variants: the normalised text, the same with one leading bank
prefix removed (``DESCRIPTION_PREFIXES``, e.g. "payment to", "transfer to"),
and that with digit-only words removed.

Rules, highest precedence first (``RULES``):

1. ``account_number``: the beneficiary's account number (>= 8 digits) appears
   inside a SINGLE digit run of the description (digits optionally grouped by
   spaces or hyphens, e.g. "9999 0000 1234"). Digits from separate runs are
   never joined. ``matched_token`` is masked to ``***`` + last 3 digits; the
   full number never appears in a token.
2. ``reference_exact``: normalised ``referenceName`` equals a description
   variant (token length >= ``MIN_EXACT_LEN``).
3. ``beneficiary_name_exact``: same, for ``beneficiaryName``.
4. ``name_exact``: same, for ``name``.
5. ``name_prefix``: over referenceName, beneficiaryName and name, either the
   token (>= ``MIN_PREFIX_LEN`` chars) is a whole-word prefix of a variant
   ("acme trading" in "acme trading pty"), or a variant of at least
   ``MIN_PREFIX_LEN`` chars and at least 2 words is a prefix of the token (a
   truncated description, "acme trad" -> "acme trading").

Fail-closed: the FIRST rule (in the order above) that hits any beneficiary
decides. Hitting beneficiaries are first grouped by payee (``payee_key``: same
account number of >= 8 digits AND same branch code, exact stripped string; a
record without a usable account number is its own payee). Exactly one distinct
payee -> ``matched``, labelled with the lowest hitting beneficiary id of that
payee. More than one PAYEE -> ``ambiguous`` (no beneficiary id), with NO
fall-through to a weaker rule to break the tie. Pooled accounts registered under
different names collapse to the lowest hitting id (no name guard). Existing
ambiguous rows keep their old candidate_count until status or token changes. No rule hits -> ``no_candidate``. Exact beats truncation by
precedence: with beneficiaries "J Smith" and "J Smithers", the description
"J SMITH" matches J Smith (exact) and "J SMITHERS" matches J Smithers (exact);
"J SMIT" matches neither (too short). There is no fuzzy or edit-distance
matching of any kind.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Sequence

# Q2 (provisional, fail-closed): transactionType values that are payments/EFTs.
PAYMENT_TRANSACTION_TYPES: frozenset[str] = frozenset(
    {"OnlineBankingPayments", "OnlineBankingTransfers", "FasterPay"}
)

# Q5 precedence, highest confidence first.
RULES: tuple[str, ...] = (
    "account_number",
    "reference_exact",
    "beneficiary_name_exact",
    "name_exact",
    "name_prefix",
)

MIN_ACCOUNT_DIGITS = 8   # account rule / own-account guard (0007 precedent)
MIN_EXACT_LEN = 5        # normalised token length for *_exact ("mom", "rent" excluded)
MIN_PREFIX_LEN = 7       # > 6 chars for the prefix rule (0005/0007 precedent)
MIN_TRUNCATED_WORDS = 2  # a truncated description must have >= 2 words

# Leading bank boilerplate stripped from descriptions (normalised; longest first).
DESCRIPTION_PREFIXES: tuple[str, ...] = (
    "immediate payment to",
    "payshap to",
    "payment to",
    "transfer to",
    "eft to",
)

# PII allowlist: API key -> column. Nothing else is ever read from the payload.
SNAPSHOT_FIELDS: tuple[tuple[str, str], ...] = (
    ("beneficiaryId", "beneficiary_id"),
    ("beneficiaryName", "beneficiary_name"),
    ("name", "name"),
    ("referenceName", "reference_name"),
    ("bank", "bank"),
    ("code", "branch_code"),
    ("accountNumber", "account_number"),
)

STATUS_MATCHED = "matched"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_NO_CANDIDATE = "no_candidate"
NO_RULE = "none"

_NON_ALNUM = re.compile(r"[\W_]+")
_NON_DIGIT = re.compile(r"[^0-9]")
_DIGIT_RUN = re.compile(r"[0-9](?:[0-9 -]*[0-9])?")


@dataclass(frozen=True, repr=False)
class BeneficiaryRecord:
    beneficiary_id: str
    beneficiary_name: str | None
    name: str | None
    reference_name: str | None
    bank: str | None
    branch_code: str | None
    account_number: str | None

    def __repr__(self) -> str:  # never expose account digits in logs/reprs
        return (
            f"BeneficiaryRecord(beneficiary_id={self.beneficiary_id!r}, "
            f"beneficiary_name={self.beneficiary_name!r}, name={self.name!r}, "
            f"reference_name={self.reference_name!r}, bank={self.bank!r}, "
            f"branch_code={self.branch_code!r}, "
            f"account_number={mask_account(self.account_number)!r})"
        )


@dataclass(frozen=True)
class TxCandidate:
    transaction_hash: str
    type: str | None
    transaction_type: str | None
    description: str | None
    amount: Decimal | float | None
    flow_type: str | None


@dataclass(frozen=True)
class MatchResult:
    transaction_hash: str
    status: str
    beneficiary_id: str | None
    match_rule: str
    matched_token: str
    candidate_count: int


def normalise(text: str | None) -> str:
    """NFKC -> casefold -> non-alphanumerics to spaces -> collapse whitespace."""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", str(text)).casefold()
    return " ".join(_NON_ALNUM.sub(" ", folded).split())


def digits_only(text: str | None) -> str:
    """Keep ASCII digits 0-9 only."""
    if not text:
        return ""
    return _NON_DIGIT.sub("", str(text))


def mask_account(number: str | None) -> str:
    """'***' + last 3 digits, or '' when there are no digits."""
    digits = digits_only(number)
    if not digits:
        return ""
    return "***" + digits[-3:]


def description_variants(description: str | None) -> tuple[str, ...]:
    """Normalised description, minus one leading bank prefix, minus digit words."""
    norm = normalise(description)
    stripped = norm
    for prefix in DESCRIPTION_PREFIXES:
        if norm == prefix:
            stripped = ""
            break
        if norm.startswith(prefix + " "):
            stripped = norm[len(prefix) + 1:]
            break
    no_digits = " ".join(w for w in stripped.split() if not w.isdigit())
    out: list[str] = []
    for v in (norm, stripped, no_digits):
        if v and v not in out:
            out.append(v)
    return tuple(out)


def _description_digit_runs(description: str | None) -> tuple[str, ...]:
    if not description:
        return ()
    return tuple(digits_only(run) for run in _DIGIT_RUN.findall(str(description)))


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_beneficiaries(data: object) -> list[BeneficiaryRecord] | None:
    """Build snapshot records field-by-field from ``SNAPSHOT_FIELDS`` only.

    Returns None when ``data`` is not a list (treated as a fetch failure).
    Items that are not dicts or lack a ``beneficiaryId`` are skipped. Output is
    sorted and de-duplicated on ``beneficiary_id`` deterministically.
    """
    if not isinstance(data, list):
        return None
    records: list[BeneficiaryRecord] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        values = {col: _clean(item.get(key)) for key, col in SNAPSHOT_FIELDS}
        if not values["beneficiary_id"]:
            continue
        records.append(BeneficiaryRecord(**values))
    records.sort(key=_record_sort_key)
    out: list[BeneficiaryRecord] = []
    seen: set[str] = set()
    for rec in records:
        if rec.beneficiary_id in seen:
            continue
        seen.add(rec.beneficiary_id)
        out.append(rec)
    return out


def _record_sort_key(rec: BeneficiaryRecord) -> tuple[str, ...]:
    return tuple(getattr(rec, col) or "" for _, col in SNAPSHOT_FIELDS)


def eligible_beneficiaries(
    benes: Iterable[BeneficiaryRecord],
    own_account_numbers: Iterable[str | None],
) -> tuple[BeneficiaryRecord, ...]:
    """Drop beneficiaries that are one of our own accounts; sort by id."""
    own = {d for d in (digits_only(n) for n in own_account_numbers)
           if len(d) >= MIN_ACCOUNT_DIGITS}
    kept = [
        b for b in benes
        if digits_only(b.account_number) not in own
    ]
    return tuple(sorted(kept, key=_record_sort_key))


def is_candidate(tx: TxCandidate) -> bool:
    """DEBIT, negative amount, allowlisted payment type, not an internal transfer."""
    if (tx.type or "").upper() != "DEBIT":
        return False
    if tx.amount is None or not tx.amount < 0:
        return False
    if tx.transaction_type not in PAYMENT_TRANSACTION_TYPES:
        return False
    return tx.flow_type != "internal_transfer"


def _exact_hit(field: str | None, variants: tuple[str, ...]) -> str | None:
    token = normalise(field)
    if len(token) >= MIN_EXACT_LEN and token in variants:
        return token
    return None


def _prefix_hit(field: str | None, variants: tuple[str, ...]) -> str | None:
    token = normalise(field)
    if not token:
        return None
    for v in variants:
        if len(token) >= MIN_PREFIX_LEN and v.startswith(token + " "):
            return token
        if (len(v) >= MIN_PREFIX_LEN
                and len(v.split()) >= MIN_TRUNCATED_WORDS
                and token.startswith(v)):
            return token
    return None


def _rule_hit(rule: str, bene: BeneficiaryRecord, variants: tuple[str, ...],
              digit_runs: tuple[str, ...]) -> str | None:
    """The beneficiary-side token that hit under ``rule``, or None."""
    if rule == "account_number":
        digits = digits_only(bene.account_number)
        if len(digits) >= MIN_ACCOUNT_DIGITS and any(digits in run for run in digit_runs):
            return mask_account(digits)
        return None
    if rule == "reference_exact":
        return _exact_hit(bene.reference_name, variants)
    if rule == "beneficiary_name_exact":
        return _exact_hit(bene.beneficiary_name, variants)
    if rule == "name_exact":
        return _exact_hit(bene.name, variants)
    if rule == "name_prefix":
        hits = [t for t in (_prefix_hit(f, variants) for f in
                            (bene.reference_name, bene.beneficiary_name, bene.name))
                if t is not None]
        return min(hits) if hits else None
    raise ValueError(f"unknown rule {rule!r}")


def payee_key(bene: BeneficiaryRecord) -> str:
    """Identity of the real-world payee behind a beneficiary record.

    Records with a usable account number (>= ``MIN_ACCOUNT_DIGITS`` digits) share
    a payee when digits and branch code (exact, stripped string; None equals
    blank) are equal. Any other record is its own payee, keyed by its id.
    """
    digits = digits_only(bene.account_number)
    if len(digits) >= MIN_ACCOUNT_DIGITS:
        return f"acct:{digits}|{bene.branch_code or ''}"
    return f"id:{bene.beneficiary_id}"


def match_transaction(tx: TxCandidate,
                      benes: Sequence[BeneficiaryRecord]) -> MatchResult | None:
    """Match one transaction; None when it is not a candidate at all."""
    if not is_candidate(tx):
        return None
    variants = description_variants(tx.description)
    digit_runs = _description_digit_runs(tx.description)
    for rule in RULES:
        # payee key -> {hitting beneficiary id -> token}
        payees: dict[str, dict[str, str]] = {}
        for bene in benes:
            token = _rule_hit(rule, bene, variants, digit_runs)
            if token is None:
                continue
            ids = payees.setdefault(payee_key(bene), {})
            prev = ids.get(bene.beneficiary_id)
            ids[bene.beneficiary_id] = token if prev is None else min(prev, token)
        if not payees:
            continue
        if len(payees) == 1:
            (ids,) = payees.values()
            return MatchResult(tx.transaction_hash, STATUS_MATCHED, min(ids),
                               rule, min(ids.values()), 1)
        return MatchResult(tx.transaction_hash, STATUS_AMBIGUOUS, None, rule,
                           min(t for ids in payees.values() for t in ids.values()),
                           len(payees))
    return MatchResult(tx.transaction_hash, STATUS_NO_CANDIDATE, None,
                       NO_RULE, "", 0)


def _result_sort_key(r: MatchResult) -> tuple:
    return (r.transaction_hash, r.status, r.beneficiary_id or "", r.match_rule,
            r.matched_token, r.candidate_count)


def match_all(
    txs: Iterable[TxCandidate],
    benes: Iterable[BeneficiaryRecord],
    own_account_numbers: Iterable[str | None],
) -> list[MatchResult]:
    """Match every candidate transaction; sorted by transaction_hash, one per hash."""
    eligible = eligible_beneficiaries(benes, own_account_numbers)
    results = [r for r in (match_transaction(tx, eligible) for tx in txs)
               if r is not None]
    results.sort(key=_result_sort_key)
    out: list[MatchResult] = []
    for r in results:
        if out and out[-1].transaction_hash == r.transaction_hash:
            continue
        out.append(r)
    return out
