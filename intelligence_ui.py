"""
intelligence_ui.py — Streamlit page for the Customer Intelligence view.

Audience: sales reps. Not stakeholders.
The Dashboard answers "how is the business trending?"; this page
answers "who should I contact today, and why?". Every row in every
bucket is an actionable outreach target — the page is an operational
to-do list, not a trend report.

Imported + called by pages/Intelligence.py (Streamlit auto-discovers
pages/*.py; keeping the logic here separates it from that thin
wrapper so it can also be imported by tests).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

import auth
import roles
import customer_intelligence as ci
from analytics_advanced import (
    DISPLAY_NAMES,
    display_name,
    exclude_test_stores,
)
from normalize import normalize_run_data
from ui_errors import wrap_page
from ui_theme import apply_shared_theme


ROOT = Path(__file__).parent
LATEST_RUN_FILE = ROOT / "results" / "latest" / "run.json"


# ---------------------------------------------------------------------
# Data loading — single run, local JSON. Supabase-based historical
# diffs are a follow-up.
# ---------------------------------------------------------------------


def _load_latest_run() -> dict[str, Any] | None:
    if not LATEST_RUN_FILE.exists():
        return None
    import json
    with LATEST_RUN_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def _sellers_by_app_from(run: dict[str, Any]) -> dict[str, list[dict]]:
    return run.get("data") or {}


# ---------------------------------------------------------------------
# Column presentation — pick a minimal, rep-useful set so the tables
# aren't a wall of raw jsonb. CSV export keeps the full row.
# ---------------------------------------------------------------------

_PRIMARY_COLS = [
    "store_url",
    "email",
    "username",
    "plan",
    "product_count",
    "order_count",
    "failed_order_count",
    "steps_completed",
    "installed_on",
    "source_country",
]


def _table_from_rows(rows: list[dict]) -> pd.DataFrame:
    """Build a DataFrame from enriched seller rows, picking the columns
    reps actually scan. `_insight` sub-dicts are expanded to visible
    `days_since_install` / `failure_ratio` columns where relevant."""
    records: list[dict] = []
    for r in rows:
        rec = {c: r.get(c, "") for c in _PRIMARY_COLS}
        ins = r.get("_insight", {})
        rec["days_installed"] = ins.get("days_since_install") or "—"
        if "failure_ratio" in ins:
            rec["failure_ratio"] = f"{ins['failure_ratio'] * 100:.1f}%"
        records.append(rec)
    return pd.DataFrame.from_records(records)


def _download_csv(df: pd.DataFrame, filename: str) -> None:
    if df.empty:
        return
    st.download_button(
        "⬇ Download CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name=filename,
        mime="text/csv",
        use_container_width=False,
    )


# ---------------------------------------------------------------------
# Sidebar — app picker. Single-select for the MVP (one app = one
# bucket list; cross-app leads are a later iteration).
# ---------------------------------------------------------------------


def _render_sidebar(available_apps: list[str]) -> str:
    with st.sidebar:
        st.markdown(
            '<div class="sidebar-brand">cHAP <span class="sidebar-brand-accent">'
            'Customer</span> Intelligence</div>'
            '<div class="sidebar-tagline">For sales reps</div>',
            unsafe_allow_html=True,
        )

        st.markdown(
            '<div class="sidebar-section">Filters</div>',
            unsafe_allow_html=True,
        )

        if not available_apps:
            st.info("No scraped data yet. Run a scrape from Admin → Overview.")
            return ""

        # Single-select: each bucket table is app-scoped. The user said
        # "we will choose the app" — singular.
        pick_idx = st.selectbox(
            "App",
            options=list(range(len(available_apps))),
            format_func=lambda i: display_name(available_apps[i]),
            help="Pick the app whose sellers you want to work today.",
        )
        return available_apps[pick_idx]


# ---------------------------------------------------------------------
# Main — entry point for pages/Intelligence.py.
# ---------------------------------------------------------------------


@wrap_page
def main() -> None:
    principal = auth.gate()
    auth.require("view_dashboard", principal)

    st.set_page_config(
        page_title="Customer Intelligence — cHAP",
        page_icon="🎯",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    apply_shared_theme()
    auth.sign_out_button(st)

    st.title("🎯 Customer Intelligence")
    st.caption(
        "Actionable leads for sales reps. Each bucket below is a list "
        "of sellers worth reaching out to today, grouped by why they "
        "matter. Pick an app in the sidebar; the buckets recompute."
    )

    run = _load_latest_run()
    if not run:
        st.warning(
            "No scraped data available yet (`results/latest/run.json` is "
            "missing). Open **Admin → Overview → Run scrape now** to "
            "populate it, then come back."
        )
        return

    sellers_by_app = _sellers_by_app_from(run)
    sellers_by_app, _ = normalize_run_data(sellers_by_app, {})
    sellers_by_app = exclude_test_stores(sellers_by_app)

    available_apps = sorted(
        [a for a, rows in sellers_by_app.items() if rows]
    )
    app_key = _render_sidebar(available_apps)
    if not app_key:
        return

    sellers = sellers_by_app.get(app_key, []) or []
    run_stamp = run.get("run_stamp", "unknown")

    # Header row — scope + freshness at a glance.
    st.markdown(
        f"### Viewing: **{display_name(app_key)}**  "
        f"<span style='color:#64748b; font-size:0.85rem;'>· "
        f"{len(sellers)} sellers in the latest scrape · run {run_stamp}"
        f"</span>",
        unsafe_allow_html=True,
    )

    today = date.today()
    buckets = ci.buckets_for(sellers, today=today)

    if not buckets:
        st.error(
            "Couldn't compute insight buckets for this app. Technical "
            "details are in the Streamlit Cloud logs — send the admin "
            "a screenshot if this persists."
        )
        return

    # Summary strip: how many leads are in each bucket.
    counts = " · ".join(
        f"{b.title} · **{b.count}**" for b in buckets if b.count > 0
    )
    if counts:
        st.markdown(
            f'<div style="padding:10px 14px; background:#f1f5f9; '
            f'border-radius:8px; margin:8px 0 18px 0; font-size:0.88rem;">'
            f'{counts}'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        st.info(
            "No leads fall into any actionable bucket for this app "
            "right now. That's either because the app has no sellers "
            "yet or everyone is already converted + stable — rare, "
            "but nice when it happens."
        )
        return

    # Render every bucket with >0 rows as its own tab. Empty buckets
    # are collapsed into an expander at the bottom so the page doesn't
    # scream about 0s — reps only see actionable work by default.
    active = [b for b in buckets if b.count > 0]
    inactive = [b for b in buckets if b.count == 0]

    tab_labels = [f"{b.title}  ·  {b.count}" for b in active]
    tabs = st.tabs(tab_labels)
    for tab, bucket in zip(tabs, active):
        with tab:
            st.caption(bucket.definition)
            df = _table_from_rows(bucket.rows)
            st.dataframe(df, hide_index=True, use_container_width=True)
            _download_csv(
                df,
                f"intel_{bucket.id}_{app_key}_{run_stamp}.csv",
            )

    if inactive:
        with st.expander(
            f"Empty buckets ({len(inactive)}) — no one fits here right now",
            expanded=False,
        ):
            for b in inactive:
                st.markdown(f"**{b.title}** — {b.definition}")
                st.caption("0 sellers match this definition today.")
                st.write("")

    st.divider()
    st.caption(
        "Coming next: day-over-day delta (what changed since the last "
        "scrape — new installs, plan changes, failed-order spikes); "
        "pulled directly from Supabase snapshots so nothing is lost "
        "when the admin panel drops a seller. AI-augmented outreach "
        "recommendations per lead are also in the pipeline."
    )
