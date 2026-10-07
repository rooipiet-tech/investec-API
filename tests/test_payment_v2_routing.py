"""S11: reconcile / route / fingerprints / reverify (pure, offline)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tests.v2_harness import ACME_RAW, BETA_RAW, KEY, T0, mod

pytestmark = pytest.mark.xfail(strict=False, reason="S11 red: routing/execute/fingerprints not built yet")


def cand(amount=None, currency=None, payee=None, origin="typed"):
    return mod("routing").Candidate(amount, currency, payee, origin)


def reconcile(*cands):
    return mod("routing").reconcile(list(cands))


# ----------------------------------------------------------------- reconcile
def test_typed_amount_and_payee_reconcile_ok():
    r = reconcile(cand("100.00", "ZAR"), cand(payee="Acme Trading"))
    assert (r.status, r.amount, r.currency, r.payee, r.image_derived, r.figures_source) == \
        ("ok", "100.00", "ZAR", "Acme Trading", False, "typed")


def test_no_amount_is_none_and_no_payee_is_none():
    assert reconcile(cand(payee="Acme")).status == "none"
    assert reconcile(cand("100.00", "ZAR")).status == "none"
    assert reconcile().status == "none"


def test_any_disagreement_on_amount_payee_or_currency_is_conflict():
    assert reconcile(cand("100.00", "ZAR"), cand("200.00", "ZAR"), cand(payee="Acme")).status == "conflict"
    assert reconcile(cand("100.00", "ZAR"), cand(payee="Acme"), cand(payee="Beta")).status == "conflict"
    assert reconcile(cand("100.00", "ZAR"), cand("100.00", "USD", origin="attachment"), cand(payee="Acme")).status == "conflict"


def test_payee_comparison_is_case_and_space_insensitive():
    r = reconcile(cand("100.00", "ZAR"), cand(payee="Acme  Trading"), cand(payee="acme trading", origin="attachment"))
    assert r.status == "ok"


def test_image_dollar_amount_conflicts_with_zar_body():
    r = reconcile(cand("100.00", "ZAR"), cand(payee="Acme"), cand("100.00", "USD", origin="image"))
    assert r.status == "conflict" and r.reason


def test_image_only_non_zar_currency_parks():
    for currency in ("USD", None, "EUR"):
        r = reconcile(cand("100.00", currency, origin="image"), cand(payee="Acme", origin="image"))
        assert r.status == "conflict" and r.reason == "currency_not_zar", currency


def test_reconciled_currency_must_be_zar():
    r = reconcile(cand("100.00", "USD", origin="attachment"), cand(payee="Acme"))
    assert r.status == "conflict" and r.reason == "currency_not_zar"
    assert reconcile(cand("100.00", "ZAR", origin="attachment"), cand(payee="Acme")).status == "ok"


def test_image_amount_differs_from_body_amount_conflicts():
    r = reconcile(cand("100.00", "ZAR"), cand(payee="Acme"), cand("150.00", "ZAR", origin="image"))
    assert r.status == "conflict"


def test_image_only_amount_is_image_derived_and_figures_source_image():
    r = reconcile(cand("100.00", "ZAR", origin="image"), cand(payee="Acme"))
    assert r.status == "ok" and r.image_derived is True and r.figures_source == "image"


def test_typed_amount_with_image_only_payee_is_batched_with_figures_source_image():
    r = reconcile(cand("100.00", "ZAR"), cand(payee="Acme", origin="image"))
    assert r.status == "ok" and r.image_derived is True and r.figures_source == "image"


def test_typed_amount_and_typed_payee_with_image_corroboration_is_typed():
    r = reconcile(cand("100.00", "ZAR"), cand(payee="Acme"), cand("100.00", "ZAR", origin="image"),
                  cand(payee="ACME", origin="image"))
    assert r.status == "ok" and r.image_derived is False and r.figures_source == "typed"


def test_attachment_only_amount_has_figures_source_attachment():
    r = reconcile(cand("100.00", "ZAR", origin="attachment"), cand(payee="Acme"))
    assert r.status == "ok" and r.figures_source == "attachment" and r.image_derived is False


def test_forwarded_and_quoted_text_count_as_typed_source():
    for origin in ("forwarded", "quoted"):
        assert reconcile(cand("100.00", "ZAR", origin=origin), cand(payee="Acme", origin=origin)).figures_source == "typed"


# --------------------------------------------------------------------- route
def benes():
    from invespend.payments import beneficiaries
    return beneficiaries.from_api([ACME_RAW, BETA_RAW])


def rec_ok(payee="Acme Trading", amount="100.00"):
    return mod("routing").Reconciled("ok", amount, "ZAR", payee, False, "typed", "")


def route(reconciled=None, beneficiaries=..., observations=None, *, hold_recent=True, hold=timedelta(0), cap=20000.0, now=T0):
    r = mod("routing")
    return r.route(reconciled or rec_ok(), benes() if beneficiaries is ... else beneficiaries, observations or {},
                   hold_recent=hold_recent, hold=hold, per_payment_cap=cap, now=now)


def test_registered_exact_name_is_ready():
    result = route()
    assert result.kind == "ready" and result.path == "registered" and result.beneficiary.beneficiary_id == "ben-acme"


def test_unknown_payee_is_new_payee_path_new_payee():
    result = route(rec_ok("Nobody Ltd"))
    assert result.kind == "new_payee" and result.path == "new_payee" and result.beneficiary is None


def test_beneficiaries_none_parks_list_unavailable_not_new_payee():
    result = route(beneficiaries=None)
    assert (result.kind, result.reason, result.path) == ("park", "beneficiary_list_unavailable", "new_payee")


def test_ambiguous_name_parks_never_new_payee():
    from invespend.payments import beneficiaries
    dup = beneficiaries.from_api([ACME_RAW, {**ACME_RAW, "beneficiaryId": "ben-acme2"}])
    result = route(beneficiaries=dup)
    assert (result.kind, result.reason, result.path) == ("park", "beneficiary_ambiguous", "new_payee")


def test_over_per_payment_cap_parks_and_is_never_offered():
    result = route(rec_ok(amount="20000.01"))
    assert (result.kind, result.reason, result.path) == ("park", "over_per_payment_cap", "registered")
    assert route(rec_ok(amount="20000.00")).kind == "ready"
    assert route(rec_ok(amount="1.00"), cap=0).kind == "park"                          # cap unset: fail closed


def test_hold_zero_makes_registered_recent_flag_inert():
    obs = {"ben-acme": mod("instructions").BeneficiaryObservation(T0, False, "f", None)}
    assert route(observations=obs, hold=timedelta(0), hold_recent=True).kind == "ready"


def test_recent_registered_beneficiary_held_when_hold_nonzero():
    I = mod("instructions").BeneficiaryObservation
    fresh = {"ben-acme": I(T0 - timedelta(hours=1), False, "f", None)}
    result = route(observations=fresh, hold=timedelta(hours=24), now=T0)
    assert result.kind == "held" and result.recent and result.eligible_at == T0 - timedelta(hours=1) + timedelta(hours=24)
    old = {"ben-acme": I(T0 - timedelta(hours=48), False, "f", None)}
    assert route(observations=old, hold=timedelta(hours=24)).kind == "ready"
    established = {"ben-acme": I(T0 - timedelta(hours=1), True, "f", None)}
    assert route(observations=established, hold=timedelta(hours=24)).kind == "ready"          # bootstrap beneficiaries
    assert route(observations=fresh, hold=timedelta(hours=24), hold_recent=False).kind == "ready"


def test_fingerprint_change_on_established_beneficiary_is_recent_held_first_seen_unchanged():
    I = mod("instructions").BeneficiaryObservation
    obs = {"ben-acme": I(T0 - timedelta(days=30), True, "f2", T0 - timedelta(hours=2))}
    result = route(observations=obs, hold=timedelta(hours=24))
    assert result.kind == "held" and result.eligible_at == T0 - timedelta(hours=2) + timedelta(hours=24)


def test_beneficiary_path_helper_matches_route():
    path = mod("routing").beneficiary_path
    assert path("Acme Trading", benes())[0] == "registered"
    assert path("Nobody", benes())[0] == "new_payee"
    assert path("Acme Trading", None)[0] == "new_payee"


# -------------------------------------------------------------- fingerprints
def test_fingerprint_changes_when_only_account_number_changes():
    f = mod("fingerprints")
    base = f.beneficiary_fingerprint(ACME_RAW, KEY)
    assert base == f.beneficiary_fingerprint(dict(ACME_RAW), KEY) and len(base) == 64
    assert base != f.beneficiary_fingerprint({**ACME_RAW, "accountNumber": "1234567891"}, KEY)
    assert base != f.beneficiary_fingerprint({**ACME_RAW, "code": "250656"}, KEY)
    assert base != f.beneficiary_fingerprint({**ACME_RAW, "beneficiaryName": "Acme Trading 2"}, KEY)
    assert base != f.beneficiary_fingerprint(ACME_RAW, KEY + "x")
    assert "1234567890" not in base


def test_account_hmac_is_keyed_digits_only():
    f = mod("fingerprints")
    assert f.account_hmac("1234 5678-90", KEY) == f.account_hmac("1234567890", KEY) != f.account_hmac("1234567891", KEY)
    assert f.account_hmac("1234567890", KEY) != f.account_hmac("1234567890", "other")
    with pytest.raises(ValueError):
        f.account_hmac("1234567890", "")


# ------------------------------------------------------------------- reverify
def stored(**over):
    f = mod("fingerprints")
    row = {"beneficiary_id": "ben-acme", "payee_name_norm": "acme trading", "beneficiary_fingerprint": f.beneficiary_fingerprint(ACME_RAW, KEY)}
    row.update(over)
    return row


def reverify(row=None, raw=None, key=KEY):
    from invespend.payments import beneficiaries
    raw = [ACME_RAW, BETA_RAW] if raw is None else raw
    return mod("execute").reverify_beneficiary(row or stored(), None if raw is False else beneficiaries.from_api(raw),
                                               None if raw is False else raw, key)


def test_reverify_ok_when_unchanged():
    assert reverify() is None


def test_reverify_failures_are_park_reason_codes():
    assert reverify(raw=False) == "beneficiary_list_unavailable"
    assert reverify(key="") == "fingerprint_key_missing"
    assert reverify(raw=[BETA_RAW]) == "beneficiary_removed"
    assert reverify(raw=[{**ACME_RAW, "accountNumber": "1234567891"}, BETA_RAW]) == "beneficiary_changed"
    assert reverify(raw=[{**ACME_RAW, "beneficiaryName": "Acme Trading Co"}, BETA_RAW]) == "beneficiary_changed"
    assert reverify(raw=[ACME_RAW, {**ACME_RAW, "beneficiaryId": "ben-acme2"}]) == "beneficiary_ambiguous"
    assert reverify(stored(beneficiary_id=None)) == "beneficiary_missing"
    assert reverify(stored(beneficiary_fingerprint=None)) == "beneficiary_changed"
