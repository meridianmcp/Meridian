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
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

_log = logging.getLogger(__name__)

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
        CHECK (status IN ('pending','todo','in_progress','done','failed','skipped','pushed')),
    item_group TEXT,
    pushed_to TEXT,
    human_id TEXT,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    task_id TEXT,
    notes TEXT,
    feedback_thumb SMALLINT,
    feedback_note TEXT,
    milestone_type TEXT NOT NULL DEFAULT 'task'
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
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','superseded')),
    superseded_by TEXT REFERENCES decisions_pinned(id),
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
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    tenant_id TEXT REFERENCES tenants(id),
    client_id TEXT,
    exp BIGINT NOT NULL,
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

-- v3.4 — workspace-level settings: tenant-global defaults that every project
-- session can read at startup (e.g. a default HITL auto-answer posture, a
-- default sprint label). Singleton row (id='singleton') per workspace DB, same
-- one-workspace-per-DB model as workspace_notes / workspace_decisions.
CREATE TABLE IF NOT EXISTS workspace_settings (
    id TEXT PRIMARY KEY DEFAULT 'singleton',
    hitl_auto_answer_default INTEGER NOT NULL DEFAULT 0,
    sprint_name_default TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
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


async def _migrate_task_log_backlog_future(db: aiosqlite.Connection) -> None:
    """Rebuild ``task_log`` to add 'backlog' and 'future' statuses (v1.9.x).

    SQLite cannot ALTER a CHECK constraint, so the table is rebuilt in-place
    with the extended status list. No-op when already migrated.
    """
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_log'"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    table_sql = (row["sql"] if isinstance(row, dict) else row[0]) or ""
    if "'backlog'" in table_sql:
        return  # already migrated
    # Introspect exact columns — handles any schema version (4, 8, 10 cols)
    async with db.execute("PRAGMA table_info(task_log)") as _cur:
        _col_rows = list(await _cur.fetchall())
    _col_names = [(r["name"] if isinstance(r, dict) else r[1]) for r in _col_rows]

    def _make_col_def(r: Any) -> str:
        cid, name, ctype, notnull, dflt, pk = r
        if pk:
            return f"{name} {ctype} PRIMARY KEY"
        if name == "status":
            return (
                f"{name} {ctype} NOT NULL DEFAULT 'done' CHECK "
                "(status IN ('pending','in_progress','done','failed',"
                "'pending-hitl','backlog','future'))"
            )
        parts = [f"{name} {ctype}"]
        if notnull:
            parts.append("NOT NULL")
        if dflt is not None:
            # PRAGMA returns datetime('now') without outer parens; wrap if needed
            wrapped = dflt if dflt.startswith("'") or dflt.lstrip('-').isdigit() else f"({dflt})"
            parts.append(f"DEFAULT {wrapped}")
        return " ".join(parts)

    col_defs = ",\n            ".join(_make_col_def(r) for r in _col_rows)
    col_list = ", ".join(_col_names)

    await db.executescript(
        f"""
        BEGIN;
        ALTER TABLE task_log RENAME TO task_log_pre_backlog;
        CREATE TABLE task_log (
            {col_defs}
        );
        INSERT INTO task_log ({col_list})
        SELECT {col_list} FROM task_log_pre_backlog;
        DROP TABLE task_log_pre_backlog;
        CREATE INDEX IF NOT EXISTS idx_tasks_project
            ON task_log(project_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tasks_session
            ON task_log(session_id);
        COMMIT;
        """
    )


async def _migrate_task_log_backburner(db: aiosqlite.Connection) -> None:
    """Rebuild ``task_log`` to add 'backburner' status (v2.5.x).

    SQLite cannot ALTER a CHECK constraint, so the table is rebuilt in-place
    with the extended status list. No-op when already migrated.
    """
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_log'"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    table_sql = (row["sql"] if isinstance(row, dict) else row[0]) or ""
    if "'backburner'" in table_sql:
        return  # already migrated
    async with db.execute("PRAGMA table_info(task_log)") as _cur:
        _col_rows = list(await _cur.fetchall())
    _col_names = [(r["name"] if isinstance(r, dict) else r[1]) for r in _col_rows]

    def _make_col_def(r: Any) -> str:
        cid, name, ctype, notnull, dflt, pk = r
        if pk:
            return f"{name} {ctype} PRIMARY KEY"
        if name == "status":
            return (
                f"{name} {ctype} NOT NULL DEFAULT 'done' CHECK "
                "(status IN ('pending','in_progress','done','failed',"
                "'pending-hitl','backlog','future','backburner'))"
            )
        parts = [f"{name} {ctype}"]
        if notnull:
            parts.append("NOT NULL")
        if dflt is not None:
            wrapped = dflt if dflt.startswith("'") or dflt.lstrip('-').isdigit() else f"({dflt})"
            parts.append(f"DEFAULT {wrapped}")
        return " ".join(parts)

    col_defs = ",\n            ".join(_make_col_def(r) for r in _col_rows)
    col_list = ", ".join(_col_names)

    await db.executescript(
        f"""
        BEGIN;
        ALTER TABLE task_log RENAME TO task_log_pre_backburner;
        CREATE TABLE task_log (
            {col_defs}
        );
        INSERT INTO task_log ({col_list})
        SELECT {col_list} FROM task_log_pre_backburner;
        DROP TABLE task_log_pre_backburner;
        CREATE INDEX IF NOT EXISTS idx_tasks_project
            ON task_log(project_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tasks_session
            ON task_log(session_id);
        COMMIT;
        """
    )


async def _migrate_task_log_hitl(db: aiosqlite.Connection) -> None:
    """Rebuild ``task_log`` if its CHECK constraint predates v0.2.0.

    SQLite can't ``ALTER`` a CHECK constraint, so on an older database we
    rebuild the table in place: copy rows out, drop, recreate with the new
    constraint, copy rows back. No-op when the schema is already current.
    """
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_log'"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    table_sql = (row["sql"] if isinstance(row, dict) else row[0]) or ""
    if "pending-hitl" in table_sql:
        return  # already migrated

    await db.executescript(
        """
        BEGIN;
        ALTER TABLE task_log RENAME TO task_log_v01;
        CREATE TABLE task_log (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'done'
                CHECK (status IN ('pending','in_progress','done','failed','pending-hitl')),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO task_log (id, session_id, project_id, description, status, created_at)
        SELECT id, session_id, project_id, description, status, created_at
        FROM task_log_v01;
        DROP TABLE task_log_v01;
        CREATE INDEX IF NOT EXISTS idx_tasks_project
            ON task_log(project_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tasks_session
            ON task_log(session_id);
        COMMIT;
        """
    )


async def _column_exists(
    db: aiosqlite.Connection, table: str, column: str
) -> bool:
    """Return True if ``column`` already exists on ``table`` in this DB."""
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return any((row["name"] if isinstance(row, dict) else row[1]) == column for row in rows)


async def _migrate_add_column_if_missing(
    db: aiosqlite.Connection, table: str, column: str, decl: str
) -> None:
    """Idempotently ``ALTER TABLE ADD COLUMN`` if it's not already there."""
    if not await _column_exists(db, table, column):
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        await db.commit()


async def _migrate_human_identity(db: aiosqlite.Connection) -> None:
    """v0.3.2 — add nullable human-identity columns to legacy DBs."""
    await _migrate_add_column_if_missing(db, "projects", "creator_human_id", "TEXT")
    await _migrate_add_column_if_missing(db, "sessions", "human_id", "TEXT")


async def _migrate_v24_task_tree_and_framework(db: aiosqlite.Connection) -> None:
    """v2.4 — task_log.parent_task_id (task tree for sub-agent work) and
    sessions.agent_framework (claude_code | cursor | windsurf | langgraph
    | autogen | openviking | custom). Also projects.project_token for the
    POST /events webhook intake (framework integrations push events with
    X-Meridian-Token header)."""
    await _migrate_add_column_if_missing(db, "task_log", "parent_task_id", "TEXT")
    await _migrate_add_column_if_missing(
        db, "sessions", "agent_framework", "TEXT DEFAULT 'claude_code'"
    )
    await _migrate_add_column_if_missing(db, "projects", "project_token", "TEXT")


async def _migrate_v25_feedback_and_notifications(db: aiosqlite.Connection) -> None:
    """v2.5 — sprint_items feedback columns + tenants notification_prefs."""
    await _migrate_add_column_if_missing(
        db, "sprint_items", "feedback_thumb", "SMALLINT"
    )
    await _migrate_add_column_if_missing(
        db, "sprint_items", "feedback_note", "TEXT"
    )
    await _migrate_add_column_if_missing(
        db, "tenants", "notification_prefs", "TEXT NOT NULL DEFAULT '{}'"
    )


async def _migrate_v33_hitl_kind_payload(db: aiosqlite.Connection) -> None:
    """v3.3 — hitl_requests.kind discriminates a normal 'question' from an
    'md_section_update' (a proposed markdown section replacement); payload is a
    JSON blob carrying {file, anchor, content, base_hash, diff} for the latter."""
    await _migrate_add_column_if_missing(
        db, "hitl_requests", "kind", "TEXT NOT NULL DEFAULT 'question'"
    )
    await _migrate_add_column_if_missing(db, "hitl_requests", "payload", "TEXT")


async def _migrate_v34_hitl_auto_answer(db: aiosqlite.Connection) -> None:
    """v3.4 — projects.hitl_auto_answer: when on, request_hitl auto-resolves
    immediately (first option, answered_by='auto') so trusted projects never
    block on the HITL queue. Auto-answered rows stay in the queue for audit."""
    await _migrate_add_column_if_missing(
        db, "projects", "hitl_auto_answer", "INTEGER NOT NULL DEFAULT 0"
    )


async def _migrate_v34_workspace_settings(db: aiosqlite.Connection) -> None:
    """v3.4 — workspace_settings singleton table (tenant-global defaults).
    CREATE_TABLES covers fresh DBs; this is the upgrade path for existing ones."""
    await db.executescript(
        "CREATE TABLE IF NOT EXISTS workspace_settings ("
        "    id TEXT PRIMARY KEY DEFAULT 'singleton',"
        "    hitl_auto_answer_default INTEGER NOT NULL DEFAULT 0,"
        "    sprint_name_default TEXT,"
        "    updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ");"
    )
    await db.commit()


async def _migrate_dunning_fields(db: aiosqlite.Connection) -> None:
    """v2.6 — dunning: track when a tenant's payment first failed."""
    await _migrate_add_column_if_missing(db, "tenants", "payment_failed_at", "TEXT")
    await _migrate_add_column_if_missing(db, "tenants", "dunning_email_sent", "INTEGER NOT NULL DEFAULT 0")
    # v2.7 — add github_sub for GitHub OAuth
    await _migrate_add_column_if_missing(db, "tenants", "github_sub", "TEXT")


async def _migrate_overage_fields(db: aiosqlite.Connection) -> None:
    """v2.6 — per-tenant compute + storage overage tracking and caps."""
    await _migrate_add_column_if_missing(db, "tenants", "compute_overage_cap_usd", "NUMERIC(8,2) DEFAULT 0")
    await _migrate_add_column_if_missing(db, "tenants", "storage_overage_cap_usd", "NUMERIC(8,2) DEFAULT 0")
    await _migrate_add_column_if_missing(db, "tenants", "compute_cu_hours_used", "NUMERIC(10,4) DEFAULT 0")
    await _migrate_add_column_if_missing(db, "tenants", "storage_gb_used", "NUMERIC(10,4) DEFAULT 0")
    await _migrate_add_column_if_missing(db, "tenants", "overage_reset_at", "TEXT")
    await _migrate_add_column_if_missing(db, "tenants", "compute_throttled_at", "TEXT")


async def _migrate_v26_client_type(db: aiosqlite.Connection) -> None:
    """v2.6 — sessions.client_type: claude-code | claude-desktop | cursor | other."""
    await _migrate_add_column_if_missing(db, "sessions", "client_type", "TEXT")


async def _migrate_ntfy_notifications(db: aiosqlite.Connection) -> None:
    """v1.0.1 — projects.ntfy_url: optional ntfy push notification endpoint.

    Stores the ntfy server URL + topic (e.g. https://ntfy.sh/my-topic) so
    the server can push HITL and sprint-complete notifications without email.
    Works for self-hosted (SQLite) and hosted (Postgres via the pg_adapter).
    """
    await _migrate_add_column_if_missing(db, "projects", "ntfy_url", "TEXT")


async def _migrate_github_integration(db: aiosqlite.Connection) -> None:
    """v3.1 — per-tenant GitHub PAT and repo for the GitHub MCP tools.

    github_pat stores the encrypted personal access token.
    github_repo stores owner/repo (e.g. "acme/myapp").
    github_branch stores the default branch (default: "main").
    """
    await _migrate_add_column_if_missing(db, "tenants", "github_pat", "TEXT")
    await _migrate_add_column_if_missing(db, "tenants", "github_repo", "TEXT")
    await _migrate_add_column_if_missing(db, "tenants", "github_branch", "TEXT")


async def _migrate_sprint_item_dependencies(db: aiosqlite.Connection) -> None:
    """v2.6 — sprint_items dependency tracking.

    depends_on: foreign key to parent sprint item (NULL = no dependency).
    failure_mode: what to do when depends_on item has failed.
      'continue' (default) — this item can still be claimed.
      'stop' — this item is blocked when the parent has failed.
    """
    await _migrate_add_column_if_missing(db, "sprint_items", "depends_on", "TEXT")
    await _migrate_add_column_if_missing(
        db, "sprint_items", "failure_mode", "TEXT NOT NULL DEFAULT 'continue'"
    )


async def _migrate_v09_notes_and_magic_links(db: aiosqlite.Connection) -> None:
    """v0.9 — project_notes (per-project wiki) + magic_link_tokens
    (email magic-link auth). Both new in v0.9; idempotent CREATE."""
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS project_notes (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            tags TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_notes_project
            ON project_notes(project_id);

        CREATE TABLE IF NOT EXISTS magic_link_tokens (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            used_at TEXT,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_magic_email
            ON magic_link_tokens(email, used_at);
        """
    )
    await db.commit()


async def _migrate_v24_pinned_decisions_and_hitl(db: aiosqlite.Connection) -> None:
    """v2.4 — decisions_pinned + hitl_requests tables. CREATE_TABLES adds
    them on fresh DBs; this migration covers existing dev/prod DBs."""
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS decisions_pinned (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'TECHNICAL',
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','superseded')),
            superseded_by TEXT REFERENCES decisions_pinned(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_decisions_pinned_project
            ON decisions_pinned(project_id, status);

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
            answered_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_hitl_project
            ON hitl_requests(project_id, status);
        CREATE INDEX IF NOT EXISTS idx_hitl_assigned
            ON hitl_requests(assigned_to, status);
        """
    )
    await db.commit()


async def _migrate_goal_field_timestamps(db: aiosqlite.Connection) -> None:
    """v2.3 — per-field updated-at timestamps on goal_states.

    Pre-v2.3 every goal save inserted a new row, so per-field freshness
    could be walked from row history. v2.3's dedup means sprint-only
    changes are in-place UPDATEs on the latest row, collapsing the
    per-field history. Three new nullable columns let us record which
    field changed when. ``get_goal_field_ages`` reads these; absent
    values fall back to the row's ``updated_at`` so pre-migration data
    still renders.
    """
    await _migrate_add_column_if_missing(db, "goal_states", "ns_updated_at", "TEXT")
    await _migrate_add_column_if_missing(db, "goal_states", "content_updated_at", "TEXT")
    await _migrate_add_column_if_missing(db, "goal_states", "sprint_updated_at", "TEXT")


async def _migrate_task_claims(db: aiosqlite.Connection) -> None:
    """v0.3.3 — add ``claimed_by`` / ``claimed_at`` columns for the
    distributed task lock. Both nullable, so ALTER TABLE is safe."""
    await _migrate_add_column_if_missing(db, "task_log", "claimed_by", "TEXT")
    await _migrate_add_column_if_missing(db, "task_log", "claimed_at", "TEXT")


async def _migrate_task_sprint_link(db: aiosqlite.Connection) -> None:
    """v2.6 — link task_log rows back to their sprint item when applicable."""
    await _migrate_add_column_if_missing(
        db, "task_log", "sprint_item_id", "TEXT"
    )


async def _migrate_session_type(db: aiosqlite.Connection) -> None:
    """v1.2.0 — distinguish human vs worker sessions.

    'human' = the default startup protocol's session (full goal +
    decisions + ambient context). 'worker' = a slim context built
    by ``start_worker_session`` with just the task it needs to ship.
    """
    await _migrate_add_column_if_missing(
        db, "sessions", "session_type", "TEXT DEFAULT 'human'"
    )


async def _migrate_session_summary(db: aiosqlite.Connection) -> None:
    """v1.2.1 — store an LLM-generated session retrospective on the
    session row itself. Populated by :func:`summarize_session` on
    handoff / TTL expiry when the session shipped >=3 tasks."""
    await _migrate_add_column_if_missing(
        db, "sessions", "session_summary", "TEXT"
    )


async def _migrate_parent_session_id(db: aiosqlite.Connection) -> None:
    """v1.2.1 — propagate parent session through enqueued worker
    subprocesses. Stored on each task_log row so the timeline can
    show 'this task was kicked off by that other session'."""
    await _migrate_add_column_if_missing(
        db, "task_log", "parent_session_id", "TEXT"
    )


async def _migrate_decisions(db: aiosqlite.Connection) -> None:
    """v1.1.4 — append-only decisions log per project.

    Stored as a TEXT blob on ``projects``. New entries are prepended
    by :func:`set_decision` with a UTC date stamp so the file reads
    newest-first.
    """
    await _migrate_add_column_if_missing(db, "projects", "decisions", "TEXT")


async def _migrate_goal_mode(db: aiosqlite.Connection) -> None:
    """v0.4.2 — add ``goal_mode`` column to projects.

    SQLite ``ALTER TABLE ADD COLUMN`` cannot include a CHECK constraint,
    so we add the column with a plain default and rely on the Python
    layer (``set_goal_mode``) to validate the input value.
    """
    await _migrate_add_column_if_missing(
        db,
        "projects",
        "goal_mode",
        "TEXT NOT NULL DEFAULT 'manual'",
    )


async def _migrate_worker_pid(db: aiosqlite.Connection) -> None:
    """v1.0.1 — add ``worker_pid`` column for the PID watchdog.

    Stores the OS PID of the subprocess spawned by ``enqueue_claude_task``.
    The auto-summary loop uses this to detect orphaned in_progress tasks
    whose worker process has died."""
    await _migrate_add_column_if_missing(db, "task_log", "worker_pid", "INTEGER")


async def _migrate_rewind_token(db: aiosqlite.Connection) -> None:
    """v1.3.0 — add ``rewind_token`` column to projects for shareable
    read-only links into the rewind endpoint. Nullable: a project
    has no token until POST /rewind-token mints one."""
    await _migrate_add_column_if_missing(db, "projects", "rewind_token", "TEXT")


async def _migrate_project_settings(db: aiosqlite.Connection) -> None:
    """v2.6.1 — add per-project settings columns."""
    await _migrate_add_column_if_missing(
        db,
        "projects",
        "max_pinned_decisions",
        "INTEGER NOT NULL DEFAULT 20",
    )
    await _migrate_add_column_if_missing(
        db,
        "projects",
        "executor_config",
        "TEXT",
    )


async def _migrate_neon_pool_projects_free_tier(db: aiosqlite.Connection) -> None:
    """v3.1 — widen ``neon_pool_projects.tier`` to include ``free``."""
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='neon_pool_projects'"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    ddl = (row["sql"] if isinstance(row, dict) else row[0]) or ""
    if "'free'" in ddl or '"free"' in ddl:
        return
    await db.execute("PRAGMA foreign_keys = OFF")
    await db.executescript(
        """
        CREATE TABLE neon_pool_projects_new (
            id TEXT PRIMARY KEY,
            neon_project_id TEXT NOT NULL UNIQUE,
            tier TEXT NOT NULL DEFAULT 'standard'
                CHECK (tier IN ('free','standard','pro')),
            customer_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO neon_pool_projects_new (id, neon_project_id, tier, customer_count, created_at)
            SELECT id, neon_project_id, tier, customer_count, created_at
            FROM neon_pool_projects;
        DROP TABLE neon_pool_projects;
        ALTER TABLE neon_pool_projects_new RENAME TO neon_pool_projects;
        CREATE INDEX IF NOT EXISTS idx_neon_pool_projects_tier
            ON neon_pool_projects(tier, customer_count);
        """
    )
    await db.execute("PRAGMA foreign_keys = ON")
    await db.commit()


async def _migrate_tenants_free_plan(db: aiosqlite.Connection) -> None:
    """v2.9 — expand tenants.plan to allow 'free' and add trial/expiry columns.

    The old plan CHECK constraint restricted values to 'standard'|'pro'.
    SQLite can't ALTER a CHECK, so rebuild the table if constrained.
    Also adds trial_started_at + inactivity_expires_at for the free tier.
    """
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tenants'"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    ddl = (row["sql"] if isinstance(row, dict) else row[0]) or ""
    needs_rebuild = "CHECK (plan IN" in ddl
    if needs_rebuild:
        await db.execute("PRAGMA foreign_keys = OFF")
        await db.executescript(
            """
            CREATE TABLE tenants_new (
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
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO tenants_new (
                id, email, google_sub, github_sub, microsoft_sub,
                neon_project_id, neon_db_url, stripe_customer_id,
                stripe_metered_item_id, plan, pool_project_id,
                notification_prefs, created_at
            )
            SELECT
                id, email, google_sub, github_sub, microsoft_sub,
                neon_project_id, neon_db_url, stripe_customer_id,
                stripe_metered_item_id, plan, pool_project_id,
                COALESCE(notification_prefs, '{}'), created_at
            FROM tenants;
            DROP TABLE tenants;
            ALTER TABLE tenants_new RENAME TO tenants;
            """
        )
        await db.execute("PRAGMA foreign_keys = ON")
        await db.commit()
    # Idempotently add new columns (safe even after rebuild)
    await _migrate_add_column_if_missing(db, "tenants", "trial_started_at", "TEXT")
    await _migrate_add_column_if_missing(db, "tenants", "inactivity_expires_at", "TEXT")


async def _migrate_decisions_free_category(db: aiosqlite.Connection) -> None:
    """v2.9 — drop the hard category CHECK constraint on decisions_pinned.

    Category is now free-text (any string); the old enum was too rigid.
    SQLite can't DROP a CHECK constraint, so rebuild the table in place.
    No-op when the constraint is already absent.
    """
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='decisions_pinned'"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    ddl = (row["sql"] if isinstance(row, dict) else row[0]) or ""
    if "CHECK (category IN" not in ddl:
        return  # already migrated or freshly created without constraint
    await db.execute("PRAGMA foreign_keys = OFF")
    await db.executescript(
        """
        CREATE TABLE decisions_pinned_new (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'TECHNICAL',
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','superseded')),
            superseded_by TEXT REFERENCES decisions_pinned_new(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO decisions_pinned_new
            SELECT id, project_id, title, body, category, status,
                   superseded_by, created_at, updated_at
            FROM decisions_pinned;
        DROP TABLE decisions_pinned;
        ALTER TABLE decisions_pinned_new RENAME TO decisions_pinned;
        CREATE INDEX IF NOT EXISTS idx_decisions_pinned_project
            ON decisions_pinned(project_id, status);
        """
    )
    await db.execute("PRAGMA foreign_keys = ON")
    await db.commit()


async def _migrate_sessions_archived(db: aiosqlite.Connection) -> None:
    """v1.8.x — add 'archived' to the sessions CHECK constraint.

    SQLite can't ALTER a CHECK constraint, so we rebuild the table in place
    when the existing CHECK doesn't include 'archived'. No-op when current.

    task_log has a FK → sessions, so we disable FK enforcement for the
    rename/drop cycle (PRAGMA foreign_keys is a no-op inside a transaction,
    so it must be toggled at the Python level before executescript).
    """
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    if "archived" in ((row["sql"] if isinstance(row, dict) else row[0]) or ""):
        return  # already migrated

    # SQLite 3.26+ auto-rewrites FK references in other tables when a table is
    # RENAMED — so renaming sessions → sessions_v17 would silently update
    # task_log's schema to say "REFERENCES sessions_v17" and break after the
    # drop.  The safe pattern is: create sessions_new, copy, DROP old (not
    # rename), then RENAME new→sessions.  task_log's "REFERENCES sessions(id)"
    # is never touched and remains valid once sessions is back.
    await db.execute("PRAGMA foreign_keys = OFF")
    await db.executescript(
        """
        CREATE TABLE sessions_new (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            name TEXT NOT NULL,
            human_id TEXT,
            session_type TEXT DEFAULT 'human',
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','idle','closed','archived')),
            last_seen TEXT NOT NULL DEFAULT (datetime('now')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            session_summary TEXT
        );
        INSERT INTO sessions_new
            SELECT id, project_id, name, human_id, session_type, status,
                   last_seen, created_at, session_summary
            FROM sessions;
        DROP TABLE sessions;
        ALTER TABLE sessions_new RENAME TO sessions;
        CREATE INDEX IF NOT EXISTS idx_sessions_project
            ON sessions(project_id, status);
        """
    )
    await db.execute("PRAGMA foreign_keys = ON")
    await db.commit()


async def _migrate_goal_hierarchy(db: aiosqlite.Connection) -> None:
    """v0.5.2 — add ``goal_north_star`` and ``goal_sprint`` columns.

    Seeding: for each project's latest goal row that has no north_star
    set yet, copy the current content into north_star so existing goals
    are promoted to the structured hierarchy automatically.
    """
    await _migrate_add_column_if_missing(
        db, "goal_states", "goal_north_star", "TEXT"
    )
    await _migrate_add_column_if_missing(
        db, "goal_states", "goal_sprint", "TEXT"
    )
    # Seed: promote content → north_star for the latest version per project
    # where north_star is still NULL (i.e., legacy rows from before v0.5.2).
    await db.execute(
        """
        UPDATE goal_states
        SET goal_north_star = content
        WHERE goal_north_star IS NULL
          AND id IN (
              SELECT id FROM goal_states g2
              WHERE g2.project_id = goal_states.project_id
              ORDER BY version DESC
              LIMIT 1
          )
        """
    )
    await db.commit()


async def _migrate_sprint_items_v2(db: aiosqlite.Connection) -> None:
    """v1.9x — expand sprint_items: add item_group/pushed_to/human_id and
    widen status CHECK to include 'todo', 'failed', 'pushed'.

    Can't ALTER a CHECK constraint in SQLite, so the table is rebuilt in
    place using the create-copy-drop-rename pattern. sprint_items has no
    FK references FROM other tables (it only has a FK TO projects) so the
    RENAME is safe without toggling foreign_keys.

    No-op when the schema is already current (all new columns present and
    status constraint includes 'failed').
    """
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sprint_items'"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return  # fresh DB — CREATE_TABLES builds the current schema
    sql = (row["sql"] if isinstance(row, dict) else row[0]) or ""
    # Already migrated when all three new columns + 'failed' status are present.
    if ("item_group" in sql and "pushed_to" in sql
            and "human_id" in sql and "'failed'" in sql):
        return
    # Rebuild the table with the new schema.
    await db.executescript(
        """
        CREATE TABLE sprint_items_new (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            version TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN
                    ('pending','todo','in_progress','done','failed','skipped','pushed')),
            item_group TEXT,
            pushed_to TEXT,
            human_id TEXT,
            added_at TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at TEXT,
            task_id TEXT,
            notes TEXT
        );
        INSERT INTO sprint_items_new
            (id, project_id, version, title, status,
             added_at, completed_at, task_id, notes)
            SELECT id, project_id, version, title, status,
                   added_at, completed_at, task_id, notes
            FROM sprint_items;
        DROP TABLE sprint_items;
        ALTER TABLE sprint_items_new RENAME TO sprint_items;
        CREATE INDEX IF NOT EXISTS idx_sprint_items_project
            ON sprint_items(project_id, status);
        CREATE INDEX IF NOT EXISTS idx_sprint_items_version
            ON sprint_items(project_id, version);
        """
    )
    await db.commit()


async def _migrate_drop_chat_tables(db: aiosqlite.Connection) -> None:
    """v1.9.x — drop abandoned chat_sessions and chat_messages tables.

    These were removed in v1.1.0 when the in-dashboard chat feature was
    dropped. DROP IF EXISTS is idempotent — safe on both old and fresh DBs.
    """
    await db.execute("DROP TABLE IF EXISTS chat_messages")
    await db.execute("DROP TABLE IF EXISTS chat_sessions")
    await db.commit()


async def _migrate_hosted_tables(db: aiosqlite.Connection) -> None:
    """v2.0/v2.2 — add tenants, user_sessions, api_tokens, neon_pool_projects.

    Uses CREATE TABLE IF NOT EXISTS so it is idempotent on existing DBs.
    v2.2: plan column migrated free/team→standard; pool_project_id column added.
    """
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS tenants (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            google_sub TEXT UNIQUE,
            neon_project_id TEXT,
            neon_db_url TEXT,
            stripe_customer_id TEXT,
            plan TEXT NOT NULL DEFAULT 'standard',
            pool_project_id TEXT,
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
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS oauth_tokens (
            token_hash TEXT PRIMARY KEY,
            tenant_id TEXT REFERENCES tenants(id),
            client_id TEXT,
            exp BIGINT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS neon_pool_projects (
            id TEXT PRIMARY KEY,
            neon_project_id TEXT NOT NULL UNIQUE,
            tier TEXT NOT NULL DEFAULT 'standard',
            customer_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    # Migrate legacy plan values (free→standard, team→standard)
    await db.execute(
        "UPDATE tenants SET plan='standard' WHERE plan IN ('free','team')"
    )
    await _migrate_add_column_if_missing(db, "tenants", "pool_project_id", "TEXT")
    await _migrate_add_column_if_missing(db, "tenants", "microsoft_sub", "TEXT")
    await _migrate_add_column_if_missing(db, "tenants", "stripe_metered_item_id", "TEXT")
    await _migrate_github_integration(db)
    await db.commit()


async def _migrate_session_notes(db: aiosqlite.Connection) -> None:
    """v2.6 — create session_notes table if not present. Idempotent."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS session_notes (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.commit()


async def _migrate_milestone_type(db: aiosqlite.Connection) -> None:
    """v3.0 — add milestone_type to sprint_items. Idempotent."""
    await _migrate_add_column_if_missing(
        db, "sprint_items", "milestone_type", "TEXT NOT NULL DEFAULT 'task'"
    )


async def _migrate_executor_runs(db: aiosqlite.Connection) -> None:
    """v3.0 — create executor_runs table if not present. Idempotent."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS executor_runs (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            ended_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            transcript TEXT NOT NULL DEFAULT '',
            task_count INTEGER NOT NULL DEFAULT 0
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_executor_runs_session ON executor_runs(session_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_executor_runs_project "
        "ON executor_runs(project_id, started_at DESC)"
    )
    await db.commit()


async def _migrate_file_locks(db: aiosqlite.Connection) -> None:
    """v3.1 — add file_locks table for cross-session edit coordination."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS file_locks (
            id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL UNIQUE,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_locks_session ON file_locks(session_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_locks_expires ON file_locks(expires_at)"
    )
    await db.commit()


async def _migrate_workspace_layer(db: aiosqlite.Connection) -> None:
    """v3.1 — workspace_notes + workspace_decisions tables (tenant-global,
    above projects). Idempotent."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS workspace_notes (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            tags TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS workspace_decisions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'TECHNICAL',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspace_notes_created "
        "ON workspace_notes(created_at DESC)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspace_decisions_status "
        "ON workspace_decisions(status, created_at DESC)"
    )
    await db.commit()


async def _migrate_checkpoint_data(db: aiosqlite.Connection) -> None:
    """v3.1 — add sessions.checkpoint_data for per-session checkpoint snapshots
    (replaces the old checkpoint: project_notes hack). Idempotent. Also clears
    the legacy checkpoint:* notes now superseded by the column."""
    await _migrate_add_column_if_missing(db, "sessions", "checkpoint_data", "TEXT")
    await db.execute(
        "DELETE FROM project_notes WHERE title LIKE ?", ("checkpoint:%",)
    )
    await db.commit()


async def init_hosted_tables(db: aiosqlite.Connection) -> None:
    """Ensure hosted-tier tables exist on the given connection.

    Idempotent — safe to call on both fresh and existing databases.
    Delegates to ``_migrate_hosted_tables`` for SQLite; for Postgres the
    tables are already included in ``CREATE_TABLES_PG``.
    """
    await _migrate_hosted_tables(db)


async def init_db(db_path: str) -> aiosqlite.Connection:
    """Open the database, apply schema, and return the connection.

    When ``db_path`` starts with ``postgresql://`` or ``postgres://`` the
    function opens a psycopg3 connection pool and returns a
    :class:`~meridian.pg_adapter.PostgresConnection` instead of an
    aiosqlite connection.  Both expose the same async cursor API so all
    callers are unaffected.

    For Postgres, only ``CREATE TABLE IF NOT EXISTS`` is run — migration
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
    await _migrate_milestone_type(db)
    await _migrate_ntfy_notifications(db)
    await _migrate_github_integration(db)
    await _migrate_workspace_layer(db)
    await _migrate_checkpoint_data(db)
    await _migrate_v33_hitl_kind_payload(db)
    await _migrate_v34_hitl_auto_answer(db)
    await _migrate_v34_workspace_settings(db)
    await _migrate_project_icon(db)
    await _migrate_tenants_is_internal(db)
    await _migrate_workspace_members_rbac(db)
    return db


async def _migrate_workspace_members_rbac(db: aiosqlite.Connection) -> None:
    """G5.19 / G5.20 — widen workspace_members.role to allow 'admin' and add
    github_access. Idempotent.

    Strategy:
     - Add github_access column if missing (default 'read').
     - Drop the legacy CHECK constraint on role by rebuilding the table
       if and only if it's present. Existing rows preserve their role
       and pick up github_access defaulted by current role.
    """
    # github_access — new column, simple ADD COLUMN.
    await _migrate_add_column_if_missing(
        db, "workspace_members", "github_access",
        "TEXT NOT NULL DEFAULT 'read'",
    )

    # Detect the legacy CHECK by inspecting the table SQL. If it has the
    # ('owner','member','viewer') tuple, rebuild without CHECK.
    async with db.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='workspace_members'"
    ) as cur:
        row = await cur.fetchone()
    sql = (row["sql"] if row else "") or ""
    if "'owner','member','viewer'" not in sql:
        return  # already widened or never had the CHECK

    # Rebuild. We pull a stable snapshot, drop, recreate, restore.
    async with db.execute("SELECT * FROM workspace_members") as cur:
        existing = await cur.fetchall()
    columns = [d[0] for d in cur.description] if cur.description else []
    await db.execute("DROP TABLE workspace_members")
    await db.execute(
        "CREATE TABLE workspace_members ("
        "  id TEXT PRIMARY KEY,"
        "  tenant_id TEXT NOT NULL REFERENCES tenants(id),"
        "  email TEXT NOT NULL,"
        "  role TEXT NOT NULL DEFAULT 'member',"
        "  github_access TEXT NOT NULL DEFAULT 'read',"
        "  token_hash TEXT,"
        "  invited_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "  joined_at TEXT"
        ")"
    )
    for r in existing:
        d = {k: r[k] for k in columns}
        await db.execute(
            "INSERT INTO workspace_members "
            "(id, tenant_id, email, role, github_access, token_hash, "
            " invited_at, joined_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                d.get("id"), d.get("tenant_id"), d.get("email"),
                d.get("role") or "member",
                d.get("github_access") or "read",
                d.get("token_hash"),
                d.get("invited_at"),
                d.get("joined_at"),
            ),
        )
    await db.commit()


async def _migrate_project_icon(db: aiosqlite.Connection) -> None:
    """G4.17 — single-emoji icon on projects for sidebar/tab rendering."""
    await _migrate_add_column_if_missing(db, "projects", "icon", "TEXT")


# G2.10 — Set of email addresses considered "internal" for lifecycle
# purposes. The migrator backfills these to is_internal=true after the
# column is added. New entries can also be added via env var
# MERIDIAN_INTERNAL_EMAILS (comma-separated).
_INTERNAL_EMAILS_DEFAULT = frozenset({
    "ajc123private@gmail.com",
    "dradamawsome@gmail.com",
    "ajc123shopping@gmail.com",
    "termh4@umsystem.edu",
})


def _internal_emails() -> frozenset[str]:
    import os
    extra = os.environ.get("MERIDIAN_INTERNAL_EMAILS", "")
    extras = {e.strip().lower() for e in extra.split(",") if e.strip()}
    return _INTERNAL_EMAILS_DEFAULT | extras


async def _migrate_tenants_is_internal(db: aiosqlite.Connection) -> None:
    """G2.10 — Add tenants.is_internal flag and backfill known internal
    emails. is_internal tenants are excluded from churn/dunning/overage/
    free-expiry warnings and deletion. Idempotent.
    """
    await _migrate_add_column_if_missing(
        db, "tenants", "is_internal", "INTEGER NOT NULL DEFAULT 0"
    )
    # Backfill known internal emails. Idempotent.
    for email in sorted(_internal_emails()):
        await db.execute(
            "UPDATE tenants SET is_internal = 1 WHERE LOWER(email) = ?",
            (email,),
        )
    await db.commit()


async def create_project(
    db: aiosqlite.Connection,
    name: str,
    human_id: str | None = None,
) -> dict[str, Any]:
    """Insert a project and return it as a dict. Raises if the name exists.

    ``human_id`` (when provided) is recorded as the project's
    ``creator_human_id``. The creator's id is the only one allowed to
    update the goal state once goal-ownership enforcement is active.
    """
    pid = _new_id()
    await db.execute(
        "INSERT INTO projects (id, name, creator_human_id) VALUES (?, ?, ?)",
        (pid, name, human_id),
    )
    await db.commit()
    project = await get_project(db, pid)
    assert project is not None
    return project


async def get_project(
    db: aiosqlite.Connection, project_id: str
) -> dict[str, Any] | None:
    """Look up a project by id."""
    async with db.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


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
        async with db.execute(
            "SELECT p.*, gs.goal_sprint AS sprint "
            "FROM projects p "
            "LEFT JOIN goal_states gs ON gs.project_id = p.id "
            "WHERE LOWER(p.name) LIKE ? "
            "ORDER BY p.created_at DESC, gs.version DESC "
            "LIMIT 1",
            (f"%{name.lower()}%",),
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
        "SELECT id, max_pinned_decisions, executor_config, hitl_auto_answer "
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
        "hitl_auto_answer": bool(data.get("hitl_auto_answer")),
    }


async def update_project_settings(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    max_pinned_decisions: int | None = None,
    executor_config: dict[str, Any] | None = None,
    hitl_auto_answer: bool | None = None,
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
        updates.append("hitl_auto_answer = ?")
        params.append(1 if hitl_auto_answer else 0)
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
    for stmt, params in [
        ("DELETE FROM sprint_items WHERE project_id = ?", (project_id,)),
        ("DELETE FROM task_log WHERE project_id = ?", (project_id,)),
        ("DELETE FROM sessions WHERE project_id = ?", (project_id,)),
        ("DELETE FROM sessions_archived WHERE project_id = ?", (project_id,)),
        ("DELETE FROM goal_states WHERE project_id = ?", (project_id,)),
        ("DELETE FROM projects WHERE id = ?", (project_id,)),  # projects uses 'id'
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
    """
    if session_type not in {"human", "worker"}:
        raise ValueError(f"invalid session_type: {session_type!r}")
    sid = _new_id()
    await db.execute(
        "INSERT INTO sessions (id, project_id, name, human_id, session_type, agent_framework, client_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, project_id, name, human_id, session_type, agent_framework, client_type),
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


async def close_session(db: aiosqlite.Connection, session_id: str) -> None:
    """Mark a session as closed and finalize its executor_run."""
    await db.execute(
        "UPDATE sessions SET status = 'closed' WHERE id = ?",
        (session_id,),
    )
    await release_file_locks_for_session(db, session_id)
    await db.commit()
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
) -> dict[str, Any]:
    """Append a task-log entry and broadcast to live subscribers.

    ``parent_session_id`` (v1.2.1) records that this task was kicked
    off by another session — typically when an enqueue_claude_task
    worker logs its result. The timeline + auto-summary use it to
    correlate parent / child sessions.

    ``parent_task_id`` (v2.4) records that this task is a sub-step of
    another task. Lets the dashboard render multi-agent work as a tree
    (researcher → fetched 3 sources, writer → drafted reply, etc.).
    """
    if status not in {"pending", "in_progress", "done", "failed", "pending-hitl", "backlog", "future", "backburner"}:
        raise ValueError(f"invalid task status: {status}")
    tid = _new_id()
    await db.execute(
        "INSERT INTO task_log "
        "(id, session_id, project_id, description, status, parent_session_id, parent_task_id, sprint_item_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tid,
            session_id,
            project_id,
            description,
            status,
            parent_session_id,
            parent_task_id,
            sprint_item_id,
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


async def search_all(
    db: Any,
    project_id: str,
    query: str,
    limit: int = 10,
) -> dict[str, Any]:
    """Universal search across task_log, project_notes, sprint_items, and decisions_pinned.

    SQLite: LIKE %query% on all relevant text fields.
    Postgres: ILIKE with optional pg_trgm similarity fallback.

    Returns grouped results: {tasks, notes, decisions, sprint_items}.
    Each item includes a `match_type` key for the source table.
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
            "SELECT id, title, version, status, added_at AS created_at, 'sprint_item' AS match_type "
            "FROM sprint_items "
            "WHERE project_id = ? AND title ILIKE ? "
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
            "SELECT id, title, version, status, added_at AS created_at, 'sprint_item' AS match_type "
            "FROM sprint_items "
            "WHERE project_id = ? AND title LIKE ? "
            "ORDER BY added_at DESC LIMIT ?"
        )

    tasks = await _search(tasks_sql, (project_id, like_pat, limit))
    notes = await _search(notes_sql, (project_id, like_pat, like_pat, limit))
    decisions = await _search(decisions_sql, (project_id, like_pat, like_pat, limit))
    sprint_items = await _search(sprint_sql, (project_id, like_pat, limit))

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
        "SELECT id, sprint_item_id FROM task_log "
        "WHERE claimed_by IN ("
        "  SELECT id FROM sessions WHERE status IN ('active', 'idle') AND last_seen < ?"
        ") AND status = 'in_progress'",
        (cutoff,),
    ) as cur:
        stale_rows = await cur.fetchall()
    stale_task_ids = [row["id"] for row in stale_rows]
    linked_item_ids = [row["sprint_item_id"] for row in stale_rows if row["sprint_item_id"]]
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
    "pending", "todo", "in_progress", "done", "failed", "skipped", "pushed"
}


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
    """
    if failure_mode not in (None, "continue", "stop"):
        raise ValueError("failure_mode must be 'continue' or 'stop'")
    if milestone_type not in ("task", "milestone"):
        raise ValueError("milestone_type must be 'task' or 'milestone'")
    iid = _new_id()
    await db.execute(
        "INSERT INTO sprint_items "
        "(id, project_id, version, title, item_group, human_id, depends_on, failure_mode, milestone_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (iid, project_id, version, title, group, human_id,
         depends_on, failure_mode or "continue", milestone_type),
    )
    await db.commit()
    item = await get_sprint_item(db, iid)
    assert item is not None
    return item


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
) -> dict[str, Any] | None:
    """Internal: flip a sprint item's status and optionally link a task.

    Terminal statuses (done / skipped / failed / pushed) stamp
    ``completed_at``; non-terminal statuses clear it. ``pushed_to``
    records the target version when status == 'pushed'.
    """
    if status not in _VALID_SPRINT_STATUSES:
        raise ValueError(f"invalid sprint-item status: {status!r}")
    fields = ["status = ?"]
    values: list[Any] = [status]
    if status in {"done", "skipped", "failed", "pushed"}:
        fields.append("completed_at = datetime('now')")
    else:
        fields.append("completed_at = NULL")
    if task_id is not None:
        fields.append("task_id = ?")
        values.append(task_id)
    if notes is not None:
        fields.append("notes = ?")
        values.append(notes)
    if pushed_to is not None:
        fields.append("pushed_to = ?")
        values.append(pushed_to)
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
    result = await get_sprint_item(db, item_id)
    # Broadcast to dashboard WebSocket subscribers so the sprint board refreshes live.
    _publish_project_event(project_id, "sprint_item_updated", {"item_id": item_id, "status": status})
    return result


async def complete_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    task_id: str | None = None,
) -> dict[str, Any] | None:
    """Mark a sprint item ``done`` and optionally link the task that shipped it."""
    return await _update_sprint_item_status(
        db, project_id, item_id, "done", task_id=task_id
    )


async def skip_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Mark a sprint item ``skipped`` (intentionally not shipped)."""
    return await _update_sprint_item_status(
        db, project_id, item_id, "skipped", notes=reason
    )


async def start_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
) -> dict[str, Any] | None:
    """Flip a sprint item from ``pending``/``todo`` to ``in_progress``."""
    return await _update_sprint_item_status(
        db, project_id, item_id, "in_progress"
    )


async def fail_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Mark a sprint item ``failed``. ``reason`` stored in ``notes``."""
    return await _update_sprint_item_status(
        db, project_id, item_id, "failed", notes=reason
    )


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
    feedback_thumb: int | None = None,
    feedback_note: str | None = None,
) -> dict[str, Any] | None:
    """Update editable fields (title, version, feedback) of a sprint item."""
    fields: list[str] = []
    values: list[Any] = []
    if title is not None:
        fields.append("title = ?")
        values.append(title)
    if version is not None:
        fields.append("version = ?")
        values.append(version)
    if feedback_thumb is not None:
        fields.append("feedback_thumb = ?")
        values.append(int(feedback_thumb))
    if feedback_note is not None:
        fields.append("feedback_note = ?")
        values.append(feedback_note)
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


async def get_sprint_items(
    db: aiosqlite.Connection,
    project_id: str,
    status: str | None = None,
    show_blocked: bool = True,
) -> list[dict[str, Any]]:
    """List sprint items for a project, oldest first.

    ``status`` filter is optional. ``None`` returns everything so the
    dashboard can render the full timeline.

    ``show_blocked=False`` hides items whose ``depends_on`` parent is not
    yet in a terminal state (done/skipped/failed/pushed), or whose parent
    has failed while the item has ``failure_mode='stop'``.
    """
    if status is not None:
        if status not in _VALID_SPRINT_STATUSES:
            raise ValueError(
                f"invalid sprint-item status filter: {status!r}. "
                f"Valid: {sorted(_VALID_SPRINT_STATUSES)}"
            )
        query = (
            "SELECT * FROM sprint_items WHERE project_id = ? "
            "AND status = ? ORDER BY added_at ASC, rowid ASC"
        )
        params: tuple = (project_id, status)
    else:
        query = (
            "SELECT * FROM sprint_items WHERE project_id = ? "
            "ORDER BY added_at ASC, rowid ASC"
        )
        params = (project_id,)
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
        "       t.session_id, s.name AS session_name, s.human_id "
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
    for r in task_rows:
        ts = r["created_at"] or ""
        day = ts[:10]
        if len(day) != 10:
            continue
        human = r["human_id"] or "(unknown)"
        bucket = by_day.get(day)
        if bucket is None:
            bucket = {"date": day, "count": 0, "humans": {}, "_sess": {}}
            by_day[day] = bucket
        bucket["count"] += 1
        bucket["humans"][human] = bucket["humans"].get(human, 0) + 1
        sid = r["session_id"] or "(none)"
        se = bucket["_sess"].get(sid)
        if se is None:
            se = {
                "session_id": r["session_id"],
                "name": r["session_name"] or "(unknown)",
                "human": human,
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
        })

    return {
        "tasks": tasks,
        "sessions": sessions,
        "goal_events": goal_events,
        "daily_counts": daily_counts,
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
    """Rule-based session end capture: bucket done tasks into Fixed/Added/Changed
    and save as a project note tagged 'auto-capture'. No-op for sessions with
    fewer than 2 done tasks so trivial sessions don't generate noise."""
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
    await add_project_note(
        db, project_id, f"Session summary ({date_str})", "\n".join(lines), tags="auto-capture"
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
    from datetime import datetime, timezone, timedelta
    tid = _new_id()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    expires_str = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO tenants (id, email, google_sub, github_sub, microsoft_sub, "
        "plan, trial_started_at, inactivity_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, email, google_sub, github_sub, microsoft_sub, "free", now_str, expires_str),
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
        "github_pat", "github_repo", "github_branch",
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
) -> dict[str, Any]:
    """Create a web session for a tenant. Returns the session dict."""
    sid = _new_id()
    await db.execute(
        "INSERT INTO user_sessions (id, tenant_id, expires_at) VALUES (?, ?, ?)",
        (sid, tenant_id, expires_at),
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


async def create_api_token(
    db: aiosqlite.Connection,
    tenant_id: str,
    label: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Generate a bearer token for a tenant.

    Returns ``(raw_token, token_row_dict)``.  The raw token is shown once
    and never stored.  Only the SHA-256 hash is persisted.
    """
    import secrets
    import hashlib
    raw = f"sk_meridian_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    tid = _new_id()
    await db.execute(
        "INSERT INTO api_tokens (id, tenant_id, token_hash, label) VALUES (?, ?, ?, ?)",
        (tid, tenant_id, token_hash, label),
    )
    await db.commit()
    async with db.execute("SELECT * FROM api_tokens WHERE id = ?", (tid,)) as cur:
        row = await cur.fetchone()
    return raw, _row_to_dict(row)


async def get_tenant_from_token_hash(
    db: aiosqlite.Connection,
    token_hash: str,
) -> dict[str, Any] | None:
    """Look up a tenant by a pre-hashed API token.  Returns tenant dict or None."""
    async with db.execute(
        "SELECT t.* FROM tenants t "
        "JOIN api_tokens a ON a.tenant_id = t.id "
        "WHERE a.token_hash = ?",
        (token_hash,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


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


async def expire_file_locks(db: aiosqlite.Connection) -> int:
    """Delete expired file locks and return how many rows were cleared."""
    cursor = await db.execute(
        "DELETE FROM file_locks WHERE expires_at <= datetime('now')"
    )
    await db.commit()
    return cursor.rowcount


async def claim_file(
    db: aiosqlite.Connection,
    file_path: str,
    session_id: str,
    *,
    ttl_hours: int = _FILE_LOCK_TTL_HOURS,
) -> dict[str, Any]:
    """Claim a file path for a session, auto-releasing expired locks first."""
    normalized = (file_path or "").strip()
    if not normalized:
        raise ValueError("file_path is required")
    await expire_file_locks(db)
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
        }

    if existing and existing.get("session_id") == session_id:
        await db.execute(
            "UPDATE file_locks SET claimed_at = datetime('now'), "
            "expires_at = datetime('now', ? || ' hours') "
            "WHERE id = ?",
            (str(ttl_hours), existing["id"]),
        )
    else:
        await db.execute(
            "INSERT INTO file_locks (id, file_path, session_id, claimed_at, expires_at) "
            "VALUES (?, ?, ?, datetime('now'), datetime('now', ? || ' hours'))",
            (_new_id(), normalized, session_id, str(ttl_hours)),
        )
    await db.commit()
    async with db.execute(
        "SELECT * FROM file_locks WHERE file_path = ?",
        (normalized,),
    ) as cur:
        row = await cur.fetchone()
    lock = _row_to_dict(row) or {}
    return {
        "claimed": True,
        "file_path": normalized,
        "session_id": lock.get("session_id"),
        "claimed_at": lock.get("claimed_at"),
        "expires_at": lock.get("expires_at"),
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
    await db.commit()
    return cursor.rowcount > 0


async def release_file_locks_for_session(
    db: aiosqlite.Connection,
    session_id: str,
) -> int:
    """Release every file lock held by a session."""
    cursor = await db.execute(
        "DELETE FROM file_locks WHERE session_id = ?",
        (session_id,),
    )
    await db.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# v2.4 — Pinned decisions (editable constitution alongside the append-only log)
# ---------------------------------------------------------------------------

_SUGGESTED_DECISION_CATEGORIES = {
    "STRATEGIC", "COMPETITIVE", "TECHNICAL", "TACTICAL",
    "BUSINESS", "PRODUCT", "ARCHITECTURAL",
}


async def pin_decision(
    db: aiosqlite.Connection,
    project_id: str,
    title: str,
    body: str,
    category: str = "TECHNICAL",
) -> dict[str, Any]:
    """Create a new pinned decision row. Returns the inserted row.

    Pinned decisions live alongside the append-only ``projects.decisions``
    log. Use this for the "current truth" set that supersedes earlier
    statements (pricing tiers, driver choices, etc). The log captures
    micro-decisions; this captures the constitution.

    category is free-text; suggested values: STRATEGIC, COMPETITIVE, TECHNICAL,
    TACTICAL, BUSINESS, PRODUCT, ARCHITECTURAL.
    """
    did = _new_id()
    await db.execute(
        "INSERT INTO decisions_pinned (id, project_id, title, body, category) "
        "VALUES (?, ?, ?, ?, ?)",
        (did, project_id, title, body, category),
    )
    await db.commit()
    return (await get_pinned_decision(db, did)) or {"id": did}


async def get_pinned_decision(
    db: aiosqlite.Connection, decision_id: str
) -> dict[str, Any] | None:
    async with db.execute(
        "SELECT * FROM decisions_pinned WHERE id = ?", (decision_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_pinned_decisions(
    db: aiosqlite.Connection,
    project_id: str,
    include_superseded: bool = False,
) -> list[dict[str, Any]]:
    """Return all pinned decisions for a project, newest first.

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
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def update_pinned_decision(
    db: aiosqlite.Connection,
    decision_id: str,
    *,
    body: str | None = None,
    category: str | None = None,
    title: str | None = None,
    status: str | None = None,
    superseded_by: str | None = None,
) -> dict[str, Any] | None:
    """Patch any combination of body / category / title / status / superseded_by.

    Use ``status='superseded'`` + ``superseded_by=<new_id>`` to retire a
    decision while preserving the audit trail. Pass only the fields you
    intend to change; others stay untouched.
    """
    existing = await get_pinned_decision(db, decision_id)
    if existing is None:
        return None
    fields: dict[str, Any] = {}
    if body is not None:
        fields["body"] = body
    if title is not None:
        fields["title"] = title
    if category is not None:
        fields["category"] = category
    if status is not None:
        if status not in ("active", "superseded"):
            raise ValueError("status must be 'active' or 'superseded'")
        fields["status"] = status
    if superseded_by is not None:
        fields["superseded_by"] = superseded_by
    if not fields:
        return existing
    from datetime import datetime, timezone
    fields["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
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
) -> dict[str, Any]:
    """Atomic supersede: create a new active decision and mark the old as superseded.

    Returns the new decision row. The old row keeps the back-link via
    ``superseded_by`` so the dashboard can render the chain.
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
) -> dict[str, Any]:
    """Create a HITL request. Returns the inserted row.

    Sessions paused on ``urgency='blocking'`` should poll
    :func:`get_hitl_request` until ``status='answered'`` to receive
    the human's reply. Non-blocking requests still show up in the
    dashboard queue — callers can keep working and check later.
    """
    if urgency not in _VALID_HITL_URGENCY:
        raise ValueError(
            f"urgency must be one of {sorted(_VALID_HITL_URGENCY)}; got {urgency!r}"
        )
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
    # v3.4 — per-project auto-answer. When the project trusts the agent, a
    # plain question is resolved immediately (first option / generic ack) so the
    # session never blocks. md_section_update diffs stay human-gated — auto
    # approving a file write would defeat the diff-approval safeguard. The row
    # stays in the queue (status='answered', answered_by='auto') for audit.
    if kind == "question" and await _project_hitl_auto_answer(db, project_id):
        auto = _auto_hitl_answer(payload)
        answered = await answer_hitl_request(db, hid, auto, answered_by="auto")
        if answered is not None:
            return answered
    return (await get_hitl_request(db, hid)) or {"id": hid}


async def _project_hitl_auto_answer(
    db: aiosqlite.Connection, project_id: str
) -> bool:
    """True when the project has the per-project HITL auto-answer toggle on."""
    async with db.execute(
        "SELECT hitl_auto_answer FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return False
    data = _row_to_dict(row) or {}
    return bool(data.get("hitl_auto_answer"))


def _auto_hitl_answer(payload: str | None) -> str:
    """Derive the auto-answer string. v1 heuristic: if the payload carries a
    non-empty ``options`` list, pick the first option; otherwise a generic ack."""
    if payload:
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
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
    """
    if status is not None and status not in _VALID_HITL_STATUS:
        raise ValueError(
            f"status must be one of {sorted(_VALID_HITL_STATUS)} or None"
        )
    where = []
    args: list[Any] = []
    if project_id is not None:
        where.append("project_id = ?")
        args.append(project_id)
    if status is not None:
        where.append("status = ?")
        args.append(status)
    where_clause = (" WHERE " + " AND ".join(where)) if where else ""
    args.append(limit)
    sql = (
        f"SELECT * FROM hitl_requests{where_clause} "
        "ORDER BY "
        "  CASE urgency WHEN 'blocking' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, "
        "  created_at DESC LIMIT ?"
    )
    async with db.execute(sql, args) as cur:
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
        "SELECT * FROM executor_runs WHERE project_id = ? "
        "ORDER BY started_at DESC LIMIT ?",
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


async def add_project_note(
    db: aiosqlite.Connection,
    project_id: str,
    title: str,
    body: str,
    tags: str | None = None,
) -> dict[str, Any]:
    """Insert a project_notes row. tags is comma-separated free-form."""
    nid = _new_id()
    await db.execute(
        "INSERT INTO project_notes (id, project_id, title, body, tags) "
        "VALUES (?, ?, ?, ?, ?)",
        (nid, project_id, title, body, tags),
    )
    await db.commit()
    return (await get_project_note(db, nid)) or {"id": nid}


async def get_project_note(
    db: aiosqlite.Connection, note_id: str
) -> dict[str, Any] | None:
    async with db.execute(
        "SELECT * FROM project_notes WHERE id = ?", (note_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_project_notes(
    db: aiosqlite.Connection,
    project_id: str,
    tag: str | None = None,
) -> list[dict[str, Any]]:
    """Return notes for a project, newest first. Optional tag filter
    matches any comma-separated tag (substring match)."""
    if tag:
        async with db.execute(
            "SELECT * FROM project_notes WHERE project_id = ? "
            "AND tags LIKE ? ORDER BY created_at DESC",
            (project_id, f"%{tag}%"),
        ) as cur:
            rows = await cur.fetchall()
    else:
        async with db.execute(
            "SELECT * FROM project_notes WHERE project_id = ? "
            "ORDER BY created_at DESC",
            (project_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def update_project_note(
    db: aiosqlite.Connection,
    note_id: str,
    *,
    title: str | None = None,
    body: str | None = None,
    tags: str | None = None,
) -> dict[str, Any] | None:
    """Patch any combination of title/body/tags. Returns updated row."""
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


async def add_workspace_note(
    db: aiosqlite.Connection,
    title: str,
    body: str,
    tags: str | None = None,
) -> dict[str, Any]:
    """Insert a workspace_notes row. tags is comma-separated free-form.
    Workspace notes belong to the whole workspace, not a single project."""
    nid = _new_id()
    await db.execute(
        "INSERT INTO workspace_notes (id, title, body, tags) "
        "VALUES (?, ?, ?, ?)",
        (nid, title, body, tags),
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
) -> list[dict[str, Any]]:
    """Return workspace notes, newest first. Optional tag substring filter."""
    if tag:
        async with db.execute(
            "SELECT * FROM workspace_notes WHERE tags LIKE ? "
            "ORDER BY created_at DESC",
            (f"%{tag}%",),
        ) as cur:
            rows = await cur.fetchall()
    else:
        async with db.execute(
            "SELECT * FROM workspace_notes ORDER BY created_at DESC",
        ) as cur:
            rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def delete_workspace_note(
    db: aiosqlite.Connection, note_id: str
) -> bool:
    """Hard-delete a workspace note. Returns True if a row was removed."""
    async with db.execute(
        "DELETE FROM workspace_notes WHERE id = ?", (note_id,)
    ) as cur:
        rc = cur.rowcount or 0
    await db.commit()
    return rc > 0


async def pin_workspace_decision(
    db: aiosqlite.Connection,
    title: str,
    body: str,
    category: str = "TECHNICAL",
) -> dict[str, Any]:
    """Create a workspace-level pinned decision. category is free-text
    (STRATEGIC, TECHNICAL, PRODUCT, ...)."""
    did = _new_id()
    await db.execute(
        "INSERT INTO workspace_decisions (id, title, body, category) "
        "VALUES (?, ?, ?, ?)",
        (did, title, body, category),
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
) -> list[dict[str, Any]]:
    """Return workspace decisions, newest first. Active only by default."""
    if include_superseded:
        sql = "SELECT * FROM workspace_decisions ORDER BY created_at DESC"
    else:
        sql = (
            "SELECT * FROM workspace_decisions WHERE status = 'active' "
            "ORDER BY created_at DESC"
        )
    async with db.execute(sql) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def delete_workspace_decision(
    db: aiosqlite.Connection, decision_id: str
) -> bool:
    """Hard-delete a workspace decision. Returns True if a row was removed."""
    async with db.execute(
        "DELETE FROM workspace_decisions WHERE id = ?", (decision_id,)
    ) as cur:
        rc = cur.rowcount or 0
    await db.commit()
    return rc > 0


_WORKSPACE_SETTINGS_ID = "singleton"


async def get_workspace_settings(db: aiosqlite.Connection) -> dict[str, Any]:
    """Return the workspace-level settings (tenant-global defaults).

    Always returns a dict — defaults when no row has been written yet — so
    callers never have to None-check. One singleton row per workspace DB.
    """
    async with db.execute(
        "SELECT hitl_auto_answer_default, sprint_name_default, updated_at "
        "FROM workspace_settings WHERE id = ?",
        (_WORKSPACE_SETTINGS_ID,),
    ) as cur:
        row = await cur.fetchone()
    data = _row_to_dict(row) or {}
    return {
        "hitl_auto_answer_default": bool(data.get("hitl_auto_answer_default")),
        "sprint_name_default": data.get("sprint_name_default"),
        "updated_at": data.get("updated_at"),
    }


async def update_workspace_settings(
    db: aiosqlite.Connection,
    *,
    hitl_auto_answer_default: bool | None = None,
    sprint_name_default: str | None = None,
) -> dict[str, Any]:
    """Upsert the workspace settings singleton and return the new values.

    Only the fields passed (non-None) are changed. ``sprint_name_default=""``
    explicitly clears the label.
    """
    # Ensure the singleton row exists before updating individual fields.
    await db.execute(
        "INSERT INTO workspace_settings (id) VALUES (?) "
        "ON CONFLICT(id) DO NOTHING",
        (_WORKSPACE_SETTINGS_ID,),
    )
    updates: list[str] = []
    params: list[Any] = []
    if hitl_auto_answer_default is not None:
        updates.append("hitl_auto_answer_default = ?")
        params.append(1 if hitl_auto_answer_default else 0)
    if sprint_name_default is not None:
        updates.append("sprint_name_default = ?")
        params.append(sprint_name_default or None)
    if updates:
        from datetime import datetime, timezone
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        updates.append("updated_at = ?")
        params.append(now_ts)
        params.append(_WORKSPACE_SETTINGS_ID)
        await db.execute(
            f"UPDATE workspace_settings SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
    await db.commit()
    return await get_workspace_settings(db)


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
) -> dict[str, Any]:
    """Insert a pending workspace member row (joined_at=NULL).

    G5.20 — ``github_access`` caps repo-touching MCP tools for this invitee.
    Defaults from role when omitted (viewer→none, member→read, admin/owner→write).
    """
    from .. import roles as _roles  # noqa: PLC0415
    mid = _new_id()
    if github_access is None:
        github_access = _roles.default_github_access_for_role(role)
    await db.execute(
        "INSERT INTO workspace_members "
        "(id, tenant_id, email, role, github_access, token_hash) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (mid, tenant_id, email, role, github_access, token_hash),
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
) -> dict[str, Any]:
    """Collect all data belonging to a tenant as a plain dict for GDPR export."""
    from datetime import datetime, timezone

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
        "SELECT id, email, role, invited_at, joined_at FROM workspace_members WHERE tenant_id = ?",
        (tenant_id,),
    ) as cur:
        members = [_row_to_dict(r) for r in await cur.fetchall()]

    async with db.execute(
        "SELECT id, name, creator_human_id, created_at FROM projects ORDER BY created_at DESC",
    ) as cur:
        project_rows = await cur.fetchall()

    projects = []
    for pr in project_rows:
        p = _row_to_dict(pr)
        pid = p["id"]

        async with db.execute(
            "SELECT content, goal_north_star, goal_sprint, version, updated_at FROM goal_states WHERE project_id = ?",
            (pid,),
        ) as cur:
            goals = [_row_to_dict(r) for r in await cur.fetchall()]

        async with db.execute(
            "SELECT id, name, human_id, status, created_at FROM sessions WHERE project_id = ? ORDER BY created_at DESC LIMIT 100",
            (pid,),
        ) as cur:
            sessions = [_row_to_dict(r) for r in await cur.fetchall()]

        async with db.execute(
            "SELECT id, session_id, description, status, created_at FROM task_log WHERE project_id = ? ORDER BY created_at DESC LIMIT 200",
            (pid,),
        ) as cur:
            tasks = [_row_to_dict(r) for r in await cur.fetchall()]

        async with db.execute(
            "SELECT id, version, title, status, item_group FROM sprint_items WHERE project_id = ?",
            (pid,),
        ) as cur:
            sprint = [_row_to_dict(r) for r in await cur.fetchall()]

        try:
            async with db.execute(
                "SELECT id, title, body, tags, created_at FROM project_notes WHERE project_id = ?",
                (pid,),
            ) as cur:
                notes = [_row_to_dict(r) for r in await cur.fetchall()]
        except Exception:
            notes = []

        p["goal_states"] = goals
        p["sessions"] = sessions
        p["tasks"] = tasks
        p["sprint_items"] = sprint
        p["notes"] = notes
        projects.append(p)

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "tenant": tenant_data,
        "api_tokens": tokens,
        "workspace_members": members,
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
) -> dict[str, Any]:
    """Add an ephemeral note scoped to a session. Auto-deleted on session close."""
    nid = _new_id()
    await db.execute(
        "INSERT INTO session_notes (id, session_id, title, body) VALUES (?, ?, ?, ?)",
        (nid, session_id, title, body),
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
) -> list[dict[str, Any]]:
    """Return all notes for a session, newest first."""
    async with db.execute(
        "SELECT * FROM session_notes WHERE session_id = ? ORDER BY created_at DESC",
        (session_id,),
    ) as cur:
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
