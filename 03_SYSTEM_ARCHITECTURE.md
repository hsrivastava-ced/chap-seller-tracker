# System Architecture & Data Flow

## Scraper Logic (scraper.py)
1. **Login:** Access CedCommerce admin.
2. **Grid Prep:** - Click "Customize Grid" dropdown.
   - Ensure "Order Count" and "Product Count" are checked/visible.
3. **Data Extraction:**
   - Scrape "Seller List" table (Pagination handling required).
   - Switch to "Uninstalls" tab.
   - Scrape latest uninstall rows.
4. **Processing:** Pass raw data to `analytics.py`.

## Analytics Logic (analytics.py)
- Compare current `seller_id` list against Supabase.
- **New Seller:** If ID is in scrape but not DB.
- **New Uninstall:** If record appears in 'Uninstalls' tab with a timestamp newer than the last check.