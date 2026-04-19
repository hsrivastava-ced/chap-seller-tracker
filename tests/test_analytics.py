"""
Unit tests for analytics.py.

analytics is pure (no I/O, no network), so we test it exhaustively —
every KPI we show on the dashboard is derived here, and regressions
silently corrupt the "New Installs / Churn %" cards without the scrape
or Supabase push noticing anything is wrong.
"""

from __future__ import annotations

import pytest

from analytics import (
    _uninstall_key,
    analyse_run,
    build_report,
    compute_platform_split,
    compute_seller_delta,
    compute_uninstall_delta,
    flatten_to_metric_rows,
    render_markdown_report,
)


def _seller(sid: str, **extra) -> dict:
    """Small factory for a seller row — defaults match the scraper's
    minimum viable shape. Tests override only the fields they care
    about."""
    base = {
        "seller_id": sid,
        "email": f"seller{sid}@example.com",
        "store_url": f"shop{sid}.myshopify.com",
        "platforms": "Shopify",
        "installed_on": "2026-01-01",
    }
    base.update(extra)
    return base


def _uninstall(sid: str, platform: str, ts: str, **extra) -> dict:
    base = {
        "seller_id": sid,
        "platform": platform,
        "uninstalled_on": ts,
        "email": f"seller{sid}@example.com",
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------
# compute_seller_delta
# ---------------------------------------------------------------------

class TestComputeSellerDelta:
    def test_first_run_all_new(self):
        cur = [_seller("1"), _seller("2"), _seller("3")]
        out = compute_seller_delta(cur, [])
        assert out["counts"]["current"] == 3
        assert out["counts"]["previous"] == 0
        assert out["counts"]["new_installs"] == 3
        assert out["counts"]["churned_sellers"] == 0
        assert out["counts"]["retained_sellers"] == 0
        assert [r["seller_id"] for r in out["new_installs"]] == ["1", "2", "3"]

    def test_fully_overlapping(self):
        cur = [_seller("1"), _seller("2")]
        prev = [_seller("1"), _seller("2")]
        out = compute_seller_delta(cur, prev)
        assert out["counts"]["new_installs"] == 0
        assert out["counts"]["churned_sellers"] == 0
        assert out["counts"]["retained_sellers"] == 2

    def test_mixed_adds_and_removes(self):
        cur = [_seller("2"), _seller("3"), _seller("4")]
        prev = [_seller("1"), _seller("2"), _seller("3")]
        out = compute_seller_delta(cur, prev)
        assert out["counts"]["new_installs"] == 1
        assert out["counts"]["churned_sellers"] == 1
        assert out["counts"]["retained_sellers"] == 2
        assert [r["seller_id"] for r in out["new_installs"]] == ["4"]
        assert [r["seller_id"] for r in out["churned_sellers"]] == ["1"]

    def test_empty_current_all_churned(self):
        prev = [_seller("1"), _seller("2")]
        out = compute_seller_delta([], prev)
        assert out["counts"]["churned_sellers"] == 2
        assert out["counts"]["current"] == 0

    def test_both_empty(self):
        out = compute_seller_delta([], [])
        assert out["counts"] == {
            "current": 0, "previous": 0,
            "new_installs": 0, "churned_sellers": 0, "retained_sellers": 0,
        }
        assert out["new_installs"] == []
        assert out["churned_sellers"] == []

    def test_missing_seller_id_ignored(self):
        # Rows without a seller_id shouldn't crash — they're filtered out
        # of the id-set (which is what analytics actually compares on).
        cur = [_seller("1"), {"email": "anon@example.com"}]
        out = compute_seller_delta(cur, [])
        assert out["counts"]["current"] == 1

    def test_deterministic_order(self):
        # The sort-by-seller-id in _rows() matters: we snapshot-diff the
        # rendered markdown across runs, and unstable order creates noise.
        cur = [_seller("3"), _seller("1"), _seller("2")]
        out = compute_seller_delta(cur, [])
        assert [r["seller_id"] for r in out["new_installs"]] == ["1", "2", "3"]

    def test_duplicate_seller_id_uses_last(self):
        # If the scraper somehow emits duplicates, the lookup keeps the
        # *last* one. We check the row that ends up in new_installs
        # reflects that.
        cur = [_seller("1", email="first@x.com"), _seller("1", email="last@x.com")]
        out = compute_seller_delta(cur, [])
        assert out["counts"]["new_installs"] == 1
        assert out["new_installs"][0]["email"] == "last@x.com"


# ---------------------------------------------------------------------
# compute_uninstall_delta
# ---------------------------------------------------------------------

class TestComputeUninstallDelta:
    def test_dedup_key_uses_three_fields(self):
        # Same seller uninstalls twice, different times → two keys
        rows = [
            _uninstall("1", "Shopify", "2026-04-01T09:00:00"),
            _uninstall("1", "Shopify", "2026-04-05T10:00:00"),
        ]
        assert _uninstall_key(rows[0]) != _uninstall_key(rows[1])

    def test_new_uninstalls_delta(self):
        cur = [
            _uninstall("1", "Shopify", "2026-04-01T09:00:00"),
            _uninstall("2", "Shein", "2026-04-02T10:00:00"),
            _uninstall("3", "Shein", "2026-04-03T11:00:00"),
        ]
        prev = [_uninstall("1", "Shopify", "2026-04-01T09:00:00")]
        out = compute_uninstall_delta(cur, prev)
        assert out["counts"]["current"] == 3
        assert out["counts"]["previous"] == 1
        assert out["counts"]["new_uninstalls"] == 2
        assert sorted(r["seller_id"] for r in out["new_uninstalls"]) == ["2", "3"]

    def test_first_run_all_new(self):
        cur = [_uninstall("1", "Shopify", "2026-04-01T09:00:00")]
        out = compute_uninstall_delta(cur, [])
        assert out["counts"]["new_uninstalls"] == 1

    def test_previous_superset_no_new(self):
        # If the admin panel's historical log got shorter (archiving),
        # our delta should still be >= 0 — nothing new.
        cur = [_uninstall("1", "Shopify", "2026-04-01T09:00:00")]
        prev = [
            _uninstall("1", "Shopify", "2026-04-01T09:00:00"),
            _uninstall("2", "Shein", "2026-04-02T10:00:00"),
        ]
        out = compute_uninstall_delta(cur, prev)
        assert out["counts"]["new_uninstalls"] == 0

    def test_both_empty(self):
        out = compute_uninstall_delta([], [])
        assert out["counts"] == {"current": 0, "previous": 0, "new_uninstalls": 0}


# ---------------------------------------------------------------------
# compute_platform_split
# ---------------------------------------------------------------------

class TestComputePlatformSplit:
    def test_single_platform(self):
        rows = [_seller("1", platforms="Shopify") for _ in range(3)]
        assert compute_platform_split(rows) == {"Shopify": 3}

    def test_multi_platform_grouped(self):
        rows = [
            _seller("1", platforms="Shopify Temu"),
            _seller("2", platforms="Shopify Temu"),
            _seller("3", platforms="Shopify"),
        ]
        assert compute_platform_split(rows) == {"Shopify Temu": 2, "Shopify": 1}

    def test_collapses_whitespace_so_double_space_matches_single(self):
        # This is WHY _platform_slug runs " ".join(split()) — we saw the
        # admin panel render "Shopify  Temu" (2 spaces) in some rows.
        rows = [
            _seller("1", platforms="Shopify  Temu"),
            _seller("2", platforms="Shopify Temu"),
        ]
        assert compute_platform_split(rows) == {"Shopify Temu": 2}

    def test_none_goes_to_unknown(self):
        rows = [{"seller_id": "1", "platforms": None}]
        assert compute_platform_split(rows) == {"unknown": 1}

    def test_empty_list(self):
        assert compute_platform_split([]) == {}


# ---------------------------------------------------------------------
# build_report + totals
# ---------------------------------------------------------------------

class TestBuildReport:
    def test_first_run_totals(self):
        cur_sellers = {
            "shopify_temu": [_seller("1"), _seller("2")],
            "shein": [_seller("3")],
        }
        report = build_report(
            current_sellers_by_app=cur_sellers,
            previous_sellers_by_app={},
            current_uninstalls_by_app={},
            previous_uninstalls_by_app={},
            run_stamp="2026-04-18_12-00-00Z",
        )
        t = report["totals"]
        assert t["current_sellers"] == 3
        assert t["previous_sellers"] == 0
        assert t["new_installs"] == 3
        assert t["churned_sellers"] == 0
        assert t["net_growth"] == 3
        assert t["churn_rate"] == 0.0  # guarded against div-by-zero

    def test_mixed_totals_match_the_production_shape(self):
        # Mirrors the shape that the 2026-04-18_19-38-19Z run produced —
        # makes this suite a useful fingerprint if we accidentally break
        # the totals logic.
        cur = {
            "shopify_temu": [_seller(str(i)) for i in range(84)],
            "shein": [_seller(str(i + 1000)) for i in range(349)],
            "shopify_temu_eu": [_seller(str(i + 2000)) for i in range(41)],
        }
        prev = {
            "shopify_temu": [_seller(str(i)) for i in range(80)],
            "shein": [_seller(str(i + 1000)) for i in range(340)],
            "shopify_temu_eu": [_seller(str(i + 2000)) for i in range(35)],
        }
        report = build_report(
            current_sellers_by_app=cur,
            previous_sellers_by_app=prev,
            current_uninstalls_by_app={},
            previous_uninstalls_by_app={},
            run_stamp="2026-04-18_19-38-19Z",
        )
        t = report["totals"]
        assert t["current_sellers"] == 474
        assert t["previous_sellers"] == 455
        assert t["new_installs"] == 19    # 4 + 9 + 6
        assert t["churned_sellers"] == 0  # prev was a subset of cur
        assert t["net_growth"] == 19

    def test_churn_rate_nonzero(self):
        cur = {"a": [_seller("1"), _seller("2")]}
        prev = {"a": [_seller("2"), _seller("3"), _seller("4"), _seller("5")]}
        report = build_report(
            current_sellers_by_app=cur,
            previous_sellers_by_app=prev,
            current_uninstalls_by_app={},
            previous_uninstalls_by_app={},
            run_stamp="stamp",
        )
        # 3 churned out of 4 previous = 0.75
        assert pytest.approx(report["totals"]["churn_rate"], rel=1e-9) == 0.75
        assert report["apps"]["a"]["churn_rate"] == pytest.approx(0.75)

    def test_union_of_apps_across_current_and_previous(self):
        # An app that exists only in previous (completely churned) still
        # shows up in the per-app breakdown — we don't want silently
        # dropping an entire app off the radar.
        report = build_report(
            current_sellers_by_app={"a": [_seller("1")]},
            previous_sellers_by_app={"b": [_seller("2")]},
            current_uninstalls_by_app={},
            previous_uninstalls_by_app={},
            run_stamp="s",
        )
        assert set(report["apps"]) == {"a", "b"}
        assert report["apps"]["b"]["sellers"]["counts"]["churned_sellers"] == 1


# ---------------------------------------------------------------------
# flatten_to_metric_rows
# ---------------------------------------------------------------------

class TestFlattenToMetricRows:
    def test_emits_per_app_and_totals(self):
        report = build_report(
            current_sellers_by_app={"a": [_seller("1")]},
            previous_sellers_by_app={},
            current_uninstalls_by_app={},
            previous_uninstalls_by_app={},
            run_stamp="s",
        )
        rows = flatten_to_metric_rows(report)
        # At least one row per metric, per app, plus per-total.
        per_app_a = [r for r in rows if r["app_name"] == "a"]
        totals = [r for r in rows if r["app_name"] is None]
        assert len(per_app_a) > 0
        assert len(totals) > 0
        # All rows share the run_stamp
        assert all(r["run_stamp"] == "s" for r in rows)
        # Value is always a float — Supabase expects numeric
        assert all(isinstance(r["value"], float) for r in rows)

    def test_total_active_delta_is_signed(self):
        report = build_report(
            current_sellers_by_app={"a": [_seller("1"), _seller("2")]},
            previous_sellers_by_app={"a": [_seller("1")]},
            current_uninstalls_by_app={},
            previous_uninstalls_by_app={},
            run_stamp="s",
        )
        rows = flatten_to_metric_rows(report)
        total_active_totals = [
            r for r in rows if r["app_name"] is None and r["metric_name"] == "total_active"
        ]
        assert len(total_active_totals) == 1
        assert total_active_totals[0]["value"] == 2.0
        assert total_active_totals[0]["delta_from_previous"] == 1.0

    def test_platform_split_meta_preserved(self):
        report = build_report(
            current_sellers_by_app={
                "a": [_seller("1", platforms="Shopify"), _seller("2", platforms="Temu")]
            },
            previous_sellers_by_app={},
            current_uninstalls_by_app={},
            previous_uninstalls_by_app={},
            run_stamp="s",
        )
        rows = flatten_to_metric_rows(report)
        split_row = next(r for r in rows if r["metric_name"] == "platform_split" and r["app_name"] == "a")
        assert split_row["meta"] == {"split": {"Shopify": 1, "Temu": 1}}
        assert split_row["value"] == 2.0  # number of distinct combos


# ---------------------------------------------------------------------
# render_markdown_report
# ---------------------------------------------------------------------

class TestRenderMarkdownReport:
    def test_contains_totals_and_app_sections(self):
        report = build_report(
            current_sellers_by_app={"a": [_seller("1")]},
            previous_sellers_by_app={},
            current_uninstalls_by_app={},
            previous_uninstalls_by_app={},
            run_stamp="2026-04-18_12-00-00Z",
        )
        md = render_markdown_report(report)
        assert "# Seller Tracker — 2026-04-18_12-00-00Z" in md
        assert "## Totals" in md
        assert "## a" in md
        assert "Active sellers" in md


# ---------------------------------------------------------------------
# analyse_run (wrapper with safe defaults)
# ---------------------------------------------------------------------

class TestAnalyseRun:
    def test_defaults_none_to_empty(self):
        # Caller only needs to pass current_sellers + run_stamp; all
        # other args should default cleanly to empty dicts.
        report = analyse_run(
            current_sellers_by_app={"a": [_seller("1")]},
            run_stamp="s",
        )
        assert report["totals"]["current_sellers"] == 1
        assert report["totals"]["previous_sellers"] == 0
        assert report["totals"]["new_uninstalls"] == 0

    def test_returns_same_shape_as_build_report(self):
        report = analyse_run(
            current_sellers_by_app={"a": [_seller("1")]},
            previous_sellers_by_app={"a": [_seller("1")]},
            current_uninstalls_by_app={"a": []},
            previous_uninstalls_by_app={"a": []},
            run_stamp="s",
        )
        assert "apps" in report and "totals" in report
