"""
cedadmin_main.py — standalone Streamlit entry for the CedCommerce
admin (Yii2 admin.apps.cedcommerce.com) panel ONLY.

Why this exists:
    Streamlit Cloud routes one URL → one "main file path" per app.
    The cHAP dashboard runs from `dashboard.py` and uses the
    automatic `pages/` directory discovery (cHAP-Admin, Intelligence,
    CedAdmin all show up in its sidebar). That single deployment is
    fine for the project owner who wants both consoles, but it leaks
    cedadmin into every cHAP-only user's sidebar.

    Pointing a *second* Streamlit Cloud app at THIS file gives
    cedadmin its own URL (e.g. `cedcommerce-admin.streamlit.app`)
    that has no awareness of cHAP at all. Calling
    `st.navigation([...])` explicitly makes Streamlit skip its
    `pages/` auto-loader, so the cHAP pages do NOT appear here.

How to deploy on Streamlit Cloud (2-app setup):
    App #1 (cHAP — existing):
        Repository:        hsrivastava-ced/chap-seller-tracker
        Branch:            main
        Main file path:    dashboard.py
        Custom subdomain:  chap-seller-tracker (current)
        Secrets:           [supabase], [github] (cHAP repo PAT),
                           [auth], [google_oauth]
    App #2 (cedadmin — new):
        Repository:        hsrivastava-ced/chap-seller-tracker
        Branch:            main
        Main file path:    cedadmin_main.py   ← this file
        Custom subdomain:  cedcommerce-admin  (suggestion)
        Secrets:           same `[github]` block + `[auth]` so the
                           same login flow works; supabase NOT needed
                           because cedadmin reads CSV files in the
                           repo, not Postgres.
    Both apps share the same git history — pushing a fix to `main`
    redeploys both. Per-app access is enforced by `cedadmin_roles.yaml`
    (this file's gate) — a user with cHAP super_admin still cannot
    see cedadmin unless they're explicitly granted there.

Auth: shared. The `auth.gate()` flow is identical across both apps,
so signed-in cookies work on both URLs once the user has authenticated
on either.
"""
from __future__ import annotations

import streamlit as st

import cedadmin_ui


# st.navigation([...]) suppresses the pages/ auto-discovery, so the
# cHAP pages (Admin, Intelligence, CedAdmin) are NOT shown in this
# deployment's sidebar. Single-page experience for cedadmin operators.
pg = st.navigation([
    st.Page(
        cedadmin_ui.main,
        title="CedCommerce Admin",
        icon="🛒",
        url_path="cedadmin",
        default=True,
    ),
])
pg.run()
