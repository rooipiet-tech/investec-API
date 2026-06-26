# Deploying invespend on Railway

`invespend` is a **Python CLI / cron job**, not a web app. There is no HTTP
server and nothing to deploy on Vercel. On Railway it runs as a **Docker
container** (`railway.toml` → `Dockerfile`) whose scheduled jobs invoke the CLI
subcommands. All secrets are **Railway environment variables** — never commit a
`.env` (it stays git-ignored).

## 1. Set the Railway Variables

Add these under the service's **Variables** tab. Group / meaning:

**Investec (read + pay) credentials**
- `INVESTEC_CLIENT_ID`, `INVESTEC_CLIENT_SECRET`, `INVESTEC_API_KEY`
- `INVESTEC_BASE_URL` (optional; defaults to the Investec OpenAPI host)
- For a separate write-scoped key: `INVESTEC_WRITE_CLIENT_ID`,
  `INVESTEC_WRITE_CLIENT_SECRET`, `INVESTEC_WRITE_API_KEY`

**Approval signing**
- `APPROVAL_SIGNING_SECRET` — strong random string (>= 16 chars). Without it no
  token can be issued or verified (fails closed).

**Email in / out**
- IMAP (inbound approvals): `IMAP_HOST`, `IMAP_PORT`, `IMAP_USER`,
  `IMAP_PASSWORD`, `IMAP_MAILBOX`
- SMTP (outbound): `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`,
  `REPORT_SENDER`, `REPORT_RECIPIENTS`

**Database**
- `DATABASE_URL` — Postgres connection string (e.g. Supabase). Also reused by the
  Postgres state backend below.

**Caps (required before any payment passes — default 0 = fail-closed)**
- `PER_PAYMENT_CAP`, `DAILY_AGGREGATE_CAP`

**Payment safety flags**
- `PAYMENTS_DRY_RUN` (default `true`), `PAYMENTS_LIVE_ENABLE` (default `false`),
  `INVESTEC_PAYMENTS_ENABLED` (declare the main key is payment-capable),
  `PAYMENTS_BENEFICIARIES_FROM_API`

## 2. Choose how payment state survives restarts

Pending records, the **daily-aggregate cap counter**, and the **audit trail**
must persist across container restarts — otherwise a restart resets the daily cap
and loses the audit history. Pick ONE:

- **Option A — Postgres (recommended).** Set `PAYMENTS_STATE_BACKEND=postgres`.
  State is stored in the DB (reuses `DATABASE_URL`), so **no persistent volume is
  needed**. Run `invespend init-db` once to apply migrations (including the
  additive `0009_payment_approvals.sql`).

- **Option B — File + volume.** Set `PAYMENTS_STATE_BACKEND=file` (the default)
  and attach a **Railway persistent volume** mounted at the path you set in
  `PAYMENTS_STATE_DIR`. Without the volume the JSON state lives on ephemeral
  container disk and is lost on every redeploy/restart.

Without one of these, the daily-cap and audit trail will **not** survive
restarts.

## 3. Schedule the cycle

Run one approval cycle on a Railway **cron / scheduled job**:

```
invespend approve-payments --once
```

(`ingest`, `report`, and `statements` are separate scheduled jobs if you use
them.)

## 4. Go live carefully

1. Keep `PAYMENTS_DRY_RUN=true` first and watch the JSON output / audit trail —
   no write endpoint is called in dry-run.
2. When confident, enable live (`PAYMENTS_LIVE_ENABLE=true` plus a
   payment-capable credential) and make a **small-amount first live payment**
   before raising the caps.
