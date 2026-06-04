-- Correct the "effective date" precedence (supersedes the definition in 0002).
--
-- Investec's `postingDate` is when the BANK BOOKED the item, which can lag the
-- real transaction by months (a backlog of delayed postings can all land on one
-- day). Bucketing/windowing on posting_date therefore lumps years of history into
-- whatever day the batch posted. The economically meaningful date is when the
-- transaction actually happened, so effective_date (and the month bucket derived
-- from it) now prefer transaction/action/value dates, falling back to posting_date
-- only when none of those is present.
--
-- This re-derives every downstream view (categorised, flow, spend_by_category,
-- monthly_movement, monthly_flows) on the next read — no re-ingest required.
create or replace view transactions_normalized as
select
    transaction_hash,
    account_id,
    upper(coalesce(type, ''))                                              as type,
    transaction_type,
    status,
    btrim(coalesce(description, ''))                                       as description,
    card_number,
    coalesce(transaction_date, action_date, value_date, posting_date)      as effective_date,
    posting_date,
    value_date,
    amount,
    running_balance,
    date_trunc('month',
        coalesce(transaction_date, action_date, value_date, posting_date)
    )::date                                                               as month,
    raw
from transactions;
