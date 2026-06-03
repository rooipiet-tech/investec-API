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
  account, top merchants, daily trend) emailed to you. Inter-account transfers
  are reported as their own money-in/out totals and excluded from spend/income.
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
