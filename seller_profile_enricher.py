"""
seller_profile_enricher.py — fetch a seller's public store and ask
Claude what kind of business it is + what the sales opening is.

Why: the fields the scraper pulls from cHAP's admin panel say how a
seller USES our product (orders, products, plan). They say nothing
about what the seller actually sells or who they are — which is the
context a sales rep needs to personalise outreach. This module fills
that gap by reading the seller's own storefront.

Output (per seller):
    business_type:  "Brand" | "Reseller" | "Manufacturer" |
                    "Marketplace" | "Boutique" | "Unknown"
    categories:     list[str]  — top-level product categories
    insight:        str  — one-sentence "here's why they matter"
    opportunity:    str  — one-sentence "pitch the rep should lead with"
    cached_at:      ISO timestamp

Caching:
    Results are stored in `public.seller_profiles` (SQL migration in
    sql/003_seller_profiles.sql) keyed by (app_name, seller_id). The
    enricher checks the cache first; only refetches when the cached
    row is older than `max_age_days` or when the caller passes
    `force=True`. Keeps the Claude bill boring.

Dry-run:
    If `ANTHROPIC_API_KEY` isn't set, the enricher runs in dry-run
    mode: it logs what it WOULD do and returns a stub profile with
    business_type="Unknown" and a caption that says "AI analysis not
    configured". Same pattern as supabase_client.py so the page
    renders cleanly while setup is in progress.

How sales would use it:
    1. Admin (one-time): add ANTHROPIC_API_KEY to Streamlit secrets
       and apply sql/003_seller_profiles.sql in Supabase.
    2. Rep opens Customer Intelligence → a lead row → clicks "🔍
       Analyse business". The enricher fetches the storefront, sends
       ~4-8kb of distilled text to Claude, stores the result.
    3. Next time the same seller appears, cached result displays
       instantly. Refetch only when explicitly requested or after
       the TTL expires.

This module intentionally has zero Streamlit imports — the page
(intelligence_ui.py) wraps it in UI. Kept pure so it's testable and
reusable from background jobs later.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional


# ---------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------


@dataclass
class SellerProfile:
    """Structured result of analysing one seller's storefront."""
    app_name: str
    seller_id: str
    store_url: str
    business_type: str = "Unknown"
    categories: list[str] = field(default_factory=list)
    insight: str = ""
    opportunity: str = ""
    cached_at: str = ""
    # Diagnostic fields — populated even in dry-run so the admin can
    # tell why a profile is empty. Not shown to the rep by default.
    error: str = ""
    source: str = ""  # "claude", "cache", "dry_run", "error"

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------
# Storefront fetching — minimal HTML extraction; no JS rendering.
# ---------------------------------------------------------------------


# Skeletal set of "nothing-signal" hosts; we never hit these because
# they aren't the seller's own domain and tell us nothing about the
# seller's business.
_IGNORED_DOMAINS = {
    "myshopify.com",  # only the bare tenant, not subdomain
    "cifapps.com",
}


def _normalise_url(raw: str) -> Optional[str]:
    """Coerce the `store_url` field from the scraper into something
    requests can hit. Scraper writes variants like `mojosmusic.com`,
    `https://wearagex.com`, `store-name.myshopify.com`."""
    if not raw:
        return None
    raw = raw.strip()
    if raw in _IGNORED_DOMAINS:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if "." not in raw:
        return None
    return f"https://{raw.lstrip('/')}"


def _fetch_storefront_text(url: str, *, timeout: int = 10) -> Optional[str]:
    """GET the URL, strip to distilled text for the LLM. Caps output
    at ~8kb to keep token usage predictable. Returns None on failure.

    Uses requests (already a repo dep). Deliberately NOT headless-
    chromium: the LLM needs enough text to infer category, it doesn't
    need JS-rendered content.
    """
    try:
        import requests
    except ImportError:
        return None
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": "cHAP-seller-tracker/1.0 (analysis; contact hsrivastava@threecolts.com)",
                "Accept": "text/html,application/xhtml+xml",
            },
            allow_redirects=True,
        )
    except Exception:
        return None
    if not resp.ok:
        return None
    html = resp.text[:200_000]  # guard against pathological sizes
    return _html_to_text(html)[:8000]


_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _html_to_text(html: str) -> str:
    """Very small HTML→text step. Keeps <title>, <meta description>,
    headings, and body text. Drops scripts, styles, noscript blocks.
    Good enough for category inference."""
    # Extract <title>
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = (title_match.group(1).strip() if title_match else "")

    # Extract <meta name="description" content="...">
    desc_match = re.search(
        r'<meta[^>]*(?:name|property)=["\'](?:og:)?description["\'][^>]*content=["\']([^"\']*)',
        html, re.IGNORECASE,
    )
    desc = (desc_match.group(1).strip() if desc_match else "")

    # Strip scripts/styles, tags, collapse whitespace.
    body = _SCRIPT_STYLE_RE.sub(" ", html)
    body = _TAG_RE.sub(" ", body)
    body = _WS_RE.sub(" ", body).strip()

    return "\n".join(x for x in (
        f"TITLE: {title}" if title else "",
        f"DESC: {desc}" if desc else "",
        f"BODY: {body[:6000]}",
    ) if x)


# ---------------------------------------------------------------------
# Claude integration — dry-run safe
# ---------------------------------------------------------------------


_SYSTEM_PROMPT = """\
You are a B2B SaaS sales analyst. You will receive a short text dump
from a seller's public online storefront. Return a strict JSON object
(no prose, no markdown) with these keys:

  business_type     — one of: "Brand", "Reseller", "Manufacturer",
                      "Marketplace", "Boutique", "Unknown"
  categories        — array of up to 4 top-level product categories
                      in title case (e.g. ["Apparel", "Footwear"])
  insight           — a single sentence, ≤ 30 words, describing the
                      shop's scope, positioning, and likely operational
                      maturity
  opportunity       — a single sentence, ≤ 30 words, on which cHAP
                      marketplace integration would help them most and
                      why

Rules:
  - Only output JSON. No prose outside the object.
  - If the text is too thin to judge, use business_type="Unknown" and
    say so plainly in `insight`.
  - Don't hallucinate URLs, prices, or client names."""


def _call_claude(text: str, *, store_url: str) -> dict:
    """Send distilled storefront text to Claude, parse the JSON back.

    Returns a dict with keys business_type/categories/insight/opportunity
    OR raises RuntimeError with an actionable message. Caller decides
    whether to swallow the exception into a stub profile.
    """
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    try:
        from anthropic import Anthropic
    except ImportError as err:
        raise RuntimeError(
            "anthropic SDK not installed — add `anthropic` to "
            "requirements.txt and redeploy."
        ) from err

    client = Anthropic(api_key=key)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",  # cheap + fast; fine for classification
        max_tokens=600,
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Storefront URL: {store_url}\n\n"
                    f"Extracted text:\n---\n{text}\n---"
                ),
            }
        ],
    )
    raw = "".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    ).strip()
    # Claude sometimes wraps JSON in ```json ... ``` despite the system
    # prompt; strip that before parsing.
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as err:
        raise RuntimeError(f"Claude returned non-JSON: {raw[:200]}") from err
    return parsed


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------


def analyse_seller(
    *,
    app_name: str,
    seller_id: str,
    store_url: str,
    supabase_client=None,
    max_age_days: int = 30,
    force: bool = False,
) -> SellerProfile:
    """Return a SellerProfile for this seller.

    Cache-first: if supabase_client is passed and a recent row exists,
    returns it. Otherwise fetches the storefront, calls Claude,
    persists to Supabase, returns. Dry-run when ANTHROPIC_API_KEY is
    missing — the stub is recognisable (source="dry_run") so the UI
    can say so.

    Never raises — failures are captured in profile.error so the rep
    sees the reason inline.
    """
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    profile = SellerProfile(
        app_name=app_name,
        seller_id=seller_id,
        store_url=store_url or "",
        cached_at=now_iso,
    )

    # 1. Cache lookup.
    if supabase_client and not force:
        try:
            cached = _fetch_cached_profile(
                supabase_client, app_name=app_name, seller_id=seller_id,
                max_age_days=max_age_days,
            )
            if cached:
                cached["source"] = "cache"
                return SellerProfile(**{
                    k: v for k, v in cached.items()
                    if k in SellerProfile.__dataclass_fields__
                })
        except Exception as err:
            logging.warning(f"seller_profile cache lookup failed: {err}")

    # 2. Normalise URL + fetch.
    url = _normalise_url(store_url)
    if not url:
        profile.error = "No usable store_url on this row."
        profile.source = "error"
        return profile

    text = _fetch_storefront_text(url)
    if not text:
        profile.error = f"Couldn't fetch {url} (timeout / blocked / 4xx)."
        profile.source = "error"
        return profile

    # 3. Dry-run short-circuit.
    if not os.getenv("ANTHROPIC_API_KEY"):
        profile.business_type = "Unknown"
        profile.insight = (
            "AI analysis not configured — set ANTHROPIC_API_KEY in "
            "Streamlit secrets to enable."
        )
        profile.source = "dry_run"
        return profile

    # 4. Ask Claude.
    try:
        parsed = _call_claude(text, store_url=url)
    except Exception as err:
        profile.error = str(err)[:500]
        profile.source = "error"
        return profile

    profile.business_type = str(parsed.get("business_type", "Unknown"))
    profile.categories = list(parsed.get("categories") or [])[:4]
    profile.insight = str(parsed.get("insight", ""))[:500]
    profile.opportunity = str(parsed.get("opportunity", ""))[:500]
    profile.source = "claude"

    # 5. Persist to cache (best-effort).
    if supabase_client:
        try:
            _upsert_cached_profile(supabase_client, profile)
        except Exception as err:
            logging.warning(f"seller_profile cache write failed: {err}")

    return profile


# ---------------------------------------------------------------------
# Supabase cache helpers — thin wrappers over the client.
# The public.seller_profiles DDL lives in sql/003_seller_profiles.sql.
# ---------------------------------------------------------------------


def _fetch_cached_profile(
    client,
    *,
    app_name: str,
    seller_id: str,
    max_age_days: int,
) -> Optional[dict]:
    """Return the cached row if it exists AND is fresh enough."""
    if getattr(client, "dry_run", True):
        return None
    try:
        resp = (
            client._client.table("seller_profiles")
            .select("*")
            .eq("app_name", app_name)
            .eq("seller_id", seller_id)
            .limit(1)
            .execute()
        )
    except Exception:
        return None
    data = getattr(resp, "data", None) or []
    if not data:
        return None
    row = data[0]
    cached_at = row.get("cached_at")
    if not cached_at:
        return None
    try:
        dt = datetime.fromisoformat(cached_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if datetime.now(timezone.utc) - dt > timedelta(days=max_age_days):
        return None
    return row


def _upsert_cached_profile(client, profile: SellerProfile) -> None:
    if getattr(client, "dry_run", True):
        return
    client._client.table("seller_profiles").upsert(
        profile.to_dict(), on_conflict="app_name,seller_id",
    ).execute()
