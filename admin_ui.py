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


# =================================================================
# Page entry
# =================================================================
@wrap_page
def main():
    principal = auth.gate()
    auth.require("see_admin_tab", principal)

    st.set_page_config(page_title="Admin — cHAP Seller Tracker", page_icon=":gear:", layout="wide")
    auth.sign_out_button(st)

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
    #   - Users           — super-admin only, hidden otherwise
    tab_labels = ["Overview", "Add new app", "Settings"]
    if roles.can(principal, "see_users_tab"):
        tab_labels.append("Users")
    tabs = st.tabs(tab_labels)

    with tabs[0]:
        _render_overview_tab(principal)
    with tabs[1]:
        _render_add_app_tab(principal)
    with tabs[2]:
        _render_settings_tab(principal)
    if len(tabs) > 3:
        with tabs[3]:
            _render_users_tab(principal)


# =================================================================
# Overview tab — the apps table + Run scrape now
# =================================================================
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

    apps = app_registry.all_apps()
    if not apps:
        st.info(
            "No apps configured yet. Open the **Add new app** tab to set "
            "up the first one."
        )
        return

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
        rows.append({
            "App": a.label,
            "Id": a.id,
            "Status": status_label.get(a.schema_status, a.schema_status),
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
) -> None:
    """Do the three GitHub writes + optional dispatch. Shows per-step status.

    Each app's credentials live in their OWN repo secrets (APP_N_USER,
    APP_N_PASS). We write them directly — no bundle round-trip, no
    paste-back step. The workflow reads them via toJSON(secrets).
    """
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

    # 3. (optional) workflow dispatch — scoped to the NEW app only so
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
        dispatch_line = " Scrape will run at the next scheduled tick (00:00 / 12:00 IST)."

    # Stash banner + rerun so the apps table above refreshes (Streamlit
    # Cloud has a small delay before the redeploy picks up apps.yaml; the
    # local app_registry read may still show the old list until then).
    st.session_state["_add_app_success"] = (
        f"✅ **{name}** added as `{entry.id}` (schema: pending_review until first scrape).{dispatch_line}"
    )
    st.rerun()


# =================================================================
# Users tab (super admin only)
# =================================================================
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


# =================================================================
# Entrypoint for `streamlit run admin_ui.py`
# =================================================================
if __name__ == "__main__":
    main()
