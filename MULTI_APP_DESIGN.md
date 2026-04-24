# Multi-App + Auth + RBAC — Design

**Status:** in progress (2026-04-24) · **Author:** autonomous build kicked off by Hrithik's
"start the thing now" ask. Push back on anything here before it calcifies into code.

---

## 1. The ask, in one paragraph

Today the scraper + dashboard supports three hard-coded apps (`shopify_temu`,
`shein`, `shopify_temu_eu`). The CedCommerce login URL has a dropdown with
**many** more apps. Threecolts teammates beyond Hrithik should be able to:

1. Sign in to the dashboard with their `@threecolts.com` Google account — no
   one outside the domain gets in.
2. See analytics (default for any `@threecolts.com` user).
3. If granted **editor** access by the super admin, add a new admin-panel
   source — pick it from the live dropdown, paste credentials, pick what to
   scrape (installs / uninstalls / both) — and have the next scheduled scrape
   pull data from it and merge into the same tables.
4. If **super admin** (only `hsrivastava@threecolts.com` today), see and manage
   who has which role.

Schema drift between apps must never silently corrupt the unified dataset.

---

## 2. What moves from code into config

### 2.1 `apps.yaml` (committed, hand-edited only rarely)

Replaces the hard-coded `APP_IDS` / `CREDENTIALS` dicts in `config.py`.

```yaml
# apps.yaml — registry of admin-panel sources to scrape.
# Add new entries via the Streamlit admin UI; it writes + commits this file.
schema_version: 1

apps:
  - id: shopify_temu
    label: "TEMU US (Shopify)"
    dropdown_value: "shopify_temu"        # exact value passed to the login dropdown
    scrape_installs: true
    scrape_uninstalls: true
    creds_ref: APP_1                      # maps to APP_1_USER / APP_1_PASS inside CREDS
    added_by: hsrivastava@threecolts.com
    added_at: 2026-04-17T00:00:00Z
    schema_status: canonical              # canonical | pending_review | blocked

  - id: shein
    label: "SHEIN"
    dropdown_value: "shein"
    scrape_installs: true
    scrape_uninstalls: true
    creds_ref: APP_2
    added_by: hsrivastava@threecolts.com
    added_at: 2026-04-17T00:00:00Z
    schema_status: canonical

  - id: shopify_temu_eu
    label: "TEMU EU (Shopify)"
    dropdown_value: "shopify_temu_eu"
    scrape_installs: true
    scrape_uninstalls: true
    creds_ref: APP_3
    added_by: hsrivastava@threecolts.com
    added_at: 2026-04-17T00:00:00Z
    schema_status: canonical
```

### 2.2 `roles.yaml` (committed, edited via UI)

```yaml
# roles.yaml — RBAC for the dashboard.
# Edited only via the super-admin UI; commits are made automatically.
schema_version: 1

# Super admin is ALSO hard-coded in auth.py as a safety net — even if
# this file goes missing or malformed, hsrivastava@threecolts.com can
# always log in and fix it.
roles:
  hsrivastava@threecolts.com: super_admin
  # other @threecolts.com users default to "viewer" when absent
  # example entries:
  # priya@threecolts.com: editor
  # raj@threecolts.com: viewer
```

### 2.3 `canonical_schema.json` (committed, updated by schema_guard)

Records the column list we consider authoritative per `kind`
(`sellers` / `uninstalls`). Any new app whose columns differ triggers
`schema_status: pending_review` on its `apps.yaml` entry, and its rows are
excluded from the unified dataset until a super admin clicks "Approve" in
the admin UI.

---

## 3. Secrets layout

### 3.1 `CREDS` (bundled GitHub Actions secret, unchanged)

Still a dotenv-style block. Each app entry in `apps.yaml` has a `creds_ref`
(e.g. `APP_1`) that means:
`<creds_ref>_USER` and `<creds_ref>_PASS` live inside `CREDS`.

When the admin UI adds a new app, it:
1. Picks the next free slot (`APP_4`, `APP_5`, ...).
2. Appends `APP_4_USER="..."` and `APP_4_PASS="..."` lines to CREDS via the
   GitHub REST API (encrypted with the repo's libsodium public key).
3. Commits an updated `apps.yaml` with the new entry (`creds_ref: APP_4`).

### 3.2 `GH_ADMIN_PAT` (Streamlit Cloud secret, new)

Fine-grained GitHub PAT owned by Hrithik, scoped to the `chap-seller-tracker`
repo, with permissions:
- `contents: read/write` — so the UI can commit `apps.yaml` / `roles.yaml`
- `secrets: read/write` — so the UI can append credentials to `CREDS`
- `actions: read/write` — so the UI can trigger a fresh scrape after onboarding

Exposed to Streamlit only (never to the scraper — the scraper reads from
`CREDS` like today). Rotate if it ever leaks.

### 3.3 Google OAuth client (Streamlit secret, new)

`[auth]` block in Streamlit Cloud secrets (the shape Streamlit's native
`st.login()` expects):

```toml
[auth]
redirect_uri = "https://dashboard.threecolts.com/oauth2callback"
cookie_secret = "<64-char random>"
client_id = "<from Google Cloud console>"
client_secret = "<from Google Cloud console>"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
```

A single Google Cloud OAuth client (Web application), redirect URI set to
the Streamlit URL. No consent-screen domain restriction needed — we enforce
`@threecolts.com` in `auth.py`.

---

## 4. Auth + RBAC flow

```
 ┌──────────────────────────────────────────────────────────────┐
 │  user visits dashboard.threecolts.com                        │
 └───────────────┬──────────────────────────────────────────────┘
                 │
                 ▼
        ┌────────────────────┐        ┌──────────────────────┐
        │ st.login("google") │──►not  │ "Sign in with Google" │
        └────────┬───────────┘  auth  │  button               │
                 │                    └──────────────────────┘
                 ▼
        ┌─────────────────────────────────────┐
        │ auth.gate():                         │
        │  email = st.experimental_user.email  │
        │  if not email.endswith("@threecolts.com"):  st.stop() │
        │  role = roles.lookup(email)          │
        │  st.session_state["role"] = role     │
        └────────┬────────────────────────────┘
                 │
                 ▼
        ┌─────────────────────────┐
        │  render dashboard.py    │
        │  + admin tab shown iff  │
        │    role in {editor,super_admin} │
        │  + users tab shown iff  │
        │    role == super_admin  │
        └─────────────────────────┘
```

### Role permissions

| Capability                          | viewer | editor | super_admin |
|-------------------------------------|:-:|:-:|:-:|
| View dashboard analytics            | ✅ | ✅ | ✅ |
| Export CSVs from dashboard          | ✅ | ✅ | ✅ |
| See "Admin → Apps" page             | ❌ | ✅ | ✅ |
| Add new app (scrape onboarding)     | ❌ | ✅ | ✅ |
| Approve schema drift                | ❌ | ❌ | ✅ |
| See "Admin → Users" page            | ❌ | ❌ | ✅ |
| Grant / revoke roles                | ❌ | ❌ | ✅ |

Schema-drift approval is super-admin-only on purpose — it's the one lever
that can silently corrupt the unified dataset.

---

## 5. Add-new-app wizard (editor)

```
step 1: discover
  → scraper.discover_dropdown_options(LOGIN_URL) -- live Playwright call
  → returns [{value: "walmart_ca", label: "Walmart CA"}, ...]
  → filter out already-configured values (those in apps.yaml)
  → user picks ONE

step 2: credentials
  → user enters email + password
  → (client-side hint: paste the same creds you use on the admin panel)

step 3: scrape selection
  → installs?   [x]
  → uninstalls? [x]

step 4: dry run
  → we call scraper.scrape_one_app(...) with a hard cap of 1 page
  → extract the column list
  → schema_guard.compare_to_canonical(kind, columns)
  → if canonical: show "✅ matches canonical schema"
  → if drift:     show diff table, status = pending_review,
                  rows stored but excluded from unified view until
                  super admin approves

step 5: commit
  → append to CREDS secret (APP_N_USER / APP_N_PASS)
  → append to apps.yaml (new entry with creds_ref: APP_N)
  → optional: trigger a full scrape now via workflow_dispatch
```

---

## 6. Files this design introduces

| Path                        | Role |
|-----------------------------|------|
| `apps.yaml`                 | app registry, committed |
| `roles.yaml`                | user → role map, committed |
| `canonical_schema.json`     | expected column lists per kind |
| `app_registry.py`           | loads apps.yaml + CREDS into CREDENTIALS dict |
| `auth.py`                   | Google OAuth gate + `@threecolts.com` enforcement |
| `roles.py`                  | role lookup + permission checks |
| `schema_guard.py`           | schema drift detection for new apps |
| `admin_ui.py`               | Streamlit page: apps + users admin |
| `github_secret_updater.py`  | libsodium-encrypted secret updates via REST API |
| `MULTI_APP_DESIGN.md`       | this doc |

Files touched (minimal diffs):
- `config.py` — delegate to `app_registry`
- `scraper.py` — iterate `app_registry.active_apps()` instead of hard-coded
  loop; honor `scrape_installs` / `scrape_uninstalls` flags per app
- `dashboard.py` — add `auth.gate()` at top, show admin tab link conditionally
- `requirements.txt` — add `pyyaml`, `pynacl`, `requests` (requests is
  likely already there), `streamlit>=1.42` for native `st.login()`
- `.github/workflows/scrape.yml` — no change (CREDS still parsed in Python)

---

## 7. Open questions I decided on my own (push back if wrong)

1. **Auth mechanism: Streamlit native `st.login()`, not Cloudflare Access.**
   Cloudflare Access would be simpler operationally but Streamlit native is
   chosen because (a) it keeps the identity inside the app so RBAC logic
   runs against a known email, and (b) no dependency on a specific
   Cloudflare plan. If Streamlit 1.42+ native login proves flaky, fallback
   is `streamlit-oauth` — same Google OAuth client, different library.

2. **Roles storage: YAML in the repo, not Supabase.**
   Rationale: simple, version-controlled ("who granted this?" is a git
   blame away), no extra network hop on every page load, survives Supabase
   outages. Trade-off: role changes trigger a repo push (~30 s Streamlit
   redeploy). Acceptable given change frequency is low.

3. **Super admin is hard-coded in `auth.py` AS WELL AS `roles.yaml`.**
   So even a broken `roles.yaml` can't lock Hrithik out. He can always log
   in, open the users tab, and fix the file. Multiple super-admins can be
   added via the UI later.

4. **New-app onboarding requires a live Playwright call from Streamlit Cloud
   to the CedCommerce login page (step 1, discover dropdown).** This means
   Streamlit Cloud needs Playwright + Chromium installed, not just the
   GitHub Actions runner. `packages.txt` + `requirements.txt` entries
   needed. If this proves too heavy on Streamlit's free tier, fallback:
   super admin pastes the dropdown option list manually one time.

5. **Schema drift: new app's rows are saved but fenced.** They persist to
   `results/latest/{app}.csv` with a `schema_status=pending_review` marker
   in `run.json`. The unified analytics in `dashboard.py` filter to
   `schema_status=canonical` by default. Super admin can either approve
   (promoting them) or unlink the app.

---

## 8. Rollout order (what I'm actually building now)

1. ✅ This doc.
2. Registry foundation: `apps.yaml`, `app_registry.py`, `config.py` refactor.
3. RBAC foundation: `roles.yaml`, `roles.py` (with super_admin hard-coded fallback).
4. Auth module: `auth.py` (Google OAuth gate).
5. Schema drift guard: `schema_guard.py` + `canonical_schema.json`.
6. GitHub secret updater: `github_secret_updater.py`.
7. Admin UI: `admin_ui.py` (apps + users tabs).
8. Wire into `dashboard.py` (thin integration, no rewrite).
9. Update `scraper.py` to honor new registry (needs the big file — last).
10. `requirements.txt` bump + Streamlit secrets doc.

Phases 2–7 are independent modules; each is usable on its own. Phase 8+
is where the pieces meet the existing large files, so those land last to
minimize risk.
