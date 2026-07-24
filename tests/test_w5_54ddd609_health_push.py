"""54ddd609 — slot-health RECOVERY must reach an already-connected MCP session.

Bug: when a suppressed tunnel slot recovers (client sends a healthy
``plugin_status`` after an unhealthy one), the server updated ``_slot_health`` but
nothing invalidated the cached tool list / signalled the standard MCP
``notifications/tools/list_changed`` — so a claude.ai session that cached the old
(failed / empty) ``tools/list`` kept the recovered slot's tools invisible until a
full tunnel reconnect.

Fix (server-side, meridian/routes/tunnel.py): a RECOVERY plugin_status
(unhealthy->healthy transition) drops ``_tunnel_tool_routes`` for the tenant and
marks a pending tools/list_changed signal (``notify_tools_list_changed`` /
``consume_tools_list_changed``). The ``/mcp`` transport is stateless (no
server->session push channel), so cache invalidation + a drainable marker is the
smallest correct thing: the recovered tools reappear on the very next tools/list.

These are pure in-process unit tests — no server, ports, network, or sleeps.
"""
from __future__ import annotations

import asyncio

import pytest

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian.routes import tunnel as tn


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset the per-process health / routing / pending registries around each test."""
    def _reset():
        tn._slot_health.clear()
        tn._slot_status_detail.clear()
        tn._tunnel_tool_routes.clear()
        tn._tools_list_changed_pending.clear()
    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# Core recovery-transition behaviour (unhealthy -> healthy)
# ---------------------------------------------------------------------------

def test_recovery_transition_marks_list_changed_and_invalidates_cache():
    """unhealthy -> healthy fires notify_tools_list_changed: cache dropped + marker set."""
    tid = "t-recover"
    # Seed a stale routing cache (as if a prior tools/list ran while the slot was up).
    tn._tunnel_tool_routes[tid] = {"desktop-commander__read_file": "dc"}

    # Slot goes unhealthy (suppressed) — NOT a recovery, so no signal yet.
    tn._record_slot_health(tid, "dc", False, reason="preflight failed", detail="port 8813")
    assert tn._slot_is_healthy(tid, "dc") is False
    assert tid not in tn._tools_list_changed_pending

    # Now the RECOVERY report: unhealthy -> healthy.
    tn._record_slot_health(tid, "dc", True)

    # Standard MCP "tools list changed" signal is pending for this tenant...
    assert tid in tn._tools_list_changed_pending
    # ...and the stale routing cache was invalidated so the next call re-discovers.
    assert tid not in tn._tunnel_tool_routes
    # Health flipped back and the diagnostic cleared (existing 9a8645c1 behaviour).
    assert tn._slot_is_healthy(tid, "dc") is True
    assert "dc" not in tn._slot_status_detail.get(tid, {})


def test_non_recovery_healthy_report_does_not_signal():
    """A healthy report when the slot was ALREADY healthy is not a recovery — no signal.

    (Also covers the very first report a client sends for a slot: default is
    assumed-healthy, so healthy->healthy must not spuriously fire list_changed.)"""
    tid = "t-steady"
    tn._tunnel_tool_routes[tid] = {"filesystem__read_file": "fs"}

    # First-ever report is healthy: default assumed-healthy => healthy->healthy.
    tn._record_slot_health(tid, "fs", True)
    assert tid not in tn._tools_list_changed_pending
    # A second healthy report changes nothing either.
    tn._record_slot_health(tid, "fs", True)
    assert tid not in tn._tools_list_changed_pending
    # No recovery => the routing cache is left intact (no needless invalidation).
    assert tn._tunnel_tool_routes.get(tid) == {"filesystem__read_file": "fs"}


def test_unhealthy_report_does_not_signal():
    """A healthy -> unhealthy transition (a slot dying) is not a recovery."""
    tid = "t-die"
    tn._record_slot_health(tid, "extract", True)   # healthy baseline
    tn._tunnel_tool_routes[tid] = {"extractor__find_symbol": "extract"}
    tn._record_slot_health(tid, "extract", False, reason="access_denied", detail="x")
    assert tid not in tn._tools_list_changed_pending
    # Cache is NOT dropped by going unhealthy (list_tunnel_tools already filters it).
    assert tn._tunnel_tool_routes.get(tid) == {"extractor__find_symbol": "extract"}
    assert tn._slot_is_healthy(tid, "extract") is False


def test_empty_slot_label_is_ignored():
    """A blank slot label is a no-op and never signals (guards the early return)."""
    tid = "t-blank"
    tn._record_slot_health(tid, "", True)
    assert tid not in tn._tools_list_changed_pending
    assert tid not in tn._slot_health


# ---------------------------------------------------------------------------
# consume_tools_list_changed — fire-once drain semantics
# ---------------------------------------------------------------------------

def test_consume_is_true_once_then_false():
    tid = "t-consume"
    tn._record_slot_health(tid, "dc", False)
    tn._record_slot_health(tid, "dc", True)  # recovery
    assert tn.consume_tools_list_changed(tid) is True
    # Drained — a second consume returns False (no re-fire without a new recovery).
    assert tn.consume_tools_list_changed(tid) is False


def test_consume_unknown_tenant_is_false():
    assert tn.consume_tools_list_changed("never-seen") is False


def test_notify_helper_is_idempotent():
    tid = "t-idem"
    tn._tunnel_tool_routes[tid] = {"x": "fs"}
    tn.notify_tools_list_changed(tid)
    tn.notify_tools_list_changed(tid)  # second call is harmless
    assert tid in tn._tools_list_changed_pending
    assert tid not in tn._tunnel_tool_routes
    # One drain clears it regardless of how many times notify fired.
    assert tn.consume_tools_list_changed(tid) is True
    assert tn.consume_tools_list_changed(tid) is False


# ---------------------------------------------------------------------------
# Integration with list_tunnel_tools — a re-list observes the recovery once
# ---------------------------------------------------------------------------

def _stub_fetch(monkeypatch, tools_by_label):
    """Stub _fetch_slot_tools so no real WebSocket/proxy is touched."""
    async def fake_fetch(tenant_id, label, *, budget=None):
        return label, list(tools_by_label.get(label, []))
    monkeypatch.setattr(tn, "_fetch_slot_tools", fake_fetch)


def test_relist_after_recovery_reaggregates_and_drains_marker(monkeypatch):
    """After a recovery, the next list_tunnel_tools re-includes the slot's tools and
    clears the pending marker (the session that re-lists sees the recovered tools)."""
    tid = "t-relist"
    # Pretend a dc tunnel is live so list_tunnel_tools will fetch its slot.
    tn._tunnel_dc_sockets[tid] = object()
    try:
        _stub_fetch(monkeypatch, {"dc": [{"name": "read_file"}]})

        # dc was suppressed then recovered -> marker pending, cache dropped.
        tn._record_slot_health(tid, "dc", False)
        tn._record_slot_health(tid, "dc", True)
        assert tid in tn._tools_list_changed_pending

        tools = asyncio.run(tn.list_tunnel_tools(tid))

        # The recovered slot's tool is now advertised (namespaced) again...
        names = {t["name"] for t in tools}
        assert "desktop-commander__read_file" in names
        # ...the routing cache is rebuilt...
        assert tn._tunnel_tool_routes[tid]["desktop-commander__read_file"] == "dc"
        # ...and the pending marker was drained by the re-aggregation.
        assert tid not in tn._tools_list_changed_pending
        assert tn.consume_tools_list_changed(tid) is False
    finally:
        tn._tunnel_dc_sockets.pop(tid, None)


def test_recovered_slot_suppressed_while_unhealthy_then_visible(monkeypatch):
    """Before recovery the unhealthy slot's tools are suppressed; after the healthy
    report they reappear on the next list — the end-to-end bug this item fixes."""
    tid = "t-e2e"
    tn._tunnel_dc_sockets[tid] = object()
    try:
        _stub_fetch(monkeypatch, {"dc": [{"name": "read_file"}]})

        # While unhealthy, list_tunnel_tools skips the dc slot entirely.
        tn._record_slot_health(tid, "dc", False)
        tools_down = asyncio.run(tn.list_tunnel_tools(tid))
        assert not any(t["name"].startswith("desktop-commander__") for t in tools_down)

        # Client recovers the slot (healthy plugin_status).
        tn._record_slot_health(tid, "dc", True)
        tools_up = asyncio.run(tn.list_tunnel_tools(tid))
        assert any(t["name"] == "desktop-commander__read_file" for t in tools_up)
    finally:
        tn._tunnel_dc_sockets.pop(tid, None)


# ---------------------------------------------------------------------------
# The WS receive handler path — a recovery plugin_status frame flows through
# _record_slot_health (both handler bodies call it identically).
# ---------------------------------------------------------------------------

def test_plugin_status_recovery_frame_triggers_signal():
    """Simulate the server receiving the two frames a recovering client sends over a
    slot WS: an unhealthy plugin_status then a healthy one. The second must signal.

    Mirrors the branch in tunnel_ws / _serve_tunnel_ws that dispatches a
    ``plugin_status`` message into _record_slot_health."""
    tid = "t-frame"

    def _apply(frame: dict) -> None:
        # Exactly what the WS handlers do for a msg_type == "plugin_status".
        tn._record_slot_health(
            tid, frame.get("slot") or "fs", frame.get("healthy", True),
            reason=frame.get("reason"), detail=frame.get("detail"),
        )

    _apply({"type": "plugin_status", "slot": "word", "healthy": False,
            "reason": "unreachable", "detail": "port 8811"})
    assert tid not in tn._tools_list_changed_pending

    _apply({"type": "plugin_status", "slot": "word", "healthy": True})
    assert tn.consume_tools_list_changed(tid) is True
