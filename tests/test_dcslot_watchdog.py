"""3bde892a — request-timeout watchdog for a hung dc-slot SlotProxy.

A dc (Desktop Commander) tunnel slot can hang forever on a genuine MCP
``initialize`` handshake because mcp-proxy's ``createServer()`` has no internal
timeout. The request-level httpx timeout (``_LOCAL_REQUEST_TIMEOUT``) in
``_relay_request`` fires, but the pre-fix code just reported a 502 and left the
mcp-proxy alive. Because :meth:`SlotProxy.is_running` falls back to a port check,
a zombie proxy still bound to the port keeps ``is_running`` True and makes
``ensure_running()`` a no-op forever — the slot is silently dead until the whole
tunnel restarts.

These tests prove the fix (fix direction #1 from 1fa3b3f0's diagnosis): a request
that TIMES OUT against a slot force-kills the SlotProxy so the NEXT request
re-triggers ``ensure_running()``/respawn — reusing the same kill+respawn recovery
a failed health probe already gets via ``_reprobe_once``. Scoped so a normal
slow-but-successful request never force-kills a healthy slot.
"""
from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock

import httpx

from meridian import tunnel_client as tc


# ---------------------------------------------------------------------------
# _relay_request tags a genuine timeout distinctly (and ONLY a timeout)
# ---------------------------------------------------------------------------

def _relay(msg, handler):
    """Run _relay_request against a mock local proxy defined by ``handler``."""
    async def _inner():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await tc._relay_request(client, "http://127.0.0.1:8813", msg)

    return asyncio.run(_inner())


def test_relay_request_marks_timeout_with_private_flag():
    """A request-level timeout is a 502 that ALSO carries the private
    ``_timed_out`` marker so the lazy-spawn caller can recover the slot."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("initialize handshake hung", request=request)

    msg = {"type": "request", "id": "t1", "method": "GET", "path": "/mcp"}
    resp = _relay(msg, handler)
    assert resp["status"] == 502
    assert resp.get("_timed_out") is True
    assert tc._relay_timed_out(resp) is True


def test_relay_request_connect_error_is_not_marked_timeout():
    """A connection-refused failure is a 502 but NOT a timeout — it must not carry
    the ``_timed_out`` marker (killing the slot there would be wrong)."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    msg = {"type": "request", "id": "t2", "method": "GET", "path": "/mcp"}
    resp = _relay(msg, handler)
    assert resp["status"] == 502
    assert "_timed_out" not in resp
    assert tc._relay_timed_out(resp) is False


def test_relay_request_success_is_not_marked_timeout():
    """A normal (slow-but-successful) response is never marked as a timeout, so the
    watchdog never force-kills a healthy slot."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    msg = {"type": "request", "id": "t3", "method": "GET", "path": "/mcp"}
    resp = _relay(msg, handler)
    assert resp["status"] == 200
    assert tc._relay_timed_out(resp) is False


def test_relay_timed_out_reader_is_robust():
    """``_relay_timed_out`` never raises on non-dict / missing-key input."""
    assert tc._relay_timed_out(None) is False
    assert tc._relay_timed_out("not-a-dict") is False
    assert tc._relay_timed_out({}) is False
    assert tc._relay_timed_out({"_timed_out": False}) is False
    assert tc._relay_timed_out({"_timed_out": True}) is True


# ---------------------------------------------------------------------------
# _kill_on_request_timeout force-kills a running proxy (best-effort)
# ---------------------------------------------------------------------------

def test_kill_on_request_timeout_kills_running_proxy():
    class FakeProxy:
        label = "dc"
        port = 8813
        is_running = True
        killed = 0

        def kill(self, reason: str = "stopped"):
            # ddd46cc8 — _kill_on_request_timeout now calls
            # kill(reason="transport_closed"); accept the kwarg like the real
            # SlotProxy.kill() does.
            type(self).killed += 1
            type(self).is_running = False

    proxy = FakeProxy()
    asyncio.run(tc._kill_on_request_timeout(proxy))
    assert proxy.killed == 1


def test_kill_on_request_timeout_noop_when_not_running():
    class FakeProxy:
        label = "dc"
        port = 8813
        is_running = False
        killed = 0

        def kill(self):
            type(self).killed += 1

    proxy = FakeProxy()
    asyncio.run(tc._kill_on_request_timeout(proxy))
    assert proxy.killed == 0


def test_kill_on_request_timeout_swallows_kill_errors():
    """A kill() that raises must never break the relay loop."""
    class FakeProxy:
        label = "dc"
        port = 8813
        is_running = True

        def kill(self):
            raise RuntimeError("taskkill blew up")

    # Must not raise.
    asyncio.run(tc._kill_on_request_timeout(FakeProxy()))


# ---------------------------------------------------------------------------
# Integration — a timed-out first request kills the slot; the NEXT request
# respawns it (recovers within one cycle) rather than staying dead.
# ---------------------------------------------------------------------------

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


def test_run_connection_lazy_timeout_kills_then_next_request_respawns(monkeypatch):
    """The heart of 3bde892a: first request TIMES OUT against a just-spawned dc
    slot → the SlotProxy is force-killed → the SECOND request re-spawns it (one
    recovery cycle), and the private ``_timed_out`` marker never leaks on the wire.
    """
    sent = []

    class FakeWS:
        def __init__(self):
            self._msgs = [
                json.dumps({"type": "request", "id": "1"}),  # spawns → TIMES OUT
                json.dumps({"type": "request", "id": "2"}),  # must re-spawn + relay
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._msgs:
                raise StopAsyncIteration
            return self._msgs.pop(0)

        async def send(self, data):
            sent.append(data)

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    # First relay times out (marker set, like _relay_request does); second succeeds.
    calls = {"n": 0}

    async def fake_relay(client, base, msg, tool_prefix=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"type": "response", "id": msg["id"], "status": 502,
                    "_timed_out": True,
                    "headers": {"content-type": "application/json"},
                    "body": base64.b64encode(b'{"error":"timeout"}').decode()}
        return {"type": "response", "id": msg["id"], "status": 200}

    import httpx as _httpx
    import websockets as _ws
    monkeypatch.setattr(_ws, "connect", lambda url, **kw: FakeWS())
    monkeypatch.setattr(_httpx, "AsyncClient", lambda *a, **k: FakeHttpClient())
    monkeypatch.setattr(tc, "_relay_request", fake_relay)
    # Keep pre-flight healthy so it adds no extra sent message / behaviour.
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=True))

    proxy = tc.SlotProxy(["cmd", "/c", "npx", "dc"], 8813, "dc")

    ensured = {"n": 0}
    killed = {"n": 0}

    async def fake_ensure(self):
        ensured["n"] += 1
        self._proc = _FakeProc()
        self.holder["proc"] = self._proc
    monkeypatch.setattr(tc.SlotProxy, "ensure_running", fake_ensure)

    real_kill = tc.SlotProxy.kill

    def counting_kill(self, reason: str = "stopped"):
        # ddd46cc8 — forward the reason kwarg to the real kill() so diagnostics
        # still get recorded correctly under this monkeypatch.
        killed["n"] += 1
        real_kill(self, reason)
    monkeypatch.setattr(tc.SlotProxy, "kill", counting_kill)
    # taskkill (win32 path of _terminate_proc_tree) must not hit the real shell.
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    asyncio.run(tc._run_connection_lazy("wss://x/tunnel/t", proxy, "dc"))

    # Two spawns: the initial lazy spawn AND the respawn after the timeout kill.
    assert ensured["n"] == 2, "slot did not respawn after the timeout kill"
    # The timeout force-killed the slot exactly once (recovery within one cycle).
    assert killed["n"] == 1, "timed-out slot was not force-killed"
    # Both requests got a wire response; the private marker never leaked.
    assert len(sent) == 2
    first = json.loads(sent[0])
    second = json.loads(sent[1])
    assert first["status"] == 502
    assert "_timed_out" not in first
    assert second["status"] == 200


def test_run_connection_lazy_slow_success_does_not_kill(monkeypatch):
    """A slow-but-successful request must NOT force-kill the slot — the watchdog is
    scoped strictly to actual timeouts."""
    sent = []

    class FakeWS:
        def __init__(self):
            self._msgs = [json.dumps({"type": "request", "id": "1"})]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._msgs:
                raise StopAsyncIteration
            return self._msgs.pop(0)

        async def send(self, data):
            sent.append(data)

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    async def fake_relay(client, base, msg, tool_prefix=None):
        # Slow but SUCCESSFUL — no _timed_out marker.
        return {"type": "response", "id": msg["id"], "status": 200}

    import httpx as _httpx
    import websockets as _ws
    monkeypatch.setattr(_ws, "connect", lambda url, **kw: FakeWS())
    monkeypatch.setattr(_httpx, "AsyncClient", lambda *a, **k: FakeHttpClient())
    monkeypatch.setattr(tc, "_relay_request", fake_relay)
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=True))

    proxy = tc.SlotProxy(["cmd", "/c", "npx", "dc"], 8813, "dc")

    ensured = {"n": 0}
    killed = {"n": 0}

    async def fake_ensure(self):
        ensured["n"] += 1
        self._proc = _FakeProc()
        self.holder["proc"] = self._proc
    monkeypatch.setattr(tc.SlotProxy, "ensure_running", fake_ensure)

    def counting_kill(self):
        killed["n"] += 1
    monkeypatch.setattr(tc.SlotProxy, "kill", counting_kill)

    asyncio.run(tc._run_connection_lazy("wss://x/tunnel/t", proxy, "dc"))

    assert ensured["n"] == 1          # spawned once, never respawned
    assert killed["n"] == 0           # healthy slot NOT killed
    assert len(sent) == 1
    assert json.loads(sent[0])["status"] == 200
