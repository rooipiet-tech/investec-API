-- Lock down the payment tables exposed via PostgREST (Supabase linter
-- 0013_rls_disabled_in_public). These tables are created outside this repo (by
-- the payments component, not by any migration here), but they live in `public`
-- with RLS off, so the Supabase anon / authenticated roles — anything holding the
-- public API key — could read them. `payment_pending` in particular holds live
-- payment instructions (amount, beneficiary_id, source_account_id / _last3, nonce,
-- message_id_hash), and the other two hold payment totals and an audit trail.
--
-- Enable RLS with NO policies, exactly like accounts/transactions/balances: the
-- direct Postgres (owner) and service_role connections that run payments still
-- bypass RLS and are unaffected, while the public API key can no longer read the
-- data. IF EXISTS keeps this a no-op on a database where the payments component
-- has not (yet) created these tables — e.g. a fresh init-db.
alter table if exists payment_pending     enable row level security;
alter table if exists payment_daily_total enable row level security;
alter table if exists payment_audit       enable row level security;
