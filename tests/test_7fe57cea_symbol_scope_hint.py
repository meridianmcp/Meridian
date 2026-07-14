"""Tests for 7fe57cea — symbol-scope hint on file-only touches_resources.

Covers:
  (a) add_sprint_item with a file:-only resource and a title containing a
      likely symbol name produces a symbol_scope_hint (informational, non-fatal).
  (b) A symbol:-scoped resource produces no symbol_scope_hint.
  (c) Filing still succeeds in both cases (non-fatal, item is persisted).
  (d) fan_out_sprint_items with a file:-only resource in one item produces
      symbol_scope_hints in the batch result.
  (e) The pure helper _check_file_only_resources_warning covers edge cases:
      - file+symbol entry (no second ":" segment) triggers no warning
      - symbol: prefix triggers no warning
      - inferred: prefix triggers no warning
      - bare file: with a symbol name in title generates a hint with example
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
# Unit tests for the pure helper _check_file_only_resources_warning
# ---------------------------------------------------------------------------

def test_helper_file_only_with_symbol_candidate_in_title():
    """A bare file: entry + snake_case token in title produces a hint."""
    hint = mh._check_file_only_resources_warning(
        ["file:meridian/db/sprint_items.py"],
        "fix add_sprint_item to handle duplicate titles gracefully",
    )
    assert hint is not None
    assert "SYMBOL_SCOPE_HINT" in hint
    # The hint should reference the file-only entry
    assert "file:meridian/db/sprint_items.py" in hint
    # Should suggest the symbol-scoped form
    assert "file:path.py:symbol_name" in hint or "add_sprint_item" in hint


def test_helper_file_plus_symbol_no_warning():
    """A file: entry that already includes :symbol triggers no warning."""
    hint = mh._check_file_only_resources_warning(
        ["file:meridian/db/sprint_items.py:add_sprint_item"],
        "fix add_sprint_item to handle duplicate titles",
    )
    assert hint is None


def test_helper_symbol_prefix_no_warning():
    """A symbol: entry triggers no warning."""
    hint = mh._check_file_only_resources_warning(
        ["symbol:meridian/db/__init__.py::create_project"],
        "update create_project to accept optional description",
    )
    assert hint is None


def test_helper_inferred_prefix_no_warning():
    """An inferred: entry is auto-generated server-side and should not warn."""
    hint = mh._check_file_only_resources_warning(
        ["inferred:file:meridian/server.py"],
        "update server route for new endpoint",
    )
    assert hint is None


def test_helper_multiple_entries_mixed():
    """Mixed entries: only file-only ones appear in the hint."""
    hint = mh._check_file_only_resources_warning(
        [
            "file:meridian/mcp/handler.py",          # file-only — should warn
            "file:meridian/db/__init__.py:get_project",  # has symbol — OK
            "symbol:meridian/server.py::app",        # symbol: prefix — OK
        ],
        "update get_project and dispatch logic",
    )
    assert hint is not None
    assert "file:meridian/mcp/handler.py" in hint
    # The symbol-scoped and symbol: entries should NOT appear as warnings
    assert "file:meridian/db/__init__.py:get_project" not in hint.split("SYMBOL_SCOPE_HINT")[-1]


def test_helper_no_file_entries_no_warning():
    """No file: entries at all — no warning."""
    hint = mh._check_file_only_resources_warning(
        ["db:migrations", "route:/api/v1/items"],
        "add new migration for sprint items table",
    )
    assert hint is None


def test_helper_empty_resources_no_warning():
    """Empty/None resources — no warning."""
    assert mh._check_file_only_resources_warning(None, "some title") is None
    assert mh._check_file_only_resources_warning([], "some title") is None


def test_helper_no_candidates_still_returns_hint():
    """Even with no extractable symbol tokens, a bare file: entry still warns."""
    hint = mh._check_file_only_resources_warning(
        ["file:meridian/mcp/handler.py"],
        "fix the bug",  # too short/generic to extract identifiers
    )
    assert hint is not None
    assert "SYMBOL_SCOPE_HINT" in hint


# ---------------------------------------------------------------------------
# Integration tests via _dispatch_mcp_tool / _handle_sprint_tools
# ---------------------------------------------------------------------------

def test_add_sprint_item_file_only_resource_produces_hint():
    """(a) file:-only resource with a symbol-like title token → symbol_scope_hint,
    but item is still filed (non-fatal)."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "hint-proj-a"))
        out = _run(mh._dispatch_mcp_tool("add_sprint_item", {
            "project_id": proj["id"],
            "version": "v1",
            "title": "fix add_sprint_item duplicate handling",
            "touches_resources": ["file:meridian/mcp/handler.py"],
        }, db, "/tmp"))
        # Non-fatal — item should be filed
        assert "error" not in out or out.get("error") != "duplicate"
        assert out.get("id") or out.get("title")  # item was created
        # Hint is present
        assert out.get("symbol_scope_hint") is not None
        assert "SYMBOL_SCOPE_HINT" in out["symbol_scope_hint"]
        assert "file:meridian/mcp/handler.py" in out["symbol_scope_hint"]
        # Verify item actually persisted
        items = _run(db_module.get_sprint_items(db, proj["id"]))
        assert any("add_sprint_item" in (it.get("title") or "") for it in items)
    finally:
        _run(db.close())


def test_add_sprint_item_symbol_scoped_resource_no_hint():
    """(b) symbol:-scoped resource → no symbol_scope_hint."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "hint-proj-b"))
        out = _run(mh._dispatch_mcp_tool("add_sprint_item", {
            "project_id": proj["id"],
            "version": "v1",
            "title": "update create_project to accept optional metadata",
            "touches_resources": ["symbol:meridian/db/__init__.py::create_project"],
        }, db, "/tmp"))
        # No hint when properly symbol-scoped
        assert out.get("symbol_scope_hint") is None
        # Item still filed
        items = _run(db_module.get_sprint_items(db, proj["id"]))
        assert any("create_project" in (it.get("title") or "") for it in items)
    finally:
        _run(db.close())


def test_add_sprint_item_file_plus_symbol_no_hint():
    """file:path.py:symbol_name (the preferred form) → no hint."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "hint-proj-c"))
        out = _run(mh._dispatch_mcp_tool("add_sprint_item", {
            "project_id": proj["id"],
            "version": "v1",
            "title": "update add_sprint_item to accept wave parameter",
            "touches_resources": ["file:meridian/mcp/handler.py:add_sprint_item"],
        }, db, "/tmp"))
        assert out.get("symbol_scope_hint") is None
        items = _run(db_module.get_sprint_items(db, proj["id"]))
        assert any("wave" in (it.get("title") or "") for it in items)
    finally:
        _run(db.close())


def test_add_sprint_item_no_touches_resources_no_hint():
    """No touches_resources at all → no hint (inferred entries are exempt)."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "hint-proj-d"))
        out = _run(mh._dispatch_mcp_tool("add_sprint_item", {
            "project_id": proj["id"],
            "version": "v1",
            "title": "add new feature for dashboard export",
        }, db, "/tmp"))
        assert out.get("symbol_scope_hint") is None
    finally:
        _run(db.close())


def test_fan_out_sprint_items_file_only_produces_hints():
    """fan_out_sprint_items with a file:-only item → symbol_scope_hints list."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "hint-proj-fan"))
        out = _run(mh._dispatch_mcp_tool("fan_out_sprint_items", {
            "project_id": proj["id"],
            "items": [
                {
                    "title": "fix add_sprint_item file-only resource hint",
                    "version": "v1",
                    "touches_resources": ["file:meridian/mcp/handler.py"],
                },
                {
                    "title": "update fan_out_sprint_items to surface warnings",
                    "version": "v1",
                    "touches_resources": ["symbol:meridian/mcp/handler.py::fan_out"],
                },
            ],
        }, db, "/tmp"))
        # Result is success
        assert out.get("count") == 2
        assert len(out.get("item_ids", [])) == 2
        # Hints present for the file-only item
        hints = out.get("symbol_scope_hints", [])
        assert len(hints) == 1  # Only one item had a file-only resource
        assert "SYMBOL_SCOPE_HINT" in hints[0]
        assert "file:meridian/mcp/handler.py" in hints[0]
    finally:
        _run(db.close())


def test_fan_out_sprint_items_all_symbol_scoped_no_hints():
    """fan_out_sprint_items with all symbol-scoped items → no symbol_scope_hints."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "hint-proj-fan2"))
        out = _run(mh._dispatch_mcp_tool("fan_out_sprint_items", {
            "project_id": proj["id"],
            "items": [
                {
                    "title": "update create_project function",
                    "version": "v1",
                    "touches_resources": ["symbol:meridian/db/__init__.py::create_project"],
                },
                {
                    "title": "update get_project function",
                    "version": "v1",
                    "touches_resources": ["file:meridian/db/__init__.py:get_project"],
                },
            ],
        }, db, "/tmp"))
        assert out.get("count") == 2
        assert not out.get("symbol_scope_hints")
    finally:
        _run(db.close())
