# Investec Programmable Banking → Database → Weekly Spend Report

A secure, **zero-cost** cloud architecture that:

1. Pulls your Investec account transactions into a Postgres database (a clean
   foundation to build further solutions on).
2. Generates an Excel spend-analysis workbook every week and emails it to you.

Everything below runs on free tiers and managed services — there is **no server
to patch, no VM to pay for**.

---

## 1. High-level architecture

```
                          ┌─────────────────────────────────────────┐
                          │              GitHub repo                  │
                          │   (code + encrypted Actions secrets)      │
                          └───────────────────┬───────────────────────┘
                                              │
                 ┌────────────────────────────┴─────────────────────────┐
                 │                  GitHub Actions (free)                 │
                 │                                                        │
   cron: daily   │   ┌──────────────────┐      cron: Fri 15:00 UTC       │
  ───────────────┼──▶│  ingest job      │      ┌──────────────────────┐  │
                 │   │  (Python)        │      │  weekly-report job   │  │
                 │   └────────┬─────────┘      │  (Python)            │  │
                 │            │                └─────────┬────────────┘  │
                 └────────────┼──────────────────────────┼───────────────┘
                              │ HTTPS (OAuth2)            │ read
                              ▼                           ▼
                  ┌────────────────────┐      ┌──────────────────────────┐
                  │  Investec Open API │      │   Supabase Postgres       │
                  │  openapi.investec  │      │   (free tier)             │
                  └────────────────────┘      │   accounts / transactions │
                              │                └─────────────┬────────────┘
                              │ upsert                       │
                              └──────────────────────────────┘
                                                              │
                                              build .xlsx     ▼
                                              ┌──────────────────────────┐
                                              │  Gmail SMTP (app pwd)     │
                                              │  → emails workbook to you │
                                              └──────────────────────────┘
```

### Why these components (and why they're free)

| Concern        | Choice                          | Free-tier reality |
|----------------|---------------------------------|-------------------|
| Scheduler/compute | **GitHub Actions** cron workflows | 2,000 min/month free on private repos, unlimited on public. Our jobs run seconds. |
| Database       | **Supabase** (managed Postgres) | Free project: 500 MB DB, ample for years of personal transactions. A Supabase MCP server is already connected to this environment. |
| Secrets        | **GitHub encrypted secrets**    | Free, encrypted at rest, injected only into job runtime. |
| Email          | **Gmail SMTP + app password**   | Free. App passwords scope access without exposing your main password. |
| Reporting      | **pandas + openpyxl**           | Open source. |

> **Self-hosting variant:** every component has a like-for-like swap if you ever
> outgrow free tiers — GitHub Actions → any cron host, Supabase → any Postgres,
> Gmail SMTP → SES/SendGrid. The application code does not change.

---

## 2. Data flow

### Ingestion (daily)
1. GitHub Actions cron triggers `invespend ingest --days 7`.
2. The job authenticates to Investec with the **OAuth2 client-credentials** flow
   (`client_id` + `secret` + `x-api-key`), receiving a short-lived bearer token.
3. It lists accounts (`GET /za/pb/v1/accounts`) and upserts them.
4. For each account it captures today's balance snapshot
   (`GET /za/pb/v1/accounts/{id}/balance`) into `balances` — non-fatal, so a
   balance hiccup never aborts the transaction sync.
5. For each account it pulls transactions for a rolling window
   (`GET /za/pb/v1/accounts/{id}/transactions?fromDate=&toDate=`).
6. Each transaction is given a **deterministic hash** (the public API has no stable
   transaction id), categorised with a rule engine, and **upserted** — so re-running
   is idempotent and a 7-day overlap window self-heals any gaps or late postings.
7. A row is written to `sync_runs` for observability.

### Reporting (weekly)
1. GitHub Actions cron triggers `invespend report --send` on Friday.
2. The job reads the last 7 days from Postgres into a DataFrame.
3. It builds a multi-sheet Excel workbook: summary, by-category, by-account,
   top merchants, and a daily trend.
4. It emails the workbook as an attachment via Gmail SMTP.

---

## 3. Security model

Banking data is sensitive. The design minimises the blast radius of any single
failure.

- **No long-lived servers.** Compute exists only for the seconds a job runs, then
  the runner is destroyed. Nothing to compromise between runs.
- **Secrets never touch the repo.** All credentials live in GitHub Actions
  encrypted secrets and are injected as env vars at runtime. `.env` is git-ignored;
  only `.env.example` (no values) is committed.
- **Least-privilege Investec keys.** The Open API client-credentials grant is
  **read-only** for account/transaction data — it cannot move money. Use a key
  dedicated to this integration so it can be revoked independently.
- **Least-privilege DB access.** Two roles, not one:
  - **Ingest** connects with the owner/service role (it must write `transactions`).
  - **Reporting / Power BI** connect with a **read-only role scoped to the views**
    (`db/roles.sql`), set as `REPORT_DATABASE_URL`. The report falls back to
    `DATABASE_URL` only if that is unset.
  Enable Row Level Security (already on for the base tables); the ingest job uses a
  direct Postgres connection over TLS.
- **Email via scoped app password**, never your account password; revocable in one
  click, and rotate periodically.
- **TLS everywhere** — Investec API, Supabase Postgres (`sslmode=require`), SMTP.
- **Idempotent + auditable.** Deterministic hashing prevents duplicates; `sync_runs`
  records every execution and any error.
- **Secret rotation.** Because secrets are centralised in GitHub, rotation is:
  update the secret → next scheduled run picks it up. No redeploy.

### Threat-model quick reference

| Threat | Mitigation |
|--------|-----------|
| Leaked repo | No secrets in code; data lives in Supabase, not the repo. |
| Leaked Investec key | Read-only scope (cannot transact); revoke + rotate. |
| Leaked DB string | Scoped role; rotate in Supabase; enable RLS. |
| Compromised email | App password is revocable and email-only. |
| Replay / duplicate ingest | Deterministic hash (incl. `day_seq`) + `ON CONFLICT` upsert. |
| Silent failures | `sync_runs` audit table, surfaced as a **Sync Health** sheet (via the `sync_health` view) in the weekly report; a failed scheduled run also opens/updates a GitHub issue. |

### Free-tier constraints & mitigations

| Constraint (free tier) | Mitigation |
|------------------------|-----------|
| Supabase pauses after 7 days of DB inactivity | The daily ingest writes every day, so the timer never resets — solved by design. |
| No managed backups on free Supabase | Weekly `invespend backup` runs `pg_dump`, gzips it (and encrypts with `BACKUP_PASSPHRASE` if set), and uploads it as a CI artifact. |
| GitHub disables cron after ~60 days of repo inactivity | A weekly `heartbeat` workflow commits to a dedicated `heartbeat` branch (keeping `main` history clean) and best-effort re-enables the scheduled workflows. |
| No private networking / IP allowlist on free | Strong secrets + enforced SSL + RLS. Upgrade path: Supabase Pro network restrictions or an Azure private endpoint. |
| 500 MB DB cap | Transactions are tiny — years of headroom. Monitor `sync_runs` growth. |

### Compliance (POPIA)

- **Data residency.** Free-tier Supabase runs in the AWS region chosen at project
  creation, likely **not** South Africa — that is cross-border processing of
  personal/financial data under POPIA.
- **Scope.** This design is for Canvas Intelligence's **own** banking data. Do
  **not** extend it to client transaction data on the free tier.
- **Upgrade trigger.** Before any client data or sensitive scale, migrate storage
  to a South-Africa-resident option (e.g. Azure Database for PostgreSQL, South
  Africa North) with private networking and managed backups.
- This is a technical architecture, not legal advice; confirm POPIA handling with a
  qualified advisor before processing any data beyond your own.

---

## 4. Database schema

Migrations live in [`db/migrations/`](db/migrations/) and are applied in order by
`invespend init-db`, which records each applied file in `schema_migrations` so a
migration runs exactly once — the nightly init is a lock-free no-op in the steady
state. Core objects:

**Tables**
- **`accounts`** — one row per Investec account.
- **`transactions`** — the raw landing table: normalised transactions with the
  original payload kept in a `raw jsonb` column (so you never lose fidelity), keyed
  by `transaction_hash` and disambiguated within a day by `day_seq`.
- **`category_map`** — keyword → category dimension the categorised view joins
  to. It is an exact projection of the canonical Python rules in `categorize.py`,
  re-synced on every `init-db` (and pruned of stale keywords), so categories have
  one source of truth: edit `categorize.py`, never the table.
- **`balances`** — daily balance snapshot per account (one row per account per day)
  from `getAccountBalance`, for charting balance-over-time and reconciliation.
- **`sync_runs`** — ingest audit log (exposed to the reporting role through the
  `sync_health` view, which the weekly report condenses into a Sync Health sheet).

**Build-layer views** (`0002_views.sql`) — every consumer (Power BI, the weekly
report, future solutions) reads these, never the raw table:
- **`transactions_normalized`** — typed, trimmed, with an effective date + month.
- **`transactions_categorized`** — category re-derived from `category_map`.
- **`transactions_flow`** — labels each transaction `internal_transfer` /
  `external_inflow` / `external_outflow`. The `accounts` table is the authoritative
  list of owned accounts; a transaction is an internal transfer when its
  counterparty resolves to another owned account via a layered signal (own account
  number in the description → own holder name *with transfer context*, so a salary
  that mentions your name stays external → a guarded equal-and-opposite matched
  leg on the same value date). `flow_signal` records which tier fired.
- **`spend_by_category`** — spend/income aggregated per category per month
  (**excludes** `internal_transfer` so own-account movements aren't double-counted).
- **`monthly_flows`** — per month: external inflow vs outflow vs internal-transfer
  volume — the headline internal-vs-external view.
- **`monthly_movement`** — month-over-month movement bridge per category.
- **`balance_reconciliation`** — bank-reported balance vs the running balance on
  the latest stored transaction, flagged `reconciled` when they agree to the cent.

This schema is intentionally a **clean base to build on**: budgets, alerts, a
dashboard, ML categorisation, etc. all read from the views.

> **Least-privilege roles:** ingest uses the owner/service connection; reporting and
> Power BI use a read-only role scoped to the views — see
> [`db/roles.sql`](db/roles.sql) and set it as `REPORT_DATABASE_URL`.

---

## 5. Build-on-top ideas (the "build solutions from" part)

Because the data lands in plain Postgres, you can layer on:

- **A dashboard** — Supabase exposes an instant REST/GraphQL API + you can point
  Metabase/Grafana (both free) at the DB.
- **Real-time alerts** — Investec also supports per-transaction webhooks/lambdas in
  the programmable-banking sandbox; pipe those into the same `transactions` table.
- **Budgets & anomaly detection** — scheduled SQL or a small ML model over history.
- **Smarter categorisation** — replace the rule engine with an LLM call enriching
  `category`.

---

## 6. Repository layout

```
.
├── ARCHITECTURE.md            ← this file
├── README.md                  ← setup & run instructions
├── pyproject.toml             ← package, CLI entry point, ruff/mypy/pytest config
├── .env.example               ← copy to .env (git-ignored) for local runs
├── db/
│   ├── migrations/            ← 0001…0008, applied once each (schema_migrations)
│   ├── grants.sql             ← read-only report-role grants (applied by init-db)
│   └── roles.sql              ← operator step: create the report_readonly role
├── src/invespend/
│   ├── config.py              ← env-driven settings (lazy per-command validation)
│   ├── investec_client.py     ← OAuth2 client + API calls (read-only)
│   ├── categorize.py          ← rule-based spend categoriser
│   ├── db.py                  ← Postgres connection, migrations + upserts
│   ├── ingest.py              ← daily ingestion / backfill job
│   ├── report.py              ← Excel spend-analysis builder
│   ├── backup.py              ← streamed pg_dump → gzip/encrypted artifact
│   ├── emailer.py             ← SMTP sender
│   └── cli.py                 ← init-db | ingest | report | backup | backfill-hashes
├── tests/                     ← unit tests + DB integration tests (TEST_DATABASE_URL)
└── .github/workflows/
    ├── ingest.yml             ← daily cron
    ├── weekly-report.yml      ← Friday cron (report + backup)
    ├── backfill.yml           ← manual historical backfill
    ├── heartbeat.yml          ← weekly keep-alive (dedicated branch)
    └── tests.yml              ← lint + type-check + unit/integration tests
```

See **README.md** for the step-by-step setup.
