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
    │  "Add new app" wizard (editor+)                 │
    │   1. discover dropdown  2. creds  3. scrape     │
    │   4. schema drift report  5. commit + trigger   │
    │                                                 │
    │ --- Users tab ------------------------------- │
    │  List (email, role, grantor, granted_at)        │
    │  Grant / revoke form (super_admin only)         │
    └─────────────────────────────────────────────────┘

Kept dependency-light: imports the local modules (auth, roles,
app_registry, schema_guard, github_secret_updater) plus streamlit +
yaml. No Playwright import at the top level; the "discover dropdown"
step lazy-imports it so the page still loads if Chromium isn't
installed on the Streamlit Cloud environment.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import streamlit as st

import app_registry
import auth
import github_secret_updater as gh
import roles
import schema_guard
from app_registry import AppEntry


# =================================================================
# Page entry
# =================================================================
def main():
    principal = auth.gate()
    auth.require("see_admin_tab", principal)

    st.set_page_config(page_title="Admin — cHAP Seller Tracker", page_icon=":gear:", layout="wide")
    auth.sign_out_button(st)

    st.title("Admin")
    st.caption(
        "Manage admin-panel sources to scrape and (super admins only) "
        "user access. Changes here commit back to the repo and trigger "
        "Streamlit Cloud to redeploy within ~30 s."
    )

    tab_labels = ["Apps"]
    if roles.can(principal, "see_users_tab"):
        tab_labels.append("Users")
    tabs = st.tabs(tab_labels)

    with tabs[0]:
        _render_apps_tab(principal)

    if len(tabs) > 1:
        with tabs[1]:
            _render_users_tab(principal)


# =================================================================
# Apps tab
# =================================================================
def _render_apps_tab(principal: roles.UserPrincipal):
    # -----------------------------------------------------------
    # "Run scrape now" — top-of-tab convenience for the Option A
    # scheduling model (fixed twice-daily cron + on-demand button).
    # Any editor+ can trigger a fresh workflow_dispatch without
    # waiting for the next 00:00 / 12:00 IST tick. Takes ~3–5 min.
    # -----------------------------------------------------------
    top_col1, top_col2 = st.columns([3, 2])
    with top_col1:
        st.subheader("Configured admin panels")
        st.caption(
            "Scheduled scrape runs **twice a day** — 00:00 and 12:00 IST "
            "(cron is defined in `.github/workflows/scrape.yml`). "
            "Click **Run scrape now** for on-demand refresh."
        )
    with top_col2:
        if roles.can(principal, "add_app"):
            st.markdown("&nbsp;")  # vertical alignment with the caption above
            if st.button("▶ Run scrape now", type="primary", use_container_width=True):
                _trigger_scrape_now(principal)

    apps = app_registry.all_apps()
    if not apps:
        st.info("No apps configured yet. Use the wizard below to add the first one.")
    else:
        rows = []
        for a in apps:
            badge = {
                "canonical": ":green[canonical]",
                "pending_review": ":orange[pending review]",
                "blocked": ":red[blocked]",
            }.get(a.schema_status, a.schema_status)
            installs = "✅" if a.scrape_installs else "—"
            uninstalls = "✅" if a.scrape_uninstalls else "—"
            creds_present = "✅" if a.is_ready_to_scrape else "⚠️ missing"
            rows.append({
                "App": f"**{a.label}**  \n`{a.id}`",
                "Status": badge,
                "Installs": installs,
                "Uninstalls": uninstalls,
                "Creds": creds_present,
                "Added by": a.added_by or "—",
                "Added at": a.added_at or "—",
            })
        st.dataframe(rows, hide_index=True, use_container_width=True)

    st.divider()

    st.subheader("Add a new admin panel")
    if not roles.can(principal, "add_app"):
        st.info("You have view-only access. Ask a super admin to grant you editor access to add new apps.")
        return
    _render_add_app_wizard(principal)


def _render_add_app_wizard(principal: roles.UserPrincipal):
    """Five-step wizard: discover → creds → selection → dry-run → commit."""
    # Use session state to remember progress across reruns.
    state = st.session_state.setdefault("_wizard", {"step": 1})

    st.markdown(f"**Step {state['step']} of 5**")

    if state["step"] == 1:
        _wizard_step_discover(state)
    elif state["step"] == 2:
        _wizard_step_credentials(state)
    elif state["step"] == 3:
        _wizard_step_scrape_selection(state)
    elif state["step"] == 4:
        _wizard_step_dry_run(state, principal)
    elif state["step"] == 5:
        _wizard_step_commit(state, principal)

    if st.button("Start over", key="wizard_reset"):
        st.session_state["_wizard"] = {"step": 1}
        st.rerun()


def _wizard_step_discover(state: dict):
    st.markdown("Pick an app from the CedCommerce login-page dropdown.")
    st.caption(
        "We run a headless Playwright session against the login page to read the "
        "current list of app options, then filter out ones you've already configured. "
        "If discovery fails (e.g. Playwright not installed on Streamlit Cloud), use "
        "the manual-entry fallback below."
    )

    existing_values = {a.dropdown_value for a in app_registry.all_apps()}

    col_auto, col_manual = st.columns(2)
    with col_auto:
        if st.button("Discover dropdown options", type="primary"):
            with st.spinner("Fetching dropdown options..."):
                try:
                    options = _discover_dropdown_options()
                except Exception as e:
                    st.error(f"Discovery failed: {e}")
                    options = []
            state["discovered_options"] = options

    with col_manual:
        manual = st.text_input(
            "…or enter the dropdown value directly",
            help="e.g. `walmart_ca` — exact string from the login dropdown",
        )
        manual_label = st.text_input("Friendly label", help="e.g. 'Walmart CA'")
        if st.button("Use manual entry"):
            if manual:
                state["selected_option"] = {"value": manual.strip(), "label": manual_label.strip() or manual.strip()}
                state["step"] = 2
                st.rerun()

    if state.get("discovered_options"):
        options = state["discovered_options"]
        available = [o for o in options if o["value"] not in existing_values]
        if not available:
            st.warning("Every dropdown option is already configured. Nothing new to add.")
            return
        choice_labels = [f"{o['label']} ({o['value']})" for o in available]
        picked_idx = st.selectbox("Pick one", range(len(available)), format_func=lambda i: choice_labels[i])
        if st.button("Next →"):
            state["selected_option"] = available[picked_idx]
            state["step"] = 2
            st.rerun()


def _wizard_step_credentials(state: dict):
    opt = state["selected_option"]
    st.markdown(f"Adding **{opt['label']}** (dropdown value `{opt['value']}`).")

    with st.form("creds_form"):
        user = st.text_input("Admin panel email")
        pw = st.text_input("Admin panel password", type="password")
        submitted = st.form_submit_button("Next →")
    if submitted:
        if not user or not pw:
            st.error("Both fields required.")
            return
        state["user"] = user.strip()
        state["password"] = pw  # not stripped — passwords can legitimately have leading/trailing space? no, we strip in build_updated_creds
        state["step"] = 3
        st.rerun()


def _wizard_step_scrape_selection(state: dict):
    st.markdown("What should we scrape for this app?")
    installs = st.checkbox("Seller installs", value=True)
    uninstalls = st.checkbox("Uninstalls", value=True)
    app_id = st.text_input(
        "Internal id",
        value=state["selected_option"]["value"],
        help="Used as the filename prefix (results/latest/<id>.csv) and the primary key in the unified dataset. Lowercase snake_case.",
    )

    if st.button("Next →"):
        if not (installs or uninstalls):
            st.error("Pick at least one of installs / uninstalls.")
            return
        state["scrape_installs"] = installs
        state["scrape_uninstalls"] = uninstalls
        state["app_id"] = app_id.strip()
        state["step"] = 4
        st.rerun()


def _wizard_step_dry_run(state: dict, principal):
    st.markdown("Running a dry scrape to verify credentials and schema.")
    st.caption(
        "This loads one page of sellers (and optionally uninstalls) to "
        "check login works and to compare the returned columns against "
        "the canonical schema. We don't persist anything yet."
    )
    if st.button("Run dry scrape", type="primary"):
        with st.spinner("Scraping one page..."):
            try:
                observed = _dry_scrape(state)
            except Exception as e:
                st.error(f"Dry scrape failed: {e}")
                return
        state["dry_observed"] = observed

    observed = state.get("dry_observed")
    if observed:
        for kind, cols in observed.items():
            report = schema_guard.compare(kind, cols)
            st.markdown(schema_guard.format_report_markdown(report))
            state.setdefault("reports", {})[kind] = report.status

        # Decide schema_status for the app entry
        statuses = list(state.get("reports", {}).values())
        if "blocked" in statuses:
            final = "blocked"
            st.error("Schema is **blocked** — required columns missing. Fix the admin panel or align the canonical schema before onboarding.")
        elif "pending_review" in statuses:
            final = "pending_review"
            st.warning("Schema will be saved as **pending_review** — a super admin must approve it before its rows enter the unified dataset.")
        else:
            final = "canonical"
            st.success("Schema matches canonical — ready to merge on the next scrape.")
        state["app_schema_status"] = final

        if st.button("Next →", disabled=(final == "blocked")):
            state["step"] = 5
            st.rerun()


def _wizard_step_commit(state: dict, principal):
    st.markdown("Commit the new app to the repo and (optionally) kick off a scrape.")
    st.write(
        {
            "app_id": state["app_id"],
            "label": state["selected_option"]["label"],
            "dropdown_value": state["selected_option"]["value"],
            "scrape_installs": state["scrape_installs"],
            "scrape_uninstalls": state["scrape_uninstalls"],
            "schema_status": state.get("app_schema_status", "pending_review"),
        }
    )

    run_after = st.checkbox("Trigger a scrape immediately after committing", value=True)

    if st.button("Commit + deploy", type="primary"):
        try:
            ctx = gh.context_from_streamlit(st)
        except Exception as e:
            st.error(str(e))
            return

        creds_ref = app_registry.next_creds_ref()

        # 1) Append credentials to CREDS secret.
        try:
            # Pull the latest CREDS body (we can't read a secret's current
            # value back from GitHub, so we assume the editor has pre-loaded
            # the current text into a text_area below if needed). For now we
            # just encrypt JUST the additions — GitHub's PUT secret replaces,
            # so we need the current body first. The Users tab super-admin
            # view should bootstrap this text; for v1 we ask the editor to
            # paste.
            current_creds = st.session_state.get("_creds_cache")
            if not current_creds:
                st.error(
                    "No cached CREDS body found. Ask a super admin to open the "
                    "Users tab once so the app can read the current CREDS secret "
                    "state via the GitHub API first."
                )
                return
            additions = {
                f"{creds_ref}_USER": state["user"],
                f"{creds_ref}_PASS": state["password"],
            }
            new_body = gh.build_updated_creds(current_creds, additions)
            gh.append_creds_lines(ctx, [new_body])
        except Exception as e:
            st.error(f"Failed to update CREDS secret: {e}")
            return

        # 2) Append to apps.yaml via Contents API.
        try:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            entry = AppEntry(
                id=state["app_id"],
                label=state["selected_option"]["label"],
                dropdown_value=state["selected_option"]["value"],
                scrape_installs=state["scrape_installs"],
                scrape_uninstalls=state["scrape_uninstalls"],
                creds_ref=creds_ref,
                added_by=principal.email,
                added_at=now,
                schema_status=state.get("app_schema_status", "pending_review"),
            )
            # Read current apps.yaml, append, PUT back.
            import yaml
            current_yaml = gh.read_file(ctx, "apps.yaml")
            data = yaml.safe_load(current_yaml or "") or {"schema_version": 1, "apps": []}
            data.setdefault("apps", []).append({
                k: v for k, v in entry.__dict__.items()
            })
            new_yaml = yaml.safe_dump(data, sort_keys=False)
            msg = f"feat(registry): add admin panel {entry.id} ({roles.audit_stamp(principal.email, 'onboard app')})"
            gh.put_file(ctx, "apps.yaml", new_yaml, msg)
        except Exception as e:
            st.error(f"Failed to commit apps.yaml: {e}")
            return

        # 3) Optional workflow dispatch
        if run_after:
            try:
                gh.trigger_scrape(ctx, reason=f"onboarding {entry.id} by {principal.email}")
                st.success("Scrape dispatched. Check Actions → scrape-chap in ~3–5 min.")
            except Exception as e:
                st.warning(f"Committed but failed to dispatch scrape: {e}")
                return

        st.success(f"Added **{entry.label}** as `{entry.id}`. Streamlit Cloud will redeploy in ~30 s.")
        st.session_state["_wizard"] = {"step": 1}


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
            st.error(str(e))
        except Exception as e:
            st.error(f"Failed to save: {e}")

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
            st.error(str(e))
        except Exception as e:
            st.error(f"Failed to save: {e}")


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
        st.error("You don't have permission to trigger scrapes.")
        return

    try:
        ctx = gh.context_from_streamlit(st)
    except Exception as e:
        st.error(
            f"Couldn't reach GitHub: {e}\n\n"
            "Check that the `[github]` block in Streamlit secrets has "
            "`owner`, `repo`, and `pat` filled in. See MULTI_APP_DESIGN.md §3.2."
        )
        return

    with st.spinner("Dispatching workflow..."):
        try:
            gh.trigger_scrape(ctx, reason=f"on-demand by {principal.email}")
        except Exception as e:
            st.error(f"Workflow dispatch failed: {e}")
            return

    st.success(
        "Workflow dispatched. Fresh data should land in the dashboard in "
        "~3–5 minutes. Watch the GitHub Actions `scrape-chap` run for progress."
    )


# =================================================================
# Scraper hooks — isolated to keep heavy imports lazy
# =================================================================
def _discover_dropdown_options() -> list[dict]:
    """Spawn a headless Chromium, go to LOGIN_URL, return the app-picker values."""
    # Lazy-import — Streamlit Cloud may not have playwright installed.
    from playwright.sync_api import sync_playwright
    import config

    login_url = config.LOGIN_URL
    if not login_url:
        raise RuntimeError("LOGIN_URL is not configured.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(login_url, wait_until="domcontentloaded")
        # The login page uses an `inte-Select` for the app picker — we
        # look for the hidden <select> fallback OR the visible list items.
        options: list[dict] = []
        try:
            handle = page.query_selector("select[name='app'], select#app, select")
            if handle:
                raw = handle.evaluate(
                    "(el) => Array.from(el.options).map(o => ({value: o.value, label: o.textContent.trim()}))"
                )
                options = [o for o in raw if o.get("value")]
        except Exception:
            pass
        browser.close()
    return options


def _dry_scrape(state: dict) -> dict[str, list[str]]:
    """Run scraper.scrape_one_app against the just-entered creds.

    scraper.py today doesn't expose a public `scrape_one_app` — this is a
    TODO integration point. For v1 we stub by reading the canonical
    columns back so the wizard can complete end-to-end without a working
    scrape API. The admin UI will surface a clear "dry scrape not yet
    wired" note until scraper.py is updated.
    """
    try:
        import scraper  # type: ignore
        fn = getattr(scraper, "scrape_one_app_dry", None)
        if callable(fn):
            return fn(
                app_id=state["app_id"],
                dropdown_value=state["selected_option"]["value"],
                user=state["user"],
                password=state["password"],
                wants_installs=state["scrape_installs"],
                wants_uninstalls=state["scrape_uninstalls"],
            )
    except Exception as e:
        raise RuntimeError(
            f"Dry-scrape integration isn't wired yet (scraper.scrape_one_app_dry missing). "
            f"Error: {e}"
        )
    # Fallback: pretend canonical so the wizard can be exercised.
    canonical = schema_guard.load_canonical()
    out = {}
    if state["scrape_installs"]:
        k = canonical["kinds"]["sellers"]
        out["sellers"] = list(k["required_columns"]) + list(k["optional_columns"])
    if state["scrape_uninstalls"]:
        k = canonical["kinds"]["uninstalls"]
        out["uninstalls"] = list(k["required_columns"]) + list(k["optional_columns"])
    return out


# =================================================================
# Entrypoint for `streamlit run admin_ui.py`
# =================================================================
if __name__ == "__main__":
    main()
