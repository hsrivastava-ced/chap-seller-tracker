import csv
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
from config import APP_IDS, LOGIN_URL, USERNAME, PASSWORD, HEADLESS, CREDENTIALS
import scrape_validator as sv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DEBUG_DOM_FILE = "debug_dom.txt"
ERROR_SHOT_FILE = "error_debug.png"

# Where persisted output lands. `results/latest/` always holds the freshest
# per-app CSV (handy for diffs); `results/history/<timestamp>/` keeps an
# append-only audit of every run.
RESULTS_DIR = Path("results")
LATEST_DIR = RESULTS_DIR / "latest"
HISTORY_DIR = RESULTS_DIR / "history"
# Staging lands alongside latest/; we promote it atomically only when every
# per-app validation report clears `is_promotable`. When validation blocks
# the run, latest/ is left UNTOUCHED — consumers (dashboard, Supabase push)
# keep reading the previous good snapshot — and INVALID_RUN.md lands in
# latest/ as a breadcrumb for the next operator.
STAGING_DIR = RESULTS_DIR / "staging"

# Canonical column order for CSVs — keeps every run's columns stable even
# though not every app populates every field (e.g. shein-only `app_type`).
CSV_COLUMNS = [
    "seller_id",
    "store_url",
    "username",
    "email",
    "platforms",
    "installed_on",
    "app_type",
    "source_country",
    # Post-"Customize Grid" KPIs. When the customize step succeeds these
    # hold real numbers; when it fails they'll be blank (scraper is tolerant
    # of missing header entries — see extract_row_data).
    "order_count",
    "product_count",
    "failed_order_count",
    "steps_completed",
    "plan",
    "last_sync",
    "webhooks",
    "action",
]

# Uninstalls table has a very different shape than the Seller List.
# Verified 2026-04-18 against the live cHAP uninstalls page (user screenshot):
# the table exposes only 4 columns — `User Id | Email | Shops | Username`.
# The `Shops` cell is a NESTED list of `(platform, date, time)` tuples per
# row (one user can uninstall multiple platforms — e.g. Shopify + Shein at
# different timestamps). We explode that into one CSV row per
# (seller_id, platform) uninstall event so diffing + Supabase upsert keys
# stay unambiguous.
UNINSTALL_CSV_COLUMNS = [
    "seller_id",
    "email",
    "username",
    "platform",        # e.g. "Shopify" / "Shein" / "Temu" (one per uninstall)
    "uninstalled_on",  # "YYYY-MM-DD HH:MM:SS" extracted from the Shops cell
    "shops_raw",       # raw text of the Shops cell — kept for audit/debug
]

# Selectors that indicate we're already authenticated and inside the dashboard.
# Used by login_and_prepare to short-circuit when an earlier session's cookies
# cause /auth/login to 302 us straight into the app.
_DASHBOARD_HINT_SELECTORS = (
    "tr.ant-table-row",
    ".ant-table-tbody tr.ant-table-row",
    ".ant-table-body table tbody tr",
    ".inte-table tbody tr",
)


def _app_id_variants(app_id: str):
    """Return reasonable display-text candidates for an internal app id."""
    raw = app_id.strip()
    spaced = raw.replace("_", " ")
    return [
        raw,
        spaced,
        spaced.title(),
        spaced.upper(),
        spaced.replace(" Eu", " EU").title(),  # EU casing
    ]


def _dump_dom(page, reason: str, filename: str = DEBUG_DOM_FILE):
    """Auto-debug helper: write the current page HTML to a debug file."""
    try:
        html = page.content()
        Path(filename).write_text(html, encoding="utf-8")
        logging.info(
            f"🪵 DOM dumped to '{filename}' ({len(html)} chars) — reason: {reason}"
        )
    except Exception as dump_err:
        logging.error(f"Could not dump DOM: {dump_err}")


def _screenshot(page, filename: str):
    """Best-effort full-page screenshot; never raises."""
    try:
        page.screenshot(path=filename, full_page=True)
        logging.info(f"📸 Screenshot saved: '{filename}'")
    except Exception as err:
        logging.error(f"Could not save screenshot '{filename}': {err}")


def _is_on_dashboard(page) -> bool:
    """Heuristic: is the page already inside an authenticated dashboard?"""
    try:
        if "login" in (page.url or "").lower():
            return False
    except Exception:
        pass
    for sel in _DASHBOARD_HINT_SELECTORS:
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


def login_and_prepare(page, app_id, username=None, password=None):
    """
    Log in to cHAP. The login form does NOT use Ant Design — it uses
    CedCommerce's custom "inte-*" component library. Relevant shape:

      <div class="inte-formElement--Wrap ... inte-select--thick" aria-expanded="false">
        <h3>Select the Integration Apps</h3>
        <div class="inte-formElemet--Inner">
          <div class="inte-formElement inte-select">
            <span class="inte__Select--Selected">
              <div class="inte-select-placeholder">Select</div>   <!-- trigger -->
            </span>
            <div class="inte-select inte-select--Fake"
                 style="visibility: hidden; opacity: 0">          <!-- popup -->
              <ul class="inte-select-options">
                <li class="inte-Select__Select--Item" value="shopify_temu">…</li>
                …
              </ul>
            </div>
          </div>
        </div>
      </div>

    Crucially, option `value` attributes are exactly the snake-case ids
    from `.env` — no fuzzy matching required.
    """
    # Resolve per-app credentials strictly. The previous version fell
    # back to the legacy USERNAME/PASSWORD module-level values (which
    # are just APP_1's creds) when a specific app's creds were missing
    # — that silently submitted the wrong credentials for every app
    # other than APP_1, making the failure look like "cHAP rejected our
    # login" instead of "we don't have creds for this app". Fail loudly
    # with an actionable message instead.
    if username is None or password is None:
        lookup = CREDENTIALS.get(app_id)
        if lookup:
            username = username or lookup[0]
            password = password or lookup[1]
    if not username or not password:
        # Locate the app in the registry so we can tell the user the
        # exact APP_N_USER/APP_N_PASS keys to set.
        import app_registry
        app = app_registry.get(app_id)
        ref = (app.creds_ref if app else "") or "APP_?"
        raise RuntimeError(
            f"No credentials configured for app_id={app_id!r}. Set "
            f"{ref}_USER and {ref}_PASS in .env (local) or as GitHub "
            f"Actions secrets (CI). The legacy USERNAME/PASSWORD fallback "
            f"was intentionally removed — it masked missing-creds bugs "
            f"by logging in as the wrong account."
        )

    logging.info(f"🚀 Navigating to {LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    logging.info(f"   ↳ landed at: {page.url}")

    try:
        # 1. Wait for the form to render — but cap the wait short so we can
        #    detect the "already-authenticated" case quickly. If the server
        #    302'd us to a dashboard because of stale cookies/storage, the
        #    username input will never appear; we'd rather notice and skip.
        #
        # Cold-start flake: on first-hit the cifapps.com frontend can take
        # >20s to hydrate, or occasionally never renders at all (JS bundle
        # hiccup). Retry with a page reload up to 2 times before giving up
        # so a single bad cold start doesn't lose the whole app. Verified
        # 2026-04-18: shopify_temu failed on first run with 6s+14s both
        # timing out; a reload would have recovered it.
        login_rendered = False
        last_err = None
        for attempt in range(3):
            try:
                try:
                    page.wait_for_selector(
                        "input[placeholder='Enter Username']", timeout=6000
                    )
                    logging.info("✅ Login form rendered.")
                except PwTimeout:
                    if _is_on_dashboard(page):
                        logging.warning(
                            f"⚠️  No login form at {page.url}; already "
                            "authenticated. Skipping login steps."
                        )
                        return
                    # Not on a dashboard and still no form — give it a bit
                    # more time, some first-hits of the page render slowly.
                    logging.info(
                        "   ↳ form not yet rendered; waiting another 14s…"
                    )
                    page.wait_for_selector(
                        "input[placeholder='Enter Username']", timeout=14000
                    )
                    logging.info("✅ Login form rendered (late).")
                login_rendered = True
                break
            except PwTimeout as pw_err:
                last_err = pw_err
                if attempt < 2:
                    logging.warning(
                        f"   ↳ login form never rendered (attempt {attempt+1}/3); "
                        "reloading page and retrying…"
                    )
                    try:
                        page.goto(LOGIN_URL, wait_until="domcontentloaded")
                    except Exception as nav_err:
                        logging.warning(
                            f"   ↳ reload navigation failed ({nav_err}); "
                            "trying page.reload() instead"
                        )
                        try:
                            page.reload(wait_until="domcontentloaded")
                        except Exception:
                            pass
                    page.wait_for_timeout(500)
                    continue
        if not login_rendered:
            raise last_err or PwTimeout("Login form never rendered.")

        # 2. Locate the component root for the Integration Apps dropdown
        #    by anchoring on its label. This scopes us to exactly one
        #    select widget, regardless of how many appear elsewhere.
        logging.info(f"🖱️  Opening Integration Apps dropdown (target: {app_id})")
        component = page.locator(
            "div.inte-formElement--Wrap:has(h3:has-text('Select the Integration Apps'))"
        ).first

        # The clickable target is the .inte-formElement.inte-select inside it.
        trigger = component.locator(".inte-formElement.inte-select").first

        clicked = False
        try:
            trigger.click(force=True, timeout=10000)
            clicked = True
            logging.info("✅ Clicked dropdown trigger.")
        except Exception as err:
            logging.warning(f"Trigger click failed: {err}; trying placeholder span.")
            try:
                component.locator(".inte__Select--Selected").first.click(
                    force=True, timeout=10000
                )
                clicked = True
                logging.info("✅ Clicked dropdown via selected-span fallback.")
            except Exception as err2:
                _dump_dom(page, f"dropdown click failed: {err2}")
                raise

        if not clicked:
            _dump_dom(page, "dropdown never clicked")
            raise RuntimeError("Unable to open the Integration Apps dropdown.")

        # 3. Wait for the popup to become visible. The component flips
        #    `aria-expanded` on the wrapper and the inner panel's inline
        #    `visibility: hidden; opacity: 0` to visible. Wait for either:
        #    the expanded attr, or the first option becoming visible.
        try:
            page.wait_for_selector(
                "li.inte-Select__Select--Item",
                timeout=10000,
                state="visible",
            )
        except PwTimeout:
            _dump_dom(page, "options panel never appeared after dropdown click")
            raise
        page.wait_for_timeout(300)  # let the popup animation settle

        # 4. Pick the option by EXACT value attribute — the values are the
        #    raw snake-case ids (shopify_temu, shein, shopify_temu_eu).
        option = page.locator(f"li.inte-Select__Select--Item[value='{app_id}']").first
        if option.count() == 0:
            # Surface all available values for diagnostics.
            all_opts = page.locator("li.inte-Select__Select--Item")
            values = [
                all_opts.nth(i).get_attribute("value") or all_opts.nth(i).inner_text().strip()
                for i in range(all_opts.count())
            ]
            _dump_dom(page, f"no option with value='{app_id}' (available: {values})")
            raise RuntimeError(
                f"Could not find dropdown option for '{app_id}'. "
                f"Available values were: {values}"
            )
        try:
            option.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        option.click(force=True)
        logging.info(f"✅ Selected app option '{app_id}'")

        # 5. Fill credentials (per-app).
        logging.info(f"⌨️  Filling credentials for user '{username}'...")
        page.fill("input[placeholder='Enter Username']", username)
        page.fill("input[placeholder='Enter Password']", password)

        # 6. Submit. The button is disabled until all three fields are
        #    valid; wait for it to become enabled before clicking.
        logging.info("🔘 Clicking Login...")
        login_btn = page.locator("button:has-text('Login'):not([disabled])").first
        try:
            login_btn.wait_for(state="visible", timeout=10000)
        except PwTimeout:
            # Fallback: click whatever Login button exists (force).
            page.click("button:has-text('Login')", force=True)
        else:
            login_btn.click()

        # 7. Wait for dashboard to load. Check for login rejection first —
        #    if a toast/error banner appears OR we remain on /auth/login
        #    after a few seconds, raise a clear error instead of waiting
        #    30s for a table that'll never arrive.
        try:
            page.wait_for_url(lambda url: "login" not in url, timeout=15000)
        except PwTimeout:
            # Still on the login URL → almost certainly bad credentials or
            # a rejected app selection. Surface a useful message.
            toast_text = ""
            for sel in [".inte-toast--Wrapper", ".ant-message", ".ant-notification", ".inte__error"]:
                try:
                    t = page.locator(sel).inner_text(timeout=500).strip()
                    if t:
                        toast_text = t
                        break
                except Exception:
                    continue
            raise RuntimeError(
                f"Login was not accepted for app '{app_id}'. Still on login URL. "
                f"Toast/banner: {toast_text or '(none detected)'}"
            )

        # Use the same row locator the scraper uses — `tr.ant-table-row`.
        # The generic "table tbody tr" was matching header tables / measure
        # rows inconsistently and timing out even when the dashboard had
        # fully rendered (confirmed from debug_dom dumps).
        page.wait_for_selector(
            "tr.ant-table-row",
            timeout=30000,
        )
        logging.info(f"🎉 Inside Dashboard! URL: {page.url}")

        # Some apps show TWO filter dropdowns at the top of the seller
        # page — a framework (shopify/prestashop/woocommerce/etc.) AND
        # a target app. The framework dropdown defaults to a single
        # value (e.g. "shopify"), so the seller list only contains
        # rows from that one framework. Flip it to "all" so cross-
        # framework sellers (Prestashop + Shopify on the same app)
        # aren't silently dropped.
        try:
            _ensure_framework_filter_is_all(page, app_id)
        except Exception as err:
            # Non-fatal — many apps don't have this dropdown (only one
            # supported framework), and the scrape should still proceed
            # with whatever rows the default filter shows.
            logging.debug(
                f"framework-to-all skipped for {app_id}: {err}"
            )

    except Exception as e:
        shot = f"error_login_{app_id}.png"
        dom = f"debug_dom_login_{app_id}.txt"
        _screenshot(page, shot)
        _screenshot(page, ERROR_SHOT_FILE)  # keep legacy path too
        _dump_dom(page, f"login failure for {app_id}: {e}", filename=dom)
        _dump_dom(page, f"login failure for {app_id}: {e}")  # legacy path
        logging.error(
            f"❌ Failed at login step for '{app_id}' (URL: {page.url}). "
            f"Screenshot: '{shot}' / DOM: '{dom}'. Error: {e}"
        )
        raise


def _ensure_framework_filter_is_all(page, app_id: str) -> None:
    """Flip the top-of-dashboard framework dropdown to 'all' so the
    seller list isn't silently restricted to one integration type.

    Some cHAP apps (e.g. TEMU EU, Mirakl variants) support multiple
    source frameworks — Shopify, PrestaShop, WooCommerce, etc. The
    admin-panel dashboard shows TWO dropdowns at the top of the seller
    list: framework (shopify/prestashop/…/all) and target app
    (temu/shein/…). The framework one defaults to a single value, so
    if we leave it, we miss every seller on other frameworks.

    Strategy: find the leftmost inte-Select header with a value that
    is NOT already 'all'. Click to open. Pick the 'all' option. Wait
    for the table to re-render. If no such dropdown exists (single-
    framework app), no-op.

    Kept tolerant — single PwTimeout doesn't raise. We log and
    continue.
    """
    from playwright.sync_api import TimeoutError as PwTimeout

    # Candidates: every inte-Select header inside the main content
    # (NOT the sidebar nav). The framework dropdown sits above the
    # sellers table.
    headers = page.locator("main .inte-Select__Select--Header")
    count = headers.count()
    if count == 0:
        return

    target_header = None
    for i in range(count):
        h = headers.nth(i)
        try:
            text = (h.inner_text() or "").strip().lower()
        except Exception:
            continue
        # If this header's visible value is already "all", nothing to do.
        if text == "all":
            target_header = None
            break
        # A single-word value that doesn't equal "all" is almost certainly
        # the framework filter (shopify / prestashop / woocommerce / …).
        # The target-app dropdown reads like "shein" / "temu" which are
        # app identifiers, not framework names — those stay untouched.
        FRAMEWORK_VALUES = {
            "shopify", "prestashop", "woocommerce", "magento",
            "bigcommerce", "wix", "squarespace",
        }
        if text in FRAMEWORK_VALUES:
            target_header = h
            break

    if target_header is None:
        return  # already 'all', or no framework filter present

    logging.info(
        f"🧭 Flipping framework filter to 'all' for {app_id} "
        f"(was '{text}') so cross-framework sellers aren't dropped."
    )
    try:
        target_header.scroll_into_view_if_needed()
        target_header.click()
    except Exception as err:
        logging.debug(f"framework header click failed: {err}")
        return

    # The dropdown popup should surface an option with value/text 'all'.
    try:
        page.wait_for_selector(
            "li.inte-Select__Select--Item",
            timeout=6000,
            state="visible",
        )
    except PwTimeout:
        logging.debug("framework options panel didn't open")
        return

    # Prefer value="all"; fall back to the option whose visible text is 'all'.
    all_option = page.locator("li.inte-Select__Select--Item[value='all']").first
    if all_option.count() == 0:
        # Hunt by visible text instead.
        all_option = page.locator(
            "li.inte-Select__Select--Item:has-text('all')"
        ).first
    if all_option.count() == 0:
        logging.debug("no 'all' option in framework dropdown")
        return

    try:
        all_option.click()
    except Exception as err:
        logging.debug(f"selecting 'all' failed: {err}")
        return

    # Give the seller table a beat to re-query. The row set may shrink
    # OR grow depending on the framework mix — both are fine.
    page.wait_for_timeout(800)
    try:
        page.wait_for_selector("tr.ant-table-row", timeout=10000)
    except PwTimeout:
        # Occasionally 'all' produces zero rows (app with only one
        # framework that's now deselected). Not fatal; scraper's
        # empty-table path handles it.
        pass
    logging.info(f"   ↳ framework filter now 'all' for {app_id}.")


# ---------------------------------------------------------------------------
# Seller extraction
# ---------------------------------------------------------------------------

# Header-text → canonical field mapping. Column *order* differs per app —
# e.g. shein has a "App Type" column that shopify_temu doesn't — so we
# resolve the CELL_INDEX at runtime by reading the table's <th> row.
HEADER_ALIASES = {
    "seller_id":      ("seller id",),
    "store_url":      ("store url",),
    "username":       ("username", "user name"),
    "email":          ("email id", "email"),
    "platforms":      ("marketplace/platform connected", "marketplace / platform connected",
                       "platforms", "platform connected"),
    "installed_on":   ("installed on", "install date"),
    "app_type":       ("app type",),
    "source_country": ("source country", "country"),
    # Optional columns revealed only after clicking "Customize Grid".
    # Verified 2026-04-18 from the shopify_temu DOM dump — the widget
    # exposes 11 toggleable columns. Labels below are exact matches for
    # what's rendered; aliases stay loose in case copy shifts across apps.
    "order_count":        ("order count", "orders", "order sync", "orders count"),
    "product_count":      ("product count", "products", "product sync", "products count"),
    "failed_order_count": ("failed orders", "failed order count", "failed order"),
    "steps_completed":    ("steps completed", "onboarding steps", "setup steps"),
    "plan":               ("plan details", "plan", "plan name", "subscription"),
    "last_sync":          ("last sync", "last sync at", "last synced"),
    "webhooks":           ("webhooks",),
    "action":         ("action", "actions"),
}

# Same shape as HEADER_ALIASES but tuned for the Uninstalls table. Left-nav
# click takes us to the uninstalls view which reuses AntD (`.ant-table-row`)
# plus the same inte-* pagination — only the columns differ.
#
# Verified 2026-04-18: the live uninstalls table shows just four columns,
# `User Id | Email | Shops | Username`. The `Shops` cell is nested — each
# row may contain multiple `(platform, YYYY-MM-DD, HH:MM:SS)` tuples, one
# per uninstalled platform. Aliases below cover minor label variants we
# might run into on other app builds (`userid`, plain `user`, etc.).
UNINSTALL_HEADER_ALIASES = {
    "seller_id": ("user id", "seller id", "userid", "user_id"),
    "email":     ("email id", "email"),
    "shops":     ("shops", "shop"),
    "username":  ("username", "user name"),
}


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def build_header_index(page, aliases: dict = HEADER_ALIASES) -> dict:
    """
    Read the AntD header row and return {canonical_field: column_index}.

    AntD uses two separate <table> elements for fixed headers — the header
    table has class `ant-table-thead`. We look up headers inside that, then
    map each header text to a canonical field via `aliases`.

    **Scoping matters.** An uninstalls run surfaced a case where a date-range
    filter above the data table also rendered an `.ant-table-thead` (AntD
    date-pickers use one internally). The page-level selector picked up the
    filter's two empty `<th>` instead of the real data-table headers. To
    avoid that, we first try to find the `.ant-table-wrapper` that contains
    our data rows and scope the thead lookup to it; only fall back to the
    whole page if no wrapper matches.

    The `aliases` dict is parameterized so the same helper serves the Seller
    List table (HEADER_ALIASES) AND the Uninstalls table
    (UNINSTALL_HEADER_ALIASES), which share the AntD shell but have
    different column sets.
    """
    # Prefer the wrapper that actually contains data rows — this is the
    # main results table, not an ant-table used internally by a filter,
    # date-picker, or empty-state card.
    scoped_wrapper = page.locator(
        ".ant-table-wrapper:has(tr.ant-table-row)"
    ).first
    if scoped_wrapper.count() > 0:
        ths = scoped_wrapper.locator(".ant-table-thead th")
    else:
        # No real data rows on screen yet (e.g. empty-state view). Fall back
        # to page scope so we still log whatever <th> text is present.
        ths = page.locator(".ant-table-thead th")

    count = ths.count()
    raw_headers = []
    for i in range(count):
        try:
            raw_headers.append(ths.nth(i).inner_text())
        except Exception:
            raw_headers.append("")
    normalized = [_normalize(h) for h in raw_headers]

    index = {}
    for field, alias_list in aliases.items():
        for i, hdr in enumerate(normalized):
            if any(a in hdr for a in alias_list):
                index[field] = i
                break

    logging.info(
        f"   ↳ header map (from {count} <th>): {index} — raw: {raw_headers}"
    )
    return index


def extract_row_data(row, header_index: dict, aliases: dict = HEADER_ALIASES):
    """
    Extract one table row into a dict keyed by the canonical field names
    defined in `aliases`. Missing cells come back as "" so downstream code
    can always index without KeyError.

    Generalization of the old extract_seller_data — the same function now
    serves both the Seller List and the Uninstalls table. Two small
    specializations are preserved regardless of alias set:
      - `store_url` is right-trimmed of trailing slashes (URL normalization).
      - `platforms` falls back to <img alt/title> joined by commas when the
        cell renders platform icons rather than text.
      - `seller_id` prefers the row's `data-row-key` (stable, full-length)
        over the visible cell (which truncates on long hex IDs).
    """
    cells = row.locator("td")
    n = cells.count()
    if n < 2:
        return None

    def cell_at(i):
        if i is None or i < 0 or i >= n:
            return ""
        # inner_text collapses multi-line cells (like stacked "Shopify/Temu")
        return re.sub(r"\s+", " ", cells.nth(i).inner_text()).strip()

    result = {}
    for field in aliases.keys():
        result[field] = cell_at(header_index.get(field))

    # Platforms: fall back to <img alt/title> joined by commas when the
    # cell renders platform icons rather than text.
    if "platforms" in result and not result["platforms"]:
        try:
            p_idx = header_index.get("platforms")
            if p_idx is not None and p_idx < n:
                imgs = cells.nth(p_idx).locator("img")
                result["platforms"] = ",".join(
                    (imgs.nth(i).get_attribute("alt") or imgs.nth(i).get_attribute("title") or "").strip()
                    for i in range(imgs.count())
                )
        except Exception:
            pass

    # store_url normalization — drop trailing slash so diffs against
    # Supabase stay stable.
    if "store_url" in result and result["store_url"]:
        result["store_url"] = result["store_url"].rstrip("/")

    # Prefer the authoritative seller id from `data-row-key` over the
    # visible cell (the cell sometimes truncates long hex IDs). Works for
    # both the Seller List (row-key == seller id) and the Uninstalls table
    # (row-key may be the uninstall log id; still stable for de-dup).
    row_key = row.get_attribute("data-row-key") or ""
    if "seller_id" in result:
        result["seller_id"] = result["seller_id"] or row_key
    elif row_key:
        result["_row_key"] = row_key

    # --- Defensive column-shift guard ------------------------------------
    # On 2026-04-18 we shipped a bug where ~150 shein rows had the header
    # map built at page 1 re-used against pages that rendered a different
    # column count; rows came back with `platforms='View'`, dates in
    # `order_count`, etc. The per-page header rebuild addresses the root
    # cause, but if a page's thead reflows mid-extraction (or ships with
    # a subtly different label we haven't aliased), we want to REJECT
    # rather than silently emit garbage.
    #
    # Heuristics:
    #   - `platforms` should contain a real platform name. If it equals
    #     'View' (the webhooks-column render text) or looks like a date
    #     (DD/MM/YYYY, YYYY-MM-DD) the map is clearly shifted.
    #   - `installed_on` should look date-shaped when non-empty. If we
    #     see "Shopify" / "Custom App" / "United States" there, the map
    #     has drifted.
    platforms = result.get("platforms") or ""
    installed_on = result.get("installed_on") or ""
    _looks_like_date = re.match(r"^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}$", platforms) or \
                        re.match(r"^\d{4}-\d{2}-\d{2}", platforms)
    _installed_is_text = installed_on and not re.search(r"\d", installed_on)
    if platforms.strip().lower() == "view" or _looks_like_date or _installed_is_text:
        logging.warning(
            f"   ↳ row rejected as column-shifted: row_key={row_key} "
            f"platforms={platforms!r} installed_on={installed_on!r}"
        )
        return None

    return result


# Keep the old name as a thin alias so any external callers / tests that
# imported `extract_seller_data` continue to work.
def extract_seller_data(row, header_index: dict):
    return extract_row_data(row, header_index, HEADER_ALIASES)


# Regex for tuples inside the Uninstalls `Shops` cell. Live text looks like
# "Shopify 2026-02-18 20:13:03 Shein 2026-02-18 20:14:07" — i.e. platform
# name, ISO date, HH:MM:SS, possibly repeated several times in one cell.
# We match a (non-whitespace) platform name followed by date + time, which
# tolerates extra whitespace / newlines between the tuple entries.
_UNINSTALL_SHOPS_TUPLE = re.compile(
    r"([A-Za-z][A-Za-z0-9_ \-\+\/]*?)\s+"
    r"(\d{4}-\d{2}-\d{2})\s+"
    r"(\d{2}:\d{2}:\d{2})"
)


def _parse_shops_cell(shops_text: str) -> list[tuple[str, str]]:
    """
    Parse the nested `Shops` cell text into a list of (platform, uninstalled_on)
    tuples.

    Input looks like: `"Shopify 2026-02-18 20:13:03 Shein 2026-02-18 20:14:07"`
    (one DOM cell can list multiple uninstalled platforms). Returns
    `[("Shopify", "2026-02-18 20:13:03"), ("Shein", "2026-02-18 20:14:07")]`.

    If the regex finds nothing (e.g. the cell renders differently for a
    particular app build), returns an empty list; the caller should fall
    back to emitting a single row with the raw text preserved so nothing
    gets silently dropped.
    """
    if not shops_text:
        return []
    out: list[tuple[str, str]] = []
    for m in _UNINSTALL_SHOPS_TUPLE.finditer(shops_text):
        platform = m.group(1).strip()
        # Strip any trailing join words picked up by the greedy platform
        # match (shouldn't happen with the lazy quantifier, but belt+braces).
        platform = re.sub(r"\s+$", "", platform)
        iso = f"{m.group(2)} {m.group(3)}"
        out.append((platform, iso))
    return out


def extract_uninstall_row(row, header_index: dict, aliases: dict = UNINSTALL_HEADER_ALIASES):
    """
    Uninstalls-specific extractor: returns a LIST of flat dicts, one per
    (platform, uninstalled_on) tuple found in the row's `Shops` cell.

    Signature matches `extract_row_data` but returns a list (possibly
    empty, possibly length-N) instead of a single dict, so the paginator
    must handle a list-returning extractor via `extractor=...`.

    Guarantees every dict has the UNINSTALL_CSV_COLUMNS keys populated; if
    the regex misses entirely, falls back to a single row with `platform`
    and `uninstalled_on` blank but `shops_raw` preserved — better to keep
    the user record than silently drop it.
    """
    cells = row.locator("td")
    n = cells.count()
    if n < 1:
        return []

    def cell_at(i):
        if i is None or i < 0 or i >= n:
            return ""
        return re.sub(r"\s+", " ", cells.nth(i).inner_text()).strip()

    raw_shops_idx = header_index.get("shops")
    # Prefer the full innerText of the Shops cell over the whitespace-collapsed
    # version for regex parsing — but both should work since our regex is
    # whitespace-tolerant.
    shops_raw = cell_at(raw_shops_idx)

    row_key = row.get_attribute("data-row-key") or ""
    seller_id = cell_at(header_index.get("seller_id")) or row_key
    email = cell_at(header_index.get("email"))
    username = cell_at(header_index.get("username"))

    tuples = _parse_shops_cell(shops_raw)
    if not tuples:
        # Keep the row; just flag that we couldn't decompose the shops cell.
        return [{
            "seller_id": seller_id,
            "email": email,
            "username": username,
            "platform": "",
            "uninstalled_on": "",
            "shops_raw": shops_raw,
        }]

    out = []
    for platform, uninstalled_on in tuples:
        out.append({
            "seller_id": seller_id,
            "email": email,
            "username": username,
            "platform": platform,
            "uninstalled_on": uninstalled_on,
            "shops_raw": shops_raw,
        })
    return out


def _scrape_paginated_ant_table(
    page,
    label: str,
    aliases: dict = HEADER_ALIASES,
    max_pages: int | None = None,
    extractor=None,
    dedup_key=None,
    trace_sink: list | None = None,
) -> list:
    """
    Walk an AntD table + inte-* pagination and return every row as a dict.

    Powers both `scrape_seller_table` (Seller List view) and
    `scrape_uninstalls_table` (Uninstalls view) — the shells are identical
    AntD + inte-* pagination; only the columns differ, which is handled
    via the `aliases` parameter.

    Args:
        page: active Playwright page, already on the table view.
        label: log prefix, e.g. "shopify_temu" or "shein uninstalls" —
            also used in diagnostic artifact filenames so seller-page and
            uninstalls-page failures don't clobber each other.
        aliases: header → field alias dict (HEADER_ALIASES or
            UNINSTALL_HEADER_ALIASES).
        max_pages: hard upper bound if set (useful for smoke tests); None
            means iterate until Next is disabled/missing.
        extractor: optional callable `(row_locator, header_index, aliases)
            -> dict | list[dict] | None`. Defaults to `extract_row_data`
            (returns a single dict). Uninstalls passes in
            `extract_uninstall_row` which returns a LIST per row because
            one DOM row can expand into multiple (platform, timestamp)
            uninstall events.
        dedup_key: optional callable `(row_dict) -> str` for de-dup across
            pages. Defaults to seller_id/row_key based. Uninstalls uses
            `seller_id|platform|uninstalled_on` so multiple platform
            uninstalls per user don't collapse into one.
    """
    rows_out = []
    page_num = 1
    seen_ids = set()     # guard against duplicate pages on slow renders
    header_index = None  # built lazily on first successful page
    # Per-page trace for scrape_validator.check_pagination. Only populated
    # when caller supplies a sink list. Each entry is a dict; scraper.main()
    # converts them to PageTrace dataclass instances before validating.
    total_rows_reported: int | None = None
    total_pages_reported: int | None = None

    # Default extractor returns a single dict per row; callers can supply
    # one that returns a list (uninstalls expand one row → many events).
    if extractor is None:
        extractor = extract_row_data

    # Slugify the label so artifact filenames stay filesystem-safe.
    safe_label = re.sub(r"[^A-Za-z0-9_]+", "_", label).strip("_") or "table"

    logging.info(f"📊 scraping table '{label}'; URL: {page.url}")

    while True:
        logging.info(f"📄 Scraping {label} — Page {page_num}")
        # Wait for REAL data rows (ant-table-row), an empty-state card,
        # or the inte-* empty placeholder — whichever comes first. We must
        # handle the empty case explicitly because brand-new apps can have
        # ZERO uninstalls, in which case the table never renders rows.
        try:
            page.wait_for_selector(
                "tr.ant-table-row, .ant-empty, .inte-emptyState, "
                ".ant-table-tbody tr.ant-table-row, .inte-table tbody tr",
                timeout=30000,
            )
        except PwTimeout as wait_err:
            shot = f"error_scrape_{safe_label}_p{page_num}.png"
            dom = f"debug_dom_scrape_{safe_label}_p{page_num}.txt"
            _screenshot(page, shot)
            _dump_dom(
                page,
                f"scrape wait timeout for {label} page {page_num}: {wait_err}",
                filename=dom,
            )
            logging.error(
                f"❌ Table rows never appeared for {label} p{page_num} "
                f"(URL: {page.url}). Screenshot: '{shot}' / DOM: '{dom}'."
            )
            raise

        # Explicit empty-state check before we even count rows.
        #
        # Pitfall we hit on 2026-04-18: a top-of-page date-range filter
        # *also* renders an `.ant-empty` card in its default state, which
        # made the page-level selector flag the uninstalls table as empty
        # even when real rows existed. The fix is two-fold:
        #  1. Only trust an empty-state that lives INSIDE a wrapper whose
        #     .ant-table-placeholder / .ant-empty belongs to the MAIN data
        #     table — approximated by requiring a placeholder <tr> (which
        #     date-pickers don't render) over a loose .ant-empty match.
        #  2. Hedge significantly longer (3.5s total) before accepting a
        #     zero — the uninstalls API is noticeably slower than the
        #     seller-list one.
        real_rows = page.locator(
            "tr.ant-table-row:not(.ant-table-placeholder)"
        )
        # Only the data table renders a `<tr class="ant-table-placeholder">`
        # as the empty-state row. Date-pickers render `.ant-empty` but NOT
        # this row shape, so this selector is a much cleaner signal.
        placeholder_row_visible = (
            page.locator("tr.ant-table-placeholder").count() > 0
        )
        if real_rows.count() == 0 and placeholder_row_visible:
            logging.info(
                f"   ↳ empty-state row detected; re-checking after 3.5s hedge..."
            )
            page.wait_for_timeout(3500)
            real_rows = page.locator(
                "tr.ant-table-row:not(.ant-table-placeholder)"
            )
            still_placeholder = (
                page.locator("tr.ant-table-placeholder").count() > 0
            )
            if real_rows.count() == 0 and still_placeholder:
                logging.info(
                    f"   ↳ empty-state confirmed — 0 real rows for {label}."
                )
                break
            logging.info(
                f"   ↳ empty-state was transient; "
                f"{real_rows.count()} rows appeared after wait."
            )

        # Scope to real data rows, falling back to broader selectors only if
        # nothing matches. The `:not(.ant-table-placeholder)` guard keeps us
        # from counting AntD's empty-state row as "1 data row".
        rows = real_rows if real_rows.count() > 0 else page.locator("tr.ant-table-row")
        if rows.count() == 0:
            rows = page.locator(".ant-table-body table tbody tr:not(.ant-table-measure-row):not(.ant-table-placeholder)")
        if rows.count() == 0:
            rows = page.locator(".inte-table tbody tr")

        # Empty-state short-circuit — we only break here if the
        # placeholder <tr> is genuinely in the data table's tbody. The
        # loose `.ant-empty / .inte-emptyState` check we used to do was
        # unreliable because the uninstalls page's date-range filter
        # renders `.ant-empty` in its default closed state (see above).
        if rows.count() == 0:
            if page.locator("tr.ant-table-placeholder").count() > 0:
                logging.info(
                    f"   ↳ data-table placeholder row visible — 0 rows for {label}."
                )
            else:
                logging.warning(
                    f"   ↳ no rows matched any selector (and no placeholder <tr>) — "
                    f"aborting loop for {label}."
                )
            break
        logging.info(f"   ↳ {rows.count()} data rows visible on page {page_num}")

        # Build header index PER PAGE (not once per run).
        #
        # Why per page: verified 2026-04-18 on shein — during pagination
        # retry clicks the table occasionally re-rendered with FEWER
        # columns (5 Customize Grid columns were absent on pages 11-16
        # etc.). A run-wide header map built at page 1 (16 columns) then
        # extracted wrong cells from those shorter pages, silently
        # producing 149 rows with `platforms='View'`, dates in
        # `order_count`, etc. Per-page rebuild ensures each row's
        # extraction uses the column layout actually rendered on its own
        # page. Rebuild cost is ~50ms/page — negligible vs. the data
        # integrity we gain.
        prev_header_index = header_index
        header_index = build_header_index(page, aliases=aliases)
        if (
            prev_header_index is not None
            and header_index
            and set(header_index) != set(prev_header_index)
        ):
            logging.warning(
                f"   ↳ header map CHANGED between pages on {label} "
                f"(page {page_num}). Previous keys={sorted(prev_header_index)} "
                f"→ now={sorted(header_index)}. "
                "Extraction will use the new map — double-check Customize "
                "Grid state on the dashboard for this app."
            )
            # Re-dump DOM so the regression is easy to reproduce offline.
            try:
                _dump_dom(
                    page,
                    f"header map changed mid-scrape on {label} page {page_num}",
                    filename=f"debug_dom_headerchange_{safe_label}_p{page_num}.txt",
                )
            except Exception:
                pass
        if prev_header_index is None:
            # Diagnostic: if the header map came back empty despite having
            # data rows on screen, our thead-scoping is almost certainly
            # picking up the wrong table (e.g. a filter's internal
            # `ant-table-thead`). Dump the page once so we can inspect.
            if not header_index and rows.count() > 0:
                dump_path = f"debug_dom_headerfail_{safe_label}.txt"
                _dump_dom(
                    page,
                    f"empty header map on {label} page {page_num} "
                    f"despite {rows.count()} visible rows",
                    filename=dump_path,
                )
                _screenshot(page, f"error_headerfail_{safe_label}.png")
                # Also capture the first row's HTML so we can see what
                # columns are actually present.
                try:
                    first_row_html = rows.nth(0).evaluate("el => el.outerHTML")
                    Path(f"debug_first_row_{safe_label}.html").write_text(
                        first_row_html or "", encoding="utf-8"
                    )
                    logging.info(
                        f"   ↳ first-row HTML dumped to "
                        f"'debug_first_row_{safe_label}.html' "
                        f"({len(first_row_html or '')} chars)"
                    )
                except Exception as row_dump_err:
                    logging.warning(f"   ↳ could not dump first row HTML: {row_dump_err}")

        new_on_page = 0
        for i in range(rows.count()):
            data = extractor(rows.nth(i), header_index, aliases=aliases)
            if not data:
                continue
            # Normalize to a list so single-dict and list-returning
            # extractors share the same de-dup path. Uninstalls expand one
            # DOM row into multiple (platform, timestamp) dicts.
            if isinstance(data, dict):
                items = [data]
            else:
                items = list(data)

            for idx_in_row, item in enumerate(items):
                if dedup_key is not None:
                    uid = dedup_key(item)
                else:
                    uid = (
                        item.get("seller_id")
                        or item.get("_row_key")
                        or f"{page_num}:{i}:{idx_in_row}"
                    )
                if uid in seen_ids:
                    continue
                seen_ids.add(uid)
                item.pop("_row_key", None)
                rows_out.append(item)
                new_on_page += 1

        logging.info(f"   ↳ captured {new_on_page} new rows (total {len(rows_out)})")

        # --- Trace sink for the post-run validator ---------------------
        # Record observations about this page BEFORE we try to paginate.
        # The validator uses these to check: (a) page numbers increment
        # by 1, (b) first-row keys are unique across pages, (c) scraped
        # total matches reported total within tolerance, (d) no empty
        # intermediate pages.
        if trace_sink is not None:
            try:
                first_row_key_this = ""
                if rows.count() > 0:
                    first_row_key_this = rows.nth(0).get_attribute("data-row-key") or ""
            except Exception:
                first_row_key_this = ""
            trace_sink.append({
                "page_num": page_num,
                "first_row_key": first_row_key_this,
                "row_count": rows.count(),
                "reported_total_rows": total_rows_reported,
                "reported_total_pages": total_pages_reported,
            })

        if max_pages is not None and page_num >= max_pages:
            break

        # --- Pagination (custom `inte-*` library, not AntD) ---
        # Layout: `.inte-flex--spacing-MediumTight` row contains four
        # `.inte-flex__item` children: [prev button, page-number input,
        # "of N" text, next button]. The next button is icon-only; disabled
        # state is signalled by `disabled=""` attribute + `inte-btn-disable`.
        pag_row = page.locator(
            "div.inte-flex.inte-flex--spacing-MediumTight:has(.inte-Pagination--PageCount)"
        ).first
        if pag_row.count() == 0:
            # The pagination bar can hydrate slightly AFTER the data rows
            # (observed 2026-04-18 on shein: 20 data rows visible but
            # `inte-Pagination--PageCount` still in a loading state). This
            # ALSO happens on page 2+ during transitions — the old bar gets
            # unmounted while the new one is rendering, and after a retry
            # click the DOM churn can take 10–15 s to settle. A full
            # 20-row page always means more pages, never a short single
            # page, so at the page-size boundary we hedge up to 20 s
            # before declaring the list exhausted.
            if rows.count() >= 20:
                logging.info(
                    "   ↳ pagination bar not yet rendered; waiting up to 20s..."
                )
                try:
                    page.wait_for_selector(
                        "div.inte-flex.inte-flex--spacing-MediumTight "
                        ".inte-Pagination--PageCount",
                        timeout=30000,
                        state="visible",
                    )
                    pag_row = page.locator(
                        "div.inte-flex.inte-flex--spacing-MediumTight"
                        ":has(.inte-Pagination--PageCount)"
                    ).first
                except PwTimeout:
                    pass
            if pag_row.count() == 0:
                logging.info("   ↳ no pagination row — single-page result.")
                break

        # Parse "of N" if we haven't yet — gives us a sanity upper bound.
        if page_num == 1:
            try:
                of_text = pag_row.locator("div.inte-flex__item").nth(2).inner_text()
                m = re.search(r"of\s+(\d+)", of_text)
                if m:
                    total_pages_reported = int(m.group(1))
                    logging.info(f"   ↳ pagination reports {total_pages_reported} total pages")
            except Exception:
                pass

        # Also parse the "Showing 1 - N of TOTAL" chip (different DOM node
        # from the "of N pages" text above). The validator uses this as
        # the reference total-row count for the tolerance check.
        try:
            showing_text = page.locator(
                "div.inte-Pagination div.inte-flex__item > span"
            ).first.inner_text()
            m2 = re.search(r"of\s+(\d+)", showing_text or "")
            if m2:
                total_rows_reported = int(m2.group(1))
        except Exception:
            pass

        # Best-effort current-page indicator: the <input> in the pagination
        # bar shows the active page number. We read it to detect page skips
        # (e.g. a retry-click race landing two transitions in a row).
        # Logged-only for now — fix is in the retry logic itself.
        try:
            page_input = pag_row.locator("input").first
            if page_input.count() > 0:
                reported_page_num = int(
                    (page_input.get_attribute("value") or "").strip() or "0"
                )
                if reported_page_num and reported_page_num != page_num:
                    logging.warning(
                        f"   ↳ page-skip detected: loop is on page {page_num} "
                        f"but pagination bar reports page {reported_page_num}. "
                        "This usually means a Next click double-advanced during "
                        "a retry — see the fix in _click_next_and_wait."
                    )
        except Exception:
            pass

        # The next button is the LAST button in the pagination flex row.
        next_btn = pag_row.locator("button").last
        if next_btn.count() == 0:
            logging.info("   ↳ no next button — end of pagination.")
            break

        # Disabled check — attribute first, class as backup.
        is_disabled = next_btn.get_attribute("disabled") is not None
        if not is_disabled:
            classes = next_btn.get_attribute("class") or ""
            if "inte-btn-disable" in classes:
                is_disabled = True
        if is_disabled:
            logging.info("   ↳ Next button disabled — end of pagination.")
            break

        # Capture the first row's data-row-key so we can detect the table
        # actually re-rendered (network may look idle but the table update
        # is a React state flip, not a fresh request).
        prev_first_key = ""
        try:
            if rows.count() > 0:
                prev_first_key = rows.nth(0).get_attribute("data-row-key") or ""
        except Exception:
            pass

        # Belt-and-braces: dismiss any lingering inte-select popup before
        # clicking Next. The perPage-Sorter popup has been observed
        # sticking in `aria-expanded="true"` AFTER an option click,
        # overlaying the Next button in the same pagination footer and
        # causing silent no-op clicks. Pressing Escape is a cheap,
        # idempotent way to dismiss it — if nothing is open, Escape is a
        # no-op in the inte-select code path.
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            page.evaluate(
                """() => {
                    // Only force-close the perPage sorter popup specifically;
                    // we don't want to interfere with other inte-selects on
                    // the page (e.g. Customize Grid, if reopened).
                    const w = document.querySelector(
                        'div.inte-Pagination-perPage--Sorter '
                        + 'div.inte-formElement--Wrap'
                    );
                    if (w && w.getAttribute('aria-expanded') === 'true') {
                        w.setAttribute('aria-expanded', 'false');
                    }
                    document.querySelectorAll(
                        'div.inte-Pagination-perPage--Sorter '
                        + 'div.inte-select--Fake'
                    ).forEach(el => {
                        if (el.style.opacity !== '0') {
                            el.style.visibility = 'hidden';
                            el.style.opacity = '0';
                        }
                    });
                }"""
            )
        except Exception:
            pass

        def _click_next_and_wait(prev_key: str, timeout_ms: int) -> bool:
            """Click the Next button and wait up to `timeout_ms` for the
            first-row data-row-key to change. Returns True on success.

            Re-resolves `next_btn` fresh because inte-* pagination
            unmounts + remounts the button during page transitions —
            a stale handle click can be a silent no-op.

            Also hedges up to 15s for the pagination bar to be visible
            before clicking: during a retry window the bar may be mid-
            remount, and a too-short click timeout (5s) fires before the
            bar rehydrates, wasting the whole retry. Verified
            2026-04-18 on shein page 6→7 retry.
            """
            # Wait for the pagination bar to actually be on-screen before
            # attempting the click. 15s covers shein's worst observed
            # remount window.
            try:
                page.wait_for_selector(
                    "div.inte-flex.inte-flex--spacing-MediumTight"
                    ":has(.inte-Pagination--PageCount) button",
                    timeout=15000,
                    state="visible",
                )
            except PwTimeout:
                logging.debug(
                    "   ↳ pagination bar not visible within 15s; "
                    "attempting click anyway"
                )
            btn = page.locator(
                "div.inte-flex.inte-flex--spacing-MediumTight"
                ":has(.inte-Pagination--PageCount) button"
            ).last
            try:
                btn.click(timeout=15000)
            except Exception as ce:
                logging.warning(f"   ↳ Next click error ({ce})")
                return False
            if not prev_key:
                try:
                    page.wait_for_load_state("networkidle", timeout=timeout_ms)
                except PwTimeout:
                    pass
                return True
            try:
                page.wait_for_function(
                    """(prev) => {
                        const r = document.querySelector('tr.ant-table-row');
                        return r && r.getAttribute('data-row-key') !== prev;
                    }""",
                    arg=prev_key,
                    timeout=timeout_ms,
                )
                return True
            except PwTimeout:
                return False

        # First attempt: click Next and wait up to 25s. Shein's seller
        # endpoint has been observed taking ~11-12s/page on a good run but
        # occasionally >25s on a slow one. 25s covers the common slow case.
        refreshed = _click_next_and_wait(prev_first_key, timeout_ms=60000)
        was_retry = False

        # If the table didn't refresh, the most common causes are:
        #   (a) the click didn't land — inte-* pagination unmounts/remounts
        #       the Next button during transitions, so a stale click can
        #       be a silent no-op;
        #   (b) the backend is just slow (shein at page 6+ sometimes).
        #
        # CRITICAL: before re-clicking, we MUST check if the table already
        # flipped during the timeout window. Observed 2026-04-18 on shein:
        # first click timed out at 25s, but the backend had landed the
        # transition at ~t=26s — by the time we re-clicked, we were
        # already on page N+1, and the second click jumped us to page N+2.
        # That silently skipped 20 rows. It happened 3 times on shein and
        # cost us 60 sellers (289 captured vs 349 baseline).
        #
        # Fix: poll the DOM briefly. If the first-row key already
        # changed, accept the transition WITHOUT re-clicking. Only if it
        # genuinely hasn't changed do we trigger the retry click.
        if not refreshed and prev_first_key:
            try:
                first_row_now = page.locator("tr.ant-table-row").first
                current_key_now = (
                    first_row_now.get_attribute("data-row-key") or ""
                    if first_row_now.count() > 0
                    else ""
                )
            except Exception:
                current_key_now = ""
            if current_key_now and current_key_now != prev_first_key:
                logging.info(
                    f"   ↳ wait_for_function timed out but table DID flip "
                    f"(prev={prev_first_key[:8]}… now={current_key_now[:8]}…) — "
                    "accepting transition without re-click to avoid skipping a page."
                )
                refreshed = True
            else:
                # Grace period: give the in-flight transition another
                # 8s to surface before we declare the click a no-op
                # and re-click. This costs us at worst 8s on a slow
                # page but eliminates the double-advance race.
                try:
                    page.wait_for_function(
                        """(prev) => {
                            const r = document.querySelector('tr.ant-table-row');
                            return r && r.getAttribute('data-row-key') !== prev;
                        }""",
                        arg=prev_first_key,
                        timeout=15000,
                    )
                    logging.info(
                        "   ↳ table flipped during 8s grace period — "
                        "accepting without re-click."
                    )
                    refreshed = True
                except PwTimeout:
                    logging.warning(
                        "   ↳ table didn't refresh in 25s + 8s grace — "
                        "re-clicking Next and waiting another 30s "
                        "(click was likely a genuine no-op)."
                    )
                    refreshed = _click_next_and_wait(
                        prev_first_key, timeout_ms=60000
                    )
                    was_retry = True

        if not refreshed and prev_first_key:
            # Final check — look at DOM one more time before bailing.
            try:
                current = page.locator("tr.ant-table-row").first
                if current.count() > 0:
                    current_key = current.get_attribute("data-row-key") or ""
                    if current_key and current_key != prev_first_key:
                        refreshed = True
            except Exception:
                pass
            if not refreshed:
                logging.warning(
                    "   ↳ first row key unchanged after 2 Next clicks + "
                    "55s total — stopping pagination to avoid duplicate rows."
                )
                break

        # Settle time before next iteration. We ALREADY confirmed the table
        # re-rendered (wait_for_function returned on data-row-key flip), so
        # this is just a tiny head-start for the pagination bar to re-mount
        # before the next loop iteration inspects it. After a retry click
        # the DOM is churnier, so we budget a bit more.
        # Reduced 2026-04-18: was 1500/2500 ms, which added ~25-45 s to a
        # full shein scrape (17+ pages). The pagination-bar hedge inside
        # `_click_next_and_wait` already guards against the false-bail case
        # this sleep was originally there to paper over.
        page.wait_for_timeout(600 if was_retry else 250)
        page_num += 1

    return rows_out


def customize_grid_select_all(
    page,
    app_name: str,
    observed_labels_sink: list | None = None,
) -> bool:
    """
    Open the "Customize Grid" dropdown on the Seller List page and tick
    every available column checkbox, then close the panel so the table
    re-renders with all columns visible.

    Verified 2026-04-18 against a live DOM dump, the widget uses the same
    CedCommerce `inte-*` ChoiceList pattern as the login form's
    Integration Apps dropdown:

        <div class="inte-formElement--Wrap inte-formElement--ChoiceList ..."
             aria-expanded="false">
          <div class="inte-formElemet--Inner">
            <div class="inte-formElement inte-select inte-Select__ChoiceList">
              <h3>Customize Grid</h3>                              <!-- label  -->
              <div class="inte-formElemet__Arrow">…caret…</div>    <!-- caret  -->
              <div class="inte-select inte-select--Fixed inte-select--Fake"
                   style="visibility: hidden; opacity: 0">
                <ul class="inte-choiceList--dropdown">
                  <ul class="inte-choiceList--options">
                    <li class="inte-Select__ChoiceList--Item"
                        value="product_count">
                      <div class="inte-form__checkbox">
                        <input type="checkbox" class="inte__checkoxFake"
                               [checked=""]>
                        …
                      </div>
                    </li>
                    …
                  </ul>
                </ul>
              </div>
            </div>
          </div>
        </div>

    The popup is already in the DOM on page load — it's just hidden with
    inline `visibility: hidden; opacity: 0`, and `aria-expanded` flips to
    `"true"` on the wrapper when the trigger is clicked. This is the SAME
    mechanism as login_and_prepare, so we reuse that pattern verbatim.

    Returns True if at least one checkbox was toggled on (or all boxes
    were already ticked). Failures are NEVER fatal — the scraper must
    still run with whatever default columns are visible.
    """
    safe_label = re.sub(r"[^A-Za-z0-9_]+", "_", app_name).strip("_") or "app"
    _dump_dom(
        page,
        f"baseline DOM snapshot for {app_name} seller page (pre-customize)",
        filename=f"debug_dom_seller_pre_{safe_label}.txt",
    )

    # Wrapper-scoped locator — anchored on the h3 text so we hit exactly
    # one Customize Grid widget even if there are other inte-* ChoiceLists
    # on the page (e.g. "More Filters").
    wrapper = page.locator(
        "div.inte-formElement--Wrap:has(h3:has-text('Customize Grid'))"
    ).first
    if wrapper.count() == 0:
        logging.warning(
            f"⚠️  Customize Grid wrapper not found for {app_name}; "
            f"scraping with default columns only. Baseline DOM in "
            f"debug_dom_seller_pre_{safe_label}.txt."
        )
        return False

    trigger = wrapper.locator(".inte-formElement.inte-select").first
    try:
        trigger.click(timeout=5000)
        logging.info(f"🛠️  Clicked Customize Grid trigger for {app_name}")
    except Exception as err:
        logging.warning(f"   ↳ trigger click failed ({err}); trying force.")
        try:
            trigger.click(force=True, timeout=5000)
        except Exception as err2:
            logging.warning(
                f"⚠️  Could not open Customize Grid for {app_name}: {err2}"
            )
            return False

    # Wait for the option list to become visible — the wrapper flips
    # `aria-expanded` to true, and the `.inte-select--Fake` panel's inline
    # `visibility: hidden` clears. Waiting for one visible option is the
    # cleanest signal the popup actually rendered open.
    try:
        page.wait_for_selector(
            "li.inte-Select__ChoiceList--Item",
            timeout=5000,
            state="visible",
        )
    except PwTimeout:
        _dump_dom(
            page,
            f"Customize Grid popup never became visible for {app_name}",
            filename=f"debug_dom_seller_customize_{safe_label}.txt",
        )
        logging.warning(
            f"⚠️  Customize Grid popup didn't open for {app_name}; "
            "continuing with default columns."
        )
        return False

    # Let any CSS transition finish before clicking options.
    page.wait_for_timeout(250)

    # Dump the open-panel state for posterity — handy if columns shift
    # in a future CedCommerce release.
    _dump_dom(
        page,
        f"Customize Grid panel open for {app_name}",
        filename=f"debug_dom_seller_customize_{safe_label}.txt",
    )

    # --- Helpers ----------------------------------------------------------
    #
    # `_tick_unchecked` does one pass over the option list, toggling every
    # currently-unticked `<li>` (optionally filtered to a label allow-list
    # so we can retry just the items that didn't stick on a previous pass).
    #
    # Why this is a helper and not inline: React 17's state scheduler
    # batches rapid consecutive setState calls. Verified 2026-04-18: when
    # we clicked 5 checkboxes back-to-back with no delay, React merged the
    # updates and ended up reverting 3–5 of the toggles. Spacing the
    # clicks with `wait_for_timeout(200)` after each click lets each
    # setState commit before the next event fires.
    def _wait_for_labels_populated(timeout_ms: int = 5000) -> bool:
        """Wait until every ChoiceList option has a non-empty label text.

        React renders the `<li>` skeleton before the label children are
        populated, so a too-fast read returns empty strings for some
        options. Verified 2026-04-18 on shein: `_tick_unchecked` logged
        `"newly ticked: Order Count, Failed Orders, , "` — two empty
        labels. Those empty strings then auto-pass the substring-match
        in `_wait_for_expected_columns` (since `"" in blob` is always
        True), so missing columns are silently tolerated and the scrape
        proceeds with the wrong schema. Hedge up to 5s to guarantee we
        read every label with real text before making toggle decisions.
        """
        try:
            page.wait_for_function(
                """() => {
                    const items = document.querySelectorAll(
                        'li.inte-Select__ChoiceList--Item'
                    );
                    if (items.length === 0) return false;
                    return Array.from(items).every(li => {
                        const lbl = li.querySelector(
                            'label.inte__checkbox-Label'
                        );
                        return lbl && lbl.innerText.trim().length > 0;
                    });
                }""",
                timeout=timeout_ms,
            )
            return True
        except PwTimeout:
            return False

    def _tick_unchecked(only_labels: set | None = None):
        # Guard against the DOM race where labels haven't populated yet —
        # reading an empty-string label causes downstream verification to
        # silently accept missing columns.
        _wait_for_labels_populated(timeout_ms=5000)

        options = wrapper.locator("li.inte-Select__ChoiceList--Item")
        total_opts = options.count()
        if total_opts == 0:
            return 0, 0, []

        local_toggled = 0
        local_already_on = 0
        local_labels = []
        for i in range(total_opts):
            li = options.nth(i)
            try:
                checkbox = li.locator("input[type='checkbox']").first
                # Authoritative initial state — the HTML `checked` attribute
                # reflects only the INITIAL value React rendered (React
                # updates the `.checked` DOM property, not the attribute).
                # For our use case (toggling default-off columns on) that's
                # fine: we only need to know "was it off originally".
                is_checked = checkbox.get_attribute("checked") is not None
                label_count = li.locator("label.inte__checkbox-Label").count()
                label_text = (
                    li.locator("label.inte__checkbox-Label").first.inner_text()
                    if label_count > 0
                    else (li.get_attribute("value") or f"option-{i}")
                ).strip()

                # Skip options whose label we still couldn't resolve — an
                # empty string would silently auto-pass column verification
                # (empty-string substring match). Better to miss one
                # default-off column than to corrupt the schema check.
                if not label_text:
                    logging.debug(
                        f"   ↳ option #{i} has empty label even after poll; "
                        "skipping to avoid spurious pass"
                    )
                    continue

                if is_checked:
                    local_already_on += 1
                    continue
                if only_labels is not None and label_text not in only_labels:
                    continue

                click_target = (
                    li.locator("label.inte__checkbox-Label").first
                    if label_count > 0
                    else li
                )
                try:
                    click_target.click(timeout=1500)
                except Exception:
                    click_target.click(force=True, timeout=1500)
                local_toggled += 1
                local_labels.append(label_text)
                # Let React commit the state change before the next click.
                # Without this, rapid clicks interleave in React's update
                # queue and some toggles get overridden.
                page.wait_for_timeout(200)
            except Exception as err:
                logging.debug(f"   ↳ option #{i} toggle failed ({err}); skipping")

        return local_toggled, local_already_on, local_labels

    def _popup_is_open() -> bool:
        try:
            return wrapper.get_attribute("aria-expanded") == "true"
        except Exception:
            return False

    def _close_popup() -> bool:
        """Close the Customize Grid popup safely.

        CRITICAL: do NOT re-click `.inte-formElement.inte-select` to close.
        When the popup is open, its `<ul class="inte-choiceList--dropdown">`
        renders INLINE inside the trigger div (the `inte-select--Fake
        inte-select--Fixed` floating wrapper is only present while hidden).
        That absorbs the popup into the trigger's bounding box, so a
        second `trigger.click()` lands on a middle `<li>` and unticks an
        option — OR misses the close handler (aria-expanded stays "true",
        popup overlays pagination row).
        """
        for _ in range(3):
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            page.wait_for_timeout(200)
            if not _popup_is_open():
                return True
            try:
                page.mouse.click(5, 5)
            except Exception:
                pass
            page.wait_for_timeout(200)
            if not _popup_is_open():
                return True
        return False

    def _thead_missing_once(expected: list[str]) -> list[str]:
        """Return labels from `expected` that don't appear in the live
        thead RIGHT NOW. Single-shot check — the caller is expected to poll.
        We substring-match against a joined blob so minor whitespace
        differences between "Product Count" and a <th> with wrapper spans
        don't cause false misses.
        """
        try:
            texts = [
                (th.inner_text() or "").strip().lower()
                for th in page.locator(
                    ".ant-table-wrapper:has(tr.ant-table-row) .ant-table-thead th"
                ).all()
            ]
        except Exception:
            return list(expected)
        blob = " | ".join(texts)
        return [lbl for lbl in expected if lbl.lower() not in blob]

    def _wait_for_expected_columns(
        expected: list[str], timeout_ms: int = 15000
    ) -> list[str]:
        """Poll the thead up to `timeout_ms` for every label in `expected`
        to appear. Returns the list of labels still missing when the timeout
        fires (empty on success).

        This exists because on shein + shopify_temu the table re-renders
        several seconds AFTER Customize Grid closes — our previous one-shot
        check fired too early and triggered harmful retries that reopened
        the popup and sometimes unticked options.
        """
        if not expected:
            return []
        import time as _time

        deadline = _time.monotonic() + (timeout_ms / 1000.0)
        last_missing = list(expected)
        while _time.monotonic() < deadline:
            last_missing = _thead_missing_once(expected)
            if not last_missing:
                return []
            page.wait_for_timeout(250)
        return last_missing

    def _wait_for_table_settle() -> None:
        # We used to wait for `networkidle` here, but the admin panel has
        # background heartbeats / analytics pings that keep the network
        # "active" indefinitely — the call reliably hit its 5s timeout,
        # adding 5s per Customize Grid pass per app (~30s per full run).
        # The *real* signal that the thead has reflowed is
        # `_wait_for_expected_columns`, which we always call right after.
        # A small static settle is enough to let the close-animation
        # finish before we start polling the thead.
        page.wait_for_timeout(250)

    # --- Enumerate target labels (currently unticked options) -----------
    #
    # IMPORTANT: do NOT burst-tick every option in one popup pass. The
    # admin panel reloads the seller table after each checkbox toggle, and
    # on large tables (shein = 349 rows) the table reload tears down the
    # popup mid-burst — later clicks land on detached DOM and silently
    # drop. Verified 2026-04-19 by diffing the run.json first-row dump:
    # for shein/shopify_temu every row had plan / failed_order_count /
    # steps_completed / last_sync empty, while shopify_temu_eu (41 rows,
    # fast reload) had plan populated with real values.
    #
    # The correct pattern — mirroring what a human has to do in the UI —
    # is tick ONE checkbox, let the table reload, reopen the popup, tick
    # the next. We do that serially below.
    _wait_for_labels_populated(timeout_ms=5000)
    options_now = wrapper.locator("li.inte-Select__ChoiceList--Item")
    total = options_now.count()
    if total == 0:
        logging.warning(
            f"   ↳ no ChoiceList items found inside Customize Grid for {app_name}"
        )
        return False

    target_labels: list[str] = []
    observed_labels: list[str] = []  # EVERY non-empty label we see, ticked or not
    already_on = 0
    for i in range(total):
        li = options_now.nth(i)
        try:
            checkbox = li.locator("input[type='checkbox']").first
            is_checked = checkbox.get_attribute("checked") is not None
            label_count = li.locator("label.inte__checkbox-Label").count()
            if label_count == 0:
                continue
            label_text = li.locator(
                "label.inte__checkbox-Label"
            ).first.inner_text().strip()
            if not label_text:
                # Skip empty labels — their substring-match would auto-pass
                # column verification, corrupting the integrity check.
                continue
            observed_labels.append(label_text)
            if is_checked:
                already_on += 1
                continue
            target_labels.append(label_text)
        except Exception as err:
            logging.debug(f"   ↳ enumerate option #{i} failed ({err}); skipping")

    # Surface the full observed label set to the caller so scrape_validator
    # can diff it against grid_columns.yaml. Populated whether or not any
    # ticking succeeds — the diff is what matters, not the action.
    if observed_labels_sink is not None:
        observed_labels_sink.extend(observed_labels)

    logging.info(
        f"   ↳ Customize Grid ({total} options): {already_on} already on, "
        f"{len(target_labels)} to tick serially for {app_name}"
    )
    if target_labels:
        logging.info(f"   ↳ will tick: {', '.join(target_labels)}")

    # Close the popup before starting the serial loop so each
    # `_tick_one_label` call starts from a clean "popup closed" baseline.
    if not _close_popup():
        logging.warning(
            f"⚠️  Customize Grid popup for {app_name} did not close cleanly "
            "before serial tick pass — continuing anyway."
        )
    _wait_for_table_settle()

    # --- Serial tick helper ---------------------------------------------

    def _tick_one_label(label: str, *, verify_ms: int = 10000) -> bool:
        """Open the Customize Grid popup, tick the checkbox whose label
        matches `label`, close the popup, and wait for that column to
        appear in the thead. Returns True on success.

        Idempotent: if the label is already ticked when we open the
        popup (can happen if an earlier burst-click DID stick), we still
        verify the thead and return True/False accordingly.
        """
        try:
            trigger.click(timeout=5000)
        except Exception:
            try:
                trigger.click(force=True, timeout=5000)
            except Exception as err:
                logging.warning(
                    f"   ↳ could not reopen Customize Grid to tick "
                    f"{label!r}: {err}"
                )
                return False
        try:
            page.wait_for_selector(
                "li.inte-Select__ChoiceList--Item",
                timeout=5000,
                state="visible",
            )
        except PwTimeout:
            logging.warning(
                f"   ↳ popup did not reopen (visible) for {label!r}"
            )
            return False
        page.wait_for_timeout(150)
        _wait_for_labels_populated(timeout_ms=5000)

        # Locate the <li> whose label matches.
        items = wrapper.locator("li.inte-Select__ChoiceList--Item")
        target_li = None
        already_ticked = False
        for i in range(items.count()):
            li = items.nth(i)
            lbl_loc = li.locator("label.inte__checkbox-Label")
            if lbl_loc.count() == 0:
                continue
            try:
                current = lbl_loc.first.inner_text().strip()
            except Exception:
                continue
            if current != label:
                continue
            target_li = li
            try:
                cb = li.locator("input[type='checkbox']").first
                already_ticked = cb.get_attribute("checked") is not None
            except Exception:
                already_ticked = False
            break

        if target_li is None:
            logging.warning(
                f"   ↳ label {label!r} not present in reopened popup"
            )
            _close_popup()
            _wait_for_table_settle()
            return False

        if not already_ticked:
            click_target = target_li.locator(
                "label.inte__checkbox-Label"
            ).first
            try:
                click_target.click(timeout=1500)
            except Exception:
                try:
                    click_target.click(force=True, timeout=1500)
                except Exception as err:
                    logging.warning(
                        f"   ↳ click on {label!r} failed: {err}"
                    )
                    _close_popup()
                    _wait_for_table_settle()
                    return False
            # Give React a moment to commit the setState.
            page.wait_for_timeout(200)

        # Close popup + wait for thead to include the column.
        _close_popup()
        _wait_for_table_settle()
        still_missing = _wait_for_expected_columns([label], timeout_ms=verify_ms)
        return not still_missing

    # --- Serial tick pass ------------------------------------------------
    labels_toggled: list[str] = []
    labels_failed: list[str] = []
    for label in target_labels:
        if _tick_one_label(label):
            labels_toggled.append(label)
        else:
            labels_failed.append(label)

    logging.info(
        f"   ↳ serial tick pass: {len(labels_toggled)}/{len(target_labels)} "
        f"stuck on first pass for {app_name}"
    )
    if labels_toggled:
        logging.info(f"   ↳ ticked: {', '.join(labels_toggled)}")
    if labels_failed:
        logging.warning(
            f"   ↳ failed first pass: {labels_failed} — will retry individually"
        )
        # One retry pass per missing label — same popup-per-tick pattern.
        retried_ok: list[str] = []
        for label in list(labels_failed):
            if _tick_one_label(label, verify_ms=15000):
                retried_ok.append(label)
                labels_failed.remove(label)
                labels_toggled.append(label)
        if retried_ok:
            logging.info(f"   ↳ retry pass recovered: {retried_ok}")

    # --- Final full-thead verification ------------------------------------
    expected = labels_toggled + labels_failed
    missing = _wait_for_expected_columns(expected, timeout_ms=15000) if expected else []

    if missing:
        logging.warning(
            f"⚠️  Customize Grid: {len(missing)} columns still absent from "
            f"thead for {app_name}: {missing}. Scrape will proceed with "
            f"the columns that did stick."
        )
    elif labels_toggled:
        logging.info(
            f"   ↳ Customize Grid: all {len(labels_toggled)} requested "
            f"columns present in thead for {app_name}"
        )

    return len(labels_toggled) > 0 or already_on > 0


def set_page_size_100(page, app_name: str) -> bool:
    """
    Flip the seller-list "Items :" dropdown from its default (20) to 100.

    Why: on shein (349 rows) the scraper otherwise walks 18 pages. Each
    page click carries a ~1–2 s React re-render + occasional pagination-bar
    remount hedge, so shein alone costs ~30–45 s just in pagination waits.
    Setting page size to 100 drops that to 4 pages — same row count, a
    quarter of the pagination churn. For shopify_temu (84 rows) it's 1 page
    instead of 5; for shopify_temu_eu (41 rows) it's already a single page
    so this is a cheap no-op.

    Verified 2026-04-19 against debug_dom_seller_shein.txt — the widget is
    a vanilla `inte-select` (NOT a ChoiceList), so the interaction is the
    same shape as the login app dropdown: click trigger → wait for popup
    options → click the `li[value='100']`. Structure:

        <div class="inte-Pagination-perPage--Sorter">
          <div class="inte-formElement--Wrap ... inte-select--thin"
               aria-expanded="false">
            <div class="inte-formElement inte-select inte-Pagination--perPage">
              <span class="inte__Select--Selected">20</span>    <!-- current -->
              <div class="inte-select inte-select--Fake ..."
                   style="visibility: hidden; opacity: 0;">
                <ul aria-label="inte-select-options">
                  <li class="inte-Select__Select--Item" value="10">10</li>
                  <li class="inte-Select__Select--Item" value="20">20</li>
                  <li class="inte-Select__Select--Item" value="50">50</li>
                  <li class="inte-Select__Select--Item" value="100">100</li>
                </ul>
              </div>
            </div>
          </div>
        </div>

    Call AFTER `customize_grid_select_all` so column selection is already
    persisted — the table reload that follows the page-size flip can
    otherwise race the customize-grid popup/click cycle.

    Non-fatal: returns False on any failure and the scrape proceeds with
    the default page size. Returns True if already at 100 (no-op) or if
    the flip succeeded and we saw the table grow past 20 rows (or the
    pagination "Showing 1 - N of TOTAL" text reflect the new size).
    """
    logging.info(f"🔢 Setting page size to 100 for {app_name}")

    # The "Items :" dropdown lives inside `.inte-Pagination-perPage--Sorter`
    # which is itself inside the `.inte-Pagination` footer card. Scope by
    # that ancestor to avoid matching any other inte-select on the page.
    sorter = page.locator(
        "div.inte-Pagination div.inte-Pagination-perPage--Sorter"
    ).first
    try:
        sorter.wait_for(state="visible", timeout=10000)
    except PwTimeout:
        logging.warning(
            f"   ↳ page-size Sorter not visible for {app_name} within 10s; "
            "leaving default page size"
        )
        return False

    # Short-circuit: already at 100 (e.g. scraper re-entered mid-run).
    try:
        current = sorter.locator("span.inte__Select--Selected").first.inner_text().strip()
        if current == "100":
            logging.info(f"   ↳ {app_name} already at page size 100; skipping")
            return True
    except Exception:
        current = ""

    # Snapshot the row count / first-row key so we can detect the reload.
    rows = page.locator("tr.ant-table-row")
    try:
        prev_rows_count = rows.count()
    except Exception:
        prev_rows_count = 0
    prev_first_key = ""
    try:
        if prev_rows_count > 0:
            prev_first_key = rows.nth(0).get_attribute("data-row-key") or ""
    except Exception:
        pass

    # --- Open the dropdown ---
    trigger = sorter.locator(
        "div.inte-formElement.inte-select.inte-Pagination--perPage"
    ).first
    try:
        trigger.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    try:
        trigger.click(timeout=5000)
    except Exception as err:
        logging.warning(
            f"   ↳ primary click on page-size trigger failed ({err}); force-clicking"
        )
        try:
            trigger.click(force=True, timeout=5000)
        except Exception as err2:
            logging.warning(f"   ↳ force-click also failed ({err2}); giving up")
            return False

    # --- Wait for the popup options to be visible ---
    # The `<li value="100">` is the most reliable selector — it's scoped by
    # the exact value attribute, and page.wait_for_selector (with state=visible)
    # hedges the CSS opacity-0/visibility-hidden transition.
    option = page.locator("li.inte-Select__Select--Item[value='100']").first
    try:
        option.wait_for(state="visible", timeout=5000)
    except PwTimeout:
        logging.warning(
            f"   ↳ page-size option '100' not visible within 5s for {app_name}; "
            "dumping DOM"
        )
        _dump_dom(
            page,
            f"page-size dropdown never opened for {app_name}",
            filename=f"debug_dom_pagesize_{app_name}.txt",
        )
        return False

    # --- Click the '100' option ---
    try:
        option.click(timeout=5000)
    except Exception as err:
        logging.warning(
            f"   ↳ click on page-size '100' failed ({err}); trying force-click"
        )
        try:
            option.click(force=True, timeout=5000)
        except Exception as err2:
            logging.warning(f"   ↳ force-click on '100' also failed ({err2})")
            return False

    # --- Force the popup closed ---
    # CRITICAL (2026-04-24): The inte-select popup does NOT auto-close on
    # option-click in the perPage Sorter — verified against a live debug
    # DOM dump where `.inte-select--Fake` retained `visibility:visible;
    # opacity:1` and the wrapper kept `aria-expanded="true"` AFTER the
    # '100' click. That leaves an invisible-looking but hit-testable
    # overlay sitting directly on top of the pagination Next button
    # (both are in the `.inte-Pagination` footer). Every subsequent
    # Next click then lands on the popup (a silent no-op), the table
    # never advances past page 1, and the scraper bails after the
    # 2-click retry window — bleeding all rows past row 20 on every
    # app. Symptom: shein captures 20 instead of 349, temu 20 instead
    # of 84, temu_eu 20 instead of 41.
    #
    # Fix: press Escape + wait for aria-expanded to flip back to "false".
    # If Escape doesn't take (some inte-select builds only listen for
    # click-outside, not keyup), fall through to the JS force-close
    # below. We avoid synthesizing a mouse-click on the page to dismiss
    # the popup because misplacing it by a few pixels could click the
    # Prev button or another pagination control.
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    try:
        page.wait_for_function(
            """() => {
                const w = document.querySelector(
                    'div.inte-Pagination-perPage--Sorter '
                    + 'div.inte-formElement--Wrap'
                );
                if (!w) return true;  // wrapper gone? treat as closed
                return w.getAttribute('aria-expanded') !== 'true';
            }""",
            timeout=3000,
        )
    except PwTimeout:
        logging.warning(
            f"   ↳ page-size popup never reported aria-expanded=false for "
            f"{app_name}; forcing via JS"
        )
        # Last-resort: toggle inline style + aria-expanded ourselves so
        # the paginator isn't overlaid. This is ugly but safe — we are
        # only modifying presentation, not firing any React handlers.
        try:
            page.evaluate(
                """() => {
                    const w = document.querySelector(
                        'div.inte-Pagination-perPage--Sorter '
                        + 'div.inte-formElement--Wrap'
                    );
                    if (w) w.setAttribute('aria-expanded', 'false');
                    document.querySelectorAll(
                        'div.inte-Pagination-perPage--Sorter '
                        + 'div.inte-select--Fake'
                    ).forEach(el => {
                        el.style.visibility = 'hidden';
                        el.style.opacity = '0';
                    });
                }"""
            )
        except Exception:
            pass

    # --- Wait for the selected text to flip to "100" ---
    # This is a pure React state flip — no network needed for the label
    # update. 3s is more than enough.
    try:
        page.wait_for_function(
            """() => {
                const el = document.querySelector(
                    'div.inte-Pagination-perPage--Sorter span.inte__Select--Selected'
                );
                return el && el.textContent.trim() === '100';
            }""",
            timeout=3000,
        )
    except PwTimeout:
        logging.warning(
            f"   ↳ selected label never flipped to '100' for {app_name}; "
            "continuing anyway"
        )

    # --- Wait for the table to actually reload with the new page size ---
    # The admin panel fires a new request, rebuilds the tbody, and updates
    # the "Showing 1 - N of TOTAL" text. User flagged this can take 20–30s
    # on slow runs, so we hedge up to 45s. Signals we accept (whichever
    # arrives first):
    #   (a) the "Showing 1 - N" row count exceeds 20, OR
    #   (b) the first-row data-row-key changes (fresh tbody render), OR
    #   (c) the pagination "of N" total pages drops (4 on shein = good).
    settled = False
    try:
        page.wait_for_function(
            """(prev) => {
                // (a) Showing 1 - N of TOTAL  -> N > 20 means new page size stuck
                const showing = document.querySelector(
                    'div.inte-Pagination div.inte-flex__item > span'
                );
                const parent = showing ? showing.parentElement : null;
                if (parent) {
                    const m = parent.textContent.match(/Showing\\s+\\d+\\s*-\\s*(\\d+)\\s+of/);
                    if (m && parseInt(m[1], 10) > 20) return true;
                }
                // (b) first-row key changed  -> tbody re-rendered
                const r = document.querySelector('tr.ant-table-row');
                if (r && prev && r.getAttribute('data-row-key') !== prev) return true;
                // (c) DOM might also expose total-pages via pag input; the
                //     "of N" text is a sibling. Check for count reduction:
                const items = document.querySelectorAll(
                    'div.inte-Pagination div.inte-flex__item'
                );
                for (const it of items) {
                    const mt = it.textContent.match(/^of\\s+(\\d+)$/);
                    if (mt && parseInt(mt[1], 10) <= 5) return true;
                }
                return false;
            }""",
            arg=prev_first_key,
            timeout=45000,
        )
        settled = True
    except PwTimeout:
        logging.warning(
            f"   ↳ page-size reload signal never observed within 45s for "
            f"{app_name}; the scrape will still run but may be on the old "
            "page size"
        )

    # Tiny settle so the scraper's first-page header-map build sees a
    # stable thead/tbody instead of a mid-reflow snapshot.
    page.wait_for_timeout(300)

    if settled:
        try:
            new_rows = page.locator("tr.ant-table-row").count()
        except Exception:
            new_rows = 0
        logging.info(
            f"   ↳ page size 100 applied for {app_name}; "
            f"rows visible now: {new_rows} (was {prev_rows_count})"
        )
    return settled


def scrape_seller_table(page, app_name, max_pages=None, trace_sink: list | None = None):
    """
    Scrape the Seller List table across ALL pages. Thin wrapper over the
    generic `_scrape_paginated_ant_table`; kept as a separate name so
    callers reading `main()` can see seller-vs-uninstall intent at a glance.

    `trace_sink` — if provided, the paginator appends one entry per page
    (page_num, first_row_key, row_count, reported_total_rows,
    reported_total_pages) for downstream pagination sanity checks.
    """
    # Unconditional baseline DOM dump of the seller table post-customize
    # (or default if customize failed) — same pattern we use for uninstalls.
    safe_label = re.sub(r"[^A-Za-z0-9_]+", "_", app_name).strip("_") or "app"
    _dump_dom(
        page,
        f"baseline DOM snapshot for {app_name} seller table",
        filename=f"debug_dom_seller_{safe_label}.txt",
    )
    return _scrape_paginated_ant_table(
        page, app_name, aliases=HEADER_ALIASES, max_pages=max_pages,
        trace_sink=trace_sink,
    )


# ---------------------------------------------------------------------------
# Uninstalls — Phase 2.4
# ---------------------------------------------------------------------------

def navigate_to_uninstalls(page) -> None:
    """
    From the post-login dashboard, click the left-nav "Uninstalls" item and
    wait for the uninstall table (or an empty-state card) to render.

    The left-nav entries are SPA-routed — they have NO `href`, just click
    handlers — so we must trigger them via a DOM click rather than
    `page.goto(...)`. Observed structure (from debug_dom dumps):

        <li class="inte-flex__item">
          <a class="inte__Menus">
            <span class="inte__menuIcon">…svg…</span>
            <span class="inte__menuItem">Uninstalls</span>
          </a>
        </li>
    """
    logging.info("🔗 Clicking left-nav → Uninstalls")

    nav_link = page.locator(
        "a.inte__Menus:has(span.inte__menuItem:has-text('Uninstalls'))"
    ).first
    if nav_link.count() == 0:
        # Fallback: try the menu-item span directly (click event likely
        # bubbles up to the anchor via React). If we still can't find it,
        # dump the current DOM for diagnosis.
        alt = page.locator("span.inte__menuItem:has-text('Uninstalls')").first
        if alt.count() == 0:
            _dump_dom(page, "uninstalls nav link not found",
                      filename="debug_dom_uninstalls_nav_missing.txt")
            _screenshot(page, "error_uninstalls_nav_missing.png")
            raise RuntimeError(
                "Could not find 'Uninstalls' link in left nav. "
                "DOM dumped to debug_dom_uninstalls_nav_missing.txt."
            )
        nav_link = alt

    # Capture current state so we can detect the transition either via URL
    # change or via the table's first data-row-key flipping.
    prev_url = page.url
    prev_first_key = ""
    try:
        first_row = page.locator("tr.ant-table-row").first
        if first_row.count() > 0:
            prev_first_key = first_row.get_attribute("data-row-key") or ""
    except Exception:
        pass

    try:
        nav_link.click(timeout=8000)
    except Exception as click_err:
        # Last-ditch: force-click.
        logging.warning(f"   ↳ primary click failed ({click_err}); force-clicking.")
        nav_link.click(force=True, timeout=8000)

    # Best-effort URL-change wait. SPA may or may not change the path; the
    # table-row check below is the real signal.
    try:
        page.wait_for_url(
            lambda url: url != prev_url,
            timeout=6000,
        )
        logging.info(f"   ↳ URL changed: {page.url}")
    except PwTimeout:
        logging.info(f"   ↳ URL did not change (SPA route). Still: {page.url}")

    # Wait for either new rows (with a different first-row key) or an
    # empty-state card. Whichever arrives first ends the wait.
    try:
        if prev_first_key:
            page.wait_for_function(
                """(prev) => {
                    const r = document.querySelector('tr.ant-table-row');
                    if (r && r.getAttribute('data-row-key') !== prev) return true;
                    if (document.querySelector('.ant-empty, .inte-emptyState')) return true;
                    return false;
                }""",
                arg=prev_first_key,
                timeout=15000,
            )
        else:
            page.wait_for_selector(
                "tr.ant-table-row, .ant-empty, .inte-emptyState",
                timeout=15000,
            )
    except PwTimeout:
        _screenshot(page, "error_uninstalls_nav_wait.png")
        _dump_dom(page, "timeout waiting for uninstalls table/empty state",
                  filename="debug_dom_uninstalls_nav_wait.txt")
        raise

    # Let any late-arriving sorters/filters settle before the scraper loop
    # starts reading rows. Small static settle is enough — the scraper
    # loop has its own empty-state hedge and per-page header rebuild that
    # handle slower renders.
    page.wait_for_timeout(250)


def scrape_uninstalls_table(
    page, app_name, max_pages=None, trace_sink: list | None = None,
) -> list:
    """
    Scrape the Uninstalls table across all pages for one app. Assumes the
    page is already on the uninstalls view (call `navigate_to_uninstalls`
    first). Returns a flat list of per-(seller, platform) uninstall dicts
    keyed by UNINSTALL_CSV_COLUMNS.

    Diagnostic: writes an unconditional `debug_dom_uninstalls_<app>.txt`
    on every run so if the column layout changes again we have fresh
    ground truth for the next scraper-fix cycle (this was the #1 thing
    that let us diagnose the seller-page fix earlier — same pattern here).
    """
    safe_label = re.sub(r"[^A-Za-z0-9_]+", "_", app_name).strip("_") or "app"
    dump_path = f"debug_dom_uninstalls_{safe_label}.txt"
    _dump_dom(
        page,
        f"baseline DOM snapshot for {app_name} uninstalls page",
        filename=dump_path,
    )

    # De-dup key: one record per (seller, platform, uninstalled_on) — a
    # user who uninstalled Shopify AND Shein must count as two events.
    def _uninstall_dedup(item: dict) -> str:
        return "|".join([
            item.get("seller_id", "") or "",
            item.get("platform", "") or "",
            item.get("uninstalled_on", "") or "",
        ])

    return _scrape_paginated_ant_table(
        page,
        label=f"{app_name} uninstalls",
        aliases=UNINSTALL_HEADER_ALIASES,
        max_pages=max_pages,
        extractor=extract_uninstall_row,
        dedup_key=_uninstall_dedup,
        trace_sink=trace_sink,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _now_stamp() -> str:
    """Timestamp safe for filesystems: `2026-04-17_20-34-29Z`."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%SZ")


def _write_csv(path: Path, rows: list, columns: list) -> None:
    """Write rows to a CSV using `columns` as the canonical field order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            # Fill missing keys with "" so every column is present even when
            # an app doesn't populate it (e.g. non-shein apps have no app_type).
            w.writerow({k: r.get(k, "") for k in columns})


def _load_previous_counts() -> dict:
    """
    Peek at the existing `results/latest/` CSVs before they get overwritten.
    Returns `{"sellers": {app: N}, "uninstalls": {app: N}}`. Missing files =
    0 (first run). Used by scrape_validator.check_row_count to guard against
    silent data-loss (e.g. a pagination regression that returns 1 page).
    """
    prev = {"sellers": {}, "uninstalls": {}}
    if not LATEST_DIR.exists():
        return prev
    for p in LATEST_DIR.glob("*.csv"):
        try:
            with p.open("r", encoding="utf-8") as f:
                # Subtract 1 for the header line. Empty file → 0.
                n = max(0, sum(1 for _ in f) - 1)
        except Exception:
            n = 0
        name = p.stem
        if name.endswith("_uninstalls"):
            prev["uninstalls"][name[: -len("_uninstalls")]] = n
        else:
            prev["sellers"][name] = n
    return prev


def persist_results(
    sellers_by_app: dict,
    uninstalls_by_app: dict | None = None,
    stamp: str | None = None,
    promote_latest: bool = True,
) -> dict:
    """
    Write per-app seller + uninstall CSVs (latest + history) and a combined
    run JSON. `sellers_by_app` keeps the legacy key name so we don't break
    the existing seller-CSV filenames under `latest/`.

    Output layout:

        results/
          latest/                          # only touched when promote_latest
            <app>.csv                      # sellers
            <app>_uninstalls.csv           # uninstalls (Phase 2.4)
            run.json                       # mirror of the history snapshot
          history/
            <stamp>/                       # always written (audit trail)
              <app>.csv
              <app>_uninstalls.csv
              run.json
          staging/<stamp>/                 # written when promote_latest=False
            <app>.csv                      # — the run is preserved verbatim,
            <app>_uninstalls.csv             but NOT visible to the dashboard
            run.json                         until a super admin promotes it.

    When `promote_latest=False` the previous `latest/` is left untouched so
    consumers keep reading the last-good snapshot. This is the guardrail
    that lets scrape_validator refuse a partially-corrupt run.

    Returns a dict with the paths written, so main() can log them clearly.
    """
    stamp = stamp or _now_stamp()
    history_run_dir = HISTORY_DIR / stamp
    history_run_dir.mkdir(parents=True, exist_ok=True)
    # Staging dir is only used when we DON'T promote — keeps the failed
    # run's bytes on disk for post-mortem without stepping on latest/.
    staging_run_dir = STAGING_DIR / stamp
    target_dir = LATEST_DIR if promote_latest else staging_run_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    uninstalls_by_app = uninstalls_by_app or {}
    written = {
        "latest": {},
        "history": {},
        "staging": {},
        "json": None,
        "promoted": promote_latest,
    }
    dest_bucket = "latest" if promote_latest else "staging"

    # --- sellers ---
    for app_name, rows in sellers_by_app.items():
        target_path = target_dir / f"{app_name}.csv"
        _write_csv(target_path, rows, CSV_COLUMNS)
        written[dest_bucket][app_name] = str(target_path)
        hist_path = history_run_dir / f"{app_name}.csv"
        _write_csv(hist_path, rows, CSV_COLUMNS)
        written["history"][app_name] = str(hist_path)

    # --- uninstalls ---
    for app_name, rows in uninstalls_by_app.items():
        key = f"{app_name}_uninstalls"
        target_path = target_dir / f"{key}.csv"
        _write_csv(target_path, rows, UNINSTALL_CSV_COLUMNS)
        written[dest_bucket][key] = str(target_path)
        hist_path = history_run_dir / f"{key}.csv"
        _write_csv(hist_path, rows, UNINSTALL_CSV_COLUMNS)
        written["history"][key] = str(hist_path)

    # One combined JSON per run — has metadata + all rows, good for feeding
    # straight into the Supabase upsert step later. Kept backwards-compatible
    # with the earlier shape by keeping a top-level `counts` + `total` (for
    # sellers), while adding an `uninstalls` section beside them.
    run_json = {
        "run_stamp": stamp,
        "run_started_utc": datetime.now(timezone.utc).isoformat(),
        "counts": {name: len(rows) for name, rows in sellers_by_app.items()},
        "total": sum(len(rows) for rows in sellers_by_app.values()),
        "uninstall_counts": {name: len(rows) for name, rows in uninstalls_by_app.items()},
        "uninstall_total": sum(len(rows) for rows in uninstalls_by_app.values()),
        "data": sellers_by_app,
        "uninstalls": uninstalls_by_app,
    }
    json_path = history_run_dir / "run.json"
    json_path.write_text(json.dumps(run_json, indent=2, ensure_ascii=False), encoding="utf-8")
    # Mirror to whichever bucket this run is targeting. Blocked runs land
    # in staging/<stamp>/run.json and leave latest/run.json untouched —
    # the dashboard keeps rendering the last-good snapshot.
    mirror_json = target_dir / "run.json"
    mirror_json.write_text(json.dumps(run_json, indent=2, ensure_ascii=False), encoding="utf-8")
    written["json"] = str(json_path)

    return written


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    # Snapshot the previous run's row counts BEFORE any scraping so we can
    # diff them against this run. Drop >50% = block; >20% = pending_review.
    previous_counts = _load_previous_counts()
    grid_cfg = sv._load_grid_columns()

    # TARGET_APP — when set by the workflow_dispatch input, scrape only
    # that one app (by id). Admin-UI onboarding uses this to validate a
    # new panel without hammering cHAP's backend with a full re-scrape
    # of everything already configured. Empty / unset → behave as before
    # (loop all apps — that's the scheduled-cron path).
    target_app = (os.getenv("TARGET_APP") or "").strip()
    if target_app:
        logging.info(f"🎯 TARGET_APP set → scoping run to '{target_app}' only.")

    # Per-app validator context lives outside playwright so a browser crash
    # can't lose it. `traces` holds the pagination traces produced by the
    # paginator's trace_sink; `observed_labels` holds what Customize Grid
    # showed us.
    seller_traces: dict[str, list] = {}
    uninstall_traces: dict[str, list] = {}
    observed_labels_by_app: dict[str, list] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)

        # Apps with shared_schedule=False live in their own workflow
        # file and run on their own cron. The shared scrape.yml should
        # NOT also scrape them (double-hitting cHAP's backend). When
        # TARGET_APP is set, that override wins — a targeted dispatch
        # should always honor the request regardless of routing.
        import app_registry as _ar
        _solo_ids = {
            a.id for a in _ar.all_apps() if not a.shared_schedule
        }

        all_sellers = {}
        all_uninstalls = {}
        for app_name, app_id in APP_IDS.items():
            if not app_id:
                continue
            # Targeted single-app run: skip anything that doesn't match.
            # Match on either the app's key in the registry (name) or its
            # runtime id, since both forms flow through TARGET_APP depending
            # on the caller.
            if target_app and target_app not in (app_name, app_id):
                logging.info(
                    f"⏭  Skipping '{app_name}' — TARGET_APP='{target_app}'."
                )
                continue
            # Solo-schedule app (own workflow file). Only skip when we're
            # NOT running a targeted dispatch for it.
            if not target_app and app_id in _solo_ids:
                logging.info(
                    f"⏭  Skipping '{app_name}' — runs on its own schedule "
                    f"(see .github/workflows/scrape_{app_id}.yml)."
                )
                continue
            user, pwd = CREDENTIALS.get(app_id, (None, None))
            logging.info(f"\n--- ATTEMPTING: {app_name} ({app_id}) as {user} ---")

            # Fresh context per app — otherwise cookies from app #1 stick
            # and the next app's `goto(LOGIN_URL)` auto-redirects to the
            # already-authenticated dashboard (no login form → username
            # selector times out).
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()
            # Belt-and-braces: clear cookies + storage too, in case a
            # service worker or IndexedDB survived context creation.
            try:
                context.clear_cookies()
            except Exception as err:
                logging.debug(f"clear_cookies failed (non-fatal): {err}")
            try:
                login_and_prepare(page, app_id, username=user, password=pwd)

                # --- Customize Grid (Phase 2.2) ---
                # Tick every available column so optional KPIs like
                # Order Count / Product Count land in the CSV. Non-fatal:
                # if the widget isn't found, the scraper still runs with
                # whatever default columns the dashboard rendered. The
                # observed_labels sink captures the full popup label set so
                # scrape_validator can diff it against grid_columns.yaml.
                observed_labels: list[str] = []
                try:
                    customize_grid_select_all(
                        page, app_name, observed_labels_sink=observed_labels,
                    )
                except Exception:
                    logging.exception(
                        f"Customize Grid step raised for {app_name}; "
                        "continuing with default columns."
                    )
                observed_labels_by_app[app_name] = observed_labels

                # --- Page size: 20 → 100 (perf) ---
                # Do this AFTER Customize Grid so the grid-column-sticking
                # race (serial tick / popup re-open) has already completed
                # against the default 20-row table. Flipping to 100 now
                # means pagination on shein drops from ~18 pages to ~4.
                # Non-fatal: on failure we keep the default page size and
                # just do more page turns.
                try:
                    set_page_size_100(page, app_name)
                except Exception:
                    logging.exception(
                        f"Page-size flip to 100 raised for {app_name}; "
                        "continuing at default page size."
                    )

                # --- Sellers ---
                seller_trace: list = []
                sellers = scrape_seller_table(
                    page, app_name, trace_sink=seller_trace,
                )
                seller_traces[app_name] = seller_trace
                logging.info(f"⭐ SELLERS: Found {len(sellers)} for {app_name}.")
                all_sellers[app_name] = sellers
                for idx, seller in enumerate(sellers[:5], start=1):
                    logging.info(f"  [{idx}] {seller}")

                # --- Uninstalls (Phase 2.4) ---
                # Wrapped separately so an uninstall-page failure doesn't
                # wipe the seller data we already scraped successfully.
                try:
                    navigate_to_uninstalls(page)
                    uninstall_trace: list = []
                    uninstalls = scrape_uninstalls_table(
                        page, app_name, trace_sink=uninstall_trace,
                    )
                    uninstall_traces[app_name] = uninstall_trace
                    logging.info(
                        f"🗑️  UNINSTALLS: Found {len(uninstalls)} for {app_name}."
                    )
                    all_uninstalls[app_name] = uninstalls
                    for idx, u in enumerate(uninstalls[:3], start=1):
                        logging.info(f"  [U{idx}] {u}")
                except Exception:
                    logging.exception(
                        f"Uninstalls scrape failed for {app_name}; "
                        "sellers already captured. Continuing."
                    )
            except Exception:
                logging.exception(f"Aborting {app_name}; moving on.")
            finally:
                page.close()
                context.close()
        browser.close()

    # --- Validation (Phase: fail-proof guardrail) ---------------------------
    # Build one ValidationReport per (app, kind). If ANY report flags
    # `status=blocked` we refuse to overwrite the previous `latest/` and
    # drop the run into `results/staging/<stamp>/` instead. A markdown
    # breadcrumb (INVALID_RUN.md) lands in latest/ so the next operator
    # (or admin UI) can see that the latest snapshot is stale-by-design.
    all_reports: list = []

    def _traces_to_pagetraces(raw: list) -> list:
        return [sv.PageTrace(
            page_num=e.get("page_num", 0),
            first_row_key=e.get("first_row_key", ""),
            row_count=e.get("row_count", 0),
            reported_total_rows=e.get("reported_total_rows"),
            reported_total_pages=e.get("reported_total_pages"),
        ) for e in raw]

    for app_name in list(all_sellers.keys()) + [
        a for a in all_uninstalls.keys() if a not in all_sellers
    ]:
        seller_rows = all_sellers.get(app_name, [])
        seller_report = sv.validate_app(
            app_name=app_name,
            kind="sellers",
            observed_grid_labels=observed_labels_by_app.get(app_name, []),
            pagination_trace=_traces_to_pagetraces(seller_traces.get(app_name, [])),
            scraped_row_count=len(seller_rows),
            previous_row_count=previous_counts["sellers"].get(app_name, 0),
            grid_cfg=grid_cfg,
        )
        all_reports.append(seller_report)

        if app_name in all_uninstalls:
            uninstall_rows = all_uninstalls[app_name]
            # Uninstalls don't have a Customize Grid popup, so observed_labels
            # is empty and the grid check is a no-op for them (validator
            # treats empty observed as "not applicable").
            uninstall_report = sv.validate_app(
                app_name=app_name,
                kind="uninstalls",
                observed_grid_labels=None,
                pagination_trace=_traces_to_pagetraces(uninstall_traces.get(app_name, [])),
                scraped_row_count=len(uninstall_rows),
                previous_row_count=previous_counts["uninstalls"].get(app_name, 0),
                grid_cfg=grid_cfg,
            )
            all_reports.append(uninstall_report)

    promote = all(r.is_promotable for r in all_reports) if all_reports else False
    blocked = [r for r in all_reports if r.status == "blocked"]
    pending = [r for r in all_reports if r.status == "pending_review"]

    if blocked:
        logging.warning(
            f"🛑 Validation BLOCKED this run ({len(blocked)} report(s) "
            f"flagged). Previous latest/ snapshot preserved."
        )
        for r in blocked:
            logging.warning(f"   ↳ {r.app_name} / {r.kind}: blocked")
    elif pending:
        logging.warning(
            f"⚠️  {len(pending)} validator report(s) flagged pending_review. "
            f"Promoting anyway (soft signal only); admin UI should surface."
        )

    # --- Persist -----------------------------------------------------------
    if all_sellers or all_uninstalls:
        try:
            stamp = _now_stamp()
            written = persist_results(
                all_sellers,
                uninstalls_by_app=all_uninstalls,
                stamp=stamp,
                promote_latest=promote,
            )
            seller_total = sum(len(v) for v in all_sellers.values())
            uninstall_total = sum(len(v) for v in all_uninstalls.values())
            bucket = "latest" if promote else "staging"
            logging.info(
                f"💾 Persisted {seller_total} sellers + {uninstall_total} "
                f"uninstalls across {len(all_sellers)} apps → {bucket}/"
            )
            for key, path in written[bucket].items():
                logging.info(f"   ↳ {bucket}: {path}")
            logging.info(f"   ↳ run snapshot: {written['json']}")

            # If validation blocked: drop a markdown breadcrumb INTO latest/
            # so anyone opening the dashboard sees *why* the numbers look
            # stale. Also write the full validator report next to it for
            # post-mortem. `latest/` CSVs themselves are untouched.
            if not promote:
                LATEST_DIR.mkdir(parents=True, exist_ok=True)
                report_md = sv.format_run_report(
                    all_reports,
                    promoted=False,
                    stamp=stamp,
                )
                (LATEST_DIR / "INVALID_RUN.md").write_text(
                    report_md, encoding="utf-8",
                )
                logging.warning(
                    f"   ↳ breadcrumb written: {LATEST_DIR / 'INVALID_RUN.md'}"
                )
            else:
                # Happy path: clear any stale breadcrumb from a previous
                # blocked run so the dashboard doesn't keep showing the
                # warning after the issue is resolved.
                stale = LATEST_DIR / "INVALID_RUN.md"
                if stale.exists():
                    stale.unlink()
        except Exception:
            logging.exception("Failed to persist results; in-memory data is intact.")
    else:
        logging.warning("No results to persist — every app failed to scrape.")

    return {"sellers": all_sellers, "uninstalls": all_uninstalls}


if __name__ == "__main__":
    main()