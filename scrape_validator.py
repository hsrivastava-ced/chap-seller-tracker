"""
scrape_validator.py — post-scrape invariant checks.

Philosophy: a scrape run is only "good" if it passes EVERY invariant below.
A single red flag is enough to reject the run — we'd rather keep yesterday's
data than silently overwrite it with garbage.

Three invariant families:

1. `check_customize_grid`  — observed popup labels match the canonical
   expectation (grid_columns.yaml). Missing required → block. Extras →
   pending_review.

2. `check_pagination`      — for a given app, the per-page trace has:
      * monotonically-incrementing page numbers (+1 per step)
      * unique first-row keys across pages (no looped pagination)
      * scraped-row-count within ±5 % of the dashboard-reported total
      * no page returned zero rows before the last page

3. `check_row_count`       — a single app's row count did not crater
   vs the previous successful run (soft limit: -20% triggers review,
   -50 % blocks). This catches "login succeeded but landed on empty
   state" silent failures.

All three return a `CheckReport` dataclass so the caller can render a
single unified `ValidationReport.format_markdown()` for the INVALID_RUN.md
sentinel file.

Status values:
  "ok"              — run is clean, safe to promote
  "pending_review"  — drift detected, operator should review but data
                      is not obviously wrong. Run PROMOTES but gets
                      schema_status=pending_review in apps.yaml.
  "blocked"         — an invariant failed hard. Run does NOT promote;
                      results/latest/ stays at its previous state.

Design notes:
  - The validator is PURE. It doesn't touch the filesystem, doesn't
    call Supabase, doesn't run playwright. It takes structured input
    (lists/dicts) + YAML config and returns dataclasses. This means
    unit-testing is trivial — no fixtures beyond the YAML file.
  - The pagination invariants require the scraper to RECORD per-page
    data during the walk. See `scraper._scrape_paginated_ant_table`
    for the PageTrace entries.
  - YAML is loaded once per validation pass; cheap enough that we
    don't bother caching.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


# =====================================================================
# Config loader
# =====================================================================
GRID_COLUMNS_YAML = Path(__file__).parent / "grid_columns.yaml"


def _load_grid_columns() -> dict:
    """Read grid_columns.yaml; tolerate absence (returns empty default)."""
    if not GRID_COLUMNS_YAML.exists():
        logging.warning(
            "grid_columns.yaml is missing — Customize Grid checks will "
            "accept any observed layout as canonical."
        )
        return {"schema_version": 1, "default": {"required": [], "optional": []}, "apps": {}}
    try:
        with GRID_COLUMNS_YAML.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        # Validator should NEVER crash the scraper — better to soft-accept.
        logging.error(f"grid_columns.yaml parse failure: {e}")
        return {"schema_version": 1, "default": {"required": [], "optional": []}, "apps": {}}


def _expected_for(app_name: str, grid_cfg: dict) -> tuple[list[str], list[str]]:
    """Return (required, optional) label lists for `app_name`, falling
    back to `default` if no per-app override exists."""
    apps = (grid_cfg or {}).get("apps", {}) or {}
    if app_name in apps:
        cfg = apps[app_name]
    else:
        cfg = (grid_cfg or {}).get("default", {}) or {}
    return list(cfg.get("required", []) or []), list(cfg.get("optional", []) or [])


def _norm_label(s: str) -> str:
    """Case-insensitive, whitespace-collapsed comparison key."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# =====================================================================
# Reports
# =====================================================================
@dataclass
class CheckReport:
    name: str              # human-readable check name ("customize_grid" etc.)
    status: str            # "ok" | "pending_review" | "blocked"
    observations: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        return self.status == "blocked"

    @property
    def needs_review(self) -> bool:
        return self.status == "pending_review"


@dataclass
class PageTrace:
    """One per page of a paginated table scrape."""
    page_num: int                # 1-based page number reported by the Ant paginator
    first_row_key: str           # data-row-key attr of the first row
    row_count: int               # visible rows this page
    reported_total_rows: Optional[int] = None  # "Showing … of TOTAL"
    reported_total_pages: Optional[int] = None


@dataclass
class ValidationReport:
    """Full per-app validation verdict."""
    app_name: str
    kind: str                # "sellers" | "uninstalls"
    checks: list[CheckReport] = field(default_factory=list)

    @property
    def status(self) -> str:
        if any(c.is_blocked for c in self.checks):
            return "blocked"
        if any(c.needs_review for c in self.checks):
            return "pending_review"
        return "ok"

    @property
    def is_promotable(self) -> bool:
        """True if this run may overwrite results/latest/."""
        return self.status != "blocked"

    def format_markdown(self) -> str:
        lines = [f"### {self.app_name} / {self.kind} — **{self.status}**"]
        for c in self.checks:
            lines.append(f"- **{c.name}** → _{c.status}_")
            for o in c.observations:
                lines.append(f"    - {o}")
            for r in c.recommendations:
                lines.append(f"    - 👉 {r}")
        return "\n".join(lines)


# =====================================================================
# Individual checks
# =====================================================================
def check_customize_grid(
    app_name: str,
    observed_labels: list[str],
    grid_cfg: Optional[dict] = None,
) -> CheckReport:
    """Diff observed Customize Grid labels vs canonical expectation."""
    cfg = grid_cfg or _load_grid_columns()
    required, optional = _expected_for(app_name, cfg)

    req_norm = {_norm_label(r) for r in required}
    opt_norm = {_norm_label(o) for o in optional}
    obs_norm = {_norm_label(x) for x in observed_labels}

    missing_required = sorted(
        r for r in required if _norm_label(r) not in obs_norm
    )
    unexpected = sorted(
        x for x in observed_labels
        if _norm_label(x) not in req_norm and _norm_label(x) not in opt_norm
    )

    obs_count = len(observed_labels)
    report = CheckReport(
        name="customize_grid",
        status="ok",
        observations=[
            f"Observed {obs_count} column options in Customize Grid.",
            f"Required: {len(required)} (missing: {len(missing_required)})",
            f"Unexpected extras: {len(unexpected)}",
        ],
    )

    if missing_required:
        report.status = "blocked"
        report.observations.append(
            f"Missing REQUIRED columns: {missing_required!r}"
        )
        report.recommendations.append(
            "The admin panel shape changed. Either revert the UI or update "
            "`grid_columns.yaml` via the admin UI → Apps → Review drift flow."
        )
        return report

    if unexpected:
        report.status = "pending_review"
        report.observations.append(f"Unexpected labels: {unexpected!r}")
        report.recommendations.append(
            "New Customize Grid column detected. Run promoted but marked "
            "`pending_review`. Super admin should confirm and add it to "
            "`grid_columns.yaml` optional list."
        )

    return report


def check_pagination(
    app_name: str,
    trace: list[PageTrace],
    scraped_row_count: int,
    *,
    total_tolerance: float = 0.05,
) -> CheckReport:
    """Verify pagination walked correctly and captured roughly all rows.

    Invariants checked:
      1. Page numbers form a strictly-increasing sequence starting at 1.
      2. All first-row keys are distinct (no loop / stuck-on-same-page).
      3. Scraped row count is within `total_tolerance` of the max reported
         total rows.
      4. No intermediate page had zero rows. (Last page CAN have fewer
         rows than page-size, so only empty-page-before-last fails.)
    """
    report = CheckReport(name="pagination", status="ok")

    if not trace:
        # Single-page unempty result is valid; completely empty set is
        # suspicious only if previous runs had data. That's handled
        # separately by check_row_count.
        report.observations.append("No pagination trace captured.")
        return report

    # Invariant 1 — page-number sequence
    pages = [t.page_num for t in trace]
    expected = list(range(1, len(pages) + 1))
    if pages != expected:
        report.status = "blocked"
        report.observations.append(
            f"Page numbers {pages} did not form the expected sequence {expected}."
        )
        report.recommendations.append(
            "Pagination likely double-advanced or repeated. Re-run after "
            "confirming the Next-button click handler is not stuck."
        )

    # Invariant 2 — unique first-row keys
    keys = [t.first_row_key for t in trace if t.first_row_key]
    if keys and len(set(keys)) < len(keys):
        # find the dupes
        seen: dict[str, int] = {}
        dupes: list[str] = []
        for k in keys:
            seen[k] = seen.get(k, 0) + 1
        for k, n in seen.items():
            if n > 1:
                dupes.append(f"{k} ×{n}")
        report.status = "blocked"
        report.observations.append(
            f"Duplicate first-row keys across pages: {dupes}. "
            "Scraper likely looped on the same page."
        )
        report.recommendations.append(
            "Inspect the scraper's Next-button resolution + first-key "
            "change detection in `_scrape_paginated_ant_table`."
        )

    # Invariant 3 — row count within tolerance of reported total
    reported_totals = [t.reported_total_rows for t in trace if t.reported_total_rows is not None]
    if reported_totals:
        target = max(reported_totals)
        if target > 0:
            delta = abs(scraped_row_count - target) / target
            report.observations.append(
                f"Scraped {scraped_row_count}/{target} rows "
                f"(Δ {delta:.1%}, tolerance {total_tolerance:.0%})."
            )
            if delta > total_tolerance:
                # Under-scrape is worse than over-scrape. >tolerance in
                # either direction → block.
                if report.status != "blocked":
                    report.status = "blocked"
                report.recommendations.append(
                    "Row count diverges from dashboard-reported total beyond "
                    f"tolerance ({total_tolerance:.0%}). Check for lost "
                    "pages (click didn't advance) or a failed filter reset."
                )

    # Invariant 4 — no empty page before the last
    for i, t in enumerate(trace[:-1]):
        if t.row_count == 0:
            if report.status != "blocked":
                report.status = "blocked"
            report.observations.append(
                f"Page {t.page_num} returned 0 rows but is not the last page."
            )
            report.recommendations.append(
                "Intermediate empty page — scraper probably read the DOM "
                "mid-reflow. Add a settle hedge before row enumeration."
            )

    return report


def check_row_count(
    app_name: str,
    kind: str,
    current: int,
    *,
    previous: Optional[int] = None,
    soft_drop: float = 0.20,
    hard_drop: float = 0.50,
) -> CheckReport:
    """Compare current run's row count vs last known good run.

    Drops of ≤ soft_drop → ok.
    soft_drop < drop ≤ hard_drop → pending_review.
    drop > hard_drop → blocked.

    If `previous` is None, we can't compute a delta; returns ok with a note.
    """
    report = CheckReport(name=f"row_count_{kind}", status="ok")
    report.observations.append(f"Current {kind}: {current} rows.")

    if previous is None or previous == 0:
        report.observations.append(
            "No previous row count available for comparison — skipping drop check."
        )
        return report

    drop = max(0.0, (previous - current) / previous)
    report.observations.append(
        f"Previous {kind}: {previous} rows. Drop: {drop:.1%} "
        f"(soft={soft_drop:.0%}, hard={hard_drop:.0%})."
    )

    if drop > hard_drop:
        report.status = "blocked"
        report.recommendations.append(
            f"Row count dropped >{hard_drop:.0%} vs previous run. "
            "Likely a silent login/landing failure — keep last-good CSV."
        )
    elif drop > soft_drop:
        report.status = "pending_review"
        report.recommendations.append(
            f"Row count dropped {drop:.1%} vs previous run. "
            "Promoted but flagged for operator review."
        )
    return report


# =====================================================================
# Per-app rollup
# =====================================================================
def validate_app(
    app_name: str,
    kind: str,
    *,
    observed_grid_labels: Optional[list[str]] = None,
    pagination_trace: Optional[list[PageTrace]] = None,
    scraped_row_count: int = 0,
    previous_row_count: Optional[int] = None,
    grid_cfg: Optional[dict] = None,
) -> ValidationReport:
    """Run every applicable check for one (app, kind) pair."""
    report = ValidationReport(app_name=app_name, kind=kind, checks=[])

    # Customize Grid applies to sellers only; uninstalls have a fixed
    # 4-col table with no popup.
    if kind == "sellers" and observed_grid_labels is not None:
        report.checks.append(
            check_customize_grid(app_name, observed_grid_labels, grid_cfg)
        )

    if pagination_trace is not None:
        report.checks.append(
            check_pagination(app_name, pagination_trace, scraped_row_count)
        )

    report.checks.append(
        check_row_count(app_name, kind, scraped_row_count, previous=previous_row_count)
    )
    return report


# =====================================================================
# Aggregate formatter — used by scraper.main() to write INVALID_RUN.md
# =====================================================================
def format_run_report(
    reports: list[ValidationReport],
    *,
    promoted: bool,
    stamp: str,
) -> str:
    """Produce the human-readable markdown sentinel file body."""
    worst = "ok"
    order = {"ok": 0, "pending_review": 1, "blocked": 2}
    for r in reports:
        if order[r.status] > order[worst]:
            worst = r.status

    lines = [
        f"# Scrape run report — {stamp}",
        "",
        f"**Overall status:** {worst}",
        f"**Promoted to results/latest/:** {'yes' if promoted else 'NO — last-good snapshot preserved'}",
        "",
        "## Per-(app, kind) findings",
        "",
    ]
    for r in reports:
        lines.append(r.format_markdown())
        lines.append("")

    if worst == "blocked":
        lines += [
            "---",
            "",
            "### What happens next",
            "",
            "At least one invariant failed hard. The scraper did **not** "
            "overwrite `results/latest/` — yesterday's data is still what "
            "the dashboard serves. Inspect the per-check recommendations "
            "above, fix the underlying issue, and re-run the workflow.",
        ]
    elif worst == "pending_review":
        lines += [
            "---",
            "",
            "### What happens next",
            "",
            "Promoted to `results/latest/`, but flagged `pending_review` in "
            "apps.yaml. A super admin should reconcile the drift (add the "
            "new column to grid_columns.yaml, or restore the original admin "
            "panel layout) before the next run.",
        ]
    return "\n".join(lines)
