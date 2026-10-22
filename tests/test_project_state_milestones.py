from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian.mcp_tools import _MCP_TOOLS_LIST, _READ_ONLY_TOOLS
from meridian.pointers import build_project_state_milestone_pointer, resolve_pointer
from meridian.session_milestones import (
    _manifest_receipt,
    _safe_text,
    assess_checkpoint_escalation,
    maybe_capture_project_state_milestone,
)


def test_checkpoint_escalation_handles_material_and_adaptive_risk():
    routine = assess_checkpoint_escalation(milestone_trigger=None, risk_signals=[])
    assert routine["captured"] is False
    assert routine["risk_threshold"] == 4

    material = assess_checkpoint_escalation(
        milestone_trigger="goal_scope_changed", risk_signals=[]
    )
    assert material["captured"] is True
    assert material["reason"] == "material_transition"

    risk = assess_checkpoint_escalation(
        milestone_trigger=None,
        risk_signals=["provider_unavailable", "dirty_worktree"],
    )
    assert risk["captured"] is True
    assert risk["risk_score"] == 5

    adapted = assess_checkpoint_escalation(
        milestone_trigger=None,
        risk_signals=["provider_unavailable", "dirty_worktree"],
        recent_milestone_count=12,
    )
    assert adapted["captured"] is False
    assert adapted["risk_threshold"] == 7

    immediate = assess_checkpoint_escalation(
        milestone_trigger=None, risk_signals=["artifact_hash_mismatch"]
    )
    assert immediate["captured"] is True
    with pytest.raises(ValueError, match="milestone_trigger"):
        assess_checkpoint_escalation(milestone_trigger="unknown", risk_signals=[])


@pytest.mark.asyncio
async def test_milestone_snapshot_keeps_local_manifest_as_unverified_metadata(db):
    project = await db_module.create_project(db, "project-state-milestone-snapshot")
    session = await db_module.register_session(db, project["id"], "milestone-session")
    await db_module.set_goal(
        db, project["id"], "Current sprint goal", north_star="Meridian project scope"
    )
    note_body = "private note body stays out of the snapshot"
    decision_body = "private decision body stays out of the snapshot"
    insight_body = "private insight body stays out of the snapshot"
    await db_module.add_project_note(
        db, project["id"], "Reference note", note_body, kind="reference"
    )
    await db_module.pin_decision(db, project["id"], "Pinned decision", decision_body)
    await db_module.create_insight(
        db, project["id"], "Strategic insight", insight_body, horizon="permanent"
    )
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Visible active item", prospect_bypass=True
    )
    await db_module.add_sprint_item_pointer(
        db,
        project["id"],
        item["id"],
        "code",
        [{"uri": "a.py", "selector": {"type": "range", "start_line": 1, "end_line": 2}}],
        label="source pointer",
    )
    digest = "a" * 64

    routine = await maybe_capture_project_state_milestone(
        db, project["id"], session["id"]
    )
    assert routine["captured"] is False

    first = await maybe_capture_project_state_milestone(
        db,
        project["id"],
        session["id"],
        milestone_trigger="goal_scope_changed",
    )
    assert first["captured"] is True

    result = await maybe_capture_project_state_milestone(
        db,
        project["id"],
        session["id"],
        risk_signals=["provider_unavailable", "dirty_worktree"],
        artifact_manifest={
            "status": "available",
            "sha256": digest,
            "occurrence_count": 2,
            "local_root": "C:/Users/private/project",
            "raw_content": "must not enter the hosted snapshot",
        },
    )

    assert result["captured"] is True
    milestone = result["milestone"]
    assert milestone["integrity_verified"] is True
    snapshot = milestone["snapshot"]
    assert snapshot["goal_scope"]["project_name"] == project["name"]
    assert snapshot["goal_scope"]["goal_content"] == "Current sprint goal"
    assert snapshot["goal_scope"]["north_star"] == "Meridian project scope"
    assert snapshot["sprint_progress"]["counts_by_status"]["pending"] == 1
    assert snapshot["sprint_progress"]["items"][0]["id"] == item["id"]
    assert snapshot["durable_records"]["notes"][0]["content_sha256"]
    assert snapshot["durable_records"]["decisions"][0]["content_sha256"]
    assert snapshot["durable_records"]["insights"][0]["content_sha256"]
    assert snapshot["evidence_pointers"][0]["pointer_sha256"]
    assert all(
        body not in str(snapshot)
        for body in (note_body, decision_body, insight_body)
    )
    assert snapshot["artifact_manifest"] == {
        "status": "available",
        "sha256": digest,
        "occurrence_count": 2,
        "source": "local_client_report",
        "server_verified": False,
    }
    assert snapshot["source_authority"]["raw_chat"] == "not_copied"
    assert snapshot["source_authority"]["artifact_bytes"] == "not_copied"
    assert "C:/Users/private/project" not in str(snapshot)
    assert "must not enter the hosted snapshot" not in str(snapshot)
    assert result["pointer"]["source_type"] == "project_state_milestone"
    assert result["escalation"]["recent_milestones_24h"] == 1


@pytest.mark.asyncio
async def test_milestone_pointer_is_hash_checked_and_project_scoped(db):
    project = await db_module.create_project(db, "project-state-pointer-owner")
    other_project = await db_module.create_project(db, "project-state-pointer-other")
    session = await db_module.register_session(db, project["id"], "milestone-pointer-session")
    result = await maybe_capture_project_state_milestone(
        db,
        project["id"],
        session["id"],
        milestone_trigger="recovery_verified",
    )
    pointer = build_project_state_milestone_pointer(result["milestone"])

    resolved = await resolve_pointer(
        db, pointer, project_id=project["id"], citation_resolver=lambda *_args: None
    )
    assert resolved["targets"][0]["resolved"] is True
    foreign = await resolve_pointer(
        db, pointer, project_id=other_project["id"], citation_resolver=lambda *_args: None
    )
    assert foreign["targets"][0]["resolved"] is False

    tampered = {
        **pointer,
        "targets": [
            {
                **pointer["targets"][0],
                "selector": {
                    **pointer["targets"][0]["selector"],
                    "content_hash": "0" * 64,
                },
            }
        ],
    }
    rejected = await resolve_pointer(
        db, tampered, project_id=project["id"], citation_resolver=lambda *_args: None
    )
    assert rejected["targets"][0]["resolved"] is False


@pytest.mark.asyncio
async def test_milestones_form_an_append_only_hash_chain(db):
    project = await db_module.create_project(db, "project-state-milestone-chain")
    session = await db_module.register_session(db, project["id"], "milestone-chain-session")
    first = await db_module.append_project_state_milestone(
        db,
        project["id"],
        session["id"],
        trigger="manual_requested",
        risk_score=0,
        risk_threshold=4,
        snapshot={"captured_at": "2026-10-03T00:00:00Z", "step": 1},
    )
    second = await db_module.append_project_state_milestone(
        db,
        project["id"],
        session["id"],
        trigger="manual_requested",
        risk_score=0,
        risk_threshold=4,
        snapshot={"captured_at": "2026-10-03T00:01:00Z", "step": 2},
    )

    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert second["previous_hash"] == first["content_hash"]
    assert first["integrity_verified"] is True
    assert second["integrity_verified"] is True
    assert [row["sequence"] for row in await db_module.list_project_state_milestones(db, project["id"])] == [2, 1]

    # PostgreSQL is exercised through its migration trigger in the integration
    # suite; this direct statement checks the SQLite append-only trigger.
    if not hasattr(db, "_pool"):
        with pytest.raises(Exception):
            await db.execute(
                "UPDATE project_state_milestones SET trigger = 'tampered' WHERE id = ?",
                (first["id"],),
            )


def test_checkpoint_and_read_tool_expose_the_milestone_contract():
    from meridian.pg_adapter import _PG_MIGRATIONS_LATE, _migrate_pg_project_state_milestones

    tools_by_name = {tool["name"]: tool for tool in _MCP_TOOLS_LIST}
    checkpoint = tools_by_name["checkpoint"]["inputSchema"]["properties"]
    assert {"milestone_trigger", "risk_signals", "artifact_manifest"} <= set(checkpoint)
    assert "get_project_state_milestones" in tools_by_name
    assert "get_project_state_milestones" in _READ_ONLY_TOOLS
    assert _migrate_pg_project_state_milestones in _PG_MIGRATIONS_LATE


@pytest.mark.asyncio
async def test_milestone_read_handler_pages_with_project_scope(db):
    from meridian.mcp.handlers.session_tools import handle_get_project_state_milestones

    project = await db_module.create_project(db, "project-state-read-handler")
    session = await db_module.register_session(db, project["id"], "milestone-read-session")
    for step in (1, 2):
        await db_module.append_project_state_milestone(
            db,
            project["id"],
            session["id"],
            trigger="manual_requested",
            risk_score=0,
            risk_threshold=4,
            snapshot={"captured_at": f"2026-10-03T00:0{step}:00Z", "step": step},
        )

    latest = await handle_get_project_state_milestones(
        {"project_id": project["id"], "limit": 1}, db, "", None, None
    )
    assert latest["project_id"] == project["id"]
    assert [row["sequence"] for row in latest["milestones"]] == [2]
    assert latest["next_before_sequence"] == 2

    earlier = await handle_get_project_state_milestones(
        {"project_id": project["id"], "limit": 1, "before_sequence": 2},
        db,
        "",
        None,
        None,
    )
    assert [row["sequence"] for row in earlier["milestones"]] == [1]


def test_manifest_receipt_validation_and_safe_text_boundaries():
    assert _manifest_receipt(None)["status"] == "not_reported"
    with pytest.raises(ValueError, match="must be an object"):
        _manifest_receipt([])
    with pytest.raises(ValueError, match="status is not supported"):
        _manifest_receipt({"status": "unknown"})
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        _manifest_receipt({"status": "available", "sha256": "ABC"})
    with pytest.raises(ValueError, match="non-negative integer"):
        _manifest_receipt({"status": "available", "occurrence_count": True})
    with pytest.raises(ValueError, match="non-negative integer"):
        _manifest_receipt({"status": "available", "occurrence_count": -1})

    assert _safe_text(None) is None
    assert _safe_text(12) is None
    assert _safe_text("") is None
    assert _safe_text("bounded text", limit=7) == "bounded"


@pytest.mark.asyncio
async def test_milestone_capture_rejects_missing_project_and_foreign_session(db):
    project = await db_module.create_project(db, "project-state-milestone-scope")
    other_project = await db_module.create_project(db, "project-state-milestone-foreign")
    session = await db_module.register_session(db, other_project["id"], "foreign-session")

    with pytest.raises(ValueError, match="project was not found"):
        await maybe_capture_project_state_milestone(
            db, "missing-project", session["id"], milestone_trigger="manual_requested"
        )
    with pytest.raises(ValueError, match="does not belong"):
        await maybe_capture_project_state_milestone(
            db, project["id"], session["id"], milestone_trigger="manual_requested"
        )
