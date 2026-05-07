"""
cedadmin_analytics.py — pure data layer for the CedCommerce admin
panel scrape. Lives next to scraper output, not next to cHAP's
analytics module — the two are intentionally isolated.

What's in here:
  - parse_plan(plan_str)           → (label, price_usd, period_months)
  - parse_date(s)                  → date | None
  - normalize_row(row)              → dict with computed numeric fields
                                       added (price_monthly, lifespan_days,
                                       days_since_login, etc.)
  - bucket_for_lead(row, today)    → SQL bucket id or None
  - LEAD_BUCKETS                   → ordered list of (id, label, hint)
  - mrr_breakdown(rows)            → dict of MRR per plan tier + total
  - plan_movement_series(rows)     → time series of new / churn / etc.
  - cohort_table(rows)              → install-month cohorts × conversion %
  - score_health(row, today)       → 0-100 composite (low = at risk)
  - score_opportunity(row, today)  → 0-100 (high = upsell candidate)
  - score_winback(row, today)      → 0-100 for License Expired sellers

Pure functions only. UI lives in cedadmin_ui.py.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Optional


# ---------------------------------------------------------------------
# Plan parsing
# ---------------------------------------------------------------------
# Order matters: longer/more-specific labels first so "Lite" doesn't
# match before "Subscription Lite".
_PLAN_TIER_HINTS: tuple[tuple[str, str], ...] = (
    ("enterprise plus",  "Enterprise Plus"),
    ("enterprise",       "Enterprise"),
    ("standard",         "Standard"),
    ("basic",            "Basic"),
    ("starter",          "Starter"),
    ("premium",          "Premium"),
    ("pro",              "Pro"),
    ("plus",             "Plus"),
    ("lite",             "Lite"),
    ("custom",           "Custom"),
    ("combo",            "Combo"),
    ("trial",            "Trial"),
    ("free",             "Free"),
)

# Period detection from common phrasings. Returns months.
_PERIOD_HINTS: tuple[tuple[str, int], ...] = (
    ("yearly",                      12),
    ("year subscription",           12),
    ("year",                        12),    # bare "1 Year ..."
    ("annual",                      12),
    ("half-yearly",                  6),
    ("half yearly",                  6),
    ("6 months",                     6),
    ("6 month",                      6),
    ("quarterly",                    3),
    ("quaterly",                     3),    # the panel actually misspells it
    ("3 months",                     3),
    ("3 month",                      3),
    ("9 months",                     9),
    ("9 month",                      9),
    ("monthly",                      1),
    ("month",                        1),
)

# Pricing lookup for unpriced labels we KNOW exist on the panel but
# don't carry an explicit price string. Operator-editable in the UI
# later; for now reasonable approximations from the priced rows.
# Keyed by lowercased + collapsed label. Values: monthly-equivalent USD.
_LABEL_PRICE_FALLBACK: dict[str, float] = {
    "monthly":              25.0,
    "yearly":              200.0 / 12,    # ~$16.67/mo
    "half-yearly":         100.0 / 6,
    "quaterly":             60.0 / 3,
    "quarterly":            60.0 / 3,
    "pro":                  99.0,
    "combo":                50.0,
    "recurring":            25.0,
    "1 year subscription plan":  150.0 / 12,
    "6 months subscription plan": 90.0 / 6,
    "3 months subscription plan": 60.0 / 3,
    "9 month":             100.0 / 9,
}


@dataclass(frozen=True)
class ParsedPlan:
    raw: str
    label: str               # e.g. "Lite", "Basic", "Standard", "Custom", "" if unknown
    price_usd: Optional[float]   # absolute amount on the plan string
    period_months: int       # 1, 3, 6, 12, ... 0 means "unknown"
    monthly_equivalent: float    # 0 if unknown

    @property
    def is_paid(self) -> bool:
        return self.monthly_equivalent > 0


def parse_plan(plan_str: str) -> ParsedPlan:
    """Best-effort parser. Returns price=None / period_months=0 when
    nothing maps; caller decides how to treat unknown plans.
    """
    raw = (plan_str or "").strip()
    if not raw or raw.lower() in {"(not set)", "n/a", "none", "-"}:
        return ParsedPlan(raw=raw, label="", price_usd=None,
                          period_months=0, monthly_equivalent=0.0)

    lower = raw.lower()
    # Tier label — first hit wins.
    label = ""
    for needle, pretty in _PLAN_TIER_HINTS:
        if needle in lower:
            label = pretty
            break
    # Period.
    months = 0
    for needle, n in _PERIOD_HINTS:
        if needle in lower:
            months = n
            break
    # Explicit price.
    price = None
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", raw)
    if m:
        try:
            price = float(m.group(1).replace(",", ""))
        except ValueError:
            price = None

    # Monthly-equivalent.
    if price is not None and months > 0:
        monthly = price / months
    elif price is not None:
        # Price but no period — assume monthly (conservative for MRR).
        monthly = price
    else:
        # Fall back to label lookup.
        monthly = float(_LABEL_PRICE_FALLBACK.get(lower, 0.0))

    # Fall back to period-as-label when no tier word matched. Most
    # CedCommerce plan strings on Walmart are just "Monthly" / "Yearly"
    # / "Quaterly" with no explicit tier — better to bucket them by
    # cadence than dump everything into "Unknown tier".
    if not label:
        if months == 12:
            label = "Yearly"
        elif months == 6:
            label = "Half-Yearly"
        elif months == 3:
            label = "Quarterly"
        elif months == 1:
            label = "Monthly"
        elif months == 9:
            label = "9 Month"

    return ParsedPlan(
        raw=raw, label=label, price_usd=price,
        period_months=months, monthly_equivalent=round(monthly, 2),
    )


# ---------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------
_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
)


def parse_date(s: str) -> Optional[date]:
    raw = (s or "").strip()
    if not raw or raw.lower() in {"(not set)", "n/a", "-", "0000-00-00"}:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------
# Row normalisation — adds computed fields callers reuse downstream.
# ---------------------------------------------------------------------
def normalize_row(row: dict, *, today: Optional[date] = None) -> dict:
    today = today or date.today()
    out = dict(row)

    plan = parse_plan(row.get("current_subscribed_plan", ""))
    out["_plan_label"] = plan.label
    out["_plan_period_months"] = plan.period_months
    out["_plan_price_usd"] = plan.price_usd
    out["_mrr_usd"] = plan.monthly_equivalent

    install_d = parse_date(row.get("installation_date", ""))
    uninstall_d = parse_date(row.get("uninstalltion_date", ""))
    payment_d = parse_date(row.get("payment_date", ""))
    expiration_d = parse_date(row.get("expiration_date", ""))
    last_login_d = parse_date(row.get("last_login_in_app", ""))

    out["_installed_date"] = install_d
    out["_uninstalled_date"] = uninstall_d
    out["_payment_date"] = payment_d
    out["_expiration_date"] = expiration_d
    out["_last_login_date"] = last_login_d

    out["_lifespan_days"] = (
        (uninstall_d or today) - install_d
    ).days if install_d else None

    out["_days_since_install"] = (today - install_d).days if install_d else None
    out["_days_since_login"] = (today - last_login_d).days if last_login_d else None
    out["_days_to_expiration"] = (
        (expiration_d - today).days if expiration_d else None
    )
    out["_days_since_payment"] = (
        (today - payment_d).days if payment_d else None
    )
    if install_d and payment_d and payment_d >= install_d:
        out["_days_to_first_payment"] = (payment_d - install_d).days
    else:
        out["_days_to_first_payment"] = None

    # Numeric coercions (the panel renders ints as strings).
    for k in ("total_orders", "success_orders", "failed_orders",
              "total_skus", "published_sku", "staged_sku"):
        v = (row.get(k) or "").strip()
        try:
            out[f"_{k}_n"] = int(v) if v else 0
        except ValueError:
            out[f"_{k}_n"] = 0

    out["_failure_rate"] = (
        out["_failed_orders_n"] / out["_total_orders_n"]
        if out["_total_orders_n"] > 0 else 0.0
    )

    # Multi-app cross-sell signal.
    other = (row.get("other_oldapps") or "").strip()
    out["_other_oldapps_count"] = (
        len([x for x in re.split(r"[,|]", other) if x.strip()])
        if other and other != "(not set)" else 0
    )

    # Plan-history depth.
    history = (row.get("all_plans_subscribed") or "").strip()
    out["_plan_history_count"] = (
        len([p for p in history.split("|") if p.strip()])
        if history and history != "(not set)" else 0
    )

    return out


# ---------------------------------------------------------------------
# SQL lead buckets — what the support team sorts the world by.
# Order: most-actionable first.
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class LeadBucket:
    id: str
    tier: str          # "Hot" | "Warm" | "Cool"
    label: str
    hint: str          # one-line "why this matters"


LEAD_BUCKETS: tuple[LeadBucket, ...] = (
    LeadBucket(
        id="renewal_at_risk",
        tier="Hot",
        label="Renewal at risk (≤14 days)",
        hint="Currently paying, expiration date inside 14 days. "
             "Schedule the renewal call this week.",
    ),
    LeadBucket(
        id="trial_conversion",
        tier="Hot",
        label="Trial — high activity",
        hint="In trial, real order/SKU activity. Likely to convert "
             "with a nudge before trial expires.",
    ),
    LeadBucket(
        id="upgrade_ready",
        tier="Hot",
        label="Free seller — upgrade ready",
        hint="On Free, has shipped real orders + sizable catalogue. "
             "The classic SQL pattern.",
    ),
    LeadBucket(
        id="winback_high_value",
        tier="Hot",
        label="License expired — winback",
        hint="Was paying, now lapsed. High historical activity = "
             "high winback probability.",
    ),
    LeadBucket(
        id="cross_sell_oldapps",
        tier="Warm",
        label="Cross-sell — uses other CedCommerce apps",
        hint="Has entries in `other_oldapps`. Already trusts the brand.",
    ),
    LeadBucket(
        id="paid_idle",
        tier="Warm",
        label="Paid but idle",
        hint="Currently paying but no login >30 days. Pre-emptive "
             "save call before churn fires.",
    ),
    LeadBucket(
        id="failure_spike",
        tier="Warm",
        label="High order-failure rate",
        hint="Failure rate >20% on a paying account. Support touch "
             "to prevent integration-driven churn.",
    ),
    LeadBucket(
        id="stuck_onboarding",
        tier="Cool",
        label="Stuck in onboarding",
        hint="Installed >7 days ago, onboarding_status not COMPLETE. "
             "Support, not sales.",
    ),
    LeadBucket(
        id="reinstall_committed",
        tier="Cool",
        label="Reinstalled after uninstalling",
        hint="Came back after leaving. Committed user — convert with "
             "a tailored offer.",
    ),
)


def bucket_for_lead(row: dict, today: Optional[date] = None) -> Optional[str]:
    """Classify one normalized row into a lead bucket id, or None.
    Pass `row` through `normalize_row()` first.

    A row only fits ONE bucket — the highest-priority match wins so
    the support team doesn't see the same seller in 4 lists.
    """
    today = today or date.today()
    install_status = (row.get("installation_status") or "").strip().lower()
    purchase_status = (row.get("purchase_status") or "").strip()
    uninstalled = row.get("_uninstalled_date") is not None

    is_paying = purchase_status == "Purchased"
    is_trial_active = purchase_status == "Trial Expired" and install_status == "install"
    is_free = purchase_status in ("Free Subscription", "Free Subscription Expire")
    is_lapsed = purchase_status in ("License Expired", "Free Subscription Expire", "Trial Expired")

    orders = row.get("_total_orders_n", 0)
    skus = row.get("_published_sku_n", 0)
    days_since_login = row.get("_days_since_login")
    days_to_exp = row.get("_days_to_expiration")
    failure_rate = row.get("_failure_rate", 0.0)
    days_since_install = row.get("_days_since_install")
    onboarding = (row.get("onboarding_status") or "").strip().upper()

    # Hot: renewal at risk
    if (
        is_paying and install_status == "install"
        and days_to_exp is not None and 0 <= days_to_exp <= 14
    ):
        return "renewal_at_risk"

    # Hot: trial conversion (in trial, has activity)
    if is_trial_active and (orders >= 1 or skus >= 5):
        return "trial_conversion"

    # Hot: free + upgrade-ready
    if is_free and install_status == "install" and (orders >= 10 or skus >= 50):
        return "upgrade_ready"

    # Hot: winback (lapsed paid with strong history)
    if (
        purchase_status == "License Expired"
        and (orders >= 50 or row.get("_plan_history_count", 0) >= 2)
    ):
        return "winback_high_value"

    # Warm: paid + idle
    if (
        is_paying and install_status == "install"
        and days_since_login is not None and days_since_login >= 30
    ):
        return "paid_idle"

    # Warm: paid + failure spike
    if (
        is_paying and install_status == "install"
        and failure_rate >= 0.20 and orders >= 10
    ):
        return "failure_spike"

    # Warm: cross-sell signal
    if (
        install_status == "install"
        and row.get("_other_oldapps_count", 0) >= 1
        and not is_paying        # prefer non-paid cross-sell candidates
    ):
        return "cross_sell_oldapps"

    # Cool: reinstall (currently install but with a prior uninstall date)
    if install_status == "install" and uninstalled:
        return "reinstall_committed"

    # Cool: stuck onboarding — tightened. Only flag the FRESHLY
    # stuck (7-60 day window) so we don't drown the bucket in years
    # of legacy installs that never finished. >60d treats it as
    # cold, not actionable.
    if (
        install_status == "install"
        and days_since_install is not None and 7 <= days_since_install <= 60
        and onboarding and onboarding not in ("COMPLETE", "")
    ):
        return "stuck_onboarding"

    return None


# ---------------------------------------------------------------------
# Aggregations the dashboard renders.
# ---------------------------------------------------------------------
def mrr_breakdown(normalized_rows: Iterable[dict]) -> dict:
    """Sum monthly revenue per plan tier; only currently-installed +
    Purchased sellers contribute. Returns:
        {
          "by_tier": {"Lite": 12000, "Basic": ...},
          "total_mrr": 45678.0,
          "active_paid_count": 1027,
          "rows_with_unknown_price": 412,
        }
    """
    by_tier: dict[str, float] = defaultdict(float)
    active_paid = 0
    unknown_price = 0
    for r in normalized_rows:
        if (r.get("installation_status") or "").lower() != "install":
            continue
        if (r.get("purchase_status") or "") != "Purchased":
            continue
        active_paid += 1
        mrr = r.get("_mrr_usd", 0.0) or 0.0
        if mrr <= 0:
            unknown_price += 1
            continue
        tier = r.get("_plan_label") or "Unknown tier"
        by_tier[tier] += mrr
    total = sum(by_tier.values())
    return {
        "by_tier": dict(by_tier),
        "total_mrr": round(total, 2),
        "annual_run_rate": round(total * 12, 2),
        "active_paid_count": active_paid,
        "rows_with_unknown_price": unknown_price,
    }


def install_movement_series(normalized_rows: Iterable[dict]) -> dict:
    """Per-month new installs and uninstalls, returned as ordered lists
    aligned by month (YYYY-MM). Useful for a stacked-bar chart.
    """
    new_per_month: Counter[str] = Counter()
    churn_per_month: Counter[str] = Counter()
    for r in normalized_rows:
        d = r.get("_installed_date")
        if d:
            new_per_month[d.strftime("%Y-%m")] += 1
        d2 = r.get("_uninstalled_date")
        if d2:
            churn_per_month[d2.strftime("%Y-%m")] += 1
    months = sorted(set(new_per_month) | set(churn_per_month))
    return {
        "months": months,
        "new": [new_per_month.get(m, 0) for m in months],
        "churn": [churn_per_month.get(m, 0) for m in months],
        "net": [new_per_month.get(m, 0) - churn_per_month.get(m, 0) for m in months],
    }


def country_distribution(normalized_rows: Iterable[dict]) -> list[tuple[str, int, int]]:
    """[(country, install_count, paid_count), ...] sorted desc by install."""
    install: Counter[str] = Counter()
    paid: Counter[str] = Counter()
    for r in normalized_rows:
        if (r.get("installation_status") or "").lower() != "install":
            continue
        country = (r.get("country") or "").strip() or "(unknown)"
        install[country] += 1
        if (r.get("purchase_status") or "") == "Purchased":
            paid[country] += 1
    return sorted(
        [(c, install[c], paid[c]) for c in install],
        key=lambda x: -x[1],
    )


def cohort_table(normalized_rows: Iterable[dict]) -> list[dict]:
    """Install-month cohorts × paid conversion %. One row per month."""
    cohorts: dict[str, list[dict]] = defaultdict(list)
    for r in normalized_rows:
        d = r.get("_installed_date")
        if not d:
            continue
        cohorts[d.strftime("%Y-%m")].append(r)
    out = []
    for month in sorted(cohorts):
        rows = cohorts[month]
        installs = len(rows)
        paid_now = sum(
            1 for r in rows
            if (r.get("purchase_status") or "") == "Purchased"
            and (r.get("installation_status") or "").lower() == "install"
        )
        ever_paid = sum(
            1 for r in rows
            if r.get("_payment_date") is not None
        )
        out.append({
            "cohort": month,
            "installs": installs,
            "ever_paid": ever_paid,
            "ever_paid_pct": round(100 * ever_paid / installs, 1) if installs else 0,
            "still_paid_now": paid_now,
            "still_paid_pct": round(100 * paid_now / installs, 1) if installs else 0,
        })
    return out


# ---------------------------------------------------------------------
# Per-seller composite scores (0-100). Higher number = stronger
# signal in the named direction.
# ---------------------------------------------------------------------
def score_health(row: dict, today: Optional[date] = None) -> int:
    """For currently-paying sellers. Lower = more at-risk.

    Components (weighted):
      - Login recency  (0..40)
      - Failure rate   (0..30 inverse — lower fail = higher score)
      - Onboarding     (0..15)
      - Renewal status (0..15 — closer to expiry = lower)
    """
    today = today or date.today()
    days_login = row.get("_days_since_login")
    failure = row.get("_failure_rate", 0.0)
    onboarding = (row.get("onboarding_status") or "").upper() == "COMPLETE"
    days_to_exp = row.get("_days_to_expiration")

    s = 0.0
    if days_login is None:
        s += 5
    elif days_login <= 7:
        s += 40
    elif days_login <= 30:
        s += 25
    elif days_login <= 60:
        s += 12
    else:
        s += 0

    s += max(0.0, 30.0 * (1.0 - min(failure, 1.0)))

    s += 15 if onboarding else 5

    if days_to_exp is None:
        s += 10
    elif days_to_exp >= 60:
        s += 15
    elif days_to_exp >= 30:
        s += 10
    elif days_to_exp >= 14:
        s += 5
    else:
        s += 0

    return int(round(min(100.0, s)))


def score_opportunity(row: dict, today: Optional[date] = None) -> int:
    """For Free / Trial sellers. Higher = better upsell candidate."""
    orders = row.get("_total_orders_n", 0)
    skus = row.get("_published_sku_n", 0)
    days_since_install = row.get("_days_since_install") or 0

    s = 0.0
    s += min(40.0, math.log1p(orders) * 8)            # 0 → 40 around 250 orders
    s += min(30.0, math.log1p(skus) * 5)              # 0 → 30 around 100 skus
    if 30 <= days_since_install <= 365:
        s += 15
    elif days_since_install >= 7:
        s += 8
    if (row.get("_days_since_login") or 999) <= 14:
        s += 15
    return int(round(min(100.0, s)))


def score_winback(row: dict, today: Optional[date] = None) -> int:
    """For License Expired. Higher = more worth a winback call."""
    if (row.get("purchase_status") or "") != "License Expired":
        return 0
    today = today or date.today()
    plan_history = row.get("_plan_history_count", 0)
    orders = row.get("_total_orders_n", 0)
    exp = row.get("_expiration_date")
    days_lapsed = (today - exp).days if exp else 999

    s = 0.0
    s += min(35.0, plan_history * 7)
    s += min(35.0, math.log1p(orders) * 6)
    if days_lapsed <= 30:
        s += 30
    elif days_lapsed <= 90:
        s += 22
    elif days_lapsed <= 180:
        s += 15
    elif days_lapsed <= 365:
        s += 8
    return int(round(min(100.0, s)))
