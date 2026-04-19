# 04_BUILD_ROADMAP: The cHAP Multi-App Pipeline

## 🟢 Phase 1: Local Environment & Schema Init
- [ ] **Python Venv:** Set up environment and install `playwright`, `supabase`, `pandas`, `python-dotenv`.
- [ ] **Browser Engine:** Run `playwright install chromium`.
- [ ] **Supabase DDL:** Execute SQL to create `snapshots`, `uninstalls_log`, and `metrics_daily` with proper data types.
- [ ] **Secrets Management:** Populate `.env` with specific App IDs and Supabase credentials.

## 🟡 Phase 2: The Scraper Engine (The Core Challenge)
- [ ] **Step 2.1: Login Module:** - Implement dropdown selection logic. 
    - Handle potential "Wait for Selector" timeouts.
- [ ] **Step 2.2: Grid Customizer:** - Script the click sequence for "Customize Grid".
    - Verification: Ensure "Order Count" appears in the DOM before scraping.
- [ ] **Step 2.3: Pagination & Scraping:** - Write the loop to iterate through pages.
    - Logic to scrape the "Platforms" column (handling multiple icons like Shopify + Shein in one cell).
- [ ] **Step 2.4: Uninstall Scraping:** - Navigate to the `Uninstalls` URL path.
    - Parse the timestamp and map it back to the correct `admin_source`.

## 🔵 Phase 3: Data Integrity & Analytics
- [ ] **Normalization:** - Clean "Store URL" strings (remove trailing slashes).
    - Standardize all dates to ISO-8601.
- [ ] **Delta Detection (Pandas):** - Compare `latest_scrape` vs `previous_scrape` from Supabase.
    - Identify `New Installs` (ID exists in New but not Old).
    - Identify `Churned Sellers` (ID exists in Old but not New).

## 🟠 Phase 4: Scheduling & Error Logging
- [ ] **Scheduler:** Set `scheduler.py` to trigger every 5-10 minutes.
- [ ] **Log Engine:** If the "Next" button fails or Login fails, write the error message and a screenshot path to a local `logs/` folder.

## 🔴 Phase 5: Streamlit Dashboard (The UI)
- [ ] **Multitenant Filtering:** Sidebar dropdown to switch between the 3 Apps.
- [ ] **KPI Cards:** Display "Total Active", "Total Uninstalls", and "Growth %".
- [ ] **Historical Trend:** Line chart showing total sellers over the last 30 days.