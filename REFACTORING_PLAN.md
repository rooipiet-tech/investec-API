# invespend: beneficiary matching (run `beneficiary-matching`), plan for iteration 1

Spec: `.loop/spec.json` v1.0.0 (FROZEN). Inputs: `.loop/GOAL.md`, `.loop/research.md`, `.loop/domain.md`.
Base commit: the HEAD at the start of the build, currently `acb973d`. S0 records the exact SHA.
Test floor: 211 passed, 0 failed, 0 errors, measured with `.venv/bin/python -m pytest -q`.
Previous run's plan: archived to `.loop/archive/refactor-plan-prev.md`.

## Binding resolutions (from spec `open_questions[].resolution`)

| Q | Resolution | Effect on this plan |
|---|---|---|
| Q1 | Store and show the FULL beneficiary account number, in the new table only. Protected by deny_all RLS and never logged. It is not used in `matched_token`. No cellNo, email or raw. | The `beneficiaries.account_number` column is the full value. `matched_token` for the account rule is masked as `***` + last 3 digits. |
| Q2 | Provisional allowlist is `OnlineBankingPayments`, `OnlineBankingTransfers` and `FasterPay`. NULL or unknown types fail closed. | Single constant `PAYMENT_TRANSACTION_TYPES`. |
| Q3 | Gated hook inside `ingest`. No new subcommand. Help stays byte-identical. | `cli.py` is not touched. |
| Q4 | Freeze `matched` rows. Re-evaluate only unmatched, ambiguous and new rows. Soft-retire beneficiaries. | Freeze is enforced twice: in the Python candidate query and in the SQL upsert `WHERE` clause. |
| Q5 | Precedence: account digits (>=8) > referenceName exact > beneficiaryName exact > name exact > guarded prefix (>6). | `RULES` tuple, highest first. |
| Q6 | No Excel change. F10 is strict. | `report.py` and `statements.py` get zero diff. |

## Staged change set

Order is S0 → S7. Each stage leaves the suite green.

| ID | Change | Files | Risk | Criteria advanced |
|---|---|---|---|---|
| S0 | Refresh the help baseline from the unmodified base commit and record the SHA. | `.loop/baseline/help__top.txt` (refresh), `help_approve-payments.txt`, `help_pay-selftest.txt` (new), other 6 re-captured, `.loop/baseline/BASE_SHA` | LOW | F1 (and F3 oracle) |
| S1 | Additive migration with 2 tables and 1 view, plus RLS deny_all and security_invoker. | `db/migrations/0012_beneficiary_matching.sql` (new) | MEDIUM (it runs on every ingest inside `init_db`) | F4 F5 F6 F7 F18 F21 F25 F29 F32 |
| S2 | Pure matcher module: normalisation, allowlist, rule precedence, fail-closed. | `src/invespend/beneficiary_match.py` (new) | LOW | F12 F13 F14 F15 F16 F18 F20 F26 F27 F28 |
| S3 | Append-only DB helpers for snapshot upsert/retire, candidate load and match upsert. | `src/invespend/db.py` (new functions appended at end only) | LOW-MEDIUM | F7 F8 F17 F25 F32 |
| S4 | Config flag `BENEFICIARY_MATCHING_ENABLED`, default False. | `src/invespend/config.py` (one field + one `load()` line) | LOW | F11 |
| S5 | Orchestrator (API, then DB) plus a gated, non-fatal ingest hook that only logs. | `src/invespend/beneficiary_sync.py` (new), `src/invespend/ingest.py` (+~7 lines) | MEDIUM | F9 F11 F19 F20 F17 |
| S6 | Tests in NEW files only, with golden fixtures that use fake data. | `tests/test_beneficiary_match.py`, `tests/test_beneficiary_sync.py`, `tests/test_beneficiary_migration.py`, `tests/test_beneficiary_ingest_freeze.py`, `tests/fixtures/beneficiary/*.json` (all new) | LOW | F2 F5-F21 F25-F28 F30 F32 |
| S7 | Documentation. | `.env.example` (append 3 lines), `README.md` (new section) | LOW | F28 F31 |

Aggregate risk: **MEDIUM**. Every change is additive. The MEDIUM items are S1 (the SQL re-runs on every ingest) and S5 (it touches the ingest control flow). Both are contained by idempotent guards, the default-off flag and `try/except`.

Untouched, with zero diff: `cli.py`, `report.py`, `statements.py`, `categorize.py`, `investec_client.py`, `src/invespend/payments/**`, `db/migrations/0001-0011`, `db/roles.sql`, every existing test file, `pyproject.toml`.

---

## S0: refresh the baseline (precondition; must be the first commit before any source diff)

- From the clean base tree, run `.venv/bin/invespend --help > .loop/baseline/help__top.txt`.
- For each `sub` in `init-db ingest report statements backup backfill-hashes approve-payments pay-selftest`, run `invespend $sub --help > .loop/baseline/help_$sub.txt`.
- Write `git rev-parse HEAD` to `.loop/baseline/BASE_SHA`.
- Commit this alone, before S1. Verify by running `git stash; regenerate; diff` and checking the output is empty.

## S1: `db/migrations/0012_beneficiary_matching.sql`

The file uses a unique `0012_` prefix, so it sorts after `0011_deny_all_policies.sql`. It contains only DDL on new objects. It has no INSERT/UPDATE/DELETE, no index on existing tables, and no ALTER, DROP or REPLACE of existing objects.

```sql
create table if not exists beneficiaries (
    beneficiary_id    text primary key,          -- Investec beneficiaryId
    beneficiary_name  text,
    name              text,
    reference_name    text,
    bank              text,
    branch_code       text,                      -- API field `code`
    account_number    text,                      -- FULL number (Q1); never logged
    first_seen_at     timestamptz not null default now(),
    last_seen_at      timestamptz not null,      -- = synced_at of the last fetch that contained it
    active            boolean not null default true
);

create table if not exists beneficiary_matches (
    transaction_hash  text primary key,          -- NO FK (F7): 0008 deletes / backfill-hashes rewrites
    status            text not null check (status in ('matched','ambiguous','no_candidate')),
    beneficiary_id    text,                      -- set iff status='matched' (no FK; beneficiaries never hard-deleted)
    match_rule        text not null,             -- rule id, or 'none' for no_candidate
    matched_token     text not null,             -- normalised beneficiary-side token ('' for no_candidate); never a full acct no.
    candidate_count   integer not null,
    snapshot_at       timestamptz not null,      -- synced_at of the beneficiary snapshot used
    matched_at        timestamptz not null default now(),
    constraint beneficiary_matches_matched_has_id
        check ((status = 'matched') = (beneficiary_id is not null))
);

-- RLS: 0011 pattern, repeated for the new tables only (0011's list is NOT edited).
alter table beneficiaries enable row level security;
drop policy if exists deny_all on beneficiaries;
create policy deny_all on beneficiaries for all to public using (false) with check (false);
alter table beneficiary_matches enable row level security;
drop policy if exists deny_all on beneficiary_matches;
create policy deny_all on beneficiary_matches for all to public using (false) with check (false);

create or replace view transactions_beneficiary with (security_invoker = on) as
select
    f.transaction_hash, f.account_id, f.effective_date, f.description, f.amount,
    f.type, f.transaction_type, f.flow_type,
    m.status            as match_status,
    m.beneficiary_id,
    b.beneficiary_name,
    b.name              as beneficiary_alt_name,
    b.reference_name    as beneficiary_reference,
    b.bank              as beneficiary_bank,
    b.branch_code       as beneficiary_branch_code,
    b.account_number    as beneficiary_account_number,
    b.active            as beneficiary_active,
    m.match_rule, m.matched_token, m.candidate_count, m.snapshot_at, m.matched_at
from beneficiary_matches m
join transactions_flow f on f.transaction_hash = m.transaction_hash   -- inner join: orphans invisible (F7)
left join beneficiaries b on b.beneficiary_id = m.beneficiary_id
where f.flow_type is distinct from 'internal_transfer'                -- F18
  and not exists (                                                    -- F18 defence in depth
      select 1 from accounts a
      where length(regexp_replace(coalesce(a.account_number,''), '[^0-9]', '', 'g')) >= 8
        and regexp_replace(a.account_number, '[^0-9]', '', 'g')
          = regexp_replace(coalesce(b.account_number,''), '[^0-9]', '', 'g'));
```

How the view joins:
- `beneficiary_matches` is inner-joined to `transactions_flow`, which is built from `transactions`. A match whose hash was deleted by 0008 or rewritten by `backfill-hashes` therefore never surfaces (F7).
- `beneficiaries` is left-joined, so `ambiguous` and `no_candidate` rows remain queryable through `match_status` (F32).
- A soft-retired beneficiary still resolves its name and number (F25).
- The view sits beside `transactions_flow` and does not replace it (F21).

Idempotency (F5): `if not exists`, drop-then-create policy, and `create or replace` on a NEW view only. Re-running with an identical column list is a no-op. No earlier migration drops `transactions_flow` (verified by grep: no `drop view`/`cascade` in 0001-0011), so the dependent view never blocks re-runs of 0007's `create or replace view transactions_flow`, which keeps the same columns.

## S2: `src/invespend/beneficiary_match.py` (pure; stdlib only: `re`, `unicodedata`, `dataclasses`, `decimal`)

The module docstring documents the rule list, the precedence, the guards and the fail-closed behaviour (F26). The module has no `datetime.now`, `random`, `difflib`, logging of values, or I/O.

```python
PAYMENT_TRANSACTION_TYPES: frozenset[str] = frozenset(
    {"OnlineBankingPayments", "OnlineBankingTransfers", "FasterPay"})   # Q2 provisional
RULES: tuple[str, ...] = ("account_number", "reference_exact",
                          "beneficiary_name_exact", "name_exact", "name_prefix")  # Q5 order
MIN_ACCOUNT_DIGITS = 8      # account rule guard (0007 precedent)
MIN_EXACT_LEN = 5           # normalised token length for *_exact ("Mom","Rent" excluded)
MIN_PREFIX_LEN = 7          # >6, prefix rule guard (0005/0007 precedent)
DESCRIPTION_PREFIXES: tuple[str, ...] = ("immediate payment to", "payshap to",
                                         "payment to", "transfer to", "eft to")  # longest first
SNAPSHOT_FIELDS: tuple[tuple[str, str], ...] = (            # api key -> column (PII allowlist, F20)
    ("beneficiaryId", "beneficiary_id"), ("beneficiaryName", "beneficiary_name"),
    ("name", "name"), ("referenceName", "reference_name"), ("bank", "bank"),
    ("code", "branch_code"), ("accountNumber", "account_number"))

@dataclass(frozen=True)
class BeneficiaryRecord:
    beneficiary_id: str; beneficiary_name: str | None; name: str | None
    reference_name: str | None; bank: str | None; branch_code: str | None
    account_number: str | None
    def __repr__(self) -> str: ...      # masks account_number (no digits in logs/reprs)

@dataclass(frozen=True)
class TxCandidate:
    transaction_hash: str; type: str | None; transaction_type: str | None
    description: str | None; amount: Decimal | float | None; flow_type: str | None

@dataclass(frozen=True)
class MatchResult:
    transaction_hash: str; status: str        # 'matched'|'ambiguous'|'no_candidate'
    beneficiary_id: str | None; match_rule: str; matched_token: str; candidate_count: int

def normalise(text: str | None) -> str
    # NFKC -> casefold -> every non-alnum char -> ' ' -> collapse whitespace -> strip
def digits_only(text: str | None) -> str
def mask_account(number: str | None) -> str            # '***' + last 3 digits, '' if none
def description_variants(description: str | None) -> tuple[str, ...]
    # (norm, norm minus one leading DESCRIPTION_PREFIX, that minus digit-only tokens), deduped, order fixed
def parse_beneficiaries(data: object) -> list[BeneficiaryRecord] | None
    # None if data is not a list (dict/None/str -> treated as fetch failure, F19/F28);
    # skips non-dict items and items with falsy beneficiaryId; builds field-by-field from
    # SNAPSHOT_FIELDS only (cellNo/emailAddress/referenceAccountNumber never read); str()+strip;
    # returns sorted by beneficiary_id; de-dupes on beneficiary_id (first by sort wins, deterministic)
def eligible_beneficiaries(benes: Iterable[BeneficiaryRecord],
                           own_account_numbers: Iterable[str | None]) -> tuple[BeneficiaryRecord, ...]
    # drops any bene whose digits_only(account_number) (len>=MIN_ACCOUNT_DIGITS) equals
    # digits_only(own) for some own account (F18); returns sorted by beneficiary_id
def is_candidate(tx: TxCandidate) -> bool
    # (tx.type or '').upper()=='DEBIT' and amount is not None and amount < 0
    # and tx.transaction_type in PAYMENT_TRANSACTION_TYPES and tx.flow_type != 'internal_transfer'
def match_transaction(tx: TxCandidate, benes: Sequence[BeneficiaryRecord]) -> MatchResult | None
    # None if not is_candidate(tx). For rule in RULES: hits = {bene_id: token}; first rule with
    # >=1 distinct id decides: 1 -> matched; >1 -> ambiguous (beneficiary_id None,
    # matched_token = min(tokens), candidate_count = len(ids)); NO fall-through after a rule fires.
    # No rule fires -> no_candidate (match_rule 'none', matched_token '', count 0).
def match_all(txs: Iterable[TxCandidate], benes: Iterable[BeneficiaryRecord],
              own_account_numbers: Iterable[str | None]) -> list[MatchResult]
    # eligible_beneficiaries() once; results for candidates only; sorted by transaction_hash
```

Rule semantics. `V` is `description_variants(tx.description)`. A token is `normalise(field)`.

- `account_number`: `len(d := digits_only(bene.account_number)) >= 8` and `d in digits_only(tx.description)`. `matched_token = mask_account(...)`. The full number never goes in a token (F16/F20).
- `reference_exact`, `beneficiary_name_exact`, `name_exact`: `len(token) >= MIN_EXACT_LEN` and `token in V`.
- `name_prefix` (over reference, beneficiaryName and name). For some `v in V`, either:
  - `len(token) >= 7` and `v.startswith(token + " ")`, which is a word-boundary prefix ("Acme Trading" inside "acme trading pty"); or
  - `len(v) >= 7` and `token.startswith(v)`, which covers a truncated description such as "ACME TRAD" → "acme trading".
- Determinism (F12): candidates are iterated in sorted order, sets are reduced with `min` or `sorted`, and nothing depends on the clock, locale or hash order. `str.casefold` and NFKC are locale-independent.

## S3: `src/invespend/db.py`, append only (no edits above the current last line)

```python
def upsert_beneficiaries(conn, records: Sequence[BeneficiaryRecord], synced_at: datetime) -> int
    # insert into beneficiaries (beneficiary_id, beneficiary_name, name, reference_name, bank,
    #   branch_code, account_number, last_seen_at, active) values (%s,...,%s,true)
    # on conflict (beneficiary_id) do update set beneficiary_name=excluded.beneficiary_name,
    #   name=excluded.name, reference_name=excluded.reference_name, bank=excluded.bank,
    #   branch_code=excluded.branch_code, account_number=excluded.account_number,
    #   last_seen_at=excluded.last_seen_at, active=true        -- first_seen_at untouched
def retire_missing_beneficiaries(conn, synced_at: datetime) -> int
    # update beneficiaries set active=false where active and last_seen_at < %s   (soft; never DELETE)
def load_own_account_numbers(conn) -> list[str]
    # select account_number from accounts where account_number is not null order by 1
def load_active_beneficiaries(conn) -> list[BeneficiaryRecord]
    # select <7 cols> from beneficiaries where active order by beneficiary_id
def load_match_candidates(conn, transaction_types: Sequence[str]) -> list[TxCandidate]
    # select f.transaction_hash, f.type, f.transaction_type, f.description, f.amount, f.flow_type
    # from transactions_flow f left join beneficiary_matches m on m.transaction_hash = f.transaction_hash
    # where f.type = 'DEBIT' and f.amount < 0 and f.transaction_type = any(%s)
    #   and f.flow_type is distinct from 'internal_transfer'
    #   and (m.transaction_hash is null or m.status <> 'matched')       -- Q4 freeze (read side)
    # order by f.transaction_hash
def upsert_beneficiary_matches(conn, results: Sequence[MatchResult], snapshot_at: datetime) -> int
    # insert into beneficiary_matches (transaction_hash, status, beneficiary_id, match_rule,
    #   matched_token, candidate_count, snapshot_at) values (...)
    # on conflict (transaction_hash) do update set status=excluded.status,
    #   beneficiary_id=excluded.beneficiary_id, match_rule=excluded.match_rule,
    #   matched_token=excluded.matched_token, candidate_count=excluded.candidate_count,
    #   snapshot_at=excluded.snapshot_at, matched_at=now()
    # where beneficiary_matches.status <> 'matched'                     -- Q4 freeze (write side)
    #   and (beneficiary_matches.status, beneficiary_matches.beneficiary_id,
    #        beneficiary_matches.match_rule, beneficiary_matches.matched_token)
    #       is distinct from (excluded.status, excluded.beneficiary_id,
    #        excluded.match_rule, excluded.matched_token)               -- no churn (F17)
```

How Q4 freeze works in SQL: a `matched` row is never selected as a candidate, and even if one were passed, the `DO UPDATE ... WHERE status <> 'matched'` turns the conflict into a no-op. `ambiguous` and `no_candidate` rows are re-evaluated on every run against the latest active snapshot, and are only rewritten if the outcome changes.

Soft-retire: beneficiaries are never deleted. Matches made against a beneficiary that was later renamed keep their original `matched_token`, which records the name at the time of the match. The view shows the current name next to it, so a rename leaves a trace (F25).

F8: none of these helpers touch `transactions`, `accounts` (read only), `balances`, `sync_runs` or `category_map` for writes. `_TX_COLUMNS`, `_tx_params`, `transaction_hash`, `assign_day_seq` and `upsert_transaction(s)` are unchanged. The import of the `beneficiary_match` types is a type-only import under `TYPE_CHECKING`, plus a local import inside the functions, so there is no import cycle.

## S4: config flag

- `config.py`: add `beneficiary_matching_enabled: bool = False` as the last field, after `payments_state_backend`. In `Settings.load()`, add `beneficiary_matching_enabled=_opt_bool("BENEFICIARY_MATCHING_ENABLED", False)`.
- Existing `Settings(**kwargs)` and `_Settings()` stubs keep working because the field has a default and the hook reads it with `getattr(settings, "beneficiary_matching_enabled", False)`.
- The flag is distinct from `PAYMENTS_BENEFICIARIES_FROM_API` (F11).

## S5: orchestrator and ingest hook

New module `src/invespend/beneficiary_sync.py`. It is not pure, but it keeps `beneficiary_match.py` clock-free.

```python
def sync_and_match(client: InvestecClient, database_url: str,
                   *, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict
    # 1. data = client.get_beneficiaries()          -- API, NO DB connection open (F19)
    # 2. records = parse_beneficiaries(data); if records is None: log.warning("beneficiary fetch
    #    returned malformed payload; snapshot kept"); return {}      -- no writes (F19/F28)
    # 3. if not records: log.warning(...empty; snapshot kept); return {}   -- never mass-retire on empty
    # 4. synced_at = now()
    #    with db.connect(url): upsert_beneficiaries; retire_missing_beneficiaries;
    #                          own = load_own_account_numbers; benes = load_active_beneficiaries;
    #                          txs = load_match_candidates(PAYMENT_TRANSACTION_TYPES sorted)
    # 5. results = match_all(txs, benes, own)       -- pure, no connection open
    # 6. with db.connect(url): upsert_beneficiary_matches(results, synced_at)
    # 7. log.info counts only (beneficiaries=N, matched=a, ambiguous=b, no_candidate=c); return the same
    #    dict (NOT merged into the ingest summary). Never logs names/account numbers.
```

Hook in `ingest.py`. It is inserted after the `try/except` block, so it runs only after `finish_sync_run(status="success")` and before `summary = {...}`:

```python
    if getattr(settings, "beneficiary_matching_enabled", False):
        try:
            from . import beneficiary_sync
            beneficiary_sync.sync_and_match(client, url)
        except Exception as exc:  # noqa: BLE001 - non-fatal, logging only
            log.warning("Beneficiary matching skipped (%s)", type(exc).__name__)
```

Properties of the hook:
- With the flag off, no code path executes, there are no API calls and no SQL (F11).
- `sync_runs` was already finalised, so a failure cannot flip its status (F9/F19).
- The summary dict and its keys are unchanged. `cli.py:43` stdout and the new-account alerts are unchanged (F9).
- Only the exception class name is logged, never its message, which could carry data (F20).

## S6: tests (NEW files only; `git diff --name-status <base> -- tests/` shows only `A`)

All fixtures use synthetic data: names like "Acme Trading", and account numbers like `9999 0000 1234` that are obviously fake. There are no real values (F24).

| Test (file) | Criteria |
|---|---|
| `test_beneficiary_migration.py::test_0012_is_last_and_unique_prefix` | F4 |
| `::test_0012_idempotent_guards` (every create table has `if not exists`; every policy is preceded by `drop policy if exists`; no `insert/update/delete`; no `alter/drop/replace` on the list of existing objects; `create or replace view` only for `transactions_beneficiary`) | F5 |
| `::test_0012_rls_deny_all_and_security_invoker` | F6 |
| `::test_0012_no_fk_to_transactions_and_inner_join` (no `references`; `join transactions_flow` is not `left`) | F7 |
| `::test_0012_view_columns` (the F21 column list is present, selects from transactions_flow, filters internal_transfer) | F18 F21 |
| `::test_0012_cheap` (no `create index`, no `insert ... select`) | F29 |
| `::test_0012_pii_columns` (no jsonb/raw on beneficiaries; no cell/email column; match_rule/matched_token `not null`; status check covers 3 values) | F16 F20 F32 |
| `::test_existing_migrations_unchanged` (sha256 of 0001-0011 equals the hashes pinned at base in the fixture) | F4 |
| `test_beneficiary_match.py::test_normalise_*` (case, punctuation, whitespace, NFKC, None) | F12 |
| `::test_permutation_invariance` (itertools permutations of txs and benes give an identical `match_all`) | F12 |
| `::test_module_has_no_clock_random_fuzzy` (source grep: datetime.now/random/difflib/SequenceMatcher absent) | F12 F15 |
| `::test_credit_never_matches` / `::test_debit_matches` | F13 |
| `::test_payment_type_gate[...]` (parametrized over the 3 allowlisted types → match; CardPurchases, FeesAndInterest, ATMWithdrawals, DebitOrders, None, "Bogus" → None) plus `::test_allowlist_is_single_constant` | F14 |
| `::test_fail_closed[...]` golden cases: `Acme`/`Acme Trading` with desc "ACME" → no_candidate; `J Smith`/`J Smithers` with desc "J SMIT" → no_candidate, desc "J SMITHERS" → J Smithers only (never J Smith); two benes with the same name → ambiguous; "Mom", "Rent" → no_candidate; desc "ACME TRAD" with "Acme Trading" and "Acme Trading Two" → ambiguous; single clean → matched | F15 F27 |
| `::test_no_fall_through_on_ambiguity` (2 hits on reference_exact plus 1 on name_exact → ambiguous) | F15 F26 |
| `::test_precedence` (A via referenceName, B via beneficiaryName → A, rule reference_exact) | F26 |
| `::test_match_explainable` (every positive golden case: rule ∈ RULES, matched_token equals the expected normalised token; account rule token == `***234`) | F16 F20 |
| `::test_own_account_excluded` (a bene with an own Investec number plus a matching desc → no match; own account at another bank remains eligible) and `::test_internal_transfer_excluded` | F18 |
| `::test_golden_shapes[...]` from `tests/fixtures/beneficiary/golden_descriptions.json`: uppercase, truncated, "Transfer to"/"PAYMENT TO" prefix, digit-grouped account number, ambiguous, renamed (old name no longer matches the new snapshot), deleted (inactive bene not a candidate) | F27 |
| `::test_parse_beneficiaries_allowlist` (input has cellNo/emailAddress/referenceAccountNumber/extra → keys exactly SNAPSHOT_FIELDS; repr has no account digits) | F20 |
| `::test_parse_beneficiaries_malformed[dict, None, "x", 5]` → None | F19 F28 |
| `test_beneficiary_sync.py` uses its own `_FakeConn`/`_FakeCursor` (copied pattern, not imported from test_payment_pg_store) and `monkeypatch db._open` | |
| `::test_sync_api_before_connect` (call-order spy: get_beneficiaries happens before the first `_open`) | F19 |
| `::test_sync_fetch_raises_or_malformed_no_writes` (raises / dict / None / [] → zero SQL, warning logged) | F19 F28 |
| `::test_sync_soft_retire` (sync A, then sync B without X → `update ... active=false` issued, no `delete` anywhere in the SQL log) | F25 |
| `::test_match_upsert_freeze_sql` (the upsert SQL contains `where beneficiary_matches.status <> 'matched'`; the candidate SQL excludes matched) | F17 F25 |
| `::test_idempotent_rerun` (two runs with the same fakes produce identical param lists) | F17 |
| `::test_status_values_persisted` (ambiguous and no_candidate rows are written with distinct status) | F32 |
| `::test_only_new_tables_written` (every insert/update/delete in the SQL log targets `beneficiaries` or `beneficiary_matches`) | F8 |
| `::test_no_account_digits_logged` (caplog over a full sync plus match contains no fixture account digits) | F20 |
| `test_beneficiary_ingest_freeze.py` (see below) | F9 F10 F11 F19 |

### How F9 and F10 are verified

F9 covers the ingest stdout, the summary and `sync_runs`. `test_beneficiary_ingest_freeze.py` does the following:
- Monkeypatch `ingest.InvestecClient` with a `_FakeClient` that has accounts, balance, transactions and a `get_beneficiaries` spy.
- Monkeypatch `db.connect` with a no-op contextmanager yielding a recording fake connection.
- Monkeypatch `db.init_db`, `start_sync_run` (returns 1), `get_account_ids`, `upsert_account`, `upsert_balance`, `upsert_transactions` and `finish_sync_run` (recording kwargs).
- Monkeypatch `cli.Settings.load` to return a `Settings` built with the flag set.

It then runs `cli.cmd_ingest(argparse.Namespace(days=None, from_date="2026-01-01", to_date="2026-01-07", resume=False, full=False))` under `capsys` for three scenarios: (a) flag OFF, (b) flag ON with a working fake, (c) flag ON with `get_beneficiaries` raising, plus a variant returning `{"bad": 1}`. It asserts:
- `capsys.out` is identical across a/b/c.
- `run_ingest(...)` returns equal dicts with `list(keys) == ["from_date","to_date","accounts","transactions_upserted","balances_captured","new_accounts"]`.
- The `finish_sync_run` call args are identical, with status `"success"` in all three.
- For (a), `get_beneficiaries.call_count == 0` and no SQL in the fake log names `beneficiar`.
- For (c), no exception is raised and the warning appears in caplog (stderr), not in stdout.
- `Settings.load()` with the env var unset gives `beneficiary_matching_enabled is False` (F11).

F10 covers the Excel output, which is strict under Q6.
- Primary guard: `git diff <base> -- src/invespend/report.py src/invespend/statements.py` is empty, and the existing `test_report.py`/`test_statements.py` stay green. The tester checks both.
- Secondary pytest guard (`::test_excel_outputs_unchanged`): build a fixed synthetic DataFrame and write it via `report.build_spend_summary` + `report.write_workbook` and via `statements.build_account_statement` + `statements.write_statement_workbook` into `tmp_path`, once with the flag off and once after running the S5 hook with the flag on (beneficiary modules imported).
- Compare the two workbooks cell-by-cell with openpyxl: sheet names and order, values, `number_format`, column widths. This is needed because the xlsx zip timestamps are not byte-stable.
- Also compare against a committed JSON digest `tests/fixtures/beneficiary/excel_golden.json`, generated from the base code (the generator lives in the test as a helper; JSON is used because `*.xlsx` is git-ignored).
- Assert `report.COLUMNS` and `statements.STATEMENT_COLUMNS` equal their frozen literals.

## S7: docs

- `.env.example`: append a commented block: `# Beneficiary matching (off by default): label outgoing EFTs with the registered beneficiary.` followed by `BENEFICIARY_MATCHING_ENABLED=false`. No real values (F28).
- `README.md`: new section "Optional: beneficiary matching" covering:
  - the flag;
  - that it runs after a successful ingest and is non-fatal;
  - the `transactions_beneficiary` view;
  - the rule list and its precedence;
  - fail-closed and ambiguous behaviour;
  - freeze semantics;
  - the PII note: the full account number is stored and deny_all RLS applies;
  - a sample query: `select effective_date, description, amount, beneficiary_name, beneficiary_bank, beneficiary_account_number, match_rule from transactions_beneficiary where match_status = 'matched' order by effective_date desc;` (F31).

## Risks flagged

1. **R1, MEDIUM (S1):** 0012 re-runs on every ingest inside `init_db` with a 15s `lock_timeout`. A syntax or idempotency error would break the daily ingest even with the flag OFF, because the migration applies regardless of the flag. Mitigations:
   - Static tests (F5/F6).
   - `create or replace view ... with (security_invoker = on)` needs Postgres 15 or later, which Supabase provides (0009 already relies on it).
   - The view's column list must never be reordered in later runs; only appending is allowed.
   - No test runs the SQL against a real Postgres. The tester should apply all migrations twice on a scratch PG if one is available.
2. **R2, MEDIUM (S5):** the ingest control flow is touched. The hook sits after `finish_sync_run`, so it cannot affect `sync_runs`. It is wrapped in a broad `try/except`. It is guarded by `getattr`. When the flag is OFF it adds zero calls.
3. **R3, MEDIUM (Q1 / PII):** the full third-party account numbers persist in `beneficiaries` and therefore land in `pg_dump` backups, which are encrypted only if `BACKUP_PASSPHRASE` is set. This was approved by the human. The plan never logs these numbers, uses a masked `__repr__` and a masked token, and caplog tests enforce this.
4. **R4, MEDIUM (Q2):** the transactionType allowlist is unverified. If prod uses other values, the matcher silently matches nothing. That is safe because it fails closed. Revisit once the human runs the type-count query.
5. **R5, LOW-MEDIUM:** a transient empty `[]` from the API would retire every beneficiary. To prevent this, an empty list is treated like a failure: it is skipped and the snapshot is kept. A user who truly deletes all beneficiaries keeps stale `active=true` rows. These rows are harmless because past matches are frozen, but they could still match new rows.
6. **R6, LOW:** `transaction_hash` can be rewritten by `backfill-hashes`. The old match row becomes an invisible orphan and the new hash is re-evaluated as an unmatched row. Orphans are never cleaned up in this run (no DELETE); this is an accepted cost.
7. **R7, LOW:** Tier C can falsely classify a row as `internal_transfer`, which hides a genuine EFT. This is fail-closed and documented.
8. **R8, LOW:** the full-history candidate scan runs every ingest (unmatched or ambiguous allowlisted debits only). Volume is small. No index is added on existing tables (F29).
9. **R9, LOW (F15 wording):** the literal F15 pair "J Smith vs J Smithers" is pinned as "no FALSE match": the truncated "J SMIT" gives no match, and "J SMITHERS" never resolves to J Smith. A clean exact description is still a legitimate match. The output-reviewer should confirm this reading.
10. **R10, LOW:** CLAUDE.md's "62" floor is stale. 211 is binding per the spec and is not edited here (out of scope).
