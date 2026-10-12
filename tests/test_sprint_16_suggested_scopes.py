"""Regression tests for 7b6b1edf -- suggested_scopes on checkpoint().

checkpoint() now returns ``suggested_scopes``: this session's task_log rows
clustered by resource overlap (linked sprint item touches_resources first,
session_activity file paths as the fallback). The field is advisory: it never
blocks, never writes, and an explicit ``scope_task_ids`` argument overrides
which rows are clustered.

Covers: meridian/db/__init__.py (suggest_session_scopes,
_activity_file_resources), meridian/mcp/handlers/session_tools.py
(handle_checkpoint), and meridian/mcp_tools.py (checkpoint inputSchema).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from meridian import db as db_module
from meridian.mcp.handlers import session_tools as st_mod


async def _task(db, sid, pid, desc, *, created_at, sprint_item_id=None, parent_task_id=None):
    """Log a done task and pin its created_at so window attribution is deterministic."""
    row = await db_module.log_task(
        db, sid, pid, desc, "done",
        parent_task_id=parent_task_id, sprint_item_id=sprint_item_id,
    )
    await db.execute(
        "UPDATE task_log SET created_at = ? WHERE id = ?", (created_at, row["id"])
    )
    return row["id"]


async def _activity(db, sid, recorded_at, summary):
    await db.execute(
        "INSERT INTO session_activity (id, session_id, tool_name, summary, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (uuid.uuid4().hex, sid, "claim_file", summary, recorded_at),
    )


async def _item(db, pid, title, touches):
    item = await db_module.add_sprint_item(
        db, pid, "v1", title, touches_resources=touches
    )
    return item["id"]


@pytest.fixture
async def ctx(db):
    project = await db_module.create_project(db, "suggested-scopes")
    session = await db_module.register_session(db, project["id"], "scopes-session")
    return db, project["id"], session["id"]


async def test_linked_items_cluster_by_declared_resources(ctx):
    db, pid, sid = ctx
    a = await _item(db, pid, "A", ["file:meridian/a.py"])
    b = await _item(db, pid, "B", ["file:meridian/a.py", "file:meridian/b.py"])
    c = await _item(db, pid, "C", ["file:meridian/c.py"])
    t1 = await _task(db, sid, pid, "one", sprint_item_id=a, created_at="2026-10-01 10:00:00")
    t2 = await _task(db, sid, pid, "two", sprint_item_id=b, created_at="2026-10-01 10:01:00")
    t3 = await _task(db, sid, pid, "three", sprint_item_id=c, created_at="2026-10-01 10:02:00")

    scopes = await db_module.suggest_session_scopes(db, pid, sid)

    assert scopes == [
        {
            "task_ids": [t1, t2],
            "resources": ["file:meridian/a.py", "file:meridian/b.py"],
            "basis": ["sprint_item"],
        },
        {"task_ids": [t3], "resources": ["file:meridian/c.py"], "basis": ["sprint_item"]},
    ]


async def test_file_claim_bridges_disjoint_symbols_into_one_scope(ctx):
    """Two different symbols in one file do not conflict with each other, but a
    whole-file claim conflicts with both, so the connected component is one scope."""
    db, pid, sid = ctx
    f = await _item(db, pid, "F", ["symbol:meridian/x.py::f"])
    g = await _item(db, pid, "G", ["symbol:meridian/x.py::g"])
    whole = await _item(db, pid, "W", ["file:meridian/x.py"])
    t1 = await _task(db, sid, pid, "f", sprint_item_id=f, created_at="2026-10-01 10:00:00")
    t2 = await _task(db, sid, pid, "g", sprint_item_id=g, created_at="2026-10-01 10:01:00")
    t3 = await _task(db, sid, pid, "w", sprint_item_id=whole, created_at="2026-10-01 10:02:00")

    scopes = await db_module.suggest_session_scopes(db, pid, sid)

    assert len(scopes) == 1
    assert scopes[0]["task_ids"] == [t1, t2, t3]


async def test_unlinked_rows_take_files_from_activity_in_their_window(ctx):
    db, pid, sid = ctx
    t1 = await _task(db, sid, pid, "one", created_at="2026-10-01 10:00:00")
    t2 = await _task(db, sid, pid, "two", created_at="2026-10-01 10:01:00")
    t3 = await _task(db, sid, pid, "three", created_at="2026-10-01 10:02:00")
    await _task(db, sid, pid, "four", created_at="2026-10-01 10:03:00")  # no activity
    # t1 owns everything up to 10:00:00 (including earlier activity), t2 owns
    # (10:00:00, 10:01:00], t3 owns (10:01:00, 10:02:00].
    await _activity(db, sid, "2026-10-01 09:59:30", "file_path=meridian/a.py")
    await _activity(db, sid, "2026-10-01 10:00:30", "file_path=meridian/a.py")
    await _activity(db, sid, "2026-10-01 10:01:30", "file_path=meridian/z.py")

    scopes = await db_module.suggest_session_scopes(db, pid, sid)

    assert scopes == [
        {"task_ids": [t1, t2], "resources": ["file:meridian/a.py"], "basis": ["session_activity"]},
        {"task_ids": [t3], "resources": ["file:meridian/z.py"], "basis": ["session_activity"]},
    ]


async def test_linked_item_without_resources_falls_back_to_activity(ctx):
    db, pid, sid = ctx
    bare = await _item(db, pid, "Bare", None)
    t1 = await _task(db, sid, pid, "one", sprint_item_id=bare, created_at="2026-10-01 10:00:00")
    await _activity(db, sid, "2026-10-01 09:59:00", "file_path=meridian/q.py")

    scopes = await db_module.suggest_session_scopes(db, pid, sid)

    assert scopes == [
        {"task_ids": [t1], "resources": ["file:meridian/q.py"], "basis": ["session_activity"]},
    ]


async def test_bucketed_rows_excluded_by_default_and_override_selects_them(ctx):
    db, pid, sid = ctx
    a = await _item(db, pid, "A", ["file:meridian/a.py"])
    b = await _item(db, pid, "B", ["file:meridian/a.py"])
    t1 = await _task(db, sid, pid, "parent", sprint_item_id=a, created_at="2026-10-01 10:00:00")
    t2 = await _task(
        db, sid, pid, "child", sprint_item_id=b,
        created_at="2026-10-01 10:01:00", parent_task_id=t1,
    )

    default = await db_module.suggest_session_scopes(db, pid, sid)
    assert default == [
        {"task_ids": [t1], "resources": ["file:meridian/a.py"], "basis": ["sprint_item"]},
    ]

    overridden = await db_module.suggest_session_scopes(db, pid, sid, scope_task_ids=[t2])
    assert overridden == [
        {"task_ids": [t2], "resources": ["file:meridian/a.py"], "basis": ["sprint_item"]},
    ]


async def test_no_candidates_or_unknown_override_ids_yield_nothing(ctx):
    db, pid, sid = ctx
    assert await db_module.suggest_session_scopes(db, pid, sid) == []

    await _task(db, sid, pid, "no resources", created_at="2026-10-01 10:00:00")
    assert await db_module.suggest_session_scopes(db, pid, sid) == []
    assert await db_module.suggest_session_scopes(
        db, pid, sid, scope_task_ids=["does-not-exist"]
    ) == []


def test_activity_parser_keeps_only_file_keys_and_dedupes():
    assert db_module._activity_file_resources("project_id=p1, file_path=meridian/a.py") == [
        "file:meridian/a.py"
    ]
    assert db_module._activity_file_resources(
        "file_path=meridian/a.py, path=meridian/a.py"
    ) == ["file:meridian/a.py"]
    assert db_module._activity_file_resources("description=hello there") == []
    assert db_module._activity_file_resources(None) == []


async def test_checkpoint_response_carries_suggested_scopes(ctx, monkeypatch, tmp_path):
    db, pid, sid = ctx
    a = await _item(db, pid, "A", ["file:meridian/a.py"])
    t1 = await _task(db, sid, pid, "one", sprint_item_id=a, created_at="2026-10-01 10:00:00")
    t2 = await _task(db, sid, pid, "two", sprint_item_id=a, created_at="2026-10-01 10:01:00")

    monkeypatch.setattr("meridian.server._finalize_session_md", AsyncMock(return_value=None))
    monkeypatch.setattr("meridian.db.auto_capture_session", AsyncMock(return_value=None))

    async def _fake_generate_handoff(*args, **kw):
        return (None, "scope summary", None)

    monkeypatch.setattr("meridian.handoff.generate_handoff", _fake_generate_handoff)

    async def _fake_commits(project_dict, tenant):
        return []

    def _fake_identity(tenant):
        return None

    kwargs = dict(fetch_recent_commits=_fake_commits, resolve_caller_identity=_fake_identity)
    result = await st_mod.handle_checkpoint(
        {"session_id": sid, "project_id": pid}, db, str(tmp_path), None, None, **kwargs,
    )
    expected = {"task_ids": [t1, t2], "resources": ["file:meridian/a.py"], "basis": ["sprint_item"]}
    assert result["suggested_scopes"] == [expected]

    narrowed = await st_mod.handle_checkpoint(
        {"session_id": sid, "project_id": pid, "scope_task_ids": [t2]},
        db, str(tmp_path), None, None, **kwargs,
    )
    assert narrowed["suggested_scopes"] == [
        {"task_ids": [t2], "resources": ["file:meridian/a.py"], "basis": ["sprint_item"]},
    ]


async def test_suggestion_failure_never_fails_checkpoint(ctx, monkeypatch, tmp_path):
    db, pid, sid = ctx

    monkeypatch.setattr("meridian.server._finalize_session_md", AsyncMock(return_value=None))
    monkeypatch.setattr("meridian.db.auto_capture_session", AsyncMock(return_value=None))

    async def _boom(*args, **kwargs):
        raise RuntimeError("scope derivation broke")

    monkeypatch.setattr("meridian.db.suggest_session_scopes", _boom)

    async def _fake_generate_handoff(*args, **kw):
        return (None, "still returns", None)

    monkeypatch.setattr("meridian.handoff.generate_handoff", _fake_generate_handoff)

    async def _fake_commits(project_dict, tenant):
        return []

    def _fake_identity(tenant):
        return None

    result = await st_mod.handle_checkpoint(
        {"session_id": sid, "project_id": pid}, db, str(tmp_path), None, None,
        fetch_recent_commits=_fake_commits, resolve_caller_identity=_fake_identity,
    )
    assert result["summary"] == "still returns"
    assert result["suggested_scopes"] == []


def test_checkpoint_schema_declares_scope_task_ids():
    from meridian.mcp_tools import _MCP_TOOLS_LIST

    tool = next(t for t in _MCP_TOOLS_LIST if t["name"] == "checkpoint")
    prop = tool["inputSchema"]["properties"]["scope_task_ids"]
    assert prop["type"] == "array"
    assert prop["items"] == {"type": "string"}
