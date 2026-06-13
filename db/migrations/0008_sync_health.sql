-- Observability: surface ingest run history to the reporting role.
--
-- sync_runs is a base table with RLS enabled and no policies, so the read-only
-- report role (REPORT_DATABASE_URL) cannot read it directly. This build-layer
-- view runs with its owner's privileges, exposing recent runs plus a derived
-- duration, so the weekly report can show whether ingest is healthy and a stale
-- pipeline becomes visible instead of failing silently in the Actions tab.
create or replace view sync_health as
select
    id,
    started_at,
    finished_at,
    from_date,
    to_date,
    accounts_synced,
    transactions_upserted,
    status,
    error,
    round(extract(epoch from (coalesce(finished_at, now()) - started_at))::numeric, 1)
        as duration_seconds
from sync_runs
order by started_at desc;
