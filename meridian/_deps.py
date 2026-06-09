"""Shared FastAPI dependencies — imported by both server.py and routes/*.

Extracted here to break circular imports: server.py can include routers from
routes/, and routes/*.py can import helpers from here, without either
importing from the other.

Do NOT import from meridian.server here.
"""
from __future__ import annotations

import os
import sys
import logging
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates


# ---------------------------------------------------------------------------
# Version constants
# ---------------------------------------------------------------------------

def _read_version() -> str:
    v = os.environ.get("MERIDIAN_VERSION", "")
    if v and v != "dev":
        return v
    try:
        import tomllib
        _root = Path(__file__).parent.parent
        for _fname in ("pyproject.toml", "pixi.toml"):
            try:
                with open(_root / _fname, "rb") as _f:
                    data = tomllib.load(_f)
                    ver = (
                        data.get("project", {}).get("version", "")
                        or data.get("workspace", {}).get("version", "")
                        or data.get("version", "")
                    )
                    if ver:
                        return ver
            except FileNotFoundError:
                continue
    except Exception:
        pass
    return "1.0.0-alpha"


def _read_git_sha() -> str:
    env_sha = os.environ.get("MERIDIAN_GIT_SHA", "")
    if env_sha:
        return env_sha[:12]
    try:
        import subprocess
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=Path(__file__).parent.parent,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
        return out or _read_version()
    except Exception:
        return _read_version()


_VERSION = _read_version()
_GIT_SHA = _read_git_sha()
_ASSET_VERSION = f"{_VERSION}-{_GIT_SHA}" if _GIT_SHA != _VERSION else _VERSION


# ---------------------------------------------------------------------------
# Resource path (PyInstaller-aware)
# ---------------------------------------------------------------------------

def _resource_path(relative: str) -> str:
    """Resolve a resource path relative to the package root.

    Works in dev (relative to repo) and in frozen PyInstaller exe.
    """
    base = getattr(sys, "_MEIPASS", Path(__file__).parent.parent)
    return str(Path(base) / relative)


# ---------------------------------------------------------------------------
# Jinja2 templates
# ---------------------------------------------------------------------------

_templates = Jinja2Templates(directory=_resource_path("meridian/templates"))
_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hosted-mode flag
# ---------------------------------------------------------------------------

def _hosted_mode() -> bool:
    """Return True when running as a hosted service (MERIDIAN_HOSTED=1)."""
    return os.environ.get("MERIDIAN_HOSTED", "").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Demo cookie name
# ---------------------------------------------------------------------------

_DEMO_CONTEXT_COOKIE = "meridian_demo"


# ---------------------------------------------------------------------------
# Per-tenant DB cache (module-level singleton dict shared by all importers)
# ---------------------------------------------------------------------------

_tenant_db_cache: dict[str, Any] = {}


async def _open_tenant_db_by_id(request: Request, tenant_id: str) -> Any:
    """Return the cached DB for tenant_id, opening it if not yet cached.

    URL resolution order:
      1. tenant.neon_db_url from auth DB (encrypted, normal path)
      2. MERIDIAN_AUTH_DB env var — Fly secret for admin tenant's project DB.
    """
    if tenant_id in _tenant_db_cache:
        return _tenant_db_cache[tenant_id]
    from . import db as db_module
    from .pg_adapter import init_pg_db
    auth_db = request.app.state.db
    tenant = await db_module.get_tenant_by_id(auth_db, tenant_id)
    if not tenant:
        raise HTTPException(status_code=401, detail="tenant not found")

    url: str | None = None
    if tenant.get("neon_db_url"):
        try:
            url = db_module.decrypt_field(tenant["neon_db_url"]) or None
        except Exception:
            _log.warning(
                "Failed to decrypt neon_db_url for tenant %s; falling back to MERIDIAN_AUTH_DB",
                tenant_id,
            )
            url = None
    if not url:
        url = os.environ.get("MERIDIAN_AUTH_DB") or None
    if not url:
        # Fallback for is_internal admin accounts that have no dedicated DB:
        # use app.state.db (the auth DB) so the dashboard is never blank.
        # This handles the case where an admin signs in at usemeridian.us but
        # their neon_db_url isn't provisioned yet.
        if tenant.get("is_internal"):
            conn = auth_db
            _tenant_db_cache[tenant_id] = conn
            return conn
        raise HTTPException(
            status_code=503,
            detail="tenant database not provisioned — set MERIDIAN_AUTH_DB or run set_tenant_db.py",
        )
    conn = await init_pg_db(url)
    _tenant_db_cache[tenant_id] = conn
    return conn


# ---------------------------------------------------------------------------
# Primary DB resolver for request handlers
# ---------------------------------------------------------------------------

async def _db(request: Request) -> Any:
    """Return the active DB for this request.

    - Demo cookie → demo DB
    - Hosted mode + session cookie → tenant's own Neon DB (cached)
    - Hosted mode + Bearer token → tenant's own Neon DB (cached)
    - Otherwise → app.state.db
    """
    import hashlib as _hashlib
    cached = getattr(request.state, "_db_conn", None)
    if cached is not None:
        return cached

    if request.cookies.get(_DEMO_CONTEXT_COOKIE):
        demo_db = getattr(request.app.state, "demo_db", None)
        if demo_db is not None:
            request.state._db_conn = demo_db
            return demo_db
        # demo_db is not configured. FAIL CLOSED: never fall through to the main
        # DB if it could hold real data. A remote/hosted backend always counts as
        # real data; only a pure-local SQLite self-host may serve its own DB under
        # the demo cookie. db_is_remote is set at startup; default to the env var
        # so we still fail closed if the attribute is somehow absent.
        db_is_remote = getattr(request.app.state, "db_is_remote", None)
        if db_is_remote is None:
            db_is_remote = bool(os.environ.get("MERIDIAN_DB_URL"))
        if _hosted_mode() or db_is_remote:
            raise HTTPException(
                status_code=503,
                detail="Demo DB not configured (set MERIDIAN_DEMO_DB_URL).",
            )
        conn = request.app.state.db
        request.state._db_conn = conn
        return conn

    if _hosted_mode():
        from .hosted import _SESSION_COOKIE, _read_session_cookie
        from . import db as db_module

        current_tenant_id: str | None = None

        cookie_val = request.cookies.get(_SESSION_COOKIE)
        if cookie_val:
            session_id = _read_session_cookie(cookie_val)
            if session_id:
                auth_db = request.app.state.db
                session = await db_module.get_user_session(auth_db, session_id)
                if session:
                    current_tenant_id = session["tenant_id"]

        if current_tenant_id is None:
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]
                token_hash = _hashlib.sha256(token.encode()).hexdigest()
                auth_db = request.app.state.db
                tenant = await db_module.get_tenant_from_token_hash(auth_db, token_hash)
                if tenant:
                    current_tenant_id = tenant["id"]

        if current_tenant_id is not None:
            # Workspace switching: honour X-Workspace-Tenant-Id if the current
            # user is an accepted member of the requested workspace.
            ws_header = request.headers.get("x-workspace-tenant-id", "").strip()
            if ws_header and ws_header != current_tenant_id:
                auth_db = request.app.state.db
                current_tenant = await db_module.get_tenant_by_id(auth_db, current_tenant_id)
                if current_tenant:
                    memberships = await db_module.get_workspaces_for_email(
                        auth_db, current_tenant.get("email", "")
                    )
                    if any(m["tenant_id"] == ws_header for m in memberships):
                        conn = await _open_tenant_db_by_id(request, ws_header)
                        request.state._db_conn = conn
                        return conn

            conn = await _open_tenant_db_by_id(request, current_tenant_id)
            request.state._db_conn = conn
            return conn

    conn = request.app.state.db
    request.state._db_conn = conn
    return conn


# ---------------------------------------------------------------------------
# Tenant identity helper
# ---------------------------------------------------------------------------


async def _get_tenant_from_request(request: Request) -> "dict | None":
    """Return the tenant record from the auth DB for this request, or None.

    Returns None in self-hosted (non-hosted) mode, demo mode, or when the
    request carries no valid auth credential. Callers use this to check plan
    limits without coupling every route to tenant-aware logic.
    """
    import hashlib as _hashlib

    if not _hosted_mode():
        return None
    if request.cookies.get(_DEMO_CONTEXT_COOKIE):
        return None

    from .hosted import _SESSION_COOKIE, _read_session_cookie
    from . import db as db_module

    auth_db = request.app.state.db

    cookie_val = request.cookies.get(_SESSION_COOKIE)
    if cookie_val:
        session_id = _read_session_cookie(cookie_val)
        if session_id:
            session = await db_module.get_user_session(auth_db, session_id)
            if session:
                return await db_module.get_tenant_by_id(auth_db, session["tenant_id"])

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        token_hash = _hashlib.sha256(token.encode()).hexdigest()
        return await db_module.get_tenant_from_token_hash(auth_db, token_hash)

    return None


# ---------------------------------------------------------------------------
# Data directory helper
# ---------------------------------------------------------------------------

def _data_dir(request: Request) -> str:
    """Pull the active data directory off app.state."""
    return request.app.state.data_dir


# ---------------------------------------------------------------------------
# Demo request detection
# ---------------------------------------------------------------------------

def _is_demo_request(request: Request) -> bool:
    """Return True when the request is in demo mode (env flag or cookie)."""
    env_demo = os.environ.get("MERIDIAN_DEMO", "").lower() in ("1", "true", "yes")
    cookie_demo = bool(request.cookies.get(_DEMO_CONTEXT_COOKIE))
    return env_demo or cookie_demo


# ---------------------------------------------------------------------------
# Input size validation
# ---------------------------------------------------------------------------

def validate_input_size(value: str | None, field_name: str, max_chars: int) -> None:
    """Raise HTTPException(400) if value exceeds max_chars. Never truncates silently."""
    if len(value or "") > max_chars:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} exceeds {max_chars} character limit",
        )
