# invespend — Domain Invariants (refactor-preservation contract)

Scope: protect financial numbers, dedup decisions, dates, ordering, and report
formatting from "harmless" refactors. Cite `file:line` for every claim.

---

## 1. Core domain concepts

- **Transaction** — one bank line item. Persisted in `transactions`
  (`db/migrations/0001_init.sql:16-34`). `amount` is **signed**: debits negative
  (`db/migrations/0001_init.sql:29`; sign applied in
  `src/invespend/db.py:194-198`). `raw` jsonb keeps the full payload, never lost
  (`0001_init.sql:32`).
- **Account** — `accounts` table is the authoritative list of *owned* accounts;
  anything not in it is external (`0005_flow.sql:3-8`).
- **day_seq** — within-day sequence counter (0-based) over otherwise-identical
  same-day transactions, in API order (`src/invespend/db.py:109-124`). Folded into
  the hash so two identical same-day items (e.g. two R45 coffees) stay distinct
  (`db.py:127-144`, `tests/test_dedup.py:13-21`). Column added in
  `0003_day_seq.sql:5`.
- **transaction_hash** — deterministic dedup primary key (the API has no stable
  id): `sha256(account_id | valueDate | actionDate | amount | description | day_seq)`
  (`src/invespend/db.py:127-144`). Date fields use `or ""` so JSON null and a
  missing key hash identically (`db.py:138-142`, `tests/test_dedup.py:39-45`).
- **effective_date / economic date** — `coalesce(transaction_date, action_date,
  value_date, posting_date)` (`0006_effective_date.sql:22`). Supersedes the
  posting-date-first definition in `0002_views.sql:61`. ALL windowing,
  bucketing (`month`), and statement/report ordering use this, NOT posting_date,
  because posting_date can lag by months (`0006_effective_date.sql:1-12`,
  `src/invespend/report.py:24-33`). Note: `report.py` / `statements.py` read
  `effective_date` from the `transactions_flow` view, but the raw-table dedup in
  `0008_deduplicate.sql:28` recomputes its own coalesce inline.
- **running_balance** — bank-reported balance AFTER each transaction; authoritative
  when present, used as-is (`statements.py:351-359`). Drives intra-day chain
  ordering (`statements.py:175-285`).
- **Flow classification** — `transactions_flow` view labels each row
  `internal_transfer` / `external_inflow` / `external_outflow`
  (`0007_flow_account_number_match.sql:21-116`, supersedes `0005_flow.sql:19`).
  Three tiers, highest-confidence first: Tier A own-account-number in description
  (verbatim OR digit-normalised, `0007:39-47`); Tier B own-holder-name with
  `length(nm)>6` guard (`0007:51-55`); Tier C equal-and-opposite leg on a
  different owned account, same `value_date`, with fee/charge/reversal/refund
  excluded on both legs (`0007:57-68`). Internal transfers are excluded from all
  spend/income aggregates so they aren't double-counted (`0005_flow.sql:108-117`).
- **Category** — rule engine, first/lowest-priority match wins. Two parallel
  implementations that MUST stay in sync: Python `categorize()`
  (`src/invespend/categorize.py:9-35`, used at ingest) and the SQL `category_map`
  + `transactions_categorized` view (`0002_views.sql:11-89`, re-derives on read).
  Order-sensitive: **Fees before Income** because "FeesAndInterest" contains
  "interest" (`categorize.py:16-18`, `0002_views.sql:39-41`). Fallback `Other`.
- **Group** — report routing config in `src/invespend/groups.py:36-47`; matches by
  exact account_number or case-insensitive name substring, first match wins
  (`groups.py:25-28`). `EXCLUDE_ACCOUNTS` drops accounts from ALL output
  (`groups.py:31-34`).

---

## 2. Invariants that MUST hold after refactor

1. **Hash formula is frozen.** Exact field set, order, `|` separator, `or ""`
   null-coalescing, and `str()` coercion in `db.py:136-144`. Any change re-keys
   every row and breaks rolling-window dedup idempotency. `assign_day_seq`
   counter must increment in API order (`db.py:117-123`). Backfill recomputes via
   the SAME functions to stay byte-identical (`db.py:380-392`,
   `tests/test_backfill.py:74-81`).
2. **Sign convention.** `type == "DEBIT"` → `-abs(amount)`, else `+abs(amount)`
   (`db.py:194-198`). Downstream spend = `amount < 0`, income = `amount > 0`
   everywhere (`report.py:136-138`, `statements.py:393-398`,
   `0005_flow.sql:97-98`).
3. **effective_date precedence** = `transaction_date → action_date → value_date →
   posting_date` (`0006_effective_date.sql:22,28`). Must match across the view,
   the dedup migration's inline coalesce (`0008_deduplicate.sql:28`), and the
   reconciliation view which still keys on `posting_date`
   (`0004_balances.sql:38-39`) — do not silently unify these.
4. **Read-time dedup (ROW_NUMBER) is identical in 3 places** and must stay so:
   partition `(account_id, amount, description, running_balance, effective_date)`,
   `order by ingested_at desc`, keep `rn = 1` — `report.py:45-49`,
   `statements.py:96-100`, `statements.py:137-140`. The base-table dedup migration
   uses the SAME partition (`0008_deduplicate.sql:21-29`) but restricted to
   `running_balance is not null` (`0008:32`).
5. **Intra-day ordering is by running-balance chain, NOT day_seq.** Greedy
   min-distance chain where each row's `(running_balance − amount)` matches the
   previous row's `running_balance` (`statements.py:175-285`). day_seq is only a
   fallback / tie-break (`statements.py:327-329`). Orphan-detection anchor when no
   opening (`statements.py:228-233`); NULL-rb rows placed arithmetically with a
   `< 0.015` tolerance (`statements.py:267`). This is heavily relied upon — see
   `tests/test_statements.py:242-354`.
6. **Balance carry-forward.** Use bank `running_balance` as-is when present (round
   2dp); else `prev + amount` (round 2dp); else **leave blank — never fabricate
   from 0** (`statements.py:351-359`, `tests/test_statements.py:127-139`).
   Opening inferred by stepping back off first row only when it has a balance
   (`statements.py:384-385`). Closing = last forward balance BEFORE display
   reversal (`statements.py:399`).
7. **Display order.** Statement: balances computed forward (oldest→newest), then
   reversed for newest-first display (`statements.py:402-404`). Report
   `Transactions`/`Internal Transfers` sheets sorted by `effective_date` ascending
   (`report.py:180-181`).
8. **Money rounding.** All money aggregates `round(x, 2)` and cast to `float`
   (`report.py:147-153`, `statements.py:395-399`). Reconciliation tolerance
   `abs(diff) < 0.01` (`statements.py:603`, `0004_balances.sql:49`); chain
   tolerance `< 0.015` (`statements.py:267`).
9. **MONEY_FORMAT string is a contract.** Exact Accounting format in
   `src/invespend/__init__.py:10`; applied to every numeric (non-bool) cell
   (`report.py:210-213`, `statements.py:483-484,514-519`). `_money()` CLI helper
   mirrors it as space-grouped thousands (`cli.py:177-180`).
10. **Internal transfers excluded from spend/income** in both report
    (`report.py:129-138`) and statement (`statements.py:389-398`), reported on
    their own sheet/totals. Aggregation views also exclude them
    (`0005_flow.sql:116`).
11. **Idempotent upserts.** `on conflict (transaction_hash) do nothing` and
    `cur.rowcount` counts only new rows (`db.py:227-260`). Balance: one row per
    account per day, `on conflict (account_id, as_of_date) do update`
    (`db.py:293-301`, `0004_balances.sql:20`).
12. **Categorizer order sensitivity** (Fees before Income) in both engines
    (`categorize.py:16-18`, `0002_views.sql:39-41`).

---

## 3. Observable outputs (behavioural contract)

### Weekly Excel report (`report.py`)
- Sheet set/order: **Summary, By Category, By Account, Top Merchants, Daily Trend,
  Internal Transfers, Transactions** (`report.py:183-191`), then **Monthly
  Movement** and **Reconciliation** appended if their views resolve
  (`report.py:263-270`). Empty-input set omits the last two
  (`report.py:122-125`).
- **Summary** rows, in order: Total spend, Total income, Net, Transactions,
  Categories, Internal transfers (excluded), Internal transfer volume
  (`report.py:140-156`). Columns `Metric`, `Value`.
- **By Category**: cols `category, total_spend, transactions`, sorted by
  total_spend desc (`report.py:158-163`). **By Account**: `account_number,
  account_name, total_spend` desc — uses account_NUMBER not id
  (`report.py:165-168`, `tests/test_report.py:79-84`). **Top Merchants**: top 15
  by spend (`report.py:170-173`). **Daily Trend**: `date, total_spend`
  (`report.py:175-178`).
- Empty sheet → single-cell `{"info": ["No data for this period"]}`
  (`report.py:199`). Sheet names truncated to 31 chars (`report.py:200`).
  Bold header row 1; numeric (non-bool) cells get MONEY_FORMAT; col widths
  `min(maxlen+2, 50)` (`report.py:203-213`).
- Filename: `spend-analysis_{start}_to_{end}{_label}.xlsx`; window is 7 days
  ending `end` (start = end−6) (`report.py:235-236,271-273`).

### Account statements (`statements.py`)
- One `.xlsx` per account; accounts with no transactions are skipped
  (`statements.py:578-581,549`). Columns fixed: **Date, Description, Category,
  Debit, Credit, Balance** (`statements.py:34`). Debit = `−amount` when amount<0;
  Credit = amount when >0; the other is None (`statements.py:375-376`).
- 14-row header block above the table at `startrow=14` (`statements.py:453,459`):
  Account statement / name / number / Period / Currency / Opening balance / Money
  in (excl. transfers) / Money out (excl. transfers) / Transfers in / Transfers
  out / Closing balance / Investec balance (as of …) / Statement vs Investec /
  Reconciled (`statements.py:463-478`). Row 14 = Reconciled, colour-coded green
  (`FFC6EFCE`) / red (`FFFFC7CE`) / N/A (`statements.py:486-493`,
  `tests/test_statements.py:201-239`).
- Reconciliation text: `RECONCILED` | `DIFFERENCE {+/-,.2f}` | `N/A — no balance
  snapshot` (`statements.py:444-449`). `reconciled` true iff `abs(closing −
  investec) < 0.01` (`statements.py:601-603`).
- Table header fill `DDEBF7`, frozen panes at first data row, date format
  `yyyy-mm-dd`, fixed col widths `{1:12,2:42,3:16,4:14,5:14,6:16}`
  (`statements.py:499-522`). Filename
  `statement_{safe_number}_{start}_to_{end}.xlsx` (`statements.py:620-623`);
  default span = full history (`statements.py:552-553`).

### Emails / summaries (`cli.py`, `account_alert.py`)
- Report email subject `Weekly spend analysis: {start} to {end}`, body
  `_report_body` (`cli.py:100-105,155`). Statements email subject
  `Account statements ({span}) as at {end} — N account(s)`, body lists each
  account's txn count + closing balance via `_money()` (space-grouped)
  (`cli.py:108-121,212-216`).
- New-account alert: subject/body shape in `account_alert.py:76-138`; per-account
  block fields and UNCLAIMED/claimed status + suggested group
  (`account_alert.py:92-112`, tests `tests/test_account_alert.py`).
- Per-group fan-out: each GROUP gets its own report/statement, plus a "default"
  bucket excluding all grouped accounts (`cli.py:160-174,221-234`).

---

## 4. Hidden coupling / footguns (ranked by danger)

1. **The 3-way duplicated ROW_NUMBER dedup SQL** (`report.py:45-49`,
   `statements.py:96-100`, `statements.py:137-140`) + the base-table dedup
   (`0008_deduplicate.sql:21-32`). A refactor that "DRYs" one partition/order
   clause and diverges another silently changes which duplicate row survives →
   different amounts/balances. No test exercises the SQL (pure-Python tests only).
   **Highest danger.**
2. **Intra-day chain-sort algorithm** (`statements.py:175-285`). Subtle: greedy
   min-distance, orphan anchor, NULL-rb arithmetic placement, `0.015` tolerance,
   `anchor_is_day_opening` degenerate-gap skip (`statements.py:250-285`). Any
   reordering or tolerance tweak changes printed balances/order. Well-tested for
   the cases present (`test_statements.py:242-354`) but easy to break on
   uncovered shapes (ties, circular amounts).
3. **Two categorizers that must agree** (`categorize.py` vs `0002_views.sql`
   `category_map`). Ingest writes Python's result to `transactions.category`, but
   reports read the SQL view's re-derived category — they can drift. Fees-before-
   Income ordering is load-bearing in both.
4. **effective_date defined in 3 spots** (view `0006:22`, dedup migration
   inline `0008:28`, reconciliation still on posting_date `0004:38`). Unifying
   them "for consistency" would change dedup grouping or reconciliation pairing.
5. **Flow classifier Tier C self-join** depends on `value_date` equality and exact
   `amount = -c.amount`, with fee/charge/reversal/refund regex exclusion on BOTH
   legs (`0007:57-68`). Changing the regex, the date field, or the sign test
   re-labels transfers ↔ spend, moving headline spend numbers. Untested in code.
6. **Sign + abs logic** (`db.py:194-198`): refactoring to "simplify" the
   debit/credit branch could flip a sign for non-DEBIT/CREDIT `type` values.
7. **MONEY_FORMAT** literal and the bool-exclusion guard (`not isinstance(cell,
   bool)`) — a refactor formatting booleans as numbers would render the
   reconciliation flag as `1.00` (`report.py:212`, comment `report.py:208-209`).
8. **`is_backfill` early-stop** (`ingest.py:144-169`): the `(to_date−from_date).days
   > chunk_days and not full` condition and `MAX_EMPTY_CHUNKS=3` streak govern how
   much history is pulled; altering it changes what data exists to report on.

---

## 5. Behavioural test gaps (higher-risk / leave alone)

- **All SQL views and migrations are untested** — flow classification (all 3
  tiers, digit-normalisation), `transactions_categorized`, `spend_by_category`,
  `monthly_movement`, `balance_reconciliation`, and the `0008` dedup DELETE. Tests
  are pure-Python only. Treat every `.sql` file as fragile.
- **Read-time ROW_NUMBER dedup** in `report.load_transactions` /
  `statements.load_*` is never exercised against a DB.
- **Report filtering** (include/exclude patterns & account numbers,
  exclude-before-include) in `report.py:241-258` and `statements.py:563-576` is
  untested.
- **`generate_weekly_report` / `generate_account_statements` orchestration**
  (filename, sheet appending, recon attachment) — only the pure builders
  (`build_spend_summary`, `build_account_statement`) and `write_*` are tested.
- **Categorizer / category_map drift**: no test asserts the two engines agree.
- **Reconciliation view pairing** (`0004_balances.sql`) and its posting_date keying
  are untested.
- **Ingest run loop** (`run_ingest`) — only `_date_chunks` and `extract_balance_fields`
  are unit-tested; resume/backfill/empty-streak paths are not.
