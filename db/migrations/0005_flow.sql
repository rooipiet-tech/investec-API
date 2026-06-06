-- Internal-transfer vs external-flow classification (ARCHITECTURE §4.4 / §5.3).
--
-- The `accounts` table is the authoritative list of *internal* (owned) accounts:
-- anything not in it is external. Because ingest only ever pulls transactions for
-- accounts we own, a genuine internal transfer is stored TWICE (a debit leg on the
-- source account and an equal-and-opposite credit leg on the destination account),
-- while an external payment appears only once. We use that, plus the counterparty
-- identifiers Investec writes into the description, to label every transaction.
--
-- This is a build-layer view: editing the rules re-classifies history on the next
-- read, no re-ingest required. Consumers must exclude `internal_transfer` from
-- spend/income aggregates so money merely moving between your own pockets is not
-- double-counted as both an outflow and an inflow.

-- Supports the Tier-C matched-pair lookup (equal-and-opposite leg on the same
-- value date); without it the self-join degrades to a sequential scan per row.
create index if not exists idx_tx_value_amount on transactions (value_date, amount);

create or replace view transactions_flow as
with own_numbers as (
    select distinct account_number
    from accounts
    where account_number is not null and length(account_number) >= 8
),
own_names as (
    select distinct lower(account_name)   as nm from accounts where account_name   is not null
    union
    select distinct lower(reference_name) as nm from accounts where reference_name is not null
)
select
    c.*,
    -- Layered classifier, highest-confidence signal first.
    case
        -- Tier A: the description embeds one of our OTHER own account numbers
        -- (Investec writes the destination account number into transfer text, e.g.
        --  "Transfer to Card Internet ... 10010900709 Mr J P Van Zyl 10010900709").
        when exists (
            select 1 from own_numbers o
            where o.account_number <> coalesce(a.account_number, '')
              and c.description like '%' || o.account_number || '%'
        ) then 'internal_transfer'

        -- Tier B: the description names one of our own account holders. The length
        -- guard avoids matching very short reference names against unrelated text.
        when exists (
            select 1 from own_names nm
            where length(nm.nm) > 6
              and c.description ilike '%' || nm.nm || '%'
        ) then 'internal_transfer'

        -- Tier C: a guarded matched pair — an equal-and-opposite leg on a DIFFERENT
        -- owned account on the same value date. Fee/charge/refund/reversal rows are
        -- excluded on both legs because identical month-end bank fees (and duplicate
        -- merchant refunds) across accounts otherwise masquerade as transfers.
        when c.description !~* '(fee|charge|reversal|refund)'
         and exists (
            select 1 from transactions m
            where m.account_id <> c.account_id
              and m.value_date = c.value_date
              and m.amount     = -c.amount
              and coalesce(m.description, '') !~* '(fee|charge|reversal|refund)'
         ) then 'internal_transfer'

        when c.amount > 0 then 'external_inflow'
        else 'external_outflow'
    end as flow_type,

    -- Which signal fired (for auditing / tuning the rules).
    case
        when exists (
            select 1 from own_numbers o
            where o.account_number <> coalesce(a.account_number, '')
              and c.description like '%' || o.account_number || '%'
        ) then 'counterparty_account_number'
        when exists (
            select 1 from own_names nm
            where length(nm.nm) > 6 and c.description ilike '%' || nm.nm || '%'
        ) then 'counterparty_name'
        when c.description !~* '(fee|charge|reversal|refund)'
         and exists (
            select 1 from transactions m
            where m.account_id <> c.account_id
              and m.value_date = c.value_date
              and m.amount = -c.amount
              and coalesce(m.description, '') !~* '(fee|charge|reversal|refund)'
         ) then 'matched_pair'
        else null
    end as flow_signal,

    -- Best-effort counterparty: the owned account whose number appears in the text
    -- (Tier A only — the one case where direction is unambiguous).
    (
        select acc.account_id
        from accounts acc
        where acc.account_number is not null
          and length(acc.account_number) >= 8
          and acc.account_number <> coalesce(a.account_number, '')
          and c.description like '%' || acc.account_number || '%'
        limit 1
    ) as counterparty_account_id
from transactions_categorized c
left join accounts a on a.account_id = c.account_id;

-- ── Aggregations now exclude internal transfers ──────────────────────────────
-- Spend = external outflow, income = external inflow. Internal transfers are money
-- moving between owned accounts and must not count as either (replaces the 0002
-- definition, keeping the same output columns so monthly_movement still resolves).
create or replace view spend_by_category as
select
    month,
    category,
    sum(case when amount < 0 then -amount else 0 end) as total_spend,
    sum(case when amount > 0 then  amount else 0 end) as total_income,
    count(*)                                          as transactions
from transactions_flow
where flow_type <> 'internal_transfer'
group by month, category;

-- Headline view of the question this migration answers: per month, how much money
-- flowed in/out to the outside world vs merely shuffled between your own accounts.
create or replace view monthly_flows as
select
    month,
    sum(case when flow_type = 'external_inflow'    then amount else 0 end)  as external_inflow,
    sum(case when flow_type = 'external_outflow'   then -amount else 0 end) as external_outflow,
    sum(case when flow_type = 'internal_transfer' and amount > 0 then amount else 0 end) as internal_transfer_volume,
    count(*) filter (where flow_type = 'internal_transfer') as internal_transfer_legs,
    count(*) filter (where flow_type like 'external_%')      as external_txns
from transactions_flow
group by month;
