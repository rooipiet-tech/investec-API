# Build note: beneficiary-matching, iteration 1

Base SHA: `c2bbfaed2d58e91658ab92e3bb2cd694a249030d` (recorded in `.loop/baseline/BASE_SHA`).
Test runner: `uv run --frozen pytest -q` (`--frozen` so `uv.lock` is never rewritten; `uv.lock` unchanged).

## Stages

| Stage | Status | Files |
|---|---|---|
| S0 | done, own commit `c6ef1d3` | `.loop/baseline/help__top.txt` (refreshed), `help_{init-db,ingest,report,statements,backup,backfill-hashes}.txt` (re-captured, unchanged bytes), `help_approve-payments.txt`, `help_pay-selftest.txt` (new), `.loop/baseline/BASE_SHA`, `tests/fixtures/beneficiary/excel_golden.json` (T8), `tests/fixtures/beneficiary/migration_hashes.json` |
| S1 | done | `db/migrations/0012_beneficiary_matching.sql` (new) |
| S2 | done | `src/invespend/beneficiary_match.py` (new, pure, stdlib only) |
| S3 | done | `src/invespend/db.py` (append-only, +157 lines, 0 deletions) |
| S4 | done | `src/invespend/config.py` (+5 lines: field + load line) |
| S5 | done | `src/invespend/beneficiary_sync.py` (new), `src/invespend/ingest.py` (+10 lines, 0 deletions) |
| S6 | done | `tests/test_beneficiary_match.py`, `tests/test_beneficiary_sync.py`, `tests/test_beneficiary_migration.py`, `tests/test_beneficiary_ingest_freeze.py`, `tests/fixtures/beneficiary/golden_descriptions.json` (all new) |
| S7 | done | `.env.example` (+3 lines), `README.md` (+55 lines, new section) |

## Test counts

- Base (T12, re-measured at c2bbfae): **211 passed**, 0 failed, 0 errors.
- After S1-S7: **310 passed**, 0 failed, 0 errors (211 + 99 new).
- Mutation sanity check: adding a `print` inside the hook makes `test_ingest_output_frozen_off_vs_on` fail (reverted).

## Checks

- CLI help (top + 8 subcommands) byte-identical to refreshed baseline after the change (captured with `COLUMNS=80`).
- Zero diff vs base: `db/migrations/0001-0011`, `db/roles.sql`, `cli.py`, `report.py`, `statements.py`, `payments/`, `pyproject.toml`, `uv.lock`, all pre-existing test files.
- `.env` still git-ignored; not created or staged.
- Scratch Postgres 16.13 cluster (scratchpad): fresh DB, `init_db` applied 3x then a 4th time -> `pg_dump --schema-only` identical (only pg_dump's random `\restrict` key differs). Upgrade path: base migrations 0001-0011 applied, dumped, then `init_db` x2 -> diff contains only ADDED lines, all for `beneficiaries`, `beneficiary_matches`, `transactions_beneficiary`, their RLS and deny_all policies (no pre-existing object changed). End-to-end `sync_and_match` against real PG: matched/ambiguous/own-account exclusion, soft-retire (retired beneficiary still resolves in the view), orphan match row invisible in view, 10001-row match upsert in batches OK. Not a test dependency.

## Tightenings applied

T1 hook uses `beneficiary_sync.sync_and_match` -> `db.connect` via module attribute; ingest-freeze test patches `db.connect`, makes `db._open` a tripwire, asserts `get_beneficiaries` call count and writes to both new tables. T2 J Smith/J Smithers and Acme Trading/Acme Trading Two golden cases + docstring + README. T3 truncation branch requires >= 2 words (`one_word_generic_truncation` fixture). T4 account rule matches within a single digit run (`account_digits_cross_boundary` fixture; space and hyphen grouping match). T5 static SQL tests strip `--` comments, check statement heads, pin ordered view column list. T6 append-only SQL comment in 0012 + pinned-column test. T7 `now=` injected; idempotency compares full SQL+params of two runs. T8 golden Excel digest generated at base, committed in S0, compared after running the hook ON. T9 match/beneficiary upserts batched at 500 rows; test with 1201 rows -> 500/500/201. T10 retirement is `update ... where active and not (beneficiary_id = any(fetched_ids))`, never DELETE. T11/T13 documented in README (no roles.sql edit, no code change). T12 base re-measured (211).

## Deviations / notes

- `retire_missing_beneficiaries(conn, fetched_ids)` takes fetched ids instead of `synced_at` (per T10, supersedes the plan signature).
- `load_match_candidates` filters `upper(f.type) = 'DEBIT'` (plan text had `f.type = 'DEBIT'`); the Python `is_candidate` gate is the authority either way.
- `match_all` additionally de-duplicates results per `transaction_hash` (deterministic) so a single INSERT never hits the same key twice.
- `parse_beneficiaries` sorts/dedupes on the full allowlisted tuple (id first) so the dedupe winner is independent of input order.
- Extra pinned fixture `tests/fixtures/beneficiary/migration_hashes.json` (sha256 of 0001-0011 at base) committed in S0 for `test_existing_migrations_unchanged`.
- Synthetic account numbers in tests/README use the obviously-fake `9999 0000 ...` pattern (12 digits); Settings in tests use dummy `test-secret`/`test-key` values. Risk-security greps for long digit runs / `client_secret` will hit these synthetic values.
- Sync logs counts only; ingest hook logs only the exception class name.
