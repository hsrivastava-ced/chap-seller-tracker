"""
Diagnostic: find the cHAP login dropdown and list its options.

Robust to cHAP selector drift — tries multiple locator strategies, and
if none work, dumps the top-level page HTML so we can see what class
names/elements are actually there.

Run with:
    source .venv/bin/activate
    python scripts/inspect_chap_dropdown.py
"""
import os
import re
import sys
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

load_dotenv()
url = os.environ["LOGIN_URL"]
print(f"Navigating to {url}\n")

# Dropdown-header candidates (most likely first).
HEADER_SELECTORS = [
    ".inte-Select__Select--Header",
    ".inte-Select--Header",
    "[class*='Select--Header']",
    "[class*='Select__Header']",
    "div[role='combobox']",
    ".ant-select-selector",
    # The dropdown often sits under an <h3> with exact text.
    "div:has(> h3:has-text('Select the Integration Apps')) [class*='Select']",
]

# Option-item candidates.
ITEM_SELECTORS = [
    "li.inte-Select__Select--Item",
    "li[class*='Select--Item']",
    "li[class*='Select__Item']",
    ".ant-select-item-option",
    "[role='option']",
]


def try_find(page, selectors, timeout_ms=8000):
    """Return the first locator that resolves to >=1 visible element, else None."""
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=timeout_ms, state="visible")
            count = page.locator(sel).count()
            if count > 0:
                print(f"  ✓ matched selector: {sel}  ({count} element(s))")
                return page.locator(sel), sel
        except PwTimeout:
            continue
    return None, None


with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
    page.goto(url, wait_until="domcontentloaded")

    # Let the React SPA finish mounting.
    print("Waiting up to 30s for the page to settle…")
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PwTimeout:
        print("  (networkidle didn't fire — continuing anyway)")

    print("\nLooking for the dropdown header…")
    header, header_sel = try_find(page, HEADER_SELECTORS, timeout_ms=4000)

    if header is None:
        print("\n❌ None of the header selectors matched.")
        print("Dumping visible text + class samples so we can see what's there:\n")
        body_html = page.content()
        # Show any element with 'Select' in its class name.
        classes = sorted(set(re.findall(r'class="([^"]*Select[^"]*)"', body_html)))
        print("Classes containing 'Select' on this page:")
        for c in classes[:30]:
            print(f"  {c}")
        # Visible labels so we can hand-pick the right one.
        print("\nAll visible h1-h4 on the page:")
        for tag in ("h1", "h2", "h3", "h4"):
            for t in page.locator(tag).all_inner_texts():
                if t.strip():
                    print(f"  <{tag}> {t.strip()!r}")
        print("\nDump of all <select> elements (in case native select fallback exists):")
        for i in range(page.locator("select").count()):
            print(f"  select[{i}] name={page.locator('select').nth(i).get_attribute('name')!r}")
        browser.close()
        sys.exit(1)

    print(f"\nClicking header via {header_sel}…")
    # First() in case multiple match.
    try:
        header.first.click()
    except Exception as e:
        print(f"  click failed: {e}")
        browser.close()
        sys.exit(1)

    print("\nWaiting for options to render…")
    items, item_sel = try_find(page, ITEM_SELECTORS, timeout_ms=6000)

    if items is None:
        print("\n❌ None of the option selectors matched.")
        browser.close()
        sys.exit(1)

    count = items.count()
    print(f"\nFound {count} option(s) via {item_sel}:\n")
    print(f"{'value':40}  label")
    print("-" * 80)
    for i in range(count):
        el = items.nth(i)
        value = el.get_attribute("value") or ""
        # Some UIs store the value on a data-* attr instead.
        if not value:
            value = el.get_attribute("data-value") or ""
        label = el.inner_text().strip()
        print(f"{repr(value):40}  {repr(label)}")

    print("\nDone.")
    browser.close()
