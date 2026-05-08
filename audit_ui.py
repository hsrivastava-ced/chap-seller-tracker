"""
audit_ui.py — Streamlit page for the audit / activity dashboard.

Three tabs:
  - 🟢 Active now      — heartbeats within the last 5 minutes
  - 🔑 Login history   — last N successful sign-ins, filterable by email
  - 📜 Activity log    — last N actions, filterable by email/console/action

Gated to super_admin (cHAP) only — viewers and editors do NOT see who
else is signed in. Per Hrithik's "owner sees the whole picture, no one
else does" preference (the audit page is for the project owner / CEO).
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st

import audit
import auth
import roles
from ui_errors import wrap_page
from ui_theme import (
    PALETTE,
    apply_shared_theme,
    tc_kpi as _kpi,
    tc_section as _section,
)


def _gate() -> auth.UserPrincipal:
    principal = auth.gate()
    if not roles.can(principal, "see_users_tab"):  # super_admin only
        st.set_page_config(
            page_title="Audit — access required",
            page_icon=":lock:",
            layout="centered",
        )
        st.error(
            f"Your account `{principal.email}` doesn't have access to "
            f"the audit page."
        )
        st.caption(
            "The audit page lists every login + page view + action across "
            "both consoles, so it's restricted to super-admins. Ask a "
            "super admin if you need a specific export."
        )
        if st.button("Sign out", key="audit_denied_signout"):
            auth._do_sign_out(st)
        st.stop()
    return principal


@wrap_page
def main() -> None:
    principal = _gate()

    st.set_page_config(
        page_title="Audit — cHAP",
        page_icon="📜",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    apply_shared_theme()

    audit.heartbeat(
        principal.email, console="chap", page="Audit",
        user_agent=audit.current_user_agent(st),
    )

    st.title("📜 Audit")
    st.caption(
        "Who's signed in right now, who logged in recently, and what "
        "every editor / viewer has been doing across both consoles. "
        "Backed by `public.login_log`, `public.activity_log`, and "
        "`public.active_sessions` (sql/005_audit_log.sql)."
    )

    with st.sidebar:
        auth.sign_out_button(st)

    tab_active, tab_logins, tab_activity = st.tabs([
        "🟢 Active now", "🔑 Login history", "📜 Activity log",
    ])

    with tab_active:
        _render_active_tab()
    with tab_logins:
        _render_logins_tab()
    with tab_activity:
        _render_activity_tab()


# ---------------------------------------------------------------------
# Tab 1 — active right now
# ---------------------------------------------------------------------
def _render_active_tab() -> None:
    col_window, col_refresh = st.columns([3, 1])
    window_minutes = int(col_window.slider(
        "Active window (minutes)",
        min_value=1, max_value=60, value=5, step=1,
        help="Treat anyone whose last_seen_at is within this many "
             "minutes as 'active'. Streamlit heartbeats throttle at "
             "60s, so going below 1 min may show false negatives."
    ))
    if col_refresh.button("🔄 Refresh", use_container_width=True, key="audit_active_refresh"):
        st.rerun()

    rows = audit.fetch_active(window_minutes=window_minutes)

    chap = [r for r in rows if r.get("console") == "chap"]
    cedadmin = [r for r in rows if r.get("console") == "cedadmin"]

    cols = st.columns(3)
    _kpi(
        cols[0],
        label="🟢 Active now",
        value=f"{len(rows):,}",
        sub=f"within last {window_minutes} min",
        color="#22c55e",
        help_text="Distinct (email, console) sessions seen recently.",
    )
    _kpi(
        cols[1],
        label="📊 cHAP",
        value=f"{len(chap):,}",
        sub="sessions on chap-seller-tracker",
        color="#3b82f6",
        help_text="Active sessions on the cHAP console (Dashboard / "
                  "Admin / Intelligence).",
    )
    _kpi(
        cols[2],
        label="🛒 cedadmin",
        value=f"{len(cedadmin):,}",
        sub="sessions on cedcommerce-admin",
        color="#a78bfa",
        help_text="Active sessions on the cedadmin console (separate URL).",
    )

    if not rows:
        st.info(
            "No-one is active right now. Either the table hasn't been "
            "populated yet (sql/005_audit_log.sql may not have been "
            "applied), or everyone is genuinely idle."
        )
        return

    st.write("")
    _section(
        "Live sessions",
        sub="One row per (email, console). Page = where they were last seen.",
    )

    df = pd.DataFrame([
        {
            "Email":       r.get("email"),
            "Console":     r.get("console"),
            "Page":        r.get("page"),
            "Last seen":   r.get("last_seen_at"),
            "Started":     r.get("started_at"),
            "User-agent":  _short_ua(r.get("user_agent")),
        }
        for r in rows
    ])
    st.dataframe(
        df, hide_index=True, use_container_width=True,
        column_config={
            "Last seen": st.column_config.DatetimeColumn(
                "Last seen", help="UTC. Heartbeats every ~60s.",
            ),
            "Started":   st.column_config.DatetimeColumn(
                "Session start",
                help="When the user's first heartbeat for this console "
                     "in the current session landed.",
            ),
        },
    )


# ---------------------------------------------------------------------
# Tab 2 — login history
# ---------------------------------------------------------------------
def _render_logins_tab() -> None:
    col_email, col_limit = st.columns([3, 1])
    email_filter = col_email.text_input(
        "Filter by email (exact match, blank = all)",
        "",
        key="audit_login_filter",
        help="Lowercase. Substring match isn't supported on this tab "
             "to keep the index hits cheap.",
    ).strip().lower()
    limit = int(col_limit.number_input(
        "Limit",
        min_value=10, max_value=500, value=50, step=10,
        key="audit_login_limit",
    ))

    rows = audit.fetch_recent_logins(limit=limit, email=email_filter or None)

    if not rows:
        if email_filter:
            st.caption(f"No logins recorded for `{email_filter}`.")
        else:
            st.info(
                "No login history yet. The first login after "
                "`sql/005_audit_log.sql` is applied will start populating "
                "this tab."
            )
        return

    df = pd.DataFrame([
        {
            "When":         r.get("logged_in_at"),
            "Email":        r.get("email"),
            "Console":      r.get("console"),
            "IP":           r.get("ip") or "—",
            "User-agent":   _short_ua(r.get("user_agent")),
        }
        for r in rows
    ])
    st.dataframe(
        df, hide_index=True, use_container_width=True,
        column_config={
            "When": st.column_config.DatetimeColumn(
                "When", help="UTC.",
            ),
            "IP":   st.column_config.TextColumn(
                "IP",
                help="Best-effort — pulled from X-Forwarded-For. Blank "
                     "rows = local-dev sign-in or a header that wasn't "
                     "forwarded.",
            ),
        },
    )

    st.caption(f"{len(rows):,} login(s) shown. Pulled from `public.login_log`.")


# ---------------------------------------------------------------------
# Tab 3 — activity log
# ---------------------------------------------------------------------
_ACTION_OPTIONS = (
    "(any)",
    "page_view",
    "manual_edit",
    "csv_download",
    "grant_role",
    "revoke_role",
    "scrape_dispatch",
    "app_filter_change",
)
_CONSOLE_OPTIONS = ("(any)", "chap", "cedadmin")


def _render_activity_tab() -> None:
    col_email, col_action, col_console, col_window = st.columns(4)
    email_filter = col_email.text_input(
        "Email (exact)", "", key="audit_act_email",
    ).strip().lower()
    action_filter = col_action.selectbox(
        "Action", options=_ACTION_OPTIONS, index=0,
        key="audit_act_action",
        help="page_view = navigation. Others = edits / downloads / "
             "grants / dispatches.",
    )
    console_filter = col_console.selectbox(
        "Console", options=_CONSOLE_OPTIONS, index=0,
        key="audit_act_console",
    )
    window_hours = int(col_window.number_input(
        "Window (hours)",
        min_value=1, max_value=24 * 30, value=24, step=1,
        key="audit_act_hours",
        help="Rows newer than this many hours. Default 24h.",
    ))

    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    rows = audit.fetch_recent_activity(
        limit=500,
        email=email_filter or None,
        action=action_filter if action_filter != "(any)" else None,
        console=console_filter if console_filter != "(any)" else None,
        since=since,
    )

    # Summary KPIs across the filtered set.
    cols = st.columns(4)
    _kpi(
        cols[0],
        label="📜 Events",
        value=f"{len(rows):,}",
        sub=f"in last {window_hours}h",
        color="#3b82f6",
        help_text="Total filtered activity rows.",
    )
    distinct_users = len({r.get("email") for r in rows if r.get("email")})
    _kpi(
        cols[1],
        label="👥 Distinct users",
        value=f"{distinct_users:,}",
        sub="who triggered an event",
        color="#a78bfa",
        help_text="Distinct emails appearing in the filtered set.",
    )
    edits = sum(1 for r in rows if r.get("action") == "manual_edit")
    _kpi(
        cols[2],
        label="✏️ Edits",
        value=f"{edits:,}",
        sub="manual_edit rows",
        color="#f59e0b",
        help_text="Manual seller-record edits via Intelligence.",
    )
    downloads = sum(1 for r in rows if r.get("action") == "csv_download")
    _kpi(
        cols[3],
        label="⬇ Downloads",
        value=f"{downloads:,}",
        sub="CSV exports",
        color="#22c55e",
        help_text="CSV downloads from Intelligence / Sellers tabs.",
    )

    if not rows:
        st.info("No activity matches these filters.")
        return

    st.write("")
    df = pd.DataFrame([
        {
            "When":     r.get("occurred_at"),
            "Email":    r.get("email"),
            "Console":  r.get("console"),
            "Page":     r.get("page"),
            "Action":   r.get("action"),
            "Target":   _format_target(r),
            "Details":  _format_details(r.get("details")),
        }
        for r in rows
    ])
    st.dataframe(
        df, hide_index=True, use_container_width=True,
        column_config={
            "When": st.column_config.DatetimeColumn("When", help="UTC."),
            "Action": st.column_config.TextColumn(
                "Action",
                help="page_view = navigated to a page. manual_edit = "
                     "wrote to public.sellers. csv_download = exported. "
                     "grant_role / revoke_role = access change.",
            ),
            "Target": st.column_config.TextColumn(
                "Target",
                help="What the action acted on — seller_id, app_id, "
                     "user email, etc.",
            ),
        },
    )

    st.caption(f"{len(rows):,} event(s). Pulled from `public.activity_log`.")


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------
def _short_ua(ua: str | None) -> str:
    """Compact user-agent for tables — full string is too long to
    read at a glance. Keeps the browser + OS, drops version fluff."""
    if not ua:
        return "—"
    # Cheap heuristic: pick the first parenthetical (OS) and the last
    # token containing 'Chrome|Safari|Firefox|Edge|Opera'.
    s = ua
    bits = []
    if "(" in s and ")" in s:
        os_part = s.split("(", 1)[1].split(")", 1)[0]
        bits.append(os_part)
    for browser in ("Edg/", "Chrome/", "Firefox/", "Safari/", "Opera/"):
        if browser in s:
            tail = s[s.index(browser):].split(" ", 1)[0]
            bits.append(tail.rstrip("/"))
            break
    return " · ".join(bits) or s[:60]


def _format_target(r: dict) -> str:
    tt = r.get("target_type")
    tid = r.get("target_id")
    if tt and tid:
        return f"{tt}:{tid}"
    if tid:
        return str(tid)
    return "—"


def _format_details(d) -> str:
    if not d:
        return "—"
    if isinstance(d, dict):
        bits = []
        for k, v in d.items():
            if v is None or v == "":
                continue
            sv = str(v)
            if len(sv) > 40:
                sv = sv[:37] + "…"
            bits.append(f"{k}={sv}")
        return " · ".join(bits) or "—"
    return str(d)
