"""
ui_errors.py — uniform, friendly error rendering for Streamlit pages.

Two goals:
  1. Every error the user sees reads as plain English with a clear next
     step, not a raw traceback or a 403 JSON blob.
  2. Severity is calibrated: config-fixable issues show as yellow
     warnings, unexpected failures as red errors. This avoids training
     the user to ignore red (the default for everything on Streamlit).

Usage:

    from ui_errors import show_error, show_warning, wrap_page

    @wrap_page                               # global safety net
    def main():
        ...

    try:
        risky_github_call()
    except Exception as e:
        show_warning(
            "Couldn't save the schedule.",
            hint="Your GitHub PAT is missing the Workflows permission. "
                 "Grant it under Settings → PATs and try again.",
            cause=e,
        )
        return

Design notes:
  - Technical details go in a collapsed expander. The user sees a short
    sentence; the admin (or a developer opening the page) can expand
    for the raw message.
  - Every non-info message includes a contact line. The user explicitly
    asked for "ask them to contact me" — CONTACT_EMAIL below is the
    owner's address.
  - No CSS hacks. Streamlit's default alerts respect light/dark theme;
    overriding colors looks great in one mode and broken in the other.
    What makes these cards *feel* better is short prose + severity
    calibration, not palette surgery.
"""
from __future__ import annotations

import functools
import traceback
from typing import Callable, Optional, Union

import streamlit as st

# Single place to change if the super-admin's address ever moves.
# Matches roles.HARD_CODED_SUPER_ADMINS.
CONTACT_EMAIL = "hsrivastava@threecolts.com"


def show_error(
    title: str,
    *,
    hint: Optional[str] = None,
    cause: Union[Exception, str, None] = None,
) -> None:
    """Red alert for genuine failures (bugs, unexpected network issues).

    Use `show_warning` instead for anything the user can fix by tweaking
    config — warnings don't train the user to ignore red.
    """
    _render("error", title, hint=hint, cause=cause, show_contact=True)


def show_warning(
    title: str,
    *,
    hint: Optional[str] = None,
    cause: Union[Exception, str, None] = None,
) -> None:
    """Yellow alert for fixable config / permission issues."""
    _render("warning", title, hint=hint, cause=cause, show_contact=True)


def show_info(
    title: str,
    *,
    hint: Optional[str] = None,
) -> None:
    """Blue alert for expected states the user should know about (e.g.
    'Supabase in dry-run mode — data won't persist')."""
    _render("info", title, hint=hint, cause=None, show_contact=False)


def _render(
    severity: str,
    title: str,
    *,
    hint: Optional[str],
    cause: Union[Exception, str, None],
    show_contact: bool,
) -> None:
    render_fn = {
        "error":   st.error,
        "warning": st.warning,
        "info":    st.info,
    }[severity]

    # Compose the visible message. Keep the first line to one sentence —
    # users scan, they don't read.
    body = title
    if hint:
        body += f"\n\n**What to try:**  {hint}"
    render_fn(body)

    if show_contact:
        st.caption(
            f"If it keeps happening, email **{CONTACT_EMAIL}** with a "
            f"screenshot of this page — include the technical details below."
        )

    if cause is not None:
        with st.expander("Technical details", expanded=False):
            if isinstance(cause, Exception):
                st.text(f"{type(cause).__name__}: {cause}")
            else:
                st.text(str(cause))


# ---------------------------------------------------------------------
# Global safety net — catches anything we forgot to handle explicitly
# ---------------------------------------------------------------------
def wrap_page(fn: Callable) -> Callable:
    """Decorator: catches uncaught exceptions in a Streamlit page entry
    point and renders them via show_error instead of Streamlit's raw
    red traceback block.

    Preserves control-flow exceptions (st.stop, st.rerun) so Streamlit's
    own machinery keeps working.
    """
    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BaseException as exc:
            # Streamlit uses custom exceptions for control flow.
            # Check by class name — the import path moves between
            # Streamlit versions; the name is stable.
            if type(exc).__name__ in (
                "StopException",
                "RerunException",
                "RerunData",  # older Streamlit
            ):
                raise
            # Log full traceback to the Streamlit Cloud logs so the
            # admin can actually debug.
            traceback.print_exc()
            show_error(
                "Something went wrong on this page.",
                hint=(
                    "Try refreshing the browser tab. If the error keeps "
                    "happening, send the admin a screenshot including "
                    "the technical details below."
                ),
                cause=exc,
            )
            st.stop()
    return _wrapped
