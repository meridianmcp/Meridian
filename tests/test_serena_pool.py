"""Tests for 64650cb4 — Serena daemon pool keyed by repo_path.

Process spawning and the clock are injected, so no real Serena is started.
"""
from __future__ import annotations

import pytest

from meridian import serena_pool as sp
from meridian.serena_pool import SerenaDaemonPool, build_serena_command, resolve_repo_path


class FakeProc:
    """Minimal subprocess.Popen stand-in for the pool's spawn hook."""

    def __init__(self, cmd):
        self.cmd = cmd
        self._alive = True
        self.terminated = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._alive = False


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


def _pool(**kw):
    procs = []

    def spawn(cmd):
        p = FakeProc(cmd)
        procs.append(p)
        return p

    clock = Clock()
    pool = SerenaDaemonPool(spawn=spawn, now=clock, **kw)
    return pool, procs, clock


# ── command + header helpers ────────────────────────────────────────────────

def test_build_serena_command_has_transport_and_project():
    cmd = build_serena_command("/repo/x", 8825)
    assert "--transport" in cmd and "streamable-http" in cmd
    assert cmd[cmd.index("--port") + 1] == "8825"
    assert cmd[cmd.index("--project") + 1] == "/repo/x"
    assert cmd[cmd.index("--open-web-dashboard") + 1] == "false"


def test_build_serena_command_uses_claude_code_context():
    # Regression: was "ide-assistant" (deprecated); must be "claude-code".
    cmd = build_serena_command("/repo/x", 8825)
    assert "--context" in cmd
    assert cmd[cmd.index("--context") + 1] == "claude-code"


def test_resolve_repo_path_header_case_insensitive():
    assert resolve_repo_path({"X-Meridian-Repo-Path": "/a"}, "/def") == "/a"
    assert resolve_repo_path({"x-meridian-repo-path": "/b"}, "/def") == "/b"
    assert resolve_repo_path({}, "/def") == "/def"
    assert resolve_repo_path({"x-meridian-repo-path": "  "}, "/def") == "/def"
    assert resolve_repo_path(None, "/def") == "/def"


# ── pool spawn / reuse / routing ────────────────────────────────────────────

def test_get_or_spawn_spawns_once_and_reuses(tmp_path):
    pool, procs, clock = _pool()
    d1 = pool.get_or_spawn(str(tmp_path))
    assert len(procs) == 1
    assert d1.is_alive
    clock.advance(5)
    d2 = pool.get_or_spawn(str(tmp_path))
    assert d2 is d1  # reused, no new process
    assert len(procs) == 1
    assert d2.last_used == clock.t  # touched on reuse


def test_distinct_repo_paths_get_distinct_daemons(tmp_path):
    pool, procs, _ = _pool()
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    da = pool.get_or_spawn(str(a))
    db = pool.get_or_spawn(str(b))
    assert da is not db
    assert da.port != db.port
    assert len(procs) == 2
    assert len(pool) == 2


def test_repo_path_normalization_collapses_spellings(tmp_path):
    pool, procs, _ = _pool()
    sub = tmp_path / "x"; sub.mkdir()
    pool.get_or_spawn(str(sub))
    # Same path via a './x/.' style spelling resolves to the same daemon.
    pool.get_or_spawn(str(tmp_path / "x" / "." ))
    assert len(procs) == 1
    assert len(pool) == 1


def test_dead_daemon_respawns_on_same_port(tmp_path):
    pool, procs, _ = _pool()
    d1 = pool.get_or_spawn(str(tmp_path))
    port = d1.port
    d1.proc._alive = False  # simulate crash
    d2 = pool.get_or_spawn(str(tmp_path))
    assert d2 is not d1
    assert d2.port == port  # reused the dead daemon's port
    assert len(procs) == 2


def test_next_port_skips_used(tmp_path):
    pool, _, _ = _pool(base_port=9000)
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    da = pool.get_or_spawn(str(a))
    db = pool.get_or_spawn(str(b))
    assert {da.port, db.port} == {9000, 9001}


def test_daemon_for_does_not_spawn(tmp_path):
    pool, procs, _ = _pool()
    assert pool.daemon_for(str(tmp_path)) is None
    assert len(procs) == 0
    pool.get_or_spawn(str(tmp_path))
    assert pool.daemon_for(str(tmp_path)) is not None


# ── idle reaping + shutdown ─────────────────────────────────────────────────

def test_reap_idle_kills_only_idle(tmp_path):
    pool, procs, clock = _pool(idle_kill_seconds=600)
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    da = pool.get_or_spawn(str(a))
    clock.advance(700)               # a is now 700s idle
    db = pool.get_or_spawn(str(b))   # b just spawned (fresh)
    reaped = pool.reap_idle()
    assert da.repo_path in reaped
    assert db.repo_path not in reaped
    assert len(pool) == 1
    assert da.proc.terminated is True


def test_shutdown_kills_all(tmp_path):
    pool, procs, _ = _pool()
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    pool.get_or_spawn(str(a))
    pool.get_or_spawn(str(b))
    pool.shutdown()
    assert len(pool) == 0
    assert all(p.terminated for p in procs)


def test_idle_seconds_and_touch(tmp_path):
    pool, _, clock = _pool()
    d = pool.get_or_spawn(str(tmp_path))
    clock.advance(120)
    assert d.idle_seconds(clock.t) == 120
    d.touch(clock.t)
    assert d.idle_seconds(clock.t) == 0


# ── relay integration (tunnel_client) ───────────────────────────────────────

@pytest.mark.asyncio
async def test_extract_pool_connection_routes_by_header(monkeypatch, tmp_path):
    """The pooled extract relay routes a request to the repo's daemon port."""
    from meridian import tunnel_client as tc

    pool, procs, _ = _pool()

    captured = {}

    async def fake_relay(http_client, local_base, msg, tool_prefix=None):
        captured["local_base"] = local_base
        return {"type": "response", "id": msg.get("id"), "status": 200, "headers": {}, "body": ""}

    monkeypatch.setattr(tc, "_relay_request", fake_relay)

    # Pre-flight is incidental to this routing test — keep the slot healthy so
    # it doesn't emit an extra plugin_status message (d71ba2e7).
    async def _healthy(*a, **k):
        return True
    monkeypatch.setattr(tc, "_probe_slot_health", _healthy)

    repo = tmp_path / "myrepo"; repo.mkdir()

    # Fake websocket: yields one request then stops.
    sent = []

    class FakeWS:
        def __init__(self):
            self._msgs = [
                '{"type":"request","id":"r1","method":"POST","path":"/mcp",'
                '"headers":{"X-Meridian-Repo-Path":"' + str(repo).replace("\\", "/") + '"}}'
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def __aiter__(self):
            async def gen():
                for m in self._msgs:
                    yield m
            return gen()

        async def send(self, data):
            sent.append(data)

    import meridian.tunnel_client as tcmod

    class FakeConnect:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return FakeWS()

        async def __aexit__(self, *a):
            return False

    fake_ws_mod = type("M", (), {"connect": lambda *a, **k: FakeConnect()})
    fake_httpx_mod = type("H", (), {"AsyncClient": lambda *a, **k: _AsyncCtxNoop()})
    monkeypatch.setitem(__import__("sys").modules, "websockets", fake_ws_mod)
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx_mod)

    await tcmod._run_extract_pool_connection("ws://x", pool, str(tmp_path), "extract")

    # A daemon was spawned for the header repo and the relay targeted its port.
    daemon = pool.daemon_for(str(repo))
    assert daemon is not None
    assert captured["local_base"] == f"http://127.0.0.1:{daemon.port}"
    assert len(sent) == 1


class _AsyncCtxNoop:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False
