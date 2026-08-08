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
import os
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from meridian import tunnel_client as tc


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    """Build a real httpx.HTTPStatusError, e.g. to simulate a 401 from /me."""
    request = httpx.Request("GET", "https://x/me")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(str(status_code), request=request, response=response)


async def _tolerate_never_ready(coro):
    """39c8cf2c — a handful of pre-existing tests in this file drive
    _run_connection/_run_connection_lazy with a FakeWS that closes having
    delivered zero messages, purely as inert scaffolding for asserting on
    something else entirely (e.g. the kwargs passed to websockets.connect).
    That shape is now indistinguishable from a genuine "opened then closed
    before ever becoming ready" connection, so those two functions correctly
    raise TunnelNeverReadyError for it (see tests/test_tunnel_client.py for
    dedicated behavior coverage of that new exception). Swallow it here so
    tests whose actual assertions happened before the raise are unaffected."""
    try:
        await coro
    except tc._tunnel_lifecycle.TunnelNeverReadyError:
        pass


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
# 676e53a3 — explicit ping_timeout on every websockets.connect() call site
# ---------------------------------------------------------------------------

def test_websocket_connect_sites_set_explicit_ping_timeout(monkeypatch):
    """All three tunnel connect() call sites (_run_connection,
    _run_connection_lazy, _run_extract_pool_connection) must pass an explicit,
    more-generous ping_timeout rather than relying on the websockets library
    default (~20s) — a single delayed pong under load must not make the
    CLIENT unilaterally close ("no close frame received or sent")."""
    captured: list[dict] = []

    class FakeWS:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        def __aiter__(self):
            return self
        async def __anext__(self):
            raise StopAsyncIteration  # no messages — connect() then exit cleanly

    class FakeHttpClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass

    def fake_connect(url, **kw):
        captured.append(kw)
        return FakeWS()

    import httpx as _httpx
    import websockets as _ws
    monkeypatch.setattr(_ws, "connect", fake_connect)
    monkeypatch.setattr(_httpx, "AsyncClient", lambda *a, **k: FakeHttpClient())

    # 39c8cf2c — a FakeWS delivering zero messages before closing is exactly
    # the "opened then closed before ever becoming ready" case _run_connection
    # / _run_connection_lazy now correctly refuse to call a successful
    # "connected" (see test_tunnel_client_readiness_gating.py-equivalent
    # coverage in tests/test_tunnel_client.py for the dedicated behavior
    # tests). This test only cares about the kwargs each connect() call site
    # was given, which `fake_connect` already captured before either function
    # got a chance to raise — tolerate the new exception here.
    asyncio.run(_tolerate_never_ready(tc._run_connection("wss://x/tunnel/t", 8808, "fs")))

    proxy = tc.SlotProxy(["x"], 8808, "fs")
    asyncio.run(_tolerate_never_ready(
        tc._run_connection_lazy("wss://x/tunnel/t", proxy, "fs")
    ))

    pool = MagicMock()
    asyncio.run(
        tc._run_extract_pool_connection("wss://x/tunnel/t", pool, "/repo", "extract")
    )

    assert len(captured) == 3, "expected exactly 3 websockets.connect() call sites"
    for kw in captured:
        assert kw.get("ping_interval") == 20
        assert kw.get("ping_timeout") == tc._WS_PING_TIMEOUT
    # The chosen timeout must be more generous than the ping interval itself,
    # and comfortably clear the local relay request budget (28s) so a slow
    # but alive relay never gets mistaken for a dead peer.
    assert tc._WS_PING_TIMEOUT > 20
    assert tc._WS_PING_TIMEOUT >= tc._LOCAL_REQUEST_TIMEOUT


# ---------------------------------------------------------------------------
# 0b3ea61a — explicit open_timeout on every websockets.connect() call site
# ---------------------------------------------------------------------------

def test_tunnel_connect_timeout_env_resolution(monkeypatch):
    """_tunnel_connect_timeout() mirrors _run_cmd_timeout()'s env-var-with-
    fallback convention: MERIDIAN_TUNNEL_CONNECT_TIMEOUT overrides the 30.0s
    default, and an invalid/non-numeric value falls back rather than raising."""
    monkeypatch.delenv("MERIDIAN_TUNNEL_CONNECT_TIMEOUT", raising=False)
    assert tc._tunnel_connect_timeout() == 30.0

    monkeypatch.setenv("MERIDIAN_TUNNEL_CONNECT_TIMEOUT", "45")
    assert tc._tunnel_connect_timeout() == 45.0

    monkeypatch.setenv("MERIDIAN_TUNNEL_CONNECT_TIMEOUT", "not-a-number")
    assert tc._tunnel_connect_timeout() == 30.0

    monkeypatch.setenv("MERIDIAN_TUNNEL_CONNECT_TIMEOUT", "0")
    assert tc._tunnel_connect_timeout() == 30.0  # non-positive → fallback

    monkeypatch.setenv("MERIDIAN_TUNNEL_CONNECT_TIMEOUT", "-5")
    assert tc._tunnel_connect_timeout() == 30.0  # negative → fallback


def test_websocket_connect_sites_set_explicit_open_timeout(monkeypatch):
    """0b3ea61a — all three tunnel connect() call sites (_run_connection,
    _run_connection_lazy, _run_extract_pool_connection) must pass an explicit
    open_timeout rather than silently relying on the websockets library
    default (10s) for the CONNECT handshake itself. That default sits earlier
    in the sequence than ping/pong (676e53a3) or any relayed request, and is
    NOT reachable by MCP_TIMEOUT (a `claude` CLI env var, unrelated to this
    process) — a cold Fly.io machine wake can plausibly exceed it, flatly
    failing a connect attempt that would have succeeded moments later."""
    captured: list[dict] = []

    class FakeWS:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        def __aiter__(self):
            return self
        async def __anext__(self):
            raise StopAsyncIteration  # no messages — connect() then exit cleanly

    class FakeHttpClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass

    def fake_connect(url, **kw):
        captured.append(kw)
        return FakeWS()

    import httpx as _httpx
    import websockets as _ws
    monkeypatch.setattr(_ws, "connect", fake_connect)
    monkeypatch.setattr(_httpx, "AsyncClient", lambda *a, **k: FakeHttpClient())
    monkeypatch.setenv("MERIDIAN_TUNNEL_CONNECT_TIMEOUT", "37")

    asyncio.run(_tolerate_never_ready(tc._run_connection("wss://x/tunnel/t", 8808, "fs")))

    proxy = tc.SlotProxy(["x"], 8808, "fs")
    asyncio.run(_tolerate_never_ready(
        tc._run_connection_lazy("wss://x/tunnel/t", proxy, "fs")
    ))

    pool = MagicMock()
    asyncio.run(
        tc._run_extract_pool_connection("wss://x/tunnel/t", pool, "/repo", "extract")
    )

    assert len(captured) == 3, "expected exactly 3 websockets.connect() call sites"
    for kw in captured:
        # Picks up the env override, proving it is not hardcoded independent
        # of any configuration mechanism.
        assert kw.get("open_timeout") == 37.0


# ---------------------------------------------------------------------------
# 13161001 — _is_clean_server_close: distinguish a clean server-initiated
# close (1012 service restart) from a genuine connection failure
# ---------------------------------------------------------------------------

def test_is_clean_server_close_detects_1012_only():
    import websockets.exceptions as _wse
    import websockets.frames as _wsf

    # A server-initiated 1012 ("Service Restart") close → clean, not a failure.
    restart_exc = _wse.ConnectionClosedError(_wsf.Close(1012, "restart"), None)
    assert tc._is_clean_server_close(restart_exc) is True

    # Any other close code (e.g. 1011 internal error) is a genuine failure.
    error_exc = _wse.ConnectionClosedError(_wsf.Close(1011, "internal error"), None)
    assert tc._is_clean_server_close(error_exc) is False

    # A ConnectionClosed with no received frame at all (abnormal closure).
    abnormal_exc = _wse.ConnectionClosedError(None, None)
    assert tc._is_clean_server_close(abnormal_exc) is False

    # A plain non-websockets exception is never treated as a clean close.
    assert tc._is_clean_server_close(RuntimeError("dropped")) is False


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


def test_reconnect_loop_resets_backoff_on_clean_1012_but_climbs_on_real_failure(
    monkeypatch,
):
    """13161001 — a clean server-initiated close (1012 service restart) must
    reset backoff exactly like a successful connection, NOT climb it like a
    repeated failure; a genuine repeated failure (RuntimeError) must still
    climb backoff exponentially as before."""
    import websockets.exceptions as _wse
    import websockets.frames as _wsf

    attempts = {"n": 0}

    async def fake_run_connection(ws_url, port, label, tool_prefix=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            # Clean server-initiated restart — should NOT escalate backoff.
            raise _wse.ConnectionClosedError(_wsf.Close(1012, "restart"), None)
        if attempts["n"] in (2, 3):
            # Genuine repeated failures — backoff SHOULD keep climbing.
            raise RuntimeError("dropped")
        raise asyncio.CancelledError

    sleeps: list[float] = []

    async def fake_sleep(n):
        sleeps.append(n)

    monkeypatch.setattr(tc, "_run_connection", fake_run_connection)
    monkeypatch.setattr(tc.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tc._reconnect_loop("wss://x", 8808, "fs"))

    assert attempts["n"] == 4
    # 1st sleep: base backoff after the clean 1012 close (reset, not climbed).
    # 2nd/3rd sleeps: climbing exponentially across the two real failures.
    assert sleeps == [1.0, 2.0, 4.0]


# ---------------------------------------------------------------------------
# SlotProxy + lazy-spawn coroutines (3649a61a) — lazy plugin spawning
# ---------------------------------------------------------------------------

class _FakeProc:
    """Minimal subprocess.Popen stand-in: alive until .terminate()/.kill().

    Implements the context-manager protocol (__enter__/__exit__) because real
    Popen does and subprocess.run()'s stdlib implementation always spawns via
    `with Popen(...) as process:` -- any code path that reaches subprocess.run
    while tc.subprocess.Popen is patched to this class needs it to behave like
    a real Popen would (e.g. _kill_all_previously_spawned_pids' Windows taskkill).
    """
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
    def __enter__(self):
        return self
    def __exit__(self, *exc_info):
        return False


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


def test_slotproxy_ensure_running_spawn_does_not_block_event_loop(monkeypatch):
    """31de9cf7 — a slow/stuck spawn must not freeze the event loop.

    ensure_running() used to call ``_spawn_with_cache_retry`` as a plain
    synchronous function inside an async method. That function contains
    genuinely blocking calls deep inside it (time.sleep(0.1), and — on a
    fast-exit failure — Popen.communicate(timeout=5.0) via
    _probe_tar_entry_error). Since asyncio is single-threaded/cooperative, a
    plain synchronous call there froze the ENTIRE event loop for the duration
    of the spawn, starving every OTHER slot's WebSocket handshake/keepalive
    coroutine on the same loop — reproducing the live bug where one slot's
    stuck cold-spawn cascaded into all 7 slots timing out "during opening
    handshake" simultaneously.

    This test simulates a slow spawn by monkeypatching
    ``_spawn_with_cache_retry`` to do a real blocking ``time.sleep`` before
    returning, then runs ``ensure_running()`` concurrently (via
    ``asyncio.gather``) with a lightweight heartbeat coroutine that ticks a
    counter on a short ``asyncio.sleep`` interval. If the event loop is
    frozen while the spawn is in flight, the heartbeat cannot advance at all
    during that window (count stays 0). With the fix (``asyncio.to_thread``
    offloading the blocking call to a worker thread) the heartbeat keeps
    ticking throughout. Verified by temporarily reverting the fix (plain
    synchronous call) — this test fails (heartbeat count == 0) — and passes
    again with ``asyncio.to_thread`` restored.
    """
    import contextlib
    import time as _time

    SPAWN_SECONDS = 0.5
    HEARTBEAT_INTERVAL = 0.05

    def slow_spawn(cmd, env, label, diagnostics=None):
        # Stands in for the real blocking chain (time.sleep + Popen.communicate)
        # inside _spawn_with_cache_retry — a real, thread-blocking sleep, not an
        # asyncio one, so it only "yields" if run off the event loop thread.
        # ddd46cc8 — ensure_running() now passes self.diagnostics as a 4th
        # positional arg; accept (and ignore) it here.
        _time.sleep(SPAWN_SECONDS)
        return _FakeProc(cmd)

    monkeypatch.setattr(tc, "_spawn_with_cache_retry", slow_spawn)
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=True))
    monkeypatch.setattr(tc, "_port_is_open", lambda port: False)

    sp = tc.SlotProxy(["proxy", "cmd"], 8899, "fs")
    heartbeats = {"count": 0}

    async def heartbeat():
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            heartbeats["count"] += 1

    async def run():
        hb_task = asyncio.create_task(heartbeat())
        await sp.ensure_running()
        hb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb_task

    asyncio.run(run())

    # SPAWN_SECONDS / HEARTBEAT_INTERVAL == 10 possible ticks. A frozen event
    # loop would show 0 (maybe 1 from scheduling slop right at the boundary);
    # an unblocked loop should tick close to the full budget. Require a
    # healthy majority to keep the assertion robust against CI scheduling jitter.
    assert heartbeats["count"] >= 5, (
        f"heartbeat only ticked {heartbeats['count']} times during a "
        f"{SPAWN_SECONDS}s spawn — event loop appears to have been blocked"
    )
    assert sp.is_running


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
    def counting_kill(reason: str = "stopped"):
        # ddd46cc8 — _idle_killer now calls kill(reason="idle_killed"); accept
        # (and forward) the reason kwarg like the real SlotProxy.kill().
        killed["n"] += 1
        real_kill(reason)
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


def test_reconnect_loop_lazy_resets_backoff_on_clean_1012_but_climbs_on_real_failure(
    monkeypatch,
):
    """13161001 — same fix as _reconnect_loop, applied to the lazy-spawn loop:
    a clean 1012 server restart resets backoff; real repeated failures still
    climb it."""
    import websockets.exceptions as _wse
    import websockets.frames as _wsf

    attempts = {"n": 0}

    async def fake_run_connection_lazy(
        ws_url, proxy, label, tool_prefix=None, known_repo_paths=None
    ):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _wse.ConnectionClosedError(_wsf.Close(1012, "restart"), None)
        if attempts["n"] in (2, 3):
            raise RuntimeError("dropped")
        raise asyncio.CancelledError

    sleeps: list[float] = []

    async def fake_sleep(n):
        sleeps.append(n)

    monkeypatch.setattr(tc, "_run_connection_lazy", fake_run_connection_lazy)
    monkeypatch.setattr(tc.asyncio, "sleep", fake_sleep)

    proxy = tc.SlotProxy(["x"], 8808, "fs")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tc._reconnect_loop_lazy("wss://x", proxy, "fs"))

    assert attempts["n"] == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_reconnect_loop_extract_pool_resets_backoff_on_clean_1012_but_climbs_on_real_failure(
    monkeypatch,
):
    """13161001 — same fix applied to the pooled code-extractor reconnect
    loop: a clean 1012 server restart resets backoff; real repeated failures
    still climb it."""
    import websockets.exceptions as _wse
    import websockets.frames as _wsf

    attempts = {"n": 0}

    async def fake_run_extract_pool_connection(
        ws_url, pool, default_repo_path, label, tool_prefix=None
    ):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _wse.ConnectionClosedError(_wsf.Close(1012, "restart"), None)
        if attempts["n"] in (2, 3):
            raise RuntimeError("dropped")
        raise asyncio.CancelledError

    sleeps: list[float] = []

    async def fake_sleep(n):
        sleeps.append(n)

    monkeypatch.setattr(
        tc, "_run_extract_pool_connection", fake_run_extract_pool_connection
    )
    monkeypatch.setattr(tc.asyncio, "sleep", fake_sleep)

    pool = MagicMock()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            tc._reconnect_loop_extract_pool("wss://x", pool, "/repo", "extract")
        )

    assert attempts["n"] == 4
    assert sleeps == [1.0, 2.0, 4.0]


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

    # ab956c80 — persistent mcp-proxy rejects a sessionless tools/list with
    # HTTP 400 even while mcp-debugger is healthy. The probe must initialize a
    # temporary session, list tools through it, and clean that session up.
    calls = []

    class _Resp:
        def __init__(self, status, headers=None):
            self.status_code = status
            self.headers = headers or {}

    class _PersistentClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, url, *, json, headers):
            method = json["method"]
            session_id = headers.get("Mcp-Session-Id")
            calls.append((method, session_id))
            if calls == [("tools/list", None)]:
                return _Resp(400)
            if method == "initialize":
                return _Resp(200, {"mcp-session-id": "debug-session"})
            if method == "notifications/initialized":
                return _Resp(202)
            return _Resp(200)
        async def delete(self, url, *, headers):
            calls.append(("DELETE", headers.get("Mcp-Session-Id")))
            return _Resp(200)

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "AsyncClient", _PersistentClient)

    assert asyncio.run(tc._probe_slot_health(8821, attempts=1, delay=0)) is True
    assert calls == [
        ("tools/list", None),
        ("initialize", None),
        ("notifications/initialized", "debug-session"),
        ("tools/list", "debug-session"),
        ("DELETE", "debug-session"),
    ]


def test_probe_slot_health_bounds_whole_persistent_handshake(monkeypatch):
    """One slow handshake cannot multiply the 10s per-attempt startup budget."""
    class _SlowClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, *a, **k):
            await asyncio.sleep(1)
            return _FakeProbeResp(400)

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "AsyncClient", _SlowClient)
    monkeypatch.setattr(tc, "_SLOT_HEALTH_ATTEMPT_TIMEOUT", 0.02)

    started = time.monotonic()
    assert asyncio.run(
        tc._probe_slot_health(8821, attempts=1, delay=0)
    ) is False
    assert time.monotonic() - started < 0.2


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
    # 089a936a — the unhealthy report now also carries an actionable reason/detail
    # (additive; the type/slot/healthy triple is unchanged).
    assert len(sent) == 1
    msg = sent[0]
    assert msg["type"] == "plugin_status"
    assert msg["slot"] == "fs"
    assert msg["healthy"] is False
    assert msg["reason"] == "unreachable"
    assert msg["detail"]


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
    # 089a936a — the unhealthy report now also carries reason/detail (additive).
    assert any(
        m.get("type") == "plugin_status"
        and m.get("slot") == "fs"
        and m.get("healthy") is False
        and m.get("reason") == "unreachable"
        and m.get("detail")
        for m in sent
    )
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


def test_reprobe_backs_off_after_max_retries(monkeypatch):
    """c325b8eb: a slot that NEVER recovers (e.g. "dc" under ongoing AV/Defender
    file-lock interference on every single npx extraction attempt — a genuinely
    persistent failure, not a one-off) must not get re-kicked at the flat
    _SLOT_REPROBE_INTERVAL forever. After _WATCHDOG_MAX_RETRIES straight failed
    reprobes the background _reprobe() task must back off to the much longer
    _WATCHDOG_COOLDOWN_SECONDS cadence, mirroring _proc_watchdog's already-proven
    fast-retry-then-cooldown shape."""
    monkeypatch.setattr(tc, "_SLOT_REPROBE_INTERVAL", 0.03)
    monkeypatch.setattr(tc, "_WATCHDOG_MAX_RETRIES", 2)
    monkeypatch.setattr(tc, "_WATCHDOG_COOLDOWN_SECONDS", 0.2)

    n_requests = tc._WATCHDOG_MAX_RETRIES + 1  # enough failing requests to escalate
    call_times: list[float] = []

    async def fake_reprobe_once(proxy, probe):
        call_times.append(time.monotonic())
        return False  # never recovers — simulates persistent AV lock interference

    monkeypatch.setattr(tc, "_reprobe_once", fake_reprobe_once)

    sent = []

    class FakeWS:
        def __init__(self):
            self._n = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            self._n += 1
            if self._n <= n_requests:
                return json.dumps({"type": "request", "id": str(self._n)})
            # Escalation already happened — idle-ping the connection long enough
            # for several background reprobe cycles (including at least one past
            # the fast-retry cap) to fire, then end the stream.
            if len(call_times) < tc._WATCHDOG_MAX_RETRIES + 3:
                await asyncio.sleep(0.03)
                return json.dumps({"type": "ping"})
            raise StopAsyncIteration

        async def send(self, data):
            sent.append(json.loads(data))

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    import httpx as _httpx
    import websockets as _ws
    monkeypatch.setattr(_ws, "connect", lambda url, **kw: FakeWS())
    monkeypatch.setattr(_httpx, "AsyncClient", lambda *a, **k: FakeHttpClient())

    async def fake_ensure(self):  # proxy never comes up
        return None
    monkeypatch.setattr(tc.SlotProxy, "ensure_running", fake_ensure)

    proxy = tc.SlotProxy(["x"], 8808, "dc")
    asyncio.run(tc._run_connection_lazy("wss://x", proxy, "dc"))

    assert len(call_times) >= tc._WATCHDOG_MAX_RETRIES + 2
    gaps = [b - a for a, b in zip(call_times, call_times[1:])]
    fast_gaps = gaps[: tc._WATCHDOG_MAX_RETRIES]
    cooled_gap = gaps[tc._WATCHDOG_MAX_RETRIES]
    # Fast-retry gaps stay near _SLOT_REPROBE_INTERVAL; once the cap is
    # exceeded the gap jumps to the much larger cooldown.
    assert all(g < tc._WATCHDOG_COOLDOWN_SECONDS * 0.6 for g in fast_gaps)
    assert cooled_gap >= tc._WATCHDOG_COOLDOWN_SECONDS * 0.6


# ---------------------------------------------------------------------------
# ddd46cc8 — unified slot lifecycle contract: SlotState/SlotDiagnostics,
# failure classification, kill() reasons, _spawn_with_cache_retry diagnostics,
# _preflight_slot(proxy=...), quarantine/reprobe, cross-slot isolation.
# ---------------------------------------------------------------------------

# --- pure classification helpers --------------------------------------------

def test_classify_stderr_signature_detects_module_not_found():
    r = tc._classify_stderr_signature(
        "Traceback (most recent call last):\n"
        "ModuleNotFoundError: No module named 'mcp.server.fastmcp'\n"
    )
    assert r is not None
    state, detail = r
    assert state is tc.SlotState.DEPENDENCY_MISSING
    assert "mcp.server.fastmcp" in detail


def test_classify_stderr_signature_detects_import_error_pydantic_settings():
    r = tc._classify_stderr_signature("ImportError: No module named 'pydantic_settings'")
    assert r is not None
    assert r[0] is tc.SlotState.DEPENDENCY_MISSING
    assert "pydantic_settings" in r[1]


def test_classify_stderr_signature_none_for_unrelated_text():
    assert tc._classify_stderr_signature("Server listening on port 8813\n") is None
    assert tc._classify_stderr_signature("") is None
    assert tc._classify_stderr_signature(None) is None


def test_classify_launch_exception_file_not_found_is_dependency_missing():
    state, detail = tc._classify_launch_exception(FileNotFoundError("no such file: uvx"))
    assert state is tc.SlotState.DEPENDENCY_MISSING
    assert "uvx" in detail or "not found" in detail


def test_classify_launch_exception_generic_error_is_child_crashed():
    state, detail = tc._classify_launch_exception(OSError("boom"))
    assert state is tc.SlotState.CHILD_CRASHED
    assert "boom" in detail


def test_probe_fast_exit_stderr_captures_output(monkeypatch):
    class _FastExitProc:
        def __init__(self, *a, **k): pass
        def communicate(self, timeout=None):
            return (b"", b"ModuleNotFoundError: No module named 'pydantic_settings'\n")
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FastExitProc())
    text = tc._probe_fast_exit_stderr(["python", "-m", "x"], None, wait_seconds=1.0)
    assert "ModuleNotFoundError" in text


def test_probe_fast_exit_stderr_returns_none_on_timeout(monkeypatch):
    class _HangingProc:
        def __init__(self, *a, **k): pass
        def communicate(self, timeout=None):
            raise tc.subprocess.TimeoutExpired(cmd="x", timeout=timeout)
        def kill(self): pass
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _HangingProc())
    assert tc._probe_fast_exit_stderr(["x"], None, wait_seconds=0.01) is None


def test_probe_fast_exit_stderr_returns_none_on_popen_exception(monkeypatch):
    def boom(*a, **k):
        raise OSError("cannot spawn")
    monkeypatch.setattr(tc.subprocess, "Popen", boom)
    assert tc._probe_fast_exit_stderr(["x"], None) is None


# --- SlotDiagnostics / SlotState -------------------------------------------

def test_slot_diagnostics_set_updates_state_phase_and_extra_fields():
    d = tc.SlotDiagnostics(slot="fs")
    assert d.state is tc.SlotState.CONFIGURED
    before = d.updated_at
    d.set(tc.SlotState.HEALTHY, retry_count=0, root_cause=None)
    assert d.state is tc.SlotState.HEALTHY
    assert d.phase == "healthy"  # defaults to state.value when phase omitted
    assert d.updated_at >= before


def test_slot_diagnostics_to_dict_is_json_safe():
    d = tc.SlotDiagnostics(slot="dc")
    d.set(tc.SlotState.DEPENDENCY_MISSING, root_cause="missing pydantic_settings")
    out = d.to_dict()
    assert out["slot"] == "dc"
    assert out["state"] == "dependency_missing"
    assert out["root_cause"] == "missing pydantic_settings"
    json.dumps(out)  # must not raise — every field is JSON-serializable


def test_slot_is_quarantined_only_true_for_quarantined_state():
    d = tc.SlotDiagnostics(slot="fs")
    assert tc._slot_is_quarantined(d) is False
    d.set(tc.SlotState.DEGRADED)
    assert tc._slot_is_quarantined(d) is False
    d.set(tc.SlotState.QUARANTINED)
    assert tc._slot_is_quarantined(d) is True


# --- quarantine decision (pure) ---------------------------------------------

def test_note_reprobe_failure_transient_never_quarantines():
    d = tc.SlotDiagnostics(slot="fs")
    for _ in range(10):
        quarantined = tc._note_reprobe_failure(d, None)  # unclassified == transient
        assert quarantined is False
    assert d.state is tc.SlotState.RECONNECTING
    assert d.consecutive_deterministic_failures == 0


def test_note_reprobe_failure_deterministic_quarantines_after_threshold():
    d = tc.SlotDiagnostics(slot="docs")
    classification = (tc.SlotState.DEPENDENCY_MISSING, "missing 'mcp.server.fastmcp'")
    results = [tc._note_reprobe_failure(d, classification) for _ in range(tc._QUARANTINE_THRESHOLD)]
    # Not quarantined until the threshold-th consecutive deterministic failure.
    assert results[:-1] == [False] * (tc._QUARANTINE_THRESHOLD - 1)
    assert results[-1] is True
    assert d.state is tc.SlotState.QUARANTINED
    assert d.quarantine_reason == "missing 'mcp.server.fastmcp'"
    assert d.next_retry_at is not None


def test_note_reprobe_failure_flaky_blip_does_not_accumulate_toward_quarantine():
    """A single non-deterministic failure mixed into an otherwise-deterministic
    streak resets the streak — only a CONSISTENT deterministic signature earns
    quarantine, matching the "transient failures recover, deterministic ones
    quarantine" design split."""
    d = tc.SlotDiagnostics(slot="docs")
    classification = (tc.SlotState.CHILD_CRASHED, "exited immediately")
    tc._note_reprobe_failure(d, classification)
    tc._note_reprobe_failure(d, classification)
    assert d.consecutive_deterministic_failures == 2
    tc._note_reprobe_failure(d, None)  # one flaky/transient blip
    assert d.consecutive_deterministic_failures == 0
    # Would need _QUARANTINE_THRESHOLD MORE consecutive deterministic failures now.
    for _ in range(tc._QUARANTINE_THRESHOLD - 1):
        assert tc._note_reprobe_failure(d, classification) is False
    assert tc._note_reprobe_failure(d, classification) is True


def test_note_reprobe_success_fully_resets_diagnostics():
    d = tc.SlotDiagnostics(slot="docs")
    d.set(tc.SlotState.QUARANTINED, quarantine_reason="x", retry_count=9,
          consecutive_deterministic_failures=5)
    tc._note_reprobe_success(d)
    assert d.state is tc.SlotState.HEALTHY
    assert d.retry_count == 0
    assert d.consecutive_deterministic_failures == 0
    assert d.quarantine_reason is None
    assert d.last_healthy_at is not None


# --- SlotProxy.kill(reason=...) --------------------------------------------

def test_kill_reason_idle_killed_sets_diagnostics_state(monkeypatch):
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)
    sp = tc.SlotProxy(["x"], 8808, "fs")
    sp._proc = tc.subprocess.Popen(["x"])
    sp.holder["proc"] = sp._proc
    sp.kill(reason="idle_killed")
    assert sp.diagnostics.state is tc.SlotState.IDLE_KILLED


def test_kill_reason_transport_closed_sets_diagnostics_state(monkeypatch):
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)
    sp = tc.SlotProxy(["x"], 8808, "fs")
    sp._proc = tc.subprocess.Popen(["x"])
    sp.holder["proc"] = sp._proc
    sp.kill(reason="transport_closed")
    assert sp.diagnostics.state is tc.SlotState.TRANSPORT_CLOSED


def test_kill_default_reason_is_stopped(monkeypatch):
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)
    sp = tc.SlotProxy(["x"], 8808, "fs")
    sp._proc = tc.subprocess.Popen(["x"])
    sp.holder["proc"] = sp._proc
    sp.kill()
    assert sp.diagnostics.state is tc.SlotState.STOPPED


def test_kill_unrecognised_reason_falls_back_to_stopped(monkeypatch):
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)
    sp = tc.SlotProxy(["x"], 8808, "fs")
    sp._proc = tc.subprocess.Popen(["x"])
    sp.holder["proc"] = sp._proc
    sp.kill(reason="some-typo'd-reason")  # must not raise
    assert sp.diagnostics.state is tc.SlotState.STOPPED


def test_kill_on_reused_occupant_does_not_touch_diagnostics(monkeypatch):
    """A reused (not-ours) occupant is never actually torn down — diagnostics
    must stay whatever they already were (kill() returns before recording)."""
    sp = tc.SlotProxy(["x"], 8808, "fs")
    sp._reused = True
    sp.diagnostics.set(tc.SlotState.HEALTHY)
    sp.kill(reason="idle_killed")
    assert sp.diagnostics.state is tc.SlotState.HEALTHY  # unchanged
    assert sp._reused is False  # reuse tracking still dropped


# --- _spawn_with_cache_retry(diagnostics=...) -------------------------------

def test_spawn_with_cache_retry_without_diagnostics_unchanged(monkeypatch):
    """Every pre-existing (positional, 3-arg) caller passes no diagnostics —
    confirm the classification code path is never even entered in that case."""
    monkeypatch.setattr(tc, "_probe_fast_exit_stderr", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not be called when diagnostics is None")))
    fake_proc = _FakeProc()
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: fake_proc)
    result = tc._spawn_with_cache_retry(["uvx", "some-tool"], None, "test")
    assert result is fake_proc


def test_spawn_with_cache_retry_classifies_dependency_missing_fast_exit(monkeypatch):
    """A fast-exiting Popen whose stderr shows a ModuleNotFoundError classifies
    onto *diagnostics* as DEPENDENCY_MISSING — the confirmed failure pattern
    (a connector child dying at import time because e.g. mcp.server.fastmcp or
    pydantic_settings isn't installed) — while leaving the function's own
    return value/retry behaviour untouched."""
    exited_proc = _FakeProc()
    exited_proc._alive = False
    exited_proc.returncode = 1
    retried_proc = _FakeProc()

    calls = {"n": 0}
    def _popen(cmd, env=None, **kw):
        calls["n"] += 1
        return exited_proc if calls["n"] == 1 else retried_proc

    monkeypatch.setattr(tc.subprocess, "Popen", _popen)
    monkeypatch.setattr(tc, "_scoped_cache_clear", lambda cmd, label="": True)
    monkeypatch.setattr(
        tc, "_probe_fast_exit_stderr",
        lambda cmd, env, wait_seconds=2.0: "ModuleNotFoundError: No module named 'mcp.server.fastmcp'",
    )
    # Keep the existing TAR-error probe out of the way (unrelated classifier).
    monkeypatch.setattr(tc, "_probe_tar_entry_error", lambda *a, **k: False)

    diag = tc.SlotDiagnostics(slot="docs")
    result = tc._spawn_with_cache_retry(["python", "-m", "meridian_docs"], None, "docs", diag)

    assert result is retried_proc  # existing retry behaviour unaffected
    assert diag.state is tc.SlotState.DEPENDENCY_MISSING
    assert "mcp.server.fastmcp" in diag.dependency_missing
    assert diag.exit_code == 1


def test_spawn_with_cache_retry_fast_exit_without_signature_is_child_crashed(monkeypatch):
    exited_proc = _FakeProc()
    exited_proc._alive = False
    exited_proc.returncode = 1
    retried_proc = _FakeProc()

    calls = {"n": 0}
    def _popen(cmd, env=None, **kw):
        calls["n"] += 1
        return exited_proc if calls["n"] == 1 else retried_proc

    monkeypatch.setattr(tc.subprocess, "Popen", _popen)
    monkeypatch.setattr(tc, "_scoped_cache_clear", lambda cmd, label="": True)
    monkeypatch.setattr(tc, "_probe_fast_exit_stderr", lambda *a, **k: "segfault, core dumped")
    monkeypatch.setattr(tc, "_probe_tar_entry_error", lambda *a, **k: False)

    diag = tc.SlotDiagnostics(slot="dc")
    result = tc._spawn_with_cache_retry(["npx", "-y", "tool"], None, "dc", diag)

    assert result is retried_proc
    assert diag.state is tc.SlotState.CHILD_CRASHED
    assert diag.exit_code == 1


# --- _preflight_slot(proxy=...) ---------------------------------------------

def test_preflight_slot_without_proxy_unchanged(monkeypatch):
    """Byte-identical to the pre-existing (no proxy kwarg) behaviour."""
    sent = []
    class _WS:
        async def send(self, data): sent.append(json.loads(data))
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=False))
    healthy = asyncio.run(tc._preflight_slot(_WS(), 8808, "fs"))
    assert healthy is False
    assert sent == [{
        "type": "plugin_status", "slot": "fs", "healthy": False,
        "reason": "unreachable", "detail": sent[0]["detail"],
    }]


def test_preflight_slot_with_proxy_marks_diagnostics_healthy(monkeypatch):
    class _WS:
        async def send(self, data): pass
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=True))
    sp = tc.SlotProxy(["x"], 8808, "code")
    healthy = asyncio.run(tc._preflight_slot(_WS(), 8808, "code", proxy=sp))
    assert healthy is True
    assert sp.diagnostics.state is tc.SlotState.HEALTHY


def test_preflight_slot_with_proxy_marks_diagnostics_tools_list_timeout(monkeypatch):
    sent = []
    class _WS:
        async def send(self, data): sent.append(json.loads(data))
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=False))
    sp = tc.SlotProxy(["x"], 8808, "code")
    healthy = asyncio.run(tc._preflight_slot(_WS(), 8808, "code", proxy=sp))
    assert healthy is False
    assert sp.diagnostics.state is tc.SlotState.TOOLS_LIST_TIMEOUT
    # The wire report also carries the state so a server-side consumer can
    # distinguish this from a bare "unhealthy".
    assert sent[0]["state"] == "tools_list_timeout"


# --- reprobe_slot — explicit operator recovery ------------------------------

def test_reprobe_slot_recovers_a_quarantined_slot(monkeypatch):
    sp = tc.SlotProxy(["x"], 8808, "docs")
    sp.diagnostics.set(
        tc.SlotState.QUARANTINED, quarantine_reason="missing dependency",
        consecutive_deterministic_failures=tc._QUARANTINE_THRESHOLD,
    )

    async def fake_reprobe_once(proxy, probe):
        return True  # operator fixed the underlying issue; probe now succeeds
    monkeypatch.setattr(tc, "_reprobe_once", fake_reprobe_once)

    result = asyncio.run(tc.reprobe_slot(sp))
    assert result.state is tc.SlotState.HEALTHY
    assert result.quarantine_reason is None
    assert result.consecutive_deterministic_failures == 0


def test_reprobe_slot_still_failing_reaffirms_quarantine(monkeypatch):
    sp = tc.SlotProxy(["x"], 8808, "docs")
    sp.diagnostics.set(
        tc.SlotState.DEPENDENCY_MISSING, root_cause="still missing",
        consecutive_deterministic_failures=tc._QUARANTINE_THRESHOLD,
    )

    async def fake_reprobe_once(proxy, probe):
        return False  # still broken
    monkeypatch.setattr(tc, "_reprobe_once", fake_reprobe_once)

    result = asyncio.run(tc.reprobe_slot(sp))
    assert result.state is tc.SlotState.QUARANTINED


# --- integration: deterministic quarantine + cross-slot isolation ----------

def test_run_connection_lazy_quarantines_after_persistent_dependency_missing(monkeypatch):
    """End-to-end through _run_connection_lazy's real _reprobe() closure: a
    slot whose spawn ALWAYS fails with the confirmed dependency-missing
    signature (FileNotFoundError from Popen) is escalated to unhealthy, then
    QUARANTINED after _QUARANTINE_THRESHOLD consecutive deterministic reprobe
    failures — and the quarantine plugin_status carries state/quarantine_reason."""
    monkeypatch.setattr(tc, "_SLOT_REPROBE_INTERVAL", 0.01)
    monkeypatch.setattr(tc, "_WATCHDOG_MAX_RETRIES", 1)
    monkeypatch.setattr(tc, "_QUARANTINE_THRESHOLD", 2)
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    def always_missing(cmd, *a, **k):
        raise FileNotFoundError("no such file: uvx")
    monkeypatch.setattr(tc.subprocess, "Popen", always_missing)

    sent = []
    n_requests = tc._WATCHDOG_MAX_RETRIES + 1

    class FakeWS:
        def __init__(self):
            self._n = 0
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def __aiter__(self): return self
        async def __anext__(self):
            self._n += 1
            if self._n <= n_requests:
                return json.dumps({"type": "request", "id": str(self._n)})
            # Escalation happened — idle-ping long enough for several reprobe
            # cycles (past the quarantine threshold) to run, then end.
            if self._n < n_requests + 20:
                await asyncio.sleep(0.02)
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

    proxy = tc.SlotProxy(["uvx", "meridian-docs-mcp"], 8808, "docs")
    asyncio.run(tc._run_connection_lazy("wss://x", proxy, "docs"))

    statuses = [m for m in sent if m.get("type") == "plugin_status"]
    quarantine_msgs = [m for m in statuses if m.get("state") == "quarantined"]
    assert quarantine_msgs, f"never quarantined; statuses={statuses}"
    assert quarantine_msgs[0]["quarantine_reason"]
    assert proxy.diagnostics.state is tc.SlotState.QUARANTINED


def test_quarantine_is_isolated_per_slot_never_cross_contaminates():
    """Two independent SlotProxy instances (as run_tunnel constructs one per
    connector slot) never share diagnostics state — quarantining one must
    leave a sibling slot's diagnostics completely untouched."""
    broken = tc.SlotProxy(["uvx", "docs-mcp"], 8813, "docs")
    healthy = tc.SlotProxy(["uvx", "outputs-mcp"], 8814, "outputs")

    classification = (tc.SlotState.DEPENDENCY_MISSING, "missing 'pydantic_settings'")
    for _ in range(tc._QUARANTINE_THRESHOLD):
        tc._note_reprobe_failure(broken.diagnostics, classification)
    tc._note_reprobe_success(healthy.diagnostics)

    assert broken.diagnostics.state is tc.SlotState.QUARANTINED
    assert healthy.diagnostics.state is tc.SlotState.HEALTHY
    assert healthy.diagnostics.quarantine_reason is None
    assert healthy.diagnostics.consecutive_deterministic_failures == 0


# ---------------------------------------------------------------------------
# _inject_mcp_entries — non-dict top-level + non-dict mcpServers (778)
# ---------------------------------------------------------------------------

def test_inject_mcp_entries_replaces_non_dict_top_level():
    out = tc._inject_mcp_entries("[1, 2, 3]", {"filesystem": {"type": "http", "url": "u"}})
    data = json.loads(out)
    assert data["mcpServers"]["filesystem"]["url"] == "u"


def test_inject_mcp_entries_replaces_non_dict_mcpservers():
    out = tc._inject_mcp_entries(
        json.dumps({"mcpServers": "oops-a-string"}),
        {"filesystem": {"type": "http", "url": "u"}},
    )
    data = json.loads(out)
    assert data["mcpServers"]["filesystem"]["url"] == "u"


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
    monkeypatch.setattr(tc.asyncio, "sleep", AsyncMock(return_value=None))
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
    """Cached token path: /me actually rejects (401), re-auth via browser also
    cancelled → 2."""
    monkeypatch.setattr(tc, "_force_utf8_io", lambda: None)
    monkeypatch.setattr(tc, "_resolve_token", lambda t: "")
    monkeypatch.setattr(tc, "_read_cached_token", lambda b: "sk_cached")
    monkeypatch.setattr(tc, "_fetch_me", AsyncMock(side_effect=_http_error(401)))
    monkeypatch.setattr(tc, "_browser_auth_flow", AsyncMock(return_value=""))
    rc = _run_tunnel(token=None, base_url="https://x")
    assert rc == 2


def test_run_tunnel_cached_rejected_browser_ok_then_me_fails_returns_1(monkeypatch):
    """Cached token actually rejected (401) → browser succeeds → second /me
    still fails → exit 1."""
    monkeypatch.setattr(tc, "_force_utf8_io", lambda: None)
    monkeypatch.setattr(tc, "_resolve_token", lambda t: "")
    monkeypatch.setattr(tc, "_read_cached_token", lambda b: "sk_cached")
    monkeypatch.setattr(tc, "_fetch_me", AsyncMock(side_effect=_http_error(401)))
    monkeypatch.setattr(tc, "_browser_auth_flow", AsyncMock(return_value="sk_new"))
    rc = _run_tunnel(token=None, base_url="https://x")
    assert rc == 1


def test_run_tunnel_cached_token_transient_failure_retries_then_succeeds(monkeypatch):
    """dcf6d187 — a network hiccup (not an auth rejection) on the cached-token
    /me check must NOT discard the cached token / force a browser re-auth. The
    SAME token is retried once and, if that succeeds, the tunnel proceeds with
    no re-authentication at all."""
    monkeypatch.setattr(tc, "_force_utf8_io", lambda: None)
    monkeypatch.setattr(tc, "_resolve_token", lambda t: "")
    monkeypatch.setattr(tc, "_read_cached_token", lambda b: "sk_cached")
    monkeypatch.setattr(tc.asyncio, "sleep", AsyncMock(return_value=None))
    browser_auth = AsyncMock(return_value="sk_should_not_be_used")
    monkeypatch.setattr(tc, "_browser_auth_flow", browser_auth)
    fetch_me = AsyncMock(
        side_effect=[
            httpx.ConnectError("connection refused"),
            {"tenant_id": "t1", "plan": "pro"},
        ]
    )
    monkeypatch.setattr(tc, "_fetch_me", fetch_me)
    monkeypatch.setattr(tc, "_write_cached_token", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_fetch_filesystem_roots", AsyncMock(return_value=([], [], "", [])))
    monkeypatch.setattr(tc, "_ensure_node", lambda auto: False)
    rc = _run_tunnel(token=None, base_url="https://x")
    # Node missing → 1, but the point is: no browser re-auth was triggered and
    # the cached token was reused after one transient-failure retry.
    assert rc == 1
    browser_auth.assert_not_called()
    assert fetch_me.await_count == 2


def test_fetch_me_with_retry_does_not_retry_auth_rejection(monkeypatch):
    """A genuine 401/403 is raised immediately — no retry, no wasted backoff."""
    monkeypatch.setattr(tc.asyncio, "sleep", AsyncMock(side_effect=AssertionError("should not sleep")))
    fetch_me = AsyncMock(side_effect=_http_error(401))
    monkeypatch.setattr(tc, "_fetch_me", fetch_me)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(tc._fetch_me_with_retry("https://x", "sk_tok"))
    assert fetch_me.await_count == 1


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
    # 986117fc — the staleness-alert git probe shells out via subprocess.run,
    # which internally spawns through the same tc.subprocess.Popen patched
    # below (FakeProc) -- left live it would (a) inflate the `procs` count
    # these tests assert on and (b) since FakeProc has no `communicate`, get
    # swallowed as a fail-open None anyway. No-op it here so `procs` reflects
    # only real plugin/slot spawns; the probe itself has dedicated, isolated
    # coverage in test_tunnel_client.py.
    monkeypatch.setattr(tc, "_tunnel_client_commit_hash_sync", lambda *a, **k: None)

    procs = []

    class FakeProc:
        def __init__(self, cmd):
            self.cmd = cmd
            procs.append(self)
        def poll(self): return None  # alive (so SlotProxy.is_running is True)
        def terminate(self): pass
        def wait(self, timeout=None): return 0
        def kill(self): pass
        # subprocess.run()'s stdlib impl always does `with Popen(...) as process:`
        # -- _kill_all_previously_spawned_pids' Windows taskkill call goes through
        # subprocess.run, so this stand-in needs the context-manager protocol too.
        def __enter__(self): return self
        def __exit__(self, *exc_info): return False

    monkeypatch.setattr(tc.subprocess, "Popen", lambda cmd, *a, **k: FakeProc(cmd))

    # aaddb273 — _kill_stale_port_occupant scans REAL system ports via
    # psutil.net_connections() and (on Windows) shells out to taskkill, which
    # goes through the same tc.subprocess.Popen patched above. On a dev machine
    # that happens to have a real process bound to one of the test's slot ports
    # (e.g. an actual meridian tunnel session running elsewhere), this silently
    # inflates the `procs` list the tests assert against, making the test's
    # pass/fail depend on ambient system state. This helper is about the
    # spawn/config-writing path, not stale-occupant handling (which has its own
    # dedicated, thorough coverage in test_tunnel_client.py) — no-op it here so
    # these tests are hermetic regardless of what happens to be running locally.
    monkeypatch.setattr(tc, "_kill_stale_port_occupant", lambda *a, **k: None)
    # _kill_all_previously_spawned_pids reads/writes a REAL file at
    # Path.home()/".meridian"/"spawned_pids.json" -- not test-scoped. Left live,
    # a successful run would overwrite that real file (used by actual local
    # `meridian --tunnel` sessions) with "[]". Its own behavior has dedicated,
    # properly-isolated coverage in test_tunnel_client.py; no-op it here too,
    # for the same hermeticity reason as _kill_stale_port_occupant above.
    monkeypatch.setattr(tc, "_kill_all_previously_spawned_pids", lambda *a, **k: None)

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


def test_run_tunnel_wires_staleness_alert_loop_with_started_commit(monkeypatch, tmp_path):
    """986117fc — run_tunnel captures the startup commit via
    _tunnel_client_commit_hash_sync and schedules _staleness_alert_loop with
    it, end-to-end through the real wiring (not just the loop in isolation).
    """
    _stub_run_tunnel_spawn(monkeypatch)
    # Override the stub's default (None) with a specific fake commit so we can
    # assert it flows through to the loop.
    monkeypatch.setattr(tc, "_tunnel_client_commit_hash_sync", lambda *a, **k: "cafef00d1234")

    seen_args = []

    async def fake_staleness_loop(started_commit, root_dir=None, interval=tc._STALENESS_CHECK_INTERVAL_SECONDS):
        seen_args.append(started_commit)
        return None

    monkeypatch.setattr(tc, "_staleness_alert_loop", fake_staleness_loop)
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={"tenant_id": "tid-x", "plan": "pro"}),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    rc = _run_tunnel(token="sk", base_url="https://x", repo_path=str(tmp_path))
    assert rc == 0
    assert seen_args == ["cafef00d1234"]


def test_run_tunnel_no_staleness_warning_masks_no_plugins_error(monkeypatch, tmp_path):
    """The staleness task must be appended AFTER the 'nothing to serve' guard —
    it must never turn a real all-slots-disabled error into a fake success."""
    monkeypatch.setattr(tc, "_force_utf8_io", lambda: None)
    monkeypatch.setattr(tc, "_tunnel_client_commit_hash_sync", lambda *a, **k: "cafef00d1234")
    monkeypatch.setattr(tc, "_resolve_token", lambda t: "sk_tok")
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={
            "tenant_id": "t", "plan": "pro",
            "tunnel_plugins_config": [
                {"name": "filesystem", "enabled": False},
                {"name": "code-intel", "enabled": False},
                {"name": "code-extractor", "enabled": False},
            ],
        }),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))
    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tmp_path))
    assert rc == 1


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


def test_run_tunnel_code_slot_wired_with_dedicated_cache_and_reuse(monkeypatch, tmp_path):
    """3475c72f/8e10fb80 — the default (non-overridden) code-intel slot's
    SlotProxy is constructed with reuse_existing=True and an env carrying a
    dedicated CBM_CACHE_DIR, end-to-end through run_tunnel's real wiring (not
    just the helper functions in isolation)."""
    _stub_run_tunnel_spawn(monkeypatch, code_binary="/bin/codebase-memory-mcp")
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={"tenant_id": "tid-x", "plan": "pro"}),
    )
    monkeypatch.setattr(
        tc, "_fetch_filesystem_roots",
        AsyncMock(return_value=([], [], "", [])),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))

    captured = {}
    real_slot_proxy = tc.SlotProxy

    class _RecordingSlotProxy(real_slot_proxy):
        def __init__(self, cmd, port, label, env=None, client_id="", reuse_existing=False):
            if label == "code":
                captured["env"] = env
                captured["reuse_existing"] = reuse_existing
            super().__init__(
                cmd, port, label, env=env, client_id=client_id,
                reuse_existing=reuse_existing,
            )

    monkeypatch.setattr(tc, "SlotProxy", _RecordingSlotProxy)

    rc = _run_tunnel(token="sk", base_url="https://x", repo_path=str(tmp_path))

    assert rc == 0
    assert captured["reuse_existing"] is True
    env = captured["env"]
    assert env is not None
    assert env["CBM_CACHE_DIR"] == str(tc._code_intel_cache_dir())
    # Popen(env=...) REPLACES the whole child env — PATH must survive the merge.
    assert env["PATH"] == os.environ["PATH"]


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


# ---------------------------------------------------------------------------
# 2b04a361 — Office-slot spawn env forces UTF-8 stdio for the child so the
# third-party Python MCP servers (docx-mcp / powerpoint-mcp) can't crash their
# own loggers on non-ASCII (e.g. Chinese) log lines under Windows cp1252.
# ---------------------------------------------------------------------------

def test_office_slot_spawn_env_forces_utf8_with_plugin_env(monkeypatch):
    # The `word` slot carries a plugin env (MCP_AUTHOR); the UTF-8 stdio vars are
    # layered on top of it AND the parent env, so the docx-mcp child writes UTF-8.
    monkeypatch.setenv("PARENT_ONLY", "keep")
    out = tc._office_slot_spawn_env({"MCP_AUTHOR": "Adam", "MCP_AUTHOR_INITIALS": "AC"})
    assert out is not None
    # UTF-8 stdio forced (the actual fix).
    assert out["PYTHONIOENCODING"] == "utf-8:replace"
    assert out["PYTHONUTF8"] == "1"
    # Plugin env still merged.
    assert out["MCP_AUTHOR"] == "Adam"
    assert out["MCP_AUTHOR_INITIALS"] == "AC"
    # Parent env still inherited.
    assert out["PARENT_ONLY"] == "keep"


def test_office_slot_spawn_env_forces_utf8_without_plugin_env(monkeypatch):
    # A slot with NO plugin env (ppt/dc) must STILL get the UTF-8 override — unlike
    # _plugin_spawn_env, which would return None (inherit parent) here. We never
    # inherit silently; we always materialise the encoding vars.
    monkeypatch.setenv("PARENT_ONLY", "keep")
    for empty in (None, {}, "nope", {"": "blank"}):
        out = tc._office_slot_spawn_env(empty)
        assert out is not None
        assert out["PYTHONIOENCODING"] == "utf-8:replace"
        assert out["PYTHONUTF8"] == "1"
        assert out["PARENT_ONLY"] == "keep"


def test_office_slot_spawn_env_encoding_survives_nonascii_log_line(monkeypatch):
    # End-to-end proof that the declared child encoding neutralises the bug: a
    # non-ASCII (Chinese) log line, encoded with the encoding+errorhandler the
    # child is told to use, does NOT raise — the exact crash 2b04a361 fixes.
    #   Under the buggy cp1252 default this raises UnicodeEncodeError; under the
    #   forced "utf-8:replace" it round-trips (or degrades gracefully) instead.
    env = tc._office_slot_spawn_env(None)
    raw = env["PYTHONIOENCODING"]  # e.g. "utf-8:replace"
    enc, _, errors = raw.partition(":")
    errors = errors or "strict"
    chinese_log = "docx-mcp 日志：正在生成文档 — 完成"  # non-ASCII + em-dash
    # This is what the child's logger does when it writes to its stdout stream.
    encoded = chinese_log.encode(enc, errors=errors)
    assert isinstance(encoded, bytes) and encoded  # no UnicodeEncodeError raised
    # Sanity: the same line under Windows' legacy cp1252 strict default WOULD crash,
    # confirming the fix is load-bearing (not a no-op).
    with pytest.raises(UnicodeEncodeError):
        chinese_log.encode("cp1252")


def test_run_tunnel_office_slot_child_gets_utf8_stdio_env(monkeypatch, tmp_path):
    """End-to-end: enabling the `powerpoint` slot spawns powerpoint-mcp with
    PYTHONIOENCODING=utf-8:replace + PYTHONUTF8=1 in the child env (2b04a361).
    The office spawn env must carry the UTF-8 override alongside the plugin's
    own env vars.

    4b26c2ef — this used to exercise the `word` slot (docx-mcp), but word is
    now RETIRED: resolve_plugins() forces its `enabled` to False
    unconditionally, so it can no longer be lazy-spawned via config at all —
    swapped to `powerpoint`, a still-overridable office slot, to keep this
    end-to-end UTF-8-env coverage alive. The tenant config below also keeps a
    `word: {enabled: true}` entry alongside it, asserting the companion
    regression: even with an explicit enable, word must NOT spawn."""
    _stub_run_tunnel_spawn(monkeypatch)

    # Capture the env each Popen is handed (FakeProc only records cmd).
    spawned: list[dict] = []
    real_fake_popen = tc.subprocess.Popen

    def cap_popen(cmd, *a, **k):
        spawned.append({"cmd": cmd, "env": k.get("env")})
        return real_fake_popen(cmd, *a, **k)

    monkeypatch.setattr(tc.subprocess, "Popen", cap_popen)
    monkeypatch.setattr(tc, "_restore_mcp_json", lambda *a, **k: None)
    # detect_office_binaries must not auto-enable slots from the host PATH — we
    # drive enablement purely from the config so the test is host-independent.
    # run_tunnel imports it locally from meridian.tunnel_plugins, so patch it there.
    import meridian.tunnel_plugins as _tp
    monkeypatch.setattr(_tp, "detect_office_binaries", lambda *a, **k: set())

    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={
            "tenant_id": "tid-word", "plan": "pro",
            "tunnel_plugins_config": [
                {"name": "powerpoint", "enabled": True},
                # 4b26c2ef — retired: must be ignored even though explicitly enabled.
                {"name": "word", "enabled": True},
            ],
        }),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tmp_path))
    assert rc == 0

    # Find the powerpoint-mcp (powerpoint slot) spawn and inspect its env.
    ppt_spawns = [
        s for s in spawned
        if any("powerpoint-mcp" in str(t) for t in s["cmd"])
    ]
    assert ppt_spawns, "powerpoint slot (powerpoint-mcp) was not spawned"
    env = ppt_spawns[0]["env"]
    assert env is not None, "powerpoint slot spawned with inherited env — UTF-8 not forced"
    assert env.get("PYTHONIOENCODING") == "utf-8:replace"
    assert env.get("PYTHONUTF8") == "1"

    # 4b26c2ef — word is RETIRED: forced off regardless of the explicit
    # `enabled: true` above, so docx-mcp must never spawn at all.
    word_spawns = [
        s for s in spawned
        if any("docx-mcp" in str(t) for t in s["cmd"])
    ]
    assert not word_spawns, "retired word slot (docx-mcp) was spawned despite being retired"


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


# ---------------------------------------------------------------------------
# 12afe021 — outputs/debug slots must actually be wired into run_tunnel
# (previously declared in BUILTIN_PLUGINS + fully wired server-side, but the
# client never created a SlotProxy or reconnect loop for either).
# ---------------------------------------------------------------------------

def test_run_tunnel_wires_outputs_slot_when_enabled(monkeypatch, tmp_path):
    """Enabling the 'outputs' slot (meridian-outputs) via tunnel_plugins_config
    must spawn its SlotProxy through the same office-family lazy-spawn +
    reconnect-loop path used by ppt/word/dc/docs/zotero — the exact wiring
    that was previously entirely missing for this slot."""
    procs = _stub_run_tunnel_spawn(monkeypatch)
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={
            "tenant_id": "tid-outputs", "plan": "pro",
            "tunnel_plugins_config": [
                {"name": "meridian-outputs", "enabled": True},
            ],
        }),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tmp_path))
    assert rc == 0
    # fs + code + extract (core, default-enabled) + the newly-wired outputs slot.
    assert len(procs) == 4
    outputs_spawns = [p for p in procs if any("meridian-outputs-mcp" in str(t) for t in p.cmd)]
    assert outputs_spawns, "outputs slot (meridian-outputs-mcp) was not spawned"


def test_run_tunnel_wires_debug_slot_when_enabled(monkeypatch, tmp_path):
    """Enabling the 'debug' slot (mcp-debugger) via tunnel_plugins_config must
    spawn its SlotProxy the same way. debug's session_mode is 'persistent'
    (BUILTIN_PLUGINS), so it must be added to office_ports/office_proxies and
    NOT get an idle-killer, mirroring Desktop Commander."""
    procs = _stub_run_tunnel_spawn(monkeypatch)
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={
            "tenant_id": "tid-debug", "plan": "pro",
            "tunnel_plugins_config": [
                {"name": "mcp-debugger", "enabled": True},
            ],
        }),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tmp_path))
    assert rc == 0
    assert len(procs) == 4
    debug_spawns = [p for p in procs if any("@debugmcp/mcp-debugger" in str(t) for t in p.cmd)]
    assert debug_spawns, "debug slot (mcp-debugger) was not spawned"


def test_run_tunnel_debug_slot_disabled_by_default_stays_unwired(monkeypatch, tmp_path):
    """debug (like zotero) is opt-in / disabled-by-default in BUILTIN_PLUGINS
    (121e6a27) — with NO override at all, it must NOT spawn. This is the other
    half of the 12afe021 fix: joining the office-family loop must respect each
    plugin's own `enabled` flag, not spawn unconditionally."""
    procs = _stub_run_tunnel_spawn(monkeypatch)
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={"tenant_id": "tid-nodebug", "plan": "pro"}),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tmp_path))
    assert rc == 0
    # Only the core fs/code/extract slots spawn — outputs/debug stay disabled
    # by default, matching BUILTIN_PLUGINS' enabled=False for both.
    assert len(procs) == 3
    assert not any("mcp-debugger" in str(t) for p in procs for t in p.cmd)
    assert not any("meridian-outputs-mcp" in str(t) for p in procs for t in p.cmd)


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
    # No-op: reads/writes a REAL Path.home()/".meridian"/spawned_pids.json, not
    # test-scoped -- see the matching no-op + comment in _stub_run_tunnel_spawn.
    monkeypatch.setattr(tc, "_kill_all_previously_spawned_pids", lambda *a, **k: None)

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


def test_run_tunnel_legacy_resolved_list_extract_none_falls_back_to_serena_default(
    monkeypatch, tmp_path,
):
    """9d9a92cc: Backward-compat — an older server sends an already-resolved
    plugin list with code-extractor command=None. This must now fall back to
    the CURRENT default (Serena, via the SerenaDaemonPool), NOT skip the slot
    (the old behavior, which routed a None command through the now-obsolete
    _resolve_extractor_inner_cmd/mcp-server-code-extractor fallback and
    treated a None resolver result as "unavailable"). See
    _resolve_extract_slot_command's docstring for the full history."""
    procs = _stub_run_tunnel_spawn(monkeypatch, code_binary=None)
    # The legacy extractor resolver must NOT be consulted anymore for a
    # missing command — if it were, this stub returning None would still
    # incorrectly skip the slot, defeating the point of this test.
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
    # fs spawns; extract command=None now resolves to the Serena pool default
    # instead of skipping → 2 procs (fs + the pool-spawned Serena daemon).
    assert rc == 0
    assert len(procs) == 2
    assert any(
        "serena-agent" in str(t) for p in procs for t in p.cmd
    ), "extract slot did not fall back to the current Serena default"
    assert not any(
        "mcp-server-code-extractor" in str(t) for p in procs for t in p.cmd
    ), "extract slot must never silently launch the obsolete extractor package"


def test_run_tunnel_explicit_legacy_extractor_override_still_honored(monkeypatch, tmp_path):
    """ada39096 — the flip side of the defaulting fix above: a tenant who
    EXPLICITLY configures the old mcp-server-code-extractor command for the
    extract slot must still get it (an explicit choice is never overridden),
    via the generic custom-command path — not via _resolve_extractor_inner_cmd,
    which stays uncalled either way now that it is no longer wired into this
    decision as an implicit fallback."""
    procs = _stub_run_tunnel_spawn(monkeypatch, code_binary=None)
    legacy_extractor_calls = []
    monkeypatch.setattr(
        tc, "_resolve_extractor_inner_cmd",
        lambda: legacy_extractor_calls.append(1) or ["uvx", "mcp-server-code-extractor"],
    )
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={
            "tenant_id": "t", "plan": "pro",
            "tunnel_plugins_config": {
                "code-extractor": {"command": ["uvx", "mcp-server-code-extractor"]},
            },
        }),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tmp_path))
    assert rc == 0
    assert len(procs) == 2  # fs + the explicitly-overridden extract proxy
    assert any("mcp-server-code-extractor" in p.cmd for p in procs), [p.cmd for p in procs]
    assert not any("serena-agent" in p.cmd for p in procs)
    assert legacy_extractor_calls == []


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
    # No-op: reads/writes a REAL Path.home()/".meridian"/spawned_pids.json, not
    # test-scoped -- see the matching no-op + comment in _stub_run_tunnel_spawn.
    monkeypatch.setattr(tc, "_kill_all_previously_spawned_pids", lambda *a, **k: None)

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
        # subprocess.run() spawns via `with Popen(...) as process:` -- needed for
        # _kill_all_previously_spawned_pids' Windows taskkill call.
        def __enter__(self): return self
        def __exit__(self, *exc_info): return False

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
    # Built-ins still point at the hosted relay (ef162c28 — new plugin-derived key).
    assert entries["filesystem"]["url"].startswith("https://usemeridian.us/fs/mcp/")
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
    monkeypatch.setattr(tc, "_restore_mcp_json", lambda *a, **k: None)

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
    monkeypatch.setattr(tc, "_restore_mcp_json", lambda *a, **k: None)
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
    monkeypatch.setattr(tc, "_restore_mcp_json", lambda *a, **k: None)
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
        # subprocess.run() spawns via `with Popen(...) as process:` -- needed for
        # _kill_all_previously_spawned_pids' Windows taskkill call.
        def __enter__(self): return self
        def __exit__(self, *exc_info): return False

    monkeypatch.setattr(tc.subprocess, "Popen", lambda cmd, *a, **k: TimeoutProc())

    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tmp_path))
    assert rc == 0
    # Every started proc (fs+code+extract) had .kill() invoked after wait timeout.
    assert killed["n"] >= 1
