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


# ---------------------------------------------------------------------------
# path-quote-strip — _normalize_path_arg de-quotes pasted path args so a
# literal surrounding '"C:\\My Docs"' / "'C:/x'" is served correctly. Only
# MATCHED surrounding quote pairs are removed; interior chars & lone quotes
# are left untouched. Idempotent.
# ---------------------------------------------------------------------------

def test_normalize_path_arg_strips_matched_double_quotes():
    assert tc._normalize_path_arg('"C:\\Users\\me\\My Docs"') == "C:\\Users\\me\\My Docs"


def test_normalize_path_arg_strips_matched_single_quotes():
    assert tc._normalize_path_arg("'C:/x'") == "C:/x"


def test_normalize_path_arg_strips_surrounding_whitespace_then_quotes():
    assert tc._normalize_path_arg('  "p"  ') == "p"


def test_normalize_path_arg_clean_path_unchanged():
    p = r"C:\Users\me\Documents"
    assert tc._normalize_path_arg(p) == p


def test_normalize_path_arg_preserves_interior_space():
    # An interior space (unquoted) is a real path char — never stripped.
    assert tc._normalize_path_arg("C:/Program Files/App") == "C:/Program Files/App"


def test_normalize_path_arg_leaves_lone_leading_quote():
    # A single unmatched quote is NOT a wrapping pair — keep it verbatim.
    assert tc._normalize_path_arg('"C:/x') == '"C:/x'
    assert tc._normalize_path_arg("C:/x'") == "C:/x'"


def test_normalize_path_arg_unwinds_repeated_and_mixed_wrapping():
    assert tc._normalize_path_arg('\'"C:/x"\'') == "C:/x"


def test_normalize_path_arg_empty_and_whitespace():
    assert tc._normalize_path_arg("") == ""
    assert tc._normalize_path_arg("   ") == ""
    assert tc._normalize_path_arg('""') == ""


def test_build_proxy_command_strips_quotes_from_real_root(tmp_path):
    # A quoted root pointing at a REAL dir must be served as that (de-quoted)
    # dir — proving the quote-strip took effect end-to-end in the builder.
    quoted = f'"{tmp_path}"'
    cmd = tc._build_proxy_command("npx", str(tmp_path), roots=[quoted])
    assert str(tmp_path) in cmd
    assert quoted not in cmd


def test_unservable_roots_ignores_quotes_for_real_dir(tmp_path):
    # A quoted root that resolves to an existing dir is NOT flagged unservable
    # (the quotes are stripped before the os.path.isdir check).
    quoted = f'"{tmp_path}"'
    assert tc._unservable_roots([quoted]) == []


def test_build_code_proxy_command_structure(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "linux")  # test POSIX path — no --shell
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


def test_build_code_proxy_command_shell_for_native_exe_on_windows(monkeypatch):
    # .exe binaries also need --shell on Windows: mcp-proxy (Node.js) can't spawn
    # them directly in all environments. Matches FS slot's unconditional --shell.
    monkeypatch.setattr(tc.sys, "platform", "win32")
    exe = r"C:\Users\13144\.meridian\bin\codebase-memory-mcp.exe"
    cmd = tc._build_code_proxy_command("npx.cmd", exe, port=9009)
    assert "--shell" in cmd
    sep = cmd.index("--")
    assert cmd[sep + 1] == exe


# ---------------------------------------------------------------------------
# 89bc72c4 — code-intel slot: a binary path with a SPACE was split by cmd.exe
# under mcp-proxy --shell → WinError 3 "The system cannot find the path
# specified". The command builder now resolves such a path to its 8.3 short
# name (_win_shell_safe_path) so cmd.exe sees a single space-free token.
# ---------------------------------------------------------------------------

def test_win_shell_safe_path_noop_on_posix(monkeypatch):
    # Non-Windows: never rewrite the path (no cmd.exe splitting to worry about).
    monkeypatch.setattr(tc.sys, "platform", "linux")
    p = "/home/john smith/.meridian/bin/codebase-memory-mcp"
    assert tc._win_shell_safe_path(p) == p


def test_win_shell_safe_path_noop_without_space(monkeypatch):
    # Space-free path: nothing to fix, returned verbatim (no short-path lookup).
    monkeypatch.setattr(tc.sys, "platform", "win32")
    p = r"C:\Users\13144\.meridian\bin\codebase-memory-mcp.exe"
    assert tc._win_shell_safe_path(p) == p


def test_win_shell_safe_path_uses_short_name_for_spaced_path(monkeypatch):
    # When the path has a space, resolve it to the 8.3 short (space-free) form.
    # GetShortPathNameW is Windows-only, so stub the ctypes call so this runs on
    # the Linux CI runner too (no real Windows stdlib attr is touched).
    monkeypatch.setattr(tc.sys, "platform", "win32")
    spaced = r"C:\Users\John Smith\.meridian\bin\codebase-memory-mcp.exe"
    short = r"C:\Users\JOHNSM~1\.meridian\bin\CODEBA~1.EXE"

    def fake_safe(path):
        return short if path == spaced else path

    monkeypatch.setattr(tc, "_win_shell_safe_path", fake_safe)
    cmd = tc._build_code_proxy_command("npx.cmd", spaced, port=9009)
    assert "--shell" in cmd
    sep = cmd.index("--")
    # The inner binary token is the space-free short path — cmd.exe won't split it.
    assert cmd[sep + 1] == short
    assert " " not in cmd[sep + 1]


def test_win_shell_safe_path_falls_back_when_short_name_unavailable(monkeypatch):
    # 8.3 generation disabled / lookup fails → return the original path unchanged
    # (fail-soft: no worse than before; the startup warning surfaces the risk).
    monkeypatch.setattr(tc.sys, "platform", "win32")
    spaced = r"C:\Users\John Smith\.meridian\bin\codebase-memory-mcp.exe"
    # ctypes.windll doesn't exist on non-Windows → the lazy import raises inside
    # _win_shell_safe_path, which swallows it and returns the input unchanged.
    assert tc._win_shell_safe_path(spaced) == spaced


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
    fake_content = b"\x7fELF fake binary" + b"\x00" * (1024 * 1024)  # >1MB to pass size check

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
# Tool-name prefixing (b4455202)
# ---------------------------------------------------------------------------

_TOOLS_LIST_JSON = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "tools": [
            {"name": "read_file", "description": "Read a file"},
            {"name": "write_file", "description": "Write a file"},
        ]
    },
}


def test_prefix_tool_name_adds_prefix():
    assert tc._prefix_tool_name("read_file", "Filesystem") == "Filesystem__read_file"


def test_prefix_tool_name_never_double_prefixes():
    once = tc._prefix_tool_name("read_file", "Filesystem")
    assert tc._prefix_tool_name(once, "Filesystem") == "Filesystem__read_file"


def test_prefix_tool_name_passes_non_string_through():
    assert tc._prefix_tool_name(None, "Filesystem") is None
    assert tc._prefix_tool_name(123, "Filesystem") == 123


def test_apply_tool_prefix_plain_json():
    body = json.dumps(_TOOLS_LIST_JSON).encode()
    out = json.loads(tc._apply_tool_prefix(body, "Filesystem"))
    names = [t["name"] for t in out["result"]["tools"]]
    assert names == ["Filesystem__read_file", "Filesystem__write_file"]


def test_apply_tool_prefix_sse_framed():
    body = (
        b"event: message\r\n"
        + b"data: " + json.dumps(_TOOLS_LIST_JSON).encode() + b"\r\n\r\n"
    )
    out = tc._apply_tool_prefix(body, "Serena").decode()
    # SSE framing (event line, CRLF, blank separator) is preserved.
    assert out.startswith("event: message\r\n")
    assert out.endswith("\r\n\r\n")
    # The data payload's tool names are prefixed.
    data_line = [ln for ln in out.splitlines() if ln.startswith("data:")][0]
    payload = json.loads(data_line[len("data:"):].strip())
    assert [t["name"] for t in payload["result"]["tools"]] == [
        "Serena__read_file", "Serena__write_file"
    ]


def test_apply_tool_prefix_no_prefix_is_noop():
    body = json.dumps(_TOOLS_LIST_JSON).encode()
    assert tc._apply_tool_prefix(body, None) == body
    assert tc._apply_tool_prefix(body, "") == body


def test_apply_tool_prefix_leaves_non_tools_list_untouched():
    # A tools/call result (no result.tools list) must pass through byte-for-byte.
    body = json.dumps({
        "jsonrpc": "2.0", "id": 2,
        "result": {"content": [{"type": "text", "text": "hi"}]},
    }).encode()
    assert tc._apply_tool_prefix(body, "Filesystem") == body


def test_apply_tool_prefix_non_json_passthrough():
    body = b"not json at all { tools"
    assert tc._apply_tool_prefix(body, "Filesystem") == body


def test_apply_tool_prefix_never_double_prefixes_body():
    body = json.dumps(_TOOLS_LIST_JSON).encode()
    once = tc._apply_tool_prefix(body, "Filesystem")
    twice = tc._apply_tool_prefix(once, "Filesystem")
    names = [t["name"] for t in json.loads(twice)["result"]["tools"]]
    assert names == ["Filesystem__read_file", "Filesystem__write_file"]


def test_relay_request_prefixes_tools_list_and_drops_content_length():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_TOOLS_LIST_JSON)

    msg = {"type": "request", "id": "t1", "method": "POST", "path": "/mcp"}

    async def _inner():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await tc._relay_request(
                client, "http://127.0.0.1:8808", msg, tool_prefix="Filesystem"
            )

    resp = asyncio.run(_inner())
    out = json.loads(base64.b64decode(resp["body"]))
    assert [t["name"] for t in out["result"]["tools"]] == [
        "Filesystem__read_file", "Filesystem__write_file"
    ]
    # Content-Length is recomputed by the server, so the stale one is dropped.
    assert not any(k.lower() == "content-length" for k in resp["headers"])


def test_relay_request_no_prefix_leaves_tools_unchanged():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_TOOLS_LIST_JSON)

    msg = {"type": "request", "id": "t2", "method": "POST", "path": "/mcp"}
    resp = _relay(msg, handler)
    out = json.loads(base64.b64decode(resp["body"]))
    assert [t["name"] for t in out["result"]["tools"]] == ["read_file", "write_file"]


def test_builtin_plugins_have_prefix_field():
    from meridian.tunnel_plugins import BUILTIN_PLUGINS
    by_name = {p["name"]: p for p in BUILTIN_PLUGINS}
    # 49905647 — every builtin prefix MUST be None: the server-side bridge
    # (routes/tunnel.py SLOT_DISPLAY_NAMES) already namespaces each slot's tools,
    # so a client prefix here would double-prefix (filesystem__Filesystem__...).
    assert by_name["filesystem"]["prefix"] is None
    assert by_name["code-extractor"]["prefix"] is None
    assert by_name["code-intel"]["prefix"] is None
    assert by_name["powerpoint"]["prefix"] is None
    # Every builtin declares the field (so resolve_plugins always carries it).
    assert all("prefix" in p for p in BUILTIN_PLUGINS)


def test_resolve_plugins_carries_prefix():
    from meridian.tunnel_plugins import resolve_plugins
    by_slot = {p["slot"]: p for p in resolve_plugins(None)}
    # All None — server bridge owns the only tool-name namespacing. (49905647)
    assert by_slot["fs"]["prefix"] is None
    assert by_slot["extract"]["prefix"] is None
    assert by_slot["code"]["prefix"] is None


# ---------------------------------------------------------------------------
# .mcp.json auto-update (STEP 2)
# ---------------------------------------------------------------------------

def test_tunnel_mcp_entries_urls():
    # ef162c28 — connectors are keyed by the plugin behind each slot.
    entries = tc._tunnel_mcp_entries("https://usemeridian.us", "tid-123")
    assert set(entries) == {"filesystem", "codebase-memory", "serena"}
    assert entries["filesystem"]["type"] == "http"
    assert entries["filesystem"]["url"] == "https://usemeridian.us/fs/mcp/tid-123/mcp"
    assert entries["codebase-memory"]["url"] == "https://usemeridian.us/code/mcp/tid-123/mcp"
    assert entries["serena"]["url"] == "https://usemeridian.us/extract/mcp/tid-123/mcp"
    # The new keys are exactly TUNNEL_MCP_KEYS; the legacy set is distinct.
    assert set(tc.TUNNEL_MCP_KEYS) == {"filesystem", "codebase-memory", "serena"}
    assert tc._LEGACY_TUNNEL_MCP_KEYS == (
        "meridian-fs", "meridian-code", "meridian-extractor",
    )


def test_inject_mcp_entries_into_empty():
    out = tc._inject_mcp_entries(None, {"filesystem": {"type": "http", "url": "u"}})
    data = json.loads(out)
    assert data["mcpServers"]["filesystem"]["url"] == "u"


def test_inject_mcp_entries_preserves_existing_servers_and_keys():
    existing = json.dumps({
        "mcpServers": {"meridian": {"command": "pixi", "args": ["run"]}},
        "otherTopLevel": 7,
    })
    entries = tc._tunnel_mcp_entries("https://usemeridian.us", "t1")
    out = tc._inject_mcp_entries(existing, entries, "https://usemeridian.us", "t1")
    data = json.loads(out)
    # Existing connector and unrelated keys survive; ours are merged in.
    assert data["mcpServers"]["meridian"]["command"] == "pixi"
    assert data["otherTopLevel"] == 7
    assert data["mcpServers"]["filesystem"]["url"].endswith("/fs/mcp/t1/mcp")
    assert "codebase-memory" in data["mcpServers"]


def test_inject_mcp_entries_recovers_from_malformed_json():
    out = tc._inject_mcp_entries("{not json", {"filesystem": {"type": "http", "url": "u"}})
    data = json.loads(out)
    assert data["mcpServers"]["filesystem"]["url"] == "u"


def test_inject_mcp_entries_migrates_legacy_slot_named_keys():
    # ef162c28 — an existing .mcp.json from an OLD tunnel build has the legacy
    # slot-named keys. Injecting must REMOVE those and write the new plugin
    # names, leaving no stale duplicates pointing at the same URLs.
    base, tid = "https://usemeridian.us", "t9"
    existing = json.dumps({"mcpServers": {
        "meridian-fs": {"type": "http", "url": f"{base}/fs/mcp/{tid}/mcp"},
        "meridian-code": {"type": "http", "url": f"{base}/code/mcp/{tid}/mcp"},
        "meridian-extractor": {"type": "http", "url": f"{base}/extract/mcp/{tid}/mcp"},
        "user-thing": {"command": "node"},
    }})
    entries = tc._tunnel_mcp_entries(base, tid)
    out = tc._inject_mcp_entries(existing, entries, base, tid)
    servers = json.loads(out)["mcpServers"]
    # Legacy keys gone, new keys present, exactly once each — no duplicates.
    assert "meridian-fs" not in servers
    assert "meridian-code" not in servers
    assert "meridian-extractor" not in servers
    assert servers["filesystem"]["url"] == f"{base}/fs/mcp/{tid}/mcp"
    assert servers["codebase-memory"]["url"] == f"{base}/code/mcp/{tid}/mcp"
    assert servers["serena"]["url"] == f"{base}/extract/mcp/{tid}/mcp"
    # The user's own server is untouched.
    assert servers["user-thing"] == {"command": "node"}


def test_inject_mcp_entries_reinject_is_idempotent():
    # ef162c28 — running the tunnel twice must not accumulate duplicate or
    # suffixed entries: a prior run's new-named entries are ours (by URL/name)
    # and get rewritten in place.
    base, tid = "https://usemeridian.us", "t7"
    entries = tc._tunnel_mcp_entries(base, tid)
    first = tc._inject_mcp_entries(None, entries, base, tid)
    second = tc._inject_mcp_entries(first, entries, base, tid)
    servers = json.loads(second)["mcpServers"]
    assert set(servers) == {"filesystem", "codebase-memory", "serena"}


def test_inject_mcp_entries_collision_with_user_server_is_suffixed():
    # ef162c28 — the user runs their OWN `filesystem` server (different URL).
    # We must NOT clobber it: write under a suffixed key instead.
    base, tid = "https://usemeridian.us", "t3"
    existing = json.dumps({"mcpServers": {
        "filesystem": {"type": "http", "url": "https://example.com/my-fs"},
    }})
    entries = tc._tunnel_mcp_entries(base, tid)
    out = tc._inject_mcp_entries(existing, entries, base, tid)
    servers = json.loads(out)["mcpServers"]
    # The user's server is untouched at its original key.
    assert servers["filesystem"] == {"type": "http", "url": "https://example.com/my-fs"}
    # Ours landed under a suffixed key pointing at our relay.
    assert servers["filesystem-meridian"]["url"] == f"{base}/fs/mcp/{tid}/mcp"
    # code/extract had no collision → plain names.
    assert servers["codebase-memory"]["url"] == f"{base}/code/mcp/{tid}/mcp"
    assert servers["serena"]["url"] == f"{base}/extract/mcp/{tid}/mcp"


def test_unique_mcp_key_suffix_escalation():
    # ef162c28 — first collision → -meridian, then -meridian-2, -3, …
    assert tc._unique_mcp_key("filesystem", set()) == "filesystem"
    assert tc._unique_mcp_key("filesystem", {"filesystem"}) == "filesystem-meridian"
    assert tc._unique_mcp_key(
        "filesystem", {"filesystem", "filesystem-meridian"}
    ) == "filesystem-meridian-2"
    assert tc._unique_mcp_key(
        "filesystem", {"filesystem", "filesystem-meridian", "filesystem-meridian-2"}
    ) == "filesystem-meridian-3"


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
    assert "serena" in data["mcpServers"]  # ef162c28 — new plugin-derived name
    # original was None (we created it) → restore deletes the file
    tc._restore_mcp_json(snaps, "https://usemeridian.us", "tid")
    assert not mcp.exists()


def test_install_mcp_json_fresh_file_gets_new_names(tmp_path):
    # ef162c28 (a) — a brand new .mcp.json gets the plugin-derived names, and
    # none of the legacy slot names.
    tc._install_mcp_json(tmp_path, "https://usemeridian.us", "tid")
    servers = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert set(servers) == {"filesystem", "codebase-memory", "serena"}
    assert not (set(servers) & set(tc._LEGACY_TUNNEL_MCP_KEYS))


def test_install_mcp_json_migrates_existing_legacy_entries(tmp_path):
    # ef162c28 (b) — an existing .mcp.json with the OLD keys is migrated:
    # old removed, new present, no duplicates, user's own server preserved.
    base, tid = "https://usemeridian.us", "tid"
    mcp = tmp_path / ".mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {
        "meridian-fs": {"type": "http", "url": f"{base}/fs/mcp/{tid}/mcp"},
        "meridian-code": {"type": "http", "url": f"{base}/code/mcp/{tid}/mcp"},
        "meridian-extractor": {"type": "http", "url": f"{base}/extract/mcp/{tid}/mcp"},
        "mine": {"command": "x"},
    }}), encoding="utf-8")

    tc._install_mcp_json(tmp_path, base, tid)
    servers = json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"]
    assert not (set(servers) & set(tc._LEGACY_TUNNEL_MCP_KEYS))
    assert {"filesystem", "codebase-memory", "serena"} <= set(servers)
    assert servers["mine"] == {"command": "x"}
    # No legacy+new duplicate pair for the fs relay URL.
    fs_url = f"{base}/fs/mcp/{tid}/mcp"
    assert [k for k, v in servers.items() if v.get("url") == fs_url] == ["filesystem"]


def test_install_mcp_json_restore_returns_exact_original(tmp_path):
    mcp = tmp_path / ".mcp.json"
    original = '{\n  "mcpServers": {\n    "meridian": {"command": "pixi"}\n  }\n}\n'
    mcp.write_text(original, encoding="utf-8")

    snaps = tc._install_mcp_json(tmp_path, "https://usemeridian.us", "tid")
    # During the session our entries are present alongside the user's.
    live = json.loads(mcp.read_text(encoding="utf-8"))
    assert "filesystem" in live["mcpServers"]
    assert live["mcpServers"]["meridian"]["command"] == "pixi"

    tc._restore_mcp_json(snaps, "https://usemeridian.us", "tid")
    # Restored byte-for-byte to what the user had (they had none of ours).
    assert mcp.read_text(encoding="utf-8") == original


def test_restore_mcp_json_removes_both_legacy_and_new_keys(tmp_path):
    # ef162c28 (d) — cleanup must strip BOTH legacy and new keys even if the
    # snapshotted original still held stale entries (e.g. a prior crash).
    base, tid = "https://usemeridian.us", "tid"
    mcp = tmp_path / ".mcp.json"
    # The "original" we snapshot already carries a stale legacy AND a stale new
    # entry (both ours) plus the user's own server.
    original = json.dumps({"mcpServers": {
        "meridian-fs": {"type": "http", "url": f"{base}/fs/mcp/{tid}/mcp"},
        "serena": {"type": "http", "url": f"{base}/extract/mcp/{tid}/mcp"},
        "mine": {"command": "x"},
    }})
    tc._restore_mcp_json([(mcp, original)], base, tid)
    servers = json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"]
    # Both a legacy and a new key were stripped; only the user's survives.
    assert "meridian-fs" not in servers
    assert "serena" not in servers
    assert servers == {"mine": {"command": "x"}}


def test_install_mcp_json_updates_existing_cursor_config(tmp_path):
    cursor = tmp_path / ".cursor" / "mcp.json"
    cursor.parent.mkdir(parents=True)
    cursor.write_text("{}", encoding="utf-8")

    snaps = tc._install_mcp_json(tmp_path, "https://usemeridian.us", "tid")
    paths = {p for p, _ in snaps}
    assert cursor in paths
    data = json.loads(cursor.read_text(encoding="utf-8"))
    assert "filesystem" in data["mcpServers"]


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


# ---------------------------------------------------------------------------
# Package cache locations — _package_cache_locations / first-run banner (a887155d)
# ---------------------------------------------------------------------------

def test_package_cache_locations_has_keys(monkeypatch):
    """Returns npx/uvx/uv_tools, all non-empty strings, no subprocess needed."""
    loc = tc._package_cache_locations()
    assert set(loc) == {"npx", "uvx", "uv_tools"}
    assert all(isinstance(v, str) and v for v in loc.values())
    # npx packages live under the npm cache's _npx subdir.
    assert loc["npx"].endswith("_npx")


def test_package_cache_locations_respects_env(monkeypatch):
    """Explicit package-manager env vars override the per-OS defaults."""
    monkeypatch.setenv("npm_config_cache", "/custom/npm")
    monkeypatch.setenv("UV_CACHE_DIR", "/custom/uvcache")
    monkeypatch.setenv("UV_TOOL_DIR", "/custom/uvtools")
    loc = tc._package_cache_locations()
    assert loc["npx"] == str(tc.Path("/custom/npm") / "_npx")
    assert loc["uvx"] == "/custom/uvcache"
    assert loc["uv_tools"] == "/custom/uvtools"


def test_print_package_cache_locations_first_run_then_suppressed(monkeypatch, tmp_path, capsys):
    """Prints once on first run, writes a marker, and stays silent thereafter."""
    marker = tmp_path / ".cache_locations_shown"
    monkeypatch.setattr(tc, "_cache_locations_marker", lambda: marker)

    assert tc._print_package_cache_locations() is True
    out1 = capsys.readouterr().out
    assert "Package caches" in out1
    assert marker.exists()

    # Second run: marker present → no print, returns False.
    assert tc._print_package_cache_locations() is False
    assert capsys.readouterr().out == ""

    # force=True prints again even with the marker present.
    assert tc._print_package_cache_locations(force=True) is True
    assert "Package caches" in capsys.readouterr().out


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


# ---------------------------------------------------------------------------
# 4ea1b9d5 — session_mode: persistent slots omit --stateless
# ---------------------------------------------------------------------------

def test_build_proxy_for_inner_persistent_omits_stateless():
    cmd = tc._build_proxy_for_inner("npx", ["dc", "--serve"], 8813, stateless=False)
    assert "--stateless" not in cmd
    # Streamable HTTP transport is still present.
    assert cmd[:6] == ["npx", "-y", "mcp-proxy", "--port", "8813", "--server"]
    assert cmd[cmd.index("--") + 1:] == ["dc", "--serve"]


def test_build_proxy_for_inner_stateless_default_true():
    cmd = tc._build_proxy_for_inner("npx", ["x"], 8813)
    assert "--stateless" in cmd


def test_reprobe_once_kills_and_respawns_stuck_persistent_slot():
    """a898710a — a persistent slot that's is_running=True but failing its health
    probe (inner server dead, parent alive) is force-restarted via kill+respawn
    and recovers on the re-probe."""
    class FakeProxy:
        def __init__(self):
            self.port = 8813
            self.is_running = True
            self.killed = 0
            self.ensured = 0

        def kill(self):
            self.killed += 1
            self.is_running = False

        async def ensure_running(self):
            self.ensured += 1
            self.is_running = True

    proxy = FakeProxy()
    calls = {"n": 0}

    async def probe(_port):
        calls["n"] += 1
        return calls["n"] >= 2  # unhealthy first, healthy after kill+respawn

    loop = asyncio.new_event_loop()
    try:
        healthy = loop.run_until_complete(tc._reprobe_once(proxy, probe))
    finally:
        loop.close()
    assert healthy is True
    assert proxy.killed == 1
    assert proxy.ensured == 1
    assert calls["n"] == 2


def test_reprobe_once_no_kill_when_healthy_first_probe():
    """a898710a — a slot healthy on the first probe is not needlessly killed."""
    class FakeProxy:
        port = 8813
        is_running = True
        killed = 0

        def kill(self):
            type(self).killed += 1

        async def ensure_running(self):
            pass

    proxy = FakeProxy()

    async def probe(_port):
        return True

    loop = asyncio.new_event_loop()
    try:
        healthy = loop.run_until_complete(tc._reprobe_once(proxy, probe))
    finally:
        loop.close()
    assert healthy is True
    assert proxy.killed == 0


# ---------------------------------------------------------------------------
# 089a936a — pre-flight diagnostics (reason/detail) + cold-fetch probe budget
# ---------------------------------------------------------------------------

def test_preflight_failure_hint_dc_is_specific():
    """089a936a — the dc slot's pre-flight failure hint names Desktop Commander,
    the port, and the npx install command."""
    reason, detail = tc._preflight_failure_hint("dc", 8813)
    assert reason == "unreachable"
    assert "Desktop Commander" in detail
    assert "8813" in detail
    assert "desktop-commander" in detail


def test_preflight_failure_hint_generic_slot():
    """089a936a — a non-cold-fetch slot still gets a non-empty reason + detail
    mentioning the slot label and port (generic, not the dc-specific text)."""
    reason, detail = tc._preflight_failure_hint("fs", 8810)
    assert reason == "unreachable"
    assert "fs" in detail
    assert "8810" in detail
    assert "Desktop Commander" not in detail


def test_preflight_slot_failure_reports_reason_and_detail(monkeypatch):
    """089a936a — a FAILED pre-flight reports a non-empty reason AND detail to
    _report_slot_health (not a bare healthy=False), so the dashboard can show an
    actionable warning. Uses the dc slot (port 8813) as the example, matching the
    _reprobe_once tests."""
    # Force the probe to fail without spawning anything real.
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=False))
    report = AsyncMock()
    monkeypatch.setattr(tc, "_report_slot_health", report)

    ws = object()  # never touched — _report_slot_health is mocked
    loop = asyncio.new_event_loop()
    try:
        healthy = loop.run_until_complete(tc._preflight_slot(ws, 8813, "dc"))
    finally:
        loop.close()

    assert healthy is False
    report.assert_awaited_once()
    args, kwargs = report.call_args
    # positional: (ws, label, healthy)
    assert args[1] == "dc"
    assert args[2] is False
    # 089a936a — reason + detail are populated (not a bare False report).
    assert kwargs.get("reason") == "unreachable"
    assert kwargs.get("detail")
    assert "Desktop Commander" in kwargs["detail"]
    assert "8813" in kwargs["detail"]


def test_preflight_slot_dc_uses_larger_cold_fetch_budget(monkeypatch):
    """089a936a — the dc (cold-fetch) slot's first pre-flight uses the LARGER
    probe budget (attempts=4, delay=5.0) to tolerate the npx cold download, while
    a normal slot uses the default (attempts=2, delay=3.0)."""
    calls: list[dict] = []

    async def fake_probe(port, *, attempts=2, delay=3.0):
        calls.append({"port": port, "attempts": attempts, "delay": delay})
        return True  # healthy → no unhealthy report path

    monkeypatch.setattr(tc, "_probe_slot_health", fake_probe)
    monkeypatch.setattr(tc, "_report_slot_health", AsyncMock())

    ws = object()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(tc._preflight_slot(ws, 8813, "dc"))
        loop.run_until_complete(tc._preflight_slot(ws, 8810, "fs"))
    finally:
        loop.close()

    assert calls[0] == {"port": 8813, "attempts": 4, "delay": 5.0}
    assert calls[1] == {"port": 8810, "attempts": 2, "delay": 3.0}
    # The larger budget must match the declared cold-fetch constant.
    assert (calls[0]["attempts"], calls[0]["delay"]) == tc._PREFLIGHT_BUDGET_COLD_FETCH
    assert (calls[1]["attempts"], calls[1]["delay"]) == tc._PREFLIGHT_BUDGET_DEFAULT


def test_preflight_slot_office_slots_use_cold_fetch_budget(monkeypatch):
    """24b6cb5d — the Office slots (ppt/word) uvx-download their inner server on
    first spawn, exactly like DC's npx fetch, so they must get the SAME larger
    cold-fetch pre-flight budget. Before this fix only dc did, and ppt failed the
    standard ~23s budget on a cold cache ('tunnel:ppt: pre-flight ... FAILED')."""
    # Both office slots are declared cold-fetch alongside dc.
    assert tc._COLD_FETCH_SLOTS == frozenset({"dc", "ppt", "word"})

    calls: list[dict] = []

    async def fake_probe(port, *, attempts=2, delay=3.0):
        calls.append({"port": port, "attempts": attempts, "delay": delay})
        return True

    monkeypatch.setattr(tc, "_probe_slot_health", fake_probe)
    monkeypatch.setattr(tc, "_report_slot_health", AsyncMock())

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(tc._preflight_slot(object(), 8811, "ppt"))
        loop.run_until_complete(tc._preflight_slot(object(), 8812, "word"))
    finally:
        loop.close()

    for c in calls:
        assert (c["attempts"], c["delay"]) == tc._PREFLIGHT_BUDGET_COLD_FETCH


def test_preflight_slot_explicit_budget_overrides_default(monkeypatch):
    """089a936a — explicit attempts/delay override the label-derived default, so
    callers keep full control and the background reprobe (attempts=1) is
    unaffected by the cold-fetch defaults."""
    calls: list[dict] = []

    async def fake_probe(port, *, attempts=2, delay=3.0):
        calls.append({"attempts": attempts, "delay": delay})
        return True

    monkeypatch.setattr(tc, "_probe_slot_health", fake_probe)
    monkeypatch.setattr(tc, "_report_slot_health", AsyncMock())

    loop = asyncio.new_event_loop()
    try:
        # dc slot but explicit budget → explicit wins over the cold-fetch default.
        loop.run_until_complete(
            tc._preflight_slot(object(), 8813, "dc", attempts=1, delay=0.0)
        )
    finally:
        loop.close()

    assert calls[0] == {"attempts": 1, "delay": 0.0}


def test_builtin_plugins_session_mode():
    from meridian.tunnel_plugins import BUILTIN_PLUGINS
    by_name = {p["name"]: p for p in BUILTIN_PLUGINS}
    # Desktop Commander runs stateful terminal sessions → persistent.
    assert by_name["desktop-commander"]["session_mode"] == "persistent"
    # Everything else is stateless (one-shot tunnel relay).
    for name in ("filesystem", "code-intel", "code-extractor", "powerpoint", "word"):
        assert by_name[name]["session_mode"] == "stateless"
    # Every builtin declares the field so resolve_plugins always carries it.
    assert all("session_mode" in p for p in BUILTIN_PLUGINS)


def test_resolve_plugins_carries_session_mode():
    from meridian.tunnel_plugins import resolve_plugins
    by_slot = {p["slot"]: p for p in resolve_plugins(None)}
    assert by_slot["dc"]["session_mode"] == "persistent"
    assert by_slot["fs"]["session_mode"] == "stateless"


# ---------------------------------------------------------------------------
# Windows shutdown hardening — process group + tree teardown (ff0809e1)
# ---------------------------------------------------------------------------

def test_spawn_kwargs_new_process_group_on_windows(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "win32")
    kw = tc._spawn_kwargs()
    # Children get their own group so a console Ctrl+C never reaches them (which
    # is what triggers the "Terminate batch job (Y/N)?" hang). CREATE_NEW_PROCESS_GROUP
    # is a Windows-only subprocess constant (0x00000200) — reference it by value so
    # this assertion also runs on the Linux CI host, where the attribute is absent.
    assert kw == {"creationflags": 0x00000200}


def test_spawn_kwargs_win32_when_constant_missing(monkeypatch):
    # Reproduces the Linux CI host: sys.platform forced to win32 but the stdlib
    # defines no CREATE_NEW_PROCESS_GROUP. _spawn_kwargs must fall back to the
    # literal value, not raise AttributeError (regression for the deploy gate).
    monkeypatch.setattr(tc.sys, "platform", "win32")
    monkeypatch.delattr(tc.subprocess, "CREATE_NEW_PROCESS_GROUP", raising=False)
    assert tc._spawn_kwargs() == {"creationflags": 0x00000200}


def test_spawn_kwargs_empty_on_posix(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "linux")
    assert tc._spawn_kwargs() == {}


def test_terminate_proc_tree_uses_taskkill_on_windows(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "win32")
    calls = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv

    class _Proc:
        pid = 4321
        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(tc.subprocess, "run", fake_run)
    tc._terminate_proc_tree(_Proc())
    # /T kills the whole tree (node + cmd grandchildren), /F forces it.
    assert calls["argv"] == ["taskkill", "/F", "/T", "/PID", "4321"]


def test_terminate_proc_tree_terminates_on_posix(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "linux")
    events = []

    class _Proc:
        pid = 10
        def terminate(self):
            events.append("terminate")
        def wait(self, timeout=None):
            events.append("wait")
            return 0
        def kill(self):
            events.append("kill")

    tc._terminate_proc_tree(_Proc())
    assert events == ["terminate", "wait"]  # clean exit, no kill escalation


def test_terminate_proc_tree_none_is_noop():
    # No proc for a slot that never spawned — must not raise.
    tc._terminate_proc_tree(None)


# ---------------------------------------------------------------------------
# 44892730 — _kill_stale_port_occupant: kill a prior-generation process still
# bound to a slot's port before the fresh tunnel-client generation spawns its
# own, closing the confirmed duplicate-process bug.
# ---------------------------------------------------------------------------


class _FakeAddr:
    def __init__(self, port):
        self.port = port


class _FakeConn:
    def __init__(self, port, pid, status="LISTEN"):
        self.laddr = _FakeAddr(port)
        self.pid = pid
        self.status = status


def _install_fake_psutil(monkeypatch, connections, *, sentinel_listen="LISTEN"):
    import types

    fake_psutil = types.ModuleType("psutil")
    fake_psutil.CONN_LISTEN = sentinel_listen
    fake_psutil.net_connections = lambda kind="inet": connections
    killed = {}

    class _FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        def terminate(self):
            killed.setdefault("terminate", []).append(self.pid)

        def wait(self, timeout=None):
            pass

        def kill(self):
            killed.setdefault("kill", []).append(self.pid)

    fake_psutil.Process = _FakeProcess
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)
    return killed


def test_kill_stale_port_occupant_kills_matching_listener_windows(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "win32")
    conns = [_FakeConn(port=8809, pid=999), _FakeConn(port=8810, pid=111)]
    _install_fake_psutil(monkeypatch, conns)
    calls = []
    monkeypatch.setattr(tc.subprocess, "run", lambda argv, **kw: calls.append(argv))

    tc._kill_stale_port_occupant(8809, "code")

    assert calls == [["taskkill", "/F", "/T", "/PID", "999"]]


def test_kill_stale_port_occupant_kills_matching_listener_posix(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "linux")
    conns = [_FakeConn(port=8809, pid=999)]
    killed = _install_fake_psutil(monkeypatch, conns)

    tc._kill_stale_port_occupant(8809, "code")

    assert killed.get("terminate") == [999]


def test_kill_stale_port_occupant_noop_when_no_listener_on_port(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "win32")
    conns = [_FakeConn(port=9999, pid=999)]  # different port
    _install_fake_psutil(monkeypatch, conns)
    calls = []
    monkeypatch.setattr(tc.subprocess, "run", lambda argv, **kw: calls.append(argv))

    tc._kill_stale_port_occupant(8809, "code")

    assert calls == []


def test_kill_stale_port_occupant_ignores_non_listen_connections(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "win32")
    conns = [_FakeConn(port=8809, pid=999, status="ESTABLISHED")]
    _install_fake_psutil(monkeypatch, conns)
    calls = []
    monkeypatch.setattr(tc.subprocess, "run", lambda argv, **kw: calls.append(argv))

    tc._kill_stale_port_occupant(8809, "code")

    assert calls == []


def test_kill_stale_port_occupant_survives_missing_psutil(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _fail_psutil_import(name, *a, **kw):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fail_psutil_import)
    tc._kill_stale_port_occupant(8809, "code")  # must not raise


def test_kill_stale_port_occupant_never_raises_on_internal_error(monkeypatch):
    import types

    fake_psutil = types.ModuleType("psutil")
    fake_psutil.CONN_LISTEN = "LISTEN"

    def _boom(kind="inet"):
        raise OSError("permission denied")

    fake_psutil.net_connections = _boom
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)

    tc._kill_stale_port_occupant(8809, "code")  # must not raise


def test_dc_default_command_wraps_cmd_on_windows(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "win32")
    assert tc._dc_default_command() == [
        "cmd", "/c", "npx", "-y", "@wonderwhy-er/desktop-commander@latest",
    ]


def test_dc_default_command_bare_npx_on_posix(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "linux")
    assert tc._dc_default_command() == [
        "npx", "-y", "@wonderwhy-er/desktop-commander@latest",
    ]


# ---------------------------------------------------------------------------
# _proc_watchdog — relaunch a local proxy subprocess when it dies (aff0b1d1)
# ---------------------------------------------------------------------------

class _StopLoop(Exception):
    """Sentinel raised from a patched asyncio.sleep to end the watchdog loop."""


class _DeadProc:
    returncode = 1

    def poll(self):
        return 1  # exited


class _LiveProc:
    returncode = None

    def poll(self):
        return None  # still running


def test_proc_watchdog_relaunches_on_exit(monkeypatch):
    holder = {"proc": _DeadProc(), "cmd": ["mcp-proxy", "--port", "8808"],
              "env": {"X": "1"}, "label": "fs"}
    spawned = []

    def fake_popen(cmd, env=None, **kwargs):
        spawned.append((cmd, env))
        return _LiveProc()

    monkeypatch.setattr(tc.subprocess, "Popen", fake_popen)

    calls = {"n": 0}

    async def fake_sleep(_d):
        calls["n"] += 1
        if calls["n"] >= 2:   # let the first iteration run, then break
            raise _StopLoop()

    monkeypatch.setattr(tc.asyncio, "sleep", fake_sleep)

    try:
        asyncio.run(tc._proc_watchdog(holder, poll_interval=0))
    except _StopLoop:
        pass

    # The dead proc was relaunched from holder['cmd'] with holder['env'].
    assert spawned == [(["mcp-proxy", "--port", "8808"], {"X": "1"})]
    assert isinstance(holder["proc"], _LiveProc)


def test_proc_watchdog_leaves_running_proc(monkeypatch):
    holder = {"proc": _LiveProc(), "cmd": ["x"], "env": None, "label": "code"}
    spawned = []
    monkeypatch.setattr(tc.subprocess, "Popen",
                        lambda *a, **k: spawned.append(1) or _LiveProc())

    async def fake_sleep(_d):
        raise _StopLoop()

    monkeypatch.setattr(tc.asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(tc._proc_watchdog(holder))
    except _StopLoop:
        pass
    assert spawned == []  # a running proc is never relaunched


def test_proc_watchdog_gives_up_after_max_retries(monkeypatch):
    """Crash isolation (edff64a6): a proxy that keeps exiting (e.g. ENOENT) is
    relaunched at most ``max_retries`` times, then the watchdog gives up and
    returns instead of spin-looping forever. No _StopLoop needed — if the loop
    were still unbounded this test would hang."""
    holder = {"proc": _DeadProc(), "cmd": ["broken-cmd"], "env": None, "label": "extract"}
    spawned = []

    # Every relaunch yields another already-dead proc → the slot never recovers.
    def fake_popen(cmd, env=None, **kwargs):
        spawned.append(cmd)
        return _DeadProc()

    monkeypatch.setattr(tc.subprocess, "Popen", fake_popen)

    async def fake_sleep(_d):  # no real waiting; backoff values don't matter here
        return None

    monkeypatch.setattr(tc.asyncio, "sleep", fake_sleep)

    # Terminates on its own via the give-up return (no exception).
    asyncio.run(tc._proc_watchdog(holder, poll_interval=0, max_retries=3))

    # Relaunched exactly max_retries times, then stopped.
    assert len(spawned) == 3


def test_proc_watchdog_healthy_tick_resets_failure_streak(monkeypatch):
    """A proxy that recovers (a healthy tick) clears the crash streak: each
    relaunch lives one tick then exits, so the watchdog keeps recovering it well
    past max_retries instead of giving up — only *consecutive* failures count."""

    class _FlakyProc:
        """poll() reports running for `alive` ticks, then exits with code 1."""
        def __init__(self, alive):
            self._alive = alive
            self.returncode = None

        def poll(self):
            if self._alive > 0:
                self._alive -= 1
                return None
            self.returncode = 1
            return 1

    holder = {"proc": _FlakyProc(alive=0), "cmd": ["c"], "env": None, "label": "fs"}
    spawned = []
    calls = {"n": 0}

    def fake_popen(cmd, env=None, **kwargs):
        spawned.append(cmd)
        return _FlakyProc(alive=1)  # each relaunch runs one healthy tick, then dies

    monkeypatch.setattr(tc.subprocess, "Popen", fake_popen)

    async def fake_sleep(_d):
        calls["n"] += 1
        if calls["n"] >= 10:  # plenty of ticks to exceed the cap via resets
            raise _StopLoop()

    monkeypatch.setattr(tc.asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(tc._proc_watchdog(holder, poll_interval=0, max_retries=3))
    except _StopLoop:
        pass

    # Recovered repeatedly — relaunch count exceeds max_retries, so it never gave up.
    assert len(spawned) > 3


# ---------------------------------------------------------------------------
# set_active_repo control message — pool.default_repo_path mutation
# ---------------------------------------------------------------------------

def test_extract_pool_set_active_repo_updates_default():
    """set_active_repo control message updates pool.default_repo_path in-place."""
    from meridian.serena_pool import SerenaDaemonPool
    pool = SerenaDaemonPool(default_repo_path="/original/repo")

    # Replicate the exact logic inside _run_extract_pool_connection.
    msg = {"type": "set_active_repo", "repo_path": "/new/repo"}
    new_path = str(msg.get("repo_path") or "").strip()
    if new_path:
        pool.default_repo_path = pool._normalize(new_path)

    assert pool.default_repo_path == pool._normalize("/new/repo")


def test_extract_pool_set_active_repo_blank_is_noop():
    """set_active_repo with blank repo_path leaves default_repo_path unchanged."""
    from meridian.serena_pool import SerenaDaemonPool
    pool = SerenaDaemonPool(default_repo_path="/original/repo")
    original = pool.default_repo_path

    msg = {"type": "set_active_repo", "repo_path": "   "}
    new_path = str(msg.get("repo_path") or "").strip()
    if new_path:
        pool.default_repo_path = pool._normalize(new_path)

    assert pool.default_repo_path == original


# ---------------------------------------------------------------------------
# Script-mode entrypoint — `python meridian/__main__.py` (regression for 9ec44f0)
# ---------------------------------------------------------------------------

def test_tunnel_script_mode_import():
    """`python meridian/__main__.py --help` must run in script mode (no package
    context) without tripping the relative-import guard.

    Regression for 9ec44f0: before the __package__ shim in __main__.py, a
    script-mode invocation (`python meridian/__main__.py --tunnel`, or the
    installer's `python meridian ...`) raised "attempted relative import with no
    known parent package" the moment it hit `from .tunnel_client import ...`.
    --help is the hermetic smoke test: it exercises module import + the shim
    without needing Node/npx or the network.
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    main_py = root / "meridian" / "__main__.py"
    proc = subprocess.run(
        [sys.executable, str(main_py), "--help"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"script-mode --help exited {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    # The exact failure 9ec44f0 fixed, and any adjacent import breakage.
    assert "attempted relative import" not in proc.stderr, proc.stderr
    assert "ImportError" not in proc.stderr, proc.stderr
    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr
    # argparse writes usage to stdout on --help.
    assert "usage" in proc.stdout.lower()


# ---------------------------------------------------------------------------
# Version check — _version_tuple / _update_notice (4bde9437)
# ---------------------------------------------------------------------------

def test_version_tuple_parses_dotted():
    assert tc._version_tuple("0.1.6") == (0, 1, 6)
    assert tc._version_tuple("1.2.3") == (1, 2, 3)
    assert tc._version_tuple("2.0") == (2, 0)


def test_version_tuple_truncates_at_nonnumeric_segment():
    # PEP 440 pre-release suffixes: leading digits of a segment count, the rest
    # is ignored; a fully non-numeric segment stops parsing.
    assert tc._version_tuple("0.2.0rc1") == (0, 2, 0)
    assert tc._version_tuple("1.4.0.dev1") == (1, 4, 0)
    assert tc._version_tuple("1.2.beta") == (1, 2)


def test_version_tuple_unparseable_is_empty():
    assert tc._version_tuple("") == ()
    assert tc._version_tuple("nightly") == ()
    assert tc._version_tuple("   ") == ()


def test_update_notice_when_server_newer():
    notice = tc._update_notice("0.1.6", "0.1.7")
    assert notice is not None
    assert "0.1.7" in notice and "0.1.6" in notice
    assert "upgrade" in notice.lower()


def test_update_notice_none_when_equal_or_ahead():
    assert tc._update_notice("0.1.6", "0.1.6") is None
    assert tc._update_notice("0.2.0", "0.1.9") is None  # local ahead of server


def test_update_notice_none_on_missing_or_bad_version():
    # Fail-open: any missing/unparseable version yields no nag, never raises.
    assert tc._update_notice("0.1.6", "") is None
    assert tc._update_notice("", "0.1.7") is None
    assert tc._update_notice("0.1.6", "unknown") is None
    assert tc._update_notice("0.1.6", None) is None  # type: ignore[arg-type]


def test_update_notice_minor_and_major_bumps():
    assert tc._update_notice("0.1.6", "0.2.0") is not None   # minor
    assert tc._update_notice("0.9.9", "1.0.0") is not None   # major
    assert tc._update_notice("1.0.0", "1.0.1") is not None   # patch
