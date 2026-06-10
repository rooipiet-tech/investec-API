-- Read-only reporting grants, applied by `invespend init-db` on every run
-- (after the migrations).
--
-- db/roles.sql documents the one-off operator step of creating the
-- `report_readonly` role. This file keeps that role's privileges in sync with
-- the build layer: because it runs on every init-db, the role can be created
-- at any time (before or after the views exist) and the next scheduled run
-- converges. The DO block is a no-op until the role exists, and skips the
-- GRANTs once they are already in place so the steady state takes no locks.
--
-- Only the build-layer views are granted, never the base tables: the views
-- execute with their owner's privileges, while RLS (enabled with no policies)
-- blanks the base tables for every other role — which is the point.
do $$
begin
    if exists (select 1 from pg_roles where rolname = 'report_readonly')
       and not (
            has_table_privilege('report_readonly', 'public.transactions_flow', 'select')
        and has_table_privilege('report_readonly', 'public.monthly_flows', 'select')
        and has_table_privilege('report_readonly', 'public.balance_reconciliation', 'select')
       )
    then
        grant usage on schema public to report_readonly;
        grant select on
            public.transactions_normalized,
            public.transactions_categorized,
            public.transactions_flow,
            public.spend_by_category,
            public.monthly_movement,
            public.monthly_flows,
            public.balance_reconciliation
        to report_readonly;
    end if;
end $$;
