"""Tests for sprint item 84f77597 — expose a tenant-safe
``move_workspace_note_to_project`` MCP tool.

Covers the MCP surface only (schema registration, HTTP/MCP dispatch, stdio
transport parity + real dispatch, and the destination-side defense-in-depth
via the generic ``scoped_project_ids`` gate). Db-layer tenant-safety,
race/compensation, and failure-path-atomicity coverage for
``db_module.move_workspace_note_to_project`` itself lives alongside the
existing ``test_workspace_note_move_to_project`` in tests/test_core.py.

Destination-side tenant ownership note: ``projects`` has no ``tenant_id``
column reachable from this connection (see
``db_module.get_tenant_id_for_project``'s docstring — it is documented to
degrade to ``None`` on hosted Neon), so the db layer cannot enforce
destination ownership directly. Per this item's discovery brief, real
enforcement for hosted multi-tenant callers is the existing generic
project-scope gate in ``mcp/handler.py`` (``scoped_project_ids`` /
95499c3e / decision 6fe5210c) — this file proves that gate actually covers
the new tool, mirroring ``test_handler_coverage_0882b8d6.py``'s coverage of
the same gate for other tools.
"""
from __future__ import annotations

import json

import pytest

import meridian.server as srv
from meridian import db as db_module
from meridian.mcp import handler as mh
from meridian.mcp_tools import _MCP_TOOLS_LIST, _READ_ONLY_TOOLS, _TOOL_WORKFLOW_TIER

TOOL = "move_workspace_note_to_project"


def _tool() -> dict:
    return next(t for t in _MCP_TOOLS_LIST if t["name"] == TOOL)


def _req(method, params=None, req_id=1):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


# ---------------------------------------------------------------------------
# 1. Canonical schema (_MCP_TOOLS_LIST) checks
# ---------------------------------------------------------------------------


def test_tool_registered_exactly_once():
    names = [t["name"] for t in _MCP_TOOLS_LIST if t["name"] == TOOL]
    assert names == [TOOL]


def test_schema_advertises_note_id_and_project_alternatives():
    tool = _tool()
    props = tool["inputSchema"]["properties"]
    assert "note_id" in props
    assert props["note_id"]["type"] == "string"
    assert "project_id" in props
    assert props["project_id"]["type"] == "string"
    assert "project_name" in props
    assert props["project_name"]["type"] == "string"
    assert "alternative to project_id" in props["project_name"]["description"]
    required = tool["inputSchema"].get("required") or []
    # note_id is the one truly required field; project_id/project_name are
    # alternatives enforced at the handler, not the schema.
    assert required == ["note_id"]
    assert "project_id" not in required
    assert "project_name" not in required


def test_tool_is_mutating_not_read_only():
    """It hard-removes the source workspace note as a side effect — never
    read-only, matching every other workspace write tool."""
    assert TOOL not in _READ_ONLY_TOOLS
    tool = _tool()
    assert tool["annotations"]["readOnlyHint"] is False
    assert tool["annotations"]["idempotentHint"] is False


def test_tool_category_and_workflow_tier():
    tool = _tool()
    assert tool["category"] == "workspace"
    assert tool["role_relevance"] == "both"
    assert _TOOL_WORKFLOW_TIER[TOOL] == "maintenance-only"
    assert tool["workflow_tier"] == "maintenance-only"


# ---------------------------------------------------------------------------
# 2. HTTP/MCP dispatch — routes through _handle_notes_decisions to the real
#    db_module function (mirrors test_ac4df52f_notes_decisions_dispatch.py).
# ---------------------------------------------------------------------------


async def test_dispatch_moves_note(db):
    p = await db_module.create_project(db, "84f77597-dispatch-target")
    note = await db_module.add_workspace_note(db, "shared note", "body text", "tag1")

    result = await srv._dispatch_mcp_tool(
        TOOL, {"note_id": note["id"], "project_id": p["id"]}, db, "/tmp",
    )

    assert "error" not in result
    assert result["project_id"] == p["id"]
    assert result["title"] == "shared note"
    assert await db_module.get_workspace_notes(db) == []


async def test_dispatch_resolves_project_name(db):
    p = await db_module.create_project(db, "84f77597-by-name")
    note = await db_module.add_workspace_note(db, "by-name note", "body")

    result = await srv._dispatch_mcp_tool(
        TOOL, {"note_id": note["id"], "project_name": "84f77597-by-name"}, db, "/tmp",
    )

    assert "error" not in result
    assert result["project_id"] == p["id"]


async def test_dispatch_missing_note_id_returns_error(db):
    p = await db_module.create_project(db, "84f77597-missing-note-id")
    result = await srv._dispatch_mcp_tool(
        TOOL, {"project_id": p["id"]}, db, "/tmp",
    )
    assert result == {"error": "note_id is required"}


async def test_dispatch_missing_project_returns_error(db):
    note = await db_module.add_workspace_note(db, "orphan", "body")
    result = await srv._dispatch_mcp_tool(
        TOOL, {"note_id": note["id"]}, db, "/tmp",
    )
    assert result == {"error": "project_id (or project_name) is required"}
    # Nothing was touched.
    assert {n["title"] for n in await db_module.get_workspace_notes(db)} == {"orphan"}


async def test_dispatch_unknown_project_returns_error_note_preserved(db):
    note = await db_module.add_workspace_note(db, "keep-me-dispatch", "body")
    result = await srv._dispatch_mcp_tool(
        TOOL, {"note_id": note["id"], "project_id": "no-such-project"}, db, "/tmp",
    )
    assert "error" in result
    assert {n["title"] for n in await db_module.get_workspace_notes(db)} == {"keep-me-dispatch"}


async def test_direct_handler_respects_tenant_scope(db):
    """Handler forwards _mcp_tenant_id straight to the db layer — tenant B
    cannot move tenant A's workspace note even by guessing its id."""
    from meridian.mcp.handlers import notes_decisions as nd_mod

    p = await db_module.create_project(db, "84f77597-tenant-dest")
    note = await db_module.add_workspace_note(db, "A-only", "body", tenant_id="tenant-a")

    result = await nd_mod.handle_move_workspace_note_to_project(
        {"note_id": note["id"], "project_id": p["id"]},
        db, "/tmp", None, "tenant-b",
    )
    assert "error" in result
    # Source note untouched — still visible under tenant A.
    a_titles = {n["title"] for n in await db_module.get_workspace_notes(db, tenant_id="tenant-a")}
    assert "A-only" in a_titles


# ---------------------------------------------------------------------------
# 3. Destination-side defense-in-depth: the generic scoped_project_ids gate
#    (mirrors test_handle_request_scoped_project_ids_denies_out_of_scope in
#    tests/test_handler_coverage_0882b8d6.py).
# ---------------------------------------------------------------------------


async def test_scoped_project_ids_denies_out_of_scope_destination(db):
    """A caller scoped to a different project set cannot move a workspace
    note onto a project outside their scope — the same defense-in-depth
    every other project_id-named write tool gets automatically."""
    p = await db_module.create_project(db, "84f77597-out-of-scope-dest")
    note = await db_module.add_workspace_note(db, "scope-test", "body")

    resp = await mh._handle_mcp_request(
        _req("tools/call", {
            "name": TOOL,
            "arguments": {"note_id": note["id"], "project_id": p["id"]},
        }),
        db=db, data_dir="/tmp",
        scoped_project_ids=["ffffffff-ffff-ffff-ffff-000000000000"],  # not p["id"]
    )
    assert resp["error"]["code"] == -32603
    assert "access scope" in resp["error"]["message"]
    # Nothing was moved.
    assert {n["title"] for n in await db_module.get_workspace_notes(db)} == {"scope-test"}


async def test_scoped_project_ids_allows_in_scope_destination(db):
    p = await db_module.create_project(db, "84f77597-in-scope-dest")
    note = await db_module.add_workspace_note(db, "scope-ok", "body")

    resp = await mh._handle_mcp_request(
        _req("tools/call", {
            "name": TOOL,
            "arguments": {"note_id": note["id"], "project_id": p["id"]},
        }),
        db=db, data_dir="/tmp",
        scoped_project_ids=[p["id"]],
    )
    assert "error" not in resp
    # tools/call wraps the tool's return value as MCP content: a JSON string
    # inside result.content[0].text (see handler.py's json.dumps(result) call).
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert "error" not in payload
    assert payload["project_id"] == p["id"]


# ---------------------------------------------------------------------------
# 4. stdio transport parity + real dispatch (mirrors
#    tests/test_dffcde86_worktree_mcp_exposure.py section 3).
# ---------------------------------------------------------------------------


def _build_stdio_server(monkeypatch, db):
    async def _return_db(*_a, **_k):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    server, _run_stdio = srv.build_mcp_server()
    return server


async def _stdio_list_tools(server):
    import mcp.types as mcp_types

    list_handler = server.request_handlers[mcp_types.ListToolsRequest]
    listed = await list_handler(mcp_types.ListToolsRequest())
    return listed.root.tools


async def _stdio_call(server, name, arguments):
    import mcp.types as mcp_types

    call_handler = server.request_handlers[mcp_types.CallToolRequest]
    called = await call_handler(
        mcp_types.CallToolRequest(
            params=mcp_types.CallToolRequestParams(name=name, arguments=arguments)
        )
    )
    return json.loads(called.root.content[0].text)


async def test_stdio_schema_matches_canonical_schema(db, monkeypatch):
    server = _build_stdio_server(monkeypatch, db)
    tools = await _stdio_list_tools(server)
    stdio_tool = next(t for t in tools if t.name == TOOL)

    canonical = _tool()
    assert stdio_tool.inputSchema == canonical["inputSchema"]
    assert stdio_tool.description == canonical["description"]


async def test_stdio_call_moves_real_note(db, monkeypatch):
    server = _build_stdio_server(monkeypatch, db)
    project = await db_module.create_project(db, "84f77597-stdio-move")
    note = await db_module.add_workspace_note(db, "stdio note", "body")

    result = await _stdio_call(
        server, TOOL, {"note_id": note["id"], "project_id": project["id"]},
    )

    assert "error" not in result
    assert result["project_id"] == project["id"]
    assert await db_module.get_workspace_notes(db) == []
