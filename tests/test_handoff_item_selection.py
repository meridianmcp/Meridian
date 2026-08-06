"""94f48e4d — selected_item_ids: scope a handoff to exactly the requested
items' dependency closure, fail-closed on any invalid id.

Root-cause note: the sprint item's own premise ("the core generate_handoff
function already accepts selected_item_ids") was FALSE at the time this item
was picked up — grep across this worktree and origin/dev found zero existing
references anywhere. This file (and the meridian/handoff.py /
meridian/mcp/handler.py / meridian/routes/handoff.py changes it covers)
implements the feature from scratch, then wires it through every transport,
rather than merely "forwarding" a pre-existing parameter.

Covers:
  (a) core generate_handoff: selection restricts the pending list to exactly
      the requested items.
  (b) dependency closure: a still-pending depends_on ancestor is pulled in
      automatically even when not explicitly selected.
  (c) an already-done/in_progress ancestor is NOT pulled in (nothing left to
      do there) and does not itself cause a rejection.
  (d) fail-closed rejection for each reason: not_found, wrong_project,
      wrong_version, and not_pending (done/skipped/in_progress).
  (e) selected_item_ids is a pure no-op when omitted/empty — zero behavior
      change for every existing caller.
  (f) MCP handler.py transport parity (mirrors the pattern in
      tests/test_stdio_handoff_arg_parity.py for force_include_ids/version).
  (g) HTTP routes/handoff.py transport parity, including the 422
      HANDOFF_SELECTION_BLOCKED structured-error shape.
  (h) stdio transport parity (schema exposure + dispatch forwarding +
      structured refusal), mirroring tests/test_stdio_handoff_arg_parity.py's
      own coverage of force_include_ids/version/strict_evidence.
  (i) delta mode's <continuation_manifest> pending_item_ids stays consistent
      with the scoped selection instead of leaking the full board (a real
      gap found while building this: build_continuation_manifest queries the
      board independently of generate_handoff's own pending_sprint_items).
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module
import meridian.server as srv  # noqa: F401 — load the server before handler to avoid its import cycle


# ---------------------------------------------------------------------------
# Core generate_handoff behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_selected_item_ids_restricts_to_exactly_the_requested_items(db, tmp_path):
    project = await db_module.create_project(db, "select-basic")
    keep = await db_module.add_sprint_item(db, project["id"], "v1", "keep this item")
    drop = await db_module.add_sprint_item(
        db, project["id"], "v1", "excluded backlog entry", force=True,
    )

    _, content, _ = await handoff_module.generate_handoff(
        db, project["id"], str(tmp_path), skip_ai_summary=True, mode="full",
        selected_item_ids=[keep["id"]],
    )

    assert keep["id"][:8] in content
    assert "keep this item" in content
    assert drop["id"][:8] not in content
    assert "excluded backlog entry" not in content


@pytest.mark.asyncio
async def test_selected_item_ids_none_is_a_pure_noop(db, tmp_path):
    project = await db_module.create_project(db, "select-noop")
    a = await db_module.add_sprint_item(db, project["id"], "v1", "item a")
    b = await db_module.add_sprint_item(db, project["id"], "v1", "item b")

    _, content, _ = await handoff_module.generate_handoff(
        db, project["id"], str(tmp_path), skip_ai_summary=True, mode="full",
    )

    assert a["id"][:8] in content
    assert b["id"][:8] in content


@pytest.mark.asyncio
async def test_selected_item_ids_dependency_closure_pulls_in_pending_ancestor(db, tmp_path):
    project = await db_module.create_project(db, "select-closure")
    parent = await db_module.add_sprint_item(db, project["id"], "v1", "prerequisite parent")
    child = await db_module.add_sprint_item(
        db, project["id"], "v1", "dependent child", depends_on=parent["id"],
    )
    unrelated = await db_module.add_sprint_item(db, project["id"], "v1", "unrelated item")

    _, content, _ = await handoff_module.generate_handoff(
        db, project["id"], str(tmp_path), skip_ai_summary=True, mode="full",
        selected_item_ids=[child["id"]],
    )

    assert child["id"][:8] in content
    assert "dependent child" in content
    # the still-pending prerequisite is pulled in automatically, unrequested
    assert parent["id"][:8] in content
    assert "prerequisite parent" in content
    assert unrelated["id"][:8] not in content


@pytest.mark.asyncio
async def test_selected_item_ids_closure_skips_already_done_ancestor(db, tmp_path):
    project = await db_module.create_project(db, "select-closure-done")
    parent = await db_module.add_sprint_item(db, project["id"], "v1", "already done parent")
    await db_module.complete_sprint_item(db, project["id"], parent["id"])
    child = await db_module.add_sprint_item(
        db, project["id"], "v1", "child of done parent", depends_on=parent["id"],
    )

    _, content, _ = await handoff_module.generate_handoff(
        db, project["id"], str(tmp_path), skip_ai_summary=True, mode="full",
        selected_item_ids=[child["id"]],
    )

    assert child["id"][:8] in content
    # done ancestor is not re-surfaced as pending -- nothing left to do there
    assert "already done parent" not in content


@pytest.mark.asyncio
async def test_selected_item_ids_multi_hop_closure_walks_to_root(db, tmp_path):
    project = await db_module.create_project(db, "select-closure-multihop")
    grandparent = await db_module.add_sprint_item(db, project["id"], "v1", "root prerequisite")
    parent = await db_module.add_sprint_item(
        db, project["id"], "v1", "middle prerequisite", depends_on=grandparent["id"],
    )
    child = await db_module.add_sprint_item(
        db, project["id"], "v1", "leaf item", depends_on=parent["id"],
    )

    _, content, _ = await handoff_module.generate_handoff(
        db, project["id"], str(tmp_path), skip_ai_summary=True, mode="full",
        selected_item_ids=[child["id"]],
    )

    assert child["id"][:8] in content
    assert parent["id"][:8] in content
    assert grandparent["id"][:8] in content


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "delta", "goal"])
async def test_selected_item_ids_applies_across_full_delta_goal_modes(db, tmp_path, mode):
    project = await db_module.create_project(db, f"select-modes-{mode}")
    keep = await db_module.add_sprint_item(db, project["id"], "v1", "mode-scoped keep")
    drop = await db_module.add_sprint_item(
        db, project["id"], "v1", "excluded from this scope", force=True,
    )

    _, content, _ = await handoff_module.generate_handoff(
        db, project["id"], str(tmp_path), skip_ai_summary=True, mode=mode,
        selected_item_ids=[keep["id"]],
    )

    assert keep["id"][:8] in content
    assert drop["id"][:8] not in content


# ---------------------------------------------------------------------------
# Fail-closed rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_selected_item_ids_rejects_unknown_id(db, tmp_path):
    project = await db_module.create_project(db, "select-not-found")
    await db_module.add_sprint_item(db, project["id"], "v1", "an item")

    with pytest.raises(handoff_module.HandoffSelectionError) as excinfo:
        await handoff_module.generate_handoff(
            db, project["id"], str(tmp_path), skip_ai_summary=True, mode="full",
            selected_item_ids=["not-a-real-id"],
        )
    assert excinfo.value.rejected == [{"id": "not-a-real-id", "reason": "not_found"}]


@pytest.mark.asyncio
async def test_selected_item_ids_rejects_cross_project_id(db, tmp_path):
    project_a = await db_module.create_project(db, "select-cross-a")
    project_b = await db_module.create_project(db, "select-cross-b")
    foreign = await db_module.add_sprint_item(db, project_b["id"], "v1", "belongs to b")

    with pytest.raises(handoff_module.HandoffSelectionError) as excinfo:
        await handoff_module.generate_handoff(
            db, project_a["id"], str(tmp_path), skip_ai_summary=True, mode="full",
            selected_item_ids=[foreign["id"]],
        )
    assert excinfo.value.rejected == [{"id": foreign["id"], "reason": "wrong_project"}]


@pytest.mark.asyncio
async def test_selected_item_ids_rejects_wrong_version_when_scoped(db, tmp_path):
    project = await db_module.create_project(db, "select-wrong-version")
    other_version = await db_module.add_sprint_item(db, project["id"], "v2", "v2 item")

    with pytest.raises(handoff_module.HandoffSelectionError) as excinfo:
        await handoff_module.generate_handoff(
            db, project["id"], str(tmp_path), skip_ai_summary=True, mode="full",
            version="v1",
            selected_item_ids=[other_version["id"]],
        )
    rejected = excinfo.value.rejected
    assert rejected[0]["id"] == other_version["id"]
    assert rejected[0]["reason"] == "wrong_version"
    assert rejected[0]["item_version"] == "v2"
    assert rejected[0]["requested_version"] == "v1"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["done", "skipped", "in_progress"])
async def test_selected_item_ids_rejects_non_pending_statuses(db, tmp_path, status):
    project = await db_module.create_project(db, f"select-not-pending-{status}")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "not claimable")
    if status == "done":
        await db_module.complete_sprint_item(db, project["id"], item["id"])
    elif status == "skipped":
        await db_module.skip_sprint_item(db, project["id"], item["id"])
    else:
        assert status == "in_progress"
        await db_module.claim_sprint_item(db, project["id"], item["id"])

    with pytest.raises(handoff_module.HandoffSelectionError) as excinfo:
        await handoff_module.generate_handoff(
            db, project["id"], str(tmp_path), skip_ai_summary=True, mode="full",
            selected_item_ids=[item["id"]],
        )
    rejected = excinfo.value.rejected
    assert rejected[0]["id"] == item["id"]
    assert rejected[0]["reason"] == "not_pending"
    assert rejected[0]["status"] == status


@pytest.mark.asyncio
async def test_selected_item_ids_any_invalid_id_fails_the_whole_call_closed(db, tmp_path):
    """A mix of one valid + one invalid id must reject the ENTIRE call --
    never silently render a partial/narrower scope than what was requested."""
    project = await db_module.create_project(db, "select-partial-invalid")
    valid = await db_module.add_sprint_item(db, project["id"], "v1", "valid item")

    with pytest.raises(handoff_module.HandoffSelectionError):
        await handoff_module.generate_handoff(
            db, project["id"], str(tmp_path), skip_ai_summary=True, mode="full",
            selected_item_ids=[valid["id"], "bogus-id"],
        )

    # Nothing was persisted for the refused call -- a follow-up unrestricted
    # handoff still sees the valid item as ordinary pending work.
    _, content, _ = await handoff_module.generate_handoff(
        db, project["id"], str(tmp_path), skip_ai_summary=True, mode="full",
    )
    assert valid["id"][:8] in content


# ---------------------------------------------------------------------------
# MCP handler.py transport parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_handler_forwards_selected_item_ids(db, tmp_path):
    project = await db_module.create_project(db, "select-mcp-handler")
    keep = await db_module.add_sprint_item(db, project["id"], "v1", "handler keep")
    drop = await db_module.add_sprint_item(db, project["id"], "v1", "handler drop")

    result = await srv._dispatch_mcp_tool(
        "generate_handoff",
        {
            "project_id": project["id"],
            "mode": "full",
            "selected_item_ids": [keep["id"]],
        },
        db, str(tmp_path),
    )

    assert "error" not in result
    assert keep["id"][:8] in result["content"]
    assert drop["id"][:8] not in result["content"]


@pytest.mark.asyncio
async def test_mcp_handler_selected_item_ids_structured_refusal(db, tmp_path):
    project = await db_module.create_project(db, "select-mcp-handler-refused")
    await db_module.add_sprint_item(db, project["id"], "v1", "an item")

    result = await srv._dispatch_mcp_tool(
        "generate_handoff",
        {
            "project_id": project["id"],
            "mode": "full",
            "selected_item_ids": ["totally-bogus"],
        },
        db, str(tmp_path),
    )

    assert result["error"] == "HANDOFF_SELECTION_BLOCKED"
    assert result["project_id"] == project["id"]
    assert result["selection_rejected"] == [{"id": "totally-bogus", "reason": "not_found"}]
    # A refused call must not also carry the normal success shape.
    assert "content" not in result
    assert "file_path" not in result


# ---------------------------------------------------------------------------
# HTTP routes/handoff.py transport parity
# ---------------------------------------------------------------------------


def test_http_route_forwards_selected_item_ids(client):
    project = client.post("/projects", json={"name": "select-http"}).json()
    keep = client.post(
        f"/projects/{project['id']}/sprint-items",
        json={"version": "v1", "title": "http keep"},
    ).json()
    drop = client.post(
        f"/projects/{project['id']}/sprint-items",
        json={"version": "v1", "title": "http drop"},
    ).json()

    r = client.post(
        f"/projects/{project['id']}/handoff",
        json={"mode": "full", "selected_item_ids": [keep["id"]]},
    )
    assert r.status_code == 200
    body = r.json()
    assert keep["id"][:8] in body["content"]
    assert drop["id"][:8] not in body["content"]


def test_http_route_selected_item_ids_returns_422_structured_error(client):
    project = client.post("/projects", json={"name": "select-http-refused"}).json()

    r = client.post(
        f"/projects/{project['id']}/handoff",
        json={"mode": "full", "selected_item_ids": ["nope-not-real"]},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "HANDOFF_SELECTION_BLOCKED"
    assert detail["project_id"] == project["id"]
    assert detail["selection_rejected"] == [{"id": "nope-not-real", "reason": "not_found"}]


# ---------------------------------------------------------------------------
# stdio transport parity (mirrors tests/test_stdio_handoff_arg_parity.py)
# ---------------------------------------------------------------------------


def _build_stdio_server(monkeypatch, db):
    """Same pattern as tests/test_stdio_handoff_arg_parity.py's own helper."""
    import meridian.server as server_module

    async def _return_db(*_a, **_k):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    server, _run_stdio = server_module.build_mcp_server()
    return server


async def _call_generate_handoff(server, arguments):
    import json as _json
    import mcp.types as mcp_types

    call_handler = server.request_handlers[mcp_types.CallToolRequest]
    called = await call_handler(
        mcp_types.CallToolRequest(
            params=mcp_types.CallToolRequestParams(
                name="generate_handoff",
                arguments=arguments,
            )
        )
    )
    return _json.loads(called.root.content[0].text)


@pytest.mark.asyncio
async def test_stdio_generate_handoff_schema_exposes_selected_item_ids(db, monkeypatch):
    import mcp.types as mcp_types

    server = _build_stdio_server(monkeypatch, db)
    list_handler = server.request_handlers[mcp_types.ListToolsRequest]
    listed = await list_handler(mcp_types.ListToolsRequest())
    tool = next(t for t in listed.root.tools if t.name == "generate_handoff")
    props = tool.inputSchema["properties"]

    assert "selected_item_ids" in props
    assert props["selected_item_ids"]["type"] == "array"


@pytest.mark.asyncio
async def test_stdio_generate_handoff_forwards_selected_item_ids(db, monkeypatch):
    project = await db_module.create_project(db, "select-stdio")
    keep = await db_module.add_sprint_item(db, project["id"], "v1", "stdio keep")
    drop = await db_module.add_sprint_item(
        db, project["id"], "v1", "excluded stdio entry", force=True,
    )

    server = _build_stdio_server(monkeypatch, db)
    result = await _call_generate_handoff(
        server,
        {
            "project_id": project["id"],
            "mode": "full",
            "selected_item_ids": [keep["id"]],
        },
    )

    assert "error" not in result
    assert keep["id"][:8] in result["content"]
    assert drop["id"][:8] not in result["content"]


@pytest.mark.asyncio
async def test_stdio_generate_handoff_selected_item_ids_structured_refusal(db, monkeypatch):
    project = await db_module.create_project(db, "select-stdio-refused")
    await db_module.add_sprint_item(db, project["id"], "v1", "an item")

    server = _build_stdio_server(monkeypatch, db)
    result = await _call_generate_handoff(
        server,
        {
            "project_id": project["id"],
            "mode": "full",
            "selected_item_ids": ["not-real-either"],
        },
    )

    assert result["error"] == "HANDOFF_SELECTION_BLOCKED"
    assert result["project_id"] == project["id"]
    assert result["selection_rejected"] == [{"id": "not-real-either", "reason": "not_found"}]
    assert "content" not in result
    assert "path" not in result


# ---------------------------------------------------------------------------
# continuation_manifest scoping (delta mode) — real gap found while building
# this feature: build_continuation_manifest re-queries the board
# independently, so it needs its own restriction, not just pending_sprint_items.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delta_continuation_manifest_respects_selection(db, tmp_path):
    project = await db_module.create_project(db, "select-manifest-scope")
    keep = await db_module.add_sprint_item(db, project["id"], "v1", "manifest keep")
    drop = await db_module.add_sprint_item(
        db, project["id"], "v1", "excluded manifest entry", force=True,
    )

    _, content, _ = await handoff_module.generate_handoff(
        db, project["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        selected_item_ids=[keep["id"]],
    )

    import re
    match = re.search(r"<continuation_manifest>\s*(\{.*?\})\s*</continuation_manifest>", content, re.S)
    assert match is not None, "delta mode must always embed a continuation_manifest"
    import json as _json
    manifest = _json.loads(match.group(1))

    assert manifest["pending_item_ids"] == [keep["id"]]
    assert drop["id"] not in manifest["pending_item_ids"]


@pytest.mark.asyncio
async def test_delta_continuation_manifest_unrestricted_without_selection(db, tmp_path):
    """Sanity check: the fix above must not restrict the manifest when
    selected_item_ids is NOT given — item_count/pending_item_ids stay the
    canonical, full-board view for genuine staleness detection."""
    project = await db_module.create_project(db, "select-manifest-unrestricted")
    a = await db_module.add_sprint_item(db, project["id"], "v1", "manifest item a")
    b = await db_module.add_sprint_item(db, project["id"], "v1", "manifest item b", force=True)

    _, content, _ = await handoff_module.generate_handoff(
        db, project["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
    )

    import re
    match = re.search(r"<continuation_manifest>\s*(\{.*?\})\s*</continuation_manifest>", content, re.S)
    assert match is not None
    import json as _json
    manifest = _json.loads(match.group(1))

    assert set(manifest["pending_item_ids"]) == {a["id"], b["id"]}
