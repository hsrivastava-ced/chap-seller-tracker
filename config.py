"""
config.py — runtime config loader.

Historical note: this file used to hand-maintain APP_1/2/3 env lookups,
which meant adding a fourth admin panel required editing code. That logic
moved to `app_registry.py` (backed by `apps.yaml`). This file is now a
thin shim that:

  1. calls load_dotenv() so local `.env` usage keeps working
  2. exposes the historical names (APP_IDS, CREDENTIALS, USERNAME, PASSWORD,
     LOGIN_URL, SUPABASE_URL/KEY, HEADLESS) so existing imports don't break
  3. derives those names from the registry instead of hard-coding them

If you're writing NEW code, import from `app_registry` directly —
`active_apps()` / `credentials_for()` give you everything here and more.
"""
from __future__ import annotations

import os
from dotenv import load_dotenv

# Load .env before we read the registry — the registry itself reads
# APP_N_USER / APP_N_PASS out of os.environ.
load_dotenv()

import app_registry  # noqa: E402  (intentional ordering: dotenv first)


def _get_bool(name, default="true"):
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off")


# -----------------------------------------------------------------
# Derived from the registry
# -----------------------------------------------------------------
_REGISTRY = app_registry.all_apps()

# {app_id -> app_id}  (kept as a dict for back-compat with old callers
#  that did `for slug in APP_IDS.values()`).
APP_IDS = {a.id: a.id for a in _REGISTRY}

# {app_id -> (user, password)}  — only entries whose creds resolve.
# Stripped like the old code did: if an env key is missing we drop the app.
CREDENTIALS = {}
for _app in _REGISTRY:
    _u, _p = app_registry.credentials_for(_app)
    if _u and _p:
        CREDENTIALS[_app.id] = (_u, _p)

# -----------------------------------------------------------------
# Back-compat singletons (do NOT use for anything but app #1)
# -----------------------------------------------------------------
# The old code exposed USERNAME / PASSWORD as module-level globals tied
# to APP_1. Keep those names working for any stale reference, but point
# them at whichever app has creds_ref=APP_1 (or the first registry
# entry if no APP_1 exists).
_first = next(
    (a for a in _REGISTRY if a.creds_ref == "APP_1"),
    _REGISTRY[0] if _REGISTRY else None,
)
if _first:
    _u1, _p1 = app_registry.credentials_for(_first)
else:
    _u1, _p1 = None, None
USERNAME = _u1
PASSWORD = _p1

# -----------------------------------------------------------------
# Non-app config (unchanged)
# -----------------------------------------------------------------
LOGIN_URL = os.getenv("LOGIN_URL")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Default to headless so scheduler.py / cron / systemd runs don't pop
# a browser window on the host. For local debugging, set HEADLESS=false
# in .env (or `HEADLESS=false python3 scraper.py`) to watch the scrape
# in a visible Chromium window.
HEADLESS = _get_bool("HEADLESS", "true")
