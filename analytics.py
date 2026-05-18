"""
Delta detection and KPI computation for the cHAP Seller Tracker.

Given the current scrape output (same shape as scraper.main() returns) and
a previous snapshot (pulled back from Supabase), compute:

  1. Per-app seller deltas
       - new_installs      : seller_ids in current \\ previous
       - churned_sellers   : seller_ids in previous \\ current
       - retained_sellers  : seller_ids in current ∩ previous
  2. Per-app uninstall deltas
       - new_uninstalls    : (seller_id, platform, uninstalled_on) tuples
                             in current \\ previous
  3. Headline KPIs suitable for the `metrics` table
       - total_active, by_platform_split, churn_rate, install_velocity, ...

The output is both:
  - a structured Python report dict (used by pipeline.py + dashboard)
  - a flat list of metric rows ready for supabase_client.push_metrics()

None of this talks to Supabase directly — analytics.py is pure, so unit
tests and sandbox smoke-tests can exercise it without network access.
"""

from __future__ import annotations

import logging
from typing import Any

# Uninstall-dedup key: (seller_id, platform, uninstalled_on). Timestamp
# alone is not unique (two sellers can uninstall the same minute), and
# seller_id alone is not unique (same seller can uninstall Shopify AND
# Temu at different times).
_UNINSTALL_KEY_FIELDS = ("seller_id", "platform", "uninstalled_on")


# ---------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------

def _seller_id_set(rows: list[dict]) -> set[str]:
    ids: set[str] = set()
    for r in rows or []:
        sid = r.get("seller_id")
        if sid:
            ids.add(sid)
    return ids


def _rows_by_seller_id(rows: list[dict]) -> dict[str, dict]:
    """Build a seller_id → row lookup. Preserves the LAST row when
    duplicates exist (shouldn't happen after scraper dedup, but we
    defend against it)."""
    out: dict[str, dict] = {}
    for r in rows or []:
        sid = r.get("seller_id")
        if sid:
            out[sid] = r
    return out


def _uninstall_key(row: dict) -> tuple:
    return tuple(row.get(k, "") for k in _UNINSTALL_KEY_FIELDS)


def _platform_slug(platforms: str | None) -> str:
    """Canonicalise the seller's 'platforms' column. Preserves the raw
    casing but trims whitespace and collapses consecutive spaces so
    "Shopify  Temu" and "Shopify Temu" group together."""
    if not platforms:
        return "unknown"
    return " ".join(platforms.split())


# ---------------------------------------------------------------------
# Per-app delta computation
# ---------------------------------------------------------------------

def compute_seller_delta(
    current_rows: list[dict],
    previous_rows: list[dict],
) -> dict[str, Any]:
    """Return a delta dict for one app's seller list.

    First-run behaviour: if `previous_rows` is empty, every current row
    counts as new_installs (by definition). This matches the UX we want
    on the dashboard — the first post-install run shows every seller as
    "new".
    """
    current_ids = _seller_id_set(current_rows)
    previous_ids = _seller_id_set(previous_rows)

    new_ids = current_ids - previous_ids
    churned_ids = previous_ids - current_ids
    retained_ids = current_ids & previous_ids

    cur_lookup = _rows_by_seller_id(current_rows)
    prev_lookup = _rows_by_seller_id(previous_rows)

    def _rows(ids: set[str], lookup: dict[str, dict]) -> list[dict]:
        # Stable order: sort by seller_id so diff output is deterministic
        # (important for snapshot-diffing the markdown report between runs).
        return [lookup[i] for i in sorted(ids) if i in lookup]

    return {
        "counts": {
            "current": len(current_ids),
            "previous": len(previous_ids),
            "new_installs": len(new_ids),
            "churned_sellers": len(churned_ids),
            "retained_sellers": len(retained_ids),
        },
        "new_installs": _rows(new_ids, cur_lookup),
        "churned_sellers": _rows(churned_ids, prev_lookup),
        "retained_sellers": sorted(retained_ids),  # ids only — the rows are in current
    }


def compute_uninstall_delta(
    current_rows: list[dict],
    previous_rows: list[dict],
) -> dict[str, Any]:
    """Delta for one app's uninstalls table. The admin panel shows the
    *historical* uninstall log, so "new uninstalls since last run" is
    current_keys \\ previous_keys on (seller_id, platform, uninstalled_on).
    """
    current_keys = {_uninstall_key(r) for r in current_rows or []}
    previous_keys = {_uninstall_key(r) for r in previous_rows or []}
    new_keys = current_keys - previous_keys

    # Return the ROWS, not just the keys — caller usually wants email/url too.
    key_to_row = {_uninstall_key(r): r for r in current_rows or []}
    new_rows = [key_to_row[k] for k in sorted(new_keys) if k in key_to_row]

    return {
        "counts": {
            "current": len(current_keys),
            "previous": len(previous_keys),
            "new_uninstalls": len(new_keys),
        },
        "new_uninstalls": new_rows,
    }


# ---------------------------------------------------------------------
# Headline report (cross-app)
# ---------------------------------------------------------------------

def compute_platform_split(rows: list[dict]) -> dict[str, int]:
    """Count sellers per platform combo. `platforms` is a free-text cell
    like "Shopify Temu" or "Shopify Shein Amazon"; we treat the full
    string as the group key (a seller with multiple platforms shows up
    under the combo, not double-counted)."""
    split: dict[str, int] = {}
    for r in rows or []:
        p = _platform_slug(r.get("platforms"))
        split[p] = split.get(p, 0) + 1
    return split


def build_report(
    current_sellers_by_app: dict[str, list[dict]],
    previous_sellers_by_app: dict[str, list[dict]],
    current_uninstalls_by_app: dict[str, list[dict]],
    previous_uninstalls_by_app: dict[str, list[dict]],
    *,
    run_stamp: str,
) -> dict[str, Any]:
    """Compute the full report dict. The shape is designed for two
    consumers:
      - pipeline.py           (writes markdown + pushes metrics)
      - streamlit dashboard   (renders KPI cards + tables)
    """
    apps = sorted(
        set(current_sellers_by_app)
        | set(previous_sellers_by_app)
        | set(current_uninstalls_by_app)
        | set(previous_uninstalls_by_app)
    )

    per_app: dict[str, dict[str, Any]] = {}
    for app in apps:
        cur_sellers = current_sellers_by_app.get(app, [])
        prev_sellers = previous_sellers_by_app.get(app, [])
        cur_unins = current_uninstalls_by_app.get(app, [])
        prev_unins = previous_uninstalls_by_app.get(app, [])

        seller_delta = compute_seller_delta(cur_sellers, prev_sellers)
        unins_delta = compute_uninstall_delta(cur_unins, prev_unins)
        platform_split = compute_platform_split(cur_sellers)

        # Churn rate = churned / previous_total. Guard against div-by-zero
        # (first run has previous_total == 0).
        prev_total = seller_delta["counts"]["previous"]
        churn_rate = (
            seller_delta["counts"]["churned_sellers"] / prev_total
            if prev_total
            else 0.0
        )

        per_app[app] = {
            "sellers": seller_delta,
            "uninstalls": unins_delta,
            "platform_split": platform_split,
            "churn_rate": churn_rate,
        }

    # Totals across apps
    total_current = sum(per_app[a]["sellers"]["counts"]["current"] for a in per_app)
    total_previous = sum(per_app[a]["sellers"]["counts"]["previous"] for a in per_app)
    total_new = sum(per_app[a]["sellers"]["counts"]["new_installs"] for a in per_app)
    total_churned = sum(
        per_app[a]["sellers"]["counts"]["churned_sellers"] for a in per_app
    )
    total_new_unins = sum(
        per_app[a]["uninstalls"]["counts"]["new_uninstalls"] for a in per_app
    )

    return {
        "run_stamp": run_stamp,
        "apps": per_app,
        "totals": {
            "current_sellers": total_current,
            "previous_sellers": total_previous,
            "new_installs": total_new,
            "churned_sellers": total_churned,
            "new_uninstalls": total_new_unins,
            "net_growth": total_new - total_churned,
            "churn_rate": (
                total_churned / total_previous if total_previous else 0.0
            ),
        },
    }


# ---------------------------------------------------------------------
# Metric rows (for supabase.push_metrics)
# ---------------------------------------------------------------------

def flatten_to_metric_rows(report: dict[str, Any]) -> list[dict]:
    """Convert a report dict into rows ready for the `metrics` table.
    Each row: {run_stamp, app_name, metric_name, value, delta_from_previous, meta}.

    We emit one row per (app_name, metric_name) pair. Cross-app totals
    are emitted with app_name=None (the DDL allows NULL).
    """
    run_stamp = report["run_stamp"]
    rows: list[dict] = []

    def _row(app: str | None, name: str, value: float, *, delta=None, meta=None):
        rows.append(
            {
                "run_stamp": run_stamp,
                "app_name": app,
                "metric_name": name,
                "value": float(value),
                "delta_from_previous": (
                    float(delta) if delta is not None else None
                ),
                "meta": meta,
            }
        )

    # Per-app KPIs
    for app, data in report["apps"].items():
        c = data["sellers"]["counts"]
        u = data["uninstalls"]["counts"]

        _row(app, "total_active", c["current"], delta=c["current"] - c["previous"])
        _row(app, "new_installs", c["new_installs"])
        _row(app, "churned_sellers", c["churned_sellers"])
        _row(app, "retained_sellers", c["retained_sellers"])
        _row(app, "churn_rate", data["churn_rate"])
        _row(app, "new_uninstalls", u["new_uninstalls"])
        _row(
            app,
            "platform_split",
            # Single-number summary: how many distinct platform combos the
            # app has. The actual breakdown is in meta, where the dashboard
            # can render it as-is.
            len(data["platform_split"]),
            meta={"split": data["platform_split"]},
        )

    # Cross-app totals
    t = report["totals"]
    _row(
        None,
        "total_active",
        t["current_sellers"],
        delta=t["current_sellers"] - t["previous_sellers"],
    )
    _row(None, "new_installs", t["new_installs"])
    _row(None, "churned_sellers", t["churned_sellers"])
    _row(None, "new_uninstalls", t["new_uninstalls"])
    _row(None, "net_growth", t["net_growth"])
    _row(None, "churn_rate", t["churn_rate"])

    return rows


# ---------------------------------------------------------------------
# Markdown renderer (for quick-glance local reports)
# ---------------------------------------------------------------------

def render_markdown_report(report: dict[str, Any]) -> str:
    """Human-readable summary. Paste-friendly into Slack / email. We deliberately
    keep this terse — the full detail lives in Supabase and the JSON run
    snapshot on disk."""
    t = report["totals"]
    # Heuristic: if EVERY current seller looks like a new install AND
    # NOTHING churned, the previous-snapshot baseline almost certainly
    # came back empty (RLS / push failure / first run). Surface the
    # marker in the report itself so a reader doesn't draw growth
    # conclusions from a degenerate diff.
    stale_diff = (
        t["previous_sellers"] == 0
        and t["current_sellers"] > 0
        and t["new_installs"] == t["current_sellers"]
        and t["churned_sellers"] == 0
    )
    lines = [
        f"# Seller Tracker — {report['run_stamp']}",
        "",
    ]
    if stale_diff:
        lines.extend([
            "> ⚠️  **Stale diff** — previous-snapshot baseline was empty, "
            "so every current seller looks 'new' and the churn/growth "
            "numbers below are NOT real growth. Likely cause: Supabase "
            "snapshot reads are returning 0 rows (RLS or anon key). "
            "Check the CI log for the `_load_previous_from_supabase` "
            "diagnostic lines.",
            "",
        ])
    lines.extend([
        "## Totals",
        f"- Active sellers (all apps): **{t['current_sellers']}** "
        f"(Δ vs previous: {t['current_sellers'] - t['previous_sellers']:+d})",
        f"- New installs: **{t['new_installs']}**",
        f"- Churned sellers: **{t['churned_sellers']}**",
        f"- Net growth: **{t['net_growth']:+d}**",
        f"- New uninstalls: **{t['new_uninstalls']}**",
        f"- Churn rate: **{t['churn_rate']:.2%}**",
        "",
    ])
    for app, data in report["apps"].items():
        c = data["sellers"]["counts"]
        u = data["uninstalls"]["counts"]
        lines.append(f"## {app}")
        lines.append(
            f"- Sellers: {c['current']} current "
            f"(new +{c['new_installs']} / churned −{c['churned_sellers']})"
        )
        lines.append(f"- New uninstalls: {u['new_uninstalls']}")
        lines.append("- Platforms:")
        for platform, count in sorted(
            data["platform_split"].items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"    - {platform}: {count}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------
# Convenience entry point (used by pipeline.py)
# ---------------------------------------------------------------------

def analyse_run(
    *,
    current_sellers_by_app: dict[str, list[dict]],
    previous_sellers_by_app: dict[str, list[dict]] | None = None,
    current_uninstalls_by_app: dict[str, list[dict]] | None = None,
    previous_uninstalls_by_app: dict[str, list[dict]] | None = None,
    run_stamp: str,
) -> dict[str, Any]:
    """Glue: wraps build_report with safe defaults for missing previous
    snapshots (first-run of the pipeline)."""
    previous_sellers_by_app = previous_sellers_by_app or {}
    current_uninstalls_by_app = current_uninstalls_by_app or {}
    previous_uninstalls_by_app = previous_uninstalls_by_app or {}

    report = build_report(
        current_sellers_by_app=current_sellers_by_app,
        previous_sellers_by_app=previous_sellers_by_app,
        current_uninstalls_by_app=current_uninstalls_by_app,
        previous_uninstalls_by_app=previous_uninstalls_by_app,
        run_stamp=run_stamp,
    )
    logging.info(
        "📊 Analysis complete: "
        f"+{report['totals']['new_installs']} new / "
        f"-{report['totals']['churned_sellers']} churned / "
        f"{report['totals']['new_uninstalls']} new uninstalls."
    )
    return report
