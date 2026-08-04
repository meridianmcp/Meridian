"""Tests for 64650cb4 — Serena daemon pool keyed by repo_path.

Process spawning and the clock are injected, so no real Serena is started.
"""
from __future__ import annotations

import itertools
import json

import pytest

from meridian import serena_pool as sp
from meridian.serena_pool import SerenaDaemonPool, build_serena_command, resolve_repo_path

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
