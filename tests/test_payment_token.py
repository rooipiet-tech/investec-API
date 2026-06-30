"""F2/F10: HMAC token — bound, single-use(nonce), expiring, constant-time."""
import hmac
import time

import pytest

from invespend.payments import token as token_mod


def _issue(**over):
    base = dict(
        source_account_id="ACC1",
        amount="100.00",
        beneficiary_id="BEN1",
        dedup_key="dk",
        nonce="nonce-1",
    )
    base.update(over)
    return token_mod.issue_token("sekret", **base)


def test_valid_token_verifies():
    tok, claim = _issue()
    out = token_mod.verify_token(
        "sekret", tok,
        source_account_id="ACC1", amount="100.00",
        beneficiary_id="BEN1", dedup_key="dk",
    )
    assert out is not None
    assert out.nonce == "nonce-1"


def test_wrong_secret_rejected():
    tok, _ = _issue()
    assert token_mod.verify_token(
        "WRONG", tok,
        source_account_id="ACC1", amount="100.00",
        beneficiary_id="BEN1", dedup_key="dk",
    ) is None


@pytest.mark.parametrize("field,value", [
    ("source_account_id", "OTHER"),
    ("amount", "999.00"),
    ("beneficiary_id", "BEN2"),
    ("dedup_key", "other"),
])
def test_altered_binding_rejected(field, value):
    tok, _ = _issue()
    kwargs = dict(source_account_id="ACC1", amount="100.00",
                  beneficiary_id="BEN1", dedup_key="dk")
    kwargs[field] = value
    assert token_mod.verify_token("sekret", tok, **kwargs) is None


def test_expired_rejected():
    tok, _ = _issue(ttl_seconds=10, now=time.time() - 100)
    assert token_mod.verify_token(
        "sekret", tok,
        source_account_id="ACC1", amount="100.00",
        beneficiary_id="BEN1", dedup_key="dk",
    ) is None


def test_token_string_carries_no_secret():
    tok, _ = _issue()
    assert "sekret" not in tok


def test_verify_uses_compare_digest(monkeypatch):
    called = {}
    real = hmac.compare_digest

    def spy(a, b):
        called["yes"] = True
        return real(a, b)

    monkeypatch.setattr(token_mod.hmac, "compare_digest", spy)
    tok, _ = _issue()
    token_mod.verify_token(
        "sekret", tok,
        source_account_id="ACC1", amount="100.00",
        beneficiary_id="BEN1", dedup_key="dk",
    )
    assert called.get("yes")


def test_garbage_and_empty_token_fail_closed():
    for bad in ("", "not-a-token", "a:b", "a:b:c:d"):
        assert token_mod.verify_token(
            "sekret", bad,
            source_account_id="ACC1", amount="100.00",
            beneficiary_id="BEN1", dedup_key="dk",
        ) is None


def test_nonce_is_random_and_unique():
    n1 = token_mod.new_nonce()
    n2 = token_mod.new_nonce()
    assert n1 != n2 and len(n1) > 8


def test_token_bound_to_one_payment_only():
    # F10: T1 bound to P1 must not validate against P2's binding.
    t1, _ = _issue(amount="100.00", dedup_key="P1", nonce="n1")
    # P2 has a different dedup_key/amount; T1 must fail against it.
    assert token_mod.verify_token(
        "sekret", t1,
        source_account_id="ACC1", amount="200.00",
        beneficiary_id="BEN1", dedup_key="P2",
    ) is None
    # T1 still validates against P1.
    assert token_mod.verify_token(
        "sekret", t1,
        source_account_id="ACC1", amount="100.00",
        beneficiary_id="BEN1", dedup_key="P1",
    ) is not None
