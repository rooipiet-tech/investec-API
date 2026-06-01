"""Rule-based spend categoriser.

Deliberately simple and transparent — a clean seam to later replace with an
LLM or ML classifier without touching the rest of the pipeline.
"""
from __future__ import annotations

# Ordered list of (category, keywords). First match wins.
_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Groceries", ("woolworths", "checkers", "pick n pay", "pnp", "spar", "food lover", "shoprite")),
    ("Dining", ("restaurant", "uber eats", "mr d", "kfc", "nandos", "steers", "cafe", "coffee", "starbucks")),
    ("Transport", ("uber", "bolt", "gautrain", "engen", "shell", "bp ", "sasol", "fuel", "petrol", "parking")),
    ("Utilities", ("eskom", "city of", "municipal", "water", "electricity", "prepaid", "vodacom", "mtn", "telkom", "rain", "afrihost")),
    ("Subscriptions", ("netflix", "spotify", "youtube", "showmax", "dstv", "apple.com", "google", "microsoft", "amazon prime")),
    ("Health", ("pharmacy", "clicks", "dis-chem", "dischem", "medical", "discovery health", "hospital")),
    # Fees before Income: Investec's "FeesAndInterest" type contains "interest".
    ("Fees", ("fee", "charge", "service charge", "admin fee")),
    ("Income", ("salary", "deposit", "interest", "refund", "reversal")),
    ("Transfers", ("transfer", "payshap", "immediate payment", "eft", "payment to")),
    ("Cash", ("atm", "cash withdrawal", "withdrawal")),
    ("Shopping", ("takealot", "amazon", "mr price", "edgars", "game", "makro", "builders")),
]


def categorize(description: str | None, transaction_type: str | None = None) -> str:
    """Return a spend category for a transaction.

    Matches against the description (and the Investec ``transactionType``) using
    case-insensitive keyword rules; falls back to ``Other``.
    """
    haystack = " ".join(filter(None, [description or "", transaction_type or ""])).lower()
    for category, keywords in _RULES:
        if any(kw in haystack for kw in keywords):
            return category
    return "Other"
