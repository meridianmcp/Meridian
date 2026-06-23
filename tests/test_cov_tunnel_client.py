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

    async def fake_relay(client, base, msg, label=""):
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

    async def fake_run_connection(ws_url, port, label):
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
# run_tunnel — happy-ish path: all slots up, reconnect loops short-circuit
# ---------------------------------------------------------------------------

def _stub_run_tunnel_spawn(monkeypatch, *, code_binary="/bin/codebase-memory-mcp",
                           extractor_inner=None):
    """Patch out all real I/O so run_tunnel can run end-to-end synchronously."""
    monkeypatch.setattr(tc, "_force_utf8_io", lambda: None)
    monkeypatch.setattr(tc, "_resolve_token", lambda t: "sk_tok")
    monkeypatch.setattr(tc, "_find_npx", lambda: "npx")
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
        def terminate(self): pass
        def wait(self, timeout=None): return 0
        def kill(self): pass

    monkeypatch.setattr(tc.subprocess, "Popen", lambda cmd, *a, **k: FakeProc(cmd))

    # Reconnect loops + watchdogs should return immediately (no real WS / polling).
    async def fake_reconnect(ws_url, port, label):
        return None

    async def fake_watchdog(holder, poll_interval=3.0):
        return None

    monkeypatch.setattr(tc, "_reconnect_loop", fake_reconnect)
    monkeypatch.setattr(tc, "_proc_watchdog", fake_watchdog)
    return procs


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


def test_run_tunnel_fs_npx_not_found_returns_1(monkeypatch, tmp_path):
    _stub_run_tunnel_spawn(monkeypatch)
    monkeypatch.setattr(
        tc, "_fetch_me",
        AsyncMock(return_value={"tenant_id": "t", "plan": "pro"}),
    )
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))

    def popen_enoent(cmd, *a, **k):
        raise FileNotFoundError("npx missing")

    monkeypatch.setattr(tc.subprocess, "Popen", popen_enoent)
    rc = _run_tunnel(token="sk_tok", base_url="https://x", repo_path=str(tmp_path))
    # fs proxy spawn FileNotFoundError → early return 1 (lines 940-946).
    assert rc == 1


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
    """code-intel + extractor Popen raising → warning branch, proc set to None,
    but fs proxy keeps the tunnel alive (exit 0)."""
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

    async def fake_reconnect(ws_url, port, label):
        return None

    async def fake_watchdog(holder, poll_interval=3.0):
        return None

    monkeypatch.setattr(tc, "_reconnect_loop", fake_reconnect)
    monkeypatch.setattr(tc, "_proc_watchdog", fake_watchdog)

    class FakeProc:
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
