-- Build layer (ARCHITECTURE §4.4 / §5.3): a category dimension plus the
-- analytical views that every consumer (Power BI, the weekly report, any future
-- solution) reads from. Consumers must never read the raw `transactions` table
-- directly — they read these views, so cleaning/categorisation logic lives in
-- exactly one place.

-- ── Category dimension ───────────────────────────────────────────────────────
-- Keyword → category map. `priority` ascending = first match wins, mirroring the
-- ordered rule engine in categorize.py. This table is a synced PROJECTION of
-- those rules: `invespend init-db` re-derives it from categorize.CATEGORY_RULES
-- (and prunes stale keywords) on every run, so edit categorize.py — not this
-- table. The seed below only bootstraps a brand-new database before the first
-- sync; categorisation re-derives on the next view read, no re-ingest required.
create table if not exists category_map (
    id        bigserial primary key,
    keyword   text not null,
    category  text not null,
    priority  int  not null default 100
);

create unique index if not exists idx_category_map_keyword on category_map (keyword);
create index        if not exists idx_category_map_priority on category_map (priority);

-- Seed once (idempotent: only when the table is empty). Mirrors categorize.py.
insert into category_map (keyword, category, priority)
select keyword, category, priority from (values
    ('woolworths','Groceries',10), ('checkers','Groceries',11), ('pick n pay','Groceries',12),
    ('pnp','Groceries',13), ('spar','Groceries',14), ('food lover','Groceries',15), ('shoprite','Groceries',16),
    ('restaurant','Dining',20), ('uber eats','Dining',21), ('mr d','Dining',22), ('kfc','Dining',23),
    ('nandos','Dining',24), ('steers','Dining',25), ('cafe','Dining',26), ('coffee','Dining',27), ('starbucks','Dining',28),
    ('uber','Transport',30), ('bolt','Transport',31), ('gautrain','Transport',32), ('engen','Transport',33),
    ('shell','Transport',34), ('bp ','Transport',35), ('sasol','Transport',36), ('fuel','Transport',37),
    ('petrol','Transport',38), ('parking','Transport',39),
    ('eskom','Utilities',40), ('city of','Utilities',41), ('municipal','Utilities',42), ('water','Utilities',43),
    ('electricity','Utilities',44), ('prepaid','Utilities',45), ('vodacom','Utilities',46), ('mtn','Utilities',47),
    ('telkom','Utilities',48), ('rain','Utilities',49), ('afrihost','Utilities',50),
    ('netflix','Subscriptions',60), ('spotify','Subscriptions',61), ('youtube','Subscriptions',62),
    ('showmax','Subscriptions',63), ('dstv','Subscriptions',64), ('apple.com','Subscriptions',65),
    ('google','Subscriptions',66), ('microsoft','Subscriptions',67), ('amazon prime','Subscriptions',68),
    ('pharmacy','Health',70), ('clicks','Health',71), ('dis-chem','Health',72), ('dischem','Health',73),
    ('medical','Health',74), ('discovery health','Health',75), ('hospital','Health',76),
    -- Fees before Income: Investec's "FeesAndInterest" type contains "interest".
    ('fee','Fees',80), ('charge','Fees',81), ('service charge','Fees',82), ('admin fee','Fees',83),
    ('salary','Income',90), ('deposit','Income',91), ('interest','Income',92), ('refund','Income',93), ('reversal','Income',94),
    ('transfer','Transfers',100), ('payshap','Transfers',101), ('immediate payment','Transfers',102),
    ('eft','Transfers',103), ('payment to','Transfers',104),
    ('atm','Cash',110), ('cash withdrawal','Cash',111), ('withdrawal','Cash',112),
    ('takealot','Shopping',120), ('amazon','Shopping',121), ('mr price','Shopping',122), ('edgars','Shopping',123),
    ('game','Shopping',124), ('makro','Shopping',125), ('builders','Shopping',126)
) as seed(keyword, category, priority)
where not exists (select 1 from category_map);

-- ── Normalised view ──────────────────────────────────────────────────────────
-- Typed, trimmed, with a single effective date and month bucket to aggregate on.
create or replace view transactions_normalized as
select
    transaction_hash,
    account_id,
    upper(coalesce(type, ''))                                           as type,
    transaction_type,
    status,
    btrim(coalesce(description, ''))                                    as description,
    card_number,
    coalesce(posting_date, value_date, action_date, transaction_date)  as effective_date,
    posting_date,
    value_date,
    amount,
    running_balance,
    date_trunc('month',
        coalesce(posting_date, value_date, action_date, transaction_date)
    )::date                                                            as month,
    raw
from transactions;

-- ── Categorised view ─────────────────────────────────────────────────────────
-- Re-derives category from the category_map dimension (lowest priority match
-- wins), so categorisation is owned by the build layer, not frozen at ingest.
create or replace view transactions_categorized as
select
    n.*,
    coalesce(
        (
            select cm.category
            from category_map cm
            where lower(n.description || ' ' || coalesce(n.transaction_type, ''))
                  like '%' || lower(cm.keyword) || '%'
            order by cm.priority asc
            limit 1
        ),
        'Other'
    ) as category
from transactions_normalized n;

-- ── Aggregations ─────────────────────────────────────────────────────────────
-- Amounts are signed (debits negative); spend = outflow, income = inflow.
create or replace view spend_by_category as
select
    month,
    category,
    sum(case when amount < 0 then -amount else 0 end) as total_spend,
    sum(case when amount > 0 then  amount else 0 end) as total_income,
    count(*)                                          as transactions
from transactions_categorized
group by month, category;

-- Month-over-month movement bridge per category.
create or replace view monthly_movement as
select
    month,
    category,
    total_spend,
    lag(total_spend) over (partition by category order by month)               as prev_month_spend,
    total_spend
        - coalesce(lag(total_spend) over (partition by category order by month), 0) as movement
from spend_by_category;

-- Keep the build-layer objects locked down like the base tables.
alter table category_map enable row level security;
