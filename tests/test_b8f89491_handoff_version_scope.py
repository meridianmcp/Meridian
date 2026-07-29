"""b8f89491 — every paste-ready handoff mode honors session sprint scope."""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module
import meridian.server  # noqa: F401 — load the server before handler to avoid its import cycle
from meridian.mcp import handler as mcp_handler


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "delta", "starter", "goal"])
async def test_all_handoff_modes_scope_to_session_version(db, tmp_path, mode):
    project = await db_module.create_project(db, f"handoff-scope-{mode}")
    await db_module.add_sprint_item(
        db, project["id"], "v0.2.5", "old unrelated backlog item"
    )
    target = await db_module.add_sprint_item(
        db, project["id"], "v0.2.6", "target scoped sprint item"
    )
    session = await db_module.register_session(
        db, project["id"], f"executor-{mode}", sprint_version="v0.2.6"
    )

    _, content, _ = await handoff_module.generate_handoff(
        db,
        project["id"],
        str(tmp_path),
        skip_ai_summary=True,
        mode=mode,
        session_id=session["id"],
    )

    assert target["id"][:8] in content
    assert "old unrelated backlog item" not in content


@pytest.mark.asyncio
async def test_explicit_version_overrides_session_scope_for_full_handoff(db, tmp_path):
    project = await db_module.create_project(db, "handoff-explicit-version")
    await db_module.add_sprint_item(db, project["id"], "v1", "session item")
    target = await db_module.add_sprint_item(
        db, project["id"], "v2", "explicit target item"
    )
    session = await db_module.register_session(
        db, project["id"], "executor-explicit", sprint_version="v1"
    )

    _, content, _ = await handoff_module.generate_handoff(
        db,
        project["id"],
        str(tmp_path),
        skip_ai_summary=True,
        mode="full",
        session_id=session["id"],
        version="v2",
    )

    assert target["id"][:8] in content
    assert "explicit target item" in content
    assert "session item" not in content


@pytest.mark.asyncio
async def test_unscoped_full_handoff_retains_cross_version_behavior(db, tmp_path):
    project = await db_module.create_project(db, "handoff-unscoped")
    first = await db_module.add_sprint_item(db, project["id"], "v1", "v1 item")
    second = await db_module.add_sprint_item(db, project["id"], "v2", "v2 item")

    _, content, _ = await handoff_module.generate_handoff(
        db, project["id"], str(tmp_path), skip_ai_summary=True, mode="goal"
    )

    assert first["id"][:8] in content
    assert second["id"][:8] in content


@pytest.mark.asyncio
async def test_readiness_header_uses_version_scoped_sprint_text_not_global(db, tmp_path):
    """The '=== HANDOFF READINESS ===' header must not leak the project-global
    legacy goal.sprint text into a version-scoped session's handoff, even
    though item-level filtering was already correctly scoped. Regression for
    the gap _resolve_session_sprint_version's own tests didn't cover: only
    2 items across 2 versions never exercised the readiness block, which
    reads a completely separate (project-global) data source (goal.sprint)
    than the pending-item list.
    """
    project = await db_module.create_project(db, "handoff-readiness-scope")
    await db_module.set_goal(
        db, project["id"], "unscoped content",
        sprint="GLOBAL-WRONG-SCOPE legacy v0.2.5 text",
    )
    await db_module.add_sprint_item(db, project["id"], "v0.2.5", "old backlog item")
    await db_module.add_sprint_item(db, project["id"], "v0.2.6", "target item")
    # Human override, applied AFTER the items exist — add_sprint_item's own
    # auto-regeneration (f9188526) only re-synthesizes on a SUBSEQUENT add,
    # so this persists through generate_handoff (which never calls
    # add_sprint_item itself).
    await db_module.upsert_sprint_version_description(
        db, project["id"], "v0.2.6", "deterministic capability handoffs"
    )
    session = await db_module.register_session(
        db, project["id"], "executor-readiness", sprint_version="v0.2.6"
    )

    _, content, _ = await handoff_module.generate_handoff(
        db, project["id"], str(tmp_path),
        skip_ai_summary=True, mode="full", session_id=session["id"],
    )

    assert "v0.2.6 — deterministic capability handoffs" in content
    assert "GLOBAL-WRONG-SCOPE" not in content


@pytest.mark.asyncio
async def test_mcp_handoff_forwards_version_and_returns_scope_metadata(db, tmp_path):
    project = await db_module.create_project(db, "mcp-handoff-scope")
    await db_module.add_sprint_item(
        db, project["id"], "v1", "wrong MCP scope unique b8", force=True
    )
    await db_module.add_sprint_item(
        db, project["id"], "v2", "correct MCP scope unique b8", force=True
    )
    target = (await db_module.get_sprint_items(db, project["id"], version="v2"))[0]
    session = await db_module.register_session(
        db, project["id"], "mcp-executor", sprint_version="v1"
    )

    result = await mcp_handler._dispatch_mcp_tool(
        "generate_handoff",
        {
            "project_id": project["id"],
            "session_id": session["id"],
            "mode": "goal",
            "version": "v2",
        },
        db,
        str(tmp_path),
    )

    assert target["id"][:8] in result["content"]
    assert "wrong MCP scope" not in result["content"]
    assert result["scope"] == {
        "requested_version": "v2",
        "effective_version": "v2",
        "session_id": session["id"],
    }
