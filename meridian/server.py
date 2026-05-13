"""FastAPI HTTP server and MCP stdio server for Meridian.

This module exposes two surfaces backed by the same async SQLite database:

* A FastAPI app (``app``) reachable on port 7878 by default. Used by the
  demo script and any HTTP client.
* An MCP server (built in :func:`build_mcp_server`) reachable via stdio.
  Wired up in :mod:`meridian.__main__` and consumed by Claude Desktop /
  Claude Code / Cursor / Windsurf.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, StreamingResponse

from . import dashboard as dashboard_module
from . import db as db_module
from . import enqueue as enqueue_module
from . import handoff as handoff_module
from .models import (
    ChatRequest,
    EnqueueTask,
    GoalSet,
    GoalState,
    HandoffResult,
    Project,
    ProjectCreate,
    Session,
    SessionRegister,
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
    try:
        yield
    finally:
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
    return await db_module.create_project(_db(request), body.name)


@app.get("/projects/{project_id}", response_model=Project)
async def get_project(project_id: str, request: Request) -> dict[str, Any]:
    """Look up a project by id."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@app.get("/projects/{project_id}/goal", response_model=GoalState)
async def get_goal(project_id: str, request: Request) -> dict[str, Any]:
    """Read the latest goal state. 404 if the project or goal is missing."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    goal = await db_module.get_goal(_db(request), project_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not set")
    return goal


@app.post("/projects/{project_id}/goal", response_model=GoalState)
async def set_goal(
    project_id: str, body: GoalSet, request: Request
) -> dict[str, Any]:
    """Upsert the goal state, incrementing version."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.set_goal(_db(request), project_id, body.content)


@app.get("/projects/{project_id}/sessions", response_model=list[Session])
async def get_sessions(
    project_id: str, request: Request
) -> list[dict[str, Any]]:
    """List active sessions attached to the project."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
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
        _db(request), body.project_id, body.name
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


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_html() -> str:
    """Serve the single-file dashboard UI."""
    return dashboard_module.DASHBOARD_HTML


@app.get("/config/api-key")
async def api_key_status() -> dict[str, bool]:
    """Tell the dashboard whether ``ANTHROPIC_API_KEY`` is set on the server.

    Returns ``{"configured": bool}`` — never the key itself.
    """
    return {"configured": bool(os.environ.get("ANTHROPIC_API_KEY"))}


@app.post("/dashboard/chat")
async def dashboard_chat(body: ChatRequest, request: Request):
    """Proxy a streaming Anthropic chat call as Server-Sent Events.

    The server holds the API key and forwards each text delta as a
    ``data: {"delta": "..."}`` line. The frontend reads the stream and
    appends deltas into the active assistant bubble.
    """
    project = await db_module.get_project(_db(request), body.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    messages = [m.model_dump() for m in body.messages]
    stream = dashboard_module.stream_anthropic_chat(
        messages=messages,
        system_prompt=body.system_prompt,
        model=body.model,
        max_tokens=body.max_tokens,
    )
    return StreamingResponse(stream, media_type="text/event-stream")


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
                    "for log_task."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "session_name": {"type": "string"},
                    },
                    "required": ["project_id", "session_name"],
                },
            ),
            Tool(
                name="get_goal",
                description=(
                    "Read the current goal state for a project. This is "
                    "the shared directive all sessions work toward. Read "
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
                    "Set or update the goal state. All sessions see this "
                    "immediately. Version increments on each update. "
                    "Content may be a JSON object or a plain string."
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
                    },
                    "required": ["project_id", "content"],
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
                    db, arguments["project_id"], arguments["session_name"]
                )
            elif name == "get_goal":
                goal = await db_module.get_goal(db, arguments["project_id"])
                result = goal or {"error": "goal not set"}
            elif name == "set_goal":
                result = await db_module.set_goal(
                    db, arguments["project_id"], arguments["content"]
                )
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
