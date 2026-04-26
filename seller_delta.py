"""
seller_delta.py — compute what changed between two scrape snapshots.

The key value prop: cHAP's admin panel drops a seller's profile data
the moment they uninstall, but we keep every scrape in Supabase's
`snapshots` table. That means we can still answer "who churned AND
what were they worth" by diffing the most recent successful scrape
against a prior one.

Inputs are plain row lists (the same `data` shape the scraper writes
into results/latest/run.json), so this module is reusable from:
  - the Intelligence page's "What changed" feed
  - future daily-summary emails
  - a Slack/webhook notifier

No Streamlit imports here — pure compute.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional


def _num(val) -> int:
    if val is None:
        return 0
    try:
        s = str(val).strip()
        if not s or s.lower() in ("n/a", "na", "none", "-", "—"):
            return 0
        return int(float(s.replace(",", "")))
    except (ValueError, TypeError):
        return 0


def _plan_of(row: dict) -> str:
    return (row.get("plan") or "").strip()


def _key_by_seller(rows: Iterable[dict]) -> dict[str, dict]:
    """Index a list of seller rows by seller_id so we can cross-reference
    without repeated O(n) scans."""
    out: dict[str, dict] = {}
    for r in rows or []:
        sid = (r.get("seller_id") or "").strip()
        if sid:
            out[sid] = r
    return out


# ---------------------------------------------------------------------
# Event shape — one entry per change. The UI layer renders a timeline
# from these; downstream notifiers can filter/group by `kind`.
# ---------------------------------------------------------------------


@dataclass
class DeltaEvent:
    kind: str                # "new_install" | "churned" | "plan_upgrade"
                             # | "plan_downgrade" | "order_spike"
                             # | "failed_order_spike"
    app_id: str
    seller_id: str
    # Identity snippets — kept denormalised so the UI doesn't need to
    # cross-reference another table to render a row.
    store_url: str = ""
    email: str = ""
    username: str = ""
    # Plan context — always populated for plan_* kinds, available for
    # others so the UI can show "was on Pro" for a churn event.
    plan_before: str = ""
    plan_after: str = ""
    # Magnitude — raw numbers the UI can render as "12 → 34 (+22)".
    value_before: Optional[int] = None
    value_after: Optional[int] = None
    # Human-readable headline — the UI uses this as the primary line.
    headline: str = ""


# ---------------------------------------------------------------------
# Thresholds — exposed as constants so we can tune without touching
# the comparison logic. Keep them named + documented so downstream
# reviewers know the "why".
# ---------------------------------------------------------------------

# Order spike: at least a 50% jump AND an absolute delta ≥ 5 orders.
# Filters out 0 → 2 noise on near-empty sellers.
ORDER_SPIKE_MIN_ABS = 5
ORDER_SPIKE_MIN_PCT = 0.5

# Failed-order spike: any jump ≥ 3 flagged. Low threshold because
# failures are always worth a heads-up.
FAILED_ORDER_MIN_DELTA = 3


# ---------------------------------------------------------------------
# Core — compute events for one app.
# ---------------------------------------------------------------------


def compute_events(
    app_id: str,
    prior_rows: list[dict],
    latest_rows: list[dict],
) -> list[DeltaEvent]:
    """Return a list of DeltaEvents for the transition prior → latest.

    Never raises — broken input rows are skipped with a warning log.
    """
    events: list[DeltaEvent] = []
    try:
        prior = _key_by_seller(prior_rows)
        latest = _key_by_seller(latest_rows)

        # --- New installs: in latest, not in prior -------------------
        for sid, row in latest.items():
            if sid in prior:
                continue
            events.append(DeltaEvent(
                kind="new_install",
                app_id=app_id,
                seller_id=sid,
                store_url=row.get("store_url", "") or "",
                email=row.get("email", "") or "",
                username=row.get("username", "") or "",
                plan_after=_plan_of(row),
                headline=(
                    f"New install on {app_id}: "
                    f"{row.get('store_url') or row.get('username') or sid}"
                    + (f" · plan {_plan_of(row)}" if _plan_of(row) else "")
                ),
            ))

        # --- Churned: in prior, not in latest ------------------------
        # This is the uniquely valuable one — cHAP's admin panel has
        # already forgotten these sellers; only our snapshots remember
        # what they were worth when they were still installed.
        for sid, row in prior.items():
            if sid in latest:
                continue
            orders = _num(row.get("order_count"))
            products = _num(row.get("product_count"))
            plan = _plan_of(row)
            details = []
            if plan:
                details.append(f"plan {plan}")
            if orders:
                details.append(f"{orders} orders")
            if products:
                details.append(f"{products} products")
            events.append(DeltaEvent(
                kind="churned",
                app_id=app_id,
                seller_id=sid,
                store_url=row.get("store_url", "") or "",
                email=row.get("email", "") or "",
                username=row.get("username", "") or "",
                plan_before=plan,
                value_before=orders,
                headline=(
                    f"Churned on {app_id}: "
                    f"{row.get('store_url') or row.get('username') or sid}"
                    + (" · " + ", ".join(details) if details else "")
                ),
            ))

        # --- Retained-but-changed ------------------------------------
        for sid, now_row in latest.items():
            prev_row = prior.get(sid)
            if not prev_row:
                continue

            # Plan upgrade / downgrade (lexicographic ordering is not
            # accurate for real tier hierarchy; we just report both
            # sides and let the UI label it, since plan-tier ordering
            # is cHAP-specific).
            #
            # Skip transitions to/from empty values — those are almost
            # always Customize Grid having lost the Plan column on one
            # of the two scrapes (data quality, not a real plan change).
            # Verified 2026-04-26: a single SHEIN scrape produced 87
            # "Basic → N/A" / "Starter → N/A" events purely because the
            # latest run failed to tick the Plan Details column.
            prev_plan = _plan_of(prev_row)
            now_plan = _plan_of(now_row)
            _missing_plan = lambda p: not (p or "").strip() or (p or "").strip().lower() in {"n/a", "—", "-", "none"}
            if prev_plan != now_plan and not _missing_plan(prev_plan) and not _missing_plan(now_plan):
                # Classify upgrade vs downgrade using "paid → free" and
                # "free → paid" as the only reliable directional signal.
                # Everything else is "changed".
                from customer_intelligence import is_paid
                prev_paid = is_paid(prev_plan)
                now_paid = is_paid(now_plan)
                if not prev_paid and now_paid:
                    kind = "plan_upgrade"
                elif prev_paid and not now_paid:
                    kind = "plan_downgrade"
                else:
                    kind = "plan_change"
                events.append(DeltaEvent(
                    kind=kind,
                    app_id=app_id,
                    seller_id=sid,
                    store_url=now_row.get("store_url", "") or "",
                    email=now_row.get("email", "") or "",
                    username=now_row.get("username", "") or "",
                    plan_before=prev_plan or "—",
                    plan_after=now_plan or "—",
                    headline=(
                        f"Plan change on {app_id}: "
                        f"{now_row.get('store_url') or sid} · "
                        f"{prev_plan or '—'} → {now_plan or '—'}"
                    ),
                ))

            # Order spike — only upward jumps.
            prev_orders = _num(prev_row.get("order_count"))
            now_orders = _num(now_row.get("order_count"))
            d_orders = now_orders - prev_orders
            if d_orders >= ORDER_SPIKE_MIN_ABS and (
                prev_orders == 0 or d_orders / max(prev_orders, 1) >= ORDER_SPIKE_MIN_PCT
            ):
                events.append(DeltaEvent(
                    kind="order_spike",
                    app_id=app_id,
                    seller_id=sid,
                    store_url=now_row.get("store_url", "") or "",
                    email=now_row.get("email", "") or "",
                    username=now_row.get("username", "") or "",
                    plan_after=_plan_of(now_row),
                    value_before=prev_orders,
                    value_after=now_orders,
                    headline=(
                        f"Order spike on {app_id}: "
                        f"{now_row.get('store_url') or sid} · "
                        f"{prev_orders} → {now_orders} (+{d_orders})"
                    ),
                ))

            # Failed-order spike — any sizeable jump.
            prev_failed = _num(prev_row.get("failed_order_count"))
            now_failed = _num(now_row.get("failed_order_count"))
            d_failed = now_failed - prev_failed
            if d_failed >= FAILED_ORDER_MIN_DELTA:
                events.append(DeltaEvent(
                    kind="failed_order_spike",
                    app_id=app_id,
                    seller_id=sid,
                    store_url=now_row.get("store_url", "") or "",
                    email=now_row.get("email", "") or "",
                    username=now_row.get("username", "") or "",
                    plan_after=_plan_of(now_row),
                    value_before=prev_failed,
                    value_after=now_failed,
                    headline=(
                        f"Failures rising on {app_id}: "
                        f"{now_row.get('store_url') or sid} · "
                        f"{prev_failed} → {now_failed} failed orders "
                        f"(+{d_failed})"
                    ),
                ))
    except Exception as err:
        # Never let a malformed row kill the feed.
        logging.exception("compute_events failed for app=%s: %s", app_id, err)
    return events


def summarise(events: Iterable[DeltaEvent]) -> dict[str, int]:
    """Roll events up into a headline strip (counts per kind)."""
    counts: dict[str, int] = {}
    for e in events or []:
        counts[e.kind] = counts.get(e.kind, 0) + 1
    return counts
