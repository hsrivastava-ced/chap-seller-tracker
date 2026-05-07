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


def _resolve_creds_from_streamlit_secrets() -> tuple[str | None, str | None]:
    """Pull SUPABASE_URL/KEY from Streamlit secrets when env vars are missing.

    Streamlit Cloud secrets aren't automatically exposed as env vars for
    nested keys, so config.py's os.getenv() returns None there even when
    the operator pasted creds into Settings → Secrets. We accept either:

        SUPABASE_URL = "..."          # top-level
        SUPABASE_KEY = "..."

    or:

        [supabase]
        url = "..."                   # nested
        key = "..."

    Returns (None, None) if streamlit isn't importable or no secrets match.
    """
    try:
        import streamlit as st
    except Exception:
        return None, None
    try:
        # Top-level keys come through st.secrets directly.
        url = st.secrets.get("SUPABASE_URL") or None
        key = st.secrets.get("SUPABASE_KEY") or None
        if url and key:
            return url, key
        block = dict(st.secrets.get("supabase", {}) or {})
        return block.get("url"), block.get("key")
    except Exception:
        return None, None


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
        # Streamlit Cloud secrets aren't always exposed as env vars
        # (depending on whether they're top-level or nested), so fall
        # back to reading st.secrets directly when env vars are absent.
        if not (self.url and self.key):
            fallback_url, fallback_key = _resolve_creds_from_streamlit_secrets()
            self.url = self.url or fallback_url
            self.key = self.key or fallback_key
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

    # ---- sellers (relational, manual-edit-aware) ------------------------

    # Columns the upsert RPC accepts. Mirrors canonical_schema.json
    # (kind=sellers). Extra keys land in the `extra_fields` jsonb.
    _SELLERS_CANONICAL_FIELDS = frozenset({
        "seller_id", "store_url", "email", "username", "platforms",
        "installed_on", "action", "app_type", "failed_order_count",
        "last_sync", "order_count", "plan", "product_count",
        "source_country", "steps_completed", "webhooks",
    })

    def upsert_sellers(
        self,
        app_name: str,
        rows: list[dict],
        *,
        run_stamp: str,
    ) -> int:
        """Upsert a scrape into public.sellers with manual-edit guard.

        Calls the `public.upsert_sellers_with_guard(rows jsonb, run_stamp text)`
        SQL function (from sql/002_manual_edits.sql), which preserves data
        fields for rows where `manually_edited_at IS NOT NULL` but still
        advances last_scraped_at / last_scraped_run. Returns the number of
        rows the server reported as upserted (0 in dry-run).

        Callers typically invoke this alongside `push_snapshot` so we keep
        both the immutable jsonb history AND the queryable projection.
        """
        if not rows:
            return 0
        if self._dry_run or self._client is None:
            logging.info(
                f"🧪 [dry-run] would upsert {len(rows)} seller row(s) into "
                f"public.sellers for app={app_name} run={run_stamp}"
            )
            return 0

        payload = [self._prepare_seller_row(app_name, r) for r in rows]
        try:
            resp = self._client.rpc(
                "upsert_sellers_with_guard",
                {"rows": payload, "run_stamp": run_stamp},
            ).execute()
            data = getattr(resp, "data", None)
            return int(data) if isinstance(data, int) else int(data or 0)
        except Exception as err:
            logging.error(f"upsert_sellers_with_guard failed: {err}")
            return 0

    @classmethod
    def _prepare_seller_row(cls, app_name: str, row: dict) -> dict:
        """Split a scraper row into canonical fields + extra_fields jsonb.

        Keeps the RPC contract stable regardless of what the scraper adds.
        """
        canonical: dict[str, Any] = {"app_name": app_name}
        extra: dict[str, Any] = {}
        for k, v in row.items():
            if k in cls._SELLERS_CANONICAL_FIELDS:
                canonical[k] = v
            elif k in ("app_name", "run_stamp"):
                continue  # never let a scraper row override these
            else:
                extra[k] = v
        if extra:
            canonical["extra_fields"] = extra
        return canonical

    def fetch_sellers(
        self,
        *,
        app_name: str | None = None,
        manually_edited_only: bool = False,
        limit: int | None = None,
    ) -> list[dict]:
        if self._dry_run or self._client is None:
            logging.info(
                f"🧪 [dry-run] would fetch sellers app={app_name or 'any'} "
                f"manual_only={manually_edited_only} limit={limit}"
            )
            return []
        try:
            q = self._client.table("sellers").select("*")
            if app_name:
                q = q.eq("app_name", app_name)
            if manually_edited_only:
                q = q.not_.is_("manually_edited_at", "null")
            q = q.order("last_scraped_at", desc=True)
            if limit:
                q = q.limit(limit)
            resp = q.execute()
            return list(getattr(resp, "data", None) or [])
        except Exception as err:
            logging.error(f"fetch_sellers failed: {err}")
            return []

    def apply_manual_edit(
        self,
        *,
        app_name: str,
        seller_id: str,
        field: str,
        new_value: Any,
        editor_email: str,
        old_value: Any = None,
        reason: str | None = None,
    ) -> int:
        """Record a single manual edit, let the DB trigger bump
        manually_edited_at, then push the new value into sellers.

        Steps (all server-side):
          1. INSERT into manual_edits_log     → fn_manual_edits_touch
             trigger sets sellers.manually_edited_at = now()
          2. UPDATE sellers SET <field>=new   → only the one field,
             leaving manually_edited_at intact (it was just bumped).

        Callers need not pre-check permissions — roles.can(...) at the
        UI layer is the gate. This method is dumb on purpose so it's
        reusable from scripts.
        """
        if field not in self._SELLERS_CANONICAL_FIELDS:
            raise ValueError(
                f"{field!r} is not a canonical seller field; edit via "
                f"extra_fields jsonb if needed."
            )
        if self._dry_run or self._client is None:
            logging.info(
                f"🧪 [dry-run] would edit {app_name}/{seller_id}.{field}: "
                f"{old_value!r} → {new_value!r} (by {editor_email})"
            )
            return 0
        try:
            self._client.table("manual_edits_log").insert({
                "editor_email": editor_email,
                "app_name": app_name,
                "seller_id": seller_id,
                "field": field,
                "old_value": None if old_value is None else str(old_value),
                "new_value": None if new_value is None else str(new_value),
                "reason": reason,
            }).execute()
            self._client.table("sellers").update({field: new_value}).match({
                "app_name": app_name, "seller_id": seller_id,
            }).execute()
            return 1
        except Exception as err:
            logging.error(f"apply_manual_edit failed: {err}")
            return 0

    def fetch_manual_edits(
        self,
        *,
        app_name: str | None = None,
        seller_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        if self._dry_run or self._client is None:
            return []
        try:
            q = self._client.table("manual_edits_log").select("*")
            if app_name:
                q = q.eq("app_name", app_name)
            if seller_id:
                q = q.eq("seller_id", seller_id)
            resp = q.order("edited_at", desc=True).limit(limit).execute()
            return list(getattr(resp, "data", None) or [])
        except Exception as err:
            logging.error(f"fetch_manual_edits failed: {err}")
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

    # ---- auth_users (sql/004) -------------------------------------------
    # Email/password auth with admin approval. See sql/004_auth_users.sql
    # for the table shape. All methods short-circuit in dry-run.

    def get_auth_user(self, email: str) -> dict | None:
        if self._dry_run or self._client is None:
            return None
        try:
            resp = (
                self._client.table("auth_users")
                .select("*")
                .eq("email", email.lower())
                .limit(1)
                .execute()
            )
            data = list(getattr(resp, "data", None) or [])
            return data[0] if data else None
        except Exception as err:
            logging.error(f"get_auth_user({email}) failed: {err}")
            return None

    def create_auth_user(
        self,
        *,
        email: str,
        password_hash: str,
        display_name: str = "",
        status: str = "pending",
        approved_by: str | None = None,
    ) -> tuple[bool, str | None]:
        """Insert a new auth_users row.

        Returns (ok, error_message). On success error_message is None;
        on failure ok=False and error_message has the underlying reason
        (RLS denial, missing creds, network, etc.) so callers can show
        something more useful than "rejected".
        """
        if self._dry_run:
            return False, (
                "Supabase is in dry-run mode (SUPABASE_URL or SUPABASE_KEY missing). "
                "Add them to Streamlit secrets at the top level."
            )
        if self._client is None:
            return False, "Supabase client failed to initialize."
        row: dict[str, Any] = {
            "email": email.lower(),
            "password_hash": password_hash,
            "display_name": display_name or "",
            "status": status,
        }
        if status == "approved":
            row["approved_at"] = datetime.now(timezone.utc).isoformat()
            if approved_by:
                row["approved_by"] = approved_by
        try:
            resp = self._client.table("auth_users").insert(row).execute()
            if getattr(resp, "data", None):
                return True, None
            return False, "Insert returned no rows (RLS may be blocking writes)."
        except Exception as err:
            msg = str(err) or err.__class__.__name__
            logging.error(f"create_auth_user({email}) failed: {msg}")
            return False, msg

    def list_auth_users(self, *, status: str | None = None) -> list[dict]:
        if self._dry_run or self._client is None:
            return []
        try:
            q = self._client.table("auth_users").select("*").order("requested_at", desc=True)
            if status:
                q = q.eq("status", status)
            resp = q.execute()
            return list(getattr(resp, "data", None) or [])
        except Exception as err:
            logging.error(f"list_auth_users(status={status}) failed: {err}")
            return []

    def update_auth_user_status(
        self,
        email: str,
        *,
        status: str,
        approved_by: str | None = None,
    ) -> bool:
        if self._dry_run or self._client is None:
            logging.info(f"🧪 [dry-run] would set {email} → status={status}")
            return False
        patch: dict[str, Any] = {"status": status}
        if status == "approved":
            patch["approved_at"] = datetime.now(timezone.utc).isoformat()
            if approved_by:
                patch["approved_by"] = approved_by
        try:
            resp = (
                self._client.table("auth_users")
                .update(patch)
                .eq("email", email.lower())
                .execute()
            )
            return bool(getattr(resp, "data", None))
        except Exception as err:
            logging.error(f"update_auth_user_status({email}) failed: {err}")
            return False

    def update_auth_user_last_login(self, email: str) -> None:
        if self._dry_run or self._client is None:
            return
        try:
            self._client.table("auth_users").update(
                {"last_login_at": datetime.now(timezone.utc).isoformat()}
            ).eq("email", email.lower()).execute()
        except Exception as err:
            logging.error(f"update_auth_user_last_login({email}) failed: {err}")

    # ---- utility ---------------------------------------------------------

    @property
    def dry_run(self) -> bool:
        return bool(self._dry_run)


def utc_stamp() -> str:
    """Match scraper.py's `_now_stamp` format so run_stamp joins work."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%SZ")
