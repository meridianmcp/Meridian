"""Fault-injection tests for the timeout + bulkhead hardening (1394edcd).

Adam's ask was to make the failure modes "basically completely solid" — which
means deliberately simulating the exact failures that happened in the 5116078b
incident and asserting the system fails fast + stays isolated, not just trusting
the guard code from a code review. Three injected faults:

1. A hanging filesystem walk behind ``search_outputs`` (the literal incident) —
   must fast-fail through the real MCP dispatch, never the ~4-minute silent hang.
2. A hung tunnel slot — must NOT block an independent slot (cross-slot isolation).
3. A saturated tunnel slot — must fast-fail with 503 instead of unbounded pile-up.
"""
from __future__ import annotations

import asyncio
import base64
import time

import pytest


# ---------------------------------------------------------------------------
# Fault 1 — a hanging in-process filesystem walk fails fast (e5f96adf)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hanging_search_outputs_fails_fast_not_minutes(tmp_path, monkeypatch):
    from meridian import server as srv
    from meridian import outputs_indexer as oi
    from meridian import hardening as h

    (tmp_path / "a.csv").write_text("x,y\n1,2\n", encoding="utf-8")

    def _hanging_walk(*_a, **_k):
        # Stand-in for the real pathological os.walk + hash + DuckDB build that
        # hung ~4 minutes. Bounded here so an orphaned bulkhead thread clears.
        time.sleep(3)
        return {"hits": [], "total_indexed": 0}

    monkeypatch.setattr(oi, "search_outputs", _hanging_walk)
    monkeypatch.setattr(h, "HEAVY_TOOL_TIMEOUT_SECONDS", 0.3)
    h._reset_for_tests()

    t0 = time.monotonic()
    result = await srv._dispatch_mcp_tool(
        "search_outputs",
        {"outputs_dir": str(tmp_path), "query": "x"},
        None,
        str(tmp_path),
    )
    elapsed = time.monotonic() - t0

    assert result["timed_out"] is True
    assert result["hits"] == []
    assert "error" in result
    assert elapsed < 2.0  # fast-fail — nowhere near the 3s hang (let alone 4 min)
    h._reset_for_tests()


# ---------------------------------------------------------------------------
# tunnel fakes
# ---------------------------------------------------------------------------

class _SilentWS:
    """Accepts the request but never resolves the future — a wedged backend."""

    async def send_json(self, payload):
        return None


class _InlineWS:
    """Resolves the pending future inline — a healthy, responsive backend."""

    def __init__(self, pending, response):
        self._pending = pending
        self._response = response

    async def send_json(self, payload):
        fut = self._pending.get(payload["id"])
        if fut is not None and not fut.done():
            fut.set_result({**self._response, "id": payload["id"]})


_OK_RESPONSE = {
    "status": 200,
    "headers": {"content-type": "application/json"},
    "body": base64.b64encode(b'{"ok":1}').decode(),
}


# ---------------------------------------------------------------------------
# Fault 2 — a hung slot does not block an independent slot (1d021501)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hung_tunnel_slot_does_not_block_other_slot(monkeypatch):
    from meridian.routes import tunnel as tn

    monkeypatch.setattr(tn, "_PROXY_TIMEOUT", 0.4)
    tn._slot_inflight.clear()
    tn._pending_reqs.clear()
    tn._pending_code_reqs.clear()
    tn._tunnel_sockets["t1"] = _SilentWS()  # fs slot: hangs
    tn._tunnel_code_sockets["t1"] = _InlineWS(tn._pending_code_reqs, _OK_RESPONSE)
    try:
        fs_task = asyncio.ensure_future(tn._do_proxy(
            "t1", "POST", "/mcp", "", {}, b"x",
            tn._tunnel_sockets, tn._pending_reqs, "fs",
        ))
        code_task = asyncio.ensure_future(tn._do_proxy(
            "t1", "POST", "/mcp", "", {}, b"x",
            tn._tunnel_code_sockets, tn._pending_code_reqs, "code",
        ))
        # The healthy 'code' slot returns FAST while 'fs' is still mid-hang —
        # proof the two slots don't share a blocking resource.
        code_resp = await asyncio.wait_for(code_task, timeout=0.15)
        assert code_resp.status_code == 200
        # 'fs' eventually times out on its own — an isolated, contained failure.
        fs_resp = await fs_task
        assert fs_resp.status_code == 504
    finally:
        tn._tunnel_sockets.clear()
        tn._tunnel_code_sockets.clear()
        tn._pending_reqs.clear()
        tn._pending_code_reqs.clear()
        tn._slot_inflight.clear()


# ---------------------------------------------------------------------------
# Fault 3 — a saturated slot fast-fails with 503, blast radius contained (1d021501)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_saturated_slot_fails_fast_with_503(monkeypatch):
    from meridian.routes import tunnel as tn

    monkeypatch.setattr(tn, "_max_slot_inflight", lambda: 1)
    monkeypatch.setattr(tn, "_SLOT_ACQUIRE_TIMEOUT", 0.15)
    monkeypatch.setattr(tn, "_PROXY_TIMEOUT", 3.0)
    tn._slot_inflight.clear()
    tn._pending_reqs.clear()
    tn._tunnel_sockets["t1"] = _SilentWS()  # holds the single permit, never frees it
    hung = None
    try:
        hung = asyncio.ensure_future(tn._do_proxy(
            "t1", "POST", "/mcp", "", {}, b"x",
            tn._tunnel_sockets, tn._pending_reqs, "fs",
        ))
        await asyncio.sleep(0.05)  # let it acquire the sole in-flight permit

        t0 = time.monotonic()
        resp2 = await tn._do_proxy(
            "t1", "POST", "/mcp", "", {}, b"x",
            tn._tunnel_sockets, tn._pending_reqs, "fs",
        )
        elapsed = time.monotonic() - t0

        assert resp2.status_code == 503
        assert b"saturated" in resp2.body
        assert elapsed < 1.0  # bounded by _SLOT_ACQUIRE_TIMEOUT, not _PROXY_TIMEOUT
    finally:
        if hung is not None:
            hung.cancel()
            await asyncio.gather(hung, return_exceptions=True)
        tn._tunnel_sockets.clear()
        tn._pending_reqs.clear()
        tn._slot_inflight.clear()
