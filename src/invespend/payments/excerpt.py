"""The sanitised original-text excerpt of the figure line (RS3AF5-1). Pure, stdlib only.

The batch approval email shows, next to ``ZAR 500.00``, a short excerpt of what the sender actually wrote
(``Read from: "Amount: R500 for rent"``) so the approver always sees the written currency and wording. The text is
untrusted, so ``sanitise_excerpt`` makes it inert:

* redaction is ``outcome.sanitize_provider_message`` (NFKC fold, control / format / zero-width characters dropped,
  whitespace collapsed, addresses, secret words, JWTs, token-like strings and 6+ digit runs redacted);
* brackets become parentheses (no ``[BATCH ...]`` / ``[INV-...]`` tag), double quotes become single quotes;
* batch refs, instruction refs, the own-notice sentinel text and the words ``approve`` / ``cancel`` are replaced by
  ``(removed)``, so the excerpt can never look like a command, a batch reference or our own notice;
* at most ``MAX_EXCERPT`` characters; idempotent (re-sanitising a sanitised excerpt is a no-op) and linear.

The same function runs at creation (what is stored in ``figures_excerpt``), inside both stores' create validation
and again when the email is built.
"""
from __future__ import annotations

import re

from .loopguard import NOTICE_FIRST_LINE
from .outcome import sanitize_provider_message

MAX_EXCERPT = 80
_REMOVED = "(removed)"
_NEUTRALISE = re.compile(
    r"approve|cancel|B-[0-9]{4}-[0-9a-f]{4}|INV-[0-9a-f]{12}|invespend automated notice|do not reply with payment instructions|"
    + re.escape(NOTICE_FIRST_LINE),
    re.IGNORECASE | re.ASCII,
)
_BRACKETS = str.maketrans({"[": "(", "]": ")", '"': "'"})


def sanitise_excerpt(text: object) -> str:
    """The inert, one-line, <= ``MAX_EXCERPT`` character version of ``text`` ("" when nothing is left). Never raises."""
    try:
        if text is None:
            return ""
        clean = sanitize_provider_message(text).translate(_BRACKETS)
        clean = _NEUTRALISE.sub(_REMOVED, clean)
        clean = " ".join(clean.split())
        clean = clean[:MAX_EXCERPT].rstrip()
        return clean
    except Exception:  # pragma: no cover - defensive, the contract is "never raises"
        return ""
