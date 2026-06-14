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

app.include_router(_notes_router)
app.include_router(_hitl_router)
app.include_router(_sprint_router)
app.include_router(_sessions_router)
app.include_router(_tasks_router)
app.include_router(_decisions_router)
app.include_router(_handoff_router)
app.include_router(_admin_router)
app.include_router(_workspace_router)

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
            if _oa_tokens.get(oauth_hash) or await _get_oauth_token_from_db(auth_db, oauth_hash):
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

# ---------------------------------------------------------------------------
# v2.0 — Rate limiter (slowapi, in-memory, no Redis required)
# ---------------------------------------------------------------------------
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address

    _limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = _limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    _RATE_LIMIT = "100/minute"
except ImportError:
    _limiter = None  # type: ignore[assignment]
    _RATE_LIMIT = "100/minute"


def _rate_limit(rate: str):
    """Return a decorator that applies *rate* via slowapi, or a no-op when slowapi is absent."""
    def decorator(func):
        if _limiter is None:
            return func
        return _limiter.limit(rate)(func)
    return decorator


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


# ---------------------------------------------------------------------------
# v2.0 — Google OAuth routes
# ---------------------------------------------------------------------------

@app.get("/auth/login")
async def auth_login(request: Request):
    """Serve sign-in page with Google and GitHub OAuth buttons."""
    from .hosted import auth_login as _auth_login
    return await _auth_login(request)


@app.get("/auth/google/login")
async def auth_google_login(request: Request):
    """Redirect browser directly to Google OAuth consent page."""
    from .hosted import auth_google_login as _auth_google_login
    return await _auth_google_login(request)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    """Handle Google OAuth callback — create/update tenant, set session cookie."""
    from .hosted import auth_callback as _auth_callback
    return await _auth_callback(request)


@app.get("/auth/github/login")
async def auth_github_login(request: Request):
    """Redirect browser to GitHub OAuth consent page."""
    from .hosted import auth_github_login as _auth_github_login
    return await _auth_github_login(request)


@app.get("/auth/github/callback")
async def auth_github_callback(request: Request):
    """Handle GitHub OAuth callback — create/update tenant, set session cookie."""
    from .hosted import auth_github_callback as _auth_github_callback
    return await _auth_github_callback(request)


@app.get("/auth/github/repo-connect")
async def auth_github_repo_connect(request: Request):
    """Redirect browser to GitHub OAuth for repo connection."""
    from .hosted import auth_github_repo_connect as _auth_github_repo_connect
    return await _auth_github_repo_connect(request)


@app.get("/auth/github/repo-callback")
async def auth_github_repo_callback(request: Request):
    """Handle GitHub repo-connect callback and store repo access."""
    from .hosted import auth_github_repo_callback as _auth_github_repo_callback
    db = await _db(request)
    return await _auth_github_repo_callback(request, db)


@app.get("/auth/microsoft/login")
async def auth_microsoft_login(request: Request):
    """Redirect browser to Microsoft OAuth consent page."""
    from .hosted import auth_microsoft_login as _auth_microsoft_login
    return await _auth_microsoft_login(request)


@app.get("/auth/microsoft/callback")
async def auth_microsoft_callback(request: Request):
    """Handle Microsoft OAuth callback — create/update tenant, set session cookie."""
    from .hosted import auth_microsoft_callback as _auth_microsoft_callback
    return await _auth_microsoft_callback(request)


@app.get("/auth/email-required")
async def auth_email_required(request: Request) -> HTMLResponse:
    """Shown when OAuth provider returned no usable email (e.g. GitHub with private email)."""
    provider = request.query_params.get("provider", "your provider")
    html = f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<title>Email required — Meridian</title>
<style>body{{font-family:system-ui,sans-serif;max-width:480px;margin:80px auto;padding:0 20px;color:#e8eaed;background:#0d1117}}
h2{{color:#58a6ff}}p{{color:#8b949e;line-height:1.6}}a{{color:#58a6ff}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:24px;margin-top:24px}}
</style></head><body>
<div class="card">
<h2>Email address required</h2>
<p>We couldn't get a verified email from {provider}. Meridian needs your email to create your account.</p>
<p>To fix this:<br>
&nbsp;&nbsp;1. Go to <a href="https://github.com/settings/emails" target="_blank" rel="noopener">github.com/settings/emails</a><br>
&nbsp;&nbsp;2. Add and verify a primary email address<br>
&nbsp;&nbsp;3. <a href="/auth/github/login">Try signing in again</a></p>
<p>Or <a href="/auth/login">use a magic link</a> to sign in with your email directly.</p>
</div>
</body></html>"""
    return HTMLResponse(html)


@app.get("/auth/logout")
async def auth_logout(request: Request):
    """Clear session cookie and delete DB session."""
    from .hosted import auth_logout as _auth_logout
    return await _auth_logout(request)


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


@app.get("/auth/magic/verify")
async def auth_magic_verify(request: Request, token: str = ""):
    """v0.9 — consume a magic-link token, create a session, redirect.

    Single-use: marks ``used_at`` on success so re-clicking the same
    link doesn't re-authenticate. New tenants flow through the OAuth
    paywall check — redirected to /pricing?signup=1 if no Stripe
    subscription yet.
    """
    from .hosted import auth_magic_verify as _impl
    return await _impl(request, token)


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


@app.get("/projects", response_model=list[Project])
async def list_projects(request: Request) -> list[dict[str, Any]]:
    """List every project."""
    return await db_module.list_projects(await _db(request))


@app.post("/projects", response_model=Project, status_code=201)
async def create_project(
    body: ProjectCreate, request: Request
) -> dict[str, Any]:
    """Create a new project. 409 if the name is already in use."""
    existing = await db_module.get_project_by_name(await _db(request), body.name)
    if existing is not None:
        raise HTTPException(
            status_code=409, detail=f"project '{body.name}' already exists"
        )
    tenant = await _get_tenant_from_request(request)
    if tenant and tenant.get("plan") == "free":
        existing_projects = await db_module.list_projects(await _db(request))
        if len(existing_projects) >= 1:
            raise HTTPException(
                status_code=403,
                detail="Free tier is limited to 1 project. Upgrade to Solo ($20/mo) for unlimited projects.",
            )
    # G4.15 — safety limit: projects per tenant
    from . import limits as _limits  # noqa: PLC0415
    all_projects = await db_module.list_projects(await _db(request))
    _limits.check_projects_per_tenant(len(all_projects))
    return await db_module.create_project(
        await _db(request), body.name, human_id=body.human_id
    )


@app.get("/setup/needed")
async def setup_needed(request: Request) -> dict[str, Any]:
    """Returns {needed: true} if no projects exist yet (first-run wizard trigger)."""
    projects = await db_module.list_projects(await _db(request))
    return {"needed": len(projects) == 0}


@app.get("/projects/by-name/{name}")
async def get_project_by_name(name: str, request: Request) -> dict[str, Any]:
    """Look up a project by name (case-insensitive substring match).

    Returns the project row plus a brief goal summary so a cold session
    can confirm it found the right project without a second round-trip.
    """
    db = await _db(request)
    # Exact match first (most common case).
    project = await db_module.get_project_by_name(db, name)
    if project is None:
        # Case-insensitive substring fallback.
        all_projects = await db_module.list_projects(db)
        lower = name.lower()
        matches = [p for p in all_projects if lower in p["name"].lower()]
        if matches:
            project = matches[0]
    if project is None:
        raise HTTPException(
            status_code=404, detail=f"no project found matching '{name}'"
        )
    goal = await db_module.get_goal(db, project["id"])
    return {
        "project": project,
        "goal_version": goal["version"] if goal else None,
        "goal_summary": (str(goal["content"])[:200] if goal else None),
    }


@app.get("/projects/{project_id}", response_model=Project)
async def get_project(project_id: str, request: Request) -> dict[str, Any]:
    """Look up a project by id."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@app.get("/projects/{project_id}/settings", response_model=ProjectSettings)
async def get_project_settings(project_id: str, request: Request) -> dict[str, Any]:
    """Return persisted per-project dashboard settings."""
    settings = await db_module.get_project_settings(await _db(request), project_id)
    if settings is None:
        raise HTTPException(status_code=404, detail="project not found")
    return settings


@app.patch("/projects/{project_id}/settings", response_model=ProjectSettings)
async def patch_project_settings(
    project_id: str, body: ProjectSettingsPatch, request: Request
) -> dict[str, Any]:
    """Update persisted per-project dashboard settings."""
    executor_config_dict = (
        normalize_executor_config(body.executor_config.model_dump(exclude_none=True))
        if body.executor_config is not None
        else None
    )
    settings = await db_module.update_project_settings(
        await _db(request),
        project_id,
        max_pinned_decisions=body.max_pinned_decisions,
        executor_config=executor_config_dict,
        hitl_auto_answer=body.hitl_auto_answer,
        auto_worktrees=body.auto_worktrees,
        require_merge_approval=body.require_merge_approval,
    )
    if settings is None:
        raise HTTPException(status_code=404, detail="project not found")
    return settings


@app.get("/projects/{project_id}/ntfy")
async def get_project_ntfy(project_id: str, request: Request) -> dict[str, Any]:
    """Return the ntfy push URL configured for this project."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    url = await db_module.get_project_ntfy_url(await _db(request), project_id)
    return {"ntfy_url": url or ""}


def _canonicalize_notify_target(raw: str | None) -> str | None:
    """G1.7 — normalize the notify target so ntfy entries are stored as the
    topic path segment only, while emails and webhooks pass through.

    Examples:
      "https://ntfy.sh/foo"   -> "foo"
      "https://ntfy.sh/foo/"  -> "foo"
      "ntfy.sh/foo"           -> "foo"
      "foo"                   -> "foo"
      "you@example.com"       -> "you@example.com"
      "https://hooks.slack.com/services/abc" -> "https://hooks.slack.com/services/abc"
      ""                      -> None
    """
    if not raw:
        return None
    val = str(raw).strip()
    if not val:
        return None
    # Email → pass through.
    if "@" in val and "://" not in val:
        return val
    lower = val.lower()
    for prefix in ("https://ntfy.sh/", "http://ntfy.sh/", "ntfy.sh/"):
        if lower.startswith(prefix):
            topic = val[len(prefix):].strip().strip("/")
            return topic or None
    # Any other URL with a scheme → webhook, pass through.
    if "://" in val:
        return val
    # Bare token, no slashes → treat as ntfy topic.
    return val.strip("/") or None


async def _ensure_unique_ntfy_topic(
    db: Any, project_id: str, topic: str
) -> str:
    """G1.7 — make sure ``topic`` is not already in use by another project in
    this DB. Suffix with -2, -3, … until free. Returns the topic actually
    used. Pure topic strings only; webhooks/emails skip this check upstream.
    """
    projects = await db_module.list_projects(db)
    in_use = {
        str(p.get("ntfy_url") or "").strip().lower()
        for p in projects
        if p.get("id") != project_id and p.get("ntfy_url")
    }
    base = topic
    candidate = base
    n = 2
    while candidate.lower() in in_use:
        candidate = f"{base}-{n}"
        n += 1
        if n > 999:
            break
    return candidate


@app.get("/projects/{project_id}/ntfy")
async def get_project_ntfy(
    project_id: str, request: Request
) -> dict[str, Any]:
    """Return the current notification settings for this project."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    db = await _db(request)
    notify_url = await db_module.get_project_ntfy_url(db, project_id)
    notify_email = await db_module.get_project_notify_email(db, project_id)
    return {
        "ntfy_url": notify_url or "",
        "notify_url": notify_url or "",
        "notify_email": notify_email or "",
    }


@app.patch("/projects/{project_id}/ntfy")
async def set_project_ntfy(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Save (or clear) the notify URL and/or notify_email for this project.

    Accepts ``notify_url`` (preferred) or ``ntfy_url`` (legacy) key for the
    ntfy/webhook channel, and ``notify_email`` for the email channel.
    ntfy entries are canonicalized to the topic path segment only and
    suffixed with -2/-3/… if another project in this DB already uses
    the same topic. Emails and non-ntfy webhooks pass through verbatim.

    After saving a non-empty notify_url, fires a welcome notification so
    ntfy.sh topics are created on first publish (avoids 404 on first
    real alert).
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    db = await _db(request)
    # Handle ntfy_url / webhook channel
    if "notify_url" in body or "ntfy_url" in body:
        raw_value = str(body.get("notify_url") or body.get("ntfy_url") or "").strip() or None
        notify_url = _canonicalize_notify_target(raw_value)
        if notify_url and "://" not in notify_url and "@" not in notify_url:
            # bare topic → enforce per-DB uniqueness
            notify_url = await _ensure_unique_ntfy_topic(db, project_id, notify_url)
        await db_module.set_project_ntfy_url(db, project_id, notify_url)
        if notify_url:
            # Fire a welcome notification immediately so ntfy.sh creates the topic
            try:
                ntfy_full = notify_url
                if "://" not in ntfy_full and "@" not in ntfy_full:
                    ntfy_full = f"https://ntfy.sh/{ntfy_full}"
                await _dispatch_notification(
                    ntfy_full,
                    "Notifications active",
                    "You will receive alerts here for HITL requests and sprint completions.",
                    event="setup",
                )
            except Exception:  # noqa: BLE001
                pass
    else:
        notify_url = await db_module.get_project_ntfy_url(db, project_id)
    # Handle notify_email channel
    if "notify_email" in body:
        raw_email = str(body.get("notify_email") or "").strip() or None
        await db_module.set_project_notify_email(db, project_id, raw_email)
        notify_email = raw_email
    else:
        notify_email = await db_module.get_project_notify_email(db, project_id)
    return {
        "ntfy_url": notify_url or "",
        "notify_url": notify_url or "",
        "notify_email": notify_email or "",
    }


@app.post("/projects/{project_id}/notify/test")
async def test_project_notification(
    project_id: str, request: Request
) -> dict[str, Any]:
    """Send a test notification to verify the configured notify URL and/or email."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    db = await _db(request)
    notify_url = await db_module.get_project_ntfy_url(db, project_id)
    notify_email = await db_module.get_project_notify_email(db, project_id)
    if not notify_url and not notify_email:
        raise HTTPException(status_code=400, detail="No notify URL or email configured for this project")
    sent_to = []
    if notify_url:
        ntfy_full = notify_url
        if "://" not in ntfy_full and "@" not in ntfy_full:
            ntfy_full = f"https://ntfy.sh/{ntfy_full}"
        await _dispatch_notification(
            ntfy_full,
            "Meridian test notification",
            "Test from the Meridian dashboard. If you see this, notifications are working!",
            event="test",
        )
        sent_to.append(notify_url)
    if notify_email:
        await _send_email_notification(
            notify_email,
            "[Meridian] Test notification",
            "Test from the Meridian dashboard. If you see this, email notifications are working!",
        )
        sent_to.append(notify_email)
    return {"ok": True, "sent_to": sent_to}


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


def _md_ts() -> str:
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _md_one_line(text: str, limit: int = 200) -> str:
    """Collapse a multi-line decision/note body to a single trimmed line."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


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


@app.patch("/projects/{project_id}/icon")
async def set_project_icon(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """G4.17 — set or clear the single-emoji icon for a project.

    Body: ``{"icon": "🎯"}`` or ``{"icon": null}``. Stored as the user-provided
    string capped to a short length (typical emoji is 1-4 codepoints); the
    frontend never expects more than ~8 chars. Wider validation lives in
    the UI picker.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    raw = body.get("icon")
    icon: str | None
    if raw is None:
        icon = None
    else:
        icon = str(raw).strip()[:8] or None
    db = await _db(request)
    await db.execute(
        "UPDATE projects SET icon = ? WHERE id = ?",
        (icon, project_id),
    )
    await db.commit()
    db_module.publish_global(
        {"type": "project_icon_changed", "project_id": project_id, "icon": icon}
    )
    return await db_module.get_project(db, project_id)


@app.post("/projects/{project_id}/rename")
async def rename_project(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """v1.9.x — rename a project.  Broadcasts project_renamed WS event."""
    new_name = str(body.get("name") or "").strip()
    if not new_name:
        raise HTTPException(400, "name is required")
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    existing = await db_module.get_project_by_name(await _db(request), new_name)
    if existing and existing["id"] != project_id:
        raise HTTPException(409, f"project '{new_name}' already exists")
    updated = await db_module.rename_project(await _db(request), project_id, new_name)
    db_module.publish_global(
        {"type": "project_renamed", "project_id": project_id, "name": new_name}
    )
    return updated


@app.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: str, request: Request) -> None:
    """v1.9.x — delete a project and all data.

    Returns 409 if any tasks are in_progress, 404 if the project is unknown.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    try:
        await db_module.delete_project(await _db(request), project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/projects/{project_id}/goal", response_model=GoalState)
async def get_goal(project_id: str, request: Request) -> dict[str, Any]:
    """Read the latest goal state plus ambient task context.

    The response payload (v0.4.2+) includes ``ambient_tasks`` — the
    five most recent task rows, newest first, as ``{status, description,
    created_at}`` dicts. Cold sessions can render the directive *and*
    last activity from a single MCP call.

    G8.34/G9 — Returns 200 with an empty stub when the project exists
    but no goal has been set yet (previously 404). The 404-as-empty
    semantics produced a console error on the dashboard's initial
    render for every fresh project, which made the panel-render
    Playwright test flake by environment. Browsers can't tell the
    difference between "field is empty" and "fetch threw 4xx", so
    the only honest answer is 200 with empty fields. Returns 404 still
    when the project itself does not exist.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    goal = await db_module.get_goal(await _db(request), project_id)
    if goal is None:
        recent = await db_module.get_tasks(await _db(request), project_id, limit=5)
        return {
            "id": "",
            "project_id": project_id,
            "content": "",
            "version": 0,
            "created_at": "",
            "updated_at": "",
            "ambient_tasks": [
                {
                    "status": t["status"],
                    "description": t["description"],
                    "created_at": t["created_at"],
                }
                for t in recent
            ],
            "north_star": None,
            "sprint": None,
        }
    recent = await db_module.get_tasks(await _db(request), project_id, limit=5)
    goal["ambient_tasks"] = [
        {
            "status": t["status"],
            "description": t["description"],
            "created_at": t["created_at"],
        }
        for t in recent
    ]
    # v1.1.3 — per-field ages + coherence warning so the dashboard
    # can paint green / amber / red dots and so cold sessions see
    # which fields have gone stale before doing anything.
    field_ages = await db_module.get_goal_field_ages(
        await _db(request), project_id
    )
    coherence = db_module.compute_coherence_warning(field_ages)
    goal["field_ages"] = field_ages
    goal["coherence_warning"] = coherence
    # v1.1.4 — append-only decisions log.
    decisions = await db_module.get_decisions(await _db(request), project_id)
    goal["decisions"] = decisions
    # v0.6.1 — also serve the XML envelope so MCP / cache-aware consumers
    # don't have to re-stitch fields locally. The JSON keys stay for the
    # dashboard and the test suite.
    goal["xml"] = db_module.build_goal_xml(
        goal, project["name"], goal["ambient_tasks"], coherence,
        decisions=decisions,
    )
    # v0.6.2 — pre-built Anthropic content blocks with cache_control
    # markers on the static fields. Callers can pass these straight
    # into messages.create() to get prompt caching for free.
    goal["cache_blocks"] = db_module.build_goal_cache_blocks(
        goal, project["name"], goal["ambient_tasks"]
    )
    return goal


@app.patch("/projects/{project_id}/goal-mode")
async def patch_goal_mode(
    project_id: str, body: GoalModeSet, request: Request
) -> dict[str, str]:
    """Switch a project between 'manual' and 'auto' goal modes."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        await db_module.set_goal_mode(await _db(request), project_id, body.mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"project_id": project_id, "goal_mode": body.mode}


@app.get("/projects/{project_id}/goal-mode")
async def get_goal_mode(project_id: str, request: Request) -> dict[str, str]:
    """Return the current goal mode for a project."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    mode = await db_module.get_goal_mode(await _db(request), project_id)
    return {"project_id": project_id, "goal_mode": mode}


@app.post("/projects/{project_id}/goal", response_model=GoalState)
async def set_goal(
    project_id: str, body: GoalSet, request: Request
) -> dict[str, Any]:
    """Upsert the goal state, incrementing version.

    Goal-ownership rule (v0.3.2): if the project has a recorded
    ``creator_human_id`` *and* the request body supplies a ``human_id``
    that doesn't match, refuse with 403. Sessions without a human_id
    (legacy callers, MCP workers that don't claim an identity) keep
    their old write privilege.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    owner = await db_module.get_project_owner(await _db(request), project_id)
    if owner is not None and body.human_id is not None and body.human_id != owner:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "goal_locked",
                "message": (
                    "Only the project owner can set the goal. "
                    "Use the HITL queue to propose changes."
                ),
            },
        )
    _goal_str = body.content if isinstance(body.content, str) else json.dumps(body.content)
    validate_input_size(_goal_str, "goal", 10_000)
    if body.north_star is not None:
        validate_input_size(body.north_star, "north_star", 10_000)
    if body.sprint is not None:
        validate_input_size(body.sprint, "version_goal", 10_000)
    result = await db_module.set_goal(
        await _db(request), project_id, body.content,
        north_star=body.north_star, sprint=body.sprint,
        minor=body.minor,
    )
    await goal_md_module.sync_db_to_goal_md(await _db(request), project_id)
    return result


@app.post("/projects/{project_id}/goal/north-star", response_model=GoalState)
async def set_north_star(
    project_id: str, body: SetNorthStarRequest, request: Request
) -> dict[str, Any]:
    """v0.5.2 — update only the north star field.

    Owner-only: requires ``human_id`` matching the project creator.
    Returns the new goal version.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    validate_input_size(body.north_star, "north_star", 10_000)
    # Ownership check skipped in hosted mode — session cookie already proves
    # the caller owns this project. human_id check only applies to local no-auth.
    try:
        result = await db_module.set_north_star(
            await _db(request), project_id, body.north_star
        )
        await goal_md_module.sync_db_to_goal_md(await _db(request), project_id)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/projects/{project_id}/goal/sprint", response_model=GoalState)
async def set_sprint(
    project_id: str, body: SetSprintRequest, request: Request
) -> dict[str, Any]:
    """v0.5.2 — update only the sprint field.

    Any team member can update the sprint — no ownership check.
    Returns the new goal version.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    validate_input_size(body.sprint, "version_goal", 10_000)
    try:
        result = await db_module.set_sprint(
            await _db(request), project_id, body.sprint
        )
        await goal_md_module.sync_db_to_goal_md(await _db(request), project_id)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/projects/{project_id}/start-worker-session")
async def start_worker_session_endpoint(
    project_id: str, request: Request,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """v1.2.0 — REST mirror of the MCP ``start_worker_session`` tool.

    Optional body: ``{task_id}``. Returns
    ``{session_id, task, worker_context}`` or 404 when there's no
    claimable task / the named task doesn't belong to this project.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        return await db_module.start_worker_session(
            await _db(request),
            project_id,
            task_id=(body or {}).get("task_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/projects/{project_id}/decisions")
async def post_decision_endpoint(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """v1.1.4 — append a decision entry to the project's append-only
    decisions log. Body: ``{text}``."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")
    validate_input_size(text, "decision text", 100_000)
    updated = await db_module.set_decision(await _db(request), project_id, text)
    return {"project_id": project_id, "decisions": updated}


@app.get("/projects/{project_id}/timeline")
async def get_timeline_endpoint(
    project_id: str, request: Request
) -> dict[str, Any]:
    """v1.1.1 — return the data needed to render the Activity Timeline.

    v1.6.x — filtered to only meaningful history: completed/failed tasks
    plus goal-change events. Session idle/active events were noise and
    have been dropped (the LIVE vtab covers active sessions instead).
    Pending/in_progress tasks belong on the LIVE tab, not in history.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    timeline = await db_module.get_timeline(await _db(request), project_id)
    return {
        "tasks": [
            t for t in timeline.get("tasks", [])
            if t.get("status") in ("done", "failed")
        ],
        "sessions": [],
        "goal_events": timeline.get("goal_events", []),
        "daily_counts": timeline.get("daily_counts", []),
        "people": timeline.get("people", []),
        "clients": timeline.get("clients", []),
    }


@app.get("/projects/{project_id}/rewind")
async def get_rewind(
    project_id: str,
    request: Request,
    days: int = 7,
    token: str | None = None,
) -> dict[str, Any]:
    """v1.3.0 — "Last X days" project rewind summary.

    Returns versions shipped, goal changes, decisions logged, session
    summaries, sprint items completed, and task counts for the period.
    When a ``token`` query param is supplied, it must match the project's
    stored ``rewind_token`` — letting an external link validate ownership
    without any other auth (Meridian is local-first; no token = no auth
    required, same as every other endpoint).
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if token is not None:
        stored = await db_module.get_rewind_token(await _db(request), project_id)
        if not stored or token != stored:
            raise HTTPException(status_code=403, detail="invalid rewind token")
    if days <= 0:
        raise HTTPException(status_code=422, detail="days must be positive")
    return await db_module.get_rewind_data(await _db(request), project_id, days)


@app.post("/projects/{project_id}/rewind-token")
async def post_rewind_token(
    project_id: str, request: Request
) -> dict[str, str]:
    """v1.3.0 — mint (or return) the project's shareable rewind token.

    The token is stored on the projects row so subsequent calls return
    the same value; teams can publish a link once without it rotating.
    Response: ``{"token": "<uuid4>", "expires": "never"}``.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    token = await db_module.get_or_create_rewind_token(await _db(request), project_id)
    return {"token": token, "expires": "never"}


@app.get("/projects/{project_id}/goal-history")
async def get_goal_history(
    project_id: str, request: Request
) -> list[dict[str, Any]]:
    """Return meaningful goal versions for a project, newest first.

    AUTO BLOCKS-only versions are collapsed out so the history shows
    only real content changes. Each entry: version, north_star,
    version_goal, sprint, created_at. Used by the Rewind goal subtab.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.get_goal_history(await _db(request), project_id)


@app.get("/projects/{project_id}/stats")
async def get_project_stats(
    project_id: str, request: Request, days: int = 30
) -> dict[str, Any]:
    """Return activity stats for the Charts subtab.

    Returns tasks/day series and sprint completion % per version.
    ``days`` defaults to 30, max 365.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    days = max(1, min(days, 365))
    return await db_module.get_project_stats(await _db(request), project_id, days)


# Sprint item routes → meridian/routes/sprint.py


@app.get("/projects/{project_id}/sessions", response_model=list[Session])
async def get_sessions(
    project_id: str, request: Request, active_only: bool = True
) -> list[dict[str, Any]]:
    """List sessions attached to the project.

    Pass ``?active_only=false`` to include closed and archived sessions
    (useful for the LIVE tab showing recent session outcomes).
    Expires stale sessions (last_seen > 30 min ago) before returning so
    the dashboard doesn't accumulate ghost entries indefinitely.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    await _expire_and_generate_handoffs(await _db(request), _data_dir(request))
    return await db_module.get_sessions(
        await _db(request), project_id, active_only=active_only
    )


# Tasks + claim/release routes → meridian/routes/tasks.py


# Handoff route → meridian/routes/handoff.py


# Session lifecycle routes → meridian/routes/sessions.py


# /tasks POST + PATCH → meridian/routes/tasks.py


@app.get("/projects/{project_id}/worktrees")
async def list_worktrees(project_id: str, request: Request) -> list[dict[str, Any]]:
    """List active git worktrees registered for a project."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    # Degrade gracefully: a missing/not-yet-migrated active_worktrees table must
    # not 500 the dashboard panel — return [] and log instead.
    try:
        return await db_module.list_active_worktrees(await _db(request), project_id)
    except Exception as exc:  # noqa: BLE001
        import logging as _l
        _l.getLogger("meridian.server").warning(
            "list_worktrees failed for project %s: %s", project_id, exc
        )
        return []


@app.post("/projects/{project_id}/worktrees", status_code=201)
async def create_worktree(
    project_id: str, request: Request, body: WorktreeCreate
) -> dict[str, Any]:
    """Register a git worktree for a session. Call after `git worktree add`."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.register_worktree(
        await _db(request),
        body.session_id,
        project_id,
        body.branch,
        body.path,
        item_id=body.item_id,
    )


@app.delete("/projects/{project_id}/worktrees/{worktree_id}", status_code=204)
async def delete_worktree(
    project_id: str, worktree_id: str, request: Request
) -> None:
    """Mark a registered worktree as removed. Call after `git worktree remove`."""
    removed = await db_module.remove_worktree(await _db(request), worktree_id)
    if not removed:
        raise HTTPException(status_code=404, detail="worktree not found or already removed")


@app.get("/projects/{project_id}/export/pdf")
async def export_project_pdf(project_id: str, request: Request):
    """Generate a tamper-evident IP attribution PDF for the project.

    Contains north star, version goal, sprint, full task log with
    timestamps and session names, and a SHA-256 hash of the content
    embedded in the footer.
    """
    import hashlib
    from fpdf import FPDF
    import io

    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    goal = await db_module.get_goal(db, project_id)
    tasks = await db_module.get_tasks(db, project_id, limit=200)
    sessions = await db_module.get_sessions(db, project_id, active_only=False)
    session_names = {s["id"]: s["name"] for s in sessions}

    # Build text content for hashing
    lines = [
        f"MERIDIAN IP ATTRIBUTION RECORD",
        f"Project: {project['name']} ({project['id']})",
        f"Generated: {__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}",
        "",
    ]
    if goal:
        lines += [
            f"Goal Version: {goal['version']}",
            f"North Star: {goal.get('north_star') or '(not set)'}",
            f"Version Goal: {goal['content']}",
            f"Sprint: {goal.get('sprint') or '(not set)'}",
            "",
        ]
    lines.append("TASK LOG:")
    for t in tasks:
        sname = session_names.get(t["session_id"], t["session_id"][:8])
        lines.append(f"[{t['created_at']}] [{t['status'].upper()}] {sname}: {t['description']}")

    full_text = "\n".join(lines)
    sha256 = hashlib.sha256(full_text.encode()).hexdigest()

    # Build PDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Meridian IP Attribution Record", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 7, f"Project: {project['name']}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    if goal:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, "Goal Hierarchy", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        for label, val in [
            ("North Star", goal.get("north_star") or "(not set)"),
            ("Version Goal", str(goal["content"])),
            ("Sprint", goal.get("sprint") or "(not set)"),
        ]:
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(30, 6, f"{label}:", new_x="RIGHT", new_y="LAST")
            pdf.set_font("Helvetica", "", 9)
            # Multi-line safe: use multi_cell for value
            x, y = pdf.get_x(), pdf.get_y()
            pdf.multi_cell(0, 6, val[:300])
        pdf.ln(4)

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, f"Task Log ({len(tasks)} entries)", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Courier", "", 7)
    for t in tasks:
        sname = session_names.get(t["session_id"], t["session_id"][:8])
        row = f"[{t['created_at']}] [{t['status'].upper()}] {sname}: {t['description']}"
        pdf.multi_cell(0, 5, row[:200])

    # Footer with SHA256
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(0, 5, f"SHA-256: {sha256}")

    pdf_bytes = pdf.output()
    return Response(
        content=bytes(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{project["name"]}_ip_record.pdf"'
        },
    )


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

_DEMO_FILE_CONTENT: dict[str, dict[str, str]] = {
    "backend-api-v2": {
        "AGENTS.md": """\
# backend-api-v2 — Agent Session Instructions

## Start a session

Run `start_session(project_id="...", session_name="describe-what-youre-doing")` at session start.
This returns your sprint items, active sessions, and goal in one call.

## Project context

REST API in Python (FastAPI) + Postgres. Rate limiting via Redis token bucket.
API key auth: `mk_live_` / `mk_test_` prefixed keys, only the hash stored.

## Session rules

- `log_task(session_id, project_id, desc)` after each meaningful task
- `pin_decision(...)` for architectural choices
- `request_hitl(...)` when blocked on a human decision
- `checkpoint(...)` before ending

## Common gotchas

- Use `%s` placeholders in psycopg3 queries, never `?`
- Rate limiter tests: fakeredis for unit tests, real Redis for nightly suite
- API keys: always hash before storage, never log raw key value
- Migrations in `alembic/` — never edit schema directly in prod
""",
        "ROADMAP.md": """\
# backend-api-v2 Roadmap

## v1.2 (current sprint)
- [x] Redis token bucket rate limiting
- [x] POST /v1/api-keys — issue API keys
- [x] DELETE /v1/api-keys/{id} — revoke keys
- [x] OpenAPI spec generation + Redoc UI at /docs
- [x] Integration test suite (47 tests)
- [ ] TypeScript client SDK from OpenAPI spec
- [ ] Python client SDK from OpenAPI spec
- [ ] Load test rate limiter at 10k req/min

## v1.3
- [ ] Outbound webhooks — POST on entity change
- [ ] Event streaming via SSE for live updates

## v1.4
- [ ] Multi-tenant isolation (schema-per-tenant)
""",
        "DEVLOG.md": """\
# backend-api-v2 Dev Log

## 2026-06-04
**Bob** — Finished POST /v1/api-keys and DELETE revoke endpoint. All tests passing.
Worker 2 started TypeScript SDK generation from OpenAPI spec.

## 2026-06-03
**Alice** — Unblocked Bob's API keys PR — resolved merge conflict with worker 1's OpenAPI changes.
**Worker 1** — Merged api_keys endpoints into spec. 47 integration tests all green.

## 2026-05-28
**Bob** — Rate limiter shipped to staging. 1000 req/min load test holding steady.
Found off-by-one in window reset logic — fixed + test added.

## 2026-05-22
**Alice** — Architecture review: cursor pagination wins over offset.
URL versioning (/v1/) confirmed. Reviewed Bob's rate limiter PR.
Redis connection pool leak on exception path — fixed.
""",
        "CLAUDE.md": """\
# backend-api-v2 — Session Instructions

## Project ID
`PROJECT_ID=<project-id>`

## Stack
Python 3.12, FastAPI, psycopg3 (asyncpg removed), Redis, Alembic migrations.
CI: GitHub Actions — tests + lint on PR, Docker build on merge.

## Key rules
- `%s` placeholders only in SQL — never `?`
- API key generation: 32-byte base64url, prefix `mk_live_`/`mk_test_`, store hash only
- Rate limiter: Redis EVAL scripts for atomic token bucket operations
- All endpoints under `/v1/` prefix

## Before pushing
Run `pytest -x` — all 47 integration tests must pass.
""",
        "README.md": """\
# backend-api-v2

Fast, observable REST API — rate limiting, API key auth, full OpenAPI spec.

## Quick start

```bash
docker compose up        # Postgres + Redis
uvicorn main:app --reload
```

Docs: http://localhost:8000/docs

## Auth

Issue an API key: `POST /v1/api-keys`
Authenticate: `Authorization: Bearer mk_live_...`

## Rate limits

100 req/min per API key.
Response headers: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`.
""",
        "DECISIONS.md": """\
# backend-api-v2 — Architectural Decisions

## Postgres over MySQL [2026-05-05]
Better JSON operators, pgvector for future embedding search, existing team familiarity.

## API versioning via URL prefix, not Accept header [2026-05-07]
/v1/ is visible in logs, easy to route in nginx, simpler for SDK consumers.

## Redis token bucket for rate limiting [2026-05-28]
Survives deploys, consistent across instances. In-memory rejected — state lost on restart.

## API keys prefixed mk_live_ / mk_test_ [2026-05-30]
Stripe-style prefixes identifiable in logs and support tickets.
Raw key shown once on creation, only hash stored. 32-byte base64url = 256 bits entropy.

## Cursor-based pagination over offset [2026-05-22]
Offset pagination breaks when rows are inserted during traversal.
Cursor (created_at + id) is stable under concurrent writes. Default 20, max 100.
""",
    },
    "data-pipeline": {
        "AGENTS.md": """\
# data-pipeline — Agent Session Instructions

## Start a session

Run `start_session(project_id="...", session_name="describe-what-youre-doing")` at session start.

## Project context

Kafka + Flink + Iceberg ETL pipeline ingesting 50M events/day from 8 sources.
Avro schemas with Confluent Schema Registry. Dead Letter Queue per source.

## Session rules

- `log_task(session_id, project_id, desc)` after each meaningful task
- `pin_decision(...)` for schema or architecture choices
- `request_hitl(...)` for any schema evolution change touching production consumers
- `checkpoint(...)` before ending

## Critical: schema changes

Any change to a registered Avro schema must go through HITL in Meridian before applying.
Incompatible changes break consumers — caught at Schema Registry only if FORWARD_TRANSITIVE is enforced.
""",
        "ROADMAP.md": """\
# data-pipeline Roadmap

## v0.3 (current sprint)
- [x] Dead Letter Queue per source topic
- [x] DLQ replay tooling (scripts/dlq_replay.py)
- [x] Consumer auto-scaling (HPA at 50k lag)
- [x] PagerDuty alerts: DLQ spike, consumer lag, disk
- [x] Iceberg table compaction nightly job
- [x] E2E test suite (34 tests)
- [x] On-call runbook — all alert types documented
- [ ] Load test: 50M events/day sustained simulation

## v0.4
- [ ] Schema evolution: backward compatibility CI gate
- [ ] Data lineage tracking (OpenLineage)
- [ ] Cost dashboard: Kafka + compute + storage by source
- [ ] DLQ self-service replay UI

## v0.5
- [ ] Debezium CDC for Postgres change data capture
""",
        "DEVLOG.md": """\
# data-pipeline Dev Log

## 2026-06-04
**Jordan** — Updated on-call runbooks for DLQ, Kafka broker failure, schema registry outage.
**Worker 1** — E2E test suite: 34 tests, all 8 sources covered. Load test running now (48M/day, investigating).
**Worker 2** — Snapshot expiry policy live: 30 snapshots retained, storage costs projected -60%.

## 2026-06-03
**Jordan** — PagerDuty alerts wired: DLQ spike, consumer lag >100k, broker disk >80%.
Silence rules: IoT sensor source excluded during 02:00-04:00 maintenance window.
**Worker 2** — Iceberg compaction live. Query latency -40% on 30-day lookbacks.

## 2026-05-30
**Maya** — Fixed consumer offset commit race on rebalance. Was causing duplicate processing at partition handoff.
Consumer auto-scaling live: HPA triggers above 50k lag, tested under 3x load.

## 2026-05-28
**Jordan** — DLQ consumer: logs error, emits metric, archives to S3 Parquet. Replay tooling complete.
Stress test: 10k bad events injected, all landed in DLQ, zero main pipeline impact.

## 2026-05-28
**Maya** — Schema Registry deployed. All 8 sources registered with Avro schemas. FORWARD_TRANSITIVE policy set.
""",
        "CLAUDE.md": """\
# data-pipeline — Session Instructions

## Project ID
`PROJECT_ID=<project-id>`

## Stack
Apache Kafka (3-broker, k8s), Confluent Schema Registry (Avro),
Apache Flink (streaming, 5-min tumbling windows), Apache Iceberg + S3,
Python consumers (Faust), PagerDuty, Grafana.

## Key rules
- Schema changes: ALWAYS request_hitl before modifying a registered schema
- DLQ topics: `{source}-dlq` — never publish to main topic from replay without HITL
- Flink: periodic 1-second watermarks, 2-minute out-of-orderness bound
- Consumer offsets: commit after successful processing only — never auto-commit

## Testing
`pytest tests/` for unit tests.
E2E tests in `tests/e2e/` require full Kafka + Flink + Iceberg stack running.
""",
        "README.md": """\
# data-pipeline

ETL pipeline ingesting 50M events/day from 8 sources. Kafka → Flink → Iceberg.

## Architecture

```
8 sources → Kafka topics → Avro validation → Flink windowing → Iceberg tables
                                ↓
                         Dead Letter Queue → S3 Parquet archive
```

## Local dev

```bash
docker compose up  # Kafka, Schema Registry, Flink, MinIO (S3-compatible)
python -m consumers.clickstream
```

## Key metrics

- 50M events/day throughput target
- p99 end-to-end latency: 4m12s (target: <5 min)
- On-call burden target: <2 pages/week
""",
        "DECISIONS.md": """\
# data-pipeline — Architectural Decisions

## Avro over Protobuf for schema serialization [2026-05-14]
Avro integrates natively with Confluent Schema Registry. Protobuf requires cross-team code generation.

## Flink over Spark Streaming for windowing [2026-05-22]
Flink true streaming (not micro-batch). Lower latency, better watermark handling.
Spark Streaming prototyped: 3x higher p99 latency, rejected.

## Iceberg over Delta Lake and Hudi for table format [2026-05-22]
Best Flink native connector. Partition evolution without rewrite. PyIceberg for Python consumers.

## Dead Letter Queue per source, not per error type [2026-05-30]
Per-source DLQs easier to operate — on-call knows which team owns each queue.

## FORWARD_TRANSITIVE schema compatibility policy [2026-05-28]
Writers must be backward compatible; readers handle forward compatible changes.
Breaks caught at Schema Registry registration time.

## PagerDuty over OpsGenie for alerting [2026-06-02]
Company standard, existing on-call rotation, Grafana integration already set up.
""",
    },
}


@app.get("/projects/{project_id}/files")
async def list_project_files(
    project_id: str, request: Request
) -> list[str]:
    """Return the list of editable markdown files for a project.

    Files that do not yet exist on disk are still listed so the user can
    create them from the dashboard. 403 is raised if the project is unknown.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return _EDITABLE_FILES


@app.get("/projects/{project_id}/files/{filename}")
async def get_project_file(
    project_id: str, filename: str, request: Request
) -> dict[str, str]:
    """Read one editable markdown file and return its content.

    Returns ``{"filename": ..., "content": ...}`` with an empty string when
    the file does not yet exist. 403 if the filename is not in the allow-list.
    In demo mode, returns realistic fake content keyed by project name.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if filename not in _EDITABLE_FILES:
        raise HTTPException(status_code=403, detail="file not in allow-list")
    from fastapi.responses import JSONResponse
    env_demo = os.environ.get("MERIDIAN_DEMO", "").lower() in ("1", "true", "yes")
    cookie_demo = bool(request.cookies.get(_DEMO_CONTEXT_COOKIE))
    if env_demo or cookie_demo:
        proj_name = project.get("name", "")
        demo_files = _DEMO_FILE_CONTENT.get(proj_name, {})
        content = demo_files.get(filename, "")
        return JSONResponse(content={"filename": filename, "content": content}, headers={"Content-Type": "application/json; charset=utf-8"})
    path = _REPO_ROOT / filename
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    return JSONResponse(content={"filename": filename, "content": content}, headers={"Content-Type": "application/json; charset=utf-8"})


@app.put("/projects/{project_id}/files/{filename}")
async def put_project_file(
    project_id: str, filename: str, body: FileContent, request: Request
) -> dict[str, object]:
    """Write content to one editable markdown file.

    Creates the file if it does not exist. 403 if the filename is not in the
    allow-list. Returns ``{"filename": ..., "size": <bytes>}``.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if filename not in _EDITABLE_FILES:
        raise HTTPException(status_code=403, detail="file not in allow-list")
    path = _REPO_ROOT / filename
    path.write_text(body.content, encoding="utf-8")
    return {"filename": filename, "size": len(body.content.encode("utf-8"))}


@app.post("/projects/{project_id}/devlog")
async def append_devlog_entry(
    project_id: str, body: dict, request: Request
) -> dict[str, object]:
    """Append a user-written line to DEVLOG.md via the devlog anchor."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    line = f"- {_md_ts()} {text}"
    await md_anchors_module.apply_append("DEVLOG.md", "devlog", line)
    return {"ok": True}


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
    goal: dict | None,
    sprint_items: list[dict],
    pending_tasks: list[dict],
    sessions: list[dict],
    recent_decisions: list[str],
    *,
    mode: str = "full",
    repo_root: str | None = None,
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


@app.get("/projects/{project_id}/context-block")
async def get_project_context_block(
    project_id: str, request: Request, mode: str = "full"
) -> Response:
    """v2.3 — plain-text context block suitable for direct clipboard paste.

    Query: ``?mode=full`` (default) or ``?mode=chat`` (shorter).
    Returns ``text/plain`` — the dashboard "Code Handoff" / "Copy chat
    context" buttons stream this straight into ``navigator.clipboard``.
    """
    if mode not in ("full", "chat"):
        raise HTTPException(status_code=400, detail="mode must be 'full' or 'chat'")
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    goal = await db_module.get_goal(await _db(request), project_id)
    sprint_items = await db_module.get_sprint_items(
        await _db(request), project_id, status="pending"
    )
    all_tasks = await db_module.get_tasks(await _db(request), project_id, limit=20)
    pending_tasks = [
        t for t in all_tasks if t.get("status") in ("pending", "in_progress", "done")
    ][:10]
    sessions = await db_module.get_sessions(
        await _db(request), project_id, active_only=True
    )
    decisions_raw = (project.get("decisions") or "").strip()
    recent_decisions = [
        l.strip() for l in decisions_raw.splitlines() if l.strip()
    ][-5:]
    text = _render_context_block(
        project, goal, sprint_items, pending_tasks, sessions, recent_decisions,
        mode=mode,
        repo_root=str(Path.cwd()) if mode == "full" else None,
    )
    return Response(content=text, media_type="text/plain; charset=utf-8")


@app.get("/projects/{project_id}/context")
async def get_project_context(
    project_id: str, request: Request
) -> dict[str, Any]:
    """Return a onboarding context payload for new chat sessions (v1.9.x).

    A new claude.ai chat can paste this JSON to get up to speed instantly.
    Returns: north_star, current_sprint, sprint_items (pending), recent_decisions
    (last 5), pending_tasks (last 10), recent_sessions (last 5), file_map.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    goal = await db_module.get_goal(await _db(request), project_id)
    sprint_items = await db_module.get_sprint_items(
        await _db(request), project_id, status="pending", show_blocked=False
    )
    all_tasks = await db_module.get_tasks(await _db(request), project_id, limit=20)
    pending_tasks = [t for t in all_tasks if t["status"] in ("pending", "in_progress")][:10]
    sessions = await db_module.get_sessions(await _db(request), project_id, active_only=True)
    decisions_raw = (project.get("decisions") or "").strip()
    recent_decisions = [l.strip() for l in decisions_raw.splitlines() if l.strip()][-5:]
    return {
        "project": {"id": project["id"], "name": project["name"]},
        "north_star": goal.get("north_star") if goal else None,
        "current_sprint": goal.get("sprint") if goal else None,
        "version_goal": goal.get("content") if goal else None,
        "sprint_items": sprint_items,
        "recent_decisions": recent_decisions,
        "pending_tasks": pending_tasks,
        "recent_sessions": sessions[:5],
        "file_map": list(_EDITABLE_FILES),
    }


# Decisions routes → meridian/routes/decisions.py
# ---------------------------------------------------------------------------
# v2.4 — HITL (human-in-the-loop) queue
# ---------------------------------------------------------------------------


# HITL routes → meridian/routes/hitl.py
# Notes routes → meridian/routes/notes.py


# ---------------------------------------------------------------------------
# v2.4 — Team visibility (per-human swimlane + standup digest data)
# ---------------------------------------------------------------------------


@app.get("/team/summary")
async def get_team_summary_endpoint(
    request: Request, project_id: str | None = None, days: int = 1
) -> dict[str, Any]:
    """Aggregate task_log + sessions by human_id over the last N days.

    ``project_id`` optional — omit to roll up across all projects.
    Returns ``{period_days, humans:[...], active_count}``. Used by the
    Team tab cards, swimlane timeline, and standup digest.
    """
    return await db_module.get_team_summary(await _db(request), project_id, days)


# ---------------------------------------------------------------------------
# v2.4 — Webhook intake for framework integrations
# ---------------------------------------------------------------------------


@app.post("/projects/{project_id}/events", status_code=201)
async def post_project_event(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Normalize a framework event into Meridian's task_log.

    Auth: ``X-Meridian-Token: <project_token>`` header.  The token grants
    write-access to a single project's task_log only — call
    ``ensure_project_token`` to mint one (returned by the dashboard's
    Project settings panel, by GET /projects/{id}/webhook-token).

    Body schema (all optional except description+session_name):
    ```
    {
      "type": "task_completed" | "checkpoint" | "hitl_request" | "session_start",
      "session_name": "langgraph-researcher",
      "human_id": "langgraph",
      "agent_framework": "langgraph",
      "description": "researcher agent fetched 3 sources",
      "status": "done",
      "parent_task_id": null,
      "metadata": {}
    }
    ```
    """
    db = await _db(request)
    auth_token = request.headers.get("X-Meridian-Token", "")
    project_by_token = await db_module.get_project_by_token(db, auth_token) if auth_token else None
    if project_by_token is None or project_by_token["id"] != project_id:
        raise HTTPException(status_code=401, detail="invalid or missing X-Meridian-Token")

    event_type = body.get("type") or "task_completed"
    description = (body.get("description") or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="description required")
    session_name = body.get("session_name") or f"webhook/{body.get('agent_framework', 'custom')}"
    human_id = body.get("human_id")
    framework = body.get("agent_framework") or "custom"
    status = body.get("status") or "done"

    # Find-or-create a session for this framework/human/name combo so
    # bursty webhook traffic doesn't create one session per event.
    sessions = await db_module.get_sessions(db, project_id, active_only=False)
    target = next(
        (s for s in sessions if s.get("name") == session_name and s.get("agent_framework") == framework),
        None,
    )
    if target is None:
        target = await db_module.register_session(
            db, project_id, session_name,
            human_id=human_id, agent_framework=framework,
        )

    if event_type == "hitl_request":
        return await db_module.request_hitl(
            db, project_id, description,
            session_id=target["id"], context=body.get("context"),
            urgency=body.get("urgency", "normal"),
            assigned_to=body.get("assigned_to"),
        )

    task = await db_module.log_task(
        db, target["id"], project_id, description,
        status=status, parent_task_id=body.get("parent_task_id"),
    )
    return {"task": task, "session_id": target["id"], "event_type": event_type}


@app.get("/projects/{project_id}/webhook-token")
async def get_project_webhook_token(
    project_id: str, request: Request
) -> dict[str, Any]:
    """Mint-and-return the project webhook token. Shown ONCE in the UI."""
    token = await db_module.ensure_project_token(await _db(request), project_id)
    if token is None:
        raise HTTPException(status_code=404, detail="project not found")
    return {"project_id": project_id, "token": token}


# ---------------------------------------------------------------------------
# v3.0 — Executor runs
# ---------------------------------------------------------------------------


@app.get("/projects/{project_id}/search")
async def search_project_all(
    project_id: str,
    request: Request,
    q: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    """Universal search across tasks, notes, decisions, and sprint items."""
    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if not q.strip():
        return {"query": q, "tasks": [], "notes": [], "decisions": [], "sprint_items": [], "total": 0}
    return await db_module.search_all(db, project_id, q.strip(), limit=limit)


@app.get("/projects/{project_id}/runs")
async def get_project_runs(
    project_id: str,
    request: Request,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List executor_runs for a project, newest first."""
    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    runs = await db_module.get_executor_runs(db, project_id, limit=limit)
    for run in runs:
        if run.get("started_at") and run.get("ended_at"):
            from datetime import datetime
            try:
                fmt = "%Y-%m-%d %H:%M:%S"
                start = datetime.strptime(run["started_at"], fmt)
                end = datetime.strptime(run["ended_at"], fmt)
                run["duration_s"] = int((end - start).total_seconds())
            except Exception:
                run["duration_s"] = None
        else:
            run["duration_s"] = None
    return runs


@app.get("/projects/{project_id}/runs/{run_id}")
async def get_project_run(
    project_id: str,
    run_id: str,
    request: Request,
) -> dict[str, Any]:
    """Return a single executor_run with full transcript."""
    db = await _db(request)
    run = await db_module.get_executor_run(db, run_id)
    if run is None or run.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="run not found")
    return run


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


async def _require_workspace_perm(
    request: Request, tenant: dict[str, Any], perm: str,
) -> str:
    """G5.19 — Resolve the calling user's role in the tenant's workspace
    and 403 if they lack ``perm``. Returns the resolved role on success.

    The tenant owner (email matches tenant.email) implicitly has every
    permission. For invitees, ROLE_PERMS gates the action.
    """
    from .hosted import get_current_tenant as _get_ct  # noqa: PLC0415
    from .roles import has_perm  # noqa: PLC0415
    try:
        caller = await _get_ct(request)
    except HTTPException:
        raise HTTPException(403, "Sign in required")
    caller_email = caller.get("email", "")
    resolved = await db_module.resolve_member_role(
        request.app.state.db, tenant["id"], caller_email,
    )
    role = resolved[0] if resolved else None
    if not has_perm(role, perm):
        raise HTTPException(
            403,
            f"Workspace role '{role or 'none'}' lacks permission '{perm}'",
        )
    return role  # type: ignore[return-value]


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


@app.get("/workspace/notes")
async def workspace_list_notes(request: Request, tag: str | None = None) -> list[dict[str, Any]]:
    """List workspace-level notes, newest first."""
    db = await _db(request)
    _t = await _get_tenant_from_request(request)
    return await db_module.get_workspace_notes(
        db, tag=tag, tenant_id=_t["id"] if _t else None
    )


@app.post("/workspace/notes", status_code=201)
async def workspace_add_note(request: Request) -> dict[str, Any]:
    """Add a workspace-level note."""
    db = await _db(request)
    body = await request.json()
    title = (body.get("title") or "").strip()
    content = (body.get("body") or "").strip()
    if not title or not content:
        raise HTTPException(status_code=422, detail="title and body are required")
    validate_input_size(title, "note title", 500)
    validate_input_size(content, "note body", 10_000_000)
    _t = await _get_tenant_from_request(request)
    return await db_module.add_workspace_note(
        db, title, content, body.get("tags"), tenant_id=_t["id"] if _t else None
    )


@app.patch("/workspace/notes/{note_id}")
async def workspace_update_note(request: Request, note_id: str) -> dict[str, Any]:
    """Patch title/body/tags on a workspace note."""
    db = await _db(request)
    body = await request.json()
    _t = await _get_tenant_from_request(request)
    result = await db_module.update_workspace_note(
        db, note_id,
        title=body.get("title"),
        body=body.get("body"),
        tags=body.get("tags"),
        tenant_id=_t["id"] if _t else None,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="note not found")
    return result


@app.delete("/workspace/notes/{note_id}", status_code=204)
async def workspace_delete_note(request: Request, note_id: str) -> None:
    """Delete a workspace note."""
    db = await _db(request)
    _t = await _get_tenant_from_request(request)
    deleted = await db_module.delete_workspace_note(
        db, note_id, tenant_id=_t["id"] if _t else None
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="note not found")


def _is_demo_request(request: Request) -> bool:
    """Return True when the request is in demo mode (env flag or cookie)."""
    env_demo = os.environ.get("MERIDIAN_DEMO", "").lower() in ("1", "true", "yes")
    cookie_demo = bool(request.cookies.get(_DEMO_CONTEXT_COOKIE))
    return env_demo or cookie_demo


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


@app.post("/account/delete")
async def delete_account(request: Request) -> Response:
    """Self-service account deletion. Requires JSON body: {\"confirmation\": \"DELETE\"}."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    if _is_demo_request(request):
        return JSONResponse(
            {"detail": "Not available in demo mode. Sign up at usemeridian.us"},
            status_code=403,
        )
    from .hosted import get_current_tenant, cancel_stripe_subscription, _drop_tenant_neon_database, send_account_deleted_email
    from .roles import PERM_DELETE_TENANT  # noqa: PLC0415
    from fastapi.responses import JSONResponse
    tenant = await get_current_tenant(request)
    # G5.19 — only the tenant owner can delete the account. Admin
    # invitees are explicitly excluded by ROLE_PERMS.
    await _require_workspace_perm(request, tenant, PERM_DELETE_TENANT)
    body = await request.json()
    if body.get("confirmation") != "DELETE":
        raise HTTPException(status_code=400, detail="Type DELETE to confirm account deletion.")

    stripe_id = tenant.get("stripe_customer_id")
    if stripe_id:
        await cancel_stripe_subscription(stripe_id)

    if tenant.get("neon_project_id"):
        asyncio.create_task(_drop_tenant_neon_database(tenant))

    email = tenant.get("email", "")
    await db_module.delete_tenant_records(request.app.state.db, tenant["id"])

    if email:
        asyncio.create_task(send_account_deleted_email(email))

    resp = JSONResponse({"deleted": True})
    resp.delete_cookie("meridian_session")
    return resp


@app.get("/settings/mcp-config")
async def get_mcp_config(request: Request) -> dict[str, Any]:
    """Return project list + base URL for building the MCP client config snippet."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant
    tenant = await get_current_tenant(request)
    projects = await db_module.list_projects(await _db(request))
    return {
        "projects": [{"id": p["id"], "name": p["name"]} for p in projects],
        "base_url": os.environ.get("MERIDIAN_SERVER_URL", "https://usemeridian.us"),
        "tenant_id": tenant["id"],
    }


@app.get("/settings/usage")
async def get_usage_settings(request: Request) -> dict[str, Any]:
    """Return current compute + storage usage and overage caps for the tenant."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant, PLAN_LIMITS, COMPUTE_OVERAGE_RATE, STORAGE_OVERAGE_RATE
    tenant = await get_current_tenant(request)
    plan = tenant.get("plan") or "standard"
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
    unlimited = math.isinf(limits["cu_hours"])
    # float('inf') is not valid JSON for the browser's JSON.parse — emit null + a flag.
    cu_limit = None if math.isinf(limits["cu_hours"]) else limits["cu_hours"]
    cu_grace = None if unlimited else limits["cu_hours"] + limits["grace_cu_hours"]
    gb_limit = None if math.isinf(limits["storage_gb"]) else limits["storage_gb"]
    return {
        "plan": plan,
        "unlimited": unlimited,
        "compute": {
            "used": float(tenant.get("compute_cu_hours_used") or 0),
            "limit": cu_limit,
            "grace": cu_grace,
            "cap_usd": float(tenant.get("compute_overage_cap_usd") or 0),
            "throttled": bool(tenant.get("compute_throttled_at")),
            "rate": COMPUTE_OVERAGE_RATE,
            "unlimited": unlimited,
        },
        "storage": {
            "used_gb": float(tenant.get("storage_gb_used") or 0),
            "limit_gb": gb_limit,
            "cap_usd": float(tenant.get("storage_overage_cap_usd") or 0),
            "rate": STORAGE_OVERAGE_RATE,
            "unlimited": unlimited,
        },
    }


@app.patch("/settings/usage")
async def update_usage_caps(request: Request) -> dict[str, Any]:
    """Update compute and storage overage spending caps for the tenant."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant
    tenant = await get_current_tenant(request)
    body = await request.json()
    compute_cap = float(body.get("compute_cap") or 0)
    storage_cap = float(body.get("storage_cap") or 0)
    await db_module.update_tenant(
        request.app.state.db, tenant["id"],
        compute_overage_cap_usd=compute_cap,
        storage_overage_cap_usd=storage_cap,
    )
    return {"status": "ok", "compute_cap": compute_cap, "storage_cap": storage_cap}


# ---------------------------------------------------------------------------
# GitHub integration endpoints (hosted-tier only)
# ---------------------------------------------------------------------------

@app.post("/projects/{project_id}/github/connect")
async def github_connect(project_id: str, request: Request) -> dict[str, Any]:
    """Connect or update the tenant's GitHub repo settings."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    import httpx as _httpx
    from .hosted import _github_user_snapshot as _github_snapshot
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    body = await request.json()
    pat = (body.get("pat") or body.get("token") or body.get("access_token") or "").strip()
    repo = (body.get("repo") or "").strip()
    branch = (body.get("branch") or "main").strip()
    if not repo or "/" not in repo:
        raise HTTPException(status_code=422, detail="repo must be owner/repo format")
    fresh = await db_module.get_tenant_by_id(request.app.state.db, tenant["id"])
    stored_pat = db_module.decrypt_field((fresh or {}).get("github_pat"))
    validate_pat = pat or stored_pat or ""
    github_user = ""
    avatar_url = ""
    repos: list[dict[str, Any]] = []
    try:
        if validate_pat:
            snapshot = await _github_snapshot(validate_pat)
            github_user = snapshot.get("login", "")
            avatar_url = snapshot.get("avatar_url", "")
            repos = snapshot.get("repos") or []
            if repo and repos:
                repo_lookup = {r.get("full_name", ""): r for r in repos if r.get("full_name")}
                if repo not in repo_lookup:
                    repo = repos[0].get("full_name", repo)
                    branch = repos[0].get("default_branch") or branch
                elif branch == "main" and repo_lookup[repo].get("default_branch"):
                    branch = repo_lookup[repo].get("default_branch") or branch
        elif not stored_pat:
            raise HTTPException(status_code=422, detail="GitHub is not connected")
    except _httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach GitHub: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if pat:
        await db_module.update_tenant(
            request.app.state.db, tenant["id"],
            github_pat=db_module.encrypt_field(pat),
        )
    db = await _db(request)
    await db_module.update_project_settings(
        db, project_id,
        github_repo=repo,
        github_branch=branch,
    )
    return {
        "connected": True,
        "repo": repo,
        "branch": branch,
        "github_user": github_user,
        "avatar_url": avatar_url,
        "repos": repos,
    }


@app.get("/projects/{project_id}/github/status")
async def github_status(project_id: str, request: Request) -> dict[str, Any]:
    """Return the project's current GitHub connection status."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import _github_user_snapshot as _github_snapshot
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        fresh = await db_module.get_tenant_by_id(request.app.state.db, tenant["id"])
        if not fresh:
            return {"connected": False, "pat_linked": False, "repo": "", "branch": "main",
                    "github_user": "", "avatar_url": "", "repos": [], "last_verified": None}
        token = db_module.decrypt_field(fresh.get("github_pat"))
        project = await db_module.get_project(await _db(request), project_id)
        selected_repo = (project or {}).get("github_repo") or ""
        selected_branch = (project or {}).get("github_branch") or "main"
        snapshot: dict[str, Any] | None = None
        if token:
            try:
                snapshot = await _github_snapshot(token)
            except Exception:
                snapshot = None
        repos = (snapshot or {}).get("repos") or []
        if selected_repo and repos and not any(r.get("full_name") == selected_repo for r in repos):
            repos = [{"full_name": selected_repo, "name": selected_repo.split("/")[-1], "owner": selected_repo.split("/")[0] if "/" in selected_repo else "", "html_url": "", "default_branch": selected_branch, "private": False, "updated_at": ""}] + repos
        return {
            "connected": bool(token and selected_repo),
            "pat_linked": bool(token),
            "repo": selected_repo,
            "branch": selected_branch,
            "github_user": (snapshot or {}).get("login", ""),
            "avatar_url": (snapshot or {}).get("avatar_url", ""),
            "repos": repos,
            "last_verified": None,
        }
    except Exception:
        return {"connected": False, "pat_linked": False, "repo": "", "branch": "main",
                "github_user": "", "avatar_url": "", "repos": [], "last_verified": None}


@app.get("/projects/{project_id}/repo-image")
async def repo_image_proxy(project_id: str, request: Request, path: str = ""):
    """G7.32 — proxy a repo-relative image through the project's GitHub PAT.

    Used by markdown preview to render images that live in the connected
    repo (e.g. ``![](docs/screenshots/foo.png)``) without exposing the PAT
    to the browser. Returns the raw bytes with the upstream Content-Type.

    Limits:
     - Hosted-only (PAT lives on the tenant).
     - Path is normalized to disallow ``..``; absolute URLs are rejected.
     - Falls back to 404 when the project isn't connected to a repo.
     - 1 MB response cap (oversized images are 413).
    """
    if not _hosted_mode():
        raise HTTPException(404)
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(401, "not authenticated")
    fresh = await db_module.get_tenant_by_id(request.app.state.db, tenant["id"])
    pat = db_module.decrypt_field((fresh or {}).get("github_pat")) if fresh else None
    project = await db_module.get_project(await _db(request), project_id)
    repo = (project or {}).get("github_repo") or ""
    branch = (project or {}).get("github_branch") or "main"
    if not repo or not pat:
        raise HTTPException(404, "no repo connected")
    clean = path.strip().lstrip("/")
    if not clean or ".." in clean.split("/") or "://" in clean:
        raise HTTPException(400, "invalid path")
    if "/" not in repo:
        raise HTTPException(400, "repo not owner/name")
    owner, repo_name = repo.split("/", 1)
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo_name}/{branch}/{clean}"
    import httpx  # noqa: PLC0415
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(raw_url, headers={"Authorization": f"Bearer {pat}"})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"upstream fetch failed: {exc}") from exc
    if r.status_code == 404:
        raise HTTPException(404, "file not found in repo")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, "upstream error")
    body = r.content
    if len(body) > 1_000_000:
        raise HTTPException(413, "image too large")
    return Response(
        content=body,
        media_type=r.headers.get("content-type", "application/octet-stream"),
        headers={"Cache-Control": "private, max-age=60"},
    )




@app.post("/projects/{project_id}/github/push-mcp-template", status_code=201)
async def push_mcp_template(project_id: str, request: Request) -> dict[str, Any]:
    """Push template.mcp.json to the connected GitHub repo.

    Fails with 409 if the file already exists. The template contains a
    placeholder Bearer token — users fill it in locally; .mcp.json should
    be gitignored so the real token never gets committed.
    """
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")

    fresh = await db_module.get_tenant_by_id(request.app.state.db, tenant["id"])
    project = await db_module.get_project(await _db(request), project_id)
    pat = (fresh or {}).get("github_pat")
    repo = (project or {}).get("github_repo")
    if not pat or not repo:
        raise HTTPException(status_code=400, detail="No GitHub repo connected. Connect one in Settings first.")

    token = db_module.decrypt_field(pat)
    if not token:
        raise HTTPException(status_code=400, detail="GitHub token could not be decrypted. Reconnect your repo.")

    import httpx as _httpx
    gh_headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}

    template_content = json.dumps({
        "mcpServers": {
            "meridian": {
                "type": "http",
                "url": "https://usemeridian.us/mcp",
                "headers": {
                    "Authorization": "Bearer sk_meridian_YOUR_KEY_HERE"
                }
            }
        }
    }, indent=2)
    template_b64 = base64.b64encode(template_content.encode()).decode()

    async with _httpx.AsyncClient(timeout=10) as http:
        # Check if file already exists
        check = await http.get(
            f"https://api.github.com/repos/{repo}/contents/template.mcp.json",
            headers=gh_headers,
        )
        if check.status_code == 200:
            raise HTTPException(status_code=409, detail="template.mcp.json already exists in the repo.")

        # Create the file
        r = await http.put(
            f"https://api.github.com/repos/{repo}/contents/template.mcp.json",
            headers=gh_headers,
            json={
                "message": "Add Meridian MCP config template",
                "content": template_b64,
            },
        )
        if r.status_code not in (201, 200):
            raise HTTPException(status_code=r.status_code, detail=f"GitHub API error: {r.text[:200]}")

    return {"pushed": True, "file": "template.mcp.json", "repo": repo}

@app.delete("/projects/{project_id}/github/disconnect", status_code=200)
async def github_disconnect(project_id: str, request: Request) -> dict[str, Any]:
    """Clear the project's stored GitHub repo (keeps tenant PAT for other projects)."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    db = await _db(request)
    await db_module.update_project_settings(
        db, project_id,
        github_repo=None,
        github_branch=None,
    )
    _GITHUB_REPOS_CACHE.pop(tenant["id"], None)
    return {"disconnected": True}


# Per-tenant in-memory cache of the accessible GitHub repo list. Avoids hitting
# the GitHub API on every dropdown render; refreshed lazily after 24h or on demand.
_GITHUB_REPOS_CACHE: dict[str, dict[str, Any]] = {}
_GITHUB_REPOS_TTL_SECONDS = 24 * 3600


@app.get("/projects/{project_id}/github/repos")
async def github_repos(project_id: str, request: Request) -> dict[str, Any]:
    """Return the tenant's accessible GitHub repos for the connect dropdown.

    Cached in-memory for 24h per tenant; pass ?refresh=1 to force a re-fetch.
    """
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    import time as _time
    from .hosted import _github_user_snapshot as _github_snapshot
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    fresh = await db_module.get_tenant_by_id(request.app.state.db, tenant["id"])
    token = db_module.decrypt_field((fresh or {}).get("github_pat"))
    if not token:
        return {"connected": False, "repos": [], "synced_at": None}
    force = request.query_params.get("refresh") in ("1", "true", "yes")
    now = _time.time()
    cached = _GITHUB_REPOS_CACHE.get(tenant["id"])
    if cached and not force and (now - cached["fetched_at"]) < _GITHUB_REPOS_TTL_SECONDS:
        return {"connected": True, "repos": cached["repos"], "synced_at": cached["fetched_at"], "cached": True}
    try:
        snapshot = await _github_snapshot(token)
    except Exception as exc:
        if cached:
            return {"connected": True, "repos": cached["repos"], "synced_at": cached["fetched_at"], "cached": True, "stale": True}
        raise HTTPException(status_code=502, detail=f"GitHub repo fetch failed: {exc}") from exc
    repos = snapshot.get("repos") or []
    _GITHUB_REPOS_CACHE[tenant["id"]] = {"repos": repos, "fetched_at": now}
    return {"connected": True, "repos": repos, "synced_at": now, "cached": False}


# Common branch names we offer as a fallback when the live GitHub list is
# unavailable (e.g. the API is unreachable). The repo's current/default branch
# is always merged in by the caller so the saved value never disappears.
_FALLBACK_BRANCHES = ("main", "master", "dev", "develop", "gh-pages")


@app.get("/projects/{project_id}/github/branches")
async def github_branches(project_id: str, request: Request) -> dict[str, Any]:
    """v2.8 — list the branches of a repo so the Branch field can be a dropdown.

    Query: ``?repo=owner/name`` (defaults to the tenant's connected repo).
    Falls back to a static list of common branches if GitHub can't be reached,
    so the dropdown always has sensible options.
    """
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import _github_repo_branches
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    fresh = await db_module.get_tenant_by_id(request.app.state.db, tenant["id"])
    project = await db_module.get_project(await _db(request), project_id)
    repo = (request.query_params.get("repo") or (project or {}).get("github_repo") or "").strip()
    default_branch = (project or {}).get("github_branch") or "main"
    token = db_module.decrypt_field((fresh or {}).get("github_pat")) if fresh else None
    branches: list[str] = []
    source = "fallback"
    if token and repo and "/" in repo:
        try:
            branches = await _github_repo_branches(token, repo)
            source = "github"
        except Exception:  # noqa: BLE001
            branches = []
    if not branches:
        # Merge the saved branch + common defaults, preserving order, no dupes.
        seen: set[str] = set()
        for b in (default_branch, *_FALLBACK_BRANCHES):
            if b and b not in seen:
                seen.add(b)
                branches.append(b)
    return {"repo": repo, "branches": branches, "default_branch": default_branch, "source": source}


@app.get("/mcp/quickstart", response_class=PlainTextResponse)
async def mcp_quickstart() -> str:
    """One-page MCP quick reference — the 5 tools you use 90% of the time.

    Returns plain text cheat sheet, suitable for pasting into a new chat session
    or displaying in the dashboard Settings tab.
    """
    tool_count = len(_MCP_TOOLS_LIST)
    return f"""\
# Meridian MCP — Quick Reference

The 5 tools you use 90% of the time:

| Tool | One-liner | Example |
|------|-----------|---------|
| start_session | Register session, get full project context | start_session(project_id="abc-123", session_name="feature-x") |
| log_task | Record completed work | log_task(session_id="sid", project_id="abc-123", description="Fixed OAuth redirect") |
| checkpoint | Snapshot: auto-capture + delta handoff + next /goal | checkpoint(session_id="sid", project_id="abc-123") |
| pin_decision | Add to live constitution | pin_decision(project_id="abc-123", title="Use psycopg3", body="asyncpg has DLL issues on Windows", category="TECHNICAL") |
| request_hitl | Surface blocking question to human | request_hitl(project_id="abc-123", question="Rate-limit per IP or per token?", urgency="blocking") |

## Session lifecycle

  list_projects()        ← first, if you don't know the project_id
  start_session()       ← always first
  log_task()            ← after any meaningful work
  pin_decision()        ← for architectural choices
  request_hitl()        ← when you need a human call
  checkpoint()          ← before ending / before context fills

## Auto-hooks (recommended)

Wire Claude Code / Codex to call these automatically:

  Mac/Linux:  curl -fsSL https://usemeridian.us/hooks.sh | bash
  Windows:    irm https://usemeridian.us/hooks.ps1 | iex

## Full tool reference

  GET /mcp/tools-doc     — complete markdown reference ({tool_count} tools)
  https://docs.usemeridian.us
"""


@app.get("/mcp/tools-doc", response_class=PlainTextResponse)
async def mcp_tools_doc() -> str:
    """Generate organized markdown MCP tool reference."""
    tool_map = {t["name"]: t for t in _MCP_TOOLS_LIST}

    def _render_tool(name: str, override_desc: str | None = None) -> list[str]:
        tool = tool_map.get(name)
        if not tool:
            return []
        out = [f"\n### `{name}`\n"]
        desc = override_desc or tool.get("description", "")
        out.append(f"{desc}\n")
        props = (tool.get("inputSchema") or {}).get("properties") or {}
        required = set((tool.get("inputSchema") or {}).get("required") or [])
        if props:
            out.append("\n| Parameter | Type | Required | Description |")
            out.append("|-----------|------|----------|-------------|")
            for k, v in props.items():
                req = "required" if k in required else "optional"
                desc_col = (v.get("description") or "").replace("|", "\\|")
                out.append(f"| `{k}` | {v.get('type', 'string')} | {req} | {desc_col} |")
        example = _TOOL_EXAMPLES.get(name)
        if example:
            out.append(f"\n**Example:**\n```\n{example}\n```")
        out.append("")
        out.append("---")
        out.append("")
        return out

    n = len(_MCP_TOOLS_LIST)
    lines: list[str] = [
        "# MCP Tool Reference\n",
        "\n",
        f"Meridian exposes **{n} tools** over MCP.\n",
        "\n",
        "They fall into two usage patterns:\n",
        "\n",
        "- **Planner sessions** (claude.ai, planning work) - `start_session` · `pin_decision` · `update_decision` · `add_note` · `get_context_block` · `generate_handoff`\n",
        "- **Executor sessions** (Claude Code, Cursor, automated workers) - `start_session` · `log_task` · `request_hitl` · `get_session_brief` · `generate_handoff`\n",
        "\n",
        "---\n",
        "\n",
        "## Quick Reference - 5 tools you use 90% of the time\n",
        "\n",
        "| Tool | One-liner | Example call |\n",
        "|------|-----------|-------------|\n",
        "| `start_session` | Register session, get full project context | `start_session(project_id=\"abc-123\", session_name=\"feature-x\", human_id=\"alice\")` |\n",
        "| `log_task` | Record completed work to the shared task log | `log_task(session_id=\"sid\", project_id=\"abc-123\", description=\"Wired OAuth redirect\")` |\n",
        "| `checkpoint` | Snapshot progress: auto-capture + delta handoff + next /goal | `checkpoint(session_id=\"sid\", project_id=\"abc-123\")` |\n",
        "| `pin_decision` | Add an architectural decision to the live constitution | `pin_decision(project_id=\"abc-123\", title=\"Use psycopg3\", body=\"asyncpg has DLL issues on Windows\", category=\"TECHNICAL\")` |\n",
        "| `request_hitl` | Surface a blocking question to the human queue | `request_hitl(project_id=\"abc-123\", question=\"Should we rate-limit per IP or per token?\", urgency=\"blocking\")` |\n",
        "\n> **Tip:** Use `checkpoint()` instead of `generate_handoff()` when ending a session — it also runs `auto_capture` and returns the next `/goal` string.\n",
        "\n---\n",
        "## Starting a session\n",
    ]
    lines += _render_tool("start_session",
        "Register a session and get the full project context (goal, sprint, recent tasks, decisions) in one call. "
        "**Use this instead of `register_session`.**")
    lines += _render_tool("get_session_brief",
        "Read-only: Call this FIRST for project summaries or to see what a session did — returns session, tasks, "
        "decisions, and recent commits in one call. Compact session orientation (<500 tokens): sprint focus, "
        "pending items, recent tasks, blocking failures, and open HITL requests. Ideal for worker/automation "
        "sessions that don't need the full context.")
    lines += ["## Tasks\n"]
    lines += _render_tool("log_task",
        "Log what this session did, is doing, or failed at. Call frequently — this is the primary signal in the "
        "timeline and handoffs.\n\n"
        "Valid statuses: `pending` · `in_progress` · `done` · `failed` · `backlog` · `future` · `backburner`")
    lines += _render_tool("get_tasks")
    lines += _render_tool("search_tasks")
    lines += ["## Goal & sprint\n"]
    lines += _render_tool("get_goal")
    lines += _render_tool("set_goal")
    lines += ["## Executor config & file coordination\n"]
    lines += _render_tool("set_executor_config",
        "Store project-level executor defaults so worker sessions start with repo path, env file, test command, "
        "deploy command, shell, branch, and the injected credentials rule.")
    lines += _render_tool("claim_file",
        "Claim exclusive edit rights on a file path for this session. Locks auto-expire after 2 hours.")
    lines += _render_tool("release_file",
        "Release a file lock held by this session when you're done editing.")
    lines += _render_tool("idle_until_session_done",
        "Read-only: Wait on another session before touching a shared file. The tool polls every 30 seconds until the watched session is done.")
    lines += ["## Decisions\n"]
    lines += _render_tool("pin_decision",
        "Record an authoritative decision that supersedes earlier statements. Pinned decisions appear in every "
        "session's context block.\n\n"
        "Categories: `STRATEGIC` · `COMPETITIVE` · `TECHNICAL` · `TACTICAL` · `BUSINESS` · `PRODUCT` · `ARCHITECTURAL`")
    lines += _render_tool("update_decision",
        "Patch a pinned decision. Pass `new_title` + `new_body` to atomically supersede (creates a new row, marks "
        "old as superseded). Otherwise patches in place.")
    lines += _render_tool("get_pinned_decisions")
    lines += ["## Human-in-the-loop (HITL)\n"]
    lines += _render_tool("request_hitl",
        "Surface a question to the human queue. `urgency='blocking'` pauses the session until answered — poll "
        "`get_hitl_request` to resume. `normal`/`high` land in the dashboard without blocking.")
    lines += _render_tool("get_hitl_request",
        "Read-only: Poll a HITL request for the human's answer. Returns the row including `status` "
        "(`pending`/`answered`/`dismissed`) and `answer` text.")
    lines += ["## Handoff & context\n"]
    lines += _render_tool("generate_handoff",
        "Read-only: Generate a context handoff document. `mode='full'` writes the complete L0/L1/L2 handoff. `mode='delta'` "
        "returns a compact session summary with completed items, pending items, and the next `/goal` string.")
    lines += _render_tool("get_context_block",
        "Read-only: Return a compact plain-text context block (north star, sprint, pending sprint items, recent tasks, recent "
        "decisions, active sessions). Use `mode='full'` to paste into a fresh Claude Code session; `mode='chat'` "
        "for a shorter paste into claude.ai.")
    lines += ["## Notes\n"]
    lines += _render_tool("add_note",
        "Add a per-project wiki note. Use for setup instructions, gotchas, environment details, how-tos.")
    lines += _render_tool("get_notes", "Read-only: List project notes (newest first). Filter by tag substring.")
    lines += _render_tool("delete_note")
    lines += ["## Projects\n"]
    lines += _render_tool("create_project")
    lines += ["## Legacy\n"]
    lines += _render_tool("register_session",
        "!!! note \"Deprecated\"\n    Use `start_session` instead — it registers the session **and** returns "
        "goal + context in one call.")
    # strip trailing ---
    while lines and lines[-1].strip() in ("---", ""):
        lines.pop()
    return "\n".join(part.rstrip("\n") for part in lines).rstrip() + "\n"

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


def _hook_script_path(filename: str) -> Path:
    return Path(__file__).parent.parent / filename


def _mask_api_token_hash(token_hash: str | None) -> str:
    """Return a stable masked display string for a stored API token hash.
    Shows only last 4 chars of the hash as a stable identifier."""
    value = (token_hash or "").strip()
    if len(value) >= 4:
        return f"sk_meridian_••••••••{value[-4:]}"
    return "sk_meridian_••••••••..."


async def _get_authenticated_tenant(request: Request) -> dict[str, Any]:
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


def _watcher_script_path(filename: str) -> Path:
    return Path(__file__).parent.parent / "scripts" / filename


@app.get("/install_watcher.ps1")
async def get_install_watcher_ps1() -> PlainTextResponse:
    """a7c43cc1 — serve the claude --rc FileSystemWatcher installer for Windows."""
    script_path = _watcher_script_path("install_watcher.ps1")
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="install_watcher.ps1 not found")
    return PlainTextResponse(script_path.read_text(encoding="utf-8"))


@app.get("/install_watcher.sh")
async def get_install_watcher_sh() -> PlainTextResponse:
    """a7c43cc1 — serve the claude --rc FSEvents/inotify installer for macOS/Linux."""
    script_path = _watcher_script_path("install_watcher.sh")
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="install_watcher.sh not found")
    return PlainTextResponse(script_path.read_text(encoding="utf-8"))


@app.get("/hooks.ps1")
async def get_hooks_ps1() -> PlainTextResponse:
    script_path = _hook_script_path("hooks.ps1")
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="hooks.ps1 not found")
    return PlainTextResponse(script_path.read_text(encoding="utf-8"))


@app.get("/hooks.sh")
async def get_hooks_sh() -> PlainTextResponse:
    script_path = _hook_script_path("hooks.sh")
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="hooks.sh not found")
    return PlainTextResponse(script_path.read_text(encoding="utf-8"))


@app.get("/install.sh")
async def get_install_sh() -> PlainTextResponse:
    script_path = _hook_script_path("install.sh")
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="install.sh not found")
    return PlainTextResponse(script_path.read_text(encoding="utf-8"))


@app.get("/install.ps1")
async def get_install_ps1() -> PlainTextResponse:
    script_path = _hook_script_path("install.ps1")
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="install.ps1 not found")
    return PlainTextResponse(script_path.read_text(encoding="utf-8"))


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
# Token-based OAuth hooks — browser connect + machine registry (2da12762)
# ---------------------------------------------------------------------------


@app.get("/auth/hooks-connect")
async def hooks_connect(request: Request, hostname: str = "") -> Any:
    """Browser endpoint: register THIS machine to the logged-in tenant and show
    the registration_token. Redirects to login when there is no session."""
    from fastapi.responses import HTMLResponse, RedirectResponse
    from urllib.parse import quote
    hostname = (hostname or "").strip()
    try:
        tenant = await _get_authenticated_tenant(request)
    except HTTPException:
        nxt = quote(str(request.url), safe="")
        return RedirectResponse(url=f"/auth/login?next={nxt}", status_code=303)
    if not hostname:
        raise HTTPException(status_code=400, detail="hostname required")
    auth_db = request.app.state.db
    token = await db_module.register_hostname(auth_db, tenant["id"], hostname)
    safe_host = html_module.escape(hostname)
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Machine connected — Meridian</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<style>body{font-family:system-ui,sans-serif;max-width:640px;margin:64px auto;"
        "padding:0 20px;color:#1a1a2e}code{background:#f0f0f5;padding:2px 6px;border-radius:4px}"
        ".tok{display:block;background:#0f1020;color:#7cf;padding:14px;border-radius:8px;"
        "font-family:ui-monospace,monospace;word-break:break-all;margin:16px 0}"
        ".ok{color:#1a9c4a;font-weight:600}</style></head><body>"
        f"<h1>✅ <span class='ok'>{safe_host}</span> connected</h1>"
        "<p>This machine is now registered for Meridian session hooks. The hook "
        "script uses the registration token below — no API token needed.</p>"
        f"<div class='tok'>{html_module.escape(token)}</div>"
        "<p>You can close this tab. Manage or revoke machines anytime under "
        "<strong>Settings → Known Machines</strong>.</p>"
        "</body></html>"
    )
    return HTMLResponse(body)


@app.get("/auth/hooks-status")
async def hooks_status(request: Request, hostname: str = "") -> dict[str, Any]:
    """Return {registered, token} for the logged-in tenant's hostname. The token
    is echoed only to the authenticated owner so the installer can finish wiring
    the hook script after the browser connect."""
    tenant = await _get_authenticated_tenant(request)
    auth_db = request.app.state.db
    return await db_module.get_hostname_status(
        auth_db, tenant["id"], (hostname or "").strip()
    )


@app.get("/projects/{project_id}/registered-machines")
async def list_registered_machines(project_id: str, request: Request) -> list[dict[str, Any]]:
    """List the tenant's registered hook machines (token omitted) for the
    Settings → Known Machines panel. Registry is per-tenant; project_id only
    scopes the dashboard route."""
    tenant = await _get_authenticated_tenant(request)
    auth_db = request.app.state.db
    return await db_module.list_registered_hostnames(auth_db, tenant["id"])


@app.delete("/projects/{project_id}/registered-machines/{machine_id}", status_code=204)
async def revoke_registered_machine(
    project_id: str, machine_id: str, request: Request
) -> Response:
    """Revoke one of the tenant's registered machines."""
    tenant = await _get_authenticated_tenant(request)
    auth_db = request.app.state.db
    ok = await db_module.revoke_registered_hostname(auth_db, tenant["id"], machine_id)
    if not ok:
        raise HTTPException(status_code=404, detail="machine not found")
    return Response(status_code=204)


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


async def _block_non_admin_connection_writes(request: Request) -> None:
    """G1.9 — connection profiles live in the hosted server's meridian.toml.
    Non-admin tenants must not be able to mutate them. Returns 403 cleanly
    instead of the surprising 404 when, e.g., the dashboard tried to
    activate a connection name that doesn't exist in the toml at all.
    """
    if not _hosted_mode():
        return
    from .hosted import get_current_tenant, is_admin_db  # noqa: PLC0415
    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        raise HTTPException(403, "Sign in to manage connections")
    if not await is_admin_db(tenant.get("email", ""), request.app.state.db):
        raise HTTPException(
            403, "Connection profiles are admin-only on the hosted service"
        )


@app.post("/config/connections")
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



@app.delete("/config/connections/{name}")
async def delete_connection(name: str, request: Request) -> dict[str, Any]:
    """v1.9.x — remove a named connection profile from meridian.toml.

    Hosted non-admin tenants get 403 (see _block_non_admin_connection_writes).
    """
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
# Waitlist
# ---------------------------------------------------------------------------


@app.post("/waitlist", status_code=201)
async def join_waitlist(request: Request) -> dict[str, Any]:
    """POST {"email": "...", "note": "..."} — add to hosted-tier waitlist.

    Returns the created entry. 409 on duplicate email.
    """
    body = await request.json()
    email = (body.get("email") or "").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="valid email required")
    plan = (body.get("plan") or "standard").strip()
    source = (body.get("source") or "landing").strip()
    note_parts = []
    if body.get("note"):
        note_parts.append(body["note"].strip())
    note_parts.append(f"plan:{plan} source:{source}")
    note = " ".join(note_parts) if note_parts else None
    db = await _db(request)
    try:
        entry = await db_module.add_waitlist_entry(db, email, note)
    except Exception as exc:
        if "UNIQUE" in str(exc) or "unique" in str(exc):
            raise HTTPException(status_code=409, detail="email already on waitlist")
        raise
    # Fire-and-forget confirmation email — never block the response
    if _hosted_mode():
        try:
            from .hosted import send_waitlist_confirmation_email  # noqa: PLC0415
            asyncio.create_task(send_waitlist_confirmation_email(email))
        except Exception:
            pass
    return entry


@app.get("/waitlist")
async def list_waitlist(request: Request) -> list[dict[str, Any]]:
    """GET all waitlist entries, newest first. Admin use only."""
    db = request.app.state.db
    return await db_module.get_waitlist(db)


@app.get("/admin/waitlist", response_class=HTMLResponse)
async def admin_waitlist_page(request: Request) -> HTMLResponse:
    """Admin waitlist management page — shows signups, tenant stats, approve/delete buttons."""
    from .hosted import get_current_tenant, is_admin_db  # noqa: PLC0415

    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        return HTMLResponse("<h1>403</h1><p>Not authenticated.</p>", status_code=403)
    if not await is_admin_db(tenant.get("email", ""), request.app.state.db):
        return HTMLResponse("<h1>403</h1><p>Admin only.</p>", status_code=403)

    db = request.app.state.db
    entries = await db_module.get_waitlist(db)

    async def _count(sql: str) -> int:
        async with db.execute(sql) as cur:
            row = await cur.fetchone()
        return (row[0] if row else 0) or 0

    total_tenants = await _count("SELECT COUNT(*) FROM tenants")
    free_tenants = await _count("SELECT COUNT(*) FROM tenants WHERE plan='free'")
    paid_tenants = await _count("SELECT COUNT(*) FROM tenants WHERE plan NOT IN ('free','') AND plan IS NOT NULL")

    rows_html = "".join(
        f"""<tr>
          <td style="padding:6px 10px;border-bottom:1px solid #2a2d35">{html_module.escape(e.get("email",""))}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #2a2d35;color:#8b8fa8;font-size:11px">{html_module.escape((e.get("created_at") or "")[:16])}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #2a2d35;color:#8b8fa8;font-size:11px">{html_module.escape(e.get("note") or "")}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #2a2d35">
            <button onclick="delWL('{html_module.escape(e.get('id',''))}',this)" style="background:#2a0f0f;border:1px solid #5a1a1a;color:#e05252;border-radius:3px;padding:2px 8px;font-size:10px;cursor:pointer">Delete</button>
          </td>
        </tr>"""
        for e in entries
    ) or "<tr><td colspan='4' style='padding:16px;text-align:center;color:#8b8fa8'>No waitlist entries.</td></tr>"

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Admin — Waitlist — Meridian</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d0d0f;color:#e8eaf0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:32px 24px}}
h1{{font-size:1.4rem;margin-bottom:8px}}nav a{{color:#6c8fff;text-decoration:none;font-size:13px;margin-right:16px}}
.stats{{display:flex;gap:16px;margin:20px 0}}
.stat{{background:#16181c;border:1px solid #2a2d35;border-radius:8px;padding:12px 18px;min-width:120px}}
.stat .n{{font-size:1.6rem;font-weight:700;color:#6c8fff}}.stat .l{{font-size:11px;color:#8b8fa8;margin-top:2px}}
table{{width:100%;border-collapse:collapse;background:#16181c;border:1px solid #2a2d35;border-radius:8px;overflow:hidden;margin-top:16px}}
th{{padding:8px 10px;text-align:left;background:#1e2029;font-size:11px;color:#8b8fa8;border-bottom:1px solid #2a2d35}}
tr:hover td{{background:#1a1c23}}
</style>
</head>
<body>
<nav><a href="/dashboard">← Dashboard</a> <a href="/admin/health">Health</a></nav>
<h1 style="margin-top:16px">Waitlist Management</h1>
<p style="color:#8b8fa8;font-size:13px;margin-top:4px">{len(entries)} total signup{"s" if len(entries)!=1 else ""}</p>
<div class="stats">
  <div class="stat"><div class="n">{len(entries)}</div><div class="l">Waitlist</div></div>
  <div class="stat"><div class="n">{total_tenants}</div><div class="l">Total Tenants</div></div>
  <div class="stat"><div class="n">{free_tenants}</div><div class="l">Free Plan</div></div>
  <div class="stat"><div class="n">{paid_tenants}</div><div class="l">Paid Plan</div></div>
</div>
<table>
<thead><tr><th>Email</th><th>Signed Up</th><th>Note</th><th>Action</th></tr></thead>
<tbody id="wl-body">{rows_html}</tbody>
</table>
<script>
async function delWL(id, btn) {{
  if (!confirm('Delete this waitlist entry?')) return;
  const r = await fetch('/admin/waitlist/' + id, {{method:'DELETE'}});
  if (r.ok) {{ btn.closest('tr').remove(); }} else {{ alert('Failed: ' + r.status); }}
}}
</script>
</body></html>"""
    return HTMLResponse(html)


@app.delete("/admin/waitlist/{entry_id}")
async def admin_delete_waitlist_entry(entry_id: str, request: Request) -> dict[str, Any]:
    """Delete a waitlist entry by id. Admin only."""
    from .hosted import get_current_tenant, is_admin_db  # noqa: PLC0415

    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        raise HTTPException(status_code=403, detail="not authenticated")
    if not await is_admin_db(tenant.get("email", ""), request.app.state.db):
        raise HTTPException(status_code=403, detail="admin only")
    db = request.app.state.db
    await db.execute("DELETE FROM waitlist WHERE id = ?", (entry_id,))
    await db.commit()
    return {"deleted": True, "id": entry_id}


@app.get("/waitlist-pending")
async def waitlist_pending(request: Request) -> HTMLResponse:
    """Landing page for non-admin users who sign in during pre-launch."""
    message = (request.query_params.get("message") or "").strip()
    badge = "Early access is full" if message else "✓ You're on the list"
    heading = "You're on the waitlist" if message else "Thanks for signing up!"
    body = (
        message
        if message
        else "Meridian is in early access. We'll email you when your account is ready."
    )
    html = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>You're on the waitlist — Meridian</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d0f;color:#e8eaf0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#16181c;border:1px solid #2a2d35;border-radius:12px;padding:44px 40px;
  max-width:480px;width:100%;margin:20px;text-align:center}
.logo{font-size:1.3rem;font-weight:700;color:#e8eaf0;margin-bottom:24px}
.logo span{color:#6c8fff}
h1{font-size:1.5rem;font-weight:700;margin-bottom:12px}
p{color:#8b8fa8;font-size:.9rem;line-height:1.6;margin-bottom:16px}
.badge{display:inline-block;background:#1e2029;border:1px solid #2a2d35;border-radius:20px;
  padding:6px 16px;font-size:.8rem;color:#6c8fff;margin-bottom:24px}
a{color:#6c8fff;text-decoration:none}
a:hover{text-decoration:underline}
</style>
</head>
<body>
<div class="card">
  <div class="logo">⬡ <span>Meridian</span></div>
  <div class="badge">__BADGE__</div>
  <h1>__HEADING__</h1>
  <p>__BODY__</p>
  <p>In the meantime, explore the live demo or read the docs.</p>
  <div style="display:flex;gap:10px;justify-content:center;margin-top:20px;flex-wrap:wrap">
    <a href="/" style="display:inline-block;background:#1a1c23;border:1px solid #2a2d35;border-radius:8px;padding:9px 18px;color:#e8eaf0;font-size:.85rem;text-decoration:none">← Back to home</a>
    <a href="/demo" style="display:inline-block;background:#7c3aed;border:none;border-radius:8px;padding:9px 18px;color:#fff;font-size:.85rem;text-decoration:none">→ Try the live demo</a>
    <a href="https://docs.usemeridian.us" target="_blank" style="display:inline-block;background:#1a1c23;border:1px solid #2a2d35;border-radius:8px;padding:9px 18px;color:#e8eaf0;font-size:.85rem;text-decoration:none">Read the docs</a>
  </div>
  <p style="margin-top:24px;font-size:.78rem"><a href="/auth/logout">sign out</a></p>
</div>
</body>
</html>"""
    html = (
        html.replace("__BADGE__", badge)
        .replace("__HEADING__", heading)
        .replace("__BODY__", body)
    )
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# v1.0 — Stripe Checkout (API-based, plan-aware)
# ---------------------------------------------------------------------------


@app.get("/billing/portal")
async def billing_portal_redirect(request: Request):
    """G2.11 — open a Stripe Customer Portal session for the signed-in tenant.

    Routes to /pricing when the tenant has no stripe_customer_id (free tier
    or trial), to /auth/login when not signed in, and to the Stripe-hosted
    portal otherwise.
    """
    from fastapi.responses import RedirectResponse  # noqa: PLC0415
    from .hosted import create_stripe_billing_portal_session, get_current_tenant  # noqa: PLC0415

    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        return RedirectResponse("/auth/login?next=/billing/portal", status_code=302)

    try:
        url = await create_stripe_billing_portal_session(tenant)
    except ValueError:
        # No stripe_customer_id yet — direct the user to subscribe instead.
        return RedirectResponse("/pricing", status_code=302)
    except RuntimeError:
        # Stripe not configured (local dev) — fall through to pricing.
        return RedirectResponse("/pricing", status_code=302)

    return RedirectResponse(url, status_code=302)


@app.post("/billing/portal")
async def billing_portal_json(request: Request) -> dict[str, str]:
    """Return Stripe billing portal URL as JSON for dashboard AJAX calls (e7d4400b)."""
    from .hosted import create_stripe_billing_portal_session, get_current_tenant  # noqa: PLC0415

    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not tenant.get("stripe_customer_id"):
        raise HTTPException(status_code=404, detail="No billing account")
    try:
        url = await create_stripe_billing_portal_session(tenant)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"url": url}


@app.get("/checkout")
async def checkout_redirect(request: Request, plan: str = "standard") -> RedirectResponse:
    """Create a Stripe Checkout Session and redirect to it.

    Requires an active session cookie. ``plan`` must be ``standard`` or ``pro``.
    Falls back to the payment link if Stripe API is not configured.
    """
    from .hosted import create_stripe_checkout_session, get_current_tenant

    if plan not in ("standard", "pro"):
        raise HTTPException(status_code=400, detail="plan must be standard or pro")

    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        return RedirectResponse(f"/auth/login?next=/checkout%3Fplan%3D{plan}", status_code=302)

    try:
        url = await create_stripe_checkout_session(tenant, plan)
    except RuntimeError:
        # Stripe not configured — fall back to payment link
        fallback = os.environ.get("STRIPE_PAYMENT_LINK", "/auth/login")
        return RedirectResponse(fallback, status_code=302)

    return RedirectResponse(url, status_code=302)


# ---------------------------------------------------------------------------
# v2.0 — Stripe webhook
# ---------------------------------------------------------------------------


@app.post("/webhooks/stripe", status_code=200)
async def stripe_webhook(request: Request) -> dict[str, str]:
    """Handle Stripe webhook events.

    Verifies the ``Stripe-Signature`` header against ``STRIPE_WEBHOOK_SECRET``.
    On ``checkout.session.completed`` or ``invoice.paid``:
      1. Upserts the tenant by email.
      2. Provisions a Neon DB if not already done.
      3. Creates an API bearer token and sends a welcome email.

    Returns ``{"status": "ok"}`` on success or if the event type is ignored.
    Returns 400 on signature verification failure.
    """
    import hmac as _hmac
    import hashlib as _hashlib
    import time as _time

    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    raw_body = await request.body()
    sig_header = request.headers.get("Stripe-Signature", "")

    # Verify signature if secret is configured
    if webhook_secret:
        try:
            _verify_stripe_signature(raw_body, sig_header, webhook_secret)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        event = json.loads(raw_body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc

    event_type = event.get("type", "")
    _HANDLED = {
        "checkout.session.completed",
        "invoice.paid",
        "customer.subscription.created",
        "invoice.payment_failed",
        "customer.subscription.past_due",
    }
    if event_type not in _HANDLED:
        return {"status": "ignored"}

    event_obj = event.get("data", {}).get("object", {})
    email = (
        event_obj.get("customer_email")
        or event_obj.get("customer_details", {}).get("email")
        or ""
    )
    stripe_customer_id = event_obj.get("customer", "")

    db = request.app.state.db

    # Dunning: payment failed → stamp payment_failed_at and return early.
    if event_type in ("invoice.payment_failed", "customer.subscription.past_due"):
        if stripe_customer_id:
            tenant = await db_module.get_tenant_by_stripe_customer(db, stripe_customer_id)
            if tenant and not tenant.get("payment_failed_at"):
                from datetime import datetime, timezone as _tz
                ts = datetime.now(_tz.utc).isoformat()
                await db_module.update_tenant(db, tenant["id"], payment_failed_at=ts, dunning_email_sent=0)
        return {"status": "dunning_started"}

    if not email:
        return {"status": "no_email"}

    # Resolve plan from checkout metadata (standard or pro); default to standard
    plan = event_obj.get("metadata", {}).get("plan", "standard")
    if plan not in ("standard", "pro"):
        plan = "standard"

    tenant = await db_module.upsert_tenant(db, email=email)
    if stripe_customer_id:
        tenant = await db_module.update_tenant(
            db, tenant["id"], stripe_customer_id=stripe_customer_id, plan=plan
        )
        # Payment recovered — clear any dunning state
        if tenant and tenant.get("payment_failed_at"):
            tenant = await db_module.update_tenant(
                db, tenant["id"], payment_failed_at=None, dunning_email_sent=0
            )

    # Extract metered subscription item ID when overage price is configured.
    # Best-effort — never block provisioning if this fails.
    from .hosted import STRIPE_OVERAGE_PRICE_ID as _OVERAGE_PRICE_ID
    subscription_id = event_obj.get("subscription")
    if subscription_id and _OVERAGE_PRICE_ID and stripe_customer_id:
        try:
            import stripe as _stripe
            _stripe.api_key = os.environ.get("STRIPE_API_KEY", "")
            sub = _stripe.Subscription.retrieve(subscription_id)
            metered_item_id = next(
                (i.id for i in sub.items.data if i.price.id == _OVERAGE_PRICE_ID),
                None,
            )
            if metered_item_id:
                await db_module.update_tenant(
                    db, tenant["id"], stripe_metered_item_id=metered_item_id
                )
        except Exception:  # noqa: BLE001
            pass

    # Capacity check before provisioning
    from .hosted import check_capacity, provision_neon_db, send_welcome_email
    try:
        await check_capacity(db)
    except RuntimeError as cap_exc:
        import logging
        logging.getLogger(__name__).error("Capacity exceeded: %s", cap_exc)
        return {"status": "capacity_exceeded"}

    # Provision Neon DB
    try:
        tenant = await provision_neon_db(tenant["id"], db)
    except Exception as exc:
        # Log but don't fail the webhook — Stripe will retry
        import logging
        logging.getLogger(__name__).error("Neon provisioning failed for %s: %s", email, exc)
        return {"status": "provisioning_queued"}

    # Create API token + send welcome email
    raw_token, _token_row = await db_module.create_api_token(db, tenant["id"], label="welcome")
    try:
        await send_welcome_email(email, raw_token, tenant)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Welcome email failed for %s: %s", email, exc)

    return {"status": "ok"}


def _verify_stripe_signature(raw_body: bytes, sig_header: str, secret: str) -> None:
    """Verify a Stripe webhook signature. Raises ValueError on failure."""
    import hmac
    import hashlib
    import time

    parts = {k: v for part in sig_header.split(",") for k, v in [part.split("=", 1)] if "=" in part}
    timestamp = parts.get("t", "")
    sig = parts.get("v1", "")
    if not timestamp or not sig:
        raise ValueError("missing signature components")

    try:
        ts = int(timestamp)
    except ValueError:
        raise ValueError("invalid timestamp")

    tolerance = 300  # 5 minutes
    if abs(time.time() - ts) > tolerance:
        raise ValueError("webhook timestamp too old")

    payload = f"{timestamp}.{raw_body.decode()}"
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise ValueError("signature mismatch")


# ---------------------------------------------------------------------------
# v2.0 — Bearer token management
# ---------------------------------------------------------------------------


@app.post("/auth/tokens", status_code=201)
async def create_api_token(request: Request) -> dict[str, Any]:
    """Generate a new API bearer token for the authenticated tenant.

    Requires a valid session cookie (browser flow) or existing bearer token.
    Returns ``{"token": "sk_meridian_...", "id": "...", "label": "..."}``
    where ``token`` is shown exactly once and never stored in plain text.
    """
    tenant = await _get_authenticated_tenant(request)

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    label = (body.get("label") or "").strip() or None
    token_type = body.get("token_type") or "readwrite"
    if token_type not in ("readwrite", "readonly"):
        token_type = "readwrite"
    db = request.app.state.db
    # If a label is provided, delete any existing token with the same label first
    # so the label acts as a unique slot (prevents token accumulation + revocation loops)
    if label:
        await db_module.delete_api_tokens_by_label(db, tenant["id"], label)
    raw_token, token_row = await db_module.create_api_token(db, tenant["id"], label, token_type=token_type)
    return {
        "token": raw_token,
        "id": token_row["id"],
        "label": token_row["label"],
        "token_type": token_row.get("token_type", "readwrite"),
        "created_at": token_row["created_at"],
    }


@app.get("/auth/tokens")
async def list_api_tokens(request: Request) -> list[dict[str, Any]]:
    """List API bearer tokens for the authenticated tenant."""
    tenant = await _get_authenticated_tenant(request)
    db = request.app.state.db
    tokens = await db_module.list_api_tokens(db, tenant["id"])
    return [
        {
            "id": token["id"],
            "label": token.get("label"),
            "token_type": token.get("token_type") or "readwrite",
            "created_at": token.get("created_at"),
            "masked_token": _mask_api_token_hash(token.get("token_hash")),
        }
        for token in tokens
    ]


@app.delete("/auth/tokens/{token_id}", status_code=204)
async def delete_api_token(token_id: str, request: Request) -> Response:
    """Revoke an API bearer token for the authenticated tenant."""
    tenant = await _get_authenticated_tenant(request)
    db = request.app.state.db
    deleted = await db_module.delete_api_token(db, tenant["id"], token_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="token not found")
    return Response(status_code=204)


@app.get("/auth/me")
async def get_me(request: Request) -> dict[str, Any]:
    """Return the authenticated tenant's profile (session cookie or bearer), including projects."""
    tenant = await _get_authenticated_tenant(request)
    safe = {k: v for k, v in tenant.items() if k not in ("neon_db_url", "github_pat")}
    safe["github_connected"] = bool(tenant.get("github_pat"))
    try:
        project_db = await _open_tenant_db_by_id(request, tenant["id"])
        projects = await db_module.list_project_summaries(project_db)
        safe["projects"] = [
            {"id": p["id"], "name": p["name"]}
            for p in (projects or [])
        ]
    except Exception:
        safe["projects"] = []
    return safe


@app.get("/auth/install", response_class=HTMLResponse)
async def auth_install_page(request: Request) -> HTMLResponse:
    """One-time install token page — requires browser session, returns a short-lived token."""
    if not _hosted_mode():
        raise HTTPException(status_code=404, detail="not available in self-hosted mode")
    from datetime import datetime, timezone, timedelta
    tenant = await _get_authenticated_tenant(request)
    db = request.app.state.db
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    raw_token, _ = await db_module.create_api_token(
        db, tenant["id"], label="install", expires_at=expires_at
    )
    email = tenant.get("email", "")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Meridian Connect</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#x1F9ED;</text></svg>">
  <style>
    body{{font-family:system-ui,sans-serif;background:#0d0d0d;color:#e8e8e8;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
    .card{{background:#1a1a1a;border:1px solid #2e2e2e;border-radius:12px;padding:32px;max-width:520px;width:100%}}
    h2{{margin:0 0 8px;font-size:20px}}
    p{{color:#888;margin:0 0 20px;font-size:14px}}
    .token-box{{font-family:monospace;font-size:13px;background:#0d0d0d;border:1px solid #333;border-radius:6px;padding:14px;word-break:break-all;user-select:all;cursor:pointer;color:#7dd3fc;line-height:1.5}}
    .copy-btn{{margin-top:12px;padding:8px 20px;border-radius:6px;border:none;background:#3b82f6;color:#fff;cursor:pointer;font-size:13px;width:100%}}
    .copy-btn:active{{background:#2563eb}}
    .note{{font-size:12px;color:#555;margin-top:16px;line-height:1.5}}
    .email{{color:#888;font-size:12px;margin-bottom:20px}}
  </style>
</head>
<body>
  <div class="card">
    <h2>Meridian Connect</h2>
    <div class="email">Signed in as {email}</div>
    <p>Copy this token and paste it into the installer. It expires in 10 minutes and can only be used once.</p>
    <div style="position:relative;margin-bottom:12px">
      <div class="token-box" id="token" style="filter:blur(6px);transition:filter 0.2s;word-break:break-all;user-select:all">{raw_token}</div>
      <button onclick="toggleReveal()" id="revealBtn" style="position:absolute;top:50%;right:10px;transform:translateY(-50%);background:#222;border:1px solid #444;color:#aaa;border-radius:4px;padding:3px 10px;font-size:12px;cursor:pointer">Show</button>
    </div>
    <button class="copy-btn" onclick="copyToken()">Copy token</button>
    <div class="note">This token grants one-time installer access. Never share it — treat it like a password.</div>
  </div>
  <script>
    var _revealed = false;
    function toggleReveal() {{
      _revealed = !_revealed;
      document.getElementById('token').style.filter = _revealed ? 'none' : 'blur(6px)';
      document.getElementById('revealBtn').textContent = _revealed ? 'Hide' : 'Show';
    }}
    function copyToken() {{
      navigator.clipboard.writeText('{raw_token}').then(() => {{
        const btn = document.querySelector('.copy-btn');
        btn.textContent = '✓ Copied!';
        setTimeout(() => btn.textContent = 'Copy token', 2000);
      }});
    }}
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


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


def _github_tools_for_tenant(tenant: dict) -> list[dict[str, Any]]:
    """Return the 5 GitHub tool defs if the tenant has a GitHub PAT set."""
    if not db_module.decrypt_field(tenant.get("github_pat")):
        return []
    _pid_prop = {"project_id": {"type": "string", "description": "Project ID whose GitHub repo to use."}}
    return [
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
        status_filter = args.get("status", "pending")
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
        return await db_module.add_sprint_item(
            db, args["project_id"], args["version"], args["title"],
            group=args.get("group"),
            human_id=args.get("human_id"),
            depends_on=args.get("depends_on"),
            failure_mode=args.get("failure_mode"),
            milestone_type=args.get("milestone_type", "task"),
        )
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
        result = await db_module.set_sprint(db, args["project_id"], args["sprint"])
        await goal_md_module.sync_db_to_goal_md(db, args["project_id"])
        return result
    if name == "get_sprint_items":
        include_human = args.get("human", True)
        if isinstance(include_human, bool):
            pass
        else:
            include_human = str(include_human).lower() not in ("false", "0", "no")
        return await db_module.get_sprint_items(
            db, args["project_id"],
            status=args.get("status"),
            include_human=include_human,
        )
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

        item = await db_module.claim_sprint_item(db, args["project_id"], args["item_id"])
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
        hitl_rows = await db_module.list_hitl_requests(db, project_id, status="pending")
        sprint_items = await db_module.get_sprint_items(db, project_id, status="pending")
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
        # 277567dc — surface the actual pending HITL questions (not just a count)
        # so a session sees what needs a human decision without a second call.
        if hitl_rows:
            hitl_xml = (
                f'<hitl_pending count="{len(hitl_rows)}">\n'
                + "\n".join(
                    f'  <request id="{h.get("id","")}" urgency="{h.get("urgency","normal")}">'
                    f'{(h.get("question") or "")[:140]}</request>'
                    for h in hitl_rows[:5]
                )
                + "\n</hitl_pending>"
            )
        else:
            hitl_xml = ""
        blocking_xml = f'<blocking>{(blocking[0].get("description") or "")[:100]}</blocking>' if blocking else ""
        # v2.6 — include session scratch-pad notes at top of brief
        notes_xml = ""
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
        brief = (
            f'<session_brief project_id="{project_id}" role="{role}">\n'
            f'{notes_xml}'
            f'<sprint>{sprint_str[:200]}</sprint>\n'
            f'<pending_items>\n{sprint_items_xml}\n</pending_items>\n'
            f'<last_tasks>\n{tasks_xml}\n</last_tasks>\n'
            f'{blocking_xml}\n'
            f'{hitl_xml}\n'
            f'</session_brief>'
        )
        return {"text": brief, "project_id": project_id, "role": role}
    raise ValueError(f"unknown tool: {name}")


_MCP_RATE_LIMIT = "100/minute"

# ---------------------------------------------------------------------------
# 9768d806 — MCP SSE transport (for dnakov/claude-mcp Chrome extension)
# ---------------------------------------------------------------------------
# Protocol: GET /mcp/sse opens an SSE stream, receives "endpoint" event with
# the POST URL. Client POSTs JSON-RPC to POST /mcp/sse?session_id=<uuid> and
# reads the JSON response directly from the HTTP response body.

_SSE_SESSIONS: dict[str, dict[str, Any]] = {}  # session_id → {db, queue}

# ── OAuth 2.0 for claude.ai custom connector ──────────────────────────────
import secrets as _sec, hashlib as _hs, base64 as _b64, time as _tm, json as _json
from urllib.parse import urlencode as _ue
from fastapi.responses import RedirectResponse as _RR

_oa_clients: dict = {}
_oa_codes: dict = {}

# Tokens persisted to disk in local mode for backwards compatibility.
_OA_TOKEN_FILE = DEFAULT_DATA_DIR / "oauth_tokens.json"

def _oauth_token_hash(token: str) -> str:
    return _hs.sha256(token.encode()).hexdigest()


def _normalize_oa_tokens(tokens: dict[str, Any]) -> dict[str, dict[str, Any]]:
    now = int(_tm.time())
    normalized: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in (tokens or {}).items():
        if not isinstance(raw_value, dict):
            continue
        try:
            exp = int(raw_value.get("exp", 0))
        except (TypeError, ValueError):
            continue
        if exp <= now:
            continue
        token_hash = (
            raw_key
            if isinstance(raw_key, str)
            and len(raw_key) == 64
            and all(c in "0123456789abcdef" for c in raw_key.lower())
            else _oauth_token_hash(str(raw_key))
        )
        normalized[token_hash] = {
            "tenant_id": raw_value.get("tenant_id"),
            "client_id": raw_value.get("client_id"),
            "exp": exp,
        }
    return normalized


def _load_oa_tokens_file() -> dict[str, dict[str, Any]]:
    try:
        if _OA_TOKEN_FILE.exists():
            data = _json.loads(_OA_TOKEN_FILE.read_text())
            return _normalize_oa_tokens(data)
    except Exception:
        pass
    return {}

def _save_oa_tokens(tokens: dict) -> None:
    if _hosted_mode():
        return
    try:
        _OA_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _OA_TOKEN_FILE.write_text(_json.dumps(tokens))
    except Exception:
        pass

async def _ensure_oauth_token_table(db: Any) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS oauth_tokens (
            token_hash TEXT PRIMARY KEY,
            tenant_id TEXT,
            client_id TEXT,
            exp BIGINT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS oauth_clients (
            client_id TEXT PRIMARY KEY,
            client_secret TEXT NOT NULL,
            redirect_uris TEXT NOT NULL DEFAULT '[]',
            client_name TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS oauth_codes (
            code TEXT PRIMARY KEY,
            tenant_id TEXT,
            redirect_uri TEXT NOT NULL,
            code_challenge TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.commit()


async def _upsert_oauth_token(
    db: Any,
    token_hash: str,
    *,
    tenant_id: str | None,
    client_id: str,
    exp: int,
) -> None:
    await db.execute(
        "INSERT INTO oauth_tokens (token_hash, tenant_id, client_id, exp) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(token_hash) DO UPDATE SET "
        "tenant_id = excluded.tenant_id, "
        "client_id = excluded.client_id, "
        "exp = excluded.exp",
        (token_hash, tenant_id, client_id, exp),
    )
    await db.commit()


async def _get_oauth_token_from_db(
    db: Any,
    token_hash: str,
) -> dict[str, Any] | None:
    async with db.execute(
        "SELECT token_hash, tenant_id, client_id, exp "
        "FROM oauth_tokens WHERE token_hash = ? AND exp > ?",
        (token_hash, int(_tm.time())),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "tenant_id": row["tenant_id"],
        "client_id": row["client_id"],
        "exp": int(row["exp"]),
    }


async def _load_oauth_tokens_from_db(db: Any) -> dict[str, dict[str, Any]]:
    async with db.execute(
        "SELECT token_hash, tenant_id, client_id, exp "
        "FROM oauth_tokens WHERE exp > ?",
        (int(_tm.time()),),
    ) as cur:
        rows = await cur.fetchall()
    return {
        row["token_hash"]: {
            "tenant_id": row["tenant_id"],
            "client_id": row["client_id"],
            "exp": int(row["exp"]),
        }
        for row in rows
    }


async def _hydrate_oauth_cache(auth_db: Any) -> None:
    global _oa_tokens, _oa_clients

    await _ensure_oauth_token_table(auth_db)
    _oa_tokens = await _load_oauth_tokens_from_db(auth_db)
    # Load persisted OAuth client registrations (DCR)
    try:
        import json as _json
        async with auth_db.execute("SELECT client_id, client_secret, redirect_uris FROM oauth_clients") as cur:
            rows = await cur.fetchall()
        for row in rows:
            _oa_clients[row["client_id"]] = {
                "secret": row["client_secret"],
                "redirect_uris": _json.loads(row["redirect_uris"] or "[]")
            }
        if rows:
            print(f"[oauth] loaded {len(rows)} persisted client registrations")
    except Exception:
        pass

    if _hosted_mode():
        return

    legacy_tokens = _load_oa_tokens_file()
    if not legacy_tokens:
        return

    _oa_tokens.update(legacy_tokens)
    for token_hash, token_data in legacy_tokens.items():
        await _upsert_oauth_token(
            auth_db,
            token_hash,
            tenant_id=token_data.get("tenant_id"),
            client_id=str(token_data.get("client_id") or ""),
            exp=int(token_data.get("exp", 0)),
        )
    _save_oa_tokens(_oa_tokens)


_oa_tokens: dict[str, dict[str, Any]] = {}


@app.get("/.well-known/oauth-authorization-server")
async def _oauth_meta(request: Request):
    b = str(request.base_url).rstrip("/")
    return JSONResponse({"issuer": b,
        "client_name": "Meridian", "logo_uri": "https://usemeridian.us/static/logo.svg",
        "authorization_endpoint": f"{b}/oauth/authorize",
        "token_endpoint": f"{b}/oauth/token",
        "registration_endpoint": f"{b}/oauth/register",
        "device_authorization_endpoint": f"{b}/oauth/device",
        "scopes_supported": ["mcp"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "urn:ietf:params:oauth:grant-type:device_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"]})


@app.get("/.well-known/oauth-protected-resource")
async def _oauth_protected_resource_meta(request: Request):
    b = str(request.base_url).rstrip("/")
    return JSONResponse({
        "resource": f"{b}/mcp",
        "authorization_servers": [b],
        "scopes_supported": ["mcp"],
        "bearer_methods_supported": ["header"],
    })


@app.post("/oauth/register")
async def _oauth_reg(request: Request):
    d = await request.json()
    cid, cs = _sec.token_urlsafe(16), _sec.token_urlsafe(32)
    redirect_uris = d.get("redirect_uris", [])
    client_name = d.get("client_name", "")
    _oa_clients[cid] = {"secret": cs, "redirect_uris": redirect_uris}
    # Persist to DB so registrations survive restarts
    try:
        import json as _json
        auth_db = request.app.state.db
        await auth_db.execute(
            "INSERT OR REPLACE INTO oauth_clients (client_id, client_secret, redirect_uris, client_name) VALUES (?, ?, ?, ?)",
            (cid, cs, _json.dumps(redirect_uris), client_name)
        )
        await auth_db.commit()
    except Exception:
        pass  # in-memory fallback still works
    return JSONResponse({"client_id": cid, "client_secret": cs,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code"],
        "token_endpoint_auth_method": "client_secret_post"}, status_code=201)


@app.post("/oauth/device")
async def _oauth_device(request: Request):
    """RFC 8628 device authorization endpoint.

    Returns {device_code, user_code, verification_uri, verification_uri_complete,
    expires_in, interval}. No auth required — the flow is initiated by the device.
    """
    import string as _str
    auth_db = request.app.state.db
    b = str(request.base_url).rstrip("/")
    device_code = _sec.token_hex(32)
    # User code: 4 uppercase letters + "-" + 4 uppercase letters
    _chars = _str.ascii_uppercase
    user_code = (
        "".join(_sec.choice(_chars) for _ in range(4))
        + "-"
        + "".join(_sec.choice(_chars) for _ in range(4))
    )
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td_cls
    expires_at = (_dt.now(tz=_tz.utc) + _td_cls(seconds=300)).strftime("%Y-%m-%d %H:%M:%S")
    await auth_db.execute(
        "INSERT INTO device_codes (device_code, user_code, expires_at) VALUES (?, ?, ?)",
        (device_code, user_code, expires_at),
    )
    await auth_db.commit()
    return JSONResponse({
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": f"{b}/activate",
        "verification_uri_complete": f"{b}/activate?code={user_code}",
        "expires_in": 300,
        "interval": 5,
    })


@app.get("/activate", response_class=HTMLResponse)
async def _activate_get(request: Request):
    """Device activation page — shows approval UI for a pending device_code."""
    if _hosted_mode():
        try:
            from .hosted import _SESSION_COOKIE, _read_session_cookie
            cookie_val = request.cookies.get(_SESSION_COOKIE, "")
            if not cookie_val:
                raise ValueError("no session cookie")
            sid = _read_session_cookie(cookie_val)
            if not sid or not await db_module.get_user_session(request.app.state.db, sid):
                raise ValueError("invalid session")
        except Exception:
            from urllib.parse import quote as _q
            orig_qs = str(request.url.query)
            next_path = f"/activate?{orig_qs}" if orig_qs else "/activate"
            return _RR(f"/auth/login?next={_q(next_path)}")

    code_param = (request.query_params.get("code") or "").strip().upper()
    b = str(request.base_url).rstrip("/")
    auth_db = request.app.state.db

    row_data: dict | None = None
    if code_param:
        from datetime import datetime as _dt, timezone as _tz
        async with auth_db.execute(
            "SELECT device_code, user_code, expires_at, approved FROM device_codes WHERE user_code = ?",
            (code_param,),
        ) as _cur:
            _row = await _cur.fetchone()
        if _row:
            _row_d = dict(zip(["device_code", "user_code", "expires_at", "approved"], _row)) if not hasattr(_row, "keys") else dict(_row)
            _exp_str = _row_d.get("expires_at", "")
            try:
                _exp_dt = _dt.fromisoformat(str(_exp_str).replace("Z", "+00:00"))
                if _exp_dt.tzinfo is None:
                    _exp_dt = _exp_dt.replace(tzinfo=_tz.utc)
                if _dt.now(tz=_tz.utc) <= _exp_dt and not _row_d.get("approved"):
                    row_data = _row_d
            except Exception:
                pass

    if code_param and row_data is None:
        # Code not found / expired / already used
        error_msg = "This code has expired or was already used. Start the device flow again."
        return HTMLResponse(content=_activate_page(b, code_param, error=error_msg))

    return HTMLResponse(content=_activate_page(b, code_param, row=row_data))


def _activate_page(base_url: str, code: str, *, row: dict | None = None, error: str | None = None) -> str:
    if error:
        body_html = f'<div class="error">{error}</div>'
    elif row:
        uc = row.get("user_code", code)
        body_html = f'''
        <p class="sub">A device or application wants to connect to your Meridian account.</p>
        <div class="code-box">{uc}</div>
        <p class="sub" style="font-size:13px;margin-bottom:24px">Confirm this code matches what your device shows.</p>
        <form method="POST" action="{base_url}/activate">
          <input type="hidden" name="user_code" value="{uc}">
          <div class="btn-row">
            <button type="submit" name="action" value="approve" class="btn-approve">Approve</button>
            <button type="submit" name="action" value="deny" class="btn-deny">Deny</button>
          </div>
        </form>'''
    else:
        body_html = '''
        <p class="sub">Enter the code shown on your device.</p>
        <form method="GET" action="/activate" style="margin-top:16px">
          <input type="text" name="code" placeholder="XXXX-XXXX" autofocus
            style="width:100%;max-width:220px;text-align:center;font-size:20px;font-family:var(--mono);
                   background:#1a1a1a;border:1px solid #444;border-radius:6px;color:#e8e8e8;
                   padding:10px 12px;letter-spacing:4px">
          <button type="submit" style="display:block;margin:12px auto 0;padding:8px 24px;background:#3b82f6;
            color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:14px">Continue</button>
        </form>'''

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Meridian — Activate Device</title>
  <style>
    :root{{--mono:'IBM Plex Mono',monospace}}
    body{{font-family:system-ui,sans-serif;background:#0d0d0d;color:#e8e8e8;
          display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
    .card{{background:#1a1a1a;border:1px solid #2e2e2e;border-radius:12px;
           padding:32px 40px;max-width:460px;width:100%;text-align:center}}
    h2{{margin:0 0 6px;font-size:20px;color:#fff}}
    .sub{{color:#888;font-size:14px;margin:0 0 16px}}
    .code-box{{font-family:var(--mono);font-size:28px;letter-spacing:8px;color:#7dd3fc;
               background:#0d0d0d;border:1px solid #333;border-radius:8px;
               padding:16px 24px;display:inline-block;margin-bottom:16px}}
    .btn-row{{display:flex;gap:12px;justify-content:center;margin-top:8px}}
    .btn-approve{{padding:10px 28px;background:#22c55e;color:#fff;border:none;
                  border-radius:6px;font-size:15px;cursor:pointer;font-weight:600}}
    .btn-approve:hover{{background:#16a34a}}
    .btn-deny{{padding:10px 28px;background:#3a3a3a;color:#ccc;border:1px solid #555;
               border-radius:6px;font-size:15px;cursor:pointer}}
    .btn-deny:hover{{background:#555}}
    .error{{color:#f87171;background:#2a1111;border:1px solid #7f1d1d;border-radius:6px;
            padding:12px 16px;font-size:14px}}
  </style>
</head>
<body>
  <div class="card">
    <h2>Meridian</h2>
    {body_html}
  </div>
</body>
</html>"""


@app.post("/activate")
async def _activate_post(request: Request):
    """Handle device approval or denial."""
    if _hosted_mode():
        try:
            from .hosted import _SESSION_COOKIE, _read_session_cookie, get_current_tenant
            tenant = await get_current_tenant(request)
            tenant_id = tenant["id"]
        except Exception:
            return _RR("/auth/login?next=/activate")
    else:
        tenant_id = None

    form = dict(await request.form())
    user_code = (form.get("user_code") or "").strip().upper()
    action = (form.get("action") or "").strip()
    auth_db = request.app.state.db

    if not user_code:
        return _RR("/activate", status_code=303)

    async with auth_db.execute(
        "SELECT device_code, user_code, expires_at, approved FROM device_codes WHERE user_code = ?",
        (user_code,),
    ) as _cur:
        _row = await _cur.fetchone()

    if _row is None:
        return _RR("/activate", status_code=303)

    if action == "approve":
        await auth_db.execute(
            "UPDATE device_codes SET tenant_id = ?, approved = 1 WHERE user_code = ?",
            (tenant_id, user_code),
        )
        await auth_db.commit()
    else:
        await auth_db.execute("DELETE FROM device_codes WHERE user_code = ?", (user_code,))
        await auth_db.commit()

    return _RR("/dashboard", status_code=303)


@app.get("/oauth/authorize")
async def _oauth_auth(request: Request):
    p = dict(request.query_params)
    # ── Session cookie guard (hosted mode only) ─────────────────────────────
    # Prevents unauthenticated callers from obtaining MCP access tokens.
    # Local / self-hosted installs skip this guard (_hosted_mode() is False).
    if _hosted_mode():
        authed = False
        from .hosted import _SESSION_COOKIE, _read_session_cookie
        auth_db = request.app.state.db
        cookie_val = request.cookies.get(_SESSION_COOKIE, "")
        if cookie_val:
            sid = _read_session_cookie(cookie_val)
            if sid and await db_module.get_user_session(auth_db, sid):
                authed = True
        if not authed:
            # Also accept a bearer token (API-key flow)
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer "):
                tok_hash = hashlib.sha256(auth_header[7:].encode()).hexdigest()
                if await db_module.get_tenant_from_token_hash(auth_db, tok_hash):
                    authed = True
        if not authed:
            from urllib.parse import quote as _q
            orig_qs = str(request.url.query)
            next_path = f"/oauth/authorize?{orig_qs}" if orig_qs else "/oauth/authorize"
            return _RR(f"/auth/login?next={_q(next_path)}")
    # ── Auto-approve ────────────────────────────────────────────────────────
    # In hosted mode, capture the tenant_id from the session so MCP requests
    # can be routed to the correct per-tenant project DB.
    _tenant_id: str | None = None
    if _hosted_mode():
        from .hosted import _SESSION_COOKIE, _read_session_cookie
        _cookie = request.cookies.get(_SESSION_COOKIE, "")
        _sid = _read_session_cookie(_cookie) if _cookie else None
        if _sid:
            _sess = await db_module.get_user_session(request.app.state.db, _sid)
            if _sess:
                _tenant_id = _sess.get("tenant_id")

    code = _sec.token_hex(32)
    _redirect_uri = p.get("redirect_uri", "")
    _challenge = p.get("code_challenge") or ""
    _oa_codes[code] = {"client_id": p.get("client_id", ""),
        "redirect_uri": _redirect_uri,
        "challenge": _challenge,
        "tenant_id": _tenant_id,
        "exp": _tm.time() + 600}
    try:
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td_cls
        _expires = (_dt.now(tz=_tz.utc) + _td_cls(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        _adb = request.app.state.db
        await _adb.execute(
            "INSERT OR REPLACE INTO oauth_codes (code, tenant_id, redirect_uri, code_challenge, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (code, _tenant_id, _redirect_uri, _challenge, _expires),
        )
        await _adb.commit()
    except Exception:
        pass
    qs = _ue({"code": code, "state": p.get("state", "")})
    return _RR(f"{_redirect_uri}?{qs}")


@app.get("/oauth/device-callback")
async def _oauth_device_callback(request: Request):
    """Show auth code on a success page; JS auto-redirects to the original
    localhost callback so the MCP SDK completes the flow without user action
    in local sessions. Remote sessions see the URL to paste."""
    p = dict(request.query_params)
    code = p.get("code", "")
    state = p.get("state", "")
    to = p.get("to", "")  # original localhost redirect_uri
    callback_url = f"{to}?code={code}&state={state}" if to else ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Meridian — Authorized</title>
<style>
body{{font-family:system-ui,sans-serif;background:#0f0f0f;color:#e5e5e5;
  display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.card{{background:#1a1a1a;border:1px solid #333;border-radius:12px;padding:2rem 2.5rem;
  max-width:520px;width:90%;text-align:center}}
h1{{color:#4ade80;margin:0 0 .5rem}}
p{{color:#999;margin:.5rem 0}}
.url{{background:#111;border:1px solid #444;border-radius:6px;padding:.75rem 1rem;
  font-family:monospace;font-size:.8rem;word-break:break-all;text-align:left;
  color:#e5e5e5;margin:1rem 0;cursor:pointer;user-select:all}}
.copy-btn{{background:#4ade80;color:#000;border:none;border-radius:6px;
  padding:.5rem 1.25rem;font-weight:600;cursor:pointer;font-size:.9rem}}
.copy-btn:hover{{background:#22c55e}}
.note{{font-size:.8rem;color:#666;margin-top:.75rem}}
</style></head>
<body><div class="card">
<h1>&#10003; Authorized</h1>
<p>Paste this URL into your terminal when prompted:</p>
<div class="url" id="cburl" onclick="copyUrl()">{callback_url}</div>
<button class="copy-btn" onclick="copyUrl()">Copy URL</button>
<p class="note" id="note">In a local session this page will auto-close.</p>
</div>
<script>
function copyUrl(){{
  var u = document.getElementById('cburl').textContent;
  navigator.clipboard && navigator.clipboard.writeText(u).then(function(){{
    document.querySelector('.copy-btn').textContent = 'Copied!';
  }});
}}
// Auto-redirect for local sessions — localhost server may be listening.
var to = {_json.dumps(callback_url)};
if (to) {{
  fetch(to, {{mode:'no-cors'}}).then(function(){{
    document.getElementById('note').textContent = 'Local session detected — you can close this tab.';
  }}).catch(function(){{}});
  // Hard redirect after short delay so the MCP SDK receives the code.
  setTimeout(function(){{ window.location.href = to; }}, 800);
}}
</script></body></html>"""
    from fastapi.responses import HTMLResponse as _HR
    return _HR(html)


@app.post("/oauth/token")
async def _oauth_token(request: Request):
    ct = request.headers.get("content-type", "")
    d = dict(await request.json() if "json" in ct else await request.form())
    grant_type = d.get("grant_type", "")

    # ── RFC 8628 device_code grant ──────────────────────────────────────────
    if grant_type == "urn:ietf:params:oauth:grant-type:device_code":
        device_code = (d.get("device_code") or "").strip()
        if not device_code:
            return JSONResponse({"error": "invalid_request", "error_description": "device_code required"}, status_code=400)
        auth_db = request.app.state.db
        from datetime import datetime as _dt, timezone as _tz
        async with auth_db.execute(
            "SELECT device_code, user_code, tenant_id, expires_at, approved FROM device_codes WHERE device_code = ?",
            (device_code,),
        ) as _cur:
            _row = await _cur.fetchone()
        if _row is None:
            return JSONResponse({"error": "expired_token", "error_description": "device code expired or not found"}, status_code=400)
        _row_d = dict(zip(["device_code", "user_code", "tenant_id", "expires_at", "approved"], _row)) if not hasattr(_row, "keys") else dict(_row)
        # Check expiry
        try:
            _exp_dt = _dt.fromisoformat(str(_row_d["expires_at"]).replace("Z", "+00:00"))
            if _exp_dt.tzinfo is None:
                _exp_dt = _exp_dt.replace(tzinfo=_tz.utc)
            if _dt.now(tz=_tz.utc) > _exp_dt:
                await auth_db.execute("DELETE FROM device_codes WHERE device_code = ?", (device_code,))
                await auth_db.commit()
                return JSONResponse({"error": "expired_token", "error_description": "device code expired"}, status_code=400)
        except Exception:
            return JSONResponse({"error": "expired_token"}, status_code=400)
        if not _row_d.get("approved"):
            return JSONResponse({"error": "authorization_pending"}, status_code=200)
        # Approved — issue token
        await auth_db.execute("DELETE FROM device_codes WHERE device_code = ?", (device_code,))
        await auth_db.commit()
        tok = f"sk_meridian_{_sec.token_urlsafe(32)}"
        tok_hash = _oauth_token_hash(tok)
        _oa_tenant_id = _row_d.get("tenant_id")
        tok_data = {"client_id": d.get("client_id", "meridian"), "exp": int(_tm.time() + 86400 * 90), "tenant_id": _oa_tenant_id}
        _oa_tokens[tok_hash] = tok_data
        if _oa_tenant_id:
            import uuid as _uuid
            _api_tid = str(_uuid.uuid4())
            try:
                await auth_db.execute(
                    "INSERT INTO api_tokens (id, tenant_id, token_hash, label, token_type) VALUES (?, ?, ?, ?, ?)",
                    (_api_tid, _oa_tenant_id, tok_hash, "claude-code-oauth", "readwrite"),
                )
                await auth_db.commit()
            except Exception:
                pass
        else:
            await _upsert_oauth_token(auth_db, tok_hash, tenant_id=None, client_id=tok_data["client_id"], exp=tok_data["exp"])
        _save_oa_tokens(_oa_tokens)
        return JSONResponse({"access_token": tok, "token_type": "bearer", "expires_in": 86400 * 90})

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    # S256 is the only supported PKCE method
    _method = d.get("code_challenge_method", "")
    if _method and _method != "S256":
        return JSONResponse({"error": "invalid_request", "error_description": "Only S256 code_challenge_method is supported"}, status_code=400)
    code = d.get("code", "")
    auth_db = request.app.state.db
    # Look up code from DB first (survives restarts), fall back to in-memory
    cd: dict | None = None
    try:
        from datetime import datetime as _dt, timezone as _tz
        async with auth_db.execute(
            "SELECT tenant_id, redirect_uri, code_challenge, expires_at FROM oauth_codes WHERE code = ?",
            (code,),
        ) as _cur:
            _row = await _cur.fetchone()
        if _row:
            _exp_str = _row["expires_at"] if hasattr(_row, "__getitem__") else _row[3]
            _exp_dt = _dt.fromisoformat(str(_exp_str).replace("Z", "+00:00"))
            if _exp_dt.tzinfo is None:
                from datetime import timezone as _tz2
                _exp_dt = _exp_dt.replace(tzinfo=_tz2.utc)
            if _dt.now(tz=_tz.utc) > _exp_dt:
                await auth_db.execute("DELETE FROM oauth_codes WHERE code = ?", (code,))
                await auth_db.commit()
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            cd = {
                "client_id": d.get("client_id", ""),
                "redirect_uri": _row["redirect_uri"] if hasattr(_row, "__getitem__") else _row[1],
                "challenge": _row["code_challenge"] if hasattr(_row, "__getitem__") else _row[2],
                "tenant_id": _row["tenant_id"] if hasattr(_row, "__getitem__") else _row[0],
            }
            await auth_db.execute("DELETE FROM oauth_codes WHERE code = ?", (code,))
            await auth_db.commit()
    except Exception:
        pass
    if cd is None:
        if code not in _oa_codes:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        _mem = _oa_codes.pop(code)
        if _tm.time() > _mem["exp"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        cd = {
            "client_id": _mem.get("client_id", ""),
            "redirect_uri": _mem.get("redirect_uri", ""),
            "challenge": _mem.get("challenge", ""),
            "tenant_id": _mem.get("tenant_id"),
        }
    # redirect_uri must match what was stored
    if cd["redirect_uri"] and d.get("redirect_uri") != cd["redirect_uri"]:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    # Validate code_verifier length
    v = d.get("code_verifier", "")
    if v and not (43 <= len(v) <= 128):
        return JSONResponse({"error": "invalid_request", "error_description": "code_verifier must be 43-128 characters"}, status_code=400)
    # Verify PKCE challenge when present
    if cd["challenge"]:
        if not v:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        ch = _b64.urlsafe_b64encode(_hs.sha256(v.encode()).digest()).decode().rstrip("=")
        if ch != cd["challenge"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
    # Generate sk_meridian_ token
    tok = f"sk_meridian_{_sec.token_urlsafe(32)}"
    tok_hash = _oauth_token_hash(tok)
    tenant_id = cd.get("tenant_id")
    tok_data = {
        "client_id": cd["client_id"],
        "exp": int(_tm.time() + 86400 * 90),
        "tenant_id": tenant_id,
    }
    _oa_tokens[tok_hash] = tok_data
    if tenant_id:
        # Hosted mode: store in api_tokens so Bearer auth and _db() routing both work
        import uuid as _uuid
        _api_tid = str(_uuid.uuid4())
        try:
            await auth_db.execute(
                "INSERT INTO api_tokens (id, tenant_id, token_hash, label, token_type)"
                " VALUES (?, ?, ?, ?, ?)",
                (_api_tid, tenant_id, tok_hash, "oauth", "readwrite"),
            )
            await auth_db.commit()
        except Exception:
            pass
    else:
        # Self-hosted / local: store in oauth_tokens as before
        await _upsert_oauth_token(
            auth_db,
            tok_hash,
            tenant_id=None,
            client_id=tok_data["client_id"],
            exp=tok_data["exp"],
        )
    _save_oa_tokens(_oa_tokens)
    return JSONResponse({"access_token": tok, "token_type": "bearer", "expires_in": 86400 * 90})


@app.get("/mcp")
async def _mcp_get(request: Request):
    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept:
        return _RR("/mcp/sse")
    return JSONResponse({"name": "meridian", "version": "1.0", "transport": "http+sse"})

# ── End OAuth ──────────────────────────────────────────────────────────────


_SSE_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Cache-Control": "no-cache, no-store",
    "X-Accel-Buffering": "no",
}


@app.options("/mcp/sse")
async def mcp_sse_options(request: Request) -> Response:
    """CORS preflight for chrome-extension:// origin."""
    return Response(status_code=204, headers=_SSE_CORS_HEADERS)


@app.get("/mcp/sse")
async def mcp_sse_get(request: Request) -> StreamingResponse:
    """MCP SSE transport GET — opens event stream for dnakov/claude-mcp.

    Sends ``event: endpoint`` with POST URL, then heartbeats every 15 s.
    No strict auth required: uses the same _db() resolver (Bearer / cookie /
    local fallback) so both local and hosted-tier clients work.
    """
    import uuid as _uuid
    import anyio as _anyio

    db = await _db(request)
    data_dir = _data_dir(request)

    # Reuse session_id on reconnect if client sends one and it's valid
    requested_sid = request.query_params.get("session_id")
    if requested_sid and requested_sid in _SSE_SESSIONS:
        session_id = requested_sid
        _SSE_SESSIONS[session_id]["db"] = db
    else:
        session_id = str(_uuid.uuid4())
        _SSE_SESSIONS[session_id] = {"db": db, "data_dir": data_dir}

    endpoint_path = f"/mcp/sse?session_id={session_id}"

    async def _stream():
        try:
            yield f"event: endpoint\ndata: {endpoint_path}\n\n"
            # Heartbeat loop — exit when client disconnects
            while True:
                if await request.is_disconnected():
                    break
                yield ": heartbeat\n\n"
                await _anyio.sleep(15)
        finally:
            _SSE_SESSIONS.pop(session_id, None)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers=_SSE_CORS_HEADERS,
    )


@app.post("/mcp/sse")
async def mcp_sse_post(request: Request) -> Any:
    """MCP SSE transport POST — JSON-RPC handler for dnakov/claude-mcp.

    Client POSTs a JSON-RPC 2.0 message; response is returned as JSON in the
    HTTP response body (extension reads it directly, not via the SSE stream).
    """
    from fastapi.responses import JSONResponse as _JSONResp

    session_id = request.query_params.get("session_id", "")
    sess = _SSE_SESSIONS.get(session_id)
    db = sess["db"] if sess else await _db(request)
    data_dir = sess["data_dir"] if sess else _data_dir(request)

    try:
        body = await request.json()
    except Exception:
        return _JSONResp(_jsonrpc_err(None, -32700, "parse error"), status_code=400, headers=_SSE_CORS_HEADERS)

    if isinstance(body, list):
        results = [await _handle_mcp_request(item, db, data_dir) for item in body]
        return _JSONResp(results, headers=_SSE_CORS_HEADERS)

    result = await _handle_mcp_request(body, db, data_dir)
    return _JSONResp(result, headers=_SSE_CORS_HEADERS)


@app.post("/feedback", status_code=201)
async def submit_feedback(request: Request) -> dict[str, str]:
    """Submit user feedback. Requires JSON body: {\"type\": \"...\", \"message\": \"...\", \"email\": \"...\"}."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    if _is_demo_request(request):
        return {"id": "demo"}
    from .hosted import get_current_tenant
    tenant = await get_current_tenant(request)
    if not tenant:
        raise HTTPException(status_code=401, detail="Not authenticated")

    body = await request.json()
    feedback_type = body.get("type", "general")
    message = (body.get("message") or "").strip()
    email = body.get("email", tenant.get("email"))

    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    # Validate email format if provided
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        raise HTTPException(status_code=400, detail="Invalid email address")

    # Feedback goes in the auth DB (not project DB) — tenants table is there
    auth_db = request.app.state.db
    try:
        feedback_id = await db_module.add_feedback(auth_db, tenant["id"], feedback_type, message, email)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Feedback save failed: {exc}") from exc
    return {"id": feedback_id}


# ---------------------------------------------------------------------------
# Per-token rate limiting for /mcp POST
# ---------------------------------------------------------------------------

import time as _mcp_time

# {token_hash: (count, window_start_epoch_seconds)}
_mcp_rate_counters: dict[str, tuple[int, float]] = {}
_MCP_RATE_WINDOW = 60  # seconds


def _mcp_rate_check(token_hash: str, limit: int) -> bool:
    """Return True (= rate limited) if the token has exceeded limit calls/min."""
    now = _mcp_time.monotonic()
    count, window_start = _mcp_rate_counters.get(token_hash, (0, now))
    if now - window_start >= _MCP_RATE_WINDOW:
        # New window
        _mcp_rate_counters[token_hash] = (1, now)
        return False
    if count >= limit:
        return True
    _mcp_rate_counters[token_hash] = (count + 1, window_start)
    return False


@app.post("/mcp")
async def remote_mcp(request: Request) -> Any:
    try:
        return await _remote_mcp_inner(request)
    except Exception as _e:
        import logging as _log
        _req_id = getattr(request.state, "request_id", "unknown")
        _log.getLogger("meridian.server").exception(
            "unhandled exception in remote_mcp (request_id=%s)", _req_id, exc_info=_e
        )
        try:
            _body = await request.body()
            _req_id_from_body = __import__("json").loads(_body).get("id")
        except Exception:
            _req_id_from_body = None
        from fastapi.responses import JSONResponse as _JR
        return _JR({"jsonrpc": "2.0", "id": _req_id_from_body, "error": {"code": -32603, "message": "internal error — please retry"}})


async def _remote_mcp_inner(request: Request) -> Any:
    """Remote MCP endpoint — JSON-RPC 2.0 over HTTP.

    Accepts OAuth bearer tokens and Meridian API keys over the same endpoint.
    Rate-limited: 600 req/min per authenticated token, 60 req/min for free tier.
    Accepts a single JSON-RPC 2.0 message or a batch (list).
    """
    from fastapi.responses import JSONResponse

    if _limiter is not None:
        try:
            await _limiter._check_request_limit(request, None, False)
        except Exception:
            pass  # rate limiting is best-effort; don't block on errors

    # Check local OAuth tokens first (claude.ai connector via tunnel or hosted OAuth)
    _auth = request.headers.get("authorization", "")
    _bearer = _auth.removeprefix("Bearer ").strip()
    _bearer_hash = _oauth_token_hash(_bearer) if _bearer else ""
    _td = _oa_tokens.get(_bearer_hash) if _bearer_hash else None
    if _td is None and _bearer_hash:
        _td = await _get_oauth_token_from_db(request.app.state.db, _bearer_hash)
        if _td is not None:
            _oa_tokens[_bearer_hash] = _td
    if _td is not None:
        if _tm.time() > _td.get("exp", 0):
            _oa_tokens.pop(_bearer_hash, None)
            _base_r = str(request.base_url).rstrip("/")
            return JSONResponse(
                {"error": "token_expired"},
                status_code=401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{_base_r}/.well-known/oauth-protected-resource"'},
            )
        try:
            _body = await request.json()
        except Exception:
            return JSONResponse(_jsonrpc_err(None, -32700, "parse error"), status_code=400)
        # In hosted mode, route to the tenant's project DB (not the shared auth DB).
        # tenant_id is stored in _oa_tokens when the OAuth flow ran in hosted mode.
        _mdb = request.app.state.db
        _oa_tenant_id = _td.get("tenant_id")
        if _oa_tenant_id and _hosted_mode():
            try:
                from ._deps import _open_tenant_db_by_id
                _mdb = await _open_tenant_db_by_id(request, _oa_tenant_id)
            except Exception:
                pass  # fall back to auth DB — better than 500
        _mdd = request.app.state.data_dir
        _oa_tenant = None
        if _oa_tenant_id and _hosted_mode():
            _oa_tenant = await db_module.get_tenant_by_id(request.app.state.db, _oa_tenant_id)
        if isinstance(_body, list):
            return JSONResponse([await _handle_mcp_request(i, _mdb, _mdd, tenant=_oa_tenant) for i in _body])
        return JSONResponse(await _handle_mcp_request(_body, _mdb, _mdd, tenant=_oa_tenant))

    tenant = None
    if _bearer_hash:
        tenant = await db_module.get_tenant_from_token_hash(request.app.state.db, _bearer_hash)
    if tenant is None:
        import logging as _logging
        _raw_auth = request.headers.get("authorization", "")
        _logging.getLogger("meridian.mcp_auth").warning(
            "[mcp_auth] unrecognised token raw=%r ua=%r",
            (_raw_auth[:60] if _raw_auth else "(none)"),
            request.headers.get("user-agent", "")[:60],
        )
        _base = str(request.base_url).rstrip("/")
        return JSONResponse(
            {"detail": "invalid API token"},
            status_code=401,
            headers={
                "WWW-Authenticate": (
                    f'Bearer realm="MCP",'
                    f' error="invalid_token",'
                    f' resource_metadata="{_base}/.well-known/oauth-protected-resource",'
                    f' device_authorization_endpoint="{_base}/oauth/device"'
                ),
            },
        )

    # Extract token_type for read-only enforcement ('readwrite' or 'readonly').
    _token_type = (tenant.pop("_token_type", None) or "readwrite")

    # Per-token rate limiting: 60/min for free tier, 600/min for others.
    _plan = (tenant.get("plan") or "free").lower()
    _rate_limit = 100 if _plan == "free" else 1000
    if _mcp_rate_check(_bearer_hash, _rate_limit):
        return JSONResponse(
            {"detail": f"rate limit exceeded ({_rate_limit} req/min)"},
            status_code=429,
            headers={"Retry-After": "60"},
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_jsonrpc_err(None, -32700, "parse error"), status_code=400)

    db = await _db(request)
    data_dir = _data_dir(request)

    if isinstance(body, list):
        results = [await _handle_mcp_request(item, db, data_dir, tenant=tenant, token_type=_token_type) for item in body]
        return JSONResponse(results)

    result = await _handle_mcp_request(body, db, data_dir, tenant=tenant, token_type=_token_type)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# MCP server — implementation lives in meridian/mcp/stdio_handler.py
# ---------------------------------------------------------------------------

from .mcp.stdio_handler import build_mcp_server  # noqa: F401
