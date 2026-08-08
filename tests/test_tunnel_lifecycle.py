"""Tests for meridian/tunnel_lifecycle.py (39c8cf2c).

Pure state-machine + small asyncio-helper tests. No real WebSocket, no real
process — every "process" involved is the test's own event loop and, where a
real subprocess is genuinely useful (none of the readiness-gate mechanics
need one), this file still never spawns or signals a real ambient process.
"""
from __future__ import annotations

import asyncio

import pytest

from meridian import tunnel_lifecycle as tl


# ---------------------------------------------------------------------------
# TunnelLifecycle state machine
# ---------------------------------------------------------------------------


def test_new_lifecycle_starts_cold():
    lc = tl.TunnelLifecycle(label="fs")
    assert lc.state is tl.LifecycleState.COLD
    assert lc.is_ready is False
    assert lc.ready_since is None
    assert lc.last_transition_at is None
    assert lc.history == []


def test_full_happy_path_transitions():
    lc = tl.TunnelLifecycle(label="fs")
    lc.mark_connecting()
    assert lc.state is tl.LifecycleState.CONNECTING
    lc.mark_ws_open()
    assert lc.state is tl.LifecycleState.WS_OPEN
    assert lc.is_ready is False
    lc.mark_ready()
    assert lc.state is tl.LifecycleState.READY
    assert lc.is_ready is True
    assert lc.ready_since == lc.last_transition_at


def test_leaving_ready_clears_ready_since():
    lc = tl.TunnelLifecycle(label="fs")
    lc.mark_ws_open()
    lc.mark_ready()
    assert lc.ready_since is not None
    lc.mark_reconnecting("dropped")
    assert lc.state is tl.LifecycleState.RECONNECTING
    assert lc.is_ready is False
    assert lc.ready_since is None


def test_never_ready_and_client_lost_and_stopped_transitions():
    lc = tl.TunnelLifecycle(label="fs")
    lc.mark_ws_open()
    lc.mark_never_ready("closed before ready")
    assert lc.state is tl.LifecycleState.NEVER_READY

    lc2 = tl.TunnelLifecycle(label="code")
    lc2.mark_ws_open()
    lc2.mark_ready()
    lc2.mark_client_lost("ConnectionClosedError")
    assert lc2.state is tl.LifecycleState.CLIENT_LOST
    assert lc2.is_ready is False

    lc3 = tl.TunnelLifecycle(label="extract")
    lc3.mark_stopped("shutdown")
    assert lc3.state is tl.LifecycleState.STOPPED


def test_history_is_bounded_ring_buffer():
    lc = tl.TunnelLifecycle(label="fs")
    for i in range(tl._MAX_HISTORY + 10):
        lc.mark_ws_open(detail=str(i))
    assert len(lc.history) == tl._MAX_HISTORY
    # Oldest entries are dropped; the buffer keeps the most recent ones.
    details = [t.detail for t in lc.history]
    assert details[-1] == str(tl._MAX_HISTORY + 9)
    assert details[0] == str(10)  # first 10 (0..9) were evicted


def test_snapshot_is_json_shaped_and_matches_state():
    lc = tl.TunnelLifecycle(label="word")
    lc.mark_ws_open("opened")
    lc.mark_ready("first frame")
    snap = lc.snapshot()
    assert snap["label"] == "word"
    assert snap["state"] == "ready"
    assert snap["is_ready"] is True
    assert snap["ready_since"] == lc.ready_since
    assert isinstance(snap["history"], list)
    assert snap["history"][-1] == {"state": "ready", "at": lc.last_transition_at, "detail": "first frame"}


def test_lifecycle_uses_injectable_clock_not_wall_clock():
    calls = {"n": 0}

    def fake_clock() -> float:
        calls["n"] += 1
        return 42.0 * calls["n"]

    lc = tl.TunnelLifecycle(label="fs", clock=fake_clock)
    lc.mark_ws_open()
    assert lc.last_transition_at == 42.0
    lc.mark_ready()
    assert lc.last_transition_at == 84.0
    assert lc.ready_since == 84.0


# ---------------------------------------------------------------------------
# ReadinessGate
# ---------------------------------------------------------------------------


def test_readiness_gate_announces_exactly_once():
    gate = tl.ReadinessGate()
    assert gate.announced is False
    assert gate.announce() is True
    assert gate.announced is True
    # Every subsequent call is a no-op that returns False.
    assert gate.announce() is False
    assert gate.announce() is False
    assert gate.announced is True


# ---------------------------------------------------------------------------
# TunnelNeverReadyError
# ---------------------------------------------------------------------------


def test_never_ready_error_message_names_the_label():
    exc = tl.TunnelNeverReadyError("dc")
    assert exc.label == "dc"
    assert "dc" in str(exc)
    assert "never" in str(exc).lower() or "before ever becoming ready" in str(exc)
    assert isinstance(exc, RuntimeError)


# ---------------------------------------------------------------------------
# start_grace_timer / stop_grace_timer
# ---------------------------------------------------------------------------


def test_grace_timer_fires_announce_after_elapsed_time():
    announced = []

    async def _run():
        task = tl.start_grace_timer(lambda: announced.append(1), grace_seconds=0.01)
        await asyncio.sleep(0.05)
        assert announced == [1]
        await tl.stop_grace_timer(task)

    asyncio.run(_run())


def test_grace_timer_cancelled_before_firing_never_announces():
    announced = []

    async def _run():
        task = tl.start_grace_timer(lambda: announced.append(1), grace_seconds=10.0)
        # Cancel almost immediately — well before the 10s grace window.
        await tl.stop_grace_timer(task)
        assert announced == []
        assert task.cancelled() or task.done()

    asyncio.run(_run())


def test_stop_grace_timer_on_none_is_a_safe_noop():
    async def _run():
        await tl.stop_grace_timer(None)  # must not raise

    asyncio.run(_run())


def test_stop_grace_timer_after_natural_completion_is_safe():
    """Calling stop_grace_timer AFTER the timer already fired (announce()
    already ran) must not raise — cancel() on an already-done task is a
    documented asyncio no-op."""
    announced = []

    async def _run():
        task = tl.start_grace_timer(lambda: announced.append(1), grace_seconds=0.01)
        await asyncio.sleep(0.05)
        assert announced == [1]
        await tl.stop_grace_timer(task)  # already done — must be a safe no-op

    asyncio.run(_run())


def test_grace_timer_and_message_race_only_announces_once():
    """Simulates the real _run_connection*/... race: the grace timer AND an
    explicit early announce() both fire; the ReadinessGate must ensure only
    the first one is ever acted upon."""
    calls = []
    gate = tl.ReadinessGate()

    def _announce():
        if gate.announce():
            calls.append("fired")

    async def _run():
        task = tl.start_grace_timer(_announce, grace_seconds=10.0)
        # "First message arrives" well before the grace window would fire.
        _announce()
        await tl.stop_grace_timer(task)
        assert calls == ["fired"]

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Process-wide registry
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_registry():
    tl.reset_registry()
    yield
    tl.reset_registry()


def test_get_lifecycle_is_lazily_created_and_cached():
    lc1 = tl.get_lifecycle("fs")
    lc2 = tl.get_lifecycle("fs")
    assert lc1 is lc2
    lc3 = tl.get_lifecycle("code")
    assert lc3 is not lc1


def test_snapshot_all_reflects_every_tracked_label():
    tl.get_lifecycle("fs").mark_ws_open()
    tl.get_lifecycle("code").mark_ready()
    snap = tl.snapshot_all()
    assert set(snap.keys()) == {"fs", "code"}
    assert snap["fs"]["state"] == "ws_open"
    assert snap["code"]["state"] == "ready"


def test_reset_registry_clears_tracked_lifecycles():
    tl.get_lifecycle("fs")
    assert tl.snapshot_all()
    tl.reset_registry()
    assert tl.snapshot_all() == {}
