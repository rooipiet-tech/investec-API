"""Pure matcher tests (beneficiary matching). Synthetic data only."""
import itertools
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from invespend import beneficiary_match as bm
from invespend.beneficiary_match import (
    PAYMENT_TRANSACTION_TYPES,
    RULES,
    BeneficiaryRecord,
    MatchResult,
    TxCandidate,
    description_variants,
    digits_only,
    eligible_beneficiaries,
    is_candidate,
    mask_account,
    match_all,
    match_transaction,
    normalise,
    parse_beneficiaries,
)

FIXTURES = Path(__file__).parent / "fixtures" / "beneficiary"
GOLDEN = json.loads((FIXTURES / "golden_descriptions.json").read_text())["cases"]
FAKE_ACCT = "9999 0000 1234"


def _bene(bid, beneficiary_name=None, name=None, reference_name=None,
          account_number=None, bank="Test Bank"):
    return BeneficiaryRecord(bid, beneficiary_name, name, reference_name, bank,
                             None, account_number)


def _tx(description, *, h="h1", type_="DEBIT", transaction_type="OnlineBankingPayments",
        amount=-100.0, flow_type="external_outflow"):
    return TxCandidate(h, type_, transaction_type, description, amount, flow_type)


# ── normalisation (F12) ─────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("ACME Trading", "acme trading"),
    ("  Acme   Trading \t\n", "acme trading"),
    ("Acme-Trading (Pty) Ltd.", "acme trading pty ltd"),
    ("O'Brien & Sons", "o brien sons"),
    ("ＡＣＭＥ", "acme"),                   # NFKC full-width
    ("STRASSE", "strasse"),
    ("Straße", "strasse"),                  # casefold
    ("under_score", "under score"),
    (None, ""),
    ("", ""),
    ("---", ""),
])
def test_normalise(raw, expected):
    assert normalise(raw) == expected


def test_digits_and_mask():
    assert digits_only("9999 0000-1234") == "999900001234"
    assert digits_only(None) == ""
    assert mask_account(FAKE_ACCT) == "***234"
    assert mask_account(None) == ""
    assert mask_account("abc") == ""


def test_description_variants_fixed_order():
    assert description_variants("PAYMENT TO Acme Trading 12345") == (
        "payment to acme trading 12345", "acme trading 12345", "acme trading")
    assert description_variants("Acme") == ("acme",)
    assert description_variants(None) == ()
    # Longest prefix wins ("immediate payment to" before "payment to").
    assert description_variants("Immediate Payment to Acme")[1] == "acme"


# ── determinism (F12) ───────────────────────────────────────────────────────

def _perm_fixture():
    benes = [
        _bene("ben-1", "Acme Trading"),
        _bene("ben-2", "Acme Trading Two"),
        _bene("ben-3", "Dup Payee"),
        _bene("ben-4", "Dup Payee"),
        _bene("ben-5", "Someone", account_number=FAKE_ACCT),
    ]
    txs = [
        _tx("ACME TRAD", h="h1"),
        _tx("ACME TRADING", h="h2"),
        _tx("DUP PAYEE", h="h3"),
        _tx("Transfer to 9999 0000 1234", h="h4"),
        _tx("Nobody", h="h5"),
    ]
    return txs, benes


def test_permutation_invariance():
    txs, benes = _perm_fixture()
    expected = match_all(txs, benes, [])
    assert [r.transaction_hash for r in expected] == ["h1", "h2", "h3", "h4", "h5"]
    for tp in itertools.permutations(txs):
        for bp in itertools.permutations(benes):
            assert match_all(list(tp), list(bp), []) == expected


def test_module_has_no_clock_random_fuzzy():
    src = Path(bm.__file__).read_text()
    imported = set(re.findall(r"^\s*(?:from|import)\s+([\w.]+)", src, re.M))
    assert imported <= {"__future__", "re", "unicodedata", "dataclasses", "decimal",
                        "typing"}, imported
    for banned in (r"\bdatetime\b", r"\brandom\.", r"\bdifflib\b", r"SequenceMatcher",
                   r"(?i)levenshtein", r"\.now\(", r"\btime\.", r"\blogging\.",
                   r"\bopen\(", r"\bprint\("):
        assert not re.search(banned, src), banned


def test_rules_order_is_fixed():
    assert RULES == ("account_number", "reference_exact", "beneficiary_name_exact",
                     "name_exact", "name_prefix")


# ── DEBIT-only (F13) ────────────────────────────────────────────────────────

def test_credit_never_matches():
    benes = [_bene("ben-a", "Acme Trading", reference_name="Acme Trading")]
    assert match_transaction(_tx("Acme Trading", type_="CREDIT", amount=100.0), benes) is None
    assert match_transaction(_tx("Acme Trading", type_="CREDIT", amount=-100.0), benes) is None
    assert match_transaction(_tx("Acme Trading", type_="DEBIT", amount=100.0), benes) is None
    assert match_transaction(_tx("Acme Trading", amount=None), benes) is None
    assert match_all([_tx("Acme Trading", type_="CREDIT", amount=5.0)], benes, []) == []


def test_debit_matches():
    benes = [_bene("ben-a", "Acme Trading")]
    r = match_transaction(_tx("Acme Trading", type_="debit", amount=Decimal("-5.00")), benes)
    assert r.status == "matched" and r.beneficiary_id == "ben-a"


# ── payment-type gate (F14) ─────────────────────────────────────────────────

@pytest.mark.parametrize("ttype, matches", [
    ("OnlineBankingPayments", True),
    ("OnlineBankingTransfers", True),
    ("FasterPay", True),
    ("CardPurchases", False),
    ("FeesAndInterest", False),
    ("ATMWithdrawals", False),
    ("DebitOrders", False),
    (None, False),
    ("Bogus", False),
    ("onlinebankingpayments", False),
])
def test_payment_type_gate(ttype, matches):
    benes = [_bene("ben-a", "Acme Trading")]
    r = match_transaction(_tx("ACME TRADING", transaction_type=ttype), benes)
    if matches:
        assert r is not None and r.status == "matched"
    else:
        assert r is None


def test_allowlist_is_single_constant():
    assert PAYMENT_TRANSACTION_TYPES == frozenset(
        {"OnlineBankingPayments", "OnlineBankingTransfers", "FasterPay"})
    src = Path(bm.__file__).read_text()
    assert src.count('"OnlineBankingPayments"') == 1
    assert len(re.findall(r"^PAYMENT_TRANSACTION_TYPES\b", src, re.M)) == 1


# ── golden fixtures: fail-closed, shapes, explainability (F15/F16/F27) ──────

@pytest.mark.parametrize("case", GOLDEN, ids=[c["id"] for c in GOLDEN])
def test_golden_shapes(case):
    benes = parse_beneficiaries(case["beneficiaries"])
    r = match_transaction(_tx(case["description"]), benes)
    exp = case["expect"]
    assert r == MatchResult("h1", exp["status"], exp["beneficiary_id"], exp["match_rule"],
                            exp["matched_token"], exp["candidate_count"])
    if r.status != "no_candidate":
        assert r.match_rule in RULES
        assert r.matched_token
    # A full account number never appears in a token.
    for b in benes:
        d = digits_only(b.account_number)
        if d:
            assert d not in digits_only(r.matched_token) or len(digits_only(r.matched_token)) <= 3


def test_golden_covers_required_shapes():
    ids = {c["id"] for c in GOLDEN}
    for required in ("uppercase", "truncated", "transfer_to_prefix", "payment_to_prefix",
                     "digit_grouped_account", "ambiguous_same_name", "renamed", "deleted",
                     "exact_beats_truncation_smith", "exact_beats_truncation_acme",
                     "one_word_generic_truncation", "account_digits_cross_boundary"):
        assert required in ids


def test_no_fall_through_on_ambiguity():
    benes = [
        _bene("ben-a", reference_name="Shared Ref"),
        _bene("ben-b", reference_name="Shared Ref"),
        _bene("ben-c", name="Shared Ref"),
    ]
    r = match_transaction(_tx("SHARED REF"), benes)
    assert r.status == "ambiguous"
    assert r.match_rule == "reference_exact"
    assert r.beneficiary_id is None and r.candidate_count == 2


def test_precedence():
    benes = [_bene("ben-a", reference_name="Acme Trading"),
             _bene("ben-b", beneficiary_name="Acme Trading")]
    r = match_transaction(_tx("ACME TRADING"), benes)
    assert (r.status, r.beneficiary_id, r.match_rule) == ("matched", "ben-a", "reference_exact")


def test_account_rule_beats_names_and_is_masked():
    benes = [_bene("ben-a", "Acme Trading", account_number=FAKE_ACCT),
             _bene("ben-b", "Acme Trading")]
    r = match_transaction(_tx("ACME TRADING 9999 0000 1234"), benes)
    assert (r.status, r.beneficiary_id, r.match_rule, r.matched_token) == (
        "matched", "ben-a", "account_number", "***234")


def test_account_rule_min_digits():
    benes = [_bene("ben-a", "Zed", account_number="1234567")]  # 7 digits: too short
    r = match_transaction(_tx("Payment 1234567"), benes)
    assert r.status == "no_candidate"


# ── own-account exclusion (F18) ─────────────────────────────────────────────

def test_own_account_excluded():
    own = ["99990000123"]
    benes = [_bene("ben-own", "My Savings", account_number="9999 0000 123"),
             _bene("ben-ext", "My Other Bank", account_number="9999 1111 222")]
    assert [b.beneficiary_id for b in eligible_beneficiaries(benes, own)] == ["ben-ext"]
    res = match_all([_tx("Transfer to 9999 0000 123", h="h1"),
                     _tx("MY SAVINGS", h="h2"),
                     _tx("MY OTHER BANK", h="h3")], benes, own)
    by_hash = {r.transaction_hash: r for r in res}
    assert by_hash["h1"].status == "no_candidate"
    assert by_hash["h2"].status == "no_candidate"
    assert by_hash["h3"].status == "matched" and by_hash["h3"].beneficiary_id == "ben-ext"


def test_internal_transfer_excluded():
    benes = [_bene("ben-a", "Acme Trading")]
    assert match_transaction(_tx("ACME TRADING", flow_type="internal_transfer"), benes) is None
    assert is_candidate(_tx("x", flow_type=None))


# ── snapshot parsing / PII allowlist (F19/F20/F28) ──────────────────────────

def test_parse_beneficiaries_allowlist():
    data = [{
        "beneficiaryId": " ben-a ", "beneficiaryName": "Acme Trading", "name": "Acme",
        "referenceName": "Acme Ref", "bank": "Test Bank", "code": "000000",
        "accountNumber": FAKE_ACCT, "cellNo": "0000000000",
        "emailAddress": "nobody@example.invalid", "referenceAccountNumber": "REF-1",
        "extra": {"nested": True},
    }]
    recs = parse_beneficiaries(data)
    assert len(recs) == 1
    rec = recs[0]
    assert set(vars(rec)) == {col for _, col in bm.SNAPSHOT_FIELDS}
    assert rec.beneficiary_id == "ben-a" and rec.branch_code == "000000"
    assert rec.account_number == FAKE_ACCT  # Q1: full number kept in the snapshot
    text = repr(rec)
    assert "1234" not in text.replace("***234", "") and "99990000" not in text
    assert "0000000000" not in text and "example.invalid" not in text


def test_parse_beneficiaries_skips_bad_items_and_dedupes():
    data = [{"beneficiaryId": "b"}, "junk", 5, {"name": "no id"},
            {"beneficiaryId": ""}, {"beneficiaryId": "a", "name": "Z"},
            {"beneficiaryId": "a", "name": "Y"}]
    recs = parse_beneficiaries(data)
    assert [r.beneficiary_id for r in recs] == ["a", "b"]
    assert recs[0].name == "Y"  # deterministic: smallest full tuple wins
    assert parse_beneficiaries(list(reversed(data))) == recs


@pytest.mark.parametrize("data", [{"data": []}, None, "x", 5, {"beneficiaryId": "a"}])
def test_parse_beneficiaries_malformed(data):
    assert parse_beneficiaries(data) is None
