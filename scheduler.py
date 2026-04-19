"""
Interval runner for the cHAP Seller Tracker (Phase 4).

Wraps `pipeline.run_pipeline` in a supervision loop so the full
scrape → push → analyse chain fires on a fixed cadence. Designed for
long-running background use (systemd / launchd / Docker), but can also
be driven from a terminal during development.

Design goals:

  * **Resilient by default.** A single pipeline failure must NOT crash
    the scheduler. We catch every exception, log the traceback, and
    continue — a transient Playwright flake or Supabase 5xx must not
    take the whole tracker down for hours.

  * **Controlled cadence.** The scraper takes ~2-3 minutes end-to-end,
    so naively sleeping for `interval` would drift on slow runs. We
    compute the *remaining* time after each run finishes and sleep
    only for that, clamped to a small floor so we never hammer the
    upstream admin panel back-to-back.

  * **Exponential backoff on repeat failures.** The 2nd, 3rd, … back-
    to-back failures sleep progressively longer (2x, 4x, 8x the base
    interval, capped at 1h). The counter resets on the next success.
    This prevents us from generating a storm of login attempts when
    the admin panel is rate-limiting or down.

  * **Bounded, structured logging.** Run summaries land in
    `logs/scheduler.log` (rotated), while each pipeline invocation
    logs through the normal pipeline.py handler chain. Combined, the
    operator can answer "when did we last scrape?" and "what did that
    run produce?" from the log file alone.

CLI:

    # Default: run every 10 minutes, forever
    python3 scheduler.py

    # Every 5 minutes (aggressive; only useful for debugging)
    python3 scheduler.py --interval 300

    # Run N times then exit (handy for smoke tests / cron wrappers that
    # prefer to own the process lifecycle)
    python3 scheduler.py --max-runs 3 --interval 120

    # Skip Supabase writes for a local smoke loop
    python3 scheduler.py --dry-run

SIGTERM / SIGINT: the scheduler finishes the in-flight pipeline run and
exits cleanly on the next scheduling decision. (We deliberately don't
cancel mid-scrape; aborting Playwright partway leaves the admin panel
session in a weird state.)

Deployment notes live in `04_BUILD_ROADMAP.md` (Phase 4 checklist).
A systemd unit template is included in a block comment at the end of
this file — copy-paste ready when you're ready to daemonise.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys
import time
from pathlib import Path
from typing import Any

from pipeline import run_pipeline

# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------

# Default cadence: every 10 minutes. The roadmap Phase 4 target is
# 5-10 minutes; 10 gives the admin-panel backend breathing room and
# keeps our login retries well clear of any burst limit.
DEFAULT_INTERVAL_S = 10 * 60

# If a run finishes and the remaining time is less than this floor, we
# still sleep the floor to avoid immediately hammering the panel.
MIN_IDLE_S = 15

# Backoff cap — never sleep longer than 1h after a streak of failures.
# At that point an operator should notice via the log file, so we keep
# the cadence somewhat visible.
MAX_BACKOFF_S = 60 * 60

LOG_DIR = Path(__file__).parent / "logs"
SCHED_LOG = LOG_DIR / "scheduler.log"


# ---------------------------------------------------------------------
# Log setup
# ---------------------------------------------------------------------

def _setup_logging(verbose: bool) -> None:
    """Configure root logging with both a rotating file handler and a
    stderr stream handler.

    File handler goes to `logs/scheduler.log` with 5MB rotation + 5
    backups — plenty for a couple of weeks of 10-minute runs and
    cheap to tail when debugging."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    # Clear any handlers pipeline._setup_logging might have set in a
    # previous invocation — the scheduler owns root logging for the
    # life of the process.
    for h in list(root.handlers):
        root.removeHandler(h)

    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )

    file_h = logging.handlers.RotatingFileHandler(
        SCHED_LOG,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_h.setFormatter(fmt)
    root.addHandler(file_h)

    stream_h = logging.StreamHandler(sys.stderr)
    stream_h.setFormatter(fmt)
    root.addHandler(stream_h)


# ---------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------

class _ShutdownFlag:
    """Tiny thread-safe-ish flag for cooperative shutdown. Set when we
    receive SIGTERM/SIGINT; checked in the main loop between runs."""
    def __init__(self) -> None:
        self.requested: bool = False
        self.reason: str | None = None

    def request(self, signame: str) -> None:
        self.requested = True
        self.reason = signame


def _install_signal_handlers(flag: _ShutdownFlag) -> None:
    """Install SIGINT (Ctrl-C) and SIGTERM (systemd stop) handlers that
    flip the shutdown flag. The main loop will exit cleanly after the
    current pipeline invocation finishes.

    We deliberately do NOT raise KeyboardInterrupt here — the pipeline
    may be mid-Playwright; interrupting synchronously would leave the
    browser dangling. Instead we mark the flag and let the loop notice.
    """
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda s, _frame: flag.request(signal.Signals(s).name))
        except (ValueError, OSError):
            # signal.signal raises on non-main threads or on Windows for
            # SIGTERM. We ignore those — CLI usage is the target and the
            # process will still exit on KeyboardInterrupt via bubbling.
            pass


# ---------------------------------------------------------------------
# Backoff policy
# ---------------------------------------------------------------------

def _compute_sleep(
    *,
    interval_s: float,
    elapsed_s: float,
    consecutive_failures: int,
) -> float:
    """Decide how long to sleep until the next pipeline invocation.

    On a healthy streak (consecutive_failures==0) we target the
    configured interval, subtracting the time the last run already
    consumed. On a failure streak we apply exponential backoff:

        sleep = min(interval * 2^failures, MAX_BACKOFF_S)

    so 1 failure → 2x wait, 2 failures → 4x wait, ..., capped at 1h.

    The `elapsed_s` subtraction is not applied to the backoff branch —
    after a failure we always pause for at least the full backoff
    window, regardless of how quickly the crash happened.
    """
    if consecutive_failures > 0:
        # 2**N grows fast; cap to avoid multi-hour quiet periods.
        wait = interval_s * (2 ** consecutive_failures)
        return float(min(wait, MAX_BACKOFF_S))
    remaining = interval_s - elapsed_s
    return float(max(remaining, MIN_IDLE_S))


# ---------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------

def _run_once(dry_run: bool) -> dict[str, Any] | None:
    """Execute one pipeline pass. Returns the report dict on success,
    None on failure. Never raises — all errors are caught and logged.

    We swallow everything (including KeyboardInterrupt inside the
    scrape) because the scheduler owns shutdown, not the pipeline.
    """
    start = time.monotonic()
    try:
        out = run_pipeline(dry_run=dry_run)
        elapsed = time.monotonic() - start
        if isinstance(out, dict) and out.get("error"):
            logging.warning(
                f"⚠️  Pipeline returned error marker "
                f"({out.get('error')}) after {elapsed:.1f}s"
            )
            return None
        logging.info(f"✅ Pipeline OK in {elapsed:.1f}s")
        return out
    except KeyboardInterrupt:
        # Let the outer loop notice shutdown via the signal flag.
        logging.warning("KeyboardInterrupt during pipeline — propagating to scheduler loop")
        raise
    except Exception:  # noqa: BLE001 — intentional catch-all
        elapsed = time.monotonic() - start
        logging.exception(f"🔥 Pipeline raised after {elapsed:.1f}s")
        return None


def _sleep_with_shutdown(seconds: float, flag: _ShutdownFlag) -> None:
    """Sleep in 1-second ticks so a shutdown signal can break us out
    promptly. Using a single long time.sleep() would leave SIGTERM
    latency up to `seconds`, which is annoying for systemd stop."""
    end = time.monotonic() + seconds
    while not flag.requested:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def scheduler_loop(
    *,
    interval_s: float,
    max_runs: int | None,
    dry_run: bool,
) -> int:
    """Main supervisory loop. Returns a POSIX-ish exit code:
      0 → shut down cleanly (ran to max_runs, or received SIGTERM with
          at least one successful run)
      1 → exited without ever succeeding (all attempts failed)
    """
    flag = _ShutdownFlag()
    _install_signal_handlers(flag)

    runs_done = 0
    consecutive_failures = 0
    any_success = False

    logging.info(
        f"🗓️  Scheduler starting: interval={interval_s:.0f}s, "
        f"max_runs={max_runs or '∞'}, dry_run={dry_run}"
    )

    while not flag.requested:
        start = time.monotonic()
        result = _run_once(dry_run=dry_run)
        elapsed = time.monotonic() - start
        runs_done += 1

        if result is not None:
            any_success = True
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            logging.warning(
                f"Failure streak length now {consecutive_failures}; "
                f"applying exponential backoff on the next sleep."
            )

        if flag.requested:
            logging.info(
                f"🛑 Shutdown signal ({flag.reason}) received after run "
                f"#{runs_done}; exiting loop."
            )
            break

        if max_runs is not None and runs_done >= max_runs:
            logging.info(f"🏁 Reached max_runs={max_runs}; exiting loop.")
            break

        sleep_s = _compute_sleep(
            interval_s=interval_s,
            elapsed_s=elapsed,
            consecutive_failures=consecutive_failures,
        )
        logging.info(f"💤 Sleeping {sleep_s:.0f}s until next run.")
        _sleep_with_shutdown(sleep_s, flag)

    logging.info(
        f"📊 Scheduler done. runs_attempted={runs_done}, "
        f"any_success={any_success}, consecutive_failures={consecutive_failures}"
    )
    return 0 if any_success or runs_done == 0 else 1


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def _cli() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Interval runner for the cHAP Seller Tracker pipeline. "
            "Intended for long-running systemd/launchd use; handles "
            "transient failures with exponential backoff and clean "
            "shutdown on SIGTERM/SIGINT."
        )
    )
    parser.add_argument(
        "--interval",
        dest="interval_s",
        type=float,
        default=DEFAULT_INTERVAL_S,
        help=(
            f"Target seconds between run *starts*. "
            f"Default: {DEFAULT_INTERVAL_S} (10 min)."
        ),
    )
    parser.add_argument(
        "--max-runs",
        dest="max_runs",
        type=int,
        default=None,
        help="Exit after N runs. Omit for continuous operation.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Pass --dry-run through to pipeline (skip Supabase writes).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG logging on the root logger.",
    )
    args = parser.parse_args()

    _setup_logging(verbose=args.verbose)

    try:
        exit_code = scheduler_loop(
            interval_s=args.interval_s,
            max_runs=args.max_runs,
            dry_run=args.dry_run,
        )
    except KeyboardInterrupt:
        logging.info("Received KeyboardInterrupt at top level — exiting.")
        exit_code = 0
    return exit_code


if __name__ == "__main__":
    sys.exit(_cli())


# ---------------------------------------------------------------------
# systemd unit template (copy into /etc/systemd/system/chap-scraper.service
# and `systemctl daemon-reload && systemctl enable --now chap-scraper`)
# ---------------------------------------------------------------------
# [Unit]
# Description=cHAP Seller Tracker scraping loop
# After=network-online.target
# Wants=network-online.target
#
# [Service]
# Type=simple
# User=chap
# WorkingDirectory=/opt/chap-scraper
# EnvironmentFile=/opt/chap-scraper/.env
# ExecStart=/opt/chap-scraper/.venv/bin/python3 scheduler.py --interval 600
# Restart=on-failure
# RestartSec=30
# StandardOutput=append:/var/log/chap-scraper.out
# StandardError=append:/var/log/chap-scraper.err
#
# [Install]
# WantedBy=multi-user.target
