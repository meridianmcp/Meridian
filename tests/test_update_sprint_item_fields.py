"""Regression tests for update_sprint_item field persistence.

Covers two bugs reported on 2026-07-12:
  * 07a4bad1 — update_sprint_item(priority="high") did not stick (response and a
    follow-up read both showed priority="normal").
  * 7df67168 — update_sprint_item(touches_resources=[...]) was silently discarded,
    leaving a wrong auto-inferred value in place.

Both go through the real MCP dispatch (``_dispatch_mcp_tool``) so the test exercises
the same path a connected client hits.
"""
import pytest

from meridian import db as db_module
from meridian import server as srv


async def _make_project_and_item(db, *, touches=None):
    proj = await srv._dispatch_mcp_tool(
        "create_project", {"name": "upd-fields"}, db, "/tmp"
    )
    pid = proj["id"]
    add_args = {"project_id": pid, "version": "v1", "title": "wire up the widget"}
    if touches is not None:
        add_args["touches_resources"] = touches
    item = await srv._dispatch_mcp_tool("add_sprint_item", add_args, db, "/tmp")
    return pid, item["id"]


@pytest.mark.asyncio
async def test_update_priority_persists(db):
    """priority set via update_sprint_item is returned AND survives a re-read."""
    pid, item_id = await _make_project_and_item(db)

    updated = await srv._dispatch_mcp_tool(
        "update_sprint_item",
        {"project_id": pid, "item_id": item_id, "priority": "high"},
        db, "/tmp",
    )
    assert updated.get("priority") == "high", updated

    reread = await db_module.get_sprint_item(db, item_id)
    assert reread["priority"] == "high", reread


@pytest.mark.asyncio
async def test_update_touches_resources_overrides_inferred(db):
    """Explicit touches_resources on update replaces a prior inferred value."""
    # Create with no explicit resources so auto-inference fills something in.
    pid, item_id = await _make_project_and_item(db)
    explicit = ["file:meridian/agent_defaults.py:symbol:DEFAULT_AGENT_INSTRUCTIONS"]

    updated = await srv._dispatch_mcp_tool(
        "update_sprint_item",
        {"project_id": pid, "item_id": item_id, "touches_resources": explicit},
        db, "/tmp",
    )
    stored = db_module.parse_touches_resources(updated.get("touches_resources"))
    assert stored == db_module.parse_touches_resources(explicit), updated

    reread = await db_module.get_sprint_item(db, item_id)
    assert db_module.parse_touches_resources(reread["touches_resources"]) == \
        db_module.parse_touches_resources(explicit), reread


@pytest.mark.asyncio
async def test_add_keeps_explicit_touches_resources_no_inference(db):
    """Explicit touches_resources at create time are NOT overwritten by the
    title-based auto-inference (07bdfdbb only infers when none supplied)."""
    explicit = ["file:meridian/agent_defaults.py:symbol:DEFAULT_AGENT_INSTRUCTIONS"]
    pid, item_id = await _make_project_and_item(db, touches=explicit)

    reread = await db_module.get_sprint_item(db, item_id)
    stored = db_module.parse_touches_resources(reread["touches_resources"])
    assert stored == db_module.parse_touches_resources(explicit), reread
    # And crucially: no "inferred:" provenance marker leaked in.
    assert not any("inferred:" in s for s in (reread["touches_resources"] or "")), reread
