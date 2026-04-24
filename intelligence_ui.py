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

import os
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

import auth
import roles
import customer_intelligence as ci
import seller_profile_enricher as spe
from analytics_advanced import (
    DISPLAY_NAMES,
    display_name,
    exclude_test_stores,
)
from normalize import normalize_run_data
from supabase_client import SupabaseClient
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

            # ---- AI business analysis (per lead) ------------------
            # Drilldown: pick a shop from this bucket → Claude analyses
            # the storefront → we cache it + show business type,
            # categories, positioning, pitch opening.
            _render_analyse_block(
                bucket_rows=bucket.rows,
                bucket_id=bucket.id,
                app_key=app_key,
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
    _render_bulk_enrich_section(
        principal=principal,
        sellers_by_app_all=sellers_by_app,
    )
    st.divider()
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
# AI business analysis — per-lead drilldown under each bucket.
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
