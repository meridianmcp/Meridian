"""Coverage-oriented tests for meridian/tunnel_client.py.

This file targets the lines NOT already exercised by tests/test_tunnel_client.py:
the browser auth flow, _fetch_me, _run_connection, _reconnect_loop, the
non-http(s) URL branches, _find_npx on Windows, several small error branches,
and the large run_tunnel() orchestration with all early-exit and happy paths.

Everything is mocked — no real network, subprocess, websocket, or filesystem
side effects outside tmp_path. Owned by a separate session from
test_tunnel_client.py; do not duplicate tests there.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from meridian import tunnel_client as tc


# ---------------------------------------------------------------------------
# _write_cached_token — chmod failure tolerated; malformed existing file
# ---------------------------------------------------------------------------

def test_write_cached_token_tolerates_chmod_failure(monkeypatch, tmp_path):
    cfg = tmp_path / ".meridian" / "config.json"
    monkeypatch.setattr(tc, "_config_path", lambda: cfg)

    def boom_chmod(self, mode):
        raise PermissionError("no chmod here")

    monkeypatch.setattr(tc.Path, "chmod", boom_chmod)
    # Should not raise despite chmod failing (lines 129-132).
    tc._write_cached_token("https://usemeridian.us", "sk_x")
    assert cfg.exists()
    assert tc._read_cached_token("https://usemeridian.us") == "sk_x"


def test_write_cached_token_recovers_from_malformed_existing(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(tc, "_config_path", lambda: cfg)
    cfg.write_text("{not json at all", encoding="utf-8")
    # Existing-but-malformed file → start clean (lines 119-120) but still write.
    tc._write_cached_token("https://usemeridian.us", "sk_new")
    assert tc._read_cached_token("https://usemeridian.us") == "sk_new"


# ---------------------------------------------------------------------------
# URL builders — the else branch for a non-http(s) base (lines 197, 226, 244)
# ---------------------------------------------------------------------------

def test_ws_url_passthrough_for_non_http_base():
    # base lacks http:// or https:// → used verbatim as ws_base.
    url = tc._ws_url("wss://already", "t", "tok")
    assert url.startswith("wss://already/tunnel/t?token=")


def test_ws_code_url_passthrough_for_non_http_base():
    url = tc._ws_code_url("custom-base", "t", "tok")
    assert url.startswith("custom-base/tunnel-code/t?token=")


def test_ws_extract_url_passthrough_for_non_http_base():
    url = tc._ws_extract_url("custom-base", "t", "tok")
    assert url.startswith("custom-base/tunnel-extract/t?token=")


# ---------------------------------------------------------------------------
# _managed_bin_dir (line 325)
# ---------------------------------------------------------------------------

def test_managed_bin_dir_under_home(monkeypatch, tmp_path):
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    assert tc._managed_bin_dir() == tmp_path / ".meridian" / "bin"


# ---------------------------------------------------------------------------
# _find_npx — Windows branches (lines 519-529)
# ---------------------------------------------------------------------------

def test_find_npx_windows_uses_which_cmd(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "win32")
    monkeypatch.setattr(
        tc.shutil, "which",
        lambda name: r"C:\npm\npx.cmd" if name == "npx.cmd" else None,
    )
    assert tc._find_npx() == r"C:\npm\npx.cmd"


def test_find_npx_windows_appdata_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(tc.sys, "platform", "win32")
    monkeypatch.setattr(tc.shutil, "which", lambda name: None)
    appdata = tmp_path / "AppData" / "Roaming"
    npm = appdata / "npm"
    npm.mkdir(parents=True)
    shim = npm / "npx.cmd"
    shim.touch()
    monkeypatch.setenv("APPDATA", str(appdata))
    assert tc._find_npx() == str(shim)


def test_find_npx_windows_last_resort_literal(monkeypatch, tmp_path):
    monkeypatch.setattr(tc.sys, "platform", "win32")
    monkeypatch.setattr(tc.shutil, "which", lambda name: None)
    monkeypatch.setenv("APPDATA", str(tmp_path / "nonexistent"))
    # which misses, no shim on disk → falls back to bare "npx.cmd" literal.
    assert tc._find_npx() == "npx.cmd"


# ---------------------------------------------------------------------------
# _download_codebase_memory_mcp — "no suitable asset" branch (436-441)
# ---------------------------------------------------------------------------

def test_download_returns_none_when_no_suitable_asset(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "_managed_bin_dir", lambda: tmp_path)
    monkeypatch.setattr(tc, "_pick_release_asset", lambda assets: None)

    def make_client(*a, **kw):
        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return {"tag_name": "v1", "assets": []}

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kw): return FakeResp()

        return FakeClient()

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "AsyncClient", make_client)
    assert asyncio.run(tc._download_codebase_memory_mcp()) is None


# ---------------------------------------------------------------------------
# _index_code_dir — non-200 probe break path + index HTTP-error branch
# ---------------------------------------------------------------------------

def test_index_code_dir_index_returns_error_status(monkeypatch):
    """Probe succeeds; index call returns >=400 → error-branch logged (612-617)."""
    statuses = iter([200, 404])  # first POST (probe), second POST (index)

    class FakeResp:
        def __init__(self, code): self.status_code = code

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, **kw):
            return FakeResp(next(statuses))

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)
    # Must complete without raising even on the error branch.
    asyncio.run(tc._index_code_dir(8809, "/repo"))


def test_index_code_dir_index_raises_is_caught(monkeypatch):
    """Probe OK, index POST raises → exception branch (616-618) handled."""
    state = {"n": 0}

    class FakeResp:
        status_code = 200

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, **kw):
            state["n"] += 1
            if state["n"] == 1:
                return FakeResp()  # probe succeeds
            raise RuntimeError("index boom")

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)
    asyncio.run(tc._index_code_dir(8809, "/repo"))  # no raise


# ---------------------------------------------------------------------------
# _fetch_me (lines 676-684)
# ---------------------------------------------------------------------------

def test_fetch_me_returns_json(monkeypatch):
    captured = {}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"tenant_id": "t1", "plan": "pro"}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, headers=None, **kw):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResp()

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)
    me = asyncio.run(tc._fetch_me("https://x", "sk_tok"))
    assert me == {"tenant_id": "t1", "plan": "pro"}
    assert captured["url"] == "https://x/me"
    assert captured["headers"]["Authorization"] == "Bearer sk_tok"


# ---------------------------------------------------------------------------
# _run_connection (lines 689-707) — drive the async-for over a fake ws
# ---------------------------------------------------------------------------

def test_run_connection_relays_request_and_skips_noise(monkeypatch):
    sent = []
    relayed = []

    class FakeWS:
        def __init__(self):
            self._msgs = [
                "not-json-at-all",                       # ValueError → continue
                json.dumps([1, 2, 3]),                   # not a dict → continue
                json.dumps({"type": "ping"}),            # ping → continue
                json.dumps({"type": "request", "id": "1"}),  # relayed
            ]
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def __aiter__(self): return self
        async def __anext__(self):
            if not self._msgs:
                raise StopAsyncIteration
            return self._msgs.pop(0)
        async def send(self, data):
            sent.append(data)

    fake_ws = FakeWS()

    def fake_connect(url, **kw):
        return fake_ws

    class FakeHttpClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    async def fake_relay(client, base, msg, label="", tool_prefix=None):
        relayed.append(msg)
        return {"type": "response", "id": msg["id"], "status": 200}

    import httpx as _httpx
    import websockets as _ws
    monkeypatch.setattr(_ws, "connect", fake_connect)
    monkeypatch.setattr(_httpx, "AsyncClient", lambda *a, **k: FakeHttpClient())
    monkeypatch.setattr(tc, "_relay_request", fake_relay)

    asyncio.run(tc._run_connection("wss://x/tunnel/t", 8808, "fs"))
    # Only the single request message was relayed and a response sent.
    assert len(relayed) == 1
    assert relayed[0]["type"] == "request"
    assert len(sent) == 1
    assert json.loads(sent[0])["status"] == 200


# ---------------------------------------------------------------------------
# _reconnect_loop (lines 714-730)
# ---------------------------------------------------------------------------

def test_reconnect_loop_backs_off_then_cancels(monkeypatch):
    """A failing connection is retried after a backoff sleep; CancelledError
    from the connection propagates out (the clean-shutdown path)."""
    attempts = {"n": 0}

    async def fake_run_connection(ws_url, port, label, tool_prefix=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("dropped")  # logged, then backoff sleep
        raise asyncio.CancelledError  # second pass → propagate (line 721-722)

    sleeps = []

    async def fake_sleep(n):
        sleeps.append(n)

    monkeypatch.setattr(tc, "_run_connection", fake_run_connection)
    monkeypatch.setattr(tc.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tc._reconnect_loop("wss://x", 8808, "fs"))
    assert attempts["n"] == 2
    assert sleeps  # at least one backoff sleep happened


# ---------------------------------------------------------------------------
# SlotProxy + lazy-spawn coroutines (3649a61a) — lazy plugin spawning
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


def test_slotproxy_ensure_running_spawns_once_then_kill(monkeypatch):
    """ensure_running spawns lazily (not at construction); kill() tears it down."""
    spawned = []
    monkeypatch.setattr(tc.subprocess, "Popen",
                        lambda cmd, *a, **k: spawned.append(cmd) or _FakeProc(cmd))
    # Skip the 1s port-bind sleep.
    monkeypatch.setattr(tc.asyncio, "sleep", AsyncMock(return_value=None))
    # taskkill (win32 path of _terminate_proc_tree) must not hit the real shell.
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    sp = tc.SlotProxy(["proxy", "cmd"], 8808, "fs")
    assert not sp.is_running          # NOT spawned at construction (lazy)
    assert len(spawned) == 0

    asyncio.run(sp.ensure_running())
    assert sp.is_running
    assert len(spawned) == 1
    # Second call is a no-op while already running.
    asyncio.run(sp.ensure_running())
    assert len(spawned) == 1

    sp.kill()
    assert not sp.is_running
    assert sp.holder["proc"] is None


def test_slotproxy_ensure_running_swallows_spawn_failure(monkeypatch):
    """A Popen that raises leaves the proxy not-running (no exception escapes)."""
    def boom(cmd, *a, **k):
        raise FileNotFoundError("no such binary")
    monkeypatch.setattr(tc.subprocess, "Popen", boom)
    sp = tc.SlotProxy(["bad", "cmd"], 8810, "extract")
    asyncio.run(sp.ensure_running())  # must not raise
    assert not sp.is_running
    assert sp.holder["proc"] is None


def test_slotproxy_idle_seconds_and_touch(monkeypatch):
    sp = tc.SlotProxy(["x"], 8808, "fs")
    assert sp.idle_seconds() == 0.0   # never used
    t = {"now": 1000.0}
    monkeypatch.setattr(tc.time, "monotonic", lambda: t["now"])
    sp.touch()
    t["now"] = 1042.0
    assert sp.idle_seconds() == 42.0


def test_idle_killer_kills_idle_proxy_then_cancels(monkeypatch):
    """_idle_killer kills a running proxy once it exceeds the idle window."""
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)
    sp = tc.SlotProxy(["x"], 8808, "fs")
    sp._proc = tc.subprocess.Popen(["x"])
    sp.holder["proc"] = sp._proc
    assert sp.is_running

    killed = {"n": 0}
    real_kill = sp.kill
    def counting_kill():
        killed["n"] += 1
        real_kill()
    sp.kill = counting_kill
    # Force the idle window to be exceeded.
    monkeypatch.setattr(sp, "idle_seconds", lambda: 9999.0)

    # Make the killer's poll-sleep a real (instant) yield so the loop ticks but
    # control still returns to the event loop; cancel after the first kill.
    # Capture the real sleep first — patching tc.asyncio.sleep replaces it on the
    # shared asyncio module, so we must not call the patched name from inside.
    _real_sleep = asyncio.sleep
    async def quick_sleep(_n):
        await _real_sleep(0)
    monkeypatch.setattr(tc.asyncio, "sleep", quick_sleep)

    async def drive():
        task = asyncio.ensure_future(tc._idle_killer(sp, idle_seconds=1.0))
        for _ in range(20):
            await _real_sleep(0)
            if killed["n"]:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert killed["n"] >= 1


def test_run_connection_lazy_spawns_on_request_and_relays(monkeypatch):
    """_run_connection_lazy brings the proxy up on the first request, touches it,
    relays, and skips ping/non-dict/non-json noise."""
    sent = []
    relayed = []

    class FakeWS:
        def __init__(self):
            self._msgs = [
                "not-json",                               # ValueError → skip
                json.dumps([1, 2]),                       # not a dict → skip
                json.dumps({"type": "ping"}),             # ping → skip
                json.dumps({"type": "request", "id": "1"}),  # spawns + relays
            ]
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def __aiter__(self): return self
        async def __anext__(self):
            if not self._msgs:
                raise StopAsyncIteration
            return self._msgs.pop(0)
        async def send(self, data): sent.append(data)

    class FakeHttpClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    async def fake_relay(client, base, msg, tool_prefix=None):
        relayed.append((base, msg))
        return {"type": "response", "id": msg["id"], "status": 200}

    import httpx as _httpx
    import websockets as _ws
    monkeypatch.setattr(_ws, "connect", lambda url, **kw: FakeWS())
    monkeypatch.setattr(_httpx, "AsyncClient", lambda *a, **k: FakeHttpClient())
    monkeypatch.setattr(tc, "_relay_request", fake_relay)
    # Pre-flight is incidental here — keep it healthy so it sends no extra msg.
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=True))

    proxy = tc.SlotProxy(["x"], 8808, "fs")
    ensured = {"n": 0}

    async def fake_ensure(self):
        ensured["n"] += 1
        self._proc = _FakeProc()
        self.holder["proc"] = self._proc
    monkeypatch.setattr(tc.SlotProxy, "ensure_running", fake_ensure)

    asyncio.run(tc._run_connection_lazy("wss://x/tunnel/t", proxy, "fs"))
    assert ensured["n"] == 1                       # spawned on first request
    assert len(relayed) == 1
    assert relayed[0][0] == "http://127.0.0.1:8808"
    assert len(sent) == 1
    assert json.loads(sent[0])["status"] == 200


def test_run_connection_lazy_returns_503_when_spawn_fails(monkeypatch):
    """If ensure_running can't bring the proxy up, the request gets a 503 (not a
    server-side timeout)."""
    sent = []

    class FakeWS:
        def __init__(self):
            self._msgs = [json.dumps({"type": "request", "id": "7"})]
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def __aiter__(self): return self
        async def __anext__(self):
            if not self._msgs:
                raise StopAsyncIteration
            return self._msgs.pop(0)
        async def send(self, data): sent.append(data)

    class FakeHttpClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    import httpx as _httpx
    import websockets as _ws
    monkeypatch.setattr(_ws, "connect", lambda url, **kw: FakeWS())
    monkeypatch.setattr(_httpx, "AsyncClient", lambda *a, **k: FakeHttpClient())

    proxy = tc.SlotProxy(["x"], 8808, "fs")

    async def fake_ensure(self):  # spawn fails → still not running
        return None
    monkeypatch.setattr(tc.SlotProxy, "ensure_running", fake_ensure)

    asyncio.run(tc._run_connection_lazy("wss://x/tunnel/t", proxy, "fs"))
    assert len(sent) == 1
    assert json.loads(sent[0])["status"] == 503


def test_reconnect_loop_lazy_backs_off_then_cancels(monkeypatch):
    """A failing lazy connection is retried after backoff; CancelledError from the
    connection propagates out (clean shutdown)."""
    attempts = {"n": 0}

    async def fake_run_connection_lazy(ws_url, proxy, label, tool_prefix=None, known_repo_paths=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("dropped")
        raise asyncio.CancelledError

    sleeps = []

    async def fake_sleep(n):
        sleeps.append(n)

    monkeypatch.setattr(tc, "_run_connection_lazy", fake_run_connection_lazy)
    monkeypatch.setattr(tc.asyncio, "sleep", fake_sleep)

    proxy = tc.SlotProxy(["x"], 8808, "fs")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tc._reconnect_loop_lazy("wss://x", proxy, "fs"))
    assert attempts["n"] == 2
    assert sleeps


# ---------------------------------------------------------------------------
# d71ba2e7 — core slot pre-flight health check
# ---------------------------------------------------------------------------

class _FakeProbeResp:
    def __init__(self, status):
        self.status_code = status


def _patch_probe_client(monkeypatch, statuses):
    """Patch httpx.AsyncClient so each .post() pops the next status (or raises
    on an Exception value)."""
    seq = list(statuses)

    class _FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **k):
            val = seq.pop(0)
            if isinstance(val, Exception):
                raise val
            return _FakeProbeResp(val)

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)


def test_probe_slot_health_ok_first_try(monkeypatch):
    _patch_probe_client(monkeypatch, [200])
    assert asyncio.run(tc._probe_slot_health(8808, attempts=2, delay=0)) is True


def test_probe_slot_health_retries_then_succeeds(monkeypatch):
    # First a connection error, then a 200 on the retry.
    _patch_probe_client(monkeypatch, [ConnectionError("refused"), 200])
    assert asyncio.run(tc._probe_slot_health(8808, attempts=2, delay=0)) is True


def test_probe_slot_health_all_fail(monkeypatch):
    _patch_probe_client(monkeypatch, [503, 500])
    assert asyncio.run(tc._probe_slot_health(8808, attempts=2, delay=0)) is False


def test_preflight_slot_reports_unhealthy_on_failure(monkeypatch):
    sent = []

    class _WS:
        async def send(self, data): sent.append(json.loads(data))

    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=False))
    healthy = asyncio.run(tc._preflight_slot(_WS(), 8808, "fs"))
    assert healthy is False
    assert sent == [{"type": "plugin_status", "slot": "fs", "healthy": False}]


def test_preflight_slot_healthy_sends_nothing(monkeypatch):
    sent = []

    class _WS:
        async def send(self, data): sent.append(json.loads(data))

    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=True))
    healthy = asyncio.run(tc._preflight_slot(_WS(), 8808, "code"))
    assert healthy is True
    assert sent == []


def test_run_connection_lazy_preflight_reports_unhealthy(monkeypatch):
    """First request triggers spawn + pre-flight; a failing probe sends a
    plugin_status(unhealthy) up the WS before relaying."""
    sent = []

    class FakeWS:
        def __init__(self):
            self._msgs = [json.dumps({"type": "request", "id": "1"})]
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def __aiter__(self): return self
        async def __anext__(self):
            if not self._msgs:
                raise StopAsyncIteration
            return self._msgs.pop(0)
        async def send(self, data): sent.append(json.loads(data))

    class FakeHttpClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    async def fake_relay(client, base, msg, tool_prefix=None):
        return {"type": "response", "id": msg["id"], "status": 200}

    import httpx as _httpx
    import websockets as _ws
    monkeypatch.setattr(_ws, "connect", lambda url, **kw: FakeWS())
    monkeypatch.setattr(_httpx, "AsyncClient", lambda *a, **k: FakeHttpClient())
    monkeypatch.setattr(tc, "_relay_request", fake_relay)
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=False))

    proxy = tc.SlotProxy(["x"], 8808, "fs")

    async def fake_ensure(self):
        self._proc = _FakeProc()
        self.holder["proc"] = self._proc
    monkeypatch.setattr(tc.SlotProxy, "ensure_running", fake_ensure)

    asyncio.run(tc._run_connection_lazy("wss://x/tunnel/t", proxy, "fs"))
    # Both the plugin_status (unhealthy) and the relayed response were sent.
    assert {"type": "plugin_status", "slot": "fs", "healthy": False} in sent
    assert any(m.get("status") == 200 for m in sent)


# ---------------------------------------------------------------------------
# 9a8645c1 — Serena access-denied classification
# ---------------------------------------------------------------------------

def test_classify_serena_failure_access_denied():
    f = tc._classify_serena_failure
    # PermissionError → access_denied.
    r = f(PermissionError("access is denied"))
    assert r is not None and r[0] == "access_denied"
    # WinError 5 string signature.
    assert f("[WinError 5] Access is denied: 'My Music'")[0] == "access_denied"
    # POSIX errno 13.
    assert f(OSError("[Errno 13] Permission denied"))[0] == "access_denied"
    # Benign / unrelated → None (caller falls back to a generic reason).
    assert f(RuntimeError("connection refused")) is None
    assert f("") is None
    assert f(None) is None


def test_report_slot_health_carries_reason(monkeypatch):
    sent = []

    class _WS:
        async def send(self, data): sent.append(json.loads(data))

    asyncio.run(tc._report_slot_health(_WS(), "extract", False,
                                       reason="access_denied", detail="fix your repo path"))
    assert sent == [{
        "type": "plugin_status", "slot": "extract", "healthy": False,
        "reason": "access_denied", "detail": "fix your repo path",
    }]


# ---------------------------------------------------------------------------
# a3410a9c — core slot watchdog escalation + background re-probe recovery
# ---------------------------------------------------------------------------

def test_run_connection_lazy_escalates_after_repeated_spawn_failures(monkeypatch):
    """Once spawn failures exceed the retry budget, the slot is reported
    unhealthy exactly once; every failed request still gets a 503."""
    sent = []
    n_requests = tc._WATCHDOG_MAX_RETRIES + 1

    class FakeWS:
        def __init__(self):
            self._msgs = [json.dumps({"type": "request", "id": str(i)}) for i in range(n_requests)]
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def __aiter__(self): return self
        async def __anext__(self):
            if not self._msgs:
                raise StopAsyncIteration
            return self._msgs.pop(0)
        async def send(self, data): sent.append(json.loads(data))

    class FakeHttpClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    import httpx as _httpx
    import websockets as _ws
    monkeypatch.setattr(_ws, "connect", lambda url, **kw: FakeWS())
    monkeypatch.setattr(_httpx, "AsyncClient", lambda *a, **k: FakeHttpClient())

    async def fake_ensure(self):  # proxy never comes up
        return None
    monkeypatch.setattr(tc.SlotProxy, "ensure_running", fake_ensure)

    proxy = tc.SlotProxy(["x"], 8808, "fs")
    asyncio.run(tc._run_connection_lazy("wss://x", proxy, "fs"))

    statuses = [m for m in sent if m.get("type") == "plugin_status"]
    assert statuses == [{"type": "plugin_status", "slot": "fs", "healthy": False}]
    assert len([m for m in sent if m.get("status") == 503]) == n_requests


def test_run_connection_lazy_reprobe_recovers(monkeypatch):
    """After escalation, the background re-probe brings the slot back and reports
    healthy once the proxy serves again."""
    monkeypatch.setattr(tc, "_SLOT_REPROBE_INTERVAL", 0.01)
    sent = []
    state = {"spawn_ok": False}
    fail_budget = tc._WATCHDOG_MAX_RETRIES + 1

    class FakeWS:
        def __init__(self):
            self._n = 0
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def __aiter__(self): return self
        async def __anext__(self):
            self._n += 1
            if self._n <= fail_budget:
                return json.dumps({"type": "request", "id": str(self._n)})
            # Escalation happened — let the proxy recover and give the re-probe
            # (0.01s) time to fire between pings, then end the stream.
            state["spawn_ok"] = True
            if self._n < fail_budget + 8:
                await asyncio.sleep(0.03)
                return json.dumps({"type": "ping"})
            raise StopAsyncIteration
        async def send(self, data): sent.append(json.loads(data))

    class FakeHttpClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    import httpx as _httpx
    import websockets as _ws
    monkeypatch.setattr(_ws, "connect", lambda url, **kw: FakeWS())
    monkeypatch.setattr(_httpx, "AsyncClient", lambda *a, **k: FakeHttpClient())
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=True))

    async def fake_ensure(self):
        if state["spawn_ok"]:
            self._proc = _FakeProc()
            self.holder["proc"] = self._proc
    monkeypatch.setattr(tc.SlotProxy, "ensure_running", fake_ensure)

    proxy = tc.SlotProxy(["x"], 8808, "fs")
    asyncio.run(tc._run_connection_lazy("wss://x", proxy, "fs"))

    statuses = [m for m in sent if m.get("type") == "plugin_status"]
    assert {"type": "plugin_status", "slot": "fs", "healthy": False} in statuses
    assert {"type": "plugin_status", "slot": "fs", "healthy": True} in statuses


# ---------------------------------------------------------------------------
# _inject_mcp_entries — non-dict top-level + non-dict mcpServers (778)
# ---------------------------------------------------------------------------

def test_inject_mcp_entries_replaces_non_dict_top_level():
    out = tc._inject_mcp_entries("[1, 2, 3]", {"meridian-fs": {"type": "http", "url": "u"}})
    data = json.loads(out)
    assert data["mcpServers"]["meridian-fs"]["url"] == "u"


def test_inject_mcp_entries_replaces_non_dict_mcpservers():
    out = tc._inject_mcp_entries(
        json.dumps({"mcpServers": "oops-a-string"}),
        {"meridian-fs": {"type": "http", "url": "u"}},
    )
    data = json.loads(out)
    assert data["mcpServers"]["meridian-fs"]["url"] == "u"


# ---------------------------------------------------------------------------
# _install_mcp_json — write failure is reported, not fatal (805-806)
# ---------------------------------------------------------------------------

def test_install_mcp_json_skips_file_on_write_error(monkeypatch, tmp_path):
    def boom_write(self, *a, **kw):
        raise OSError("read-only fs")

    monkeypatch.setattr(tc.Path, "write_text", boom_write)
    snaps = tc._install_mcp_json(tmp_path, "https://usemeridian.us", "tid")
    # The write failed for the only path → no snapshot recorded, no crash.
    assert snaps == []


def test_restore_mcp_json_tolerates_write_error(monkeypatch, tmp_path):
    mcp = tmp_path / ".mcp.json"
    # Snapshot says original existed; restore tries write_text and fails → swallowed.
    def boom_write(self, *a, **kw):
        raise OSError("nope")
    monkeypatch.setattr(tc.Path, "write_text", boom_write)
    tc._restore_mcp_json([(mcp, "original-content")])  # no raise


# ---------------------------------------------------------------------------
# _browser_auth_flow (lines 135-181)
# ---------------------------------------------------------------------------

def _patch_browser_httpx(monkeypatch, get_impl):
    """Install a fake httpx.AsyncClient whose .get runs get_impl(url, params)."""
    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, params=None, **kw):
            return await get_impl(url, params)

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)


def test_browser_auth_flow_success(monkeypatch):
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: None)

    class Resp:
        status_code = 200
        def json(self): return {"status": "complete", "token": "sk_browser"}

    async def get_impl(url, params):
        return Resp()

    _patch_browser_httpx(monkeypatch, get_impl)
    token = asyncio.run(tc._browser_auth_flow("https://x"))
    assert token == "sk_browser"


def test_browser_auth_flow_device_code_expired_404(monkeypatch):
    import webbrowser
    # webbrowser.open raising is tolerated (lines 157-158).
    monkeypatch.setattr(webbrowser, "open", lambda url: (_ for _ in ()).throw(RuntimeError("headless")))

    class Resp:
        status_code = 404

    async def get_impl(url, params):
        return Resp()

    _patch_browser_httpx(monkeypatch, get_impl)
    token = asyncio.run(tc._browser_auth_flow("https://x"))
    assert token == ""


def test_browser_auth_flow_times_out(monkeypatch):
    # NOTE: do NOT patch time.monotonic globally — the running event loop uses the
    # same module clock and patching it deadlocks the loop. Instead drive the exit
    # through asyncio.sleep raising CancelledError, which propagates out cleanly.
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: None)

    class Resp:
        status_code = 200
        def json(self): return {"status": "pending"}  # never completes

    async def get_impl(url, params):
        return Resp()

    _patch_browser_httpx(monkeypatch, get_impl)

    calls = {"n": 0}

    async def fast_sleep(n):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError  # simulate the 10-min deadline / Ctrl-C
        return None

    monkeypatch.setattr(tc.asyncio, "sleep", fast_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tc._browser_auth_flow("https://x"))
    assert calls["n"] >= 2


def test_browser_auth_flow_poll_exception_then_recovers(monkeypatch):
    """First poll raises (except→pass), second returns the token (176-177)."""
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: None)

    state = {"n": 0}

    class Resp:
        status_code = 200
        def json(self): return {"status": "complete", "token": "sk_later"}

    async def get_impl(url, params):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("transient network blip")
        return Resp()

    _patch_browser_httpx(monkeypatch, get_impl)

    async def fast_sleep(n):
        return None

    monkeypatch.setattr(tc.asyncio, "sleep", fast_sleep)
    token = asyncio.run(tc._browser_auth_flow("https://x"))
    assert token == "sk_later"


# ---------------------------------------------------------------------------
# run_tunnel — early-exit paths
# ---------------------------------------------------------------------------

def _run_tunnel(**kw):
    return asyncio.run(tc.run_tunnel(**kw))


def test_run_tunnel_returns_2_when_no_token(monkeypatch):
    monkeypatch.setattr(tc, "_force_utf8_io", lambda: None)
    monkeypatch.setattr(tc, "_resolve_token", lambda t: "")
    monkeypatch.setattr(tc, "_read_cached_token", lambda b: None)
    monkeypatch.setattr(tc, "_browser_auth_flow", AsyncMock(return_value=""))
    rc = _run_tunnel(token=None, base_url="https://x", repo_path=str(tc.Path.home()))
    assert rc == 2


def test_run_tunnel_me_failure_returns_1(monkeypatch):
    monkeypatch.setattr(tc, "_force_utf8_io", lambda: None)
    monkeypatch.setattr(tc, "_resolve_token", lambda t: "sk_tok")
    monkeypatch.setattr(tc, "_fetch_me", AsyncMock(side_effect=RuntimeError("boom")))
    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tc.Path.home()))
    assert rc == 1


def test_run_tunnel_no_tenant_id_returns_1(monkeypatch):
    monkeypatch.setattr(tc, "_force_utf8_io", lambda: None)
    monkeypatch.setattr(tc, "_resolve_token", lambda t: "sk_tok")
    monkeypatch.setattr(tc, "_fetch_me", AsyncMock(return_value={"plan": "pro"}))
    rc = _run_tunnel(token="sk_tok", base_url="https://x")
    assert rc == 1


def test_run_tunnel_non_pro_plan_returns_1(monkeypatch):
    monkeypatch.setattr(tc, "_force_utf8_io", lambda: None)
    monkeypatch.setattr(tc, "_resolve_token", lambda t: "sk_tok")
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={"tenant_id": "t1", "plan": "free"}),
    )
    rc = _run_tunnel(token="sk_tok", base_url="https://x")
    assert rc == 1


def test_run_tunnel_cached_token_rejected_then_browser_fails(monkeypatch):
    """Cached token path: /me fails, re-auth via browser also cancelled → 2."""
    monkeypatch.setattr(tc, "_force_utf8_io", lambda: None)
    monkeypatch.setattr(tc, "_resolve_token", lambda t: "")
    monkeypatch.setattr(tc, "_read_cached_token", lambda b: "sk_cached")
    monkeypatch.setattr(tc, "_fetch_me", AsyncMock(side_effect=RuntimeError("401")))
    monkeypatch.setattr(tc, "_browser_auth_flow", AsyncMock(return_value=""))
    rc = _run_tunnel(token=None, base_url="https://x")
    assert rc == 2


def test_run_tunnel_cached_rejected_browser_ok_then_me_fails_returns_1(monkeypatch):
    """Cached rejected → browser succeeds → second /me still fails → exit 1."""
    monkeypatch.setattr(tc, "_force_utf8_io", lambda: None)
    monkeypatch.setattr(tc, "_resolve_token", lambda t: "")
    monkeypatch.setattr(tc, "_read_cached_token", lambda b: "sk_cached")
    monkeypatch.setattr(tc, "_fetch_me", AsyncMock(side_effect=RuntimeError("nope")))
    monkeypatch.setattr(tc, "_browser_auth_flow", AsyncMock(return_value="sk_new"))
    rc = _run_tunnel(token=None, base_url="https://x")
    assert rc == 1


# ---------------------------------------------------------------------------
# d1c528f5 — Node.js gate + fnm auto-install
# ---------------------------------------------------------------------------

def test_check_node_true_when_both_present(monkeypatch):
    monkeypatch.setattr(tc.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert tc._check_node() is True


def test_check_node_false_when_node_missing(monkeypatch):
    monkeypatch.setattr(tc.shutil, "which", lambda name: None if name == "node" else "/x/npx")
    assert tc._check_node() is False


def test_ensure_node_present_returns_true(monkeypatch):
    monkeypatch.setattr(tc, "_check_node", lambda: True)
    assert tc._ensure_node(False) is True


def test_ensure_node_missing_no_autoinstall_returns_false(monkeypatch, capsys):
    monkeypatch.setattr(tc, "_check_node", lambda: False)
    assert tc._ensure_node(False) is False
    err = capsys.readouterr().err
    assert "Node.js" in err and "required" in err


def test_ensure_node_autoinstall_success(monkeypatch):
    monkeypatch.setattr(tc, "_check_node", lambda: False)
    monkeypatch.setattr(tc, "_install_node_via_fnm", lambda: True)
    assert tc._ensure_node(True) is True


def test_ensure_node_autoinstall_failure_returns_false(monkeypatch):
    monkeypatch.setattr(tc, "_check_node", lambda: False)
    monkeypatch.setattr(tc, "_install_node_via_fnm", lambda: False)
    assert tc._ensure_node(True) is False


def test_install_node_via_fnm_no_fnm_no_installer_returns_false(monkeypatch):
    # No fnm, no winget/scoop/curl/bash available → cannot install.
    monkeypatch.setattr(tc.shutil, "which", lambda name: None)
    assert tc._install_node_via_fnm() is False


def test_run_tunnel_node_missing_returns_1(monkeypatch):
    """No Node + auto-install off → run_tunnel exits 1 instead of spawning
    broken proxies."""
    monkeypatch.setattr(tc, "_force_utf8_io", lambda: None)
    monkeypatch.setattr(tc, "_resolve_token", lambda t: "sk_tok")
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={"tenant_id": "t1", "plan": "pro"}),
    )
    monkeypatch.setattr(tc, "_write_cached_token", lambda *a, **k: None)
    # b970fe07 — _fetch_filesystem_roots now returns a 4-tuple
    # (fs_roots, known_repo_paths, serena_repo_path, codebase_code_dirs).
    monkeypatch.setattr(tc, "_fetch_filesystem_roots", AsyncMock(return_value=([], [], "", [])))
    monkeypatch.setattr(tc, "_ensure_node", lambda auto: False)
    rc = _run_tunnel(token="sk_tok", base_url="https://x")
    assert rc == 1


# ---------------------------------------------------------------------------
# run_tunnel — happy-ish path: all slots up, reconnect loops short-circuit
# ---------------------------------------------------------------------------

def _stub_run_tunnel_spawn(monkeypatch, *, code_binary="/bin/codebase-memory-mcp",
                           extractor_inner=None):
    """Patch out all real I/O so run_tunnel can run end-to-end synchronously."""
    monkeypatch.setattr(tc, "_force_utf8_io", lambda: None)
    monkeypatch.setattr(tc, "_resolve_token", lambda t: "sk_tok")
    monkeypatch.setattr(tc, "_find_npx", lambda: "npx")
    # d1c528f5 — pretend Node is present so the gate doesn't short-circuit.
    monkeypatch.setattr(tc, "_ensure_node", lambda auto: True)
    monkeypatch.setattr(tc, "_ensure_codebase_memory_mcp",
                        AsyncMock(return_value=code_binary))
    monkeypatch.setattr(
        tc, "_resolve_extractor_inner_cmd",
        lambda: extractor_inner if extractor_inner is not None
        else ["uvx", "mcp-server-code-extractor"],
    )

    procs = []

    class FakeProc:
        def __init__(self, cmd):
            self.cmd = cmd
            procs.append(self)
        def poll(self): return None  # alive (so SlotProxy.is_running is True)
        def terminate(self): pass
        def wait(self, timeout=None): return 0
        def kill(self): pass

    monkeypatch.setattr(tc.subprocess, "Popen", lambda cmd, *a, **k: FakeProc(cmd))

    # Lazy spawn (3649a61a): built-in slots are SlotProxy objects whose proxy is
    # NOT Popen'd at startup — it spawns on the first request via ensure_running().
    # Patch ensure_running to spawn synchronously (no 1s port-bind sleep, no real
    # subprocess) so the `len(procs)` assertions still observe one Popen per slot.
    async def fake_ensure_running(self):
        if self.is_running:
            return
        self._proc = tc.subprocess.Popen(self.cmd, env=self.env)
        self.holder["proc"] = self._proc
        self.touch()

    monkeypatch.setattr(tc.SlotProxy, "ensure_running", fake_ensure_running)

    # Lazy reconnect loops spawn the proxy on connect (mirroring the first-request
    # behaviour) then return — no real WS. The idle-killer is a no-op. The legacy
    # watchdog (still used for eager custom plugins) also returns immediately.
    async def fake_reconnect_lazy(ws_url, proxy, label, tool_prefix=None, known_repo_paths=None):
        await proxy.ensure_running()
        return None

    async def fake_idle_killer(proxy, idle_seconds=tc._IDLE_KILL_SECONDS):
        return None

    async def fake_watchdog(holder, poll_interval=3.0):
        return None

    # 64650cb4 — the default-Serena extract slot is now a SerenaDaemonPool, not a
    # SlotProxy. Its reconnect loop spawns the default-repo daemon (one Popen via
    # the pool, matching the per-slot proc accounting); the idle reaper is a no-op.
    async def fake_reconnect_extract_pool(ws_url, pool, repo_path, label="extract", tool_prefix=None):
        pool.get_or_spawn(repo_path)
        return None

    async def fake_pool_reaper(pool, idle_seconds=tc._IDLE_KILL_SECONDS):
        return None

    monkeypatch.setattr(tc, "_reconnect_loop_lazy", fake_reconnect_lazy)
    monkeypatch.setattr(tc, "_idle_killer", fake_idle_killer)
    monkeypatch.setattr(tc, "_proc_watchdog", fake_watchdog)
    monkeypatch.setattr(tc, "_reconnect_loop_extract_pool", fake_reconnect_extract_pool)
    monkeypatch.setattr(tc, "_pool_idle_reaper", fake_pool_reaper)
    return procs


def test_run_tunnel_extra_fs_roots_union(monkeypatch, tmp_path):
    """cbbd0eb4 — extra_fs_roots are resolved + unioned into the fs proxy roots."""
    _stub_run_tunnel_spawn(monkeypatch)
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={"tenant_id": "tid-x", "plan": "pro"}),
    )
    monkeypatch.setattr(
        tc, "_fetch_filesystem_roots",
        # b970fe07 — 4-tuple (fs_roots, known_repo_paths, serena_repo_path, code_dirs).
        AsyncMock(return_value=(["/server/root"], [], "", [])),
    )
    captured = {}
    real_build = tc._build_proxy_command

    def cap_build(npx, repo_path, port=tc.DEFAULT_PROXY_PORT, roots=None):
        captured["roots"] = roots
        return real_build(npx, repo_path, port, roots=roots)

    monkeypatch.setattr(tc, "_build_proxy_command", cap_build)
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))
    outputs = tmp_path / "Outputs"
    outputs.mkdir()

    rc = _run_tunnel(token="sk", base_url="https://x",
                     repo_path=str(tmp_path), extra_fs_roots=[str(outputs)])
    assert rc == 0
    roots = captured["roots"]
    assert roots is not None
    assert "/server/root" in roots                       # server roots preserved
    assert any("Outputs" in r for r in roots)            # extra root unioned in


def _capture_serena_and_index(monkeypatch):
    """Capture SerenaDaemonPool default_repo_path + _index_code_dir(dir) calls.

    Returns a dict populated during a run_tunnel call: ``serena`` (the pool's
    default_repo_path) and ``indexed`` (list of dirs handed to _index_code_dir).
    """
    captured: dict = {"serena": None, "indexed": []}
    real_pool = tc.SerenaDaemonPool

    def cap_pool(*a, **k):
        captured["serena"] = k.get("default_repo_path")
        return real_pool(*a, **k)

    monkeypatch.setattr(tc, "SerenaDaemonPool", cap_pool)

    async def fake_index(port, code_dir):
        captured["indexed"].append(code_dir)

    monkeypatch.setattr(tc, "_index_code_dir", fake_index)
    return captured


def test_run_tunnel_config_serena_and_code_dirs_applied(monkeypatch, tmp_path):
    """b970fe07 fall-back-safe: with no --repo and no --code-dir, the dashboard
    serena_repo_path drives the Serena pool default and codebase_code_dirs get
    auto-indexed."""
    _stub_run_tunnel_spawn(monkeypatch)
    captured = _capture_serena_and_index(monkeypatch)
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={"tenant_id": "tid-cfg", "plan": "pro"}),
    )
    serena_dir = tmp_path / "serena_repo"
    code_dir = tmp_path / "code_repo"
    serena_dir.mkdir()
    code_dir.mkdir()
    monkeypatch.setattr(
        tc, "_fetch_filesystem_roots",
        AsyncMock(return_value=([], [], str(serena_dir), [str(code_dir)])),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    # repo_path=None / code_dirs=None → CLI flags absent → config wins.
    rc = _run_tunnel(token="sk", base_url="https://x", repo_path=None, code_dirs=None)
    assert rc == 0
    assert captured["serena"] == str(serena_dir.resolve())
    assert captured["indexed"] == [str(code_dir.resolve())]


def test_run_tunnel_cli_flags_override_config(monkeypatch, tmp_path):
    """b970fe07 fall-back-safe: when --repo and --code-dir ARE passed, the CLI
    wins and the dashboard config is ignored."""
    _stub_run_tunnel_spawn(monkeypatch)
    captured = _capture_serena_and_index(monkeypatch)
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={"tenant_id": "tid-cli", "plan": "pro"}),
    )
    cli_repo = tmp_path / "cli_repo"
    cli_code = tmp_path / "cli_code"
    cfg_serena = tmp_path / "cfg_serena"
    cfg_code = tmp_path / "cfg_code"
    for d in (cli_repo, cli_code, cfg_serena, cfg_code):
        d.mkdir()
    monkeypatch.setattr(
        tc, "_fetch_filesystem_roots",
        AsyncMock(return_value=([], [], str(cfg_serena), [str(cfg_code)])),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    rc = _run_tunnel(
        token="sk", base_url="https://x",
        repo_path=str(cli_repo), code_dirs=[str(cli_code)],
    )
    assert rc == 0
    # CLI repo wins for Serena default; config serena_repo_path ignored.
    assert captured["serena"] == str(cli_repo.resolve())
    # CLI --code-dir wins; config codebase_code_dirs ignored.
    assert captured["indexed"] == [str(cli_code.resolve())]


def test_run_tunnel_no_config_reproduces_cwd_default(monkeypatch, tmp_path):
    """b970fe07 fall-back-safe: absent config + no flags → Serena default is the
    cwd (today's exact behaviour) and nothing is auto-indexed."""
    _stub_run_tunnel_spawn(monkeypatch)
    captured = _capture_serena_and_index(monkeypatch)
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={"tenant_id": "tid-def", "plan": "pro"}),
    )
    monkeypatch.setattr(
        tc, "_fetch_filesystem_roots",
        AsyncMock(return_value=([], [], "", [])),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    rc = _run_tunnel(token="sk", base_url="https://x", repo_path=None, code_dirs=None)
    assert rc == 0
    assert captured["serena"] == str(tmp_path.resolve())   # cwd default, unchanged
    assert captured["indexed"] == []                        # no auto-index


def test_run_tunnel_full_path_all_slots(monkeypatch, tmp_path):
    procs = _stub_run_tunnel_spawn(monkeypatch)
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={"tenant_id": "tid-9", "plan": "pro"}),
    )
    # Keep .mcp.json writes inside tmp_path.
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    rc = _run_tunnel(token="sk_tok", base_url="https://x",
                     repo_path=str(tmp_path))
    assert rc == 0
    # fs + code + extract proxies were all spawned.
    assert len(procs) == 3
    # .mcp.json was created then restored (removed) on shutdown.
    assert not (tmp_path / ".mcp.json").exists()


def test_run_tunnel_admin_plan_with_disabled_slots(monkeypatch, tmp_path):
    procs = _stub_run_tunnel_spawn(monkeypatch)
    # admin plan, fs enabled but code + extract disabled via plugin config.
    plugins = [
        {"slot": "fs", "enabled": True},
        {"slot": "code", "enabled": False},
        {"slot": "extract", "enabled": False},
    ]
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={
            "tenant_id": "tid-admin", "plan": "admin",
            "tunnel_plugins": plugins,
        }),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tmp_path))
    assert rc == 0
    # Only the filesystem proxy spawned.
    assert len(procs) == 1


def test_run_tunnel_all_slots_disabled_returns_1(monkeypatch, tmp_path):
    _stub_run_tunnel_spawn(monkeypatch)
    plugins = [
        {"slot": "fs", "enabled": False},
        {"slot": "code", "enabled": False},
        {"slot": "extract", "enabled": False},
    ]
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={
            "tenant_id": "t", "plan": "pro", "tunnel_plugins": plugins,
        }),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))
    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tmp_path))
    # No tasks → "nothing to serve" → exit 1 (line 1070-1072).
    assert rc == 1


def test_plugin_spawn_env_merges_over_parent_and_coerces(monkeypatch):
    # 194a7776 — plugin env overrides merge over the parent process env; keys
    # and values are coerced to str.
    monkeypatch.setenv("PARENT_ONLY", "keep")
    out = tc._plugin_spawn_env({"ZOTERO_LOCAL": "true", "N": 5})
    assert out is not None
    assert out["ZOTERO_LOCAL"] == "true"
    assert out["N"] == "5"
    assert out["PARENT_ONLY"] == "keep"


def test_plugin_spawn_env_none_when_nothing_to_override():
    assert tc._plugin_spawn_env(None) is None
    assert tc._plugin_spawn_env({}) is None
    assert tc._plugin_spawn_env("nope") is None
    assert tc._plugin_spawn_env({"": "blankkey"}) is None


def test_run_tunnel_command_overrides_and_index(monkeypatch, tmp_path):
    """Custom commands for all three slots + code_dirs auto-index."""
    procs = _stub_run_tunnel_spawn(monkeypatch)
    plugins = [
        {"slot": "fs", "enabled": True, "command": ["fs-custom"], "port": 9001},
        {"slot": "code", "enabled": True, "command": ["codegraph"], "port": 9002},
        {"slot": "extract", "enabled": True, "command": ["ext-custom"], "port": 9003},
    ]
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={
            "tenant_id": "tid-c", "plan": "pro", "tunnel_plugins": plugins,
        }),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    indexed = []

    async def fake_index(port, code_dir):
        indexed.append((port, code_dir))

    monkeypatch.setattr(tc, "_index_code_dir", fake_index)

    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tmp_path),
                     code_dirs=[str(tmp_path)])
    assert rc == 0
    assert len(procs) == 3
    # code-intel index was scheduled for the provided code_dir on its port.
    assert indexed and indexed[0][0] == 9002


def test_run_tunnel_fs_lazy_spawn_enoent_keeps_tunnel_up(monkeypatch, tmp_path):
    """Lazy spawn (3649a61a) changes fs-failure semantics: under eager spawn an
    fs npx ENOENT aborted startup (exit 1); now the Popen is deferred to the first
    request inside SlotProxy.ensure_running, whose try/except swallows ENOENT. The
    proxy simply never comes up and the tunnel stays alive (exit 0), retrying on a
    later request rather than crashing at boot."""
    # Set up the I/O stubs manually (NOT via _stub_run_tunnel_spawn, which would
    # replace the real ensure_running we want to exercise here).
    monkeypatch.setattr(tc, "_force_utf8_io", lambda: None)
    monkeypatch.setattr(tc, "_resolve_token", lambda t: "sk_tok")
    monkeypatch.setattr(tc, "_find_npx", lambda: "npx")
    monkeypatch.setattr(tc, "_ensure_codebase_memory_mcp",
                        AsyncMock(return_value="/bin/codebase-memory-mcp"))
    monkeypatch.setattr(tc, "_resolve_extractor_inner_cmd",
                        lambda: ["uvx", "mcp-server-code-extractor"])
    # Skip ensure_running's 1s port-bind sleep on a successful spawn.
    monkeypatch.setattr(tc.asyncio, "sleep", AsyncMock(return_value=None))

    # Lazy reconnect drives the REAL ensure_running once then returns; idle-killer
    # + legacy watchdog are no-ops.
    async def fake_reconnect_lazy(ws_url, proxy, label, tool_prefix=None, known_repo_paths=None):
        await proxy.ensure_running()
        return None

    async def fake_idle_killer(proxy, idle_seconds=tc._IDLE_KILL_SECONDS):
        return None

    async def fake_watchdog(holder, poll_interval=3.0):
        return None

    # 64650cb4 — pooled extract slot: no-op so the run_tunnel task gather returns.
    async def fake_reconnect_extract_pool(ws_url, pool, repo_path, label="extract", tool_prefix=None):
        return None

    async def fake_pool_reaper(pool, idle_seconds=tc._IDLE_KILL_SECONDS):
        return None

    monkeypatch.setattr(tc, "_reconnect_loop_lazy", fake_reconnect_lazy)
    monkeypatch.setattr(tc, "_idle_killer", fake_idle_killer)
    monkeypatch.setattr(tc, "_proc_watchdog", fake_watchdog)
    monkeypatch.setattr(tc, "_reconnect_loop_extract_pool", fake_reconnect_extract_pool)
    monkeypatch.setattr(tc, "_pool_idle_reaper", fake_pool_reaper)

    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={"tenant_id": "t", "plan": "pro"}),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    def popen_enoent(cmd, *a, **k):
        raise FileNotFoundError("npx missing")

    monkeypatch.setattr(tc.subprocess, "Popen", popen_enoent)
    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tmp_path))
    # fs lazy-spawn ENOENT is swallowed by ensure_running → tunnel stays up.
    assert rc == 0


def test_run_tunnel_code_unavailable_and_extract_disabled(monkeypatch, tmp_path):
    """code-intel binary unavailable + extract slot disabled via config → both
    skip; fs alone keeps the tunnel alive (exit 0). (The extract slot now defaults
    to Serena, so 'unavailable' is expressed by disabling it, not a None resolver.)"""
    procs = _stub_run_tunnel_spawn(monkeypatch, code_binary=None)
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={
            "tenant_id": "t", "plan": "pro",
            "tunnel_plugins_config": {"code-extractor": {"enabled": False}},
        }),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tmp_path))
    # Only fs spawns; code unavailable + extract disabled but fs keeps tunnel → 0.
    assert rc == 0
    assert len(procs) == 1


def test_run_tunnel_legacy_resolved_list_extract_none_skips(monkeypatch, tmp_path):
    """Backward-compat: an older server sends an already-resolved plugin list with
    code-extractor command=None. With the resolver returning None the extract slot
    falls through and skips cleanly (fs alone → exit 0)."""
    procs = _stub_run_tunnel_spawn(monkeypatch, code_binary=None)
    monkeypatch.setattr(tc, "_resolve_extractor_inner_cmd", lambda: None)
    legacy_plugins = [
        {"name": "filesystem", "slot": "fs", "enabled": True, "command": None, "port": 8808},
        {"name": "code-extractor", "slot": "extract", "enabled": True, "command": None, "port": 8810},
    ]
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={"tenant_id": "t", "plan": "pro", "tunnel_plugins": legacy_plugins}),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tmp_path))
    # fs spawns; extract command=None + resolver None → skip.
    assert rc == 0
    assert len(procs) == 1


def test_run_tunnel_browser_auth_success_caches_token(monkeypatch, tmp_path):
    """No token/cache → browser auth succeeds → token cached after /me ok."""
    _stub_run_tunnel_spawn(monkeypatch)
    monkeypatch.setattr(tc, "_resolve_token", lambda t: "")
    monkeypatch.setattr(tc, "_read_cached_token", lambda b: None)
    monkeypatch.setattr(tc, "_browser_auth_flow", AsyncMock(return_value="sk_browser"))
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={"tenant_id": "t", "plan": "pro"}),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))
    written = {}
    monkeypatch.setattr(
        tc, "_write_cached_token",
        lambda base, tok: written.update(base=base, tok=tok),
    )
    rc = _run_tunnel(token=None, base_url="https://x", repo_path=str(tmp_path))
    assert rc == 0
    # Browser-authed token was persisted after /me confirmed it.
    assert written == {"base": "https://x", "tok": "sk_browser"}


def test_run_tunnel_code_and_extract_popen_raise_are_warned(monkeypatch, tmp_path):
    """code-intel + extractor lazy-spawn Popen raising → SlotProxy.ensure_running
    warning branch (proc stays None), but fs proxy keeps the tunnel alive (exit 0).

    Under lazy spawning (3649a61a) the Popen happens inside ensure_running, not at
    startup — so this drives the real ensure_running and asserts its try/except
    swallows the spawn failure for the code/extract slots."""
    monkeypatch.setattr(tc, "_force_utf8_io", lambda: None)
    monkeypatch.setattr(tc, "_resolve_token", lambda t: "sk_tok")
    monkeypatch.setattr(tc, "_find_npx", lambda: "npx")
    monkeypatch.setattr(tc, "_ensure_codebase_memory_mcp",
                        AsyncMock(return_value="/bin/codebase-memory-mcp"))
    monkeypatch.setattr(tc, "_resolve_extractor_inner_cmd",
                        lambda: ["uvx", "mcp-server-code-extractor"])
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={"tenant_id": "t", "plan": "pro"}),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))
    # Skip the 1s port-bind sleep ensure_running does after a successful spawn.
    monkeypatch.setattr(tc.asyncio, "sleep", AsyncMock(return_value=None))

    # Lazy reconnect loops drive the real ensure_running (to hit its spawn-failure
    # branch) then return; idle-killer + legacy watchdog are no-ops.
    async def fake_reconnect_lazy(ws_url, proxy, label, tool_prefix=None, known_repo_paths=None):
        await proxy.ensure_running()
        return None

    async def fake_idle_killer(proxy, idle_seconds=tc._IDLE_KILL_SECONDS):
        return None

    async def fake_watchdog(holder, poll_interval=3.0):
        return None

    # 64650cb4 — pooled extract slot: no-op (spawn failures are handled per-request
    # at 503, not at startup, so the default extract spawns nothing here).
    async def fake_reconnect_extract_pool(ws_url, pool, repo_path, label="extract", tool_prefix=None):
        return None

    async def fake_pool_reaper(pool, idle_seconds=tc._IDLE_KILL_SECONDS):
        return None

    monkeypatch.setattr(tc, "_reconnect_loop_lazy", fake_reconnect_lazy)
    monkeypatch.setattr(tc, "_idle_killer", fake_idle_killer)
    monkeypatch.setattr(tc, "_proc_watchdog", fake_watchdog)
    monkeypatch.setattr(tc, "_reconnect_loop_extract_pool", fake_reconnect_extract_pool)
    monkeypatch.setattr(tc, "_pool_idle_reaper", fake_pool_reaper)

    class FakeProc:
        def poll(self): return None  # alive
        def terminate(self): pass
        def wait(self, timeout=None): return 0
        def kill(self): pass

    def popen(cmd, *a, **k):
        # fs proxy (server-filesystem) starts; code-intel + extractor blow up.
        if "@modelcontextprotocol/server-filesystem" in cmd:
            return FakeProc()
        raise RuntimeError("proxy spawn failed")

    monkeypatch.setattr(tc.subprocess, "Popen", popen)

    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tmp_path))
    assert rc == 0  # fs alone keeps the tunnel up


def test_tunnel_mcp_entries_includes_custom_local_proxies():
    """Custom plugins get a LOCAL http://127.0.0.1:<port>/mcp connector entry,
    alongside the three built-in relay entries (which keep the hosted URL)."""
    entries = tc._tunnel_mcp_entries(
        "https://usemeridian.us", "tid-x",
        custom=[{"name": "fetch", "port": 8901}, {"name": "git", "port": 8902}],
    )
    # Built-ins still point at the hosted relay.
    assert entries["meridian-fs"]["url"].startswith("https://usemeridian.us/fs/mcp/")
    # Custom ones point at the local proxy, NOT the hosted server.
    assert entries["meridian-custom-fetch"] == {"type": "http", "url": "http://127.0.0.1:8901/mcp"}
    assert entries["meridian-custom-git"] == {"type": "http", "url": "http://127.0.0.1:8902/mcp"}


def test_run_tunnel_spawns_custom_plugin_locally_and_writes_mcp_json(monkeypatch, tmp_path):
    """A custom plugin in the config spawns an extra local proxy (proc_holders
    gains a custom:<name> entry) and is written into the local .mcp.json pointing
    at its 127.0.0.1 proxy — never a hosted server route (LOCAL-ONLY)."""
    procs = _stub_run_tunnel_spawn(monkeypatch)
    # Capture every watchdog holder so we can assert the custom slot is covered.
    seen_labels = []

    async def fake_watchdog(holder, poll_interval=3.0):
        seen_labels.append(holder.get("label"))
        return None

    monkeypatch.setattr(tc, "_proc_watchdog", fake_watchdog)
    # Don't restore (delete) the .mcp.json on shutdown so we can inspect it.
    monkeypatch.setattr(tc, "_restore_mcp_json", lambda snaps: None)

    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={
            "tenant_id": "tid-cust", "plan": "pro",
            "tunnel_plugins_config": [
                {"name": "fetch", "command": "uvx mcp-server-fetch", "port": 8901},
            ],
        }),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tmp_path))
    assert rc == 0
    # fs + code + extract built-ins + the one custom proxy = 4 spawns.
    assert len(procs) == 4
    # The custom inner command was wrapped + spawned (mcp-proxy on its port).
    assert any("mcp-server-fetch" in p.cmd for p in procs)
    assert any("8901" in p.cmd for p in procs)
    # proc_holders gained a custom:<name> entry (so the watchdog covers it).
    assert "custom:fetch" in seen_labels
    # .mcp.json got the local connector entry pointing at the 127.0.0.1 proxy.
    data = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["meridian-custom-fetch"]["url"] == "http://127.0.0.1:8901/mcp"


def test_run_tunnel_custom_plugin_repo_path_expanded_at_spawn(monkeypatch, tmp_path):
    """{repo_path} in a custom plugin command is expanded to the served repo at
    spawn time (resolve_custom_plugins leaves it intact; the client expands it)."""
    procs = _stub_run_tunnel_spawn(monkeypatch)
    monkeypatch.setattr(tc, "_restore_mcp_json", lambda snaps: None)
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={
            "tenant_id": "tid-rp", "plan": "pro",
            "tunnel_plugins_config": [
                {"name": "myserena", "command": "tool --project {repo_path}", "port": 8902},
            ],
        }),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tmp_path))
    assert rc == 0
    # The literal {repo_path} placeholder must not survive into the spawned command.
    custom_cmds = [p.cmd for p in procs if any("8902" in str(t) for t in p.cmd)]
    assert custom_cmds, "custom proxy was not spawned"
    flat = " ".join(custom_cmds[0])
    assert "{repo_path}" not in flat
    assert str(tmp_path) in flat


def test_run_tunnel_custom_only_keeps_tunnel_alive(monkeypatch, tmp_path):
    """With every built-in slot disabled but one custom plugin enabled, the tunnel
    still serves (the local proxy + mcp.json) — exit 0, not 'nothing to serve'."""
    procs = _stub_run_tunnel_spawn(monkeypatch)
    monkeypatch.setattr(tc, "_restore_mcp_json", lambda snaps: None)
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={
            "tenant_id": "tid-co", "plan": "pro",
            "tunnel_plugins_config": [
                {"name": "filesystem", "enabled": False},
                {"name": "code-intel", "enabled": False},
                {"name": "code-extractor", "enabled": False},
                {"name": "fetch", "command": "uvx mcp-server-fetch", "port": 8901},
            ],
        }),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tmp_path))
    assert rc == 0
    # Only the custom proxy spawned (all built-ins disabled).
    assert len(procs) == 1
    assert any("mcp-server-fetch" in p.cmd for p in procs)


def test_run_tunnel_finally_kills_proc_on_wait_timeout(monkeypatch, tmp_path):
    """On shutdown, a proc whose .wait() times out is force-killed (finally block)."""
    _stub_run_tunnel_spawn(monkeypatch)
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={"tenant_id": "t", "plan": "pro"}),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    killed = {"n": 0}

    class TimeoutProc:
        def terminate(self): pass
        def wait(self, timeout=None): raise RuntimeError("wait timed out")
        def kill(self): killed["n"] += 1

    monkeypatch.setattr(tc.subprocess, "Popen", lambda cmd, *a, **k: TimeoutProc())

    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tmp_path))
    assert rc == 0
    # Every started proc (fs+code+extract) had .kill() invoked after wait timeout.
    assert killed["n"] >= 1
