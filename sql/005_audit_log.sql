-- 005_audit_log.sql — login + page-view + action audit trail
--
-- Goal: super_admin (Hrithik) can answer three questions on demand:
--   1. Who is signed in right now?       → public.active_sessions
--   2. Who logged in recently?           → public.login_log
--   3. What did <user> do today?         → public.activity_log
--
-- Design:
--   - login_log is append-only. Every successful sign-in writes one
--     row with (email, when, ip, user-agent). Failed-login attempts
--     could be added later — not in this iteration.
--   - activity_log is append-only. Insert on meaningful actions:
--     page navigation, CSV download, manual edit, role grant/revoke,
--     scrape-on-demand dispatch. Streamlit re-renders DO NOT each
--     write a row — `audit.log_action` throttles via session_state
--     (only when page actually changes).
--   - active_sessions is a single-row-per-(email, console) UPSERT
--     table. Every page render bumps last_seen_at. "Active right
--     now" = rows with last_seen_at > now() - 5 min.
--
-- Idempotent: re-runnable. Safe to paste on top of an already-migrated db.
--
-- How to apply:
--   1. Open Supabase SQL editor for project wsbpotyqknjfulzugnxb
--   2. Paste this file and click Run
--   3. Verify tables: login_log, activity_log, active_sessions

-- ---------------------------------------------------------------------
-- login_log — append-only record of every successful sign-in
-- ---------------------------------------------------------------------
create table if not exists public.login_log (
    id            uuid primary key default gen_random_uuid(),
    email         text             not null,
    logged_in_at  timestamptz      not null default now(),
    ip            text,                              -- best-effort, may be null on local-dev
    user_agent    text,
    -- which console served the login screen — usually "chap"; "cedadmin"
    -- if the user landed on cedadmin_main.py first.
    console       text             not null default 'chap'
);
create index if not exists login_log_email_idx
    on public.login_log (email, logged_in_at desc);
create index if not exists login_log_time_idx
    on public.login_log (logged_in_at desc);

-- ---------------------------------------------------------------------
-- activity_log — append-only record of meaningful user actions
-- ---------------------------------------------------------------------
create table if not exists public.activity_log (
    id            uuid primary key default gen_random_uuid(),
    email         text             not null,
    occurred_at   timestamptz      not null default now(),
    console       text             not null,         -- 'chap' | 'cedadmin'
    page          text             not null,         -- 'Dashboard' | 'Admin' | 'Intelligence' | 'CedAdmin' | 'Audit' | ...
    action        text             not null,         -- 'page_view' | 'csv_download' | 'manual_edit' | 'grant_role' | 'revoke_role' | 'scrape_dispatch' | 'app_filter_change' | ...
    target_type   text,                              -- 'app' | 'seller' | 'role' | 'workflow' | ...
    target_id     text,                              -- 'shein' | 'abc-123' | 'vroy@threecolts.com' | ...
    details       jsonb                              -- free-form metadata, never required
);
create index if not exists activity_log_email_time_idx
    on public.activity_log (email, occurred_at desc);
create index if not exists activity_log_time_idx
    on public.activity_log (occurred_at desc);
create index if not exists activity_log_action_idx
    on public.activity_log (action, occurred_at desc);

-- ---------------------------------------------------------------------
-- active_sessions — one row per (email, console). Bumped on heartbeat.
-- ---------------------------------------------------------------------
create table if not exists public.active_sessions (
    email           text             not null,
    console         text             not null,
    page            text             not null,
    last_seen_at    timestamptz      not null default now(),
    started_at      timestamptz      not null default now(),
    user_agent      text,
    primary key (email, console)
);
create index if not exists active_sessions_last_seen_idx
    on public.active_sessions (last_seen_at desc);

-- ---------------------------------------------------------------------
-- Helper RPC: upsert_active_session(...) so the client can bump in
-- one round-trip without a SELECT-then-UPDATE race.
-- ---------------------------------------------------------------------
create or replace function public.upsert_active_session(
    p_email      text,
    p_console    text,
    p_page       text,
    p_user_agent text default null
)
returns void language plpgsql as $$
begin
    insert into public.active_sessions (email, console, page, user_agent, last_seen_at, started_at)
    values (p_email, p_console, p_page, p_user_agent, now(), now())
    on conflict (email, console) do update
        set page         = excluded.page,
            user_agent   = coalesce(excluded.user_agent, public.active_sessions.user_agent),
            last_seen_at = now();
end;
$$;
