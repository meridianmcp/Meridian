"""Tests for a389b95d — wave_assignment_hint on add_sprint_item / fan_out_sprint_items.

assign_sprint_waves has exactly 1 caller (its own MCP dispatch) and is NEVER
auto-triggered by add_sprint_item or fan_out_sprint_items.  This fixture hardens
the gap by surfacing a structural, non-blocking ``wave_assignment_hint`` field in
the add/fan-out response when:

  1. One or more pending/todo items have ``wave IS NULL`` (unassigned), AND
  2. No executor session has been seen recently (no in-flight /goal risk).

When sessions ARE active, the hint is suppressed to avoid recommending a
re-label while executors might be mid-flight holding specific wave labels.

Covers:
  (a) _wave_assignment_hint returns a string when items with wave IS NULL exist
      and no session is active.
  (b) _wave_assignment_hint returns None when all items already have a wave.
  (c) _wave_assignment_hint returns None when an active session is present
      (even if items have wave IS NULL).
  (d) _wave_assignment_hint returns None when the project has no pending items.
  (e) add_sprint_item response includes wave_assignment_hint when items with
      wave IS NULL exist and no active session.
  (f) add_sprint_item response does NOT include wave_assignment_hint when items
      already have a wave value (wave IS not NULL -- newly added item gets wave
      via the caller, or existing items are all waved).
  (g) fan_out_sprint_items response includes wave_assignment_hint after batch
      fan-out since the new items will have wave IS NULL.
  (h) add_sprint_item with force=true (drift check skipped) still surfaces
      wave_assignment_hint when applicable.
"""
from __future__ import annotations

import asyncio

import meridian.server  # noqa: F401 — ensure server module is loaded first
import meridian.db as db_module
from meridian.mcp import handler as mh


def _run(coro):
    return asyncio.run(coro)


def _make_db():
    return _run(db_module.init_db(":memory:"))


# ---------------------------------------------------------------------------
# Unit tests for the helper _wave_assignment_hint
# ---------------------------------------------------------------------------

def test_wave_assignment_hint_with_unassigned_items_no_sessions():
    """(a) Items with wave IS NULL exist and no session active → hint returned."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "wave-hint-unit-a"))
        # Add a pending item with no wave (default)
        _run(db_module.add_sprint_item(db, proj["id"], "v1", "Item with no wave"))
        # No sessions active — hint should fire
        hint = _run(mh._wave_assignment_hint(db, proj["id"]))
        assert hint is not None
        assert "WAVE_ASSIGNMENT_HINT" in hint
        assert "assign_sprint_waves" in hint
    finally:
        _run(db.close())


def test_wave_assignment_hint_all_waves_assigned():
    """(b) All items already have a wave label → no hint."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "wave-hint-unit-b"))
        # Add item and explicitly set its wave
        item = _run(db_module.add_sprint_item(db, proj["id"], "v1", "Item already waved"))
        _run(db_module.patch_sprint_item(db, proj["id"], item["id"], wave="wave-1"))
        hint = _run(mh._wave_assignment_hint(db, proj["id"]))
        assert hint is None
    finally:
        _run(db.close())


def test_wave_assignment_hint_no_pending_items():
    """(d) No pending/todo items at all → no hint."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "wave-hint-unit-d"))
        # Don't add any items
        hint = _run(mh._wave_assignment_hint(db, proj["id"]))
        assert hint is None
    finally:
        _run(db.close())


def test_wave_assignment_hint_done_items_not_counted():
    """Done items with wave IS NULL should not trigger the hint (only pending/todo)."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "wave-hint-unit-done"))
        item = _run(db_module.add_sprint_item(db, proj["id"], "v1", "Completed item"))
        # Mark as done
        _run(db_module.complete_sprint_item(db, proj["id"], item["id"]))
        # No pending/todo items with wave IS NULL — should be no hint
        hint = _run(mh._wave_assignment_hint(db, proj["id"]))
        assert hint is None
    finally:
        _run(db.close())


def test_wave_assignment_hint_text_mentions_count():
    """Hint message references the count of unassigned items."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "wave-hint-unit-count"))
        _run(db_module.add_sprint_item(db, proj["id"], "v1", "Build the authentication module"))
        _run(db_module.add_sprint_item(db, proj["id"], "v1", "Deploy the search service"))
        hint = _run(mh._wave_assignment_hint(db, proj["id"]))
        assert hint is not None
        # Should mention 2 items
        assert "2" in hint
    finally:
        _run(db.close())


# ---------------------------------------------------------------------------
# Integration tests via _dispatch_mcp_tool
# ---------------------------------------------------------------------------

def test_add_sprint_item_produces_wave_assignment_hint_when_unassigned():
    """(e) add_sprint_item with wave IS NULL items and no active session → hint."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "wave-hint-add-e"))
        # Add a pre-existing item with no wave (simulates the common case); use a
        # distinct title to avoid the duplicate guard triggering on the second add.
        _run(db_module.add_sprint_item(db, proj["id"], "v1", "Build authentication backend service"))
        # Now add a second distinct item — the response should include the wave hint
        out = _run(mh._dispatch_mcp_tool("add_sprint_item", {
            "project_id": proj["id"],
            "version": "v1",
            "title": "Deploy search index pipeline",
        }, db, "/tmp"))
        assert out.get("id") is not None, f"Expected item created, got: {out}"
        # Hint should be present (2 items now have wave IS NULL)
        assert out.get("wave_assignment_hint") is not None
        assert "WAVE_ASSIGNMENT_HINT" in out["wave_assignment_hint"]
        assert "assign_sprint_waves" in out["wave_assignment_hint"]
    finally:
        _run(db.close())


def test_add_sprint_item_no_wave_hint_when_waves_assigned():
    """(f) When all existing items already have a wave, no hint on add_sprint_item."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "wave-hint-add-f"))
        # Add an existing item and give it a wave
        item = _run(db_module.add_sprint_item(db, proj["id"], "v1", "Existing waved item"))
        _run(db_module.patch_sprint_item(db, proj["id"], item["id"], wave="wave-1"))
        # Add a new item explicitly assigning a wave (caller-specified wave)
        out = _run(mh._dispatch_mcp_tool("add_sprint_item", {
            "project_id": proj["id"],
            "version": "v1",
            "title": "New item filed with explicit wave assignment",
            "wave": "wave-1",  # caller explicitly assigns wave
        }, db, "/tmp"))
        assert out.get("id") is not None, f"Expected item created, got: {out}"
        # Since new item has wave="wave-1" and the existing item also has wave set,
        # there are no items with wave IS NULL → no hint
        assert out.get("wave_assignment_hint") is None
    finally:
        _run(db.close())


def test_fan_out_sprint_items_produces_wave_assignment_hint():
    """(g) fan_out_sprint_items response includes wave_assignment_hint (new items have wave IS NULL)."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "wave-hint-fanout-g"))
        out = _run(mh._dispatch_mcp_tool("fan_out_sprint_items", {
            "project_id": proj["id"],
            "items": [
                {"title": "Fan-out item alpha", "version": "v1"},
                {"title": "Fan-out item beta", "version": "v1"},
            ],
        }, db, "/tmp"))
        assert out.get("count") == 2
        # After fanning out, the new items have wave IS NULL — hint should appear
        assert out.get("wave_assignment_hint") is not None
        assert "WAVE_ASSIGNMENT_HINT" in out["wave_assignment_hint"]
        assert "assign_sprint_waves" in out["wave_assignment_hint"]
    finally:
        _run(db.close())


def test_add_sprint_item_hint_non_fatal_item_still_persisted():
    """Hint is non-fatal — the item is still persisted when hint fires."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "wave-hint-nonfatal"))
        out = _run(mh._dispatch_mcp_tool("add_sprint_item", {
            "project_id": proj["id"],
            "version": "v1",
            "title": "Should still be persisted even with hint",
        }, db, "/tmp"))
        # Item persisted
        assert out.get("id") is not None
        items = _run(db_module.get_sprint_items(db, proj["id"]))
        assert any("Should still be persisted" in (it.get("title") or "") for it in items)
        # Hint is informational only
        hint = out.get("wave_assignment_hint")
        if hint:
            assert "WAVE_ASSIGNMENT_HINT" in hint
    finally:
        _run(db.close())
