"""Tests for update_sprint_item's new ``depends_on`` field (56f607ec).

Previously ``depends_on`` could only be set at creation time via
``add_sprint_item`` — there was no way to fix or add dependency ordering on
an already-filed item, forcing real ordering into prose in the ``notes``
field (invisible to ``get_parallelizable_groups``, which only reads the
structured column). These tests exercise:

* patch_sprint_item can set depends_on on an existing item,
* patch_sprint_item can clear depends_on with an empty string,
* omitting depends_on from a patch leaves the stored value unchanged,
* a self-dependency (item_id == depends_on) is rejected with ValueError,
* the MCP update_sprint_item dispatch surface forwards the field both ways,
* get_parallelizable_groups actually honors a depends_on set retroactively
  via update_sprint_item (the real-world case this closes).
"""

from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import server as server_module

# _dispatch_mcp_tool is re-exported from server (importing it directly from
# meridian.mcp.handler at module top-level triggers a circular import).
_dispatch_mcp_tool = server_module._dispatch_mcp_tool


@pytest.mark.asyncio
async def test_patch_sets_depends_on(db):
    """patch_sprint_item can set depends_on on an already-filed item."""
    p = await db_module.create_project(db, "depon-set")
    pid = p["id"]
    parent = await db_module.add_sprint_item(db, pid, "v1", "parent task")
    child = await db_module.add_sprint_item(db, pid, "v1", "child task")
    assert child.get("depends_on") is None

    updated = await db_module.patch_sprint_item(
        db, pid, child["id"], depends_on=parent["id"],
    )
    assert updated["depends_on"] == parent["id"]
    stored = await db_module.get_sprint_item(db, child["id"])
    assert stored["depends_on"] == parent["id"]


@pytest.mark.asyncio
async def test_patch_clears_depends_on(db):
    """An empty string clears a previously-set depends_on."""
    p = await db_module.create_project(db, "depon-clear")
    pid = p["id"]
    parent = await db_module.add_sprint_item(db, pid, "v1", "parent task")
    child = await db_module.add_sprint_item(
        db, pid, "v1", "child task", depends_on=parent["id"],
    )
    assert child["depends_on"] == parent["id"]

    updated = await db_module.patch_sprint_item(db, pid, child["id"], depends_on="")
    assert updated["depends_on"] is None
    stored = await db_module.get_sprint_item(db, child["id"])
    assert stored["depends_on"] is None


@pytest.mark.asyncio
async def test_patch_omitting_depends_on_leaves_it_unchanged(db):
    """Omitting depends_on from a patch must not clobber the stored value."""
    p = await db_module.create_project(db, "depon-omit")
    pid = p["id"]
    parent = await db_module.add_sprint_item(db, pid, "v1", "parent task")
    child = await db_module.add_sprint_item(
        db, pid, "v1", "child task", depends_on=parent["id"],
    )
    await db_module.patch_sprint_item(db, pid, child["id"], notes="touched")
    stored = await db_module.get_sprint_item(db, child["id"])
    assert stored["depends_on"] == parent["id"]
    assert stored["notes"] == "touched"


@pytest.mark.asyncio
async def test_patch_self_dependency_rejected(db):
    """An item cannot be made to depend on itself (would deadlock it)."""
    p = await db_module.create_project(db, "depon-self")
    pid = p["id"]
    item = await db_module.add_sprint_item(db, pid, "v1", "solo task")
    with pytest.raises(ValueError):
        await db_module.patch_sprint_item(db, pid, item["id"], depends_on=item["id"])
    # Item is left untouched (still no dependency).
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["depends_on"] is None


@pytest.mark.asyncio
async def test_mcp_update_sets_and_clears_depends_on(db, tmp_path):
    """update_sprint_item via MCP dispatch can set then clear depends_on."""
    p = await db_module.create_project(db, "depon-mcp")
    pid = p["id"]
    parent = await db_module.add_sprint_item(db, pid, "v1", "mcp parent")
    child = await db_module.add_sprint_item(db, pid, "v1", "mcp child")

    updated = await _dispatch_mcp_tool(
        "update_sprint_item",
        {"project_id": pid, "item_id": child["id"], "depends_on": parent["id"]},
        db,
        str(tmp_path),
    )
    assert updated.get("depends_on") == parent["id"]

    cleared = await _dispatch_mcp_tool(
        "update_sprint_item",
        {"project_id": pid, "item_id": child["id"], "depends_on": ""},
        db,
        str(tmp_path),
    )
    assert cleared.get("depends_on") is None


@pytest.mark.asyncio
async def test_retroactive_depends_on_honored_by_parallel_groups(db):
    """The real-world case this closes: an item filed WITHOUT depends_on later
    gets real ordering fixed via update_sprint_item, and
    get_parallelizable_groups (structured-field-only) actually respects it —
    unlike a prose note in ``notes``, which it cannot see."""
    p = await db_module.create_project(db, "depon-groups")
    pid = p["id"]
    parent = await db_module.add_sprint_item(db, pid, "v1", "index tantivy corpus")
    child = await db_module.add_sprint_item(db, pid, "v1", "expose tantivy search endpoint")
    # Filed with no structured ordering — before the fix, this was a dead end.
    assert child.get("depends_on") is None

    await db_module.patch_sprint_item(db, pid, child["id"], depends_on=parent["id"])

    groups = await db_module.get_parallelizable_groups(db, pid, version="v1")
    blocked_ids = {b["id"] for b in groups.get("blocked", [])}
    assert child["id"] in blocked_ids
    eligible_ids = {
        it["id"] for grp in groups.get("groups", []) for it in grp
    }
    assert child["id"] not in eligible_ids
    assert parent["id"] in eligible_ids
