"""
run_diagnostics.py — turn a workflow run + its log into something the
Admin UI can render as "what happened, what worked, what to do next".

Three public entry points:

    classify_failure(log_text)  → (code, message, suggested_action)
    per_app_status(run_stamp, registry, log_text=None)
                                → list[dict] one row per registered app
    row_delta(run_stamp)        → {app: {added, removed, sample_added, sample_removed}}

All three are pure functions (read from disk; no network, no
side-effects), so the admin UI can wrap them in `st.cache_data` freely.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from analytics import compute_seller_delta
from app_registry import AppEntry

RESULTS_DIR = Path(__file__).parent / "results"
HISTORY_DIR = RESULTS_DIR / "history"
LATEST_DIR = RESULTS_DIR / "latest"


# =====================================================================
# 1. Failure classifier
# =====================================================================

# Ordered: the first pattern to match wins. Tighter patterns first so a
# specific failure (push race) isn't shadowed by a generic one (any
# non-zero exit).
_VERDICT_RULES: list[tuple[re.Pattern[str], str, str, str]] = [
    (
        re.compile(r"!\s*\[rejected\].*fetch first|Updates were rejected because the remote contains work"),
        "push_rejected",
        "Push to main was rejected — another workflow committed first.",
        "Re-dispatch this scrape. The scraper's merge step will preserve "
        "any apps already promoted by the racing commit, so no data is lost.",
    ),
    (
        re.compile(r"OTP|One-time Passcode sent successfully", re.IGNORECASE),
        "login_otp",
        "cHAP login was blocked by a one-time passcode prompt.",
        "Ask the panel's dev to disable OTP for the scraper account, "
        "then click Retry sync on the Overview tab.",
    ),
    (
        re.compile(r"Login form rendered.*?Login not accepted|invalid (?:credentials|username|password)", re.IGNORECASE | re.DOTALL),
        "login_rejected",
        "cHAP rejected the username/password.",
        "Re-enter the panel's credentials via Admin → Add new app → "
        "Update credentials, then Retry sync.",
    ),
    (
        re.compile(r"Customize Grid: \d+ columns still absent from thead"),
        "customize_grid_partial",
        "cHAP's Customize Grid popup didn't reopen after some columns — "
        "the seller table is missing plan/order_count for at least one app.",
        "Usually a transient cHAP UI flake — re-dispatch the scrape. "
        "If it persists for the same app two runs in a row, inspect the "
        "popup behaviour in debug_dom_seller_customize_<app>.txt.",
    ),
    (
        re.compile(r"Validator blocked: \d+ sellers"),
        "validator_blocked",
        "Validator refused to promote this run — row count dropped vs the "
        "previous run by more than the threshold (likely pagination regression).",
        "Inspect results/staging/<stamp>/ for the blocked apps. If the drop "
        "is genuine (real churn) re-run; if pagination is broken, fix the "
        "scraper before the next cron.",
    ),
    (
        re.compile(r"No data gathered — aborting pipeline"),
        "no_data",
        "Pipeline aborted because the scraper returned zero sellers across "
        "every app.",
        "Check the runner log for the first 'ATTEMPTING' block — usually a "
        "cHAP-side outage or a credential rotation. Verify cHAP loads in a "
        "browser, then Retry sync.",
    ),
    (
        re.compile(r"playwright\._impl\._api_types\.TimeoutError|Timeout \d+ms exceeded", re.IGNORECASE),
        "playwright_timeout",
        "Playwright timed out waiting for a cHAP element.",
        "Usually a cHAP-side slowness or an A/B-test that moved a selector. "
        "Re-dispatch; if it repeats, check error_*.png artifacts.",
    ),
    (
        re.compile(r"Error: Process completed with exit code [1-9]"),
        "step_nonzero_exit",
        "A workflow step exited non-zero, but no known failure pattern "
        "matched the log.",
        "Click View on the run row to read the full GitHub Actions log.",
    ),
]


def classify_failure(log_text: str) -> tuple[str, str, str]:
    """Match the runner log against known failure patterns.

    Returns `(code, human_message, suggested_action)`.

    On a healthy run (no patterns hit, no error markers), returns
    `("success", ...)`. Callers that already know the run succeeded
    from `conclusion=="success"` can skip calling this.
    """
    if not log_text:
        return (
            "log_unavailable",
            "No log text was available to classify.",
            "Open the run on GitHub to read the raw log.",
        )

    for pattern, code, msg, action in _VERDICT_RULES:
        if pattern.search(log_text):
            return (code, msg, action)

    # Heuristic: any ERROR-level lines but no known pattern.
    if re.search(r"^\d{4}-\d{2}-\d{2}.*ERROR ", log_text, re.MULTILINE):
        return (
            "unknown_error",
            "The run logged ERROR-level messages but they didn't match a "
            "known failure pattern.",
            "Open the run on GitHub and search the log for 'ERROR'.",
        )

    return ("success", "Run completed without recognized errors.", "")


# =====================================================================
# 2. Per-app status (the "which app scraped, which preserved" table)
# =====================================================================

# Captures e.g. "⭐ SELLERS: 81 unique for shopify_temu (merged from 1 framework pass(es))."
_RE_SELLERS = re.compile(
    r"SELLERS: (\d+) unique for (\w+)"
)
# "🗑️  UNINSTALLS: Found 122 for shopify_temu."
_RE_UNINSTALLS = re.compile(
    r"UNINSTALLS:\s*Found (\d+) for (\w+)"
)
# "↳ merge: preserving michael (254 sellers) from previous latest snapshot"
_RE_PRESERVED = re.compile(
    r"merge: preserving (\w+) \((\d+) sellers\)"
)
# "--- ATTEMPTING: shopify_temu (shopify_temu) as ..."
_RE_ATTEMPTING = re.compile(
    r"ATTEMPTING:\s*(\w+)"
)


def _load_history_run(run_stamp: str) -> dict | None:
    path = HISTORY_DIR / run_stamp / "run.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_latest_run() -> dict | None:
    path = LATEST_DIR / "run.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def per_app_status(
    run_stamp: str,
    registry: Iterable[AppEntry],
    log_text: str | None = None,
) -> list[dict]:
    """Return one row per registered app describing what that app did in
    this run.

    Row schema:
        {
          "app": "shopify_temu",
          "label": "TEMU US",
          "status": "scraped" | "preserved" | "missed" | "attempted_failed",
          "rows": 81 | None,
          "plan_populated": "81/81" | "0/41" | None,
          "uninstalls": 122 | None,
          "error": "Login not accepted: …" | "",
          "source": "history" | "log" | "registry-only",
        }

    Data sources, in order of preference:
      1. `results/history/<run_stamp>/run.json` — authoritative.
      2. The runner log — for runs that crashed before history was written.
      3. The registry alone — last-resort "we know this app exists, no
         signal whether it ran".
    """
    rows: list[dict] = []
    registry_list = list(registry or [])
    registry_ids = {a.id for a in registry_list}

    history = _load_history_run(run_stamp)

    # Parse the log once if we have it — used as fallback AND to surface
    # "preserved" lines (which history alone can't tell us).
    log_parsed = _parse_log_counts(log_text or "")

    for app in registry_list:
        row = {
            "app": app.id,
            "label": app.label or app.id,
            "status": "missed",
            "rows": None,
            "plan_populated": None,
            "uninstalls": None,
            "error": "",
            "source": "registry-only",
        }

        # --- preferred: history snapshot ---
        if history:
            sellers = (history.get("data") or {}).get(app.id)
            unins = (history.get("uninstalls") or {}).get(app.id)
            err = (history.get("app_errors") or {}).get(app.id) or ""

            if sellers is not None:
                row["status"] = "scraped"
                row["rows"] = len(sellers)
                populated = sum(
                    1 for r in sellers if (r.get("plan") or "").strip()
                )
                row["plan_populated"] = f"{populated}/{len(sellers)}"
                row["source"] = "history"
            if unins is not None:
                row["uninstalls"] = len(unins)
                row["source"] = "history"
            if err:
                row["error"] = err
                # If we have an error AND no rows, the app attempted but
                # didn't yield data (login failure, mid-scrape crash).
                if row["status"] == "missed":
                    row["status"] = "attempted_failed"

        # --- fallback: log-derived ---
        if row["source"] == "registry-only" and log_parsed:
            if app.id in log_parsed["sellers"]:
                row["status"] = "scraped"
                row["rows"] = log_parsed["sellers"][app.id]
                row["source"] = "log"
            if app.id in log_parsed["uninstalls"]:
                row["uninstalls"] = log_parsed["uninstalls"][app.id]
                row["source"] = "log"
            if app.id in log_parsed["preserved"]:
                row["status"] = "preserved"
                row["rows"] = log_parsed["preserved"][app.id]
                row["source"] = "log"
            elif app.id in log_parsed["attempted_no_data"]:
                # We saw "ATTEMPTING: <app>" but no SELLERS line — login
                # likely failed or scraper crashed mid-app.
                row["status"] = "attempted_failed"
                row["source"] = "log"

        rows.append(row)

    # Append any apps that appeared in history but aren't in the active
    # registry — admin should still see them (recently deregistered).
    if history:
        for app_id in (history.get("data") or {}).keys():
            if app_id in registry_ids:
                continue
            sellers = history["data"][app_id]
            rows.append({
                "app": app_id,
                "label": f"{app_id} (deregistered)",
                "status": "scraped",
                "rows": len(sellers),
                "plan_populated": None,
                "uninstalls": None,
                "error": "",
                "source": "history",
            })

    return rows


def _parse_log_counts(log_text: str) -> dict:
    """Pull sellers / uninstalls / preserved / attempted-but-no-data counts
    out of the runner log. Used as a fallback when run.json is missing."""
    sellers: dict[str, int] = {}
    uninstalls: dict[str, int] = {}
    preserved: dict[str, int] = {}
    attempted: set[str] = set()

    if not log_text:
        return {
            "sellers": sellers,
            "uninstalls": uninstalls,
            "preserved": preserved,
            "attempted_no_data": set(),
        }

    for m in _RE_ATTEMPTING.finditer(log_text):
        attempted.add(m.group(1))
    for m in _RE_SELLERS.finditer(log_text):
        sellers[m.group(2)] = int(m.group(1))
    for m in _RE_UNINSTALLS.finditer(log_text):
        uninstalls[m.group(2)] = int(m.group(1))
    for m in _RE_PRESERVED.finditer(log_text):
        preserved[m.group(1)] = int(m.group(2))

    attempted_no_data = attempted - set(sellers.keys())
    return {
        "sellers": sellers,
        "uninstalls": uninstalls,
        "preserved": preserved,
        "attempted_no_data": attempted_no_data,
    }


# =====================================================================
# 3. Row-level delta vs prior successful run
# =====================================================================

def _list_history_stamps() -> list[str]:
    """Return sorted (oldest → newest) list of stamps that have a run.json."""
    if not HISTORY_DIR.exists():
        return []
    stamps = []
    for d in HISTORY_DIR.iterdir():
        if d.is_dir() and (d / "run.json").exists():
            stamps.append(d.name)
    return sorted(stamps)


def _previous_stamp(current: str) -> str | None:
    stamps = _list_history_stamps()
    if current not in stamps:
        # Caller asked about a stamp we don't have on disk — best we can
        # do is "the latest one" if any exists.
        return stamps[-1] if stamps else None
    idx = stamps.index(current)
    if idx == 0:
        return None
    return stamps[idx - 1]


def row_delta(run_stamp: str, sample_limit: int = 5) -> dict:
    """Compute per-app row delta vs the immediately-prior history snapshot.

    Returns:
        {
          app_id: {
            "added": int,
            "removed": int,
            "current": int,
            "previous": int,
            "sample_added": [{seller_id, email, store_url}, …],
            "sample_removed": [...],
          },
          ...
        }

    Quiet on missing data: if there's no prior snapshot, every current
    app reports `previous=0, added=current, removed=0` (matches the
    dashboard's "first run = all new" UX).
    """
    current = _load_history_run(run_stamp)
    if current is None:
        return {}

    prev_stamp = _previous_stamp(run_stamp)
    previous = _load_history_run(prev_stamp) if prev_stamp else None

    cur_sellers = (current.get("data") or {})
    prev_sellers = (previous.get("data") or {}) if previous else {}

    out: dict[str, dict] = {}
    apps = set(cur_sellers.keys()) | set(prev_sellers.keys())
    for app in sorted(apps):
        delta = compute_seller_delta(
            cur_sellers.get(app, []),
            prev_sellers.get(app, []),
        )

        def _slim(rows: list[dict]) -> list[dict]:
            return [
                {
                    "seller_id": r.get("seller_id", ""),
                    "email": r.get("email", ""),
                    "store_url": r.get("store_url", ""),
                }
                for r in rows[:sample_limit]
            ]

        out[app] = {
            "added": delta["counts"]["new_installs"],
            "removed": delta["counts"]["churned_sellers"],
            "current": delta["counts"]["current"],
            "previous": delta["counts"]["previous"],
            "sample_added": _slim(delta["new_installs"]),
            "sample_removed": _slim(delta["churned_sellers"]),
        }
    return out


def find_run_stamp_in_log(log_text: str) -> str | None:
    """Extract the run_stamp the scraper printed, if any.

    The scraper logs `run snapshot: results/history/<STAMP>/run.json`
    on success. That's the cleanest link from a GitHub workflow run to
    our history folder. Returns None if the line isn't in the log
    (e.g. run died before persist_results).
    """
    if not log_text:
        return None
    m = re.search(
        r"results/history/(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}Z)/run\.json",
        log_text,
    )
    if m:
        return m.group(1)
    # Fallback: any history-stamp-shaped string preceded by "run snapshot"
    m = re.search(
        r"run.*?(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}Z)",
        log_text,
    )
    return m.group(1) if m else None
