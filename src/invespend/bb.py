"""Business Banking (/za/bb/v1) support: call, notice and cash-management accounts.

Private Banking (/za/pb/v1) and Business Banking (/za/bb/v1) are two separate
Investec APIs. The PB code (``ingest.py``) only ever sees transactional accounts;
call/cash-management accounts live on BB, which this module integrates.

For now this is a **read-only diagnostic**: it reports the shape of the live BB
data (account types, transaction field names, and the debit/credit sign
convention — which the published swagger does not document) so the ingest
normaliser can be written against verified reality rather than guesses. Account
numbers are masked and descriptions are never printed, so it is safe to run in
CI logs.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from .config import Settings
from .investec_client import InvestecClient

log = logging.getLogger(__name__)


def mask(value: object) -> str:
    """Mask an identifier, keeping only the last 4 characters."""
    s = "" if value is None else str(value)
    if len(s) <= 4:
        return "*" * len(s)
    return "*" * (len(s) - 4) + s[-4:]


def _num(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _infer_sign_convention(rows: list[dict]) -> str:
    """Compare consecutive RunningBalance deltas to Amount to learn the sign rule.

    If balance[i] - balance[i-1] ≈ Amount[i], the API already signs Amount
    (negative = debit). If it ≈ -Amount[i] for outflows, Amount is unsigned and a
    separate indicator decides direction. Returns a short human-readable verdict.
    """
    signed_hits = unsigned_hits = comparable = 0
    prev_rb: float | None = None
    for row in rows:
        rb = _num(row.get("RunningBalance"))
        amt = _num(row.get("Amount"))
        if rb is not None and prev_rb is not None and amt is not None:
            delta = round(rb - prev_rb, 2)
            comparable += 1
            if abs(delta - amt) < 0.01:
                signed_hits += 1
            elif abs(abs(delta) - abs(amt)) < 0.01:
                unsigned_hits += 1
        prev_rb = rb if rb is not None else prev_rb
    if comparable == 0:
        return "indeterminate (no consecutive RunningBalance+Amount pairs)"
    if signed_hits >= unsigned_hits and signed_hits > 0:
        return (f"Amount appears ALREADY SIGNED (delta==Amount on {signed_hits}/"
                f"{comparable} pairs)")
    return (f"Amount appears UNSIGNED (|delta|==|Amount| on {unsigned_hits}/"
            f"{comparable} pairs); a separate indicator sets direction")


def run_bb_diagnostic(settings: Settings, sample_days: int = 45,
                      sample_rows: int = 6) -> dict:
    """Probe the live BB API and log a masked report. Read-only; writes nothing."""
    client = InvestecClient(
        client_id=settings.investec_client_id,
        client_secret=settings.investec_client_secret,
        api_key=settings.investec_api_key,
        base_url=settings.investec_base_url,
    )

    try:
        accounts = client.get_bb_accounts()
    except Exception as exc:  # noqa: BLE001 - surface scope/enrolment problems clearly
        log.error("BB accounts call failed (key may lack Business Banking scope): %s", exc)
        return {"bb_available": False, "error": str(exc)}

    log.info("BB accounts returned: %d", len(accounts))
    for i, acc in enumerate(accounts, start=1):
        log.info(
            "  [%d] acct=%s type=%r category=%r productType=%r customerType=%r "
            "balanceKeys=%s",
            i,
            mask(acc.get("AccountNumber") or acc.get("AccountId")),
            acc.get("AccountType"),
            acc.get("Category"),
            acc.get("BankAccountProductType"),
            acc.get("CustomerAccountType"),
            sorted((acc.get("Balances") or {}).keys()),
        )

    sample = {}
    if accounts:
        first = accounts[0]
        acct_id = first.get("AccountId")
        to_date = date.today()
        from_date = to_date - timedelta(days=sample_days)
        try:
            txns = client.get_bb_transactions(acct_id, from_date, to_date)
        except Exception as exc:  # noqa: BLE001
            log.error("BB transactions call failed for %s: %s", mask(acct_id), exc)
            txns = []
        log.info("Sampled %d transaction(s) for %s over last %d days",
                 len(txns), mask(acct_id), sample_days)
        if txns:
            log.info("  transaction field names: %s", sorted(txns[0].keys()))
            for j, t in enumerate(txns[:sample_rows], start=1):
                log.info(
                    "  row%d: PostDate=%s ValueDate=%s Amount=%s RunningBalance=%s "
                    "Type=%r TransactionCode=%r Status=%r descLen=%d",
                    j, t.get("PostDate"), t.get("ValueDate"), t.get("Amount"),
                    t.get("RunningBalance"), t.get("Type"), t.get("TransactionCode"),
                    t.get("TransactionStatus"), len(str(t.get("Description") or "")),
                )
            log.info("  sign convention: %s", _infer_sign_convention(txns))
            sample = {"count": len(txns), "fields": sorted(txns[0].keys())}

    return {
        "bb_available": True,
        "accounts": len(accounts),
        "account_types": sorted({str(a.get("AccountType")) for a in accounts}),
        "sample": sample,
    }
