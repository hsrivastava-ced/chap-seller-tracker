"""
email_notifications.py — minimal SMTP helper for the auth flow.

Reads SMTP config from Streamlit secrets (or env vars in local dev) and
sends transactional notifications when:

  - someone requests access (admin gets a "new access request" email)
  - admin approves / denies a request (the user gets the verdict)

Designed to fail silently if SMTP isn't configured — login + signup must
keep working even when emails can't go out. Failures are logged.

Streamlit secrets shape (add this in Streamlit Cloud → app → Settings → Secrets):

    [smtp]
    host        = "smtp.gmail.com"
    port        = 465
    username    = "hsrivastava@threecolts.com"
    password    = "<Gmail app password — 16 chars no spaces>"
    from_addr   = "hsrivastava@threecolts.com"   # optional, defaults to username
    admin_email = "hsrivastava@threecolts.com"

For Gmail you MUST use an App Password (Google Account → Security → 2-Step
Verification → App passwords), not your regular Google password. Free tier
allows 500 emails/day — plenty for this internal flow.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional


_LOGGER = logging.getLogger(__name__)


def _smtp_config() -> Optional[dict]:
    """Return the SMTP config dict, or None if not configured.

    Tries Streamlit secrets first, falls back to env vars (for local dev /
    cron jobs that import this module outside a Streamlit context).
    """
    try:
        import streamlit as st
        cfg = dict(st.secrets.get("smtp", {}) or {})
        if cfg:
            return _normalize(cfg)
    except Exception:
        pass

    # Env-var fallback
    if os.getenv("SMTP_HOST") and os.getenv("SMTP_USERNAME"):
        return _normalize({
            "host": os.getenv("SMTP_HOST"),
            "port": int(os.getenv("SMTP_PORT", "465")),
            "username": os.getenv("SMTP_USERNAME"),
            "password": os.getenv("SMTP_PASSWORD", ""),
            "from_addr": os.getenv("SMTP_FROM", os.getenv("SMTP_USERNAME", "")),
            "admin_email": os.getenv("SMTP_ADMIN_EMAIL", os.getenv("SMTP_USERNAME", "")),
        })
    return None


def _normalize(cfg: dict) -> dict:
    out = dict(cfg)
    out.setdefault("port", 465)
    try:
        out["port"] = int(out["port"])
    except (TypeError, ValueError):
        out["port"] = 465
    out.setdefault("from_addr", out.get("username", ""))
    return out


def admin_email() -> Optional[str]:
    cfg = _smtp_config()
    return (cfg or {}).get("admin_email") or None


def send_email(*, to: str, subject: str, body: str) -> bool:
    """Send a single plain-text email. Returns True on success.

    No-op + logs a warning if SMTP isn't configured. The auth flow keeps
    working without email; admins just won't get notified automatically.
    """
    if not to:
        return False
    cfg = _smtp_config()
    if not cfg:
        _LOGGER.warning("SMTP not configured — would have emailed %s: %s", to, subject)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.get("from_addr", cfg["username"])
    msg["To"] = to
    msg.set_content(body)

    try:
        port = int(cfg.get("port", 465))
        if port == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg["host"], port, context=ctx, timeout=10) as server:
                server.login(cfg["username"], cfg.get("password", ""))
                server.send_message(msg)
        else:
            with smtplib.SMTP(cfg["host"], port, timeout=10) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(cfg["username"], cfg.get("password", ""))
                server.send_message(msg)
        return True
    except Exception as err:
        _LOGGER.error("send_email to %s failed: %s", to, err)
        return False


# --------------------------------------------------------------------
# Pre-formatted notifications
# --------------------------------------------------------------------
APP_NAME = "cHAP Seller Tracker"
APP_URL = "https://chap-seller-tracker.streamlit.app/"


def notify_admin_new_signup(*, requester_email: str, requester_name: str) -> bool:
    target = admin_email()
    if not target:
        return False
    name = requester_name.strip() or "(no name)"
    body = (
        f"New access request for {APP_NAME}.\n\n"
        f"  Email: {requester_email}\n"
        f"  Name:  {name}\n\n"
        f"Approve or deny in the Admin → Access tab:\n"
        f"  {APP_URL}\n"
    )
    return send_email(
        to=target,
        subject=f"[{APP_NAME}] Access request from {requester_email}",
        body=body,
    )


def notify_user_approved(*, user_email: str) -> bool:
    body = (
        f"Your access request for {APP_NAME} has been approved.\n\n"
        f"You can now sign in at:\n  {APP_URL}\n"
    )
    return send_email(
        to=user_email,
        subject=f"[{APP_NAME}] Access approved",
        body=body,
    )


def notify_user_denied(*, user_email: str) -> bool:
    body = (
        f"Your access request for {APP_NAME} has not been approved.\n\n"
        f"If you think this is a mistake, reach out to the admin "
        f"(Hrithik) directly.\n"
    )
    return send_email(
        to=user_email,
        subject=f"[{APP_NAME}] Access not approved",
        body=body,
    )
