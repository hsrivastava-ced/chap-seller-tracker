"""
Thin Supabase wrapper for the cHAP Seller Tracker.

Design:
- One client constructed from SUPABASE_URL + SUPABASE_KEY in .env.
- All row inserts return the number of rows written, or 0 + a warning if
  the key is too weak (anon/publishable) to bypass RLS.
- All reads return plain lists of dicts — no ORM, no ActiveRecord magic.
- If SUPABASE_URL or SUPABASE_KEY are missing the client enters "dry-run"
  mode: every method logs what it WOULD push/fetch but returns safe empty
  results. This lets us develop and test pipeline logic in the sandbox
  without needing network access.

Why dry-run instead of raising: the sandbox we develop in can't reach
cifapps.com *or* Supabase, so the scraper already runs on Hrithik's local
machine. We want the pipeline module to be loadable everywhere — failures
should surface only when someone actually tries to push without creds.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterable

try:
    from supabase import Client, create_client  # type: ignore
except ImportError:  # pragma: no cover — optional dep
    Client = None       # type: ignore
    create_client = None  # type: ignore

from config import SUPABASE_KEY, SUPABASE_URL


def _is_publishable_anon_key(key: str | None) -> bool:
    """Rough heuristic: the new-format publishable / anon keys start with
    `sb_publishable_` or the old-format JWT has `"role":"anon"` inside.
    We only check the prefix here — we never decode the JWT, since the key
    is a secret. A false negative (service_role key that looks anon) is
    harmless; we log the warning regardless and inserts will either work
    or fail loudly.
    """
    if not key:
        return False
    return key.startswith("sb_publishable_")


class SupabaseClient:
    """Lazy-initialised client. Dry-run mode when creds missing or the
    supabase package isn't installed."""

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        *,
        dry_run: bool | None = None,
    ):
        self.url = url or SUPABASE_URL
        self.key = key or SUPABASE_KEY
        self._dry_run = dry_run
        self._client: Client | None = None

        if self._dry_run is None:
            self._dry_run = not (self.url and self.key and create_client)

        if self._dry_run:
            reasons = []
            if not self.url:
                reasons.append("SUPABASE_URL missing")
            if not self.key:
                reasons.append("SUPABASE_KEY missing")
            if create_client is None:
                reasons.append("supabase-py not installed")
            logging.info(
                "🧪 SupabaseClient in dry-run mode "
                f"({', '.join(reasons) or 'forced'}). Inserts will be logged but not sent."
            )
            return

        assert create_client is not None  # narrows for type-checkers
        if _is_publishable_anon_key(self.key):
            logging.warning(
                "⚠️  SUPABASE_KEY looks like a publishable/anon key. Inserts "
                "will hit RLS unless you've added write policies or swapped "
                "to a service_role key. Reads should still work."
            )
        try:
            self._client = create_client(self.url, self.key)
        except Exception:
            logging.exception(
                "Failed to construct Supabase client; falling back to dry-run."
            )
            self._dry_run = True

    # ---- internal --------------------------------------------------------

    def _table(self, name: str):
        if self._dry_run or self._client is None:
            return None
        return self._client.table(name)

    def _insert(self, table_name: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        if self._dry_run or self._client is None:
            logging.info(
                f"🧪 [dry-run] would insert {len(rows)} row(s) into "
                f"public.{table_name}"
            )
            return 0
        try:
            resp = self._client.table(table_name).insert(rows).execute()
            data = getattr(resp, "data", None) or []
            return len(data)
        except Exception as err:
            logging.error(f"Supabase insert into {table_name} failed: {err}")
            return 0

    # ---- snapshots -------------------------------------------------------

    def push_snapshot(
        self,
        app_name: str,
        kind: str,
        rows: list[dict],
        *,
        run_stamp: str,
        status: str = "success",
        notes: str | None = None,
    ) -> int:
        """Insert ONE row into public.snapshots whose raw_data jsonb is the
        entire scraper payload for (app_name, kind). Returns rows written.
        """
        assert kind in ("sellers", "uninstalls"), f"Unexpected kind: {kind}"
        payload = [
            {
                "run_stamp": run_stamp,
                "app_name": app_name,
                "kind": kind,
                "row_count": len(rows),
                "raw_data": rows,
                "status": status,
                "notes": notes,
            }
        ]
        return self._insert("snapshots", payload)

    def fetch_latest_snapshots(
        self,
        *,
        kind: str,
        app_name: str | None = None,
        limit: int = 2,
    ) -> list[dict]:
        """Fetch the `limit` most recent snapshot rows for (kind) — and
        optionally filtered by app_name. Returns rows ordered newest-first.
        Used by analytics.compute_deltas to find "current" and "previous".
        """
        if self._dry_run or self._client is None:
            logging.info(
                f"🧪 [dry-run] would fetch latest {limit} snapshots for "
                f"kind={kind} app={app_name or 'any'}"
            )
            return []
        try:
            q = (
                self._client.table("snapshots")
                .select("*")
                .eq("kind", kind)
                .order("created_at", desc=True)
                .limit(limit)
            )
            if app_name:
                q = q.eq("app_name", app_name)
            resp = q.execute()
            return list(getattr(resp, "data", None) or [])
        except Exception as err:
            logging.error(f"Supabase fetch_latest_snapshots failed: {err}")
            return []

    # ---- metrics ---------------------------------------------------------

    def push_metrics(self, metrics: Iterable[dict]) -> int:
        """Insert a batch of metric rows. Each dict should have keys:
        run_stamp, app_name (nullable), metric_name, value,
        delta_from_previous (nullable), meta (nullable jsonb).
        """
        batch = list(metrics)
        return self._insert("metrics", batch)

    # ---- alerts ----------------------------------------------------------

    def push_alert(
        self,
        *,
        alert_type: str,
        message: str,
        severity: str = "info",
        app_name: str | None = None,
        run_stamp: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> int:
        return self._insert(
            "alerts_log",
            [
                {
                    "alert_type": alert_type,
                    "severity": severity,
                    "message": message,
                    "app_name": app_name,
                    "run_stamp": run_stamp,
                    "meta": meta,
                }
            ],
        )

    # ---- utility ---------------------------------------------------------

    @property
    def dry_run(self) -> bool:
        return bool(self._dry_run)


def utc_stamp() -> str:
    """Match scraper.py's `_now_stamp` format so run_stamp joins work."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%SZ")
