"""
End-to-end tests for the fail-proof guardrail wiring in scraper.py +
scrape_validator.py.

These tests do NOT touch Playwright or the live admin panel — they exercise
the validation + persistence layer with synthetic data, which is where the
real correctness question lives. The Playwright integration is a separate
concern (live-run smoke).

Run from the repo root:
    python -m pytest tests/test_scrape_guardrail.py -v
Or standalone:
    python tests/test_scrape_guardrail.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

# Stub config so scraper.py imports cleanly without real env vars.
_cfg = types.ModuleType("config")
_cfg.APP_IDS = {}
_cfg.LOGIN_URL = ""
_cfg.USERNAME = ""
_cfg.PASSWORD = ""
_cfg.HEADLESS = True
_cfg.CREDENTIALS = {}
sys.modules.setdefault("config", _cfg)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scraper as s             # noqa: E402
import scrape_validator as sv   # noqa: E402


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------
REQUIRED_LABELS = [
    "Seller Id", "Store Url", "Username", "Email Id", "Installed On",
    "Marketplace/Platform Connected", "Action",
]


def _isolated_results_dir():
    """Swap scraper's results/ paths to a tmp dir for the test's lifetime."""
    tmp = tempfile.mkdtemp(prefix="guardrail-test-")
    os.chdir(tmp)
    s.RESULTS_DIR = Path("results")
    s.LATEST_DIR = Path("results/latest")
    s.HISTORY_DIR = Path("results/history")
    s.STAGING_DIR = Path("results/staging")
    return Path(tmp)


def _seed_previous_run(row_count: int, app: str = "shein"):
    s.LATEST_DIR.mkdir(parents=True, exist_ok=True)
    p = s.LATEST_DIR / f"{app}.csv"
    with p.open("w") as f:
        f.write("seller_id\n")
        for i in range(row_count):
            f.write(f"S{i}\n")


# -----------------------------------------------------------------------
# Tier A — validator logic
# -----------------------------------------------------------------------
def test_clean_run_is_promotable():
    _isolated_results_dir()
    trace = [sv.PageTrace(
        page_num=1, first_row_key="k1", row_count=100,
        reported_total_rows=100, reported_total_pages=1,
    )]
    rep = sv.validate_app(
        app_name="shein", kind="sellers",
        observed_grid_labels=REQUIRED_LABELS,
        pagination_trace=trace,
        scraped_row_count=100, previous_row_count=100,
        grid_cfg=sv._load_grid_columns(),
    )
    assert rep.is_promotable, rep.status
    assert rep.status == "ok"


def test_missing_required_column_blocks():
    _isolated_results_dir()
    # Drop "Seller Id" — a required column — from observed labels.
    observed = [l for l in REQUIRED_LABELS if l != "Seller Id"]
    rep = sv.validate_app(
        app_name="shein", kind="sellers",
        observed_grid_labels=observed,
        pagination_trace=[sv.PageTrace(page_num=1, first_row_key="k", row_count=100,
                                       reported_total_rows=100, reported_total_pages=1)],
        scraped_row_count=100, previous_row_count=100,
        grid_cfg=sv._load_grid_columns(),
    )
    assert rep.status == "blocked"
    assert not rep.is_promotable


def test_unexpected_column_is_pending_review_not_blocked():
    _isolated_results_dir()
    rep = sv.validate_app(
        app_name="shein", kind="sellers",
        observed_grid_labels=REQUIRED_LABELS + ["Brand New Experimental Column"],
        pagination_trace=[sv.PageTrace(page_num=1, first_row_key="k", row_count=100,
                                       reported_total_rows=100, reported_total_pages=1)],
        scraped_row_count=100, previous_row_count=100,
        grid_cfg=sv._load_grid_columns(),
    )
    assert rep.status == "pending_review"
    assert rep.is_promotable, "pending_review should still allow promote"


def test_hard_row_drop_blocks():
    _isolated_results_dir()
    rep = sv.validate_app(
        app_name="shein", kind="sellers",
        observed_grid_labels=REQUIRED_LABELS,
        pagination_trace=[sv.PageTrace(page_num=1, first_row_key="k", row_count=10,
                                       reported_total_rows=10, reported_total_pages=1)],
        scraped_row_count=10, previous_row_count=100,   # 90% drop
        grid_cfg=sv._load_grid_columns(),
    )
    assert rep.status == "blocked", rep.status


def test_pagination_inconsistency_flags():
    """Paginator reported 500 total rows but only 50 were scraped."""
    _isolated_results_dir()
    trace = [
        sv.PageTrace(page_num=1, first_row_key="k1", row_count=50,
                     reported_total_rows=500, reported_total_pages=5),
    ]
    rep = sv.validate_app(
        app_name="shein", kind="sellers",
        observed_grid_labels=REQUIRED_LABELS,
        pagination_trace=trace,
        scraped_row_count=50, previous_row_count=50,
        grid_cfg=sv._load_grid_columns(),
    )
    assert rep.status in ("blocked", "pending_review")


# -----------------------------------------------------------------------
# Tier B — persistence atomicity
# -----------------------------------------------------------------------
def test_promote_true_overwrites_latest():
    _isolated_results_dir()
    _seed_previous_run(100)
    written = s.persist_results(
        {"shein": [{"seller_id": f"S{i}"} for i in range(120)]},
        stamp="promote-test",
        promote_latest=True,
    )
    assert written["promoted"] is True
    lines = (s.LATEST_DIR / "shein.csv").read_text().strip().split("\n")
    assert len(lines) == 121  # header + 120


def test_promote_false_preserves_latest_and_stages():
    _isolated_results_dir()
    _seed_previous_run(100)
    prev_bytes = (s.LATEST_DIR / "shein.csv").read_bytes()

    written = s.persist_results(
        {"shein": [{"seller_id": "X1"}]},
        stamp="blocked-test",
        promote_latest=False,
    )
    assert written["promoted"] is False
    # latest/ is byte-for-byte untouched
    assert (s.LATEST_DIR / "shein.csv").read_bytes() == prev_bytes
    # staging/<stamp>/ has the bad data preserved for post-mortem
    assert (s.STAGING_DIR / "blocked-test" / "shein.csv").exists()
    # history/ always writes
    assert (s.HISTORY_DIR / "blocked-test" / "shein.csv").exists()


def test_load_previous_counts_reads_latest():
    _isolated_results_dir()
    _seed_previous_run(73, app="shopify_temu")
    _seed_previous_run(42, app="shopify_temu_eu")
    counts = s._load_previous_counts()
    assert counts["sellers"]["shopify_temu"] == 73
    assert counts["sellers"]["shopify_temu_eu"] == 42


def test_invalid_run_markdown_rendered():
    _isolated_results_dir()
    rep_ok = sv.validate_app(
        app_name="shein", kind="sellers",
        observed_grid_labels=REQUIRED_LABELS,
        pagination_trace=[sv.PageTrace(page_num=1, first_row_key="k", row_count=100,
                                       reported_total_rows=100, reported_total_pages=1)],
        scraped_row_count=100, previous_row_count=100,
        grid_cfg=sv._load_grid_columns(),
    )
    rep_bad = sv.validate_app(
        app_name="shopify_temu", kind="sellers",
        observed_grid_labels=REQUIRED_LABELS,
        pagination_trace=[sv.PageTrace(page_num=1, first_row_key="k", row_count=5,
                                       reported_total_rows=5, reported_total_pages=1)],
        scraped_row_count=5, previous_row_count=500,   # 99% drop
        grid_cfg=sv._load_grid_columns(),
    )
    md = sv.format_run_report([rep_ok, rep_bad], promoted=False, stamp="md-test")
    assert "blocked" in md.lower()
    assert "shein" in md
    assert "shopify_temu" in md


# -----------------------------------------------------------------------
# Standalone runner — lets `python tests/test_scrape_guardrail.py` work
# without needing pytest installed.
# -----------------------------------------------------------------------
if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {t.__name__}  — {e}")
        except Exception as e:
            failures += 1
            print(f"  ERROR {t.__name__}  — {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
