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
    _BUNDLE_HASH,
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
    _RATE_LIMIT,
    _TENANT_RL_WINDOW_SECONDS,
    _TENANT_RL_PLAN_TTL_SECONDS,
    _TENANT_RL_PER_MINUTE,
    _tenant_rl_hits,
    _tenant_rl_plan_cache,
    _get_authenticated_tenant,
    _mask_api_token_hash,
    _md_ts,
    _md_one_line,
    _require_workspace_perm,
    _enforcement_context,
    _required_perm_for_request,
    _render_workspace_block,
    _render_context_block,
)

# The slowapi Limiter is a process-singleton in ._deps so extracted routers can
# apply _rate_limit at import time. The test suite reloads THIS module to get a
# "fresh" limiter; since ._deps (and the route modules) are NOT reloaded, a naive
# reload would re-run server.py's own @_rate_limit decorators and double-register
# their limits (slowapi .extend()s per key) → double counting. So drop only the
# registrations owned by THIS module before its decorators re-run, and reset the
# shared counters. Router-owned registrations (meridian.routes.*) are added once
# and must survive — clearing them would silently disable their rate limits,
# because route modules don't re-decorate on a server.py reload.
if _limiter is not None:
    _own = f"{__name__}."  # "meridian.server."
    for _store in (
        _limiter._route_limits,
        _limiter._dynamic_route_limits,
        _limiter._Limiter__marked_for_limiting,  # type: ignore[attr-defined]
    ):
        for _key in [k for k in _store if k.startswith(_own)]:
            del _store[_key]
    _limiter._application_limits.clear()
    try:
        _limiter.reset()
    except Exception:  # pragma: no cover - storage may not support reset
        pass


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
from . import dispatcher as dispatcher_module
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

    # 1d69d5d9 — seed workspace_settings from meridian.toml on first boot so the
    # 46c83e55 toml keys actually take effect (self-host). Guarded; no-op when a
    # settings row already exists or no toml/env config is present.
    await db_module.seed_workspace_settings_from_toml(db)

    # c00b1ccf — wire the offline codebase-graph-snapshot searcher into handoff
    # code-pointer enrichment. Fallback used when generate_handoff isn't handed a
    # live tunnel searcher; it reads the persisted snapshot so a fresh session
    # still gets code pointers. Fully guarded + degrades to [].
    try:
        from . import handoff as _handoff_mod  # noqa: PLC0415
        from .graph_snapshot import make_snapshot_searcher as _mk_snap_searcher  # noqa: PLC0415
        _handoff_mod.set_graph_searcher_resolver(
            lambda pid: _mk_snap_searcher(db, pid)
        )
    except Exception:  # noqa: BLE001 — never block startup on enrichment wiring
        pass

    # Per-tenant neon_db_url re-key (security item 3dbe23e3). NO-OP unless
    # MERIDIAN_MASTER_SECRET is set — so this is a zero-behavior-change deploy
    # in prod (where the secret is currently unset). Guarded + never crashes
    # boot; re-keys legacy global-key URLs to per-tenant keys, idempotently.
    try:
        from .tenant_crypto import rekey_tenant_db_urls  # noqa: PLC0415
        await rekey_tenant_db_urls(db)
    except Exception:  # noqa: BLE001 — re-key must never block server startup
        import logging as _rekey_log  # noqa: PLC0415
        _rekey_log.getLogger(__name__).warning(
            "per-tenant neon_db_url re-key raised during startup; continuing",
            exc_info=True,
        )

    # True when the main DB is a remote/Postgres backend (env URL or toml conn).
    # The demo DB resolver fails closed against this so /demo never serves real data.
    app.state.db_is_remote = bool(db_url)
    app.state.data_dir = str(data_dir)
    app.state.ws_broadcaster = dashboard_module.WebSocketBroadcaster()
    from .routes.oauth import _hydrate_oauth_cache as _hydrate_oa  # noqa: PLC0415 — c5f8ac43
    await _hydrate_oa(db)

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
            "meridian/static/dashboard.ts",
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

    # 57f7f7ba — autonomous dispatcher daemon.
    # GUARDRAIL: DEFAULT OFF. start_dispatcher_if_enabled is a no-op unless
    # MERIDIAN_DISPATCHER_ENABLED == "1". On the multi-tenant production server
    # the env var is unset, so NO worker (`claude -p`) processes are ever
    # auto-spawned. Enabling it is an explicit, opt-in operator decision on a
    # single-tenant self-hosted box. See meridian/dispatcher.py.
    app.state.dispatcher = None
    if dispatcher_module.is_enabled():
        try:
            _disp_project = os.environ.get("MERIDIAN_DISPATCHER_PROJECT_ID")
            if _disp_project:
                dispatcher_module.start_dispatcher_if_enabled(
                    app, db, _disp_project
                )
        except Exception:  # noqa: BLE001 — never block startup on the dispatcher
            pass

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
        # 57f7f7ba — stop the autonomous dispatcher if it was enabled.
        _disp = getattr(app.state, "dispatcher", None)
        if _disp is not None:
            try:
                await _disp.stop()
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
from .routes.insights import router as _insights_router      # noqa: E402
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
from .routes.blog import router as _blog_router              # noqa: E402
from .routes.marketplace import router as _marketplace_router  # noqa: E402
from .routes.tunnel import router as _tunnel_router          # noqa: E402
from .routes.oauth import router as _oauth_router            # noqa: E402
from .routes.a2a import router as _a2a_router                # noqa: E402

app.include_router(_oauth_router)
app.include_router(_notes_router)
app.include_router(_insights_router)
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
app.include_router(_blog_router)
app.include_router(_marketplace_router)
app.include_router(_tunnel_router)
app.include_router(_a2a_router)

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

# 95499c3e / decision 6fe5210c — Option A: airtight per-request project-scope
# enforcement. Matches a /projects/{uuid} or /projects/{uuid}/... path so we only
# gate by-ID project access (not the /projects listing or /projects/by-name lookup).
_PROJECT_ID_PATH_RE = re.compile(
    r"^/projects/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:/|$)"
)


@app.middleware("http")
async def project_scope_enforcement(request: Request, call_next):
    """95499c3e / decision 6fe5210c — a project-scoped workspace member is 403'd on
    any project outside their scope, even by direct ID (not just hidden from
    listings). Workspace-wide members and the tenant owner are unaffected.

    This is the single chokepoint for every ``/projects/{uuid}/*`` HTTP route —
    routes scatter ``get_project`` calls with no shared dependency, so enforcing
    here covers them all at once. Returns 403 (not 404) so existence isn't leaked.
    The MCP dispatch layer enforces the same rule separately (see /mcp).
    """
    m = _PROJECT_ID_PATH_RE.match(request.url.path)
    if m is not None:
        from ._deps import _scoped_project_ids_for_request  # noqa: PLC0415
        try:
            scoped = await _scoped_project_ids_for_request(request)
        except Exception:  # noqa: BLE001 — a scope-check failure must never 500 a request
            scoped = None
        if scoped is not None and m.group(1) not in scoped:
            return JSONResponse(
                {"detail": "Project is outside your access scope."},
                status_code=403,
            )
    return await call_next(request)


@app.middleware("http")
async def site_password_gate(request: Request, call_next):
    site_pw = os.environ.get("SITE_PASSWORD", "")
    if not site_pw:
        return await call_next(request)
    path = request.url.path
    if path in ("/health", "/failover-status", "/mcp/health", "/__gate__", "/config", "/setup/health", "/static", "/sw.js", "/manifest.webmanifest", "/mcp/tools-doc", "/mcp/quickstart", "/mcp/sse", "/mcp", "/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource", "/hooks/session-start", "/hooks/stop") or path.startswith("/static/") or path.startswith("/oauth/") or path.startswith("/status/") or path == "/demo" or path.startswith("/demo/"):
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
        # WebSocket tunnel connections pass the token via ?token= query param
        # (not an Authorization header) so we must check both.
        token_qp = request.query_params.get("token", "")
        for raw_tok in filter(None, [
            auth_header[7:].strip() if auth_header.startswith("Bearer ") else "",
            token_qp,
        ]):
            import hashlib  # noqa: PLC0415
            token_hash = hashlib.sha256(raw_tok.encode()).hexdigest()
            if await db_module.get_tenant_from_token_hash(auth_db, token_hash):
                return await call_next(request)
        if auth_header.startswith("Bearer "):
            # Also check OAuth tokens (ChatGPT and other OAuth clients use these)
            # c5f8ac43 — delegate to routes.oauth module where the in-process cache lives.
            from .routes import oauth as _om  # noqa: PLC0415
            oauth_hash = _om._oauth_token_hash(auth_header[len("Bearer "):].strip())  # noqa: SLF001
            if _om._oa_tokens.get(oauth_hash) or await _om._get_oauth_token_from_db(auth_db, oauth_hash):  # noqa: SLF001
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


# ---------------------------------------------------------------------------
# Tier-based per-tenant rate limiting (live-queue hardening, 2b93cb59)
#
# Executors poll get_sprint_progress between tasks; this meters the programmatic
# (Bearer-token) surface per tenant per minute by plan — free=500, standard=2000,
# pro/admin=unlimited. Dashboard/cookie, demo, unauthenticated, /health and
# /static traffic is never metered. FAIL-OPEN: any error resolving the tenant or
# counting hits lets the request through, so a limiter bug can never take down
# live traffic. In-memory sliding window (process-local, no Redis), matching the
# slowapi limiter's storage model.
# ---------------------------------------------------------------------------
async def _tenant_rate_limit_decision(request: Request):
    """Return a 429 JSONResponse when the bearer tenant is over its per-minute
    plan budget, else None. Hosted-mode + Bearer-token requests only."""
    import hashlib
    import time as _time

    if not _hosted_mode():
        return None
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None  # cookie/dashboard, demo, and unauth traffic is never metered
    path = request.url.path
    if path.startswith("/health") or path.startswith("/static"):
        return None

    token_hash = hashlib.sha256(auth[7:].encode()).hexdigest()
    now = _time.monotonic()
    cached = _tenant_rl_plan_cache.get(token_hash)
    if cached is not None and (now - cached[0]) < _TENANT_RL_PLAN_TTL_SECONDS:
        tenant_id, plan = cached[1]
    else:
        from . import db as _db_mod  # noqa: PLC0415
        tenant = await _db_mod.get_tenant_from_token_hash(request.app.state.db, token_hash)
        if not tenant:
            return None  # unknown token — let the route's own auth reject it
        tenant_id = tenant.get("id") or token_hash
        plan = (tenant.get("plan") or "free").lower()
        _tenant_rl_plan_cache[token_hash] = (now, (tenant_id, plan))

    limit = _TENANT_RL_PER_MINUTE.get(plan, _TENANT_RL_PER_MINUTE["free"])
    if limit is None:
        return None  # pro / admin — unlimited

    hits = _tenant_rl_hits.setdefault(tenant_id, [])
    cutoff = now - _TENANT_RL_WINDOW_SECONDS
    if hits and hits[0] < cutoff:
        keep = len(hits)
        for keep in range(len(hits)):
            if hits[keep] >= cutoff:
                break
        del hits[:keep]
    if len(hits) >= limit:
        from fastapi.responses import JSONResponse  # noqa: PLC0415
        retry = max(1, int(_TENANT_RL_WINDOW_SECONDS - (now - hits[0]))) if hits else 1
        return JSONResponse(
            status_code=429,
            content={
                "detail": f"Rate limit exceeded: plan '{plan}' allows {limit} requests/min.",
                "plan": plan,
                "limit_per_minute": limit,
                "retry_after_seconds": retry,
            },
            headers={"Retry-After": str(retry)},
        )
    hits.append(now)
    return None


@app.middleware("http")
async def _tenant_rate_limit_middleware(request: Request, call_next):
    blocked = None
    try:
        blocked = await _tenant_rate_limit_decision(request)
    except Exception:  # FAIL OPEN — a limiter bug must never break live traffic
        blocked = None
    if blocked is not None:
        return blocked
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
# 393eed0a — workspace-role enforcement (viewer read-only / member-limited)
# ---------------------------------------------------------------------------
@app.middleware("http")
async def _role_enforcement_middleware(request: Request, call_next):
    """Enforce workspace roles on cross-workspace writes.

    viewer = read-only; member = project-data writes; admin = + settings/invites;
    owner = everything. Acts ONLY when the request targets a workspace the caller
    was invited to (i.e. carries X-Workspace-Tenant-Id) — solo owners are never
    gated and pay no extra DB cost. Self-scoped writes (billing/account/auth) and
    the per-tool-gated /mcp endpoint are skipped by _required_perm_for_request.
    """
    from .roles import has_perm  # noqa: PLC0415
    required = _required_perm_for_request(request.method, request.url.path)
    if required is None:
        return await call_next(request)
    try:
        ctx = await _enforcement_context(request)
    except Exception:
        import logging as _l  # noqa: PLC0415
        _l.getLogger("meridian.roles").exception("role enforcement check failed")
        # The no-header fast path returns None WITHOUT querying, so reaching here
        # means a workspace header was present → a cross-workspace write we could
        # not verify → fail closed.
        return Response(
            content=json.dumps({
                "error": "forbidden",
                "message": "Could not verify workspace permissions.",
            }),
            status_code=403,
            media_type="application/json",
        )
    if ctx is not None and not has_perm(ctx[2], required):
        return Response(
            content=json.dumps({
                "error": "forbidden",
                "message": f"Your workspace role ('{ctx[2]}') cannot perform this action.",
            }),
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
    # 71c70308 — search crawlers welcome; AI/LLM training + scraping crawlers are
    # disallowed. Compliance is voluntary, but this documents intent and the
    # well-behaved ones honor it. Pairs with the noai/noarchive landing meta tags.
    _ai_bots = (
        "GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "anthropic-ai",
        "Claude-Web", "CCBot", "Google-Extended", "PerplexityBot", "Amazonbot",
        "Bytespider", "meta-externalagent", "Applebot-Extended", "cohere-ai",
        "Diffbot", "ImagesiftBot", "Omgilibot", "FriendlyCrawler",
    )
    blocks = "\n\n".join(f"User-agent: {b}\nDisallow: /" for b in _ai_bots)
    return (
        "# Search crawlers welcome; AI/LLM training crawlers are not (71c70308).\n"
        "User-agent: *\nAllow: /\n\n"
        f"{blocks}\n\n"
        "Sitemap: https://usemeridian.us/sitemap.xml\n"
    )


@app.get("/favicon.ico")
async def favicon() -> Response:
    """ac21d522 — serve the compass logo for bare /favicon.ico requests.

    Browsers and crawlers (incl. Google's favicon service) hit /favicon.ico
    directly when no recognised icon link is found. Redirect to the SVG so the
    site never shows a generic placeholder icon. (The HTML <link rel="icon">
    tags already point modern browsers at /static/logo.svg.)
    """
    return RedirectResponse(url="/static/logo.svg", status_code=301)


# ---------------------------------------------------------------------------
# b03be6a6 — Minimal installable PWA (manifest + service worker).
#
# The service worker's scope is limited to the directory it is served from, so
# to control /dashboard it MUST be served at the ROOT (/sw.js → scope "/"). The
# static mount lives at /static, whose scope would only cover /static/*, so we
# expose sw.js and the manifest via explicit root routes here. The underlying
# files live in meridian/static/ alongside the rest of the assets.
#
# The SW itself is deliberately NETWORK-FIRST (see sw.js) so dashboard edits show
# up on next open with no rebuild/republish. These routes send no-cache headers
# too, so a fresh sw.js is always fetched.
# ---------------------------------------------------------------------------


@app.get("/sw.js")
async def service_worker() -> Response:
    """b03be6a6 — serve the PWA service worker at the site root.

    Root scope ("/") is required so the worker can control /dashboard. The
    ``Service-Worker-Allowed: /`` header is belt-and-suspenders; serving from /
    already yields root scope. Sent no-cache so the network-first SW itself is
    never pinned to a stale version.
    """
    from fastapi.responses import FileResponse  # noqa: PLC0415

    return FileResponse(
        _resource_path("meridian/static/sw.js"),
        media_type="text/javascript",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Service-Worker-Allowed": "/",
        },
    )


@app.get("/manifest.webmanifest")
async def web_manifest() -> Response:
    """b03be6a6 — serve the web app manifest at the site root (PWA installability)."""
    from fastapi.responses import FileResponse  # noqa: PLC0415

    return FileResponse(
        _resource_path("meridian/static/manifest.webmanifest"),
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


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

# /auth/* OAuth + magic-link routes moved to meridian/routes/auth.py

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
    from .hosted import _admin_emails
    from . import __version__ as _meridian_version
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
    # 509d9de1 — free/trial users who haven't created a project yet have no
    # inactivity_expires_at, so the banner showed "limited time" with no number.
    # Fall back to a 30-day window anchored on trial_started_at (or the tenant's
    # created_at) so an actual day count always renders. Display-only.
    if days_remaining is None and plan in ("free", "trial"):
        _anchor = tenant.get("trial_started_at") or tenant.get("created_at")
        if _anchor:
            try:
                _anchor_dt = datetime.strptime(
                    str(_anchor)[:19], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                _elapsed = (datetime.now(timezone.utc) - _anchor_dt).days
                days_remaining = max(0, 30 - _elapsed)
            except (ValueError, TypeError):
                pass
    # G2.10 — internal tenants never see the "expired" / "days remaining"
    # banner. The lifecycle jobs already skip them, but a positive UX cue
    # is cleaner than leaving the expired flag set with no consequence.
    if tenant.get("is_internal"):
        expired = False
        days_remaining = None
    # 8660d701 — per-machine tunnel config. The client sends its hostname; resolve
    # that machine's config (or the per-tenant default for unconfigured machines).
    from .tunnel_plugins import resolve_plugins, select_host_config
    _me_hostname = (request.headers.get("X-Meridian-Hostname") or "").strip() or None
    _me_eff_cfg = select_host_config(
        _parsed_tunnel_plugins(tenant.get("tunnel_plugins")),
        tenant.get("tunnel_plugins_by_host"),
        _me_hostname,
    )
    return {
        "plan": plan,
        # Tunnel client reads this to nudge an upgrade when it's behind the
        # deployed server (see tunnel_client._update_notice). Single source:
        # meridian.__version__.
        "server_version": _meridian_version,
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
        "tunnel_active": bool(tenant.get("tunnel_active")),
        # Tunnel client (`meridian --tunnel`) reads this to build its
        # wss://.../tunnel/{tenant_id} and permanent /fs/mcp/{tenant_id} URLs.
        "tenant_id": tenant.get("id"),
        # Resolved tunnel plugin registry (3-slot model) — the client spawns
        # whatever is enabled here, applying any per-tenant command/port
        # overrides over the built-in defaults. NULL config → built-in defaults.
        # 8660d701 — per-machine config: the tunnel client sends X-Meridian-Hostname,
        # so we resolve THIS machine's config from tunnel_plugins_by_host (falling
        # back to the per-tenant default). Different machines, different software.
        "tunnel_plugins": resolve_plugins(_me_eff_cfg),
        # Raw per-tenant overrides so the client can re-resolve locally with
        # binary-detection (auto-enabling Office slots) while still honouring any
        # explicit enabled setting the user saved.
        "tunnel_plugins_config": _me_eff_cfg,
        "tunnel_hostname": _me_hostname,
    }


def _parsed_tunnel_plugins(raw: Any) -> Any:
    """Parse the stored tunnel_plugins JSON into Python (None on junk/empty)."""
    if isinstance(raw, str) and raw.strip():
        import json as _json
        try:
            return _json.loads(raw)
        except Exception:  # noqa: BLE001
            return None
    return raw if isinstance(raw, (dict, list)) else None


def _resolved_tunnel_plugins(raw: Any) -> list[dict[str, Any]]:
    """Parse the stored tunnel_plugins JSON and resolve it over the defaults."""
    from .tunnel_plugins import resolve_plugins
    return resolve_plugins(_parsed_tunnel_plugins(raw))


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
            "bundle_hash": _BUNDLE_HASH,
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
            "bundle_hash": _BUNDLE_HASH,
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
            "- `meridian/static/dashboard.ts` — dashboard UI",
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
    # d116642e — optional project-level invite. When omitted/blank the member is
    # workspace-wide. When set, the member is scoped to that single project. As of
    # 95499c3e (decision 6fe5210c, Option A) this is airtight per-request access
    # enforcement (403 on any other project by direct ID), not just listing-only.
    # A scoped member with an admin role is a "co-admin" (admin of that project).
    raw_project_id = body.get("project_id")
    project_id = raw_project_id.strip() if isinstance(raw_project_id, str) else None
    project_id = project_id or None
    db = request.app.state.db
    limit = _WORKSPACE_MEMBER_LIMITS.get(tenant.get("plan", "standard"), 25)
    count = await db_module.count_workspace_members(db, tenant["id"])
    if count >= limit:
        raise HTTPException(status_code=402, detail=f"Team member limit ({limit}) reached for your plan")
    raw_token = _secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    invite = await db_module.create_workspace_invite(
        db, tenant["id"], email, role, token_hash,
        github_access=github_access, project_id=project_id,
    )
    base = os.environ.get("MERIDIAN_SERVER_URL", "https://usemeridian.us").rstrip("/")
    invite_url = f"{base}/workspace/accept?token={raw_token}"
    try:
        await send_invite_email(email, invite_url, tenant["email"])
    except Exception:
        pass  # email failure doesn't block invite creation
    return {
        "id": invite["id"], "email": email, "role": role,
        "project_id": project_id, "pending": True,
    }


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
        # fbbe99af — store token in a session cookie so it survives the OAuth
        # redirect chain (the ?next= URL param alone drops the token because
        # the ?token= is parsed as a top-level query param at intermediate hops).
        from urllib.parse import quote as _q
        _secure = os.environ.get("MERIDIAN_BASE_URL", "").startswith("https://")
        _redir = _Redir(
            f"/auth/login?next={_q(f'/workspace/accept?token={token}')}",
            status_code=302,
        )
        _redir.set_cookie(
            "pending_invite_token",
            token,
            httponly=True,
            secure=_secure,
            samesite="lax",
            max_age=3600,
        )
        return _redir
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
    # Per-tenant key when MERIDIAN_MASTER_SECRET is set, else legacy global key.
    from .tenant_crypto import encrypt_tenant_db_url
    encrypted = encrypt_tenant_db_url(tenant["id"], url)
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
            "project_id": m.get("project_id"),
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

# GitHub integration routes moved to meridian/routes/github.py

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
| start_session | Register session, get full project context | start_session(project_name="my-project", session_name="feature-x") |
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
                           If urgency='blocking': display the returned `chat_prompt`
                           to the user, then poll get_hitl_request(request_id) every
                           30 s. If the user answers in chat, call answer_hitl(). First
                           answer (dashboard or chat) unblocks you.
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
        "| `start_session` | Register session, get full project context | `start_session(project_name=\"my-project\", session_name=\"feature-x\", human_id=\"alice\")` |\n",
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
    lines += _render_tool(
        "get_sprint_progress",
        "Read-only: Sprint progress summary — counts by status, `percent_complete`, and the item "
        "list.\n\n"
        "**Poll this between tasks.** After each `complete_sprint_item`, call "
        "`get_sprint_progress(project_id, session_id)` (pass `session_id`) before claiming the "
        "next item. The `board_change` field reports items a planner injected since this session "
        "started, so an executor picks them up at the item boundary without restarting — never "
        "idle-poll, only poll at task boundaries. The result is cached server-side for **10 "
        "seconds**, so parallel sessions polling together share a single DB query.\n\n"
        "Statuses include `provisional_complete` — work finished but not yet verified/deployed, a "
        "non-terminal state between `in_progress` and `done` that does not count toward "
        "`percent_complete`.")
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
    lines += ["## Parallel coordination\n"]
    lines += _render_tool("store_finding")
    lines += _render_tool("get_findings")
    lines += _render_tool("send_message")
    lines += _render_tool("receive_messages")
    lines += _render_tool("idle_until_all_done")
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
        "Surface a question to the human queue. Response includes `chat_prompt` (question + options "
        "formatted for inline display) and, when `urgency='blocking'`, a `poll_instruction`. "
        "Dual-channel: filed in the dashboard AND shown in Claude Code chat — first answer wins. "
        "For blocking: display `chat_prompt` to the user, then poll `get_hitl_request(request_id)` "
        "every 30 s. If the user answers in chat, call `answer_hitl(request_id, answer)`. "
        "`normal`/`high` land in the dashboard without blocking the session.")
    lines += _render_tool("get_hitl_request",
        "Read-only: Poll a HITL request for the human's answer. Returns the row including `status` "
        "(`pending`/`answered`/`dismissed`) and `answer` text.")
    lines += _render_tool("answer_hitl",
        "Answer a pending HITL request programmatically. Marks it answered so the waiting session "
        "can resume. Use when the human answers in Claude Code chat rather than the dashboard.")
    lines += _render_tool("dismiss_hitl",
        "Dismiss a HITL request (won't-answer / no longer relevant).")
    lines += ["## Handoff & context\n"]
    lines += _render_tool("generate_handoff",
        "Read-only: Generate a context handoff document. `mode='full'` writes the complete L0/L1/L2 handoff. `mode='delta'` "
        "returns a compact session summary with completed items, pending items, and the next `/goal` string.")
    lines += _render_tool("get_context_block",
        "Read-only: Return a compact plain-text context block (north star, sprint, pending sprint items, recent tasks, recent "
        "decisions, active sessions). Use `mode='full'` to paste into a fresh Claude Code session; `mode='chat'` "
        "for a shorter paste into claude.ai.")
    lines += ["## Planning tools\n"]
    lines += _render_tool("fan_out_sprint_items",
        "Bulk-insert sprint items in one call — lets an orchestrator LLM decompose a goal "
        "into parallel work items without N sequential `add_sprint_item` calls. Pass a list "
        "of `{title, description?, group?, version?}` dicts; returns the list of new item IDs.")
    lines += _render_tool("get_planning_brief",
        "Read-only: Return a compact planning context (sprint, north star, pending items, "
        "in-progress items, recent tasks, active sessions, recent decisions, pending HITLs). "
        "No session registration needed — designed for planning chat sessions that need to see "
        "project state without side effects.")
    lines += _render_tool("analyze_sprint")
    lines += _render_tool("reconcile_sprint_drift",
        "Read-only: Cross-reference pending sprint items against recent git commits and return "
        "items that may already be done. confidence='high' means 3+ keywords overlap (safe to "
        "mark done via `complete_sprint_item`); confidence='medium' means 1–2 (verify first). "
        "Call during planning sessions to identify board drift.")
    lines += ["## Rate limits\n"]
    lines += [
        "\n",
        "The hosted MCP surface (Bearer-token requests) is metered per tenant per minute by plan:\n",
        "\n",
        "| Plan | Requests / minute |\n",
        "|------|-------------------|\n",
        "| `free` | 500 |\n",
        "| `standard` | 2000 |\n",
        "| `pro` | unlimited |\n",
        "\n",
        "Over-limit requests receive `429 Too Many Requests` with a `Retry-After` header. "
        "Dashboard (cookie) traffic, `/health`, and `/static` are never metered, and self-hosted "
        "instances are unmetered. Polling `get_sprint_progress` between tasks stays well within "
        "these limits — the 10 s server-side cache keeps parallel polling cheap.\n",
        "\n",
        "---\n",
        "\n",
    ]
    lines += ["## Notes\n"]
    lines += _render_tool("add_note",
        "Add a per-project wiki note. Use for setup instructions, gotchas, environment details, how-tos.")
    lines += _render_tool("get_notes",
        "Read-only: List project notes (newest first), LIGHTWEIGHT by default — id/slug/title/"
        "tags/kind/priority/timestamps with NO body, so the list can't overflow context. Pull "
        "model: scan the list, then `read_note(project_id, slug)` for one note's full body. Filter "
        "by tag substring or `query` full-text search. Pass `bodies=true` only when you truly need "
        "every body inline. Pass `limit` (default 100, max 500) and/or `cursor` for a "
        "`{notes, has_more, next_cursor}` page, then re-call with `cursor=next_cursor`.")
    lines += _render_tool("read_note",
        "Read-only: Fetch one project note's full body by its per-project `slug` (the `slug` field "
        "from `get_notes`). The pull half of the list→read model.")
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


async def _build_continue_payload(
    db: aiosqlite.Connection,
    project_id: str,
    session: dict[str, Any],
    *,
    source: str | None = None,
) -> dict[str, Any]:
    """c793377d — compact 'just continue' resume block.

    Returns only what an executor needs to pick up where it left off — the
    session_id, the live pending sprint items (scoped to the session's version),
    and the ready-to-paste /goal string — and deliberately skips the heavy
    L0/L1/L2 orientation (goal_xml / cache_blocks / instructions / workspace).
    Used both by the auto-detected heartbeat-window continuation and an explicit
    mode='continue'. Every enrichment is best-effort so a resume never fails.
    """
    scoped_version = session.get("sprint_version") or None
    try:
        _all = await db_module.get_sprint_items(
            db, project_id, include_human=False, version=scoped_version,
        )
    except Exception:  # noqa: BLE001
        _all = []
    pending = [it for it in _all if (it.get("status") in ("pending", "todo"))]
    try:
        _proj = await db_module.get_project(db, project_id)
        _mode = db_module.normalize_execution_mode((_proj or {}).get("execution_mode"))
    except Exception:  # noqa: BLE001
        _mode = "autonomous"
    try:
        _settings = await db_module.get_project_settings(db, project_id)
        _max_turns = handoff_module._max_turns_from_settings(_settings)
    except Exception:  # noqa: BLE001
        _max_turns = handoff_module._DEFAULT_GOAL_MAX_TURNS
    _hitl_mode = await _hitl_auto_answer_mode_safe(db, project_id)
    # `goal_string` (not `goal`) deliberately — consumers like hooks_session_start
    # treat a result `goal` key as the goal *dict* (north_star/sprint); this is the
    # ready-to-paste /goal *command* string, a distinct shape.
    goal_string = handoff_module._build_quick_start_goal(
        pending,
        version=scoped_version,
        execution_mode=_mode,
        max_turns=_max_turns,
        hitl_auto_answer_mode=_hitl_mode,
    )
    recent = await db_module.get_tasks(db, project_id, limit=5)
    pending_slim = [
        {
            "id": it.get("id"),
            "title": it.get("title"),
            "status": it.get("status"),
            "version": it.get("version"),
        }
        for it in pending
    ]
    return {
        "continuation": True,
        "mode": "continue",
        "session_id": session["id"],
        "session": session,
        "source": source,
        "sprint_version": scoped_version,
        "pending_items": pending_slim,
        "pending_count": len(pending_slim),
        "goal_string": goal_string,
        "recent_tasks": recent,
        "note": (
            "Continue mode — resumed without re-reading L0/L1/L2 context. Claim "
            "the first pending item and keep going; call "
            "get_session_brief(project_id) only if you need the full orientation."
        ),
    }


# ecf69de8 — protocol-level EXECUTION MODE directive. start_session leads the
# agent_instructions / orientation with this line so the posture is a structured
# instruction the session can't miss, not buried freetext. 'autonomous' tells the
# session to claim and run immediately; 'interactive' tells it to ask first.
_EXECUTION_MODE_DIRECTIVES = {
    "autonomous": (
        "EXECUTION MODE: autonomous — claim and run pending sprint items "
        "immediately, do not defer. Do NOT ask which item to start; begin with "
        "the first one now."
    ),
    "interactive": (
        "EXECUTION MODE: interactive — ask for direction before executing. "
        "Review the pending sprint items and confirm with the human which to "
        "start before claiming or changing anything."
    ),
}


def _execution_mode_directive(mode: str | None) -> str:
    """Return the protocol-level EXECUTION MODE directive line for a project.

    Unknown / missing modes fall back to the autonomous directive so the line is
    always present and the default posture is non-deferential.
    """
    normalized = db_module.normalize_execution_mode(mode)
    return _EXECUTION_MODE_DIRECTIVES.get(
        normalized, _EXECUTION_MODE_DIRECTIVES["autonomous"]
    )


# 72e12ed8 — HITL auto-answer directive surfaced in the start_session orientation
# so executors know whether request_hitl resolves inline or blocks for a human.
_HITL_MODE_DIRECTIVES = {
    0: ("HITL: auto-answer OFF — request_hitl queues for a human and "
        "urgency='blocking' pauses you until answered. Still ALWAYS use request_hitl "
        "to ask the human; never just ask in chat."),
    1: ("HITL: auto-answer SAFE — request_hitl resolves immediately for "
        "non-destructive questions and returns the answer inline (it does not "
        "block). Use it freely; require_human=true still routes to a human."),
    2: ("HITL: auto-answer AGGRESSIVE — request_hitl resolves immediately and "
        "returns the answer inline for nearly all questions. Use it freely; "
        "require_human=true still forces a human reply."),
}


def _hitl_mode_directive(mode: int) -> str:
    """Protocol-level HITL directive line for the project's auto-answer mode."""
    return _HITL_MODE_DIRECTIVES.get(int(mode or 0), _HITL_MODE_DIRECTIVES[0])


async def _hitl_auto_answer_mode_safe(db: aiosqlite.Connection, project_id: str) -> int:
    """Resolve a project's HITL auto-answer mode (0/1/2); 0 on any error."""
    try:
        return int(await db_module._project_hitl_auto_answer_mode(db, project_id))
    except Exception:  # noqa: BLE001
        return 0


async def _build_orchestration_hint(
    db: aiosqlite.Connection,
    project_id: str,
    version: str | None,
) -> dict[str, Any] | None:
    """a6cacfef — auto-orchestration hint attached to ``start_session``.

    Clusters the pending sprint items (via :func:`db.get_parallelizable_groups`,
    scoped to the session's ``version`` when set) and returns a compact
    ``orchestration`` block so a fresh session sees the recommended fan-out plan
    without calling ``get_parallelizable_groups`` manually.

    ``recommended_strategy`` is ``"parallel"`` only when at least one group holds
    more than one item (i.e. two or more items can genuinely run concurrently),
    else ``"sequential"``. Note many single-item groups (group_count > 1) means
    every item conflicts with the others and they must be serialized — that is
    ``"sequential"``, not parallel. Groups are returned in a compact form (id +
    title per item) to avoid bloating the orientation. Returns ``None`` when there
    is no eligible work to plan. NEVER raises — the caller wraps in try/except, and
    on any failure start_session simply ships no hint.
    """
    grouping = await db_module.get_parallelizable_groups(db, project_id, version)
    groups = grouping.get("groups") or []
    if not groups:
        return None
    group_count = grouping.get("group_count", len(groups))
    # "parallel" only when some group can actually run >1 item at once. Multiple
    # single-item groups are mutually-conflicting work → sequential.
    any_multi = any(len(g) > 1 for g in groups)
    strategy = "parallel" if any_multi else "sequential"
    compact_groups = [
        [
            # 1da83459 — cap title length to keep the orientation hint compact.
            {"id": it.get("id"), "title": (it.get("title") or "")[:80]}
            for it in group
        ]
        for group in groups
    ]
    hint: dict[str, Any] = {
        "recommended_strategy": strategy,
        "group_count": group_count,
        "eligible_count": grouping.get("eligible_count", 0),
        "groups": compact_groups,
    }
    blocked = grouping.get("blocked") or []
    if blocked:
        hint["blocked_count"] = len(blocked)
    # de730a25 — surface undeclared items prominently. They now each run in their
    # own sequential group (not co-scheduled), but the orchestrator should know
    # parallel safety couldn't be proven for them.
    undeclared = grouping.get("undeclared_count", 0)
    _warn = ""
    if undeclared:
        hint["undeclared_count"] = undeclared
        hint["warning"] = (
            f"{undeclared} item(s) lack resource declarations — parallel safety "
            "not guaranteed; each runs in its own sequential group. Add "
            "touches_resources to let them parallelize."
        )
        _warn = f" ⚠ {hint['warning']}"
    hint["note"] = (
        f"{grouping.get('eligible_count', 0)} pending item(s) cluster into "
        f"{group_count} conflict-free group(s); recommended strategy: "
        f"{strategy}. Call get_parallelizable_groups(project_id) for full detail."
        + _warn
    )
    return hint


# 9f6aec5f — codebase-context cache: project_id → (monotonic_ts, summary). The
# architecture call hits the tenant's live code-intel tunnel, so we memoize per
# project for a short TTL to keep start_session fast across rapid re-orientations.
_CODEBASE_CONTEXT_TTL = 600.0  # 10 minutes
_codebase_context_cache: dict[str, tuple[float, dict[str, Any]]] = {}

# 2c645647 — protocol-level directive prepended to agent_instructions when a
# healthy codebase index is available, so the executor reaches for the graph
# tools instead of blind filesystem reads/grep (advisory tool descriptions alone
# don't reliably steer it — this rides at the same level as the EXECUTION MODE
# directive).
CODEBASE_INDEX_DIRECTIVE = (
    "CODEBASE INDEX AVAILABLE: use codebase__search_graph / "
    "codebase__get_code_snippet BEFORE reading files. Do NOT use filesystem read "
    "or grep for code navigation — the index is faster and pre-loaded."
)


def _summarize_architecture(arch: dict[str, Any]) -> dict[str, Any] | None:
    """Distill a codebase-memory-mcp ``get_architecture`` payload into a compact
    orientation block (top packages / layers / hotspots / entry points).

    Defensive: the architecture schema varies by indexer version, so every field
    is optional and bad shapes degrade to an empty result (→ no block injected).
    """
    if not isinstance(arch, dict):
        return None

    def _names(value: Any, key: str = "name", limit: int = 8) -> list[str]:
        out: list[str] = []
        if isinstance(value, list):
            for item in value[:limit]:
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict):
                    label = item.get(key) or item.get("path") or item.get("symbol")
                    if isinstance(label, str) and label.strip():
                        out.append(label.strip())
        return out

    summary: dict[str, Any] = {}
    packages = _names(arch.get("packages"))
    layers = _names(arch.get("layers"))
    hotspots = _names(arch.get("hotspots"), key="symbol")
    entry_points = _names(arch.get("entry_points") or arch.get("entrypoints"))
    if packages:
        summary["packages"] = packages
    if layers:
        summary["layers"] = layers
    if hotspots:
        summary["hotspots"] = hotspots
    if entry_points:
        summary["entry_points"] = entry_points
    stats = arch.get("stats") or arch.get("summary")
    if isinstance(stats, dict):
        # Keep only small scalar counts (files/symbols/edges), never large blobs.
        small = {
            k: v for k, v in stats.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        if small:
            summary["stats"] = small
    return summary or None


def _truncate_codebase_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Compact-mode variant: keep only the few highest-signal entries so an
    executor's context isn't flooded. (9f6aec5f)"""
    out: dict[str, Any] = {}
    for key, limit in (("packages", 5), ("hotspots", 5), ("entry_points", 3)):
        vals = summary.get(key)
        if isinstance(vals, list) and vals:
            out[key] = vals[:limit]
    if "stats" in summary:
        out["stats"] = summary["stats"]
    return out


def _parse_tunnel_tool_text(result: Any) -> Any:
    """Extract and JSON-decode the text payload from a tunnel ``tools/call``
    result envelope (``{"content": [{"type": "text", "text": "..."}]}``).
    Returns the decoded object, or None on any shape/parse error."""
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return None
    first = content[0]
    if not isinstance(first, dict):
        return None
    text = first.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


async def _build_codebase_context(
    tenant_id: str | None, project_id: str, *, compact: bool
) -> dict[str, Any] | None:
    """Fetch + summarize the project's codebase architecture for start_session.

    Returns a ``codebase_context`` block (top packages / layers / hotspots /
    entry points) so an executor starts already knowing the code's shape — or
    None when there's nothing to inject. Gated on a live, healthy code-intel
    tunnel and memoized per project for ``_CODEBASE_CONTEXT_TTL``. NEVER raises:
    any failure (no tunnel, unhealthy slot, not indexed, bad payload) degrades to
    None so start_session is unaffected. (9f6aec5f)
    """
    if not tenant_id:
        return None
    from .routes import tunnel as _tunnel_mod  # noqa: PLC0415 — avoid import cycle
    # Code-intel slot must be connected AND not flagged unhealthy (d71ba2e7).
    if tenant_id not in _tunnel_mod._tunnel_code_sockets:
        return None
    if not _tunnel_mod._slot_is_healthy(tenant_id, "code"):
        return None

    now = time.monotonic()
    cached = _codebase_context_cache.get(project_id)
    if cached is not None and (now - cached[0]) < _CODEBASE_CONTEXT_TTL:
        summary = cached[1]
    else:
        try:
            result = await _tunnel_mod.call_tunnel_tool(
                tenant_id, "codebase__get_architecture", {}
            )
        except Exception:  # noqa: BLE001 — tunnel/tool error → no block
            return None
        arch = _parse_tunnel_tool_text(result)
        summary = _summarize_architecture(arch) if isinstance(arch, dict) else None
        if not summary:
            return None
        _codebase_context_cache[project_id] = (now, summary)

    block = _truncate_codebase_summary(summary) if compact else dict(summary)
    block["note"] = (
        "Codebase index summary (from code-intel). Use codebase__search_graph / "
        "codebase__get_code_snippet to drill in — no filesystem search needed."
    )
    return block


async def _start_session_composite(
    db: aiosqlite.Connection,
    project_id: str,
    session_name: str,
    data_dir: str,
    human_id: str | None = None,
    client_type: str | None = None,
    role: str | None = None,
    source: str | None = None,
    compact: bool = False,
    version: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Register + goal + tasks + sessions + handoff-check in one shot.

    Replaces the four-call cold-start sequence (register_session, get_goal,
    get_tasks, check handoff file) with a single call that returns everything
    a new session needs before touching anything.

    ``compact=True`` (3689f680) returns a slim orientation block — session_id,
    sprint focus + status counts, the 3 most recent tasks, and a board_change
    count — and skips the heavy goal_xml / cache_blocks / meridian_instructions
    / workspace payload that overflows an executor's context. Full context is
    available via ``compact=False`` or ``get_session_brief``.

    ``version`` (a76cb7c0) scopes the session to a sprint-version bucket (e.g.
    "v0.1.x"). When given, the orientation's sprint counts/items are filtered to
    that version and the scope is stored on the session so later calls (/goal)
    can reuse it. When omitted, the bucket with the most pending items is
    inferred; if there are none, the session is left unscoped (every version,
    legacy behaviour). The resolved value is returned as ``sprint_version``.

    G8.34 — If a session with the same ``session_name`` is still active and
    pinged a heartbeat within the last 5 minutes, return a compact
    continuation block (no new registration, no goal-block flood). Keyed on
    (project_id, session_name); NEVER on Mcp-Session-Id since ChatGPT
    regenerates that header per tool call.
    """
    # c793377d — "just continue" resume. Auto-detect uses the 5-min heartbeat
    # window; an explicit mode='continue' widens it so an executor can resume a
    # session it knows is still its own without re-reading L0/L1/L2 context.
    _continue_window = 7 * 24 * 60 if (mode or "").lower() == "continue" else 5
    existing = await _find_continuation_session(
        db, project_id, session_name, max_idle_minutes=_continue_window
    )
    if existing is not None:
        return await _build_continue_payload(db, project_id, existing, source=source)
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
    # a76cb7c0 — resolve the sprint-version scope: explicit `version` wins;
    # otherwise infer the bucket with the most pending items. None (no pending
    # items / no versioned items) leaves the session unscoped (all versions).
    scoped_version = version
    if scoped_version is None:
        try:
            scoped_version = await db_module.infer_active_sprint_version(
                db, project_id
            )
        except Exception:
            scoped_version = None
    session = await db_module.register_session(
        db, project_id, session_name, human_id=human_id, client_type=client_type,
        sprint_version=scoped_version,
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

    # 3689f680 — compact orientation: skip the heavy goal_xml / cache_blocks /
    # instructions / workspace payload (the part that overflows an executor's
    # context) and return just enough to start working.
    if compact:
        # a76cb7c0 — scope the counts/board_change to the session's sprint
        # version when one was resolved, so an executor sees only its bucket.
        _c_items = await db_module.get_sprint_items(
            db, project_id, include_human=(role != "executor"),
            version=scoped_version,
        )
        _c_counts: dict[str, int] = {}
        for _it in _c_items:
            _st = _it.get("status") or "pending"
            _c_counts[_st] = _c_counts.get(_st, 0) + 1
        _c_started = str(session.get("created_at") or "")
        _c_board_change_n = sum(
            1 for _it in _c_items if (_it.get("added_at") or "") > _c_started
        )
        _c_board_change: int | dict[str, Any] = (
            {
                "new_items_since_session_start": _c_board_change_n,
                "message": (
                    f"{_c_board_change_n} new sprint item"
                    f"{'s' if _c_board_change_n != 1 else ''} already in queue — "
                    "call get_sprint_progress(project_id, session_id) now to see them."
                ),
            }
            if _c_board_change_n > 0
            else 0
        )
        _c_sprint = (goal or {}).get("sprint") if goal else None
        # ecf69de8 — resolve the project's executor posture so the compact
        # orientation carries it at the protocol level too.
        _c_project = await db_module.get_project(db, project_id)
        _c_mode = db_module.normalize_execution_mode(
            (_c_project or {}).get("execution_mode")
        )
        _c_mode_directive = _execution_mode_directive(_c_mode)
        # 72e12ed8 — surface the HITL auto-answer mode so executors know upfront
        # that request_hitl resolves inline (1/2) vs. blocks for a human (0).
        _c_hitl_mode = await _hitl_auto_answer_mode_safe(db, project_id)
        # 331896e1 — explicit "execute immediately" signal: when the board has
        # pending work, the session is scoped to a version, and the posture is
        # autonomous, tell the executor to claim+run now instead of asking what
        # to work on. The boolean is machine-checkable; the signal string is the
        # human-readable nudge naming the first item.
        _c_pending = _c_counts.get("pending", 0) + _c_counts.get("todo", 0)
        _c_execute_now = bool(
            _c_pending > 0 and scoped_version and _c_mode == "autonomous"
        )
        _c_execute_signal = None
        if _c_execute_now:
            _c_first = next(
                (it for it in _c_items if it.get("status") in ("pending", "todo")),
                None,
            )
            _c_execute_signal = (
                f"{_c_pending} pending item(s) in sprint {scoped_version!r}. "
                "EXECUTE NOW: claim the first unclaimed item and start working "
                "immediately — do not ask what to work on."
                + (
                    f" First up: {(_c_first.get('title') or '')[:100]}"
                    if _c_first else ""
                )
            )
        # dc462628 — surface GitHub connection status so executors know upfront
        # whether search_code / read_file / etc. are usable for this project.
        _c_github_repo = (_c_project or {}).get("github_repo") or ""
        _c_github_branch = (_c_project or {}).get("github_branch") or "main"
        _c_github_status = (
            f"connected (repo: {_c_github_repo}, branch: {_c_github_branch})"
            if _c_github_repo
            else "not connected — GitHub tools (search_code, read_file, etc.) will error until you connect a repo in Settings"
        )
        from datetime import datetime as _dt, timezone as _tz  # de193a81
        _c_now = _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")
        _c_payload = {
            "session_id": session["id"],
            "compact": True,
            # de193a81 — anchor the executor to the real date/time so a session
            # spanning multiple calendar days doesn't drift on "today".
            "current_timestamp": _c_now,
            "execution_mode": _c_mode,  # ecf69de8 — structured posture field
            "execution_mode_directive": _c_mode_directive,
            "execute_immediately": _c_execute_now,
            "execute_immediately_signal": _c_execute_signal,
            "hitl_auto_answer_mode": _c_hitl_mode,
            "hitl_auto_answer_directive": _hitl_mode_directive(_c_hitl_mode),
            "github_status": _c_github_status,
            "sprint": (str(_c_sprint)[:300] if _c_sprint else None),
            "sprint_version": scoped_version,
            "sprint_summary": {
                "total": len(_c_items),
                "done": _c_counts.get("done", 0),
                "in_progress": _c_counts.get("in_progress", 0),
                "pending": _c_pending,
            },
            # 1da83459 — truncate each task's description so a verbose executor
            # log can't bloat the compact orientation (context-budget guard).
            "recent_tasks": [
                {**_t, "description": ((_t.get("description") or "")[:200])}
                for _t in recent_tasks[:3]
            ],
            "board_change": _c_board_change,
            "note": (
                "Compact orientation. For full goal/decisions/instructions call "
                "start_session(compact=False) or get_session_brief(project_id)."
                + (
                    f" Scoped to sprint version {scoped_version!r} — sprint counts/items "
                    "are filtered to this bucket."
                    if scoped_version else ""
                )
            ),
        }
        # Per-project agent instructions are small but behaviorally critical
        # (custom rules the session must follow), so keep them even in compact.
        # ecf69de8 — lead with the EXECUTION MODE directive so the posture is the
        # first protocol-level instruction the session reads.
        # ddd8b9bf — also prepend HITL mode directive so executors know upfront
        # whether request_hitl blocks or auto-resolves.
        _c_agent = await db_module.get_agent_instructions(db, project_id)
        _c_hitl_directive = _hitl_mode_directive(_c_hitl_mode)
        _c_combined_directive = f"{_c_mode_directive}\n\n{_c_hitl_directive}"
        _c_payload["agent_instructions"] = (
            f"{_c_combined_directive}\n\n{_c_agent}" if _c_agent else _c_combined_directive
        )
        # File conflict warnings must surface in compact mode — an executor that
        # misses them will silently overwrite another session's uncommitted work.
        _c_file_warnings = await db_module.get_file_conflict_warnings(
            db, project_id, session["id"]
        )
        if _c_file_warnings:
            _c_payload["file_warnings"] = _c_file_warnings
        # a6cacfef — when pending items exist, surface the recommended fan-out
        # plan so a fresh session sees the strategy without a manual call. Never
        # let grouping break start_session (degrade to no hint).
        _c_pending = _c_payload["sprint_summary"]["pending"]
        if _c_pending > 0:
            try:
                _c_orch = await _build_orchestration_hint(
                    db, project_id, scoped_version
                )
                if _c_orch is not None:
                    _c_payload["orchestration"] = _c_orch
            except Exception:
                pass
        return _c_payload

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
    # a76cb7c0 — scope to the session's resolved sprint version when present.
    active_statuses = ("todo", "pending", "in_progress")
    all_sprint_items = await db_module.get_sprint_items(
        db, project_id, include_human=(role != "executor"),
        version=scoped_version,
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
    # ecf69de8 — lead them with the protocol-level EXECUTION MODE directive so the
    # executor posture (autonomous vs interactive) is the first instruction read.
    execution_mode = db_module.normalize_execution_mode(
        (project or {}).get("execution_mode")
    )
    mode_directive = _execution_mode_directive(execution_mode)

    # 72e12ed8 — HITL auto-answer mode in the full orientation too.
    _hitl_mode = await _hitl_auto_answer_mode_safe(db, project_id)
    # ddd8b9bf — also prepend HITL mode directive so executors know upfront whether
    # request_hitl blocks or auto-resolves (changes executor behavior significantly).
    hitl_directive = _hitl_mode_directive(_hitl_mode)
    combined_directive = f"{mode_directive}\n\n{hitl_directive}"
    agent_instructions = await db_module.get_agent_instructions(db, project_id)
    agent_instructions = (
        f"{combined_directive}\n\n{agent_instructions}"
        if agent_instructions
        else combined_directive
    )
    # 331896e1 — explicit execute-immediately signal (see compact path).
    _exec_now = bool(
        pending_items and scoped_version and execution_mode == "autonomous"
    )
    _exec_signal = None
    if _exec_now:
        _first_p = pending_items[0] if isinstance(pending_items[0], dict) else {}
        _exec_signal = (
            f"{len(pending_items)} pending item(s) in sprint {scoped_version!r}. "
            "EXECUTE NOW: claim the first unclaimed item and start working "
            "immediately — do not ask what to work on."
            + (
                f" First up: {(_first_p.get('title') or '')[:100]}"
                if _first_p else ""
            )
        )
    payload: dict[str, Any] = {
        "session_id": session["id"],
        "execution_mode": execution_mode,  # ecf69de8 — structured posture field
        "execution_mode_directive": mode_directive,
        "execute_immediately": _exec_now,
        "execute_immediately_signal": _exec_signal,
        "hitl_auto_answer_mode": _hitl_mode,
        "hitl_auto_answer_directive": _hitl_mode_directive(_hitl_mode),
        "sprint_version": scoped_version,  # a76cb7c0 — resolved scope (or None)
        "goal": goal,
        "goal_xml": goal_xml,  # v0.6.1 — always present
        "goal_cache_blocks": goal_cache_blocks,  # v0.6.2 — ready for Anthropic
        "sprint_items": pending_items,  # v1.1 — active checklist (scoped)
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
    # 1c4fdd6c — sprint-drift guard: if items are already in_progress, remind the
    # session to mark each done with complete_sprint_item (the top drift cause).
    _inprog = [it for it in all_sprint_items if it.get("status") == "in_progress"]
    if _inprog:
        _n_ip = len(_inprog)
        payload["in_progress_reminder"] = (
            f"{_n_ip} sprint item{'s are' if _n_ip != 1 else ' is'} already "
            "in_progress on this board. As you finish each, call "
            "complete_sprint_item(item_id) — items are never auto-reconciled from "
            "git, so forgetting this is what drifts the board."
        )
    if file_warnings:
        payload["file_warnings"] = file_warnings
    # a6cacfef — auto-orchestration hint: when there are pending items, attach the
    # recommended fan-out strategy + grouped plan (scoped to the session's sprint
    # version). Wrapped so a grouping failure never breaks start_session.
    if pending_items:
        try:
            orchestration = await _build_orchestration_hint(
                db, project_id, scoped_version
            )
            if orchestration is not None:
                payload["orchestration"] = orchestration
        except Exception:
            pass
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


def _normalize_hook_cwd_path(path: str) -> str:
    """Normalize a hook cwd to the canonical form used in repo_paths matching.

    Mirrors the nested ``_normalize_hook_cwd`` in ``hooks_session_start`` so the
    stop hook resolves a project from a cwd the exact same way session-start
    does (WSL /mnt/c/... → C:/..., backslashes → forward slashes, no trailing /).
    """
    value = (path or "").strip().replace("\\", "/")
    m = re.match(r"^/mnt/([a-zA-Z])(?:/(.*))?$", value)
    if m:
        drive = m.group(1).upper()
        rest = (m.group(2) or "").strip("/")
        value = f"{drive}:/{rest}" if rest else f"{drive}:/"
    return value.rstrip("/")


async def _resolve_hook_project_id(
    db: Any, cwd: str, hostname: str
) -> str | None:
    """Resolve a project id from a hook's cwd/hostname (best-effort, never raises).

    Mirrors the cwd/hostname routing the SessionStart hook uses so the Stop
    hook can target the same project when no explicit ``project_id`` is given:

    * Pass 1 — exact cwd+hostname match in a project's ``repo_paths`` (and the
      legacy single ``repo_path``), so cwd routing wins.
    * Pass 2 — hostname registered in any project's machine ``hostnames`` list.
    * Fallback — if exactly one project exists, route to it.

    Returns the project id, or ``None`` when nothing matches.
    """
    norm_cwd = _normalize_hook_cwd_path(cwd).lower()
    norm_hn = (hostname or "").strip().lower()
    if not norm_cwd and not norm_hn:
        return None
    try:
        projects = await db_module.list_projects(db)
    except Exception:  # noqa: BLE001
        return None
    if not projects:
        return None

    def _cfg(p: dict[str, Any]) -> dict[str, Any]:
        cfg = p.get("executor_config") or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except Exception:  # noqa: BLE001
                cfg = {}
        return cfg if isinstance(cfg, dict) else {}

    # Pass 1: exact cwd (+ optional hostname) match in repo_paths / legacy repo_path.
    if norm_cwd:
        for p in projects:
            cfg = _cfg(p)
            for rp in (cfg.get("repo_paths") or []):
                rp_cwd = _normalize_hook_cwd_path(rp.get("cwd", "")).lower()
                rp_host = (rp.get("hostname") or "").lower()
                if rp_cwd == norm_cwd and (not rp_host or rp_host == norm_hn):
                    return p["id"]
            legacy_rp = (cfg.get("repo_path") or "").strip()
            if legacy_rp and _normalize_hook_cwd_path(legacy_rp).lower() == norm_cwd:
                return p["id"]
    # Pass 2: hostname registered at machine level (hostnames list).
    if norm_hn:
        for p in projects:
            cfg = _cfg(p)
            if any((h.get("hostname") or "").lower() == norm_hn for h in (cfg.get("hostnames") or [])):
                return p["id"]
    # Fallback: a single-project workspace is unambiguous.
    if len(projects) == 1:
        return projects[0]["id"]
    return None


@app.post("/hooks/stop")
async def hooks_stop(body: dict[str, Any], request: Request) -> dict[str, Any]:
    """Claude Code / Codex Stop hook.

    Accepts {project_id?, session_id?, cwd?, hostname?}. Fires auto_capture +
    a delta handoff so a delta is produced even when the executor disconnects
    without calling generate_handoff itself.

    Best-effort / non-blocking: this never raises out and never returns 4xx/5xx
    for a missing session — a missing/unresolvable session yields
    ``{"ok": true, "handoff": null, "reason": "no session"}`` and any
    generate_handoff failure yields ``{"ok": true, "handoff": null, "error": ...}``
    (both 200). The agent does not wait for this before exiting.

    Resolution: an explicit ``session_id`` is preferred; otherwise the
    most-recent active session for the project (resolved from ``project_id`` or,
    failing that, the ``cwd``/``hostname`` the same way the SessionStart hook
    routes) is handed off.

    Hosted callers can authenticate with Authorization: Bearer sk_meridian_...
    to route directly to their tenant DB. Local/browser-session behavior is
    unchanged when no Bearer token is supplied.
    """
    project_id = (body.get("project_id") or "").strip()
    session_id = (body.get("session_id") or "").strip() or None
    hook_cwd = (body.get("cwd") or "").strip()
    hook_hostname = (body.get("hostname") or "").strip()
    # 571b8b60 — Claude Code passes the transcript path; a bounded read of its
    # assistant text turns enriches the delta handoff below.
    transcript_path = (body.get("transcript_path") or "").strip()
    # Resolve the tenant DB first so a bad Bearer token still 401s (hosted mode);
    # everything after this point is best-effort and only ever returns 200.
    db = await _resolve_hook_db(request)
    # No explicit project — try to route by cwd/hostname like session-start does.
    if not project_id:
        project_id = await _resolve_hook_project_id(db, hook_cwd, hook_hostname) or ""
    if not project_id:
        # Nothing identifies a project (no id, no cwd/hostname match) — can't act.
        return {"ok": False, "error": "project_id required"}
    # Resolve the session to hand off: explicit id wins, else most-recent active.
    if not session_id:
        try:
            active = await db_module.get_sessions(db, project_id, active_only=True)
            if active:
                session_id = active[0].get("id") or None
        except Exception:  # noqa: BLE001
            session_id = None
    if not session_id:
        # Best-effort: no session to summarise — never an error.
        return {"ok": True, "handoff": None, "reason": "no session"}
    # Bucket done tasks + finalize any session markdown (both guarded).
    try:
        await db_module.auto_capture_session(db, project_id, session_id)
    except Exception:  # noqa: BLE001
        pass
    try:
        await _finalize_session_md(db, project_id, session_id)
    except Exception:  # noqa: BLE001
        pass
    # Produce the delta handoff inline but fully guarded — any failure (and the
    # timeout) returns cleanly with handoff null so the hook never blocks/errors.
    try:
        from . import handoff as handoff_module_local
        # 571b8b60 — bounded transcript read (local file, capped) → work
        # narrative folded into the delta body. Guarded: any failure yields ""
        # and the handoff falls back to the plain delta.
        _narrative = ""
        if transcript_path:
            try:
                _narrative = handoff_module_local.extract_transcript_narrative(
                    transcript_path
                )
            except Exception:  # noqa: BLE001
                _narrative = ""
        path, _content = await asyncio.wait_for(
            handoff_module_local.generate_handoff(
                db, project_id, _data_dir(request), mode="delta",
                session_id=session_id, extra_narrative=_narrative or None,
            ),
            timeout=20.0,
        )
        return {
            "ok": True,
            "handoff": {
                "mode": "delta", "path": path,
                "transcript_narrative": bool(_narrative),
            },
        }
    except Exception as exc:  # noqa: BLE001
        import logging as _hook_logging
        _hook_logging.getLogger("meridian.hooks").info(
            "hooks/stop delta handoff failed for project=%s session=%s: %r",
            project_id, session_id, exc,
        )
        return {"ok": True, "handoff": None, "error": str(exc)}


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


@app.delete("/api/keys/orphaned")
async def delete_orphaned_oauth_keys(request: Request) -> dict[str, Any]:
    """Purge this tenant's orphaned OAuth API keys (``label='oauth'``, >24h old).

    Claude Code's MCP ``authorization_code`` flow mints an ``oauth``-labelled
    bearer token at ``/oauth/token`` even when the redirect back to the local
    callback fails; the user retries, orphaning the previous token. This sweeps
    those stale rows so the tenant's key list doesn't accumulate dead entries.
    Recent tokens (<24h) are left untouched in case one is still in use.
    Returns ``{"deleted": <count>}``.
    """
    tenant = await _get_authenticated_tenant(request)
    db = request.app.state.db
    deleted = await db_module.delete_orphaned_oauth_tokens(db, tenant["id"])
    return {"deleted": deleted}


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
# Tunnel device-code auth  (`meridian --tunnel` browser login flow)
# ---------------------------------------------------------------------------

import time as _tc_time

# {device_code: (raw_token, expires_epoch)}
_tunnel_device_codes: dict[str, tuple[str, float]] = {}
_TUNNEL_DEVICE_CODE_TTL = 600  # 10 minutes


def _cleanup_tunnel_device_codes() -> None:
    now = _tc_time.time()
    expired = [k for k, (_, exp) in list(_tunnel_device_codes.items()) if now > exp]
    for k in expired:
        _tunnel_device_codes.pop(k, None)


@app.get("/auth/tunnel-connect", response_class=HTMLResponse)
async def tunnel_connect_page(request: Request) -> Any:
    """Device-code page for `meridian --tunnel` browser auth.

    If the user has a session cookie, renders an Authorize button.
    If not authenticated, redirects to /auth/login with a next= return URL.
    """
    if not _hosted_mode():
        raise HTTPException(status_code=404, detail="not available in self-hosted mode")

    device_code = request.query_params.get("device_code", "").strip()
    if not device_code:
        raise HTTPException(status_code=400, detail="device_code required")

    tenant = None
    try:
        tenant = await _get_authenticated_tenant(request)
    except HTTPException:
        pass

    if tenant is None:
        from urllib.parse import quote as _q
        next_path = f"/auth/tunnel-connect?device_code={_q(device_code, safe='')}"
        return RedirectResponse(url=f"/auth/login?next={_q(next_path, safe='/')}", status_code=302)

    email = tenant.get("email", "")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Authorize Tunnel — Meridian</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#x1F9ED;</text></svg>">
  <style>
    body{{font-family:system-ui,sans-serif;background:#0d0d0d;color:#e8e8e8;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
    .card{{background:#1a1a1a;border:1px solid #2e2e2e;border-radius:12px;padding:32px;max-width:480px;width:100%;text-align:center}}
    h2{{margin:0 0 8px;font-size:20px}}
    .sub{{color:#888;margin:0 0 16px;font-size:14px;line-height:1.5}}
    .email{{color:#60a5fa;font-size:13px;margin-bottom:24px}}
    .btn{{padding:12px 32px;border-radius:8px;border:none;background:#3b82f6;color:#fff;cursor:pointer;font-size:15px;font-weight:600;width:100%;transition:background 0.15s}}
    .btn:hover{{background:#2563eb}}
    .btn:disabled{{background:#374151;cursor:default;color:#6b7280}}
    .note{{font-size:12px;color:#555;margin-top:16px;line-height:1.5}}
    .success{{display:none;color:#4ade80;font-size:15px;margin-top:16px;font-weight:500}}
    .err{{display:none;color:#f87171;font-size:13px;margin-top:12px}}
  </style>
</head>
<body>
  <div class="card">
    <h2>Authorize Meridian Tunnel</h2>
    <p class="sub"><code>meridian --tunnel</code> is requesting access to relay requests through your account.</p>
    <p class="email">Signed in as {email}</p>
    <button class="btn" id="authBtn" onclick="authorize()">Authorize tunnel access</button>
    <div class="err" id="err"></div>
    <div class="success" id="ok">&#x2713; Authorized! You can close this tab.</div>
    <p class="note">This grants your local tunnel client access. You can revoke it any time from the dashboard under API tokens.</p>
  </div>
  <script>
    async function authorize() {{
      const btn = document.getElementById('authBtn');
      btn.disabled = true;
      btn.textContent = 'Authorizing…';
      try {{
        const r = await fetch('/auth/tunnel-connect', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{device_code: '{device_code}'}})
        }});
        if (r.ok) {{
          btn.style.display = 'none';
          document.getElementById('ok').style.display = 'block';
        }} else {{
          const j = await r.json().catch(() => ({{}}));
          document.getElementById('err').textContent = j.detail || 'Authorization failed.';
          document.getElementById('err').style.display = 'block';
          btn.disabled = false;
          btn.textContent = 'Authorize tunnel access';
        }}
      }} catch (e) {{
        document.getElementById('err').textContent = 'Network error. Please try again.';
        document.getElementById('err').style.display = 'block';
        btn.disabled = false;
        btn.textContent = 'Authorize tunnel access';
      }}
    }}
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.post("/auth/tunnel-connect")
async def tunnel_connect_authorize(request: Request) -> dict[str, Any]:
    """Complete the device-code flow — create a tunnel token and register it for polling."""
    if not _hosted_mode():
        raise HTTPException(status_code=404, detail="not available in self-hosted mode")

    tenant = await _get_authenticated_tenant(request)

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    device_code = (body.get("device_code") or "").strip()
    if not device_code:
        raise HTTPException(status_code=400, detail="device_code required")

    _cleanup_tunnel_device_codes()
    if device_code in _tunnel_device_codes:
        return {"status": "ok"}  # idempotent

    # One active tunnel-cli token per tenant (overwrites any previous one).
    db = request.app.state.db
    await db_module.delete_api_tokens_by_label(db, tenant["id"], "tunnel-cli")
    raw_token, _ = await db_module.create_api_token(db, tenant["id"], label="tunnel-cli")

    _tunnel_device_codes[device_code] = (raw_token, _tc_time.time() + _TUNNEL_DEVICE_CODE_TTL)
    return {"status": "ok"}


@app.get("/auth/tunnel-poll")
async def tunnel_connect_poll(request: Request) -> dict[str, Any]:
    """Poll endpoint for the tunnel device-code flow.

    Returns ``{"status": "pending"}`` until the user clicks Authorize, then
    ``{"status": "complete", "token": "sk_meridian_..."}`` exactly once
    (the device code is consumed on first successful read).
    """
    if not _hosted_mode():
        raise HTTPException(status_code=404, detail="not available in self-hosted mode")

    device_code = request.query_params.get("device_code", "").strip()
    if not device_code:
        raise HTTPException(status_code=400, detail="device_code required")

    _cleanup_tunnel_device_codes()

    if device_code not in _tunnel_device_codes:
        return {"status": "pending"}

    raw_token, _ = _tunnel_device_codes.pop(device_code)
    return {"status": "complete", "token": raw_token}


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
    "read_file", "patch_file", "list_files", "search_code", "get_commits", "get_commit", "search_commits",
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
    "patch_file": "Patch File",
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
    """Return the GitHub tool defs if the tenant has a GitHub PAT set."""
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
            "name": "patch_file",
            "description": (
                "Make a targeted edit to a file in the project's connected GitHub repo: "
                "replace an exact substring (old_str) with new_str and commit it. old_str "
                "must match the current file contents exactly (including whitespace) and "
                "appear exactly once. Sends only the changed snippet, so it edits very "
                "large files trivially — the targeted-write counterpart to read_file."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **_pid_prop,
                    "file_path": {"type": "string", "description": "File path relative to repo root (e.g. src/main.py)"},
                    "old_str": {"type": "string", "description": "Exact substring to replace — must appear exactly once in the file."},
                    "new_str": {"type": "string", "description": "Replacement text (may be empty to delete old_str)."},
                    "branch": {"type": "string", "description": "Branch to read + commit on (default: the project's configured branch)."},
                    "message": {"type": "string", "description": "Commit message (default: 'patch_file: update <path>')."},
                    "session_id": {"type": "string", "description": "Caller session ID — if supplied, Meridian rejects the patch when the file is locked by a different session."},
                },
                "required": ["project_id", "file_path", "old_str", "new_str"],
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
            "description": "Search code in the project's connected GitHub repository using GitHub code search. Returns helpful error if no GitHub repo is connected.",
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


# MCP tool dispatcher moved to meridian/mcp/handler.py. Re-exported here so
# server.py's HTTP routes and mcp.stdio_handler keep importing from .server.
from .mcp.handler import (  # noqa: E402
    _dispatch_github_tool,
    _handle_mcp_request,
    _dispatch_mcp_tool,
    _maybe_add_log_task_nudge,
)

# ---------------------------------------------------------------------------
# 9768d806 — MCP SSE transport (for dnakov/claude-mcp Chrome extension)
# ---------------------------------------------------------------------------
# Protocol: GET /mcp/sse opens an SSE stream, receives "endpoint" event with
# the POST URL. Client POSTs JSON-RPC to POST /mcp/sse?session_id=<uuid> and
# reads the JSON response directly from the HTTP response body.

_SSE_SESSIONS: dict[str, dict[str, Any]] = {}  # session_id → {db, queue}

@app.get("/mcp")
async def _mcp_get(request: Request):
    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept:
        return _RR("/mcp/sse")
    return JSONResponse(
        {"name": "meridian", "version": "1.0", "transport": "http+sse"},
        headers={"Cache-Control": "no-store, no-cache"},
    )



_SSE_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Cache-Control": "no-cache, no-store",
    "X-Accel-Buffering": "no",
}

# bb16f9a7 — SSE keepalive heartbeat.
# Idle SSE connections get dropped by intermediary proxies (Fly / Cloudflare
# have ~60s idle-read timeouts). This wrapper races an interval timer against
# the upstream generator: whenever the upstream produces no frame within
# ``interval`` seconds, it emits a single SSE comment ping (``: ping\n\n``) to
# keep the socket warm, then resumes waiting on the upstream. Real data always
# passes through promptly — the ping only fires on genuine idle gaps, never
# alongside flowing data. Kept as a standalone async-generator so it can be
# unit-tested with a fake upstream and a short interval (no live connection).
_SSE_HEARTBEAT_FRAME = ": ping\n\n"


async def _with_sse_heartbeat(upstream, interval: float = 30.0):
    """Yield frames from ``upstream``, injecting a ``: ping`` every ``interval``s of idle.

    ``upstream`` is any async iterator of already-framed SSE strings/bytes. The
    heartbeat is a bare SSE comment line, which every compliant client silently
    ignores, so it never disturbs the MCP framing. When the upstream is
    exhausted (client disconnect / normal completion) the wrapper stops too.
    """
    it = upstream.__aiter__()
    nxt = asyncio.ensure_future(it.__anext__())
    try:
        while True:
            try:
                # shield() protects the in-flight read from wait_for's timeout
                # cancellation, so we can keep awaiting the SAME read across
                # successive heartbeat intervals on a fully idle upstream.
                frame = await asyncio.wait_for(asyncio.shield(nxt), timeout=interval)
            except asyncio.TimeoutError:
                # wait_for timed out. But the read may have *just* completed in
                # the same tick (its result/exception raced the timer) — if so,
                # deliver that instead of a spurious ping.
                if nxt.done():
                    exc = nxt.exception()
                    if isinstance(exc, StopAsyncIteration):
                        return
                    if exc is not None:
                        raise exc
                    frame = nxt.result()
                    yield frame
                    nxt = asyncio.ensure_future(it.__anext__())
                    continue
                # Genuinely idle — emit one keepalive and loop, re-waiting on the
                # still-pending read. A perpetually idle upstream thus pings
                # exactly once per interval, indefinitely.
                yield _SSE_HEARTBEAT_FRAME
                continue
            except StopAsyncIteration:
                return
            yield frame
            # Frame delivered — start the next read.
            nxt = asyncio.ensure_future(it.__anext__())
    finally:
        # Don't leak the pending read if the consumer closes us mid-flight.
        if not nxt.done():
            nxt.cancel()


@app.options("/mcp/sse")
async def mcp_sse_options(request: Request) -> Response:
    """CORS preflight for chrome-extension:// origin."""
    return Response(status_code=204, headers=_SSE_CORS_HEADERS)


@app.get("/mcp/sse")
async def mcp_sse_get(request: Request) -> StreamingResponse:
    """MCP SSE transport GET — opens event stream for dnakov/claude-mcp.

    Sends ``event: endpoint`` with POST URL, then a keepalive ``: ping`` every
    ~30 s while idle (bb16f9a7) so intermediary proxies (Fly / Cloudflare) don't
    drop the idle connection. No strict auth required: uses the same _db()
    resolver (Bearer / cookie / local fallback) so both local and hosted-tier
    clients work.
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

    async def _source():
        # Real SSE frames for this transport. Currently just the initial
        # ``endpoint`` event; afterwards the stream idles (the client POSTs its
        # JSON-RPC out-of-band and reads responses from the POST body), so this
        # generator parks on a disconnect poll. bb16f9a7: the ~30s keepalive is
        # supplied by _with_sse_heartbeat wrapping this source, so we no longer
        # emit manual heartbeats here.
        try:
            yield f"event: endpoint\ndata: {endpoint_path}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                await _anyio.sleep(1)
        finally:
            _SSE_SESSIONS.pop(session_id, None)

    return StreamingResponse(
        _with_sse_heartbeat(_source(), interval=30),
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
    import time as _tm_local  # noqa: PLC0415
    from .routes import oauth as _oa_mod  # noqa: PLC0415
    _auth = request.headers.get("authorization", "")
    _bearer = _auth.removeprefix("Bearer ").strip()
    _bearer_hash = _oa_mod._oauth_token_hash(_bearer) if _bearer else ""  # noqa: SLF001
    _td = _oa_mod._oa_tokens.get(_bearer_hash) if _bearer_hash else None  # noqa: SLF001
    if _td is None and _bearer_hash:
        _td = await _oa_mod._get_oauth_token_from_db(request.app.state.db, _bearer_hash)  # noqa: SLF001
        if _td is not None and not _td.get("_is_api_token"):
            _oa_mod._oa_tokens[_bearer_hash] = _td  # noqa: SLF001
    if _td is not None and not _td.get("_is_api_token"):
        if _tm_local.time() > _td.get("exp", 0):
            _oa_mod._oa_tokens.pop(_bearer_hash, None)  # noqa: SLF001
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
            except Exception as _db_err:
                import logging as _mcp_log
                _mcp_log.getLogger("meridian.mcp").error(
                    "[mcp] DB routing failed for tenant %s: %s — request will use auth DB (HITL bug source)",
                    _oa_tenant_id, _db_err,
                )
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

    # 393eed0a — compute a workspace-role gate only when an X-Workspace-Tenant-Id
    # header rides along (an API-token client targeting an invited workspace). The
    # claude.ai connector never sends it, so this is a no-op (no DB hit) on the
    # hot path.
    _enf_role = None
    # 95499c3e — project-scope ids for API-token callers (mirrors the HTTP
    # middleware): a scoped member's token is 403'd on out-of-scope projects at
    # the MCP dispatch layer. Both only fire when an X-Workspace-Tenant-Id header
    # rides along (the claude.ai connector never sends it → no-op, no DB hit).
    _scoped_pids = None
    if request.headers.get("x-workspace-tenant-id", "").strip():
        try:
            _ctx = await _enforcement_context(request)
            if _ctx is not None:
                _enf_role = _ctx[2]
        except Exception:
            _enf_role = None
        try:
            from ._deps import _scoped_project_ids_for_request as _scoped_fn  # noqa: PLC0415
            _scoped_pids = await _scoped_fn(request)
        except Exception:
            _scoped_pids = None

    if isinstance(body, list):
        results = [await _handle_mcp_request(item, db, data_dir, tenant=tenant, token_type=_token_type, enforce_role=_enf_role, scoped_project_ids=_scoped_pids) for item in body]
        return JSONResponse(results)

    result = await _handle_mcp_request(body, db, data_dir, tenant=tenant, token_type=_token_type, enforce_role=_enf_role, scoped_project_ids=_scoped_pids)
    return JSONResponse(result)

# ---------------------------------------------------------------------------
# MCP server — implementation lives in meridian/mcp/stdio_handler.py
# ---------------------------------------------------------------------------

from .mcp.stdio_handler import build_mcp_server  # noqa: F401
