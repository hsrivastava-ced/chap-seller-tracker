"""
ui_theme.py — shared visual constants + CSS applied to every Streamlit
page in this repo.

The CSS here enforces a single CedCommerce-/Threecolts-aligned look
across Dashboard, Admin, and Intelligence:

  * Sidebar — deep navy, every section in a uniform card with a 12 px
    radius, subtle border, consistent 14 px padding, and a softly
    tinted hover for nav rows.
  * Streamlit's auto-rendered multi-page nav links at the top of the
    sidebar get the same card treatment as our own brand / user /
    filter cards, so the column reads as one cohesive panel instead
    of "stock Streamlit chrome + custom widgets glued underneath".
  * Typography — fixed scale (H1 = 1.875 rem, H2 = 1.375 rem, H3 =
    1.125 rem, body = 0.95 rem, caption = 0.78 rem) so headings
    don't drift from page to page.
  * Card classes (`.tc-card`, `.tc-card--brand`, `.tc-card--muted`)
    are exposed for callers that prefer semantic markup over inline
    styles.

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


# Palette — primary indigo + accent violet match Threecolts' brand;
# deep-navy sidebar + warm-neutral page background match CedCommerce's
# admin tools. Single source of truth — dashboard.py mirrors this.
PALETTE: dict[str, str] = {
    # Brand
    "primary":         "#6366f1",  # indigo — primary actions, active state
    "primary_soft":    "#a5b4fc",  # indigo-300 — sidebar links
    "primary_deep":    "#4f46e5",  # indigo-600 — gradients
    "accent":          "#8b5cf6",  # violet-500 — accents
    "accent_deep":     "#7c3aed",  # violet-600 — gradient end
    # Status
    "success":         "#10b981",
    "success_soft":    "#6ee7b7",
    "danger":          "#ef4444",
    "danger_soft":     "#fca5a5",
    "warning":         "#f59e0b",
    "warning_soft":    "#fcd34d",
    "neutral":         "#94a3b8",
    # Surfaces — page (light) vs sidebar (dark navy).
    # `bg` is the DEFAULT; users can pick from BG_THEMES via the
    # sidebar "Theme" selector and the choice is persisted in
    # st.session_state.
    "bg":              "#eef2f7",  # soft cool slate — easier on eyes than pure white
    "card":            "#ffffff",
    "text":            "#0f172a",
    "text_soft":       "#64748b",
    "border":          "#e2e8f0",
    "sidebar_bg":      "#0f1729",  # deeper than slate-900 — closer to CedCommerce navy
    "sidebar_card":    "#1e293b",  # one tier lighter for cards
    "sidebar_card_hi": "#334155",  # hover/active
    "sidebar_border":  "#1e293b",
    "sidebar_text":    "#e2e8f0",
    "sidebar_muted":   "#94a3b8",
    "sidebar_dim":     "#64748b",
}


_SHARED_CSS = f"""
<style>
/* =========================================================
   1. Page chrome — backgrounds + base typography
   ========================================================= */
.main, .stApp {{
    background-color: {PALETTE["bg"]};
}}
.block-container {{
    padding-top: 2.5rem;
    padding-bottom: 3rem;
    max-width: 1400px;
}}
header[data-testid="stHeader"] {{
    background: transparent;
}}

/* Heading scale — fixed across every page so H1 is always 1.875 rem
   etc. Streamlit emits h1..h3 from `st.title`/`st.header`/`st.subheader`,
   so locking these is enough. */
h1, .stMarkdown h1 {{
    font-size: 1.875rem !important;
    line-height: 1.2 !important;
    font-weight: 700 !important;
    color: {PALETTE["text"]} !important;
    letter-spacing: -0.01em !important;
    margin-bottom: 0.25rem !important;
}}
h2, .stMarkdown h2 {{
    font-size: 1.375rem !important;
    line-height: 1.3 !important;
    font-weight: 600 !important;
    color: {PALETTE["text"]} !important;
    margin-top: 1.5rem !important;
    margin-bottom: 0.5rem !important;
}}
h3, .stMarkdown h3 {{
    font-size: 1.125rem !important;
    line-height: 1.35 !important;
    font-weight: 600 !important;
    color: {PALETTE["text"]} !important;
    margin-top: 1.25rem !important;
    margin-bottom: 0.4rem !important;
}}
h4, .stMarkdown h4 {{
    font-size: 0.95rem !important;
    line-height: 1.4 !important;
    font-weight: 600 !important;
    color: {PALETTE["text"]} !important;
    margin-top: 1rem !important;
    margin-bottom: 0.3rem !important;
}}
.stMarkdown p, .stMarkdown li {{
    font-size: 0.95rem;
    line-height: 1.55;
    color: {PALETTE["text"]};
}}
.stMarkdown small,
[data-testid="stCaptionContainer"] {{
    font-size: 0.78rem !important;
    color: {PALETTE["text_soft"]} !important;
}}

/* =========================================================
   2. Sidebar — deep navy, uniform cards, consistent rhythm
   ========================================================= */
section[data-testid="stSidebar"],
section[data-testid="stSidebar"] > div,
div[data-testid="stSidebarContent"],
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {{
    background-color: {PALETTE["sidebar_bg"]} !important;
}}
section[data-testid="stSidebar"][aria-expanded="true"] {{
    min-width: 280px !important;
}}
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {{
    padding: 1rem 0.85rem !important;
}}
/* Default text + icons in the sidebar — light slate. */
section[data-testid="stSidebar"],
section[data-testid="stSidebar"] * {{
    color: {PALETTE["sidebar_text"]} !important;
}}
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
section[data-testid="stSidebar"] small {{
    color: {PALETTE["sidebar_muted"]} !important;
}}

/* --- Streamlit's auto-rendered multi-page nav (Dashboard / Admin /
   Intelligence at the very top of the sidebar). Style every link as
   a card-row so the column reads as one cohesive panel. --- */
section[data-testid="stSidebar"] [data-testid="stSidebarNav"] {{
    background: transparent !important;
    padding: 0 !important;
    margin-bottom: 0.5rem !important;
}}
section[data-testid="stSidebar"] [data-testid="stSidebarNav"] ul {{
    padding: 0 !important;
    margin: 0 !important;
}}
section[data-testid="stSidebar"] [data-testid="stSidebarNav"] a {{
    display: block !important;
    padding: 9px 14px !important;
    margin: 2px 0 !important;
    border-radius: 8px !important;
    background: {PALETTE["sidebar_card"]} !important;
    border: 1px solid {PALETTE["sidebar_border"]} !important;
    color: {PALETTE["sidebar_text"]} !important;
    font-size: 0.92rem !important;
    font-weight: 500 !important;
    text-decoration: none !important;
    transition: background-color 120ms ease, border-color 120ms ease;
}}
section[data-testid="stSidebar"] [data-testid="stSidebarNav"] a span {{
    color: {PALETTE["sidebar_text"]} !important;
    font-size: 0.92rem !important;
    font-weight: 500 !important;
    text-transform: capitalize !important;
}}
section[data-testid="stSidebar"] [data-testid="stSidebarNav"] a:hover {{
    background: {PALETTE["sidebar_card_hi"]} !important;
    border-color: {PALETTE["primary"]} !important;
}}
/* Active page link — thicker accent border + brand-tinted background. */
section[data-testid="stSidebar"] [data-testid="stSidebarNav"] a[aria-current="page"],
section[data-testid="stSidebar"] [data-testid="stSidebarNav"] li[aria-current="page"] a {{
    background: linear-gradient(90deg, rgba(99,102,241,0.20) 0%, rgba(139,92,246,0.10) 100%) !important;
    border-color: {PALETTE["primary"]} !important;
    color: white !important;
}}

/* --- The "Signed in as" + Sign out auth widgets (auth.sign_out_button)
   were rendering as bare captions + a button against the dark panel.
   Wrap them in an implicit card via the parent block container styling. --- */
section[data-testid="stSidebar"] [data-testid="stCaptionContainer"]:first-of-type,
section[data-testid="stSidebar"] [data-testid="element-container"]:has(> div > [data-testid="stCaptionContainer"]) {{
    margin-bottom: 0.25rem !important;
}}

/* --- Inputs / selects inside the sidebar — keep readable on dark. --- */
section[data-testid="stSidebar"] input,
section[data-testid="stSidebar"] textarea,
section[data-testid="stSidebar"] [data-baseweb="select"] > div,
section[data-testid="stSidebar"] [data-baseweb="tag"] {{
    background-color: {PALETTE["sidebar_card_hi"]} !important;
    color: #f1f5f9 !important;
    border-color: #475569 !important;
}}
section[data-testid="stSidebar"] [data-baseweb="select"] svg {{
    fill: #cbd5e1 !important;
}}
/* Multiselect tag pills — first character was being clipped on every
   tag (TEMU US → EMU US). Root cause: BaseWeb's inner element has
   `direction: ltr; box-sizing: content-box; transform: translate3d(...)`
   that makes any inline-level override of padding leak the first
   glyph out of the container's visible area. Fix: force `min-width:
   fit-content` + ensure the parent multi-line input doesn't clip. */
[data-baseweb="tag"] {{
    padding: 4px 12px !important;
    font-size: 0.85rem !important;
    line-height: 1.2 !important;
    overflow: visible !important;
    min-width: fit-content !important;
    margin: 2px !important;
}}
[data-baseweb="tag"] > * {{
    overflow: visible !important;
    text-overflow: clip !important;
    min-width: 0 !important;
    white-space: nowrap !important;
}}
[data-baseweb="tag"] [title],
[data-baseweb="tag"] span {{
    padding: 0 4px !important;
    font-size: 0.85rem !important;
    overflow: visible !important;
    text-overflow: clip !important;
    white-space: nowrap !important;
}}
/* The select/multiselect host control itself — let the tags expand
   inside it without the host's own clipping clipping the first
   character of the first tag. */
[data-baseweb="select"] [data-baseweb="tag"]:first-child {{
    margin-left: 4px !important;
}}

/* --- Sidebar buttons. Primary stays indigo. Secondary becomes a
   transparent slate with a light border so labels stay visible. --- */
section[data-testid="stSidebar"] button[kind="secondary"],
section[data-testid="stSidebar"] button[data-testid="baseButton-secondary"] {{
    background-color: transparent !important;
    color: {PALETTE["sidebar_text"]} !important;
    border: 1px solid #475569 !important;
    border-radius: 8px !important;
    font-weight: 500 !important;
}}
section[data-testid="stSidebar"] button[kind="secondary"]:hover,
section[data-testid="stSidebar"] button[data-testid="baseButton-secondary"]:hover {{
    background-color: {PALETTE["sidebar_card_hi"]} !important;
    border-color: #64748b !important;
}}

/* --- Inline <code> + code blocks. Default Streamlit renders them on
   a tinted light surface that's unreadable on dark. Force a deep
   slate with light mono so the role chip etc. stay legible. --- */
section[data-testid="stSidebar"] code,
section[data-testid="stSidebar"] pre {{
    background-color: #0f172a !important;
    color: #e2e8f0 !important;
    border: 1px solid #334155 !important;
    border-radius: 6px !important;
    padding: 1px 6px !important;
    font-size: 0.78rem !important;
}}

/* --- Sidebar typography rhythm. --- */
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] span,
section[data-testid="stSidebar"] div {{
    font-size: 0.88rem;
    line-height: 1.45;
}}
section[data-testid="stSidebar"] [data-testid="stSidebarContent"] h1,
section[data-testid="stSidebar"] [data-testid="stSidebarContent"] h2,
section[data-testid="stSidebar"] [data-testid="stSidebarContent"] h3 {{
    font-size: 0.74rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.10em !important;
    text-transform: uppercase !important;
    color: {PALETTE["sidebar_dim"]} !important;
    margin-top: 1rem !important;
    margin-bottom: 0.4rem !important;
}}

/* =========================================================
   3. Cards — `.tc-card` family for callers that prefer the
      class over inline style attributes
   ========================================================= */
.tc-card {{
    padding: 14px 16px;
    background: {PALETTE["card"]};
    border: 1px solid {PALETTE["border"]};
    border-radius: 12px;
    margin-bottom: 12px;
}}
.tc-card--brand {{
    background: linear-gradient(135deg, {PALETTE["primary_deep"]} 0%, {PALETTE["accent_deep"]} 100%);
    color: #f8fafc;
    border: none;
}}
.tc-card--muted {{
    background: #0f172a;
    border: 1px solid #334155;
    color: {PALETTE["sidebar_text"]};
}}
.tc-eyebrow {{
    font-size: 0.66rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: {PALETTE["text_soft"]};
    font-weight: 700;
}}
.tc-card--brand .tc-eyebrow,
.tc-card--muted .tc-eyebrow {{
    color: rgba(255, 255, 255, 0.8);
}}

/* =========================================================
   4. Buttons + tabs — primary indigo, tab underline matches
   ========================================================= */
button[kind="primary"] {{
    background-color: {PALETTE["primary"]} !important;
    border-color: {PALETTE["primary"]} !important;
    color: white !important;
    border-radius: 8px !important;
    font-weight: 500 !important;
}}
button[kind="primary"]:hover {{
    filter: brightness(0.92);
}}
div[data-testid="stTabs"] button[role="tab"] {{
    font-size: 0.92rem !important;
    font-weight: 500 !important;
}}
div[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {{
    color: {PALETTE["primary"]} !important;
    border-bottom-color: {PALETTE["primary"]} !important;
    font-weight: 600 !important;
}}

/* =========================================================
   5. Dataframes — softer grid, brand-tinted header
   ========================================================= */
[data-testid="stDataFrame"] {{
    border: 1px solid {PALETTE["border"]};
    border-radius: 10px;
    overflow: hidden;
}}
</style>
"""


BG_THEMES: dict[str, dict[str, str]] = {
    # Three handpicked page-background palettes. `bg` is the page
    # surface, `border` is the per-card outline so light-on-light
    # gradients still register. Sidebar stays navy across all three.
    "Slate":  {"bg": "#eef2f7", "border": "#e2e8f0", "label": "Slate (cool · default)"},
    "Warm":   {"bg": "#faf6f1", "border": "#ead8c6", "label": "Warm (cream · easy on eyes)"},
    "Mist":   {"bg": "#eaf3fb", "border": "#cfe2f3", "label": "Mist (sky · airy)"},
}


def apply_shared_theme() -> None:
    """Inject the shared CSS snippet. Call at the top of every page's
    main(), after `st.set_page_config` but before rendering widgets.
    Safe to call multiple times per session — Streamlit dedupes the
    injected style block.
    """
    # Pull the user's BG theme choice from session_state (set via
    # render_theme_picker in the sidebar). Falls back to Slate, the
    # default soft cool surface.
    theme_key = st.session_state.get("_bg_theme", "Slate")
    theme = BG_THEMES.get(theme_key, BG_THEMES["Slate"])
    PALETTE["bg"] = theme["bg"]
    PALETTE["border"] = theme["border"]
    css = _SHARED_CSS.replace("{{BG_OVERRIDE}}", theme["bg"])
    st.markdown(css, unsafe_allow_html=True)
    # Inline override to force the override even if the page also
    # injects its own _inject_css later (e.g. dashboard.py).
    st.markdown(
        f'<style>.main, .stApp {{ background-color: {theme["bg"]} !important; }}</style>',
        unsafe_allow_html=True,
    )


def render_theme_picker() -> None:
    """Render a 3-option BG theme selector in the sidebar. Choice
    persists in session_state and the page reruns to apply it. Call
    from any page's sidebar render — Intelligence, Dashboard, Admin.
    """
    current = st.session_state.get("_bg_theme", "Slate")
    options = list(BG_THEMES.keys())
    pick = st.sidebar.selectbox(
        "Background theme",
        options=options,
        index=options.index(current) if current in options else 0,
        format_func=lambda k: BG_THEMES[k]["label"],
        key="_bg_theme",
        help="Switch the page background — sidebar stays the same. Choice persists for this session.",
    )
    return pick
