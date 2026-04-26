"""
seller_delta_source.py — fetch the prior + latest snapshots for an app
from Supabase (primary), git history of results/latest/run.json
(secondary), or local results/history/ (final fallback).

Keeps the data-access logic out of seller_delta.py (pure compute) and
out of intelligence_ui.py (UI). Makes each piece testable + swappable.

Supabase source:
    Queries public.snapshots for (app_name=…, kind='sellers') ordered
    by created_at desc, takes the two most recent rows.

Git source:
    Walks `git log` for `results/latest/run.json` and pulls the two
    most-recent versions of the file (current HEAD + previous commit).
    Works on Streamlit Cloud without any external service since the
    deployment is itself a git checkout. The whole point of this
    source: avoid an empty delta feed when Supabase isn't wired up
    (or when its first snapshot is still pending).

Local history source:
    Scans results/history/<stamp>/run.json sorted newest-first.
    Used only on dev machines where the scraper has been run locally.

All sources return (prior_stamp, latest_stamp, prior_rows, latest_rows)
with (None, None, [], []) when no comparison can be built — caller
renders "no prior snapshot" rather than crashing.
"""
from __future__ import annotations

import json
import logging
import subprocess
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


def from_git_history(
    repo_root: Path, *, app_name: str, run_json_rel: str = "results/latest/run.json",
) -> tuple[Optional[str], Optional[str], list[dict], list[dict]]:
    """Pull the two most-recent committed versions of run.json from git.

    Why this source exists: the dashboard's "What changed" feed needs
    two snapshots to diff. Supabase is the long-term plan, but until
    its `snapshots` table has 2+ rows the feed is silent. Meanwhile
    the repo itself stores every successful scrape's run.json (the
    `chore(data): scrape …` commits the scraper bot pushes). Reading
    those two commits gives us a working delta feed immediately,
    without any external service, and it stays accurate even if
    Supabase later goes down.

    The subprocess call is small + read-only. We bound output to two
    commits each so even on a long-running repo this is microseconds.
    """
    try:
        # Filter to scraper-bot commits only — `chore(data): scrape …`.
        # Other commits to run.json (manual fixes, merges, restores)
        # don't represent a fresh scrape and would surface zero deltas.
        # We pull more than 2 since human commits may interleave.
        out = subprocess.run(
            ["git", "log", "-n", "20", "--pretty=%H %s",
             "--", run_json_rel],
            cwd=str(repo_root),
            check=True, capture_output=True, text=True, timeout=8,
        )
    except (FileNotFoundError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as err:
        logging.warning(f"delta source git log failed: {err}")
        return (None, None, [], [])

    scrape_shas: list[str] = []
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        sha, _, msg = line.partition(" ")
        if msg.startswith("chore(data): scrape"):
            scrape_shas.append(sha)
        if len(scrape_shas) >= 2:
            break

    if not scrape_shas:
        return (None, None, [], [])

    def _read_at(sha: str) -> tuple[Optional[str], list[dict]]:
        try:
            blob = subprocess.run(
                ["git", "show", f"{sha}:{run_json_rel}"],
                cwd=str(repo_root),
                check=True, capture_output=True, text=True, timeout=8,
            ).stdout
            payload = json.loads(blob)
            stamp = payload.get("run_stamp")
            rows = list((payload.get("data") or {}).get(app_name) or [])
            return (stamp, rows)
        except Exception as err:
            logging.warning(f"delta source git show failed for {sha}: {err}")
            return (None, [])

    latest_stamp, latest_rows = _read_at(scrape_shas[0])
    if len(scrape_shas) < 2:
        # Only one scrape commit so far — no prior to compare against.
        return (None, latest_stamp, [], latest_rows)

    prior_stamp, prior_rows = _read_at(scrape_shas[1])
    return (prior_stamp, latest_stamp, prior_rows, latest_rows)


def from_git_history_uninstalls(
    repo_root: Path, *, app_name: str, run_json_rel: str = "results/latest/run.json",
) -> tuple[Optional[str], Optional[str], list[dict], list[dict]]:
    """Same shape as from_git_history but pulls the `uninstalls` map for
    an app. Lets the UI surface "new uninstalls since yesterday" — the
    most actionable lead-quality signal in the whole tracker.

    Reuses the git plumbing in from_git_history; just reads a different
    key out of the run.json payload.
    """
    try:
        out = subprocess.run(
            ["git", "log", "-n", "20", "--pretty=%H %s",
             "--", run_json_rel],
            cwd=str(repo_root),
            check=True, capture_output=True, text=True, timeout=8,
        )
    except Exception as err:
        logging.warning(f"delta source git log (uninstalls) failed: {err}")
        return (None, None, [], [])

    scrape_shas: list[str] = []
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        sha, _, msg = line.partition(" ")
        if msg.startswith("chore(data): scrape"):
            scrape_shas.append(sha)
        if len(scrape_shas) >= 2:
            break

    if not scrape_shas:
        return (None, None, [], [])

    def _read_at(sha: str) -> tuple[Optional[str], list[dict]]:
        try:
            blob = subprocess.run(
                ["git", "show", f"{sha}:{run_json_rel}"],
                cwd=str(repo_root),
                check=True, capture_output=True, text=True, timeout=8,
            ).stdout
            payload = json.loads(blob)
            stamp = payload.get("run_stamp")
            rows = list((payload.get("uninstalls") or {}).get(app_name) or [])
            return (stamp, rows)
        except Exception as err:
            logging.warning(f"delta source git show (uninstalls) failed for {sha}: {err}")
            return (None, [])

    latest_stamp, latest_rows = _read_at(scrape_shas[0])
    if len(scrape_shas) < 2:
        return (None, latest_stamp, [], latest_rows)
    prior_stamp, prior_rows = _read_at(scrape_shas[1])
    return (prior_stamp, latest_stamp, prior_rows, latest_rows)
