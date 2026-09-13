"""Tests for sprint item 73499c59 — proposal_to_handoff orchestration command
with a typed proposal-run receipt.

Covers the new module (:mod:`meridian.proposal_handoff`) directly, its MCP
handler (:func:`meridian.mcp.handlers.sprint_tools.handle_proposal_to_handoff`),
and the tool's wiring into ``_handle_sprint_tools``'s dispatch table in
``meridian/mcp/handler.py`` — the same three layers
tests/test_ba4f879b_sprint_tools_dispatch.py exercises for its own tools.

Uses the standard ``db`` fixture (in-memory SQLite, see conftest.py) exactly
like its sibling proposal/handoff integration suites (e.g.
tests/test_research_os_end_to_end.py, tests/test_proposal_handoff_contract.py).
"""
from __future__ import annotations

import pytest

import meridian.server  # noqa: F401 — must be imported before handler to avoid cycle
from meridian import db as db_module
from meridian import proposal_handoff as ph_module
from meridian.mcp import handler as mh
from meridian.mcp.handlers import sprint_tools as st_mod

pytestmark = pytest.mark.asyncio


async def _setup(
    db,
    title: str = "Add typed proposal-run receipts to proposal_to_handoff",
    body: str = "Investigate and ship the typed receipt object end to end.",
):
    project = await db_module.create_project(db, "proposal-handoff-test")
    pid = project["id"]
    session = await db_module.register_session(db, pid, "proposal-handoff-session")
    sid = session["id"]
    proposal = await db_module.add_workspace_proposal(
        db, title, body, actor="tester", session_id=sid,
    )
    return pid, sid, proposal["id"]


# ---------------------------------------------------------------------------
# Core orchestration: meridian.proposal_handoff.proposal_to_handoff
# ---------------------------------------------------------------------------

async def test_creates_items_pointers_and_scoped_handoff(db, tmp_path):
    pid, sid, proposal_id = await _setup(db)
    items = [
        {
            "title": "Design the ProposalRunReceipt schema",
            "touches_resources": ["file:meridian/proposal_handoff.py"],
        },
        {
            # "$0" references the sibling entry above by index, resolved to
            # its REAL sprint-item id before add_sprint_item ever sees it.
            "title": "Wire proposal_to_handoff into sprint_tools",
            "depends_on": "$0",
        },
    ]

    receipt = await ph_module.proposal_to_handoff(
        db, pid, proposal_id, items, str(tmp_path), session_id=sid, mode="goal",
    )

    assert len(receipt.created_item_ids) == 2
    assert receipt.skipped_items == []
    assert receipt.proposal_id == proposal_id
    assert receipt.project_id == pid

    # Every created item got exactly one durable pointer back to the proposal.
    assert set(receipt.pointer_ids) == set(receipt.created_item_ids)
    assert receipt.pointer_errors == {}
    for item_id, pointer_id in receipt.pointer_ids.items():
        pointers = await db_module.get_sprint_item_pointers(db, item_id)
        matched = [p for p in pointers if p["id"] == pointer_id]
        assert len(matched) == 1, "the receipt's pointer_id must be a REAL, durable pointer row"
        pointer = matched[0]
        assert pointer["source_type"] == "proposal"
        assert pointer["targets"][0]["selector"]["id"] == proposal_id
        assert pointer["targets"][0]["selector"]["type"] == "node_id"

    # The "$0" sibling reference resolved to the first item's real id.
    second_item = await db_module.get_sprint_item(db, receipt.created_item_ids[1])
    assert second_item["depends_on"] == receipt.created_item_ids[0]

    # A handoff was generated, scoped to exactly these 2 new items, and the
    # receipt's executable verdict is a direct passthrough of that handoff's
    # own unified proposal-run-scope contract (never a second computation).
    assert receipt.handoff_error is None
    assert receipt.handoff_path
    assert receipt.handoff_content_length and receipt.handoff_content_length > 0
    assert receipt.executable is True
    assert receipt.executable_reasons == []
    assert receipt.hitl_filed is None
    assert receipt.deviation_category is None
    assert receipt.proposal_scope_hash

    # The typed receipt is exposed as a plain JSON-serializable dict at the
    # API boundary (what the MCP handler actually returns).
    as_dict = receipt.to_dict()
    assert as_dict["created_item_ids"] == receipt.created_item_ids
    assert as_dict["executable"] is True


async def test_duplicate_title_is_skipped_not_a_crash(db, tmp_path):
    pid, sid, proposal_id = await _setup(db)
    # Seed an existing pending item with the EXACT title one spec will reuse.
    await db_module.add_sprint_item(
        db, pid, "current", "Ship the typed proposal run receipt object",
    )
    items = [
        {"title": "Ship the typed proposal run receipt object"},  # duplicate
        {"title": "Attach durable pointers back to the source proposal"},
    ]

    receipt = await ph_module.proposal_to_handoff(
        db, pid, proposal_id, items, str(tmp_path), session_id=sid, mode="goal",
    )

    assert len(receipt.created_item_ids) == 1
    assert len(receipt.skipped_items) == 1
    assert receipt.skipped_items[0]["index"] == 0
    assert receipt.skipped_items[0]["reason"] == "duplicate"
    assert receipt.skipped_items[0]["existing"] is not None
    # The batch is NOT aborted: the second, genuinely distinct item still
    # gets created and pointered.
    assert receipt.pointer_ids


async def test_missing_title_entry_is_skipped(db, tmp_path):
    pid, sid, proposal_id = await _setup(db)
    items = [{"title": "   "}, {"title": "A real item"}]

    receipt = await ph_module.proposal_to_handoff(
        db, pid, proposal_id, items, str(tmp_path), session_id=sid, skip_handoff=True,
    )

    assert len(receipt.created_item_ids) == 1
    created_item = await db_module.get_sprint_item(db, receipt.created_item_ids[0])
    assert created_item["title"] == "A real item"
    assert len(receipt.skipped_items) == 1
    assert receipt.skipped_items[0]["index"] == 0
    assert receipt.skipped_items[0]["reason"] == "missing_title"


async def test_requires_nonempty_items(db, tmp_path):
    pid, sid, proposal_id = await _setup(db)
    with pytest.raises(ValueError, match="at least one entry"):
        await ph_module.proposal_to_handoff(db, pid, proposal_id, [], str(tmp_path))


async def test_unknown_proposal_raises_before_any_write(db, tmp_path):
    project = await db_module.create_project(db, "proposal-handoff-missing-proposal")
    with pytest.raises(ValueError, match="not found"):
        await ph_module.proposal_to_handoff(
            db, project["id"], "00000000-0000-0000-0000-000000000000",
            [{"title": "x"}], str(tmp_path),
        )
    # Nothing was created.
    items = await db_module.get_sprint_items(db, project["id"])
    assert items == []


async def test_unknown_project_raises(db, tmp_path):
    pid, sid, proposal_id = await _setup(db)
    with pytest.raises(ValueError, match="not found"):
        await ph_module.proposal_to_handoff(
            db, "00000000-0000-0000-0000-000000000000", proposal_id,
            [{"title": "x"}], str(tmp_path),
        )


async def test_skip_handoff_flag_reports_non_executable_with_reason(db, tmp_path):
    pid, sid, proposal_id = await _setup(db)
    receipt = await ph_module.proposal_to_handoff(
        db, pid, proposal_id, [{"title": "Add a focused unit test"}], str(tmp_path),
        session_id=sid, skip_handoff=True,
    )
    assert receipt.created_item_ids
    assert receipt.handoff_path is None
    assert receipt.executable is False
    assert receipt.executable_reasons == ["handoff_skipped"]


# ---------------------------------------------------------------------------
# HITL deviation gate (reuses meridian.proposal_promotion._classify_deviation)
# ---------------------------------------------------------------------------

async def test_destructive_deviation_files_a_hitl_gate(db, tmp_path):
    pid, sid, proposal_id = await _setup(
        db,
        title="Wipe legacy telemetry rows before the migration",
        body="One-time cleanup task that deletes rows predating the schema change.",
    )
    receipt = await ph_module.proposal_to_handoff(
        db, pid, proposal_id, [{"title": "Purge the legacy telemetry table"}],
        str(tmp_path), session_id=sid, mode="goal",
    )

    assert receipt.deviation_category == "destructive_behavior"
    assert receipt.hitl_filed is not None
    assert receipt.hitl_filed.get("kind") == "proposal_deviation"
    live = await db_module.list_hitl_requests(db, pid, status="pending")
    assert any(h["id"] == receipt.hitl_filed["id"] for h in live)
    # A live HITL gate is surfaced but does not, by itself, flip executable —
    # matches build_proposal_run_scope's own contract (only a genuinely
    # FAILED capability or structural contradiction does that).
    assert any(g.get("id") == receipt.hitl_filed["id"] for g in receipt.hitl_gates)


async def test_override_reason_bypasses_the_hitl_gate(db, tmp_path):
    pid, sid, proposal_id = await _setup(
        db,
        title="Wipe legacy telemetry rows before the migration",
        body="One-time cleanup task that deletes rows predating the schema change.",
    )
    receipt = await ph_module.proposal_to_handoff(
        db, pid, proposal_id, [{"title": "Purge the legacy telemetry table"}],
        str(tmp_path), session_id=sid, mode="goal",
        override_reason="Reviewed with the team; safe one-time cleanup.",
    )

    assert receipt.deviation_category == "destructive_behavior"
    assert receipt.hitl_filed is None
    assert receipt.deviation_override_reason == "Reviewed with the team; safe one-time cleanup."
    live = await db_module.list_hitl_requests(db, pid, status="pending")
    assert live == []


# ---------------------------------------------------------------------------
# MCP handler + dispatch-table wiring
# ---------------------------------------------------------------------------

async def test_handle_proposal_to_handoff_mcp_tool(db, tmp_path):
    pid, sid, proposal_id = await _setup(db)
    args = {
        "project_id": pid,
        "proposal_id": proposal_id,
        "session_id": sid,
        "items": [{"title": "Implement the orchestration command"}],
        "mode": "goal",
    }
    result = await st_mod.handle_proposal_to_handoff(args, db, str(tmp_path), None, None)
    assert "error" not in result
    assert len(result["created_item_ids"]) == 1
    assert result["executable"] is True
    assert result["pointer_ids"]


async def test_handle_proposal_to_handoff_validates_required_args(db, tmp_path):
    missing_project = await st_mod.handle_proposal_to_handoff(
        {"proposal_id": "x", "items": [{"title": "y"}]}, db, str(tmp_path), None, None,
    )
    assert missing_project == {"error": "project_id is required (or pass project_name)"}

    missing_proposal = await st_mod.handle_proposal_to_handoff(
        {"project_id": "x", "items": [{"title": "y"}]}, db, str(tmp_path), None, None,
    )
    assert missing_proposal == {"error": "proposal_id is required"}

    empty_items = await st_mod.handle_proposal_to_handoff(
        {"project_id": "x", "proposal_id": "y", "items": []}, db, str(tmp_path), None, None,
    )
    assert "error" in empty_items


async def test_handler_surfaces_not_found_as_clean_error_not_a_crash(db, tmp_path):
    project = await db_module.create_project(db, "proposal-handoff-handler-nf")
    result = await st_mod.handle_proposal_to_handoff(
        {
            "project_id": project["id"],
            "proposal_id": "00000000-0000-0000-0000-000000000000",
            "items": [{"title": "x"}],
        },
        db, str(tmp_path), None, None,
    )
    assert "error" in result
    assert "not found" in result["error"]


async def test_dispatched_via_handle_sprint_tools(db, tmp_path):
    pid, sid, proposal_id = await _setup(db)
    args = {
        "project_id": pid,
        "proposal_id": proposal_id,
        "session_id": sid,
        "items": [{"title": "Reach the handler through _handle_sprint_tools"}],
    }
    result = await mh._handle_sprint_tools(
        "proposal_to_handoff", args, db, str(tmp_path), None, None,
    )
    assert result is not mh._MISS
    assert result["created_item_ids"]


async def test_handle_proposal_to_handoff_is_exported_from_sprint_tools():
    assert hasattr(st_mod, "handle_proposal_to_handoff")
    import inspect
    assert inspect.iscoroutinefunction(st_mod.handle_proposal_to_handoff)
