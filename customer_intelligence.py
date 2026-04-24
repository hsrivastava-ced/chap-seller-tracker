"""
customer_intelligence.py — insight buckets for the Sales Rep view.

Different audience than the Dashboard:
  - Dashboard (dashboard.py)          → stakeholder aggregates, trends
  - Customer Intelligence (this file) → per-seller, actionable leads

Every bucket returns a list of enriched seller dicts that sales reps
can export to CSV and work through. Each bucket has a single-sentence
definition so a rep opening the page can understand what they're
looking at without asking.

The input shape matches what scraper.py writes into
`results/latest/run.json` (and what Supabase stores in
public.snapshots.raw_data). Fields are strings — we coerce numerics
in the helpers below.

Future work:
  - Delta tracking: compare today's snapshot vs yesterday's to flag
    plan changes, order spikes, failed-order increases. Needs a
    second snapshot passed in.
  - Churn-risk scoring: combine signals (failed_order trend, no
    orders in N days, steps_completed stuck) into a single score.
  - AI-augmented notes: given a seller's profile, generate a
    one-line outreach recommendation.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Optional


# ---------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------

_NO_PLAN_VALUES = {"", "n/a", "na", "none", "free", "trial", "-", "—"}


def _num(val) -> int:
    """Best-effort int parse. 'N/A', '', None → 0."""
    if val is None:
        return 0
    s = str(val).strip()
    if not s or s.lower() in _NO_PLAN_VALUES:
        return 0
    try:
        return int(s.replace(",", ""))
    except ValueError:
        # Sometimes the scraper writes floats like "800.0" or "—".
        try:
            return int(float(s.replace(",", "")))
        except ValueError:
            return 0


def is_paid(plan: str) -> bool:
    """Heuristic: a 'plan' string that isn't in the no-plan lexicon is
    a real subscription. Matches how the Dashboard's Paid/Not-Paid
    split already treats the field."""
    return (plan or "").strip().lower() not in _NO_PLAN_VALUES


_DATE_PATTERNS = (
    "%d/%m/%Y",  # 01/04/2026 (DD/MM/YYYY — cHAP default)
    "%Y-%m-%d",  # 2026-04-01
    "%d-%m-%Y",  # 01-04-2026
)


def parse_install_date(val) -> Optional[date]:
    """Return a `date` from cHAP's DD/MM/YYYY (or ISO) formats, else None."""
    if not val:
        return None
    s = str(val).strip()
    for pat in _DATE_PATTERNS:
        try:
            return datetime.strptime(s, pat).date()
        except ValueError:
            continue
    return None


def days_since_install(val, *, today: Optional[date] = None) -> Optional[int]:
    """Whole days between `installed_on` and today. None if unparseable."""
    d = parse_install_date(val)
    if d is None:
        return None
    today = today or date.today()
    return max(0, (today - d).days)


# ---------------------------------------------------------------------
# Enrichment: add a `_insight` sub-dict to each row so the UI can
# render consistent columns regardless of which bucket we're showing.
# ---------------------------------------------------------------------


def _enrich(row: dict, *, today: Optional[date] = None) -> dict:
    """Return a shallow-copied row with an `_insight` dict summarising
    the numeric/boolean signals the buckets use + the unified fit
    score and its temperature tier. Keeps the original row unchanged
    for callers that still need raw strings.
    """
    plan = (row.get("plan") or "").strip()
    order_count = _num(row.get("order_count"))
    product_count = _num(row.get("product_count"))
    failed_orders = _num(row.get("failed_order_count"))
    steps = _num(row.get("steps_completed"))
    days = days_since_install(row.get("installed_on"), today=today)
    paid = is_paid(plan)
    score = _fit_score(
        paid=paid,
        days_since_install=days,
        product_count=product_count,
        order_count=order_count,
        failed_orders=failed_orders,
        steps_completed=steps,
    )
    out = dict(row)
    out["_insight"] = {
        "plan": plan,
        "paid": paid,
        "order_count": order_count,
        "product_count": product_count,
        "failed_order_count": failed_orders,
        "steps_completed": steps,
        "days_since_install": days,
        "fit_score": score,
        "temperature": temperature_for(score),
    }
    return out


# ---------------------------------------------------------------------
# Fit score + temperature — quantified priority so reps can sort one
# unified list instead of eyeballing across 5 buckets.
# ---------------------------------------------------------------------

def _fit_score(
    *,
    paid: bool,
    days_since_install: Optional[int],
    product_count: int,
    order_count: int,
    failed_orders: int,
    steps_completed: int,
) -> int:
    """0-100 blended score combining the same signals the buckets use.

    Two profiles: conversion (for free sellers → "worth talking to")
    and upsell (for paid sellers → "ready for a bigger plan"). Caller
    doesn't pick which one — the function reads `paid` and does the
    right thing.

    Conversion profile weights (free plan):
      days_since_install → up to 35 points (longer = more committed)
      product_count     → up to 30 points (catalog = investment)
      order_count       → up to 30 points (already earning)
      steps_completed   → up to 5 points  (setup progress)

    Upsell profile weights (paid plan):
      base for being paid → 30 points
      order_count        → up to 50 points (volume drives upsell)
      product_count      → up to 20 points
      penalty if failure_ratio > 10% → −15

    Score is clamped to [0, 100].
    """
    days = days_since_install or 0
    if not paid:
        score = 0.0
        score += min(35.0, days / 4.3)         # ~150 days caps this
        score += min(30.0, product_count / 20.0)
        score += min(30.0, order_count * 2.0)  # 15 orders → max
        score += min(5.0, steps_completed * 1.0)
    else:
        score = 30.0
        score += min(50.0, order_count / 10.0)
        score += min(20.0, product_count / 50.0)
        if order_count > 0 and failed_orders / order_count > 0.1:
            score -= 15.0
    return max(0, min(100, round(score)))


# Tier thresholds — tuned so a realistic seller distribution spreads
# across all four buckets rather than everyone landing in "Low". Mirrors
# the Hot/Warm/Cool/Low palette from the reference dashboard.
TIER_HOT_MIN = 75
TIER_WARM_MIN = 50
TIER_COOL_MIN = 25


def temperature_for(score: int) -> str:
    """Map a fit score to one of Hot / Warm / Cool / Low."""
    if score >= TIER_HOT_MIN:
        return "Hot"
    if score >= TIER_WARM_MIN:
        return "Warm"
    if score >= TIER_COOL_MIN:
        return "Cool"
    return "Low"


def temperature_emoji(tier: str) -> str:
    return {"Hot": "🔥", "Warm": "☀", "Cool": "❄", "Low": "💤"}.get(tier, "")


def tier_counts(sellers: Iterable[dict], *, today: Optional[date] = None) -> dict[str, int]:
    """Total sellers in each temperature tier — powers the summary
    counter strip at the top of the page."""
    counts = {"Hot": 0, "Warm": 0, "Cool": 0, "Low": 0}
    for r in sellers or []:
        enriched = _enrich(r, today=today)
        counts[enriched["_insight"]["temperature"]] += 1
    return counts


# ---------------------------------------------------------------------
# Buckets — each returns a list of enriched rows.
#
# Every function takes a list of RAW rows (from run.json). They call
# `_enrich` internally so callers don't need to think about it.
# ---------------------------------------------------------------------


@dataclass
class Bucket:
    """UI-ready packaging of one insight: the rows that match + the
    definition the rep sees above the table + the best "what to do"
    headline per row. Kept as a dataclass so downstream the UI can
    pick exactly which keys to show."""
    id: str
    title: str
    definition: str
    rows: list[dict]

    @property
    def count(self) -> int:
        return len(self.rows)


def priority_leads(
    sellers: Iterable[dict],
    *,
    min_install_days: int = 60,
    today: Optional[date] = None,
) -> Bucket:
    """Installed long enough to know if they'll ever pay + has shown
    intent (any products OR any orders OR >=1 step completed) but
    still on a free / no-plan. These are the highest-ROI outreach
    targets: they've invested setup time, aren't dormant, but haven't
    converted. Sales should prioritise this list every morning."""
    out = []
    for row in sellers or []:
        enriched = _enrich(row, today=today)
        ins = enriched["_insight"]
        if ins["paid"]:
            continue
        if ins["days_since_install"] is None:
            continue
        if ins["days_since_install"] < min_install_days:
            continue
        has_intent = (
            ins["order_count"] > 0
            or ins["product_count"] > 0
            or ins["steps_completed"] >= 1
        )
        if has_intent:
            out.append(enriched)
    # Newest-shown-intent first: order by install date descending so
    # reps don't re-drill the same stale leads every week.
    out.sort(key=lambda r: r["_insight"]["days_since_install"], reverse=True)
    return Bucket(
        id="priority_leads",
        title="🎯 Priority leads",
        definition=(
            f"Installed ≥{min_install_days} days ago, shows intent "
            "(products, orders, or setup steps), still on a free/no "
            "plan. These are the best conversion bets — they've done "
            "the work, just haven't paid."
        ),
        rows=out,
    )


def free_plan_with_orders(
    sellers: Iterable[dict], *, today: Optional[date] = None,
) -> Bucket:
    """Getting orders but hasn't paid. Closer to converting than
    priority_leads because they already have real business flowing."""
    out = []
    for row in sellers or []:
        enriched = _enrich(row, today=today)
        ins = enriched["_insight"]
        if ins["paid"]:
            continue
        if ins["order_count"] > 0:
            out.append(enriched)
    out.sort(key=lambda r: r["_insight"]["order_count"], reverse=True)
    return Bucket(
        id="free_plan_with_orders",
        title="💸 Free plan, earning orders",
        definition=(
            "Sellers on a free / no plan who already have ≥1 order — "
            "they're using the product, revenue is flowing. Quickest "
            "path to a paid conversion."
        ),
        rows=out,
    )


def high_volume_free(
    sellers: Iterable[dict], *, min_products: int = 500,
    today: Optional[date] = None,
) -> Bucket:
    """No plan / free plan but imported a meaningful catalog. The
    product count implies they intend to use the platform seriously."""
    out = []
    for row in sellers or []:
        enriched = _enrich(row, today=today)
        ins = enriched["_insight"]
        if ins["paid"]:
            continue
        if ins["product_count"] >= min_products:
            out.append(enriched)
    out.sort(key=lambda r: r["_insight"]["product_count"], reverse=True)
    return Bucket(
        id="high_volume_free",
        title="📦 Heavy catalog, no plan",
        definition=(
            f"Imported ≥{min_products:,} products but still on a free "
            "plan. Infrastructure investment without revenue — usually "
            "means they're ready to scale but haven't committed to a "
            "tier yet."
        ),
        rows=out,
    )


def upsell_candidates(
    sellers: Iterable[dict],
    *,
    min_orders_for_upsell: int = 200,
    today: Optional[date] = None,
) -> Bucket:
    """On a paid plan AND order volume is high. Signal: they've
    outgrown their current tier. Pair with plan-tier metadata to
    suggest the next step."""
    out = []
    for row in sellers or []:
        enriched = _enrich(row, today=today)
        ins = enriched["_insight"]
        if not ins["paid"]:
            continue
        if ins["order_count"] >= min_orders_for_upsell:
            out.append(enriched)
    out.sort(key=lambda r: r["_insight"]["order_count"], reverse=True)
    return Bucket(
        id="upsell_candidates",
        title="📈 Paid & growing fast",
        definition=(
            f"Already on a paid plan and processing ≥{min_orders_for_upsell:,} "
            "orders — candidates for a tier upgrade. Review plan + "
            "usage to suggest the next SKU."
        ),
        rows=out,
    )


def churn_risks(
    sellers: Iterable[dict],
    *,
    min_failed: int = 10,
    min_failure_ratio: float = 0.1,
    today: Optional[date] = None,
) -> Bucket:
    """Failed-order volume is high either in absolute terms or as a
    fraction of total orders. Without day-over-day history we can't
    flag RISING failures yet — that's a follow-up once we wire this
    page to Supabase snapshots. For now we flag the active failure
    count as a leading indicator."""
    out = []
    for row in sellers or []:
        enriched = _enrich(row, today=today)
        ins = enriched["_insight"]
        failed = ins["failed_order_count"]
        orders = ins["order_count"]
        ratio = (failed / orders) if orders else 0.0
        if failed >= min_failed or (orders and ratio >= min_failure_ratio):
            enriched["_insight"]["failure_ratio"] = round(ratio, 3)
            out.append(enriched)
    out.sort(
        key=lambda r: r["_insight"]["failed_order_count"], reverse=True,
    )
    return Bucket(
        id="churn_risks",
        title="⚠️ Churn risk — failing orders",
        definition=(
            f"Sellers with ≥{min_failed} failed orders, OR a failure "
            f"ratio ≥{int(min_failure_ratio*100)}% of their total. "
            "A sustained failure rate predicts churn — worth reaching "
            "out before they uninstall."
        ),
        rows=out,
    )


# ---------------------------------------------------------------------
# Top-level orchestrator — the page calls this once per app.
# ---------------------------------------------------------------------


def buckets_for(
    sellers: list[dict],
    *,
    today: Optional[date] = None,
) -> list[Bucket]:
    """Compute every bucket for a single app's seller list. Order in
    the returned list is the order the UI will render them — most-
    actionable first."""
    try:
        return [
            priority_leads(sellers, today=today),
            free_plan_with_orders(sellers, today=today),
            high_volume_free(sellers, today=today),
            upsell_candidates(sellers, today=today),
            churn_risks(sellers, today=today),
        ]
    except Exception as err:
        # Never let a single bad row kill the whole page.
        logging.exception("buckets_for failed: %s", err)
        return []
