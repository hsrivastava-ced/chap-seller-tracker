"""
cedadmin_roles.py — access control for the cedadmin pages.

INTENTIONALLY isolated from `roles.py` (cHAP). A user granted any
cHAP role does NOT automatically get cedadmin access — they have to
be added separately to `cedadmin_roles.yaml`. This is per Hrithik's
"separate access" rule for the cedadmin panel.

API mirrors `roles.py` so callers reading both modules' code recognize
the shape:
    - load() → {email: role_str}
    - role_for(email) → "super_admin" | "editor" | "viewer" | None
    - can(email, action) → bool

Hard-coded super admins (`roles.HARD_CODED_SUPER_ADMINS`) are also
super_admin here as a break-glass — keeps the project owner unable to
lock themselves out by editing the YAML wrong.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import roles as _chap_roles  # for HARD_CODED_SUPER_ADMINS only

SUPER_ADMIN = "super_admin"
EDITOR = "editor"
VIEWER = "viewer"

_DEFAULT_PATH = Path(__file__).resolve().parent / "cedadmin_roles.yaml"

# Action → minimum role required.
_ACTIONS: dict[str, str] = {
    "view_cedadmin":     VIEWER,
    "view_intelligence": VIEWER,
    "export_csv":        EDITOR,
    "manage_grants":     SUPER_ADMIN,
}

_ORDER = {SUPER_ADMIN: 3, EDITOR: 2, VIEWER: 1}


def _load(path: Optional[Path] = None) -> dict[str, str]:
    p = Path(path) if path else _DEFAULT_PATH
    if not p.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    out = {}
    for k, v in (data.get("roles") or {}).items():
        if isinstance(k, str) and isinstance(v, str):
            out[k.strip().lower()] = v.strip()
    return out


def role_for(email: str, *, path: Optional[Path] = None) -> Optional[str]:
    if not email:
        return None
    e = email.strip().lower()
    if e in _chap_roles.HARD_CODED_SUPER_ADMINS:
        return SUPER_ADMIN
    return _load(path).get(e)


def can(email: str, action: str, *, path: Optional[Path] = None) -> bool:
    role = role_for(email, path=path)
    if role is None:
        return False
    needed = _ACTIONS.get(action)
    if needed is None:
        return False
    return _ORDER.get(role, 0) >= _ORDER.get(needed, 99)


def list_grants(*, path: Optional[Path] = None) -> list[tuple[str, str]]:
    """All explicitly-granted (email, role) pairs sorted by role rank."""
    return sorted(
        _load(path).items(),
        key=lambda x: (-_ORDER.get(x[1], 0), x[0]),
    )
