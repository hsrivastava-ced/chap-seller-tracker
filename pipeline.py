"""
End-to-end pipeline for the cHAP Seller Tracker (Phase 3).

Chain:
  1. scraper.main()                        → scrape all 3 apps
  2. supabase.push_snapshot(*)              → persist raw payload per app/kind
  3. supabase.fetch_latest_snapshots(...)   → pull previous snapshots (N=2)
  4. analytics.analyse_run(current, prev)   → compute deltas + KPIs
  5. supabase.push_metrics(...)             → persist KPI rows
  6. Write markdown delta report to disk    → results/reports/<stamp>.md

Entry points:
  python3 pipeline.py           # full end-to-end (scrape → push → analyse)
  python3 pipeline.py --replay <run_stamp>
                                # skip scrape; load from results/history/<stamp>
                                # and re-run analytics (great for dashboards
                                # backfill + local smoke-testing)
  python3 pipeline.py --dry-run # scrape but skip Supabase writes (still
                                # runs analytics + writes local report)

Design choices:
- Scraper stays pure: we call it as a function, not as a subprocess.
- Supabase writes are always "insert" (never upsert) — each run is an
  immutable snapshot, and we pull the latest rows by `created_at desc`.
- If Supabase is unreachable the pipeline still writes the markdown
  report and does NOT crash (we already have CSV+JSON on disk).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from analytics import analyse_run, flatten_to_metric_rows, render_markdown_report
from normalize import normalize_run_data
from supabase_client import SupabaseClient, utc_stamp

RESULTS_DIR = Path(__file__).parent / "results"
HISTORY_DIR = RESULTS_DIR / "history"
REPORTS_DIR = RESULTS_DIR / "reports"


# ---------------------------------------------------------------------
# Log setup
# ---------------------------------------------------------------------

def _setup_logging():
    # Keep the format identical to scraper.py so concatenated logs read
    # as one continuous stream.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


# ---------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------

def _load_run_json(run_stamp: str) -> dict[str, Any]:
    """Read results/history/<stamp>/run.json and return its parsed body.

    This lets us replay a past run through the analytics + push pipeline
    without re-scraping. Useful for:
      - backfilling Supabase from disk snapshots
      - sandbox smoke-testing (we can't re-scrape without live egress)
    """
    path = HISTORY_DIR / run_stamp / "run.json"
    if not path.exists():
        raise FileNotFoundError(f"No run snapshot at {path}")
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_previous_from_supabase(
    client: SupabaseClient,
    current_stamp: str,
    *,
    apps: list[str] | None = None,
) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Return (previous_sellers_by_app, previous_uninstalls_by_app) from
    the most recent snapshot in Supabase whose run_stamp != current_stamp.

    `apps` defaults to the union of apps in apps.yaml — passing the list
    explicitly (from this run's actually-scraped app keys) is preferred
    so we never silently skip an app that was onboarded after this file
    was last touched.

    Logs a one-line diagnostic per (app, kind) showing rows fetched +
    rows picked — without this it's impossible to tell from CI logs
    whether the diff baseline came back empty because pushes were
    blocked (RLS / anon key) or because it's a genuinely-first run.
    """
    prev_sellers: dict[str, list[dict]] = {}
    prev_unins: dict[str, list[dict]] = {}

    if apps is None:
        apps = _apps_from_yaml() or [
            "shopify_temu", "shein", "shopify_temu_eu", "shein_woocommerce",
        ]

    total_rows_seen = 0
    total_buckets_filled = 0
    for app in apps:
        for kind, bucket in (("sellers", prev_sellers), ("uninstalls", prev_unins)):
            rows = client.fetch_latest_snapshots(
                kind=kind, app_name=app, limit=2,
            )
            total_rows_seen += len(rows)
            # Skip the row that matches the run we just pushed.
            picked = next(
                (r for r in rows if r.get("run_stamp") != current_stamp),
                None,
            )
            if picked:
                bucket[app] = picked.get("raw_data") or []
                if bucket[app]:
                    total_buckets_filled += 1
            logging.info(
                f"⏮  previous {kind:>10} for {app:<22}: "
                f"fetched {len(rows)} row(s), "
                f"picked={picked is not None}, "
                f"raw_rows={(len(picked.get('raw_data') or []) if picked else 0)}"
            )

    if total_rows_seen == 0 and not client.dry_run:
        logging.warning(
            "⚠️  Supabase returned ZERO previous-snapshot rows across "
            "all (app, kind) pairs. Diff vs previous will treat every "
            "current row as a new install, every previous row as "
            "uninstalled — bogus deltas. Likely causes (most → least): "
            "(1) SUPABASE_KEY is the anon key and snapshots-table RLS "
            "blocks anonymous reads — switch to a service_role key; "
            "(2) the snapshots table is empty because pushes have been "
            "failing silently (check 'Wrote N/M snapshot row(s)' line "
            "above this in the log)."
        )
    elif total_buckets_filled == 0 and not client.dry_run:
        logging.warning(
            "⚠️  Fetched %d previous-snapshot row(s) but ZERO of them "
            "had usable raw_data — diff will treat everyone as new. "
            "Inspect public.snapshots in Supabase: the rows likely have "
            "raw_data = null because earlier push_snapshot calls only "
            "wrote metadata.",
            total_rows_seen,
        )
    else:
        logging.info(
            f"📚 Loaded previous snapshots: {total_buckets_filled} "
            f"non-empty (app, kind) bucket(s) from {total_rows_seen} "
            f"fetched row(s)."
        )

    return prev_sellers, prev_unins


def _apps_from_yaml() -> list[str]:
    """Read app ids from apps.yaml, in declaration order. Returns [] on
    any failure (caller falls back to a hardcoded list)."""
    try:
        import yaml
        with open(Path(__file__).parent / "apps.yaml", "r") as fh:
            doc = yaml.safe_load(fh) or {}
        return [a["id"] for a in (doc.get("apps") or []) if a.get("id")]
    except Exception as err:
        logging.debug(f"_apps_from_yaml failed: {err}")
        return []


# ---------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------

def _write_markdown_report(report: dict, run_stamp: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{run_stamp}.md"
    path.write_text(render_markdown_report(report), encoding="utf-8")
    return path


def _push_all_snapshots(
    client: SupabaseClient,
    *,
    sellers_by_app: dict[str, list[dict]],
    uninstalls_by_app: dict[str, list[dict]],
    run_stamp: str,
) -> dict[str, int]:
    """Push one snapshots row per (app, kind). Returns a {label: count}
    map of how many rows Supabase accepted — useful for detecting RLS
    rejections (count will be 0 when the anon key is blocked)."""
    written: dict[str, int] = {}
    for app, rows in sorted(sellers_by_app.items()):
        written[f"{app}.sellers"] = client.push_snapshot(
            app_name=app, kind="sellers", rows=rows, run_stamp=run_stamp,
        )
        # Also upsert into the relational public.sellers table (Task #80)
        # so the dashboard can query per-seller state and preserve manual
        # edits across scrapes. Failure here must NOT abort — the jsonb
        # snapshot above is authoritative; the relational projection is
        # a convenience.
        try:
            written[f"{app}.sellers.relational"] = client.upsert_sellers(
                app_name=app, rows=rows, run_stamp=run_stamp,
            )
        except Exception as err:
            logging.warning(
                f"upsert_sellers({app}) failed; snapshot already persisted. {err}"
            )
            written[f"{app}.sellers.relational"] = 0
    for app, rows in sorted(uninstalls_by_app.items()):
        written[f"{app}.uninstalls"] = client.push_snapshot(
            app_name=app, kind="uninstalls", rows=rows, run_stamp=run_stamp,
        )
    return written


# ---------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------

def run_pipeline(
    *,
    replay_stamp: str | None = None,
    dry_run: bool = False,
    previous_from_disk: str | None = None,
) -> dict[str, Any]:
    """Main pipeline. Returns the report dict so programmatic callers
    (cron wrapper, tests) can inspect the outcome without parsing logs.

    Parameters
    ----------
    replay_stamp
        If set, skip the scraper and load current-run data from
        results/history/<replay_stamp>/run.json instead.
    dry_run
        If True, skip Supabase pushes; still fetch previous data (from
        disk or empty), run analytics, and write the markdown report.
    previous_from_disk
        If set, also load the PREVIOUS run's data from disk rather than
        Supabase. Useful for local smoke-testing when Supabase is empty
        or unreachable.
    """
    current_stamp = utc_stamp()

    # 1. Gather current data (scrape live OR replay from disk).
    if replay_stamp:
        logging.info(f"🔁 Replaying run snapshot {replay_stamp} (no scrape)")
        payload = _load_run_json(replay_stamp)
        current_stamp = payload.get("run_stamp", current_stamp)
        sellers_by_app = payload.get("data", {}) or {}
        uninstalls_by_app = payload.get("uninstalls_data", {}) or {}
        # scraper's run.json uses "data" for sellers; uninstalls may be
        # in "uninstalls_data" or inline — normalise below.
        if not uninstalls_by_app:
            uninstalls_by_app = payload.get("uninstalls", {}) or {}
    else:
        # Deferred import so sandbox environments without Playwright can
        # still import pipeline.py (e.g. to run `--replay`).
        from scraper import main as scraper_main

        logging.info("🏁 Starting live scrape")
        scrape_out = scraper_main() or {}
        sellers_by_app = scrape_out.get("sellers", {}) or {}
        uninstalls_by_app = scrape_out.get("uninstalls", {}) or {}

    if not sellers_by_app and not uninstalls_by_app:
        logging.error("🚫 No data gathered — aborting pipeline.")
        return {"error": "no_data"}

    # 2. Push current snapshots to Supabase (unless dry-run).
    client = SupabaseClient()
    snapshot_writes: dict[str, int] = {}
    if not dry_run:
        snapshot_writes = _push_all_snapshots(
            client,
            sellers_by_app=sellers_by_app,
            uninstalls_by_app=uninstalls_by_app,
            run_stamp=current_stamp,
        )
        total_written = sum(snapshot_writes.values())
        if total_written == 0 and not client.dry_run:
            logging.warning(
                "⚠️  0 snapshot rows written to Supabase — likely blocked by "
                "RLS. Continuing with analytics against local/prev data; "
                "consider swapping SUPABASE_KEY for a service_role key."
            )
        else:
            logging.info(
                f"📥 Wrote {total_written} snapshot row(s) across "
                f"{len(snapshot_writes)} (app,kind) pairs."
            )

    # 3. Pull previous snapshots for delta.
    prev_sellers: dict[str, list[dict]] = {}
    prev_unins: dict[str, list[dict]] = {}
    if previous_from_disk:
        logging.info(
            f"⏪ Loading previous from disk snapshot {previous_from_disk}"
        )
        try:
            prev_payload = _load_run_json(previous_from_disk)
            prev_sellers = prev_payload.get("data", {}) or {}
            prev_unins = (
                prev_payload.get("uninstalls_data")
                or prev_payload.get("uninstalls")
                or {}
            )
        except Exception as err:
            logging.warning(f"Could not load disk previous: {err}")
    elif not dry_run:
        prev_sellers, prev_unins = _load_previous_from_supabase(
            client, current_stamp,
            # Pass the apps we actually scraped so an app onboarded after
            # this file was last touched still gets its diff baseline.
            apps=sorted(
                set(sellers_by_app.keys()) | set(uninstalls_by_app.keys())
            ),
        )

    # 4. Normalise + compute deltas + KPIs.
    # Normalise BOTH current and previous so URL-casing / trailing-slash /
    # date-format drift between runs doesn't show up as spurious churn or
    # new_install. The scraper writes raw values to disk on purpose
    # (we want the on-disk snapshot to be faithful to what the admin
    # panel rendered), so normalisation is applied here in-memory.
    sellers_by_app, uninstalls_by_app = normalize_run_data(
        sellers_by_app, uninstalls_by_app
    )
    prev_sellers, prev_unins = normalize_run_data(prev_sellers, prev_unins)

    report = analyse_run(
        current_sellers_by_app=sellers_by_app,
        previous_sellers_by_app=prev_sellers,
        current_uninstalls_by_app=uninstalls_by_app,
        previous_uninstalls_by_app=prev_unins,
        run_stamp=current_stamp,
    )

    # 5. Push metrics to Supabase.
    if not dry_run:
        metric_rows = flatten_to_metric_rows(report)
        written = client.push_metrics(metric_rows)
        logging.info(
            f"📐 Wrote {written}/{len(metric_rows)} metric row(s) to Supabase."
        )

    # 6. Write local markdown report.
    report_path = _write_markdown_report(report, current_stamp)
    logging.info(f"📝 Markdown report: {report_path}")

    return {
        "report": report,
        "snapshot_writes": snapshot_writes,
        "report_path": str(report_path),
    }


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def _cli():
    parser = argparse.ArgumentParser(description="cHAP Seller Tracker pipeline")
    parser.add_argument(
        "--replay",
        dest="replay_stamp",
        default=None,
        help=(
            "Run analytics on a past run snapshot instead of scraping. "
            "Value should match a directory name under results/history/."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Supabase writes; still run analytics and local report.",
    )
    parser.add_argument(
        "--previous-from-disk",
        dest="previous_from_disk",
        default=None,
        help=(
            "Use results/history/<STAMP>/run.json as the 'previous' "
            "snapshot (instead of pulling from Supabase)."
        ),
    )
    args = parser.parse_args()

    _setup_logging()
    out = run_pipeline(
        replay_stamp=args.replay_stamp,
        dry_run=args.dry_run,
        previous_from_disk=args.previous_from_disk,
    )
    if "error" in out:
        sys.exit(1)


if __name__ == "__main__":
    _cli()
