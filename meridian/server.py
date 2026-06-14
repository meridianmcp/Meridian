"""FastAPI HTTP server and MCP stdio server for Meridian.

This module exposes two surfaces backed by the same async SQLite database:

* A FastAPI app (``app``) reachable on port 7878 by default. Used by the
  demo script and any HTTP client.
* An MCP server (built in :func:`build_mcp_server`) reachable via stdio.
  Wired up in :mod:`meridian.__main__` and consumed by Claude Desktop /
  Claude Code / Cursor / Windsurf.
"""

from __future__ import annotations

import hashlib
import html as html_module
import json
import asyncio
import math
import os
import re
import signal
import time
from contextlib import asynccontextmanager
from pathlib import Path

# ---------------------------------------------------------------------------
# Shared helpers (templates, _db, _hosted_mode, etc.) live in _deps.py so
# that routes/ modules can import them without circular-importing server.py.
# ---------------------------------------------------------------------------
from ._deps import (
    _VERSION,
    _ASSET_VERSION,
    _GIT_SHA,
    _resource_path,
    _templates,
    _hosted_mode,
    _DEMO_CONTEXT_COOKIE,
    _tenant_db_cache,
    _open_tenant_db_by_id,
    _db,
    _data_dir,
    _is_demo_request,
    _get_tenant_from_request,
    validate_input_size,
    _limiter,
    _rate_limit,
    _get_authenticated_tenant,
    _mask_api_token_hash,
    _md_ts,
    _md_one_line,
    _require_workspace_perm,
    _render_workspace_block,
    _render_context_block,
)


def _load_meridian_md() -> str:
    """v2.3 — load the MERIDIAN.md session-instructions file.

    Resolution order:
    1. ``MERIDIAN.md`` at the repo root (project-specific override)
    2. ``meridian/MERIDIAN.md`` bundled with the package (built-in default)
    3. Empty string if neither exists.

    Returns the file contents as a string. Cheap to call repeatedly — the
    file is tiny and the OS page cache makes re-reads nearly free.
    """
    pkg_dir = Path(__file__).parent
    repo_root = pkg_dir.parent
    for candidate in (repo_root / "MERIDIAN.md", pkg_dir / "MERIDIAN.md"):
        try:
            if candidate.exists():
                return candidate.read_text(encoding="utf-8")
        except OSError:
            continue
    return ""


from typing import Any

import aiosqlite
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import dashboard as dashboard_module
from . import db as db_module
from .executor_config import (
    build_executor_config_block,
    executor_config_for_output,
    normalize_executor_config,
)
from . import goal_md as goal_md_module
from . import enqueue as enqueue_module
from . import handoff as handoff_module
from . import toml_config as toml_config_module
from . import md_anchors as md_anchors_module
from . import git_md as git_md_module
from .demo_seed import _seed_demo_data, _seed_decisions_from_file
from .models import (
    ClaimTaskRequest,
    ClaimTaskResponse,
    EnqueueTask,
    FileContent,
    GoalModeSet,
    GoalSet,
    GoalState,
    HandoffResult,
    Project,
    ProjectCreate,
    ProjectSettings,
    ProjectSettingsPatch,
    Session,
    SessionRegister,
    SetNorthStarRequest,
    SetSprintRequest,
    StartSessionRequest,
    Task,
    TaskCreate,
    TaskUpdate,
    WorktreeCreate,
)
from .mcp_tools import _MCP_TOOLS_LIST, _TOOL_EXAMPLES, _READ_ONLY_TOOLS as _mcp_readonly_tools

# Default on-disk location. Overridable via MERIDIAN_DB env var, which the
# test suite uses to redirect to ``:memory:``.
DEFAULT_DB_PATH = os.environ.get(
    "MERIDIAN_DB", str(Path("data") / "meridian.db")
)
DEFAULT_DATA_DIR = Path(
    os.environ.get("MERIDIAN_DATA_DIR", "data")
)

_DEMO_COOKIE = "meridian_demo_access"
_DEMO_COOKIE_MAX_AGE = 24 * 3600


def _check_demo_cookie(request: Request) -> bool:
    """Return True when the demo access cookie is present and valid."""
    from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

    secret = (
        os.environ.get("SESSION_SECRET")
        or os.environ.get("MERIDIAN_SESSION_SECRET")
        or "demo-fallback-secret"
    )
    token = request.cookies.get(_DEMO_COOKIE, "")
    if not token:
        return False
    try:
        URLSafeTimedSerializer(secret).loads(token, max_age=_DEMO_COOKIE_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired, Exception):  # noqa: BLE001
        return False


def _demo_gate_html(error: str = "") -> str:
    """Return the password gate page HTML."""
    err_html = f'<div class="err">{error}</div>' if error else '<div class="err"></div>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Meridian — Preview Access</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d0d0d;color:#e2e8f0;font-family:'IBM Plex Mono',monospace,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center}}
.card{{background:#161616;border:1px solid #2d2d2d;border-radius:8px;padding:40px;max-width:380px;width:100%;margin:20px}}
.logo{{font-size:22px;font-weight:600;color:#a78bfa;margin-bottom:6px}}
.sub{{color:#6b7280;font-size:12px;margin-bottom:28px}}
.err{{color:#f87171;font-size:12px;margin-bottom:12px;min-height:18px}}
label{{display:block;color:#9ca3af;font-size:11px;margin-bottom:6px}}
input{{width:100%;background:#0d0d0d;border:1px solid #2d2d2d;color:#e2e8f0;padding:8px 12px;border-radius:4px;font-family:inherit;font-size:13px;margin-bottom:16px;outline:none}}
input:focus{{border-color:#7c3aed}}
button{{width:100%;background:#7c3aed;color:#fff;border:none;padding:10px;border-radius:4px;cursor:pointer;font-size:13px;font-family:inherit}}
button:hover{{background:#6d28d9}}
.req{{text-align:center;margin-top:16px;font-size:11px;color:#6b7280}}
.req a{{color:#a78bfa;text-decoration:none}}
.req a:hover{{text-decoration:underline}}
</style>
</head>
<body>
<div class="card">
  <div class="logo">&#x1f9ed; Meridian</div>
  <div class="sub">Preview access required</div>
  {err_html}
  <form method="POST" action="/demo-auth">
    <label>Access password</label>
    <input type="password" name="password" autofocus placeholder="Enter password">
    <button type="submit">Enter preview &#x2192;</button>
  </form>
  <div class="req">Request access: <a href="mailto:hello@usemeridian.us">hello@usemeridian.us</a></div>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Session keepalive — keep a busy session's ``last_seen`` fresh.
#
# ``last_seen`` only advances when a session makes an MCP tool call (the
# implicit bump in :func:`build_mcp_server`). A session that goes heads-down on
# non-MCP work — git, bash, file edits — makes no calls for minutes, so its
# ``last_seen`` drifts past the 10-minute live window and a second session's
# coordination check mistakes it for dead and starts on the same files.
#
# Fix: every tool call marks the session "connected"; a background loop then
# refreshes last_seen for connected sessions every minute, for as long as they
# stay within SESSION_KEEPALIVE_TTL_S of their last call. Sessions idle past
# the TTL are forgotten so genuinely-dead ones still expire on schedule.
# ---------------------------------------------------------------------------
SESSION_KEEPALIVE_INTERVAL_S = int(os.environ.get("MERIDIAN_KEEPALIVE_INTERVAL_S", "60"))
SESSION_KEEPALIVE_TTL_S = int(os.environ.get("MERIDIAN_KEEPALIVE_TTL_S", "600"))

# session_id -> monotonic timestamp of the last activity that proved liveness.
_CONNECTED_SESSIONS: dict[str, float] = {}


def _mark_session_connected(session_id, now=None) -> None:
    """Record that *session_id* just proved it's alive (any tool call) so the
    keepalive loop holds its ``last_seen`` fresh through quiet, non-MCP work."""
    if not session_id:
        return
    _CONNECTED_SESSIONS[session_id] = time.monotonic() if now is None else now


async def _keepalive_connected_sessions(db, now=None, ttl_s=SESSION_KEEPALIVE_TTL_S):
    """Refresh ``last_seen`` for every connected session still within *ttl_s*
    of its last activity; forget the rest. Returns the ids refreshed.

    Split out from the loop so a single tick can be driven from tests with an
    explicit clock."""
    if now is None:
        now = time.monotonic()
    fresh, stale = [], []
    for sid, ts in list(_CONNECTED_SESSIONS.items()):
        (fresh if now - ts <= ttl_s else stale).append(sid)
    for sid in stale:
        _CONNECTED_SESSIONS.pop(sid, None)
    if fresh:
        try:
            await db_module.keepalive_sessions(db, fresh)
        except Exception:  # noqa: BLE001 — a failed bump must not kill the loop
            pass
    return fresh


async def _run_session_keepalive_loop(db) -> None:
    """Periodically refresh connected sessions. Started by both the FastAPI
    lifespan (hosted/HTTP clients) and the stdio entrypoint (local clients) so
    a busy session never looks dead to a coordinating one regardless of how it
    connected."""
    while True:
        try:
            await asyncio.sleep(SESSION_KEEPALIVE_INTERVAL_S)
            await _keepalive_connected_sessions(db)
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001 — never let the loop die
            continue


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the SQLite connection on startup, close it on shutdown.

    Also: load environment variables from ``./.env`` if present so the
    dashboard chat proxy can find ``ANTHROPIC_API_KEY``. We do this here
    rather than at import time so test fixtures can override the env
    without an .env file leaking into them.
    """
    # psycopg3 requires SelectorEventLoop on Windows.
    # Uvicorn resets the loop policy in its worker process — patch it here
    # before any psycopg3 pool creation happens.
    import sys as _sys
    if _sys.platform == "win32":
        import selectors as _sel
        import asyncio as _aio
        _loop = _aio.get_event_loop()
        if not isinstance(_loop, _aio.SelectorEventLoop):
            _new_loop = _aio.SelectorEventLoop(_sel.SelectSelector())
            _aio.set_event_loop(_new_loop)
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=False)
    except ImportError:
        pass  # dotenv is optional — env can be set by the launcher.

    data_dir = Path(os.environ.get("MERIDIAN_DATA_DIR", str(DEFAULT_DATA_DIR)))
    data_dir.mkdir(parents=True, exist_ok=True)

    # v1.9.x — meridian.toml connection profiles. Loaded before env check so
    # MERIDIAN_DB_URL env always wins (CI/containers).  Skipped for :memory:
    # so the test suite isn't affected.
    _db_override = os.environ.get("MERIDIAN_DB", DEFAULT_DB_PATH)
    if _db_override != ":memory:":
        _toml_url, _toml_conn_name = toml_config_module.get_toml_db_url()
        if _toml_url:
            # toml explicitly says use postgres — set env var
            os.environ["MERIDIAN_DB_URL"] = _toml_url
        elif _toml_conn_name is not None:
            # toml explicitly says use sqlite (local) — clear env var override
            os.environ.pop("MERIDIAN_DB_URL", None)
        # else: no toml at all — respect existing env var or use SQLite default

    db_url = os.environ.get("MERIDIAN_DB_URL")
    db_path: str | None = None
    if db_url:
        db = await db_module.init_db(db_url)
    else:
        db_path = os.environ.get("MERIDIAN_DB", DEFAULT_DB_PATH)
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        db = await db_module.init_db(db_path)
    app.state.db = db
    # True when the main DB is a remote/Postgres backend (env URL or toml conn).
    # The demo DB resolver fails closed against this so /demo never serves real data.
    app.state.db_is_remote = bool(db_url)
    app.state.data_dir = str(data_dir)
    app.state.ws_broadcaster = dashboard_module.WebSocketBroadcaster()
    await _hydrate_oauth_cache(db)

    # v2.2 — isolated demo DB.
    # Priority: MERIDIAN_DEMO_DB_URL → MERIDIAN_STANDARD_KEY (legacy secret
    # name on the hosted Fly app) → in-memory SQLite fallback.
    # NEVER falls through to production DB.
    demo_db_url = (
        os.environ.get("MERIDIAN_DEMO_DB_URL")
        or os.environ.get("MERIDIAN_STANDARD_KEY")
    )

    async def _init_demo(url: str) -> None:
        demo_db = await db_module.init_db(url)
        app.state.demo_db = demo_db
        # Skip seeding when using an external Postgres demo DB —
        # it was seeded manually via scripts/seed_demo.py
        # Only seed in-memory SQLite fallback

    async def _init_demo_inmemory() -> None:
        demo_db = await db_module.init_db(":memory:")
        app.state.demo_db = demo_db
        await _seed_demo_data(demo_db)

    skip_demo = os.environ.get("MERIDIAN_SKIP_DEMO", "").lower() in ("1", "true", "yes")

    if skip_demo:
        app.state.demo_db = None
    elif demo_db_url:
        try:
            # 30s timeout — Neon cold-start can take ~15-20s; 30s gives it room
            await asyncio.wait_for(_init_demo(demo_db_url), timeout=30.0)
        except Exception:  # noqa: BLE001
            try:
                await asyncio.wait_for(_init_demo_inmemory(), timeout=5.0)
            except Exception:  # noqa: BLE001
                app.state.demo_db = None
    else:
        try:
            await asyncio.wait_for(_init_demo_inmemory(), timeout=5.0)
        except Exception:  # noqa: BLE001
            app.state.demo_db = None

    # Guard: MERIDIAN_DEMO=true requires MERIDIAN_DEMO_DB_URL to prevent
    # accidental production DB exposure via the demo route.
    if os.environ.get("MERIDIAN_DEMO", "").lower() in ("1", "true", "yes"):
        if not os.environ.get("MERIDIAN_DEMO_DB_URL") and not os.environ.get("MERIDIAN_SKIP_DEMO"):
            import logging as _log
            _log.getLogger(__name__).warning(
                "MERIDIAN_DEMO=true but MERIDIAN_DEMO_DB_URL not set — "
                "using in-memory SQLite demo DB. Set MERIDIAN_DEMO_DB_URL for persistence."
            )

    # v0.4.2 — periodic auto-summary task. Interval comes from env so
    # tests can run it on a sub-second cadence; default is ten minutes.
    interval_s = float(os.environ.get("MERIDIAN_AUTO_SUMMARY_INTERVAL", 600))
    app.state.last_storage_check_ts = 0.0  # epoch seconds

    async def _auto_summary_loop() -> None:
        import time as _time
        while True:
            try:
                await asyncio.sleep(interval_s)
                await db_module.run_auto_summary_cycle(db)
                # Archive sessions silent for 24h+ so dead runs drop out of the
                # active list and release any in_progress tasks they were holding.
                try:
                    await db_module.expire_inactive_sessions(db)
                except Exception:  # noqa: BLE001
                    pass
                # v1.0.1 — PID watchdog: mark orphaned in_progress tasks as failed
                try:
                    stale = await db_module.get_in_progress_tasks_with_pid(db)
                    for t in stale:
                        pid = t.get("worker_pid")
                        if pid is None:
                            continue
                        try:
                            os.kill(int(pid), 0)  # 0 = check existence only
                        except (ProcessLookupError, PermissionError, OSError):
                            # PID is dead — mark the task failed
                            # OSError covers Windows WinError 87 for non-existent PIDs
                            await db_module.update_task(
                                db, t["id"],
                                status="failed",
                                description=(
                                    f"[claude-error] worker process died "
                                    f"unexpectedly (PID {pid})"
                                ),
                            )
                except Exception:  # noqa: BLE001
                    pass
                # v1.0 — hourly storage overage check (hosted only)
                if os.environ.get("MERIDIAN_HOSTED", "").lower() in ("1", "true", "yes"):
                    now_ts = _time.monotonic()
                    if now_ts - app.state.last_storage_check_ts >= 3600:
                        app.state.last_storage_check_ts = now_ts
                        try:
                            from .hosted import run_storage_overage_check
                            await run_storage_overage_check(db)
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            from .hosted import run_dunning_cleanup
                            await run_dunning_cleanup(db)
                        except Exception:  # noqa: BLE001
                            pass
                        # Daily at 3am: compute + storage overage check
                        import time as _time2
                        _local_hour = _time2.localtime().tm_hour
                        if _local_hour == 3:
                            try:
                                from .hosted import run_overage_check
                                await run_overage_check(db)
                            except Exception:  # noqa: BLE001
                                pass
                # v2.4 — refresh the CLAUDE.md <current_state> block for
                # every project on each cycle. Local dev only — skipped
                # on hosted multi-tenant deployments where there is no
                # repo-root CLAUDE.md to write.
                if os.environ.get("MERIDIAN_HOSTED", "").lower() not in ("1", "true", "yes"):
                    try:
                        await _refresh_claude_md_current_state(db, _REPO_ROOT)
                    except Exception:  # noqa: BLE001
                        pass
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001 — never let the loop die
                continue

    summary_task = asyncio.create_task(_auto_summary_loop())
    app.state.auto_summary_task = summary_task

    # v2.5 — seed decisions_pinned from DECISIONS.md on first run
    if db_path is None or db_path != ":memory:":
        try:
            projects = await db_module.list_projects(db)
            for _p in projects:
                await _seed_decisions_from_file(db, _p["id"])
        except Exception:  # noqa: BLE001
            pass

    # v0.6.3 — GOAL.md startup sync: if the file exists and names a known
    # project, pull its contents into the DB before serving any requests.
    if db_path is None or db_path != ":memory:":
        try:
            await goal_md_module.sync_goal_md_to_db(db)
        except Exception:  # noqa: BLE001
            pass

    # v0.6.3 — optional live file-watch (no-op when watchfiles not installed,
    # and skipped on Windows due to ProactorEventLoop deadlock with awatch).
    if db_path != ":memory:":
        watch_task = asyncio.create_task(goal_md_module.watch_goal_md(db))
    else:
        async def _noop_watch() -> None:
            pass
        watch_task = asyncio.create_task(_noop_watch())
    app.state.watch_task = watch_task

    # v1.7.0 — server update detection: hash key files at startup,
    # recheck every 60s, broadcast update_available to all WS clients if changed.
    def _server_hash() -> str:
        h = hashlib.md5()
        for rel in [
            "meridian/server.py",
            "meridian/db.py",
            "meridian/static/dashboard.js",
        ]:
            try:
                h.update(Path(rel).read_bytes())
            except OSError:
                pass
        return h.hexdigest()

    app.state.startup_hash = _server_hash()

    async def _version_check_loop() -> None:
        while True:
            try:
                await asyncio.sleep(60)
                if _server_hash() != app.state.startup_hash:
                    db_module.publish_global({"type": "update_available"})
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                continue

    version_task = asyncio.create_task(_version_check_loop())
    app.state.version_task = version_task

    keepalive_task = asyncio.create_task(_run_session_keepalive_loop(db))
    app.state.keepalive_task = keepalive_task

    try:
        yield

    finally:
        summary_task.cancel()
        watch_task.cancel()
        version_task.cancel()
        keepalive_task.cancel()
        try:
            await summary_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        try:
            await watch_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        try:
            await keepalive_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await db.close()


app = FastAPI(
    title="Meridian",
    description="Multi-session Claude coordinator MCP server.",
    version="0.1.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Routers — extracted route modules (routes/ package)
# Include these BEFORE middleware so route matching is registered correctly.
# ---------------------------------------------------------------------------
from .routes.notes import router as _notes_router            # noqa: E402
from .routes.hitl import router as _hitl_router              # noqa: E402
from .routes.sprint import router as _sprint_router          # noqa: E402
from .routes.sessions import router as _sessions_router      # noqa: E402
from .routes.tasks import router as _tasks_router            # noqa: E402
from .routes.decisions import router as _decisions_router    # noqa: E402
from .routes.handoff import router as _handoff_router        # noqa: E402
from .routes.admin import router as _admin_router            # noqa: E402
from .routes.workspace import router as _workspace_router    # noqa: E402
from .routes.auth import router as _auth_router              # noqa: E402
from .routes.files import router as _files_router            # noqa: E402
from .routes.export import router as _export_router          # noqa: E402
from .routes.github import router as _github_router          # noqa: E402
from .routes.billing import router as _billing_router        # noqa: E402
from .routes.hooks import router as _hooks_router            # noqa: E402
from .routes.projects import router as _projects_router      # noqa: E402
from .mcp.http_handler import router as _mcp_http_router     # noqa: E402
from .mcp.http_handler import (                              # noqa: E402
    _hydrate_oauth_cache,
    _oauth_token_hash,
    _get_oauth_token_from_db,
)
from .mcp import http_handler as _mcp_http_handler           # noqa: E402

app.include_router(_notes_router)
app.include_router(_hitl_router)
app.include_router(_sprint_router)
app.include_router(_sessions_router)
app.include_router(_tasks_router)
app.include_router(_decisions_router)
app.include_router(_handoff_router)
app.include_router(_admin_router)
app.include_router(_workspace_router)
app.include_router(_auth_router)
app.include_router(_files_router)
app.include_router(_export_router)
app.include_router(_github_router)
app.include_router(_billing_router)
app.include_router(_hooks_router)
app.include_router(_projects_router)
app.include_router(_mcp_http_router)

# ---------------------------------------------------------------------------
# Password gate middleware
# ---------------------------------------------------------------------------
_GATE_COOKIE = "meridian_site_access"
_GATE_MAX_AGE = 24 * 3600

def _gate_html(error=""):
    err = f'<p style="color:#f87171;margin-top:8px;font-size:13px">{error}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Meridian</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d0d0f;color:#e8eaf0;font-family:-apple-system,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center}}
.card{{background:#16181c;border:1px solid #2a2d35;border-radius:10px;padding:40px;max-width:360px;width:100%;margin:20px}}
.logo{{font-size:20px;font-weight:700;color:#6c8fff;margin-bottom:4px}}
.sub{{color:#8b8fa8;font-size:12px;margin-bottom:28px}}
label{{display:block;font-size:13px;color:#8b8fa8;margin-bottom:6px}}
input{{width:100%;padding:10px 12px;background:#0d0d0f;border:1px solid #2a2d35;border-radius:6px;color:#e8eaf0;font-size:14px;outline:none}}
input:focus{{border-color:#6c8fff}}
button{{width:100%;margin-top:14px;padding:11px;background:#6c8fff;border:none;border-radius:6px;color:#fff;font-weight:700;font-size:14px;cursor:pointer}}</style>
</head><body><div class="card">
<div class="logo">⬡ Meridian</div>
<div class="sub">Preview access required</div>
<form method="post" action="/__gate__">
<label>Password</label>
<input type="password" name="password" autofocus placeholder="Enter preview password">
{err}<button type="submit">Continue</button></form></div></body></html>"""

@app.middleware("http")
async def site_password_gate(request: Request, call_next):
    site_pw = os.environ.get("SITE_PASSWORD", "")
    if not site_pw:
        return await call_next(request)
    path = request.url.path
    if path in ("/health", "/failover-status", "/mcp/health", "/__gate__", "/config", "/static", "/mcp/tools-doc", "/mcp/quickstart", "/mcp/sse", "/mcp", "/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource", "/hooks/session-start", "/hooks/stop") or path.startswith("/static/") or path.startswith("/oauth/") or path.startswith("/status/") or path == "/demo" or path.startswith("/demo/"):
        return await call_next(request)
    # Demo cookie bypasses site password gate — demo users don't go through __gate__
    if request.cookies.get(_DEMO_CONTEXT_COOKIE):
        return await call_next(request)
    if _hosted_mode():
        from .hosted import _SESSION_COOKIE, _read_session_cookie
        from . import db as db_module
        auth_db = request.app.state.db
        cookie_val = request.cookies.get(_SESSION_COOKIE, "")
        if cookie_val:
            session_id = _read_session_cookie(cookie_val)
            if session_id and await db_module.get_user_session(auth_db, session_id):
                return await call_next(request)
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            import hashlib

            token_hash = hashlib.sha256(auth_header[7:].encode()).hexdigest()
            if await db_module.get_tenant_from_token_hash(auth_db, token_hash):
                return await call_next(request)
            # Also check OAuth tokens (ChatGPT and other OAuth clients use these)
            oauth_hash = _oauth_token_hash(auth_header[len("Bearer "):].strip())
            if _mcp_http_handler._oa_tokens.get(oauth_hash) or await _get_oauth_token_from_db(auth_db, oauth_hash):
                return await call_next(request)
    from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
    from fastapi.responses import HTMLResponse
    secret = os.environ.get("SESSION_SECRET", "fallback")
    token = request.cookies.get(_GATE_COOKIE, "")
    valid = False
    if token:
        try:
            URLSafeTimedSerializer(secret).loads(token, max_age=_GATE_MAX_AGE)
            valid = True
        except Exception:
            valid = False
    if not valid:
        return HTMLResponse(_gate_html())
    return await call_next(request)

@app.post("/__gate__")
async def gate_submit(request: Request):
    from fastapi.responses import HTMLResponse, RedirectResponse
    from itsdangerous import URLSafeTimedSerializer
    site_pw = os.environ.get("SITE_PASSWORD", "")
    secret = os.environ.get("SESSION_SECRET", "fallback")
    form = await request.form()
    entered = form.get("password", "")
    if entered != site_pw:
        return HTMLResponse(_gate_html("Incorrect password"))
    token = URLSafeTimedSerializer(secret).dumps("granted")
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(_GATE_COOKIE, token, max_age=_GATE_MAX_AGE, httponly=True, samesite="lax")
    return response

# v2.0 — Rate limiter (slowapi) — limiter singleton lives in _deps.py
# ---------------------------------------------------------------------------
try:
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    if _limiter is not None:
        app.state.limiter = _limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
except ImportError:
    pass
_RATE_LIMIT = "100/minute"


# G4.15 — Safety-limit exception → 429
from . import limits as _limits_module  # noqa: E402, PLC0415


@app.exception_handler(_limits_module.LimitExceeded)
async def _limit_exceeded_handler(request: Request, exc: _limits_module.LimitExceeded):  # noqa: ARG001
    from fastapi.responses import JSONResponse  # noqa: PLC0415
    return JSONResponse(
        status_code=429,
        content={
            "detail": str(exc),
            "kind": exc.kind,
            "limit": exc.limit,
            "current": exc.current,
        },
    )


# ---------------------------------------------------------------------------
# X-Request-ID middleware + global exception handler
# ---------------------------------------------------------------------------

import uuid as _uuid
import logging as _logging

_req_id_logger = _logging.getLogger("meridian.server")


@app.middleware("http")
async def _request_id_middleware(request: Request, call_next):
    """Attach a uuid4 X-Request-ID to every response and request state."""
    req_id = str(_uuid.uuid4())
    request.state.request_id = req_id
    # Peek at body for MCP initialize — must be done before call_next consumes it
    _is_mcp_init = False
    if request.url.path in ("/mcp", "/mcp/sse") and request.method == "POST":
        try:
            _raw = await request.body()
            if b'"initialize"' in _raw:
                _is_mcp_init = True
        except Exception:
            pass
    response = await call_next(request)
    response.headers["X-Request-ID"] = req_id
    # MCP 2025-03-26: return Mcp-Session-Id on initialize so Claude Code can load tools
    if _is_mcp_init:
        response.headers["Mcp-Session-Id"] = req_id
    return response


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    req_id = getattr(request.state, "request_id", "unknown")
    _req_id_logger.exception(
        "unhandled exception on %s %s (request_id=%s)",
        request.method,
        request.url.path,
        req_id,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content={"error": "internal server error", "request_id": req_id},
    )


# ---------------------------------------------------------------------------
# Custom 404 / 500 error pages
# ---------------------------------------------------------------------------

_ERROR_PAGE_STYLE = (
    "body{background:#0b0c0e;color:#fff;font-family:'IBM Plex Mono',ui-monospace,"
    "'Cascadia Code','Fira Mono',monospace;min-height:100vh;display:flex;"
    "align-items:center;justify-content:center;margin:0}"
    ".card{text-align:center}"
    "h1{font-size:3rem;margin:0 0 0.5rem}"
    "p{color:#aaa;margin:0 0 1.5rem}"
    "a{color:#6c8fff;text-decoration:none}"
    "a:hover{text-decoration:underline}"
)


def _error_page(code: int, message: str) -> HTMLResponse:
    html = (
        "<!doctype html><html lang='en'><head>"
        "<meta charset='utf-8'>"
        f"<title>{code}</title>"
        f"<style>{_ERROR_PAGE_STYLE}</style>"
        "</head><body><div class='card'>"
        f"<h1>{code}</h1>"
        f"<p>{message}</p>"
        "<a href='/'>&#8592; back to home</a>"
        "</div></body></html>"
    )
    return HTMLResponse(content=html, status_code=code)


@app.exception_handler(404)
async def _404_handler(request: Request, exc: Exception) -> HTMLResponse:  # noqa: ARG001
    return _error_page(404, "not found")


@app.exception_handler(500)
async def _500_handler(request: Request, exc: Exception) -> HTMLResponse:  # noqa: ARG001
    return _error_page(500, "something went wrong")


# ---------------------------------------------------------------------------
# v2.0-fixes — Demo read-only middleware (MERIDIAN_DEMO=true)
# ---------------------------------------------------------------------------

_DEMO_WRITE_ALLOWLIST = {"/__gate__", "/mcp/sse"}
_DEMO_WRITE_ALLOWLIST_PREFIXES = ("/auth/", "/demo", "/waitlist", "/health")
_DEMO_CONTEXT_COOKIE = "meridian_demo"


@app.middleware("http")
async def _body_size_guard_middleware(request: Request, call_next):
    """G4.15 — reject requests with bodies past the safety threshold before
    they reach a handler. Trusts Content-Length when present; this is the
    standard cheap fast-fail, with the actual body-stream cutoff handled by
    Starlette / uvicorn at a much higher absolute cap.
    """
    if request.method in ("POST", "PUT", "PATCH"):
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                size = int(cl)
            except ValueError:
                size = -1
            if size > 0:
                try:
                    _limits_module.check_body_bytes(size)
                except _limits_module.LimitExceeded as exc:
                    from fastapi.responses import JSONResponse  # noqa: PLC0415
                    return JSONResponse(
                        status_code=429,
                        content={
                            "detail": str(exc),
                            "kind": exc.kind,
                            "limit": exc.limit,
                            "current": exc.current,
                        },
                    )
    return await call_next(request)


# Content-Security-Policy for the MCP JSON-RPC surface. The /mcp endpoints
# serve only JSON / SSE — never executable HTML — so a deny-all policy is both
# correct and required for the OpenAI Apps SDK submission, which flags any MCP
# route lacking a CSP header.
_MCP_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"


@app.middleware("http")
async def _mcp_csp_middleware(request: Request, call_next):
    """Stamp a strict Content-Security-Policy on all /mcp route responses."""
    response = await call_next(request)
    if request.url.path == "/mcp" or request.url.path.startswith("/mcp/"):
        response.headers["Content-Security-Policy"] = _MCP_CSP
    return response


def _tenant_marker_from_request(request: Request) -> str | None:
    """Item 39 — lightweight tenant identifier for error context.

    Returns a truncated cookie/token marker. No DB lookup — the alerting path
    must stay synchronous-cheap so it never blocks request flow on Postgres.
    """
    cookie = request.cookies.get("meridian_session", "")
    if cookie:
        return f"session:{cookie[:12]}"
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return f"token:{auth[7:19]}"
    return None


@app.middleware("http")
async def _error_tracking_middleware(request: Request, call_next):
    """Item 39 — log unhandled exceptions and feed the 5xx counter.

    Wraps every request:
      - If the route handler raises, log route+tenant+traceback, record the
        synthetic 500 for the rolling window, and return a clean 500 envelope.
      - If the response status >= 500, record it.

    The recording is async-safe but the alert dispatch itself is fired off
    via ``asyncio.create_task`` inside ``record_5xx`` so the response is never
    delayed by ntfy/Resend I/O.
    """
    from . import error_alerting as _ea  # noqa: PLC0415
    tenant = _tenant_marker_from_request(request)
    route = request.url.path or "/"
    try:
        response = await call_next(request)
    except Exception as exc:  # noqa: BLE001
        import logging as _log  # noqa: PLC0415
        import traceback as _tb  # noqa: PLC0415
        _log.getLogger(__name__).error(
            "unhandled %s on %s %s (tenant=%s)\n%s",
            type(exc).__name__,
            request.method,
            route,
            tenant,
            _tb.format_exc(),
        )
        await _ea.record_5xx(route, tenant, 500)
        from fastapi.responses import JSONResponse  # noqa: PLC0415
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error"},
        )
    if response.status_code >= 500:
        await _ea.record_5xx(route, tenant, response.status_code)
    return response


@app.middleware("http")
async def _demo_read_only_middleware(request: Request, call_next):
    """Block all mutating requests when MERIDIAN_DEMO=true or demo cookie set.

    /auth/*, /demo*, /waitlist, and /health are always allowed so OAuth,
    the password gate, and health checks function in demo mode.
    """
    env_demo = os.environ.get("MERIDIAN_DEMO", "").lower() in ("1", "true", "yes")
    cookie_demo = bool(request.cookies.get(_DEMO_CONTEXT_COOKIE))
    path = request.url.path
    allowed = (
        path in _DEMO_WRITE_ALLOWLIST
        or any(path.startswith(p) for p in _DEMO_WRITE_ALLOWLIST_PREFIXES)
    )
    if (
        (env_demo or cookie_demo)
        and request.method in ("POST", "PUT", "PATCH", "DELETE")
        and not allowed
    ):
        return Response(
            content=json.dumps({"error": "demo_readonly", "message": "Read-only demo — sign in for full access"}),
            status_code=403,
            media_type="application/json",
        )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Item 39 — error alerting test endpoint
# ---------------------------------------------------------------------------


@app.get("/admin/__error_test")
async def trigger_test_error(kind: str = "exception") -> Any:
    """Force an error response to exercise the 5xx counter + admin alerting.

    Gated by ``MERIDIAN_ENABLE_ERROR_TEST=1`` so it only exists on preview /
    staging — never on prod. ``kind=exception`` (default) raises an unhandled
    Exception so the middleware logs the traceback and synthesizes a 500.
    ``kind=500`` returns a raw HTTPException(500) so the middleware records
    the response status without going through the exception path.
    """
    if os.environ.get("MERIDIAN_ENABLE_ERROR_TEST", "").lower() not in ("1", "true", "yes"):
        raise HTTPException(status_code=404)
    if kind == "500":
        raise HTTPException(status_code=500, detail="forced 500 for alert drill")
    raise RuntimeError("forced unhandled exception for alert drill")


# ---------------------------------------------------------------------------
# v1.0.2 — Static files + Jinja2 templates
# ---------------------------------------------------------------------------

class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles that disables browser caching.

    Assets are cache-busted via the ?v={asset_version} query param, but a
    stale dashboard.js/css cached without revalidation defeats that. Forcing
    no-cache makes the browser revalidate every load, so a deploy is picked
    up immediately.
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


app.mount(
    "/static",
    _NoCacheStaticFiles(directory=_resource_path("meridian/static")),
    name="static",
)

# Per-tenant DB connections cached by tenant_id (opened on first use, never closed).
# The dict lives in _deps.py; the import above brings it into this namespace.


# _open_tenant_db_by_id, _db, _data_dir — imported from ._deps above.


# ---------------------------------------------------------------------------
# FastAPI endpoints
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def landing_page(request: Request) -> HTMLResponse:
    """Landing page — headline, CTAs, waitlist form.

    When DEMO_PASSWORD is set the page is behind a password gate so the
    preview URL can be shared without exposing the live dashboard publicly.
    """
    if os.environ.get("DEMO_PASSWORD"):
        if not _check_demo_cookie(request):
            return HTMLResponse(_demo_gate_html())
    stripe_payment_link = os.environ.get("STRIPE_PAYMENT_LINK", "/auth/login")
    stripe_pro_checkout = "/checkout?plan=pro" if os.environ.get("STRIPE_PRO_PRICE_ID") else ""
    stripe_pro_payment_link = os.environ.get("STRIPE_PRO_PAYMENT_LINK", stripe_pro_checkout)
    resp = _templates.TemplateResponse(
        request, "landing.html", {
            "stripe_payment_link": stripe_payment_link,
            "stripe_pro_checkout": stripe_pro_checkout,
            "stripe_pro_payment_link": stripe_pro_payment_link,
        }
    )
    resp.headers["Cache-Control"] = "no-cache, no-store"
    return resp


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt() -> str:
    return "User-agent: *\nAllow: /\nSitemap: https://usemeridian.us/sitemap.xml"


@app.get("/sitemap.xml")
async def sitemap_xml() -> Response:
    today = "2026-06-09"
    urls = ["/", "/demo", "/pricing", "/install-mcp"]
    items = "\n".join(
        f"  <url><loc>https://usemeridian.us{u}</loc>"
        f"<lastmod>{today}</lastmod><changefreq>weekly</changefreq></url>"
        for u in urls
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{items}\n"
        "</urlset>"
    )
    return Response(content=xml, media_type="application/xml")


@app.post("/demo-auth")
async def demo_auth_post(request: Request):
    """Validate demo password and set a signed access cookie.

    On success redirects to /dashboard; on failure re-renders the gate page
    with an error message.  Exempt from the MERIDIAN_DEMO read-only
    middleware so this endpoint is reachable in preview mode.
    """
    from fastapi.responses import RedirectResponse
    from itsdangerous import URLSafeTimedSerializer

    form = await request.form()
    password = str(form.get("password", ""))
    expected = os.environ.get("DEMO_PASSWORD", "")
    if not expected or password != expected:
        return HTMLResponse(
            _demo_gate_html("Incorrect password. Please try again."),
            status_code=401,
        )
    secret = (
        os.environ.get("SESSION_SECRET")
        or os.environ.get("MERIDIAN_SESSION_SECRET")
        or "demo-fallback-secret"
    )
    token = URLSafeTimedSerializer(secret).dumps("demo")
    resp = RedirectResponse(url="/dashboard", status_code=302)
    resp.set_cookie(
        _DEMO_COOKIE,
        token,
        max_age=_DEMO_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return resp


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "service": "meridian"}


@app.get("/failover-status")
async def failover_status() -> dict[str, bool]:
    """ITEM 7 — report whether this instance is serving in failover mode.

    Driven by the ``MERIDIAN_IS_FAILOVER`` env var so the dashboard can show a
    standby-region banner. Public + unauthenticated so the banner can render
    before the user signs in.
    """
    flag = (os.environ.get("MERIDIAN_IS_FAILOVER") or "").strip().lower()
    return {"is_failover": flag in ("1", "true", "yes", "on")}


# Live status shields (/status/*) are defined further below — the canonical
# rate-limited implementation lives next to the cached _MCP_TOOL_COUNT.


@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request) -> HTMLResponse:
    """Static Terms of Service page."""
    return _templates.TemplateResponse(request, "terms.html")


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request) -> HTMLResponse:
    """Static Privacy Policy page."""
    return _templates.TemplateResponse(request, "privacy.html")


@app.get("/changelog", response_class=HTMLResponse)
async def changelog_page(request: Request) -> HTMLResponse:
    """Public changelog rendered from DB — newest entries first.

    Falls back to DEVLOG.md when no DB entries exist (bootstrap / self-hosted).
    Admin users see inline controls to add/edit/delete entries.
    """
    db = await _db(request)
    entries = await db_module.list_changelog_entries(db)
    is_admin = False
    if _hosted_mode():
        try:
            from .hosted import get_current_tenant, is_admin_db
            tenant = await get_current_tenant(request)
            is_admin = await is_admin_db(tenant.get("email", ""), db)
        except Exception:  # noqa: BLE001
            pass

    if not entries:
        # Bootstrap: render DEVLOG.md when the DB table is empty
        devlog_path = Path(__file__).parent.parent / "DEVLOG.md"
        raw = devlog_path.read_text(encoding="utf-8") if devlog_path.exists() else ""
        parts = re.split(r"\n(?=## )", raw)
        for part in parts:
            if not part.startswith("## "):
                continue
            lines = part.split("\n", 1)
            title = lines[0][3:].strip()
            body = lines[1].strip() if len(lines) > 1 else ""
            body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", body)
            body = re.sub(r"`([^`]+)`", r"<code>\1</code>", body)
            entries.append({"id": None, "version": None, "title": title,
                            "body": body, "published_at": None})
        entries.reverse()

    return _templates.TemplateResponse(
        request, "changelog.html",
        {"entries": entries, "is_admin": is_admin},
    )


@app.get("/api/changelog-entries")
async def api_changelog_entries(request: Request) -> dict:
    """Return changelog entries as JSON."""
    db = await _db(request)
    entries = await db_module.list_changelog_entries(db)
    return {"entries": entries}


@app.post("/api/admin/changelog-entries")
async def api_create_changelog_entry(request: Request) -> dict:
    """Admin-only: create a new changelog entry."""
    if not _hosted_mode():
        raise HTTPException(status_code=403, detail="admin API only available in hosted mode")
    from .hosted import get_current_tenant, is_admin_db
    tenant = await get_current_tenant(request)
    db = await _db(request)
    if not await is_admin_db(tenant.get("email", ""), db):
        raise HTTPException(status_code=403, detail="admin only")
    body_data = await request.json()
    title = (body_data.get("title") or "").strip()
    body = (body_data.get("body") or "").strip()
    version = (body_data.get("version") or "").strip() or None
    published_at = (body_data.get("published_at") or "").strip() or None
    if not title:
        raise HTTPException(status_code=422, detail="title is required")
    entry = await db_module.create_changelog_entry(db, title, body, version, published_at)
    return entry


@app.patch("/api/admin/changelog-entries/{entry_id}")
async def api_update_changelog_entry(request: Request, entry_id: str) -> dict:
    """Admin-only: update a changelog entry."""
    if not _hosted_mode():
        raise HTTPException(status_code=403, detail="admin API only available in hosted mode")
    from .hosted import get_current_tenant, is_admin_db
    tenant = await get_current_tenant(request)
    db = await _db(request)
    if not await is_admin_db(tenant.get("email", ""), db):
        raise HTTPException(status_code=403, detail="admin only")
    body_data = await request.json()
    entry = await db_module.update_changelog_entry(
        db, entry_id,
        title=body_data.get("title"),
        body=body_data.get("body"),
        version=body_data.get("version"),
        published_at=body_data.get("published_at"),
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")
    return entry


@app.delete("/api/admin/changelog-entries/{entry_id}")
async def api_delete_changelog_entry(request: Request, entry_id: str) -> dict:
    """Admin-only: delete a changelog entry."""
    if not _hosted_mode():
        raise HTTPException(status_code=403, detail="admin API only available in hosted mode")
    from .hosted import get_current_tenant, is_admin_db
    tenant = await get_current_tenant(request)
    db = await _db(request)
    if not await is_admin_db(tenant.get("email", ""), db):
        raise HTTPException(status_code=403, detail="admin only")
    deleted = await db_module.delete_changelog_entry(db, entry_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="entry not found")
    return {"deleted": True}


@app.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request) -> HTMLResponse:
    """Pricing page — Free / Solo / Team tiers with waitlist forms when hosted launch is pending."""
    solo_link = os.environ.get("STRIPE_PAYMENT_LINK", "/auth/login?signup=1")
    team_link = os.environ.get("STRIPE_PRO_PAYMENT_LINK", "/auth/login?signup=1")
    launch_open = bool(os.environ.get("MERIDIAN_LAUNCH_OPEN"))
    waitlist_mode = not launch_open and not bool(os.environ.get("STRIPE_PAYMENT_LINK"))
    email = ""
    try:
        from .hosted import _SESSION_COOKIE, _read_session_cookie
        cookie_val = request.cookies.get(_SESSION_COOKIE)
        if cookie_val:
            session_id = _read_session_cookie(cookie_val)
            if session_id:
                db = await _db(request)
                user_session = await db_module.get_user_session(db, session_id)
                if user_session:
                    tenant = await db_module.get_tenant_by_id(db, user_session["tenant_id"]) or {}
                    email = tenant.get("email", "")
                    if email and not waitlist_mode:
                        sep = "&" if "?" in solo_link else "?"
                        solo_link += f"{sep}prefilled_email={email}"
                        sep = "&" if "?" in team_link else "?"
                        team_link += f"{sep}prefilled_email={email}"
    except Exception:
        pass
    return _templates.TemplateResponse(request, "pricing.html", {
        "solo_link": solo_link,
        "team_link": team_link,
        "waitlist_mode": waitlist_mode,
        "email": email,
    })


@app.get("/install-mcp", response_class=HTMLResponse)
async def install_mcp_page(request: Request) -> HTMLResponse:
    """Onboarding page: step-by-step guide to connect Claude to Meridian via MCP.

    Shows copy-ready Name + URL fields for both local and hosted configs.
    Token generation calls POST /auth/tokens client-side.
    Linked from README, docs quickstart, and dashboard Settings tab.
    """
    resp = _templates.TemplateResponse(request, "install_mcp.html", {})
    resp.headers["Cache-Control"] = "no-cache, no-store"
    return resp


# v0.9 — Magic-link request (rate-limited — must stay in server.py; slowapi
# does not wire @_rate_limit correctly for routes included via APIRouter)
@app.post("/auth/magic")
@_rate_limit("5/minute")
async def auth_magic_request(request: Request):
    """v0.9 — request a magic-link email. Rate-limited.

    Body: ``{"email": "user@example.com"}``. Sends a single-use signed
    link via Resend. Idempotent within the 24-hour token window — if a
    valid unused token exists for this email, returns success without
    sending a duplicate email.
    """
    from .hosted import auth_magic_request as _impl
    return await _impl(request)


@app.get("/config")
async def server_config(request: Request) -> dict[str, Any]:
    """v0.6.5 — expose runtime configuration to the dashboard.

    Allows the frontend to be location-agnostic: it reads the server URL
    from this endpoint rather than hardcoding localhost. Also used by the
    PyInstaller exe launcher to confirm the server is up.

    Fields:
      * ``server_url`` — the absolute URL the frontend should target.
        Defaults to ``http://{host}:{port}`` but is overridable via
        ``MERIDIAN_SERVER_URL`` for hosted / reverse-proxied deployments.
      * ``host`` / ``port`` — split form, useful for tooling.
      * ``version`` — current Meridian version, surfaced in the dashboard
        title bar so a user can see which build they're talking to.
      * ``db`` — ``memory`` or ``sqlite``; lets the dashboard show a
        scratch-DB warning during tests.
    """
    host = os.environ.get("MERIDIAN_HOST", "127.0.0.1")
    port = int(os.environ.get("MERIDIAN_PORT", "7878"))
    default_url = f"http://{host}:{port}"
    server_url = os.environ.get("MERIDIAN_SERVER_URL", default_url)
    # Demo requests reflect the cookie, not just the env flag — otherwise a /demo
    # visitor on a self-host gets the real connection switcher + project list
    # (split-brain). When in demo mode, never expose the host's connection config.
    is_demo = _is_demo_request(request)
    if is_demo:
        return {
            "server_url": server_url,
            "host": host,
            "port": port,
            "version": _VERSION,
            "db": "demo",
            "demo_db": "postgres" if os.environ.get("MERIDIAN_DEMO_DB_URL") else "sqlite",
            "db_host": "",
            "toml_exists": False,
            "toml_path": "",
            "connection_name": "demo",
            "connections": [],
            "demo_mode": True,
            "stripe_payment_link": os.environ.get("STRIPE_PAYMENT_LINK", "/pricing"),
        }
    _, conn_name = toml_config_module.get_active_db_url()
    # Parse hostname from MERIDIAN_DB_URL for the connection label (1f92d344)
    import re as _re_cfg
    _raw_db_url = os.environ.get("MERIDIAN_DB_URL", "")
    _db_host = ""
    if _raw_db_url:
        _m = _re_cfg.search(r"@([^/:?]+)", _raw_db_url)
        if _m:
            _db_host = _m.group(1)
            if len(_db_host) > 22:
                _db_host = _db_host[:20] + "…"
    return {
        "server_url": server_url,
        "host": host,
        "port": port,
        "version": _VERSION,
        "db": (
            "postgres" if os.environ.get("MERIDIAN_DB_URL")
            else "memory" if os.environ.get("MERIDIAN_DB") == ":memory:"
            else "sqlite"
        ),
        "db_host": _db_host,
        "toml_exists": toml_config_module.toml_exists(),
        "toml_path": str(toml_config_module._toml_path() or (Path.cwd() / "meridian.toml")),
        "connection_name": conn_name,
        "connections": toml_config_module.list_connections(),
        "demo_mode": False,
        "stripe_payment_link": os.environ.get("STRIPE_PAYMENT_LINK", "/pricing"),
    }


@app.get("/tools")
async def list_tools_endpoint() -> list[dict[str, Any]]:
    """Return MCP tool definitions for the dashboard Docs vtab."""
    return _MCP_TOOLS_LIST


# ---------------------------------------------------------------------------
# v1.0.0-alpha — public status/shields endpoints (sprint item 29b33fdb)
# ---------------------------------------------------------------------------
# shields.io endpoint-badge JSON (https://shields.io/badges/endpoint-badge).
# No auth, read-only, rate-limited to 1 req/5s per IP so the public badges
# can't be used to hammer the DB. /status/hooks is intentionally omitted until
# the registered_hostnames table (OAuth-hooks item) lands.
_MCP_TOOL_COUNT = len(_MCP_TOOLS_LIST)


def _status_rate_limit(func):
    """Apply the 1-req/5s/IP shields rate limit when slowapi is available.

    No-op passthrough when slowapi isn't installed (self-host minimal deploy),
    so the endpoints still function — just without the per-IP cap.
    """
    if _limiter is None:
        return func
    return _limiter.limit("1/5 seconds")(func)


@app.get("/status/server")
@_status_rate_limit
async def status_server(request: Request) -> dict[str, Any]:
    """shields.io badge: server liveness."""
    return {
        "schemaVersion": 1,
        "label": "meridian",
        "message": "online",
        "color": "brightgreen",
    }


@app.get("/status/tools")
@_status_rate_limit
async def status_tools(request: Request) -> dict[str, Any]:
    """shields.io badge: MCP tool count (cached at startup)."""
    return {
        "schemaVersion": 1,
        "label": "MCP tools",
        "message": f"{_MCP_TOOL_COUNT} tools",
        "color": "6c8fff",
    }


@app.get("/status/sessions")
@_status_rate_limit
async def status_sessions(request: Request) -> dict[str, Any]:
    """shields.io badge: count of currently-live sessions."""
    db = request.app.state.db
    try:
        n = await db_module.count_active_sessions(db)
    except Exception:
        n = 0
    return {
        "schemaVersion": 1,
        "label": "active sessions",
        "message": f"{n} live",
        "color": "brightgreen" if n else "lightgrey",
    }


@app.get("/me")
async def me_endpoint(request: Request) -> dict[str, Any]:
    """Return the current user's plan info. Returns {} for anonymous/self-hosted."""
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        return {}
    from datetime import datetime, timezone
    plan = tenant.get("plan") or "standard"
    expires_raw = tenant.get("inactivity_expires_at")
    days_remaining: int | None = None
    expired = False
    if expires_raw:
        try:
            expires_dt = datetime.strptime(expires_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            delta = expires_dt - datetime.now(timezone.utc)
            days_remaining = max(0, delta.days)
            expired = delta.total_seconds() <= 0
        except ValueError:
            pass
    # G2.10 — internal tenants never see the "expired" / "days remaining"
    # banner. The lifecycle jobs already skip them, but a positive UX cue
    # is cleaner than leaving the expired flag set with no consequence.
    if tenant.get("is_internal"):
        expired = False
        days_remaining = None
    return {
        "plan": plan,
        "email": tenant.get("email", ""),
        "trial_started_at": tenant.get("trial_started_at"),
        "inactivity_expires_at": expires_raw,
        "days_remaining": days_remaining,
        "expired": expired,
        # G2.11 — tells the dashboard whether to render "Manage billing"
        # (true → opens Stripe portal) or "Upgrade" (false → /pricing).
        "has_stripe_customer": bool(tenant.get("stripe_customer_id")),
        # G2.10 — internal marker, used by the dashboard to suppress
        # the upgrade banner and similar nag UI for staff accounts.
        "is_internal": bool(tenant.get("is_internal")),
        "is_admin": tenant.get("email", "") in _admin_emails(),
    }


@app.get("/me/workspaces")
async def me_workspaces(request: Request) -> list[dict[str, Any]]:
    """Return all workspaces the current user belongs to.

    Always includes the user's own workspace as the first entry (is_own=true).
    Followed by any workspaces they've accepted an invite to.
    Returns [] in self-hosted mode.
    """
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        return []
    own = {
        "tenant_id": tenant["id"],
        "owner_email": tenant.get("email", ""),
        "role": "owner",
        "is_own": True,
    }
    invited = await db_module.get_workspaces_for_email(
        request.app.state.db, tenant.get("email", "")
    )
    result = [own]
    for m in invited:
        if m.get("tenant_id") != tenant["id"]:
            result.append({
                "tenant_id": m["tenant_id"],
                "owner_email": m.get("owner_email", ""),
                "role": m.get("role", "member"),
                "is_own": False,
            })
    return result


# Project CRUD + project-level routes → meridian/routes/projects.py


async def _send_email_notification(to_email: str, subject: str, body_text: str) -> None:
    """Send a notification email via Resend.

    Silently skips if ``RESEND_API_KEY`` is not set. Raises ``HTTPException``
    with Resend's response body when the API rejects the request so callers
    like ``/notify/test`` can surface the real failure reason.
    """
    import httpx as _httpx

    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        return  # dev mode — skip
    base = os.environ.get("MERIDIAN_BASE_URL", "https://usemeridian.us").rstrip("/")
    from_addr = os.environ.get("MERIDIAN_FROM_EMAIL", "Meridian <noreply@usemeridian.us>")
    html_body = (
        f"<p>{body_text}</p>"
        f"<p><a href='{base}/dashboard'>Open Meridian dashboard →</a></p>"
        f"<hr style='border:none;border-top:1px solid #e5e7eb;margin:16px 0'>"
        f"<p style='color:#9ca3af;font-size:12px'>You're receiving this because you configured "
        f"<code>{to_email}</code> as your Meridian notification address. "
        f"<a href='{base}/dashboard'>Update in Settings →</a></p>"
    )
    async with _httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": from_addr,
                    "to": [to_email],
                    "subject": subject,
                    "text": body_text,
                    "html": html_body,
                },
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Resend request failed: {exc}") from exc
        if resp.status_code < 200 or resp.status_code >= 300:
            raise HTTPException(status_code=resp.status_code, detail=_response_error_detail(resp))


def _response_error_detail(resp: Any) -> str:
    """Best-effort extraction of a useful error message from an HTTP response."""
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        payload = None
    if isinstance(payload, dict):
        for key in ("detail", "message", "error"):
            value = payload.get(key)
            if isinstance(value, dict):
                value = value.get("detail") or value.get("message") or value.get("error")
            if value:
                return str(value)
        return json.dumps(payload, ensure_ascii=False)
    if payload is not None:
        return str(payload)
    text = getattr(resp, "text", "")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return f"request failed with status {getattr(resp, 'status_code', 'unknown')}"


_NOTIFICATION_PREF_DEFAULTS: dict[str, bool] = {
    "hitl": True,
    "stalled": True,
    "storage": True,
    "sprint": True,
}


def _notification_prefs_from_raw(raw: Any) -> dict[str, bool]:
    """Normalize notification prefs to a complete {pref: bool} mapping."""
    prefs = dict(_NOTIFICATION_PREF_DEFAULTS)
    if isinstance(raw, dict):
        payload = raw
    else:
        try:
            payload = json.loads(raw or "{}")
        except Exception:  # noqa: BLE001
            payload = {}
    if isinstance(payload, dict):
        for key in _NOTIFICATION_PREF_DEFAULTS:
            if key in payload:
                prefs[key] = bool(payload[key])
    return prefs


def _notification_pref_enabled(
    tenant: dict[str, Any] | None,
    pref_key: str | None,
) -> bool:
    """Return True when the given preference is enabled or unset."""
    if pref_key is None:
        return True
    if tenant is None:
        return True
    prefs = _notification_prefs_from_raw(tenant.get("notification_prefs"))
    return bool(prefs.get(pref_key, True))


async def _dispatch_notification(
    notify_url: str, title: str, body_text: str, event: str = "notification"
) -> None:
    """Route a notification based on the URL format.

    Routing rules:
    - Contains ``@`` and no ``/``: treat as email → send via Resend.
    - Contains ``ntfy.sh`` or starts with ``ntfy://``: POST ntfy-style
      (body = plain text, Title/Priority/Tags headers).
    - Anything else (``https://…``): POST JSON webhook (Slack, Discord,
      custom webhooks all understand ``{"title": …, "body": …}``).

    Raises on error — callers that want best-effort should catch.
    """
    import httpx as _httpx

    url = notify_url.strip()
    if not url:
        return

    # Email address — no slashes, has @
    if "@" in url and "/" not in url:
        subject = f"[Meridian] {title}"
        await _send_email_notification(url, subject, body_text)
        return

    async with _httpx.AsyncClient(timeout=5.0) as client:
        if "ntfy.sh" in url or url.startswith("ntfy://"):
            # ntfy protocol: body = plain text, special headers carry metadata
            resp = await client.post(
                url,
                content=body_text.encode(),
                headers={"Title": title, "Priority": "high", "Tags": event},
            )
        else:
            # Generic webhook: POST JSON (compatible with Slack incoming webhooks,
            # Discord webhooks via {content: …}, and custom HTTP receivers)
            resp = await client.post(
                url,
                json={
                    "title": title,
                    "body": body_text,
                    "event": event,
                    "source": "meridian",
                },
            )
        if resp.status_code < 200 or resp.status_code >= 300:
            raise HTTPException(status_code=resp.status_code, detail=_response_error_detail(resp))


async def _notify_project(
    db: Any, project_id: str, title: str, body_text: str, event: str = "notification"
) -> None:
    """Best-effort notification for a project.  Silently ignores all errors.

    Fires to ntfy_url AND notify_email independently — both are tried even if
    one fails.
    """
    # ntfy_url / webhook channel
    try:
        notify_url = await db_module.get_project_ntfy_url(db, project_id)
        if notify_url:
            # G1.7 — stored ntfy values are topic-only; reconstruct the full URL
            # at dispatch time. Anything else (email / non-ntfy webhook) is
            # already in its dispatchable form.
            if "://" not in notify_url and "@" not in notify_url:
                notify_url = f"https://ntfy.sh/{notify_url}"
            await _dispatch_notification(notify_url, title, body_text, event)
    except Exception as exc:  # noqa: BLE001
        # 11064ab0 — log instead of swallowing silently; silent failures are why
        # there was "no evidence" a ntfy ping ever fired.
        import logging as _l
        _l.getLogger("meridian.notify").warning(
            "ntfy notification failed for project %s (event=%s): %s", project_id, event, exc
        )
    # notify_email channel — fired independently so ntfy failure doesn't block email
    try:
        notify_email = await db_module.get_project_notify_email(db, project_id)
        if notify_email:
            await _send_email_notification(notify_email, f"[Meridian] {title}", body_text)
    except Exception as exc:  # noqa: BLE001
        import logging as _l
        _l.getLogger("meridian.notify").warning(
            "email notification failed for project %s (event=%s): %s", project_id, event, exc
        )


async def _on_hitl_answered(
    db: Any, request_row: dict[str, Any], *, approved: bool
) -> dict[str, Any]:
    """Side-effect for an answered/dismissed HITL request.

    For ``kind='md_section_update'`` that was approved, apply the proposed
    markdown section replacement and record the touched file (committed later at
    checkpoint via :func:`_finalize_session_md`). Never raises — any failure is
    returned as ``apply_error`` so the answer itself still succeeds. Legacy
    ``'question'`` requests are a no-op.
    """
    kind = (request_row or {}).get("kind")

    if kind == "hook_project_select" and approved:
        # Store hostname → project mapping so future hooks auto-route
        raw = request_row.get("payload")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (ValueError, TypeError):
            payload = {}
        hostname = (payload.get("hostname") or "").strip()
        answer = (request_row.get("answer") or "")
        projects_in_payload = payload.get("projects") or []
        chosen = next((p for p in projects_in_payload if p.get("name") == answer), None)
        if chosen and hostname:
            try:
                cfg = await db_module.get_executor_config(db, chosen["id"])
                hostnames = cfg.get("hostnames") or []
                norm_hn = hostname.lower()
                if not any(h.get("hostname", "").lower() == norm_hn for h in hostnames):
                    hostnames.append({"hostname": hostname, "auto_add_cwds": False})
                    cfg["hostnames"] = hostnames
                    await db_module.set_executor_config(db, chosen["id"], cfg)
            except Exception:  # noqa: BLE001
                pass
        return {}

    if kind != "md_section_update":
        return {}
    if not approved:
        return {"applied": False, "reason": "rejected"}
    raw = request_row.get("payload")
    try:
        payload = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {"applied": False, "apply_error": "payload is not valid JSON"}
    file = payload.get("file")
    anchor = payload.get("anchor")
    content = payload.get("content")
    base_hash = payload.get("base_hash")
    if not file or not anchor or content is None:
        return {"applied": False, "apply_error": "payload missing file/anchor/content"}
    if base_hash is not None:
        current = md_anchors_module.anchor_content_hash(file, anchor)
        if current is not None and current != base_hash:
            return {"applied": False, "apply_error": "section changed since proposal; re-draft"}
    try:
        path = await md_anchors_module.apply_replace(file, anchor, content)
    except md_anchors_module.AnchorError as exc:
        return {"applied": False, "apply_error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — never crash the answer flow
        return {"applied": False, "apply_error": f"write failed: {exc}"}
    if path is None:
        return {"applied": False, "reason": "no-op-or-hosted"}
    return {"applied": True, "file": file, "anchor": anchor}


async def _answer_hitl_and_apply(
    db: Any,
    request_id: str,
    answer: str,
    *,
    answered_by: str | None = None,
    approved: bool = True,
) -> dict[str, Any] | None:
    """The ONLY correct way to answer a HITL request — stores the answer then
    runs :func:`_on_hitl_answered`. Both the ``answer_hitl`` MCP tool and the
    route ``PATCH /hitl/{id}`` funnel through here so an ``md_section_update`` is
    applied exactly once. Returns the (possibly enriched) row, or ``None`` when
    the request was not found.
    """
    row = await db_module.answer_hitl_request(
        db, request_id, answer, answered_by=answered_by
    )
    if row is None:
        return None
    extra = await _on_hitl_answered(db, row, approved=approved)
    return {**row, **extra}


def _md_normalize_tags(tags: Any) -> list[str]:
    if not tags:
        return []
    items = tags if isinstance(tags, (list, tuple)) else str(tags).split(",")
    return [str(t).strip().lower() for t in items if str(t).strip()]


async def _append_decision_to_md(title: str, body: str, category: str) -> None:
    """Best-effort append of a pinned decision to DECISIONS.md's decisions-log
    anchor. The committable-category guard lives in ``apply_append`` (passed the
    category), so STRATEGIC/COMPETITIVE/BUSINESS decisions are skipped and never
    reach the committed repo. Never raises."""
    try:
        line = (
            f"- {_md_ts()} **{(category or '').strip().upper()}** — "
            f"{title.strip()}: {_md_one_line(body)}"
        )
        await md_anchors_module.apply_append(
            "DECISIONS.md", "decisions-log", line, category=category,
        )
    except Exception:  # noqa: BLE001 — auto-append must never break the tool
        pass


async def _append_note_to_roadmap(
    title: str, body: str, tags: Any, category: str | None
) -> None:
    """Best-effort append of a roadmap-tagged note to ROADMAP.md's roadmap-notes
    anchor. Requires the 'roadmap' tag AND an explicit committable category —
    notes carry no category column, so the default is fail-closed. Never raises."""
    try:
        if "roadmap" not in _md_normalize_tags(tags):
            return
        if not md_anchors_module.is_committable_category(category):
            return
        line = f"- {_md_ts()} {title.strip()}: {_md_one_line(body)}"
        await md_anchors_module.apply_append(
            "ROADMAP.md", "roadmap-notes", line, category=category,
        )
    except Exception:  # noqa: BLE001
        pass


async def _build_devlog_line(
    db: Any, project_id: str, session_id: str | None
) -> str | None:
    """One-line DEVLOG summary for a session from its done tasks. ``None`` when
    the session logged no done work (keeps trivial sessions out of the log)."""
    if not session_id:
        return None
    name = session_id[:8]
    try:
        for s in await db_module.get_sessions(db, project_id, active_only=False):
            if s.get("id") == session_id:
                name = s.get("name") or name
                break
    except Exception:  # noqa: BLE001
        pass
    try:
        async with db.execute(
            "SELECT description FROM task_log WHERE session_id = ? "
            "AND status = 'done' ORDER BY created_at DESC",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    descs = [r["description"] for r in rows if r and r["description"]]
    if not descs:
        return None
    extra = f" (+{len(descs) - 1} more)" if len(descs) > 1 else ""
    return f"- {_md_ts()} **{name}** — {_md_one_line(descs[0], 120)}{extra}"


async def _finalize_session_md(
    db: Any, project_id: str, session_id: str | None
) -> None:
    """At checkpoint / session-end: append a DEVLOG line and commit any markdown
    the session touched (decisions, roadmap notes, approved section updates) in a
    single pathspec-scoped commit. Best-effort; never raises, never blocks the
    checkpoint. No-ops in hosted mode (md_anchors + git_md self-skip)."""
    try:
        line = await _build_devlog_line(db, project_id, session_id)
        if line:
            await md_anchors_module.apply_append("DEVLOG.md", "devlog", line)
    except Exception:  # noqa: BLE001
        pass
    try:
        touched = md_anchors_module.drain_touched()
        if touched:
            await git_md_module.commit_touched_md(
                touched,
                f"docs: meridian auto-update {_md_ts()}",
                cwd=md_anchors_module.md_root(),
            )
    except Exception:  # noqa: BLE001
        pass


async def _maybe_notify(
    db: Any,
    project_id: str,
    title: str,
    body_text: str,
    event: str = "notification",
    *,
    tenant: dict[str, Any] | None = None,
    pref_key: str | None = None,
) -> None:
    """Send a project notification when the tenant has the event enabled."""
    try:
        if not _notification_pref_enabled(tenant, pref_key):
            return
        await _notify_project(db, project_id, title, body_text, event)
    except Exception:  # noqa: BLE001
        pass


# set_project_icon, rename_project, delete_project → meridian/routes/projects.py


# Goal routes, sessions, worktrees, PDF export → meridian/routes/projects.py

# Sprint item routes → meridian/routes/sprint.py
# Tasks + claim/release routes → meridian/routes/tasks.py
# Handoff route → meridian/routes/handoff.py
# Session lifecycle routes → meridian/routes/sessions.py


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request) -> Any:
    """Plan selection page for new users after first login."""
    from .hosted import _SESSION_COOKIE, _read_session_cookie
    tenant: dict = {}
    try:
        cookie_val = request.cookies.get(_SESSION_COOKIE)
        if cookie_val:
            session_id = _read_session_cookie(cookie_val)
            if session_id:
                db = await _db(request)
                user_session = await db_module.get_user_session(db, session_id)
                if user_session:
                    tenant = await db_module.get_tenant_by_id(db, user_session["tenant_id"]) or {}
    except Exception:
        pass
    standard_link = os.environ.get("STRIPE_PAYMENT_LINK", "#")
    pro_link = os.environ.get("STRIPE_PRO_PAYMENT_LINK", "#")
    email = tenant.get("email", "")
    if email:
        sep = "&" if "?" in standard_link else "?"
        standard_link += f"{sep}prefilled_email={email}"
        sep = "&" if "?" in pro_link else "?"
        pro_link += f"{sep}prefilled_email={email}"
    return _templates.TemplateResponse(request, "onboarding.html", {
        "standard_link": standard_link,
        "pro_link": pro_link,
        "email": email,
    })


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_html(request: Request) -> Any:
    """Serve the Meridian dashboard from a Jinja2 template."""
    if os.environ.get("DEMO_PASSWORD"):
        if not _check_demo_cookie(request):
            return HTMLResponse(_demo_gate_html())
    is_admin = False
    if _hosted_mode():
        from .hosted import get_current_tenant, is_admin_db

        try:
            tenant = await get_current_tenant(request)
            is_admin = await is_admin_db(tenant.get("email", ""), request.app.state.db)
        except HTTPException:
            return RedirectResponse(url="/auth/login", status_code=302)
    response = _templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "version": _VERSION,
            "asset_version": _ASSET_VERSION,
            "demo_mode": False,
            "hosted_mode": os.environ.get("MERIDIAN_HOSTED", "").lower() in ("1", "true", "yes"),
            "is_admin": is_admin,
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    # Always clear the demo context cookie so /demo → /dashboard doesn't
    # leave the user stuck in read-only demo mode.
    response.delete_cookie(_DEMO_CONTEXT_COOKIE)
    return response


@app.get("/setup", response_class=HTMLResponse)
async def setup_redirect(request: Request) -> Any:
    """b6c9f20d — First-run setup alias for binary users.

    Redirects to /dashboard where the first-run wizard (ez-wizard modal)
    automatically detects no existing projects and walks the user through
    creating their first project and connecting an MCP client.
    """
    return RedirectResponse(url="/dashboard", status_code=302)


@app.get("/demo", response_class=HTMLResponse)
async def demo_dashboard(request: Request) -> Any:
    """Public read-only demo dashboard backed by MERIDIAN_DEMO_DB_URL.

    Sets a short-lived cookie so subsequent API calls from this browser
    session are routed to the isolated demo DB and writes are blocked.
    Exempt from SITE_PASSWORD gate so the URL is always public.
    """
    response = _templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "version": _VERSION,
            "asset_version": _ASSET_VERSION,
            "demo_mode": True,
            "hosted_mode": False,
            "is_admin": False,
        },
    )
    response.set_cookie(
        _DEMO_CONTEXT_COOKIE,
        "1",
        max_age=3600,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-cache, no-store"
    return response


@app.get("/config/api-key")
async def api_key_status() -> dict:
    """Tell the dashboard which auth method is active.

    Returns ``{"configured": bool, "method": "oauth"|"api_key"|null}``.
    The token itself is never included in the response.
    """
    _, method = dashboard_module.get_auth_token()
    return {"configured": method is not None, "method": method}



# ---------------------------------------------------------------------------
# File editing endpoints
# ---------------------------------------------------------------------------

# Repo root is the parent of this package directory (meridian/).
_REPO_ROOT = Path(__file__).parent.parent
# The dashboard only allows editing these specific files.
_EDITABLE_FILES: list[str] = ["AGENTS.md", "CLAUDE.md"]

# File-editing and context-block routes → meridian/routes/files.py

# Decisions routes → meridian/routes/decisions.py
# ---------------------------------------------------------------------------
# v2.4 — HITL (human-in-the-loop) queue
# ---------------------------------------------------------------------------


# HITL routes → meridian/routes/hitl.py
# Notes routes → meridian/routes/notes.py


# team/summary, events, webhook-token, search, runs → meridian/routes/projects.py


# ---------------------------------------------------------------------------
# CLAUDE.md auto-update helper
# ---------------------------------------------------------------------------

_CLAUDE_MD_STATE_MARKER = "\n\n---\n<!-- MERIDIAN STATE — auto-generated, do not edit below -->\n"


async def _regenerate_claude_md(
    db: aiosqlite.Connection,
    project_id: str,
    repo_root: Path,
) -> None:
    """Append/replace the MERIDIAN STATE section at the bottom of CLAUDE.md.

    The human-written content above the marker is preserved exactly.  Only
    the section below the marker is overwritten.  If CLAUDE.md does not yet
    have the marker it is appended.  Silently no-ops on any I/O error so a
    failing write never blocks the caller.
    """
    try:
        from datetime import datetime, timezone
        goal = await db_module.get_goal(db, project_id)
        sprint_items = await db_module.get_sprint_items(db, project_id, status="pending")
        project = await db_module.get_project(db, project_id)
        all_tasks = await db_module.get_tasks(db, project_id, limit=20)
        pending_tasks = [t for t in all_tasks if t["status"] in ("pending", "in_progress")][:5]
        decisions_raw = ((project or {}).get("decisions") or "").strip()
        recent_decisions = [l.strip() for l in decisions_raw.splitlines() if l.strip()][-5:]

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [f"## Current Sprint State  _(auto-updated {now})_", ""]
        if goal:
            if goal.get("north_star"):
                lines += [f"**North Star:** {goal['north_star']}", ""]
            if goal.get("sprint"):
                lines += [f"**Sprint:** {goal['sprint']}", ""]
        if sprint_items:
            lines.append("**Pending Sprint Items:**")
            for item in sprint_items[:10]:
                lines.append(f"- [ ] {item['title']}")
            lines.append("")
        if recent_decisions:
            lines.append("**Recent Decisions:**")
            for d in recent_decisions:
                lines.append(f"- {d}")
            lines.append("")
        if pending_tasks:
            lines.append("**Pending Tasks:**")
            for t in pending_tasks:
                lines.append(f"- [{t['status'].upper()}] {t['description'][:120]}")
            lines.append("")
        lines += [
            "**Key Files:**",
            "- `meridian/server.py` — FastAPI app + MCP handlers",
            "- `meridian/db.py` — all DB functions (SQLite + Postgres)",
            "- `meridian/static/dashboard.js` — dashboard UI",
            "- `tests/test_core.py` — full test suite",
            "- `data/meridian-build_handoff.md` — session handoff",
            "",
        ]
        new_state_section = _CLAUDE_MD_STATE_MARKER + "\n".join(lines)

        claude_md_path = repo_root / "CLAUDE.md"
        existing = claude_md_path.read_text(encoding="utf-8") if claude_md_path.exists() else ""
        if _CLAUDE_MD_STATE_MARKER.strip() in existing:
            base = existing.split(_CLAUDE_MD_STATE_MARKER.strip())[0].rstrip()
            updated = base + new_state_section
        else:
            updated = existing.rstrip() + new_state_section
        claude_md_path.write_text(updated, encoding="utf-8")
    except Exception:
        pass  # never block the caller on a write failure


# v2.4 — auto-refresh the <current_state>...</current_state> block. This
# is the dev-facing target the new CLAUDE.md layout uses (see CLAUDE.md
# top). Pre-v2.4 the bottom MERIDIAN STATE section was the only target;
# both are now kept in sync — the bottom section stays for backward compat
# with sessions reading the legacy marker.
import re as _re_module


async def _refresh_claude_md_current_state(
    db: aiosqlite.Connection,
    repo_root: Path,
) -> None:
    """Refresh the ``<current_state>...</current_state>`` block in CLAUDE.md.

    The default project is the most-recently-updated one. Body lines:
    sprint, last updated timestamp, and the 5 most recent task
    descriptions. The block content is regenerated from scratch every
    call; surrounding markdown stays untouched.

    Silently no-ops on any I/O error (file missing, permission denied,
    no projects) so a failing write never blocks the auto-summary loop.
    """
    try:
        from datetime import datetime, timezone
        claude_md_path = repo_root / "CLAUDE.md"
        if not claude_md_path.exists():
            return
        existing = claude_md_path.read_text(encoding="utf-8")
        if "<current_state>" not in existing or "</current_state>" not in existing:
            return
        projects = await db_module.list_projects(db)
        if not projects:
            return
        # Pick the project with the most recent task — that's the one
        # the human is actively working on.
        primary = projects[0]
        try:
            best = None
            best_ts = ""
            for p in projects:
                tasks = await db_module.get_tasks(db, p["id"], limit=1)
                if tasks and tasks[0]["created_at"] > best_ts:
                    best_ts = tasks[0]["created_at"]
                    best = p
            if best is not None:
                primary = best
        except Exception:  # noqa: BLE001
            pass

        goal = await db_module.get_goal(db, primary["id"])
        tasks = await db_module.get_tasks(db, primary["id"], limit=5)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        body_lines = [
            "<!-- Auto-updated by Meridian. Do not edit manually. -->",
            f"Project: {primary['name']} ({primary['id'][:8]})",
            f"Last updated: {now}",
        ]
        if goal:
            if goal.get("sprint"):
                body_lines.append(f"Sprint: {goal['sprint']}")
            if goal.get("north_star"):
                ns = goal["north_star"][:200].replace("\n", " ")
                body_lines.append(f"North Star: {ns}{'…' if len(goal['north_star']) > 200 else ''}")
        if tasks:
            body_lines.append("Recent:")
            for t in tasks:
                desc = (t.get("description") or "").replace("\n", " ")[:120]
                status = (t.get("status") or "?").upper()
                body_lines.append(f"  - [{status}] {desc}")

        new_block = "<current_state>\n" + "\n".join(body_lines) + "\n</current_state>"
        updated = _re_module.sub(
            r"<current_state>[\s\S]*?</current_state>",
            new_block,
            existing,
            count=1,
        )
        if updated != existing:
            claude_md_path.write_text(updated, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


_ROADMAP_AUTO_COMMENT = "<!-- meridian-auto: {version} -->"


async def _update_roadmap_version_history(
    db: aiosqlite.Connection,
    project_id: str,
    version: str,
    repo_root: Path,
) -> None:
    """Insert or replace a version-history bullet in ROADMAP.md.

    Reads all *done* sprint items for *version*, builds a one-line summary,
    and writes it into the ``## Version history`` section.  Each entry is
    tagged with an HTML comment so it can be found on the next update.
    Silently no-ops on any I/O or DB error.
    """
    try:
        items = await db_module.get_sprint_items(db, project_id)
        done = [i for i in items if i.get("version") == version and i.get("status") == "done"]
        if not done:
            return

        roadmap_path = repo_root / "ROADMAP.md"
        content = roadmap_path.read_text(encoding="utf-8") if roadmap_path.exists() else ""

        titles = "; ".join(i["title"] for i in done[:5])
        if len(done) > 5:
            titles += f"; +{len(done) - 5} more"
        marker = _ROADMAP_AUTO_COMMENT.format(version=version)
        new_line = f"- **{version}** — {titles} {marker}"

        lines = content.splitlines()
        in_history = False
        marker_idx: int | None = None
        history_end_idx: int | None = None

        for i, line in enumerate(lines):
            if line.strip() == "## Version history":
                in_history = True
            elif in_history and marker in line:
                marker_idx = i
                break
            elif in_history and line.startswith("---"):
                history_end_idx = i
                break

        if marker_idx is not None:
            lines[marker_idx] = new_line
        elif history_end_idx is not None:
            lines.insert(history_end_idx, new_line)
        else:
            content = content.rstrip() + "\n" + new_line + "\n"
            roadmap_path.write_text(content, encoding="utf-8")
            return

        roadmap_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass  # never block the caller on a write failure


@app.websocket("/ws/{project_id}")
async def ws_project(ws: WebSocket, project_id: str) -> None:
    """Push task-log events to dashboard clients for one project."""
    broadcaster: dashboard_module.WebSocketBroadcaster = (
        ws.app.state.ws_broadcaster
    )
    await broadcaster.serve(ws, project_id)




@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page() -> HTMLResponse:
    """Admin password gate for the /admin panel."""
    html = """<!DOCTYPE html><html><head><meta charset=utf-8>
<title>Admin login — Meridian</title>
<style>body{font-family:system-ui,sans-serif;background:#0d1117;color:#e8eaed;display:grid;place-items:center;min-height:100vh;margin:0}
form{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:32px;width:320px}
h2{margin:0 0 20px;font-size:1.1rem}
input{width:100%;padding:10px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#e8eaed;font-size:.95rem;box-sizing:border-box;margin-bottom:12px}
button{width:100%;padding:10px;background:#6c8fff;color:#fff;border:none;border-radius:4px;font-weight:700;cursor:pointer;font-size:.95rem}
</style></head><body>
<form method="post" action="/admin/login">
<h2>⬡ Admin access</h2>
<input type="password" name="password" placeholder="Admin password" autofocus autocomplete="current-password">
<button type="submit">Enter</button>
</form></body></html>"""
    return HTMLResponse(html)


@app.post("/admin/login")
async def admin_login_post(request: Request) -> Any:
    """Validate admin password and set signed cookie."""
    from fastapi.responses import RedirectResponse
    form = await request.form()
    password = str(form.get("password", ""))
    expected = os.environ.get("MERIDIAN_ADMIN_PASSWORD", "")
    if not expected:
        return RedirectResponse("/admin", status_code=303)
    import secrets
    if not secrets.compare_digest(password, expected):
        return HTMLResponse("""<!DOCTYPE html><html><head><meta charset=utf-8>
<title>Admin login — Meridian</title>
<style>body{font-family:system-ui,sans-serif;background:#0d1117;color:#e8eaed;display:grid;place-items:center;min-height:100vh;margin:0}
form{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:32px;width:320px}
h2{margin:0 0 8px;font-size:1.1rem}p.err{color:#f85149;margin:0 0 12px;font-size:.9rem}
input{width:100%;padding:10px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#e8eaed;font-size:.95rem;box-sizing:border-box;margin-bottom:12px}
button{width:100%;padding:10px;background:#6c8fff;color:#fff;border:none;border-radius:4px;font-weight:700;cursor:pointer;font-size:.95rem}
</style></head><body><form method="post" action="/admin/login">
<h2>⬡ Admin access</h2><p class="err">Incorrect password</p>
<input type="password" name="password" placeholder="Admin password" autofocus>
<button type="submit">Enter</button></form></body></html>""", status_code=401)
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        "meridian_admin", expected,
        httponly=True, samesite="strict",
        secure=os.environ.get("MERIDIAN_SERVER_URL", "").startswith("https://"),
    )
    return response


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request) -> Any:
    """Admin dashboard — restricted to MERIDIAN_ADMIN_EMAILS + optional password."""
    from .hosted import get_current_tenant, is_admin_db, check_admin_password

    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        return HTMLResponse(
            '<meta http-equiv="refresh" content="0;url=/auth/login">',
            status_code=302,
        )

    if not await is_admin_db(tenant.get("email", ""), request.app.state.db):
        raise HTTPException(status_code=403, detail="admin access only")
    if not check_admin_password(request):
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/admin/login", status_code=302)

    db = request.app.state.db

    def _r(row: Any) -> dict[str, Any]:
        """Convert an aiosqlite.Row or dict row to a plain dict."""
        if row is None:
            return {}
        if isinstance(row, dict):
            return row
        return {k: row[k] for k in row.keys()}

    # Gather stats
    async with db.execute(
        "SELECT COUNT(*) as n FROM tenants WHERE plan='pro'"
    ) as cur:
        row = await cur.fetchone()
        active_count = _r(row).get("n", 0) if row else 0

    async with db.execute(
        "SELECT COUNT(*) as n FROM tenants WHERE plan='free'"
    ) as cur:
        row = await cur.fetchone()
        free_count = _r(row).get("n", 0) if row else 0

    async with db.execute(
        "SELECT email, plan, created_at, neon_project_id FROM tenants ORDER BY created_at DESC LIMIT 20"
    ) as cur:
        rows = await cur.fetchall()
        recent_tenants = [_r(r) for r in rows] if rows else []

    rows_html = "".join(
        f"<tr><td>{t['email']}</td><td>{t['plan']}</td><td>{t['created_at'][:10]}</td>"
        f"<td>{'✓' if t['neon_project_id'] else '—'}</td></tr>"
        for t in recent_tenants
    )

    html = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Meridian Admin</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d0d0f;color:#e8eaf0;font-family:-apple-system,sans-serif;padding:2rem}}
.wrap{{max-width:900px;margin:0 auto}}
h1{{font-size:1.5rem;font-weight:800;margin-bottom:1.5rem}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1rem;margin-bottom:2rem}}
.stat{{background:#16181c;border:1px solid #2a2d35;border-radius:8px;padding:1.2rem;text-align:center}}
.stat .n{{font-size:2rem;font-weight:800;color:#6c8fff}}
.stat .l{{font-size:.8rem;color:#8b8fa8;margin-top:.3rem}}
table{{width:100%;border-collapse:collapse;background:#16181c;border-radius:8px;overflow:hidden}}
th{{background:#0d0d0f;color:#8b8fa8;font-size:.8rem;padding:.6rem 1rem;text-align:left}}
td{{padding:.6rem 1rem;border-top:1px solid #2a2d35;font-size:.85rem;color:#e8eaf0}}
a{{color:#6c8fff}}
</style>
</head>
<body>
<div class="wrap">
<h1>&#x1f9ed; Meridian Admin <small style="font-size:.7em;color:#8b8fa8">{tenant['email']}</small></h1>
<div class="stats">
  <div class="stat"><div class="n">{active_count}</div><div class="l">Pro tenants</div></div>
  <div class="stat"><div class="n">{free_count}</div><div class="l">Free tenants</div></div>
  <div class="stat"><div class="n">{active_count + free_count}</div><div class="l">Total accounts</div></div>
  <div class="stat"><div class="n">1000</div><div class="l">Neon project cap</div></div>
</div>
<h2 style="margin-bottom:.8rem;font-size:1rem;color:#8b8fa8">Recent accounts (last 20)</h2>
<table>
<thead><tr><th>Email</th><th>Plan</th><th>Joined</th><th>Neon</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>
<p style="margin-top:1.5rem;font-size:.8rem;color:#8b8fa8">
  <a href="/">← Home</a> &nbsp;·&nbsp; <a href="/auth/logout">Sign out</a>
</p>
</div>
</body>
</html>"""
    return HTMLResponse(html)


@app.patch("/settings/notifications")
async def update_notification_prefs(request: Request) -> dict[str, Any]:
    """v2.5 — save email notification preferences for the authenticated tenant."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant
    tenant = await get_current_tenant(request)
    body = await request.json()
    prefs = _notification_prefs_from_raw(body)
    await db_module.update_tenant(
        request.app.state.db, tenant["id"], notification_prefs=json.dumps(prefs)
    )
    return {"status": "ok", "prefs": prefs}


@app.get("/settings/notifications")
async def get_notification_prefs(request: Request) -> dict[str, Any]:
    """v2.5 — return current notification preferences for the authenticated tenant."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant
    tenant = await get_current_tenant(request)
    prefs = _notification_prefs_from_raw(tenant.get("notification_prefs"))
    return {"prefs": prefs}


_WORKSPACE_MEMBER_LIMITS: dict[str, int] = {"standard": 25, "pro": 50}


@app.post("/workspace/invite", status_code=201)
async def workspace_invite(request: Request) -> dict[str, Any]:
    """Invite a new workspace member. Sends invite email via Resend."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant, send_invite_email
    import hashlib, secrets as _secrets
    tenant = await get_current_tenant(request)
    # G5.19 — admin+ can invite. Tenant owners always pass; admin invitees
    # pass; member/viewer get 403.
    from .roles import (  # noqa: PLC0415
        VALID_ROLES, VALID_GITHUB_ACCESS,
        default_github_access_for_role, PERM_INVITE,
    )
    await _require_workspace_perm(request, tenant, PERM_INVITE)
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    role = (body.get("role") or "member").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="valid email required")
    if role not in VALID_ROLES:
        raise HTTPException(
            status_code=422,
            detail=f"role must be one of {sorted(VALID_ROLES)}",
        )
    github_access = (body.get("github_access") or "").strip().lower()
    if github_access and github_access not in VALID_GITHUB_ACCESS:
        raise HTTPException(
            status_code=422,
            detail=f"github_access must be one of {sorted(VALID_GITHUB_ACCESS)}",
        )
    if not github_access:
        github_access = default_github_access_for_role(role)
    db = request.app.state.db
    limit = _WORKSPACE_MEMBER_LIMITS.get(tenant.get("plan", "standard"), 25)
    count = await db_module.count_workspace_members(db, tenant["id"])
    if count >= limit:
        raise HTTPException(status_code=402, detail=f"Team member limit ({limit}) reached for your plan")
    raw_token = _secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    invite = await db_module.create_workspace_invite(
        db, tenant["id"], email, role, token_hash, github_access=github_access,
    )
    base = os.environ.get("MERIDIAN_SERVER_URL", "https://usemeridian.us").rstrip("/")
    invite_url = f"{base}/workspace/accept?token={raw_token}"
    try:
        await send_invite_email(email, invite_url, tenant["email"])
    except Exception:
        pass  # email failure doesn't block invite creation
    return {"id": invite["id"], "email": email, "role": role, "pending": True}


@app.post("/workspace/invite/{member_id}/resend", status_code=200)
async def workspace_invite_resend(request: Request, member_id: str) -> dict[str, Any]:
    """Resend invite email for a pending workspace member."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant, send_invite_email
    import hashlib, secrets as _secrets
    tenant = await get_current_tenant(request)
    from .roles import PERM_INVITE  # noqa: PLC0415
    await _require_workspace_perm(request, tenant, PERM_INVITE)
    db = request.app.state.db
    member = await db_module.get_workspace_member_by_id(db, member_id, tenant["id"])
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")
    if member.get("joined_at") is not None:
        raise HTTPException(status_code=409, detail="Invite already accepted")
    raw_token = _secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    await db_module.refresh_workspace_invite_token(db, member_id, tenant["id"], token_hash)
    base = os.environ.get("MERIDIAN_SERVER_URL", "https://usemeridian.us").rstrip("/")
    invite_url = f"{base}/workspace/accept?token={raw_token}"
    try:
        await send_invite_email(member["email"], invite_url, tenant["email"])
    except Exception:
        pass
    return {"id": member_id, "email": member["email"], "resent": True}


@app.get("/workspace/accept")
async def workspace_accept(request: Request, token: str = "") -> HTMLResponse:
    """Accept a workspace invite. Marks joined_at and redirects to dashboard."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    if not token:
        return HTMLResponse("<h2>Invalid invite link.</h2><p><a href='/auth/login'>Sign in</a></p>", status_code=400)
    import hashlib
    from fastapi.responses import RedirectResponse as _Redir
    from .hosted import get_current_tenant as _get_cur_tenant  # noqa: PLC0415
    # Check auth BEFORE consuming the token so unauthenticated users can be
    # sent to login and then bounced back here to complete the accept.
    try:
        await _get_cur_tenant(request)
        authenticated = True
    except Exception:
        authenticated = False
    if not authenticated:
        # Preserve the token across the login flow via ?next=
        return _Redir(f"/auth/login?next=/workspace/accept?token={token}", status_code=302)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db = request.app.state.db
    invite = await db_module.get_workspace_invite_by_token_hash(db, token_hash)
    if not invite:
        return HTMLResponse(
            "<h2>Invite not found or already used.</h2><p><a href='/dashboard'>Go to dashboard</a></p>",
            status_code=404,
        )
    await db_module.accept_workspace_invite(db, invite["id"])
    await db_module.upsert_tenant(db, email=invite["email"])
    return _Redir("/dashboard", status_code=302)


@app.post("/workspace/connect-db", status_code=200)
async def workspace_connect_db(request: Request) -> dict[str, Any]:
    """Store a custom Postgres connection string as the user's project DB."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant
    from .pg_adapter import open_pg_connection
    tenant = await get_current_tenant(request)
    body = await request.json()
    url: str = (body.get("url") or "").strip()
    if not url or not url.startswith("postgresql"):
        raise HTTPException(status_code=422, detail="Invalid connection string")
    # Validate connectivity before storing
    try:
        test_conn = await open_pg_connection(url)
        await test_conn.close()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not connect: {exc}") from exc
    encrypted = db_module.encrypt_field(url)
    auth_db = request.app.state.db
    await db_module.update_tenant(auth_db, tenant["id"], neon_db_url=encrypted)
    # Evict cached connection so next request re-opens with the new URL
    from . import _deps
    _deps._tenant_db_cache.pop(tenant["id"], None)
    return {"connected": True}


@app.get("/workspace/members")
async def workspace_list_members(request: Request) -> list[dict[str, Any]]:
    """List all workspace members (pending and accepted) for the current tenant."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant
    tenant = await get_current_tenant(request)
    members = await db_module.list_workspace_members(request.app.state.db, tenant["id"])
    return [
        {
            "id": m["id"],
            "email": m["email"],
            "role": m["role"],
            "github_access": m.get("github_access"),
            "joined_at": m.get("joined_at"),
            "pending": m.get("joined_at") is None,
        }
        for m in members
    ]


@app.patch("/workspace/members/{member_id}")
async def workspace_update_member(request: Request, member_id: str) -> dict[str, Any]:
    """v2.8 — change a workspace member's role (and github_access cap).

    Admin+ only (PERM_INVITE, same gate as add/remove). When ``role`` changes
    without an explicit ``github_access``, the cap is reset to that role's
    default so a promotion/demotion carries sane repo access automatically.
    """
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant
    from .roles import (  # noqa: PLC0415
        VALID_ROLES, VALID_GITHUB_ACCESS,
        default_github_access_for_role, ROLE_OWNER, PERM_INVITE,
    )
    tenant = await get_current_tenant(request)
    await _require_workspace_perm(request, tenant, PERM_INVITE)
    body = await request.json()
    role = (body.get("role") or "").strip() or None
    github_access = (body.get("github_access") or "").strip().lower() or None
    if role is not None and role not in VALID_ROLES:
        raise HTTPException(
            status_code=422, detail=f"role must be one of {sorted(VALID_ROLES)}",
        )
    # Promoting an invitee to full owner is a billing/ownership transfer, not a
    # role edit — block it here so the implicit tenant owner stays singular.
    if role == ROLE_OWNER:
        raise HTTPException(status_code=422, detail="cannot assign the owner role")
    if github_access is not None and github_access not in VALID_GITHUB_ACCESS:
        raise HTTPException(
            status_code=422,
            detail=f"github_access must be one of {sorted(VALID_GITHUB_ACCESS)}",
        )
    if role is not None and github_access is None:
        github_access = default_github_access_for_role(role)
    if role is None and github_access is None:
        raise HTTPException(status_code=422, detail="role or github_access required")
    updated = await db_module.update_workspace_member(
        request.app.state.db, member_id, tenant["id"],
        role=role, github_access=github_access,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="member not found")
    return {
        "id": updated["id"],
        "email": updated["email"],
        "role": updated["role"],
        "github_access": updated.get("github_access"),
        "pending": updated.get("joined_at") is None,
    }


@app.delete("/workspace/members/{member_id}", status_code=204)
async def workspace_remove_member(request: Request, member_id: str) -> None:
    """Remove a workspace member or revoke a pending invite."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant
    tenant = await get_current_tenant(request)
    await db_module.delete_workspace_member(request.app.state.db, member_id, tenant["id"])


# workspace/notes + workspace/decisions + workspace/settings → meridian/routes/workspace.py

def _is_demo_request(request: Request) -> bool:
    """Return True when the request is in demo mode (env flag or cookie)."""
    env_demo = os.environ.get("MERIDIAN_DEMO", "").lower() in ("1", "true", "yes")
    cookie_demo = bool(request.cookies.get(_DEMO_CONTEXT_COOKIE))
    return env_demo or cookie_demo


# Export, account management, settings, and GitHub integration routes extracted
# to routes/export.py and routes/github.py respectively.
# NOTE: export_my_data stays in server.py because slowapi @_rate_limit does not
# wire correctly for routes included via APIRouter (same constraint as magic-link).
@app.get("/export/my-data")
@_rate_limit("3/minute")
async def export_my_data(request: Request) -> Response:
    """GDPR data portability — returns a JSON file of all account data."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    if _is_demo_request(request):
        return JSONResponse(
            {"detail": "Not available in demo mode. Sign up at usemeridian.us"},
            status_code=403,
        )
    from .hosted import get_current_tenant
    tenant = await get_current_tenant(request)
    # Account rows (tenant/tokens/members) live in the auth DB; the tenant's
    # project data lives in its own per-tenant DB. Pass both so the export
    # actually contains projects (hosted mode previously exported empty arrays).
    data = await db_module.export_tenant_data(
        request.app.state.db, tenant["id"], project_db=await _db(request),
    )
    payload = json.dumps(data, indent=2, default=str).encode()
    email_slug = (tenant.get("email") or "user").split("@")[0][:20]
    filename = f"meridian-export-{email_slug}.json"
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# /mcp/quickstart and /mcp/tools-doc → meridian/mcp/http_handler.py

# /admin/health, /admin/git-status → meridian/routes/admin.py


# DELETE /tasks/{task_id} → meridian/routes/tasks.py

@app.post("/tasks/enqueue", response_model=Task, status_code=202)
async def enqueue_task(body: EnqueueTask, request: Request) -> dict[str, Any]:
    """Paid-tier: queue a Claude subprocess and return the pending task row.

    Responds with 202 Accepted so clients can distinguish this from a
    synchronous task creation. The worker runs in the background; poll
    ``GET /projects/{id}/tasks`` to see the result land.
    """
    _req_db = await _db(request)
    project = await db_module.get_project(_req_db, body.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    async with _req_db.execute(
        "SELECT id FROM sessions WHERE id = ?", (body.session_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return await enqueue_module.enqueue_claude_task(
        _req_db,
        body.session_id,
        body.project_id,
        body.prompt,
        timeout=body.timeout,
    )


# ---------------------------------------------------------------------------
# v0.4.5 — expire sessions and auto-generate handoffs
# ---------------------------------------------------------------------------


async def _expire_and_generate_handoffs(
    db: aiosqlite.Connection, data_dir: str
) -> dict[str, Any]:
    """Expire stale sessions and regenerate handoffs for affected projects.

    Returns ``{"count": n, "auto_handoff_generated": bool}``. Handoff
    generation failures are swallowed so a bad project never blocks the
    sessions endpoint from returning.
    """
    result = await db_module.expire_idle_sessions(db)
    generated = False
    import os as _os
    _skip = not _os.environ.get("ANTHROPIC_API_KEY")
    for pid in result["project_ids"]:
        try:
            # v2.4 — auto-generated handoffs from the idle-expire loop
            # skip the Haiku ai_summary unless the key is set.
            await handoff_module.generate_handoff(
                db, pid, data_dir, skip_ai_summary=_skip
            )
            generated = True
        except Exception:  # noqa: BLE001
            pass
        try:
            await _regenerate_claude_md(db, pid, _REPO_ROOT)
        except Exception:  # noqa: BLE001
            pass
    return {"count": result["count"], "auto_handoff_generated": generated}


# ---------------------------------------------------------------------------
# v0.4.4 — start_session composite helper + endpoint
# ---------------------------------------------------------------------------


async def _load_executor_session_context(
    db: aiosqlite.Connection,
    project_id: str,
) -> tuple[dict[str, Any], str]:
    """Return normalized executor config plus the injected context block."""
    settings = await db_module.get_project_settings(db, project_id)
    raw_config = (settings or {}).get("executor_config") if settings else None
    return executor_config_for_output(raw_config), build_executor_config_block(raw_config)


async def _idle_until_session_done(
    db: aiosqlite.Connection,
    session_id: str,
    *,
    poll_seconds: int = 30,
) -> dict[str, Any]:
    """Poll every 30 seconds until the target session is closed/archived."""
    while True:
        async with db.execute(
            "SELECT id, status, project_id, name, human_id, last_seen "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return {"session_id": session_id, "done": True, "status": "missing"}
        session = row if isinstance(row, dict) else {k: row[k] for k in row.keys()}
        status_val = session.get("status") or "missing"
        if status_val in {"closed", "archived"}:
            return {
                "session_id": session_id,
                "done": True,
                "status": status_val,
                "project_id": session.get("project_id"),
                "name": session.get("name"),
                "human_id": session.get("human_id"),
                "last_seen": session.get("last_seen"),
            }
        await asyncio.sleep(max(1, poll_seconds))


async def _find_continuation_session(
    db: aiosqlite.Connection,
    project_id: str,
    session_name: str,
    max_idle_minutes: int = 5,
) -> dict[str, Any] | None:
    """G8.34 — Look for an active session with this name whose last heartbeat
    is within ``max_idle_minutes`` minutes. Returns the session row or None.

    Matching keys: project_id + session_name (NOT the MCP-Session-Id header,
    because ChatGPT regenerates that per call so it can never identify a
    continuation). The session name is the logical handle the client picked
    at startup; re-registering with the same name is the resume signal.
    """
    from datetime import datetime, timezone, timedelta
    sessions = await db_module.get_sessions(db, project_id, active_only=True)
    if not sessions:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_idle_minutes)
    for s in sessions:
        if (s.get("name") or "") != session_name:
            continue
        last_seen_raw = s.get("last_seen") or ""
        if not last_seen_raw:
            continue
        try:
            seen = datetime.strptime(last_seen_raw, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc,
            )
        except ValueError:
            continue
        if seen >= cutoff:
            return s
    return None


async def _start_session_composite(
    db: aiosqlite.Connection,
    project_id: str,
    session_name: str,
    data_dir: str,
    human_id: str | None = None,
    client_type: str | None = None,
    role: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Register + goal + tasks + sessions + handoff-check in one shot.

    Replaces the four-call cold-start sequence (register_session, get_goal,
    get_tasks, check handoff file) with a single call that returns everything
    a new session needs before touching anything.

    G8.34 — If a session with the same ``session_name`` is still active and
    pinged a heartbeat within the last 5 minutes, return a compact
    continuation block (no new registration, no goal-block flood). Keyed on
    (project_id, session_name); NEVER on Mcp-Session-Id since ChatGPT
    regenerates that header per tool call.
    """
    existing = await _find_continuation_session(db, project_id, session_name)
    if existing is not None:
        # Compact resume block — caller already has the heavy context.
        recent = await db_module.get_tasks(db, project_id, limit=10)
        return {
            "continuation": True,
            "session": existing,
            "source": source,
            "recent_tasks": recent,
            "note": "Resumed existing session (last_seen within 5 min). "
                    "Call start_session(source='startup') after a real cold boot "
                    "to get the full orientation block.",
        }
    # v1.8.x — archive sessions silent for 7+ days so they don't crowd
    # the active list seen by new sessions.
    await db_module.archive_empty_sessions(db)
    await db_module.archive_stale_sessions(db, project_id)
    try:
        await db_module.expire_file_locks(db)
    except Exception:
        pass

    # 5b13b7b6 — session name uniqueness: reject if an active session with this
    # name was seen within the last 60 seconds (genuine concurrent duplicate start).
    # Sessions older than 60 s are handled by the continuation window or by the
    # stale-replacement flow above — don't block those.
    from datetime import datetime, timedelta, timezone as _tz
    _uniq_cutoff = (
        datetime.now(_tz.utc) - timedelta(seconds=60)
    ).strftime("%Y-%m-%d %H:%M:%S")
    _active_sessions = await db_module.get_sessions(db, project_id, active_only=True)
    for _as in _active_sessions:
        if (_as.get("name") or "").lower() != session_name.lower():
            continue
        if _as.get("status") != "active":
            continue
        _ls = _as.get("last_seen") or ""
        if _ls > _uniq_cutoff:
            raise ValueError(
                f"session '{session_name}' already exists and is active "
                f"(id: {_as['id'][:8]}...) — use a different name or close "
                "the existing session first."
            )

    if not human_id and not _hosted_mode():
        human_id = db_module.get_default_human_id()
    session = await db_module.register_session(
        db, project_id, session_name, human_id=human_id, client_type=client_type
    )
    _mark_session_connected(session["id"])
    # G9.x - If start_session is called with an explicit project_id, any pending
    # hook_project_select HITL for this project is redundant -- dismiss it silently.
    # The executor already chose this project by calling start_session.
    try:
        await db.execute(
            "UPDATE hitl_requests SET status='dismissed' WHERE project_id=? AND kind='hook_project_select' AND status='pending'",
            (project_id,),
        )
        await db.commit()
    except Exception:
        pass
    try:
        await db_module.create_executor_run(db, session["id"], project_id)
    except Exception:
        pass
    released_stale_claims = await db_module.release_stale_task_claims(
        db,
        project_id,
        exclude_session_id=session["id"],
        max_age_hours=2,
    )
    if released_stale_claims:
        noun = "claim" if released_stale_claims == 1 else "claims"
        await db_module.log_task(
            db,
            session["id"],
            project_id,
            f"Auto-released {released_stale_claims} stale {noun} older than 2 hours from previous sessions.",
            "done",
        )

    goal = await db_module.get_goal(db, project_id)
    if goal is not None:
        recent_5 = await db_module.get_tasks(db, project_id, limit=5)
        goal["ambient_tasks"] = [
            {
                "status": t["status"],
                "description": t["description"],
                "created_at": t["created_at"],
            }
            for t in recent_5
        ]

    recent_tasks = await db_module.get_tasks(db, project_id, limit=10)

    await _expire_and_generate_handoffs(db, data_dir)
    active_sessions = await db_module.get_sessions(db, project_id, active_only=True)

    project = await db_module.get_project(db, project_id)
    project_name = project["name"] if project else project_id
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", project_name).strip("-") or "project"
    handoff_path_str = str(Path(data_dir) / f"{slug}_handoff.md")
    handoff_exists = Path(handoff_path_str).exists()

    # v0.6.1 — stamp the XML envelope onto the goal so MCP consumers
    # get a single ready-to-prompt block. Always present (even when
    # goal is None) so the contract is uniform.
    ambient_for_xml = goal.get("ambient_tasks") if goal else []
    # v1.1.3 — coherence warning + per-field ages.
    field_ages = await db_module.get_goal_field_ages(db, project_id)
    coherence = db_module.compute_coherence_warning(field_ages)
    # v1.1.4 — append-only decisions log (skipped for worker sessions
    # since v1.2.0's start_worker_session builds its own slim
    # worker_context block).
    decisions = await db_module.get_decisions(db, project_id)
    if goal is not None:
        goal["field_ages"] = field_ages
        goal["coherence_warning"] = coherence
        goal["decisions"] = decisions
    goal_xml = db_module.build_goal_xml(
        goal, project_name, ambient_for_xml, coherence,
        decisions=decisions,
    )
    # v0.6.2 — Anthropic-API content blocks with cache_control on
    # the two static fields. Same ambient slice used by the XML.
    goal_cache_blocks = db_module.build_goal_cache_blocks(
        goal, project_name, ambient_for_xml
    )
    if goal is not None:
        goal["xml"] = goal_xml
        goal["cache_blocks"] = goal_cache_blocks

    # v1.1 — surface the active sprint checklist so cold sessions see
    # what's in flight before doing anything. "Active" = todo/pending/in_progress.
    # Executor sessions (role="executor") exclude human-assigned tasks.
    active_statuses = ("todo", "pending", "in_progress")
    all_sprint_items = await db_module.get_sprint_items(
        db, project_id, include_human=(role != "executor")
    )
    pending_items = [
        it for it in all_sprint_items if it.get("status") in active_statuses
    ]
    sprint_items_xml = db_module.build_sprint_items_xml(pending_items)

    # v2.3 — every cold session reads the coordination protocol on entry.
    meridian_instructions = _load_meridian_md()

    # v3.4 — inject workspace-level decisions + notes so a cold executor sees
    # tenant-global conventions on entry without a separate get_context_block
    # round-trip. Same source + renderer as get_context_block.
    try:
        ws_decisions = await db_module.get_workspace_decisions(db)
        ws_notes = await db_module.get_workspace_notes(db)
        workspace_context = _render_workspace_block(ws_decisions, ws_notes)
    except Exception:
        workspace_context = ""

    # v3.1 — executor sessions get their project-level config injected so they
    # can use repo_path, test_cmd, etc. without manual lookup.
    executor_config: dict[str, Any] | None = None
    executor_context: str | None = None
    if role == "executor":
        try:
            executor_config, executor_context = await _load_executor_session_context(
                db, project_id
            )
        except Exception:
            executor_config = executor_config_for_output({})
            executor_context = build_executor_config_block({})

    # File conflict warnings: flag any files claimed by other live sessions.
    file_warnings = await db_module.get_file_conflict_warnings(
        db, project_id, session["id"]
    )

    # 8a0c5a78 — inject per-project agent instructions so every session sees them.
    agent_instructions = await db_module.get_agent_instructions(db, project_id)

    payload: dict[str, Any] = {
        "session_id": session["id"],
        "goal": goal,
        "goal_xml": goal_xml,  # v0.6.1 — always present
        "goal_cache_blocks": goal_cache_blocks,  # v0.6.2 — ready for Anthropic
        "sprint_items": pending_items,  # v1.1 — active checklist
        "sprint_items_xml": sprint_items_xml,
        "recent_tasks": recent_tasks,
        "active_sessions": active_sessions,
        "handoff_exists": handoff_exists,
        "handoff_path": handoff_path_str,
        "files": list(_EDITABLE_FILES),
        "meridian_instructions": meridian_instructions,  # v2.3
        "workspace_context": workspace_context,  # v3.4 — tenant-global block
    }
    if agent_instructions:
        payload["agent_instructions"] = agent_instructions
    if file_warnings:
        payload["file_warnings"] = file_warnings
    if executor_config is not None:
        payload["executor_config"] = executor_config
        payload["executor_context"] = executor_context
        payload["role"] = "executor"
    return payload


@app.post("/projects/{project_id}/start-session")
async def start_session_endpoint(
    project_id: str, body: StartSessionRequest, request: Request
) -> dict[str, Any]:
    """v0.4.4 — one call to start a coordinated session.

    Registers the caller, fetches goal + ambient tasks, fetches the last 10
    tasks, lists active sessions, and reports whether a handoff file already
    exists on disk. Replaces 4 separate MCP calls at session cold-start.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    validate_input_size(body.session_name, "session name", 200)
    try:
        return await _start_session_composite(
            await _db(request),
            project_id,
            body.session_name,
            _data_dir(request),
            human_id=body.human_id,
            client_type=body.client,
            role=body.role,
            source=body.source,
        )
    except ValueError as _ve:
        raise HTTPException(status_code=400, detail=str(_ve)) from _ve


# ---------------------------------------------------------------------------
# Admin — shutdown
# ---------------------------------------------------------------------------


# /admin/shutdown + /admin/restart → meridian/routes/admin.py


# /admin/snapshot → meridian/routes/admin.py


# ---------------------------------------------------------------------------
# Hooks - Claude Code / Codex session lifecycle endpoints
# ---------------------------------------------------------------------------


async def _resolve_hook_db(request: Request) -> Any:
    """Resolve the DB for hook routes.

    If the request carries an explicit Bearer token, always use that tenant and
    fail fast on invalid tokens instead of silently falling back to the shared
    auth/local DB. If no Bearer token is present, preserve the normal _db()
    behavior so browser-session and self-hosted flows keep working unchanged.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return await _db(request)

    raw_token = auth_header[len("Bearer "):].strip()
    if not raw_token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    auth_db = request.app.state.db
    tenant = await db_module.get_tenant_from_token_hash(auth_db, token_hash)
    if tenant is None:
        raise HTTPException(status_code=401, detail="invalid API token")

    conn = await _open_tenant_db_by_id(request, tenant["id"])
    request.state._db_conn = conn
    return conn


def _hook_is_executor(body: dict[str, Any]) -> bool:
    """True when a SessionStart hook payload denotes an executor session.

    Executor sessions (Claude Code run with --dangerously-skip-permissions, or
    an explicit executor flag) auto-claim sprint items. Plain conversational
    sessions must not — they get context injected but no claim instruction.
    Defaults to False so an unsignalled hook is treated as plain chat.
    """
    perm = str(body.get("permission_mode") or "").strip().lower()
    if perm in ("bypasspermissions", "bypass", "dangerously-skip-permissions"):
        return True
    if str(body.get("session_role") or "").strip().lower() == "executor":
        return True
    return bool(body.get("executor")) or bool(body.get("dangerously_skip_permissions"))


@app.post("/hooks/session-start")
async def hooks_session_start(body: dict[str, Any], request: Request) -> dict[str, Any]:
    """Claude Code / Codex SessionStart hook.

    Accepts {project_id, session_name?}. Calls start_session() and returns
    {hookSpecificOutput: {additionalContext: "..."}} so Claude Code injects
    the project context into the agent's initial context window automatically.

    Hosted callers can authenticate with Authorization: Bearer sk_meridian_...
    to route directly to their tenant DB. Local/browser-session behavior is
    unchanged when no Bearer token is supplied.
    """
    project_id = (body.get("project_id") or "").strip()
    session_name = (body.get("session_name") or "hook-session").strip()
    hook_cwd = (body.get("cwd") or "").strip()
    hook_hostname = (body.get("hostname") or "").strip()
    registration_token = (body.get("registration_token") or "").strip()

    # Token-based OAuth hooks: a hook script with no Bearer token but a
    # hostname + registration_token authenticates against registered_hostnames
    # in the control-plane DB. Unknown hostname/token MUST fail open to an empty
    # context (Claude Code must always start cleanly) — never 401.
    _has_bearer = request.headers.get("Authorization", "").startswith("Bearer ")
    if not _has_bearer and registration_token:
        _auth_db = request.app.state.db
        _tid = await db_module.resolve_hostname_registration(
            _auth_db, hook_hostname, registration_token
        )
        if not _tid:
            return {"hookSpecificOutput": {
                "hookEventName": "SessionStart", "additionalContext": ""}}
        db = await _open_tenant_db_by_id(request, _tid)
        request.state._db_conn = db
    else:
        db = await _resolve_hook_db(request)

    def _normalize_hook_cwd(path: str) -> str:
        value = (path or "").strip().replace("\\", "/")
        m = re.match(r"^/mnt/([a-zA-Z])(?:/(.*))?$", value)
        if m:
            drive = m.group(1).upper()
            rest = (m.group(2) or "").strip("/")
            value = f"{drive}:/{rest}" if rest else f"{drive}:/"
        return value.rstrip("/")

    normalized_hook_cwd = _normalize_hook_cwd(hook_cwd)
    import logging as _hook_logging
    _hook_logging.getLogger("meridian.hooks").info(
        "hooks/session-start cwd raw=%r normalized=%r hostname=%r project_id=%r",
        hook_cwd,
        normalized_hook_cwd,
        hook_hostname,
        project_id,
    )

    if not project_id:
        # No project_id in payload -- auto-route by cwd/hostname match
        projects = await db_module.list_projects(db)
        if not projects:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=400, content={"error": "no projects found -- create a project first"})
        # Routing — hostname-first: one machine = one project by default.
        # Pass 1: exact cwd+hostname match in repo_paths
        matched = None
        norm_cwd = normalized_hook_cwd.lower().rstrip("/")
        norm_hn_ar = hook_hostname.lower() if hook_hostname else ""
        for p in projects:
            cfg = p.get("executor_config") or {}
            if isinstance(cfg, str):
                import json as _json
                try: cfg = _json.loads(cfg)
                except Exception: cfg = {}
            repo_paths = cfg.get("repo_paths") or []
            for rp in repo_paths:
                rp_cwd = _normalize_hook_cwd(rp.get("cwd", "")).lower().rstrip("/")
                rp_host = rp.get("hostname", "").lower()
                if rp_cwd == norm_cwd and (not rp_host or rp_host == norm_hn_ar):
                    matched = p
                    break
            # dab3ba0c — a project still on the legacy single repo_path (not yet
            # migrated to repo_paths) must still win on a cwd match, so cwd-based
            # routing takes priority over the hostname-only fallback below.
            if not matched and norm_cwd:
                legacy_rp = (cfg.get("repo_path") or "").strip()
                if legacy_rp and _normalize_hook_cwd(legacy_rp).lower().rstrip("/") == norm_cwd:
                    matched = p
            if matched:
                break
        # Pass 2: hostname registered in any project's hostnames list (hostname-only match)
        if not matched and norm_hn_ar:
            for p in projects:
                cfg = p.get("executor_config") or {}
                if isinstance(cfg, str):
                    import json as _json_hn
                    try: cfg = _json_hn.loads(cfg)
                    except Exception: cfg = {}
                if any(h.get("hostname", "").lower() == norm_hn_ar for h in (cfg.get("hostnames") or [])):
                    matched = p
                    break
        if not matched:
            # No hostname/cwd match -- if only 1 project, register hostname; else fire HITL
            if len(projects) == 1:
                project = projects[0]
                project_id = project["id"]
                # Auto-register hostname (not just cwd) so all future sessions from this machine route here
                if hook_hostname:
                    try:
                        import json as _json2
                        cfg2 = project.get("executor_config") or {}
                        if isinstance(cfg2, str):
                            try: cfg2 = _json2.loads(cfg2)
                            except Exception: cfg2 = {}
                        hostnames2 = cfg2.get("hostnames") or []
                        norm_hn2 = hook_hostname.lower()
                        if not any(h.get("hostname", "").lower() == norm_hn2 for h in hostnames2):
                            hostnames2.append({"hostname": hook_hostname, "auto_add_cwds": False})
                            cfg2["hostnames"] = hostnames2
                            await db_module.set_executor_config(db, project_id, cfg2)
                            project["executor_config"] = cfg2
                    except Exception:
                        pass
            else:
                # Multiple projects -- fire HITL (deduped by kind+cwd) so user picks in dashboard/chat
                # Check if HITL already pending for this cwd (dedup)
                hitl_exists = False
                for p2 in projects:
                    try:
                        cur = await db.execute(
                            "SELECT id FROM hitl_requests WHERE project_id=? AND kind='hook_project_select' AND status='pending' LIMIT 1",
                            (p2["id"],)
                        )
                        if await cur.fetchone():
                            hitl_exists = True
                            break
                    except Exception:
                        pass
                if not hitl_exists:
                    # File HITL on the first project (arbitrary anchor)
                    try:
                        import json as _json_hitl
                        await db_module.request_hitl(
                            db,
                            project_id=projects[0]["id"],
                            question=f"Which project is this session for? (cwd: {normalized_hook_cwd or hook_cwd})",
                            urgency="normal",
                            kind="hook_project_select",
                            payload=_json_hitl.dumps({"cwd": normalized_hook_cwd or hook_cwd, "raw_cwd": hook_cwd, "hostname": hook_hostname, "projects": [{"id": p2["id"], "name": p2["name"]} for p2 in projects]}),
                        )
                    except Exception:
                        pass
                # Return empty context -- session runs without Meridian context until user answers
                return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ""}}
        else:
            project = matched
            project_id = project["id"]
    else:
        project = await db_module.get_project(db, project_id)
        if project is None:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=404, content={"error": "project not found"})
    # v2.8 — Codex/Claude hook sessions rarely pass a human_id, so they land on
    # the timeline as "(unknown)". Fall back to the workspace display name the
    # user set in Settings so their automated sessions are attributed to them.
    human_id = body.get("human_id")
    if not human_id:
        try:
            ws = await db_module.get_workspace_settings(db)
            human_id = (ws.get("display_name") or "").strip() or None
        except Exception:  # noqa: BLE001
            human_id = None
    result = await _start_session_composite(
        db, project_id, session_name, _data_dir(request),
        human_id=human_id,
        client_type="hook",
    )
    goal = result.get("goal") or {}
    sprint_items = await db_module.get_sprint_items(db, project_id, status="pending")
    recent = result.get("recent_tasks") or []
    lines = [f"PROJECT: {project['name']} ({project_id})"]
    # repo_paths tracking: auto-add new locations, HITL on unknown hostname/path
    if hook_cwd:
        try:
            import json as _json

            def _norm_p(p: str) -> str:
                return _normalize_hook_cwd(p).lower()

            exec_cfg = (project.get("executor_config") or {})
            if isinstance(exec_cfg, str):
                exec_cfg = _json.loads(exec_cfg) or {}
            # Migration: legacy repo_path (single string) → repo_paths array
            repo_paths = exec_cfg.get("repo_paths")
            if repo_paths is None:
                old_rp = (exec_cfg.get("repo_path") or "").strip()
                if old_rp:
                    repo_paths = [{"hostname": "unknown", "cwd": old_rp}]
                    exec_cfg["repo_paths"] = repo_paths
                    exec_cfg.pop("repo_path", None)
                    await db_module.set_executor_config(db, project_id, exec_cfg)
                else:
                    repo_paths = []

            norm_cwd = _norm_p(normalized_hook_cwd or hook_cwd)
            norm_hn = hook_hostname.lower() if hook_hostname else ""

            exact_match = any(
                _norm_p(e.get("hostname", "")).lower() == norm_hn
                and _norm_p(e.get("cwd", "")) == norm_cwd
                for e in repo_paths
            )
            hostname_match = any(
                _norm_p(e.get("hostname", "")).lower() == norm_hn
                for e in repo_paths
            ) if norm_hn else False

            # Hostname registered at machine level (hostnames list) → route silently
            hn_registered = any(
                h.get("hostname", "").lower() == norm_hn
                for h in (exec_cfg.get("hostnames") or [])
            ) if norm_hn else False

            if hn_registered:
                # Hostname registered → proceed regardless of cwd
                hn_entry = next(
                    (h for h in (exec_cfg.get("hostnames") or []) if h.get("hostname", "").lower() == norm_hn),
                    None,
                )
                if hn_entry and hn_entry.get("auto_add_cwds", False) and norm_cwd and not exact_match:
                    repo_paths.append({"hostname": hook_hostname, "cwd": normalized_hook_cwd or hook_cwd})
                    exec_cfg["repo_paths"] = repo_paths
                    await db_module.set_executor_config(db, project_id, exec_cfg)
            elif not repo_paths:
                # Case 1: empty → auto-add silently, proceed
                exec_cfg["repo_paths"] = [{"hostname": hook_hostname or "unknown", "cwd": normalized_hook_cwd or hook_cwd}]
                exec_cfg.pop("repo_path", None)
                await db_module.set_executor_config(db, project_id, exec_cfg)
            elif exact_match:
                # Case 2: exact match → proceed silently
                pass
            elif hostname_match:
                # Case 3: hostname known in repo_paths, cwd differs → blocking HITL (deduped)
                existing_hitl = await db_module.list_hitl_requests(db, project_id, status="pending", limit=50)
                already_pending = any(r.get("kind") == "hook_cwd_mismatch" for r in existing_hitl)
                if not already_pending:
                    hn_paths = [e["cwd"] for e in repo_paths if _norm_p(e.get("hostname", "")).lower() == norm_hn]
                    removal_list = ", ".join(hn_paths)
                    opts = [
                        "Stop — cancel this session",
                        f"Add this location — keep existing, add {normalized_hook_cwd or hook_cwd}",
                        f"I moved here — remove [{removal_list}], add {normalized_hook_cwd or hook_cwd}",
                        "Just this once — proceed without saving",
                    ]
                    hitl = await db_module.request_hitl(
                        db, project_id,
                        question=f"⚠ Session started from {normalized_hook_cwd or hook_cwd} on {hook_hostname} — not a known location for this project.",
                        context=f"Known paths for {hook_hostname}: {removal_list}",
                        urgency="blocking",
                        kind="hook_cwd_mismatch",
                        payload=_json.dumps({"options": opts}),
                    )
                    hid = (hitl or {}).get("id", "")
                    lines.insert(0, (
                        f"⚠ HITL filed (id: {hid}): unrecognised location for this project.\n"
                        f"FIRST: call get_hitl_request(id='{hid}') and act on the answer before starting work.\n"
                        f"Options: (1) Stop (2) Add this location (3) I moved here (4) Just this once"
                    ))
            else:
                # Case 4: unknown hostname → blocking HITL (deduped)
                existing_hitl = await db_module.list_hitl_requests(db, project_id, status="pending", limit=50)
                already_pending = any(r.get("kind") == "hook_cwd_mismatch" for r in existing_hitl)
                if not already_pending:
                    opts = [
                        "Stop — cancel this session",
                        f"Add this machine and location — {hook_hostname}/{normalized_hook_cwd or hook_cwd}",
                        "Just this once — proceed without saving",
                    ]
                    hitl = await db_module.request_hitl(
                        db, project_id,
                        question=f"⚠ New machine {hook_hostname!r} connecting to this project from {normalized_hook_cwd or hook_cwd}.",
                        context=f"cwd: {normalized_hook_cwd or hook_cwd}",
                        urgency="blocking",
                        kind="hook_cwd_mismatch",
                        payload=_json.dumps({"options": opts}),
                    )
                    hid = (hitl or {}).get("id", "")
                    lines.insert(0, (
                        f"⚠ HITL filed (id: {hid}): unknown machine {hook_hostname!r}.\n"
                        f"FIRST: call get_hitl_request(id='{hid}') and act on the answer before starting work.\n"
                        f"Options: (1) Stop (2) Add this machine (3) Just this once"
                    ))
        except Exception:  # noqa: BLE001
            pass
    if goal.get("north_star"):
        lines.append(f"NORTH STAR: {goal['north_star'][:300]}")
    if goal.get("sprint"):
        lines.append(f"SPRINT: {goal['sprint'][:300]}")
    if sprint_items:
        lines.append(f"\nPENDING SPRINT ITEMS ({len(sprint_items)}):")
        for it in sprint_items[:8]:
            lines.append(f"- {it.get('id', '')} {it.get('title', '')[:120]}")
    if recent:
        lines.append("\nRECENT TASKS:")
        for t in recent[:5]:
            lines.append(f"- [{t.get('status','?').upper()}] {str(t.get('description',''))[:120]}")
    lines.append(f"\nSESSION ID: {result.get('session_id', '')}")
    # b11fc37d — only nudge auto-claim for *executor* sessions. Plain chat sessions
    # get full context injected but must NOT be told to claim a sprint item, or
    # casual conversational sessions start grabbing work. The local hook forwards
    # Claude Code's permission_mode; bypassPermissions (i.e.
    # --dangerously-skip-permissions) or an explicit executor flag means executor.
    if sprint_items and _hook_is_executor(body):
        top_item_id = sprint_items[0].get("id", "")
        lines.append(
            f"\nINSTRUCTION: Your first MCP call must be claim_sprint_item on the top "
            f"pending sprint item before starting any work. "
            f"Top item id: {top_item_id}"
        )
    # fffdcc95 — yellow warning when an executor runs in the main checkout instead of an
    # isolated .claude/worktrees/ tree; parallel sessions there can clobber each other.
    if _hook_is_executor(body) and "/.claude/worktrees/" not in (normalized_hook_cwd or "").lower():
        lines.insert(0, (
            "⚠️ Parallel safety degraded: this executor is running in the main checkout, "
            "not a .claude/worktrees/ isolate. Stage ONLY your own files by path "
            "(never git add -A) so you don't sweep up another session's work."
        ))
    additional_context = "\n".join(lines)
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": additional_context}}


@app.post("/hooks/stop")
async def hooks_stop(body: dict[str, Any], request: Request) -> dict[str, Any]:
    """Claude Code / Codex Stop hook.

    Accepts {project_id, session_id?}. Fires auto_capture + delta handoff
    and returns immediately. Fire-and-forget — the agent does not wait for
    this to complete before exiting.

    Hosted callers can authenticate with Authorization: Bearer sk_meridian_...
    to route directly to their tenant DB. Local/browser-session behavior is
    unchanged when no Bearer token is supplied.
    """
    project_id = (body.get("project_id") or "").strip()
    session_id = (body.get("session_id") or "").strip() or None
    if not project_id:
        return {"ok": False, "error": "project_id required"}
    db = await _resolve_hook_db(request)
    if session_id:
        try:
            await db_module.auto_capture_session(db, project_id, session_id)
        except Exception:  # noqa: BLE001
            pass
        await _finalize_session_md(db, project_id, session_id)
    try:
        from . import handoff as handoff_module_local
        await asyncio.wait_for(
            handoff_module_local.generate_handoff(
                db, project_id, _data_dir(request), mode="delta", session_id=session_id
            ),
            timeout=20.0,
        )
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True}


# ---------------------------------------------------------------------------
# Session queue — queue the next /goal to run back-to-back (10e6b265)
# ---------------------------------------------------------------------------


@app.post("/projects/{project_id}/queue-session")
async def queue_session(project_id: str, request: Request) -> dict[str, Any]:
    """Queue the next /goal string; it's appended to the next handoff and then
    cleared. Empty body clears the queue."""
    db = await _db(request)
    body = await request.json()
    goal = (body.get("goal") or "").strip()
    await db_module.set_queued_session(db, project_id, goal or None)
    return {"queued": bool(goal), "goal": goal or None}


@app.get("/projects/{project_id}/queued-session")
async def get_queued_session_endpoint(project_id: str, request: Request) -> dict[str, Any]:
    """Return the currently queued next-session goal, or null."""
    db = await _db(request)
    return {"goal": await db_module.get_queued_session(db, project_id)}


# ---------------------------------------------------------------------------
# v2.0 — Remote MCP endpoint (HTTP JSON-RPC 2.0 transport)
# ---------------------------------------------------------------------------

_MCP_PROTOCOL_VERSION = "2025-03-26"
_MCP_SERVER_INFO = {"name": "meridian", "version": _VERSION}


def _jsonrpc_ok(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_err(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# GitHub MCP tools — injected per-tenant when github_pat is set
# ---------------------------------------------------------------------------

_GITHUB_TOOL_NAMES = frozenset({
    "read_file", "list_files", "search_code", "get_commits", "get_commit", "search_commits",
    "get_workflow_runs", "get_workflow_run_logs", "trigger_workflow", "git_diff",
    "list_branches", "list_issues", "create_issue", "get_issue",
})


_GITHUB_READ_ONLY = frozenset({
    "read_file", "list_files", "search_code", "get_commits", "search_commits",
    "get_commit", "get_workflow_runs", "get_workflow_run_logs",
    "git_diff", "list_branches", "list_issues", "get_issue",
})
_GITHUB_TITLE_OVERRIDES: dict[str, str] = {
    "read_file": "Read File",
    "list_files": "List Files",
    "search_code": "Search Code",
    "get_commits": "Get Commits",
    "search_commits": "Search Commits",
    "get_commit": "Get Commit",
    "get_workflow_runs": "Get Workflow Runs",
    "get_workflow_run_logs": "Get Workflow Run Logs",
    "trigger_workflow": "Trigger Workflow",
    "git_diff": "Git Diff",
    "list_branches": "List Branches",
    "list_issues": "List Issues",
    "create_issue": "Create Issue",
    "get_issue": "Get Issue",
}


def _github_tools_for_tenant(tenant: dict) -> list[dict[str, Any]]:
    """Return the 5 GitHub tool defs if the tenant has a GitHub PAT set."""
    if not db_module.decrypt_field(tenant.get("github_pat")):
        return []
    _pid_prop = {"project_id": {"type": "string", "description": "Project ID whose GitHub repo to use."}}
    _tools = [
        {
            "name": "read_file",
            "description": "Read a file from the project's connected GitHub repository. Returns decoded UTF-8 content.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID whose GitHub repo to use."},
                    "path": {"type": "string", "description": "File path relative to repo root (e.g. src/main.py)"},
                    "ref": {"type": "string", "description": "Branch, tag, or commit SHA (default: configured branch)"},
                },
                "required": ["project_id", "path"],
            },
        },
        {
            "name": "list_files",
            "description": "List all files in the project's connected GitHub repository (recursive tree).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **_pid_prop,
                    "path": {"type": "string", "description": "Subdirectory to list (default: repo root)"},
                },
                "required": ["project_id"],
            },
        },
        {
            "name": "search_code",
            "description": "Search code in the project's connected GitHub repository using GitHub code search.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **_pid_prop,
                    "query": {"type": "string", "description": "Search query string (GitHub code search syntax)"},
                },
                "required": ["project_id", "query"],
            },
        },
        {
            "name": "get_commits",
            "description": "Return recent commits from the project's connected GitHub repository. Returns helpful error if no GitHub repo is connected.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **_pid_prop,
                    "limit": {"type": "integer", "description": "Number of commits to return (default: 50, max: 50)"},
                    "since": {"type": "string", "description": "ISO 8601 date string to filter commits after this date (optional, e.g. '2024-01-01T00:00:00Z')"},
                },
                "required": ["project_id"],
            },
        },
        {
            "name": "search_commits",
            "description": "Search recent commits by message substring. Fetches up to 100 recent commits and filters by case-insensitive match. Returns helpful error if no GitHub repo is connected.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **_pid_prop,
                    "query": {"type": "string", "description": "Case-insensitive substring to search in commit messages"},
                    "limit": {"type": "integer", "description": "Max results to return (default: 20)"},
                },
                "required": ["project_id", "query"],
            },
        },
        {
            "name": "get_commit",
            "description": "Return details for a specific commit from the project's connected GitHub repository.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **_pid_prop,
                    "sha": {"type": "string", "description": "Full or short commit SHA"},
                },
                "required": ["project_id", "sha"],
            },
        },
        {
            "name": "get_workflow_runs",
            "description": "List recent GitHub Actions workflow runs with status/conclusion/url. Optionally filter by workflow file name (e.g. deploy.yml).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **_pid_prop,
                    "workflow_name": {"type": "string", "description": "Workflow file name (e.g. deploy.yml) to filter by. Omit for all runs."},
                    "limit": {"type": "integer", "description": "Max runs to return (default: 10, max: 50)"},
                },
                "required": ["project_id"],
            },
        },
        {
            "name": "get_workflow_run_logs",
            "description": "Return the failed job steps for a GitHub Actions run, with the last 50 log lines per failed job. Use to see why CI is red.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **_pid_prop,
                    "run_id": {"type": "string", "description": "Workflow run id (from get_workflow_runs)"},
                },
                "required": ["project_id", "run_id"],
            },
        },
        {
            "name": "trigger_workflow",
            "description": "Fire a GitHub Actions workflow_dispatch event. ref defaults to the project's configured branch (or main). inputs is an optional object of workflow inputs.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **_pid_prop,
                    "workflow_name": {"type": "string", "description": "Workflow file name (e.g. deploy.yml)"},
                    "inputs": {"type": "object", "description": "Optional workflow_dispatch inputs."},
                    "ref": {"type": "string", "description": "Git ref to run on (default: configured branch or main)."},
                },
                "required": ["project_id", "workflow_name"],
            },
        },
        {
            "name": "git_diff",
            "description": "Compare two refs (base...head) in the connected repo. Returns changed files with additions/deletions/patch and the total commit count.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **_pid_prop,
                    "base": {"type": "string", "description": "Base ref (branch, tag, or SHA)"},
                    "head": {"type": "string", "description": "Head ref (branch, tag, or SHA)"},
                },
                "required": ["project_id", "base", "head"],
            },
        },
        {
            "name": "list_branches",
            "description": "List branches in the project's connected GitHub repository. Returns name, head sha, and protected flag.",
            "inputSchema": {
                "type": "object",
                "properties": {**_pid_prop},
                "required": ["project_id"],
            },
        },
        {
            "name": "list_issues",
            "description": "List issues in the connected repo. Returns number, title, state, labels, created_at, url, and a body preview. Pull requests are excluded.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **_pid_prop,
                    "state": {"type": "string", "enum": ["open", "closed", "all"], "description": "Issue state filter (default: open)"},
                    "labels": {"type": "string", "description": "Comma-separated label names to filter by."},
                    "limit": {"type": "integer", "description": "Max issues to return (default: 20, max: 50)"},
                },
                "required": ["project_id"],
            },
        },
        {
            "name": "create_issue",
            "description": "Open a new issue in the connected repo. Returns the created issue number and url.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **_pid_prop,
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "Optional label names."},
                },
                "required": ["project_id", "title"],
            },
        },
        {
            "name": "get_issue",
            "description": "Read a single issue plus its comments from the connected repo.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **_pid_prop,
                    "number": {"type": "integer", "description": "Issue number"},
                },
                "required": ["project_id", "number"],
            },
        },
    ]
    for _t in _tools:
        _ro = _t["name"] in _GITHUB_READ_ONLY
        _gh_title = _GITHUB_TITLE_OVERRIDES.get(_t["name"], _t["name"].replace("_", " ").title())
        _t["title"] = _gh_title
        _t["annotations"] = {
            "title": _gh_title,
            "readOnlyHint": _ro,
            "destructiveHint": False,
            "openWorldHint": False,
            "idempotentHint": _ro,
        }
    return _tools


async def _dispatch_github_tool(name: str, args: dict[str, Any], tenant: dict, db: Any) -> Any:
    # Guard: check if project has a GitHub repo connected
    project_id = args.get("project_id")
    if project_id:
        try:
            proj = await db_module.get_project(db, project_id)
            if proj and not proj.get("github_repo"):
                return {
                    "error": "no_github_repo",
                    "message": f"No GitHub repo connected for project {project_id}. "
                               f"Go to Settings → Connect GitHub repo to connect one.",
                }
        except Exception:
            pass
    """Dispatch a GitHub MCP tool call using the tenant's PAT and per-project repo."""
    import httpx as _httpx
    import base64 as _b64
    pat = db_module.decrypt_field(tenant.get("github_pat"))
    if not pat:
        return {"error": "GitHub not connected — connect via Settings > Connect Claude Code > GitHub"}
    project_id = (args.get("project_id") or "").strip()
    if not project_id:
        return {"error": "project_id is required — pass the project whose GitHub repo you want to read"}
    project = await db_module.get_project(db, project_id)
    if project is None:
        return {"error": f"project '{project_id}' not found"}
    repo = (project.get("github_repo") or "").strip()
    branch = (project.get("github_branch") or "main").strip()
    if not repo:
        return {"error": f"No GitHub repo connected for project {project_id} — use POST /projects/{project_id}/github/connect"}
    gh_headers = {"Authorization": f"token {pat}", "Accept": "application/vnd.github+json"}
    async with _httpx.AsyncClient(timeout=15.0) as http:
        if name == "read_file":
            path = args.get("path", "")
            ref = args.get("ref") or branch
            r = await http.get(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                headers=gh_headers,
                params={"ref": ref},
            )
            if r.status_code == 404:
                return {"error": f"File not found: {path}"}
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return {"entries": [{"name": e["name"], "type": e["type"], "path": e["path"]} for e in data]}
            content_b64 = data.get("content", "")
            content = _b64.b64decode(content_b64).decode("utf-8", errors="replace")
            return {"path": data["path"], "sha": data["sha"], "size": data["size"], "content": content}

        if name == "list_files":
            path = args.get("path") or ""
            r = await http.get(
                f"https://api.github.com/repos/{repo}/git/trees/HEAD",
                headers=gh_headers,
                params={"recursive": "1"},
            )
            r.raise_for_status()
            tree = r.json().get("tree", [])
            files = [e["path"] for e in tree if e.get("type") == "blob"]
            if path:
                files = [f for f in files if f.startswith(path)]
            return {"repo": repo, "count": len(files), "files": files}

        if name == "search_code":
            query = args.get("query", "")
            r = await http.get(
                "https://api.github.com/search/code",
                headers=gh_headers,
                params={"q": f"{query} repo:{repo}"},
            )
            r.raise_for_status()
            items = r.json().get("items", [])
            return {
                "total_count": r.json().get("total_count", 0),
                "items": [{"path": i["path"], "sha": i["sha"], "url": i.get("html_url", "")} for i in items[:20]],
            }

        if name == "get_commits":
            limit = min(int(args.get("limit") or 50), 50)
            params: dict[str, str] = {"per_page": str(limit)}
            if args.get("since"):
                params["since"] = args["since"]
            r = await http.get(
                f"https://api.github.com/repos/{repo}/commits",
                headers=gh_headers,
                params=params,
            )
            r.raise_for_status()
            commits = r.json()
            return {
                "commits": [
                    {
                        "sha": c["sha"][:12],
                        "message": c["commit"]["message"].split("\n")[0],
                        "author": c["commit"]["author"]["name"],
                        "date": c["commit"]["author"]["date"],
                    }
                    for c in commits
                ]
            }

        if name == "search_commits":
            query = (args.get("query") or "").lower()
            limit = min(int(args.get("limit") or 20), 100)
            r = await http.get(
                f"https://api.github.com/repos/{repo}/commits",
                headers=gh_headers,
                params={"per_page": "100"},
            )
            r.raise_for_status()
            all_commits = r.json()
            matched = [
                {
                    "sha": c["sha"][:12],
                    "message": c["commit"]["message"].split("\n")[0],
                    "author": c["commit"]["author"]["name"],
                    "date": c["commit"]["author"]["date"],
                }
                for c in all_commits
                if query in c["commit"]["message"].lower()
            ][:limit]
            return {"query": args.get("query"), "count": len(matched), "commits": matched}

        if name == "get_commit":
            sha = args.get("sha", "")
            r = await http.get(
                f"https://api.github.com/repos/{repo}/commits/{sha}",
                headers=gh_headers,
            )
            if r.status_code == 404:
                return {"error": f"Commit not found: {sha}"}
            r.raise_for_status()
            c = r.json()
            files = [{"filename": f["filename"], "status": f["status"], "additions": f.get("additions", 0), "deletions": f.get("deletions", 0)} for f in c.get("files", [])[:50]]
            return {
                "sha": c["sha"],
                "message": c["commit"]["message"],
                "author": c["commit"]["author"]["name"],
                "date": c["commit"]["author"]["date"],
                "files_changed": len(c.get("files", [])),
                "files": files,
            }

        if name == "get_workflow_runs":
            limit = min(int(args.get("limit") or 10), 50)
            wf = (args.get("workflow_name") or "").strip()
            url = (
                f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/runs"
                if wf else
                f"https://api.github.com/repos/{repo}/actions/runs"
            )
            r = await http.get(url, headers=gh_headers, params={"per_page": str(limit)})
            if r.status_code == 404:
                return {"error": f"Workflow not found: {wf}" if wf else "No Actions runs found"}
            r.raise_for_status()
            runs = r.json().get("workflow_runs", [])
            return {
                "repo": repo,
                "count": len(runs),
                "runs": [
                    {
                        "id": run["id"],
                        "name": run.get("name"),
                        "status": run.get("status"),
                        "conclusion": run.get("conclusion"),
                        "created_at": run.get("created_at"),
                        "html_url": run.get("html_url"),
                    }
                    for run in runs
                ],
            }

        if name == "get_workflow_run_logs":
            run_id = str(args.get("run_id") or "").strip()
            r = await http.get(
                f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs",
                headers=gh_headers,
            )
            if r.status_code == 404:
                return {"error": f"Run not found: {run_id}"}
            r.raise_for_status()
            jobs = r.json().get("jobs", [])
            failed = [j for j in jobs if j.get("conclusion") == "failure"]
            out = []
            for j in failed:
                failed_steps = [
                    {"name": s.get("name"), "number": s.get("number")}
                    for s in j.get("steps", [])
                    if s.get("conclusion") == "failure"
                ]
                log_excerpt = ""
                try:
                    lr = await http.get(
                        f"https://api.github.com/repos/{repo}/actions/jobs/{j['id']}/logs",
                        headers=gh_headers,
                        follow_redirects=True,
                    )
                    if lr.status_code == 200:
                        lines = lr.text.splitlines()
                        log_excerpt = "\n".join(lines[-50:])
                except Exception:  # noqa: BLE001 — logs are best-effort
                    log_excerpt = ""
                out.append({
                    "job": j.get("name"),
                    "job_id": j.get("id"),
                    "html_url": j.get("html_url"),
                    "failed_steps": failed_steps,
                    "log_tail": log_excerpt,
                })
            return {"run_id": run_id, "failed_job_count": len(failed), "failed_jobs": out}

        if name == "trigger_workflow":
            wf = (args.get("workflow_name") or "").strip()
            ref = (args.get("ref") or branch).strip()
            inputs = args.get("inputs") or {}
            body: dict[str, Any] = {"ref": ref}
            if inputs:
                body["inputs"] = inputs
            r = await http.post(
                f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/dispatches",
                headers=gh_headers,
                json=body,
            )
            if r.status_code == 404:
                return {"error": f"Workflow not found: {wf}"}
            if r.status_code not in (201, 204):
                return {"error": f"Dispatch failed ({r.status_code}): {r.text[:200]}"}
            return {"dispatched": True, "workflow": wf, "ref": ref, "inputs": inputs}

        if name == "git_diff":
            base = (args.get("base") or "").strip()
            head = (args.get("head") or "").strip()
            r = await http.get(
                f"https://api.github.com/repos/{repo}/compare/{base}...{head}",
                headers=gh_headers,
            )
            if r.status_code == 404:
                return {"error": f"Refs not found: {base}...{head}"}
            r.raise_for_status()
            data = r.json()
            files = [
                {
                    "filename": f["filename"],
                    "status": f.get("status"),
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                    "patch": f.get("patch", ""),
                }
                for f in data.get("files", [])[:50]
            ]
            return {
                "base": base,
                "head": head,
                "total_commits": data.get("total_commits", 0),
                "files": files,
            }

        if name == "list_branches":
            r = await http.get(
                f"https://api.github.com/repos/{repo}/branches",
                headers=gh_headers,
                params={"per_page": "100"},
            )
            r.raise_for_status()
            branches = r.json()
            return {
                "repo": repo,
                "count": len(branches),
                "branches": [
                    {
                        "name": b["name"],
                        "sha": b.get("commit", {}).get("sha", "")[:12],
                        "protected": b.get("protected", False),
                    }
                    for b in branches
                ],
            }

        if name == "list_issues":
            state = (args.get("state") or "open").strip()
            limit = min(int(args.get("limit") or 20), 50)
            params = {"state": state, "per_page": str(limit)}
            if args.get("labels"):
                params["labels"] = args["labels"]
            r = await http.get(
                f"https://api.github.com/repos/{repo}/issues",
                headers=gh_headers,
                params=params,
            )
            r.raise_for_status()
            issues = r.json()
            return {
                "repo": repo,
                "state": state,
                "issues": [
                    {
                        "number": i["number"],
                        "title": i.get("title"),
                        "state": i.get("state"),
                        "labels": [lbl["name"] for lbl in i.get("labels", [])],
                        "created_at": i.get("created_at"),
                        "html_url": i.get("html_url"),
                        "body_preview": (i.get("body") or "")[:200],
                    }
                    for i in issues
                    if "pull_request" not in i  # exclude PRs
                ],
            }

        if name == "create_issue":
            title = (args.get("title") or "").strip()
            if not title:
                return {"error": "title is required"}
            body_payload: dict[str, Any] = {"title": title, "body": args.get("body") or ""}
            if args.get("labels"):
                body_payload["labels"] = args["labels"]
            r = await http.post(
                f"https://api.github.com/repos/{repo}/issues",
                headers=gh_headers,
                json=body_payload,
            )
            if r.status_code not in (200, 201):
                return {"error": f"Create issue failed ({r.status_code}): {r.text[:200]}"}
            i = r.json()
            return {
                "number": i["number"],
                "title": i.get("title"),
                "state": i.get("state"),
                "html_url": i.get("html_url"),
            }

        if name == "get_issue":
            number = str(args.get("number") or "").strip()
            r = await http.get(
                f"https://api.github.com/repos/{repo}/issues/{number}",
                headers=gh_headers,
            )
            if r.status_code == 404:
                return {"error": f"Issue not found: {number}"}
            r.raise_for_status()
            i = r.json()
            comments = []
            try:
                cr = await http.get(
                    f"https://api.github.com/repos/{repo}/issues/{number}/comments",
                    headers=gh_headers,
                    params={"per_page": "30"},
                )
                if cr.status_code == 200:
                    comments = [
                        {
                            "author": c.get("user", {}).get("login"),
                            "created_at": c.get("created_at"),
                            "body": (c.get("body") or "")[:500],
                        }
                        for c in cr.json()
                    ]
            except Exception:  # noqa: BLE001 — comments are best-effort
                comments = []
            return {
                "number": i["number"],
                "title": i.get("title"),
                "state": i.get("state"),
                "labels": [lbl["name"] for lbl in i.get("labels", [])],
                "body": i.get("body") or "",
                "html_url": i.get("html_url"),
                "comments": comments,
            }

    return {"error": f"Unknown GitHub tool: {name}"}


async def _handle_mcp_request(
    body: dict[str, Any], db: Any, data_dir: str,
    tenant: dict[str, Any] | None = None,
    token_type: str = "readwrite",
) -> dict[str, Any]:
    """Dispatch one JSON-RPC 2.0 MCP request and return the response dict."""
    req_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}

    if method == "initialize":
        return _jsonrpc_ok(req_id, {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "serverInfo": _MCP_SERVER_INFO,
            "capabilities": {"tools": {}},
        })

    if method in ("notifications/initialized", "ping"):
        return _jsonrpc_ok(req_id, {})

    if method == "tools/list":
        tools = list(_MCP_TOOLS_LIST)
        if tenant:
            tools = tools + _github_tools_for_tenant(tenant)
        return _jsonrpc_ok(req_id, {"tools": tools})

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        if token_type == "readonly" and name not in _mcp_readonly_tools:
            return _jsonrpc_err(req_id, -32603, f"tool '{name}' not allowed for read-only tokens")
        try:
            if name in _GITHUB_TOOL_NAMES and tenant:
                result = await _dispatch_github_tool(name, args, tenant, db)
            else:
                result = await _dispatch_mcp_tool(name, args, db, data_dir, tenant=tenant)
            return _jsonrpc_ok(req_id, {"content": [{"type": "text", "text": json.dumps(result)}]})
        except Exception as exc:
            return _jsonrpc_err(req_id, -32603, str(exc))

    return _jsonrpc_err(req_id, -32601, f"method not found: {method}")


async def _maybe_add_log_task_nudge(db: Any, task: dict[str, Any]) -> dict[str, Any]:
    """Append a soft nudge to log_task result when session logs many tasks with no sprint work."""
    try:
        settings = await db_module.get_workspace_settings(db)
        threshold = settings.get("log_task_sprint_nudge_threshold", 5)
        if not threshold:
            return task
        session_id = task.get("session_id")
        project_id = task.get("project_id")
        if not session_id or not project_id:
            return task
        async with db.execute(
            "SELECT COUNT(*) AS cnt FROM task_log WHERE session_id = ? AND status != 'failed'",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        task_count = int(row["cnt"]) if row else 0
        if task_count < threshold:
            return task
        async with db.execute(
            "SELECT COUNT(*) AS cnt FROM sprint_items WHERE project_id = ? "
            "AND claimed_at >= (SELECT created_at FROM sessions WHERE id = ?)",
            (project_id, session_id),
        ) as cur:
            row = await cur.fetchone()
        sprint_count = int(row["cnt"]) if row else 0
        if sprint_count > 0:
            return task
        task = dict(task)
        task["nudge"] = (
            f"You have logged {task_count} tasks inline with no sprint items. "
            "If this is coordinated work, consider filing sprint items for better tracking. "
            "Set log_task_sprint_nudge_threshold=0 in workspace settings to disable."
        )
    except Exception:  # noqa: BLE001
        pass
    return task


def _parse_touches_files(raw: Any) -> list[str]:
    """Decode a sprint item's touches_files field into normalized file paths."""
    if raw is None:
        return []
    if isinstance(raw, list):
        values = raw
    else:
        text = str(raw).strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
            values = decoded if isinstance(decoded, list) else [decoded]
        except Exception:  # noqa: BLE001
            values = [part.strip() for part in text.split(",")]
    paths: list[str] = []
    for value in values:
        path = str(value or "").strip().replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        if path:
            paths.append(path)
    return paths


async def _sprint_item_file_claim_conflicts(
    db: Any,
    project_id: str,
    item_id: str,
    *,
    exclude_session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return active file locks overlapping a sprint item's touches_files."""
    item = await db_module.get_sprint_item(db, item_id)
    if item is None or item.get("project_id") != project_id:
        return []
    touches = {path.lower() for path in _parse_touches_files(item.get("touches_files"))}
    if not touches:
        return []
    await db_module.expire_file_locks(db)
    params: list[Any] = [project_id]
    exclude_clause = ""
    if exclude_session_id:
        exclude_clause = "AND fl.session_id != ? "
        params.append(exclude_session_id)
    async with db.execute(
        "SELECT fl.file_path, fl.session_id, s.name AS session_name, s.last_seen "
        "FROM file_locks fl "
        "JOIN sessions s ON s.id = fl.session_id "
        "WHERE s.project_id = ? "
        f"{exclude_clause}"
        "AND s.status IN ('active', 'live') "
        "AND (s.last_seen IS NULL OR s.last_seen > datetime('now', '-10 minutes'))",
        tuple(params),
    ) as cur:
        rows = await cur.fetchall()
    conflicts: list[dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        path = str(r.get("file_path") or "").strip().replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        if path.lower() not in touches:
            continue
        conflicts.append({
            "file_path": path,
            "session_id": r.get("session_id"),
            "session_name": r.get("session_name"),
            "last_seen": r.get("last_seen"),
            "sprint_item_id": item_id,
        })
    return conflicts


async def _fetch_recent_commits(
    project: dict[str, Any],
    tenant: dict[str, Any] | None,
) -> list[str]:
    """Fetch last 20 commit messages for a project.

    Tries GitHub API if tenant has github_pat and project has github_repo;
    falls back to local ``git log --oneline -20``. Returns plain message strings.
    Non-fatal — returns empty list on any failure.
    """
    import subprocess as _sp  # noqa: PLC0415
    commits: list[str] = []
    try:
        if tenant:
            pat = db_module.decrypt_field(tenant.get("github_pat"))
            repo = (project.get("github_repo") or "").strip()
            if pat and repo:
                import httpx as _httpx  # noqa: PLC0415
                gh_headers = {
                    "Authorization": f"token {pat}",
                    "Accept": "application/vnd.github+json",
                }
                async with _httpx.AsyncClient(timeout=8.0) as http:
                    r = await http.get(
                        f"https://api.github.com/repos/{repo}/commits",
                        headers=gh_headers,
                        params={"per_page": "20"},
                    )
                    if r.status_code == 200:
                        for c in r.json():
                            msg = c["commit"]["message"].split("\n")[0]
                            commits.append(msg)
                if commits:
                    return commits
    except Exception:  # noqa: BLE001
        pass
    try:
        result = _sp.run(
            ["git", "log", "--oneline", "-20"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line and " " in line:
                _, _, msg = line.partition(" ")
                commits.append(msg)
    except Exception:  # noqa: BLE001
        pass
    return commits


async def _dispatch_mcp_tool(
    name: str,
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None = None,
) -> Any:
    """Route a tools/call to the appropriate db_module function."""
    # Tenant scope for the workspace layer (notes/decisions/settings). None for
    # self-host / unauthenticated; the db functions then skip isolation.
    _mcp_tenant_id = tenant.get("id") if tenant else None
    # b6ab6e83 — project_name resolver: accept project_name as alternative to
    # project_id, and resolve non-UUID project_id values as human-readable names.
    _pid_raw = args.get("project_id", "")
    _pname_raw = args.get("project_name", "")
    _is_uuid = bool(re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        _pid_raw, re.I,
    ))
    if _pname_raw or (_pid_raw and not _is_uuid):
        _lookup = _pname_raw or _pid_raw
        _resolved_proj = await db_module.get_project_by_name(db, _lookup)
        if _resolved_proj:
            args = {**args, "project_id": _resolved_proj["id"]}
        elif _pname_raw and not _pid_raw:
            raise ValueError(f"no project found matching name '{_lookup}'")
    if name == "create_project":
        existing = await db_module.get_project_by_name(db, args["name"])
        if existing is not None:
            return {"error": f"project '{args['name']}' already exists", "project": existing}
        return await db_module.create_project(db, args["name"])
    if name == "register_session":
        hid = args.get("human_id")
        if not hid and not _hosted_mode():
            hid = db_module.get_default_human_id()
        return await db_module.register_session(
            db, args["project_id"], args["session_name"],
            hid,
            agent_framework=args.get("agent_framework", "claude_code"),
            client_type=args.get("client"),
        )
    if name == "start_session":
        return await _start_session_composite(
            db,
            args["project_id"],
            args["session_name"],
            data_dir,
            human_id=args.get("human_id"),
            client_type=args.get("client"),
            role=args.get("role"),
        )
    if name == "list_projects":
        return await db_module.list_project_summaries(db)
    if name == "get_project_by_name":
        project = await db_module.get_project_by_name(db, args["name"])
        if project is None:
            raise ValueError(f"no project found matching '{args['name']}'")
        return {
            "id": project["id"],
            "name": project["name"],
            "sprint": project.get("sprint"),
        }
    if name == "get_goal":
        goal = await db_module.get_goal(db, args["project_id"])
        if goal and goal.get("decisions") and len(goal["decisions"]) > 3000:
            goal["decisions"] = goal["decisions"][-3000:]
        return goal
    if name == "set_goal":
        return await db_module.set_goal(db, args["project_id"], args["content"])
    if name == "set_north_star":
        return await db_module.set_north_star(db, args["project_id"], args["north_star"])
    if name == "log_task":
        validate_input_size(args.get("description"), "description", 50_000)
        task = await db_module.log_task(
            db, args["session_id"], args["project_id"],
            args["description"], args.get("status", "done"),
            parent_task_id=args.get("parent_task_id"),
        )
        return await _maybe_add_log_task_nudge(db, task)
    if name == "get_tasks":
        return await db_module.get_tasks(db, args["project_id"], args.get("limit", 20))
    if name == "search_tasks":
        return await db_module.search_tasks(
            db, args["project_id"], args["query"], args.get("limit", 5)
        )
    if name == "generate_handoff":
        from . import handoff as handoff_module_local
        session_id = args.get("session_id")
        if not isinstance(session_id, str):
            session_id = None
        mode = handoff_module_local.resolve_handoff_mode(
            args.get("mode"),
            session_id,
        )
        # Fetch recent commits for reconcile annotations (non-fatal)
        _gh_project = await db_module.get_project(db, args["project_id"])
        _gh_commits = await _fetch_recent_commits(_gh_project or {}, tenant)
        try:
            path, content = await asyncio.wait_for(
                handoff_module_local.generate_handoff(
                    db,
                    args["project_id"],
                    data_dir,
                    mode=mode,
                    session_id=session_id,
                    commit_messages=_gh_commits or [],
                ),
                timeout=90.0,
            )
        except asyncio.TimeoutError:
            path, content = await handoff_module_local._generate_handoff_l0(
                db, args["project_id"], data_dir
            )
            mode = "full"
        return {"file_path": path, "content": content, "mode": mode}
    if name == "pin_decision":
        validate_input_size(args.get("title"), "decision title", 500)
        validate_input_size(args.get("body"), "decision body", 100_000)
        category = args.get("category", "TECHNICAL")
        result = await db_module.pin_decision(
            db, args["project_id"], args["title"], args["body"], category,
        )
        await _append_decision_to_md(args["title"], args["body"], category)
        return result
    if name == "update_decision":
        new_title = args.get("new_title")
        new_body = args.get("new_body")
        if new_title and new_body:
            return await db_module.supersede_pinned_decision(
                db, args["decision_id"], new_title, new_body, args.get("category"),
            )
        result = await db_module.update_pinned_decision(
            db, args["decision_id"],
            body=args.get("body"),
            title=args.get("title"),
            category=args.get("category"),
            status=args.get("status"),
            superseded_by=args.get("superseded_by"),
        )
        if result is None:
            raise ValueError("decision not found")
        return result
    if name == "get_pinned_decisions":
        return await db_module.get_pinned_decisions(
            db, args["project_id"],
            include_superseded=bool(args.get("include_superseded", False)),
        )
    if name == "archive_decision":
        deleted = await db_module.delete_pinned_decision(db, args["decision_id"])
        if not deleted:
            raise ValueError("decision not found")
        return {"deleted": True, "decision_id": args["decision_id"]}
    if name == "checkpoint":
        session_id = args["session_id"]
        project_id = args["project_id"]
        await db_module.auto_capture_session(db, project_id, session_id)
        await _finalize_session_md(db, project_id, session_id)
        from . import handoff as handoff_module_local
        # Fetch recent commits for reconcile annotations (non-fatal)
        _ckpt_project = await db_module.get_project(db, project_id)
        _commit_messages = await _fetch_recent_commits(_ckpt_project or {}, tenant)
        try:
            _, content = await asyncio.wait_for(
                handoff_module_local.generate_handoff(
                    db, project_id, data_dir, mode="delta", session_id=session_id,
                    commit_messages=_commit_messages or [],
                ),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            content = "delta handoff timed out"
        pending_items = await db_module.get_sprint_items(db, project_id, status="pending")
        # Log drift warnings for high-confidence reconcile matches (non-fatal)
        if _commit_messages and pending_items:
            try:
                _commits_for_reconcile = []
                for _line in _commit_messages:
                    _sha = ""
                    _msg = _line
                    _commits_for_reconcile.append({"sha": _sha, "message": _msg})
                _matches = handoff_module_local.reconcile_sprint_items(
                    pending_items, _commits_for_reconcile
                )
                for _m in _matches:
                    if _m.get("confidence") == "high":
                        _first_sha = (_m["matching_commits"][0].get("sha") or "")[:8]
                        await db_module.log_task(
                            db, session_id, project_id,
                            f"Sprint board drift detected: {_m['item_id'][:8]} "
                            f"may already be done (matches commit {_first_sha})",
                            status="pending",
                        )
            except Exception:  # noqa: BLE001
                pass
        ids_str = ", ".join(it["id"][:8] for it in pending_items[:8])
        next_goal = (
            f'/goal Complete sprint items: {", ".join(it["id"] for it in pending_items[:8])}. '
            f"Done when complete_sprint_item()\'d, tests pass, generate_handoff called."
        ) if pending_items else "/goal Continue work — all sprint items done."
        # 04f03ee4 — include start_session one-liner so next session can resume immediately
        start_fresh = f'start_session(project_id="{project_id}", session_name="describe-what-youre-doing")'
        # fa595ad8 — store snapshot for Recent Sessions dashboard panel (non-fatal)
        # v3.1 — snapshot now lives on sessions.checkpoint_data, not a checkpoint:* note.
        try:
            from datetime import datetime as _ckpt_dt, timezone as _ckpt_tz
            async with db.execute(
                "SELECT name FROM sessions WHERE id = ?", (session_id,)
            ) as _sc:
                _sr = await _sc.fetchone()
            _session_name = (_sr["name"] if _sr else None) or session_id[:8]
            async with db.execute(
                "SELECT COUNT(*) AS n FROM task_log "
                "WHERE session_id = ? AND status = 'done'",
                (session_id,),
            ) as _tc:
                _tr = await _tc.fetchone()
            _items_done = int(_tr["n"]) if _tr else 0
            _summary_line = (content or "").split("\n")[0][:140]
            await db_module.set_session_checkpoint(
                db, session_id,
                {
                    "session_id": session_id,
                    "session_name": _session_name,
                    "items_done": _items_done,
                    "summary_line": _summary_line,
                    "next_goal": next_goal,
                    "start_fresh": start_fresh,
                    "checkpointed_at": _ckpt_dt.now(_ckpt_tz.utc).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                },
            )
        except Exception:
            pass  # non-fatal — checkpoint still returns normally
        # Write plain-text session_summary for RECENT RUNS panel display
        try:
            _shipped_titles: list[str] = []
            async with db.execute(
                "SELECT si.title FROM sprint_items si "
                "JOIN task_log tl ON tl.id = si.task_id "
                "WHERE tl.session_id = ? AND si.status = 'done'",
                (session_id,),
            ) as _si_cur:
                for _si_row in await _si_cur.fetchall():
                    _t = _si_row["title"] if hasattr(_si_row, "__getitem__") else _si_row[0]
                    if _t not in _shipped_titles:
                        _shipped_titles.append(_t)
            async with db.execute(
                "SELECT DISTINCT si.title FROM sprint_items si "
                "JOIN task_log tl ON tl.sprint_item_id = si.id "
                "WHERE tl.session_id = ? AND tl.status = 'done' AND si.status = 'done'",
                (session_id,),
            ) as _si_cur2:
                for _si_row2 in await _si_cur2.fetchall():
                    _t2 = _si_row2["title"] if hasattr(_si_row2, "__getitem__") else _si_row2[0]
                    if _t2 not in _shipped_titles:
                        _shipped_titles.append(_t2)
            _shipped_str = ", ".join(_shipped_titles) if _shipped_titles else "none"
            _plain_summary = (
                f"Shipped: {_shipped_str}. "
                f"Tasks done: {_items_done}. "
                f"Deploy: no."
            )
            await db.execute(
                "UPDATE sessions SET session_summary = ? WHERE id = ?",
                (_plain_summary, session_id),
            )
        except Exception:
            pass  # non-fatal
        return {
            "summary": content,
            "pending_count": len(pending_items),
            "pending_ids": ids_str,
            "next_goal": next_goal,
            "start_fresh": start_fresh,
        }
    if name == "request_hitl":
        validate_input_size(args.get("question"), "question", 10_000)
        validate_input_size(args.get("context"), "context", 50_000)
        _hitl_kind = args.get("kind", "question")
        if _hitl_kind not in ("question", "correction"):
            _hitl_kind = "question"
        result = await db_module.request_hitl(
            db, args["project_id"], args["question"],
            session_id=args.get("session_id"),
            context=args.get("context"),
            urgency=args.get("urgency", "normal"),
            assigned_to=args.get("assigned_to"),
            kind=_hitl_kind,
        )
        # v3.4 — auto-answered requests need no human; skip the notification.
        if result.get("answered_by") != "auto":
            # Notify via configured notify_url — best-effort, non-blocking
            _hitl_urgency = args.get("urgency", "normal").upper()
            _hitl_q = args["question"][:200]
            _hitl_base = os.environ.get("MERIDIAN_BASE_URL", "https://usemeridian.us").rstrip("/")
            await _maybe_notify(
                db, args["project_id"],
                f"Action needed ({_hitl_urgency})",
                f"{_hitl_q}\n\nAnswer at: {_hitl_base}/dashboard",
                event="hitl",
                tenant=tenant,
                pref_key="hitl",
            )
        return result
    if name == "get_hitl_request":
        result = await db_module.get_hitl_request(db, args["request_id"])
        if result is None:
            raise ValueError("hitl request not found")
        return result
    if name == "add_note":
        validate_input_size(args.get("title"), "note title", 500)
        validate_input_size(args.get("body"), "note body", 10_000_000)
        result = await db_module.add_project_note(
            db, args["project_id"], args["title"], args["body"],
            args.get("tags"),
        )
        await _append_note_to_roadmap(
            args["title"], args["body"], args.get("tags"), args.get("category"),
        )
        return result
    if name == "get_notes":
        return await db_module.get_project_notes(
            db, args["project_id"], tag=args.get("tag"),
        )
    if name == "delete_note":
        ok = await db_module.delete_project_note(db, args["note_id"])
        return {"deleted": ok}
    if name == "add_workspace_note":
        validate_input_size(args.get("title"), "note title", 500)
        validate_input_size(args.get("body"), "note body", 10_000_000)
        return await db_module.add_workspace_note(
            db, args["title"], args["body"], args.get("tags"),
            tenant_id=_mcp_tenant_id,
        )
    if name == "get_workspace_notes":
        return await db_module.get_workspace_notes(
            db, tag=args.get("tag"), tenant_id=_mcp_tenant_id,
        )
    if name == "pin_workspace_decision":
        validate_input_size(args.get("title"), "decision title", 500)
        validate_input_size(args.get("body"), "decision body", 100_000)
        return await db_module.pin_workspace_decision(
            db, args["title"], args["body"],
            category=args.get("category", "TECHNICAL"),
            tenant_id=_mcp_tenant_id,
        )
    if name == "get_workspace_decisions":
        return await db_module.get_workspace_decisions(
            db, include_superseded=args.get("include_superseded", False),
            tenant_id=_mcp_tenant_id,
        )
    if name == "get_workspace_settings":
        return await db_module.get_workspace_settings(db, tenant_id=_mcp_tenant_id)
    if name == "update_workspace_settings":
        return await db_module.update_workspace_settings(
            db,
            hitl_auto_answer_default=args.get("hitl_auto_answer_default"),
            sprint_name_default=args.get("sprint_name_default"),
            handoff_template=args.get("handoff_template"),
            tenant_id=_mcp_tenant_id,
        )
    if name == "get_context_block":
        # v2.3 — assemble the same shape as /projects/{id}/context-block but
        # return both the rendered text AND the source dict so MCP clients
        # can choose to render their own variant.
        # v2.5 — wrap in semantic XML for better Claude Code parsing.
        project_id = args["project_id"]
        mode = args.get("mode", "full")
        project = await db_module.get_project(db, project_id)
        if project is None:
            raise ValueError("project not found")
        goal = await db_module.get_goal(db, project_id)
        sprint_items = await db_module.get_sprint_items(
            db, project_id, status="pending"
        )
        all_tasks = await db_module.get_tasks(db, project_id, limit=20)
        pending_tasks = [
            t for t in all_tasks if t.get("status") in ("pending", "in_progress", "done")
        ][:10]
        sessions = await db_module.get_sessions(db, project_id, active_only=True)
        decisions_raw = (project.get("decisions") or "").strip()
        recent_decisions = [
            l.strip() for l in decisions_raw.splitlines() if l.strip()
        ][-5:]
        text = _render_context_block(
            project, goal, sprint_items, pending_tasks, sessions, recent_decisions,
            mode=mode,
        )
        # v3.1 — workspace decisions + notes apply across all projects; surface
        # them at the very top so a fresh session sees org-wide truth first.
        ws_decisions = await db_module.get_workspace_decisions(db, tenant_id=_mcp_tenant_id)
        ws_notes = await db_module.get_workspace_notes(db, tenant_id=_mcp_tenant_id)
        ws_block = _render_workspace_block(ws_decisions, ws_notes)
        if ws_block:
            text = f"{ws_block}\n\n{text}"
        xml_text = f'<meridian_context project_id="{project_id}" mode="{mode}">\n{text}\n</meridian_context>'
        return {"mode": mode, "text": xml_text, "project_id": project_id}
    if name == "list_hitl_requests":
        # dcf1e428 — default 'recent' (pending + answered last 24h) so dismissed HITLs
        # don't give false "no pending HITLs" confidence to planning sessions.
        status_filter = args.get("status", "recent")
        if status_filter == "all":
            status_filter = None
        # project_id is optional — None lists across all projects (like the
        # dashboard), so cross-project HITLs aren't missed (277567dc).
        return await db_module.list_hitl_requests(
            db, args.get("project_id"),
            status=status_filter,
            limit=args.get("limit", 50),
        )
    if name == "answer_hitl":
        result = await _answer_hitl_and_apply(
            db, args["request_id"], args["answer"],
            answered_by=args.get("answered_by"), approved=True,
        )
        if result is None:
            raise ValueError("hitl request not found")
        return result
    if name == "dismiss_hitl":
        result = await db_module.dismiss_hitl_request(db, args["request_id"])
        if result is not None:
            await _on_hitl_answered(db, result, approved=False)
        if result is None:
            raise ValueError("hitl request not found")
        return result
    if name == "update_md_section":
        md_file = args["file"]
        anchor = args["anchor"]
        content = args["content"]
        # Raises ValueError for non-replace anchors / unknown files / README.
        md_anchors_module.assert_replace_target(md_file, anchor)
        # v1.1 — force=true: human planning sessions (claude.ai) skip the HITL
        # round-trip and apply the section replacement directly. Executor sessions
        # omit force (default False) so the diff stays human-gated as before.
        if args.get("force") in (True, 1, "true", "1", "yes"):
            try:
                path = await md_anchors_module.apply_replace(md_file, anchor, content)
            except md_anchors_module.AnchorError as exc:
                return {"applied": False, "apply_error": str(exc)}
            except Exception as exc:  # noqa: BLE001 — never crash the tool call
                return {"applied": False, "apply_error": f"write failed: {exc}"}
            if path is None:
                return {"applied": False, "reason": "no-op-or-hosted"}
            return {"applied": True, "forced": True, "file": md_file, "anchor": anchor}
        diff = md_anchors_module.build_diff(md_file, anchor, content)
        payload = json.dumps({
            "file": md_file,
            "anchor": anchor,
            "content": content,
            "base_hash": md_anchors_module.anchor_content_hash(md_file, anchor),
            "diff": diff,
        })
        return await db_module.request_hitl(
            db, args["project_id"],
            question=f"Approve update to {md_file} § {anchor}?",
            session_id=args.get("session_id"),
            context=(
                f"Proposed section replacement for {md_file} (anchor: {anchor}). "
                "Review the diff in the dashboard, then Approve or Reject."
            ),
            urgency=args.get("urgency", "normal"),
            kind="md_section_update",
            payload=payload,
        )
    if name == "list_sessions":
        active_only = args.get("status", "active") != "all"
        return await db_module.get_sessions(db, args["project_id"], active_only=active_only)
    if name == "add_sprint_note":
        validate_input_size(args.get("title"), "note title", 500)
        validate_input_size(args.get("body"), "note body", 10_000_000)
        return await db_module.add_session_note(
            db, args["session_id"], args["title"], args["body"]
        )
    if name == "get_sprint_notes":
        return await db_module.get_session_notes(db, args["session_id"])
    if name == "add_sprint_item":
        validate_input_size(args.get("title"), "sprint item title", 500)
        # 10c0f6a0 — fuzzy duplicate guard: warn if title closely matches existing item
        _new_words = set(args["title"].lower().split())
        _dup_warnings: list[dict[str, Any]] = []
        if len(_new_words) >= 3:
            _all_items = await db_module.get_sprint_items(db, args["project_id"])
            for _ex in _all_items:
                if _ex.get("status") in {"pending", "todo", "in_progress", "done"}:
                    _ex_words = set(_ex["title"].lower().split())
                    if _ex_words:
                        _overlap = len(_new_words & _ex_words) / len(_new_words)
                        if _overlap >= 0.6:
                            _dup_warnings.append({
                                "item_id": _ex["id"], "title": _ex["title"][:120],
                                "status": _ex["status"], "match_pct": round(_overlap * 100),
                            })
        # fd86aacc — warn if active executor sessions exist when adding a new item
        _active_session_warnings: list[str] = []
        try:
            from datetime import datetime, timezone as _tz
            _active_sessions = await db_module.get_sessions(db, args["project_id"])
            _now_ts = datetime.now(_tz.utc)
            for _sess in _active_sessions:
                _ls = _sess.get("last_seen")
                if _ls:
                    try:
                        _ls_dt = datetime.fromisoformat(str(_ls).replace("Z", "+00:00"))
                        if _ls_dt.tzinfo is None:
                            _ls_dt = _ls_dt.replace(tzinfo=_tz.utc)
                        if (_now_ts - _ls_dt).total_seconds() < 600:
                            _active_session_warnings.append(
                                f"session '{_sess.get('name', _sess.get('id','?'))}' is active"
                            )
                    except Exception:
                        pass
        except Exception:
            pass
        _new_item = await db_module.add_sprint_item(
            db, args["project_id"], args["version"], args["title"],
            group=args.get("group"),
            human_id=args.get("human_id"),
            depends_on=args.get("depends_on"),
            failure_mode=args.get("failure_mode"),
            milestone_type=args.get("milestone_type", "task"),
        )
        _extra: dict[str, Any] = {}
        if _dup_warnings:
            _extra["duplicate_warnings"] = _dup_warnings
        if _active_session_warnings:
            _extra["active_session_warning"] = (
                "WARNING: " + "; ".join(_active_session_warnings)
                + " — new item added but may not be picked up until next session start."
            )
        if _extra:
            _new_item = {**_new_item, **_extra}
        return _new_item
    if name == "update_sprint_item":
        validate_input_size(args.get("title"), "sprint item title", 500)
        validate_input_size(args.get("notes"), "sprint item notes", 50_000)
        item = await db_module.patch_sprint_item(
            db, args["project_id"], args["item_id"],
            title=args.get("title"),
            version=args.get("version"),
            notes=args.get("notes"),
            human_id=args.get("human_id"),
            item_group=args.get("group"),
        )
        return item or {"error": "sprint item not found"}
    if name == "set_sprint":
        # 62d321dd — guard: warn if pending items were never started before rolling sprint
        if not args.get("force"):
            _unstarted = [
                it for it in await db_module.get_sprint_items(db, args["project_id"], status="pending")
                if it.get("claimed_at") is None
            ]
            if _unstarted:
                _list = "\n".join(
                    f'  [{it["id"][:8]}] {it.get("title","")[:100]}'
                    for it in _unstarted[:10]
                )
                return {
                    "warning": (
                        f"WARNING: {len(_unstarted)} item(s) from the current sprint were never "
                        f"started:\n{_list}\n"
                        "Proceeding will leave these orphaned. Move them to the new sprint version "
                        "or push to backlog first. Call set_sprint again with force=true to override."
                    ),
                    "unstarted_count": len(_unstarted),
                    "unstarted_ids": [it["id"] for it in _unstarted],
                    "sprint_not_updated": True,
                }
        result = await db_module.set_sprint(db, args["project_id"], args["sprint"])
        await goal_md_module.sync_db_to_goal_md(db, args["project_id"])
        return result
    if name == "get_sprint_progress":
        # 0507f4a1 — sprint progress summary
        _version_filter = args.get("version")
        _group_filter = args.get("item_group")
        _all = await db_module.get_sprint_items(db, args["project_id"])
        if _version_filter:
            _all = [it for it in _all if it.get("version") == _version_filter]
        if _group_filter:
            _all = [it for it in _all if it.get("item_group") == _group_filter]
        _counts: dict[str, int] = {}
        for _it in _all:
            _st = _it.get("status") or "pending"
            _counts[_st] = _counts.get(_st, 0) + 1
        _done_n = _counts.get("done", 0)
        _total = len(_all)
        _pct = round(100 * _done_n / _total) if _total else 0
        return {
            "total": _total,
            "done": _done_n,
            "in_progress": _counts.get("in_progress", 0),
            "pending": _counts.get("pending", 0),
            "failed": _counts.get("failed", 0),
            "skipped": _counts.get("skipped", 0),
            "percent_complete": _pct,
            "by_status": _counts,
            "items": [
                {"id": it["id"], "title": (it.get("title") or "")[:80], "status": it.get("status")}
                for it in _all
            ],
        }
    if name == "get_sprint_items":
        include_human = args.get("human", True)
        if isinstance(include_human, bool):
            pass
        else:
            include_human = str(include_human).lower() not in ("false", "0", "no")
        _items = await db_module.get_sprint_items(
            db, args["project_id"],
            status=args.get("status"),
            include_human=include_human,
        )
        # 10c0f6a0 — stale-session warning: in_progress items claimed >2h ago
        from datetime import datetime as _dt_cls
        _now_utc = _dt_cls.utcnow()
        for _i, _it in enumerate(_items):
            if _it.get("status") == "in_progress" and _it.get("claimed_at"):
                try:
                    _ca = _dt_cls.fromisoformat(_it["claimed_at"].split(".")[0].replace("Z", ""))
                    _age_h = (_now_utc - _ca).total_seconds() / 3600
                    if _age_h > 2:
                        _items[_i] = {**_it, "stale_warning": True, "stale_age_hours": round(_age_h, 1)}
                except Exception:  # noqa: BLE001
                    pass
        return _items
    if name == "claim_sprint_item":
        # ITEM 3 — protect installer scripts: refuse to claim a sprint item whose
        # touches_files includes hooks.ps1 / hooks.sh unless force=true is passed.
        _force = args.get("force") in (True, 1, "true", "1", "yes")
        if not _force:
            _pitem = await db_module.get_sprint_item(db, args["item_id"])
            if _pitem is not None:
                _touched = [p.lower() for p in _parse_touches_files(_pitem.get("touches_files"))]
                _hits = sorted({fn for fn in ("hooks.ps1", "hooks.sh")
                                if any(t == fn or t.endswith("/" + fn) for t in _touched)})
                if _hits:
                    return {
                        "error": "PROTECTED",
                        "message": ("Sprint item touches protected installer scripts "
                                    f"({', '.join(_hits)}). Pass force=true to override."),
                        "protected_files": _hits,
                    }

        # 0716c9e0 — parallel safety: load project settings once for both
        # auto_worktrees (suggest worktree by default) and isolation=worktree.
        _suggest_worktree = False
        _exec_cfg: dict[str, Any] = {}
        _proj_settings_claim: dict[str, Any] = {}
        try:
            _ps = await db_module.get_project_settings(db, args["project_id"])
            _proj_settings_claim = _ps or {}
            _raw_cfg = _proj_settings_claim.get("executor_config")
            if _raw_cfg:
                _exec_cfg = json.loads(_raw_cfg) if isinstance(_raw_cfg, str) else (_raw_cfg or {})
        except Exception:  # noqa: BLE001
            pass
        _isolation = (_exec_cfg or {}).get("isolation", "")
        _aw_raw = _proj_settings_claim.get("auto_worktrees")
        _auto_worktrees = bool(int(_aw_raw) if _aw_raw is not None else 1)
        if _isolation == "worktree" or _auto_worktrees:
            _suggest_worktree = True
        else:
            conflicts = await _sprint_item_file_claim_conflicts(
                db,
                args["project_id"],
                args["item_id"],
                exclude_session_id=args.get("session_id"),
            )
            if conflicts:
                return {
                    "error": "CONFLICT",
                    "message": "Cannot claim sprint item: active session has overlapping claimed files.",
                    "conflicts": conflicts,
                }

        try:
            item = await db_module.claim_sprint_item(db, args["project_id"], args["item_id"])
        except ValueError:
            # 10c0f6a0 — if already in_progress, check for stale claim and surface info
            _stale_item = await db_module.get_sprint_item(db, args["item_id"])
            if _stale_item and _stale_item.get("status") == "in_progress" and _stale_item.get("claimed_at"):
                from datetime import datetime as _dt_cls
                try:
                    _ca = _dt_cls.fromisoformat(_stale_item["claimed_at"].split(".")[0].replace("Z", ""))
                    _age_h = (_dt_cls.utcnow() - _ca).total_seconds() / 3600
                    if _age_h > 2:
                        return {
                            "error": "STALE_CLAIM",
                            "message": (
                                f"Item is in_progress but claimed {round(_age_h, 1)}h ago with no recent "
                                "activity — the claiming session may have ended. Safe to force-reclaim "
                                "by updating status to 'pending' first via update_sprint_item."
                            ),
                            "stale_age_hours": round(_age_h, 1),
                            "claimed_at": _stale_item["claimed_at"],
                            "item": _stale_item,
                        }
                except Exception:  # noqa: BLE001
                    pass
            raise
        if item is None:
            raise ValueError("sprint item not found")

        if _suggest_worktree:
            item_id_short = item["id"][:8]
            _session_id_claim = args.get("session_id") or ""
            if _auto_worktrees and _isolation != "worktree" and _session_id_claim:
                # Default path: .claude/worktrees/{session_id_short} (gitignored)
                wt_branch = f"worktree/{item_id_short}"
                wt_path = f".claude/worktrees/{_session_id_claim[:8]}"
            else:
                # Legacy isolation=worktree path: repo-relative sibling dir
                repo_path = ""
                _repo_paths = _exec_cfg.get("repo_paths")
                if _repo_paths and isinstance(_repo_paths, list) and _repo_paths:
                    _first = _repo_paths[0]
                    repo_path = (_first.get("cwd") or "") if isinstance(_first, dict) else str(_first)
                if not repo_path:
                    repo_path = _exec_cfg.get("repo_path") or ""
                repo_name = os.path.basename(repo_path.rstrip("/\\")) if repo_path else "repo"
                wt_branch = f"worktree/{item_id_short}"
                wt_path = f"../{repo_name}-worktree-{item_id_short}"
            item = dict(item)
            item.update({
                "worktree_suggested": True,
                "worktree_branch": wt_branch,
                "worktree_path": wt_path,
                "worktree_setup_cmd": f"git worktree add {wt_path} -b {wt_branch}",
                "worktree_cleanup_cmd": f"git worktree remove {wt_path} --force",
                "worktree_merge_cmd": (
                    f"git checkout dev && git merge {wt_branch} --no-edit "
                    f"&& git branch -d {wt_branch}"
                ),
            })

        return item
    if name == "add_subtask":
        return await db_module.add_subtask(
            db, args["project_id"], args["parent_id"], args["title"]
        )
    if name == "split_sprint_item":
        return await db_module.split_sprint_item(
            db, args["project_id"], args["item_id"], args["titles"]
        )
    if name == "merge_sprint_items":
        return await db_module.merge_sprint_items(
            db, args["project_id"], args["item_ids"], args["new_title"]
        )
    if name == "complete_sprint_item":
        # 0716c9e0 — check active worktree before marking done.
        _complete_session_id = args.get("session_id") or ""
        _merge_warning: dict[str, Any] | None = None
        if _complete_session_id:
            try:
                _ps_complete = await db_module.get_project_settings(db, args["project_id"])
                _req_merge = bool(int((_ps_complete or {}).get("require_merge_approval") or 1))
                if _req_merge:
                    _wt = await db_module.get_active_worktree_for_session(db, _complete_session_id)
                    if _wt:
                        _hitl = await db_module.request_hitl(
                            db, args["project_id"],
                            f"Session has active worktree on branch '{_wt['branch']}' "
                            f"at '{_wt['path']}'. Merge to main before closing. "
                            f"Run: git checkout dev && git merge {_wt['branch']} --no-edit",
                            session_id=_complete_session_id,
                            urgency="normal", kind="correction",
                        )
                        _merge_warning = {
                            "worktree_branch": _wt["branch"],
                            "worktree_path": _wt["path"],
                            "hitl_id": (_hitl or {}).get("id"),
                            "message": "Merge reminder filed — see HITL queue.",
                        }
            except Exception:  # noqa: BLE001
                pass

        item = await db_module.complete_sprint_item(
            db, args["project_id"], args["item_id"],
            task_id=args.get("task_id"),
        )
        if item is None:
            raise ValueError("sprint item not found")
        if _merge_warning:
            item = dict(item)
            item["merge_warning"] = _merge_warning
        # Notify only when the sprint is fully complete.
        active_statuses = {"pending", "todo", "in_progress"}
        remaining_items = await db_module.get_sprint_items(db, args["project_id"])
        if not any((it.get("status") or "") in active_statuses for it in remaining_items):
            await _maybe_notify(
                db, args["project_id"],
                "Sprint done ✓",
                "All sprint items are complete.",
                event="sprint_done",
                tenant=tenant,
                pref_key="sprint",
            )
        return item
    if name == "get_session_log":
        run = await db_module.get_executor_run_by_session(db, args.get("session_id", ""))
        if run is None:
            return {"error": "no run found for session"}
        return {
            "run_id": run["id"],
            "session_id": run["session_id"],
            "started_at": run["started_at"],
            "ended_at": run.get("ended_at"),
            "status": run["status"],
            "task_count": run["task_count"],
            "transcript": run["transcript"],
        }
    if name == "get_agent_instructions":
        instructions = await db_module.get_agent_instructions(db, args["project_id"])
        return {"project_id": args["project_id"], "agent_instructions": instructions}
    if name == "set_agent_instructions":
        validate_input_size(args.get("instructions"), "agent_instructions", 100_000)
        instructions = (args.get("instructions") or "").strip() or None
        return await db_module.set_agent_instructions(db, args["project_id"], instructions)
    if name == "set_executor_config":
        cfg_fields = {
            k: args[k]
            for k in ("repo_path", "env_file", "test_cmd", "test_min",
                      "deploy_cmd", "shell_type", "branch")
            if k in args
        }
        return await db_module.set_executor_config(db, args["project_id"], cfg_fields)
    if name == "claim_file":
        return await db_module.claim_file(db, args["file_path"], args["session_id"])
    if name == "release_file":
        released = await db_module.release_file(db, args["file_path"], args["session_id"])
        return {"released": released, "file_path": args["file_path"]}
    if name == "idle_until_session_done":
        return await _idle_until_session_done(db, args["watching_session_id"])
    if name == "search_all":
        return await db_module.search_all(
            db, args["project_id"], args["query"],
            limit=args.get("limit", 10),
        )
    if name == "get_session_brief":
        # v2.5 — single-call orientation, <500 tokens, XML output.
        project_id = args["project_id"]
        role = args.get("role", "worker")
        session_id_for_notes = args.get("session_id")
        goal = await db_module.get_goal(db, project_id)
        tasks = await db_module.get_tasks(db, project_id, limit=5)
        # dcf1e428 — use 'recent' to surface pending + answered last 24h for planning sessions
        hitl_rows = await db_module.list_hitl_requests(db, project_id, status="recent")
        sprint_items = await db_module.get_sprint_items(db, project_id, status="pending")
        # 0507f4a1 — sprint progress summary for session brief
        _all_items_for_progress = await db_module.get_sprint_items(db, project_id)
        _done_count = sum(1 for it in _all_items_for_progress if it.get("status") == "done")
        _total_count = len(_all_items_for_progress)
        _pct = round(100 * _done_count / _total_count) if _total_count else 0
        blocking = [t for t in tasks if t.get("status") == "failed"]
        sprint_str = (goal.get("sprint") or "") if goal else ""
        tasks_xml = "\n".join(
            f'  <task status="{t.get("status","?")}">{(t.get("description") or "")[:80]}</task>'
            for t in tasks
        )
        sprint_items_xml = "\n".join(
            f'  <item version="{it.get("version","")}">{(it.get("title") or "")[:80]}</item>'
            for it in sprint_items[:5]
        )
        # 277567dc — surface pending HITLs so session sees what needs a human decision.
        # dcf1e428 — also surface recently answered HITLs so planning sessions can see what was decided.
        _pending_hitls = [h for h in hitl_rows if h.get("status") == "pending"]
        _answered_hitls = [h for h in hitl_rows if h.get("status") != "pending"]
        if _pending_hitls:
            hitl_xml = (
                f'<hitl_pending count="{len(_pending_hitls)}">\n'
                + "\n".join(
                    f'  <request id="{h.get("id","")}" urgency="{h.get("urgency","normal")}">'
                    f'{(h.get("question") or "")[:140]}</request>'
                    for h in _pending_hitls[:5]
                )
                + "\n</hitl_pending>"
            )
        else:
            hitl_xml = ""
        if _answered_hitls:
            hitl_xml += (
                f'\n<hitl_recent count="{len(_answered_hitls)}">\n'
                + "\n".join(
                    f'  <request status="{h.get("status","?")}">'
                    f'Q: {(h.get("question") or "")[:80]} '
                    f'A: {(h.get("answer") or "")[:80]}</request>'
                    for h in _answered_hitls[:3]
                )
                + "\n</hitl_recent>"
            )
        blocking_xml = f'<blocking>{(blocking[0].get("description") or "")[:100]}</blocking>' if blocking else ""
        # v2.6 — include session scratch-pad notes at top of brief
        notes_xml = ""
        new_items_xml = ""
        if session_id_for_notes:
            try:
                session_notes = await db_module.get_session_notes(db, session_id_for_notes)
                if session_notes:
                    notes_xml = "<session_notes>\n" + "\n".join(
                        f'  <note title="{n.get("title","")}">{(n.get("body") or "")[:120]}</note>'
                        for n in session_notes
                    ) + "\n</session_notes>\n"
            except Exception:
                pass
            # fd86aacc — show items added to the board since this session started
            try:
                _all_sess = await db_module.get_sessions(db, project_id, active_only=False)
                _curr_sess = next(
                    (s for s in _all_sess if s.get("id") == session_id_for_notes), None
                )
                if _curr_sess and _curr_sess.get("created_at"):
                    _sess_started = str(_curr_sess["created_at"])
                    _all_items = await db_module.get_sprint_items(db, project_id)
                    _new_count = sum(
                        1 for it in _all_items
                        if (it.get("added_at") or "") >= _sess_started
                    )
                    if _new_count > 0:
                        new_items_xml = (
                            f'<board_change>{_new_count} item{"s" if _new_count != 1 else ""}'
                            f' added since this session started</board_change>\n'
                        )
            except Exception:
                pass
        _progress_xml = (
            f'<progress done="{_done_count}" total="{_total_count}" pct="{_pct}%"/>\n'
            if _total_count else ""
        )
        brief = (
            f'<session_brief project_id="{project_id}" role="{role}">\n'
            f'{notes_xml}'
            f'{new_items_xml}'
            f'{_progress_xml}'
            f'<sprint>{sprint_str[:200]}</sprint>\n'
            f'<pending_items>\n{sprint_items_xml}\n</pending_items>\n'
            f'<last_tasks>\n{tasks_xml}\n</last_tasks>\n'
            f'{blocking_xml}\n'
            f'{hitl_xml}\n'
            f'</session_brief>'
        )
        return {"text": brief, "project_id": project_id, "role": role}
    raise ValueError(f"unknown tool: {name}")

# ---------------------------------------------------------------------------
# MCP server — implementation lives in meridian/mcp/stdio_handler.py
# ---------------------------------------------------------------------------

from .mcp.stdio_handler import build_mcp_server  # noqa: F401
