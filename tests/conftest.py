"""Shared pytest fixtures for Meridian's test suite.

Backend selection (keystone 98aa7eb7 — Postgres correctness is non-negotiable,
pinned decision f70b731a):

* By default the whole suite runs on **SQLite** (in-memory) so local
  ``pixi run test`` stays fast and dependency-free.
* When ``TEST_DATABASE_URL`` (or ``DATABASE_URL``) is set in the environment,
  the ``db`` fixture — and the ``client`` fixture's FastAPI app — instead run
  against a **real Postgres** database via the existing ``meridian.pg_adapter``
  psycopg3 path.  CI (see ``.github/workflows/test.yml``) runs the suite a
  second time this way against a ``postgres:16`` service container so that
  Postgres-only bugs (e.g. the b7f41c73 datetime-serialization regression, which
  SQLite ``:memory:`` hid) are caught before they reach prod.

Fixtures:

* ``db``    — Meridian's schema on the active backend.  In-memory aiosqlite by
              default; a fresh Postgres schema per test when ``TEST_DATABASE_URL``
              is set.
* ``db_pg`` — Postgres connection.  Skipped unless ``TEST_DATABASE_URL`` is set.
* ``anydb`` — parametrized fixture that yields both ``db`` (SQLite) and ``db_pg``
              (useful for tests that should pass on both backends).
* ``client`` — FastAPI TestClient.  Backed by in-memory SQLite by default, or the
               ``TEST_DATABASE_URL`` Postgres DB when that env var is set.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Backend selection helpers
# ---------------------------------------------------------------------------

def _base_pg_url() -> str | None:
    """Return the raw Postgres URL the suite should target, or ``None`` for SQLite.

    ``TEST_DATABASE_URL`` is preferred (explicit, test-only) but ``DATABASE_URL``
    is accepted as a fallback so CI service-container conventions work with no
    extra wiring.  Only ``postgres[ql]://`` URLs activate the Postgres path; any
    other value (or absence) keeps the default SQLite backend.
    """
    for key in ("TEST_DATABASE_URL", "DATABASE_URL"):
        url = os.environ.get(key)
        if url and url.startswith(("postgresql://", "postgres://")):
            return url
    return None


def pytest_configure(config):
    # 98aa7eb7 — let tests that are inherently SQLite-specific (sqlite_master /
    # PRAGMA schema introspection, with no Postgres analog) opt out of the PG run.
    config.addinivalue_line(
        "markers",
        "sqlite_only: SQLite-specific test (sqlite_master/PRAGMA); skipped on the Postgres backend",
    )


def pytest_collection_modifyitems(config, items):
    # On the Postgres run (TEST_DATABASE_URL points at PG), skip @pytest.mark.sqlite_only
    # tests — they assert against SQLite-only schema introspection. The default SQLite
    # run is unaffected.
    if _base_pg_url() is None:
        return
    skip_pg = pytest.mark.skip(reason="SQLite-specific test; not run on the Postgres backend")
    for item in items:
        if "sqlite_only" in item.keywords:
            item.add_marker(skip_pg)


def _worker_db_name(base_db: str) -> str:
    """Per-xdist-worker database name so ``-n auto`` shards don't collide.

    Under ``pytest -n auto`` each xdist worker runs in its own process
    (``PYTEST_XDIST_WORKER`` = ``gw0``, ``gw1``, …).  Because each test drops and
    recreates the whole ``public`` schema for isolation, workers sharing one
    database would destroy each other's tables mid-test.  Giving every worker its
    own database (``<base>_gw0`` …) makes the DROP/CREATE SCHEMA safe and keeps
    workers fully independent.  With no xdist (``PYTEST_XDIST_WORKER`` unset) the
    base database is used unchanged.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not worker:
        return base_db
    # Keep it a valid unquoted identifier: letters/digits/underscore only.
    safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in worker)
    return f"{base_db}_{safe}"


def _split_pg_url(url: str) -> tuple[str, str]:
    """Split a Postgres URL into ``(base_without_dbname, dbname)``.

    ``postgresql://u:p@host:5432/meridian_test?sslmode=…`` → the leading portion
    up to (but not including) the ``/dbname`` and the bare database name (query
    string stripped).  Used to swap in a per-worker database name.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    dbname = parts.path.lstrip("/").split("/")[0]
    # Reassemble scheme://netloc without the path so we can append /<db>.
    prefix = f"{parts.scheme}://{parts.netloc}"
    return prefix, dbname


def _pg_test_url() -> str | None:
    """Postgres URL for the active xdist worker, or ``None`` for SQLite.

    Rewrites the base ``TEST_DATABASE_URL``/``DATABASE_URL`` to point at this
    worker's dedicated database (see :func:`_worker_db_name`).  Any query string
    (e.g. ``?sslmode=require``) on the base URL is dropped — the CI service
    container is plain TCP and psycopg's channel-binding params are stripped
    anyway by the adapter.
    """
    base = _base_pg_url()
    if not base:
        return None
    prefix, dbname = _split_pg_url(base)
    return f"{prefix}/{_worker_db_name(dbname)}"


async def _ensure_worker_db_exists() -> None:
    """Create this worker's database if it doesn't already exist.

    Connects to the maintenance ``postgres`` database on the same server and runs
    ``CREATE DATABASE`` for the worker DB.  Idempotent: races between workers (or a
    pre-existing DB) are swallowed.  No-op when not targeting Postgres.
    """
    base = _base_pg_url()
    if not base:
        return

    import psycopg  # local import: keeps the SQLite path free of psycopg

    from meridian.pg_adapter import _strip_unsupported_pg_query_params

    prefix, base_db = _split_pg_url(base)
    worker_db = _worker_db_name(base_db)
    admin_url = _strip_unsupported_pg_query_params(f"{prefix}/postgres")
    async with await psycopg.AsyncConnection.connect(
        admin_url, autocommit=True
    ) as conn:
        cur = await conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (worker_db,)
        )
        exists = await cur.fetchone()
        if not exists:
            try:
                # CREATE DATABASE can't be parameterized or run in a txn; the
                # name comes from PYTEST_XDIST_WORKER (sanitized) so it's safe.
                await conn.execute(f'CREATE DATABASE "{worker_db}"')
            except psycopg.errors.DuplicateDatabase:
                pass  # another worker won the race — fine.


async def _reset_pg_schema(url: str) -> None:
    """Drop and recreate the ``public`` schema so each test starts fresh.

    This is the Postgres analogue of SQLite's throwaway ``:memory:`` DB: rather
    than maintain a hand-curated ``DELETE FROM <table>`` list (which silently
    rots as tables are added), we blow away the whole schema and let
    ``init_db`` rebuild the current schema from ``CREATE_TABLES_*`` + migrations.
    That guarantees full schema parity with prod on every test, and complete
    isolation between tests sharing one (per-worker) database.

    Uses a short-lived psycopg3 connection (autocommit) opened directly on the
    running test's event loop — deliberately *not* the pooled adapter, since we
    need to run DDL before the schema (and pool) exist.
    """
    import psycopg  # local import: keeps the SQLite path free of psycopg

    from meridian.pg_adapter import _strip_unsupported_pg_query_params

    clean_url = _strip_unsupported_pg_query_params(url)
    async with await psycopg.AsyncConnection.connect(
        clean_url, autocommit=True
    ) as conn:
        # CASCADE drops every table/sequence/type owned by the schema; recreating
        # it immediately restores an empty namespace on the default search_path.
        await conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        await conn.execute("CREATE SCHEMA public")


async def _open_fresh_pg_db(url: str):
    """Reset the Postgres schema, then build Meridian's schema and return the conn."""
    from meridian import db as db_module

    await _ensure_worker_db_exists()
    await _reset_pg_schema(url)
    return await db_module.init_db(url)


@pytest.fixture
def event_loop_policy():
    """Event-loop policy pytest-asyncio uses to build each test's loop.

    On Windows the entry-point modules (``meridian.__main__`` /
    ``meridian.tunnel_main``) set the *global* asyncio policy to
    ``WindowsSelectorEventLoopPolicy`` at import time — needed at runtime for async
    psycopg, but a Windows SelectorEventLoop cannot spawn subprocesses
    (``asyncio.create_subprocess_exec`` raises ``NotImplementedError``). The enqueue
    worker tests spawn subprocesses, so whether an xdist worker happened to import
    one of those entry-point modules flipped those tests between pass and fail —
    a real, pre-existing xdist-sharding flake (f73810d5 made Selector more prevalent
    and unmasked it).

    Pin the Proactor policy for test loops on Windows so subprocess spawning is
    deterministic. That is already the Windows asyncio *default*; the entry-point
    modules only override it for real psycopg, which is Linux-only in CI and skipped
    locally (``db_pg`` needs ``TEST_DATABASE_URL``), so nothing exercised locally
    needs Selector. No-op off Windows — return the default policy and never touch
    the Windows-only symbol there (keeps Linux CI import-safe).
    """
    if sys.platform == "win32":
        return asyncio.WindowsProactorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest_asyncio.fixture
async def db():
    """Meridian's schema on the active backend, isolated per test.

    Default: a fresh in-memory SQLite connection.  When ``TEST_DATABASE_URL``
    (or ``DATABASE_URL``) points at Postgres, the schema is dropped/recreated and
    rebuilt via ``init_db`` so the entire suite exercises the real prod backend.
    """
    from meridian import db as db_module

    pg_url = _pg_test_url()
    if pg_url:
        conn = await _open_fresh_pg_db(pg_url)
    else:
        conn = await db_module.init_db(":memory:")
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def db_pg():
    """Fresh Postgres connection — skipped unless TEST_DATABASE_URL is set."""
    url = _pg_test_url()
    if not url:
        pytest.skip("TEST_DATABASE_URL not set — skipping Postgres test")

    conn = await _open_fresh_pg_db(url)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def anydb(request):
    """SQLite *or* Postgres DB — parametrized so tests run on both backends.

    The 'postgres' variant is automatically skipped when TEST_DATABASE_URL
    is not set in the environment, so the suite stays green locally with
    SQLite only.
    """
    from meridian import db as db_module

    if request.param == "sqlite":
        conn = await db_module.init_db(":memory:")
        try:
            yield conn
        finally:
            await conn.close()
    else:
        url = _pg_test_url()
        if not url:
            pytest.skip("TEST_DATABASE_URL not set — skipping Postgres variant")
        conn = await _open_fresh_pg_db(url)
        try:
            yield conn
        finally:
            await conn.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """FastAPI TestClient backed by SQLite (default) or Postgres (TEST_DATABASE_URL).

    When ``TEST_DATABASE_URL`` (or ``DATABASE_URL``) targets Postgres, the schema
    is reset and the server lifespan is pointed at that URL via ``MERIDIAN_DB_URL``
    so the whole app — routes, MCP handlers, lifespan — runs on real Postgres in
    CI.  Otherwise it uses an in-memory SQLite DB and a temp data dir, unchanged.
    """
    pg_url = _pg_test_url()

    if pg_url:
        # Reset the schema on a throwaway loop before the app boots so the server
        # lifespan sees a fresh, current schema (init_db is idempotent, but a
        # DROP/CREATE guarantees isolation from the previous client test). Ensure
        # this worker's database exists first (no-op if already created).
        async def _prepare() -> None:
            await _ensure_worker_db_exists()
            await _reset_pg_schema(pg_url)

        asyncio.run(_prepare())
        # Point the lifespan at Postgres. MERIDIAN_DB_URL wins over MERIDIAN_DB.
        monkeypatch.setenv("MERIDIAN_DB_URL", pg_url)
        # MERIDIAN_DB must not be ":memory:" or the toml-profile block is skipped;
        # a non-memory sentinel keeps the URL override active without touching disk.
        monkeypatch.setenv("MERIDIAN_DB", str(tmp_path / "unused.db"))
    else:
        monkeypatch.setenv("MERIDIAN_DB", ":memory:")
        # Block load_dotenv(override=False) from injecting a real MERIDIAN_DB_URL
        # from a local .env file — the key must already be present so dotenv skips
        # it. An empty string is falsy, so the lifespan still takes the SQLite path.
        monkeypatch.setenv("MERIDIAN_DB_URL", "")

    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    # v2.2 — also block MERIDIAN_DEMO_DB_URL so the lifespan doesn't try to
    # connect to Neon and seed demo data during tests (would hang on every
    # client fixture if a .env file with a real demo URL is present).
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    # Skip the in-memory demo DB fallback so tests that send the demo cookie
    # get a proper 503 (security guard) rather than routing to an unexpected
    # in-memory DB.  Tests that need demo data should use the demo_client fixture.
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    # v0.6.3 — redirect GOAL.md into the same temp dir so test
    # writebacks don't touch the repo's real file.
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    # v3.3 — redirect the markdown-anchor root into the temp dir so DEVLOG/
    # DECISIONS/ROADMAP/CLAUDE/AGENTS auto-updates (and the checkpoint git
    # commit) never touch — or commit — the real repo docs during tests.
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))

    # Import after env vars are set so the module sees them.
    from fastapi.testclient import TestClient

    # Force a fresh import so lifespan picks up the env vars cleanly.
    import importlib
    import meridian.server as server_module

    server_module = importlib.reload(server_module)

    with TestClient(server_module.app) as c:
        yield c
