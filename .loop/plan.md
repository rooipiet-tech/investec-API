# plan.md, email-payment-v2, iteration 1 (concise; full detail in /home/user/investec-API/REFACTORING_PLAN.md)

Spec v1.0.0 frozen (F1-F41). Base 2eee10c. Floor 330 passed. Live stays OFF. Aggregate risk MEDIUM (S11 HIGH but contained: dry-run default, mocks, no retries, CAS, caps).
Previous plan archived: .loop/archive/beneficiary-matching-run/REFACTORING_PLAN.md.

| ID | Change | Files | Risk | Criteria |
|---|---|---|---|---|
| S0 | Baseline help capture at 2eee10c + BASE_SHA; copy to tests/fixtures/cli_help; frozen-surface test; measure 330 | .loop/baseline/*, tests/test_payment_frozen_surface.py | LOW | F1 F2 F22 |
| S1 | F20: failing test, then pass approval_recipients from config (PAYMENTS_NOTIFY_RECIPIENTS / allowed senders) | cli.py, config.py, tests/test_payment_cli_wiring.py | MED | F20 F26 |
| S2 | F21: failing test, then [INV-ref] subject + In-Reply-To linkage, token verified against original pending | payments/dedup.py, inbox.py, pipeline.py, notify.py | MED | F21 |
| S3 | Client: no-retry write session, fresh token, parse_payment_response (200+ErrorMessage/AuthorisationRequired = failure), PaymentUnknownOutcome | investec_client.py, payments/outcome.py, pipeline.py, selftest.py | MED | F17 F40 F15 |
| S4 | Caps fail closed (_opt_cap; unparsable no longer crashes load) | config.py, payments/caps.py | LOW | F18 |
| S5 | sender_auth.authenticate_sender (From allowlist, trusted authserv-id dkim+spf aligned, no ARC, never Reply-To) | payments/sender_auth.py | MED | F6 F7 F30 |
| S6 | parse_trigger (`pay` + 3 digits), resolve_source_unique | payments/trigger.py, accounts.py | LOW | F4 F5 F33 |
| S7 | Content gathering: typed/forwarded/quoted split, html, rfc822 unwrap, bank details | payments/content.py, bankdetails.py, inbox.py | MED | F7 F8 F32 |
| S8 | ImageExtractor protocol, Null/Fake, validate_extraction, Q11 seam, adversarial fixture | payments/images.py, tests/fixtures/payments_v2 | LOW-MED | F32-F38 |
| S9 | Migration 0013 (payment_instruction, payment_beneficiary_seen, RLS deny_all) + InstructionStore (Memory/Pg, CAS, claim_for_execution) | db/migrations/0013_*.sql, payments/instructions.py | MED | F23 F29 F39 F14 |
| S10 | Notification builders incl. paste-details (nine-field order, branch code reference-only) | payments/notify.py | LOW | F10 F11 F13 |
| S11 | routing/execute/cycle, PAYMENTS_MODE=v2 dispatch in cmd_approve_payments (no CLI change) | payments/routing.py, execute.py, cycle.py, cli.py, config.py | HIGH (contained) | F3 F9 F12-F17 F19 F26 F27 F33-F36 F39 F40 |
| S12 | payments-cycle.yml (cron */15, dispatch, concurrency, live only via repo var, default false) | .github/workflows | LOW | F28 F24 |
| S13 | Runbook, README, .env.example, DEPLOY note (24h unconfirmed, paid-once blocker, R20k caveat) | docs/, README.md | LOW | F31 F41 F18 |
| S14 | BLOCKED on Q10: engine adapter + dependency | payments/images_<engine>.py | MED | F25 |

Slicing: it1 = S0-S6; it2 = S7-S10; it3 = S11-S13; S14 after Q10.
Red-then-green commits for S1, S2, S3, S4. No edits to existing tests, ingest/report/statements/db/migrations 0001-0012.
F22 verified by: help fixture byte-compare test, diff allowlist vs 2eee10c, unmodified existing tests, legacy summary keys test.
Human decisions needed: immediate-pay no cancel window; R20k vs R30k; unconfirmed 24h + paid-once blocker; Q10 engine (Claude vision vs tesseract); Q11 image amount policy (default confirm); pay-key split/listing scope; live OFF G1-G3. See REFACTORING_PLAN.md section 4.
