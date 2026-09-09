"""Tests for sprint item d17a437a -- bounded cross-MCP batch research fan-out
(the ``tunnel_research`` batch_read adapter: code-intel / meridian-docs /
meridian-outputs, plus tunnel/slot readiness diagnostics).

Covers:

1. ``tunnel_research.diagnostics`` -- degrades gracefully (no exception) when
   no tenant/session context is available; reports per-surface
   ``slot_connected`` by reading ``meridian.routes.tunnel``'s own live socket
   registries (via ``_label_maps``, the exact function
   ``build_tunnel_diagnostics`` itself uses) rather than a separate,
   independently-maintained check; groups routed tool names by surface using
   the SAME allowlist ``call`` enforces.
2. ``tunnel_research.call`` -- rejects an unrecognized or known-mutating tool
   name as VALIDATION_ERROR before ever attempting to dispatch it; reports
   NOT_FOUND (never a silent/blank success) when no tunnel is active, and
   again when the tool genuinely isn't exposed on any connected slot; on a
   real dispatch, REUSES ``meridian.mcp.handler._tunnel_proxy_outputs_tool``
   verbatim (proven by monkeypatching that exact function and asserting the
   adapter forwards tenant_id/tool/arguments to it unchanged) rather than a
   separately-implemented tunnel call path.
3. Engine-level ``tenant_id`` threading (``batch_read`` -> adapter
   operation): additive and backward-compatible -- a pre-existing 3-arg
   adapter operation (the ``sprint_board`` shape) is dispatched unchanged
   even when ``batch_read()`` is called with a ``tenant_id``; a NEW operation
   that opts in via a ``tenant_id`` keyword parameter receives the exact
   value ``batch_read()`` was called with, never something request-supplied
   (``args`` cannot spoof it).
4. Registration: ``tunnel_research`` sits in ``batch_read.DEFAULT_ADAPTERS``
   next to ``sprint_board``/``profile`` -- the same registry, not a parallel
   mechanism -- and the ``batch_read`` MCP tool's own description mentions it.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

import meridian.server as server_module  # noqa: F401 -- load before mcp.handler (import-cycle guard)
from meridian import batch_read as br_module
from meridian import db as db_module
from meridian import mcp_tools


@pytest_asyncio.fixture
async def project(db):
    return await db_module.create_project(db, "tunnel-research-test-proj")


# ---------------------------------------------------------------------------
# Registration -- same registry as sprint_board/profile, not a parallel one.
# ---------------------------------------------------------------------------

def test_tunnel_research_registered_in_default_adapters():
    assert "tunnel_research" in br_module.DEFAULT_ADAPTERS
    ops = br_module.DEFAULT_ADAPTERS["tunnel_research"]
    assert set(ops) == {"diagnostics", "call"}


def test_batch_read_tool_description_mentions_tunnel_research():
    tool = next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == "batch_read")
    assert "tunnel_research" in tool["description"]
    # the schema itself is unchanged -- new adapters don't need new schema fields.
    assert tool["inputSchema"]["required"] == ["requests"]


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_diagnostics_degrades_gracefully_with_no_tenant_context(db, project):
    requests = [
        {"request_id": "diag", "adapter": "tunnel_research", "operation": "diagnostics"},
    ]
    resp = await br_module.batch_read(db, project_id=project["id"], requests=requests)
    by_id = {r["request_id"]: r for r in resp["results"]}
    assert by_id["diag"]["status"] == "ok"
    result = by_id["diag"]["result"]
    assert result["tenant_id"] is None
    assert result["tunnel_active"] is False
    assert set(result["surfaces"]) == {"code", "docs", "outputs"}
    assert all(v["slot_connected"] is False for v in result["surfaces"].values())
    assert result["routed_tools"] == {"code": [], "docs": [], "outputs": []}
    assert "reason" in result


@pytest.mark.asyncio
async def test_diagnostics_reports_connected_slots_and_routed_tools(db, project, monkeypatch):
    from meridian.routes import tunnel as tunnel_mod

    tenant_id = "tenant-diag-1"
    fake_ws = object()
    monkeypatch.setitem(tunnel_mod._tunnel_outputs_sockets, tenant_id, fake_ws)
    monkeypatch.setitem(
        tunnel_mod._tunnel_tool_routes, tenant_id,
        {"outputs__search_outputs": "outputs", "filesystem__read_file": "fs"},
    )
    try:
        requests = [
            {"request_id": "diag", "adapter": "tunnel_research", "operation": "diagnostics"},
        ]
        resp = await br_module.batch_read(
            db, project_id=project["id"], requests=requests, tenant_id=tenant_id,
        )
        result = resp["results"][0]["result"]
        assert result["tenant_id"] == tenant_id
        assert result["tunnel_active"] is True
        assert result["surfaces"]["outputs"]["slot_connected"] is True
        assert result["surfaces"]["code"]["slot_connected"] is False
        assert result["surfaces"]["docs"]["slot_connected"] is False
        # search_outputs is on the allowlist -> grouped under "outputs";
        # read_file is NOT a research-surface tool -> not grouped anywhere.
        assert result["routed_tools"]["outputs"] == ["search_outputs"]
        assert result["routed_tools"]["code"] == []
        assert result["routed_tools"]["docs"] == []
    finally:
        tunnel_mod._tunnel_outputs_sockets.pop(tenant_id, None)
        tunnel_mod._tunnel_tool_routes.pop(tenant_id, None)


@pytest.mark.asyncio
async def test_diagnostics_refresh_calls_list_tunnel_tools(db, project, monkeypatch):
    from meridian.routes import tunnel as tunnel_mod

    tenant_id = "tenant-diag-refresh"
    called = {"n": 0}

    async def _fake_list_tunnel_tools(tid):
        called["n"] += 1
        assert tid == tenant_id
        return []

    monkeypatch.setattr(tunnel_mod, "list_tunnel_tools", _fake_list_tunnel_tools)
    requests = [
        {"request_id": "diag", "adapter": "tunnel_research", "operation": "diagnostics",
         "args": {"refresh": True}},
    ]
    resp = await br_module.batch_read(
        db, project_id=project["id"], requests=requests, tenant_id=tenant_id,
    )
    assert resp["results"][0]["status"] == "ok"
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_diagnostics_refresh_failure_is_swallowed(db, project, monkeypatch):
    """A broken discovery refresh must never break the whole diagnostics read."""
    from meridian.routes import tunnel as tunnel_mod

    async def _boom(tid):
        raise RuntimeError("tunnel discovery boom")

    monkeypatch.setattr(tunnel_mod, "list_tunnel_tools", _boom)
    requests = [
        {"request_id": "diag", "adapter": "tunnel_research", "operation": "diagnostics",
         "args": {"refresh": True}},
    ]
    resp = await br_module.batch_read(
        db, project_id=project["id"], requests=requests, tenant_id="tenant-x",
    )
    assert resp["results"][0]["status"] == "ok"


# ---------------------------------------------------------------------------
# call -- allowlist gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_rejects_unknown_tool_as_validation_error(db, project):
    requests = [
        {"request_id": "c", "adapter": "tunnel_research", "operation": "call",
         "args": {"tool": "not_a_real_tool"}, "tenant_id": "irrelevant"},
    ]
    resp = await br_module.batch_read(
        db, project_id=project["id"], requests=requests, tenant_id="tenant-x",
    )
    result = resp["results"][0]
    assert result["status"] == "error"
    assert result["error_code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_call_rejects_known_mutating_tool_as_validation_error(db, project):
    """annotate_outputs is a real, known tool on the outputs surface -- but it
    mutates, so it must never be reachable through this READ-ONLY adapter,
    even though it IS wired to _tunnel_proxy_outputs_tool for its own,
    unrelated top-level MCP tool."""
    requests = [
        {"request_id": "c", "adapter": "tunnel_research", "operation": "call",
         "args": {"tool": "annotate_outputs", "arguments": {}}},
    ]
    resp = await br_module.batch_read(
        db, project_id=project["id"], requests=requests, tenant_id="tenant-x",
    )
    result = resp["results"][0]
    assert result["status"] == "error"
    assert result["error_code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_call_bad_arguments_shape_is_validation_error(db, project):
    requests = [
        {"request_id": "c", "adapter": "tunnel_research", "operation": "call",
         "args": {"tool": "search_outputs", "arguments": "not-a-dict"}},
    ]
    resp = await br_module.batch_read(
        db, project_id=project["id"], requests=requests, tenant_id="tenant-x",
    )
    assert resp["results"][0]["error_code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# call -- readiness gating (never dispatch blind)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_with_no_tenant_context_is_not_found(db, project):
    requests = [
        {"request_id": "c", "adapter": "tunnel_research", "operation": "call",
         "args": {"tool": "search_outputs", "arguments": {}}},
    ]
    resp = await br_module.batch_read(db, project_id=project["id"], requests=requests)
    result = resp["results"][0]
    assert result["status"] == "error"
    assert result["error_code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_call_with_no_active_tunnel_is_not_found(db, project):
    requests = [
        {"request_id": "c", "adapter": "tunnel_research", "operation": "call",
         "args": {"tool": "search_outputs", "arguments": {}}},
    ]
    resp = await br_module.batch_read(
        db, project_id=project["id"], requests=requests, tenant_id="tenant-never-connected",
    )
    result = resp["results"][0]
    assert result["status"] == "error"
    assert result["error_code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_call_tool_not_on_any_connected_slot_is_not_found(db, project, monkeypatch):
    from meridian.routes import tunnel as tunnel_mod

    tenant_id = "tenant-connected-but-no-match"
    monkeypatch.setitem(tunnel_mod._tunnel_outputs_sockets, tenant_id, object())

    # The socket is connected but the routing cache has no entry for this
    # tenant yet -- _tunnel_proxy_outputs_tool's own real behavior is to
    # trigger a fresh discovery pass (list_tunnel_tools) before giving up.
    # Stub it to a no-op (routing cache stays empty) rather than let it try
    # to actually talk to our fake, non-functional `object()` socket.
    async def _fake_list_tunnel_tools(tid):
        return []

    monkeypatch.setattr(tunnel_mod, "list_tunnel_tools", _fake_list_tunnel_tools)
    try:
        requests = [
            {"request_id": "c", "adapter": "tunnel_research", "operation": "call",
             "args": {"tool": "search_outputs", "arguments": {}}},
        ]
        resp = await br_module.batch_read(
            db, project_id=project["id"], requests=requests, tenant_id=tenant_id,
        )
        result = resp["results"][0]
        assert result["status"] == "error"
        assert result["error_code"] == "NOT_FOUND"
    finally:
        tunnel_mod._tunnel_outputs_sockets.pop(tenant_id, None)


# ---------------------------------------------------------------------------
# call -- real dispatch reuses meridian.mcp.handler._tunnel_proxy_outputs_tool
# verbatim, never a separately re-implemented tunnel call path.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_dispatches_via_existing_tunnel_proxy_mechanism(db, project, monkeypatch):
    import meridian.server as _server_module  # noqa: F401 -- load before mcp.handler (import-cycle guard)
    from meridian.mcp import handler as handler_module
    from meridian.routes import tunnel as tunnel_mod

    tenant_id = "tenant-real-dispatch"
    monkeypatch.setitem(tunnel_mod._tunnel_outputs_sockets, tenant_id, object())

    seen_calls = []

    async def _fake_proxy(tid, bare_name, arguments):
        seen_calls.append((tid, bare_name, arguments))
        return {"hits": ["fake-hit"], "outputs_dir": arguments.get("outputs_dir")}

    monkeypatch.setattr(handler_module, "_tunnel_proxy_outputs_tool", _fake_proxy)
    try:
        requests = [
            {"request_id": "c", "adapter": "tunnel_research", "operation": "call",
             "args": {"tool": "search_outputs", "arguments": {"outputs_dir": "/tmp/out", "query": "loss curve"}}},
        ]
        resp = await br_module.batch_read(
            db, project_id=project["id"], requests=requests, tenant_id=tenant_id,
        )
        result = resp["results"][0]
        assert result["status"] == "ok"
        assert result["result"] == {"hits": ["fake-hit"], "outputs_dir": "/tmp/out"}
        assert seen_calls == [
            (tenant_id, "search_outputs", {"outputs_dir": "/tmp/out", "query": "loss curve"}),
        ]
    finally:
        tunnel_mod._tunnel_outputs_sockets.pop(tenant_id, None)


@pytest.mark.asyncio
async def test_call_defaults_missing_arguments_to_empty_dict(db, project, monkeypatch):
    import meridian.server as _server_module  # noqa: F401
    from meridian.mcp import handler as handler_module
    from meridian.routes import tunnel as tunnel_mod

    tenant_id = "tenant-default-args"
    monkeypatch.setitem(tunnel_mod._tunnel_code_sockets, tenant_id, object())

    async def _fake_proxy(tid, bare_name, arguments):
        assert arguments == {}
        return {"ok": True}

    monkeypatch.setattr(handler_module, "_tunnel_proxy_outputs_tool", _fake_proxy)
    try:
        requests = [
            {"request_id": "c", "adapter": "tunnel_research", "operation": "call",
             "args": {"tool": "search_graph"}},
        ]
        resp = await br_module.batch_read(
            db, project_id=project["id"], requests=requests, tenant_id=tenant_id,
        )
        assert resp["results"][0]["status"] == "ok"
    finally:
        tunnel_mod._tunnel_code_sockets.pop(tenant_id, None)


# ---------------------------------------------------------------------------
# engine-level tenant_id threading: additive, backward-compatible.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_legacy_three_arg_adapter_unaffected_by_tenant_id(db, project):
    """A pre-existing 3-arg operation (the sprint_board/profile shape) must
    dispatch exactly as before even when batch_read() is called with a
    tenant_id -- _accepts_tenant_id must not force tenant_id onto an
    operation that never declared it."""
    seen = []

    async def _legacy_op(db, project_id, args):
        seen.append((project_id, args))
        return {"ok": True}

    registry = {"legacy": {"op": _legacy_op}}
    requests = [{"request_id": "r1", "adapter": "legacy", "operation": "op", "args": {"x": 1}}]
    resp = await br_module.batch_read(
        db, project_id=project["id"], requests=requests, adapters=registry, tenant_id="tenant-y",
    )
    assert resp["results"][0]["status"] == "ok"
    assert seen == [(project["id"], {"x": 1})]


@pytest.mark.asyncio
async def test_new_style_operation_receives_engine_tenant_id_not_request_supplied(db, project):
    """tenant_id must come from batch_read()'s OWN call-level argument (the
    authenticated MCP session's tenant), never from a request's own 'args'
    -- a request cannot spoof a different tenant_id for itself."""
    received = []

    async def _tenant_aware_op(db, project_id, args, *, tenant_id=None):
        received.append(tenant_id)
        return {"tenant_id_seen": tenant_id}

    registry = {"aware": {"op": _tenant_aware_op}}
    requests = [
        {"request_id": "r1", "adapter": "aware", "operation": "op",
         "args": {"tenant_id": "spoofed-tenant"}},
    ]
    resp = await br_module.batch_read(
        db, project_id=project["id"], requests=requests, adapters=registry,
        tenant_id="real-authenticated-tenant",
    )
    result = resp["results"][0]
    assert result["status"] == "ok"
    assert result["result"] == {"tenant_id_seen": "real-authenticated-tenant"}
    assert received == ["real-authenticated-tenant"]


@pytest.mark.asyncio
async def test_new_style_operation_gets_none_tenant_id_when_call_omits_it(db, project):
    async def _tenant_aware_op(db, project_id, args, *, tenant_id=None):
        return {"tenant_id_seen": tenant_id}

    registry = {"aware": {"op": _tenant_aware_op}}
    requests = [{"request_id": "r1", "adapter": "aware", "operation": "op"}]
    resp = await br_module.batch_read(db, project_id=project["id"], requests=requests, adapters=registry)
    assert resp["results"][0]["result"] == {"tenant_id_seen": None}
