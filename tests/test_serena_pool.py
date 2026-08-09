"""Tests for 64650cb4 — Serena daemon pool keyed by repo_path.

Process spawning and the clock are injected, so no real Serena is started.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from meridian import serena_pool as sp
from meridian.serena_pool import (
    SerenaDaemonPool,
    build_serena_command,
    ensure_serena_headless,
    is_serena_command,
    resolve_repo_path,
)

# Distinct, deterministic-enough fake OS pids for every FakeProc spawned across
# the whole module, so two sibling pools' spawned daemons never collide.
_fake_pid_counter = itertools.count(90000)


class FakeProc:
    """Minimal subprocess.Popen stand-in for the pool's spawn hook."""

    def __init__(self, cmd, pid=None):
        self.cmd = cmd
        self._alive = True
        self.terminated = False
        self.pid = pid if pid is not None else next(_fake_pid_counter)

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

def test_base_port_does_not_collide_with_any_declared_tunnel_plugin_port():
    """a1a870d5 (2026-07-19) regression: SERENA_POOL_BASE_PORT was 8820,
    IDENTICAL to tunnel_plugins.DEFAULT_OUTPUTS_PORT (also 8820), since the
    Serena pool allocates its first repo's daemon on base_port (see
    SerenaDaemonPool._next_port). Assert the base port -- and the pool's
    sequential range immediately above it -- avoids every fixed port
    tunnel_plugins declares. See also
    scripts.tunnel_smoke_test.check_port_collisions for the general
    (not Serena-specific) regression check across ALL declared ports."""
    from meridian import tunnel_plugins as tp

    fixed_ports = {
        tp.DEFAULT_FS_PORT, tp.DEFAULT_CODE_PORT, tp.DEFAULT_EXTRACT_PORT,
        tp.DEFAULT_PPT_PORT, tp.DEFAULT_WORD_PORT, tp.DEFAULT_DC_PORT,
        tp.DEFAULT_DOCS_PORT, tp.DEFAULT_ZOTERO_PORT, tp.DEFAULT_OUTPUTS_PORT,
        tp.DEFAULT_DEBUG_PORT,
        *tp.CUSTOM_SLOT_PORTS.values(),
    }
    # A generous window covering many concurrently-pooled repos, not just the
    # first daemon -- the historical bug was on base_port itself, but a range
    # check catches the same class of bug one port further along too.
    pool_range = range(sp.SERENA_POOL_BASE_PORT, sp.SERENA_POOL_BASE_PORT + 50)
    assert fixed_ports.isdisjoint(pool_range)


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


# ── canonical headless enforcement (e99b09e9) ───────────────────────────────

_PRE_HEADLESS_SERENA_CMD = [
    "uvx", "--from", "serena-agent", "serena", "start-mcp-server",
    "--context", "ide-assistant",
    "--project", "{repo_path}",
]


def test_is_serena_command_detects_regardless_of_flags():
    assert is_serena_command(_PRE_HEADLESS_SERENA_CMD) is True
    assert is_serena_command(build_serena_command("/repo", 8825)) is True


def test_is_serena_command_rejects_other_shapes():
    assert is_serena_command(["uvx", "mcp-server-code-extractor"]) is False
    assert is_serena_command(["npx", "-y", "@modelcontextprotocol/server-filesystem"]) is False
    assert is_serena_command(None) is False
    assert is_serena_command("uvx serena-agent start-mcp-server") is False  # not a list/tuple


def test_ensure_serena_headless_inserts_missing_flag():
    """A command saved before the headless flag existed (pre-344dd5e) gets it
    forced on, regardless of where it came from (stale override, hand-edit)."""
    out = ensure_serena_headless(_PRE_HEADLESS_SERENA_CMD)
    assert out[out.index("--open-web-dashboard") + 1] == "false"
    # Original input is untouched (fresh list returned).
    assert "--open-web-dashboard" not in _PRE_HEADLESS_SERENA_CMD


def test_ensure_serena_headless_fixes_wrong_value():
    cmd = [
        "uvx", "--from", "serena-agent", "serena", "start-mcp-server",
        "--open-web-dashboard", "true",
        "--project", "/x",
    ]
    out = ensure_serena_headless(cmd)
    assert out[out.index("--open-web-dashboard") + 1] == "false"


def test_ensure_serena_headless_idempotent_on_correct_command():
    cmd = build_serena_command("/repo", 8825)
    assert ensure_serena_headless(cmd) == cmd


def test_ensure_serena_headless_leaves_non_serena_commands_untouched():
    cmd = ["uvx", "mcp-server-fetch"]
    assert ensure_serena_headless(cmd) == cmd
    cmd2 = ["npx", "-y", "@wonderwhy-er/desktop-commander"]
    assert ensure_serena_headless(cmd2) == cmd2


def test_ensure_serena_headless_respects_local_opt_in_env(monkeypatch):
    """The MERIDIAN_SERENA_DASHBOARD escape hatch is off by default (forces
    headless); when set truthy on this machine, the command is left as
    authored so a developer can debug against the real dashboard."""
    monkeypatch.delenv(sp.SERENA_DASHBOARD_OPT_IN_ENV, raising=False)
    assert ensure_serena_headless(_PRE_HEADLESS_SERENA_CMD)[
        ensure_serena_headless(_PRE_HEADLESS_SERENA_CMD).index("--open-web-dashboard") + 1
    ] == "false"

    monkeypatch.setenv(sp.SERENA_DASHBOARD_OPT_IN_ENV, "1")
    out = ensure_serena_headless(_PRE_HEADLESS_SERENA_CMD)
    assert "--open-web-dashboard" not in out  # left exactly as authored


def test_build_serena_command_still_headless_by_default(monkeypatch):
    monkeypatch.delenv(sp.SERENA_DASHBOARD_OPT_IN_ENV, raising=False)
    cmd = build_serena_command("/repo/x", 8825)
    assert cmd[cmd.index("--open-web-dashboard") + 1] == "false"


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


# ── 8c6d88c9: active-project identity across default_repo_path drift ───────

def test_get_or_spawn_identity_unaffected_by_unrelated_default_drift(tmp_path):
    """get_or_spawn is keyed purely off its explicit repo_path argument, so
    mutating pool.default_repo_path (what a set_active_repo project-switch
    control message does — see tunnel_client._run_extract_pool_connection)
    must never change which daemon an already-in-flight, explicitly-repo'd
    request is routed to."""
    pool, procs, _ = _pool()
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()

    da_before = pool.get_or_spawn(str(a))

    # Project switch: default flips to repo B (mirrors the set_active_repo
    # control-message handler in tunnel_client.py).
    pool.default_repo_path = pool._normalize(str(b))

    # A caller still explicitly asking for repo A (e.g. a request resolved
    # against a per-request X-Meridian-Repo-Path header before the switch)
    # must get back the SAME daemon — identity survives the drift.
    da_after = pool.get_or_spawn(str(a))
    assert da_after is da_before
    assert len(procs) == 1  # no duplicate spawned for A just because default moved


def test_diagnostics_receipt_correctly_attributed_after_repo_switch(tmp_path):
    """After spawning daemons for two repos and switching the pool's active
    default between them, diagnostics() — the durable per-daemon record other
    code treats as a receipt of what's registered — must still attribute
    each entry's repo_path/port/pid to the correct repo, never swapped or
    merged across the switch."""
    pool, procs, _ = _pool()
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()

    da = pool.get_or_spawn(str(a))
    pool.default_repo_path = pool._normalize(str(b))  # project switch
    db = pool.get_or_spawn(str(b))

    diag = {entry["repo_path"]: entry for entry in pool.diagnostics()}
    assert set(diag) == {da.repo_path, db.repo_path}
    assert diag[da.repo_path]["port"] == da.port
    assert diag[da.repo_path]["pid"] == da.pid
    assert diag[db.repo_path]["port"] == db.port
    assert diag[db.repo_path]["pid"] == db.pid
    # The two entries must never collide — distinct ports/pids per identity.
    assert diag[da.repo_path]["port"] != diag[db.repo_path]["port"]
    assert diag[da.repo_path]["pid"] != diag[db.repo_path]["pid"]


# ── structured launch/terminate diagnostics (e99b09e9) ──────────────────────

def test_get_or_spawn_on_launch_reports_new_spawn(tmp_path, monkeypatch):
    monkeypatch.delenv(sp.SERENA_DASHBOARD_OPT_IN_ENV, raising=False)
    pool, procs, _ = _pool()
    seen = []
    d = pool.get_or_spawn(str(tmp_path), on_launch=seen.append)
    assert len(seen) == 1
    info = seen[0]
    assert info["reused"] is False
    assert info["port"] == d.port
    assert info["pid"] == d.proc.pid
    assert info["dashboard"] == "headless"
    assert isinstance(info["command_hash"], str) and len(info["command_hash"]) == 12
    assert Path(info["repo_path"]) == Path(str(tmp_path)).resolve()


def test_get_or_spawn_on_launch_reports_reuse(tmp_path):
    pool, procs, _ = _pool()
    seen = []
    d1 = pool.get_or_spawn(str(tmp_path))
    d2 = pool.get_or_spawn(str(tmp_path), on_launch=seen.append)
    assert d2 is d1
    assert len(seen) == 1
    assert seen[0]["reused"] is True
    assert seen[0]["pid"] == d1.proc.pid


def test_get_or_spawn_on_launch_never_leaks_raw_command(tmp_path):
    """Diagnostics carry a hash, never the literal command tokens (a
    tenant-customized command_builder could embed a pasted secret)."""
    pool, procs, _ = _pool()
    seen = []
    pool.get_or_spawn(str(tmp_path), on_launch=seen.append)
    assert set(seen[0].keys()) == {
        "repo_path", "port", "pid", "reused", "dashboard", "command_hash",
    }


def test_get_or_spawn_on_launch_dashboard_field_reflects_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv(sp.SERENA_DASHBOARD_OPT_IN_ENV, "true")
    pool, procs, _ = _pool()
    seen = []
    pool.get_or_spawn(str(tmp_path), on_launch=seen.append)
    assert seen[0]["dashboard"] == "gui"


def test_get_or_spawn_without_on_launch_unchanged_behavior(tmp_path):
    """on_launch is fully optional — omitting it changes nothing."""
    pool, procs, _ = _pool()
    d = pool.get_or_spawn(str(tmp_path))
    assert d.is_alive
    assert len(procs) == 1


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


def test_reap_idle_on_terminate_reports_idle_timeout_reason(tmp_path):
    pool, procs, clock = _pool(idle_kill_seconds=600)
    a = tmp_path / "a"; a.mkdir()
    da = pool.get_or_spawn(str(a))
    clock.advance(700)
    seen = []
    pool.reap_idle(on_terminate=seen.append)
    assert len(seen) == 1
    assert seen[0] == {
        "repo_path": da.repo_path, "port": da.port,
        "pid": da.proc.pid, "reason": "idle_timeout",
    }


def test_shutdown_kills_all(tmp_path):
    pool, procs, _ = _pool()
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    pool.get_or_spawn(str(a))
    pool.get_or_spawn(str(b))
    pool.shutdown()
    assert len(pool) == 0
    assert all(p.terminated for p in procs)


def test_shutdown_on_terminate_reports_tunnel_shutdown_reason(tmp_path):
    pool, procs, _ = _pool()
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    da = pool.get_or_spawn(str(a))
    db = pool.get_or_spawn(str(b))
    seen = []
    pool.shutdown(on_terminate=seen.append)
    assert {info["repo_path"] for info in seen} == {da.repo_path, db.repo_path}
    assert all(info["reason"] == "tunnel_shutdown" for info in seen)
    assert {info["pid"] for info in seen} == {da.proc.pid, db.proc.pid}


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


# ── host-local broker (92aaedb7) ────────────────────────────────────────────

def _broker_pool(tmp_path, owner_id, self_pid, liveness, **kw):
    """Pool variant wired for the host-local broker. Two pools built via this
    helper against the same tmp_path share a broker_dir, so they behave like
    two sibling tunnel_client processes on one host. *liveness* is a plain
    dict of ``{pid: bool}``; any pid not present defaults to "alive" (the
    common case — most tests only need to mark specific pids as dead)."""
    procs = []

    def spawn(cmd):
        p = FakeProc(cmd)
        procs.append(p)
        return p

    clock = Clock()
    pool = SerenaDaemonPool(
        spawn=spawn, now=clock,
        broker_dir=tmp_path / "broker",
        owner_id=owner_id,
        pid_alive=lambda pid: liveness.get(pid, True),
        self_pid=lambda: self_pid,
        **kw,
    )
    return pool, procs, clock


def test_config_fingerprint_stable_and_sensitive_to_command():
    a = sp.config_fingerprint(["uvx", "serena", "--project", "/x"])
    b = sp.config_fingerprint(["uvx", "serena", "--project", "/x"])
    c = sp.config_fingerprint(["uvx", "serena", "--project", "/y"])
    assert a == b
    assert a != c


def test_broker_disabled_by_default_no_filesystem_touched(tmp_path, monkeypatch):
    """broker_dir defaults to None: get_or_spawn/reap_idle/shutdown must never
    touch the filesystem unless a caller explicitly opts in."""
    def _boom(*a, **k):
        raise AssertionError("filesystem touched with broker disabled")
    monkeypatch.setattr(sp, "_write_json", _boom)
    monkeypatch.setattr(sp, "_read_json", _boom)
    pool, procs, clock = _pool()
    d = pool.get_or_spawn(str(tmp_path))
    clock.advance(sp.IDLE_KILL_SECONDS + 1)
    pool.reap_idle()
    assert d.owned is True


def test_sibling_pool_adopts_instead_of_spawning_duplicate(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    liveness = {}
    pool_a, procs_a, _ = _broker_pool(tmp_path, "pool-a", 111, liveness)
    pool_b, procs_b, _ = _broker_pool(tmp_path, "pool-b", 222, liveness)

    da = pool_a.get_or_spawn(str(repo))
    assert len(procs_a) == 1

    db = pool_b.get_or_spawn(str(repo))
    assert len(procs_b) == 0  # adopted -- did not spawn a duplicate
    assert db is not da
    assert db.owned is False
    assert db.owner_id == "pool-b"
    assert db.port == da.port
    assert db.pid == da.pid


def test_adoption_refused_on_config_fingerprint_mismatch(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    liveness = {}
    pool_a, procs_a, _ = _broker_pool(tmp_path, "pool-a", 111, liveness)

    def other_builder(repo_path, port):
        return build_serena_command(repo_path, port) + ["--extra-flag"]

    pool_b, procs_b, _ = _broker_pool(
        tmp_path, "pool-b", 222, liveness, command_builder=other_builder,
    )

    pool_a.get_or_spawn(str(repo))
    db = pool_b.get_or_spawn(str(repo))

    assert len(procs_b) == 1  # config drift -- spawned its own, did not adopt
    assert db.owned is True


def test_adoption_skips_dead_daemon_and_cleans_up_descriptor(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    liveness = {}
    pool_a, procs_a, _ = _broker_pool(tmp_path, "pool-a", 111, liveness)
    da = pool_a.get_or_spawn(str(repo))
    liveness[da.pid] = False  # the daemon process has crashed

    pool_b, procs_b, _ = _broker_pool(tmp_path, "pool-b", 222, liveness)
    db = pool_b.get_or_spawn(str(repo))

    assert len(procs_b) == 1  # could not adopt a dead daemon -- spawned fresh
    assert db.owned is True
    key_dir = pool_b._key_dir(pool_b._normalize(str(repo)))
    descriptor = json.loads((key_dir / "daemon.json").read_text())
    assert descriptor["pid"] == db.pid  # stale descriptor replaced, not left dangling


def test_next_port_avoids_sibling_pools_live_ports(tmp_path):
    liveness = {}
    pool_a, procs_a, _ = _broker_pool(tmp_path, "pool-a", 111, liveness, base_port=9500)
    pool_b, procs_b, _ = _broker_pool(tmp_path, "pool-b", 222, liveness, base_port=9500)

    repo_x = tmp_path / "x"; repo_x.mkdir()
    repo_y = tmp_path / "y"; repo_y.mkdir()

    da = pool_a.get_or_spawn(str(repo_x))
    db = pool_b.get_or_spawn(str(repo_y))  # a DIFFERENT repo -- must not collide

    assert da.port == 9500
    assert db.port != 9500
    assert db.owned is True


def test_owned_spawn_writes_descriptor_and_lease_files(tmp_path):
    liveness = {}
    pool, procs, _ = _broker_pool(tmp_path, "pool-a", 111, liveness)
    repo = tmp_path / "repo"; repo.mkdir()
    d = pool.get_or_spawn(str(repo))

    key_dir = pool._key_dir(pool._normalize(str(repo)))
    descriptor = json.loads((key_dir / "daemon.json").read_text())
    assert descriptor["pid"] == d.pid
    assert descriptor["port"] == d.port
    assert descriptor["config_fingerprint"] == d.config_fingerprint
    assert descriptor["start_time"] == d.start_time

    lease = json.loads((key_dir / "lease-pool-a.json").read_text())
    assert lease["owner_id"] == "pool-a"
    assert lease["tunnel_pid"] == 111


def test_reap_idle_releases_but_spares_daemon_still_leased_by_sibling(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    liveness = {}
    pool_a, procs_a, clock_a = _broker_pool(
        tmp_path, "pool-a", 111, liveness, idle_kill_seconds=600,
    )
    pool_b, procs_b, _ = _broker_pool(
        tmp_path, "pool-b", 222, liveness, idle_kill_seconds=600,
    )

    da = pool_a.get_or_spawn(str(repo))
    pool_b.get_or_spawn(str(repo))  # pool_b adopts -- now leasing it too

    clock_a.advance(700)
    reaped = pool_a.reap_idle()

    assert da.repo_path in reaped     # pool_a released its own tracking
    assert len(pool_a) == 0
    assert da.proc.terminated is False  # NOT killed -- pool_b still leases it


def test_last_lessee_terminates_daemon_by_pid_after_spawner_crashes(tmp_path):
    """Crash cleanup: pool_a spawned the daemon and then its tunnel process
    died without releasing its lease. pool_b (which adopted) is the only
    remaining live lessee, so when IT releases, it must terminate the
    daemon by pid (it never held a Popen handle for it)."""
    repo = tmp_path / "repo"; repo.mkdir()
    liveness = {}
    pool_a, procs_a, _ = _broker_pool(tmp_path, "pool-a", 111, liveness)
    da = pool_a.get_or_spawn(str(repo))

    pool_b, procs_b, clock_b = _broker_pool(
        tmp_path, "pool-b", 222, liveness, idle_kill_seconds=600,
    )
    db = pool_b.get_or_spawn(str(repo))
    assert db.owned is False

    # pool_a's tunnel process crashes; the daemon itself (a detached child)
    # is untouched and stays alive.
    liveness[111] = False

    killed = {}
    pool_b._terminate_by_pid = lambda pid: killed.setdefault("pid", pid)

    clock_b.advance(700)
    reaped = pool_b.reap_idle()

    assert db.repo_path in reaped
    assert killed["pid"] == da.pid


def test_shutdown_terminates_when_sole_claimant(tmp_path):
    liveness = {}
    pool, procs, _ = _broker_pool(tmp_path, "pool-a", 111, liveness)
    repo = tmp_path / "repo"; repo.mkdir()
    d = pool.get_or_spawn(str(repo))

    pool.shutdown()

    assert d.proc.terminated is True
    assert len(pool) == 0
    key_dir = pool._key_dir(pool._normalize(str(repo)))
    assert not (key_dir / "daemon.json").exists()


def test_quarantine_threshold_and_cooldown(tmp_path):
    pool, _, clock = _broker_pool(tmp_path, "pool-a", 111, {})
    key = "some/repo"
    assert pool._is_quarantined(key, clock.t) is False
    for _ in range(sp.QUARANTINE_AFTER_FAILURES):
        pool._note_adopt_failure(key, clock.t)
    assert pool._is_quarantined(key, clock.t) is True
    clock.advance(sp.QUARANTINE_COOLDOWN_SECONDS + 1)
    assert pool._is_quarantined(key, clock.t) is False


def test_quarantined_key_spawns_fresh_instead_of_adopting(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    liveness = {}
    pool_a, procs_a, _ = _broker_pool(tmp_path, "pool-a", 111, liveness)
    pool_a.get_or_spawn(str(repo))  # writes a perfectly valid, live descriptor

    pool_b, procs_b, clock_b = _broker_pool(tmp_path, "pool-b", 222, liveness)
    key = pool_b._normalize(str(repo))
    for _ in range(sp.QUARANTINE_AFTER_FAILURES):
        pool_b._note_adopt_failure(key, clock_b.t)

    db = pool_b.get_or_spawn(str(repo))

    assert len(procs_b) == 1  # quarantined -- skipped adoption, spawned fresh
    assert db.owned is True


def test_max_daemons_evicts_least_recently_used_owned_daemon(tmp_path):
    pool, procs, clock = _pool(max_daemons=2)
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    c = tmp_path / "c"; c.mkdir()
    da = pool.get_or_spawn(str(a))
    clock.advance(10)
    db = pool.get_or_spawn(str(b))
    clock.advance(10)
    dc = pool.get_or_spawn(str(c))

    assert len(pool) == 2
    assert da.proc.terminated is True   # least-recently-used evicted
    assert db.proc.terminated is False
    assert dc.proc.terminated is False


def test_diagnostics_reports_full_fields(tmp_path):
    pool, procs, clock = _pool()
    d = pool.get_or_spawn(str(tmp_path))
    diag = pool.diagnostics()

    assert len(diag) == 1
    entry = diag[0]
    assert entry["repo_path"] == d.repo_path
    assert entry["port"] == d.port
    assert entry["pid"] == d.pid
    assert entry["owned"] is True
    assert entry["health"] == sp.HEALTH_HEALTHY
    assert entry["quarantined"] is False
    assert entry["config_fingerprint"] == d.config_fingerprint
    assert entry["start_time"] == d.start_time
    assert entry["idle_seconds"] == 0


def test_diagnostics_reports_unhealthy_for_dead_daemon(tmp_path):
    pool, procs, clock = _pool()
    d = pool.get_or_spawn(str(tmp_path))
    d.proc._alive = False
    diag = pool.diagnostics()
    assert diag[0]["health"] == sp.HEALTH_UNHEALTHY


def test_has_live_lease_true_only_for_matching_pid_with_live_lease(tmp_path):
    broker = tmp_path / "broker"
    key_dir = broker / "abc123"
    key_dir.mkdir(parents=True)
    (key_dir / "daemon.json").write_text(json.dumps({"pid": 4242, "port": 8700}))
    (key_dir / "lease-x.json").write_text(json.dumps({"owner_id": "x", "tunnel_pid": 555}))

    assert sp.has_live_lease(broker, 4242, pid_alive=lambda pid: pid == 555) is True
    assert sp.has_live_lease(broker, 4242, pid_alive=lambda pid: False) is False
    assert sp.has_live_lease(broker, 9999, pid_alive=lambda pid: True) is False
    assert sp.has_live_lease(broker / "missing", 1, pid_alive=lambda pid: True) is False


# ── host-local memory/CPU budgets (9c8336c4) ────────────────────────────────


def _breaching_sampler(pid):
    """Fake psutil.Process double whose memory_info() always reports a
    current_bytes value that exceeds any test budget below."""
    class _Mem:
        rss = 10 * 1024 * 1024 * 1024  # 10 GiB -- comfortably over budget

    class _Proc:
        def memory_info(self):
            return _Mem()

        def cpu_percent(self, interval=None):
            return 0.0

    return _Proc()


def _healthy_sampler(pid):
    class _Mem:
        rss = 100 * 1024 * 1024  # 100 MiB -- comfortably under budget

    class _Proc:
        def memory_info(self):
            return _Mem()

        def cpu_percent(self, interval=None):
            return 1.0

    return _Proc()


def test_check_budgets_disabled_is_a_no_op(tmp_path):
    pool, procs, clock = _pool()
    pool.get_or_spawn(str(tmp_path))
    reports = pool.check_budgets(sp._process_budget.ProcessBudget(enabled=False), sampler=_breaching_sampler)
    assert reports == []
    assert procs[0].terminated is False


def test_check_budgets_within_budget_takes_no_action(tmp_path):
    pool, procs, clock = _pool()
    pool.get_or_spawn(str(tmp_path))
    cfg = sp._process_budget.ProcessBudget(enabled=True, max_memory_bytes=1024 * 1024 * 1024)
    reports = pool.check_budgets(cfg, sampler=_healthy_sampler)
    assert len(reports) == 1
    assert reports[0].action == "none"
    assert procs[0].terminated is False


def test_check_budgets_quiesces_then_terminates_owned_daemon(tmp_path):
    pool, procs, clock = _pool()
    pool.get_or_spawn(str(tmp_path))
    cfg = sp._process_budget.ProcessBudget(enabled=True, max_memory_bytes=1024 * 1024 * 1024)

    r1 = pool.check_budgets(cfg, sampler=_breaching_sampler)
    assert r1[0].action == "quiesce"
    assert procs[0].terminated is False
    assert len(pool) == 1  # not removed yet

    r2 = pool.check_budgets(cfg, sampler=_breaching_sampler)
    assert r2[0].action == "kill"
    assert procs[0].terminated is True
    assert len(pool) == 0  # removed from local tracking


def test_check_budgets_never_touches_adopted_leased_daemon(tmp_path):
    """An adopted (not-owned) daemon belongs to a sibling pool's own
    registry -- this pool must never throttle/terminate it, matching the
    sprint's "only processes proven owned ... may be throttled" rule."""
    repo = tmp_path / "repo"
    repo.mkdir()
    liveness = {}
    pool_a, procs_a, _ = _broker_pool(tmp_path, "pool-a", 111, liveness)
    da = pool_a.get_or_spawn(str(repo))

    pool_b, procs_b, _ = _broker_pool(tmp_path, "pool-b", 222, liveness)
    db = pool_b.get_or_spawn(str(repo))
    assert db.owned is False

    cfg = sp._process_budget.ProcessBudget(enabled=True, max_memory_bytes=1024 * 1024 * 1024)
    # Two calls -- enough to escalate to "kill" for an OWNED daemon -- must
    # produce zero reports against pool_b, since it owns nothing.
    assert pool_b.check_budgets(cfg, sampler=_breaching_sampler) == []
    assert pool_b.check_budgets(cfg, sampler=_breaching_sampler) == []
    assert da.proc.terminated is False  # pool_a's real daemon left untouched


def test_check_budgets_on_terminate_callback_fires_with_reason():
    pool, procs, clock = _pool()
    pool.get_or_spawn("/some/repo")
    cfg = sp._process_budget.ProcessBudget(enabled=True, max_memory_bytes=1024 * 1024 * 1024)
    pool.check_budgets(cfg, sampler=_breaching_sampler)  # quiesce

    seen = []
    pool.check_budgets(cfg, sampler=_breaching_sampler, on_terminate=lambda info: seen.append(info))
    assert len(seen) == 1
    assert seen[0]["reason"] == "budget_exceeded"
    assert seen[0]["pid"] == procs[0].pid


def test_check_budgets_spares_daemon_still_leased_by_sibling(tmp_path):
    """A budget breach on a daemon a sibling pool still leases must not kill
    it out from under that sibling -- same "leased daemons are never pulled
    out" contract as reap_idle/_release_daemon."""
    repo = tmp_path / "repo"
    repo.mkdir()
    liveness = {}
    pool_a, procs_a, _ = _broker_pool(tmp_path, "pool-a", 111, liveness)
    da = pool_a.get_or_spawn(str(repo))
    pool_b, procs_b, _ = _broker_pool(tmp_path, "pool-b", 222, liveness)
    pool_b.get_or_spawn(str(repo))  # pool_b adopts -- now leasing it too

    cfg = sp._process_budget.ProcessBudget(enabled=True, max_memory_bytes=1024 * 1024 * 1024)
    pool_a.check_budgets(cfg, sampler=_breaching_sampler)  # quiesce
    pool_a.check_budgets(cfg, sampler=_breaching_sampler)  # kill attempt

    assert da.proc.terminated is False  # pool_b's live lease saved it
    assert len(pool_a) == 0  # pool_a released its own local tracking regardless
