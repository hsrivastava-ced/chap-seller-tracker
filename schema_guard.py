"""
schema_guard.py — detect + surface schema drift when onboarding a new app.

Why this exists:
    The Plotly charts in dashboard.py and the analytics in
    analytics_advanced.py assume a specific column list in each scraped
    dataset. If a newly-added admin panel returns a slightly different
    layout (e.g. missing `plan`, or adding `subscription_tier`), silently
    merging it into the unified dataset will either crash the dashboard
    or — worse — produce subtly wrong numbers.

    This module is the check that gates a new app's first real scrape.

Public surface:

    compare(kind, observed_columns)         -> SchemaReport
    is_canonical(report)                    -> bool
    format_report_markdown(report)          -> str  (for the admin UI)
    load_canonical(path=None)               -> dict
    save_canonical(data, path=None)         -> None
    promote_to_canonical(kind, columns)     -> None  (super-admin only)

Design choice — "required vs optional":
    The canonical JSON splits columns into required (must be present or
    we can't render) and optional (nice-to-have; missing them is fine).
    A report is `canonical` iff all required columns are present AND no
    unexpected extra columns appear. Extras go in `unexpected_extras`
    and bump the status to `pending_review`. That stricter mode is on
    purpose: if scraper.py accidentally starts emitting a new column
    for one app but not others, we'd rather catch it loudly than
    merge inconsistent tables.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

_DEFAULT_PATH = Path(__file__).resolve().parent / "canonical_schema.json"

SELLERS = "sellers"
UNINSTALLS = "uninstalls"
_ALLOWED_KINDS = (SELLERS, UNINSTALLS)


@dataclass
class SchemaReport:
    """Result of comparing an observed column list to canonical."""
    kind: str
    observed: list[str]
    expected_required: list[str]
    expected_optional: list[str]
    missing_required: list[str] = field(default_factory=list)
    unexpected_extras: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        """canonical | pending_review | blocked

        - canonical       : exact match (required present, no extras)
        - pending_review  : all required present but there are extras
        - blocked         : missing a required column → can't merge
        """
        if self.missing_required:
            return "blocked"
        if self.unexpected_extras:
            return "pending_review"
        return "canonical"


# --------------------------------------------------------------------
# Load / save
# --------------------------------------------------------------------
def load_canonical(path: Optional[Path] = None) -> dict:
    p = Path(path) if path else _DEFAULT_PATH
    if not p.exists():
        return {"schema_version": 1, "kinds": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def save_canonical(data: dict, path: Optional[Path] = None) -> None:
    p = Path(path) if path else _DEFAULT_PATH
    p.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _expected_for(kind: str, path: Optional[Path] = None) -> tuple[list[str], list[str]]:
    data = load_canonical(path)
    k = (data.get("kinds") or {}).get(kind) or {}
    req = list(k.get("required_columns") or [])
    opt = list(k.get("optional_columns") or [])
    return req, opt


# --------------------------------------------------------------------
# Compare
# --------------------------------------------------------------------
def compare(kind: str, observed_columns: Iterable[str], path: Optional[Path] = None) -> SchemaReport:
    """Compare the scraped columns of one kind to the canonical layout."""
    if kind not in _ALLOWED_KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {_ALLOWED_KINDS}")

    observed = list(observed_columns)
    observed_set = set(observed)
    required, optional = _expected_for(kind, path)
    allowed = set(required) | set(optional)

    missing_required = [c for c in required if c not in observed_set]
    unexpected_extras = sorted(observed_set - allowed)

    return SchemaReport(
        kind=kind,
        observed=observed,
        expected_required=required,
        expected_optional=optional,
        missing_required=missing_required,
        unexpected_extras=unexpected_extras,
    )


def is_canonical(report: SchemaReport) -> bool:
    return report.status == "canonical"


# --------------------------------------------------------------------
# Admin UI helpers
# --------------------------------------------------------------------
def format_report_markdown(report: SchemaReport) -> str:
    """Render a SchemaReport as markdown for Streamlit to show to editors.

    Kept pure-string so the admin UI can either `st.markdown` it directly
    or wrap it in an expander/table.
    """
    lines = [f"### Schema report — `{report.kind}`",
             f"**Status:** `{report.status}`",
             ""]

    if report.missing_required:
        lines += [
            "**Missing required columns** (merge blocked):",
            *(f"- `{c}`" for c in report.missing_required),
            "",
        ]
    if report.unexpected_extras:
        lines += [
            "**Unexpected extra columns** (need super-admin review):",
            *(f"- `{c}`" for c in report.unexpected_extras),
            "",
        ]
    if report.status == "canonical":
        lines += ["All required columns present. No extras. Ready to merge."]
    return "\n".join(lines)


def promote_to_canonical(kind: str, columns: Iterable[str], path: Optional[Path] = None) -> None:
    """Super-admin action: accept a new column list as canonical.

    Typically called when onboarding an app whose admin panel includes
    an extra column we want to start tracking globally. Promoting it
    updates canonical_schema.json so all future runs treat it as
    expected (optional by default; can be upgraded to required
    manually by editing the JSON).
    """
    if kind not in _ALLOWED_KINDS:
        raise ValueError(f"unknown kind {kind!r}")

    data = load_canonical(path)
    data.setdefault("schema_version", 1)
    kinds = data.setdefault("kinds", {})
    entry = kinds.setdefault(kind, {"required_columns": [], "optional_columns": []})

    existing = set(entry.get("required_columns") or []) | set(entry.get("optional_columns") or [])
    new_optional = [c for c in columns if c not in existing]
    entry["optional_columns"] = list(entry.get("optional_columns") or []) + new_optional

    save_canonical(data, path)
