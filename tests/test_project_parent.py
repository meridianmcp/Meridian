"""Coverage for set_parent_project / rename_project (7acb8563).

Makes the projects.parent_project_id relationship editable AFTER creation
(create_project only accepted it at creation time), plus an MCP wrapper over the
existing db.rename_project. Exercises the one-level-deep invariant (3b6ff466) at
the DB layer and both tools through the real _dispatch_mcp_tool path.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module


# ---------------------------------------------------------------------------
# DB layer — set_parent_project invariants
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_parent_project_attach_and_detach(db):
    parent = await db_module.create_project(db, "pp-parent")
    child = await db_module.create_project(db, "pp-child")
    assert child.get("parent_project_id") in (None, "")

    attached = await db_module.set_parent_project(db, child["id"], parent["id"])
    assert attached["parent_project_id"] == parent["id"]

    detached = await db_module.set_parent_project(db, child["id"], None)
    assert detached.get("parent_project_id") in (None, "")


@pytest.mark.asyncio
async def test_set_parent_project_missing_project_returns_none(db):
    assert await db_module.set_parent_project(db, "does-not-exist", None) is None


@pytest.mark.asyncio
async def test_set_parent_project_rejects_self_parent(db):
    p = await db_module.create_project(db, "pp-self")
    with pytest.raises(ValueError):
        await db_module.set_parent_project(db, p["id"], p["id"])


@pytest.mark.asyncio
async def test_set_parent_project_rejects_missing_parent(db):
    p = await db_module.create_project(db, "pp-orphan")
    with pytest.raises(ValueError):
        await db_module.set_parent_project(db, p["id"], "no-such-parent")


@pytest.mark.asyncio
async def test_set_parent_project_rejects_two_levels(db):
    """A parent that is itself a subproject can't be a parent (one level deep)."""
    top = await db_module.create_project(db, "pp-top")
    mid = await db_module.create_project(db, "pp-mid", parent_project_id=top["id"])
    leaf = await db_module.create_project(db, "pp-leaf")
    with pytest.raises(ValueError):
        await db_module.set_parent_project(db, leaf["id"], mid["id"])


@pytest.mark.asyncio
async def test_set_parent_project_rejects_reparenting_a_parent(db):
    """A project that already HAS subprojects can't become a subproject itself."""
    a = await db_module.create_project(db, "pp-a")
    _b = await db_module.create_project(db, "pp-b", parent_project_id=a["id"])
    c = await db_module.create_project(db, "pp-c")
    with pytest.raises(ValueError):
        await db_module.set_parent_project(db, a["id"], c["id"])


# ---------------------------------------------------------------------------
# MCP dispatch — set_parent_project / rename_project
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_set_parent_project_by_name(db):
    from meridian import server as srv
    parent = await db_module.create_project(db, "pp-mcp-parent")
    child = await db_module.create_project(db, "pp-mcp-child")

    result = await srv._dispatch_mcp_tool(
        "set_parent_project",
        {"project_name": "pp-mcp-child", "parent_project_name": "pp-mcp-parent"},
        db, "/tmp",
    )
    assert result["parent_project_id"] == parent["id"]

    # omitting the parent detaches (top-level again)
    detached = await srv._dispatch_mcp_tool(
        "set_parent_project", {"project_id": child["id"]}, db, "/tmp",
    )
    assert detached.get("parent_project_id") in (None, "")


@pytest.mark.asyncio
async def test_mcp_set_parent_project_invalid_returns_error(db):
    from meridian import server as srv
    p = await db_module.create_project(db, "pp-mcp-bad")
    result = await srv._dispatch_mcp_tool(
        "set_parent_project",
        {"project_id": p["id"], "parent_project_id": p["id"]},  # self-parent
        db, "/tmp",
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_mcp_rename_project(db):
    from meridian import server as srv
    p = await db_module.create_project(db, "pp-rename-old")
    result = await srv._dispatch_mcp_tool(
        "rename_project",
        {"project_id": p["id"], "new_name": "pp-rename-new"},
        db, "/tmp",
    )
    assert result["name"] == "pp-rename-new"
    assert (await db_module.get_project(db, p["id"]))["name"] == "pp-rename-new"


def test_parent_project_tools_registered():
    from meridian.mcp_tools import _MCP_TOOLS_LIST, _READ_ONLY_TOOLS
    names = {t["name"] for t in _MCP_TOOLS_LIST}
    assert {"set_parent_project", "rename_project"} <= names
    # both are writes, not read-only
    assert "set_parent_project" not in _READ_ONLY_TOOLS
    assert "rename_project" not in _READ_ONLY_TOOLS
