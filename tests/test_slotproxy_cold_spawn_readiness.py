"""Regression test for e75f4fc4 — SlotProxy.ensure_running's cold-spawn readiness.

GROUND-TRUTH FINDING (direct code read of meridian/tunnel_client.py): the lazy
respawn path (SlotProxy.ensure_running, fired on the first request after a
30-minute idle-kill or on first-ever use) waited a BLIND asyncio.sleep(1.0)
before considering the proxy ready — long enough for a warm restart, not for a
genuinely cold npx/uvx package-resolution + server-boot. That gap let the first
real request after a cold spawn hit an unready proxy and hang until mcp-proxy's
own internal ~60s timeout (-32001), which the 1s pause did nothing to prevent.

_probe_slot_health already existed and is proven for exactly this "is the proxy
actually answering tools/list yet" check — it already backs the REACTIVE
post-timeout watchdog (3bde892a). The fix reuses it in ensure_running's cold-
spawn path too, instead of a blind sleep, so the FIRST attempt gets the same
readiness guarantee the reactive recovery path already had.
"""
import asyncio
from unittest.mock import AsyncMock

import meridian.tunnel_client as tc


class _FakeProc:
    """Minimal subprocess.Popen stand-in: alive until .terminate()/.kill()."""
    def __init__(self, cmd=None, *a, **k):
        self.cmd = cmd
        self.pid = 4242
        self._alive = True
        self.returncode = None

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._alive = False
        self.returncode = -9


def test_ensure_running_calls_probe_slot_health_not_blind_sleep(monkeypatch):
    """The cold-spawn path must consult _probe_slot_health (real readiness),
    not just wait a fixed duration and assume success."""
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    calls = []

    async def fake_probe(port, *, attempts=2, delay=3.0):
        calls.append({"port": port, "attempts": attempts, "delay": delay})
        return True

    monkeypatch.setattr(tc, "_probe_slot_health", fake_probe)

    sp = tc.SlotProxy(["proxy", "cmd"], 8813, "dc")
    asyncio.run(sp.ensure_running())

    assert len(calls) == 1
    assert calls[0]["port"] == 8813
    # A generous window — enough for a real cold npx/uvx resolve, not a blind 1s.
    assert calls[0]["attempts"] >= 4
    assert calls[0]["delay"] >= 2.0


def test_ensure_running_succeeds_quietly_when_probe_healthy(monkeypatch, capsys):
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=True))

    sp = tc.SlotProxy(["proxy", "cmd"], 8813, "dc")
    asyncio.run(sp.ensure_running())

    assert sp.is_running
    out = capsys.readouterr().out
    assert "did not answer" not in out


def test_ensure_running_warns_but_does_not_raise_when_probe_unhealthy(monkeypatch, capsys):
    """A cold spawn that never answers within the readiness window must not
    raise (matches the prior no-raise behavior) but MUST log a diagnosable
    warning instead of silently pretending the proxy is ready."""
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=False))

    sp = tc.SlotProxy(["proxy", "cmd"], 8813, "dc")
    asyncio.run(sp.ensure_running())  # must not raise

    out = capsys.readouterr().out
    assert "did not answer" in out
    assert "dc" in out


def test_ensure_running_touches_last_used_regardless_of_probe_outcome(monkeypatch):
    """touch() must fire whether the probe succeeds or fails — a request is
    about to be attempted against the slot either way, so idle-tracking must
    reflect that (matches prior behavior of the unconditional sleep+touch)."""
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    for outcome in (True, False):
        monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=outcome))
        sp = tc.SlotProxy(["proxy", "cmd"], 8813, "dc")
        assert sp.idle_seconds() == 0.0
        asyncio.run(sp.ensure_running())
        assert sp._last_used != 0.0


def test_ensure_running_still_noop_when_already_running(monkeypatch):
    """Second call while the proxy is already running must not re-probe —
    matches the existing is_running short-circuit at the top of ensure_running."""
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(tc, "_probe_slot_health", probe)

    sp = tc.SlotProxy(["proxy", "cmd"], 8813, "dc")
    asyncio.run(sp.ensure_running())
    assert probe.call_count == 1
    asyncio.run(sp.ensure_running())
    assert probe.call_count == 1  # no second probe — already running
