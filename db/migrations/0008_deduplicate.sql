-- Remove duplicate rows caused by mutable date fields (valueDate, actionDate)
-- changing between ingest runs as transactions settle.
--
-- When the Investec API first returns a pending transaction it may supply
-- actionDate=null; after settlement the same call returns actionDate="2026-06-15".
-- The old hash formula produced "None" for null and was therefore different from
-- the settled hash, inserting a second row rather than updating the first.
--
-- Dedup key: (account_id, amount, description, running_balance, effective_date).
-- Two rows that match on all five fields are the same bank event; keep the most
-- recently ingested one (latest API snapshot) and delete the rest.
-- Only rows with a non-null running_balance are deduplicated — pending rows
-- without a balance anchor are left alone to avoid collapsing genuinely distinct
-- same-day same-amount pending charges.
--
-- Idempotent: a re-run finds no groups with rn > 1 and deletes nothing.

delete from transactions
where transaction_hash in (
    select transaction_hash from (
        select t.transaction_hash,
               row_number() over (
                   partition by
                       t.account_id,
                       t.amount,
                       t.description,
                       t.running_balance,
                       coalesce(t.transaction_date, t.action_date, t.value_date, t.posting_date)
                   order by t.ingested_at desc
               ) as rn
        from transactions t
        where t.running_balance is not null
    ) sub
    where rn > 1
);
