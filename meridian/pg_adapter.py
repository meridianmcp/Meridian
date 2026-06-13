"""Postgres adapter providing an aiosqlite-compatible API for Meridian's db layer.

Uses psycopg3 (pure-Python async driver) by default — no compiled extensions,
works on all platforms including Windows without DLL issues.

Falls back to asyncpg if psycopg is not installed (legacy behaviour).

SQL translation rules:
  ?       → %s  (psycopg3 positional placeholder)
  datetime('now')  → to_char(now() at time zone 'utc', ...)
  datetime('now', X || ' minutes') / 'hours' / 'days' → same with interval cast
  PRAGMA ...       → no-op
  rowid            → removed from ORDER BY (UUID PKs don't need it)
  sqlite_master    → fake result that passes all migration guards
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)


def _is_transient_pg_error(exc: Exception) -> bool:
    """Return True for connection-level errors that are safe to retry once.

    Catches Neon AdminShutdown (idle scale-to-zero) and generic broken-pipe /
    closed-connection errors.  Import is deferred so the module stays importable
    even without psycopg installed (SQLite-only mode).
    """
    msg = str(exc).lower()
    transient_phrases = (
        "adminshutdown",
        "server closed the connection",
        "connection is closed",
        "ssl connection has been closed",
        "broken pipe",
        "connection reset",
        "consuming input failed",
        # A pooled connection's cached prepared plan was invalidated by DDL
        # (e.g. ALTER TABLE ADD COLUMN run by a migration). The retry closes the
        # stale connection so a fresh one re-prepares the statement.
        "cached plan must not change result type",
    )
    if any(p in msg for p in transient_phrases):
        return True
    try:
        import psycopg.errors as _pe  # type: ignore[import]
        if isinstance(exc, (_pe.AdminShutdown, _pe.ConnectionDoesNotExist)):
            return True
    except ImportError:
        pass
    try:
        import psycopg  # type: ignore[import]
        if isinstance(exc, psycopg.OperationalError):
            return True
    except ImportError:
        pass
    return False


def _same_pg_host(url_a: str, url_b: str) -> bool:
    """Return True when two Postgres URLs point to the same host+port."""
    try:
        host_a = url_a.split("@", 1)[1].split("/")[0]
        host_b = url_b.split("@", 1)[1].split("/")[0]
        return host_a == host_b
    except IndexError:
        return url_a == url_b


def _strip_unsupported_pg_query_params(url: str) -> str:
    """Remove query params unsupported by psycopg without corrupting the URL."""
    parsed = urlsplit(url)
    if not parsed.query:
        return url
    filtered = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "channel_binding"
    ]
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(filtered, doseq=True),
            parsed.fragment,
        )
    )


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

    # 2. datetime('now', %s || ' minutes') / 'hours' / 'days' — interval forms
    sql = re.sub(
        r"datetime\('now',\s*(%s)\s*\|\|\s*'([^']+)'\)",
        lambda m: (
            f"to_char(now() at time zone 'utc' + ({m.group(1)} || '{m.group(2)}')::interval,"
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
        """Run multiple semicolon-separated statements (skipping PRAGMAs).

        Strips ``-- ...`` line comments before splitting so a ``;`` inside a
        comment doesn't chop a statement mid-line (regression caught by the
        G5.19 workspace_members comment that contained a literal ``;``).
        """
        decommented = "\n".join(
            line.split("--", 1)[0] for line in sql.splitlines()
        )
        stmts: list[str] = []
        for raw in decommented.split(";"):
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
        # aiosqlite callers pass None to mean "no params"; normalise here.
        if params is None:
            params = ()
        stripped = sql.strip()
        upper = stripped.upper()

        # ---- SQLite-specific no-ops ----------------------------------------

        if upper.startswith("PRAGMA"):
            m = re.match(r"PRAGMA\s+table_info\((\w+)\)", stripped, re.IGNORECASE)
            if m:
                return await self._table_info(m.group(1))
            return _PgCursor([], 0)

        if "sqlite_master" in stripped.lower():
            return _PgCursor([{"sql": "pending-hitl 'backlog' 'backburner'", "name": "task_log"}], 1)

        # ---- Normal statement ----------------------------------------------

        pg_sql, pg_params = _pg_adapt_sql(sql, params)
        q_upper = pg_sql.lstrip().upper()

        return await self._execute_with_retry(pg_sql, pg_params, q_upper)

    async def _execute_with_retry(
        self, pg_sql: str, pg_params: list, q_upper: str, _attempt: int = 0
    ) -> _PgCursor:
        """Execute pg_sql with a single retry on transient connection errors.

        Neon scale-to-zero fires an AdminShutdown on idle connections.  The
        pool's max_idle/max_lifetime settings minimise exposure, but if a
        stale connection slips through we catch OperationalError here and
        retry once with a fresh connection from the pool.

        On AdminShutdown/OperationalError we explicitly close the borrowed
        connection before retrying.  psycopg_pool detects the broken state
        automatically, but closing it eagerly ensures the pool opens a fresh
        connection immediately rather than first trying to reuse the dead one.
        """
        conn = None
        try:
            conn = await self._pool.getconn()
            async with conn.cursor(row_factory=_dict_row_factory) as cur:
                await cur.execute(pg_sql, pg_params if pg_params else None)
                if q_upper.startswith(("SELECT", "WITH")):
                    rows = await cur.fetchall()
                    result = _PgCursor(rows, len(rows))
                else:
                    rc = cur.rowcount if cur.rowcount is not None else 0
                    result = _PgCursor([], rc)
            await self._pool.putconn(conn)
            return result
        except Exception as exc:  # noqa: BLE001
            # On transient errors, close the stale connection explicitly so
            # the pool creates a fresh one on the next acquire rather than
            # handing out the same broken connection again.
            if conn is not None and _is_transient_pg_error(exc):
                try:
                    await conn.close()
                except Exception:  # noqa: BLE001
                    pass
                conn = None
            elif conn is not None:
                try:
                    await self._pool.putconn(conn)
                except Exception:  # noqa: BLE001
                    pass
            # Retry once on connection-level errors (AdminShutdown, broken pipe,
            # closed connection) that indicate a stale pool connection.
            if _attempt == 0 and _is_transient_pg_error(exc):
                return await self._execute_with_retry(pg_sql, pg_params, q_upper, 1)
            raise

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
    ntfy_url TEXT,
    notify_email TEXT,
    max_pinned_decisions INTEGER NOT NULL DEFAULT 20,
    executor_config TEXT,
    rewind_token TEXT,
    hitl_auto_answer INTEGER NOT NULL DEFAULT 0,
    icon TEXT,
    github_repo TEXT,
    github_branch TEXT,
    queued_session TEXT,
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
    client_type TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    last_seen TEXT NOT NULL DEFAULT ({_TS}),
    created_at TEXT NOT NULL DEFAULT ({_TS}),
    session_summary TEXT,
    checkpoint_data TEXT
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
    sprint_item_id TEXT,
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
    notes TEXT,
    feedback_thumb SMALLINT,
    feedback_note TEXT,
    milestone_type TEXT NOT NULL DEFAULT 'task'
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
    answered_at TEXT,
    kind TEXT NOT NULL DEFAULT 'question',
    payload TEXT
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

-- v2.6 — session_notes: ephemeral per-session scratch pad.
CREATE TABLE IF NOT EXISTS session_notes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT ({_TS})
);
CREATE INDEX IF NOT EXISTS idx_session_notes_session ON session_notes(session_id);

-- v3.0 — executor_runs: one row per Claude Code / worker session execution.
CREATE TABLE IF NOT EXISTS executor_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL DEFAULT ({_TS}),
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    transcript TEXT NOT NULL DEFAULT '',
    task_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_executor_runs_session ON executor_runs(session_id);
CREATE INDEX IF NOT EXISTS idx_executor_runs_project ON executor_runs(project_id, started_at DESC);

CREATE TABLE IF NOT EXISTS file_locks (
    id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    claimed_at TEXT NOT NULL DEFAULT ({_TS}),
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_file_locks_session ON file_locks(session_id);
CREATE INDEX IF NOT EXISTS idx_file_locks_expires ON file_locks(expires_at);

-- worktree isolation: live git worktrees registered per session.
CREATE TABLE IF NOT EXISTS active_worktrees (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    item_id TEXT,
    branch TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT ({_TS}),
    removed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_active_worktrees_session ON active_worktrees(session_id);
CREATE INDEX IF NOT EXISTS idx_active_worktrees_project ON active_worktrees(project_id, removed_at);

-- v3.1 — workspace layer: tenant-global notes + decisions above projects.
CREATE TABLE IF NOT EXISTS workspace_notes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tags TEXT,
    tenant_id TEXT,
    created_at TEXT NOT NULL DEFAULT ({_TS})
);
CREATE TABLE IF NOT EXISTS workspace_decisions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'TECHNICAL',
    status TEXT NOT NULL DEFAULT 'active',
    tenant_id TEXT,
    created_at TEXT NOT NULL DEFAULT ({_TS})
);
CREATE INDEX IF NOT EXISTS idx_workspace_notes_created ON workspace_notes(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_workspace_decisions_status ON workspace_decisions(status, created_at DESC);
-- tenant_id indexes added by _migrate_pg_workspace_tenant_isolation (migration handles existing DBs)

-- v3.4 — workspace-level settings singleton (tenant-global defaults).
CREATE TABLE IF NOT EXISTS workspace_settings (
    id TEXT PRIMARY KEY DEFAULT 'singleton',
    hitl_auto_answer_default INTEGER NOT NULL DEFAULT 0,
    sprint_name_default TEXT,
    display_name TEXT,
    log_task_sprint_nudge_threshold INTEGER NOT NULL DEFAULT 5,
    handoff_template TEXT,
    updated_at TEXT NOT NULL DEFAULT ({_TS})
);
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
-- v2.9 — free tier: trial_started_at + inactivity_expires_at
-- v3.1 — github_pat/repo/branch for GitHub MCP tools
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
    trial_started_at TEXT,
    inactivity_expires_at TEXT,
    github_pat TEXT,
    github_repo TEXT,
    github_branch TEXT,
    notification_prefs TEXT NOT NULL DEFAULT '{{}}',
    is_internal INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    tenant_id TEXT REFERENCES tenants(id),
    client_id TEXT,
    exp BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- v2.1 dark — multi-user roles
-- G5.19/G5.20 — role widened to include 'admin'; github_access caps
-- repo-touching MCP tools. App layer (meridian.roles) is the source of
-- truth for valid values; DB-level CHECK kept on github_access only.
CREATE TABLE IF NOT EXISTS workspace_members (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    github_access TEXT NOT NULL DEFAULT 'read'
        CHECK (github_access IN ('none','read','write')),
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

-- v2.5 — admins: DB-managed admin email list. Replaces MERIDIAN_ADMIN_EMAILS env var.
CREATE TABLE IF NOT EXISTS admins (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    added_by TEXT,
    added_at TEXT NOT NULL DEFAULT ({_TS}),
    notes TEXT
);
"""

# Backward-compat alias — the test suite imports this name directly.
CREATE_TABLES_PG = CREATE_TABLES_CORE + CREATE_TABLES_HOSTED


async def open_pg_connection(url: str) -> PostgresConnection:
    """Open a psycopg3 connection pool with Neon scale-to-zero resilience.

    Used by _deps._open_tenant_db_by_id for per-tenant Postgres DBs and by
    standalone scripts (set_tenant_db.py, etc.) that need direct DB access
    without running the full migration chain.

    Neon resilience settings match init_pg_db — see that function for
    rationale.  min_size=0 is critical: it lets the pool release all
    connections when idle so Neon can hibernate cleanly, rather than keeping
    one connection alive that will receive AdminShutdown after the 5-min
    idle-shutdown boundary.
    """
    try:
        import psycopg  # noqa: F401
        from psycopg_pool import AsyncConnectionPool  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "psycopg[binary] and psycopg-pool are required. "
            "Install: pip install 'psycopg[binary]' psycopg-pool"
        ) from exc

    clean_url = _strip_unsupported_pg_query_params(url)

    pool = AsyncConnectionPool(
        clean_url,
        min_size=0,          # allow all connections to close on idle (Neon hibernation)
        max_size=5,
        open=False,
        max_idle=60.0,       # proactively close before Neon's 5-min idle-shutdown
        max_lifetime=240.0,  # recycle before Neon's 300 s idle-timeout boundary
        reconnect_timeout=30.0,
        kwargs={
            "autocommit": True,
            # Disable server-side prepared statements. A cached plan tied to a
            # table's result shape breaks ("cached plan must not change result
            # type") when a migration alters that table while pooled connections
            # hold the stale plan. We re-parse per execute instead.
            "prepare_threshold": None,
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 5,
        },
    )
    await pool.open(wait=True, timeout=30.0)
    return PostgresConnection(pool)


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
    clean_url = _strip_unsupported_pg_query_params(url)

    # open=False → pool created without connecting. Connections are made lazily
    # on first acquire(), which happens inside the running Uvicorn event loop
    # (after lifespan yield). This avoids ProactorEventLoop issues on Windows
    # where psycopg3 can't use the loop during startup.
    #
    # Neon scale-to-zero resilience:
    #   min_size=0   — allow all connections to close when idle so Neon can
    #                  hibernate without the pool keeping a connection alive.
    #   max_idle=60  — proactively close connections idle >60 s before Neon's
    #                  default 5-min idle-shutdown fires (avoids AdminShutdown
    #                  on the first request after the server has been quiet).
    #   max_lifetime=300 — recycle any connection older than 5 min; Neon may
    #                  silently drop backends at its idle timeout boundary.
    #   reconnect_timeout=30 — if a borrowed connection fails, the pool
    #                  retries establishing a fresh one for up to 30 s.
    pool = AsyncConnectionPool(
        clean_url,
        min_size=0,
        max_size=10,
        open=False,
        max_idle=60.0,
        max_lifetime=240.0,  # recycle before Neon 300s idle timeout
        reconnect_timeout=30.0,
        kwargs={
            "autocommit": True,
            # Disable server-side prepared statements. A cached plan tied to a
            # table's result shape breaks ("cached plan must not change result
            # type") when a migration alters that table while pooled connections
            # hold the stale plan. We re-parse per execute instead.
            "prepare_threshold": None,
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
    # Migrations are run through _run_pg_migrations so that a single failing
    # migration logs a WARNING but never crashes uvicorn startup. A bad
    # migration (a stray index on a not-yet-created column) once killed all
    # four prod machines for an hour on 2026-06-13 — startup must survive any
    # individual migration error. Every _migrate_pg_* is idempotent
    # (ADD COLUMN IF NOT EXISTS, CREATE TABLE IF NOT EXISTS, etc.), so skipping
    # one this boot and retrying it next boot is safe.
    #
    # Ordering matters and is preserved: core migrations first, then the
    # main-auth-DB-only set, then the late migrations that run on every DB.
    await _run_pg_migrations(conn, _PG_MIGRATIONS_CORE)
    if is_main_db:
        await _run_pg_migrations(conn, _PG_MIGRATIONS_HOSTED)
    await _run_pg_migrations(conn, _PG_MIGRATIONS_LATE)
    return conn


async def _run_pg_migrations(conn: PostgresConnection, migrations: tuple) -> None:
    """Run each migration in isolation; log and continue on failure.

    A migration error must never abort startup — see init_pg_db for the
    outage that motivated this. Each migration is idempotent, so a failure
    here is retried on the next boot.
    """
    for migration in migrations:
        try:
            await migration(conn)
        except Exception as exc:  # noqa: BLE001 — startup must survive any migration error
            logger.warning(
                "pg migration %s failed (continuing startup): %s",
                getattr(migration, "__name__", repr(migration)),
                exc,
            )


async def _migrate_pg_sprint_items_claimed_at(conn: PostgresConnection) -> None:
    """Add claimed_at to sprint_items (Task 8/9) and milestone_type if missing."""
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS claimed_at TEXT;"
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS milestone_type TEXT NOT NULL DEFAULT 'task'"
    )


async def _migrate_pg_sprint_item_tree(conn: PostgresConnection) -> None:
    """Add parent_id, split_from, merged_into, merged_from to sprint_items (Task 10)."""
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS parent_id TEXT DEFAULT NULL;"
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS split_from TEXT DEFAULT NULL;"
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS merged_into TEXT DEFAULT NULL;"
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS merged_from TEXT DEFAULT NULL"
    )


async def _migrate_pg_api_token_type(conn: PostgresConnection) -> None:
    """Add token_type to api_tokens for read-only token support (Task 3)."""
    await conn.executescript(
        "ALTER TABLE api_tokens ADD COLUMN IF NOT EXISTS token_type TEXT NOT NULL DEFAULT 'readwrite'"
    )


async def _migrate_pg_api_token_expires_at(conn: PostgresConnection) -> None:
    """Add expires_at to api_tokens for short-lived install tokens."""
    await conn.executescript(
        "ALTER TABLE api_tokens ADD COLUMN IF NOT EXISTS expires_at TEXT"
    )


async def _migrate_pg_oauth_codes(conn: PostgresConnection) -> None:
    """Add oauth_codes table for PKCE OAuth flow."""
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS oauth_codes ("
        "    code TEXT PRIMARY KEY,"
        "    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,"
        "    redirect_uri TEXT NOT NULL,"
        "    code_challenge TEXT NOT NULL,"
        "    expires_at TEXT NOT NULL,"
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'))"
        ")"
    )


async def _migrate_pg_github_to_projects(conn: PostgresConnection) -> None:
    """Move github_repo + github_branch from tenants to projects (Task 1)."""
    await conn.executescript(
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS github_repo TEXT;"
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS github_branch TEXT"
    )


async def _migrate_pg_queued_session(conn: PostgresConnection) -> None:
    """10e6b265 — projects.queued_session for back-to-back /goal runs."""
    await conn.executescript(
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS queued_session TEXT"
    )


async def _migrate_pg_workspace_members_rbac(conn: PostgresConnection) -> None:
    """G5.19 / G5.20 — drop the legacy role CHECK (which excluded 'admin')
    and add github_access. Idempotent.

    On Postgres 11+ ADD COLUMN with a DEFAULT and DROP CONSTRAINT IF
    EXISTS are both online-safe — no table rewrite, no long lock.
    """
    await conn.executescript(
        "ALTER TABLE workspace_members "
        "ADD COLUMN IF NOT EXISTS github_access TEXT NOT NULL DEFAULT 'read'"
    )
    # Drop the legacy CHECK on role if present. The constraint name is
    # auto-generated; we look it up via pg_constraint.
    try:
        await conn.execute(
            "DO $$ "
            "DECLARE c text; "
            "BEGIN "
            "  SELECT conname INTO c FROM pg_constraint "
            "   WHERE conrelid = 'workspace_members'::regclass "
            "     AND contype = 'c' "
            "     AND pg_get_constraintdef(oid) LIKE '%role%owner%member%viewer%' "
            "   LIMIT 1; "
            "  IF c IS NOT NULL THEN "
            "    EXECUTE 'ALTER TABLE workspace_members DROP CONSTRAINT ' || quote_ident(c); "
            "  END IF; "
            "END $$",
            None,
        )
    except Exception:  # noqa: BLE001 — best-effort; new installs already lack it
        pass
    # Add the github_access CHECK (idempotent — ignore failure if
    # constraint already exists).
    try:
        await conn.execute(
            "ALTER TABLE workspace_members ADD CONSTRAINT "
            "workspace_members_github_access_chk "
            "CHECK (github_access IN ('none','read','write'))",
            None,
        )
    except Exception:  # noqa: BLE001
        pass
    # Ensure invited_at and joined_at exist — they may be absent on
    # instances created before the column was added to the DDL.
    await conn.executescript(
        "ALTER TABLE workspace_members "
        f"ADD COLUMN IF NOT EXISTS invited_at TEXT NOT NULL DEFAULT ({_TS})"
    )
    await conn.executescript(
        "ALTER TABLE workspace_members "
        "ADD COLUMN IF NOT EXISTS joined_at TEXT"
    )


async def _migrate_pg_tenants_is_internal(conn: PostgresConnection) -> None:
    """G2.10 — tenants.is_internal column + backfill known internal emails.
    Postgres mirror of db._migrate_tenants_is_internal. Online-safe ADD
    COLUMN (default 0, no table rewrite on PG 11+). Idempotent.
    """
    from . import db as db_module  # noqa: PLC0415
    await conn.executescript(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS is_internal INTEGER NOT NULL DEFAULT 0"
    )
    # Legacy databases created the column as BOOLEAN. Normalize to INTEGER so
    # the backfill below (and every is_internal = 1 caller) is type-correct on
    # every DB. boolean::integer yields 0/1.
    async with conn.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = 'tenants' AND column_name = 'is_internal'",
        (),
    ) as cur:
        col = await cur.fetchone()
    if col and col["data_type"] == "boolean":
        await conn.execute(
            "ALTER TABLE tenants ALTER COLUMN is_internal DROP DEFAULT", ()
        )
        await conn.execute(
            "ALTER TABLE tenants ALTER COLUMN is_internal TYPE INTEGER "
            "USING (is_internal::integer)",
            (),
        )
        await conn.execute(
            "ALTER TABLE tenants ALTER COLUMN is_internal SET DEFAULT 0", ()
        )
    # Backfill known internal emails.
    for email in sorted(db_module._internal_emails()):
        await conn.execute(
            "UPDATE tenants SET is_internal = 1 WHERE LOWER(email) = ?",
            (email,),
        )


async def _migrate_pg_workspace_tenant_isolation(conn: PostgresConnection) -> None:
    """Add tenant_id to workspace_notes/decisions/settings. Idempotent."""
    for sql in [
        "ALTER TABLE workspace_notes ADD COLUMN IF NOT EXISTS tenant_id TEXT",
        "ALTER TABLE workspace_decisions ADD COLUMN IF NOT EXISTS tenant_id TEXT",
        "ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS tenant_id TEXT",
        "CREATE INDEX IF NOT EXISTS idx_ws_notes_tenant ON workspace_notes(tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_ws_decisions_tenant ON workspace_decisions(tenant_id)",
    ]:
        await conn.execute(sql)


async def _migrate_pg_admin_plan(conn: PostgresConnection) -> None:
    """Set plan='admin' for tenants whose email is in MERIDIAN_ADMIN_EMAILS / ADMIN_EMAIL.

    Makes the plan column authoritative for admin-DB routing in _deps.py.
    Idempotent — safe to run on every startup.
    """
    whitelist_raw = os.environ.get("MERIDIAN_ADMIN_EMAILS", os.environ.get("ADMIN_EMAIL", ""))
    if not whitelist_raw:
        return
    admin_emails = {e.strip().lower() for e in whitelist_raw.split(",") if e.strip()}
    for email in sorted(admin_emails):
        await conn.execute(
            "UPDATE tenants SET plan = 'admin' WHERE LOWER(email) = ? AND plan != 'admin'",
            (email,),
        )


async def get_project_ntfy_url(
    db: PostgresConnection, project_id: str
) -> str | None:
    """Return the ntfy URL for a Postgres-backed project, or None if unset."""
    async with db.execute(
        "SELECT ntfy_url FROM projects WHERE id = ?",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return row["ntfy_url"] or None


async def set_project_ntfy_url(
    db: PostgresConnection, project_id: str, ntfy_url: str | None
) -> None:
    """Persist or clear the ntfy URL on a Postgres-backed project."""
    await db.execute(
        "UPDATE projects SET ntfy_url = ? WHERE id = ?",
        (ntfy_url or None, project_id),
    )
    await db.commit()


async def get_project_notify_email(
    db: PostgresConnection, project_id: str
) -> str | None:
    """Return the notify_email for a Postgres-backed project, or None if unset."""
    async with db.execute(
        "SELECT notify_email FROM projects WHERE id = ?",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return row["notify_email"] or None


async def set_project_notify_email(
    db: PostgresConnection, project_id: str, notify_email: str | None
) -> None:
    """Persist or clear the notify_email on a Postgres-backed project."""
    await db.execute(
        "UPDATE projects SET notify_email = ? WHERE id = ?",
        (notify_email or None, project_id),
    )
    await db.commit()


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


async def _migrate_pg_v32_workspace_and_checkpoint(conn: PostgresConnection) -> None:
    """v3.1 — workspace_notes + workspace_decisions tables and
    sessions.checkpoint_data column on existing Postgres DBs. CREATE_TABLES_CORE
    covers fresh DBs; this is the upgrade path. Runs on every DB (core)."""
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS workspace_notes ("
        "    id TEXT PRIMARY KEY,"
        "    title TEXT NOT NULL,"
        "    body TEXT NOT NULL,"
        "    tags TEXT,"
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'))"
        ");"
        "CREATE TABLE IF NOT EXISTS workspace_decisions ("
        "    id TEXT PRIMARY KEY,"
        "    title TEXT NOT NULL,"
        "    body TEXT NOT NULL,"
        "    category TEXT NOT NULL DEFAULT 'TECHNICAL',"
        "    status TEXT NOT NULL DEFAULT 'active',"
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS'))"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_workspace_notes_created ON workspace_notes(created_at DESC);"
        "CREATE INDEX IF NOT EXISTS idx_workspace_decisions_status ON workspace_decisions(status, created_at DESC);"
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS checkpoint_data TEXT;"
        "DELETE FROM project_notes WHERE title LIKE 'checkpoint:%'"
    )


async def _migrate_pg_v24_task_tree_and_framework(conn: PostgresConnection) -> None:
    """v2.4 — task_log.parent_task_id + sessions.agent_framework + projects.project_token."""
    await conn.executescript(
        "ALTER TABLE task_log ADD COLUMN IF NOT EXISTS parent_task_id TEXT;"
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS agent_framework TEXT DEFAULT 'claude_code';"
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS project_token TEXT"
    )


async def _migrate_pg_project_settings(conn: PostgresConnection) -> None:
    """v2.6.1 — add per-project settings columns and notification target."""
    await conn.executescript(
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS max_pinned_decisions INTEGER NOT NULL DEFAULT 20;"
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS executor_config TEXT;"
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS ntfy_url TEXT"
    )


async def _migrate_pg_notify_email(conn: PostgresConnection) -> None:
    """v2.5.1 — add notify_email column to projects for separate email notifications."""
    await conn.executescript(
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS notify_email TEXT"
    )


async def _migrate_pg_file_locks(conn: PostgresConnection) -> None:
    """v3.1 — create file_locks table on existing Postgres DBs."""
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS file_locks ("
        "    id TEXT PRIMARY KEY,"
        "    file_path TEXT NOT NULL UNIQUE,"
        "    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,"
        f"    claimed_at TEXT NOT NULL DEFAULT ({_TS}),"
        "    expires_at TEXT NOT NULL"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_file_locks_session ON file_locks(session_id);"
        "CREATE INDEX IF NOT EXISTS idx_file_locks_expires ON file_locks(expires_at)"
    )


async def _migrate_pg_active_worktrees(conn: PostgresConnection) -> None:
    """Create active_worktrees on existing Postgres DBs. It was missing from the
    PG schema entirely (only in the SQLite path), so GET /worktrees 500'd on
    hosted with 'relation active_worktrees does not exist'."""
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS active_worktrees ("
        "    id TEXT PRIMARY KEY,"
        "    session_id TEXT NOT NULL REFERENCES sessions(id),"
        "    project_id TEXT NOT NULL REFERENCES projects(id),"
        "    item_id TEXT,"
        "    branch TEXT NOT NULL,"
        "    path TEXT NOT NULL,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS}),"
        "    removed_at TEXT"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_active_worktrees_session ON active_worktrees(session_id);"
        "CREATE INDEX IF NOT EXISTS idx_active_worktrees_project ON active_worktrees(project_id, removed_at)"
    )


async def _migrate_pg_task_sprint_link(conn: PostgresConnection) -> None:
    """v2.6 â€” link task_log rows back to their sprint item when applicable."""
    await conn.executescript(
        "ALTER TABLE task_log ADD COLUMN IF NOT EXISTS sprint_item_id TEXT"
    )


async def _migrate_pg_v26_client_type(conn: PostgresConnection) -> None:
    """v2.6 — sessions.client_type for client app presence indicators."""
    await conn.executescript(
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS client_type TEXT"
    )


async def _migrate_pg_v27_pg_trgm(conn: PostgresConnection) -> None:
    """v2.7 — enable pg_trgm extension for fast trigram-based task search.
    CREATE EXTENSION is idempotent (IF NOT EXISTS). Adds GIN index on
    task_log.description so similarity() queries stay fast at scale."""
    await conn.executescript(
        "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
        "CREATE INDEX IF NOT EXISTS idx_task_log_desc_trgm "
        "ON task_log USING gin(description gin_trgm_ops)"
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


async def _migrate_pg_v33_hitl_kind_payload(conn: PostgresConnection) -> None:
    """v3.3 — hitl_requests.kind + payload for the markdown section-update flow.

    Must run on ALL DBs (incl. Neon-provisioned customer DBs), not just the main
    auth DB — hitl_requests lives on every project DB. ADD COLUMN IF NOT EXISTS
    is idempotent; mirrors db._migrate_v33_hitl_kind_payload.
    """
    await conn.executescript(
        "ALTER TABLE hitl_requests ADD COLUMN IF NOT EXISTS "
        "kind TEXT NOT NULL DEFAULT 'question';"
        "ALTER TABLE hitl_requests ADD COLUMN IF NOT EXISTS payload TEXT"
    )


async def _migrate_pg_v34_hitl_auto_answer(conn: PostgresConnection) -> None:
    """v3.4 — projects.hitl_auto_answer per-project toggle. Mirrors
    db._migrate_v34_hitl_auto_answer. Idempotent ADD COLUMN IF NOT EXISTS."""
    await conn.executescript(
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS "
        "hitl_auto_answer INTEGER NOT NULL DEFAULT 0"
    )


async def _migrate_pg_v34_workspace_settings(conn: PostgresConnection) -> None:
    """v3.4 — workspace_settings singleton table on existing Postgres DBs.
    Runs on ALL DBs (lives on every workspace DB). Mirrors
    db._migrate_v34_workspace_settings."""
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS workspace_settings ("
        "    id TEXT PRIMARY KEY DEFAULT 'singleton',"
        "    hitl_auto_answer_default INTEGER NOT NULL DEFAULT 0,"
        "    sprint_name_default TEXT,"
        f"    updated_at TEXT NOT NULL DEFAULT ({_TS})"
        ")"
    )


async def _migrate_pg_workspace_settings_columns(conn: PostgresConnection) -> None:
    """Add display_name and log_task_sprint_nudge_threshold to existing workspace_settings rows."""
    await conn.executescript(
        "ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS display_name TEXT;"
        "ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS "
        "log_task_sprint_nudge_threshold INTEGER NOT NULL DEFAULT 5;"
        "ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS handoff_template TEXT"
    )


async def _migrate_pg_project_icon(conn: PostgresConnection) -> None:
    """G4.17 — projects.icon (single-emoji column for sidebar/tab rendering)."""
    await conn.executescript(
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS icon TEXT"
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


async def _migrate_pg_v25_sprint_feedback(conn: PostgresConnection) -> None:
    """v2.5 — sprint_items thumbs-up/down feedback columns.

    Mirrors db._migrate_v25_feedback_and_notifications for Postgres. Neither
    CREATE_TABLES_PG nor any prior migration added these, so existing AND fresh
    Postgres DBs both need them. ADD COLUMN IF NOT EXISTS is idempotent.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS feedback_thumb SMALLINT;"
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS feedback_note TEXT"
    )


async def _migrate_pg_v28_dunning_and_github_sub(conn: PostgresConnection) -> None:
    """v2.8 — dunning fields + github_sub + overage tracking on tenants.

    These columns exist in CREATE_TABLES_HOSTED for new DBs but were added
    after initial prod deployment, so existing DBs need this migration.
    ADD COLUMN IF NOT EXISTS is idempotent — safe to run on every startup.
    """
    await conn.executescript(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS github_sub TEXT UNIQUE;"
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS payment_failed_at TEXT;"
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS dunning_email_sent INTEGER NOT NULL DEFAULT 0;"
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS compute_overage_cap_usd NUMERIC(8,2) DEFAULT 0;"
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS storage_overage_cap_usd NUMERIC(8,2) DEFAULT 0;"
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS compute_cu_hours_used NUMERIC(10,4) DEFAULT 0;"
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS storage_gb_used NUMERIC(10,4) DEFAULT 0;"
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS overage_reset_at TEXT;"
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS compute_throttled_at TEXT"
    )


async def _migrate_pg_v29_free_tier_columns(conn: PostgresConnection) -> None:
    """v2.9 — free tier columns on tenants.

    trial_started_at + inactivity_expires_at support the 30-day free tier.
    ADD COLUMN IF NOT EXISTS is idempotent — safe to run on every startup.
    """
    await conn.executescript(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS trial_started_at TEXT;"
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS inactivity_expires_at TEXT"
    )


async def _migrate_pg_v31_github_integration(conn: PostgresConnection) -> None:
    """v3.1 — per-tenant GitHub PAT and repo for the GitHub MCP tools.

    ADD COLUMN IF NOT EXISTS is idempotent — safe to run on every startup.
    """
    await conn.executescript(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS github_pat TEXT;"
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS github_repo TEXT;"
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS github_branch TEXT"
    )


async def _migrate_pg_v25_notification_prefs(conn: PostgresConnection) -> None:
    """v2.5 — tenants.notification_prefs for email notification settings.

    Mirrors db._migrate_v25_feedback_and_notifications for Postgres. Missing
    from CREATE_TABLES_HOSTED, so PATCH /settings/notifications 500s on every
    hosted DB until this runs. ADD COLUMN IF NOT EXISTS is idempotent.
    """
    await conn.executescript(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS "
        "notification_prefs TEXT NOT NULL DEFAULT '{}'"
    )


async def _migrate_pg_v25_admins_table(conn: PostgresConnection) -> None:
    """v2.5 — create admins table and seed known admin emails.

    Idempotent: CREATE IF NOT EXISTS + INSERT ... ON CONFLICT DO NOTHING.
    """
    import uuid as _uuid
    await conn.executescript(
        f"CREATE TABLE IF NOT EXISTS admins ("
        f"    id TEXT PRIMARY KEY,"
        f"    email TEXT NOT NULL UNIQUE,"
        f"    added_by TEXT,"
        f"    added_at TEXT NOT NULL DEFAULT ({_TS}),"
        f"    notes TEXT"
        f");"
    )
    seed_emails = [
        ("hello@usemeridian.us", "primary owner"),
        ("hello@usemeridian.us", "secondary account"),
        ("[admin-redacted]", "team"),
    ]
    for email, notes in seed_emails:
        await conn.execute(
            "INSERT INTO admins (id, email, added_by, notes) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (email) DO NOTHING",
            (str(_uuid.uuid4()), email, "system", notes),
        )


# ── Migration registry ──────────────────────────────────────────────────────
# Ordered tuples consumed by _run_pg_migrations (see init_pg_db). Order is
# load-bearing and matches the historical call sequence exactly. Defined at
# module level — after every _migrate_pg_* function exists — so the references
# resolve at import time. Each migration is idempotent and runs through a
# per-migration try/except so one failure can't crash uvicorn startup.
_PG_MIGRATIONS_CORE = (
    _migrate_pg_sprint_items_v2,
    _migrate_pg_v25_sprint_feedback,
    _migrate_pg_drop_chat_tables,
    _migrate_pg_goal_field_timestamps,
    _migrate_pg_v24_task_tree_and_framework,
    _migrate_pg_project_settings,
    _migrate_pg_notify_email,
    _migrate_pg_file_locks,
    _migrate_pg_active_worktrees,
    _migrate_pg_task_sprint_link,
    _migrate_pg_v26_client_type,
    _migrate_pg_v27_pg_trgm,
    _migrate_pg_v24_pinned_decisions_and_hitl,
    _migrate_pg_v09_notes_and_magic_links,
    _migrate_pg_v32_workspace_and_checkpoint,
    _migrate_pg_v33_hitl_kind_payload,
    _migrate_pg_v34_hitl_auto_answer,
    _migrate_pg_v34_workspace_settings,
    _migrate_pg_workspace_settings_columns,
    _migrate_pg_project_icon,
)

# Main auth DB only — tenants, billing, admins, RBAC. Skipped on per-customer DBs.
_PG_MIGRATIONS_HOSTED = (
    _migrate_pg_v10_tenant_columns,
    _migrate_pg_v25_admins_table,
    _migrate_pg_v28_dunning_and_github_sub,
    _migrate_pg_v29_free_tier_columns,
    _migrate_pg_v31_github_integration,
    _migrate_pg_v25_notification_prefs,
    _migrate_pg_tenants_is_internal,
    _migrate_pg_workspace_members_rbac,
    _migrate_pg_admin_plan,
)

# Late migrations — run on every DB after the hosted-only set.
_PG_MIGRATIONS_LATE = (
    _migrate_pg_workspace_tenant_isolation,
    _migrate_pg_sprint_items_claimed_at,
    _migrate_pg_sprint_item_tree,
    _migrate_pg_api_token_type,
    _migrate_pg_api_token_expires_at,
    _migrate_pg_oauth_codes,
    _migrate_pg_github_to_projects,
    _migrate_pg_queued_session,
)
