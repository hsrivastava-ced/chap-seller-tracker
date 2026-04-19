"""
Unit tests for analytics_advanced.py.

analytics_advanced powers every number the stakeholder dashboard shows:
monthly / quarterly / yearly trends, growth %, cumulative curves,
dimensional breakdowns (plan, country, platforms), onboarding-step
cohorts, order-activity segmentation, install velocity, and uninstall
platform split. These tests freeze the contract: if someone edits a
bucket boundary or a growth-rate formula, the stakeholder-visible
numbers will change, and this suite catches that before it lands.

The module is pure (no I/O, no network, no Playwright) so the tests
are equally pure — no fixtures beyond the tiny factories below. Run:

    python3 -m pytest tests/test_analytics_advanced.py -v
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from analytics_advanced import (
    _bucket_order_count,
    _coerce_int,
    _dim_breakdown,
    _month_key,
    _month_range,
    _parse_iso_date,
    _pct_change,
    _quarter_key,
    _quarter_range,
    _timeseries_counts,
    _year_key,
    _year_range,
    activity_segmentation,
    build_stakeholder_report,
    compute_cumulative,
    compute_growth_rates,
    dimensional_breakdown,
    fmt_date_long,
    fmt_month,
    fmt_quarter,
    fmt_year,
    install_velocity,
    render_stakeholder_markdown,
    steps_by_install_month,
    timeseries_by_period,
    uninstall_platform_split,
)


# ---------------------------------------------------------------------
# Small row factories
# ---------------------------------------------------------------------

def _seller(sid: str, installed_on: str = "2026-01-15", **extra) -> dict:
    base = {
        "seller_id": sid,
        "email": f"seller{sid}@example.com",
        "store_url": f"shop{sid}.myshopify.com",
        "platforms": "Shopify",
        "installed_on": installed_on,
        "order_count": 0,
        "failed_order_count": 0,
    }
    base.update(extra)
    return base


def _uninstall(sid: str, uninstalled_on: str = "2026-02-01",
               platform: str = "Shopify", **extra) -> dict:
    base = {
        "seller_id": sid,
        "uninstalled_on": uninstalled_on,
        "platform": platform,
    }
    base.update(extra)
    return base


# =====================================================================
# Date parsing
# =====================================================================

class TestParseIsoDate:
    def test_parses_plain_iso_date(self):
        assert _parse_iso_date("2026-04-18") == date(2026, 4, 18)

    def test_parses_iso_with_time_t_separator(self):
        assert _parse_iso_date("2026-04-18T09:30:15") == date(2026, 4, 18)

    def test_parses_iso_with_time_space_separator(self):
        assert _parse_iso_date("2026-04-18 09:30:15") == date(2026, 4, 18)

    def test_parses_iso_with_microseconds(self):
        # The leading-19-char slice should still match '%Y-%m-%dT%H:%M:%S'.
        assert _parse_iso_date("2026-04-18T19:38:19.123") == date(2026, 4, 18)

    def test_parses_iso_with_zulu_suffix(self):
        assert _parse_iso_date("2026-04-18T00:00:00Z") == date(2026, 4, 18)

    def test_passthrough_date(self):
        d = date(2026, 1, 1)
        assert _parse_iso_date(d) is d

    def test_passthrough_datetime(self):
        assert _parse_iso_date(datetime(2026, 4, 18, 9, 30)) == date(2026, 4, 18)

    def test_returns_none_for_empty(self):
        assert _parse_iso_date("") is None
        assert _parse_iso_date(None) is None
        assert _parse_iso_date("   ") is None

    def test_returns_none_for_uk_locale_not_auto_retranslated(self):
        # Must NOT silently treat "03/04/2026" as a date. The normaliser
        # handles UK-locale strings upstream; a raw one here means a bug
        # escaped normalisation and we want it loud.
        assert _parse_iso_date("03/04/2026") is None

    def test_returns_none_for_nonsense(self):
        assert _parse_iso_date("not a date") is None
        assert _parse_iso_date(12345) is None


# =====================================================================
# Period keys & formatters
# =====================================================================

class TestPeriodKeys:
    def test_month_key_zero_pads(self):
        assert _month_key(date(2026, 4, 1)) == "2026-04"
        assert _month_key(date(2026, 12, 31)) == "2026-12"

    def test_quarter_key_boundaries(self):
        assert _quarter_key(date(2026, 1, 1)) == "2026-Q1"
        assert _quarter_key(date(2026, 3, 31)) == "2026-Q1"
        assert _quarter_key(date(2026, 4, 1)) == "2026-Q2"
        assert _quarter_key(date(2026, 6, 30)) == "2026-Q2"
        assert _quarter_key(date(2026, 7, 1)) == "2026-Q3"
        assert _quarter_key(date(2026, 9, 30)) == "2026-Q3"
        assert _quarter_key(date(2026, 10, 1)) == "2026-Q4"
        assert _quarter_key(date(2026, 12, 31)) == "2026-Q4"

    def test_year_key(self):
        assert _year_key(date(2026, 6, 15)) == "2026"

    def test_month_keys_lexicographically_sortable(self):
        keys = [_month_key(date(2025, 11, 1)),
                _month_key(date(2026, 1, 1)),
                _month_key(date(2025, 12, 1))]
        assert sorted(keys) == ["2025-11", "2025-12", "2026-01"]


class TestFormatters:
    def test_fmt_month_pretty(self):
        assert fmt_month("2026-04") == "Apr 2026"
        assert fmt_month("2023-10") == "Oct 2023"

    def test_fmt_month_invalid_passthrough(self):
        # Stakeholder reports should never blow up on a bad key —
        # just show it verbatim.
        assert fmt_month("nonsense") == "nonsense"
        assert fmt_month("2026-13") == "2026-13"  # out-of-range month

    def test_fmt_quarter(self):
        assert fmt_quarter("2026-Q2") == "Q2 2026"
        assert fmt_quarter("2024-Q4") == "Q4 2024"

    def test_fmt_quarter_invalid_passthrough(self):
        assert fmt_quarter("junk") == "junk"

    def test_fmt_year_identity(self):
        assert fmt_year("2026") == "2026"

    def test_fmt_date_long_full(self):
        assert fmt_date_long("2026-04-18") == "18 Apr 2026"
        assert fmt_date_long("2026-04-18T09:30:15") == "18 Apr 2026"

    def test_fmt_date_long_empty_on_unparseable(self):
        assert fmt_date_long("") == ""
        assert fmt_date_long(None) == ""
        assert fmt_date_long("garbage") == ""


# =====================================================================
# Period enumeration (gap-filling)
# =====================================================================

class TestPeriodRanges:
    def test_month_range_inclusive_single_month(self):
        r = _month_range(date(2026, 4, 1), date(2026, 4, 30))
        assert r == ["2026-04"]

    def test_month_range_across_year_boundary(self):
        r = _month_range(date(2025, 11, 1), date(2026, 2, 28))
        assert r == ["2025-11", "2025-12", "2026-01", "2026-02"]

    def test_month_range_reverse_returns_empty(self):
        # start > end guard — we'd rather emit nothing than an infinite loop.
        assert _month_range(date(2026, 6, 1), date(2026, 1, 1)) == []

    def test_quarter_range_across_year_boundary(self):
        r = _quarter_range(date(2025, 10, 1), date(2026, 6, 30))
        assert r == ["2025-Q4", "2026-Q1", "2026-Q2"]

    def test_year_range(self):
        r = _year_range(date(2023, 6, 15), date(2026, 1, 2))
        assert r == ["2023", "2024", "2025", "2026"]


# =====================================================================
# Time-series bucketing
# =====================================================================

class TestTimeseriesCounts:
    def test_bucket_by_month(self):
        rows = [
            {"installed_on": "2026-01-15"},
            {"installed_on": "2026-01-28"},
            {"installed_on": "2026-02-03"},
        ]
        got = _timeseries_counts(rows, "installed_on", _month_key)
        assert got == {"2026-01": 2, "2026-02": 1}

    def test_skips_unparseable(self):
        rows = [
            {"installed_on": "2026-01-15"},
            {"installed_on": ""},
            {"installed_on": None},
            {"installed_on": "not a date"},
            {"installed_on": "2026-01-20"},
        ]
        got = _timeseries_counts(rows, "installed_on", _month_key)
        assert got == {"2026-01": 2}

    def test_empty_input(self):
        assert _timeseries_counts([], "installed_on", _month_key) == {}
        assert _timeseries_counts(None, "installed_on", _month_key) == {}


class TestTimeseriesByPeriod:
    def _fixture(self):
        sellers = {
            "shopify_temu": [
                _seller("A", installed_on="2026-01-05"),
                _seller("B", installed_on="2026-01-20"),
                _seller("C", installed_on="2026-03-10"),  # Feb is the gap
            ],
            "shein": [
                _seller("D", installed_on="2026-02-02"),
            ],
        }
        unins = {
            "shopify_temu": [
                _uninstall("X", uninstalled_on="2026-02-15"),
            ],
            "shein": [
                _uninstall("Y", uninstalled_on="2026-03-05"),
            ],
        }
        return sellers, unins

    def test_monthly_shape(self):
        sellers, unins = self._fixture()
        r = timeseries_by_period(sellers_by_app=sellers,
                                 uninstalls_by_app=unins,
                                 period="month")
        assert r["period"] == "month"
        assert r["periods"] == ["2026-01", "2026-02", "2026-03"]
        # per-app installs
        assert r["installs"]["shopify_temu"] == \
            {"2026-01": 2, "2026-02": 0, "2026-03": 1}
        assert r["installs"]["shein"] == \
            {"2026-01": 0, "2026-02": 1, "2026-03": 0}
        # combined installs
        assert r["installs"]["all_apps"] == \
            {"2026-01": 2, "2026-02": 1, "2026-03": 1}
        # uninstalls
        assert r["uninstalls"]["all_apps"] == \
            {"2026-01": 0, "2026-02": 1, "2026-03": 1}

    def test_gap_filling_even_when_only_one_side_populated(self):
        # Sellers span Jan–Mar, but uninstalls are empty. Uninstall
        # series still needs to be densified to the same periods so
        # the chart doesn't have a ragged edge.
        sellers, _ = self._fixture()
        r = timeseries_by_period(sellers_by_app=sellers,
                                 uninstalls_by_app={},
                                 period="month")
        assert r["periods"] == ["2026-01", "2026-02", "2026-03"]

    def test_quarterly_bucketing(self):
        sellers, unins = self._fixture()
        r = timeseries_by_period(sellers_by_app=sellers,
                                 uninstalls_by_app=unins,
                                 period="quarter")
        # Everything falls in 2026-Q1.
        assert r["periods"] == ["2026-Q1"]
        assert r["installs"]["all_apps"] == {"2026-Q1": 4}
        assert r["uninstalls"]["all_apps"] == {"2026-Q1": 2}

    def test_yearly_bucketing_cross_year(self):
        sellers = {
            "a": [_seller("A", installed_on="2024-07-01"),
                  _seller("B", installed_on="2025-03-15"),
                  _seller("C", installed_on="2026-01-01")]
        }
        r = timeseries_by_period(sellers_by_app=sellers,
                                 uninstalls_by_app={},
                                 period="year")
        assert r["periods"] == ["2024", "2025", "2026"]
        assert r["installs"]["a"] == {"2024": 1, "2025": 1, "2026": 1}

    def test_include_combined_false_skips_all_apps_key(self):
        sellers, unins = self._fixture()
        r = timeseries_by_period(sellers_by_app=sellers,
                                 uninstalls_by_app=unins,
                                 period="month",
                                 include_combined=False)
        assert "all_apps" not in r["installs"]
        assert "all_apps" not in r["uninstalls"]

    def test_empty_inputs_return_empty_periods(self):
        r = timeseries_by_period(sellers_by_app={},
                                 uninstalls_by_app={},
                                 period="month")
        assert r["periods"] == []
        assert r["installs"] == {"all_apps": {}}
        assert r["uninstalls"] == {"all_apps": {}}


# =====================================================================
# Growth rates
# =====================================================================

class TestPctChange:
    def test_normal_positive_growth(self):
        assert _pct_change(150, 100) == 50.0

    def test_normal_negative_growth(self):
        assert _pct_change(50, 100) == -50.0

    def test_flat(self):
        assert _pct_change(100, 100) == 0.0

    def test_div_by_zero_returns_none(self):
        # Stakeholder UI renders None as "—"/"N/A" rather than infinity
        # or a synthetic "+100%".
        assert _pct_change(10, 0) is None
        assert _pct_change(0, 0) is None


class TestComputeGrowthRates:
    def test_first_period_has_no_predecessor(self):
        series = {"app": {"2026-01": 10, "2026-02": 20, "2026-03": 15}}
        out = compute_growth_rates(series)
        assert out["app"]["2026-01"] is None
        assert out["app"]["2026-02"] == 100.0  # 10 → 20
        assert out["app"]["2026-03"] == -25.0  # 20 → 15

    def test_from_zero_returns_none(self):
        series = {"app": {"2026-01": 0, "2026-02": 5}}
        out = compute_growth_rates(series)
        assert out["app"]["2026-01"] is None
        assert out["app"]["2026-02"] is None  # prev was 0 → div-by-zero

    def test_empty_series(self):
        assert compute_growth_rates({}) == {}
        assert compute_growth_rates({"app": {}}) == {"app": {}}


class TestCumulative:
    def test_running_total(self):
        series = {"a": {"2026-01": 3, "2026-02": 2, "2026-03": 5}}
        out = compute_cumulative(series)
        assert out["a"] == {"2026-01": 3, "2026-02": 5, "2026-03": 10}

    def test_preserves_order(self):
        series = {"a": {"2026-01": 3, "2026-02": 0, "2026-03": 7}}
        out = compute_cumulative(series)
        assert list(out["a"].keys()) == ["2026-01", "2026-02", "2026-03"]
        assert out["a"]["2026-02"] == 3
        assert out["a"]["2026-03"] == 10

    def test_empty(self):
        assert compute_cumulative({}) == {}


# =====================================================================
# Dimensional breakdowns
# =====================================================================

class TestDimBreakdown:
    def test_counts_and_coverage(self):
        rows = [
            {"plan": "Free"},
            {"plan": "Pro"},
            {"plan": "Pro"},
            {"plan": ""},
            {"plan": None},
            {"plan": "(empty)"},
        ]
        counts, cov = _dim_breakdown(rows, "plan")
        assert counts == {"Free": 1, "Pro": 2, "(not set)": 3}
        # 3 of 6 populated.
        assert cov == pytest.approx(50.0)

    def test_strips_whitespace(self):
        rows = [{"plan": "  Pro "}, {"plan": "Pro"}]
        counts, _ = _dim_breakdown(rows, "plan")
        assert counts == {"Pro": 2}

    def test_empty_rows(self):
        counts, cov = _dim_breakdown([], "plan")
        assert counts == {}
        assert cov == 0.0


class TestDimensionalBreakdown:
    def test_multi_app_combined(self):
        rows_by_app = {
            "a": [{"plan": "Free"}, {"plan": "Pro"}],
            "b": [{"plan": "Pro"}, {"plan": ""}],
        }
        r = dimensional_breakdown(rows_by_app, "plan")
        assert r["field"] == "plan"
        assert r["breakdown"]["a"] == {"Free": 1, "Pro": 1}
        assert r["breakdown"]["b"] == {"Pro": 1, "(not set)": 1}
        assert r["breakdown"]["all_apps"] == \
            {"Free": 1, "Pro": 2, "(not set)": 1}
        assert r["coverage"]["a"] == pytest.approx(100.0)
        assert r["coverage"]["b"] == pytest.approx(50.0)
        assert r["coverage"]["all_apps"] == pytest.approx(75.0)

    def test_include_combined_false(self):
        r = dimensional_breakdown({"a": [{"plan": "X"}]}, "plan",
                                  include_combined=False)
        assert "all_apps" not in r["breakdown"]
        assert "all_apps" not in r["coverage"]

    def test_unpopulated_app_zero_coverage(self):
        # shopify_temu has no plan column surfaced → 0% coverage.
        r = dimensional_breakdown(
            {"shopify_temu": [{"seller_id": "A"}, {"seller_id": "B"}]},
            "plan",
        )
        assert r["coverage"]["shopify_temu"] == pytest.approx(0.0)
        assert r["breakdown"]["shopify_temu"] == {"(not set)": 2}


# =====================================================================
# Steps by install-month
# =====================================================================

class TestStepsByInstallMonth:
    def test_per_month_step_matrix(self):
        sellers_by_app = {
            "eu": [
                _seller("A", installed_on="2026-01-05", steps_completed="3/4"),
                _seller("B", installed_on="2026-01-20", steps_completed="3/4"),
                _seller("C", installed_on="2026-01-25", steps_completed="4/4"),
                _seller("D", installed_on="2026-02-10", steps_completed="4/4"),
                _seller("E", installed_on="2026-02-11"),  # no steps_completed
            ],
        }
        r = steps_by_install_month(sellers_by_app)
        assert r["by_app"]["eu"]["2026-01"] == {"3/4": 2, "4/4": 1}
        assert r["by_app"]["eu"]["2026-02"] == {"4/4": 1, "(not set)": 1}
        # combined == same single app
        assert r["by_app"]["all_apps"]["2026-01"] == {"3/4": 2, "4/4": 1}
        # coverage: 4 of 5 have non-empty steps_completed.
        assert r["coverage"]["eu"] == pytest.approx(80.0)
        # steps_values collects distinct values across all apps.
        assert "3/4" in r["steps_values"]
        assert "4/4" in r["steps_values"]
        assert "(not set)" in r["steps_values"]

    def test_unparseable_installed_on_skipped(self):
        sellers_by_app = {
            "a": [
                _seller("A", installed_on="2026-01-01", steps_completed="1/4"),
                _seller("B", installed_on="not a date", steps_completed="1/4"),
            ],
        }
        r = steps_by_install_month(sellers_by_app)
        # The broken-date row is skipped from the month matrix but is
        # still counted in the coverage *denominator* (it exists in the
        # data). `covered` is only incremented after the date-parse
        # check passes + the steps value is non-empty, so here only 1
        # of 2 rows contributes to `covered` → 50%. This matches the
        # user's expectation that low coverage flags data-quality
        # issues like broken install dates.
        assert r["by_app"]["a"] == {"2026-01": {"1/4": 1}}
        assert r["coverage"]["a"] == pytest.approx(50.0)

    def test_empty_input(self):
        r = steps_by_install_month({})
        assert r["by_app"] == {"all_apps": {}}
        assert r["coverage"]["all_apps"] == 0.0


# =====================================================================
# Activity segmentation
# =====================================================================

class TestCoerceInt:
    def test_regular_int(self):
        assert _coerce_int(42) == 42

    def test_string_int(self):
        assert _coerce_int("42") == 42

    def test_string_with_commas(self):
        # Admin panel renders "1,234"
        assert _coerce_int("1,234") == 1234

    def test_empty_returns_default(self):
        assert _coerce_int(None) == 0
        assert _coerce_int("") == 0

    def test_custom_default(self):
        assert _coerce_int("junk", default=-1) == -1


class TestBucketOrderCount:
    def test_zero(self):
        assert _bucket_order_count(0) == "0 orders"

    def test_low(self):
        assert _bucket_order_count(1) == "1-10 orders"
        assert _bucket_order_count(10) == "1-10 orders"

    def test_mid(self):
        assert _bucket_order_count(11) == "11-100 orders"
        assert _bucket_order_count(100) == "11-100 orders"
        assert _bucket_order_count(101) == "101-1k orders"
        assert _bucket_order_count(1000) == "101-1k orders"

    def test_top_bucket_unbounded(self):
        assert _bucket_order_count(1001) == "1k+ orders"
        assert _bucket_order_count(500_000) == "1k+ orders"


class TestActivitySegmentation:
    def test_full_segmentation(self):
        sellers_by_app = {
            "a": [
                _seller("A", order_count=0, failed_order_count=0),
                _seller("B", order_count=5, failed_order_count=1),
                _seller("C", order_count=50, failed_order_count=0),
                _seller("D", order_count="1,234", failed_order_count=3),
            ],
        }
        r = activity_segmentation(sellers_by_app)
        a = r["by_app"]["a"]
        assert a["total_sellers"] == 4
        assert a["zero_order_sellers"] == 1
        assert a["active_sellers"] == 3
        assert a["sellers_with_failed_orders"] == 2
        # 0 + 5 + 50 + 1234 = 1289
        assert a["total_orders"] == 1289
        assert a["total_failed_orders"] == 4
        assert a["order_buckets"]["0 orders"] == 1
        assert a["order_buckets"]["1-10 orders"] == 1
        assert a["order_buckets"]["11-100 orders"] == 1
        assert a["order_buckets"]["1k+ orders"] == 1
        # Every bucket label present, even when zero.
        assert set(a["order_buckets"].keys()) == set(r["buckets"])

    def test_bucket_order_preserved(self):
        sellers_by_app = {"a": [_seller("A")]}
        r = activity_segmentation(sellers_by_app)
        assert r["buckets"] == [
            "0 orders", "1-10 orders", "11-100 orders",
            "101-1k orders", "1k+ orders",
        ]

    def test_empty_app(self):
        r = activity_segmentation({"a": []})
        assert r["by_app"]["a"]["total_sellers"] == 0
        assert r["by_app"]["a"]["total_orders"] == 0


# =====================================================================
# Install velocity
# =====================================================================

class TestInstallVelocity:
    def test_rolling_30_over_90_default(self):
        asof = date(2026, 4, 30)
        sellers_by_app = {
            "a": [
                # Inside the last 30 days of asof (window ends at asof).
                _seller("A", installed_on="2026-04-20"),
                _seller("B", installed_on="2026-04-15"),
                # 40 days before asof — still in 90-day lookback but
                # outside any given day's 30-day window by most of the span.
                _seller("C", installed_on="2026-03-21"),
                # Way outside lookback.
                _seller("Z", installed_on="2024-01-01"),
            ],
        }
        r = install_velocity(sellers_by_app, asof=asof,
                             lookback_days=90, window_days=30)
        # Shape
        assert r["asof"] == "2026-04-30"
        assert r["lookback_days"] == 90
        assert r["window_days"] == 30
        # 91 days in the series (lookback inclusive).
        assert len(r["days"]) == 91
        # Last day window is (04-00, 04-30] → A, B, and C fall in
        # (04-00, 04-30] but C is 03-21 which is >= 04-01 on calendar?
        # 04-30 minus 30 = 03-31. So C (03-21) is OUTSIDE the final
        # window. A + B are inside.
        assert r["series"]["a"]["2026-04-30"] == 2
        # First day window is (start - 30d, start] — A, B, C all
        # AFTER start (start = 2026-01-30), so 0.
        assert r["series"]["a"]["2026-01-30"] == 0

    def test_asof_defaults_to_today(self):
        # Just verify the asof default isn't raising — content is
        # environment-dependent so we only sanity-check shape.
        r = install_velocity({"a": []})
        assert "asof" in r
        assert len(r["days"]) == r["lookback_days"] + 1

    def test_skips_unparseable_dates(self):
        asof = date(2026, 4, 30)
        sellers_by_app = {
            "a": [
                _seller("A", installed_on="2026-04-15"),
                _seller("B", installed_on=""),
                _seller("C", installed_on="invalid"),
            ],
        }
        r = install_velocity(sellers_by_app, asof=asof,
                             lookback_days=30, window_days=30)
        # Only A counts — on 04-30, window is (04-00, 04-30] → yes.
        assert r["series"]["a"]["2026-04-30"] == 1

    def test_combined_rollup(self):
        asof = date(2026, 4, 30)
        sellers_by_app = {
            "a": [_seller("A", installed_on="2026-04-20")],
            "b": [_seller("B", installed_on="2026-04-25")],
        }
        r = install_velocity(sellers_by_app, asof=asof,
                             lookback_days=30, window_days=30)
        assert r["series"]["all_apps"]["2026-04-30"] == 2


# =====================================================================
# Uninstall platform split
# =====================================================================

class TestUninstallPlatformSplit:
    def test_per_app_plus_combined(self):
        unins_by_app = {
            "shopify_temu": [
                _uninstall("A", platform="Shopify"),
                _uninstall("B", platform="Temu"),
                _uninstall("C", platform="Temu"),
            ],
            "shopify_temu_eu": [
                _uninstall("D", platform="Prestashop"),
                _uninstall("E", platform="Shopify"),
            ],
        }
        r = uninstall_platform_split(unins_by_app)
        assert r["by_app"]["shopify_temu"] == {"Shopify": 1, "Temu": 2}
        assert r["by_app"]["shopify_temu_eu"] == \
            {"Prestashop": 1, "Shopify": 1}
        assert r["by_app"]["all_apps"] == \
            {"Shopify": 2, "Temu": 2, "Prestashop": 1}

    def test_missing_platform_labelled_empty(self):
        unins_by_app = {
            "a": [
                _uninstall("A", platform=""),
                _uninstall("B", platform=None),
                _uninstall("C", platform="   "),
            ],
        }
        r = uninstall_platform_split(unins_by_app)
        assert r["by_app"]["a"] == {"(empty)": 3}

    def test_include_combined_false(self):
        r = uninstall_platform_split(
            {"a": [_uninstall("A", platform="X")]}, include_combined=False
        )
        assert "all_apps" not in r["by_app"]

    def test_empty_input(self):
        r = uninstall_platform_split({})
        assert r["by_app"] == {"all_apps": {}}


# =====================================================================
# End-to-end report assembly + markdown render
# =====================================================================

class TestBuildStakeholderReport:
    def _sample(self):
        sellers = {
            "shopify_temu_eu": [
                _seller("A", installed_on="2026-01-05",
                        plan="Pro", source_country="US",
                        platforms="Shopify,Temu",
                        steps_completed="4/4",
                        order_count=15, failed_order_count=0,
                        app_type="marketplace"),
                _seller("B", installed_on="2026-02-10",
                        plan="Free", source_country="IN",
                        platforms="Shopify,Temu",
                        steps_completed="2/4",
                        order_count=0, failed_order_count=0,
                        app_type="marketplace"),
            ],
            "shein": [
                _seller("C", installed_on="2026-02-15",
                        platforms="Shopify,Shein",
                        order_count=200, failed_order_count=2),
            ],
        }
        unins = {
            "shopify_temu_eu": [
                _uninstall("X", uninstalled_on="2026-03-01",
                           platform="Prestashop"),
            ],
            "shein": [
                _uninstall("Y", uninstalled_on="2026-03-15",
                           platform="Shopify"),
            ],
        }
        return sellers, unins

    def test_report_has_all_sections(self):
        sellers, unins = self._sample()
        r = build_stakeholder_report(
            sellers_by_app=sellers,
            uninstalls_by_app=unins,
            run_stamp="2026-04-18T09:30:00",
            asof=date(2026, 4, 18),
        )
        assert r["run_stamp"] == "2026-04-18T09:30:00"
        assert r["run_stamp_pretty"] == "18 Apr 2026"
        # Full stakeholder shape — note `app_type` was dropped per
        # stakeholder feedback ("App type is nothing. I dont need this.")
        # and `paid` / `product_activity` / `activity_by_paid` were
        # added to back the new Paid-vs-Not-Paid + Product-Activity
        # panels on the dashboard.
        for key in ("monthly", "quarterly", "yearly", "dimensions",
                    "steps_by_install_month", "activity",
                    "install_velocity", "uninstall_platform_split",
                    "paid", "product_activity", "activity_by_paid"):
            assert key in r, f"missing top-level key: {key}"
        # Monthly growth pct attached in-situ.
        assert "installs_growth_pct" in r["monthly"]
        assert "installs_cumulative" in r["monthly"]
        # Dimensions we still compute (plan is kept even though the
        # dashboard renders Paid/Not Paid — downstream code surfaces
        # the coverage badge). app_type is gone intentionally.
        for field in ("plan", "steps_completed", "source_country", "platforms"):
            assert field in r["dimensions"]
        assert "app_type" not in r["dimensions"], (
            "app_type should have been removed per stakeholder feedback"
        )

    def test_report_respects_asof_for_velocity(self):
        sellers, unins = self._sample()
        r = build_stakeholder_report(
            sellers_by_app=sellers,
            uninstalls_by_app=unins,
            run_stamp="2026-04-18T00:00:00",
            asof=date(2026, 4, 18),
        )
        assert r["install_velocity"]["asof"] == "2026-04-18"

    def test_empty_inputs_do_not_crash(self):
        r = build_stakeholder_report(
            sellers_by_app={},
            uninstalls_by_app={},
            run_stamp="2026-04-18",
        )
        assert r["monthly"]["periods"] == []
        assert r["activity"]["by_app"]["all_apps"]["total_sellers"] == 0


class TestRenderStakeholderMarkdown:
    def test_markdown_contains_all_major_headers(self):
        sellers = {
            "shopify_temu_eu": [
                _seller("A", installed_on="2026-01-05",
                        plan="Pro", source_country="US",
                        platforms="Shopify,Temu",
                        steps_completed="4/4",
                        order_count=15),
            ],
        }
        unins = {
            "shopify_temu_eu": [
                _uninstall("X", uninstalled_on="2026-02-01",
                           platform="Prestashop"),
            ],
        }
        report = build_stakeholder_report(
            sellers_by_app=sellers,
            uninstalls_by_app=unins,
            run_stamp="2026-04-18T00:00:00",
            asof=date(2026, 4, 18),
        )
        md = render_stakeholder_markdown(report)
        # Title
        assert "cHAP Seller Tracker" in md
        # Section headers we promised stakeholders.
        assert "## Monthly Installs & Uninstalls" in md
        assert "## Quarterly Installs & Uninstalls" in md
        assert "## Yearly Installs & Uninstalls" in md
        assert "## Plan Distribution" in md
        assert "## Source Country Distribution" in md
        assert "## Framework / Platform Combo" in md
        assert "## Order Activity Segmentation" in md
        assert "## Onboarding Step Distribution" in md
        assert "## Uninstall Platform Split" in md
        # Human-readable dates in the tables.
        assert "Jan 2026" in md
        # Run-date line in the preamble.
        assert "18 Apr 2026" in md

    def test_growth_pct_rendered_as_dash_when_none(self):
        # Single-month series → every growth-pct is None → rendered "—".
        sellers = {"a": [_seller("A", installed_on="2026-01-05")]}
        report = build_stakeholder_report(
            sellers_by_app=sellers,
            uninstalls_by_app={},
            run_stamp="2026-04-18",
            asof=date(2026, 4, 18),
        )
        md = render_stakeholder_markdown(report)
        assert "—" in md  # em-dash placeholder for N/A growth
