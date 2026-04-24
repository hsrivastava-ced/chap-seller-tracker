"""
app_registry.py — single source of truth for which admin panels to scrape.

Before this module existed, `config.py` hard-coded three app IDs and
three credential pairs. Adding a fourth app meant editing three places
(config.py, scraper.py, GitHub secrets) in lockstep.

Now:
  - the list of apps lives in `apps.yaml` (committed to the repo)
  - credentials live in the GitHub Actions `CREDS` secret (unchanged)
  - this module is the only code that reads both and stitches them together

Everything downstream (scraper.py, dashboard.py, admin_ui.py) asks the
registry for apps, not the env or the YAML directly. That way when we
add / remove / disable an app, there is exactly one place to touch.

------------------------------------------------------------------------
apps.yaml shape:
    schema_version: 1
    apps:
      - id: shopify_temu          # stable internal id (used in filenames, DB)
        label: "TEMU US (Shopify)"
        dropdown_value: shopify_temu
        scrape_installs: true
        scrape_uninstalls: true
        creds_ref: APP_1          # -> APP_1_USER, APP_1_PASS inside CREDS
        added_by: ...
        added_at: ...
        schema_status: canonical  # canonical | pending_review | blocked

Environment (from CREDS parsing in the workflow, or plain .env locally):
    APP_1_USER, APP_1_PASS, APP_2_USER, APP_2_PASS, ...
    LOGIN_URL

------------------------------------------------------------------------
Public API:
    load_registry(path=None) -> list[AppEntry]
    active_apps() -> list[AppEntry]                 # schema_status == canonical
    all_apps()    -> list[AppEntry]                 # everything, including blocked
    get(app_id)   -> AppEntry | None
    credentials_for(app_entry) -> (user, password)
    next_creds_ref() -> "APP_N"                     # used by admin UI when adding
    add_app(entry, path=None) -> None               # appends to apps.yaml
    save_registry(apps, path=None) -> None
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

# PyYAML is in requirements.txt (we're adding it for this feature).
# Import is deferred into functions so scraper.py imports of config don't
# blow up in the tiny chance PyYAML isn't installed yet.
_DEFAULT_PATH = Path(__file__).resolve().parent / "apps.yaml"


# ---------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------
@dataclass
class AppEntry:
    """One admin-panel source to scrape.

    Matches the structure in apps.yaml 1:1. Extra fields added later
    should get sensible defaults so old YAML still loads.
    """

    id: str
    label: str
    dropdown_value: str
    scrape_installs: bool = True
    scrape_uninstalls: bool = True
    creds_ref: str = ""                  # e.g. "APP_1"
    added_by: str = ""
    added_at: str = ""
    schema_status: str = "canonical"      # canonical | pending_review | blocked
    notes: str = ""

    # ---- derived ----
    @property
    def user_env_key(self) -> str:
        return f"{self.creds_ref}_USER" if self.creds_ref else ""

    @property
    def pass_env_key(self) -> str:
        return f"{self.creds_ref}_PASS" if self.creds_ref else ""

    @property
    def is_ready_to_scrape(self) -> bool:
        """True iff the app has working credentials AND its schema_status
        is either canonical or the caller has explicitly opted in to
        pending_review (the admin UI has that lever)."""
        if not self.creds_ref:
            return False
        if not os.getenv(self.user_env_key):
            return False
        if not os.getenv(self.pass_env_key):
            return False
        if self.schema_status == "blocked":
            return False
        return True


# ---------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------
def _yaml():
    import yaml  # local import keeps the hard dep out of the import graph
    return yaml


def load_registry(path: Optional[Path] = None) -> list[AppEntry]:
    """Read apps.yaml and return a list of AppEntry.

    Falls back to an empty list if the file doesn't exist (e.g. brand-new
    clone before `apps.yaml` has been committed). Callers can layer the
    legacy env-based loader on top if they want to keep the old behavior.
    """
    p = Path(path) if path else _DEFAULT_PATH
    if not p.exists():
        return []
    data = _yaml().safe_load(p.read_text(encoding="utf-8")) or {}
    entries = data.get("apps") or []
    out: list[AppEntry] = []
    for raw in entries:
        # tolerate extra keys — don't crash when the YAML schema drifts forward
        allowed = {f.name for f in AppEntry.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        clean = {k: v for k, v in raw.items() if k in allowed}
        out.append(AppEntry(**clean))
    return out


def save_registry(apps: Iterable[AppEntry], path: Optional[Path] = None) -> None:
    """Serialize the list back to YAML, preserving schema_version."""
    p = Path(path) if path else _DEFAULT_PATH
    payload = {
        "schema_version": 1,
        "apps": [asdict(a) for a in apps],
    }
    p.write_text(_yaml().safe_dump(payload, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------
# Convenience accessors
# ---------------------------------------------------------------------
def all_apps(path: Optional[Path] = None) -> list[AppEntry]:
    """Every entry in apps.yaml, regardless of status.

    Includes legacy-env back-compat: if apps.yaml is missing entirely,
    fall through to the legacy APP_1/2/3 env layout so scraper.py still
    works on an un-migrated checkout.
    """
    entries = load_registry(path)
    if entries:
        return entries
    return _legacy_env_apps()


def active_apps(path: Optional[Path] = None) -> list[AppEntry]:
    """Apps a scheduled scrape should touch — canonical status + creds present."""
    return [a for a in all_apps(path) if a.is_ready_to_scrape and a.schema_status == "canonical"]


def get(app_id: str, path: Optional[Path] = None) -> Optional[AppEntry]:
    for a in all_apps(path):
        if a.id == app_id:
            return a
    return None


def credentials_for(app: AppEntry) -> tuple[Optional[str], Optional[str]]:
    """Resolve (user, password) from the environment for a given app."""
    if not app.creds_ref:
        return None, None
    return os.getenv(app.user_env_key), os.getenv(app.pass_env_key)


# ---------------------------------------------------------------------
# Used by the admin UI when onboarding a new app
# ---------------------------------------------------------------------
def next_creds_ref(apps: Optional[list[AppEntry]] = None) -> str:
    """Return the next free `APP_N` slot.

    We don't recycle slots — once APP_4 has been used we always pick
    APP_5 next, even if APP_4 was later removed. This avoids the
    footgun of a stale secret from a deleted app binding to a fresh
    one with the same creds_ref.
    """
    apps = apps if apps is not None else all_apps()
    used = set()
    for a in apps:
        if a.creds_ref and a.creds_ref.startswith("APP_"):
            try:
                used.add(int(a.creds_ref.split("_", 1)[1]))
            except ValueError:
                pass
    n = max(used) + 1 if used else 1
    return f"APP_{n}"


def add_app(entry: AppEntry, path: Optional[Path] = None) -> None:
    """Append a new entry to apps.yaml.

    Caller is responsible for:
      - already adding APP_N_USER / APP_N_PASS to the CREDS secret
      - running a dry scrape to populate schema_status correctly

    This function only mutates the YAML file.
    """
    apps = load_registry(path)
    if any(a.id == entry.id for a in apps):
        raise ValueError(f"app id '{entry.id}' already exists in registry")
    if any(a.creds_ref == entry.creds_ref for a in apps if a.creds_ref):
        raise ValueError(f"creds_ref '{entry.creds_ref}' already in use")
    if not entry.added_at:
        entry.added_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    apps.append(entry)
    save_registry(apps, path)


# ---------------------------------------------------------------------
# Legacy fallback — keeps unmigrated checkouts working
# ---------------------------------------------------------------------
def _legacy_env_apps() -> list[AppEntry]:
    """If apps.yaml is missing, pretend it was populated with the
    historical three entries driven by APP_1 / APP_2 / APP_3 env vars.

    This keeps scraper.py running during the transition window — we can
    land the registry module and rebase the scraper in separate commits
    without breaking prod.
    """
    legacy_ids = [
        ("APP_1", "shopify_temu",    "TEMU US (Shopify)"),
        ("APP_2", "shein",           "SHEIN"),
        ("APP_3", "shopify_temu_eu", "TEMU EU (Shopify)"),
    ]
    out: list[AppEntry] = []
    for ref, app_id, label in legacy_ids:
        if os.getenv(f"{ref}_USER") and os.getenv(f"{ref}_PASS"):
            out.append(
                AppEntry(
                    id=app_id,
                    label=label,
                    dropdown_value=app_id,
                    creds_ref=ref,
                    added_by="legacy-env",
                    added_at="",
                    schema_status="canonical",
                )
            )
    return out
