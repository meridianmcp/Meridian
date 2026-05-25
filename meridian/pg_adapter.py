"""Postgres adapter providing an aiosqlite-compatible API for Meridian's db layer.

Uses psycopg3 (pure-Python async driver) by default — no compiled extensions,
works on all platforms including Windows without DLL issues.

Falls back to asyncpg if psycopg is not installed (legacy behaviour).

SQL translation rules:
  ?       → %s  (psycopg3 positional placeholder)
  datetime('now')  → to_char(now() at time zone 'utc', ...)
  datetime('now', X || ' minutes') → same with interval cast
  PRAGMA ...       → no-op
  rowid            → removed from ORDER BY (UUID PKs don't need it)
  sqlite_master    → fake result that passes all migration guards
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any


def _same_pg_host(url_a: str, url_b: str) -> bool:
    """Return True when two Postgres URLs point to the same host+port."""
    try:
        host_a = url_a.split("@", 1)[1].split("/")[0]
        host_b = url_b.split("@", 1)[1].split("/")[0]
        return host_a == host_b
    except IndexError:
        return url_a == url_b


# ---------------------------------------------------------------------------
# SQL translation  (? → %s for psycopg3)
# ---------------------------------------------------------------------------

_DATETIME_NOW_EXPR = "to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')"


def _pg_adapt_sql(sql: str, params: tuple) -> tuple[str, list]:
    """Convert SQLite-flavoured SQL + params to Postgres-compatible form.

    Returns ``(pg_sql, pg_params_list)``.  Uses %s placeholders (psycopg3).
    """
    # 1. Escape literal % used in LIKE patterns BEFORE replacing ? → %s
    #    so LIKE '%foo%' becomes LIKE '%%foo%%' (psycopg3 treats % as placeholder)
    sql = re.sub(r"'([^']*%[^']*)'", lambda m: "'" + m.group(1).replace("%", "%%") + "'", sql)

    # 2. ? → %s positional placeholders
    sql = re.sub(r"\?", "%s", sql)

    # 2. datetime('now', %s || ' minutes') — expire_idle_sessions pattern
    sql = re.sub(
        r"datetime\('now',\s*(%s)\s*\|\|\s*' minutes'\)",
        lambda m: (
            f"to_char(now() at time zone 'utc' + ({m.group(1)} || ' minutes')::interval,"
            f" 'YYYY-MM-DD HH24:MI:SS')"
        ),
        sql,
    )

    # 3. datetime('now', %s) — general param form, e.g. '-7 days'
    sql = re.sub(
        r"datetime\('now',\s*(%s)\)",
        lambda m: (
            f"to_char(now() at time zone 'utc' + {m.group(1)}::interval,"
            f" 'YYYY-MM-DD HH24:MI:SS')"
        ),
        sql,
    )

    # 4. datetime('now') bare
    sql = re.sub(r"datetime\('now'\)", _DATETIME_NOW_EXPR, sql)

    # 5. Remove secondary sort on rowid
    sql = re.sub(r",\s*(?:\w+\.)?rowid\s+(?:ASC|DESC)", "", sql, flags=re.IGNORECASE)

    return sql, list(params)


def _parse_rowcount(status: str) -> int:
    """Parse psycopg3 command-completion tag like 'UPDATE 3' → 3."""
    try:
        return int(str(status).strip().split()[-1])
    except (IndexError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Fake cursor wrapping asyncpg results
# ---------------------------------------------------------------------------


class _PgCursor:
    """Fake aiosqlite.Cursor over psycopg3 results.

    Supports ``fetchone()`` / ``fetchall()`` and exposes ``rowcount``.
    Rows are plain dicts keyed by column name.
    """

    __slots__ = ("_rows", "rowcount")

    def __init__(self, rows: list, rowcount: int = 0) -> None:
        self._rows = rows
        self.rowcount = rowcount

    @staticmethod
    def _to_dict(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        if isinstance(row, dict):
            return row
        if hasattr(row, "keys"):
            return dict(row)
        return row

    async def fetchone(self) -> dict[str, Any] | None:
        return self._to_dict(self._rows[0]) if self._rows else None

    async def fetchall(self) -> list[dict[str, Any]]:
        return [self._to_dict(r) for r in self._rows]  # type: ignore[misc]

    def keys(self) -> list[str]:
        if self._rows and hasattr(self._rows[0], "keys"):
            return list(self._rows[0].keys())
        return []


# ---------------------------------------------------------------------------
# psycopg3 row factory — returns plain dicts keyed by column name
# ---------------------------------------------------------------------------

def _dict_row_factory(cursor: Any) -> Any:
    """psycopg3 row_factory that returns dicts."""
    cols = [d.name for d in (cursor.description or [])]

    def make_row(values: tuple) -> dict:
        return dict(zip(cols, values))

    return make_row


# ---------------------------------------------------------------------------
# Dual-mode proxy (awaitable + async context manager)
# ---------------------------------------------------------------------------


class _ExecProxy:
    """Proxy returned by PostgresConnection.execute().

    Can be awaited (``cursor = await db.execute(sql)``) *or* used as an
    async context manager (``async with db.execute(sql) as cur:``).
    Mimics the aiosqlite cursor proxy so db.py callers are unchanged.
    """

    __slots__ = ("_coro", "_cursor")

    def __init__(self, coro) -> None:
        self._coro = coro
        self._cursor: _PgCursor | None = None

    async def _resolve(self) -> _PgCursor:
        if self._cursor is None:
            self._cursor = await self._coro
        return self._cursor

    # ---- Awaitable interface -----------------------------------------------

    def __await__(self):  # type: ignore[override]
        return self._resolve().__await__()

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount if self._cursor else 0

    # ---- Cursor methods (forwarded to the inner _PgCursor) -----------------

    async def fetchone(self) -> dict[str, Any] | None:
        c = await self._resolve()
        return await c.fetchone()

    async def fetchall(self) -> list[dict[str, Any]]:
        c = await self._resolve()
        return await c.fetchall()

    # ---- Async context manager interface -----------------------------------

    async def __aenter__(self) -> "_ExecProxy":
        await self._resolve()
        return self

    async def __aexit__(self, *_args: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# PostgresConnection — the public class
# ---------------------------------------------------------------------------

_PG_TABLE_INFO_QUERY = ""  # unused — kept for import compat


class PostgresConnection:
    """psycopg3 pool wrapper providing an aiosqlite-compatible interface.

    Used as a drop-in for ``aiosqlite.Connection`` throughout db.py.
    Migration functions (``_migrate_*``) are all no-ops for Postgres because
    the full schema is created once on startup.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self.row_factory = None

    # ------------------------------------------------------------------ API

    def execute(self, sql: str, params: tuple = ()) -> _ExecProxy:
        return _ExecProxy(self._do_execute(sql, params))

    async def executescript(self, sql: str) -> None:
        """Run multiple semicolon-separated statements (skipping PRAGMAs)."""
        stmts: list[str] = []
        for raw in sql.split(";"):
            s = raw.strip()
            if not s:
                continue
            if s.upper().startswith("PRAGMA"):
                continue
            if "sqlite_master" in s.lower():
                continue
            pg_s, _ = _pg_adapt_sql(s, ())
            stmts.append(pg_s)

        if not stmts:
            return

        async with self._pool.connection() as conn:
            async with conn.transaction():
                for stmt in stmts:
                    await conn.execute(stmt)

    async def commit(self) -> None:
        """No-op: psycopg3 autocommit handles this."""

    async def close(self) -> None:
        await self._pool.close()

    # ---------------------------------------------------------------- Internals

    async def _do_execute(self, sql: str, params: tuple = ()) -> _PgCursor:
        stripped = sql.strip()
        upper = stripped.upper()

        # ---- SQLite-specific no-ops ----------------------------------------

        if upper.startswith("PRAGMA"):
            m = re.match(r"PRAGMA\s+table_info\((\w+)\)", stripped, re.IGNORECASE)
            if m:
                return await self._table_info(m.group(1))
            return _PgCursor([], 0)

        if "sqlite_master" in stripped.lower():
            return _PgCursor([{"sql": "pending-hitl", "name": "task_log"}], 1)

        # ---- Normal statement ----------------------------------------------

        pg_sql, pg_params = _pg_adapt_sql(sql, params)
        q_upper = pg_sql.lstrip().upper()

        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=_dict_row_factory) as cur:
                await cur.execute(pg_sql, pg_params if pg_params else None)
                if q_upper.startswith(("SELECT", "WITH")):
                    rows = await cur.fetchall()
                    return _PgCursor(rows, len(rows))
                else:
                    rc = cur.rowcount if cur.rowcount is not None else 0
                    return _PgCursor([], rc)

    async def _table_info(self, table_name: str) -> _PgCursor:
        pg_query = """
            SELECT ordinal_position - 1 AS cid,
                   column_name          AS name,
                   data_type            AS type,
                   0                    AS notnull,
                   column_default       AS dflt_value,
                   0                    AS pk
            FROM information_schema.columns
            WHERE table_name = %s
            ORDER BY ordinal_position
        """
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(pg_query, (table_name,))
                rows = await cur.fetchall()
                desc = cur.description or []
                col_names = [d.name for d in desc]
        fake_rows = [
            tuple(row[col_names.index(c)] if c in col_names else None
                  for c in ("cid", "name", "type", "notnull", "dflt_value", "pk"))
            for row in rows
        ]
        return _PgCursor(fake_rows, len(fake_rows))


# ---------------------------------------------------------------------------
# Postgres-compatible CREATE TABLE DDL
# ---------------------------------------------------------------------------

_TS = "to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')"

# Tables that go in every Postgres DB — customer DBs and the main auth DB.
CREATE_TABLES_CORE = f"""
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    creator_human_id TEXT,
    goal_mode TEXT NOT NULL DEFAULT 'manual',
    decisions TEXT,
    rewind_token TEXT,
    created_at TEXT NOT NULL DEFAULT ({_TS})
);

CREATE TABLE IF NOT EXISTS goal_states (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    content TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    goal_north_star TEXT,
    goal_sprint TEXT,
    created_at TEXT NOT NULL DEFAULT ({_TS}),
    updated_at TEXT NOT NULL DEFAULT ({_TS})
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    human_id TEXT,
    session_type TEXT DEFAULT 'human',
    status TEXT NOT NULL DEFAULT 'active',
    last_seen TEXT NOT NULL DEFAULT ({_TS}),
    created_at TEXT NOT NULL DEFAULT ({_TS}),
    session_summary TEXT
);

CREATE TABLE IF NOT EXISTS task_log (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'done',
    claimed_by TEXT,
    claimed_at TEXT,
    parent_session_id TEXT,
    worker_pid INTEGER,
    created_at TEXT NOT NULL DEFAULT ({_TS})
);

CREATE TABLE IF NOT EXISTS sprint_items (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    version TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    item_group TEXT,
    pushed_to TEXT,
    human_id TEXT,
    added_at TEXT NOT NULL DEFAULT ({_TS}),
    completed_at TEXT,
    task_id TEXT,
    notes TEXT
);

-- v2.4 — decisions_pinned: editable constitution. See db.py for rationale.
CREATE TABLE IF NOT EXISTS decisions_pinned (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'TECHNICAL',
    status TEXT NOT NULL DEFAULT 'active',
    superseded_by TEXT REFERENCES decisions_pinned(id),
    created_at TEXT NOT NULL DEFAULT ({_TS}),
    updated_at TEXT NOT NULL DEFAULT ({_TS})
);

-- v2.4 — hitl_requests: human-in-the-loop coordination queue.
CREATE TABLE IF NOT EXISTS hitl_requests (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id TEXT REFERENCES sessions(id),
    question TEXT NOT NULL,
    context TEXT,
    urgency TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL DEFAULT 'pending',
    answer TEXT,
    answered_by TEXT,
    assigned_to TEXT,
    created_at TEXT NOT NULL DEFAULT ({_TS}),
    answered_at TEXT
);

-- v0.9 — project_notes: per-project wiki.
CREATE TABLE IF NOT EXISTS project_notes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tags TEXT,
    created_at TEXT NOT NULL DEFAULT ({_TS}),
    updated_at TEXT NOT NULL DEFAULT ({_TS})
);

CREATE INDEX IF NOT EXISTS idx_goal_project ON goal_states(project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON task_log(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON task_log(session_id);
CREATE INDEX IF NOT EXISTS idx_sprint_items_project ON sprint_items(project_id, status);
CREATE INDEX IF NOT EXISTS idx_sprint_items_version ON sprint_items(project_id, version);
CREATE INDEX IF NOT EXISTS idx_decisions_pinned_project ON decisions_pinned(project_id, status);
CREATE INDEX IF NOT EXISTS idx_hitl_project ON hitl_requests(project_id, status);
CREATE INDEX IF NOT EXISTS idx_hitl_assigned ON hitl_requests(assigned_to, status);
CREATE INDEX IF NOT EXISTS idx_notes_project ON project_notes(project_id);
"""

# Tables that go ONLY in the main auth DB (MERIDIAN_DB_URL).
# Customer DBs provisioned by Neon get only CREATE_TABLES_CORE.
CREATE_TABLES_HOSTED = f"""
-- v1.9.x — waitlist: pre-launch email capture for hosted tier.
CREATE TABLE IF NOT EXISTS waitlist (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT ({_TS})
);

-- v2.0 — hosted tier: tenants, web sessions, API bearer tokens
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
    plan TEXT NOT NULL DEFAULT 'standard',
    pool_project_id TEXT,
    created_at TEXT NOT NULL DEFAULT ({_TS})
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT ({_TS})
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT,
    created_at TEXT NOT NULL DEFAULT ({_TS})
);

-- v2.1 dark — multi-user roles, not exposed in UI or API at launch
CREATE TABLE IF NOT EXISTS workspace_members (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member'
        CHECK (role IN ('owner','member','viewer')),
    token_hash TEXT,
    invited_at TEXT NOT NULL DEFAULT ({_TS}),
    joined_at TEXT
);

-- v2.1 dark — per-tenant named environments, not exposed at launch
CREATE TABLE IF NOT EXISTS tenant_environments (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    neon_db_name TEXT,
    token_hash TEXT,
    is_default SMALLINT NOT NULL DEFAULT 0
        CHECK (is_default IN (0,1))
);

-- v2.2 — Neon pool project registry.
CREATE TABLE IF NOT EXISTS neon_pool_projects (
    id TEXT PRIMARY KEY,
    neon_project_id TEXT NOT NULL UNIQUE,
    tier TEXT NOT NULL DEFAULT 'standard',
    customer_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT ({_TS})
);

-- v0.9 — magic_link_tokens: email magic-link auth flow.
CREATE TABLE IF NOT EXISTS magic_link_tokens (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    used_at TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT ({_TS})
);
CREATE INDEX IF NOT EXISTS idx_magic_email ON magic_link_tokens(email, used_at);
"""

# Backward-compat alias — the test suite imports this name directly.
CREATE_TABLES_PG = CREATE_TABLES_CORE + CREATE_TABLES_HOSTED


async def init_pg_db(url: str) -> PostgresConnection:
    """Open a psycopg3 connection pool, run schema DDL, return the wrapper.

    Called by ``db.init_db()`` when the path starts with ``postgres://`` or
    ``postgresql://``.  Pure-Python psycopg3 — no compiled extensions,
    works on all platforms including Windows without DLL issues.
    """
    try:
        import psycopg  # noqa: F401  — validate install
        from psycopg_pool import AsyncConnectionPool  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "psycopg[binary] and psycopg-pool are required for Postgres support. "
            "Install: pip install 'psycopg[binary]' psycopg-pool"
        ) from exc

    # Strip channel_binding param — not supported by psycopg3
    clean_url = re.sub(r"[?&]channel_binding=[^&]*", "", url)
    clean_url = re.sub(r"\?&", "?", clean_url).rstrip("?")

    # open=False → pool created without connecting. Connections are made lazily
    # on first acquire(), which happens inside the running Uvicorn event loop
    # (after lifespan yield). This avoids ProactorEventLoop issues on Windows
    # where psycopg3 can't use the loop during startup.
    pool = AsyncConnectionPool(
        clean_url,
        min_size=1,
        max_size=10,
        open=False,
        reconnect_timeout=30.0,
        kwargs={
            "autocommit": True,
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 5,
        },
    )
    # Open the pool — now safe because SelectorEventLoop is active
    await pool.open(wait=True, timeout=30.0)

    conn = PostgresConnection(pool)
    await conn.executescript(CREATE_TABLES_CORE)
    # Only create hosted tables (tenants, sessions, billing, etc.) on the
    # main auth DB. Customer DBs provisioned by Neon get CORE tables only.
    main_db_url = os.environ.get("MERIDIAN_DB_URL", "")
    is_main_db = not main_db_url or _same_pg_host(url, main_db_url)
    if is_main_db:
        await conn.executescript(CREATE_TABLES_HOSTED)
    await _migrate_pg_sprint_items_v2(conn)
    await _migrate_pg_drop_chat_tables(conn)
    await _migrate_pg_goal_field_timestamps(conn)
    await _migrate_pg_v24_task_tree_and_framework(conn)
    await _migrate_pg_v24_pinned_decisions_and_hitl(conn)
    await _migrate_pg_v09_notes_and_magic_links(conn)
    if is_main_db:
        await _migrate_pg_v10_tenant_columns(conn)
    return conn


async def _migrate_pg_v09_notes_and_magic_links(conn: PostgresConnection) -> None:
    """v0.9 — project_notes + magic_link_tokens on existing Postgres DBs.
    CREATE_TABLES_PG covers fresh DBs; this is the upgrade path."""
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS project_notes ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,"
        "    title TEXT NOT NULL,"
        "    body TEXT NOT NULL,"
        "    tags TEXT,"
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')),"
        "    updated_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'))"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_notes_project ON project_notes(project_id);"
        "CREATE TABLE IF NOT EXISTS magic_link_tokens ("
        "    id TEXT PRIMARY KEY,"
        "    email TEXT NOT NULL,"
        "    token_hash TEXT NOT NULL UNIQUE,"
        "    used_at TEXT,"
        "    expires_at TEXT NOT NULL,"
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'))"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_magic_email ON magic_link_tokens(email, used_at)"
    )


async def _migrate_pg_v24_task_tree_and_framework(conn: PostgresConnection) -> None:
    """v2.4 — task_log.parent_task_id + sessions.agent_framework + projects.project_token."""
    await conn.executescript(
        "ALTER TABLE task_log ADD COLUMN IF NOT EXISTS parent_task_id TEXT;"
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS agent_framework TEXT DEFAULT 'claude_code';"
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS project_token TEXT"
    )


async def _migrate_pg_v24_pinned_decisions_and_hitl(conn: PostgresConnection) -> None:
    """v2.4 — decisions_pinned + hitl_requests on existing Postgres DBs.
    CREATE_TABLES_PG covers fresh DBs; this is the upgrade path."""
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS decisions_pinned ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,"
        "    title TEXT NOT NULL,"
        "    body TEXT NOT NULL,"
        "    category TEXT NOT NULL DEFAULT 'TECHNICAL',"
        "    status TEXT NOT NULL DEFAULT 'active',"
        "    superseded_by TEXT REFERENCES decisions_pinned(id),"
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')),"
        "    updated_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'))"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_decisions_pinned_project ON decisions_pinned(project_id, status);"
        "CREATE TABLE IF NOT EXISTS hitl_requests ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,"
        "    session_id TEXT REFERENCES sessions(id),"
        "    question TEXT NOT NULL,"
        "    context TEXT,"
        "    urgency TEXT NOT NULL DEFAULT 'normal',"
        "    status TEXT NOT NULL DEFAULT 'pending',"
        "    answer TEXT,"
        "    answered_by TEXT,"
        "    assigned_to TEXT,"
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')),"
        "    answered_at TEXT"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_hitl_project ON hitl_requests(project_id, status);"
        "CREATE INDEX IF NOT EXISTS idx_hitl_assigned ON hitl_requests(assigned_to, status)"
    )


async def _migrate_pg_goal_field_timestamps(conn: PostgresConnection) -> None:
    """v2.3 — per-field timestamps on goal_states (Postgres side).

    See ``db._migrate_goal_field_timestamps`` for the rationale.
    ``ADD COLUMN IF NOT EXISTS`` is idempotent.
    """
    await conn.executescript(
        "ALTER TABLE goal_states ADD COLUMN IF NOT EXISTS ns_updated_at TEXT;"
        "ALTER TABLE goal_states ADD COLUMN IF NOT EXISTS content_updated_at TEXT;"
        "ALTER TABLE goal_states ADD COLUMN IF NOT EXISTS sprint_updated_at TEXT"
    )


async def _migrate_pg_drop_chat_tables(conn: PostgresConnection) -> None:
    """v1.9.x — drop abandoned chat_sessions and chat_messages tables."""
    await conn.executescript(
        "DROP TABLE IF EXISTS chat_messages CASCADE;"
        "DROP TABLE IF EXISTS chat_sessions CASCADE"
    )


async def _migrate_pg_v10_tenant_columns(conn: PostgresConnection) -> None:
    """v1.0 — add microsoft_sub and stripe_metered_item_id to tenants on existing DBs."""
    await conn.executescript(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS microsoft_sub TEXT UNIQUE;"
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS stripe_metered_item_id TEXT;"
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS pool_project_id TEXT"
    )


async def _migrate_pg_sprint_items_v2(conn: PostgresConnection) -> None:
    """Add item_group/pushed_to/human_id to sprint_items if missing.

    ADD COLUMN IF NOT EXISTS is idempotent — safe to run on every startup.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS item_group TEXT;"
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS pushed_to TEXT;"
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS human_id TEXT"
    )
