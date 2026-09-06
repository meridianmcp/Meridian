"""Hooks, connections, and script-serving routes."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .._deps import _hosted_mode
from .. import hook_paths as hook_paths_module

router = APIRouter()


# ---------------------------------------------------------------------------
# Script path helpers
# ---------------------------------------------------------------------------

def _hook_script_path(filename: str) -> Path:
    """Locate a Meridian-owned installer script (hooks.ps1, hooks.sh, ...).

    e5eec33b — the naive ``__file__``-relative candidate breaks the same way
    a non-editable/site-packages install already breaks ``_watcher_script_path``
    below: it resolves to the install directory, not necessarily a real,
    writable checkout. Mirrors that function's existing fallback shape, but
    goes through ``hook_paths.resolve_active_repo_root`` for the fallback
    candidate so both this route module and the active-repository hook
    resolver agree on what "the repo" means (never a bare, unresolved
    fragment) when the primary candidate doesn't exist.
    """
    primary = Path(__file__).parent.parent.parent / filename
    if primary.exists():
        return primary
    fallback_root = hook_paths_module.resolve_active_repo_root(cwd=str(Path.cwd()))
    if fallback_root is not None:
        candidate = fallback_root / filename
        if candidate.exists():
            return candidate
    return primary


def _watcher_script_path(filename: str) -> Path:
    # In Docker (non-editable install), __file__ resolves to site-packages,
    # not /app. WORKDIR is /app and scripts/ lives there, so fall back to cwd.
    repo_root = Path(__file__).resolve().parent.parent.parent
    candidate = repo_root / "scripts" / filename
    if candidate.exists():
        return candidate
    return Path.cwd() / "scripts" / filename


# ---------------------------------------------------------------------------
# Script-serving routes
# ---------------------------------------------------------------------------

@router.get("/install_watcher.ps1")
async def get_install_watcher_ps1() -> PlainTextResponse:
    """a7c43cc1 — serve the claude --rc FileSystemWatcher installer for Windows."""
    script_path = _watcher_script_path("install_watcher.ps1")
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="install_watcher.ps1 not found")
    return PlainTextResponse(script_path.read_text(encoding="utf-8"))


@router.get("/install_watcher.sh")
async def get_install_watcher_sh() -> PlainTextResponse:
    """a7c43cc1 — serve the claude --rc FSEvents/inotify installer for macOS/Linux."""
    script_path = _watcher_script_path("install_watcher.sh")
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="install_watcher.sh not found")
    return PlainTextResponse(script_path.read_text(encoding="utf-8"))


@router.get("/install_tunnel.ps1")
async def get_install_tunnel_ps1() -> PlainTextResponse:
    """e05d0e02 — serve the Windows Task Scheduler installer that keeps
    `meridian --tunnel` alive across logins/reboots."""
    script_path = _watcher_script_path("install_tunnel.ps1")
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="install_tunnel.ps1 not found")
    return PlainTextResponse(script_path.read_text(encoding="utf-8"))


@router.get("/install_tunnel.sh")
async def get_install_tunnel_sh() -> PlainTextResponse:
    """e05d0e02 — serve the macOS LaunchAgent / Linux systemd installer that
    keeps `meridian --tunnel` alive across logins/reboots."""
    script_path = _watcher_script_path("install_tunnel.sh")
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="install_tunnel.sh not found")
    return PlainTextResponse(script_path.read_text(encoding="utf-8"))


@router.get("/hooks.ps1")
async def get_hooks_ps1() -> PlainTextResponse:
    script_path = _hook_script_path("hooks.ps1")
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="hooks.ps1 not found")
    return PlainTextResponse(script_path.read_text(encoding="utf-8"))


@router.get("/hooks.sh")
async def get_hooks_sh() -> PlainTextResponse:
    script_path = _hook_script_path("hooks.sh")
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="hooks.sh not found")
    return PlainTextResponse(script_path.read_text(encoding="utf-8"))


@router.get("/install.sh")
async def get_install_sh() -> PlainTextResponse:
    script_path = _hook_script_path("install.sh")
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="install.sh not found")
    return PlainTextResponse(script_path.read_text(encoding="utf-8"))


@router.get("/install.ps1")
async def get_install_ps1() -> PlainTextResponse:
    script_path = _hook_script_path("install.ps1")
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="install.ps1 not found")
    return PlainTextResponse(script_path.read_text(encoding="utf-8"))


@router.get("/install-windows.ps1")
async def get_install_windows_ps1() -> PlainTextResponse:
    # Installs the standalone meridian.exe binary into ~/.local/bin + user PATH
    # (the Windows counterpart to install.sh). Lives in scripts/, unlike the
    # root-level install.ps1 which installs meridian-connect.
    script_path = _watcher_script_path("install-windows.ps1")
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="install-windows.ps1 not found")
    return PlainTextResponse(script_path.read_text(encoding="utf-8"))


@router.get("/hooks_install.ps1")
async def get_hooks_install_ps1() -> PlainTextResponse:
    # e9f18530 — hooks installer's keyless auth step. Runs the RFC 8628 device
    # authorization grant (POST /oauth/device -> approve at /activate -> poll
    # POST /oauth/token) so the installer never needs a pasted static API key.
    # Distinct from hooks.ps1 (the token-rotating hooks installer).
    script_path = _watcher_script_path("hooks_install.ps1")
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="hooks_install.ps1 not found")
    return PlainTextResponse(script_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Hook configuration diagnostics (34f76536)
# ---------------------------------------------------------------------------

@router.get("/hooks/diagnostics")
async def get_hooks_diagnostics() -> dict[str, Any]:
    """34f76536 — live hook-configuration diagnostics for the active repo.

    Surfaces ``hook_paths.diagnose_configured_hooks`` as a real endpoint
    instead of logic that only tests ever exercised. Classifies every hook
    command configured in the active repo's
    ``.claude/settings.json`` as ``ok`` / a genuine missing REQUIRED project
    hook (``$CLAUDE_PROJECT_DIR``-scoped, must exist in this repo) / an
    OPTIONAL global hook that is legitimately absent until the user runs
    the ``hooks.ps1``/``hooks.sh`` installer on this machine / unresolvable.
    A missing OPTIONAL hook is never treated as a problem here -- only
    ``missing_required_count`` signals a genuine, actionable misconfiguration.

    Never raises: an absent or unreadable settings.json yields
    ``settings_found: false`` with an empty ``hooks`` list rather than a
    404/500, since a diagnostics endpoint must never itself become a source
    of failure for a session that polls it during orientation.
    """
    repo_root = hook_paths_module.resolve_active_repo_root(cwd=str(Path.cwd()))
    settings_path = (repo_root / ".claude" / "settings.json") if repo_root else None
    if settings_path is None or not settings_path.exists():
        return {
            "settings_found": False,
            "settings_path": str(settings_path) if settings_path else None,
            "repo_root": str(repo_root) if repo_root else None,
            "hooks": [],
            "missing_required_count": 0,
        }
    diagnostics = hook_paths_module.diagnose_configured_hooks(
        settings_path, repo_root=repo_root
    )
    missing_required = [
        d for d in diagnostics
        if d["status"] == hook_paths_module.STATUS_MISSING_REQUIRED
    ]
    return {
        "settings_found": True,
        "settings_path": str(settings_path),
        "repo_root": str(repo_root),
        "hooks": diagnostics,
        "missing_required_count": len(missing_required),
    }


# ---------------------------------------------------------------------------
# Connection management routes
# ---------------------------------------------------------------------------

async def _block_non_admin_connection_writes(request: Request) -> None:
    """G1.9 — connection profiles live in the hosted server's meridian.toml.
    Non-admin tenants must not be able to mutate them. Returns 403 cleanly
    instead of the surprising 404 when, e.g., the dashboard tried to
    activate a connection name that doesn't exist in the toml at all.
    """
    if not _hosted_mode():
        return
    from ..hosted import get_current_tenant, is_admin_db  # noqa: PLC0415
    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        raise HTTPException(403, "Sign in to manage connections")
    if not await is_admin_db(tenant.get("email", ""), request.app.state.db):
        raise HTTPException(
            403, "Connection profiles are admin-only on the hosted service"
        )


@router.post("/config/connections")
async def save_connection(body: dict[str, Any], request: Request) -> dict[str, Any]:
    """v1.9.x — save a new connection profile to meridian.toml.

    Body fields:
      * ``name``      — profile name (e.g. "local", "neon")
      * ``type``      — "sqlite" or "postgres"
      * ``url``       — Postgres URL (required when type == "postgres")
      * ``activate``  — if true, set as the active connection (default true)

    Hosted non-admin tenants get 403; the dashboard hides the picker for
    them too, but this is the canonical defense.
    """
    from .. import toml_config as toml_config_module  # noqa: PLC0415
    await _block_non_admin_connection_writes(request)
    name = str(body.get("name", "local")).strip()
    conn_type = body.get("type")  # optional — if omitted, reuse existing
    url = str(body.get("url", "")).strip()
    activate = bool(body.get("activate", True))

    if not name:
        raise HTTPException(400, "name is required")

    # Load existing toml or start fresh.
    data = toml_config_module.load_toml() or {}
    connections: dict[str, dict[str, str]] = {}
    for cname, ccfg in data.get("connections", {}).items():
        connections[cname] = dict(ccfg)

    if conn_type is not None:
        # Creating or updating a connection profile
        if conn_type not in ("sqlite", "postgres"):
            raise HTTPException(400, "type must be 'sqlite' or 'postgres'")
        if conn_type == "postgres" and not url:
            raise HTTPException(400, "url is required for postgres connections")
        new_cfg: dict[str, str] = {"type": conn_type}
        if conn_type == "postgres":
            new_cfg["url"] = url
        connections[name] = new_cfg
    elif name == "env":
        # "env" is the synthetic connection backed by MERIDIAN_DB_URL (hosted).
        # It is never written to meridian.toml and is already the active DB, so
        # re-selecting it (clicking the active connection in the picker) is a
        # no-op rather than a 404.
        return {"ok": True, "connection_name": "env", "restart_required": False}
    elif name not in connections and name != "local":
        raise HTTPException(404, f"connection '{name}' not found in meridian.toml")

    current_default = data.get("default", {}).get("connection", "local")
    toml_config_module.save_toml(
        default_connection=name if activate else current_default,
        connections=connections,
    )
    return {
        "ok": True,
        "connection_name": name,
        "restart_required": activate and conn_type == "postgres",
    }


@router.delete("/config/connections/{name}")
async def delete_connection(name: str, request: Request) -> dict[str, Any]:
    """v1.9.x — remove a named connection profile from meridian.toml.

    Hosted non-admin tenants get 403 (see _block_non_admin_connection_writes).
    """
    from .. import toml_config as toml_config_module  # noqa: PLC0415
    await _block_non_admin_connection_writes(request)
    data = toml_config_module.load_toml() or {}
    connections: dict[str, dict[str, str]] = {
        cname: dict(ccfg)
        for cname, ccfg in data.get("connections", {}).items()
    }
    if name not in connections:
        raise HTTPException(404, f"connection '{name}' not found")
    del connections[name]
    current_default = data.get("default", {}).get("connection", "local")
    # If we deleted the active connection, fall back to local
    if current_default == name:
        current_default = "local"
    toml_config_module.save_toml(
        default_connection=current_default,
        connections=connections,
    )
    return {"ok": True, "deleted": name}


# ---------------------------------------------------------------------------
# Package-install verification gate (23f21820)
# ---------------------------------------------------------------------------

class _PkgGuardRequest(BaseModel):
    command: str


@router.post("/pkg-guard/check")
async def pkg_guard_check(body: _PkgGuardRequest) -> dict[str, Any]:
    """23f21820 -- package-install verification gate.

    Accepts a Bash command string (from the PreToolUse hook), extracts any
    pip/npm/uvx install operations, and checks each package against its
    registry (PyPI JSON API or npm registry). Returns a gate decision:
      - action: "allow" | "warn"
      - message: advisory text (empty when allow)
      - packages: list of extracted package names
      - results: per-package registry check details
      - allowlisted_all: true when every package was in the known-good allowlist

    Fails open: any server-side error returns action="allow" so a Meridian
    availability issue never permanently wedges an executor session.
    """
    from ..pkg_install_guard import gate_command  # noqa: PLC0415

    try:
        result = gate_command(body.command)
    except Exception as exc:
        # Fail open -- never let a bug in the guard block legitimate work.
        return {
            "action": "allow",
            "message": f"pkg-guard internal error (failing open): {exc}",
            "packages": [],
            "results": [],
            "allowlisted_all": True,
        }
    return result
