"""F4: beneficiary allowlist — exact, fail-closed, never pays a parsed account."""
from invespend.payments.beneficiaries import Beneficiary, resolve_beneficiary

ALLOW = [
    Beneficiary(beneficiary_id="BEN_ACME", name="Acme", match_names=["acme"]),
    Beneficiary(beneficiary_id="BEN_BETA", name="Beta", match_names=["beta"]),
]


def test_exact_match_returns_registered_id():
    b = resolve_beneficiary("Acme Trading", ALLOW)
    assert b is not None
    assert b.beneficiary_id == "BEN_ACME"


def test_no_match_fails_closed():
    assert resolve_beneficiary("Unknown Payee", ALLOW) is None


def test_empty_payee_fails_closed():
    assert resolve_beneficiary("", ALLOW) is None
    assert resolve_beneficiary("   ", ALLOW) is None


def test_ambiguous_multi_match_fails_closed():
    allow = [
        Beneficiary(beneficiary_id="A", name="A", match_names=["acme"]),
        Beneficiary(beneficiary_id="B", name="B", match_names=["acme"]),
    ]
    assert resolve_beneficiary("Acme", allow) is None


def test_resolution_never_returns_parsed_account_string():
    # A parsed account number is never a beneficiary; it cannot resolve.
    assert resolve_beneficiary("10010900709", ALLOW) is None


def test_default_allowlist_is_empty_fail_closed():
    # With no registered payees every resolution fails closed.
    assert resolve_beneficiary("Acme", []) is None
