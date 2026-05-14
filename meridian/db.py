"""SQLite persistence layer for Meridian.

All functions are async and operate on an `aiosqlite.Connection`. IDs are
uuid4 strings; timestamps are ISO-format strings produced by SQLite's
`datetime('now')`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import aiosqlite

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

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS goal_states (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    content TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','idle','closed')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_log (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'done'
        CHECK (status IN ('pending','done','failed','pending-hitl')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    cli_session_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_goal_project
    ON goal_states(project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_project
    ON sessions(project_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_project
    ON task_log(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_session
    ON task_log(session_id);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_project
    ON chat_sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_chat_messages_project
    ON chat_messages(project_id, created_at);
"""


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
    table_sql = row[0] or ""
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
                CHECK (status IN ('pending','done','failed','pending-hitl')),
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


async def init_db(db_path: str) -> aiosqlite.Connection:
    """Open the SQLite database, apply schema, and return the connection.

    The caller owns the connection and is responsible for closing it.
    Runs idempotent migrations: a v0.1.x database missing the
    ``pending-hitl`` CHECK value is rebuilt in place.
    """
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await db.executescript(CREATE_TABLES)
    await db.commit()
    await _migrate_task_log_hitl(db)
    return db


async def create_project(db: aiosqlite.Connection, name: str) -> dict[str, Any]:
    """Insert a project and return it as a dict. Raises if the name exists."""
    pid = _new_id()
    await db.execute(
        "INSERT INTO projects (id, name) VALUES (?, ?)",
        (pid, name),
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
    """Look up a project by its unique name."""
    async with db.execute(
        "SELECT * FROM projects WHERE name = ?", (name,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def list_projects(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return every project, newest first."""
    async with db.execute(
        "SELECT * FROM projects ORDER BY created_at DESC"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


async def get_goal(
    db: aiosqlite.Connection, project_id: str
) -> dict[str, Any] | None:
    """Return the latest goal state for a project, or None if unset."""
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
    return goal


async def set_goal(
    db: aiosqlite.Connection, project_id: str, content: Any
) -> dict[str, Any]:
    """Upsert the goal state for a project, incrementing version each call."""
    existing = await get_goal(db, project_id)
    encoded = _encode_content(content)
    if existing is None:
        gid = _new_id()
        await db.execute(
            "INSERT INTO goal_states (id, project_id, content, version) "
            "VALUES (?, ?, ?, 1)",
            (gid, project_id, encoded),
        )
    else:
        new_version = int(existing["version"]) + 1
        gid = _new_id()
        await db.execute(
            "INSERT INTO goal_states (id, project_id, content, version) "
            "VALUES (?, ?, ?, ?)",
            (gid, project_id, encoded, new_version),
        )
    await db.commit()
    goal = await get_goal(db, project_id)
    assert goal is not None
    return goal


async def register_session(
    db: aiosqlite.Connection, project_id: str, name: str
) -> dict[str, Any]:
    """Create a session row in 'active' state."""
    sid = _new_id()
    await db.execute(
        "INSERT INTO sessions (id, project_id, name) VALUES (?, ?, ?)",
        (sid, project_id, name),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM sessions WHERE id = ?", (sid,)
    ) as cur:
        row = await cur.fetchone()
    session = _row_to_dict(row)
    assert session is not None
    return session


async def update_session_seen(
    db: aiosqlite.Connection, session_id: str
) -> None:
    """Bump a session's last_seen timestamp to now."""
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now') WHERE id = ?",
        (session_id,),
    )
    await db.commit()


async def close_session(db: aiosqlite.Connection, session_id: str) -> None:
    """Mark a session as closed."""
    await db.execute(
        "UPDATE sessions SET status = 'closed' WHERE id = ?",
        (session_id,),
    )
    await db.commit()


async def get_sessions(
    db: aiosqlite.Connection,
    project_id: str,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    """List sessions for a project, newest first."""
    if active_only:
        query = (
            "SELECT * FROM sessions WHERE project_id = ? "
            "AND status != 'closed' ORDER BY last_seen DESC"
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
) -> dict[str, Any]:
    """Append a task-log entry and broadcast to live subscribers."""
    if status not in {"pending", "done", "failed", "pending-hitl"}:
        raise ValueError(f"invalid task status: {status}")
    tid = _new_id()
    await db.execute(
        "INSERT INTO task_log (id, session_id, project_id, description, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (tid, session_id, project_id, description, status),
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
) -> dict[str, Any] | None:
    """Update a task's status and/or description in place.

    Returns the updated task dict, or None if the id doesn't exist. Used by
    the paid-tier ``enqueue_claude_task`` worker to mark a pending task done
    or failed once the subprocess returns.
    """
    fields: list[str] = []
    values: list[Any] = []
    if status is not None:
        if status not in {"pending", "done", "failed", "pending-hitl"}:
            raise ValueError(f"invalid task status: {status}")
        fields.append("status = ?")
        values.append(status)
    if description is not None:
        fields.append("description = ?")
        values.append(description)
    if not fields:
        return await get_task(db, task_id)
    values.append(task_id)
    await db.execute(
        f"UPDATE task_log SET {', '.join(fields)} WHERE id = ?", values
    )
    await db.commit()
    updated = await get_task(db, task_id)
    if updated is not None:
        _publish_task("task_updated", updated)
    return updated


async def get_tasks(
    db: aiosqlite.Connection, project_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Return recent tasks for a project, newest first."""
    async with db.execute(
        "SELECT * FROM task_log WHERE project_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (project_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Chat persistence (v0.3.0)
# ---------------------------------------------------------------------------


async def get_or_create_chat_session(
    db: aiosqlite.Connection, project_id: str
) -> dict[str, Any]:
    """Return the existing chat session for a project, or create one.

    Each project has at most one active chat session row; subsequent calls
    return the same row. The ``cli_session_id`` column is populated later
    by :func:`update_chat_session_cli_id` once the CLI emits its session
    handle.
    """
    async with db.execute(
        "SELECT * FROM chat_sessions WHERE project_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is not None:
        result = _row_to_dict(row)
        assert result is not None
        return result
    sid = _new_id()
    await db.execute(
        "INSERT INTO chat_sessions (id, project_id) VALUES (?, ?)",
        (sid, project_id),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM chat_sessions WHERE id = ?", (sid,)
    ) as cur:
        row = await cur.fetchone()
    result = _row_to_dict(row)
    assert result is not None
    return result


async def update_chat_session_cli_id(
    db: aiosqlite.Connection, project_id: str, cli_session_id: str
) -> None:
    """Store the claude CLI session ID so the next message can ``--resume``."""
    await db.execute(
        "UPDATE chat_sessions SET cli_session_id = ? WHERE project_id = ?",
        (cli_session_id, project_id),
    )
    await db.commit()


async def save_chat_message(
    db: aiosqlite.Connection, project_id: str, role: str, content: str
) -> dict[str, Any]:
    """Persist one chat turn (user or assistant) for a project."""
    if role not in {"user", "assistant"}:
        raise ValueError(f"invalid chat message role: {role!r}")
    mid = _new_id()
    await db.execute(
        "INSERT INTO chat_messages (id, project_id, role, content) "
        "VALUES (?, ?, ?, ?)",
        (mid, project_id, role, content),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM chat_messages WHERE id = ?", (mid,)
    ) as cur:
        row = await cur.fetchone()
    result = _row_to_dict(row)
    assert result is not None
    return result


async def get_chat_history(
    db: aiosqlite.Connection, project_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    """Return chat messages for a project in chronological order (oldest first)."""
    async with db.execute(
        "SELECT * FROM chat_messages WHERE project_id = ? "
        "ORDER BY created_at ASC, rowid ASC LIMIT ?",
        (project_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


async def expire_idle_sessions(
    db: aiosqlite.Connection, max_age_minutes: int = 30
) -> int:
    """Mark sessions idle when their last_seen is older than *max_age_minutes*.

    Returns the number of rows updated. Only 'active' sessions are
    considered — 'idle' and 'closed' sessions are left untouched.
    """
    cursor = await db.execute(
        "UPDATE sessions SET status = 'idle' "
        "WHERE status = 'active' "
        "AND last_seen < datetime('now', ? || ' minutes')",
        (f"-{max_age_minutes}",),
    )
    await db.commit()
    return cursor.rowcount
