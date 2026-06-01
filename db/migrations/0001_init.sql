-- Investec Programmable Banking ingestion schema
-- Apply via: invespend init-db   (or paste into the Supabase SQL editor)

create table if not exists accounts (
    account_id      text primary key,
    account_number  text,
    account_name    text,
    reference_name  text,
    product_name    text,
    kyc_compliant   boolean,
    profile_id      text,
    first_seen_at   timestamptz not null default now(),
    last_synced_at  timestamptz
);

create table if not exists transactions (
    -- Deterministic dedup key: the public API exposes no stable transaction id.
    transaction_hash  text primary key,
    account_id        text not null references accounts(account_id),
    type              text,            -- DEBIT / CREDIT
    transaction_type  text,            -- e.g. CardPurchases, FeesAndInterest
    status            text,
    description       text,
    card_number       text,
    posting_date      date,
    value_date        date,
    action_date       date,
    transaction_date  date,
    amount            numeric(18,2) not null,   -- signed: debits negative
    running_balance   numeric(18,2),
    category          text,            -- derived by the rule engine
    raw               jsonb not null,  -- original payload, never lose fidelity
    ingested_at       timestamptz not null default now()
);

create index if not exists idx_tx_account_date on transactions (account_id, posting_date);
create index if not exists idx_tx_posting_date on transactions (posting_date);
create index if not exists idx_tx_category     on transactions (category);

-- Ingest audit log for observability.
create table if not exists sync_runs (
    id                     bigserial primary key,
    started_at             timestamptz not null default now(),
    finished_at            timestamptz,
    from_date              date,
    to_date                date,
    accounts_synced        int default 0,
    transactions_upserted  int default 0,
    status                 text,        -- running / success / error
    error                  text
);

-- ── Security ────────────────────────────────────────────────────────────────
-- Lock these tables down. With RLS enabled and NO policies, the Supabase anon /
-- authenticated roles (i.e. anything using the public API key) cannot read this
-- banking data. The ingest/report jobs connect with the direct Postgres role,
-- which bypasses RLS, so they are unaffected. Add explicit policies later only
-- if you intentionally expose this data to a client app.
alter table accounts     enable row level security;
alter table transactions enable row level security;
alter table sync_runs    enable row level security;
