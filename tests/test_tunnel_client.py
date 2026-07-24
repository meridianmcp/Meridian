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
import os
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
# _extract_jsonrpc_error — surface silent index failures (f193bbd1)
# ---------------------------------------------------------------------------

def test_extract_jsonrpc_error_clean_response_returns_none():
    """A normal 200 tools/call response with no error signals returns None."""
    body = b'{"jsonrpc":"2.0","id":"idx","result":{"content":[{"type":"text","text":"ok"}]}}'
    assert tc._extract_jsonrpc_error(body) is None


def test_extract_jsonrpc_error_jsonrpc_error_object():
    """A JSON-RPC error object is detected and its message returned."""
    body = b'{"jsonrpc":"2.0","id":"idx","error":{"code":-32603,"message":"internal error"}}'
    result = tc._extract_jsonrpc_error(body)
    assert result is not None
    assert "internal error" in result


def test_extract_jsonrpc_error_jsonrpc_error_string():
    """A JSON-RPC error as a plain string is detected."""
    body = b'{"jsonrpc":"2.0","id":"idx","error":"index failed"}'
    result = tc._extract_jsonrpc_error(body)
    assert result is not None
    assert "index failed" in result


def test_extract_jsonrpc_error_mcp_result_is_error():
    """MCP result.isError=true with content text is surfaced."""
    body = (
        b'{"jsonrpc":"2.0","id":"idx","result":{"isError":true,'
        b'"content":[{"type":"text","text":"path not found"}]}}'
    )
    result = tc._extract_jsonrpc_error(body)
    assert result is not None
    assert "path not found" in result


def test_extract_jsonrpc_error_mcp_content_item_is_error():
    """MCP result.content[*].isError=true is surfaced (2025-03-26 convention)."""
    body = (
        b'{"jsonrpc":"2.0","id":"idx","result":{"content":['
        b'{"type":"text","isError":true,"text":"scan aborted"}]}}'
    )
    result = tc._extract_jsonrpc_error(body)
    assert result is not None
    assert "scan aborted" in result


def test_extract_jsonrpc_error_non_json_body_returns_none():
    """A non-JSON body (plain text, SSE) is silently ignored."""
    assert tc._extract_jsonrpc_error(b"event: message\ndata: {bad json}\n\n") is None


def test_extract_jsonrpc_error_empty_body_returns_none():
    """Empty / None body returns None without raising."""
    assert tc._extract_jsonrpc_error(b"") is None
    assert tc._extract_jsonrpc_error(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _extract_jsonrpc_error — application-level status/worker failure (cd28b329)
#
# codebase-memory-mcp's index_repository can report a server-side failure
# (status=error, per-worker worker_failed/exit_code=1) WITHOUT setting the
# MCP isError convention anywhere in the envelope. Before this fix that shape
# passed straight through _extract_jsonrpc_error as a clean response, so
# _index_code_dir printed "code-intel: indexed" despite the failure.
# ---------------------------------------------------------------------------

def test_extract_jsonrpc_error_status_error_on_result():
    """A bare result.status == "error" (no isError anywhere) is detected."""
    body = b'{"jsonrpc":"2.0","id":"idx","result":{"status":"error"}}'
    result = tc._extract_jsonrpc_error(body)
    assert result is not None
    assert "status=error" in result


def test_extract_jsonrpc_error_worker_failed_exit_code_1_twice():
    """The exact reported shape: status=error + 2x worker_failed, exit_code=1."""
    body = (
        b'{"jsonrpc":"2.0","id":"idx","result":{"status":"error","workers":['
        b'{"status":"worker_failed","exit_code":1},'
        b'{"status":"worker_failed","exit_code":1}'
        b']}}'
    )
    result = tc._extract_jsonrpc_error(body)
    assert result is not None
    assert "status=error" in result
    assert "2 worker(s) failed" in result
    assert "exit_code=1" in result


def test_extract_jsonrpc_error_worker_failed_without_top_level_status():
    """Worker failures alone (no top-level status field) are still detected."""
    body = (
        b'{"jsonrpc":"2.0","id":"idx","result":{"workers":['
        b'{"status":"worker_failed","exit_code":1}]}}'
    )
    result = tc._extract_jsonrpc_error(body)
    assert result is not None
    assert "1 worker(s) failed" in result


def test_extract_jsonrpc_error_nonzero_exit_code_without_status_string():
    """A worker with a nonzero exit_code but no recognized status string still counts."""
    body = b'{"jsonrpc":"2.0","id":"idx","result":{"workers":[{"exit_code":1}]}}'
    result = tc._extract_jsonrpc_error(body)
    assert result is not None
    assert "1 worker(s) failed" in result


def test_extract_jsonrpc_error_status_nested_in_content_text():
    """The status/worker blob nested inside result.content[*].text JSON is detected."""
    inner = '{"status":"error","workers":[{"status":"worker_failed","exit_code":1}]}'
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": "idx",
        "result": {"content": [{"type": "text", "text": inner}]},
    }).encode()
    result = tc._extract_jsonrpc_error(body)
    assert result is not None
    assert "status=error" in result
    assert "1 worker(s) failed" in result


def test_extract_jsonrpc_error_healthy_status_not_flagged():
    """A clean status value (e.g. "ok") must not be misreported as a failure."""
    body = b'{"jsonrpc":"2.0","id":"idx","result":{"status":"ok","workers":[{"status":"ok","exit_code":0}]}}'
    assert tc._extract_jsonrpc_error(body) is None


def test_index_code_dir_reports_worker_failed_status_error_as_failure(monkeypatch, capsys):
    """End-to-end: index_repository's HTTP-200 status=error/worker_failed body must
    surface as a warning, not a silent 'code-intel: indexed' success (cd28b329).
    """
    import httpx as _httpx

    _call_count = [0]

    class FakeResp:
        def __init__(self, body: bytes):
            self.status_code = 200
            self.content = body

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, **kw):
            _call_count[0] += 1
            if _call_count[0] == 1:
                return FakeResp(b'{"jsonrpc":"2.0","id":"probe","result":{"tools":[]}}')
            return FakeResp(
                b'{"jsonrpc":"2.0","id":"idx","result":{"status":"error","workers":['
                b'{"status":"worker_failed","exit_code":1},'
                b'{"status":"worker_failed","exit_code":1}'
                b']}}'
            )

    monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)
    asyncio.run(tc._index_code_dir(8809, "/repo"))

    captured = capsys.readouterr()
    assert "status=error" in captured.err
    assert "worker(s) failed" in captured.err
    assert "code-intel: indexed" not in captured.out


def test_index_code_dir_logs_jsonrpc_error_body(monkeypatch, capsys):
    """_index_code_dir warns when index_repository returns HTTP 200 with a JSON-RPC error body.

    This is the silent-failure gap: the old code printed 'indexed' on any HTTP 200,
    even when the response body carried a JSON-RPC error that means the index is
    incomplete. The fix surfaces these in the tunnel log.
    """
    import httpx as _httpx

    # First call (probe tools/list) returns 200 clean; second (tools/call) returns
    # 200 but with a JSON-RPC error body — the silent-failure case.
    _call_count = [0]

    class FakeResp:
        def __init__(self, body: bytes):
            self.status_code = 200
            self.content = body

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, **kw):
            _call_count[0] += 1
            if _call_count[0] == 1:
                # Probe: clean 200
                return FakeResp(b'{"jsonrpc":"2.0","id":"probe","result":{"tools":[]}}')
            # index_repository call: HTTP 200 with JSON-RPC error body
            return FakeResp(
                b'{"jsonrpc":"2.0","id":"idx","error":{"code":-32603,"message":"scan partial"}}'
            )

    monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)
    asyncio.run(tc._index_code_dir(8809, "/repo"))

    captured = capsys.readouterr()
    # Must warn on stderr, not silently print "indexed"
    assert "scan partial" in captured.err
    assert "indexed" not in captured.out


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
# bf31787c — meridian server entry injection
# ---------------------------------------------------------------------------

def test_meridian_server_url():
    # The main Meridian server URL is just {base_url}/mcp (tenant-agnostic).
    assert tc._meridian_server_url("https://usemeridian.us") == "https://usemeridian.us/mcp"
    assert tc._meridian_server_url("https://usemeridian.us/") == "https://usemeridian.us/mcp"


def test_tunnel_mcp_entries_includes_meridian_when_token_provided():
    # bf31787c — when a token is supplied, the 'meridian' server entry is
    # injected as the first entry with the correct URL and Authorization header.
    base, tid = "https://usemeridian.us", "tid-123"
    tok = "sk_meridian_test"
    entries = tc._tunnel_mcp_entries(base, tid, token=tok)
    assert "meridian" in entries
    assert entries["meridian"]["type"] == "http"
    assert entries["meridian"]["url"] == "https://usemeridian.us/mcp"
    assert entries["meridian"]["headers"] == {"Authorization": f"Bearer {tok}"}
    # Slot connectors still present.
    assert "filesystem" in entries
    assert "codebase-memory" in entries
    assert "serena" in entries


def test_tunnel_mcp_entries_omits_meridian_without_token():
    # bf31787c — without a token (or empty token) no 'meridian' entry is injected,
    # preserving backward-compat for callers that don't have the token handy.
    base, tid = "https://usemeridian.us", "tid-123"
    assert "meridian" not in tc._tunnel_mcp_entries(base, tid)
    assert "meridian" not in tc._tunnel_mcp_entries(base, tid, token=None)
    assert "meridian" not in tc._tunnel_mcp_entries(base, tid, token="")


def test_install_mcp_json_injects_meridian_server_entry(tmp_path):
    # bf31787c — with a token, _install_mcp_json writes the 'meridian' entry.
    base, tid, tok = "https://usemeridian.us", "tid-x", "sk_meridian_tok"
    tc._install_mcp_json(tmp_path, base, tid, token=tok)
    servers = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert "meridian" in servers
    assert servers["meridian"]["url"] == f"{base}/mcp"
    assert servers["meridian"]["headers"]["Authorization"] == f"Bearer {tok}"
    # Slot connectors still present.
    assert "filesystem" in servers
    assert "codebase-memory" in servers
    assert "serena" in servers


def test_install_mcp_json_without_token_omits_meridian_entry(tmp_path):
    # bf31787c — without a token the 'meridian' entry is absent (not a regression
    # for callers that omit the token parameter).
    tc._install_mcp_json(tmp_path, "https://usemeridian.us", "tid-x")
    servers = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert "meridian" not in servers
    assert set(servers) == {"filesystem", "codebase-memory", "serena"}


def test_is_our_mcp_entry_recognises_meridian_server_url():
    # bf31787c — an entry whose url is {base_url}/mcp is recognised as ours
    # so restore and re-inject don't accumulate stale entries.
    base, tid = "https://usemeridian.us", "tid-123"
    entry = {"type": "http", "url": f"{base}/mcp", "headers": {"Authorization": "Bearer tok"}}
    assert tc._is_our_mcp_entry("meridian", entry, base, tid)
    # A user's own server called 'meridian' pointing elsewhere must NOT be flagged.
    other = {"type": "http", "url": "https://example.com/mcp"}
    assert not tc._is_our_mcp_entry("meridian", other, base, tid)


def test_inject_mcp_entries_meridian_reinject_is_idempotent(tmp_path):
    # bf31787c — a second tunnel run should overwrite (not duplicate) the meridian
    # entry, and should NOT create 'meridian-meridian' due to collision handling.
    base, tid, tok = "https://usemeridian.us", "tid-y", "sk_meridian_tok"
    entries = tc._tunnel_mcp_entries(base, tid, token=tok)
    first = tc._inject_mcp_entries(None, entries, base, tid)
    second = tc._inject_mcp_entries(first, entries, base, tid)
    servers = json.loads(second)["mcpServers"]
    # Exactly one 'meridian' key, no 'meridian-meridian'.
    assert "meridian" in servers
    assert "meridian-meridian" not in servers
    assert servers["meridian"]["url"] == f"{base}/mcp"


def test_restore_mcp_json_removes_meridian_server_entry(tmp_path):
    # bf31787c — restore must strip the injected 'meridian' server entry so it
    # doesn't linger after the tunnel stops.
    base, tid, tok = "https://usemeridian.us", "tid-z", "sk_meridian_tok"
    mcp = tmp_path / ".mcp.json"
    snaps = tc._install_mcp_json(tmp_path, base, tid, token=tok)
    assert "meridian" in json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"]

    tc._restore_mcp_json(snaps, base, tid)
    # File was created (original=None) so it is deleted on restore.
    assert not mcp.exists()


def test_restore_mcp_json_strips_meridian_entry_from_existing_file(tmp_path):
    # bf31787c — if the file already existed and we merged in the meridian entry,
    # restore must strip it and leave any user-owned entries intact.
    base, tid, tok = "https://usemeridian.us", "tid-w", "sk_meridian_tok"
    mcp = tmp_path / ".mcp.json"
    original_data = {"mcpServers": {"my-server": {"command": "node"}}}
    mcp.write_text(json.dumps(original_data), encoding="utf-8")

    snaps = tc._install_mcp_json(tmp_path, base, tid, token=tok)
    # During the session both 'meridian' and 'my-server' are present.
    live = json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"]
    assert "meridian" in live
    assert "my-server" in live

    tc._restore_mcp_json(snaps, base, tid)
    restored = json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"]
    assert "meridian" not in restored
    assert "filesystem" not in restored
    assert restored == {"my-server": {"command": "node"}}


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


def test_build_proxy_for_inner_resolves_bare_npx_inner_command(monkeypatch):
    """0675d588 — a literal ``"npx"`` inner token (e.g. the debug slot's static
    default) must be resolved via _find_npx, not passed through bare: on
    Windows a bare npx is the extension-less shim, and since it doesn't end in
    .cmd/.bat the --shell mitigation never triggers, so mcp-proxy's no-shell
    spawn of it raises ENOENT."""
    monkeypatch.setattr(tc.sys, "platform", "win32")
    monkeypatch.setattr(tc, "_find_npx", lambda: r"C:\npm\npx.cmd")
    cmd = tc._build_proxy_for_inner("npx.cmd", ["npx", "-y", "@debugmcp/mcp-debugger"], 8821)
    inner = cmd[cmd.index("--") + 1:]
    assert inner == [r"C:\npm\npx.cmd", "-y", "@debugmcp/mcp-debugger"]
    # Resolved to a .cmd shim -> the existing Windows shell mitigation fires.
    assert "--shell" in cmd


def test_build_proxy_for_inner_makes_resolved_npx_shell_safe(monkeypatch):
    """9130fc7d -- _find_npx() can resolve to a real, space-containing path
    (e.g. ``C:\\Program Files\\nodejs\\npx.cmd``). Under mcp-proxy's --shell
    (cmd.exe) invocation an unescaped space truncates the command ("'C:\\Program'
    is not recognized"). _build_proxy_for_inner must run the resolved path
    through _win_shell_safe_path (already used elsewhere for this exact class
    of bug) before using it, not just _find_npx() alone."""
    monkeypatch.setattr(tc.sys, "platform", "win32")
    monkeypatch.setattr(tc, "_find_npx", lambda: r"C:\Program Files\nodejs\npx.cmd")
    monkeypatch.setattr(tc, "_win_shell_safe_path", lambda p: r"C:\PROGRA~1\nodejs\npx.cmd")
    cmd = tc._build_proxy_for_inner("npx.cmd", ["npx", "-y", "@debugmcp/mcp-debugger"], 8821)
    inner = cmd[cmd.index("--") + 1:]
    assert inner[0] == r"C:\PROGRA~1\nodejs\npx.cmd"
    assert " " not in inner[0]


def test_debug_default_command_is_bare_npx_not_cmd_wrapped(monkeypatch):
    """9130fc7d -- unlike the (historical) _dc_default_command cmd/c wrap,
    _debug_default_command must stay a bare npx invocation on every platform:
    _build_proxy_for_inner's shared npx.cmd resolution + --shell already
    handles Windows safely (one process layer), and a second cmd/c wrapper
    here would reintroduce the extra-nested-cmd.exe readiness-probe failure
    the dc cmd/c wrap was found to cause live."""
    monkeypatch.setattr(tc.sys, "platform", "win32")
    assert tc._debug_default_command() == ["npx", "-y", "@debugmcp/mcp-debugger"]
    monkeypatch.setattr(tc.sys, "platform", "linux")
    assert tc._debug_default_command() == ["npx", "-y", "@debugmcp/mcp-debugger"]


def test_office_slot_command_falls_back_to_debug_default(monkeypatch):
    """9130fc7d -- mirrors dc's existing fallback: when the debug slot's
    plugin dict has no explicit command, _office_slot_command must fall back
    to _debug_default_command() rather than returning None."""
    assert tc._office_slot_command("debug", {}) == ["npx", "-y", "@debugmcp/mcp-debugger"]
    assert tc._office_slot_command("debug", None) == ["npx", "-y", "@debugmcp/mcp-debugger"]


def test_build_proxy_for_inner_leaves_non_npx_inner_command_untouched(monkeypatch):
    monkeypatch.setattr(tc, "_find_npx", lambda: r"C:\npm\npx.cmd")
    cmd = tc._build_proxy_for_inner("npx", ["codegraph", "--stdio"], 8809)
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
    standard ~23s budget on a cold cache ('tunnel:ppt: pre-flight ... FAILED').
    105e56b9 — docs/zotero also added to the cold-fetch set (uvx --from <local-path>
    / uvx zotero-mcp can take 30-150s on a cold cache, same root cause)."""
    # All cold-fetch slots are declared; the original dc/ppt/word trio plus docs/zotero.
    assert frozenset({"dc", "ppt", "word"}).issubset(tc._COLD_FETCH_SLOTS)

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


# ---------------------------------------------------------------------------
# aaddb273 — per-client session-to-process liveness: slot-claim helpers and
# the live-client guard in _kill_stale_port_occupant.
# ---------------------------------------------------------------------------


def test_write_and_clear_slot_claim_roundtrip(tmp_path, monkeypatch):
    """_write_slot_claim writes a readable JSON claim; _clear_slot_claim removes it."""
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    tc._write_slot_claim(9001, "client-abc")
    claim_path = tc._slot_claim_path(9001)
    assert claim_path.exists()
    data = json.loads(claim_path.read_text())
    assert data["client_id"] == "client-abc"
    assert data["tunnel_pid"] == os.getpid()
    tc._clear_slot_claim(9001)
    assert not claim_path.exists()


def test_is_slot_claimed_by_live_client_live_different_client(tmp_path, monkeypatch):
    """Returns True when the claim names a DIFFERENT client whose PID is still alive."""
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    # Write a claim from a "different" client — use os.getpid() as the tunnel_pid
    # (it is definitely alive) but a different client_id. This uses the REAL
    # psutil (not yet patched below), so the claim carries a genuine create_time.
    tc._write_slot_claim(9002, "other-client-xyz")
    real_create_time = json.loads(tc._slot_claim_path(9002).read_text()).get("create_time")

    import types
    fake_psutil = types.ModuleType("psutil")
    fake_psutil.pid_exists = lambda pid: True  # the other tunnel is still alive

    class _FakeProc:
        """PID-reuse hardening (2026-07-17) checks create_time(), not just
        pid_exists() -- the fake psutil module must provide a Process() that
        returns the exact create_time the claim was written with, so the
        comparison in _is_slot_claimed_by_live_client genuinely matches."""

        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return real_create_time

    fake_psutil.Process = _FakeProc
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)

    assert tc._is_slot_claimed_by_live_client(9002, "my-client-abc") is True


def test_is_slot_claimed_by_live_client_dead_tunnel_pid(tmp_path, monkeypatch):
    """Returns False when the claiming tunnel PID is no longer alive (orphan)."""
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    tc._write_slot_claim(9003, "other-client-xyz")

    import types
    fake_psutil = types.ModuleType("psutil")
    fake_psutil.pid_exists = lambda pid: False  # the other tunnel is DEAD
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)

    assert tc._is_slot_claimed_by_live_client(9003, "my-client-abc") is False


def test_is_slot_claimed_by_live_client_same_client_id(tmp_path, monkeypatch):
    """Returns False when the claim belongs to THIS run (same client_id)."""
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    tc._write_slot_claim(9004, "same-client")

    import types
    fake_psutil = types.ModuleType("psutil")
    fake_psutil.pid_exists = lambda pid: True
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)

    # Same client_id — treat as orphan from a prior incomplete teardown.
    assert tc._is_slot_claimed_by_live_client(9004, "same-client") is False


def test_is_slot_claimed_by_live_client_no_claim_file(tmp_path, monkeypatch):
    """Returns False when there is no claim file (pre-feature orphan)."""
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    # No claim written — missing file must return False.
    assert tc._is_slot_claimed_by_live_client(9005, "my-client") is False


def test_record_spawned_pid_writes_registry_entry(tmp_path, monkeypatch):
    """_record_spawned_pid writes a JSON list entry with pid/label/owner fields."""
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))

    class _FakeProc3:
        pid = 4242

    tc._record_spawned_pid(_FakeProc3(), "docs")

    entries = json.loads(tc._all_spawned_registry_path().read_text())
    assert len(entries) == 1
    assert entries[0]["pid"] == 4242
    assert entries[0]["label"] == "docs"
    assert entries[0]["owner_tunnel_pid"] == os.getpid()


def test_record_spawned_pid_appends_to_existing_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))

    class _FakeProc3:
        def __init__(self, pid):
            self.pid = pid

    tc._record_spawned_pid(_FakeProc3(1), "docs")
    tc._record_spawned_pid(_FakeProc3(2), "outputs")

    entries = json.loads(tc._all_spawned_registry_path().read_text())
    assert [e["pid"] for e in entries] == [1, 2]


def test_record_spawned_pid_survives_object_with_no_pid_attribute(tmp_path, monkeypatch):
    """6884a668: a Popen-like object missing .pid must not crash the spawn path."""
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))

    class _NoPid:
        pass

    tc._record_spawned_pid(_NoPid(), "docs")  # must not raise
    assert not tc._all_spawned_registry_path().exists()


def test_kill_all_previously_spawned_pids_kills_genuine_orphan(tmp_path, monkeypatch):
    """A recorded child whose OWNER tunnel is dead is a genuine orphan -- killed."""
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(tc.sys, "platform", "linux")

    registry = tc._all_spawned_registry_path()
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps([
        {"pid": 555, "label": "docs", "create_time": 1000.0,
         "owner_tunnel_pid": 111, "owner_tunnel_create_time": 2000.0},
    ]))

    import types
    fake_psutil = types.ModuleType("psutil")
    killed = {}

    class _FakeProc:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            if self.pid == 555:
                return 1000.0  # matches -- genuine child, not PID-reused
            raise Exception("owner tunnel pid no longer exists")  # 111 is dead

        def terminate(self):
            killed.setdefault("terminate", []).append(self.pid)

        def wait(self, timeout=None):
            pass

    fake_psutil.Process = _FakeProc
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)

    tc._kill_all_previously_spawned_pids()

    assert killed.get("terminate") == [555]
    # Registry cleared after the sweep.
    assert json.loads(registry.read_text()) == []


def test_kill_all_previously_spawned_pids_spares_live_owner_tunnel(tmp_path, monkeypatch):
    """A recorded child whose OWNER tunnel is STILL alive is left alone --
    it belongs to a different, currently-running tunnel invocation."""
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(tc.sys, "platform", "linux")

    registry = tc._all_spawned_registry_path()
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps([
        {"pid": 555, "label": "docs", "create_time": 1000.0,
         "owner_tunnel_pid": 111, "owner_tunnel_create_time": 2000.0},
    ]))

    import types
    fake_psutil = types.ModuleType("psutil")
    killed = {}

    class _FakeProc:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            # Both the child and its owner tunnel are still alive and match.
            return 1000.0 if self.pid == 555 else 2000.0

        def terminate(self):
            killed.setdefault("terminate", []).append(self.pid)

    fake_psutil.Process = _FakeProc
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)

    tc._kill_all_previously_spawned_pids()

    assert killed == {}, "child owned by a still-live different tunnel must not be killed"
    # Registry still cleared -- this generation starts recording from empty.
    assert json.loads(registry.read_text()) == []


def test_kill_all_previously_spawned_pids_skips_pid_reused_entry(tmp_path, monkeypatch):
    """If the recorded create_time no longer matches, the PID was reused by an
    unrelated process -- must not be killed."""
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(tc.sys, "platform", "linux")

    registry = tc._all_spawned_registry_path()
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps([
        {"pid": 555, "label": "docs", "create_time": 1000.0,
         "owner_tunnel_pid": 111, "owner_tunnel_create_time": 2000.0},
    ]))

    import types
    fake_psutil = types.ModuleType("psutil")
    killed = {}

    class _FakeProc:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return 9999.0  # mismatch for every pid -- reused

        def terminate(self):
            killed.setdefault("terminate", []).append(self.pid)

    fake_psutil.Process = _FakeProc
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)

    tc._kill_all_previously_spawned_pids()

    assert killed == {}


def test_kill_all_previously_spawned_pids_survives_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    tc._kill_all_previously_spawned_pids()  # must not raise


def test_kill_all_previously_spawned_pids_clears_registry_even_without_psutil(tmp_path, monkeypatch):
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    registry = tc._all_spawned_registry_path()
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps([{"pid": 1, "label": "x"}]))

    import builtins
    real_import = builtins.__import__

    def _fail_psutil_import(name, *a, **kw):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fail_psutil_import)

    tc._kill_all_previously_spawned_pids()  # must not raise
    assert json.loads(registry.read_text()) == []


def test_spawn_with_cache_retry_records_pid_on_success(tmp_path, monkeypatch):
    """Integration: a successful _spawn_with_cache_retry call records the
    child in the all-spawned registry (6884a668)."""
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(tc.time, "sleep", lambda n: None)

    class _FakeProc4:
        pid = 7777
        returncode = None

        def poll(self):
            return None  # still running -- no TAR-error path

    monkeypatch.setattr(tc.subprocess, "Popen", lambda cmd, env=None, **kw: _FakeProc4())

    result = tc._spawn_with_cache_retry(["npx", "-y", "some-tool"], None, "docs")

    assert result.pid == 7777
    entries = json.loads(tc._all_spawned_registry_path().read_text())
    assert any(e["pid"] == 7777 and e["label"] == "docs" for e in entries)


def test_kill_stale_port_occupant_spares_live_client_process(tmp_path, monkeypatch):
    """aaddb273 scenario 1: a live second client's process is NOT killed.

    A port is held by a process that belongs to a DIFFERENT, still-running
    tunnel client.  Even though the port is occupied and our launcher handle is
    None, the live-client guard must prevent the kill.
    """
    monkeypatch.setattr(tc.sys, "platform", "linux")
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))

    # Write a claim for the OTHER client (different client_id, PID still alive).
    # Uses the REAL psutil (not yet patched below), so the claim carries a
    # genuine create_time that the fake Process.create_time() below must match.
    tc._write_slot_claim(8809, "live-other-client")
    real_create_time = json.loads(tc._slot_claim_path(8809).read_text()).get("create_time")

    import types
    fake_psutil = types.ModuleType("psutil")
    fake_psutil.CONN_LISTEN = "LISTEN"
    fake_psutil.net_connections = lambda kind="inet": [_FakeConn(port=8809, pid=888)]
    fake_psutil.pid_exists = lambda pid: True   # the other tunnel is still alive

    killed = {}

    class _FakeProc:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return real_create_time

        def terminate(self):
            killed.setdefault("terminate", []).append(self.pid)

        def wait(self, timeout=None):
            pass

    fake_psutil.Process = _FakeProc
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)

    tc._kill_stale_port_occupant(8809, "fs", current_client_id="my-new-client")

    # The live-client guard fired — process must NOT have been killed.
    assert killed == {}


def test_kill_stale_port_occupant_kills_genuinely_orphaned_process(tmp_path, monkeypatch):
    """aaddb273 scenario 2: a genuinely orphaned process IS killed as 963d0bd does.

    A port is occupied by a process from a PRIOR tunnel generation whose PID
    is no longer alive.  The live-client check returns False (dead tunnel) so
    the kill proceeds exactly as before.
    """
    monkeypatch.setattr(tc.sys, "platform", "linux")
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))

    # Write a claim for a prior client whose tunnel process is now dead.
    tc._write_slot_claim(8810, "prior-dead-client")

    import types
    fake_psutil = types.ModuleType("psutil")
    fake_psutil.CONN_LISTEN = "LISTEN"
    fake_psutil.net_connections = lambda kind="inet": [_FakeConn(port=8810, pid=777)]
    fake_psutil.pid_exists = lambda pid: False  # dead tunnel

    killed = {}

    class _FakeProc:
        def __init__(self, pid):
            self.pid = pid

        def terminate(self):
            killed.setdefault("terminate", []).append(self.pid)

        def wait(self, timeout=None):
            pass

    fake_psutil.Process = _FakeProc
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)

    tc._kill_stale_port_occupant(8810, "code", current_client_id="my-new-client")

    # Dead tunnel == orphan; must have been killed.
    assert killed.get("terminate") == [777]


def test_slot_proxy_client_id_stored_on_init():
    """SlotProxy stores the passed client_id."""
    proxy = tc.SlotProxy(["cmd"], 9100, "test", client_id="abc-123")
    assert proxy.client_id == "abc-123"


def test_slot_proxy_client_id_defaults_to_empty():
    """SlotProxy client_id defaults to empty string when not provided."""
    proxy = tc.SlotProxy(["cmd"], 9101, "test")
    assert proxy.client_id == ""


def test_dc_default_command_wraps_cmd_on_windows(monkeypatch):
    # 83bd7f21 -- no longer cmd/c-wrapped; bare npx lets _build_proxy_for_inner's
    # shared npx.cmd resolution + --shell handle it (one process layer, not two).
    monkeypatch.setattr(tc.sys, "platform", "win32")
    assert tc._dc_default_command() == [
        "npx", "-y",
        f"@wonderwhy-er/desktop-commander@{tc._DC_PINNED_VERSION}",
    ]


def test_dc_default_command_bare_npx_on_posix(monkeypatch):
    # 3db4f8d8 — pinned to _DC_PINNED_VERSION, not @latest.
    monkeypatch.setattr(tc.sys, "platform", "linux")
    assert tc._dc_default_command() == [
        "npx", "-y", f"@wonderwhy-er/desktop-commander@{tc._DC_PINNED_VERSION}",
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


def test_proc_watchdog_cools_down_instead_of_giving_up(monkeypatch):
    """b9e4967d (root-caused live 2026-07-18): a proxy that keeps exiting (e.g.
    ENOENT) is relaunched at the fast cadence for at most ``max_retries``
    attempts, but the watchdog must NOT exit permanently after that — a bare
    ``return`` there used to end the watchdog task forever, so a slot's local
    proxy stayed dead even after the underlying bug was fixed out-of-band,
    while the WebSocket layer kept reporting tunnel_active: true. Instead the
    watchdog keeps retrying at the long ``cooldown`` cadence, so it can still
    self-heal."""
    holder = {"proc": _DeadProc(), "cmd": ["broken-cmd"], "env": None, "label": "extract"}
    spawned = []

    # Every relaunch yields another already-dead proc — the slot never recovers
    # on its own, but the watchdog should keep trying rather than give up.
    def fake_popen(cmd, env=None, **kwargs):
        spawned.append(cmd)
        return _DeadProc()

    monkeypatch.setattr(tc.subprocess, "Popen", fake_popen)

    sleeps = []
    calls = {"n": 0}

    async def fake_sleep(d):
        sleeps.append(d)
        calls["n"] += 1
        # Let it run well past max_retries (3) so we observe the cooldown phase.
        if calls["n"] >= 6:
            raise _StopLoop()

    monkeypatch.setattr(tc.asyncio, "sleep", fake_sleep)

    try:
        asyncio.run(
            tc._proc_watchdog(holder, poll_interval=0, max_retries=3, cooldown=999)
        )
    except _StopLoop:
        pass

    # It kept relaunching past max_retries instead of giving up for good.
    assert len(spawned) > 3
    # Once fast retries are exhausted, the sleep duration switches to the long
    # cooldown value (not the small exponential fast-retry backoff).
    assert sleeps[-1] == 999


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


# ---------------------------------------------------------------------------
# 105b5aa9 — ensure_running pre-spawn port-occupancy check
# ---------------------------------------------------------------------------

class _FakeProc:
    """Minimal Popen stand-in for ensure_running tests."""
    def __init__(self):
        self.pid = 12345
    def poll(self):
        return None  # alive


async def _run_ensure(proxy):
    """Helper: run ensure_running in a fresh event loop."""
    await proxy.ensure_running()


def _run_sync(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_ensure_running_spawns_when_port_is_free(monkeypatch):
    """105b5aa9 — when the port is free, ensure_running proceeds to Popen as
    usual (the port-occupancy check path is transparent on a clean port)."""
    spawned = []
    monkeypatch.setattr(tc, "_port_is_open", lambda port, **kw: False)
    monkeypatch.setattr(tc.subprocess, "Popen", lambda cmd, env=None, **kw: spawned.append(cmd) or _FakeProc())
    monkeypatch.setattr(tc, "_write_slot_claim", lambda *a, **kw: None)
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=True))

    proxy = tc.SlotProxy(["mcp-proxy", "--port", "9200"], 9200, "test", client_id="cl-1")
    _run_sync(_run_ensure(proxy))

    assert len(spawned) == 1
    assert proxy.is_running


def test_ensure_running_kills_orphan_then_spawns(monkeypatch):
    """105b5aa9 — when the port is occupied by an orphan (no live-client claim),
    ensure_running calls _kill_stale_port_occupant, then spawns normally."""
    spawned = []
    kill_calls = []
    monkeypatch.setattr(tc, "_port_is_open", lambda port, **kw: True)
    monkeypatch.setattr(tc, "_is_slot_claimed_by_live_client", lambda port, cid: False)
    monkeypatch.setattr(
        tc, "_kill_stale_port_occupant",
        lambda port, label, current_client_id="": kill_calls.append((port, label)),
    )
    monkeypatch.setattr(tc.subprocess, "Popen", lambda cmd, env=None, **kw: spawned.append(cmd) or _FakeProc())
    monkeypatch.setattr(tc, "_write_slot_claim", lambda *a, **kw: None)
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=True))

    proxy = tc.SlotProxy(["mcp-proxy", "--port", "9201"], 9201, "fs", client_id="cl-2")
    _run_sync(_run_ensure(proxy))

    assert kill_calls == [(9201, "fs")]
    assert len(spawned) == 1


def test_ensure_running_refuses_live_client_port(monkeypatch, capsys):
    """105b5aa9 — when the port is held by a DIFFERENT still-live tunnel client,
    ensure_running must NOT kill it and must NOT spawn; it logs an error and
    returns without a Popen call."""
    spawned = []
    kill_calls = []
    monkeypatch.setattr(tc, "_port_is_open", lambda port, **kw: True)
    monkeypatch.setattr(tc, "_is_slot_claimed_by_live_client", lambda port, cid: True)
    monkeypatch.setattr(
        tc, "_kill_stale_port_occupant",
        lambda port, label, current_client_id="": kill_calls.append(port),
    )
    monkeypatch.setattr(tc.subprocess, "Popen", lambda cmd, env=None, **kw: spawned.append(cmd) or _FakeProc())

    proxy = tc.SlotProxy(["mcp-proxy", "--port", "9202"], 9202, "code", client_id="cl-3")
    _run_sync(_run_ensure(proxy))

    assert kill_calls == [], "must NOT kill a live-client-owned port"
    assert spawned == [], "must NOT spawn when port is live-client-owned"
    captured = capsys.readouterr()
    assert "live tunnel client" in captured.err


def test_ensure_running_no_double_spawn_under_concurrent_requests(monkeypatch):
    """105b5aa9 / lock semantics — if two coroutines race to ensure_running on a
    free port, only one Popen is issued (the asyncio.Lock prevents double-spawn)."""
    import asyncio as _asyncio

    spawn_count = [0]

    def fake_popen(cmd, env=None, **kw):
        spawn_count[0] += 1
        return _FakeProc()

    monkeypatch.setattr(tc, "_port_is_open", lambda port, **kw: False)
    monkeypatch.setattr(tc.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tc, "_write_slot_claim", lambda *a, **kw: None)
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=True))

    proxy = tc.SlotProxy(["mcp-proxy", "--port", "9203"], 9203, "test2", client_id="cl-4")

    async def _run_two():
        await _asyncio.gather(proxy.ensure_running(), proxy.ensure_running())

    loop = _asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run_two())
    finally:
        loop.close()

    assert spawn_count[0] == 1, f"expected 1 spawn, got {spawn_count[0]}"


def test_ensure_running_noop_when_already_running(monkeypatch):
    """105b5aa9 / regression — a slot that is_running skips the port check and
    Popen entirely (no spurious kills on a healthy slot)."""
    kill_calls = []
    spawned = []
    monkeypatch.setattr(
        tc, "_kill_stale_port_occupant",
        lambda port, label, current_client_id="": kill_calls.append(port),
    )
    monkeypatch.setattr(tc.subprocess, "Popen", lambda cmd, env=None, **kw: spawned.append(cmd) or _FakeProc())

    proxy = tc.SlotProxy(["mcp-proxy", "--port", "9204"], 9204, "fs2")
    # Seed a fake live proc so is_running is True from the start.
    proxy._proc = _FakeProc()
    proxy.holder["proc"] = proxy._proc

    _run_sync(_run_ensure(proxy))

    assert kill_calls == []
    assert spawned == []


# ---------------------------------------------------------------------------
# 8e10fb80 — reuse_existing: detect-and-reuse an already-running occupant of
# the slot's port instead of blindly spawning a duplicate. Scoped to slots
# constructed with reuse_existing=True (currently just "code" — see the
# SlotProxy docstring for why fs/extract must NOT opt in).
# ---------------------------------------------------------------------------

def test_slot_proxy_reuse_existing_defaults_false():
    """A slot built the old way (no reuse_existing kwarg) keeps the pre-
    8e10fb80 refuse/kill behaviour — reuse is strictly opt-in."""
    proxy = tc.SlotProxy(["cmd"], 9300, "fs")
    assert proxy.reuse_existing is False
    assert proxy._reused is False


def test_ensure_running_reuse_existing_live_healthy_reuses_no_spawn(monkeypatch):
    """8e10fb80 — reuse_existing=True + a live, healthy occupant on the port:
    ensure_running must NOT Popen a new process, must NOT consult the
    claim-file/kill machinery at all, and must leave the slot usable
    (is_running True) afterward."""
    spawned = []
    kill_calls = []
    claim_checks = []
    monkeypatch.setattr(tc, "_port_is_open", lambda port, **kw: True)
    monkeypatch.setattr(
        tc, "_is_slot_claimed_by_live_client",
        lambda port, cid: claim_checks.append(port) or False,
    )
    monkeypatch.setattr(
        tc, "_kill_stale_port_occupant",
        lambda port, label, current_client_id="": kill_calls.append(port),
    )
    monkeypatch.setattr(tc.subprocess, "Popen", lambda cmd, env=None, **kw: spawned.append(cmd) or _FakeProc())
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=True))

    proxy = tc.SlotProxy(
        ["mcp-proxy", "--port", "9210"], 9210, "code",
        client_id="cl-reuse-1", reuse_existing=True,
    )
    _run_sync(_run_ensure(proxy))

    assert spawned == [], "must NOT spawn a duplicate when a healthy occupant already exists"
    assert kill_calls == [], "must NOT kill the reused occupant"
    assert claim_checks == [], "reuse short-circuits before the claim-file check"
    assert proxy._reused is True
    assert proxy.is_running is True


def test_ensure_running_reuse_existing_unhealthy_falls_back_to_orphan_kill(monkeypatch):
    """8e10fb80 — reuse_existing=True but the occupant does NOT answer as a
    healthy MCP server (e.g. a genuinely dead/wedged process still holding the
    port): falls back to the existing orphan kill-then-spawn path rather than
    trusting an unresponsive occupant."""
    spawned = []
    kill_calls = []
    monkeypatch.setattr(tc, "_port_is_open", lambda port, **kw: True)
    monkeypatch.setattr(tc, "_is_slot_claimed_by_live_client", lambda port, cid: False)
    monkeypatch.setattr(
        tc, "_kill_stale_port_occupant",
        lambda port, label, current_client_id="": kill_calls.append((port, label)),
    )
    monkeypatch.setattr(tc.subprocess, "Popen", lambda cmd, env=None, **kw: spawned.append(cmd) or _FakeProc())
    monkeypatch.setattr(tc, "_write_slot_claim", lambda *a, **kw: None)
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=False))

    proxy = tc.SlotProxy(
        ["mcp-proxy", "--port", "9211"], 9211, "code",
        client_id="cl-reuse-2", reuse_existing=True,
    )
    _run_sync(_run_ensure(proxy))

    assert proxy._reused is False
    assert kill_calls == [(9211, "code")]
    assert len(spawned) == 1


def test_ensure_running_reuse_existing_false_never_probes_health(monkeypatch):
    """Regression guard — a slot NOT opted into reuse (reuse_existing=False,
    the default for fs/extract) must never even consult the health probe, and
    must keep the pre-8e10fb80 refuse-on-live-peer behaviour, even if a probe
    WOULD report healthy."""
    spawned = []
    probe_calls = []

    async def fake_probe(port, **kw):
        probe_calls.append(port)
        return True

    monkeypatch.setattr(tc, "_port_is_open", lambda port, **kw: True)
    monkeypatch.setattr(tc, "_is_slot_claimed_by_live_client", lambda port, cid: True)
    monkeypatch.setattr(tc, "_probe_slot_health", fake_probe)
    monkeypatch.setattr(tc.subprocess, "Popen", lambda cmd, env=None, **kw: spawned.append(cmd) or _FakeProc())

    proxy = tc.SlotProxy(["mcp-proxy", "--port", "9212"], 9212, "fs", client_id="cl-reuse-3")
    _run_sync(_run_ensure(proxy))

    assert probe_calls == [], "reuse_existing=False must skip the health probe entirely"
    assert spawned == []
    assert proxy._reused is False


def test_slot_proxy_is_running_reused_tracks_live_port_and_self_clears(monkeypatch):
    """A reused proxy (no owned _proc) reports running exactly while the port
    stays open, and clears its own _reused flag once the port goes away so a
    subsequent ensure_running() correctly re-detects (respawn or re-reuse)."""
    proxy = tc.SlotProxy(["cmd"], 9213, "code", reuse_existing=True)
    proxy._reused = True

    monkeypatch.setattr(tc, "_port_is_open", lambda port, **kw: True)
    assert proxy.is_running is True
    assert proxy._reused is True  # unchanged while still live

    monkeypatch.setattr(tc, "_port_is_open", lambda port, **kw: False)
    assert proxy.is_running is False
    assert proxy._reused is False  # cleared once the occupant is gone


def test_slot_proxy_kill_on_reused_does_not_terminate_or_clear_claim(monkeypatch):
    """8e10fb80 — kill() on a reused proxy must NOT terminate the process (we
    don't own it — it could belong to a peer tunnel or an external process)
    and must NOT clear the port's slot-claim file (also not ours to clear).
    It only drops our own reuse tracking."""
    terminate_calls = []
    clear_calls = []
    monkeypatch.setattr(tc, "_terminate_proc_tree", lambda proc: terminate_calls.append(proc))
    monkeypatch.setattr(tc, "_clear_slot_claim", lambda port: clear_calls.append(port))

    proxy = tc.SlotProxy(["cmd"], 9214, "code", reuse_existing=True)
    proxy._reused = True

    proxy.kill()

    assert terminate_calls == [], "must not terminate a process we don't own"
    assert clear_calls == [], "must not clear a claim file we don't own"
    assert proxy._reused is False


def test_slot_proxy_kill_on_owned_proc_still_terminates_normally(monkeypatch):
    """Regression guard — the reused-kill guard must not interfere with the
    ordinary owned-process kill path."""
    terminate_calls = []
    clear_calls = []
    monkeypatch.setattr(tc, "_terminate_proc_tree", lambda proc: terminate_calls.append(proc))
    monkeypatch.setattr(tc, "_clear_slot_claim", lambda port: clear_calls.append(port))

    proxy = tc.SlotProxy(["cmd"], 9215, "code", reuse_existing=True)
    fake = _FakeProc()
    proxy._proc = fake
    proxy.holder["proc"] = fake

    proxy.kill()

    assert terminate_calls == [fake]
    assert clear_calls == [9215]
    assert proxy._proc is None
    assert proxy.holder["proc"] is None


# ---------------------------------------------------------------------------
# 3475c72f — a dedicated CBM_CACHE_DIR for the tunnel's own code-intel spawn,
# so it can never collide (same on-disk index .db) with any externally-
# spawned copy of codebase-memory-mcp (Desktop-direct-connector, a stray
# earlier generation, etc.).
# ---------------------------------------------------------------------------

def test_code_intel_cache_dir_is_dedicated_meridian_managed_path(monkeypatch, tmp_path):
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    cache_dir = tc._code_intel_cache_dir()
    base = tmp_path / ".meridian" / "code-intel-cache"
    # dee49d99 — no longer a single fixed path: it's a per-process-unique
    # subdirectory of the base managed path (see the pid/create_time tests
    # below for the actual uniqueness guarantee).
    assert cache_dir.parent == base
    assert str(os.getpid()) in cache_dir.name
    # Distinct from _managed_bin_dir (the binary install dir) and from
    # codebase-memory-mcp's own unset-env default (~/.cache/codebase-memory-mcp).
    assert cache_dir != tc._managed_bin_dir()
    assert "code-intel-cache" in str(cache_dir)


def test_code_intel_cache_dir_stable_within_one_process(monkeypatch, tmp_path):
    """dee49d99 — repeated calls within the SAME process must return the
    identical path: this process's pid/create_time identity does not change
    over its own lifetime, so within-generation warm-index reuse (e.g. across
    an idle-kill + lazy-respawn of the SAME long-running tunnel process) still
    works exactly as before this fix."""
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    assert tc._code_intel_cache_dir() == tc._code_intel_cache_dir()


def test_code_intel_cache_dir_differs_across_tunnel_generations(monkeypatch, tmp_path):
    """dee49d99 — the actual fix: two different tunnel PROCESS invocations
    (simulated here via different os.getpid() return values, standing in for
    two different `meridian --tunnel` generations across a fast restart) must
    resolve to two DIFFERENT cache dirs. This is what stops a fast restart's
    freshly-spawned codebase-memory-mcp from ever opening the same on-disk
    index .db the dying generation's copy may still have a handle on —
    closing the race unconditionally rather than depending on the dying
    process's OS-level handle being released before the new one opens it."""
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(tc.os, "getpid", lambda: 111111)
    first = tc._code_intel_cache_dir()
    monkeypatch.setattr(tc.os, "getpid", lambda: 222222)
    second = tc._code_intel_cache_dir()
    assert first != second
    assert first.parent == second.parent == tmp_path / ".meridian" / "code-intel-cache"


def test_code_intel_cache_dir_falls_back_to_pid_only_without_psutil(monkeypatch, tmp_path):
    """When psutil can't resolve this process's create_time (unavailable, or
    the lookup itself raises), the dir must still be unique by pid alone
    rather than raising or silently reverting to the pre-fix fixed path.

    ``_code_intel_cache_dir`` does ``import psutil`` locally inside its own
    try/except, so the fake module must be injected into ``sys.modules``
    (what the ``import`` statement actually consults) rather than patched
    onto ``tc`` itself."""
    import sys

    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))

    class _BoomProcess:
        def __init__(self, pid):
            raise RuntimeError("psutil unavailable in this test")

    fake_psutil = type("_FakePsutil", (), {"Process": _BoomProcess})
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    cache_dir = tc._code_intel_cache_dir()
    assert cache_dir == tmp_path / ".meridian" / "code-intel-cache" / str(os.getpid())


def test_code_intel_spawn_env_sets_cbm_cache_dir_and_preserves_parent_env(monkeypatch, tmp_path):
    """subprocess.Popen(env=...) REPLACES the child's whole environment when
    not None — a bare {"CBM_CACHE_DIR": ...} override would silently drop
    PATH and break the spawn entirely. Guard that the merge is a full copy of
    the parent env plus the one override."""
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("MERIDIAN_TEST_SENTINEL_VAR", "sentinel-value-xyz")

    env = tc._code_intel_spawn_env()

    assert env["CBM_CACHE_DIR"] == str(tc._code_intel_cache_dir())
    assert env["PATH"] == os.environ["PATH"]
    assert env["MERIDIAN_TEST_SENTINEL_VAR"] == "sentinel-value-xyz"


def test_code_intel_spawn_env_creates_cache_dir_on_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    assert not (tmp_path / ".meridian" / "code-intel-cache").exists()
    tc._code_intel_spawn_env()
    assert (tmp_path / ".meridian" / "code-intel-cache").is_dir()


def test_code_intel_spawn_env_honours_explicit_cbm_cache_dir_override(monkeypatch, tmp_path):
    """An operator who already set CBM_CACHE_DIR (e.g. a shared/network path
    for codebase-memory-mcp's own team-sharing artifact feature) is not
    overridden — this fix targets the unset-default collision, not an
    intentional operator choice."""
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    custom = str(tmp_path / "operator-chosen-cache")
    monkeypatch.setenv("CBM_CACHE_DIR", custom)

    env = tc._code_intel_spawn_env()

    assert env["CBM_CACHE_DIR"] == custom
    # And it must not have been silently created under our managed path.
    assert not (tmp_path / ".meridian" / "code-intel-cache").exists()


# ---------------------------------------------------------------------------
# dee49d99 — sweep OLD per-generation code-intel cache dirs left behind by
# the pid/create_time-suffixed _code_intel_cache_dir.
# ---------------------------------------------------------------------------

def test_cleanup_stale_code_intel_cache_dirs_noop_when_base_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    # Base dir doesn't exist at all yet -- must not raise.
    tc._cleanup_stale_code_intel_cache_dirs()
    assert not (tmp_path / ".meridian" / "code-intel-cache").exists()


def test_cleanup_stale_code_intel_cache_dirs_reclaims_dead_generations(monkeypatch, tmp_path):
    """A dead generation's dir (its pid is no longer alive) is reclaimed; the
    CURRENT process's own dir and any non-pid-suffixed directory are left
    untouched. psutil is faked (rather than using a real-but-nonexistent pid)
    so the test can never flake against a real live process coincidentally
    reusing an arbitrary pid number on the CI machine."""
    import sys

    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    base = tmp_path / ".meridian" / "code-intel-cache"
    base.mkdir(parents=True)
    own_pid = os.getpid()

    class _FakeProc:
        def __init__(self, pid):
            if pid != own_pid:
                raise RuntimeError("no such process")
            self._pid = pid

        def create_time(self):
            return 1000.0

    fake_psutil = type("_FakePsutil", (), {"Process": _FakeProc})
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    own_dir = base / f"{own_pid}-1000000"
    own_dir.mkdir()
    dead_dir = base / "999999-123456789"
    dead_dir.mkdir()
    (dead_dir / "index.db").write_text("stale")
    not_pid_dir = base / "not-a-pid-dir"
    not_pid_dir.mkdir()

    tc._cleanup_stale_code_intel_cache_dirs()

    assert own_dir.exists()          # never touch our own generation's dir
    assert not dead_dir.exists()     # dead pid -- reclaimed
    assert not_pid_dir.exists()      # not one of our pid-suffixed dirs -- left alone


def test_cleanup_stale_code_intel_cache_dirs_skips_without_psutil(monkeypatch, tmp_path):
    """No psutil means no safe way to tell a live generation from a dead one
    -- skip cleanup entirely rather than guessing (and possibly deleting a
    still-in-use index). Setting ``sys.modules["psutil"] = None`` is the
    standard idiom for simulating a missing module: the import machinery
    raises ImportError immediately rather than re-resolving the real one."""
    import sys

    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    base = tmp_path / ".meridian" / "code-intel-cache"
    base.mkdir(parents=True)
    some_dir = base / "999999-123456789"
    some_dir.mkdir()

    monkeypatch.setitem(sys.modules, "psutil", None)

    tc._cleanup_stale_code_intel_cache_dirs()

    assert some_dir.exists()  # left untouched -- cleanup skipped entirely


# ---------------------------------------------------------------------------
# a9d1ef7f — scoped cache-clear + spawn retry on uvx/npx tool-spawn failure
# ---------------------------------------------------------------------------


def test_detect_spawn_tool_uvx_bare():
    assert tc._detect_spawn_tool(["uvx", "my-package"]) == ("uvx", "my-package")


def test_detect_spawn_tool_uvx_with_y_flag():
    assert tc._detect_spawn_tool(["uvx", "-y", "some-pkg"]) == ("uvx", "some-pkg")


def test_detect_spawn_tool_uvx_from_flag_skips_value():
    # "uvx --from serena-agent serena start-mcp-server" — package is the token
    # AFTER the --from value ("serena-agent"), which is "serena".
    result = tc._detect_spawn_tool(
        ["uvx", "--from", "serena-agent", "serena", "start-mcp-server"]
    )
    assert result == ("uvx", "serena")


def test_detect_spawn_tool_uvx_exe_suffix():
    assert tc._detect_spawn_tool(["uvx.exe", "mcp-server"]) == ("uvx", "mcp-server")


def test_detect_spawn_tool_npx_bare():
    assert tc._detect_spawn_tool(["npx", "-y", "mcp-proxy"]) == ("npx", "mcp-proxy")


def test_detect_spawn_tool_npx_cmd_suffix():
    result = tc._detect_spawn_tool(["C:\\npm\\npx.cmd", "-y", "mcp-proxy"])
    assert result == ("npx", "mcp-proxy")


def test_detect_spawn_tool_cmd_c_wrapper_unwrap():
    # Windows "cmd /c npx -y mcp-proxy" pattern
    result = tc._detect_spawn_tool(
        ["cmd", "/c", "npx", "-y", "@wonderwhy-er/desktop-commander@latest"]
    )
    assert result == ("npx", "@wonderwhy-er/desktop-commander@latest")


def test_detect_spawn_tool_non_package_manager_returns_none():
    assert tc._detect_spawn_tool(["python", "-m", "code_extractor"]) is None


def test_detect_spawn_tool_bare_binary_returns_none():
    assert tc._detect_spawn_tool(["/usr/bin/my-mcp-server"]) is None


def test_detect_spawn_tool_empty_cmd_returns_none():
    assert tc._detect_spawn_tool([]) is None


def test_detect_spawn_tool_not_a_list_returns_none():
    assert tc._detect_spawn_tool(None) is None  # type: ignore[arg-type]


def test_detect_spawn_tool_uvx_no_package_after_flags_returns_none():
    # All tokens are flags / flag-values — no package name survives.
    assert tc._detect_spawn_tool(["uvx", "--from", "pkg-src"]) is None


def test_detect_spawn_tool_npx_flag_equals_form():
    # --cache=/tmp/c style — the "=" form is skipped, package follows
    result = tc._detect_spawn_tool(["npx", "--cache=/tmp/c", "mcp-proxy"])
    assert result == ("npx", "mcp-proxy")


# ---------------------------------------------------------------------------
# _spawn_with_cache_retry: behaviour tests
# ---------------------------------------------------------------------------


def test_spawn_with_cache_retry_succeeds_first_try(monkeypatch):
    """Normal path: first Popen succeeds → cache-clear and retry are never called."""
    clear_called = []
    monkeypatch.setattr(tc, "_scoped_cache_clear", lambda cmd, label="": clear_called.append(1))

    spawn_count = [0]
    fake_proc = _FakeProc()

    def _popen(cmd, env=None, **kw):
        spawn_count[0] += 1
        return fake_proc

    monkeypatch.setattr(tc.subprocess, "Popen", _popen)

    result = tc._spawn_with_cache_retry(["uvx", "my-tool"], None, "test")
    assert result is fake_proc
    assert spawn_count[0] == 1
    assert clear_called == [], "cache should NOT be cleared on a successful first spawn"


def test_spawn_with_cache_retry_clears_cache_and_retries_on_failure(monkeypatch):
    """Failure path: first Popen raises → scoped clear → second Popen succeeds."""
    clear_calls = []
    monkeypatch.setattr(
        tc, "_scoped_cache_clear",
        lambda cmd, label="": clear_calls.append(cmd) or True,
    )

    spawn_count = [0]
    fake_proc = _FakeProc()

    def _popen(cmd, env=None, **kw):
        spawn_count[0] += 1
        if spawn_count[0] == 1:
            raise FileNotFoundError("ENOENT: uvx not found (simulated first-try failure)")
        return fake_proc

    monkeypatch.setattr(tc.subprocess, "Popen", _popen)

    result = tc._spawn_with_cache_retry(["uvx", "mcp-server-code-extractor"], None, "extract")
    assert result is fake_proc
    assert spawn_count[0] == 2, "should have attempted exactly 2 spawns"
    assert len(clear_calls) == 1, "cache-clear should be called once between attempts"
    assert clear_calls[0] == ["uvx", "mcp-server-code-extractor"]


def test_spawn_with_cache_retry_raises_original_error_if_retry_also_fails(monkeypatch):
    """Both Popen attempts fail → the ORIGINAL first-try exception is re-raised."""
    monkeypatch.setattr(tc, "_scoped_cache_clear", lambda cmd, label="": True)

    spawn_count = [0]
    original_exc = OSError("spawn totally broken")

    def _popen(cmd, env=None, **kw):
        spawn_count[0] += 1
        if spawn_count[0] == 1:
            raise original_exc
        raise RuntimeError("retry also failed")

    monkeypatch.setattr(tc.subprocess, "Popen", _popen)

    import pytest
    with pytest.raises(OSError, match="spawn totally broken"):
        tc._spawn_with_cache_retry(["npx", "-y", "mcp-proxy"], None, "fs")
    assert spawn_count[0] == 2


def test_spawn_with_cache_retry_no_cache_clear_for_non_pm_command(monkeypatch):
    """A bare binary command that fails: retry fires, but cache-clear returns False."""
    clear_results = []

    def _fake_clear(cmd, label=""):
        result = tc._detect_spawn_tool(cmd) is not None  # mirrors real behaviour
        clear_results.append(result)
        return result

    monkeypatch.setattr(tc, "_scoped_cache_clear", _fake_clear)

    spawn_count = [0]
    fake_proc = _FakeProc()

    def _popen(cmd, env=None, **kw):
        spawn_count[0] += 1
        if spawn_count[0] == 1:
            raise OSError("ENOENT")
        return fake_proc

    monkeypatch.setattr(tc.subprocess, "Popen", _popen)

    result = tc._spawn_with_cache_retry(["/usr/local/bin/my-server"], None, "custom:foo")
    assert result is fake_proc
    # clear was called but detected no package manager (returns False), retry still ran
    assert spawn_count[0] == 2
    assert clear_results == [False], "no package-manager detected → clear returned False"


# ---------------------------------------------------------------------------
# _scoped_cache_clear: per-tool-type behaviour (unit-tested with mocks)
# ---------------------------------------------------------------------------


def test_scoped_cache_clear_uvx_calls_uv_cache_clean(monkeypatch, tmp_path):
    """uvx command → runs 'uv cache clean <pkg>', returns True."""
    import subprocess as _subprocess

    monkeypatch.setattr(tc.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    run_calls = []

    def _fake_run(cmd, **kw):
        run_calls.append(cmd)
        class R:
            returncode = 0
            stdout = stderr = ""
        return R()

    monkeypatch.setattr(tc.subprocess, "run", _fake_run)

    result = tc._scoped_cache_clear(["uvx", "some-mcp-tool"], "test")
    assert result is True
    assert len(run_calls) == 1
    assert run_calls[0] == ["/usr/bin/uv", "cache", "clean", "some-mcp-tool"]


def test_scoped_cache_clear_npx_removes_npx_cache_dir(monkeypatch, tmp_path):
    """npx command → removes the _npx cache dir, returns True."""
    npx_dir = tmp_path / "_npx"
    npx_dir.mkdir()
    (npx_dir / "some_cache_entry").mkdir()

    # Patch _package_cache_locations to return our tmp dir.
    monkeypatch.setattr(
        tc, "_package_cache_locations",
        lambda: {"npx": str(npx_dir), "uvx": str(tmp_path / "uv"), "uv_tools": str(tmp_path / "tools")},
    )

    result = tc._scoped_cache_clear(["npx", "-y", "mcp-proxy"], "fs")
    assert result is True
    # The _npx dir should have been removed.
    assert not npx_dir.exists()


def test_scoped_cache_clear_returns_false_for_non_pm_command(monkeypatch):
    """Non-uvx/npx command → returns False immediately, no subprocess call."""
    run_calls = []
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **kw: run_calls.append(a))

    result = tc._scoped_cache_clear(["python", "-m", "code_extractor"], "extract")
    assert result is False
    assert run_calls == []


def test_scoped_cache_clear_npx_noop_when_cache_dir_missing(monkeypatch, tmp_path):
    """_npx dir doesn't exist → returns True anyway (retry should still proceed)."""
    monkeypatch.setattr(
        tc, "_package_cache_locations",
        lambda: {"npx": str(tmp_path / "_npx_missing"), "uvx": "", "uv_tools": ""},
    )
    result = tc._scoped_cache_clear(["npx", "-y", "@pkg/tool"], "slot")
    assert result is True  # attempted (retry proceeds even when dir is absent)


def test_scoped_cache_clear_uvx_uv_not_found_still_returns_true(monkeypatch, tmp_path):
    """uv binary not on PATH and not in ~/.local/bin → returns True (retry proceeds)."""
    monkeypatch.setattr(tc.shutil, "which", lambda name: None)
    # Make ~/.local/bin/uv non-existent.
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))

    result = tc._scoped_cache_clear(["uvx", "--from", "serena-agent", "serena"], "extract")
    assert result is True  # retry should still be attempted


# ---------------------------------------------------------------------------
# 3db4f8d8 — TAR_ENTRY_ERROR detection + thorough cache clear + pinned version
# ---------------------------------------------------------------------------


class _FakeProc2:
    """Minimal Popen stand-in for TAR/retry tests."""
    returncode = None

    def __init__(self, exit_code=None):
        self._exit_code = exit_code

    def poll(self):
        return self._exit_code

    def kill(self): pass
    def wait(self, timeout=None): pass
    def terminate(self): pass


# --- _is_tar_entry_error ---------------------------------------------------

def test_is_tar_entry_error_canonical_uppercase():
    """TAR_ENTRY_ERROR (the canonical npm error code) is detected case-insensitively."""
    assert tc._is_tar_entry_error("npm ERR! TAR_ENTRY_ERROR ENOTSUP")


def test_is_tar_entry_error_lowercase():
    assert tc._is_tar_entry_error("error: tar_entry_error while extracting")


def test_is_tar_entry_error_integrity_check_failed():
    """npm v9+ uses 'integrity check failed' for the same class of corruption."""
    assert tc._is_tar_entry_error("npm ERR! Integrity check failed for package.tgz")


def test_is_tar_entry_error_eintegrity():
    assert tc._is_tar_entry_error("npm ERR! code EINTEGRITY")


def test_is_tar_entry_error_corrupted_package():
    assert tc._is_tar_entry_error("error: corrupted package detected during install")


def test_is_tar_entry_error_unexpected_end_of_data():
    assert tc._is_tar_entry_error("Error: unexpected end of data")


def test_is_tar_entry_error_clean_output_returns_false():
    assert not tc._is_tar_entry_error("Server started on port 3000")
    assert not tc._is_tar_entry_error("Desktop Commander MCP server ready")


def test_is_tar_entry_error_empty_returns_false():
    assert not tc._is_tar_entry_error("")
    assert not tc._is_tar_entry_error(None)  # type: ignore[arg-type]


# --- _dc_default_command pinned version ------------------------------------

def test_dc_default_command_uses_pinned_version():
    """_dc_default_command must NOT use @latest — it must use the pinned version."""
    cmd = tc._dc_default_command()
    # Unwrap the Windows cmd /c wrapper if present
    flat = " ".join(cmd)
    assert "@latest" not in flat, "desktop-commander must be pinned, not @latest"
    assert tc._DC_PINNED_VERSION in flat, (
        f"expected pinned version {tc._DC_PINNED_VERSION!r} in command {cmd!r}"
    )


def test_dc_default_command_windows_not_wrapped(monkeypatch):
    # 83bd7f21 -- no longer cmd/c-wrapped on Windows (see the sibling
    # test_dc_default_command_wraps_cmd_on_windows for the full rationale):
    # the extra nested cmd.exe layer was found live to break mcp-proxy's
    # tools/list readiness probe. _build_proxy_for_inner's shared npx.cmd
    # resolution + --shell now handles this uniformly for all slots.
    monkeypatch.setattr(tc.sys, "platform", "win32")
    cmd = tc._dc_default_command()
    assert cmd[0] == "npx"
    assert "cmd" not in cmd
    assert tc._DC_PINNED_VERSION in " ".join(cmd)
    assert "@latest" not in " ".join(cmd)


def test_dc_default_command_posix_not_wrapped(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "linux")
    cmd = tc._dc_default_command()
    assert cmd[0] == "npx"
    assert tc._DC_PINNED_VERSION in " ".join(cmd)


def test_dc_pinned_version_in_builtin_plugins():
    """BUILTIN_PLUGINS desktop-commander entry must reference the pinned version
    in its comment or command — the @latest string must not appear there."""
    from meridian.tunnel_plugins import BUILTIN_PLUGINS
    dc = next(p for p in BUILTIN_PLUGINS if p["name"] == "desktop-commander")
    # command is None (spawned via _dc_default_command fallback) — that's fine;
    # the resolved command must carry the pinned version, not @latest.
    # _office_slot_command(dc_slot, dc_plugin) falls back to _dc_default_command().
    resolved = tc._office_slot_command("dc", dc)
    assert resolved is not None
    flat = " ".join(resolved)
    assert "@latest" not in flat
    assert tc._DC_PINNED_VERSION in flat


# --- _scoped_cache_clear_thorough ------------------------------------------

def test_scoped_cache_clear_thorough_calls_npm_cache_clean(monkeypatch, tmp_path):
    """For an npx command, thorough clear calls npm cache clean --force."""
    npx_dir = tmp_path / "_npx"
    npx_dir.mkdir()
    monkeypatch.setattr(
        tc, "_package_cache_locations",
        lambda: {"npx": str(npx_dir), "uvx": "", "uv_tools": ""},
    )
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/usr/bin/npm" if name == "npm" else None)

    npm_calls = []

    def _fake_run(cmd, **kw):
        npm_calls.append(cmd)
        class R:
            returncode = 0
            stdout = stderr = ""
        return R()

    monkeypatch.setattr(tc.subprocess, "run", _fake_run)

    tc._scoped_cache_clear_thorough(
        ["npx", "-y", "@wonderwhy-er/desktop-commander@0.2.46"], "dc"
    )
    # The full npm cache clean must have been attempted.
    assert any("cache" in " ".join(c) and "clean" in " ".join(c) for c in npm_calls), (
        f"expected npm cache clean call; got {npm_calls!r}"
    )
    assert any("--force" in c for c in npm_calls)


def test_scoped_cache_clear_thorough_skips_npm_for_uvx(monkeypatch, tmp_path):
    """For a uvx command, thorough clear only does the scoped uv clear (no npm)."""
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    run_calls = []

    def _fake_run(cmd, **kw):
        run_calls.append(cmd)
        class R:
            returncode = 0
            stdout = stderr = ""
        return R()

    monkeypatch.setattr(tc.subprocess, "run", _fake_run)

    tc._scoped_cache_clear_thorough(["uvx", "my-mcp-server"], "extract")
    # Only uv cache clean — no npm call.
    assert all("uv" in str(c[0]) for c in run_calls), (
        f"unexpected non-uv calls: {run_calls!r}"
    )
    assert not any("npm" in " ".join(c) for c in run_calls)


def test_scoped_cache_clear_thorough_npm_not_found_doesnt_raise(monkeypatch, tmp_path):
    """If npm is not on PATH, thorough clear logs and returns gracefully."""
    npx_dir = tmp_path / "_npx"
    npx_dir.mkdir()
    monkeypatch.setattr(
        tc, "_package_cache_locations",
        lambda: {"npx": str(npx_dir), "uvx": "", "uv_tools": ""},
    )
    monkeypatch.setattr(tc.shutil, "which", lambda name: None)  # npm not found
    # Must not raise.
    tc._scoped_cache_clear_thorough(
        ["npx", "-y", "@wonderwhy-er/desktop-commander@0.2.46"], "dc"
    )


# --- _probe_tar_entry_error ------------------------------------------------

def test_probe_tar_entry_error_detects_tar_in_stderr(monkeypatch):
    """When the probed process exits quickly with TAR_ENTRY_ERROR in stderr,
    _probe_tar_entry_error returns True."""
    import subprocess as _sp

    class _TarProc:
        returncode = 1
        def communicate(self, timeout=None):
            return b"", b"npm ERR! TAR_ENTRY_ERROR ENOTSUP"

    monkeypatch.setattr(
        tc.subprocess, "Popen",
        lambda cmd, env=None, **kw: _TarProc(),
    )
    assert tc._probe_tar_entry_error(
        ["npx", "-y", "@wonderwhy-er/desktop-commander@0.2.46"], None, "dc"
    ) is True


def test_probe_tar_entry_error_returns_false_for_long_running_process(monkeypatch):
    """A process that doesn't exit within the probe window is NOT a TAR error."""
    import subprocess as _sp

    class _LongRunningProc:
        returncode = None
        def communicate(self, timeout=None):
            raise _sp.TimeoutExpired(cmd=["npx"], timeout=timeout)
        def kill(self): pass

    monkeypatch.setattr(
        tc.subprocess, "Popen",
        lambda cmd, env=None, **kw: _LongRunningProc(),
    )
    assert tc._probe_tar_entry_error(
        ["npx", "-y", "@wonderwhy-er/desktop-commander@0.2.46"], None, "dc",
        wait_seconds=0.01,
    ) is False


def test_probe_tar_entry_error_returns_false_on_clean_fast_exit(monkeypatch):
    """A process that exits cleanly (no TAR signature) returns False."""

    class _CleanProc:
        returncode = 0
        def communicate(self, timeout=None):
            return b"Desktop Commander ready", b""
        def kill(self): pass

    monkeypatch.setattr(
        tc.subprocess, "Popen",
        lambda cmd, env=None, **kw: _CleanProc(),
    )
    assert tc._probe_tar_entry_error(["npx", "-y", "tool"], None, "dc") is False


def test_probe_tar_entry_error_returns_false_on_popen_exception(monkeypatch):
    """If the diagnostic Popen itself raises, the probe returns False (never raises)."""
    def _boom(cmd, env=None, **kw):
        raise OSError("cannot spawn")

    monkeypatch.setattr(tc.subprocess, "Popen", _boom)
    # Must not raise — returns False.
    result = tc._probe_tar_entry_error(["npx", "-y", "tool"], None, "dc")
    assert result is False


# --- _spawn_with_cache_retry TAR_ENTRY_ERROR path --------------------------

def test_spawn_with_cache_retry_tar_error_uses_thorough_clear(monkeypatch):
    """When the first Popen succeeds but the process immediately exits with a
    TAR_ENTRY_ERROR, the thorough clear is used (not just scoped clear) and
    a second Popen is attempted."""
    thorough_called = []
    scoped_called = []
    monkeypatch.setattr(
        tc, "_scoped_cache_clear_thorough",
        lambda cmd, label="": thorough_called.append(cmd),
    )
    monkeypatch.setattr(
        tc, "_scoped_cache_clear",
        lambda cmd, label="": scoped_called.append(cmd) or True,
    )
    monkeypatch.setattr(
        tc, "_probe_tar_entry_error",
        lambda cmd, env, label, wait_seconds=5.0: True,
    )
    # Patch time.sleep to avoid a real 100ms pause in the test.
    monkeypatch.setattr(tc.time, "sleep", lambda n: None)

    spawn_count = [0]
    fake_proc_alive = _FakeProc2(exit_code=None)   # second spawn stays alive
    fake_proc_dead = _FakeProc2(exit_code=1)        # first spawn exits immediately

    def _popen(cmd, env=None, **kw):
        spawn_count[0] += 1
        if spawn_count[0] == 1:
            return fake_proc_dead
        return fake_proc_alive

    monkeypatch.setattr(tc.subprocess, "Popen", _popen)

    result = tc._spawn_with_cache_retry(
        ["npx", "-y", "@wonderwhy-er/desktop-commander@0.2.46"], None, "dc"
    )
    assert result is fake_proc_alive
    assert spawn_count[0] == 2
    assert len(thorough_called) == 1, "thorough clear must be called for TAR error"
    assert len(scoped_called) == 0, "scoped-only clear must NOT be called for TAR error"


def test_spawn_with_cache_retry_generic_fail_uses_scoped_clear(monkeypatch):
    """The a9d1ef7f generic spawn-exception path still uses the scoped clear only."""
    thorough_called = []
    scoped_called = []
    monkeypatch.setattr(
        tc, "_scoped_cache_clear_thorough",
        lambda cmd, label="": thorough_called.append(cmd),
    )
    monkeypatch.setattr(
        tc, "_scoped_cache_clear",
        lambda cmd, label="": scoped_called.append(cmd) or True,
    )
    monkeypatch.setattr(tc.time, "sleep", lambda n: None)

    spawn_count = [0]
    fake_proc = _FakeProc2(exit_code=None)

    def _popen(cmd, env=None, **kw):
        spawn_count[0] += 1
        if spawn_count[0] == 1:
            raise OSError("ENOENT — generic spawn failure")
        return fake_proc

    monkeypatch.setattr(tc.subprocess, "Popen", _popen)

    result = tc._spawn_with_cache_retry(["npx", "-y", "mcp-proxy"], None, "fs")
    assert result is fake_proc
    assert spawn_count[0] == 2
    assert len(scoped_called) == 1, "scoped clear must be called for generic spawn failure"
    assert len(thorough_called) == 0, "thorough clear must NOT be called for generic failure"


def test_spawn_with_cache_retry_fast_exit_non_tar_uses_scoped_clear(monkeypatch):
    """Process exits immediately but without TAR signature → falls back to scoped clear."""
    thorough_called = []
    scoped_called = []
    monkeypatch.setattr(
        tc, "_scoped_cache_clear_thorough",
        lambda cmd, label="": thorough_called.append(cmd),
    )
    monkeypatch.setattr(
        tc, "_scoped_cache_clear",
        lambda cmd, label="": scoped_called.append(cmd) or True,
    )
    monkeypatch.setattr(
        tc, "_probe_tar_entry_error",
        lambda cmd, env, label, wait_seconds=5.0: False,  # not a TAR error
    )
    monkeypatch.setattr(tc.time, "sleep", lambda n: None)

    spawn_count = [0]
    fake_proc_dead = _FakeProc2(exit_code=1)
    fake_proc_alive = _FakeProc2(exit_code=None)

    def _popen(cmd, env=None, **kw):
        spawn_count[0] += 1
        if spawn_count[0] == 1:
            return fake_proc_dead
        return fake_proc_alive

    monkeypatch.setattr(tc.subprocess, "Popen", _popen)

    result = tc._spawn_with_cache_retry(["npx", "-y", "some-tool"], None, "slot")
    assert result is fake_proc_alive
    assert spawn_count[0] == 2
    assert len(scoped_called) == 1, "scoped clear used for non-TAR fast-exit"
    assert len(thorough_called) == 0


# ---------------------------------------------------------------------------
# Staleness alert v1 (986117fc) — cheap local git-commit-drift warning.
#
# "manual Ctrl-C + restart every time a client-side fix lands" friction: at
# startup, capture the commit this process is actually running; periodically
# re-check the on-disk commit; warn (never restart) on divergence. Purely
# local (git rev-parse), no server round-trip, no live-reset/self-restart
# (explicitly out of scope for this item).
# ---------------------------------------------------------------------------

def _run_git(args: list, cwd: str) -> None:
    import subprocess as _sp
    _sp.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, check=True,
        env={**os.environ,
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"},
    )


def _make_commit(repo_dir: str, filename: str, content: str) -> str:
    import subprocess as _sp
    with open(os.path.join(repo_dir, filename), "w") as f:
        f.write(content)
    _run_git(["add", "-A"], repo_dir)
    _run_git(["commit", "-m", f"add {filename}"], repo_dir)
    out = _sp.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir,
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def test_tunnel_client_git_root_is_this_modules_directory():
    """_tunnel_client_git_root points at the dir containing tunnel_client.py."""
    assert tc._tunnel_client_git_root() == str(tc.Path(tc.__file__).resolve().parent)


class TestTunnelClientCommitHashSync:
    """Real (temp, on-disk) git repos — no mocking of git itself, mirrors
    prospect.py's TestGitCommitDriftSync."""

    def test_not_a_directory_returns_none(self):
        assert tc._tunnel_client_commit_hash_sync("/definitely/not/a/real/path/xyz") is None

    def test_not_a_git_repo_returns_none(self, tmp_path):
        d = tmp_path / "not_a_repo"
        d.mkdir()
        assert tc._tunnel_client_commit_hash_sync(str(d)) is None

    def test_real_repo_matches_head(self, tmp_path):
        repo = tmp_path / "repo1"
        repo.mkdir()
        _run_git(["init"], str(repo))
        commit1 = _make_commit(str(repo), "a.txt", "one")
        assert tc._tunnel_client_commit_hash_sync(str(repo)) == commit1

    def test_updates_after_new_commit(self, tmp_path):
        repo = tmp_path / "repo2"
        repo.mkdir()
        _run_git(["init"], str(repo))
        _make_commit(str(repo), "a.txt", "one")
        commit2 = _make_commit(str(repo), "b.txt", "two")
        assert tc._tunnel_client_commit_hash_sync(str(repo)) == commit2

    def test_git_binary_missing_returns_none(self, monkeypatch, tmp_path):
        def _raise(*a, **k):
            raise FileNotFoundError("git not found")
        monkeypatch.setattr(tc.subprocess, "run", _raise)
        assert tc._tunnel_client_commit_hash_sync(str(tmp_path)) is None

    def test_defaults_to_module_root_when_no_arg_given(self, monkeypatch):
        """No root_dir passed → falls back to _tunnel_client_git_root()."""
        seen = {}

        class FakeResult:
            returncode = 0
            stdout = "deadbeef1234\n"

        def fake_run(cmd, cwd=None, **kw):
            seen["cwd"] = cwd
            return FakeResult()

        monkeypatch.setattr(tc.subprocess, "run", fake_run)
        result = tc._tunnel_client_commit_hash_sync()
        assert result == "deadbeef1234"
        assert seen["cwd"] == tc._tunnel_client_git_root()


def test_staleness_alert_loop_returns_immediately_without_baseline():
    """No started_commit (e.g. not a git checkout at startup) → no-op, no loop."""
    async def drive():
        await asyncio.wait_for(tc._staleness_alert_loop(None), timeout=1.0)
    asyncio.run(drive())  # would hang/timeout if the loop didn't early-return


def test_staleness_alert_loop_warns_once_on_divergence(monkeypatch, capsys):
    """Disk commit differs from the started commit → one clear stderr warning."""
    calls = {"n": 0}

    def fake_hash(root_dir=None):
        calls["n"] += 1
        return "def456abcdef"  # always diverged from started_commit below

    monkeypatch.setattr(tc, "_tunnel_client_commit_hash_sync", fake_hash)

    _real_sleep = asyncio.sleep
    async def quick_sleep(_n):
        await _real_sleep(0)
    monkeypatch.setattr(tc.asyncio, "sleep", quick_sleep)

    async def drive():
        task = asyncio.ensure_future(
            tc._staleness_alert_loop("abc123000000", interval=0.001)
        )
        # Each poll hops through a REAL thread-pool executor (asyncio.to_thread),
        # so give it real wall-clock slices to run rather than instant yields.
        for _ in range(300):
            await _real_sleep(0.01)
            if calls["n"] >= 3:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert calls["n"] >= 3, "commit-hash probe never ran enough times for this test to be meaningful"
    captured = capsys.readouterr()
    # Warned exactly once despite multiple poll iterations seeing the same
    # diverged commit (no spam once a divergence has already been reported).
    assert captured.err.count("has been updated since this process started") == 1
    assert "abc123000000"[:12] in captured.err
    assert "def456abcdef"[:12] in captured.err
    assert "restart the tunnel" in captured.err


def test_staleness_alert_loop_no_warning_when_commit_unchanged(monkeypatch, capsys):
    """Disk commit == started commit → no warning ever printed."""
    monkeypatch.setattr(tc, "_tunnel_client_commit_hash_sync", lambda root_dir=None: "same000000")

    _real_sleep = asyncio.sleep
    async def quick_sleep(_n):
        await _real_sleep(0)
    monkeypatch.setattr(tc.asyncio, "sleep", quick_sleep)

    async def drive():
        task = asyncio.ensure_future(
            tc._staleness_alert_loop("same000000", interval=0.001)
        )
        for _ in range(10):
            await _real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    captured = capsys.readouterr()
    assert "has been updated" not in captured.err


def test_staleness_alert_loop_survives_probe_exception(monkeypatch, capsys):
    """A raising commit-hash probe must never kill the loop (fail-open)."""
    state = {"n": 0}

    def flaky_hash(root_dir=None):
        state["n"] += 1
        raise RuntimeError("git transiently unavailable")

    monkeypatch.setattr(tc, "_tunnel_client_commit_hash_sync", flaky_hash)

    _real_sleep = asyncio.sleep
    async def quick_sleep(_n):
        await _real_sleep(0)
    monkeypatch.setattr(tc.asyncio, "sleep", quick_sleep)

    async def drive():
        task = asyncio.ensure_future(
            tc._staleness_alert_loop("abc123000000", interval=0.001)
        )
        # Each poll hops through a REAL thread-pool executor (asyncio.to_thread),
        # so give it real wall-clock slices to run rather than instant yields.
        for _ in range(300):
            await _real_sleep(0.01)
            if state["n"] >= 3:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())  # must not raise out of drive()
    captured = capsys.readouterr()
    assert "has been updated" not in captured.err
    assert state["n"] >= 3
