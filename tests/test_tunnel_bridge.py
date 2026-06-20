"""Tests for the single-connector tunnel bridge (meridian/routes/tunnel.py).

The bridge surfaces a tenant's live fs/code/extractor tunnel tools through the
Meridian remote-MCP endpoint: `tools/list` aggregates them and `tools/call`
routes matching names back over the WebSocket relay. These tests exercise the
bridge helpers directly (with `_do_proxy` stubbed) and the handler integration
(with the bridge stubbed), so neither needs a real WebSocket.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.responses import Response

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian.routes import tunnel as tn
from meridian.mcp import handler as mh


@pytest.fixture(autouse=True)
def _clean_bridge_state():
    """Reset per-process tunnel registries between tests."""
    tn._tunnel_sockets.clear()
    tn._tunnel_code_sockets.clear()
    tn._tunnel_extract_sockets.clear()
    tn._tunnel_tool_routes.clear()
    yield
    tn._tunnel_sockets.clear()
    tn._tunnel_code_sockets.clear()
    tn._tunnel_extract_sockets.clear()
    tn._tunnel_tool_routes.clear()


# ---------------------------------------------------------------------------
# _parse_mcp_payload
# ---------------------------------------------------------------------------

def test_parse_mcp_payload_plain_json():
    raw = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}).encode()
    out = tn._parse_mcp_payload(raw)
    assert out["result"]["tools"] == []


def test_parse_mcp_payload_sse_framed():
    body = (
        "event: message\n"
        'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"trace_path"}]}}\n\n'
    ).encode()
    out = tn._parse_mcp_payload(body)
    assert out["result"]["tools"][0]["name"] == "trace_path"


def test_parse_mcp_payload_empty_and_garbage():
    assert tn._parse_mcp_payload(b"") is None
    assert tn._parse_mcp_payload(None) is None
    assert tn._parse_mcp_payload(b"not json at all") is None


# ---------------------------------------------------------------------------
# has_active_tunnel
# ---------------------------------------------------------------------------

def test_has_active_tunnel():
    assert tn.has_active_tunnel("t1") is False
    tn._tunnel_code_sockets["t1"] = object()
    assert tn.has_active_tunnel("t1") is True


# ---------------------------------------------------------------------------
# list_tunnel_tools / call_tunnel_tool
# ---------------------------------------------------------------------------

def _stub_proxy(monkeypatch, responder):
    """Patch tunnel._do_proxy with a responder(label, method, params) -> dict|Response."""
    async def fake_do_proxy(tenant_id, method, path, query, headers, body, sockets, pending, label):
        req = json.loads(body.decode())
        result = responder(label, req["method"], req.get("params") or {})
        if isinstance(result, Response):
            return result
        return Response(content=json.dumps(result).encode(), status_code=200,
                        media_type="application/json")
    monkeypatch.setattr(tn, "_do_proxy", fake_do_proxy)


def test_list_tunnel_tools_aggregates_and_reserves(monkeypatch):
    tn._tunnel_sockets["t1"] = object()
    tn._tunnel_code_sockets["t1"] = object()

    def responder(label, method, params):
        assert method == "tools/list"
        if label == "fs":
            return {"result": {"tools": [{"name": "read_file"}, {"name": "list_directory"}]}}
        if label == "code":
            return {"result": {"tools": [{"name": "trace_path"}]}}
        return {"result": {"tools": []}}

    _stub_proxy(monkeypatch, responder)
    # Reserve read_file (collides with GitHub) — it must be dropped.
    tools = asyncio.run(tn.list_tunnel_tools("t1", reserved_names={"read_file"}))
    names = {t["name"] for t in tools}
    assert names == {"list_directory", "trace_path"}
    # Routing cache populated for the survivors only.
    assert tn._tunnel_tool_routes["t1"] == {"list_directory": "fs", "trace_path": "code"}


def test_call_tunnel_tool_routes_to_owner(monkeypatch):
    tn._tunnel_code_sockets["t1"] = object()
    tn._tunnel_tool_routes["t1"] = {"trace_path": "code"}

    seen = {}

    def responder(label, method, params):
        seen["label"] = label
        seen["method"] = method
        seen["params"] = params
        return {"result": {"content": [{"type": "text", "text": "traced"}]}}

    _stub_proxy(monkeypatch, responder)
    result = asyncio.run(tn.call_tunnel_tool("t1", "trace_path", {"symbol": "foo"}))
    assert result["content"][0]["text"] == "traced"
    assert seen["label"] == "code"
    assert seen["method"] == "tools/call"
    assert seen["params"] == {"name": "trace_path", "arguments": {"symbol": "foo"}}


def test_call_tunnel_tool_cold_cache_discovers(monkeypatch):
    """No cached route → bridge re-lists tools, then routes the call."""
    tn._tunnel_extract_sockets["t1"] = object()

    def responder(label, method, params):
        if method == "tools/list":
            return {"result": {"tools": [{"name": "get_symbols"}]}}
        return {"result": {"content": [{"type": "text", "text": "ok"}]}}

    _stub_proxy(monkeypatch, responder)
    result = asyncio.run(tn.call_tunnel_tool("t1", "get_symbols", {}))
    assert result["content"][0]["text"] == "ok"


def test_call_tunnel_tool_unknown_returns_none(monkeypatch):
    tn._tunnel_sockets["t1"] = object()

    def responder(label, method, params):
        return {"result": {"tools": [{"name": "read_file"}]}}

    _stub_proxy(monkeypatch, responder)
    assert asyncio.run(tn.call_tunnel_tool("t1", "no_such_tool", {})) is None


def test_call_tunnel_tool_surfaces_error(monkeypatch):
    tn._tunnel_code_sockets["t1"] = object()
    tn._tunnel_tool_routes["t1"] = {"trace_path": "code"}

    def responder(label, method, params):
        return {"error": {"code": -32603, "message": "boom"}}

    _stub_proxy(monkeypatch, responder)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(tn.call_tunnel_tool("t1", "trace_path", {}))


# ---------------------------------------------------------------------------
# Handler integration: tools/list aggregation + tools/call routing
# ---------------------------------------------------------------------------

def test_handler_tools_list_appends_tunnel_tools(monkeypatch):
    tenant = {"id": "t1", "plan": "pro"}

    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: True)

    async def fake_list(tid, reserved):
        assert tid == "t1"
        # Native tool names must be reserved.
        assert "log_task" in reserved
        return [{"name": "trace_path", "description": "graph"}]

    monkeypatch.setattr(tn, "list_tunnel_tools", fake_list)

    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    resp = asyncio.run(mh._handle_mcp_request(body, db=None, data_dir="/tmp", tenant=tenant))
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "trace_path" in names
    assert "log_task" in names  # native tools still present


def test_handler_tools_list_skips_bridge_without_tunnel(monkeypatch):
    tenant = {"id": "t1", "plan": "pro"}
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: False)

    called = {"list": False}

    async def fake_list(tid, reserved):
        called["list"] = True
        return []

    monkeypatch.setattr(tn, "list_tunnel_tools", fake_list)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    asyncio.run(mh._handle_mcp_request(body, db=None, data_dir="/tmp", tenant=tenant))
    assert called["list"] is False  # no tunnel → never queried


def test_handler_tools_call_routes_to_tunnel(monkeypatch):
    tenant = {"id": "t1", "plan": "pro"}
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: True)

    async def fake_call(tid, name, args):
        assert name == "trace_path"
        return {"content": [{"type": "text", "text": "graph-result"}]}

    monkeypatch.setattr(tn, "call_tunnel_tool", fake_call)
    body = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "trace_path", "arguments": {"symbol": "x"}},
    }
    resp = asyncio.run(mh._handle_mcp_request(body, db=None, data_dir="/tmp", tenant=tenant))
    # Tunnel result is passed through verbatim (already an MCP content envelope).
    assert resp["result"]["content"][0]["text"] == "graph-result"


def test_handler_tools_call_native_tool_not_routed_to_tunnel(monkeypatch):
    """A native tool name must never be intercepted by the bridge."""
    tenant = {"id": "t1", "plan": "pro"}
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: True)

    routed = {"hit": False}

    async def fake_call(tid, name, args):
        routed["hit"] = True
        return {"content": []}

    monkeypatch.setattr(tn, "call_tunnel_tool", fake_call)

    # log_task is native; it should dispatch natively (and fail on db=None), never
    # reach the tunnel bridge.
    body = {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "log_task", "arguments": {}},
    }
    asyncio.run(mh._handle_mcp_request(body, db=None, data_dir="/tmp", tenant=tenant))
    assert routed["hit"] is False
