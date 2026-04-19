-- 001_init.sql — base schema for the CedCommerce cHAP Seller Tracker
--
-- Intent: this file is idempotent. You can run it once during initial
-- setup, or re-run it against an existing Supabase database without
-- blowing away data. All statements use IF NOT EXISTS / DO $$ blocks so
-- partial re-runs are safe.
--
-- How to apply:
--   1. Open Supabase SQL editor for project wsbpotyqknjfulzugnxb
--   2. Paste this file and click Run
--   3. Verify tables in Table Editor (snapshots, metrics, alerts_log)
--
-- RLS NOTE: Supabase enables RLS by default on every new table. The key
-- currently in .env is a `sb_publishable_*` value, which maps to the
-- `anon` role. Anon cannot write to these tables unless we either:
--   (a) add a permissive INSERT policy for anon, or
--   (b) swap SUPABASE_KEY for a service_role key (recommended for a
--       server-side ingestion pipeline — never ship this key to a UI).
-- Policies at the bottom of this file assume (b) and only grant SELECT
-- to anon for the dashboard to read.

-- ---------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------

create table if not exists public.snapshots (
    id            uuid primary key default gen_random_uuid(),
    created_at    timestamptz      not null default now(),
    run_stamp     text             not null,           -- e.g. "2026-04-18_15-03-53Z"
    app_name      text             not null,           -- shopify_temu | shein | shopify_temu_eu
    kind          text             not null,           -- 'sellers' | 'uninstalls'
    row_count     integer          not null default 0,
    raw_data      jsonb            not null,           -- list of row dicts from scraper
    status        text             not null default 'success',
    notes         text
);
create index if not exists snapshots_app_kind_created_idx
    on public.snapshots (app_name, kind, created_at desc);
create index if not exists snapshots_run_stamp_idx
    on public.snapshots (run_stamp);

create table if not exists public.metrics (
    id                    uuid primary key default gen_random_uuid(),
    computed_at           timestamptz      not null default now(),
    run_stamp             text             not null,
    app_name              text,                         -- null = "all apps"
    metric_name           text             not null,    -- e.g. 'total_active', 'new_installs', 'churned'
    value                 numeric          not null,
    delta_from_previous   numeric,
    meta                  jsonb                         -- optional extras (e.g. list of seller_ids)
);
create index if not exists metrics_name_time_idx
    on public.metrics (metric_name, computed_at desc);
create index if not exists metrics_app_time_idx
    on public.metrics (app_name, computed_at desc);

create table if not exists public.alerts_log (
    id            uuid primary key default gen_random_uuid(),
    triggered_at  timestamptz      not null default now(),
    run_stamp     text,
    app_name      text,
    alert_type    text             not null,           -- 'scrape_failure' | 'anomaly' | 'new_install' | ...
    severity      text             not null default 'info',   -- info | warn | error
    message       text             not null,
    meta          jsonb
);
create index if not exists alerts_log_time_idx
    on public.alerts_log (triggered_at desc);
create index if not exists alerts_log_type_time_idx
    on public.alerts_log (alert_type, triggered_at desc);

-- ---------------------------------------------------------------------
-- RLS
-- ---------------------------------------------------------------------
-- Enable RLS and (as above) assume writes come from a service_role key
-- (which bypasses RLS entirely). Readers get SELECT via anon.

alter table public.snapshots   enable row level security;
alter table public.metrics     enable row level security;
alter table public.alerts_log  enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public' and tablename = 'snapshots' and policyname = 'snapshots_read_anon'
    ) then
        create policy snapshots_read_anon on public.snapshots
            for select to anon using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public' and tablename = 'metrics' and policyname = 'metrics_read_anon'
    ) then
        create policy metrics_read_anon on public.metrics
            for select to anon using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public' and tablename = 'alerts_log' and policyname = 'alerts_log_read_anon'
    ) then
        create policy alerts_log_read_anon on public.alerts_log
            for select to anon using (true);
    end if;
end$$;

-- If you want to keep using the anon key for ingestion too (NOT recommended
-- for a long-running scraper, but OK for a first smoke test), uncomment:
--
-- do $$
-- begin
--     if not exists (
--         select 1 from pg_policies
--         where schemaname = 'public' and tablename = 'snapshots' and policyname = 'snapshots_write_anon'
--     ) then
--         create policy snapshots_write_anon on public.snapshots
--             for insert to anon with check (true);
--     end if;
-- end$$;
