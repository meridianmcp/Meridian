"""asyncpg adapter providing an aiosqlite-compatible API for Meridian's db layer.

When MERIDIAN_DB_URL points at a Postgres database, db.init_db() returns a
PostgresConnection instead of an aiosqlite.Connection. All code in db.py
that uses the connection object works unchanged because PostgresConnection
exposes the same async API.

SQL translation rules:
  ?       → $1, $2, ...     (positional placeholder)
  datetime('now')  → to_char(now() at time zone 'utc', ...)
  datetime('now', X || ' minutes') → same with interval cast
  PRAGMA ...       → no-op
  rowid            → removed from ORDER BY (UUID PKs don't need it)
  sqlite_master    → fake result that passes all migration guards
"""

from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# SQL translation
# ---------------------------------------------------------------------------

_DATETIME_NOW_EXPR = "to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')"


def _pg_adapt_sql(sql: str, params: tuple) -> tuple[str, list]:
    """Convert SQLite-flavoured SQL + params to Postgres-compatible form.

    Returns ``(pg_sql, pg_params_list)``. The params list has the same
    length and order as the input tuple; only the SQL is rewritten.
    """
    # 1. ? → $N positional placeholders
    counter = [0]

    def _replace_q(_m: re.Match) -> str:
        counter[0] += 1
        return f"${counter[0]}"

    sql = re.sub(r"\?", _replace_q, sql)

    # 2. datetime('now', $N || ' minutes') — expire_idle_sessions pattern
    #    param is a string like "-30"; SQL appends the unit via ||
    sql = re.sub(
        r"datetime\('now',\s*(\$\d+)\s*\|\|\s*' minutes'\)",
        lambda m: (
            f"to_char(now() at time zone 'utc' + ({m.group(1)} || ' minutes')::interval,"
            f" 'YYYY-MM-DD HH24:MI:SS')"
        ),
        sql,
    )

    # 3. datetime('now', $N) — general param form, e.g. '-7 days'
    sql = re.sub(
        r"datetime\('now',\s*(\$\d+)\)",
        lambda m: (
            f"to_char(now() at time zone 'utc' + {m.group(1)}::interval,"
            f" 'YYYY-MM-DD HH24:MI:SS')"
        ),
        sql,
    )

    # 4. datetime('now') bare — replace with formatted text timestamp
    sql = re.sub(r"datetime\('now'\)", _DATETIME_NOW_EXPR, sql)

    # 5. Remove secondary sort on rowid (UUID PKs don't need tiebreakers)
    #    Handles both bare `rowid` and table-qualified `t.rowid` forms.
    sql = re.sub(r",\s*(?:\w+\.)?rowid\s+(?:ASC|DESC)", "", sql, flags=re.IGNORECASE)

    return sql, list(params)


def _parse_rowcount(status: str) -> int:
    """Parse asyncpg command-completion tag like 'UPDATE 3' → 3."""
    try:
        return int(status.strip().split()[-1])
    except (IndexError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Fake cursor wrapping asyncpg results
# ---------------------------------------------------------------------------


class _PgCursor:
    """Fake aiosqlite.Cursor over asyncpg results.

    Supports ``fetchone()`` / ``fetchall()`` and exposes ``rowcount`` so
    callers that check cursor.rowcount work unchanged.
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
        # asyncpg Record supports .keys()
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

_PG_TABLE_INFO_QUERY = """
    SELECT ordinal_position - 1 AS cid,
           column_name          AS name,
           data_type            AS type,
           0                    AS notnull,
           column_default        AS dflt_value,
           0                    AS pk
    FROM information_schema.columns
    WHERE table_name = $1
    ORDER BY ordinal_position
"""


class PostgresConnection:
    """asyncpg pool wrapper providing an aiosqlite-compatible interface.

    Used as a drop-in for ``aiosqlite.Connection`` throughout db.py.
    Migration functions (``_migrate_*``) are all no-ops for Postgres because
    the full schema is created once on startup; all CHECK constraints and
    columns are present from day one.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        # Ignored — asyncpg Records are already dict-convertible
        self.row_factory = None

    # ------------------------------------------------------------------ API

    def execute(self, sql: str, params: tuple = ()) -> _ExecProxy:
        """Return a dual-mode proxy over a single SQL statement."""
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

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for stmt in stmts:
                    await conn.execute(stmt)

    async def commit(self) -> None:
        """No-op: asyncpg auto-commits each statement by default."""

    async def close(self) -> None:
        """Close the underlying connection pool."""
        await self._pool.close()

    # ---------------------------------------------------------------- Internals

    async def _do_execute(self, sql: str, params: tuple = ()) -> _PgCursor:
        """Execute one statement and return a _PgCursor."""
        stripped = sql.strip()
        upper = stripped.upper()

        # ---- SQLite-specific statements that are no-ops on Postgres --------

        if upper.startswith("PRAGMA"):
            # PRAGMA table_info is used by _column_exists migration guard;
            # return rows in the expected format so the guard sees the column.
            m = re.match(r"PRAGMA\s+table_info\((\w+)\)", stripped, re.IGNORECASE)
            if m:
                return await self._table_info(m.group(1))
            return _PgCursor([], 0)

        if "sqlite_master" in stripped.lower():
            # Migration guards check sqlite_master to see if a table already
            # has a column / constraint. Return a fake row that makes every
            # migration guard believe the schema is fully up to date.
            return _PgCursor([{"sql": "pending-hitl", "name": "task_log"}], 1)

        # ---- Normal statement ----------------------------------------------

        pg_sql, pg_params = _pg_adapt_sql(sql, params)
        q_upper = pg_sql.lstrip().upper()

        async with self._pool.acquire() as conn:
            if q_upper.startswith(("SELECT", "WITH")):
                rows = await conn.fetch(pg_sql, *pg_params)
                return _PgCursor(list(rows), len(rows))
            else:
                status = await conn.execute(pg_sql, *pg_params)
                return _PgCursor([], _parse_rowcount(status))

    async def _table_info(self, table_name: str) -> _PgCursor:
        """Return column info in PRAGMA table_info row format (row[1] = name)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_PG_TABLE_INFO_QUERY, table_name)
        # Convert to list of tuples so row[1] == column_name works in
        # _column_exists: ``any(row[1] == column for row in rows)``
        fake_rows = [
            (
                r["cid"],
                r["name"],
                r["type"],
                r["notnull"],
                r["dflt_value"],
                r["pk"],
            )
            for r in rows
        ]
        return _PgCursor(fake_rows, len(fake_rows))


# ---------------------------------------------------------------------------
# Postgres-compatible CREATE TABLE DDL
# ---------------------------------------------------------------------------

CREATE_TABLES_PG = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    creator_human_id TEXT,
    goal_mode TEXT NOT NULL DEFAULT 'manual',
    decisions TEXT,
    rewind_token TEXT,
    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);

CREATE TABLE IF NOT EXISTS goal_states (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    content TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    goal_north_star TEXT,
    goal_sprint TEXT,
    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')),
    updated_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    human_id TEXT,
    session_type TEXT DEFAULT 'human',
    status TEXT NOT NULL DEFAULT 'active',
    last_seen TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')),
    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')),
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
    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    cli_session_id TEXT,
    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);

CREATE TABLE IF NOT EXISTS sprint_items (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    version TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    added_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')),
    completed_at TEXT,
    task_id TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);

CREATE INDEX IF NOT EXISTS idx_goal_project ON goal_states(project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON task_log(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON task_log(session_id);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_project ON chat_sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_chat_messages_project ON chat_messages(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sprint_items_project ON sprint_items(project_id, status);
CREATE INDEX IF NOT EXISTS idx_sprint_items_version ON sprint_items(project_id, version);
"""


async def init_pg_db(url: str) -> PostgresConnection:
    """Open an asyncpg pool, run CREATE TABLE IF NOT EXISTS, return the wrapper.

    Called by ``db.init_db()`` when the path starts with ``postgres://`` or
    ``postgresql://``. All SQLite migration helpers are skipped — Postgres
    starts fresh with the full current schema every time.
    """
    try:
        import asyncpg  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "asyncpg is required for Postgres support. "
            "Install it: pip install asyncpg"
        ) from exc

    pool = await asyncpg.create_pool(url, min_size=1, max_size=10)
    conn = PostgresConnection(pool)
    await conn.executescript(CREATE_TABLES_PG)
    return conn
