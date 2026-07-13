"""Tests for enforced wave grouping on sprint items (58a45b92).

A stored, deterministic `wave` label replaces recompute-every-time parallel
grouping: assign_sprint_waves auto-fills it from the conflict-free groups, and
update_sprint_item(wave=...) hand-edits it. Runs on both backends via the `db`
fixture.
"""
import pytest

from meridian import db as db_module
from meridian import server as srv
from meridian.mcp_tools import (
    _MCP_TOOLS_LIST, _READ_ONLY_TOOLS, _TITLE_OVERRIDES, _TOOL_EXAMPLES,
)


async def _project(db):
    proj = await srv._dispatch_mcp_tool("create_project", {"name": "waves"}, db, "/tmp")
    return proj["id"]


@pytest.mark.asyncio
async def test_wave_column_exists_and_defaults_null(db):
    pid = await _project(db)
    item = await db_module.add_sprint_item(db, pid, "v1", "unassigned item")
    assert "wave" in item
    assert item["wave"] is None


@pytest.mark.asyncio
async def test_add_and_update_wave_roundtrip(db):
    pid = await _project(db)
    created = await srv._dispatch_mcp_tool(
        "add_sprint_item",
        {"project_id": pid, "version": "v1", "title": "pin me", "wave": "wave-3"},
        db, "/tmp",
    )
    assert created["wave"] == "wave-3"

    # Hand-edit via update_sprint_item.
    updated = await srv._dispatch_mcp_tool(
        "update_sprint_item",
        {"project_id": pid, "item_id": created["id"], "wave": "wave-9"},
        db, "/tmp",
    )
    assert updated["wave"] == "wave-9"

    # Empty string clears it (unassigned).
    cleared = await srv._dispatch_mcp_tool(
        "update_sprint_item",
        {"project_id": pid, "item_id": created["id"], "wave": ""},
        db, "/tmp",
    )
    assert cleared["wave"] is None

    # Omitting wave leaves it untouched.
    await srv._dispatch_mcp_tool(
        "update_sprint_item",
        {"project_id": pid, "item_id": created["id"], "wave": "wave-2"},
        db, "/tmp",
    )
    touched = await srv._dispatch_mcp_tool(
        "update_sprint_item",
        {"project_id": pid, "item_id": created["id"], "notes": "no wave here"},
        db, "/tmp",
    )
    assert touched["wave"] == "wave-2"


@pytest.mark.asyncio
async def test_assign_sprint_waves_maps_conflict_free_groups(db):
    pid = await _project(db)
    # A and B share file:a.py (conflict -> different waves). C is disjoint (co-batches
    # with A in the first wave).
    a = await db_module.add_sprint_item(db, pid, "v1", "edit a one", touches_resources=["file:a.py"])
    b = await db_module.add_sprint_item(db, pid, "v1", "edit a two", touches_resources=["file:a.py"], force=True)
    c = await db_module.add_sprint_item(db, pid, "v1", "edit c", touches_resources=["file:c.py"])

    result = await srv._dispatch_mcp_tool(
        "assign_sprint_waves", {"project_id": pid}, db, "/tmp",
    )
    assert result["assigned"] == 3
    assert result["wave_count"] == 2

    ra = await db_module.get_sprint_item(db, a["id"])
    rb = await db_module.get_sprint_item(db, b["id"])
    rc = await db_module.get_sprint_item(db, c["id"])
    # All labelled.
    assert ra["wave"] and rb["wave"] and rc["wave"]
    # The two conflicting items (share file:a.py) land in DIFFERENT waves; which of
    # A/B is wave-1 vs wave-2 depends on the deterministic-but-uuid-tied sort order.
    assert ra["wave"] != rb["wave"]
    assert {ra["wave"], rb["wave"]} == {"wave-1", "wave-2"}
    # C is disjoint from both, so first-fit always co-batches it into wave-1.
    assert rc["wave"] == "wave-1"
    # The returned mapping partitions all three ids across exactly two waves.
    assert c["id"] in result["waves"]["wave-1"]
    assert len(result["waves"]["wave-2"]) == 1
    assert set(result["waves"]["wave-1"]) | set(result["waves"]["wave-2"]) == {
        a["id"], b["id"], c["id"]
    }


@pytest.mark.asyncio
async def test_assign_sprint_waves_idempotent(db):
    pid = await _project(db)
    await db_module.add_sprint_item(db, pid, "v1", "solo", touches_resources=["file:x.py"])
    first = await db_module.assign_sprint_waves(db, pid)
    second = await db_module.assign_sprint_waves(db, pid)
    assert first["waves"] == second["waves"]


def test_assign_sprint_waves_registered_as_write_tool():
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    assert "assign_sprint_waves" in by_name
    tool = by_name["assign_sprint_waves"]
    assert tool["inputSchema"]["required"] == []
    assert "project_name" in tool["inputSchema"]["properties"]
    # It mutates -> must NOT be advertised read-only.
    assert "assign_sprint_waves" not in _READ_ONLY_TOOLS
    assert _TITLE_OVERRIDES["assign_sprint_waves"] == "Assign Sprint Waves"
    assert "assign_sprint_waves" in _TOOL_EXAMPLES
