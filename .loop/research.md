# invespend — Codebase Research Map

Factual ground-truth for the refactor. Stack: Python 3.12 target (CI uses 3.12; `pyproject.toml` declares `requires-python = ">=3.10"`). Deps: requests, psycopg[binary], pandas, openpyxl, python-dotenv. Console entry point: `invespend = "invespend.cli:main"` (`pyproject.toml:21-22`). Baseline test count: **62 passing** (`pytest --collect-only` → "62 tests collected").

---

## 1. Module inventory (`src/invespend/*.py`)

| Module | Lines | Responsibility | Public surface | Serves subcommand(s) |
|---|---|---|---|---|
| `__init__.py` | 10 | Package version + the shared Excel `MONEY_FORMAT` constant (`__init__.py:10`). | `__version__`, `MONEY_FORMAT` | report, statements |
| `cli.py` | 310 | argparse wiring + per-subcommand handlers; group fan-out for report/statements; new-account alert email orchestration. | `build_parser`, `main`, `cmd_*` handlers, `_send_group_report`, `_send_group_statements`, `_alert_new_accounts`, `_money`, `_report_body`, `_statements_body` | all (init-db, ingest, report, statements, backup, backfill-hashes) |
| `config.py` | 113 | Env-driven `Settings` dataclass (frozen); `.env` load; email-validation helper. | `Settings` (+ `.load`, `.reporting_db_url`, `.require_email`), `_require`, `_opt` | all (every handler calls `Settings.load()`) |
| `db.py` | 392 | Postgres persistence: connect/retry, migrations apply, hashing/dedup, upserts (account/tx/balance), sync-run log, hash backfill. | `connect`, `init_db`, `oldest_posting_date`, `get_account_ids`, `assign_day_seq`, `transaction_hash`, `upsert_account`, `upsert_transaction`, `upsert_transactions`, `extract_balance_fields`, `upsert_balance`, `start_sync_run`, `finish_sync_run`, `backfill_transaction_hashes`; privates `_open`,`_group_key`,`_parse_date`,`_tx_params`,`_TX_COLUMNS` | init-db, ingest, backfill-hashes (+ report/statements via `connect`) |
| `ingest.py` | 199 | Daily sync + historical backfill orchestration (API→DB, windowed, decoupled transactions). | `run_ingest`; privates `_date_chunks`; consts `CHUNK_DAYS=90`, `MAX_EMPTY_CHUNKS=3` | ingest |
| `investec_client.py` | 100 | Investec Open API read-only client: OAuth2 client-credentials, accounts/balance/transactions reads, retry/session. | `InvestecClient` (+ `get_accounts`, `get_balance`, `get_transactions`) | ingest |
| `categorize.py` | 35 | Rule-based keyword categoriser (ordered `_RULES`, first match wins). | `categorize` | ingest (write-time category) |
| `report.py` | 275 | Weekly analytical workbook: load tx/movement/recon from views, pure aggregation, formatted xlsx. | `load_transactions`, `load_monthly_movement`, `load_reconciliation`, `build_spend_summary`, `write_workbook`, `generate_weekly_report`; const `COLUMNS` | report |
| `statements.py` | 649 | Per-account bank-statement workbooks: load per-account tx, intraday running-balance chain sort, statement build, formatted xlsx, reconciliation. | `Account` (dataclass), `load_accounts`, `load_account_transactions`, `load_opening_balance`, `load_investec_balance`, `build_account_statement`, `write_statement_workbook`, `generate_account_statements`; privates `_greedy_chain`,`_chain_sort_day`,`_safe_filename`; consts `STATEMENT_COLUMNS`,`_MONEY_COLS` | statements |
| `account_alert.py` | 138 | Detect new/unclaimed accounts; build alert email body/subject; group-suggestion heuristics. | `suggest_group`, `find_unclaimed`, `build_alert_email`; consts `_PERSONAL_HINTS`,`_BUSINESS_HINTS` | ingest (via cli `_alert_new_accounts`) |
| `groups.py` | 47 | Hard-coded account groupings + exclusions (the single config point). | `Group` (dataclass + `.matches`), `EXCLUDE_ACCOUNTS`, `GROUPS` | report, statements, ingest-alert |
| `emailer.py` | 72 | SMTP send with attachments. | `send_email`, `send_report` (back-compat wrapper); private `_attach` | report, statements |
| `backup.py` | 70 | `pg_dump` → gzip → optional openssl AES-256 encryption. | `run_backup`; const `_DUMP_PGOPTIONS` | backup |

Note: `scripts/export_groups.py` (182 lines) is a standalone helper outside the package, run only by the `export-groups` workflow; imports `invespend.groups`. Not part of the CLI surface.

---

## 2. CLI surface (the observable contract)

`build_parser()` (`cli.py:256-300`): `prog="invespend"`, subparsers `dest="command", required=True`. `main()` (`cli.py:303-306`) sets up logging then dispatches `args.func(args)`. Every handler returns `int` (0 on success).

Subcommands and exact stdout:

- **`init-db`** (`cli.py:260`, handler `cmd_init_db` 27-32) — no args. Applies all migrations. Prints `"Schema applied."`.
- **`ingest`** (`cli.py:262-275`, handler `cmd_ingest` 35-45). Flags:
  - `--days` (int, default None) — "Days of history to pull (default: INGEST_WINDOW_DAYS)"
  - `--from` → `dest=from_date` (default None) — "Backfill start date YYYY-MM-DD (overrides --days)"
  - `--to` → `dest=to_date` (default None) — "End date YYYY-MM-DD (default: today)"
  - `--resume` (store_true) / `--full` (store_true)
  - Prints `f"Ingest: {summary}"` (summary is a dict, see ingest §6). Then `_alert_new_accounts` may print: `"New account(s) detected: [...]"`, optionally `"Unclaimed account(s) needing group assignment: [...]"`, and on send `"Alert email sent to: ..."`.
- **`report`** (`cli.py:277-279`, handler `cmd_report` 160-174). Flag: `--send` (store_true) "Email the report". For each group + default bucket prints `f"Report {tag} written: {path} ({info})"` (`cli.py:146`) and, if sent, `f"Report {tag} emailed."` (`cli.py:157`). `tag` is `"({label})"` or `"(default)"`.
- **`statements`** (`cli.py:281-290`, handler `cmd_statements` 221-234). Flags: `--send` (store_true), `--days` (int, default None) "Trailing window in days; omit for full history". Prints `f"Statements {tag}: {len(paths)} file(s)"` (`cli.py:201`) and, if sent, `f"Statements {tag} emailed."` (`cli.py:218`).
- **`backup`** (`cli.py:292-293`, handler `cmd_backup` 237-241) — no args. Prints `f"Backup written: {path}"`.
- **`backfill-hashes`** (`cli.py:295-298`, handler `cmd_backfill_hashes` 244-253) — no args; reads `DATABASE_URL` directly (env first, then `Settings.load()`). Prints `f"Backfill: {summary}"`.

Observable-contract specifics that MUST be preserved:
- The `_money` formatter (`cli.py:177-180`): `f"{value:,.2f}".replace(",", " ")` → space-grouped thousands; `None`→`"n/a"`. Used in `_statements_body`.
- Email bodies `_report_body` (100-105) and `_statements_body` (108-121) — exact strings, the trailing `"— invespend"`, bullet `"  • "`.
- The group fan-out order in `cmd_report`/`cmd_statements`: named GROUPS first (in list order), then the default bucket with all named names/numbers excluded (`cli.py:163-173`, `224-233`).
- Tests pin parser behaviour: `tests/test_cli.py` asserts `from_date`/`to_date`/`days`/`resume`/`full`/`send` defaults and dest names.

---

## 3. Test inventory

All tests are **pure-unit** — none open a real DB. DB-touching code is exercised via fakes/pure functions. Total **62** test functions.

| File | Tests | Covers | DB? |
|---|---|---|---|
| `test_account_alert.py` | 14 | `find_unclaimed`, `suggest_group`, `build_alert_email`; uses `monkeypatch.setattr(groups_module, "GROUPS"/"EXCLUDE_ACCOUNTS", ...)`. | No (monkeypatch) |
| `test_backfill.py` | 2 | `backfill_transaction_hashes` rekey + idempotency, via `_FakeCursor`/`_FakeConn` in-memory stand-ins (lines 10-49). | No (fake conn) |
| `test_balances.py` | 2 | `extract_balance_fields` mapping + None/default-currency. | No (pure) |
| `test_categorize.py` | 3 | `categorize` known merchants, fallback, transactionType use. | No (pure) |
| `test_cli.py` | 3 | `build_parser` arg defaults/dests for ingest + report. | No (pure) |
| `test_config.py` | 2 | `Settings.load` credential stripping + app-password space removal; uses `importlib.reload` + `monkeypatch.setenv`. | No (env) |
| `test_db.py` | 2 | `_tx_params`/`_TX_COLUMNS` arity + debit-sign-negative. | No (pure) |
| `test_dedup.py` | 4 | `assign_day_seq` + `transaction_hash` distinctness, repull idempotency, per-account isolation, null==missing date. | No (pure) |
| `test_ingest_chunks.py` | 3 | `_date_chunks` coverage, single-chunk, chunk-size. | No (pure) |
| `test_report.py` | 7 | `build_spend_summary` totals/sort/empty/internal-exclusion/by-account; `write_workbook` + accounting format (reads xlsx with openpyxl, `tmp_path`). | No (pure + tmp file) |
| `test_statements.py` | 20 | `build_account_statement` ordering/split/gap-fill/totals/internal/inference/blank-anchor/empty; `write_statement_workbook` (+recon pass/fail/none); `_chain_sort_day` intraday cases; `_safe_filename`. | No (pure + tmp file) |

Counts sum to 62. No `conftest.py`, no live-DB fixtures, no network. Refactors that keep the public function signatures listed in §1 will keep these green.

---

## 4. DB migrations (`db/migrations/*.sql`)

Applied in filename sort order by `db.init_db` (`db.py:78-81`), each idempotent. Also `db/roles.sql` exists (read-only role; not applied by init-db).

1. **0001_init** — Base tables: `accounts` (PK `account_id`), `transactions` (PK `transaction_hash`, signed `amount numeric(18,2)`, `raw jsonb`, `ingested_at`), `sync_runs` audit. Indexes `idx_tx_account_date`, `idx_tx_posting_date`, `idx_tx_category`. **Enables RLS (no policies)** on accounts/transactions/sync_runs — the security model; direct Postgres role bypasses it.
2. **0002_views** — `category_map` table + seed (mirrors `categorize.py`); views `transactions_normalized`, `transactions_categorized`, `spend_by_category`, `monthly_movement`. RLS on `category_map`.
3. **0003_day_seq** — `alter table transactions add column day_seq int not null default 0`. Underpins the dedup hash.
4. **0004_balances** — `balances` table (unique `(account_id, as_of_date)`), index, **view `balance_reconciliation`**. RLS on balances.
5. **0005_flow** — index `idx_tx_value_amount`; **view `transactions_flow`** (3-tier internal-transfer classifier + `flow_signal` + `counterparty_account_id`); redefines `spend_by_category` (now excludes internal transfers); new view `monthly_flows`.
6. **0006_effective_date** — redefines `transactions_normalized` so `effective_date` precedence becomes `transaction_date, action_date, value_date, posting_date` (supersedes 0002's posting-first order). Cascades to all downstream views on next read.
7. **0007_flow_account_number_match** — redefines `transactions_flow` (supersedes 0005): Tier-A now also digit-normalised match (`regexp_replace … '[^0-9]'`). Strictly additive.
8. **0008_deduplicate** — one-off DELETE of duplicate rows keyed on `(account_id, amount, description, running_balance, effective_date)`, keeping newest `ingested_at`; only rows with non-null `running_balance`. Idempotent.

Schema objects present (final state): tables `accounts`, `transactions`, `sync_runs`, `category_map`, `balances`; views `transactions_normalized`, `transactions_categorized`, `spend_by_category` (v2), `monthly_movement`, `transactions_flow` (v2), `monthly_flows`, `balance_reconciliation`.

**Refactor must NOT disturb** (no schema changes per goal, but Python reads these contracts):
- View column lists consumed by Python: `transactions_flow` columns (`effective_date, account_id, type, transaction_type, description, amount, category, flow_type, running_balance, transaction_hash, …`) — `report.load_transactions` (`report.py:37-60`) and `statements.load_account_transactions` (`statements.py:91-110`), `load_opening_balance` (124-149).
- `monthly_movement` columns `month, category, total_spend, prev_month_spend, movement` (`report.py:78-83`).
- `balance_reconciliation` columns (`report.py:98-105`).
- The `flow_type` literal **`'internal_transfer'`** string is matched in Python (`report.py:130-131`, `statements.py:390`) — do not rename.
- The dedup window function in `report.py`/`statements.py` partitions by `(account_id, amount, description, running_balance, effective_date)` ordered by `ingested_at desc` — mirrors 0008; must stay aligned if touched.
- Migration filename sort order is load-bearing (later migrations supersede earlier view defs).

---

## 5. Code smells / refactor opportunities

Ranked LOW/MEDIUM/HIGH = risk-to-change (HIGH = touching it most easily perturbs observable behaviour).

**Duplication (good targets, mostly LOW–MEDIUM):**
- **Account include/exclude filtering** is implemented three times with subtly different shapes: `report.generate_weekly_report` (`report.py:241-258`, pandas `.apply` mask), `statements.generate_account_statements` (`statements.py:563-576`, per-row Python), `account_alert.find_unclaimed`/`groups.Group.matches` (`groups.py:25-28`, `account_alert.py:63-73`). A shared matcher helper would remove ~3 copies. **MEDIUM** (report uses name+number-in-one-mask semantics; statements applies exclude-then-include separately — behaviour差 must be preserved exactly).
- **Group fan-out** in `cmd_report` (`cli.py:160-174`) and `cmd_statements` (`cli.py:221-234`) is near-identical (compute `all_names`/`all_numbers`, loop groups, then default bucket). Extractable to one helper taking the per-group sender. **LOW–MEDIUM**.
- **`_send_group_report` vs `_send_group_statements`** (`cli.py:124-157` / `183-218`) share recipient-resolution + "no recipients → warn + skip" + tag logic. **LOW–MEDIUM**.
- **The dedup CTE / ROW_NUMBER block** is copy-pasted across three queries in `report.py` (37-60) and `statements.py` (91-110, 133-149). SQL — **HIGH** to consolidate (any drift changes which row survives dedup → changes numbers).
- **SMTP send loop** is duplicated: `emailer.send_email` (`emailer.py:57-60`) and the inline SMTP block in `cli._alert_new_accounts` (`cli.py:84-97`). The alert path reimplements message-building instead of reusing `send_email`. **LOW**.
- **categorize.py `_RULES` vs migration 0002 `category_map` seed** are two hand-synced copies of the same keyword→category map (`categorize.py:9-22` ↔ `0002_views.sql`). Documented as intentional ("Mirrors categorize.py") but a real duplication/maintenance smell. **MEDIUM** (changing either alters categorisation; behaviour-sensitive).

**Long / complex functions:**
- `statements._chain_sort_day` (`statements.py:192-285`, ~93 lines) — the intraday greedy-chain reorder with 3 cases and NULL-rb gap placement. The single most intricate logic; well-tested (4 dedicated tests) but dense. **HIGH** risk; refactor only with the chain-sort tests as a guard.
- `statements.build_account_statement` (288-415, ~127 lines) — mixes sort, chain reorder, balance carry, column split, totals, display reverse. Splittable into smaller pure helpers. **MEDIUM–HIGH**.
- `statements.write_statement_workbook` (424-524) and `generate_account_statements` (527-649) — long, mix DB I/O + formatting + reconciliation. **MEDIUM** (formatting is observable in xlsx but not asserted cell-exact beyond a few tests).
- `ingest.run_ingest` (`ingest.py:43-199`, ~156 lines) — orchestration mixing API calls, several short DB transactions, new-account detection, backfill early-stop. Extractable phases (accounts, balances, transactions). **MEDIUM**.

**Mixed concerns:**
- `cli.py` holds business logic (alert-email construction + SMTP in `_alert_new_accounts`, recipient resolution, body builders) that arguably belongs in `account_alert.py`/`emailer.py`. **LOW–MEDIUM**.
- `db.backfill_transaction_hashes` (`db.py:350-392`) embeds grouping + rehash logic inline; partly mirrors `assign_day_seq`. **LOW**.

**Magic constants / literals:**
- Reconciliation tolerance `0.01` appears in `statements.py:603` and SQL (`0004`, `0007` via `< 0.01`); chain-sort tolerance `0.015` (`statements.py:267`); `round(...,2)` scattered. Centralising risks divergence — **MEDIUM**.
- `header_rows = 14` (`statements.py:453`) and the hard-coded `widths` dicts (`statements.py:511`, `report.py:205`) are magic but isolated. **LOW**.
- Hex fill colours and `Font` styling are inline literals in `statements.write_statement_workbook` and `scripts/export_groups.py`. **LOW**.
- `"internal_transfer"` / `"external_inflow"` / `"external_outflow"` string literals used in both SQL and Python — should be named constants but value must not change. **MEDIUM**.

**Inconsistent naming / typing:**
- `report_database_url` (field) vs `reporting_db_url` (property) — close names, easy confusion (`config.py:65,94`). **LOW**.
- `load_investec_balance` return is annotated as a forward-ref string `tuple[float | None, "date | None"]` (`statements.py:158`) — inconsistent with `from __future__ import annotations` already in effect (the quotes are redundant). **LOW**.
- `loaded: list[tuple[..., object]]` (`statements.py:558`) uses `object` for `investec_date` instead of `date | None`. **LOW**.
- Several broad `except Exception` with `# noqa: BLE001` (ingest balance loop, report optional sheets, cli alert) — intentional but worth a typed/narrowed pass. **LOW** (changing catch scope is behavioural — keep as-is unless certain).

**Dead / questionable code:**
- **Workflow `ingest.yml` passes `REPORT_GROUP_1..5_*` and `REPORT_EXCLUDE_ACCOUNTS` env vars that nothing reads** — groups are hard-coded in `groups.py`; `config.Settings` never reads `REPORT_GROUP_*`. Confirmed via grep (only hit is the workflow). Dead config surface (CI-only, not Python code). **LOW** to note; out of refactor scope unless cleaning CI.
- `upsert_transaction` (single-row, `db.py:219-230`) appears unused by `ingest` (which uses batched `upsert_transactions`); only its `_tx_params` helper is unit-tested. Possible dead public function — verify no external caller before removal. **LOW**.
- `send_report` (`emailer.py:64-72`) is a thin back-compat wrapper around `send_email`; only `cli._send_group_report` uses it. Could be inlined. **LOW**.
- `categorize` is called at ingest write-time, but the report/statements read `category` from the `transactions_flow`/`category_map` build layer, so the ingest-time category is effectively overridden downstream. Not dead, but the two categorisation paths are redundant by design. **MEDIUM** to touch.

---

## 6. Coupling notes (import graph + risk concentration)

High-level import graph (who imports whom):

```
cli.py ──▶ db, account_alert, backup, config, emailer, groups, ingest, report, statements
ingest.py ──▶ db, categorize, config, investec_client
report.py ──▶ __init__(MONEY_FORMAT), db, config
statements.py ──▶ __init__(MONEY_FORMAT), db, config
account_alert.py ──▶ groups, config
backup.py ──▶ config
emailer.py ──▶ config
config.py ──▶ groups (late import inside require_email, to avoid circular)
db.py ──▶ (stdlib + psycopg only; no intra-package imports)
groups.py ──▶ (none)
categorize.py ──▶ (none)
investec_client.py ──▶ (requests only)
```

- `config.Settings` is the universal hub (every handler calls `Settings.load()`); `db.connect` is the second hub (ingest, report, statements, cli all use it).
- `groups.GROUPS`/`EXCLUDE_ACCOUNTS` are module-level globals read directly by `cli`, `account_alert`, `config.require_email`, and tests (which monkeypatch the *module* attribute — so any refactor that changes how GROUPS is referenced must keep it patchable at `invespend.groups.GROUPS`).
- Note the deliberate late import `from . import groups as _groups` in `config.require_email` (`config.py:111`) to avoid a circular import; preserve.

Where observable-behaviour risk concentrates (treat as HIGH-care zones):

1. **Ingestion dedup / hashing** — `db.assign_day_seq` + `db.transaction_hash` + `db._group_key` (`db.py:98-144`) define row identity. The same functions back live ingest, `backfill_transaction_hashes`, and the dedup window functions in SQL. Any change to field order, the `or ""` null handling, or `day_seq` semantics changes which rows are considered duplicates → silently alters stored data and every downstream number. Guarded by `test_dedup.py` + `test_backfill.py`. **HIGHEST risk.**

2. **Report formatting / aggregation** — `report.build_spend_summary` (`report.py:113-191`) defines sheet set, internal-transfer exclusion, rounding, sort orders; `report.write_workbook` defines money-format application. Sheet names/order and the empty-frame `{...7 sheets...}` contract (`report.py:121-125`) are asserted by `test_report.py`. **HIGH.**

3. **Statement generation / running balance** — `statements.build_account_statement` + `_chain_sort_day` + `_greedy_chain` (`statements.py:175-415`). The intraday chain sort and balance carry-forward directly determine printed balances and totals; the newest-first display reversal and the blank-anchor rule are observable. `STATEMENT_COLUMNS` order is the on-statement layout. Reconciliation tolerance `0.01`. **HIGH.**

4. **CLI stdout + email bodies** — every `print(...)` in `cli.py` and the body builders (`_report_body`, `_statements_body`, `_money`) are the user-facing contract. Group fan-out ordering. **MEDIUM–HIGH.**

5. **SQL ↔ Python contracts** — view column lists, the `'internal_transfer'` literal, and the dedup CTE shape must stay synchronised across `report.py`/`statements.py` and `db/migrations`. Schema is frozen by the goal, but Python-side column unpacking is positional (`Account(*row)`, fixed `cols` lists) so column-order assumptions are load-bearing. **MEDIUM–HIGH.**

Lower-risk, well-isolated zones suitable for early refactoring: `categorize.py`, `groups.py`, `emailer.py`, `backup.py`, `config.py` (pure/IO-thin, fully or nearly fully unit-covered).
