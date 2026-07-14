"""Shared FastAPI dependencies â€” imported by both server.py and routes/*.

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

from .roles import has_perm, PERM_WRITE, PERM_INVITE, PERM_SETTINGS


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


def _read_bundle_hash() -> str:
    """9aba783f — content hash of the dashboard bundle, emitted by build.mjs into
    ``meridian/static/asset-manifest.json``. Used as the bundle's ``?v=`` token so
    every content change busts the browser cache even when no version / git SHA is
    available in prod. Falls back to ``_ASSET_VERSION`` when the manifest is absent
    (e.g. a dev checkout that hasn't run ``node build.mjs`` yet)."""
    import json
    try:
        manifest = Path(__file__).parent / "static" / "asset-manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        h = str(data.get("bundle_hash") or "").strip()
        if h:
            return h
    except Exception:
        pass
    return _ASSET_VERSION


_VERSION = _read_version()
_GIT_SHA = _read_git_sha()
_ASSET_VERSION = f"{_VERSION}-{_GIT_SHA}" if _GIT_SHA != _VERSION else _VERSION
_BUNDLE_HASH = _read_bundle_hash()


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
# Local-filesystem-tool guard (workspace decision 0dedff91, 2026-07-12)
# ---------------------------------------------------------------------------
#
# ARCHITECTURAL LAW: any MCP tool whose job requires touching a path on the
# CALLER's own local filesystem -- not Meridian's own server/database -- must
# be built as a tunnel-spawned local plugin (codebase-memory-mcp, Serena,
# meridian-docs, desktop-commander are the correct pattern), never as a
# handler.py-dispatched server-side function calling os.path.*/open()/os.stat()
# etc. directly on a caller-supplied path. Violated twice before this guard
# existed: ingest_document (832d67af) and search_code_semantic -- both tried
# to open a caller's local path on the SERVER's own filesystem, where it can
# never exist, producing misleading "file/dir not found" errors instead of an
# honest explanation. No path-normalization or error-message fix can solve
# this class of bug -- the two computers are physically different machines.
#
# Every server-side dispatch function that touches a caller-supplied local
# path parameter MUST call this FIRST and return its result verbatim (as the
# tool's error field) when it is not None, instead of attempting the
# filesystem call.

def require_local_fs_access(tool_name: str) -> str | None:
    """Return an honest error string if this process cannot reach the
    caller's local filesystem (hosted mode), else None.

    Call this before any os.path.isdir/exists, open(), os.stat(), or os.walk()
    on a caller-supplied path in a server-side MCP tool dispatch function.
    """
    if _hosted_mode():
        return (
            f"{tool_name} requires access to YOUR local filesystem and cannot "
            "run on hosted Meridian -- the server has no access to your machine. "
            "Use the tunnel-routed equivalent instead (e.g. codebase__search_graph, "
            "extractor__find_symbol, or a meridian-docs/desktop-commander tool)."
        )
    return None


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
      2. MERIDIAN_AUTH_DB env var â€” Fly secret for admin tenant's project DB.
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
        from .tenant_crypto import decrypt_tenant_db_url  # noqa: PLC0415
        try:
            # Dual-read: per-tenant ("v2:") or legacy global key, transparently.
            url = decrypt_tenant_db_url(tenant_id, tenant["neon_db_url"]) or None
        except Exception:
            _log.warning("Failed to decrypt neon_db_url for tenant %s", tenant_id)
            url = None
    # MERIDIAN_AUTH_DB is the admin's project DB — only admin-plan tenants may use it.
    if not url and tenant.get("plan") == "admin":
        url = os.environ.get("MERIDIAN_AUTH_DB") or None
    if not url:
        # Fallback: admin accounts without a dedicated Neon DB use the auth DB directly.
        if tenant.get("plan") == "admin":
            conn = auth_db
            _tenant_db_cache[tenant_id] = conn
            return conn
        raise HTTPException(
            status_code=503,
            detail="tenant database not provisioned - please wait or contact support",
        )
    # 894f7645 — provisioning can race the first request (the DB URL is written
    # but Postgres isn't accepting connections yet). Retry a few times before
    # surfacing the 503 so a freshly-provisioned tenant doesn't hard-fail.
    import asyncio  # noqa: PLC0415
    conn = None
    for _attempt in range(3):
        try:
            conn = await init_pg_db(url)
            break
        except Exception as exc:  # noqa: BLE001
            if _attempt == 2:
                _log.warning(
                    "tenant %s DB init failed after %d attempts: %s",
                    tenant_id, _attempt + 1, exc,
                )
                raise HTTPException(
                    status_code=503,
                    detail="tenant database is provisioning — please retry in a moment",
                )
            await asyncio.sleep(0.5 * (_attempt + 1))
    _tenant_db_cache[tenant_id] = conn
    return conn


# ---------------------------------------------------------------------------
# Primary DB resolver for request handlers
# ---------------------------------------------------------------------------

async def _db(request: Request) -> Any:
    """Return the active DB for this request.

    - Demo cookie â†’ demo DB
    - Hosted mode + session cookie â†’ tenant's own Neon DB (cached)
    - Hosted mode + Bearer token â†’ tenant's own Neon DB (cached)
    - Otherwise â†’ app.state.db
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


async def _scoped_project_ids_for_request(request: Request) -> "list[str] | None":
    """d116642e — listing-only project scoping for the current request.

    Returns the list of project_ids the caller is scoped to when they are a
    *project-scoped* member of the workspace they are currently viewing
    (i.e. they switched into a workspace they were invited to with a specific
    project_id). Returns ``None`` when scoping does not apply — the caller is
    the workspace owner, a workspace-wide member, in self-hosted/demo mode, or
    unauthenticated — meaning every project is listed (current behavior).

    Scoping only fires when an ``X-Workspace-Tenant-Id`` header points the
    caller at a *different* workspace than their own (a true cross-workspace
    view); a caller in their own workspace always sees everything.

    This is listing-only: it filters what the dashboard shows. It does NOT
    block direct-by-ID access to sibling projects in the same workspace —
    airtight per-request enforcement is deferred pending the product decision
    (pin b11c7cf6). Writes stay gated by role enforcement (393eed0a).
    """
    if not _hosted_mode():
        return None
    if request.cookies.get(_DEMO_CONTEXT_COOKIE):
        return None

    ws_header = request.headers.get("x-workspace-tenant-id", "").strip()
    if not ws_header:
        return None

    from . import db as db_module

    caller = await _get_tenant_from_request(request)
    if not caller:
        return None
    caller_email = caller.get("email", "")
    if ws_header == caller.get("id"):
        # Viewing own workspace — never scoped.
        return None

    auth_db = request.app.state.db
    return await db_module.get_scoped_project_ids_for_member(
        auth_db, ws_header, caller_email
    )


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

# e5592013 — non-blocking lint hint surfaced when a note titled "MANUAL …" is
# created: those are almost always human tasks that belong on the sprint board.
_MANUAL_NOTE_LINT = (
    "Heads-up: notes with 'MANUAL' in the title are usually human tasks — "
    "consider add_sprint_item(human_id='adam', milestone_type='human') so it "
    "lands on the sprint board instead of the notes wiki."
)


def validate_input_size(value: str | None, field_name: str, max_chars: int) -> None:
    """Raise HTTPException(400) if value exceeds max_chars. Never truncates silently."""
    if len(value or "") > max_chars:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} exceeds {max_chars} character limit",
        )


# ---------------------------------------------------------------------------
# Rate limiter (slowapi, in-memory, no Redis required)
#
# The Limiter instance lives here so extracted routers can apply ``_rate_limit``
# at import time without importing from server.py (circular). server.py owns the
# one-time registration: ``app.state.limiter`` + the RateLimitExceeded handler.
# ---------------------------------------------------------------------------
try:
    from slowapi import Limiter
    from slowapi.util import get_remote_address

    _limiter = Limiter(key_func=get_remote_address)
except ImportError:  # pragma: no cover - slowapi always installed in prod
    _limiter = None  # type: ignore[assignment]

_RATE_LIMIT = "100/minute"


# ---------------------------------------------------------------------------
# Tier-based per-tenant rate limiting (live-queue hardening, 2b93cb59)
#
# Bearer-token (programmatic / MCP) traffic is metered per tenant per minute by
# plan. Cookie/dashboard, demo, unauthenticated, /health and /static traffic is
# never metered. The state lives here (next to the slowapi limiter) so the
# canonical test reset below clears it too — no cross-test bleed.
# ``None`` means unlimited (pro/admin). The limiter is FAIL-OPEN at the call
# site: any error resolving the tenant or counting hits lets the request pass.
# ---------------------------------------------------------------------------
_TENANT_RL_WINDOW_SECONDS = 60.0
_TENANT_RL_PLAN_TTL_SECONDS = 60.0
_TENANT_RL_PER_MINUTE: dict[str, "int | None"] = {
    "free": 500,
    "standard": 2000,
    "pro": None,
    "admin": None,
}
# tenant_id -> monotonic timestamps of requests within the current window
_tenant_rl_hits: dict[str, list[float]] = {}
# token_hash -> (cached_at_monotonic, (tenant_id, plan))
_tenant_rl_plan_cache: dict[str, "tuple[float, tuple[str, str]]"] = {}

# ---------------------------------------------------------------------------
# 3295c784 — shared (cross-Fly-instance) rate-limit counters.
#
# The default limiter above counts hits in the per-process ``_tenant_rl_hits``
# dict, so across N Fly machines a tenant's effective budget is ~Nx intended.
# When ``MERIDIAN_SHARED_RATE_LIMIT`` is set, the decision path instead uses a
# Postgres atomic counter (mcp_rate_counters) that every instance shares —
# pinned decision 2ad938a0 (Postgres over Redis, no new infra).
#
# The DB round-trip is mitigated by ``_tenant_rl_over_limit``: once a tenant is
# seen over-budget in a given window we short-circuit every later request in
# that same window WITHOUT touching the DB. This dict is process-local (only a
# performance cache — the DB counter is the source of truth) and is cleared by
# ``_reset_tenant_rate_limit`` so tests never bleed across each other.
# Keyed by (tenant_id, window_start_minute) -> True.
# ``MERIDIAN_SHARED_RATE_LIMIT`` defaults OFF: prod behavior is unchanged.
# ---------------------------------------------------------------------------
_tenant_rl_over_limit: dict["tuple[str, int]", bool] = {}


def _shared_rate_limit_enabled() -> bool:
    """Return True when the shared (cross-instance) rate limiter is opted in."""
    return os.environ.get("MERIDIAN_SHARED_RATE_LIMIT", "").lower() in (
        "1", "true", "yes",
    )


def _reset_tenant_rate_limit() -> None:
    """Clear per-tenant rate-limit counters and the token→plan cache."""
    _tenant_rl_hits.clear()
    _tenant_rl_plan_cache.clear()
    _tenant_rl_over_limit.clear()


def _rate_limit(rate: str):
    """Return a decorator that applies *rate* via slowapi, or a no-op when slowapi is absent."""
    def decorator(func):
        if _limiter is None:
            return func
        return _limiter.limit(rate)(func)
    return decorator


def _reset_limiter_storage() -> None:
    """Clear all in-memory rate-limit counters and route registrations.

    Used by tests: since _limiter is a module-level singleton (not re-created
    on importlib.reload(server)), we must flush the storage counters AND the
    route-limit registrations that accumulate across server reloads.
    """
    _reset_tenant_rate_limit()
    if _limiter is None:
        return
    try:
        if hasattr(_limiter, "_storage"):
            _limiter._storage.reset()
    except Exception:
        pass
    try:
        if hasattr(_limiter, "_route_limits"):
            _limiter._route_limits.clear()
        if hasattr(_limiter, "_dynamic_route_limits"):
            _limiter._dynamic_route_limits.clear()
    except Exception:
        pass


def _reset_limiter_counts() -> None:
    """Clear only in-memory rate-limit COUNTERS, not route registrations.

    8a52dd26 -- for callers that reuse the same `app`/`_limiter` across tests
    without importlib.reload(server) between them. Route-limit registrations
    (`_route_limits`/`_dynamic_route_limits`) only happen once, at decorator
    time, and must stay intact; unlike _reset_limiter_storage() (which clears
    them because a reload will re-populate them), clearing them here would
    permanently disable rate limiting after the first test.
    """
    _reset_tenant_rate_limit()
    if _limiter is None:
        return
    try:
        if hasattr(_limiter, "_storage"):
            _limiter._storage.reset()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Authenticated tenant helper (session cookie or Bearer token)
# ---------------------------------------------------------------------------

async def _get_authenticated_tenant(request: Request) -> "dict[str, Any]":
    """Resolve the current hosted tenant from session cookie or bearer token."""
    from .hosted import get_current_tenant, get_tenant_from_bearer

    tenant = None
    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        pass
    if tenant is None:
        try:
            tenant = await get_tenant_from_bearer(request)
        except HTTPException:
            pass
    if tenant is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return tenant


def _mask_api_token_hash(token_hash: str | None) -> str:
    """Return a stable masked display string for a stored API token hash."""
    value = (token_hash or "").strip()
    if len(value) >= 4:
        return f"sk_meridian_••••••••{value[-4:]}"
    return "sk_meridian_••••••••..."


# ---------------------------------------------------------------------------
# Markdown timestamp helpers
# ---------------------------------------------------------------------------

def _md_ts() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _md_one_line(text: str, limit: int = 200) -> str:
    """Collapse a multi-line text body to a single trimmed line."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


# ---------------------------------------------------------------------------
# Context-block renderers (shared by routes/files.py and server.py MCP handler)
# ---------------------------------------------------------------------------

def _render_workspace_block(
    decisions: list[dict], notes: list[dict]
) -> str:
    """v3.1 — render workspace-level decisions + notes as a compact text block.

    Workspace decisions/notes are tenant-global (above any single project), so
    they are prepended to every project's context block + handoff. Returns an
    empty string when there is nothing to show, so callers can skip the join.
    """
    if not decisions and not notes:
        return ""
    lines = ["WORKSPACE (applies to all projects):"]
    for d in decisions[:10]:
        cat = (d.get("category") or "").strip()
        prefix = f"[{cat}] " if cat else ""
        lines.append(f"  • DECISION {prefix}{d.get('title', '')}: {d.get('body', '')}")
    for n in notes[:10]:
        tags = (n.get("tags") or "").strip()
        suffix = f" ({tags})" if tags else ""
        lines.append(f"  • NOTE {n.get('title', '')}: {n.get('body', '')}{suffix}")
    return "\n".join(lines)


def _render_context_block(
    project: dict,
    goal: "dict | None",
    sprint_items: list[dict],
    pending_tasks: list[dict],
    sessions: list[dict],
    recent_decisions: list[str],
    *,
    mode: str = "full",
    repo_root: "str | None" = None,
) -> str:
    """v2.3 — render the project context as a single text block.

    ``mode='full'`` produces the Code Handoff variant — everything a fresh
    Claude Code session needs to continue work without re-deriving state.
    ``mode='chat'`` produces the shorter "new claude.ai conversation"
    variant — drops sessions and trims long fields so a paste into a new
    chat doesn't overflow the first message.

    Returns a plain-text block (no JSON, no markdown fences) suitable for
    direct clipboard copy + paste.
    """
    short_id = project["id"].split("-")[0]
    lines = [f"PROJECT: {project['name']} ({short_id})"]
    if goal:
        if goal.get("north_star"):
            ns = goal["north_star"]
            if mode == "chat" and len(ns) > 400:
                ns = ns[:400].rstrip() + "…"
            lines.append(f"NORTH STAR: {ns}")
        if goal.get("sprint"):
            lines.append(f"SPRINT: {goal['sprint']}")
        if mode == "full" and goal.get("content"):
            vg = goal["content"]
            if isinstance(vg, dict):
                vg = vg.get("content") or str(vg)
            if len(vg) > 2000:
                vg = vg[:2000].rstrip() + "…"
            lines += ["", "VERSION GOAL:", vg]
    if sprint_items:
        lines += ["", "PENDING SPRINT ITEMS:"]
        for it in sprint_items[:10]:
            lines.append(f"- [{it.get('status', '?')}] {it.get('title', '')}")
    if pending_tasks:
        cap = 10 if mode == "full" else 5
        lines += ["", f"RECENT TASKS (last {min(len(pending_tasks), cap)}):"]
        for t in pending_tasks[:cap]:
            stat = (t.get("status") or "?").upper()
            desc = t.get("description") or ""
            if len(desc) > 200:
                desc = desc[:200].rstrip() + "…"
            lines.append(f"- [{stat}] {desc}")
    if mode == "full" and sessions:
        lines += ["", "ACTIVE SESSIONS:"]
        for s in sessions[:5]:
            lines.append(f"- {s.get('name', '?')} ({s.get('status', '?')})")
    if recent_decisions:
        lines += ["", "RECENT DECISIONS:"]
        for d in recent_decisions[-5:]:
            text = d
            if len(text) > 240:
                text = text[:240].rstrip() + "…"
            lines.append(f"- {text}")
    if mode == "full":
        if repo_root:
            lines += ["", f"REPO: {repo_root}"]
        lines += ["TEST: pixi run test"]
    lines += [
        "",
        f"To continue: connect to Meridian and call start_session(project_id=\"{project['id']}\", session_name=\"<name>\").",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Workspace permission check
# ---------------------------------------------------------------------------

async def _require_workspace_perm(
    request: Request, tenant: "dict[str, Any]", perm: str,
) -> str:
    """Resolve the calling user's role in the tenant's workspace and 403 if they lack perm."""
    from . import db as _db_module
    from .hosted import get_current_tenant as _get_ct
    from .roles import has_perm
    try:
        caller = await _get_ct(request)
    except HTTPException:
        caller = None
    if caller is None:
        # Bearer/API-token callers (MCP, scripts) aren't cookie-authenticated.
        caller = await _get_tenant_from_request(request)
    if caller is None:
        raise HTTPException(403, "Sign in required")
    caller_email = caller.get("email", "")
    resolved = await _db_module.resolve_member_role(
        request.app.state.db, tenant["id"], caller_email,
    )
    role = resolved[0] if resolved else None
    if not has_perm(role, perm):
        raise HTTPException(
            403,
            f"Workspace role '{role or 'none'}' lacks permission '{perm}'",
        )
    return role  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 393eed0a — server-side workspace-role enforcement on cross-workspace writes
# ---------------------------------------------------------------------------

# Mutating paths that act on the CALLER'S OWN account (never the active
# workspace) — billing, account, auth/token management — plus pre-auth/public
# endpoints and the per-tool-gated /mcp endpoint. These are exempt from
# workspace-role enforcement even when an X-Workspace-Tenant-Id header rides
# along (e.g. a member viewing an invited workspace clicks "manage billing",
# which still targets THEIR OWN Stripe customer).
_ROLE_ENFORCE_SKIP_PREFIXES: tuple[str, ...] = (
    "/auth/", "/oauth/", "/billing", "/account", "/demo", "/waitlist",
    "/health", "/webhooks/", "/mcp", "/__gate__",
)


def _required_perm_for_request(method: str, path: str) -> "str | None":
    """Map a mutating request to the workspace permission it requires, or None
    to skip role enforcement for this path.

    Only POST/PUT/PATCH/DELETE are gated. Self-scoped and public paths return
    None. Everything else returns the least privilege a member-tier role would
    need: team management → INVITE, workspace/project config → SETTINGS, whole-
    project deletion → SETTINGS, and ordinary project-data writes → WRITE.
    """
    if method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    if any(path.startswith(p) for p in _ROLE_ENFORCE_SKIP_PREFIXES):
        return None
    # Team management — add / remove / modify workspace members.
    if path.startswith("/workspace/invite") or path.startswith("/workspace/members"):
        return PERM_INVITE
    # Workspace / project configuration — a member must not reconfigure an
    # owner's workspace, project settings, notify targets, or GitHub link.
    if path == "/workspace/settings":
        return PERM_SETTINGS
    if path.endswith("/settings") or path.endswith("/ntfy") or "/github/" in path:
        return PERM_SETTINGS
    # Deleting an entire project (DELETE /projects/{id}, not a sub-resource).
    if method == "DELETE" and path.startswith("/projects/") and path.count("/") == 2:
        return PERM_SETTINGS
    # Default: ordinary project-data write (tasks, sessions, sprint, notes,
    # decisions, goal, handoff, …) — members allowed, viewers blocked.
    return PERM_WRITE


async def _enforcement_context(
    request: Request,
) -> "tuple[str, str, str] | None":
    """Return ``(caller_email, active_workspace_tenant_id, role)`` when this
    request operates on a workspace the caller was INVITED to, else ``None``.

    Role enforcement only matters cross-workspace: every tenant is implicitly
    owner of their own workspace. We fast-path with NO database hit when there
    is no ``X-Workspace-Tenant-Id`` header — the overwhelmingly common solo-owner
    case the dashboard never sends a header for. ``None`` is returned for
    self-hosted, demo, unauthenticated, own-workspace, and not-a-member requests
    (none of which need a role gate at this layer).
    """
    if not _hosted_mode():
        return None
    if request.cookies.get(_DEMO_CONTEXT_COOKIE):
        return None
    ws_header = request.headers.get("x-workspace-tenant-id", "").strip()
    if not ws_header:
        return None  # operating on own workspace → owner → no gate, no DB hit
    caller = await _get_tenant_from_request(request)
    if caller is None:
        return None  # unauthenticated → the route's own auth returns 401
    if ws_header == caller.get("id"):
        return None  # header explicitly names the caller's own workspace
    from . import db as _db_module
    resolved = await _db_module.resolve_member_role(
        request.app.state.db, ws_header, caller.get("email", ""),
    )
    if resolved is None:
        # Not an accepted member of ws_header → _db() will NOT switch the active
        # DB to it, so the write lands on the caller's own DB where they are
        # owner. Nothing to gate here.
        return None
    return (caller.get("email", ""), ws_header, resolved[0])
