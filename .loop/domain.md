# Domain constraints: beneficiary-matching run

reused_learnings: L-0003 (PII allowlist-at-write), L-0006 (machine-readable envelope, if a new CLI surface is added),
L-0007 (isolated subpackage, additions-only, freeze check). From .loop/archive/payments-run/domain.md these still apply:
FAIL-CLOSED-ON-AMBIGUITY, LAST3-IS-WEAK-SELECTOR, POPIA-LAWFUL-MINIMISATION, "never log full account numbers".
The payment-execution constraints (token/caps/live switch) are out of scope here. Matching is read-only labelling.

Environment facts checked this run: `.venv/bin/python -m pytest -q` gives **211 passed** (CLAUDE.md's "62" is stale; use 211
as the floor). The system python has no psycopg, so use `.venv/bin/python`.

---------------------------------------------------------------------------------------------------
## 1. Investec Programmable Banking (ZA PB v1) facts
Labels: [REPO] = ground truth in this repo. [DOC] = Investec public docs, high confidence. [INF] = inferred, verify.

### 1.1 Transactions (`GET /za/pb/v1/accounts/{id}/transactions`, investec_client.py:99-110)
- [REPO] Response path is `data.transactions[]` (investec_client.py:110).
- [REPO] Fields the code reads are `type`, `transactionType`, `status`, `description`, `cardNumber`, `postingDate`,
  `valueDate`, `actionDate`, `transactionDate`, `amount`, `runningBalance` (db.py:192-216). The whole payload is also
  kept in `transactions.raw` jsonb (db.py:215, 0001_init.sql:32).
- [REPO] `amount` from the API is unsigned. The sign comes from `type`: DEBIT gives a negative amount, anything else is
  positive (db.py:194-198). A DEBIT row has `amount < 0` and `type='DEBIT'`.
- [DOC] The response has **no beneficiary or counterparty field**: no beneficiaryId, no counterparty account, no
  structured reference. `description` is the only counterparty signal. The repo already relies on this: 0005_flow.sql:7-8
  says "counterparty identifiers Investec writes into the description".
- [DOC/INF] Newer responses may also carry `accountId` and `uuid`. These are in `raw` only, not in columns. Do NOT
  assume `uuid` is stable or present: db.py:128 says "the public API has no stable transaction id".
- [INF] How an outgoing EFT's `description` is built: it is usually the **"my reference"** text captured when the payment
  was made (the default for a saved beneficiary is the beneficiary's `referenceName`), or else the beneficiary name. It
  may be **uppercased**, **truncated** (SA bank statement reference fields are short, around 20-30 chars), have collapsed
  or extra whitespace, and carry bank-added prefixes or suffixes (for example "Transfer to ...", "PAYMENT TO ...", or a
  digit-grouped account number as in 0007_flow_account_number_match.sql:11-13). Expect partial or prefix overlap, not
  equality.
- [INF] `transactionType` values for payments are NOT verified. Repo fixtures are synthetic: tests use "CardPurchases",
  "Transfer", "Salary", "Subscription" (tests/test_statements.py:46). 0001_init.sql:21 mentions "CardPurchases",
  "FeesAndInterest". Likely real values include `OnlineBankingPayments`, `Transfers`, `FasterPay`, `DebitOrders`,
  `CardPurchases`, `ATMWithdrawals`, `FeesAndInterest`, `Deposits`, but this is UNVERIFIED. **Open question Q2.**

### 1.2 Beneficiaries (`GET /za/pb/v1/accounts/beneficiaries`, investec_client.py:112-118)
- [REPO] Response path is `data[]`, a flat list. It is not `data.beneficiaries` (investec_client.py:118). The endpoint is
  profile-wide and not per account.
- [REPO] The keys the code actually consumes are `beneficiaryId` (required; a row without it is skipped,
  beneficiaries.py:44-46), `beneficiaryName` with fallback to `name` (beneficiaries.py:47), `accountNumber` (used for
  last-3 digits only, beneficiaries.py:48,53), and `emailAddress` (lower-cased, beneficiaries.py:54).
  Test fixtures use the same keys (tests/test_payment_beneficiaries.py:49-54,64).

| field | status | meaning / matching use |
|---|---|---|
| beneficiaryId | REPO+DOC | Stable opaque id. Use it as the snapshot key and the match target. |
| beneficiaryName | REPO+DOC | The payee name as captured by the user. Primary name token. |
| name | REPO (fallback) / INF | Unclear whether this is a nickname or a duplicate of beneficiaryName. Treat it as a secondary name token. |
| accountNumber | REPO+DOC | Third-party bank account number (PII, see section 5). Only for exclusion and display. |
| referenceName | DOC/INF | Probably the "my reference" shown on the USER's statement. **Likely the strongest description signal.** |
| referenceAccountNumber | DOC/INF | Probably the "their reference" shown on the payee's statement. Usually NOT in our description. |
| code | DOC/INF | Branch / universal branch code. |
| bank | DOC | Bank name string. |
| lastPaymentAmount | DOC/INF | String amount. Mutable. |
| lastPaymentDate | DOC/INF | Date string. The format is unverified, so parse defensively. Mutable. |
| cellNo, emailAddress | DOC | Contact PII. Not needed for matching, so do not store them (minimisation). |
| categoryId | DOC/INF | Links to beneficiary categories (a separate endpoint, also INF). |
| profileId | DOC/INF | Investec profile. |
| fasterPaymentAllowed | DOC/INF | Boolean. |
| beneficiaryType | INF (low) | May be absent. Do not depend on it. |
| approvedBeneficiaryCategory | INF (low) | May be absent. Do not depend on it. |

- [DOC] Beneficiaries are only those the user created in online banking. The list is a **current-state snapshot**:
  entries get renamed, re-referenced, deleted or recreated with a new id. The API has no history.
- Implication: `lastPaymentAmount/Date` describe only the latest payment and change over time. They may corroborate a
  match but must not be the sole rule. Any such rule has to be pinned to a snapshot to stay deterministic.

---------------------------------------------------------------------------------------------------
## 2. Core concepts (unchanged; context for the matcher)
- **transaction_hash** = sha256(account_id|valueDate|actionDate|amount|description|day_seq) (db.py:127-144). `description`
  is a hash input, so **any mutation of `transactions.description` re-keys or duplicates rows**.
- **day_seq** is a within-day counter over identical group keys, in API order (db.py:98-124).
- **Dedup** works at three layers:
  - insert-level `on conflict (transaction_hash) do nothing` (db.py:227, 257);
  - destructive `0008_deduplicate.sql:18-35` (`DELETE ... rn>1`), which is **re-executed on every ingest** because
    `init_db` re-applies all migrations (db.py:78-81, ingest.py:80-81);
  - read-time `row_number()` dedup in report.py:45-49 and statements.py:96-100.
- **effective_date** = coalesce(transaction_date, action_date, value_date, posting_date) (0006_effective_date.sql:22).
- **Running balance**: the bank's `running_balance` is authoritative. Gaps are filled arithmetically and blanks are never
  fabricated (statements.py:350-361). Same-day rows are ordered by chaining balances (statements.py:330-348).
- **Flow classification** (`transactions_flow`, 0007_flow_account_number_match.sql:21-116): Tier A matches an own account
  number (verbatim or digit-normalised), Tier B an own holder name with length >6, Tier C a guarded equal-and-opposite
  leg. Results are `internal_transfer` / `external_inflow` / `external_outflow` plus `flow_signal`.
- **Categories** are assigned twice: at ingest (categorize.py:25-35) and at view time (`category_map`,
  0002_views.sql:75-89). EFTs fall into "Transfers" via the 'eft' / 'payment to' / 'transfer' keywords.
- **Groups** (groups.py) route reports and statements per recipient set. They have no beneficiary relevance.

---------------------------------------------------------------------------------------------------
## 3. Matching invariants (each MUST become a testable spec criterion)
- **M1 DEBIT-ONLY**: candidates are only rows with `upper(type)='DEBIT'` (equivalently `amount<0`, db.py:195-196).
  CREDIT rows always produce no match.
- **M2 PAYMENT-TYPES-ONLY**: candidates are only rows whose `transaction_type` is in an explicit, code-defined allowlist
  of payment/transfer types (see Q2). Card purchases, fees, debit orders and ATM rows never match. An unknown or NULL
  type is excluded (fail closed).
- **M3 NOT-INTERNAL**: exclude rows where `transactions_flow.flow_type='internal_transfer'`. Also never offer as a
  candidate any beneficiary whose digit-normalised `accountNumber` equals an own `accounts.account_number`: a user's own
  Investec account saved as a beneficiary is a transfer, not a third party. A user's own account at ANOTHER bank stays a
  legitimate beneficiary (external_outflow).
- **M4 DETERMINISTIC**: the output depends only on (transaction row, beneficiary snapshot). It must not depend on dict
  or set iteration order, DB row order, clock, or locale. Normalisation is pure and documented (casefold, collapse
  whitespace, optionally strip punctuation) and must be unit-tested. Rule precedence is a fixed ordered list, highest
  confidence first, the same pattern as Tier A/B/C.
- **M5 FAIL-CLOSED**: 0 candidates means no match. More than 1 candidate (distinct beneficiaryIds) under the
  highest-precedence rule that fired also means no match, recorded as "ambiguous" if a status is stored. Do not fall
  through to a weaker rule to break a tie. Mirrors resolve_beneficiary (beneficiaries.py:84-87). Minimum token length
  guards are required (analogous to `length(nm)>6`, 0005_flow.sql:47 and `length>=8` for numbers, 0005_flow.sql:23).
  No fuzzy or edit-distance matching.
- **M6 EXPLAINABLE**: every stored match records `match_rule` (for example `reference_exact`, `name_exact`,
  `reference_prefix`) and `matched_token`, the normalised beneficiary-side token that hit (a name or reference, never a
  full account number). This is the same idea as `flow_signal` (0007:74-98).
- **M7 NON-MUTATING**: never INSERT/UPDATE/DELETE on `transactions`, `accounts`, `balances`, `sync_runs`,
  `category_map`. Never `create or replace` any existing view. Never change `transaction_hash`, `day_seq`,
  `description`, `category`, or `raw`. Matching output lives only in NEW tables and views.
- **M8 IDEMPOTENT**: re-running with the same snapshot gives a byte-identical match set (upsert keyed on
  transaction_hash, or a full deterministic recompute). It must not create duplicate rows or flip-flop.
- **M9 SNAPSHOT SEMANTICS**: a sync takes a timestamped snapshot of the beneficiary list. A beneficiary renamed,
  deleted or recreated with a new id must NOT silently rewrite past explanations without trace. Recommended: store
  `synced_at`/`snapshot_id` on beneficiaries and `matched_at` + `snapshot_id` on matches. Either (a) keep past matches
  and only match unmatched rows, or (b) recompute everything against the latest snapshot. The spec must pick one; **Q4**.
  Deleted beneficiaries are kept, marked (`last_seen_at` / `active=false`), and not hard-deleted, so old matches still
  resolve a name.
- **M10 ORPHAN-SAFE**: match rows reference `transaction_hash` **without an FK**, or with ON UPDATE/DELETE CASCADE.
  `backfill-hashes` UPDATEs transaction_hash (db.py:385-389) and 0008 DELETEs rows on every ingest (0008:18-35). A plain
  FK would make ingest or backfill fail. The read view must inner-join `transactions` so orphans are invisible, and
  should apply the same row_number dedup as report.py:45-49 if it is meant to line up with reports.
- **M11 OFF-BY-DEFAULT**: a new Settings bool, default False, read through `_opt_bool` (config.py:57-62; pattern at
  config.py:112,207). When off there are zero API calls to `/beneficiaries`, zero writes, and zero extra stdout.
- **M12 API-FAILURE-ISOLATION**: a failed beneficiaries fetch must not fail ingest, alter `sync_runs` status, or wipe the
  existing snapshot. Fail closed means keep the old snapshot and produce no new matches. This mirrors the non-fatal
  balance fetch at ingest.py:131-137, and also follows the ingest.py:3-11 rule that no DB transaction is open during API
  calls.

---------------------------------------------------------------------------------------------------
## 4. Observable-output contracts (byte-identical unless the spec says otherwise)
- **Ingest stdout**: `print(f"Ingest: {summary}")` (cli.py:43), where summary has keys in order from_date, to_date,
  accounts, transactions_upserted, balances_captured, new_accounts (ingest.py:190-197). **Adding a key changes stdout.**
  New-account alert prints are at cli.py:62-67 and 91.
- **sync_runs** columns and status semantics (db.py:316-347). `transactions_upserted` counts only new transaction rows.
- **Report workbook** (report.py):
  - COLUMNS (report.py:20-21);
  - sheet order Summary, By Category, By Account, Top Merchants, Daily Trend, Internal Transfers, Transactions,
    + Monthly Movement, Reconciliation (report.py:183-191, 264-268);
  - Summary metric labels (report.py:142-145);
  - empty-sheet placeholder "No data for this period" (report.py:199);
  - MONEY_FORMAT on every numeric non-bool cell (report.py:210-213, __init__.py MONEY_FORMAT);
  - filename `spend-analysis_{start}_to_{end}{_label}.xlsx` (report.py:272);
  - info dict (report.py:275).
  The report reads an explicit column list from `transactions_flow` (report.py:39-59). New views do not affect it, but
  **editing transactions_flow would**.
- **Statement workbook** (statements.py):
  - STATEMENT_COLUMNS = Date, Description, Category, Debit, Credit, Balance (statements.py:34);
  - 14-row meta header with fixed labels (statements.py:453, 463-478);
  - reconciliation text and colours (statements.py:444-449, 487-493);
  - column widths (statements.py:511);
  - newest-first display (statements.py:403-404);
  - filename `statement_{acct}_{start}_to_{end}.xlsx` (statements.py:620-623);
  - the "Description" column is the stripped raw description (statements.py:373). **Do not replace it with a
    beneficiary name.**
- **Emails**: bodies at cli.py:96-117 and subjects at cli.py:170, 225-228. `_money` formatting is at cli.py:192-195.
- **CLI help baselines** (.loop/baseline/): sub-command help for ingest, report, statements, init-db, backup and
  backfill-hashes matches current code byte-for-byte (verified this run). **`help__top.txt` is STALE**: it lacks
  `approve-payments` and `pay-selftest`, which exist in cli.py:446-472. Comparing against it fails today, before any
  change (see F2).
- **Schema/views**: all existing tables and the views transactions_normalized, transactions_categorized,
  spend_by_category, monthly_movement, balance_reconciliation, transactions_flow and monthly_flows stay unchanged.
  Migrations 0001-0011 are not edited.

---------------------------------------------------------------------------------------------------
## 5. PII / security
- `accountNumber` of a beneficiary is **third-party PII** (POPIA s9-11, s14). `cellNo` and `emailAddress` are also PII.
- Repo conventions:
  - payment tables store **last-3 only, no full account number / PAN** (0009_payment_approvals.sql:6-7,
    beneficiaries.py:24-25, 53);
  - the archive says "never log full account numbers";
  - L-0003 says build stored records **field by field from an allowlist**, never by scrubbing `raw`.
  - Own-account numbers ARE stored in full (0001_init.sql:6), but that is the user's own data.
- RLS: every new table needs `enable row level security` plus a `deny_all` policy **in the new migration**, using the
  0011 pattern (0011_deny_all_policies.sql:19-38). 0011's target list is hardcoded, so do not edit it; create a new
  migration that repeats the pattern. Every new view needs `security_invoker = on` (0009_security_invoker_rls.sql:26-32).
- Backups: `pg_dump` dumps the whole DB (backup.py:43) and is encrypted only if BACKUP_PASSPHRASE is set. Whatever we
  store lands in backup artifacts.
- **Recommended stance**:
  - store `beneficiary_id`, `beneficiary_name`, `bank`, `code` (branch), `reference_name` and `account_last3` only;
  - do NOT store the full `accountNumber`, `cellNo`, `emailAddress`, `referenceAccountNumber`, or a raw JSON blob;
  - display the account number as masked `****709`;
  - if equality on the full number is needed (for M3 own-account exclusion), compute it in memory at sync time and
    persist only a boolean `is_own_account`, or a keyed HMAC if it must persist. Never store a plain unsalted sha256,
    because a ~10-11 digit space is brute-forceable;
  - never log beneficiary account numbers.
  The goal text asks to show the "account number", which conflicts with this stance. **Open question Q1 for the human.**

---------------------------------------------------------------------------------------------------
## 6. Hidden coupling / footguns, ranked by danger
1. **FK to transactions.transaction_hash** would break ingest: 0008 re-runs its DELETE on every `init_db`
   (ingest.py:80-81 → db.py:80-81 → 0008:18-35), and backfill-hashes UPDATEs hashes (db.py:385-389). Use no FK (M10).
2. **Stale help baseline**: .loop/baseline/help__top.txt omits approve-payments/pay-selftest. If the tester compares
   against it, the gate is red before any change. If someone "fixes" it by regenerating after adding a subcommand, a CLI
   change slips through. The orchestrator must decide on a re-baseline from HEAD (or git-stash) before building.
3. **Touching description, hash or day_seq**, for example normalising description in place, or adding a column to
   `_TX_COLUMNS` (db.py:185-189, 16-param batch math at db.py:226, 248). Any change re-keys rows and creates duplicates.
4. **`create or replace view transactions_flow`, or any existing view**: report.py and statements.py SQL depend on it.
   Postgres only allows appending columns, and even that changes `f.*` consumers. New logic goes in a NEW view.
5. **Ingest summary/print**: adding a `beneficiaries_*` key to the summary dict, or a print, changes `Ingest: {...}`
   stdout (cli.py:43). Gate any added output behind the setting, or put it in a separate surface (Q3).
6. **Migration idempotency**: every migration re-runs on every ingest in ONE transaction with lock_timeout 15s
   (db.py:78-81). The new migration (`0012_*.sql`) must be fully re-runnable: `create table if not exists`,
   `create or replace view` (on new views only), `drop policy if exists` → `create policy`, and no seed INSERT without
   `where not exists`. A non-idempotent statement breaks every future ingest. Sort is lexicographic, and the two `0009_*`
   files already coexist, so pick a unique `0012_` prefix.
7. **API call during an open DB transaction** violates the resilience model (ingest.py:3-11). Fetch beneficiaries first,
   then write in a short transaction.
8. **Settings dataclass is frozen** (config.py:65). A new field must have a default, sit after the existing defaulted
   fields, and be loaded in `Settings.load` (config.py:177-216). Tests build a `_Settings()` stub
   (tests/test_payment_notify.py), so use `getattr(settings, "...", False)` like cli.py:300.
9. **Coupling to payments/beneficiaries.py**: `from_api`/`resolve_beneficiary` semantics are covered by
   tests/test_payment_beneficiaries.py and used by the pipeline (pipeline.py:85-88, 178) and selftest (selftest.py:50-60).
   Reuse read-only. Do not change their exact-match/substring behaviour to suit matching.
10. **Truncated/uppercased description and prefix rules**: prefix matching of a short description against long
    references is the main false-positive risk. Require a minimum token length and fail closed on >1 hit.
11. **Tier C false internal_transfer** (0007:57-68): a genuine third-party EFT whose amount coincidentally equals an
    opposite leg on another own account the same day is excluded by M3. This is acceptable because it fails closed, but
    document it.
12. **categorize "Transfers" vs view category**: these can differ (ingest-time vs category_map). Don't use `category`
    as the payment-type gate. Use `transaction_type` (M2).

---------------------------------------------------------------------------------------------------
## 7. Behavioural test gaps (untested = higher risk = leave alone)
- No test for the real Investec `transactionType` vocabulary or real EFT description shapes. All fixtures are
  synthetic. The new matcher needs its own golden fixtures: uppercase, truncated, prefixed, ambiguous, renamed and
  deleted cases.
- `investec_client.get_beneficiaries` response-shape handling (`data` list vs dict) is exercised only via mocks in the
  payment tests. Its handling of a non-list `data` is untested.
- No DB-level tests: migrations, views (`transactions_flow`, row_number dedup SQL), RLS and 0008 re-run are untested
  against Postgres. The migration-order check is the only guard, so keep the new migration minimal.
- `run_ingest` end-to-end stdout is untested (only chunking, tests/test_ingest_chunks.py). If ingest is touched, add a
  test asserting summary keys are unchanged when the feature is off.
- Help-text byte equality has only the baseline files (see footgun 2). There is no pytest asserting it.

---------------------------------------------------------------------------------------------------
## 8. Open questions for the human
- **Q1 (PII)**: store last-3 only (recommended) or the full beneficiary account number? Show it masked (`****709`) or in
  full in the view?
- **Q2 (types)**: which `transaction_type` values are payments/transfers? Suggested: run
  `select transaction_type, count(*) from transactions where type='DEBIT' group by 1` on prod and freeze the allowlist
  in code.
- **Q3 (surface)**: trigger sync+match inside `ingest` (gated, no stdout change) or via a NEW subcommand (changes
  top-level help, which needs explicit spec allowance plus a re-baseline)?
- **Q4 (snapshot)**: are past matches frozen at match time, or recomputed against the latest beneficiary snapshot?
- **Q5**: should `referenceName` (my-reference) rank above `beneficiaryName`? This is recommended, but whether
  referenceName is really the my-reference needs confirming against one real payload.

confidence: high on repo invariants and footguns (cited); medium on the PII stance; low on Investec field semantics
marked [INF] (referenceName vs referenceAccountNumber, transactionType vocabulary, description shape).
