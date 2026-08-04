"""Tests for ecc8b280 — machine-readable continuation/terminal-ready gate.

Closes an observed premature-termination / reward-hacking failure mode: an
autonomous executor completed a batch, explicitly acknowledged two newly
claimed items and a remaining batch, then yielded with "let me know" despite
running in autonomous/no-confirmation mode and no genuine blocker on file.

Covers:
  * the pure `meridian.continuation_gate.compute_continuation_state` helper
    (remaining pending items, remaining in_progress items, genuine blocker
    escape, autonomous vs interactive mode, empty/terminal board);
  * `generate_handoff`'s `strict_continuation`/`checkpoint` gate (full mode);
  * the `continuation` field on `get_sprint_progress`;
  * the `continuation` advisory field on `complete_sprint_item`;
  * the reward-hacking regression itself: completing part of a batch while
    actionable work remains must never report terminal_ready=True.
"""
from __future__ import annotations

import pytest

from meridian import continuation_gate
from meridian import db as db_module
from meridian import handoff as handoff_module


# ---------------------------------------------------------------------------
# Pure helper: compute_continuation_state
# ---------------------------------------------------------------------------


def test_no_items_is_terminal_ready():
    state = continuation_gate.compute_continuation_state([])
    assert state["terminal_ready"] is True
    assert state["continuation_required"] is False
    assert state["actionable_count"] == 0


def test_all_done_items_is_terminal_ready():
    items = [
        {"id": "a", "status": "done"},
        {"id": "b", "status": "failed"},
        {"id": "c", "status": "skipped"},
    ]
    state = continuation_gate.compute_continuation_state(items, execution_mode="autonomous")
    assert state["terminal_ready"] is True
    assert state["continuation_required"] is False
    assert state["actionable_count"] == 0


def test_remaining_pending_items_requires_continuation():
    items = [{"id": "a", "status": "pending"}]
    state = continuation_gate.compute_continuation_state(items, execution_mode="autonomous")
    assert state["continuation_required"] is True
    assert state["terminal_ready"] is False
    assert state["actionable_pending_count"] == 1
    assert state["actionable_in_progress_count"] == 0
    assert state["actionable_item_ids"] == ["a"]


def test_remaining_in_progress_items_requires_continuation():
    items = [{"id": "a", "status": "in_progress"}]
    state = continuation_gate.compute_continuation_state(items, execution_mode="autonomous")
    assert state["continuation_required"] is True
    assert state["terminal_ready"] is False
    assert state["actionable_in_progress_count"] == 1
    assert state["actionable_pending_count"] == 0


def test_todo_status_also_counts_as_actionable():
    items = [{"id": "a", "status": "todo"}]
    state = continuation_gate.compute_continuation_state(items, execution_mode="autonomous")
    assert state["continuation_required"] is True


def test_genuine_blocker_escape():
    """A pending item with a structured blocker_kind is NOT actionable —
    the genuine-blocker escape hatch."""
    items = [{"id": "a", "status": "pending", "blocker_kind": "manual"}]
    state = continuation_gate.compute_continuation_state(items, execution_mode="autonomous")
    assert state["continuation_required"] is False
    assert state["terminal_ready"] is True
    assert state["blocked_count"] == 1
    assert state["blocked_item_ids"] == ["a"]
    assert state["actionable_count"] == 0


def test_notes_only_blocker_claim_does_not_escape():
    """The exact reward-hacking shape this gate exists to catch: prose in
    notes claiming a blocker, but no structured blocker_kind set, must NOT
    be treated as a genuine blocker."""
    items = [
        {
            "id": "a",
            "status": "pending",
            "blocker_kind": None,
            "notes": "blocked, waiting on human input",
        }
    ]
    state = continuation_gate.compute_continuation_state(items, execution_mode="autonomous")
    assert state["continuation_required"] is True
    assert state["blocked_count"] == 0


def test_interactive_mode_never_requires_hard_continuation():
    """Interactive mode already gates every claim on human confirmation —
    the hard continuation block only applies to autonomous mode."""
    items = [{"id": "a", "status": "pending"}]
    state = continuation_gate.compute_continuation_state(items, execution_mode="interactive")
    assert state["continuation_required"] is False
    assert state["terminal_ready"] is True
    assert state["execution_mode"] == "interactive"


def test_unknown_execution_mode_normalizes_to_autonomous():
    items = [{"id": "a", "status": "pending"}]
    state = continuation_gate.compute_continuation_state(items, execution_mode="bogus")
    assert state["execution_mode"] == "autonomous"
    assert state["continuation_required"] is True


def test_default_execution_mode_is_autonomous():
    items = [{"id": "a", "status": "pending"}]
    state = continuation_gate.compute_continuation_state(items)
    assert state["execution_mode"] == "autonomous"
    assert state["continuation_required"] is True


def test_mixed_batch_only_unblocked_items_count():
    items = [
        {"id": "a", "status": "done"},
        {"id": "b", "status": "pending", "blocker_kind": "manual"},
        {"id": "c", "status": "pending"},
    ]
    state = continuation_gate.compute_continuation_state(items, execution_mode="autonomous")
    assert state["continuation_required"] is True
    assert state["actionable_item_ids"] == ["c"]
    assert state["blocked_item_ids"] == ["b"]


def test_non_dict_items_are_skipped_defensively():
    state = continuation_gate.compute_continuation_state(
        [None, "not-a-dict", {"id": "a", "status": "pending"}],  # type: ignore[list-item]
        execution_mode="autonomous",
    )
    assert state["actionable_count"] == 1


# ---------------------------------------------------------------------------
# generate_handoff (full mode): strict_continuation / checkpoint gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_handoff_strict_continuation_blocks_on_remaining_pending(
    db, tmp_path,
):
    p = await db_module.create_project(db, "continuation-strict-blocks")
    await db_module.add_sprint_item(db, p["id"], "v1", "Some pending item")

    out_dir = tmp_path / "cg-out"
    out_dir.mkdir()
    with pytest.raises(handoff_module.HandoffContinuationRequired) as excinfo:
        await handoff_module.generate_handoff(
            db, p["id"], str(out_dir), skip_ai_summary=True, mode="full",
            strict_continuation=True,
        )
    state = excinfo.value.continuation_state
    assert state["continuation_required"] is True
    assert state["actionable_pending_count"] == 1
    # Nothing was written for this refused call.
    assert list(out_dir.iterdir()) == []
    pending = await db_module.get_pending_goal(db, p["id"])
    assert pending is None


@pytest.mark.asyncio
async def test_generate_handoff_strict_continuation_blocks_on_in_progress(db, tmp_path):
    p = await db_module.create_project(db, "continuation-strict-in-progress")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Claimed item")
    await db_module.claim_sprint_item(db, p["id"], item["id"])

    out_dir = tmp_path / "cg-out2"
    out_dir.mkdir()
    with pytest.raises(handoff_module.HandoffContinuationRequired) as excinfo:
        await handoff_module.generate_handoff(
            db, p["id"], str(out_dir), skip_ai_summary=True, mode="full",
            strict_continuation=True,
        )
    assert excinfo.value.continuation_state["actionable_in_progress_count"] == 1


@pytest.mark.asyncio
async def test_generate_handoff_strict_continuation_passes_when_all_done(db, tmp_path):
    p = await db_module.create_project(db, "continuation-strict-clean")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Item to finish")
    await db_module.claim_sprint_item(db, p["id"], item["id"])
    await db_module.complete_sprint_item(db, p["id"], item["id"])

    out_dir = tmp_path / "cg-out3"
    out_dir.mkdir()
    path, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(out_dir), skip_ai_summary=True, mode="full",
        strict_continuation=True,
    )
    assert path  # did not raise; file was written
    assert list(out_dir.iterdir())


@pytest.mark.asyncio
async def test_generate_handoff_checkpoint_bypasses_strict_continuation(db, tmp_path):
    """checkpoint=True marks the call as a mid-run progress report — it must
    never be refused by strict_continuation regardless of remaining work."""
    p = await db_module.create_project(db, "continuation-checkpoint-bypass")
    await db_module.add_sprint_item(db, p["id"], "v1", "Some pending item")

    out_dir = tmp_path / "cg-out4"
    out_dir.mkdir()
    path, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(out_dir), skip_ai_summary=True, mode="full",
        strict_continuation=True, checkpoint=True,
    )
    assert path
    assert list(out_dir.iterdir())


@pytest.mark.asyncio
async def test_generate_handoff_genuine_blocker_escapes_strict_continuation(db, tmp_path):
    p = await db_module.create_project(db, "continuation-blocker-escape")
    await db_module.add_sprint_item(
        db, p["id"], "v1", "Blocked item", blocker_kind="manual",
    )

    out_dir = tmp_path / "cg-out5"
    out_dir.mkdir()
    path, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(out_dir), skip_ai_summary=True, mode="full",
        strict_continuation=True,
    )
    assert path
    assert list(out_dir.iterdir())


@pytest.mark.asyncio
async def test_generate_handoff_interactive_mode_escapes_strict_continuation(db, tmp_path):
    p = await db_module.create_project(
        db, "continuation-interactive-escape", execution_mode="interactive",
    )
    await db_module.add_sprint_item(db, p["id"], "v1", "Some pending item")

    out_dir = tmp_path / "cg-out6"
    out_dir.mkdir()
    path, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(out_dir), skip_ai_summary=True, mode="full",
        strict_continuation=True,
    )
    assert path
    assert list(out_dir.iterdir())


@pytest.mark.asyncio
async def test_generate_handoff_continuation_status_populated_without_strict(db, tmp_path):
    """continuation_status is always populated for full mode, regardless of
    strict_continuation — a caller can read it without opting into refusal."""
    p = await db_module.create_project(db, "continuation-status-always")
    await db_module.add_sprint_item(db, p["id"], "v1", "Some pending item")

    out_dir = tmp_path / "cg-out7"
    out_dir.mkdir()
    continuation_status: dict = {}
    path, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(out_dir), skip_ai_summary=True, mode="full",
        continuation_status=continuation_status,
    )
    assert path
    assert continuation_status["continuation_required"] is True
    assert continuation_status["actionable_pending_count"] == 1


@pytest.mark.asyncio
async def test_generate_handoff_strict_continuation_off_by_default(db, tmp_path):
    """A caller that never passes strict_continuation sees zero behaviour
    change — the handoff is rendered exactly as before this feature existed."""
    p = await db_module.create_project(db, "continuation-default-off")
    await db_module.add_sprint_item(db, p["id"], "v1", "Some pending item")

    out_dir = tmp_path / "cg-out8"
    out_dir.mkdir()
    path, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(out_dir), skip_ai_summary=True, mode="full",
    )
    assert path
    assert list(out_dir.iterdir())


@pytest.mark.asyncio
async def test_generate_handoff_mcp_dispatch_strict_continuation_blocked_response(db, tmp_path):
    """The MCP dispatch layer (handler.py) surfaces HandoffContinuationRequired
    as a structured HANDOFF_CONTINUATION_BLOCKED response, mirroring the
    strict_evidence/HANDOFF_EVIDENCE_BLOCKED contract exactly."""
    import meridian.server as srv

    p = await db_module.create_project(db, "continuation-mcp-dispatch")
    await db_module.add_sprint_item(db, p["id"], "v1", "Some pending item")

    res = await srv._dispatch_mcp_tool(
        "generate_handoff",
        {
            "project_id": p["id"], "mode": "full", "strict_continuation": True,
        },
        db, str(tmp_path),
    )
    assert res["error"] == "HANDOFF_CONTINUATION_BLOCKED"
    assert res["continuation_status"]["continuation_required"] is True


@pytest.mark.asyncio
async def test_generate_handoff_mcp_dispatch_continuation_status_on_success(db, tmp_path):
    import meridian.server as srv

    p = await db_module.create_project(db, "continuation-mcp-success")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Item to finish")
    await db_module.claim_sprint_item(db, p["id"], item["id"])
    await db_module.complete_sprint_item(db, p["id"], item["id"])

    res = await srv._dispatch_mcp_tool(
        "generate_handoff",
        {"project_id": p["id"], "mode": "full"},
        db, str(tmp_path),
    )
    assert "error" not in res
    assert res["continuation_status"]["terminal_ready"] is True
    assert res["checkpoint"] is False
    assert res["strict_continuation"] is False


# ---------------------------------------------------------------------------
# get_sprint_progress: machine-readable `continuation` field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_sprint_progress_reports_continuation_required(db):
    import meridian.server as srv

    p = await db_module.create_project(db, "progress-continuation-required")
    await db_module.add_sprint_item(db, p["id"], "v1", "pending task")

    res = await srv._dispatch_mcp_tool(
        "get_sprint_progress", {"project_id": p["id"]}, db, "/tmp"
    )
    assert res["continuation"]["continuation_required"] is True
    assert res["continuation"]["terminal_ready"] is False


@pytest.mark.asyncio
async def test_get_sprint_progress_reports_terminal_ready_when_all_done(db):
    import meridian.server as srv

    p = await db_module.create_project(db, "progress-terminal-ready")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "done task")
    await db_module.claim_sprint_item(db, p["id"], item["id"])
    await db_module.complete_sprint_item(db, p["id"], item["id"])

    res = await srv._dispatch_mcp_tool(
        "get_sprint_progress", {"project_id": p["id"]}, db, "/tmp"
    )
    assert res["continuation"]["terminal_ready"] is True
    assert res["continuation"]["continuation_required"] is False


# ---------------------------------------------------------------------------
# complete_sprint_item: advisory `continuation` field on the returned item
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_sprint_item_reports_continuation_required_when_siblings_remain(db):
    """The reward-hacking regression: completing ONE item out of a batch
    must never report terminal_ready=True while a sibling remains actionable."""
    p = await db_module.create_project(db, "complete-item-continuation")
    item1 = await db_module.add_sprint_item(db, p["id"], "v1", "first item")
    await db_module.add_sprint_item(db, p["id"], "v1", "second item — still pending")
    await db_module.claim_sprint_item(db, p["id"], item1["id"])

    result = await db_module.complete_sprint_item(db, p["id"], item1["id"])
    assert result["continuation"]["continuation_required"] is True
    assert result["continuation"]["terminal_ready"] is False


@pytest.mark.asyncio
async def test_complete_sprint_item_reports_terminal_ready_when_last_item(db):
    p = await db_module.create_project(db, "complete-item-terminal")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "only item")
    await db_module.claim_sprint_item(db, p["id"], item["id"])

    result = await db_module.complete_sprint_item(db, p["id"], item["id"])
    assert result["continuation"]["terminal_ready"] is True
    assert result["continuation"]["continuation_required"] is False


@pytest.mark.asyncio
async def test_stdio_generate_handoff_schema_exposes_continuation_args(db, monkeypatch):
    """Arg-parity coverage for the stdio transport, mirroring
    test_stdio_handoff_arg_parity.py's existing schema-exposure test for the
    strict_evidence/force_include_ids gap this same test module documents."""
    import mcp.types as mcp_types
    import meridian.server as server_module

    async def _return_db(*_a, **_k):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    server, _run_stdio = server_module.build_mcp_server()
    list_handler = server.request_handlers[mcp_types.ListToolsRequest]
    listed = await list_handler(mcp_types.ListToolsRequest())
    tool = next(t for t in listed.root.tools if t.name == "generate_handoff")
    props = tool.inputSchema["properties"]

    assert "checkpoint" in props
    assert props["checkpoint"]["type"] == "boolean"
    assert "strict_continuation" in props
    assert props["strict_continuation"]["type"] == "boolean"


@pytest.mark.asyncio
async def test_stdio_generate_handoff_strict_continuation_returns_structured_error(
    db, monkeypatch,
):
    """strict_continuation threads through the stdio dispatch branch and
    returns the same structured HANDOFF_CONTINUATION_BLOCKED shape as the
    HTTP MCP dispatch."""
    import mcp.types as mcp_types
    import meridian.server as server_module

    project = await db_module.create_project(db, "stdio-strict-continuation")
    await db_module.add_sprint_item(db, project["id"], "v1", "pending item")

    async def _return_db(*_a, **_k):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    server, _run_stdio = server_module.build_mcp_server()

    call_handler = server.request_handlers[mcp_types.CallToolRequest]
    called = await call_handler(
        mcp_types.CallToolRequest(
            params=mcp_types.CallToolRequestParams(
                name="generate_handoff",
                arguments={
                    "project_id": project["id"],
                    "mode": "full",
                    "strict_continuation": True,
                },
            )
        )
    )
    import json as _json

    result = _json.loads(called.root.content[0].text)
    assert result["error"] == "HANDOFF_CONTINUATION_BLOCKED"
    assert result["project_id"] == project["id"]
    assert result["continuation_status"]["continuation_required"] is True
    assert "content" not in result
    assert "path" not in result


@pytest.mark.asyncio
async def test_complete_sprint_item_continuation_scoped_to_own_version(db):
    """A sibling item in a DIFFERENT version bucket must not force
    continuation_required on a session scoped to the completed item's own
    version."""
    p = await db_module.create_project(db, "complete-item-version-scope")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "v1 item")
    await db_module.add_sprint_item(db, p["id"], "v2", "unrelated v2 item")
    await db_module.claim_sprint_item(db, p["id"], item["id"])

    result = await db_module.complete_sprint_item(db, p["id"], item["id"])
    assert result["continuation"]["terminal_ready"] is True


# ---------------------------------------------------------------------------
# REST route (POST /projects/{id}/handoff): continuation_status + 422
# ---------------------------------------------------------------------------


def test_post_handoff_endpoint_strict_continuation_returns_structured_422(client):
    """strict_continuation=True with actionable work remaining returns a
    structured 422 (HANDOFF_CONTINUATION_BLOCKED), mirroring the existing
    strict_evidence/HANDOFF_EVIDENCE_BLOCKED contract exactly."""
    project = client.post("/projects", json={"name": "http-strict-continuation"}).json()
    pid = project["id"]
    client.post(
        f"/projects/{pid}/sprint-items", json={"version": "v1", "title": "pending item"},
    )

    r = client.post(f"/projects/{pid}/handoff", json={"strict_continuation": True})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "HANDOFF_CONTINUATION_BLOCKED"
    assert detail["project_id"] == pid
    assert detail["continuation_status"]["continuation_required"] is True


def test_post_handoff_endpoint_continuation_status_always_returned(client):
    project = client.post("/projects", json={"name": "http-continuation-status"}).json()
    pid = project["id"]
    client.post(
        f"/projects/{pid}/sprint-items", json={"version": "v1", "title": "pending item"},
    )

    r = client.post(f"/projects/{pid}/handoff", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["continuation_status"]["continuation_required"] is True


def test_post_handoff_endpoint_checkpoint_bypasses_strict_continuation(client):
    project = client.post("/projects", json={"name": "http-continuation-checkpoint"}).json()
    pid = project["id"]
    client.post(
        f"/projects/{pid}/sprint-items", json={"version": "v1", "title": "pending item"},
    )

    r = client.post(
        f"/projects/{pid}/handoff",
        json={"strict_continuation": True, "checkpoint": True},
    )
    assert r.status_code == 200
