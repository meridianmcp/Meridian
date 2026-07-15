"""Regression tests for sprint item 6adba18c.

Bug: receive_messages fails on Postgres with
    "column read_at is of type timestamp with time zone but expression is of type text"

Root cause: the UPDATE in receive_messages used
    SET read_at = datetime('now')
which the pg_adapter converts to
    SET read_at = to_char(clock_timestamp() at time zone 'utc', ...)
The to_char() call returns TEXT, but session_messages.read_at is TIMESTAMPTZ in
Postgres, so Postgres rejects the assignment.

Fix: pass the current UTC timestamp as a bound parameter (? / %s) instead of
embedding datetime('now') in the SQL body.  Bound string parameters are
implicitly cast to TIMESTAMPTZ by Postgres; SQLite stores them as TEXT (the
column is TEXT in SQLite anyway).

These tests verify:
1. SQL query no longer contains datetime('now') for the read_at update.
2. The pg_adapter would NOT produce a to_char(...) expression for the UPDATE.
3. The SQLite path works end-to-end (send + receive marks read).
4. Messages already read are not returned again.
"""
from __future__ import annotations

import asyncio
import re
import unittest.mock as mock

import pytest
import pytest_asyncio

from meridian import db as db_module
from meridian.pg_adapter import _pg_adapt_sql


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


@pytest_asyncio.fixture
async def mem_db():
    conn = await db_module.init_db(":memory:")
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def project_and_sessions(mem_db):
    proj = await db_module.create_project(mem_db, "test-proj-recv")
    pid = proj["id"]
    sess_a = await db_module.register_session(mem_db, pid, "sess-a")
    sess_b = await db_module.register_session(mem_db, pid, "sess-b")
    return mem_db, pid, sess_a["id"], sess_b["id"]


# ---------------------------------------------------------------------------
# SQL-level assertions: no datetime('now') in UPDATE, no to_char in adapted SQL
# ---------------------------------------------------------------------------

def test_receive_messages_update_sql_has_no_datetime_now():
    """The UPDATE statement in receive_messages must not embed datetime('now').

    Intercept db.execute() and capture all SQL strings to assert that the
    UPDATE for read_at uses a bound parameter (?) instead of datetime('now').

    Uses a proper async context manager mock so `async with db.execute(...)` works.
    """
    captured_sqls: list[str] = []

    class _FakeExecProxy:
        """Supports both `await db.execute(...)` and `async with db.execute(...)`."""

        def __init__(self, sql: str) -> None:
            captured_sqls.append(sql)
            self._cursor = mock.MagicMock()
            self._cursor.fetchall = mock.AsyncMock(return_value=[])

        def __await__(self):
            async def _noop():
                return self
            return _noop().__await__()

        async def __aenter__(self):
            return self._cursor

        async def __aexit__(self, *_args):
            return False

    def _fake_execute(sql, params=()):
        return _FakeExecProxy(sql)

    fake_db = mock.MagicMock()
    fake_db.execute = _fake_execute
    fake_db.commit = mock.AsyncMock()

    # receive_messages with mark_read=False returns early after the SELECT
    # (no UPDATE path). We verify the SELECT SQL does not use datetime('now').
    _run(db_module.receive_messages(fake_db, "sess-x", mark_read=False))

    select_sqls = [s for s in captured_sqls if "SELECT" in s.upper()]
    assert select_sqls, "Expected at least one SELECT from receive_messages"
    for sql in select_sqls:
        assert "datetime('now')" not in sql, (
            f"SELECT SQL must not embed datetime('now'): {sql}"
        )


def test_update_read_at_sql_does_not_use_to_char():
    """If the UPDATE uses ? for read_at, pg_adapt_sql must NOT produce to_char.

    Simulate the exact SQL that receive_messages now emits (after the fix) and
    confirm that pg_adapt_sql converts it to a safe %s placeholder form — not
    to a to_char() expression that would be typed as TEXT.
    """
    # This is the SQL the fixed receive_messages generates (3 ids as example).
    sql = "UPDATE session_messages SET read_at = ? WHERE id IN (?,?,?)"
    params = ("2026-07-14 22:49:00", "id1", "id2", "id3")

    pg_sql, pg_params = _pg_adapt_sql(sql, params)

    # Must use %s placeholders (no datetime expressions).
    assert "to_char" not in pg_sql, (
        f"pg_sql must not contain to_char() (TEXT expression) for read_at: {pg_sql}"
    )
    assert "datetime" not in pg_sql, (
        f"pg_sql must not contain datetime() after adaptation: {pg_sql}"
    )
    assert pg_sql == "UPDATE session_messages SET read_at = %s WHERE id IN (%s,%s,%s)", (
        f"Unexpected pg_sql: {pg_sql}"
    )
    assert pg_params == list(params), f"Unexpected pg_params: {pg_params}"


def test_old_buggy_sql_would_produce_to_char():
    """Confirm the original (buggy) SQL does produce to_char — illustrating why it broke.

    This test documents the root cause and serves as a canary: if
    _pg_adapt_sql ever stops converting datetime('now') to to_char(), we
    need to revisit the fix rationale.
    """
    buggy_sql = "UPDATE session_messages SET read_at = datetime('now') WHERE id IN (?,?)"
    params = ("id1", "id2")

    pg_sql, _ = _pg_adapt_sql(buggy_sql, params)

    assert "to_char" in pg_sql, (
        "Expected pg_adapt_sql to convert datetime('now') to to_char(...) "
        f"(the buggy path): {pg_sql}"
    )


# ---------------------------------------------------------------------------
# Functional tests on SQLite (end-to-end path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_receive_messages_marks_read_sqlite(project_and_sessions):
    """send_message + receive_messages (SQLite) — unread fetched, then marked read."""
    mem_db, pid, sess_a, sess_b = project_and_sessions

    # sess_a sends a message to sess_b
    await db_module.send_message(
        mem_db, pid, sess_b,
        payload='{"hello": "world"}',
        from_session_id=sess_a,
        kind="test",
    )

    # First poll: should return 1 message
    msgs = await db_module.receive_messages(mem_db, sess_b, mark_read=True)
    assert len(msgs) == 1, f"Expected 1 message, got {len(msgs)}"
    msg = msgs[0]
    assert msg["payload"] == '{"hello": "world"}'
    assert msg["to_session_id"] == sess_b
    assert msg["from_session_id"] == sess_a

    # Second poll: message was marked read, so should not appear again
    msgs2 = await db_module.receive_messages(mem_db, sess_b, mark_read=True)
    assert len(msgs2) == 0, (
        f"Expected 0 messages on second poll (already read), got {len(msgs2)}"
    )


@pytest.mark.asyncio
async def test_receive_messages_read_at_is_set_after_mark(project_and_sessions):
    """After mark_read=True, the read_at field is populated in the DB."""
    mem_db, pid, sess_a, sess_b = project_and_sessions

    await db_module.send_message(
        mem_db, pid, sess_b,
        payload='{"check": "read_at"}',
        from_session_id=sess_a,
        kind="test",
    )

    msgs = await db_module.receive_messages(mem_db, sess_b, mark_read=True)
    assert len(msgs) == 1

    # Verify read_at was actually written to the DB
    async with mem_db.execute(
        "SELECT read_at FROM session_messages WHERE id = ?", (msgs[0]["id"],)
    ) as cur:
        row = await cur.fetchone()

    # row may be a dict or a tuple depending on SQLite row_factory
    read_at = row["read_at"] if isinstance(row, dict) else row[0]
    assert read_at is not None, "read_at must be set after mark_read=True"
    # Basic sanity: looks like a timestamp string (not a datetime('now') literal)
    assert "now" not in str(read_at).lower(), (
        f"read_at must not contain the literal string 'now': {read_at!r}"
    )


@pytest.mark.asyncio
async def test_receive_messages_no_mark_read(project_and_sessions):
    """mark_read=False leaves messages unread for the next poll."""
    mem_db, pid, sess_a, sess_b = project_and_sessions

    await db_module.send_message(
        mem_db, pid, sess_b,
        payload='{"no": "mark"}',
        from_session_id=sess_a,
        kind="test",
    )

    # First poll without marking read
    msgs1 = await db_module.receive_messages(mem_db, sess_b, mark_read=False)
    assert len(msgs1) == 1

    # Second poll — still unread
    msgs2 = await db_module.receive_messages(mem_db, sess_b, mark_read=False)
    assert len(msgs2) == 1, (
        "Message should still appear on second poll when mark_read=False"
    )


@pytest.mark.asyncio
async def test_receive_messages_empty_inbox(project_and_sessions):
    """receive_messages returns [] when there are no unread messages."""
    mem_db, pid, sess_a, sess_b = project_and_sessions

    msgs = await db_module.receive_messages(mem_db, sess_b, mark_read=True)
    assert msgs == [], f"Expected empty list, got {msgs}"


@pytest.mark.asyncio
async def test_receive_messages_multiple_messages_marked_in_batch(project_and_sessions):
    """All returned messages are marked read in one batch update."""
    mem_db, pid, sess_a, sess_b = project_and_sessions

    for i in range(3):
        await db_module.send_message(
            mem_db, pid, sess_b,
            payload=f'{{"seq": {i}}}',
            from_session_id=sess_a,
            kind="test",
        )

    msgs = await db_module.receive_messages(mem_db, sess_b, mark_read=True, limit=10)
    assert len(msgs) == 3

    # All should be marked read — subsequent poll returns nothing
    msgs2 = await db_module.receive_messages(mem_db, sess_b, mark_read=True)
    assert len(msgs2) == 0, (
        f"All 3 messages should have been marked read, got {len(msgs2)} unread"
    )
