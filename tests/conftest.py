"""Shared pytest fixtures for Meridian's test suite.

Fixtures:

* ``db``    — in-memory aiosqlite connection (SQLite, always available).
* ``db_pg`` — asyncpg-backed PostgresConnection.  Skipped unless
              ``TEST_DATABASE_URL`` is set in the environment.
* ``anydb`` — parametrized fixture that yields both ``db`` and ``db_pg``
              (useful for tests that should pass on both backends).
* ``client`` — FastAPI TestClient backed by an in-memory SQLite DB.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def db():
    """Fresh in-memory SQLite connection with Meridian's schema applied."""
    from meridian import db as db_module

    conn = await db_module.init_db(":memory:")
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def db_pg():
    """Fresh Postgres connection — skipped unless TEST_DATABASE_URL is set."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set — skipping Postgres test")

    from meridian import db as db_module

    conn = await db_module.init_db(url)
    # Wipe all tables before each test so tests are isolated.
    for table in (
        "chat_messages", "chat_sessions", "sprint_items",
        "task_log", "sessions", "goal_states", "projects",
    ):
        await conn.execute(f"DELETE FROM {table}")
    await conn.commit()
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
        url = os.environ.get("TEST_DATABASE_URL")
        if not url:
            pytest.skip("TEST_DATABASE_URL not set — skipping Postgres variant")
        conn = await db_module.init_db(url)
        for table in (
            "chat_messages", "chat_sessions", "sprint_items",
            "task_log", "sessions", "goal_states", "projects",
        ):
            await conn.execute(f"DELETE FROM {table}")
        await conn.commit()
        try:
            yield conn
        finally:
            await conn.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """FastAPI TestClient backed by an in-memory DB and a temp data dir."""
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    # Block load_dotenv(override=False) from injecting a real MERIDIAN_DB_URL
    # from a local .env file — the key must already be present so dotenv skips
    # it. An empty string is falsy, so the lifespan still takes the SQLite path.
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
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
