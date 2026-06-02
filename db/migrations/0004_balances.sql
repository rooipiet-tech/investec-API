-- Daily balance snapshots (ARCHITECTURE §4.1: getAccountBalance).
--
-- The transactions feed is the line-by-line statement; this captures the single
-- "balance right now" figure each day so we can (a) chart balance over time and
-- (b) reconcile the bank-reported balance against the running balance on the
-- latest stored transaction.
create table if not exists balances (
    id                 bigserial primary key,
    account_id         text not null references accounts(account_id),
    as_of_date         date not null default current_date,
    captured_at        timestamptz not null default now(),
    current_balance    numeric(18,2),
    available_balance  numeric(18,2),
    budget_balance     numeric(18,2),
    straight_balance   numeric(18,2),
    cash_balance       numeric(18,2),
    currency           text default 'ZAR',
    raw                jsonb not null,
    -- One snapshot per account per day; a same-day re-run refreshes it.
    unique (account_id, as_of_date)
);

create index if not exists idx_balances_account_date on balances (account_id, as_of_date);

-- Reconciliation: bank-reported balance vs the running balance on the most
-- recent stored transaction. `reconciled` is true when they agree to the cent.
create or replace view balance_reconciliation as
with latest_balance as (
    select distinct on (account_id)
        account_id, as_of_date, current_balance, available_balance, captured_at
    from balances
    order by account_id, as_of_date desc, captured_at desc
),
latest_txn as (
    select distinct on (account_id)
        account_id, posting_date, running_balance
    from transactions
    where running_balance is not null
    order by account_id, posting_date desc, ingested_at desc
)
select
    b.account_id,
    b.as_of_date,
    b.current_balance                                          as api_current_balance,
    b.available_balance                                        as api_available_balance,
    t.posting_date                                             as latest_txn_date,
    t.running_balance                                          as latest_txn_running_balance,
    b.current_balance - t.running_balance                      as difference,
    abs(coalesce(b.current_balance - t.running_balance, 0)) < 0.01 as reconciled
from latest_balance b
left join latest_txn t using (account_id);

alter table balances enable row level security;
