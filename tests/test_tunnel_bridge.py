"""Tests for the single-connector tunnel bridge (meridian/routes/tunnel.py).

The bridge surfaces a tenant's live fs/code/extractor tunnel tools through the
Meridian remote-MCP endpoint: `tools/list` aggregates them and `tools/call`
routes matching names back over the WebSocket relay. These tests exercise the
bridge helpers directly (with `_do_proxy` stubbed) and the handler integration
(with the bridge stubbed), so neither needs a real WebSocket.
"""
from __future__ import annotations

import asyncio
import base64
import json
import types

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


# ---------------------------------------------------------------------------
# _do_proxy — HTTP relay over the tunnel socket
# ---------------------------------------------------------------------------

class _FakeWS:
    """Resolves the pending future inline when the server sends a request."""

    def __init__(self, pending, response):
        self._pending = pending
        self._response = response

    async def send_json(self, payload):
        fut = self._pending.get(payload["id"])
        if fut is not None and not fut.done():
            fut.set_result({**self._response, "id": payload["id"]})


def test_do_proxy_503_when_socket_not_connected():
    resp = asyncio.run(tn._do_proxy(
        "t1", "POST", "/mcp", "", {}, b"x",
        tn._tunnel_sockets, tn._pending_reqs, "fs",
    ))
    assert resp.status_code == 503
    assert b"fs tunnel not connected" in resp.body


def test_do_proxy_success_roundtrip_strips_hop_headers():
    body = base64.b64encode(b'{"ok":true}').decode()
    response = {
        "status": 200,
        # transfer-encoding/content-length are hop-by-hop and must be dropped.
        "headers": {"content-type": "application/json",
                    "transfer-encoding": "chunked", "content-length": "11"},
        "body": body,
    }
    tn._tunnel_sockets["t1"] = _FakeWS(tn._pending_reqs, response)
    resp = asyncio.run(tn._do_proxy(
        "t1", "POST", "/mcp", "", {}, b'{"q":1}',
        tn._tunnel_sockets, tn._pending_reqs, "fs",
    ))
    assert resp.status_code == 200
    assert resp.body == b'{"ok":true}'
    hdr_keys = {k.lower() for k in resp.headers.keys()}
    assert "transfer-encoding" not in hdr_keys
    assert "content-type" in hdr_keys
    # Pending map is cleaned up after the request resolves.
    assert tn._pending_reqs == {}


# ---------------------------------------------------------------------------
# Proxy route guards — hosted mode + status endpoint + header forwarding
# ---------------------------------------------------------------------------

def test_code_proxy_503_when_not_hosted(monkeypatch):
    monkeypatch.setattr(tn, "_hosted_mode", lambda: False)
    resp = asyncio.run(tn._code_proxy("t1", "/mcp", None))
    assert resp.status_code == 503
    assert b"hosted mode" in resp.body


def test_extract_proxy_503_when_not_hosted(monkeypatch):
    monkeypatch.setattr(tn, "_hosted_mode", lambda: False)
    resp = asyncio.run(tn._extract_proxy("t1", "/mcp", None))
    assert resp.status_code == 503
    assert b"hosted mode" in resp.body


def test_tunnel_status_reports_active_sockets():
    tn._tunnel_sockets["t1"] = object()
    tn._tunnel_code_sockets["t1"] = object()
    status = asyncio.run(tn.tunnel_status("t1"))
    assert status == {
        "tenant_id": "t1",
        "active": True,
        "code_active": True,
        "extract_active": False,
    }


def test_fwd_headers_strips_sensitive_and_host():
    req = types.SimpleNamespace(headers={
        "host": "usemeridian.us",
        "authorization": "Bearer x",
        "cookie": "s=1",
        "x-forwarded-for": "1.2.3.4",
        "accept": "application/json",
    })
    assert tn._fwd_headers(req) == {"accept": "application/json"}


# ---------------------------------------------------------------------------
# _do_proxy — timeout (504) and send-failure (502) paths
# ---------------------------------------------------------------------------

class _SilentWS:
    """Accepts the request but never resolves the future (forces a timeout)."""

    async def send_json(self, payload):
        return None


class _BrokenWS:
    """Raises on send, exercising the 502 transport-error path."""

    async def send_json(self, payload):
        raise RuntimeError("socket gone")


def test_do_proxy_504_on_timeout(monkeypatch):
    monkeypatch.setattr(tn, "_PROXY_TIMEOUT", 0.05)
    tn._tunnel_sockets["t1"] = _SilentWS()
    resp = asyncio.run(tn._do_proxy(
        "t1", "POST", "/mcp", "", {}, b"x",
        tn._tunnel_sockets, tn._pending_reqs, "fs",
    ))
    assert resp.status_code == 504
    assert b"timeout" in resp.body
    assert tn._pending_reqs == {}  # cleaned up after timeout


def test_do_proxy_502_on_send_failure():
    tn._tunnel_sockets["t1"] = _BrokenWS()
    resp = asyncio.run(tn._do_proxy(
        "t1", "POST", "/mcp", "", {}, b"x",
        tn._tunnel_sockets, tn._pending_reqs, "fs",
    ))
    assert resp.status_code == 502
    assert tn._pending_reqs == {}


# ---------------------------------------------------------------------------
# HTTP proxy route wrappers — /fs/mcp /code/mcp /extract/mcp
# ---------------------------------------------------------------------------

class _FakeReq:
    """Minimal stand-in for a Starlette Request for the proxy route wrappers."""

    def __init__(self, path, query="", method="POST", headers=None, body=b""):
        self.method = method
        self.headers = headers or {}
        self.url = types.SimpleNamespace(path=path, query=query)
        self._body = body

    async def body(self):
        return self._body


def test_fs_mcp_proxy_503_when_not_hosted(monkeypatch):
    monkeypatch.setattr(tn, "_hosted_mode", lambda: False)
    resp = asyncio.run(tn.fs_mcp_proxy("t1", _FakeReq("/fs/mcp/t1")))
    assert resp.status_code == 503
    assert b"hosted mode" in resp.body


def test_fs_mcp_proxy_strips_prefix_and_503_without_tunnel(monkeypatch):
    monkeypatch.setattr(tn, "_hosted_mode", lambda: True)
    captured = {}

    async def fake_proxy(tenant_id, method, path, query, headers, body_bytes):
        captured["path"] = path
        captured["query"] = query
        return Response(content=b'{"error":"fs tunnel not connected"}',
                        status_code=503, media_type="application/json")

    monkeypatch.setattr(tn, "_proxy_request", fake_proxy)
    req = _FakeReq("/fs/mcp/t1/mcp", query="x=1", headers={"host": "h", "accept": "j"})
    resp = asyncio.run(tn.fs_mcp_proxy("t1", req))
    assert resp.status_code == 503
    # The /fs/mcp/{tenant_id} prefix is stripped so the local proxy sees /mcp.
    assert captured["path"] == "/mcp"
    assert captured["query"] == "x=1"


def test_fs_mcp_proxy_subpath_builds_local_path(monkeypatch):
    monkeypatch.setattr(tn, "_hosted_mode", lambda: True)
    captured = {}

    async def fake_proxy(tenant_id, method, path, query, headers, body_bytes):
        captured["path"] = path
        return Response(content=b"{}", status_code=200, media_type="application/json")

    monkeypatch.setattr(tn, "_proxy_request", fake_proxy)
    resp = asyncio.run(tn.fs_mcp_proxy_subpath("t1", "sse", _FakeReq("/fs/mcp/t1/sse")))
    assert resp.status_code == 200
    assert captured["path"] == "/sse"


def test_code_mcp_proxy_routes_to_code_socket(monkeypatch):
    monkeypatch.setattr(tn, "_hosted_mode", lambda: True)
    resp = asyncio.run(tn.code_mcp_proxy("t1", _FakeReq("/code/mcp/t1")))
    # No code socket connected → 503 from _do_proxy.
    assert resp.status_code == 503
    assert b"code tunnel not connected" in resp.body


def test_extract_mcp_proxy_routes_to_extract_socket(monkeypatch):
    monkeypatch.setattr(tn, "_hosted_mode", lambda: True)
    resp = asyncio.run(tn.extract_mcp_proxy("t1", _FakeReq("/extract/mcp/t1")))
    assert resp.status_code == 503
    assert b"extract tunnel not connected" in resp.body
