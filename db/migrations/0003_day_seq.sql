-- Align the dedup key with ARCHITECTURE §5.2. The transaction_hash now folds in
-- a within-day sequence counter (day_seq) so genuinely identical same-day
-- transactions (e.g. two R45 coffees) are kept as distinct rows instead of
-- relying on running_balance, which can be null on pending rows.
alter table transactions add column if not exists day_seq int not null default 0;
