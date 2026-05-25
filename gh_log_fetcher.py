"""
gh_log_fetcher.py — download GitHub Actions workflow run logs.

Used by the Admin → Runs tab to surface a log tail inline so a super
admin can tell *why* a red run died without bouncing to github.com.

The GitHub REST API returns workflow run logs as a zip archive:

    GET /repos/{owner}/{repo}/actions/runs/{run_id}/logs
        → 302 redirect to a short-lived blob URL
        → blob content = zip with one .txt per step

We unzip in memory, return `{step_name: log_text}`. The admin UI then
picks the "Scrape and commit" step (or whichever step actually ran the
scraper) and shows the tail.

Notes
-----
- The endpoint requires `Actions: Read` on the PAT — already granted
  for the existing Runs tab, no new scope to ask for.
- Logs are retained 90 days by default. For older runs the API returns
  410 Gone; caller treats that as "log unavailable" rather than an error.
- One run's full log can be a few MB. We don't stream — Streamlit only
  needs the last ~150 lines anyway and the zip itself is ≤10 MB even for
  long scrape runs.
"""
from __future__ import annotations

import io
import zipfile
from typing import Dict

import requests

from github_secret_updater import RepoContext


class LogUnavailable(RuntimeError):
    """Raised when the log can't be fetched for a known, non-bug reason
    (run too old, run still in progress, run cancelled before steps
    produced output). The admin UI shows a friendly message; this is
    distinct from a genuine network/auth error which propagates as-is."""


def download_run_logs(ctx: RepoContext, run_id: int | str) -> Dict[str, str]:
    """Return `{step_name: log_text}` for one workflow run.

    `step_name` is taken from the zip member filename. GitHub names them
    like `1_Set up job.txt` / `5_Scrape and commit.txt` — the leading
    digit is the step ordinal. We strip the `.txt` suffix but keep the
    ordinal so the admin UI can sort them in execution order.
    """
    url = f"{ctx.base}/actions/runs/{run_id}/logs"
    resp = requests.get(
        url, headers=ctx.headers, timeout=30, allow_redirects=True,
    )
    if resp.status_code == 410:
        raise LogUnavailable(
            "GitHub has expired this run's logs (default retention is 90 "
            "days). Re-dispatch the workflow to get a fresh log."
        )
    if resp.status_code == 404:
        raise LogUnavailable(
            "GitHub couldn't find logs for this run — it may still be "
            "queued or have been cancelled before any step produced output."
        )
    resp.raise_for_status()

    out: Dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        for member in zf.namelist():
            # Skip the top-level summary files (`0_<job>.txt`) — we want
            # step-level granularity. Job-level logs also live inside the
            # zip but they're verbose duplicates.
            if "/" not in member and member.endswith(".txt"):
                # GitHub also drops the job-level rollup `<job>.txt` at the
                # root. Keep it under a synthetic key so the UI can still
                # use it if step files are missing (rare).
                key = member.rsplit(".txt", 1)[0]
                out.setdefault(f"_job__{key}", zf.read(member).decode(
                    "utf-8", errors="replace"
                ))
                continue
            if not member.endswith(".txt"):
                continue
            # Step file: `<job>/<step_ordinal>_<step_name>.txt`
            name = member.split("/", 1)[1].rsplit(".txt", 1)[0]
            out[name] = zf.read(member).decode("utf-8", errors="replace")
    return out


def pick_scrape_step(logs: Dict[str, str]) -> tuple[str, str]:
    """Pick the step whose log is most likely the scraper run.

    Returns `(step_name, log_text)`. Falls back to the longest step log
    (the scraper is by far the longest-running step) when name matching
    misses — robust against per-app workflow files that name the step
    differently.
    """
    if not logs:
        return ("", "")

    # 1. Name match — strongest signal.
    for name, text in logs.items():
        lower = name.lower()
        if "scrape" in lower and "pipeline" not in lower:
            # Match "Scrape and commit" / "Run scrape" / similar.
            return (name, text)
    for name, text in logs.items():
        if "pipeline" in name.lower() or "run pipeline" in name.lower():
            return (name, text)

    # 2. Fallback: longest non-job-rollup file.
    step_files = {k: v for k, v in logs.items() if not k.startswith("_job__")}
    if step_files:
        name, text = max(step_files.items(), key=lambda kv: len(kv[1]))
        return (name, text)

    # 3. Last resort: job rollup.
    name, text = next(iter(logs.items()))
    return (name, text)


def tail(text: str, n_lines: int = 150) -> str:
    """Return the last `n_lines` of a multi-line string."""
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-n_lines:])
