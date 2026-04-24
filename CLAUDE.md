<!-- code-review-graph MCP tools -->
## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.

---

## Production state (post-launch — 2026-04-25)

This is the load-bearing context any agent picking up this repo should
know.

**Active app fleet (4):** TEMU US (`shopify_temu`), SHEIN (`shein`),
TEMU EU (`shopify_temu_eu`), SHEIN WooCommerce (`shein_woocommerce`).
All ride the shared cron in `.github/workflows/scrape.yml` (06:30 +
18:30 UTC daily). Per-app credentials live in repo secrets
`APP_N_USER` / `APP_N_PASS` keyed by `creds_ref` in `apps.yaml`.

**Removed before launch:** `shopify_gearexchange` (was APP_5). Repo
secrets for APP_5 are intentionally kept in case re-onboarding is
needed.

**Streamlit surfaces:**
- `dashboard.py` (entry, stakeholder analytics)
- `pages/Admin.py` → `admin_ui.py` (onboard apps, view runs, manage users)
- `pages/Intelligence.py` → `intelligence_ui.py` (sales-rep leads + super-admin AI preview)

**Scraper quirks worth remembering:**
- The cHAP seller page renders a framework dropdown (`shopify`/
  `prestashop`/etc.) above the table. `_ensure_framework_filter_is_all`
  in `scraper.py` always flips it to `all` after login, then waits
  for `.ant-spin-spinning` to disappear before letting downstream
  pagination read state. Skipping the spinner wait causes scraper to
  read `1 total page` mid-refresh and miss most rows.
- The dropdown is in the topbar, NOT inside `<main>`. Selectors:
  `div.inte-formElement.inte-select.custom-select--style` for the
  select, `.inte__Select--Selected` for the current value,
  `li.inte-Select__Select--Item[value='all']` for the option.
- Always-on table columns (`Action`, `Seller Id`, `Username`) are
  NOT in the Customize Grid popup — they're permanent. Don't list
  them in `grid_columns.yaml::default.required` or the validator
  will false-block every run.

**Deferred (don't rebuild from scratch):**
- Manual-edit UI on the sellers table (Task #80) — Supabase RPC
  `upsert_sellers_with_guard` and `apply_manual_edit` already exist
  in `supabase_client.py`; the Streamlit UI to call them isn't built.
- AI seller-website enrichment — `seller_profile_enricher.py` is
  ready but no-ops in dry-run mode (no `ANTHROPIC_API_KEY`).
  Stakeholder preview lives in `intelligence_ui.py::_DEMO_SUMMARY`.

**Where to look:**
- Live app: deploys from `main` to Streamlit Cloud.
- cHAP source: https://app-v2-frontend.cifapps.com/auth/login
- Repo: https://github.com/hsrivastava-ced/chap-seller-tracker
- Run history: Admin → Runs tab (or GitHub → Actions).
