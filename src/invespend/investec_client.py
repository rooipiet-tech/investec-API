"""Investec Open API client (read-only reads + guarded write path).

Implements the OAuth2 client-credentials flow and the account/transaction reads.
The read credentials granted here cannot move money — they only read account data.

The write helpers (``_post`` / ``create_payment``) are ADDITIVE and are reached
ONLY in live mode (an explicit enable flag AND a write-scoped credential — see
config.live_enabled()). In the default dry-run posture they are never called.

ASSUMPTION (OQ1): the exact Investec payment endpoint + scope are UNVERIFIED.
The endpoint path is isolated below in a single clearly-marked constant and is
exercised only by mocked tests — it is NOT asserted as a confirmed live URL.
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

    # ── auth ──────────────────────────────────────────────────────────────────
    def _get_token(self) -> str:
        # Reuse the token until ~1 minute before expiry.
        if self._token and time.time() < self._token_expiry - 60:
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
    # Endpoint + request shape VERIFIED against Investec Programmable Banking docs:
    # POST /za/pb/v1/accounts/{accountId}/paymultiple with a "paymentList" of
    # {beneficiaryId, amount, myReference, theirReference}. Requires a credential
    # with payments enabled and the beneficiary pre-registered online (Investec
    # only pays beneficiaries you have created + paid once via online banking).
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

    def create_payment(
        self,
        source_account_id: str,
        beneficiary_id: str,
        amount: str,
        reference: str = "",
        my_reference: str = "",
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
        return self._post(path, payload)
