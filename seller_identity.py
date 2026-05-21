"""Canonical seller identity layer for the cHAP fleet.

Pure functions in this module — no I/O. The DB-bound resolver lives in
`supabase_client.py::SupabaseClient.resolve_seller_identity` and calls
these normalizers + the decision helpers below.

Why pure: identity is the foundation everything KPI- and analytics-wise
will rely on. Bugs in normalization silently merge or split real
sellers, which is the worst kind of data rot. Pure functions are
trivially unit-tested (see `test_seller_identity.py`), and the rules
encoded here are the single source of truth — `006_seller_identity.sql`
just stores the result, it doesn't reimplement the matching.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# Email local-parts that get treated as "generic" — info@, admin@, etc.
# Still trusted for identity resolution (we don't have a better signal),
# but the resolver flags the seller record with a note so reviewers
# notice when a single generic email links many shops.
GENERIC_EMAIL_LOCAL_PARTS: frozenset[str] = frozenset({
    "info", "admin", "hello", "support", "contact", "sales",
    "help", "team", "office", "service", "shop", "store",
    "owner", "ceo", "founder",
})

# Gmail-family domains where dots in the local part are insignificant
# and `+tag` aliases all route to the same inbox. Normalising these
# means `john.doe+chap@gmail.com` and `johndoe@gmail.com` resolve to
# the same seller — desirable, otherwise we'd see the same person as
# two accounts.
GMAIL_DOMAINS: frozenset[str] = frozenset({
    "gmail.com", "googlemail.com",
})


def normalize_email(raw: str | None) -> str:
    """Return the canonical form of an email for identity matching.

    Rules:
      1. Trim + lowercase.
      2. Drop everything that isn't `local@domain` (no email → "").
      3. For gmail / googlemail: strip dots in local part, drop `+tag`.

    Always safe to call. Empty / malformed input → "".
    """
    if not raw or not isinstance(raw, str):
        return ""
    s = raw.strip().lower()
    if "@" not in s:
        return ""
    local, _, domain = s.rpartition("@")
    if not local or not domain:
        return ""

    if domain in GMAIL_DOMAINS:
        local = local.split("+", 1)[0]
        local = local.replace(".", "")

    return f"{local}@{domain}"


def is_generic_email(canonical_email: str) -> bool:
    """True if the local part is in GENERIC_EMAIL_LOCAL_PARTS.

    Use after `normalize_email`. The resolver uses this to attach a
    note to the seller record, not to block matching. We've discussed
    not denylisting these — losing identity is worse than the rare
    over-merge an `info@` causes.
    """
    if not canonical_email or "@" not in canonical_email:
        return False
    local = canonical_email.split("@", 1)[0]
    return local in GENERIC_EMAIL_LOCAL_PARTS


_PROTOCOL_RE = re.compile(r"^https?://", re.IGNORECASE)
_WWW_RE = re.compile(r"^www\.", re.IGNORECASE)
_SHOPIFY_SUFFIX_RE = re.compile(r"\.myshopify\.com$", re.IGNORECASE)


def normalize_shop_url(raw: str | None) -> str:
    """Return the canonical shop URL for identity matching.

    Handles the edge cases observed in cHAP scrapes:
      - The `michael` app sometimes returns space-separated multi-URL:
        `'www.relevantplay.com relevant-play.myshopify.com'`. We prefer
        the `.myshopify.com` form (stable across custom-domain swaps);
        otherwise the longest non-empty token.
      - Protocol (`https?://`), `www.`, and trailing path / query are
        stripped — they're noise, not identity.
      - `.myshopify.com` suffix is KEPT — that IS the canonical Shopify
        identifier; dropping it would collide every `foo.myshopify.com`
        with a custom-domain `foo.com`.

    Always safe to call. Empty / malformed → "".
    """
    if not raw or not isinstance(raw, str):
        return ""
    s = raw.strip().lower()
    if not s:
        return ""

    # Multi-URL field: split on any whitespace, prefer myshopify token.
    tokens = [t for t in re.split(r"\s+", s) if t]
    if len(tokens) > 1:
        myshop = [t for t in tokens if ".myshopify.com" in t]
        s = myshop[0] if myshop else max(tokens, key=len)
    else:
        s = tokens[0] if tokens else ""

    if not s:
        return ""

    s = _PROTOCOL_RE.sub("", s)
    s = _WWW_RE.sub("", s)
    # Drop trailing path / query / fragment — keep the host only.
    s = s.split("/", 1)[0]
    s = s.split("?", 1)[0]
    s = s.split("#", 1)[0]
    return s.strip(". ")


# Map cHAP's free-text `platforms` field ('Shopify Shein', 'Woocommerce',
# 'Shopify Temu', …) to a stable framework token. We keep the first
# recognised token; multi-platform sellers are handled at the install
# level (see `app_installs` future work / `sellers.platforms` raw column).
_FRAMEWORK_TOKENS: tuple[tuple[str, str], ...] = (
    ("woocommerce", "woocommerce"),
    ("prestashop", "prestashop"),
    ("bigcommerce", "bigcommerce"),
    ("magento", "magento"),
    ("opencart", "opencart"),
    ("shopify", "shopify"),
)


def extract_framework(platforms_raw: str | None) -> str:
    """'Shopify Shein' → 'shopify'; 'Woocommerce' → 'woocommerce'.

    Returns "" when no framework is recognisable. The framework lives
    on `seller_shops`, not on the install — a shop's underlying tech
    is a property of the shop, not of which cHAP app it's installed on.
    """
    if not platforms_raw or not isinstance(platforms_raw, str):
        return ""
    s = platforms_raw.strip().lower()
    if not s:
        return ""
    for needle, token in _FRAMEWORK_TOKENS:
        if needle in s:
            return token
    return ""


# Test-store detection. Two signals merged from existing logic in
# `analytics_advanced.py::_is_test_store` — see [[feedback_cedadmin_panel_isolation]]
# (test store rules apply identically across panels). Imported here so the
# identity layer's `is_test_store` flag is consistent with the dashboard's
# exclusion filter.
TEST_EMAIL_DOMAINS: frozenset[str] = frozenset({
    "threecolts.com", "cedcommerce.com",
})

TEST_EMAIL_EXACT: frozenset[str] = frozenset({
    "syedubaidhussain11@gmail.com",
})

_TEST_URL_TOKENS: tuple[str, ...] = (
    "test", "demo", "dev", "staging", "sandbox",
)


def _looks_like_test_subdomain(host: str) -> bool:
    """Detect test/demo/dev tokens as standalone segments or as suffixes
    of segments in a Shopify-style subdomain. Mirror of the existing
    analytics rule — keeps test-store classification consistent."""
    if not host:
        return False
    # Strip .myshopify.com so we look at the seller-chosen identifier.
    base = _SHOPIFY_SUFFIX_RE.sub("", host)
    for segment in re.split(r"[-_.]+", base):
        if not segment:
            continue
        if segment in _TEST_URL_TOKENS:
            return True
        for token in _TEST_URL_TOKENS:
            if len(segment) > len(token) and segment.endswith(token):
                return True
    return False


def is_test_store(*, email_canonical: str, shop_url_canonical: str) -> bool:
    """Test-store check used by the resolver. Conservative: True only
    on a positive signal, so a legit shop with missing fields is not
    accidentally hidden.

    Pass *normalized* inputs (already through `normalize_email` /
    `normalize_shop_url`)."""
    if email_canonical:
        if email_canonical in TEST_EMAIL_EXACT:
            return True
        domain = email_canonical.rsplit("@", 1)[-1]
        if any(domain == d or domain.endswith("." + d) for d in TEST_EMAIL_DOMAINS):
            return True
    if shop_url_canonical and _looks_like_test_subdomain(shop_url_canonical):
        return True
    return False


# ---------------------------------------------------------------------
# Resolver decision helpers
# ---------------------------------------------------------------------
# The actual DB resolver lives in supabase_client.py — it queries
# sellers_canonical / seller_shops, then calls these helpers to decide
# what to write. Splitting the decisions out keeps the rules testable
# without a Supabase round-trip.

@dataclass(frozen=True)
class ResolveInputs:
    """Normalized inputs to the resolver."""
    email_canonical: str
    shop_url_canonical: str
    framework: str
    source_country: str
    app_name: str
    chap_seller_id: str
    run_stamp: str
    is_test_store: bool


@dataclass(frozen=True)
class ExistingSeller:
    """Result of looking up sellers_canonical by primary_email or alias."""
    seller_key: str
    primary_email: str
    email_aliases: tuple[str, ...]


@dataclass(frozen=True)
class ExistingShop:
    """Result of looking up seller_shops by shop_url_canonical."""
    shop_id: str
    seller_key: str | None  # may be NULL during backfill races


@dataclass(frozen=True)
class ResolveDecision:
    """What the resolver should do — produced by `decide(...)`.

    The caller (supabase_client) executes these as DB writes. Keeping
    them as a tuple of explicit actions lets us log every change and
    dry-run a backfill without DB writes.
    """
    create_seller: bool                          # insert a new sellers_canonical row
    use_seller_key: str | None                   # link to this existing seller_key (when create_seller=False)
    add_email_alias: str | None                  # append this email to the resolved seller's email_aliases
    collision_blocked: bool                      # alias would conflict; do not add
    create_shop: bool                            # insert a new seller_shops row
    use_shop_id: str | None                      # link to this existing shop_id (when create_shop=False)
    relink_shop_seller: bool                     # the shop existed but its seller_key disagreed; we re-pointed it
    note: str                                    # human-readable explanation for the log


def decide(
    *,
    inputs: ResolveInputs,
    seller_by_email: ExistingSeller | None,       # match on primary_email
    seller_by_alias: ExistingSeller | None,       # match on email_aliases
    shop_existing: ExistingShop | None,
    seller_by_shop: ExistingSeller | None,        # seller currently linked to shop_existing
) -> ResolveDecision:
    """Pure decision function. Given the lookup results, decide what to
    write. The caller is responsible for running the four lookups and
    persisting the returned decision.

    The matrix:
      - shop exists, seller-by-email matches shop's seller     → reuse both, no changes
      - shop exists, no seller match anywhere                  → reuse shop; add this email as alias to shop's seller
      - shop exists, seller-by-email differs from shop's owner → collision; auto-add as alias UNLESS that email already
                                                                  belongs to a different seller (then block + log)
      - shop new, seller exists by email/alias                 → create shop linked to that seller
      - shop new, seller new                                   → create both
    """
    # ---- Resolve which seller_key to use (prefer match-by-email; then alias; then shop's owner; then create) ----
    resolved_seller: ExistingSeller | None = (
        seller_by_email or seller_by_alias or seller_by_shop
    )

    create_seller = resolved_seller is None
    use_seller_key = None if create_seller else resolved_seller.seller_key

    # ---- Alias logic ----
    add_alias: str | None = None
    collision_blocked = False
    note_parts: list[str] = []

    if shop_existing is not None and seller_by_shop is not None:
        # The shop has an existing owner. Decide whether the new email
        # needs to be recorded as an alias.
        shop_owner_email = seller_by_shop.primary_email
        shop_owner_aliases = set(seller_by_shop.email_aliases)
        new_email = inputs.email_canonical

        same_owner = (
            seller_by_email is not None
            and seller_by_email.seller_key == seller_by_shop.seller_key
        ) or (
            seller_by_alias is not None
            and seller_by_alias.seller_key == seller_by_shop.seller_key
        )

        if not same_owner and new_email and new_email != shop_owner_email \
                and new_email not in shop_owner_aliases:
            # The shop's existing owner doesn't already know about this email.
            # If the new email belongs to a *different* seller, blocking is the
            # right call — silently aliasing would create ambiguity about which
            # seller "owns" that email going forward.
            email_owned_by_different_seller = (
                (seller_by_email is not None
                 and seller_by_email.seller_key != seller_by_shop.seller_key)
                or (seller_by_alias is not None
                    and seller_by_alias.seller_key != seller_by_shop.seller_key)
            )
            if email_owned_by_different_seller:
                collision_blocked = True
                note_parts.append(
                    f"email '{new_email}' already belongs to a different "
                    f"seller; alias NOT added — manual review needed"
                )
            else:
                add_alias = new_email
                note_parts.append(
                    f"new email '{new_email}' seen on existing shop; "
                    f"auto-added as alias to seller {seller_by_shop.seller_key}"
                )
        # When same_owner: nothing to do, the email is already known.

        # The resolver uses the shop's existing seller_key (we shouldn't
        # silently re-point a shop to a different seller).
        use_seller_key = seller_by_shop.seller_key
        create_seller = False
    elif shop_existing is None and resolved_seller is None and inputs.email_canonical:
        # Brand-new seller will be created — primary_email becomes this.
        note_parts.append(f"new seller created for '{inputs.email_canonical}'")

    # ---- Shop logic ----
    if shop_existing is None:
        create_shop = True
        use_shop_id = None
        relink_shop_seller = False
        note_parts.append(
            f"new shop '{inputs.shop_url_canonical}' created"
        )
    else:
        create_shop = False
        use_shop_id = shop_existing.shop_id
        relink_shop_seller = False  # never auto-relink (data integrity)

    return ResolveDecision(
        create_seller=create_seller,
        use_seller_key=use_seller_key,
        add_email_alias=add_alias,
        collision_blocked=collision_blocked,
        create_shop=create_shop,
        use_shop_id=use_shop_id,
        relink_shop_seller=relink_shop_seller,
        note="; ".join(note_parts) if note_parts else "no change",
    )
