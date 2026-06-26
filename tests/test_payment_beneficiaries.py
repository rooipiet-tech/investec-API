"""F4: beneficiary allowlist — exact, fail-closed, never pays a parsed account."""
from invespend.payments.beneficiaries import (
    Beneficiary,
    from_api,
    resolve_beneficiary,
)

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


# ── from_api mapping ─────────────────────────────────────────────────────────
def test_from_api_maps_fields_and_derives_last3():
    raw = [{
        "beneficiaryId": "B1",
        "beneficiaryName": "Acme Pty Ltd",
        "accountNumber": "10010900709",
        "emailAddress": "Pay@Acme.CO.ZA",
    }]
    out = from_api(raw)
    assert len(out) == 1
    b = out[0]
    assert b.beneficiary_id == "B1"
    assert b.name == "Acme Pty Ltd"
    assert b.account_last3 == "709"
    assert b.email == "pay@acme.co.za"  # lower-cased


def test_from_api_name_falls_back_to_name_key():
    out = from_api([{"beneficiaryId": "B1", "name": "Beta"}])
    assert out[0].name == "Beta"
    assert out[0].account_last3 == ""  # no accountNumber
    assert out[0].email == ""


def test_from_api_skips_entries_without_beneficiary_id():
    raw = [
        {"beneficiaryName": "NoId"},
        {"beneficiaryId": "", "beneficiaryName": "Empty"},
        {"beneficiaryId": "B2", "beneficiaryName": "Ok"},
    ]
    out = from_api(raw)
    assert [b.beneficiary_id for b in out] == ["B2"]


def test_from_api_empty_or_none_yields_empty_allowlist():
    assert from_api([]) == []
    assert from_api(None) == []


# ── resolution against an API-built allowlist (exact name/email, fail-closed) ──
_API_RAW = [
    {"beneficiaryId": "B1", "beneficiaryName": "Acme", "emailAddress": "pay@acme.co.za"},
    {"beneficiaryId": "B2", "beneficiaryName": "Beta", "emailAddress": "pay@beta.co.za"},
]


def test_api_allowlist_exact_name_resolves():
    allow = from_api(_API_RAW)
    b = resolve_beneficiary("acme", allow)  # case-insensitive exact name
    assert b is not None and b.beneficiary_id == "B1"


def test_api_allowlist_exact_email_resolves():
    allow = from_api(_API_RAW)
    b = resolve_beneficiary("PAY@ACME.CO.ZA", allow)  # exact email
    assert b is not None and b.beneficiary_id == "B1"


def test_api_allowlist_substring_does_not_match():
    allow = from_api(_API_RAW)
    # Fuzzy/substring on the name must NOT resolve (exact only).
    assert resolve_beneficiary("Acme Trading Pty", allow) is None


def test_api_allowlist_no_match_fails_closed():
    allow = from_api(_API_RAW)
    assert resolve_beneficiary("Unknown", allow) is None


def test_api_allowlist_ambiguous_multi_match_fails_closed():
    raw = [
        {"beneficiaryId": "B1", "beneficiaryName": "Acme"},
        {"beneficiaryId": "B2", "beneficiaryName": "Acme"},
    ]
    assert resolve_beneficiary("Acme", from_api(raw)) is None
