"""FastAPI HTTP server and MCP stdio server for Meridian.

This module exposes two surfaces backed by the same async SQLite database:

* A FastAPI app (``app``) reachable on port 7878 by default. Used by the
  demo script and any HTTP client.
* An MCP server (built in :func:`build_mcp_server`) reachable via stdio.
  Wired up in :mod:`meridian.__main__` and consumed by Claude Desktop /
  Claude Code / Cursor / Windsurf.
"""

from __future__ import annotations

import json
import asyncio
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from . import dashboard as dashboard_module
from . import db as db_module
from . import goal_md as goal_md_module
from . import enqueue as enqueue_module
from . import handoff as handoff_module
from .models import (
    ChatHistoryItem,
    ChatRequest,
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
    Session,
    SessionRegister,
    SetNorthStarRequest,
    SetSprintRequest,
    StartSessionRequest,
    Task,
    TaskCreate,
    TaskUpdate,
)

# Default on-disk location. Overridable via MERIDIAN_DB env var, which the
# test suite uses to redirect to ``:memory:``.
DEFAULT_DB_PATH = os.environ.get(
    "MERIDIAN_DB", str(Path("data") / "meridian.db")
)
DEFAULT_DATA_DIR = Path(
    os.environ.get("MERIDIAN_DATA_DIR", "data")
)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the SQLite connection on startup, close it on shutdown.

    Also: load environment variables from ``./.env`` if present so the
    dashboard chat proxy can find ``ANTHROPIC_API_KEY``. We do this here
    rather than at import time so test fixtures can override the env
    without an .env file leaking into them.
    """
    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
    except ImportError:
        pass  # dotenv is optional — env can be set by the launcher.

    db_path = os.environ.get("MERIDIAN_DB", DEFAULT_DB_PATH)
    data_dir = Path(os.environ.get("MERIDIAN_DATA_DIR", str(DEFAULT_DATA_DIR)))

    # In-memory DB skips filesystem setup.
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    db = await db_module.init_db(db_path)
    app.state.db = db
    app.state.data_dir = str(data_dir)
    app.state.ws_broadcaster = dashboard_module.WebSocketBroadcaster()

    # v0.4.2 — periodic auto-summary task. Interval comes from env so
    # tests can run it on a sub-second cadence; default is ten minutes.
    interval_s = float(os.environ.get("MERIDIAN_AUTO_SUMMARY_INTERVAL", 600))

    async def _auto_summary_loop() -> None:
        while True:
            try:
                await asyncio.sleep(interval_s)
                await db_module.run_auto_summary_cycle(db)
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001 — never let the loop die
                continue

    summary_task = asyncio.create_task(_auto_summary_loop())
    app.state.auto_summary_task = summary_task

    # v0.6.3 — GOAL.md startup sync: if the file exists and names a known
    # project, pull its contents into the DB before serving any requests.
    if db_path != ":memory:":
        try:
            await goal_md_module.sync_goal_md_to_db(db)
        except Exception:  # noqa: BLE001
            pass

    # v0.6.3 — optional live file-watch (no-op when watchfiles not installed).
    watch_task = asyncio.create_task(goal_md_module.watch_goal_md(db))
    app.state.watch_task = watch_task

    try:
        yield
    finally:
        summary_task.cancel()
        watch_task.cancel()
        try:
            await summary_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        try:
            await watch_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await db.close()


app = FastAPI(
    title="Meridian",
    description="Multi-session Claude coordinator MCP server.",
    version="0.1.0",
    lifespan=lifespan,
)


def _db(request: Request) -> aiosqlite.Connection:
    """Pull the active DB connection off ``app.state``."""
    return request.app.state.db


def _data_dir(request: Request) -> str:
    """Pull the active data directory off ``app.state``."""
    return request.app.state.data_dir


# ---------------------------------------------------------------------------
# FastAPI endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "service": "meridian"}


@app.get("/config")
async def server_config() -> dict[str, Any]:
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
    return {
        "server_url": server_url,
        "host": host,
        "port": port,
        "version": "1.1.0",
        "db": "memory" if os.environ.get("MERIDIAN_DB") == ":memory:" else "sqlite",
    }


@app.get("/projects", response_model=list[Project])
async def list_projects(request: Request) -> list[dict[str, Any]]:
    """List every project."""
    return await db_module.list_projects(_db(request))


@app.post("/projects", response_model=Project, status_code=201)
async def create_project(
    body: ProjectCreate, request: Request
) -> dict[str, Any]:
    """Create a new project. 409 if the name is already in use."""
    existing = await db_module.get_project_by_name(_db(request), body.name)
    if existing is not None:
        raise HTTPException(
            status_code=409, detail=f"project '{body.name}' already exists"
        )
    return await db_module.create_project(
        _db(request), body.name, human_id=body.human_id
    )


@app.get("/setup/needed")
async def setup_needed(request: Request) -> dict[str, Any]:
    """Returns {needed: true} if no projects exist yet (first-run wizard trigger)."""
    projects = await db_module.list_projects(_db(request))
    return {"needed": len(projects) == 0}


@app.get("/projects/by-name/{name}")
async def get_project_by_name(name: str, request: Request) -> dict[str, Any]:
    """Look up a project by name (case-insensitive substring match).

    Returns the project row plus a brief goal summary so a cold session
    can confirm it found the right project without a second round-trip.
    """
    db = _db(request)
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
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@app.get("/projects/{project_id}/goal", response_model=GoalState)
async def get_goal(project_id: str, request: Request) -> dict[str, Any]:
    """Read the latest goal state plus ambient task context.

    The response payload (v0.4.2+) includes ``ambient_tasks`` — the
    five most recent task rows, newest first, as ``{status, description,
    created_at}`` dicts. Cold sessions can render the directive *and*
    last activity from a single MCP call.

    404 if the project does not exist or the goal hasn't been set yet.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    goal = await db_module.get_goal(_db(request), project_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not set")
    recent = await db_module.get_tasks(_db(request), project_id, limit=5)
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
        _db(request), project_id
    )
    coherence = db_module.compute_coherence_warning(field_ages)
    goal["field_ages"] = field_ages
    goal["coherence_warning"] = coherence
    # v1.1.4 — append-only decisions log.
    decisions = await db_module.get_decisions(_db(request), project_id)
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
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        await db_module.set_goal_mode(_db(request), project_id, body.mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"project_id": project_id, "goal_mode": body.mode}


@app.get("/projects/{project_id}/goal-mode")
async def get_goal_mode(project_id: str, request: Request) -> dict[str, str]:
    """Return the current goal mode for a project."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    mode = await db_module.get_goal_mode(_db(request), project_id)
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
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    owner = await db_module.get_project_owner(_db(request), project_id)
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
    result = await db_module.set_goal(
        _db(request), project_id, body.content,
        north_star=body.north_star, sprint=body.sprint,
    )
    await goal_md_module.sync_db_to_goal_md(_db(request), project_id)
    return result


@app.post("/projects/{project_id}/goal/north-star", response_model=GoalState)
async def set_north_star(
    project_id: str, body: SetNorthStarRequest, request: Request
) -> dict[str, Any]:
    """v0.5.2 — update only the north star field.

    Owner-only: requires ``human_id`` matching the project creator.
    Returns the new goal version.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    owner = await db_module.get_project_owner(_db(request), project_id)
    if owner is not None and body.human_id != owner:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "goal_locked",
                "message": "Only the project owner can set the north star.",
            },
        )
    try:
        result = await db_module.set_north_star(
            _db(request), project_id, body.north_star
        )
        await goal_md_module.sync_db_to_goal_md(_db(request), project_id)
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
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        result = await db_module.set_sprint(
            _db(request), project_id, body.sprint
        )
        await goal_md_module.sync_db_to_goal_md(_db(request), project_id)
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
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        return await db_module.start_worker_session(
            _db(request),
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
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")
    updated = await db_module.set_decision(_db(request), project_id, text)
    return {"project_id": project_id, "decisions": updated}


@app.get("/projects/{project_id}/timeline")
async def get_timeline_endpoint(
    project_id: str, request: Request
) -> dict[str, Any]:
    """v1.1.1 — return the data needed to render the Activity Timeline.

    The dashboard renders a swimlane per session with task pills laid
    out on a time axis and vertical dashed lines at goal-change events.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.get_timeline(_db(request), project_id)


# ---------------------------------------------------------------------------
# Sprint items (v1.1) — checklist alongside the free-text sprint field.
# ---------------------------------------------------------------------------


@app.get("/projects/{project_id}/sprint-items")
async def list_sprint_items(
    project_id: str, request: Request, status: str | None = None
) -> list[dict[str, Any]]:
    """List sprint items, optionally filtered by status."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        return await db_module.get_sprint_items(
            _db(request), project_id, status=status
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post(
    "/projects/{project_id}/sprint-items", status_code=201
)
async def add_sprint_item_endpoint(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Append a pending sprint item. Body: ``{version, title}``."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    version = (body.get("version") or "").strip()
    title = (body.get("title") or "").strip()
    if not version or not title:
        raise HTTPException(
            status_code=422, detail="version and title are required"
        )
    return await db_module.add_sprint_item(
        _db(request), project_id, version, title
    )


@app.post("/projects/{project_id}/sprint-items/{item_id}/complete")
async def complete_sprint_item_endpoint(
    project_id: str, item_id: str, request: Request,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark a sprint item ``done``. Optional body: ``{task_id}``."""
    item = await db_module.complete_sprint_item(
        _db(request),
        project_id,
        item_id,
        task_id=(body or {}).get("task_id"),
    )
    if item is None:
        raise HTTPException(status_code=404, detail="sprint item not found")
    return item


@app.post("/projects/{project_id}/sprint-items/{item_id}/skip")
async def skip_sprint_item_endpoint(
    project_id: str, item_id: str, request: Request,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark a sprint item ``skipped``. Optional body: ``{reason}``."""
    item = await db_module.skip_sprint_item(
        _db(request),
        project_id,
        item_id,
        reason=(body or {}).get("reason"),
    )
    if item is None:
        raise HTTPException(status_code=404, detail="sprint item not found")
    return item


@app.get("/projects/{project_id}/sessions", response_model=list[Session])
async def get_sessions(
    project_id: str, request: Request
) -> list[dict[str, Any]]:
    """List active sessions attached to the project.

    Expires stale sessions (last_seen > 30 min ago) before returning so
    the dashboard doesn't accumulate ghost entries indefinitely.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    await _expire_and_generate_handoffs(_db(request), _data_dir(request))
    return await db_module.get_sessions(
        _db(request), project_id, active_only=True
    )


@app.get("/projects/{project_id}/tasks", response_model=list[Task])
async def get_tasks(
    project_id: str, request: Request, limit: int = 20
) -> list[dict[str, Any]]:
    """List recent tasks for a project, newest first."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.get_tasks(_db(request), project_id, limit=limit)


@app.get("/projects/{project_id}/tasks/claimable", response_model=list[Task])
async def get_claimable_tasks(
    project_id: str, request: Request, limit: int = 20
) -> list[dict[str, Any]]:
    """List unclaimed pending tasks for a project (v0.3.3).

    Workers poll this endpoint to find work that isn't already locked
    by another session.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.get_claimable_tasks(
        _db(request), project_id, limit=limit
    )


@app.post(
    "/projects/{project_id}/tasks/claim", response_model=ClaimTaskResponse
)
async def claim_task_endpoint(
    project_id: str, body: ClaimTaskRequest, request: Request
) -> dict[str, Any]:
    """Atomically claim a pending task. Returns ``claimed=False`` when
    another worker holds the lock — the worker should try the next row."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    claimed = await db_module.claim_task(
        _db(request), body.task_id, body.session_id
    )
    if claimed is None:
        existing = await db_module.get_task(_db(request), body.task_id)
        return {
            "task_id": body.task_id,
            "claimed": False,
            "claimed_by": existing["claimed_by"] if existing else None,
        }
    return {
        "task_id": body.task_id,
        "claimed": True,
        "claimed_by": claimed["claimed_by"],
    }


@app.post("/projects/{project_id}/tasks/release")
async def release_task_endpoint(
    project_id: str, body: ClaimTaskRequest, request: Request
) -> dict[str, Any]:
    """Release a previously-claimed task. 404 when no claim is held
    by the given session."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    released = await db_module.release_task(
        _db(request), body.task_id, body.session_id
    )
    if not released:
        raise HTTPException(
            status_code=404,
            detail="task not claimed by this session",
        )
    return {"task_id": body.task_id, "released": True}


@app.post("/projects/{project_id}/handoff", response_model=HandoffResult)
async def generate_handoff(
    project_id: str, request: Request
) -> dict[str, Any]:
    """Render and write the handoff file for a project."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    path, content = await handoff_module.generate_handoff(
        _db(request), project_id, _data_dir(request)
    )
    return {"path": path, "content": content}


@app.post("/sessions/register", response_model=Session, status_code=201)
async def register_session(
    body: SessionRegister, request: Request
) -> dict[str, Any]:
    """Create a session row tied to a project."""
    project = await db_module.get_project(_db(request), body.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.register_session(
        _db(request), body.project_id, body.name, human_id=body.human_id
    )


@app.post("/sessions/{session_id}/close")
async def close_session(session_id: str, request: Request) -> dict[str, str]:
    """Mark a session closed."""
    async with _db(request).execute(
        "SELECT id FROM sessions WHERE id = ?", (session_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    await db_module.close_session(_db(request), session_id)
    return {"status": "closed", "session_id": session_id}


@app.post("/sessions/{session_id}/heartbeat")
async def heartbeat_session(
    session_id: str, request: Request
) -> dict[str, str]:
    """v0.5.1 — touch ``last_seen`` to keep this session out of the
    idle sweep. Long-running workers call this every few minutes so
    the 30 minute TTL doesn't expire them while they're still alive.
    404 when the session id is unknown or already closed."""
    ok = await db_module.heartbeat_session(_db(request), session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="session not found")
    return {"status": "ok", "session_id": session_id}


@app.post("/tasks", response_model=Task, status_code=201)
async def create_task(body: TaskCreate, request: Request) -> dict[str, Any]:
    """Append a task-log entry."""
    project = await db_module.get_project(_db(request), body.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    async with _db(request).execute(
        "SELECT id FROM sessions WHERE id = ?", (body.session_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return await db_module.log_task(
        _db(request),
        body.session_id,
        body.project_id,
        body.description,
        body.status,
    )


@app.patch("/tasks/{task_id}", response_model=Task)
async def patch_task(
    task_id: str, body: TaskUpdate, request: Request
) -> dict[str, Any]:
    """Update a task's status and/or description in place.

    Used by the dashboard to flip HITL tasks to done/failed when the
    human replies. 404 when the id is unknown, 422 on invalid status.
    """
    existing = await db_module.get_task(_db(request), task_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="task not found")
    try:
        updated = await db_module.update_task(
            _db(request),
            task_id,
            status=body.status,
            description=body.description,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    assert updated is not None
    return updated


@app.delete("/projects/{project_id}/chat/history", status_code=204)
async def clear_chat_history(project_id: str, request: Request) -> None:
    """Delete all chat messages and session for a project."""
    database = _db(request)
    await database.execute("DELETE FROM chat_messages WHERE project_id = ?", (project_id,))
    await database.execute("DELETE FROM chat_sessions WHERE project_id = ?", (project_id,))
    await database.commit()


@app.get(
    "/projects/{project_id}/chat/history",
    response_model=list[ChatHistoryItem],
)
async def get_chat_history(
    project_id: str, request: Request, limit: int = 50
) -> list[dict[str, Any]]:
    """Return persisted chat messages for a project (oldest first).

    The dashboard calls this on tab open to restore conversation history
    across page refreshes. Returns an empty list when no messages exist yet.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.get_chat_history(_db(request), project_id, limit=limit)


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

    db = _db(request)
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


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_html() -> str:
    """Serve the single-file dashboard UI."""
    return dashboard_module.DASHBOARD_HTML


@app.get("/config/api-key")
async def api_key_status() -> dict:
    """Tell the dashboard which auth method is active.

    Returns ``{"configured": bool, "method": "oauth"|"api_key"|null}``.
    The token itself is never included in the response.
    """
    _, method = dashboard_module.get_auth_token()
    return {"configured": method is not None, "method": method}


@app.post("/dashboard/chat")
async def dashboard_chat(body: ChatRequest, request: Request):
    """Proxy a streaming Claude chat call as Server-Sent Events.

    The default backend (``mode="cli"``) spawns the ``claude`` CLI
    binary so the conversation draws from the user's Max-plan
    allowance via the OAuth token already on disk. Set ``mode="api"``
    in the request body to use the metered Anthropic API directly.

    Each text chunk is forwarded as a ``data: {"delta": "..."}`` line;
    the stream terminates with ``data: [DONE]``.

    Persistence: the user message is saved to ``chat_messages`` before
    streaming begins; the complete assistant reply is appended after the
    stream finishes. A ``chat_sessions`` row is created on first use so
    future CLI ``--resume`` support has a home for the session handle.
    """
    project = await db_module.get_project(_db(request), body.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    messages = [m.model_dump() for m in body.messages]
    # Inject model identity into system prompt so the model knows its version
    model_hint = f"You are {body.model} running in the Meridian dashboard."
    if body.system_prompt:
        body = body.model_copy(update={"system_prompt": model_hint + "\n\n" + body.system_prompt})
    else:
        body = body.model_copy(update={"system_prompt": model_hint})
    db = _db(request)
    project_id = body.project_id

    # Persist the user's message and ensure a chat session exists.
    if messages and messages[-1].get("role") == "user":
        await db_module.save_chat_message(db, project_id, "user", messages[-1]["content"])
    chat_session = await db_module.get_or_create_chat_session(db, project_id)

    # Select the streaming backend.
    if body.mode == "cli":
        # v0.4.1 — if the CLI session id is known from a previous turn
        # pass it as --resume so the conversation continues; capture
        # whatever id the CLI emits on this turn for the next one.
        resume_id = chat_session.get("cli_session_id") if chat_session else None

        async def _save_session_id(new_id: str) -> None:
            await db_module.update_chat_session_cli_id(db, project_id, new_id)

        raw_stream = dashboard_module.stream_claude_cli_chat(
            messages=messages,
            system_prompt=body.system_prompt,
            model=body.model,
            max_tokens=body.max_tokens,
            resume_session_id=resume_id,
            on_session_id=_save_session_id,
        )
    else:
        raw_stream = dashboard_module.stream_anthropic_chat(
            messages=messages,
            system_prompt=body.system_prompt,
            model=body.model,
            max_tokens=body.max_tokens,
        )

    async def _saving_stream():
        """Yield every SSE chunk and save the full assistant reply at the end."""
        acc: list[str] = []
        async for chunk in raw_stream:
            yield chunk
            # Each chunk is one complete SSE event: b"data: {...}\n\n" or b"data: [DONE]\n\n"
            if chunk != b"data: [DONE]\n\n" and chunk.startswith(b"data: "):
                try:
                    payload = json.loads(chunk[6:].decode("utf-8").strip())
                    if "delta" in payload:
                        acc.append(payload["delta"])
                except Exception:  # noqa: BLE001
                    pass
        full_text = "".join(acc).strip()
        if full_text:
            try:
                await db_module.save_chat_message(db, project_id, "assistant", full_text)
            except Exception:  # noqa: BLE001
                pass

    return StreamingResponse(_saving_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# File editing endpoints
# ---------------------------------------------------------------------------

# Repo root is the parent of this package directory (meridian/).
_REPO_ROOT = Path(__file__).parent.parent
# The dashboard only allows editing these specific files.
_EDITABLE_FILES: list[str] = ["AGENTS.md", "ROADMAP.md", "DEVLOG.md", "CLAUDE.md"]


@app.get("/projects/{project_id}/files")
async def list_project_files(
    project_id: str, request: Request
) -> list[str]:
    """Return the list of editable markdown files for a project.

    Files that do not yet exist on disk are still listed so the user can
    create them from the dashboard. 403 is raised if the project is unknown.
    """
    project = await db_module.get_project(_db(request), project_id)
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
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if filename not in _EDITABLE_FILES:
        raise HTTPException(status_code=403, detail="file not in allow-list")
    path = _REPO_ROOT / filename
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    return {"filename": filename, "content": content}


@app.put("/projects/{project_id}/files/{filename}")
async def put_project_file(
    project_id: str, filename: str, body: FileContent, request: Request
) -> dict[str, object]:
    """Write content to one editable markdown file.

    Creates the file if it does not exist. 403 if the filename is not in the
    allow-list. Returns ``{"filename": ..., "size": <bytes>}``.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if filename not in _EDITABLE_FILES:
        raise HTTPException(status_code=403, detail="file not in allow-list")
    path = _REPO_ROOT / filename
    path.write_text(body.content, encoding="utf-8")
    return {"filename": filename, "size": len(body.content.encode("utf-8"))}


@app.websocket("/ws/{project_id}")
async def ws_project(ws: WebSocket, project_id: str) -> None:
    """Push task-log events to dashboard clients for one project."""
    broadcaster: dashboard_module.WebSocketBroadcaster = (
        ws.app.state.ws_broadcaster
    )
    await broadcaster.serve(ws, project_id)


@app.post("/tasks/enqueue", response_model=Task, status_code=202)
async def enqueue_task(body: EnqueueTask, request: Request) -> dict[str, Any]:
    """Paid-tier: queue a Claude subprocess and return the pending task row.

    Responds with 202 Accepted so clients can distinguish this from a
    synchronous task creation. The worker runs in the background; poll
    ``GET /projects/{id}/tasks`` to see the result land.
    """
    project = await db_module.get_project(_db(request), body.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    async with _db(request).execute(
        "SELECT id FROM sessions WHERE id = ?", (body.session_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return await enqueue_module.enqueue_claude_task(
        _db(request),
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
    for pid in result["project_ids"]:
        try:
            await handoff_module.generate_handoff(db, pid, data_dir)
            generated = True
        except Exception:  # noqa: BLE001
            pass
    return {"count": result["count"], "auto_handoff_generated": generated}


# ---------------------------------------------------------------------------
# v0.4.4 — start_session composite helper + endpoint
# ---------------------------------------------------------------------------


async def _start_session_composite(
    db: aiosqlite.Connection,
    project_id: str,
    session_name: str,
    data_dir: str,
    human_id: str | None = None,
) -> dict[str, Any]:
    """Register + goal + tasks + sessions + handoff-check in one shot.

    Replaces the four-call cold-start sequence (register_session, get_goal,
    get_tasks, check handoff file) with a single call that returns everything
    a new session needs before touching anything.
    """
    session = await db_module.register_session(
        db, project_id, session_name, human_id=human_id
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

    # v1.1 — surface the still-pending sprint checklist so cold sessions
    # see what's in flight before doing anything.
    pending_items = await db_module.get_sprint_items(
        db, project_id, status="pending"
    )
    sprint_items_xml = db_module.build_sprint_items_xml(pending_items)

    return {
        "session_id": session["id"],
        "goal": goal,
        "goal_xml": goal_xml,  # v0.6.1 — always present
        "goal_cache_blocks": goal_cache_blocks,  # v0.6.2 — ready for Anthropic
        "sprint_items": pending_items,  # v1.1 — pending checklist
        "sprint_items_xml": sprint_items_xml,
        "recent_tasks": recent_tasks,
        "active_sessions": active_sessions,
        "handoff_exists": handoff_exists,
        "handoff_path": handoff_path_str,
        "files": list(_EDITABLE_FILES),
    }


@app.post("/projects/{project_id}/start-session")
async def start_session_endpoint(
    project_id: str, body: StartSessionRequest, request: Request
) -> dict[str, Any]:
    """v0.4.4 — one call to start a coordinated session.

    Registers the caller, fetches goal + ambient tasks, fetches the last 10
    tasks, lists active sessions, and reports whether a handoff file already
    exists on disk. Replaces 4 separate MCP calls at session cold-start.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await _start_session_composite(
        _db(request),
        project_id,
        body.session_name,
        _data_dir(request),
        human_id=body.human_id,
    )


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------


def build_mcp_server():
    """Construct the MCP server with all eight Meridian tools.

    The server opens its own dedicated SQLite connection because MCP runs in
    a separate event-loop context from FastAPI. Tools return JSON-serialisable
    dicts; descriptions are written verbosely so Claude knows when to use
    them without further prompting.
    """
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
    import json

    server: Server = Server("meridian")

    # Lazy holder for the DB connection — opened on first use because the
    # stdio entrypoint is sync up to the point we hit asyncio.run().
    state: dict[str, Any] = {"db": None, "data_dir": None}

    async def _ensure_db() -> aiosqlite.Connection:
        if state["db"] is None:
            db_path = os.environ.get("MERIDIAN_DB", DEFAULT_DB_PATH)
            data_dir = Path(
                os.environ.get("MERIDIAN_DATA_DIR", str(DEFAULT_DATA_DIR))
            )
            if db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            data_dir.mkdir(parents=True, exist_ok=True)
            state["db"] = await db_module.init_db(db_path)
            state["data_dir"] = str(data_dir)
        return state["db"]

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """Advertise every Meridian tool to the MCP client."""
        return [
            Tool(
                name="create_project",
                description=(
                    "Create a new Meridian project to coordinate sessions "
                    "around. Returns the project id and name. Project names "
                    "must be unique."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            ),
            Tool(
                name="register_session",
                description=(
                    "Register this Claude session with a project. Call at "
                    "the START of every session before using any other "
                    "tools. Store the returned session_id — you need it "
                    "for log_task. Optionally pass human_id to attach "
                    "the session to a teammate (e.g. \"adam\") so the "
                    "dashboard groups sessions per human and the goal "
                    "ownership rule can recognise the writer."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "session_name": {"type": "string"},
                        "human_id": {
                            "type": "string",
                            "description": "Optional human owner identifier.",
                        },
                    },
                    "required": ["project_id", "session_name"],
                },
            ),
            Tool(
                name="get_goal",
                description=(
                    "Read the current goal state plus ambient context "
                    "for a project. Returns all three goal levels "
                    "(north_star, content/version goal, sprint) plus the "
                    "last 5 task descriptions so a cold session knows the "
                    "directive AND recent activity from one call. Read "
                    "this after registering."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"project_id": {"type": "string"}},
                    "required": ["project_id"],
                },
            ),
            Tool(
                name="set_goal",
                description=(
                    "Set or update the version goal (content). All "
                    "sessions see this immediately. Version increments on "
                    "each update. Content may be a JSON object or a plain "
                    "string. Optionally supply north_star or sprint to "
                    "update those fields at the same time; omit to "
                    "preserve existing values."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "content": {
                            "oneOf": [
                                {"type": "object"},
                                {"type": "string"},
                            ]
                        },
                        "north_star": {"type": "string"},
                        "sprint": {"type": "string"},
                    },
                    "required": ["project_id", "content"],
                },
            ),
            Tool(
                name="set_north_star",
                description=(
                    "Update only the north star — the long-lived product "
                    "vision that rarely changes. Owner-only: pass the "
                    "same human_id used when creating the project. "
                    "Returns 403 if the human_id doesn't match."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "north_star": {"type": "string"},
                        "human_id": {"type": "string"},
                    },
                    "required": ["project_id", "north_star", "human_id"],
                },
            ),
            Tool(
                name="set_sprint",
                description=(
                    "Update only the sprint — the short-term focus that "
                    "changes each session or week. Any team member can "
                    "call this; no ownership check."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "sprint": {"type": "string"},
                    },
                    "required": ["project_id", "sprint"],
                },
            ),
            Tool(
                name="log_task",
                description=(
                    "Log what this session just did, is doing, or failed "
                    "at. Call frequently to keep all sessions informed of "
                    "progress. Status is one of 'pending', 'done', "
                    "'failed' (default 'done')."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "project_id": {"type": "string"},
                        "description": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "done", "failed"],
                            "default": "done",
                        },
                    },
                    "required": [
                        "session_id",
                        "project_id",
                        "description",
                    ],
                },
            ),
            Tool(
                name="get_tasks",
                description=(
                    "Get recent tasks across all sessions. Shows what "
                    "everyone has done. Newest first."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "limit": {"type": "integer", "default": 20},
                    },
                    "required": ["project_id"],
                },
            ),
            Tool(
                name="get_sessions",
                description=(
                    "List all active sessions connected to this project."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"project_id": {"type": "string"}},
                    "required": ["project_id"],
                },
            ),
            Tool(
                name="generate_handoff",
                description=(
                    "Generate a context handoff file. Call when context is "
                    "filling up or before ending a session. A new session "
                    "can read this file to resume with full context. "
                    "Returns file path and rendered content."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"project_id": {"type": "string"}},
                    "required": ["project_id"],
                },
            ),
            Tool(
                name="enqueue_claude_task",
                description=(
                    "PAID-TIER. Queue a long-running Claude Code subprocess "
                    "without blocking this session. Returns immediately with "
                    "a pending task row; the worker writes its result back "
                    "into the same row when it finishes. Poll get_tasks to "
                    "see the result. Use this when an MCP tool call would "
                    "otherwise time out waiting for a Claude subprocess to "
                    "complete."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "project_id": {"type": "string"},
                        "prompt": {"type": "string"},
                        "timeout": {
                            "type": "number",
                            "default": 600.0,
                            "description": (
                                "Seconds before the worker is killed. Pass "
                                "0 or a negative number to disable."
                            ),
                        },
                    },
                    "required": ["session_id", "project_id", "prompt"],
                },
            ),
            Tool(
                name="claim_task",
                description=(
                    "Atomically claim a pending task so no other worker "
                    "picks it up. Returns claimed=True on success or "
                    "claimed=False (with the current holder) when another "
                    "session already holds the lock. Call this before "
                    "doing the work; pair with release_task on completion "
                    "or failure."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "task_id": {"type": "string"},
                        "session_id": {"type": "string"},
                    },
                    "required": ["project_id", "task_id", "session_id"],
                },
            ),
            Tool(
                name="heartbeat",
                description=(
                    "Touch this session's last_seen so the idle sweep "
                    "doesn't expire it. Long-running workers should call "
                    "this every ~5 minutes between log_task calls. "
                    "Returns ok=True when the session exists and is "
                    "still open."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "session_id": {"type": "string"},
                    },
                    "required": ["project_id", "session_id"],
                },
            ),
            Tool(
                name="release_task",
                description=(
                    "Release a task previously claimed by this session. "
                    "Returns success=True when the claim was held by the "
                    "calling session, False otherwise (someone else's lock "
                    "is left untouched)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "task_id": {"type": "string"},
                        "session_id": {"type": "string"},
                    },
                    "required": ["project_id", "task_id", "session_id"],
                },
            ),
            Tool(
                name="start_worker_session",
                description=(
                    "v1.2.0 — register a worker session and claim its "
                    "task in one call. Returns a slim worker_context "
                    "XML block (version_goal + claimed task + repo + "
                    "test_cmd + commit_pattern + done_when) under ~500 "
                    "tokens. Use this for Claude Code subprocess workers "
                    "that should NOT see north_star, decisions, sprint "
                    "history, or ambient task log. If task_id is "
                    "omitted, the oldest unclaimed pending task is "
                    "picked automatically."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "task_id": {"type": "string"},
                    },
                    "required": ["project_id"],
                },
            ),
            Tool(
                name="set_decision",
                description=(
                    "Append a decision entry to the project's "
                    "append-only decisions log (v1.1.4). Each entry "
                    "is prepended with a UTC date stamp so newest "
                    "decisions appear first. Use this to record "
                    "architectural calls, scope reductions, key "
                    "trade-offs — anything a future session must "
                    "know before doing the work."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["project_id", "text"],
                },
            ),
            Tool(
                name="add_sprint_item",
                description=(
                    "Append a pending item to the project's machine-trackable "
                    "sprint checklist (v1.1). Use this when you start work on "
                    "a new version so the next session sees what's in flight. "
                    "Returns the new item."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "version": {"type": "string"},
                        "title": {"type": "string"},
                    },
                    "required": ["project_id", "version", "title"],
                },
            ),
            Tool(
                name="complete_sprint_item",
                description=(
                    "Mark a sprint item done. Pass task_id to link the "
                    "task that shipped it; the timeline correlates them. "
                    "Returns the updated item or null if the id is unknown."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "item_id": {"type": "string"},
                        "task_id": {"type": "string"},
                    },
                    "required": ["project_id", "item_id"],
                },
            ),
            Tool(
                name="skip_sprint_item",
                description=(
                    "Mark a sprint item skipped (intentionally not shipped). "
                    "Provide a one-line ``reason`` so a future session can "
                    "understand the call."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "item_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["project_id", "item_id"],
                },
            ),
            Tool(
                name="get_sprint_items",
                description=(
                    "List sprint items for a project. Optional status "
                    "filter (pending|in_progress|done|skipped). Cold "
                    "sessions read this to know what's still owed."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": [
                                "pending", "in_progress", "done", "skipped",
                            ],
                        },
                    },
                    "required": ["project_id"],
                },
            ),
            Tool(
                name="start_session",
                description=(
                    "Single call to start a coordinated session. Registers "
                    "you, reads goal + ambient context, shows recent work, "
                    "lists active sessions, and tells you where the handoff "
                    "file is. Call this INSTEAD of register_session + "
                    "get_goal + get_tasks separately. Returns: session_id, "
                    "goal (with ambient_tasks), recent_tasks (last 10), "
                    "active_sessions, handoff_exists, handoff_path, files."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "session_name": {"type": "string"},
                        "human_id": {
                            "type": "string",
                            "description": "Optional human owner identifier.",
                        },
                    },
                    "required": ["project_id", "session_name"],
                },
            ),
            Tool(
                name="list_projects",
                description=(
                    "List all Meridian projects with their names and ids. "
                    "No parameters required. Use this when you don't know "
                    "the project_id — find the project by name, then pass "
                    "its id to register_session or start_session. Returns "
                    "[{id, name, created_at}] newest first."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="get_project_by_name",
                description=(
                    "Look up a project by name (case-insensitive, substring "
                    "match). Returns the project id plus a brief goal "
                    "summary. Use this for cold starts when you know the "
                    "project name but not the UUID: call this first, then "
                    "pass the returned id to start_session."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "Full or partial project name — "
                                "case-insensitive substring match."
                            ),
                        }
                    },
                    "required": ["name"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict[str, Any]
    ) -> list[TextContent]:
        """Dispatch an MCP tool call to the matching db/handoff function."""
        db = await _ensure_db()
        result: Any
        try:
            if name == "create_project":
                existing = await db_module.get_project_by_name(
                    db, arguments["name"]
                )
                if existing is not None:
                    result = {
                        "error": f"project '{arguments['name']}' already exists",
                        "project": existing,
                    }
                else:
                    result = await db_module.create_project(
                        db, arguments["name"]
                    )
            elif name == "register_session":
                result = await db_module.register_session(
                    db,
                    arguments["project_id"],
                    arguments["session_name"],
                    human_id=arguments.get("human_id"),
                )
            elif name == "get_goal":
                goal = await db_module.get_goal(db, arguments["project_id"])
                if goal is None:
                    # Even an unset goal returns a valid XML skeleton so
                    # cold sessions don't have to special-case 404.
                    project = await db_module.get_project(
                        db, arguments["project_id"]
                    )
                    project_name = project["name"] if project else ""
                    result = {
                        "error": "goal not set",
                        "xml": db_module.build_goal_xml(
                            None, project_name, []
                        ),
                        "cache_blocks": db_module.build_goal_cache_blocks(
                            None, project_name, []
                        ),
                    }
                else:
                    # v0.4.2/3 — surface the last five task descriptions
                    # alongside the goal so cold sessions get ambient
                    # context inline with the directive.
                    recent = await db_module.get_tasks(
                        db, arguments["project_id"], limit=5
                    )
                    goal["ambient_tasks"] = [
                        {
                            "status": t["status"],
                            "description": t["description"],
                            "created_at": t["created_at"],
                        }
                        for t in recent
                    ]
                    project = await db_module.get_project(
                        db, arguments["project_id"]
                    )
                    project_name = project["name"] if project else ""
                    field_ages = await db_module.get_goal_field_ages(
                        db, arguments["project_id"]
                    )
                    coherence = db_module.compute_coherence_warning(field_ages)
                    goal["field_ages"] = field_ages
                    goal["coherence_warning"] = coherence
                    decisions = await db_module.get_decisions(
                        db, arguments["project_id"]
                    )
                    goal["decisions"] = decisions
                    goal["xml"] = db_module.build_goal_xml(
                        goal, project_name, goal["ambient_tasks"], coherence,
                        decisions=decisions,
                    )
                    goal["cache_blocks"] = db_module.build_goal_cache_blocks(
                        goal, project_name, goal["ambient_tasks"]
                    )
                    result = goal
            elif name == "set_goal":
                result = await db_module.set_goal(
                    db,
                    arguments["project_id"],
                    arguments["content"],
                    north_star=arguments.get("north_star"),
                    sprint=arguments.get("sprint"),
                )
            elif name == "set_north_star":
                owner = await db_module.get_project_owner(
                    db, arguments["project_id"]
                )
                if owner is not None and arguments["human_id"] != owner:
                    result = {
                        "error": "goal_locked",
                        "message": "Only the project owner can set the north star.",
                    }
                else:
                    try:
                        result = await db_module.set_north_star(
                            db, arguments["project_id"], arguments["north_star"]
                        )
                        await goal_md_module.sync_db_to_goal_md(
                            db, arguments["project_id"]
                        )
                    except ValueError as exc:
                        result = {"error": str(exc)}
            elif name == "set_sprint":
                try:
                    result = await db_module.set_sprint(
                        db, arguments["project_id"], arguments["sprint"]
                    )
                    await goal_md_module.sync_db_to_goal_md(
                        db, arguments["project_id"]
                    )
                except ValueError as exc:
                    result = {"error": str(exc)}
            elif name == "log_task":
                result = await db_module.log_task(
                    db,
                    arguments["session_id"],
                    arguments["project_id"],
                    arguments["description"],
                    arguments.get("status", "done"),
                )
            elif name == "get_tasks":
                result = await db_module.get_tasks(
                    db,
                    arguments["project_id"],
                    limit=int(arguments.get("limit", 20)),
                )
            elif name == "get_sessions":
                result = await db_module.get_sessions(
                    db, arguments["project_id"], active_only=True
                )
            elif name == "generate_handoff":
                path, content = await handoff_module.generate_handoff(
                    db, arguments["project_id"], state["data_dir"]
                )
                result = {"path": path, "content": content}
            elif name == "enqueue_claude_task":
                raw_timeout = arguments.get("timeout", 600.0)
                # Treat 0 / negative as "no timeout" — Claude jobs can be
                # genuinely open-ended.
                timeout: float | None
                try:
                    timeout = float(raw_timeout)
                    if timeout <= 0:
                        timeout = None
                except (TypeError, ValueError):
                    timeout = 600.0
                result = await enqueue_module.enqueue_claude_task(
                    db,
                    arguments["session_id"],
                    arguments["project_id"],
                    arguments["prompt"],
                    timeout=timeout,
                )
            elif name == "claim_task":
                claimed = await db_module.claim_task(
                    db,
                    arguments["task_id"],
                    arguments["session_id"],
                )
                if claimed is None:
                    existing = await db_module.get_task(
                        db, arguments["task_id"]
                    )
                    result = {
                        "task_id": arguments["task_id"],
                        "claimed": False,
                        "claimed_by": (
                            existing["claimed_by"] if existing else None
                        ),
                    }
                else:
                    result = {
                        "task_id": arguments["task_id"],
                        "claimed": True,
                        "claimed_by": claimed["claimed_by"],
                    }
            elif name == "release_task":
                released = await db_module.release_task(
                    db,
                    arguments["task_id"],
                    arguments["session_id"],
                )
                result = {
                    "task_id": arguments["task_id"],
                    "success": released,
                }
            elif name == "heartbeat":
                ok = await db_module.heartbeat_session(
                    db, arguments["session_id"]
                )
                result = {"session_id": arguments["session_id"], "ok": ok}
            elif name == "start_session":
                result = await _start_session_composite(
                    db,
                    arguments["project_id"],
                    arguments["session_name"],
                    state["data_dir"],
                    human_id=arguments.get("human_id"),
                )
            elif name == "list_projects":
                result = await db_module.list_projects(db)
            elif name == "get_project_by_name":
                name_arg = arguments["name"]
                project = await db_module.get_project_by_name(db, name_arg)
                if project is None:
                    all_projects = await db_module.list_projects(db)
                    lower = name_arg.lower()
                    matches = [
                        p for p in all_projects
                        if lower in p["name"].lower()
                    ]
                    project = matches[0] if matches else None
                if project is None:
                    result = {
                        "error": f"no project found matching '{name_arg}'"
                    }
                else:
                    goal = await db_module.get_goal(db, project["id"])
                    result = {
                        "project": project,
                        "goal_version": goal["version"] if goal else None,
                        "goal_summary": (
                            str(goal["content"])[:200] if goal else None
                        ),
                    }
            elif name == "start_worker_session":
                try:
                    result = await db_module.start_worker_session(
                        db,
                        arguments["project_id"],
                        task_id=arguments.get("task_id"),
                    )
                except ValueError as exc:
                    result = {"error": str(exc)}
            elif name == "set_decision":
                try:
                    updated = await db_module.set_decision(
                        db,
                        arguments["project_id"],
                        arguments["text"],
                    )
                    result = {
                        "project_id": arguments["project_id"],
                        "decisions": updated,
                    }
                except ValueError as exc:
                    result = {"error": str(exc)}
            elif name == "add_sprint_item":
                result = await db_module.add_sprint_item(
                    db,
                    arguments["project_id"],
                    arguments["version"],
                    arguments["title"],
                )
            elif name == "complete_sprint_item":
                item = await db_module.complete_sprint_item(
                    db,
                    arguments["project_id"],
                    arguments["item_id"],
                    task_id=arguments.get("task_id"),
                )
                result = item or {"error": "sprint item not found"}
            elif name == "skip_sprint_item":
                item = await db_module.skip_sprint_item(
                    db,
                    arguments["project_id"],
                    arguments["item_id"],
                    reason=arguments.get("reason"),
                )
                result = item or {"error": "sprint item not found"}
            elif name == "get_sprint_items":
                result = await db_module.get_sprint_items(
                    db,
                    arguments["project_id"],
                    status=arguments.get("status"),
                )
            else:
                result = {"error": f"unknown tool: {name}"}
        except Exception as exc:  # noqa: BLE001 — surface to MCP client
            result = {"error": f"{type(exc).__name__}: {exc}"}

        return [TextContent(type="text", text=json.dumps(result, default=str))]

    async def run_stdio() -> None:
        """Run the MCP server over stdio until the client disconnects."""
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    return server, run_stdio
