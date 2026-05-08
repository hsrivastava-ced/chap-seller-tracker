# Deploying cedadmin as a separate Streamlit Cloud URL

The cedadmin panel ships from the **same git repo** as cHAP but runs
as a **second Streamlit Cloud app** so it gets its own URL and only
shows cedadmin in the sidebar (no cHAP page leakage).

## Why two apps, not one

| Concern | One-app | Two-app (chosen) |
| --- | --- | --- |
| URL | shared (`chap-seller-tracker.streamlit.app`) | distinct (`cedcommerce-admin.streamlit.app`) |
| Sidebar nav | both consoles always visible | only the relevant pages per URL |
| Per-team sharing | exposes cHAP to cedadmin team | each team gets only their console |
| Deploy cycle | one push redeploys everything | one push redeploys both (same repo) |
| Access control | `roles.yaml` + `cedadmin_roles.yaml` (already separate) | identical — gating is in code, not deployment |

## Repo layout (one repo, two entry points)

```
dashboard.py         ← cHAP entry  (auto-discovers pages/ → Admin, Intelligence, CedAdmin)
cedadmin_main.py     ← cedadmin entry (uses st.navigation; pages/ NOT discovered)
pages/
  Admin.py           ← cHAP admin
  Intelligence.py    ← cHAP intelligence
  CedAdmin.py        ← thin wrapper, ONLY visible from dashboard.py URL
cedadmin_ui.py       ← shared UI module — both wrappers call .main()
cedadmin_roles.yaml  ← cedadmin-specific access list
roles.yaml           ← cHAP-specific access list
```

The trick: `cedadmin_main.py` calls `st.navigation([...])` explicitly,
which makes Streamlit skip its automatic `pages/` discovery for that
deployment. So cHAP pages do not appear in the cedadmin sidebar.

## Streamlit Cloud setup

Both apps point at the **same** GitHub repository / branch (`main`).
Streamlit Cloud lets you deploy multiple apps from the same repo by
giving each a different "Main file path".

### App #1 — cHAP (existing, no changes)

| Field | Value |
| --- | --- |
| Repository | `hsrivastava-ced/chap-seller-tracker` |
| Branch | `main` |
| Main file path | `dashboard.py` |
| App URL | `chap-seller-tracker.streamlit.app` (current) |
| Secrets needed | `[supabase]`, `[github]`, `[auth]`, `[google_oauth]` |

### App #2 — cedadmin (new)

1. In Streamlit Cloud, click **New app**.
2. Pick the same `hsrivastava-ced/chap-seller-tracker` repo + `main` branch.
3. Set **Main file path** to `cedadmin_main.py`.
4. Pick a custom subdomain — suggestion: `cedcommerce-admin`.
5. Open **Advanced settings → Secrets** and paste:
   ```toml
   [auth]
   # same SECRET_KEY value as cHAP — shared signing key so a user
   # signed in on one URL is recognised on the other.
   SECRET_KEY = "..."

   [github]
   # PAT with `contents: write` on this repo so the Access tab can
   # commit cedadmin_roles.yaml back when grants change.
   owner = "hsrivastava-ced"
   repo = "chap-seller-tracker"
   pat = "..."

   # Supabase + google_oauth are NOT needed for cedadmin — the panel
   # reads CSV files committed to the repo, not Postgres, and uses
   # email/password auth shared with cHAP.
   ```
6. Click **Deploy**.

That's it — both URLs go live and stay in sync (one push to `main`
redeploys both). To pause cedadmin without touching cHAP, hit
**Settings → Hibernate** on App #2 only.

## Sharing URLs with the right teams

| Team | URL |
| --- | --- |
| Sales / CS leads watching cHAP | `chap-seller-tracker.streamlit.app` |
| Support team building Walmart SQLs | `cedcommerce-admin.streamlit.app` |
| You (project owner) | both — you have super_admin in both |

Access on each URL is gated independently:
- cHAP: edit `roles.yaml` (or use Admin → Users tab on app #1).
- cedadmin: edit `cedadmin_roles.yaml` (or use Access tab on app #2 — only super admins see it).

A user added to one is **NOT** automatically granted the other —
that's the "separate access" rule.

## Local dev

You can run either entry locally:

```bash
# cHAP
streamlit run dashboard.py

# cedadmin (single-tab, no cHAP nav)
streamlit run cedadmin_main.py
```

Same Python venv, same `.env`, no further config needed.
