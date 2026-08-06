"""Tests for meridian/pg_adapter.py's PostgresConnection sqlite_master
translation (246eccb6).

WHY THIS EXISTS
----------------
SQLite-authored code across this codebase (migration guards in
meridian/db/migrations.py, plus a few tests) queries the SQLite-only system
catalog ``sqlite_master`` to ask "does table/index X exist" or "what is X's
DDL text". PostgresConnection.execute() used to intercept EVERY such query
and return one hard-coded fake row -- ``{"sql": "pending-hitl 'backlog'
'backburner'", "name": "task_log"}`` -- regardless of what was actually being
asked. That made tests/test_workspace_proposals.py::
test_add_workspace_proposal_idempotency_key_unique_index_exists fail against
real Postgres in CI (GitHub Actions run 31047597879 on origin/dev 5390b65):
the test asks for ``idx_workspace_proposals_idempotency`` and asserts
``UNIQUE`` in the returned ``sql`` text, but got the unrelated task_log fake
instead.

PostgresConnection._sqlite_master now parses the WHERE clause (table/index
name, literal or ``?``-bound) and issues the equivalent REAL introspection
query against Postgres's own catalogs (``pg_indexes`` / ``information_schema``)
instead of returning a fixed fake row.

NO LIVE POSTGRES IN THIS ENVIRONMENT: like
tests/test_core.py::test_pg_retry_closes_stale_connection_on_cached_plan_error,
these tests mock the psycopg3 pool/connection/cursor objects
PostgresConnection wraps and exercise the REAL translation logic
(PostgresConnection._do_execute / ._sqlite_master / ._sqlite_master_index /
._sqlite_master_table) end to end -- only the outermost driver calls are
faked. This is a meaningful unit test of the translation logic, but it is
NOT a substitute for the real-Postgres coverage tests/test_workspace_proposals.py
gets from the test-postgres CI job (a real postgres:16 service container);
that job is what actually proves this fix against a live server.
"""
from __future__ import annotations

import pytest

from meridian.pg_adapter import PostgresConnection

# ---------------------------------------------------------------------------
# Fakes: psycopg3 pool/connection/cursor via the ``pool.connection()``
# async-context-manager shape that PostgresConnection._table_info and the new
# _sqlite_master_* helpers use (as opposed to the raw getconn()/putconn()
# pair _execute_with_retry uses -- see that method's own FakePool in
# tests/test_core.py::test_pg_retry_closes_stale_connection_on_cached_plan_error
# for the other shape already established in this codebase).
# ---------------------------------------------------------------------------


class _ScriptedCursor:
    """Fake psycopg3 cursor.

    Rows are chosen by a substring match against the executed SQL text,
    configured via ``responses`` -- a list of ``(substring, rows)`` pairs
    checked in order; the first substring found in the executed SQL wins. A
    query matching no configured substring gets an EMPTY result (never an
    error and never a fabricated row) -- the same "not found" behavior real
    Postgres gives for a query with no matching catalog rows.
    """

    def __init__(self, responses: list[tuple[str, list[dict]]], calls: list[tuple[str, tuple]]) -> None:
        self._responses = responses
        self._calls = calls
        self._rows: list[dict] = []

    async def __aenter__(self) -> "_ScriptedCursor":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, sql: str, params: tuple | None = None) -> None:
        self._calls.append((sql, params))
        for substring, rows in self._responses:
            if substring in sql:
                self._rows = rows
                return
        self._rows = []

    async def fetchone(self) -> dict | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[dict]:
        return list(self._rows)


class _ScriptedConn:
    def __init__(self, responses: list[tuple[str, list[dict]]], calls: list[tuple[str, tuple]]) -> None:
        self._responses = responses
        self._calls = calls

    def cursor(self, row_factory=None) -> _ScriptedCursor:  # noqa: ANN001
        return _ScriptedCursor(self._responses, self._calls)


class _ConnCtx:
    def __init__(self, responses: list[tuple[str, list[dict]]], calls: list[tuple[str, tuple]]) -> None:
        self._responses = responses
        self._calls = calls

    async def __aenter__(self) -> _ScriptedConn:
        return _ScriptedConn(self._responses, self._calls)

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _ScriptedPool:
    """Fake psycopg_pool.AsyncConnectionPool exposing only ``.connection()``."""

    def __init__(self, responses: list[tuple[str, list[dict]]]) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._responses = responses

    def connection(self) -> _ConnCtx:
        return _ConnCtx(self._responses, self.calls)


def _pg(responses: list[tuple[str, list[dict]]]) -> tuple[PostgresConnection, _ScriptedPool]:
    """Build a PostgresConnection wired to a scripted fake pool."""
    pool = _ScriptedPool(responses)
    return PostgresConnection(pool), pool


# ---------------------------------------------------------------------------
# type='index' -- the workspace-proposals idempotency-index regression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_master_index_query_returns_real_index_definition():
    """The EXACT query shape tests/test_workspace_proposals.py::
    test_add_workspace_proposal_idempotency_key_unique_index_exists issues,
    translated to a real ``pg_indexes`` lookup. Must return the real
    ``indexdef`` text (containing UNIQUE), not the old task_log placeholder.
    """
    db, pool = _pg([
        (
            "pg_indexes",
            [{
                "indexname": "idx_workspace_proposals_idempotency",
                "indexdef": (
                    "CREATE UNIQUE INDEX idx_workspace_proposals_idempotency "
                    "ON workspace_proposals USING btree (COALESCE(tenant_id, ''), "
                    "idempotency_key) WHERE (idempotency_key IS NOT NULL)"
                ),
            }],
        ),
    ])

    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' "
        "AND name = 'idx_workspace_proposals_idempotency'"
    ) as cur:
        row = await cur.fetchone()

    assert row is not None
    assert row["name"] == "idx_workspace_proposals_idempotency"
    assert "UNIQUE" in row["sql"].upper()
    # Query-aware, not fabricated: the real index name was actually sent as
    # a bound parameter to the pg_indexes lookup.
    assert pool.calls[-1][1] == ("idx_workspace_proposals_idempotency",)


@pytest.mark.asyncio
async def test_sqlite_master_index_query_not_found_returns_none():
    """A negative case: an index that does not exist must resolve to "not
    found" (fetchone() is None) -- never a false-positive fabricated row."""
    db, _pool = _pg([])  # no configured pg_indexes match -> empty result

    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' "
        "AND name = 'idx_does_not_exist'"
    ) as cur:
        row = await cur.fetchone()

    assert row is None


@pytest.mark.asyncio
async def test_sqlite_master_index_query_bound_param_placeholder():
    """The ``name = ?`` bound-parameter shape (used elsewhere in this
    codebase, e.g. tests/test_proposal_evidence_linkage.py against a raw
    aiosqlite connection) must resolve the same way as an inline literal."""
    db, pool = _pg([
        (
            "pg_indexes",
            [{"indexname": "idx_proposal_evidence_links_unique", "indexdef": "CREATE UNIQUE INDEX ..."}],
        ),
    ])

    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        ("idx_proposal_evidence_links_unique",),
    ) as cur:
        row = await cur.fetchone()

    assert row is not None
    assert row["name"] == "idx_proposal_evidence_links_unique"
    assert pool.calls[-1][1] == ("idx_proposal_evidence_links_unique",)


# ---------------------------------------------------------------------------
# type='table' -- another real sqlite_master consumer (356d6ac8's
# file_patch_counters migration guard, tests/test_356d6ac8_file_patch_counters.py::
# test_migration_creates_table) must ALSO get a correct, query-aware answer,
# not the old blanket task_log fake.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_master_table_query_positive():
    """The file_patch_counters existence-check shape: table exists."""
    db, pool = _pg([
        ("information_schema.tables", [{"table_name": "file_patch_counters"}]),
    ])

    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='file_patch_counters'"
    ) as cur:
        row = await cur.fetchone()

    assert row is not None
    assert row["name"] == "file_patch_counters"
    assert pool.calls[-1][1] == ("file_patch_counters",)


@pytest.mark.asyncio
async def test_sqlite_master_table_query_negative():
    """Same shape, table absent -- must be a real "not found", not a
    fabricated match."""
    db, _pool = _pg([])  # no information_schema.tables match

    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='does_not_exist'"
    ) as cur:
        row = await cur.fetchone()

    assert row is None


@pytest.mark.asyncio
async def test_sqlite_master_table_sql_column_uses_real_catalog_columns():
    """The ``SELECT sql FROM sqlite_master WHERE type='table' AND name=...``
    shape (meridian/db/migrations.py's legacy-CHECK-constraint detectors,
    e.g. the workspace_members / task_log rebuild guards). These guards never
    actually run against a Postgres connection in production (Postgres gets
    its schema from pg_adapter.py's own DDL literals, not migrations.py's
    guarded ``_migrate_*`` helpers -- meridian/db/__init__.py::init_db routes
    postgresql:// URLs straight to init_pg_db, bypassing them entirely), but
    the translation must still answer honestly from real catalog data rather
    than returning the old unrelated task_log placeholder if ever reached.
    """
    db, pool = _pg([
        ("information_schema.tables", [{"table_name": "workspace_members"}]),
        (
            "information_schema.columns",
            [
                {"column_name": "id", "data_type": "text"},
                {"column_name": "role", "data_type": "text"},
            ],
        ),
    ])

    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='workspace_members'"
    ) as cur:
        row = await cur.fetchone()

    assert row is not None
    assert row["name"] == "workspace_members"
    # Real column data made it into the reconstructed text -- not a fixed string.
    assert "id" in row["sql"]
    assert "role" in row["sql"]
    assert "task_log" not in row["sql"]
    assert "pending-hitl" not in row["sql"]


# ---------------------------------------------------------------------------
# The regression itself: no query, recognized or not, may ever return the
# old hard-coded task_log placeholder.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_master_never_returns_old_task_log_placeholder():
    """Direct regression guard for the bug report: PostgresConnection.execute()
    must never again return the fixed
    ``{"sql": "pending-hitl 'backlog' 'backburner'", "name": "task_log"}``
    row for an sqlite_master query, no matter what table/index was asked
    about."""
    db, _pool = _pg([
        (
            "pg_indexes",
            [{"indexname": "some_other_index", "indexdef": "CREATE INDEX some_other_index ON t(c)"}],
        ),
    ])

    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'some_other_index'"
    ) as cur:
        row = await cur.fetchone()

    assert row is not None
    assert row["name"] != "task_log"
    assert "pending-hitl" not in (row["sql"] or "")


@pytest.mark.asyncio
async def test_sqlite_master_unrecognized_shape_returns_empty_not_fabricated():
    """A query shape this codebase does not actually issue through the
    Postgres backend (e.g. bulk-listing every sqlite_master row, only ever
    used against a raw SQLite connection directly -- see
    tests/test_fixture_performance.py -- never through PostgresConnection)
    must resolve to an EMPTY result, never the old fabricated row and never
    a crash."""
    db, _pool = _pg([])

    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'index', 'trigger', 'view')"
    ) as cur:
        rows = await cur.fetchall()

    assert rows == []


# ---------------------------------------------------------------------------
# SQLite parity: the same idempotency-index query the workspace-proposals
# test issues against Postgres (mocked above) must find the SAME real index
# when asked directly of a real SQLite connection -- proving neither backend
# lies about the index's existence, just answered from a different catalog.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_master_index_parity_with_real_sqlite_backend():
    """Real aiosqlite connection, real schema (via init_db) -- confirms the
    UNIQUE idx_workspace_proposals_idempotency index this whole regression is
    about genuinely exists on the SQLite side too, so the Postgres-side mock
    coverage above is checking the two backends agree, not just checking the
    mock in isolation."""
    from meridian import db as db_module

    conn = await db_module.init_db(":memory:")
    try:
        async with conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_workspace_proposals_idempotency'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        sql = row["sql"] if isinstance(row, dict) else row[0]
        assert "UNIQUE" in (sql or "").upper()
    finally:
        await conn.close()
