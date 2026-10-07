-- Email-payment v2 (batch approval): instruction state, beneficiary first-seen memory, message identity, small meta.
-- ADDITIVE and RE-RUN SAFE: init_db re-applies every migration on every run under a 15s lock_timeout, so every statement
-- here is a no-op the second time and takes no AccessExclusive/ShareLock on an already-configured table:
--   * create table if not exists (a no-op catalog check on re-run);
--   * indexes, row level security and the deny_all policy ONLY inside guarded do-blocks
--     (create index if not exists still takes a ShareLock before noticing the index exists);
--   * no views, no drops, no unconditional alter table.
-- No full account number, PAN, token, image data or email body column (only last-3, keyed HMACs and opaque ids).
-- Statuses (12) mirror payments/instructions.py TRANSITIONS; the plan section 2a/2b is the single source.

create table if not exists payment_instruction (
    instruction_id        text primary key,            -- refs.instruction_id_for(Message-ID, From) hex; NEVER derived from extracted fields
    status                text not null check (status in (
        'awaiting_approval','awaiting_beneficiary','held','accepted','submitting',
        'executed','failed','needs_review','needs_authorisation','cancelled','expired','parked')),
    path                  text not null check (path in ('registered','new_payee')),
    amount                numeric(18,2) not null,
    currency              text not null default 'ZAR',
    source_account_id     text not null,               -- opaque Investec id
    source_profile_id     text,
    source_account_last3  text not null,
    payee_name_norm       text,                        -- normalised name for exact re-resolution (third-party data, minimised)
    beneficiary_id        text,                        -- null until registered
    beneficiary_fingerprint text,                      -- keyed HMAC over name|accountNumber|code of the RAW beneficiary; not reversible without the key
    account_hmac          text,                        -- keyed HMAC of the account number extracted from mail/image (null if none); never the number
    recent_beneficiary    boolean not null default false,
    daily_reserved        boolean not null default false, -- amount currently counted in payment_daily_total for this instruction
    reserved_day          date,                        -- the SAST day whose payment_daily_total row holds the reservation
    my_reference          text,
    their_reference       text,
    figures_source        text not null check (figures_source in ('typed','attachment','image')),
    figures_excerpt       text,                        -- sanitised <= 80 char excerpt of the line the amount was read from (display only, set once at create, NOT bound by offer_digest)
    message_id_hash       text not null,
    notify_to             text not null,               -- allowlisted auth_from of the creating message; every email about the row goes here
    received_at           timestamptz not null,        -- trusted receipt time (older of INTERNALDATE / topmost Received), NOT the Date header
    first_seen_at         timestamptz,                 -- OUR first observation of the beneficiary, set once
    eligible_at           timestamptz,                 -- batch-eligible time = first_seen_at + hold (hold default 0h), set once
    batch_ref             text,                        -- set by offer_batch together with item_no, offered_at, offer_digest
    item_no               integer check (item_no between 1 and 999),
    offered_at            timestamptz,                 -- the approval window starts here
    batch_notified_at     timestamptz,                 -- set once, after the batch email was sent (all rows of the batch)
    offer_digest          text,                        -- canonical sha256 of what the batch email showed (instructions.offer_digest)
    approved_at           timestamptz,                 -- set once by approve_item: the ONLY way into 'accepted'
    expires_at            timestamptz not null,
    paste_notified_at     timestamptz,
    hold_notified_at      timestamptz,
    executed_at           timestamptz,
    execution_mode        text,                        -- dry-run | live
    outcome_code          text,                        -- short code only
    outcome_message       text,                        -- SANITISED provider message (<= 200 chars), failed/needs_authorisation only
    updated_at            timestamptz not null,
    check ((batch_ref is null) = (item_no is null) and (batch_ref is null) = (offered_at is null) and (batch_ref is null) = (offer_digest is null)),
    check (status not in ('accepted','submitting','executed','failed','needs_review','needs_authorisation')
           or (approved_at is not null and batch_ref is not null))
);

do $$
begin
    if to_regclass('payment_instruction_status_idx') is null then
        create index payment_instruction_status_idx on payment_instruction (status);
    end if;
    if to_regclass('payment_instruction_batch_item_idx') is null then
        create unique index payment_instruction_batch_item_idx on payment_instruction (batch_ref, item_no) where batch_ref is not null;
    end if;
end $$;

create table if not exists payment_beneficiary_seen (    -- NEVER cleaned up: the memory of "first seen" and of the last fingerprint
    beneficiary_id          text primary key,
    first_seen_at           timestamptz not null,        -- insert ... on conflict do nothing: never reset
    established             boolean not null default false, -- true for beneficiaries present when v2 was first enabled
    last_fingerprint        text,
    fingerprint_changed_at  timestamptz                  -- set when last_fingerprint changes; does NOT touch first_seen_at
);

create table if not exists payment_message_seen (        -- one row per fetched message, EVERY outcome
    instruction_id    text primary key,
    outcome           text not null,                     -- 'processing' on insert, then a short code
    seen_at           timestamptz not null,
    auth_from         text,                              -- written ONLY after authenticate_sender ok; the only address a resend notice may go to
    resend_notified_at timestamptz                       -- set once when the stuck-message sweep sent the resend notice
);

create table if not exists payment_v2_meta (              -- bootstrap marker and small durable markers; never cleaned up
    key     text primary key,
    value   text not null,
    set_at  timestamptz not null
);

do $$
begin
    if not (select relrowsecurity from pg_class where oid = to_regclass('payment_instruction')) then
        alter table payment_instruction enable row level security;
    end if;
    if not exists (select 1 from pg_policies
                   where schemaname = current_schema() and tablename = 'payment_instruction' and policyname = 'deny_all') then
        create policy deny_all on payment_instruction for all to public using (false) with check (false);
    end if;
end $$;

do $$
begin
    if not (select relrowsecurity from pg_class where oid = to_regclass('payment_beneficiary_seen')) then
        alter table payment_beneficiary_seen enable row level security;
    end if;
    if not exists (select 1 from pg_policies
                   where schemaname = current_schema() and tablename = 'payment_beneficiary_seen' and policyname = 'deny_all') then
        create policy deny_all on payment_beneficiary_seen for all to public using (false) with check (false);
    end if;
end $$;

do $$
begin
    if not (select relrowsecurity from pg_class where oid = to_regclass('payment_message_seen')) then
        alter table payment_message_seen enable row level security;
    end if;
    if not exists (select 1 from pg_policies
                   where schemaname = current_schema() and tablename = 'payment_message_seen' and policyname = 'deny_all') then
        create policy deny_all on payment_message_seen for all to public using (false) with check (false);
    end if;
end $$;

do $$
begin
    if not (select relrowsecurity from pg_class where oid = to_regclass('payment_v2_meta')) then
        alter table payment_v2_meta enable row level security;
    end if;
    if not exists (select 1 from pg_policies
                   where schemaname = current_schema() and tablename = 'payment_v2_meta' and policyname = 'deny_all') then
        create policy deny_all on payment_v2_meta for all to public using (false) with check (false);
    end if;
end $$;
