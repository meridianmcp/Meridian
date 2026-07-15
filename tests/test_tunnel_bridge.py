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

import httpx
import pytest
from fastapi.responses import Response

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian.routes import tunnel as tn
from meridian.mcp import handler as mh


@pytest.fixture(autouse=True)
def _clean_bridge_state():
    """Reset per-process tunnel registries between tests."""
    def _reset():
        tn._tunnel_sockets.clear()
        tn._tunnel_code_sockets.clear()
        tn._tunnel_extract_sockets.clear()
        tn._tunnel_ppt_sockets.clear()
        tn._tunnel_word_sockets.clear()
        tn._tunnel_tool_routes.clear()
    _reset()
    yield
    _reset()


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


def test_slot_health_tracks_non_fs_slots():
    """a898710a — dc/ppt/word slots (served by _serve_tunnel_ws) can now record
    health via plugin_status, so a failed pre-flight surfaces as 'unhealthy'
    rather than falling back to 'inactive'. A later healthy report clears it."""
    try:
        # Default: assumed healthy until a plugin_status says otherwise.
        assert tn._slot_is_healthy("t-dc", "dc") is True
        tn._record_slot_health("t-dc", "dc", False, reason="preflight failed", detail="port 8813")
        assert tn._slot_is_healthy("t-dc", "dc") is False
        # Recovery / healthy report flips it back and clears any diagnostic.
        tn._record_slot_health("t-dc", "dc", True)
        assert tn._slot_is_healthy("t-dc", "dc") is True
    finally:
        tn._clear_slot_health("t-dc")


def test_custom_slots_registered_and_routed():
    """8fb69d54 — the 4 custom slots (p0-p3) are in _TUNNEL_LABELS + display names,
    _label_maps routes each to its own registry, has_active_tunnel detects a live
    custom socket, and CUSTOM_SLOT_PORTS pre-allocates ports 8814-8817."""
    from meridian.tunnel_plugins import CUSTOM_SLOT_PORTS
    assert CUSTOM_SLOT_PORTS == {"p0": 8814, "p1": 8815, "p2": 8816, "p3": 8817}
    for s in ("p0", "p1", "p2", "p3"):
        assert s in tn._TUNNEL_LABELS
        assert s in tn.SLOT_DISPLAY_NAMES
        sockets, pending = tn._label_maps(s)
        assert sockets is tn._tunnel_custom_sockets[s]
        assert pending is tn._pending_custom_reqs[s]
    assert tn.has_active_tunnel("t-custom") is False
    tn._tunnel_custom_sockets["p1"]["t-custom"] = object()
    try:
        assert tn.has_active_tunnel("t-custom") is True
    finally:
        tn._tunnel_custom_sockets["p1"].pop("t-custom", None)


def test_custom_slot_ws_routes_registered():
    """8fb69d54 — the /tunnel-p0 … /tunnel-p3 WebSocket routes exist on the app."""
    import meridian.server as _srv
    paths = [getattr(r, "path", "") for r in _srv.app.routes]
    for s in ("p0", "p1", "p2", "p3"):
        assert any(f"/tunnel-{s}/" in p for p in paths), f"missing route for {s}"


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
    # Connector-prefixed names can't collide with the bare reserved name, so all
    # three survive — namespaced by their connector slot.
    tools = asyncio.run(tn.list_tunnel_tools("t1", reserved_names={"read_file"}))
    names = {t["name"] for t in tools}
    assert names == {"filesystem__read_file", "filesystem__list_directory", "codebase__trace_path"}
    assert tn._tunnel_tool_routes["t1"] == {
        "filesystem__read_file": "fs", "filesystem__list_directory": "fs",
        "codebase__trace_path": "code",
    }


def test_list_tunnel_tools_namespaces_titles_for_source(monkeypatch):
    """connector-source — a slot whose inner server advertises a bare tool
    ``title`` (filesystem's "Read File") gets its title namespaced with the
    source ("Filesystem: Read File") so claude.ai's tool-permission UI shows the
    plugin; a tool with NO title is unchanged (its prefixed name carries the
    source); nested inputSchema param titles are left alone; no double-prefixing."""
    tn._tunnel_sockets["t1"] = object()
    tn._tunnel_code_sockets["t1"] = object()

    def responder(label, method, params):
        if label == "fs":
            return {"result": {"tools": [
                {"name": "read_file", "title": "Read File",
                 "inputSchema": {"properties": {"path": {"title": "File Path"}}}},
                {"name": "already", "title": "Filesystem: Already"},  # pre-namespaced
            ]}}
        if label == "code":
            return {"result": {"tools": [{"name": "trace_path"}]}}  # no tool title
        return {"result": {"tools": []}}

    _stub_proxy(monkeypatch, responder)
    try:
        tools = asyncio.run(tn.list_tunnel_tools("t1"))
        by_name = {t["name"]: t for t in tools}
        # fs tool title namespaced with the source connector.
        assert by_name["filesystem__read_file"]["title"] == "Filesystem: Read File"
        # nested inputSchema param title left untouched.
        assert (by_name["filesystem__read_file"]["inputSchema"]["properties"]
                ["path"]["title"] == "File Path")
        # an already-namespaced title is NOT double-prefixed.
        assert by_name["filesystem__already"]["title"] == "Filesystem: Already"
        # a tool with no title gets none added (its prefixed name shows the source).
        assert "title" not in by_name["codebase__trace_path"]
    finally:
        tn._tunnel_sockets.pop("t1", None)
        tn._tunnel_code_sockets.pop("t1", None)
        tn._tunnel_tool_routes.pop("t1", None)


# ---------------------------------------------------------------------------
# d71ba2e7 — core slot health registry + suppression
# ---------------------------------------------------------------------------

def test_slot_health_registry_record_query_clear():
    try:
        assert tn._slot_is_healthy("th", "fs") is True       # default: healthy
        tn._record_slot_health("th", "fs", False)
        assert tn._slot_is_healthy("th", "fs") is False
        tn._record_slot_health("th", "code", True)
        assert tn._slot_is_healthy("th", "code") is True
        # Clear one slot leaves the other.
        tn._clear_slot_health("th", "fs")
        assert tn._slot_is_healthy("th", "fs") is True
        assert tn._slot_is_healthy("th", "code") is True
        # Empty slot value is ignored.
        tn._record_slot_health("th", "", False)
        # Clear all.
        tn._clear_slot_health("th")
        assert tn._slot_health.get("th") is None
    finally:
        tn._slot_health.pop("th", None)


def test_list_tunnel_tools_suppresses_unhealthy_slot(monkeypatch):
    tn._tunnel_sockets["th2"] = object()
    tn._tunnel_code_sockets["th2"] = object()
    tn._record_slot_health("th2", "fs", False)  # fs marked unhealthy

    def responder(label, method, params):
        if label == "fs":
            return {"result": {"tools": [{"name": "read_file"}]}}
        if label == "code":
            return {"result": {"tools": [{"name": "trace_path"}]}}
        return {"result": {"tools": []}}

    _stub_proxy(monkeypatch, responder)
    try:
        tools = asyncio.run(tn.list_tunnel_tools("th2"))
        names = {t["name"] for t in tools}
        # fs suppressed; only code's tool survives.
        assert names == {"codebase__trace_path"}
        assert "filesystem__read_file" not in names
    finally:
        tn._slot_health.pop("th2", None)
        tn._tunnel_sockets.pop("th2", None)
        tn._tunnel_code_sockets.pop("th2", None)
        tn._tunnel_tool_routes.pop("th2", None)


def test_tunnel_status_includes_slot_health():
    tn._tunnel_sockets["th3"] = object()
    tn._record_slot_health("th3", "extract", False)
    try:
        status = asyncio.run(tn.tunnel_status("th3"))
        assert status["active"] is True
        assert status["slot_health"] == {"extract": False}
    finally:
        tn._slot_health.pop("th3", None)
        tn._tunnel_sockets.pop("th3", None)


def test_slot_status_detail_record_clear():
    """9a8645c1 — unhealthy reports stash a reason/detail; healthy clears it."""
    try:
        tn._record_slot_health("thd", "extract", False,
                               reason="access_denied", detail="use a specific repo path")
        assert tn._slot_status_detail["thd"]["extract"]["reason"] == "access_denied"
        status = asyncio.run(tn.tunnel_status("thd"))
        assert status["slot_status"]["extract"]["reason"] == "access_denied"
        assert "specific repo path" in status["slot_status"]["extract"]["detail"]
        tn._record_slot_health("thd", "extract", True)  # recovery clears it
        assert "extract" not in tn._slot_status_detail.get("thd", {})
    finally:
        tn._slot_health.pop("thd", None)
        tn._slot_status_detail.pop("thd", None)


def test_clear_slot_health_drops_detail():
    """Disconnect clears both health and the diagnostic detail."""
    try:
        tn._record_slot_health("thc", "extract", False, reason="access_denied", detail="x")
        tn._clear_slot_health("thc", "extract")
        assert tn._slot_status_detail.get("thc") is None
        assert tn._slot_health.get("thc") is None
    finally:
        tn._slot_health.pop("thc", None)
        tn._slot_status_detail.pop("thc", None)


def test_call_tunnel_tool_routes_to_owner(monkeypatch):
    tn._tunnel_code_sockets["t1"] = object()
    # Routing cache is keyed by the connector-prefixed name.
    tn._tunnel_tool_routes["t1"] = {"codebase__trace_path": "code"}

    seen = {}

    def responder(label, method, params):
        seen["label"] = label
        seen["method"] = method
        seen["params"] = params
        return {"result": {"content": [{"type": "text", "text": "traced"}]}}

    _stub_proxy(monkeypatch, responder)
    result = asyncio.run(tn.call_tunnel_tool("t1", "codebase__trace_path", {"symbol": "foo"}))
    assert result["content"][0]["text"] == "traced"
    assert seen["label"] == "code"
    assert seen["method"] == "tools/call"
    # The prefix is stripped before forwarding to the tunnel's local proxy.
    assert seen["params"] == {"name": "trace_path", "arguments": {"symbol": "foo"}}


# ---------------------------------------------------------------------------
# a19538fe — cross-instance tunnel miss (DB flag fallback for legibility)
# ---------------------------------------------------------------------------

def test_cross_instance_miss_true_when_db_active_but_memory_miss():
    # DB says the tunnel is active, but THIS instance holds no socket → the
    # socket is on a sibling Fly instance.
    assert tn.tunnel_cross_instance_miss({"id": "t1", "tunnel_active": 1}) is True


def test_cross_instance_miss_false_when_socket_present():
    tn._tunnel_sockets["t1"] = object()
    # In-memory socket present → not a cross-instance miss (it's genuinely here).
    assert tn.tunnel_cross_instance_miss({"id": "t1", "tunnel_active": 1}) is False


def test_cross_instance_miss_false_when_db_inactive():
    # No DB flag → a real "not connected", not a cross-instance miss.
    assert tn.tunnel_cross_instance_miss({"id": "t1", "tunnel_active": 0}) is False
    assert tn.tunnel_cross_instance_miss({"id": "t1"}) is False


def test_cross_instance_miss_false_when_tenant_none():
    assert tn.tunnel_cross_instance_miss(None) is False
    assert isinstance(tn.CROSS_INSTANCE_MISS_MESSAGE, str)
    assert tn.CROSS_INSTANCE_MISS_MESSAGE


def test_build_graph_searcher_none_without_tunnel():
    """4cfaecc2 — no active tunnel → no searcher (enrichment stays a no-op)."""
    assert tn.build_graph_searcher(None) is None
    assert tn.build_graph_searcher("no-tunnel") is None


def test_build_graph_searcher_queries_code_intel_slot(monkeypatch):
    """4cfaecc2 — with a tunnel, the searcher issues search_graph over the tunnel
    and unwraps the MCP text envelope into the raw match payload.

    51bdacc0 — the routes cache is keyed by the PREFIXED tool name
    (codebase__search_graph) as list_tunnel_tools populates it; build_graph_searcher
    must pass the prefixed name or call_tunnel_tool returns None and the graph rung
    never runs.
    """
    tn._tunnel_sockets["t1"] = object()
    tn._tunnel_code_sockets["t1"] = object()
    # Use the prefixed name — this is what list_tunnel_tools actually stores.
    tn._tunnel_tool_routes["t1"] = {"codebase__search_graph": "code"}
    seen = {}

    def responder(label, method, params):
        seen["params"] = params
        payload = {"results": [{"file": "x.py", "name": "foo"}]}
        return {"result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}

    _stub_proxy(monkeypatch, responder)
    searcher = tn.build_graph_searcher("t1")
    assert searcher is not None
    matches = asyncio.run(searcher("some query"))
    # call_tunnel_tool strips the prefix before forwarding, so the bare name is sent
    assert seen["params"]["name"] == "search_graph"
    assert matches["results"][0]["file"] == "x.py"


def test_build_graph_searcher_swallows_tunnel_errors(monkeypatch):
    """The searcher never raises — a tunnel error yields None for that query."""
    tn._tunnel_sockets["t1"] = object()

    async def boom(*_a, **_k):
        raise RuntimeError("tunnel down")

    monkeypatch.setattr(tn, "call_tunnel_tool", boom)
    searcher = tn.build_graph_searcher("t1")
    assert searcher is not None
    assert asyncio.run(searcher("q")) is None


def test_build_graph_searcher_uses_prefixed_name(monkeypatch):
    """51bdacc0 — build_graph_searcher must pass the PREFIXED tool name
    (codebase__search_graph) to call_tunnel_tool so the routes cache lookup
    succeeds.  list_tunnel_tools stores prefixed names; passing the bare
    'search_graph' always returns None (cold-cache discover also finds only the
    prefixed name) and the graph rung of prospect_symbol never fires."""
    tn._tunnel_sockets["t1-prefix"] = object()
    tn._tunnel_code_sockets["t1-prefix"] = object()
    # Simulate the routes cache as list_tunnel_tools would populate it.
    tn._tunnel_tool_routes["t1-prefix"] = {"codebase__search_graph": "code"}
    seen_name: list[str] = []

    async def fake_call_tunnel(tid, name, args, **kw):
        seen_name.append(name)
        payload = {"results": [{"file": "y.py", "name": "bar"}]}
        return {"content": [{"type": "text", "text": json.dumps(payload)}]}

    monkeypatch.setattr(tn, "call_tunnel_tool", fake_call_tunnel)
    searcher = tn.build_graph_searcher("t1-prefix")
    assert searcher is not None
    result = asyncio.run(searcher("bar_func"))
    # The prefixed name must be passed so the routes cache resolves it.
    assert seen_name == ["codebase__search_graph"], (
        f"build_graph_searcher passed {seen_name!r} instead of ['codebase__search_graph']; "
        "bare 'search_graph' is never in the routes cache and causes silent None return"
    )
    assert result is not None
    tn._tunnel_sockets.pop("t1-prefix", None)
    tn._tunnel_code_sockets.pop("t1-prefix", None)
    tn._tunnel_tool_routes.pop("t1-prefix", None)


def test_call_tunnel_tool_strips_prefix_before_forward(monkeypatch):
    """call_tunnel_tool('codebase__get_symbols_tool') forwards bare 'get_symbols_tool'."""
    tn._tunnel_code_sockets["t1"] = object()
    tn._tunnel_tool_routes["t1"] = {"codebase__get_symbols_tool": "code"}
    seen = {}

    def responder(label, method, params):
        seen["params"] = params
        return {"result": {"content": []}}

    _stub_proxy(monkeypatch, responder)
    asyncio.run(tn.call_tunnel_tool("t1", "codebase__get_symbols_tool", {}))
    assert seen["params"]["name"] == "get_symbols_tool"


def test_call_tunnel_tool_cold_cache_discovers(monkeypatch):
    """No cached route → bridge re-lists tools (which prefixes), then routes the
    call by the prefixed name and forwards the bare name."""
    tn._tunnel_extract_sockets["t1"] = object()
    seen = {}

    def responder(label, method, params):
        if method == "tools/list":
            return {"result": {"tools": [{"name": "get_symbols"}]}}
        seen["params"] = params
        return {"result": {"content": [{"type": "text", "text": "ok"}]}}

    _stub_proxy(monkeypatch, responder)
    # Caller uses the advertised (prefixed) name; cold cache rediscovers it.
    result = asyncio.run(tn.call_tunnel_tool("t1", "extractor__get_symbols", {}))
    assert result["content"][0]["text"] == "ok"
    assert seen["params"]["name"] == "get_symbols"  # bare name forwarded


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


def test_handler_tools_list_signals_error_when_tunnel_fetch_fails(monkeypatch):
    """7033c8e2 — a tunnel that is CONNECTED but whose tool fetch throws must not
    silently drop to the short native list. The native tools still serve, but the
    result carries a machine-readable degraded/error signal so the failure is
    visible instead of silent."""
    tenant = {"id": "t1", "plan": "pro"}
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: True)

    async def boom(tid, reserved):
        raise RuntimeError("socket reset")

    monkeypatch.setattr(tn, "list_tunnel_tools", boom)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    resp = asyncio.run(mh._handle_mcp_request(body, db=None, data_dir="/tmp", tenant=tenant))
    # native tools still present — the hiccup never breaks tools/list
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "log_task" in names
    # ...but the failure is SIGNALLED, not swallowed
    health = resp["result"].get("_meta", {}).get("meridian/tunnelHealth")
    assert health and health["status"] == "error"
    assert "socket reset" in health.get("detail", "")


def test_handler_tools_list_signals_degraded_when_tunnel_returns_zero(monkeypatch):
    """7033c8e2 — a connected tunnel that advertises ZERO tools (slot still
    starting / failed pre-flight) is flagged 'degraded', not shown as an
    unexplained short list."""
    tenant = {"id": "t1", "plan": "pro"}
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: True)

    async def empty(tid, reserved):
        return []

    monkeypatch.setattr(tn, "list_tunnel_tools", empty)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    resp = asyncio.run(mh._handle_mcp_request(body, db=None, data_dir="/tmp", tenant=tenant))
    health = resp["result"].get("_meta", {}).get("meridian/tunnelHealth")
    assert health and health["status"] == "degraded"


def test_handler_tools_list_no_health_meta_when_tunnel_healthy(monkeypatch):
    """7033c8e2 — on the happy path (tunnel returns tools) NO health _meta is
    attached, so the signal only ever appears on a real problem."""
    tenant = {"id": "t1", "plan": "pro"}
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: True)

    async def ok(tid, reserved):
        return [{"name": "trace_path", "description": "graph"}]

    monkeypatch.setattr(tn, "list_tunnel_tools", ok)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    resp = asyncio.run(mh._handle_mcp_request(body, db=None, data_dir="/tmp", tenant=tenant))
    assert "trace_path" in {t["name"] for t in resp["result"]["tools"]}
    meta = resp["result"].get("_meta") or {}
    assert "meridian/tunnelHealth" not in meta


def test_handler_tools_call_routes_to_tunnel(monkeypatch):
    tenant = {"id": "t1", "plan": "pro"}
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: True)

    async def fake_call(tid, name, args, **kwargs):
        # 73d233e4 — the handler now threads db/session_id through so the word
        # write-guard can consult claims; accept them via **kwargs.
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
        "ppt_active": False,
        "word_active": False,
        "dc_active": False,
        "docs_active": False,
        "zotero_active": False,
        "slot_health": {},  # d71ba2e7 — no slots reported unhealthy
        "slot_status": {},  # 9a8645c1 — no slot diagnostics
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


# ---------------------------------------------------------------------------
# Phase 3 — code-intel-first description rewriting at the bridge
# ---------------------------------------------------------------------------

def test_rewrite_tool_description_prepends_for_prefixed_read_file():
    out = tn._rewrite_tool_description({"name": "filesystem__read_file", "description": "Read a file."})
    assert out["description"].startswith("IMPORTANT:")
    assert "Read a file." in out["description"]


def test_rewrite_tool_description_handles_read_multiple_and_empty_desc():
    out = tn._rewrite_tool_description({"name": "filesystem__read_multiple_files"})
    assert out["description"] == tn._CODE_INTEL_FIRST_GUIDANCE


def test_rewrite_tool_description_skips_bare_and_non_fs_read_file():
    # Bare (un-prefixed) read_file is NOT rewritten — only the filesystem connector's is.
    bare = {"name": "read_file", "description": "Read a file."}
    assert tn._rewrite_tool_description(bare) is bare
    # A codebase-connector read_file is a different server — also left alone.
    code = {"name": "codebase:read_file", "description": "graph read"}
    assert tn._rewrite_tool_description(code) is code


def test_rewrite_tool_description_leaves_other_tools_untouched():
    tool = {"name": "codebase:search_graph", "description": "Query the graph."}
    assert tn._rewrite_tool_description(tool) is tool


def test_rewrite_tool_description_is_idempotent():
    once = tn._rewrite_tool_description({"name": "filesystem__read_file", "description": "x"})
    twice = tn._rewrite_tool_description(once)
    assert once["description"] == twice["description"]
    assert twice["description"].count("IMPORTANT:") == 1


def test_list_tunnel_tools_prefixes_and_rewrites(monkeypatch):
    """Aggregated tools are connector-prefixed (display names); only
    filesystem__read_file gets the rewrite."""
    tn._tunnel_sockets["t1"] = object()
    tn._tunnel_code_sockets["t1"] = object()

    def responder(label, method, params):
        if label == "fs":
            return {"result": {"tools": [
                {"name": "read_file", "description": "Read a file."},
                {"name": "list_directory", "description": "List a dir."},
            ]}}
        if label == "code":
            return {"result": {"tools": [{"name": "get_symbols_tool", "description": "syms"}]}}
        return {"result": {"tools": []}}

    _stub_proxy(monkeypatch, responder)
    tools = asyncio.run(tn.list_tunnel_tools("t1"))
    by_name = {t["name"]: t for t in tools}
    # Names are connector-namespaced with full display names.
    assert "codebase__get_symbols_tool" in by_name
    assert "filesystem__read_file" in by_name and "filesystem__list_directory" in by_name
    assert "get_symbols_tool" not in by_name  # bare name not advertised
    # Only filesystem__read_file gets the code-intel-first directive.
    assert by_name["filesystem__read_file"]["description"].startswith("IMPORTANT:")
    assert by_name["filesystem__list_directory"]["description"] == "List a dir."
    assert by_name["codebase__get_symbols_tool"]["description"] == "syms"


def test_fs_and_code_read_file_coexist(monkeypatch):
    """filesystem__read_file and codebase__read_file are distinct, non-colliding."""
    tn._tunnel_sockets["t1"] = object()
    tn._tunnel_code_sockets["t1"] = object()

    def responder(label, method, params):
        return {"result": {"tools": [{"name": "read_file", "description": f"{label} read"}]}}

    _stub_proxy(monkeypatch, responder)
    tools = asyncio.run(tn.list_tunnel_tools("t1"))
    names = {t["name"] for t in tools}
    assert {"filesystem__read_file", "codebase__read_file"} <= names
    routes = tn._tunnel_tool_routes["t1"]
    assert routes["filesystem__read_file"] == "fs"   # routed back via internal label
    assert routes["codebase__read_file"] == "code"


def test_slot_display_names_cover_all_labels():
    """Every tunnel slot has a display name (so no label leaks as a raw prefix)."""
    for label in tn._TUNNEL_LABELS:
        assert label in tn.SLOT_DISPLAY_NAMES


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


# ---------------------------------------------------------------------------
# Office tunnels (ppt / word) — route guards + roundtrip
# ---------------------------------------------------------------------------

def test_ppt_proxy_503_when_not_hosted(monkeypatch):
    monkeypatch.setattr(tn, "_hosted_mode", lambda: False)
    resp = asyncio.run(tn.ppt_mcp_proxy("t1", _FakeReq("/ppt/mcp/t1")))
    assert resp.status_code == 503
    assert b"hosted mode" in resp.body


def test_word_proxy_503_when_not_hosted(monkeypatch):
    monkeypatch.setattr(tn, "_hosted_mode", lambda: False)
    resp = asyncio.run(tn.word_mcp_proxy("t1", _FakeReq("/word/mcp/t1")))
    assert resp.status_code == 503
    assert b"hosted mode" in resp.body


def test_ppt_proxy_503_when_no_socket(monkeypatch):
    monkeypatch.setattr(tn, "_hosted_mode", lambda: True)
    resp = asyncio.run(tn.ppt_mcp_proxy("t1", _FakeReq("/ppt/mcp/t1")))
    assert resp.status_code == 503
    assert b"ppt tunnel not connected" in resp.body


def test_word_proxy_subpath_200_roundtrip(monkeypatch):
    monkeypatch.setattr(tn, "_hosted_mode", lambda: True)
    response = {"status": 200, "headers": {"content-type": "application/json"},
                "body": base64.b64encode(b'{"ok":1}').decode()}
    tn._tunnel_word_sockets["t1"] = _FakeWS(tn._pending_word_reqs, response)
    resp = asyncio.run(tn.word_mcp_proxy_subpath("t1", "mcp", _FakeReq("/word/mcp/t1/mcp")))
    assert resp.status_code == 200
    assert resp.body == b'{"ok":1}'


def test_office_slots_in_label_maps_and_bridge():
    # _label_maps resolves the new slots to their own registries.
    assert tn._label_maps("ppt")[0] is tn._tunnel_ppt_sockets
    assert tn._label_maps("word")[0] is tn._tunnel_word_sockets
    assert "ppt" in tn._TUNNEL_LABELS and "word" in tn._TUNNEL_LABELS
    tn._tunnel_word_sockets["t1"] = object()
    assert tn.has_active_tunnel("t1") is True


# ---------------------------------------------------------------------------
# send_active_repo_control — helper for the MCP handler
# ---------------------------------------------------------------------------

class _FakeExtractWS:
    """Minimal WebSocket stub for send_active_repo_control tests."""
    def __init__(self, raise_on_send=False):
        self.sent = []
        self._raise = raise_on_send

    async def send_json(self, obj):
        if self._raise:
            raise RuntimeError("ws broken")
        self.sent.append(obj)


def test_send_active_repo_control_not_connected():
    # No extract socket for this tenant → not_connected status.
    result = asyncio.run(tn.send_active_repo_control("no-tenant", "/some/repo"))
    assert result["status"] == "not_connected"
    assert "extract tunnel" in result["message"]


def test_send_active_repo_control_ok():
    ws = _FakeExtractWS()
    tn._tunnel_extract_sockets["t1"] = ws
    result = asyncio.run(tn.send_active_repo_control("t1", "/my/repo"))
    assert result == {"status": "ok", "repo_path": "/my/repo"}
    assert ws.sent == [{"type": "set_active_repo", "repo_path": "/my/repo"}]


def test_send_active_repo_control_send_error():
    ws = _FakeExtractWS(raise_on_send=True)
    tn._tunnel_extract_sockets["t1"] = ws
    result = asyncio.run(tn.send_active_repo_control("t1", "/my/repo"))
    assert result["status"] == "error"
    assert "ws broken" in result["message"]


# ---------------------------------------------------------------------------
# 4d9ad87b — _tenant_active_repo cache + X-Meridian-Repo-Path injection
# ---------------------------------------------------------------------------

def test_send_active_repo_control_updates_cache():
    """send_active_repo_control always updates _tenant_active_repo (even without a WS)."""
    tn._tenant_active_repo.pop("rpc-tenant", None)
    tn._tunnel_extract_sockets.pop("rpc-tenant", None)
    asyncio.run(tn.send_active_repo_control("rpc-tenant", "/cached/repo"))
    assert tn._tenant_active_repo.get("rpc-tenant") == "/cached/repo"


def test_send_active_repo_control_with_ws_updates_cache_and_sends(monkeypatch):
    """send_active_repo_control with a live WS updates the cache AND sends the control msg."""
    ws = _FakeExtractWS()
    tn._tunnel_extract_sockets["rpc-ws-tenant"] = ws
    tn._tenant_active_repo.pop("rpc-ws-tenant", None)
    result = asyncio.run(tn.send_active_repo_control("rpc-ws-tenant", "/ws/repo"))
    assert result["status"] == "ok"
    assert tn._tenant_active_repo.get("rpc-ws-tenant") == "/ws/repo"
    assert ws.sent[0]["repo_path"] == "/ws/repo"


def test_call_tunnel_tool_injects_repo_path_header(monkeypatch):
    """call_tunnel_tool injects X-Meridian-Repo-Path from _tenant_active_repo cache."""
    tn._tunnel_extract_sockets["rph-tenant"] = object()
    tn._tunnel_tool_routes["rph-tenant"] = {"extractor__find_symbol": "extract"}
    tn._tenant_active_repo["rph-tenant"] = "/my/active/repo"
    captured_headers = {}

    async def fake_do_proxy(tenant_id, method, path, query, headers, body, sockets, pending, label):
        captured_headers.update(headers)
        return Response(
            content=json.dumps({"result": {"content": [{"type": "text", "text": "ok"}]}}).encode(),
            status_code=200, media_type="application/json",
        )

    monkeypatch.setattr(tn, "_do_proxy", fake_do_proxy)
    asyncio.run(tn.call_tunnel_tool("rph-tenant", "extractor__find_symbol", {"name": "foo"}))
    assert captured_headers.get("x-meridian-repo-path") == "/my/active/repo"


def test_call_tunnel_tool_explicit_repo_path_overrides_cache(monkeypatch):
    """An explicit repo_path arg overrides the cached value."""
    tn._tunnel_extract_sockets["rph2"] = object()
    tn._tunnel_tool_routes["rph2"] = {"extractor__find_symbol": "extract"}
    tn._tenant_active_repo["rph2"] = "/cached/repo"
    captured_headers = {}

    async def fake_do_proxy(tenant_id, method, path, query, headers, body, sockets, pending, label):
        captured_headers.update(headers)
        return Response(
            content=json.dumps({"result": {"content": []}}).encode(),
            status_code=200, media_type="application/json",
        )

    monkeypatch.setattr(tn, "_do_proxy", fake_do_proxy)
    asyncio.run(tn.call_tunnel_tool("rph2", "extractor__find_symbol", {}, repo_path="/explicit/repo"))
    assert captured_headers.get("x-meridian-repo-path") == "/explicit/repo"


def test_call_tunnel_tool_no_repo_path_no_header(monkeypatch):
    """Without a repo_path (no cache, no arg) the header is not injected."""
    tn._tunnel_extract_sockets["rph3"] = object()
    tn._tunnel_tool_routes["rph3"] = {"extractor__find_symbol": "extract"}
    tn._tenant_active_repo.pop("rph3", None)
    captured_headers = {}

    async def fake_do_proxy(tenant_id, method, path, query, headers, body, sockets, pending, label):
        captured_headers.update(headers)
        return Response(
            content=json.dumps({"result": {"content": []}}).encode(),
            status_code=200, media_type="application/json",
        )

    monkeypatch.setattr(tn, "_do_proxy", fake_do_proxy)
    asyncio.run(tn.call_tunnel_tool("rph3", "extractor__find_symbol", {}))
    assert "x-meridian-repo-path" not in captured_headers


# ---------------------------------------------------------------------------
# 9f6aec5f — codebase-context injection for start_session orientation
# ---------------------------------------------------------------------------

import meridian.server as srv  # noqa: E402


def test_summarize_architecture_extracts_fields():
    arch = {
        "packages": [{"name": "meridian"}, {"name": "tests"}],
        "layers": ["routes", "db"],
        "hotspots": [{"symbol": "server.app"}, {"symbol": "db.init_db"}],
        "entry_points": [{"path": "meridian/__main__.py"}],
        "stats": {"files": 120, "symbols": 3400, "blob": "x" * 999},
    }
    out = srv._summarize_architecture(arch)
    assert out["packages"] == ["meridian", "tests"]
    assert out["layers"] == ["routes", "db"]
    assert out["hotspots"] == ["server.app", "db.init_db"]
    assert out["entry_points"] == ["meridian/__main__.py"]
    # Only small scalar stats survive; the big blob is dropped.
    assert out["stats"] == {"files": 120, "symbols": 3400}


def test_summarize_architecture_garbage_returns_none():
    assert srv._summarize_architecture(None) is None
    assert srv._summarize_architecture({}) is None
    assert srv._summarize_architecture({"packages": "nope"}) is None


def test_truncate_codebase_summary_limits():
    summary = {
        "packages": list("abcdefgh"),
        "hotspots": list("12345678"),
        "entry_points": ["a", "b", "c", "d"],
        "layers": ["x", "y"],
        "stats": {"files": 1},
    }
    out = srv._truncate_codebase_summary(summary)
    assert out["packages"] == list("abcde")     # capped at 5
    assert out["hotspots"] == list("12345")      # capped at 5
    assert out["entry_points"] == ["a", "b", "c"]  # capped at 3
    assert out["stats"] == {"files": 1}
    assert "layers" not in out                   # dropped in compact form


def test_parse_tunnel_tool_text_decodes():
    res = {"content": [{"type": "text", "text": json.dumps({"packages": []})}]}
    assert srv._parse_tunnel_tool_text(res) == {"packages": []}
    assert srv._parse_tunnel_tool_text({"content": []}) is None
    assert srv._parse_tunnel_tool_text({"content": [{"text": "not json"}]}) is None
    assert srv._parse_tunnel_tool_text(None) is None


def test_build_codebase_context_no_tunnel_returns_none():
    assert asyncio.run(srv._build_codebase_context(None, "p1", compact=True)) is None
    # tenant set but no code socket connected.
    assert asyncio.run(srv._build_codebase_context("tc", "p1", compact=True)) is None


def test_build_codebase_context_unhealthy_slot_returns_none(monkeypatch):
    tn._tunnel_code_sockets["tc"] = object()
    tn._record_slot_health("tc", "code", False)
    try:
        out = asyncio.run(srv._build_codebase_context("tc", "p1", compact=True))
        assert out is None
    finally:
        tn._slot_health.pop("tc", None)


def test_build_codebase_context_fetches_and_caches(monkeypatch):
    tn._tunnel_code_sockets["tc"] = object()
    calls = {"n": 0}

    async def fake_call(tenant_id, name, args):
        calls["n"] += 1
        assert name == "codebase__get_architecture"
        return {"content": [{"type": "text", "text": json.dumps({
            "packages": [{"name": "meridian"}],
            "hotspots": [{"symbol": "server.app"}],
        })}]}

    monkeypatch.setattr(tn, "call_tunnel_tool", fake_call)
    srv._codebase_context_cache.pop("p9", None)
    try:
        out = asyncio.run(srv._build_codebase_context("tc", "p9", compact=False))
        assert out["packages"] == ["meridian"]
        assert out["hotspots"] == ["server.app"]
        assert "note" in out
        # Second call within TTL is served from cache (no second tunnel call).
        out2 = asyncio.run(srv._build_codebase_context("tc", "p9", compact=True))
        assert out2["packages"] == ["meridian"]
        assert calls["n"] == 1
    finally:
        srv._codebase_context_cache.pop("p9", None)


def test_build_codebase_context_not_indexed_returns_none(monkeypatch):
    tn._tunnel_code_sockets["tc"] = object()

    async def fake_call(tenant_id, name, args):
        return {"content": [{"type": "text", "text": json.dumps({})}]}  # empty arch

    monkeypatch.setattr(tn, "call_tunnel_tool", fake_call)
    srv._codebase_context_cache.pop("pne", None)
    out = asyncio.run(srv._build_codebase_context("tc", "pne", compact=True))
    assert out is None


# ---------------------------------------------------------------------------
# 9dde426f — GET /tunnel/registry must be fully async (no blocking urlopen)
# ---------------------------------------------------------------------------


class _FakeRegistryResp:
    """Stand-in for an httpx.Response from the MCP registry."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return self._payload


def _fake_async_client(handler):
    """Return a lambda producing an async-context-manager httpx stand-in whose
    .get(...) delegates to `handler(url, params, headers)` (sync or raising)."""

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            return handler(url, params, headers)

    return lambda *a, **k: _FakeClient()


def _call_registry(qp=None):
    req = types.SimpleNamespace(query_params=qp or {})
    resp = asyncio.run(tn.get_mcp_registry(req))
    return resp.status_code, json.loads(resp.body)


def test_registry_is_async_and_parses_servers(monkeypatch):
    """The handler awaits httpx.AsyncClient (never a blocking urlopen) and maps
    the upstream payload into the trimmed server shape."""
    tn._registry_cache.clear()
    payload = {
        "servers": [
            {
                "id": "io.example/foo",
                "name": "foo",
                "description": "d" * 400,  # trimmed to 200
                "source_code_location": {"url": "https://example.com/foo"},
                "packages": [{"runtime": "npm", "name": "foo-mcp", "package_arguments": []}],
            }
        ],
        "nextCursor": "abc",
    }

    def handler(url, params, headers):
        assert url == tn._REGISTRY_BASE  # params passed separately, not pre-encoded
        assert params["limit"] == "20"
        assert headers["Accept"] == "application/json"
        return _FakeRegistryResp(payload)

    monkeypatch.setattr(tn.httpx, "AsyncClient", _fake_async_client(handler))
    status, body = _call_registry()
    assert status == 200
    assert body["next_cursor"] == "abc"
    assert len(body["servers"]) == 1
    s = body["servers"][0]
    assert s["id"] == "io.example/foo"
    assert s["homepage"] == "https://example.com/foo"
    assert s["install_command"].startswith("npx -y foo-mcp")
    assert len(s["description"]) == 200


def test_registry_upstream_error_returns_503_when_uncached(monkeypatch):
    """A slow/failing upstream must fail fast with 503 + empty list, not hang
    the loop and not raise."""
    tn._registry_cache.clear()

    def boom(url, params, headers):
        raise httpx.ConnectTimeout("upstream slow")

    monkeypatch.setattr(tn.httpx, "AsyncClient", _fake_async_client(boom))
    status, body = _call_registry({"limit": "5"})
    assert status == 503
    assert body["servers"] == []
    assert "registry unavailable" in body["error"]


def test_registry_serves_cache_on_later_error(monkeypatch):
    """Once a page is cached, a subsequent upstream failure serves the cached
    copy (cached=True) instead of a 503 — so the browse UI never blanks out."""
    tn._registry_cache.clear()
    good = {"servers": [{"id": "a", "name": "a"}], "nextCursor": None}
    monkeypatch.setattr(
        tn.httpx, "AsyncClient",
        _fake_async_client(lambda u, p, h: _FakeRegistryResp(good)),
    )
    status, body = _call_registry()
    assert status == 200 and body.get("cached") is not True

    def boom(url, params, headers):
        raise httpx.ReadTimeout("later failure")

    monkeypatch.setattr(tn.httpx, "AsyncClient", _fake_async_client(boom))
    status2, body2 = _call_registry()
    assert status2 == 200
    assert body2["cached"] is True
    assert body2["servers"][0]["id"] == "a"
