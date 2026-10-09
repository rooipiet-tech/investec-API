# Go-live gate checklist (carried forward items)

G1 dry-run soak; G2 sandbox test (openapisandbox.investec.com); G3 human enables live.

Items to verify at G2 / G1 with REAL replies (cannot be tested offline):
- Investec paymultiple response: which Status strings mean success vs held/pending/needs-authorisation (a 200 entry with a PaymentReferenceNumber and an unrecognised non-fail Status, e.g. "Held", currently maps to success); rejection body field names (F40); per-payment API limit (community FAQ says R20,000).
- Gmail: topmost Authentication-Results header shape/authserv-id on the real bot mailbox (preflight check; runbook must state the MTA-overwrites-inbound-A-R assumption).
- IMAP fetch marks mail read: read a dedicated label only, never INBOX (the owner's own Gmail account may be used, see Amendment 6). At G1 check the Authentication-Results header on a self-sent message and on one from another allowed sender.
- Benefits to confirm in Investec Online: which scopes the single API key has.

## Decision 2026-10-08 (orchestrator; classifier)
- A 200 response is NEVER classified 'failed' until G2 shows real Investec failure bodies: 200 + ErrorMessage and no entry reference => 'unknown' (needs_review, reservation kept, sanitised message in the owner's email, no resend). 'failed' (release, re-instructable) only for a definite HTTP 4xx PaymentRejected.
- At G2: record real failure bodies (status code, ErrorMessage text, field names), then consider an allow-list of definite-rejection texts to restore 'failed' for 200 rejections.
