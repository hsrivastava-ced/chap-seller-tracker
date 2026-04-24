"""
github_secret_updater.py — update repo secrets + commit files via the GitHub REST API.

Used by the Streamlit admin UI. When an editor adds a new admin panel:

    1. We need to append APP_N_USER / APP_N_PASS lines to the bundled
       `CREDS` secret so the next scheduled scrape has credentials.
    2. We need to commit the updated `apps.yaml` / `roles.yaml` back
       to `main` so the Streamlit app itself redeploys and reads the
       new list.
    3. Optionally, we trigger a fresh workflow run so the editor
       doesn't have to wait until midnight IST to see data.

All three use the same GitHub REST v3 endpoints + a fine-grained PAT
stored in Streamlit secrets as `GH_ADMIN_PAT`. The PAT must be scoped
to this one repo with:

    Contents:  read/write         (commit apps.yaml, roles.yaml)
    Secrets:   read/write         (update CREDS)
    Actions:   read/write         (dispatch workflow)

Why libsodium: GitHub requires secrets to be encrypted with the repo's
libsodium public key before being sent. We use PyNaCl (`pynacl`) to do
that client-side — nothing ever lands in plaintext on GitHub's side
except inside the black-box secrets store.

Everything in this module is network-I/O; nothing to smoke-test without
real creds. The admin UI wraps each call in a try/except and surfaces
errors to the editor inline.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class RepoContext:
    owner: str
    repo: str
    branch: str = "main"
    token: str = ""

    @property
    def base(self) -> str:
        return f"https://api.github.com/repos/{self.owner}/{self.repo}"

    @property
    def headers(self) -> dict:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }


def context_from_streamlit(st) -> RepoContext:
    """Pull [github] config out of Streamlit secrets.

    Expected secrets.toml block:

        [github]
        owner = "your-org-or-user"
        repo  = "chap-seller-tracker"
        branch = "main"
        pat   = "github_pat_..."
    """
    try:
        cfg = dict(st.secrets.get("github", {}))
    except Exception as e:
        raise RuntimeError(
            "Streamlit secrets missing a [github] section — the admin UI "
            "needs owner/repo/pat to commit back to the repo."
        ) from e
    try:
        return RepoContext(
            owner=cfg["owner"],
            repo=cfg["repo"],
            branch=cfg.get("branch", "main"),
            token=cfg["pat"],
        )
    except KeyError as e:
        raise RuntimeError(
            f"[github] secrets block is missing `{e.args[0]}`. Required keys: "
            "owner, repo, pat. Optional: branch (default 'main')."
        )


# ---------------------------------------------------------------------
# Generic single-secret writer (create or update)
# ---------------------------------------------------------------------
def put_repo_secret(ctx: RepoContext, name: str, value: str) -> None:
    """Create or update one repo-level GitHub Actions secret.

    Uses libsodium sealed-box encryption against the repo's public key,
    then PUT /actions/secrets/<name>. GitHub treats absent + present
    the same for PUT, so this covers both first-time creation and
    rotation. Required PAT permission: Secrets: Read and write.

    Named secrets are the right primitive for per-app credentials —
    unlike the monolithic CREDS bundle, each write is idempotent and
    independent, so adding app N+1 can't accidentally stomp app N.
    The scrape workflow reads them back via `toJSON(secrets)`.
    """
    import requests
    from nacl import public  # type: ignore

    r = requests.get(
        f"{ctx.base}/actions/secrets/public-key",
        headers=ctx.headers,
        timeout=15,
    )
    r.raise_for_status()
    pk = r.json()
    pk_bytes = base64.b64decode(pk["key"])

    sealed = public.SealedBox(public.PublicKey(pk_bytes)).encrypt(
        value.encode("utf-8")
    )
    encrypted_b64 = base64.b64encode(sealed).decode("utf-8")

    r = requests.put(
        f"{ctx.base}/actions/secrets/{name}",
        headers=ctx.headers,
        json={"encrypted_value": encrypted_b64, "key_id": pk["key_id"]},
        timeout=15,
    )
    if r.status_code not in (201, 204):
        raise RuntimeError(
            f"GitHub rejected secret `{name}`: {r.status_code} {r.text}"
        )


# ---------------------------------------------------------------------
# CREDS secret update (legacy — kept for historical callers)
# ---------------------------------------------------------------------
def append_creds_lines(ctx: RepoContext, new_lines: list[str]) -> None:
    """Append `KEY="value"` lines to the existing CREDS secret.

    Round-trips the current CREDS value by:
        1. Reading what we THINK is in it from the .env-style cache kept
           in Streamlit session_state (the UI stores this when the
           super-admin first opens the Apps tab).
        2. Appending the new lines.
        3. Encrypting against the repo's libsodium public key.
        4. PUTting it back.

    GitHub's API does NOT let us read a secret's current value back
    (that's a feature, not a bug). So we can only update by setting the
    full new value. The caller must pass `new_lines` as the complete
    set of additions; `_build_new_creds` handles string splicing.

    Security note: `new_lines` are raw credentials. Never log them.
    """
    import requests
    from nacl import encoding, public  # type: ignore

    # Fetch the public key we'll encrypt against.
    r = requests.get(
        f"{ctx.base}/actions/secrets/public-key",
        headers=ctx.headers,
        timeout=15,
    )
    r.raise_for_status()
    pk = r.json()
    pk_bytes = base64.b64decode(pk["key"])

    # Build the new CREDS body. We keep a cached version in session; the
    # admin UI passes it via `current_creds_body` (see build_updated_creds()).
    # Here we just encrypt whatever the caller gave us.
    plaintext = "\n".join(new_lines).encode("utf-8")

    sealed_box = public.SealedBox(public.PublicKey(pk_bytes))
    encrypted = sealed_box.encrypt(plaintext)
    encrypted_b64 = base64.b64encode(encrypted).decode("utf-8")

    r = requests.put(
        f"{ctx.base}/actions/secrets/CREDS",
        headers=ctx.headers,
        json={"encrypted_value": encrypted_b64, "key_id": pk["key_id"]},
        timeout=15,
    )
    if r.status_code not in (201, 204):
        raise RuntimeError(
            f"GitHub rejected CREDS update: {r.status_code} {r.text}"
        )


def build_updated_creds(current_body: str, additions: dict[str, str]) -> str:
    """Produce a new CREDS body string from the current one + new k=v pairs.

    Preserves existing lines verbatim (so formatting, comments, blank
    lines all survive). Only appends new entries.

    Emit format: `KEY=value` with NO quoting. The workflow's CREDS
    parser (`scrape.yml` → Unpack step) strips matching outer quotes if
    present but has no escape mechanism for embedded quotes, so the
    safest wire format is to skip quoting entirely and let Python pick
    up the raw value. This is fine because we never feed CREDS to bash
    `source` anymore — the workflow parses it with `parse_dotenv`.

    Validation:
      - newlines in a value are rejected (they'd break the dotenv format)
      - leading/trailing whitespace is stripped (matches the parser's
        `\\s*` behavior, so surprises are impossible)
    """
    body = current_body.rstrip()
    out_lines = [body] if body else []
    for k, v in additions.items():
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
            raise ValueError(f"invalid env key: {k!r}")
        clean = v.strip()
        if "\n" in clean or "\r" in clean:
            raise ValueError(
                f"value for {k!r} contains a newline; CREDS is line-delimited "
                "and can't represent multi-line secrets"
            )
        out_lines.append(f"{k}={clean}")
    return "\n".join(out_lines) + "\n"


# ---------------------------------------------------------------------
# Commit apps.yaml / roles.yaml
# ---------------------------------------------------------------------
def put_file(
    ctx: RepoContext,
    repo_path: str,
    new_text: str,
    commit_message: str,
    committer_name: str = "chap-admin-ui",
    committer_email: str = "chap-admin-ui@users.noreply.github.com",
) -> None:
    """Upsert a file on `ctx.branch` with `new_text` and commit.

    Implements the "read SHA → PUT with sha" flow required by
    `PUT /contents/{path}`. If the file doesn't exist yet, the SHA is
    omitted (GitHub treats the PUT as a create).
    """
    import requests

    # Step 1: get current SHA (if any)
    sha: Optional[str] = None
    r = requests.get(
        f"{ctx.base}/contents/{repo_path}",
        params={"ref": ctx.branch},
        headers=ctx.headers,
        timeout=15,
    )
    if r.status_code == 200:
        sha = r.json().get("sha")
    elif r.status_code != 404:
        raise RuntimeError(f"GET {repo_path} failed: {r.status_code} {r.text}")

    payload = {
        "message": commit_message,
        "content": base64.b64encode(new_text.encode("utf-8")).decode("utf-8"),
        "branch": ctx.branch,
        "committer": {"name": committer_name, "email": committer_email},
        "author": {"name": committer_name, "email": committer_email},
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(
        f"{ctx.base}/contents/{repo_path}",
        headers=ctx.headers,
        json=payload,
        timeout=20,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"PUT {repo_path} failed: {r.status_code} {r.text}")


def read_file(ctx: RepoContext, repo_path: str) -> str:
    """Read a file's contents from GitHub via Contents API.

    Used by the admin UI to pull down the current apps.yaml / roles.yaml
    to edit. Returns "" if the file doesn't exist.
    """
    import requests
    r = requests.get(
        f"{ctx.base}/contents/{repo_path}",
        params={"ref": ctx.branch},
        headers=ctx.headers,
        timeout=15,
    )
    if r.status_code == 404:
        return ""
    r.raise_for_status()
    content_b64 = r.json().get("content", "")
    return base64.b64decode(content_b64).decode("utf-8")


# ---------------------------------------------------------------------
# Trigger workflow
# ---------------------------------------------------------------------
def trigger_scrape(ctx: RepoContext, reason: str = "onboarding") -> None:
    """POST workflow_dispatch to run scrape-chap now."""
    import requests
    r = requests.post(
        f"{ctx.base}/actions/workflows/scrape.yml/dispatches",
        headers=ctx.headers,
        json={"ref": ctx.branch, "inputs": {"reason": reason}},
        timeout=15,
    )
    if r.status_code not in (204,):
        raise RuntimeError(f"workflow_dispatch failed: {r.status_code} {r.text}")


# ---------------------------------------------------------------------
# Dotenv parse helper (reused from scrape.yml fix)
# ---------------------------------------------------------------------
_DOTENV_LINE = re.compile(
    r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$'
)


def parse_dotenv(text: str) -> dict[str, str]:
    """Same parser the workflow uses — for the UI to introspect CREDS content."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _DOTENV_LINE.match(line)
        if not m:
            continue
        k, v = m.group(1), m.group(2)
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        out[k] = v
    return out
