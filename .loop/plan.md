# plan: beneficiary-matching, iteration 1 (full detail in /REFACTORING_PLAN.md)

Base: HEAD at build start (currently acb973d). Floor: 211 passed / 0 fail / 0 err. Spec v1.0.0 FROZEN; Q1-Q6 resolutions binding.

| ID | Change | Files | Risk | Criteria |
|---|---|---|---|---|
| S0 | Re-capture help for top + 8 subcommands from the clean base; write BASE_SHA; commit first | .loop/baseline/* | LOW | F1, F3 |
| S1 | Migration with tables `beneficiaries` (full acct no., first/last_seen_at, active) and `beneficiary_matches` (PK transaction_hash, no FK, status check, rule/token NOT NULL, snapshot_at, matched_at); RLS deny_all on both; view `transactions_beneficiary` with security_invoker that inner-joins transactions_flow, left-joins beneficiaries, and excludes internal_transfer and own-account numbers | db/migrations/0012_beneficiary_matching.sql | MEDIUM | F4-F7, F18, F21, F25, F29, F32 |
| S2 | Pure matcher: PAYMENT_TRANSACTION_TYPES, RULES (account_number > reference_exact > beneficiary_name_exact > name_exact > name_prefix), guards 8/5/7, normalise/variants, parse_beneficiaries (field allowlist, None on malformed input), eligible_beneficiaries (own-account exclusion), match_transaction/match_all (fail-closed, no fall-through, sorted output) | src/invespend/beneficiary_match.py | LOW | F12-F16, F18, F20, F26-F28 |
| S3 | Append-only DB helpers: upsert_beneficiaries, retire_missing_beneficiaries (soft), load_own_account_numbers, load_active_beneficiaries, load_match_candidates (skips matched), upsert_beneficiary_matches (`ON CONFLICT ... DO UPDATE ... WHERE status <> 'matched' AND changed`) | src/invespend/db.py (append only) | LOW-MED | F7, F8, F17, F25, F32 |
| S4 | `beneficiary_matching_enabled: bool = False` as the last field, loaded via `_opt_bool("BENEFICIARY_MATCHING_ENABLED")` | src/invespend/config.py | LOW | F11 |
| S5 | `sync_and_match(client, url, *, now=...)`: API call first (no conn open), malformed or empty result means keep the snapshot and do nothing; then one short write+read txn, a pure match, and a short upsert txn; logs counts only. Ingest hook placed after the try/except (after finish_sync_run success), gated by `getattr` flag, `except Exception: log.warning(type name)` | src/invespend/beneficiary_sync.py (new), src/invespend/ingest.py (+~7 lines) | MEDIUM | F9, F11, F17, F19, F20 |
| S6 | New test files only: test_beneficiary_migration.py (static SQL), test_beneficiary_match.py (pure + golden JSON), test_beneficiary_sync.py (fake conn/SQL log), test_beneficiary_ingest_freeze.py (cmd_ingest stdout/summary/sync_runs identical for OFF, ON, ON-failing; Excel cell-by-cell digest + COLUMNS literals) | tests/test_beneficiary_*.py, tests/fixtures/beneficiary/*.json | LOW | F2, F5-F21, F25-F28, F30, F32 |
| S7 | Commented flag (false) in .env.example; README section with flag, view, rules, fail-closed, PII note, sample query | .env.example, README.md | LOW | F28, F31 |

Zero diff: cli.py, report.py, statements.py, categorize.py, investec_client.py, payments/**, db/migrations/0001-0011, db/roles.sql, existing tests, pyproject.toml (F3, F8, F10, F22, F23, F30).

Aggregate risk: MEDIUM. Every change is additive. The risky parts are migration 0012, which re-runs on every ingest even with the flag off, and the edit to ingest control flow.
Main flags: R1 0012 is not exercised against a real PG in pytest; R3 full third-party account numbers persist and reach backups (approved under Q1); R4 the type allowlist is unverified, so the matcher may match nothing; R5 an empty API list is treated as a failure so beneficiaries are not mass-retired; R9 the reading of the "J Smith vs J Smithers" pair in F15 should be confirmed by the reviewer.
