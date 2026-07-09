-- Add an explicit deny-all RLS policy to every locked-down table.
--
-- These tables run with RLS enabled and no policies, which already denies the
-- Supabase anon / authenticated (public API) roles every row — the secure state
-- we want. The linter still flags it (INFO rls_enabled_no_policy) because a table
-- with RLS on but zero policies is *usually* a mistake. Making the intent explicit
-- with a permissive `using (false)` policy keeps the behaviour identical (still
-- deny-all for the public roles; the owner / service_role connections that run
-- ingest, reporting and payments bypass RLS and are unaffected) and clears the
-- advisory.
--
-- Permissive (not restrictive) on purpose: it contributes nothing on its own, so
-- if a real access policy is ever added later it simply OR-s in alongside this one
-- rather than being silently overridden by a restrictive deny.
--
-- Idempotent (drop-then-create) and guarded with to_regclass so tables created
-- outside this repo (the payment_* tables, the ad-hoc backup) are handled when
-- present and skipped on a fresh database where they don't exist.
do $$
declare
    t text;
    targets text[] := array[
        'accounts', 'balances', 'category_map', 'sync_runs', 'transactions',
        'transactions_backup_20260615',
        'payment_pending', 'payment_daily_total', 'payment_audit'
    ];
begin
    foreach t in array targets loop
        if to_regclass('public.' || t) is not null then
            execute format('alter table public.%I enable row level security', t);
            execute format('drop policy if exists deny_all on public.%I', t);
            execute format(
                'create policy deny_all on public.%I for all to public '
                'using (false) with check (false)', t
            );
        end if;
    end loop;
end $$;
