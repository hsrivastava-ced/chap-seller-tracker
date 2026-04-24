-- 003_seller_profiles.sql — AI-enriched seller business profiles
--
-- Backs `seller_profile_enricher.py`. Each row is the structured
-- output from Claude after analysing a seller's public storefront:
-- business_type, categories, one-sentence insight + opportunity. The
-- enricher hits this table first and only refetches when the cached
-- row is older than 30 days (or the caller passes force=True), so
-- the Claude bill stays predictable.
--
-- Depends on: 001_init.sql (gen_random_uuid() extension), 002_manual_edits.sql.
-- Idempotent: re-runnable.

create table if not exists public.seller_profiles (
    app_name        text        not null,
    seller_id       text        not null,
    store_url       text,
    business_type   text        not null default 'Unknown',
    -- Claude returns an array of 1-4 categories in title case.
    categories      text[]      not null default '{}',
    -- One-sentence strings — we don't expect more than a short
    -- paragraph. Enforced at write-time in Python (500-char cap).
    insight         text        not null default '',
    opportunity     text        not null default '',
    -- Provenance — "claude" | "cache" | "dry_run" | "error". Lets
    -- the admin audit which rows came from the LLM vs. a fallback.
    source          text        not null default 'unknown',
    error           text        not null default '',
    cached_at       timestamptz not null default now(),
    primary key (app_name, seller_id)
);

create index if not exists seller_profiles_cached_at_idx
    on public.seller_profiles (cached_at desc);
create index if not exists seller_profiles_business_type_idx
    on public.seller_profiles (business_type)
    where business_type <> 'Unknown';

-- RLS — anon SELECT (for the dashboard's inline display); writes
-- come from the server-side scrape pipeline via service_role which
-- bypasses RLS.
alter table public.seller_profiles enable row level security;
do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'seller_profiles'
          and policyname = 'seller_profiles_read_anon'
    ) then
        create policy seller_profiles_read_anon on public.seller_profiles
            for select to anon using (true);
    end if;
end$$;
