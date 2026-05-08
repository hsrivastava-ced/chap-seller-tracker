"""
audit.py — login + activity audit trail backed by Supabase.

Three tables (sql/005_audit_log.sql):
  - login_log         every successful sign-in    (append-only)
  - activity_log      every meaningful action     (append-only)
  - active_sessions   one row per (email, console) (upserted heartbeat)

Public API:
  log_login(email, *, ip, user_agent, console)
  heartbeat(email, *, console, page, user_agent)        ← call from page main()
  log_action(email, *, console, page, action, target_type, target_id, details)
  fetch_active(window_minutes=5)
  fetch_recent_logins(limit=50)
  fetch_recent_activity(limit=100, *, email=None, action=None, console=None)

Failure mode: every write is wrapped in try/except so a Supabase outage
does NOT block sign-in or page rendering — the worst case is a missing
audit row. The fetch helpers return [] on failure so the Audit page
degrades gracefully.

Streamlit context helpers (`current_ip`, `current_user_agent`) read
`st.context.headers` (Streamlit ≥1.36); they return None on local dev
or any version without the API.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Streamlit context helpers — best-effort IP / user-agent extraction
# ---------------------------------------------------------------------
def current_ip(st) -> Optional[str]:
    """Return the requesting client's IP, or None if unavailable.

    On Streamlit Cloud, the original IP comes through X-Forwarded-For
    (the platform terminates TLS in front). Fall back to X-Real-IP if
    XFF is missing. Both headers can be spoofed by a client behind a
    misconfigured proxy — fine for an internal-tools audit log, NOT
    for security gating.
    """
    headers = _safe_headers(st)
    if not headers:
        return None
    xff = headers.get("X-Forwarded-For") or headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip() or None
    real = headers.get("X-Real-IP") or headers.get("x-real-ip")
    return (real or "").strip() or None


def current_user_agent(st) -> Optional[str]:
    headers = _safe_headers(st)
    if not headers:
        return None
    ua = headers.get("User-Agent") or headers.get("user-agent")
    return (ua or "").strip() or None


def _safe_headers(st) -> Optional[dict[str, str]]:
    try:
        ctx = getattr(st, "context", None)
        if ctx is None:
            return None
        return dict(ctx.headers or {})
    except Exception:
        return None


# ---------------------------------------------------------------------
# Internal — get a SupabaseClient lazily so module import doesn't fail
# in tests / local-dev without secrets.
# ---------------------------------------------------------------------
def _client():
    try:
        from supabase_client import SupabaseClient
        sb = SupabaseClient()
        if sb._dry_run or sb._client is None:
            return None
        return sb._client
    except Exception as err:
        _LOGGER.debug(f"audit: SupabaseClient unavailable ({err})")
        return None


# ---------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------
def log_login(
    email: str, *,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    console: str = "chap",
) -> None:
    """Record one successful sign-in. Silently no-ops on Supabase failure."""
    client = _client()
    if client is None:
        return
    try:
        client.table("login_log").insert({
            "email": (email or "").strip().lower(),
            "ip": ip,
            "user_agent": user_agent,
            "console": console,
        }).execute()
    except Exception as err:
        _LOGGER.debug(f"audit: log_login failed: {err}")


def heartbeat(
    email: str, *,
    console: str,
    page: str,
    user_agent: Optional[str] = None,
    throttle_seconds: int = 60,
    st=None,
) -> None:
    """Bump active_sessions for this (email, console). Throttled via
    session_state so we don't slam Supabase on every Streamlit re-run.

    Also logs a single 'page_view' to activity_log when the page
    actually changes (not on re-renders of the same page).
    """
    if not email:
        return
    if st is None:
        try:
            import streamlit as st  # type: ignore
        except Exception:
            st = None

    # Throttle the upsert. Streamlit re-runs on every widget event, so
    # without throttling we'd write hundreds of rows per session.
    skip_upsert = False
    if st is not None:
        ss = getattr(st, "session_state", {})
        last_key = f"_audit_hb_{console}"
        last_ts = ss.get(last_key)
        if last_ts is not None:
            age = (datetime.now(timezone.utc) - last_ts).total_seconds()
            if age < throttle_seconds:
                skip_upsert = True
        if not skip_upsert:
            ss[last_key] = datetime.now(timezone.utc)

    client = _client()
    if client is None:
        return

    if not skip_upsert:
        try:
            client.rpc("upsert_active_session", {
                "p_email":      email.strip().lower(),
                "p_console":    console,
                "p_page":       page,
                "p_user_agent": user_agent,
            }).execute()
        except Exception as err:
            _LOGGER.debug(f"audit: heartbeat upsert failed: {err}")

    # Log a page_view to activity_log only when page changed since the
    # last logged page in this Streamlit session.
    if st is not None:
        ss = getattr(st, "session_state", {})
        last_pv_key = f"_audit_last_page_{console}"
        last_pv = ss.get(last_pv_key)
        if last_pv != page:
            log_action(
                email=email,
                console=console,
                page=page,
                action="page_view",
            )
            ss[last_pv_key] = page


def log_action(
    *,
    email: str,
    console: str,
    page: str,
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
) -> None:
    """Record one meaningful user action. Silently no-ops on failure."""
    client = _client()
    if client is None:
        return
    try:
        client.table("activity_log").insert({
            "email":       (email or "").strip().lower(),
            "console":     console,
            "page":        page,
            "action":      action,
            "target_type": target_type,
            "target_id":   target_id,
            "details":     details,
        }).execute()
    except Exception as err:
        _LOGGER.debug(f"audit: log_action failed: {err}")


# ---------------------------------------------------------------------
# Reads — used by audit_ui
# ---------------------------------------------------------------------
def fetch_active(window_minutes: int = 5) -> list[dict]:
    """Rows from active_sessions where last_seen_at is within the
    window. Sorted newest-first."""
    client = _client()
    if client is None:
        return []
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()
        resp = (
            client.table("active_sessions")
            .select("*")
            .gte("last_seen_at", cutoff)
            .order("last_seen_at", desc=True)
            .execute()
        )
        return list(getattr(resp, "data", None) or [])
    except Exception as err:
        _LOGGER.debug(f"audit: fetch_active failed: {err}")
        return []


def fetch_recent_logins(limit: int = 50, *, email: Optional[str] = None) -> list[dict]:
    client = _client()
    if client is None:
        return []
    try:
        q = client.table("login_log").select("*")
        if email:
            q = q.eq("email", email.strip().lower())
        resp = q.order("logged_in_at", desc=True).limit(limit).execute()
        return list(getattr(resp, "data", None) or [])
    except Exception as err:
        _LOGGER.debug(f"audit: fetch_recent_logins failed: {err}")
        return []


def fetch_recent_activity(
    limit: int = 100, *,
    email: Optional[str] = None,
    action: Optional[str] = None,
    console: Optional[str] = None,
    since: Optional[datetime] = None,
) -> list[dict]:
    client = _client()
    if client is None:
        return []
    try:
        q = client.table("activity_log").select("*")
        if email:
            q = q.eq("email", email.strip().lower())
        if action:
            q = q.eq("action", action)
        if console:
            q = q.eq("console", console)
        if since:
            q = q.gte("occurred_at", since.isoformat())
        resp = q.order("occurred_at", desc=True).limit(limit).execute()
        return list(getattr(resp, "data", None) or [])
    except Exception as err:
        _LOGGER.debug(f"audit: fetch_recent_activity failed: {err}")
        return []
