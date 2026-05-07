"""
cedadmin_ui.py — Streamlit UI for the CedCommerce admin scrape.

Tabs:
  - 📊 Dashboard       — revenue + plan movements + top accounts
  - 🎯 Intelligence    — SQL lead buckets the support team works
  - 📋 Sellers         — full filterable table over all 28 columns

Strictly separate from cHAP — reads from cedadmin_data/, uses
cedadmin_roles for access. Auth uses the shared auth.gate(); only
the per-app permissions differ.

Design notes (2026-05-08 redesign):
  - Every KPI card carries a `help=` tooltip explaining how the
    support team uses it.
  - Charts via plotly so hovers show exact values; bar charts use a
    consistent colour palette tied to plan-tier so a tier renders
    the same colour everywhere it appears.
  - Status pills (Purchased / Trial Expired / License Expired / etc.)
    are colour-coded via st.dataframe's column_config.TextColumn so
    the eye lands on revenue states without scanning the row text.
  - All widgets carry an explicit `key=` so duplicated labels across
    tabs don't trip Streamlit's element-id collision check.
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

import auth
import cedadmin_analytics as ca
import cedadmin_roles
from ui_theme import (
    PALETTE,
    apply_shared_theme,
    tc_kpi as _kpi,
    tc_section as _section,
    tc_freshness_pill,
)


DATA_DIR = Path("cedadmin_data/latest")

# Single source of truth for plan-tier colours so the donut, the bar
# chart, and the badges all agree.
TIER_PALETTE: dict[str, str] = {
    "Yearly":      "#22c55e",  # green — long commitment
    "Monthly":     "#3b82f6",  # blue — recurring
    "Quarterly":   "#a855f7",  # purple
    "Half-Yearly": "#0ea5e9",  # sky
    "9 Month":     "#06b6d4",  # cyan
    "Pro":         "#f59e0b",  # amber
    "Combo":       "#ec4899",  # pink
    "Lite":        "#84cc16",  # lime
    "Basic":       "#10b981",  # emerald
    "Standard":    "#14b8a6",  # teal
    "Premium":     "#8b5cf6",  # violet
    "Custom":      "#6366f1",  # indigo
    "Free":        "#94a3b8",  # slate
    "Trial":       "#fbbf24",  # yellow
    "Enterprise":  "#ef4444",  # red — high value
    "Unknown":     "#cbd5e1",  # neutral
}

PURCHASE_PALETTE: dict[str, str] = {
    "Purchased":               "#22c55e",
    "Free Subscription":       "#3b82f6",
    "Trial Expired":           "#fbbf24",
    "Free Subscription Expire": "#f59e0b",
    "License Expired":         "#ef4444",
    "Not Purchase":            "#94a3b8",
    "(not set)":               "#cbd5e1",
}

TIER_BADGE_BG = {tier: color for tier, color in TIER_PALETTE.items()}


# --------------------------------------------------------------------
# Data load (cached)
# --------------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner=False)
def _load_walmart_us() -> tuple[list[dict], Optional[str]]:
    """Returns (normalized_rows, run_stamp). Empty list if file missing."""
    csv_path = DATA_DIR / "walmart_us__analytics.csv"
    stamp_path = DATA_DIR / "walmart_us__analytics.stamp"
    if not csv_path.exists():
        return [], None
    with csv_path.open(encoding="utf-8") as f:
        raw_rows = list(csv.DictReader(f))
    today = date.today()
    norm = [ca.normalize_row(r, today=today) for r in raw_rows]
    stamp = stamp_path.read_text(encoding="utf-8").strip() if stamp_path.exists() else None
    return norm, stamp


def _gate() -> auth.UserPrincipal:
    principal = auth.gate()
    if not cedadmin_roles.can(principal.email, "view_cedadmin"):
        st.set_page_config(
            page_title="CedCommerce Admin — access required",
            page_icon=":lock:",
            layout="centered",
        )
        st.error(
            f"Your account `{principal.email}` doesn't have access to the "
            f"CedCommerce admin dashboard."
        )
        st.caption(
            "This dashboard is gated separately from the cHAP dashboard. "
            "Ask a super admin to add you to `cedadmin_roles.yaml`."
        )
        if st.button("Sign out", key="cedadmin_denied_signout"):
            auth._do_sign_out(st)
        st.stop()
    return principal


# --------------------------------------------------------------------
# Main entry
# --------------------------------------------------------------------
def main() -> None:
    principal = _gate()

    st.set_page_config(
        page_title="CedCommerce Admin — Walmart Analytics",
        page_icon="🛒",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    apply_shared_theme()  # cHAP/cedadmin share the same look + sidebar.

    rows, stamp = _load_walmart_us()
    if not rows:
        st.title("🛒 CedCommerce Admin")
        st.warning(
            "No scraped data yet. Trigger Actions → `scrape-cedadmin` "
            "→ Run workflow, then come back."
        )
        with st.sidebar:
            auth.sign_out_button(st)
        return

    today = date.today()

    with st.sidebar:
        st.markdown("### CedCommerce Admin")
        st.caption(f"Last scrape: {stamp or 'unknown'}")
        st.caption(f"Sellers in latest snapshot: **{len(rows):,}**")
        st.divider()
        st.caption(f"Signed in as **{principal.email}**")
        cedadmin_role = cedadmin_roles.role_for(principal.email) or "viewer"
        st.caption(f"cedadmin role: `{cedadmin_role}`")
        if st.button("Sign out", use_container_width=True, key="cedadmin_sb_signout"):
            auth._do_sign_out(st)

    # Page header — bold + tag chip with last-scrape time so freshness
    # is the first thing the eye lands on.
    st.markdown(
        f'<div style="display:flex; align-items:center; gap:14px; margin-bottom:6px;">'
        f'<h1 style="margin:0;">🛒 CedCommerce Admin · Walmart US</h1>'
        f'{tc_freshness_pill("📅 " + (stamp or "unknown"))}'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Revenue + plan-movement + SQL pipeline for the Walmart Analytics "
        "section. Re-runs daily at 12:00 IST."
    )

    tab_dash, tab_intel, tab_table = st.tabs(
        ["📊 Dashboard", "🎯 Intelligence", "📋 Sellers"]
    )

    with tab_dash:
        _render_dashboard_tab(rows, today=today)
    with tab_intel:
        _render_intelligence_tab(rows, today=today, principal=principal)
    with tab_table:
        _render_sellers_tab(rows, principal=principal)


# --------------------------------------------------------------------
# Dashboard tab
# --------------------------------------------------------------------
def _render_dashboard_tab(rows: list[dict], today: date) -> None:
    m = ca.mrr_breakdown(rows)
    install_count = sum(
        1 for r in rows if (r.get("installation_status") or "").lower() == "install"
    )
    purchased_count = sum(
        1 for r in rows
        if (r.get("installation_status") or "").lower() == "install"
        and (r.get("purchase_status") or "") == "Purchased"
    )
    free_count = sum(
        1 for r in rows
        if (r.get("installation_status") or "").lower() == "install"
        and (r.get("purchase_status") or "") in ("Free Subscription", "Free Subscription Expire")
    )
    trial_count = sum(
        1 for r in rows
        if (r.get("installation_status") or "").lower() == "install"
        and (r.get("purchase_status") or "") == "Trial Expired"
    )
    total_seller_count = len(rows)
    uninstall_count = total_seller_count - install_count
    paid_pct = (100 * purchased_count / install_count) if install_count else 0

    cols = st.columns(5)
    _kpi(
        cols[0],
        label="💰 MRR",
        value=f"${m['total_mrr']:,.0f}",
        sub=f"ARR ≈ ${m['annual_run_rate']:,.0f}",
        color="#22c55e",
        help_text=(
            "Monthly Recurring Revenue from active+paid sellers with "
            "extractable plan prices. Used to track revenue health "
            "over time and prioritise high-value churn risks."
        ),
    )
    _kpi(
        cols[1],
        label="💳 Active Paid",
        value=f"{purchased_count:,}",
        sub=f"{paid_pct:.1f}% of installed",
        color="#a78bfa",
        help_text=(
            "Currently-installed sellers with purchase_status = "
            "Purchased. The denominator for upsell, retention, and "
            "renewal-renewal calculations."
        ),
    )
    _kpi(
        cols[2],
        label="🆓 Free + Trial",
        value=f"{free_count + trial_count:,}",
        sub=f"{free_count:,} free · {trial_count:,} trial",
        color="#60a5fa",
        help_text=(
            "Currently-installed sellers on Free Subscription or in "
            "Trial. The upsell pool — Intelligence tab ranks them by "
            "orders + SKUs into the SQL call list."
        ),
    )
    _kpi(
        cols[3],
        label="🟢 Currently Installed",
        value=f"{install_count:,}",
        sub=f"of {total_seller_count:,} ever",
        color="#fbbf24",
        help_text=(
            "Sellers whose latest installation_status is install. The "
            "active-customer count for any given snapshot — falls when "
            "uninstalls outpace new installs in a period."
        ),
    )
    _kpi(
        cols[4],
        label="🗑 Lifetime Uninstalls",
        value=f"{uninstall_count:,}",
        sub="audit trail of churn",
        color="#94a3b8",
        help_text=(
            "Total sellers whose latest status is uninstall. Used in "
            "the install-vs-uninstall monthly chart below to spot "
            "churn waves and plan-tier-specific retention drops."
        ),
    )

    st.write("")
    _section(
        "Plan tier mix",
        sub="Currently-installed sellers on Purchased status, sliced by plan cadence.",
    )

    col_pie, col_table = st.columns([3, 2])

    # Plan-tier counts (currently installed + paid).
    from collections import Counter
    tier_counts = Counter()
    tier_mrr: dict[str, float] = dict(m["by_tier"])
    for r in rows:
        if (r.get("installation_status") or "").lower() != "install":
            continue
        if (r.get("purchase_status") or "") != "Purchased":
            continue
        tier_counts[r.get("_plan_label") or "Unknown"] += 1

    if tier_counts:
        # Donut: count of paying sellers by tier.
        import plotly.graph_objects as go
        labels = list(tier_counts.keys())
        values = [tier_counts[k] for k in labels]
        colors = [TIER_PALETTE.get(k, TIER_PALETTE["Unknown"]) for k in labels]
        fig = go.Figure(data=[go.Pie(
            labels=labels, values=values, hole=0.55,
            marker=dict(colors=colors, line=dict(color=PALETTE["card"], width=2)),
            textposition="outside",
            textinfo="label+percent",
            hovertemplate="<b>%{label}</b><br>Sellers: %{value}<br>%{percent}<extra></extra>",
        )])
        fig.update_layout(
            height=320,
            margin=dict(t=10, b=10, l=10, r=10),
            showlegend=False,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color=PALETTE["text"], size=12),
        )
        col_pie.plotly_chart(fig, use_container_width=True, key="dash_tier_donut")

        # Side: table with seller count + MRR per tier.
        tier_df = pd.DataFrame([
            {
                "Tier": k,
                "Sellers": tier_counts[k],
                "MRR ($)": round(tier_mrr.get(k, 0), 2),
                "ARPA": round(tier_mrr.get(k, 0) / tier_counts[k], 2)
                       if tier_counts[k] else 0,
            }
            for k in labels
        ]).sort_values("MRR ($)", ascending=False)
        col_table.dataframe(
            tier_df,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Tier": st.column_config.TextColumn(
                    "Tier",
                    help="Plan cadence label parsed from current_subscribed_plan."
                ),
                "Sellers": st.column_config.NumberColumn(
                    "Sellers", format="%d",
                    help="Currently-installed sellers paying on this tier."
                ),
                "MRR ($)": st.column_config.NumberColumn(
                    "MRR ($)", format="$%.0f",
                    help="Sum of monthly-equivalent revenue across this tier."
                ),
                "ARPA": st.column_config.NumberColumn(
                    "ARPA", format="$%.2f",
                    help="Average Revenue Per Account = MRR ÷ Sellers."
                ),
            },
            height=320,
        )

    st.write("")
    _section(
        "Purchase status breakdown",
        sub="All sellers (installed + uninstalled) — shows the lapsed cohort sizes.",
    )

    # Purchase status across ALL sellers (not just installed) so we
    # see lapsed cohorts.
    status_counts = Counter((r.get("purchase_status") or "(not set)") for r in rows)
    if status_counts:
        import plotly.graph_objects as go
        labels = sorted(status_counts.keys(), key=lambda k: -status_counts[k])
        values = [status_counts[k] for k in labels]
        colors = [PURCHASE_PALETTE.get(k, "#cbd5e1") for k in labels]
        fig = go.Figure(data=[go.Bar(
            x=values, y=labels, orientation="h",
            marker_color=colors,
            text=[f"{v:,}" for v in values],
            textposition="outside",
            hovertemplate="<b>%{y}</b><br>Sellers: %{x:,}<extra></extra>",
        )])
        fig.update_layout(
            height=240,
            margin=dict(t=10, b=10, l=10, r=10),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color=PALETTE["text"], size=12),
            xaxis=dict(showgrid=False, showticklabels=False),
            yaxis=dict(showgrid=False),
        )
        st.plotly_chart(fig, use_container_width=True, key="dash_purchase_bar")

    st.write("")
    _section(
        "Install vs Uninstall — last 24 months",
        sub="Green = new installs, red = uninstalls, dotted line = net.",
    )

    series = ca.install_movement_series(rows)
    if series["months"]:
        import plotly.graph_objects as go
        months = series["months"][-24:]
        installs = series["new"][-24:]
        uninstalls = series["churn"][-24:]
        net = series["net"][-24:]
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=months, y=installs, name="Installs",
            marker_color="#22c55e",
            hovertemplate="<b>%{x}</b><br>Installs: %{y:,}<extra></extra>",
        ))
        fig.add_trace(go.Bar(
            x=months, y=[-u for u in uninstalls], name="Uninstalls",
            marker_color="#ef4444",
            hovertemplate="<b>%{x}</b><br>Uninstalls: %{customdata:,}<extra></extra>",
            customdata=uninstalls,
        ))
        fig.add_trace(go.Scatter(
            x=months, y=net, name="Net",
            mode="lines+markers",
            line=dict(color="#fbbf24", width=2, dash="dot"),
            hovertemplate="<b>%{x}</b><br>Net: %{y:+,}<extra></extra>",
        ))
        fig.update_layout(
            height=320,
            barmode="relative",
            margin=dict(t=20, b=10, l=10, r=10),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color=PALETTE["text"], size=12),
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02,
                xanchor="right", x=1,
            ),
            xaxis=dict(showgrid=False),
            yaxis=dict(zeroline=True, zerolinecolor=PALETTE["border"]),
        )
        st.plotly_chart(fig, use_container_width=True, key="dash_movement_bar")

    st.write("")
    _section(
        "Top 25 paying accounts",
        sub="Ranked by monthly-equivalent revenue. Click a column header to sort.",
    )

    paying = [
        r for r in rows
        if (r.get("installation_status") or "").lower() == "install"
        and (r.get("purchase_status") or "") == "Purchased"
        and (r.get("_mrr_usd") or 0) > 0
    ]
    paying.sort(key=lambda r: -(r.get("_mrr_usd") or 0))
    top25 = paying[:25]
    if top25:
        df = pd.DataFrame([
            {
                "Email": r.get("email"),
                "Shop": r.get("shop_url"),
                "Country": r.get("country") or "—",
                "Tier": r.get("_plan_label") or "—",
                "MRR ($)": round(r.get("_mrr_usd", 0), 2),
                "Plan": r.get("current_subscribed_plan", "")[:50],
                "Orders": r.get("_total_orders_n", 0),
                "Last login": (
                    r.get("_last_login_date").isoformat()
                    if r.get("_last_login_date") else "—"
                ),
            }
            for r in top25
        ])
        st.dataframe(
            df,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Tier": st.column_config.TextColumn("Tier", help="Plan cadence."),
                "MRR ($)": st.column_config.NumberColumn(
                    "MRR ($)", format="$%.2f",
                    help="Monthly-equivalent revenue from this account."
                ),
                "Orders": st.column_config.NumberColumn(
                    "Orders", format="%d",
                    help="Total orders ever processed for this seller."
                ),
            },
        )

    st.write("")
    _section(
        "Geographic distribution",
        sub="Top 15 countries by install count, with paid-conversion rate.",
    )

    geo = ca.country_distribution(rows)[:15]
    if geo:
        df = pd.DataFrame([
            {"Country": c, "Installed": ic, "Paid": pc,
             "% Paid": (100 * pc / ic) if ic else 0}
            for c, ic, pc in geo
        ])
        st.dataframe(
            df,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Country": st.column_config.TextColumn("Country"),
                "Installed": st.column_config.NumberColumn(
                    "Installed", format="%d",
                    help="Currently-installed sellers in this country."
                ),
                "Paid": st.column_config.NumberColumn(
                    "Paid", format="%d",
                    help="Of those installed, how many are on Purchased status."
                ),
                "% Paid": st.column_config.ProgressColumn(
                    "% Paid", min_value=0, max_value=100, format="%.1f%%",
                    help="Conversion rate within country: Paid ÷ Installed."
                ),
            },
        )


# --------------------------------------------------------------------
# Intelligence tab
# --------------------------------------------------------------------
def _render_intelligence_tab(
    rows: list[dict], today: date, principal: auth.UserPrincipal,
) -> None:
    st.markdown(
        "Each bucket is a **call list** for the support team. "
        "Highest-priority bucket wins per seller — no double-counting."
    )

    # ---- Filters across all buckets -----------------------------------
    with st.expander("🔍 Filters (apply across all buckets)", expanded=False):
        col1, col2, col3 = st.columns(3)
        f_country = col1.text_input(
            "Country contains",
            "",
            key="intel_filter_country",
            help="Substring match on the country field. Leave blank for all.",
        ).strip().lower()
        f_min_orders = col2.number_input(
            "Min total_orders",
            value=0, step=10,
            key="intel_filter_min_orders",
            help="Only show sellers with at least this many orders. Useful "
                 "to surface bigger-revenue opportunities only.",
        )
        f_business = col3.text_input(
            "Business category contains",
            "",
            key="intel_filter_business",
            help="Substring match on business_category. Try 'apparel', "
                 "'electronics', etc.",
        ).strip().lower()

    def _passes(r):
        if f_country and f_country not in (r.get("country") or "").lower():
            return False
        if f_min_orders and r.get("_total_orders_n", 0) < f_min_orders:
            return False
        if f_business and f_business not in (r.get("business_category") or "").lower():
            return False
        return True

    # Bucket every passing row.
    by_bucket: dict[str, list[dict]] = {b.id: [] for b in ca.LEAD_BUCKETS}
    for r in rows:
        if not _passes(r):
            continue
        bid = ca.bucket_for_lead(r, today=today)
        if bid:
            by_bucket[bid].append(r)

    # ---- Tier counter strip (top of tab) ------------------------------
    tier_counts = {"Hot": 0, "Warm": 0, "Cool": 0}
    for b in ca.LEAD_BUCKETS:
        tier_counts[b.tier] += len(by_bucket.get(b.id, []))
    grand_total = sum(tier_counts.values())

    cols = st.columns(4)
    _kpi(
        cols[0],
        label="🔥 Hot leads",
        value=f"{tier_counts['Hot']:,}",
        sub="call this week",
        color="#ef4444",
        help_text="Renewal-at-risk + Trial conversion + Upgrade-ready + Winback.",
    )
    _kpi(
        cols[1],
        label="☀ Warm leads",
        value=f"{tier_counts['Warm']:,}",
        sub="follow-up next 2 weeks",
        color="#f59e0b",
        help_text="Cross-sell + Paid-but-idle + Failure-rate-spike.",
    )
    _kpi(
        cols[2],
        label="❄ Cool leads",
        value=f"{tier_counts['Cool']:,}",
        sub="monitor / nurture",
        color="#3b82f6",
        help_text="Stuck-onboarding (support, not sales) + Reinstall-committed.",
    )
    _kpi(
        cols[3],
        label="📋 Total SQLs",
        value=f"{grand_total:,}",
        sub="surfaced from latest scrape",
        color="#a78bfa",
        help_text="Sum of all bucketed leads after filters. Each seller fits "
                  "exactly one bucket.",
    )

    st.write("")

    # ---- Per-bucket expander cards -----------------------------------
    for b in ca.LEAD_BUCKETS:
        rows_b = by_bucket.get(b.id, [])
        rows_b.sort(key=lambda r: -_lead_sort_key(r, b.id, today))

        emoji = {"Hot": "🔥", "Warm": "☀", "Cool": "❄"}[b.tier]
        tier_color = {"Hot": "#ef4444", "Warm": "#f59e0b", "Cool": "#3b82f6"}[b.tier]
        with st.expander(
            f"{emoji} {b.label} — {len(rows_b):,} sellers",
            expanded=(b.tier == "Hot" and len(rows_b) > 0),
        ):
            # Per-bucket header strip — tier badge + hint copy.
            st.markdown(
                f"""<div style="display:flex; align-items:center; gap:10px; margin:4px 0 10px 0;">
                  <span class="ked-tier" style="background:{tier_color};">{b.tier.upper()}</span>
                  <span style="color:#94a3b8; font-size:0.9rem;">{b.hint}</span>
                </div>""",
                unsafe_allow_html=True,
            )

            if not rows_b:
                st.caption("Empty — no sellers match this bucket today.")
                continue

            view = rows_b[:200]
            df = pd.DataFrame([_lead_row_summary(r, b.id, today) for r in view])
            # Score columns get a progress bar so the strongest leads
            # pop visually without scanning numbers.
            score_cols = [c for c in df.columns if c in ("Health", "Opportunity", "Winback score")]
            col_cfg = {}
            for c in score_cols:
                col_cfg[c] = st.column_config.ProgressColumn(
                    c, min_value=0, max_value=100, format="%d",
                    help={
                        "Health": "0-100 composite — login recency × failure rate × onboarding × renewal proximity. Lower = at risk.",
                        "Opportunity": "0-100 composite for free/trial sellers — orders + SKUs + age + login recency. Higher = upsell now.",
                        "Winback score": "0-100 — historical plans × order volume × recency-of-lapse. Higher = call this week.",
                    }.get(c, ""),
                )
            if "MRR ($)" in df.columns:
                col_cfg["MRR ($)"] = st.column_config.NumberColumn(
                    "MRR ($)", format="$%.2f",
                    help="Monthly-equivalent revenue from this seller."
                )
            if "Days to expiry" in df.columns:
                col_cfg["Days to expiry"] = st.column_config.NumberColumn(
                    "Days to expiry", format="%d",
                    help="Negative = already expired. Positive = renewal call within window."
                )
            if "Failure rate" in df.columns:
                col_cfg["Failure rate"] = st.column_config.TextColumn(
                    "Failure rate",
                    help="failed_orders / total_orders. >20% on a paying account is a support trigger."
                )
            if "Plan" in df.columns:
                col_cfg["Plan"] = st.column_config.TextColumn(
                    "Plan",
                    help="Truncated current_subscribed_plan string."
                )
            st.dataframe(
                df, hide_index=True, use_container_width=True,
                column_config=col_cfg,
            )

            if len(rows_b) > 200:
                st.caption(
                    f"Showing top 200 of {len(rows_b):,}. Use Filters above to "
                    f"narrow, or download the full bucket as CSV."
                )

            if cedadmin_roles.can(principal.email, "export_csv"):
                full_df = pd.DataFrame([_lead_row_summary(r, b.id, today) for r in rows_b])
                st.download_button(
                    f"⬇ Download {b.id}.csv ({len(rows_b):,} rows)",
                    data=full_df.to_csv(index=False).encode("utf-8"),
                    file_name=f"sql_{b.id}_{today.isoformat()}.csv",
                    mime="text/csv",
                    key=f"intel_dl_{b.id}",
                )
            else:
                st.caption(
                    "📎 CSV export is editor-only. Ask a super admin to grant "
                    "you the **editor** role on cedadmin."
                )


def _lead_row_summary(r: dict, bucket_id: str, today: date) -> dict:
    """Tabular summary of a single seller row inside a lead bucket."""
    base = {
        "Email": r.get("email"),
        "Shop": r.get("shop_url"),
        "Country": r.get("country") or "—",
        "Orders": r.get("_total_orders_n", 0),
        "SKUs": r.get("_published_sku_n", 0),
    }
    if bucket_id in ("renewal_at_risk", "paid_idle", "failure_spike"):
        base["MRR ($)"] = round(r.get("_mrr_usd", 0), 2)
        base["Plan"] = r.get("current_subscribed_plan", "")[:35]
        base["Health"] = ca.score_health(r, today=today)
    if bucket_id == "renewal_at_risk":
        base["Days to expiry"] = r.get("_days_to_expiration")
    if bucket_id == "paid_idle":
        base["Days since login"] = r.get("_days_since_login")
    if bucket_id == "failure_spike":
        base["Failure rate"] = f"{r.get('_failure_rate', 0) * 100:.1f}%"
    if bucket_id in ("upgrade_ready", "trial_conversion", "cross_sell_oldapps"):
        base["Plan"] = r.get("current_subscribed_plan", "")[:30]
        base["Opportunity"] = ca.score_opportunity(r, today=today)
    if bucket_id == "winback_high_value":
        base["Last expired"] = (
            r.get("_expiration_date").isoformat() if r.get("_expiration_date") else "—"
        )
        base["Plan history"] = r.get("_plan_history_count", 0)
        base["Winback score"] = ca.score_winback(r, today=today)
    if bucket_id == "cross_sell_oldapps":
        base["Other apps"] = r.get("_other_oldapps_count", 0)
    if bucket_id == "stuck_onboarding":
        base["Onboarding"] = r.get("onboarding_status") or "—"
        base["Days since install"] = r.get("_days_since_install")
    if bucket_id == "reinstall_committed":
        base["Last uninstall"] = (
            r.get("_uninstalled_date").isoformat() if r.get("_uninstalled_date") else "—"
        )
    return base


def _lead_sort_key(r: dict, bucket_id: str, today: date) -> float:
    """Rank rows WITHIN a bucket — most-actionable first."""
    if bucket_id == "renewal_at_risk":
        days = r.get("_days_to_expiration") or 999
        mrr = r.get("_mrr_usd") or 0
        return mrr * 1000 + (30 - max(0, days)) * 10
    if bucket_id == "trial_conversion":
        return r.get("_total_orders_n", 0) + r.get("_published_sku_n", 0) * 0.5
    if bucket_id == "upgrade_ready":
        return ca.score_opportunity(r, today=today)
    if bucket_id == "winback_high_value":
        return ca.score_winback(r, today=today)
    if bucket_id == "paid_idle":
        return (r.get("_mrr_usd") or 0) * 100 + (r.get("_days_since_login") or 0)
    if bucket_id == "failure_spike":
        return r.get("_failure_rate", 0) * (r.get("_total_orders_n", 0) or 1)
    return r.get("_total_orders_n", 0) or 0


# --------------------------------------------------------------------
# Sellers tab — full filterable table over all 28 columns
# --------------------------------------------------------------------
def _render_sellers_tab(rows: list[dict], principal: auth.UserPrincipal) -> None:
    st.markdown(
        "All 26k+ sellers, every column. Use the filters to narrow; "
        "download the filtered subset as CSV (editor+)."
    )

    with st.expander("🔍 Filters", expanded=True):
        col1, col2, col3 = st.columns(3)
        f_text = col1.text_input(
            "Email / shop contains",
            "",
            key="sellers_filter_text",
            help="Substring match across email and shop_url.",
        ).strip().lower()
        f_install = col2.selectbox(
            "Installation status",
            options=["any", "install", "uninstall"],
            key="sellers_filter_install",
            help="install = currently active. uninstall = lifetime churned.",
        )
        f_purchase = col3.selectbox(
            "Purchase status",
            options=[
                "any", "Purchased", "Trial Expired", "License Expired",
                "Free Subscription", "Free Subscription Expire", "Not Purchase",
            ],
            key="sellers_filter_purchase",
            help="Revenue state. Purchased = currently paying.",
        )
        col4, col5, col6 = st.columns(3)
        f_country = col4.text_input(
            "Country contains",
            "",
            key="sellers_filter_country",
            help="Substring match on country.",
        ).strip().lower()
        f_min_orders = col5.number_input(
            "Min total_orders",
            value=0, step=10,
            key="sellers_filter_min_orders",
            help="Floor on order count.",
        )
        f_min_skus = col6.number_input(
            "Min published_sku",
            value=0, step=10,
            key="sellers_filter_min_skus",
            help="Floor on published SKU count.",
        )

    def _passes(r):
        if f_text and f_text not in ((r.get("email") or "") + " " + (r.get("shop_url") or "")).lower():
            return False
        if f_install != "any" and (r.get("installation_status") or "") != f_install:
            return False
        if f_purchase != "any" and (r.get("purchase_status") or "") != f_purchase:
            return False
        if f_country and f_country not in (r.get("country") or "").lower():
            return False
        if f_min_orders and r.get("_total_orders_n", 0) < f_min_orders:
            return False
        if f_min_skus and r.get("_published_sku_n", 0) < f_min_skus:
            return False
        return True

    filtered = [r for r in rows if _passes(r)]
    st.caption(f"Matching: **{len(filtered):,}** of {len(rows):,} sellers")

    if not filtered:
        return

    csv_cols = [c for c in filtered[0].keys() if not c.startswith("_")]
    df = pd.DataFrame([{c: r.get(c) for c in csv_cols} for r in filtered[:500]])
    st.dataframe(df, hide_index=True, use_container_width=True)
    if len(filtered) > 500:
        st.caption(
            f"Showing top 500. Download the full {len(filtered):,}-row "
            f"filtered set as CSV."
        )

    if cedadmin_roles.can(principal.email, "export_csv"):
        full_df = pd.DataFrame([{c: r.get(c) for c in csv_cols} for r in filtered])
        st.download_button(
            f"⬇ Download {len(filtered):,} sellers as CSV",
            data=full_df.to_csv(index=False).encode("utf-8"),
            file_name=f"walmart_us_sellers_{date.today().isoformat()}.csv",
            mime="text/csv",
            key="sellers_dl_filtered",
        )
    else:
        st.caption("📎 CSV export is editor-only on cedadmin.")
