"""Shared pytest fixtures for Meridian's test suite.

Two fixtures:

* ``db`` — an in-memory aiosqlite connection with schema applied. Async,
  one connection per test.
* ``client`` — a FastAPI :class:`fastapi.testclient.TestClient` wired up
  to use an in-memory DB via ``MERIDIAN_DB=:memory:``. Sync, since
  TestClient is sync.
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


@pytest.fixture
def client(tmp_path, monkeypatch):
    """FastAPI TestClient backed by an in-memory DB and a temp data dir."""
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))

    # Import after env vars are set so the module sees them.
    from fastapi.testclient import TestClient

    # Force a fresh import so lifespan picks up the env vars cleanly.
    import importlib
    import meridian.server as server_module

    server_module = importlib.reload(server_module)

    with TestClient(server_module.app) as c:
        yield c
