"""Regression coverage for worker-local test resources and SQLite cloning."""

from __future__ import annotations

import os
import sqlite3

import pytest

import conftest


async def _table_names(conn) -> set[str]:
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger', 'view')"
    )
    return {row[0] for row in await cursor.fetchall()}


@pytest.mark.asyncio
async def test_sqlite_template_copy_is_isolated_and_schema_complete(
    _sqlite_schema_template,
):
    """Each clone starts empty but has the complete migrated schema."""
    from meridian import db as db_module

    first = await conftest._open_sqlite_from_template(_sqlite_schema_template)
    second = await conftest._open_sqlite_from_template(_sqlite_schema_template)
    reference = await db_module.init_db(":memory:")
    try:
        await db_module.create_project(first, "template-only-project")
        projects = await db_module.list_projects(second)
        assert projects == []
        assert await _table_names(first) == await _table_names(reference)
        assert await _table_names(second) == await _table_names(reference)
    finally:
        await first.close()
        await second.close()
        await reference.close()


@pytest.mark.asyncio
async def test_sqlite_template_copy_preserves_connection_pragmas(
    _sqlite_schema_template,
):
    conn = await conftest._open_sqlite_from_template(_sqlite_schema_template)
    try:
        foreign_keys = await (await conn.execute("PRAGMA foreign_keys")).fetchone()
        busy_timeout = await (await conn.execute("PRAGMA busy_timeout")).fetchone()
        assert foreign_keys[0] == 1
        assert busy_timeout[0] == 5000
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_sqlite_template_copy_fails_closed_for_missing_template(tmp_path):
    with pytest.raises((OSError, sqlite3.Error)):
        await conftest._open_sqlite_from_template(tmp_path / "missing-schema.db")

    empty_template = tmp_path / "empty-schema.db"
    sqlite3.connect(empty_template).close()
    with pytest.raises(RuntimeError, match="template is empty"):
        await conftest._open_sqlite_from_template(empty_template)
    # The failed source connection is closed by the helper (important on Windows,
    # where an open SQLite handle would prevent this cleanup).
    empty_template.unlink()


def test_postgres_worker_names_are_distinct_and_unsuffixed_without_xdist(
    monkeypatch,
):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    assert conftest._worker_db_name("meridian_test") == "meridian_test"

    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    first = conftest._worker_db_name("meridian_test")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    second = conftest._worker_db_name("meridian_test")
    assert first == "meridian_test_gw0"
    assert second == "meridian_test_gw1"
    assert first != second


def test_pg_worker_name_sanitizes_identifier_characters(monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "worker-a/b")
    assert conftest._worker_db_name("meridian_test") == "meridian_test_worker_a_b"


def test_worker_template_path_is_under_pytest_temp_root(
    _sqlite_schema_template, tmp_path_factory
):
    template = os.path.abspath(_sqlite_schema_template)
    temp_root = os.path.abspath(str(tmp_path_factory.getbasetemp()))
    assert template.startswith(temp_root + os.sep)
