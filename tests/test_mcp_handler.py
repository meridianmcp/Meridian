"""Tests for the generation-aware tools/list manifest (sprint item 49d8244d) —
the meridian/mcp/handler.py half.

Covers:
  * ``tools/list`` attaches ``_meta["meridian/toolManifest"]`` with the
    existing ``revision``/``count`` fields PLUS (when a tenant is present) a
    nested ``tunnel`` object carrying manifest_hash/config_generation/
    slot_health/generated_at/age_seconds from
    ``routes.tunnel.tunnel_manifest_snapshot``.
  * The ``refresh_tool_manifest`` MCP tool now also forces a synchronous
    tunnel re-aggregation (via ``routes.tunnel.refresh_tunnel_manifest``) when
    a tenant is connected, bounded to an outer 5s timeout, degrading to the
    last-known snapshot on timeout/failure without ever breaking the call.
  * Concurrent ``tools/list`` calls for the same tenant stay consistent.

Pure in-process unit tests (no real WebSocket/network) — mirrors
tests/test_tunnel_bridge.py's style for the handler/bridge boundary.
"""
from __future__ import annotations

import asyncio

import pytest

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian.mcp import handler as mh
from meridian.routes import tunnel as tn


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset per-process tunnel/manifest registries around each test."""
    def _reset():
        tn._tunnel_sockets.clear()
        tn._slot_health.clear()
        tn._slot_status_detail.clear()
        tn._tunnel_tool_routes.clear()
        tn._tunnel_manifest_generated_at.clear()
        tn._tools_list_changed_pending.clear()
    _reset()
    yield
    _reset()


def _list_request():
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


# ---------------------------------------------------------------------------
# tools/list — _meta["meridian/toolManifest"] shape
# ---------------------------------------------------------------------------

def test_tools_list_self_hosted_has_no_tunnel_submanifest():
    """No tenant (self-hosted/unauthenticated) → revision/count present, but
    no `tunnel` sub-object (nothing to snapshot without a tenant scope)."""
    resp = asyncio.run(mh._handle_mcp_request(_list_request(), db=None, data_dir="/tmp", tenant=None))
    meta = resp["result"]["_meta"]["meridian/toolManifest"]
    assert isinstance(meta["revision"], str) and len(meta["revision"]) == 64
    assert meta["count"] > 0
    assert "tunnel" not in meta


def test_tools_list_with_tenant_attaches_tunnel_submanifest(monkeypatch):
    tenant = {"id": "t-meta-1", "plan": "pro"}
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: True)

    async def fake_list_tunnel_tools(tid, reserved):
        return [{"name": "filesystem__read_file", "description": "d"}]

    monkeypatch.setattr(tn, "list_tunnel_tools", fake_list_tunnel_tools)

    resp = asyncio.run(mh._handle_mcp_request(_list_request(), db=None, data_dir="/tmp", tenant=tenant))
    meta = resp["result"]["_meta"]["meridian/toolManifest"]
    assert "tunnel" in meta
    tunnel_meta = meta["tunnel"]
    assert set(tunnel_meta) >= {
        "manifest_hash", "tool_count", "slot_health", "config_generation",
        "generated_at", "age_seconds", "list_changed_pending", "has_active_tunnel",
    }
    assert tunnel_meta["has_active_tunnel"] is True


def test_tools_list_no_active_tunnel_still_has_tunnel_submanifest_with_nulls(monkeypatch):
    """A tenant with NO active tunnel still gets a tunnel sub-object (from
    tunnel_manifest_snapshot's honest "nothing built yet" shape), not an
    absent key — so a client can always look at the same path."""
    tenant = {"id": "t-meta-2", "plan": "pro"}
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: False)
    monkeypatch.setattr(tn, "tunnel_cross_instance_miss", lambda tenant_: False)

    resp = asyncio.run(mh._handle_mcp_request(_list_request(), db=None, data_dir="/tmp", tenant=tenant))
    meta = resp["result"]["_meta"]["meridian/toolManifest"]
    assert meta["tunnel"]["manifest_hash"] is None
    assert meta["tunnel"]["has_active_tunnel"] is False


def test_tools_list_tunnel_health_meta_coexists_with_manifest_meta(monkeypatch):
    """The pre-existing tunnelHealth signal (7033c8e2) and the new manifest
    block (49d8244d) are independent `_meta` keys — neither displaces the other."""
    tenant = {"id": "t-meta-3", "plan": "pro"}
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: True)

    async def boom(tid, reserved):
        raise RuntimeError("socket reset")

    monkeypatch.setattr(tn, "list_tunnel_tools", boom)

    resp = asyncio.run(mh._handle_mcp_request(_list_request(), db=None, data_dir="/tmp", tenant=tenant))
    meta = resp["result"]["_meta"]
    assert meta["meridian/tunnelHealth"]["status"] == "error"
    assert "tunnel" in meta["meridian/toolManifest"]


def test_tools_list_revision_covers_full_aggregated_set(monkeypatch):
    """`revision` is computed over native+GitHub+tunnel tools together — a
    tunnel-only tool changes the revision even though native tools didn't."""
    tenant = {"id": "t-meta-4", "plan": "pro"}
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: True)

    async def fake_tools_a(tid, reserved):
        return [{"name": "filesystem__read_file", "description": "d"}]

    async def fake_tools_b(tid, reserved):
        return [{"name": "filesystem__read_file", "description": "d"},
                {"name": "filesystem__write_file", "description": "d2"}]

    monkeypatch.setattr(tn, "list_tunnel_tools", fake_tools_a)
    resp_a = asyncio.run(mh._handle_mcp_request(_list_request(), db=None, data_dir="/tmp", tenant=tenant))
    rev_a = resp_a["result"]["_meta"]["meridian/toolManifest"]["revision"]

    monkeypatch.setattr(tn, "list_tunnel_tools", fake_tools_b)
    resp_b = asyncio.run(mh._handle_mcp_request(_list_request(), db=None, data_dir="/tmp", tenant=tenant))
    rev_b = resp_b["result"]["_meta"]["meridian/toolManifest"]["revision"]

    assert rev_a != rev_b


# ---------------------------------------------------------------------------
# refresh_tool_manifest tool — extended to force a tunnel re-aggregation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_tool_manifest_self_hosted_unchanged(db):
    """No tenant → behaves exactly as before 49d8244d (no tunnel key at all)."""
    result = await mh._dispatch_mcp_tool("refresh_tool_manifest", {}, db, "/tmp")
    assert "tunnel" not in result
    assert "list_changed_refired" not in result


@pytest.mark.asyncio
async def test_refresh_tool_manifest_with_tenant_forces_tunnel_rebuild(db, monkeypatch):
    tenant = {"id": "t-refresh-1", "plan": "pro"}
    calls = {"n": 0}

    async def fake_fetch(tid, label):
        calls["n"] += 1
        if label == "fs":
            return label, [{"name": "read_file", "description": "d"}]
        return label, []

    monkeypatch.setattr(tn, "_fetch_slot_tools", fake_fetch)

    result = await mh._dispatch_mcp_tool("refresh_tool_manifest", {}, db, "/tmp", tenant=tenant)

    assert result["list_changed_refired"] is True
    assert result["tunnel"]["tool_count"] == 1
    assert result["tunnel"]["manifest_hash"] is not None
    # A real rebuild happened synchronously — every slot got fetched.
    assert calls["n"] == len(tn._TUNNEL_LABELS)


@pytest.mark.asyncio
async def test_refresh_tool_manifest_bounded_on_slow_tunnel(db, monkeypatch):
    """A wedged tunnel refresh must not hang the tool call — bounded to 5s,
    degrading to the last-known (possibly empty) snapshot."""
    tenant = {"id": "t-refresh-slow", "plan": "pro"}

    async def hangs_forever(tid, reserved_names=frozenset()):
        await asyncio.sleep(30)
        return {}

    monkeypatch.setattr(tn, "refresh_tunnel_manifest", hangs_forever)

    import time as _time
    start = _time.monotonic()
    result = await mh._dispatch_mcp_tool("refresh_tool_manifest", {}, db, "/tmp", tenant=tenant)
    elapsed = _time.monotonic() - start

    assert elapsed < 8.0, f"refresh_tool_manifest took {elapsed:.1f}s — outer timeout did not bound it"
    assert result["list_changed_refired"] is False
    assert "tunnel" in result  # degraded snapshot, not a missing key


@pytest.mark.asyncio
async def test_refresh_tool_manifest_survives_tunnel_module_exception(db, monkeypatch):
    tenant = {"id": "t-refresh-boom", "plan": "pro"}

    async def boom(tid, reserved_names=frozenset()):
        raise RuntimeError("tunnel module blew up")

    monkeypatch.setattr(tn, "refresh_tunnel_manifest", boom)

    result = await mh._dispatch_mcp_tool("refresh_tool_manifest", {}, db, "/tmp", tenant=tenant)
    # The manifest itself (native tools) is still returned — a tunnel hiccup
    # must never break the tool call.
    assert result["count"] > 0
    assert result["list_changed_refired"] is False


# ---------------------------------------------------------------------------
# Concurrent tools/list — two overlapping requests for the same tenant.
# ---------------------------------------------------------------------------

def test_concurrent_tools_list_requests_stay_consistent(monkeypatch):
    tenant = {"id": "t-concurrent-list", "plan": "pro"}
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: True)

    async def fake_list_tunnel_tools(tid, reserved):
        await asyncio.sleep(0)
        return [{"name": "filesystem__read_file", "description": "d"}]

    monkeypatch.setattr(tn, "list_tunnel_tools", fake_list_tunnel_tools)

    async def _run():
        return await asyncio.gather(
            mh._handle_mcp_request(_list_request(), db=None, data_dir="/tmp", tenant=tenant),
            mh._handle_mcp_request(_list_request(), db=None, data_dir="/tmp", tenant=tenant),
        )

    resp1, resp2 = asyncio.run(_run())
    names1 = {t["name"] for t in resp1["result"]["tools"]}
    names2 = {t["name"] for t in resp2["result"]["tools"]}
    assert "filesystem__read_file" in names1
    assert "filesystem__read_file" in names2
    rev1 = resp1["result"]["_meta"]["meridian/toolManifest"]["revision"]
    rev2 = resp2["result"]["_meta"]["meridian/toolManifest"]["revision"]
    assert rev1 == rev2  # same underlying tool set → same deterministic hash
