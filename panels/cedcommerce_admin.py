"""
panels/cedcommerce_admin.py — adapter for the CedCommerce Yii2 admin
panel at admin.apps.cedcommerce.com.

This adapter is INTENTIONALLY isolated from the cHAP scraper:

  - Output lives under `cedadmin_data/` (cHAP uses `results/`).
  - Sections are configured in `cedadmin_apps.yaml`, NOT `apps.yaml`.
  - Authenticates with its own `CED_ADMIN_USER` / `CED_ADMIN_PASS`
    credentials (panel-level, not per-app).
  - Has its own GitHub Actions workflow + cron schedule.
  - The cHAP dashboard does NOT surface this data; a separate
    surface lands in a future phase.

Each marketplace card on the panel (WALMART US, NEWEGG US, ONBUY,
WISH, …) exposes multiple SECTIONS (Shop Details, Order Details,
Bookmarks, Refund, Analytics, …). An (app, section) pair is the
unit of scraping. The operator picks which sections to sync via
`cedadmin_apps.yaml::apps[*].sections`.

Phase 1 (this file as committed): only the Walmart US → Analytics
section is wired. Future phases add the other sections + a UI for
toggling them.

Tech stack notes (from 2026-05-08 recon):
  - Yii2 PHP, Apache, _backendCSRF + APPSCEDCOMMERCEBACKSESSID cookies.
  - Login form fields: AdminLoginForm[username] / [password].
  - Submit button has a JS click interceptor — calling button.click()
    silently no-ops. Use document.forms[0].submit() to bypass.
  - GridView accepts `?per-page=N` (no observed cap) and
    `?columns[X]=1` to enable optional columns. Single GET with
    per-page=50000 returns the entire dataset for any section.
  - ~12s for Walmart Analytics' 26,366 rows × 29 cols via plain HTTP.
    DOM-rendering that many rows in headless Chromium times out.
    So: login via Playwright, steal cookies, fetch grid via requests.
"""
from __future__ import annotations

import csv
import logging
import os
import sys
import time
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Optional

import requests

LOGGER = logging.getLogger(__name__)

PANEL_BASE_URL = "https://admin.apps.cedcommerce.com"
LOGIN_URL = f"{PANEL_BASE_URL}/base/site/index"

# Output is intentionally separate from cHAP's `results/` tree — see
# module docstring. Dashboard for this panel lands in a future phase.
DATA_DIR = Path("cedadmin_data")
LATEST_DIR = DATA_DIR / "latest"
HISTORY_DIR = DATA_DIR / "history"

# Per-(app, section) config. Each section has its own URL path and
# column whitelist. Adding a section is a single dict entry.
#
# Phase 1: walmart_us / analytics only. Future phases extend by
# discovering each section's URL + column checkboxes during a one-off
# probe (mirrors apps.yaml::frameworks auto-discovery for cHAP).
SECTIONS: dict[tuple[str, str], dict] = {
    ("walmart_us", "analytics"): {
        "label": "Walmart US — Analytics",
        "path": "/walmartanalytics/index/index",
        "columns": (
            "MID", "SHOP_URL", "EMAIL", "SHOP_NAME", "CONTACT_NUMBER",
            "INSTALLATION_STATUS", "INSTALLATION_DATE", "UNINSTALLTION_DATE",
            "LAST_LOGIN_IN_APP", "CONFIG_STATUS", "ONBOARDING_STATUS",
            "SHOPIFY_PLAN", "PURCHASE_STATUS", "CURRENT_SUBSCRIBED_PLAN",
            "PAYMENT_TYPE", "ALL_PLANS_SUBSCRIBED", "PAYMENT_DATE",
            "EXPIRATION_DATE", "TOTAL_SKUS", "PUBLISHED_SKU", "STAGED_SKU",
            "TOTAL_ORDERS", "SUCCESS_ORDERS", "FAILED_ORDERS",
            "PROVINCE", "COUNTRY", "OTHER_OLDAPPS", "BUSINESS_CATEGORY",
        ),
    },
}


def login_and_get_cookies(username: str, password: str) -> dict:
    """Run a headless browser through the login form, return cookie dict.

    Returns {cookie_name: value}. Caller hands these to requests.get
    via the `cookies=` kwarg. The browser is closed before returning.
    """
    from playwright.sync_api import sync_playwright

    if not username or not password:
        raise ValueError("login_and_get_cookies needs username + password")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(viewport={"width": 1400, "height": 900})
            page = ctx.new_page()

            page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
            page.locator(
                "input[name='AdminLoginForm[username]']"
            ).first.fill(username)
            page.locator(
                "input[name='AdminLoginForm[password]']"
            ).first.fill(password)

            with page.expect_navigation(timeout=20000):
                page.evaluate("document.forms[0].submit()")

            if "login" in page.url.lower():
                raise RuntimeError(
                    f"login failed — landed on {page.url} after submit"
                )

            cookies = {c["name"]: c["value"] for c in ctx.cookies()}
            if "APPSCEDCOMMERCEBACKSESSID" not in cookies:
                raise RuntimeError(
                    "login did not produce a session cookie; "
                    f"got: {list(cookies)}"
                )
            return cookies
        finally:
            browser.close()


def fetch_grid_html(
    cookies: dict, path: str, columns: tuple[str, ...], per_page: int = 50000,
) -> str:
    """GET the grid page with all `columns` enabled and a giant per-page.

    Returns the raw HTML body. ~35MB / ~12s for Walmart's 26k rows × 29
    cols. Caller parses.
    """
    qs = "&".join(f"columns[{c}]=1" for c in columns)
    url = f"{PANEL_BASE_URL}{path}?per-page={per_page}&{qs}"
    LOGGER.info(f"GET {url[:120]}… (qs len={len(qs)})")
    t0 = time.time()
    r = requests.get(
        url,
        cookies=cookies,
        timeout=180,
        headers={"User-Agent": "Mozilla/5.0 cedadmin-scraper/1.0"},
    )
    elapsed = time.time() - t0
    LOGGER.info(
        f"  HTTP {r.status_code} in {elapsed:.1f}s, "
        f"body size: {len(r.content):,} bytes"
    )
    r.raise_for_status()
    return r.text


def parse_grid_to_rows(html: str) -> list[dict]:
    """Pull the seller table out of the page HTML and return a list of
    row dicts. Column keys are the panel's UPPER_SNAKE_CASE names
    (matching the SECTIONS columns config), so downstream code doesn't
    have to reason about the panel's HTML quirks.

    The grid uses a 2-row thead (column name + filter row), so
    pandas.read_html produces a MultiIndex on columns. We flatten by
    taking only the first level — the second level is filter widgets
    we don't care about — and drop the unnamed leading column (row
    number / checkbox).
    """
    import pandas as pd

    # `match=` filters out unrelated tables on the page (filter forms,
    # nav stubs). Wrap the literal HTML in StringIO to silence
    # pandas' deprecation warning.
    tables = pd.read_html(
        StringIO(html), match=r"Mid|MID|Shop\s+Url", flavor="lxml",
    )
    if not tables:
        raise RuntimeError("could not locate the seller grid in the response HTML")
    df = max(tables, key=len)
    LOGGER.info(f"parsed grid: {len(df):,} rows × {len(df.columns)} cols")

    # Flatten MultiIndex columns. Take level 0 (the panel-rendered
    # column names — "Mid", "Shop  Url", "Email", etc.).
    if hasattr(df.columns, "get_level_values"):
        df.columns = df.columns.get_level_values(0)

    # Normalise: collapse multi-spaces, snake_case, drop "unnamed" cols
    # (the leading row-number / checkbox column).
    import re
    new_cols = []
    keep_idx = []
    for i, col in enumerate(df.columns):
        normalized = re.sub(r"\s+", "_", str(col).strip()).lower()
        if normalized.startswith("unnamed"):
            continue
        new_cols.append(normalized)
        keep_idx.append(i)
    df = df.iloc[:, keep_idx]
    df.columns = new_cols

    out: list[dict] = []
    for raw in df.to_dict(orient="records"):
        row = {}
        for k, v in raw.items():
            if v is None or _is_nan(v):
                row[k] = ""
            else:
                row[k] = str(v).strip()
        out.append(row)
    return out


def _is_nan(v) -> bool:
    try:
        import math
        return isinstance(v, float) and math.isnan(v)
    except Exception:
        return False


def scrape_section(
    app_id: str, section_id: str, *, username: str, password: str,
) -> list[dict]:
    """Login + fetch + parse for one (app, section) pair on this panel.

    Returns a list of row dicts ready to hand to a CSV writer.
    """
    cfg = SECTIONS.get((app_id, section_id))
    if not cfg:
        raise ValueError(
            f"unknown (app, section)=({app_id}, {section_id}); "
            f"known: {sorted(SECTIONS.keys())}"
        )
    LOGGER.info(f"scrape_section: ({app_id}, {section_id}) → {cfg['label']}")
    cookies = login_and_get_cookies(username, password)
    LOGGER.info(f"  logged in; got {len(cookies)} cookie(s)")
    html = fetch_grid_html(cookies, cfg["path"], cfg["columns"])
    rows = parse_grid_to_rows(html)
    LOGGER.info(f"  → {len(rows):,} rows for {app_id}/{section_id}")
    return rows


# ---------------------------------------------------------------------
# CLI: `python -m panels.cedcommerce_admin <app_id> [<section_id>]`
# ---------------------------------------------------------------------
def _output_paths(app_id: str, section_id: str) -> tuple[Path, Path, Path, Path]:
    """Return (latest_csv, previous_csv, history_csv, stamp_file) paths.

    `previous_csv` lives next to `latest_csv` in cedadmin_data/latest/
    (which IS committed back to git, unlike history/). Each scrape
    rotates the OLD latest into previous BEFORE writing the new latest,
    so the dashboard always has a two-snapshot diff available without
    needing the gitignored history tree.
    """
    latest = LATEST_DIR / f"{app_id}__{section_id}.csv"
    previous = LATEST_DIR / f"{app_id}__{section_id}.previous.csv"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%SZ")
    history = HISTORY_DIR / stamp / f"{app_id}__{section_id}.csv"
    stamp_file = LATEST_DIR / f"{app_id}__{section_id}.stamp"
    return latest, previous, history, stamp_file


def _write_csv(rows: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        out_path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in keys})


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if len(sys.argv) < 2:
        print(
            "usage: python -m panels.cedcommerce_admin <app_id> [<section_id>]\n"
            f"  known sections: {sorted(SECTIONS.keys())}",
            file=sys.stderr,
        )
        return 2
    app_id = sys.argv[1]
    section_id = sys.argv[2] if len(sys.argv) > 2 else "analytics"

    user = os.environ.get("CED_ADMIN_USER", "").strip()
    pw = os.environ.get("CED_ADMIN_PASS", "")
    if not user or not pw:
        print(
            "ERROR: set CED_ADMIN_USER and CED_ADMIN_PASS env vars",
            file=sys.stderr,
        )
        return 3

    rows = scrape_section(app_id, section_id, username=user, password=pw)

    latest, previous, history, stamp_file = _output_paths(app_id, section_id)

    # Rotate the prior latest → previous BEFORE writing the new
    # latest. This gives the dashboard a two-snapshot diff (current
    # vs prior) for the "What changed since last sync" section.
    if latest.exists():
        try:
            import shutil
            shutil.copy2(latest, previous)
            LOGGER.info(f"rotated prior latest → {previous}")
        except Exception as err:
            LOGGER.warning(f"couldn't rotate prior latest to previous: {err}")

    _write_csv(rows, latest)
    _write_csv(rows, history)
    stamp_file.write_text(
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        encoding="utf-8",
    )
    LOGGER.info(f"wrote {len(rows):,} rows → {latest}")
    LOGGER.info(f"history snapshot → {history}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
