"""Regression test for d71cfaaf.

Reported as a SEVERE bug: "update_sprint_item silently marked an item status=done
as a side effect of an unrelated notes-only call". update_sprint_item's schema has
no status field and its handler never forwards a status to patch_sprint_item, so a
notes-only update must leave status untouched. This proves it and guards against a
regression (and documents that the observed 'done' almost certainly came from a
complete_sprint_item(notes=...) call, which is the tool that DOES set done).
"""
import pytest

from meridian import db as db_module
from meridian import server as srv


async def _project_and_item(db):
    proj = await srv._dispatch_mcp_tool("create_project", {"name": "no-side-fx"}, db, "/tmp")
    pid = proj["id"]
    item = await srv._dispatch_mcp_tool(
        "add_sprint_item",
        {"project_id": pid, "version": "v1", "title": "do the thing"},
        db, "/tmp",
    )
    return pid, item["id"]


@pytest.mark.asyncio
async def test_notes_only_update_does_not_change_status(db):
    pid, item_id = await _project_and_item(db)
    before = await db_module.get_sprint_item(db, item_id)
    assert before["status"] == "pending"

    updated = await srv._dispatch_mcp_tool(
        "update_sprint_item",
        {"project_id": pid, "item_id": item_id, "notes": "just an FYI note"},
        db, "/tmp",
    )
    assert updated["notes"] == "just an FYI note"
    assert updated["status"] == "pending", updated  # NOT flipped to done

    after = await db_module.get_sprint_item(db, item_id)
    assert after["status"] == "pending", after
    assert after["completed_at"] is None, after


@pytest.mark.asyncio
async def test_repeated_field_updates_never_complete_item(db):
    """title/group/priority/touches edits also never terminate the item."""
    pid, item_id = await _project_and_item(db)
    for patch in (
        {"title": "renamed"},
        {"group": "backend"},
        {"priority": "high"},
        {"touches_resources": ["file:meridian/server.py"]},
        {"notes": "context"},
    ):
        await srv._dispatch_mcp_tool(
            "update_sprint_item", {"project_id": pid, "item_id": item_id, **patch}, db, "/tmp",
        )
        cur = await db_module.get_sprint_item(db, item_id)
        assert cur["status"] == "pending", (patch, cur)
