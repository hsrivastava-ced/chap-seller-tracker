"""
seller_delta_source.py — fetch the prior + latest snapshots for an app
from either Supabase (primary) or local results/history/ (fallback).

Keeps the data-access logic out of seller_delta.py (pure compute) and
out of intelligence_ui.py (UI). Makes each piece testable + swappable.

Supabase source:
    Queries public.snapshots for (app_name=…, kind='sellers') ordered
    by created_at desc, takes the two most recent rows, returns
    (prior_run_stamp, latest_run_stamp, prior_rows, latest_rows).

Local source:
    Scans results/history/<stamp>/run.json sorted newest-first, loads
    the top two, pulls out the matching app's seller list.

Returns (None, None, [], []) in every path where we can't build a
comparison — caller renders "no prior snapshot" rather than crashing.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional


def from_supabase(
    supabase_client, *, app_name: str
) -> tuple[Optional[str], Optional[str], list[dict], list[dict]]:
    """Return (prior_stamp, latest_stamp, prior_rows, latest_rows)."""
    if supabase_client is None or getattr(supabase_client, "dry_run", True):
        return (None, None, [], [])
    try:
        # supabase-py's table() API — latest two rows for the app.
        resp = (
            supabase_client._client.table("snapshots")
            .select("*")
            .eq("kind", "sellers")
            .eq("app_name", app_name)
            .order("created_at", desc=True)
            .limit(2)
            .execute()
        )
    except Exception as err:
        logging.warning(f"delta source Supabase query failed: {err}")
        return (None, None, [], [])
    rows = list(getattr(resp, "data", None) or [])
    if len(rows) < 2:
        # Only have one snapshot — nothing to compare against yet.
        if rows:
            return (None, rows[0].get("run_stamp"), [], list(rows[0].get("raw_data") or []))
        return (None, None, [], [])
    latest, prior = rows[0], rows[1]
    return (
        prior.get("run_stamp"),
        latest.get("run_stamp"),
        list(prior.get("raw_data") or []),
        list(latest.get("raw_data") or []),
    )


def from_local_history(
    history_dir: Path, *, app_name: str
) -> tuple[Optional[str], Optional[str], list[dict], list[dict]]:
    """Fallback: read results/history/<stamp>/run.json sorted newest-first."""
    if not history_dir.exists():
        return (None, None, [], [])
    stamps = sorted(
        [d.name for d in history_dir.iterdir()
         if d.is_dir() and (d / "run.json").exists()],
        reverse=True,
    )
    if not stamps:
        return (None, None, [], [])
    if len(stamps) < 2:
        only = _load_app_rows(history_dir / stamps[0] / "run.json", app_name)
        return (None, stamps[0], [], only)
    latest = _load_app_rows(history_dir / stamps[0] / "run.json", app_name)
    prior = _load_app_rows(history_dir / stamps[1] / "run.json", app_name)
    return (stamps[1], stamps[0], prior, latest)


def _load_app_rows(path: Path, app_name: str) -> list[dict]:
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except Exception as err:
        logging.warning(f"delta source local load failed ({path}): {err}")
        return []
    return list((data.get("data") or {}).get(app_name) or [])
