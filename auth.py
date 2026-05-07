"""
auth.py — Google OAuth gate for the Streamlit dashboard.

What it does:
    1. Forces a Google login before anything else renders.
    2. Verifies the signed-in email is on @threecolts.com.
    3. Looks up the user's role in roles.yaml and stashes a UserPrincipal
       in st.session_state so the rest of the app (dashboard.py, admin_ui.py)
       can branch on permissions without re-reading YAML every page.

Why streamlit-oauth (not native st.login):
    Streamlit 1.56's native st.login("google") + Authlib's callback
    handler raises MismatchingStateError on every sign-in
    (the CSRF cookie isn't validated against the URL state). The
    streamlit-oauth library uses a popup + postMessage flow and avoids
    the broken cookie handshake. We pin authlib<1.7 in requirements
    just in case anything else imports it transitively.

Setup (done ONCE in Streamlit Cloud → app → Settings → Secrets):

    [auth]
    redirect_uri = "https://chap-seller-tracker.streamlit.app/"

    [auth.google]
    client_id     = "<from Google Cloud console>"
    client_secret = "<from Google Cloud console>"

    NOTE: for the streamlit-oauth library path, redirect_uri should be
    the app's own URL (the popup posts the OAuth code back to the
    parent window via postMessage; the path doesn't need to be a
    dedicated route). Whatever value you pick MUST be added verbatim
    to Google Cloud Console → APIs & Services → Credentials → OAuth
    client → Authorized redirect URIs.

    NOTE: cookie_secret / server_metadata_url are NOT required by the
    library path — they were only used by the native st.login flow.

Local dev (without real Google creds):
    Set AUTH_DEV_EMAIL=hsrivastava@threecolts.com in .env before running
    `streamlit run dashboard.py`. The gate accepts that identity without
    doing an OAuth handshake. This path is GUARDED — it only activates
    when the env var is set AND the Streamlit runtime is clearly local
    (no [auth] section configured). Production will never take this branch.
"""
from __future__ import annotations

import os
from typing import Optional

import roles
from roles import UserPrincipal


# --------------------------------------------------------------------
# Public entry point — call this at the top of every Streamlit page.
# --------------------------------------------------------------------
def gate() -> UserPrincipal:
    """Force login and return the resolved principal.

    On failure (not signed in, wrong domain) this function calls
    `st.stop()` so the rest of the page never renders. The caller can
    assume a non-None principal when `gate()` returns.

    The login page (when shown) calls `st.set_page_config` itself —
    callers MUST NOT call set_page_config before invoking gate().
    """
    import streamlit as st  # local import: streamlit is heavy and the
                            # tests in this repo don't need it

    principal = st.session_state.get("_principal")
    if principal:
        return principal

    email = _email_from_cached_token(st)

    if not email and _is_local_dev(st):
        dev = os.getenv("AUTH_DEV_EMAIL", "").strip()
        if dev:
            email = dev

    if not email:
        # Render login page (does set_page_config first, then the button).
        # If the OAuth round-trip just completed, this also returns the token.
        token = _render_login_and_capture_token(st)
        if token:
            st.session_state["_oauth_token"] = token
            st.rerun()
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
# Internals — token / email resolution
# --------------------------------------------------------------------
def _email_from_cached_token(st) -> Optional[str]:
    """If a token is already in session, exchange it for the user's email.

    On failure (expired/revoked token) we clear it so the user falls through
    to the login page instead of looping on a dead session.
    """
    token = st.session_state.get("_oauth_token")
    if not token:
        return None
    email = _email_from_token(token)
    if not email:
        st.session_state.pop("_oauth_token", None)
    return email


def _email_from_token(token: dict) -> Optional[str]:
    """Round-trip the access_token through Google's userinfo endpoint."""
    import requests
    access = (token or {}).get("access_token")
    if not access:
        return None
    try:
        r = requests.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
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


def _auth_config(st) -> Optional[dict]:
    """Return a flattened auth config dict or None if not configured.

    Accepts either:

        [auth]                              [auth]
        client_id = "..."                   redirect_uri = "..."
        client_secret = "..."        OR
        redirect_uri = "..."                [auth.google]
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
    """No [auth] block in secrets AND we're not on Streamlit Cloud."""
    if _auth_config(st):
        return False
    if os.getenv("STREAMLIT_SHARING_MODE"):
        return False
    return True


# --------------------------------------------------------------------
# Login page rendering — set_page_config FIRST, then everything else.
# --------------------------------------------------------------------
def _render_login_and_capture_token(st) -> Optional[dict]:
    """Render the sign-in page and (if the popup just completed) return a token."""
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
        # Local dev: no secrets configured. Show the env-var hint and stop.
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
        return None

    try:
        from streamlit_oauth import OAuth2Component
    except Exception:
        st.error(
            "The `streamlit-oauth` package is not installed. Add it to "
            "`requirements.txt` and redeploy."
        )
        return None

    oauth = OAuth2Component(
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        authorize_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        refresh_token_endpoint="https://oauth2.googleapis.com/token",
        revoke_token_endpoint="https://oauth2.googleapis.com/revoke",
    )

    result = oauth.authorize_button(
        "Sign in with Google",
        redirect_uri=cfg["redirect_uri"],
        scope="openid email profile",
        key="chap_google_signin",
        use_container_width=True,
    )

    st.caption(
        "Only @threecolts.com identities are permitted. "
        "If you need access, ask the admin (Hrithik)."
    )

    if result and "token" in result:
        return result["token"]
    return None


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
    for k in ("_principal", "_oauth_token", "_pending_email"):
        st.session_state.pop(k, None)
    logout = getattr(st, "logout", None)
    if callable(logout):
        try:
            logout()
        except Exception:
            pass
    st.rerun()
