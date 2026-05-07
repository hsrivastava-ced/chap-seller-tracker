"""
auth.py — Google OAuth gate for the Streamlit dashboard.

What it does:
    1. Forces a Google login before anything else renders.
    2. Verifies the signed-in email is on @threecolts.com.
    3. Looks up the user's role in roles.yaml and stashes a UserPrincipal
       in st.session_state so the rest of the app (dashboard.py, admin_ui.py)
       can branch on permissions without re-reading YAML every page.

Mechanism: Streamlit's native st.login("google").

    This is the original working flow. It was broken briefly when Authlib
    1.7.0 (Apr 2026) tightened OAuth state validation in a way that
    raised MismatchingStateError on every callback — that's why
    requirements.txt pins Authlib<1.7. With that pin in place, native
    auth works again, and we use it directly.

Required setup (already configured for production):

    Google Cloud Console → APIs & Services → Credentials → OAuth client
    → Authorized redirect URIs:

        https://chap-seller-tracker.streamlit.app/oauth2callback

    Streamlit Cloud → app → Settings → Secrets:

        [auth]
        redirect_uri  = "https://chap-seller-tracker.streamlit.app/oauth2callback"
        cookie_secret = "<32+ random bytes — generate with `python -c 'import secrets; print(secrets.token_urlsafe(48))'`>"

        [auth.google]
        client_id           = "<from Google Cloud console>"
        client_secret       = "<from Google Cloud console>"
        server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"

    Notes:
    - cookie_secret MUST be set explicitly. Without it Streamlit generates
      a random one per process — and on Streamlit Cloud the worker can
      reboot between Sign-in click and OAuth callback, which makes the
      state cookie unverifiable.
    - server_metadata_url is required by st.login("google") to resolve
      Google's OIDC endpoints (token, userinfo, jwks).
    - The [auth.google] block must be a nested table; flat [auth] with
      client_id/secret only works for st.login() with no provider arg.

Local dev (without real Google creds):
    Set AUTH_DEV_EMAIL=hsrivastava@threecolts.com in .env. The gate
    accepts that identity without doing OAuth. Only active when no
    [auth] secrets are configured AND we're not on Streamlit Cloud.
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

    On failure (not signed in, wrong domain) calls `st.stop()`. Callers
    MUST NOT call set_page_config before this — the login screen calls
    it itself.
    """
    import streamlit as st  # heavy; not needed in tests

    principal = st.session_state.get("_principal")
    if principal:
        return principal

    email = _email_from_native_user(st)

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
# Internals
# --------------------------------------------------------------------
def _email_from_native_user(st) -> Optional[str]:
    """Read the signed-in email from Streamlit's native st.user.

    Streamlit 1.42+ exposes the OAuth identity at `st.user`. On 1.56
    only `st.user` exists (not `st.experimental_user`). Returns None
    if not logged in.
    """
    try:
        user = getattr(st, "user", None)
        if user is None:
            return None
        if not bool(getattr(user, "is_logged_in", False)):
            return None
        email = getattr(user, "email", None)
        return email.strip() if email else None
    except Exception:
        return None


def _has_auth_secrets(st) -> bool:
    """True if Streamlit secrets has the [auth] block + a Google provider."""
    try:
        auth = st.secrets.get("auth", {})
    except Exception:
        return False
    if not auth:
        return False
    needed = {"redirect_uri", "cookie_secret"}
    if not needed.issubset(set(auth.keys())):
        return False
    google = {}
    try:
        google = dict(auth.get("google", {}) or {})
    except Exception:
        return False
    return {"client_id", "client_secret"}.issubset(set(google.keys()))


def _is_local_dev(st) -> bool:
    if _has_auth_secrets(st):
        return False
    if os.getenv("STREAMLIT_SHARING_MODE"):
        return False
    return True


# --------------------------------------------------------------------
# Login page rendering — set_page_config FIRST, then everything else.
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

    if not _has_auth_secrets(st):
        if _is_local_dev(st) and not os.getenv("AUTH_DEV_EMAIL"):
            st.info(
                "Local dev: set `AUTH_DEV_EMAIL=you@threecolts.com` in `.env` "
                "to skip the OAuth dance while developing."
            )
        else:
            st.error(
                "OAuth is not configured. The `[auth]` block is missing or "
                "incomplete in Streamlit secrets. See `auth.py` docstring "
                "for the expected shape."
            )
        return

    login = getattr(st, "login", None)
    if not callable(login):
        st.error(
            "This Streamlit version doesn't support `st.login()`. "
            "Streamlit ≥1.42 is required (you should be on 1.56+)."
        )
        return

    if st.button("Sign in with Google", type="primary", use_container_width=True):
        login("google")

    st.caption(
        "Only @threecolts.com identities are permitted. "
        "If you need access, ask the admin (Hrithik)."
    )


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
    st.session_state.pop("_principal", None)
    logout = getattr(st, "logout", None)
    if callable(logout):
        try:
            logout()
            return  # st.logout() reruns the app itself
        except Exception:
            pass
    st.rerun()
