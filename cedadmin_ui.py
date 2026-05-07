"""
cedadmin_ui.py — Streamlit UI for the CedCommerce admin scrape.

Three tabs:
  - Dashboard       — revenue + plan movements + headlines
  - Intelligence    — SQL lead buckets the support team works
  - Sellers         — full filterable table over all 28 columns

Strictly separate from cHAP — reads from cedadmin_data/, uses
cedadmin_roles for access. Auth still uses the shared auth.gate()
because the login form is the same; only the per-app permissions
differ.
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


DATA_DIR = Path("cedadmin_data/latest")


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


def _kpi_card(label: str, value: str, sublabel: str = "",
              color: str = "#0f172a", bg: str = "#1e293b") -> str:
    return (
        f'<div style="padding:14px 18px; background:{bg}; '
        f'border-radius:10px; border:1px solid #334155;">'
        f'<div style="color:#94a3b8; font-size:0.78rem; font-weight:600; '
        f'letter-spacing:0.06em; text-transform:uppercase;">{label}</div>'
        f'<div style="color:{color}; font-size:1.75rem; font-weight:700; '
        f'line-height:1.1; margin-top:6px; font-variant-numeric:tabular-nums;">'
        f'{value}</div>'
        + (
            f'<div style="color:#94a3b8; font-size:0.8rem; margin-top:4px;">'
            f'{sublabel}</div>'
            if sublabel else ""
        )
        + '</div>'
    )


def _gate() -> auth.UserPrincipal:
    """Login + cedadmin access check. Renders an access-denied screen
    if the user is logged in to cHAP but isn't on the cedadmin grant
    list."""
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
        if st.button("Sign out"):
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
        if st.button("Sign out", use_container_width=True):
            auth._do_sign_out(st)

    st.title("🛒 CedCommerce Admin · Walmart US")
    st.caption(
        "Revenue + plan-movement + SQL pipeline for the Walmart Analytics "
        "section. Re-runs daily at 12:00 IST; manually via Actions → "
        "scrape-cedadmin → Run workflow."
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
    total_seller_count = len(rows)
    uninstall_count = total_seller_count - install_count

    cols = st.columns(5)
    cols[0].markdown(
        _kpi_card(
            "💰 MRR (extractable)",
            f"${m['total_mrr']:,.0f}",
            sublabel=f"ARR ≈ ${m['annual_run_rate']:,.0f}",
            color="#22c55e",
        ),
        unsafe_allow_html=True,
    )
    cols[1].markdown(
        _kpi_card(
            "💳 Active Paid",
            f"{purchased_count:,}",
            sublabel=f"{m['rows_with_unknown_price']:,} with unknown price",
            color="#a78bfa",
        ),
        unsafe_allow_html=True,
    )
    cols[2].markdown(
        _kpi_card(
            "🆓 Free / Trial",
            f"{free_count:,}",
            sublabel="On Free or Trial tier",
            color="#60a5fa",
        ),
        unsafe_allow_html=True,
    )
    cols[3].markdown(
        _kpi_card(
            "🟢 Currently Installed",
            f"{install_count:,}",
            sublabel=f"of {total_seller_count:,} ever",
            color="#fbbf24",
        ),
        unsafe_allow_html=True,
    )
    cols[4].markdown(
        _kpi_card(
            "🗑 Lifetime Uninstalls",
            f"{uninstall_count:,}",
            sublabel="audit trail of churn",
            color="#94a3b8",
        ),
        unsafe_allow_html=True,
    )

    st.divider()

    # Plan tier breakdown chart + top accounts side-by-side.
    left, right = st.columns([3, 2])

    with left:
        st.markdown("#### MRR by plan tier")
        if m["by_tier"]:
            tier_df = pd.DataFrame(
                [{"Tier": k, "MRR (USD)": round(v, 2)} for k, v in m["by_tier"].items()]
            ).sort_values("MRR (USD)", ascending=False)
            st.bar_chart(tier_df.set_index("Tier"), height=320)
        else:
            st.caption("No paid sellers with extractable price.")

    with right:
        st.markdown("#### Plan-tier seller counts")
        from collections import Counter
        tier_counts = Counter()
        for r in rows:
            if (r.get("installation_status") or "").lower() != "install":
                continue
            if (r.get("purchase_status") or "") != "Purchased":
                continue
            tier_counts[r.get("_plan_label") or "Unknown"] += 1
        if tier_counts:
            tier_count_df = pd.DataFrame(
                [{"Tier": k, "Sellers": v} for k, v in tier_counts.items()]
            ).sort_values("Sellers", ascending=False)
            st.dataframe(tier_count_df, hide_index=True, use_container_width=True, height=320)

    st.divider()

    # Plan movements over time — installs vs uninstalls.
    series = ca.install_movement_series(rows)
    if series["months"]:
        st.markdown("#### Install vs uninstall — monthly")
        movement_df = pd.DataFrame({
            "Month": series["months"],
            "Installs": series["new"],
            "Uninstalls": series["churn"],
            "Net": series["net"],
        })
        # Limit to the last 24 months for readability — earlier history
        # available via the cohort tab (future).
        movement_df = movement_df.tail(24)
        st.bar_chart(
            movement_df.set_index("Month")[["Installs", "Uninstalls"]],
            height=300,
        )

    st.divider()

    # Top paying accounts.
    st.markdown("#### Top 25 paying accounts (by MRR)")
    paying = [
        r for r in rows
        if (r.get("installation_status") or "").lower() == "install"
        and (r.get("purchase_status") or "") == "Purchased"
        and (r.get("_mrr_usd") or 0) > 0
    ]
    paying.sort(key=lambda r: -(r.get("_mrr_usd") or 0))
    top25 = paying[:25]
    if top25:
        st.dataframe(
            [
                {
                    "Email": r.get("email"),
                    "Shop": r.get("shop_url"),
                    "Country": r.get("country") or "—",
                    "Plan": r.get("current_subscribed_plan", "")[:40],
                    "Tier": r.get("_plan_label") or "—",
                    "MRR ($)": f"{r.get('_mrr_usd', 0):.2f}",
                    "Orders": r.get("_total_orders_n", 0),
                    "Last login": (
                        r.get("_last_login_date").isoformat()
                        if r.get("_last_login_date") else "—"
                    ),
                }
                for r in top25
            ],
            hide_index=True,
            use_container_width=True,
        )

    st.divider()

    # Geographic distribution.
    st.markdown("#### Installs by country (top 15)")
    geo = ca.country_distribution(rows)[:15]
    if geo:
        geo_df = pd.DataFrame(
            [
                {"Country": c, "Installed": ic, "Paid": pc,
                 "% Paid": f"{(100 * pc / ic):.1f}%" if ic else "—"}
                for c, ic, pc in geo
            ]
        )
        st.dataframe(geo_df, hide_index=True, use_container_width=True)


# --------------------------------------------------------------------
# Intelligence tab
# --------------------------------------------------------------------
def _render_intelligence_tab(
    rows: list[dict], today: date, principal: auth.UserPrincipal,
) -> None:
    st.markdown(
        "Each bucket below is a **call list** for the support team. "
        "Highest-priority bucket wins — sellers don't appear in two lists."
    )
    st.caption(
        "Filters apply across all buckets. Per-bucket downloads CSV the "
        "rows in that view."
    )

    # Filters (apply globally to the bucket lists below).
    with st.expander("Filters", expanded=False):
        f_country = st.text_input("Country contains", "").strip().lower()
        f_min_orders = st.number_input("Min total_orders", value=0, step=10)
        f_business = st.text_input("business_category contains", "").strip().lower()

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

    # Tier counter strip.
    tier_counts = {"Hot": 0, "Warm": 0, "Cool": 0}
    for b in ca.LEAD_BUCKETS:
        tier_counts[b.tier] += len(by_bucket.get(b.id, []))
    cols = st.columns(3)
    for i, (tier, color) in enumerate([("Hot", "#ef4444"), ("Warm", "#f59e0b"), ("Cool", "#3b82f6")]):
        cols[i].markdown(
            _kpi_card(f"{tier} leads", f"{tier_counts[tier]:,}", color=color),
            unsafe_allow_html=True,
        )

    st.divider()

    # Render each bucket as an expander with seller table inside.
    for b in ca.LEAD_BUCKETS:
        rows_b = by_bucket.get(b.id, [])
        rows_b.sort(key=lambda r: -_lead_sort_key(r, b.id, today))

        emoji = {"Hot": "🔥", "Warm": "☀", "Cool": "❄"}[b.tier]
        with st.expander(
            f"{emoji} {b.label} — **{len(rows_b):,}** sellers",
            expanded=(b.tier == "Hot" and len(rows_b) > 0),
        ):
            st.caption(b.hint)
            if not rows_b:
                st.caption("Empty — no sellers match this bucket today.")
                continue

            # Cap rendered rows at 200; the CSV download has all of
            # them. Without a cap a 3,000-row Streamlit table renders
            # slowly and overwhelms the support team's eye.
            view = rows_b[:200]
            df = pd.DataFrame([_lead_row_summary(r, b.id, today) for r in view])
            st.dataframe(df, hide_index=True, use_container_width=True)
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
                )
            else:
                st.caption(
                    "📎 CSV export is editor-only. Ask a super admin to grant "
                    "you the **editor** role on cedadmin."
                )


def _lead_row_summary(r: dict, bucket_id: str, today: date) -> dict:
    """Tabular summary of a single seller row inside a lead bucket.

    Columns adapt to the bucket so the most-relevant signal sits next
    to email/shop. Health / opportunity / winback scores are only
    computed when relevant to keep the table tight.
    """
    base = {
        "Email": r.get("email"),
        "Shop": r.get("shop_url"),
        "Country": r.get("country") or "—",
        "Orders": r.get("_total_orders_n", 0),
        "SKUs": r.get("_published_sku_n", 0),
    }
    if bucket_id in ("renewal_at_risk", "paid_idle", "failure_spike"):
        base["MRR ($)"] = f"{r.get('_mrr_usd', 0):.2f}"
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
    """How to rank rows WITHIN a bucket — most-actionable first."""
    if bucket_id == "renewal_at_risk":
        # Smallest days-to-expiration AND highest MRR first.
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
        # Highest MRR + most days idle first.
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

    # Column-level filters — keep it terse; full-table filters live in
    # the dataframe widget itself.
    with st.expander("Filters", expanded=True):
        col1, col2, col3 = st.columns(3)
        f_text = col1.text_input("Email / shop contains", "").strip().lower()
        f_install = col2.selectbox(
            "Installation status", options=["any", "install", "uninstall"],
        )
        f_purchase = col3.selectbox(
            "Purchase status",
            options=[
                "any", "Purchased", "Trial Expired", "License Expired",
                "Free Subscription", "Free Subscription Expire", "Not Purchase",
            ],
        )
        col4, col5, col6 = st.columns(3)
        f_country = col4.text_input("Country contains", "").strip().lower()
        f_min_orders = col5.number_input("Min total_orders", value=0, step=10)
        f_min_skus = col6.number_input("Min published_sku", value=0, step=10)

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

    # Show only the underlying CSV columns (drop the _ prefixed
    # computed fields from the table view — they live behind the
    # filters).
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
        )
    else:
        st.caption("📎 CSV export is editor-only on cedadmin.")
