-- Email-payment-approval state in Postgres (OQ2 opt-in backend).
-- ADDITIVE and idempotent: applies cleanly after 0001-0008, touches no existing
-- table or view. Lets a Railway deploy keep pending records, the daily-aggregate
-- cap, and the audit trail without a persistent volume.
--
-- Column set mirrors the file store's minimal-field allowlist (PR2/F20): no full
-- account number / PAN, no secret columns. Only last-3 or a hash is ever stored.

create table if not exists payment_pending (
    dedup_key             text primary key,
    status                text not null,            -- pending | executed | burned | parked
    amount                numeric(18,2) not null,
    currency              text,
    beneficiary_id        text,
    source_account_id     text,                     -- opaque Investec id, NOT a PAN
    source_account_last3  text,
    message_id_hash       text,
    nonce                 text,
    created_at            timestamptz,
    executed_at           timestamptz,
    execution_mode        text                      -- dry-run | live
);

create index if not exists payment_pending_status_idx on payment_pending (status);

create table if not exists payment_daily_total (
    day    date primary key,
    total  numeric(18,2) not null default 0
);

create table if not exists payment_audit (
    id      bigserial primary key,
    ts      timestamptz not null,
    step    text not null,
    detail  jsonb
);

create index if not exists payment_audit_ts_idx on payment_audit (ts);
