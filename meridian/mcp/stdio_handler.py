"""MCP stdio transport handler — build_mcp_server() lives here."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import aiosqlite

from .. import db as db_module
from .. import goal_md as goal_md_module
from .. import handoff as handoff_module
from .. import enqueue as enqueue_module
from .. import toml_config as toml_config_module


def _create_stdio_initialization_options(server: Any) -> Any:
    """Create stdio initialization options with deterministic tool invalidation."""
    from mcp.server.lowlevel.server import NotificationOptions

    return server.create_initialization_options(
        notification_options=NotificationOptions(tools_changed=True),
    )


def build_mcp_server():
    """Construct the MCP server with all eight Meridian tools.

    The server opens its own dedicated SQLite connection because MCP runs in
    a separate event-loop context from FastAPI. Tools return JSON-serialisable
    dicts; descriptions are written verbosely so Claude knows when to use
    them without further prompting.
    """
    # Lazy imports — server.py re-exports build_mcp_server so these must be
    # deferred to function-call time to avoid a circular import at module load.
    from ..server import (
        _dispatch_mcp_tool,
        _start_session_composite,
        _regenerate_claude_md,
        _idle_until_session_done,
        _maybe_add_log_task_nudge,
        _run_session_keepalive_loop,
        _mark_session_connected,
        _load_meridian_md,
        _REPO_ROOT,
        DEFAULT_DATA_DIR,
        DEFAULT_DB_PATH,
    )

    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        GetPromptResult,
        Prompt,
        PromptArgument,
        PromptMessage,
        TextContent,
        Tool,
    )
    import json

    # Slash-command prompt templates — share the exact same registry + builders
    # as the HTTP /mcp surface (meridian/mcp/handler.py) so both transports
    # advertise identical prompts and bodies.
    from . import handler as _handler
    from ..mcp_tools import _MCP_TOOLS_LIST

    server: Server = Server("meridian")

    def _shared_tool(name: str) -> Tool:
        """Build a stdio Tool from the canonical HTTP/MCP schema."""
        schema = next(item for item in _MCP_TOOLS_LIST if item["name"] == name)
        return Tool(
            name=schema["name"],
            description=schema["description"],
            inputSchema=schema["inputSchema"],
        )

    # Lazy holder for the DB connection — opened on first use because the
    # stdio entrypoint is sync up to the point we hit asyncio.run().
    state: dict[str, Any] = {"db": None, "data_dir": None}

    async def _ensure_db() -> aiosqlite.Connection:
        if state["db"] is None:
            try:
                from dotenv import load_dotenv

                load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent.parent / ".env", override=False)
            except ImportError:
                pass
            data_dir = Path(
                os.environ.get("MERIDIAN_DATA_DIR", str(DEFAULT_DATA_DIR))
            )
            data_dir.mkdir(parents=True, exist_ok=True)
            # v1.9.x — read meridian.toml connection profiles (same logic as lifespan).
            # Without this the MCP server always falls back to local SQLite even when
            # the toml says use Postgres.
            _db_override = os.environ.get("MERIDIAN_DB", DEFAULT_DB_PATH)
            if _db_override != ":memory:":
                _toml_url, _toml_conn_name = toml_config_module.get_toml_db_url()
                if _toml_url:
                    os.environ["MERIDIAN_DB_URL"] = _toml_url
                elif _toml_conn_name is not None:
                    os.environ.pop("MERIDIAN_DB_URL", None)
            db_url = os.environ.get("MERIDIAN_DB_URL")
            if db_url:
                state["db"] = await db_module.init_db(db_url)
            else:
                db_path = os.environ.get("MERIDIAN_DB", DEFAULT_DB_PATH)
                if db_path != ":memory:":
                    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
                state["db"] = await db_module.init_db(db_path)
            state["data_dir"] = str(data_dir)
        return state["db"]

    @server.list_prompts()
    async def list_prompts() -> list[Prompt]:
        """Advertise the slash-command prompt templates (shared with HTTP /mcp)."""
        return [
            Prompt(
                name=p["name"],
                description=p.get("description", ""),
                arguments=[
                    PromptArgument(
                        name=a["name"],
                        description=a.get("description", ""),
                        required=bool(a.get("required", False)),
                    )
                    for a in p.get("arguments", [])
                ],
            )
            for p in _handler._MCP_PROMPTS
        ]

    @server.get_prompt()
    async def get_prompt(
        name: str, arguments: dict[str, str] | None
    ) -> GetPromptResult:
        """Render one prompt. ``executor-goal`` pulls live pending sprint items.

        Delegates to the same async builder the HTTP surface uses so the two
        transports never drift. An unknown name raises ValueError, which the MCP
        SDK surfaces to the client as an error (mirrors the -32602 JSON-RPC error
        on the HTTP path).
        """
        db = await _ensure_db()
        messages = await _handler._build_prompt_messages_async(
            name, arguments or {}, db
        )
        description = next(
            (p["description"] for p in _handler._MCP_PROMPTS if p["name"] == name),
            "",
        )
        return GetPromptResult(
            description=description,
            messages=[
                PromptMessage(
                    role=m["role"],
                    content=TextContent(
                        type="text", text=m["content"]["text"]
                    ),
                )
                for m in messages
            ],
        )

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
                    "properties": {
                        "name": {"type": "string"},
                        "execution_mode": {
                            "type": "string",
                            "enum": ["autonomous", "interactive"],
                            "description": (
                                "Executor posture for sessions on this project. "
                                "'autonomous' (default) claims and runs sprint "
                                "items immediately without asking; 'interactive' "
                                "asks for direction first. Editable later in "
                                "dashboard Settings."
                            ),
                        },
                        "parent_project_id": {
                            "type": "string",
                            "description": (
                                "Optional parent project id — makes this a "
                                "subproject that inherits the parent's north_star "
                                "when it has none of its own. Subprojects are one "
                                "level deep: the parent must exist and must not "
                                "itself be a subproject."
                            ),
                        },
                    },
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
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "session_name": {"type": "string"},
                        "human_id": {
                            "type": "string",
                            "description": "Optional human owner identifier.",
                        },
                    },
                    "required": ["session_name"],
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
                    "properties": {"project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."}},
                    "required": [],
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
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "content": {
                            "oneOf": [
                                {"type": "object"},
                                {"type": "string"},
                            ]
                        },
                        "north_star": {"type": "string"},
                        "sprint": {"type": "string"},
                    },
                    "required": ["content"],
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
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "north_star": {"type": "string"},
                        "human_id": {"type": "string"},
                    },
                    "required": ["north_star", "human_id"],
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
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "sprint": {"type": "string"},
                    },
                    "required": ["sprint"],
                },
            ),
            Tool(
                name="set_executor_config",
                description=(
                    "Store per-project executor defaults (repo_path, test_cmd, "
                    "deploy_cmd, etc.) so executor sessions auto-load them via "
                    "start_session(role='executor'). Set once; all executors "
                    "inherit automatically."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "repo_path": {"type": "string", "description": "Absolute path to the repo root."},
                        "env_file": {"type": "string", "description": "Path to .env file for the executor."},
                        "test_cmd": {"type": "string", "description": "Command to run the test suite."},
                        "test_min": {"type": "integer", "description": "Minimum passing test count."},
                        "deploy_cmd": {"type": "string", "description": "Command to deploy (e.g. git push)."},
                        "shell_type": {"type": "string", "description": "Shell to use: bash, powershell, cmd."},
                        "branch": {"type": "string", "description": "Default working branch."},
                    },
                    "required": [],
                },
            ),
            Tool(
                name="claim_file",
                description=(
                    "Claim exclusive edit rights on a file path for this session. "
                    "Returns {claimed: true} on success or {claimed: false, holder_session_id} "
                    "when another session holds the lock. The response also carries a "
                    "`code_notes` list of code-anchored project notes (kind='code') for this "
                    "path — read them before editing. Locks auto-expire after 2 hours. "
                    "Always release_file() when done editing."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "file_path": {"type": "string", "description": "Repo-relative or absolute file path."},
                        "symbol": {"type": "string", "description": "Optional symbol to scope surfaced code-anchored notes to."},
                    },
                    "required": ["session_id", "file_path"],
                },
            ),
            Tool(
                name="release_file",
                description=(
                    "Release a file lock held by this session. "
                    "Silently succeeds if the lock was already released or expired."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "file_path": {"type": "string"},
                    },
                    "required": ["session_id", "file_path"],
                },
            ),
            Tool(
                name="idle_until_session_done",
                description=(
                    "Check whether a specific session has finished. "
                    "Use when you need to wait for another session to complete before editing "
                    "a shared file. Returns {done: true/false, status, suggested_wait_seconds}. "
                    "Poll with the suggested delay until done=true."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "watching_session_id": {"type": "string", "description": "The session ID to watch."},
                    },
                    "required": ["watching_session_id"],
                },
            ),
            Tool(
                name="log_task",
                description=(
                    "Log what this session just did, is doing, or failed "
                    "at. Call frequently to keep all sessions informed of "
                    "progress. Status is one of 'pending', 'done', "
                    "'failed' (default 'done'). Optional kind classifies the "
                    "entry: shipped (default), found, decided, blocked."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "description": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "done", "failed"],
                            "default": "done",
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["shipped", "found", "decided", "blocked"],
                            "description": "Entry taxonomy. shipped=work done, found=discovery, decided=arch choice, blocked=blocker.",
                        },
                    },
                    "required": [
                        "session_id",
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
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "limit": {"type": "integer", "default": 20},
                    },
                    "required": [],
                },
            ),
            Tool(
                name="search_tasks",
                description=(
                    "Search past tasks by keyword or phrase. Uses trigram "
                    "similarity on Postgres, LIKE on SQLite. Returns top "
                    "matches with a similarity score so you can find related "
                    "work done by any session."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
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
                    "EXECUTOR SESSIONS: MANDATORY - call at end of every session "
                    "before disconnect. Never write markdown manually. "
                    "Generate a context handoff file. Call when context is "
                    "filling up or before ending a session. mode='full' "
                    "writes the complete L0/L1/L2 handoff. mode='delta' "
                    "returns a compact session update with completed items, "
                    "remaining pending items, and the next /goal string. "
                    "mode='starter' returns a <=20-line paste-after-/compact "
                    "block: project_id, start_session command, last 5 done, "
                    "top 3 pending IDs, and a /goal string. "
                    "mode='planner' gives strategic context for claude.ai. "
                    "FORWARD THE RETURNED content FIELD VERBATIM to the user "
                    "(a5e8aa74) - the server delivers content as the EXACT raw "
                    "handoff text, with NO Markdown code fence, header, or "
                    "blockquote added around it (earlier versions wrapped it in a "
                    "4-backtick fence under 5234877f; removed because it broke "
                    "copy-paste fidelity). Output the field value as-is, as the "
                    "sole plain-text bubble - do NOT add your own fence, header, "
                    "blockquote, or any other wrapping on the calling side either."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "mode": {
                            "type": "string",
                            "enum": ["full", "delta", "planner", "starter", "goal"],
                        },
                        "session_id": {
                            "type": "string",
                            "description": (
                                "Optional session id for auto-delta on repeat "
                                "calls in the same chat."
                            ),
                        },
                        "version": {
                            "type": "string",
                            "description": (
                                "(b8f89491) Optional explicit sprint-version bucket "
                                "(e.g. 'v0.2.6') to scope this handoff to. Wins over "
                                "the calling session's own stored sprint_version."
                            ),
                        },
                        "force_include_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "(45f519a0) Optional list of sprint-item ids to "
                                "force-include in the pending list for this call "
                                "only, even when their deferred_until is in the "
                                "future. deferred_until is NOT cleared."
                            ),
                        },
                        "strict_evidence": {
                            "type": "boolean",
                            "description": (
                                "(8a883f60) Opt-in fail-closed evidence check. When "
                                "true, a failed/degraded best-effort capability "
                                "makes this call refuse instead of degrading."
                            ),
                        },
                        "strict_pointer_evidence": {
                            "type": "boolean",
                            "description": (
                                "(eb8b6894) Opt-in: the claimable batch's "
                                "UNPROSPECTED exclusion requires a pending item's "
                                "pointer(s) to have actually RESOLVED, not merely "
                                "be present."
                            ),
                        },
                        "checkpoint": {
                            "type": "boolean",
                            "description": (
                                "(ecc8b280) Mark this call as a mid-run progress "
                                "report rather than a final, session-ending "
                                "handoff (full/delta modes only). Never blocked "
                                "by strict_continuation below."
                            ),
                        },
                        "strict_continuation": {
                            "type": "boolean",
                            "description": (
                                "(ecc8b280) Opt-in fail-closed continuation check. "
                                "When true and checkpoint is not set, refuses to "
                                "render/persist a full/delta handoff if actionable "
                                "pending/in_progress items remain with no "
                                "blocker_kind while execution_mode=autonomous."
                            ),
                        },
                    },
                    "required": [],
                },
            ),
            # f46372e8 — load_handoff and verify_handoff_token were never
            # advertised OR dispatched on the stdio transport: this file's
            # list_tools()/call_tool() are the actual implementation behind
            # build_mcp_server() (meridian/server.py re-exports it directly),
            # and neither tool name appeared anywhere in either function, so a
            # self-hosted stdio MCP client (the "Self-hosted (from source)"
            # config in AGENTS.md) could not call either one — every call
            # fell through call_tool()'s final `else` and returned
            # {"error": "unknown tool: ..."}. This silently broke the entire
            # trusted-handoff-channel and token-verification security model
            # (AGENTS.md's "Handoff delivery & trust" section) for anyone on
            # this transport, forcing them to skip verification entirely —
            # exactly the failure mode implicated in the 2026-08-04 incident
            # this sprint item traces back to. _shared_tool() pulls the exact
            # same schema HTTP/MCP already advertises (meridian/mcp_tools.py's
            # _MCP_TOOLS_LIST) so the three transports can never advertise
            # divergent schemas for these tools going forward.
            _shared_tool("load_handoff"),
            _shared_tool("verify_handoff_token"),
            Tool(
                name="get_context_block",
                description=(
                    "Return a compact plain-text context block — north star, "
                    "sprint, pending sprint items, recent tasks, recent "
                    "decisions, active sessions. mode='full' (default) for "
                    "the Code Handoff variant into a fresh Claude Code "
                    "session; mode='chat' for a shorter paste into a new "
                    "claude.ai conversation."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "mode": {
                            "type": "string",
                            "enum": ["full", "chat"],
                            "default": "full",
                        },
                    },
                    "required": [],
                },
            ),
            Tool(
                name="pin_decision",
                description=(
                    "v2.4 — create a pinned decision (editable constitution). "
                    "Use for authoritative current truth that supersedes "
                    "earlier statements. The append-only set_decision log "
                    "captures every micro-decision; pin_decision holds the "
                    "live constitution. category: STRATEGIC, COMPETITIVE, "
                    "TECHNICAL, TACTICAL, BUSINESS, PRODUCT, ARCHITECTURAL. "
                    "priority (urgent|normal|low, default normal) weights "
                    "dashboard ordering + injected context."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "category": {"type": "string"},
                        "priority": {
                            "type": "string",
                            "enum": ["urgent", "normal", "low"],
                        },
                    },
                    "required": ["title", "body"],
                },
            ),
            Tool(
                name="update_decision",
                description=(
                    "v2.4 — patch a pinned decision. Pass new_title + new_body "
                    "to atomically supersede (new active row created, old "
                    "marked superseded with back-link). Otherwise patches "
                    "body/title/category/status/priority in place. Editing the "
                    "body appends the prior body to the append-only edit_log."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "decision_id": {"type": "string"},
                        "new_title": {"type": "string"},
                        "new_body": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "category": {"type": "string"},
                        "priority": {
                            "type": "string",
                            "enum": ["urgent", "normal", "low"],
                        },
                        "status": {"type": "string"},
                    },
                    "required": ["decision_id"],
                },
            ),
            Tool(
                name="get_pinned_decisions",
                description=(
                    "v2.4 — list pinned decisions for a project, highest "
                    "priority first (urgent → normal → low, then newest). "
                    "Active only by default; pass include_superseded=true for "
                    "the full history. Each row includes its priority and a "
                    "parsed edit_log array of prior bodies ({body, ts})."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "include_superseded": {"type": "boolean"},
                    },
                    "required": [],
                },
            ),
            Tool(
                name="request_hitl",
                description=(
                    "v2.4 — surface a question to the human-in-the-loop queue. "
                    "urgency='blocking' pauses this session until answered "
                    "(poll get_hitl_request). 'normal' / 'high' land in the "
                    "dashboard but don't block. assigned_to routes to a "
                    "specific human_id; null = broadcast."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "question": {"type": "string"},
                        "session_id": {"type": "string"},
                        "context": {"type": "string"},
                        "urgency": {
                            "type": "string",
                            "enum": ["normal", "high", "blocking"],
                            "default": "normal",
                        },
                        "assigned_to": {"type": "string"},
                    },
                    "required": ["question"],
                },
            ),
            Tool(
                name="get_hitl_request",
                description=(
                    "v2.4 — poll a HITL request for the human's answer. "
                    "Returns the row with status ('pending'|'answered'|"
                    "'dismissed') and answer text."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"request_id": {"type": "string"}},
                    "required": ["request_id"],
                },
            ),
            Tool(
                name="list_hitl_requests",
                description=(
                    "v2.4 — list HITL requests for a project without needing "
                    "UUIDs. Returns pending queue by default; pass status='all' "
                    "to see answered/dismissed items too. Use before answer_hitl "
                    "or dismiss_hitl to find request IDs."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "status": {
                            "type": "string",
                            "description": "Filter: 'pending' (default), 'answered', 'dismissed', or 'all'.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results, default 50.",
                        },
                    },
                    "required": [],
                },
            ),
            Tool(
                name="answer_hitl",
                description=(
                    "v2.4 — answer a pending HITL request so the waiting "
                    "session can resume. Use list_hitl_requests to find "
                    "request IDs."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "request_id": {"type": "string"},
                        "answer": {"type": "string"},
                        "answered_by": {
                            "type": "string",
                            "description": "Optional human_id of the answerer.",
                        },
                    },
                    "required": ["request_id", "answer"],
                },
            ),
            Tool(
                name="dismiss_hitl",
                description=(
                    "v2.4 — dismiss a HITL request (won't-answer / no longer "
                    "relevant). Stays in audit trail. Use list_hitl_requests "
                    "to find request IDs."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"request_id": {"type": "string"}},
                    "required": ["request_id"],
                },
            ),
            Tool(
                name="update_md_section",
                description=(
                    "v3.3 — propose a replacement for an anchored section of an "
                    "agent template doc (CLAUDE.md or AGENTS.md). Does NOT write "
                    "the file directly: it creates a human-in-the-loop request "
                    "with a diff preview; a human approves it in the dashboard, "
                    "then Meridian replaces that section and stages it for the "
                    "next checkpoint commit. 'anchor' is the section name between "
                    "the MERIDIAN:ANCHOR:START/END comments. (ROADMAP/DECISIONS/"
                    "DEVLOG are append-only and not replaceable.)"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "file": {
                            "type": "string",
                            "description": "CLAUDE.md | AGENTS.md",
                        },
                        "anchor": {"type": "string"},
                        "content": {
                            "type": "string",
                            "description": "Full proposed body for the section.",
                        },
                        "session_id": {"type": "string"},
                        "urgency": {
                            "type": "string",
                            "enum": ["normal", "high", "blocking"],
                        },
                    },
                    "required": ["file", "anchor", "content"],
                },
            ),
            Tool(
                name="list_sessions",
                description=(
                    "v2.4 — list active sessions for a project. Useful to see "
                    "what's currently running before filing new sprint items. "
                    "Pass status='all' to include closed sessions."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "status": {
                            "type": "string",
                            "description": "Filter: 'active' (default) or 'all'.",
                        },
                    },
                    "required": [],
                },
            ),
            Tool(
                name="add_note",
                description=(
                    "Add a per-project wiki note (setup, gotcha, howto, env, ...). "
                    "Free-form title/body; comma-separated tags optional. Optional kind "
                    "(wiki=gotcha/rule/howto, insight=strategic/product analysis, "
                    "reference=external/one-off docs, code=warning/context anchored to a file) "
                    "controls how the dashboard renders it. For a code anchor pass kind='code' "
                    "plus file_path (and optional symbol): the note is surfaced automatically "
                    "when a session calls claim_file/get_file_claims for that path. "
                    "Tag a note 'roadmap' AND pass a committable category (TECHNICAL/ARCHITECTURAL/PRODUCT) "
                    "to also append it to ROADMAP.md's roadmap-notes anchor."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "tags": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": ["wiki", "insight", "reference", "code", "document"],
                            "description": "Note taxonomy for dashboard rendering.",
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["high", "normal", "low"],
                            "description": "high-priority notes surface first in generate_handoff and planner context.",
                        },
                        "file_path": {
                            "type": "string",
                            "description": "Code anchor (kind='code'): path this note warns about; surfaced at claim_file/get_file_claims for the same path.",
                        },
                        "symbol": {
                            "type": "string",
                            "description": "Optional symbol (class/function/method) to scope the code anchor to. File-level anchors surface for any symbol.",
                        },
                        "source": {
                            "type": "string",
                            "description": "Provenance: a URL or file path this note was ingested from (used by kind='document').",
                        },
                        "category": {
                            "type": "string",
                            "description": "Required when tags includes 'roadmap'. E.g. TECHNICAL, ARCHITECTURAL, PRODUCT.",
                        },
                    },
                    "required": ["title", "body"],
                },
            ),
            Tool(
                name="ingest_document",
                description=(
                    "e3f150d0 — turn a Word/PDF/text document into a queryable "
                    "kind='document' note with a source link (a report, thesis "
                    "chapter, or spec doc becomes searchable project memory). "
                    "Pass file_path OR content (one required): file_path is "
                    "extracted SERVER-SIDE, STDLIB ONLY (.txt/.md/.markdown and "
                    "source files read directly; .docx unzipped + paragraphs "
                    "extracted, no python-docx, no new deps). For .pdf or any "
                    "type Meridian can't parse server-side, extract the text with "
                    "your OWN tools and pass it as content (passing a .pdf "
                    "file_path returns an error telling you to do this). title "
                    "defaults to the file's basename; source defaults to "
                    "file_path. The stored body is capped (truncated with a "
                    "'…[truncated]' marker if very long; the kept prefix stays "
                    "searchable). Meridian never summarizes — pass a summary as "
                    "content if you want one stored. Returns the created note "
                    "(id, slug, title, source)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "file_path": {
                            "type": "string",
                            "description": "Path to a .txt/.md/.docx file to extract server-side (stdlib only). For .pdf or other types, pass pre-extracted text as 'content' instead.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Pre-extracted document text. Use for PDFs and any type Meridian can't parse server-side. Takes precedence over file_path.",
                        },
                        "title": {
                            "type": "string",
                            "description": "Note title. Defaults to the file's basename.",
                        },
                        "source": {
                            "type": "string",
                            "description": "Provenance URL/path stored on the note. Defaults to file_path.",
                        },
                        "tags": {
                            "type": "string",
                            "description": "Comma-separated tags.",
                        },
                    },
                    "required": [],
                },
            ),
            Tool(
                name="get_document_structure",
                description=(
                    "13462df2 — return the heading outline of a Word .docx WITHOUT "
                    "ingesting it as a note. Parsed server-side (stdlib only, no "
                    "python-docx, no persistent index); returns paragraph_count, "
                    "heading_count, and an ordered list of headings (level, text, "
                    "para_id) — a fast structural map of a thesis chapter / spec."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Path to a server-accessible .docx file.",
                        },
                    },
                    "required": ["file_path"],
                },
            ),
            Tool(
                name="get_latex_structure",
                description=(
                    "106118cd — parse a LaTeX (.tex) source's structure WITHOUT a "
                    "PDF intermediary (pylatexenc, pure-Python). Returns "
                    "heading_count, an ordered headings outline and a nested tree "
                    "of \\part/\\chapter/\\section/\\subsection/\\subsubsection/"
                    "\\paragraph (level, kind, text, children), unexpanded_inputs "
                    "(\\input/\\include, not expanded) and a bibliography list "
                    "(thebibliography \\bibitem, and \\bibliography{...} + a "
                    "sibling .bib). Malformed LaTeX returns a partial/empty result."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Path to a server-accessible .tex file. A sibling .bib referenced by \\bibliography is resolved relative to it.",
                        },
                        "source": {
                            "type": "string",
                            "description": "Raw LaTeX source, as an alternative to file_path. Ignored when file_path is given.",
                        },
                    },
                    "required": [],
                },
            ),
            Tool(
                name="index_equation",
                description=(
                    "06df6ab3 — index ONE Word equation (OMML) against a "
                    "document already stored in the doc-structure store (via "
                    "ingest_document or a prior reindex — pass the SAME "
                    "source/path as `doc`). omml_or_latex is auto-detected: a "
                    "string starting with '<' is raw OMML XML (stored as-is); "
                    "anything else is LaTeX (real OMML generated best-effort — "
                    "latex2mathml piped through a hand-written MathML->OOXML "
                    "mapper; null omml on an unsupported construct, never an "
                    "error). Before inserting, the normalized LaTeX is "
                    "fuzzy-matched against equations already stored for this "
                    "document — a near-duplicate is still inserted but surfaced "
                    "via near_duplicates so it isn't silently missed. Returns "
                    "{equation, near_duplicates}."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "doc": {"type": "string", "description": "The stored document's source (the path/URL it was ingested/reindexed under)."},
                        "omml_or_latex": {"type": "string", "description": "Raw OMML XML (starts with '<') OR a LaTeX source string."},
                        "semantic_label": {"type": "string", "description": "Optional human label for the equation."},
                    },
                    "required": ["doc", "omml_or_latex"],
                },
            ),
            Tool(
                name="find_similar_equation",
                description=(
                    "06df6ab3 — fuzzy-match a LaTeX string against every "
                    "equation already indexed for one stored document, best "
                    "match first (difflib similarity score 0..1 against each "
                    "stored latex_normalized). Returns {document_id, matches} — "
                    "an empty list (never an error) when the document has no "
                    "stored equations, or doc doesn't resolve."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "doc": {"type": "string", "description": "The stored document's source (the path/URL it was ingested/reindexed under)."},
                        "latex": {"type": "string", "description": "LaTeX source to fuzzy-match against this document's stored equations."},
                        "limit": {"type": "integer", "description": "Max matches to return (default 5)."},
                    },
                    "required": ["doc", "latex"],
                },
            ),
            Tool(
                name="insert_equation",
                description=(
                    "51a595e7 — write an OMML equation DIRECTLY into a stored "
                    "document's source .docx (real OOXML write-back), collapsing "
                    "the manual resolve->open->parse->splice->rewrite->reindex "
                    "flow into one call. The document must already be stored (via "
                    "ingest_document, which registers a docx/latex document's "
                    "structure in the doc-structure store) and have a filesystem "
                    "`source` path. Target the paragraph by `para_id` (its "
                    "w14:paraId, or the synthesized 'p{index}' id surfaced as "
                    "element_id by the read tools). equation_id_or_omml resolves "
                    "in order: an existing indexed equation id for this document "
                    "(reuses its OMML); a string starting with '<' as raw OMML "
                    "XML; else a LaTeX source (converted best-effort). position = "
                    "'append' (default, inline at the paragraph's end) | 'before' "
                    "| 'after' (its own display-equation paragraph). The equation "
                    "index is resynced from the modified file afterward. Returns "
                    "{document_id, source, para_id, position, omml, resync} or "
                    "{error}; the file is never mutated when resolution fails."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "doc": {"type": "string", "description": "The stored document's source (the path it was ingested/reindexed under; must resolve to a .docx on disk)."},
                        "para_id": {"type": "string", "description": "Target paragraph id — its w14:paraId, or the synthesized 'p{index}' id surfaced as element_id by the read tools."},
                        "equation_id_or_omml": {"type": "string", "description": "An existing indexed equation id (reuses its OMML), OR raw OMML XML (starts with '<'), OR a LaTeX source string."},
                        "position": {"type": "string", "enum": ["append", "before", "after"], "description": "Where to place the equation relative to the paragraph. Default 'append' (inline)."},
                    },
                    "required": ["doc", "para_id", "equation_id_or_omml"],
                },
            ),
            Tool(
                name="update_paragraph",
                description=(
                    "f978e588 — ID-addressable docx WRITE (write counterpart of "
                    "the get_element_by_id / paraId read primitive). Rewrites ONE "
                    "paragraph in a stored .docx addressed by its w14:paraId "
                    "('p{index}' fallback) — NEVER by text match — then re-syncs "
                    "the doc_elements index row. Pass the SAME source/path as "
                    "`doc`. Provide EXACTLY ONE of new_text (a plain string, one "
                    "unformatted run) OR runs (a list of runs, each a bare string "
                    "or {text, bold?, italic?, underline?}; basic run formatting "
                    "applied, paragraph style preserved). Returns {document_id, "
                    "para_id, new_text, elements_resynced, source_path}; "
                    "elements_resynced=0 for a plain body paragraph is expected "
                    "(only headings persist as elements). Errors — never a silent "
                    "no-op — when doc/source/para_id doesn't resolve. "
                    "f7ee1ba7 — pass session_id to enable scoped-region claim "
                    "enforcement: the write is rejected if another session owns "
                    "the target para_id or holds a whole-file lock."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "doc": {"type": "string", "description": "The stored document's source (the path/URL it was ingested/reindexed under)."},
                        "para_id": {"type": "string", "description": "The target paragraph's w14:paraId (or 'p{index}' fallback)."},
                        "new_text": {"type": "string", "description": "New paragraph text as a single unformatted run. Provide this OR runs, not both."},
                        "runs": {"type": "array", "description": "List of runs — each a plain string or a {text, bold?, italic?, underline?} object. Provide this OR new_text, not both.", "items": {"type": ["string", "object"]}},
                        "session_id": {"type": "string", "description": "f7ee1ba7 — calling session id. Enables scoped-region enforcement when provided."},
                    },
                    "required": ["doc", "para_id"],
                },
            ),
            Tool(
                name="find_symbol_usages",
                description=(
                    "9605edb0 — READ-ONLY cross-reference tracking. Given a "
                    "document and EITHER a doc_equations id OR a symbol / "
                    "normalized-LaTeX string, resolve it to one target "
                    "normalized-LaTeX (an equation id uses that row's stored "
                    "latex_normalized; a raw string is normalized with the SAME "
                    "normalize_latex the store uses) and return every place the "
                    "target reappears — matching equations plus paragraphs whose "
                    "text contains the symbol. Each hit carries element_id, "
                    "document_id, ordinal, matched_text, context "
                    "(equation|paragraph) and an is_definition/is_reuse flag: the "
                    "earliest occurrence by ordinal is the definition, later ones "
                    "are reuse, so a later mention can be checked to point back "
                    "to the definition. Returns {document_id, target, "
                    "resolved_from, hits} — an empty hits list (never an error) "
                    "when nothing matches or doc doesn't resolve."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "doc": {"type": "string", "description": "The stored document's source (the path/URL it was ingested/reindexed under)."},
                        "symbol_or_equation_id": {"type": "string", "description": "A doc_equations row id, OR a raw symbol / normalized-LaTeX string to track."},
                    },
                    "required": ["doc", "symbol_or_equation_id"],
                },
            ),
            Tool(
                name="index_figure",
                description=(
                    "c623e648 — index ONE figure into the SEMANTIC figure index "
                    "against a document already stored in the doc-structure "
                    "store (via ingest_document or a prior reindex — pass the "
                    "SAME source/path as `doc`). The figure parallel of "
                    "index_equation, COMPLEMENTARY to the structural "
                    "kind='figure' section-tree placement (adds caption dedup + "
                    "similarity, does not replace placement). Provide file_path "
                    "and/or caption. Before inserting, the normalized caption is "
                    "fuzzy-matched against figures already indexed for this "
                    "document — a near-duplicate is still inserted but surfaced "
                    "via near_duplicates so it isn't silently missed. The "
                    "file_path is checked on disk: a missing file is flagged "
                    "(file_exists + missing_files), never a hard failure. Returns "
                    "{figure, near_duplicates, missing_files}."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "doc": {"type": "string", "description": "The stored document's source (the path/URL it was ingested/reindexed under)."},
                        "file_path": {"type": "string", "description": "Path to the figure's asset on disk (checked for existence; missing is flagged, not fatal)."},
                        "caption": {"type": "string", "description": "The figure's caption (drives normalized-caption dedup/similarity)."},
                        "semantic_label": {"type": "string", "description": "Optional human label for the figure."},
                    },
                    "required": ["doc"],
                },
            ),
            Tool(
                name="find_similar_figure",
                description=(
                    "c623e648 — fuzzy-match a free-text description OR a file "
                    "path against every figure already indexed for one stored "
                    "document, best match first (difflib similarity score 0..1, "
                    "the better of the match against normalized_caption and "
                    "against file_path). Returns {document_id, matches} — an "
                    "empty list (never an error) when the document has no "
                    "indexed figures, or doc doesn't resolve."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "doc": {"type": "string", "description": "The stored document's source (the path/URL it was ingested/reindexed under)."},
                        "description_or_path": {"type": "string", "description": "A free-text description OR a file path to fuzzy-match against this document's indexed figures."},
                        "limit": {"type": "integer", "description": "Max matches to return (default 5)."},
                    },
                    "required": ["doc", "description_or_path"],
                },
            ),
            Tool(
                name="index_table",
                description=(
                    "2622182d — index ONE table into the SEMANTIC table index "
                    "against a document already stored in the doc-structure "
                    "store (via ingest_document or a prior reindex — pass the "
                    "SAME source/path as `doc`). The table parallel of "
                    "index_figure, COMPLEMENTARY to the structural "
                    "kind='table' section-tree placement (adds caption dedup + "
                    "similarity, does not replace placement). Provide caption "
                    "and/or table_index. A near-duplicate is still inserted but "
                    "surfaced via near_duplicates. When paired_figure_id is "
                    "omitted, the nearest figure in the same structural section "
                    "is suggested (advisory only). Returns "
                    "{table, near_duplicates}."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "doc": {"type": "string", "description": "The stored document's source (the path/URL it was ingested/reindexed under)."},
                        "table_index": {"type": "integer", "description": "The table's document-order index."},
                        "caption": {"type": "string", "description": "The table's caption (drives normalized-caption dedup/similarity)."},
                        "semantic_label": {"type": "string", "description": "Optional human label for the table."},
                        "paired_figure_id": {"type": "string", "description": "Optional id of a related figure; omit to receive an advisory suggestion."},
                    },
                    "required": ["doc"],
                },
            ),
            Tool(
                name="find_similar_table",
                description=(
                    "2622182d — fuzzy-match a free-text description against "
                    "every table already indexed for one stored document, best "
                    "match first (difflib similarity score 0..1 against "
                    "normalized_caption). Returns {document_id, matches} — an "
                    "empty list (never an error) when the document has no "
                    "indexed tables, or doc doesn't resolve."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "doc": {"type": "string", "description": "The stored document's source (the path/URL it was ingested/reindexed under)."},
                        "description": {"type": "string", "description": "A free-text description to fuzzy-match against this document's indexed tables."},
                        "limit": {"type": "integer", "description": "Max matches to return (default 5)."},
                    },
                    "required": ["doc", "description"],
                },
            ),
            Tool(
                name="check_embedded_staleness",
                description=(
                    "432fcfcb — detect whether a figure or table EMBEDDED into "
                    "a .docx has since drifted from its generating source (a "
                    "plot script output, CSV, etc.). Distinct from the .docx "
                    "mtime staleness check: this checks whether the SOURCE FILE "
                    "that fed the embedded copy changed AFTER the copy was made. "
                    "Covers figures (resolved via file_path + outputs_dir) and "
                    "tables (explicit source_path) via one shared mechanism. "
                    "Uses SHA-256 fingerprint + mtime from the outputs_index "
                    "(same outputs_dir resolve-through as find_similar_figure). "
                    "Returns {stale, reason, source_path, embed_sha256, "
                    "current_sha256, embed_mtime, current_mtime}. "
                    "stale=False: unchanged; stale=True: drifted "
                    "(reason='content-changed'); stale=None: "
                    "source-missing or no-source-provenance (distinct states)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "doc": {"type": "string", "description": "The stored document's source (the path/URL it was ingested/reindexed under)."},
                        "kind": {"type": "string", "enum": ["figure", "table"], "description": "Whether to check a figure or a table."},
                        "figure_id": {"type": "string", "description": "Id of the stored doc_figures row (kind=figure). Used to auto-resolve source_path from file_path."},
                        "table_id": {"type": "string", "description": "Id of the stored doc_tables row (kind=table). Informational; source_path must still be given explicitly for tables."},
                        "source_path": {"type": "string", "description": "Explicit path to the generating source file on disk. For figures this is inferred from file_path+outputs_dir when absent; for tables it must be supplied."},
                        "outputs_dir": {"type": "string", "description": "The meridian-outputs directory to resolve the figure file_path through (figures only). Triggers the same OutputsFtsIndex resolve-through used by find_similar_figure to obtain the embed-time sha256."},
                        "embed_sha256": {"type": "string", "description": "SHA-256 recorded at embed time. When absent the tool looks it up from the outputs_index via outputs_dir."},
                        "embed_mtime": {"type": "number", "description": "Mtime recorded at embed time (Unix float). Used as fallback when no sha256 is available."},
                    },
                    "required": ["doc", "kind"],
                },
            ),
            Tool(
                name="audit_figure_table_provenance",
                description=(
                    "6b657a8b — batch analogue of check_embedded_staleness: "
                    "walks EVERY figure and table stored for a document and "
                    "links each caption to its embedded asset, its "
                    "exact/fallback output match, SHA-256, and generating "
                    "script, in one whole-document integrity report. Figures "
                    "resolve via file_path + outputs_dir (exact match, then a "
                    "relocation-tolerant basename fallback); a basename match "
                    "with 2+ same-name candidates is reported as ambiguous "
                    "(non-authoritative), never silently picked. Tables carry "
                    "no file_path, so their generating script is inferred from "
                    "the caption text itself and traced forward via "
                    "find_outputs_by_source; zero or 2+ traced outputs are "
                    "reported as orphan/ambiguous respectively, never guessed. "
                    "Returns {figures, tables, summary} where each figure/table "
                    "entry carries status in "
                    "{ok, ambiguous, orphan, mismatch, unresolved} plus reason. "
                    "outputs_dir omitted or hosted mode -> every entry reports "
                    "unresolved/no-outputs-dir rather than erroring — a document "
                    "with no local outputs tree is still auditable for "
                    "structure, just not for provenance."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "doc": {"type": "string", "description": "The stored document's source (the path/URL it was ingested/reindexed under)."},
                        "outputs_dir": {"type": "string", "description": "The meridian-outputs directory to resolve figures/tables against. Omitted (or hosted mode) yields unresolved/no-outputs-dir entries rather than an error."},
                    },
                    "required": ["doc"],
                },
            ),
            Tool(
                name="ingest_document_structure",
                description=(
                    "db42acce — persist pre-parsed structural data "
                    "(headings/figures/tables) into the doc-structure store, keyed "
                    "on the SAME source as ingest_document(content=...) so "
                    "find_similar_figure / index_figure / index_table all see the "
                    "correct document_id. Use the tunnel-side "
                    "ingest_local_document_structure tool (meridian-docs extension) "
                    "to parse a local .docx and forward the blocks JSON here. "
                    "Returns {document_id, source, doc_type, element_count}."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — alternative to project_id."},
                        "source": {"type": "string", "description": "Source key matching what ingest_document stored (usually the local file path)."},
                        "blocks": {"type": "string", "description": "JSON-encoded list of body blocks from document_content_tree ('blocks' key). Server converts to elements via elements_from_docx_content_tree."},
                        "doc_type": {"type": "string", "description": "Document type: 'docx' (default) or 'latex'."},
                        "title": {"type": "string", "description": "Document title (optional)."},
                    },
                    "required": ["source", "blocks"],
                },
            ),
            Tool(
                name="search_outputs",
                description=(
                    "a0e9133e — READ-ONLY BM25 full-text search over a run's "
                    "OUTPUTS tree (csv/json/npy + other artifacts), backed by "
                    "DuckDB native FTS. Walks outputs_dir recursively: each "
                    ".csv/.json contributes extracted text + a cheap fingerprint "
                    "(CSV columns / JSON keys / inferred generating_script); .npy "
                    "is metadata-only (never array content); other binaries are "
                    "metadata + name only. Canonical-vs-archival is TWO-STAGE and "
                    "NEVER destructive: a filename heuristic (_old / _old_N / "
                    "leading underscore) flags a CANDIDATE, a SHA-256 hash "
                    "CONFIRMS — an archival copy identical to its canonical twin "
                    "is deprioritized (is_archival=true, canonical_path set), a "
                    "same-pattern file whose content DIFFERS is surfaced as its "
                    "own distinct hit. Nothing is deleted/hidden on disk. Pass "
                    "include_archival=false to drop archival hits. Returns "
                    "{outputs_dir, query, total_indexed, hits:[{path, score, "
                    "bm25, is_archival, canonical_path, kind, generating_script, "
                    "csv_columns, json_keys, size, mtime}]}."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "outputs_dir": {"type": "string", "description": "Absolute path to the outputs directory tree to index and search (walked recursively)."},
                        "query": {"type": "string", "description": "The BM25 query — one or more search terms."},
                        "limit": {"type": "integer", "description": "Max ranked hits to return (default 10)."},
                        "include_archival": {"type": "boolean", "description": "Default true — archival copies deprioritized but returned. false excludes confirmed-archival files."},
                    },
                    "required": ["outputs_dir", "query"],
                },
            ),
            Tool(
                name="search_code_semantic",
                description=(
                    "93fce816 — Cursor-style LOCAL semantic code search over a "
                    "source tree. Parses Python (stdlib ast) + TypeScript/"
                    "JavaScript (tree-sitter) into SEMANTIC CHUNKS at function/"
                    "class/method boundaries PLUS the un-named blocks "
                    "search_graph can't see (module-level dict/list literals, "
                    "bare calls, __main__ guards). Incremental: a content MERKLE "
                    "TREE detects exactly which files changed since the last pass "
                    "and re-chunks only the divergent subtree, so repeat calls on "
                    "an unchanged tree are near-free. Search is HYBRID — DuckDB "
                    "native FTS (Okapi BM25) for keyword match, fused via "
                    "Reciprocal Rank Fusion with an OPTIONAL local-embedding "
                    "vector leg (DuckDB VSS / HNSW cosine over Model2Vec) when "
                    "MERIDIAN_CODE_INDEX_VECTORS is enabled; otherwise pure BM25. "
                    "Entirely local in a DuckDB sidecar — no cloud round-trip. "
                    "Returns {root_dir, query, total_indexed, vectors_enabled, "
                    "vectors_active, hits:[{chunk_id, path, language, kind, name, "
                    "line_start, line_end, content, score, bm25, bm25_rank, "
                    "vector_rank}]}. A missing dir / empty tree returns an empty "
                    "hits list, never an error."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "root_dir": {"type": "string", "description": "Absolute path to the source tree root to index and search (walked recursively; vendored/build dirs pruned)."},
                        "query": {"type": "string", "description": "The search query — keywords and/or a natural-language description of the code you want."},
                        "limit": {"type": "integer", "description": "Max ranked hits to return (default 10)."},
                        "kind": {"type": "string", "description": "Optional chunk-kind filter (e.g. 'function', 'class', 'method', 'interface', 'module')."},
                        "reindex": {"type": "boolean", "description": "Default true — run an incremental Merkle-diff reindex before searching so results reflect the current tree. false searches the last-built index as-is."},
                    },
                    "required": ["root_dir", "query"],
                },
            ),
            Tool(
                name="get_notes",
                description=(
                    "v0.9 — list project notes (newest first), LIGHTWEIGHT by "
                    "default: each item is id/slug/title/tags/kind/priority/"
                    "timestamps with NO body, so the list never overflows "
                    "context. Pull model: scan the list, then read_note("
                    "project_id, slug) for one note's full body. Optional "
                    "``tag`` filter matches any comma-separated tag. Pass "
                    "bodies=true only when you truly need every body inline. "
                    "Pagination: pass limit (default 100, max 500) and/or "
                    "cursor for a {notes, has_more, next_cursor} envelope, then "
                    "re-call with cursor=next_cursor; omit both for the full list."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "tag": {"type": "string"},
                        "bodies": {
                            "type": "boolean",
                            "description": "Default false. true returns full bodies inline (legacy); prefer read_note(slug).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Page size (default 100, clamped 1..500). Passing limit or cursor returns the {notes, has_more, next_cursor} envelope.",
                        },
                        "cursor": {
                            "type": "integer",
                            "description": "Offset cursor from a prior page's next_cursor. Passing it returns the {notes, has_more, next_cursor} envelope.",
                        },
                    },
                    "required": [],
                },
            ),
            Tool(
                name="read_note",
                description=(
                    "5a5bba43 — fetch one project note's full body by its "
                    "per-project slug (the ``slug`` field from get_notes). The "
                    "pull half of the list→read model."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "slug": {"type": "string"},
                    },
                    "required": ["slug"],
                },
            ),
            Tool(
                name="delete_note",
                description=(
                    "v0.9 — hard-delete a project note by id."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"note_id": {"type": "string"}},
                    "required": ["note_id"],
                },
            ),
            Tool(
                name="add_workspace_note",
                description=(
                    "v3.1 — add a workspace-level note that applies across ALL "
                    "projects (onboarding, shared conventions, cross-cutting "
                    "infra). Injected at the top of every project's context "
                    "block + handoff. Tags are comma-separated."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "tags": {"type": "string"},
                    },
                    "required": ["title", "body"],
                },
            ),
            Tool(
                name="get_workspace_notes",
                description=(
                    "v3.1 — list workspace-level notes (newest first). "
                    "Optional ``tag`` substring filter."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"tag": {"type": "string"}},
                    "required": [],
                },
            ),
            _shared_tool("get_workspace_proposals"),
            Tool(
                name="pin_workspace_decision",
                description=(
                    "v3.1 — pin a workspace-level decision that applies across "
                    "ALL projects (shared architecture, org-wide standards). "
                    "Injected at the top of every project's context block + "
                    "handoff. category is free-text."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "category": {"type": "string"},
                    },
                    "required": ["title", "body"],
                },
            ),
            Tool(
                name="get_workspace_decisions",
                description=(
                    "v3.1 — list workspace-level pinned decisions (active "
                    "only by default, newest first)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "include_superseded": {"type": "boolean"},
                    },
                    "required": [],
                },
            ),
            Tool(
                name="add_workspace_sprint_item",
                description=(
                    "Add an item to the workspace personal backlog — a "
                    "cross-project board NOT tied to any single project "
                    "(thesis + Meridian + personal goals in one view). "
                    "'group' is the cross-project bucket (e.g. 'thesis', "
                    "'meridian', 'personal'). New items start as 'todo'."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "group": {"type": "string"},
                        "human_id": {"type": "string"},
                    },
                    "required": ["title"],
                },
            ),
            Tool(
                name="get_workspace_sprint_items",
                description=(
                    "List workspace personal-backlog items (grouped by "
                    "'group', then position). Optional 'status' and 'group' "
                    "filters."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "group": {"type": "string"},
                    },
                    "required": [],
                },
            ),
            Tool(
                name="update_workspace_sprint_item",
                description=(
                    "Edit a workspace personal-backlog item: title, status, "
                    "group, or human_id. Only the fields passed are changed; "
                    "empty string clears group/human_id. done/skipped/failed "
                    "stamps completed_at."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "string"},
                        "title": {"type": "string"},
                        "status": {"type": "string"},
                        "group": {"type": "string"},
                        "human_id": {"type": "string"},
                    },
                    "required": ["item_id"],
                },
            ),
            Tool(
                name="complete_workspace_sprint_item",
                description=(
                    "Mark a workspace personal-backlog item done (stamps "
                    "completed_at)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "string"},
                    },
                    "required": ["item_id"],
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
                name="complete_task",
                description=(
                    "Mark a claimed task as done and log an optional "
                    "completion note. Call this after finishing the work "
                    "described in the task. Pair with claim_task at the "
                    "start of work. If the task was already marked done "
                    "or failed by another process, the call is safe and "
                    "returns the current task state."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "task_id": {"type": "string"},
                        "session_id": {"type": "string"},
                        "note": {
                            "type": "string",
                            "description": "optional completion note appended to the task description",
                        },
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
            _shared_tool("add_sprint_item"),
            Tool(
                name="update_sprint_item",
                description=(
                    "Edit fields on an existing sprint item: title, version, "
                    "notes, human_id (assignee), or group. Only the fields you "
                    "pass are changed; omitted fields are left untouched. Pass "
                    "an empty string for human_id or group to clear it. Returns "
                    "the updated item, or an error if the id is unknown."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "item_id": {"type": "string"},
                        "title": {"type": "string", "description": "New title."},
                        "version": {
                            "type": "string",
                            "description": "Move the item to a different version/sprint bucket.",
                        },
                        "notes": {
                            "type": "string",
                            "description": "Free-form note/context shown on the item.",
                        },
                        "human_id": {
                            "type": "string",
                            "description": "Reassign to a person (assignee); empty string clears it.",
                        },
                        "group": {
                            "type": "string",
                            "description": "Objective name to group the item under (item_group); empty string clears it.",
                        },
                    },
                    "required": ["item_id"],
                },
            ),
            Tool(
                name="complete_sprint_item",
                description=(
                    "Mark a sprint item done. Pass task_id to link the "
                    "task that shipped it; the timeline correlates them. "
                    "Returns the updated item or null if the id is unknown. "
                    "If the notes reference a commit whose GitHub Actions CI is "
                    "genuinely FAILING, completion is REFUSED (error CI_FAILING); "
                    "pass override_ci=true to acknowledge and complete anyway. "
                    "Unknown/pending CI never blocks."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "item_id": {"type": "string"},
                        "task_id": {"type": "string"},
                        "override_ci": {"type": "boolean", "description": "Set true to complete even when GitHub Actions CI for the referenced commit is failing (escape hatch — the failing CI is recorded on the item)."},
                    },
                    "required": ["item_id"],
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
                name="fail_sprint_item",
                description=(
                    "Mark a sprint item failed — attempted but could not "
                    "be shipped. Provide a one-line ``reason`` so the next "
                    "session knows what went wrong. The item stays on the "
                    "board in 'failed' state so it isn't silently lost."
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
                name="push_sprint_item",
                description=(
                    "Push a sprint item to a future version. Use this when "
                    "scope creep means the item won't fit this sprint. "
                    "``to_version`` records where it was moved (e.g. 'v2.0'). "
                    "The item status becomes 'pushed'; the next sprint can "
                    "add it fresh with add_sprint_item."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "item_id": {"type": "string"},
                        "to_version": {
                            "type": "string",
                            "description": "Target version string, e.g. 'v2.0'.",
                        },
                    },
                    "required": ["project_id", "item_id", "to_version"],
                },
            ),
            Tool(
                name="get_sprint_items",
                description=(
                    "List sprint items for a project. Optional status filter "
                    "(todo|pending|in_progress|done|failed|skipped|pushed). "
                    "Pass human=false to exclude human-assigned tasks (default: true). "
                    "Cold sessions read this to know what's still owed. By default, items "
                    "sharing a parent_id or item_group collapse into one summary row per "
                    "cluster — pass expand=true for the full ungrouped list."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "status": {
                            "type": "string",
                            "enum": [
                                "pending", "todo", "in_progress",
                                "done", "failed", "skipped", "pushed",
                            ],
                        },
                        "human": {
                            "type": "boolean",
                            "description": "Include items with milestone_type='human'. Default: true. Pass false to hide human tasks (used by executor sessions).",
                        },
                        "expand": {
                            "type": "boolean",
                            "description": "Default false: collapse parent_id/item_group clusters (2+ items) into one summary row each. Pass true for the full ungrouped item list.",
                        },
                    },
                    "required": [],
                },
            ),
            Tool(
                name="add_sprint_item_pointer",
                description=(
                    "2976e168 — attach a GENERIC POINTER to a sprint item: a "
                    "composable reference to a thing-in-a-source (LSP Location + "
                    "W3C Web Annotation Selector). targets is an ARRAY of {uri, "
                    "selector, subSelector?} (native multi-file); selector.type is "
                    "range (line span) | symbol (qualified_name) | node_id "
                    "(doc_store element) | zotero_key. An optional subSelector "
                    "nests finer granularity. Stored as JSON, not per-domain "
                    "columns. Malformed pointers are rejected. Returns the stored "
                    "pointer."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "sprint_item_id": {"type": "string", "description": "The sprint item to attach the pointer to."},
                        "source_type": {"type": "string", "description": "Domain: code | docs | citation | … (free text)."},
                        "targets": {
                            "type": "array",
                            "description": "Non-empty array of {uri, selector, subSelector?} targets.",
                            "items": {"type": "object"},
                        },
                        "label": {"type": "string", "description": "Optional human-readable label."},
                    },
                    "required": ["sprint_item_id", "source_type", "targets"],
                },
            ),
            Tool(
                name="get_sprint_item_pointers",
                description=(
                    "2976e168 — list the generic pointers on a sprint item "
                    "(oldest first). Each is {id, source_type, targets, label, "
                    "created_at}. Read-only; does NOT resolve targets (use "
                    "resolve_sprint_item_pointers for that)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "sprint_item_id": {"type": "string", "description": "The sprint item whose pointers to list."},
                    },
                    "required": ["sprint_item_id"],
                },
            ),
            Tool(
                name="resolve_sprint_item_pointers",
                description=(
                    "2976e168 — resolve every generic pointer on a sprint item to "
                    "its concrete location, dispatching by selector.type: range "
                    "as-is; symbol → file+line via the cached code graph; node_id "
                    "→ doc_store element; zotero_key → Zotero local API. A "
                    "subSelector narrows the outer resolution. Best-effort — an "
                    "unresolvable target yields {resolved:false, reason}; the pass "
                    "NEVER fails."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "sprint_item_id": {"type": "string", "description": "The sprint item whose pointers to resolve."},
                    },
                    "required": ["sprint_item_id"],
                },
            ),
            Tool(
                name="start_session",
                description=(
                    "Single call to start a coordinated session. Registers "
                    "you, reads goal + ambient context, shows recent work, "
                    "lists active sessions, and tells you where the handoff "
                    "file is. If project_id is unknown, call list_projects() "
                    "first. Call this INSTEAD of register_session + "
                    "get_goal + get_tasks separately. Returns: session_id, "
                    "goal (with ambient_tasks), recent_tasks (last 10), "
                    "active_sessions, handoff_exists, handoff_path, files."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "session_name": {"type": "string"},
                        "human_id": {
                            "type": "string",
                            "description": "Optional human owner identifier.",
                        },
                        "client": {
                            "type": "string",
                            "enum": ["claude-code", "claude-desktop", "cursor", "other"],
                            "description": "Client app — used for presence indicators.",
                        },
                        "role": {
                            "type": "string",
                            "enum": ["executor"],
                            "description": "Pass 'executor' to inject executor_config and credentials guidance.",
                        },
                        "version": {
                            "type": "string",
                            "description": (
                                "Optional sprint-version bucket (e.g. 'v0.1.x') to "
                                "scope this session to — sprint progress/items in the "
                                "orientation and /goal filter to it. Omit to auto-infer "
                                "the bucket with the most pending items."
                            ),
                        },
                        "expand_stale": {
                            "type": "boolean",
                            "description": (
                                "Default false. When a goal field (north_star / "
                                "version_goal / sprint) is flagged stale by the "
                                "coherence check, the orientation collapses it to a "
                                "one-line summary instead of dumping the week-old "
                                "body. Pass true to expand those fields to their "
                                "full text (get_session_brief also returns full text)."
                            ),
                        },
                    },
                    "required": ["session_name"],
                },
            ),
            Tool(
                name="list_projects",
                description=(
                    "Call first when project_id is unknown. Returns the "
                    "current tenant's projects as [{id, name, sprint, "
                    "created_at}] newest first."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="get_project_by_name",
                description=(
                    "Look up a project by name (case-insensitive substring "
                    "match). Returns the first hit with id, name, and sprint. "
                    "Use this when you know the project name but not the UUID."
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
            Tool(
                name="get_session_brief",
                description=(
                    "Single-call session orientation — sprint focus, pending sprint "
                    "items, recent tasks, blocking failures, and pending HITL in a "
                    "compact XML envelope (<500 tokens). Use instead of start_session "
                    "+ get_context_block for worker/automation sessions."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
                        "role": {
                            "type": "string",
                            "enum": ["worker", "planner", "review"],
                            "description": "Context verbosity. worker=sprint+tasks only.",
                        },
                    },
                    "required": [],
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
                    # 3b6ff466 — optional parent_project_id → one-level-deep
                    # subproject; an invalid/nested parent raises ValueError.
                    try:
                        result = await db_module.create_project(
                            db, arguments["name"],
                            execution_mode=arguments.get("execution_mode"),
                            parent_project_id=arguments.get("parent_project_id"),
                        )
                    except ValueError as exc:
                        result = {"error": str(exc)}
            elif name == "register_session":
                result = await db_module.register_session(
                    db,
                    arguments["project_id"],
                    arguments["session_name"],
                    human_id=arguments.get("human_id"),
                    client_type=arguments.get("client"),
                )
            elif name == "get_goal":
                _goal_timed_out = False
                try:
                    goal = await asyncio.wait_for(
                        db_module.get_goal(db, arguments["project_id"]),
                        timeout=30.0,
                    )
                except asyncio.TimeoutError:
                    result = {"error": "timeout", "message": "get_goal timed out. Try get_context_block instead."}
                    _goal_timed_out = True
                if not _goal_timed_out and goal is None:
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
                elif not _goal_timed_out:
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
                    decisions_raw = await db_module.get_decisions(
                        db, arguments["project_id"]
                    )
                    # Truncate to last 3000 chars — MCP context has hard limits
                    if decisions_raw and len(decisions_raw) > 3000:
                        decisions_raw = decisions_raw[-3000:]
                    goal["decisions"] = decisions_raw
                    goal["xml"] = db_module.build_goal_xml(
                        goal, project_name, goal["ambient_tasks"], coherence,
                        decisions=decisions_raw,
                    )
                    goal["cache_blocks"] = db_module.build_goal_cache_blocks(
                        goal, project_name, goal["ambient_tasks"]
                    )
                    # v2.3 — inject MERIDIAN.md session instructions so
                    # every cold session learns the coordination protocol
                    # without explicit prompting. Project-root override
                    # wins over the built-in default.
                    meridian_md = _load_meridian_md()
                    if meridian_md:
                        goal["meridian_instructions"] = meridian_md
                    result = goal
            elif name == "set_goal":
                result = await db_module.set_goal(
                    db,
                    arguments["project_id"],
                    arguments["content"],
                    north_star=arguments.get("north_star"),
                    sprint=arguments.get("sprint"),
                    minor=bool(arguments.get("minor", False)),
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
            elif name == "set_executor_config":
                cfg_fields = {
                    k: arguments[k]
                    for k in ("repo_path", "env_file", "test_cmd", "test_min",
                              "deploy_cmd", "shell_type", "branch")
                    if k in arguments
                }
                try:
                    result = await db_module.set_executor_config(
                        db, arguments["project_id"], cfg_fields
                    )
                except ValueError as exc:
                    result = {"error": str(exc)}
            elif name == "claim_file":
                try:
                    result = await db_module.claim_file(
                        db,
                        arguments["file_path"],
                        arguments["session_id"],
                        symbol=arguments.get("symbol"),
                    )
                except ValueError as exc:
                    result = {"error": str(exc)}
            elif name == "release_file":
                released = await db_module.release_file(
                    db,
                    arguments["file_path"],
                    arguments["session_id"],
                )
                result = {"released": released, "file_path": arguments["file_path"]}
            elif name == "claim_docx_region":
                # f7ee1ba7 — Model B scoped docx-region claim.
                result = await db_module.claim_docx_region(
                    db,
                    session_id=arguments["session_id"],
                    file_path=arguments["file_path"],
                    element_id=arguments["element_id"],
                )
            elif name == "get_docx_region_claims":
                # f7ee1ba7 — read-only: active scoped region claims on a .docx.
                result = {
                    "file_path": arguments["file_path"],
                    "claims": await db_module.get_docx_region_claims(
                        db, arguments["file_path"]
                    ),
                }
            elif name == "release_docx_region_claims":
                # f7ee1ba7 — release scoped docx-region claims.
                released = await db_module.release_docx_region_claims(
                    db, arguments["session_id"],
                    file_path=arguments.get("file_path"),
                    element_id=arguments.get("element_id"),
                )
                result = {
                    "released": released,
                    "session_id": arguments["session_id"],
                    "file_path": arguments.get("file_path"),
                    "element_id": arguments.get("element_id"),
                }
            elif name == "idle_until_session_done":
                _idle_kwargs = {}
                if arguments.get("timeout_seconds") is not None:
                    _idle_kwargs["timeout_seconds"] = float(arguments["timeout_seconds"])
                result = await _idle_until_session_done(
                    db,
                    arguments["watching_session_id"],
                    **_idle_kwargs,
                )
            elif name == "log_task":
                result = await db_module.log_task(
                    db,
                    arguments["session_id"],
                    arguments["project_id"],
                    arguments["description"],
                    arguments.get("status", "done"),
                    parent_task_id=arguments.get("parent_task_id"),
                )
                result = await _maybe_add_log_task_nudge(db, result)
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
                session_id = arguments.get("session_id")
                if not isinstance(session_id, str):
                    session_id = None
                mode = handoff_module.resolve_handoff_mode(
                    arguments.get("mode"),
                    session_id,
                )
                # 45f519a0/b8f89491/8a883f60/eb8b6894 — mirror handler.py's HTTP
                # MCP dispatch exactly, so the stdio transport stops silently
                # dropping these args (the gap this comment fixes: previously
                # only mode/session_id were ever read here).
                _stdio_version = arguments.get("version")
                if isinstance(_stdio_version, str) and not _stdio_version.strip():
                    _stdio_version = None
                _stdio_force_include_ids: list[str] | None = None
                _raw_stdio_fii = arguments.get("force_include_ids")
                if isinstance(_raw_stdio_fii, list):
                    _stdio_force_include_ids = [str(x) for x in _raw_stdio_fii if x]
                _stdio_strict_evidence = bool(arguments.get("strict_evidence"))
                _stdio_strict_pointer_evidence = bool(
                    arguments.get("strict_pointer_evidence")
                )
                # 3cab355a — mirror handler.py's out-param: one entry per
                # requested force_include_ids id that failed validation
                # (unknown/cross-project/cross-version/not-pending). See
                # handoff.generate_handoff's force_include_rejected docstring.
                _stdio_force_include_rejected: list[dict[str, Any]] = []
                # ecc8b280 — mirror handler.py's continuation gate args exactly.
                _stdio_checkpoint = bool(arguments.get("checkpoint"))
                _stdio_strict_continuation = bool(arguments.get("strict_continuation"))
                _stdio_continuation_status: dict[str, Any] = {}
                _handoff_evidence_blocked = False
                _handoff_continuation_blocked = False
                try:
                    path, content, _ = await asyncio.wait_for(
                        handoff_module.generate_handoff(
                            db,
                            arguments["project_id"],
                            state["data_dir"],
                            mode=mode,
                            session_id=session_id,
                            version=_stdio_version,
                            force_include_ids=_stdio_force_include_ids,
                            strict_evidence=_stdio_strict_evidence,
                            strict_pointer_evidence=_stdio_strict_pointer_evidence,
                            force_include_rejected=_stdio_force_include_rejected,
                            checkpoint=_stdio_checkpoint,
                            strict_continuation=_stdio_strict_continuation,
                            continuation_status=_stdio_continuation_status,
                        ),
                        timeout=90.0,
                    )
                except asyncio.TimeoutError:
                    path, content = await handoff_module._generate_handoff_l0(
                        db, arguments["project_id"], state["data_dir"]
                    )
                    mode = "full"
                except handoff_module.HandoffEvidenceRequired as exc:
                    # 8a883f60 — mirror handler.py's structured refusal: nothing
                    # was rendered/persisted, so surface that instead of falling
                    # through to the generic error string.
                    result = {
                        "error": "HANDOFF_EVIDENCE_BLOCKED",
                        "project_id": arguments["project_id"],
                        "evidence_status": exc.evidence_status,
                        "evidence_errors": exc.errors,
                        "message": str(exc),
                    }
                    _handoff_evidence_blocked = True
                except handoff_module.HandoffContinuationRequired as exc:
                    # ecc8b280 — mirror handler.py's structured refusal: nothing
                    # was rendered/persisted for this call.
                    result = {
                        "error": "HANDOFF_CONTINUATION_BLOCKED",
                        "project_id": arguments["project_id"],
                        "continuation_status": exc.continuation_state,
                        "message": str(exc),
                    }
                    _handoff_continuation_blocked = True
                if not _handoff_evidence_blocked and not _handoff_continuation_blocked:
                    # a5e8aa74 — return content EXACTLY as generate_handoff rendered
                    # it, via the shared helper meridian/mcp/handler.py and
                    # meridian/routes/handoff.py also use, so all transports emit a
                    # byte-identical, unwrapped contract. This replaces the 5234877f
                    # four-backtick fence: the fence broke verbatim forwarding of the
                    # /goal block (see format_handoff_mcp_content's docstring).
                    result = {
                        "path": path,
                        "content": handoff_module.format_handoff_mcp_content(content),
                        "mode": mode,
                        # 3cab355a — see the comment above the generate_handoff
                        # call; [] when force_include_ids was empty/absent or
                        # the 90s timeout fired before validation ran.
                        "force_include_rejected": _stdio_force_include_rejected,
                        "continuation_status": _stdio_continuation_status,
                    }
            elif name == "get_context_block":
                # v2.3 — reuse the dispatch impl so HTTP and stdio share one path.
                result = await _dispatch_mcp_tool(
                    "get_context_block", arguments, db, state["data_dir"]
                )
            elif name in ("load_handoff", "verify_handoff_token"):
                # f46372e8 — these two were advertised nowhere and dispatched
                # nowhere on the stdio transport (see the list_tools() comment
                # above _shared_tool("load_handoff")); route through the same
                # _dispatch_mcp_tool -> _handle_task_tools path the HTTP MCP
                # transport uses so all three transports share one
                # implementation and can't drift out of sync with each other.
                result = await _dispatch_mcp_tool(
                    name, arguments, db, state["data_dir"]
                )
            elif name in (
                "pin_decision", "update_decision", "get_pinned_decisions",
                "archive_decision",
                "request_hitl", "get_hitl_request",
                "list_hitl_requests", "answer_hitl", "dismiss_hitl",
                "update_md_section",
                "list_sessions",
                "add_note", "ingest_document", "get_document_structure", "get_latex_structure", "get_notes", "read_note", "delete_note",
                "get_citation_edges", "resolve_citations",
                "index_equation", "find_similar_equation", "insert_equation", "update_paragraph", "find_symbol_usages",
                "index_figure", "find_similar_figure",
                "index_table", "find_similar_table",
                "check_embedded_staleness",
                "audit_figure_table_provenance",
                "search_outputs",
                "search_code_semantic",
                "add_sprint_item",
                "add_sprint_item_pointer", "get_sprint_item_pointers",
                "resolve_sprint_item_pointers",
                "add_workspace_note", "get_workspace_notes",
                "get_workspace_proposals",
                "pin_workspace_decision", "get_workspace_decisions",
            ):
                # v2.4/v0.9 — share dispatch with HTTP MCP so both surfaces stay in sync.
                result = await _dispatch_mcp_tool(
                    name, arguments, db, state["data_dir"]
                )
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
                task = await db_module.claim_task(db, arguments["task_id"], arguments["session_id"])
                result = task or {"error": "task not found, already claimed, or not pending"}
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
                # 3689f680 — compact by default (full block via compact=False).
                # a76cb7c0 — optional `version` scopes the session to a
                # sprint-version bucket.
                # ce3693e4 — resolve project_name → project_id. The stdio path
                # never went through _dispatch_mcp_tool's central resolver, so
                # start_session(project_name=...) raised a bare
                # KeyError('project_id') here even though every project-scoped
                # stdio schema advertises project_name. Resolve it (project_id
                # wins when both are given), then guard so a missing project
                # returns a clean error instead of a KeyError.
                _pid = (arguments.get("project_id") or "").strip()
                if not _pid and arguments.get("project_name"):
                    _p = await db_module.get_project_by_name(
                        db, str(arguments["project_name"])
                    )
                    _pid = (_p or {}).get("id", "") if _p else ""
                if not _pid:
                    result = {"error": "project_id (or project_name) is required"}
                else:
                    # 599d0097 — session_name is optional; generate a default
                    # from the first pending item when omitted/blank.
                    _sname = (arguments.get("session_name") or "").strip()
                    if not _sname:
                        _sname = await db_module.generate_default_session_name(
                            db, _pid
                        )
                    result = await _start_session_composite(
                        db,
                        _pid,
                        _sname,
                        state["data_dir"],
                        human_id=arguments.get("human_id"),
                        client_type=arguments.get("client"),
                        role=arguments.get("role"),
                        compact=arguments.get("compact", True),
                        version=arguments.get("version"),
                        # 2b4e69aa — collapse coherence-flagged-stale goal fields
                        # to a one-liner by default; opt back into full bodies
                        # with expand_stale=true.
                        expand_stale=bool(arguments.get("expand_stale", False)),
                    )
            elif name == "list_projects":
                result = await db_module.list_project_summaries(db)
            elif name == "get_project_by_name":
                name_arg = arguments["name"]
                project = await db_module.get_project_by_name(db, name_arg)
                if project is None:
                    result = {
                        "error": f"no project found matching '{name_arg}'"
                    }
                else:
                    result = {
                        "id": project["id"],
                        "name": project["name"],
                        "sprint": project.get("sprint"),
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
            elif name == "update_sprint_item":
                item = await db_module.patch_sprint_item(
                    db,
                    arguments["project_id"],
                    arguments["item_id"],
                    title=arguments.get("title"),
                    version=arguments.get("version"),
                    notes=arguments.get("notes"),
                    human_id=arguments.get("human_id"),
                    item_group=arguments.get("group"),
                )
                result = item or {"error": "sprint item not found"}
            elif name == "complete_sprint_item":
                try:
                    item = await db_module.complete_sprint_item(
                        db,
                        arguments["project_id"],
                        arguments["item_id"],
                        task_id=arguments.get("task_id"),
                    )
                    result = item or {"error": "sprint item not found"}
                except ValueError as exc:
                    result = {"error": str(exc)}
            elif name == "skip_sprint_item":
                try:
                    item = await db_module.skip_sprint_item(
                        db,
                        arguments["project_id"],
                        arguments["item_id"],
                        reason=arguments.get("reason"),
                    )
                    result = item or {"error": "sprint item not found"}
                except ValueError as exc:
                    result = {"error": str(exc)}
            elif name == "fail_sprint_item":
                try:
                    item = await db_module.fail_sprint_item(
                        db,
                        arguments["project_id"],
                        arguments["item_id"],
                        reason=arguments.get("reason"),
                    )
                    result = item or {"error": "sprint item not found"}
                except ValueError as exc:
                    result = {"error": str(exc)}
            elif name == "push_sprint_item":
                try:
                    item = await db_module.push_sprint_item(
                        db,
                        arguments["project_id"],
                        arguments["item_id"],
                        arguments["to_version"],
                    )
                    result = item or {"error": "sprint item not found"}
                except ValueError as exc:
                    result = {"error": str(exc)}
            elif name == "get_sprint_items":
                _sh_items = await db_module.get_sprint_items(
                    db,
                    arguments["project_id"],
                    status=arguments.get("status"),
                )
                result = db_module.collapse_sprint_item_clusters(
                    _sh_items, expand=bool(arguments.get("expand", False))
                )
            elif name == "complete_task":
                task = await db_module.get_task(db, arguments["task_id"])
                if task is None:
                    result = {"error": f"task {arguments['task_id']} not found"}
                else:
                    note = arguments.get("note", "")
                    new_desc = (
                        f"{task['description']} — {note}" if note else task["description"]
                    )
                    updated = await db_module.update_task(
                        db,
                        arguments["task_id"],
                        status="done",
                        description=new_desc,
                    )
                    db_module._publish_task("task_updated", updated or task)
                    result = updated or task
                    # Update CLAUDE.md with current sprint state.
                    await _regenerate_claude_md(db, task["project_id"], _REPO_ROOT)
            elif name == "get_session_brief":
                result = await _dispatch_mcp_tool(
                    "get_session_brief", arguments, db, state["data_dir"]
                )
            elif name == "get_session_log":
                session_id_arg = arguments.get("session_id", "")
                run = await db_module.get_executor_run_by_session(db, session_id_arg)
                if run is None:
                    result = {"error": "no run found for session"}
                else:
                    result = {
                        "run_id": run["id"],
                        "session_id": run["session_id"],
                        "started_at": run["started_at"],
                        "ended_at": run.get("ended_at"),
                        "status": run["status"],
                        "task_count": run["task_count"],
                        "transcript": run["transcript"],
                    }
            else:
                result = {"error": f"unknown tool: {name}"}
        except Exception as exc:  # noqa: BLE001 — surface to MCP client
            result = {"error": f"{type(exc).__name__}: {exc}"}

        # Implicit last_seen bump: any tool call that carries a session_id
        # keeps the session alive without requiring explicit heartbeats.
        _session_id = arguments.get("session_id")
        if _session_id and name != "heartbeat":
            try:
                await db_module.update_session_seen(db, _session_id)
            except Exception:
                pass
        # Track liveness so the keepalive loop can hold last_seen fresh while
        # this session is heads-down on non-MCP work (git/bash/file edits).
        _mark_session_connected(_session_id)

        return [TextContent(type="text", text=json.dumps(result, default=str))]

    async def run_stdio() -> None:
        """Run the MCP server over stdio until the client disconnects."""
        # Keep this local session's last_seen fresh while it's busy on non-MCP
        # work (git/bash/file ops) — otherwise a second local session sees it as
        # dead inside the live window and starts on the same files.
        keepalive_db = await _ensure_db()
        keepalive = asyncio.create_task(_run_session_keepalive_loop(keepalive_db))
        try:
            async with stdio_server() as (read_stream, write_stream):
                await server.run(
                    read_stream,
                    write_stream,
                    # Advertise the standard invalidation capability. Claude
                    # Desktop and Cursor own their local tool caches; the
                    # server must not guess or edit vendor-specific cache
                    # paths. They can instead refresh from the canonical
                    # tools/list response when this notification is emitted.
                    _create_stdio_initialization_options(server),
                )
        finally:
            keepalive.cancel()
            try:
                await keepalive
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    return server, run_stdio
