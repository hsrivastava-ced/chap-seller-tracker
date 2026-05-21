"""Unit tests for `seller_identity.py`.

Run: `python3 test_seller_identity.py`. No pytest dependency — uses
plain `assert` so it fits the project's tooling. Exits non-zero on
failure with a readable trace.

These tests exercise the cases observed in real cHAP scrapes:
  - Gmail dot/+tag normalization (test set: `john.doe+chap@gmail.com`)
  - The `michael` panel's multi-URL field with whitespace separator
  - Shopify subdomains with test/demo/dev tokens
  - Generic local-parts (`info@…`, `admin@…`) — trusted but flagged
  - The decide(...) matrix: brand-new, returning, collision, conflict
"""
from __future__ import annotations

import sys
import traceback

from seller_identity import (
    ExistingSeller,
    ExistingShop,
    ResolveInputs,
    decide,
    extract_framework,
    is_generic_email,
    is_test_store,
    normalize_email,
    normalize_shop_url,
)


def _expect(label: str, got, want):
    if got != want:
        raise AssertionError(f"{label}: got {got!r}, want {want!r}")


def test_normalize_email_basics():
    _expect("plain", normalize_email("Foo@Bar.com"), "foo@bar.com")
    _expect("trim", normalize_email("  ALICE@EXAMPLE.COM  "), "alice@example.com")
    _expect("empty", normalize_email(""), "")
    _expect("none", normalize_email(None), "")
    _expect("no-at", normalize_email("not-an-email"), "")
    _expect("non-str", normalize_email(123), "")


def test_normalize_email_gmail():
    _expect("gmail dots", normalize_email("john.doe@gmail.com"), "johndoe@gmail.com")
    _expect("gmail +tag", normalize_email("johndoe+chap@gmail.com"), "johndoe@gmail.com")
    _expect("gmail dots+tag", normalize_email("j.o.h.n.doe+temu@gmail.com"), "johndoe@gmail.com")
    _expect("googlemail", normalize_email("john.doe@googlemail.com"), "johndoe@googlemail.com")
    # Non-gmail domains: dots in local part are preserved (significant elsewhere)
    _expect("non-gmail dots", normalize_email("john.doe@example.com"), "john.doe@example.com")


def test_is_generic_email():
    assert is_generic_email("info@example.com")
    assert is_generic_email("admin@shop.com")
    assert not is_generic_email("alice@example.com")
    assert not is_generic_email("")
    assert not is_generic_email("malformed")


def test_normalize_shop_url_basics():
    _expect("plain", normalize_shop_url("foo.myshopify.com"), "foo.myshopify.com")
    _expect("https", normalize_shop_url("https://foo.myshopify.com"), "foo.myshopify.com")
    _expect("http+www", normalize_shop_url("http://www.foo.com"), "foo.com")
    _expect("trailing slash", normalize_shop_url("foo.com/"), "foo.com")
    _expect("path", normalize_shop_url("foo.com/admin/orders"), "foo.com")
    _expect("query", normalize_shop_url("foo.com?ref=temu"), "foo.com")
    _expect("uppercase", normalize_shop_url("FOO.MYSHOPIFY.COM"), "foo.myshopify.com")
    _expect("none", normalize_shop_url(None), "")
    _expect("empty", normalize_shop_url(""), "")


def test_normalize_shop_url_multi_url():
    # The `michael` panel sometimes returns space-separated multi-URL.
    # Prefer the myshopify form.
    _expect(
        "michael multi",
        normalize_shop_url("www.relevantplay.com relevant-play.myshopify.com"),
        "relevant-play.myshopify.com",
    )
    _expect(
        "no myshopify, longest wins",
        normalize_shop_url("foo.com longerstore.io"),
        "longerstore.io",
    )


def test_extract_framework():
    _expect("shopify shein", extract_framework("Shopify Shein"), "shopify")
    _expect("shopify temu", extract_framework("Shopify Temu"), "shopify")
    _expect("woocommerce", extract_framework("Woocommerce"), "woocommerce")
    _expect("prestashop", extract_framework("PrestaShop"), "prestashop")
    _expect("empty", extract_framework(""), "")
    _expect("none", extract_framework(None), "")
    _expect("unknown", extract_framework("FancyCart"), "")


def test_is_test_store():
    # Internal domains
    assert is_test_store(email_canonical="alice@threecolts.com", shop_url_canonical="foo.com")
    assert is_test_store(email_canonical="bob@cedcommerce.com", shop_url_canonical="bar.myshopify.com")
    # Explicit allowlist
    assert is_test_store(
        email_canonical="syedubaidhussain11@gmail.com",
        shop_url_canonical="anything.com",
    )
    # Test-flagged subdomain
    assert is_test_store(email_canonical="real@gmail.com", shop_url_canonical="shein-test-x.myshopify.com")
    assert is_test_store(email_canonical="real@gmail.com", shop_url_canonical="sheindemo.myshopify.com")
    assert is_test_store(email_canonical="real@gmail.com", shop_url_canonical="alfaparf-staging.myshopify.com")
    # NOT test: real email, real-looking domain
    assert not is_test_store(email_canonical="alice@example.com", shop_url_canonical="alicesshop.myshopify.com")
    # Avoid false positives on real words like 'development', 'demography', 'qatar'
    assert not is_test_store(email_canonical="alice@example.com", shop_url_canonical="development.com")
    assert not is_test_store(email_canonical="alice@example.com", shop_url_canonical="demography.com")


# ---------------------------------------------------------------------
# decide() matrix
# ---------------------------------------------------------------------

def _inputs(**kw) -> ResolveInputs:
    defaults = dict(
        email_canonical="alice@example.com",
        shop_url_canonical="alice.myshopify.com",
        framework="shopify",
        source_country="US",
        app_name="shein",
        chap_seller_id="abc123",
        run_stamp="2026-05-21_10-00-00Z",
        is_test_store=False,
    )
    defaults.update(kw)
    return ResolveInputs(**defaults)


def test_decide_brand_new():
    """First time we see this seller and this shop."""
    d = decide(
        inputs=_inputs(),
        seller_by_email=None,
        seller_by_alias=None,
        shop_existing=None,
        seller_by_shop=None,
    )
    assert d.create_seller is True
    assert d.create_shop is True
    assert d.use_seller_key is None
    assert d.use_shop_id is None
    assert d.add_email_alias is None
    assert d.collision_blocked is False


def test_decide_returning_known_seller_known_shop():
    """Both already exist and the email matches the shop's owner — no-op."""
    alice = ExistingSeller(seller_key="SK-ALICE", primary_email="alice@example.com", email_aliases=())
    shop = ExistingShop(shop_id="SH-ALICE", seller_key="SK-ALICE")
    d = decide(
        inputs=_inputs(),
        seller_by_email=alice,
        seller_by_alias=None,
        shop_existing=shop,
        seller_by_shop=alice,
    )
    assert d.create_seller is False
    assert d.create_shop is False
    assert d.use_seller_key == "SK-ALICE"
    assert d.use_shop_id == "SH-ALICE"
    assert d.add_email_alias is None
    assert d.collision_blocked is False


def test_decide_new_shop_under_returning_seller():
    """Alice already exists; opens a new storefront on another app."""
    alice = ExistingSeller(seller_key="SK-ALICE", primary_email="alice@example.com", email_aliases=())
    d = decide(
        inputs=_inputs(shop_url_canonical="newshop.myshopify.com", app_name="shopify_temu"),
        seller_by_email=alice,
        seller_by_alias=None,
        shop_existing=None,
        seller_by_shop=None,
    )
    assert d.create_seller is False
    assert d.use_seller_key == "SK-ALICE"
    assert d.create_shop is True
    assert d.use_shop_id is None


def test_decide_email_change_on_existing_shop_auto_alias():
    """Same shop returns with a new email belonging to no other seller.
    Per the user's rule: auto-add as alias to the shop's owner + log."""
    alice = ExistingSeller(seller_key="SK-ALICE", primary_email="alice@example.com", email_aliases=())
    shop = ExistingShop(shop_id="SH-ALICE", seller_key="SK-ALICE")
    d = decide(
        inputs=_inputs(email_canonical="newemail@example.com"),
        seller_by_email=None,    # not previously seen
        seller_by_alias=None,
        shop_existing=shop,
        seller_by_shop=alice,
    )
    assert d.create_seller is False
    assert d.use_seller_key == "SK-ALICE"      # shop's owner unchanged
    assert d.add_email_alias == "newemail@example.com"
    assert d.collision_blocked is False
    assert d.create_shop is False
    assert "auto-added as alias" in d.note


def test_decide_collision_blocked_when_email_owned_elsewhere():
    """Shop returns with an email that already belongs to a DIFFERENT
    seller. Don't auto-alias — flag for review."""
    alice = ExistingSeller(seller_key="SK-ALICE", primary_email="alice@example.com", email_aliases=())
    bob = ExistingSeller(seller_key="SK-BOB", primary_email="bob@example.com", email_aliases=())
    shop = ExistingShop(shop_id="SH-ALICE", seller_key="SK-ALICE")
    d = decide(
        inputs=_inputs(email_canonical="bob@example.com"),
        seller_by_email=bob,        # bob is already a different seller
        seller_by_alias=None,
        shop_existing=shop,
        seller_by_shop=alice,
    )
    assert d.use_seller_key == "SK-ALICE"      # shop stays linked to alice
    assert d.add_email_alias is None
    assert d.collision_blocked is True
    assert "manual review" in d.note


def test_decide_alias_already_known_no_op():
    """The new email is already in the existing seller's alias list — no change."""
    alice = ExistingSeller(
        seller_key="SK-ALICE",
        primary_email="alice@example.com",
        email_aliases=("alice2@example.com",),
    )
    shop = ExistingShop(shop_id="SH-ALICE", seller_key="SK-ALICE")
    d = decide(
        inputs=_inputs(email_canonical="alice2@example.com"),
        seller_by_email=None,
        seller_by_alias=alice,    # found via alias match
        shop_existing=shop,
        seller_by_shop=alice,
    )
    assert d.add_email_alias is None
    assert d.collision_blocked is False


# ---------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------

def _all_tests():
    g = globals()
    return [(name, fn) for name, fn in g.items()
            if name.startswith("test_") and callable(fn)]


def main() -> int:
    failures = 0
    tests = _all_tests()
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception:
            failures += 1
            print(f"  ✗ {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
