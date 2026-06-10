-- Flow classification v2: fix a Tier-B false positive and expose account fields.
--
-- 1. Tier B previously labelled a transaction `internal_transfer` whenever the
--    description contained an own account-holder name. But salary and other
--    third-party payments routinely embed the holder's name ("SALARY J P VAN
--    ZYL"), so real external income was silently misclassified and excluded
--    from every spend/income figure. Tier B now also requires transfer
--    context: the Investec transaction type is 'Transfers', or the description
--    carries a transfer keyword. Genuine inter-account transfers always have
--    one of those ("Transfer to/from ..."); a salary EFT has neither.
--
-- 2. The view now exposes account_number / account_name (it already joined
--    accounts internally). Consumers — in particular the read-only report
--    role, which RLS correctly blocks from the base accounts table — no longer
--    need to join accounts themselves. Columns are appended at the end, which
--    CREATE OR REPLACE VIEW permits.
--
-- Build-layer change only: history is re-classified on the next read.

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

        -- Tier B: the description names one of our own account holders AND the
        -- transaction has transfer context (type or keyword). The context guard
        -- keeps salary/third-party payments that mention the holder's name
        -- external; the length guard avoids matching very short reference names.
        when (c.transaction_type = 'Transfers'
              or c.description ~* '\m(transfer|trf|payshap)')
         and exists (
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
        when (c.transaction_type = 'Transfers'
              or c.description ~* '\m(transfer|trf|payshap)')
         and exists (
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
    ) as counterparty_account_id,

    a.account_number,
    a.account_name
from transactions_categorized c
left join accounts a on a.account_id = c.account_id;
