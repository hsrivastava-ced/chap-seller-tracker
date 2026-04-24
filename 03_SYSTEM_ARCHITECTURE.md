# Detailed System Architecture: cHAP Multi-App Engine

## 1. Multi-App Configuration (`config.py`)
- **Source:** Parses `.env` for entries prefixed with `APP_`.
- **Structure:** Generates a `List[Dict]` containing:
  - `app_id`: (e.g., 'shopify_temu', 'shein', 'shopify_temu_eu')
  - `credentials`: {email, password}
  - `target_url`: The shared cHAP login portal.

## 2. Scraper Engine Logic (`scraper.py`)
Developed using **Playwright Python (Async or Sync)**. 
### A. The Login Flow (The Selector Logic)
1. **Dropdown Interaction:** Locate the `select` element or custom dropdown div for "Integration Apps".
2. **Dynamic Selection:** Use `page.select_option()` or `click()` based on the `app_id`.
3. **Identity Injection:** Fill fields using CSS selectors (e.g., `input[type="email"]`, `input[type="password"]`).
4. **Auth Persistence:** Check for successful navigation to the Dashboard to confirm login.

### B. Grid Manipulation & Extraction
1. **Column Visibility:** Trigger the "Customize Grid" modal. Check the state of "Order Count" and "Product Count". If unchecked, click to enable.
2. **Table Parsing:** - Extract rows from the `Sellers` table.
   - Map columns: Seller ID, Store URL, Email, Platforms (nested list), Installed Date.
3. **The Pagination Algorithm:**
   - Detect "Next" button selector.
   - **Condition:** `While button is not disabled AND button is visible`: 
     - Scrape current rows -> Click Next -> Wait for network idle/table reload.

### C. Context Switching
- After scraping one app, the scraper must **Logout** or **Clear Cookies** to reset the login page for the next app in the loop.

## 3. Database Layer (`db.py`)
- **Client:** Supabase-py.
- **Upsert Strategy:** Use a Conflict Target of `(seller_id, admin_source)` to ensure we update metrics for existing sellers instead of creating duplicates.

## 4. Orchestrator (`main.py`)
- Manages the loop. 
- **Error Recovery:** If `shein` fails, it must catch the error, log it to `alerts_log`, and continue to `shopify_temu_eu`.