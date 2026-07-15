"""Tests for 1365e01a — tunnel-routing of search_outputs / annotate_outputs.

Verifies that:

1. _tunnel_proxy_outputs_tool returns None when no active tunnel is present.
2. _tunnel_proxy_outputs_tool returns None when the tunnel has no matching tool.
3. _tunnel_proxy_outputs_tool returns the decoded JSON dict when the tunnel exposes
   a prefixed tool (e.g. ``filesystem__search_outputs``) and the call succeeds.
4. _tunnel_proxy_outputs_tool falls back to None on tunnel call error (no raise).
5. _tunnel_proxy_outputs_tool triggers list_tunnel_tools when routing cache is cold.
6. search_outputs in hosted mode with no tenant → honest error, tunnel_tried=False.
7. search_outputs in hosted mode with tenant but no tunnel → honest error, tunnel_tried=True.
8. search_outputs in hosted mode with tenant + tunnel exposing the tool → proxied result.
9. annotate_outputs in hosted mode with tenant + tunnel exposing the tool → proxied result.
10. annotate_outputs in hosted mode with no tunnel → honest error preserved.
11. Non-regression: required-arg guards (outputs_dir, query) still fire before hosted-check.
12. Non-regression: existing hosted-mode error shape is preserved when tunnel unavailable.

All tests mock the tunnel layer via monkeypatch on the real tunnel module to
avoid real WebSocket/tunnel infrastructure.
"""
from __future__ import annotations

import asyncio
import json
import unittest.mock as mock

import pytest

import meridian.server  # noqa: F401 — initialise server module before handler import
from meridian.mcp import handler as mh
from meridian.routes import tunnel as tunnel_mod


def _run(coro):
    return asyncio.run(coro)


def _req(method, params=None, req_id=1):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


# ---------------------------------------------------------------------------
# _tunnel_proxy_outputs_tool unit tests (monkeypatch real tunnel module attrs)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proxy_returns_none_when_no_active_tunnel(monkeypatch):
    """_tunnel_proxy_outputs_tool returns None immediately if no active tunnel."""
    monkeypatch.setattr(tunnel_mod, "has_active_tunnel", lambda tenant_id: False)
    monkeypatch.setattr(tunnel_mod, "_tunnel_tool_routes", {})

    result = await mh._tunnel_proxy_outputs_tool("tenant-1", "search_outputs", {})
    assert result is None


@pytest.mark.asyncio
async def test_proxy_returns_none_when_tunnel_has_no_matching_tool(monkeypatch):
    """_tunnel_proxy_outputs_tool returns None when the routing cache has no tool
    matching the bare name (tunnel is active but doesn't run meridian-outputs-mcp)."""
    monkeypatch.setattr(tunnel_mod, "has_active_tunnel", lambda tenant_id: True)
    # Cache has only filesystem tools, not search_outputs
    monkeypatch.setattr(tunnel_mod, "_tunnel_tool_routes", {
        "tenant-1": {
            "filesystem__read_file": "fs",
            "filesystem__list_directory": "fs",
        }
    })
    monkeypatch.setattr(tunnel_mod, "list_tunnel_tools", mock.AsyncMock(return_value=[]))

    result = await mh._tunnel_proxy_outputs_tool("tenant-1", "search_outputs", {})
    assert result is None


@pytest.mark.asyncio
async def test_proxy_finds_prefixed_tool_and_returns_decoded_json(monkeypatch):
    """_tunnel_proxy_outputs_tool finds filesystem__search_outputs in the cache,
    calls call_tunnel_tool, and returns the JSON-decoded result dict."""
    monkeypatch.setattr(tunnel_mod, "has_active_tunnel", lambda tenant_id: True)
    monkeypatch.setattr(tunnel_mod, "_tunnel_tool_routes", {
        "tenant-1": {
            "filesystem__read_file": "fs",
            "filesystem__search_outputs": "fs",
        }
    })
    proxied_payload = {
        "outputs_dir": "/local/outputs",
        "query": "accuracy",
        "hits": [{"path": "/local/outputs/run_1/results.csv", "score": 1.0}],
        "total_indexed": 3,
    }
    call_tunnel_tool_mock = mock.AsyncMock(return_value={
        "content": [{"type": "text", "text": json.dumps(proxied_payload)}]
    })
    monkeypatch.setattr(tunnel_mod, "call_tunnel_tool", call_tunnel_tool_mock)
    monkeypatch.setattr(tunnel_mod, "list_tunnel_tools", mock.AsyncMock(return_value=[]))

    result = await mh._tunnel_proxy_outputs_tool(
        "tenant-1", "search_outputs",
        {"outputs_dir": "/local/outputs", "query": "accuracy"},
    )

    assert result == proxied_payload
    call_tunnel_tool_mock.assert_awaited_once_with(
        "tenant-1", "filesystem__search_outputs",
        {"outputs_dir": "/local/outputs", "query": "accuracy"},
    )


@pytest.mark.asyncio
async def test_proxy_returns_none_on_tunnel_call_error(monkeypatch):
    """_tunnel_proxy_outputs_tool swallows tunnel errors and returns None
    so the caller can fall through to the honest hosted-mode error."""
    monkeypatch.setattr(tunnel_mod, "has_active_tunnel", lambda tenant_id: True)
    monkeypatch.setattr(tunnel_mod, "_tunnel_tool_routes", {
        "tenant-1": {"filesystem__search_outputs": "fs"}
    })
    monkeypatch.setattr(
        tunnel_mod, "call_tunnel_tool",
        mock.AsyncMock(side_effect=RuntimeError("tunnel timeout")),
    )
    monkeypatch.setattr(tunnel_mod, "list_tunnel_tools", mock.AsyncMock(return_value=[]))

    result = await mh._tunnel_proxy_outputs_tool(
        "tenant-1", "search_outputs",
        {"outputs_dir": "/local/outputs", "query": "q"},
    )
    assert result is None


@pytest.mark.asyncio
async def test_proxy_triggers_list_tunnel_tools_when_cache_cold(monkeypatch):
    """_tunnel_proxy_outputs_tool re-discovers tools via list_tunnel_tools when
    the routing cache is cold (no entry for this tenant), then retries the lookup."""
    monkeypatch.setattr(tunnel_mod, "has_active_tunnel", lambda tenant_id: True)

    # Start with a reference to the real routes dict so we can mutate it
    fake_routes: dict = {}
    monkeypatch.setattr(tunnel_mod, "_tunnel_tool_routes", fake_routes)

    proxied_payload = {"hits": [], "total_indexed": 0, "outputs_dir": "/x", "query": "q"}
    call_mock = mock.AsyncMock(return_value={
        "content": [{"type": "text", "text": json.dumps(proxied_payload)}]
    })
    monkeypatch.setattr(tunnel_mod, "call_tunnel_tool", call_mock)

    async def _populate_cache(tenant_id):
        fake_routes[tenant_id] = {"filesystem__search_outputs": "fs"}
        return []

    list_mock = mock.AsyncMock(side_effect=_populate_cache)
    monkeypatch.setattr(tunnel_mod, "list_tunnel_tools", list_mock)

    result = await mh._tunnel_proxy_outputs_tool(
        "tenant-1", "search_outputs",
        {"outputs_dir": "/x", "query": "q"},
    )

    assert result == proxied_payload
    list_mock.assert_awaited_once_with("tenant-1")


# ---------------------------------------------------------------------------
# Integration-level tests via _handle_outputs_tools directly
# ---------------------------------------------------------------------------

def _run_outputs_call(name: str, arguments: dict, hosted: bool = True,
                      tenant: "dict | None" = None) -> dict:
    """Call _handle_outputs_tools directly with controlled hosted_mode and tenant."""
    with mock.patch("meridian.mcp.handler._hosted_mode", return_value=hosted):
        return _run(mh._handle_outputs_tools(
            name, arguments,
            db=None, data_dir="/tmp",
            tenant=tenant, _mcp_tenant_id=None,
        ))


def test_search_outputs_hosted_no_tenant_returns_honest_error_tunnel_tried_false():
    """1365e01a — hosted mode with no tenant (unauthenticated) falls through to
    honest error immediately, with tunnel_tried=False (no tenant to probe)."""
    result = _run_outputs_call(
        "search_outputs",
        {"outputs_dir": "/local/outputs", "query": "accuracy"},
        hosted=True,
        tenant=None,
    )
    assert result.get("hosted") is True
    assert result.get("tunnel_tried") is False
    assert result["hits"] == []
    assert "cannot run on hosted Meridian" in result["error"]


def test_search_outputs_hosted_with_tenant_no_tunnel_returns_honest_error_tunnel_tried_true(monkeypatch):
    """1365e01a — hosted mode with a tenant but no active tunnel → honest error
    with tunnel_tried=True (we tried, but there was nothing to route to)."""
    monkeypatch.setattr(tunnel_mod, "has_active_tunnel", lambda tenant_id: False)
    monkeypatch.setattr(tunnel_mod, "_tunnel_tool_routes", {})

    result = _run_outputs_call(
        "search_outputs",
        {"outputs_dir": "/local/outputs", "query": "accuracy"},
        hosted=True,
        tenant={"id": "tenant-abc"},
    )

    assert result.get("hosted") is True
    assert result.get("tunnel_tried") is True
    assert result["hits"] == []
    assert "cannot run on hosted Meridian" in result["error"]


def test_search_outputs_hosted_with_tunnel_proxies_successfully(monkeypatch):
    """1365e01a — hosted mode with active tunnel exposing search_outputs → call
    is proxied and the decoded JSON dict is returned (not the honest error)."""
    monkeypatch.setattr(tunnel_mod, "has_active_tunnel", lambda tenant_id: True)
    monkeypatch.setattr(tunnel_mod, "_tunnel_tool_routes", {
        "tenant-abc": {"filesystem__search_outputs": "fs"}
    })
    proxied = {
        "outputs_dir": "/local/outputs",
        "query": "accuracy",
        "hits": [{"path": "/local/outputs/run_1/metrics.csv", "score": 0.9}],
        "total_indexed": 5,
    }
    monkeypatch.setattr(
        tunnel_mod, "call_tunnel_tool",
        mock.AsyncMock(return_value={
            "content": [{"type": "text", "text": json.dumps(proxied)}]
        }),
    )
    monkeypatch.setattr(tunnel_mod, "list_tunnel_tools", mock.AsyncMock(return_value=[]))

    result = _run_outputs_call(
        "search_outputs",
        {"outputs_dir": "/local/outputs", "query": "accuracy"},
        hosted=True,
        tenant={"id": "tenant-abc"},
    )

    # Should be the proxied result, not the honest error
    assert result == proxied
    assert "error" not in result
    assert result["hits"][0]["path"] == "/local/outputs/run_1/metrics.csv"


def test_annotate_outputs_hosted_with_tunnel_proxies_successfully(monkeypatch):
    """1365e01a — hosted mode with active tunnel exposing annotate_outputs → call
    is proxied and the decoded JSON dict is returned."""
    monkeypatch.setattr(tunnel_mod, "has_active_tunnel", lambda tenant_id: True)
    monkeypatch.setattr(tunnel_mod, "_tunnel_tool_routes", {
        "tenant-abc": {"filesystem__annotate_outputs": "fs"}
    })
    proxied = {
        "path": "/local/outputs/run_1",
        "note": "PCA on, BFS off",
        "created_at": "2026-07-15T00:00:00",
        "source": "tool",
    }
    monkeypatch.setattr(
        tunnel_mod, "call_tunnel_tool",
        mock.AsyncMock(return_value={
            "content": [{"type": "text", "text": json.dumps(proxied)}]
        }),
    )
    monkeypatch.setattr(tunnel_mod, "list_tunnel_tools", mock.AsyncMock(return_value=[]))

    result = _run_outputs_call(
        "annotate_outputs",
        {
            "outputs_dir": "/local/outputs",
            "path": "/local/outputs/run_1",
            "note": "PCA on, BFS off",
        },
        hosted=True,
        tenant={"id": "tenant-abc"},
    )

    assert result == proxied
    assert "error" not in result


def test_annotate_outputs_hosted_no_tunnel_returns_honest_error(monkeypatch):
    """1365e01a — hosted mode with no tunnel → annotate_outputs returns honest
    error, preserving 0dedff91 guard behaviour."""
    monkeypatch.setattr(tunnel_mod, "has_active_tunnel", lambda tenant_id: False)
    monkeypatch.setattr(tunnel_mod, "_tunnel_tool_routes", {})

    result = _run_outputs_call(
        "annotate_outputs",
        {
            "outputs_dir": "/local/outputs",
            "path": "/local/outputs/run_1",
            "note": "test note",
        },
        hosted=True,
        tenant={"id": "tenant-abc"},
    )

    assert result.get("hosted") is True
    assert "cannot run on hosted Meridian" in result["error"]


# ---------------------------------------------------------------------------
# Non-regression: arg guards still fire BEFORE the hosted-mode check
# ---------------------------------------------------------------------------

def test_search_outputs_missing_outputs_dir_raises_even_in_hosted_mode(monkeypatch):
    """Non-regression: outputs_dir required-arg guard fires before hosted-mode check."""
    monkeypatch.setattr("meridian.mcp.handler._hosted_mode", lambda: True)
    import meridian.db as db_module
    db = _run(db_module.init_db(":memory:"))
    resp = _run(mh._handle_mcp_request(
        _req("tools/call", {
            "name": "search_outputs",
            "arguments": {"query": "something"},
        }),
        db=db, data_dir="/tmp",
    ))
    assert resp["error"]["code"] == -32603
    assert "outputs_dir is required" in resp["error"]["message"]


def test_search_outputs_missing_query_raises_even_in_hosted_mode(monkeypatch):
    """Non-regression: query required-arg guard fires before hosted-mode check."""
    monkeypatch.setattr("meridian.mcp.handler._hosted_mode", lambda: True)
    import meridian.db as db_module
    db = _run(db_module.init_db(":memory:"))
    resp = _run(mh._handle_mcp_request(
        _req("tools/call", {
            "name": "search_outputs",
            "arguments": {"outputs_dir": "/some/dir"},
        }),
        db=db, data_dir="/tmp",
    ))
    assert resp["error"]["code"] == -32603
    assert "query is required" in resp["error"]["message"]


def test_search_outputs_hosted_mode_no_tenant_still_returns_honest_error(monkeypatch):
    """Non-regression: the 0dedff91 honest-error guard still fires in hosted mode
    when there is no tenant (unauthenticated call — _tunnel_proxy not called)."""
    monkeypatch.setattr("meridian.mcp.handler._hosted_mode", lambda: True)
    import meridian.db as db_module
    db = _run(db_module.init_db(":memory:"))
    resp = _run(mh._handle_mcp_request(
        _req("tools/call", {
            "name": "search_outputs",
            "arguments": {"outputs_dir": "/local/outputs", "query": "accuracy"},
        }),
        db=db, data_dir="/tmp",
        tenant=None,
    ))
    # Success envelope — the tool returns the error dict, not a JSON-RPC error
    assert "result" in resp
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload.get("hosted") is True
    assert payload.get("tunnel_tried") is False
    assert "cannot run on hosted Meridian" in payload["error"]
    assert payload["hits"] == []
