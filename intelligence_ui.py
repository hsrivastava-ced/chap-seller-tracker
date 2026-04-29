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

import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

import auth
import roles
import customer_intelligence as ci
import seller_delta
import seller_delta_source
import seller_profile_enricher as spe
from analytics_advanced import _is_test_store
from analytics_advanced import (
    DISPLAY_NAMES,
    display_name,
    exclude_test_stores,
)
from normalize import normalize_run_data
from supabase_client import SupabaseClient
from ui_errors import wrap_page
from ui_theme import apply_shared_theme, render_theme_picker


ROOT = Path(__file__).parent
LATEST_RUN_FILE = ROOT / "results" / "latest" / "run.json"
HISTORY_DIR = ROOT / "results" / "history"


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
    """Build a rep-facing DataFrame. Prefixes two derived columns so
    fit score + priority are the first things the eye lands on, then
    the identity + usage fields."""
    records: list[dict] = []
    for r in rows:
        ins = r.get("_insight", {})
        tier = ins.get("temperature", "Low")
        rec = {
            "Priority": f"{ci.temperature_emoji(tier)} {tier}",
            "Fit": f"{ins.get('fit_score', 0)}",
        }
        for c in _PRIMARY_COLS:
            rec[c] = r.get(c, "")
        rec["days_installed"] = ins.get("days_since_install") or "—"
        if "failure_ratio" in ins:
            rec["failure_ratio"] = f"{ins['failure_ratio'] * 100:.1f}%"
        records.append(rec)
    return pd.DataFrame.from_records(records)


def _tier_counter_card(label: str, count: int, color: str, emoji: str) -> str:
    """Compact counter card — mirrors the Hot/Warm/Cool/Low strip from
    the reference dashboard. Uses the dashboard palette values so the
    two pages look like one product."""
    return (
        f'<div style="padding:14px 18px; background:#1e293b; '
        f'border-radius:10px; border:1px solid #334155;">'
        f'<div style="color:#94a3b8; font-size:0.78rem; font-weight:600; '
        f'letter-spacing:0.06em; text-transform:uppercase;">'
        f'{emoji} {label}</div>'
        f'<div style="color:{color}; font-size:1.9rem; font-weight:700; '
        f'line-height:1.1; margin-top:6px; font-variant-numeric:tabular-nums;">'
        f'{count:,}</div>'
        f'</div>'
    )


def _download_csv(
    df: pd.DataFrame, filename: str, *, principal=None,
) -> None:
    """Render a CSV download button — but only for editor+ roles.

    Viewers see the data on screen but can't pull it down. Rationale:
    download = mass-email target list, and the same raw contact info
    rows are what reps are looking at. Gate keeps the data inside the
    app rather than scattered across desktops.
    """
    if df.empty:
        return
    if principal is not None and not roles.can(principal, "export_csv"):
        st.caption(
            "📎 CSV export is disabled for viewer accounts. Ask a super "
            "admin to grant you the **editor** role in Admin → Users "
            "if you need to download."
        )
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


def _render_sidebar(available_apps: list[str], *, principal=None) -> str:
    """Sidebar designed to match a real CRM's info-density — uniform
    cards for brand / nav / user / filters so the visual rhythm stays
    consistent down the column. Brand colors mirror Threecolts
    (indigo) + CedCommerce (violet accent)."""
    with st.sidebar:
        # ---- BRAND CARD --------------------------------------------
        st.markdown(
            '<div style="padding:14px 16px; border-radius:10px; '
            'background:linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%); '
            'color:#f8fafc; margin-bottom:12px;">'
            '<div style="font-size:0.66rem; letter-spacing:0.12em; '
            'text-transform:uppercase; opacity:0.85;">CedCommerce · Threecolts</div>'
            '<div style="font-size:1.15rem; font-weight:700; '
            'margin-top:4px; line-height:1.15;">cHAP Customer Intelligence</div>'
            '<div style="font-size:0.72rem; opacity:0.8; margin-top:2px;">'
            'For Business Development reps</div>'
            '</div>',
            unsafe_allow_html=True,
        )

        # ---- USER CARD ---------------------------------------------
        if principal is not None:
            role_color = {
                "super_admin": "#f59e0b",
                "editor": "#10b981",
                "viewer": "#64748b",
            }.get(principal.role, "#94a3b8")
            st.markdown(
                f'<div style="padding:12px 14px; border-radius:10px; '
                f'background:#0f172a; border:1px solid #334155; '
                f'margin-bottom:12px;">'
                f'<div style="display:flex; align-items:center; gap:10px;">'
                f'<div style="width:34px; height:34px; border-radius:50%; '
                f'background:linear-gradient(135deg, #6366f1, #a855f7); '
                f'color:white; font-weight:700; font-size:0.95rem; '
                f'display:flex; align-items:center; justify-content:center;">'
                f'{principal.email[0].upper()}</div>'
                f'<div style="min-width:0; flex:1;">'
                f'<div style="color:#e2e8f0; font-size:0.82rem; '
                f'font-weight:600; white-space:nowrap; overflow:hidden; '
                f'text-overflow:ellipsis;">{principal.email}</div>'
                f'<div style="color:{role_color}; font-size:0.68rem; '
                f'font-weight:700; text-transform:uppercase; letter-spacing:0.06em;'
                f'">{principal.role}</div>'
                f'</div></div></div>',
                unsafe_allow_html=True,
            )

        # ---- FILTERS CARD ------------------------------------------
        st.markdown(
            '<div style="padding:14px; border-radius:10px; '
            'background:#1e293b; border:1px solid #334155; '
            'margin-bottom:12px;">'
            '<div style="color:#94a3b8; font-size:0.68rem; '
            'font-weight:700; letter-spacing:0.1em; '
            'text-transform:uppercase; margin-bottom:10px;">🎯 Filters</div>',
            unsafe_allow_html=True,
        )

        if not available_apps:
            st.info("No scraped data yet. Run a scrape from Admin → Overview.")
            st.markdown("</div>", unsafe_allow_html=True)
            return ""

        # Default app preference: SHEIN. The user explicitly asked for
        # SHEIN to be the default on every page reload, so reps land on
        # the busiest panel without an extra click. Falls back to
        # whatever's first if SHEIN isn't in this run's data (e.g.
        # SHEIN scrape failed). Persisted across reruns via session_state.
        DEFAULT_APP = "shein"
        if DEFAULT_APP in available_apps:
            default_idx = available_apps.index(DEFAULT_APP)
        else:
            default_idx = 0
        # Single-select: each bucket table is app-scoped.
        pick_idx = st.selectbox(
            "App",
            options=list(range(len(available_apps))),
            index=default_idx,
            format_func=lambda i: display_name(available_apps[i]),
            help="Pick the app whose sellers you want to work today. Defaults to SHEIN.",
        )
        st.markdown("</div>", unsafe_allow_html=True)

        # ---- THEME PICKER (3 background palettes) ------------------
        render_theme_picker()

        # ---- FOOTER CARD -------------------------------------------
        st.markdown(
            '<div style="padding:10px 14px; border-radius:10px; '
            'background:#0f172a; border:1px solid #334155; '
            'margin-top:10px;">'
            '<div style="color:#64748b; font-size:0.66rem; '
            'font-weight:700; letter-spacing:0.1em; '
            'text-transform:uppercase;">Data source</div>'
            '<div style="color:#cbd5e1; font-size:0.8rem; margin-top:4px;">'
            'cHAP admin panel · Supabase cache</div>'
            '<div style="color:#94a3b8; font-size:0.7rem; margin-top:2px;">'
            'Auto-syncs 00:00 + 12:00 IST</div>'
            '</div>',
            unsafe_allow_html=True,
        )

        # ---- SIGN OUT (sidebar footer) ------------------------------
        # Stays at the very bottom so it reads as a logout footer
        # rather than competing with the brand/user/filter cards above.
        st.markdown('<div style="margin-top:14px;"></div>', unsafe_allow_html=True)
        auth.sign_out_button(st, skip_caption=True)

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
    app_key = _render_sidebar(available_apps, principal=principal)
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

    # ---- Temperature counter strip (🔥 Hot / ☀ Warm / ❄ Cool / 💤 Low).
    # Same shape as the reference dashboard the user shared — instant
    # read on pipeline composition before scrolling into bucket tables.
    tiers = ci.tier_counts(sellers, today=today)
    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(
        _tier_counter_card("Hot", tiers["Hot"], "#ef4444", "🔥"),
        unsafe_allow_html=True,
    )
    c2.markdown(
        _tier_counter_card("Warm", tiers["Warm"], "#f59e0b", "☀"),
        unsafe_allow_html=True,
    )
    c3.markdown(
        _tier_counter_card("Cool", tiers["Cool"], "#3b82f6", "❄"),
        unsafe_allow_html=True,
    )
    c4.markdown(
        _tier_counter_card("Low", tiers["Low"], "#94a3b8", "💤"),
        unsafe_allow_html=True,
    )
    st.write("")  # spacing before the buckets

    buckets = ci.buckets_for(sellers, today=today)

    if not buckets:
        st.error(
            "Couldn't compute insight buckets for this app. Technical "
            "details are in the Streamlit Cloud logs — send the admin "
            "a screenshot if this persists."
        )
        return

    # Summary strip: bucket-level counts. Temperature tiers above
    # already show the big picture — this zooms in on actionable
    # groupings for reps who want to jump straight to the relevant tab.
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

    # ---- Day-over-day delta feed ------------------------------------
    _render_delta_feed(app_key=app_key)

    # ---- Last 7 days movement ---------------------------------------
    # Wider window than the delta feed (which only compares last 2
    # snapshots): rolls up every install/uninstall whose date falls in
    # the past 7 days. Joins each uninstall back to the active-seller
    # snapshot (by email or store) so we can show plan + lifetime
    # orders + product count for each uninstall — the BD reps' priority
    # signal for win-back outreach.
    _render_seven_day_movement(
        app_key=app_key,
        sellers=sellers,
        uninstalls=run.get("uninstalls", {}).get(app_key, []) or [],
        today=today,
    )

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
                principal=principal,
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
    _render_advanced_preview(principal=principal, app_key=app_key)
    st.caption(
        "Coming next: day-over-day delta (what changed since the last "
        "scrape — new installs, plan changes, failed-order spikes), "
        "pulled directly from Supabase snapshots so nothing is lost "
        "when the admin panel drops a seller."
    )


# ---------------------------------------------------------------------
# Bulk enrichment — batch every seller for the selected apps so the
# cache fills up in one go. After that, scrapes only pay Claude cost
# for NEW seller_ids.
# ---------------------------------------------------------------------


def _render_bulk_enrich_section(
    *,
    principal,
    sellers_by_app_all: dict[str, list[dict]],
) -> None:
    if not roles.can(principal, "approve_schema_drift"):
        # Writes live data to Supabase + spends Claude tokens — gate to
        # super-admin only.
        return

    with st.expander("🧠 Bulk AI enrichment (super-admin only)", expanded=False):
        st.markdown(
            "Run the AI business-analysis **once over every currently-"
            "scraped seller** for the selected apps so the `seller_"
            "profiles` table fills up in one pass. After this, only "
            "net-new sellers hit Claude on subsequent scrapes — keeps "
            "the token bill flat."
        )

        # Multiselect of apps — user said "both SHEIN apps + both TEMU
        # apps", so let them pick any combination.
        available = sorted(
            [a for a, rows in sellers_by_app_all.items() if rows]
        )
        if not available:
            st.info("No scraped data to enrich yet.")
            return
        picked = st.multiselect(
            "Apps to enrich",
            options=available,
            default=available,
            format_func=display_name,
            help="Every seller row under these apps will be analysed "
                 "(or skipped if already cached within the last 30 days).",
        )

        skip_cached = st.checkbox(
            "Skip sellers already in the cache",
            value=True,
            help="When on, only NEW sellers (or ones whose cached row "
                 "is older than 30 days) hit Claude. This is the "
                 "incremental mode — safe to re-run daily.",
        )

        # Estimate-ahead: show how many Claude calls this would make.
        total_rows = sum(
            len(sellers_by_app_all.get(a, []) or []) for a in picked
        )
        st.caption(
            f"Scope: **{total_rows:,}** seller rows across "
            f"{len(picked)} app(s). With `skip_cached=True`, the actual "
            f"Claude calls will be (total − already-cached)."
        )

        if st.button("Run bulk enrichment", type="primary", key="bulk_enrich"):
            if not picked:
                st.warning("Pick at least one app.")
                return
            key = os.environ.get("ANTHROPIC_API_KEY") or (
                st.secrets.get("ANTHROPIC_API_KEY", "")
                if hasattr(st, "secrets") else ""
            )
            if not key:
                st.error(
                    "ANTHROPIC_API_KEY isn't set in Streamlit secrets. "
                    "The batch would run in dry-run mode and produce no "
                    "useful analysis. Add the key first, reboot, retry."
                )
                return
            # Streamlit secrets values don't propagate to os.environ
            # automatically — the enricher reads os.getenv, so copy.
            os.environ["ANTHROPIC_API_KEY"] = str(key)

            sb = SupabaseClient()
            if sb.dry_run:
                st.error(
                    "Supabase client is in dry-run mode (creds missing "
                    "or supabase-py not installed). Bulk enrichment "
                    "needs a live Supabase connection to cache results."
                )
                return

            # Narrow sellers_by_app to the picked subset.
            scope = {a: sellers_by_app_all.get(a, []) or [] for a in picked}

            progress = st.progress(0, text="Starting…")
            live_stats = st.empty()

            def _cb(done, total, profile):
                pct = int(done * 100 / max(total, 1))
                progress.progress(
                    pct,
                    text=f"{done:,} / {total:,} · last: "
                         f"{profile.store_url or profile.seller_id} "
                         f"({profile.source})",
                )
                live_stats.caption(
                    f"Latest · business_type={profile.business_type} · "
                    f"source={profile.source}"
                )

            with st.spinner("Enriching…"):
                stats = spe.bulk_enrich(
                    sellers_by_app=scope,
                    supabase_client=sb,
                    skip_cached=skip_cached,
                    progress_cb=_cb,
                )

            progress.progress(100, text="Done.")
            st.success(
                f"✅ Finished. Processed {stats['processed']:,} / "
                f"{stats['total']:,} — "
                f"🧠 {stats['claude_hits']:,} fresh AI calls · "
                f"💾 {stats['cache_hits']:,} cache hits · "
                f"⚠️ {stats['errors']:,} errors."
            )
            st.caption(
                "All results are cached in `public.seller_profiles`. "
                "The **🔍 Analyse business** buttons above now return "
                "instantly for every row (source = cache)."
            )


# ---------------------------------------------------------------------
# AI business analysis — per-lead drilldown.
#
# NOT RENDERED in the current UI — the live Anthropic-API flow is
# parked until we have API access. The functions stay in the repo so
# we can wire them back up (via _render_analyse_block) the day the
# key lands in Streamlit secrets. The Advanced Intelligence preview
# section below uses static sample data to show stakeholders what
# the final surface will look like.
# ---------------------------------------------------------------------


def _render_analyse_block(
    *, bucket_rows: list[dict], bucket_id: str, app_key: str,
) -> None:
    """Pick-a-shop + 🔍 Analyse business panel.

    Hits seller_profile_enricher, which:
      - returns from Supabase cache (public.seller_profiles) when
        recent enough,
      - else fetches the seller's storefront + asks Claude for
        business_type / categories / insight / opportunity,
      - else runs in dry-run mode (no ANTHROPIC_API_KEY set) so the
        UI still explains what the user should do.

    All network + Claude work is best-effort — errors surface inline,
    never block the rest of the page.
    """
    if not bucket_rows:
        return

    st.markdown("**🔍 Analyse a shop's business**")
    st.caption(
        "Pick a row above and we'll read the seller's public storefront, "
        "classify the business, and suggest an outreach angle. Results "
        "cache in Supabase for 30 days."
    )

    # Index shops by store_url so we can label them with the user's
    # visible identifier (store_url) but recover the full row on submit.
    options = [
        (r.get("store_url") or r.get("seller_id") or "—", r)
        for r in bucket_rows
    ]
    if not options:
        return
    pick_idx = st.selectbox(
        "Shop",
        options=list(range(len(options))),
        format_func=lambda i: options[i][0],
        key=f"analyse_pick_{bucket_id}",
        label_visibility="collapsed",
    )
    label, row = options[pick_idx]

    force = st.checkbox(
        "Re-fetch (ignore cached result)",
        value=False, key=f"analyse_force_{bucket_id}",
    )

    if st.button(
        "🔍 Analyse business",
        key=f"analyse_btn_{bucket_id}",
        type="primary",
    ):
        with st.spinner(f"Analysing {label}…"):
            try:
                sb = SupabaseClient()
                profile = spe.analyse_seller(
                    app_name=app_key,
                    seller_id=row.get("seller_id") or "",
                    store_url=row.get("store_url") or "",
                    supabase_client=sb,
                    force=force,
                )
            except Exception as err:
                st.error(f"Analysis failed: {err}")
                return
        _render_profile(profile)


def _render_profile(profile) -> None:
    """Render a SellerProfile as a card in the main panel."""
    if profile.source == "error":
        st.warning(
            f"Couldn't analyse: {profile.error or 'unknown reason'}",
            icon="⚠️",
        )
        return
    if profile.source == "dry_run":
        st.info(
            "**AI analysis isn't configured yet.** The storefront "
            "fetched cleanly, but the Claude call was skipped because "
            "`ANTHROPIC_API_KEY` isn't set in Streamlit secrets. Ask "
            "the admin to add the key, then click **Analyse business** "
            "again.",
            icon="🧪",
        )
        return

    # ---- Successful analysis ----
    cat_html = ""
    for c in profile.categories or []:
        cat_html += (
            f'<span style="display:inline-block; padding:2px 8px; '
            f'margin:0 6px 4px 0; border-radius:12px; '
            f'background:#334155; color:#e2e8f0; font-size:0.75rem;">'
            f'{c}</span>'
        )
    origin = {"claude": "AI (fresh)", "cache": "cached", "dry_run": "dry-run"}.get(
        profile.source, profile.source
    )
    # Build the "no categories" fallback OUTSIDE the f-string — PEP 701
    # (backslashes inside f-expressions) only lands in Python 3.12, and
    # Streamlit Cloud pins 3.11. Keeping this snippet as a regular
    # string means the same code runs on both.
    empty_cats = (
        '<span style="color:#64748b">No categories parsed</span>'
    )
    cats_block = cat_html or empty_cats
    insight_text = profile.insight or "—"
    opportunity_text = profile.opportunity or "—"
    st.markdown(
        f'<div style="padding:14px 18px; margin-top:8px; '
        f'background:#0f172a; border-radius:10px; border:1px solid #334155;">'
        f'<div style="color:#94a3b8; font-size:0.72rem; '
        f'text-transform:uppercase; letter-spacing:0.06em; '
        f'font-weight:600;">Business type · {origin}</div>'
        f'<div style="color:#f1f5f9; font-size:1.3rem; font-weight:700; '
        f'margin:4px 0 10px;">{profile.business_type}</div>'
        f'<div>{cats_block}</div>'
        f'<div style="margin-top:12px; color:#cbd5e1; font-size:0.92rem; '
        f'line-height:1.5;"><b>Insight:</b> {insight_text}</div>'
        f'<div style="margin-top:8px; color:#a5b4fc; font-size:0.92rem; '
        f'line-height:1.5;"><b>Opportunity:</b> {opportunity_text}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


# =====================================================================
# Advanced Intelligence — stakeholder preview (static sample data).
#
# Purpose: show decision-makers the *vision* for AI-assisted seller
# intelligence before we've paid to wire up a live model. Every number
# and every profile below is hand-crafted sample data — there is NO
# call to Anthropic here, no DB lookup, no scraped content. The
# section is gated to super_admin so regular viewers don't confuse
# this preview with real live data.
#
# When / how to flip to live:
#   1. Provision an ANTHROPIC_API_KEY (or alternative model provider).
#   2. Re-enable _render_analyse_block under each bucket tab.
#   3. Re-enable _render_bulk_enrich_section from main().
#   4. Swap _render_advanced_preview for a live version that reads
#      public.seller_profiles directly.
# =====================================================================


_DEMO_SUMMARY = {
    "shopify_temu": {
        "analysed": 84, "brands": 26, "resellers": 41,
        "manufacturers": 9, "boutiques": 8,
        "top_categories": [
            ("Home & Kitchen", 23), ("Apparel", 19),
            ("Toys & Games", 14), ("Electronics", 11), ("Beauty", 9),
        ],
        "high_priority_leads": 12,
    },
    "shein": {
        "analysed": 61, "brands": 34, "resellers": 18,
        "manufacturers": 3, "boutiques": 6,
        "top_categories": [
            ("Apparel", 38), ("Accessories", 14),
            ("Footwear", 11), ("Beauty", 7), ("Home", 3),
        ],
        "high_priority_leads": 9,
    },
    "shopify_temu_eu": {
        "analysed": 42, "brands": 14, "resellers": 21,
        "manufacturers": 4, "boutiques": 3,
        "top_categories": [
            ("Home & Kitchen", 13), ("Apparel", 11),
            ("Pet Supplies", 6), ("Electronics", 5), ("Garden", 4),
        ],
        "high_priority_leads": 6,
    },
    "shein_woocommerce": {
        "analysed": 20, "brands": 12, "resellers": 5,
        "manufacturers": 1, "boutiques": 2,
        "top_categories": [
            ("Apparel", 11), ("Beauty", 4),
            ("Accessories", 3), ("Home", 2),
        ],
        "high_priority_leads": 3,
    },
    "shopify_gearexchange": {
        "analysed": 7, "brands": 3, "resellers": 2,
        "manufacturers": 1, "boutiques": 1,
        "top_categories": [
            ("Musical Instruments", 5), ("Audio Gear", 2),
        ],
        "high_priority_leads": 2,
    },
}


_DEMO_PROFILES = {
    "shopify_temu": [
        {
            "store": "mojosmusic.com",
            "email": "tom@mojosmusic.com",
            "business_type": "Brand",
            "categories": ["Musical Instruments", "Audio Gear"],
            "insight": (
                "Family-run music retailer with ~470 SKUs — focused on "
                "guitars and audio equipment. Brand-owned Shopify; not "
                "dropshipping."
            ),
            "opportunity": (
                "Add TikTok Shop integration — instrument demos perform "
                "well there; their catalog is ready to cross-post."
            ),
            "confidence": 92,
        },
        {
            "store": "knickknacktoyshack.myshopify.com",
            "email": "steven@knickknacktoyshack.com",
            "business_type": "Reseller",
            "categories": ["Toys & Games", "Collectibles"],
            "insight": (
                "Shopify reseller sitting on ~900 SKUs with low monthly "
                "order volume — typical catalog-heavy early-stage shop."
            ),
            "opportunity": (
                "Offer Amazon Channel listing — their product diversity "
                "would benefit from marketplace reach before paying for "
                "ads on their own store."
            ),
            "confidence": 84,
        },
    ],
    "shein": [
        {
            "store": "trendation-shop.de",
            "email": "info@trendation-shop.de",
            "business_type": "Brand",
            "categories": ["Apparel", "Footwear"],
            "insight": (
                "German fashion brand with ~30k SKUs — heavy catalog, "
                "German-language storefront, serious logistics operation."
            ),
            "opportunity": (
                "Priority for SHEIN EU expansion — large catalog, "
                "EU-native brand, Basic plan caps their listing "
                "throughput. Upsell to Growth tier."
            ),
            "confidence": 95,
        },
        {
            "store": "sheindemo.myshopify.com",
            "email": "schauhan@threecolts.com",
            "business_type": "Unknown",
            "categories": [],
            "insight": (
                "Internal demo store — thin product catalog (15 SKUs), "
                "no orders flowing. Exclude from rep outreach."
            ),
            "opportunity": "—",
            "confidence": 98,
        },
    ],
    "shopify_gearexchange": [
        {
            "store": "diabloguitars.com",
            "email": "orders@diabloguitars.com",
            "business_type": "Brand",
            "categories": ["Musical Instruments", "Guitars"],
            "insight": (
                "Custom guitar builder with strong brand identity, "
                "~1,200 SKUs and 11 monthly orders on Silver plan — "
                "product-market fit visible, under-monetized."
            ),
            "opportunity": (
                "Upsell to Gold: their reorder rate + handcrafted "
                "margin justifies premium placement + feature exposure."
            ),
            "confidence": 89,
        },
        {
            "store": "egaguitars.com",
            "email": "jt.trevino@egaguitars.com",
            "business_type": "Brand",
            "categories": ["Musical Instruments", "Guitars"],
            "insight": (
                "Guitar brand with 350 orders on Bronze plan — highest "
                "throughput in the GearExchange cohort. 4 failed orders "
                "suggests fulfilment friction worth flagging."
            ),
            "opportunity": (
                "Dual play: upsell to Silver for higher throughput "
                "limits AND loop Support in on the failed-order thread."
            ),
            "confidence": 94,
        },
    ],
}


def _demo_counts(app_key: str) -> dict:
    return _DEMO_SUMMARY.get(app_key, {})


def _render_advanced_preview(*, principal, app_key: str) -> None:
    """Stakeholder-facing preview of the advanced-intelligence vision.

    Super-admin only. Renders a summary dashboard + two example seller
    profile cards using hand-crafted sample data. Every element is
    labelled `PREVIEW / SAMPLE DATA` so there's no confusion about
    what's real and what's shown for selling-the-idea purposes.
    """
    if not roles.can(principal, "approve_schema_drift"):
        return

    with st.expander(
        "🔮 Advanced Intelligence (Preview · Super-admin only)",
        expanded=False,
    ):
        st.markdown(
            '<div style="padding:10px 14px; background:#fef3c7; '
            'border-left:4px solid #f59e0b; border-radius:6px; '
            'margin-bottom:18px; color:#78350f; font-size:0.9rem;">'
            '<b>Preview · Sample data only.</b>  This panel shows the '
            'vision for AI-assisted seller intelligence. None of the '
            'numbers or profiles below are live — they are hand-crafted '
            'examples so stakeholders can see what the finished surface '
            'will look like before we commit to a model provider.'
            '</div>',
            unsafe_allow_html=True,
        )

        summary = _demo_counts(app_key)
        if summary:
            _render_preview_summary(app_key, summary)
        else:
            st.info(
                f"No preview data configured for {display_name(app_key)} "
                "— add it to `_DEMO_SUMMARY` in intelligence_ui.py to "
                "show a sample dashboard for this app."
            )

        profiles = _DEMO_PROFILES.get(app_key, [])
        if profiles:
            st.markdown("#### 🧾 Example seller profiles")
            st.caption(
                "This is what each lead's detail card will look like "
                "once live AI analysis is enabled. Business type, "
                "categories, positioning note + suggested pitch are "
                "generated from the seller's public storefront."
            )
            for p in profiles:
                _render_demo_profile_card(p)


def _render_preview_summary(app_key: str, s: dict) -> None:
    """Top summary strip — what stakeholders see first."""
    st.markdown(f"#### 📊 Coverage · {display_name(app_key)}")
    c1, c2, c3, c4, c5 = st.columns(5)

    def _mini(col, label, value, color):
        col.markdown(
            f'<div style="padding:12px 14px; background:#0f172a; '
            f'border-radius:10px; border:1px solid #334155;">'
            f'<div style="color:#94a3b8; font-size:0.68rem; '
            f'font-weight:600; letter-spacing:0.06em; '
            f'text-transform:uppercase;">{label}</div>'
            f'<div style="color:{color}; font-size:1.5rem; '
            f'font-weight:700; margin-top:4px;">{value}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    _mini(c1, "Analysed", f"{s['analysed']:,}", "#a5b4fc")
    _mini(c2, "Brands", f"{s['brands']:,}", "#10b981")
    _mini(c3, "Resellers", f"{s['resellers']:,}", "#3b82f6")
    _mini(c4, "Manufacturers", f"{s['manufacturers']:,}", "#f59e0b")
    _mini(c5, "Boutiques", f"{s['boutiques']:,}", "#ec4899")

    st.write("")

    cats = s.get("top_categories") or []
    if cats:
        st.markdown("**🏷 Top product categories** (seller count per category)")
        chips = "".join(
            f'<span style="display:inline-block; margin:0 8px 6px 0; '
            f'padding:6px 12px; border-radius:16px; background:#334155; '
            f'color:#e2e8f0; font-size:0.84rem;">'
            f'{name} <span style="color:#94a3b8;">· {count}</span>'
            f'</span>'
            for name, count in cats
        )
        st.markdown(f'<div>{chips}</div>', unsafe_allow_html=True)

    hp = s.get("high_priority_leads", 0)
    if hp:
        st.markdown(
            f'<div style="margin-top:16px; padding:12px 16px; '
            f'background:rgba(239,68,68,0.08); border-left:4px solid '
            f'#ef4444; border-radius:6px; color:#e2e8f0;">'
            f'🔥 <b>{hp}</b> high-priority leads flagged by AI — '
            f'brands/manufacturers on free or low-tier plans with '
            f'meaningful catalog + order signal. (Preview figure.)'
            f'</div>',
            unsafe_allow_html=True,
        )


def _render_demo_profile_card(p: dict) -> None:
    """Single seller profile card — dark card with AI-generated
    business_type, categories, insight, opportunity + confidence %."""
    cat_html = "".join(
        f'<span style="display:inline-block; padding:2px 10px; '
        f'margin:0 6px 4px 0; border-radius:12px; background:#334155; '
        f'color:#e2e8f0; font-size:0.78rem;">{c}</span>'
        for c in p.get("categories") or []
    )
    if not cat_html:
        cat_html = (
            '<span style="color:#64748b; font-size:0.85rem;">'
            '(no categories inferred)</span>'
        )
    confidence = p.get("confidence", 0)
    conf_color = (
        "#10b981" if confidence >= 85
        else "#f59e0b" if confidence >= 70
        else "#94a3b8"
    )
    st.markdown(
        f'<div style="padding:16px 20px; margin:10px 0; background:#0f172a; '
        f'border-radius:10px; border:1px solid #334155;">'
        f'<div style="display:flex; justify-content:space-between; '
        f'align-items:flex-start; gap:16px;">'
        f'<div>'
        f'<div style="color:#94a3b8; font-size:0.72rem; '
        f'text-transform:uppercase; letter-spacing:0.06em; '
        f'font-weight:600;">{p["store"]}  ·  {p["email"]}</div>'
        f'<div style="color:#f1f5f9; font-size:1.15rem; '
        f'font-weight:700; margin:3px 0 8px;">{p["business_type"]}</div>'
        f'<div>{cat_html}</div>'
        f'</div>'
        f'<div style="text-align:right;">'
        f'<div style="color:#64748b; font-size:0.68rem; '
        f'text-transform:uppercase;">Confidence</div>'
        f'<div style="color:{conf_color}; font-size:1.3rem; '
        f'font-weight:700;">{confidence}%</div>'
        f'</div>'
        f'</div>'
        f'<div style="margin-top:12px; color:#cbd5e1; font-size:0.92rem; '
        f'line-height:1.5;"><b>Insight:</b> {p["insight"]}</div>'
        f'<div style="margin-top:8px; color:#a5b4fc; font-size:0.92rem; '
        f'line-height:1.5;"><b>Opportunity:</b> {p["opportunity"]}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


# =====================================================================
# Day-over-day delta feed — what changed since the previous scrape.
# Powered by seller_delta + seller_delta_source; reads Supabase first
# (so churned sellers — who cHAP's own UI has forgotten — are still
# traceable), falls back to local results/history.
# =====================================================================


_DELTA_STYLE = {
    "new_install":        ("🟢", "#10b981", "New install"),
    "churned":            ("🔴", "#ef4444", "Churned"),
    "plan_upgrade":       ("⬆️", "#6366f1", "Upgraded to paid"),
    "plan_downgrade":     ("⬇️", "#f59e0b", "Dropped to free"),
    "plan_change":        ("🔄", "#8b5cf6", "Plan changed"),
    "order_spike":        ("📈", "#10b981", "Order spike"),
    "failed_order_spike": ("⚠️", "#ef4444", "Failures rising"),
}


def _parse_seller_date(raw: str | None) -> date | None:
    """Parse a date out of cHAP's two date conventions.

    Active sellers' `installed_on` is "DD/MM/YYYY". Uninstalls'
    `uninstalled_on` is "YYYY-MM-DD HH:MM:SS". Returns a date object
    or None if neither format matches.
    """
    if not raw:
        return None
    raw = str(raw).strip()
    # ISO-style first (uninstalls); take just the date portion.
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        pass
    # DD/MM/YYYY (active sellers).
    try:
        return datetime.strptime(raw[:10], "%d/%m/%Y").date()
    except ValueError:
        return None


def _render_seven_day_movement(
    *, app_key: str, sellers: list[dict], uninstalls: list[dict], today: date,
) -> None:
    """Show a 7-day window of installs + uninstalls for the selected app.

    Joins each uninstall back to the seller-list metadata (by email or
    normalised store URL) so each uninstall card shows what the seller
    was worth at install time — plan, lifetime orders, product count.
    The BD team uses this to prioritise win-back outreach: a 4,000-order
    Pro account that just left is the call of the day; a free-plan
    seller who never set up isn't.
    """
    from datetime import timedelta

    cutoff = today - timedelta(days=7)

    # ---- Bucket installs + uninstalls by date ---------------------------
    recent_installs = [
        s for s in sellers
        if (d := _parse_seller_date(s.get("installed_on"))) and d >= cutoff
    ]
    # Each seller appears once per platform in cHAP's uninstalls list
    # (Shopify uninstall + SHEIN uninstall = 2 rows, same seller_id).
    # Dedup by seller_id and keep the EARLIEST uninstalled_on per
    # seller — that's when they first started uninstalling. The shops_raw
    # field already preserves both per-platform timestamps for display.
    _recent_raw = [
        u for u in uninstalls
        if (d := _parse_seller_date(u.get("uninstalled_on"))) and d >= cutoff
    ]
    by_sid: dict[str, dict] = {}
    for u in _recent_raw:
        sid = u.get("seller_id") or ""
        if not sid:
            # No seller_id — fall back to email + store_url as a key.
            sid = f"{u.get('email','')}|{u.get('username','')}"
        existing = by_sid.get(sid)
        if existing is None:
            by_sid[sid] = u
            continue
        # Keep the earliest timestamp (first uninstall action).
        d_existing = _parse_seller_date(existing.get("uninstalled_on"))
        d_new = _parse_seller_date(u.get("uninstalled_on"))
        if d_new and (not d_existing or d_new < d_existing):
            by_sid[sid] = u
    recent_uninstalls = list(by_sid.values())

    # Build install-time stats index from the active sellers list — by
    # email + by normalised store URL — so each uninstall card can
    # surface plan / lifetime orders / product count.
    def _norm_url(s: str) -> str:
        s = (s or "").strip().lower()
        for prefix in ("https://", "http://", "www."):
            if s.startswith(prefix):
                s = s[len(prefix):]
        return s.rstrip("/")
    by_email: dict[str, dict] = {}
    by_store: dict[str, dict] = {}
    for s in sellers:
        em = (s.get("email") or "").strip().lower()
        st_url = _norm_url(s.get("store_url") or "")
        if em:
            by_email[em] = s
        if st_url:
            by_store[st_url] = s

    label = (
        f"📅 Last 7 days · {len(recent_installs)} new install"
        f"{'s' if len(recent_installs) != 1 else ''} · "
        f"{len(recent_uninstalls)} uninstall"
        f"{'s' if len(recent_uninstalls) != 1 else ''}"
    )
    with st.expander(label, expanded=len(recent_uninstalls) > 0 or len(recent_installs) > 0):
        st.caption(
            f"Window: {cutoff.strftime('%b %d')} – {today.strftime('%b %d, %Y')}. "
            "Uninstalls below are joined to install-time data so you can "
            "see plan + lifetime orders for each — that's how you "
            "prioritise win-back outreach."
        )

        if not recent_installs and not recent_uninstalls:
            st.info(
                "No install/uninstall activity captured in the last 7 days "
                f"for {display_name(app_key)}. Either the app is steady-state, "
                "or scrape coverage gaps mean the dates we have don't fall "
                "in this window."
            )
            return

        # ---- 7-day daily timeline (counts per day) ---------------------
        from datetime import timedelta as _td
        day_buckets: dict[str, dict[str, int]] = {}
        for offset in range(7, -1, -1):
            d = today - _td(days=offset)
            day_buckets[d.isoformat()] = {"installs": 0, "uninstalls": 0}
        for s in recent_installs:
            d = _parse_seller_date(s.get("installed_on"))
            if d and d.isoformat() in day_buckets:
                day_buckets[d.isoformat()]["installs"] += 1
        for u in recent_uninstalls:
            d = _parse_seller_date(u.get("uninstalled_on"))
            if d and d.isoformat() in day_buckets:
                day_buckets[d.isoformat()]["uninstalls"] += 1

        chart_df = pd.DataFrame(
            [{"date": k, "Installs": v["installs"], "Uninstalls": v["uninstalls"]}
             for k, v in day_buckets.items()]
        )
        chart_df["date"] = pd.to_datetime(chart_df["date"])
        st.bar_chart(chart_df.set_index("date"), height=220)

        # ---- Recent uninstalls table (joined with install-time stats) --
        if recent_uninstalls:
            st.markdown(
                f'<div style="margin:14px 0 6px 0;"><b style="color:#ef4444; '
                f'font-size:0.95rem;">📞 {len(recent_uninstalls)} uninstall'
                f'{"s" if len(recent_uninstalls) != 1 else ""} in the past '
                f'7 days · ranked by lifetime orders</b></div>'
                '<div style="color:#64748b; font-size:0.82rem; margin-bottom:8px;">'
                'Highest-orders sellers first — those are the win-back priority calls.'
                '</div>',
                unsafe_allow_html=True,
            )

            joined_rows = []
            for u in recent_uninstalls:
                em = (u.get("email") or "").strip().lower()
                store = _norm_url(u.get("username") or u.get("store_url") or "")
                install_row = by_email.get(em) or by_store.get(store) or {}
                try:
                    orders = int(install_row.get("order_count") or 0)
                except (ValueError, TypeError):
                    orders = 0
                try:
                    products = int(install_row.get("product_count") or 0)
                except (ValueError, TypeError):
                    products = 0
                plan = (install_row.get("plan") or "").strip()
                if plan.lower() in {"n/a", "—", "-", "none", ""}:
                    plan = "—"
                joined_rows.append({
                    "Uninstalled on": u.get("uninstalled_on") or "",
                    "Email": u.get("email") or "—",
                    "Store": u.get("username") or u.get("store_url") or "—",
                    "Plan at uninstall": plan,
                    "Lifetime orders": orders,
                    "Products": products,
                    "Platform": u.get("platform") or "—",
                })
            joined_rows.sort(key=lambda r: -r["Lifetime orders"])
            st.dataframe(
                pd.DataFrame(joined_rows),
                hide_index=True,
                use_container_width=True,
            )

        # ---- Recent installs table -------------------------------------
        if recent_installs:
            st.markdown(
                f'<div style="margin:14px 0 6px 0;"><b style="color:#10b981; '
                f'font-size:0.95rem;">🌱 {len(recent_installs)} new install'
                f'{"s" if len(recent_installs) != 1 else ""} in the past '
                f'7 days</b></div>'
                '<div style="color:#64748b; font-size:0.82rem; margin-bottom:8px;">'
                'Welcome these sellers, confirm setup, and start the relationship.'
                '</div>',
                unsafe_allow_html=True,
            )
            install_rows = []
            for s in recent_installs:
                plan = (s.get("plan") or "").strip()
                if plan.lower() in {"n/a", "—", "-", "none", ""}:
                    plan = "—"
                try:
                    orders = int(s.get("order_count") or 0)
                except (ValueError, TypeError):
                    orders = 0
                install_rows.append({
                    "Installed on": s.get("installed_on") or "",
                    "Email": s.get("email") or "—",
                    "Store": s.get("store_url") or "—",
                    "Plan": plan,
                    "Orders": orders,
                    "Source": s.get("source_country") or "—",
                })
            # Most recent installs first.
            install_rows.sort(key=lambda r: r["Installed on"], reverse=True)
            st.dataframe(
                pd.DataFrame(install_rows),
                hide_index=True,
                use_container_width=True,
            )


def _render_delta_feed(*, app_key: str) -> None:
    """Show a timeline of changes between the most recent scrape and
    the one before it — the "seller movement" view.

    Critical for BD reps: cHAP's own admin panel drops seller detail
    the moment they uninstall, but our Supabase snapshots still have
    their full profile. The Churned events here remember the orders /
    products / plan of sellers who've already vanished from cHAP.
    """
    sb = SupabaseClient()
    prior_stamp, latest_stamp, prior_rows, latest_rows = (
        seller_delta_source.from_supabase(sb, app_name=app_key)
    )

    # If Supabase has < 2 rows for this app, fall back to git history of
    # results/latest/run.json. The scraper-bot commits one snapshot per
    # successful run as `chore(data): scrape …`, so two consecutive
    # commits give us a 100% reliable diff source — this works on
    # Streamlit Cloud immediately without any external service.
    if not prior_rows or not latest_rows:
        p_stamp, l_stamp, p_rows, l_rows = (
            seller_delta_source.from_git_history(ROOT, app_name=app_key)
        )
        if p_rows and l_rows:
            prior_stamp, latest_stamp = p_stamp, l_stamp
            prior_rows, latest_rows = p_rows, l_rows

    # Final fallback: local results/history/<stamp>/run.json (only useful
    # on dev machines where the scraper has been run locally).
    if not prior_rows or not latest_rows:
        p_stamp, l_stamp, p_rows, l_rows = (
            seller_delta_source.from_local_history(
                HISTORY_DIR, app_name=app_key
            )
        )
        if p_rows and l_rows:
            prior_stamp, latest_stamp = p_stamp, l_stamp
            prior_rows, latest_rows = p_rows, l_rows

    # Strip internal/test stores from BOTH snapshots before computing
    # any rail. Without this the `🌱 New install` and `🔄 Reinstall`
    # rails surface internal QA accounts (syedubaidhussain11@gmail.com,
    # @threecolts.com aliases, etc.) that the user has explicitly asked
    # to be hidden. The seller-list page already filters via
    # `exclude_test_stores` upstream — same rule must apply here so the
    # delta feed stays consistent with the Hot/Warm/Cool counts.
    prior_rows = [s for s in (prior_rows or []) if not _is_test_store(s)]
    latest_rows = [s for s in (latest_rows or []) if not _is_test_store(s)]

    # Same multi-source chain for the uninstalls list — different key
    # in run.json. We need this list for two things:
    #  1. Surface "new uninstalls since prior scrape" — highest-value
    #     callback list for BD reps.
    #  2. Gate "churned" events to ONLY sellers that actually appear in
    #     uninstalls. A seller missing from the latest scrape but NOT
    #     in uninstalls is a scrape-coverage gap, not real churn —
    #     flagging them as churn would mislead the team (verified
    #     2026-04-26: SHEIN scrape=200 vs cHAP truth=347, all 48
    #     supposed churns were ghosts).
    new_uninstalls: list[dict] = []
    latest_uninst: list[dict] = []
    try:
        _, _, prior_uninst, latest_uninst = (
            seller_delta_source.from_git_history_uninstalls(
                ROOT, app_name=app_key,
            )
        )
        # Strip internal/test stores from the uninstalls list too — same
        # rule as the seller list above. Otherwise the new-uninstalls
        # rail surfaces threecolts test accounts the rep can't action.
        latest_uninst = [u for u in (latest_uninst or []) if not _is_test_store(u)]
        prior_uninst = [u for u in (prior_uninst or []) if not _is_test_store(u)]
        if latest_uninst:
            prior_ids = {u.get("seller_id") for u in (prior_uninst or [])}
            seen: set = set()
            for u in latest_uninst:
                sid = u.get("seller_id")
                if sid and sid not in prior_ids and sid not in seen:
                    seen.add(sid)
                    new_uninstalls.append(u)
    except Exception as err:
        logging.debug(f"new-uninstalls lookup failed for {app_key}: {err}")

    if not prior_rows or not latest_rows:
        with st.expander(
            "⚡ What changed (day-over-day) · no prior snapshot yet",
            expanded=False,
        ):
            st.caption(
                "Needs two scraped runs to compare. The next scheduled "
                "scrape (00:00 IST or 12:00 IST) will create the second "
                "snapshot, and this feed lights up automatically on the "
                "next page reload."
            )
        return

    events = seller_delta.compute_events(
        app_key, prior_rows, latest_rows,
        latest_uninstalls=latest_uninst,
    )
    counts = seller_delta.summarise(events)

    # ---- Three actionable rails for BD reps -----------------------------
    # All three are derived from the same prior↔latest seller lists +
    # uninstalls list, but separated visually so reps can scan each as
    # its own callback list:
    #   1. New installs   — sellers to onboard (welcome, drive setup)
    #   2. New uninstalls — sellers who just left (call, learn why)
    #   3. Reinstalls     — sellers who returned (re-engage, ensure stick)
    prior_seller_ids = {s.get("seller_id") for s in prior_rows}
    new_installs_list = [
        s for s in latest_rows
        if s.get("seller_id") and s.get("seller_id") not in prior_seller_ids
    ]

    # Reinstalls: present in the LATEST active seller list AND ALSO in
    # the latest uninstalls list. Same store / email appears on both
    # sides — they uninstalled at some point and have since installed
    # again. The user flagged swaggboutique.com as the canonical example.
    # Match on email (most reliable) + store-url fallback (collapses
    # www./protocol variants).
    def _norm_url(s: str) -> str:
        s = (s or "").strip().lower()
        for prefix in ("https://", "http://", "www."):
            if s.startswith(prefix):
                s = s[len(prefix):]
        return s.rstrip("/")
    uninst_emails = {(u.get("email") or "").strip().lower()
                     for u in (latest_uninst or []) if u.get("email")}
    uninst_stores = {_norm_url(u.get("username") or u.get("store_url") or "")
                     for u in (latest_uninst or [])}
    uninst_stores.discard("")
    reinstalls_list: list[dict] = []
    seen_re: set[str] = set()
    for s in latest_rows:
        sid = s.get("seller_id") or ""
        if sid in seen_re:
            continue
        email = (s.get("email") or "").strip().lower()
        store = _norm_url(s.get("store_url") or "")
        if (email and email in uninst_emails) or (store and store in uninst_stores):
            reinstalls_list.append(s)
            seen_re.add(sid)

    # Install-time stats lookup for the new-uninstall cards. cHAP's
    # uninstalls table drops plan/order_count/product_count, but our
    # active-sellers history (prior_rows) still has it for sellers
    # that were active in the prior scrape. Build a quick index by
    # email + normalised store-url so each new-uninstall card can show
    # what the seller was worth at install time — lifetime orders +
    # plan are the most useful signals for win-back outreach.
    install_index_by_email: dict[str, dict] = {}
    install_index_by_store: dict[str, dict] = {}
    for s in prior_rows:
        em = (s.get("email") or "").strip().lower()
        st_url = _norm_url(s.get("store_url") or "")
        if em:
            install_index_by_email[em] = s
        if st_url:
            install_index_by_store[st_url] = s

    # Coverage-gap detection runs silently — we still SUPPRESS the
    # ghost-churn events (handled inside seller_delta.compute_events
    # via the latest_uninstalls gate), but we don't surface a banner
    # to the audience. Failure mode of a scraper bug shouldn't be
    # broadcast on the BD-rep dashboard. Super admins see the same
    # thing reflected in the run report under Admin → Runs.
    prior_ids = {s.get("seller_id") for s in prior_rows}
    latest_ids = {s.get("seller_id") for s in latest_rows}
    uninst_ids = {u.get("seller_id") for u in (latest_uninst or [])}
    ghost_count = len(prior_ids - latest_ids - uninst_ids)
    coverage_gap_silent = ghost_count > 5 or (
        prior_rows and len(latest_rows) < 0.85 * len(prior_rows)
        and ghost_count > 0
    )
    if coverage_gap_silent:
        logging.warning(
            f"intelligence: scrape coverage gap detected for {app_key} — "
            f"{ghost_count} prior sellers missing from latest scrape "
            f"and absent from uninstalls. Ghost-churn suppressed."
        )

    # Headline strip — counts per kind.
    def _pill(kind: str) -> str:
        emoji, color, label = _DELTA_STYLE.get(kind, ("·", "#94a3b8", kind))
        n = counts.get(kind, 0)
        if not n:
            return ""
        return (
            f'<span style="display:inline-block; padding:4px 12px; '
            f'margin:0 8px 4px 0; border-radius:14px; '
            f'background:rgba(148,163,184,0.12); border:1px solid '
            f'rgba(148,163,184,0.25); color:{color}; font-size:0.82rem; '
            f'font-weight:600;">{emoji} {label} · {n}</span>'
        )
    strip = "".join(_pill(k) for k in _DELTA_STYLE.keys() if counts.get(k))
    if not strip:
        strip = (
            '<span style="color:#94a3b8; font-size:0.88rem;">'
            'No movement between the last two scrapes — every seller '
            'held steady.</span>'
        )

    with st.expander(
        f"⚡ What changed (day-over-day) · {len(events)} events",
        expanded=len(events) > 0,
    ):
        st.markdown(
            f'<div style="color:#94a3b8; font-size:0.8rem; '
            f'margin-bottom:10px;">Comparing <b>{latest_stamp or "latest"}</b> '
            f'vs <b>{prior_stamp or "prior"}</b></div>{strip}',
            unsafe_allow_html=True,
        )

        # New uninstalls — highest-priority callback list. A seller that
        # just left in the past 12-24h is the single most-actionable lead.
        if new_uninstalls:
            st.markdown(
                '<div style="margin:14px 0 8px 0; padding:10px 14px; '
                'background:rgba(239,68,68,0.08); border-left:4px solid '
                '#ef4444; border-radius:6px;">'
                f'<b style="color:#ef4444;">📞 {len(new_uninstalls)} new '
                f'uninstall{"s" if len(new_uninstalls) != 1 else ""} since '
                'the prior scrape</b><br>'
                '<span style="color:#94a3b8; font-size:0.85rem;">'
                'These sellers just uninstalled. Reach out today to learn '
                'why and what would bring them back.</span></div>',
                unsafe_allow_html=True,
            )
            for u in new_uninstalls[:10]:
                _render_new_uninstall_card(
                    u,
                    install_by_email=install_index_by_email,
                    install_by_store=install_index_by_store,
                )
            if len(new_uninstalls) > 10:
                st.caption(f"+ {len(new_uninstalls) - 10} more uninstalls — see the Uninstalls bucket below.")

        # New installs — fresh sellers who weren't in the prior scrape.
        # BD reps welcome them, confirm setup, and start the relationship.
        if new_installs_list:
            st.markdown(
                '<div style="margin:14px 0 8px 0; padding:10px 14px; '
                'background:rgba(16,185,129,0.08); border-left:4px solid '
                '#10b981; border-radius:6px;">'
                f'<b style="color:#10b981;">🌱 {len(new_installs_list)} new '
                f'install{"s" if len(new_installs_list) != 1 else ""} since '
                'the prior scrape</b><br>'
                '<span style="color:#94a3b8; font-size:0.85rem;">'
                'New sellers just installed. Welcome them, confirm their '
                'setup is complete, and start the relationship before they '
                'go cold.</span></div>',
                unsafe_allow_html=True,
            )
            for s in new_installs_list[:10]:
                _render_new_install_card(s)
            if len(new_installs_list) > 10:
                st.caption(f"+ {len(new_installs_list) - 10} more new installs.")

        # Reinstalls — sellers in the active list whose email/store also
        # shows up in the uninstalls history. They left and came back.
        # High-leverage outreach: they've used the product, churned, and
        # returned — talk to them about why they came back and what
        # would make them stay.
        if reinstalls_list:
            st.markdown(
                '<div style="margin:14px 0 8px 0; padding:10px 14px; '
                'background:rgba(139,92,246,0.10); border-left:4px solid '
                '#8b5cf6; border-radius:6px;">'
                f'<b style="color:#8b5cf6;">🔄 {len(reinstalls_list)} '
                f'reinstall{"s" if len(reinstalls_list) != 1 else ""} '
                'detected (in current sellers AND uninstalls list)</b><br>'
                '<span style="color:#94a3b8; font-size:0.85rem;">'
                'These sellers uninstalled at some point and are back '
                'now. Find out what changed — they\'re your best signal '
                'on what brings sellers back, and your warmest re-engagement '
                'leads.</span></div>',
                unsafe_allow_html=True,
            )
            for s in reinstalls_list[:10]:
                _render_reinstall_card(s)
            if len(reinstalls_list) > 10:
                st.caption(f"+ {len(reinstalls_list) - 10} more reinstalls.")

        if not events and not new_uninstalls and not new_installs_list and not reinstalls_list:
            st.info(
                "No flagged changes this cycle. Reps: no follow-up "
                "events to action; check the buckets below for "
                "steady-state outreach work."
            )
            return

        if events:
            st.write("")
            # Render up to 40 events, newest-kind-first (groups of the
            # same kind stay clustered). Reps scan this as a timeline.
            kind_order = list(_DELTA_STYLE.keys())
            events_sorted = sorted(
                events,
                key=lambda e: (
                    kind_order.index(e.kind) if e.kind in kind_order else 99,
                    -(e.value_after or 0),
                ),
            )
            for ev in events_sorted[:40]:
                _render_delta_event_card(ev)
            if len(events) > 40:
                st.caption(
                    f"+ {len(events) - 40} more events. Run a query on "
                    "`public.snapshots` for the full diff, or narrow the "
                    "app in the sidebar."
                )


def _render_new_uninstall_card(
    u: dict,
    *,
    install_by_email: dict | None = None,
    install_by_store: dict | None = None,
) -> None:
    """Compact card for one newly-uninstalled seller — what BD reps need
    to call/email today.

    Joins back to the prior active-sellers list (via email or store URL)
    to surface the seller's plan + lifetime orders + product count at
    the time of uninstall — cHAP drops these on the uninstalls page,
    so without this join BD reps would see "uninstalled" with no
    context on the seller's value. The install-time data answers
    "was this a small free-plan seller or a 4,000-order Pro account?"
    which dictates how the rep prioritises the callback.
    """
    email = u.get("email") or u.get("user_email") or "—"
    username = u.get("username") or ""
    store_field = u.get("store_url") or u.get("shop") or ""
    sid = u.get("seller_id") or ""
    uninstalled_on = (u.get("uninstalled_on") or "").strip()
    platform = (u.get("platform") or "").strip()

    contact_bits = []
    if email and email != "—":
        contact_bits.append(f'<a href="mailto:{email}" style="color:#a5b4fc;">{email}</a>')
    if username:
        contact_bits.append(f'<span style="color:#94a3b8;">{username}</span>')
    contact = " · ".join(contact_bits) or '<span style="color:#94a3b8;">no contact info captured</span>'

    # --- Look up install-time stats by email or store URL -------------
    install_row: dict = {}
    if install_by_email and email and email != "—":
        install_row = install_by_email.get(email.strip().lower(), {}) or {}
    if not install_row and install_by_store:
        # store_url may be in either `username` (cHAP uninstall row's
        # store-URL column) or `store_url` (uninstall variants).
        for candidate in (username, store_field):
            if not candidate:
                continue
            cand = (candidate or "").strip().lower()
            for prefix in ("https://", "http://", "www."):
                if cand.startswith(prefix):
                    cand = cand[len(prefix):]
            cand = cand.rstrip("/")
            if cand in install_by_store:
                install_row = install_by_store[cand]
                break

    # --- Build the "what they were worth" sub-line --------------------
    stat_bits = []
    plan = (install_row.get("plan") or "").strip()
    orders = install_row.get("order_count")
    products = install_row.get("product_count")
    failed = install_row.get("failed_order_count")
    if plan and plan.lower() not in {"n/a", "—", "-", "none"}:
        stat_bits.append(f'plan: <b>{plan}</b>')
    if orders not in (None, "", 0, "0"):
        try:
            stat_bits.append(f'lifetime orders: <b>{int(orders):,}</b>')
        except (ValueError, TypeError):
            pass
    if products not in (None, "", 0, "0"):
        try:
            stat_bits.append(f'products: <b>{int(products):,}</b>')
        except (ValueError, TypeError):
            pass
    if failed not in (None, "", 0, "0"):
        try:
            stat_bits.append(f'failed orders: <b>{int(failed):,}</b>')
        except (ValueError, TypeError):
            pass
    stats_line = " · ".join(stat_bits)

    meta_bits = []
    if uninstalled_on:
        meta_bits.append(f'uninstalled {uninstalled_on}')
    if platform:
        meta_bits.append(f'via {platform}')
    meta_bits.append(f'seller_id: {sid}')
    meta_line = " · ".join(meta_bits)

    # --- Render -------------------------------------------------------
    stats_html = (
        f'<div style="color:#cbd5e1; font-size:0.82rem; margin-top:3px;">'
        f'<span style="color:#94a3b8;">at uninstall:</span> {stats_line}</div>'
        if stats_line else ""
    )
    st.markdown(
        f'<div style="display:flex; align-items:flex-start; padding:10px 14px; '
        f'margin-bottom:6px; background:#1e293b; border-radius:8px; '
        f'border-left:3px solid #ef4444;">'
        f'<span style="font-size:1.1rem; margin-right:12px; line-height:1.3;">📞</span>'
        f'<div style="flex:1; min-width:0;">'
        f'<div style="color:#e2e8f0; font-weight:600;">{contact}</div>'
        f'{stats_html}'
        f'<div style="color:#64748b; font-size:0.76rem; margin-top:3px;">'
        f'{meta_line}</div></div></div>',
        unsafe_allow_html=True,
    )


def _render_new_install_card(s: dict) -> None:
    """One welcome card for a new install. Surfaces email + store + plan
    + installed date so a BD rep can pick up the relationship cold."""
    email = (s.get("email") or "").strip() or "—"
    store = (s.get("store_url") or "").strip()
    plan = (s.get("plan") or "").strip()
    installed = (s.get("installed_on") or "").strip()
    sid = s.get("seller_id") or ""
    contact_bits = []
    if email and email != "—":
        contact_bits.append(f'<a href="mailto:{email}" style="color:#a5b4fc;">{email}</a>')
    if store:
        contact_bits.append(f'<span style="color:#94a3b8;">{store}</span>')
    contact = " · ".join(contact_bits) or '<span style="color:#94a3b8;">no contact info captured</span>'
    detail_bits = []
    if installed:
        detail_bits.append(f"installed {installed}")
    if plan:
        detail_bits.append(f"plan: <b>{plan}</b>")
    detail_bits.append(f"seller_id: {sid}")
    detail = " · ".join(detail_bits)
    st.markdown(
        f'<div style="display:flex; align-items:center; padding:10px 14px; '
        f'margin-bottom:6px; background:#1e293b; border-radius:8px; '
        f'border-left:3px solid #10b981;">'
        f'<span style="font-size:1.1rem; margin-right:12px;">🌱</span>'
        f'<div style="flex:1; min-width:0;">'
        f'<div style="color:#e2e8f0; font-weight:600;">{contact}</div>'
        f'<div style="color:#64748b; font-size:0.78rem; margin-top:2px;">'
        f'{detail}</div></div></div>',
        unsafe_allow_html=True,
    )


def _render_reinstall_card(s: dict) -> None:
    """Card for a reinstall — seller is currently active AND in the
    uninstalls history. Highlights that they came back.
    """
    email = (s.get("email") or "").strip() or "—"
    store = (s.get("store_url") or "").strip()
    plan = (s.get("plan") or "").strip()
    orders = s.get("order_count") or 0
    sid = s.get("seller_id") or ""
    contact_bits = []
    if email and email != "—":
        contact_bits.append(f'<a href="mailto:{email}" style="color:#a5b4fc;">{email}</a>')
    if store:
        contact_bits.append(f'<span style="color:#94a3b8;">{store}</span>')
    contact = " · ".join(contact_bits) or '<span style="color:#94a3b8;">no contact info captured</span>'
    detail_bits = []
    if plan:
        detail_bits.append(f"plan: <b>{plan}</b>")
    if orders:
        detail_bits.append(f"orders: <b>{orders}</b>")
    detail_bits.append(f"seller_id: {sid}")
    detail = " · ".join(detail_bits)
    st.markdown(
        f'<div style="display:flex; align-items:center; padding:10px 14px; '
        f'margin-bottom:6px; background:#1e293b; border-radius:8px; '
        f'border-left:3px solid #8b5cf6;">'
        f'<span style="font-size:1.1rem; margin-right:12px;">🔄</span>'
        f'<div style="flex:1; min-width:0;">'
        f'<div style="color:#e2e8f0; font-weight:600;">{contact}</div>'
        f'<div style="color:#64748b; font-size:0.78rem; margin-top:2px;">'
        f'{detail}</div></div></div>',
        unsafe_allow_html=True,
    )


_EVENT_PLAIN_LANGUAGE = {
    "new_install": "Just installed — onboard them and confirm setup.",
    "churned": "Uninstalled. Reach out today to learn why.",
    "plan_upgrade": "Upgraded to a paid plan — strong success signal.",
    "plan_downgrade": "Dropped to a free plan — engagement at risk.",
    "plan_change": "Switched plans — confirm intent matches their growth.",
    "order_spike": (
        "Order volume jumped sharply since the last scrape — momentum "
        "signal, good time to upsell or check in."
    ),
    "failed_order_spike": (
        "Failed-order count is climbing. Sustained failures usually "
        "precede an uninstall — reach out to debug their integration "
        "before they leave."
    ),
}


def _render_delta_event_card(ev) -> None:
    """One row per event — dark card with left color-bar.

    Plain-language explanation lives BELOW the metric so any reader
    (BD rep, super admin, exec) understands what the event means
    without needing to know what 'failed_order_spike' is internally.
    """
    emoji, color, label = _DELTA_STYLE.get(ev.kind, ("·", "#94a3b8", ev.kind))

    # Plan change sub-line — only render when there's an actual plan
    # value on at least one side. Empty/N-A plans are noise.
    def _is_real(p): return bool((p or "").strip()) and (p or "").strip().lower() not in {"n/a", "—", "-", "none"}
    plan_line = ""
    if ev.kind in ("plan_upgrade", "plan_downgrade", "plan_change"):
        if _is_real(ev.plan_before) or _is_real(ev.plan_after):
            plan_line = (
                f'<span style="color:#94a3b8;">plan:</span> '
                f'<b>{ev.plan_before or "—"} → {ev.plan_after or "—"}</b>'
            )
    elif _is_real(ev.plan_after):
        plan_line = f'<span style="color:#94a3b8;">plan:</span> <b>{ev.plan_after}</b>'
    elif _is_real(ev.plan_before):
        plan_line = f'<span style="color:#94a3b8;">was on:</span> <b>{ev.plan_before}</b>'

    # Magnitude sub-line — relabel by event kind so the count is
    # self-describing. "Failed orders" reads better than "count".
    mag_line = ""
    if ev.value_before is not None and ev.value_after is not None:
        delta = ev.value_after - ev.value_before
        sign = "+" if delta > 0 else ""
        metric_label = {
            "failed_order_spike": "failed orders",
            "order_spike": "orders",
        }.get(ev.kind, "count")
        mag_line = (
            f'<span style="color:#94a3b8;">{metric_label}:</span> '
            f'<b>{ev.value_before} → {ev.value_after}</b> '
            f'<span style="color:{color};">({sign}{delta})</span>'
        )

    meta_bits = [x for x in (plan_line, mag_line) if x]
    meta_line = "  ·  ".join(meta_bits)

    explainer = _EVENT_PLAIN_LANGUAGE.get(ev.kind, "")

    identity = ev.store_url or ev.username or ev.email or ev.seller_id

    st.markdown(
        f'<div style="display:flex; gap:12px; padding:10px 14px; '
        f'margin:6px 0; border-radius:8px; background:#0f172a; '
        f'border-left:4px solid {color};">'
        f'<div style="font-size:1.25rem; line-height:1;">{emoji}</div>'
        f'<div style="flex:1; min-width:0;">'
        f'<div style="color:#e2e8f0; font-size:0.92rem; '
        f'font-weight:600;">{label}'
        f'<span style="color:#64748b; font-weight:400;"> · </span>'
        f'<span style="color:#cbd5e1; font-weight:500;">{identity}</span>'
        f'</div>'
        + (
            f'<div style="color:#94a3b8; font-size:0.8rem; '
            f'margin-top:3px;">{meta_line}</div>'
            if meta_line else ""
        )
        + (
            f'<div style="color:#cbd5e1; font-size:0.8rem; '
            f'margin-top:5px; line-height:1.45;">{explainer}</div>'
            if explainer else ""
        )
        + f'</div></div>',
        unsafe_allow_html=True,
    )
