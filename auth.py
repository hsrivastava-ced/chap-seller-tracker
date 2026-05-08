"""
auth.py — email/password gate for the cHAP Seller Tracker.

Replaces the Google OAuth flow that kept fighting Streamlit Cloud
(`/oauth2callback` handler crashes, Authlib 1.7 state breakage, popup
polling never matching, etc.). This is fully self-hosted: Supabase
holds the user list, the admin (Hrithik) approves new sign-ups from
the Admin panel, and SMTP fires off the "request received / approved /
denied" notification emails.

Flow at a glance:

    First-time user:
      1. Hits the app, sees Sign in / Request access tabs.
      2. Picks "Request access", enters @threecolts.com email + name +
         password they'll remember.
      3. Row inserted into auth_users with status='pending'. Admin
         email goes out automatically (if SMTP is configured).
      4. User sees "Request submitted — you'll get an email when
         it's approved."

    Admin:
      5. Opens Admin → Access tab, sees the pending request.
      6. Clicks Approve. status flips to 'approved', user gets the
         "your access is approved" email.

    Returning user:
      7. Comes back, enters email + password, lands on the dashboard.
      8. Role is resolved via roles.yaml (existing logic — no change).

Hard-coded super admin (hsrivastava@threecolts.com) auto-approves on
first sign-up, so the system bootstraps without needing someone else
to flip a Supabase row.

Required setup:

    [1] Run sql/004_auth_users.sql in Supabase (one-time).

    [2] Streamlit Cloud → app → Settings → Secrets:

        # SMTP for notification emails (Gmail App Password recommended).
        # If omitted, login still works but emails won't fire.
        [smtp]
        host        = "smtp.gmail.com"
        port        = 465
        username    = "hsrivastava@threecolts.com"
        password    = "<Gmail App Password>"
        from_addr   = "hsrivastava@threecolts.com"
        admin_email = "hsrivastava@threecolts.com"

    [3] Supabase URL/key are already in secrets via SUPABASE_URL /
        SUPABASE_KEY env vars (config.py reads them).

    Old [auth] / [auth.google] blocks from the Google OAuth era can
    be deleted from secrets — no longer used.

Local dev (without real Supabase creds):
    Set AUTH_DEV_EMAIL=hsrivastava@threecolts.com in .env. The gate
    accepts that identity without checking Supabase. Production never
    takes this branch (Supabase URL is always set in Streamlit Cloud).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets as pysecrets
import time
from typing import Optional

import roles
from roles import UserPrincipal


_LOGGER = logging.getLogger(__name__)

PBKDF2_ITERATIONS = 600_000
EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
MIN_PASSWORD_LEN = 8

# Idle session timeout. Sliding window — every gate() call refreshes.
# Streamlit Cloud workers restart frequently and wipe session_state, so
# without a persistence layer every restart bounces the user back to
# login. We persist the session token in `st.query_params` (URL):
# synchronous reads on every page load, survives reloads, no extra
# component dependency. Token is HMAC-signed with cookie_secret so the
# URL exposure is acceptable for an internal tool — anyone who
# screen-shares the URL is sharing a 20-min session, not a credential.
SESSION_TTL_SECONDS = 20 * 60         # 20 minutes
SESSION_WARN_SECONDS = 60             # show "expires in 1 min" warning
SESSION_REFRESH_THRESHOLD_SECONDS = 5 * 60  # only re-issue token when
                                            # < this much time remains,
                                            # to avoid changing the URL
                                            # on every interaction
SESSION_QUERY_PARAM = "s"


# --------------------------------------------------------------------
# Public entry point — call this at the top of every Streamlit page.
# --------------------------------------------------------------------
def gate() -> UserPrincipal:
    """Force login and return the resolved principal.

    On failure (not signed in, wrong domain, account not approved)
    calls `st.stop()`. Callers MUST NOT call set_page_config before
    this — the login screen calls it itself.

    Session lifecycle:
      - First successful login sets a `chap_session` cookie containing
        an HMAC-signed (email, expires_at) token. cookie_secret is
        the signing key.
      - Every subsequent gate() call validates the cookie. If valid
        and the principal isn't already cached in session_state,
        rebuild it via `roles.principal_for(email)`.
      - If less than SESSION_REFRESH_THRESHOLD_SECONDS remain on the
        cookie, a fresh one is issued (sliding window — active users
        stay logged in indefinitely; idle users time out after
        SESSION_TTL_SECONDS).
    """
    import streamlit as st

    principal = st.session_state.get("_principal")
    if principal:
        # Even when principal is cached, keep the URL token fresh so
        # the next reload after a worker restart restores cleanly.
        _maybe_refresh_url_token(st, principal.email)
        return principal

    # URL-token fast path: restore the principal across a Streamlit
    # Cloud worker restart without re-authenticating.
    url_email = _email_from_url_token(st)
    if url_email:
        principal = roles.principal_for(url_email)
        if principal:
            st.session_state["_principal"] = principal
            _maybe_refresh_url_token(st, url_email)
            return principal

    # Local-dev escape hatch: AUTH_DEV_EMAIL bypasses Supabase.
    if _is_local_dev(st):
        dev = os.getenv("AUTH_DEV_EMAIL", "").strip().lower()
        if dev:
            principal = roles.principal_for(dev)
            if principal:
                st.session_state["_principal"] = principal
                return principal

    _render_auth_page(st)
    st.stop()


def require(action: str, principal: Optional[UserPrincipal] = None) -> UserPrincipal:
    """Assert that the current principal has permission for `action`."""
    import streamlit as st
    if principal is None:
        principal = st.session_state.get("_principal")
    if not roles.can(principal, action):
        st.error("You don't have permission to do that.")
        st.stop()
    return principal


def sign_out_button(st=None, *, skip_caption: bool = False):
    """Render a small 'Sign out' button + idle-timer in the sidebar."""
    if st is None:
        import streamlit as st  # noqa: F811
    principal = st.session_state.get("_principal")
    if not principal:
        return
    with st.sidebar:
        if not skip_caption:
            st.caption(f"Signed in as **{principal.email}**")
            st.caption(f"Role: `{principal.role}`")
        # Idle countdown — read the cookie's expiry rather than tracking
        # a session_state value, so worker restarts don't lose the
        # countdown either. Updates on every Streamlit interaction
        # (button clicks, tab switches, etc.) — there's no ambient
        # polling. If a user stares at the screen for >20 min without
        # interacting, their next click logs them out.
        secs_left = _session_seconds_remaining(st)
        if secs_left is not None:
            mins, secs = divmod(max(0, secs_left), 60)
            if secs_left <= SESSION_WARN_SECONDS:
                st.warning(
                    f"⏰ Session expires in {mins}m {secs}s — "
                    f"any click extends it."
                )
            else:
                st.caption(f"Session: {mins}m {secs}s remaining")
        if st.button("Sign out", use_container_width=True):
            _do_sign_out(st)


# --------------------------------------------------------------------
# URL-token session — HMAC-signed (email, expires_at) survives
# Streamlit Cloud worker restarts so users don't bounce to login on
# every reload. Lives in `st.query_params["s"]`. Sync read, sync
# write, no extra component dependency.
# --------------------------------------------------------------------
def _cookie_secret(st) -> Optional[str]:
    """Signing key for the URL session token.

    Resolution order:
      1. `[auth].cookie_secret` from Streamlit secrets (preferred — operator
         sets a stable random value once and sessions survive across
         redeploys + rotations of unrelated secrets).
      2. `CHAP_COOKIE_SECRET` env var (local-dev convenience).
      3. Derived from `[supabase].url + [supabase].service_role_key` —
         deterministic per-deployment so sessions persist across reboots
         even when the operator didn't explicitly set `cookie_secret`.
         Trade-off: rotating the Supabase service key invalidates every
         live session, but that's already a "force everyone to sign in
         again" event.

    The derived fallback exists because before it, a missing
    `[auth].cookie_secret` made `_set_url_token` silently no-op, and
    "sign in then reload → bounced to login" looked like a session-
    persistence regression rather than missing config.
    """
    try:
        secret = (st.secrets.get("auth", {}) or {}).get("cookie_secret")
    except Exception:
        secret = None
    if secret:
        return secret
    env = os.getenv("CHAP_COOKIE_SECRET")
    if env:
        return env
    # Derived fallback — stable per-deployment, no new secret required.
    try:
        sb = (st.secrets.get("supabase", {}) or {})
        url = sb.get("url") or ""
        key = sb.get("service_role_key") or sb.get("anon_key") or ""
    except Exception:
        url = ""
        key = ""
    if url and key:
        return hashlib.sha256(
            f"chap-cookie-v1|{url}|{key}".encode("utf-8")
        ).hexdigest()
    return None


def _make_session_token(email: str, secret: str, ttl: int = SESSION_TTL_SECONDS) -> str:
    """`email|expires_at|hex_sig` — base64url-encoded as one URL-safe string."""
    expires_at = int(time.time()) + ttl
    payload = f"{email.lower()}|{expires_at}"
    sig = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256,
    ).hexdigest()[:32]
    return _b64(f"{payload}|{sig}".encode("utf-8"))


def _verify_session_token(token: str, secret: str) -> Optional[tuple[str, int]]:
    """Return (email, expires_at) if the token is valid + unexpired."""
    if not token or not secret:
        return None
    try:
        raw = _b64d(token).decode("utf-8")
        email, expires_at_s, sig = raw.rsplit("|", 2)
        expires_at = int(expires_at_s)
    except Exception:
        return None
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{email}|{expires_at}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    if expires_at < int(time.time()):
        return None
    return email, expires_at


def _read_url_token(st) -> Optional[str]:
    """Pull the `s` query param. Streamlit returns either str or list."""
    try:
        token = st.query_params.get(SESSION_QUERY_PARAM)
    except Exception:
        return None
    if isinstance(token, list):
        token = token[0] if token else None
    return token or None


def _email_from_url_token(st) -> Optional[str]:
    secret = _cookie_secret(st)
    if not secret:
        return None
    token = _read_url_token(st)
    if not token:
        return None
    parsed = _verify_session_token(token, secret)
    if not parsed:
        # Stale / forged — clear the param so the next gate() call
        # cleanly falls through to the login page instead of looping
        # on a dead token.
        try:
            del st.query_params[SESSION_QUERY_PARAM]
        except Exception:
            pass
        return None
    return parsed[0]


def _session_seconds_remaining(st) -> Optional[int]:
    secret = _cookie_secret(st)
    if not secret:
        return None
    token = _read_url_token(st)
    parsed = _verify_session_token(token or "", secret)
    if not parsed:
        return None
    return parsed[1] - int(time.time())


def _set_url_token(st, email: str) -> None:
    secret = _cookie_secret(st)
    if not secret:
        return
    try:
        st.query_params[SESSION_QUERY_PARAM] = _make_session_token(email, secret)
    except Exception as err:
        _LOGGER.debug(f"failed to set session URL token: {err}")


def _maybe_refresh_url_token(st, email: str) -> None:
    """Re-issue the URL token when < 5 min remain so active users stay
    logged in indefinitely without rewriting the URL on every rerun."""
    secs_left = _session_seconds_remaining(st)
    if secs_left is None:
        _set_url_token(st, email)
        return
    if secs_left < SESSION_REFRESH_THRESHOLD_SECONDS:
        _set_url_token(st, email)


def _clear_url_token(st) -> None:
    try:
        if SESSION_QUERY_PARAM in st.query_params:
            del st.query_params[SESSION_QUERY_PARAM]
    except Exception:
        pass


# --------------------------------------------------------------------
# Password hashing — pbkdf2_sha256, stdlib only.
# Format: pbkdf2_sha256$<iters>$<salt_b64>$<hash_b64>
# --------------------------------------------------------------------
def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = pysecrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return (
        f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(derived)}"
    )


def verify_password(password: str, stored: str) -> bool:
    if not stored or not stored.startswith("pbkdf2_sha256$"):
        return False
    try:
        _, iters_s, salt_b64, hash_b64 = stored.split("$")
        iterations = int(iters_s)
        salt = _b64d(salt_b64)
        expected = _b64d(hash_b64)
    except Exception:
        return False
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(derived, expected)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# --------------------------------------------------------------------
# Login / signup page rendering
# --------------------------------------------------------------------
def _render_auth_page(st) -> None:
    st.set_page_config(
        page_title="Sign in — cHAP Seller Tracker",
        page_icon=":lock:",
        layout="centered",
    )
    st.markdown("# cHAP Seller Tracker")
    st.caption("Internal sales/admin dashboard for the cHAP seller fleet.")

    # Show the post-signup confirmation if it's queued in session.
    pending_msg = st.session_state.pop("_signup_pending_msg", None)
    if pending_msg:
        st.success(pending_msg)

    tab_login, tab_signup = st.tabs(["Sign in", "Request access"])

    with tab_login:
        _render_login_form(st)

    with tab_signup:
        _render_signup_form(st)


def _render_login_form(st) -> None:
    with st.form("login_form", clear_on_submit=False):
        email = st.text_input("Email", placeholder="you@threecolts.com").strip().lower()
        password = st.text_input("Password", type="password")
        submit = st.form_submit_button("Sign in", type="primary", use_container_width=True)

    if not submit:
        return

    if not email or not password:
        st.error("Email and password are both required.")
        return
    if not EMAIL_RE.match(email):
        st.error("That doesn't look like a valid email.")
        return

    user = _supabase_get_user(email)
    if not user:
        st.error("No account with that email. Use the **Request access** tab to sign up.")
        return

    status = (user.get("status") or "pending").lower()
    if status == "pending":
        st.warning("Your request is still pending admin approval. You'll get an email when it's approved.")
        return
    if status == "denied":
        st.error("This account has been denied access. Contact the admin if you think that's a mistake.")
        return

    if not verify_password(password, user.get("password_hash") or ""):
        st.error("Wrong password. Try again.")
        return

    # Authenticated. Resolve role via roles.yaml (existing logic).
    principal = roles.principal_for(email)
    if principal is None:
        # Shouldn't happen — signup enforces @threecolts.com — but
        # handle gracefully if roles.yaml gets out of sync.
        st.error(
            f"`{email}` is signed in but isn't on the allowed "
            f"`@{roles.ALLOWED_DOMAIN}` domain."
        )
        return

    st.session_state["_principal"] = principal
    _supabase_update_last_login(email)
    _set_url_token(st, email)
    # Audit trail — best-effort, swallow failures so a flaky Supabase
    # doesn't block login.
    try:
        import audit
        audit.log_login(
            email,
            ip=audit.current_ip(st),
            user_agent=audit.current_user_agent(st),
            console=st.session_state.get("_audit_console", "chap"),
        )
    except Exception:
        pass
    st.rerun()


def _render_signup_form(st) -> None:
    st.caption(
        f"New here? Request access with your **@{roles.ALLOWED_DOMAIN}** email. "
        f"The admin (Hrithik) will be notified and you'll get an email when "
        f"your request is approved."
    )
    with st.form("signup_form", clear_on_submit=False):
        email = st.text_input("Email", placeholder=f"you@{roles.ALLOWED_DOMAIN}").strip().lower()
        display_name = st.text_input("Your name", placeholder="First Last").strip()
        password = st.text_input(
            "Password",
            type="password",
            help=f"At least {MIN_PASSWORD_LEN} characters. You'll use this every time you sign in.",
        )
        confirm = st.text_input("Confirm password", type="password")
        submit = st.form_submit_button("Request access", type="primary", use_container_width=True)

    if not submit:
        return

    if not email or not display_name or not password:
        st.error("All fields are required.")
        return
    if not EMAIL_RE.match(email):
        st.error("That doesn't look like a valid email.")
        return
    if not email.endswith(f"@{roles.ALLOWED_DOMAIN}"):
        st.error(f"Only @{roles.ALLOWED_DOMAIN} email addresses are accepted.")
        return
    if len(password) < MIN_PASSWORD_LEN:
        st.error(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
        return
    if password != confirm:
        st.error("Passwords don't match.")
        return

    existing = _supabase_get_user(email)
    if existing:
        status = (existing.get("status") or "pending").lower()
        if status == "approved":
            st.error("There's already an account with this email — sign in on the other tab.")
        elif status == "pending":
            st.warning("There's already a pending request for this email. Wait for admin approval.")
        else:
            st.error("This account has been denied access. Contact the admin.")
        return

    # Hard-coded super admin auto-approves on signup so the system
    # bootstraps cleanly. See roles.HARD_CODED_SUPER_ADMINS.
    auto_approved = email in {a.lower() for a in roles.HARD_CODED_SUPER_ADMINS}
    initial_status = "approved" if auto_approved else "pending"
    approver = email if auto_approved else None

    ok, err_msg = _supabase_create_user(
        email=email,
        password_hash=hash_password(password),
        display_name=display_name,
        status=initial_status,
        approved_by=approver,
    )
    if not ok:
        st.error(f"Couldn't save your request. Supabase said: {err_msg}")
        st.caption(
            "If the message mentions RLS, the `auth_users` table needs an "
            "INSERT policy for `anon`. If it mentions missing creds, add "
            "`SUPABASE_URL` and `SUPABASE_KEY` to Streamlit secrets."
        )
        return

    if auto_approved:
        st.session_state["_signup_pending_msg"] = (
            "Account created and auto-approved (super admin). Sign in on the **Sign in** tab."
        )
    else:
        _safe_notify_admin(requester_email=email, requester_name=display_name)
        st.session_state["_signup_pending_msg"] = (
            f"Request submitted for **{email}**. The admin (Hrithik) will be "
            f"notified by email; you'll hear back as soon as it's approved."
        )
    st.rerun()


# --------------------------------------------------------------------
# Supabase + email helpers (lazy imports so tests don't need them)
# --------------------------------------------------------------------
def _supabase_client():
    try:
        from supabase_client import SupabaseClient
        return SupabaseClient()
    except Exception as err:
        _LOGGER.error("auth: failed to construct SupabaseClient: %s", err)
        return None


def _supabase_get_user(email: str) -> Optional[dict]:
    client = _supabase_client()
    if client is None:
        return None
    return client.get_auth_user(email)


def _supabase_create_user(
    *,
    email: str,
    password_hash: str,
    display_name: str,
    status: str,
    approved_by: Optional[str],
) -> tuple[bool, Optional[str]]:
    client = _supabase_client()
    if client is None:
        return False, "Supabase client could not be constructed."
    return client.create_auth_user(
        email=email,
        password_hash=password_hash,
        display_name=display_name,
        status=status,
        approved_by=approved_by,
    )


def _supabase_update_last_login(email: str) -> None:
    client = _supabase_client()
    if client is None:
        return
    client.update_auth_user_last_login(email)


def _safe_notify_admin(*, requester_email: str, requester_name: str) -> None:
    try:
        from email_notifications import notify_admin_new_signup
        notify_admin_new_signup(requester_email=requester_email, requester_name=requester_name)
    except Exception as err:
        _LOGGER.warning("notify_admin_new_signup failed: %s", err)


# --------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------
def _is_local_dev(st) -> bool:
    """No Supabase creds AND not on Streamlit Cloud."""
    if os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_KEY"):
        return False
    if os.getenv("STREAMLIT_SHARING_MODE"):
        return False
    return True


def _do_sign_out(st) -> None:
    st.session_state.pop("_principal", None)
    _clear_url_token(st)
    st.rerun()
