"""Regression test for 6a17e735 — patch_sprint_item's status parameter was a
real backdoor around every dedicated state-transition guard.

CONFIRMED BUG: patch_sprint_item accepted ANY value in _VALID_SPRINT_STATUSES
(including terminal ones — done/skipped/failed/pushed) and wrote it directly
via raw SQL, with only an enum-membership check. Unlike _update_sprint_item_status
(the internal function complete_sprint_item/skip_sprint_item/fail_sprint_item/
provisional_complete_sprint_item all funnel through), it did NOT: stamp/clear
completed_at, enforce the required_notes evidence gate, roll up a parent item,
advance a mixed-ownership task chain, stamp claimed_at, invalidate the
sprint-items cache, or publish the live dashboard event. Any caller passing
status= directly silently bypassed all of that.

The MCP tool surface (update_sprint_item, both the main and stdio handlers)
never forwarded status, so it wasn't exploitable through the officially
documented tool. But meridian/routes/sprint.py's dashboard PATCH endpoint DID
forward status (already self-restricted to pending/indeterminate at the route
level) — and the underlying db-layer function itself had no restriction at
all, a latent landmine for any other/future caller.

FIX: patch_sprint_item now only allows the non-terminal, no-business-logic
subset {pending, todo, indeterminate}; every other status raises ValueError
naming the correct dedicated function. The allowed subset also now clears
completed_at, invalidates the cache, and publishes the live event — matching
_update_sprint_item_status's behavior for a non-terminal transition.
"""
import pytest

from meridian import db as db_module


async def _project_with_item(db, *, required_notes=False):
    p = await db_module.create_project(db, "patch-status-guard")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "guarded item")
    if required_notes:
        await db_module.patch_sprint_item(db, p["id"], item["id"], required_notes=True)
    return p, item


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_status,dedicated_fn", [
    ("done", "complete_sprint_item"),
    ("skipped", "skip_sprint_item"),
    ("failed", "fail_sprint_item"),
    ("pushed", "push_sprint_item"),
    ("in_progress", "claim_sprint_item"),
    ("provisional_complete", "provisional_complete_sprint_item"),
])
async def test_patch_sprint_item_rejects_guarded_statuses(db, blocked_status, dedicated_fn):
    p, item = await _project_with_item(db)
    with pytest.raises(ValueError, match=blocked_status):
        await db_module.patch_sprint_item(db, p["id"], item["id"], status=blocked_status)
    # The item must be completely unaffected by the rejected attempt.
    unchanged = await db_module.get_sprint_item(db, item["id"])
    assert unchanged["status"] == "pending"


@pytest.mark.asyncio
async def test_patch_sprint_item_cannot_bypass_required_notes_evidence_gate(db):
    """The exact real-world exploit this item describes: a required_notes item
    marked done with zero evidence via the generic patcher instead of
    complete_sprint_item's guarded evidence check."""
    p, item = await _project_with_item(db, required_notes=True)
    with pytest.raises(ValueError):
        await db_module.patch_sprint_item(db, p["id"], item["id"], status="done")
    unchanged = await db_module.get_sprint_item(db, item["id"])
    assert unchanged["status"] == "pending"
    assert unchanged["completed_at"] is None
    # The REAL gate (SprintItemEvidenceRequired via complete_sprint_item) still
    # correctly blocks this same item with no evidence — proving the fix didn't
    # accidentally disable the real guard, just closed the bypass around it.
    with pytest.raises(db_module.SprintItemEvidenceRequired):
        await db_module.complete_sprint_item(db, p["id"], item["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed_status", ["pending", "todo", "indeterminate"])
async def test_patch_sprint_item_allows_administrative_reset_statuses(db, allowed_status):
    p, item = await _project_with_item(db)
    # First get it into a real non-pending state to prove this is a genuine
    # transition, not a no-op.
    await db_module.patch_sprint_item(db, p["id"], item["id"], status="todo")
    result = await db_module.patch_sprint_item(db, p["id"], item["id"], status=allowed_status)
    assert result["status"] == allowed_status


@pytest.mark.asyncio
async def test_patch_sprint_item_reset_clears_stale_completed_at(db):
    """Core data-integrity fix: resetting a done item back to pending via
    patch_sprint_item must clear completed_at, not leave it stale — the exact
    gap that could produce a "status=pending but completed_at is set" state."""
    p, item = await _project_with_item(db)
    completed = await db_module.complete_sprint_item(db, p["id"], item["id"])
    assert completed["completed_at"] is not None

    reset = await db_module.patch_sprint_item(db, p["id"], item["id"], status="pending")
    assert reset["status"] == "pending"
    assert reset["completed_at"] is None


@pytest.mark.asyncio
async def test_patch_sprint_item_status_change_invalidates_cache(db, monkeypatch):
    p, item = await _project_with_item(db)
    calls = []
    monkeypatch.setattr(
        db_module, "_invalidate_sprint_items_cache",
        lambda project_id: calls.append(project_id),
    )
    await db_module.patch_sprint_item(db, p["id"], item["id"], status="todo")
    assert calls == [p["id"]]


@pytest.mark.asyncio
async def test_patch_sprint_item_status_change_publishes_live_event(db, monkeypatch):
    p, item = await _project_with_item(db)
    events = []
    monkeypatch.setattr(
        db_module, "_publish_project_event",
        lambda project_id, event_type, payload: events.append((project_id, event_type, payload)),
    )
    await db_module.patch_sprint_item(db, p["id"], item["id"], status="indeterminate")
    assert len(events) == 1
    proj, etype, payload = events[0]
    assert proj == p["id"]
    assert etype == "sprint_item_updated"
    assert payload["item_id"] == item["id"]
    assert payload["status"] == "indeterminate"


@pytest.mark.asyncio
async def test_patch_sprint_item_other_fields_unaffected_when_status_omitted(db, monkeypatch):
    """Regression: patching non-status fields (the overwhelming majority of
    real update_sprint_item calls) must not trigger cache/event side effects
    that didn't exist before, and must continue to work exactly as before."""
    p, item = await _project_with_item(db)
    calls = []
    monkeypatch.setattr(
        db_module, "_invalidate_sprint_items_cache",
        lambda project_id: calls.append(project_id),
    )
    result = await db_module.patch_sprint_item(
        db, p["id"], item["id"], notes="just a note", human_id="alice",
    )
    assert result["notes"] == "just a note"
    assert result["human_id"] == "alice"
    assert calls == []  # no status change -> no cache invalidation


@pytest.mark.asyncio
async def test_dedicated_functions_still_work_unaffected(db):
    """Sanity: the real, guarded transition functions are completely unaffected
    by this fix — they don't go through patch_sprint_item at all."""
    p, item = await _project_with_item(db)
    result = await db_module.complete_sprint_item(db, p["id"], item["id"])
    assert result["status"] == "done"
    assert result["completed_at"] is not None


@pytest.mark.asyncio
async def test_mcp_update_sprint_item_tool_unaffected(db):
    """End-to-end: the real MCP tool surface never forwarded status anyway
    (confirmed by direct code read of both the main and stdio handlers), so
    it must continue to work exactly as before — this fix changes nothing
    about the tool callers actually use."""
    import meridian.server as srv

    p, item = await _project_with_item(db)
    result = await srv._dispatch_mcp_tool(
        "update_sprint_item",
        {"project_id": p["id"], "item_id": item["id"], "notes": "via MCP tool"},
        db, "/tmp",
    )
    assert result["notes"] == "via MCP tool"
    assert result["status"] == "pending"  # unchanged — status was never forwarded


@pytest.mark.asyncio
async def test_sprint_route_allowed_values_compatible_with_new_restriction(db):
    """meridian/routes/sprint.py's dashboard PATCH endpoint already
    self-restricted status to {pending, indeterminate} at the route level
    (its own 422 check). Confirm patch_sprint_item's new, stricter restriction
    is a superset that still accepts everything the route allows through —
    the two layers must agree, not have one silently override the other."""
    route_allowed_statuses = {"pending", "indeterminate"}
    assert route_allowed_statuses <= db_module._PATCH_SPRINT_ITEM_ALLOWED_STATUSES

    p, item = await _project_with_item(db)
    await db_module.complete_sprint_item(db, p["id"], item["id"])
    for status in route_allowed_statuses:
        reset = await db_module.patch_sprint_item(db, p["id"], item["id"], status=status)
        assert reset["status"] == status
        assert reset["completed_at"] is None
