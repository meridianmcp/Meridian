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
# 59c0e609 — regression: the union must serve EVERY distinct configured root,
# order-preserved, deduped ONLY on exact match. The reported bug was "2 of 3
# roots served on tunnel restart" (repository + OneDrive present, an underscore
# 'Masters_Thesis/Outputs' path missing). Lock in that neither Windows path
# shapes (underscore / space / mixed parents / trailing slash) nor a
# single-project vs multi-project layout ever collapses a distinct root.
# ---------------------------------------------------------------------------

# The three real-world path shapes from the report: a repo dir, a OneDrive dir,
# and an underscore-containing dir under a different parent.
_R_REPO = r"C:\Users\13144\Documents\Meridian\repository"
_R_ONEDRIVE = r"C:\Users\13144\OneDrive\Documents"
_R_OUTPUTS = r"C:\Users\13144\Documents\Masters_Thesis\Outputs"


def test_union_filesystem_roots_keeps_all_three_single_project():
    """All 3 roots configured on ONE project → all 3 served, order-preserved."""
    projects = [
        {"executor_config": json.dumps(
            {"filesystem_roots": [_R_REPO, _R_ONEDRIVE, _R_OUTPUTS]}
        )},
    ]
    assert tn._union_filesystem_roots(projects) == [_R_REPO, _R_ONEDRIVE, _R_OUTPUTS]


def test_union_filesystem_roots_keeps_all_three_across_projects():
    """The same 3 roots spread across 3 projects → union preserves all 3.

    Guards the tenant-scoped union path (``_union_filesystem_roots`` iterating
    every project row from ``list_projects``): a per-project cap / early break /
    'first project wins' bug would drop the 2nd or 3rd here.
    """
    projects = [
        {"executor_config": json.dumps({"filesystem_roots": [_R_REPO]})},
        {"executor_config": json.dumps({"filesystem_roots": [_R_ONEDRIVE]})},
        {"executor_config": json.dumps({"filesystem_roots": [_R_OUTPUTS]})},
    ]
    assert tn._union_filesystem_roots(projects) == [_R_REPO, _R_ONEDRIVE, _R_OUTPUTS]


def test_union_filesystem_roots_dedupe_is_exact_match_only():
    """Dedup collapses ONLY byte-identical paths — a trailing slash, a differing
    drive-letter case, or an extra space makes a DISTINCT root that must survive.

    (Exact-match dedup is the correct behaviour: the union has no business
    canonicalising client-local Windows paths the server can't see.)
    """
    projects = [
        {"executor_config": json.dumps({"filesystem_roots": [
            _R_OUTPUTS,            # canonical
            _R_OUTPUTS,            # exact dup → collapses
            _R_OUTPUTS + "\\",     # trailing sep → distinct, kept
            _R_OUTPUTS.lower(),    # case variant → distinct, kept
        ]})},
    ]
    assert tn._union_filesystem_roots(projects) == [
        _R_OUTPUTS, _R_OUTPUTS + "\\", _R_OUTPUTS.lower(),
    ]


def test_union_filesystem_roots_keeps_path_with_space():
    """A root containing a space (another shape the report warned about) survives;
    only surrounding whitespace is trimmed, never internal."""
    spaced = r"D:\Research Data\Thesis Outputs"
    projects = [
        {"executor_config": json.dumps({"filesystem_roots": [
            _R_REPO, "  " + spaced + "  ", _R_OUTPUTS,
        ]})},
    ]
    assert tn._union_filesystem_roots(projects) == [_R_REPO, spaced, _R_OUTPUTS]


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


def test_unservable_roots_flags_missing_and_space(tmp_path, monkeypatch):
    """59c0e609 — configured roots the inner filesystem server would silently drop
    are surfaced with a reason, so a "served 2 of 3" is diagnosable not silent."""
    real = tmp_path / "exists"
    real.mkdir()
    missing = tmp_path / "gone"  # never created
    flagged = dict(tc._unservable_roots([str(real), str(missing), "", "  "]))
    assert str(missing) in flagged and "does not exist" in flagged[str(missing)]
    assert str(real) not in flagged  # existing dir → served, not flagged

    # On Windows an existing dir whose path contains a space is unservable
    # (mcp-proxy --shell concatenates args unescaped). os.path.isdir/str ops only
    # — no Windows-only stdlib touched, so this is safe on the Linux CI runner.
    spaced = tmp_path / "has space"
    spaced.mkdir()
    monkeypatch.setattr(tc.sys, "platform", "win32")
    flagged_win = dict(tc._unservable_roots([str(real), str(spaced)]))
    assert str(spaced) in flagged_win and "space" in flagged_win[str(spaced)]
    assert str(real) not in flagged_win  # exists + no space → served


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


# ---------------------------------------------------------------------------
# live-fs-roots — client: _set_fs_roots_on_cmd (full-list REPLACE helper)
# ---------------------------------------------------------------------------

def test_set_fs_roots_on_cmd_replaces_dirs_exactly():
    """The dirs after the fs-server token are REPLACED with exactly the new set
    (normalized, deduped, order-preserved) — a removal shrinks the served list."""
    cmd = tc._build_proxy_command("npx", "/home/me", 8808, roots=["/a", "/b", "/c"])
    updated, changed = tc._set_fs_roots_on_cmd(cmd, ["/a", "/c"])
    assert changed is True
    idx = updated.index("@modelcontextprotocol/server-filesystem")
    assert updated[idx + 1:] == ["/a", "/c"]  # /b removed, order kept


def test_set_fs_roots_on_cmd_dequotes_and_dedupes():
    """Quoted paths are de-quoted (via _normalize_path_arg) and exact dups collapse."""
    cmd = tc._build_proxy_command("npx", "/home/me", 8808, roots=["/a"])
    updated, changed = tc._set_fs_roots_on_cmd(cmd, ['"/x"', "/x", "  /y  "])
    assert changed is True
    idx = updated.index("@modelcontextprotocol/server-filesystem")
    assert updated[idx + 1:] == ["/x", "/y"]


def test_set_fs_roots_on_cmd_noop_when_unchanged():
    """Requesting the current dir set changes nothing (changed False, cmd is-same)."""
    cmd = tc._build_proxy_command("npx", "/home/me", 8808, roots=["/a", "/b"])
    updated, changed = tc._set_fs_roots_on_cmd(cmd, ["/a", "/b"])
    assert changed is False
    assert updated is cmd


def test_set_fs_roots_on_cmd_refuses_empty():
    """An empty/all-blank list is a no-op — never strip the fs server to zero dirs."""
    cmd = tc._build_proxy_command("npx", "/home/me", 8808, roots=["/a"])
    for empty in ([], ["", "  "], ['""']):
        updated, changed = tc._set_fs_roots_on_cmd(cmd, empty)
        assert changed is False
        assert updated is cmd


def test_set_fs_roots_on_cmd_no_server_token():
    """No fs-server token in the command → no change."""
    cmd = ["npx", "-y", "mcp-proxy", "--port", "8808"]
    _, changed = tc._set_fs_roots_on_cmd(cmd, ["/x"])
    assert changed is False


# ---------------------------------------------------------------------------
# live-fs-roots — client: _run_connection_lazy handles set_fs_roots
# ---------------------------------------------------------------------------

def test_run_connection_lazy_handles_set_fs_roots(monkeypatch):
    """A set_fs_roots control message REPLACES proxy.cmd's dirs with exactly the
    given roots (normalized) and kills/restarts the proxy — mirrors the
    add_fs_roots handler test but for the full-list-replace (removal) path."""
    import websockets as _ws
    import httpx as _httpx

    # Start with 3 roots served; set_fs_roots down to 2 (a removal).
    cmd_orig = tc._build_proxy_command("npx", "/home/me", 8808, roots=["/a", "/b", "/c"])

    class FakeWS:
        def __init__(self):
            self._msgs = [
                json.dumps({"type": "set_fs_roots", "roots": ['"/a"', "/c"]}),
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

    class _AliveProc(_FakeProc):
        def poll(self):
            return None  # None → is_running True

    proxy = tc.SlotProxy(list(cmd_orig), 8808, "fs")
    # Mark the proxy "running" so the handler exercises kill + respawn.
    proxy._proc = _AliveProc()
    proxy.holder["proc"] = proxy._proc
    kills = {"n": 0}

    def fake_kill(self):
        kills["n"] += 1
        self._proc = None
    monkeypatch.setattr(tc.SlotProxy, "kill", fake_kill)

    respawns = {"n": 0}

    async def fake_ensure(self):
        respawns["n"] += 1
        self._proc = _AliveProc()
        self.holder["proc"] = self._proc
    monkeypatch.setattr(tc.SlotProxy, "ensure_running", fake_ensure)

    asyncio.run(tc._run_connection_lazy("wss://x/tunnel/t", proxy, "fs"))

    idx = proxy.cmd.index("@modelcontextprotocol/server-filesystem")
    assert proxy.cmd[idx + 1:] == ["/a", "/c"]  # exactly the new set, de-quoted
    assert "/b" not in proxy.cmd                 # the removed root is gone
    assert kills["n"] == 1 and respawns["n"] == 1


# ---------------------------------------------------------------------------
# live-fs-roots — backend: send_set_fs_roots_control
# ---------------------------------------------------------------------------

def test_send_set_fs_roots_control_not_connected():
    result = asyncio.run(tn.send_set_fs_roots_control("no-tenant", ["/x"]))
    assert result["status"] == "not_connected"


def test_send_set_fs_roots_control_ok():
    ws = _FakeFsWS()
    tn._tunnel_sockets["t-set"] = ws
    try:
        result = asyncio.run(tn.send_set_fs_roots_control("t-set", ["/a", "/b"]))
        assert result == {"status": "ok", "roots": ["/a", "/b"]}
        assert ws.sent == [{"type": "set_fs_roots", "roots": ["/a", "/b"]}]
    finally:
        tn._tunnel_sockets.pop("t-set", None)


def test_send_set_fs_roots_control_send_error():
    ws = _FakeFsWS(raise_on_send=True)
    tn._tunnel_sockets["t-set-err"] = ws
    try:
        result = asyncio.run(tn.send_set_fs_roots_control("t-set-err", ["/x"]))
        assert result["status"] == "error"
    finally:
        tn._tunnel_sockets.pop("t-set-err", None)


# ---------------------------------------------------------------------------
# live-fs-roots — backend: persistence helpers (real in-memory DB round-trip)
# ---------------------------------------------------------------------------

def test_persist_add_filesystem_root_appends_and_returns_union():
    async def _run():
        db = await db_module.init_db(":memory:")
        p1 = await db_module.create_project(db, "fs-add-1")
        await db_module.set_executor_config(db, p1["id"], {"filesystem_roots": ["/a"]})
        projects = await db_module.list_projects(db)
        union = await tn._persist_add_filesystem_root(db, projects, "/b")
        # Re-read from DB to confirm it persisted (not just the in-memory copy).
        cfg = await db_module.get_executor_config(db, p1["id"])
        return union, cfg
    union, cfg = asyncio.run(_run())
    assert union == ["/a", "/b"]
    assert cfg["filesystem_roots"] == ["/a", "/b"]


def test_persist_add_filesystem_root_dedupes_existing():
    async def _run():
        db = await db_module.init_db(":memory:")
        p1 = await db_module.create_project(db, "fs-add-dup")
        await db_module.set_executor_config(db, p1["id"], {"filesystem_roots": ["/a"]})
        projects = await db_module.list_projects(db)
        union = await tn._persist_add_filesystem_root(db, projects, "/a")
        cfg = await db_module.get_executor_config(db, p1["id"])
        return union, cfg
    union, cfg = asyncio.run(_run())
    assert union == ["/a"]
    assert cfg["filesystem_roots"] == ["/a"]  # no duplicate written


def test_persist_remove_filesystem_root_strips_from_all_projects():
    async def _run():
        db = await db_module.init_db(":memory:")
        p1 = await db_module.create_project(db, "fs-rm-1")
        p2 = await db_module.create_project(db, "fs-rm-2")
        await db_module.set_executor_config(db, p1["id"], {"filesystem_roots": ["/a", "/shared"]})
        await db_module.set_executor_config(db, p2["id"], {"filesystem_roots": ["/shared", "/b"]})
        projects = await db_module.list_projects(db)
        union = await tn._persist_remove_filesystem_root(db, projects, "/shared")
        c1 = await db_module.get_executor_config(db, p1["id"])
        c2 = await db_module.get_executor_config(db, p2["id"])
        return union, c1, c2
    union, c1, c2 = asyncio.run(_run())
    assert "/shared" not in union
    assert c1["filesystem_roots"] == ["/a"]
    assert c2["filesystem_roots"] == ["/b"]


# ---------------------------------------------------------------------------
# live-fs-roots — backend: POST /tunnel/filesystem-roots
# ---------------------------------------------------------------------------

class _FakeReq:
    """Minimal Request stub: JSON body + query params for the fs-root routes."""
    def __init__(self, body=None, query=None, bad_json=False):
        self._body = body
        self._bad_json = bad_json
        self.query_params = query or {}

    async def json(self):
        if self._bad_json:
            raise ValueError("no body")
        return self._body


def _patch_route_db(monkeypatch, projects):
    """Wire _get_tenant_from_request/_db/list_projects for a route test."""
    monkeypatch.setattr(tn, "_get_tenant_from_request", AsyncMock(return_value={"id": "t1"}))
    monkeypatch.setattr(tn, "_db", AsyncMock(return_value=object()))
    monkeypatch.setattr(tn.db_module, "list_projects", AsyncMock(return_value=projects))


def test_add_route_persists_and_pushes_add_control(monkeypatch):
    """POST persists the root via _persist_add_filesystem_root AND pushes it live
    via send_add_fs_roots_control (spied). Returns the union + live status."""
    projects = [{"id": "p1", "executor_config": json.dumps({"filesystem_roots": ["/a"]})}]
    _patch_route_db(monkeypatch, projects)
    persisted = AsyncMock(return_value=["/a", "/b"])
    monkeypatch.setattr(tn, "_persist_add_filesystem_root", persisted)
    add_spy = AsyncMock(return_value={"status": "ok", "roots": ["/b"]})
    monkeypatch.setattr(tn, "send_add_fs_roots_control", add_spy)

    resp = asyncio.run(tn.add_tunnel_filesystem_root(_FakeReq(body={"path": "/b"})))
    data = _decode_route_body(resp)
    assert data["roots"] == ["/a", "/b"]
    assert data["live"] == {"status": "ok", "roots": ["/b"]}
    # persisted with the normalized path; live pushed the same single path.
    assert persisted.await_args.args[2] == "/b"
    add_spy.assert_awaited_once_with("t1", ["/b"])


def test_add_route_dequotes_path(monkeypatch):
    """A quoted path is de-quoted before persist + live push."""
    projects = [{"id": "p1", "executor_config": json.dumps({"filesystem_roots": []})}]
    _patch_route_db(monkeypatch, projects)
    persisted = AsyncMock(return_value=["/quoted/dir"])
    monkeypatch.setattr(tn, "_persist_add_filesystem_root", persisted)
    add_spy = AsyncMock(return_value={"status": "not_connected"})
    monkeypatch.setattr(tn, "send_add_fs_roots_control", add_spy)

    resp = asyncio.run(tn.add_tunnel_filesystem_root(_FakeReq(body={"path": '"/quoted/dir"'})))
    assert _decode_route_body(resp)["roots"] == ["/quoted/dir"]
    assert persisted.await_args.args[2] == "/quoted/dir"        # de-quoted for persist
    add_spy.assert_awaited_once_with("t1", ["/quoted/dir"])     # de-quoted for live push


def test_add_route_accepts_root_key(monkeypatch):
    """The body may use "root" instead of "path"."""
    projects = [{"id": "p1", "executor_config": json.dumps({"filesystem_roots": []})}]
    _patch_route_db(monkeypatch, projects)
    monkeypatch.setattr(tn, "_persist_add_filesystem_root", AsyncMock(return_value=["/via-root"]))
    monkeypatch.setattr(tn, "send_add_fs_roots_control", AsyncMock(return_value={"status": "ok"}))
    resp = asyncio.run(tn.add_tunnel_filesystem_root(_FakeReq(body={"root": "/via-root"})))
    assert _decode_route_body(resp)["roots"] == ["/via-root"]


def test_add_route_rejects_empty_path(monkeypatch):
    """An empty/blank/quoted-empty path is a 400 and never touches the DB or WS."""
    _patch_route_db(monkeypatch, [])
    persisted = AsyncMock()
    add_spy = AsyncMock()
    monkeypatch.setattr(tn, "_persist_add_filesystem_root", persisted)
    monkeypatch.setattr(tn, "send_add_fs_roots_control", add_spy)
    for bad in ({"path": ""}, {"path": "   "}, {"path": '""'}, {}):
        resp = asyncio.run(tn.add_tunnel_filesystem_root(_FakeReq(body=bad)))
        assert resp.status_code == 400
    persisted.assert_not_awaited()
    add_spy.assert_not_awaited()


def test_add_route_requires_auth(monkeypatch):
    """No tenant → 401."""
    monkeypatch.setattr(tn, "_get_tenant_from_request", AsyncMock(return_value=None))
    resp = asyncio.run(tn.add_tunnel_filesystem_root(_FakeReq(body={"path": "/x"})))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# live-fs-roots — backend: DELETE /tunnel/filesystem-roots
# ---------------------------------------------------------------------------

def test_delete_route_persists_and_pushes_set_control(monkeypatch):
    """DELETE persists the removal via _persist_remove_filesystem_root AND pushes
    the FULL new list live via send_set_fs_roots_control (spied)."""
    projects = [{"id": "p1", "executor_config": json.dumps({"filesystem_roots": ["/a", "/b"]})}]
    _patch_route_db(monkeypatch, projects)
    persisted = AsyncMock(return_value=["/a"])  # /b removed → union is ["/a"]
    monkeypatch.setattr(tn, "_persist_remove_filesystem_root", persisted)
    set_spy = AsyncMock(return_value={"status": "ok", "roots": ["/a"]})
    monkeypatch.setattr(tn, "send_set_fs_roots_control", set_spy)

    resp = asyncio.run(tn.remove_tunnel_filesystem_root(_FakeReq(body={"path": "/b"})))
    data = _decode_route_body(resp)
    assert data["roots"] == ["/a"]
    assert data["live"] == {"status": "ok", "roots": ["/a"]}
    assert persisted.await_args.args[2] == "/b"
    # set_fs_roots gets the FULL remaining list, not the removed path.
    set_spy.assert_awaited_once_with("t1", ["/a"])


def test_delete_route_reads_path_from_query(monkeypatch):
    """The path may come from ?path= (no body)."""
    projects = [{"id": "p1", "executor_config": json.dumps({"filesystem_roots": ["/a", "/b"]})}]
    _patch_route_db(monkeypatch, projects)
    persisted = AsyncMock(return_value=["/a"])
    monkeypatch.setattr(tn, "_persist_remove_filesystem_root", persisted)
    monkeypatch.setattr(tn, "send_set_fs_roots_control", AsyncMock(return_value={"status": "ok"}))
    resp = asyncio.run(
        tn.remove_tunnel_filesystem_root(_FakeReq(query={"path": "/b"}, bad_json=True))
    )
    assert _decode_route_body(resp)["roots"] == ["/a"]
    assert persisted.await_args.args[2] == "/b"


def test_delete_route_dequotes_path(monkeypatch):
    """A quoted path is de-quoted before the remove + live push."""
    projects = [{"id": "p1", "executor_config": json.dumps({"filesystem_roots": ["/keep", "/drop"]})}]
    _patch_route_db(monkeypatch, projects)
    persisted = AsyncMock(return_value=["/keep"])
    monkeypatch.setattr(tn, "_persist_remove_filesystem_root", persisted)
    set_spy = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(tn, "send_set_fs_roots_control", set_spy)
    resp = asyncio.run(tn.remove_tunnel_filesystem_root(_FakeReq(body={"path": '"/drop"'})))
    assert _decode_route_body(resp)["roots"] == ["/keep"]
    assert persisted.await_args.args[2] == "/drop"  # de-quoted
    set_spy.assert_awaited_once_with("t1", ["/keep"])


def test_delete_route_rejects_empty_path(monkeypatch):
    """No path anywhere → 400, no DB write, no WS push."""
    _patch_route_db(monkeypatch, [])
    persisted = AsyncMock()
    set_spy = AsyncMock()
    monkeypatch.setattr(tn, "_persist_remove_filesystem_root", persisted)
    monkeypatch.setattr(tn, "send_set_fs_roots_control", set_spy)
    resp = asyncio.run(tn.remove_tunnel_filesystem_root(_FakeReq(body={}, query={})))
    assert resp.status_code == 400
    persisted.assert_not_awaited()
    set_spy.assert_not_awaited()


def test_delete_route_requires_auth(monkeypatch):
    """No tenant → 401."""
    monkeypatch.setattr(tn, "_get_tenant_from_request", AsyncMock(return_value=None))
    resp = asyncio.run(tn.remove_tunnel_filesystem_root(_FakeReq(body={"path": "/x"})))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# live-fs-roots — backend: end-to-end route → real DB persistence (no mocks on
# the persist helpers), so the POST/DELETE actually round-trip through
# set_executor_config. Only the tenant/db/list_projects + control-send are wired.
# ---------------------------------------------------------------------------

def test_add_then_delete_route_roundtrip_real_db(monkeypatch):
    async def _run():
        db = await db_module.init_db(":memory:")
        p1 = await db_module.create_project(db, "fs-e2e")
        await db_module.set_executor_config(db, p1["id"], {"filesystem_roots": ["/existing"]})

        monkeypatch.setattr(tn, "_get_tenant_from_request", AsyncMock(return_value={"id": "t1"}))
        monkeypatch.setattr(tn, "_db", AsyncMock(return_value=db))
        # list_projects hits the real DB (not mocked) so the route persists for real.
        add_spy = AsyncMock(return_value={"status": "not_connected"})
        set_spy = AsyncMock(return_value={"status": "not_connected"})
        monkeypatch.setattr(tn, "send_add_fs_roots_control", add_spy)
        monkeypatch.setattr(tn, "send_set_fs_roots_control", set_spy)

        add_resp = await tn.add_tunnel_filesystem_root(_FakeReq(body={"path": "  /new/dir  "}))
        after_add = await db_module.get_executor_config(db, p1["id"])

        del_resp = await tn.remove_tunnel_filesystem_root(_FakeReq(body={"path": "/existing"}))
        after_del = await db_module.get_executor_config(db, p1["id"])
        return (_decode_route_body(add_resp), after_add,
                _decode_route_body(del_resp), after_del, add_spy, set_spy)

    add_data, after_add, del_data, after_del, add_spy, set_spy = asyncio.run(_run())
    # ADD: normalized (trimmed) path appended + persisted; union returned.
    assert add_data["roots"] == ["/existing", "/new/dir"]
    assert after_add["filesystem_roots"] == ["/existing", "/new/dir"]
    add_spy.assert_awaited_once_with("t1", ["/new/dir"])
    # DELETE: /existing stripped from the persisted config; set_fs_roots gets the
    # remaining full list.
    assert del_data["roots"] == ["/new/dir"]
    assert after_del["filesystem_roots"] == ["/new/dir"]
    set_spy.assert_awaited_once_with("t1", ["/new/dir"])
