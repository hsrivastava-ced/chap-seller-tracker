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

CREATE TABLE IF NOT EXISTS public.auth_users (
  email TEXT PRIMARY KEY,
  password_hash TEXT NOT NULL,
  display_name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  approved_at TIMESTAMPTZ,
  approved_by TEXT,
  last_login_at TIMESTAMPTZ
);

-- The CHECK lives in its own ALTER so the file stays paste-friendly in
-- Supabase's SQL Editor (multi-line inline CHECK confused the parser
-- on first run — see commit 36f59ab for the fix).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'auth_users_status_check'
  ) THEN
    ALTER TABLE public.auth_users
      ADD CONSTRAINT auth_users_status_check
      CHECK (status IN ('pending', 'approved', 'denied'));
  END IF;
END$$;

CREATE INDEX IF NOT EXISTS auth_users_status_idx ON public.auth_users (status);
