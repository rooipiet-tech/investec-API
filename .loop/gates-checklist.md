# Go-live gate checklist (carried forward items)

G1 dry-run soak; G2 sandbox test (openapisandbox.investec.com); G3 human enables live.

Items to verify at G2 / G1 with REAL replies (cannot be tested offline):
- Investec paymultiple response: which Status strings mean success vs held/pending/needs-authorisation (a 200 entry with a PaymentReferenceNumber and an unrecognised non-fail Status, e.g. "Held", currently maps to success); rejection body field names (F40); per-payment API limit (community FAQ says R20,000).
- Gmail: topmost Authentication-Results header shape/authserv-id on the real bot mailbox (preflight check; runbook must state the MTA-overwrites-inbound-A-R assumption).
- IMAP fetch marks mail read: use a dedicated mailbox/label only.
- Benefits to confirm in Investec Online: which scopes the single API key has.
