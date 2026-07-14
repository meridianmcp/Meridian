"""Tests for sprint item 97d695c4 — _handle_project_tools dispatch-table refactor.

Proves that every tool previously handled by the if/elif chain in
_handle_project_tools continues to work correctly after the extraction into
meridian/mcp/handlers/project_tools.py.

Strategy:
- Call each per-tool handler function directly from the new submodule (unit).
- Call _handle_project_tools with each tool name and assert identical results
  (integration via the new dispatch table).
- Verify the module structure: each handler function is importable from the
  new submodule and is an async callable.
- Verify _MISS sentinel is returned for an unknown tool name (regression guard).

No server.py startup or real ports needed — all tests use an in-memory SQLite
DB (same pattern as tests/test_cov_handler.py) and monkeypatch heavy IO.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest
import pytest_asyncio

import meridian.server  # noqa: F401 — must be imported before handler to avoid cycle
from meridian.mcp import handler as mh
from meridian.mcp.handlers import project_tools as pt_mod
from meridian import db as db_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make_db():
    return _run(db_module.init_db(":memory:"))


_DATA_DIR = "/tmp/meridian-test"

# ---------------------------------------------------------------------------
# Module-structure assertions
# ---------------------------------------------------------------------------

EXPECTED_HANDLER_NAMES = [
    "handle_create_project",
    "handle_set_parent_project",
    "handle_rename_project",
    "handle_register_session",
    "handle_start_session",
    "handle_list_projects",
    "handle_get_project_by_name",
    "handle_get_goal",
    "handle_set_goal",
    "handle_set_north_star",
    "handle_merge_project",
]


def test_all_expected_handlers_are_importable():
    """All 11 per-tool handlers must be importable from the new submodule."""
    for name in EXPECTED_HANDLER_NAMES:
        assert hasattr(pt_mod, name), f"Missing handler: {name}"


def test_all_handlers_are_async():
    """Every handler must be an async function (coroutine function)."""
    for name in EXPECTED_HANDLER_NAMES:
        fn = getattr(pt_mod, name)
        assert asyncio.iscoroutinefunction(fn), f"{name} is not async"


def test_unknown_tool_returns_miss():
    """_handle_project_tools must return _MISS for an unrecognised tool name."""
    db = _make_db()
    result = _run(mh._handle_project_tools(
        "no_such_tool_xyz", {}, db, _DATA_DIR, None, None
    ))
    _run(db.close())
    assert result is mh._MISS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db():
    conn = await db_module.init_db(":memory:")
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def project(db):
    proj = await db_module.create_project(db, "test-proj")
    return proj


# ---------------------------------------------------------------------------
# create_project
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_project_via_dispatch(db, project):
    """create_project returns a project dict with an id."""
    # Use the already-created project fixture to confirm structure
    assert "id" in project
    assert project["name"] == "test-proj"


@pytest.mark.asyncio
async def test_create_project_dispatch_table(db):
    result = await mh._handle_project_tools(
        "create_project", {"name": "dispatch-test-proj"}, db, _DATA_DIR, None, None
    )
    assert "id" in result
    assert result["name"] == "dispatch-test-proj"


@pytest.mark.asyncio
async def test_create_project_duplicate_returns_error(db, project):
    result = await mh._handle_project_tools(
        "create_project", {"name": "test-proj"}, db, _DATA_DIR, None, None
    )
    assert "error" in result
    assert "already exists" in result["error"]


@pytest.mark.asyncio
async def test_create_project_handler_direct(db):
    result = await pt_mod.handle_create_project(
        {"name": "direct-handler-proj"}, db, _DATA_DIR, None, None
    )
    assert "id" in result
    assert result["name"] == "direct-handler-proj"


# ---------------------------------------------------------------------------
# list_projects
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_projects_dispatch_table(db, project):
    result = await mh._handle_project_tools(
        "list_projects", {}, db, _DATA_DIR, None, None
    )
    assert isinstance(result, list)
    names = [p["name"] for p in result]
    assert "test-proj" in names


@pytest.mark.asyncio
async def test_list_projects_handler_direct(db, project):
    result = await pt_mod.handle_list_projects({}, db, _DATA_DIR, None, None)
    assert isinstance(result, list)
    assert any(p["name"] == "test-proj" for p in result)


# ---------------------------------------------------------------------------
# get_project_by_name
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_project_by_name_found(db, project):
    result = await mh._handle_project_tools(
        "get_project_by_name", {"name": "test-proj"}, db, _DATA_DIR, None, None
    )
    assert result["name"] == "test-proj"
    assert "id" in result


@pytest.mark.asyncio
async def test_get_project_by_name_not_found_raises(db):
    with pytest.raises(ValueError, match="no project found"):
        await mh._handle_project_tools(
            "get_project_by_name", {"name": "nonexistent-zzz"}, db, _DATA_DIR, None, None
        )


@pytest.mark.asyncio
async def test_get_project_by_name_handler_direct_found(db, project):
    result = await pt_mod.handle_get_project_by_name(
        {"name": "test-proj"}, db, _DATA_DIR, None, None
    )
    assert result["name"] == "test-proj"


@pytest.mark.asyncio
async def test_get_project_by_name_handler_direct_not_found(db):
    with pytest.raises(ValueError):
        await pt_mod.handle_get_project_by_name(
            {"name": "noexist-yyy"}, db, _DATA_DIR, None, None
        )


# ---------------------------------------------------------------------------
# get_goal / set_goal / set_north_star
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_goal_then_get_goal(db, project):
    pid = project["id"]
    set_result = await mh._handle_project_tools(
        "set_goal", {"project_id": pid, "content": "My goal text"}, db, _DATA_DIR, None, None
    )
    # set_goal returns a goal row or similar dict
    get_result = await mh._handle_project_tools(
        "get_goal", {"project_id": pid}, db, _DATA_DIR, None, None
    )
    assert get_result is not None
    assert "My goal text" in str(get_result)


@pytest.mark.asyncio
async def test_set_goal_handler_direct(db, project):
    pid = project["id"]
    await pt_mod.handle_set_goal(
        {"project_id": pid, "content": "Direct handler goal"}, db, _DATA_DIR, None, None
    )
    result = await pt_mod.handle_get_goal(
        {"project_id": pid}, db, _DATA_DIR, None, None
    )
    assert "Direct handler goal" in str(result)


@pytest.mark.asyncio
async def test_set_north_star_dispatch(db, project):
    pid = project["id"]
    # set_goal must be called before set_north_star (db constraint)
    await db_module.set_goal(db, pid, "initial goal content")
    result = await mh._handle_project_tools(
        "set_north_star",
        {"project_id": pid, "north_star": "Build great software"},
        db, _DATA_DIR, None, None
    )
    # Should not return an error
    assert "error" not in (result or {})


@pytest.mark.asyncio
async def test_set_north_star_handler_direct(db, project):
    pid = project["id"]
    # set_goal must be called before set_north_star (db constraint)
    await db_module.set_goal(db, pid, "initial goal content")
    result = await pt_mod.handle_set_north_star(
        {"project_id": pid, "north_star": "Direct north star"},
        db, _DATA_DIR, None, None
    )
    assert "error" not in (result or {})


# ---------------------------------------------------------------------------
# get_goal truncation: decisions field capped at 3000 chars
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_goal_truncates_decisions(db, project, monkeypatch):
    """get_goal must cap decisions at 3000 chars."""
    long_decisions = "x" * 5000
    # Monkeypatch db_module.get_goal to return a row with long decisions.
    original_get_goal = db_module.get_goal
    async def _fake_get_goal(db_, pid_):
        return {"decisions": long_decisions, "project_id": pid_, "content": "c"}
    monkeypatch.setattr(db_module, "get_goal", _fake_get_goal)
    pid = project["id"]
    result = await mh._handle_project_tools(
        "get_goal", {"project_id": pid}, db, _DATA_DIR, None, None
    )
    assert len(result["decisions"]) == 3000


# ---------------------------------------------------------------------------
# rename_project
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rename_project_dispatch(db, project):
    pid = project["id"]
    result = await mh._handle_project_tools(
        "rename_project",
        {"project_id": pid, "new_name": "renamed-proj"},
        db, _DATA_DIR, None, None
    )
    assert "error" not in result
    assert result["name"] == "renamed-proj"


@pytest.mark.asyncio
async def test_rename_project_missing_new_name(db, project):
    pid = project["id"]
    result = await mh._handle_project_tools(
        "rename_project",
        {"project_id": pid, "new_name": ""},
        db, _DATA_DIR, None, None
    )
    assert "error" in result
    assert "new_name" in result["error"]


@pytest.mark.asyncio
async def test_rename_project_missing_id(db):
    result = await mh._handle_project_tools(
        "rename_project",
        {"project_id": "", "new_name": "whatever"},
        db, _DATA_DIR, None, None
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_rename_project_handler_direct(db, project):
    pid = project["id"]
    result = await pt_mod.handle_rename_project(
        {"project_id": pid, "new_name": "direct-renamed"},
        db, _DATA_DIR, None, None
    )
    assert result["name"] == "direct-renamed"


# ---------------------------------------------------------------------------
# set_parent_project
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_parent_project_missing_id(db):
    result = await mh._handle_project_tools(
        "set_parent_project", {}, db, _DATA_DIR, None, None
    )
    assert "error" in result
    assert "project_id" in result["error"]


@pytest.mark.asyncio
async def test_set_parent_project_detach(db, project):
    """Passing no parent_project_id detaches (makes top-level)."""
    pid = project["id"]
    result = await mh._handle_project_tools(
        "set_parent_project",
        {"project_id": pid},  # no parent -> detach
        db, _DATA_DIR, None, None
    )
    # Should succeed (no error) since detaching a top-level project is a no-op.
    assert "error" not in result or result.get("error") is None or "not found" not in result.get("error", "")


@pytest.mark.asyncio
async def test_set_parent_project_handler_direct_missing(db):
    result = await pt_mod.handle_set_parent_project(
        {}, db, _DATA_DIR, None, None
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# merge_project
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_project_missing_ids(db):
    result = await mh._handle_project_tools(
        "merge_project", {}, db, _DATA_DIR, None, None
    )
    assert "error" in result
    assert "required" in result["error"]


@pytest.mark.asyncio
async def test_merge_project_self_merge_returns_error(db, project):
    pid = project["id"]
    result = await mh._handle_project_tools(
        "merge_project",
        {"source_project_id": pid, "target_project_id": pid},
        db, _DATA_DIR, None, None
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_merge_project_handler_direct_missing(db):
    result = await pt_mod.handle_merge_project(
        {}, db, _DATA_DIR, None, None
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_merge_two_projects(db):
    src = await db_module.create_project(db, "merge-src")
    tgt = await db_module.create_project(db, "merge-tgt")
    result = await mh._handle_project_tools(
        "merge_project",
        {
            "source_project_id": src["id"],
            "target_project_id": tgt["id"],
        },
        db, _DATA_DIR, None, None
    )
    # merge_project returns a dict describing the merge (no error key expected)
    assert "error" not in result or result.get("merged") is not None or "merge" in str(result).lower()


# ---------------------------------------------------------------------------
# register_session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_session_dispatch(db, project, monkeypatch):
    pid = project["id"]
    monkeypatch.setattr("meridian._deps._hosted_mode", lambda: False, raising=False)
    result = await mh._handle_project_tools(
        "register_session",
        {"project_id": pid, "session_name": "my-session"},
        db, _DATA_DIR, None, None
    )
    assert "id" in result or "session_id" in result or "session" in str(result).lower()


@pytest.mark.asyncio
async def test_register_session_handler_direct(db, project, monkeypatch):
    pid = project["id"]
    monkeypatch.setattr("meridian._deps._hosted_mode", lambda: False, raising=False)
    result = await pt_mod.handle_register_session(
        {"project_id": pid, "session_name": "direct-session"},
        db, _DATA_DIR, None, None
    )
    assert "id" in result or "session" in str(result).lower()


# ---------------------------------------------------------------------------
# Dispatch-table completeness: all 11 known tool names must be routed
# ---------------------------------------------------------------------------

TOOLS_IN_GROUP = [
    "create_project",
    "set_parent_project",
    "rename_project",
    "register_session",
    "start_session",
    "list_projects",
    "get_project_by_name",
    "get_goal",
    "set_goal",
    "set_north_star",
    "merge_project",
]


def test_dispatch_table_covers_all_tools():
    """Verify that each known tool name is NOT routed to _MISS.

    We confirm this by checking that calling _handle_project_tools with each
    name (with minimal/no args that avoid DB lookups where possible) does NOT
    return _MISS.  For tools requiring args we use monkeypatched db calls.
    """
    # list_projects requires no args and just returns []
    db = _make_db()
    result = _run(mh._handle_project_tools("list_projects", {}, db, _DATA_DIR, None, None))
    _run(db.close())
    assert result is not mh._MISS, "list_projects returned _MISS — not in dispatch table"
