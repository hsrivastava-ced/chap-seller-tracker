"""
Offline tests for the Task #80 manual-edit guard primitives.

These tests exercise `SupabaseClient` in dry-run mode (no network, no
supabase-py required) and the pure `_prepare_seller_row` helper. Live
behaviour of the `upsert_sellers_with_guard` RPC is covered by the SQL
itself (sql/002_manual_edits.sql) and the end-to-end smoke in the plan.

Run from repo root:
    python tests/test_manual_edit_guard.py
"""
from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

# Stub config so supabase_client.py imports cleanly without real env vars.
# Set every attr any sibling test (e.g. test_scrape_guardrail) might need —
# pytest collection runs all test modules into the same interpreter, so
# `setdefault` here would otherwise win the race and starve the other test.
_cfg = types.ModuleType("config")
_cfg.SUPABASE_URL = ""
_cfg.SUPABASE_KEY = ""
_cfg.APP_IDS = {}
_cfg.LOGIN_URL = ""
_cfg.USERNAME = ""
_cfg.PASSWORD = ""
_cfg.HEADLESS = True
_cfg.CREDENTIALS = {}
sys.modules.setdefault("config", _cfg)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import supabase_client as sc  # noqa: E402


# ---------------------------------------------------------------------
# _prepare_seller_row — pure function, no client needed
# ---------------------------------------------------------------------
def test_prepare_seller_row_splits_canonical_and_extra():
    row = {
        "seller_id": "s_1",
        "store_url": "https://example.com",
        "email": "a@b.c",
        "username": "alice",
        "platforms": "shopify",
        "installed_on": "2026-04-24",
        "plan": "Pro",
        "unknown_col": "hello",
        "another_extra": 42,
    }
    out = sc.SupabaseClient._prepare_seller_row("shein", row)
    assert out["app_name"] == "shein"
    assert out["seller_id"] == "s_1"
    assert out["plan"] == "Pro"
    assert out["extra_fields"] == {"unknown_col": "hello", "another_extra": 42}
    # canonical fields must NOT leak into extra_fields
    assert "seller_id" not in out["extra_fields"]


def test_prepare_seller_row_strips_caller_provided_app_name_and_run_stamp():
    # A scraper row must never override app_name / run_stamp — those come
    # from the call-site, not the data.
    row = {
        "seller_id": "s_2",
        "app_name": "malicious",
        "run_stamp": "malicious_stamp",
        "email": "x@y.z",
    }
    out = sc.SupabaseClient._prepare_seller_row("shein", row)
    assert out["app_name"] == "shein"
    assert "run_stamp" not in out
    # Those keys are also NOT silently forwarded into extra_fields.
    assert out.get("extra_fields") is None or "run_stamp" not in out["extra_fields"]


def test_prepare_seller_row_no_extras_means_no_extra_fields_key():
    row = {"seller_id": "s_3", "email": "c@d.e"}
    out = sc.SupabaseClient._prepare_seller_row("shein", row)
    assert "extra_fields" not in out


# ---------------------------------------------------------------------
# apply_manual_edit — validation + dry-run
# ---------------------------------------------------------------------
def test_apply_manual_edit_rejects_unknown_field():
    client = sc.SupabaseClient(url="", key="", dry_run=True)
    try:
        client.apply_manual_edit(
            app_name="shein",
            seller_id="s_1",
            field="not_a_real_column",
            new_value="x",
            editor_email="hsrivastava@threecolts.com",
        )
    except ValueError as e:
        assert "canonical" in str(e)
        return
    raise AssertionError("apply_manual_edit should reject unknown field")


def test_apply_manual_edit_dry_run_noop(caplog=None):
    client = sc.SupabaseClient(url="", key="", dry_run=True)
    with _capture_logs() as logs:
        n = client.apply_manual_edit(
            app_name="shein",
            seller_id="s_1",
            field="plan",
            new_value="MANUAL",
            editor_email="hsrivastava@threecolts.com",
            old_value="Free",
            reason="typo",
        )
    assert n == 0
    assert any("dry-run" in m for m in logs), "expected dry-run log line"


# ---------------------------------------------------------------------
# upsert_sellers — dry-run
# ---------------------------------------------------------------------
def test_upsert_sellers_dry_run_noop():
    client = sc.SupabaseClient(url="", key="", dry_run=True)
    with _capture_logs() as logs:
        n = client.upsert_sellers(
            app_name="shein",
            rows=[{"seller_id": "s_1", "email": "a@b.c"}],
            run_stamp="2026-04-24_00-00-00Z",
        )
    assert n == 0
    assert any("dry-run" in m and "upsert" in m for m in logs)


def test_upsert_sellers_empty_rows_returns_zero():
    client = sc.SupabaseClient(url="", key="", dry_run=True)
    assert client.upsert_sellers(app_name="shein", rows=[], run_stamp="x") == 0


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------
class _capture_logs:
    """Capture INFO+ log messages from the root logger for assertion.

    Mirrors the no-pytest style used by test_scrape_guardrail.py — no
    pytest fixtures, just a context manager.
    """
    def __enter__(self):
        self._records: list[str] = []
        self._handler = logging.Handler()
        self._handler.emit = lambda rec: self._records.append(rec.getMessage())
        logging.getLogger().addHandler(self._handler)
        logging.getLogger().setLevel(logging.INFO)
        return self._records

    def __exit__(self, *exc):
        logging.getLogger().removeHandler(self._handler)


# ---------------------------------------------------------------------
# Runner (no-pytest, matches tests/test_scrape_guardrail.py)
# ---------------------------------------------------------------------
if __name__ == "__main__":
    import inspect
    tests = [
        (n, f) for n, f in inspect.getmembers(sys.modules[__name__], inspect.isfunction)
        if n.startswith("test_")
    ]
    ok = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS", name)
            ok += 1
        except Exception as e:
            print("FAIL", name, e)
    print(f"{ok}/{len(tests)}")
    sys.exit(0 if ok == len(tests) else 1)
