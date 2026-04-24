"""
Streamlit dashboard for the cHAP Seller Tracker — stakeholder edition.

Inspired by the payments-dashboard design Hrithik shared (dark sidebar
with branding + grouped nav, colorful KPI cards, Plotly charts, a
"Last Updated / Refresh Frequency" block pinned to the bottom-left).

Design ethos:
- Dark sidebar, light content. Big bold numbers on the cards.
- Plotly for all charts so we can pick the palette and keep the look
  consistent with the reference.
- Sidebar filters are deliberately short: App picker (stakeholder
  display names SHEIN / TEMU US / TEMU EU / All Apps) + Year picker.
  The Run / Compare-to pickers from the engineering dashboard are gone
  — stakeholders just want "latest".
- Test stores (emails ending in threecolts.com / cedcommerce.com) are
  dropped before any metric is computed.
- Month / quarter axis labels are mm/yy (04/26) per the stakeholder PDF.

Launch:
    cd "Admin Panel Scrapper"
    streamlit run dashboard.py
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import auth
import roles
from ui_errors import wrap_page
from ui_theme import apply_shared_theme
from analytics_advanced import (
    DISPLAY_NAMES,
    build_stakeholder_report,
    display_name,
    exclude_test_stores,
    filter_by_year,
    fmt_date_long,
    fmt_month_short,
    fmt_quarter_short,
    fmt_year,
)
from normalize import normalize_run_data

# ---------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------

ROOT = Path(__file__).parent
RESULTS_DIR = ROOT / "results"
HISTORY_DIR = RESULTS_DIR / "history"
REPORTS_DIR = RESULTS_DIR / "reports"
LATEST_RUN_FILE = RESULTS_DIR / "latest" / "run.json"

# Internal app keys the scraper writes. The Sidebar shows stakeholder
# labels (SHEIN / TEMU US / TEMU EU / …) via DISPLAY_NAMES. Seeded with
# the original 3 apps + "all_apps" so `from dashboard import APP_KEYS`
# callers never see an empty list at import time. It's rebound in
# main() (via _discover_app_keys) to the actual apps present in
# apps.yaml AND the loaded run data — new apps onboarded through the
# Admin UI automatically show up without touching this list.
APP_KEYS: list[str] = ["all_apps", "shein", "shopify_temu", "shopify_temu_eu"]


def _discover_app_keys(
    sellers_by_app: dict, uninstalls_by_app: dict,
) -> list[str]:
    """Return the canonical APP_KEYS list for this run.

    Source of truth is the registry (apps.yaml) — anything marked
    pending_review or canonical is fair game. We also union in any
    app_id actually present in the loaded scrape data, in case the
    registry hasn't caught up yet (e.g. a scrape committed before
    apps.yaml was updated). "all_apps" is always first so downstream
    `[a for a in APP_KEYS if a != "all_apps"]` filters still work.
    """
    try:
        import app_registry
        registered = [a.id for a in app_registry.all_apps() or []]
    except Exception:
        registered = []
    in_data = list((sellers_by_app or {}).keys()) + list(
        (uninstalls_by_app or {}).keys()
    )
    seen: list[str] = []
    for key in registered + in_data:
        if key and key != "all_apps" and key not in seen:
            seen.append(key)
    return ["all_apps"] + seen

# Apps that surface a multi-platform mix (Shopify + Prestashop + ...).
# The Framework / Platform and Uninstall Platform sections only make
# sense for these — SHEIN and TEMU US are Shopify-only, so showing a
# single 1-bar chart would add noise.
MULTI_PLATFORM_APPS: set[str] = {"shopify_temu_eu"}

# Palette cribbed from the reference dashboard. Keeping these as module
# constants so individual chart builders can reuse them and the palette
# is consistent.
PALETTE = {
    "primary": "#6366f1",       # indigo — primary metric
    "primary_soft": "#a5b4fc",
    "success": "#10b981",       # green — installs / paid / growth+
    "success_soft": "#6ee7b7",
    "danger": "#ef4444",        # red — uninstalls / churn / growth-
    "danger_soft": "#fca5a5",
    "warning": "#f59e0b",       # amber — averages / warnings
    "warning_soft": "#fcd34d",
    "accent": "#8b5cf6",        # violet — secondary highlight
    "neutral": "#94a3b8",
    "bg": "#f3f4f6",
    "card": "#ffffff",
    "text": "#0f172a",
    "text_soft": "#64748b",
    "sidebar_bg": "#1e293b",
    "sidebar_text": "#e2e8f0",
    "sidebar_muted": "#94a3b8",
}

# Bar color pools for stacked charts (steps, activity buckets).
STACK_COLORS = [
    PALETTE["primary"],
    PALETTE["success"],
    PALETTE["warning"],
    PALETTE["accent"],
    PALETTE["danger"],
    "#0ea5e9",  # sky
    "#ec4899",  # pink
]


# ---------------------------------------------------------------------
# Custom CSS — dark sidebar, card-style containers, KPI styling
# ---------------------------------------------------------------------

def _inject_css() -> None:
    """Inject the stylesheet that makes Streamlit look like the
    reference dashboard. Kept in one place so tweaks don't require
    hunting across the file."""
    st.markdown(
        f"""
<style>
    /* Overall page */
    .main, .stApp {{
        background-color: {PALETTE["bg"]};
    }}
    .block-container {{
        padding-top: 3.5rem;
        padding-bottom: 3rem;
    }}
    header[data-testid="stHeader"] {{
        background: transparent;
    }}

    /* ---------- Sidebar ----------
       The min-width rule is scoped to the expanded state so that when
       the user collapses the sidebar (chevron top-left), the 280px of
       reserved space actually goes away and the main content flows
       into the full viewport width. Without this scoping, Streamlit's
       collapse animation leaves a ghost column on the left and every
       panel looks right-shifted. */
    section[data-testid="stSidebar"] {{
        background-color: {PALETTE["sidebar_bg"]};
    }}
    section[data-testid="stSidebar"][aria-expanded="true"] {{
        min-width: 280px !important;
    }}
    section[data-testid="stSidebar"][aria-expanded="false"] {{
        min-width: 0 !important;
        width: 0 !important;
    }}
    section[data-testid="stSidebar"] * {{
        color: {PALETTE["sidebar_text"]};
    }}

    /* The ">" chevron that appears once the sidebar is collapsed. In
       newer Streamlit builds the testid is `collapsedControl`; older
       ones name it differently. Target all the variants so the button
       is always styled and, more importantly, visible against the
       light page background. */
    button[data-testid="collapsedControl"],
    div[data-testid="stSidebarCollapsedControl"],
    button[kind="headerNoPadding"] {{
        display: flex !important;
        visibility: visible !important;
        opacity: 1 !important;
        background-color: {PALETTE["sidebar_bg"]} !important;
        color: #ffffff !important;
        border-radius: 0 10px 10px 0 !important;
        padding: 10px 12px !important;
        box-shadow: 2px 2px 6px rgba(15, 23, 42, 0.15) !important;
        top: 1rem !important;
        left: 0 !important;
        z-index: 1000 !important;
    }}
    button[data-testid="collapsedControl"] svg,
    div[data-testid="stSidebarCollapsedControl"] svg,
    button[kind="headerNoPadding"] svg {{
        color: #ffffff !important;
        fill: #ffffff !important;
    }}

    /* Make sure the main content container doesn't keep the old
       sidebar offset when collapsed — force it to span the viewport
       once the sidebar is gone. */
    section[data-testid="stSidebar"][aria-expanded="false"] ~ section .block-container {{
        padding-left: 2rem;
        padding-right: 2rem;
        max-width: 100%;
    }}
    section[data-testid="stSidebar"] .stSelectbox label,
    section[data-testid="stSidebar"] .stRadio label {{
        color: {PALETTE["sidebar_muted"]} !important;
        font-size: 0.75rem;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        font-weight: 600;
    }}
    .sidebar-brand {{
        font-weight: 700;
        font-size: 1.3rem;
        padding-bottom: 0.25rem;
    }}
    .sidebar-brand-accent {{
        color: {PALETTE["primary_soft"]};
    }}
    .sidebar-tagline {{
        color: {PALETTE["sidebar_muted"]};
        font-size: 0.78rem;
        margin-bottom: 1.5rem;
        border-bottom: 1px solid #334155;
        padding-bottom: 1rem;
    }}
    .sidebar-section {{
        color: {PALETTE["sidebar_muted"]};
        font-size: 0.7rem;
        letter-spacing: 0.14em;
        text-transform: uppercase;
        margin-top: 1.4rem;
        margin-bottom: 0.4rem;
        font-weight: 600;
    }}
    .sidebar-footer {{
        border-top: 1px solid #334155;
        margin-top: 2rem;
        padding-top: 1rem;
        font-size: 0.78rem;
        color: {PALETTE["sidebar_muted"]};
        line-height: 1.5;
    }}
    .sidebar-footer-label {{
        color: {PALETTE["sidebar_muted"]};
        font-size: 0.7rem;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        font-weight: 600;
    }}
    .sidebar-footer-value {{
        color: {PALETTE["sidebar_text"]};
        font-size: 0.82rem;
        font-weight: 500;
        margin-bottom: 0.7rem;
    }}

    /* ---------- Header ---------- */
    .page-title {{
        font-size: 1.8rem;
        font-weight: 700;
        color: {PALETTE["text"]};
        display: inline-block;
        margin-right: 0.6rem;
    }}
    .page-badge {{
        display: inline-block;
        background-color: #ede9fe;
        color: {PALETTE["accent"]};
        padding: 4px 12px;
        border-radius: 999px;
        font-size: 0.8rem;
        font-weight: 600;
        vertical-align: middle;
    }}

    /* ---------- KPI cards (tightened — user feedback: reduce whitespace,
       align numbers, compact growth pills so more panels sit above
       the fold) ---------- */
    .kpi-card {{
        background-color: {PALETTE["card"]};
        border-radius: 12px;
        padding: 12px 16px 10px 16px;
        box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
        height: 100%;
    }}
    .kpi-label {{
        display: flex;
        align-items: center;
        gap: 6px;
        color: {PALETTE["text_soft"]};
        font-size: 0.66rem;
        font-weight: 600;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        line-height: 1.1;
    }}
    .kpi-sublabel {{
        color: {PALETTE["text_soft"]};
        font-size: 0.7rem;
        margin-top: 0;
        line-height: 1.1;
    }}
    .kpi-value {{
        font-size: 1.7rem;
        font-weight: 700;
        margin: 4px 0 1px 0;
        line-height: 1.05;
        font-variant-numeric: tabular-nums;
    }}
    .kpi-foot {{
        color: {PALETTE["text_soft"]};
        font-size: 0.7rem;
        line-height: 1.1;
    }}

    /* Growth pill — the legend strip above the KPI grid */
    .kpi-legend span {{
        font-size: 0.72rem !important;
        margin-right: 10px !important;
    }}

    /* Content panels — reduced vertical padding so the stack is denser */
    .panel {{
        background-color: {PALETTE["card"]};
        border-radius: 12px;
        padding: 14px 20px 10px 20px;
        box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
        margin-bottom: 0.75rem;
    }}
    .panel-title {{
        font-size: 1rem;
        font-weight: 700;
        color: {PALETTE["text"]};
        margin: 0;
    }}
    .panel-sub {{
        color: {PALETTE["text_soft"]};
        font-size: 0.78rem;
        margin-top: 1px;
        margin-bottom: 8px;
        line-height: 1.35;
    }}

    /* Per-panel download row — sits at the bottom of each panel, right-
       aligned, compact. Streamlit's default download_button is bulky;
       these overrides tame it. */
    .stDownloadButton {{
        margin-top: 2px !important;
    }}
    .stDownloadButton > button {{
        padding: 3px 10px !important;
        font-size: 0.72rem !important;
        border-radius: 8px !important;
        background-color: {PALETTE["bg"]} !important;
        color: {PALETTE["text_soft"]} !important;
        border: 1px solid #e2e8f0 !important;
    }}
    .stDownloadButton > button:hover {{
        background-color: {PALETTE["primary_soft"]} !important;
        color: #ffffff !important;
        border-color: {PALETTE["primary_soft"]} !important;
    }}

    /* Header — Last Updated pill lives here now (moved out of sidebar
       per user feedback: 'Show run freshness more prominently'). */
    .header-stamp {{
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 4px 10px;
        background: #ecfdf5;
        color: #047857;
        border-radius: 999px;
        font-size: 0.72rem;
        font-weight: 600;
        vertical-align: middle;
        margin-left: 10px;
    }}
    .header-stamp-dot {{
        width: 6px;
        height: 6px;
        border-radius: 999px;
        background: #10b981;
    }}

    /* Tables — soften Streamlit's default */
    .stDataFrame {{
        border-radius: 10px;
        overflow: hidden;
    }}
</style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------
# Loaders — same local-JSON shape as before
# ---------------------------------------------------------------------

_LATEST_STAMP = "__latest__"


def _list_run_stamps() -> list[str]:
    """Enumerate runs for the dashboard.

    Primary source is `results/history/<stamp>/run.json` — one directory
    per scrape. But history is gitignored (to keep the repo clean) and
    may be empty on a fresh clone or after a repo cleanup. When that's
    the case, fall back to `results/latest/run.json` via a synthetic
    "__latest__" stamp so the dashboard still has data to render.
    """
    stamps: list[str] = []
    if HISTORY_DIR.exists():
        for d in HISTORY_DIR.iterdir():
            if d.is_dir() and (d / "run.json").exists():
                stamps.append(d.name)
    stamps.sort(reverse=True)
    if stamps:
        return stamps
    # History empty — check for the latest-run file.
    if LATEST_RUN_FILE.exists():
        return [_LATEST_STAMP]
    return []


@st.cache_data(show_spinner=False)
def _load_run(stamp: str) -> dict[str, Any]:
    path = (
        LATEST_RUN_FILE if stamp == _LATEST_STAMP
        else HISTORY_DIR / stamp / "run.json"
    )
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _uninstalls_from_run(run: dict[str, Any]) -> dict[str, list[dict]]:
    return (
        run.get("uninstalls_data")
        or run.get("uninstalls")
        or {}
    )


def _parse_stamp_dt(stamp: str) -> datetime | None:
    """Run stamps look like 'YYYY-MM-DD_HH-MM-SSZ'. Return a naive UTC
    datetime, or None if unparseable."""
    try:
        s = stamp.rstrip("Z").replace("_", " ")
        return datetime.strptime(s, "%Y-%m-%d %H-%M-%S")
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------
# Helper: filter report slices by selected app
# ---------------------------------------------------------------------

def _apps_to_show(app_key: str, available: list[str]) -> list[str]:
    """Given the sidebar app choice, return which keys to render in a
    per-app section. 'all_apps' means 'show each real app plus the
    combined view'; a single-app choice means just that one (and the
    combined series, which is a separate line on a single chart)."""
    if app_key == "all_apps":
        return [a for a in available if a != "all_apps"] + ["all_apps"]
    return [a for a in [app_key] if a in available]


# ---------------------------------------------------------------------
# Plotly builders
# ---------------------------------------------------------------------

def _plotly_layout(
    fig: go.Figure, *, height: int = 320, show_legend: bool = True,
) -> go.Figure:
    """Common layout tweaks — transparent background so the panel's
    white card shows through, light grid, compact margins.

    Legends are ON by default: the stakeholder feedback on the first
    draft was 'I can't tell what each color means', so every chart now
    shows the color → trace mapping underneath the plot."""
    fig.update_layout(
        height=height,
        margin=dict(l=10, r=10, t=10, b=60 if show_legend else 20),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        showlegend=show_legend,
        # dragmode=False — nukes click-drag box-zoom / pan gestures at the
        # layout level. Combined with PLOTLY_CONFIG's scrollZoom=False and
        # per-axis fixedrange=True below, the chart becomes pan/zoom-inert
        # (exactly what stakeholders asked for after seeing bars warp on
        # scroll on both desktop and mobile).
        dragmode=False,
        legend=dict(
            orientation="h",
            yanchor="top", y=-0.18,
            xanchor="center", x=0.5,
            font=dict(size=11, color=PALETTE["text"]),
            bgcolor="rgba(0,0,0,0)",
            itemsizing="constant",
        ),
        xaxis=dict(
            showgrid=False,
            tickfont=dict(size=11, color=PALETTE["text_soft"]),
            fixedrange=True,
        ),
        yaxis=dict(
            gridcolor="#e5e7eb",
            tickfont=dict(size=11, color=PALETTE["text_soft"]),
            fixedrange=True,
        ),
    )
    return fig


def _install_trend_figure(monthly: dict, apps_to_plot: list[str]) -> go.Figure:
    """Multi-line chart of installs per app over time, mm/yy axis."""
    fig = go.Figure()
    palette = [PALETTE["primary"], PALETTE["success"], PALETTE["warning"],
               PALETTE["accent"], PALETTE["danger"]]
    periods = monthly.get("periods", [])
    x = [fmt_month_short(p) for p in periods]
    for i, app in enumerate(apps_to_plot):
        series = monthly["installs"].get(app, {})
        y = [series.get(p, 0) for p in periods]
        fig.add_trace(go.Scatter(
            x=x, y=y,
            mode="lines+markers",
            name=display_name(app),
            line=dict(color=palette[i % len(palette)], width=2.5),
            marker=dict(size=6),
            hovertemplate=f"<b>{display_name(app)}</b><br>%{{x}}: %{{y}} installs<extra></extra>",
        ))
    return _plotly_layout(fig, height=320)


def _installs_vs_uninstalls_figure(
    monthly: dict,
    app_key: str = "all_apps",
    highlight_period: str | None = None,
) -> go.Figure:
    """Green bars for installs, red bars below the axis for uninstalls.
    Matches the reference 'Installs vs Uninstalls' widget shape.

    `app_key` — which per-app series to render. Respecting this fixes the
    old bug where the chart was always the combined 'all_apps' totals
    even when the user had picked SHEIN only.

    `highlight_period` — if set (e.g. '2026-02'), that month's bars are
    drawn in a deeper saturated color so the user can *see* which month
    the Month filter selected. Makes the filter change visibly land on
    the chart without having to stare at the KPI cards.
    """
    periods = monthly.get("periods", [])
    x = [fmt_month_short(p) for p in periods]
    inst = [monthly["installs"].get(app_key, {}).get(p, 0) for p in periods]
    unin = [-monthly["uninstalls"].get(app_key, {}).get(p, 0) for p in periods]

    # Deeper / lighter color per bar depending on whether it matches the
    # highlighted month. If no highlight, every bar uses the "soft"
    # fill — same look as before this change.
    inst_colors = [
        PALETTE["success"] if highlight_period and p == highlight_period
        else PALETTE["success_soft"]
        for p in periods
    ]
    unin_colors = [
        PALETTE["danger"] if highlight_period and p == highlight_period
        else PALETTE["danger_soft"]
        for p in periods
    ]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=x, y=inst,
        name="Installs",
        marker_color=inst_colors,
        hovertemplate="<b>%{x}</b><br>Installs: %{y}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=x, y=unin,
        name="Uninstalls",
        marker_color=unin_colors,
        hovertemplate="<b>%{x}</b><br>Uninstalls: %{customdata}<extra></extra>",
        customdata=[abs(v) for v in unin],
    ))
    fig.update_layout(barmode="relative")
    return _plotly_layout(fig, height=320)


def _paid_bar_figure(paid: dict, apps_to_plot: list[str]) -> go.Figure:
    """Grouped bar: Paid vs Not Paid per app."""
    labels = [display_name(a) for a in apps_to_plot]
    paid_vals = [paid["by_app"].get(a, {}).get("Paid", 0) for a in apps_to_plot]
    nopaid = [paid["by_app"].get(a, {}).get("Not Paid", 0) for a in apps_to_plot]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=labels, y=paid_vals, name="Paid",
        marker_color=PALETTE["success"],
        text=paid_vals, textposition="outside",
    ))
    fig.add_trace(go.Bar(
        x=labels, y=nopaid, name="Not Paid",
        marker_color=PALETTE["neutral"],
        text=nopaid, textposition="outside",
    ))
    fig.update_layout(barmode="group")
    return _plotly_layout(fig, height=320)


def _activity_stacked_figure(
    activity: dict, apps_to_plot: list[str], *, value_col_prefix: str = "order",
) -> go.Figure:
    """Horizontal stacked bars showing bucket composition per app.
    Works for both order_buckets and product_buckets — caller controls
    which."""
    buckets = activity["buckets"]
    fig = go.Figure()
    for i, b in enumerate(buckets):
        y_apps = [display_name(a) for a in apps_to_plot]
        x_vals = []
        for a in apps_to_plot:
            per_app = activity["by_app"].get(a, {})
            key = f"{value_col_prefix}_buckets"
            x_vals.append(per_app.get(key, {}).get(b, 0))
        fig.add_trace(go.Bar(
            y=y_apps, x=x_vals, name=b, orientation="h",
            marker_color=STACK_COLORS[i % len(STACK_COLORS)],
            hovertemplate=f"<b>{b}</b><br>%{{y}}: %{{x}} sellers<extra></extra>",
        ))
    fig.update_layout(barmode="stack")
    return _plotly_layout(fig, height=max(220, 60 * len(apps_to_plot) + 80))


def _velocity_figure(velocity: dict, apps_to_plot: list[str]) -> go.Figure:
    fig = go.Figure()
    palette = [PALETTE["primary"], PALETTE["success"], PALETTE["warning"],
               PALETTE["accent"]]
    days = velocity["days"]
    for i, app in enumerate(apps_to_plot):
        s = velocity["series"].get(app, {})
        y = [s.get(d, 0) for d in days]
        fig.add_trace(go.Scatter(
            x=pd.to_datetime(days), y=y, mode="lines",
            name=display_name(app),
            line=dict(color=palette[i % len(palette)], width=2),
            hovertemplate=f"<b>{display_name(app)}</b><br>%{{x|%b %d}}: %{{y}}<extra></extra>",
        ))
    return _plotly_layout(fig, height=320)


def _uninstall_platform_figure(block: dict, apps_to_plot: list[str]) -> go.Figure:
    fig = go.Figure()
    # Collect the union of platforms across apps so bars line up.
    platforms: list[str] = []
    for a in apps_to_plot:
        for p in block.get("by_app", {}).get(a, {}):
            if p not in platforms:
                platforms.append(p)
    for i, platform in enumerate(platforms):
        y = [block["by_app"].get(a, {}).get(platform, 0) for a in apps_to_plot]
        fig.add_trace(go.Bar(
            x=[display_name(a) for a in apps_to_plot], y=y,
            name=platform,
            marker_color=STACK_COLORS[i % len(STACK_COLORS)],
        ))
    fig.update_layout(barmode="stack")
    return _plotly_layout(fig, height=320)


def _cumulative_figure(monthly: dict, apps_to_plot: list[str]) -> go.Figure:
    fig = go.Figure()
    palette = [PALETTE["primary"], PALETTE["success"], PALETTE["warning"],
               PALETTE["accent"]]
    periods = monthly.get("periods", [])
    x = [fmt_month_short(p) for p in periods]
    for i, app in enumerate(apps_to_plot):
        s = monthly["installs_cumulative"].get(app, {})
        y = [s.get(p, 0) for p in periods]
        fig.add_trace(go.Scatter(
            x=x, y=y, mode="lines+markers",
            name=display_name(app),
            line=dict(color=palette[i % len(palette)], width=2.5),
            fill="tozeroy" if app == "all_apps" else None,
            fillcolor=f"rgba(99, 102, 241, 0.08)" if app == "all_apps" else None,
        ))
    return _plotly_layout(fig, height=320)


# ---------------------------------------------------------------------
# Card + panel renderers
# ---------------------------------------------------------------------

def _kpi_card(
    label: str,
    value: str,
    *,
    sublabel: str = "",
    foot: str = "",
    value_color: str | None = None,
) -> str:
    color_style = f"color:{value_color};" if value_color else ""
    sub_html = f'<div class="kpi-sublabel">{sublabel}</div>' if sublabel else ""
    foot_html = f'<div class="kpi-foot">{foot}</div>' if foot else ""
    return (
        f'<div class="kpi-card">'
        f'<div class="kpi-label">{label}</div>'
        f'{sub_html}'
        f'<div class="kpi-value" style="{color_style}">{value}</div>'
        f'{foot_html}'
        f'</div>'
    )


def _panel_open(title: str, sub: str = "") -> None:
    sub_html = f'<div class="panel-sub">{sub}</div>' if sub else ""
    st.markdown(
        f'<div class="panel">'
        f'<div class="panel-title">{title}</div>'
        f'{sub_html}',
        unsafe_allow_html=True,
    )


def _panel_close() -> None:
    st.markdown("</div>", unsafe_allow_html=True)


# ---------------------------------------------------------------------
# Per-panel CSV export + Plotly chart config
# ---------------------------------------------------------------------

# Module-level counter so duplicate download buttons get unique Streamlit
# widget keys even when the same panel is revisited via state changes.
_DL_COUNTER: dict[str, int] = {}


def _download_csv(df: pd.DataFrame, filename: str, *, label: str = "⬇ Download CSV") -> None:
    """Render a small 'Download CSV' button with the dataframe serialized.
    The key is made unique per (filename, invocation) so rerenders don't
    clobber each other."""
    if df is None or df.empty:
        return
    _DL_COUNTER[filename] = _DL_COUNTER.get(filename, 0) + 1
    key = f"dl_{filename}_{_DL_COUNTER[filename]}"
    try:
        st.download_button(
            label=label,
            data=df.to_csv(index=False).encode("utf-8"),
            file_name=filename,
            mime="text/csv",
            key=key,
            use_container_width=False,
        )
    except Exception:
        # Never let a download-button failure kill the dashboard —
        # worst case the user loses the download, not the panel.
        pass


# Plotly modebar config used for every chart. `displaylogo=False` hides
# the Plotly logo; keeping `toImage` and `resetScale2d` in the modebar
# gives users PNG export (user asked for 'Download PNG per panel'). We
# strip the zoom/pan tools because stakeholders don't need them and they
# clutter the top-right corner of each card.
PLOTLY_CONFIG = {
    "displaylogo": False,
    # Hide the mode bar entirely. The previous config removed specific
    # buttons and disabled scrollZoom, but stakeholders still managed to
    # accidentally trigger zoom via trackpad gestures / pinch / rogue
    # buttons that slipped through the filter. Killing the whole bar is
    # the cleanest fix — hover tooltips still work for reading values,
    # and PNG export will come back as an explicit per-panel button later.
    "displayModeBar": False,
    # Belt-and-braces: even without the modebar, these flags stop
    # stray mouse-wheel / pinch / double-click from warping the plot.
    "scrollZoom": False,
    "doubleClick": False,
    "showAxisDragHandles": False,
    "showAxisRangeEntryBoxes": False,
}


# ---------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------

def _render_sidebar(
    *,
    stamps: list[str],
    available_years: list[int],
    available_periods: list[str],
    available_apps: list[str],
) -> tuple[list[str], str, int | None, str | None, str]:
    """Returns (selected_apps, app_key_for_legacy_charts, year, month, run_stamp).

    selected_apps: list of real app_ids the user picked (never includes
    "all_apps" — that pseudo-key is an internal aggregation label).
    Default is every configured app selected.

    app_key_for_legacy_charts: a single app_id string still expected by
    KPI cards + per-app panels that haven't been refactored to accept a
    list. Derivation rule:
      - exactly 1 selected   → that app's id
      - all real apps selected → "all_apps" (use the pre-aggregated view)
      - partial (2+ but not all) → "all_apps" with a caption noting that
        detailed KPIs still aggregate across every configured app while
        the trend charts already honor the selection per-series.

    This keeps the sidebar contract simple (it returns a legacy key the
    existing callers understand) while giving the user true multi-pick
    control for the charts that already render per-app series.
    """
    with st.sidebar:
        st.markdown(
            '<div class="sidebar-brand">cHAP <span class="sidebar-brand-accent">'
            'Seller</span> Tracker</div>'
            '<div class="sidebar-tagline">Stakeholder Analytics</div>',
            unsafe_allow_html=True,
        )

        st.markdown('<div class="sidebar-section">Filters</div>',
                    unsafe_allow_html=True)

        # Multi-select across every configured app. No more "All Apps"
        # pseudo-option — the user explicitly picks which apps to include.
        # Default is the FIRST app in the registry order (shopify_temu
        # → TEMU US by current apps.yaml order). Stakeholders wanted
        # the dashboard to open on a single real app rather than an
        # all-app aggregate — they can then add more apps to compare.
        selected_apps = st.multiselect(
            "Apps",
            options=available_apps,
            default=[available_apps[0]] if available_apps else [],
            format_func=display_name,
            help=(
                "Pick one or more apps to focus on. The dashboard "
                "opens on the first app; add more to compare side by "
                "side, or select all for the combined view."
            ),
        )
        if not selected_apps:
            # Empty selection is unusable — silently fall back to first.
            selected_apps = available_apps[:1] or list(available_apps)

        # Legacy key for KPI cards + single-app panels that still take
        # one `app_key: str`. All-selected → "all_apps" (pre-aggregated).
        # Single → that app's id. Partial (2+ but not all) → "all_apps"
        # for now, with a note. A later pass can compute true partial
        # aggregates on the fly.
        if len(selected_apps) == 1:
            app_key = selected_apps[0]
        else:
            app_key = "all_apps"

        year_choices = ["All years"] + [str(y) for y in available_years]
        year_label = st.selectbox(
            "Year",
            year_choices,
            index=0,
            help="Restrict installs / uninstalls to a single calendar year. "
                 "Growth rates still compare against the prior period "
                 "(e.g. 2026 vs 2025, Q1 2026 vs Q4 2025).",
        )
        year = None if year_label == "All years" else int(year_label)

        # Month picker — constrained by the selected year when one is
        # picked, otherwise shows every month we have data for. The
        # machine key ('2026-04') is stored so downstream filtering is
        # unambiguous; the UI label is '04/26'.
        if year is not None:
            prefix = f"{year:04d}-"
            candidate_months = [p for p in available_periods if p.startswith(prefix)]
        else:
            candidate_months = list(available_periods)
        month_display_pairs = [("All months", None)] + [
            (fmt_month_short(p), p) for p in candidate_months
        ]
        month_label = st.selectbox(
            "Month",
            [lbl for lbl, _ in month_display_pairs],
            index=0,
            help="Drill into a single month. KPI cards + growth rows "
                 "narrow to this month; trend charts keep their full range "
                 "so you see how that month fits the arc.",
        )
        month_key = next(
            mk for lbl, mk in month_display_pairs if lbl == month_label
        )

        stamp = stamps[0] if stamps else ""

        # NOTE: The "Last Updated" + "Refresh Frequency" block used to
        # live here as a sidebar footer. Per 2026-04-19 feedback it's now
        # rendered in the top header row (see `_render_header`) so users
        # who land on the page cold see freshness at a glance without
        # opening the sidebar. A smaller "Schedule" line stays here as a
        # contextual reminder — not the source of truth for freshness.
        st.markdown(
            """
<div class="sidebar-footer">
    <div class="sidebar-footer-label">Sync Schedule</div>
    <div class="sidebar-footer-value">12 AM + 12 PM IST · daily</div>
    <div style="font-size:0.72rem;">🔄 via GitHub Actions</div>
</div>
            """,
            unsafe_allow_html=True,
        )

        return selected_apps, app_key, year, month_key, stamp


# ---------------------------------------------------------------------
# Available-years helper
# ---------------------------------------------------------------------

def _collect_years(
    sellers_by_app: dict[str, list[dict]],
    uninstalls_by_app: dict[str, list[dict]],
) -> list[int]:
    """Return all calendar years present in install_on / uninstalled_on,
    descending. Powers the sidebar year picker."""
    from analytics_advanced import _parse_iso_date as _parse
    years: set[int] = set()
    for rows in (sellers_by_app or {}).values():
        for r in rows or []:
            d = _parse(r.get("installed_on"))
            if d:
                years.add(d.year)
    for rows in (uninstalls_by_app or {}).values():
        for r in rows or []:
            d = _parse(r.get("uninstalled_on"))
            if d:
                years.add(d.year)
    return sorted(years, reverse=True)


# ---------------------------------------------------------------------
# Main page sections
# ---------------------------------------------------------------------

def _render_header(app_key: str, year: int | None, run_dt: datetime | None) -> None:
    """'Overview' + year/month pill on the left, Last Updated stamp +
    admin chip on the right. The freshness stamp used to live in the
    sidebar footer — moved to the header per 2026-04-19 feedback so
    users see it above the fold on first load."""
    badge_text: str
    if year is not None:
        badge_text = f"{year}"
    elif run_dt is not None:
        badge_text = run_dt.strftime("%b %Y")
    else:
        badge_text = "Latest"

    if run_dt is not None:
        last_updated = run_dt.strftime("%b %d, %Y · %H:%M UTC")
    else:
        last_updated = "unknown"

    left, right = st.columns([5, 3])
    with left:
        st.markdown(
            f'<span class="page-title">Overview</span>'
            f'<span class="page-badge">{badge_text}</span>'
            f'<span class="header-stamp" title="Timestamp of the latest scraper run">'
            f'<span class="header-stamp-dot"></span>'
            f'Last updated · {last_updated}'
            f'</span>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div style="color:{PALETTE["text_soft"]}; font-size:0.85rem; '
            f'margin-top:4px;">Viewing: <b>{display_name(app_key)}</b></div>',
            unsafe_allow_html=True,
        )
    with right:
        st.markdown(
            f'<div style="text-align:right; padding-top:10px; color:{PALETTE["text_soft"]}; '
            'font-size:0.85rem;">'
            f'Hrithik Srivastava · <b>Admin</b>'
            '</div>',
            unsafe_allow_html=True,
        )


def _kpi_row(
    stake: dict,
    app_key: str,
    year: int | None,
    month_key: str | None,
    *,
    growth_source: dict | None = None,
) -> None:
    """Top row — 8 KPI cards in two rows. Each card's value color is
    semantic: indigo = primary count, green = positive/paid, red =
    churn/uninstalls, amber = caution (not-paid, zero-order), violet
    accent. A legend strip is rendered above the cards so stakeholders
    can map color → meaning at a glance.

    `growth_source` is an optional separate report (typically the full
    unfiltered one) used solely for the MoM growth% card. This lets
    Jan 2026 MoM reference Dec 2025 even when the year filter would
    have hidden Dec 2025 from `stake`.
    """
    paid = stake["paid"]
    totals = paid["totals"]
    active = totals.get(app_key, 0)

    monthly = stake["monthly"]
    periods = monthly.get("periods", [])

    # Pick the "focus period" for New Installs / Uninstalls cards.
    # Precedence: specific month > selected year > latest month.
    if month_key is not None:
        new_installs = monthly["installs"].get(app_key, {}).get(month_key, 0)
        new_uninstalls = monthly["uninstalls"].get(app_key, {}).get(month_key, 0)
        period_sub = f"Month: {fmt_month_short(month_key)}"
        mom_ref = month_key
    elif year is not None:
        year_prefix = f"{year:04d}"
        year_periods = [p for p in periods if p.startswith(year_prefix)]
        new_installs = sum(monthly["installs"].get(app_key, {}).get(p, 0)
                           for p in year_periods)
        new_uninstalls = sum(monthly["uninstalls"].get(app_key, {}).get(p, 0)
                             for p in year_periods)
        period_sub = f"In {year}"
        mom_ref = year_periods[-1] if year_periods else None
    elif periods:
        mom_ref = periods[-1]
        new_installs = monthly["installs"].get(app_key, {}).get(mom_ref, 0)
        new_uninstalls = monthly["uninstalls"].get(app_key, {}).get(mom_ref, 0)
        period_sub = f"Month: {fmt_month_short(mom_ref)}"
    else:
        new_installs = 0
        new_uninstalls = 0
        period_sub = "—"
        mom_ref = None

    # MoM growth — referenced to the focus period, computed over the
    # FULL time series (so Jan 2026 can compare to Dec 2025 even when
    # the year filter hides earlier months).
    growth_block = (growth_source or stake)["monthly"]
    growth_series = growth_block.get("installs_growth_pct", {}).get(app_key, {})
    if mom_ref is not None and growth_series.get(mom_ref) is not None:
        mom = growth_series[mom_ref]
        mom_str = f"{mom:+.1f}%"
        mom_color = PALETTE["success"] if mom >= 0 else PALETTE["danger"]
    else:
        mom_str = "—"
        mom_color = PALETTE["text_soft"]

    paid_count = paid["by_app"].get(app_key, {}).get("Paid", 0)
    notpaid_count = paid["by_app"].get(app_key, {}).get("Not Paid", 0)

    # Color-legend strip so every card's color is interpretable at a glance.
    swatch = lambda c, label: (
        f'<span style="display:inline-flex; align-items:center; gap:6px; '
        f'margin-right:14px; color:{PALETTE["text_soft"]}; font-size:0.78rem;">'
        f'<span style="width:10px; height:10px; border-radius:2px; '
        f'background:{c}; display:inline-block;"></span>{label}</span>'
    )
    st.markdown(
        '<div style="margin-bottom:10px;">'
        + swatch(PALETTE["primary"], "Primary count")
        + swatch(PALETTE["success"], "Installs · Paid · Growth↑")
        + swatch(PALETTE["danger"], "Uninstalls · Growth↓")
        + swatch(PALETTE["warning"], "Not Paid · Caution")
        + swatch(PALETTE["accent"], "Zero-order accent")
        + '</div>',
        unsafe_allow_html=True,
    )

    row1 = st.columns(4)
    row1[0].markdown(
        _kpi_card(
            "👥 Active Installs",
            f"{active:,}",
            sublabel=f"Across {display_name(app_key)}",
            value_color=PALETTE["primary"],
        ),
        unsafe_allow_html=True,
    )
    row1[1].markdown(
        _kpi_card(
            "🟢 New Installs",
            f"{new_installs:,}",
            sublabel=period_sub,
            value_color=PALETTE["success"],
        ),
        unsafe_allow_html=True,
    )
    row1[2].markdown(
        _kpi_card(
            "🔴 Uninstalls",
            f"{new_uninstalls:,}",
            sublabel=period_sub,
            value_color=PALETTE["danger"],
        ),
        unsafe_allow_html=True,
    )
    row1[3].markdown(
        _kpi_card(
            "📈 MoM Growth",
            mom_str,
            sublabel="Installs vs prior month",
            value_color=mom_color,
        ),
        unsafe_allow_html=True,
    )

    st.write("")  # small gap
    row2 = st.columns(4)
    row2[0].markdown(
        _kpi_card(
            "💳 Paid Sellers",
            f"{paid_count:,}",
            sublabel="On a real plan",
            value_color=PALETTE["success"],
        ),
        unsafe_allow_html=True,
    )
    row2[1].markdown(
        _kpi_card(
            "🕗 Not Paid",
            f"{notpaid_count:,}",
            sublabel="Free / no plan",
            value_color=PALETTE["warning"],
        ),
        unsafe_allow_html=True,
    )
    # Activity numbers
    act = stake["activity"]["by_app"].get(app_key, {})
    row2[2].markdown(
        _kpi_card(
            "📦 Active (≥1 order)",
            f"{act.get('active_sellers', 0):,}",
            sublabel=f"of {act.get('total_sellers', 0):,} sellers",
            value_color=PALETTE["primary"],
        ),
        unsafe_allow_html=True,
    )
    row2[3].markdown(
        _kpi_card(
            "⚠️ Zero-order",
            f"{act.get('zero_order_sellers', 0):,}",
            sublabel="Installed but never transacted",
            value_color=PALETTE["accent"],
        ),
        unsafe_allow_html=True,
    )


def _trend_panels(
    stake: dict,
    app_key: str,
    month_key: str | None = None,
    selected_apps: list[str] | None = None,
) -> None:
    """Two side-by-side panels: Installs trend line + Installs/Uninstalls bars.
    Matches the reference 'Revenue Trend' + 'Installs vs Uninstalls' duo.

    `selected_apps` (new): the explicit list of apps the user picked in
    the sidebar multiselect. Drives which lines render on the trend
    chart — one series per selected app. When None (legacy callers)
    falls back to the old behavior (derive from app_key).
    """
    if selected_apps:
        apps_for_lines = [a for a in selected_apps if a != "all_apps"]
    else:
        apps_for_lines = (
            [a for a in APP_KEYS if a != "all_apps"]
            if app_key == "all_apps"
            else [app_key]
        )
    # Decide which monthly series to render on the right-hand (bar) chart.
    # 'All Apps' view shows the combined total so stakeholders see the
    # whole picture; a single-app view shows just that app.
    ivu_app = "all_apps" if app_key == "all_apps" else app_key

    # Subtitle text spells out what each counting rule actually is. The
    # per-seller dedup note lives here because the user called it out
    # explicitly: a seller who removed both Shopify + Shein in the same
    # month should be ONE uninstall event, not two. (Fixed 2026-04-24.)
    if app_key == "all_apps":
        ivu_sub = (
            "Green bars = sellers who installed that month, summed across "
            "all three apps. Red bars (below the axis) = sellers who "
            "uninstalled, also summed across apps. A seller is counted "
            "once per app per month — removing Shopify + Shein in the "
            "same month is 1 uninstall on SHEIN, not 2."
        )
    else:
        ivu_sub = (
            f"Green bars = sellers who newly installed {display_name(app_key)} "
            "that month. Red bars (below the axis) = sellers who uninstalled. "
            "A seller who removed multiple platforms in the same month is "
            "counted once."
        )
    if month_key is not None:
        ivu_sub += (
            f" · Narrowed to the 12 months ending **{fmt_month_short(month_key)}**."
        )

    # Narrow the data window when a specific month is picked. Previous
    # behavior showed the full history with the picked month highlighted
    # — stakeholders asked for the chart to actually respect the filter,
    # not just annotate it. 12-month window keeps enough context that
    # trends remain readable while honoring the user's selection.
    monthly_view = _narrow_monthly(stake["monthly"], month_key, window=12)
    axis_note = (
        f"x-axis: months (ending {fmt_month_short(month_key)})"
        if month_key else "x-axis: all months with data"
    )

    col1, col2 = st.columns(2)
    with col1:
        _panel_open(
            "Install Trend",
            f"How many sellers newly installed the app each month. "
            f"One line per app; {axis_note}.",
        )
        st.plotly_chart(
            _install_trend_figure(monthly_view, apps_for_lines),
            use_container_width=True,
            config=PLOTLY_CONFIG,
        )
        periods = monthly_view.get("periods", [])
        installs = monthly_view.get("installs", {})
        trend_rows = []
        for p in periods:
            row = {"Month": fmt_month_short(p)}
            for a in apps_for_lines:
                row[display_name(a)] = installs.get(a, {}).get(p, 0)
            trend_rows.append(row)
        _download_csv(pd.DataFrame.from_records(trend_rows),
                      f"install_trend_{app_key}.csv")
        _panel_close()
    with col2:
        _panel_open("Installs vs Uninstalls", ivu_sub)
        st.plotly_chart(
            _installs_vs_uninstalls_figure(
                monthly_view, app_key=ivu_app, highlight_period=month_key,
            ),
            use_container_width=True,
            config=PLOTLY_CONFIG,
        )
        uninstalls = monthly_view.get("uninstalls", {})
        ivu_rows = []
        for p in monthly_view.get("periods", []):
            ivu_rows.append({
                "Month": fmt_month_short(p),
                "Installs": installs.get(ivu_app, {}).get(p, 0),
                "Uninstalls": uninstalls.get(ivu_app, {}).get(p, 0),
            })
        _download_csv(pd.DataFrame.from_records(ivu_rows),
                      f"installs_vs_uninstalls_{app_key}.csv")
        _panel_close()


def _narrow_monthly(monthly: dict, month_key: str | None, *, window: int = 12) -> dict:
    """Return a shallow copy of `monthly` with `periods` clipped to a
    window ending at month_key. When month_key is None or absent from
    periods, returns monthly unchanged."""
    if not month_key:
        return monthly
    periods = monthly.get("periods", []) or []
    if month_key not in periods:
        return monthly
    end = periods.index(month_key) + 1
    start = max(0, end - window)
    return {**monthly, "periods": periods[start:end]}


def _paid_panel(stake: dict, app_key: str) -> None:
    """Paid vs Not Paid — replaces the old Plan Distribution per user
    feedback ('So I just want paid and not paid. For all apps.')."""
    paid = stake["paid"]
    apps_to_plot = (
        [a for a in APP_KEYS if a != "all_apps"]
        if app_key == "all_apps"
        else [app_key]
    )
    _panel_open(
        "Paid vs Not Paid",
        "Sellers split by subscription status. 'Paid' means the seller has "
        "a real plan name on their account; 'Not Paid' is blank / N/A / "
        "free / trial. Counts are a snapshot as of the latest run.",
    )
    st.plotly_chart(
        _paid_bar_figure(paid, apps_to_plot),
        use_container_width=True,
        config=PLOTLY_CONFIG,
    )
    df = pd.DataFrame.from_records([
        {
            "App": display_name(a),
            "Paid": paid["by_app"].get(a, {}).get("Paid", 0),
            "Not Paid": paid["by_app"].get(a, {}).get("Not Paid", 0),
        }
        for a in apps_to_plot
    ])
    _download_csv(df, f"paid_vs_notpaid_{app_key}.csv")
    _panel_close()


def _plan_breakdown_panel(stake: dict, app_key: str) -> None:
    """Plan Breakdown — new in 2026-04-19 after the Customize Grid fix
    landed real plan data (SHEIN: 17 distinct plans, TEMU US: 7, TEMU EU: 8).
    Shows the top plans by seller count with the Paid / Not Paid roll-up
    made explicit.

    Why a separate panel: Paid vs Not Paid tells you the binary; this tells
    you *which* paid plans matter. Stakeholders asked for this specifically
    after seeing the full plan variety in the scrape."""
    plan_dim = stake["dimensions"].get("plan") or {}
    breakdown_all = plan_dim.get("breakdown", {})

    # When All Apps is selected, show a stacked bar of plan × app so the
    # viewer can see which app each plan belongs to. For a single app,
    # show a horizontal bar chart sorted by count.
    if app_key == "all_apps":
        apps = [a for a in APP_KEYS if a != "all_apps"]
        # Collect all plan labels across apps
        per_app_counts: dict[str, dict[str, int]] = {}
        all_labels: set[str] = set()
        for a in apps:
            counts = breakdown_all.get(a, {})
            per_app_counts[a] = counts
            all_labels.update(counts.keys())
        # Drop '(not set)' / 'N/A' — those are Not-Paid noise, already
        # counted in the Paid vs Not Paid panel.
        NOT_PAID_LABELS = {"(not set)", "n/a", "na", ""}
        plan_labels = [l for l in all_labels if l.strip().lower() not in NOT_PAID_LABELS]
        if not plan_labels:
            return
        # Order labels by total count desc; cap at 20 for readability.
        label_totals = {
            lbl: sum(per_app_counts[a].get(lbl, 0) for a in apps)
            for lbl in plan_labels
        }
        ordered = sorted(label_totals.items(), key=lambda kv: -kv[1])[:20]
        labels_order = [lbl for lbl, _ in ordered]

        _panel_open(
            "Plan Breakdown",
            "Top plans by seller count. Groups the active seller base by the "
            "exact plan name on their account. 'N/A' and blank plans are "
            "excluded — those are covered in the Paid vs Not Paid panel. "
            "Stacked by app so you can see which marketplace each plan comes from.",
        )
        rows = []
        for lbl in labels_order:
            for a in apps:
                rows.append({
                    "Plan": lbl,
                    "App": display_name(a),
                    "Sellers": per_app_counts[a].get(lbl, 0),
                })
        df_long = pd.DataFrame.from_records(rows)
        fig = px.bar(
            df_long, x="Sellers", y="Plan", color="App", orientation="h",
            color_discrete_sequence=[
                PALETTE["primary"], PALETTE["success"], PALETTE["accent"],
            ],
        )
        fig.update_layout(
            yaxis=dict(autorange="reversed", categoryorder="array",
                       categoryarray=labels_order),
            barmode="stack",
        )
        st.plotly_chart(_plotly_layout(fig, height=max(340, 26 * len(labels_order) + 80)),
                        use_container_width=True, config=PLOTLY_CONFIG)
        # CSV: wide format — one row per plan with per-app + total columns.
        df_wide_rows = []
        for lbl in labels_order:
            row = {"Plan": lbl}
            total = 0
            for a in apps:
                v = per_app_counts[a].get(lbl, 0)
                row[display_name(a)] = v
                total += v
            row["Total"] = total
            df_wide_rows.append(row)
        _download_csv(pd.DataFrame.from_records(df_wide_rows), "plan_breakdown_all_apps.csv")
        _panel_close()
        return

    # Single-app view
    counts = breakdown_all.get(app_key, {}) or {}
    NOT_PAID_LABELS = {"(not set)", "n/a", "na", ""}
    filtered = [
        (lbl, n) for lbl, n in counts.items()
        if lbl.strip().lower() not in NOT_PAID_LABELS
    ]
    if not filtered:
        return
    filtered.sort(key=lambda kv: -kv[1])
    filtered = filtered[:20]
    df = pd.DataFrame.from_records(
        [{"Plan": lbl, "Sellers": n} for lbl, n in filtered]
    )
    _panel_open(
        "Plan Breakdown",
        f"Top plans by seller count for {display_name(app_key)}. 'N/A' and "
        "blank plans are excluded — those are covered in the Paid vs Not Paid "
        "panel. Use this to see which paid tiers actually have traction.",
    )
    fig = px.bar(
        df, x="Sellers", y="Plan", orientation="h",
        color="Sellers",
        color_continuous_scale=["#c7d2fe", PALETTE["primary"]],
        text="Sellers",
    )
    fig.update_traces(textposition="outside")
    fig.update_coloraxes(showscale=False)
    fig.update_layout(yaxis=dict(autorange="reversed"))
    st.plotly_chart(_plotly_layout(fig, height=max(280, 28 * len(df) + 60)),
                    use_container_width=True, config=PLOTLY_CONFIG)
    _download_csv(df, f"plan_breakdown_{app_key}.csv")
    _panel_close()


def _order_activity_panel(stake: dict, app_key: str) -> None:
    apps = (
        [a for a in APP_KEYS if a != "all_apps"]
        if app_key == "all_apps"
        else [app_key]
    )
    _panel_open(
        "Order Activity Segmentation",
        "Each seller bucketed by their lifetime order count (0, 1–10, "
        "11–50, …). Answers 'of the sellers we have, how many are "
        "actually transacting and at what volume?' The table below "
        "breaks that same view down by Paid vs Not Paid.",
    )
    st.plotly_chart(
        _activity_stacked_figure(stake["activity"], apps, value_col_prefix="order"),
        use_container_width=True,
        config=PLOTLY_CONFIG,
    )

    # Paid / Not Paid split inside activity.
    rows = []
    for a in apps:
        d = stake["activity_by_paid"].get(a, {})
        paid_d = d.get("Paid", {})
        nop_d = d.get("Not Paid", {})
        rows.append({
            "App": display_name(a),
            "Paid — Active": paid_d.get("active", 0),
            "Paid — Zero-order": paid_d.get("zero_order", 0),
            "Not Paid — Active": nop_d.get("active", 0),
            "Not Paid — Zero-order": nop_d.get("zero_order", 0),
            "Total orders (Paid)": paid_d.get("total_orders", 0),
            "Total orders (Not Paid)": nop_d.get("total_orders", 0),
        })
    df_act = pd.DataFrame.from_records(rows) if rows else pd.DataFrame()
    if not df_act.empty:
        st.dataframe(df_act, hide_index=True, use_container_width=True)
    _download_csv(df_act, f"order_activity_{app_key}.csv")
    _panel_close()


def _product_activity_panel(stake: dict, app_key: str) -> None:
    apps = (
        [a for a in APP_KEYS if a != "all_apps"]
        if app_key == "all_apps"
        else [app_key]
    )
    _panel_open(
        "Product Activity Segmentation",
        "Each seller bucketed by how many products they've listed "
        "(0, 1–10, 11–100, …). Answers 'how many sellers actually "
        "populated a catalog vs installed and stopped?' Mirror of Order "
        "Activity, but measures setup effort rather than sales.",
    )
    st.plotly_chart(
        _activity_stacked_figure(stake["product_activity"], apps,
                                 value_col_prefix="product"),
        use_container_width=True,
        config=PLOTLY_CONFIG,
    )
    # Paid/Not Paid product totals
    rows = []
    for a in apps:
        d = stake["activity_by_paid"].get(a, {})
        rows.append({
            "App": display_name(a),
            "Paid — Products": d.get("Paid", {}).get("total_products", 0),
            "Not Paid — Products": d.get("Not Paid", {}).get("total_products", 0),
        })
    df_prod = pd.DataFrame.from_records(rows) if rows else pd.DataFrame()
    if not df_prod.empty:
        st.dataframe(df_prod, hide_index=True, use_container_width=True)
    _download_csv(df_prod, f"product_activity_{app_key}.csv")
    _panel_close()


def _framework_panel(stake: dict, app_key: str) -> None:
    """Only shown for TEMU EU. The other apps are Shopify-only so the
    chart would be a single bar."""
    # Selected app must be TEMU EU (or All Apps, in which case we only
    # render the TEMU EU slice).
    if app_key not in MULTI_PLATFORM_APPS and app_key != "all_apps":
        return

    plat = stake["dimensions"]["platforms"]
    target = "shopify_temu_eu"
    counts = plat["breakdown"].get(target, {})
    if not counts:
        return
    # Strip the "(not set)" bucket if it would dominate the chart.
    non_empty = {k: v for k, v in counts.items() if k != "(not set)"}
    if not non_empty:
        return

    _panel_open(
        "Framework / Platform Combo",
        f"Which ecommerce framework {display_name(target)} sellers are "
        "running on (Shopify, Prestashop, BigCommerce, etc.). Only "
        "rendered for TEMU EU — SHEIN and TEMU US are Shopify-only, so "
        "a single-bar chart would be noise.",
    )
    df = pd.DataFrame(
        sorted(non_empty.items(), key=lambda kv: -kv[1]),
        columns=["label", "count"],
    )
    fig = px.bar(
        df, x="count", y="label", orientation="h",
        color_discrete_sequence=[PALETTE["accent"]],
        text="count",
    )
    fig.update_traces(textposition="outside")
    fig.update_layout(yaxis=dict(autorange="reversed"))
    st.plotly_chart(_plotly_layout(fig, height=max(260, 28 * len(df) + 60)),
                    use_container_width=True,
                    config=PLOTLY_CONFIG)
    _download_csv(df.rename(columns={"label": "Framework", "count": "Sellers"}),
                  "framework_temu_eu.csv")
    _panel_close()


def _source_country_panel(stake: dict, app_key: str) -> None:
    dim = stake["dimensions"]["source_country"]
    counts = dim["breakdown"].get(app_key, {})
    non_empty = {k: v for k, v in counts.items() if k != "(not set)"}
    if not non_empty:
        return
    top_n = 15
    top = dict(sorted(non_empty.items(), key=lambda kv: -kv[1])[:top_n])
    df = pd.DataFrame(list(top.items()), columns=["country", "sellers"])
    _panel_open(
        "Source Country Distribution",
        f"Where {display_name(app_key)} sellers say they're based. "
        f"Top {top_n} countries by seller count. Blank / '(not set)' "
        "values are excluded.",
    )
    fig = px.bar(
        df, x="sellers", y="country", orientation="h",
        color="sellers",
        color_continuous_scale=["#c7d2fe", PALETTE["primary"]],
        text="sellers",
    )
    fig.update_traces(textposition="outside")
    fig.update_coloraxes(showscale=False)
    fig.update_layout(yaxis=dict(autorange="reversed"))
    st.plotly_chart(_plotly_layout(fig, height=max(320, 28 * len(df) + 60)),
                    use_container_width=True,
                    config=PLOTLY_CONFIG)
    _download_csv(df, f"source_country_{app_key}.csv")
    _panel_close()


def _velocity_panel(stake: dict, app_key: str, year: int | None) -> None:
    apps = (
        [a for a in APP_KEYS if a != "all_apps"]
        if app_key == "all_apps"
        else [app_key]
    )
    sub = (
        "On any given day, how many new installs happened in the prior "
        "30 days (a rolling sum). A smoother read of momentum than the "
        "raw daily install count. "
        + (f"Showing {year} only." if year else "Full window.")
    )
    _panel_open("Install Velocity", sub)
    st.plotly_chart(
        _velocity_figure(stake["install_velocity"], apps),
        use_container_width=True,
        config=PLOTLY_CONFIG,
    )
    # CSV: per-app rolling-30d series
    vel = stake["install_velocity"]
    series = vel.get("series", {}) if isinstance(vel, dict) else {}
    all_days = sorted({d for a in apps for d in (series.get(a, {}) or {}).keys()})
    vel_rows = []
    for d in all_days:
        row = {"Date": d}
        for a in apps:
            row[display_name(a)] = (series.get(a, {}) or {}).get(d, 0)
        vel_rows.append(row)
    _download_csv(pd.DataFrame.from_records(vel_rows),
                  f"install_velocity_{app_key}.csv")
    _panel_close()


def _uninstall_platform_panel(stake: dict, app_key: str) -> None:
    if app_key not in MULTI_PLATFORM_APPS and app_key != "all_apps":
        return
    block = stake["uninstall_platform_split"]
    target = "shopify_temu_eu"
    if target not in block["by_app"] or not block["by_app"][target]:
        return
    _panel_open(
        "Uninstall Platform Split",
        "Of TEMU EU sellers who uninstalled, which framework they were "
        "running (Shopify, Prestashop, etc.). Useful for spotting "
        "whether churn is concentrated on one platform.",
    )
    st.plotly_chart(
        _uninstall_platform_figure(block, [target]),
        use_container_width=True,
        config=PLOTLY_CONFIG,
    )
    plat_counts = block["by_app"].get(target, {})
    _download_csv(
        pd.DataFrame.from_records([
            {"Platform": k, "Uninstalls": v} for k, v in plat_counts.items()
        ]),
        "uninstall_platform_temu_eu.csv",
    )
    _panel_close()


def _cumulative_panel(stake: dict, app_key: str) -> None:
    apps = (
        [a for a in APP_KEYS if a != "all_apps"]
        if app_key == "all_apps"
        else [app_key]
    )
    _panel_open(
        "Cumulative Active Sellers",
        "Running total of the live seller base, month by month. At each "
        "point, the line shows how many sellers had installed by that "
        "month AND are still active today (not uninstalled). In short: "
        "'how big was our live base at the end of that month?'",
    )
    st.plotly_chart(
        _cumulative_figure(stake["monthly"], apps + (["all_apps"] if app_key == "all_apps" else [])),
        use_container_width=True,
        config=PLOTLY_CONFIG,
    )
    # CSV: monthly cumulative per app
    periods_c = stake["monthly"].get("periods", [])
    cum_series = stake["monthly"].get("cumulative_active", {})
    cum_rows = []
    for p in periods_c:
        row = {"Month": fmt_month_short(p)}
        for a in apps:
            row[display_name(a)] = cum_series.get(a, {}).get(p, 0)
        cum_rows.append(row)
    _download_csv(pd.DataFrame.from_records(cum_rows),
                  f"cumulative_active_{app_key}.csv")
    _panel_close()


def _growth_tables_panel(
    stake: dict, app_key: str, year: int | None, month_key: str | None,
) -> None:
    """Growth-rate tables. Δ% is computed over the FULL unfiltered time
    series — so Q1 2026 can compare against Q4 2025, and 2026 YoY can
    compare against 2025 — even when the year filter hides rows from
    display. Year and month filters narrow which rows are rendered;
    the deltas themselves keep their cross-year references."""
    _panel_open(
        "Growth Rates",
        "Period-over-period percentage change in installs. '—' means "
        "no prior period exists to compare against. Deltas span across "
        "year boundaries (Q1/26 vs Q4/25, 2026 vs 2025).",
    )

    def _table(block: dict, key_fmt, label: str, scope: str) -> None:
        growth = block.get("installs_growth_pct", {}).get(app_key, {})
        periods = block.get("periods", [])

        # Display-row filtering depends on the scope of the table:
        #   - 'month'   : hide rows outside the selected year/month
        #   - 'quarter' : hide rows outside the selected year
        #   - 'year'    : keep both the selected year AND the prior
        #                  year (prior is the comparison basis — hiding
        #                  it would leave the user wondering what 2026
        #                  is compared against)
        def _keep(p: str) -> bool:
            if scope == "month":
                if month_key is not None:
                    return p == month_key
                if year is not None:
                    return p.startswith(f"{year:04d}-")
                return True
            if scope == "quarter":
                if year is not None:
                    return p.startswith(f"{year:04d}-")
                return True
            if scope == "year":
                if year is not None:
                    return int(p) in (year, year - 1)
                return True
            return True

        rows = []
        for p in periods:
            if not _keep(p):
                continue
            v = growth.get(p)
            rows.append({
                label: key_fmt(p),
                "Installs Δ %": "—" if v is None else f"{v:+.1f}%",
            })
        st.markdown(f"**{label}**")
        if rows:
            st.dataframe(pd.DataFrame.from_records(rows),
                         hide_index=True, use_container_width=True,
                         height=min(350, 38 * len(rows) + 40))
        else:
            st.caption("_No periods in the selected window._")

    col1, col2, col3 = st.columns(3)
    with col1:
        _table(stake["monthly"], fmt_month_short, "Month", "month")
    with col2:
        _table(stake["quarterly"], fmt_quarter_short, "Quarter", "quarter")
    with col3:
        _table(stake["yearly"], fmt_year, "Year", "year")

    # Combined CSV for the whole growth-tables panel — one workbook-like
    # file with monthly / quarterly / yearly stacked. Simpler for
    # stakeholders than three separate downloads.
    combined_rows: list[dict] = []
    for block, key_fmt, label in [
        (stake["monthly"], fmt_month_short, "Month"),
        (stake["quarterly"], fmt_quarter_short, "Quarter"),
        (stake["yearly"], fmt_year, "Year"),
    ]:
        growth = block.get("installs_growth_pct", {}).get(app_key, {})
        for p in block.get("periods", []):
            v = growth.get(p)
            combined_rows.append({
                "Scope": label,
                "Period": key_fmt(p),
                "Installs Δ %": "" if v is None else f"{v:+.1f}",
            })
    _download_csv(pd.DataFrame.from_records(combined_rows),
                  f"growth_rates_{app_key}.csv")
    _panel_close()


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

@wrap_page
def main() -> None:
    # ------------------------------------------------------------
    # Auth gate — must run BEFORE st.set_page_config, because the
    # login-prompt path inside auth.gate() calls set_page_config
    # itself. If the user is already signed in, gate() returns a
    # UserPrincipal and we proceed to configure the main dashboard
    # page layout below.
    #
    # Access rules (see MULTI_APP_DESIGN.md §4):
    #   - any @threecolts.com user can view the dashboard
    #   - editors get a "Go to Admin" link in the sidebar
    #   - super admins additionally see the Users tab inside Admin
    # ------------------------------------------------------------
    principal = auth.gate()
    auth.require("view_dashboard", principal)

    st.set_page_config(
        page_title="cHAP Seller Tracker",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    apply_shared_theme()  # shared sidebar / button look across pages
    _inject_css()

    # Sidebar: identity + (if allowed) link to admin.
    auth.sign_out_button(st)
    if roles.can(principal, "see_admin_tab"):
        with st.sidebar:
            st.page_link(
                "pages/Admin.py",
                label="⚙ Admin panel",
                help="Add new scraper sources and (super admin) manage users.",
            )

    stamps = _list_run_stamps()
    if not stamps:
        st.warning(
            "No runs found under `results/history/`. "
            "Run `python3 scraper.py` or `python3 pipeline.py` first."
        )
        return

    # Latest run — stakeholders want the newest snapshot.
    latest_stamp = stamps[0]
    run = _load_run(latest_stamp)
    sellers_by_app = run.get("data") or {}
    unins_by_app = _uninstalls_from_run(run)

    # Normalise before anything else, then strip test stores.
    sellers_by_app, unins_by_app = normalize_run_data(sellers_by_app, unins_by_app)
    sellers_by_app = exclude_test_stores(sellers_by_app)
    unins_by_app = exclude_test_stores(unins_by_app)

    # Year choices derive from the data we have.
    available_years = _collect_years(sellers_by_app, unins_by_app)

    # Build a FULL report first so we know what month periods exist —
    # the sidebar Month picker is driven from this and the Growth tables
    # compute deltas over the full series (so Q1/26 can reference Q4/25
    # and 2026 can reference 2025 even when the year filter is active).
    stamp = stamps[0]
    stake_full = build_stakeholder_report(
        sellers_by_app=sellers_by_app,
        uninstalls_by_app=unins_by_app,
        run_stamp=stamp,
        drop_test_stores=False,  # already stripped above
    )
    available_periods = stake_full["monthly"].get("periods", [])

    # Rebind APP_KEYS to whatever the registry + data actually has.
    # Picks up newly onboarded apps (shein_woocommerce,
    # shopify_gearexchange, …) without editing the hardcoded list above.
    discovered = _discover_app_keys(sellers_by_app, unins_by_app)
    APP_KEYS.clear()
    APP_KEYS.extend(discovered)

    # Which apps the data actually has — sidebar multiselect is sourced
    # from this so we never offer apps the dataset doesn't include.
    available_apps = [
        a for a in APP_KEYS
        if a != "all_apps" and (
            (sellers_by_app or {}).get(a) or (unins_by_app or {}).get(a)
        )
    ]
    if not available_apps:
        # First-ever run or empty dataset — fall back to the registry
        # order so the multiselect isn't empty.
        available_apps = [a for a in APP_KEYS if a != "all_apps"]

    selected_apps, app_key, year, month_key, stamp = _render_sidebar(
        stamps=stamps,
        available_years=available_years,
        available_periods=available_periods,
        available_apps=available_apps,
    )

    # If the user narrowed the app selection but didn't reduce to a
    # single app, the legacy KPI pipeline still aggregates across every
    # configured app (see _render_sidebar docstring). Let them know
    # upfront so the data isn't misleading.
    if 1 < len(selected_apps) < len(available_apps):
        picked = ", ".join(display_name(a) for a in selected_apps)
        st.info(
            f"Showing **{picked}**. Trend charts render one line per "
            f"selected app. Aggregate KPI cards and growth tables still "
            f"sum across every configured app — true partial-selection "
            f"aggregates are coming in a follow-up."
        )

    # Apply year filter — installs by installed_on, uninstalls by
    # uninstalled_on — for the "demographic" report. Growth tables and
    # the KPI MoM card keep using stake_full so cross-year comparisons
    # remain visible.
    sellers_year = filter_by_year(sellers_by_app, date_field="installed_on", year=year)
    unins_year = filter_by_year(unins_by_app, date_field="uninstalled_on", year=year)

    stake = build_stakeholder_report(
        sellers_by_app=sellers_year,
        uninstalls_by_app=unins_year,
        run_stamp=stamp,
        drop_test_stores=False,  # already stripped above
    )

    # ------------ Header + KPI row ------------
    run_dt = _parse_stamp_dt(stamp)
    _render_header(app_key, year, run_dt)
    st.write("")  # small breathing room between header and KPIs

    # KPI row uses stake_full's growth series so the MoM card can
    # reference a prior month even if the year filter would have hidden
    # it (e.g. Jan 2026 MoM comparing to Dec 2025).
    _kpi_row(stake, app_key, year, month_key, growth_source=stake_full)
    st.write("")

    # ------------ Trend duo ------------
    # Passing month_key lets the Installs vs Uninstalls bars highlight the
    # selected month so the sidebar filter is visibly reflected on the chart
    # (user flagged the filter 'does nothing' when only KPIs updated).
    _trend_panels(stake, app_key, month_key=month_key, selected_apps=selected_apps)

    # ------------ Paid vs Not Paid ------------
    _paid_panel(stake, app_key)

    # ------------ Plan Breakdown (new 2026-04-19) ------------
    # Sits right after Paid vs Not Paid because it drills into the Paid
    # slice by exact plan name. Only meaningful now that the Customize
    # Grid fix lands full plan data across all apps.
    _plan_breakdown_panel(stake, app_key)

    # ------------ Activity panels ------------
    _order_activity_panel(stake, app_key)
    _product_activity_panel(stake, app_key)

    # ------------ Country distribution ------------
    _source_country_panel(stake, app_key)

    # ------------ Framework (TEMU EU only) ------------
    _framework_panel(stake, app_key)

    # ------------ Install Velocity ------------
    _velocity_panel(stake, app_key, year)

    # ------------ Uninstall Platform (TEMU EU only) ------------
    _uninstall_platform_panel(stake, app_key)

    # ------------ Cumulative curve ------------
    _cumulative_panel(stake, app_key)

    # ------------ Growth tables ------------
    # Always driven from stake_full so deltas cross year boundaries.
    _growth_tables_panel(stake_full, app_key, year, month_key)

    # ------------ Raw markdown digest (if pipeline wrote one) ------------
    report_path = REPORTS_DIR / f"{stamp}.md"
    if report_path.exists():
        with st.expander("📝 Markdown digest (from pipeline.py)"):
            st.markdown(report_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
