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
4. For each account it pulls transactions for a rolling window
   (`GET /za/pb/v1/accounts/{id}/transactions?fromDate=&toDate=`).
5. Each transaction is given a **deterministic hash** (the public API has no stable
   transaction id), categorised with a rule engine, and **upserted** — so re-running
   is idempotent and a 7-day overlap window self-heals any gaps or late postings.
6. A row is written to `sync_runs` for observability.

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
- **Least-privilege DB access.** Use the Supabase **connection string for a role
  scoped to these tables**, not the service-role key. Enable Row Level Security if
  you later expose the data to a client app; the ingest job uses a direct Postgres
  connection over TLS.
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
| Replay / duplicate ingest | Deterministic hash + `ON CONFLICT` upsert. |
| Silent failures | `sync_runs` audit table + Actions run history/notifications. |

---

## 4. Database schema

See [`db/migrations/0001_init.sql`](db/migrations/0001_init.sql). Core tables:

- **`accounts`** — one row per Investec account.
- **`transactions`** — normalised transactions with a derived `category`, the
  original payload kept in a `raw jsonb` column (so you never lose fidelity), keyed
  by `transaction_hash`.
- **`sync_runs`** — ingest audit log.

This schema is intentionally a **clean base to build on**: budgets, alerts, a
dashboard, ML categorisation, etc. can all read from `transactions`.

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
├── pyproject.toml             ← package + `invespend` CLI entry point
├── requirements.txt
├── .env.example               ← copy to .env (git-ignored) for local runs
├── db/migrations/0001_init.sql
├── src/invespend/
│   ├── config.py              ← env-driven settings
│   ├── investec_client.py     ← OAuth2 client + API calls (read-only)
│   ├── categorize.py          ← rule-based spend categoriser
│   ├── db.py                  ← Postgres connection + upserts
│   ├── ingest.py              ← daily ingestion job
│   ├── report.py              ← Excel spend-analysis builder
│   ├── emailer.py             ← SMTP sender
│   └── cli.py                 ← `invespend init-db | ingest | report`
├── tests/                     ← unit tests for categoriser + aggregation
└── .github/workflows/
    ├── ingest.yml             ← daily cron
    └── weekly-report.yml      ← Friday cron
```

See **README.md** for the step-by-step setup.
