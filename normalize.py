"""
Data normalisation for the cHAP Seller Tracker.

The scraper hands us what the admin panel rendered: dates in DD/MM/YYYY
(UK locale), store URLs that sometimes have scheme / trailing slash /
mixed case, emails with stray whitespace, etc. Downstream consumers
(analytics, Supabase, dashboard) are all happier with a canonical form.

Keep this module pure (no I/O, no Playwright, no network). That way
unit tests and the pipeline can normalise data offline, and we don't
have to pay the scraping cost just to test a date-format edge case.

Two guiding rules:
  1. **Never lose information.** If a value can't be parsed into a
     cleaner form we keep the original rather than dropping it.
  2. **Idempotent.** Calling normalise twice yields the same result as
     once; safe to apply on a re-run without drift.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any

# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------

# Seller row date-like columns. `installed_on` comes in DD/MM/YYYY from
# the admin panel. `last_sync` is usually empty but occasionally a
# timestamp string in various formats — we normalise it best-effort.
_SELLER_DATE_FIELDS = ("installed_on", "last_sync")

# Uninstall row date-like columns. `uninstalled_on` comes in
# "YYYY-MM-DD HH:MM:SS" already but we still run it through so the
# output shape is consistent.
_UNINSTALL_DATE_FIELDS = ("uninstalled_on",)

# String fields that should have whitespace collapsed. These are the
# ones that can realistically get double-spaces from the admin panel's
# label concatenation (e.g. "Shopify  Temu").
_SELLER_STRING_FIELDS = (
    "username",
    "email",
    "platforms",
    "source_country",
    "plan",
    "app_type",
)
_UNINSTALL_STRING_FIELDS = (
    "username",
    "email",
    "platform",
    "shops_raw",
)

# Fields holding URLs we want canonicalised.
_URL_FIELDS = ("store_url",)

# Fields that should be lowercased after whitespace collapse. Emails
# are case-insensitive per RFC 5321 §2.4 and case-mixing is a
# well-known cause of duplicate-seller false positives.
_LOWERCASE_FIELDS = ("email",)


# ---------------------------------------------------------------------
# Primitive normalisers
# ---------------------------------------------------------------------

def normalize_whitespace(value: Any) -> Any:
    """Collapse runs of whitespace to a single space and strip the ends.

    Non-strings pass through unchanged so this is safe to call on
    whole row dicts via map-style helpers. Returns empty string for
    None (matching the scraper's own empty-cell convention)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    # `split()` on no-arg collapses all runs of any whitespace (spaces,
    # tabs, NBSPs rendered as U+00A0 — which the admin panel occasionally
    # emits). Joining with a single space then strip handles leading /
    # trailing whitespace for free.
    return " ".join(value.split()).strip()


def normalize_store_url(value: Any) -> Any:
    """Canonicalise a store URL so `Example.MyShopify.com/` and
    `example.myshopify.com` hash to the same string.

    - Strips whitespace.
    - Drops scheme (http://, https://) — the admin panel is
      inconsistent about this; keeping scheme would split the same
      store into two identity keys.
    - Lowercases the host portion only (path is case-sensitive on some
      hosting).
    - Strips a single trailing slash on the full URL.
    - Drops `www.` prefix (admin panel occasionally includes it, the
      Shopify backend does not).

    Non-string values pass through untouched."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s:
        return ""

    # Drop scheme, if any — re.sub is case-insensitive via (?i).
    s = re.sub(r"(?i)^https?://", "", s)

    # Split host + path. Host is everything up to the first '/'.
    if "/" in s:
        host, _, rest = s.partition("/")
        path = "/" + rest
    else:
        host, path = s, ""

    host = host.lower()
    if host.startswith("www."):
        host = host[4:]

    recombined = host + path
    # One trailing slash is noise (shopify backends treat them the same).
    if recombined.endswith("/") and recombined.count("/") == 1 and not path:
        recombined = recombined[:-1]
    elif recombined.endswith("/"):
        recombined = recombined[:-1]
    return recombined


# Accepted input formats for `normalize_date`. Ordered most-specific
# first so ISO timestamps win over ambiguous numeric forms.
_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%dT%H:%M:%S.%f%z",   # ISO with micro + tz
    "%Y-%m-%dT%H:%M:%S%z",      # ISO with tz
    "%Y-%m-%dT%H:%M:%S",        # ISO naive
    "%Y-%m-%d %H:%M:%S",        # uninstalled_on shape
    "%Y-%m-%d",                 # ISO date only
    "%d/%m/%Y %H:%M:%S",        # UK with time
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",                 # UK date only  ← admin panel default
    "%d-%m-%Y",
    "%m/%d/%Y",                 # US — last-resort fallback; see note
)


def normalize_date(value: Any, *, prefer_iso_date: bool = False) -> Any:
    """Parse a date-like string into a canonical ISO form.

    Returns:
      - "" for empty / None / unparseable
      - "YYYY-MM-DD" when the source carried no time component, OR
        when prefer_iso_date=True (downstream consumers that only care
        about the calendar date set this).
      - "YYYY-MM-DDTHH:MM:SS" otherwise (no TZ suffix — the admin panel
        doesn't expose one; we don't invent one).

    We prefer DD/MM/YYYY over MM/DD/YYYY because the cHAP admin panel
    is UK-localised. This means an ambiguous "03/04/2026" parses as
    3-April, not 4-March. The MM/DD/YYYY entry in _DATE_FORMATS is a
    last-resort for values that fail everything else (e.g. "13/25/2025"
    obviously isn't DD/MM; rarely hit in practice).
    """
    if value is None or isinstance(value, (date, datetime)):
        if isinstance(value, datetime):
            return value.isoformat(timespec="seconds").replace("+00:00", "")
        if isinstance(value, date):
            return value.isoformat()
        return ""
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s:
        return ""

    parsed: datetime | None = None
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        # Last try: fromisoformat handles a few niche variants strptime
        # doesn't (e.g. +00:00 offset without the colon variant).
        try:
            parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            logging.debug(f"normalize_date: could not parse {s!r}; keeping original")
            return s

    if prefer_iso_date or (
        parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0
        and parsed.microsecond == 0
    ):
        return parsed.date().isoformat()

    # No TZ suffix — admin panel values are local-time ambiguous.
    out = parsed.replace(tzinfo=None).isoformat(timespec="seconds")
    return out


# ---------------------------------------------------------------------
# Row-level normalisers
# ---------------------------------------------------------------------

def normalize_seller_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict with scraper seller fields canonicalised.

    Does NOT mutate the input — important because the same dict is
    usually shared between on-disk run.json (already written) and the
    analytics layer (which is about to consume the normalised form).
    """
    if not row:
        return row
    out = dict(row)

    for k in _SELLER_STRING_FIELDS:
        if k in out:
            out[k] = normalize_whitespace(out[k])
    for k in _URL_FIELDS:
        if k in out:
            out[k] = normalize_store_url(out[k])
    for k in _LOWERCASE_FIELDS:
        v = out.get(k)
        if isinstance(v, str):
            out[k] = v.lower()
    for k in _SELLER_DATE_FIELDS:
        if k in out:
            # installed_on should always collapse to date-only; last_sync
            # preserves time resolution if present.
            prefer_date = (k == "installed_on")
            out[k] = normalize_date(out[k], prefer_iso_date=prefer_date)
    return out


def normalize_uninstall_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict with scraper uninstall fields canonicalised."""
    if not row:
        return row
    out = dict(row)

    for k in _UNINSTALL_STRING_FIELDS:
        if k in out:
            out[k] = normalize_whitespace(out[k])
    for k in _LOWERCASE_FIELDS:
        v = out.get(k)
        if isinstance(v, str):
            out[k] = v.lower()
    for k in _UNINSTALL_DATE_FIELDS:
        if k in out:
            out[k] = normalize_date(out[k])
    return out


# ---------------------------------------------------------------------
# Top-level helper for pipeline.py
# ---------------------------------------------------------------------

def normalize_run_data(
    sellers_by_app: dict[str, list[dict]] | None,
    uninstalls_by_app: dict[str, list[dict]] | None,
) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Apply row-level normalisation across a full scrape payload.

    Returns fresh dicts / lists — input is not mutated. Use this right
    before analytics so that deltas are computed on canonical data
    (otherwise "Example.myshopify.com" from the previous run and
    "example.myshopify.com/" from the current run would look like two
    different sellers, and we'd over-count churn/new_installs).
    """
    sellers_out: dict[str, list[dict]] = {}
    for app, rows in (sellers_by_app or {}).items():
        sellers_out[app] = [normalize_seller_row(r) for r in (rows or [])]

    unins_out: dict[str, list[dict]] = {}
    for app, rows in (uninstalls_by_app or {}).items():
        unins_out[app] = [normalize_uninstall_row(r) for r in (rows or [])]

    return sellers_out, unins_out
