"""Backfill the canonical seller identity layer from local scrape data.

What this does:
  1. Reads `results/latest/run.json` — the most recent cHAP scrape across
     all 6 apps (single source of truth, always present).
  2. Normalizes email + store URL via `seller_identity.normalize_*`.
  3. Resolves identities — same person/business across apps gets one
     `seller_key`; same storefront gets one `shop_id`. Uses the rules
     in `seller_identity.decide(...)`.
  4. Writes to Supabase:
       - `sellers_canonical` (one row per unique seller)
       - `seller_shops` (one row per unique storefront)
       - `seller_identity_log` (audit trail of every decision)
       - Updates `sellers.seller_key` + `sellers.shop_id` on rows that
         already exist in the legacy table (so the dashboard can join).
  5. Idempotent: re-running reuses existing seller_key / shop_id by
     reading the identity tables first. Safe to run after every scrape
     until the live pipeline integration lands.

How to run:
    python3 backfill_seller_identity.py            # commits to Supabase
    python3 backfill_seller_identity.py --dry-run  # log only, no writes

Prereqs:
  - `sql/006_seller_identity.sql` already applied to Supabase.
  - SUPABASE_URL + SUPABASE_KEY env vars set (service_role recommended
    so RLS doesn't block writes).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from normalize import normalize_run_data
from seller_identity import (
    ExistingSeller,
    ExistingShop,
    ResolveInputs,
    decide,
    extract_framework,
    is_test_store,
    normalize_email,
    normalize_shop_url,
)
from supabase_client import SupabaseClient


CHAP_APPS = (
    "shopify_temu", "shein", "shopify_temu_eu", "shein_woocommerce",
    "shopify_gearexchange", "michael",
)

LATEST_RUN = Path(__file__).resolve().parent / "results" / "latest" / "run.json"


# ---------------------------------------------------------------------
# In-memory identity graph
# ---------------------------------------------------------------------

class IdentityGraph:
    """In-memory mirror of the identity tables. Mutates as the resolver
    processes scrape rows; persists to Supabase at the end."""

    def __init__(self) -> None:
        # sellers_canonical
        self.sellers: dict[str, dict] = {}            # seller_key -> row dict
        self.email_to_seller: dict[str, str] = {}     # canonical email -> seller_key
        # seller_shops
        self.shops: dict[str, dict] = {}              # shop_id -> row dict
        self.url_to_shop: dict[str, str] = {}         # canonical url -> shop_id
        # seller_identity_log
        self.log_events: list[dict] = []
        # Track which seller_keys / shop_ids existed BEFORE this run
        # (so we know what to INSERT vs UPDATE).
        self.pre_existing_sellers: set[str] = set()
        self.pre_existing_shops: set[str] = set()
        # Track which seller rows we'll need to write back to existing
        # `sellers` table — keyed by (app_name, chap_seller_id).
        self.install_links: dict[tuple[str, str], dict] = {}

    # ---- hydrate from Supabase (for idempotent re-runs) ----

    def hydrate(self, client: SupabaseClient) -> None:
        """Load whatever's already in Supabase so a re-run reuses keys
        instead of duplicating sellers/shops."""
        if client._dry_run or client._client is None:  # type: ignore[attr-defined]
            logging.info("🧪 hydrate: dry-run mode — starting from empty graph")
            return
        try:
            sc_rows = client._client.table("sellers_canonical").select("*").execute().data or []
            for r in sc_rows:
                self.sellers[r["seller_key"]] = r
                self.pre_existing_sellers.add(r["seller_key"])
                self.email_to_seller[r["primary_email"]] = r["seller_key"]
                for alias in (r.get("email_aliases") or []):
                    self.email_to_seller.setdefault(alias, r["seller_key"])
            sh_rows = client._client.table("seller_shops").select("*").execute().data or []
            for r in sh_rows:
                self.shops[r["shop_id"]] = r
                self.pre_existing_shops.add(r["shop_id"])
                self.url_to_shop[r["shop_url_canonical"]] = r["shop_id"]
            logging.info(
                f"🔌 hydrated from Supabase: "
                f"{len(self.sellers)} sellers, {len(self.shops)} shops"
            )
        except Exception as err:
            logging.warning(f"hydrate failed (treating as empty): {err}")

    # ---- lookups used by the resolver ----

    def _seller_by_primary(self, canonical_email: str) -> ExistingSeller | None:
        sk = self.email_to_seller.get(canonical_email)
        if not sk:
            return None
        row = self.sellers.get(sk)
        if not row:
            return None
        return ExistingSeller(
            seller_key=sk,
            primary_email=row["primary_email"],
            email_aliases=tuple(row.get("email_aliases") or []),
        )

    def _shop_existing(self, canonical_url: str) -> ExistingShop | None:
        sid = self.url_to_shop.get(canonical_url)
        if not sid:
            return None
        row = self.shops.get(sid)
        return ExistingShop(shop_id=sid, seller_key=(row or {}).get("seller_key"))

    def _seller_by_shop(self, shop: ExistingShop | None) -> ExistingSeller | None:
        if shop is None or not shop.seller_key:
            return None
        row = self.sellers.get(shop.seller_key)
        if not row:
            return None
        return ExistingSeller(
            seller_key=shop.seller_key,
            primary_email=row["primary_email"],
            email_aliases=tuple(row.get("email_aliases") or []),
        )

    # ---- core: resolve one scrape row ----

    def upsert_install(
        self,
        *,
        app_name: str,
        chap_seller_id: str,
        email_raw: str,
        url_raw: str,
        platforms: str,
        source_country: str,
        run_stamp: str,
    ) -> dict | None:
        """Run the resolver for one (app, scrape-row) and apply the
        decision in-memory. Returns the install-link record (or None
        if the row had no usable identity signal)."""
        email_canonical = normalize_email(email_raw)
        url_canonical = normalize_shop_url(url_raw)
        if not email_canonical and not url_canonical:
            logging.warning(
                f"skipped row with no email and no URL: app={app_name} "
                f"chap_seller_id={chap_seller_id}"
            )
            return None

        framework = extract_framework(platforms)
        test_flag = is_test_store(
            email_canonical=email_canonical,
            shop_url_canonical=url_canonical,
        )

        inputs = ResolveInputs(
            email_canonical=email_canonical,
            shop_url_canonical=url_canonical,
            framework=framework,
            source_country=source_country or "",
            app_name=app_name,
            chap_seller_id=chap_seller_id,
            run_stamp=run_stamp,
            is_test_store=test_flag,
        )
        shop = self._shop_existing(url_canonical) if url_canonical else None
        seller_by_email = self._seller_by_primary(email_canonical) if email_canonical else None
        seller_by_shop = self._seller_by_shop(shop)

        decision = decide(
            inputs=inputs,
            seller_by_email=seller_by_email,
            seller_by_alias=None,  # primary lookup covers both via email_to_seller dict
            shop_existing=shop,
            seller_by_shop=seller_by_shop,
        )

        now = datetime.now(timezone.utc).isoformat()

        # ---- Apply seller decision ----
        if decision.create_seller:
            seller_key = str(uuid.uuid4())
            self.sellers[seller_key] = {
                "seller_key": seller_key,
                "primary_email": email_canonical,
                "primary_email_raw": email_raw,
                "email_aliases": [],
                "is_test_store": test_flag,
                "first_seen_at": now,
                "last_seen_at": now,
                "created_at": now,
                "updated_at": now,
            }
            if email_canonical:
                self.email_to_seller[email_canonical] = seller_key
            self.log_events.append({
                "event_type": "create_seller",
                "seller_key": seller_key,
                "shop_id": None,
                "app_name": app_name,
                "chap_seller_id": chap_seller_id,
                "run_stamp": run_stamp,
                "old_value": None,
                "new_value": {"primary_email": email_canonical},
                "note": decision.note,
                "created_at": now,
            })
        else:
            seller_key = decision.use_seller_key
            # Bump last_seen_at + maybe relax is_test_store
            row = self.sellers[seller_key]
            row["last_seen_at"] = now
            row["updated_at"] = now
            if row.get("is_test_store") and not test_flag:
                # Existing seller had ALL-test shops; this row is real → unflag.
                row["is_test_store"] = False
                self.log_events.append({
                    "event_type": "update_test_store_flag",
                    "seller_key": seller_key,
                    "shop_id": None,
                    "app_name": app_name,
                    "chap_seller_id": chap_seller_id,
                    "run_stamp": run_stamp,
                    "old_value": {"is_test_store": True},
                    "new_value": {"is_test_store": False},
                    "note": "real (non-test) install observed for previously-all-test seller",
                    "created_at": now,
                })

        # ---- Apply alias decision ----
        if decision.add_email_alias:
            aliases = list(self.sellers[seller_key].get("email_aliases") or [])
            if decision.add_email_alias not in aliases:
                aliases.append(decision.add_email_alias)
                self.sellers[seller_key]["email_aliases"] = aliases
                self.email_to_seller[decision.add_email_alias] = seller_key
                self.log_events.append({
                    "event_type": "add_email_alias",
                    "seller_key": seller_key,
                    "shop_id": shop.shop_id if shop else None,
                    "app_name": app_name,
                    "chap_seller_id": chap_seller_id,
                    "run_stamp": run_stamp,
                    "old_value": {"aliases": aliases[:-1]},
                    "new_value": {"aliases": aliases},
                    "note": decision.note,
                    "created_at": now,
                })
        if decision.collision_blocked:
            self.log_events.append({
                "event_type": "collision_blocked",
                "seller_key": seller_key,
                "shop_id": shop.shop_id if shop else None,
                "app_name": app_name,
                "chap_seller_id": chap_seller_id,
                "run_stamp": run_stamp,
                "old_value": None,
                "new_value": {"attempted_email": email_canonical},
                "note": decision.note,
                "created_at": now,
            })

        # ---- Apply shop decision ----
        if decision.create_shop:
            shop_id = str(uuid.uuid4())
            self.shops[shop_id] = {
                "shop_id": shop_id,
                "seller_key": seller_key,
                "shop_url_canonical": url_canonical,
                "shop_url_raw": url_raw,
                "shop_name": None,
                "framework": framework or None,
                "source_country": source_country or None,
                "is_test_store": test_flag,
                "panel": "chap",
                "first_seen_at": now,
                "last_seen_at": now,
                "created_at": now,
                "updated_at": now,
            }
            if url_canonical:
                self.url_to_shop[url_canonical] = shop_id
            self.log_events.append({
                "event_type": "create_shop",
                "seller_key": seller_key,
                "shop_id": shop_id,
                "app_name": app_name,
                "chap_seller_id": chap_seller_id,
                "run_stamp": run_stamp,
                "old_value": None,
                "new_value": {"shop_url_canonical": url_canonical, "framework": framework},
                "note": decision.note,
                "created_at": now,
            })
        else:
            shop_id = decision.use_shop_id
            row = self.shops[shop_id]
            row["last_seen_at"] = now
            row["updated_at"] = now
            # Top up missing fields opportunistically
            if not row.get("framework") and framework:
                row["framework"] = framework
            if not row.get("source_country") and source_country:
                row["source_country"] = source_country

        # ---- Install link (writeback to legacy `sellers` table) ----
        link = {
            "app_name": app_name,
            "seller_id": chap_seller_id,
            "seller_key": seller_key,
            "shop_id": shop_id,
        }
        self.install_links[(app_name, chap_seller_id)] = link
        return link


# ---------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------

def _persist(
    graph: IdentityGraph,
    client: SupabaseClient,
    *,
    commit: bool,
    sellers_rows_by_app: dict[str, list[dict]],
    run_stamp: str,
) -> dict:
    """Write the in-memory graph to Supabase. Returns counters for the
    summary printout.

    Also populates the legacy `sellers` table (the per-(app, seller_id)
    relational projection) via `upsert_sellers_with_guard`. Two reasons
    this happens here, not in pipeline.py:
      1. Pipeline historically only wrote to `snapshots`; `sellers` has
         been empty in prod. Backfilling once gets us to a clean state.
      2. We need the rows there BEFORE the FK update (seller_key /
         shop_id) at the bottom of this function has anything to match.
    """
    counters = {
        "legacy_sellers_upserted": 0,
        "sellers_inserted": 0, "sellers_updated": 0,
        "shops_inserted": 0, "shops_updated": 0,
        "log_inserted": 0,
        "sellers_link_updated": 0,
    }
    if not commit:
        counters["sellers_inserted"] = len(graph.sellers) - len(graph.pre_existing_sellers)
        counters["shops_inserted"] = len(graph.shops) - len(graph.pre_existing_shops)
        counters["log_inserted"] = len(graph.log_events)
        counters["sellers_link_updated"] = len(graph.install_links)
        counters["legacy_sellers_upserted"] = sum(len(v) for v in sellers_rows_by_app.values())
        logging.info("🧪 dry-run — no Supabase writes performed")
        return counters

    # 0. Legacy `sellers` table — one batch per app, manual-edit guarded
    # by the upsert_sellers_with_guard RPC. Has to run first so the FK
    # update at the end has rows to match.
    for app_name, rows in sellers_rows_by_app.items():
        if not rows:
            continue
        n = client.upsert_sellers(app_name, rows, run_stamp=run_stamp)
        counters["legacy_sellers_upserted"] += n
        logging.info(f"  legacy sellers upserted: {app_name} → {n}")

    # 1. sellers_canonical — insert new, update existing
    new_sellers = [s for k, s in graph.sellers.items() if k not in graph.pre_existing_sellers]
    upd_sellers = [s for k, s in graph.sellers.items() if k in graph.pre_existing_sellers]
    if new_sellers:
        try:
            resp = client._client.table("sellers_canonical").insert(new_sellers).execute()
            counters["sellers_inserted"] = len(resp.data or [])
        except Exception as err:
            logging.error(f"insert sellers_canonical failed: {err}")
    for s in upd_sellers:
        try:
            client._client.table("sellers_canonical").update({
                "email_aliases": s.get("email_aliases") or [],
                "is_test_store": s.get("is_test_store", False),
                "last_seen_at": s["last_seen_at"],
            }).eq("seller_key", s["seller_key"]).execute()
            counters["sellers_updated"] += 1
        except Exception as err:
            logging.error(f"update sellers_canonical {s['seller_key']} failed: {err}")

    # 2. seller_shops — insert new, update existing
    new_shops = [s for k, s in graph.shops.items() if k not in graph.pre_existing_shops]
    upd_shops = [s for k, s in graph.shops.items() if k in graph.pre_existing_shops]
    if new_shops:
        try:
            resp = client._client.table("seller_shops").insert(new_shops).execute()
            counters["shops_inserted"] = len(resp.data or [])
        except Exception as err:
            logging.error(f"insert seller_shops failed: {err}")
    for s in upd_shops:
        try:
            client._client.table("seller_shops").update({
                "framework": s.get("framework"),
                "source_country": s.get("source_country"),
                "is_test_store": s.get("is_test_store", False),
                "last_seen_at": s["last_seen_at"],
            }).eq("shop_id", s["shop_id"]).execute()
            counters["shops_updated"] += 1
        except Exception as err:
            logging.error(f"update seller_shops {s['shop_id']} failed: {err}")

    # 3. seller_identity_log — insert all events (audit table, append-only)
    if graph.log_events:
        # Chunk inserts to avoid hitting payload limits.
        chunk = 500
        for i in range(0, len(graph.log_events), chunk):
            batch = graph.log_events[i:i + chunk]
            try:
                resp = client._client.table("seller_identity_log").insert(batch).execute()
                counters["log_inserted"] += len(resp.data or [])
            except Exception as err:
                logging.error(f"insert seller_identity_log batch failed: {err}")

    # 4. sellers (legacy per-app table) — update FK columns where rows exist
    for (app_name, chap_seller_id), link in graph.install_links.items():
        try:
            resp = client._client.table("sellers").update({
                "seller_key": link["seller_key"],
                "shop_id": link["shop_id"],
            }).match({"app_name": app_name, "seller_id": chap_seller_id}).execute()
            if resp.data:
                counters["sellers_link_updated"] += len(resp.data)
        except Exception as err:
            logging.error(f"update sellers FK ({app_name},{chap_seller_id}) failed: {err}")

    return counters


# ---------------------------------------------------------------------
# Public API — used by pipeline.py to refresh identity on every scrape
# ---------------------------------------------------------------------

def apply_identity_layer(
    client: SupabaseClient,
    sellers_by_app: dict[str, list[dict]],
    *,
    run_stamp: str,
) -> dict:
    """Idempotent identity refresh for a single scrape's worth of sellers.

    Called from pipeline.py after `upsert_sellers` so the per-(app, seller)
    legacy rows already exist when we set their seller_key/shop_id FKs.

    Returns the same counter dict as the standalone backfill so the
    pipeline can log a one-line summary alongside its other write counts.

    Skips the legacy `sellers` upsert step (passes empty dict) because
    pipeline.py already did that — avoids double-writing per app.
    """
    graph = IdentityGraph()
    graph.hydrate(client)
    for app_name, rows in (sellers_by_app or {}).items():
        for row in rows or []:
            graph.upsert_install(
                app_name=app_name,
                chap_seller_id=(row.get("seller_id") or "").strip(),
                email_raw=row.get("email") or "",
                url_raw=row.get("store_url") or row.get("username") or "",
                platforms=row.get("platforms") or "",
                source_country=row.get("source_country") or "",
                run_stamp=run_stamp,
            )
    return _persist(
        graph, client, commit=True,
        sellers_rows_by_app={},   # pipeline already upserted; skip duplicate
        run_stamp=run_stamp,
    )


# ---------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="Don't write to Supabase")
    p.add_argument("--source", type=Path, default=LATEST_RUN,
                   help="Path to run.json (default: results/latest/run.json)")
    args = p.parse_args()

    if not args.source.exists():
        logging.error(f"source file not found: {args.source}")
        return 1

    with open(args.source) as f:
        run = json.load(f)
    raw_data = run.get("data") or {}
    run_stamp = run.get("run_stamp") or args.source.stat().st_mtime.__str__()

    # Normalize scrape dates so anything downstream is consistent.
    sellers_norm, _ = normalize_run_data(raw_data, {})

    client = SupabaseClient()
    graph = IdentityGraph()
    graph.hydrate(client)

    seen = 0
    for app_name in CHAP_APPS:
        rows = sellers_norm.get(app_name, [])
        for row in rows:
            graph.upsert_install(
                app_name=app_name,
                chap_seller_id=(row.get("seller_id") or "").strip(),
                email_raw=row.get("email") or "",
                url_raw=row.get("store_url") or row.get("username") or "",
                platforms=row.get("platforms") or "",
                source_country=row.get("source_country") or "",
                run_stamp=run_stamp,
            )
            seen += 1

    commit = not args.dry_run
    counters = _persist(
        graph, client, commit=commit,
        sellers_rows_by_app={app: sellers_norm.get(app, []) for app in CHAP_APPS},
        run_stamp=run_stamp,
    )

    print()
    print("=" * 64)
    print(f"Backfill {'committed' if commit else 'DRY RUN — no writes'}")
    print("=" * 64)
    print(f"  scrape rows processed:         {seen}")
    print(f"  unique sellers in graph:       {len(graph.sellers)}")
    print(f"  unique shops in graph:         {len(graph.shops)}")
    print(f"  install links (legacy FK):     {len(graph.install_links)}")
    print()
    print(f"  sellers (per-install) upserted:{counters['legacy_sellers_upserted']}")
    print(f"  sellers_canonical inserted:    {counters['sellers_inserted']}")
    print(f"  sellers_canonical updated:     {counters['sellers_updated']}")
    print(f"  seller_shops inserted:         {counters['shops_inserted']}")
    print(f"  seller_shops updated:          {counters['shops_updated']}")
    print(f"  seller_identity_log inserted:  {counters['log_inserted']}")
    print(f"  sellers FK rows updated:       {counters['sellers_link_updated']}")

    # Detail: aliases auto-added + collisions blocked
    aliases = sum(1 for e in graph.log_events if e["event_type"] == "add_email_alias")
    collisions = sum(1 for e in graph.log_events if e["event_type"] == "collision_blocked")
    if aliases or collisions:
        print()
        print(f"  email aliases auto-added:      {aliases}")
        print(f"  collisions blocked (review):   {collisions}")

    # Cross-app insight (the whole reason for this layer)
    apps_per_seller: dict[str, set[str]] = defaultdict(set)
    for (app, _sid), link in graph.install_links.items():
        apps_per_seller[link["seller_key"]].add(app)
    multi_app = {sk: apps for sk, apps in apps_per_seller.items() if len(apps) > 1}
    print()
    print(f"  sellers on >1 cHAP app:        {len(multi_app)}")
    if multi_app:
        print(f"  (top 5 by app count:)")
        for sk, apps in sorted(multi_app.items(), key=lambda kv: -len(kv[1]))[:5]:
            row = graph.sellers[sk]
            print(f"    {row['primary_email']:35} {sorted(apps)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
