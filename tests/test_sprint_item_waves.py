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


# ---------------------------------------------------------------------------
# 90955d26 — assign_sprint_waves projects depends_on-blocked items into future
# waves rather than dropping them with wave=NULL. Covers:
#   * a simple A→B chain (B lands in wave-2, not NULL)
#   * a three-level chain A→B→C
#   * resource conflicts inside the first (unblocked) wave still split into
#     wave-1 / wave-2, and the blocked dep then lands in wave-3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_sprint_waves_projects_dep_into_future_wave(db):
    """A depends_on-blocked item receives a future-wave label, not NULL."""
    pid = await _project(db)
    # A is a root (no dep), B depends on A.
    a = await db_module.add_sprint_item(db, pid, "v1", "root item A")
    b = await db_module.add_sprint_item(
        db, pid, "v1", "dep item B", depends_on=a["id"], force=True
    )
    result = await db_module.assign_sprint_waves(db, pid)

    ra = await db_module.get_sprint_item(db, a["id"])
    rb = await db_module.get_sprint_item(db, b["id"])

    # Both must be assigned a wave (neither NULL).
    assert ra["wave"] is not None, "root item A should have a wave label"
    assert rb["wave"] is not None, "dep item B should have a wave label, not NULL"
    # B's wave must be numerically later than A's wave.
    a_num = int(ra["wave"].split("-")[1])
    b_num = int(rb["wave"].split("-")[1])
    assert b_num > a_num, (
        f"Dep item B (wave-{b_num}) must be in a later wave than root A (wave-{a_num})"
    )
    # assigned count includes the blocked item.
    assert result["assigned"] == 2
    # wave_count reflects at least 2 distinct waves.
    assert result["wave_count"] >= 2


@pytest.mark.asyncio
async def test_assign_sprint_waves_three_level_chain(db):
    """A→B→C three-level chain: each level lands in a strictly later wave."""
    pid = await _project(db)
    a = await db_module.add_sprint_item(db, pid, "v1", "level 0 root")
    b = await db_module.add_sprint_item(
        db, pid, "v1", "level 1 dep", depends_on=a["id"], force=True
    )
    c = await db_module.add_sprint_item(
        db, pid, "v1", "level 2 dep", depends_on=b["id"], force=True
    )
    await db_module.assign_sprint_waves(db, pid)

    ra = await db_module.get_sprint_item(db, a["id"])
    rb = await db_module.get_sprint_item(db, b["id"])
    rc = await db_module.get_sprint_item(db, c["id"])

    assert ra["wave"] and rb["wave"] and rc["wave"], "all three must be labelled"
    wa = int(ra["wave"].split("-")[1])
    wb = int(rb["wave"].split("-")[1])
    wc = int(rc["wave"].split("-")[1])
    assert wa < wb < wc, (
        f"Expected wave order A<B<C but got wave-{wa}/wave-{wb}/wave-{wc}"
    )


@pytest.mark.asyncio
async def test_assign_sprint_waves_conflict_plus_dep(db):
    """Resource conflict in layer 0 splits into wave-1/wave-2; blocked dep gets wave-3."""
    pid = await _project(db)
    # Two items share file:x.py — they'll conflict -> separate sub-waves within layer 0.
    a = await db_module.add_sprint_item(
        db, pid, "v1", "edit x first", touches_resources=["file:x.py"]
    )
    b = await db_module.add_sprint_item(
        db, pid, "v1", "edit x second", touches_resources=["file:x.py"], force=True
    )
    # C depends on A and will be projected into a future layer.
    c = await db_module.add_sprint_item(
        db, pid, "v1", "depends on a", depends_on=a["id"], force=True
    )
    result = await db_module.assign_sprint_waves(db, pid)

    ra = await db_module.get_sprint_item(db, a["id"])
    rb = await db_module.get_sprint_item(db, b["id"])
    rc = await db_module.get_sprint_item(db, c["id"])

    # A and B must be in different waves (resource conflict).
    assert ra["wave"] != rb["wave"], "conflicting items must be in different waves"
    # C's wave must be later than A's wave (dependency).
    wa = int(ra["wave"].split("-")[1])
    wc = int(rc["wave"].split("-")[1])
    assert wc > wa, (
        f"Dep item C (wave-{wc}) must be later than A (wave-{wa})"
    )
    # All three are assigned.
    assert result["assigned"] == 3
    # At least 2 waves (layer-0 conflict + layer-1 dep).
    assert result["wave_count"] >= 2


@pytest.mark.asyncio
async def test_topo_depth_map_basic():
    """_topo_depth_map correctly computes depth for a simple chain."""
    items = [
        {"id": "a", "depends_on": None},
        {"id": "b", "depends_on": "a"},
        {"id": "c", "depends_on": "b"},
    ]
    dm = db_module._topo_depth_map(items)
    assert dm["a"] == 0
    assert dm["b"] == 1
    assert dm["c"] == 2


@pytest.mark.asyncio
async def test_topo_depth_map_cycle_safe():
    """_topo_depth_map handles a dependency cycle without infinite recursion."""
    items = [
        {"id": "a", "depends_on": "b"},
        {"id": "b", "depends_on": "a"},
    ]
    dm = db_module._topo_depth_map(items)
    # Both items should have a depth (0 is fine — cycle treated as root).
    assert "a" in dm and "b" in dm


@pytest.mark.asyncio
async def test_topo_depth_map_external_dep_is_root():
    """_topo_depth_map treats an external (out-of-set) dep as depth 0 (root)."""
    items = [
        {"id": "a", "depends_on": "external-id-not-in-set"},
        {"id": "b", "depends_on": "a"},
    ]
    dm = db_module._topo_depth_map(items)
    assert dm["a"] == 0  # external dep → root
    assert dm["b"] == 1  # in-set dep on a → wave 1
