-- Harden the internal-transfer classifier's Tier-A signal (supersedes the
-- transactions_flow definition in 0005_flow.sql).
--
-- Tier A flags a transaction as an internal transfer when one of our OTHER own
-- account numbers appears in its description — the strongest, direction-unambiguous
-- signal, and the only one that fires from a SINGLE leg (so a transfer to a
-- newly-opened own account is caught immediately, before that account's matching
-- credit leg has been ingested for Tier C to pair against).
--
-- The 0005 match was an exact substring (`description like '%' || number || '%'`).
-- Investec writes account numbers into transfer text with inconsistent grouping
-- ("Transfer to ... 1001 0900 709" vs the stored "10010900709"), so the exact
-- match silently misses and the transfer is scored as external spend. We add a
-- digit-normalised comparison — strip every non-digit from BOTH sides, then
-- substring-match — alongside the original (so this is strictly additive: anything
-- the exact match caught still matches). The length(>=8) guard on own_numbers is
-- retained, making a coincidental digit-blob collision negligible.
--
-- Build-layer view: re-classifies history on the next read, no re-ingest required.

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
        -- Tier A: the description embeds one of our OTHER own account numbers,
        -- matched both verbatim and digit-normalised (so grouped/spaced numbers
        -- in the transfer text still match the stored, unformatted number).
        when exists (
            select 1 from own_numbers o
            where o.account_number <> coalesce(a.account_number, '')
              and (
                  c.description like '%' || o.account_number || '%'
                  or regexp_replace(coalesce(c.description, ''), '[^0-9]', '', 'g')
                     like '%' || regexp_replace(o.account_number, '[^0-9]', '', 'g') || '%'
              )
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
              and (
                  c.description like '%' || o.account_number || '%'
                  or regexp_replace(coalesce(c.description, ''), '[^0-9]', '', 'g')
                     like '%' || regexp_replace(o.account_number, '[^0-9]', '', 'g') || '%'
              )
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
          and (
              c.description like '%' || acc.account_number || '%'
              or regexp_replace(coalesce(c.description, ''), '[^0-9]', '', 'g')
                 like '%' || regexp_replace(acc.account_number, '[^0-9]', '', 'g') || '%'
          )
        limit 1
    ) as counterparty_account_id
from transactions_categorized c
left join accounts a on a.account_id = c.account_id;
