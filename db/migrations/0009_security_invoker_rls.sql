-- Security hardening: make the analytical views honour the *caller's* RLS, and
-- lock down a stray ad-hoc backup table. Both close findings raised by the
-- Supabase database linter (0010_security_definer_view, 0013_rls_disabled_in_public,
-- 0023_sensitive_columns_exposed) without changing any observable output.
--
-- ── Why the views were a hole ────────────────────────────────────────────────
-- Postgres runs a view with the privileges of its OWNER by default
-- (`security_invoker = off`), which the linter reports as "SECURITY DEFINER".
-- Our base tables (accounts, transactions, balances, category_map) have RLS
-- enabled with NO policies precisely so the Supabase anon / authenticated roles
-- — anything holding the public API key — cannot read this banking data. But a
-- view owned by `postgres` bypasses that RLS, so querying the views through
-- PostgREST leaked every locked-down row straight back out.
--
-- `security_invoker = on` (Postgres 15+) makes each view execute with the RLS and
-- permissions of the role that QUERIES it. The public roles then hit the empty
-- policy set on the base tables and see nothing, while the ingest/report/report
-- jobs — which connect with the direct Postgres (table-owner) role and so bypass
-- RLS — keep seeing everything. The view definitions are untouched; only the
-- execution context changes, so legitimate output is byte-for-byte unchanged.
--
-- Idempotent and order-safe: every view below is created by an earlier migration,
-- and init-db re-applies all migrations in order each run, so this ALTER always
-- runs last and wins.

alter view transactions_normalized   set (security_invoker = on);
alter view transactions_categorized  set (security_invoker = on);
alter view spend_by_category         set (security_invoker = on);
alter view monthly_movement          set (security_invoker = on);
alter view balance_reconciliation    set (security_invoker = on);
alter view transactions_flow         set (security_invoker = on);
alter view monthly_flows             set (security_invoker = on);

-- ── Stray backup table ───────────────────────────────────────────────────────
-- `transactions_backup_20260615` is an ad-hoc snapshot taken by hand before the
-- 0008 dedup migration. It is not part of this schema (no migration creates it),
-- but in the live database it sits in `public` with RLS off and a `card_number`
-- column, so PostgREST exposed sensitive financial data to the public API key.
-- Enable RLS with no policies to lock it down exactly like the real tables. The
-- IF EXISTS guard keeps this a no-op on a fresh database where the table was
-- never created.
alter table if exists transactions_backup_20260615 enable row level security;
