"""
admin_ui.py — Streamlit page for app onboarding + user management.

Usage:
    Linked from dashboard.py as a secondary page:
        st.Page("admin_ui.py", title="Admin", icon=":gear:", url_path="admin")

    For development you can also run it standalone:
        AUTH_DEV_EMAIL=hsrivastava@threecolts.com streamlit run admin_ui.py

Layout:
    ┌─────────────────────────────────────────────────┐
    │ Admin                                           │
    │                                                 │
    │ [ Apps ]  [ Users (super_admin only) ]          │
    │                                                 │
    │ --- Apps tab -------------------------------- │
    │  Table of current apps with status badges       │
    │  "Add new app" single form (editor+):           │
    │   name · id · dropdown · creds · scrape toggles │
    │   → commits apps.yaml + CREDS secret, dispatches │
    │                                                 │
    │ --- Users tab ------------------------------- │
    │  List (email, role, grantor, granted_at)        │
    │  Grant / revoke form (super_admin only)         │
    └─────────────────────────────────────────────────┘

Kept dependency-light: imports the local modules (auth, roles,
app_registry, github_secret_updater) plus streamlit + yaml. No Playwright
dependency — schema validation happens in the first real scrape (via
scrape_validator + schema_guard inside the GitHub Actions run), so the
admin UI never has to spawn a browser.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import streamlit as st

import app_registry
import auth
import github_secret_updater as gh
import roles
from app_registry import AppEntry
from ui_errors import show_error, show_warning, show_info, wrap_page
from ui_theme import apply_shared_theme, render_theme_picker


# =================================================================
# Constants — tied to cHAP, not generic
# =================================================================
# The admin UI only works against the CedCommerce cHAP admin panel.
# Everything below (supported app list, field shapes) assumes that.
CHAP_LOGIN_URL = "https://app-v2-frontend.cifapps.com/auth/login"

# List of values emitted by the cHAP login page's app-picker dropdown.
# These are stable identifiers — the cHAP team adds entries when new
# marketplaces come online. Update this list as new ones launch.
# Labels are the "nice" display names (with occasional disambiguation
# for regional / framework variants).
SUPPORTED_APPS: list[dict] = [
    {"value": "aliexpress",                 "label": "AliExpress"},
    {"value": "bigcommerce",                "label": "BigCommerce"},
    {"value": "global_mcf",                 "label": "Global MCF"},
    {"value": "google_shopify_express",     "label": "Google Shopify Express"},
    {"value": "joom",                       "label": "Joom"},
    {"value": "michael",                    "label": "Michael"},
    {"value": "mirakl_woocommerce",         "label": "Mirakl WooCommerce"},
    {"value": "mirakl_woocommerce_staging", "label": "Mirakl WooCommerce (Staging)"},
    {"value": "miravia",                    "label": "Miravia"},
    {"value": "shein",                      "label": "SHEIN"},
    {"value": "shein_woocommerce",          "label": "SHEIN WooCommerce"},
    {"value": "shopify_gearexchange",       "label": "Shopify GearExchange"},
    {"value": "shopify_temu",               "label": "TEMU US (Shopify)"},
    {"value": "shopify_temu_eu",            "label": "TEMU EU (Shopify)"},
    {"value": "shopline-catch",             "label": "Shopline Catch"},
    {"value": "shopline_amazon",            "label": "Shopline Amazon"},
    {"value": "shopline_ebay",              "label": "Shopline eBay"},
    {"value": "tiktok",                     "label": "TikTok"},
    {"value": "trendyol",                   "label": "Trendyol"},
    {"value": "zoho",                       "label": "Zoho"},
]

# Frequency choices for the global scrape schedule. GitHub Actions cron
# is UTC. 12h option keeps the existing 00:00 / 12:00 IST alignment for
# familiarity; 6h / 24h use simple UTC intervals. Minimum 6h is a
# product constraint (cHAP rate-limits, plus our own scrape run takes
# ~5 min per app).
FREQ_CHOICES: dict[str, dict] = {
    "Every 6 hours (4× per day)": {
        "hours": 6,
        "crons": [("0 */6 * * *", "every 6 hours (UTC)")],
    },
    "Every 12 hours (00:00 + 12:00 IST)": {
        "hours": 12,
        "crons": [
            ("30 18 * * *", "00:00 IST = 18:30 UTC previous day"),
            ("30 6 * * *",  "12:00 IST = 06:30 UTC same day"),
        ],
    },
    "Once per day (00:00 IST)": {
        "hours": 24,
        "crons": [("30 18 * * *", "00:00 IST = 18:30 UTC previous day")],
    },
}


# Schedule options presented in the Add-new-app form. The first entry is
# the default (shared with the main scrape.yml cron — no per-app
# workflow file created). The others each commit a dedicated
# .github/workflows/scrape_<app_id>.yml running on its own cron.
_ADD_APP_SCHEDULE_CHOICES: dict[str, dict] = {
    "Shared schedule — runs with other apps (twice daily, 00:00 & 12:00 IST)": {
        "shared": True,
        "cron": None,
        "summary": "shared 12h",
    },
    "Solo — every 6 hours (4× per day)": {
        "shared": False,
        "cron": "0 */6 * * *",
        "summary": "every 6 hours",
    },
    "Solo — once a day at 00:00 IST": {
        "shared": False,
        "cron": "30 18 * * *",
        "summary": "daily at 00:00 IST",
    },
    "Solo — once a day at 12:00 IST": {
        "shared": False,
        "cron": "30 6 * * *",
        "summary": "daily at 12:00 IST",
    },
}


# =================================================================
# Page entry
# =================================================================
@wrap_page
def main():
    principal = auth.gate()
    auth.require("see_admin_tab", principal)

    import audit
    audit.heartbeat(
        principal.email, console="chap", page="Admin",
        user_agent=audit.current_user_agent(st),
    )

    st.set_page_config(page_title="Admin — cHAP Seller Tracker", page_icon=":gear:", layout="wide")
    apply_shared_theme()  # same sidebar / button look as Dashboard

    st.title("Admin")
    st.caption(
        "Each tab does one thing. Changes commit back to the repo and "
        "Streamlit Cloud redeploys within ~30 s."
    )

    # Tab structure (split by concern rather than role — tabs the user
    # doesn't have permission for still show, just with a polite
    # "view-only" message inside):
    #   - Overview        — configured apps + Run scrape now
    #   - Add new app     — focused form (requires vault unlocked)
    #   - Settings        — credential vault + scrape schedule
    #   - Runs            — live history from GitHub Actions
    #   - Users           — super-admin only, hidden otherwise
    tab_labels = ["Overview", "Add new app", "Settings", "Runs"]
    show_access_tab = roles.can(principal, "see_users_tab")
    if show_access_tab:
        tab_labels.append("Access")
        tab_labels.append("Users")
    tabs = st.tabs(tab_labels)

    with tabs[0]:
        _render_overview_tab(principal)
    with tabs[1]:
        _render_add_app_tab(principal)
    with tabs[2]:
        _render_settings_tab(principal)
    with tabs[3]:
        _render_runs_tab(principal)
    if show_access_tab:
        with tabs[4]:
            _render_access_tab(principal)
        with tabs[5]:
            _render_users_tab(principal)

    # Sidebar footer: theme picker + sign out.
    render_theme_picker()
    auth.sign_out_button(st, skip_caption=True)


# =================================================================
# Overview tab — the apps table + Run scrape now
# =================================================================
def _render_app_error_cards(
    principal: roles.UserPrincipal,
    apps: list[AppEntry],
) -> None:
    """Surface per-app login/scrape failures and let the panel owner
    retry once the cHAP-side issue is fixed.

    The flow this supports: an admin onboards a new panel via "Add new
    app" → workflow_dispatch fires a TARGET_APP scrape → scrape fails
    (e.g. cHAP renders an OTP screen) → run.json records the error
    reason → THIS UI shows the reason on the Overview tab → admin asks
    their dev to disable OTP → admin clicks "Retry sync" → workflow
    dispatched again with TARGET_APP → on success the error clears
    automatically.
    """
    import json as _json
    from pathlib import Path as _Path

    latest_path = _Path("results/latest/run.json")
    if not latest_path.exists():
        return
    try:
        latest = _json.loads(latest_path.read_text(encoding="utf-8"))
    except Exception:
        return

    errors = (latest.get("app_errors") or {})
    if not errors:
        return

    # Banner header.
    st.markdown(
        f'<div style="margin:6px 0 10px 0; padding:10px 14px; '
        f'background:rgba(239,68,68,0.08); border-left:4px solid '
        f'#ef4444; border-radius:6px;">'
        f'<b style="color:#dc2626;">⚠ {len(errors)} admin panel'
        f'{"s" if len(errors) != 1 else ""} need'
        f'{"" if len(errors) != 1 else "s"} attention</b><br>'
        f'<span style="color:#64748b; font-size:0.85rem;">'
        f'These panels failed their last scrape. Read the cause, fix '
        f'the cHAP-side issue (often OTP, captcha, or stale credentials), '
        f'then click Retry sync.</span></div>',
        unsafe_allow_html=True,
    )

    apps_by_id = {a.id: a for a in apps}
    can_retry = roles.can(principal, "add_app")
    for app_id, reason in errors.items():
        app = apps_by_id.get(app_id)
        label = app.label if app else app_id
        # Compact per-app card: name, reason, Retry button.
        col_msg, col_btn = st.columns([5, 1])
        with col_msg:
            st.markdown(
                f'<div style="padding:10px 14px; background:#fff; '
                f'border:1px solid #fecaca; border-left:3px solid #ef4444; '
                f'border-radius:8px; margin-bottom:8px;">'
                f'<div style="font-weight:600; color:#0f172a;">'
                f'{label} <span style="color:#94a3b8; font-weight:400;">'
                f'· {app_id}</span></div>'
                f'<div style="color:#dc2626; font-size:0.85rem; margin-top:3px;">'
                f'{reason}</div></div>',
                unsafe_allow_html=True,
            )
        with col_btn:
            if can_retry:
                if st.button("🔁 Retry sync", key=f"retry_{app_id}", use_container_width=True):
                    _retry_sync_for_app(principal, app_id)


def _retry_sync_for_app(principal: roles.UserPrincipal, app_id: str) -> None:
    """Dispatch a TARGET_APP scrape so the failed panel is retried in
    isolation. The scheduler will pick the run up; on success the error
    in latest/run.json's app_errors map clears automatically and this
    card disappears on the next dashboard reload."""
    if not roles.can(principal, "add_app"):
        show_warning(
            "You don't have permission to start a scrape.",
            hint="Ask a super admin to grant you the **editor** role.",
        )
        return
    try:
        ctx = gh.context_from_streamlit(st)
    except Exception as e:
        show_warning(
            "Couldn't reach GitHub to trigger the retry.",
            hint="Streamlit secrets need a `[github]` block with `pat`.",
            cause=e,
        )
        return
    try:
        gh.trigger_scrape(
            ctx,
            reason=f"retry sync for {app_id} by {principal.email}",
            target_app=app_id,
        )
        st.success(
            f"✓ Retry dispatched for **{app_id}**. The scrape runs on "
            "GitHub Actions in ~3–5 min. Refresh this page after that — "
            "if the error clears, the data lands on the dashboard "
            "automatically and the panel owner gets visibility on the "
            "Customer Intelligence page."
        )
    except Exception as e:
        show_warning(
            "Couldn't dispatch the retry.",
            hint="The PAT needs Actions:write on the repo.",
            cause=e,
        )


def _render_scrape_health_banner() -> None:
    """Detect partial-failure scrapes and warn the admin.

    A scheduled cron run can succeed for some apps and fail for others
    (login fails, cHAP-side outage, etc.). The scraper's merge logic
    keeps the prior rows in results/latest/run.json so the dashboard
    doesn't drop to zero — but admins still need to KNOW which apps
    were affected so they can investigate. We compare the run.json's
    history-snapshot run_stamp (what THIS run scraped) against the
    full active-app list from apps.yaml; any active app missing from
    the snapshot's `data` map = a scrape that didn't capture rows.
    """
    import json as _json
    import subprocess as _sp
    from pathlib import Path as _Path

    latest_path = _Path("results/latest/run.json")
    if not latest_path.exists():
        st.info(
            "No scrape data on disk yet. Click **▶ Run scrape now** above "
            "or wait for the next scheduled cron."
        )
        return

    try:
        latest = _json.loads(latest_path.read_text(encoding="utf-8"))
    except Exception as err:
        show_warning(
            "Couldn't read the latest scrape snapshot.",
            cause=err,
        )
        return

    latest_run_stamp = latest.get("run_stamp", "unknown")
    latest_apps = set((latest.get("data") or {}).keys())

    # Pull THIS-RUN-only history snapshot to know which apps were
    # actually scraped (not merged-forward from a prior run). Latest
    # /run.json mixes merged + fresh data; the matching history file
    # only has what this run captured. If the snapshot dir doesn't
    # exist locally (deployments don't track results/history/), fall
    # back to the most recent chore(data) commit's run.json — that's
    # the per-run snapshot before merge.
    fresh_apps: set[str] = set()
    try:
        out = _sp.run(
            ["git", "log", "-n", "10", "--pretty=%H %s",
             "--", "results/latest/run.json"],
            cwd=".", check=True, capture_output=True, text=True, timeout=8,
        )
        for line in (out.stdout or "").splitlines():
            sha, _, msg = line.partition(" ")
            if not msg.startswith("chore(data): scrape"):
                continue
            try:
                blob = _sp.run(
                    ["git", "show", f"{sha}:results/latest/run.json"],
                    cwd=".", check=True, capture_output=True, text=True, timeout=8,
                ).stdout
                fresh = _json.loads(blob)
                fresh_apps = set((fresh.get("data") or {}).keys())
                break
            except Exception:
                continue
    except Exception:
        pass

    active_ids = {a.id for a in (app_registry.all_apps() or [])}
    if not active_ids:
        return

    # An app is "stale" if it's in the active registry but the most
    # recent FRESH scrape didn't capture it. Without the merge logic
    # these would show 0 rows; with it they show prior data. Either
    # way the admin needs to know.
    stale_apps = sorted(active_ids - fresh_apps) if fresh_apps else []
    # An app is "empty" if its latest run.json data is 0 rows.
    empty_apps = sorted(
        app_id for app_id in active_ids
        if app_id in latest_apps and len((latest.get("data") or {}).get(app_id) or []) == 0
    )

    if not stale_apps and not empty_apps:
        st.markdown(
            f'<div style="padding:10px 14px; background:rgba(16,185,129,0.10); '
            f'border-left:4px solid #10b981; border-radius:6px; '
            f'margin:6px 0 18px 0; font-size:0.88rem;">'
            f'<b style="color:#059669;">✓ All scrapers healthy</b> · '
            f'<span style="color:#475569;">latest snapshot: {latest_run_stamp}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
        return

    # Header banner.
    total_problems = len(stale_apps) + len(empty_apps)
    st.markdown(
        f'<div style="padding:12px 14px; background:rgba(245,158,11,0.10); '
        f'border-left:4px solid #f59e0b; border-radius:6px; '
        f'margin:6px 0 14px 0; font-size:0.88rem;">'
        f'<b style="color:#b45309;">⚠ {total_problems} app(s) need attention</b> '
        f'<span style="color:#64748b;">· latest snapshot: {latest_run_stamp}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Per-app row with Retry sync button. Combines stale (missing from
    # last fresh scrape) + empty (captured but 0 rows) into one list.
    # Retry dispatches a TARGET_APP=<id> workflow_dispatch so only the
    # affected panel re-runs.
    affected: list[tuple[str, str]] = []
    for app_id in stale_apps:
        affected.append((
            app_id,
            "Most recent scheduled scrape did NOT capture this app "
            "(login failure, cHAP outage, or pagination issue). The "
            "dashboard still shows the last successful snapshot via "
            "the merge logic; click Retry once the cHAP-side issue "
            "is resolved.",
        ))
    for app_id in empty_apps:
        affected.append((
            app_id,
            "Latest snapshot shows 0 rows. cHAP may have nothing to "
            "scrape, OR the scraper hit an error post-login. Check the "
            "Runs tab for the full output.",
        ))

    can_retry = (
        st.session_state.get("_principal") is not None
        and roles.can(st.session_state["_principal"], "add_app")
    )
    for app_id, reason in affected:
        col_msg, col_btn = st.columns([5, 1])
        with col_msg:
            st.markdown(
                f'<div style="padding:9px 14px; background:#fff; '
                f'border:1px solid #fde68a; border-left:3px solid #f59e0b; '
                f'border-radius:8px; margin-bottom:8px;">'
                f'<div style="font-weight:600; color:#0f172a; font-size:0.92rem;">'
                f'{app_id}</div>'
                f'<div style="color:#475569; font-size:0.82rem; margin-top:3px; '
                f'line-height:1.45;">{reason}</div></div>',
                unsafe_allow_html=True,
            )
        with col_btn:
            if can_retry:
                if st.button(
                    "🔁 Retry sync",
                    key=f"retry_health_{app_id}",
                    use_container_width=True,
                ):
                    _retry_sync_for_app(
                        st.session_state["_principal"], app_id,
                    )


def _render_overview_tab(principal: roles.UserPrincipal):
    top_col1, top_col2 = st.columns([3, 2])
    with top_col1:
        st.subheader("Configured admin panels")
        st.caption(
            "These apps run on the shared schedule. To add one, open the "
            "**Add new app** tab. To change how often they run, open "
            "**Settings**."
        )
    with top_col2:
        if roles.can(principal, "add_app"):
            st.markdown("&nbsp;")
            if st.button("▶ Run scrape now", type="primary", use_container_width=True):
                _trigger_scrape_now(principal)

    # ---- Scrape health banner --------------------------------------
    # Surfaces apps whose latest scrape produced 0 rows OR whose data
    # in results/latest/run.json is stale (meaning the merge logic
    # preserved their prior rows because this run failed). Without
    # this, a failed scheduled cron looks identical to a clean scrape
    # — admin can't tell which apps need attention.
    _render_scrape_health_banner()

    apps = app_registry.all_apps()
    if not apps:
        st.info(
            "No apps configured yet. Open the **Add new app** tab to set "
            "up the first one."
        )
        return

    # Per-app error cards + Retry-sync buttons. Reads app_errors from
    # results/latest/run.json — populated by scraper.py whenever a per-
    # app login or scrape raises. Lets the panel owner see the cause
    # ("Login not accepted: One-time Passcode sent successfully!") and
    # click Retry once their dev fixes the cHAP-side issue.
    _render_app_error_cards(principal, apps)

    # Status emoji — st.dataframe renders cells as plain text, so no
    # `:green[...]` Streamlit tags. Creds column deliberately omitted
    # (see earlier fix: env-var check never succeeds on Streamlit Cloud).
    status_label = {
        "canonical": "🟢 canonical",
        "pending_review": "🟡 pending review",
        "blocked": "🔴 blocked",
    }
    rows = []
    for a in apps:
        # str() to survive PyYAML auto-coercing unquoted ISO timestamps
        # into datetime objects (can't slice a datetime).
        added_at_str = str(a.added_at) if a.added_at else "—"
        # Frameworks: short-form display. ["auto"] means discovery
        # hasn't run yet (next scrape will populate). Empty list (rare)
        # falls back to auto.
        fw_list = list(getattr(a, "frameworks", None) or ["auto"])
        if fw_list == ["auto"]:
            fw_display = "auto (pending discovery)"
        else:
            fw_display = ", ".join(fw_list)
        rows.append({
            "App": a.label,
            "Id": a.id,
            "Status": status_label.get(a.schema_status, a.schema_status),
            "Frameworks": fw_display,
            "Installs": "✅" if a.scrape_installs else "—",
            "Uninstalls": "✅" if a.scrape_uninstalls else "—",
            "Added by": (a.added_by or "—").split("@")[0],
            "Added": added_at_str[:10],
        })
    st.dataframe(rows, hide_index=True, use_container_width=True)


def _render_add_app_wizard(principal: roles.UserPrincipal):
    """Focused onboarding form. Assumes the credential vault is already
    unlocked (the caller — `_render_add_app_tab` — gates on that and
    routes the user to Settings first if not). No CREDS mechanics
    surface in this form; it just collects the per-app fields.
    """
    # Show last-success banner so a rerun after submit feels intentional.
    last = st.session_state.pop("_add_app_success", None)
    if last:
        st.success(last)

    # Fixed URL banner — this UI is cHAP-only, so we don't ask the user
    # to type a login URL.
    st.info(
        f"**Works on the cHAP admin panel only.** We log into "
        f"`{CHAP_LOGIN_URL}` with the credentials you provide below, "
        f"then scrape the install / uninstall lists for the selected app."
    )

    existing_values = {a.dropdown_value for a in app_registry.all_apps()}
    available = [a for a in SUPPORTED_APPS if a["value"] not in existing_values]
    if not available:
        st.success("🎉 Every supported cHAP app is already onboarded.")
        return

    with st.form("add_app_form", clear_on_submit=True):
        # --- App selection -----------------------------------------------
        labels = [f"{a['label']} — {a['value']}" for a in available]
        picked_idx = st.selectbox(
            "App to onboard",
            options=range(len(available)),
            format_func=lambda i: labels[i],
            help=(
                "List mirrors the app-picker dropdown on the cHAP login page. "
                "Already-configured apps are filtered out."
            ),
        )
        picked = available[picked_idx]

        display_name = st.text_input(
            "Display name (optional)",
            value=picked["label"],
            help=(
                "How this app shows up in dashboards and the apps table. "
                "Defaults to the cHAP friendly name; edit if you want a "
                "shorter or clearer one."
            ),
        )

        # --- Credentials -------------------------------------------------
        st.markdown("**Credentials for this app's cHAP login**")
        c_email, c_pw = st.columns(2)
        with c_email:
            email = st.text_input(
                "Email",
                placeholder="ops@cedcommerce.com",
                help="The email you use to sign in to cHAP for this app.",
            )
        with c_pw:
            password = st.text_input(
                "Password", type="password",
                help="Stored encrypted in the GitHub Actions `CREDS` secret.",
            )

        # --- Scrape toggles ---------------------------------------------
        st.markdown("**What to scrape**")
        c_ins, c_uni = st.columns(2)
        with c_ins:
            wants_installs = st.checkbox(
                "Seller installs", value=True,
                help="Active sellers currently installed on this app.",
            )
        with c_uni:
            wants_uninstalls = st.checkbox(
                "Uninstalls", value=True,
                help="Sellers who uninstalled this app (churn tracking).",
            )

        # --- Schedule routing ------------------------------------------
        st.markdown("**How often should this app be scraped?**")
        st.caption(
            "Pick the shared schedule (default) to run alongside the other "
            "existing apps twice a day — simplest option. Pick a solo "
            "schedule to isolate this app in its own GitHub Actions "
            "workflow with its own cron, so its load doesn't stack with "
            "the others (useful if cHAP's backend struggles)."
        )
        schedule_choice = st.selectbox(
            "Schedule",
            options=list(_ADD_APP_SCHEDULE_CHOICES.keys()),
            index=0,
            help=(
                "The shared schedule runs 00:00 and 12:00 IST. Solo "
                "schedules get their own `.github/workflows/scrape_<app>.yml` "
                "committed automatically."
            ),
        )

        run_after = st.checkbox(
            "Dispatch a test scrape right after adding",
            value=True,
            help=(
                "Kicks off the GitHub Actions workflow for this new app. "
                "First run takes ~3-5 min; results show up in the Overview "
                "tab above and on the Dashboard."
            ),
        )

        submitted = st.form_submit_button(
            "Add admin panel", type="primary", use_container_width=True
        )

    if not submitted:
        return

    # --- Validation -----------------------------------------------------
    errs: list[str] = []
    if not display_name.strip():
        errs.append("Display name can't be empty.")
    if not email.strip() or not password:
        errs.append("Email and password are both required.")
    if not (wants_installs or wants_uninstalls):
        errs.append("Pick at least one of installs / uninstalls.")
    if errs:
        show_warning(
            "Please fix the following before submitting:",
            hint="\n\n".join(f"• {e}" for e in errs),
        )
        return

    _commit_new_app(
        principal=principal,
        name=display_name.strip(),
        app_id=picked["value"],
        dropdown=picked["value"],
        email=email.strip(),
        password=password,
        wants_installs=wants_installs,
        wants_uninstalls=wants_uninstalls,
        run_after=run_after,
        schedule_choice=schedule_choice,
    )


def _commit_new_app(
    *,
    principal: roles.UserPrincipal,
    name: str,
    app_id: str,
    dropdown: str,
    email: str,
    password: str,
    wants_installs: bool,
    wants_uninstalls: bool,
    run_after: bool,
    schedule_choice: str,
) -> None:
    """Do the three GitHub writes + optional dispatch. Shows per-step status.

    Each app's credentials live in their OWN repo secrets (APP_N_USER,
    APP_N_PASS). We write them directly — no bundle round-trip, no
    paste-back step. The workflow reads them via toJSON(secrets).

    Schedule routing:
      - "Shared schedule" (default): entry.shared_schedule = True, no
        per-app workflow file. scraper.py's main loop scrapes it on
        the shared 12h cron.
      - "Solo" variants: commits `.github/workflows/scrape_<app_id>.yml`
        with a dedicated cron. scraper.py skips the app on the shared
        cron so it isn't double-scraped.
    """
    sched = _ADD_APP_SCHEDULE_CHOICES.get(schedule_choice) or {}
    shared_schedule = bool(sched.get("shared", True))
    solo_cron = sched.get("cron")
    try:
        ctx = gh.context_from_streamlit(st)
    except Exception as e:
        show_warning(
            "We couldn't connect to GitHub.",
            hint=(
                "The Streamlit secrets are probably missing the `[github]` "
                "block (owner, repo, pat). Ask the admin to add it and "
                "reboot the app."
            ),
            cause=e,
        )
        return

    creds_ref = app_registry.next_creds_ref()

    # 1. Write the two per-app secrets. Each PUT is idempotent and
    # independent, so a failure here never damages other apps' secrets.
    with st.status("Saving credentials…", expanded=False) as status:
        try:
            gh.put_repo_secret(ctx, f"{creds_ref}_USER", email)
            gh.put_repo_secret(ctx, f"{creds_ref}_PASS", password)
            status.update(label="Credentials saved", state="complete")
        except Exception as e:
            status.update(label="Couldn't save credentials", state="error")
            show_warning(
                "We couldn't save the credentials for this app.",
                hint=(
                    "Most likely the GitHub token is missing the "
                    "**Secrets: Read and write** permission. Ask the admin "
                    "to update the PAT and reboot the app. Nothing has been "
                    "committed — safe to try again after the fix."
                ),
                cause=e,
            )
            return

    # 2. apps.yaml
    with st.status("Registering the app…", expanded=False) as status:
        try:
            import yaml
            entry = AppEntry(
                id=app_id,
                label=name,
                dropdown_value=dropdown,
                scrape_installs=wants_installs,
                scrape_uninstalls=wants_uninstalls,
                creds_ref=creds_ref,
                added_by=principal.email,
                added_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                # First real scrape will flip this to canonical/pending_review/blocked.
                schema_status="pending_review",
                shared_schedule=shared_schedule,
            )
            current_yaml = gh.read_file(ctx, "apps.yaml")
            data = yaml.safe_load(current_yaml or "") or {"schema_version": 1, "apps": []}
            data.setdefault("apps", []).append({k: v for k, v in entry.__dict__.items()})
            gh.put_file(
                ctx,
                "apps.yaml",
                yaml.safe_dump(data, sort_keys=False),
                f"feat(registry): add admin panel {entry.id} "
                f"({roles.audit_stamp(principal.email, 'onboard app')})",
            )
            status.update(label="App registered", state="complete")
        except Exception as e:
            status.update(label="Registration failed", state="error")
            # Partial-success state: credentials already saved but the
            # app isn't registered yet. Make that explicit so the admin
            # knows to check the CREDS secret too.
            show_error(
                "The credentials were saved but registering the app failed.",
                hint=(
                    "Retry the form — CREDS is append-only-safe so a "
                    "second attempt won't duplicate. If the same error "
                    "happens again, contact the admin so they can clean up "
                    "the orphaned credential entry in the GitHub secret."
                ),
                cause=e,
            )
            return

    # 3. (optional) per-app workflow file — only when the user picked a
    # solo schedule. Commits `.github/workflows/scrape_<app_id>.yml` so
    # the app runs on its own cron, isolated from the shared sweep.
    solo_workflow_line = ""
    if not shared_schedule and solo_cron:
        with st.status("Creating dedicated workflow…", expanded=False) as status:
            try:
                wf_path = f".github/workflows/scrape_{app_id}.yml"
                wf_body = _render_per_app_workflow_yaml(
                    app_id=app_id,
                    cron=solo_cron,
                    label=name,
                    cron_summary=sched.get("summary", solo_cron),
                )
                gh.put_file(
                    ctx,
                    wf_path,
                    wf_body,
                    f"feat(scrape): dedicated workflow for {app_id} "
                    f"({sched.get('summary', solo_cron)}) "
                    f"({roles.audit_stamp(principal.email, 'add per-app workflow')})",
                )
                status.update(
                    label=f"Dedicated workflow committed ({sched.get('summary', 'solo')})",
                    state="complete",
                )
                solo_workflow_line = (
                    f" Dedicated workflow: "
                    f"`.github/workflows/scrape_{app_id}.yml` "
                    f"(runs {sched.get('summary', solo_cron)})."
                )
            except Exception as e:
                status.update(label="Workflow create failed", state="error")
                show_warning(
                    "Couldn't create the per-app workflow file.",
                    hint=(
                        "The app is registered and the credentials are "
                        "saved. The main shared-schedule scrape will still "
                        "pick it up (since shared_schedule defaults to "
                        "True on failure). If you want the solo schedule, "
                        "check that the GitHub PAT has "
                        "**Workflows: Read and write** permission and "
                        "re-run the add."
                    ),
                    cause=e,
                )

    # 4. (optional) workflow dispatch — scoped to the NEW app only so
    # onboarding doesn't trigger a full re-scrape of every configured app
    # (which would hammer cHAP's MongoDB unnecessarily).
    if run_after:
        try:
            gh.trigger_scrape(
                ctx,
                reason=f"onboarding {entry.id} by {principal.email}",
                target_app=entry.id,
            )
            dispatch_line = (
                f" Test scrape dispatched for `{entry.id}` only — results "
                f"in ~3-5 min (existing apps are not re-scraped)."
            )
        except Exception as e:
            dispatch_line = f" (Added, but scrape dispatch failed: {e})"
    else:
        if shared_schedule:
            dispatch_line = " Scrape will run at the next shared tick (00:00 / 12:00 IST)."
        else:
            dispatch_line = f" Scrape will run on its solo schedule ({sched.get('summary', 'configured')})."

    # Stash banner + rerun so the apps table above refreshes (Streamlit
    # Cloud has a small delay before the redeploy picks up apps.yaml; the
    # local app_registry read may still show the old list until then).
    st.session_state["_add_app_success"] = (
        f"✅ **{name}** added as `{entry.id}` "
        f"(schema: pending_review until first scrape).{solo_workflow_line}{dispatch_line}"
    )
    st.rerun()


def _render_per_app_workflow_yaml(
    *, app_id: str, cron: str, label: str, cron_summary: str
) -> str:
    """Generate the YAML for a per-app scrape workflow.

    This is near-identical to `.github/workflows/scrape.yml` except:
      - name is `scrape-<app_id>` so each workflow shows distinctly
        in the Actions tab
      - only ONE cron entry (the user's choice), vs the shared twice-daily
      - TARGET_APP env var is baked into the Run step so the scraper
        only hits this one app
      - artifact upload uses an app-scoped name so multiple solo workflows
        don't collide on artifact naming

    The secret-unpack, preflight, commit, and debug-upload steps are
    copy-pasted from the shared workflow so they stay in lockstep. If
    the shared workflow evolves, per-app files won't auto-update —
    that's a conscious tradeoff for simplicity. Admin can regenerate
    by re-onboarding the app (or we can add a "regenerate" button later).
    """
    # keep the cron double-quoted in YAML so special chars round-trip safe
    cron_q = cron.replace('"', '\\"')
    return f"""# =============================================================================
#  Auto-generated per-app scrape workflow for {app_id}.
#  Runs on its OWN cron ({cron_summary}) so it doesn't stack with the
#  shared scrape.yml. Generated by the admin UI — if you edit by hand,
#  re-onboarding the app will overwrite your changes.
# =============================================================================

name: scrape-{app_id}

on:
  schedule:
    - cron: "{cron_q}"   # {cron_summary}
  workflow_dispatch:
    inputs:
      reason:
        description: "Why this manual run?"
        required: false
        default: "on-demand"

concurrency:
  group: scrape-{app_id}-${{{{ github.ref }}}}
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  scrape:
    name: Scrape {label} ({app_id})
    runs-on: ubuntu-latest
    timeout-minutes: 15
    env:
      PYTHONUNBUFFERED: "1"
      HEADLESS: "true"
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 1

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: "pip"
          cache-dependency-path: "requirements.txt"

      - name: Install deps
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Install Playwright Chromium
        run: python -m playwright install --with-deps chromium

      - name: Unpack repo secrets into job env
        shell: bash
        env:
          SECRETS_JSON: ${{{{ toJSON(secrets) }}}}
        run: |
          set -e
          python - <<'PY'
          import os, json, sys, secrets as _secrets
          data = json.loads(os.environ.get("SECRETS_JSON") or "{{}}")
          # Derive the required keys from apps.yaml so this file doesn't
          # hardcode the per-app secret prefix (APP_N_*).
          import yaml
          try:
              registry = yaml.safe_load(open("apps.yaml")) or {{}}
          except Exception as err:
              print(f"::error::Couldn't read apps.yaml: {{err}}")
              sys.exit(1)
          required = ["LOGIN_URL"]
          for app in (registry.get("apps") or []):
              if app.get("id") != "{app_id}":
                  continue
              ref = (app.get("creds_ref") or "").strip()
              if ref:
                  required.extend([f"{{ref}}_USER", f"{{ref}}_PASS"])
          unpacked = {{k: data[k] for k in required if data.get(k)}}
          missing = [k for k in required if not unpacked.get(k)]
          if missing:
              print(f"::error::Missing required secrets: {{', '.join(missing)}}")
              sys.exit(1)
          gh_env = os.environ.get("GITHUB_ENV")
          with open(gh_env, "a", encoding="utf-8") as f:
              for k, v in unpacked.items():
                  delim = "EOF_" + _secrets.token_hex(8)
                  while delim in (v or ""):
                      delim = "EOF_" + _secrets.token_hex(8)
                  f.write(f"{{k}}<<{{delim}}\\n{{v}}\\n{{delim}}\\n")
          print("Unpacked keys (value lengths only):")
          for k in sorted(unpacked):
              print(f"  {{k}}: len={{len(unpacked[k])}}")
          PY

      - name: Run scraper (target={app_id})
        shell: bash
        env:
          TARGET_APP: "{app_id}"
        run: |
          set -e
          python scraper.py

      - name: Commit results back to main
        run: |
          git config user.name "chap-scraper-bot"
          git config user.email "chap-scraper-bot@users.noreply.github.com"
          git add results/
          if git diff --cached --quiet; then
            echo "No changes in results/ — skipping commit."
            exit 0
          fi
          STAMP=$(date -u +"%Y-%m-%d_%H-%M-%SZ")
          git commit -m "chore(data): scrape {app_id} ${{STAMP}}"
          git push origin HEAD:main

      - name: Upload debug artifacts on failure
        if: failure()
        uses: actions/upload-artifact@v4
        with:
          name: scraper-{app_id}-debug-${{{{ github.run_id }}}}
          path: |
            debug_dom_*.txt
            error_*.png
          if-no-files-found: ignore
          retention-days: 14
"""


# =================================================================
# Users tab (super admin only)
# =================================================================
def _render_access_tab(principal):
    """Approve / deny sign-up requests stored in Supabase auth_users.

    Roles still come from roles.yaml — this tab only controls whether
    a credential pair can authenticate. Approving here lets the user
    sign in; their role is then resolved via roles.yaml (defaulting
    to 'viewer' for anyone not explicitly listed).
    """
    from supabase_client import SupabaseClient
    from email_notifications import notify_user_approved, notify_user_denied

    st.subheader("Access requests")
    st.caption(
        "Pending sign-ups need an admin approval before the user can log in. "
        "Approving sends them a notification email (if SMTP is configured)."
    )

    client = SupabaseClient()
    if client.dry_run:
        st.warning(
            "Supabase isn't configured — approvals can't persist. "
            "Make sure SUPABASE_URL and SUPABASE_KEY are set in Streamlit secrets."
        )
        return

    pending = client.list_auth_users(status="pending")
    if not pending:
        st.info("No pending requests right now.")
    else:
        st.markdown(f"**{len(pending)} pending request(s)**")
        for u in pending:
            email = u.get("email", "")
            name = u.get("display_name") or "(no name)"
            requested_at = (u.get("requested_at") or "")[:19].replace("T", " ")
            with st.container(border=True):
                cols = st.columns([3, 1, 1])
                cols[0].markdown(f"**{email}**  \n{name}  \n_Requested {requested_at} UTC_")
                if cols[1].button("Approve", key=f"approve_{email}", type="primary"):
                    ok = client.update_auth_user_status(
                        email, status="approved", approved_by=principal.email
                    )
                    if ok:
                        notify_user_approved(user_email=email)
                        st.success(f"Approved {email}.")
                        st.rerun()
                    else:
                        st.error(f"Couldn't approve {email}.")
                if cols[2].button("Deny", key=f"deny_{email}"):
                    ok = client.update_auth_user_status(
                        email, status="denied", approved_by=principal.email
                    )
                    if ok:
                        notify_user_denied(user_email=email)
                        st.success(f"Denied {email}.")
                        st.rerun()
                    else:
                        st.error(f"Couldn't deny {email}.")

    st.divider()

    st.subheader("All accounts")
    all_users = client.list_auth_users()
    if not all_users:
        st.caption("No accounts yet.")
        return
    rows = []
    for u in all_users:
        rows.append({
            "Email": u.get("email", ""),
            "Name": u.get("display_name") or "—",
            "Status": (u.get("status") or "").title(),
            "Requested": (u.get("requested_at") or "")[:19].replace("T", " "),
            "Approved": (u.get("approved_at") or "—")[:19].replace("T", " ") if u.get("approved_at") else "—",
            "Last login": (u.get("last_login_at") or "—")[:19].replace("T", " ") if u.get("last_login_at") else "—",
            "Approved by": u.get("approved_by") or "—",
        })
    st.dataframe(rows, hide_index=True, use_container_width=True)


def _render_users_tab(principal):
    st.subheader("Who has access")
    rows = []
    for email, role in roles.list_assigned():
        origin = "hard-coded" if email in roles.HARD_CODED_SUPER_ADMINS else "roles.yaml"
        rows.append({"Email": email, "Role": role, "Origin": origin})
    st.dataframe(rows, hide_index=True, use_container_width=True)

    st.caption(
        f"Any `@{roles.ALLOWED_DOMAIN}` user not listed above still gets the **viewer** role by default. "
        "Listing them only serves to promote them to editor or super_admin."
    )

    st.divider()

    st.subheader("Grant a role")
    with st.form("grant_role"):
        new_email = st.text_input("Email (must end in @" + roles.ALLOWED_DOMAIN + ")")
        new_role = st.selectbox("Role", [roles.VIEWER, roles.EDITOR, roles.SUPER_ADMIN])
        submitted = st.form_submit_button("Grant")
    if submitted:
        try:
            roles.set_role(new_email, new_role)
            _commit_roles_yaml(principal, f"grant {new_role} to {new_email}")
            st.success(f"Set `{new_email}` → `{new_role}`. Redeploy in ~30 s.")
            st.rerun()
        except ValueError as e:
            # Input-validation failure (e.g. non-threecolts email). Not
            # a bug — surface as a warning with the raw reason as hint.
            show_warning("That email can't be granted a role.", hint=str(e))
        except Exception as e:
            show_warning(
                "We couldn't save the new role.",
                hint="Retry in a moment. If it repeats, contact the admin.",
                cause=e,
            )

    st.subheader("Revoke a role")
    candidates = [
        email for email in (dict(roles.list_assigned())).keys()
        if email not in roles.HARD_CODED_SUPER_ADMINS
    ]
    if not candidates:
        st.caption("No revocable entries. The hard-coded super admin can't be revoked.")
        return
    with st.form("revoke_role"):
        pick = st.selectbox("Email", candidates)
        submitted = st.form_submit_button("Revoke")
    if submitted:
        try:
            roles.revoke_role(pick)
            _commit_roles_yaml(principal, f"revoke {pick}")
            st.success(f"Revoked `{pick}` (falls back to viewer). Redeploy in ~30 s.")
            st.rerun()
        except ValueError as e:
            show_warning("That user can't be revoked.", hint=str(e))
        except Exception as e:
            show_warning(
                "We couldn't revoke the role.",
                hint="Retry in a moment. If it repeats, contact the admin.",
                cause=e,
            )


def _commit_roles_yaml(principal, action: str):
    """Push the local roles.yaml back up to GitHub so the live
    Streamlit Cloud deploy picks it up (otherwise the change only
    lives on the ephemeral runner)."""
    try:
        ctx = gh.context_from_streamlit(st)
    except Exception:
        # Local-dev: skip remote commit.
        return
    from pathlib import Path
    body = Path("roles.yaml").read_text(encoding="utf-8")
    msg = f"chore(roles): {roles.audit_stamp(principal.email, action)}"
    gh.put_file(ctx, "roles.yaml", body, msg)


def _commit_apps_yaml(principal, action: str):
    """Push the local apps.yaml back up to GitHub so the change
    survives the next Streamlit Cloud redeploy. Mirror of
    _commit_roles_yaml — same pattern, different file."""
    try:
        ctx = gh.context_from_streamlit(st)
    except Exception:
        return
    from pathlib import Path
    body = Path("apps.yaml").read_text(encoding="utf-8")
    msg = f"chore(apps): {roles.audit_stamp(principal.email, action)}"
    gh.put_file(ctx, "apps.yaml", body, msg)


# =================================================================
# On-demand workflow trigger — "Run scrape now" button
# =================================================================
def _trigger_scrape_now(principal: roles.UserPrincipal) -> None:
    """POST a workflow_dispatch to run `.github/workflows/scrape.yml` now.

    The scheduled cron fires twice a day (00:00 + 12:00 IST). This
    gives editor+ users an escape hatch when they don't want to wait —
    e.g. right after onboarding a new app they want to see data on
    the dashboard before the next tick.

    Surfaces success / failure inline via st.success / st.error. A
    successful dispatch returns 204 immediately; the actual scrape
    takes ~3–5 min (login + paginator walk × N apps), so we nudge the
    user to re-check in a few minutes rather than blocking the UI.
    """
    # Re-gate in-function — defence in depth. The outer button is
    # already hidden from viewers, but role could have been revoked
    # between render and click.
    if not roles.can(principal, "add_app"):
        show_warning(
            "You don't have permission to start a scrape.",
            hint=(
                "Ask a super admin to grant you the **editor** role in the "
                "Users tab."
            ),
        )
        return

    try:
        ctx = gh.context_from_streamlit(st)
    except Exception as e:
        show_warning(
            "We couldn't reach GitHub.",
            hint=(
                "The Streamlit secrets need a `[github]` block with `owner`, "
                "`repo`, and `pat`. Ask the admin to add it and reboot the app."
            ),
            cause=e,
        )
        return

    with st.spinner("Dispatching workflow…"):
        try:
            gh.trigger_scrape(ctx, reason=f"on-demand by {principal.email}")
        except Exception as e:
            show_warning(
                "We couldn't start the scrape.",
                hint=(
                    "Usually this is a missing GitHub permission on the "
                    "connected token (Actions: write). Retry in a minute; "
                    "if it persists, contact the admin."
                ),
                cause=e,
            )
            return

    try:
        import audit
        audit.log_action(
            email=principal.email,
            console="chap",
            page="Admin",
            action="scrape_dispatch",
            target_type="workflow",
            target_id="scrape-chap",
        )
    except Exception:
        pass

    st.success(
        "Workflow dispatched. Fresh data should land in the dashboard in "
        "~3–5 minutes. Watch the GitHub Actions `scrape-chap` run for progress."
    )


# =================================================================
# Scrape schedule (global cron)
# =================================================================
# Design: one knob drives `.github/workflows/scrape.yml`'s schedule
# block. We rewrite the `schedule:` section via the Contents API instead
# of surgery on arbitrary YAML (comments, anchors, etc. elsewhere in the
# file are preserved). Min 6 h, enforced by FREQ_CHOICES only offering
# 6/12/24 h options.
_SCRAPE_WORKFLOW_PATH = ".github/workflows/scrape.yml"
_SCHEDULE_BLOCK_RE = re.compile(
    r"(^\s*schedule:\s*\n)"
    r"((?:^\s*-\s*cron:.*\n)+)",
    re.MULTILINE,
)


def _render_schedule_section(principal: roles.UserPrincipal) -> None:
    """Expander that shows the current cron + lets editors change it."""
    current_label = _detect_current_schedule_label()
    with st.expander(
        f"⏱ Scrape schedule  —  current: **{current_label or 'unknown'}**",
        expanded=False,
    ):
        st.caption(
            "The GitHub Actions workflow scrapes every configured app on "
            "this cadence. Minimum 6 hours — cHAP rate-limits, and a full "
            "run already takes ~5 min per app."
        )

        if not roles.can(principal, "add_app"):
            st.info("View only. Ask a super admin to change the schedule.")
            return

        # Default the dropdown to whatever scrape.yml currently has.
        keys = list(FREQ_CHOICES.keys())
        default_idx = keys.index(current_label) if current_label in keys else 1
        choice = st.selectbox(
            "Pick a new schedule",
            options=keys,
            index=default_idx,
        )

        if st.button("Save schedule", key="save_sched"):
            if choice == current_label:
                st.info("Schedule is already set to that. Nothing to commit.")
                return
            try:
                _apply_schedule_change(principal, choice)
                st.success(
                    f"✅ Schedule updated to **{choice}**. GitHub will pick it "
                    f"up on the next cron tick; Streamlit Cloud redeploys this "
                    f"UI within ~30 s so the 'current' label refreshes."
                )
            except Exception as e:
                # A 403 on .github/workflows/*.yml = the PAT is missing
                # the Workflows write permission. GitHub enforces that
                # separately from contents:write. Surface a targeted fix.
                msg = str(e)
                is_perm = (
                    "403" in msg
                    and ("workflow" in msg.lower() or "Resource not accessible" in msg)
                )
                if is_perm:
                    show_warning(
                        "We couldn't change the schedule — the connected "
                        "GitHub token doesn't have permission to edit "
                        "workflow files.",
                        hint=(
                            "Ask the admin to update the GitHub PAT: under "
                            "**Settings → Developer settings → Personal "
                            "access tokens**, give the token **Workflows: "
                            "Read and write** permission, then paste the "
                            "new value into Streamlit Cloud secrets "
                            "(`[github].pat`) and reboot the app."
                        ),
                        cause=e,
                    )
                else:
                    show_warning(
                        "Couldn't save the new schedule.",
                        hint="Retry in a minute. If the same error repeats, "
                             "send the admin the technical details below.",
                        cause=e,
                    )


def _detect_current_schedule_label() -> Optional[str]:
    """Return the FREQ_CHOICES label matching scrape.yml's current crons.

    Reads the local checkout (Streamlit Cloud auto-syncs on push). If the
    existing cron set doesn't match any predefined option (e.g. someone
    hand-edited the workflow), returns None — the UI flags that as
    "unknown" and offers the choices anyway.
    """
    try:
        text = Path(_SCRAPE_WORKFLOW_PATH).read_text(encoding="utf-8")
    except Exception:
        return None
    crons = set(re.findall(r'-\s*cron:\s*"([^"]+)"', text))
    for label, info in FREQ_CHOICES.items():
        if {c for c, _ in info["crons"]} == crons:
            return label
    return None


def _apply_schedule_change(principal: roles.UserPrincipal, choice: str) -> None:
    """Read scrape.yml from GitHub, rewrite the schedule block, commit."""
    ctx = gh.context_from_streamlit(st)
    current_yaml = gh.read_file(ctx, _SCRAPE_WORKFLOW_PATH)
    new_yaml = _rewrite_cron_in_workflow(current_yaml, FREQ_CHOICES[choice]["crons"])
    if new_yaml == current_yaml:
        return  # already at the target — no-op
    msg = (
        f"chore(schedule): set scrape cron to {choice.lower()} "
        f"({roles.audit_stamp(principal.email, 'update schedule')})"
    )
    gh.put_file(ctx, _SCRAPE_WORKFLOW_PATH, new_yaml, msg)


def _rewrite_cron_in_workflow(
    yaml_text: str, crons: list[tuple[str, str]]
) -> str:
    """Replace the `schedule:` block's cron entries in a workflow YAML.

    Preserves surrounding indentation, comments on unrelated lines, and
    anything else in the workflow. Only touches the cron lines directly
    under `schedule:`.
    """
    def _replace(match: re.Match) -> str:
        schedule_line = match.group(1)
        old_block = match.group(2)
        first = old_block.splitlines(keepends=True)[0]
        indent = first[: len(first) - len(first.lstrip())]
        new_block = "".join(
            f'{indent}- cron: "{c}"   # {cmt}\n' if cmt else f'{indent}- cron: "{c}"\n'
            for c, cmt in crons
        )
        return schedule_line + new_block

    new_text, n = _SCHEDULE_BLOCK_RE.subn(_replace, yaml_text, count=1)
    if n == 0:
        raise RuntimeError(
            f"Couldn't find a `schedule:` block in {_SCRAPE_WORKFLOW_PATH}. "
            "Edit the file manually once and re-try."
        )
    return new_text


# =================================================================
# Add-new-app tab — wrapper that gates on the credential vault
# =================================================================
def _render_add_app_tab(principal: roles.UserPrincipal) -> None:
    """Gate on permission, then render the focused add form.

    Credentials are written directly to per-app GitHub secrets on
    submit, so there's no vault step or bundle paste here anymore.
    """
    if not roles.can(principal, "add_app"):
        show_info(
            "You have view-only access.",
            hint="Ask a super admin to grant you the **editor** role in the "
                 "Users tab so you can onboard new apps.",
        )
        return

    _render_add_app_wizard(principal)


# =================================================================
# Settings tab — credential vault + scrape schedule
# =================================================================
def _render_settings_tab(principal: roles.UserPrincipal) -> None:
    if not roles.can(principal, "add_app"):
        show_info(
            "Settings are editor-only.",
            hint="Ask a super admin to grant you the **editor** role in the "
                 "Users tab.",
        )
        return

    st.subheader("⏱ Scrape schedule")
    _render_schedule_section(principal)

    st.divider()
    st.subheader("🧭 Frameworks per app")
    _render_frameworks_section(principal)


def _render_frameworks_section(principal: roles.UserPrincipal) -> None:
    """Edit the per-app `frameworks` list in apps.yaml.

    cHAP shows a framework dropdown above the seller list (shopify /
    prestashop / woocommerce / etc.). The scraper iterates over each
    listed framework and merges the rows by seller_id — that's how we
    capture every seller AND keep cHAP's per-framework plan-data
    response (verified 2026-05-07: cHAP's "all" view drops plan
    badges, only the per-framework views include them).

    UI follows feedback_ux_style.md:
      - One row per app, no separate page.
      - Comma-separated input with inline validation against the
        canonical list of known frameworks.
      - "Re-discover" button per row resets to ["auto"] so the next
        scrape re-reads cHAP's dropdown options.
    """
    KNOWN_FRAMEWORKS = (
        "shopify", "prestashop", "woocommerce", "magento",
        "bigcommerce", "wix", "squarespace",
    )

    st.caption(
        "Each app's framework list controls which cHAP dropdown values "
        "the scraper iterates. Use **auto** to let the next scrape "
        "discover them. Comma-separate multiple frameworks (e.g. "
        "`shopify, woocommerce`)."
    )

    apps = app_registry.all_apps()
    if not apps:
        st.caption("No apps configured yet.")
        return

    for a in apps:
        current = list(getattr(a, "frameworks", None) or ["auto"])
        current_str = ", ".join(current)
        with st.container(border=True):
            cols = st.columns([3, 5, 2, 2])
            cols[0].markdown(f"**{a.label}**  \n`{a.id}`")
            new_str = cols[1].text_input(
                "Frameworks",
                value=current_str,
                key=f"frameworks_input_{a.id}",
                label_visibility="collapsed",
                help="Comma-separated. Use 'auto' to re-discover on next scrape.",
            )
            # Inline validation — the input is parsed live and the
            # status row below shows ✓ or ⚠️ before the user clicks Save.
            parsed = [
                x.strip().lower() for x in (new_str or "").split(",") if x.strip()
            ]
            if not parsed:
                parsed = ["auto"]
            unknown = [
                p for p in parsed
                if p != "auto" and p not in KNOWN_FRAMEWORKS
            ]
            if unknown:
                cols[1].caption(
                    f"⚠️ unknown: {', '.join(unknown)} — "
                    f"valid values: {', '.join(KNOWN_FRAMEWORKS)} or `auto`"
                )
            else:
                cols[1].caption(
                    f"✓ will scrape: {', '.join(parsed)}"
                    if parsed != ["auto"]
                    else "✓ will discover on next scrape"
                )

            save_disabled = bool(unknown) or (parsed == current)
            if cols[2].button(
                "Save",
                key=f"frameworks_save_{a.id}",
                disabled=save_disabled,
                use_container_width=True,
            ):
                ok = app_registry.update_frameworks(a.id, parsed)
                if ok:
                    _commit_apps_yaml(
                        principal,
                        f"set frameworks={parsed} for {a.id}",
                    )
                    st.success(
                        f"Updated `{a.id}` → frameworks: {', '.join(parsed)}. "
                        f"Streamlit Cloud redeploy in ~30s; next scrape uses "
                        f"the new list."
                    )
                    st.rerun()
                else:
                    show_warning(
                        f"Couldn't update `{a.id}`.",
                        hint="The app id may have been removed from "
                             "apps.yaml in a parallel edit. Reload and retry.",
                    )

            if cols[3].button(
                "Re-discover",
                key=f"frameworks_redisc_{a.id}",
                disabled=current == ["auto"],
                use_container_width=True,
                help="Resets to 'auto' so the next scrape re-reads cHAP's dropdown options.",
            ):
                ok = app_registry.update_frameworks(a.id, ["auto"])
                if ok:
                    _commit_apps_yaml(
                        principal,
                        f"reset frameworks to auto for {a.id}",
                    )
                    st.success(
                        f"`{a.id}` will re-discover frameworks on the next "
                        f"scrape."
                    )
                    st.rerun()


# =================================================================
# Runs tab — live GitHub Actions history
# =================================================================
def _render_runs_tab(principal: roles.UserPrincipal) -> None:
    """Live feed of the last 20 scrape runs across every workflow.

    Pulls from GET /actions/runs using the same PAT the rest of the
    admin UI uses. Shows when / what / pass-fail / how long / link so a
    super admin can diagnose a red scheduled run without leaving the app.
    """
    st.subheader("Recent scrape runs")
    st.caption(
        "Live from GitHub Actions — covers the shared `scrape.yml` AND "
        "any per-app workflow files (`scrape_<id>.yml`). Click **open →** "
        "on any row to jump to the full log."
    )

    # Refresh button in the top-right so super admins can poll without
    # reloading the whole page (which would bounce through OAuth etc.).
    _, refresh_col = st.columns([4, 1])
    with refresh_col:
        if st.button("🔄 Refresh", key="runs_refresh", use_container_width=True):
            st.rerun()

    try:
        ctx = gh.context_from_streamlit(st)
    except Exception as e:
        show_warning(
            "We couldn't reach GitHub to load run history.",
            hint="Check that the `[github]` block in Streamlit secrets has "
                 "`owner`, `repo`, and `pat`. Ask the admin to fix and reboot.",
            cause=e,
        )
        return

    with st.spinner("Loading last 20 runs from GitHub…"):
        try:
            runs = gh.list_workflow_runs(ctx, limit=20)
        except Exception as e:
            show_warning(
                "Couldn't load run history.",
                hint="The PAT needs `Actions: Read` permission (usually "
                     "included with read access). If the error persists, "
                     "contact the admin.",
                cause=e,
            )
            return

    if not runs:
        st.info("No workflow runs yet. Dispatch one from **Overview → Run scrape now**.")
        return

    # --- build the display table ---------------------------------------
    status_emoji = {
        "success":   "✅ success",
        "failure":   "❌ failure",
        "cancelled": "⏹ cancelled",
        "skipped":   "⏭ skipped",
        "timed_out": "⏰ timed out",
    }
    event_label = {
        "schedule":          "⏱ scheduled",
        "workflow_dispatch": "🖱 manual",
        "push":              "📤 push",
    }

    rows = []
    for r in runs:
        conclusion = r.get("conclusion")
        if conclusion is None and r.get("status") in ("in_progress", "queued"):
            status = f"⏳ {r.get('status', 'running')}"
        else:
            status = status_emoji.get(conclusion, f"❓ {conclusion or '?'}")

        start = r.get("run_started_at") or r.get("created_at")
        end = r.get("updated_at")
        dur = "—"
        if start and end:
            try:
                s = datetime.fromisoformat(start.replace("Z", "+00:00"))
                e = datetime.fromisoformat(end.replace("Z", "+00:00"))
                total = int((e - s).total_seconds())
                if total >= 60:
                    dur = f"{total // 60}m {total % 60}s"
                else:
                    dur = f"{total}s"
            except Exception:
                pass

        when = (start or "")[:16].replace("T", " ") if start else "—"

        rows.append({
            "When (UTC)": when,
            "Workflow": r.get("name") or "—",
            "Status": status,
            "Duration": dur,
            "Trigger": event_label.get(r.get("event"), r.get("event") or "?"),
            "View": r.get("html_url") or "",
        })

    st.dataframe(
        rows,
        hide_index=True,
        use_container_width=True,
        column_config={
            "View": st.column_config.LinkColumn(
                "View", display_text="open →"
            ),
        },
    )

    # Quick summary band below the table.
    successes = sum(1 for r in runs if r.get("conclusion") == "success")
    failures = sum(1 for r in runs if r.get("conclusion") == "failure")
    running = sum(
        1 for r in runs if r.get("status") in ("in_progress", "queued")
    )
    st.caption(
        f"Showing last {len(runs)}: ✅ {successes} success · "
        f"❌ {failures} fail · ⏳ {running} running."
    )


# =================================================================
# Entrypoint for `streamlit run admin_ui.py`
# =================================================================
if __name__ == "__main__":
    main()
