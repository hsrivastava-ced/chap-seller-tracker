# cHAP Seller Tracker — Analytics Spec

Stakeholder-facing documentation of every metric computed across the
cHAP Seller Tracker pipeline. This is the canonical reference for
"what does that number on the dashboard mean?". Pair with
`analytics.py` (per-run deltas) and `analytics_advanced.py`
(longitudinal trends).

---

## Source of truth

Three integrations are scraped from the CedCommerce cHAP admin panel:

| App name         | Admin panel app              |
|------------------|------------------------------|
| `shopify_temu`   | Shopify × Temu (US)          |
| `shein`          | Shopify × Shein              |
| `shopify_temu_eu`| Shopify × Temu (EU region)   |

Each run produces two per-app datasets:

- **Active sellers** (`data` key in `run.json`) — snapshot of every
  seller currently installed on the app at scrape time.
- **Uninstall log** (`uninstalls_data` key) — historical log of
  uninstall events the admin panel still retains.

All dates are normalised to ISO (`YYYY-MM-DD` or
`YYYY-MM-DDTHH:MM:SS`) before analytics run — see `normalize.py`.
For display, `analytics_advanced.fmt_*` helpers convert to
stakeholder-friendly forms ("Apr 2026", "Q2 2026", "18 Apr 2026").

---

## Data shape reference

### Active-seller row

| Field | Type | Example | Coverage |
|---|---|---|---|
| `seller_id` | string | `69cd21be45afb6f71b0435b2` | 100% |
| `store_url` | string | `testshop.myshopify.com` | 100% |
| `username` | string | `mshukla@threecolts.com` | 100% |
| `email` | string | `mshukla@threecolts.com` | 100% |
| `platforms` | string (space-joined) | `Shopify Temu` | 100% |
| `installed_on` | date | `2026-04-01` | 100% |
| `app_type` | string | `Custom App`, `Sales Channel App` | `shein` only |
| `source_country` | string | `United States` | 100% |
| `order_count` | int | `800` | 100% |
| `product_count` | int | `800` | 100% |
| `failed_order_count` | int | `12` | varies |
| `steps_completed` | string (numeric) | `"0"`, `"1"`, … | `shopify_temu_eu` only |
| `plan` | string | `Starter`, `Elite`, `N/A` | `shopify_temu_eu` only |
| `last_sync` | datetime | `2026-04-15T10:56:04` | varies |
| `webhooks` | string | `View` | 100% |
| `action` | string | `` | rarely populated |

### Uninstall-log row

| Field | Type | Example |
|---|---|---|
| `seller_id` | string | `69a7cd9fa1f9def86008afe2` |
| `email` | string | `zip627@gmail.com` |
| `username` | string | `zip627@gmail.com` |
| `platform` | string | `Shopify`, `Temu`, `Shein`, `Prestashop` |
| `uninstalled_on` | datetime | `2026-04-15T10:56:04` |
| `shops_raw` | string | `Shopify 2026-04-15 10:56:04 Temu 2026-04-15 11:02:06` |

---

## Metric catalog

Each metric below lists: **what it measures**, **formula / definition**,
**source fields**, and any **caveats** stakeholders should be aware of.

### 1. Per-run delta KPIs (from `analytics.py`)

Shown at the top of the dashboard — point-in-time comparison of the
current run vs. the prior run.

#### 1.1 Total Active

- **Formula:** `|current_sellers|` (count of seller rows returned by
  the scraper on the latest run).
- **Δ vs previous:** `|current| - |previous|`.
- **Grain:** per app + cross-app total.

#### 1.2 New Installs

- **Formula:** `|current_seller_ids \ previous_seller_ids|`.
- **Definition:** sellers whose `seller_id` appears in the current
  run but not in the prior run. First-run edge case: every seller is
  counted as new.

#### 1.3 Churned Sellers

- **Formula:** `|previous_seller_ids \ current_seller_ids|`.
- **Definition:** sellers present in the prior run but absent from
  the current run. *Not the same as "New Uninstalls"* — a seller can
  disappear from the active list without a new row in the uninstall
  log (e.g. if the scrape missed a page — we defend against this, but
  churn is the user-facing metric whether or not the uninstall row
  exists yet).

#### 1.4 New Uninstalls

- **Formula:** `|current_uninstall_keys \ previous_uninstall_keys|`
  where each `key = (seller_id, platform, uninstalled_on)`.
- **Definition:** uninstall events that appear in the current
  uninstall log but not in the previous one. The 3-tuple key matters:
  the same seller can uninstall from Shopify *and* Temu at different
  times, and both events are distinct.

#### 1.5 Churn Rate

- **Formula:** `churned_sellers / previous_sellers` (per app and overall).
- **Caveat:** on the first run `previous_sellers == 0`, so we emit
  `0.0` to avoid a divide-by-zero and a misleading `100%` on day one.

#### 1.6 Net Growth

- **Formula:** `new_installs - churned_sellers`.
- **Caveat:** this is a *seller-count* net, not a *revenue* net.
  Churn of a high-order-count seller and gain of a zero-order seller
  both show as `+0`.

#### 1.7 Platform Split (per app)

- **Formula:** grouping the currently-active sellers by the `platforms`
  field.
- **Normalisation:** runs of whitespace collapse to single space
  (`"Shopify  Temu"` and `"Shopify Temu"` group together), but order
  is preserved (`"Shopify Temu"` vs `"Temu Shopify"` remain distinct
  — usually one is a data-entry variant we flag in QA).

---

### 2. Time-series metrics (from `analytics_advanced.py`)

Each of monthly / quarterly / yearly is computed three ways: per app,
and a combined `all_apps` roll-up.

> **Install-cohort caveat** — the "Installs per period" series is
> computed from the **currently-active** seller list, bucketed by each
> seller's `installed_on` date. Sellers who installed in a given
> period AND later uninstalled are *not* in the active list and so
> are not counted in that period's installs. For very recent months
> this is close to accurate (almost everyone who installed this week
> is still installed). For months a year or more in the past it is a
> **lower bound**. True historical installs would require either a
> separate "installs log" endpoint (the admin panel doesn't expose
> one we can scrape) or long-term snapshot differencing (needs months
> of snapshot history — we currently have ~1 week).

#### 2.1 Monthly Installs

- **Formula:**
  `installs_by_month[app][m] = |{ s ∈ active_sellers[app] : month(s.installed_on) = m }|`
- **Output grain:** gap-filled monthly series from the earliest to
  latest observed install month across the whole dataset.
- **Display:** `fmt_month` → `"Apr 2026"`.

#### 2.2 Monthly Uninstalls

- **Formula:**
  `uninstalls_by_month[app][m] = |{ u ∈ uninstall_log[app] : month(u.uninstalled_on) = m }|`
- Unlike installs, this is *not* a lower bound — the uninstall log is
  the system of record for uninstall events.

#### 2.3 Quarterly Installs / Uninstalls

- **Formula:** same as monthly, with the bucketing function
  `quarter(d) = f"{d.year}-Q{((d.month-1)//3)+1}"`.
- **Display:** `fmt_quarter` → `"Q2 2026"`.

#### 2.4 Yearly Installs / Uninstalls

- **Formula:** same as monthly, bucketing by calendar year.
- **Display:** `"2026"` (identity).

#### 2.5 Month-on-Month Growth Rate (MoM %)

- **Formula:**
  `growth[m] = (installs[m] - installs[m-1]) / installs[m-1] * 100`
- **Null handling:** if `installs[m-1] == 0` we emit `null` (rendered
  as `—` in the table) instead of `∞` or `100%`. This covers the
  first observed month and any zero-install hiatus.
- **Same formula for QoQ and YoY**, substituting the period.

#### 2.6 Cumulative Active Sellers

- **Formula:** running total of monthly installs — `Σ installs[≤m]`.
- **Interpretation:** for an `all_apps` view, this traces the "active
  base" growth curve.
- **Caveat:** same lower-bound caveat as §2.1 — cumulative is
  cumulative-of-currently-active, not cumulative-of-ever-installed.

#### 2.7 Install Velocity (30-day rolling)

- **Formula:**
  `velocity[D] = |{ s : D - window < s.installed_on ≤ D }|`
  where the default `window = 30 days`, `lookback = 90 days`, giving
  a daily-granularity series for the last quarter.
- **Use:** smooths out weekly wobble (Mondays pick up delayed weekend
  installs) so stakeholders can see velocity trend, not calendar
  artefacts.

---

### 3. Dimensional breakdowns (active sellers)

Each returns `breakdown[app][label] = count` and a `coverage[app]`
percentage — "what fraction of rows actually have this field populated".
Useful so the dashboard can mark low-coverage apps rather than showing
empty charts.

#### 3.1 Plan Distribution

- **Source field:** `plan`.
- **Coverage:** `shopify_temu_eu` = 100%; `shopify_temu` and `shein` = 0%
  (the column isn't surfaced in those admin panels — the cell is empty).
- **Values seen (as of 2026-04-18):** `N/A`, `Starter`, `Regular`,
  `Premium`, `Elite`, `Elite Yearly`, `Custom Yearly`, `Standard`.

#### 3.2 Framework / Platform Combo

- **Source field:** `platforms`.
- **Formula:** count of currently-active sellers grouped by the
  space-separated platforms string.
- **Current shape:** `shopify_temu` → `"Shopify Temu"` for ~99% (one
  outlier `"Temu Shopify"` from a data-entry variant); `shein` →
  100% `"Shopify Shein"`; `shopify_temu_eu` → 100% `"Shopify Temu"`.

#### 3.3 Source Country

- **Source field:** `source_country`.
- **Top countries (all apps, 2026-04-18):** United States (398),
  United Kingdom (33), Canada (6), Germany (7), Australia (3), Spain (8).

#### 3.4 App Type

- **Source field:** `app_type`.
- **Coverage:** only `shein` surfaces this (`Custom App` ≈ 99.4%,
  `Sales Channel App` ≈ 0.6%). Retained in the spec because future
  Shopify API migrations may expose it on other integrations.

#### 3.5 Onboarding Steps by Install Month

- **Source fields:** `steps_completed`, `installed_on`.
- **Formula:**
  `matrix[app][install_month][step] = |{ s : month(s.installed_on)=install_month ∧ s.steps_completed=step }|`
- **Coverage:** `shopify_temu_eu` = 100% (all currently `"0"`);
  others = 0%.
- **Use:** track whether a given install cohort is progressing
  through onboarding — "of the 7 sellers who installed in Sep 2025,
  how many have completed which steps today?"

---

### 4. Order activity segmentation

Bucketed counts of currently-active sellers by lifetime order count
(not this-period orders — the admin panel doesn't distinguish).

- **Buckets:**
  - `0 orders`
  - `1-10 orders`
  - `11-100 orders`
  - `101-1k orders`
  - `1k+ orders`
- **Additional rollup fields:**
  - `zero_order_sellers` — count of sellers with exactly 0 orders.
  - `active_sellers` = `total_sellers - zero_order_sellers` (≥1 order).
  - `sellers_with_failed_orders` — count of sellers with
    `failed_order_count > 0`.
  - `total_orders` = Σ `order_count` across all sellers.
  - `total_failed_orders` = Σ `failed_order_count`.

**As of 2026-04-18 (all apps combined):**

| Metric | Value |
|---|---:|
| Total sellers | 474 |
| Active (≥1 order) | 323 |
| Zero-order | 151 |
| With failed orders | 24 |
| Total lifetime orders | 780,922 |

---

### 5. Uninstall platform split

- **Source field:** `platform` on each uninstall-log row.
- **Formula:** count of uninstall events grouped by platform, per app.
- **Why it matters:** The `shopify_temu_eu` uninstall log reveals a
  Prestashop platform (22 events) that isn't visible in the active
  seller list — historical evidence the EU integration supported
  Prestashop sellers.
- **Current distribution (2026-04-18, all apps):** Shopify 280,
  Shein 141, Temu 116, Prestashop 22.

---

## Formula glossary

| Symbol | Meaning |
|---|---|
| `|X|` | Cardinality (count) of set `X`. |
| `X \ Y` | Set difference — elements of `X` not in `Y`. |
| `X ∩ Y` | Set intersection. |
| `Σ` | Summation (over sellers, events, or periods depending on context). |
| `month(d)` | Calendar-month bucket of date `d`, as `YYYY-MM`. |
| `quarter(d)` | Calendar-quarter bucket, as `YYYY-Q{1..4}`. |
| Δ | "Change" — usually `current - previous` or `(current-previous)/previous`. |

---

## Data freshness & reconciliation

- **Scraper cadence:** scheduled runs (via `scheduler.py`) fire every
  10 minutes by default. Each run writes a run snapshot to
  `results/history/<stamp>/run.json` and — when Supabase credentials
  are configured — an immutable row per (app, kind) to the `snapshots`
  table plus per-app + total metric rows to the `metrics` table.
- **Source of truth for trends:** the uninstall log persists
  historically in the admin panel, so quarterly / yearly uninstall
  figures are stable across runs. The install series is re-computed
  every run from the then-current active list, so figures for recent
  months will tick up as new installs come in.
- **Re-running the analytics on a replay:** `pipeline.py --replay
  <stamp>` re-runs every metric on a stored snapshot without
  re-scraping, which is useful for backfilling the stakeholder report
  history.

---

## Known gaps / roadmap candidates

- **True historical installs** — need either an "installs log" scrape
  endpoint or 6+ months of retained snapshots. Planning to accumulate
  the snapshot differencing approach over the next several months.
- **Plan / step coverage for `shopify_temu` and `shein`** — blocked on
  whether the admin panel surfaces those columns for those apps.
  If not, the dashboard will keep badging them 0% coverage.
- **Cohort retention (install-month × still-active-today %)** — blocked
  on uninstall rows not carrying `installed_on`. Reachable via join on
  `seller_id` to the latest active snapshot where still present.
- **Revenue-weighted churn** — need the admin panel to expose a
  per-seller lifetime-revenue field; `order_count` is our best proxy today.

---

*Last updated: 2026-04-19. If you add a metric to `analytics.py` or
`analytics_advanced.py`, update this file in the same commit.*
