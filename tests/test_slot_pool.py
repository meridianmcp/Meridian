"""Tests for 39aae23f — elastic backend-copy pool behind a stateless tunnel slot.

Covers the pure pool primitive (meridian/slot_pool.py): least-busy / round-robin
dispatch, elastic min/max sizing + burst, idle scale-down, and the config-layer
helpers in meridian/tunnel_plugins.py (slot_pool_config + pool override
normalization). The process spawner, terminator, and clock are all injected, so
no real subprocess or socket is ever created.
"""
from __future__ import annotations

import pytest

from meridian import slot_pool as sp
from meridian.slot_pool import (
    SlotPool,
    BackendCopy,
    resolve_pool_size,
    DISPATCH_LEAST_BUSY,
    DISPATCH_ROUND_ROBIN,
    DEFAULT_MIN_COPIES,
    DEFAULT_MAX_COPIES,
)
from meridian import tunnel_plugins as tp


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
    def __init__(self, t0=1000.0):
        self.t = t0

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
    pool = SlotPool(
        command_builder=lambda port: ["mcp-proxy", "--port", str(port)],
        base_port=9200,
        spawn=spawn,
        now=clock,
        **kw,
    )
    return pool, procs, clock


# ── resolve_pool_size (config coercion) ─────────────────────────────────────

def test_resolve_pool_size_defaults_on_garbage():
    assert resolve_pool_size(None) == (DEFAULT_MIN_COPIES, DEFAULT_MAX_COPIES)
    assert resolve_pool_size("nonsense") == (DEFAULT_MIN_COPIES, DEFAULT_MAX_COPIES)
    assert resolve_pool_size([1, 2]) == (DEFAULT_MIN_COPIES, DEFAULT_MAX_COPIES)
    # bool is NOT a valid size spec (would otherwise be an int).
    assert resolve_pool_size(True) == (DEFAULT_MIN_COPIES, DEFAULT_MAX_COPIES)


def test_resolve_pool_size_int_shorthand():
    assert resolve_pool_size(4) == (1, 4)
    # An int below 1 still floors at 1/1.
    assert resolve_pool_size(0) == (1, 1)


def test_resolve_pool_size_dict_forms_and_aliases():
    assert resolve_pool_size({"min": 2, "max": 5}) == (2, 5)
    assert resolve_pool_size({"min_copies": 3, "max_copies": 6}) == (3, 6)


def test_resolve_pool_size_clamps_inverted_and_floors_min():
    # max < min → max raised to min.
    assert resolve_pool_size({"min": 5, "max": 2}) == (5, 5)
    # min floors at 1.
    assert resolve_pool_size({"min": 0, "max": 3}) == (1, 3)


# ── lazy spawn + ensure_min ─────────────────────────────────────────────────

def test_no_copies_until_used():
    pool, procs, _ = _pool()
    assert len(pool) == 0
    assert len(procs) == 0


def test_ensure_min_spawns_min_copies():
    pool, procs, _ = _pool(min_copies=2, max_copies=3)
    pool.ensure_min()
    assert len(pool) == 2
    assert len(procs) == 2
    assert pool.ports == [9200, 9201]


def test_acquire_spawns_first_copy_lazily():
    pool, procs, _ = _pool(min_copies=1, max_copies=2)
    copy = pool.acquire()
    assert len(procs) == 1
    assert copy.port == 9200
    assert copy.inflight == 1
    assert copy.total_served == 1


# ── elastic burst (up to max) ───────────────────────────────────────────────

def test_burst_spawns_second_copy_when_first_busy():
    pool, procs, _ = _pool(min_copies=1, max_copies=2)
    c1 = pool.acquire()          # spawns copy #1, now busy
    c2 = pool.acquire()          # copy #1 busy → burst copy #2
    assert c1.port != c2.port
    assert len(pool) == 2
    assert len(procs) == 2


def test_burst_capped_at_max():
    pool, procs, _ = _pool(min_copies=1, max_copies=2)
    pool.acquire()
    pool.acquire()               # now at max (2), both busy
    c3 = pool.acquire()          # cannot burst past max → reuse a busy copy
    assert len(pool) == 2
    assert len(procs) == 2
    # The third request piled onto one of the two existing copies.
    assert c3.port in (9200, 9201)


def test_no_burst_when_idle_capacity_exists():
    pool, procs, _ = _pool(min_copies=1, max_copies=3)
    c1 = pool.acquire()
    pool.release(c1)             # copy #1 now idle
    c2 = pool.acquire()          # idle capacity exists → reuse, don't burst
    assert c2 is c1
    assert len(pool) == 1
    assert len(procs) == 1


# ── least-busy dispatch ─────────────────────────────────────────────────────

def test_least_busy_prefers_idle_copy():
    pool, _, _ = _pool(min_copies=1, max_copies=3, dispatch=DISPATCH_LEAST_BUSY)
    c1 = pool.acquire()          # copy #1 (inflight 1)
    c2 = pool.acquire()          # burst copy #2 (inflight 1)
    pool.release(c1)             # copy #1 now inflight 0
    c3 = pool.acquire()          # least-busy → copy #1 (0 inflight)
    assert c3 is c1
    assert c1.inflight == 1
    assert c2.inflight == 1


def test_least_busy_tie_breaks_on_total_served():
    pool, _, _ = _pool(min_copies=2, max_copies=2, dispatch=DISPATCH_LEAST_BUSY)
    pool.ensure_min()            # two idle copies
    a = pool.acquire()           # picks the less-served/lowest-port copy
    pool.release(a)
    b = pool.acquire()           # a has served 1 → tie-break sends this to the other
    assert b.port != a.port


# ── round-robin dispatch ────────────────────────────────────────────────────

def test_round_robin_cycles_copies():
    pool, _, _ = _pool(min_copies=3, max_copies=3, dispatch=DISPATCH_ROUND_ROBIN)
    pool.ensure_min()
    ports = []
    for _ in range(6):
        c = pool.acquire()
        ports.append(c.port)
        pool.release(c)          # release so no burst, pure rotation
    # Two full sweeps over the three copies, in order.
    assert ports == [9200, 9201, 9202, 9200, 9201, 9202]


def test_invalid_dispatch_falls_back_to_least_busy():
    pool, _, _ = _pool(dispatch="bogus")
    assert pool.dispatch == DISPATCH_LEAST_BUSY


# ── release accounting ──────────────────────────────────────────────────────

def test_release_floors_at_zero():
    pool, _, _ = _pool()
    c = pool.acquire()
    pool.release(c)
    pool.release(c)              # double release must not go negative
    assert c.inflight == 0


def test_release_none_is_noop():
    pool, _, _ = _pool()
    pool.release(None)           # must not raise
    assert len(pool) == 0


def test_total_inflight_sums_across_copies():
    pool, _, _ = _pool(min_copies=1, max_copies=3)
    pool.acquire()
    pool.acquire()
    assert pool.total_inflight == 2


# ── dead-copy pruning ───────────────────────────────────────────────────────

def test_dead_copy_pruned_and_respawned():
    pool, procs, _ = _pool(min_copies=1, max_copies=2)
    c1 = pool.acquire()
    c1.proc._alive = False       # simulate the copy's process crashing
    c2 = pool.acquire()          # dead copy pruned, min re-established → new copy
    assert c2 is not c1
    assert len(pool) == 1
    assert len(procs) == 2


# ── idle scale-down ─────────────────────────────────────────────────────────

def test_reap_idle_scales_back_to_min():
    pool, procs, clock = _pool(min_copies=1, max_copies=3, idle_scale_down_seconds=600)
    c1 = pool.acquire()
    c2 = pool.acquire()          # burst
    c3 = pool.acquire()          # burst
    pool.release(c1); pool.release(c2); pool.release(c3)
    assert len(pool) == 3
    clock.advance(700)           # all idle past TTL
    reaped = pool.reap_idle()
    # Scaled back down to min_copies (1); two extras reaped.
    assert len(pool) == 1
    assert len(reaped) == 2


def test_reap_idle_never_below_min():
    pool, _, clock = _pool(min_copies=2, max_copies=4, idle_scale_down_seconds=600)
    pool.ensure_min()            # two copies
    clock.advance(700)
    reaped = pool.reap_idle()
    assert reaped == []
    assert len(pool) == 2


def test_reap_idle_skips_busy_copies():
    pool, _, clock = _pool(min_copies=1, max_copies=3, idle_scale_down_seconds=600)
    c1 = pool.acquire()          # stays busy (never released)
    c2 = pool.acquire()          # burst
    pool.release(c2)
    clock.advance(700)
    reaped = pool.reap_idle()
    # c1 is busy → not reaped even though above min; c2 idle above min → reaped.
    assert c2.port in reaped
    assert c1.port not in reaped
    assert len(pool) == 1


def test_reap_idle_leaves_fresh_copies():
    pool, _, clock = _pool(min_copies=1, max_copies=3, idle_scale_down_seconds=600)
    pool.acquire()
    c2 = pool.acquire()          # burst, still in-flight
    pool.release(c2)
    clock.advance(100)           # NOT past the 600s TTL
    assert pool.reap_idle() == []
    assert len(pool) == 2


# ── shutdown ────────────────────────────────────────────────────────────────

def test_shutdown_terminates_all():
    pool, procs, _ = _pool(min_copies=2, max_copies=2)
    pool.ensure_min()
    pool.shutdown()
    assert len(pool) == 0
    assert all(p.terminated for p in procs)


def test_backend_copy_is_alive_handles_bad_handle():
    # A None proc or a poll() that raises both count as dead, never raise.
    assert BackendCopy(1, None, 0.0).is_alive is False

    class Boom:
        def poll(self):
            raise RuntimeError("boom")

    assert BackendCopy(1, Boom(), 0.0).is_alive is False


# ── next_port allocation ────────────────────────────────────────────────────

def test_next_port_skips_bound_ports():
    pool, _, _ = _pool(min_copies=3, max_copies=3)
    pool.ensure_min()
    assert pool.ports == [9200, 9201, 9202]


# ── config layer: tunnel_plugins.slot_pool_config ───────────────────────────

def test_slot_pool_config_default_stateless_slot_pools():
    # A resolved built-in stateless slot with no override → elastic 1..2 default.
    fs = tp.plugin_by_slot(None, "fs")
    cfg = tp.slot_pool_config(fs)
    assert cfg == {"enabled": True, "min": 1, "max": 2}


def test_slot_pool_config_persistent_slot_never_pools():
    # Desktop Commander (dc) is session_mode="persistent" → always single-copy,
    # even if a config tries to force a pool.
    dc = tp.plugin_by_slot({"desktop-commander": {"pool": {"max": 4}}}, "dc")
    assert dc["session_mode"] == "persistent"
    assert tp.slot_pool_config(dc) == {"enabled": False, "min": 1, "max": 1}


def test_slot_pool_config_honors_override_on_stateless_slot():
    plugins = tp.resolve_plugins({"code-intel": {"pool": {"min": 2, "max": 4}}})
    code = next(p for p in plugins if p["slot"] == "code")
    assert tp.slot_pool_config(code) == {"enabled": True, "min": 2, "max": 4}


def test_slot_pool_config_int_shorthand_override():
    plugins = tp.resolve_plugins({"filesystem": {"pool": 3}})
    fs = next(p for p in plugins if p["slot"] == "fs")
    assert tp.slot_pool_config(fs) == {"enabled": True, "min": 1, "max": 3}


def test_slot_pool_config_disabled_collapses_to_single():
    plugins = tp.resolve_plugins({"filesystem": {"pool": {"enabled": False}}})
    fs = next(p for p in plugins if p["slot"] == "fs")
    assert tp.slot_pool_config(fs) == {"enabled": False, "min": 1, "max": 1}
    # max<=1 also collapses (nothing to load-balance).
    plugins2 = tp.resolve_plugins({"filesystem": {"pool": {"max": 1}}})
    fs2 = next(p for p in plugins2 if p["slot"] == "fs")
    assert tp.slot_pool_config(fs2) == {"enabled": False, "min": 1, "max": 1}


def test_slot_pool_config_clamps_inverted_override():
    plugins = tp.resolve_plugins({"word": {"enabled": True, "pool": {"min": 5, "max": 2}}})
    word = next(p for p in plugins if p["slot"] == "word")
    assert tp.slot_pool_config(word) == {"enabled": True, "min": 5, "max": 5}


def test_slot_pool_config_non_dict_returns_single():
    assert tp.slot_pool_config(None) == {"enabled": False, "min": 1, "max": 1}
    assert tp.slot_pool_config("nope") == {"enabled": False, "min": 1, "max": 1}


def test_normalize_pool_override_shapes():
    # int shorthand → {"max": N}
    assert tp._normalize_pool_override(3) == {"max": 3}
    # bool → enabled toggle
    assert tp._normalize_pool_override(False) == {"enabled": False}
    # dict with aliases
    assert tp._normalize_pool_override({"min_copies": 2, "max_copies": 5}) == {"min": 2, "max": 5}
    # garbage → None (keep default)
    assert tp._normalize_pool_override("x") is None
    assert tp._normalize_pool_override([]) is None


def test_pool_override_round_trips_through_normalize_config():
    cfg = tp.normalize_plugins_config({"code-intel": {"pool": {"min": 2, "max": 3}}})
    assert cfg["code-intel"]["pool"] == {"min": 2, "max": 3}


def test_pool_override_flows_through_resolve_plugins():
    plugins = tp.resolve_plugins([{"name": "filesystem", "pool": 4}])
    fs = next(p for p in plugins if p["slot"] == "fs")
    # resolve_plugins carried the normalized pool override onto the resolved slot.
    assert fs["pool"] == {"max": 4}
