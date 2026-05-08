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
import roles as _chap_roles  # for HARD_CODED_SUPER_ADMINS + audit_stamp
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
def _load_walmart_us() -> tuple[list[dict], list[dict], Optional[str]]:
    """Returns (current_normalized, previous_normalized, run_stamp).

    `previous_normalized` is empty until the second scrape runs, since
    the rotation only kicks in when the scraper has a prior latest to
    move aside. The "What changed" section silently hides itself in
    that case.
    """
    csv_path = DATA_DIR / "walmart_us__analytics.csv"
    prev_path = DATA_DIR / "walmart_us__analytics.previous.csv"
    stamp_path = DATA_DIR / "walmart_us__analytics.stamp"
    if not csv_path.exists():
        return [], [], None
    today = date.today()
    with csv_path.open(encoding="utf-8") as f:
        raw_rows = list(csv.DictReader(f))
    norm = [ca.normalize_row(r, today=today) for r in raw_rows]
    prev = []
    if prev_path.exists():
        with prev_path.open(encoding="utf-8") as f:
            prev_raw = list(csv.DictReader(f))
        prev = [ca.normalize_row(r, today=today) for r in prev_raw]
    stamp = stamp_path.read_text(encoding="utf-8").strip() if stamp_path.exists() else None
    return norm, prev, stamp


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

    import audit
    audit.heartbeat(
        principal.email, console="cedadmin", page="CedAdmin",
        user_agent=audit.current_user_agent(st),
    )

    st.set_page_config(
        page_title="CedCommerce Admin — Walmart Analytics",
        page_icon="🛒",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    apply_shared_theme()  # cHAP/cedadmin share the same look + sidebar.

    rows, prev_rows, stamp = _load_walmart_us()
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

    can_manage = cedadmin_roles.can(principal.email, "manage_grants")
    tab_labels = ["📊 Dashboard", "🎯 Intelligence", "📋 Sellers"]
    if can_manage:
        tab_labels.append("🔐 Access")
    tabs = st.tabs(tab_labels)

    with tabs[0]:
        _render_dashboard_tab(rows, today=today, prev_rows=prev_rows)
    with tabs[1]:
        _render_intelligence_tab(rows, today=today, principal=principal)
    with tabs[2]:
        _render_sellers_tab(rows, principal=principal)
    if can_manage:
        with tabs[3]:
            _render_access_tab(principal=principal)


# --------------------------------------------------------------------
# What changed since last sync — top-of-Dashboard delta section.
# --------------------------------------------------------------------
def _render_whats_changed(
    rows: list[dict], prev_rows: list[dict], *, today: date,
) -> None:
    diff = ca.snapshot_diff(rows, prev_rows, today=today)
    s = diff["summary"]

    _section(
        "What changed since the previous sync",
        sub="Diff between this scrape and the one before it. Hides "
            "automatically on the very first run after onboarding.",
    )

    # Delta KPI strip — green for positive, red for negative.
    mrr_delta = s["mrr_delta"]
    mrr_color = (
        "#22c55e" if mrr_delta > 0 else
        "#ef4444" if mrr_delta < 0 else
        PALETTE["text_soft"]
    )
    mrr_arrow = "↑" if mrr_delta > 0 else ("↓" if mrr_delta < 0 else "↔")
    paid_delta = s["current_paid"] - s["previous_paid"]
    paid_arrow = "↑" if paid_delta > 0 else ("↓" if paid_delta < 0 else "↔")
    paid_color = (
        "#22c55e" if paid_delta > 0 else
        "#ef4444" if paid_delta < 0 else
        PALETTE["text_soft"]
    )

    cols = st.columns(5)
    _kpi(
        cols[0],
        label="📈 MRR change",
        value=f"{mrr_arrow} ${abs(mrr_delta):,.0f}",
        sub=f"${s['mrr_now']:,.0f} now (was ${s['mrr_prev']:,.0f})",
        color=mrr_color,
        help_text=(
            "Net MRR change between snapshots — sum of (new payers + "
            "upgrades) minus (churn + downgrades). The single most "
            "important sales-cycle health metric."
        ),
    )
    _kpi(
        cols[1],
        label="💳 Paid sellers Δ",
        value=f"{paid_arrow} {abs(paid_delta):,}",
        sub=f"{s['current_paid']:,} now (was {s['previous_paid']:,})",
        color=paid_color,
        help_text="Net change in active+paid sellers between scrapes.",
    )
    _kpi(
        cols[2],
        label="🆕 New payers",
        value=f"{s['new_payers']:,}",
        sub="Status flipped to Purchased",
        color="#22c55e",
        help_text=(
            "Sellers whose purchase_status moved to Purchased since "
            "the previous scrape. Could be new sign-ups OR existing "
            "free-tier sellers who upgraded."
        ),
    )
    _kpi(
        cols[3],
        label="❌ Newly churned",
        value=f"{s['newly_churned']:,}",
        sub="Was Purchased, no longer is",
        color="#ef4444",
        help_text=(
            "Sellers whose purchase_status moved off Purchased. Could "
            "be License Expired, Trial Expired, or uninstalled."
        ),
    )
    _kpi(
        cols[4],
        label="🔁 Plan changes",
        value=f"{s['plan_upgrades'] + s['plan_downgrades']:,}",
        sub=f"{s['plan_upgrades']:,} ↑ · {s['plan_downgrades']:,} ↓",
        color="#a78bfa",
        help_text=(
            "Sellers who changed plan within Purchased status. Up = "
            "MRR went up, Down = MRR went down. Filters on this in "
            "Intelligence to find expansion/contraction patterns."
        ),
    )

    # Small extra-counts strip — installs / uninstalls.
    cols2 = st.columns(2)
    _kpi(
        cols2[0],
        label="🟢 New installs",
        value=f"{s['new_installs']:,}",
        sub="Brand-new accounts since last sync",
        color="#0ea5e9",
        help_text="Sellers seen for the first time OR moved from "
                  "uninstall back to install.",
    )
    _kpi(
        cols2[1],
        label="🗑 New uninstalls",
        value=f"{s['new_uninstalls']:,}",
        sub="Was install, now uninstall",
        color="#94a3b8",
        help_text="Sellers whose installation_status flipped to "
                  "uninstall since the previous scrape.",
    )

    # Detail tables for each change category — collapsed by default
    # so the strip stays scannable, expanded when curious.
    if s["new_payers"]:
        with st.expander(
            f"🆕 New payers detail — {s['new_payers']:,} sellers",
            expanded=False,
        ):
            df = pd.DataFrame(diff["new_payer_rows"])
            if not df.empty:
                st.dataframe(
                    df, hide_index=True, use_container_width=True,
                    column_config={
                        "mrr": st.column_config.NumberColumn(
                            "MRR ($)", format="$%.2f",
                            help="Monthly-equivalent revenue from this new payer."
                        ),
                    },
                )

    if s["newly_churned"]:
        with st.expander(
            f"❌ Newly churned detail — {s['newly_churned']:,} sellers",
            expanded=False,
        ):
            df = pd.DataFrame(diff["newly_churned_rows"])
            if not df.empty:
                st.dataframe(
                    df, hide_index=True, use_container_width=True,
                    column_config={
                        "previous_mrr": st.column_config.NumberColumn(
                            "Previous MRR ($)", format="$%.2f",
                            help="What this seller was paying before churning."
                        ),
                    },
                )

    if s["plan_upgrades"] + s["plan_downgrades"]:
        with st.expander(
            f"🔁 Plan changes detail — "
            f"{s['plan_upgrades'] + s['plan_downgrades']:,} sellers",
            expanded=False,
        ):
            df = pd.DataFrame(diff["plan_change_rows"])
            if not df.empty:
                st.dataframe(
                    df, hide_index=True, use_container_width=True,
                    column_config={
                        "previous_mrr": st.column_config.NumberColumn(
                            "Prev MRR", format="$%.2f",
                        ),
                        "new_mrr": st.column_config.NumberColumn(
                            "New MRR", format="$%.2f",
                        ),
                        "delta": st.column_config.NumberColumn(
                            "Δ MRR", format="$%+.2f",
                            help="Positive = upgrade, negative = downgrade.",
                        ),
                    },
                )

    if s["new_uninstalls"]:
        with st.expander(
            f"🗑 Newly uninstalled detail — {s['new_uninstalls']:,} sellers",
            expanded=False,
        ):
            df = pd.DataFrame(diff["newly_uninstalled_rows"])
            if not df.empty:
                st.dataframe(
                    df, hide_index=True, use_container_width=True,
                    column_config={
                        "previous_mrr": st.column_config.NumberColumn(
                            "Was paying ($)", format="$%.2f",
                        ),
                    },
                )

    st.divider()


# --------------------------------------------------------------------
# Dashboard tab
# --------------------------------------------------------------------
def _render_dashboard_tab(
    rows: list[dict], today: date, prev_rows: list[dict] | None = None,
) -> None:
    # ===================================================================
    # Top-of-tab: "What changed since last sync" — first thing the eye
    # lands on so an operator opening the page sees deltas before
    # absolute numbers. Hidden when there's no previous snapshot yet.
    # ===================================================================
    if prev_rows:
        _render_whats_changed(rows, prev_rows, today=today)

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

    # =================================================================
    # Section: Renewal forecast — what revenue is up for renewal soon?
    # =================================================================
    st.write("")
    _section(
        "Renewal forecast (next 90 days)",
        sub="Active+paid sellers bucketed by days-to-expiration. Each bucket "
            "is a renewal-call list with the MRR exposure attached.",
    )
    rf = ca.renewal_forecast(rows, today=today)

    rcols = st.columns(3)
    _kpi(
        rcols[0],
        label="MRR at risk · 90 days",
        value=f"${rf['total_at_risk_mrr']:,.0f}",
        sub=f"{rf['total_at_risk_sellers']:,} sellers",
        color="#ef4444",
        help_text=(
            "Sum of monthly-equivalent revenue from currently-paying "
            "sellers whose subscription expires within the next 90 days. "
            "Renewing this many accounts is the single highest-leverage "
            "support-team activity each quarter."
        ),
    )
    _kpi(
        rcols[1],
        label="Already expired",
        value=f"{rf['already_expired_count']:,}",
        sub="install + Purchased + past expiry",
        color="#f59e0b",
        help_text=(
            "Sellers still showing as installed AND Purchased but whose "
            "expiration_date is already in the past. Likely a payment-"
            "renewal lag or a churn that hasn't been finalized yet — "
            "highest-priority winback call."
        ),
    )
    _kpi(
        rcols[2],
        label="Renewal MRR · next 14 days",
        value=f"${(rf['buckets'][0]['mrr'] + rf['buckets'][1]['mrr']):,.0f}",
        sub=f"{rf['buckets'][0]['sellers'] + rf['buckets'][1]['sellers']:,} sellers",
        color="#a78bfa",
        help_text=(
            "MRR concentrated in the most urgent two buckets (0-7 + 8-14 "
            "days). This is the call list for THIS week."
        ),
    )

    st.write("")
    if rf["buckets"]:
        import plotly.graph_objects as go
        labels = [b["label"] for b in rf["buckets"]]
        sellers = [b["sellers"] for b in rf["buckets"]]
        mrrs = [b["mrr"] for b in rf["buckets"]]
        fig = go.Figure(data=[
            go.Bar(
                x=labels, y=mrrs, name="MRR ($)",
                marker_color=["#ef4444", "#f97316", "#f59e0b", "#fbbf24", "#fde047"],
                text=[f"${v:,.0f}" for v in mrrs],
                textposition="outside",
                hovertemplate="<b>%{x}</b><br>MRR: $%{y:,.2f}<br>"
                              "Sellers: %{customdata}<extra></extra>",
                customdata=sellers,
            ),
        ])
        fig.update_layout(
            height=300,
            margin=dict(t=20, b=10, l=10, r=10),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color=PALETTE["text"], size=12),
            yaxis=dict(showgrid=False, tickprefix="$"),
            xaxis=dict(showgrid=False),
            showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True, key="dash_renewal_forecast")

    # =================================================================
    # Section: Health-score distribution
    # =================================================================
    st.write("")
    _section(
        "Health score distribution (active paid base)",
        sub="0 = high churn risk, 100 = healthy. Skew tells you whether "
            "your paying base is durable or fragile right now.",
    )
    hd = ca.health_distribution(rows, today=today)
    if any(b["sellers"] for b in hd):
        import plotly.graph_objects as go
        labels = [b["label"] for b in hd]
        sellers = [b["sellers"] for b in hd]
        # Colour gradient from red (low score) to green (high)
        colors = [
            "#ef4444", "#f97316", "#f59e0b", "#fbbf24", "#facc15",
            "#a3e635", "#84cc16", "#65a30d", "#22c55e", "#16a34a",
        ]
        fig = go.Figure(data=[
            go.Bar(
                x=labels, y=sellers,
                marker_color=colors,
                text=[f"{v:,}" for v in sellers],
                textposition="outside",
                hovertemplate="<b>Score %{x}</b><br>Sellers: %{y:,}<br>"
                              "MRR: $%{customdata:,.0f}<extra></extra>",
                customdata=[b["mrr"] for b in hd],
            ),
        ])
        fig.update_layout(
            height=280,
            margin=dict(t=20, b=10, l=10, r=10),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color=PALETTE["text"], size=12),
            xaxis=dict(showgrid=False, title="Health score"),
            yaxis=dict(showgrid=False, title="Sellers"),
            showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True, key="dash_health_hist")

    # =================================================================
    # Section: Failure rate by business_category
    # =================================================================
    st.write("")
    _section(
        "Failure rate by business category",
        sub="failed_orders ÷ total_orders, segments with ≥10 sellers "
            "and ≥200 total orders. Top of the list = where to send "
            "support before it becomes a churn driver.",
    )
    fr = ca.failure_rate_by_segment(
        rows, segment="business_category",
        min_sellers=10, min_orders_total=200,
    )[:15]
    if fr:
        df = pd.DataFrame([
            {
                "Category": f["segment"],
                "Sellers": f["sellers"],
                "Orders": f["orders"],
                "Failed": f["failed"],
                "Failure rate": f["failure_rate"] * 100,
                "MRR ($)": f["mrr"],
            }
            for f in fr
        ])
        st.dataframe(
            df,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Category": st.column_config.TextColumn(
                    "Category",
                    help="business_category from the seller record.",
                ),
                "Sellers": st.column_config.NumberColumn("Sellers", format="%d"),
                "Orders": st.column_config.NumberColumn("Orders", format="%d"),
                "Failed": st.column_config.NumberColumn("Failed", format="%d"),
                "Failure rate": st.column_config.ProgressColumn(
                    "Failure rate", min_value=0, max_value=100,
                    format="%.1f%%",
                    help="failed_orders ÷ total_orders for this category.",
                ),
                "MRR ($)": st.column_config.NumberColumn(
                    "MRR ($)", format="$%.0f",
                    help="Sum of monthly revenue from this category — "
                         "shows whether high-failure categories are "
                         "high-value or long-tail."
                ),
            },
        )

    # =================================================================
    # Section: Plan-flow Sankey — upgrades / downgrades / sideways
    # =================================================================
    st.write("")
    _section(
        "Plan flow — most-frequent upgrade / downgrade transitions",
        sub="Adjacent pairs from each seller's plan-history list. "
            "Width of the link = how many sellers made that transition.",
    )
    pf = ca.plan_flow_pairs(rows, top_n_pairs=20)
    if pf:
        import plotly.graph_objects as go
        # Build the Sankey index (each unique label gets an integer id).
        labels: list[str] = []
        idx: dict[str, int] = {}
        for p in pf:
            for k in (p["from"], p["to"]):
                if k not in idx:
                    idx[k] = len(labels)
                    labels.append(k)
        fig = go.Figure(data=[go.Sankey(
            arrangement="snap",
            node=dict(
                label=labels,
                pad=15, thickness=18,
                line=dict(color=PALETTE["border"], width=0.5),
                color=PALETTE["primary"],
            ),
            link=dict(
                source=[idx[p["from"]] for p in pf],
                target=[idx[p["to"]] for p in pf],
                value=[p["count"] for p in pf],
                color="rgba(99, 102, 241, 0.35)",  # primary @ 35%
                hovertemplate=(
                    "<b>%{source.label}</b> → "
                    "<b>%{target.label}</b><br>"
                    "Sellers: %{value}<extra></extra>"
                ),
            ),
        )])
        fig.update_layout(
            height=420,
            margin=dict(t=10, b=10, l=10, r=10),
            paper_bgcolor="rgba(0,0,0,0)",
            font=dict(color=PALETTE["text"], size=11),
        )
        st.plotly_chart(fig, use_container_width=True, key="dash_plan_sankey")
    else:
        st.caption(
            "No plan-history transitions captured yet — `all_plans_subscribed` "
            "is empty for every seller in this snapshot."
        )

    # =================================================================
    # Section: Cohort conversion table
    # =================================================================
    st.write("")
    _section(
        "Install cohort conversion",
        sub="Each row is sellers who installed in that month. ever_paid = "
            "made any payment ever; still_paid = currently install + "
            "Purchased.",
    )
    cohorts = ca.cohort_table(rows)
    if cohorts:
        # Show the most recent 18 months — older history isn't typically
        # actionable, just historical context.
        recent = cohorts[-18:]
        df = pd.DataFrame(recent)
        df = df[["cohort", "installs", "ever_paid", "ever_paid_pct",
                 "still_paid_now", "still_paid_pct"]]
        st.dataframe(
            df,
            hide_index=True,
            use_container_width=True,
            column_config={
                "cohort": st.column_config.TextColumn("Install month"),
                "installs": st.column_config.NumberColumn(
                    "Installs", format="%d",
                    help="Sellers who first installed during this month.",
                ),
                "ever_paid": st.column_config.NumberColumn(
                    "Ever paid", format="%d",
                    help="Of those installs, how many ever made a payment.",
                ),
                "ever_paid_pct": st.column_config.ProgressColumn(
                    "Ever-paid %", min_value=0, max_value=100,
                    format="%.1f%%",
                    help="ever_paid ÷ installs. Higher = better top-of-funnel "
                         "conversion for that cohort.",
                ),
                "still_paid_now": st.column_config.NumberColumn(
                    "Still paid", format="%d",
                    help="Of those installs, how many are STILL on Purchased "
                         "status today.",
                ),
                "still_paid_pct": st.column_config.ProgressColumn(
                    "Retained %", min_value=0, max_value=100,
                    format="%.1f%%",
                    help="still_paid_now ÷ installs. Long-run retention rate "
                         "for the cohort.",
                ),
            },
        )

    # =================================================================
    # Section: Revenue concentration (Pareto)
    # =================================================================
    st.write("")
    _section(
        "Revenue concentration",
        sub="What share of MRR sits with the top X% of paying sellers? "
            "If concentration is high, protecting those few accounts IS "
            "your retention strategy.",
    )
    rc = ca.revenue_concentration(rows)
    if rc["active_paid"] and rc["total_mrr"]:
        ccols = st.columns(4)
        for i, (label, share, sub_help) in enumerate([
            ("Top 1%",  rc["top_1_pct_share"],
             "MRR carried by the top-1% of paying sellers (by MRR)."),
            ("Top 5%",  rc["top_5_pct_share"],
             "MRR carried by the top 5%."),
            ("Top 10%", rc["top_10_pct_share"],
             "MRR carried by the top 10%."),
            ("Top 20%", rc["top_20_pct_share"],
             "MRR carried by the top 20% — the classic Pareto lens."),
        ]):
            _kpi(
                ccols[i],
                label=label,
                value=f"{share * 100:.1f}%",
                sub=f"of ${rc['total_mrr']:,.0f} total MRR",
                color="#a78bfa",
                help_text=sub_help,
            )

    # =================================================================
    # Section: Save-call list — high MRR + low health
    # =================================================================
    st.write("")
    _section(
        "Save-call list (highest MRR × lowest health)",
        sub="Currently-paying sellers ranked by mrr × (100 − health). "
            "These are the accounts most worth a proactive call before "
            "renewal day.",
    )
    risk_rows = ca.predictive_churn_risk(rows, today=today)[:25]
    if risk_rows:
        df = pd.DataFrame(risk_rows)
        df = df[[
            "email", "shop_url", "country", "mrr", "health",
            "days_since_login", "days_to_expiration", "failure_rate",
            "risk_score",
        ]]
        st.dataframe(
            df, hide_index=True, use_container_width=True,
            column_config={
                "email":     st.column_config.TextColumn("Email"),
                "shop_url":  st.column_config.TextColumn("Shop"),
                "country":   st.column_config.TextColumn("Country"),
                "mrr":       st.column_config.NumberColumn(
                    "MRR ($)", format="$%.2f",
                ),
                "health":    st.column_config.ProgressColumn(
                    "Health", min_value=0, max_value=100, format="%d",
                    help="0 = at risk, 100 = healthy.",
                ),
                "days_since_login": st.column_config.NumberColumn(
                    "Days idle", format="%d",
                    help="Days since last login. Higher = more disengaged.",
                ),
                "days_to_expiration": st.column_config.NumberColumn(
                    "Days to renew", format="%d",
                    help="Negative = already past expiration.",
                ),
                "failure_rate": st.column_config.NumberColumn(
                    "Failure %", format="%.1f%%",
                    help="failed_orders ÷ total_orders × 100.",
                ),
                "risk_score": st.column_config.ProgressColumn(
                    "Risk score", min_value=0,
                    max_value=max((r["risk_score"] for r in risk_rows), default=1),
                    format="%.0f",
                    help="MRR × (100 − Health). Highest = call FIRST.",
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
                clicked = st.download_button(
                    f"⬇ Download {b.id}.csv ({len(rows_b):,} rows)",
                    data=full_df.to_csv(index=False).encode("utf-8"),
                    file_name=f"sql_{b.id}_{today.isoformat()}.csv",
                    mime="text/csv",
                    key=f"intel_dl_{b.id}",
                )
                if clicked:
                    try:
                        import audit
                        audit.log_action(
                            email=principal.email,
                            console="cedadmin",
                            page="Intelligence",
                            action="csv_download",
                            target_type="lead_bucket",
                            target_id=b.id,
                            details={"rows": int(len(rows_b))},
                        )
                    except Exception:
                        pass
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
        clicked = st.download_button(
            f"⬇ Download {len(filtered):,} sellers as CSV",
            data=full_df.to_csv(index=False).encode("utf-8"),
            file_name=f"walmart_us_sellers_{date.today().isoformat()}.csv",
            mime="text/csv",
            key="sellers_dl_filtered",
        )
        if clicked:
            try:
                import audit
                audit.log_action(
                    email=principal.email,
                    console="cedadmin",
                    page="Sellers",
                    action="csv_download",
                    target_type="seller_table",
                    target_id="walmart_us",
                    details={"rows": int(len(filtered))},
                )
            except Exception:
                pass
    else:
        st.caption("📎 CSV export is editor-only on cedadmin.")


# --------------------------------------------------------------------
# Access tab — super-admin grant management for cedadmin
# --------------------------------------------------------------------
# Strictly separate from the cHAP Users tab in admin_ui.py. A user
# with cHAP super_admin does NOT automatically get cedadmin access —
# they have to be added here as well. This is per Hrithik's "separate
# access" rule for the cedadmin panel.
def _render_access_tab(principal: auth.UserPrincipal) -> None:
    _section(
        "CedCommerce admin access",
        sub="Grants are SEPARATE from cHAP. Adding someone here gives "
            "them visibility on this panel only — their cHAP role is "
            "untouched, and vice-versa.",
    )

    # ---- Role legend so super admins know what they're granting ----
    with st.expander("ℹ️  What each role can do", expanded=False):
        st.markdown(
            """
            | Role | Dashboard | Intelligence | Sellers tab | CSV export | Manage grants |
            | --- | --- | --- | --- | --- | --- |
            | **viewer** | ✅ | ✅ | ✅ | ❌ | ❌ |
            | **editor** | ✅ | ✅ | ✅ | ✅ | ❌ |
            | **super_admin** | ✅ | ✅ | ✅ | ✅ | ✅ |

            Hard-coded super admins (the project owner) always have
            super_admin here as a break-glass — they cannot lock
            themselves out by editing the YAML wrong.
            """
        )

    # ---- Grant form ----
    with st.form("cedadmin_grant_form", clear_on_submit=True):
        st.markdown("**➕ Grant access**")
        col_email, col_role = st.columns([3, 1])
        new_email = col_email.text_input(
            "Email",
            placeholder="someone@threecolts.com",
            key="cedadmin_grant_email",
            help="Threecolts email of the user to grant. Will be lowercased.",
        )
        new_role = col_role.selectbox(
            "Role",
            options=[
                cedadmin_roles.VIEWER,
                cedadmin_roles.EDITOR,
                cedadmin_roles.SUPER_ADMIN,
            ],
            index=0,
            key="cedadmin_grant_role",
            help="viewer = read-only, editor = +CSV export, super_admin = "
                 "+ this Access tab.",
        )
        submit = st.form_submit_button("Grant / Update")
        if submit:
            try:
                cedadmin_roles.set_grant(new_email, new_role)
            except ValueError as e:
                st.error(f"Invalid input: {e}")
            except Exception as e:
                st.error(f"Couldn't write cedadmin_roles.yaml: {e}")
            else:
                _commit_cedadmin_roles_yaml(
                    principal,
                    f"grant {new_role} to {new_email.strip().lower()}",
                )
                try:
                    import audit
                    audit.log_action(
                        email=principal.email,
                        console="cedadmin",
                        page="Access",
                        action="grant_role",
                        target_type="user",
                        target_id=new_email.strip().lower(),
                        details={"role": new_role},
                    )
                except Exception:
                    pass
                st.success(
                    f"Granted **{new_role}** to **{new_email.strip().lower()}** "
                    f"on cedadmin. Change is live and pushed to GitHub."
                )
                _load_walmart_us.clear()  # role lookups don't cache, but be safe
                st.rerun()

    st.write("")

    # ---- Current grants table ----
    st.markdown("**🗂 Current grants**")
    grants = cedadmin_roles.list_grants()
    hard_coded = set(_chap_roles.HARD_CODED_SUPER_ADMINS)

    if not grants and not hard_coded:
        st.caption("No grants configured.")
        return

    rows_grants: list[dict] = []
    for email in sorted(hard_coded):
        rows_grants.append({
            "Email": email,
            "Role": "super_admin",
            "Source": "hard-coded (roles.py)",
            "_revocable": False,
        })
    for email, role in grants:
        if email in hard_coded:
            # Already shown above with the hard-coded source.
            continue
        rows_grants.append({
            "Email": email,
            "Role": role,
            "Source": "cedadmin_roles.yaml",
            "_revocable": True,
        })

    df = pd.DataFrame(rows_grants)
    st.dataframe(
        df.drop(columns=["_revocable"]),
        hide_index=True,
        use_container_width=True,
        column_config={
            "Email": st.column_config.TextColumn("Email"),
            "Role": st.column_config.TextColumn(
                "Role",
                help="super_admin > editor > viewer.",
            ),
            "Source": st.column_config.TextColumn(
                "Source",
                help="Where the grant comes from. hard-coded entries can't "
                     "be revoked from the UI — edit roles.py for that.",
            ),
        },
    )

    # ---- Per-row revoke (revocable rows only) ----
    revocable = [r for r in rows_grants if r["_revocable"]]
    if revocable:
        st.markdown("**Revoke a grant**")
        col_select, col_btn = st.columns([3, 1])
        target = col_select.selectbox(
            "Email to revoke",
            options=[r["Email"] for r in revocable],
            key="cedadmin_revoke_target",
            help="Removes the email from cedadmin_roles.yaml. They will lose "
                 "cedadmin access on next page load. Their cHAP role is "
                 "unaffected.",
        )
        if col_btn.button(
            "🗑 Revoke", key="cedadmin_revoke_btn",
            type="primary", use_container_width=True,
        ):
            if target in hard_coded:
                st.error(
                    f"`{target}` is hard-coded in roles.py and can't be "
                    f"revoked from the UI."
                )
            else:
                removed = cedadmin_roles.revoke_grant(target)
                if removed:
                    _commit_cedadmin_roles_yaml(
                        principal, f"revoke {target}",
                    )
                    try:
                        import audit
                        audit.log_action(
                            email=principal.email,
                            console="cedadmin",
                            page="Access",
                            action="revoke_role",
                            target_type="user",
                            target_id=target,
                        )
                    except Exception:
                        pass
                    st.success(f"Revoked cedadmin access for **{target}**.")
                    _load_walmart_us.clear()
                    st.rerun()
                else:
                    st.warning(f"`{target}` wasn't in cedadmin_roles.yaml.")


def _commit_cedadmin_roles_yaml(
    principal: auth.UserPrincipal, action: str,
) -> None:
    """Push cedadmin_roles.yaml back up to GitHub so the live Streamlit
    Cloud deploy picks it up. Mirror of admin_ui._commit_roles_yaml,
    but writing to a different file. Local-dev (no [github] secrets)
    silently no-ops — the file is still updated on disk."""
    try:
        import github_secret_updater as gh
        ctx = gh.context_from_streamlit(st)
    except Exception:
        return
    try:
        body = Path("cedadmin_roles.yaml").read_text(encoding="utf-8")
        msg = f"chore(cedadmin-roles): {_chap_roles.audit_stamp(principal.email, action)}"
        gh.put_file(ctx, "cedadmin_roles.yaml", body, msg)
    except Exception as e:
        st.warning(
            f"Local change saved, but couldn't commit to GitHub: {e}. "
            f"The change will be lost on the next Streamlit Cloud redeploy "
            f"unless someone commits cedadmin_roles.yaml manually."
        )
