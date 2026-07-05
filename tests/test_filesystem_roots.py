"""Tests for configurable filesystem connector roots (executor_config.filesystem_roots).

Covers: the executor_config schema + DB save/load round-trip, the per-tenant
union helper used by GET /tunnel/filesystem-roots, and the tunnel client's
filesystem-server command builder (multi-root vs home-dir fallback).
"""
from __future__ import annotations

import asyncio
import json

from meridian import db as db_module
from meridian import tunnel_client as tc
from meridian.executor_config import normalize_executor_config
from meridian.routes import tunnel as tn


# ---------------------------------------------------------------------------
# Schema + DB round-trip
# ---------------------------------------------------------------------------

def test_normalize_executor_config_keeps_filesystem_roots():
    out = normalize_executor_config({"filesystem_roots": ["/x", "/y"], "bogus": 1})
    assert out["filesystem_roots"] == ["/x", "/y"]
    assert "bogus" not in out


def test_executor_config_filesystem_roots_roundtrip():
    async def _run():
        db = await db_module.init_db(":memory:")
        proj = await db_module.create_project(db, "fs-roots-roundtrip")
        await db_module.set_executor_config(
            db, proj["id"],
            {"filesystem_roots": ["C:/Users/me/Documents", "D:/Projects"], "repo_paths": []},
        )
        return await db_module.get_executor_config(db, proj["id"])

    cfg = asyncio.run(_run())
    assert cfg["filesystem_roots"] == ["C:/Users/me/Documents", "D:/Projects"]


# ---------------------------------------------------------------------------
# Per-tenant union (GET /tunnel/filesystem-roots helper)
# ---------------------------------------------------------------------------

def test_union_filesystem_roots_dedupes_and_parses():
    projects = [
        {"executor_config": json.dumps({"filesystem_roots": ["/a", "/b"]})},
        {"executor_config": {"filesystem_roots": ["/b", "/c"]}},  # already a dict
        {"executor_config": None},                                 # no config
        {"executor_config": "not json at all"},                    # malformed
        {"executor_config": json.dumps({"filesystem_roots": [" ", 5, "/d "]})},  # junk + trim
    ]
    assert tn._union_filesystem_roots(projects) == ["/a", "/b", "/c", "/d"]


def test_union_filesystem_roots_empty_when_none_set():
    assert tn._union_filesystem_roots([{"executor_config": json.dumps({"repo_paths": []})}]) == []
    assert tn._union_filesystem_roots([]) == []


# ---------------------------------------------------------------------------
# Filesystem-server command builder (multi-root vs home-dir fallback)
# ---------------------------------------------------------------------------

def test_build_proxy_command_uses_roots_when_provided():
    cmd = tc._build_proxy_command("npx", "/home/me", 8808, roots=["/a", "/b"])
    assert "@modelcontextprotocol/server-filesystem" in cmd
    # Both roots are appended as the served dirs.
    sep = cmd.index("--")
    assert cmd[sep + 1:] == ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/a", "/b"]


def test_build_proxy_command_falls_back_to_repo_path():
    cmd = tc._build_proxy_command("npx", "/home/me", 8808, roots=[])
    assert cmd[-1] == "/home/me"
    # None behaves the same (default unchanged).
    assert tc._build_proxy_command("npx", "/home/me", 8808)[-1] == "/home/me"


def test_build_proxy_command_ignores_blank_roots():
    cmd = tc._build_proxy_command("npx", "/home/me", 8808, roots=["", "  "])
    assert cmd[-1] == "/home/me"


# ---------------------------------------------------------------------------
# _union_repo_paths helper (GET /tunnel/filesystem-roots → known_repo_paths)
# ---------------------------------------------------------------------------

def test_union_repo_paths_collects_from_executor_config():
    projects = [
        {"executor_config": json.dumps({"repo_path": "/home/me/proj"})},
        {"executor_config": {"repo_path": "/other/repo"}},   # already a dict
        {"executor_config": None},                            # no config
        {"executor_config": "bad json"},                      # malformed
        {"executor_config": json.dumps({"repo_path": "  "})},  # blank
    ]
    assert tn._union_repo_paths(projects) == ["/home/me/proj", "/other/repo"]


def test_union_repo_paths_deduplicates():
    projects = [
        {"executor_config": json.dumps({"repo_path": "/a"})},
        {"executor_config": json.dumps({"repo_path": "/a"})},
        {"executor_config": json.dumps({"repo_path": "/b"})},
    ]
    assert tn._union_repo_paths(projects) == ["/a", "/b"]


def test_union_repo_paths_empty():
    assert tn._union_repo_paths([]) == []
    assert tn._union_repo_paths([{"executor_config": json.dumps({"filesystem_roots": ["/x"]})}]) == []


# ---------------------------------------------------------------------------
# b970fe07 — serena_repo_path + codebase_code_dirs (executor_config extensions)
# ---------------------------------------------------------------------------

def test_normalize_executor_config_keeps_serena_repo_path_and_code_dirs():
    """b970fe07 — the two new keys survive normalize; a non-empty code_dirs list
    is preserved (and junk keys are still dropped)."""
    out = normalize_executor_config({
        "serena_repo_path": "  C:/repo  ",
        "codebase_code_dirs": ["/a", "/b"],
        "bogus": 1,
    })
    assert out["serena_repo_path"] == "C:/repo"          # str trimmed
    assert out["codebase_code_dirs"] == ["/a", "/b"]     # non-empty list kept
    assert "bogus" not in out


def test_normalize_executor_config_drops_empty_serena_repo_path():
    """An all-whitespace serena_repo_path is dropped (mirrors other str keys)."""
    out = normalize_executor_config({"serena_repo_path": "   ", "codebase_code_dirs": []})
    assert "serena_repo_path" not in out
    # An empty list is retained as-is (same as filesystem_roots/repo_paths).
    assert out["codebase_code_dirs"] == []


def test_executor_config_serena_and_code_dirs_roundtrip():
    async def _run():
        db = await db_module.init_db(":memory:")
        proj = await db_module.create_project(db, "serena-code-roundtrip")
        await db_module.set_executor_config(
            db, proj["id"],
            {"serena_repo_path": "C:/Users/me/repo",
             "codebase_code_dirs": ["C:/Users/me/repo", "D:/other"]},
        )
        return await db_module.get_executor_config(db, proj["id"])

    cfg = asyncio.run(_run())
    assert cfg["serena_repo_path"] == "C:/Users/me/repo"
    assert cfg["codebase_code_dirs"] == ["C:/Users/me/repo", "D:/other"]


def test_first_serena_repo_path_takes_first_non_empty():
    projects = [
        {"executor_config": json.dumps({"repo_path": "/x"})},          # no serena key
        {"executor_config": json.dumps({"serena_repo_path": "  "})},   # blank → skip
        {"executor_config": {"serena_repo_path": "/first/repo"}},      # dict, wins
        {"executor_config": json.dumps({"serena_repo_path": "/second"})},
    ]
    assert tn._first_serena_repo_path(projects) == "/first/repo"


def test_first_serena_repo_path_empty_when_unset():
    assert tn._first_serena_repo_path([]) == ""
    assert tn._first_serena_repo_path(
        [{"executor_config": json.dumps({"filesystem_roots": ["/x"]})},
         {"executor_config": None},
         {"executor_config": "not json"}]
    ) == ""


def test_union_codebase_code_dirs_dedupes_and_parses():
    projects = [
        {"executor_config": json.dumps({"codebase_code_dirs": ["/a", "/b"]})},
        {"executor_config": {"codebase_code_dirs": ["/b", "/c"]}},     # dict, dedupe /b
        {"executor_config": None},                                     # no config
        {"executor_config": "bad json"},                               # malformed
        {"executor_config": json.dumps({"codebase_code_dirs": [" ", 5, "/d "]})},
    ]
    assert tn._union_codebase_code_dirs(projects) == ["/a", "/b", "/c", "/d"]


def test_union_codebase_code_dirs_empty_when_unset():
    assert tn._union_codebase_code_dirs([]) == []
    assert tn._union_codebase_code_dirs(
        [{"executor_config": json.dumps({"repo_path": "/x"})}]
    ) == []


# ---------------------------------------------------------------------------
# _add_fs_roots_to_cmd helper
# ---------------------------------------------------------------------------

def test_add_fs_roots_to_cmd_appends_new_roots():
    cmd = tc._build_proxy_command("npx", "/home/me", 8808, roots=["/a"])
    updated, changed = tc._add_fs_roots_to_cmd(cmd, ["/b", "/c"])
    assert changed is True
    assert "@modelcontextprotocol/server-filesystem" in updated
    assert "/b" in updated
    assert "/c" in updated
    assert "/a" in updated


def test_add_fs_roots_to_cmd_deduplicates_existing():
    cmd = tc._build_proxy_command("npx", "/home/me", 8808, roots=["/a", "/b"])
    updated, changed = tc._add_fs_roots_to_cmd(cmd, ["/a", "/b"])
    assert changed is False
    assert updated is cmd


def test_add_fs_roots_to_cmd_no_server_token():
    cmd = ["npx", "-y", "mcp-proxy", "--port", "8808"]
    _, changed = tc._add_fs_roots_to_cmd(cmd, ["/x"])
    assert changed is False


# ---------------------------------------------------------------------------
# _extract_denied_path helper
# ---------------------------------------------------------------------------

def test_extract_denied_path_finds_path():
    import base64
    body = b'{"error":"Access denied - path outside allowed directories: /secret/dir"}'
    resp = {"body": base64.b64encode(body).decode(), "status": 403}
    result = tc._extract_denied_path(resp)
    assert result == "/secret/dir"


def test_extract_denied_path_returns_none_when_not_denied():
    import base64
    body = b'{"result": "ok"}'
    resp = {"body": base64.b64encode(body).decode(), "status": 200}
    assert tc._extract_denied_path(resp) is None


def test_extract_denied_path_returns_none_on_bad_body():
    resp = {"body": "not base64!!!", "status": 403}
    assert tc._extract_denied_path(resp) is None


# ---------------------------------------------------------------------------
# _is_subpath helper
# ---------------------------------------------------------------------------

def test_is_subpath_detects_child():
    from pathlib import Path
    assert tc._is_subpath(Path("/a/b/c"), Path("/a/b")) is True
    assert tc._is_subpath(Path("/a/b"), Path("/a/b")) is True  # same path


def test_is_subpath_rejects_sibling():
    from pathlib import Path
    assert tc._is_subpath(Path("/a/x"), Path("/a/b")) is False
    assert tc._is_subpath(Path("/other"), Path("/a/b")) is False


# ---------------------------------------------------------------------------
# send_add_fs_roots_control
# ---------------------------------------------------------------------------

class _FakeFsWS:
    """Minimal WebSocket stub for send_add_fs_roots_control tests."""
    def __init__(self, raise_on_send=False):
        self.sent = []
        self._raise = raise_on_send

    async def send_json(self, obj):
        if self._raise:
            raise RuntimeError("ws broken")
        self.sent.append(obj)


def test_send_add_fs_roots_control_not_connected():
    result = asyncio.run(tn.send_add_fs_roots_control("no-tenant", ["/x"]))
    assert result["status"] == "not_connected"


def test_send_add_fs_roots_control_ok():
    ws = _FakeFsWS()
    tn._tunnel_sockets["t2"] = ws
    try:
        result = asyncio.run(tn.send_add_fs_roots_control("t2", ["/a", "/b"]))
        assert result == {"status": "ok", "roots": ["/a", "/b"]}
        assert ws.sent == [{"type": "add_fs_roots", "roots": ["/a", "/b"]}]
    finally:
        tn._tunnel_sockets.pop("t2", None)


def test_send_add_fs_roots_control_send_error():
    ws = _FakeFsWS(raise_on_send=True)
    tn._tunnel_sockets["t3"] = ws
    try:
        result = asyncio.run(tn.send_add_fs_roots_control("t3", ["/x"]))
        assert result["status"] == "error"
    finally:
        tn._tunnel_sockets.pop("t3", None)


# ---------------------------------------------------------------------------
# _run_connection_lazy: add_fs_roots control message
# ---------------------------------------------------------------------------

def test_run_connection_lazy_handles_add_fs_roots(monkeypatch):
    """An add_fs_roots control message updates proxy.cmd and kills/restarts the proxy."""
    import websockets as _ws
    import httpx as _httpx

    cmd_orig = tc._build_proxy_command("npx", "/home/me", 8808, roots=["/a"])

    class FakeWS:
        def __init__(self):
            self._msgs = [
                json.dumps({"type": "add_fs_roots", "roots": ["/b"]}),
            ]
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def __aiter__(self): return self
        async def __anext__(self):
            if not self._msgs:
                raise StopAsyncIteration
            return self._msgs.pop(0)
        async def send(self, data): pass

    class FakeHttpClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    monkeypatch.setattr(_ws, "connect", lambda url, **kw: FakeWS())
    monkeypatch.setattr(_httpx, "AsyncClient", lambda *a, **k: FakeHttpClient())

    proxy = tc.SlotProxy(list(cmd_orig), 8808, "fs")
    kills = {"n": 0}

    def fake_kill(self):
        kills["n"] += 1
    monkeypatch.setattr(tc.SlotProxy, "kill", fake_kill)

    async def fake_ensure(self):
        self._proc = _FakeProc()
        self.holder["proc"] = self._proc
    monkeypatch.setattr(tc.SlotProxy, "ensure_running", fake_ensure)

    asyncio.run(tc._run_connection_lazy("wss://x/tunnel/t", proxy, "fs"))
    assert "/b" in proxy.cmd


class _FakeProc:
    returncode = None
    def kill(self): pass
    async def wait(self): return None


# ---------------------------------------------------------------------------
# b970fe07 — GET /tunnel/filesystem-roots route: new fields in the response
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock  # noqa: E402


def _decode_route_body(resp):
    """Decode a route's JSON Response body to a dict."""
    body = resp.body
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    return json.loads(body)


def test_get_tunnel_filesystem_roots_returns_serena_and_code_dirs(monkeypatch):
    """b970fe07 — the route surfaces serena_repo_path (first non-empty) and
    codebase_code_dirs (deduped union) alongside the existing fields."""
    projects = [
        {"executor_config": json.dumps({
            "filesystem_roots": ["/root/a"],
            "repo_path": "/repo/a",
            "serena_repo_path": "/serena/a",
            "codebase_code_dirs": ["/code/a", "/code/b"],
        })},
        {"executor_config": json.dumps({
            "serena_repo_path": "/serena/b",       # second project — first wins
            "codebase_code_dirs": ["/code/b", "/code/c"],  # /code/b dedupes
        })},
    ]
    monkeypatch.setattr(tn, "_get_tenant_from_request", AsyncMock(return_value={"id": "t1"}))
    monkeypatch.setattr(tn, "_db", AsyncMock(return_value=object()))
    monkeypatch.setattr(tn.db_module, "list_projects", AsyncMock(return_value=projects))

    resp = asyncio.run(tn.get_tunnel_filesystem_roots(object()))
    data = _decode_route_body(resp)
    assert data["filesystem_roots"] == ["/root/a"]
    assert data["known_repo_paths"] == ["/repo/a"]
    assert data["serena_repo_path"] == "/serena/a"                      # first non-empty
    assert data["codebase_code_dirs"] == ["/code/a", "/code/b", "/code/c"]  # deduped union


def test_get_tunnel_filesystem_roots_defaults_when_unset(monkeypatch):
    """No project configures the new keys → empty defaults (today's behaviour)."""
    projects = [{"executor_config": json.dumps({"filesystem_roots": ["/x"]})}]
    monkeypatch.setattr(tn, "_get_tenant_from_request", AsyncMock(return_value={"id": "t1"}))
    monkeypatch.setattr(tn, "_db", AsyncMock(return_value=object()))
    monkeypatch.setattr(tn.db_module, "list_projects", AsyncMock(return_value=projects))

    data = _decode_route_body(asyncio.run(tn.get_tunnel_filesystem_roots(object())))
    assert data["serena_repo_path"] == ""
    assert data["codebase_code_dirs"] == []


def test_get_tunnel_filesystem_roots_no_tenant_returns_empty(monkeypatch):
    """Unauthenticated → all four fields default (no DB access)."""
    monkeypatch.setattr(tn, "_get_tenant_from_request", AsyncMock(return_value=None))
    data = _decode_route_body(asyncio.run(tn.get_tunnel_filesystem_roots(object())))
    assert data == {
        "filesystem_roots": [], "known_repo_paths": [],
        "serena_repo_path": "", "codebase_code_dirs": [],
    }


def test_fetch_filesystem_roots_parses_new_fields(monkeypatch):
    """b970fe07 — the client fetch returns the 4-tuple, parsing/sanitising the
    two new fields from the route JSON."""
    class _FakeResp:
        status_code = 200
        def json(self):
            return {
                "filesystem_roots": ["/a", " ", 5],
                "known_repo_paths": ["/repo"],
                "serena_repo_path": "  /serena  ",
                "codebase_code_dirs": ["/c1", "", "/c2 "],
            }

    class _FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _FakeResp()

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)
    fs, known, serena, code = asyncio.run(
        tc._fetch_filesystem_roots("https://x", "sk_tok")
    )
    assert fs == ["/a"]
    assert known == ["/repo"]
    assert serena == "/serena"
    assert code == ["/c1", "/c2"]


def test_fetch_filesystem_roots_defaults_on_error(monkeypatch):
    """Network/parse failure → 4-tuple of empty defaults (today's fallback)."""
    class _BoomClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): raise RuntimeError("network down")

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "AsyncClient", _BoomClient)
    assert asyncio.run(tc._fetch_filesystem_roots("https://x", "sk")) == ([], [], "", [])
