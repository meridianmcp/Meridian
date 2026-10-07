"""Synthetic noisy-tenant drill for sprint item 1b2fbebe.

This test file validates the routing/guardrail LOGIC of Meridian's
pool telemetry classifier and recommender using a discrete-event
simulator, NOT real Neon contention. A real staging drill against a
disposable Neon pool project is a separate, owner-gated step; see
docs/infra-neon-tenant-pool-isolation.md.

The simulator models multiple shared-endpoint pools, each with
parallel slots, and feeds simulated per-request latencies into the
real TenantPoolMeter. Tests verify that:
- Baseline (normal load) produces no hot classification
- A single flooder inflates its co-tenants' latency while leaving
  other pools unaffected
- The recommender correctly identifies the flooder and proposes
  isolation
- Following the recommendation restores co-tenants
- Alerts deduplicate within cooldown windows
- Telemetry stays redacted and bounded
"""

from __future__ import annotations

import json

from meridian.pool_telemetry import (
    AlertLedger,
    LoadThresholds,
    TenantPoolMeter,
    new_advisories,
    pool_label,
    tenant_label,
)


class Clock:
    """Simulated monotonic clock."""

    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class Pool:
    """Simulated Neon endpoint with parallel slots."""

    def __init__(self, name: str, servers: int = 2, service_s: float = 0.4):
        self.name = name
        self.servers = servers
        self.service_s = service_s
        self.free_at = [0.0] * servers

    def serve(self, arrival_t: float) -> float:
        """
        Serve a request arriving at arrival_t.
        Returns latency (finish_t - arrival_t).
        Updates slot availability.
        """
        slot_idx = min(range(self.servers), key=lambda i: self.free_at[i])
        start_t = max(arrival_t, self.free_at[slot_idx])
        finish_t = start_t + self.service_s
        self.free_at[slot_idx] = finish_t
        return finish_t - arrival_t


class Sim:
    """Discrete-event simulator for pools and tenants.

    Simulated time is absolute and carries over between ``run`` calls (each call continues
    where the last stopped), so the meter's one-minute buckets and the pools' backlog both
    see one continuous timeline.
    """

    def __init__(self, clock: Clock):
        self.clock = clock
        self.pools: dict[str, Pool] = {}
        self.placement: dict[str, str] = {}  # tenant_id -> pool_name
        self.now = 0.0
        self._acc: dict[str, float] = {}
        self.meter = TenantPoolMeter(thresholds=LoadThresholds(), clock=clock)

    def add_pool(self, name: str, servers: int = 2, service_s: float = 0.4):
        self.pools[name] = Pool(name, servers, service_s)

    def place_tenant(self, tenant_id: str, pool_name: str):
        self.placement[tenant_id] = pool_name

    def run(self, seconds: int, rates: dict[str, float]):
        """Advance simulated time in 1-second ticks. ``rates`` maps tenant -> requests/s.

        Each tenant keeps a fractional accumulator (deterministic, no randomness); the
        arrivals of a tick are spread evenly inside it. A tenant's accumulator starts at a
        small per-tenant phase offset so equal-rate tenants do not all arrive together.
        """
        tenants = sorted(rates)
        for i, tenant_id in enumerate(tenants):
            self._acc.setdefault(tenant_id, (i / len(tenants)) * 0.5)

        for _ in range(seconds):
            tick_start = self.now
            arrivals: list[tuple[float, str]] = []
            for tenant_id in tenants:
                rate = rates[tenant_id]
                if rate <= 0 or self.placement.get(tenant_id) not in self.pools:
                    continue
                self._acc[tenant_id] += rate
                n = int(self._acc[tenant_id])
                self._acc[tenant_id] -= n
                arrivals.extend((tick_start + (k + 0.5) / n, tenant_id) for k in range(n))

            # Serve in arrival order, as a real shared endpoint would.
            for arrival_t, tenant_id in sorted(arrivals):
                pool_name = self.placement[tenant_id]
                latency_s = self.pools[pool_name].serve(arrival_t)
                self.clock.t = arrival_t
                self.meter.record(tenant_id, pool_name, duration_ms=latency_s * 1000.0)

            self.now += 1.0
            self.clock.t = self.now


# ---------------------------------------------------------------------------
# Scenario helpers
# ---------------------------------------------------------------------------

# Requests per second. Pool capacity is servers / service_s = 5 req/s; the flooder alone
# (7 req/s) exceeds it, every normal tenant sends 6 requests a minute.
NORMAL = {**{f"a{i}": 0.1 for i in range(1, 7)}, **{f"b{i}": 0.1 for i in range(1, 3)}}
FLOOD = {**NORMAL, "a1": 7.0}

MINUTE = 60


def _topology(*extra_pools: str) -> tuple[Clock, Sim]:
    """Pool A holds a1..a6, pool B holds b1..b2; extra pools start empty."""
    clock = Clock()
    sim = Sim(clock)
    for name in ("A", "B", *extra_pools):
        sim.add_pool(name)
    for i in range(1, 7):
        sim.place_tenant(f"a{i}", "A")
    for i in range(1, 3):
        sim.place_tenant(f"b{i}", "B")
    return clock, sim


def _pool_p95(sim: Sim, name: str) -> float | None:
    return next((w.p95_ms for w in sim.meter.pool_windows() if w.pool == pool_label(name)), None)


def _tenant_p95(sim: Sim, name: str) -> float | None:
    window = sim.meter.tenant_window(tenant_label(name))
    return window.p95_ms if window else None


def _baseline() -> tuple[Clock, Sim, dict]:
    """Six quiet minutes: the reference every later relationship is measured against."""
    clock, sim = _topology("C")
    sim.run(6 * MINUTE, NORMAL)
    base = {
        "pool_a": _pool_p95(sim, "A"),
        "pool_b": _pool_p95(sim, "B"),
        "a2": _tenant_p95(sim, "a2"),
        "b1": _tenant_p95(sim, "b1"),
    }
    assert all(v is not None for v in base.values())
    return clock, sim, base


def _flooded() -> tuple[Clock, Sim, dict]:
    clock, sim, base = _baseline()
    sim.run(12 * MINUTE, FLOOD)
    return clock, sim, base


# ---------------------------------------------------------------------------
# The drill
# ---------------------------------------------------------------------------


def test_baseline_nothing_is_hot():
    _, sim, base = _baseline()
    sim.run(10 * MINUTE, NORMAL)  # 16 quiet minutes in all
    assert "hot" not in sim.meter.classify_all().values()
    assert sim.meter.recommendations() == []
    noisy = sim.meter._th.noisy_p95_ms
    assert all(w.p95_ms < noisy for w in sim.meter.pool_windows())


def test_flood_inflates_only_the_co_tenants_of_the_same_pool():
    _, sim, base = _flooded()
    noisy = sim.meter._th.noisy_p95_ms

    # Pool A: the flooder's pool-mates are slowed far beyond their own baseline...
    assert _pool_p95(sim, "A") >= 5 * base["pool_a"]
    assert _pool_p95(sim, "A") >= noisy
    assert _tenant_p95(sim, "a2") >= noisy
    # ...while tenants of a DIFFERENT pool do not notice at all.
    assert _pool_p95(sim, "B") <= 1.5 * base["pool_b"]
    assert _tenant_p95(sim, "b1") <= 1.5 * base["b1"]
    assert _tenant_p95(sim, "b1") < noisy


def test_flood_is_classified_hot_and_only_the_flooder():
    _, sim, _ = _flooded()
    states = sim.meter.classify_all()
    assert states[tenant_label("a1")] == "hot"
    assert [name for name in NORMAL if name != "a1" and states[tenant_label(name)] == "hot"] == []


def test_recommendation_is_a_dedicated_project_for_the_flooder_only():
    _, sim, _ = _flooded()
    (rec,) = sim.meter.recommendations()
    assert rec.tenant == tenant_label("a1")
    assert rec.action == "dedicated_project"
    assert rec.reason == "hot_with_noisy_neighbours"
    assert rec.advisory is True
    assert rec.co_tenants == 5  # a2..a6, the ones with traffic
    assert rec.pool == pool_label("A")
    assert rec.co_tenant_p95_ms >= sim.meter._th.noisy_p95_ms


def test_a_short_burst_is_not_hot():
    _, sim, _ = _baseline()
    sim.run(3 * MINUTE, FLOOD)   # shorter than the sustain window
    sim.run(14 * MINUTE, NORMAL)
    assert sim.meter.classify_all()[tenant_label("a1")] != "hot"
    assert sim.meter.recommendations() == []


def test_following_the_recommendation_restores_the_co_tenants():
    _, sim, base = _flooded()
    noisy = sim.meter._th.noisy_p95_ms
    assert _tenant_p95(sim, "a2") >= noisy  # the damage the recommendation answers

    sim.placement["a1"] = "C"  # apply the (advisory) recommendation: a dedicated pool
    sim.run(20 * MINUTE, FLOOD)  # the flooder keeps flooding, now only itself

    # Former co-tenants are back to their own baseline; the other pool never moved.
    for name in ("a2", "a3", "a4", "a5", "a6"):
        assert _tenant_p95(sim, name) <= 1.5 * base["a2"], name
    assert _pool_p95(sim, "A") < noisy
    assert _pool_p95(sim, "B") <= 1.5 * base["pool_b"]

    # The flooder is still hot, but there is nobody left to protect: no further move.
    assert sim.meter.classify_all()[tenant_label("a1")] == "hot"
    (rec,) = sim.meter.recommendations()
    assert (rec.action, rec.reason) == ("stay_pooled", "hot_alone")
    assert rec.pool == pool_label("C")
    assert sim.meter.tenant_window(tenant_label("a1")).pool == pool_label("C")


def test_new_advisories_alert_once_per_cooldown():
    clock, sim, _ = _flooded()
    ledger = AlertLedger(cooldown_seconds=300.0, clock=clock)

    first = new_advisories(meter=sim.meter, ledger=ledger)
    assert [r.action for r in first] == ["dedicated_project"]
    assert new_advisories(meter=sim.meter, ledger=ledger) == []  # inside the cooldown

    clock.t += 301.0  # past the cooldown, still inside the 15-minute meter window
    assert len(new_advisories(meter=sim.meter, ledger=ledger)) == 1


def test_telemetry_stays_bounded_and_redacted_during_the_drill():
    clock, sim, _ = _flooded()
    snapshot = json.dumps(sim.meter.snapshot())

    for raw in list(NORMAL) + ["A", "B"]:
        assert f'"{raw}"' not in snapshot, f"raw id {raw!r} leaked"
    assert "@" not in snapshot
    assert sim.meter.snapshot()["tracked_tenants"] <= 8

    bounded = TenantPoolMeter(thresholds=LoadThresholds(), max_tenants=4, clock=clock)
    for name in NORMAL:
        bounded.record(name, "A", duration_ms=50.0)
    assert bounded.snapshot()["tracked_tenants"] == 4
    assert bounded.evicted_tenants == len(NORMAL) - 4
