# plan.md, email-payment-v2, iteration 1, REVISION 2 (concise; full detail in /home/user/investec-API/REFACTORING_PLAN.md)

Spec v1.0.0 frozen (F1-F41), untouched. Base 2eee10c. Floor 330 passed. Live stays OFF. Aggregate risk MEDIUM (S11 HIGH but contained: dry-run default, mocks, no retries, CAS, caps, age gate, duplicate guard).
Plan-review verdict `revise` (B1-B9, T1-T16) folded in; resolutions table = REFACTORING_PLAN.md section 3a; state/transition table (B6) = section 2a, BEFORE S9.

| ID | Change | Files | Risk | Criteria |
|---|---|---|---|---|
| S0 | Baseline help capture at 2eee10c + BASE_SHA; fixtures; frozen-surface test (COLUMNS monkeypatched in-test, py3.12 guard: T13); measure 330 | .loop/baseline/*, tests/test_payment_frozen_surface.py | LOW | F1 F2 F22 |
| S1 | F20: failing test, then approval_recipients via getattr-safe settings (B7), signing-secret guard + no per-cycle resend (T14) | cli.py, config.py, tests/test_payment_cli_wiring.py | MED | F20 F26 |
| S2 | F21: [INV-ref] + In-Reply-To linkage; digest resolved by nonce / exactly one ref, lookup before last-3 step (B9) | payments/dedup.py, inbox.py, pipeline.py, notify.py | MED | F21 |
| S3 | Client: no-retry write session, allow_redirects=False, 3xx/undecodable 200 = unknown (T1), fresh token, parse_payment_response, per-message legacy catch | investec_client.py, payments/outcome.py, pipeline.py, selftest.py | MED | F17 F40 F15 |
| S4 | Caps fail closed (_opt_cap) | config.py, payments/caps.py | LOW | F18 |
| S5 | sender_auth: topmost trusted A-R only, RFC 8601 tokenizer, header.i/d, strict alignment + DMARC, single ASCII From (B1) | payments/sender_auth.py, tests/fixtures/payments_v2/auth | MED | F6 F7 F30 |
| S6 | parse_trigger strict grammar (T9), strip_trigger (B2), resolve_source_unique | payments/trigger.py, accounts.py | LOW | F4 F5 F33 |
| S7 | Content gathering; FAIL-CLOSED typed text on any forward/reply indicator, HTML structure preferred, subject never searched (B3) | payments/content.py, bankdetails.py, inbox.py | MED | F7 F8 F32 |
| S8 | ImageExtractor, Null/Fake, validate_extraction (+currency, non-ZAR conflict: T5), Q11 seam | payments/images.py, tests/fixtures/payments_v2 | LOW-MED | F32-F38 |
| 2a | State/transition table: accepted, awaiting_*, held, submitting, terminals incl. parked; TRANSITIONS; crash/expiry rules (B6) | (design, in plan) | - | F14 F16 F23 |
| S9 | Migration 0013 (payment_instruction, payment_message_seen, payment_beneficiary_seen+established; RLS deny_all) + InstructionStore; id = sha256(Message-ID|From) (B4); HMAC fingerprint/account (T6,T7); claim: day row insert then lock, POST outside txn, stale-submitting threshold (T2) | db/migrations/0013_*.sql, payments/instructions.py, tests/test_payment_state_table.py | MED | F23 F29 F39 F14 |
| S10 | Notification builders incl. paste-details; loop-guard headers Auto-Submitted / X-Invespend-Notification / own Message-ID (B8) | payments/notify.py | LOW | F10 F11 F13 |
| S11 | routing/execute/cycle, PAYMENTS_MODE=v2 via getattr default legacy; message-age gate (B5), commands first line only (T10), duplicate park (T11), ambiguity park (T8), recent-beneficiary hold (T12 HUMAN DECISION), store check before fetch + generic failure notice (T4), v2 error envelope class+scrubbed (T15), no extractor before auth+trigger (T16) | payments/routing.py, execute.py, cycle.py, cli.py, config.py | HIGH (contained) | F3 F9 F12-F17 F19 F26 F27 F33-F36 F39 F40 |
| S12 | payments-cycle.yml: cron */15, dispatch, concurrency, gated on repo var PAYMENTS_CYCLE_ENABLED (default off), Environment `payments` restricted to main, no PR trigger, live never literal true (T3) | .github/workflows | LOW | F28 F24 |
| S13 | Runbook, README, .env.example, DEPLOY note (24h unconfirmed, paid-once blocker, R20k caveat, T4/T12/T14 notes) | docs/, README.md | LOW | F31 F41 F18 |
| S14 | BLOCKED on Q10: engine adapter + dependency | payments/images_<engine>.py | MED | F25 |

Slicing: it1 = S0-S6; it2 = S7-S10 (S9 only after 2a accepted); it3 = S11-S13; S14 stays BLOCKED on Q10. Migration 0013 not committed before S11 design is checked against 2a (no 0014).
Red-then-green commits for S1, S2, S3, S4. No edits to existing tests, ingest/report/statements/db/migrations 0001-0012.
F22 verified by: help fixture byte-compare test, diff allowlist vs 2eee10c, unmodified existing tests, legacy summary keys test.
HUMAN DECISION (T12): default = registered beneficiaries first seen within the hold window go to the HELD path, with first-deploy bootstrap (beneficiaries present at enablement are established); alternative PAYMENTS_HOLD_REGISTERED_RECENT=false accepts the Investec-rejection risk.
Other human decisions: immediate-pay no cancel window; R20k vs R30k; unconfirmed 24h + paid-once blocker; Q10 engine; Q11 image policy (default confirm); pay-key split/listing scope; live OFF G1-G3. See REFACTORING_PLAN.md section 4.
Spec impact: none edited. Flags: T12 default narrows F12 to established registered payees; B4/B5 add an age window under F16.
