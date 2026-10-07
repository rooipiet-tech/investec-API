"""Investec Open API client (reads + guarded write path).

Implements the OAuth2 client-credentials flow and the account/transaction reads.

The write helpers (``_post`` / ``create_payment``) are reached ONLY in live mode
(an explicit enable flag AND a payment-capable credential, see
config.live_enabled()). In the default dry-run posture they are never called.

The payment endpoint and request shape are documented in the Investec
Programmable Banking swagger (mirror cited in ``.loop/investec-api-research.md``):
``POST /za/pb/v1/accounts/{accountId}/paymultiple``, OAuth scope
``beneficiarypayments``. That scope is ALSO needed to LIST beneficiaries, so a key
that can list beneficiaries can also pay. Community notes, UNCONFIRMED by the
official docs: a R20,000 per-payment API limit, and a beneficiary must have been
paid once in Investec Online before API payments work.

The v2 executor uses ``create_payment(..., fresh_token=True)`` (hardened: no
retries, no redirects, positive "not sent" detection); the default call keeps
the original path unchanged.
"""
from __future__ import annotations

import base64
import time
from datetime import date

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class InvestecClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        api_key: str,
        base_url: str = "https://openapi.investec.com",
        timeout: int = 30,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._token: str | None = None
        self._token_expiry: float = 0.0

        self._session = requests.Session()
        retry = Retry(
            total=4,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
        )
        self._session.mount("https://", HTTPAdapter(max_retries=retry))

        # Write session (hardened v2 path): NO retry adapter, so a payment POST is
        # never resent by the transport layer.
        self._write_session = requests.Session()
        self._write_session.mount("https://", HTTPAdapter(max_retries=0))
        self._write_session.mount("http://", HTTPAdapter(max_retries=0))

    # ── auth ──────────────────────────────────────────────────────────────────
    def _get_token(self, force: bool = False) -> str:
        # Reuse the token until ~1 minute before expiry (unless forced).
        if not force and self._token and time.time() < self._token_expiry - 60:
            return self._token

        basic = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()
        resp = self._session.post(
            f"{self._base_url}/identity/v2/oauth2/token",
            headers={
                "Authorization": f"Basic {basic}",
                "x-api-key": self._api_key,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={"grant_type": "client_credentials"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = time.time() + int(payload.get("expires_in", 1800))
        return self._token

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = self._session.get(
            f"{self._base_url}{path}",
            headers={
                "Authorization": f"Bearer {self._get_token()}",
                "x-api-key": self._api_key,
                "Accept": "application/json",
            },
            params=params,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ── reads ─────────────────────────────────────────────────────────────────
    def get_accounts(self) -> list[dict]:
        return self._get("/za/pb/v1/accounts").get("data", {}).get("accounts", [])

    def get_balance(self, account_id: str) -> dict:
        return self._get(f"/za/pb/v1/accounts/{account_id}/balance").get("data", {})

    def get_transactions(
        self, account_id: str, from_date: date, to_date: date
    ) -> list[dict]:
        data = self._get(
            f"/za/pb/v1/accounts/{account_id}/transactions",
            params={
                "fromDate": from_date.isoformat(),
                "toDate": to_date.isoformat(),
            },
        )
        return data.get("data", {}).get("transactions", [])

    def get_beneficiaries(self) -> list[dict]:
        """List the user's pre-registered Investec beneficiaries (read-only).

        Returns the raw ``data`` list; mapping to the allowlist dataclass and the
        exact-match/fail-closed resolution live in payments.beneficiaries.
        """
        return self._get("/za/pb/v1/accounts/beneficiaries").get("data", [])

    # ── writes (ADDITIVE; live-mode only) ─────────────────────────────────────
    # Endpoint + request shape per the Investec Programmable Banking swagger:
    # POST /za/pb/v1/accounts/{accountId}/paymultiple with a "paymentList" of
    # {beneficiaryId, amount, myReference, theirReference}. Requires a credential
    # with the beneficiarypayments scope and a beneficiary pre-registered online
    # (the API cannot create beneficiaries; "paid once online first" is an
    # unconfirmed community note).
    PAY_MULTIPLE_PATH = "/za/pb/v1/accounts/{account_id}/paymultiple"

    def _post(self, path: str, payload: dict) -> dict:
        resp = self._session.post(
            f"{self._base_url}{path}",
            headers={
                "Authorization": f"Bearer {self._get_token()}",
                "x-api-key": self._api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _post_once(self, path: str, payload: dict, token: str) -> dict:
        """Hardened single write attempt (v2). The token is a PARAMETER: this method
        never fetches one, so "not sent" is decided entirely before it is called.

        No retries, no redirects. Every outcome after the request may have left the
        process is either a definite rejection (4xx other than 408/429) or
        ``PaymentUnknownOutcome`` (money may have moved; never resent).
        """
        from .payments.outcome import (
            PaymentRejected,
            PaymentUnknownOutcome,
            provider_message_from_body,
        )

        def _decoded(resp) -> object:
            try:
                return resp.json()
            except Exception:
                return None

        def _rejected(resp) -> PaymentRejected:
            return PaymentRejected(resp.status_code, provider_message_from_body(_decoded(resp)))

        try:
            resp = self._write_session.post(
                f"{self._base_url}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "x-api-key": self._api_key,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._timeout,
                allow_redirects=False,
            )
        except requests.exceptions.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if isinstance(status, int) and 400 <= status < 500 and status not in (408, 429):
                raise _rejected(exc.response) from None
            raise PaymentUnknownOutcome("HTTPError") from None
        except requests.RequestException as exc:
            raise PaymentUnknownOutcome(type(exc).__name__) from None

        status = resp.status_code
        if 400 <= status < 500 and status not in (408, 429):
            raise _rejected(resp)
        if status < 200 or status >= 300:
            # 1xx, 3xx (never followed), 408 (receipt ambiguous), 429, 5xx
            raise PaymentUnknownOutcome(f"HTTP {status}")
        body = _decoded(resp)
        if not isinstance(body, dict):
            raise PaymentUnknownOutcome("undecodable response body")
        return body

    def create_payment(
        self,
        source_account_id: str,
        beneficiary_id: str,
        amount: str,
        reference: str = "",
        my_reference: str = "",
        *,
        fresh_token: bool = False,
    ) -> dict:
        """Submit a single payment to a pre-registered beneficiary (live-mode only).

        Pays ONLY by ``beneficiary_id`` — never a raw account number. Endpoint and
        request shape are verified against Investec's published API; the live call
        is exercised in tests via mocks (no real money in CI).
        """
        path = self.PAY_MULTIPLE_PATH.format(account_id=source_account_id)
        payload = {
            "paymentList": [
                {
                    "beneficiaryId": beneficiary_id,
                    "amount": str(amount),
                    "myReference": my_reference or reference,
                    "theirReference": reference,
                }
            ]
        }
        if not fresh_token:
            return self._post(path, payload)
        # Hardened v2 write: force-fetch a token first. A failure here is POSITIVE
        # proof that nothing was sent; the underlying text is not echoed.
        from .payments.outcome import PaymentNotSent

        try:
            token = self._get_token(force=True)
        except Exception:
            raise PaymentNotSent("token fetch failed before the payment was sent") from None
        return self._post_once(path, payload, token)
