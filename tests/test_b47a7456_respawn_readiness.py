"""b47a7456 — a respawned slot that misses its cold-spawn readiness window must
not stay advertised as healthy.

Drives the real ``_run_connection_lazy`` request loop through three requests:

1. first request  -> spawn, readiness probe OK, preflight OK, request relayed
2. idle-kill      -> the proxy is torn down between requests
3. second request -> respawn; its readiness probe (inside ensure_running) is
   the one that either passes or misses the window

The respawn path skips the per-connection preflight (it already ran), so before
this fix a missed respawn readiness window left the slot advertised as healthy
with no re-probe. The fix reports the slot unhealthy (server suppresses its
tools) and starts the background re-probe. The relayed request is unchanged.
"""
import asyncio
import json

import meridian.tunnel_client as tc


class _FakeProc:
    """Minimal subprocess.Popen stand-in: alive until terminated/killed."""

    def __init__(self, cmd=None, *a, **k):
        self.cmd = cmd
        self.pid = 4244
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


class _FakeWS:
    """Async-iterable WS stub. A callable step runs synchronously (used to
    simulate an idle-kill between two inbound requests); any other step is
    yielded as an inbound message."""

    def __init__(self, steps, sent):
        self._steps = list(steps)
        self._sent = sent

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        while self._steps:
            step = self._steps.pop(0)
            if callable(step):
                step()
                continue
            return step
        raise StopAsyncIteration

    async def send(self, data):
        self._sent.append(json.loads(data))


class _FakeHttpClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _drive(monkeypatch, probe_results):
    """Run three requests through ``_run_connection_lazy`` for slot ``fs``.

    *probe_results* is consumed in call order by ``_probe_slot_health``; any
    call beyond it reports False. Returns (sent, proxy, probe_calls).
    """
    import httpx as _httpx
    import websockets as _ws

    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_port_is_open", lambda *a, **k: False)

    proxy = tc.SlotProxy(["proxy", "cmd"], 8813, "fs")

    results = iter(probe_results)
    probe_calls = []

    async def fake_probe(port, *, attempts=2, delay=3.0):
        probe_calls.append(port)
        return next(results, False)

    monkeypatch.setattr(tc, "_probe_slot_health", fake_probe)

    async def fake_relay(client, base, msg, tool_prefix=None):
        return {"type": "response", "id": msg["id"], "status": 200}

    monkeypatch.setattr(tc, "_relay_request", fake_relay)
    monkeypatch.setattr(_httpx, "AsyncClient", lambda *a, **k: _FakeHttpClient())

    sent = []
    steps = [
        json.dumps({"type": "request", "id": "1"}),
        lambda: proxy.kill(reason="idle_killed"),  # idle-kill between requests
        json.dumps({"type": "request", "id": "2"}),
    ]
    monkeypatch.setattr(
        _ws, "connect", lambda url, **kw: _FakeWS(steps, sent)
    )

    asyncio.run(tc._run_connection_lazy("wss://x/tunnel/t", proxy, "fs"))
    return sent, proxy, probe_calls


def _unhealthy_reports(sent):
    return [
        (i, m) for i, m in enumerate(sent)
        if m.get("type") == "plugin_status" and m.get("slot") == "fs"
        and m.get("healthy") is False
    ]


def test_respawn_readiness_miss_reports_slot_unhealthy(monkeypatch):
    """Respawn probe misses -> the slot is reported unhealthy (tools suppressed),
    and the diagnostics stay truthful about the missed window."""
    # ensure_running probe (req1 spawn) OK, preflight (req1) OK,
    # ensure_running probe (req2 respawn) MISSES.
    sent, proxy, probe_calls = _drive(monkeypatch, [True, True, False])

    unhealthy = _unhealthy_reports(sent)
    assert len(unhealthy) == 1, f"expected one unhealthy report, got {sent!r}"
    report_idx, _ = unhealthy[0]

    responses = [i for i, m in enumerate(sent) if m.get("status") == 200]
    assert len(responses) == 2, "both requests are still relayed"
    # The unhealthy report is sent before the second request's relayed response.
    assert report_idx < responses[1]
    # Only the initial spawn probe, the first-connection preflight, and the
    # respawn probe ran -- no extra preflight on the respawn.
    assert len(probe_calls) == 3
    assert proxy.diagnostics.state is tc.SlotState.STARTUP_TIMEOUT


def test_respawn_readiness_hit_stays_healthy_and_unreported(monkeypatch):
    """Respawn probe passes -> no unhealthy report; nothing is suppressed."""
    sent, proxy, probe_calls = _drive(monkeypatch, [True, True, True])

    assert _unhealthy_reports(sent) == []
    assert len([m for m in sent if m.get("status") == 200]) == 2
    assert len(probe_calls) == 3
    assert proxy.diagnostics.state is tc.SlotState.HEALTHY
