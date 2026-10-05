"""Payee collapse tests (F33-F36). Synthetic data only."""
import itertools

from invespend.beneficiary_match import (
    BeneficiaryRecord,
    TxCandidate,
    match_transaction,
    payee_key,
)

ACCT = "9999 0000 1234"


def _b(bid, name=None, acct=None, branch=None):
    return BeneficiaryRecord(bid, name, None, None, "Test Bank", branch, acct)


def _tx(desc):
    return TxCandidate("h1", "DEBIT", "OnlineBankingPayments", desc, -10.0,
                       "external_outflow")


def test_same_account_branch_matches_lowest_hitting_id():
    r = match_transaction(_tx("Acme Trading"), [
        _b("b2", "Acme Trading", ACCT, "100"), _b("b1", "Acme Trading", ACCT, "100")])
    assert (r.status, r.beneficiary_id, r.candidate_count) == ("matched", "b1", 1)


def test_lowest_hitting_id_not_lower_nonhitting():
    benes = [_b("a0", "Other Person", ACCT, "100"),
             _b("b1", "Acme Trading", ACCT, "100"),
             _b("b2", "Acme Trading", ACCT, "100")]
    r = match_transaction(_tx("Acme Trading"), benes)
    assert r.status == "matched" and r.beneficiary_id == "b1"


def test_same_name_different_accounts_ambiguous():
    r = match_transaction(_tx("Acme Trading"), [
        _b("b1", "Acme Trading", "11112222", "1"), _b("b2", "Acme Trading", "33334444", "1")])
    assert (r.status, r.beneficiary_id, r.candidate_count) == ("ambiguous", None, 2)


def test_blank_and_short_accounts_never_merged():
    for acct in (None, "", "1234567"):
        r = match_transaction(_tx("Acme Trading"), [
            _b("b1", "Acme Trading", acct), _b("b2", "Acme Trading", acct)])
        assert r.status == "ambiguous" and r.candidate_count == 2


def test_branch_differs_ambiguous_none_equals_blank():
    r = match_transaction(_tx("Acme Trading"), [
        _b("b1", "Acme Trading", ACCT, "100"), _b("b2", "Acme Trading", ACCT, "200")])
    assert r.status == "ambiguous"
    r = match_transaction(_tx("Acme Trading"), [
        _b("b1", "Acme Trading", ACCT, None), _b("b2", "Acme Trading", ACCT, "")])
    assert r.status == "matched" and r.beneficiary_id == "b1"


def test_two_ids_one_payee_plus_other_payee_counts_payees():
    r = match_transaction(_tx("Acme Trading"), [
        _b("p1", "Acme Trading", "11112222", "1"), _b("p2", "Acme Trading", "11112222", "1"),
        _b("q1", "Acme Trading", "33334444", "1")])
    assert (r.status, r.candidate_count) == ("ambiguous", 2)


def test_shuffle_invariance():
    benes = [_b("b1", "Acme Trading", ACCT, "1"), _b("b2", "Acme Trading", ACCT, "1"),
             _b("b3", "Acme Trading", "55556666", "1"), _b("b4", "Acme Trading")]
    results = {match_transaction(_tx("Acme Trading"), list(p))
               for p in itertools.permutations(benes)}
    assert len(results) == 1
    only_dups = {match_transaction(_tx("Acme Trading"), list(p))
                 for p in itertools.permutations(benes[:2])}
    assert len(only_dups) == 1


def test_account_rule_collapses_and_never_leaks_digits():
    r = match_transaction(_tx("Pay 9999 0000 1234 now"), [
        _b("b2", "Zed", ACCT, "1"), _b("b1", "Yan", ACCT, "1")])
    assert r.status == "matched" and r.beneficiary_id == "b1"
    assert r.match_rule == "account_number" and r.matched_token == "***234"
    assert "99990000" not in repr(r) and "99990000" not in repr(_b("b1", acct=ACCT))


def test_pooled_account_different_names_collapse_to_lowest_id():
    # Pinned (frozen F33): no name guard; label is the lowest hitting id.
    r = match_transaction(_tx("Pool Savings"), [
        _b("b2", "Pool Savings", ACCT, "1"), _b("b1", "Pool Savings", ACCT, "1"),
        _b("b0", "Unrelated Name", ACCT, "1")])
    assert r.status == "matched" and r.beneficiary_id == "b1"


def test_payee_key_shape():
    assert payee_key(_b("x", acct=ACCT, branch="7")) == "acct:999900001234|7"
    assert payee_key(_b("x", acct="123")) == "id:x"
