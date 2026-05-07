-- 004_auth_users.sql — email/password auth with admin approval
--
-- Replaces the Google-OAuth flow that kept fighting Streamlit Cloud.
-- Each row is one person who has requested or been granted access to
-- the dashboard. password_hash stores a pbkdf2_sha256 string in the
-- format `pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>`.
--
-- Status lifecycle:
--   pending  → new sign-up, waiting for admin (Hrithik) to approve.
--   approved → can sign in.
--   denied   → can't sign in. Kept (rather than deleted) so we have
--              an audit trail of who requested and was rejected.
--
-- The hard-coded super-admin in roles.py (hsrivastava@threecolts.com)
-- is auto-approved on first sign-up so the admin can bootstrap without
-- needing someone else to flip a row in Supabase.
--
-- Idempotent: re-runnable.

create table if not exists public.auth_users (
    email           text primary key,
    password_hash   text not null,
    display_name    text not null default '',
    status          text not null default 'pending'
                       check (status in ('pending', 'approved', 'denied')),
    requested_at    timestamptz not null default now(),
    approved_at     timestamptz,
    approved_by     text,
    last_login_at   timestamptz
);

create index if not exists auth_users_status_idx on public.auth_users (status);
