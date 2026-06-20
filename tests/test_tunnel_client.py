"""Tests for the `meridian --tunnel` client (meridian/tunnel_client.py).

Covers config resolution, URL building, the npx/proxy command construction,
asset selection, auto-download logic, and the request-relay framing (via an
httpx MockTransport). Network and subprocess orchestration in run_tunnel() is
not exercised here.
"""
from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock

import httpx

from meridian import tunnel_client as tc


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------

def test_resolve_token_prefers_arg(monkeypatch):
    monkeypatch.setenv("MERIDIAN_API_KEY", "sk_meridian_env")
    assert tc._resolve_token("sk_meridian_arg") == "sk_meridian_arg"


def test_resolve_token_env_precedence(monkeypatch):
    monkeypatch.delenv("MERIDIAN_API_KEY", raising=False)
    monkeypatch.setenv("BEARER_TOKEN", "sk_meridian_bearer")
    assert tc._resolve_token() == "sk_meridian_bearer"

    monkeypatch.setenv("MERIDIAN_API_KEY", "sk_meridian_apikey")
    assert tc._resolve_token() == "sk_meridian_apikey"


def test_resolve_token_strips_bearer_prefix(monkeypatch):
    monkeypatch.delenv("MERIDIAN_API_KEY", raising=False)
    monkeypatch.delenv("BEARER_TOKEN", raising=False)
    assert tc._resolve_token("Bearer sk_meridian_x") == "sk_meridian_x"


def test_resolve_token_empty_when_unset(monkeypatch):
    monkeypatch.delenv("MERIDIAN_API_KEY", raising=False)
    monkeypatch.delenv("BEARER_TOKEN", raising=False)
    assert tc._resolve_token() == ""


# ---------------------------------------------------------------------------
# Base URL resolution
# ---------------------------------------------------------------------------

def test_resolve_base_url_default(monkeypatch):
    monkeypatch.delenv("MERIDIAN_URL", raising=False)
    assert tc._resolve_base_url() == tc.DEFAULT_BASE_URL


def test_resolve_base_url_arg_overrides_env(monkeypatch):
    monkeypatch.setenv("MERIDIAN_URL", "https://env.example.com")
    assert tc._resolve_base_url("https://arg.example.com/") == "https://arg.example.com"


def test_resolve_base_url_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("MERIDIAN_URL", "https://x.example.com/")
    assert tc._resolve_base_url() == "https://x.example.com"


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------

def test_ws_url_https_to_wss():
    url = tc._ws_url("https://usemeridian.us", "tenant-123", "sk_tok")
    assert url.startswith("wss://usemeridian.us/tunnel/tenant-123?token=")
    assert "token=sk_tok" in url


def test_ws_url_http_to_ws():
    url = tc._ws_url("http://localhost:7878", "t1", "tok")
    assert url.startswith("ws://localhost:7878/tunnel/t1?token=")


def test_ws_url_quotes_token():
    url = tc._ws_url("https://x", "t", "a/b+c d")
    # '/', '+', and space must be percent-encoded
    assert "a%2Fb%2Bc%20d" in url


def test_permanent_url_targets_mcp_transport_endpoint():
    # Must point at the /mcp transport, NOT the proxy root (which 404s).
    assert (
        tc._permanent_url("https://usemeridian.us/", "abc")
        == "https://usemeridian.us/fs/mcp/abc/mcp"
    )


def test_sse_url_targets_sse_endpoint():
    assert (
        tc._sse_url("https://usemeridian.us", "abc")
        == "https://usemeridian.us/fs/mcp/abc/sse"
    )


def test_ws_code_url_https_to_wss():
    url = tc._ws_code_url("https://usemeridian.us", "tenant-123", "sk_tok")
    assert url.startswith("wss://usemeridian.us/tunnel-code/tenant-123?token=")
    assert "token=sk_tok" in url


def test_ws_code_url_http_to_ws():
    url = tc._ws_code_url("http://localhost:7878", "t1", "tok")
    assert url.startswith("ws://localhost:7878/tunnel-code/t1?token=")


def test_permanent_code_url():
    assert (
        tc._permanent_code_url("https://usemeridian.us/", "abc")
        == "https://usemeridian.us/code/mcp/abc/mcp"
    )


def test_ws_extract_url_https_to_wss():
    url = tc._ws_extract_url("https://usemeridian.us", "tenant-123", "sk_tok")
    assert url.startswith("wss://usemeridian.us/tunnel-extract/tenant-123?token=")
    assert "token=sk_tok" in url


def test_ws_extract_url_http_to_ws():
    url = tc._ws_extract_url("http://localhost:7878", "t1", "tok")
    assert url.startswith("ws://localhost:7878/tunnel-extract/t1?token=")


def test_permanent_extract_url():
    assert (
        tc._permanent_extract_url("https://usemeridian.us/", "abc")
        == "https://usemeridian.us/extract/mcp/abc/mcp"
    )


# ---------------------------------------------------------------------------
# npx + proxy command
# ---------------------------------------------------------------------------

def test_find_npx_returns_nonempty_string():
    assert isinstance(tc._find_npx(), str)
    assert tc._find_npx()


def test_build_proxy_command_structure():
    cmd = tc._build_proxy_command("npx", "/repo", port=9000)
    assert cmd[0] == "npx"
    assert "mcp-proxy" in cmd
    assert "--port" in cmd
    assert "9000" in cmd
    # Streamable HTTP + stateless mode required for the tunnel relay.
    assert "--server" in cmd
    assert "stream" in cmd
    assert "--stateless" in cmd
    # The separator + the wrapped filesystem server must be present.
    assert "--" in cmd
    assert "@modelcontextprotocol/server-filesystem" in cmd
    assert cmd[-1] == "/repo"
    # mcp-proxy comes before the separator; filesystem server after it.
    sep = cmd.index("--")
    assert "mcp-proxy" in cmd[:sep]
    assert "@modelcontextprotocol/server-filesystem" in cmd[sep:]
    # Inner command is bare npx (resolved by mcp-proxy / the shell), not a path.
    assert cmd[sep + 1] == "npx"


def test_build_proxy_command_uses_shell_on_windows(monkeypatch):
    # --shell is required on Windows (Node refuses to spawn .cmd without it);
    # omitted elsewhere to avoid unescaped shell arg concatenation.
    monkeypatch.setattr(tc.sys, "platform", "win32")
    assert "--shell" in tc._build_proxy_command("npx.cmd", "C:/repo")
    monkeypatch.setattr(tc.sys, "platform", "linux")
    assert "--shell" not in tc._build_proxy_command("npx", "/repo")


def test_build_code_proxy_command_structure():
    cmd = tc._build_code_proxy_command("npx", "/usr/local/bin/codebase-memory-mcp", port=9009)
    assert cmd[0] == "npx"
    assert "mcp-proxy" in cmd
    assert "--port" in cmd
    assert "9009" in cmd
    assert "--server" in cmd
    assert "stream" in cmd
    assert "--stateless" in cmd
    assert "--" in cmd
    sep = cmd.index("--")
    assert cmd[sep + 1] == "/usr/local/bin/codebase-memory-mcp"
    # codebase-memory-mcp is a native binary — no --shell needed
    assert "--shell" not in cmd


def test_build_extractor_proxy_command_structure(monkeypatch):
    # mcp-proxy (outer) is npm; the inner launcher is the PyPI extractor (uvx here).
    monkeypatch.setattr(tc.sys, "platform", "linux")
    inner = ["uvx", "mcp-server-code-extractor"]
    cmd = tc._build_extractor_proxy_command("npx", inner, port=9010)
    assert cmd[0] == "npx"
    assert "mcp-proxy" in cmd
    assert "--port" in cmd
    assert "9010" in cmd
    assert "--server" in cmd
    assert "stream" in cmd
    assert "--stateless" in cmd
    sep = cmd.index("--")
    # Inner command is the resolved extractor launcher, NOT npx.
    assert cmd[sep + 1:] == inner
    # No --shell on non-Windows.
    assert "--shell" not in cmd


def test_build_extractor_proxy_command_no_shell_for_uvx_exe_on_windows(monkeypatch):
    # uvx.exe is a real executable → spawns directly, no --shell needed.
    monkeypatch.setattr(tc.sys, "platform", "win32")
    inner = [r"C:\Users\13144\.local\bin\uvx.exe", "mcp-server-code-extractor"]
    cmd = tc._build_extractor_proxy_command(r"C:\npm\npx.cmd", inner, port=9010)
    assert "--shell" not in cmd
    sep = cmd.index("--")
    assert cmd[sep + 1:] == inner


def test_build_extractor_proxy_command_shell_for_cmd_inner_on_windows(monkeypatch):
    # A .cmd/.bat inner launcher must go through a shell on Windows.
    monkeypatch.setattr(tc.sys, "platform", "win32")
    inner = [r"C:\some\tool.cmd", "mcp-server-code-extractor"]
    cmd = tc._build_extractor_proxy_command(r"C:\npm\npx.cmd", inner, port=9010)
    assert "--shell" in cmd
    sep = cmd.index("--")
    assert cmd[sep + 1:] == inner


def test_build_extractor_proxy_command_python_module_inner(monkeypatch):
    # pip fallback: inner is `python -m code_extractor`.
    monkeypatch.setattr(tc.sys, "platform", "linux")
    inner = ["/env/bin/python", "-m", "code_extractor"]
    cmd = tc._build_extractor_proxy_command("npx", inner, port=9010)
    sep = cmd.index("--")
    assert cmd[sep + 1:] == ["/env/bin/python", "-m", "code_extractor"]


# ---------------------------------------------------------------------------
# _find_uvx / _resolve_extractor_inner_cmd
# ---------------------------------------------------------------------------

def test_find_uvx_from_path(monkeypatch):
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/usr/local/bin/uvx" if name == "uvx" else None)
    assert tc._find_uvx() == "/usr/local/bin/uvx"


def test_find_uvx_local_bin_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(tc.shutil, "which", lambda name: None)
    monkeypatch.setattr(tc.sys, "platform", "linux")
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    uvx = tmp_path / ".local" / "bin" / "uvx"
    uvx.parent.mkdir(parents=True)
    uvx.touch()
    assert tc._find_uvx() == str(uvx)


def test_find_uvx_returns_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(tc.shutil, "which", lambda name: None)
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    assert tc._find_uvx() is None


def test_resolve_extractor_inner_prefers_uvx(monkeypatch):
    monkeypatch.setattr(tc, "_find_uvx", lambda: "/usr/local/bin/uvx")
    assert tc._resolve_extractor_inner_cmd() == ["/usr/local/bin/uvx", "mcp-server-code-extractor"]


def test_resolve_extractor_inner_pip_fallback_when_no_uvx(monkeypatch):
    monkeypatch.setattr(tc, "_find_uvx", lambda: None)
    import importlib.util as _ilu
    # Package not yet importable → triggers pip install.
    monkeypatch.setattr(_ilu, "find_spec", lambda name: None)
    installed = {}

    def fake_run(cmd, **kw):
        installed["cmd"] = cmd
        return None  # subprocess.run(..., check=True) return value is unused

    monkeypatch.setattr(tc.subprocess, "run", fake_run)
    monkeypatch.setattr(tc.sys, "executable", "/env/bin/python")
    result = tc._resolve_extractor_inner_cmd()
    assert result == ["/env/bin/python", "-m", "code_extractor"]
    # pip install was attempted for the PyPI package.
    assert installed["cmd"][:4] == ["/env/bin/python", "-m", "pip", "install"]
    assert "mcp-server-code-extractor" in installed["cmd"]


def test_resolve_extractor_inner_skips_install_when_already_present(monkeypatch):
    monkeypatch.setattr(tc, "_find_uvx", lambda: None)
    import importlib.util as _ilu
    monkeypatch.setattr(_ilu, "find_spec", lambda name: object())  # already importable
    called = {"run": False}
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: called.__setitem__("run", True))
    monkeypatch.setattr(tc.sys, "executable", "/env/bin/python")
    result = tc._resolve_extractor_inner_cmd()
    assert result == ["/env/bin/python", "-m", "code_extractor"]
    assert called["run"] is False  # no pip install when already present


def test_resolve_extractor_inner_returns_none_on_install_failure(monkeypatch):
    monkeypatch.setattr(tc, "_find_uvx", lambda: None)
    import importlib.util as _ilu
    monkeypatch.setattr(_ilu, "find_spec", lambda name: None)

    def boom(*a, **k):
        raise RuntimeError("pip exploded")

    monkeypatch.setattr(tc.subprocess, "run", boom)
    assert tc._resolve_extractor_inner_cmd() is None


def test_build_code_proxy_command_uses_shell_for_cmd_shim_on_windows(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "win32")
    shim = r"C:\Users\13144\AppData\Roaming\npm\codebase-memory-mcp.cmd"
    cmd = tc._build_code_proxy_command("npx.cmd", shim, port=9009)
    assert "--shell" in cmd  # .cmd shim must go through a shell
    sep = cmd.index("--")
    assert cmd[sep + 1] == shim


def test_build_code_proxy_command_no_shell_for_native_exe_on_windows(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "win32")
    exe = r"C:\Users\13144\.meridian\bin\codebase-memory-mcp.exe"
    cmd = tc._build_code_proxy_command("npx.cmd", exe, port=9009)
    # A real .exe spawns directly — no --shell (preserves space-in-path support).
    assert "--shell" not in cmd
    sep = cmd.index("--")
    assert cmd[sep + 1] == exe


# ---------------------------------------------------------------------------
# _index_code_dir
# ---------------------------------------------------------------------------

def test_index_code_dir_succeeds_after_probe(monkeypatch):
    """_index_code_dir waits for proxy readiness then calls tools/call."""
    calls = []

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, **kw):
            calls.append(kw.get("json", {}).get("method"))
            return FakeResp()

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)

    asyncio.run(tc._index_code_dir(8809, "/repo"))
    assert "tools/list" in calls
    assert "tools/call" in calls


def test_index_code_dir_gives_up_after_timeout(monkeypatch):
    """_index_code_dir exits gracefully when the proxy never starts."""
    import httpx as _httpx

    class ErrorClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **kw): raise Exception("refused")

    monkeypatch.setattr(_httpx, "AsyncClient", ErrorClient)
    # Patch sleep to avoid a 60-second real wait
    sleep_count = []

    async def fast_sleep(n):
        sleep_count.append(n)
        if len(sleep_count) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    try:
        asyncio.run(tc._index_code_dir(8809, "/repo"))
    except asyncio.CancelledError:
        pass
    assert len(sleep_count) > 0


# ---------------------------------------------------------------------------
# _find_codebase_memory_mcp — PATH + managed dir
# ---------------------------------------------------------------------------

def test_find_codebase_memory_mcp_checks_path(monkeypatch):
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/usr/bin/codebase-memory-mcp")
    assert tc._find_codebase_memory_mcp() == "/usr/bin/codebase-memory-mcp"


def test_find_codebase_memory_mcp_checks_managed_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(tc.shutil, "which", lambda name: None)
    monkeypatch.setattr(tc, "_managed_bin_dir", lambda: tmp_path)
    # Point APPDATA at an empty dir so the Windows npm-global probe finds nothing.
    monkeypatch.setenv("APPDATA", str(tmp_path / "empty-appdata"))
    # Create the binary in the managed dir.
    bin_name = "codebase-memory-mcp.exe" if tc.sys.platform == "win32" else "codebase-memory-mcp"
    (tmp_path / bin_name).touch()
    result = tc._find_codebase_memory_mcp()
    assert result == str(tmp_path / bin_name)


def test_find_codebase_memory_mcp_returns_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(tc.shutil, "which", lambda name: None)
    monkeypatch.setattr(tc, "_managed_bin_dir", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "empty-appdata"))
    assert tc._find_codebase_memory_mcp() is None


def test_find_codebase_memory_mcp_npm_global_cmd_on_windows(monkeypatch, tmp_path):
    # shutil.which misses (npm dir not on PATH) but the .cmd shim exists in the
    # npm global location → fallback must resolve it. Force win32 so the probe runs.
    monkeypatch.setattr(tc.sys, "platform", "win32")
    monkeypatch.setattr(tc.shutil, "which", lambda name: None)
    monkeypatch.setattr(tc, "_managed_bin_dir", lambda: tmp_path / "managed")
    appdata = tmp_path / "AppData" / "Roaming"
    npm_dir = appdata / "npm"
    npm_dir.mkdir(parents=True)
    shim = npm_dir / "codebase-memory-mcp.cmd"
    shim.touch()
    monkeypatch.setenv("APPDATA", str(appdata))
    assert tc._find_codebase_memory_mcp() == str(shim)


# ---------------------------------------------------------------------------
# _pick_release_asset
# ---------------------------------------------------------------------------

_FAKE_ASSETS = [
    {"name": "codebase-memory-mcp-x86_64-pc-windows-msvc.exe", "browser_download_url": "https://gh/win.exe"},
    {"name": "codebase-memory-mcp-x86_64-unknown-linux-musl",  "browser_download_url": "https://gh/linux"},
    {"name": "codebase-memory-mcp-aarch64-apple-darwin",        "browser_download_url": "https://gh/mac-arm"},
    {"name": "codebase-memory-mcp-x86_64-apple-darwin",         "browser_download_url": "https://gh/mac-x64"},
    {"name": "codebase-memory-mcp-source.tar.gz",               "browser_download_url": "https://gh/src.tar.gz"},
]


def test_pick_release_asset_windows(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "win32")
    import platform as _p
    monkeypatch.setattr(_p, "machine", lambda: "AMD64")
    asset = tc._pick_release_asset(_FAKE_ASSETS)
    assert asset is not None
    assert asset["name"].endswith(".exe")


def test_pick_release_asset_windows_never_picks_darwin(monkeypatch):
    """Core regression: darwin-amd64 must NOT be selected on Windows even if
    no Windows asset exists — arch match alone must not beat hard OS exclusion."""
    monkeypatch.setattr(tc.sys, "platform", "win32")
    import platform as _p
    monkeypatch.setattr(_p, "machine", lambda: "AMD64")
    darwin_only = [
        {"name": "codebase-memory-mcp-x86_64-apple-darwin",
         "browser_download_url": "https://gh/mac-x64"},
        {"name": "codebase-memory-mcp-aarch64-apple-darwin",
         "browser_download_url": "https://gh/mac-arm"},
    ]
    assert tc._pick_release_asset(darwin_only) is None


def test_pick_release_asset_linux_never_picks_windows(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "linux")
    import platform as _p
    monkeypatch.setattr(_p, "machine", lambda: "x86_64")
    win_only = [
        {"name": "codebase-memory-mcp-x86_64-pc-windows-msvc.exe",
         "browser_download_url": "https://gh/win.exe"},
    ]
    assert tc._pick_release_asset(win_only) is None


def test_pick_release_asset_linux(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "linux")
    import platform as _p
    monkeypatch.setattr(_p, "machine", lambda: "x86_64")
    asset = tc._pick_release_asset(_FAKE_ASSETS)
    assert asset is not None
    assert "linux" in asset["name"]
    assert not asset["name"].endswith(".tar.gz")


def test_pick_release_asset_macos_arm(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "darwin")
    import platform as _p
    monkeypatch.setattr(_p, "machine", lambda: "arm64")
    asset = tc._pick_release_asset(_FAKE_ASSETS)
    assert asset is not None
    assert "aarch64" in asset["name"] or "arm64" in asset["name"]


def test_pick_release_asset_returns_none_for_empty():
    assert tc._pick_release_asset([]) is None


# ---------------------------------------------------------------------------
# _ensure_codebase_memory_mcp — find-or-download
# ---------------------------------------------------------------------------

def test_ensure_returns_existing_binary(monkeypatch):
    monkeypatch.setattr(tc, "_find_codebase_memory_mcp", lambda: "/usr/bin/codebase-memory-mcp")
    result = asyncio.run(tc._ensure_codebase_memory_mcp())
    assert result == "/usr/bin/codebase-memory-mcp"


def test_ensure_downloads_when_missing(monkeypatch):
    monkeypatch.setattr(tc, "_find_codebase_memory_mcp", lambda: None)
    monkeypatch.setattr(tc, "_download_codebase_memory_mcp", AsyncMock(return_value="/tmp/codebase-memory-mcp"))
    result = asyncio.run(tc._ensure_codebase_memory_mcp())
    assert result == "/tmp/codebase-memory-mcp"


def test_ensure_returns_none_on_download_failure(monkeypatch):
    monkeypatch.setattr(tc, "_find_codebase_memory_mcp", lambda: None)
    monkeypatch.setattr(tc, "_download_codebase_memory_mcp", AsyncMock(return_value=None))
    result = asyncio.run(tc._ensure_codebase_memory_mcp())
    assert result is None


def test_download_codebase_memory_mcp_installs_binary(monkeypatch, tmp_path):
    """_download_codebase_memory_mcp writes binary and returns its path."""
    monkeypatch.setattr(tc, "_managed_bin_dir", lambda: tmp_path)

    fake_release = {
        "tag_name": "v1.2.3",
        "assets": [
            {"name": "codebase-memory-mcp-x86_64-unknown-linux-musl",
             "browser_download_url": "https://gh/linux"},
        ],
    }
    fake_content = b"\x7fELF fake binary"

    def make_mock_client(*args, **kwargs):
        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return fake_release
            content = fake_content

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kw): return FakeResp()

        return FakeClient()

    monkeypatch.setattr(tc.sys, "platform", "linux")
    monkeypatch.setattr(tc, "_pick_release_asset", lambda assets: assets[0])

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "AsyncClient", make_mock_client)

    result = asyncio.run(tc._download_codebase_memory_mcp())
    assert result is not None
    dest = tmp_path / "codebase-memory-mcp"
    assert dest.exists()
    assert dest.read_bytes() == fake_content


def test_download_codebase_memory_mcp_returns_none_on_http_error(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "_managed_bin_dir", lambda: tmp_path)

    def make_error_client(*args, **kwargs):
        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kw):
                raise Exception("network error")

        return FakeClient()

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "AsyncClient", make_error_client)

    result = asyncio.run(tc._download_codebase_memory_mcp())
    assert result is None


# ---------------------------------------------------------------------------
# Request relay
# ---------------------------------------------------------------------------

def _relay(msg, handler):
    """Run _relay_request against a mock local proxy defined by `handler`."""
    async def _inner():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await tc._relay_request(client, "http://127.0.0.1:8808", msg)

    return asyncio.run(_inner())


def test_relay_request_success_roundtrip():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    msg = {
        "type": "request",
        "id": "req-1",
        "method": "POST",
        "path": "/message",
        "query": "sessionId=42",
        "headers": {"content-type": "application/json", "host": "stale-host"},
        "body": base64.b64encode(b'{"hello":1}').decode(),
    }
    resp = _relay(msg, handler)

    assert resp["type"] == "response"
    assert resp["id"] == "req-1"
    assert resp["status"] == 200
    decoded = json.loads(base64.b64decode(resp["body"]))
    assert decoded == {"ok": True}
    # query was appended, body forwarded, stale Host dropped.
    assert captured["url"] == "http://127.0.0.1:8808/message?sessionId=42"
    assert captured["method"] == "POST"
    assert captured["body"] == b'{"hello":1}'


def test_relay_request_empty_body_when_no_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    msg = {"type": "request", "id": "r2", "method": "GET", "path": "/"}
    resp = _relay(msg, handler)
    assert resp["status"] == 204
    assert resp["body"] == ""


def test_relay_request_local_failure_returns_502():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    msg = {"type": "request", "id": "r3", "method": "GET", "path": "/sse"}
    resp = _relay(msg, handler)
    assert resp["type"] == "response"
    assert resp["id"] == "r3"
    assert resp["status"] == 502
    err = json.loads(base64.b64decode(resp["body"]))
    assert "local proxy error" in err["error"]


def test_relay_request_drops_host_header():
    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(200)

    msg = {
        "type": "request", "id": "r4", "method": "GET", "path": "/",
        "headers": {"Host": "evil.example.com", "x-keep": "yes"},
    }
    _relay(msg, handler)
    # httpx sets Host to the real target, never the forwarded stale value.
    assert seen_headers.get("host") == "127.0.0.1:8808"
    assert seen_headers.get("x-keep") == "yes"


# ---------------------------------------------------------------------------
# .mcp.json auto-update (STEP 2)
# ---------------------------------------------------------------------------

def test_tunnel_mcp_entries_urls():
    entries = tc._tunnel_mcp_entries("https://usemeridian.us", "tid-123")
    assert set(entries) == {"meridian-fs", "meridian-code", "meridian-extractor"}
    assert entries["meridian-fs"]["type"] == "http"
    assert entries["meridian-fs"]["url"] == "https://usemeridian.us/fs/mcp/tid-123/mcp"
    assert entries["meridian-code"]["url"] == "https://usemeridian.us/code/mcp/tid-123/mcp"
    assert entries["meridian-extractor"]["url"] == "https://usemeridian.us/extract/mcp/tid-123/mcp"


def test_inject_mcp_entries_into_empty():
    out = tc._inject_mcp_entries(None, {"meridian-fs": {"type": "http", "url": "u"}})
    data = json.loads(out)
    assert data["mcpServers"]["meridian-fs"]["url"] == "u"


def test_inject_mcp_entries_preserves_existing_servers_and_keys():
    existing = json.dumps({
        "mcpServers": {"meridian": {"command": "pixi", "args": ["run"]}},
        "otherTopLevel": 7,
    })
    entries = tc._tunnel_mcp_entries("https://usemeridian.us", "t1")
    out = tc._inject_mcp_entries(existing, entries)
    data = json.loads(out)
    # Existing connector and unrelated keys survive; ours are merged in.
    assert data["mcpServers"]["meridian"]["command"] == "pixi"
    assert data["otherTopLevel"] == 7
    assert data["mcpServers"]["meridian-fs"]["url"].endswith("/fs/mcp/t1/mcp")
    assert "meridian-code" in data["mcpServers"]


def test_inject_mcp_entries_recovers_from_malformed_json():
    out = tc._inject_mcp_entries("{not json", {"meridian-fs": {"type": "http", "url": "u"}})
    data = json.loads(out)
    assert data["mcpServers"]["meridian-fs"]["url"] == "u"


def test_mcp_json_paths_includes_cursor_only_when_present(tmp_path):
    # No .cursor/mcp.json → only .mcp.json is targeted.
    paths = tc._mcp_json_paths(tmp_path)
    assert paths == [tmp_path / ".mcp.json"]

    cursor = tmp_path / ".cursor" / "mcp.json"
    cursor.parent.mkdir(parents=True)
    cursor.write_text("{}", encoding="utf-8")
    paths = tc._mcp_json_paths(tmp_path)
    assert cursor in paths


def test_install_mcp_json_creates_then_restore_deletes(tmp_path):
    snaps = tc._install_mcp_json(tmp_path, "https://usemeridian.us", "tid")
    mcp = tmp_path / ".mcp.json"
    assert mcp.exists()
    data = json.loads(mcp.read_text(encoding="utf-8"))
    assert "meridian-extractor" in data["mcpServers"]
    # original was None (we created it) → restore deletes the file
    tc._restore_mcp_json(snaps)
    assert not mcp.exists()


def test_install_mcp_json_restore_returns_exact_original(tmp_path):
    mcp = tmp_path / ".mcp.json"
    original = '{\n  "mcpServers": {\n    "meridian": {"command": "pixi"}\n  }\n}\n'
    mcp.write_text(original, encoding="utf-8")

    snaps = tc._install_mcp_json(tmp_path, "https://usemeridian.us", "tid")
    # During the session our entries are present alongside the user's.
    live = json.loads(mcp.read_text(encoding="utf-8"))
    assert "meridian-fs" in live["mcpServers"]
    assert live["mcpServers"]["meridian"]["command"] == "pixi"

    tc._restore_mcp_json(snaps)
    # Restored byte-for-byte to what the user had.
    assert mcp.read_text(encoding="utf-8") == original


def test_install_mcp_json_updates_existing_cursor_config(tmp_path):
    cursor = tmp_path / ".cursor" / "mcp.json"
    cursor.parent.mkdir(parents=True)
    cursor.write_text("{}", encoding="utf-8")

    snaps = tc._install_mcp_json(tmp_path, "https://usemeridian.us", "tid")
    paths = {p for p, _ in snaps}
    assert cursor in paths
    data = json.loads(cursor.read_text(encoding="utf-8"))
    assert "meridian-fs" in data["mcpServers"]


# ---------------------------------------------------------------------------
# _force_utf8_io — Windows cp1252 crash guard
# ---------------------------------------------------------------------------

def test_force_utf8_io_sets_env_and_reconfigures(monkeypatch):
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    calls = []

    class FakeStream:
        def reconfigure(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(tc.sys, "stdout", FakeStream())
    monkeypatch.setattr(tc.sys, "stderr", FakeStream())
    tc._force_utf8_io()
    assert tc.os.environ["PYTHONIOENCODING"] == "utf-8"
    # Both streams reconfigured to utf-8 with replacement (never raises).
    assert calls == [{"encoding": "utf-8", "errors": "replace"}] * 2


def test_force_utf8_io_tolerates_streams_without_reconfigure(monkeypatch):
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    # A stream lacking .reconfigure (e.g. pytest capture) must not raise.
    monkeypatch.setattr(tc.sys, "stdout", object())
    monkeypatch.setattr(tc.sys, "stderr", object())
    tc._force_utf8_io()  # should not raise
    assert tc.os.environ["PYTHONIOENCODING"] == "utf-8"


# ---------------------------------------------------------------------------
# Token cache — _config_path / _read_cached_token / _write_cached_token
# ---------------------------------------------------------------------------

def test_config_path_under_home(monkeypatch, tmp_path):
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    assert tc._config_path() == tmp_path / ".meridian" / "config.json"


def test_read_cached_token_missing_file_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "_config_path", lambda: tmp_path / "config.json")
    assert tc._read_cached_token("https://usemeridian.us") is None


def test_write_then_read_cached_token_roundtrips(monkeypatch, tmp_path):
    cfg = tmp_path / ".meridian" / "config.json"
    monkeypatch.setattr(tc, "_config_path", lambda: cfg)
    tc._write_cached_token("https://usemeridian.us", "sk_meridian_abc")
    # File written under a freshly-created parent dir.
    assert cfg.exists()
    assert tc._read_cached_token("https://usemeridian.us") == "sk_meridian_abc"


def test_read_cached_token_rejects_other_base_url(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(tc, "_config_path", lambda: cfg)
    tc._write_cached_token("https://usemeridian.us", "sk_meridian_abc")
    # A token cached for one base_url must never be served for another.
    assert tc._read_cached_token("https://other.example") is None


def test_read_cached_token_rejects_expired(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(tc, "_config_path", lambda: cfg)
    tc._write_cached_token("https://usemeridian.us", "sk_meridian_abc")
    # Rewind the cached expiry into the past.
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["tunnel_token"]["expires_at"] = 0
    cfg.write_text(json.dumps(data), encoding="utf-8")
    assert tc._read_cached_token("https://usemeridian.us") is None


def test_read_cached_token_tolerates_malformed_json(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(tc, "_config_path", lambda: cfg)
    cfg.write_text("{not valid json", encoding="utf-8")
    assert tc._read_cached_token("https://usemeridian.us") is None


def test_write_cached_token_preserves_other_keys(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(tc, "_config_path", lambda: cfg)
    cfg.write_text(json.dumps({"unrelated": "keep-me"}), encoding="utf-8")
    tc._write_cached_token("https://usemeridian.us", "sk_meridian_xyz")
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["unrelated"] == "keep-me"
    assert data["tunnel_token"]["token"] == "sk_meridian_xyz"


# ---------------------------------------------------------------------------
# _build_proxy_for_inner — generic mcp-proxy wrapper for command overrides
# ---------------------------------------------------------------------------

def test_build_proxy_for_inner_structure():
    cmd = tc._build_proxy_for_inner("npx", ["codegraph", "--stdio"], 8809)
    assert cmd[:7] == ["npx", "-y", "mcp-proxy", "--port", "8809",
                       "--server", "stream"]
    assert "--stateless" in cmd
    # Inner command appears after the -- separator.
    assert cmd[cmd.index("--") + 1:] == ["codegraph", "--stdio"]


def test_build_proxy_for_inner_no_shell_for_exe_on_windows(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "win32")
    cmd = tc._build_proxy_for_inner("npx.cmd", ["codegraph.exe"], 8809)
    assert "--shell" not in cmd


def test_build_proxy_for_inner_shell_for_cmd_shim_on_windows(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "win32")
    cmd = tc._build_proxy_for_inner("npx.cmd", ["codegraph.cmd"], 8809)
    assert "--shell" in cmd
