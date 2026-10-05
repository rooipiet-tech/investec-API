# Research — run `beneficiary-matching` (phase 0, factual map)

Scope: map everything a "transaction -> registered Investec beneficiary" matcher touches. Read-only research;
no code changed. Line numbers verified against HEAD `ae2ac78`.

## 0. Ground-truth baseline (measured this run, NOT the CLAUDE.md numbers)
- `.venv/bin/python -m pytest -q` -> **211 passed, 0 failed, 0 errors** (1.5s). CLAUDE.md's "62 passing" is stale
  (pre-payments). 23 test files, 203 `def test_` functions (rest are parametrized cases).
- System python has no pytest/invespend; use `/home/user/investec-API/.venv/bin/python`.
- `invespend --help` today lists 8 subcommands: init-db, ingest, report, statements, backup, backfill-hashes,
  **approve-payments, pay-selftest** (cli.py:402-474). `.loop/baseline/help__top.txt` lists only the first 6 -> the
  baseline help snapshot is STALE vs HEAD (diff confirmed). Re-capture baseline before using it as an oracle.
- No test connects to a real Postgres; no test executes any migration SQL (grep `migrat|MIGRATIONS` in tests = 0 hits).

## 1. Module inventory (relevant to this feature)
| Module | Lines | Responsibility / public API | CLI |
|---|---|---|---|
| src/invespend/ingest.py | 199 | `run_ingest(settings, window_days, from_date, to_date, chunk_days, resume, full) -> dict` (43-199); `_date_chunks` (34) | ingest |
| src/invespend/db.py | 392 | `connect`(49), `init_db`(66), `assign_day_seq`(109), `transaction_hash`(127), `upsert_account`(156), `_TX_COLUMNS`(185), `_tx_params`(192), `upsert_transaction(s)`(219/233), `upsert_balance`(283), sync_runs (316/326), `backfill_transaction_hashes`(350) | init-db, ingest, backfill-hashes |
| src/invespend/investec_client.py | 166 | `InvestecClient`: `_get_token`(52), `_get`(77), `get_accounts`(92), `get_balance`(95), `get_transactions`(98), **`get_beneficiaries`(110-116)**, `_post`(126), `create_payment`(141) | ingest, approve-payments, pay-selftest |
| src/invespend/payments/beneficiaries.py | 91 | `Beneficiary` dataclass(13-26), `ALLOWLIST=[]`(31), `from_api`(34-56), `resolve_beneficiary`(59-79), `_matches`(82-91) | approve-payments, pay-selftest |
| src/invespend/categorize.py | 35 | `categorize(description, transaction_type)`(25-35), `_RULES`(9-22) | ingest |
| src/invespend/config.py | 238 | `Settings` frozen dataclass (65-238), `_opt`(48), `_opt_bool`(57), `Settings.load`(177-216) | all |
| src/invespend/report.py | 275 | `COLUMNS`(20-21), `load_transactions`(24-69), `build_spend_summary`(113-191), `write_workbook`(194), `generate_weekly_report`(217) | report |
| src/invespend/statements.py | 649 | `STATEMENT_COLUMNS`(34), `load_account_transactions`(68-121), `build_account_statement`(288-415), `write_statement_workbook`(424-525), `generate_account_statements`(527) | statements |
| src/invespend/cli.py | 484 | `cmd_*`, `build_parser`(402), `main`(477) | all |
| src/invespend/payments/pipeline.py | 288 | uses `bene.from_api(client.get_beneficiaries())` at 85-91; `resolve_beneficiary` at 178 | approve-payments |
| src/invespend/payments/selftest.py | 224 | `_registered_beneficiary`(41-62) uses `from_api(client.get_beneficiaries())` at 53 | pay-selftest |

## 2. Transaction payload as handled
API: `GET /za/pb/v1/accounts/{id}/transactions?fromDate&toDate` -> `data.transactions` list (investec_client.py:98-108).
Keys the code reads from each `tx` dict (db.py:192-216, db.py:98-106, db.py:127-144, ingest.py:156):
`type`(DEBIT/CREDIT), `transactionType`, `status`, `description`, `cardNumber`, `postingDate`, `valueDate`,
`actionDate`, `transactionDate`, `amount`, `runningBalance`. No beneficiary/counterparty field is read — none exists in
the payload as handled.
- `raw jsonb not null` = `json.dumps(tx)` — the **entire unmodified API dict** (db.py:215; schema 0001_init.sql:32).
  So any extra fields Investec returns are preserved in `raw` and queryable (`raw->>'key'`) without re-ingest.
- `amount` stored **signed**: DEBIT -> negative (db.py:194-198). Debit filter = `amount < 0` or `upper(type)='DEBIT'`.
- **Off-limits — dedup/hash:** `transaction_hash = sha256(account_id|valueDate|actionDate|amount|description|day_seq)`
  (db.py:127-144); `day_seq` grouping key includes description (db.py:98-106); upsert is `on conflict
  (transaction_hash) do nothing` (db.py:255-258) — rows are **never updated after insert**. Consequence: a matcher must
  NOT write into `transactions` (no new column update path, no description normalisation at ingest); store results in a
  separate table or compute in a view. 0008_deduplicate.sql:18-35 also partitions on `description`.

### Every read of `description`
- Python: ingest.py:156 (categorize); db.py:105 (`_group_key`), db.py:141 (hash), db.py:205 (column value);
  categorize.py:31; report.py:42,47,56 (select/partition), report.py:171 (Top Merchants groupby);
  statements.py:93,97,105 (select/partition), statements.py:137 (opening-balance partition), statements.py:373 (Description column).
- SQL: 0001_init.sql:23 (column); 0002_views.sql:59 (`btrim(coalesce(description,''))` in transactions_normalized),
  0002:82 (category match); 0006_effective_date.sql:20 (normalized redefined, same btrim); 0005_flow.sql:40,48,55,61,73,77,79,85,98;
  0007_flow_account_number_match.sql:43-45,54,61,67,80-82,87,91,95,109-111 (Tier A/B/C internal-transfer signals);
  0008_deduplicate.sql:26.
### Every read of `transaction_type` / `transactionType`
ingest.py:156; db.py:203; categorize.py:31; 0001:21; 0002:57,82; 0006:18; report.py:42,55 (in COLUMNS ->
emitted in Transactions/Internal Transfers sheets); statements.py:93,105,111 (loaded but NOT in STATEMENT_COLUMNS).

## 3. Which `transaction_type` values denote EFT/payments
- **Nothing in the repo enumerates them.** Only real Investec values evidenced: `CardPurchases`, `FeesAndInterest`
  (0001_init.sql:21 comment; categorize.py:16; tests/test_categorize.py:17). Test-only synthetic values
  `Transfer`, `Salary`, `Subscription` appear in DataFrame fixtures (tests/test_statements.py:46,251,278; tests/test_report.py:17,57) — NOT real API values.
- Payments are only recognised indirectly by **description keywords**: categorize.py:19 `("transfer","payshap",
  "immediate payment","eft","payment to") -> Transfers`; mirrored in category_map seed 0002_views.sql:42-43 (priorities 100-104).
  Note categorized view re-derives category from category_map (0002:75-89) so the ingest-time `category` column is not what reports show.
- 0005/0007 do not use transaction_type at all; they classify internal vs external by account number/name/matched pair.
- UNVERIFIED (external knowledge, confirm with `select transaction_type, count(*) from transactions where amount<0 group by 1`
  on the live DB): Investec PB transactionType values reportedly include e.g. `OnlineBankingPayments`,
  `OnlineBankingTransfers`, `FasterPay`, `DebitOrders`, `Deposits`, `ATMWithdrawals`, `CardPurchases`, `FeesAndInterest`.
  The spec should not hard-code a list without live evidence; a description-based + `amount<0` guard is the evidenced path.

## 4. Beneficiaries API & mapping
- `InvestecClient.get_beneficiaries()` -> `GET /za/pb/v1/accounts/beneficiaries`, returns `.get("data", [])` raw list
  (investec_client.py:110-116). Read-only, same `_get`/token as reads (77-89), retried on 429/5xx (43-49).
- **No beneficiary-categories call exists** (grep `categor` in investec_client.py: none). Only reads: accounts, balance,
  transactions, beneficiaries (92-116). (Investec reportedly also exposes `/za/pb/v1/accounts/beneficiarycategories` — unverified, not wrapped.)
- `from_api` (beneficiaries.py:34-56) field names used: `beneficiaryId` (43, skip if falsy — 44-45), `beneficiaryName`
  then fallback `name` (46), `accountNumber` -> digits -> **last-3 only** (47, 52), `emailAddress` lower-cased (53).
  `match_names` is never populated from the API (only static entries), so API entries match by exact name/email only.
- Other beneficiary fields referenced ANYWHERE in repo: **none** (grep for `referenceName`/`bank`/`lastPaymentDate`/
  `branchCode`/`referenceAccountNumber`/`lastPaymentAmount`/`fasterPaymentAllowed` against beneficiaries: 0 hits).
  `referenceName` appears only for **accounts** (ingest.py:115; db.py:177). The test fixture dicts use only
  beneficiaryId/beneficiaryName/name/accountNumber/emailAddress (tests/test_payment_beneficiaries.py:49-54,65,72-76,87-90,117-120).
  UNVERIFIED external: Investec beneficiary objects reportedly carry `beneficiaryId, accountNumber, code (branch),
  bank, beneficiaryName, lastPaymentAmount, lastPaymentDate, cellNo, emailAddress, name, referenceAccountNumber,
  referenceName, categoryId, profileId, fasterPaymentAllowed`. Since `raw` can be stored whole, the snapshot table
  should keep a raw jsonb and not assume these keys exist.
- `resolve_beneficiary` (59-79) is EXACT equality on name/email (`_matches` 82-91), fail-closed on 0 or >1 — it cannot
  match a free-text description containing a name ("Payment to Acme Pty" != "Acme"); test
  `test_api_allowlist_substring_does_not_match` (tests/test_payment_beneficiaries.py:105-108) pins that. A transaction
  matcher must be a NEW function; do NOT loosen `resolve_beneficiary` (money-movement safety path, 15 tests).

## 5. Migrations
Files (lexical = apply order; `sorted(MIGRATIONS_DIR.glob("*.sql"))`, db.py:80):
0001_init, 0002_views, 0003_day_seq, 0004_balances, 0005_flow, 0006_effective_date, 0007_flow_account_number_match,
0008_deduplicate, **0009_payment_approvals, 0009_security_invoker_rls** (duplicate prefix; "p" < "s" so payment_approvals
applies first), 0010_lock_payment_tables, 0011_deny_all_policies. Next free number: **0012**.
- `init_db` (db.py:66-81): `SET LOCAL lock_timeout='15s'` then executes EVERY migration file every time, in one
  transaction. Called by `init-db` (cli.py:27-32, prints `Schema applied.`) AND at the start of every `ingest`
  (ingest.py:80-81). => a new migration must be fully idempotent (`if not exists`, `create or replace`, drop-then-create
  policy) and cheap; it will re-run on every daily ingest.
- Migration-order "check": there is no pytest test. The tester agent's check is `.claude/agents/tester.md:9-10`
  ("numbers contiguously" + `git diff --stat db/` shows no change to existing SQL). Duplicate 0009 already violates strict
  contiguity — spec should define the check as "existing files byte-identical; new file(s) sort after 0011".
- RLS pattern a new table must follow:
  - 0001_init.sql:59-61 / 0004:53 / 0002:115: `alter table X enable row level security;` (no policies).
  - 0011_deny_all_policies.sql:19-38: per table, guarded by `to_regclass`, `enable row level security`,
    `drop policy if exists deny_all`, `create policy deny_all on public.X for all to public using (false) with check (false)`.
    Its target array (22-26) is hard-coded and must NOT be edited -> the new migration must repeat this pattern for its own table(s).
  - Views: 0009_security_invoker_rls.sql:26-32 ALTERs only the 7 existing views. A new view must set
    `security_invoker = on` itself (e.g. `create or replace view v with (security_invoker = on) as ...` or an `alter view` in
    the same migration); otherwise it bypasses base-table RLS via PostgREST (the exact linter finding 0009 fixed).
  - db/roles.sql:16-23 (commented operator doc) grants report_readonly only listed views; a new view would not be granted
    (fine — reporting doesn't read it).
- Comment inconsistency: 0010_lock_payment_tables.sql:2-3 says payment tables are "created outside this repo" but
  0009_payment_approvals.sql:9-38 creates them.

## 6. Views a beneficiary view would build on
- `transactions_normalized` (latest def 0006_effective_date.sql:13-31): trimmed description, signed amount, `effective_date`,
  `month`, `raw`. `transactions_categorized` (0002:75-89) = normalized.* + category. `transactions_flow` (latest def
  0007:21-116) = categorized.* + `flow_type`, `flow_signal`, `counterparty_account_id`.
- Report (report.py:50) and statements (statements.py:101) read `transactions_flow` with EXPLICIT column lists, so a NEW
  separate view does not affect them. **Do not `create or replace` any existing view** (Postgres only allows appending
  columns, and it would change frozen definitions); a new view e.g. `transactions_beneficiary` should select from
  `transactions_flow` (to reuse `flow_type` and exclude `internal_transfer`) left-joined to a match table/snapshot.
- Minimum the view needs: `transaction_hash` (stable join key; PK of transactions, 0001:18), `account_id`,
  `effective_date`, `description`, `amount`, `flow_type`, beneficiary_id/name/bank/account(display), match rule/score
  for explainability. Note `transaction_hash` can change via `backfill-hashes` (db.py:350-392 updates the PK) — a
  persisted match table keyed by hash can go stale; a view that recomputes, or a match table refreshed each sync, avoids that.
- Existing precedent for description matching in SQL: 0007:43-47 verbatim + digit-normalised `regexp_replace(...,'[^0-9]','','g')`
  substring, guarded by `length>=8`; Tier B name match guarded by `length(nm) > 6` (0007:51-55). Reusable heuristics for a
  beneficiary-account-number signal and short-name guard.

## 7. Config / feature flag pattern
- Optional bool: `_opt_bool(name, default)` (config.py:57-62); field on frozen `Settings` WITH a default
  (e.g. `payments_beneficiaries_from_api: bool = False`, config.py:111-112) and wired in `load()` (config.py:207).
  Documented in `.env.example:55-57`.
- Tests build `Settings(**kwargs)` directly with a subset of fields (tests/test_pay_selftest.py:22-31; tests/test_config.py)
  -> any new field MUST have a default or those tests break.
- Existing flag `PAYMENTS_BENEFICIARIES_FROM_API` governs the *payments allowlist* only (pipeline.py:85-88, selftest.py:50);
  reusing it for matching would couple features — a separate flag (default False) is cleaner.
### Hooking a sync step into ingest without changing output
- `cmd_ingest` prints `Ingest: {summary}` (cli.py:43) where summary is the dict at ingest.py:190-197; then may print
  new-account lines (cli.py:62-66). Adding ANY key to `summary` changes stdout -> forbidden. `sync_runs` counters
  (ingest.py:171-187) must also stay the same.
- Safe hook: after accounts sync (ingest.py:102-107), mirror the balance pattern (ingest.py:129-137): API call with no DB txn
  open, then short `db.connect` write, wrapped in `try/except Exception: log.warning(...)` so a beneficiaries failure is
  non-fatal and doesn't flip the sync run to `error` (the outer try at 97/178 re-raises). Gate on the new flag so default
  path executes zero extra API calls. Logging goes to stderr via `logging.basicConfig` (cli.py `_setup_logging`), not stdout.
- Alternative: a separate subcommand would change the CLI surface (needs explicit spec allowance) — init-db + view-only
  match + ingest hook avoids that.

## 8. Report / statements — what "unchanged output" protects
- Weekly report: query columns report.py:37-60, `COLUMNS` report.py:20-21
  (`effective_date, account_number, account_name, type, transaction_type, description, amount, category, flow_type`).
  Sheets: Summary(140-156), By Category(158), By Account(165), Top Merchants(170, groups on description), Daily Trend(175),
  Internal Transfers(180 — full df columns), Transactions(181 — full df columns), Monthly Movement(264), Reconciliation(268).
  Adding a column to `load_transactions` would change the Transactions + Internal Transfers sheets. Stdout line cli.py:157.
- Statements: `STATEMENT_COLUMNS = ["Date","Description","Category","Debit","Credit","Balance"]` (statements.py:34),
  built at 370-379, `reindex(columns=STATEMENT_COLUMNS)` at 458, 14-row header block 452-487. Stdout cli.py:216.
- Both read `transactions_flow`; untouched as long as no existing view is replaced.

## 9. Tests that constrain / help
- Fake Investec clients with `get_beneficiaries`: tests/test_pay_selftest.py:34-48 (`_Client(accounts, beneficiaries_raw)`),
  tests/test_payment_pipeline.py:33,246,302. No fake for `get_transactions`/`run_ingest` — **run_ingest has no test**
  (tests/test_ingest_chunks.py only tests `_date_chunks`).
- Fake DB: `_FakeConn/_FakeCursor` + `monkeypatch.setattr(db, "_open", ...)` (tests/test_payment_pg_store.py:18-77);
  fake cursor in tests/test_backfill.py:11. Pure helpers tested without DB: tests/test_db.py (`_tx_params` 16 columns pinned at :9-11 and amount index 11 at :16-19).
- Constraining: tests/test_payment_beneficiaries.py (15 tests pin from_api + exact-only resolve); tests/test_report.py:43
  pins sheet names; tests/test_statements.py:148 pins statement column header; tests/test_cli.py pins ingest/report args;
  tests/test_categorize.py pins keyword rules.
- Matching logic should be a pure function (list[dict tx] x list[beneficiary] -> match) to be unit-testable offline, as
  report/statements do (pure builders + thin loaders).

## 10. Risks (ranked)
HIGH
- H1 Ambiguity: beneficiaries sharing a name or where one name is a prefix/substring of another ("Acme" vs "Acme Trading",
  "J Smith" vs "J Smithers"); short names ("Mom", "Rent") matching unrelated text; beneficiary names colliding with own
  account holder names (0007 Tier B, 51-55) — such debits are already `internal_transfer` and should be excluded. Must fail
  closed when >1 candidate (mirror beneficiaries.py:76-79) and guard minimum name length (precedent 0007:53 `>6`).
- H2 Hash/dedup coupling: any write to `transactions` or change to `description` handling breaks idempotency (db.py:127-144,
  233-261) and `backfill-hashes`. Keep matcher read-only over transactions.
- H3 Observable output drift: adding summary keys (ingest.py:190-197 -> cli.py:43), touching report COLUMNS (report.py:20),
  `create or replace` of existing views, or editing 0001-0011 would violate the frozen contract.
- H4 PII: repo convention is last-3 only for beneficiary/payee account numbers (beneficiaries.py:23-24; 0009_payment_approvals.sql:6-7;
  notify.py:9,23 forbids "full account"). The GOAL asks to show "account number". Storing full beneficiary account numbers
  (in a column or a raw jsonb snapshot) departs from that convention; note `accounts.account_number` already stores full own
  numbers (0001:6) and descriptions can contain full numbers (0007:11-12 comment). Spec must decide: full number behind
  deny_all RLS vs last-3/masked display; and whether the snapshot `raw` is stored at all (it would contain the full number,
  email, cell number).
MEDIUM
- M1 Description truncation/format: Investec description text is free-form and may be truncated or abbreviated (unverified
  length); the only evidenced EFT format is "Transfer to ... <acct> <name> <acct>" (0007:11-12, 0005:35-36). Name may appear
  partially -> prefix matching temptation increases false positives. Prefer: beneficiary account-number digit match
  (0007-style normalisation, long-number guard) > reference/name exact token match > no match.
- M2 Unknown EFT `transactionType` values (section 3) — gating on an unverified enum could silently match nothing.
- M3 Migration re-applied every ingest (ingest.py:80-81) inside `lock_timeout 15s`; heavy view/index DDL adds lock risk.
- M4 `transaction_hash` mutable via backfill-hashes (db.py:385-390) -> persisted match rows keyed by hash may orphan.
- M5 Snapshot staleness: beneficiaries deleted/renamed in Investec after a payment; historic transactions matched against the
  current list only. Snapshot needs first/last-seen semantics or matches are "as of current registry".
LOW
- L1 Duplicate 0009 prefix (0009_payment_approvals vs 0009_security_invoker_rls) — order is lexical and stable but confusing.
- L2 0010 comment contradicts 0009_payment_approvals (section 5).
- L3 Stale baselines: CLAUDE.md "62 passing" and `.loop/baseline/help__top.txt` (6 subcommands) vs actual 211 / 8 subcommands.
- L4 `exports/groups_export.xlsx` exists although `*.xlsx` is git-ignored (.gitignore) — unrelated, noted only.

## 11. Coupling notes (where observable-behaviour risk concentrates)
- `ingest.py` <-> `cli.py:43`: the summary dict IS stdout. Any ingest extension must be side-channel (DB + logging).
- `db.init_db` runs all SQL on every ingest: a broken new migration breaks daily ingest, not just init-db.
- `transactions_flow` is the shared read model for report + statements + monthly_flows; new objects must sit beside it, not replace it.
- `payments/beneficiaries.py` is shared with the money-moving pipeline; reuse `from_api` read-only at most (it drops all but
  4 fields and keeps only last-3), and put matching in a new module (e.g. `src/invespend/beneficiary_match.py`) to avoid
  touching payment-safety semantics.
- Dependencies: everything needed (requests, psycopg, stdlib re/difflib) already present; no new deps required.
