"""F37: representative beneficiary id for a matched payee."""
import itertools

from invespend.beneficiary_match import (
    BeneficiaryRecord,
    TxCandidate,
    match_transaction,
)

ACCT = "9999 0000 1234"


def _b(bid, beneficiary_name=None, name=None, reference_name=None,
       account_number=ACCT, branch="123456"):
    return BeneficiaryRecord(bid, beneficiary_name, name, reference_name,
                             "Test Bank", branch, account_number)


def _tx(description):
    return TxCandidate("h1", "DEBIT", "OnlineBankingPayments", description,
                       -100.0, "external_outflow")


def _match(description, benes):
    return match_transaction(_tx(description), benes)


def test_label_match_non_hitting_beats_lower_id_hitting():
    benes = [
        _b("a", reference_name="Jos van Zyl", beneficiary_name="Huis"),
        _b("b", beneficiary_name="Jos van Zyl", reference_name="Other"),
    ]
    res = _match("JOS VAN ZYL", benes)
    assert res.status == "matched"
    assert res.beneficiary_id == "b"
    assert res.match_rule == "reference_exact"


def test_prod_like_case():
    benes = [
        _b("a", beneficiary_name="HUIS", reference_name="JOS VAN ZYL"),
        _b("b", beneficiary_name="Jos van Zyl", reference_name="Other"),
    ]
    res = _match("JOS VAN ZYL", benes)
    assert (res.status, res.beneficiary_id) == ("matched", "b")


def test_no_label_match_picks_lowest_hitting_id():
    benes = [
        _b("c", reference_name="Rent Landlord", beneficiary_name="Zed"),
        _b("a", beneficiary_name="Other", reference_name="Nothing"),
        _b("b", reference_name="Rent Landlord", beneficiary_name="Yan"),
    ]
    res = _match("Rent Landlord", benes)
    assert res.beneficiary_id == "b"


def test_beneficiary_name_tier_beats_name_tier():
    benes = [
        _b("a", name="Jos van Zyl", reference_name="Other"),
        _b("b", beneficiary_name="Jos van Zyl", reference_name="Other2"),
        _b("c", reference_name="Jos van Zyl"),
    ]
    res = _match("Jos van Zyl", benes)
    assert res.beneficiary_id == "b"


def test_name_tier_beats_plain_hitting():
    benes = [
        _b("a", reference_name="Jos van Zyl", beneficiary_name="Huis"),
        _b("b", name="Jos van Zyl", reference_name="Other"),
    ]
    assert _match("Jos van Zyl", benes).beneficiary_id == "b"


def test_short_label_never_promoted():
    benes = [
        _b("a", reference_name="Mom Gift Fund", beneficiary_name="Huis"),
        _b("b", beneficiary_name="Mom", reference_name="Other"),
    ]
    res = _match("Mom", benes)
    # "mom" is shorter than MIN_EXACT_LEN: no rule hits, no label promotion.
    assert res.status == "no_candidate"
    benes2 = [
        _b("a", reference_name="Mom Gift Fund", beneficiary_name="Huis"),
        _b("b", beneficiary_name="Mom", reference_name="Other"),
        _b("c", reference_name="Mom"),
    ]
    assert _match("Mom", benes2).status == "no_candidate"
    benes3 = [
        _b("a", reference_name="Rent Money", beneficiary_name="Huis"),
        _b("b", beneficiary_name="Rent", reference_name="Other"),
    ]
    res3 = _match("Rent Money", benes3)
    assert res3.beneficiary_id == "a"


def test_shuffle_invariance_all_permutations():
    benes = [
        _b("a", beneficiary_name="HUIS", reference_name="JOS VAN ZYL"),
        _b("b", beneficiary_name="Jos van Zyl", reference_name="Other"),
        _b("c", name="Jos van Zyl", reference_name="JOS VAN ZYL"),
        _b("d", reference_name="JOS VAN ZYL"),
    ]
    seen = {_match("JOS VAN ZYL", list(p)) for p in itertools.permutations(benes)}
    assert len(seen) == 1
    assert seen.pop().beneficiary_id == "b"


def test_ambiguity_and_counts_unchanged():
    benes = [
        _b("a", reference_name="Jos van Zyl", account_number="1111 2222 3333"),
        _b("b", reference_name="Jos van Zyl", account_number="4444 5555 6666"),
    ]
    res = _match("Jos van Zyl", benes)
    assert (res.status, res.beneficiary_id, res.candidate_count) == (
        "ambiguous", None, 2)
    none = _match("Unknown Person", benes)
    assert (none.status, none.match_rule, none.candidate_count) == (
        "no_candidate", "none", 0)


def test_status_rule_token_count_unchanged_for_matched():
    benes = [
        _b("a", beneficiary_name="HUIS", reference_name="JOS VAN ZYL"),
        _b("b", beneficiary_name="Jos van Zyl", reference_name="Other"),
    ]
    res = _match("JOS VAN ZYL", benes)
    assert (res.status, res.match_rule, res.matched_token,
            res.candidate_count) == ("matched", "reference_exact",
                                     "jos van zyl", 1)


def test_account_digits_never_in_token_or_repr():
    benes = [
        _b("a", beneficiary_name="Huis"),
        _b("b", beneficiary_name="Jos van Zyl"),
    ]
    res = _match("Pay 9999 0000 1234 Jos van Zyl", benes)
    assert res.status == "matched" and res.match_rule == "account_number"
    assert res.matched_token == "***234"
    assert "99990000" not in repr(res) and "99990000" not in repr(benes)
    assert "9999 0000 1234" not in repr(res)
