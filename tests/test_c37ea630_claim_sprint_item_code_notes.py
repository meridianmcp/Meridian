"""c37ea630 — claim_sprint_item proactively surfaces code-anchored notes.

Tests that when a sprint item with touches_resources (file: entries) is claimed,
the MCP dispatch response includes a ``touches_resources_code_notes`` field
containing any code-anchored notes for those files -- mirroring the existing
``code_notes`` field on claim_file / get_file_claims.

Key properties:
- Fail-open: claim succeeds even when there are no matching code notes (field absent).
- Only files with actual notes appear in the list (compact output).
- File-level anchors (no symbol) surface; symbol-specific anchors also surface
  for the file-level lookup (file path extracted from symbol: entry).
- Items without touches_resources produce no touches_resources_code_notes field.
"""
from __future__ import annotations

import json

import pytest

import meridian.db as db_module
import meridian.server as srv  # noqa: F401 -- needed before importing handler


@pytest.mark.asyncio
async def test_claim_sprint_item_surfaces_code_notes_for_file_resources(db):
    """c37ea630 -- claim_sprint_item includes touches_resources_code_notes when
    the item's touches_resources has file: entries with code-anchored notes."""
    p = await db_module.create_project(db, "c37ea630-basic")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "test-worker")
    sid = sess["id"]

    fpath = "meridian/db/__init__.py"
    # Add a code-anchored note for the file.
    await db_module.add_project_note(
        db, pid, "Inline index outage rule",
        "Never add CREATE INDEX inline in the unguarded base schema -- 2026-07-04 outage",
        kind="code", file_path=fpath,
    )

    # Create a sprint item declaring this file in touches_resources.
    item = await db_module.add_sprint_item(
        db, pid, "v1", "Fix schema migration",
        touches_resources=json.dumps([f"file:{fpath}"]),
    )
    iid = item["id"]

    result = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": iid, "session_id": sid},
        db, "/tmp",
    )

    assert result.get("status") == "in_progress" or result.get("id") == iid, (
        f"Expected claimed item, got: {result}"
    )
    assert "touches_resources_code_notes" in result, (
        "claim_sprint_item should include touches_resources_code_notes when file has code notes"
    )
    notes_list = result["touches_resources_code_notes"]
    assert isinstance(notes_list, list)
    assert len(notes_list) == 1
    entry = notes_list[0]
    assert entry["file_path"] == fpath
    note_titles = [n["title"] for n in entry["notes"]]
    assert "Inline index outage rule" in note_titles


@pytest.mark.asyncio
async def test_claim_sprint_item_no_code_notes_means_field_absent(db):
    """c37ea630 -- when no code-anchored notes exist for the item's files,
    touches_resources_code_notes is absent from the claim response (not empty list)."""
    p = await db_module.create_project(db, "c37ea630-empty")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "test-worker-2")
    sid = sess["id"]

    # Item touches a file that has NO code-anchored notes.
    item = await db_module.add_sprint_item(
        db, pid, "v1", "Edit a file with no notes",
        touches_resources=json.dumps(["file:meridian/static/dashboard.css"]),
    )
    iid = item["id"]

    result = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": iid, "session_id": sid},
        db, "/tmp",
    )

    assert result.get("status") == "in_progress" or result.get("id") == iid
    # Field should be absent (not present with empty list) when no notes exist.
    assert "touches_resources_code_notes" not in result, (
        "touches_resources_code_notes should be absent when no code notes exist for the file"
    )


@pytest.mark.asyncio
async def test_claim_sprint_item_no_touches_resources_means_no_field(db):
    """c37ea630 -- items without any touches_resources produce no
    touches_resources_code_notes field in the claim response."""
    p = await db_module.create_project(db, "c37ea630-nores")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "test-worker-3")
    sid = sess["id"]

    item = await db_module.add_sprint_item(
        db, pid, "v1", "Generic task with no resource declarations",
    )
    iid = item["id"]

    result = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": iid, "session_id": sid},
        db, "/tmp",
    )

    assert result.get("status") == "in_progress" or result.get("id") == iid
    assert "touches_resources_code_notes" not in result


@pytest.mark.asyncio
async def test_claim_sprint_item_symbol_resource_extracts_file_for_notes(db):
    """c37ea630 -- touches_resources with symbol: entries have their file portion
    extracted and checked for code-anchored notes (file-level anchors surface)."""
    p = await db_module.create_project(db, "c37ea630-sym")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "test-worker-4")
    sid = sess["id"]

    fpath = "meridian/mcp/handler.py"
    # Add a file-level code note for the file the symbol lives in.
    await db_module.add_project_note(
        db, pid, "Handler dispatch safety note",
        "Always fail-open: errors in dispatch must not block the claim",
        kind="code", file_path=fpath,
    )

    # Item's touches_resources uses a symbol: entry (file::symbol format).
    item = await db_module.add_sprint_item(
        db, pid, "v1", "Refactor dispatch",
        touches_resources=json.dumps([f"symbol:{fpath}::_dispatch_mcp_tool"]),
    )
    iid = item["id"]

    result = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": iid, "session_id": sid},
        db, "/tmp",
    )

    assert result.get("status") == "in_progress" or result.get("id") == iid
    assert "touches_resources_code_notes" in result, (
        "symbol: resource should trigger file-path lookup and surface code notes"
    )
    entries = result["touches_resources_code_notes"]
    assert any(e["file_path"] == fpath for e in entries)
    all_titles = [n["title"] for e in entries for n in e["notes"]]
    assert "Handler dispatch safety note" in all_titles


@pytest.mark.asyncio
async def test_claim_sprint_item_multiple_files_only_those_with_notes(db):
    """c37ea630 -- when multiple files are declared, only those with code notes
    appear in touches_resources_code_notes; files without notes are omitted."""
    p = await db_module.create_project(db, "c37ea630-multi")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "test-worker-5")
    sid = sess["id"]

    file_with_note = "meridian/db/migrations.py"
    file_without_note = "meridian/db/workspace.py"

    await db_module.add_project_note(
        db, pid, "Migration ordering note",
        "Append only; never reorder existing migration entries",
        kind="code", file_path=file_with_note,
    )

    item = await db_module.add_sprint_item(
        db, pid, "v1", "Add migration and update workspace",
        touches_resources=json.dumps([
            f"file:{file_with_note}",
            f"file:{file_without_note}",
        ]),
    )
    iid = item["id"]

    result = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": iid, "session_id": sid},
        db, "/tmp",
    )

    assert result.get("status") == "in_progress" or result.get("id") == iid
    assert "touches_resources_code_notes" in result
    entries = result["touches_resources_code_notes"]
    file_paths_in_result = {e["file_path"] for e in entries}
    assert file_with_note in file_paths_in_result
    # File without notes should NOT appear.
    assert file_without_note not in file_paths_in_result
