-- Beneficiary matching: label outgoing payments with the registered beneficiary.
--
-- Additive only. Two NEW tables and one NEW view; no existing table or view is
-- altered, dropped or replaced. Safe to re-run on every ingest (init_db applies
-- all migrations each run): `if not exists`, drop-then-create policies, and
-- `create or replace view` on the new view only. No data is written here.
--
-- beneficiaries        snapshot of the Investec beneficiary list (PII allowlist:
--                      no cell number, email or raw payload). Soft-retired via
--                      `active = false`, never hard-deleted. account_number holds
--                      the FULL number (approved); protected by deny_all RLS.
-- beneficiary_matches  one row per transaction_hash. Deliberately NO foreign key
--                      to transactions: 0008 deletes and backfill-hashes re-keys
--                      transaction_hash, so orphaned rows are simply invisible
--                      through the inner join in the view below.

create table if not exists beneficiaries (
    beneficiary_id    text primary key,
    beneficiary_name  text,
    name              text,
    reference_name    text,
    bank              text,
    branch_code       text,
    account_number    text,
    first_seen_at     timestamptz not null default now(),
    last_seen_at      timestamptz not null,
    active            boolean not null default true
);

create table if not exists beneficiary_matches (
    transaction_hash  text primary key,
    status            text not null check (status in ('matched', 'ambiguous', 'no_candidate')),
    beneficiary_id    text,
    match_rule        text not null,
    matched_token     text not null,
    candidate_count   integer not null,
    snapshot_at       timestamptz not null,
    matched_at        timestamptz not null default now(),
    constraint beneficiary_matches_matched_has_id
        check ((status = 'matched') = (beneficiary_id is not null))
);

-- RLS: same deny_all pattern as 0011, applied to the new tables only.
alter table beneficiaries enable row level security;
drop policy if exists deny_all on beneficiaries;
create policy deny_all on beneficiaries for all to public using (false) with check (false);

alter table beneficiary_matches enable row level security;
drop policy if exists deny_all on beneficiary_matches;
create policy deny_all on beneficiary_matches for all to public using (false) with check (false);

-- Read surface, beside transactions_flow (which it does not replace).
-- NOTE: this view's column list is APPEND-ONLY. `create or replace view` cannot
-- reorder, rename or drop columns, so any future change may only add columns at
-- the end; otherwise this migration would fail on every subsequent ingest.
create or replace view transactions_beneficiary with (security_invoker = on) as
select
    f.transaction_hash,
    f.account_id,
    f.effective_date,
    f.description,
    f.amount,
    f.type,
    f.transaction_type,
    f.flow_type,
    m.status            as match_status,
    m.beneficiary_id,
    b.beneficiary_name,
    b.name              as beneficiary_alt_name,
    b.reference_name    as beneficiary_reference,
    b.bank              as beneficiary_bank,
    b.branch_code       as beneficiary_branch_code,
    b.account_number    as beneficiary_account_number,
    b.active            as beneficiary_active,
    m.match_rule,
    m.matched_token,
    m.candidate_count,
    m.snapshot_at,
    m.matched_at
from beneficiary_matches m
join transactions_flow f on f.transaction_hash = m.transaction_hash
left join beneficiaries b on b.beneficiary_id = m.beneficiary_id
where f.flow_type is distinct from 'internal_transfer'
  and not exists (
      select 1 from accounts a
      where length(regexp_replace(coalesce(a.account_number, ''), '[^0-9]', '', 'g')) >= 8
        and regexp_replace(a.account_number, '[^0-9]', '', 'g')
          = regexp_replace(coalesce(b.account_number, ''), '[^0-9]', '', 'g')
  );
