"""Investec Open API client (read-only).

Implements the OAuth2 client-credentials flow and the account/transaction reads.
The credentials granted here cannot move money — they only read account data.
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

    # ── Business Banking / CIB (call, notice, cash-management accounts) ─────────
    # A separate API (/za/bb/v1) from Private Banking (/za/pb/v1). Same OAuth /
    # x-api-key auth. Payloads use PascalCase, embed balances in the accounts
    # response, and paginate transactions. These reads are also money-can't-move.
    def get_bb_accounts(self) -> list[dict]:
        return self._get("/za/bb/v1/accounts").get("data", {}).get("accounts", [])

    def get_bb_transactions(
        self,
        account_id: str,
        from_date: date | None = None,
        to_date: date | None = None,
        max_pages: int = 100,
    ) -> list[dict]:
        """All transactions for a BB account, following pagination.

        ``fromDate``/``toDate`` default (server-side) to the last 180 days; pass
        them for a backfill window. ``max_pages`` is a safety bound.
        """
        params: dict = {}
        if from_date is not None:
            params["fromDate"] = from_date.isoformat()
        if to_date is not None:
            params["toDate"] = to_date.isoformat()

        out: list[dict] = []
        page = 1
        while page <= max_pages:
            data = self._get(
                f"/za/bb/v1/accounts/{account_id}/transactions",
                params={**params, "page": page},
            )
            batch = data.get("data", {}).get("transactions", [])
            out.extend(batch)
            total_pages = (data.get("meta") or {}).get("totalPages")
            if not batch or not total_pages or page >= int(total_pages):
                break
            page += 1
        return out
