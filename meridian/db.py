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
    creator_human_id TEXT,
    goal_mode TEXT NOT NULL DEFAULT 'manual'
        CHECK (goal_mode IN ('manual', 'auto')),
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
    claimed_by TEXT,
    claimed_at TEXT,
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


async def _column_exists(
    db: aiosqlite.Connection, table: str, column: str
) -> bool:
    """Return True if ``column`` already exists on ``table`` in this DB."""
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return any(row[1] == column for row in rows)


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


async def _migrate_task_claims(db: aiosqlite.Connection) -> None:
    """v0.3.3 — add ``claimed_by`` / ``claimed_at`` columns for the
    distributed task lock. Both nullable, so ALTER TABLE is safe."""
    await _migrate_add_column_if_missing(db, "task_log", "claimed_by", "TEXT")
    await _migrate_add_column_if_missing(db, "task_log", "claimed_at", "TEXT")


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


async def init_db(db_path: str) -> aiosqlite.Connection:
    """Open the SQLite database, apply schema, and return the connection.

    The caller owns the connection and is responsible for closing it.
    Runs idempotent migrations: legacy DBs are upgraded in place.
    """
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode = WAL")   # concurrent read+write
    await db.execute("PRAGMA busy_timeout = 5000")  # retry up to 5 s before LOCKED
    await db.execute("PRAGMA foreign_keys = ON")
    await db.executescript(CREATE_TABLES)
    await db.commit()
    await _migrate_task_log_hitl(db)
    await _migrate_human_identity(db)
    await _migrate_task_claims(db)
    await _migrate_goal_mode(db)
    await _migrate_goal_hierarchy(db)
    return db


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


def build_goal_xml(
    goal: dict[str, Any] | None,
    project_name: str,
    recent_tasks: list[dict[str, Any]] | None = None,
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
    out.append('  <recent_tasks cache="false">')
    for t in recent_tasks or []:
        status = escape(str(t.get("status") or ""))
        ts = escape(str(t.get("created_at") or ""))
        desc = escape(str(t.get("description") or ""))
        out.append(
            f'    <task status="{status}" ts="{ts}">{desc}</task>'
        )
    out.append("  </recent_tasks>")
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
) -> dict[str, Any]:
    """Upsert the goal state for a project, incrementing version each call.

    ``north_star`` and ``sprint`` are optional. When omitted, the values
    from the previous goal row are carried forward (backward compat). Pass
    an explicit value to change them. Since v0.5.2.
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
    new_version = 1 if existing is None else int(existing["version"]) + 1
    gid = _new_id()
    await db.execute(
        "INSERT INTO goal_states "
        "(id, project_id, content, version, goal_north_star, goal_sprint) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (gid, project_id, encoded, new_version, final_north_star, final_sprint),
    )
    await db.commit()
    goal = await get_goal(db, project_id)
    assert goal is not None
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
) -> dict[str, Any]:
    """Create a session row in 'active' state.

    ``human_id`` lets a session attach a human owner identifier so the
    dashboard can group ``adam/claude-sonnet-xyz`` sessions together and
    so the goal-ownership rule can match a writer to the project creator.
    """
    sid = _new_id()
    await db.execute(
        "INSERT INTO sessions (id, project_id, name, human_id) "
        "VALUES (?, ?, ?, ?)",
        (sid, project_id, name, human_id),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM sessions WHERE id = ?", (sid,)
    ) as cur:
        row = await cur.fetchone()
    session = _row_to_dict(row)
    assert session is not None
    return session


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
    if row is None or row[0] is None:
        return "manual"
    return row[0]


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
    return await set_goal(db, project_id, new_content)


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
    return row[0]


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
    call ``log_task`` often enough to keep the 30 minute TTL fresh."""
    cursor = await db.execute(
        "UPDATE sessions SET last_seen = datetime('now') "
        "WHERE id = ? AND status != 'closed'",
        (session_id,),
    )
    await db.commit()
    return cursor.rowcount > 0


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
        "UPDATE task_log SET claimed_by = ?, claimed_at = datetime('now') "
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


async def release_task(
    db: aiosqlite.Connection, task_id: str, session_id: str
) -> bool:
    """Release a claim previously taken by ``session_id``.

    Returns True if a claim was released; False if the task wasn't held
    by that session (someone else's claim is left untouched).
    """
    cursor = await db.execute(
        "UPDATE task_log SET claimed_by = NULL, claimed_at = NULL "
        "WHERE id = ? AND claimed_by = ?",
        (task_id, session_id),
    )
    await db.commit()
    if cursor.rowcount == 0:
        return False
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
    affected_project_ids: list[str] = [row[0] for row in rows]

    cursor = await db.execute(
        "UPDATE sessions SET status = 'idle' "
        "WHERE status = 'active' "
        "AND last_seen < datetime('now', ? || ' minutes')",
        (f"-{max_age_minutes}",),
    )
    await db.commit()
    return {"count": cursor.rowcount, "project_ids": affected_project_ids}
