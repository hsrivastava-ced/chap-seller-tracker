"""
roles.py — RBAC lookup + permission checks.

Data lives in `roles.yaml` (committed to the repo, edited through the
admin UI). See MULTI_APP_DESIGN.md §4 for the full permission matrix.

Design decisions worth flagging:

  1. The super-admin email `hsrivastava@threecolts.com` is hard-coded
     in `HARD_CODED_SUPER_ADMINS` below, AS WELL AS being in roles.yaml.
     This is a safety net — if someone edits roles.yaml wrong and
     removes themselves, the hard-coded entry guarantees the actual
     owner of the project can still log in and repair the file. When
     adding new super admins via the UI, we only modify roles.yaml;
     the hard-coded list stays as-is.

  2. Default role for a valid @threecolts.com email absent from
     roles.yaml is "viewer". This matches the product brief: "any
     threecolts user will be able to view".

  3. Non-@threecolts.com emails get role=None — callers should treat
     that as "forbidden". auth.py rejects those before this module
     even sees them; roles.py is defense in depth.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Keep this tiny — only for break-glass recovery. Grant via the UI
# rather than editing this list.
HARD_CODED_SUPER_ADMINS = frozenset({
    "hsrivastava@threecolts.com",
})

ALLOWED_DOMAIN = "threecolts.com"

_DEFAULT_PATH = Path(__file__).resolve().parent / "roles.yaml"

# Role constants (use these instead of string literals when calling `can()`).
SUPER_ADMIN = "super_admin"
EDITOR = "editor"
VIEWER = "viewer"
_ALL_ROLES = (SUPER_ADMIN, EDITOR, VIEWER)


@dataclass(frozen=True)
class UserPrincipal:
    """A logged-in user, post-auth."""
    email: str
    role: str                       # super_admin | editor | viewer

    @property
    def is_super_admin(self) -> bool:
        return self.role == SUPER_ADMIN

    @property
    def is_editor(self) -> bool:
        return self.role in (EDITOR, SUPER_ADMIN)


# ---------------------------------------------------------------------
# YAML I/O
# ---------------------------------------------------------------------
def _yaml():
    import yaml
    return yaml


def _load_raw(path: Optional[Path] = None) -> dict:
    p = Path(path) if path else _DEFAULT_PATH
    if not p.exists():
        return {}
    try:
        return _yaml().safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        # roles.yaml broken? Don't crash — hard-coded admins still work.
        return {}


def _save_raw(raw: dict, path: Optional[Path] = None) -> None:
    p = Path(path) if path else _DEFAULT_PATH
    p.write_text(_yaml().safe_dump(raw, sort_keys=False), encoding="utf-8")


def all_roles(path: Optional[Path] = None) -> dict[str, str]:
    """Return a dict of {email: role} merged with the hard-coded super admins."""
    raw = _load_raw(path)
    mapping: dict[str, str] = dict(raw.get("roles") or {})
    # Hard-coded super admins always win over roles.yaml (so no-one can
    # demote Hrithik by editing the file).
    for email in HARD_CODED_SUPER_ADMINS:
        mapping[email.lower()] = SUPER_ADMIN
    return {e.lower(): r for e, r in mapping.items()}


# ---------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------
def role_for(email: str, path: Optional[Path] = None) -> Optional[str]:
    """Resolve an email to a role.

    Returns:
        - one of super_admin / editor / viewer
        - None if the email is outside the allowed domain (caller should
          treat as "reject")
    """
    if not email:
        return None
    email_l = email.lower().strip()
    # domain gate
    if not email_l.endswith("@" + ALLOWED_DOMAIN):
        return None

    mapping = all_roles(path)
    if email_l in mapping:
        role = mapping[email_l]
        if role in _ALL_ROLES:
            return role
        # Unknown role in YAML → treat as viewer rather than crashing.
    return VIEWER


def principal_for(email: str, path: Optional[Path] = None) -> Optional[UserPrincipal]:
    role = role_for(email, path)
    if role is None:
        return None
    return UserPrincipal(email=email.lower().strip(), role=role)


# ---------------------------------------------------------------------
# Permission checks
# ---------------------------------------------------------------------
def can(principal: Optional[UserPrincipal], action: str) -> bool:
    """Single permission oracle — add new capabilities here as we build.

    Capability names are stable strings used by the UI and by server-side
    gates alike. Keep them in sync with the table in MULTI_APP_DESIGN.md.
    """
    if principal is None:
        return False

    match action:
        case "view_dashboard":
            return principal.role in (VIEWER, EDITOR, SUPER_ADMIN)
        case "export_csv":
            return principal.role in (VIEWER, EDITOR, SUPER_ADMIN)
        case "see_admin_tab":
            return principal.role in (EDITOR, SUPER_ADMIN)
        case "add_app":
            return principal.role in (EDITOR, SUPER_ADMIN)
        case "approve_schema_drift":
            return principal.role == SUPER_ADMIN
        case "edit_seller":
            # Per-row manual edits go through public.sellers + the
            # manual_edits_log audit trail (sql/002_manual_edits.sql).
            # Editors fix typos / annotations; viewers can't write.
            return principal.role in (EDITOR, SUPER_ADMIN)
        case "see_users_tab" | "grant_role" | "revoke_role":
            return principal.role == SUPER_ADMIN
        case _:
            return False


# ---------------------------------------------------------------------
# Mutations (used by the super-admin Users tab)
# ---------------------------------------------------------------------
def set_role(email: str, role: str, path: Optional[Path] = None) -> None:
    """Write (or overwrite) a role for an email in roles.yaml.

    Caller must verify the acting principal has `grant_role`. This
    function is dumb on purpose — it never checks permissions, so
    it's also reusable from one-off admin scripts.
    """
    if role not in _ALL_ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {_ALL_ROLES}")
    email_l = email.lower().strip()
    if not email_l.endswith("@" + ALLOWED_DOMAIN):
        raise ValueError(f"only @{ALLOWED_DOMAIN} emails can be assigned a role")

    raw = _load_raw(path)
    raw.setdefault("schema_version", 1)
    mapping = dict(raw.get("roles") or {})
    mapping[email_l] = role
    raw["roles"] = mapping
    _save_raw(raw, path)


def revoke_role(email: str, path: Optional[Path] = None) -> None:
    """Remove an email from roles.yaml (effectively demotes them to
    default viewer, since any @threecolts.com user still gets viewer)."""
    email_l = email.lower().strip()
    if email_l in HARD_CODED_SUPER_ADMINS:
        raise ValueError(
            f"{email_l} is hard-coded as super_admin and cannot be revoked via the UI"
        )
    raw = _load_raw(path)
    mapping = dict(raw.get("roles") or {})
    mapping.pop(email_l, None)
    raw["roles"] = mapping
    _save_raw(raw, path)


# ---------------------------------------------------------------------
# Small helpers the UI uses
# ---------------------------------------------------------------------
def list_assigned(path: Optional[Path] = None) -> list[tuple[str, str]]:
    """Return a sorted list of (email, role) — hard-coded entries first."""
    mapping = all_roles(path)
    def sort_key(item):
        email, role = item
        rank = {SUPER_ADMIN: 0, EDITOR: 1, VIEWER: 2}.get(role, 9)
        hc = 0 if email in HARD_CODED_SUPER_ADMINS else 1
        return (rank, hc, email)
    return sorted(mapping.items(), key=sort_key)


def audit_stamp(actor_email: str, action: str) -> str:
    """Return a one-line audit string for commit messages.

    Used when auto-committing roles.yaml / apps.yaml changes from the UI:
    every mutation records who did it and when.
    """
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"{action} by {actor_email} at {ts}"
