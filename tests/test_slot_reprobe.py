"""16e02240 — periodic re-probe for tunnel slots suppressed by _record_slot_health.

A slot marked unhealthy used to stay excluded from every tools/list until the
CLIENT sent a fresh healthy plugin_status. A transient hiccup with no follow-up
recovery report left the slot dark forever. These tests prove the optimistic
re-probe: a slot suppressed longer than MERIDIAN_SLOT_UNHEALTHY_TTL is treated
healthy again (so the next tools/list re-advertises it), while a freshly
suppressed one stays suppressed.
"""
from __future__ import annotations

import asyncio
import time

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian.routes import tunnel as tn


def _reset(tid: str) -> None:
    tn._slot_health.pop(tid, None)
    tn._slot_unhealthy_since.pop(tid, None)
    tn._slot_status_detail.pop(tid, None)
    tn._tools_list_changed_pending.discard(tid)


# ---------------------------------------------------------------------------
# _slot_unhealthy_ttl config
# ---------------------------------------------------------------------------

def test_ttl_default_and_override(monkeypatch):
    monkeypatch.delenv("MERIDIAN_SLOT_UNHEALTHY_TTL", raising=False)
    assert tn._slot_unhealthy_ttl() == 120.0
    monkeypatch.setenv("MERIDIAN_SLOT_UNHEALTHY_TTL", "30")
    assert tn._slot_unhealthy_ttl() == 30.0
    # Garbage falls back to the default rather than raising.
    monkeypatch.setenv("MERIDIAN_SLOT_UNHEALTHY_TTL", "not-a-number")
    assert tn._slot_unhealthy_ttl() == 120.0


# ---------------------------------------------------------------------------
# core re-probe behaviour
# ---------------------------------------------------------------------------

def test_freshly_suppressed_slot_stays_unhealthy(monkeypatch):
    """A just-marked-unhealthy slot is still suppressed while inside the TTL."""
    monkeypatch.setenv("MERIDIAN_SLOT_UNHEALTHY_TTL", "120")
    tid = "reprobe-fresh"
    try:
        tn._record_slot_health(tid, "code", False, reason="preflight failed")
        assert tn._slot_is_healthy(tid, "code") is False
    finally:
        _reset(tid)


def test_suppressed_slot_reprobed_healthy_after_ttl(monkeypatch):
    """Once the suppression is older than the TTL, the slot re-probes healthy."""
    monkeypatch.setenv("MERIDIAN_SLOT_UNHEALTHY_TTL", "120")
    tid = "reprobe-aged"
    try:
        tn._record_slot_health(tid, "code", False)
        assert tn._slot_is_healthy(tid, "code") is False
        # Backdate the suppression timestamp past the TTL window.
        tn._slot_unhealthy_since[tid]["code"] = time.monotonic() - 121.0
        assert tn._slot_is_healthy(tid, "code") is True
        # The recorded health bit is still False — this is an optimistic re-probe,
        # not a recovery report.
        assert tn._slot_health[tid]["code"] is False
    finally:
        _reset(tid)


def test_fresh_and_aged_slots_are_independent(monkeypatch):
    """An aged slot re-probes while a sibling freshly-suppressed slot stays dark."""
    monkeypatch.setenv("MERIDIAN_SLOT_UNHEALTHY_TTL", "120")
    tid = "reprobe-mixed"
    try:
        tn._record_slot_health(tid, "code", False)   # will be aged
        tn._record_slot_health(tid, "extract", False)  # stays fresh
        tn._slot_unhealthy_since[tid]["code"] = time.monotonic() - 200.0
        assert tn._slot_is_healthy(tid, "code") is True      # aged → re-probe
        assert tn._slot_is_healthy(tid, "extract") is False  # fresh → suppressed
    finally:
        _reset(tid)


def test_ttl_zero_disables_reprobe(monkeypatch):
    """TTL <= 0 restores the pre-16e02240 behaviour: suppressed until recovery."""
    monkeypatch.setenv("MERIDIAN_SLOT_UNHEALTHY_TTL", "0")
    tid = "reprobe-disabled"
    try:
        tn._record_slot_health(tid, "code", False)
        tn._slot_unhealthy_since[tid]["code"] = time.monotonic() - 10_000.0
        assert tn._slot_is_healthy(tid, "code") is False
    finally:
        _reset(tid)


def test_repeated_unhealthy_report_does_not_extend_window(monkeypatch):
    """A repeated unhealthy report must NOT keep pushing the re-probe out — else a
    chatty-but-broken slot would never re-probe. Only the first (healthy->unhealthy)
    transition stamps the timer."""
    monkeypatch.setenv("MERIDIAN_SLOT_UNHEALTHY_TTL", "120")
    tid = "reprobe-nostretch"
    try:
        tn._record_slot_health(tid, "code", False)
        # Backdate so we are already near the edge of the window.
        old = time.monotonic() - 119.0
        tn._slot_unhealthy_since[tid]["code"] = old
        # A second unhealthy report (still inside the window, so _slot_is_healthy is
        # False → was_unhealthy True) must not re-stamp the timer.
        tn._record_slot_health(tid, "code", False, reason="still broken")
        assert tn._slot_unhealthy_since[tid]["code"] == old
    finally:
        _reset(tid)


def test_recovery_clears_timestamp(monkeypatch):
    """An explicit healthy report drops the timestamp so a later unhealthy report
    starts a fresh window rather than inheriting a stale one."""
    monkeypatch.setenv("MERIDIAN_SLOT_UNHEALTHY_TTL", "120")
    tid = "reprobe-recovery"
    try:
        tn._record_slot_health(tid, "code", False)
        assert "code" in tn._slot_unhealthy_since.get(tid, {})
        tn._record_slot_health(tid, "code", True)
        assert "code" not in tn._slot_unhealthy_since.get(tid, {})
        assert tn._slot_is_healthy(tid, "code") is True
    finally:
        _reset(tid)


def test_clear_slot_health_drops_timestamp(monkeypatch):
    """Disconnect (WS close) clears the unhealthy-since timestamp too."""
    monkeypatch.setenv("MERIDIAN_SLOT_UNHEALTHY_TTL", "120")
    tid = "reprobe-clear"
    try:
        tn._record_slot_health(tid, "code", False)
        tn._record_slot_health(tid, "extract", False)
        tn._clear_slot_health(tid, "code")   # one slot
        assert "code" not in tn._slot_unhealthy_since.get(tid, {})
        assert "extract" in tn._slot_unhealthy_since.get(tid, {})
        tn._clear_slot_health(tid)           # all
        assert tid not in tn._slot_unhealthy_since
    finally:
        _reset(tid)


def _stub_proxy(monkeypatch, responder):
    """Patch tunnel._do_proxy with a responder(label, method, params) -> dict.

    Mirrors the helper in tests/test_tunnel_bridge.py so the aggregator can run
    without a real WebSocket."""
    import json

    from fastapi.responses import Response

    async def fake_do_proxy(tenant_id, method, path, query, headers, body, sockets, pending, label):
        req = json.loads(body.decode())
        result = responder(label, req["method"], req.get("params") or {})
        return Response(content=json.dumps(result).encode(), status_code=200,
                        media_type="application/json")

    monkeypatch.setattr(tn, "_do_proxy", fake_do_proxy)


# ---------------------------------------------------------------------------
# ddd46cc8 — optional SlotDiagnostics fields (state/retry_count/
# quarantine_reason) riding along on _record_slot_health, purely additive.
# ---------------------------------------------------------------------------

def test_record_slot_health_without_diagnostics_fields_keeps_old_shape(monkeypatch):
    """Every pre-existing caller omits state/retry_count/quarantine_reason —
    the stored dict must stay EXACTLY {reason, detail}, no extra keys."""
    tid = "reprobe-old-shape"
    try:
        tn._record_slot_health(tid, "code", False, reason="unreachable", detail="x")
        assert tn._slot_status_detail[tid]["code"] == {
            "reason": "unreachable", "detail": "x",
        }
    finally:
        _reset(tid)


def test_record_slot_health_stores_state_and_quarantine_reason(monkeypatch):
    tid = "reprobe-quarantine-fields"
    try:
        tn._record_slot_health(
            tid, "docs", False, reason="quarantined", detail="missing dependency",
            state="quarantined", retry_count=3, quarantine_reason="missing dependency",
        )
        entry = tn._slot_status_detail[tid]["docs"]
        assert entry["state"] == "quarantined"
        assert entry["retry_count"] == 3
        assert entry["quarantine_reason"] == "missing dependency"
    finally:
        _reset(tid)


def test_record_slot_health_diagnostics_fields_surface_via_tunnel_status(monkeypatch):
    tid = "reprobe-status-surface"
    tn._tunnel_sockets[tid] = object()
    try:
        tn._record_slot_health(
            tid, "docs", False, reason="quarantined", detail="missing dependency",
            state="quarantined", quarantine_reason="missing dependency",
        )
        status = asyncio.run(tn.tunnel_status(tid))
        assert status["slot_status"]["docs"]["state"] == "quarantined"
        assert status["slot_status"]["docs"]["quarantine_reason"] == "missing dependency"
    finally:
        _reset(tid)
        tn._tunnel_sockets.pop(tid, None)


def test_record_slot_health_healthy_report_clears_diagnostics_fields():
    """A recovery report (healthy=True) drops the whole diagnostic entry —
    including any state/quarantine_reason a prior unhealthy report stored —
    exactly as it already does for reason/detail."""
    tid = "reprobe-recovery-clears-state"
    try:
        tn._record_slot_health(
            tid, "docs", False, state="quarantined", quarantine_reason="x",
        )
        assert "docs" in tn._slot_status_detail.get(tid, {})
        tn._record_slot_health(tid, "docs", True)
        assert "docs" not in tn._slot_status_detail.get(tid, {})
    finally:
        _reset(tid)


def test_list_tunnel_tools_readvertises_after_ttl(monkeypatch):
    """End-to-end: a slot suppressed past the TTL is re-advertised by
    list_tunnel_tools; a freshly-suppressed one is not."""
    monkeypatch.setenv("MERIDIAN_SLOT_UNHEALTHY_TTL", "120")
    tid = "reprobe-list"
    tn._tunnel_sockets[tid] = object()
    tn._tunnel_code_sockets[tid] = object()

    def responder(label, method, params):
        if label == "fs":
            return {"result": {"tools": [{"name": "read_file"}]}}
        if label == "code":
            return {"result": {"tools": [{"name": "trace_path"}]}}
        return {"result": {"tools": []}}

    _stub_proxy(monkeypatch, responder)
    try:
        # fs freshly suppressed → excluded.
        tn._record_slot_health(tid, "fs", False)
        names = {t["name"] for t in asyncio.run(tn.list_tunnel_tools(tid))}
        assert "filesystem__read_file" not in names
        assert "codebase__trace_path" in names
        # Age fs past the TTL → re-advertised on the next aggregation.
        tn._slot_unhealthy_since[tid]["fs"] = time.monotonic() - 200.0
        tn._tunnel_tool_routes.pop(tid, None)
        names = {t["name"] for t in asyncio.run(tn.list_tunnel_tools(tid))}
        assert "filesystem__read_file" in names
    finally:
        _reset(tid)
        tn._tunnel_sockets.pop(tid, None)
        tn._tunnel_code_sockets.pop(tid, None)
        tn._tunnel_tool_routes.pop(tid, None)
