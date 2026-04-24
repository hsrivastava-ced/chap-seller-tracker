"""
ui_theme.py — shared visual constants + a tiny CSS snippet applied to
every Streamlit page in this repo.

Before this module existed:
  - `.streamlit/config.toml` set the global theme (primary color,
    backgrounds, text)
  - dashboard.py's `_inject_css()` styled the sidebar + KPI cards +
    trend panels
  - admin_ui.py used Streamlit's default sidebar
The mismatch was visible: the Dashboard had a dark slate sidebar, the
Admin page had a light gray one, same app. This module keeps a single
minimal CSS snippet (sidebar + primary-button polish) so every page
calls `apply_shared_theme()` at the top of its `main()` and looks
coherent. Dashboard still adds its extra KPI / panel styles on top.

Usage:
    from ui_theme import PALETTE, apply_shared_theme

    def main():
        auth.gate()
        st.set_page_config(...)
        apply_shared_theme()
        ...
"""
from __future__ import annotations

import streamlit as st


# The canonical palette. Copied from dashboard.py's PALETTE dict so both
# sides stay in sync — if you update one, update the other. Moving the
# palette into this module long-term is cleaner but risks a big churn
# diff; keeping it mirrored for now.
PALETTE: dict[str, str] = {
    "primary": "#6366f1",
    "primary_soft": "#a5b4fc",
    "success": "#10b981",
    "success_soft": "#6ee7b7",
    "danger": "#ef4444",
    "danger_soft": "#fca5a5",
    "warning": "#f59e0b",
    "warning_soft": "#fcd34d",
    "accent": "#8b5cf6",
    "neutral": "#94a3b8",
    "bg": "#f3f4f6",
    "card": "#ffffff",
    "text": "#0f172a",
    "text_soft": "#64748b",
    "sidebar_bg": "#1e293b",
    "sidebar_text": "#e2e8f0",
    "sidebar_muted": "#94a3b8",
}


_SHARED_CSS = f"""
<style>
/* ---- sidebar — dark slate, same across Admin + Dashboard ---- */
/* !important is required here because Streamlit's built-in theme
   (secondaryBackgroundColor) wins specificity otherwise and forces
   a light-gray sidebar. Cover both stSidebar and its inner content
   wrapper since the outer selector only reaches the panel. */
section[data-testid="stSidebar"],
section[data-testid="stSidebar"] > div,
div[data-testid="stSidebarContent"],
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {{
    background-color: {PALETTE["sidebar_bg"]} !important;
}}
/* Text + icons inside the sidebar → light. */
section[data-testid="stSidebar"],
section[data-testid="stSidebar"] * {{
    color: {PALETTE["sidebar_text"]} !important;
}}
/* Filter labels / captions / help icons — softer muted. */
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
section[data-testid="stSidebar"] small {{
    color: {PALETTE["sidebar_muted"]} !important;
}}
/* Links (page links, admin link) — lighter indigo so they're visible
   against the dark slate. */
section[data-testid="stSidebar"] a,
section[data-testid="stSidebar"] a span {{
    color: {PALETTE["primary_soft"]} !important;
}}
/* Form inputs INSIDE the sidebar need to stay readable — keep their
   own light-gray surface so user input is visible, not the dark slate. */
section[data-testid="stSidebar"] input,
section[data-testid="stSidebar"] textarea,
section[data-testid="stSidebar"] [data-baseweb="select"] > div,
section[data-testid="stSidebar"] [data-baseweb="tag"] {{
    background-color: #334155 !important;
    color: #f1f5f9 !important;
    border-color: #475569 !important;
}}
section[data-testid="stSidebar"] [data-baseweb="select"] svg {{
    fill: #cbd5e1 !important;
}}

/* ---- primary buttons — indigo, matches theme primary ---- */
button[kind="primary"] {{
    background-color: {PALETTE["primary"]} !important;
    border-color: {PALETTE["primary"]} !important;
    color: white !important;
}}
button[kind="primary"]:hover {{
    filter: brightness(0.92);
}}

/* ---- tab strip — subtle underline, consistent across pages ---- */
div[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {{
    color: {PALETTE["primary"]} !important;
    border-bottom-color: {PALETTE["primary"]} !important;
}}
</style>
"""


def apply_shared_theme() -> None:
    """Inject the shared CSS snippet. Call at the top of every page's
    main(), after `st.set_page_config` but before rendering widgets.
    Safe to call multiple times per session — Streamlit dedupes the
    injected style block.
    """
    st.markdown(_SHARED_CSS, unsafe_allow_html=True)
