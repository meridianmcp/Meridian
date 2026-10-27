"""Schema parity for the seven Zotero bridge write tools across the MCP transports (the 325276f8 pattern).

The canonical schema is meridian/mcp_tools.py's _MCP_TOOLS_LIST. The HTTP transport advertises it through /tools and
the JSON-RPC tools/list. The stdio transport advertises it through _shared_tool(). A stdio call reaches the same
handler as an HTTP call. Nothing here reaches Zotero: a fake client stands in for it.
"""

from __future__ import annotations

import json

import pytest

import meridian.server  # noqa: F401 - import first to avoid the handler/server import cycle
from meridian import db as db_module
from meridian import zotero_bridge_transport as transport_mod
from meridian.mcp import handler as mh
from meridian.mcp_tools import _MCP_TOOLS_LIST

ZOTERO_TOOLS = (
    "zotero_create_collection",
    "zotero_move_collection",
    "zotero_move_item",
    "zotero_create_webpage",
    "zotero_attach_pdf",
    "zotero_add_child_note",
    "zotero_batch",
)


def _canonical(name: str) -> dict:
    return next(t for t in _MCP_TOOLS_LIST if t["name"] == name)


class FakeClient:
    """A stand-in for the process-wide client. Records calls; every write returns a fixed key."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def create_collection(self, name, parent_key=None):
        self.calls.append(("create_collection", (name, parent_key)))
        return "NEWCOLL01"


def _build_stdio_server(monkeypatch, db):
    """Builds the stdio MCP server with its lazy DB pinned to ``db``. Same pattern as the 325276f8 parity test."""
    import meridian.server as server_module

    async def _return_db(*_a, **_k):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    server, _run_stdio = server_module.build_mcp_server()
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


@pytest.fixture(autouse=True)
def _local_mode(monkeypatch):
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ZOTERO_TOOLS)
async def test_stdio_tools_list_advertises_the_canonical_schema(name, db, monkeypatch):
    server = _build_stdio_server(monkeypatch, db)
    tools = await _stdio_list_tools(server)
    tool = next(t for t in tools if t.name == name)

    canonical = _canonical(name)
    assert tool.description == canonical["description"]
    assert tool.inputSchema == canonical["inputSchema"]


@pytest.mark.parametrize("name", ZOTERO_TOOLS)
def test_http_tools_listing_advertises_the_canonical_schema(name, client):
    listed = {tool["name"]: tool for tool in client.get("/tools").json()}

    canonical = _canonical(name)
    assert listed[name]["description"] == canonical["description"]
    assert listed[name]["inputSchema"] == canonical["inputSchema"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ZOTERO_TOOLS)
async def test_stdio_and_http_advertise_identical_schemas(name, db, monkeypatch, client):
    server = _build_stdio_server(monkeypatch, db)
    stdio_tool = next(t for t in await _stdio_list_tools(server) if t.name == name)
    http_tool = next(t for t in client.get("/tools").json() if t["name"] == name)

    assert json.dumps(stdio_tool.inputSchema, sort_keys=True) == json.dumps(http_tool["inputSchema"], sort_keys=True)
    assert stdio_tool.description == http_tool["description"]


@pytest.mark.asyncio
async def test_a_stdio_call_reaches_the_zotero_handler(db, monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(transport_mod, "get_client", lambda: fake)
    server = _build_stdio_server(monkeypatch, db)

    result = await _stdio_call(server, "zotero_create_collection", {"name": "Reading list"})

    assert result == {"ok": True, "error": None, "collection_key": "NEWCOLL01", "status": "created"}
    assert fake.calls == [("create_collection", ("Reading list", None))]


@pytest.mark.asyncio
async def test_stdio_and_http_dispatch_to_the_same_handler(db, monkeypatch, tmp_path):
    fake = FakeClient()
    monkeypatch.setattr(transport_mod, "get_client", lambda: fake)
    server = _build_stdio_server(monkeypatch, db)

    via_stdio = await _stdio_call(server, "zotero_create_collection", {"name": "Reading list"})
    via_http = await mh._dispatch_mcp_tool(
        "zotero_create_collection", {"name": "Reading list"}, db, str(tmp_path)
    )

    assert via_stdio == via_http
    assert [call[0] for call in fake.calls] == ["create_collection", "create_collection"]
