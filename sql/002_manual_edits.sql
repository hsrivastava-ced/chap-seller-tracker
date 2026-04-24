-- 002_manual_edits.sql — relational sellers table + manual-edit preservation
--
-- Depends on: 001_init.sql (creates snapshots/metrics/alerts_log). This
-- migration layers a relational `sellers` table on top of the immutable
-- snapshot history so the dashboard can offer row-level manual edits
-- (fix a typo, flag a seller, annotate a plan) WITHOUT losing those
-- edits on the next scrape.
--
-- Idempotent: re-runnable. Safe to paste on top of an already-migrated db.
--
-- Design (Task #80):
--   - Scraper keeps writing to snapshots (append-only history — unchanged).
--   - Pipeline ALSO calls upsert_sellers_with_guard(...) which inserts/
--     updates this relational table, BUT preserves any row with
--     manually_edited_at IS NOT NULL. For locked rows we only bump
--     last_scraped_at so we can still tell the seller was present in the
--     most recent scrape.
--   - Manual edits go through manual_edits_log (full audit). A trigger
--     on that log bumps sellers.manually_edited_at atomically, so edits
--     and their audit entries are always consistent.
--
-- How to apply:
--   1. Open Supabase SQL editor for project wsbpotyqknjfulzugnxb
--   2. Paste this file and click Run
--   3. Verify tables: sellers, manual_edits_log

-- ---------------------------------------------------------------------
-- sellers — relational projection of the latest scrape, with manual-edit
-- preservation. Columns mirror canonical_schema.json (required + optional
-- for kind=sellers). Unknown columns from pending_review apps land in
-- extra_fields jsonb so we don't have to migrate schema per new app.
-- ---------------------------------------------------------------------
create table if not exists public.sellers (
    app_name              text             not null,
    seller_id             text             not null,
    -- canonical required
    store_url             text,
    email                 text,
    username              text,
    platforms             text,
    installed_on          text,
    -- canonical optional
    action                text,
    app_type              text,
    failed_order_count    integer,
    last_sync             text,
    order_count           integer,
    plan                  text,
    product_count         integer,
    source_country        text,
    steps_completed       text,
    webhooks              text,
    -- pending_review / novel columns
    extra_fields          jsonb,
    -- lifecycle
    first_seen_at         timestamptz      not null default now(),
    last_scraped_at       timestamptz      not null default now(),
    last_scraped_run      text,                         -- run_stamp of the most recent scrape touching this row
    -- manual-edit guard: when non-null, upsert_sellers_with_guard will
    -- only update last_scraped_at/last_scraped_run and leave data fields
    -- alone. Bumped automatically by the trg_manual_edits_touch trigger.
    manually_edited_at    timestamptz,
    primary key (app_name, seller_id)
);
create index if not exists sellers_last_scraped_idx on public.sellers (last_scraped_at desc);
create index if not exists sellers_manually_edited_idx
    on public.sellers (manually_edited_at)
    where manually_edited_at is not null;

-- ---------------------------------------------------------------------
-- manual_edits_log — audit trail for every manual change to sellers.
-- Each row is one (field, old_value → new_value) tuple. The trigger
-- below flips sellers.manually_edited_at to now() whenever a row lands
-- here, so the lock is set atomically with the audit entry.
-- ---------------------------------------------------------------------
create table if not exists public.manual_edits_log (
    id             uuid primary key default gen_random_uuid(),
    edited_at      timestamptz      not null default now(),
    editor_email   text             not null,
    app_name       text             not null,
    seller_id      text             not null,
    field          text             not null,
    old_value      text,
    new_value      text,
    reason         text
);
create index if not exists manual_edits_log_seller_idx
    on public.manual_edits_log (app_name, seller_id, edited_at desc);
create index if not exists manual_edits_log_time_idx
    on public.manual_edits_log (edited_at desc);

-- ---------------------------------------------------------------------
-- Trigger: every manual_edits_log insert bumps sellers.manually_edited_at
-- ---------------------------------------------------------------------
create or replace function public.fn_manual_edits_touch()
returns trigger language plpgsql as $$
begin
    update public.sellers
       set manually_edited_at = greatest(coalesce(manually_edited_at, new.edited_at), new.edited_at)
     where app_name = new.app_name
       and seller_id = new.seller_id;
    return new;
end;
$$;

drop trigger if exists trg_manual_edits_touch on public.manual_edits_log;
create trigger trg_manual_edits_touch
    after insert on public.manual_edits_log
    for each row execute function public.fn_manual_edits_touch();

-- ---------------------------------------------------------------------
-- upsert_sellers_with_guard(rows jsonb, run_stamp text) → integer
--
-- Server-side upsert that preserves manually-edited rows. The CASE in
-- DO UPDATE lets us keep the data fields frozen while still advancing
-- last_scraped_at / last_scraped_run, so "seen in this scrape" stays
-- honest even for locked rows.
--
-- Called from supabase_client.SupabaseClient.upsert_sellers(...)
-- ---------------------------------------------------------------------
create or replace function public.upsert_sellers_with_guard(
    rows jsonb,
    run_stamp text
)
returns integer language plpgsql as $$
declare
    n integer;
begin
    with src as (
        select
            r->>'app_name'                               as app_name,
            r->>'seller_id'                              as seller_id,
            r->>'store_url'                              as store_url,
            r->>'email'                                  as email,
            r->>'username'                               as username,
            r->>'platforms'                              as platforms,
            r->>'installed_on'                           as installed_on,
            r->>'action'                                 as action,
            r->>'app_type'                               as app_type,
            nullif(r->>'failed_order_count','')::int     as failed_order_count,
            r->>'last_sync'                              as last_sync,
            nullif(r->>'order_count','')::int            as order_count,
            r->>'plan'                                   as plan,
            nullif(r->>'product_count','')::int          as product_count,
            r->>'source_country'                         as source_country,
            r->>'steps_completed'                        as steps_completed,
            r->>'webhooks'                               as webhooks,
            case when r ? 'extra_fields' then r->'extra_fields' else null end as extra_fields
        from jsonb_array_elements(rows) as r
        where r ? 'app_name' and r ? 'seller_id'
    ),
    upserted as (
        insert into public.sellers as s (
            app_name, seller_id, store_url, email, username, platforms,
            installed_on, action, app_type, failed_order_count, last_sync,
            order_count, plan, product_count, source_country, steps_completed,
            webhooks, extra_fields, last_scraped_at, last_scraped_run
        )
        select
            app_name, seller_id, store_url, email, username, platforms,
            installed_on, action, app_type, failed_order_count, last_sync,
            order_count, plan, product_count, source_country, steps_completed,
            webhooks, extra_fields, now(), run_stamp
        from src
        on conflict (app_name, seller_id) do update set
            store_url          = case when s.manually_edited_at is null then excluded.store_url          else s.store_url          end,
            email              = case when s.manually_edited_at is null then excluded.email              else s.email              end,
            username           = case when s.manually_edited_at is null then excluded.username           else s.username           end,
            platforms          = case when s.manually_edited_at is null then excluded.platforms          else s.platforms          end,
            installed_on       = case when s.manually_edited_at is null then excluded.installed_on       else s.installed_on       end,
            action             = case when s.manually_edited_at is null then excluded.action             else s.action             end,
            app_type           = case when s.manually_edited_at is null then excluded.app_type           else s.app_type           end,
            failed_order_count = case when s.manually_edited_at is null then excluded.failed_order_count else s.failed_order_count end,
            last_sync          = case when s.manually_edited_at is null then excluded.last_sync          else s.last_sync          end,
            order_count        = case when s.manually_edited_at is null then excluded.order_count        else s.order_count        end,
            plan               = case when s.manually_edited_at is null then excluded.plan               else s.plan               end,
            product_count      = case when s.manually_edited_at is null then excluded.product_count      else s.product_count      end,
            source_country     = case when s.manually_edited_at is null then excluded.source_country     else s.source_country     end,
            steps_completed    = case when s.manually_edited_at is null then excluded.steps_completed    else s.steps_completed    end,
            webhooks           = case when s.manually_edited_at is null then excluded.webhooks           else s.webhooks           end,
            extra_fields       = case when s.manually_edited_at is null then excluded.extra_fields       else s.extra_fields       end,
            -- always advance "seen in this scrape" even for locked rows
            last_scraped_at    = excluded.last_scraped_at,
            last_scraped_run   = excluded.last_scraped_run
        returning 1
    )
    select count(*) into n from upserted;
    return coalesce(n, 0);
end;
$$;

-- ---------------------------------------------------------------------
-- RLS
-- ---------------------------------------------------------------------
alter table public.sellers           enable row level security;
alter table public.manual_edits_log  enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public' and tablename = 'sellers' and policyname = 'sellers_read_anon'
    ) then
        create policy sellers_read_anon on public.sellers
            for select to anon using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public' and tablename = 'manual_edits_log' and policyname = 'manual_edits_log_read_anon'
    ) then
        create policy manual_edits_log_read_anon on public.manual_edits_log
            for select to anon using (true);
    end if;
end$$;
