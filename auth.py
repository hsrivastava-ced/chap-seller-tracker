"""
auth.py — Google OAuth gate for the Streamlit dashboard.

What it does:
    1. Forces a Google login before anything else renders.
    2. Verifies the signed-in email is on @threecolts.com.
    3. Looks up the user's role in roles.yaml and stashes a UserPrincipal
       in st.session_state so the rest of the app (dashboard.py, admin_ui.py)
       can branch on permissions without re-reading YAML every page.

Streamlit native vs library fallback:
    Streamlit ≥ 1.42 ships a native `st.login("google")` + `st.experimental_user`
    that we prefer. If the runtime is older, or `st.login` isn't available
    for any reason, we fall back to the `streamlit-oauth` library using the
    same Google Cloud client. Callers never have to know which path ran;
    `gate()` is the single entry point.

Setup (done ONCE in Streamlit Cloud → app → Settings → Secrets):

    [auth]
    redirect_uri  = "https://chap-seller-tracker.streamlit.app/oauth2callback"
    cookie_secret = "<32+ random bytes — generate with `python -c 'import secrets; print(secrets.token_urlsafe(48))'`>"

    [auth.google]
    client_id           = "<from Google Cloud console>"
    client_secret       = "<from Google Cloud console>"
    server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"

    NOTE: the provider must live in a nested `[auth.google]` table because
    `gate()` calls `st.login("google")` (named provider). A flat `[auth]`
    block with client_id/secret at the top level only works when calling
    `st.login()` with no args, which we don't.

    NOTE: `cookie_secret` MUST be set explicitly. If it's missing, Streamlit
    generates a random one per process — and on Streamlit Cloud the app
    reboots between Sign-in click and OAuth callback, which makes the state
    cookie unverifiable and produces `MismatchingStateError` (CSRF Warning).

    NOTE: the redirect_uri value here MUST exactly match the one registered
    in Google Cloud Console → APIs & Services → Credentials → OAuth client
    → Authorized redirect URIs. No trailing slash, no `~/+/` prefix.

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

    On failure (not signed in, wrong domain, malformed role file) this
    function calls `st.stop()` so the rest of the page never renders.
    The caller can assume a non-None principal when `gate()` returns.
    """
    import streamlit as st  # local import: streamlit is heavy and the
                            # tests in this repo don't need it

    # Cache principal in session_state — we only need to resolve once
    # per user session. This also means role changes require the user
    # to refresh; that's fine (and matches the design).
    principal = st.session_state.get("_principal")
    if principal:
        return principal

    email = _resolve_email(st)
    if not email:
        _render_login_prompt(st)
        st.stop()

    principal = roles.principal_for(email)
    if principal is None:
        # Valid Google sign-in, wrong domain.
        _render_domain_reject(st, email)
        st.stop()

    st.session_state["_principal"] = principal
    return principal


def require(action: str, principal: Optional[UserPrincipal] = None) -> UserPrincipal:
    """Assert that the current principal has permission for `action`.

    Used inside pages that are already past `gate()` but want to block
    a sub-capability. Example:

        p = auth.gate()
        auth.require("add_app", p)   # raises st.stop() if not editor+
    """
    import streamlit as st
    if principal is None:
        principal = st.session_state.get("_principal")
    if not roles.can(principal, action):
        st.error("You don't have permission to do that.")
        st.stop()
    return principal


def sign_out_button(st=None, *, skip_caption: bool = False):
    """Render a small 'Sign out' button in the sidebar.

    Clears the session and kicks the user back to the login screen.

    `skip_caption=True` is used by pages that already render their own
    user card (Intelligence, Dashboard) — those pages show the email
    + role in a styled card, so the bare-text "Signed in as" caption
    just creates visual noise above the card. When True, only the
    Sign out button is rendered.
    """
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
def _resolve_email(st) -> Optional[str]:
    """Try, in order: native st.experimental_user → streamlit-oauth → local-dev env."""
    # 1. Native Streamlit auth (1.42+)
    email = _resolve_email_native(st)
    if email:
        return email

    # 2. Library fallback (streamlit-oauth + Google OIDC)
    email = _resolve_email_library(st)
    if email:
        return email

    # 3. Local dev escape hatch (explicit env var, only if no [auth] secrets)
    if _is_local_dev(st):
        dev = os.getenv("AUTH_DEV_EMAIL", "").strip()
        if dev:
            return dev
    return None


def _resolve_email_native(st) -> Optional[str]:
    """Use `st.login` + `st.user` (1.42+) if available.

    Streamlit 1.42 renamed the identity attribute from `st.experimental_user`
    to `st.user`. On 1.56 (our pinned version) only `st.user` exists, so
    reading `experimental_user` silently returned None and trapped us in
    a login→redirect→login loop after successful OAuth.
    """
    try:
        user = getattr(st, "user", None) or getattr(st, "experimental_user", None)
        if user is None:
            return None
        is_logged_in = bool(getattr(user, "is_logged_in", False))
        if not is_logged_in:
            # Expose st.login on the prompt screen — render_login_prompt
            # calls it directly. Here we just report "not logged in".
            return None
        email = getattr(user, "email", None)
        return email.strip() if email else None
    except Exception:
        return None


def _resolve_email_library(st) -> Optional[str]:
    """Fallback: streamlit-oauth against the same Google client.

    We lazy-import so environments that don't install the package
    still work via the native path or local-dev env var.
    """
    try:
        from streamlit_oauth import OAuth2Component  # type: ignore
    except Exception:
        return None
    cfg = _auth_config(st)
    if not cfg:
        return None

    oauth = OAuth2Component(
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        authorize_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        refresh_token_endpoint="https://oauth2.googleapis.com/token",
        revoke_token_endpoint="https://oauth2.googleapis.com/revoke",
    )
    token = st.session_state.get("_oauth_token")
    if not token:
        # Render the button — user clicks, gets redirected to Google,
        # comes back with a code, we exchange it for a token.
        result = oauth.authorize_button(
            "Sign in with Google",
            redirect_uri=cfg["redirect_uri"],
            scope="openid email profile",
            use_container_width=True,
        )
        if not result or "token" not in result:
            return None
        token = result["token"]
        st.session_state["_oauth_token"] = token

    # Decode the id_token to pull the email. id_tokens are signed JWTs;
    # we validate by round-tripping through Google's userinfo endpoint
    # (simpler than carrying a JWT-verify dep).
    import requests
    try:
        r = requests.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {token['access_token']}"},
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
    """Return the [auth] secrets block or None if not configured."""
    try:
        cfg = dict(st.secrets.get("auth", {}))
    except Exception:
        return None
    needed = {"client_id", "client_secret", "redirect_uri"}
    if not needed.issubset(cfg.keys()):
        return None
    return cfg


def _is_local_dev(st) -> bool:
    """Rough heuristic: no [auth] block in secrets AND we're not on Streamlit Cloud."""
    if _auth_config(st):
        return False
    # Streamlit Cloud sets STREAMLIT_SERVER_PORT + STREAMLIT_SHARING_MODE env
    # vars; on prod we definitely should never take the local-dev branch.
    if os.getenv("STREAMLIT_SHARING_MODE"):
        return False
    return True


# --------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------
def _render_login_prompt(st) -> None:
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
    # Native path: Streamlit 1.42+ exposes st.login
    login = getattr(st, "login", None)
    if callable(login):
        if st.button("Sign in with Google", type="primary", use_container_width=True):
            login("google")
        st.caption(
            "Only @threecolts.com identities are permitted. "
            "If you need access, ask the admin (Hrithik)."
        )
        return

    # Library fallback: attempt to call _resolve_email_library once —
    # it renders its own button.
    email = _resolve_email_library(st)
    if email:
        # Edge case: library path returned an email on this render.
        st.session_state["_pending_email"] = email
        st.rerun()

    # Local-dev path hint
    if _is_local_dev(st) and not os.getenv("AUTH_DEV_EMAIL"):
        st.info(
            "Local dev: set `AUTH_DEV_EMAIL=you@threecolts.com` in `.env` "
            "to skip the OAuth dance while developing."
        )


def _render_domain_reject(st, email: str) -> None:
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
        logout()
    st.rerun()
