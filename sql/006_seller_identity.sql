-- 006_seller_identity.sql — canonical seller identity layer
--
-- Purpose: turn the existing per-app `sellers` rows into a graph where
-- the same real-world business/person is recognised across cHAP apps.
-- One human → one `seller_key`. One storefront → one `shop_id`. A shop
-- can be installed on multiple apps; each (shop, app) install stays as
-- a row in the existing `sellers` table, now annotated with its
-- `seller_key` + `shop_id` FKs (option C from the design discussion).
--
-- Depends on: 001_init.sql (gen_random_uuid), 002_manual_edits.sql
-- (`sellers` table this migration extends).
--
-- Idempotent: re-runnable. All CREATEs use IF NOT EXISTS; column adds
-- use ADD COLUMN IF NOT EXISTS; policies are wrapped in DO blocks.
--
-- Scope: cHAP only. Walmart (cedadmin) deliberately not modelled here —
-- when/if we want to merge identities across panels later, we add a
-- `panel` column to `seller_shops` (default 'chap') and broaden the
-- resolver. Schema already supports that direction.

-- ---------------------------------------------------------------------
-- sellers_canonical — one row per real-world business/person
-- ---------------------------------------------------------------------
-- Identity rules:
--   - `primary_email` is the gmail-normalized (dots/+tag stripped),
--     lowercased form of the email cHAP first saw for this seller.
--   - `email_aliases` accumulates any other emails ever seen tied to
--     a shop already linked to this seller. Auto-added by the resolver;
--     every add lands in `seller_identity_log` for audit.
--   - `is_test_store` is derived: TRUE iff every shop under this seller
--     is itself a test store. Updated by the resolver / backfill.
create table if not exists public.sellers_canonical (
    seller_key      uuid        primary key default gen_random_uuid(),
    primary_email   text        not null,
    primary_email_raw text,                                       -- pre-normalization, for audit
    email_aliases   text[]      not null default '{}',
    display_name    text,
    is_test_store   boolean     not null default false,
    notes           text,
    first_seen_at   timestamptz not null default now(),
    last_seen_at    timestamptz not null default now(),
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create unique index if not exists sellers_canonical_primary_email_idx
    on public.sellers_canonical (lower(primary_email));

-- GIN index lets `WHERE 'x@y.com' = ANY(email_aliases)` use the index
-- instead of a scan. Fleet is small today (<1k sellers) but cheap to add.
create index if not exists sellers_canonical_aliases_gin_idx
    on public.sellers_canonical using gin (email_aliases);

create index if not exists sellers_canonical_last_seen_idx
    on public.sellers_canonical (last_seen_at desc);

-- ---------------------------------------------------------------------
-- seller_shops — one row per distinct storefront
-- ---------------------------------------------------------------------
-- `shop_url_canonical` is the normalized identifier:
--   - lowercase, trim
--   - if raw contains whitespace (cHAP's michael panel sometimes returns
--     'www.foo.com bar.myshopify.com'), prefer the `.myshopify.com`
--     token; otherwise the longest non-empty token
--   - strip protocol, www., trailing slash, query string, path
--   - keep `.myshopify.com` suffix (canonical for Shopify stores)
create table if not exists public.seller_shops (
    shop_id              uuid        primary key default gen_random_uuid(),
    seller_key           uuid        not null references public.sellers_canonical(seller_key) on delete restrict,
    shop_url_canonical   text        not null,
    shop_url_raw         text,
    shop_name            text,
    framework            text,                                       -- shopify | woocommerce | prestashop | …
    source_country       text,
    is_test_store        boolean     not null default false,
    panel                text        not null default 'chap',        -- bridge for future cedadmin/walmart merge
    first_seen_at        timestamptz not null default now(),
    last_seen_at         timestamptz not null default now(),
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now()
);

create unique index if not exists seller_shops_url_canonical_idx
    on public.seller_shops (shop_url_canonical, panel);

create index if not exists seller_shops_seller_key_idx
    on public.seller_shops (seller_key);

create index if not exists seller_shops_last_seen_idx
    on public.seller_shops (last_seen_at desc);

-- ---------------------------------------------------------------------
-- seller_identity_log — audit trail for every resolver decision
-- ---------------------------------------------------------------------
-- Every state-changing call from the resolver writes one row here. Lets
-- us replay how the identity graph was built, debug collisions, and
-- satisfy Hrithik's "Auto add new email and make a log" rule.
--
-- event_type values:
--   create_seller          — new sellers_canonical row
--   create_shop            — new seller_shops row
--   add_email_alias        — alias appended to existing seller (collision auto-resolve)
--   collision_blocked      — would-be alias already belongs to a different seller; no change made
--   shop_seller_relink     — a shop's seller_key changed (rare; manual-merge or backfill correction)
--   update_test_store_flag — is_test_store re-evaluated and flipped
create table if not exists public.seller_identity_log (
    log_id          uuid        primary key default gen_random_uuid(),
    event_type      text        not null,
    seller_key      uuid,
    shop_id         uuid,
    app_name        text,
    chap_seller_id  text,
    run_stamp       text,
    old_value       jsonb,
    new_value       jsonb,
    note            text,
    created_at      timestamptz not null default now()
);

create index if not exists seller_identity_log_seller_idx
    on public.seller_identity_log (seller_key, created_at desc);

create index if not exists seller_identity_log_event_idx
    on public.seller_identity_log (event_type, created_at desc);

create index if not exists seller_identity_log_run_idx
    on public.seller_identity_log (run_stamp);

-- ---------------------------------------------------------------------
-- Extend existing `sellers` (per-app install rows) with identity FKs
-- ---------------------------------------------------------------------
-- Nullable for now — backfill fills them, then ongoing scrapes populate
-- via the resolver. Foreign keys with ON DELETE SET NULL so identity
-- table changes never cascade-delete install rows (those are scrape
-- history and shouldn't be removed by an identity reshuffle).
alter table public.sellers
    add column if not exists seller_key uuid
        references public.sellers_canonical(seller_key) on delete set null;

alter table public.sellers
    add column if not exists shop_id uuid
        references public.seller_shops(shop_id) on delete set null;

create index if not exists sellers_seller_key_idx on public.sellers (seller_key);
create index if not exists sellers_shop_id_idx   on public.sellers (shop_id);

-- ---------------------------------------------------------------------
-- updated_at touch triggers — keep `updated_at` honest without relying
-- on every writer to set it. Cheap and standard.
-- ---------------------------------------------------------------------
create or replace function public.fn_touch_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at := now();
    return new;
end$$;

do $$
begin
    if not exists (select 1 from pg_trigger where tgname = 'trg_sellers_canonical_touch') then
        create trigger trg_sellers_canonical_touch
            before update on public.sellers_canonical
            for each row execute function public.fn_touch_updated_at();
    end if;
    if not exists (select 1 from pg_trigger where tgname = 'trg_seller_shops_touch') then
        create trigger trg_seller_shops_touch
            before update on public.seller_shops
            for each row execute function public.fn_touch_updated_at();
    end if;
end$$;

-- ---------------------------------------------------------------------
-- RLS — anon SELECT (dashboard reads). Writes are service_role only,
-- which bypasses RLS, matching every other table in this project.
-- ---------------------------------------------------------------------
alter table public.sellers_canonical    enable row level security;
alter table public.seller_shops         enable row level security;
alter table public.seller_identity_log  enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public' and tablename = 'sellers_canonical'
          and policyname = 'sellers_canonical_read_anon'
    ) then
        create policy sellers_canonical_read_anon on public.sellers_canonical
            for select to anon using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public' and tablename = 'seller_shops'
          and policyname = 'seller_shops_read_anon'
    ) then
        create policy seller_shops_read_anon on public.seller_shops
            for select to anon using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public' and tablename = 'seller_identity_log'
          and policyname = 'seller_identity_log_read_anon'
    ) then
        create policy seller_identity_log_read_anon on public.seller_identity_log
            for select to anon using (true);
    end if;
end$$;
