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

from datetime import datetime, timezone
from typing import Optional

import streamlit as st

import app_registry
import auth
import github_secret_updater as gh
import roles
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
        st.info("No apps configured yet. Fill in the form below to add the first one.")
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
    """Single-form onboarding: name → creds → scrape toggles → commit + dispatch.

    Replaces the 5-step wizard. Three reasons:
      - Playwright discovery never worked on Streamlit Cloud (no Chromium).
      - The dry-scrape step was a TODO stub that returned canned data.
      - Multi-step state machine made a rare action feel heavy.
    Validation that used to live in the dry-scrape step now happens on the
    first real scrape — the guardrail + schema_guard flag the app
    "pending_review" or "canonical" in the table above.
    """
    # Show last-success banner, if any, so a rerun after submit doesn't
    # look like nothing happened.
    last = st.session_state.pop("_add_app_success", None)
    if last:
        st.success(last)

    existing_ids = {a.id for a in app_registry.all_apps()}
    cache_present = bool(st.session_state.get("_creds_cache"))

    if not cache_present:
        st.info(
            "**First time adding an app in this session.** GitHub doesn't let us "
            "read the current `CREDS` secret back, so the first add needs you to "
            "paste the current body (see the **Current CREDS bundle** section at "
            "the bottom). Subsequent adds will reuse a cached copy automatically."
        )

    with st.form("add_app_form", clear_on_submit=False):
        c1, c2 = st.columns(2)
        with c1:
            name = st.text_input(
                "Friendly name",
                placeholder="Walmart CA",
                help="Shown in dashboards and the apps table.",
            )
        with c2:
            app_id = st.text_input(
                "Internal id",
                placeholder="walmart_ca",
                help="Lowercase snake_case. Used as CSV filename prefix and DB key.",
            )

        dropdown = st.text_input(
            "Dropdown value",
            placeholder="walmart_ca",
            help=(
                "The exact `value` attribute from the app-picker `<select>` on the "
                "CedCommerce login page. Right-click the dropdown → Inspect to grab "
                "it. Often identical to the internal id."
            ),
        )

        st.markdown("**Credentials for this admin panel**")
        c3, c4 = st.columns(2)
        with c3:
            email = st.text_input("Email", placeholder="ops@cedcommerce.com")
        with c4:
            password = st.text_input("Password", type="password")

        st.markdown("**What to scrape**")
        c5, c6 = st.columns(2)
        with c5:
            wants_installs = st.checkbox("Seller installs", value=True)
        with c6:
            wants_uninstalls = st.checkbox("Uninstalls", value=True)

        run_after = st.checkbox(
            "Run a scrape right after adding",
            value=True,
            help="Dispatches the GitHub Actions workflow. Results land in ~3-5 min.",
        )

        with st.expander(
            "Current CREDS bundle" + ("  (cached — expand only to override)" if cache_present else "  ← REQUIRED for first add"),
            expanded=not cache_present,
        ):
            st.caption(
                "GitHub secrets can be written but not read back. Paste the current "
                "`CREDS` repo-secret body so we can append the new `APP_N_USER` / "
                "`APP_N_PASS` lines without losing the existing apps."
            )
            creds_body = st.text_area(
                "CREDS body",
                value=st.session_state.get("_creds_cache", ""),
                height=160,
                label_visibility="collapsed",
                placeholder="APP_1_USER=...\nAPP_1_PASS=...\nAPP_2_USER=...",
            )

        submitted = st.form_submit_button(
            "Add admin panel", type="primary", use_container_width=True
        )

    if not submitted:
        return

    # --- Validation -----------------------------------------------------
    errs: list[str] = []
    if not name.strip():
        errs.append("Friendly name is required.")
    if not app_id.strip():
        errs.append("Internal id is required.")
    elif not all(c.islower() or c.isdigit() or c == "_" for c in app_id.strip()):
        errs.append("Internal id must be lowercase snake_case (letters, digits, underscores).")
    elif app_id.strip() in existing_ids:
        errs.append(f"Internal id `{app_id.strip()}` already exists.")
    if not dropdown.strip():
        errs.append("Dropdown value is required.")
    if not email.strip() or not password:
        errs.append("Both email and password are required.")
    if not (wants_installs or wants_uninstalls):
        errs.append("Pick at least one of installs / uninstalls.")
    if not creds_body.strip():
        errs.append(
            "Paste the current CREDS bundle (see the section above). "
            "We need it to append new credentials without dropping existing ones."
        )
    if errs:
        for e in errs:
            st.error(e)
        return

    # --- Commit flow ----------------------------------------------------
    _commit_new_app(
        principal=principal,
        name=name.strip(),
        app_id=app_id.strip(),
        dropdown=dropdown.strip(),
        email=email.strip(),
        password=password,
        wants_installs=wants_installs,
        wants_uninstalls=wants_uninstalls,
        current_creds=creds_body,
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
    current_creds: str,
    run_after: bool,
) -> None:
    """Do the three GitHub writes + optional dispatch. Shows per-step status."""
    try:
        ctx = gh.context_from_streamlit(st)
    except Exception as e:
        st.error(str(e))
        return

    creds_ref = app_registry.next_creds_ref()

    # 1. CREDS secret
    with st.status("Updating CREDS secret…", expanded=False) as status:
        try:
            new_body = gh.build_updated_creds(
                current_creds,
                {
                    f"{creds_ref}_USER": email,
                    f"{creds_ref}_PASS": password,
                },
            )
            gh.append_creds_lines(ctx, [new_body])
            # Cache for next add in same session.
            st.session_state["_creds_cache"] = new_body
            status.update(label="CREDS secret updated", state="complete")
        except Exception as e:
            status.update(label="CREDS update failed", state="error")
            st.error(f"Couldn't update CREDS secret: {e}")
            return

    # 2. apps.yaml
    with st.status("Committing apps.yaml…", expanded=False) as status:
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
            status.update(label="apps.yaml committed", state="complete")
        except Exception as e:
            status.update(label="apps.yaml commit failed", state="error")
            st.error(
                f"CREDS updated but apps.yaml commit failed: {e}. "
                f"Check the repo — you may need to revert the CREDS secret."
            )
            return

    # 3. (optional) workflow dispatch
    if run_after:
        try:
            gh.trigger_scrape(
                ctx, reason=f"onboarding {entry.id} by {principal.email}"
            )
            dispatch_line = (
                f" Scrape dispatched — check the Actions tab for results in "
                f"~3-5 min."
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
# Entrypoint for `streamlit run admin_ui.py`
# =================================================================
if __name__ == "__main__":
    main()
