"""8047802f -- list_plugins must not misreport a busy-but-alive tunnel slot as
dead when its own live tools/list probe gets bulkhead-saturated by real traffic.

Bug report: list_plugins reported active=false/invocable=false/tool_count=0 for
desktop-commander ('dc') and meridian-outputs ('outputs') in a live session that
had just made dozens of successful dc:*/meridian-outputs:* tool calls with real
results -- the status field did not reflect ground truth (same bug class as
1a799e52 in a different subsystem).

Root cause: `_fetch_slot_tools` (routes/tunnel.py) issues its own tools/list
request through the exact same per-(slot, tenant) proxy path
(`_tunnel_jsonrpc` -> `_do_proxy`) as every real tools/call against that slot,
including the same in-flight bulkhead semaphore (`_slot_semaphore`, capped at
`MERIDIAN_MAX_SLOT_INFLIGHT`, default 8) and its 1s fail-fast acquire timeout.
A slot serving dozens of real, concurrent tool calls in the same session can
transiently saturate that bulkhead and reject THIS diagnostic probe -- the
busier (and healthier) the slot, the MORE likely the probe collapses to 0
tools. `list_plugins` treated "0 tools this fetch" as proof of death
(active=False/invocable=False/tool_count=0), which is wrong for a slot that is
demonstrably still being invoked successfully via the cached tool-routing
table (`_tunnel_tool_routes`) that real `call_tunnel_tool` dispatch reads from.

Fix: cross-check any slot whose live probe came back empty against
`_tunnel_tool_routes` for that tenant. If the underlying socket is still
connected AND the cache maps >=1 live tool name to that slot, trust that
ground truth (it is literally what powers real invocations) over a single
saturated probe instead of reporting the slot as dead. A slot with no cached
routes AND no live probe result is still correctly reported as dead, and a
slot whose socket has genuinely disconnected is never resurrected from a
stale cache entry.

Unit-level with mocks only -- no real socket/network/tunnel is touched.
"""
from __future__ import annotations

import pytest

# Import the full server package first so meridian.mcp.handler is loaded through
# its normal path (importing meridian.mcp.handler as the very first import trips a
# circular import with meridian.server). _dispatch_mcp_tool is imported per-test.
import meridian.server  # noqa: F401
from meridian.mcp.handler import _dispatch_mcp_tool
from meridian.routes import tunnel as tunnel_mod


_TENANT = {"id": "tenant-8047802f", "plan": "pro"}


@pytest.mark.asyncio
async def test_saturated_probe_does_not_mask_a_live_routed_slot(monkeypatch, db, tmp_path):
    """dc/outputs whose live tools/list probe returns 0 tools this fetch (the
    saturated-bulkhead scenario) are still reported active/invocable/with their
    real tool names when the routing cache proves real tool calls are actually
    being served on those slots right now."""
    tenant_id = _TENANT["id"]

    monkeypatch.setattr(tunnel_mod, "has_active_tunnel", lambda _tid: True)

    async def _fake_fetch_slot_tools(_tenant_id, label, *, budget=None):
        # Every slot's live probe comes back empty this call -- simulating every
        # slot momentarily failing to win the per-slot in-flight semaphore.
        return label, []

    monkeypatch.setattr(tunnel_mod, "_fetch_slot_tools", _fake_fetch_slot_tools)

    # The dc/outputs sockets are still genuinely connected (real traffic is
    # actively flowing through them) -- `_label_maps` membership is the only
    # thing the fallback checks, so a lightweight sentinel is enough.
    monkeypatch.setitem(tunnel_mod._tunnel_dc_sockets, tenant_id, object())
    monkeypatch.setitem(tunnel_mod._tunnel_outputs_sockets, tenant_id, object())

    # The routing cache real `call_tunnel_tool` dispatch reads from to route
    # actual invocations -- populated earlier this session and still current.
    monkeypatch.setitem(
        tunnel_mod._tunnel_tool_routes,
        tenant_id,
        {
            "desktop-commander__read_file": "dc",
            "desktop-commander__list_directory": "dc",
            "meridian-outputs__search_outputs": "outputs",
        },
    )

    result = await _dispatch_mcp_tool(
        "list_plugins", {}, db, str(tmp_path), tenant=_TENANT
    )
    by_slot = {p["slot"]: p for p in result["plugins"]}

    dc = by_slot["dc"]
    assert dc["active"] is True
    assert dc["invocable"] is True
    assert dc["tool_count"] == 2
    assert "desktop-commander__read_file" in dc["tools"]
    assert "desktop-commander__list_directory" in dc["tools"]

    outputs = by_slot["outputs"]
    assert outputs["active"] is True
    assert outputs["invocable"] is True
    assert outputs["tool_count"] == 1
    assert "meridian-outputs__search_outputs" in outputs["tools"]

    # A slot with no cached route entries at all (genuinely dark, e.g. fs here)
    # stays correctly reported as dead -- the fallback must not resurrect
    # every slot, only ones the routing cache actually vouches for.
    fs = by_slot["fs"]
    assert fs["active"] is False
    assert fs["invocable"] is False
    assert fs["tool_count"] == 0
    assert fs["tools"] == []


@pytest.mark.asyncio
async def test_live_probe_result_wins_over_cache_when_both_present(monkeypatch, db, tmp_path):
    """When the live probe DOES succeed for a slot, its result is used as-is --
    the cache fallback only fills in slots the live fetch left empty."""
    tenant_id = _TENANT["id"]

    monkeypatch.setattr(tunnel_mod, "has_active_tunnel", lambda _tid: True)

    async def _fake_fetch_slot_tools(_tenant_id, label, *, budget=None):
        if label == "dc":
            return label, [{"name": "read_file"}]  # live probe succeeds for dc
        return label, []

    monkeypatch.setattr(tunnel_mod, "_fetch_slot_tools", _fake_fetch_slot_tools)
    monkeypatch.setitem(tunnel_mod._tunnel_dc_sockets, tenant_id, object())

    # Stale/different cached route data for dc -- must be ignored since the
    # live probe already confirmed dc this fetch.
    monkeypatch.setitem(
        tunnel_mod._tunnel_tool_routes,
        tenant_id,
        {"desktop-commander__read_file": "dc", "desktop-commander__old_tool": "dc"},
    )

    result = await _dispatch_mcp_tool(
        "list_plugins", {}, db, str(tmp_path), tenant=_TENANT
    )
    dc = next(p for p in result["plugins"] if p["slot"] == "dc")
    assert dc["active"] is True
    assert dc["tool_count"] == 1
    assert dc["tools"] == ["desktop-commander__read_file"]


@pytest.mark.asyncio
async def test_stale_routes_for_a_disconnected_socket_are_not_resurrected(monkeypatch, db, tmp_path):
    """If the routing cache still has entries for a slot whose socket is
    actually gone (real disconnect), the fallback must NOT report it active --
    that would misreport a genuinely dead slot as alive."""
    tenant_id = _TENANT["id"]

    monkeypatch.setattr(tunnel_mod, "has_active_tunnel", lambda _tid: True)

    async def _fake_fetch_slot_tools(_tenant_id, label, *, budget=None):
        return label, []

    monkeypatch.setattr(tunnel_mod, "_fetch_slot_tools", _fake_fetch_slot_tools)

    # NOTE: no socket registered for 'dc' -- it has genuinely disconnected.
    monkeypatch.setitem(
        tunnel_mod._tunnel_tool_routes,
        tenant_id,
        {"desktop-commander__read_file": "dc"},
    )

    result = await _dispatch_mcp_tool(
        "list_plugins", {}, db, str(tmp_path), tenant=_TENANT
    )
    dc = next(p for p in result["plugins"] if p["slot"] == "dc")
    assert dc["active"] is False
    assert dc["invocable"] is False
    assert dc["tool_count"] == 0
    assert dc["tools"] == []
