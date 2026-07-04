"""Persistence layer for Meridian — SQLite (default) or Postgres.

All functions are async.  The ``db`` parameter accepted by every function is
either an ``aiosqlite.Connection`` (SQLite path) or a
``meridian.pg_adapter.PostgresConnection`` (Postgres path, activated when
``MERIDIAN_DB_URL`` is set).  Both expose the same async cursor API so the
function bodies are identical for both backends.

IDs are uuid4 strings; timestamps are ISO-format strings produced by
SQLite's ``datetime('now')`` or the Postgres equivalent in pg_adapter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

_log = logging.getLogger(__name__)

_UNSET = object()  # sentinel for "not passed" in optional keyword args

# ecf69de8 — per-project executor posture. 'autonomous' (default): claim and run
# pending sprint items immediately without asking for direction. 'interactive':
# review the items and ask the human which to start first. Anything else falls
# back to the default so a bad value never persists.
EXECUTION_MODES = ("autonomous", "interactive")
DEFAULT_EXECUTION_MODE = "autonomous"


def normalize_execution_mode(mode: str | None) -> str:
    """Coerce an execution_mode input to a valid value.

    Returns the lowercased value when it's one of EXECUTION_MODES, otherwise the
    default ('autonomous'). Never raises — callers can pass user input directly.
    """
    if isinstance(mode, str):
        candidate = mode.strip().lower()
        if candidate in EXECUTION_MODES:
            return candidate
    return DEFAULT_EXECUTION_MODE

# ---------------------------------------------------------------------------
# Transparent field encryption (Fernet, MERIDIAN_ENCRYPTION_KEY env var).
# enc:<base64> prefix allows zero-downtime migration — plaintext values are
# returned as-is until overwritten, then automatically encrypted on next write.
# ---------------------------------------------------------------------------

_FERNET_INSTANCE: Any = None


def _fernet() -> Any:
    global _FERNET_INSTANCE
    if _FERNET_INSTANCE is None:
        key = os.environ.get("MERIDIAN_ENCRYPTION_KEY", "")
        if key:
            from cryptography.fernet import Fernet  # noqa: PLC0415
            _FERNET_INSTANCE = Fernet(key.encode() if isinstance(key, str) else key)
    return _FERNET_INSTANCE


def encrypt_field(value: str | None) -> str | None:
    """Encrypt value; returns ``enc:<base64>`` string. Passthrough when no key."""
    if not value:
        return value
    f = _fernet()
    if not f:
        return value
    return "enc:" + f.encrypt(value.encode()).decode()


def decrypt_field(value: str | None) -> str | None:
    """Decrypt an ``enc:`` prefixed value; plaintext values pass through."""
    if not value:
        return value
    if not value.startswith("enc:"):
        return value
    f = _fernet()
    if not f:
        return value
    return f.decrypt(value[4:].encode()).decode()


# In-process pub/sub. Subscribers register an asyncio.Queue keyed by
# project_id; any call to log_task / update_task forwards a serialisable
# event dict so dashboard WebSockets see MCP-driven activity in real time.
_TASK_LISTENERS: dict[str, set[asyncio.Queue]] = {}


def subscribe_tasks(project_id: str) -> asyncio.Queue:
    """Register a new listener queue for a project's task stream."""
    q: asyncio.Queue = asyncio.Queue()
    _TASK_LISTENERS.setdefault(project_id, set()).add(q)
    return q


def unsubscribe_tasks(project_id: str, queue: asyncio.Queue) -> None:
    """Drop a previously-registered listener queue. Safe to call twice."""
    bucket = _TASK_LISTENERS.get(project_id)
    if bucket and queue in bucket:
        bucket.discard(queue)
        if not bucket:
            _TASK_LISTENERS.pop(project_id, None)


def publish_global(event: dict[str, Any]) -> None:
    """Fan-out a global event (e.g. update_available) to every WS subscriber."""
    for listeners in list(_TASK_LISTENERS.values()):
        for q in list(listeners):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


def _publish_task(event_type: str, task: dict[str, Any]) -> None:
    """Fan-out a task event to every subscriber of the project.

    Synchronous, non-blocking: drops the event if a queue is full. The
    dashboard WebSocket reader drains its queue continuously so a full
    queue means the socket is wedged — letting it back-pressure is wrong.
    """
    project_id = task.get("project_id")
    if not project_id:
        return
    listeners = _TASK_LISTENERS.get(project_id)
    if not listeners:
        return
    event = {"type": event_type, "task": task}
    for q in list(listeners):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def _publish_project_event(project_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """Fan-out a project-scoped event (not necessarily task-shaped) to WS subscribers.

    Used for sprint item status changes, goal updates, and session start events
    so the dashboard refreshes in real-time without polling.
    """
    listeners = _TASK_LISTENERS.get(project_id)
    n_listeners = len(listeners) if listeners else 0
    _log.debug(
        "WS broadcast: type=%s project=%s listeners=%d",
        event_type, project_id[:8], n_listeners,
    )
    if not listeners:
        return
    event = {"type": event_type, "project_id": project_id, **payload}
    for q in list(listeners):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            _log.warning("WS broadcast queue full for project %s — event dropped", project_id[:8])

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    creator_human_id TEXT,
    goal_mode TEXT NOT NULL DEFAULT 'manual'
        CHECK (goal_mode IN ('manual', 'auto')),
    decisions TEXT,
    max_pinned_decisions INTEGER NOT NULL DEFAULT 20,
    executor_config TEXT,
    hitl_auto_answer INTEGER NOT NULL DEFAULT 0,
    icon TEXT,
    agent_instructions TEXT,
    execution_mode TEXT NOT NULL DEFAULT 'autonomous'
        CHECK (execution_mode IN ('autonomous', 'interactive')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'parked', 'archived')),
    priority TEXT NOT NULL DEFAULT 'P2'
        CHECK (priority IN ('P0', 'P1', 'P2')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS goal_states (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    content TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    goal_north_star TEXT,
    goal_sprint TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    human_id TEXT,
    session_type TEXT DEFAULT 'human',
    client_type TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','idle','closed','archived')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    checkpoint_data TEXT,
    sprint_version TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_log (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'done'
        CHECK (status IN ('pending','in_progress','done','failed','pending-hitl','backlog','future','backburner')),
    claimed_by TEXT,
    claimed_at TEXT,
    sprint_item_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v1.9.x — waitlist: pre-launch email capture for hosted tier.
-- email is unique; note is optional "how will you use it" context.
CREATE TABLE IF NOT EXISTS waitlist (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v1.1 — sprint_items: machine-trackable checklist alongside the
-- free-text sprint field. Each item is one thing the sprint needs
-- shipped (a version number, a feature, a fix). status moves through
-- todo → in_progress → done|failed|skipped|pushed. task_id optionally
-- links the item to the task that finished it. item_group lets items
-- be grouped by objective. pushed_to records the version an item was
-- deferred to. human_id attributes the item to a person.
CREATE TABLE IF NOT EXISTS sprint_items (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    version TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','todo','in_progress','provisional_complete','done','failed','skipped','pushed','indeterminate')),
    item_group TEXT,
    pushed_to TEXT,
    human_id TEXT,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    claimed_at TEXT,
    task_id TEXT,
    notes TEXT,
    feedback_thumb SMALLINT,
    feedback_note TEXT,
    milestone_type TEXT NOT NULL DEFAULT 'task',
    parent_id TEXT DEFAULT NULL REFERENCES sprint_items(id),
    split_from TEXT DEFAULT NULL,
    merged_into TEXT DEFAULT NULL,
    merged_from TEXT DEFAULT NULL,
    -- 4f02340e: mixed-ownership task chains. owner of a subtask: 'human' | 'ai'
    -- | NULL (unassigned). Drives the alternating claim/handoff state machine
    -- in _advance_task_chain. NULL on parents and on legacy single-owner items.
    owner TEXT DEFAULT NULL,
    -- b944c905: human-readable per-project id (title slug, deduped). UUID stays
    -- the primary key; slug is the board-facing identifier.
    slug TEXT
);

-- v2.4 — decisions_pinned: editable constitution alongside the append-only
-- decisions log. Pinned decisions are short-lived authoritative statements
-- ("we're using psycopg3", "pricing tier X is $20") that supersede each
-- other. The append-only log captures every micro-decision; pinned holds
-- the current truth. status='superseded' rows keep history; UI filters
-- to 'active'.
CREATE TABLE IF NOT EXISTS decisions_pinned (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'TECHNICAL',
    priority TEXT NOT NULL DEFAULT 'normal',
    edit_log TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','superseded')),
    superseded_by TEXT REFERENCES decisions_pinned(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 0b711a9d — insights: durable strategic understanding, a first-class knowledge
-- type distinct from decisions (choices w/ lifecycle) and notes (reference).
-- horizon ∈ permanent|year|quarter (validated in Python, no DB CHECK so the
-- vocabulary can grow without a table rebuild). permanent insights always
-- surface in get_planning_brief.
CREATE TABLE IF NOT EXISTS insights (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    horizon TEXT NOT NULL DEFAULT 'quarter',
    tags TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v0.9 — project_notes: per-project wiki for setup, gotchas, env vars,
-- how-tos. Plain table; no goal hierarchy, no version, no history.
-- Tags are comma-separated free-form (setup, gotcha, howto, env, ...).
CREATE TABLE IF NOT EXISTS project_notes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tags TEXT,
    note_kind TEXT,
    slug TEXT,
    file_path TEXT,
    symbol TEXT,
    source TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v2.4 — hitl_requests: human-in-the-loop coordination queue. Sessions
-- can pause waiting for a human answer (urgency='blocking') or surface a
-- non-blocking question (urgency='normal'/'high'). assigned_to routes to
-- a specific human_id; null = broadcast. answered_at + answered_by close
-- the loop for audit.
CREATE TABLE IF NOT EXISTS hitl_requests (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id TEXT REFERENCES sessions(id),
    question TEXT NOT NULL,
    context TEXT,
    urgency TEXT NOT NULL DEFAULT 'normal'
        CHECK (urgency IN ('normal','high','blocking')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','answered','dismissed')),
    answer TEXT,
    answered_by TEXT,
    assigned_to TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    answered_at TEXT,
    kind TEXT NOT NULL DEFAULT 'question',
    payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_goal_project
    ON goal_states(project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_project
    ON sessions(project_id, status);
CREATE INDEX IF NOT EXISTS idx_decisions_pinned_project
    ON decisions_pinned(project_id, status);
CREATE INDEX IF NOT EXISTS idx_hitl_project
    ON hitl_requests(project_id, status);
CREATE INDEX IF NOT EXISTS idx_hitl_assigned
    ON hitl_requests(assigned_to, status);
CREATE INDEX IF NOT EXISTS idx_notes_project
    ON project_notes(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_project
    ON task_log(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_session
    ON task_log(session_id);
CREATE INDEX IF NOT EXISTS idx_sprint_items_project
    ON sprint_items(project_id, status);
CREATE INDEX IF NOT EXISTS idx_sprint_items_version
    ON sprint_items(project_id, version);

-- v2.0 — hosted tier: tenants, web sessions, API bearer tokens
-- v2.2 — plan updated to standard/pro (was free/pro/team)
-- v2.9 — free tier re-introduced as default plan for new signups
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    google_sub TEXT UNIQUE,
    github_sub TEXT UNIQUE,
    microsoft_sub TEXT UNIQUE,
    neon_project_id TEXT,
    neon_db_url TEXT,
    stripe_customer_id TEXT,
    stripe_metered_item_id TEXT,
    plan TEXT NOT NULL DEFAULT 'free',
    pool_project_id TEXT,
    notification_prefs TEXT NOT NULL DEFAULT '{}',
    trial_started_at TEXT,
    inactivity_expires_at TEXT,
    is_internal INTEGER NOT NULL DEFAULT 0,
    tunnel_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT,
    token_type TEXT NOT NULL DEFAULT 'readwrite',
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    tenant_id TEXT REFERENCES tenants(id),
    client_id TEXT,
    exp BIGINT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS oauth_codes (
    code TEXT PRIMARY KEY,
    tenant_id TEXT,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS device_codes (
    device_code TEXT PRIMARY KEY,
    user_code TEXT NOT NULL UNIQUE,
    tenant_id TEXT,
    expires_at TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v2.1 dark — multi-user roles, not exposed in UI or API at launch
-- G5.19/G5.20 — role widened to include 'admin'; github_access caps
-- repo-touching MCP tools per invitee. App layer (meridian.roles) is the
-- source of truth for valid values; no DB-level CHECK so adding a future
-- role doesn't require a table rebuild on every SQLite install.
CREATE TABLE IF NOT EXISTS workspace_members (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    github_access TEXT NOT NULL DEFAULT 'read',
    -- d116642e: project-level invites foundation.
    -- NULL = workspace-wide member (sees all projects, current behavior);
    -- set = project-scoped member (listing-only scoping, see get_workspaces_for_email).
    -- Airtight per-request access enforcement is intentionally NOT implemented
    -- here — it is gated on the open product decision in pin b11c7cf6.
    project_id TEXT,
    token_hash TEXT,
    invited_at TEXT NOT NULL DEFAULT (datetime('now')),
    joined_at TEXT
);

-- v2.1 dark — per-tenant named environments (prod/staging/dev), not exposed at launch
CREATE TABLE IF NOT EXISTS tenant_environments (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    neon_db_name TEXT,
    token_hash TEXT,
    is_default INTEGER NOT NULL DEFAULT 0
        CHECK (is_default IN (0,1))
);

-- v2.2 — Neon pool project registry.  Each pool project holds up to
-- MAX_CUSTOMERS_PER_PROJECT customer databases.  Pool projects are
-- provisioned lazily: a new Neon project is created when all existing
-- ones are full.  Tier 'standard' uses NEON_API_KEY; 'pro' uses NEON_API_KEY_PRO.
CREATE TABLE IF NOT EXISTS neon_pool_projects (
    id TEXT PRIMARY KEY,
    neon_project_id TEXT NOT NULL UNIQUE,
    tier TEXT NOT NULL DEFAULT 'standard'
        CHECK (tier IN ('free','standard','pro')),
    customer_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v2.5 — admins: DB-managed admin email list. Replaces MERIDIAN_ADMIN_EMAILS env var.
-- Env var still works as bootstrap fallback (before DB is available).
CREATE TABLE IF NOT EXISTS admins (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    added_by TEXT,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    notes TEXT
);

-- v2.6 — session_notes: ephemeral per-session scratch pad.
-- Auto-deleted when session closes. Exposed via add_sprint_note / get_sprint_notes MCP tools.
CREATE TABLE IF NOT EXISTS session_notes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    -- 0d7de2a2: thinking_sync. note_kind classifies a sprint note. NULL/'note'
    -- = a normal note; 'thinking' = a HOOKS_DEBUG_STATE scratchpad note
    -- auto-persisted by the client-side thinking_sync hook. The dashboard
    -- renders 'thinking' notes with a distinct icon.
    note_kind TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v3.0 — executor_runs: one row per Claude Code / worker session execution.
-- transcript accumulates task descriptions as the session logs work;
-- finalized on session close with ended_at + task_count.
CREATE TABLE IF NOT EXISTS executor_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','done','failed')),
    transcript TEXT NOT NULL DEFAULT '',
    task_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_executor_runs_session
    ON executor_runs(session_id);
CREATE INDEX IF NOT EXISTS idx_executor_runs_project
    ON executor_runs(project_id, started_at DESC);

CREATE TABLE IF NOT EXISTS file_locks (
    id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_file_locks_session
    ON file_locks(session_id);
CREATE INDEX IF NOT EXISTS idx_file_locks_expires
    ON file_locks(expires_at);

-- 501ec93f — resource_locks: generalize file_locks to any typed resource
-- (file:path, db:migrations, mcp_tool:name, route:METHOD:/path, pypi:publish,
-- github:tag). Same TTL + UNIQUE primitive: one holder per resource_id at a
-- time, auto-expiring by TTL or owning-session heartbeat.
CREATE TABLE IF NOT EXISTS resource_locks (
    id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL UNIQUE,
    resource_type TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resource_locks_session
    ON resource_locks(session_id);
CREATE INDEX IF NOT EXISTS idx_resource_locks_expires
    ON resource_locks(expires_at);
CREATE INDEX IF NOT EXISTS idx_resource_locks_type
    ON resource_locks(resource_type);

-- Symbol-level parallel protection (4bac57ff): claim individual class/function
-- /method line ranges so two sessions can edit the same file without colliding.
CREATE TABLE IF NOT EXISTS file_symbol_claims (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    symbol_name TEXT NOT NULL,
    symbol_type TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- Soft-release: an active claim has released_at IS NULL; released rows are
    -- retained so hotspot scoring can count distinct sessions over time.
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_file_symbol_claims_file
    ON file_symbol_claims(file_path);
CREATE INDEX IF NOT EXISTS idx_file_symbol_claims_session
    ON file_symbol_claims(session_id);

-- Blog CMS (6234f9b8): admin-authored posts; draft until published.
CREATE TABLE IF NOT EXISTS blog_posts (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    body_md TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','published')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_blog_posts_status ON blog_posts(status);

CREATE TABLE IF NOT EXISTS active_worktrees (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    item_id TEXT,
    branch TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    removed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_active_worktrees_session
    ON active_worktrees(session_id);
CREATE INDEX IF NOT EXISTS idx_active_worktrees_project
    ON active_worktrees(project_id, removed_at);

-- v3.1 — workspace layer: tenant-global notes + decisions that live above
-- individual projects. Unlike project_notes / decisions_pinned (keyed by
-- project_id), these belong to the workspace as a whole (one workspace per
-- Meridian instance / hosted tenant DB) and are injected at the top of every
-- project's context block + handoff. Tags are comma-separated free-form.
CREATE TABLE IF NOT EXISTS workspace_notes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tags TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workspace_decisions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'TECHNICAL',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','superseded')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_workspace_notes_created
    ON workspace_notes(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_workspace_decisions_status
    ON workspace_decisions(status, created_at DESC);

-- workspace_sprint_items: a tenant-global personal backlog that is NOT tied to
-- any single project, so a solo dev can track cross-project goals (thesis,
-- Meridian, personal) in one board. Mirrors the useful subset of sprint_items
-- but keyed by tenant_id instead of project_id; ``item_group`` is the
-- cross-project bucket (e.g. 'thesis'/'meridian'/'personal'). ``position``
-- orders items within a group. One workspace per DB, same isolation model as
-- workspace_notes / workspace_decisions.
CREATE TABLE IF NOT EXISTS workspace_sprint_items (
    id TEXT PRIMARY KEY,
    tenant_id TEXT,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'todo'
        CHECK (status IN ('todo','pending','in_progress','done','skipped','failed')),
    item_group TEXT,
    human_id TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_workspace_sprint_items_tenant
    ON workspace_sprint_items(tenant_id, status);

-- v3.4 — workspace-level settings: tenant-global defaults that every project
-- session can read at startup (e.g. a default HITL auto-answer posture, a
-- default sprint label). Singleton row (id='singleton') per workspace DB, same
-- one-workspace-per-DB model as workspace_notes / workspace_decisions.
CREATE TABLE IF NOT EXISTS workspace_settings (
    id TEXT PRIMARY KEY DEFAULT 'singleton',
    hitl_auto_answer_default INTEGER NOT NULL DEFAULT 0,
    sprint_name_default TEXT,
    display_name TEXT,
    log_task_sprint_nudge_threshold INTEGER NOT NULL DEFAULT 5,
    handoff_template TEXT,
    execution_mode_default TEXT,
    code_intel_enabled_default INTEGER,
    loop_enabled_default INTEGER NOT NULL DEFAULT 1,
    auto_refresh_enabled INTEGER NOT NULL DEFAULT 0,
    refresh_interval_turns INTEGER NOT NULL DEFAULT 10,
    refresh_triggers TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    email TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feedback_tenant
    ON feedback(tenant_id);
CREATE INDEX IF NOT EXISTS idx_feedback_created
    ON feedback(created_at);
"""


def get_default_human_id() -> str | None:
    """Return the best available human identifier for the current environment.

    Checks in order: ``MERIDIAN_HUMAN_ID`` env var, ``USER`` (POSIX),
    ``USERNAME`` (Windows), then the short hostname.  Returns ``None`` only
    when none of the above are set (very unusual).
    """
    import socket

    return (
        os.environ.get("MERIDIAN_HUMAN_ID")
        or os.environ.get("USER")
        or os.environ.get("USERNAME")
        or (socket.gethostname().split(".")[0][:20] or None)
    )


def _new_id() -> str:
    """Return a fresh uuid4 string."""
    return str(uuid.uuid4())


def _row_to_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    """Convert an aiosqlite Row to a plain dict, or None."""
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def _decode_content(raw: str) -> Any:
    """Goal content is stored as text. If it parses as JSON, return the
    parsed object; otherwise return the raw string."""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _encode_content(content: Any) -> str:
    """Serialize goal content to text for storage."""
    if isinstance(content, str):
        return content
    return json.dumps(content)


from .migrations import *  # noqa: F401,F403

async def init_db(db_path: str) -> aiosqlite.Connection:
    """Open the database, apply schema, and return the connection.

    When ``db_path`` starts with ``postgresql://`` or ``postgres://`` the
    function opens a psycopg3 connection pool and returns a
    :class:`~meridian.pg_adapter.PostgresConnection` instead of an
    aiosqlite connection.  Both expose the same async cursor API so all
    callers are unaffected.

    For Postgres, only ``CREATE TABLE IF NOT EXISTS`` is run â€” migration
    helpers are skipped because a fresh Postgres DB already has the full
    current schema.

    The caller owns the returned connection and is responsible for closing it.
    """
    if db_path.startswith(("postgresql://", "postgres://")):
        from ..pg_adapter import init_pg_db  # local import keeps SQLite path fast

        return await init_pg_db(db_path)  # type: ignore[return-value]

    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode = WAL")   # concurrent read+write
    await db.execute("PRAGMA busy_timeout = 5000")  # retry up to 5 s before LOCKED
    await db.execute("PRAGMA foreign_keys = ON")
    await db.executescript(CREATE_TABLES)
    await db.commit()
    await _migrate_task_log_hitl(db)
    await _migrate_task_log_backlog_future(db)
    await _migrate_task_log_backburner(db)
    await _migrate_human_identity(db)
    await _migrate_task_claims(db)
    await _migrate_task_sprint_link(db)
    await _migrate_decisions(db)
    await _migrate_session_type(db)
    await _migrate_session_summary(db)
    await _migrate_parent_session_id(db)
    await _migrate_goal_mode(db)
    await _migrate_goal_hierarchy(db)
    await _migrate_worker_pid(db)
    await _migrate_rewind_token(db)
    await _migrate_project_settings(db)
    await _migrate_neon_pool_projects_free_tier(db)
    await _migrate_sessions_archived(db)
    await _migrate_sprint_items_v2(db)
    await _migrate_drop_chat_tables(db)
    await _migrate_hosted_tables(db)
    await _migrate_goal_field_timestamps(db)
    await _migrate_v24_task_tree_and_framework(db)
    await _migrate_v24_pinned_decisions_and_hitl(db)
    await _migrate_v09_notes_and_magic_links(db)
    await _migrate_v25_feedback_and_notifications(db)
    await _migrate_dunning_fields(db)
    await _migrate_overage_fields(db)
    await _migrate_sprint_item_dependencies(db)
    await _migrate_v26_client_type(db)
    await _migrate_decisions_free_category(db)
    await _migrate_tenants_free_plan(db)
    await _migrate_session_notes(db)
    await _migrate_executor_runs(db)
    await _migrate_file_locks(db)
    await _migrate_file_symbol_claims(db)
    await _migrate_blog_posts(db)
    await _migrate_milestone_type(db)
    await _migrate_ntfy_notifications(db)
    await _migrate_notify_email(db)
    await _migrate_github_integration(db)
    await _migrate_workspace_layer(db)
    await _migrate_checkpoint_data(db)
    await _migrate_v33_hitl_kind_payload(db)
    await _migrate_v34_hitl_auto_answer(db)
    await _migrate_v34_workspace_settings(db)
    await _migrate_project_icon(db)
    await _migrate_tenants_is_internal(db)
    await _migrate_admin_plan(db)
    await _migrate_workspace_members_rbac(db)
    await _migrate_workspace_members_project_scope(db)
    await _migrate_sprint_items_indeterminate(db)
    await _migrate_sprint_item_tree(db)
    await _migrate_sprint_items_provisional_complete(db)
    await _migrate_api_token_type(db)
    await _migrate_api_tokens_expires_at(db)
    await _migrate_oauth_codes_table(db)
    await _migrate_device_codes_table(db)
    await _migrate_github_to_projects(db)
    await _migrate_touches_files(db)
    await _migrate_touches_resources(db)
    await _migrate_resource_locks(db)
    await _migrate_sprint_item_stall_count(db)
    await _migrate_active_worktrees(db)
    await _migrate_workspace_tenant_isolation(db)
    await _migrate_workspace_sprint_board(db)
    await _migrate_registered_hostnames(db)
    await _migrate_queued_session(db)
    await _migrate_pending_goal(db)
    await _migrate_parallel_safety(db)
    await _migrate_changelog_entries(db)
    await _migrate_agent_instructions(db)
    await _backfill_agent_instructions(db)
    await _migrate_note_kind(db)
    await _migrate_tunnel_active(db)
    await _migrate_code_intel(db)
    await _migrate_tunnel_plugins(db)
    await _migrate_tunnel_plugins_by_host(db)
    await _migrate_notes_priority(db)
    await _migrate_task_log_kind(db)
    await _migrate_note_slug(db)
    await _migrate_oauth_refresh_tokens(db)
    await _migrate_decision_priority_edit_log(db)
    await _migrate_code_anchored_notes(db)
    await _migrate_note_source(db)
    await _migrate_session_sprint_version(db)
    await _migrate_project_execution_mode(db)
    await _migrate_decision_code_anchor(db)
    await _migrate_session_graph_snapshots(db)
    await _migrate_agent_tasks_table(db)
    await _migrate_sprint_item_owner(db)
    await _migrate_session_note_kind(db)
    await _migrate_handoffs_table(db)
    await _migrate_decision_assumption(db)
    await _migrate_github_connections(db)
    await _migrate_sprint_item_quality_gates(db)
    await _migrate_parallel_primitives(db)
    await _migrate_project_status_priority(db)
    await _migrate_signup_attempts(db)
    await _migrate_user_session_metadata(db)
    await _migrate_provision_queue(db)
    await _migrate_codebase_graph_entities(db)
    await _migrate_insights_table(db)
    await _migrate_sprint_item_slug(db)
    await _migrate_capture_insight_notes_to_insights(db)
    return db




async def create_project(
    db: aiosqlite.Connection,
    name: str,
    human_id: str | None = None,
    execution_mode: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Insert a project and return it as a dict. Raises if the name exists.

    ``human_id`` (when provided) is recorded as the project's
    ``creator_human_id``. The creator's id is the only one allowed to
    update the goal state once goal-ownership enforcement is active.

    ``execution_mode`` (ecf69de8) sets the project's executor posture —
    'autonomous' (default) or 'interactive'. Invalid values normalise to
    'autonomous' so a bad input never persists.

    ``tenant_id`` (0bf67524) — when given, the new project is seeded from the
    workspace's cascade defaults (execution_mode_default, hitl_auto_answer_default,
    code_intel_enabled_default) for any field the caller didn't set explicitly.
    Existing projects are never touched; this is a creation-time cascade only.
    """
    # Resolve workspace defaults for seeding (best-effort; never block creation).
    _ws: dict[str, Any] = {}
    if tenant_id is not None:
        try:
            _ws = await get_workspace_settings(db, tenant_id=tenant_id)
        except Exception:  # noqa: BLE001 — missing/old workspace_settings → no seed
            _ws = {}
    if execution_mode is None and _ws.get("execution_mode_default"):
        execution_mode = _ws["execution_mode_default"]
    pid = _new_id()
    mode = normalize_execution_mode(execution_mode)
    await db.execute(
        "INSERT INTO projects (id, name, creator_human_id, execution_mode) "
        "VALUES (?, ?, ?, ?)",
        (pid, name, human_id, mode),
    )
    await db.commit()
    # Seed the remaining cascade defaults onto the fresh project row.
    _ci_default = _ws.get("code_intel_enabled_default")
    if _ci_default is not None:
        await update_project_settings(db, pid, code_intel_enabled=1 if _ci_default else 0)
    if _ws.get("hitl_auto_answer_default"):
        # Workspace default ON → seed the project to "safe" (mode 1) auto-answer.
        await update_project_settings(db, pid, hitl_auto_answer=1)
    project = await get_project(db, pid)
    assert project is not None
    return project


async def set_project_execution_mode(
    db: aiosqlite.Connection, project_id: str, mode: str
) -> dict[str, Any] | None:
    """Set the executor posture for a project. Invalid values normalise to
    'autonomous'. Returns the updated project dict, or None if not found.
    """
    normalized = normalize_execution_mode(mode)
    await db.execute(
        "UPDATE projects SET execution_mode = ? WHERE id = ?",
        (normalized, project_id),
    )
    await db.commit()
    return await get_project(db, project_id)


async def get_project(
    db: aiosqlite.Connection, project_id: str
) -> dict[str, Any] | None:
    """Look up a project by id."""
    async with db.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


_PROJECT_STATUSES = ("active", "parked", "archived")
_PROJECT_PRIORITIES = ("P0", "P1", "P2")


async def set_project_status(
    db: aiosqlite.Connection,
    project_id: str,
    status: str | None = None,
    priority: str | None = None,
) -> dict[str, Any] | None:
    """8db00fcb — update a project's organization fields (status/priority).
    Validates the enums; only the provided fields change."""
    sets: list[str] = []
    params: list[Any] = []
    if status is not None:
        if status not in _PROJECT_STATUSES:
            raise ValueError(
                f"invalid status {status!r}; expected one of {_PROJECT_STATUSES}"
            )
        sets.append("status = ?")
        params.append(status)
    if priority is not None:
        if priority not in _PROJECT_PRIORITIES:
            raise ValueError(
                f"invalid priority {priority!r}; expected one of {_PROJECT_PRIORITIES}"
            )
        sets.append("priority = ?")
        params.append(priority)
    if sets:
        params.append(project_id)
        await db.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", params
        )
        await db.commit()
    return await get_project(db, project_id)


async def get_agent_instructions(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the agent_instructions field for a project, or None if not set."""
    async with db.execute(
        "SELECT agent_instructions FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    val = row["agent_instructions"] if isinstance(row, dict) else row[0]
    return val or None


async def set_agent_instructions(
    db: aiosqlite.Connection, project_id: str, instructions: str | None
) -> dict[str, Any]:
    """Set agent_instructions for a project. Pass None to clear."""
    await db.execute(
        "UPDATE projects SET agent_instructions = ? WHERE id = ?",
        (instructions or None, project_id),
    )
    await db.commit()
    return {"project_id": project_id, "agent_instructions": instructions or None}


async def get_project_by_name(
    db: aiosqlite.Connection, name: str
) -> dict[str, Any] | None:
    """Look up a project by name using exact, then fuzzy case-insensitive match."""
    async with db.execute(
        "SELECT p.*, gs.goal_sprint AS sprint "
        "FROM projects p "
        "LEFT JOIN goal_states gs ON gs.project_id = p.id "
        "WHERE p.name = ? "
        "ORDER BY gs.version DESC "
        "LIMIT 1",
        (name,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        # Case-insensitive EXACT match fallback (not substring — avoids false 409s)
        async with db.execute(
            "SELECT p.*, gs.goal_sprint AS sprint "
            "FROM projects p "
            "LEFT JOIN goal_states gs ON gs.project_id = p.id "
            "WHERE LOWER(p.name) = ? "
            "ORDER BY p.created_at DESC, gs.version DESC "
            "LIMIT 1",
            (name.lower(),),
        ) as cur:
            row = await cur.fetchone()
    return _row_to_dict(row)


async def list_projects(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return every project row, newest first."""
    async with db.execute(
        "SELECT * FROM projects ORDER BY created_at DESC"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


async def list_project_summaries(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return project discovery summaries with sprint metadata, newest first."""
    async with db.execute(
        "SELECT p.id, p.name, gs.goal_sprint AS sprint, p.created_at "
        "FROM projects p "
        "LEFT JOIN goal_states gs "
        "ON gs.project_id = p.id "
        "AND gs.version = (SELECT MAX(version) FROM goal_states WHERE project_id = p.id) "
        "ORDER BY p.created_at DESC"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


async def rename_project(
    db: aiosqlite.Connection, project_id: str, new_name: str
) -> dict[str, Any] | None:
    """Rename a project. Returns the updated project dict or None if not found."""
    await db.execute(
        "UPDATE projects SET name = ? WHERE id = ?",
        (new_name, project_id),
    )
    await db.commit()
    return await get_project(db, project_id)


async def get_project_settings(
    db: aiosqlite.Connection, project_id: str
) -> dict[str, Any] | None:
    """Return the persisted settings for a project."""
    async with db.execute(
        "SELECT id, max_pinned_decisions, executor_config, hitl_auto_answer, "
        "auto_worktrees, require_merge_approval, code_intel_enabled, "
        "execution_mode "
        "FROM projects WHERE id = ?",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    data = _row_to_dict(row) or {}
    raw_executor_config = data.get("executor_config")
    try:
        executor_config = json.loads(raw_executor_config) if raw_executor_config else {}
    except (TypeError, ValueError):
        executor_config = {}
    return {
        "project_id": data["id"],
        "max_pinned_decisions": int(data.get("max_pinned_decisions") or 20),
        "executor_config": executor_config,
        # 035edf47 — 0=off, 1=safe, 2=aggressive (was a 0/1 bool).
        "hitl_auto_answer": int(data.get("hitl_auto_answer") or 0),
        # 0716c9e0 — parallel safety toggles; default ON (1).
        "auto_worktrees": int(data.get("auto_worktrees") if data.get("auto_worktrees") is not None else 1),
        "require_merge_approval": int(data.get("require_merge_approval") if data.get("require_merge_approval") is not None else 1),
        # Sprint-2/3 — codebase-memory-mcp toggle.
        "code_intel_enabled": int(data.get("code_intel_enabled") or 0),
        # ecf69de8 — executor posture: 'autonomous' (default) | 'interactive'.
        "execution_mode": normalize_execution_mode(data.get("execution_mode")),
    }


async def update_project_settings(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    max_pinned_decisions: int | None = None,
    executor_config: dict[str, Any] | None = None,
    hitl_auto_answer: int | None = None,
    auto_worktrees: int | None = None,
    require_merge_approval: int | None = None,
    code_intel_enabled: int | None = None,
    execution_mode: str | None = None,
    github_repo: str | None = _UNSET,
    github_branch: str | None = _UNSET,
    github_account_login: str | None = _UNSET,
) -> dict[str, Any] | None:
    """Persist project settings and return the updated values."""
    project = await get_project(db, project_id)
    if project is None:
        return None
    updates: list[str] = []
    params: list[Any] = []
    if max_pinned_decisions is not None:
        updates.append("max_pinned_decisions = ?")
        params.append(int(max_pinned_decisions))
    if executor_config is not None:
        updates.append("executor_config = ?")
        params.append(json.dumps(executor_config))
    if hitl_auto_answer is not None:
        # 035edf47 — 0=off, 1=safe, 2=aggressive. Clamp to the valid range.
        updates.append("hitl_auto_answer = ?")
        params.append(max(0, min(2, int(hitl_auto_answer))))
    if auto_worktrees is not None:
        updates.append("auto_worktrees = ?")
        params.append(1 if auto_worktrees else 0)
    if require_merge_approval is not None:
        updates.append("require_merge_approval = ?")
        params.append(1 if require_merge_approval else 0)
    if code_intel_enabled is not None:
        updates.append("code_intel_enabled = ?")
        params.append(1 if code_intel_enabled else 0)
    if execution_mode is not None:
        # ecf69de8 — normalise to a valid posture so a bad value never persists.
        updates.append("execution_mode = ?")
        params.append(normalize_execution_mode(execution_mode))
    if github_repo is not _UNSET:
        updates.append("github_repo = ?")
        params.append(github_repo or None)
    if github_branch is not _UNSET:
        updates.append("github_branch = ?")
        params.append(github_branch or None)
    if github_account_login is not _UNSET:
        updates.append("github_account_login = ?")
        params.append(github_account_login or None)
    if updates:
        params.append(project_id)
        await db.execute(
            f"UPDATE projects SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
        await db.commit()
    return await get_project_settings(db, project_id)


async def get_executor_config(
    db: aiosqlite.Connection, project_id: str
) -> dict[str, Any]:
    """Return the persisted executor_config payload for a project."""
    settings = await get_project_settings(db, project_id)
    if settings is None:
        raise ValueError(f"unknown project: {project_id}")
    cfg = settings.get("executor_config") or {}
    return cfg if isinstance(cfg, dict) else {}


# ---------------------------------------------------------------------------
# 0b061f45 — Multi-account GitHub OAuth
# ---------------------------------------------------------------------------

async def add_github_connection(
    db: aiosqlite.Connection,
    tenant_id: str,
    account_login: str,
    token: str,
    scope: str | None = None,
) -> dict[str, Any]:
    """Upsert a GitHub account connection for a tenant (encrypted token)."""
    conn_id = _new_id()
    await db.execute(
        "INSERT INTO github_connections (id, tenant_id, account_login, token, scope) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(tenant_id, account_login) DO UPDATE "
        "SET token = excluded.token, scope = excluded.scope",
        (conn_id, tenant_id, account_login, encrypt_field(token), scope),
    )
    await db.commit()
    return {"tenant_id": tenant_id, "account_login": account_login, "scope": scope}


async def get_github_connections(
    db: aiosqlite.Connection, tenant_id: str
) -> list[dict[str, Any]]:
    """List all connected GitHub accounts for a tenant (tokens are not returned)."""
    async with db.execute(
        "SELECT id, account_login, scope, connected_at FROM github_connections "
        "WHERE tenant_id = ? ORDER BY connected_at",
        (tenant_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


async def remove_github_connection(
    db: aiosqlite.Connection, tenant_id: str, account_login: str
) -> None:
    """Remove a GitHub account connection."""
    await db.execute(
        "DELETE FROM github_connections WHERE tenant_id = ? AND account_login = ?",
        (tenant_id, account_login),
    )
    await db.commit()


async def get_github_token_for_project(
    db: aiosqlite.Connection,
    tenant_id: str,
    project_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve the GitHub token for a project. Returns (token, account_login).

    Resolution order:
    1. Project's pinned github_account_login → that account's token.
    2. First connected account in github_connections (ordered by connected_at).
    3. Returns (None, None) — callers fall back to tenants.github_pat legacy.
    """
    if project_id:
        project = await get_project(db, project_id)
        pinned_login = (project or {}).get("github_account_login") or ""
        if pinned_login:
            async with db.execute(
                "SELECT token FROM github_connections "
                "WHERE tenant_id = ? AND account_login = ?",
                (tenant_id, pinned_login),
            ) as cur:
                row = await cur.fetchone()
            if row:
                r = _row_to_dict(row)
                return (decrypt_field(r.get("token")), pinned_login)

    async with db.execute(
        "SELECT account_login, token FROM github_connections "
        "WHERE tenant_id = ? ORDER BY connected_at LIMIT 1",
        (tenant_id,),
    ) as cur:
        row = await cur.fetchone()
    if row:
        r = _row_to_dict(row)
        return (decrypt_field(r.get("token")), r.get("account_login"))

    return (None, None)


async def set_executor_config(
    db: aiosqlite.Connection,
    project_id: str,
    executor_config: dict[str, Any],
) -> dict[str, Any]:
    """Persist and return the per-project executor configuration."""
    settings = await update_project_settings(
        db,
        project_id,
        executor_config=executor_config,
    )
    if settings is None:
        raise ValueError(f"unknown project: {project_id}")
    cfg = settings.get("executor_config") or {}
    return cfg if isinstance(cfg, dict) else {}


async def get_project_ntfy_url(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the ntfy URL for a project, or None if not set."""
    async with db.execute(
        "SELECT ntfy_url FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return row["ntfy_url"] or None


async def set_project_ntfy_url(
    db: aiosqlite.Connection, project_id: str, ntfy_url: str | None
) -> None:
    """Save (or clear) the ntfy URL for a project."""
    await db.execute(
        "UPDATE projects SET ntfy_url = ? WHERE id = ?",
        (ntfy_url or None, project_id),
    )
    await db.commit()


async def get_project_notify_email(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the notify_email for a project, or None if not set."""
    async with db.execute(
        "SELECT notify_email FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return row["notify_email"] or None


async def set_project_notify_email(
    db: aiosqlite.Connection, project_id: str, notify_email: str | None
) -> None:
    """Save (or clear) the notify_email for a project."""
    await db.execute(
        "UPDATE projects SET notify_email = ? WHERE id = ?",
        (notify_email or None, project_id),
    )
    await db.commit()


async def delete_project(db: aiosqlite.Connection, project_id: str) -> None:
    """Delete a project and all associated data.

    Raises ``ValueError`` if any tasks are currently ``in_progress`` so
    callers can surface a warning before proceeding.  The delete is
    unconditional for all other data (goal_states, sessions, task_log,
    sprint_items).
    """
    async with db.execute(
        "SELECT COUNT(*) as cnt FROM task_log "
        "WHERE project_id = ? AND status = 'in_progress'",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
        count = int(row["cnt"] if row else 0)
    if count:
        raise ValueError(f"{count} task(s) in_progress — complete or cancel first")

    # Cascade delete child rows first, then the project itself.
    # Order matters: hitl_requests FK -> sessions, so hitl first.
    for stmt, params in [
        ("DELETE FROM hitl_requests WHERE project_id = ?", (project_id,)),
        ("DELETE FROM sprint_items WHERE project_id = ?", (project_id,)),
        ("DELETE FROM task_log WHERE project_id = ?", (project_id,)),
        ("DELETE FROM sessions WHERE project_id = ?", (project_id,)),
        ("DELETE FROM sessions_archived WHERE project_id = ?", (project_id,)),
        ("DELETE FROM goal_states WHERE project_id = ?", (project_id,)),
        ("DELETE FROM decisions_pinned WHERE project_id = ?", (project_id,)),
        ("DELETE FROM project_notes WHERE project_id = ?", (project_id,)),
        ("DELETE FROM projects WHERE id = ?", (project_id,)),
    ]:
        try:
            await db.execute(stmt, params)
        except Exception:  # noqa: BLE001 — table may not exist in older schemas
            pass
    await db.commit()


def build_goal_xml(
    goal: dict[str, Any] | None,
    project_name: str,
    recent_tasks: list[dict[str, Any]] | None = None,
    coherence_warning: dict[str, Any] | None = None,
    decisions: str | None = None,
) -> str:
    """Serialise the goal + ambient context as XML for MCP consumers.

    Layout (v0.6.1):

        <goal version="N" project="NAME">
          <north_star cache="true">...</north_star>
          <version_goal cache="true">...</version_goal>
          <sprint cache="false">...</sprint>
          <recent_tasks cache="false">
            <task status="done" ts="...">...</task>
          </recent_tasks>
        </goal>

    ``cache="true"`` on fields that change rarely (north_star,
    version_goal) is a hint for v0.6.2's Anthropic prompt-cache
    plumbing — the field text doesn't drive any cache behaviour by
    itself but it makes the contract explicit in the wire format.
    Returns a valid XML document even when ``goal`` is None so cold
    sessions get a parseable response instead of a 404.
    """
    from xml.sax.saxutils import escape, quoteattr

    if goal is None:
        version = 0
        north_star = version_goal = sprint = ""
    else:
        version = int(goal.get("version") or 0)
        north_star = goal.get("north_star") or ""
        content = goal.get("content")
        if isinstance(content, str):
            version_goal = content
        elif content is None:
            version_goal = ""
        else:
            version_goal = json.dumps(content, indent=2)
        sprint = goal.get("sprint") or ""

    out: list[str] = []
    out.append(
        f'<goal version="{version}" project={quoteattr(project_name)}>'
    )
    out.append(f'  <north_star cache="true">{escape(north_star)}</north_star>')
    out.append(
        f'  <version_goal cache="true">{escape(version_goal)}</version_goal>'
    )
    out.append(f'  <sprint cache="false">{escape(sprint)}</sprint>')
    # v1.1.4 — append-only decisions log. Cached because it changes
    # rarely and only by explicit set_decision calls.
    if decisions is not None and decisions.strip():
        out.append(
            f'  <decisions cache="true">{escape(decisions)}</decisions>'
        )
    out.append('  <recent_tasks cache="false">')
    for t in recent_tasks or []:
        status = escape(str(t.get("status") or ""))
        ts = escape(str(t.get("created_at") or ""))
        desc = escape(str(t.get("description") or ""))
        out.append(
            f'    <task status="{status}" ts="{ts}">{desc}</task>'
        )
    out.append("  </recent_tasks>")
    # v1.1.3 — coherence_warning surfaced inline so cold sessions
    # immediately see which goal fields have gone stale.
    if coherence_warning is not None:
        level = escape(str(coherence_warning.get("level") or "ok"))
        msg = escape(str(coherence_warning.get("message") or ""))
        out.append(f'  <coherence_warning level="{level}">{msg}')
        stale = coherence_warning.get("stale_fields") or []
        for entry in stale:
            f = escape(str(entry.get("field") or ""))
            age = entry.get("age_seconds")
            age_str = f"{int(age)}" if isinstance(age, (int, float)) else ""
            out.append(
                f'    <stale field="{f}" age_seconds="{age_str}" />'
            )
        out.append('  </coherence_warning>')
    out.append("</goal>")
    return "\n".join(out)


def build_goal_cache_blocks(
    goal: dict[str, Any] | None,
    project_name: str,
    recent_tasks: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return goal text as Anthropic-API content blocks with cache hints.

    Layout (v0.6.2): four ordered text blocks ready to splat into
    ``messages[0].content`` or ``system`` on an Anthropic request.

    ``cache_control: {"type": "ephemeral"}`` is attached to the two
    blocks that change rarely:

      1. north_star  — cached
      2. version_goal — cached
      3. sprint      — no cache marker (moves every sprint review)
      4. recent_tasks — no cache marker (moves every task)

    Putting the cached blocks first matters: Anthropic's cache key is
    a prefix of the full prompt, so a hit requires the cached blocks
    to lead. Anything mutable that appears before a cached block
    invalidates the cache for every cold session.
    """
    if goal is None:
        north_star = version_goal = sprint = ""
        version = 0
    else:
        version = int(goal.get("version") or 0)
        north_star = goal.get("north_star") or ""
        content = goal.get("content")
        if isinstance(content, str):
            version_goal = content
        elif content is None:
            version_goal = ""
        else:
            version_goal = json.dumps(content, indent=2)
        sprint = goal.get("sprint") or ""

    header = (
        f"# Meridian goal — project: {project_name} (v{version})\n\n"
    )
    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"{header}## North star\n{north_star}".rstrip(),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": f"## Version goal\n{version_goal}".rstrip(),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": f"## Sprint\n{sprint}".rstrip(),
        },
    ]
    if recent_tasks:
        task_lines = ["## Recent tasks (newest first)"]
        for t in recent_tasks:
            status = (t.get("status") or "").upper()
            ts = t.get("created_at") or ""
            desc = (t.get("description") or "").replace("\n", " ")
            task_lines.append(f"- [{status}] {ts} — {desc}")
        blocks.append({"type": "text", "text": "\n".join(task_lines)})
    else:
        blocks.append(
            {"type": "text", "text": "## Recent tasks\n(no activity yet)"}
        )
    return blocks


async def get_goal(
    db: aiosqlite.Connection, project_id: str
) -> dict[str, Any] | None:
    """Return the latest goal state for a project, or None if unset.

    Since v0.5.2 the returned dict also includes ``north_star`` and
    ``sprint`` pulled from the ``goal_north_star`` / ``goal_sprint``
    columns. Both are None when not yet set.
    """
    async with db.execute(
        "SELECT * FROM goal_states WHERE project_id = ? "
        "ORDER BY version DESC LIMIT 1",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    goal = _row_to_dict(row)
    if goal is None:
        return None
    goal["content"] = _decode_content(goal["content"])
    goal["north_star"] = goal.pop("goal_north_star", None)
    goal["sprint"] = goal.pop("goal_sprint", None)
    return goal


async def set_goal(
    db: aiosqlite.Connection,
    project_id: str,
    content: Any,
    north_star: str | None = None,
    sprint: str | None = None,
    minor: bool = False,
) -> dict[str, Any]:
    """Upsert the goal state for a project.

    ``north_star`` and ``sprint`` are optional. When omitted, the values
    from the previous goal row are carried forward (backward compat). Pass
    an explicit value to change them. Since v0.5.2.

    When ``minor=True``, the latest row is updated in-place without
    incrementing the version counter. Use this for AUTO BLOCKS appends
    that should not pollute the goal history.
    """
    existing = await get_goal(db, project_id)
    encoded = _encode_content(content)
    # Carry forward north_star / sprint from the previous row when not given.
    final_north_star = north_star if north_star is not None else (
        existing.get("north_star") if existing else None
    )
    final_sprint = sprint if sprint is not None else (
        existing.get("sprint") if existing else None
    )
    # v2.3 — dedup: if the main content didn't change, treat this as a
    # minor in-place update on the existing row. Prevents the version
    # counter from spamming when only sprint / north_star changes (every
    # save would otherwise insert a new row + bump version).
    if not minor and existing is not None:
        existing_content = existing.get("content")
        existing_encoded = (
            existing_content if isinstance(existing_content, str)
            else _encode_content(existing_content)
        )
        if encoded == existing_encoded:
            minor = True
        else:
            # If ONLY the AUTO BLOCKS section changed (human prefix identical),
            # treat as minor — auto-summary updates must never bump version.
            _AUTO_SPLIT = "--- AUTO BLOCKS BELOW ---"
            def _strip_auto(s: Any) -> str:
                t = s if isinstance(s, str) else (s.get("content", "") if isinstance(s, dict) else str(s or ""))
                return t.split(_AUTO_SPLIT)[0].rstrip() if _AUTO_SPLIT in t else t.rstrip()
            if _strip_auto(encoded) == _strip_auto(existing_encoded):
                minor = True
    if minor and existing is not None:
        # In-place update — no version bump, no new row.
        # Strict AUTO BLOCKS replace: strip ALL occurrences from both sides,
        # take the new AUTO BLOCKS section from incoming only, then reconstruct.
        from datetime import datetime, timezone
        AUTO_SPLIT = "--- AUTO BLOCKS BELOW ---"

        def _to_plain(v: Any) -> str:
            """Decode goal content to a plain string for marker surgery."""
            if isinstance(v, str):
                return v
            if isinstance(v, dict):
                return v.get("content", "") or str(v)
            decoded = _decode_content(v)
            if isinstance(decoded, dict):
                return decoded.get("content", "") or ""
            return str(decoded) if decoded else ""

        existing_str = _to_plain(existing.get("content") or "")
        incoming_str = _to_plain(encoded)

        # Strip ALL occurrences of AUTO_SPLIT-and-below from existing → base.
        base = existing_str.split(AUTO_SPLIT)[0].rstrip()

        # Extract new AUTO BLOCKS from incoming (split on first occurrence).
        incoming_parts = incoming_str.split(AUTO_SPLIT, 1)
        if len(incoming_parts) > 1:
            new_auto = AUTO_SPLIT + incoming_parts[1]
            # v2.3 — use `\n\n` so the resulting layout matches
            # _AUTO_SECTION_MARKER ("\n\n--- AUTO BLOCKS BELOW ---\n").
            # Single `\n` here would make append_auto_summary's marker
            # check miss on the next cycle, producing duplicate auto
            # sections instead of replacing the old one.
            final_content = base + "\n\n" + new_auto
        else:
            # No AUTO BLOCKS in incoming — use incoming content as-is (minor
            # update that happens to contain no auto section is fine).
            final_content = incoming_str

        final_encoded = _encode_content(final_content)
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        # v2.3 — per-field timestamps: bump only the columns whose value
        # actually changed. Lets get_goal_field_ages report accurate
        # per-field freshness even when in-place UPDATEs replace what
        # used to be multi-row history.
        existing_content_str = existing.get("content")
        if not isinstance(existing_content_str, str):
            existing_content_str = _encode_content(existing_content_str)
        ns_ts = now_ts if final_north_star != existing.get("north_star") else (
            existing.get("ns_updated_at") or existing.get("updated_at")
        )
        content_ts = now_ts if final_encoded != existing_content_str else (
            existing.get("content_updated_at") or existing.get("updated_at")
        )
        sprint_ts = now_ts if final_sprint != existing.get("sprint") else (
            existing.get("sprint_updated_at") or existing.get("updated_at")
        )
        await db.execute(
            "UPDATE goal_states SET content = ?, goal_north_star = ?, goal_sprint = ?, "
            "updated_at = ?, ns_updated_at = ?, content_updated_at = ?, sprint_updated_at = ? "
            "WHERE id = ?",
            (final_encoded, final_north_star, final_sprint, now_ts,
             ns_ts, content_ts, sprint_ts, existing["id"]),
        )
        await db.commit()
    else:
        from datetime import datetime, timezone
        new_version = 1 if existing is None else int(existing["version"]) + 1
        gid = _new_id()
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        # v2.3 — per-field timestamps. For a brand-new row, every field
        # that has a value is timestamped now; null fields stay null. For
        # a fresh INSERT-style write where content changed, content_ts is
        # always now (content is non-null by schema).
        if existing is not None:
            existing_content_str = existing.get("content")
            if not isinstance(existing_content_str, str):
                existing_content_str = _encode_content(existing_content_str)
            ns_ts = now_ts if final_north_star != existing.get("north_star") else (
                existing.get("ns_updated_at") or existing.get("updated_at")
            )
            content_ts = now_ts if encoded != existing_content_str else (
                existing.get("content_updated_at") or existing.get("updated_at")
            )
            sprint_ts = now_ts if final_sprint != existing.get("sprint") else (
                existing.get("sprint_updated_at") or existing.get("updated_at")
            )
        else:
            ns_ts = now_ts if final_north_star else None
            content_ts = now_ts
            sprint_ts = now_ts if final_sprint else None
        await db.execute(
            "INSERT INTO goal_states "
            "(id, project_id, content, version, goal_north_star, goal_sprint, "
            "ns_updated_at, content_updated_at, sprint_updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (gid, project_id, encoded, new_version, final_north_star, final_sprint,
             ns_ts, content_ts, sprint_ts),
        )
        await db.commit()
    goal = await get_goal(db, project_id)
    assert goal is not None
    # Broadcast to dashboard WebSocket subscribers so the goal panel refreshes live.
    _publish_project_event(project_id, "goal_updated", {"version": goal.get("version")})
    return goal


async def set_north_star(
    db: aiosqlite.Connection, project_id: str, north_star: str
) -> dict[str, Any]:
    """Update only the north_star field, preserving current content and sprint.

    Creates a new goal row (increments version). 404-equivalent: raises
    ValueError if no goal exists yet — set the version goal first.
    """
    existing = await get_goal(db, project_id)
    if existing is None:
        raise ValueError("no goal set — call set_goal before set_north_star")
    return await set_goal(
        db, project_id, existing["content"],
        north_star=north_star, sprint=existing.get("sprint")
    )


async def set_sprint(
    db: aiosqlite.Connection, project_id: str, sprint: str
) -> dict[str, Any]:
    """Update only the sprint field, preserving current content and north_star.

    Any team member can call this (no ownership check at the db layer).
    Creates a new goal row (increments version).
    """
    existing = await get_goal(db, project_id)
    if existing is None:
        raise ValueError("no goal set — call set_goal before set_sprint")
    return await set_goal(
        db, project_id, existing["content"],
        north_star=existing.get("north_star"), sprint=sprint
    )


async def register_session(
    db: aiosqlite.Connection,
    project_id: str,
    name: str,
    human_id: str | None = None,
    session_type: str = "human",
    agent_framework: str = "claude_code",
    client_type: str | None = None,
    sprint_version: str | None = None,
) -> dict[str, Any]:
    """Create a session row in 'active' state.

    ``human_id`` lets a session attach a human owner identifier so the
    dashboard can group ``adam/claude-sonnet-xyz`` sessions together and
    so the goal-ownership rule can match a writer to the project creator.

    ``session_type`` (v1.2.0) is ``human`` by default and ``worker`` for
    sessions started via :func:`start_worker_session`. The timeline /
    auto-summary loops use it to distinguish the two kinds.

    ``agent_framework`` (v2.4) labels the originating framework so the
    Team tab can render badges. One of: claude_code (default), cursor,
    windsurf, langgraph, autogen, openviking, custom. Free-form string —
    unknown values render as 'custom' in the UI.

    ``client_type`` (v2.6) identifies the client app: claude-code, claude-desktop,
    cursor, or other. Optional — used for presence indicators in the dashboard.

    ``sprint_version`` (a76cb7c0) scopes the session to one sprint-version
    bucket. start_session passes the explicit or inferred version so later calls
    can auto-filter sprint progress/items to it. NULL = unscoped (all versions),
    the legacy behaviour.
    """
    if session_type not in {"human", "worker"}:
        raise ValueError(f"invalid session_type: {session_type!r}")
    sid = _new_id()
    await db.execute(
        "INSERT INTO sessions (id, project_id, name, human_id, session_type, agent_framework, client_type, sprint_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, project_id, name, human_id, session_type, agent_framework, client_type, sprint_version),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM sessions WHERE id = ?", (sid,)
    ) as cur:
        row = await cur.fetchone()
    session = _row_to_dict(row)
    assert session is not None
    # Broadcast to dashboard WebSocket subscribers so the session list refreshes live.
    _publish_project_event(project_id, "session_started", {
        "session_id": sid, "session_name": name, "human_id": human_id,
    })
    return session


async def generate_default_session_name(
    db: aiosqlite.Connection,
    project_id: str,
) -> str:
    """599d0097 — derive a meaningful session name when start_session gets none.

    Uses the first pending/todo sprint item's title (slugified to its first few
    words) plus a short UTC timestamp, so an unnamed session reads as e.g.
    ``wire-billing-oauth-0701-2130`` instead of forcing the caller to invent a
    string. 2bce89ed — when the board is empty, falls back to a memorable
    adjective+noun+timestamp slug (e.g. ``brisk-otter-0701-213045``) instead of
    the anonymous ``session-<ts>``. The timestamp keeps the name unique (the
    board rejects duplicate active names); year is dropped for brevity but
    seconds are kept so two sessions in the same minute don't collide.
    """
    from datetime import datetime, timezone  # local: db/__init__ has no top import
    ts = datetime.now(timezone.utc).strftime("%m%d-%H%M%S")
    try:
        items = await get_sprint_items(db, project_id, include_human=False)
    except Exception:  # noqa: BLE001 — naming must never block start_session
        items = []
    first = next(
        (it for it in items
         if (it.get("status") or "pending") in {"pending", "todo"}),
        None,
    )
    title = (first or {}).get("title") or ""
    words = re.findall(r"[a-z0-9]+", title.lower())
    if words:
        slug = "-".join(words[:5])[:48].strip("-") or "session"
        return f"{slug}-{ts}"
    # 2bce89ed — memorable adjective+noun for an empty board, chosen
    # deterministically from the timestamp (readable + unique via the ts suffix).
    _adj = ("brisk", "calm", "clever", "bold", "quiet", "swift", "warm", "keen",
            "bright", "steady", "nimble", "lucid")
    _noun = ("otter", "harbor", "cedar", "falcon", "meadow", "ember", "delta",
             "willow", "quartz", "sparrow", "atlas", "cove")
    _h = sum(ord(c) for c in ts)
    return f"{_adj[_h % len(_adj)]}-{_noun[(_h // len(_adj)) % len(_noun)]}-{ts}"


def build_worker_context_xml(
    *,
    version_goal: str,
    task_id: str,
    task_description: str,
    repo: str,
    test_cmd: str = "pixi run test",
    commit_pattern: str = (
        "Use commit.py pattern: write commit message to tmp/commit.py, "
        "run via pixi run python tmp/commit.py, then delete tmp/commit.py "
        "in the same command. GOAL.md is git-tracked — include it in the "
        "staged files (git add GOAL.md) when it has been modified."
    ),
    done_when: str = (
        "log_task done, tests green, committed (no stray .py at repo root)."
    ),
) -> str:
    """v1.2.0 — slim XML for worker sessions.

    Workers don't need north_star, decisions, or sprint history —
    they need the version goal + the one task they're claiming, plus
    the operational machinery (repo path, test cmd, commit pattern,
    completion criteria). The resulting block is intentionally short
    (under ~500 tokens) so it costs nothing to splat into a
    Claude Code worker's first turn.
    """
    from xml.sax.saxutils import escape, quoteattr

    return "\n".join([
        "<worker_context>",
        f"  <version_goal>{escape(version_goal)}</version_goal>",
        f"  <task id={quoteattr(task_id)}>{escape(task_description)}</task>",
        f"  <repo>{escape(repo)}</repo>",
        f"  <test_cmd>{escape(test_cmd)}</test_cmd>",
        f"  <commit_pattern>{escape(commit_pattern)}</commit_pattern>",
        f"  <done_when>{escape(done_when)}</done_when>",
        "</worker_context>",
    ])


async def start_worker_session(
    db: aiosqlite.Connection,
    project_id: str,
    task_id: str | None = None,
    repo: str | None = None,
    test_cmd: str | None = None,
) -> dict[str, Any]:
    """v1.2.0 — register a worker session + claim its task in one call.

    When ``task_id`` is None we pick the oldest unclaimed pending task
    for this project. The returned dict carries the new worker session
    id, the claimed task row, and the ``worker_context`` XML the
    worker should splat into its first prompt.

    Raises ``ValueError`` when no claimable task is available (and
    none was provided explicitly).
    """
    if task_id is None:
        claimable = await get_claimable_tasks(db, project_id, limit=1)
        if not claimable:
            raise ValueError("no claimable tasks available for this project")
        task = claimable[0]
    else:
        task = await get_task(db, task_id)
        if task is None or task["project_id"] != project_id:
            raise ValueError(f"task not found: {task_id}")

    short = task["id"][:8]
    session = await register_session(
        db, project_id, f"worker/{short}",
        human_id=None, session_type="worker",
    )

    claimed = await claim_task(db, task["id"], session["id"])
    if claimed is None:
        # Another worker beat us to it. Mark this session closed so
        # the timeline doesn't show a zombie row, and surface the
        # contention to the caller.
        await close_session(db, session["id"])
        raise ValueError(
            f"task {task['id']} already claimed by another worker"
        )

    goal = await get_goal(db, project_id)
    version_goal_text = ""
    if goal is not None:
        content = goal.get("content")
        if isinstance(content, str):
            version_goal_text = content
        elif content is not None:
            version_goal_text = str(content)

    xml = build_worker_context_xml(
        version_goal=version_goal_text,
        task_id=claimed["id"],
        task_description=claimed["description"] or "",
        repo=repo or os.environ.get(
            "MERIDIAN_WORKER_REPO",
            str(Path(__file__).resolve().parent.parent),
        ),
        test_cmd=test_cmd or os.environ.get(
            "MERIDIAN_WORKER_TEST_CMD", "pixi run test"
        ),
    )
    return {
        "session_id": session["id"],
        "task": claimed,
        "worker_context": xml,
    }


async def set_goal_mode(
    db: aiosqlite.Connection, project_id: str, mode: str
) -> None:
    """Switch a project between 'manual' and 'auto' goal modes (v0.4.2).

    Auto mode lets a background task append [AUTO SUMMARY] blocks to
    the goal every ten minutes so cold sessions read recent activity
    inline with the human directive.
    """
    if mode not in {"manual", "auto"}:
        raise ValueError(f"invalid goal mode: {mode!r}")
    await db.execute(
        "UPDATE projects SET goal_mode = ? WHERE id = ?", (mode, project_id)
    )
    await db.commit()


async def get_goal_mode(
    db: aiosqlite.Connection, project_id: str
) -> str:
    """Return 'manual' or 'auto' for a project (defaults to 'manual')."""
    async with db.execute(
        "SELECT goal_mode FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None or row["goal_mode"] is None:
        return "manual"
    return row["goal_mode"]


async def list_auto_mode_projects(
    db: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    """Every project currently in auto-summary mode."""
    async with db.execute(
        "SELECT * FROM projects WHERE goal_mode = 'auto'"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


def format_auto_summary_block(
    tasks: list[dict[str, Any]], timestamp: str | None = None
) -> str:
    """Render a ``[AUTO SUMMARY - <ts>]`` block from recent tasks.

    Pure function so the periodic worker is trivial to unit-test. The
    summary is plain text: one line per task with status + description.
    """
    if timestamp is None:
        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if not tasks:
        return f"[AUTO SUMMARY - {timestamp}]\n(no recent activity)"
    lines = [f"[AUTO SUMMARY - {timestamp}]"]
    for t in tasks:
        status = t.get("status", "?")
        desc = (t.get("description") or "").strip().splitlines()[0][:200]
        lines.append(f"- [{status.upper()}] {desc}")
    return "\n".join(lines)


# Anchor that separates the human-written goal text from auto-appended
# blocks. Anything BELOW this marker may be rewritten by the periodic
# task; anything above is sacred.
_AUTO_SECTION_MARKER = "\n\n--- AUTO BLOCKS BELOW ---\n"


async def run_auto_summary_cycle(
    db: aiosqlite.Connection, task_limit: int = 10
) -> int:
    """Run one pass of the v0.4.2 auto-summary loop.

    For every project in ``auto`` mode: take the last ``task_limit``
    tasks, render an [AUTO SUMMARY] block, and append it to the goal.
    Returns the number of projects updated. Exposed as a standalone
    function so the background task is trivial *and* unit-testable.
    """
    updated = 0
    projects = await list_auto_mode_projects(db)
    for project in projects:
        tasks = await get_tasks(db, project["id"], limit=task_limit)
        block = format_auto_summary_block(tasks)
        result = await append_auto_summary(db, project["id"], block)
        if result is not None:
            updated += 1
    return updated


async def append_auto_summary(
    db: aiosqlite.Connection,
    project_id: str,
    summary_block: str,
) -> dict[str, Any] | None:
    """Append a fresh ``[AUTO SUMMARY ...]`` block to the project goal.

    Strategy: preserve the human-written prefix above
    ``--- AUTO BLOCKS BELOW ---`` exactly, then replace the auto
    section with just the new block (single, freshest summary — old
    blocks are discarded to keep the goal compact). Returns the new
    goal row, or None when there's no goal yet.
    """
    existing = await get_goal(db, project_id)
    if existing is None:
        return None
    content = existing["content"]
    if not isinstance(content, str):
        # JSON-typed goals are out of scope for auto-append; bail safely.
        return existing
    if _AUTO_SECTION_MARKER in content:
        prefix = content.split(_AUTO_SECTION_MARKER, 1)[0]
    else:
        prefix = content
    new_content = prefix.rstrip() + _AUTO_SECTION_MARKER + summary_block
    # v2.3 — auto-summary must NOT bump the version goal. minor=True does
    # an in-place UPDATE of the latest row, preserving the human prefix
    # above AUTO BLOCKS exactly and only swapping the auto section. The
    # main "version goal" content stays under explicit save-goal control.
    return await set_goal(db, project_id, new_content, minor=True)


async def get_decisions(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """v1.1.4 — return the project's append-only decisions log,
    newest first. ``None`` when no decisions have been recorded."""
    async with db.execute(
        "SELECT decisions FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return row["decisions"]


async def set_decision(
    db: aiosqlite.Connection,
    project_id: str,
    text: str,
    timestamp: str | None = None,
) -> str:
    """Prepend a decision entry to the project's decisions log.

    Format per entry: ``[YYYY-MM-DD] <text>\\n\\n``. The entry is
    prepended so the latest decision sits at the top of the log —
    cold sessions read it first. Returns the full updated log.
    """
    from datetime import datetime, timezone
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = f"[{ts}] {text.strip()}\n"
    async with db.execute(
        "SELECT decisions FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise ValueError(f"unknown project: {project_id}")
    existing = (row["decisions"] or "").rstrip()
    updated = (entry + ("\n" + existing if existing else "")).rstrip() + "\n"
    await db.execute(
        "UPDATE projects SET decisions = ? WHERE id = ?",
        (updated, project_id),
    )
    await db.commit()
    return updated


async def get_project_owner(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the ``creator_human_id`` for a project, or None if unset.

    Used by the ``POST /projects/{id}/goal`` endpoint to enforce the
    "only the project owner can set goal" contract introduced in v0.3.2.
    """
    async with db.execute(
        "SELECT creator_human_id FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return row["creator_human_id"]


async def update_session_seen(
    db: aiosqlite.Connection, session_id: str
) -> None:
    """Bump a session's last_seen timestamp to now."""
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now') WHERE id = ?",
        (session_id,),
    )
    await db.commit()


async def count_active_sessions(db: aiosqlite.Connection) -> int:
    """Count sessions currently live (status active or idle, not closed/expired).

    Read-only; used by the public ``/status/sessions`` shields badge. Mirrors the
    'live' definition used by the idle-expiry sweep (status IN active/idle).
    """
    async with db.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE status IN ('active', 'idle')"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return 0
    return int(row["n"] if isinstance(row, dict) else row[0])


async def heartbeat_session(
    db: aiosqlite.Connection, session_id: str
) -> bool:
    """Touch ``last_seen`` so the idle-expiry sweep leaves this session
    alone. Returns True when the session exists; False otherwise so the
    HTTP layer can 404 cleanly. Used by long-running workers that don't
    call ``log_task`` often enough to keep the 30 minute TTL fresh.

    Optimization: if ``last_seen`` is within 5 minutes (e.g. because a
    tool call already updated it), skip the write — the session is fresh."""
    from datetime import datetime, timezone
    async with db.execute(
        "SELECT id, last_seen FROM sessions WHERE id = ? AND status != 'closed'",
        (session_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return False
    last_seen = (row["last_seen"] if isinstance(row, dict) else row[1]) or ""
    try:
        ls_dt = datetime.fromisoformat(last_seen.replace(" ", "T")).replace(tzinfo=timezone.utc)
        age_s = (datetime.now(timezone.utc) - ls_dt).total_seconds()
        if age_s < 300:
            return True  # fresh from a recent tool call — no-op
    except (ValueError, AttributeError):
        pass
    cursor = await db.execute(
        "UPDATE sessions SET last_seen = datetime('now') "
        "WHERE id = ? AND status != 'closed'",
        (session_id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def keepalive_sessions(
    db: aiosqlite.Connection, session_ids: "list[str]"
) -> int:
    """Bump ``last_seen`` to now for each still-open session in *session_ids*.

    Driven by the server's keepalive loop so a session busy on non-MCP work
    (git/bash/file ops) — and therefore making no tool calls — doesn't drift
    past the live window and get mistaken for dead by a coordinating session.
    Closed/archived rows are skipped. Returns the number of rows refreshed."""
    if not session_ids:
        return 0
    placeholders = ", ".join("?" for _ in session_ids)
    cursor = await db.execute(
        f"UPDATE sessions SET last_seen = datetime('now') "
        f"WHERE id IN ({placeholders}) AND status NOT IN ('closed', 'archived')",
        tuple(session_ids),
    )
    await db.commit()
    return cursor.rowcount


# bc9259b8 — worker stall auto-retry budget. A sprint item left in_progress by a
# closing/stale worker is re-queued to pending while its stall_count is within
# this budget; once it would exceed the budget it is marked failed silently (no
# HITL, no human ping) so the orchestrator just moves on.
_MAX_SPRINT_STALL_RETRIES = 2


async def _session_stall_summary(
    db: aiosqlite.Connection, session_id: str, *, limit: int = 5
) -> str:
    """Build a compact 'last session log' string for a stalled worker session.

    Joins the session's most recent task_log descriptions so the failure note on
    a permanently-stalled item captures what the worker was doing. Best-effort:
    returns '(no session log)' when the session logged nothing.
    """
    async with db.execute(
        "SELECT description FROM task_log WHERE session_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (session_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    descs = [str((_row_to_dict(r) or {}).get("description") or "").strip() for r in rows]
    descs = [d for d in descs if d]
    if not descs:
        return "(no session log)"
    return " | ".join(descs)


async def _stalled_item_ids_for_session(
    db: aiosqlite.Connection, session_id: str
) -> list[str]:
    """Return distinct sprint-item ids this session was working on.

    A worker links to an item via a registered worktree (active_worktrees.item_id)
    or via task_log rows tagged with sprint_item_id. The union covers both the
    worktree-isolated and single-tree worker styles.
    """
    ids: list[str] = []
    seen: set[str] = set()
    async with db.execute(
        "SELECT item_id FROM active_worktrees "
        "WHERE session_id = ? AND item_id IS NOT NULL AND removed_at IS NULL",
        (session_id,),
    ) as cur:
        for r in await cur.fetchall():
            iid = (_row_to_dict(r) or {}).get("item_id")
            if iid and iid not in seen:
                seen.add(iid)
                ids.append(iid)
    async with db.execute(
        "SELECT DISTINCT sprint_item_id FROM task_log "
        "WHERE session_id = ? AND sprint_item_id IS NOT NULL",
        (session_id,),
    ) as cur:
        for r in await cur.fetchall():
            iid = (_row_to_dict(r) or {}).get("sprint_item_id")
            if iid and iid not in seen:
                seen.add(iid)
                ids.append(iid)
    return ids


async def requeue_or_fail_stalled_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    *,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    """bc9259b8 — handle one stalled sprint item: re-queue, or fail after the budget.

    Increments ``stall_count``. While the new count is within
    :data:`_MAX_SPRINT_STALL_RETRIES`, the item is re-queued to ``pending``
    (claim cleared) so another worker can pick it up. Once the new count exceeds
    the budget the item is marked ``failed`` with the stalling session's last log
    appended to its notes — silently, with no HITL. No-op (returns None) when the
    item is missing, in another project, or not currently ``in_progress``.
    """
    item = await get_sprint_item(db, item_id)
    if item is None or item.get("project_id") != project_id:
        return None
    if (item.get("status") or "pending") != "in_progress":
        return None  # completed/failed/already re-queued — not a stall
    new_count = int(item.get("stall_count") or 0) + 1
    if new_count > _MAX_SPRINT_STALL_RETRIES:
        last_log = (
            await _session_stall_summary(db, session_id) if session_id else "(unknown session)"
        )
        reason = (
            f"Auto-failed after {new_count - 1} stall retr"
            f"{'y' if new_count - 1 == 1 else 'ies'} "
            f"(worker closed without completing). Last session log: {last_log}"
        )
        await db.execute(
            "UPDATE sprint_items SET status = 'failed', stall_count = ?, "
            "claimed_at = NULL, notes = ? WHERE id = ? AND project_id = ?",
            (new_count, reason, item_id, project_id),
        )
        await db.commit()
        _invalidate_sprint_items_cache(project_id)
        _publish_project_event(
            project_id, "sprint_item_updated", {"item_id": item_id, "status": "failed"}
        )
        updated = await get_sprint_item(db, item_id)
        return {"action": "failed", "item": updated, "stall_count": new_count}
    await db.execute(
        "UPDATE sprint_items SET status = 'pending', stall_count = ?, "
        "claimed_at = NULL, completed_at = NULL WHERE id = ? AND project_id = ?",
        (new_count, item_id, project_id),
    )
    await db.commit()
    _invalidate_sprint_items_cache(project_id)
    _publish_project_event(
        project_id, "sprint_item_updated", {"item_id": item_id, "status": "pending"}
    )
    updated = await get_sprint_item(db, item_id)
    return {"action": "requeued", "item": updated, "stall_count": new_count}


async def handle_session_stall(
    db: aiosqlite.Connection, session_id: str
) -> dict[str, Any]:
    """bc9259b8 — re-queue or fail any sprint items a closing worker left in_progress.

    Finds every sprint item this session was working on (worktree or task link)
    that is still ``in_progress`` and routes it through
    :func:`requeue_or_fail_stalled_item`. Returns ``{"requeued": [ids], "failed":
    [ids]}``. Safe no-op when the session completed its work (items already done).
    """
    async with db.execute(
        "SELECT project_id FROM sessions WHERE id = ?", (session_id,)
    ) as cur:
        srow = await cur.fetchone()
    sess = _row_to_dict(srow)
    requeued: list[str] = []
    failed: list[str] = []
    if not sess or not sess.get("project_id"):
        return {"requeued": requeued, "failed": failed}
    project_id = sess["project_id"]
    for item_id in await _stalled_item_ids_for_session(db, session_id):
        result = await requeue_or_fail_stalled_item(
            db, project_id, item_id, session_id=session_id
        )
        if result is None:
            continue
        if result["action"] == "failed":
            failed.append(item_id)
        else:
            requeued.append(item_id)
    return {"requeued": requeued, "failed": failed}


async def close_session(db: aiosqlite.Connection, session_id: str) -> None:
    """Mark a session as closed and finalize its executor_run.

    bc9259b8 — before tearing down, any sprint item this worker left in_progress
    (closed without complete_sprint_item) is re-queued to pending, or failed
    silently once it exhausts its stall-retry budget.
    """
    await db.execute(
        "UPDATE sessions SET status = 'closed' WHERE id = ?",
        (session_id,),
    )
    await release_file_locks_for_session(db, session_id)
    await release_resource_locks_for_session(db, session_id)
    await release_symbol_claims_for_session(db, session_id)
    await db.commit()
    try:
        await handle_session_stall(db, session_id)
    except Exception:  # noqa: BLE001 — stall recovery must never block session close
        pass
    try:
        await finalize_executor_run(db, session_id, status="done")
    except Exception:
        pass


async def archive_stale_sessions(
    db: aiosqlite.Connection, project_id: str, days: int = 7
) -> int:
    """Move sessions unseen for *days* days to 'archived'.

    Only 'active' and 'idle' sessions are touched — 'closed' and already-
    'archived' rows are left unchanged.  Returns the count updated.

    Any in_progress tasks held by the archived sessions are released back
    to 'pending' so they can be picked up by the next worker session.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    async with db.execute(
        "SELECT id, sprint_item_id FROM task_log "
        "WHERE claimed_by IN ("
        "  SELECT id FROM sessions "
        "  WHERE project_id = ? AND status NOT IN ('closed', 'archived') AND last_seen < ?"
        ") AND status = 'in_progress'",
        (project_id, cutoff),
    ) as cur:
        stale_rows = await cur.fetchall()
    stale_task_ids = [row["id"] for row in stale_rows]
    linked_item_ids = [
        row["sprint_item_id"] for row in stale_rows if row["sprint_item_id"]
    ]
    if stale_task_ids:
        placeholders = ", ".join("?" for _ in stale_task_ids)
        await db.execute(
            f"UPDATE task_log SET status = 'pending', claimed_by = NULL, claimed_at = NULL "
            f"WHERE id IN ({placeholders})",
            tuple(stale_task_ids),
        )
    if linked_item_ids:
        placeholders = ", ".join("?" for _ in linked_item_ids)
        await db.execute(
            f"UPDATE sprint_items SET status = 'pending', completed_at = NULL "
            f"WHERE id IN ({placeholders}) AND status = 'in_progress'",
            tuple(linked_item_ids),
        )
    cursor = await db.execute(
        "UPDATE sessions SET status = 'archived' "
        "WHERE project_id = ? "
        "AND status NOT IN ('closed', 'archived') "
        "AND last_seen < ?",
        (project_id, cutoff),
    )
    await db.commit()
    return cursor.rowcount


async def archive_empty_sessions(
    db: aiosqlite.Connection, days: int = 7
) -> int:
    """Archive sessions older than *days* that never logged any tasks."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    cursor = await db.execute(
        "UPDATE sessions SET status = 'archived' "
        "WHERE created_at < ? "
        "AND status != 'active' "
        "AND id NOT IN (SELECT DISTINCT session_id FROM task_log)",
        (cutoff,),
    )
    await db.commit()
    return cursor.rowcount


async def get_sessions(
    db: aiosqlite.Connection,
    project_id: str,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    """List sessions for a project, newest first."""
    if active_only:
        query = (
            "SELECT * FROM sessions WHERE project_id = ? "
            "AND status NOT IN ('closed', 'archived') ORDER BY last_seen DESC"
        )
    else:
        query = (
            "SELECT * FROM sessions WHERE project_id = ? "
            "ORDER BY last_seen DESC"
        )
    async with db.execute(query, (project_id,)) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


async def log_task(
    db: aiosqlite.Connection,
    session_id: str,
    project_id: str,
    description: str,
    status: str = "done",
    parent_session_id: str | None = None,
    parent_task_id: str | None = None,
    sprint_item_id: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """Append a task-log entry and broadcast to live subscribers.

    ``parent_session_id`` (v1.2.1) records that this task was kicked
    off by another session — typically when an enqueue_claude_task
    worker logs its result. The timeline + auto-summary use it to
    correlate parent / child sessions.

    ``parent_task_id`` (v2.4) records that this task is a sub-step of
    another task. Lets the dashboard render multi-agent work as a tree
    (researcher → fetched 3 sources, writer → drafted reply, etc.).

    ``kind`` (Sprint-4) is the entry taxonomy: shipped/found/decided/blocked.
    Defaults to 'shipped'. Unknown values are coerced to NULL.
    """
    if status not in {"pending", "in_progress", "done", "failed", "pending-hitl", "backlog", "future", "backburner"}:
        raise ValueError(f"invalid task status: {status}")
    if kind not in ("shipped", "found", "decided", "blocked"):
        kind = None
    tid = _new_id()
    await db.execute(
        "INSERT INTO task_log "
        "(id, session_id, project_id, description, status, parent_session_id, parent_task_id, sprint_item_id, kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tid,
            session_id,
            project_id,
            description,
            status,
            parent_session_id,
            parent_task_id,
            sprint_item_id,
            kind,
        ),
    )
    await update_session_seen(db, session_id)
    await db.commit()
    async with db.execute(
        "SELECT * FROM task_log WHERE id = ?", (tid,)
    ) as cur:
        row = await cur.fetchone()
    task = _row_to_dict(row)
    assert task is not None
    _publish_task("task_created", task)
    try:
        await append_executor_run_transcript(db, session_id, description)
    except Exception:
        pass
    return task


async def get_task(
    db: aiosqlite.Connection, task_id: str
) -> dict[str, Any] | None:
    """Look up a single task by id."""
    async with db.execute(
        "SELECT * FROM task_log WHERE id = ?", (task_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def update_task(
    db: aiosqlite.Connection,
    task_id: str,
    *,
    status: str | None = None,
    description: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any] | None:
    """Update a task's status and/or description in place.

    Returns the updated task dict, or None if the id doesn't exist. Used by
    the paid-tier ``enqueue_claude_task`` worker to mark a pending task done
    or failed once the subprocess returns.

    ``project_id`` is an optional safety guard: when supplied the UPDATE
    includes ``AND project_id = ?`` so a caller cannot accidentally mutate a
    task that belongs to a different project.
    """
    fields: list[str] = []
    values: list[Any] = []
    if status is not None:
        if status not in {"pending", "in_progress", "done", "failed", "pending-hitl"}:
            raise ValueError(f"invalid task status: {status}")
        fields.append("status = ?")
        values.append(status)
    if description is not None:
        fields.append("description = ?")
        values.append(description)
    if not fields:
        return await get_task(db, task_id)
    if project_id is not None:
        values.extend([task_id, project_id])
        where = "id = ? AND project_id = ?"
    else:
        values.append(task_id)
        where = "id = ?"
    await db.execute(
        f"UPDATE task_log SET {', '.join(fields)} WHERE {where}", values
    )
    await db.commit()
    updated = await get_task(db, task_id)
    if updated is not None:
        _publish_task("task_updated", updated)
    return updated


async def update_task_worker_pid(
    db: aiosqlite.Connection, task_id: str, pid: int
) -> None:
    """Store the worker subprocess PID on the task row.

    Called immediately after the subprocess spawns so the PID watchdog
    can detect orphaned in_progress tasks whose worker process has died.
    """
    await db.execute(
        "UPDATE task_log SET worker_pid = ? WHERE id = ?", (pid, task_id)
    )
    await db.commit()


async def get_in_progress_tasks_with_pid(
    db: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    """Return all in_progress task rows that have a worker_pid set.

    Used by the PID watchdog in the auto-summary loop to detect orphaned
    workers. A task is orphaned if its worker_pid process is no longer
    running (i.e., ``os.kill(pid, 0)`` raises ``ProcessLookupError``).
    """
    async with db.execute(
        "SELECT * FROM task_log WHERE status = 'in_progress' AND worker_pid IS NOT NULL"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def get_tasks(
    db: aiosqlite.Connection, project_id: str, limit: int = 20, offset: int = 0
) -> list[dict[str, Any]]:
    """Return recent tasks for a project, newest first.

    Joins sessions to include session_name and human_id for display.
    Supports pagination via limit/offset.
    """
    async with db.execute(
        "SELECT t.*, s.name AS session_name, s.human_id AS human_id, "
        "cs.human_id AS claimed_by_human_id, cs.name AS claimed_by_session_name "
        "FROM task_log t "
        "LEFT JOIN sessions s ON s.id = t.session_id "
        "LEFT JOIN sessions cs ON cs.id = t.claimed_by "
        "WHERE t.project_id = ? "
        "ORDER BY t.created_at DESC, t.rowid DESC LIMIT ? OFFSET ?",
        (project_id, limit, offset),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


async def search_tasks(
    db: Any,
    project_id: str,
    query: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Full-text task search.

    Postgres: uses pg_trgm similarity() — no ML model required, fast on the
    GIN index added by _migrate_pg_v27_pg_trgm. Falls back to ILIKE when
    the query is too short for trigrams (< 3 chars).

    SQLite: simple LIKE wildcard match on description.

    Returns [{id, description, status, created_at, similarity, session_name}].
    """
    like_pat = f"%{query}%"
    is_pg = hasattr(db, "_pool")
    if is_pg:
        sql = (
            "SELECT t.id, t.description, t.status, t.created_at, "
            "s.name AS session_name, "
            "COALESCE(similarity(t.description, ?), 0.0) AS similarity "
            "FROM task_log t "
            "LEFT JOIN sessions s ON s.id = t.session_id "
            "WHERE t.project_id = ? "
            "AND (similarity(t.description, ?) > 0.05 OR t.description ILIKE ?) "
            "ORDER BY similarity DESC, t.created_at DESC LIMIT ?"
        )
        params: tuple = (query, project_id, query, like_pat, limit)
    else:
        sql = (
            "SELECT t.id, t.description, t.status, t.created_at, "
            "s.name AS session_name, 1.0 AS similarity "
            "FROM task_log t "
            "LEFT JOIN sessions s ON s.id = t.session_id "
            "WHERE t.project_id = ? AND t.description LIKE ? "
            "ORDER BY t.created_at DESC LIMIT ?"
        )
        params = (project_id, like_pat, limit)
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


def _search_snippet(text: str | None, query: str, window: int = 60) -> str:
    """Return a short context window of ``text`` centered on ``query``.

    Used by :func:`search_all` to give the dashboard a preview of the matching
    body text. Case-insensitive. Adds leading/trailing ellipses when the snippet
    is clipped from a longer body. Returns an empty string when ``text`` is
    falsy or the query term is not present (e.g. a title-only match).
    """
    if not text or not query:
        return ""
    idx = text.lower().find(query.lower())
    if idx == -1:
        return ""
    start = max(0, idx - window)
    end = min(len(text), idx + len(query) + window)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


async def search_all(
    db: Any,
    project_id: str,
    query: str,
    limit: int = 10,
) -> dict[str, Any]:
    """Universal search across task_log, project_notes, sprint_items, and decisions_pinned.

    Matches both header fields (title) and body text:
      - task_log.description
      - project_notes.title + project_notes.body
      - decisions_pinned.title + decisions_pinned.body
      - sprint_items.title + sprint_items.notes

    SQLite: LIKE %query% on all relevant text fields.
    Postgres: ILIKE on all relevant text fields (portable across both backends;
    pg_trgm/tsvector are Postgres-only so we keep to ILIKE here for parity).

    Returns grouped results: {tasks, notes, decisions, sprint_items}.
    Each item includes a ``match_type`` key for the source table and a
    ``snippet`` key — a short window of the matching body text centered on the
    query term (empty string when no body field matched, e.g. a title-only
    match).
    """
    like_pat = f"%{query}%"
    is_pg = hasattr(db, "_pool")

    async def _search(sql: str, params: tuple) -> list[dict[str, Any]]:
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]

    if is_pg:
        tasks_sql = (
            "SELECT id, description, status, created_at, 'task' AS match_type "
            "FROM task_log "
            "WHERE project_id = ? AND description ILIKE ? "
            "ORDER BY created_at DESC LIMIT ?"
        )
        notes_sql = (
            "SELECT id, title, body, tags, created_at, 'note' AS match_type "
            "FROM project_notes "
            "WHERE project_id = ? AND (title ILIKE ? OR body ILIKE ?) "
            "ORDER BY created_at DESC LIMIT ?"
        )
        decisions_sql = (
            "SELECT id, title, body, category, status, created_at, 'decision' AS match_type "
            "FROM decisions_pinned "
            "WHERE project_id = ? AND status = 'active' AND (title ILIKE ? OR body ILIKE ?) "
            "ORDER BY created_at DESC LIMIT ?"
        )
        sprint_sql = (
            "SELECT id, title, notes, version, status, added_at AS created_at, 'sprint_item' AS match_type "
            "FROM sprint_items "
            "WHERE project_id = ? AND (title ILIKE ? OR notes ILIKE ?) "
            "ORDER BY added_at DESC LIMIT ?"
        )
    else:
        tasks_sql = (
            "SELECT id, description, status, created_at, 'task' AS match_type "
            "FROM task_log "
            "WHERE project_id = ? AND description LIKE ? "
            "ORDER BY created_at DESC LIMIT ?"
        )
        notes_sql = (
            "SELECT id, title, body, tags, created_at, 'note' AS match_type "
            "FROM project_notes "
            "WHERE project_id = ? AND (title LIKE ? OR body LIKE ?) "
            "ORDER BY created_at DESC LIMIT ?"
        )
        decisions_sql = (
            "SELECT id, title, body, category, status, created_at, 'decision' AS match_type "
            "FROM decisions_pinned "
            "WHERE project_id = ? AND status = 'active' AND (title LIKE ? OR body LIKE ?) "
            "ORDER BY created_at DESC LIMIT ?"
        )
        sprint_sql = (
            "SELECT id, title, notes, version, status, added_at AS created_at, 'sprint_item' AS match_type "
            "FROM sprint_items "
            "WHERE project_id = ? AND (title LIKE ? OR notes LIKE ?) "
            "ORDER BY added_at DESC LIMIT ?"
        )

    tasks = await _search(tasks_sql, (project_id, like_pat, limit))
    notes = await _search(notes_sql, (project_id, like_pat, like_pat, limit))
    decisions = await _search(decisions_sql, (project_id, like_pat, like_pat, limit))
    sprint_items = await _search(sprint_sql, (project_id, like_pat, like_pat, limit))

    # Attach a body-text snippet for each result so the dashboard search bar can
    # surface matching context. The body field name differs per content type.
    for t in tasks:
        t["snippet"] = _search_snippet(t.get("description"), query)
    for n in notes:
        n["snippet"] = _search_snippet(n.get("body"), query)
    for d in decisions:
        d["snippet"] = _search_snippet(d.get("body"), query)
    for s in sprint_items:
        s["snippet"] = _search_snippet(s.get("notes"), query)

    return {
        "query": query,
        "tasks": tasks,
        "notes": notes,
        "decisions": decisions,
        "sprint_items": sprint_items,
        "total": len(tasks) + len(notes) + len(decisions) + len(sprint_items),
    }


# ---------------------------------------------------------------------------
# Distributed task locking (v0.3.3)
# ---------------------------------------------------------------------------


async def claim_task(
    db: aiosqlite.Connection, task_id: str, session_id: str
) -> dict[str, Any] | None:
    """Atomically claim a pending task for ``session_id``.

    Returns the freshly-claimed task row, or ``None`` if the task is
    already claimed / not pending / does not exist. The single UPDATE
    statement encodes the "first writer wins" race: SQLite serialises
    writes so even concurrent claims from two parallel workers will
    only see one of them flip ``claimed_by`` from NULL.
    """
    cursor = await db.execute(
        "UPDATE task_log SET claimed_by = ?, claimed_at = datetime('now'), "
        "status = 'in_progress' "
        "WHERE id = ? AND claimed_by IS NULL AND status = 'pending'",
        (session_id, task_id),
    )
    await db.commit()
    if cursor.rowcount == 0:
        return None
    updated = await get_task(db, task_id)
    if updated is not None:
        _publish_task("task_updated", updated)
    return updated


async def get_open_task_for_sprint_item(
    db: aiosqlite.Connection, sprint_item_id: str
) -> dict[str, Any] | None:
    """Return the current pending/in-progress task row for a sprint item."""
    async with db.execute(
        "SELECT * FROM task_log WHERE sprint_item_id = ? "
        "AND status IN ('pending', 'in_progress') "
        "ORDER BY CASE WHEN status = 'in_progress' THEN 0 ELSE 1 END, "
        "created_at DESC, id DESC LIMIT 1",
        (sprint_item_id,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_blocking_dependency_for_sprint_item(
    db: aiosqlite.Connection, sprint_item_id: str
) -> dict[str, Any] | None:
    """Return the unmet parent sprint item that blocks a claim, if any."""
    item = await get_sprint_item(db, sprint_item_id)
    if item is None:
        return None
    parent_id = item.get("depends_on")
    if not parent_id:
        return None
    parent = await get_sprint_item(db, parent_id)
    if parent is None:
        return {"id": parent_id, "title": "(missing sprint item)", "status": "missing"}
    if parent.get("status") != "done":
        return parent
    return None


async def release_stale_task_claims(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    exclude_session_id: str | None = None,
    max_age_hours: int = 2,
) -> int:
    """Release stale claimed tasks older than ``max_age_hours`` for a project."""
    from datetime import datetime, timedelta, timezone

    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    ).strftime("%Y-%m-%d %H:%M:%S")
    query = (
        "SELECT id, sprint_item_id FROM task_log "
        "WHERE project_id = ? AND status = 'in_progress' "
        "AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
        "AND claimed_at < ?"
    )
    params: list[Any] = [project_id, cutoff]
    if exclude_session_id is not None:
        query += " AND claimed_by != ?"
        params.append(exclude_session_id)
    async with db.execute(query, tuple(params)) as cur:
        rows = await cur.fetchall()
    task_ids = [row["id"] for row in rows]
    if not task_ids:
        return 0
    linked_item_ids = [row["sprint_item_id"] for row in rows if row["sprint_item_id"]]
    placeholders = ", ".join("?" for _ in task_ids)
    await db.execute(
        f"UPDATE task_log SET status = 'pending', claimed_by = NULL, claimed_at = NULL "
        f"WHERE id IN ({placeholders})",
        tuple(task_ids),
    )
    if linked_item_ids:
        placeholders = ", ".join("?" for _ in linked_item_ids)
        await db.execute(
            f"UPDATE sprint_items SET status = 'pending', completed_at = NULL "
            f"WHERE id IN ({placeholders}) AND status = 'in_progress'",
            tuple(linked_item_ids),
        )
    await db.commit()
    return len(task_ids)


async def release_task(
    db: aiosqlite.Connection, task_id: str, session_id: str
) -> bool:
    """Release a claim previously taken by ``session_id``.

    Returns True if a claim was released; False if the task wasn't held
    by that session (someone else's claim is left untouched).
    """
    cursor = await db.execute(
        "UPDATE task_log SET status = 'pending', claimed_by = NULL, claimed_at = NULL "
        "WHERE id = ? AND claimed_by = ?",
        (task_id, session_id),
    )
    await db.commit()
    if cursor.rowcount == 0:
        return False
    updated = await get_task(db, task_id)
    if updated is not None and updated.get("sprint_item_id"):
        await db.execute(
            "UPDATE sprint_items SET status = 'pending', completed_at = NULL "
            "WHERE id = ? AND status = 'in_progress'",
            (updated["sprint_item_id"],),
        )
        await db.commit()
        updated = await get_task(db, task_id)
    if updated is not None:
        _publish_task("task_updated", updated)
    return True


async def get_claimable_tasks(
    db: aiosqlite.Connection, project_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Return unclaimed pending tasks, newest first.

    Worker pattern: poll this, pick a row, call :func:`claim_task` —
    if the claim returns None another worker beat you to it, try the
    next row.
    """
    async with db.execute(
        "SELECT * FROM task_log WHERE project_id = ? "
        "AND status = 'pending' AND claimed_by IS NULL "
        "ORDER BY created_at ASC, rowid ASC LIMIT ?",
        (project_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


async def expire_idle_sessions(
    db: aiosqlite.Connection, max_age_minutes: int = 30
) -> dict[str, Any]:
    """Mark sessions idle when their last_seen is older than *max_age_minutes*.

    Returns ``{"count": n, "project_ids": [...]}`` where ``project_ids`` is
    the list of distinct projects that had at least one session expire. The
    caller can use this to trigger handoff generation for affected projects
    (v0.4.5). Only 'active' sessions are considered; 'idle' and 'closed'
    sessions are left untouched.
    """
    async with db.execute(
        "SELECT DISTINCT project_id FROM sessions "
        "WHERE status = 'active' "
        "AND last_seen < datetime('now', ? || ' minutes')",
        (f"-{max_age_minutes}",),
    ) as cur:
        rows = await cur.fetchall()
    affected_project_ids: list[str] = [row["project_id"] for row in rows]

    cursor = await db.execute(
        "UPDATE sessions SET status = 'idle' "
        "WHERE status = 'active' "
        "AND last_seen < datetime('now', ? || ' minutes')",
        (f"-{max_age_minutes}",),
    )
    await db.commit()
    return {"count": cursor.rowcount, "project_ids": affected_project_ids}


async def expire_inactive_sessions(
    db: aiosqlite.Connection, max_age_hours: int = 24
) -> dict[str, Any]:
    """Archive sessions whose ``last_seen`` is older than *max_age_hours*.

    A session that hasn't checked in for a day is treated as dead: it is moved
    to 'archived' (so it drops out of the dashboard's active list) and any
    'in_progress' tasks it held — plus their linked sprint_items — are released
    back to 'pending' so another worker can claim them. Runs globally across all
    projects, unlike the per-project :func:`archive_stale_sessions`. Only
    'active' and 'idle' rows are touched. Returns ``{"count", "project_ids"}``.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    async with db.execute(
        "SELECT DISTINCT project_id FROM sessions "
        "WHERE status IN ('active', 'idle') AND last_seen < ?",
        (cutoff,),
    ) as cur:
        affected_project_ids = [row["project_id"] for row in await cur.fetchall()]

    async with db.execute(
        "SELECT id, sprint_item_id, claimed_by FROM task_log "
        "WHERE claimed_by IN ("
        "  SELECT id FROM sessions WHERE status IN ('active', 'idle') AND last_seen < ?"
        ") AND status = 'in_progress'",
        (cutoff,),
    ) as cur:
        stale_rows = [_row_to_dict(r) or {} for r in await cur.fetchall()]
    stale_task_ids = [row["id"] for row in stale_rows]
    if stale_task_ids:
        placeholders = ", ".join("?" for _ in stale_task_ids)
        await db.execute(
            f"UPDATE task_log SET status = 'pending', claimed_by = NULL, claimed_at = NULL "
            f"WHERE id IN ({placeholders})",
            tuple(stale_task_ids),
        )
    await db.commit()
    # bc9259b8 — a crashed worker never calls close_session, so route each linked
    # in_progress sprint item through the stall-retry budget here too: re-queue
    # while under budget, fail silently once exhausted (instead of resetting to
    # pending forever). Dedup on item_id; keep the claiming session for the log.
    _seen_items: set[str] = set()
    for row in stale_rows:
        iid = row.get("sprint_item_id")
        if not iid or iid in _seen_items:
            continue
        _seen_items.add(iid)
        item = await get_sprint_item(db, iid)
        if item is None:
            continue
        await requeue_or_fail_stalled_item(
            db, item["project_id"], iid, session_id=row.get("claimed_by")
        )
    cursor = await db.execute(
        "UPDATE sessions SET status = 'archived' "
        "WHERE status IN ('active', 'idle') AND last_seen < ?",
        (cutoff,),
    )
    await db.commit()
    return {"count": cursor.rowcount, "project_ids": affected_project_ids}


# ---------------------------------------------------------------------------
# Sprint items (v1.1) — machine-trackable checklist alongside the
# free-text sprint field. Fixes the "sprint drift" problem where items
# get written and silently forgotten across sessions.
# ---------------------------------------------------------------------------


_VALID_SPRINT_STATUSES = {
    "pending", "todo", "in_progress", "provisional_complete",
    "done", "failed", "skipped", "pushed", "indeterminate",
}

# Non-terminal statuses that keep a parent item "active" and never stamp
# completed_at. provisional_complete sits between in_progress and done:
# the executor has finished the work but it is not yet verified/deployed, so
# it must NOT roll a parent up to done or count toward percent_complete.
_ACTIVE_SPRINT_STATUSES = {
    "pending", "in_progress", "todo", "indeterminate", "provisional_complete",
}

# Statuses that make an existing item a *blocking* duplicate when a new item
# with a near-identical title is added. Only open/active work counts: a title
# that overlaps a finished item (done / skipped / failed / pushed) is allowed
# through, since re-doing finished work is legitimate. ``todo`` is the DB
# default for freshly-added items and is pending-equivalent here.
_DUP_BLOCKING_SPRINT_STATUSES = {"pending", "todo", "in_progress"}

# b0d42ef6 — fuzzy-duplicate threshold for add_sprint_item. Two titles are
# treated as duplicates when their word-set overlap is >= 60%.
_SPRINT_DUP_OVERLAP_THRESHOLD = 0.60


def _title_word_set(title: str) -> set[str]:
    """Tokenise a sprint-item title into a lowercased word set.

    Splits on any run of non-alphanumeric characters and lowercases, so
    "Add OAuth login!" and "add  oauth   LOGIN" both yield {add, oauth, login}.
    Punctuation and surrounding whitespace are discarded.
    """
    return {w for w in re.split(r"[^0-9a-z]+", title.lower()) if w}


def _title_word_overlap(a: set[str], b: set[str]) -> float:
    """Word-set overlap of two pre-tokenised titles, in ``[0.0, 1.0]``.

    Defined as ``|a ∩ b| / |smaller set|`` (the overlap coefficient). Dividing
    by the smaller of the two word sets makes the metric symmetric and means a
    short title that is fully contained in a longer one scores 1.0 — so
    "Add OAuth" vs "Add OAuth login and refresh-token rotation" is flagged as a
    duplicate even though the longer title has many extra words. Returns 0.0 if
    either set is empty.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))

# ---------------------------------------------------------------------------
# get_sprint_progress 10s cache — one get_sprint_items DB query serves all
# parallel sessions polling between tasks. Keyed by project_id; busted on any
# sprint-item mutation so progress counts never read stale after a write.
# ---------------------------------------------------------------------------
_SPRINT_ITEMS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_SPRINT_ITEMS_CACHE_TTL = 10.0  # seconds


def _invalidate_sprint_items_cache(project_id: str) -> None:
    """Drop the cached sprint-item list for a project after a mutation."""
    _SPRINT_ITEMS_CACHE.pop(project_id, None)


async def get_sprint_items_cached(
    db: aiosqlite.Connection, project_id: str
) -> list[dict[str, Any]]:
    """Return get_sprint_items(project_id), cached for _SPRINT_ITEMS_CACHE_TTL.

    Parallel executors polling get_sprint_progress between tasks share one DB
    query within the TTL window. Any add/update mutation calls
    _invalidate_sprint_items_cache so counts are never stale after a write.
    """
    now = time.monotonic()
    hit = _SPRINT_ITEMS_CACHE.get(project_id)
    if hit is not None and (now - hit[0]) < _SPRINT_ITEMS_CACHE_TTL:
        return hit[1]
    items = await get_sprint_items(db, project_id)
    _SPRINT_ITEMS_CACHE[project_id] = (now, items)
    return items


def _sprint_item_slug_base(text: str) -> str:
    """b944c905 — kebab-case a title into a short human-readable id base."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return "-".join(words[:6])[:60].strip("-") or "item"


async def _unique_sprint_slug(
    db: aiosqlite.Connection,
    project_id: str,
    base: str,
    exclude_id: str | None = None,
) -> str:
    """b944c905 — ``base``, or base-2/base-3/… if the slug is taken in this
    project (mirrors _unique_note_slug; slugs are unique per project)."""
    slug = base
    n = 1
    while True:
        async with db.execute(
            "SELECT id FROM sprint_items WHERE project_id = ? AND slug = ?",
            (project_id, slug),
        ) as cur:
            row = await cur.fetchone()
        existing = _row_to_dict(row)
        if existing is None or existing.get("id") == exclude_id:
            return slug
        n += 1
        slug = f"{base}-{n}"


async def add_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    version: str,
    title: str,
    group: str | None = None,
    human_id: str | None = None,
    depends_on: str | None = None,
    failure_mode: str | None = None,
    milestone_type: str = "task",
    touches_resources: Any = None,
    force: bool = False,
    slug: str | None = None,
) -> dict[str, Any]:
    """Append a new ``todo`` sprint item to a project's checklist.

    ``group`` (stored as ``item_group``) lets items be organised under
    named objectives so the dashboard sprint board can render them in
    logical clusters. ``human_id`` attributes the item to a person.
    ``depends_on`` is the id of a parent sprint item that must be done
    before this item is surfaced as claimable. ``failure_mode`` controls
    what happens when the parent has failed: 'continue' (default) allows
    this item to proceed; 'stop' blocks it.
    ``milestone_type`` is 'task' (default) or 'milestone' — milestones
    render as vertical timeline markers in the sprint swimlane.

    Duplicate guard (b0d42ef6): unless ``force`` is True, the new ``title``
    is compared (word-set overlap, see ``_title_word_overlap``) against every
    open item in the project (status pending / todo / in_progress). If any
    existing item meets the >= 60% overlap threshold the item is **not**
    inserted and a structured error dict is returned instead::

        {"error": "duplicate", "message": ..., "existing": {id, title,
         status, overlap_pct}}

    The caller can pass ``force=True`` to override the guard and insert
    anyway. Finished items (done / skipped / failed / pushed) never block,
    so legitimately re-doing past work is unaffected.
    """
    if failure_mode not in (None, "continue", "stop"):
        raise ValueError("failure_mode must be 'continue' or 'stop'")
    if milestone_type not in ("task", "milestone", "human"):
        raise ValueError("milestone_type must be 'task', 'milestone', or 'human'")
    # b0d42ef6 — block near-duplicate titles against open items unless forced.
    if not force:
        _new_words = _title_word_set(title)
        if _new_words:
            for _ex in await get_sprint_items(db, project_id):
                if _ex.get("status") not in _DUP_BLOCKING_SPRINT_STATUSES:
                    continue
                _overlap = _title_word_overlap(_new_words, _title_word_set(_ex.get("title", "")))
                if _overlap >= _SPRINT_DUP_OVERLAP_THRESHOLD:
                    _pct = round(_overlap * 100)
                    return {
                        "error": "duplicate",
                        "message": (
                            f"Sprint item not created: title is {_pct}% a word-match "
                            f"for existing {_ex['status']} item '{_ex.get('title', '')[:120]}' "
                            f"({_ex['id'][:8]}). Pass force=true to add it anyway, or update "
                            f"the existing item instead."
                        ),
                        "existing": {
                            "id": _ex["id"],
                            "title": _ex.get("title", ""),
                            "status": _ex["status"],
                            "overlap_pct": _pct,
                        },
                    }
    # 501ec93f — normalize + validate typed resource identifiers (raises on bad input).
    resources_json = serialize_touches_resources(touches_resources)
    iid = _new_id()
    # b944c905 — auto-populate a human-readable slug from the title (or a
    # caller-supplied one), deduped per project.
    _item_slug = await _unique_sprint_slug(
        db, project_id, _sprint_item_slug_base(slug or title)
    )
    await db.execute(
        "INSERT INTO sprint_items "
        "(id, project_id, version, title, item_group, human_id, depends_on, "
        "failure_mode, milestone_type, touches_resources, slug) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (iid, project_id, version, title, group, human_id,
         depends_on, failure_mode or "continue", milestone_type, resources_json, _item_slug),
    )
    await db.commit()
    item = await get_sprint_item(db, iid)
    assert item is not None
    _invalidate_sprint_items_cache(project_id)
    # ITEM 6 — live push so dashboards refresh the sprint board without polling.
    _publish_project_event(project_id, "sprint_item_added", {"item_id": iid})
    return item


async def fan_out_sprint_items(
    db: aiosqlite.Connection,
    project_id: str,
    items: list[dict[str, Any]],
) -> list[str]:
    """Bulk-insert sprint items for an orchestrator decomposing a goal.

    ``items`` is a list of dicts, each with at minimum ``title`` (required)
    and optionally ``description``, ``group``, and ``version``.  Missing
    ``version`` defaults to the empty string (same as the common add_sprint_item
    convention).  Unlike add_sprint_item the duplicate guard is **not** applied
    here — the orchestrator is assumed to have already deduped.

    Returns the list of new item IDs in insertion order.
    """
    ids: list[str] = []
    for spec in items:
        title = (spec.get("title") or "").strip()
        if not title:
            continue
        version = (spec.get("version") or spec.get("sprint") or "").strip()
        group = spec.get("group") or spec.get("item_group") or None
        description = spec.get("description") or None
        try:
            resources_json = serialize_touches_resources(spec.get("touches_resources"))
        except ValueError:
            resources_json = None  # best-effort in bulk insert — skip bad values
        iid = _new_id()
        await db.execute(
            "INSERT INTO sprint_items "
            "(id, project_id, version, title, item_group, notes, touches_resources) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (iid, project_id, version, title, group, description, resources_json),
        )
        ids.append(iid)
    if ids:
        await db.commit()
        _invalidate_sprint_items_cache(project_id)
        _publish_project_event(project_id, "sprint_items_fanned_out", {"item_ids": ids})
    return ids


async def get_sprint_item(
    db: aiosqlite.Connection, item_id: str
) -> dict[str, Any] | None:
    """Fetch one sprint item by id."""
    async with db.execute(
        "SELECT * FROM sprint_items WHERE id = ?", (item_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def _update_sprint_item_status(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    status: str,
    task_id: str | None = None,
    notes: str | None = None,
    pushed_to: str | None = None,
    actor: str | None = None,
) -> dict[str, Any] | None:
    """Internal: flip a sprint item's status and optionally link a task.

    Terminal statuses (done / skipped / failed / pushed) stamp
    ``completed_at``; non-terminal statuses clear it. ``pushed_to``
    records the target version when status == 'pushed'. ``actor`` (5823db0b)
    records the executor id/name that made this transition.
    """
    if status not in _VALID_SPRINT_STATUSES:
        raise ValueError(f"invalid sprint-item status: {status!r}")
    fields = ["status = ?"]
    values: list[Any] = [status]
    if status in {"done", "skipped", "failed", "pushed"}:
        fields.append("completed_at = datetime('now')")
    else:
        fields.append("completed_at = NULL")
    if status == "done":
        fields.append("claimed_at = COALESCE(claimed_at, datetime('now'))")
    if task_id is not None:
        fields.append("task_id = ?")
        values.append(task_id)
    if notes is not None:
        fields.append("notes = ?")
        values.append(notes)
    if pushed_to is not None:
        fields.append("pushed_to = ?")
        values.append(pushed_to)
    if actor is not None:
        fields.append("actor = ?")
        values.append(actor)
    values.append(item_id)
    values.append(project_id)
    cursor = await db.execute(
        f"UPDATE sprint_items SET {', '.join(fields)} "
        f"WHERE id = ? AND project_id = ?",
        values,
    )
    await db.commit()
    if cursor.rowcount == 0:
        return None
    _invalidate_sprint_items_cache(project_id)
    result = await get_sprint_item(db, item_id)
    # Broadcast to dashboard WebSocket subscribers so the sprint board refreshes live.
    _publish_project_event(project_id, "sprint_item_updated", {"item_id": item_id, "status": status})
    return result


async def _maybe_rollup_parent(db: aiosqlite.Connection, project_id: str, item_id: str) -> None:
    """After a child status change, roll up sibling statuses to parent if applicable."""
    item = await get_sprint_item(db, item_id)
    if item is None:
        return
    parent_id = item.get("parent_id")
    if not parent_id:
        return
    async with db.execute(
        "SELECT status FROM sprint_items WHERE parent_id = ? AND project_id = ?",
        (parent_id, project_id),
    ) as cur:
        rows = await cur.fetchall()
    statuses = [r[0] or "pending" for r in rows]
    if not statuses:
        return
    has_active = any(s in _ACTIVE_SPRINT_STATUSES for s in statuses)
    if has_active:
        return
    has_failed = any(s == "failed" for s in statuses)
    all_terminal_ok = all(s in {"done", "skipped"} for s in statuses)
    if all_terminal_ok:
        await _update_sprint_item_status(db, project_id, parent_id, "done")
    elif has_failed:
        await _update_sprint_item_status(db, project_id, parent_id, "indeterminate")


class SprintItemEvidenceRequired(ValueError):
    """Raised when complete_sprint_item is blocked by the required_notes gate
    (5823db0b) — the item is flagged required_notes but has no evidence."""


async def complete_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    task_id: str | None = None,
    notes: str | None = None,
    actor: str | None = None,
) -> dict[str, Any] | None:
    """Mark a sprint item ``done`` and optionally link the task that shipped it.

    4f02340e — when the completed item is part of a mixed-ownership subtask
    chain, advance the chain: an AI→human transition auto-files a HITL handoff,
    a human→AI transition un-blocks the next AI subtask (see
    :func:`_advance_task_chain`).

    5823db0b — quality gate: when the item is flagged ``required_notes``, refuse
    to complete unless evidence exists — an existing ``notes`` value, a linked
    ``task_id``, or a ``notes`` argument on this call (which is persisted).
    ``actor`` records which executor completed the item.
    """
    item = await get_sprint_item(db, item_id)
    if item is not None and item.get("project_id") == project_id:
        if item.get("required_notes"):
            has_evidence = bool(
                (notes or "").strip()
                or task_id
                or (item.get("notes") or "").strip()
                or (item.get("task_id"))
            )
            if not has_evidence:
                raise SprintItemEvidenceRequired(
                    f"item {item_id} requires evidence before completion — pass "
                    "notes=... (what shipped / how it was verified) or link a "
                    "task_id. This item was flagged required_notes."
                )
    result = await _update_sprint_item_status(
        db, project_id, item_id, "done", task_id=task_id, notes=notes, actor=actor
    )
    if result is not None:
        await _maybe_rollup_parent(db, project_id, item_id)
        await _advance_task_chain(db, project_id, item_id)
    return result


async def provisional_complete_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    task_id: str | None = None,
) -> dict[str, Any] | None:
    """Mark a sprint item ``provisional_complete`` — work finished but not yet
    verified/deployed.

    A non-terminal state between in_progress and done: it does not stamp
    ``completed_at``, does not count toward percent_complete, and keeps any
    parent item active (no roll-up). The executor flips it to ``done`` via
    complete_sprint_item once the change is verified/shipped.
    """
    return await _update_sprint_item_status(
        db, project_id, item_id, "provisional_complete", task_id=task_id
    )


async def skip_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Mark a sprint item ``skipped`` (intentionally not shipped)."""
    result = await _update_sprint_item_status(
        db, project_id, item_id, "skipped", notes=reason
    )
    if result is not None:
        await _maybe_rollup_parent(db, project_id, item_id)
    return result


async def start_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
) -> dict[str, Any] | None:
    """Flip a sprint item from ``pending``/``todo`` to ``in_progress``."""
    return await _update_sprint_item_status(
        db, project_id, item_id, "in_progress"
    )


async def claim_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    actor: str | None = None,
) -> dict[str, Any] | None:
    """Claim a sprint item: set status='in_progress' and claimed_at=now().

    Rejects (raises ValueError) if already in_progress, done, failed, or skipped.
    Returns None if the item doesn't exist. ``actor`` (5823db0b) records which
    executor claimed the item.
    """
    item = await get_sprint_item(db, item_id)
    if item is None:
        return None
    if item.get("project_id") != project_id:
        return None
    blocked = {"in_progress", "done", "failed", "skipped"}
    if (item.get("status") or "pending") in blocked:
        raise ValueError(
            f"cannot claim item with status '{item.get('status')}'"
        )
    cursor = await db.execute(
        "UPDATE sprint_items SET status = 'in_progress', "
        "claimed_at = datetime('now'), actor = COALESCE(?, actor) "
        "WHERE id = ? AND project_id = ?",
        (actor, item_id, project_id),
    )
    await db.commit()
    if cursor.rowcount == 0:
        return None
    updated = await get_sprint_item(db, item_id)
    _publish_project_event(project_id, "sprint_item_updated", {"item_id": item_id, "status": "in_progress"})
    return updated


async def fail_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Mark a sprint item ``failed``. ``reason`` stored in ``notes``."""
    result = await _update_sprint_item_status(
        db, project_id, item_id, "failed", notes=reason
    )
    if result is not None:
        await _maybe_rollup_parent(db, project_id, item_id)
    return result


async def push_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    to_version: str,
) -> dict[str, Any] | None:
    """Mark a sprint item ``pushed`` — deferred to a future version.

    ``to_version`` is stored in ``pushed_to`` so the board can show
    where the item was moved and the next sprint can pick it up.
    """
    if not to_version:
        raise ValueError("to_version is required for push_sprint_item")
    return await _update_sprint_item_status(
        db, project_id, item_id, "pushed", pushed_to=to_version
    )


async def patch_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    title: str | None = None,
    version: str | None = None,
    status: str | None = None,
    feedback_thumb: int | None = None,
    feedback_note: str | None = None,
    notes: str | None = None,
    human_id: str | None = None,
    item_group: str | None = None,
    touches_resources: Any = _UNSET,
    required_notes: bool | int | None = None,
) -> dict[str, Any] | None:
    """Update editable fields of a sprint item.

    Editable: title, version, status, feedback, notes, human_id (assignee),
    item_group, touches_resources, required_notes. Only fields passed as
    non-None are changed;
    omitted fields are left untouched. To clear human_id or item_group, pass an
    empty string. ``touches_resources`` (501ec93f) uses the ``_UNSET`` sentinel
    so it can be omitted entirely; pass ``None`` or ``[]`` to clear it, or a list
    / JSON string / comma-separated string of typed ids to set it.
    """
    fields: list[str] = []
    values: list[Any] = []
    if title is not None:
        fields.append("title = ?")
        values.append(title)
    if version is not None:
        fields.append("version = ?")
        values.append(version)
    if status is not None:
        if status not in _VALID_SPRINT_STATUSES:
            raise ValueError(f"invalid sprint-item status: {status!r}")
        fields.append("status = ?")
        values.append(status)
    if feedback_thumb is not None:
        fields.append("feedback_thumb = ?")
        values.append(int(feedback_thumb))
    if feedback_note is not None:
        fields.append("feedback_note = ?")
        values.append(feedback_note)
    if notes is not None:
        fields.append("notes = ?")
        values.append(notes)
    if human_id is not None:
        fields.append("human_id = ?")
        values.append(human_id or None)
    if item_group is not None:
        fields.append("item_group = ?")
        values.append(item_group or None)
    if touches_resources is not _UNSET:
        fields.append("touches_resources = ?")
        values.append(serialize_touches_resources(touches_resources))
    if required_notes is not None:
        fields.append("required_notes = ?")
        values.append(1 if required_notes else 0)
    if not fields:
        return await get_sprint_item(db, item_id)
    values.extend([item_id, project_id])
    cursor = await db.execute(
        f"UPDATE sprint_items SET {', '.join(fields)} WHERE id = ? AND project_id = ?",
        values,
    )
    await db.commit()
    if cursor.rowcount == 0:
        return None
    return await get_sprint_item(db, item_id)


async def add_subtask(
    db: aiosqlite.Connection,
    project_id: str,
    parent_id: str,
    title: str,
    owner: str | None = None,
) -> dict[str, Any]:
    """Create a child sprint item under parent_id.

    Inherits version from parent. Rejects if parent doesn't exist or is
    done/failed/skipped.

    4f02340e — mixed-ownership task chains. ``owner`` is 'human', 'ai', or None
    (unassigned). When owner-tagged subtasks are added in sequence they form a
    *chain*: each new owned subtask ``depends_on`` the previously added owned
    sibling, so only the head of the chain is claimable and ownership alternates
    as the chain advances (see :func:`_advance_task_chain`). When an AI subtask
    completes and the next link is human-owned, a HITL handoff is auto-filed;
    when a human completes theirs, the next AI subtask un-blocks (becomes
    claimable). The parent stays in_progress until every subtask is terminal
    (existing :func:`_maybe_rollup_parent` behavior — unchanged).

    Unowned subtasks (owner=None) keep the legacy behavior: no chaining,
    independently claimable.
    """
    if owner not in (None, "human", "ai"):
        raise ValueError("owner must be 'human', 'ai', or None")
    parent = await get_sprint_item(db, parent_id)
    if parent is None or parent.get("project_id") != project_id:
        raise ValueError(f"parent sprint item not found: {parent_id}")
    blocked = {"done", "failed", "skipped"}
    if (parent.get("status") or "pending") in blocked:
        raise ValueError(
            f"cannot add subtask to parent with status '{parent.get('status')}'"
        )
    # Chain owned subtasks: a new owned subtask depends on the current tail of
    # the chain — the owned sibling that no other owned sibling depends on yet.
    # This is insertion-order-independent (added_at has only second resolution,
    # so it can't be used to break ties deterministically) and portable across
    # SQLite/Postgres. Unowned subtasks never chain.
    depends_on: str | None = None
    if owner is not None:
        async with db.execute(
            "SELECT id FROM sprint_items "
            "WHERE parent_id = ? AND project_id = ? AND owner IS NOT NULL "
            "AND id NOT IN ("
            "  SELECT depends_on FROM sprint_items "
            "  WHERE parent_id = ? AND project_id = ? AND depends_on IS NOT NULL"
            ")",
            (parent_id, project_id, parent_id, project_id),
        ) as cur:
            tails = await cur.fetchall()
        # In a well-formed chain there is exactly one tail. If somehow more than
        # one (e.g. an unchained owned item existed), prefer the one matching no
        # dependents — take the first deterministically by id.
        tail_ids = sorted(
            (r["id"] if isinstance(r, dict) else r[0]) for r in tails
        )
        if tail_ids:
            depends_on = tail_ids[-1]
    iid = _new_id()
    await db.execute(
        "INSERT INTO sprint_items "
        "(id, project_id, version, title, parent_id, milestone_type, owner, depends_on) "
        "VALUES (?, ?, ?, ?, ?, 'task', ?, ?)",
        (iid, project_id, parent.get("version", ""), title, parent_id, owner, depends_on),
    )
    await db.commit()
    item = await get_sprint_item(db, iid)
    assert item is not None
    _invalidate_sprint_items_cache(project_id)
    return item


async def _advance_task_chain(
    db: aiosqlite.Connection,
    project_id: str,
    completed_item_id: str,
) -> dict[str, Any] | None:
    """4f02340e — advance a mixed-ownership subtask chain after a completion.

    Called when a subtask is marked ``done``. Finds the next link in the chain
    (the owned sibling whose ``depends_on`` is the just-completed item) and:

      - next link is **human**-owned  → auto-file a HITL handoff (kind
        ``'handoff'``, assigned to the human) so a person is pulled in. The next
        item un-blocks (its depends_on is now done) and shows as claimable in
        the human's queue.
      - next link is **ai**-owned     → no HITL; the item simply un-blocks and
        becomes claimable by an AI session (existing depends_on machinery).

    Returns the filed HITL request dict when a handoff was created, else None.
    Idempotent-ish: a handoff is only filed when the just-completed item is
    itself owned (so it is part of a chain) and a next owned link exists.
    """
    completed = await get_sprint_item(db, completed_item_id)
    if completed is None or completed.get("project_id") != project_id:
        return None
    if not completed.get("owner"):
        return None  # not part of an owned chain
    # The next link: an owned sibling that depends on the completed item.
    async with db.execute(
        "SELECT * FROM sprint_items "
        "WHERE project_id = ? AND depends_on = ? AND owner IS NOT NULL "
        "ORDER BY added_at ASC, id ASC LIMIT 1",
        (project_id, completed_item_id),
    ) as cur:
        row = await cur.fetchone()
    nxt = _row_to_dict(row) if row is not None else None
    if not nxt:
        return None
    if (nxt.get("status") or "pending") in {"done", "failed", "skipped"}:
        return None
    if nxt.get("owner") != "human":
        # AI link — nothing to file; depends_on now satisfied → claimable.
        _publish_project_event(
            project_id, "sprint_item_updated",
            {"item_id": nxt["id"], "chain": "ai_claimable"},
        )
        return None
    # Human link — pull a person in via a HITL handoff.
    title = nxt.get("title", "")
    question = (
        f"Task chain handoff: your turn on subtask '{title}'. "
        f"The preceding AI subtask ('{completed.get('title', '')}') is complete."
    )
    context = (
        f"Mixed-ownership task chain (parent {completed.get('parent_id') or '?'}). "
        f"Next subtask {nxt['id']} is assigned to a human. Mark it done "
        f"(complete_sprint_item) to release the following AI subtask."
    )
    hitl = await request_hitl(
        db, project_id, question,
        context=context,
        kind="handoff",
        assigned_to=nxt.get("human_id") or "human",
    )
    _publish_project_event(
        project_id, "sprint_item_updated",
        {"item_id": nxt["id"], "chain": "human_handoff", "hitl_id": hitl.get("id")},
    )
    return hitl


async def split_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    titles: list[str],
) -> list[dict[str, Any]]:
    """Split a sprint item into N new items at the same level.

    Closes the original (status=skipped). New items inherit parent_id and
    version from the original, with split_from=item_id.
    """
    original = await get_sprint_item(db, item_id)
    if original is None or original.get("project_id") != project_id:
        raise ValueError(f"sprint item not found: {item_id}")
    allowed = {"pending", "in_progress"}
    if (original.get("status") or "pending") not in allowed:
        raise ValueError(
            f"can only split pending or in_progress items, got '{original.get('status')}'"
        )
    if not titles:
        raise ValueError("titles must not be empty")
    # Close the original
    await _update_sprint_item_status(db, project_id, item_id, "skipped")
    # Create new items
    new_items = []
    for t in titles:
        nid = _new_id()
        await db.execute(
            "INSERT INTO sprint_items "
            "(id, project_id, version, title, parent_id, split_from, milestone_type) "
            "VALUES (?, ?, ?, ?, ?, ?, 'task')",
            (nid, project_id, original.get("version", ""), t,
             original.get("parent_id"), item_id),
        )
        await db.commit()
        new_item = await get_sprint_item(db, nid)
        if new_item:
            new_items.append(new_item)
    return new_items


async def merge_sprint_items(
    db: aiosqlite.Connection,
    project_id: str,
    item_ids: list[str],
    new_title: str,
) -> dict[str, Any]:
    """Merge N sprint items into one survivor.

    Closes all sources (status=skipped, merged_into=survivor_id).
    Creates survivor with merged_from=JSON(item_ids), version from first source.
    All sources must be pending or in_progress.
    """
    if not item_ids:
        raise ValueError("item_ids must not be empty")
    sources = []
    allowed = {"pending", "in_progress"}
    for iid in item_ids:
        item = await get_sprint_item(db, iid)
        if item is None or item.get("project_id") != project_id:
            raise ValueError(f"sprint item not found: {iid}")
        if (item.get("status") or "pending") not in allowed:
            raise ValueError(
                f"cannot merge item '{iid}' with status '{item.get('status')}'"
            )
        sources.append(item)
    # Create the survivor first
    survivor_id = _new_id()
    version = sources[0].get("version", "")
    merged_from_json = json.dumps(item_ids)
    await db.execute(
        "INSERT INTO sprint_items "
        "(id, project_id, version, title, merged_from, milestone_type) "
        "VALUES (?, ?, ?, ?, ?, 'task')",
        (survivor_id, project_id, version, new_title, merged_from_json),
    )
    # Close all sources
    for iid in item_ids:
        await db.execute(
            "UPDATE sprint_items SET status = 'skipped', completed_at = datetime('now'), "
            "merged_into = ? WHERE id = ? AND project_id = ?",
            (survivor_id, iid, project_id),
        )
    await db.commit()
    _publish_project_event(project_id, "sprint_item_updated", {"merged_into": survivor_id})
    survivor = await get_sprint_item(db, survivor_id)
    assert survivor is not None
    return survivor


async def get_sprint_items_page(
    db: aiosqlite.Connection,
    project_id: str,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Return one SQL LIMIT/OFFSET page of sprint items plus the total count.

    True server-side pagination for large completed lists (hundreds of rows) so
    the dashboard's Completed tab doesn't fetch everything at once. Mirrors
    get_sprint_items ordering. Does not do dependency (show_blocked) filtering —
    it's for flat status-filtered lists like status='done'.
    """
    where = "project_id = ?"
    params_list: list = [project_id]
    if status is not None:
        if status not in _VALID_SPRINT_STATUSES:
            raise ValueError(
                f"invalid sprint-item status filter: {status!r}. "
                f"Valid: {sorted(_VALID_SPRINT_STATUSES)}"
            )
        where += " AND status = ?"
        params_list.append(status)
    async with db.execute(
        f"SELECT COUNT(*) AS c FROM sprint_items WHERE {where}", tuple(params_list)
    ) as cur:
        crow = await cur.fetchone()
    total = int(crow["c"] if isinstance(crow, dict) else crow[0]) if crow else 0
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    async with db.execute(
        f"SELECT * FROM sprint_items WHERE {where} "
        "ORDER BY added_at ASC, rowid ASC LIMIT ? OFFSET ?",
        (*params_list, limit, offset),
    ) as cur:
        rows = await cur.fetchall()
    items = [_row_to_dict(r) for r in rows]  # type: ignore[misc]
    return items, total


async def infer_active_sprint_version(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """a76cb7c0 — infer the sprint-version bucket with the most pending items.

    Counts pending (status ``pending``/``todo``) sprint items per non-empty
    ``version`` and returns the bucket with the most. Human-assigned items
    (milestone_type='human') are excluded — executor scoping should track the
    automatable backlog, not a person's task list. Ties break on the bucket whose
    earliest pending item was added first (the older sprint), so scoping is
    stable across calls rather than flapping between equally-sized buckets.

    Returns ``None`` when there are no pending items (or none carry a version),
    so a session over an empty/version-less board is left unscoped (no filter)
    and behaves exactly as before.
    """
    counts: dict[str, int] = {}
    first_seen: dict[str, str] = {}
    for it in await get_sprint_items(db, project_id, include_human=False):
        if it.get("status") not in ("pending", "todo"):
            continue
        version = it.get("version")
        if not version:
            continue
        counts[version] = counts.get(version, 0) + 1
        added = str(it.get("added_at") or "")
        # Items arrive oldest-first, so the first add_at we see per bucket is
        # its earliest pending item — record it once for stable tie-breaking.
        first_seen.setdefault(version, added)
    if not counts:
        return None
    # Most pending wins; ties go to the bucket whose earliest pending item is
    # oldest (smallest added_at) for deterministic, non-flapping scoping.
    return max(
        counts,
        key=lambda v: (counts[v], _NEG_TS(first_seen.get(v, ""))),
    )


def _NEG_TS(ts: str) -> tuple[int, str]:
    """Sort key making an EARLIER timestamp rank HIGHER in a max() tie-break.

    Empty timestamps sort last (rank lowest). Returns a tuple whose natural
    ordering is the reverse of the string order, so ``max(...)`` prefers the
    oldest item without needing a separate min pass.
    """
    if not ts:
        return (0, "")
    # 1 outranks 0 (non-empty beats empty); the inverted string makes an
    # earlier ts compare greater than a later one under default tuple ordering.
    inverted = "".join(chr(255 - min(ord(c), 255)) for c in ts)
    return (1, inverted)


async def count_pending_sprint_items(
    db: aiosqlite.Connection, project_id: str
) -> int:
    """c0d2356d — count of not-yet-done sprint items (status pending/todo) for a
    project. Backs the Stop-hook sprint guard's /sprint/pending_count endpoint."""
    async with db.execute(
        "SELECT COUNT(*) AS c FROM sprint_items "
        "WHERE project_id = ? AND status IN ('pending', 'todo')",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return 0
    return int(row["c"] if isinstance(row, dict) else row[0])


async def get_sprint_items(
    db: aiosqlite.Connection,
    project_id: str,
    status: str | None = None,
    show_blocked: bool = True,
    include_human: bool = True,
    version: str | None = None,
) -> list[dict[str, Any]]:
    """List sprint items for a project, oldest first.

    ``status`` filter is optional. ``None`` returns everything so the
    dashboard can render the full timeline.

    ``show_blocked=False`` hides items whose ``depends_on`` parent is not
    yet in a terminal state (done/skipped/failed/pushed), or whose parent
    has failed while the item has ``failure_mode='stop'``.

    ``include_human=False`` excludes items with milestone_type='human'
    (used for executor sessions that should not see human-assigned tasks).

    ``version`` (a76cb7c0) filters to a single sprint-version bucket. ``None``
    returns every version. Used by version-scoped sessions so an executor sees
    only the items in its bucket.
    """
    clauses = ["project_id = ?"]
    params_list: list = [project_id]
    if status is not None:
        if status not in _VALID_SPRINT_STATUSES:
            raise ValueError(
                f"invalid sprint-item status filter: {status!r}. "
                f"Valid: {sorted(_VALID_SPRINT_STATUSES)}"
            )
        clauses.append("status = ?")
        params_list.append(status)
    if version is not None:
        clauses.append("version = ?")
        params_list.append(version)
    if not include_human:
        clauses.append("(milestone_type IS NULL OR milestone_type != 'human')")
    query = (
        f"SELECT * FROM sprint_items WHERE {' AND '.join(clauses)} "
        "ORDER BY added_at ASC, rowid ASC"
    )
    params: tuple = tuple(params_list)
    async with db.execute(query, params) as cur:
        rows = await cur.fetchall()
    items = [_row_to_dict(r) for r in rows]  # type: ignore[misc]
    if show_blocked:
        return items
    # Build status lookup for dependency filtering
    _terminal = {"done", "skipped", "failed", "pushed"}
    by_id = {it["id"]: it for it in items}
    # Fetch any parents not in this result set (e.g. filtered by status)
    all_statuses: dict[str, str] = {it["id"]: it["status"] for it in items}
    missing_parents = {
        it["depends_on"] for it in items
        if it.get("depends_on") and it["depends_on"] not in all_statuses
    }
    for parent_id in missing_parents:
        parent = await get_sprint_item(db, parent_id)
        if parent:
            all_statuses[parent["id"]] = parent["status"]
    result = []
    for it in items:
        pid = it.get("depends_on")
        if not pid:
            result.append(it)
            continue
        parent_status = all_statuses.get(pid, "")
        if parent_status not in _terminal:
            continue  # blocked: parent not finished
        if parent_status == "failed" and it.get("failure_mode") == "stop":
            continue  # chain stopped
        result.append(it)
    return result


async def get_goal_field_ages(
    db: aiosqlite.Connection, project_id: str, now: float | None = None
) -> dict[str, dict[str, Any]]:
    """v1.1.3 — per-field freshness for the three goal fields.

    Walks ``goal_states`` history and finds the most recent version
    where each of north_star / version_goal / sprint *actually
    changed* (or was first set). Returns
    ``{field: {updated_at, age_seconds}}`` with empty / never-set
    fields reported as ``{updated_at: None, age_seconds: None}``.

    v2.3 — prefers the per-field ``ns_updated_at`` /
    ``content_updated_at`` / ``sprint_updated_at`` columns when set
    (the latest row carries them). Falls back to history walking when
    they are NULL (pre-migration rows).
    """
    import time as _time
    now = now if now is not None else _time.time()
    async with db.execute(
        "SELECT version, goal_north_star, content, goal_sprint, updated_at, "
        "ns_updated_at, content_updated_at, sprint_updated_at "
        "FROM goal_states WHERE project_id = ? ORDER BY version ASC",
        (project_id,),
    ) as cur:
        rows = await cur.fetchall()
    field_ts: dict[str, str | None] = {
        "north_star": None, "version_goal": None, "sprint": None,
    }
    # v2.3 preferred path — if the latest row carries per-field
    # timestamps, use them directly. Cheaper and accurate even when
    # in-place UPDATEs have collapsed multi-row history.
    if rows:
        last = rows[-1]
        ns_t = last["ns_updated_at"]
        ct_t = last["content_updated_at"]
        sp_t = last["sprint_updated_at"]
        if ns_t and (last["goal_north_star"] or ""):
            field_ts["north_star"] = ns_t
        if ct_t and (last["content"] or ""):
            field_ts["version_goal"] = ct_t
        if sp_t and (last["goal_sprint"] or ""):
            field_ts["sprint"] = sp_t

    # Fall back to history walking for any field still unstamped (pre-
    # v2.3 rows). This preserves backward compat with legacy DBs.
    if any(v is None for v in field_ts.values()):
        prev: dict[str, Any] = {"north_star": "", "version_goal": "", "sprint": ""}
        for r in rows:
            row = {
                "north_star": r["goal_north_star"] or "",
                "version_goal": (r["content"] or "")
                    if isinstance(r["content"], str) else str(r["content"] or ""),
                "sprint": r["goal_sprint"] or "",
            }
            for field in ("north_star", "version_goal", "sprint"):
                if field_ts[field] is None and row[field] and row[field] != prev[field]:
                    field_ts[field] = r["updated_at"]
            prev = row

    from datetime import datetime, timezone
    out: dict[str, dict[str, Any]] = {}
    for field, ts in field_ts.items():
        if ts is None:
            out[field] = {"updated_at": None, "age_seconds": None}
            continue
        try:
            dt = datetime.fromisoformat(ts.replace(" ", "T")).replace(
                tzinfo=timezone.utc
            )
            age = max(0.0, now - dt.timestamp())
        except ValueError:
            age = None  # type: ignore[assignment]
        out[field] = {"updated_at": ts, "age_seconds": age}
    return out


def compute_coherence_warning(
    field_ages: dict[str, dict[str, Any]],
    warn_days: float = 7.0,
    critical_days: float = 30.0,
) -> dict[str, Any]:
    """Turn per-field ages into a single warning level.

    ``ok`` when every field's age < ``warn_days``.
    ``warn`` when one or more sit between ``warn`` and ``critical``.
    ``critical`` when any field is older than ``critical_days``.
    ``stale_fields`` lists every field older than ``warn_days``
    (sorted oldest-first so the UI can highlight the worst offender).
    """
    warn = warn_days * 86400
    crit = critical_days * 86400
    stale: list[dict[str, Any]] = []
    level = "ok"
    max_age = 0.0
    for field, info in field_ages.items():
        age = info.get("age_seconds")
        if age is None:
            continue
        if age >= warn:
            stale.append({"field": field, "age_seconds": age})
        if age > max_age:
            max_age = age
    stale.sort(key=lambda x: x["age_seconds"], reverse=True)
    if max_age >= crit:
        level = "critical"
    elif max_age >= warn:
        level = "warn"
    if level == "ok":
        message = "Goal fields are fresh."
    elif level == "warn":
        message = (
            "Some goal fields haven't been touched in over "
            f"{int(warn_days)} days."
        )
    else:
        message = (
            "Goal fields are stale — review and update before "
            "starting more work."
        )
    return {
        "level": level,
        "message": message,
        "stale_fields": stale,
        "max_age_seconds": max_age,
    }


#: Free-text human_id aliases that refer to the same person. Collapsed to a
#: single canonical identity so the timeline shows one calendar row per person
#: instead of one per spelling. Keys are lowercased; extend as new aliases show
#: up. (item 30 — identity unification.)
_PERSON_ALIASES: dict[str, str] = {
    "adam camerer": "adam",
    "adamcamerer": "adam",
    "adam camerer (executor)": "adam",
}


def canonical_person(human_id: str | None) -> str:
    """Map a free-text ``human_id`` to a stable canonical identity.

    Emails are already canonical (lowercased). Other values are lowercased,
    trimmed, and run through :data:`_PERSON_ALIASES`. Empty / "(unknown)"
    values collapse to a single ``"(unknown)"`` bucket.
    """
    if not human_id:
        return "(unknown)"
    raw = human_id.strip()
    if not raw or raw.lower() in ("(unknown)", "unknown", "none"):
        return "(unknown)"
    key = raw.lower()
    if "@" in key:
        return key
    return _PERSON_ALIASES.get(key, key)


async def get_timeline(
    db: aiosqlite.Connection, project_id: str
) -> dict[str, Any]:
    """v1.1.1 — return everything the Activity Timeline needs in one call.

    Returns ``{tasks, sessions, goal_events}`` where:

      * ``tasks``     — every task in the project with its session
        name attached so the frontend can lay them out per swimlane.
      * ``sessions``  — id, name, human_id, created_at (registered),
        last_seen. Drives one swimlane per row.
      * ``goal_events`` — every (north_star, version_goal, sprint)
        update with the field that changed and the timestamp. Drives
        the vertical dashed lines on the time axis.
    """
    async with db.execute(
        "SELECT t.id, t.created_at, t.status, t.description, "
        "       t.session_id, s.name AS session_name, s.human_id, "
        "       s.client_type "
        "FROM task_log t LEFT JOIN sessions s ON s.id = t.session_id "
        "WHERE t.project_id = ? "
        "ORDER BY t.created_at DESC, t.rowid DESC",
        (project_id,),
    ) as cur:
        task_rows = await cur.fetchall()
    tasks = [
        {
            "id": r["id"],
            "created_at": r["created_at"],
            "status": r["status"],
            "description": r["description"],
            "session_id": r["session_id"],
            "session_name": r["session_name"] or "(unknown)",
            "human_id": r["human_id"],
            "person": canonical_person(r["human_id"]),
            "client": r["client_type"] or "(none)",
        }
        for r in task_rows
    ]

    async with db.execute(
        "SELECT id, name, human_id, session_type, status, created_at, "
        "       last_seen, session_summary "
        "FROM sessions WHERE project_id = ? "
        "ORDER BY created_at DESC, rowid DESC",
        (project_id,),
    ) as cur:
        session_rows = await cur.fetchall()
    sessions = []
    for r in session_rows:
        summary = None
        if r["session_summary"]:
            try:
                summary = json.loads(r["session_summary"])
            except (ValueError, TypeError):
                summary = None
        sessions.append({
            "id": r["id"],
            "name": r["name"],
            "human_id": r["human_id"],
            "session_type": r["session_type"] or "human",
            "status": r["status"],
            "registered_at": r["created_at"],
            "last_seen": r["last_seen"],
            "summary": summary,
        })

    async with db.execute(
        "SELECT version, goal_north_star, content, goal_sprint, "
        "       created_at, updated_at "
        "FROM goal_states WHERE project_id = ? "
        "ORDER BY version ASC",
        (project_id,),
    ) as cur:
        goal_rows = await cur.fetchall()
    # Derive change events by diffing successive goal rows. Each row
    # produces 0–3 events depending on which fields actually changed.
    goal_events: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    for r in goal_rows:
        row = {
            "north_star": r["goal_north_star"] or "",
            "version_goal": _decode_content(r["content"] or ""),
            "sprint": r["goal_sprint"] or "",
            "version": r["version"],
            "updated_at": r["updated_at"],
        }
        if prev is None:
            # Seed events for v1 of each field.
            for field in ("north_star", "version_goal", "sprint"):
                if row[field]:
                    goal_events.append({
                        "field": field,
                        "version": row["version"],
                        "updated_at": row["updated_at"],
                    })
        else:
            for field in ("north_star", "version_goal", "sprint"):
                if row[field] != prev[field]:
                    goal_events.append({
                        "field": field,
                        "version": row["version"],
                        "updated_at": row["updated_at"],
                    })
        prev = row

    # Per-day contribution counts for the heatmap calendar. Aggregated in
    # Python from task_rows so the math is identical on SQLite and Postgres
    # (no DATE()/array_agg dialect differences). Date key = first 10 chars
    # of the stored UTC timestamp ("YYYY-MM-DD HH:MM:SS" → "YYYY-MM-DD").
    by_day: dict[str, dict[str, Any]] = {}
    people_set: set[str] = set()
    clients_set: set[str] = set()
    for r in task_rows:
        ts = r["created_at"] or ""
        day = ts[:10]
        if len(day) != 10:
            continue
        human = r["human_id"] or "(unknown)"
        person = canonical_person(r["human_id"])
        client = r["client_type"] or "(none)"
        people_set.add(person)
        clients_set.add(client)
        bucket = by_day.get(day)
        if bucket is None:
            bucket = {"date": day, "count": 0, "humans": {}, "people": {}, "_sess": {}}
            by_day[day] = bucket
        bucket["count"] += 1
        bucket["humans"][human] = bucket["humans"].get(human, 0) + 1
        bucket["people"][person] = bucket["people"].get(person, 0) + 1
        sid = r["session_id"] or "(none)"
        se = bucket["_sess"].get(sid)
        if se is None:
            se = {
                "session_id": r["session_id"],
                "name": r["session_name"] or "(unknown)",
                "human": human,
                "person": person,
                "client": client,
                "count": 0,
            }
            bucket["_sess"][sid] = se
        se["count"] += 1
    daily_counts = []
    for day in sorted(by_day):
        b = by_day[day]
        day_sessions = sorted(
            b["_sess"].values(), key=lambda s: (-s["count"], s["name"])
        )
        daily_counts.append({
            "date": b["date"],
            "count": b["count"],
            "sessions": day_sessions,
            "session_count": len(day_sessions),
            "humans": b["humans"],
            "people": b["people"],
        })

    return {
        "tasks": tasks,
        "sessions": sessions,
        "goal_events": goal_events,
        "daily_counts": daily_counts,
        "people": sorted(people_set),
        "clients": sorted(clients_set),
    }


def build_sprint_items_xml(items: list[dict[str, Any]]) -> str:
    """Serialise sprint items as a ``<sprint_items>`` XML block.

    Since v1.9x items are optionally grouped by ``item_group``. When a
    group name is set, items are wrapped in ``<group name="...">`` tags
    so cold sessions can parse the board structure. Items without a
    group are emitted at the top level (ungrouped) before any groups.

    Mirrors the get_goal XML envelope (v0.6.1) so cold sessions render
    the checklist alongside the goal text in a single prompt.
    """
    from xml.sax.saxutils import escape, quoteattr
    from collections import OrderedDict

    # Preserve insertion order: ungrouped first, then named groups in
    # first-seen order.
    groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for it in items:
        g = it.get("item_group") or ""
        if g not in groups:
            groups[g] = []
        groups[g].append(it)

    out = ['<sprint_items cache="false">']
    for group_name, group_items in groups.items():
        if group_name:
            out.append(f'  <group name={quoteattr(group_name)}>')
        for it in group_items:
            ver = quoteattr(it.get("version") or "")
            status = quoteattr(it.get("status") or "todo")
            iid = quoteattr(it.get("id") or "")
            title = escape(it.get("title") or "")
            pushed_to = it.get("pushed_to")
            attrs = f"id={iid} version={ver} status={status}"
            if pushed_to:
                attrs += f" pushed_to={quoteattr(str(pushed_to))}"
            indent = "    " if group_name else "  "
            out.append(f"{indent}<item {attrs}>{title}</item>")
        if group_name:
            out.append("  </group>")
    out.append("</sprint_items>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Session auto-summary (v1.2.1)
# ---------------------------------------------------------------------------


SESSION_SUMMARY_SCHEMA: dict[str, Any] = {
    "name": "session_summary",
    "description": (
        "Structured retrospective for a Meridian session. Generated by "
        "claude-haiku at handoff / TTL expiry when the session shipped "
        ">=3 tasks. Stored verbatim on sessions.session_summary."
    ),
    "schema": {
        "type": "object",
        "required": [
            "session_type", "tasks_completed", "key_decisions", "summary",
        ],
        "properties": {
            "session_type": {
                "type": "string",
                "enum": ["human", "worker"],
            },
            "tasks_completed": {"type": "integer", "minimum": 0},
            "key_decisions": {
                "type": "array",
                "items": {"type": "string"},
            },
            "summary": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
    },
}


def _build_summary_prompt(
    session: dict[str, Any], tasks: list[dict[str, Any]]
) -> str:
    """Render the session's tasks into a one-shot prompt for haiku."""
    lines = [
        f"Session: {session.get('name')}",
        f"Type: {session.get('session_type') or 'human'}",
        f"Started: {session.get('created_at')}",
        f"Last seen: {session.get('last_seen')}",
        "",
        "Tasks (oldest first):",
    ]
    for t in tasks:
        status = (t.get("status") or "").upper()
        desc = (t.get("description") or "").replace("\n", " ")
        lines.append(f"- [{status}] {desc[:240]}")
    lines.extend([
        "",
        "Summarise this session as JSON matching the session_summary schema.",
    ])
    return "\n".join(lines)


async def _default_haiku_summarizer(prompt: str) -> dict[str, Any] | None:
    """Default summarizer: call claude-haiku via the Anthropic SDK
    with response_format pinned to ``SESSION_SUMMARY_SCHEMA``.

    Returns ``None`` when the SDK / API key are unavailable so the
    caller can degrade gracefully (no exception). Tests inject their
    own summarizer via the ``summarize_session(summarizer=...)`` arg.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return None
    try:
        client = AsyncAnthropic(api_key=api_key)
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
            response_format={
                "type": "json_schema",
                "json_schema": SESSION_SUMMARY_SCHEMA,
            },
        )
        text_blocks = [
            getattr(b, "text", "") for b in (resp.content or [])
        ]
        text = "".join(text_blocks).strip()
        if not text:
            return None
        return json.loads(text)
    except Exception:  # noqa: BLE001 — degrade gracefully
        return None


async def summarize_session(
    db: aiosqlite.Connection,
    session_id: str,
    *,
    min_tasks: int = 3,
    summarizer: Any = None,
) -> dict[str, Any] | None:
    """Generate (or refresh) the auto-summary for a session.

    Returns the structured summary dict that got stored, or ``None``
    when the session shipped fewer than ``min_tasks`` tasks (the
    summary cost isn't worth it for trivial sessions) or the
    summarizer failed.

    ``summarizer`` is an async callable ``(prompt: str) -> dict|None``.
    Defaults to the haiku-backed implementation; tests pass a stub
    that returns a fixed dict so the suite doesn't hit the network.
    """
    async with db.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ) as cur:
        sess_row = await cur.fetchone()
    if sess_row is None:
        return None
    session = _row_to_dict(sess_row)
    assert session is not None

    async with db.execute(
        "SELECT * FROM task_log WHERE session_id = ? "
        "ORDER BY created_at DESC, rowid DESC",
        (session_id,),
    ) as cur:
        task_rows = await cur.fetchall()
    tasks = [_row_to_dict(r) for r in task_rows]
    if len(tasks) < min_tasks:
        return None

    fn = summarizer or _default_haiku_summarizer
    prompt = _build_summary_prompt(session, tasks)
    summary = await fn(prompt)
    if not isinstance(summary, dict):
        return None
    # Defensive: ensure required keys are present + sane types.
    required = {"session_type", "tasks_completed", "key_decisions", "summary"}
    if not required.issubset(summary):
        return None
    if not isinstance(summary["summary"], str) or not summary["summary"].strip():
        return None

    await db.execute(
        "UPDATE sessions SET session_summary = ? WHERE id = ?",
        (json.dumps(summary), session_id),
    )
    await db.commit()
    return summary


async def auto_capture_session(
    db: aiosqlite.Connection, project_id: str, session_id: str
) -> None:
    """Rule-based session capture: bucket done tasks into Fixed/Added/Changed
    and save as an ephemeral *session* note (scratch pad). No-op for sessions
    with fewer than 2 done tasks so trivial sessions don't generate noise.

    9d44998b — this writes to session_notes, NOT project_notes. The permanent
    artifact of a session is its task_log entries; the bucketed summary is a
    transient convenience that expires with the session rather than polluting
    the project wiki with "Session summary (date)" notes.
    """
    from datetime import datetime, timezone

    async with db.execute(
        "SELECT description FROM task_log "
        "WHERE session_id = ? AND status = 'done' "
        "ORDER BY created_at DESC",
        (session_id,),
    ) as cur:
        rows = await cur.fetchall()
    tasks = [_row_to_dict(r) for r in rows]
    if len(tasks) < 2:
        return

    categories: dict[str, list[str]] = {
        "Fixed": [
            t["description"] for t in tasks
            if any(w in t["description"].lower() for w in ["fix", "bug", "error", "broken"])
        ],
        "Added": [
            t["description"] for t in tasks
            if any(w in t["description"].lower() for w in ["add", "feat", "new", "implement"])
        ],
        "Changed": [
            t["description"] for t in tasks
            if any(w in t["description"].lower() for w in ["update", "change", "refactor", "improve"])
        ],
    }
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"Auto-capture {date_str}"]
    for label, items in categories.items():
        if items:
            lines.append(f"\n{label}:")
            for desc in items[:5]:
                lines.append(f"  - {desc[:100]}")
    if len(lines) <= 1:
        return
    await add_session_note(
        db, session_id, f"Session summary ({date_str})", "\n".join(lines)
    )


# ---------------------------------------------------------------------------
# Rewind (v1.3.0) — "Last X days" shareable project summary.
# Aggregates versions shipped, goal changes, decisions logged, session
# summaries, sprint items completed, and task counts in one async call.
# ---------------------------------------------------------------------------


def _summarize(text: str | None, limit: int = 100) -> str:
    """Truncate a multi-line string to a one-line ``limit``-char summary."""
    if not text:
        return ""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


async def get_rewind_data(
    db: aiosqlite.Connection, project_id: str, days: int
) -> dict[str, Any]:
    """Aggregate the v1.3.0 rewind payload for a project over ``days`` days.

    Returns a dict matching the documented response shape. Empty arrays
    + zero counts (never errors) when the period has no activity. Caller
    is responsible for the project existence 404 — this function trusts
    its input.
    """
    from datetime import datetime, timedelta, timezone

    if days <= 0:
        days = 7
    now_dt = datetime.now(timezone.utc)
    cutoff = (now_dt - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    now_iso = now_dt.isoformat()

    # --- Task aggregates ---------------------------------------------------
    async with db.execute(
        "SELECT status, COUNT(*) AS n FROM task_log "
        "WHERE project_id = ? AND created_at >= ? "
        "GROUP BY status",
        (project_id, cutoff),
    ) as cur:
        status_rows = await cur.fetchall()
    tasks_by_status: dict[str, int] = {r["status"]: r["n"] for r in status_rows}
    tasks_total = sum(tasks_by_status.values())

    # --- Versions shipped (best-effort proxy) ------------------------------
    # See module docstring above for the rationale: scan task_log
    # descriptions for "shipped" / commit-style prefixes ("feat:", "fix:",
    # "v1.2.0 —") in the period. Cap at 20.
    async with db.execute(
        "SELECT description, created_at FROM task_log "
        "WHERE project_id = ? AND created_at >= ? "
        "AND ("
        "    description LIKE '% shipped%' "
        " OR description LIKE 'shipped %' "
        " OR description LIKE 'feat:%' "
        " OR description LIKE 'fix:%' "
        " OR description LIKE 'docs:%' "
        " OR description LIKE 'feat(%' "
        " OR description LIKE 'v_._._%' ESCAPE '_'"
        ") "
        "ORDER BY created_at DESC LIMIT 20",
        (project_id, cutoff),
    ) as cur:
        ver_rows = await cur.fetchall()
    versions_shipped: list[str] = []
    seen: set[str] = set()
    for r in ver_rows:
        desc = _summarize(r["description"], limit=160)
        if desc and desc not in seen:
            seen.add(desc)
            versions_shipped.append(desc)

    # --- Goal changes ------------------------------------------------------
    # Walk goal_states in version order and diff successive rows. Only
    # rows whose created_at is within the period are emitted, but the
    # comparison anchor is the prior row regardless of period so a
    # field changed on day 8 isn't double-emitted on day 7.
    async with db.execute(
        "SELECT version, goal_north_star, content, goal_sprint, created_at "
        "FROM goal_states WHERE project_id = ? ORDER BY version ASC",
        (project_id,),
    ) as cur:
        all_goal_rows = await cur.fetchall()
    goal_changes: list[dict[str, Any]] = []
    prev = {"north_star": "", "version_goal": "", "sprint": ""}
    for r in all_goal_rows:
        current = {
            "north_star": r["goal_north_star"] or "",
            "version_goal": r["content"] or "",
            "sprint": r["goal_sprint"] or "",
        }
        in_period = False
        try:
            ts = (r["created_at"] or "").replace(" ", "T")
            dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
            cutoff_dt = datetime.now(timezone.utc).timestamp() - days * 86400
            in_period = dt.timestamp() >= cutoff_dt
        except (ValueError, TypeError):
            in_period = False
        if in_period:
            for field_key, label in (
                ("north_star", "north_star"),
                ("version_goal", "version_goal"),
                ("sprint", "sprint"),
            ):
                if current[field_key] != prev[field_key]:
                    goal_changes.append({
                        "field": label,
                        "old_summary": _summarize(prev[field_key]),
                        "new_summary": _summarize(current[field_key]),
                        "old_full": prev[field_key],
                        "new_full": current[field_key],
                        "changed_at": r["created_at"],
                    })
        prev = current

    # --- Decisions logged --------------------------------------------------
    # v1.1.4 stores decisions as a single TEXT blob on projects with
    # ``[YYYY-MM-DD] ...`` entries newest-first. Parse + filter by date.
    decisions_logged: list[dict[str, Any]] = []
    async with db.execute(
        "SELECT decisions FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        drow = await cur.fetchone()
    decisions_blob = drow["decisions"] if drow else None
    if decisions_blob:
        import re as _re
        from datetime import date as _date, timedelta as _td
        cutoff_date = _date.today() - _td(days=days)
        # Each entry: ``[YYYY-MM-DD] text`` separated by blank lines.
        for chunk in _re.split(r"\n\s*\n", decisions_blob.strip()):
            m = _re.match(r"^\[(\d{4}-\d{2}-\d{2})\]\s*(.*)$", chunk.strip(), _re.S)
            if not m:
                continue
            try:
                logged_date = _date.fromisoformat(m.group(1))
            except ValueError:
                continue
            if logged_date < cutoff_date:
                continue
            decisions_logged.append({
                "text": _summarize(m.group(2), limit=240),
                "logged_at": m.group(1),
            })

    # --- Session summaries -------------------------------------------------
    async with db.execute(
        "SELECT id, name, session_summary FROM sessions "
        "WHERE project_id = ? AND created_at >= ? "
        "ORDER BY created_at DESC",
        (project_id, cutoff),
    ) as cur:
        sess_rows = await cur.fetchall()
    session_summaries: list[dict[str, Any]] = []
    for s in sess_rows:
        async with db.execute(
            "SELECT COUNT(*) AS cnt FROM task_log WHERE session_id = ? AND status = 'done'",
            (s["id"],),
        ) as cur:
            row = await cur.fetchone()
        completed = int(row["cnt"]) if row else 0
        summary_text: str = s["name"]
        if s["session_summary"]:
            try:
                parsed = json.loads(s["session_summary"])
                if isinstance(parsed, dict) and parsed.get("summary"):
                    summary_text = _summarize(parsed["summary"], limit=240)
            except (ValueError, TypeError):
                pass
        session_summaries.append({
            "session_name": s["name"],
            "summary": summary_text,
            "tasks_completed": completed,
        })

    # --- Sprint items completed -------------------------------------------
    async with db.execute(
        "SELECT version, title, completed_at, status, item_group FROM sprint_items "
        "WHERE project_id = ? AND status IN ('done','skipped','failed','pushed') "
        "AND completed_at IS NOT NULL "
        "AND completed_at >= ? "
        "ORDER BY completed_at DESC",
        (project_id, cutoff),
    ) as cur:
        sprint_rows = await cur.fetchall()
    sprint_items_completed = [
        {
            "version": r["version"],
            "title": r["title"],
            "completed_at": r["completed_at"],
            "status": r["status"],
        }
        for r in sprint_rows
    ]

    # All pending/todo sprint items regardless of time window (current sprint state)
    async with db.execute(
        "SELECT version, title, status, item_group, added_at FROM sprint_items "
        "WHERE project_id = ? AND status IN ('pending','todo','in_progress') "
        "ORDER BY added_at ASC",
        (project_id,),
    ) as cur:
        pending_rows = await cur.fetchall()
    sprint_items_pending = [
        {
            "version": r["version"],
            "title": r["title"],
            "status": r["status"],
            "item_group": r["item_group"],
        }
        for r in pending_rows
    ]

    return {
        "period_days": days,
        "generated_at": now_iso,
        "versions_shipped": versions_shipped,
        "goal_changes": goal_changes,
        "decisions_logged": decisions_logged,
        "session_summaries": session_summaries,
        "sprint_items_completed": sprint_items_completed,
        "sprint_items_pending": sprint_items_pending,
        "tasks_total": tasks_total,
        "tasks_by_status": tasks_by_status,
    }


def _strip_auto_blocks(content: str) -> str:
    """Return content with the AUTO BLOCKS section removed.

    Used to detect versions that only differ in the auto-generated
    section so they can be collapsed in the goal history view.
    """
    for marker in ("\n\n--- AUTO BLOCKS BELOW ---\n", "--- AUTO BLOCKS BELOW ---"):
        if marker in content:
            return content.split(marker)[0].rstrip()
    return content


async def get_goal_history(
    db: aiosqlite.Connection, project_id: str
) -> list[dict[str, Any]]:
    """Return meaningful goal versions for a project, newest first.

    Filters out versions where only the AUTO BLOCKS section changed —
    those are housekeeping updates and create noise in the history view.
    Each entry has: version, north_star, version_goal, sprint, created_at.
    """
    async with db.execute(
        "SELECT version, goal_north_star, content, goal_sprint, created_at "
        "FROM goal_states WHERE project_id = ? ORDER BY version DESC",
        (project_id,),
    ) as cur:
        rows = await cur.fetchall()

    filtered: list[dict[str, Any]] = []
    prev_key: tuple[str, str, str] | None = None
    for r in rows:
        stripped = _strip_auto_blocks(r["content"] or "")
        key = (r["goal_north_star"] or "", stripped, r["goal_sprint"] or "")
        if key != prev_key:
            filtered.append(
                {
                    "version": r["version"],
                    "north_star": r["goal_north_star"] or "",
                    "version_goal": r["content"] or "",
                    "sprint": r["goal_sprint"] or "",
                    "created_at": r["created_at"],
                }
            )
            prev_key = key
    return filtered


async def get_project_stats(
    db: aiosqlite.Connection, project_id: str, days: int = 30
) -> dict[str, Any]:
    """Return activity stats for the charts subtab.

    Returns tasks/day series (last ``days`` days) and sprint completion
    percentage per version. Data is sourced from task_log + sprint_items.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    # Tasks done per day per human_id (join sessions for human attribution)
    async with db.execute(
        "SELECT substr(t.created_at, 1, 10) AS day, s.human_id, COUNT(*) AS cnt "
        "FROM task_log t LEFT JOIN sessions s ON t.session_id = s.id "
        "WHERE t.project_id = ? AND t.status = 'done' "
        "AND t.created_at >= ? GROUP BY day, s.human_id ORDER BY day ASC",
        (project_id, cutoff),
    ) as cur:
        task_rows = await cur.fetchall()

    tasks_by_day: dict[str, dict[str, int]] = {}
    for r in task_rows:
        day = r["day"]
        human = r["human_id"] or "unknown"
        if day not in tasks_by_day:
            tasks_by_day[day] = {}
        tasks_by_day[day][human] = r["cnt"]

    # Build full date series so days with 0 tasks still appear
    all_days = [
        (now - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        for i in range(days)
    ]
    tasks_per_day = [
        {
            "day": d,
            "by_human": tasks_by_day.get(d, {}),
            "total": sum(tasks_by_day.get(d, {}).values()),
        }
        for d in all_days
    ]

    async with db.execute(
        "SELECT substr(completed_at, 1, 10) AS day, COUNT(*) AS cnt "
        "FROM sprint_items "
        "WHERE project_id = ? AND status = 'done' "
        "AND completed_at IS NOT NULL AND completed_at >= ? "
        "GROUP BY day ORDER BY day ASC",
        (project_id, cutoff),
    ) as cur:
        sprint_day_rows = await cur.fetchall()
    sprint_by_day = {r["day"]: r["cnt"] for r in sprint_day_rows}
    sprint_items_per_day = [
        {"day": d, "total": sprint_by_day.get(d, 0)}
        for d in all_days
    ]

    # Sprint completion % per version
    async with db.execute(
        "SELECT version, status, COUNT(*) AS cnt FROM sprint_items "
        "WHERE project_id = ? GROUP BY version, status",
        (project_id,),
    ) as cur:
        sprint_rows = await cur.fetchall()

    sprint_by_version: dict[str, dict[str, int]] = {}
    for r in sprint_rows:
        v = r["version"]
        if v not in sprint_by_version:
            sprint_by_version[v] = {"done": 0, "total": 0}
        sprint_by_version[v]["total"] += r["cnt"]
        if r["status"] in ("done", "skipped"):
            sprint_by_version[v]["done"] += r["cnt"]

    sprint_velocity = [
        {
            "version": v,
            "done": d["done"],
            "total": d["total"],
            "pct": round(d["done"] / d["total"] * 100) if d["total"] else 0,
        }
        for v, d in sorted(sprint_by_version.items())
    ]

    return {
        "period_days": days,
        "tasks_per_day": tasks_per_day,
        "sprint_items_per_day": sprint_items_per_day,
        "sprint_velocity": sprint_velocity,
    }


async def get_or_create_rewind_token(
    db: aiosqlite.Connection, project_id: str
) -> str:
    """Return the project's rewind_token, minting a uuid4 if missing.

    Idempotent: a project keeps the same token for life so previously
    distributed share-links don't break on the next POST. Returns the
    final token regardless of whether it was just created.
    """
    async with db.execute(
        "SELECT rewind_token FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise ValueError(f"unknown project: {project_id}")
    existing = row["rewind_token"]
    if existing:
        return existing
    token = _new_id()
    await db.execute(
        "UPDATE projects SET rewind_token = ? WHERE id = ?",
        (token, project_id),
    )
    await db.commit()
    return token


async def get_rewind_token(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the stored rewind_token, or None if not yet minted."""
    async with db.execute(
        "SELECT rewind_token FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return row["rewind_token"]


# ---------------------------------------------------------------------------
# Waitlist
# ---------------------------------------------------------------------------


async def add_waitlist_entry(
    db: aiosqlite.Connection,
    email: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Add an email to the waitlist. Returns the entry. Raises on duplicate."""
    entry_id = _new_id()
    await db.execute(
        "INSERT INTO waitlist (id, email, note) VALUES (?, ?, ?)",
        (entry_id, email.strip().lower(), note),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM waitlist WHERE id = ?", (entry_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_waitlist(
    db: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    """Return all waitlist entries, newest first."""
    async with db.execute(
        "SELECT * FROM waitlist ORDER BY created_at DESC"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# v2.0 — Hosted tier: tenants, user sessions, API tokens
# ---------------------------------------------------------------------------

async def upsert_tenant(
    db: aiosqlite.Connection,
    email: str,
    google_sub: str | None = None,
    github_sub: str | None = None,
    microsoft_sub: str | None = None,
) -> dict[str, Any]:
    """Create or update a tenant record by email.

    Updates provider sub IDs when provided. Returns the tenant dict.
    """
    email = email.strip().lower()
    async with db.execute("SELECT * FROM tenants WHERE email = ?", (email,)) as cur:
        row = await cur.fetchone()
    if row:
        tenant = _row_to_dict(row)
        updates: list[tuple[str, str]] = []
        if google_sub and tenant.get("google_sub") != google_sub:
            updates.append(("google_sub", google_sub))
        if github_sub and tenant.get("github_sub") != github_sub:
            updates.append(("github_sub", github_sub))
        if microsoft_sub and tenant.get("microsoft_sub") != microsoft_sub:
            updates.append(("microsoft_sub", microsoft_sub))
        for col, val in updates:
            await db.execute(
                f"UPDATE tenants SET {col} = ? WHERE id = ?",  # noqa: S608
                (val, tenant["id"]),
            )
            tenant[col] = val
        if updates:
            await db.commit()
        return tenant
    tid = _new_id()
    await db.execute(
        "INSERT INTO tenants (id, email, google_sub, github_sub, microsoft_sub, "
        "plan, notification_prefs) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tid, email, google_sub, github_sub, microsoft_sub, "free",
         '{"storage":true,"sprint":true}'),
    )
    await db.commit()
    async with db.execute("SELECT * FROM tenants WHERE id = ?", (tid,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_tenant_by_id(
    db: aiosqlite.Connection,
    tenant_id: str,
) -> dict[str, Any] | None:
    """Return tenant by primary key, or None."""
    async with db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def update_tenant(
    db: aiosqlite.Connection,
    tenant_id: str,
    **fields: object,
) -> dict[str, Any] | None:
    """Update arbitrary columns on a tenant row. Returns updated dict or None."""
    allowed = {
        "neon_project_id", "neon_db_url", "stripe_customer_id", "plan", "pool_project_id",
        "stripe_metered_item_id", "notification_prefs",
        "payment_failed_at", "dunning_email_sent",
        "compute_overage_cap_usd", "storage_overage_cap_usd",
        "compute_cu_hours_used", "storage_gb_used",
        "overage_reset_at", "compute_throttled_at",
        "trial_started_at", "inactivity_expires_at",
        "github_pat", "tunnel_active", "tunnel_plugins", "tunnel_plugins_by_host",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return await get_tenant_by_id(db, tenant_id)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    await db.execute(
        f"UPDATE tenants SET {set_clause} WHERE id = ?",
        (*updates.values(), tenant_id),
    )
    await db.commit()
    return await get_tenant_by_id(db, tenant_id)


async def create_user_session(
    db: aiosqlite.Connection,
    tenant_id: str,
    expires_at: str,
    user_agent: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    """Create a web session for a tenant. Returns the session dict.

    3c28450d — optional device metadata (``user_agent``/``ip``) is stored for
    the active-sessions view; ``last_seen_at`` is seeded to now."""
    sid = _new_id()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO user_sessions "
        "(id, tenant_id, expires_at, user_agent, ip, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sid, tenant_id, expires_at,
         (user_agent or None), (ip or None), now),
    )
    await db.commit()
    async with db.execute("SELECT * FROM user_sessions WHERE id = ?", (sid,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_user_session(
    db: aiosqlite.Connection,
    session_id: str,
) -> dict[str, Any] | None:
    """Return a user_session row, or None if missing or expired."""
    async with db.execute(
        "SELECT * FROM user_sessions WHERE id = ?", (session_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    s = _row_to_dict(row)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    if s["expires_at"] < now:
        await delete_user_session(db, session_id)
        return None
    return s


async def delete_user_session(
    db: aiosqlite.Connection,
    session_id: str,
) -> None:
    """Delete a web session (logout)."""
    await db.execute("DELETE FROM user_sessions WHERE id = ?", (session_id,))
    await db.commit()


async def get_user_sessions_for_tenant(
    db: aiosqlite.Connection, tenant_id: str
) -> list[dict[str, Any]]:
    """3c28450d — all non-expired web sessions for a tenant, most-recently-seen
    first, for the active-sessions view."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    async with db.execute(
        "SELECT * FROM user_sessions WHERE tenant_id = ? AND expires_at > ? "
        "ORDER BY COALESCE(last_seen_at, created_at) DESC",
        (tenant_id, now),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def touch_user_session(
    db: aiosqlite.Connection, session_id: str
) -> None:
    """3c28450d — bump last_seen_at so the active-sessions view shows recency."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE user_sessions SET last_seen_at = ? WHERE id = ?", (now, session_id)
    )
    await db.commit()


async def revoke_user_session(
    db: aiosqlite.Connection, session_id: str, tenant_id: str
) -> bool:
    """3c28450d — delete a session only if it belongs to ``tenant_id`` (so a user
    can never revoke another tenant's session). Returns True if a row was
    removed."""
    cur = await db.execute(
        "DELETE FROM user_sessions WHERE id = ? AND tenant_id = ?",
        (session_id, tenant_id),
    )
    await db.commit()
    return (cur.rowcount or 0) > 0


async def enqueue_provision(
    db: aiosqlite.Connection, tenant_id: str, last_error: str | None = None
) -> None:
    """4c559d4e — record a tenant needing (re)provisioning. Bumps attempts and
    resets status to 'pending' so a background drain can retry it later."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with db.execute(
        "SELECT attempts FROM provision_queue WHERE tenant_id = ?", (tenant_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        await db.execute(
            "INSERT INTO provision_queue "
            "(tenant_id, status, attempts, last_error, updated_at) "
            "VALUES (?, 'pending', 1, ?, ?)",
            (tenant_id, last_error, now),
        )
    else:
        await db.execute(
            "UPDATE provision_queue SET status = 'pending', attempts = attempts + 1, "
            "last_error = ?, updated_at = ? WHERE tenant_id = ?",
            (last_error, now, tenant_id),
        )
    await db.commit()


async def mark_provision_done(db: aiosqlite.Connection, tenant_id: str) -> None:
    """4c559d4e — mark a tenant's provisioning complete (clears it from pending)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE provision_queue SET status = 'done', updated_at = ? WHERE tenant_id = ?",
        (now, tenant_id),
    )
    await db.commit()


async def get_pending_provisions(
    db: aiosqlite.Connection, limit: int = 50
) -> list[dict[str, Any]]:
    """4c559d4e — tenants still awaiting provisioning, oldest first."""
    async with db.execute(
        "SELECT * FROM provision_queue WHERE status = 'pending' "
        "ORDER BY created_at ASC LIMIT ?",
        (int(limit),),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def count_pending_provisions(db: aiosqlite.Connection) -> int:
    """4c559d4e — number of tenants awaiting (re)provisioning."""
    async with db.execute(
        "SELECT COUNT(*) FROM provision_queue WHERE status = 'pending'"
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def upsert_graph_entities(
    db: aiosqlite.Connection,
    project_id: str,
    entities: list[dict[str, Any]],
    cap: int = 500,
) -> int:
    """c00b1ccf — replace a project's cached graph snapshot with up to ``cap``
    entities (each: qualified_name + optional file/kind/signature). Returns the
    number stored. Entities beyond the cap are dropped."""
    cap = max(0, int(cap))
    await db.execute(
        "DELETE FROM codebase_graph_entities WHERE project_id = ?", (project_id,)
    )
    stored = 0
    for ent in (entities or [])[:cap]:
        qn = str(ent.get("qualified_name") or ent.get("name") or "").strip()
        if not qn:
            continue
        await db.execute(
            "INSERT INTO codebase_graph_entities "
            "(id, project_id, qualified_name, file, kind, signature) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_new_id(), project_id, qn,
             (ent.get("file") or None), (ent.get("kind") or None),
             (ent.get("signature") or None)),
        )
        stored += 1
    await db.commit()
    return stored


async def count_graph_entities(db: aiosqlite.Connection, project_id: str) -> int:
    """c00b1ccf — number of cached graph entities for a project."""
    async with db.execute(
        "SELECT COUNT(*) FROM codebase_graph_entities WHERE project_id = ?",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def search_graph_entities(
    db: aiosqlite.Connection, project_id: str, query: str, limit: int = 10
) -> list[dict[str, Any]]:
    """c00b1ccf — keyword search over a project's cached graph snapshot. Matches
    tokens (>=3 chars) of ``query`` against qualified_name/file. Returns entity
    rows (each carrying ``file``) for handoff code-pointer enrichment."""
    import re as _re
    tokens = [t for t in _re.split(r"[^A-Za-z0-9_]+", (query or "")) if len(t) >= 3]
    if not tokens:
        return []
    clauses: list[str] = []
    params: list[Any] = [project_id]
    for tok in tokens[:6]:
        clauses.append("(qualified_name LIKE ? OR file LIKE ?)")
        like = f"%{tok}%"
        params.extend([like, like])
    sql = (
        "SELECT * FROM codebase_graph_entities WHERE project_id = ? AND ("
        + " OR ".join(clauses)
        + ") LIMIT ?"
    )
    params.append(int(limit))
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def delete_api_tokens_by_label(
    db: aiosqlite.Connection,
    tenant_id: str,
    label: str,
) -> int:
    """Delete all tokens with a given label for a tenant. Returns count deleted.
    Used so label acts as a unique slot -- regenerating hooks-installer token
    doesn't leave stale tokens that cause 401 loops."""
    cur = await db.execute(
        "DELETE FROM api_tokens WHERE tenant_id = ? AND label = ?",
        (tenant_id, label),
    )
    await db.commit()
    return cur.rowcount


async def delete_orphaned_oauth_tokens(
    db: aiosqlite.Connection,
    tenant_id: str,
    *,
    label: str = "oauth",
    older_than_hours: int = 24,
) -> int:
    """Purge a tenant's stale OAuth-minted API tokens. Returns count deleted.

    The Claude Code MCP ``authorization_code`` flow mints an ``oauth``-labelled
    ``api_tokens`` row at ``/oauth/token`` even when the redirect back to the
    local callback fails; the user then retries, orphaning the previous token.
    Left alone these accumulate as dead key-list entries. This deletes rows
    matching ``label`` whose ``created_at`` is older than ``older_than_hours``,
    leaving recent (possibly in-use) tokens untouched.

    ``created_at`` is the canonical ``YYYY-MM-DD HH:MM:SS`` UTC form, so a
    lexicographic comparison against the Python-computed cutoff is correct on
    both SQLite and Postgres.
    """
    from datetime import datetime, timezone, timedelta
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=max(0, older_than_hours))
    ).strftime("%Y-%m-%d %H:%M:%S")
    cur = await db.execute(
        "DELETE FROM api_tokens WHERE tenant_id = ? AND label = ? AND created_at < ?",
        (tenant_id, label, cutoff),
    )
    await db.commit()
    return cur.rowcount


async def create_api_token(
    db: aiosqlite.Connection,
    tenant_id: str,
    label: str | None = None,
    token_type: str = "readwrite",
    expires_at: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Generate a bearer token for a tenant.

    Returns ``(raw_token, token_row_dict)``.  The raw token is shown once
    and never stored.  Only the SHA-256 hash is persisted.
    ``token_type`` is 'readwrite' (default) or 'readonly'.
    ``expires_at`` is an ISO 8601 datetime string for short-lived tokens (e.g. install tokens).
    """
    if token_type not in ("readwrite", "readonly"):
        token_type = "readwrite"
    import secrets
    import hashlib
    raw = f"sk_meridian_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    tid = _new_id()
    await db.execute(
        "INSERT INTO api_tokens (id, tenant_id, token_hash, label, token_type, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (tid, tenant_id, token_hash, label, token_type, expires_at),
    )
    await db.commit()
    async with db.execute("SELECT * FROM api_tokens WHERE id = ?", (tid,)) as cur:
        row = await cur.fetchone()
    return raw, _row_to_dict(row)


async def list_api_tokens(
    db: aiosqlite.Connection,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """Return API token rows for a tenant, newest first."""
    async with db.execute(
        "SELECT id, label, token_hash, token_type, created_at FROM api_tokens "
        "WHERE tenant_id = ? ORDER BY created_at DESC, id DESC",
        (tenant_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]


async def delete_api_token(
    db: aiosqlite.Connection,
    tenant_id: str,
    token_id: str,
) -> bool:
    """Delete one API token for a tenant. Returns True when a row was removed."""
    cur = await db.execute(
        "DELETE FROM api_tokens WHERE tenant_id = ? AND id = ?",
        (tenant_id, token_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def get_tenant_from_token_hash(
    db: aiosqlite.Connection,
    token_hash: str,
) -> dict[str, Any] | None:
    """Look up a tenant by a pre-hashed API token.

    Returns tenant dict with an extra ``_token_type`` field ('readwrite' or
    'readonly') so callers can enforce read-only restrictions without a second
    query. Returns None for expired tokens (and deletes them).
    """
    from datetime import datetime, timezone
    async with db.execute(
        "SELECT t.*, a.token_type AS _token_type, a.id AS _token_id, a.expires_at AS _token_expires_at "
        "FROM tenants t "
        "JOIN api_tokens a ON a.tenant_id = t.id "
        "WHERE a.token_hash = ?",
        (token_hash,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    d = _row_to_dict(row)
    expires_at = d.pop("_token_expires_at", None)
    token_id = d.pop("_token_id", None)
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if exp_dt < datetime.now(timezone.utc):
                if token_id:
                    await db.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
                    await db.commit()
                return None
        except Exception:
            pass
    return d


async def count_tenants_by_plan(
    db: aiosqlite.Connection,
    plan: str,
    *,
    provisioned_only: bool = False,
) -> int:
    """Count tenant rows for a plan, optionally limiting to provisioned rows."""
    query = "SELECT COUNT(*) AS n FROM tenants WHERE plan = ?"
    if provisioned_only:
        query += " AND (neon_project_id IS NOT NULL OR neon_db_url IS NOT NULL)"
    async with db.execute(query, (plan,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return 0
    if isinstance(row, dict):
        return int(row.get("n", 0) or 0)
    keys = row.keys() if hasattr(row, "keys") else []
    if keys:
        return int(row["n"] or 0)
    return int(row[0] or 0)


# ---------------------------------------------------------------------------
# v2.2 — Neon pool project management
# ---------------------------------------------------------------------------

async def get_available_pool_project(
    db: aiosqlite.Connection,
    tier: str = "standard",
    max_customers: int = 8,
) -> dict[str, Any] | None:
    """Return a pool project for the given tier that has room for more customers.

    Returns the pool project dict, or None if all projects are full (caller
    should create a new Neon project and register it via register_pool_project).
    """
    async with db.execute(
        "SELECT * FROM neon_pool_projects "
        "WHERE tier = ? AND customer_count < ? "
        "ORDER BY customer_count DESC LIMIT 1",
        (tier, max_customers),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def register_pool_project(
    db: aiosqlite.Connection,
    neon_project_id: str,
    tier: str = "standard",
) -> dict[str, Any]:
    """Register a newly created Neon project as an available pool project."""
    import uuid
    pid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO neon_pool_projects (id, neon_project_id, tier, customer_count) "
        "VALUES (?, ?, ?, 0)",
        (pid, neon_project_id, tier),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM neon_pool_projects WHERE id = ?", (pid,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def increment_pool_project_count(
    db: aiosqlite.Connection,
    neon_project_id: str,
) -> None:
    """Increment customer_count on a pool project after assigning a new customer.

    Prefer :func:`claim_pool_project_slot` for race-free atomic claim+increment.
    This helper is kept for backfill / fix-up scripts that aren't on the
    signup hot path.
    """
    await db.execute(
        "UPDATE neon_pool_projects SET customer_count = customer_count + 1 "
        "WHERE neon_project_id = ?",
        (neon_project_id,),
    )
    await db.commit()


async def claim_pool_project_slot(
    db: aiosqlite.Connection,
    tier: str = "standard",
    max_customers: int = 8,
) -> dict[str, Any] | None:
    """Item 38 — atomically reserve a slot in an available pool project.

    Replaces the read/then/write race in ``get_available_pool_project`` +
    ``increment_pool_project_count``: two concurrent signups previously could
    both see the same pool with 7/8 slots, both proceed, both increment, and
    leave the pool at 9/8 — overprovisioned past the soft cap.

    The single UPDATE-with-subquery is atomic on SQLite (statement-level) and
    safe on Postgres MVCC because the *outer* ``customer_count < ?`` re-check
    runs against the locked row, so the second concurrent UPDATE harmlessly
    no-ops once T1 has bumped the count to the cap.

    Returns the updated pool project row (with the post-increment count), or
    ``None`` if no pool has room — caller should create a new pool project
    and try again.
    """
    async with db.execute(
        "UPDATE neon_pool_projects "
        "SET customer_count = customer_count + 1 "
        "WHERE id = ("
        "  SELECT id FROM neon_pool_projects "
        "  WHERE tier = ? AND customer_count < ? "
        "  ORDER BY customer_count DESC LIMIT 1"
        ") "
        "AND customer_count < ? "
        "RETURNING *",
        (tier, max_customers, max_customers),
    ) as cur:
        row = await cur.fetchone()
    await db.commit()
    return _row_to_dict(row) if row else None


async def decrement_pool_project_count(
    db: aiosqlite.Connection,
    neon_project_id: str,
) -> None:
    """Decrement customer_count after removing a customer database."""
    await db.execute(
        "UPDATE neon_pool_projects "
        "SET customer_count = CASE "
        "WHEN customer_count > 0 THEN customer_count - 1 ELSE 0 END "
        "WHERE neon_project_id = ?",
        (neon_project_id,),
    )
    await db.commit()


async def get_pool_project_counts(
    db: aiosqlite.Connection,
    tier: str = "standard",
) -> dict[str, int]:
    """Return total project count and total customer count for a tier."""
    async with db.execute(
        "SELECT COUNT(*) as projects, COALESCE(SUM(customer_count),0) as customers "
        "FROM neon_pool_projects WHERE tier = ?",
        (tier,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return {"projects": 0, "customers": 0}
    if isinstance(row, dict):
        return {"projects": row.get("projects", 0), "customers": row.get("customers", 0)}
    keys = row.keys() if hasattr(row, "keys") else []
    if keys:
        return {"projects": row["projects"], "customers": row["customers"]}
    return {"projects": row[0] or 0, "customers": row[1] or 0}


# ---------------------------------------------------------------------------
# v3.1 — file lock coordination
# ---------------------------------------------------------------------------

_FILE_LOCK_TTL_HOURS = 2

# 39544099 — shared staleness constant so file_locks and file_symbol_claims use the
# same TTL. Both mechanisms now expire via heartbeat (session.last_seen > TTL) in
# addition to the explicit expires_at column.
_CLAIM_LIVE_HOURS = _FILE_LOCK_TTL_HOURS


def _cutoff_dt(hours: int) -> str:
    """Return an ISO-8601 datetime string ``hours`` ago (UTC).

    Used as the shared staleness cutoff for file_locks and file_symbol_claims.
    """
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_file_path(file_path: str | None) -> str:
    """Normalize a file path the same way ``claim_file`` stores it.

    claim_file stores ``(file_path or "").strip()`` verbatim — no separator
    rewriting — so code-anchored notes (771c00d7) must apply the *identical*
    rule for their ``file_path`` anchor to match a claim. Centralized here so
    the anchor and the lock can never drift apart.
    """
    return (file_path or "").strip()


async def get_code_notes_for_file(
    db: aiosqlite.Connection,
    project_id: str,
    file_path: str,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    """771c00d7 — code-anchored notes for a file (newest first).

    Returns full project_notes rows where ``note_kind='code'`` and ``file_path``
    matches (after the same normalization ``claim_file`` applies) for the given
    project. Symbol scoping:

    * file-level anchors (note ``symbol`` is NULL/empty) always surface — they
      apply to any symbol in the file;
    * symbol anchors surface only when ``symbol`` is given and matches the
      note's ``symbol``.

    So ``symbol=None`` returns file-level anchors only; passing a ``symbol``
    additionally returns anchors pinned to that symbol. Returns ``[]`` for a
    blank path so callers can pass an un-anchored claim through unchanged.
    """
    normalized = _normalize_file_path(file_path)
    if not normalized or not project_id:
        return []
    sym = (symbol or "").strip()
    if sym:
        clause = "AND (symbol IS NULL OR symbol = '' OR symbol = ?)"
        params: list[Any] = [project_id, normalized, sym]
    else:
        clause = "AND (symbol IS NULL OR symbol = '')"
        params = [project_id, normalized]
    async with db.execute(
        "SELECT * FROM project_notes "
        "WHERE project_id = ? AND note_kind = 'code' AND file_path = ? "
        f"{clause} ORDER BY created_at DESC",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def _code_notes_for_session_file(
    db: aiosqlite.Connection,
    session_id: str,
    file_path: str,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve a session's project, then return its code-anchored notes for a file.

    ``claim_file`` is keyed by session_id (not project_id), so we look up the
    owning project here before delegating to :func:`get_code_notes_for_file`.
    Best-effort: an unknown session id yields ``[]`` rather than raising, so the
    file-lock path is never broken by the additive code-notes surface.
    """
    async with db.execute(
        "SELECT project_id FROM sessions WHERE id = ?", (session_id,)
    ) as cur:
        row = await cur.fetchone()
    sess = _row_to_dict(row)
    if not sess or not sess.get("project_id"):
        return []
    return await get_code_notes_for_file(
        db, sess["project_id"], file_path, symbol
    )


async def _decision_notes_for_session_file(
    db: aiosqlite.Connection,
    session_id: str,
    file_path: str,
) -> list[dict[str, Any]]:
    """Return code-anchored decisions for the file, resolved via session's project.

    777f26b0 — companion to ``_code_notes_for_session_file``: fetches active
    decisions whose ``code_anchor`` matches ``file_path`` so they can be injected
    into the ``claim_file`` response as ``decision_notes``. Best-effort: unknown
    session ids or pre-migration DBs yield ``[]`` rather than raising.
    """
    async with db.execute(
        "SELECT project_id FROM sessions WHERE id = ?", (session_id,)
    ) as cur:
        row = await cur.fetchone()
    sess = _row_to_dict(row)
    if not sess or not sess.get("project_id"):
        return []
    return await get_decisions_for_file(db, sess["project_id"], file_path)


async def expire_file_locks(db: aiosqlite.Connection) -> int:
    """Delete expired file locks and return how many rows were cleared.

    39544099 — two expiry paths (unified with file_symbol_claims):
    1. Explicit TTL: expires_at column <= now (original).
    2. Heartbeat: owning session's last_seen is older than _CLAIM_LIVE_HOURS
       (handles crashed/orphaned sessions whose lock was never explicitly released).
    """
    stale_cutoff = _cutoff_dt(_CLAIM_LIVE_HOURS)
    cursor = await db.execute(
        "DELETE FROM file_locks WHERE expires_at <= datetime('now') "
        "OR session_id IN ("
        "    SELECT id FROM sessions "
        "    WHERE last_seen IS NOT NULL AND last_seen < ?"
        ")",
        (stale_cutoff,),
    )
    await db.commit()
    return cursor.rowcount


async def expire_stale_symbol_claims(db: aiosqlite.Connection) -> int:
    """Soft-release symbol claims whose owning session's heartbeat has gone stale.

    39544099 — parallel to expire_file_locks but for file_symbol_claims. Uses the
    same _CLAIM_LIVE_HOURS cutoff so both expiry mechanisms share one constant.
    Marks claims as released (sets released_at) rather than deleting so the hotspot
    history (session_count aggregation) is preserved.
    """
    stale_cutoff = _cutoff_dt(_CLAIM_LIVE_HOURS)
    cursor = await db.execute(
        "UPDATE file_symbol_claims SET released_at = datetime('now') "
        "WHERE released_at IS NULL "
        "AND session_id IN ("
        "    SELECT id FROM sessions "
        "    WHERE last_seen IS NOT NULL AND last_seen < ?"
        ")",
        (stale_cutoff,),
    )
    await db.commit()
    return cursor.rowcount


async def claim_file(
    db: aiosqlite.Connection,
    file_path: str,
    session_id: str,
    *,
    symbol: str | None = None,
    ttl_hours: int = _FILE_LOCK_TTL_HOURS,
    mode: str = "write",
) -> dict[str, Any]:
    """Claim a file path for a session, auto-releasing expired locks first.

    771c00d7 — the returned dict carries a ``code_notes`` list: project notes
    anchored to this file path (note_kind='code'), so the executor sees relevant
    warnings/context before editing. When ``symbol`` is given, symbol-scoped
    anchors for that symbol are preferred but file-level anchors (no symbol)
    are always included. Empty when none. Additive — existing callers are
    unaffected.

    ffa03655 — ``mode`` selects the claim grain. ``write`` (default, legacy) is
    an EXCLUSIVE lock: it blocks other writers and is itself blocked by any other
    session's live read claim ("no lock on an open door" for reads, exclusion for
    writes). ``read`` is a SHARED claim: many sessions can hold a read claim on
    the same file concurrently (zero false contention for parallel reader agents),
    blocked only by another session's exclusive write lock.
    """
    normalized = _normalize_file_path(file_path)
    if not normalized:
        raise ValueError("file_path is required")
    await expire_file_locks(db)
    await expire_file_read_claims(db)
    _mode = "read" if str(mode or "write").lower() == "read" else "write"
    if _mode == "read":
        return await _claim_file_read(db, normalized, session_id, ttl_hours, symbol)
    # ffa03655 — exclusive write waits for readers: another session's live read
    # claim blocks a write claim.
    _readers = await _other_read_claims(db, normalized, session_id)
    if _readers:
        return {
            "claimed": False,
            "reason": "read_locked",
            "claim_mode": "write",
            "file_path": normalized,
            "session_id": session_id,
            "read_claims": [r.get("session_id") for r in _readers],
            "message": (
                f"Cannot write-claim {normalized}: {len(_readers)} reader(s) hold a "
                "shared read claim. Wait for readers to release, or read-claim instead."
            ),
        }
    # 63b030a6 — file ⊃ symbol hierarchy: a whole-file lock conflicts with any
    # live symbol claim on the file held by another session. Block here so the
    # coarser grain can't silently stomp a finer one.
    _other_symbols = await _live_symbol_claims_for_file(db, normalized, session_id)
    if _other_symbols:
        _holder = _other_symbols[0]
        return {
            "claimed": False,
            "reason": "symbol_locked",
            "file_path": normalized,
            "session_id": session_id,
            "holder_session_id": _holder.get("session_id"),
            "symbol_claims": _other_symbols,
            "message": (
                f"Cannot whole-file claim {normalized}: "
                f"{len(_other_symbols)} symbol(s) on it are claimed by another live "
                "session. Claim a specific free symbol with claim_symbol, or wait."
            ),
        }
    async with db.execute(
        "SELECT * FROM file_locks WHERE file_path = ?",
        (normalized,),
    ) as cur:
        existing_row = await cur.fetchone()
    existing = _row_to_dict(existing_row)
    if existing and existing.get("session_id") != session_id:
        return {
            "claimed": False,
            "file_path": normalized,
            "session_id": session_id,
            "holder_session_id": existing.get("session_id"),
            "claimed_at": existing.get("claimed_at"),
            "expires_at": existing.get("expires_at"),
            "code_notes": await _code_notes_for_session_file(
                db, session_id, normalized, symbol
            ),
            "decision_notes": await _decision_notes_for_session_file(
                db, session_id, normalized
            ),
        }

    if existing and existing.get("session_id") == session_id:
        await db.execute(
            "UPDATE file_locks SET claimed_at = datetime('now'), "
            "expires_at = datetime('now', ? || ' hours') "
            "WHERE id = ?",
            (str(ttl_hours), existing["id"]),
        )
    else:
        # Atomic INSERT — ON CONFLICT DO NOTHING races another concurrent INSERT
        # on the same file_path. Re-select to check who won. Safe on both SQLite
        # (single-writer) and Postgres (UNIQUE constraint is atomic).
        await db.execute(
            "INSERT INTO file_locks (id, file_path, session_id, claimed_at, expires_at) "
            "VALUES (?, ?, ?, datetime('now'), datetime('now', ? || ' hours')) "
            "ON CONFLICT (file_path) DO NOTHING",
            (_new_id(), normalized, session_id, str(ttl_hours)),
        )
    await db.commit()
    async with db.execute(
        "SELECT * FROM file_locks WHERE file_path = ?",
        (normalized,),
    ) as cur:
        row = await cur.fetchone()
    lock = _row_to_dict(row) or {}
    # b033c10f — re-check after INSERT to detect concurrent claim by another session.
    # The ON CONFLICT DO NOTHING is a no-op when another session raced us; the
    # re-SELECT reveals the actual winner. UPDATE path is already idempotent.
    if lock.get("session_id") != session_id:
        return {
            "claimed": False,
            "file_path": normalized,
            "session_id": session_id,
            "holder_session_id": lock.get("session_id"),
            "claimed_at": lock.get("claimed_at"),
            "expires_at": lock.get("expires_at"),
            "code_notes": await _code_notes_for_session_file(
                db, session_id, normalized, symbol
            ),
            "decision_notes": await _decision_notes_for_session_file(
                db, session_id, normalized
            ),
        }
    return {
        "claimed": True,
        "claim_mode": "write",
        "file_path": normalized,
        "session_id": lock.get("session_id"),
        "claimed_at": lock.get("claimed_at"),
        "expires_at": lock.get("expires_at"),
        "code_notes": await _code_notes_for_session_file(
            db, session_id, normalized, symbol
        ),
        # 777f26b0 — decisions with code_anchor matching this file path.
        "decision_notes": await _decision_notes_for_session_file(
            db, session_id, normalized
        ),
    }


async def expire_file_read_claims(db: aiosqlite.Connection) -> None:
    """ffa03655 — drop read claims whose TTL has lapsed (mirrors expire_file_locks)."""
    await db.execute(
        "DELETE FROM file_read_claims WHERE expires_at < datetime('now')"
    )
    await db.commit()


async def _other_read_claims(
    db: aiosqlite.Connection, file_path: str, session_id: str
) -> list[dict[str, Any]]:
    """Live read claims on ``file_path`` held by sessions other than this one."""
    async with db.execute(
        "SELECT * FROM file_read_claims WHERE file_path = ? AND session_id != ?",
        (file_path, session_id),
    ) as cur:
        return [_row_to_dict(r) for r in await cur.fetchall()]


async def _all_read_claims(
    db: aiosqlite.Connection, file_path: str
) -> list[dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM file_read_claims WHERE file_path = ?", (file_path,)
    ) as cur:
        return [_row_to_dict(r) for r in await cur.fetchall()]


async def _claim_file_read(
    db: aiosqlite.Connection,
    normalized: str,
    session_id: str,
    ttl_hours: int,
    symbol: str | None,
) -> dict[str, Any]:
    """ffa03655 — acquire (or refresh) a SHARED read claim on ``normalized``.

    Blocked only by another session's exclusive write lock; multiple sessions may
    hold a read claim on the same file at once.
    """
    async with db.execute(
        "SELECT * FROM file_locks WHERE file_path = ?", (normalized,)
    ) as cur:
        wrow = _row_to_dict(await cur.fetchone())
    if wrow and wrow.get("session_id") != session_id:
        return {
            "claimed": False,
            "reason": "write_locked",
            "claim_mode": "read",
            "file_path": normalized,
            "session_id": session_id,
            "holder_session_id": wrow.get("session_id"),
            "message": (
                f"Cannot read-claim {normalized}: it is write-locked by another "
                "live session. Wait for the writer to release."
            ),
        }
    async with db.execute(
        "SELECT id FROM file_read_claims WHERE file_path = ? AND session_id = ?",
        (normalized, session_id),
    ) as cur:
        existing = _row_to_dict(await cur.fetchone())
    if existing:
        await db.execute(
            "UPDATE file_read_claims SET claimed_at = datetime('now'), "
            "expires_at = datetime('now', ? || ' hours') WHERE id = ?",
            (str(ttl_hours), existing["id"]),
        )
    else:
        await db.execute(
            "INSERT INTO file_read_claims (id, file_path, session_id, claimed_at, expires_at) "
            "VALUES (?, ?, ?, datetime('now'), datetime('now', ? || ' hours'))",
            (_new_id(), normalized, session_id, str(ttl_hours)),
        )
    await db.commit()
    readers = await _all_read_claims(db, normalized)
    return {
        "claimed": True,
        "claim_mode": "read",
        "file_path": normalized,
        "session_id": session_id,
        "readers": [r.get("session_id") for r in readers],
        "reader_count": len(readers),
        "code_notes": await _code_notes_for_session_file(
            db, session_id, normalized, symbol
        ),
    }


async def release_file(
    db: aiosqlite.Connection,
    file_path: str,
    session_id: str,
) -> bool:
    """Release a file lock only when it is owned by ``session_id``."""
    normalized = (file_path or "").strip()
    if not normalized:
        return False
    cursor = await db.execute(
        "DELETE FROM file_locks WHERE file_path = ? AND session_id = ?",
        (normalized, session_id),
    )
    # Also soft-release any symbol-level claims this session holds on the file
    # (4bac57ff) — keeps hotspot history while freeing the symbols.
    sym_cursor = await db.execute(
        "UPDATE file_symbol_claims SET released_at = datetime('now') "
        "WHERE file_path = ? AND session_id = ? AND released_at IS NULL",
        (normalized, session_id),
    )
    # ffa03655 — also drop any shared read claim this session holds on the file.
    read_cursor = await db.execute(
        "DELETE FROM file_read_claims WHERE file_path = ? AND session_id = ?",
        (normalized, session_id),
    )
    await db.commit()
    return cursor.rowcount > 0 or sym_cursor.rowcount > 0 or read_cursor.rowcount > 0


async def release_file_locks_for_session(
    db: aiosqlite.Connection,
    session_id: str,
) -> int:
    """Release every file lock held by a session (write locks + read claims)."""
    cursor = await db.execute(
        "DELETE FROM file_locks WHERE session_id = ?",
        (session_id,),
    )
    # ffa03655 — also drop the session's shared read claims on cleanup.
    await db.execute(
        "DELETE FROM file_read_claims WHERE session_id = ?",
        (session_id,),
    )
    await db.commit()
    return cursor.rowcount


async def get_file_conflict_warnings(
    db: aiosqlite.Connection,
    project_id: str,
    exclude_session_id: str,
) -> list[str]:
    """Return warning strings for files claimed by other recently-active sessions.

    Checks the file_locks table for locks held by sessions other than
    ``exclude_session_id`` whose owning session is still live (status active/live
    and last_seen within the last 10 minutes). Returns human-readable strings
    like ``"dashboard.js claimed by session pre-launch-final (2h ago)"``.
    """
    from datetime import datetime, timezone, timedelta
    cutoff_10m = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    warnings: list[str] = []
    try:
        async with db.execute(
            "SELECT fl.file_path, s.name AS session_name, s.id AS session_id, s.last_seen "
            "FROM file_locks fl "
            "JOIN sessions s ON s.id = fl.session_id "
            "WHERE fl.session_id != ? "
            "AND s.project_id = ? "
            "AND s.status IN ('active', 'live') "
            "AND (s.last_seen IS NULL OR s.last_seen > ?)",
            (exclude_session_id, project_id, cutoff_10m),
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            r = _row_to_dict(row)
            if not r:
                continue
            name = r.get("session_name") or (r.get("session_id") or "unknown")[:8]
            last_seen = r.get("last_seen") or ""
            if last_seen:
                warnings.append(
                    f"{r['file_path']} claimed by session {name} (last_seen {last_seen})"
                )
            else:
                warnings.append(f"{r['file_path']} claimed by session {name}")
    except Exception:  # noqa: BLE001
        pass
    return warnings


async def get_file_claims(
    db: aiosqlite.Connection,
    file_path: str,
    project_id: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Return active claims on a file: the whole-file lock plus symbol claims.

    Read-only. Expires stale whole-file locks first so callers never see a
    lock past its TTL. ``file_lock`` is the active ``file_locks`` row (with the
    holder's ``session_name``) or ``None``; ``symbol_claims`` is the list from
    :func:`get_symbol_claims`.

    771c00d7 — when ``project_id`` is supplied, the result also carries a
    ``code_notes`` list of that project's code-anchored notes for this path
    (symbol-scoped when ``symbol`` is given). ``project_id=None`` keeps the
    legacy two-key result for callers that don't track a project.
    """
    normalized = _normalize_file_path(file_path)
    await expire_file_locks(db)
    await expire_file_read_claims(db)
    async with db.execute(
        "SELECT fl.*, s.name AS session_name FROM file_locks fl "
        "LEFT JOIN sessions s ON s.id = fl.session_id "
        "WHERE fl.file_path = ?",
        (normalized,),
    ) as cur:
        row = await cur.fetchone()
    result: dict[str, Any] = {
        "file_path": normalized,
        "file_lock": _row_to_dict(row),
        "symbol_claims": await get_symbol_claims(db, normalized),
        # ffa03655 — shared read claims held on this file (many concurrent).
        "read_claims": await _all_read_claims(db, normalized),
    }
    if project_id:
        result["code_notes"] = await get_code_notes_for_file(
            db, project_id, normalized, symbol
        )
    return result


# ---------------------------------------------------------------------------
# Generalized typed-resource locks (501ec93f)
# ---------------------------------------------------------------------------

# Valid resource-type prefixes for a touches_resources / resource_locks entry.
# A typed identifier is "<type>:<value>"; 'route' additionally carries the HTTP
# method, e.g. "route:POST:/projects". Unknown types are rejected so a typo
# never silently becomes an un-conflicting resource.
RESOURCE_TYPES = (
    "file",       # file:meridian/db/__init__.py
    "symbol",     # symbol:meridian/db/__init__.py::create_project (63b030a6)
    "db",         # db:migrations
    "mcp_tool",   # mcp_tool:get_parallelizable_groups
    "route",      # route:POST:/projects
    "pypi",       # pypi:publish
    "github",     # github:tag
    "note",       # note:<slug>  — project note
    "decision",   # decision:<id>  — pinned decision
)


def parse_resource_identifier(identifier: str) -> tuple[str, str]:
    """Split a typed resource identifier into ``(resource_type, value)``.

    ``identifier`` is ``"<type>:<value>"``. The type is matched case-insensitively
    against :data:`RESOURCE_TYPES`; the value keeps any further colons intact so
    ``"route:POST:/x"`` parses to ``("route", "POST:/x")``. Raises ``ValueError``
    for a missing/unknown type or an empty value.

    07bdfdbb — a leading ``inferred:`` provenance marker (used for
    auto-populated touches_resources) is stripped before type parsing, so an
    inferred resource canonicalizes to the SAME id as an explicit one and still
    participates in conflict detection. The marker only survives in the raw
    stored value, where it flags the resource as a guess that can be overridden.
    """
    text = (identifier or "").strip()
    if text.lower().startswith("inferred:"):
        text = text[len("inferred:"):].strip()
    if ":" not in text:
        raise ValueError(
            f"invalid resource identifier {identifier!r}: expected '<type>:<value>'"
        )
    rtype, value = text.split(":", 1)
    rtype = rtype.strip().lower()
    value = value.strip()
    if rtype not in RESOURCE_TYPES:
        raise ValueError(
            f"unknown resource type {rtype!r}: expected one of {', '.join(RESOURCE_TYPES)}"
        )
    if not value:
        raise ValueError(f"resource identifier {identifier!r} has an empty value")
    return rtype, value


def _normalize_resource_file_path(value: str) -> str:
    """Normalize a file path for a ``file:``/``symbol:`` resource id: backslashes
    → slashes, drop a leading ``./`` (so the same file referenced two ways
    collides on one resource lock). Distinct from :func:`_normalize_file_path`,
    which is strip-only to match how ``claim_file`` / code-note anchors store
    paths — do not merge the two. (63b030a6)"""
    value = (value or "").replace("\\", "/")
    if value.startswith("./"):
        value = value[2:]
    return value


def normalize_resource_id(identifier: str) -> str:
    """Canonicalize a typed resource identifier for storage / comparison.

    Lowercases the type, strips whitespace, and normalizes ``file:`` paths the
    same way file locks do (backslashes → slashes, drop a leading ``./``) so the
    same file referenced two ways collides on one lock. ``symbol:`` ids
    (63b030a6, ``symbol:<path>::<symbol>``) normalize the path part the same way
    while preserving the ``::<symbol>`` scope. Returns the canonical
    ``"<type>:<value>"`` string. Raises ``ValueError`` on an invalid identifier.
    """
    rtype, value = parse_resource_identifier(identifier)
    if rtype == "file":
        value = _normalize_resource_file_path(value)
    elif rtype == "symbol":
        # symbol:<path>::<symbol> — normalize the file path, keep the symbol scope.
        path, sep, sym = value.partition("::")
        value = _normalize_resource_file_path(path) + (sep + sym if sep else "")
    return f"{rtype}:{value}"


def _resource_file_of(rid: str) -> "str | None":
    """Return the file path a ``file:`` / ``symbol:`` resource id refers to, or
    None for other resource types. (63b030a6 — cross-type conflict detection.)"""
    if rid.startswith("file:"):
        return rid[len("file:"):]
    if rid.startswith("symbol:"):
        return rid[len("symbol:"):].partition("::")[0]
    return None


def _two_resources_conflict(r1: str, r2: str) -> bool:
    """True if two normalized resource ids conflict under the file⊃symbol hierarchy.

    Rules (63b030a6):
      - identical ids conflict (file:X vs file:X, symbol:X::a vs symbol:X::a);
      - a whole-file lock conflicts with ANY symbol on that file
        (file:X vs symbol:X::a) — file is the most exclusive grain;
      - two DIFFERENT symbols on the same file do NOT conflict
        (symbol:X::a vs symbol:X::b) — that's the point of symbol-level locking;
      - everything else conflicts only on exact string equality.
    """
    if r1 == r2:
        return True
    f1, f2 = _resource_file_of(r1), _resource_file_of(r2)
    if f1 is not None and f1 == f2:
        # Same file. Conflict unless BOTH are (distinct) symbol claims.
        both_symbols = r1.startswith("symbol:") and r2.startswith("symbol:")
        return not both_symbols
    return False


def _resource_sets_conflict(a: "set[str] | frozenset[str]", b: "set[str] | frozenset[str]") -> bool:
    """True if any resource in *a* conflicts with any in *b* under the file/symbol
    hierarchy. Replaces a plain ``set.isdisjoint`` so file-vs-symbol overlaps are
    caught. (63b030a6)"""
    for ra in a:
        for rb in b:
            if _two_resources_conflict(ra, rb):
                return True
    return False


def parse_touches_resources(raw: Any) -> list[str]:
    """Decode a sprint item's ``touches_resources`` field into normalized ids.

    Accepts a JSON list, a Python list, or a comma-separated string. Each entry
    is normalized via :func:`normalize_resource_id`; entries that fail validation
    are skipped (best-effort decode so a single bad value never breaks reads).
    Duplicates are collapsed while preserving first-seen order. ``None``/empty
    yields ``[]``.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        values: list[Any] = raw
    else:
        text = str(raw).strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
            values = decoded if isinstance(decoded, list) else [decoded]
        except Exception:  # noqa: BLE001
            values = [part.strip() for part in text.split(",")]
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        candidate = str(value or "").strip()
        if not candidate:
            continue
        try:
            normalized = normalize_resource_id(candidate)
        except ValueError:
            continue
        if normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def serialize_touches_resources(raw: Any) -> str | None:
    """Normalize and JSON-encode a touches_resources input for storage.

    Returns a JSON array string, or ``None`` when there are no valid resources
    (so the column stays NULL rather than holding ``"[]"``). Raises ``ValueError``
    when an explicit identifier is malformed, so writers surface bad input
    instead of silently dropping it.
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        items = raw
    else:
        text = str(raw).strip()
        if not text:
            return None
        try:
            decoded = json.loads(text)
            items = decoded if isinstance(decoded, list) else [decoded]
        except Exception:  # noqa: BLE001
            items = [part.strip() for part in text.split(",")]
    normalized: list[str] = []
    seen: set[str] = set()
    for value in items:
        candidate = str(value or "").strip()
        if not candidate:
            continue
        # 07bdfdbb — preserve an `inferred:` provenance marker in storage so the
        # guess stays distinguishable/overridable, while deduping + comparing on
        # the underlying canonical id (normalize_resource_id strips the marker).
        marker = ""
        body = candidate
        if candidate.lower().startswith("inferred:"):
            marker = "inferred:"
            body = candidate[len("inferred:"):].strip()
        norm = normalize_resource_id(body)  # raises on bad input
        if norm not in seen:
            seen.add(norm)
            normalized.append(marker + norm)
    return json.dumps(normalized) if normalized else None


# f5f2a89d — [[slug]] backlink parsing for notes/decisions
_BACKLINK_RE = re.compile(r'\[\[([^\]]+)\]\]')


def parse_note_backlinks(body: str) -> list[str]:
    """Extract [[slug]] references from a note or decision body."""
    return _BACKLINK_RE.findall(body or "")


async def get_notes_backlinked_to(
    db: aiosqlite.Connection, project_id: str, slug: str
) -> list[dict[str, Any]]:
    """Return lightweight records for notes whose body references [[slug]]."""
    pattern = f"%[[{slug}]]%"
    async with db.execute(
        "SELECT id, slug, title FROM project_notes "
        "WHERE project_id = ? AND body LIKE ?",
        (project_id, pattern),
    ) as cur:
        rows = await cur.fetchall()
    return [{"id": r[0], "slug": r[1], "title": r[2]} for r in (rows or [])]


async def get_sprint_items_for_resource(
    db: aiosqlite.Connection, project_id: str, resource_id: str
) -> list[dict[str, Any]]:
    """Return sprint items whose touches_resources includes resource_id.

    f5f2a89d — reverse lookup used by the dashboard chip popover.
    Candidates are pre-filtered with LIKE, then confirmed with parse_touches_resources
    so inferred: markers and case don't produce false positives.
    """
    pattern = f"%{resource_id}%"
    async with db.execute(
        "SELECT * FROM sprint_items WHERE project_id = ? AND touches_resources LIKE ?",
        (project_id, pattern),
    ) as cur:
        rows = await cur.fetchall()
    items = [_row_to_dict(r) for r in (rows or []) if r]
    return [
        it for it in items
        if resource_id in parse_touches_resources(it.get("touches_resources"))
    ]


async def expire_resource_locks(db: aiosqlite.Connection) -> int:
    """Delete expired resource locks and return how many rows were cleared.

    Mirrors :func:`expire_file_locks` exactly — two expiry paths: explicit TTL
    (expires_at <= now) and owning-session heartbeat (last_seen older than
    _CLAIM_LIVE_HOURS, for crashed sessions that never released).
    """
    stale_cutoff = _cutoff_dt(_CLAIM_LIVE_HOURS)
    cursor = await db.execute(
        "DELETE FROM resource_locks WHERE expires_at <= datetime('now') "
        "OR session_id IN ("
        "    SELECT id FROM sessions "
        "    WHERE last_seen IS NOT NULL AND last_seen < ?"
        ")",
        (stale_cutoff,),
    )
    await db.commit()
    return cursor.rowcount


async def claim_resource(
    db: aiosqlite.Connection,
    resource_id: str,
    session_id: str,
    *,
    ttl_hours: int = _FILE_LOCK_TTL_HOURS,
) -> dict[str, Any]:
    """Claim a typed resource for a session, auto-releasing expired locks first.

    Same primitive as :func:`claim_file` but for any typed resource id. Returns
    ``{"claimed": bool, "resource_id", "resource_type", "session_id",
    "claimed_at", "expires_at", ...}``. When another live session already holds
    the resource, ``claimed`` is False and ``holder_session_id`` names the owner.
    Re-claiming a resource you already hold refreshes the TTL (idempotent).
    """
    normalized = normalize_resource_id(resource_id)  # raises on bad input
    rtype, _ = parse_resource_identifier(normalized)
    await expire_resource_locks(db)
    async with db.execute(
        "SELECT * FROM resource_locks WHERE resource_id = ?",
        (normalized,),
    ) as cur:
        existing_row = await cur.fetchone()
    existing = _row_to_dict(existing_row)
    if existing and existing.get("session_id") != session_id:
        return {
            "claimed": False,
            "resource_id": normalized,
            "resource_type": rtype,
            "session_id": session_id,
            "holder_session_id": existing.get("session_id"),
            "claimed_at": existing.get("claimed_at"),
            "expires_at": existing.get("expires_at"),
        }
    if existing and existing.get("session_id") == session_id:
        await db.execute(
            "UPDATE resource_locks SET claimed_at = datetime('now'), "
            "expires_at = datetime('now', ? || ' hours') WHERE id = ?",
            (str(ttl_hours), existing["id"]),
        )
    else:
        await db.execute(
            "INSERT INTO resource_locks "
            "(id, resource_id, resource_type, session_id, claimed_at, expires_at) "
            "VALUES (?, ?, ?, ?, datetime('now'), datetime('now', ? || ' hours')) "
            "ON CONFLICT (resource_id) DO NOTHING",
            (_new_id(), normalized, rtype, session_id, str(ttl_hours)),
        )
    await db.commit()
    async with db.execute(
        "SELECT * FROM resource_locks WHERE resource_id = ?",
        (normalized,),
    ) as cur:
        row = await cur.fetchone()
    lock = _row_to_dict(row) or {}
    if lock.get("session_id") != session_id:
        # Another session raced us (ON CONFLICT DO NOTHING was a no-op).
        return {
            "claimed": False,
            "resource_id": normalized,
            "resource_type": rtype,
            "session_id": session_id,
            "holder_session_id": lock.get("session_id"),
            "claimed_at": lock.get("claimed_at"),
            "expires_at": lock.get("expires_at"),
        }
    return {
        "claimed": True,
        "resource_id": normalized,
        "resource_type": rtype,
        "session_id": lock.get("session_id"),
        "claimed_at": lock.get("claimed_at"),
        "expires_at": lock.get("expires_at"),
    }


async def release_resource(
    db: aiosqlite.Connection,
    resource_id: str,
    session_id: str,
) -> bool:
    """Release a resource lock only when it is owned by ``session_id``."""
    try:
        normalized = normalize_resource_id(resource_id)
    except ValueError:
        return False
    cursor = await db.execute(
        "DELETE FROM resource_locks WHERE resource_id = ? AND session_id = ?",
        (normalized, session_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def release_resource_locks_for_session(
    db: aiosqlite.Connection,
    session_id: str,
) -> int:
    """Release every resource lock held by a session."""
    cursor = await db.execute(
        "DELETE FROM resource_locks WHERE session_id = ?",
        (session_id,),
    )
    await db.commit()
    return cursor.rowcount


async def get_resource_claims(
    db: aiosqlite.Connection,
    resource_id: str,
) -> dict[str, Any]:
    """Return the active lock on a resource (with holder session_name) or None.

    Read-only. Expires stale locks first so callers never see a lock past TTL.
    """
    normalized = normalize_resource_id(resource_id)
    await expire_resource_locks(db)
    async with db.execute(
        "SELECT rl.*, s.name AS session_name FROM resource_locks rl "
        "LEFT JOIN sessions s ON s.id = rl.session_id "
        "WHERE rl.resource_id = ?",
        (normalized,),
    ) as cur:
        row = await cur.fetchone()
    return {
        "resource_id": normalized,
        "resource_lock": _row_to_dict(row),
    }


async def get_resource_conflicts(
    db: aiosqlite.Connection,
    project_id: str,
    resources: list[str],
    *,
    exclude_session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return active resource locks (held by other live sessions) overlapping ``resources``.

    Used for pre-claim / pre-fanout conflict detection: given the resource ids a
    unit of work wants to touch, surface any that another still-live session in
    the project already holds. Stale locks are expired first.
    """
    wanted: set[str] = set()
    for r in resources or []:
        try:
            wanted.add(normalize_resource_id(r))
        except ValueError:
            continue
    if not wanted:
        return []
    await expire_resource_locks(db)
    params: list[Any] = [project_id]
    exclude_clause = ""
    if exclude_session_id:
        exclude_clause = "AND rl.session_id != ? "
        params.append(exclude_session_id)
    async with db.execute(
        "SELECT rl.resource_id, rl.resource_type, rl.session_id, "
        "       s.name AS session_name, s.last_seen "
        "FROM resource_locks rl "
        "JOIN sessions s ON s.id = rl.session_id "
        "WHERE s.project_id = ? "
        f"{exclude_clause}"
        "AND s.status IN ('active', 'live') "
        "AND (s.last_seen IS NULL OR s.last_seen > datetime('now', '-10 minutes'))",
        tuple(params),
    ) as cur:
        rows = await cur.fetchall()
    conflicts: list[dict[str, Any]] = []
    for row in rows:
        r = _row_to_dict(row) or {}
        rid = str(r.get("resource_id") or "")
        if rid not in wanted:
            continue
        conflicts.append({
            "resource_id": rid,
            "resource_type": r.get("resource_type"),
            "session_id": r.get("session_id"),
            "session_name": r.get("session_name"),
            "last_seen": r.get("last_seen"),
        })
    return conflicts


async def get_parallelizable_groups(
    db: aiosqlite.Connection,
    project_id: str,
    version: str | None = None,
) -> dict[str, Any]:
    """255096d9 — cluster pending sprint items that are safe to run in parallel.

    Algorithm:
      1. Take pending/todo items (optionally filtered to ``version``) whose
         ``depends_on`` is satisfied (no parent, or parent is done — or parent
         failed with failure_mode='continue'). Items still waiting on a parent
         are returned separately under ``blocked``.
      2. Build a conflict graph: two eligible items conflict when their
         ``touches_resources`` sets intersect (see 501ec93f). An item with no
         declared resources conflicts with nothing (empty ∩ anything = ∅).
      3. Greedy first-fit coloring partitions the items into groups such that no
         two items *within a group* share a resource — so every group is a batch
         the orchestrator can fan out simultaneously, and successive groups run
         in sequence.

    Returns ``{"version", "groups": [[item, ...], ...], "group_count",
    "eligible_count", "blocked": [...], "undeclared_count"}``. ``groups`` items
    are full sprint-item dicts with a derived ``resources`` list attached.
    """
    items = await get_sprint_items(db, project_id)
    if version is not None:
        items = [it for it in items if it.get("version") == version]
    claimable_statuses = {"pending", "todo"}
    eligible: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    # df573218 — surface currently-claimed work so an orchestrator sees the live
    # parallelism state (and knows an item it planned was grabbed by another).
    running: list[dict[str, Any]] = []
    for it in items:
        if it.get("status") == "in_progress" or (
            (it.get("status") or "pending") in claimable_statuses and it.get("claimed_at")
        ):
            running.append({
                "id": it["id"],
                "title": it.get("title", ""),
                "status": it.get("status"),
                "claimed_at": it.get("claimed_at"),
            })
        if (it.get("status") or "pending") not in claimable_statuses:
            continue
        if it.get("claimed_at"):
            continue  # already in flight
        parent_block = await get_blocking_dependency_for_sprint_item(db, it["id"])
        if parent_block is not None:
            # Parent failed + this item's failure_mode='continue' → still runnable.
            if (
                parent_block.get("status") == "failed"
                and (it.get("failure_mode") or "continue") == "continue"
            ):
                pass
            else:
                blocked.append({
                    "id": it["id"],
                    "title": it.get("title", ""),
                    "depends_on": it.get("depends_on"),
                    "blocked_by_status": parent_block.get("status"),
                })
                continue
        enriched = {**it, "resources": parse_touches_resources(it.get("touches_resources"))}
        eligible.append(enriched)
    # Stable order: oldest first, then id, so coloring is deterministic.
    eligible.sort(key=lambda it: (str(it.get("added_at") or ""), it["id"]))
    # de730a25 — separate declared from undeclared items. An item with no
    # touches_resources is disjoint with everything, so the old single-pass
    # coloring dropped it into group 0 next to declared items and they fanned
    # out together — unsafe, because an undeclared item may genuinely conflict
    # with anything. Now: color-graph only the DECLARED items into safe parallel
    # groups, then give each UNDECLARED item its own singleton group so they run
    # sequentially (parallel safety can't be proven for them).
    declared = [it for it in eligible if it["resources"]]
    undeclared_items = [it for it in eligible if not it["resources"]]
    undeclared = len(undeclared_items)
    # Greedy first-fit graph coloring on the declared items' conflict graph.
    groups: list[list[dict[str, Any]]] = []
    group_resource_sets: list[set[str]] = []
    for it in declared:
        res = set(it["resources"])
        placed = False
        for gi, used in enumerate(group_resource_sets):
            # 63b030a6 — cross-type aware: file:X conflicts with symbol:X::*, but
            # symbol:X::a and symbol:X::b can co-schedule. (plain isdisjoint missed this)
            if not _resource_sets_conflict(res, used):
                groups[gi].append(it)
                used.update(res)
                placed = True
                break
        if not placed:
            groups.append([it])
            group_resource_sets.append(set(res))
    # Each undeclared item is its own sequential group (never co-scheduled).
    for it in undeclared_items:
        groups.append([it])
    return {
        "version": version,
        "groups": groups,
        "group_count": len(groups),
        "eligible_count": len(eligible),
        "undeclared_count": undeclared,
        "blocked": blocked,
        "running": running,  # df573218 — items currently in flight
    }


async def analyze_sprint(
    db: aiosqlite.Connection,
    project_id: str,
    version: str | None = None,
) -> dict[str, Any]:
    """e77f09d1 — synthesize a structured planning brief for the current sprint.

    One call combines what a planner otherwise assembles from four:
      * parallelizability — conflict-free batches from
        :func:`get_parallelizable_groups` (group_count, max fan-out, blocked).
      * dependency chains — ``depends_on`` walked to the root for each open item.
      * resource conflicts — open items whose ``touches_resources`` intersect
        (why they can't co-schedule).
      * stalls — open items with a non-zero ``stall_count``.

    Returns a single dict with a human ``summary`` line and a
    ``recommended_strategy`` ('parallel' when any group holds >1 item).
    """
    groups_info = await get_parallelizable_groups(db, project_id, version=version)
    items = await get_sprint_items(db, project_id)
    if version is not None:
        items = [it for it in items if it.get("version") == version]
    _open = {"pending", "todo", "in_progress"}
    open_items = [it for it in items if (it.get("status") or "pending") in _open]
    by_id = {it["id"]: it for it in items}

    # Dependency chains: walk depends_on to the root for each open dependent item.
    def _chain_for(item: dict[str, Any]) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        cur: dict[str, Any] | None = item
        while cur is not None and cur["id"] not in seen:
            seen.add(cur["id"])
            chain.append({
                "id": cur["id"],
                "title": cur.get("title", ""),
                "status": cur.get("status"),
            })
            dep = cur.get("depends_on")
            cur = by_id.get(dep) if dep else None
        chain.reverse()
        return chain

    chains = [
        _chain_for(it) for it in open_items if it.get("depends_on")
    ]
    chains = [c for c in chains if len(c) > 1]
    longest_chain = max((len(c) for c in chains), default=1)

    # Resource/file conflicts among open items (shared touches_resources).
    res_map: dict[str, list[str]] = {}
    for it in open_items:
        for res in parse_touches_resources(it.get("touches_resources")):
            res_map.setdefault(res, []).append(it["id"])
    conflicts = [
        {"resource": res, "item_ids": ids}
        for res, ids in sorted(res_map.items()) if len(ids) > 1
    ]

    stalls = [
        {"id": it["id"], "title": it.get("title", ""),
         "stall_count": it.get("stall_count") or 0}
        for it in open_items if (it.get("stall_count") or 0) > 0
    ]

    groups = groups_info.get("groups", [])
    max_group = max((len(g) for g in groups), default=0)
    strategy = "parallel" if max_group > 1 else "sequential"
    summary = (
        f"{groups_info.get('eligible_count', 0)} eligible item(s) in "
        f"{groups_info.get('group_count', 0)} group(s) (max {max_group} parallel); "
        f"longest dependency chain {longest_chain}; {len(conflicts)} resource "
        f"conflict(s); {len(stalls)} stalled; "
        f"{len(groups_info.get('blocked', []))} blocked."
    )
    return {
        "version": version,
        "summary": summary,
        "recommended_strategy": strategy,
        "parallelism": {
            "group_count": groups_info.get("group_count", 0),
            "eligible_count": groups_info.get("eligible_count", 0),
            "max_parallel": max_group,
            "undeclared_count": groups_info.get("undeclared_count", 0),
            "groups": [
                [{"id": it["id"], "title": it.get("title", "")} for it in g]
                for g in groups
            ],
        },
        "dependency_chains": chains,
        "longest_chain": longest_chain,
        "file_conflicts": conflicts,
        "stalls": stalls,
        "blocked": groups_info.get("blocked", []),
        "running": groups_info.get("running", []),
    }


# ---------------------------------------------------------------------------
# Parallel-coordination primitives (Wave 4): findings + messaging + barrier.
# ---------------------------------------------------------------------------

async def store_finding(
    db: aiosqlite.Connection,
    project_id: str,
    content: str,
    *,
    session_id: str | None = None,
    key: str | None = None,
    title: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """c35370cc — persist a per-task intermediate result to ``session_findings``.

    Parallel reader agents write findings that survive session boundaries; an
    orchestrator or writer agent reads them back via :func:`get_findings`. ``key``
    is an optional bucket (e.g. a subsystem name) for scoped retrieval.
    """
    fid = _new_id()
    await db.execute(
        "INSERT INTO session_findings "
        "(id, project_id, session_id, key, title, content, task_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (fid, project_id, session_id, key, title, content, task_id),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM session_findings WHERE id = ?", (fid,)
    ) as cur:
        return _row_to_dict(await cur.fetchone()) or {}


async def get_findings(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    key: str | None = None,
    session_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """c35370cc — read stored findings for a project (newest first), optionally
    scoped by ``key`` and/or ``session_id``."""
    sql = "SELECT * FROM session_findings WHERE project_id = ?"
    params: list[Any] = [project_id]
    if key is not None:
        sql += " AND key = ?"
        params.append(key)
    if session_id is not None:
        sql += " AND session_id = ?"
        params.append(session_id)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def send_message(
    db: aiosqlite.Connection,
    project_id: str,
    to_session_id: str,
    payload: str,
    *,
    from_session_id: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """d3a3a01d — enqueue an actor-model message to another session."""
    mid = _new_id()
    await db.execute(
        "INSERT INTO session_messages "
        "(id, project_id, from_session_id, to_session_id, kind, payload) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (mid, project_id, from_session_id, to_session_id, kind, payload),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM session_messages WHERE id = ?", (mid,)
    ) as cur:
        return _row_to_dict(await cur.fetchone()) or {}


async def receive_messages(
    db: aiosqlite.Connection,
    session_id: str,
    *,
    mark_read: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """d3a3a01d — fetch unread messages addressed to ``session_id`` (oldest
    first) and, by default, mark them read so the next poll only sees new ones."""
    async with db.execute(
        "SELECT * FROM session_messages WHERE to_session_id = ? AND read_at IS NULL "
        "ORDER BY created_at ASC, id ASC LIMIT ?",
        (session_id, int(limit)),
    ) as cur:
        rows = [_row_to_dict(r) for r in await cur.fetchall()]
    if mark_read and rows:
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" for _ in ids)
        await db.execute(
            f"UPDATE session_messages SET read_at = datetime('now') WHERE id IN ({placeholders})",
            ids,
        )
        await db.commit()
    return rows


async def idle_until_all_done(
    db: aiosqlite.Connection,
    session_ids: list[str],
) -> dict[str, Any]:
    """d3a3a01d — non-blocking barrier check across sibling sessions.

    Returns ``{all_done, pending, statuses}``. A session counts as done when it
    is closed/archived (or missing); active/idle sessions are still running. The
    server can't block, so the caller polls until ``all_done`` is True — the
    A2A-compatible "wait for X, Y, Z to finish" primitive.
    """
    statuses: dict[str, Any] = {}
    pending: list[str] = []
    for sid in session_ids or []:
        async with db.execute(
            "SELECT status FROM sessions WHERE id = ?", (sid,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            statuses[sid] = "missing"
            continue
        status = row["status"] if isinstance(row, dict) else row[0]
        statuses[sid] = status
        if status in ("active", "idle"):
            pending.append(sid)
    return {"all_done": not pending, "pending": pending, "statuses": statuses}


# ---------------------------------------------------------------------------
# Symbol-level parallel protection (4bac57ff)
# ---------------------------------------------------------------------------


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Inclusive line-range overlap test."""
    return a_start <= b_end and b_start <= a_end


async def _live_symbol_claims_for_file(
    db: aiosqlite.Connection,
    file_path: str,
    exclude_session_id: str,
) -> list[dict[str, Any]]:
    """Symbol claims on ``file_path`` held by *other* still-live sessions.

    39544099 — uses _CLAIM_LIVE_HOURS (unified with file_locks TTL) as the
    staleness cutoff instead of a hardcoded 10-minute window, so both expiry
    mechanisms share the same constant. A crashed session's claims time out after
    _CLAIM_LIVE_HOURS just like a whole-file lock does.
    """
    cutoff = _cutoff_dt(_CLAIM_LIVE_HOURS)
    async with db.execute(
        "SELECT fsc.symbol_name, fsc.symbol_type, fsc.line_start, fsc.line_end, "
        "       fsc.session_id, s.name AS session_name "
        "FROM file_symbol_claims fsc "
        "JOIN sessions s ON s.id = fsc.session_id "
        "WHERE fsc.file_path = ? AND fsc.session_id != ? "
        "AND fsc.released_at IS NULL "
        "AND s.status IN ('active', 'live') "
        "AND (s.last_seen IS NULL OR s.last_seen > ?)",
        (file_path, exclude_session_id, cutoff),
    ) as cur:
        rows = await cur.fetchall()
    return [r for r in (_row_to_dict(row) for row in rows) if r]


async def claim_symbol(
    db: aiosqlite.Connection,
    session_id: str,
    file_path: str,
    symbol: str,
    content: str,
) -> dict[str, Any]:
    """Claim a single class/function/method by line range within a file.

    Parses ``content`` to locate ``symbol``'s line span, then hard-blocks if any
    *other* live session already claims an overlapping span. On a block it
    returns the conflicting claims plus ``safe_to_claim`` — symbols in the file
    whose ranges are free — so the caller can immediately pick a non-colliding
    symbol. Returns ``reason='unparseable'`` (unsupported/syntax-error/missing
    grammar) so callers can fall back to whole-file ``claim_file``.
    """
    from ..symbols import extract_symbols

    normalized = (file_path or "").strip()
    symbol = (symbol or "").strip()
    if not normalized:
        raise ValueError("file_path is required")
    if not symbol:
        raise ValueError("symbol is required")

    # 63b030a6 — file ⊃ symbol hierarchy: if another live session holds a
    # WHOLE-FILE lock on this file, no symbol-level claim is allowed (the file
    # owner may touch any symbol). Mirror the inverse block in claim_file.
    await expire_file_locks(db)
    async with db.execute(
        "SELECT session_id, claimed_at, expires_at FROM file_locks WHERE file_path = ?",
        (_normalize_file_path(normalized),),
    ) as cur:
        _fl_row = await cur.fetchone()
    _fl = _row_to_dict(_fl_row)
    if _fl and _fl.get("session_id") and _fl.get("session_id") != session_id:
        return {
            "claimed": False,
            "reason": "file_locked",
            "file_path": normalized,
            "holder_session_id": _fl.get("session_id"),
            "message": (
                f"Cannot claim symbol in {normalized}: another live session holds a "
                "whole-file lock on it. Wait for it to release, or coordinate."
            ),
        }

    symbols = extract_symbols(normalized, content or "")
    if not symbols:
        return {
            "claimed": False,
            "reason": "unparseable",
            "file_path": normalized,
            "message": (
                f"Could not extract symbols from {normalized} "
                "(unsupported language, syntax error, or missing grammar). "
                "Use whole-file claim_file instead."
            ),
        }

    target = next((s for s in symbols if s["name"] == symbol), None)
    if target is None:
        return {
            "claimed": False,
            "reason": "symbol_not_found",
            "file_path": normalized,
            "available_symbols": [s["name"] for s in symbols],
            "message": (
                f"Symbol '{symbol}' not found in {normalized}. "
                f"Available: {', '.join(s['name'] for s in symbols) or '(none)'}"
            ),
        }

    others = await _live_symbol_claims_for_file(db, normalized, session_id)
    conflicts = [
        c for c in others
        if _ranges_overlap(target["line_start"], target["line_end"], c["line_start"], c["line_end"])
    ]
    if conflicts:
        claimed_ranges = [(c["line_start"], c["line_end"]) for c in others]
        safe = [
            s["name"] for s in symbols
            if s["name"] != symbol
            and not any(_ranges_overlap(s["line_start"], s["line_end"], cs, ce) for cs, ce in claimed_ranges)
        ]
        holder = conflicts[0]
        holder_name = holder.get("session_name") or (holder.get("session_id") or "unknown")[:8]
        safe_hint = f" — you can safely claim {', '.join(safe)}" if safe else " — no other symbols are free"
        return {
            "claimed": False,
            "reason": "symbol_conflict",
            "file_path": normalized,
            "symbol": symbol,
            "conflicts": [
                {
                    "symbol_name": c["symbol_name"],
                    "line_start": c["line_start"],
                    "line_end": c["line_end"],
                    "holder_session_id": c["session_id"],
                    "holder_session_name": c.get("session_name"),
                }
                for c in conflicts
            ],
            "safe_to_claim": safe,
            "message": (
                f"⚠️ {conflicts[0]['symbol_name']} "
                f"(lines {conflicts[0]['line_start']}-{conflicts[0]['line_end']}) "
                f"claimed by session {holder_name}{safe_hint}"
            ),
        }

    # No conflict — (re)claim this symbol for the session (idempotent per symbol).
    # Drop any prior row for this exact (session, file, symbol), active or
    # released, so a re-claim is a single fresh active row.
    await db.execute(
        "DELETE FROM file_symbol_claims WHERE session_id = ? AND file_path = ? AND symbol_name = ?",
        (session_id, normalized, symbol),
    )
    await db.execute(
        "INSERT INTO file_symbol_claims "
        "(id, session_id, file_path, symbol_name, symbol_type, line_start, line_end) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_new_id(), session_id, normalized, symbol, target["type"],
         target["line_start"], target["line_end"]),
    )
    await db.commit()
    return {
        "claimed": True,
        "file_path": normalized,
        "session_id": session_id,
        "symbol": symbol,
        "symbol_type": target["type"],
        "line_start": target["line_start"],
        "line_end": target["line_end"],
    }


async def get_symbol_claims(
    db: aiosqlite.Connection,
    file_path: str,
) -> list[dict[str, Any]]:
    """Return active symbol claims on a file (released_at IS NULL), newest first."""
    async with db.execute(
        "SELECT fsc.*, s.name AS session_name FROM file_symbol_claims fsc "
        "LEFT JOIN sessions s ON s.id = fsc.session_id "
        "WHERE fsc.file_path = ? AND fsc.released_at IS NULL "
        "ORDER BY fsc.claimed_at DESC",
        ((file_path or "").strip(),),
    ) as cur:
        rows = await cur.fetchall()
    return [r for r in (_row_to_dict(row) for row in rows) if r]


async def release_symbol_claims_for_session(
    db: aiosqlite.Connection,
    session_id: str,
    file_path: str | None = None,
) -> int:
    """Soft-release a session's active symbol claims (all, or just one file).

    Sets released_at instead of deleting so hotspot scoring retains the history.
    Returns the number of claims released.
    """
    if file_path:
        cur = await db.execute(
            "UPDATE file_symbol_claims SET released_at = datetime('now') "
            "WHERE session_id = ? AND file_path = ? AND released_at IS NULL",
            (session_id, (file_path or "").strip()),
        )
    else:
        cur = await db.execute(
            "UPDATE file_symbol_claims SET released_at = datetime('now') "
            "WHERE session_id = ? AND released_at IS NULL",
            (session_id,),
        )
    await db.commit()
    return cur.rowcount


async def get_symbol_hotspots(
    db: aiosqlite.Connection,
    file_path: str | None = None,
    *,
    min_sessions: int = 3,
    days: int = 14,
) -> list[dict[str, Any]]:
    """Symbols claimed by ``min_sessions``+ distinct sessions within ``days``.

    A hotspot is a symbol many sessions keep touching — a refactor/ownership
    smell. Computed over recent rows in file_symbol_claims (active + not-yet-
    released claims within the window).
    """
    params: list[Any] = [f"-{max(0, int(days))} days"]
    where = "WHERE claimed_at > datetime('now', ?)"
    if file_path:
        where += " AND file_path = ?"
        params.append((file_path or "").strip())
    sql = (
        "SELECT file_path, symbol_name, symbol_type, "
        "COUNT(DISTINCT session_id) AS session_count "
        f"FROM file_symbol_claims {where} "
        "GROUP BY file_path, symbol_name, symbol_type "
        "HAVING COUNT(DISTINCT session_id) >= ? "
        "ORDER BY session_count DESC, file_path"
    )
    params.append(int(min_sessions))
    async with db.execute(sql, tuple(params)) as cur:
        rows = await cur.fetchall()
    return [r for r in (_row_to_dict(row) for row in rows) if r]


async def get_hotspot_suggestions(
    db: aiosqlite.Connection,
    *,
    min_sessions: int = 5,
    days: int = 30,
) -> list[dict[str, Any]]:
    """Return sprint item suggestions based on file-level contention hotspots.

    1b4760a9 — files touched by min_sessions+ distinct sessions within days are
    likely candidates for refactoring, clearer ownership, or better test coverage.
    Returns dicts with: file_path, session_count, suggestion (human-readable
    recommendation text). Computed over file_symbol_claims grouped by file_path
    (not symbol), so a heavily-edited file surfaces even if individual symbols
    each have low session counts.
    """
    params: list[Any] = [f"-{max(0, int(days))} days"]
    sql = (
        "SELECT file_path, COUNT(DISTINCT session_id) AS session_count "
        "FROM file_symbol_claims "
        "WHERE claimed_at > datetime('now', ?) "
        "GROUP BY file_path "
        "HAVING COUNT(DISTINCT session_id) >= ? "
        "ORDER BY session_count DESC, file_path"
    )
    params.append(int(min_sessions))
    async with db.execute(sql, tuple(params)) as cur:
        rows = await cur.fetchall()
    suggestions = []
    for row in rows:
        r = _row_to_dict(row)
        if not r:
            continue
        fp = r.get("file_path", "")
        sc = r.get("session_count", 0)
        suggestions.append({
            "file_path": fp,
            "session_count": sc,
            "suggestion": (
                f"Refactor or add ownership docs for {fp} — "
                f"touched by {sc} distinct sessions in the last {days} days"
            ),
        })
    return suggestions


# ---------------------------------------------------------------------------
# Blog CMS (6234f9b8) — admin-authored posts
# ---------------------------------------------------------------------------


def _slugify_title(title: str) -> str:
    """Lowercase, hyphenated, alphanumeric slug from a title."""
    import re as _re
    slug = _re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return slug or "post"


async def _unique_blog_slug(db: aiosqlite.Connection, base: str, exclude_id: str | None = None) -> str:
    """Return ``base``, or base-2/base-3/... if the slug is already taken."""
    slug = base
    n = 1
    while True:
        async with db.execute(
            "SELECT id FROM blog_posts WHERE slug = ?", (slug,)
        ) as cur:
            row = await cur.fetchone()
        existing = _row_to_dict(row)
        if existing is None or existing.get("id") == exclude_id:
            return slug
        n += 1
        slug = f"{base}-{n}"


async def upsert_blog_post(
    db: aiosqlite.Connection,
    *,
    post_id: str | None = None,
    title: str,
    body_md: str = "",
    slug: str | None = None,
) -> dict[str, Any]:
    """Create a draft, or update an existing post's title/body/slug. Status is
    not changed here — use publish_blog_post / unpublish_blog_post for that."""
    title = (title or "").strip() or "Untitled"
    if post_id:
        existing = await get_blog_post(db, post_id)
        if existing is None:
            raise ValueError("blog post not found")
        new_slug = await _unique_blog_slug(
            db, _slugify_title(slug or existing.get("slug") or title), exclude_id=post_id
        )
        await db.execute(
            "UPDATE blog_posts SET title = ?, body_md = ?, slug = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (title, body_md or "", new_slug, post_id),
        )
        await db.commit()
        return await get_blog_post(db, post_id)
    bid = _new_id()
    new_slug = await _unique_blog_slug(db, _slugify_title(slug or title))
    await db.execute(
        "INSERT INTO blog_posts (id, title, slug, body_md, status) "
        "VALUES (?, ?, ?, ?, 'draft')",
        (bid, title, new_slug, body_md or ""),
    )
    await db.commit()
    return await get_blog_post(db, bid)


async def get_blog_post(db: aiosqlite.Connection, post_id: str) -> dict[str, Any] | None:
    async with db.execute("SELECT * FROM blog_posts WHERE id = ?", (post_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_blog_post_by_slug(
    db: aiosqlite.Connection, slug: str, *, published_only: bool = True
) -> dict[str, Any] | None:
    sql = "SELECT * FROM blog_posts WHERE slug = ?"
    params: list[Any] = [(slug or "").strip()]
    if published_only:
        sql += " AND status = 'published'"
    async with db.execute(sql, tuple(params)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def list_blog_posts(
    db: aiosqlite.Connection, status: str | None = None
) -> list[dict[str, Any]]:
    """List posts newest-first. ``status`` filters to draft/published."""
    if status in ("draft", "published"):
        sql = ("SELECT * FROM blog_posts WHERE status = ? "
               "ORDER BY COALESCE(published_at, updated_at) DESC")
        params: tuple[Any, ...] = (status,)
    else:
        sql = "SELECT * FROM blog_posts ORDER BY updated_at DESC"
        params = ()
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [r for r in (_row_to_dict(row) for row in rows) if r]


async def publish_blog_post(db: aiosqlite.Connection, post_id: str) -> dict[str, Any] | None:
    cur = await db.execute(
        "UPDATE blog_posts SET status = 'published', "
        "published_at = COALESCE(published_at, datetime('now')), "
        "updated_at = datetime('now') WHERE id = ?",
        (post_id,),
    )
    await db.commit()
    if cur.rowcount == 0:
        return None
    return await get_blog_post(db, post_id)


async def unpublish_blog_post(db: aiosqlite.Connection, post_id: str) -> dict[str, Any] | None:
    cur = await db.execute(
        "UPDATE blog_posts SET status = 'draft', published_at = NULL, "
        "updated_at = datetime('now') WHERE id = ?",
        (post_id,),
    )
    await db.commit()
    if cur.rowcount == 0:
        return None
    return await get_blog_post(db, post_id)


async def delete_blog_post(db: aiosqlite.Connection, post_id: str) -> bool:
    cur = await db.execute("DELETE FROM blog_posts WHERE id = ?", (post_id,))
    await db.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Active worktrees — per-session git worktree tracking
# ---------------------------------------------------------------------------


async def register_worktree(
    db: aiosqlite.Connection,
    session_id: str,
    project_id: str,
    branch: str,
    path: str,
    item_id: str | None = None,
) -> dict[str, Any]:
    """Register a new active git worktree. Returns the inserted row."""
    wid = _new_id()
    await db.execute(
        "INSERT INTO active_worktrees (id, session_id, project_id, item_id, branch, path) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (wid, session_id, project_id, item_id, branch, path),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM active_worktrees WHERE id = ?", (wid,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


async def remove_worktree(
    db: aiosqlite.Connection,
    worktree_id: str,
) -> bool:
    """Mark a worktree as removed. Returns True when a row was updated."""
    cursor = await db.execute(
        "UPDATE active_worktrees SET removed_at = datetime('now') "
        "WHERE id = ? AND removed_at IS NULL",
        (worktree_id,),
    )
    await db.commit()
    return (cursor.rowcount or 0) > 0


async def list_active_worktrees(
    db: aiosqlite.Connection,
    project_id: str,
) -> list[dict[str, Any]]:
    """Return active (not removed) worktrees for a project, newest first."""
    async with db.execute(
        "SELECT aw.*, s.name AS session_name "
        "FROM active_worktrees aw "
        "LEFT JOIN sessions s ON s.id = aw.session_id "
        "WHERE aw.project_id = ? AND aw.removed_at IS NULL "
        "ORDER BY aw.created_at DESC",
        (project_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def get_active_worktree_for_session(
    db: aiosqlite.Connection,
    session_id: str,
) -> dict[str, Any] | None:
    """Return the most recent active worktree for a session, or None."""
    async with db.execute(
        "SELECT * FROM active_worktrees "
        "WHERE session_id = ? AND removed_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (session_id,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# v2.4 — Pinned decisions (editable constitution alongside the append-only log)
# ---------------------------------------------------------------------------

_SUGGESTED_DECISION_CATEGORIES = {
    "STRATEGIC", "COMPETITIVE", "TECHNICAL", "TACTICAL",
    "BUSINESS", "PRODUCT", "ARCHITECTURAL",
}

# 366317e9 — decision priority drives dashboard ordering + context-injection
# weight. urgent surfaces first, then normal, then low. Rank map is reused by
# get_pinned_decisions and the handoff/context-block sort keys.
_DECISION_PRIORITIES = ("urgent", "normal", "low")
_DECISION_PRIORITY_RANK = {"urgent": 0, "normal": 1, "low": 2}


def _normalize_decision_priority(priority: str | None) -> str:
    """Coerce an arbitrary priority into the {urgent, normal, low} set.

    Unknown/empty values normalize to 'normal' so a bad input never rejects a
    pin; the dashboard and context injection only understand the three levels.
    """
    p = (priority or "").strip().lower()
    return p if p in _DECISION_PRIORITY_RANK else "normal"


def _parse_decision_edit_log(raw: Any) -> list[dict[str, Any]]:
    """Parse the ``edit_log`` JSON blob into a list. NULL/empty/garbage → []."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _hydrate_decision_row(row: Any) -> dict[str, Any] | None:
    """Row → dict with ``priority`` defaulted and ``edit_log`` parsed to a list."""
    d = _row_to_dict(row)
    if d is None:
        return None
    d["priority"] = _normalize_decision_priority(d.get("priority"))
    d["edit_log"] = _parse_decision_edit_log(d.get("edit_log"))
    # 2b39549d — always surface the assumption fields (keys present even on
    # pre-migration rows). A decision with an assumption but no recorded status
    # is treated as 'unvalidated'.
    _assump = d.get("assumption")
    d["assumption"] = _assump
    d["assumption_status"] = d.get("assumption_status") or (
        "unvalidated" if _assump else None
    )
    return d


async def pin_decision(
    db: aiosqlite.Connection,
    project_id: str,
    title: str,
    body: str,
    category: str = "TECHNICAL",
    priority: str = "normal",
    assumption: str | None = None,
) -> dict[str, Any]:
    """Create a new pinned decision row. Returns the inserted row.

    Pinned decisions live alongside the append-only ``projects.decisions``
    log. Use this for the "current truth" set that supersedes earlier
    statements (pricing tiers, driver choices, etc). The log captures
    micro-decisions; this captures the constitution.

    category is free-text; suggested values: STRATEGIC, COMPETITIVE, TECHNICAL,
    TACTICAL, BUSINESS, PRODUCT, ARCHITECTURAL.

    priority (366317e9) is one of urgent | normal | low (default normal);
    invalid values normalize to 'normal'. It weights dashboard ordering and the
    decisions injected into start_session / generate_handoff context.
    """
    did = _new_id()
    priority = _normalize_decision_priority(priority)
    # 2b39549d — an assumption starts life 'unvalidated'; no assumption → NULL.
    _assump = (assumption or "").strip() or None
    _assump_status = "unvalidated" if _assump else None
    await db.execute(
        "INSERT INTO decisions_pinned "
        "(id, project_id, title, body, category, priority, assumption, assumption_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (did, project_id, title, body, category, priority, _assump, _assump_status),
    )
    await db.commit()
    # ITEM 6 — live push so the constitution view refreshes without polling.
    _publish_project_event(project_id, "decision_pinned", {"decision_id": did})
    return (await get_pinned_decision(db, did)) or {"id": did}


async def get_pinned_decision(
    db: aiosqlite.Connection, decision_id: str
) -> dict[str, Any] | None:
    async with db.execute(
        "SELECT * FROM decisions_pinned WHERE id = ?", (decision_id,)
    ) as cur:
        row = await cur.fetchone()
    return _hydrate_decision_row(row)


async def get_pinned_decisions(
    db: aiosqlite.Connection,
    project_id: str,
    include_superseded: bool = False,
) -> list[dict[str, Any]]:
    """Return all pinned decisions for a project, highest priority first.

    Rows are ordered urgent → normal → low, then newest-first within a
    priority band, so the dashboard and context injection both surface the
    most important decisions at the top. Each row carries a normalized
    ``priority`` and a parsed ``edit_log`` list (366317e9).

    Defaults to active only — superseded entries stay in history but
    are filtered out of the live constitution view.
    """
    if include_superseded:
        sql = (
            "SELECT * FROM decisions_pinned WHERE project_id = ? "
            "ORDER BY created_at DESC"
        )
        args = (project_id,)
    else:
        sql = (
            "SELECT * FROM decisions_pinned WHERE project_id = ? "
            "AND status = 'active' ORDER BY created_at DESC"
        )
        args = (project_id,)
    async with db.execute(sql, args) as cur:
        rows = await cur.fetchall()
    decisions = [_hydrate_decision_row(r) for r in rows if r is not None]
    # Stable sort by priority rank keeps the SQL newest-first order within each
    # band. urgent (0) < normal (1) < low (2).
    decisions.sort(key=lambda d: _DECISION_PRIORITY_RANK.get(d["priority"], 1))  # type: ignore[index,union-attr]
    return decisions  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 0b711a9d — strategic insights (first-class knowledge type)
# ---------------------------------------------------------------------------

_INSIGHT_HORIZONS = ("permanent", "year", "quarter")


async def create_insight(
    db: aiosqlite.Connection,
    project_id: str,
    title: str,
    body: str,
    horizon: str = "quarter",
    tags: "list | str | None" = None,
) -> dict[str, Any]:
    """Create a durable strategic insight. horizon is coerced to a valid value
    (permanent|year|quarter, default quarter). tags may be a list or a
    comma-string; stored comma-joined."""
    iid = _new_id()
    _horizon = horizon if horizon in _INSIGHT_HORIZONS else "quarter"
    if isinstance(tags, (list, tuple)):
        _tags = ",".join(str(t).strip() for t in tags if str(t).strip()) or None
    else:
        _tags = (str(tags).strip() or None) if tags else None
    await db.execute(
        "INSERT INTO insights (id, project_id, title, body, horizon, tags) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (iid, project_id, title, body or "", _horizon, _tags),
    )
    await db.commit()
    _publish_project_event(project_id, "insight_added", {"insight_id": iid, "horizon": _horizon})
    return (await get_insight(db, iid)) or {"id": iid}


async def get_insight(db: aiosqlite.Connection, insight_id: str) -> dict[str, Any] | None:
    """Fetch a single insight row by id."""
    async with db.execute("SELECT * FROM insights WHERE id = ?", (insight_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_insights(
    db: aiosqlite.Connection,
    project_id: str,
    horizon: str | None = None,
) -> list[dict[str, Any]]:
    """Return active insights for a project, newest first. Optional horizon filter
    (permanent|year|quarter); any other value is ignored (returns all horizons)."""
    if horizon in _INSIGHT_HORIZONS:
        sql = (
            "SELECT * FROM insights WHERE project_id = ? AND horizon = ? "
            "AND status = 'active' ORDER BY created_at DESC"
        )
        args: tuple[Any, ...] = (project_id, horizon)
    else:
        sql = (
            "SELECT * FROM insights WHERE project_id = ? AND status = 'active' "
            "ORDER BY created_at DESC"
        )
        args = (project_id,)
    async with db.execute(sql, args) as cur:
        rows = await cur.fetchall()
    return [d for d in (_row_to_dict(r) for r in rows) if d is not None]


async def get_decisions_for_file(
    db: aiosqlite.Connection,
    project_id: str,
    file_path: str,
) -> list[dict[str, Any]]:
    """Return active decisions with a code_anchor matching ``file_path``.

    777f26b0 — decisions with a ``code_anchor`` set are surfaced automatically
    when an executor calls ``claim_file`` for a matching path, so architectural
    decisions relevant to the file are injected into the executor's context
    before it edits. Only active decisions are returned; superseded ones are
    excluded. Returns an empty list when no matches or when the decisions_pinned
    table has no code_anchor column (pre-migration DBs).
    """
    normalized = _normalize_file_path(file_path)
    if not normalized:
        return []
    try:
        async with db.execute(
            "SELECT * FROM decisions_pinned "
            "WHERE project_id = ? AND status = 'active' "
            "AND code_anchor IS NOT NULL AND code_anchor != '' "
            "AND code_anchor = ? "
            "ORDER BY created_at DESC",
            (project_id, normalized),
        ) as cur:
            rows = await cur.fetchall()
    except Exception:
        # Column may not exist yet on pre-migration DBs — degrade gracefully.
        return []
    return [d for d in (_hydrate_decision_row(r) for r in rows) if d is not None]


async def update_pinned_decision(
    db: aiosqlite.Connection,
    decision_id: str,
    *,
    body: str | None = None,
    category: str | None = None,
    title: str | None = None,
    status: str | None = None,
    superseded_by: str | None = None,
    priority: str | None = None,
    assumption: str | None = None,
    assumption_status: str | None = None,
) -> dict[str, Any] | None:
    """Patch any combination of body / category / title / status / superseded_by /
    priority / assumption / assumption_status.

    Use ``status='superseded'`` + ``superseded_by=<new_id>`` to retire a
    decision while preserving the audit trail. Pass only the fields you
    intend to change; others stay untouched.

    366317e9 — when ``body`` is patched to a *different* value, the previous
    body is appended to the append-only ``edit_log`` JSON array as
    ``{"body": <previous body>, "ts": <iso timestamp>}`` BEFORE the row is
    overwritten. History is never dropped; multiple edits accumulate.
    ``priority`` is normalized to {urgent, normal, low} (invalid → 'normal').
    """
    from datetime import datetime, timezone
    existing = await get_pinned_decision(db, decision_id)
    if existing is None:
        return None
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    fields: dict[str, Any] = {}
    if body is not None:
        fields["body"] = body
        # Append-only edit history: snapshot the prior body before overwriting,
        # but only when the body actually changed (no-op edits don't pollute it).
        if body != existing.get("body"):
            history = _parse_decision_edit_log(existing.get("edit_log"))
            history.append({"body": existing.get("body"), "ts": now_iso})
            fields["edit_log"] = json.dumps(history)
    if title is not None:
        fields["title"] = title
    if category is not None:
        fields["category"] = category
    if priority is not None:
        fields["priority"] = _normalize_decision_priority(priority)
    if status is not None:
        if status not in ("active", "superseded"):
            raise ValueError("status must be 'active' or 'superseded'")
        fields["status"] = status
    if superseded_by is not None:
        fields["superseded_by"] = superseded_by
    # 2b39549d — assumption text + validation state.
    if assumption is not None:
        fields["assumption"] = assumption.strip() or None
    if assumption_status is not None:
        if assumption_status not in ("unvalidated", "confirmed", "invalidated"):
            raise ValueError(
                "assumption_status must be unvalidated|confirmed|invalidated"
            )
        fields["assumption_status"] = assumption_status
    if not fields:
        return existing
    fields["updated_at"] = now_iso
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    args = list(fields.values()) + [decision_id]
    await db.execute(
        f"UPDATE decisions_pinned SET {set_clause} WHERE id = ?", args
    )
    await db.commit()
    return await get_pinned_decision(db, decision_id)


async def supersede_pinned_decision(
    db: aiosqlite.Connection,
    old_decision_id: str,
    new_title: str,
    new_body: str,
    category: str | None = None,
    priority: str | None = None,
) -> dict[str, Any]:
    """Atomic supersede: create a new active decision and mark the old as superseded.

    Returns the new decision row. The old row keeps the back-link via
    ``superseded_by`` so the dashboard can render the chain. The new row inherits
    the old decision's priority unless ``priority`` overrides it (366317e9).
    """
    old = await get_pinned_decision(db, old_decision_id)
    if old is None:
        raise ValueError("decision not found")
    new = await pin_decision(
        db,
        old["project_id"],
        new_title,
        new_body,
        category or old.get("category", "TECHNICAL"),
        priority or old.get("priority", "normal"),
    )
    await update_pinned_decision(
        db, old_decision_id, status="superseded", superseded_by=new["id"]
    )
    return new


async def count_decisions(db: aiosqlite.Connection, project_id: str) -> int:
    """Return number of active pinned decisions for a project."""
    async with db.execute(
        "SELECT COUNT(*) FROM decisions_pinned WHERE project_id = ? AND status = 'active'",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    return (row[0] if row else 0) or 0


async def delete_pinned_decision(
    db: aiosqlite.Connection, decision_id: str
) -> bool:
    """Hard-delete a pinned decision by id. Returns True if deleted, False if not found."""
    cur = await db.execute(
        "DELETE FROM decisions_pinned WHERE id = ?", (decision_id,)
    )
    await db.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# v2.4 — HITL (human-in-the-loop) request queue
# ---------------------------------------------------------------------------

_VALID_HITL_URGENCY = {"normal", "high", "blocking"}
_VALID_HITL_STATUS = {"pending", "answered", "dismissed"}


async def request_hitl(
    db: aiosqlite.Connection,
    project_id: str,
    question: str,
    *,
    session_id: str | None = None,
    context: str | None = None,
    urgency: str = "normal",
    assigned_to: str | None = None,
    kind: str = "question",
    payload: str | None = None,
    options: list[str] | None = None,
    recommended: str | int | None = None,
    require_human: bool = False,
) -> dict[str, Any]:
    """Create a HITL request. Returns the inserted row.

    Sessions paused on ``urgency='blocking'`` should poll
    :func:`get_hitl_request` until ``status='answered'`` to receive
    the human's reply. Non-blocking requests still show up in the
    dashboard queue — callers can keep working and check later.

    ``options`` renders selectable answer buttons in the dashboard. ``recommended``
    (an option string, or a 0-based index into ``options``) flags the safe default:
    the dashboard highlights it with a "(recommended)" badge + border and Enter
    submits it, and an auto-answer picks it instead of the first option. Explicit
    ``payload`` JSON, when given, is merged with the options/recommended fields.
    """
    if urgency not in _VALID_HITL_URGENCY:
        raise ValueError(
            f"urgency must be one of {sorted(_VALID_HITL_URGENCY)}; got {urgency!r}"
        )
    # cd134cf1 — fold options + recommended into the payload JSON. e43e6941 —
    # also persist require_human there (no migration) so the dashboard can flag a
    # human-only request and the no-auto-answer rule survives a reload.
    if options is not None or recommended is not None or require_human:
        try:
            _pl = json.loads(payload) if payload else {}
            if not isinstance(_pl, dict):
                _pl = {}
        except (TypeError, ValueError):
            _pl = {}
        _opts = [str(o) for o in options] if options is not None else _pl.get("options")
        if _opts is not None:
            _pl["options"] = _opts
        _rec = _resolve_recommended_option(_opts, recommended)
        if _rec is not None:
            _pl["recommended"] = _rec
        if require_human:
            _pl["require_human"] = True
        payload = json.dumps(_pl)
    hid = _new_id()
    await db.execute(
        "INSERT INTO hitl_requests "
        "(id, project_id, session_id, question, context, urgency, assigned_to, "
        "kind, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (hid, project_id, session_id, question, context, urgency, assigned_to,
         kind, payload),
    )
    await db.commit()
    # ITEM 6 — live push so the HITL queue refreshes without polling.
    _publish_project_event(project_id, "hitl_filed", {"hitl_id": hid, "kind": kind})
    # e43e6941 — require_human forbids auto-answer entirely; reserve it for
    # genuinely irreversible/destructive actions (token rotation, data migrations,
    # rollbacks) so the auto-answer can never approve something that can't be
    # undone. Otherwise apply the per-project 3-way mode (035edf47); an
    # auto-answered row stays in the queue (status='answered', answered_by='auto')
    # for audit. See _hitl_should_auto_answer for the rules.
    if not require_human:
        _aa_mode = await _project_hitl_auto_answer_mode(db, project_id)
        if _hitl_should_auto_answer(_aa_mode, kind, question):
            auto = _auto_hitl_answer(payload)
            answered = await answer_hitl_request(db, hid, auto, answered_by="auto")
            if answered is not None:
                return answered
    return (await get_hitl_request(db, hid)) or {"id": hid}


async def _project_hitl_auto_answer_mode(
    db: aiosqlite.Connection, project_id: str
) -> int:
    """Per-project HITL auto-answer mode: 0=off, 1=safe, 2=aggressive."""
    async with db.execute(
        "SELECT hitl_auto_answer FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return 0
    data = _row_to_dict(row) or {}
    try:
        return max(0, min(2, int(data.get("hitl_auto_answer") or 0)))
    except (TypeError, ValueError):
        return 1 if data.get("hitl_auto_answer") else 0


# 035edf47 — keyword guards for the 3-way HITL auto-answer modes.
_HITL_DESTRUCTIVE_KEYWORDS = (
    "delete", "drop ", "truncate", "destroy", "wipe", "purge", "nuke",
    "production", "prod ", "remove", "revoke", "rm -rf", "force push",
    "force-push", "overwrite", "reset --hard",
)
_HITL_SECURITY_KEYWORDS = (
    "security", "secret", "credential", "password", "api key", "apikey",
    "private key", "token", "encrypt", "permission", "vulnerab", "auth ",
)


def _hitl_should_auto_answer(mode: int, kind: str, question: str) -> bool:
    """Decide whether a HITL of ``kind``/``question`` auto-answers under ``mode``.

    0 (off): never. 1 (safe): only an executor ``question`` with no destructive
    keyword — correction / md_section_update / hook_cwd_mismatch stay human-gated.
    2 (aggressive): everything except ``correction`` and security-sensitive text.
    """
    if mode <= 0:
        return False
    q = (question or "").lower()
    if mode == 1:  # safe
        if (kind or "question") != "question":
            return False
        return not any(k in q for k in _HITL_DESTRUCTIVE_KEYWORDS)
    # mode >= 2: aggressive
    if (kind or "") == "correction":
        return False
    return not any(k in q for k in _HITL_SECURITY_KEYWORDS)


def _resolve_recommended_option(
    options: list[str] | None, recommended: str | int | None
) -> str | None:
    """Resolve ``recommended`` (option string or 0-based index) to an option string.

    Returns None when there's nothing valid to recommend. A bool is rejected
    (``True``/``False`` are ints in Python but never a meaningful option index).
    """
    if recommended is None or isinstance(recommended, bool):
        return None
    if isinstance(recommended, int):
        if options and 0 <= recommended < len(options):
            return str(options[recommended])
        return None
    rec = str(recommended)
    if options and rec in {str(o) for o in options}:
        return rec
    # A free-text recommendation with no options list is still meaningful.
    return rec if not options else None


def _auto_hitl_answer(payload: str | None) -> str:
    """Derive the auto-answer string. Prefer an explicit ``recommended`` option;
    else the first of a non-empty ``options`` list; else a generic ack."""
    if payload:
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            rec = parsed.get("recommended")
            if isinstance(rec, str) and rec:
                return rec
            options = parsed.get("options")
            if isinstance(options, list) and options:
                return str(options[0])
    return "[auto-answered]"


async def get_hitl_request(
    db: aiosqlite.Connection, request_id: str
) -> dict[str, Any] | None:
    async with db.execute(
        "SELECT * FROM hitl_requests WHERE id = ?", (request_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def list_hitl_requests(
    db: aiosqlite.Connection,
    project_id: str | None = None,
    *,
    status: str | None = "pending",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return HITL requests, newest first.

    ``project_id=None`` returns across all projects — used by the
    dashboard's top-level HITL panel. ``status=None`` returns every
    status; default ``'pending'`` shows only the active queue.
    ``status='recent'`` returns pending + answered/dismissed in the last 24h.
    """
    # dcf1e428 — 'recent' pseudo-status: pending OR resolved in last 24h
    if status == "recent":
        from datetime import datetime, timezone, timedelta
        cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        where = []
        args: list[Any] = []
        if project_id is not None:
            where.append("project_id = ?")
            args.append(project_id)
        proj_clause = (" AND ".join(where) + " AND ") if where else ""
        args.extend([cutoff_24h, limit])
        sql = (
            f"SELECT * FROM hitl_requests WHERE {proj_clause}"
            "(status = 'pending' OR "
            " (status IN ('answered', 'dismissed') AND "
            "  COALESCE(answered_at, created_at) >= ?)) "
            "ORDER BY "
            "  CASE urgency WHEN 'blocking' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, "
            "  created_at DESC LIMIT ?"
        )
        async with db.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]
    if status is not None and status not in _VALID_HITL_STATUS:
        raise ValueError(
            f"status must be one of {sorted(_VALID_HITL_STATUS)} or None"
        )
    where2 = []
    args2: list[Any] = []
    if project_id is not None:
        where2.append("project_id = ?")
        args2.append(project_id)
    if status is not None:
        where2.append("status = ?")
        args2.append(status)
    where_clause = (" WHERE " + " AND ".join(where2)) if where2 else ""
    args2.append(limit)
    sql = (
        f"SELECT * FROM hitl_requests{where_clause} "
        "ORDER BY "
        "  CASE urgency WHEN 'blocking' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, "
        "  created_at DESC LIMIT ?"
    )
    async with db.execute(sql, args2) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def answer_hitl_request(
    db: aiosqlite.Connection,
    request_id: str,
    answer: str,
    answered_by: str | None = None,
) -> dict[str, Any] | None:
    """Mark a HITL request answered. Sessions polling for the answer pick it up."""
    from datetime import datetime, timezone
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE hitl_requests SET status = 'answered', answer = ?, "
        "answered_by = ?, answered_at = ? WHERE id = ?",
        (answer, answered_by, now_ts, request_id),
    )
    await db.commit()
    return await get_hitl_request(db, request_id)


async def dismiss_hitl_request(
    db: aiosqlite.Connection, request_id: str
) -> dict[str, Any] | None:
    """Mark a HITL request dismissed (won't-answer). Stays in audit trail."""
    await db.execute(
        "UPDATE hitl_requests SET status = 'dismissed' WHERE id = ?",
        (request_id,),
    )
    await db.commit()
    return await get_hitl_request(db, request_id)


# ---------------------------------------------------------------------------
# v2.4 — Project token for webhook intake (framework integrations)
# ---------------------------------------------------------------------------


async def ensure_project_token(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the project's webhook token, minting one if not yet set.

    Returns ``None`` if the project doesn't exist. Tokens are 32 bytes
    of url-safe base64 (~43 chars). Stored plain — these tokens grant
    write-access to a single project's task_log only, not to tenants.
    """
    project = await get_project(db, project_id)
    if project is None:
        return None
    token = project.get("project_token")
    if token:
        return token
    import secrets
    token = "pt_" + secrets.token_urlsafe(32)
    await db.execute(
        "UPDATE projects SET project_token = ? WHERE id = ?",
        (token, project_id),
    )
    await db.commit()
    return token


# ---------------------------------------------------------------------------
# v3.0 — Executor runs: one row per session execution, with transcript
# ---------------------------------------------------------------------------


async def create_executor_run(
    db: aiosqlite.Connection,
    session_id: str,
    project_id: str,
) -> dict[str, Any]:
    """Create a new executor_run row for a session. Called at session start."""
    rid = _new_id()
    await db.execute(
        "INSERT INTO executor_runs (id, session_id, project_id) VALUES (?, ?, ?)",
        (rid, session_id, project_id),
    )
    await db.commit()
    async with db.execute("SELECT * FROM executor_runs WHERE id = ?", (rid,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


async def _get_active_run_id(
    db: aiosqlite.Connection, session_id: str
) -> str | None:
    """Return the id of the active (running) executor_run for a session, if any."""
    async with db.execute(
        "SELECT id FROM executor_runs WHERE session_id = ? AND status = 'running' "
        "ORDER BY started_at DESC LIMIT 1",
        (session_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return row["id"] if isinstance(row, dict) else row[0]


async def append_executor_run_transcript(
    db: aiosqlite.Connection,
    session_id: str,
    entry: str,
) -> None:
    """Append a line to the running executor_run's transcript. No-op if no active run."""
    run_id = await _get_active_run_id(db, session_id)
    if run_id is None:
        return
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {entry}\n"
    await db.execute(
        "UPDATE executor_runs SET transcript = transcript || ?, task_count = task_count + 1 "
        "WHERE id = ?",
        (line, run_id),
    )
    await db.commit()


async def finalize_executor_run(
    db: aiosqlite.Connection,
    session_id: str,
    status: str = "done",
) -> None:
    """Mark the active executor_run for a session as finished."""
    run_id = await _get_active_run_id(db, session_id)
    if run_id is None:
        return
    from datetime import datetime, timezone
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE executor_runs SET status = ?, ended_at = ? WHERE id = ?",
        (status, now_ts, run_id),
    )
    await db.commit()


async def get_executor_runs(
    db: aiosqlite.Connection,
    project_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List executor_runs for a project, newest first."""
    async with db.execute(
        "SELECT er.*, s.name AS session_name, s.status AS session_status FROM executor_runs er "
        "LEFT JOIN sessions s ON s.id = er.session_id "
        "WHERE er.project_id = ? "
        "ORDER BY er.started_at DESC LIMIT ?",
        (project_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def get_executor_run(
    db: aiosqlite.Connection,
    run_id: str,
) -> dict[str, Any] | None:
    """Return a single executor_run by id."""
    async with db.execute(
        "SELECT * FROM executor_runs WHERE id = ?", (run_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_executor_run_by_session(
    db: aiosqlite.Connection,
    session_id: str,
) -> dict[str, Any] | None:
    """Return the most recent executor_run for a session (any status)."""
    async with db.execute(
        "SELECT * FROM executor_runs WHERE session_id = ? "
        "ORDER BY started_at DESC LIMIT 1",
        (session_id,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_project_by_token(
    db: aiosqlite.Connection, token: str
) -> dict[str, Any] | None:
    """Look up a project by its webhook token (for POST /events auth)."""
    async with db.execute(
        "SELECT * FROM projects WHERE project_token = ?", (token,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# v2.4 — Per-human team summary (Team tab data source)
# ---------------------------------------------------------------------------


async def get_team_summary(
    db: aiosqlite.Connection,
    project_id: str | None = None,
    days: int = 1,
) -> dict[str, Any]:
    """Aggregate task_log + sessions by human_id over the last N days.

    Returns ``{"period_days": N, "humans": [{"human_id", "tasks_done",
    "tasks_failed", "tasks_pending", "last_seen", "active_session",
    "recent": [...]}], "active_count": N}``. Powers the dashboard Team
    tab — live presence cards, standup digests, swimlane data source.
    """
    from datetime import datetime, timezone, timedelta
    since = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).strftime("%Y-%m-%d %H:%M:%S")

    proj_clause = "AND s.project_id = ?" if project_id else ""
    proj_args: tuple[Any, ...] = (project_id,) if project_id else ()

    # Pull sessions joined with tasks (over the window) grouped by human_id.
    sql = f"""
        SELECT
            s.human_id as human_id,
            s.id as session_id,
            s.name as session_name,
            s.status as session_status,
            s.last_seen as last_seen,
            s.agent_framework as agent_framework,
            t.id as task_id,
            t.description as task_description,
            t.status as task_status,
            t.created_at as task_created_at
        FROM sessions s
        LEFT JOIN task_log t
            ON t.session_id = s.id AND t.created_at >= ?
        WHERE s.human_id IS NOT NULL {proj_clause}
        ORDER BY s.last_seen DESC, t.created_at DESC
    """
    async with db.execute(sql, (since, *proj_args)) as cur:
        rows = await cur.fetchall()

    humans: dict[str, dict[str, Any]] = {}
    for r in rows:
        rd = _row_to_dict(r) or {}
        hid = rd.get("human_id")
        if not hid:
            continue
        bucket = humans.setdefault(
            hid,
            {
                "human_id": hid,
                "tasks_done": 0,
                "tasks_failed": 0,
                "tasks_pending": 0,
                "last_seen": rd.get("last_seen"),
                "active_session": rd.get("session_name"),
                "active_session_id": rd.get("session_id"),
                "session_status": rd.get("session_status"),
                "agent_framework": rd.get("agent_framework") or "claude_code",
                "recent": [],
            },
        )
        # First row per human (sorted by last_seen DESC) already gives us
        # the most recent session — don't overwrite.
        status = rd.get("task_status")
        if status == "done":
            bucket["tasks_done"] += 1
        elif status == "failed":
            bucket["tasks_failed"] += 1
        elif status in ("pending", "pending-hitl", "in_progress"):
            bucket["tasks_pending"] += 1
        desc = rd.get("task_description")
        if desc and len(bucket["recent"]) < 100:
            bucket["recent"].append(
                {
                    "description": desc,
                    "status": status,
                    "created_at": rd.get("task_created_at"),
                }
            )

    now_utc = datetime.now(timezone.utc)
    active_count = 0
    for h in humans.values():
        last_seen = h.get("last_seen") or ""
        try:
            ls_dt = datetime.fromisoformat(last_seen.replace(" ", "T")).replace(
                tzinfo=timezone.utc
            )
            age = (now_utc - ls_dt).total_seconds()
            if age < 300:
                h["presence"] = "active"
                active_count += 1
            elif age < 1800:
                h["presence"] = "recent"
            else:
                h["presence"] = "idle"
        except (ValueError, AttributeError):
            h["presence"] = "idle"

    return {
        "period_days": days,
        "humans": sorted(humans.values(), key=lambda x: x.get("last_seen") or "", reverse=True),
        "active_count": active_count,
    }


# ---------------------------------------------------------------------------
# v0.9 — project_notes: per-project wiki
# ---------------------------------------------------------------------------


async def _unique_note_slug(
    db: aiosqlite.Connection,
    project_id: str,
    base: str,
    exclude_id: str | None = None,
) -> str:
    """Return ``base``, or base-2/base-3/… if the slug is taken in this project.

    Slugs are unique *per project* (Obsidian ``mem:name`` style), so the same
    slug may exist under different projects. ``exclude_id`` lets a row keep its
    own slug on update without colliding with itself.
    """
    slug = base
    n = 1
    while True:
        async with db.execute(
            "SELECT id FROM project_notes WHERE project_id = ? AND slug = ?",
            (project_id, slug),
        ) as cur:
            row = await cur.fetchone()
        existing = _row_to_dict(row)
        if existing is None or existing.get("id") == exclude_id:
            return slug
        n += 1
        slug = f"{base}-{n}"


async def add_project_note(
    db: aiosqlite.Connection,
    project_id: str,
    title: str,
    body: str,
    tags: str | None = None,
    kind: str | None = None,
    priority: str = "normal",
    file_path: str | None = None,
    symbol: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Insert a project_notes row. tags is comma-separated free-form.

    ``kind`` is the note taxonomy (wiki | insight | reference | code |
    document); NULL is treated as 'wiki' by readers. Unknown values are coerced
    to NULL so the column stays a closed vocabulary.

    e3f150d0 — ``source`` records where a note was ingested from (a URL or file
    path), set by ``ingest_document`` for ``kind='document'`` notes. Nullable;
    NULL for normal notes.

    ``priority`` is high/normal/low; defaults to 'normal'. High-priority notes
    are surfaced first in generate_handoff and get_session_brief (planner role).

    771c00d7 — a code-anchored note pins a warning/context to a file: pass
    ``kind='code'`` plus a non-empty ``file_path`` (and optional ``symbol``).
    The path is normalized the same way ``claim_file`` stores it so the anchor
    matches a claim, and the note surfaces automatically at claim_file /
    get_file_claims. ``file_path``/``symbol`` are NULL for normal notes.

    5a5bba43 — a kebab-cased ``slug`` is generated from the title and stored,
    unique per project (collisions get a ``-2``/``-3``/… suffix). The slug is the
    handle ``read_note(slug)`` and the dashboard's ``mem:name`` links resolve.
    """
    if kind not in ("wiki", "insight", "reference", "code", "document"):
        kind = None
    if priority not in ("high", "normal", "low"):
        priority = "normal"
    stored_source = (source or "").strip() or None
    # 771c00d7 — code anchor: require a real path, normalize it to match claims,
    # and keep ``symbol`` only alongside a path. Anchors are independent of kind
    # so an explicitly anchored note still resolves even if kind was coerced.
    anchor_path = _normalize_file_path(file_path)
    if file_path is not None and not anchor_path:
        raise ValueError("file_path must be a non-empty string when provided")
    sym = (symbol or "").strip()
    stored_path = anchor_path or None
    stored_symbol = sym or None
    if stored_path is None:
        stored_symbol = None  # a symbol is meaningless without a file anchor
    nid = _new_id()
    slug = await _unique_note_slug(db, project_id, _slugify_note(title))
    await db.execute(
        "INSERT INTO project_notes "
        "(id, project_id, title, body, tags, note_kind, priority, slug, "
        "file_path, symbol, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (nid, project_id, title, body, tags, kind, priority, slug,
         stored_path, stored_symbol, stored_source),
    )
    await db.commit()
    # ITEM 6 — live push so the Notes tab refreshes without polling.
    _publish_project_event(project_id, "note_added", {"note_id": nid})
    return (await get_project_note(db, nid)) or {"id": nid}


async def get_project_note(
    db: aiosqlite.Connection, note_id: str
) -> dict[str, Any] | None:
    async with db.execute(
        "SELECT * FROM project_notes WHERE id = ?", (note_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def ingest_document(
    db: aiosqlite.Connection,
    project_id: str,
    file_path: str | None = None,
    content: str | None = None,
    title: str | None = None,
    source: str | None = None,
    tags: str | None = None,
) -> dict[str, Any]:
    """e3f150d0 — turn a document into a queryable ``kind='document'`` note.

    Body selection:
      * ``content`` given  → use it verbatim (the caller extracted the text, e.g.
        from a PDF with its own tooling — Meridian never parses PDFs server-side).
      * else ``file_path`` → extract the text server-side with the stdlib-only
        ``doc_ingest.extract_text`` (.txt/.md/.docx). Unsupported types (.pdf, …)
        raise, telling the caller to pass ``content`` instead.
      * neither             → ``ValueError``.

    Defaults: ``title`` falls back to the file's basename (or "Untitled
    document"); ``source`` falls back to ``file_path``. The body is capped at
    ``doc_ingest.DOC_BODY_MAX_CHARS`` (truncated with a clear "…[truncated]"
    marker if longer; the kept prefix stays full-text searchable). The note is
    stored via :func:`add_project_note` with ``kind='document'`` and ``source``.

    Meridian is a coordination store, not an LLM: this never summarizes. Pass a
    summary as ``content`` if you want one stored instead of the raw text.
    """
    from ..doc_ingest import extract_text, cap_body  # local import: avoid cycle

    if content is not None and str(content).strip() != "":
        body = content
        ingest_source = source if (source and source.strip()) else (file_path or None)
    elif file_path and str(file_path).strip():
        # Server-side, stdlib-only extraction. extract_text raises a clear
        # UnsupportedDocumentError for .pdf and other unparseable types.
        body = extract_text(file_path)
        ingest_source = source if (source and source.strip()) else file_path
    else:
        raise ValueError(
            "ingest_document requires either 'content' (pre-extracted text) or "
            "'file_path' (a .txt/.md/.docx file to extract server-side)"
        )

    # Title defaults to the file's basename, else a generic placeholder.
    doc_title = (title or "").strip()
    if not doc_title:
        if file_path and str(file_path).strip():
            doc_title = os.path.basename(file_path.strip()) or "Untitled document"
        else:
            doc_title = "Untitled document"

    capped = cap_body(body or "")
    return await add_project_note(
        db,
        project_id,
        doc_title,
        capped,
        tags,
        kind="document",
        source=(ingest_source or None),
    )


def _project_notes_where(
    db: aiosqlite.Connection,
    project_id: str,
    tag: str | None,
    query: str | None,
) -> tuple[str, list[Any]]:
    """Build the shared WHERE clause + params for the project_notes filters.

    Factored out so get_project_notes and get_project_notes_page apply the
    exact same tag / full-text filtering before list vs. paginated fetch.
    """
    is_pg = hasattr(db, "_pool")
    clauses: list[str] = ["project_id = ?"]
    params: list[Any] = [project_id]
    if tag:
        clauses.append("tags LIKE ?")
        params.append(f"%{tag}%")
    if query:
        like_pat = f"%{query}%"
        if is_pg:
            clauses.append("(title ILIKE ? OR body ILIKE ?)")
        else:
            clauses.append("(title LIKE ? OR body LIKE ?)")
        params.extend([like_pat, like_pat])
    return " AND ".join(clauses), params


def _project_notes_cols(bodies: bool) -> str:
    """Column list for a notes fetch — full row when ``bodies`` else the
    lightweight id/slug/title/kind/priority/timestamps projection."""
    return (
        "*"
        if bodies
        else "id, project_id, title, tags, note_kind, priority, slug, "
        "created_at, updated_at"
    )


async def get_project_notes(
    db: aiosqlite.Connection,
    project_id: str,
    tag: str | None = None,
    query: str | None = None,
    bodies: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return notes for a project, newest first. Optional tag filter (substring
    match on tags) and/or text query (searches title + body).

    5a5bba43 — pull model: by default (``bodies=False``) the returned rows OMIT
    the ``body`` field — a lightweight id/slug/title/kind/priority/timestamps
    list — so bulk note injection can't overflow an agent's context. Callers
    that actually render note bodies (dashboard notes view, handoff, planner
    context) pass ``bodies=True``; agents fetch a single body on demand via
    ``read_note(slug)`` → ``get_project_note_by_slug``.

    The ``query`` filter always searches the body even when bodies are omitted
    from the result — the search happens in SQL, the body just isn't returned.

    9fa119dd — ``limit``/``offset`` add SQL LIMIT/OFFSET paging (clamped to
    1..500) mirroring get_sprint_items_page. ``limit=None`` (the default) keeps
    the legacy "return every matching row" behaviour for existing callers.
    """
    where, params = _project_notes_where(db, project_id, tag, query)
    cols = _project_notes_cols(bodies)
    sql = f"SELECT {cols} FROM project_notes WHERE {where} ORDER BY created_at DESC"
    if limit is not None:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        sql += " LIMIT ? OFFSET ?"
        params = [*params, limit, offset]
    async with db.execute(sql, params or None) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


def _note_relevance_score(
    reference_count: int, age_days: float, has_decision_link: bool, read_count: int = 0
) -> float:
    """98890df1 — PageRank-ish note relevance. Reference/read counts pass through a
    saturating transform so a heavily-referenced note ranks high without a single
    outlier dominating; recency decays on a ~30-day scale. read_count stays 0 until
    note-read tracking lands (semantic similarity is the pgvector Phase 2)."""
    ref = 1.0 - 1.0 / (1.0 + max(0, reference_count))
    rec = 1.0 / (1.0 + max(0.0, age_days) / 30.0)
    read = 1.0 - 1.0 / (1.0 + max(0, read_count))
    dec = 1.0 if has_decision_link else 0.0
    return 0.45 * ref + 0.30 * rec + 0.15 * read + 0.10 * dec


async def get_project_notes_ranked(
    db: aiosqlite.Connection,
    project_id: str,
    tag: str | None = None,
    query: str | None = None,
    bodies: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """98890df1 — like get_project_notes but ordered by a relevance score
    (reference_count / recency / decision-link) instead of pure recency, so
    heavily cross-referenced notes surface and stale ones sink. Each returned row
    carries a ``relevance`` float."""
    from datetime import datetime, timezone  # noqa: PLC0415
    where, params = _project_notes_where(db, project_id, tag, query)
    # Fetch full rows (bodies are needed to count [[slug]] references) unpaged so
    # scoring sees the whole corpus, then trim after ranking.
    sql = f"SELECT * FROM project_notes WHERE {where}"
    async with db.execute(sql, params or None) as cur:
        rows = await cur.fetchall()
    notes = [_row_to_dict(r) for r in rows if r is not None]
    combined_bodies = " ".join((n.get("body") or "") for n in notes)
    try:
        decisions = await get_pinned_decisions(db, project_id)
        decision_blob = " ".join((d.get("body") or "") for d in (decisions or []))
    except Exception:  # noqa: BLE001
        decision_blob = ""
    now = datetime.now(timezone.utc)
    for n in notes:
        slug = n.get("slug") or ""
        marker = f"[[{slug}]]"
        # count references from OTHER notes (subtract self-references in own body)
        ref_count = 0
        if slug:
            ref_count = combined_bodies.count(marker) - (n.get("body") or "").count(marker)
        has_dec = bool(slug) and (marker in decision_blob)
        age_days = 0.0
        created = n.get("created_at")
        if created:
            try:
                cdt = datetime.strptime(
                    str(created)[:19], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                age_days = max(0.0, (now - cdt).total_seconds() / 86400.0)
            except (ValueError, TypeError):
                pass
        n["relevance"] = round(
            _note_relevance_score(max(0, ref_count), age_days, has_dec), 4
        )
    notes.sort(key=lambda x: (x.get("relevance", 0.0), x.get("created_at") or ""), reverse=True)
    if not bodies:
        for n in notes:
            n.pop("body", None)
    if limit is not None:
        notes = notes[: max(1, min(int(limit), 500))]
    return notes


async def get_project_notes_page(
    db: aiosqlite.Connection,
    project_id: str,
    tag: str | None = None,
    query: str | None = None,
    bodies: bool = False,
    limit: int = 100,
    cursor: int = 0,
) -> dict[str, Any]:
    """9fa119dd — one cursor page of project notes (newest first) for the
    dashboard's "Load More" notes list.

    Mirrors get_sprint_items_page's offset paging, but returns the
    cursor envelope the dashboard / get_notes tool consume::

        {"notes": [...], "has_more": bool, "next_cursor": int | None}

    The cursor is the next offset (notes are ordered ``created_at DESC`` with no
    stable secondary key, so an offset cursor is the consistent mechanism — same
    as sprint items / tasks). One extra row is fetched internally to compute
    ``has_more`` without a second COUNT query; ``next_cursor`` is the offset to
    pass back for the following page, or ``None`` when the list is exhausted.

    ``tag``, ``query`` and ``bodies`` behave exactly as in get_project_notes.
    ``limit`` is clamped to 1..500.
    """
    limit = max(1, min(int(limit), 500))
    cursor = max(0, int(cursor))
    # Fetch limit+1: the extra row tells us another page exists without COUNT.
    # Build the query directly (not via get_project_notes) so the +1 probe row
    # isn't lost to that function's own 500-row clamp at the limit==500 boundary.
    where, params = _project_notes_where(db, project_id, tag, query)
    cols = _project_notes_cols(bodies)
    async with db.execute(
        f"SELECT {cols} FROM project_notes WHERE {where} "
        "ORDER BY created_at DESC LIMIT ? OFFSET ?",
        [*params, limit + 1, cursor],
    ) as cur:
        raw = await cur.fetchall()
    rows = [_row_to_dict(r) for r in raw if r is not None]  # type: ignore[misc]
    has_more = len(rows) > limit
    notes = rows[:limit]
    next_cursor = cursor + len(notes) if has_more else None
    return {"notes": notes, "has_more": has_more, "next_cursor": next_cursor}


async def get_project_note_by_slug(
    db: aiosqlite.Connection, project_id: str, slug: str
) -> dict[str, Any] | None:
    """5a5bba43 — fetch a single full note (incl. body) by its per-project slug.

    Backs the ``read_note(slug)`` MCP tool — the pull half of the list→read
    model. Returns None when no note with that slug exists in the project.

    f5f2a89d — includes ``referenced_by``: lightweight list of notes whose body
    contains ``[[slug]]`` linking back to this note.
    """
    async with db.execute(
        "SELECT * FROM project_notes WHERE project_id = ? AND slug = ?",
        (project_id, slug),
    ) as cur:
        row = await cur.fetchone()
    note = _row_to_dict(row)
    if note is not None:
        note["referenced_by"] = await get_notes_backlinked_to(db, project_id, slug)
    return note


async def update_project_note(
    db: aiosqlite.Connection,
    note_id: str,
    *,
    title: str | None = None,
    body: str | None = None,
    tags: str | None = None,
    priority: str | None = None,
) -> dict[str, Any] | None:
    """Patch any combination of title/body/tags/priority. Returns updated row."""
    existing = await get_project_note(db, note_id)
    if existing is None:
        return None
    fields: dict[str, Any] = {}
    if title is not None:
        fields["title"] = title
    if body is not None:
        fields["body"] = body
    if tags is not None:
        fields["tags"] = tags
    if priority is not None and priority in ("high", "normal", "low"):
        fields["priority"] = priority
    if not fields:
        return existing
    from datetime import datetime, timezone
    fields["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    args = list(fields.values()) + [note_id]
    await db.execute(
        f"UPDATE project_notes SET {set_clause} WHERE id = ?", args
    )
    await db.commit()
    return await get_project_note(db, note_id)


async def delete_project_note(
    db: aiosqlite.Connection, note_id: str
) -> bool:
    """Hard-delete a note. Returns True if a row was removed."""
    async with db.execute(
        "DELETE FROM project_notes WHERE id = ?", (note_id,)
    ) as cur:
        rc = cur.rowcount or 0
    await db.commit()
    return rc > 0


# ---------------------------------------------------------------------------
# v3.1 — workspace layer: tenant-global notes + decisions above projects
# ---------------------------------------------------------------------------


def _ws_tenant_clause(tenant_id: str | None) -> tuple[str, list[Any]]:
    """Return an ``AND (...)`` scope fragment + params for tenant isolation.

    When ``tenant_id`` is None (self-host / internal callers) returns ('', [])
    so behaviour is unchanged. When provided, rows owned by that tenant *or*
    pre-isolation rows (``tenant_id IS NULL``, only ever present on a dedicated
    per-tenant DB) match — see ``_migrate_workspace_tenant_isolation``.
    """
    if tenant_id is None:
        return "", []
    return "(tenant_id = ? OR tenant_id IS NULL)", [tenant_id]


async def add_workspace_note(
    db: aiosqlite.Connection,
    title: str,
    body: str,
    tags: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Insert a workspace_notes row. tags is comma-separated free-form.
    Workspace notes belong to the whole workspace, not a single project."""
    nid = _new_id()
    await db.execute(
        "INSERT INTO workspace_notes (id, title, body, tags, tenant_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (nid, title, body, tags, tenant_id),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM workspace_notes WHERE id = ?", (nid,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) or {"id": nid}


async def get_workspace_notes(
    db: aiosqlite.Connection,
    tag: str | None = None,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return workspace notes, newest first. Optional tag substring filter.
    Scoped to ``tenant_id`` when provided (hosted)."""
    clauses: list[str] = []
    params: list[Any] = []
    if tag:
        clauses.append("tags LIKE ?")
        params.append(f"%{tag}%")
    scope, scope_params = _ws_tenant_clause(tenant_id)
    if scope:
        clauses.append(scope)
        params.extend(scope_params)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    async with db.execute(
        f"SELECT * FROM workspace_notes{where} ORDER BY created_at DESC",
        params or None,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def delete_workspace_note(
    db: aiosqlite.Connection, note_id: str, tenant_id: str | None = None
) -> bool:
    """Hard-delete a workspace note. Returns True if a row was removed.
    Cannot delete another tenant's note when ``tenant_id`` is set."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    sql = "DELETE FROM workspace_notes WHERE id = ?" + (f" AND {scope}" if scope else "")
    async with db.execute(sql, [note_id, *scope_params]) as cur:
        rc = cur.rowcount or 0
    await db.commit()
    return rc > 0


async def move_workspace_note_to_project(
    db: aiosqlite.Connection,
    note_id: str,
    project_id: str,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Convert a workspace note into a project note on ``project_id``.

    Copies title/body/tags to a new project_notes row, then deletes the
    workspace note. Returns the new project note, or None if the workspace
    note was not found (or belongs to another tenant). Atomic: the delete
    only runs after the project note is created.
    """
    scope, scope_params = _ws_tenant_clause(tenant_id)
    sql = "SELECT * FROM workspace_notes WHERE id = ?" + (f" AND {scope}" if scope else "")
    async with db.execute(sql, [note_id, *scope_params]) as cur:
        row = await cur.fetchone()
    note = _row_to_dict(row) if row is not None else None
    if not note:
        return None
    # Guard against moving to a non-existent project.
    if await get_project(db, project_id) is None:
        return None
    created = await add_project_note(
        db,
        project_id,
        note.get("title") or "",
        note.get("body") or "",
        note.get("tags"),
    )
    await delete_workspace_note(db, note_id, tenant_id=tenant_id)
    return created


async def update_workspace_note(
    db: aiosqlite.Connection,
    note_id: str,
    title: str | None = None,
    body: str | None = None,
    tags: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Patch title/body/tags on an existing workspace note (tenant-scoped)."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    scope_sql = f" AND {scope}" if scope else ""
    sets, params = [], []
    if title is not None:
        sets.append("title = ?"); params.append(title)
    if body is not None:
        sets.append("body = ?"); params.append(body)
    if tags is not None:
        sets.append("tags = ?"); params.append(tags)
    if not sets:
        async with db.execute(
            f"SELECT * FROM workspace_notes WHERE id = ?{scope_sql}",
            [note_id, *scope_params],
        ) as cur:
            row = await cur.fetchone()
        return _row_to_dict(row)
    await db.execute(
        f"UPDATE workspace_notes SET {', '.join(sets)} WHERE id = ?{scope_sql}",
        [*params, note_id, *scope_params],
    )
    await db.commit()
    async with db.execute(
        f"SELECT * FROM workspace_notes WHERE id = ?{scope_sql}",
        [note_id, *scope_params],
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def pin_workspace_decision(
    db: aiosqlite.Connection,
    title: str,
    body: str,
    category: str = "TECHNICAL",
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Create a workspace-level pinned decision. category is free-text
    (STRATEGIC, TECHNICAL, PRODUCT, ...)."""
    did = _new_id()
    await db.execute(
        "INSERT INTO workspace_decisions (id, title, body, category, tenant_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (did, title, body, category, tenant_id),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM workspace_decisions WHERE id = ?", (did,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) or {"id": did}


async def get_workspace_decisions(
    db: aiosqlite.Connection,
    include_superseded: bool = False,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return workspace decisions, newest first. Active only by default.
    Scoped to ``tenant_id`` when provided (hosted)."""
    clauses: list[str] = []
    params: list[Any] = []
    if not include_superseded:
        clauses.append("status = 'active'")
    scope, scope_params = _ws_tenant_clause(tenant_id)
    if scope:
        clauses.append(scope)
        params.extend(scope_params)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    async with db.execute(
        f"SELECT * FROM workspace_decisions{where} ORDER BY created_at DESC",
        params or None,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def delete_workspace_decision(
    db: aiosqlite.Connection, decision_id: str, tenant_id: str | None = None
) -> bool:
    """Hard-delete a workspace decision. Returns True if a row was removed.
    Cannot delete another tenant's decision when ``tenant_id`` is set."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    sql = "DELETE FROM workspace_decisions WHERE id = ?" + (f" AND {scope}" if scope else "")
    async with db.execute(sql, [decision_id, *scope_params]) as cur:
        rc = cur.rowcount or 0
    await db.commit()
    return rc > 0


# --- Workspace sprint board (tenant-global personal backlog) ----------------
# A cross-project backlog that is NOT tied to any single project. Mirrors the
# useful subset of the per-project sprint_items shape but is keyed by tenant_id
# (see _ws_tenant_clause), exactly like workspace_notes / workspace_decisions.
# ``item_group`` is the cross-project bucket ('thesis'/'meridian'/'personal').

_VALID_WS_SPRINT_STATUSES = {
    "todo", "pending", "in_progress", "done", "skipped", "failed",
}


async def add_workspace_sprint_item(
    db: aiosqlite.Connection,
    title: str,
    item_group: str | None = None,
    human_id: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Append a ``todo`` item to the workspace-level personal backlog.

    ``item_group`` is the cross-project bucket the item lives under (e.g.
    'thesis' / 'meridian' / 'personal'); ``human_id`` attributes it to a
    person. Workspace sprint items belong to the whole workspace, not a single
    project, so there is no project_id. Scoped to ``tenant_id`` when provided
    (hosted); None on self-host."""
    iid = _new_id()
    # New items go to the end of their group (highest position + 1).
    scope, scope_params = _ws_tenant_clause(tenant_id)
    where = f" WHERE {scope}" if scope else ""
    async with db.execute(
        f"SELECT COALESCE(MAX(position), -1) + 1 AS next_pos "
        f"FROM workspace_sprint_items{where}",
        scope_params or None,
    ) as cur:
        prow = await cur.fetchone()
    next_pos = (prow["next_pos"] if isinstance(prow, dict) else prow[0]) or 0
    await db.execute(
        "INSERT INTO workspace_sprint_items "
        "(id, tenant_id, title, item_group, human_id, position) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (iid, tenant_id, title, item_group or None, human_id or None, next_pos),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM workspace_sprint_items WHERE id = ?", (iid,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) or {"id": iid}


async def get_workspace_sprint_items(
    db: aiosqlite.Connection,
    status: str | None = None,
    item_group: str | None = None,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """List workspace sprint items, grouped by ``item_group`` then position.

    Optional ``status`` and ``item_group`` filters. Scoped to ``tenant_id``
    when provided (hosted); None returns everything on self-host."""
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        if status not in _VALID_WS_SPRINT_STATUSES:
            raise ValueError(
                f"invalid workspace sprint-item status filter: {status!r}. "
                f"Valid: {sorted(_VALID_WS_SPRINT_STATUSES)}"
            )
        clauses.append("status = ?")
        params.append(status)
    if item_group is not None:
        clauses.append("item_group = ?")
        params.append(item_group)
    scope, scope_params = _ws_tenant_clause(tenant_id)
    if scope:
        clauses.append(scope)
        params.extend(scope_params)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    async with db.execute(
        f"SELECT * FROM workspace_sprint_items{where} "
        "ORDER BY item_group IS NULL, item_group ASC, position ASC, created_at ASC",
        params or None,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def update_workspace_sprint_item(
    db: aiosqlite.Connection,
    item_id: str,
    title: str | None = None,
    status: str | None = None,
    item_group: str | None = None,
    human_id: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Patch editable fields of a workspace sprint item (tenant-scoped).

    Editable: title, status, item_group, human_id. Only fields passed as
    non-None are changed. Pass an empty string to clear item_group/human_id.
    A terminal status of 'done'/'skipped'/'failed' stamps ``completed_at``;
    any other status clears it. Returns the updated item, or None if no row
    matched (unknown id, or another tenant's item)."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    scope_sql = f" AND {scope}" if scope else ""
    fields: list[str] = []
    values: list[Any] = []
    if title is not None:
        fields.append("title = ?")
        values.append(title)
    if status is not None:
        if status not in _VALID_WS_SPRINT_STATUSES:
            raise ValueError(f"invalid workspace sprint-item status: {status!r}")
        fields.append("status = ?")
        values.append(status)
        if status in {"done", "skipped", "failed"}:
            fields.append("completed_at = datetime('now')")
        else:
            fields.append("completed_at = NULL")
    if item_group is not None:
        fields.append("item_group = ?")
        values.append(item_group or None)
    if human_id is not None:
        fields.append("human_id = ?")
        values.append(human_id or None)
    if not fields:
        async with db.execute(
            f"SELECT * FROM workspace_sprint_items WHERE id = ?{scope_sql}",
            [item_id, *scope_params],
        ) as cur:
            row = await cur.fetchone()
        return _row_to_dict(row)
    fields.append("updated_at = datetime('now')")
    cursor = await db.execute(
        f"UPDATE workspace_sprint_items SET {', '.join(fields)} "
        f"WHERE id = ?{scope_sql}",
        [*values, item_id, *scope_params],
    )
    await db.commit()
    if cursor.rowcount == 0:
        return None
    async with db.execute(
        f"SELECT * FROM workspace_sprint_items WHERE id = ?{scope_sql}",
        [item_id, *scope_params],
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def complete_workspace_sprint_item(
    db: aiosqlite.Connection,
    item_id: str,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Mark a workspace sprint item ``done`` (stamps ``completed_at``).

    Returns the updated item, or None if no row matched (unknown id / wrong
    tenant)."""
    return await update_workspace_sprint_item(
        db, item_id, status="done", tenant_id=tenant_id
    )


_WORKSPACE_SETTINGS_ID = "singleton"


def _ws_settings_key(tenant_id: str | None) -> str:
    """Row key for the workspace_settings singleton.

    Hosted callers key by their tenant_id so internal accounts that share the
    control-plane DB get isolated settings; self-host keeps the legacy
    'singleton' row.
    """
    return tenant_id or _WORKSPACE_SETTINGS_ID


async def get_workspace_settings(
    db: aiosqlite.Connection, tenant_id: str | None = None
) -> dict[str, Any]:
    """Return the workspace-level settings (tenant-global defaults).

    Always returns a dict — defaults when no row has been written yet — so
    callers never have to None-check. One row per tenant (or the legacy
    'singleton' row in self-host).
    """
    _cols = (
        "SELECT hitl_auto_answer_default, sprint_name_default, display_name, "
        "log_task_sprint_nudge_threshold, handoff_template, "
        "execution_mode_default, code_intel_enabled_default, "
        "loop_enabled_default, auto_refresh_enabled, refresh_interval_turns, "
        "refresh_triggers, updated_at "
        "FROM workspace_settings"
    )
    async with db.execute(
        f"{_cols} WHERE id = ?", (_ws_settings_key(tenant_id),)
    ) as cur:
        row = await cur.fetchone()
    if row is None and tenant_id is None:
        # Internal/self-host caller with no tenant context. On a dedicated
        # per-tenant DB the sole settings row is keyed by that tenant's id, so
        # fall back to it when there is exactly one row. The shared control-plane
        # DB has many rows, so this safely no-ops there (returns defaults).
        async with db.execute(f"{_cols} LIMIT 2") as cur:
            some = await cur.fetchall()
        if len(some) == 1:
            row = some[0]
    data = _row_to_dict(row) or {}
    # 0bf67524 — cascade defaults. None ⇒ "no workspace default set" (new
    # projects keep their own built-in default); a value ⇒ seed new projects.
    _emode = data.get("execution_mode_default")
    _ci_default = data.get("code_intel_enabled_default")
    # 76cf8bda — /loop auto-continue workspace default. Missing column/row ⇒
    # True (Meridian sessions default to auto-continue); a stored 0 turns it off.
    _loop_default = data.get("loop_enabled_default")
    # bf51b12e — planner context-refresh config. refresh_triggers is a JSON list
    # (NULL ⇒ None ⇒ hook uses its built-in default trigger set).
    _refresh_triggers_raw = data.get("refresh_triggers")
    _refresh_triggers: list[str] | None = None
    if _refresh_triggers_raw:
        try:
            _decoded = json.loads(_refresh_triggers_raw)
            if isinstance(_decoded, list):
                _refresh_triggers = [str(t) for t in _decoded]
        except Exception:  # noqa: BLE001 — malformed row ⇒ fall back to default
            _refresh_triggers = None
    _interval = data.get("refresh_interval_turns")
    return {
        "hitl_auto_answer_default": bool(data.get("hitl_auto_answer_default")),
        "sprint_name_default": data.get("sprint_name_default"),
        "display_name": data.get("display_name"),
        "log_task_sprint_nudge_threshold": int(data["log_task_sprint_nudge_threshold"])
        if data.get("log_task_sprint_nudge_threshold") is not None
        else 5,
        "handoff_template": data.get("handoff_template"),
        "execution_mode_default": _emode if _emode in ("autonomous", "interactive") else None,
        "code_intel_enabled_default": (None if _ci_default is None else bool(_ci_default)),
        "loop_enabled_default": (True if _loop_default is None else bool(_loop_default)),
        "auto_refresh_enabled": bool(data.get("auto_refresh_enabled")),
        "refresh_interval_turns": (int(_interval) if _interval is not None else 10) or 10,
        "refresh_triggers": _refresh_triggers,
        "updated_at": data.get("updated_at"),
    }


async def update_workspace_settings(
    db: aiosqlite.Connection,
    *,
    hitl_auto_answer_default: bool | None = None,
    sprint_name_default: str | None = None,
    display_name: str | None = None,
    log_task_sprint_nudge_threshold: int | None = None,
    handoff_template: str | None = None,
    execution_mode_default: str | None = None,
    code_intel_enabled_default: "bool | int | str | None" = None,
    loop_enabled_default: "bool | int | str | None" = None,
    auto_refresh_enabled: "bool | int | str | None" = None,
    refresh_interval_turns: int | None = None,
    refresh_triggers: "list[str] | str | None" = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Upsert the per-tenant workspace settings row and return the new values.

    Only the fields passed (non-None) are changed. ``sprint_name_default=""``
    or ``display_name=""`` explicitly clears that label. ``execution_mode_default=""``
    clears the execution-mode default (new projects revert to their own default);
    ``code_intel_enabled_default`` accepts a bool/0/1 or the string ``""`` to clear.
    """
    settings_key = _ws_settings_key(tenant_id)
    # Ensure the row exists before updating individual fields.
    await db.execute(
        "INSERT INTO workspace_settings (id, tenant_id) VALUES (?, ?) "
        "ON CONFLICT(id) DO NOTHING",
        (settings_key, tenant_id),
    )
    updates: list[str] = []
    params: list[Any] = []
    if hitl_auto_answer_default is not None:
        updates.append("hitl_auto_answer_default = ?")
        params.append(1 if hitl_auto_answer_default else 0)
    if sprint_name_default is not None:
        updates.append("sprint_name_default = ?")
        params.append(sprint_name_default or None)
    if display_name is not None:
        updates.append("display_name = ?")
        params.append(display_name.strip() or None)
    if log_task_sprint_nudge_threshold is not None:
        updates.append("log_task_sprint_nudge_threshold = ?")
        params.append(max(0, int(log_task_sprint_nudge_threshold)))
    if handoff_template is not None:
        updates.append("handoff_template = ?")
        # Empty string clears the custom template (reverts to server default).
        params.append(handoff_template.strip() or None)
    if execution_mode_default is not None:
        updates.append("execution_mode_default = ?")
        # Empty string clears the default; otherwise normalize to a valid posture.
        _emode = (execution_mode_default or "").strip().lower()
        params.append(normalize_execution_mode(_emode) if _emode else None)
    if code_intel_enabled_default is not None:
        updates.append("code_intel_enabled_default = ?")
        # "" clears; any truthy/1 → 1, falsey/0 → 0.
        if isinstance(code_intel_enabled_default, str) and not code_intel_enabled_default.strip():
            params.append(None)
        else:
            params.append(1 if code_intel_enabled_default and code_intel_enabled_default not in ("0", "false", "False") else 0)
    if loop_enabled_default is not None:
        # 76cf8bda — /loop auto-continue default. Truthy/1 → 1, falsey/0 → 0.
        updates.append("loop_enabled_default = ?")
        params.append(1 if loop_enabled_default and loop_enabled_default not in ("0", "false", "False") else 0)
    if auto_refresh_enabled is not None:
        # bf51b12e — planner context-refresh toggle. Truthy/1 → 1, falsey/0 → 0.
        updates.append("auto_refresh_enabled = ?")
        params.append(1 if auto_refresh_enabled and auto_refresh_enabled not in ("0", "false", "False") else 0)
    if refresh_interval_turns is not None:
        # At least 1 turn between interval-based refreshes.
        updates.append("refresh_interval_turns = ?")
        params.append(max(1, int(refresh_interval_turns)))
    if refresh_triggers is not None:
        # A list ⇒ JSON-encode; "" clears (revert to default trigger set);
        # any other string is stored verbatim (already JSON).
        updates.append("refresh_triggers = ?")
        if isinstance(refresh_triggers, list):
            params.append(json.dumps(refresh_triggers))
        elif isinstance(refresh_triggers, str):
            params.append(refresh_triggers.strip() or None)
        else:
            params.append(None)
    if updates:
        from datetime import datetime, timezone
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        updates.append("updated_at = ?")
        params.append(now_ts)
        params.append(settings_key)
        await db.execute(
            f"UPDATE workspace_settings SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
    await db.commit()
    return await get_workspace_settings(db, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# registered_hostnames — token-based OAuth hooks (control-plane / auth DB)
# ---------------------------------------------------------------------------


async def register_hostname(
    db: aiosqlite.Connection, tenant_id: str, hostname: str
) -> str:
    """Register (or rotate) a machine for token-based hooks and return its
    registration_token. Re-registering the same (tenant, hostname) rotates the
    token so a lost machine can be re-authorized from the dashboard."""
    import secrets
    token = secrets.token_hex(16)
    async with db.execute(
        "SELECT id FROM registered_hostnames WHERE tenant_id = ? AND hostname = ?",
        (tenant_id, hostname),
    ) as cur:
        existing = await cur.fetchone()
    if existing is not None:
        await db.execute(
            "UPDATE registered_hostnames "
            "SET registration_token = ?, registered_at = datetime('now') "
            "WHERE tenant_id = ? AND hostname = ?",
            (token, tenant_id, hostname),
        )
    else:
        await db.execute(
            "INSERT INTO registered_hostnames "
            "(id, tenant_id, hostname, registration_token) VALUES (?, ?, ?, ?)",
            (_new_id(), tenant_id, hostname, token),
        )
    await db.commit()
    return token


async def resolve_hostname_registration(
    db: aiosqlite.Connection, hostname: str, registration_token: str
) -> str | None:
    """Return the tenant_id for a hostname+token match (and bump last_seen), or
    None when there is no match. Never raises — the hook path must fail open to
    an empty context, not a 401."""
    if not hostname or not registration_token:
        return None
    async with db.execute(
        "SELECT tenant_id FROM registered_hostnames "
        "WHERE hostname = ? AND registration_token = ?",
        (hostname, registration_token),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    tenant_id = row["tenant_id"] if isinstance(row, dict) else row[0]
    await db.execute(
        "UPDATE registered_hostnames SET last_seen = datetime('now') "
        "WHERE hostname = ? AND registration_token = ?",
        (hostname, registration_token),
    )
    await db.commit()
    return tenant_id


async def get_hostname_status(
    db: aiosqlite.Connection, tenant_id: str, hostname: str
) -> dict[str, Any]:
    """Return {registered, token} for a tenant's hostname (token echoed so the
    installer can finish writing the hook script after the browser connect)."""
    async with db.execute(
        "SELECT registration_token FROM registered_hostnames "
        "WHERE tenant_id = ? AND hostname = ?",
        (tenant_id, hostname),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return {"registered": False, "token": None}
    tok = row["registration_token"] if isinstance(row, dict) else row[0]
    return {"registered": True, "token": tok}


async def list_registered_hostnames(
    db: aiosqlite.Connection, tenant_id: str
) -> list[dict[str, Any]]:
    """List a tenant's registered machines (token omitted) newest first."""
    async with db.execute(
        "SELECT id, hostname, registered_at, last_seen FROM registered_hostnames "
        "WHERE tenant_id = ? ORDER BY registered_at DESC",
        (tenant_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def revoke_registered_hostname(
    db: aiosqlite.Connection, tenant_id: str, machine_id: str
) -> bool:
    """Delete one of the tenant's registrations by id. Returns True if removed.
    Tenant-scoped so a tenant can never revoke another tenant's machine."""
    async with db.execute(
        "DELETE FROM registered_hostnames WHERE id = ? AND tenant_id = ?",
        (machine_id, tenant_id),
    ) as cur:
        rc = cur.rowcount or 0
    await db.commit()
    return rc > 0


# ---------------------------------------------------------------------------
# 10e6b265 — session queue (projects.queued_session)
# ---------------------------------------------------------------------------


async def set_queued_session(
    db: aiosqlite.Connection, project_id: str, goal: str | None
) -> None:
    """Queue the next /goal string to run after this session. Empty/None clears."""
    await db.execute(
        "UPDATE projects SET queued_session = ? WHERE id = ?",
        ((goal or None), project_id),
    )
    await db.commit()


async def get_queued_session(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the queued next-session goal, or None when nothing is queued."""
    async with db.execute(
        "SELECT queued_session FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    val = row["queued_session"] if isinstance(row, dict) else row[0]
    return val or None


async def pop_queued_session(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the queued goal and clear it (read-once) so a handoff surfaces it
    exactly once."""
    goal = await get_queued_session(db, project_id)
    if goal:
        await db.execute(
            "UPDATE projects SET queued_session = NULL WHERE id = ?", (project_id,)
        )
        await db.commit()
    return goal


# ---------------------------------------------------------------------------
# 5efe254b — trusted handoff channel (projects.pending_goal)
# ---------------------------------------------------------------------------


async def set_pending_goal(
    db: aiosqlite.Connection, project_id: str, goal: str | None
) -> None:
    """Persist the handoff /goal so the next start_session can surface it through
    a trusted MCP tool result (keyed on project_id) instead of a copy-pasted,
    spoofable chat string. Empty/None clears it. Read-once via pop_pending_goal."""
    await db.execute(
        "UPDATE projects SET pending_goal = ? WHERE id = ?",
        ((goal or None), project_id),
    )
    await db.commit()


async def get_pending_goal(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the stored handoff /goal, or None when nothing is pending."""
    async with db.execute(
        "SELECT pending_goal FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    val = row["pending_goal"] if isinstance(row, dict) else row[0]
    return val or None


async def pop_pending_goal(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the pending /goal and clear it (read-once) so start_session
    surfaces it exactly once and a stale goal never resurfaces in a later
    session."""
    goal = await get_pending_goal(db, project_id)
    if goal:
        await db.execute(
            "UPDATE projects SET pending_goal = NULL WHERE id = ?", (project_id,)
        )
        await db.commit()
    return goal


# ---------------------------------------------------------------------------
# v3.1 — per-session checkpoint snapshots (sessions.checkpoint_data)
# ---------------------------------------------------------------------------


async def set_session_checkpoint(
    db: aiosqlite.Connection, session_id: str, data: dict[str, Any]
) -> None:
    """Store a checkpoint snapshot on the session row as JSON text.
    Replaces the legacy checkpoint:* project_notes hack."""
    await db.execute(
        "UPDATE sessions SET checkpoint_data = ? WHERE id = ?",
        (json.dumps(data), session_id),
    )
    await db.commit()


async def get_session_checkpoint(
    db: aiosqlite.Connection, session_id: str
) -> dict[str, Any] | None:
    """Return the latest checkpoint snapshot for a session, or None."""
    async with db.execute(
        "SELECT checkpoint_data FROM sessions WHERE id = ?", (session_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    raw = row["checkpoint_data"]
    if not raw:
        return None
    if isinstance(raw, dict):  # Postgres may return parsed JSON
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# v0.9 — Magic-link email auth tokens
# ---------------------------------------------------------------------------


async def get_active_magic_token(
    db: aiosqlite.Connection, email: str
) -> dict[str, Any] | None:
    """Return the most recent unused, unexpired magic token for an email
    (or None). Used by the rate-limit gate to avoid sending duplicates
    when the user clicks the resend button while a valid link is in
    their inbox."""
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with db.execute(
        "SELECT * FROM magic_link_tokens WHERE email = ? "
        "AND used_at IS NULL AND expires_at > ? "
        "ORDER BY created_at DESC LIMIT 1",
        (email.lower(), now_iso),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def store_magic_token(
    db: aiosqlite.Connection,
    email: str,
    token_hash: str,
    expires_at: str,
) -> dict[str, Any]:
    """Persist a single-use magic-link token. ``token_hash`` is the
    sha256 of the raw token; the raw token is shown to the user (in
    the email) and never stored."""
    tid = _new_id()
    await db.execute(
        "INSERT INTO magic_link_tokens (id, email, token_hash, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (tid, email.lower(), token_hash, expires_at),
    )
    await db.commit()
    return {"id": tid, "email": email.lower(), "expires_at": expires_at}


async def record_signup_attempt(
    db: aiosqlite.Connection, ip_hash: str, email_hash: str
) -> None:
    """925909aa — log one magic-link signup attempt keyed by salted IP/email
    hashes, for persistent per-IP rate limiting (survives restarts)."""
    await db.execute(
        "INSERT INTO signup_attempts (id, ip_hash, email_hash) VALUES (?, ?, ?)",
        (_new_id(), ip_hash, email_hash),
    )
    await db.commit()


async def count_recent_signup_attempts(
    db: aiosqlite.Connection, ip_hash: str, since_iso: str
) -> int:
    """Count signup attempts from ``ip_hash`` at or after ``since_iso``
    (``YYYY-MM-DD HH:MM:SS`` UTC)."""
    async with db.execute(
        "SELECT COUNT(*) FROM signup_attempts WHERE ip_hash = ? AND created_at >= ?",
        (ip_hash, since_iso),
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def consume_magic_token(
    db: aiosqlite.Connection, token_hash: str
) -> dict[str, Any] | None:
    """Validate-and-mark-used. Returns the row when fresh; None if the
    token doesn't exist, was already used, or has expired. Atomic so two
    concurrent verify clicks can't both succeed."""
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with db.execute(
        "SELECT * FROM magic_link_tokens WHERE token_hash = ?",
        (token_hash,),
    ) as cur:
        row = await cur.fetchone()
    row_dict = _row_to_dict(row)
    if row_dict is None:
        return None
    if row_dict.get("used_at"):
        return None
    if (row_dict.get("expires_at") or "") < now_iso:
        return None
    await db.execute(
        "UPDATE magic_link_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL",
        (now_iso, row_dict["id"]),
    )
    await db.commit()
    return row_dict


# ---------------------------------------------------------------------------
# Workspace members — team invite flow
# ---------------------------------------------------------------------------


async def create_workspace_invite(
    db: aiosqlite.Connection,
    tenant_id: str,
    email: str,
    role: str,
    token_hash: str,
    *,
    github_access: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Insert a pending workspace member row (joined_at=NULL).

    G5.20 — ``github_access`` caps repo-touching MCP tools for this invitee.
    Defaults from role when omitted (viewer→none, member→read, admin/owner→write).

    d116642e — ``project_id`` scopes the invite to a single project. When
    ``None`` (default) the member is workspace-wide and sees every project
    (current behavior). When set, the member is project-scoped: listing-only
    scoping applies (they see only that project in listings). Airtight
    per-request access enforcement is deferred pending the product decision
    (pin b11c7cf6).
    """
    from .. import roles as _roles  # noqa: PLC0415
    mid = _new_id()
    if github_access is None:
        github_access = _roles.default_github_access_for_role(role)
    await db.execute(
        "INSERT INTO workspace_members "
        "(id, tenant_id, email, role, github_access, token_hash, project_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (mid, tenant_id, email, role, github_access, token_hash, project_id),
    )
    await db.commit()
    async with db.execute("SELECT * FROM workspace_members WHERE id = ?", (mid,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_workspace_invite_by_token_hash(
    db: aiosqlite.Connection,
    token_hash: str,
) -> dict[str, Any] | None:
    """Return a pending invite by token hash, or None if not found / already accepted."""
    async with db.execute(
        "SELECT * FROM workspace_members WHERE token_hash = ? AND joined_at IS NULL",
        (token_hash,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def accept_workspace_invite(
    db: aiosqlite.Connection,
    member_id: str,
) -> dict[str, Any] | None:
    """Mark an invite as accepted (set joined_at, clear token_hash)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE workspace_members SET joined_at = ?, token_hash = NULL WHERE id = ?",
        (now, member_id),
    )
    await db.commit()
    async with db.execute("SELECT * FROM workspace_members WHERE id = ?", (member_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_pending_invites_for_email(
    db: aiosqlite.Connection,
    email: str,
) -> list[dict[str, Any]]:
    """Return all pending workspace_members rows for email (joined_at IS NULL).

    Used at OAuth login to auto-accept invites sent before the user had an
    account (fbbe99af fallback).
    """
    async with db.execute(
        "SELECT * FROM workspace_members WHERE LOWER(email) = LOWER(?) AND joined_at IS NULL",
        (email,),
    ) as cur:
        rows = await cur.fetchall()
    return [r for r in (_row_to_dict(row) for row in rows) if r]


async def resolve_member_role(
    db: aiosqlite.Connection,
    tenant_id: str,
    email: str,
) -> tuple[str, str] | None:
    """G5.19 / G5.20 — return ``(role, github_access)`` for the given user
    in the given tenant's workspace.

    Order:
     1. If ``email`` matches the tenant row's own email → ('owner','write').
     2. Else if an accepted workspace_members row exists for this
        (tenant_id, email) → use the row's role + github_access.
     3. Else → None (caller should treat as 403 / not a member).
    """
    e = (email or "").strip().lower()
    if not e:
        return None
    async with db.execute(
        "SELECT email FROM tenants WHERE id = ?", (tenant_id,),
    ) as cur:
        tenant_row = await cur.fetchone()
    if tenant_row and (str(tenant_row["email"]).lower() == e):
        return ("owner", "write")
    async with db.execute(
        "SELECT role, github_access FROM workspace_members "
        "WHERE tenant_id = ? AND LOWER(email) = ? AND joined_at IS NOT NULL "
        "ORDER BY joined_at DESC LIMIT 1",
        (tenant_id, e),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return (
        (row["role"] or "member"),
        (row["github_access"] or "read"),
    )


async def workspace_member_accepted_for_email(
    db: aiosqlite.Connection,
    email: str,
) -> dict[str, Any] | None:
    """G5.22 — return an accepted workspace membership for ``email`` (joined,
    not pending), or None. Used by the OAuth callback to skip auto-Neon
    provisioning for invitees who already belong to someone else's workspace."""
    e = (email or "").strip().lower()
    if not e:
        return None
    async with db.execute(
        "SELECT * FROM workspace_members "
        "WHERE LOWER(email) = ? AND joined_at IS NOT NULL "
        "ORDER BY joined_at DESC LIMIT 1",
        (e,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_workspace_member_by_id(
    db: aiosqlite.Connection,
    member_id: str,
    tenant_id: str,
) -> dict[str, Any] | None:
    """Return a single workspace_members row scoped to tenant_id."""
    async with db.execute(
        "SELECT * FROM workspace_members WHERE id = ? AND tenant_id = ?",
        (member_id, tenant_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def refresh_workspace_invite_token(
    db: aiosqlite.Connection,
    member_id: str,
    tenant_id: str,
    token_hash: str,
) -> dict[str, Any] | None:
    """Replace the invite token for a pending member row."""
    await db.execute(
        "UPDATE workspace_members SET token_hash = ? WHERE id = ? AND tenant_id = ? AND joined_at IS NULL",
        (token_hash, member_id, tenant_id),
    )
    await db.commit()
    async with db.execute("SELECT * FROM workspace_members WHERE id = ?", (member_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def list_workspace_members(
    db: aiosqlite.Connection,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """Return all workspace members (pending and accepted) for a tenant."""
    async with db.execute(
        "SELECT * FROM workspace_members WHERE tenant_id = ? ORDER BY invited_at ASC",
        (tenant_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]


async def count_workspace_members(
    db: aiosqlite.Connection,
    tenant_id: str,
) -> int:
    """Return the total member count (pending + accepted) for a tenant."""
    async with db.execute(
        "SELECT COUNT(*) FROM workspace_members WHERE tenant_id = ?",
        (tenant_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return 0
    vals = list(row.values()) if hasattr(row, "values") else list(row)
    return int(vals[0]) if vals else 0


async def delete_workspace_member(
    db: aiosqlite.Connection,
    member_id: str,
    tenant_id: str,
) -> bool:
    """Remove a workspace member row. Returns True (no-op on missing row)."""
    await db.execute(
        "DELETE FROM workspace_members WHERE id = ? AND tenant_id = ?",
        (member_id, tenant_id),
    )
    await db.commit()
    return True


async def update_workspace_member(
    db: aiosqlite.Connection,
    member_id: str,
    tenant_id: str,
    *,
    role: str | None = None,
    github_access: str | None = None,
) -> dict[str, Any] | None:
    """v2.8 — update a member's role and/or github_access (admin-only edit).

    Only the fields passed (non-None) are changed. Scoped by tenant_id so a
    caller can never touch another workspace's rows. Returns the updated row,
    or None when no member matched.
    """
    updates: list[str] = []
    params: list[Any] = []
    if role is not None:
        updates.append("role = ?")
        params.append(role)
    if github_access is not None:
        updates.append("github_access = ?")
        params.append(github_access)
    if updates:
        params.extend([member_id, tenant_id])
        await db.execute(
            f"UPDATE workspace_members SET {', '.join(updates)} "
            "WHERE id = ? AND tenant_id = ?",
            tuple(params),
        )
        await db.commit()
    async with db.execute(
        "SELECT * FROM workspace_members WHERE id = ? AND tenant_id = ?",
        (member_id, tenant_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_workspaces_for_email(
    db: aiosqlite.Connection,
    email: str,
) -> list[dict[str, Any]]:
    """Return workspaces the email has been invited to (accepted rows only).

    Each row: {tenant_id, owner_email, role, github_access, project_id}.
    ``project_id`` (d116642e) is NULL for workspace-wide members and set for
    project-scoped members. Used to populate the workspace-switcher dropdown.
    """
    e = (email or "").strip().lower()
    if not e:
        return []
    async with db.execute(
        "SELECT wm.tenant_id, wm.role, wm.github_access, wm.project_id, "
        "t.email AS owner_email "
        "FROM workspace_members wm "
        "JOIN tenants t ON t.id = wm.tenant_id "
        "WHERE LOWER(wm.email) = ? AND wm.joined_at IS NOT NULL "
        "ORDER BY wm.invited_at ASC",
        (e,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]


async def get_scoped_project_ids_for_member(
    db: aiosqlite.Connection,
    tenant_id: str,
    email: str,
) -> list[str] | None:
    """d116642e — listing-only project scoping for a workspace member.

    Returns the set of project_ids an accepted member is scoped to within the
    given tenant's workspace, or ``None`` when the member is NOT project-scoped
    (i.e. has any workspace-wide row, or is not a member at all) — meaning they
    see every project.

    Semantics:
      - returns ``None``  → no scoping; caller lists all projects (default).
      - returns ``[...]`` → list only these project_ids in UI listings.

    NOTE: this is listing-only scoping. It does NOT block direct-by-ID access to
    other projects in the same workspace — airtight per-request enforcement is
    deferred pending the open product decision (pin b11c7cf6). Writes remain
    gated by role enforcement (393eed0a).
    """
    e = (email or "").strip().lower()
    if not e:
        return None
    async with db.execute(
        "SELECT project_id FROM workspace_members "
        "WHERE tenant_id = ? AND LOWER(email) = ? AND joined_at IS NOT NULL",
        (tenant_id, e),
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return None
    scoped: list[str] = []
    for r in rows:
        pid = r["project_id"] if isinstance(r, dict) else r[0]
        if pid is None:
            # Any workspace-wide membership row wins → no scoping.
            return None
        scoped.append(pid)
    return scoped


# ---------------------------------------------------------------------------
# Dunning helpers
# ---------------------------------------------------------------------------

async def get_tenant_by_stripe_customer(
    db: aiosqlite.Connection,
    stripe_customer_id: str,
) -> dict[str, Any] | None:
    """Return tenant by stripe_customer_id, or None."""
    async with db.execute(
        "SELECT * FROM tenants WHERE stripe_customer_id = ?", (stripe_customer_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def list_tenants_with_neon(
    db: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    """Return all tenants that have a provisioned Neon database."""
    async with db.execute(
        "SELECT * FROM tenants WHERE neon_project_id IS NOT NULL"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]


async def get_tenants_with_payment_failures(
    db: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    """Return all tenants currently in dunning (payment_failed_at IS NOT NULL)."""
    async with db.execute(
        "SELECT * FROM tenants WHERE payment_failed_at IS NOT NULL"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]


# ---------------------------------------------------------------------------
# GDPR / account management
# ---------------------------------------------------------------------------

async def export_tenant_data(
    db: aiosqlite.Connection,
    tenant_id: str,
    project_db: aiosqlite.Connection | None = None,
) -> dict[str, Any]:
    """Collect all data belonging to a tenant as a plain dict for GDPR export.

    Account-level rows (tenant, api_tokens, workspace_members) live in the auth
    DB (``db``). In hosted mode the tenant's *project* data lives in a separate
    per-tenant Neon DB — pass it as ``project_db`` so the export actually
    contains the projects. When omitted (self-hosted, single DB) the project
    data is read from ``db`` like before.
    """
    from datetime import datetime, timezone

    pdb = project_db if project_db is not None else db

    async with db.execute(
        "SELECT id, email, google_sub, microsoft_sub, plan, created_at FROM tenants WHERE id = ?",
        (tenant_id,),
    ) as cur:
        tenant_row = await cur.fetchone()
    tenant_data = _row_to_dict(tenant_row) if tenant_row else {}

    async with db.execute(
        "SELECT id, label, created_at FROM api_tokens WHERE tenant_id = ?",
        (tenant_id,),
    ) as cur:
        tokens = [_row_to_dict(r) for r in await cur.fetchall()]

    async with db.execute(
        "SELECT id, email, role, github_access, invited_at, joined_at FROM workspace_members WHERE tenant_id = ?",
        (tenant_id,),
    ) as cur:
        members = [_row_to_dict(r) for r in await cur.fetchall()]

    async with pdb.execute(
        "SELECT id, name, creator_human_id, decisions, created_at FROM projects ORDER BY created_at DESC",
    ) as cur:
        project_rows = await cur.fetchall()

    projects = []
    for pr in project_rows:
        p = _row_to_dict(pr)
        pid = p["id"]

        async with pdb.execute(
            "SELECT content, goal_north_star, goal_sprint, version, updated_at FROM goal_states WHERE project_id = ?",
            (pid,),
        ) as cur:
            goals = [_row_to_dict(r) for r in await cur.fetchall()]

        async with pdb.execute(
            "SELECT id, name, human_id, status, session_summary, created_at FROM sessions WHERE project_id = ? ORDER BY created_at DESC LIMIT 500",
            (pid,),
        ) as cur:
            sessions = [_row_to_dict(r) for r in await cur.fetchall()]

        async with pdb.execute(
            "SELECT id, session_id, description, status, created_at FROM task_log WHERE project_id = ? ORDER BY created_at DESC LIMIT 2000",
            (pid,),
        ) as cur:
            tasks = [_row_to_dict(r) for r in await cur.fetchall()]

        async with pdb.execute(
            "SELECT id, version, title, status, item_group, added_at FROM sprint_items WHERE project_id = ?",
            (pid,),
        ) as cur:
            sprint = [_row_to_dict(r) for r in await cur.fetchall()]

        try:
            async with pdb.execute(
                "SELECT id, title, body, tags, created_at FROM project_notes WHERE project_id = ?",
                (pid,),
            ) as cur:
                notes = [_row_to_dict(r) for r in await cur.fetchall()]
        except Exception:
            notes = []

        try:
            async with pdb.execute(
                "SELECT id, question, urgency, status, answer, created_at FROM hitl_requests WHERE project_id = ? ORDER BY created_at DESC LIMIT 500",
                (pid,),
            ) as cur:
                hitl = [_row_to_dict(r) for r in await cur.fetchall()]
        except Exception:
            hitl = []

        p["goal_states"] = goals
        p["sessions"] = sessions
        p["tasks"] = tasks
        p["sprint_items"] = sprint
        p["notes"] = notes
        p["hitl_requests"] = hitl
        projects.append(p)

    # Workspace-global notes/decisions live in the project DB too.
    try:
        ws_notes = await get_workspace_notes(pdb)
    except Exception:
        ws_notes = []
    try:
        ws_decisions = await get_workspace_decisions(pdb)
    except Exception:
        ws_decisions = []

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "tenant": tenant_data,
        "api_tokens": tokens,
        "workspace_members": members,
        "workspace_notes": ws_notes,
        "workspace_decisions": ws_decisions,
        "projects": projects,
    }


async def delete_tenant_records(
    db: aiosqlite.Connection,
    tenant_id: str,
) -> None:
    """Remove all records for a tenant from the main DB. Irreversible."""
    for stmt, params in [
        ("DELETE FROM user_sessions WHERE tenant_id = ?", (tenant_id,)),
        ("DELETE FROM api_tokens WHERE tenant_id = ?", (tenant_id,)),
        ("DELETE FROM workspace_members WHERE tenant_id = ?", (tenant_id,)),
        ("DELETE FROM tenants WHERE id = ?", (tenant_id,)),
    ]:
        await db.execute(stmt, params)
    await db.commit()


# ---------------------------------------------------------------------------
# v2.6 — Session-scoped ephemeral notes (sprint scratch pad)
# ---------------------------------------------------------------------------


async def add_session_note(
    db: aiosqlite.Connection,
    session_id: str,
    title: str,
    body: str,
    note_kind: str | None = None,
) -> dict[str, Any]:
    """Add an ephemeral note scoped to a session. Auto-deleted on session close.

    0d7de2a2 — ``note_kind`` classifies the note. None/'note' is a normal
    sprint note. 'thinking' marks a HOOKS_DEBUG_STATE scratchpad note
    auto-persisted by Claude's client-side thinking_sync hook; the dashboard
    renders these with a distinct icon and they round-trip into the next
    session brief. Any other value is normalized to None (a normal note).
    """
    if note_kind not in (None, "note", "thinking"):
        note_kind = None
    if note_kind == "note":
        note_kind = None
    nid = _new_id()
    await db.execute(
        "INSERT INTO session_notes (id, session_id, title, body, note_kind) "
        "VALUES (?, ?, ?, ?, ?)",
        (nid, session_id, title, body, note_kind),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM session_notes WHERE id = ?", (nid,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


async def get_session_notes(
    db: aiosqlite.Connection,
    session_id: str,
    note_kind: str | None = None,
) -> list[dict[str, Any]]:
    """Return all notes for a session, newest first.

    0d7de2a2 — pass ``note_kind='thinking'`` to return only thinking_sync
    scratchpad notes, or ``'note'`` for only normal notes. Omit to return all.
    """
    if note_kind == "thinking":
        sql = (
            "SELECT * FROM session_notes WHERE session_id = ? "
            "AND note_kind = 'thinking' ORDER BY created_at DESC"
        )
        params: tuple = (session_id,)
    elif note_kind == "note":
        sql = (
            "SELECT * FROM session_notes WHERE session_id = ? "
            "AND (note_kind IS NULL OR note_kind = 'note') ORDER BY created_at DESC"
        )
        params = (session_id,)
    else:
        sql = (
            "SELECT * FROM session_notes WHERE session_id = ? ORDER BY created_at DESC"
        )
        params = (session_id,)
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def delete_session_notes(
    db: aiosqlite.Connection,
    session_id: str,
) -> int:
    """Delete all notes for a session. Called on session close."""
    async with db.execute(
        "DELETE FROM session_notes WHERE session_id = ?", (session_id,)
    ) as cur:
        count = cur.rowcount if cur.rowcount is not None else 0
    await db.commit()
    return count


async def add_feedback(
    db: aiosqlite.Connection,
    tenant_id: str,
    type: str,
    message: str,
    email: str = None,
) -> str:
    """Save user feedback to the feedback table."""
    feedback_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO feedback (id, tenant_id, type, message, email) VALUES (?, ?, ?, ?, ?)",
        (feedback_id, tenant_id, type, message, email),
    )
    await db.commit()
    return feedback_id


async def list_changelog_entries(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return all changelog entries ordered newest first."""
    async with db.execute(
        "SELECT id, version, title, body, published_at, created_at "
        "FROM changelog_entries ORDER BY published_at DESC"
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) if isinstance(r, dict) else {
        "id": r[0], "version": r[1], "title": r[2], "body": r[3],
        "published_at": r[4], "created_at": r[5],
    } for r in rows]


async def create_changelog_entry(
    db: aiosqlite.Connection,
    title: str,
    body: str,
    version: str | None = None,
    published_at: str | None = None,
) -> dict[str, Any]:
    """Create a new changelog entry. Returns the created row."""
    import datetime
    entry_id = str(uuid.uuid4())
    ts = published_at or datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO changelog_entries (id, version, title, body, published_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (entry_id, version, title, body, ts),
    )
    await db.commit()
    return {"id": entry_id, "version": version, "title": title, "body": body,
            "published_at": ts, "created_at": ts}


async def update_changelog_entry(
    db: aiosqlite.Connection,
    entry_id: str,
    title: str | None = None,
    body: str | None = None,
    version: str | None = None,
    published_at: str | None = None,
) -> dict[str, Any] | None:
    """Patch a changelog entry. Returns the updated row or None if not found."""
    async with db.execute(
        "SELECT id, version, title, body, published_at, created_at "
        "FROM changelog_entries WHERE id = ?",
        (entry_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    r = dict(row) if isinstance(row, dict) else {
        "id": row[0], "version": row[1], "title": row[2], "body": row[3],
        "published_at": row[4], "created_at": row[5],
    }
    new_title = title if title is not None else r["title"]
    new_body = body if body is not None else r["body"]
    new_version = version if version is not None else r["version"]
    new_published_at = published_at if published_at is not None else r["published_at"]
    await db.execute(
        "UPDATE changelog_entries SET title=?, body=?, version=?, published_at=? WHERE id=?",
        (new_title, new_body, new_version, new_published_at, entry_id),
    )
    await db.commit()
    return {**r, "title": new_title, "body": new_body, "version": new_version,
            "published_at": new_published_at}


async def delete_changelog_entry(db: aiosqlite.Connection, entry_id: str) -> bool:
    """Delete a changelog entry. Returns True if it existed."""
    async with db.execute(
        "SELECT 1 FROM changelog_entries WHERE id = ?", (entry_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return False
    await db.execute("DELETE FROM changelog_entries WHERE id = ?", (entry_id,))
    await db.commit()
    return True


# ---------------------------------------------------------------------------
# f773a99a — Graph diff: per-session code-graph metric snapshots
# ---------------------------------------------------------------------------


async def snapshot_graph_metrics(
    db: aiosqlite.Connection,
    session_id: str,
    project_id: str,
) -> dict[str, Any]:
    """Compute proxy graph metrics for a session and persist them.

    Proxy metrics (no live code-graph traversal needed):
    - node_count: distinct file_paths in file_symbol_claims for this project.
    - edge_count: distinct (session_id, file_path) pairs — sessions touching files.
    - hotspot_count: files with 3+ distinct sessions in file_symbol_claims.
    - file_churn: distinct files touched in the last 7 days.

    Idempotent: inserts a new snapshot row each time so callers can compare
    across checkpoints. Returns the snapshot dict.
    """
    import json as _json

    # node_count: distinct files claimed in this project across all sessions
    async with db.execute(
        "SELECT COUNT(DISTINCT fsc.file_path) AS cnt "
        "FROM file_symbol_claims fsc "
        "JOIN sessions s ON s.id = fsc.session_id "
        "WHERE s.project_id = ?",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    node_count = (_row_to_dict(row) or {}).get("cnt") or 0

    # edge_count: distinct (session, file) pairs for this project
    async with db.execute(
        "SELECT COUNT(*) AS cnt "
        "FROM ("
        "    SELECT DISTINCT fsc.session_id, fsc.file_path "
        "    FROM file_symbol_claims fsc "
        "    JOIN sessions s ON s.id = fsc.session_id "
        "    WHERE s.project_id = ?"
        ")",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    edge_count = (_row_to_dict(row) or {}).get("cnt") or 0

    # hotspot_count: files with 3+ distinct sessions
    async with db.execute(
        "SELECT COUNT(*) AS cnt FROM ("
        "    SELECT fsc.file_path "
        "    FROM file_symbol_claims fsc "
        "    JOIN sessions s ON s.id = fsc.session_id "
        "    WHERE s.project_id = ? "
        "    GROUP BY fsc.file_path "
        "    HAVING COUNT(DISTINCT fsc.session_id) >= 3"
        ")",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    hotspot_count = (_row_to_dict(row) or {}).get("cnt") or 0

    # file_churn: distinct files touched in last 7 days
    async with db.execute(
        "SELECT COUNT(DISTINCT fsc.file_path) AS cnt "
        "FROM file_symbol_claims fsc "
        "JOIN sessions s ON s.id = fsc.session_id "
        "WHERE s.project_id = ? "
        "AND fsc.claimed_at > datetime('now', '-7 days')",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    file_churn = (_row_to_dict(row) or {}).get("cnt") or 0

    metrics = {
        "node_count": node_count,
        "edge_count": edge_count,
        "hotspot_count": hotspot_count,
        "file_churn": file_churn,
    }
    snap_id = _new_id()
    await db.execute(
        "INSERT INTO session_graph_snapshots "
        "(id, session_id, project_id, node_count, edge_count, hotspot_count, file_churn, metrics_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (snap_id, session_id, project_id, node_count, edge_count, hotspot_count, file_churn, _json.dumps(metrics)),
    )
    await db.commit()
    return {"snapshot_id": snap_id, "session_id": session_id, "project_id": project_id, **metrics}


async def get_graph_diff(
    db: aiosqlite.Connection,
    session_a_id: str,
    session_b_id: str,
) -> dict[str, Any]:
    """Compare the latest graph snapshots of two sessions.

    f773a99a — returns the delta between session_a and session_b's most recent
    ``session_graph_snapshots`` rows: nodes_added, nodes_removed, new_hotspots,
    file_churn_delta. Positive values mean session_b has more; negative means
    session_a has more. Returns an error dict when either session has no snapshot.
    """
    async def _latest_snap(sid: str) -> dict[str, Any] | None:
        async with db.execute(
            "SELECT * FROM session_graph_snapshots WHERE session_id = ? "
            "ORDER BY snapshot_at DESC LIMIT 1",
            (sid,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_dict(row)

    snap_a = await _latest_snap(session_a_id)
    snap_b = await _latest_snap(session_b_id)

    if snap_a is None:
        return {"error": f"No graph snapshot found for session {session_a_id[:8]}"}
    if snap_b is None:
        return {"error": f"No graph snapshot found for session {session_b_id[:8]}"}

    node_a = snap_a.get("node_count") or 0
    node_b = snap_b.get("node_count") or 0
    hotspot_a = snap_a.get("hotspot_count") or 0
    hotspot_b = snap_b.get("hotspot_count") or 0
    churn_a = snap_a.get("file_churn") or 0
    churn_b = snap_b.get("file_churn") or 0
    edge_a = snap_a.get("edge_count") or 0
    edge_b = snap_b.get("edge_count") or 0

    return {
        "session_a": session_a_id,
        "session_b": session_b_id,
        "snapshot_a_at": snap_a.get("snapshot_at"),
        "snapshot_b_at": snap_b.get("snapshot_at"),
        "nodes_added": node_b - node_a,
        "nodes_removed": max(0, node_a - node_b),
        "new_hotspots": hotspot_b - hotspot_a,
        "file_churn_delta": churn_b - churn_a,
        "edge_delta": edge_b - edge_a,
        "summary": (
            f"session_b has {node_b - node_a:+d} nodes, "
            f"{hotspot_b - hotspot_a:+d} hotspots, "
            f"{churn_b - churn_a:+d} file churn vs session_a"
        ),
    }


# ---------------------------------------------------------------------------
# A2A protocol — agent_tasks table (99e71b9e)
# ---------------------------------------------------------------------------

async def create_agent_task(
    db: aiosqlite.Connection,
    agent_id: str,
    input_data: dict[str, Any],
    session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert a new agent task with status 'submitted'. Returns the task row."""
    task_id = _new_id()
    input_json = json.dumps(input_data)
    meta_json = json.dumps(metadata) if metadata else None
    await db.execute(
        "INSERT INTO agent_tasks (id, agent_id, session_id, input, metadata) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, agent_id, session_id, input_json, meta_json),
    )
    await db.commit()
    return await _get_agent_task(db, task_id)  # type: ignore[return-value]


async def _get_agent_task(
    db: aiosqlite.Connection, task_id: str
) -> dict[str, Any] | None:
    """Fetch one agent task by id."""
    async with db.execute(
        "SELECT * FROM agent_tasks WHERE id = ?", (task_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    task = _row_to_dict(row)
    # Deserialize JSON blobs
    if task.get("input"):
        try:
            task["input"] = json.loads(task["input"])
        except Exception:
            pass
    if task.get("output"):
        try:
            task["output"] = json.loads(task["output"])
        except Exception:
            pass
    if task.get("metadata"):
        try:
            task["metadata"] = json.loads(task["metadata"])
        except Exception:
            pass
    return task


async def get_agent_task(
    db: aiosqlite.Connection, agent_id: str, task_id: str
) -> dict[str, Any] | None:
    """Fetch an agent task, scoped to agent_id for security."""
    async with db.execute(
        "SELECT * FROM agent_tasks WHERE id = ? AND agent_id = ?",
        (task_id, agent_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    task = _row_to_dict(row)
    for field in ("input", "output", "metadata"):
        if task.get(field):
            try:
                task[field] = json.loads(task[field])
            except Exception:
                pass
    return task


async def update_agent_task_status(
    db: aiosqlite.Connection,
    task_id: str,
    status: str,
    output: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Update the status (and optionally output) of an agent task."""
    output_json = json.dumps(output) if output is not None else None
    if output_json is not None:
        await db.execute(
            "UPDATE agent_tasks SET status=?, output=?, updated_at=datetime('now') WHERE id=?",
            (status, output_json, task_id),
        )
    else:
        await db.execute(
            "UPDATE agent_tasks SET status=?, updated_at=datetime('now') WHERE id=?",
            (status, task_id),
        )
    await db.commit()
    return await _get_agent_task(db, task_id)


# ---------------------------------------------------------------------------
# Handoff history (8819d6b1) — each generated handoff is its own row in the
# handoffs table, so the dashboard/planner can list history, diff between
# sessions, and detect "a new handoff arrived since you last checked"
# (ab514e43). record_handoff is called from handoff.generate_handoff.
# ---------------------------------------------------------------------------
async def record_handoff(
    db: aiosqlite.Connection,
    project_id: str,
    mode: str,
    body: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Insert a handoff history row and return it.

    ``mode`` is the rendered handoff mode (full/delta/…). ``session_id`` links
    the handoff to the session that produced it (NULL for project-level renders).
    """
    hid = _new_id()
    await db.execute(
        "INSERT INTO handoffs (id, project_id, session_id, mode, body) "
        "VALUES (?, ?, ?, ?, ?)",
        (hid, project_id, session_id, mode, body),
    )
    await db.commit()
    return (await get_handoff(db, hid)) or {"id": hid}


async def get_handoff(
    db: aiosqlite.Connection, handoff_id: str
) -> dict[str, Any] | None:
    """Fetch a single handoff row by id."""
    async with db.execute(
        "SELECT * FROM handoffs WHERE id = ?", (handoff_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_handoffs(
    db: aiosqlite.Connection,
    project_id: str,
    limit: int = 20,
    *,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return handoff history for a project, newest first (body included).

    Pass ``session_id`` to scope to one session's handoffs. ``limit`` is clamped
    to 1..200.
    """
    limit = max(1, min(int(limit or 20), 200))
    if session_id:
        sql = (
            "SELECT * FROM handoffs WHERE project_id = ? AND session_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?"
        )
        params: tuple[Any, ...] = (project_id, session_id, limit)
    else:
        sql = (
            "SELECT * FROM handoffs WHERE project_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?"
        )
        params = (project_id, limit)
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [d for d in (_row_to_dict(r) for r in rows) if d is not None]


async def get_latest_handoff(
    db: aiosqlite.Connection, project_id: str
) -> dict[str, Any] | None:
    """Return the most recent handoff row for a project, or None."""
    rows = await get_handoffs(db, project_id, limit=1)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# validate_assumption (8ec5493b) — one-call assumption validation: stamp the
# decision's assumption_status, save a code-anchored finding note, and fire a
# blocking HITL when the assumption is invalidated. Builds on the assumption
# fields added in 2b39549d.
# ---------------------------------------------------------------------------
async def validate_assumption(
    db: aiosqlite.Connection,
    decision_id: str,
    finding: str,
    confirmed: bool,
    *,
    file_path: str | None = None,
    symbol: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Confirm or invalidate the assumption a pinned decision rests on.

    - Stamps ``assumption_status`` = confirmed | invalidated on the decision.
    - Saves a code-anchored ``kind='insight'`` note carrying ``finding`` (tagged
      ``assumption`` + the status; high priority when invalidated).
    - On invalidation, fires a **blocking** HITL so work depending on the
      decision pauses for human judgment.

    Returns ``{decision, assumption_status, note, hitl}`` (``hitl`` is None when
    confirmed). Raises ValueError if the decision does not exist.
    """
    decision = await get_pinned_decision(db, decision_id)
    if decision is None:
        raise ValueError("decision not found")
    status = "confirmed" if confirmed else "invalidated"
    project_id = decision["project_id"]
    title = (decision.get("title") or "decision").strip()
    updated = await update_pinned_decision(
        db, decision_id, assumption_status=status
    )
    note = await add_project_note(
        db, project_id,
        f"Assumption {status}: {title}"[:500],
        finding or "(no finding text)",
        f"assumption,{status}",
        kind="insight",
        priority="high" if not confirmed else "normal",
        file_path=file_path, symbol=symbol,
    )
    hitl: dict[str, Any] | None = None
    if not confirmed:
        # hitl_requests.session_id is a FK — only link a session that exists so a
        # stale/unknown id degrades to a project-level HITL instead of crashing.
        _sid = session_id or None
        if _sid:
            async with db.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (_sid,)
            ) as _cur:
                if await _cur.fetchone() is None:
                    _sid = None
        hitl = await request_hitl(
            db, project_id,
            (
                f"Assumption INVALIDATED for decision '{title}': "
                f"{(finding or '').strip()[:300]}. Work depending on this "
                "decision is blocked — decide how to proceed (revise the "
                "decision, re-scope dependent items, or proceed anyway)."
            ),
            session_id=_sid,
            context=(
                f"decision_id={decision_id}; assumption="
                f"{(decision.get('assumption') or '(none recorded)')[:300]}"
            ),
            urgency="blocking",
        )
    return {
        "decision": updated,
        "assumption_status": status,
        "note": note,
        "hitl": hitl,
    }


# ---------------------------------------------------------------------------
# save_finding (e1f43ee7) — phase-agnostic capture primitive. Decoupled from any
# search tool: turns a finding from web/arxiv/code/conversation into a durable,
# addressable note with provenance. capture_research_finding (b1d36e93) is the
# web/paper-shaped wrapper over it.
# ---------------------------------------------------------------------------
_FINDING_SOURCE_TYPES = ("web", "arxiv", "code", "conversation")


async def save_finding(
    db: aiosqlite.Connection,
    project_id: str,
    summary: str,
    *,
    source_url: str | None = None,
    source_type: str = "web",
    decision_id: str | None = None,
) -> dict[str, Any]:
    """Persist a finding as an addressable ``kind='finding'`` note with provenance.

    The note is tagged ``finding`` + the source_type (so it's discoverable via
    get_notes(tag='finding')), carries ``source_url`` as provenance, and — when
    ``decision_id`` is given — is also tagged ``decision:<id>`` to link it to that
    pinned decision. The note title is derived from the first line of ``summary``.

    Returns ``{note, source_type, decision_id}``. Raises ValueError if
    ``decision_id`` is given but no such decision exists.
    """
    st = (source_type or "web").strip().lower()
    if st not in _FINDING_SOURCE_TYPES:
        st = "web"
    summary = summary or ""
    stripped = summary.strip()
    first_line = stripped.splitlines()[0] if stripped else "finding"
    title = f"Finding: {first_line}"[:200]
    tags = f"finding,{st}"
    linked: str | None = None
    if decision_id:
        if await get_pinned_decision(db, decision_id) is None:
            raise ValueError("decision not found")
        tags = f"{tags},decision:{decision_id}"
        linked = decision_id
    note = await add_project_note(
        db, project_id, title, summary, tags,
        kind="finding", source=source_url,
    )
    return {"note": note, "source_type": st, "decision_id": linked}


# ---------------------------------------------------------------------------
# get_last_session_brief (81170c84) — "what did the last session do" for a
# planning chat: the most recent session's completed sprint items, task log, and
# the latest pinned decisions, so a planner sees executor output without manual
# copy-paste.
# ---------------------------------------------------------------------------
async def get_last_session_brief(
    db: aiosqlite.Connection, project_id: str, *, exclude_session_id: str | None = None
) -> dict[str, Any] | None:
    """Summarize the most recent session for a project, or None if there are none.

    ``exclude_session_id`` skips a session (e.g. the planner's own) so the brief
    reports the last *other* session. Completed items are linked via task_id →
    task.session_id. Returns name/status/last_seen + completed_items +
    recent_tasks + recent_pinned_decisions.
    """
    sessions = await get_sessions(db, project_id, active_only=False)
    if exclude_session_id:
        sessions = [s for s in sessions if s.get("id") != exclude_session_id]
    if not sessions:
        return None
    last = sessions[0]
    sid = last["id"]
    all_tasks = await get_tasks(db, project_id, limit=200)
    session_task_ids = {t["id"] for t in all_tasks if t.get("session_id") == sid}
    session_tasks = [t for t in all_tasks if t.get("session_id") == sid][:15]
    items_all = await get_sprint_items(db, project_id)
    completed_items = [
        {
            "id": it["id"],
            "title": (it.get("title") or "")[:120],
            "status": it.get("status"),
        }
        for it in items_all
        if it.get("task_id") in session_task_ids
        and it.get("status") in {"done", "skipped", "failed", "pushed"}
    ][:20]
    pinned = await get_pinned_decisions(db, project_id)
    recent_decisions = [
        {"id": d.get("id"), "title": (d.get("title") or "")[:120]}
        for d in pinned[:3]
    ]
    return {
        "session_id": sid,
        "name": last.get("name"),
        "human_id": last.get("human_id"),
        "status": last.get("status"),
        "last_seen": last.get("last_seen"),
        "session_summary": last.get("session_summary"),
        "completed_items": completed_items,
        "recent_tasks": [
            {
                "description": (t.get("description") or "")[:160],
                "status": t.get("status"),
                "kind": t.get("kind"),
            }
            for t in session_tasks
        ],
        "recent_pinned_decisions": recent_decisions,
    }


async def get_session_file_claims(
    db: aiosqlite.Connection, session_id: str
) -> list[str]:
    """Return the file paths a session currently holds an active lock on.

    1750dccf — used by get_session_brief(role='executor') to remind an executor
    what it has claimed. Stale locks are expired first so the list is live.
    """
    await expire_file_locks(db)
    async with db.execute(
        "SELECT file_path FROM file_locks WHERE session_id = ? ORDER BY file_path",
        (session_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [r["file_path"] for r in rows if r is not None]
