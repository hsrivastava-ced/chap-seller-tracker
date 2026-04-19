# Database Schema (Supabase)

### 1. Table: `snapshots`
Stores every successful scrape.
- `id`: uuid (primary key)
- `created_at`: timestamp with timezone
- `raw_data`: jsonb (the scraped payload)
- `status`: text (success/fail)

### 2. Table: `metrics`
Stores computed KPIs after Pandas processing.
- `id`: uuid
- `timestamp`: timestamp
- `metric_name`: text
- `value`: float
- `delta_from_previous`: float

### 3. Table: `alerts_log`
- `id`: uuid
- `triggered_at`: timestamp
- `alert_type`: text
- `message`: text