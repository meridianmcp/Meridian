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

    server: Server = Server("meridian")

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
                        "repo_path": {"type": "string", "description": "Absolute path to the repo root."},
                        "env_file": {"type": "string", "description": "Path to .env file for the executor."},
                        "test_cmd": {"type": "string", "description": "Command to run the test suite."},
                        "test_min": {"type": "integer", "description": "Minimum passing test count."},
                        "deploy_cmd": {"type": "string", "description": "Command to deploy (e.g. git push)."},
                        "shell_type": {"type": "string", "description": "Shell to use: bash, powershell, cmd."},
                        "branch": {"type": "string", "description": "Default working branch."},
                    },
                    "required": ["project_id"],
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
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 5},
                    },
                    "required": ["project_id", "query"],
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
                    "mode='starter' returns a ≤20-line paste-after-/compact "
                    "block: project_id, start_session command, last 5 done, "
                    "top 3 pending IDs, and a /goal string. "
                    "mode='planner' gives strategic context for claude.ai."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "mode": {
                            "type": "string",
                            "enum": ["full", "delta", "planner", "starter"],
                        },
                        "session_id": {
                            "type": "string",
                            "description": (
                                "Optional session id for auto-delta on repeat "
                                "calls in the same chat."
                            ),
                        },
                    },
                    "required": ["project_id"],
                },
            ),
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
                        "mode": {
                            "type": "string",
                            "enum": ["full", "chat"],
                            "default": "full",
                        },
                    },
                    "required": ["project_id"],
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
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "category": {"type": "string"},
                        "priority": {
                            "type": "string",
                            "enum": ["urgent", "normal", "low"],
                        },
                    },
                    "required": ["project_id", "title", "body"],
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
                        "include_superseded": {"type": "boolean"},
                    },
                    "required": ["project_id"],
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
                    "required": ["project_id", "question"],
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
                        "status": {
                            "type": "string",
                            "description": "Filter: 'pending' (default), 'answered', 'dismissed', or 'all'.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results, default 50.",
                        },
                    },
                    "required": ["project_id"],
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
                    "required": ["project_id", "file", "anchor", "content"],
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
                        "status": {
                            "type": "string",
                            "description": "Filter: 'active' (default) or 'all'.",
                        },
                    },
                    "required": ["project_id"],
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
                    "required": ["project_id", "title", "body"],
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
                    "required": ["project_id"],
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
                    "required": ["project_id"],
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
                        "slug": {"type": "string"},
                    },
                    "required": ["project_id", "slug"],
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
            Tool(
                name="add_sprint_item",
                description=(
                    "ALWAYS call get_sprint_items first to check for existing "
                    "pending items before adding. "
                    "Append a todo item to the project's machine-trackable "
                    "sprint checklist (v1.1). Use this when you start work on "
                    "a new version so the next session sees what's in flight. "
                    "Optional: group items under a named objective with "
                    "'group'; attribute the item to a person with 'human_id'. "
                    "Use 'depends_on' to block this item until another item "
                    "finishes; 'failure_mode=stop' stops the chain if the "
                    "parent fails. Blocks near-duplicate titles (>=60% word "
                    "overlap with an open pending/in_progress item) and returns "
                    "the conflict instead of creating a duplicate; pass "
                    "force=true to add anyway. Returns the new item."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "version": {"type": "string"},
                        "title": {"type": "string"},
                        "group": {
                            "type": "string",
                            "description": (
                                "Optional objective name to group this item "
                                "under on the sprint board."
                            ),
                        },
                        "human_id": {
                            "type": "string",
                            "description": "Optional: person this item is assigned to.",
                        },
                        "depends_on": {
                            "type": "string",
                            "description": "Sprint item id that must complete before this item is claimable.",
                        },
                        "failure_mode": {
                            "type": "string",
                            "enum": ["continue", "stop"],
                            "description": "'stop' blocks this item if the parent fails. Default: 'continue'.",
                        },
                        "milestone_type": {
                            "type": "string",
                            "enum": ["task", "milestone", "human"],
                            "description": "'milestone' renders as a timeline marker; 'human' marks a task for a human (hidden from executor sessions). Default: 'task'.",
                        },
                        "force": {
                            "type": "boolean",
                            "description": "Override the duplicate guard and add the item even if its title closely matches an existing open item. Default: false.",
                        },
                    },
                    "required": ["project_id", "version", "title"],
                },
            ),
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
                    "required": ["project_id", "item_id"],
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
                    "Cold sessions read this to know what's still owed."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
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
                    },
                    "required": ["project_id", "session_name"],
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
                        "role": {
                            "type": "string",
                            "enum": ["worker", "planner", "review"],
                            "description": "Context verbosity. worker=sprint+tasks only.",
                        },
                    },
                    "required": ["project_id"],
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
            elif name == "idle_until_session_done":
                result = await _idle_until_session_done(
                    db,
                    arguments["watching_session_id"],
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
                try:
                    path, content = await asyncio.wait_for(
                        handoff_module.generate_handoff(
                            db,
                            arguments["project_id"],
                            state["data_dir"],
                            mode=mode,
                            session_id=session_id,
                        ),
                        timeout=90.0,
                    )
                except asyncio.TimeoutError:
                    path, content = await handoff_module._generate_handoff_l0(
                        db, arguments["project_id"], state["data_dir"]
                    )
                    mode = "full"
                result = {"path": path, "content": content, "mode": mode}
            elif name == "get_context_block":
                # v2.3 — reuse the dispatch impl so HTTP and stdio share one path.
                result = await _dispatch_mcp_tool(
                    "get_context_block", arguments, db, state["data_dir"]
                )
            elif name in (
                "pin_decision", "update_decision", "get_pinned_decisions",
                "archive_decision",
                "request_hitl", "get_hitl_request",
                "list_hitl_requests", "answer_hitl", "dismiss_hitl",
                "update_md_section",
                "list_sessions",
                "add_note", "ingest_document", "get_notes", "read_note", "delete_note",
                "add_workspace_note", "get_workspace_notes",
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
                result = await _start_session_composite(
                    db,
                    arguments["project_id"],
                    arguments["session_name"],
                    state["data_dir"],
                    human_id=arguments.get("human_id"),
                    client_type=arguments.get("client"),
                    role=arguments.get("role"),
                    compact=arguments.get("compact", True),
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
            elif name == "add_sprint_item":
                result = await db_module.add_sprint_item(
                    db,
                    arguments["project_id"],
                    arguments["version"],
                    arguments["title"],
                    group=arguments.get("group"),
                    human_id=arguments.get("human_id"),
                    depends_on=arguments.get("depends_on"),
                    failure_mode=arguments.get("failure_mode"),
                    milestone_type=arguments.get("milestone_type", "task"),
                    force=bool(arguments.get("force", False)),
                )
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
            elif name == "fail_sprint_item":
                item = await db_module.fail_sprint_item(
                    db,
                    arguments["project_id"],
                    arguments["item_id"],
                    reason=arguments.get("reason"),
                )
                result = item or {"error": "sprint item not found"}
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
                result = await db_module.get_sprint_items(
                    db,
                    arguments["project_id"],
                    status=arguments.get("status"),
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
                    server.create_initialization_options(),
                )
        finally:
            keepalive.cancel()
            try:
                await keepalive
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    return server, run_stdio
