"""
auth.py — Google OAuth gate for the Streamlit dashboard.

What it does:
    1. Forces a Google login before anything else renders.
    2. Verifies the signed-in email is on @threecolts.com.
    3. Looks up the user's role in roles.yaml and stashes a UserPrincipal
       in st.session_state so the rest of the app (dashboard.py, admin_ui.py)
       can branch on permissions without re-reading YAML every page.

How sign-in works (same-window redirect, no popup):
    - Login page renders an `<a href=…>` link to Google's authorize URL.
    - User clicks → browser navigates to Google → consent → Google
      redirects the same window back to `redirect_uri` with `?code=…&state=…`.
    - On that load, gate() detects the query params, verifies the state
      HMAC, exchanges the code for a token, fetches userinfo, and stores
      the token in session_state. Query params are cleared and the app
      reruns clean.

Why not native st.login("google"):
    Streamlit's native handler at /oauth2callback validates state against
    a process-local cache that doesn't survive Streamlit Cloud's frequent
    worker restarts — this manifested as MismatchingStateError on every
    sign-in. Doing the OAuth round-trip in our own code (with HMAC-signed
    state) sidesteps the issue.

Why not streamlit-oauth's popup flow:
    Streamlit auto-registers a handler at `/oauth2callback` whenever
    Authlib is installed. If the OAuth `redirect_uri` is set to that
    path, the popup hits Streamlit's native handler instead of our code,
    which silently redirects to base — the popup-poll never sees a
    matching URL and the popup just sits there showing the login page
    again. The same-window flow doesn't care which path is used (as
    long as it's NOT `/oauth2callback`).

Required setup (do BOTH steps, once):

    [1] In Streamlit Cloud → app → Settings → Secrets:

        [auth]
        redirect_uri = "https://chap-seller-tracker.streamlit.app/"
        # cookie_secret used to HMAC-sign the OAuth state token.
        # Generate with: python -c 'import secrets; print(secrets.token_urlsafe(48))'
        cookie_secret = "<32+ random bytes>"

        [auth.google]
        client_id     = "<from Google Cloud console>"
        client_secret = "<from Google Cloud console>"

    [2] In Google Cloud Console → APIs & Services → Credentials → OAuth
        client → Authorized redirect URIs, add (or change to):

            https://chap-seller-tracker.streamlit.app/

        The trailing slash matters — it must match `redirect_uri` exactly.
        DO NOT use `/oauth2callback` — Streamlit's native handler will
        intercept it and break the flow.

Local dev (without real Google creds):
    Set AUTH_DEV_EMAIL=hsrivastava@threecolts.com in .env. The gate
    accepts that identity without doing OAuth. Only active when no
    [auth] secrets are configured AND we're not on Streamlit Cloud.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets as pysecrets
from typing import Optional
from urllib.parse import urlencode

import roles
from roles import UserPrincipal


GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


# --------------------------------------------------------------------
# Public entry point — call this at the top of every Streamlit page.
# --------------------------------------------------------------------
def gate() -> UserPrincipal:
    """Force login and return the resolved principal.

    On failure (not signed in, wrong domain) this calls `st.stop()` so
    the caller never renders. Callers MUST NOT call set_page_config
    before this — the login screen calls it itself.
    """
    import streamlit as st  # heavy; not needed in tests

    principal = st.session_state.get("_principal")
    if principal:
        return principal

    # Did Google just redirect back to us? Handle the callback first
    # (consumes ?code=…&state=… from the URL and stashes a token).
    email = _try_consume_oauth_callback(st)

    if not email:
        email = _email_from_cached_token(st)

    if not email and _is_local_dev(st):
        dev = os.getenv("AUTH_DEV_EMAIL", "").strip()
        if dev:
            email = dev

    if not email:
        _render_login_page(st)
        st.stop()

    principal = roles.principal_for(email)
    if principal is None:
        _render_domain_reject(st, email)
        st.stop()

    st.session_state["_principal"] = principal
    return principal


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
    """Render a small 'Sign out' button in the sidebar."""
    if st is None:
        import streamlit as st  # noqa: F811
    principal = st.session_state.get("_principal")
    if not principal:
        return
    with st.sidebar:
        if not skip_caption:
            st.caption(f"Signed in as **{principal.email}**")
            st.caption(f"Role: `{principal.role}`")
        if st.button("Sign out", use_container_width=True):
            _do_sign_out(st)


# --------------------------------------------------------------------
# OAuth callback handling
# --------------------------------------------------------------------
def _try_consume_oauth_callback(st) -> Optional[str]:
    """If the URL has ?code=…&state=…, complete the OAuth round trip.

    Returns the user's email on success, None otherwise. Always clears
    OAuth params from the URL on exit so refreshes don't loop.
    """
    qp = st.query_params
    code = qp.get("code")
    state = qp.get("state")
    if not code:
        return None

    # Streamlit's query_params returns either a single string or a list
    # depending on version; normalize to scalar.
    if isinstance(code, list):
        code = code[0] if code else None
    if isinstance(state, list):
        state = state[0] if state else None

    cfg = _auth_config(st)
    if not cfg:
        # No config — clear params and bail.
        _clear_oauth_params(st)
        return None

    # Verify state (HMAC signed). If cookie_secret isn't configured we
    # accept any state — internal-tool, low CSRF risk, and we'd rather
    # have a working login than a perfect one.
    secret = cfg.get("cookie_secret")
    if secret and not _verify_state(state or "", secret):
        st.warning("OAuth state mismatch — please sign in again.")
        _clear_oauth_params(st)
        return None

    token = _exchange_code_for_token(
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        code=code,
        redirect_uri=cfg["redirect_uri"],
    )
    if not token:
        st.error("Couldn't exchange the OAuth code for a token. Try again.")
        _clear_oauth_params(st)
        return None

    st.session_state["_oauth_token"] = token
    _clear_oauth_params(st)
    return _email_from_token(token)


def _clear_oauth_params(st) -> None:
    """Strip ?code/?state/?error/?scope/?authuser/?prompt from the URL."""
    try:
        for k in ("code", "state", "error", "error_description", "scope", "authuser", "prompt", "hd"):
            if k in st.query_params:
                del st.query_params[k]
    except Exception:
        pass


def _exchange_code_for_token(
    *, client_id: str, client_secret: str, code: str, redirect_uri: str
) -> Optional[dict]:
    import requests
    try:
        r = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=10,
        )
        if r.status_code != 200:
            return None
        return r.json() or None
    except Exception:
        return None


def _email_from_cached_token(st) -> Optional[str]:
    """If a token is already in session, exchange it for the user's email.

    On failure (expired/revoked token) we clear it so the user falls
    through to the login page instead of looping on a dead session.
    """
    token = st.session_state.get("_oauth_token")
    if not token:
        return None
    email = _email_from_token(token)
    if not email:
        st.session_state.pop("_oauth_token", None)
    return email


def _email_from_token(token: dict) -> Optional[str]:
    import requests
    access = (token or {}).get("access_token")
    if not access:
        return None
    try:
        r = requests.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access}"},
            timeout=8,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        email = (data.get("email") or "").strip()
        if email and data.get("email_verified"):
            return email
    except Exception:
        return None
    return None


# --------------------------------------------------------------------
# HMAC-signed state — survives Streamlit Cloud worker restarts
# (unlike session_state or auth_cache).
# --------------------------------------------------------------------
def _make_state(secret: str) -> str:
    nonce = pysecrets.token_urlsafe(16)
    sig = hmac.new(secret.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return f"{nonce}.{sig}"


def _verify_state(state: str, secret: str) -> bool:
    if not state or "." not in state:
        return False
    nonce, sig = state.rsplit(".", 1)
    expected = hmac.new(secret.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return hmac.compare_digest(sig, expected)


# --------------------------------------------------------------------
# Config / env detection
# --------------------------------------------------------------------
def _auth_config(st) -> Optional[dict]:
    """Return a flattened auth config dict or None if not configured.

    Accepts either:

        [auth]                              [auth]
        client_id = "..."                   redirect_uri = "..."
        client_secret = "..."        OR     cookie_secret = "..."
        redirect_uri = "..."
                                            [auth.google]
                                            client_id = "..."
                                            client_secret = "..."
    """
    try:
        auth = dict(st.secrets.get("auth", {}))
    except Exception:
        return None
    google = {}
    try:
        google = dict(auth.get("google", {}) or {})
    except Exception:
        google = {}
    cfg = {**auth, **google}  # google sub-table wins for client_id/secret
    needed = {"client_id", "client_secret", "redirect_uri"}
    if not needed.issubset(cfg.keys()):
        return None
    return cfg


def _is_local_dev(st) -> bool:
    if _auth_config(st):
        return False
    if os.getenv("STREAMLIT_SHARING_MODE"):
        return False
    return True


# --------------------------------------------------------------------
# Login page rendering
# --------------------------------------------------------------------
def _render_login_page(st) -> None:
    st.set_page_config(
        page_title="Sign in — cHAP Seller Tracker",
        page_icon=":lock:",
        layout="centered",
    )
    st.markdown(
        """
        # cHAP Seller Tracker

        Sign in with your **@threecolts.com** Google account to continue.
        """
    )

    cfg = _auth_config(st)
    if not cfg:
        if _is_local_dev(st) and not os.getenv("AUTH_DEV_EMAIL"):
            st.info(
                "Local dev: set `AUTH_DEV_EMAIL=you@threecolts.com` in `.env` "
                "to skip the OAuth dance while developing."
            )
        else:
            st.error(
                "OAuth is not configured. The `[auth]` block is missing from "
                "Streamlit secrets. See `auth.py` docstring for the expected shape."
            )
        return

    redirect_uri = cfg["redirect_uri"]
    if redirect_uri.rstrip("/").endswith("/oauth2callback"):
        st.error(
            "**OAuth misconfigured.** `redirect_uri` ends with `/oauth2callback`, "
            "which Streamlit's native auth handler intercepts. Update the redirect "
            "URI in Streamlit secrets and Google Cloud Console to "
            f"`{redirect_uri.rsplit('/oauth2callback', 1)[0] or '/'}/` (the app root)."
        )
        return

    secret = cfg.get("cookie_secret") or cfg.get("client_secret", "")
    state = _make_state(secret) if secret else pysecrets.token_urlsafe(16)
    auth_url = _build_authorize_url(cfg["client_id"], redirect_uri, state)

    st.link_button(
        "Sign in with Google",
        url=auth_url,
        use_container_width=True,
        type="primary",
    )
    st.caption(
        "Only @threecolts.com identities are permitted. "
        "If you need access, ask the admin (Hrithik)."
    )


def _build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
        "include_granted_scopes": "true",
    }
    return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"


def _render_domain_reject(st, email: str) -> None:
    st.set_page_config(
        page_title="Access denied — cHAP Seller Tracker",
        page_icon=":no_entry:",
        layout="centered",
    )
    st.error(
        f"The account `{email}` is signed in but is not on the allowed "
        f"**@{roles.ALLOWED_DOMAIN}** domain. Please sign in with a "
        f"Threecolts Google account."
    )
    if st.button("Try again"):
        _do_sign_out(st)


def _do_sign_out(st) -> None:
    for k in ("_principal", "_oauth_token"):
        st.session_state.pop(k, None)
    _clear_oauth_params(st)
    st.rerun()
