"""
Stakeholder-facing analytics for the cHAP Seller Tracker.

This module sits alongside `analytics.py`. Where `analytics.py` answers
"what changed between the last two runs?", `analytics_advanced.py`
answers "what do our monthly / quarterly / yearly trends look like,
per app and in aggregate?"

It is pure (no I/O, no network, no Playwright), so the dashboard, CLI
report generators, and tests all call the same functions. The inputs
are the same shape the scraper + pipeline already hand around:

    sellers_by_app   : {app_name: [seller_row, ...]}
    uninstalls_by_app: {app_name: [uninstall_row, ...]}

Both are expected to have been passed through `normalize.normalize_run_data`
first, so dates are ISO-shaped strings ("YYYY-MM-DD" or
"YYYY-MM-DDTHH:MM:SS") rather than the raw UK-locale "DD/MM/YYYY" the
admin panel renders.

--------------------------------------------------------------------
Design notes — read this before wiring new metrics into the dashboard
--------------------------------------------------------------------

1. **Install-month breakdown is lower-bound for historical months.**
   We group the *currently-active* seller list by `installed_on` month.
   A seller who installed in Jan 2025 and uninstalled in Mar 2025 is
   therefore not counted in the Jan 2025 install bucket — they live in
   the uninstalls log only. The stakeholder spec doc calls this out.
   True historical installs would require either the admin panel's
   own "installs log" (we don't scrape one) or many months of snapshot
   diffs (we only have a handful). The current approach is still
   useful for recent months (where most installs are still active)
   and for cohort size trending over time.

2. **`plan` and `steps_completed` are app-scoped.** At time of writing
   only `shopify_temu_eu` surfaces those columns. We keep the column
   name generic and the "not available for this app" story is handled
   in the coverage report: every dimensional breakdown returns a
   `coverage_pct` field so the dashboard can dim or warn appropriately.

3. **Date formatting is centralised.** Stakeholder output should say
   "Apr 2026" not "2026-04". Every month/quarter/year key produced here
   has a canonical machine form ("2026-04") and a `fmt_*` helper to
   pretty-print it. That keeps sort order correct (lexicographic still
   works) while giving the UI a clean label.
"""

from __future__ import annotations

import calendar
import logging
from collections import Counter, OrderedDict, defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable

# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------

_ALL_APPS = ("shopify_temu", "shein", "shopify_temu_eu")
_COMBINED_LABEL = "all_apps"

# Short month names for pretty-printing.
_MONTH_NAMES = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

# Stakeholder-facing display names. The scraper's internal app keys
# (`shopify_temu`, `shein`, `shopify_temu_eu`) are infrastructure
# identifiers; stakeholders see the app by its go-to-market brand. The
# dashboard translates on the fly, charts, tables, and PDFs all use
# these labels.
DISPLAY_NAMES: dict[str, str] = {
    "shopify_temu": "TEMU US",
    "shopify_temu_eu": "TEMU EU",
    "shein": "SHEIN",
    "all_apps": "All Apps",
}


def display_name(app_key: str) -> str:
    """Translate internal app key to stakeholder-facing label.
    Unknown keys pass through unchanged — we'd rather show the raw key
    than lie."""
    return DISPLAY_NAMES.get(app_key, app_key)


# Test stores we should NEVER include in stakeholder numbers. These are
# internal QA sellers, typically created via a corporate email. Counting
# them inflates install / churn metrics and confuses the numbers users
# see in their own admin panel.
TEST_EMAIL_DOMAINS: tuple[str, ...] = (
    "threecolts.com",
    "cedcommerce.com",
)


def _is_test_store(row: dict) -> bool:
    """Return True if the seller/uninstall row belongs to an internal
    test store. Checks the email's domain (case-insensitive). Missing
    or malformed emails → False (we can't prove it's a test, so we keep
    it)."""
    email = row.get("email")
    if not isinstance(email, str):
        return False
    e = email.strip().lower()
    if "@" not in e:
        return False
    domain = e.rsplit("@", 1)[-1]
    return any(domain == d or domain.endswith("." + d) for d in TEST_EMAIL_DOMAINS)


def exclude_test_stores(
    rows_by_app: dict[str, list[dict]],
) -> dict[str, list[dict]]:
    """Return a shallow-filtered copy with internal-test-store rows
    dropped. Safe to apply to both seller and uninstall dicts — both
    carry an `email` column."""
    if not rows_by_app:
        return {}
    out: dict[str, list[dict]] = {}
    dropped = 0
    for app, rows in rows_by_app.items():
        kept: list[dict] = []
        for r in rows or []:
            if _is_test_store(r):
                dropped += 1
                continue
            kept.append(r)
        out[app] = kept
    if dropped:
        logging.info(f"🧹 exclude_test_stores: dropped {dropped} test-store rows")
    return out

# Order-count buckets for activity segmentation. The boundaries are
# picked so the "zero" bucket is always its own label — that's the
# segment stakeholders ask about most ("how many sellers have placed
# ZERO orders since install?").
_ORDER_BUCKETS = (
    (0, 0, "0 orders"),
    (1, 10, "1-10 orders"),
    (11, 100, "11-100 orders"),
    (101, 1_000, "101-1k orders"),
    (1_001, None, "1k+ orders"),
)


# ---------------------------------------------------------------------
# Date parsing & formatting
# ---------------------------------------------------------------------

def _parse_iso_date(value: Any) -> date | None:
    """Best-effort parser for normalized date strings. Accepts:
      - "YYYY-MM-DD"
      - "YYYY-MM-DDTHH:MM:SS"
      - "YYYY-MM-DD HH:MM:SS"
      - already-parsed `date` / `datetime`
      - anything else → None (caller decides what to do with it)

    We deliberately don't re-run the UK-locale DD/MM/YYYY shim here —
    inputs should already be normalized. If a raw "03/04/2026" slips
    through we'd mis-parse it as "march 4", so we want the bug to
    surface as `None` rather than a silent wrong-date.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Try a short list of accepted formats. We pre-truncate to the
    # format's expected length so a "2026-04-18T19:38:19.123" string
    # still matches the '%Y-%m-%dT%H:%M:%S' variant.
    for fmt, length in (
        ("%Y-%m-%dT%H:%M:%S", 19),
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%d", 10),
    ):
        try:
            return datetime.strptime(s[:length], fmt).date()
        except ValueError:
            continue
    # Last try — fromisoformat handles micros and "+00:00"
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _month_key(d: date) -> str:
    """Sort-stable month key: 'YYYY-MM'."""
    return f"{d.year:04d}-{d.month:02d}"


def _quarter_key(d: date) -> str:
    """Sort-stable quarter key: 'YYYY-Qn' where n ∈ {1,2,3,4}."""
    q = (d.month - 1) // 3 + 1
    return f"{d.year:04d}-Q{q}"


def _year_key(d: date) -> str:
    return f"{d.year:04d}"


def fmt_month(key: str) -> str:
    """'2026-04' → 'Apr 2026'. Invalid input passes through untouched —
    we'd rather show a raw key than fail a whole report."""
    try:
        y, m = key.split("-")
        return f"{_MONTH_NAMES[int(m) - 1]} {int(y)}"
    except (ValueError, IndexError):
        return key


_MONTH_NAMES = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def fmt_month_short(key: str) -> str:
    """'2026-04' → 'Apr 2026'. Previously returned '04/26' which
    stakeholders flagged as ambiguous — was it month 4 of year 2026 or
    April of 1926? Full month name + 4-digit year removes that
    guesswork at the cost of a slightly wider tick label. Plotly
    handles the extra width by rotating ticks on tight axes.
    Invalid input passes through untouched."""
    try:
        y, m = key.split("-")
        month_idx = int(m) - 1
        if 0 <= month_idx < 12:
            return f"{_MONTH_NAMES[month_idx]} {int(y):04d}"
        return key
    except (ValueError, IndexError):
        return key


def fmt_quarter_short(key: str) -> str:
    """'2026-Q2' → 'Q2/26'. Compact variant for the mm/yy-style
    sidebar. Falls back to the machine key on malformed input."""
    try:
        y, q = key.split("-")
        return f"{q}/{int(y) % 100:02d}"
    except ValueError:
        return key


def fmt_quarter(key: str) -> str:
    """'2026-Q2' → 'Q2 2026'."""
    try:
        y, q = key.split("-")
        return f"{q} {int(y)}"
    except ValueError:
        return key


def fmt_year(key: str) -> str:
    """'2026' → '2026' (identity, but exposed for symmetry so the
    dashboard can use a single formatter table)."""
    return key


def fmt_date_long(value: Any) -> str:
    """'2026-04-18' → '18 Apr 2026'. Used wherever we render a single
    date timestamp for a human audience. Empty/unparseable → ''."""
    d = _parse_iso_date(value)
    if d is None:
        return ""
    return f"{d.day:02d} {_MONTH_NAMES[d.month - 1]} {d.year:04d}"


# ---------------------------------------------------------------------
# Period enumeration — fill gaps so charts don't jump over empty months
# ---------------------------------------------------------------------

def _month_range(start: date, end: date) -> list[str]:
    """All 'YYYY-MM' keys from start-month through end-month inclusive.

    Filling zero-count months explicitly (rather than letting the chart
    library skip them) matters because otherwise the dashboard draws
    a straight line from March to May when April had zero installs,
    which misleads stakeholders."""
    if start > end:
        return []
    y, m = start.year, start.month
    keys: list[str] = []
    while (y, m) <= (end.year, end.month):
        keys.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return keys


def _quarter_range(start: date, end: date) -> list[str]:
    """All 'YYYY-Qn' keys between start and end inclusive."""
    qs, qe = _quarter_key(start), _quarter_key(end)
    out: list[str] = []
    y, q = int(qs[:4]), int(qs[-1])
    ey, eq = int(qe[:4]), int(qe[-1])
    while (y, q) <= (ey, eq):
        out.append(f"{y:04d}-Q{q}")
        q += 1
        if q > 4:
            q = 1
            y += 1
    return out


def _year_range(start: date, end: date) -> list[str]:
    return [f"{y:04d}" for y in range(start.year, end.year + 1)]


# ---------------------------------------------------------------------
# Time-series: installs & uninstalls by month / quarter / year
# ---------------------------------------------------------------------

def _timeseries_counts(
    rows: Iterable[dict],
    date_field: str,
    period_fn,
    *,
    dedup_field: str | None = None,
) -> dict[str, int]:
    """Count rows bucketed by the period derived from `date_field`.

    Rows whose date field doesn't parse are silently skipped — they're
    already logged by `normalize.normalize_date` on the way in, so we
    don't repeat the warning here.

    `dedup_field` — if set, count each distinct value of this field only
    once per (period, value) pair. Used for uninstalls: the scraper emits
    one row per (seller_id, platform) because a seller can uninstall
    Shopify + Shein at different timestamps. Stakeholders want to see
    "one seller left us in March" as one event, not two.
    Passing ``dedup_field="seller_id"`` collapses both rows into a
    single March count, regardless of how many platforms were removed.
    """
    buckets: Counter = Counter()
    if dedup_field:
        seen: dict[str, set] = defaultdict(set)
        for r in rows or []:
            d = _parse_iso_date(r.get(date_field))
            if d is None:
                continue
            key = (r.get(dedup_field) or "").strip()
            if not key:
                # No id to dedupe by — still count (better to over-report
                # than silently drop). Rare in practice; all uninstall
                # rows carry a seller_id.
                buckets[period_fn(d)] += 1
                continue
            period = period_fn(d)
            if key in seen[period]:
                continue
            seen[period].add(key)
            buckets[period] += 1
    else:
        for r in rows or []:
            d = _parse_iso_date(r.get(date_field))
            if d is None:
                continue
            buckets[period_fn(d)] += 1
    return dict(buckets)


def timeseries_by_period(
    *,
    sellers_by_app: dict[str, list[dict]],
    uninstalls_by_app: dict[str, list[dict]],
    period: str = "month",
    include_combined: bool = True,
) -> dict[str, Any]:
    """Build install + uninstall counts per period, per app + combined.

    Returns:
        {
          "period": "month" | "quarter" | "year",
          "periods": [list of keys, gap-filled, ascending],
          "installs":   {app: {period_key: n, ...}, ..., "all_apps": {...}},
          "uninstalls": {app: {period_key: n, ...}, ..., "all_apps": {...}},
        }

    If `include_combined` is False we skip the "all_apps" key — useful
    when the caller is going to roll-up manually (e.g. with its own
    filtering applied first).
    """
    period_fn = {
        "month": _month_key,
        "quarter": _quarter_key,
        "year": _year_key,
    }[period]
    range_fn = {
        "month": _month_range,
        "quarter": _quarter_range,
        "year": _year_range,
    }[period]

    installs_by_app: dict[str, dict[str, int]] = {}
    unins_by_app: dict[str, dict[str, int]] = {}

    for app, rows in (sellers_by_app or {}).items():
        installs_by_app[app] = _timeseries_counts(rows, "installed_on", period_fn)
    for app, rows in (uninstalls_by_app or {}).items():
        # Dedupe by seller_id so a seller who removed Shopify + Shein in
        # the same month counts as ONE uninstall event, not two. The raw
        # CSV emits one row per (seller, platform) for Supabase-upsert
        # ergonomics, but stakeholder-facing counts should be per-seller.
        unins_by_app[app] = _timeseries_counts(
            rows, "uninstalled_on", period_fn, dedup_field="seller_id"
        )

    # Determine the global time span so every series is gap-filled
    # consistently. If either dict is completely empty we skip filling.
    all_keys: set[str] = set()
    for d in (*installs_by_app.values(), *unins_by_app.values()):
        all_keys.update(d.keys())

    periods: list[str] = []
    if all_keys:
        # Build a dates list from keys so we can compute a start/end.
        # Month & quarter & year keys sort lexicographically, so min/max
        # string comparison gives the right endpoints for all three.
        start_key, end_key = min(all_keys), max(all_keys)
        if period == "month":
            start = date(int(start_key[:4]), int(start_key[5:7]), 1)
            end = date(int(end_key[:4]), int(end_key[5:7]), 1)
        elif period == "quarter":
            start = date(int(start_key[:4]), (int(start_key[-1]) - 1) * 3 + 1, 1)
            end = date(int(end_key[:4]), (int(end_key[-1]) - 1) * 3 + 1, 1)
        else:  # year
            start = date(int(start_key), 1, 1)
            end = date(int(end_key), 1, 1)
        periods = range_fn(start, end)

    def _densify(series: dict[str, int]) -> dict[str, int]:
        return OrderedDict((p, series.get(p, 0)) for p in periods)

    installs = {app: _densify(s) for app, s in installs_by_app.items()}
    uninstalls = {app: _densify(s) for app, s in unins_by_app.items()}

    if include_combined:
        combined_inst: Counter = Counter()
        combined_unin: Counter = Counter()
        for s in installs.values():
            combined_inst.update(s)
        for s in uninstalls.values():
            combined_unin.update(s)
        installs[_COMBINED_LABEL] = _densify(dict(combined_inst))
        uninstalls[_COMBINED_LABEL] = _densify(dict(combined_unin))

    return {
        "period": period,
        "periods": periods,
        "installs": installs,
        "uninstalls": uninstalls,
    }


# ---------------------------------------------------------------------
# Growth metrics (MoM/QoQ/YoY)
# ---------------------------------------------------------------------

def _pct_change(curr: float, prev: float) -> float | None:
    """Signed percentage change. Returns None when prev is 0 (divide-
    by-zero) — UI should render those as "—" or "N/A" rather than
    showing a synthetic 100%."""
    if prev == 0:
        return None
    return (curr - prev) / prev * 100.0


def compute_growth_rates(
    series_by_app: dict[str, dict[str, int]],
) -> dict[str, dict[str, float | None]]:
    """For each app's period→count mapping, produce period→pct-change
    (vs the immediately-previous period in the same series).

    Period ordering is taken from the series's own insertion order so
    the caller controls temporal alignment (gap-fill it yourself if you
    want consecutive periods — `timeseries_by_period` already does)."""
    out: dict[str, dict[str, float | None]] = {}
    for app, series in (series_by_app or {}).items():
        deltas: dict[str, float | None] = {}
        prev: int | None = None
        for key, value in series.items():
            deltas[key] = None if prev is None else _pct_change(value, prev)
            prev = value
        out[app] = deltas
    return out


def compute_cumulative(
    series_by_app: dict[str, dict[str, int]],
) -> dict[str, dict[str, int]]:
    """Running totals. Used for the cumulative-installs curve on the
    dashboard — shows the classic hockey-stick / plateau shape."""
    out: dict[str, dict[str, int]] = {}
    for app, series in (series_by_app or {}).items():
        running = 0
        cum: dict[str, int] = OrderedDict()
        for key, value in series.items():
            running += value
            cum[key] = running
        out[app] = cum
    return out


# ---------------------------------------------------------------------
# Dimensional breakdowns
# ---------------------------------------------------------------------

def _dim_breakdown(
    rows: list[dict],
    field: str,
    *,
    empty_label: str = "(not set)",
) -> tuple[dict[str, int], float]:
    """Return (label→count, coverage_pct) for a single dimension.

    coverage_pct = fraction of rows with a non-empty value in that
    field. Used by the dashboard to badge the chart "28% coverage"
    when we know only a subset of apps populates the column."""
    counts: Counter = Counter()
    covered = 0
    total = 0
    for r in rows or []:
        total += 1
        v = r.get(field)
        if isinstance(v, str):
            v = v.strip()
        if v in (None, "", "(empty)"):
            counts[empty_label] += 1
        else:
            counts[str(v)] += 1
            covered += 1
    pct = (covered / total * 100.0) if total else 0.0
    return dict(counts), pct


def dimensional_breakdown(
    rows_by_app: dict[str, list[dict]],
    field: str,
    *,
    include_combined: bool = True,
) -> dict[str, Any]:
    """Value distribution for `field` (e.g. 'plan', 'platforms', 'source_country',
    'steps_completed') per app + combined.

    Returns:
        {
          "field": "plan",
          "breakdown": {app: {label: n, ...}, ..., "all_apps": {...}},
          "coverage": {app: coverage_pct, ...},
        }
    """
    breakdown: dict[str, dict[str, int]] = {}
    coverage: dict[str, float] = {}
    for app, rows in (rows_by_app or {}).items():
        counts, pct = _dim_breakdown(rows, field)
        breakdown[app] = counts
        coverage[app] = pct

    if include_combined:
        combined: Counter = Counter()
        combined_rows: list[dict] = []
        for rows in (rows_by_app or {}).values():
            combined_rows.extend(rows)
        counts, pct = _dim_breakdown(combined_rows, field)
        breakdown[_COMBINED_LABEL] = counts
        coverage[_COMBINED_LABEL] = pct

    return {"field": field, "breakdown": breakdown, "coverage": coverage}


# ---------------------------------------------------------------------
# Step-wise data: how many sellers installed in a given month are on
# which onboarding step.
# ---------------------------------------------------------------------

def steps_by_install_month(
    sellers_by_app: dict[str, list[dict]],
    *,
    include_combined: bool = True,
) -> dict[str, Any]:
    """For each (app, install_month, steps_completed) produce a count.

    Result shape:
        {
          "steps_values": sorted list of distinct step values observed,
          "by_app": {
             app: {
                install_month_key: {step_value: n, ...}
             }
          },
          "coverage": {app: coverage_pct}
        }

    Coverage here means "fraction of sellers that have a non-empty
    steps_completed". For apps that don't surface the column we still
    emit rows keyed under "(not set)" so the dashboard can show the
    count split clearly.
    """
    by_app: dict[str, dict[str, dict[str, int]]] = {}
    coverage: dict[str, float] = {}
    steps_values: set[str] = set()

    def _process(app: str, rows: list[dict]) -> None:
        per_month: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        covered = 0
        total = 0
        for r in rows or []:
            total += 1
            d = _parse_iso_date(r.get("installed_on"))
            if d is None:
                continue
            mk = _month_key(d)
            s = r.get("steps_completed")
            if isinstance(s, str):
                s = s.strip()
            if s in (None, "", "(empty)"):
                label = "(not set)"
            else:
                label = str(s)
                covered += 1
            per_month[mk][label] += 1
            steps_values.add(label)
        by_app[app] = {k: dict(v) for k, v in per_month.items()}
        coverage[app] = (covered / total * 100.0) if total else 0.0

    for app, rows in (sellers_by_app or {}).items():
        _process(app, rows)

    if include_combined:
        combined_rows: list[dict] = []
        for rows in (sellers_by_app or {}).values():
            combined_rows.extend(rows)
        _process(_COMBINED_LABEL, combined_rows)

    return {
        "steps_values": sorted(steps_values),
        "by_app": by_app,
        "coverage": coverage,
    }


# ---------------------------------------------------------------------
# Order / engagement activity segmentation
# ---------------------------------------------------------------------

def _coerce_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            # Admin panel sometimes renders "1,234"
            return int(str(value).replace(",", ""))
        except ValueError:
            return default


def _bucket_order_count(n: int) -> str:
    for lo, hi, label in _ORDER_BUCKETS:
        if hi is None:
            if n >= lo:
                return label
        elif lo <= n <= hi:
            return label
    return "unknown"


def activity_segmentation(
    sellers_by_app: dict[str, list[dict]],
    *,
    include_combined: bool = True,
) -> dict[str, Any]:
    """Segment sellers by order_count bucket + count sellers with any
    failed orders. Helps stakeholders see "active revenue producers"
    vs "passive installs".

    Returns:
        {
          "buckets": [bucket labels in order],
          "by_app": {
              app: {
                 "order_buckets": {label: n},
                 "zero_order_sellers": n,
                 "sellers_with_failed_orders": n,
                 "active_sellers": n,         # >=1 order
                 "total_orders": int,         # sum of order_count
                 "total_failed_orders": int,  # sum of failed_order_count
              }
          }
        }
    """
    bucket_labels = [lbl for _, _, lbl in _ORDER_BUCKETS]

    def _process(rows: list[dict]) -> dict[str, Any]:
        bucket_counts: Counter = Counter()
        zero = 0
        with_failed = 0
        total_orders = 0
        total_failed = 0
        for r in rows or []:
            o = _coerce_int(r.get("order_count"), 0)
            f = _coerce_int(r.get("failed_order_count"), 0)
            bucket_counts[_bucket_order_count(o)] += 1
            if o == 0:
                zero += 1
            if f > 0:
                with_failed += 1
            total_orders += o
            total_failed += f
        # Ensure every bucket is present (even zero) so the UI gets a
        # clean table.
        ordered = {lbl: bucket_counts.get(lbl, 0) for lbl in bucket_labels}
        total = len(rows or [])
        return {
            "order_buckets": ordered,
            "zero_order_sellers": zero,
            "sellers_with_failed_orders": with_failed,
            "active_sellers": total - zero,
            "total_orders": total_orders,
            "total_failed_orders": total_failed,
            "total_sellers": total,
        }

    by_app: dict[str, dict[str, Any]] = {}
    for app, rows in (sellers_by_app or {}).items():
        by_app[app] = _process(rows)

    if include_combined:
        combined_rows: list[dict] = []
        for rows in (sellers_by_app or {}).values():
            combined_rows.extend(rows)
        by_app[_COMBINED_LABEL] = _process(combined_rows)

    return {"buckets": bucket_labels, "by_app": by_app}


# ---------------------------------------------------------------------
# Install velocity: rolling 30-day install count over the last N days
# ---------------------------------------------------------------------

def install_velocity(
    sellers_by_app: dict[str, list[dict]],
    *,
    asof: date | None = None,
    lookback_days: int = 90,
    window_days: int = 30,
    include_combined: bool = True,
) -> dict[str, Any]:
    """Rolling-window install count for the last `lookback_days` days.

    For each day D in [asof - lookback, asof], we report the count of
    installs in (D - window_days, D]. Stakeholders typically want a
    30-day rolling view over the last quarter — the defaults reflect
    that. The daily granularity makes the line chart smooth enough to
    see velocity changes rather than calendar-month step artifacts.
    """
    asof = asof or date.today()
    start = asof - timedelta(days=lookback_days)
    # Precompute install date → count for fast window sums.

    def _counts(rows: list[dict]) -> dict[date, int]:
        c: Counter = Counter()
        for r in rows or []:
            d = _parse_iso_date(r.get("installed_on"))
            if d is not None:
                c[d] += 1
        return dict(c)

    per_app_daily: dict[str, dict[date, int]] = {}
    for app, rows in (sellers_by_app or {}).items():
        per_app_daily[app] = _counts(rows)

    if include_combined:
        combined_rows: list[dict] = []
        for rows in (sellers_by_app or {}).values():
            combined_rows.extend(rows)
        per_app_daily[_COMBINED_LABEL] = _counts(combined_rows)

    days = [start + timedelta(days=i) for i in range(lookback_days + 1)]
    window_delta = timedelta(days=window_days)

    out_series: dict[str, dict[str, int]] = {}
    for app, day_counts in per_app_daily.items():
        s: dict[str, int] = OrderedDict()
        for D in days:
            lo = D - window_delta
            total = 0
            # Small lookback, dict has at most a few thousand days — fine
            # to iterate. If this ever gets too slow, precompute a
            # prefix-sum array over sorted days.
            for d, n in day_counts.items():
                if lo < d <= D:
                    total += n
            s[D.isoformat()] = total
        out_series[app] = s

    return {
        "asof": asof.isoformat(),
        "lookback_days": lookback_days,
        "window_days": window_days,
        "days": [d.isoformat() for d in days],
        "series": out_series,
    }


# ---------------------------------------------------------------------
# Per-app uninstall platform split (Shopify vs Temu vs Shein vs Prestashop)
# ---------------------------------------------------------------------

def uninstall_platform_split(
    uninstalls_by_app: dict[str, list[dict]],
    *,
    include_combined: bool = True,
) -> dict[str, Any]:
    """Counts uninstalls grouped by the `platform` field.

    The `shopify_temu_eu` uninstall log surfaces a Prestashop platform
    that isn't visible in the active-sellers view — this split is how
    we surface that. Also useful for stakeholder questions like
    "of the Temu-app uninstalls, what fraction were from the Temu side
    vs the Shopify side?".
    """
    by_app: dict[str, dict[str, int]] = {}
    for app, rows in (uninstalls_by_app or {}).items():
        c: Counter = Counter()
        for r in rows or []:
            p = (r.get("platform") or "").strip() or "(empty)"
            c[p] += 1
        by_app[app] = dict(c)

    if include_combined:
        combined: Counter = Counter()
        for series in by_app.values():
            combined.update(series)
        by_app[_COMBINED_LABEL] = dict(combined)

    return {"by_app": by_app}


# ---------------------------------------------------------------------
# Paid vs Not-Paid derivation
# ---------------------------------------------------------------------
#
# Stakeholders don't care about the individual plan label — they want
# the revenue-relevant question: "is this seller paying us or not?".
# The mapping rule, confirmed by the user:
#
#   Paid     = any non-empty plan value EXCEPT "N/A" or similar nulls
#   Not Paid = empty plan, missing, or explicit "N/A" / "(not set)"
#
# The admin panel surfaces plan only on some apps. For apps where the
# column isn't present at all, every seller is Not Paid by definition
# (we can't prove they're paying). That's the correct fallback — it
# prevents us from silently inflating paid-count on apps with no data.

_NOT_PAID_TOKENS: tuple[str, ...] = (
    "",
    "n/a",
    "na",
    "none",
    "-",
    "(empty)",
    "(not set)",
)


def classify_paid(plan_value: Any) -> str:
    """Return 'Paid' or 'Not Paid' for a single plan cell value."""
    if plan_value is None:
        return "Not Paid"
    s = str(plan_value).strip().lower()
    if s in _NOT_PAID_TOKENS:
        return "Not Paid"
    return "Paid"


def paid_breakdown(
    rows_by_app: dict[str, list[dict]],
    *,
    include_combined: bool = True,
) -> dict[str, Any]:
    """Count Paid vs Not Paid sellers per app + combined.

    Returns:
        {
          "by_app": {app: {"Paid": n, "Not Paid": n}},
          "totals": {app: total_sellers},
        }
    """
    by_app: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}
    for app, rows in (rows_by_app or {}).items():
        c = Counter()
        for r in rows or []:
            c[classify_paid(r.get("plan"))] += 1
        by_app[app] = {"Paid": c.get("Paid", 0), "Not Paid": c.get("Not Paid", 0)}
        totals[app] = len(rows or [])

    if include_combined:
        c = Counter()
        total = 0
        for rows in (rows_by_app or {}).values():
            for r in rows or []:
                c[classify_paid(r.get("plan"))] += 1
                total += 1
        by_app[_COMBINED_LABEL] = {
            "Paid": c.get("Paid", 0),
            "Not Paid": c.get("Not Paid", 0),
        }
        totals[_COMBINED_LABEL] = total

    return {"by_app": by_app, "totals": totals}


# ---------------------------------------------------------------------
# Product activity segmentation — same bucketing idea as order activity,
# but counts products instead of orders. Useful for answering "how many
# sellers have actually populated their catalog vs sat at zero?".
# ---------------------------------------------------------------------

_PRODUCT_BUCKETS = (
    (0, 0, "0 products"),
    (1, 10, "1-10 products"),
    (11, 100, "11-100 products"),
    (101, 1_000, "101-1k products"),
    (1_001, None, "1k+ products"),
)


def _bucket_product_count(n: int) -> str:
    for lo, hi, label in _PRODUCT_BUCKETS:
        if hi is None:
            if n >= lo:
                return label
        elif lo <= n <= hi:
            return label
    return "unknown"


def product_activity_segmentation(
    sellers_by_app: dict[str, list[dict]],
    *,
    include_combined: bool = True,
) -> dict[str, Any]:
    """Segment sellers by product_count. Same shape as
    `activity_segmentation` so the dashboard can render both through
    the same widget."""
    bucket_labels = [lbl for _, _, lbl in _PRODUCT_BUCKETS]

    def _process(rows: list[dict]) -> dict[str, Any]:
        bucket_counts: Counter = Counter()
        zero = 0
        total_products = 0
        for r in rows or []:
            p = _coerce_int(r.get("product_count"), 0)
            bucket_counts[_bucket_product_count(p)] += 1
            if p == 0:
                zero += 1
            total_products += p
        ordered = {lbl: bucket_counts.get(lbl, 0) for lbl in bucket_labels}
        total = len(rows or [])
        return {
            "product_buckets": ordered,
            "zero_product_sellers": zero,
            "active_product_sellers": total - zero,
            "total_products": total_products,
            "total_sellers": total,
        }

    by_app: dict[str, dict[str, Any]] = {}
    for app, rows in (sellers_by_app or {}).items():
        by_app[app] = _process(rows)

    if include_combined:
        combined_rows: list[dict] = []
        for rows in (sellers_by_app or {}).values():
            combined_rows.extend(rows)
        by_app[_COMBINED_LABEL] = _process(combined_rows)

    return {"buckets": bucket_labels, "by_app": by_app}


# ---------------------------------------------------------------------
# Paid/Not-Paid split inside activity buckets — lets the dashboard show
# "of the 247 active shein sellers, X are Paid / Y are Not Paid".
# ---------------------------------------------------------------------

def activity_by_paid_status(
    sellers_by_app: dict[str, list[dict]],
    *,
    include_combined: bool = True,
) -> dict[str, Any]:
    """Counts per app of sellers that are Paid/Not Paid crossed with
    active (≥1 order) / zero-order. Single-pass — small but gives the
    stakeholder 4 numbers per app they can reason about quickly.

    Returns:
        {
          app: {
            "Paid":      {"active": n, "zero_order": n, "total": n,
                          "total_orders": int, "total_products": int},
            "Not Paid":  {...},
            "total":     n,
          }
        }
    """
    def _process(rows: list[dict]) -> dict[str, Any]:
        out = {
            "Paid": {"active": 0, "zero_order": 0, "total": 0,
                     "total_orders": 0, "total_products": 0},
            "Not Paid": {"active": 0, "zero_order": 0, "total": 0,
                         "total_orders": 0, "total_products": 0},
            "total": 0,
        }
        for r in rows or []:
            status = classify_paid(r.get("plan"))
            orders = _coerce_int(r.get("order_count"), 0)
            products = _coerce_int(r.get("product_count"), 0)
            b = out[status]
            b["total"] += 1
            b["total_orders"] += orders
            b["total_products"] += products
            if orders == 0:
                b["zero_order"] += 1
            else:
                b["active"] += 1
            out["total"] += 1
        return out

    by_app: dict[str, dict[str, Any]] = {}
    for app, rows in (sellers_by_app or {}).items():
        by_app[app] = _process(rows)

    if include_combined:
        combined: list[dict] = []
        for rows in (sellers_by_app or {}).values():
            combined.extend(rows)
        by_app[_COMBINED_LABEL] = _process(combined)

    return by_app


# ---------------------------------------------------------------------
# Year-filter helper — restrict sellers/uninstalls to a single calendar
# year. Used by the dashboard when the user picks e.g. "2026" from the
# sidebar year dropdown. A value of None means "no filter".
# ---------------------------------------------------------------------

def filter_by_year(
    rows_by_app: dict[str, list[dict]],
    *,
    date_field: str,
    year: int | None,
) -> dict[str, list[dict]]:
    """Keep only rows whose date_field falls in the given calendar year.
    Rows with unparseable dates are dropped (they can't be attributed
    to a year and lumping them into "all" would mislead). If year is
    None, the input is returned unchanged."""
    if year is None or not rows_by_app:
        return rows_by_app
    out: dict[str, list[dict]] = {}
    for app, rows in rows_by_app.items():
        kept: list[dict] = []
        for r in rows or []:
            d = _parse_iso_date(r.get(date_field))
            if d is None:
                continue
            if d.year == year:
                kept.append(r)
        out[app] = kept
    return out


# ---------------------------------------------------------------------
# Headline stakeholder report — one-stop assembly
# ---------------------------------------------------------------------

def build_stakeholder_report(
    *,
    sellers_by_app: dict[str, list[dict]],
    uninstalls_by_app: dict[str, list[dict]],
    run_stamp: str,
    asof: date | None = None,
    drop_test_stores: bool = True,
) -> dict[str, Any]:
    """Produce a single nested dict bundling every metric in this
    module. Designed so the dashboard + markdown renderer can walk a
    known shape without re-assembling pieces themselves.

    Caller responsibility: pass already-normalized inputs.

    `drop_test_stores` strips sellers/uninstalls belonging to internal
    threecolts.com / cedcommerce.com email domains before any metric
    is computed. Stakeholders want the real-customer numbers, not our
    QA traffic, so this defaults to True. Pass False only when you're
    deliberately debugging the test cohort.
    """
    if drop_test_stores:
        sellers_by_app = exclude_test_stores(sellers_by_app)
        uninstalls_by_app = exclude_test_stores(uninstalls_by_app)

    monthly = timeseries_by_period(
        sellers_by_app=sellers_by_app,
        uninstalls_by_app=uninstalls_by_app,
        period="month",
    )
    quarterly = timeseries_by_period(
        sellers_by_app=sellers_by_app,
        uninstalls_by_app=uninstalls_by_app,
        period="quarter",
    )
    yearly = timeseries_by_period(
        sellers_by_app=sellers_by_app,
        uninstalls_by_app=uninstalls_by_app,
        period="year",
    )

    # Growth rates: we attach them to the same object as the raw
    # counts so callers can pick either side without re-walking the
    # periods list.
    monthly["installs_growth_pct"] = compute_growth_rates(monthly["installs"])
    monthly["uninstalls_growth_pct"] = compute_growth_rates(monthly["uninstalls"])
    quarterly["installs_growth_pct"] = compute_growth_rates(quarterly["installs"])
    quarterly["uninstalls_growth_pct"] = compute_growth_rates(quarterly["uninstalls"])
    yearly["installs_growth_pct"] = compute_growth_rates(yearly["installs"])
    yearly["uninstalls_growth_pct"] = compute_growth_rates(yearly["uninstalls"])

    monthly["installs_cumulative"] = compute_cumulative(monthly["installs"])

    # Dimensional breakdowns (active sellers only — for the "who are
    # our current customers?" view). `app_type` was explicitly dropped
    # per stakeholder feedback ("App type is nothing. I dont need this.")
    # `plan` is still computed because downstream code surfaces coverage,
    # but the dashboard now renders the Paid / Not Paid split instead.
    dims: dict[str, dict[str, Any]] = {}
    for field in ("plan", "steps_completed", "source_country", "platforms"):
        dims[field] = dimensional_breakdown(sellers_by_app, field)

    steps = steps_by_install_month(sellers_by_app)
    activity = activity_segmentation(sellers_by_app)
    product_activity = product_activity_segmentation(sellers_by_app)
    paid = paid_breakdown(sellers_by_app)
    activity_paid = activity_by_paid_status(sellers_by_app)
    velocity = install_velocity(sellers_by_app, asof=asof)
    unins_platform = uninstall_platform_split(uninstalls_by_app)

    logging.info(
        "📈 Stakeholder report assembled: "
        f"{len(monthly['periods'])} months, "
        f"{len(quarterly['periods'])} quarters, "
        f"{len(yearly['periods'])} years of coverage."
    )

    return {
        "run_stamp": run_stamp,
        "run_stamp_pretty": fmt_date_long(run_stamp[:10]) if run_stamp else "",
        "monthly": monthly,
        "quarterly": quarterly,
        "yearly": yearly,
        "dimensions": dims,
        "paid": paid,
        "steps_by_install_month": steps,
        "activity": activity,
        "product_activity": product_activity,
        "activity_by_paid": activity_paid,
        "install_velocity": velocity,
        "uninstall_platform_split": unins_platform,
    }


# ---------------------------------------------------------------------
# Markdown renderer for the stakeholder report
# ---------------------------------------------------------------------

def render_stakeholder_markdown(report: dict[str, Any]) -> str:
    """Render a stakeholder-facing markdown digest. Paste-friendly into
    email or Slack, and persists via pipeline.py alongside the run
    summary so non-technical readers have one doc to open."""
    lines: list[str] = []
    lines.append(f"# cHAP Seller Tracker — Stakeholder Report")
    if report.get("run_stamp_pretty"):
        lines.append(f"_Run date: {report['run_stamp_pretty']}_")
    lines.append("")

    # ---- Monthly installs / uninstalls ----
    monthly = report["monthly"]
    lines.append("## Monthly Installs & Uninstalls (per app + combined)")
    lines.append("")
    lines.append("| Month | App | Installs | Δ MoM % | Uninstalls | Δ MoM % |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for p in monthly["periods"]:
        # An app can show up only in installs (no uninstalls yet) or
        # only in uninstalls (installed before our history window). We
        # union the key sets so either side is reported, and `.get`
        # everywhere so the other side's missing key is zero.
        apps = list(dict.fromkeys(
            list(monthly["installs"].keys())
            + list(monthly["uninstalls"].keys())
        ))
        for app in apps:
            i = monthly["installs"].get(app, {}).get(p, 0)
            u = monthly["uninstalls"].get(app, {}).get(p, 0)
            gi = monthly["installs_growth_pct"].get(app, {}).get(p)
            gu = monthly["uninstalls_growth_pct"].get(app, {}).get(p)
            gi_s = "—" if gi is None else f"{gi:+.1f}%"
            gu_s = "—" if gu is None else f"{gu:+.1f}%"
            # Skip empty rows to keep the table compact.
            if i == 0 and u == 0 and gi is None and gu is None:
                continue
            lines.append(
                f"| {fmt_month(p)} | {app} | {i} | {gi_s} | {u} | {gu_s} |"
            )
    lines.append("")

    # ---- Quarterly ----
    quarterly = report["quarterly"]
    lines.append("## Quarterly Installs & Uninstalls")
    lines.append("")
    lines.append("| Quarter | App | Installs | Δ QoQ % | Uninstalls | Δ QoQ % |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for p in quarterly["periods"]:
        apps = list(dict.fromkeys(
            list(quarterly["installs"].keys())
            + list(quarterly["uninstalls"].keys())
        ))
        for app in apps:
            i = quarterly["installs"].get(app, {}).get(p, 0)
            u = quarterly["uninstalls"].get(app, {}).get(p, 0)
            gi = quarterly["installs_growth_pct"].get(app, {}).get(p)
            gu = quarterly["uninstalls_growth_pct"].get(app, {}).get(p)
            gi_s = "—" if gi is None else f"{gi:+.1f}%"
            gu_s = "—" if gu is None else f"{gu:+.1f}%"
            if i == 0 and u == 0 and gi is None and gu is None:
                continue
            lines.append(
                f"| {fmt_quarter(p)} | {app} | {i} | {gi_s} | {u} | {gu_s} |"
            )
    lines.append("")

    # ---- Yearly ----
    yearly = report["yearly"]
    lines.append("## Yearly Installs & Uninstalls")
    lines.append("")
    lines.append("| Year | App | Installs | Δ YoY % | Uninstalls | Δ YoY % |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for p in yearly["periods"]:
        apps = list(dict.fromkeys(
            list(yearly["installs"].keys())
            + list(yearly["uninstalls"].keys())
        ))
        for app in apps:
            i = yearly["installs"].get(app, {}).get(p, 0)
            u = yearly["uninstalls"].get(app, {}).get(p, 0)
            gi = yearly["installs_growth_pct"].get(app, {}).get(p)
            gu = yearly["uninstalls_growth_pct"].get(app, {}).get(p)
            gi_s = "—" if gi is None else f"{gi:+.1f}%"
            gu_s = "—" if gu is None else f"{gu:+.1f}%"
            if i == 0 and u == 0 and gi is None and gu is None:
                continue
            lines.append(
                f"| {fmt_year(p)} | {app} | {i} | {gi_s} | {u} | {gu_s} |"
            )
    lines.append("")

    # ---- Dimensional: plan ----
    plan = report["dimensions"]["plan"]
    lines.append("## Plan Distribution (active sellers)")
    lines.append("")
    for app, counts in plan["breakdown"].items():
        cov = plan["coverage"].get(app, 0)
        lines.append(f"### {app}  _(coverage: {cov:.0f}%)_")
        if not counts:
            lines.append("_No data._")
        else:
            for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
                lines.append(f"- {label}: **{n}**")
        lines.append("")

    # ---- Dimensional: source country ----
    country = report["dimensions"]["source_country"]
    lines.append("## Source Country Distribution (active sellers)")
    lines.append("")
    for app, counts in country["breakdown"].items():
        lines.append(f"### {app}")
        if not counts:
            lines.append("_No data._")
        else:
            for label, n in sorted(counts.items(), key=lambda kv: -kv[1])[:15]:
                lines.append(f"- {label}: **{n}**")
        lines.append("")

    # ---- Platforms ----
    plat = report["dimensions"]["platforms"]
    lines.append("## Framework / Platform Combo (active sellers)")
    lines.append("")
    for app, counts in plat["breakdown"].items():
        lines.append(f"### {app}")
        for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {label}: **{n}**")
        lines.append("")

    # ---- Activity / order buckets ----
    activity = report["activity"]
    lines.append("## Order Activity Segmentation")
    lines.append("")
    lines.append("| App | Total | Active (≥1 order) | Zero-order | With failed orders | Total orders |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for app, data in activity["by_app"].items():
        lines.append(
            f"| {app} | {data['total_sellers']} | {data['active_sellers']} | "
            f"{data['zero_order_sellers']} | {data['sellers_with_failed_orders']} | "
            f"{data['total_orders']} |"
        )
    lines.append("")

    # ---- Steps ----
    steps = report["steps_by_install_month"]
    lines.append("## Onboarding Step Distribution (by install month)")
    lines.append("")
    for app, per_month in steps["by_app"].items():
        cov = steps["coverage"].get(app, 0)
        lines.append(f"### {app}  _(steps coverage: {cov:.0f}%)_")
        if not per_month:
            lines.append("_No data._")
            lines.append("")
            continue
        all_steps = sorted({s for month in per_month.values() for s in month})
        header = ["Month"] + all_steps + ["Total"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for m in sorted(per_month):
            row = [fmt_month(m)]
            total = 0
            for s in all_steps:
                n = per_month[m].get(s, 0)
                total += n
                row.append(str(n))
            row.append(str(total))
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")

    # ---- Uninstall platform split ----
    uns = report["uninstall_platform_split"]["by_app"]
    lines.append("## Uninstall Platform Split")
    lines.append("")
    lines.append("| App | Platform | Count |")
    lines.append("|---|---|---:|")
    for app, counts in uns.items():
        for platform, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {app} | {platform} | {n} |")
    lines.append("")

    return "\n".join(lines)
