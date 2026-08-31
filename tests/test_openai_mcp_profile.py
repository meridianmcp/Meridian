"""Contract tests for the curated OpenAI MCP profile."""
from __future__ import annotations

import asyncio

import pytest

# Import the public server module first: it completes the existing
# server<->handler bootstrap before the internal handler helper is imported.
from meridian import server as _server
from meridian.mcp.handler import _handle_mcp_request
from meridian.mcp_profiles import OPENAI_PUBLIC_TOOL_NAMES, get_tool_allowlist
from meridian.mcp_tools import _MCP_TOOLS_LIST


def test_openai_profile_is_known_and_subset_of_native_tools() -> None:
    native_names = {tool["name"] for tool in _MCP_TOOLS_LIST}
    assert get_tool_allowlist("openai") == OPENAI_PUBLIC_TOOL_NAMES
    assert OPENAI_PUBLIC_TOOL_NAMES <= native_names
    assert len(OPENAI_PUBLIC_TOOL_NAMES) <= 30


def test_unknown_profile_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown MCP tool profile"):
        get_tool_allowlist("does-not-exist")


@pytest.mark.asyncio
async def test_tools_list_filters_to_profile_and_recomputes_manifest(tmp_path) -> None:
    response = await _handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        None,
        str(tmp_path),
        allowed_tool_names=OPENAI_PUBLIC_TOOL_NAMES,
    )
    tools = response["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == OPENAI_PUBLIC_TOOL_NAMES
    assert response["result"]["_meta"]["meridian/toolManifest"]["count"] == len(names)


@pytest.mark.asyncio
async def test_tool_call_outside_profile_is_rejected_before_dispatch(tmp_path) -> None:
    response = await _handle_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_server_logs", "arguments": {}},
        },
        None,
        str(tmp_path),
        allowed_tool_names=OPENAI_PUBLIC_TOOL_NAMES,
    )
    assert response["error"]["code"] == -32601
    assert "not available on this MCP profile" in response["error"]["message"]


def test_curated_route_is_registered() -> None:
    routes = {getattr(route, "path", None) for route in _server.app.routes}
    assert "/mcp/openai" in routes


def test_curated_route_filters_authenticated_tools(client) -> None:
    from meridian import db as db_module

    async def _setup() -> str:
        tenant = await db_module.upsert_tenant(client.app.state.db, "openai-profile@example.com")
        raw_token, _ = await db_module.create_api_token(client.app.state.db, tenant["id"])
        return raw_token

    raw_token = asyncio.run(_setup())
    response = client.post(
        "/mcp/openai",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert {tool["name"] for tool in result["tools"]} == OPENAI_PUBLIC_TOOL_NAMES

    excluded = client.post(
        "/mcp/openai",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "get_server_logs", "arguments": {}},
        },
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert excluded.status_code == 200
    assert excluded.json()["error"]["code"] == -32601
