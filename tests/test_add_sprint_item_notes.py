"""Regression coverage for sprint-item notes supplied at creation time."""

from __future__ import annotations

import pytest

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian import db as db_module
from meridian.mcp.handler import _dispatch_mcp_tool
from meridian.mcp_tools import _MCP_TOOLS_LIST


@pytest.mark.asyncio
async def test_add_sprint_item_persists_notes_at_creation(db):
    project = await db_module.create_project(db, "notes-db-regression")

    item = await db_module.add_sprint_item(
        db,
        project["id"],
        "v1",
        "Persist creation notes",
        notes="Acceptance context recorded up front.",
    )

    assert item["notes"] == "Acceptance context recorded up front."
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored is not None
    assert stored["notes"] == "Acceptance context recorded up front."


@pytest.mark.asyncio
async def test_mcp_add_sprint_item_forwards_notes(db, monkeypatch):
    import json

    import mcp.types as mcp_types
    import meridian.server as server_module

    project = await db_module.create_project(db, "notes-mcp-regression")

    result = await _dispatch_mcp_tool(
        "add_sprint_item",
        {
            "project_id": project["id"],
            "version": "v1",
            "title": "Forward creation notes",
            "notes": "Context passed through the MCP handler.",
            "force": True,
        },
        db,
        "/tmp",
        tenant=None,
    )

    assert result["notes"] == "Context passed through the MCP handler."
    stored = await db_module.get_sprint_item(db, result["id"])
    assert stored is not None
    assert stored["notes"] == "Context passed through the MCP handler."

    async def _return_db(*_args, **_kwargs):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    stdio_server, _run_stdio = server_module.build_mcp_server()

    list_handler = stdio_server.request_handlers[mcp_types.ListToolsRequest]
    listed = await list_handler(mcp_types.ListToolsRequest())
    add_tool = next(tool for tool in listed.root.tools if tool.name == "add_sprint_item")
    assert add_tool.inputSchema["properties"]["notes"]["type"] == "string"

    call_handler = stdio_server.request_handlers[mcp_types.CallToolRequest]
    called = await call_handler(
        mcp_types.CallToolRequest(
            params=mcp_types.CallToolRequestParams(
                name="add_sprint_item",
                arguments={
                    "project_id": project["id"],
                    "version": "v1",
                    "title": "Forward stdio creation notes",
                    "notes": "Context passed through the stdio transport.",
                    "force": True,
                },
            )
        )
    )
    stdio_result = json.loads(called.root.content[0].text)
    assert stdio_result["notes"] == "Context passed through the stdio transport."


def test_add_sprint_item_schema_exposes_notes():
    tool = next(tool for tool in _MCP_TOOLS_LIST if tool["name"] == "add_sprint_item")

    notes_schema = tool["inputSchema"]["properties"]["notes"]
    assert notes_schema["type"] == "string"
