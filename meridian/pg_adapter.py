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
  sqlite_master    → translated into the equivalent real pg_catalog /
                     information_schema introspection query (see
                     PostgresConnection._sqlite_master)
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

# 8a52dd26 -- clock_timestamp(), not now(): now()/CURRENT_TIMESTAMP is frozen at
# transaction START for the whole transaction, not evaluated per statement. In
# prod this is invisible (autocommit=True means every statement is its own
# transaction, so now() is already fresh per call). But tests/conftest.py's
# transactional test-isolation wraps a whole test in ONE transaction, so with
# now() every row a test inserts gets the IDENTICAL timestamp -- breaking any
# "newest first" ordering or elapsed-time check across rows created in the same
# test. clock_timestamp() always returns the actual current time regardless of
# transaction/savepoint boundaries, matching what callers actually mean by
# "record when this happened."
_DATETIME_NOW_EXPR = "to_char(clock_timestamp() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US')"


def _pg_adapt_sql(sql: str, params: tuple) -> tuple[str, list]:
    """Convert SQLite-flavoured SQL + params to Postgres-compatible form.

    Returns ``(pg_sql, pg_params_list)``.  Uses %s placeholders (psycopg3).
    """
    # 0. Translate SQLite-only INSERT OR IGNORE / INSERT OR REPLACE to PG equivalents
    #    BEFORE any other rewriting so downstream steps never see the SQLite syntax.
    #
    #    INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
    #    INSERT OR REPLACE → INSERT ... ON CONFLICT DO NOTHING
    #
    #    Both forms are "best-effort upserts" in the codebase (locked behind try/except);
    #    DO NOTHING is always correct for OR IGNORE, and for OR REPLACE we fall back to
    #    DO NOTHING too since the tables that use it (oauth_codes, oauth_clients, file_patch_counters)
    #    have a primary-key constraint — the conflict target is the primary key, and a plain
    #    DO NOTHING is semantically equivalent to "skip if already present", which matches
    #    the caller intent (idempotent insert / re-use existing row is fine).
    #    For a true upsert, callers should use explicit ON CONFLICT DO UPDATE.
    _had_or_ignore = bool(re.search(r"\bINSERT\s+OR\s+IGNORE\b", sql, re.IGNORECASE))
    _had_or_replace = bool(re.search(r"\bINSERT\s+OR\s+REPLACE\b", sql, re.IGNORECASE))
    if _had_or_ignore:
        sql = re.sub(r"\bINSERT\s+OR\s+IGNORE\b", "INSERT", sql, flags=re.IGNORECASE)
    if _had_or_replace:
        sql = re.sub(r"\bINSERT\s+OR\s+REPLACE\b", "INSERT", sql, flags=re.IGNORECASE)
    if _had_or_ignore or _had_or_replace:
        # Append ON CONFLICT DO NOTHING before any trailing semicolon / whitespace
        sql = re.sub(r";?\s*$", " ON CONFLICT DO NOTHING", sql.rstrip())

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
            f" 'YYYY-MM-DD HH24:MI:SS.US')"
        ),
        sql,
    )

    # 3. datetime('now', %s) — general param form, e.g. '-7 days'
    sql = re.sub(
        r"datetime\('now',\s*(%s)\)",
        lambda m: (
            f"to_char(now() at time zone 'utc' + {m.group(1)}::interval,"
            f" 'YYYY-MM-DD HH24:MI:SS.US')"
        ),
        sql,
    )

    # 3b. datetime('now', 'literal interval') — e.g. '-10 minutes', '-1 day'
    sql = re.sub(
        r"datetime\('now',\s*'([^']+)'\)",
        lambda m: (
            f"to_char(now() at time zone 'utc' + '{m.group(1)}'::interval,"
            f" 'YYYY-MM-DD HH24:MI:SS.US')"
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

    Also implements the Coroutine protocol (send/throw/close) so that
    ``asyncio.run(db.execute(sql))`` works — asyncio.run() calls
    ``asyncio.coroutines.iscoroutine()`` which requires send/throw/close to
    be present (mirrors aiosqlite's own Result class approach).
    """

    __slots__ = ("_coro", "_cursor", "_resolve_coro")

    def __init__(self, coro) -> None:
        self._coro = coro
        self._cursor: _PgCursor | None = None
        self._resolve_coro = None  # lazily initialised; shared by __await__ + send/throw

    async def _resolve(self) -> _PgCursor:
        if self._cursor is None:
            self._cursor = await self._coro
        return self._cursor

    def _get_resolve_coro(self):
        """Return the single shared _resolve() coroutine, creating it once."""
        if self._resolve_coro is None:
            self._resolve_coro = self._resolve()
        return self._resolve_coro

    # ---- Coroutine protocol (send / throw / close) -------------------------
    # Required so asyncio.run(db.execute(sql)) works: asyncio.Runner.run()
    # calls asyncio.coroutines.iscoroutine() which checks for these methods.

    def send(self, value):  # type: ignore[override]
        return self._get_resolve_coro().send(value)

    def throw(self, typ, val=None, tb=None):  # type: ignore[override]
        if val is None:
            return self._get_resolve_coro().throw(typ)
        if tb is None:
            return self._get_resolve_coro().throw(typ, val)
        return self._get_resolve_coro().throw(typ, val, tb)

    def close(self):  # type: ignore[override]
        return self._get_resolve_coro().close()

    # ---- Awaitable interface -----------------------------------------------

    def __await__(self):  # type: ignore[override]
        return self._get_resolve_coro().__await__()

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

    async def rollback(self) -> None:
        """No-op: psycopg3 autocommit=True means each statement is its own
        transaction; there is no pending transaction to roll back.

        Callers that need atomicity on Postgres must use compensating actions
        instead of relying on this no-op.  The method exists only so callers
        written for aiosqlite (which has a real rollback) don't raise
        AttributeError when running against the Postgres backend.
        """

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
            return await self._sqlite_master(stripped, params)

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
                # Fetch rows when the statement returns data: SELECTs and
                # any DML (UPDATE/INSERT/DELETE) with a RETURNING clause.
                _returns_rows = q_upper.startswith(("SELECT", "WITH")) or (
                    "RETURNING" in pg_sql.upper()
                )
                if _returns_rows:
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

    # ---- sqlite_master translation ---------------------------------------
    #
    # SQLite-authored code across this codebase (migration guards in
    # meridian/db/migrations.py, plus a handful of tests that exercise those
    # guards or assert a guarded index/table was created) queries the
    # SQLite-only system catalog ``sqlite_master`` to ask "does table/index X
    # exist" and, for a couple of legacy-CHECK-constraint detectors, "what is
    # X's DDL text". Postgres has no ``sqlite_master``; we translate the
    # WHERE clause into the equivalent real Postgres catalog query
    # (``information_schema.tables`` / ``pg_indexes``) instead of returning a
    # fixed fake row. Only the query *shapes* this codebase actually issues
    # are recognised (see 246eccb6 investigation notes): a bare
    # ``type = 'table'|'index' AND name = <literal-or-?>`` predicate,
    # optionally selecting ``name`` or ``sql``. An unrecognised shape (e.g. a
    # bulk "list every table" query, only ever used against a real SQLite
    # connection directly, never through this adapter) logs a warning and
    # returns an EMPTY result -- a truthful "nothing matched" -- rather than
    # fabricating a row that would silently lie to the caller.
    _SQLITE_MASTER_RE = re.compile(
        r"SELECT\s+(?P<col>\w+)\s+FROM\s+sqlite_master\s+WHERE\s+"
        r"type\s*=\s*'(?P<objtype>table|index)'\s+AND\s+name\s*=\s*"
        r"(?:'(?P<name_lit>[^']*)'|\?)",
        re.IGNORECASE,
    )

    async def _sqlite_master(self, stripped_sql: str, params: tuple) -> _PgCursor:
        m = self._SQLITE_MASTER_RE.search(stripped_sql)
        if not m:
            logger.warning(
                "PostgresConnection: unrecognized sqlite_master query shape; "
                "returning an empty result instead of a fabricated row: %r",
                stripped_sql,
            )
            return _PgCursor([], 0)

        name = m.group("name_lit")
        if name is None:
            # Bound as a positional ``?`` placeholder rather than inlined.
            if not params:
                return _PgCursor([], 0)
            name = params[0]

        if m.group("objtype").lower() == "index":
            return await self._sqlite_master_index(name)
        want_sql = m.group("col").lower() == "sql"
        return await self._sqlite_master_table(name, want_sql)

    async def _sqlite_master_index(self, index_name: str) -> _PgCursor:
        """Real ``pg_indexes`` lookup for a single index by name.

        ``pg_indexes.indexdef`` is the full ``CREATE [UNIQUE] INDEX ...``
        text -- the direct Postgres analog of sqlite_master's ``sql`` column
        -- so a caller asserting e.g. ``"UNIQUE" in sql.upper()`` (the
        workspace-proposals idempotency-index parity test) gets a real,
        query-aware answer instead of unrelated placeholder text.
        """
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=_dict_row_factory) as cur:
                await cur.execute(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE indexname = %s",
                    (index_name,),
                )
                row = await cur.fetchone()
        if row is None:
            return _PgCursor([], 0)
        return _PgCursor([{"name": row["indexname"], "sql": row["indexdef"]}], 1)

    async def _sqlite_master_table(self, table_name: str, want_sql: bool) -> _PgCursor:
        """Real ``information_schema.tables`` existence check for a table.

        When the caller also asked for the ``sql`` column, best-effort
        reconstructs a DDL-like string from live ``information_schema.columns``
        data (real catalog data, not a fabricated string) -- good enough for
        the "does this table exist" question every reachable-through-Postgres
        caller actually asks. The handful of migrations.py callers that
        string-search a table's ``sql`` text for a legacy SQLite CHECK
        constraint are SQLite-only migration guards that are never invoked
        against a Postgres connection in practice (Postgres provisions its
        schema from meridian/pg_adapter.py's own DDL literals, not by running
        meridian/db/migrations.py's guarded ``_migrate_*`` helpers) -- this
        reconstruction is a defensive best effort for that shape, not a
        contract any real caller currently depends on.
        """
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=_dict_row_factory) as cur:
                await cur.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_name = %s",
                    (table_name,),
                )
                exists = await cur.fetchone()
            if exists is None:
                return _PgCursor([], 0)

            sql_text = None
            if want_sql:
                async with conn.cursor(row_factory=_dict_row_factory) as cur:
                    await cur.execute(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_name = %s ORDER BY ordinal_position",
                        (table_name,),
                    )
                    columns = await cur.fetchall()
                col_sql = ", ".join(
                    f"{c['column_name']} {c['data_type']}" for c in columns
                )
                sql_text = f"CREATE TABLE {table_name} ({col_sql})"

        return _PgCursor([{"name": table_name, "sql": sql_text}], 1)


# ---------------------------------------------------------------------------
# Postgres-compatible CREATE TABLE DDL
# ---------------------------------------------------------------------------

# 8a52dd26 -- clock_timestamp(), not now(): see _DATETIME_NOW_EXPR above for why.
_TS = "to_char(clock_timestamp() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US')"

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
    auto_worktrees INTEGER NOT NULL DEFAULT 1,
    require_merge_approval INTEGER NOT NULL DEFAULT 1,
    icon TEXT,
    github_repo TEXT,
    github_branch TEXT,
    queued_session TEXT,
    pending_goal TEXT,
    parent_project_id TEXT,
    execution_mode TEXT NOT NULL DEFAULT 'autonomous',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'parked', 'archived')),
    priority TEXT NOT NULL DEFAULT 'P2' CHECK (priority IN ('P0', 'P1', 'P2')),
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
    checkpoint_data TEXT,
    sprint_version TEXT,
    goal_compliance TEXT
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
    claimed_at TEXT,
    task_id TEXT,
    notes TEXT,
    feedback_thumb SMALLINT,
    feedback_note TEXT,
    milestone_type TEXT NOT NULL DEFAULT 'task',
    -- Task-tree columns (mirror SQLite base + _migrate_pg_sprint_item_tree). NULL =
    -- no parent / not split / not merged. parent_id references sprint_items(id).
    parent_id TEXT DEFAULT NULL REFERENCES sprint_items(id),
    split_from TEXT DEFAULT NULL,
    merged_into TEXT DEFAULT NULL,
    merged_from TEXT DEFAULT NULL,
    -- 4f02340e: mixed-ownership task chains. 'human' | 'ai' | NULL (unassigned).
    owner TEXT DEFAULT NULL,
    -- v2.6 (dependency tracking, mirrors db._migrate_sprint_item_dependency):
    -- depends_on references a sibling sprint item id (NULL = no dependency);
    -- failure_mode is what to do when the depended-on item has failed —
    -- 'continue' (default, still claimable) or 'stop' (blocked on parent failure).
    depends_on TEXT,
    failure_mode TEXT NOT NULL DEFAULT 'continue',
    -- touches_files (file conflict tracking) + touches_resources (501ec93f: typed
    -- resource identifiers, JSON list generalizing touches_files). Both nullable.
    touches_files TEXT,
    touches_resources TEXT,
    -- bc9259b8: worker stall auto-retry counter. NOT NULL DEFAULT 0 so legacy rows
    -- read as "never stalled".
    stall_count INTEGER NOT NULL DEFAULT 0,
    -- 5823db0b: quality gates + actor attribution. required_notes is a note-count
    -- gate (INTEGER DEFAULT 0); actor records who last acted on the item.
    required_notes INTEGER DEFAULT 0,
    actor TEXT,
    slug TEXT,
    nickname TEXT,
    deferred_until TEXT,
    track TEXT,
    -- e08fee30: app-layer priority enum urgent|high|normal|low. Urgent-first
    -- ordering in get_sprint_items / get_parallelizable_groups. Enum enforced at
    -- the app layer (no CHECK, so the ADD COLUMN migration stays plain).
    -- (NB: this literal is an f-string; keep curly braces out of these comments.)
    priority TEXT NOT NULL DEFAULT 'normal',
    -- 2282a636: NULL = ordinary, 'manual' = blocked on a real-world action outside
    -- Meridian. Distinct from milestone_type='human'; excluded from executor scoping.
    blocker_kind TEXT,
    -- 58a45b92: stored, deterministic wave label (e.g. 'wave-1'); NULL = unassigned.
    wave TEXT,
    -- 3d6bd938: separate human-readable sprint name from the structural version
    -- identifier (mirrors SQLite). Nullable — legacy rows are NULL.
    sprint_name TEXT,
    -- 94c26322: human-set bypass flag for the prospecting safety gate. 0/default
    -- means the structural gate applies (unprospected items excluded from auto-run).
    -- 1 means an explicit human override allows the item through anyway.
    -- Settable ONLY by planning/human sessions. Plain column — no inline index.
    prospect_bypass INTEGER NOT NULL DEFAULT 0
);

-- v2.4 — decisions_pinned: editable constitution. See db.py for rationale.
CREATE TABLE IF NOT EXISTS decisions_pinned (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'TECHNICAL',
    priority TEXT NOT NULL DEFAULT 'normal',
    edit_log TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    superseded_by TEXT REFERENCES decisions_pinned(id),
    created_at TEXT NOT NULL DEFAULT ({_TS}),
    updated_at TEXT NOT NULL DEFAULT ({_TS})
);

-- 0b711a9d — insights: durable strategic understanding (mirrors SQLite).
CREATE TABLE IF NOT EXISTS insights (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    horizon TEXT NOT NULL DEFAULT 'quarter',
    tags TEXT,
    status TEXT NOT NULL DEFAULT 'active',
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
    slug TEXT,
    file_path TEXT,
    symbol TEXT,
    source TEXT,
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
    -- 0d7de2a2: thinking_sync. 'thinking' marks a HOOKS_DEBUG_STATE scratchpad
    -- note; NULL/'note' = a normal note. See db._migrate_session_note_kind.
    note_kind TEXT,
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

-- 501ec93f — resource_locks: generalized typed-resource lock (see db.py).
CREATE TABLE IF NOT EXISTS resource_locks (
    id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL UNIQUE,
    resource_type TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    claimed_at TEXT NOT NULL DEFAULT ({_TS}),
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resource_locks_session ON resource_locks(session_id);
CREATE INDEX IF NOT EXISTS idx_resource_locks_expires ON resource_locks(expires_at);
CREATE INDEX IF NOT EXISTS idx_resource_locks_type ON resource_locks(resource_type);

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

-- workspace_sprint_items: tenant-global personal backlog, NOT tied to a single
-- project. Mirrors the useful subset of sprint_items but keyed by tenant_id;
-- item_group is the cross-project bucket ('thesis'/'meridian'/'personal').
CREATE TABLE IF NOT EXISTS workspace_sprint_items (
    id TEXT PRIMARY KEY,
    tenant_id TEXT,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'todo',
    item_group TEXT,
    human_id TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT ({_TS}),
    updated_at TEXT NOT NULL DEFAULT ({_TS}),
    completed_at TEXT
);
-- idx_workspace_sprint_items_tenant is created by _migrate_pg_workspace_sprint_board,
-- NOT inline here: a pre-tenant_id copy of this table would make this unguarded
-- executescript CREATE INDEX crash startup (see the init_pg_db note above about the
-- 2026-06-13 outage). The migration runs in _run_pg_migrations, which survives it.

-- v3.4 — workspace-level settings singleton (tenant-global defaults).
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
    refresh_trigger_min_interval INTEGER NOT NULL DEFAULT 3,
    handoff_inline_pointers INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT ({_TS})
);

-- 6234f9b8 — blog_posts: admin-authored posts served at /blog/<slug>.
-- 8843250f — workspace-scoped via tenant_id.
CREATE TABLE IF NOT EXISTS blog_posts (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    body_md TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    tenant_id TEXT,
    created_at TEXT NOT NULL DEFAULT ({_TS}),
    updated_at TEXT NOT NULL DEFAULT ({_TS}),
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_blog_posts_status ON blog_posts(status);
-- idx_blog_posts_tenant is created by _migrate_pg_blog_posts_tenant (which also
-- ALTERs the column onto pre-8843250f blog_posts tables). It must NOT be inline
-- here: blog_posts predates tenant_id, so on an existing DB this unguarded
-- executescript CREATE INDEX hit a missing column and crash-looped startup
-- (exit 3) on the 2026-07-04 promote. The guarded migration handles both paths.

-- 2976e168 — sprint_item_pointers: the GENERIC POINTER PRIMITIVE (mirrors SQLite).
-- ONE table, a JSON ``targets`` array of composite target objects — NOT
-- per-domain columns. idx_sprint_item_pointers_item is created ONLY by the
-- guarded _migrate_pg_sprint_item_pointers migration, never inline here.
-- (NB: this literal is an f-string; keep curly braces out of these comments.)
CREATE TABLE IF NOT EXISTS sprint_item_pointers (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    sprint_item_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    targets TEXT NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL DEFAULT ({_TS})
);

-- 3295c784 — mcp_rate_counters: cross-instance shared hit-counting for the
-- consolidated /mcp tenant-tier rate limiter. Per-process _tenant_rl_hits
-- under-counts across N Fly machines; this windowed counter (keyed by
-- tenant_id + epoch-minute window_start) gives every instance one shared count
-- via an atomic upsert. Gated by MERIDIAN_SHARED_RATE_LIMIT (default OFF).
-- The composite PRIMARY KEY indexes lookups; the prune-by-window index lives
-- ONLY in the guarded _migrate_pg_mcp_rate_counters migration, never inline.
CREATE TABLE IF NOT EXISTS mcp_rate_counters (
    tenant_id TEXT NOT NULL,
    window_start BIGINT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, window_start)
);

-- 5c4dcc0f — workspace_proposals: human-only "drawer of inspiration" for
-- cross-project flashes of insight. Workspace-scoped (tenant_id, not
-- project_id). status: raw → investigating → promoted | rejected.
-- promoted_to_sprint_item_id links a promoted proposal to the sprint item it
-- became. NOT auto-claimable by executors — human-reviewed promotion gate only.
-- idx_workspace_proposals_tenant created by _migrate_pg_workspace_proposals
-- (guarded migration), never inline here (2026-07-04 inline-index outage rule).
CREATE TABLE IF NOT EXISTS workspace_proposals (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tags TEXT,
    status TEXT NOT NULL DEFAULT 'raw',
    promoted_to_sprint_item_id TEXT,
    tenant_id TEXT,
    family_id TEXT,
    created_at TEXT NOT NULL DEFAULT ({_TS}),
    updated_at TEXT NOT NULL DEFAULT ({_TS}),
    last_activity_at TEXT NOT NULL DEFAULT ({_TS})
);

-- 8c147109 — session_activity: lightweight ring-buffer heartbeat feed.
-- Records one row per significant MCP tool call in an executor session so a
-- remote planner can see signs of life via get_session_log even before the
-- executor calls log_task(). Bounded to the last 50 entries per session
-- (enforced by record_session_activity at write time — no DB trigger needed).
-- idx_session_activity_session is created by _migrate_pg_session_activity
-- (guarded migration), never inline here (2026-07-04 inline-index outage rule).
CREATE TABLE IF NOT EXISTS session_activity (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    summary TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT ({_TS})
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
    tunnel_active INTEGER NOT NULL DEFAULT 0,
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
    -- d116642e: project-level invites foundation. NULL = workspace-wide
    -- (current behavior); set = project-scoped (listing-only scoping for now).
    -- Airtight per-request enforcement deferred (pin b11c7cf6).
    project_id TEXT,
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

    bb16f9a7/1280c66e — bound how long any single migration statement may wait
    on a lock or run, so a canary deploy can't HANG the app past Fly's health
    check when the still-live old machine holds a lock on a hot table (e.g. an
    ``ALTER TABLE sprint_items ADD COLUMN`` behind active sprint-board queries).
    A timed-out statement raises → caught below → logged → retried next boot once
    contention clears. Reset afterward so app queries keep the pool's defaults.
    """
    _timeouts_set = False
    try:
        await conn.execute("SET lock_timeout = '5s'")
        await conn.execute("SET statement_timeout = '120s'")
        _timeouts_set = True
    except Exception:  # noqa: BLE001 — best-effort guard; never block startup
        pass
    try:
        for migration in migrations:
            try:
                await migration(conn)
            except Exception as exc:  # noqa: BLE001 — startup must survive any migration error
                logger.warning(
                    "pg migration %s failed (continuing startup): %s",
                    getattr(migration, "__name__", repr(migration)),
                    exc,
                )
    finally:
        if _timeouts_set:
            try:
                await conn.execute("SET lock_timeout = DEFAULT")
                await conn.execute("SET statement_timeout = DEFAULT")
            except Exception:  # noqa: BLE001
                pass


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
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US'))"
        ")"
    )


async def _migrate_pg_device_codes(conn: PostgresConnection) -> None:
    """e9f18530 — RFC 8628 device authorization grant table (Postgres).

    device_code / user_code hold SHA-256 HASHES of the codes, never the raw
    values. last_polled_at backs the slow_down poll-rate limiter. CREATE … IF
    NOT EXISTS + ADD COLUMN IF NOT EXISTS so re-running is a no-op and older PG
    DBs that never had the table get the full hardened schema. Mirrors
    db._migrate_device_codes_table + db._migrate_device_codes_denied_polled.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS device_codes ("
        "    device_code TEXT PRIMARY KEY,"
        "    user_code TEXT NOT NULL UNIQUE,"
        "    tenant_id TEXT,"
        "    expires_at TEXT NOT NULL,"
        "    approved INTEGER NOT NULL DEFAULT 0,"
        "    denied INTEGER NOT NULL DEFAULT 0,"
        "    last_polled_at TEXT,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS})"
        ");"
        "ALTER TABLE device_codes ADD COLUMN IF NOT EXISTS denied INTEGER NOT NULL DEFAULT 0;"
        "ALTER TABLE device_codes ADD COLUMN IF NOT EXISTS last_polled_at TEXT"
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


async def _migrate_pg_pending_goal(conn: PostgresConnection) -> None:
    """5efe254b — projects.pending_goal: the handoff /goal delivered through a
    trusted MCP tool result (keyed on project_id) instead of a spoofable
    copy-pasted chat string. Read-once. Nullable."""
    await conn.executescript(
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS pending_goal TEXT"
    )


async def _migrate_pg_insights_table(conn: PostgresConnection) -> None:
    """0b711a9d — strategic insights table (mirrors SQLite). Idempotent."""
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS insights ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,"
        "    title TEXT NOT NULL,"
        "    body TEXT NOT NULL,"
        "    horizon TEXT NOT NULL DEFAULT 'quarter',"
        "    tags TEXT,"
        "    status TEXT NOT NULL DEFAULT 'active',"
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US')),"
        "    updated_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US'))"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_insights_project ON insights(project_id, horizon);"
    )


async def _migrate_pg_sprint_item_slug(conn: PostgresConnection) -> None:
    """b944c905 — sprint_items.slug human-readable id (mirrors SQLite). Idempotent."""
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS slug TEXT"
    )


async def _migrate_pg_sprint_item_nickname(conn: PostgresConnection) -> None:
    """b6b0cee6 — sprint_items.nickname short memorable handle (mirrors SQLite). Idempotent."""
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS nickname TEXT"
    )


async def _migrate_pg_capture_insight_notes_to_insights(conn: PostgresConnection) -> None:
    """b5ed8a61 — retire the legacy ``capture_insight`` tool: MOVE every
    ``project_notes`` row with ``note_kind = 'insight'`` into the dedicated
    ``insights`` table (mirrors SQLite _migrate_capture_insight_notes_to_insights).

    Per row: INSERT into ``insights`` (reusing the note's id, horizon='quarter',
    status='active') THEN DELETE the note — insert-before-delete so a crash
    mid-row can never lose data. Reusing the note id makes this a pure MOVE and
    idempotent: after it runs there are no ``note_kind = 'insight'`` rows left,
    so a re-run selects nothing and is a no-op. PG runs autocommit — no commit.
    The adapter rewrites ``?`` → ``%s`` in the raw SQL below.
    """
    # bb16f9a7 — set-based (was per-row) so it can't slow-loop and delay startup
    # on a large table. Pure idempotent MOVE (id reuse; re-run selects nothing).
    await conn.execute(
        "INSERT INTO insights (id, project_id, title, body, horizon, tags, status) "
        "SELECT id, project_id, title, COALESCE(body, ''), 'quarter', tags, 'active' "
        "FROM project_notes WHERE note_kind = 'insight'"
    )
    await conn.execute("DELETE FROM project_notes WHERE note_kind = 'insight'")


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


async def _migrate_pg_workspace_members_project_scope(conn: PostgresConnection) -> None:
    """d116642e — project-level invites foundation.

    Adds a nullable ``project_id`` to ``workspace_members``: NULL = workspace-wide
    member (current behavior), set = project-scoped member (listing-only scoping
    for now). ADD COLUMN IF NOT EXISTS so re-running is a no-op. Mirrors
    db._migrate_workspace_members_project_scope. Airtight per-request access
    enforcement is intentionally deferred pending the product decision (pin
    b11c7cf6).
    """
    await conn.executescript(
        "ALTER TABLE workspace_members ADD COLUMN IF NOT EXISTS project_id TEXT"
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


async def _migrate_pg_workspace_sprint_board(conn: PostgresConnection) -> None:
    """workspace_sprint_items — tenant-global personal backlog (cross-project).

    Mirrors the SQLite _migrate_workspace_sprint_board. Tenant-scoped
    (tenant_id, NOT project_id). Idempotent — CREATE TABLE / INDEX IF NOT
    EXISTS. CREATE_TABLES_CORE covers fresh DBs; this is the upgrade path."""
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS workspace_sprint_items ("
        "    id TEXT PRIMARY KEY,"
        "    tenant_id TEXT,"
        "    title TEXT NOT NULL,"
        "    status TEXT NOT NULL DEFAULT 'todo',"
        "    item_group TEXT,"
        "    human_id TEXT,"
        "    position INTEGER NOT NULL DEFAULT 0,"
        "    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        "    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        "    completed_at TIMESTAMPTZ"
        ")"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspace_sprint_items_tenant "
        "ON workspace_sprint_items(tenant_id, status)"
    )


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


async def _migrate_pg_tunnel_active(conn: PostgresConnection) -> None:
    """b43b0c6a — tenants.tunnel_active: live binary-connection flag. Idempotent."""
    await conn.executescript(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS tunnel_active INTEGER NOT NULL DEFAULT 0"
    )


async def _migrate_pg_tunnel_plugins(conn: PostgresConnection) -> None:
    """Tunnel plugin registry — tenants.tunnel_plugins: per-tenant JSON config for
    what `meridian --tunnel` spawns behind each transport slot. NULL → built-in
    defaults. Mirrors the SQLite _migrate_tunnel_plugins. Idempotent."""
    await conn.executescript(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS tunnel_plugins TEXT"
    )


async def _migrate_pg_tunnel_plugins_by_host(conn: PostgresConnection) -> None:
    """8660d701 — tenants.tunnel_plugins_by_host: per-machine tunnel plugin config
    (JSON {hostname: overrides}). The legacy tunnel_plugins stays the default for
    hosts without a per-host entry. Mirrors SQLite _migrate_tunnel_plugins_by_host.
    Idempotent."""
    await conn.executescript(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS tunnel_plugins_by_host TEXT"
    )


async def _migrate_pg_code_intel(conn: PostgresConnection) -> None:
    """Sprint-2/3 — projects.code_intel_enabled: per-project Code Intelligence toggle."""
    await conn.executescript(
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS code_intel_enabled INTEGER NOT NULL DEFAULT 0"
    )


async def _migrate_pg_project_status_priority(conn: PostgresConnection) -> None:
    """8db00fcb — projects.status (active|parked|archived) + priority (P0|P1|P2)."""
    await conn.executescript(
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';"
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS priority TEXT NOT NULL DEFAULT 'P2'"
    )


async def _migrate_pg_signup_attempts(conn: PostgresConnection) -> None:
    """925909aa — persistent per-IP signup-attempt log (mirrors the SQLite
    signup_attempts table) for magic-link abuse limiting. Salted hashes only."""
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS signup_attempts ("
        "    id TEXT PRIMARY KEY,"
        "    ip_hash TEXT NOT NULL,"
        "    email_hash TEXT NOT NULL,"
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US'))"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_signup_attempts_ip ON signup_attempts(ip_hash, created_at);"
    )


async def _migrate_pg_user_session_metadata(conn: PostgresConnection) -> None:
    """3c28450d — device metadata on user_sessions (mirrors SQLite)."""
    await conn.executescript(
        "ALTER TABLE user_sessions ADD COLUMN IF NOT EXISTS user_agent TEXT;"
        "ALTER TABLE user_sessions ADD COLUMN IF NOT EXISTS ip TEXT;"
        "ALTER TABLE user_sessions ADD COLUMN IF NOT EXISTS last_seen_at TEXT"
    )


async def _migrate_pg_provision_queue(conn: PostgresConnection) -> None:
    """4c559d4e — durable provisioning queue (mirrors SQLite provision_queue)."""
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS provision_queue ("
        "    tenant_id TEXT PRIMARY KEY,"
        "    status TEXT NOT NULL DEFAULT 'pending',"
        "    attempts INTEGER NOT NULL DEFAULT 0,"
        "    last_error TEXT,"
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US')),"
        "    updated_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US'))"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_provision_queue_status ON provision_queue(status);"
    )


async def _migrate_pg_codebase_graph_entities(conn: PostgresConnection) -> None:
    """c00b1ccf — cached codebase-graph snapshot (mirrors SQLite)."""
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS codebase_graph_entities ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL,"
        "    qualified_name TEXT NOT NULL,"
        "    file TEXT,"
        "    kind TEXT,"
        "    signature TEXT,"
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US'))"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_cge_project ON codebase_graph_entities(project_id);"
    )


async def _migrate_pg_notes_priority(conn: PostgresConnection) -> None:
    """Sprint-4 — project_notes.priority: high/normal/low ranking for generate_handoff."""
    await conn.executescript(
        "ALTER TABLE project_notes ADD COLUMN IF NOT EXISTS priority TEXT NOT NULL DEFAULT 'normal'"
    )


async def _migrate_pg_task_log_kind(conn: PostgresConnection) -> None:
    """Sprint-4 — task_log.kind: shipped/found/decided/blocked taxonomy."""
    await conn.executescript(
        "ALTER TABLE task_log ADD COLUMN IF NOT EXISTS kind TEXT DEFAULT 'shipped'"
    )


async def _migrate_pg_oauth_refresh_tokens(conn: PostgresConnection) -> None:
    """Sprint-5 — oauth_refresh_tokens: RFC 6749 refresh_token with rotation."""
    await conn.executescript(
        """CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
            token_hash TEXT PRIMARY KEY,
            tenant_id TEXT,
            client_id TEXT,
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )"""
    )


async def _migrate_pg_note_slug(conn: PostgresConnection) -> None:
    """5a5bba43 — project_notes.slug: Obsidian ``mem:name`` style handle.

    Adds a nullable ``slug`` column and backfills pre-existing rows with a
    kebab-cased, per-project-unique slug derived from the title. Idempotent:
    ADD COLUMN IF NOT EXISTS plus a backfill that only fills NULL/empty slugs,
    so re-running on an already-migrated DB is a no-op. The uniqueness loop runs
    in Python (mirrors the SQLite path) to stay in sync with add_project_note's
    collision suffixing.
    """
    await conn.executescript(
        "ALTER TABLE project_notes ADD COLUMN IF NOT EXISTS slug TEXT"
    )
    async with conn.execute(
        "SELECT id, project_id, title FROM project_notes "
        "WHERE slug IS NULL OR slug = '' ORDER BY created_at ASC, id ASC"
    ) as cur:
        rows = list(await cur.fetchall())
    if not rows:
        return
    used: dict[str, set[str]] = {}
    async with conn.execute(
        "SELECT project_id, slug FROM project_notes "
        "WHERE slug IS NOT NULL AND slug != ''"
    ) as cur:
        for r in await cur.fetchall():
            used.setdefault(r["project_id"], set()).add(r["slug"])
    for r in rows:
        seen = used.setdefault(r["project_id"], set())
        base = _slugify_note_pg(r["title"])
        slug = base
        n = 1
        while slug in seen:
            n += 1
            slug = f"{base}-{n}"
        seen.add(slug)
        await conn.execute(
            "UPDATE project_notes SET slug = ? WHERE id = ?", (slug, r["id"])
        )


async def _migrate_pg_decision_priority_edit_log(conn: PostgresConnection) -> None:
    """366317e9 — decisions_pinned.priority + decisions_pinned.edit_log.

    priority (urgent | normal | low, default 'normal') drives dashboard ordering
    and context-injection weight. edit_log is an append-only JSON array of
    ``{"body": <previous body>, "ts": <iso timestamp>}`` entries pushed on every
    in-place body edit. Both ADD COLUMN IF NOT EXISTS so re-running is a no-op;
    existing rows default priority to 'normal' and leave edit_log NULL.
    """
    await conn.executescript(
        "ALTER TABLE decisions_pinned "
        "ADD COLUMN IF NOT EXISTS priority TEXT NOT NULL DEFAULT 'normal'"
    )
    await conn.executescript(
        "ALTER TABLE decisions_pinned ADD COLUMN IF NOT EXISTS edit_log TEXT"
    )


async def _migrate_pg_code_anchored_notes(conn: PostgresConnection) -> None:
    """771c00d7 — project_notes.file_path + project_notes.symbol: code-anchored notes.

    A note with note_kind='code' plus a ``file_path`` (and optional ``symbol``)
    anchors a warning/context to a file/symbol, surfaced automatically at
    ``claim_file``/``get_file_claims``. Both nullable so normal notes are
    unaffected. ADD COLUMN IF NOT EXISTS so re-running is a no-op.
    """
    await conn.executescript(
        "ALTER TABLE project_notes ADD COLUMN IF NOT EXISTS file_path TEXT"
    )
    await conn.executescript(
        "ALTER TABLE project_notes ADD COLUMN IF NOT EXISTS symbol TEXT"
    )


async def _migrate_pg_note_source(conn: PostgresConnection) -> None:
    """e3f150d0 — project_notes.source: provenance for an ingested note.

    A document-ingested note (``note_kind='document'``) records the URL or file
    path it was extracted from in ``source``. Nullable so normal notes are
    unaffected. ADD COLUMN IF NOT EXISTS so re-running is a no-op. Mirrors
    db._migrate_note_source.
    """
    await conn.executescript(
        "ALTER TABLE project_notes ADD COLUMN IF NOT EXISTS source TEXT"
    )


async def _migrate_pg_session_sprint_version(conn: PostgresConnection) -> None:
    """a76cb7c0 — sessions.sprint_version: the sprint-version bucket a session
    is scoped to.

    start_session stores either the explicit ``version`` it was given or the
    inferred bucket (most pending items) so later calls auto-filter sprint
    progress/items to it. Nullable so unscoped sessions behave as before. ADD
    COLUMN IF NOT EXISTS so re-running is a no-op. Mirrors
    db._migrate_session_sprint_version.
    """
    await conn.executescript(
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS sprint_version TEXT"
    )


async def _migrate_pg_project_execution_mode(conn: PostgresConnection) -> None:
    """ecf69de8 — projects.execution_mode: per-project executor posture.

    'autonomous' (default) — claim and run pending sprint items immediately
    without asking for direction. 'interactive' — review the items and ask the
    human which to start first. Injected at the protocol level by start_session
    and selects the /goal framing in _build_quick_start_goal. NOT NULL DEFAULT
    'autonomous' so existing rows backfill to autonomous. ADD COLUMN IF NOT
    EXISTS so re-running is a no-op. Mirrors db._migrate_project_execution_mode.
    """
    await conn.executescript(
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS execution_mode "
        "TEXT NOT NULL DEFAULT 'autonomous'"
    )


async def _migrate_pg_handoffs_table(conn: PostgresConnection) -> None:
    """8819d6b1 — handoffs: per-session handoff history.

    Postgres mirror of db._migrate_handoffs_table. CREATE TABLE / INDEX IF NOT
    EXISTS so re-running is a no-op.
    """
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS handoffs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            session_id TEXT,
            mode TEXT NOT NULL DEFAULT 'full',
            body TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_handoffs_project
            ON handoffs(project_id, created_at);
        """
    )


async def _migrate_pg_agent_tasks_table(conn: PostgresConnection) -> None:
    """99e71b9e — agent_tasks: Google A2A protocol task storage.

    Creates the agent_tasks table used by the A2A endpoint
    (POST /a2a/{agent_id}/tasks/send). CREATE TABLE IF NOT EXISTS so
    re-running is a no-op. Mirrors db._migrate_agent_tasks_table.
    """
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            session_id TEXT,
            status TEXT NOT NULL DEFAULT 'submitted'
                CHECK (status IN ('submitted','working','completed','failed','canceled')),
            input TEXT NOT NULL,
            output TEXT,
            metadata TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_agent_tasks_agent
            ON agent_tasks(agent_id, status);
        """
    )


async def _migrate_pg_session_note_kind(conn: PostgresConnection) -> None:
    """0d7de2a2 — session_notes.note_kind: thinking_sync scratchpad notes.

    Nullable ``note_kind`` ('thinking' | NULL/'note'). ADD COLUMN IF NOT EXISTS
    so re-running is a no-op. Mirrors db._migrate_session_note_kind.
    """
    await conn.executescript(
        "ALTER TABLE session_notes ADD COLUMN IF NOT EXISTS note_kind TEXT"
    )


async def _migrate_pg_sprint_item_owner(conn: PostgresConnection) -> None:
    """4f02340e — sprint_items.owner: mixed-ownership task chains.

    Nullable ``owner`` column ('human' | 'ai' | NULL) for the alternating
    claim/handoff state machine. ADD COLUMN IF NOT EXISTS so re-running is a
    no-op. Mirrors db._migrate_sprint_item_owner.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS owner TEXT"
    )


async def _migrate_pg_sprint_item_quality_gates(conn: PostgresConnection) -> None:
    """5823db0b — quality gates + actor attribution on sprint_items.

    Nullable ``required_notes`` (gate) + ``actor`` (attribution) columns. ADD
    COLUMN IF NOT EXISTS so re-running is a no-op. Mirrors
    db.migrations._migrate_sprint_item_quality_gates.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS required_notes INTEGER DEFAULT 0;"
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS actor TEXT;"
    )


async def _migrate_pg_parallel_primitives(conn: PostgresConnection) -> None:
    """Wave-4 parallel-coordination primitives (ffa03655, c35370cc, d3a3a01d).

    Postgres mirror of db.migrations._migrate_parallel_primitives: session_findings,
    session_messages, file_read_claims. CREATE ... IF NOT EXISTS (idempotent).
    """
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS session_findings (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            session_id TEXT,
            key TEXT,
            title TEXT,
            content TEXT NOT NULL,
            task_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_findings_project ON session_findings(project_id, key);
        CREATE TABLE IF NOT EXISTS session_messages (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            from_session_id TEXT,
            to_session_id TEXT NOT NULL,
            kind TEXT,
            payload TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            read_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_messages_to ON session_messages(to_session_id, read_at);
        CREATE TABLE IF NOT EXISTS file_read_claims (
            id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            session_id TEXT NOT NULL,
            claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            UNIQUE(file_path, session_id)
        );
        CREATE INDEX IF NOT EXISTS idx_read_claims_file ON file_read_claims(file_path);
        CREATE INDEX IF NOT EXISTS idx_read_claims_expires ON file_read_claims(expires_at);
        """
    )


def _slugify_note_pg(title: str) -> str:
    """Kebab-case a note title (lowercase, alnum+dashes, collapse, trim).

    Mirrors db.migrations._slugify_note / db._slugify_note so SQLite and
    Postgres produce identical slugs."""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return slug or "note"


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
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US')),"
        "    updated_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US'))"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_notes_project ON project_notes(project_id);"
        "CREATE TABLE IF NOT EXISTS magic_link_tokens ("
        "    id TEXT PRIMARY KEY,"
        "    email TEXT NOT NULL,"
        "    token_hash TEXT NOT NULL UNIQUE,"
        "    used_at TEXT,"
        "    expires_at TEXT NOT NULL,"
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US'))"
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
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US'))"
        ");"
        "CREATE TABLE IF NOT EXISTS workspace_decisions ("
        "    id TEXT PRIMARY KEY,"
        "    title TEXT NOT NULL,"
        "    body TEXT NOT NULL,"
        "    category TEXT NOT NULL DEFAULT 'TECHNICAL',"
        "    status TEXT NOT NULL DEFAULT 'active',"
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US'))"
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
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US')),"
        "    updated_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US'))"
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
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US')),"
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


async def _migrate_pg_parallel_safety(conn: PostgresConnection) -> None:
    """0716c9e0 — per-project parallel safety toggles. Mirrors db._migrate_parallel_safety."""
    await conn.executescript(
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS auto_worktrees INTEGER NOT NULL DEFAULT 1;"
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS require_merge_approval INTEGER NOT NULL DEFAULT 1"
    )


async def _migrate_pg_changelog_entries(conn: PostgresConnection) -> None:
    """03744d18 — changelog_entries table for user-facing release notes."""
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS changelog_entries ("
        f"    id TEXT PRIMARY KEY,"
        f"    version TEXT,"
        f"    title TEXT NOT NULL,"
        f"    body TEXT NOT NULL,"
        f"    published_at TEXT NOT NULL DEFAULT ({_TS}),"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS})"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_changelog_published "
        "ON changelog_entries(published_at DESC)"
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
    """Add display_name, log_task_sprint_nudge_threshold, handoff_template, and
    the 0bf67524 cascade defaults (execution_mode_default, code_intel_enabled_default)
    to existing workspace_settings rows. Idempotent (ADD COLUMN IF NOT EXISTS)."""
    await conn.executescript(
        "ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS display_name TEXT;"
        "ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS "
        "log_task_sprint_nudge_threshold INTEGER NOT NULL DEFAULT 5;"
        "ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS handoff_template TEXT;"
        "ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS execution_mode_default TEXT;"
        "ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS code_intel_enabled_default INTEGER;"
        "ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS "
        "loop_enabled_default INTEGER NOT NULL DEFAULT 1;"
        # bf51b12e — planner context-refresh config.
        "ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS "
        "auto_refresh_enabled INTEGER NOT NULL DEFAULT 0;"
        "ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS "
        "refresh_interval_turns INTEGER NOT NULL DEFAULT 10;"
        "ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS refresh_triggers TEXT;"
        # db0361bb — separate, smaller floor gating the trigger branch only.
        "ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS "
        "refresh_trigger_min_interval INTEGER NOT NULL DEFAULT 3;"
        # 36fea6ca — inline each pending item's RESOLVED pointers in the handoff
        # markdown (default on). Off (0) keeps them DB-only (separate resolve call).
        "ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS "
        "handoff_inline_pointers INTEGER NOT NULL DEFAULT 1"
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


async def _migrate_pg_agent_instructions(conn: PostgresConnection) -> None:
    """8a0c5a78 — projects.agent_instructions: per-project custom instructions
    injected into start_session so every AI session picks them up automatically.
    """
    await conn.executescript(
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS agent_instructions TEXT"
    )


async def _migrate_pg_backfill_agent_instructions(conn: PostgresConnection) -> None:
    """Set DEFAULT_AGENT_INSTRUCTIONS on projects with no custom rules (idempotent).

    Empty = NULL or empty string; projects with custom rules are never touched.
    """
    from .agent_defaults import DEFAULT_AGENT_INSTRUCTIONS  # avoid circular import
    await conn.execute(
        "UPDATE projects SET agent_instructions = %s "
        "WHERE agent_instructions IS NULL OR agent_instructions = ''",
        (DEFAULT_AGENT_INSTRUCTIONS,),
    )


async def _migrate_pg_note_kind(conn: PostgresConnection) -> None:
    """9d44998b — project_notes.note_kind (wiki | insight | reference).

    Nullable; the app treats NULL as 'wiki'. Existing rows are left untouched.
    """
    await conn.executescript(
        "ALTER TABLE project_notes ADD COLUMN IF NOT EXISTS note_kind TEXT"
    )


async def _migrate_pg_file_symbol_claims(conn: PostgresConnection) -> None:
    """345599ec — symbol-level parallel protection: per-symbol line-range claims.

    Table only existed in SQLite; hosted tier had no relation, breaking all
    symbol-level locking. CREATE TABLE IF NOT EXISTS is idempotent.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS file_symbol_claims ("
        "    id TEXT PRIMARY KEY,"
        "    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,"
        "    file_path TEXT NOT NULL,"
        "    symbol_name TEXT NOT NULL,"
        "    symbol_type TEXT NOT NULL,"
        "    line_start INTEGER NOT NULL,"
        "    line_end INTEGER NOT NULL,"
        f"   claimed_at TEXT NOT NULL DEFAULT ({_TS}),"
        "    released_at TEXT"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_file_symbol_claims_file "
        "ON file_symbol_claims(file_path);"
        "CREATE INDEX IF NOT EXISTS idx_file_symbol_claims_session "
        "ON file_symbol_claims(session_id)"
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


async def _migrate_pg_feedback(conn: PostgresConnection) -> None:
    """Create feedback table on existing Postgres DBs. Was missing from the PG
    schema entirely (only in the SQLite path), so submit-feedback 500'd on
    hosted with 'relation feedback does not exist'. Mirrors db.CREATE_TABLES's
    feedback table. Hosted-only (tenant-scoped, main auth DB)."""
    await conn.executescript(
        f"CREATE TABLE IF NOT EXISTS feedback ("
        f"    id TEXT PRIMARY KEY,"
        f"    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,"
        f"    type TEXT NOT NULL,"
        f"    message TEXT NOT NULL,"
        f"    email TEXT,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS})"
        f");"
        f"CREATE INDEX IF NOT EXISTS idx_feedback_tenant ON feedback(tenant_id);"
        f"CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at);"
    )


async def _migrate_pg_registered_hostnames(conn: PostgresConnection) -> None:
    """Create registered_hostnames table on existing Postgres DBs. Was missing
    from the PG schema entirely, so register_hostname/hostname_status/
    revoke_hostname 500'd on hosted with 'relation registered_hostnames does
    not exist'. Mirrors db.migrations._migrate_registered_hostnames.
    Hosted-only (control-plane / auth DB)."""
    await conn.executescript(
        f"CREATE TABLE IF NOT EXISTS registered_hostnames ("
        f"    id TEXT PRIMARY KEY,"
        f"    tenant_id TEXT NOT NULL,"
        f"    hostname TEXT NOT NULL,"
        f"    registration_token TEXT NOT NULL,"
        f"    registered_at TEXT NOT NULL DEFAULT ({_TS}),"
        f"    last_seen TEXT,"
        f"    UNIQUE(tenant_id, hostname)"
        f");"
        f"CREATE INDEX IF NOT EXISTS idx_reg_hostnames ON registered_hostnames(hostname);"
    )


async def _migrate_pg_redis_overage_fields(conn: PostgresConnection) -> None:
    """342dd15f — per-tenant Redis command budget for the send_message
    push-augmentation path.

    Mirrors db.migrations._migrate_redis_overage_fields for Postgres. Two new
    tenants columns:
      - redis_commands_used   NUMERIC(14,0)  current-month Upstash PUBLISH
                                             counter; reset monthly alongside
                                             compute_cu_hours_used.
      - redis_overage_cap_usd NUMERIC(8,2)   optional operator override; NULL
                                             means code-defined defaults apply
                                             ($1 warn / $2 disable / $4 alert).

    ADD COLUMN IF NOT EXISTS is idempotent — safe to run on every startup.
    Hosted-only (main auth DB, tenants table).
    """
    await conn.executescript(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS "
        "redis_commands_used NUMERIC(14,0) DEFAULT 0;"
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS "
        "redis_overage_cap_usd NUMERIC(8,2)"
    )


async def _migrate_pg_file_docx_region_claims(conn: PostgresConnection) -> None:
    """Create file_docx_region_claims table on existing Postgres DBs. Was
    missing from the PG schema entirely, so claim_docx_region/get_docx_region_
    claims/release_docx_region_claims 500'd on hosted with 'relation
    file_docx_region_claims does not exist'. Mirrors
    db.locks._migrate_docx_region_claims. Runs on every DB (LATE, not
    hosted-only — sessions exist on customer DBs too)."""
    await conn.executescript(
        f"CREATE TABLE IF NOT EXISTS file_docx_region_claims ("
        f"    id TEXT PRIMARY KEY,"
        f"    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,"
        f"    file_path TEXT NOT NULL,"
        f"    element_id TEXT NOT NULL,"
        f"    claimed_at TEXT NOT NULL DEFAULT ({_TS}),"
        f"    released_at TEXT"
        f");"
        f"CREATE INDEX IF NOT EXISTS idx_docx_region_claims_file ON file_docx_region_claims (file_path);"
        f"CREATE INDEX IF NOT EXISTS idx_docx_region_claims_session ON file_docx_region_claims (session_id);"
    )


async def _migrate_pg_docx_merge_manifests(conn: PostgresConnection) -> None:
    """fe989980 — wave-scoped DOCX merge manifests + serialized canonical merge gate.

    Creates docx_merge_manifests / docx_merge_drafts / docx_merge_anchor_locks
    on existing Postgres DBs. Mirrors db.docx_merge._migrate_docx_merge_manifests.
    Runs on every DB (LATE, not hosted-only — sessions exist on customer DBs
    too, same rationale as _migrate_pg_file_docx_region_claims).
    """
    await conn.executescript(
        f"CREATE TABLE IF NOT EXISTS docx_merge_manifests ("
        f"    id TEXT PRIMARY KEY,"
        f"    wave_id TEXT NOT NULL,"
        f"    file_path TEXT NOT NULL,"
        f"    status TEXT NOT NULL DEFAULT 'open',"
        f"    base_revision TEXT,"
        f"    merge_owner_session_id TEXT,"
        f"    merge_owner_claimed_at TEXT,"
        f"    merge_owner_expires_at TEXT,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS}),"
        f"    completed_at TEXT,"
        f"    verification TEXT"
        f");"
        f"CREATE UNIQUE INDEX IF NOT EXISTS idx_docx_merge_manifests_wave_file "
        f"ON docx_merge_manifests (wave_id, file_path);"
        f"CREATE TABLE IF NOT EXISTS docx_merge_drafts ("
        f"    id TEXT PRIMARY KEY,"
        f"    manifest_id TEXT NOT NULL REFERENCES docx_merge_manifests(id) ON DELETE CASCADE,"
        f"    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,"
        f"    draft_path TEXT NOT NULL,"
        f"    anchors TEXT NOT NULL DEFAULT '[]',"
        f"    declared_at TEXT NOT NULL DEFAULT ({_TS}),"
        f"    merged_at TEXT"
        f");"
        f"CREATE UNIQUE INDEX IF NOT EXISTS idx_docx_merge_drafts_manifest_session "
        f"ON docx_merge_drafts (manifest_id, session_id);"
        f"CREATE TABLE IF NOT EXISTS docx_merge_anchor_locks ("
        f"    id TEXT PRIMARY KEY,"
        f"    manifest_id TEXT NOT NULL REFERENCES docx_merge_manifests(id) ON DELETE CASCADE,"
        f"    element_id TEXT NOT NULL,"
        f"    draft_id TEXT,"
        f"    session_id TEXT NOT NULL,"
        f"    merged_at TEXT NOT NULL DEFAULT ({_TS})"
        f");"
        f"CREATE UNIQUE INDEX IF NOT EXISTS idx_docx_merge_anchor_locks_manifest_element "
        f"ON docx_merge_anchor_locks (manifest_id, element_id);"
        f"CREATE INDEX IF NOT EXISTS idx_docx_merge_anchor_locks_session "
        f"ON docx_merge_anchor_locks (session_id);"
    )


async def _migrate_pg_proposal_evidence_links(conn: PostgresConnection) -> None:
    """6cdc5df3 — proposal_evidence_links: durable, typed proposal-to-evidence
    linkage (notes/findings/sprint_items/decisions/artifacts). Mirrors
    db.proposal_links._migrate_proposal_evidence_links. Not present in the
    base CREATE_TABLES_CORE literal — this guarded migration is the only
    creation path on Postgres, matching _migrate_pg_docx_merge_manifests.

    The UNIQUE index is what makes link_proposal_evidence's
    ``ON CONFLICT ... DO NOTHING`` idempotent-insert pattern work.
    """
    await conn.executescript(
        f"CREATE TABLE IF NOT EXISTS proposal_evidence_links ("
        f"    id TEXT PRIMARY KEY,"
        f"    project_id TEXT NOT NULL,"
        f"    proposal_id TEXT NOT NULL,"
        f"    entity_type TEXT NOT NULL,"
        f"    entity_id TEXT NOT NULL,"
        f"    label TEXT,"
        f"    created_by TEXT,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS})"
        f");"
        f"CREATE UNIQUE INDEX IF NOT EXISTS idx_proposal_evidence_links_unique "
        f"ON proposal_evidence_links(project_id, proposal_id, entity_type, entity_id);"
        f"CREATE INDEX IF NOT EXISTS idx_proposal_evidence_links_proposal "
        f"ON proposal_evidence_links(project_id, proposal_id);"
        f"CREATE INDEX IF NOT EXISTS idx_proposal_evidence_links_entity "
        f"ON proposal_evidence_links(entity_type, entity_id);"
    )


async def _migrate_pg_wave_base_manifests(conn: PostgresConnection) -> None:
    """eb2e44f8 — immutable wave_base_manifests + active_worktrees.pid.

    Creates wave_base_manifests on existing Postgres DBs and adds the
    ``pid`` column to ``active_worktrees``. Mirrors
    db.worktree_manifest._migrate_wave_base_manifests. Not present in the
    base CREATE_TABLES_CORE literal — this guarded migration is the only
    creation path on Postgres, matching _migrate_pg_docx_merge_manifests.

    The partial unique index (``WHERE superseded_at IS NULL``) is the
    schema-level half of the immutability contract described in
    db.worktree_manifest's module docstring: Postgres supports partial
    indexes with identical syntax to SQLite, so this is a straight mirror,
    not a workaround.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS wave_base_manifests ("
        "    id TEXT PRIMARY KEY,"
        "    worktree_id TEXT NOT NULL REFERENCES active_worktrees(id),"
        "    project_id TEXT NOT NULL REFERENCES projects(id),"
        "    session_id TEXT NOT NULL,"
        "    item_id TEXT,"
        "    repo_identity TEXT NOT NULL,"
        "    base_branch TEXT NOT NULL,"
        "    base_sha TEXT NOT NULL,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS}),"
        "    superseded_at TEXT,"
        "    superseded_reason TEXT"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_wave_base_manifests_worktree "
        "ON wave_base_manifests(worktree_id);"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_wave_base_manifests_active "
        "ON wave_base_manifests(worktree_id) WHERE superseded_at IS NULL;"
        "ALTER TABLE active_worktrees ADD COLUMN IF NOT EXISTS pid INTEGER"
    )


async def _migrate_pg_pixi_env_roots(conn: PostgresConnection) -> None:
    """15610335 — external Pixi detached-environment registry, per worktree.

    Creates pixi_env_roots on existing Postgres DBs. Mirrors
    db.worktrees._migrate_pixi_env_roots. Not present in the base
    CREATE_TABLES_CORE literal — this guarded migration is the only
    creation path on Postgres, matching _migrate_pg_wave_base_manifests.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS pixi_env_roots ("
        "    id TEXT PRIMARY KEY,"
        "    worktree_id TEXT NOT NULL REFERENCES active_worktrees(id),"
        "    project_id TEXT NOT NULL REFERENCES projects(id),"
        "    root_path TEXT NOT NULL,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS}),"
        "    reclaimed_at TEXT"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_pixi_env_roots_worktree "
        "ON pixi_env_roots(worktree_id);"
        "CREATE INDEX IF NOT EXISTS idx_pixi_env_roots_project "
        "ON pixi_env_roots(project_id, reclaimed_at);"
    )


async def _migrate_pg_sprint_batch_claims(conn: PostgresConnection) -> None:
    """22cad9b8 — immutable sprint_batch_claims: atomic parallel-batch claim
    manifests, mirroring wave_base_manifests' immutability pattern.

    Creates sprint_batch_claims on existing Postgres DBs. Not present in the
    base CREATE_TABLES_CORE literal — this guarded migration is the only
    creation path on Postgres, matching _migrate_pg_wave_base_manifests.

    The partial unique index (``WHERE superseded_at IS NULL``) is the
    schema-level half of the immutability contract described in
    db.batch_claim's module docstring: only one ACTIVE manifest may exist
    per (project_id, batch_key) at a time.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS sprint_batch_claims ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL REFERENCES projects(id),"
        "    session_id TEXT NOT NULL,"
        "    batch_key TEXT NOT NULL,"
        "    item_ids TEXT NOT NULL,"
        "    item_resource_map TEXT NOT NULL,"
        "    resources TEXT NOT NULL,"
        "    status TEXT NOT NULL DEFAULT 'pending',"
        "    failure_detail TEXT,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS}),"
        "    resolved_at TEXT,"
        "    superseded_at TEXT,"
        "    superseded_reason TEXT"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_sprint_batch_claims_project "
        "ON sprint_batch_claims(project_id);"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sprint_batch_claims_active "
        "ON sprint_batch_claims(project_id, batch_key) WHERE superseded_at IS NULL;"
    )


async def _migrate_pg_sprint_batch_claims_reservation_fields(conn: PostgresConnection) -> None:
    """704edefe — extend sprint_batch_claims (Postgres) with the
    reservation/integration-queue columns. Mirrors meridian.db.batch_claim.
    _migrate_sprint_batch_claims_reservation_fields exactly; see that
    docstring for the full per-column contract (resolved_symbols,
    dependency_frontier, expected_outputs, verifier_class,
    integration_order). Postgres supports ADD COLUMN IF NOT EXISTS
    natively, so this is a single idempotent statement per column — no
    catalog probe needed, unlike the SQLite PRAGMA table_info approach."""
    await conn.executescript(
        "ALTER TABLE sprint_batch_claims ADD COLUMN IF NOT EXISTS resolved_symbols TEXT;"
        "ALTER TABLE sprint_batch_claims ADD COLUMN IF NOT EXISTS dependency_frontier TEXT;"
        "ALTER TABLE sprint_batch_claims ADD COLUMN IF NOT EXISTS expected_outputs TEXT;"
        "ALTER TABLE sprint_batch_claims ADD COLUMN IF NOT EXISTS verifier_class TEXT;"
        "ALTER TABLE sprint_batch_claims ADD COLUMN IF NOT EXISTS integration_order TEXT;"
    )


async def _migrate_pg_verification_runs(conn: PostgresConnection) -> None:
    """525d86bb — verification_runs: durable synchronous run_verification
    lifecycle records (mirrors SQLite).

    One row per run_verification dispatch: created with status='running'
    before the command is sent over the tunnel, completed exactly once (by
    db.verification_runs.complete_verification_run) with the REAL
    exit_code/status/log artifact right after the synchronous
    send_run_cmd_control wait resolves — no other writer, no polling.
    Mirrors db.verification_runs._migrate_verification_runs.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS verification_runs ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL REFERENCES projects(id),"
        "    command TEXT NOT NULL,"
        "    cwd TEXT,"
        "    worktree TEXT,"
        "    actor TEXT,"
        "    status TEXT NOT NULL DEFAULT 'running',"
        "    exit_code INTEGER,"
        "    passed INTEGER,"
        "    failed INTEGER,"
        "    stdout_tail TEXT,"
        "    stderr_tail TEXT,"
        "    message TEXT,"
        f"    started_at TEXT NOT NULL DEFAULT ({_TS}),"
        "    ended_at TEXT"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_verification_runs_project "
        "ON verification_runs(project_id, started_at DESC);"
    )


async def _migrate_pg_sprint_version_descriptions(conn: PostgresConnection) -> None:
    """f9188526 — sprint_version_descriptions: per-version-bucket summary text.

    Creates the sprint_version_descriptions table on existing Postgres DBs.
    Each (project_id, version) pair carries an auto-generated concise description
    summarising what that sprint bucket is about. Seeded and refreshed by
    add_sprint_item in sprint_items.py. CREATE TABLE / CREATE UNIQUE INDEX IF NOT
    EXISTS → idempotent. Mirrors db.migrations._migrate_sprint_version_descriptions.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS sprint_version_descriptions ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,"
        "    version TEXT NOT NULL,"
        "    description TEXT NOT NULL,"
        f"    updated_at TEXT NOT NULL DEFAULT ({_TS})"
        ");"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sprint_version_desc_pv "
        "ON sprint_version_descriptions(project_id, version)"
    )


async def _migrate_pg_workspace_settings_active_session_threshold(
    conn: PostgresConnection,
) -> None:
    """6e0e5cea — configurable active-executor-session warning threshold.

    Adds ``active_session_warning_minutes`` to workspace_settings on existing
    Postgres DBs. Default 10 matches the previously hardcoded 600-second constant
    used by _active_executor_session_warnings in handler.py.

    Mirrors db.migrations._migrate_workspace_settings_active_session_threshold.
    """
    await conn.executescript(
        "ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS "
        "active_session_warning_minutes INTEGER NOT NULL DEFAULT 10"
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
    _migrate_pg_workspace_members_project_scope,
    _migrate_pg_admin_plan,
    _migrate_pg_tunnel_active,
    _migrate_pg_tunnel_plugins,
    _migrate_pg_tunnel_plugins_by_host,
    _migrate_pg_feedback,
    _migrate_pg_registered_hostnames,
    _migrate_pg_redis_overage_fields,
)

async def _migrate_pg_decision_code_anchor(conn: PostgresConnection) -> None:
    """777f26b0 — decisions_pinned.code_anchor: optional file path anchor.

    When set, get_decisions_for_file surfaces this decision automatically when
    an executor calls claim_file for the matching path. Nullable so existing
    decisions are unaffected. ADD COLUMN IF NOT EXISTS so re-running is a no-op.
    Mirrors db._migrate_decision_code_anchor.
    """
    await conn.executescript(
        "ALTER TABLE decisions_pinned ADD COLUMN IF NOT EXISTS code_anchor TEXT"
    )


async def _migrate_pg_decision_assumption(conn: PostgresConnection) -> None:
    """2b39549d — decisions_pinned.assumption + assumption_status.

    Records the unverified assumption a decision rests on and its validation
    state (unvalidated|confirmed|invalidated). Both nullable. ADD COLUMN IF NOT
    EXISTS so re-running is a no-op. Mirrors db._migrate_decision_assumption.
    """
    await conn.executescript(
        "ALTER TABLE decisions_pinned ADD COLUMN IF NOT EXISTS assumption TEXT; "
        "ALTER TABLE decisions_pinned ADD COLUMN IF NOT EXISTS assumption_status TEXT;"
    )


async def _migrate_pg_session_graph_snapshots(conn: PostgresConnection) -> None:
    """f773a99a — session_graph_snapshots: per-session code-graph metric snapshots.

    Lightweight proxy metrics (node/edge/hotspot/churn counts) computed from
    file_symbol_claims and task_log. Used by get_graph_diff to compare two
    sessions' graph impact. CREATE TABLE IF NOT EXISTS so re-running is a no-op.
    Mirrors db._migrate_session_graph_snapshots.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS session_graph_snapshots ("
        "    id TEXT PRIMARY KEY,"
        "    session_id TEXT NOT NULL,"
        "    project_id TEXT NOT NULL,"
        "    snapshot_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US')),"
        "    node_count INTEGER NOT NULL DEFAULT 0,"
        "    edge_count INTEGER NOT NULL DEFAULT 0,"
        "    hotspot_count INTEGER NOT NULL DEFAULT 0,"
        "    file_churn INTEGER NOT NULL DEFAULT 0,"
        "    metrics_json TEXT"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_session_graph_snapshots_session "
        "ON session_graph_snapshots(session_id);"
    )


async def _migrate_pg_touches_resources(conn: PostgresConnection) -> None:
    """501ec93f — sprint_items.touches_resources: typed resource identifiers.

    JSON list generalizing touches_files. Nullable; ADD COLUMN IF NOT EXISTS so
    re-running is a no-op. Mirrors db._migrate_touches_resources.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS touches_resources TEXT"
    )


async def _migrate_pg_sprint_item_stall_count(conn: PostgresConnection) -> None:
    """bc9259b8 — sprint_items.stall_count: worker stall auto-retry counter.

    ADD COLUMN IF NOT EXISTS so re-running is a no-op. Mirrors
    db._migrate_sprint_item_stall_count.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS stall_count INTEGER NOT NULL DEFAULT 0"
    )


async def _migrate_pg_resource_locks(conn: PostgresConnection) -> None:
    """501ec93f — resource_locks: generalized typed-resource lock on existing PG DBs.

    CREATE TABLE IF NOT EXISTS so re-running is a no-op. Mirrors
    db._migrate_resource_locks.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS resource_locks ("
        "    id TEXT PRIMARY KEY,"
        "    resource_id TEXT NOT NULL UNIQUE,"
        "    resource_type TEXT NOT NULL,"
        "    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,"
        f"    claimed_at TEXT NOT NULL DEFAULT ({_TS}),"
        "    expires_at TEXT NOT NULL"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_resource_locks_session ON resource_locks(session_id);"
        "CREATE INDEX IF NOT EXISTS idx_resource_locks_expires ON resource_locks(expires_at);"
        "CREATE INDEX IF NOT EXISTS idx_resource_locks_type ON resource_locks(resource_type)"
    )


async def _migrate_pg_github_connections(conn: PostgresConnection) -> None:
    """0b061f45 — multi-account GitHub OAuth.

    ``github_connections`` stores N encrypted PATs per tenant keyed by
    account_login. ``projects.github_account_login`` pins a specific account.
    Idempotent (CREATE … IF NOT EXISTS / ADD COLUMN IF NOT EXISTS).
    Mirrors db._migrate_github_connections.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS github_connections ("
        "    id TEXT PRIMARY KEY,"
        "    tenant_id TEXT NOT NULL,"
        "    account_login TEXT NOT NULL,"
        "    token TEXT NOT NULL,"
        "    scope TEXT,"
        f"    connected_at TEXT NOT NULL DEFAULT ({_TS}),"
        "    UNIQUE(tenant_id, account_login)"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_github_connections_tenant "
        "ON github_connections(tenant_id);"
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS github_account_login TEXT"
    )


async def _migrate_pg_blog_posts(conn: PostgresConnection) -> None:
    """6234f9b8 — blog_posts table for the admin Blog CMS.

    Was added to the SQLite schema only; Postgres DBs 500'd on /admin/blog/posts
    with 'relation blog_posts does not exist'. CREATE IF NOT EXISTS so re-running
    is a no-op. Mirrors db._migrate_blog_posts.
    """
    await conn.executescript(
        f"CREATE TABLE IF NOT EXISTS blog_posts ("
        f"    id TEXT PRIMARY KEY,"
        f"    title TEXT NOT NULL,"
        f"    slug TEXT NOT NULL UNIQUE,"
        f"    body_md TEXT NOT NULL DEFAULT '',"
        f"    status TEXT NOT NULL DEFAULT 'draft',"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS}),"
        f"    updated_at TEXT NOT NULL DEFAULT ({_TS}),"
        f"    published_at TEXT"
        f");"
        f"CREATE INDEX IF NOT EXISTS idx_blog_posts_status ON blog_posts(status)"
    )


async def _migrate_pg_blog_posts_tenant(conn: PostgresConnection) -> None:
    """8843250f — workspace-scope the blog: add a nullable ``tenant_id`` to
    ``blog_posts`` + an index on it. Idempotent (ADD COLUMN IF NOT EXISTS).
    Mirrors db._migrate_blog_posts_tenant. The 'archived' lifecycle status is
    enforced at the app layer (the PG blog_posts table has no status CHECK)."""
    await conn.executescript(
        "ALTER TABLE blog_posts ADD COLUMN IF NOT EXISTS tenant_id TEXT;"
        "CREATE INDEX IF NOT EXISTS idx_blog_posts_tenant ON blog_posts(tenant_id)"
    )


async def _migrate_pg_project_parent_id(conn: PostgresConnection) -> None:
    """3b6ff466 — projects.parent_project_id: one-level-deep subprojects.

    Nullable self-reference; NULL means a top-level project. The one-level
    depth rule and parent-exists check are enforced at the app layer
    (db.create_project). The north_star fall-back to the parent lives in
    db.get_goal so every read-path (get_goal / get_planning_brief /
    get_context_block) inherits it. Idempotent (ADD COLUMN IF NOT EXISTS).
    The index lives here, never inline in CREATE_TABLES_CORE, to avoid the
    unguarded-index boot crash on a projects table predating the column.
    Mirrors db._migrate_project_parent_id."""
    await conn.executescript(
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS parent_project_id TEXT;"
        "CREATE INDEX IF NOT EXISTS idx_projects_parent ON projects(parent_project_id)"
    )


async def _migrate_pg_decision_evidence(conn: PostgresConnection) -> None:
    """9149e132 — decision_evidence: typed, code-linked decision evidence
    (mirrors db.decision_evidence._migrate_decision_evidence).

    One row per typed evidence link: a decision_id, a durable pointer (JSON,
    the same meridian.pointers shape sprint_item_pointers already uses),
    searchable evidence text, optional assumptions/applicability_scope/
    confidence, and a supersession/reversal lineage (status +
    supersedes_id/superseded_by/reversal_reason) — nothing is ever hard
    deleted. Wired into planning_search via the
    _PLANNING_SOURCE_SPECS["decision_evidence"] entry in db/__init__.py, the
    same generic lexical-only retrieval path every other source type uses.
    CREATE TABLE / INDEX IF NOT EXISTS so re-running is a no-op.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS decision_evidence ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL,"
        "    decision_id TEXT NOT NULL,"
        "    version TEXT,"
        "    pointer TEXT NOT NULL,"
        "    evidence TEXT NOT NULL,"
        "    assumptions TEXT,"
        "    applicability_scope TEXT,"
        "    confidence REAL,"
        "    status TEXT NOT NULL DEFAULT 'active',"
        "    supersedes_id TEXT,"
        "    superseded_by TEXT,"
        "    reversal_reason TEXT,"
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US')),"
        "    updated_at TEXT"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_decision_evidence_decision "
        "ON decision_evidence(decision_id);"
        "CREATE INDEX IF NOT EXISTS idx_decision_evidence_project "
        "ON decision_evidence(project_id);"
    )


async def _migrate_pg_ai_log_events(conn: PostgresConnection) -> None:
    """9e83be4a (Round 1 proposal e143949d) — ai_log_events: canonical,
    versioned, append-only ExecutionEvent storage (mirrors
    db.ai_log._migrate_ai_log_events_table — see meridian.ai_log's module
    docstring for the full envelope/versioning rationale).

    Schema/contract scaffold only — nothing in this codebase calls
    append_event yet (no capture/ingestion pipeline wired to this table).
    ``recorded_at`` defaults via ``_TS`` (clock_timestamp()-based), not
    ``now()`` — this repo's now()-vs-clock_timestamp() note (AGENTS.md /
    project memory) applies to any multi-row-per-transaction insert
    sequence, and this table is written one row per append_event call, so
    using the already-fixed ``_TS`` expression here is simply staying
    consistent with the newer tables (executor_reports, wave_run_summaries)
    rather than the older ``now()``-based ones.

    CREATE TABLE / INDEX IF NOT EXISTS so re-running is a no-op.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS ai_log_events ("
        "    id TEXT PRIMARY KEY,"
        "    schema_version INTEGER NOT NULL,"
        "    event_type TEXT NOT NULL,"
        "    project_id TEXT NOT NULL,"
        "    session_id TEXT,"
        "    tenant_id TEXT,"
        "    actor_kind TEXT NOT NULL,"
        "    actor_id TEXT,"
        "    correlation_id TEXT,"
        "    parent_event_id TEXT,"
        "    source TEXT,"
        "    payload TEXT NOT NULL DEFAULT '{}',"
        "    payload_schema TEXT,"
        "    occurred_at TEXT NOT NULL,"
        "    idempotency_key TEXT,"
        "    event_hash TEXT,"
        f"    recorded_at TEXT NOT NULL DEFAULT ({_TS})"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_ai_log_events_project "
        "ON ai_log_events(project_id, recorded_at DESC);"
        "CREATE INDEX IF NOT EXISTS idx_ai_log_events_session "
        "ON ai_log_events(session_id, recorded_at DESC);"
        "CREATE INDEX IF NOT EXISTS idx_ai_log_events_type "
        "ON ai_log_events(project_id, event_type, recorded_at DESC);"
        "CREATE INDEX IF NOT EXISTS idx_ai_log_events_correlation "
        "ON ai_log_events(correlation_id);"
        "CREATE INDEX IF NOT EXISTS idx_ai_log_events_parent "
        "ON ai_log_events(parent_event_id);"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_log_events_idempotency "
        "ON ai_log_events(project_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL;"
    )


async def _migrate_pg_proposal_intake_drafts(conn: PostgresConnection) -> None:
    """3f892ea6 — proposal_intake_drafts: one row per parsed, non-code
    deterministic proposal-intake block (mirrors
    db.workspace._migrate_proposal_intake_drafts). Not present in the base
    CREATE_TABLES_CORE literal — this guarded migration is the only creation
    path on Postgres, matching _migrate_pg_proposal_evidence_links.

    The UNIQUE index on (proposal_id, block_id) is what makes
    ingest_proposal_intake's upsert-by-block-position logic correct.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS proposal_intake_drafts ("
        "    id TEXT PRIMARY KEY,"
        "    proposal_id TEXT NOT NULL,"
        "    tenant_id TEXT,"
        "    block_id TEXT NOT NULL,"
        "    position INTEGER NOT NULL,"
        "    intake_key TEXT NOT NULL,"
        "    text TEXT NOT NULL,"
        "    source_hash TEXT NOT NULL,"
        "    route TEXT,"
        "    candidate_ids TEXT NOT NULL DEFAULT '[]',"
        "    is_code INTEGER NOT NULL DEFAULT 0,"
        "    is_duplicate INTEGER NOT NULL DEFAULT 0,"
        "    duplicate_of_block_id TEXT,"
        "    revision INTEGER NOT NULL DEFAULT 1,"
        "    history TEXT NOT NULL DEFAULT '[]',"
        "    status TEXT NOT NULL DEFAULT 'draft',"
        "    line_start INTEGER,"
        "    line_end INTEGER,"
        "    promoted_to_sprint_item_id TEXT,"
        "    promoted_to_project_id TEXT,"
        "    promoted_at TEXT,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS}),"
        f"    updated_at TEXT NOT NULL DEFAULT ({_TS})"
        ");"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_proposal_intake_drafts_block "
        "ON proposal_intake_drafts(proposal_id, block_id);"
        "CREATE INDEX IF NOT EXISTS idx_proposal_intake_drafts_position "
        "ON proposal_intake_drafts(proposal_id, position);"
        "CREATE INDEX IF NOT EXISTS idx_proposal_intake_drafts_promoted "
        "ON proposal_intake_drafts(promoted_to_sprint_item_id);"
    )


async def _migrate_pg_session_goal_compliance(conn: PostgresConnection) -> None:
    """5abf3e12 — sessions.goal_compliance: stored per-session goal-compliance
    metric (JSON: listed N vs completed M vs fully_completed).

    Written at generate_handoff by db.compute_session_goal_compliance. Nullable;
    no index (read only by the session's primary key). Idempotent
    (ADD COLUMN IF NOT EXISTS). Mirrors db._migrate_session_goal_compliance.
    """
    await conn.executescript(
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS goal_compliance TEXT"
    )


async def _migrate_pg_sprint_item_pointers(conn: PostgresConnection) -> None:
    """2976e168 — sprint_item_pointers: the GENERIC POINTER PRIMITIVE (mirrors SQLite).

    ONE table for pointers of ANY source_type, keyed to a sprint item; ``targets``
    is a JSON array of {uri, selector, subSelector?} (composite shape stored as
    JSON, NOT per-domain columns). CREATE_TABLES_CORE covers fresh DBs; this is
    the upgrade path. The index lives here, never inline in CREATE_TABLES_CORE, to
    avoid the unguarded-index boot crash. Idempotent. Mirrors
    db._migrate_sprint_item_pointers.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS sprint_item_pointers ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL,"
        "    sprint_item_id TEXT NOT NULL,"
        "    source_type TEXT NOT NULL,"
        "    targets TEXT NOT NULL,"
        "    label TEXT,"
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US'))"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_sprint_item_pointers_item "
        "ON sprint_item_pointers(sprint_item_id);"
    )


async def _migrate_pg_sprint_item_deferral(conn: PostgresConnection) -> None:
    """dec69708 — ENFORCED deferral for sprint items (mirrors SQLite).

    Adds nullable ``deferred_until`` (ISO timestamp) and ``track`` columns to
    ``sprint_items``. While ``deferred_until`` is in the future,
    claim_sprint_item REFUSES the item — a structural block, not a text-only
    pinned decision. CREATE_TABLES_CORE covers fresh DBs; this is the upgrade
    path. ADD COLUMN IF NOT EXISTS → idempotent. Mirrors
    db._migrate_sprint_item_deferral.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS deferred_until TEXT;"
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS track TEXT"
    )


async def _migrate_pg_sprint_item_priority_blocker(conn: PostgresConnection) -> None:
    """e08fee30 + 2282a636 — priority + blocker_kind on sprint_items (mirrors SQLite).

    ``priority`` — app-layer enum {urgent, high, normal, low}, NOT NULL DEFAULT
    'normal'; higher-priority pending items are surfaced/claimed/grouped first.
    ``blocker_kind`` — nullable; NULL = ordinary, 'manual' = blocked on a
    real-world action outside Meridian (distinct from milestone_type='human', and
    excluded from executor scoping the same way). f89d440f — 'superseded' =
    item's premise replaced by other work; hard-blocked at claim_sprint_item
    itself (not just a listing exclusion like 'manual'). Enums enforced at the
    app layer (see ``_VALID_SPRINT_BLOCKER_KINDS`` in db/sprint_items.py).
    CREATE_TABLES_CORE covers fresh DBs; this is the upgrade path.
    ADD COLUMN IF NOT EXISTS → idempotent. Mirrors
    db._migrate_sprint_item_priority_blocker.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS priority TEXT NOT NULL DEFAULT 'normal';"
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS blocker_kind TEXT"
    )


async def _migrate_pg_sprint_item_wave(conn: PostgresConnection) -> None:
    """58a45b92 — stored wave label on sprint_items (mirrors SQLite).

    Nullable TEXT (e.g. 'wave-1'); NULL = unassigned. CREATE_TABLES_CORE covers
    fresh DBs; this is the upgrade path. ADD COLUMN IF NOT EXISTS → idempotent.
    Mirrors db._migrate_sprint_item_wave.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS wave TEXT"
    )


async def _migrate_pg_sprint_item_dependency(conn: PostgresConnection) -> None:
    """b01326e9 / v2.6 — dependency + file-conflict columns on sprint_items.

    Backfills the sprint_items columns that had a SQLite migration but no
    Postgres upgrade path, so existing prod DBs (where CREATE TABLE IF NOT
    EXISTS is a no-op) get patched:

      depends_on    — sibling sprint item id this one depends on (NULL = none).
      failure_mode  — behaviour when depends_on has failed: 'continue' (default,
                      still claimable) or 'stop' (blocked). NOT NULL DEFAULT.
      touches_files — file-conflict tracking (predates touches_resources).

    CREATE_TABLES_CORE covers fresh DBs; this is the upgrade path. ADD COLUMN
    IF NOT EXISTS → idempotent. Mirrors db._migrate_sprint_item_dependency /
    _migrate_touches_files.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS depends_on TEXT;"
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS failure_mode TEXT NOT NULL DEFAULT 'continue';"
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS touches_files TEXT"
    )


async def _migrate_pg_mcp_rate_counters(conn: PostgresConnection) -> None:
    """3295c784 — mcp_rate_counters: cross-instance shared hit-counting for the
    consolidated /mcp tenant-tier rate limiter (mirrors SQLite).

    The per-process ``_tenant_rl_hits`` dict only counts requests on one Fly
    machine, so across N machines the effective limit is ~Nx intended. This
    windowed counter keeps one atomic count per (tenant_id, epoch-minute window)
    so every instance shares a single total. Gated by MERIDIAN_SHARED_RATE_LIMIT
    (default OFF — prod behavior unchanged until opted in).

    CREATE_TABLES_CORE covers fresh DBs; this is the upgrade path.
    CREATE TABLE / CREATE INDEX IF NOT EXISTS → idempotent. The extra index on
    ``window_start`` (for the opportunistic prune) lives here, never inline in
    the base literal (guarded-migration rule). Mirrors
    db._migrate_mcp_rate_counters.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS mcp_rate_counters ("
        "    tenant_id TEXT NOT NULL,"
        "    window_start BIGINT NOT NULL,"
        "    count INTEGER NOT NULL DEFAULT 0,"
        "    PRIMARY KEY (tenant_id, window_start)"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_mcp_rate_counters_window "
        "ON mcp_rate_counters(window_start)"
    )


async def _migrate_pg_workspace_proposals(conn: PostgresConnection) -> None:
    """5c4dcc0f — workspace_proposals: human-only "drawer of inspiration".

    Workspace-scoped (tenant_id, not project_id) table for cross-project flashes
    of insight. status: raw → investigating → promoted | rejected.
    promoted_to_sprint_item_id links a promoted proposal to its sprint item.
    NOT auto-claimable by executors. CREATE_TABLES_CORE covers fresh DBs; this
    is the upgrade path. Mirrors db._migrate_workspace_proposals.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS workspace_proposals ("
        "    id TEXT PRIMARY KEY,"
        "    title TEXT NOT NULL,"
        "    body TEXT NOT NULL,"
        "    tags TEXT,"
        "    status TEXT NOT NULL DEFAULT 'raw',"
        "    promoted_to_sprint_item_id TEXT,"
        "    tenant_id TEXT,"
        "    family_id TEXT,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS}),"
        f"    updated_at TEXT NOT NULL DEFAULT ({_TS}),"
        f"    last_activity_at TEXT NOT NULL DEFAULT ({_TS})"
        ");"
        "ALTER TABLE workspace_proposals ADD COLUMN IF NOT EXISTS family_id TEXT;"
        # 595126d (2026-08-05 hotfix) -- _TS returns a formatted TEXT value,
        # matching created_at/updated_at and the SQLite schema. Using
        # TIMESTAMPTZ here makes the entire migration roll back because
        # PostgreSQL cannot use to_char(...) as a timestamptz default,
        # leaving legacy production databases without the column that
        # proposal reads require.
        f"ALTER TABLE workspace_proposals ADD COLUMN IF NOT EXISTS last_activity_at TEXT NOT NULL DEFAULT ({_TS});"
        "UPDATE workspace_proposals SET last_activity_at = COALESCE(last_activity_at, created_at);"
        "ALTER TABLE workspace_proposals ADD COLUMN IF NOT EXISTS "
        "created_seq BIGSERIAL;"
        "CREATE INDEX IF NOT EXISTS idx_workspace_proposals_tenant "
        "ON workspace_proposals(tenant_id)"
        ";"
        "CREATE INDEX IF NOT EXISTS idx_workspace_proposals_activity "
        "ON workspace_proposals(tenant_id, last_activity_at, created_seq);"
        "CREATE INDEX IF NOT EXISTS idx_workspace_proposals_family "
        "ON workspace_proposals(tenant_id, family_id);"
        # 867317f6 -- idempotency_key: optional caller-supplied dedup key so
        # add_workspace_proposal is safe to retry. Scoped by
        # COALESCE(tenant_id, '') rather than raw tenant_id so a self-host
        # DB (tenant_id always NULL) still gets real duplicate-prevention --
        # plain NULLs are never equal under a UNIQUE index/constraint on
        # either backend. Mirrors db.migrations._migrate_workspace_proposals.
        "ALTER TABLE workspace_proposals ADD COLUMN IF NOT EXISTS idempotency_key TEXT;"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_proposals_idempotency "
        "ON workspace_proposals(COALESCE(tenant_id, ''), idempotency_key) "
        "WHERE idempotency_key IS NOT NULL;"
        "CREATE TABLE IF NOT EXISTS proposal_events ("
        "    id TEXT PRIMARY KEY,"
        "    proposal_id TEXT NOT NULL,"
        "    tenant_id TEXT,"
        "    sequence INTEGER NOT NULL,"
        "    event_type TEXT NOT NULL,"
        "    content TEXT NOT NULL DEFAULT '',"
        "    payload TEXT,"
        "    actor TEXT,"
        "    session_id TEXT,"
        "    source TEXT,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS})"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_proposal_events_proposal_sequence "
        "ON proposal_events(proposal_id, sequence);"
        "CREATE INDEX IF NOT EXISTS idx_proposal_events_tenant_created_at "
        "ON proposal_events(tenant_id, created_at)"
    )


async def _migrate_pg_pending_goal_at(conn: PostgresConnection) -> None:
    """590dcdd5 — projects.pending_goal_at: ISO-8601 UTC timestamp written
    alongside pending_goal by set_pending_goal so pop_pending_goal_with_meta
    can flag goals older than PENDING_GOAL_STALE_HOURS as possibly-stale.
    Mirrors db._migrate_pending_goal_at."""
    await conn.executescript(
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS pending_goal_at TEXT"
    )


async def _migrate_pg_file_patch_counters(conn: PostgresConnection) -> None:
    """356d6ac8 — file_patch_counters: structural-degradation early-warning signal.

    Tracks per-(session, file) write-claim counts within a session so
    get_structural_degradation_warnings can flag files patched N times without a
    deliberate refactor (refactor_flagged). Idempotent (CREATE TABLE IF NOT EXISTS
    + CREATE INDEX IF NOT EXISTS). Mirrors db._migrate_file_patch_counters.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS file_patch_counters ("
        "    id TEXT PRIMARY KEY,"
        "    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,"
        "    file_path TEXT NOT NULL,"
        "    patch_count INTEGER NOT NULL DEFAULT 0,"
        "    refactor_flagged INTEGER NOT NULL DEFAULT 0,"
        f"   first_patched_at TEXT NOT NULL DEFAULT ({_TS}),"
        f"   last_patched_at TEXT NOT NULL DEFAULT ({_TS}),"
        "    UNIQUE (session_id, file_path)"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_file_patch_counters_session "
        "ON file_patch_counters(session_id)"
    )


async def _migrate_pg_sprint_item_resources_amended(conn: PostgresConnection) -> None:
    """2593a5fe — resources_amended flag on sprint_items (mirrors SQLite).

    Nullable INTEGER default 0; set to 1 when claim_file/claim_symbol appends
    a post-declaration resource to the item's touches_resources (mid-execution
    pivot). ADD COLUMN IF NOT EXISTS → idempotent.
    Mirrors db._migrate_sprint_item_resources_amended.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS resources_amended INTEGER DEFAULT 0"
    )


async def _migrate_pg_session_activity(conn: PostgresConnection) -> None:
    """8c147109 — session_activity: lightweight ring-buffer heartbeat feed.

    Creates the session_activity table so a remote planner can see signs of
    life in an executor session even before the executor calls log_task().
    Mirrors db._migrate_session_activity. Idempotent via CREATE TABLE IF NOT
    EXISTS + CREATE INDEX IF NOT EXISTS.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS session_activity ("
        "    id TEXT PRIMARY KEY,"
        "    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,"
        "    tool_name TEXT NOT NULL,"
        "    summary TEXT NOT NULL,"
        f"    recorded_at TEXT NOT NULL DEFAULT ({_TS})"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_session_activity_session "
        "ON session_activity(session_id, recorded_at DESC)"
    )


async def _migrate_pg_connection_events(conn: PostgresConnection) -> None:
    """b12cc29f — connection_events: per-/mcp-request auth+method event log.

    Every HTTP POST /mcp the server actually receives is recorded here so a
    live or post-mortem client-side outage (Claude Desktop showing zero tools,
    auth failures) can be diagnosed without raw Fly.io log access.
    Mirrors db._migrate_connection_events. Idempotent via CREATE TABLE IF NOT
    EXISTS + CREATE INDEX IF NOT EXISTS.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS connection_events ("
        "    id TEXT PRIMARY KEY,"
        "    tenant_id TEXT,"
        "    method TEXT NOT NULL DEFAULT '',"
        "    auth_result TEXT NOT NULL DEFAULT 'unknown',"
        "    tools_returned INTEGER,"
        "    client_user_agent TEXT,"
        "    response_status INTEGER NOT NULL DEFAULT 200,"
        f"    recorded_at TEXT NOT NULL DEFAULT ({_TS})"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_connection_events_tenant "
        "ON connection_events(tenant_id, recorded_at DESC)"
    )


async def _migrate_pg_sprint_item_sprint_name(conn: PostgresConnection) -> None:
    """3d6bd938 — separate human-readable sprint name from the structural version field.

    version stays a semver-like structural identifier (e.g. 'v0.2.x');
    sprint_name is a nullable free-text label for the bucket (e.g.
    'docs-cloudflare'). ADD COLUMN IF NOT EXISTS -> idempotent.
    Mirrors db._migrate_sprint_item_sprint_name.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS sprint_name TEXT"
    )


async def _migrate_pg_proposal_slug_nickname(conn: PostgresConnection) -> None:
    """6fb48898 — workspace_proposals.slug + .nickname: human-referenceable
    secondary keys derived from the proposal title at creation time.

    Mirrors db._migrate_proposal_slug_nickname. ADD COLUMN IF NOT EXISTS is
    idempotent; both columns are nullable so existing rows are unaffected.
    """
    await conn.executescript(
        "ALTER TABLE workspace_proposals ADD COLUMN IF NOT EXISTS slug TEXT;"
        "ALTER TABLE workspace_proposals ADD COLUMN IF NOT EXISTS nickname TEXT"
    )


async def _migrate_pg_decision_slug_nickname(conn: PostgresConnection) -> None:
    """6fb48898 — decisions_pinned.slug + .nickname: human-referenceable
    secondary keys derived from the decision title at creation time.

    Mirrors db._migrate_decision_slug_nickname. ADD COLUMN IF NOT EXISTS is
    idempotent; both columns are nullable so existing rows are unaffected.
    """
    await conn.executescript(
        "ALTER TABLE decisions_pinned ADD COLUMN IF NOT EXISTS slug TEXT;"
        "ALTER TABLE decisions_pinned ADD COLUMN IF NOT EXISTS nickname TEXT"
    )


async def _migrate_pg_note_nickname(conn: PostgresConnection) -> None:
    """6fb48898 — project_notes.nickname: short memorable secondary key to
    complement the existing slug column.

    project_notes already has slug (added by _migrate_pg_note_slug). This adds
    the companion nickname column matching the sprint_items pattern.

    Mirrors db._migrate_note_nickname. ADD COLUMN IF NOT EXISTS is idempotent;
    nullable so existing rows are unaffected.
    """
    await conn.executescript(
        "ALTER TABLE project_notes ADD COLUMN IF NOT EXISTS nickname TEXT"
    )


async def _migrate_pg_wave_gate_results(conn: PostgresConnection) -> None:
    """d2430713 — wave_gate_results: persist complete_wave_gate evidence records.

    Stores the verified run_verification payload when an executor calls
    complete_wave_gate after successfully running a wave's gate action list.
    One row per (project_id, wave_label) pair — UNIQUE constraint enforces
    that each wave gate can only be completed once.

    Mirrors db._migrate_wave_gate_results. Idempotent via CREATE TABLE IF NOT
    EXISTS + CREATE INDEX IF NOT EXISTS.

    ed8e4524 — added nullable ``version`` (sprint-version scope; NULL =
    unscoped/legacy) via ``ADD COLUMN IF NOT EXISTS`` for a table that
    predates this fix. Same residual-constraint note as the SQLite mirror
    (db.migrations._migrate_wave_gate_results): the UNIQUE constraint on an
    already-existing table is not retroactively widened to include version.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS wave_gate_results ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL,"
        "    wave_label TEXT NOT NULL,"
        "    version TEXT,"
        "    gate_passed INTEGER NOT NULL DEFAULT 1,"
        "    exit_code INTEGER,"
        "    passed_count INTEGER,"
        "    failed_count INTEGER,"
        "    verification_status TEXT,"
        "    evidence_snapshot TEXT,"
        "    actor TEXT,"
        "    completed_at TEXT NOT NULL DEFAULT (now()::text),"
        "    UNIQUE(project_id, wave_label, version)"
        ");"
        "ALTER TABLE wave_gate_results ADD COLUMN IF NOT EXISTS version TEXT;"
        "CREATE INDEX IF NOT EXISTS idx_wave_gate_results_project "
        "ON wave_gate_results(project_id, wave_label);"
        "CREATE INDEX IF NOT EXISTS idx_wave_gate_results_project_version "
        "ON wave_gate_results(project_id, wave_label, version)"
    )


async def _migrate_pg_wave_gate_configs(conn: PostgresConnection) -> None:
    """74a8f420 — wave_gate_configs: the on-the-fly-configurable action pipeline
    (push_dev/push_main/deploy/wait/run_verification) attached to a wave or
    wave-range, keyed by its boundary wave (``wave_end``). claim_sprint_item
    reads this table (plus wave_gate_results) to structurally refuse claiming
    any item whose wave sorts beyond a configured-but-unpassed boundary.

    Mirrors db.migrations._migrate_wave_gate_configs. Idempotent via CREATE
    TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.

    ed8e4524 — added nullable ``version`` (sprint-version scope; NULL =
    unscoped/legacy, applies to every item regardless of its own version) via
    ``ADD COLUMN IF NOT EXISTS`` for a table that predates this fix. Same
    residual-constraint note as the SQLite mirror
    (db.migrations._migrate_wave_gate_configs): the UNIQUE constraint on an
    already-existing table is not retroactively widened to include version.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS wave_gate_configs ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL,"
        "    wave_start TEXT NOT NULL,"
        "    wave_end TEXT NOT NULL,"
        "    version TEXT,"
        "    actions TEXT NOT NULL,"
        "    actor TEXT,"
        "    created_at TEXT NOT NULL DEFAULT (now()::text),"
        "    updated_at TEXT NOT NULL DEFAULT (now()::text),"
        "    UNIQUE(project_id, wave_end, version)"
        ");"
        "ALTER TABLE wave_gate_configs ADD COLUMN IF NOT EXISTS version TEXT;"
        "CREATE INDEX IF NOT EXISTS idx_wave_gate_configs_project "
        "ON wave_gate_configs(project_id, wave_end);"
        "CREATE INDEX IF NOT EXISTS idx_wave_gate_configs_project_version "
        "ON wave_gate_configs(project_id, wave_end, version)"
    )


async def _migrate_pg_wave_gate_version_unique_constraints(
    conn: PostgresConnection,
) -> None:
    """Closes the residual-constraint gap flagged in ed8e4524's own docstrings
    on ``_migrate_pg_wave_gate_results`` / ``_migrate_pg_wave_gate_configs``.

    Both tables predate ed8e4524 on any long-running (non-fresh) database:
    their ``CREATE TABLE IF NOT EXISTS`` is a no-op there, so only the
    ``ADD COLUMN IF NOT EXISTS version`` half of that migration ever ran —
    the UNIQUE constraint stayed the OLD 2-column
    ``(project_id, wave_label)`` / ``(project_id, wave_end)`` shape instead
    of widening to include ``version``. Confirmed live: a genuinely
    version-scoped ``complete_wave_gate(version=...)`` call for a wave_label
    that already has an UNSCOPED (``version IS NULL``) result row fails with
    a raw ``duplicate key value violates unique constraint
    "wave_gate_results_project_id_wave_label_key"`` — the exact cross-version
    leak ed8e4524 was meant to close, reopened by the stale constraint on
    already-existing installs (this hosted project's table among them).

    Postgres has no ``ADD CONSTRAINT IF NOT EXISTS``, so the old auto-named
    constraint is looked up via ``pg_constraint`` (mirroring
    ``_migrate_pg_workspace_members_rbac``'s existing DROP-CONSTRAINT idiom)
    and dropped only if found; the new 3-column constraint add is wrapped in
    its own best-effort try/except so a second run (or a database that
    already has it) is a no-op either way. Online-safe on Postgres 11+ — no
    table rewrite, no long lock — matching the same idiom's own note.
    """
    for _table, _cols in (
        ("wave_gate_results", "project_id, wave_label, version"),
        ("wave_gate_configs", "project_id, wave_end, version"),
    ):
        try:
            await conn.execute(
                "DO $$ "
                "DECLARE c text; "
                "BEGIN "
                f"  SELECT conname INTO c FROM pg_constraint "
                f"   WHERE conrelid = '{_table}'::regclass "
                "     AND contype = 'u' "
                "     AND pg_get_constraintdef(oid) NOT LIKE '%version%' "
                "   LIMIT 1; "
                "  IF c IS NOT NULL THEN "
                f"    EXECUTE 'ALTER TABLE {_table} DROP CONSTRAINT ' || quote_ident(c); "
                "  END IF; "
                "END $$",
                None,
            )
        except Exception:  # noqa: BLE001 — best-effort; new installs already lack it
            pass
        try:
            await conn.execute(
                f"ALTER TABLE {_table} ADD CONSTRAINT "
                f"{_table}_version_unique_key UNIQUE ({_cols})",
                None,
            )
        except Exception:  # noqa: BLE001 — already exists (fresh install / prior run)
            pass


async def _migrate_pg_server_logs(conn: PostgresConnection) -> None:
    """f0a48685 — server_logs: application-wide WARNING/ERROR/EXCEPTION log capture.

    Every WARNING-or-above logging record emitted anywhere in the Meridian
    process is persisted here via a custom logging.Handler so post-mortem
    diagnosis of incidents is possible from a hosted-only session with no
    local machine access.

    Kept separate from connection_events: connection_events is one structured
    row per /mcp HTTP request; server_logs is one row per arbitrary log record.

    Mirrors db._migrate_server_logs. Idempotent via CREATE TABLE IF NOT EXISTS
    + CREATE INDEX IF NOT EXISTS.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS server_logs ("
        "    id TEXT PRIMARY KEY,"
        "    level TEXT NOT NULL DEFAULT 'ERROR',"
        "    logger TEXT NOT NULL DEFAULT '',"
        "    message TEXT NOT NULL DEFAULT '',"
        "    exc_text TEXT,"
        f"    recorded_at TEXT NOT NULL DEFAULT ({_TS})"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_server_logs_level_at "
        "ON server_logs(level, recorded_at DESC);"
        "CREATE INDEX IF NOT EXISTS idx_server_logs_at "
        "ON server_logs(recorded_at DESC)"
    )


async def _migrate_pg_custom_hooks(conn: PostgresConnection) -> None:
    """273287cb — custom_hooks: user-creatable Claude Code hooks.

    Generalizes past the single auto-written sprint_guard.sh/.ps1 pair so a
    project can define its own arbitrary PreToolUse/PostToolUse/Stop hooks
    that get written into .claude/hooks/ by generate_handoff.

    Mirrors db._migrate_custom_hooks. Idempotent via CREATE TABLE IF NOT
    EXISTS + CREATE INDEX IF NOT EXISTS.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS custom_hooks ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL,"
        "    name TEXT NOT NULL,"
        "    slug TEXT NOT NULL,"
        "    event TEXT NOT NULL CHECK (event IN ('PreToolUse','PostToolUse','Stop')),"
        "    matcher TEXT,"
        "    script_sh TEXT NOT NULL,"
        "    script_ps1 TEXT,"
        "    blocking INTEGER NOT NULL DEFAULT 1,"
        "    enabled INTEGER NOT NULL DEFAULT 1,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS}),"
        f"    updated_at TEXT NOT NULL DEFAULT ({_TS}),"
        "    UNIQUE(project_id, slug)"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_custom_hooks_project "
        "ON custom_hooks(project_id, event)"
    )


async def _migrate_pg_proposal_github_issue(conn: PostgresConnection) -> None:
    """3999d90f — workspace_proposals.github_issue_number + .github_issue_url.

    Storage for the "also file a GitHub issue?" conditional HITL workflow:
    promote_workspace_proposal fires a HITL when a code-related proposal is
    promoted under a project with a connected GitHub repo; if answered yes,
    the created issue's number/URL is persisted back onto the proposal here
    via set_proposal_github_issue. Both columns nullable — most proposals
    never go through this path.

    Mirrors db._migrate_proposal_github_issue. ADD COLUMN IF NOT EXISTS is
    idempotent.
    """
    await conn.executescript(
        "ALTER TABLE workspace_proposals ADD COLUMN IF NOT EXISTS "
        "github_issue_number INTEGER;"
        "ALTER TABLE workspace_proposals ADD COLUMN IF NOT EXISTS "
        "github_issue_url TEXT"
    )


async def _migrate_pg_sprint_item_prospect_bypass(conn: PostgresConnection) -> None:
    """94c26322 — human-set bypass flag for the prospecting safety gate.

    prospect_bypass (INTEGER 0/1, NOT NULL DEFAULT 0) is the ONLY structural way
    to include an unprospected sprint item in a /goal's auto-run claimable batch.
    Without real prospecting evidence AND without this flag set, the item is
    excluded from goal generation and claim_sprint_item warns hard.

    Settable ONLY by human/planning sessions via update_sprint_item.

    ADD COLUMN IF NOT EXISTS is idempotent; existing rows default to 0.
    Mirrors db._migrate_sprint_item_prospect_bypass.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS "
        "prospect_bypass INTEGER NOT NULL DEFAULT 0"
    )


async def _migrate_pg_sprint_item_require_verification(conn: PostgresConnection) -> None:
    """e2e1b682 — opt-in independent fresh-session verifier gate flag.

    require_verification (INTEGER 0/1, NOT NULL DEFAULT 0) marks a sprint item
    as needing an on-file, independent PASS (see sprint_item_verifications)
    before complete_sprint_item will let the completion stick.

    ADD COLUMN IF NOT EXISTS is idempotent; existing rows default to 0.
    Mirrors db._migrate_sprint_item_require_verification.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS "
        "require_verification INTEGER NOT NULL DEFAULT 0"
    )


async def _migrate_pg_sprint_item_verifications_table(conn: PostgresConnection) -> None:
    """e2e1b682 — sprint_item_verifications: durable audit trail of independent
    fresh-session PASS/FAIL verdicts filed against a sprint item.

    Mirrors db._migrate_sprint_item_verifications_table. Idempotent via CREATE
    TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS sprint_item_verifications ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL,"
        "    sprint_item_id TEXT NOT NULL,"
        "    verdict TEXT NOT NULL,"
        "    verifier_session_id TEXT NOT NULL,"
        "    notes TEXT,"
        "    seq INTEGER NOT NULL DEFAULT 0,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS})"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_sprint_item_verifications_item "
        "ON sprint_item_verifications(sprint_item_id, seq DESC)"
    )


async def _migrate_pg_handoff_tokens(conn: PostgresConnection) -> None:
    """cb8e7c0f — handoff_tokens: DB-backed provenance token store for cross-machine
    verify_handoff_token.

    The previous in-process _HANDOFF_TOKENS dict was process-local: on a multi-
    machine deployment (fly.toml max_count=40) generate_handoff on machine A minted
    a token into A's dict, but verify_handoff_token called from a new session on
    machine B read from B's empty dict and always returned not_found — making the
    trust boundary silently useless. Storing tokens in the shared DB fixes this.

    token (TEXT PK): the opaque random hex value embedded in <goal_token>.
    project_id (TEXT NOT NULL): the project this token was minted for.
    expires_at (TEXT NOT NULL): ISO-8601 UTC expiry timestamp.
    consumed (INTEGER NOT NULL DEFAULT 0): 1 once the token has been verified once.
    created_at (TEXT NOT NULL): for audit/cleanup purposes.

    Idempotent: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
    Mirrors db._migrate_handoff_tokens.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS handoff_tokens ("
        "    token TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL,"
        "    expires_at TEXT NOT NULL,"
        "    consumed INTEGER NOT NULL DEFAULT 0,"
        "    created_at TEXT NOT NULL DEFAULT (to_char(clock_timestamp() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US'))"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_handoff_tokens_project "
        "ON handoff_tokens(project_id);"
        "CREATE INDEX IF NOT EXISTS idx_handoff_tokens_expires "
        "ON handoff_tokens(expires_at);"
    )


async def _migrate_pg_sprint_item_required_tool(conn: PostgresConnection) -> None:
    """4d1fb28f — sprint_items.required_tool: item-level MCP tool/plugin pin
    (mirrors SQLite).

    Nullable free-form TEXT; NULL = no pin, ordinary executor discretion.
    When set, it's rendered as a hard ``<required_tool>`` directive in the
    /goal block (handoff._build_quick_start_goal / build_item_briefing).

    ADD COLUMN IF NOT EXISTS is idempotent. Mirrors
    db._migrate_sprint_item_required_tool.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS required_tool TEXT"
    )


async def _migrate_pg_sprint_item_github_issue_link(conn: PostgresConnection) -> None:
    """fdaa5b55 — sprint_items.github_issue_number / .github_issue_url /
    .github_issue_source (mirrors SQLite).

    ``github_issue_source`` is written EXCLUSIVELY by
    meridian.db.sprint_items.link_sprint_item_github_issue, and set to
    ``'meridian_auto'`` only right after a real create_issue call succeeds
    (server.py's ``_on_hitl_answered`` 'proposal_github_issue' branch) — never
    inferred from issue title/body text. NULL/anything else is treated as
    'manual'-equivalent by complete_sprint_item's close/propose gate.

    ADD COLUMN IF NOT EXISTS is idempotent. Mirrors
    db._migrate_sprint_item_github_issue_link.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS github_issue_number INTEGER;"
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS github_issue_url TEXT;"
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS github_issue_source TEXT"
    )


async def _migrate_pg_manual_issue_screening_toggle(conn: PostgresConnection) -> None:
    """5dfe34b2 / cd495afa — workspace_settings.manual_issue_screening_enabled
    (mirrors SQLite). The ONE writer is
    meridian.db.workspace.set_manual_issue_screening_enabled, which refuses to
    enable it without an answered + approved require_human=True HITL of
    kind='manual_issue_screening_toggle'. Disabling has no HITL gate (fail-safe
    direction) but is still audit-logged.

    ADD COLUMN IF NOT EXISTS is idempotent. Mirrors
    db._migrate_manual_issue_screening_toggle.
    """
    await conn.executescript(
        "ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS "
        "manual_issue_screening_enabled INTEGER NOT NULL DEFAULT 0"
    )


async def _migrate_pg_workspace_tool_priority_map(conn: PostgresConnection) -> None:
    """490e100d — workspace_settings.tool_priority_map (mirrors SQLite).

    Nullable TEXT, JSON-encoded ``{category: tool}`` dict — the workspace-level
    generalization of 4d1fb28f's per-item ``required_tool`` pin. When set,
    handoff._build_quick_start_goal renders a hard, unconditional
    ``<workspace_tool_priority>`` directive for every pending item whose
    title/notes match a configured category and that has no item-level
    override.

    ADD COLUMN IF NOT EXISTS is idempotent. Mirrors
    db._migrate_workspace_tool_priority_map.
    """
    await conn.executescript(
        "ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS tool_priority_map TEXT"
    )


async def _migrate_pg_action_audit_log_table(conn: PostgresConnection) -> None:
    """5dfe34b2 / cd495afa — action_audit_log: append-only WHAT-MERIDIAN-DID
    record (toggle flips, velocity/anomaly escalations, manual-issue link
    actions). Mirrors db._migrate_action_audit_log_table. Idempotent via
    CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS action_audit_log ("
        "    id TEXT PRIMARY KEY,"
        "    tenant_id TEXT,"
        "    project_id TEXT,"
        "    event_type TEXT NOT NULL,"
        "    actor TEXT,"
        "    detail TEXT,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS})"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_action_audit_log_scope "
        "ON action_audit_log(tenant_id, project_id, created_at DESC)"
    )


async def _migrate_pg_manual_issue_content_log_table(conn: PostgresConnection) -> None:
    """5dfe34b2 / 2178b161 — manual_issue_content_log: append-only, hashed,
    timestamped forensic log of RAW manual-issue content (title+body+
    comments), written BEFORE screening. Mirrors
    db._migrate_manual_issue_content_log_table. Idempotent via CREATE TABLE IF
    NOT EXISTS + CREATE INDEX IF NOT EXISTS.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS manual_issue_content_log ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL,"
        "    issue_number INTEGER NOT NULL,"
        "    content_hash TEXT NOT NULL,"
        "    raw_content TEXT NOT NULL,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS})"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_manual_issue_content_log_scope "
        "ON manual_issue_content_log(project_id, issue_number, created_at DESC)"
    )


async def _migrate_pg_sprint_item_github_channel(conn: PostgresConnection) -> None:
    """7c82f7c8 — ``github_channel`` on sprint_items (mirrors SQLite).

    Nullable TEXT: NULL = no channel classification recorded; 'nightly' /
    'stable' track which release channel a linked, auto-filed GitHub issue
    (fdaa5b55) was reported against — set from the issue template the
    reporter picked (channel:nightly / channel:stable labels, see
    .github/ISSUE_TEMPLATE/). 'graduated' is the third state: a bug that
    started as nightly-only noise but is now confirmed reproducing on
    stable too — needs a real fix before general release. Enum enforced at
    the app layer (see ``_VALID_SPRINT_GITHUB_CHANNELS`` in
    db/sprint_items.py). CREATE_TABLES_CORE covers fresh DBs; this is the
    upgrade path. ADD COLUMN IF NOT EXISTS → idempotent. Mirrors
    db._migrate_sprint_item_github_channel.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS github_channel TEXT"
    )


async def _migrate_pg_claim_verification_mode(conn: PostgresConnection) -> None:
    """4ef6ce5e — workspace_settings.claim_verification_mode (mirrors SQLite).

    Three states (off/advisory/strict) controlling whether a PostToolUse hook
    (meridian.claim_verify) re-checks claim_sprint_item/complete_sprint_item
    calls against live DB state and warns (advisory) or blocks (strict) on a
    narration/reality mismatch. See db.migrations._migrate_workspace_claim_verification_mode
    for the full incident/design writeup.

    ``NOT NULL DEFAULT 'off'`` — unchanged behavior for every existing
    deployment. ADD COLUMN IF NOT EXISTS is idempotent. Mirrors
    db._migrate_workspace_claim_verification_mode.
    """
    await conn.executescript(
        "ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS "
        "claim_verification_mode TEXT NOT NULL DEFAULT 'off'"
    )


async def _migrate_pg_handoff_tokens_consumed_at(conn: PostgresConnection) -> None:
    """b763d2ba — handoff_tokens.consumed_at (mirrors SQLite).

    Nullable ISO-8601 UTC timestamp of when a token was actually consumed,
    decoupling a consumed row's retention window (how long
    verify_handoff_token can still honestly report "already_consumed" instead
    of a purged-away "not_found") from the token's own short mint-time TTL.
    See db.migrations._migrate_handoff_tokens_consumed_at for the full
    2026-07-21 false-positive-spoofing-alarm writeup.

    ADD COLUMN IF NOT EXISTS is idempotent; existing rows default to NULL.
    Mirrors db._migrate_handoff_tokens_consumed_at.
    """
    await conn.executescript(
        "ALTER TABLE handoff_tokens ADD COLUMN IF NOT EXISTS consumed_at TEXT"
    )


async def _migrate_pg_handoff_tokens_body_hash(conn: PostgresConnection) -> None:
    """efaa918a — handoff_tokens.body_hash (mirrors SQLite).

    Nullable SHA-256 hex digest binding a token to the canonical body it was
    minted for, closing the 2ee0000c token/body-integrity gap documented in
    AGENTS.md. See db.migrations._migrate_handoff_tokens_body_hash for the
    full writeup.

    ADD COLUMN IF NOT EXISTS is idempotent; existing rows default to NULL, and
    a NULL body_hash means verify_handoff_token's body check is skipped —
    purely additive. Mirrors db._migrate_handoff_tokens_body_hash.
    """
    await conn.executescript(
        "ALTER TABLE handoff_tokens ADD COLUMN IF NOT EXISTS body_hash TEXT"
    )


async def _migrate_pg_board_snapshot_revisions(conn: PostgresConnection) -> None:
    """ef665ef8 — board_snapshot_revisions (mirrors SQLite).

    One row per DISTINCT revision hash observed for a ``(project_id,
    version_filter)`` bucket; ``revision_counter`` is a monotonic,
    persisted counter so a caller can tell "newer" from merely "different"
    when comparing two canonical expanded-board snapshots (see
    meridian.db.board_snapshot). CREATE_TABLES_CORE covers fresh DBs; this is
    the upgrade path. The index lives here, never inline in
    CREATE_TABLES_CORE, to avoid the unguarded-index boot crash. Idempotent.
    Mirrors db._migrate_board_snapshot_revisions.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS board_snapshot_revisions ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL,"
        "    version_filter TEXT NOT NULL DEFAULT '',"
        "    revision_hash TEXT NOT NULL,"
        "    revision_counter INTEGER NOT NULL,"
        "    item_count INTEGER NOT NULL DEFAULT 0,"
        "    created_at TEXT NOT NULL DEFAULT (to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US'))"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_board_snapshot_revisions_bucket "
        "ON board_snapshot_revisions(project_id, version_filter, revision_counter DESC);"
    )


async def _migrate_pg_wave_runs(conn: PostgresConnection) -> None:
    """2a654cb0 — wave_runs / wave_run_events / wave_run_children (mirrors SQLite).

    Durable state for a paused/resumed multi-agent wave: the run itself (with
    its immutable id, enumerated status, pinned board snapshot + revision hash,
    degraded-tool provenance, and write-once finalizer evidence), its strictly
    append-only event history (monotonic per-run seq, corrections expressed by
    superseding rather than mutation), and its per-sprint-item children (whose
    failure_mode='stop' outcome structurally blocks finalization). 7d71d6bc
    (RESCUE-R2) added child-lease/dispatch-provenance columns (agent_id,
    claimed_at, last_heartbeat_at, lease_ttl_seconds, exit_code, attempt,
    dispatch_provenance) via idempotent ADD COLUMN IF NOT EXISTS — see
    meridian.db.wave_runs for the functions that populate/read them.

    CREATE_TABLES_CORE covers fresh DBs; this is the upgrade path. Every index
    lives here, never inline in CREATE_TABLES_CORE, to avoid the unguarded-index
    boot crash. Idempotent. Mirrors db.migrations._migrate_wave_runs.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS wave_runs ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL,"
        "    version TEXT,"
        "    wave_label TEXT,"
        "    status TEXT NOT NULL DEFAULT 'planned',"
        "    board_snapshot TEXT,"
        "    revision_hash TEXT,"
        "    revision_counter INTEGER,"
        "    item_ids TEXT NOT NULL DEFAULT '[]',"
        "    degraded_tools TEXT NOT NULL DEFAULT '[]',"
        "    finalizer_evidence TEXT,"
        "    finalized_at TEXT,"
        "    actor TEXT,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS}),"
        f"    updated_at TEXT NOT NULL DEFAULT ({_TS})"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_wave_runs_project "
        "ON wave_runs(project_id, status, created_at DESC);"
        "CREATE TABLE IF NOT EXISTS wave_run_events ("
        "    id TEXT PRIMARY KEY,"
        "    wave_run_id TEXT NOT NULL,"
        "    seq INTEGER NOT NULL,"
        "    event_type TEXT NOT NULL,"
        "    from_status TEXT,"
        "    to_status TEXT,"
        "    detail TEXT,"
        "    payload TEXT,"
        "    actor TEXT,"
        "    supersedes TEXT,"
        "    superseded_by TEXT,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS}),"
        "    UNIQUE(wave_run_id, seq)"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_wave_run_events_run "
        "ON wave_run_events(wave_run_id, seq);"
        "CREATE TABLE IF NOT EXISTS wave_run_children ("
        "    id TEXT PRIMARY KEY,"
        "    wave_run_id TEXT NOT NULL,"
        "    sprint_item_id TEXT NOT NULL,"
        "    failure_mode TEXT NOT NULL DEFAULT 'continue',"
        "    status TEXT NOT NULL DEFAULT 'running',"
        "    evidence TEXT,"
        "    actor TEXT,"
        "    agent_id TEXT,"
        "    claimed_at TEXT,"
        "    last_heartbeat_at TEXT,"
        "    lease_ttl_seconds INTEGER,"
        "    exit_code INTEGER,"
        "    attempt INTEGER NOT NULL DEFAULT 1,"
        "    dispatch_provenance TEXT,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS}),"
        f"    updated_at TEXT NOT NULL DEFAULT ({_TS}),"
        "    UNIQUE(wave_run_id, sprint_item_id)"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_wave_run_children_run "
        "ON wave_run_children(wave_run_id, status);"
        # 7d71d6bc (RESCUE-R2) — additive lease/dispatch-provenance columns
        # for a wave_run_children table that may already exist from before
        # this fix (the CREATE TABLE above only applies to a fresh DB).
        # IF NOT EXISTS makes every ALTER idempotent, same pattern as the
        # sibling ADD-COLUMN migrations in this file (e.g.
        # _migrate_pg_sprint_item_tool_requirements). Mirrors
        # db.migrations._migrate_wave_runs's _migrate_add_column_if_missing
        # calls for the SQLite side.
        "ALTER TABLE wave_run_children ADD COLUMN IF NOT EXISTS agent_id TEXT;"
        "ALTER TABLE wave_run_children ADD COLUMN IF NOT EXISTS claimed_at TEXT;"
        "ALTER TABLE wave_run_children "
        "ADD COLUMN IF NOT EXISTS last_heartbeat_at TEXT;"
        "ALTER TABLE wave_run_children "
        "ADD COLUMN IF NOT EXISTS lease_ttl_seconds INTEGER;"
        "ALTER TABLE wave_run_children ADD COLUMN IF NOT EXISTS exit_code INTEGER;"
        "ALTER TABLE wave_run_children "
        "ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 1;"
        "ALTER TABLE wave_run_children "
        "ADD COLUMN IF NOT EXISTS dispatch_provenance TEXT;"
        "CREATE INDEX IF NOT EXISTS idx_wave_run_children_lease "
        "ON wave_run_children(wave_run_id, status, last_heartbeat_at);"
    )


async def _migrate_pg_project_capabilities(conn: PostgresConnection) -> None:
    """649e095f — project_capabilities (mirrors SQLite).

    One row per project: a normalized JSON list of capability declarations
    plus schema version and content hash. Mirrors
    db.migrations._migrate_project_capabilities.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS project_capabilities ("
        "    project_id TEXT PRIMARY KEY,"
        "    manifest TEXT NOT NULL DEFAULT '[]',"
        "    manifest_version INTEGER NOT NULL DEFAULT 1,"
        "    manifest_hash TEXT,"
        f"    updated_at TEXT NOT NULL DEFAULT ({_TS})"
        ");"
    )


async def _migrate_pg_capability_profiles(conn: PostgresConnection) -> None:
    """02038afe — capability_profiles (mirrors SQLite).

    One row per (scope_type, scope_id): a normalized JSON capability list,
    an explicit disabled-capability-id list, schema version, content hash,
    non-secret provenance, and updated_at. Mirrors
    db.migrations._migrate_capability_profiles.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS capability_profiles ("
        "    scope_type TEXT NOT NULL,"
        "    scope_id TEXT NOT NULL,"
        "    manifest TEXT NOT NULL DEFAULT '[]',"
        "    disabled_ids TEXT NOT NULL DEFAULT '[]',"
        "    manifest_version INTEGER NOT NULL DEFAULT 1,"
        "    manifest_hash TEXT,"
        "    provenance TEXT,"
        f"    updated_at TEXT NOT NULL DEFAULT ({_TS}),"
        "    PRIMARY KEY (scope_type, scope_id)"
        ");"
    )


async def _migrate_pg_profile_layers(conn: PostgresConnection) -> None:
    """d8481276 — profile_layers / profile_layer_revisions (mirrors SQLite).

    One row per (scope_type, scope_id): a JSON field dict, an explicit
    reset-field list, schema version, content hash, a hosted_default-only
    lifecycle state, non-secret provenance, and updated_at. The revisions
    table is an append-only audit ledger for hosted_default only, mirroring
    board_snapshot_revisions. Mirrors db.migrations._migrate_profile_layers.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS profile_layers ("
        "    scope_type TEXT NOT NULL,"
        "    scope_id TEXT NOT NULL,"
        "    schema_version INTEGER NOT NULL DEFAULT 1,"
        "    revision INTEGER NOT NULL DEFAULT 0,"
        "    fields TEXT NOT NULL DEFAULT '{}',"
        "    reset_fields TEXT NOT NULL DEFAULT '[]',"
        "    lifecycle_state TEXT,"
        "    content_hash TEXT,"
        "    provenance TEXT,"
        f"    updated_at TEXT NOT NULL DEFAULT ({_TS}),"
        "    PRIMARY KEY (scope_type, scope_id)"
        ");"
        "CREATE TABLE IF NOT EXISTS profile_layer_revisions ("
        "    id TEXT PRIMARY KEY,"
        "    scope_type TEXT NOT NULL,"
        "    scope_id TEXT NOT NULL,"
        "    revision INTEGER NOT NULL,"
        "    content_hash TEXT NOT NULL,"
        "    lifecycle_state TEXT,"
        "    fields TEXT NOT NULL DEFAULT '{}',"
        "    reset_fields TEXT NOT NULL DEFAULT '[]',"
        "    actor TEXT,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS})"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_profile_layer_revisions_scope "
        "ON profile_layer_revisions(scope_type, scope_id, revision DESC);"
    )


async def _migrate_pg_sprint_item_tool_requirements(conn: PostgresConnection) -> None:
    """76dde31f (665 follow-up) — sprint_items.tool_requirements: typed,
    per-item MCP tool-requirement contract (mirrors SQLite).

    Nullable TEXT column holding a JSON array of normalized entries (see
    meridian.tool_requirements). Distinct from touches_resources (scheduling
    metadata) and the legacy free-form required_tool pin (4d1fb28f) — the
    structured field is canonical once set; required_tool remains a
    read-time compatibility fallback only when this column is empty.

    ADD COLUMN IF NOT EXISTS is idempotent. Mirrors
    db.migrations._migrate_sprint_item_tool_requirements.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS tool_requirements TEXT"
    )


async def _migrate_pg_sprint_item_artifact_declaration(conn: PostgresConnection) -> None:
    """2f9cb288 (b7308039 / 665 follow-up) — sprint_items.artifact_kind /
    planned_output / artifact_policy (mirrors SQLite).

    ``artifact_kind`` is a plain enum column (like milestone_type/priority);
    ``planned_output``/``artifact_policy`` are nullable JSON-encoded TEXT
    columns, validated via meridian.artifact_declaration before ever
    reaching this table. NULL on all three = no declaration (read back as
    "unknown"/project-default, never a guess or a hard block).

    ADD COLUMN IF NOT EXISTS is idempotent. Mirrors
    db.migrations._migrate_sprint_item_artifact_declaration.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS artifact_kind TEXT;"
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS planned_output TEXT;"
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS artifact_policy TEXT"
    )


async def _migrate_pg_sprint_item_require_strict_evidence(conn: PostgresConnection) -> None:
    """5fe3502e — opt-in fail-closed completion-evidence gate flag.

    require_strict_evidence (INTEGER 0/1, NOT NULL DEFAULT 0) marks a sprint
    item as needing the STRICT (fail-closed) evidence verification in
    meridian.sprint_evidence_guard before complete_sprint_item's handler will
    let the completion stick. Mirrors require_verification's shape exactly.

    ADD COLUMN IF NOT EXISTS is idempotent; existing rows default to 0.
    Mirrors db.migrations._migrate_sprint_item_require_strict_evidence.
    """
    await conn.executescript(
        "ALTER TABLE sprint_items ADD COLUMN IF NOT EXISTS "
        "require_strict_evidence INTEGER NOT NULL DEFAULT 0"
    )


async def _migrate_pg_handoffs_invalidation(conn: PostgresConnection) -> None:
    """3af86d28 — invalidation/non-executable marking for a ``handoffs`` row
    (mirrors db.migrations._migrate_handoffs_invalidation).

    Four additive columns. ADD COLUMN IF NOT EXISTS is idempotent.
    """
    await conn.executescript(
        "ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS "
        "invalidated INTEGER NOT NULL DEFAULT 0;"
        "ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS invalidated_reason TEXT;"
        "ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS invalidated_at TEXT;"
        "ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS "
        "superseded_by_correction_id TEXT;"
    )


async def _migrate_pg_handoff_corrections_table(conn: PostgresConnection) -> None:
    """3af86d28 — handoff_corrections: corrective-handoff data structure
    (mirrors db.migrations._migrate_handoff_corrections_table — see that
    function's docstring for the full field-by-field rationale).

    CREATE TABLE / INDEX IF NOT EXISTS so re-running is a no-op.
    """
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS handoff_corrections (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            session_id TEXT,
            source_handoff_id TEXT NOT NULL,
            source_token TEXT,
            source_body_hash TEXT,
            version TEXT,
            requested_scope TEXT,
            blocker_classification TEXT NOT NULL,
            investigation_evidence TEXT,
            added_pointers TEXT NOT NULL DEFAULT '[]',
            removed_pointers TEXT NOT NULL DEFAULT '[]',
            superseded_pointers TEXT NOT NULL DEFAULT '[]',
            changed_resources TEXT NOT NULL DEFAULT '[]',
            pointer_repair_report TEXT,
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft','verified','superseded','blocked')),
            status_reason TEXT,
            idempotency_key TEXT,
            new_handoff_id TEXT,
            new_token TEXT,
            new_body_hash TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_handoff_corrections_project
            ON handoff_corrections(project_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_handoff_corrections_source
            ON handoff_corrections(source_handoff_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_handoff_corrections_idempotency
            ON handoff_corrections(project_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        """
    )


async def _migrate_pg_vector_index_state(conn: PostgresConnection) -> None:
    """e1475682 — vector_index_state (mirrors SQLite).

    One row per (project_id, scope): the backend actually in use
    (bm25/duckdb_vss/pgvector), the embedding model/version + dimension that
    produced it, a source_fingerprint for staleness detection, an
    incrementing revision, and the last benchmark evidence + decision reason
    behind pgvector_enabled. Mirrors
    db.vector_index_state._migrate_vector_index_state.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS vector_index_state ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL REFERENCES projects(id),"
        "    scope TEXT NOT NULL DEFAULT 'default',"
        "    backend TEXT NOT NULL DEFAULT 'bm25',"
        "    embedding_model TEXT,"
        "    embedding_version TEXT,"
        "    dimension INTEGER,"
        "    source_fingerprint TEXT,"
        "    revision INTEGER NOT NULL DEFAULT 1,"
        "    pgvector_enabled INTEGER NOT NULL DEFAULT 0,"
        "    benchmark_evidence TEXT,"
        "    benchmark_decision_reason TEXT,"
        f"    updated_at TEXT NOT NULL DEFAULT ({_TS}),"
        "    UNIQUE (project_id, scope)"
        ");"
    )


async def _migrate_pg_executor_reports(conn: PostgresConnection) -> None:
    """9154aa9a — executor_reports: durable executor-report / corrective-
    handoff-lifecycle records (mirrors db.executor_reports._migrate_executor_reports_table
    — see that function's docstring for the full field-by-field rationale).

    CREATE TABLE / INDEX IF NOT EXISTS so re-running is a no-op.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS executor_reports ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL,"
        "    version TEXT,"
        "    session_id TEXT,"
        "    source_handoff_id TEXT,"
        "    board_revision_hash TEXT,"
        "    item_outcomes TEXT NOT NULL DEFAULT '[]',"
        "    changed_resources TEXT NOT NULL DEFAULT '[]',"
        "    commits TEXT NOT NULL DEFAULT '[]',"
        "    tests TEXT,"
        "    tool_availability TEXT NOT NULL DEFAULT '[]',"
        "    artifact_evidence TEXT,"
        "    blockers TEXT NOT NULL DEFAULT '[]',"
        "    unresolved_questions TEXT NOT NULL DEFAULT '[]',"
        "    recommended_next_actions TEXT NOT NULL DEFAULT '[]',"
        "    status TEXT NOT NULL DEFAULT 'submitted'"
        "        CHECK (status IN ('submitted','accepted','superseded')),"
        "    parent_report_id TEXT,"
        "    correction_reason TEXT,"
        "    report_hash TEXT,"
        "    accepted_handoff_id TEXT,"
        "    accepted_at TEXT,"
        "    accepted_by TEXT,"
        "    idempotency_key TEXT,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS}),"
        f"    updated_at TEXT NOT NULL DEFAULT ({_TS})"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_executor_reports_project "
        "ON executor_reports(project_id, created_at DESC);"
        "CREATE INDEX IF NOT EXISTS idx_executor_reports_project_version "
        "ON executor_reports(project_id, version);"
        "CREATE INDEX IF NOT EXISTS idx_executor_reports_parent "
        "ON executor_reports(parent_report_id);"
        "CREATE INDEX IF NOT EXISTS idx_executor_reports_source_handoff "
        "ON executor_reports(source_handoff_id);"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_executor_reports_idempotency "
        "ON executor_reports(project_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL;"
    )


async def _migrate_pg_wave_run_summaries(conn: PostgresConnection) -> None:
    """bbb447ec — immutable wave-completion summaries keyed by wave_id.

    Mirrors db.wave_run_summary._migrate_wave_run_summaries. Re-running this
    migration is safe and intentionally idempotent.
    """
    await conn.executescript(
        "CREATE TABLE IF NOT EXISTS wave_run_summaries ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL,"
        "    version_filter TEXT NOT NULL DEFAULT '',"
        "    wave_id TEXT NOT NULL,"
        "    wave_run_id TEXT,"
        "    session_id TEXT,"
        "    board_revision_hash TEXT,"
        "    items TEXT NOT NULL DEFAULT '[]',"
        "    commits TEXT NOT NULL DEFAULT '[]',"
        "    changed_resources TEXT NOT NULL DEFAULT '[]',"
        "    test_receipts TEXT NOT NULL DEFAULT '[]',"
        "    blockers TEXT NOT NULL DEFAULT '[]',"
        "    exclusions TEXT NOT NULL DEFAULT '[]',"
        "    tool_availability TEXT NOT NULL DEFAULT '[]',"
        "    handoff_status TEXT,"
        "    summary_hash TEXT,"
        "    actor TEXT,"
        "    supersedes TEXT,"
        "    superseded_by TEXT,"
        "    correction_reason TEXT,"
        "    seq INTEGER NOT NULL DEFAULT 1,"
        f"    created_at TEXT NOT NULL DEFAULT ({_TS})"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_wave_run_summaries_lookup "
        "ON wave_run_summaries(project_id, version_filter, wave_id, seq DESC);"
        "CREATE INDEX IF NOT EXISTS idx_wave_run_summaries_run "
        "ON wave_run_summaries(wave_run_id);"
        "CREATE INDEX IF NOT EXISTS idx_wave_run_summaries_supersedes "
        "ON wave_run_summaries(supersedes);"
    )


# Late migrations — run on every DB after the hosted-only set.
_PG_MIGRATIONS_LATE = (
    _migrate_pg_workspace_tenant_isolation,
    _migrate_pg_workspace_sprint_board,
    _migrate_pg_sprint_items_claimed_at,
    _migrate_pg_sprint_item_tree,
    _migrate_pg_api_token_type,
    _migrate_pg_api_token_expires_at,
    _migrate_pg_oauth_codes,
    _migrate_pg_device_codes,
    _migrate_pg_github_to_projects,
    _migrate_pg_touches_resources,
    _migrate_pg_resource_locks,
    _migrate_pg_sprint_item_stall_count,
    _migrate_pg_queued_session,
    _migrate_pg_parallel_safety,
    _migrate_pg_changelog_entries,
    _migrate_pg_agent_instructions,
    _migrate_pg_backfill_agent_instructions,
    _migrate_pg_note_kind,
    _migrate_pg_file_symbol_claims,
    _migrate_pg_code_intel,
    _migrate_pg_notes_priority,
    _migrate_pg_task_log_kind,
    _migrate_pg_oauth_refresh_tokens,
    _migrate_pg_note_slug,
    _migrate_pg_decision_priority_edit_log,
    _migrate_pg_code_anchored_notes,
    _migrate_pg_note_source,
    _migrate_pg_session_sprint_version,
    _migrate_pg_project_execution_mode,
    _migrate_pg_project_status_priority,
    _migrate_pg_decision_code_anchor,
    _migrate_pg_session_graph_snapshots,
    _migrate_pg_agent_tasks_table,
    _migrate_pg_sprint_item_owner,
    _migrate_pg_session_note_kind,
    _migrate_pg_handoffs_table,
    _migrate_pg_decision_assumption,
    _migrate_pg_github_connections,
    _migrate_pg_blog_posts,
    _migrate_pg_sprint_item_quality_gates,
    _migrate_pg_parallel_primitives,
    _migrate_pg_signup_attempts,
    _migrate_pg_user_session_metadata,
    _migrate_pg_provision_queue,
    _migrate_pg_codebase_graph_entities,
    _migrate_pg_pending_goal,
    _migrate_pg_insights_table,
    _migrate_pg_sprint_item_slug,
    _migrate_pg_sprint_item_nickname,
    _migrate_pg_capture_insight_notes_to_insights,
    _migrate_pg_blog_posts_tenant,
    _migrate_pg_project_parent_id,
    _migrate_pg_session_goal_compliance,
    _migrate_pg_sprint_item_pointers,
    _migrate_pg_sprint_item_deferral,
    _migrate_pg_sprint_item_priority_blocker,
    _migrate_pg_sprint_item_wave,
    _migrate_pg_sprint_item_dependency,
    _migrate_pg_mcp_rate_counters,
    _migrate_pg_workspace_proposals,
    _migrate_pg_pending_goal_at,
    _migrate_pg_file_patch_counters,
    _migrate_pg_sprint_item_resources_amended,
    _migrate_pg_session_activity,
    _migrate_pg_file_docx_region_claims,
    _migrate_pg_connection_events,
    _migrate_pg_sprint_version_descriptions,
    _migrate_pg_workspace_settings_active_session_threshold,
    _migrate_pg_sprint_item_sprint_name,
    _migrate_pg_proposal_slug_nickname,
    _migrate_pg_decision_slug_nickname,
    _migrate_pg_note_nickname,
    _migrate_pg_sprint_item_prospect_bypass,
    _migrate_pg_handoff_tokens,
    _migrate_pg_wave_gate_results,
    _migrate_pg_wave_gate_configs,
    _migrate_pg_server_logs,
    _migrate_pg_custom_hooks,
    _migrate_pg_sprint_item_require_verification,
    _migrate_pg_sprint_item_verifications_table,
    _migrate_pg_proposal_github_issue,
    _migrate_pg_sprint_item_required_tool,
    _migrate_pg_sprint_item_github_issue_link,
    _migrate_pg_manual_issue_screening_toggle,
    _migrate_pg_action_audit_log_table,
    _migrate_pg_manual_issue_content_log_table,
    _migrate_pg_workspace_tool_priority_map,
    _migrate_pg_sprint_item_github_channel,
    _migrate_pg_claim_verification_mode,
    _migrate_pg_handoff_tokens_consumed_at,
    _migrate_pg_board_snapshot_revisions,
    _migrate_pg_wave_runs,
    _migrate_pg_handoff_tokens_body_hash,
    _migrate_pg_project_capabilities,
    _migrate_pg_capability_profiles,
    _migrate_pg_sprint_item_tool_requirements,
    _migrate_pg_sprint_item_artifact_declaration,
    _migrate_pg_docx_merge_manifests,
    _migrate_pg_proposal_evidence_links,
    _migrate_pg_wave_base_manifests,
    _migrate_pg_sprint_batch_claims,
    _migrate_pg_verification_runs,
    _migrate_pg_sprint_item_require_strict_evidence,
    _migrate_pg_handoffs_invalidation,
    _migrate_pg_handoff_corrections_table,
    _migrate_pg_vector_index_state,
    _migrate_pg_pixi_env_roots,
    _migrate_pg_executor_reports,
    _migrate_pg_wave_run_summaries,
    _migrate_pg_decision_evidence,
    _migrate_pg_ai_log_events,
    _migrate_pg_proposal_intake_drafts,
    _migrate_pg_sprint_batch_claims_reservation_fields,
    _migrate_pg_profile_layers,
    _migrate_pg_wave_gate_version_unique_constraints,
)
