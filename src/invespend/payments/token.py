"""Approval tokens — the ONLY thing that can authorize a payment (F2, F10).

A token is an HMAC-SHA256 keyed by a never-emailed/never-logged secret
(``approval_signing_secret`` from the environment), BOUND to the exact payment:
(source_account_id, amount, beneficiary_id, dedup_key, nonce, expiry).

Properties enforced here:
  * Bound — altering any field (source account, amount, beneficiary, dedup key,
    nonce, expiry) invalidates the token (F2, F10).
  * Single-use — burning is the store's job; this module exposes a stable
    ``token_id`` (the nonce) the store keys its burn-set on (F2).
  * Expiring — ``verify`` rejects an expired claim (F2).
  * Constant-time — comparison uses ``hmac.compare_digest`` (F2).
  * Secret-free output — the issued string contains only the bound public fields
    plus the MAC; the secret never appears in any returned value or repr (F2/F15).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class TokenClaim:
    """The public, bound fields a token commits to. No secret lives here."""

    source_account_id: str
    amount: str
    beneficiary_id: str
    dedup_key: str
    nonce: str
    expiry: int  # unix epoch seconds

    @property
    def token_id(self) -> str:
        """Stable single-use identity the store burns on first valid approval."""
        return self.nonce


_FIELD_SEP = "|"
_TOKEN_SEP = ":"


def new_nonce() -> str:
    """Cryptographically-random, URL-safe single-use nonce."""
    return secrets.token_urlsafe(16)


def _amount_str(amount) -> str:
    # Bind the amount as a canonical 2dp string so token binding is stable
    # regardless of int/float/Decimal/str input form.
    from decimal import Decimal

    return str(Decimal(str(amount)).quantize(Decimal("0.01")))


def _canonical(claim: TokenClaim) -> bytes:
    parts = [
        claim.source_account_id,
        _amount_str(claim.amount),
        claim.beneficiary_id,
        claim.dedup_key,
        claim.nonce,
        str(int(claim.expiry)),
    ]
    return _FIELD_SEP.join(parts).encode()


def _mac(secret: str, claim: TokenClaim) -> str:
    return hmac.new(secret.encode(), _canonical(claim), hashlib.sha256).hexdigest()


def issue_token(
    secret: str,
    *,
    source_account_id: str,
    amount,
    beneficiary_id: str,
    dedup_key: str,
    ttl_seconds: int = 86400,
    nonce: str | None = None,
    now: float | None = None,
) -> tuple[str, TokenClaim]:
    """Mint a bound token string + its claim.

    The returned string is safe to email: it carries only the bound public
    fields and the MAC — never the secret.
    """
    if not secret:
        raise ValueError("approval_signing_secret is required to issue a token")
    now = time.time() if now is None else now
    claim = TokenClaim(
        source_account_id=source_account_id,
        amount=_amount_str(amount),
        beneficiary_id=beneficiary_id,
        dedup_key=dedup_key,
        nonce=nonce or new_nonce(),
        expiry=int(now) + int(ttl_seconds),
    )
    mac = _mac(secret, claim)
    # token = nonce:expiry:mac  (the binding fields are reconstructed from the
    # pending record at verify time, NOT trusted from the wire).
    token = _TOKEN_SEP.join([claim.nonce, str(claim.expiry), mac])
    return token, claim


def verify_token(
    secret: str,
    token: str,
    *,
    source_account_id: str,
    amount,
    beneficiary_id: str,
    dedup_key: str,
    now: float | None = None,
) -> TokenClaim | None:
    """Return the claim iff ``token`` is a valid, unexpired, correctly-bound MAC.

    The binding fields (source account, amount, beneficiary, dedup key) are
    supplied by the caller from the trusted pending record — NOT read from the
    token wire form. A token therefore approves only the exact payment it was
    bound to (F10). Returns ``None`` on any failure (fail-closed, F5).
    """
    if not secret or not token:
        return None
    parts = token.split(_TOKEN_SEP)
    if len(parts) != 3:
        return None
    nonce, expiry_raw, presented_mac = parts
    try:
        expiry = int(expiry_raw)
    except ValueError:
        return None

    now = time.time() if now is None else now
    if now >= expiry:
        return None  # expired -> fail closed

    claim = TokenClaim(
        source_account_id=source_account_id,
        amount=_amount_str(amount),
        beneficiary_id=beneficiary_id,
        dedup_key=dedup_key,
        nonce=nonce,
        expiry=expiry,
    )
    expected_mac = _mac(secret, claim)
    if not hmac.compare_digest(expected_mac, presented_mac):
        return None  # wrong secret / altered binding -> fail closed
    return claim
