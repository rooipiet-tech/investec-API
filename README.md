# invespend — Investec transactions → database → weekly spend report

Pulls your Investec **Programmable Banking** transactions into a Postgres
database and emails an Excel spend-analysis at the end of every week. Runs
entirely on **free services** (GitHub Actions + Supabase + Gmail SMTP).

📐 See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the design, security model, and
build-on-top ideas.

```
Investec Open API ──(daily cron)──▶ Postgres (Supabase) ──(Fri cron)──▶ Excel ──▶ email
```

## What you get

- **`invespend ingest`** — idempotent, read-only sync of accounts + transactions.
- **`invespend report --send`** — multi-sheet `.xlsx` (summary, by category, by
  account, top merchants, daily trend) emailed to you. Internal transfers are
  reported separately and excluded from spend/income.
- **`invespend statements --send`** — a **separate** `.xlsx` **per account**,
  each a bank-statement-style transaction listing (newest first) with a running
  balance, covering the account's **full history** by default. Emailed as
  one-attachment-per-account and regenerated on each weekly run. Pass `--days N`
  for a trailing window instead.
- Two scheduled GitHub Actions workflows — no server to run.

---

## Setup

### 1. Investec API credentials
In Investec Online → **Programmable Banking → Enrolled APIs**, generate:
`client_id`, `client_secret`, and an `x-api-key`. These are **read-only** — they
cannot move money.

### 2. Database (Supabase free tier)
1. Create a free project at [supabase.com](https://supabase.com).
2. Copy the connection string: **Project Settings → Database → Connection
   string (URI)** — use the pooler URI, keep `sslmode=require`.
3. Apply the schema:
   ```bash
   invespend init-db          # applies every migration in db/migrations/ in order
   ```

> **Already had data before the `day_seq` dedup change?** Run the one-off
> `invespend backfill-hashes` once after upgrading. It re-keys existing rows to
> the new hash so the rolling-window re-pull dedupes cleanly instead of
> inserting duplicates. It is idempotent — safe to run more than once, and a
> no-op on a fresh database.

### 3. Email (Gmail app password)
Enable 2-Step Verification on your Google account, then create an **App
password** (Google Account → Security → App passwords). Use that 16-char value
as `SMTP_PASSWORD`.

### 4. GitHub Actions secrets
In the repo: **Settings → Secrets and variables → Actions**, add:

| Secret | Example |
|--------|---------|
| `INVESTEC_CLIENT_ID` / `INVESTEC_CLIENT_SECRET` / `INVESTEC_API_KEY` | from Investec |
| `INVESTEC_BASE_URL` | `https://openapi.investec.com` |
| `DATABASE_URL` | `postgresql://...@...:6543/postgres?sslmode=require` |
| `SMTP_HOST` / `SMTP_PORT` | `smtp.gmail.com` / `587` |
| `SMTP_USER` / `SMTP_PASSWORD` | your Gmail + app password |
| `REPORT_SENDER` / `REPORT_RECIPIENTS` | sender + comma-separated recipients |

The schedules then run automatically (daily ingest, Friday report). Trigger
either manually from the **Actions** tab via *Run workflow*.

---

## Everything runs in the cloud

You don't need to install or run anything locally:

- **Tests** run on GitHub Actions (`tests.yml`) on every PR and push.
- **Ingest** and **weekly report** run on their schedules (`ingest.yml`,
  `weekly-report.yml`), or on demand from the **Actions** tab → *Run workflow*.

To trigger a one-off run (e.g. a 90-day backfill), use *Run workflow* on the
**ingest** workflow from the Actions tab.

## Optional: local development

Only if you *want* to run it on your machine — not required.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # fill in your values (git-ignored)

invespend init-db
invespend ingest --days 30  # backfill a month
invespend report --send     # build + email the spend analysis
invespend statements --send # build + email a per-account bank statement each

pytest                      # run the unit tests
```

> **Security:** secrets live only in `.env` (git-ignored) locally and in GitHub
> encrypted secrets in CI. Never commit credentials.

## Optional: beneficiary matching

Labels outgoing payments with the Investec beneficiary you paid. Off by default;
it never moves money and never changes the report, statements or ingest output.

- **Flag:** set `BENEFICIARY_MATCHING_ENABLED=true` (env / GitHub secret). It is
  separate from `PAYMENTS_BENEFICIARIES_FROM_API`.
- **When it runs:** at the end of `invespend ingest`, only after the sync run was
  recorded as successful. It is non-fatal: if the beneficiary fetch fails or
  returns a malformed or empty list, a warning is logged (stderr), the existing
  beneficiary snapshot is kept and ingest finishes exactly as before.
- **Where the result lives:** the view `transactions_beneficiary` (migration
  `0012_beneficiary_matching.sql`, tables `beneficiaries` and
  `beneficiary_matches`). The view's column list is append-only.
- **Candidates:** DEBIT rows with a negative amount whose `transaction_type` is
  `OnlineBankingPayments`, `OnlineBankingTransfers` or `FasterPay` (provisional
  allowlist; NULL/unknown types never match). Internal transfers and
  beneficiaries that are one of your own accounts are excluded.
- **Rules, highest precedence first** (`match_rule`):
  1. `account_number`: the beneficiary account number (8+ digits) appears in one
     digit run of the description, spaces/hyphens allowed (`9999 0000 1234`);
  2. `reference_exact`: your reference name for the beneficiary equals the
     description (after normalising case, punctuation and spacing, and dropping
     a leading "Payment to" / "Transfer to" style prefix);
  3. `beneficiary_name_exact`, then 4. `name_exact`: same, for those fields;
  5. `name_prefix`: a name of 7+ characters is a whole-word prefix of the
     description, or a truncated description (7+ characters, 2+ words) is a
     prefix of a name.
- **Fail-closed:** the first rule that hits decides. One beneficiary: `matched`.
  Several: `ambiguous` (no beneficiary shown, no fall-through to a weaker rule).
  None: `no_candidate`. Exact beats truncation: with "J Smith" and "J Smithers",
  "J SMITH" is J Smith, "J SMITHERS" is J Smithers, "J SMIT" is neither. Short
  names ("Mom", "Rent") never match. No fuzzy matching.
- **Freeze:** a `matched` row is never rewritten. `ambiguous` and `no_candidate`
  rows are re-evaluated on each run. Beneficiaries missing from a later fetch
  are soft-retired (`beneficiary_active = false`), never deleted, so old matches
  still show their name; `matched_token` keeps the token as it was at match time.
- **Caveats:** a frozen match disappears from the view if the transaction is
  later classified `internal_transfer` or the beneficiary becomes one of your own
  accounts. The view is not de-duplicated the way the weekly report is.
- **Privacy:** the full beneficiary account number is stored in `beneficiaries`
  (deny-all RLS, never logged; `matched_token` shows only `***` + last 3 digits).
  Cell numbers, email addresses and raw payloads are not stored. Backups include
  this table, so set `BACKUP_PASSPHRASE`.
- **Access:** the read-only reporting role in `db/roles.sql` has no grant on the
  new objects, so query the view as the database owner / service role:

```sql
select effective_date, description, amount, beneficiary_name, beneficiary_bank,
       beneficiary_account_number, match_rule
from transactions_beneficiary
where match_status = 'matched'
order by effective_date desc;
```
