"""
Unit tests for normalize.py.

These are the cheapest regression net for the pipeline: every seller/
uninstall row in every scrape flows through these functions, and a
silent regression here produces phantom churn/new-install numbers in
the dashboard. Keep this suite fast and deterministic — no I/O, no
network, no Playwright.
"""

from __future__ import annotations

import pytest

from normalize import (
    normalize_date,
    normalize_run_data,
    normalize_seller_row,
    normalize_store_url,
    normalize_uninstall_row,
    normalize_whitespace,
)


# ---------------------------------------------------------------------
# normalize_whitespace
# ---------------------------------------------------------------------

class TestNormalizeWhitespace:
    def test_collapses_runs(self):
        assert normalize_whitespace("Shopify    Temu") == "Shopify Temu"

    def test_strips_ends(self):
        assert normalize_whitespace("  hello  ") == "hello"

    def test_handles_tabs_and_newlines(self):
        assert normalize_whitespace("a\tb\nc") == "a b c"

    def test_handles_nbsp(self):
        # U+00A0 NBSP — admin panel occasionally renders these instead
        # of ascii spaces, which used to confuse seller-id equality.
        assert normalize_whitespace("Shopify\u00a0Temu") == "Shopify Temu"

    def test_none_becomes_empty_string(self):
        assert normalize_whitespace(None) == ""

    def test_non_string_passes_through(self):
        assert normalize_whitespace(42) == 42
        assert normalize_whitespace(["a", "b"]) == ["a", "b"]

    def test_empty_string(self):
        assert normalize_whitespace("") == ""

    def test_idempotent(self):
        once = normalize_whitespace("  Shopify   Temu  ")
        twice = normalize_whitespace(once)
        assert once == twice == "Shopify Temu"


# ---------------------------------------------------------------------
# normalize_store_url
# ---------------------------------------------------------------------

class TestNormalizeStoreUrl:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            # Scheme stripping
            ("https://example.myshopify.com", "example.myshopify.com"),
            ("http://example.myshopify.com", "example.myshopify.com"),
            ("HTTPS://Example.MyShopify.com", "example.myshopify.com"),
            # Trailing slash
            ("example.myshopify.com/", "example.myshopify.com"),
            ("https://example.myshopify.com/", "example.myshopify.com"),
            # www. prefix
            ("www.example.myshopify.com", "example.myshopify.com"),
            ("https://www.example.myshopify.com/", "example.myshopify.com"),
            # Host casing normalised, path casing preserved
            ("Example.Myshopify.com/Admin", "example.myshopify.com/Admin"),
            # Already-canonical stays canonical (idempotent)
            ("example.myshopify.com", "example.myshopify.com"),
            # Empty / None
            ("", ""),
            ("   ", ""),
        ],
    )
    def test_canonical_forms(self, raw, expected):
        assert normalize_store_url(raw) == expected

    def test_none_becomes_empty(self):
        assert normalize_store_url(None) == ""

    def test_non_string_passes_through(self):
        assert normalize_store_url(123) == 123

    def test_idempotent(self):
        once = normalize_store_url("HTTPS://www.Example.MyShopify.com/")
        twice = normalize_store_url(once)
        assert once == twice == "example.myshopify.com"

    def test_two_urls_that_differ_only_in_formatting_hash_equal(self):
        # This is the real-world scenario we built the normaliser for:
        # the previous run wrote one form, the current run captured
        # another, and without this guarantee the dashboard would show
        # phantom churn.
        a = normalize_store_url("Example.myshopify.com/")
        b = normalize_store_url("https://example.myshopify.com")
        assert a == b


# ---------------------------------------------------------------------
# normalize_date
# ---------------------------------------------------------------------

class TestNormalizeDate:
    def test_uk_locale_date_only(self):
        # Admin panel default — 03/04/2026 is 3-April in UK locale.
        assert normalize_date("03/04/2026") == "2026-04-03"

    def test_uk_locale_date_with_time(self):
        assert normalize_date("03/04/2026 14:30:00") == "2026-04-03T14:30:00"

    def test_iso_timestamp_round_trip(self):
        assert normalize_date("2026-04-18 19:38:19") == "2026-04-18T19:38:19"

    def test_iso_date_passes_through(self):
        assert normalize_date("2026-04-18") == "2026-04-18"

    def test_empty_and_none(self):
        assert normalize_date("") == ""
        assert normalize_date("   ") == ""
        assert normalize_date(None) == ""

    def test_unparseable_keeps_original(self):
        # We MUST NOT lose information; garbage-in stays intact so
        # downstream debugging can still see what came off the scraper.
        assert normalize_date("not-a-date") == "not-a-date"

    def test_prefer_iso_date_drops_time(self):
        assert (
            normalize_date("2026-04-18 19:38:19", prefer_iso_date=True)
            == "2026-04-18"
        )

    def test_midnight_collapses_to_date_only_by_default(self):
        # If the only time info is 00:00:00 we assume it was a date
        # cell rendered with a zero time — emit date-only.
        assert normalize_date("2026-04-18 00:00:00") == "2026-04-18"

    def test_idempotent(self):
        once = normalize_date("03/04/2026")
        twice = normalize_date(once)
        assert once == twice == "2026-04-03"

    def test_dash_separated_uk_date(self):
        assert normalize_date("03-04-2026") == "2026-04-03"

    def test_non_string_passes_through(self):
        assert normalize_date(42) == 42


# ---------------------------------------------------------------------
# normalize_seller_row
# ---------------------------------------------------------------------

class TestNormalizeSellerRow:
    def test_lowercases_email_and_preserves_other_fields(self):
        row = {
            "seller_id": "123",
            "email": "SELLER@Example.COM",
            "store_url": "https://Example.MyShopify.com/",
            "platforms": "Shopify  Temu",
            "installed_on": "03/04/2026",
        }
        out = normalize_seller_row(row)
        assert out["seller_id"] == "123"          # untouched
        assert out["email"] == "seller@example.com"
        assert out["store_url"] == "example.myshopify.com"
        assert out["platforms"] == "Shopify Temu"
        assert out["installed_on"] == "2026-04-03"

    def test_does_not_mutate_input(self):
        row = {"email": "A@B.COM", "store_url": "https://X.com/"}
        original = dict(row)
        normalize_seller_row(row)
        assert row == original

    def test_tolerates_missing_fields(self):
        # Not every seller row has every field; the scraper sometimes
        # omits keys rather than writing empty strings.
        out = normalize_seller_row({"seller_id": "9"})
        assert out == {"seller_id": "9"}

    def test_empty_dict_passes_through(self):
        assert normalize_seller_row({}) == {}

    def test_none_passes_through(self):
        # Defensive — upstream shouldn't hand us None, but some older
        # run.json files may.
        assert normalize_seller_row(None) is None

    def test_idempotent(self):
        row = {
            "email": "A@B.COM",
            "store_url": "https://www.Example.MyShopify.com/",
            "platforms": " Shopify  Temu ",
            "installed_on": "03/04/2026",
        }
        once = normalize_seller_row(row)
        twice = normalize_seller_row(once)
        assert once == twice


# ---------------------------------------------------------------------
# normalize_uninstall_row
# ---------------------------------------------------------------------

class TestNormalizeUninstallRow:
    def test_normalises_expected_fields(self):
        row = {
            "seller_id": "123",
            "email": "Seller@Example.com",
            "platform": "Shopify  Temu",
            "uninstalled_on": "2026-04-18 19:38:19",
        }
        out = normalize_uninstall_row(row)
        assert out["email"] == "seller@example.com"
        assert out["platform"] == "Shopify Temu"
        assert out["uninstalled_on"] == "2026-04-18T19:38:19"

    def test_does_not_mutate_input(self):
        row = {"email": "A@B.COM"}
        original = dict(row)
        normalize_uninstall_row(row)
        assert row == original


# ---------------------------------------------------------------------
# normalize_run_data (top-level helper)
# ---------------------------------------------------------------------

class TestNormalizeRunData:
    def test_handles_empty_inputs(self):
        s, u = normalize_run_data(None, None)
        assert s == {}
        assert u == {}
        s, u = normalize_run_data({}, {})
        assert s == {}
        assert u == {}

    def test_preserves_app_keys(self):
        sellers = {"shopify_temu": [{"email": "A@B.COM"}]}
        unins = {"shein": [{"email": "X@Y.COM"}]}
        s_out, u_out = normalize_run_data(sellers, unins)
        assert set(s_out) == {"shopify_temu"}
        assert set(u_out) == {"shein"}
        assert s_out["shopify_temu"][0]["email"] == "a@b.com"
        assert u_out["shein"][0]["email"] == "x@y.com"

    def test_full_payload_shape(self):
        # Realistic mini-payload matching the scraper's output shape.
        sellers = {
            "shopify_temu": [
                {
                    "seller_id": "1",
                    "email": "ONE@Example.COM",
                    "store_url": "https://Shop1.myshopify.com/",
                    "platforms": "Shopify  Temu",
                    "installed_on": "03/04/2026",
                },
                {
                    "seller_id": "2",
                    "email": "two@example.com",
                    "store_url": "shop2.myshopify.com",
                    "platforms": "Shopify Temu",
                    "installed_on": "2026-04-03",
                },
            ],
            "shein": [],
        }
        unins = {
            "shein": [
                {
                    "seller_id": "9",
                    "email": " HELLO@World.com ",
                    "platform": "Shein",
                    "uninstalled_on": "2026-04-18 09:10:11",
                },
            ],
        }
        s_out, u_out = normalize_run_data(sellers, unins)
        # Both sellers in shopify_temu normalise to the same shape so
        # the "two runs different formatting" scenario collapses.
        r1, r2 = s_out["shopify_temu"]
        assert r1["store_url"] == "shop1.myshopify.com"
        assert r1["installed_on"] == "2026-04-03"
        assert r2["store_url"] == "shop2.myshopify.com"
        assert r2["installed_on"] == "2026-04-03"
        # Empty app preserved as empty list.
        assert s_out["shein"] == []
        # Uninstall normalisation.
        assert u_out["shein"][0]["email"] == "hello@world.com"
        assert u_out["shein"][0]["uninstalled_on"] == "2026-04-18T09:10:11"

    def test_does_not_mutate_inputs(self):
        sellers = {"a": [{"email": "A@B.COM"}]}
        unins = {"b": [{"email": "C@D.COM"}]}
        sellers_before = {"a": [dict(sellers["a"][0])]}
        unins_before = {"b": [dict(unins["b"][0])]}
        normalize_run_data(sellers, unins)
        assert sellers == sellers_before
        assert unins == unins_before
