# Database Schema (Supabase)

### 1. Table: `snapshots`
Stores every individual scrape result. 
- `id`: uuid (PK)
- `admin_source`: text (e.g., 'Admin_A', 'Admin_B')
- `seller_id`: text
- `store_url`: text
- `email`: text
- `platforms`: jsonb (e.g., ["Shopify", "Shein"])
- `order_count`: int
- `product_count`: int
- `installed_on`: timestamp
- `scraped_at`: timestamp (default: now())

### 2. Table: `uninstalls_log`
- `id`: uuid (PK)
- `admin_source`: text
- `user_id`: text
- `email`: text
- `platform`: text
- `uninstalled_at`: timestamp
- `username`: text

### 3. Table: `metrics_daily`
Computed aggregates for the dashboard.
- `date`: date (PK)
- `admin_source`: text (PK)
- `total_active_sellers`: int
- `new_installs`: int
- `new_uninstalls`: int