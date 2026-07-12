"""3295c784 — shared (cross-Fly-instance) rate-limit counter.

Covers the four required behaviours:
  1. increment_rate_counter returns monotonically increasing counts across calls
     (the atomic upsert is the shared source of truth).
  2. The flag-OFF path is byte-for-byte the existing per-process limiter — it
     never touches the DB counter table.
  3. The flag-ON path 429s a tenant once the SHARED count exceeds the plan budget.
  4. The over-limit per-process cache short-circuits a known-blocked tenant so a
     second request in the same window does NOT do a DB round-trip.
"""
from __future__ import annotations

import types

import pytest

import meridian.db as db_module
import meridian.server as srv
from meridian._deps import _reset_tenant_rate_limit


# ---------------------------------------------------------------------------
# 1. Atomic increment returns increasing counts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_increment_rate_counter_returns_increasing_counts(db):
    """Successive increments in the same window return 1, 2, 3, …; a different
    window starts its own count; a different tenant is independent."""
    window = 100
    assert await db_module.increment_rate_counter(db, "tenant-a", window) == 1
    assert await db_module.increment_rate_counter(db, "tenant-a", window) == 2
    assert await db_module.increment_rate_counter(db, "tenant-a", window) == 3

    # New window → fresh count.
    assert await db_module.increment_rate_counter(db, "tenant-a", window + 1) == 1

    # Different tenant, same window → independent count.
    assert await db_module.increment_rate_counter(db, "tenant-b", window) == 1
    assert await db_module.increment_rate_counter(db, "tenant-b", window) == 2


@pytest.mark.asyncio
async def test_prune_rate_counters_drops_old_windows(db):
    """prune_rate_counters removes windows strictly older than the cutoff and
    leaves current/newer ones intact."""
    await db_module.increment_rate_counter(db, "t", 10)
    await db_module.increment_rate_counter(db, "t", 11)
    await db_module.increment_rate_counter(db, "t", 12)

    await db_module.prune_rate_counters(db, 12)  # drop window < 12 → 10 and 11 go

    async with db.execute(
        "SELECT window_start FROM mcp_rate_counters ORDER BY window_start"
    ) as cur:
        rows = await cur.fetchall()
    remaining = [r["window_start"] for r in rows]
    assert remaining == [12]


# ---------------------------------------------------------------------------
# Shared harness for the decision-path tests
# ---------------------------------------------------------------------------

def _fake_req(db, token="tok", path="/mcp"):
    return types.SimpleNamespace(
        headers={"authorization": f"Bearer {token}"},
        url=types.SimpleNamespace(path=path),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db)),
    )


def _install_fake_tenant(monkeypatch, plan="free", tid="tenant-free"):
    async def _fake_tenant(_auth_db, _token_hash):
        return {"id": tid, "plan": plan}
    monkeypatch.setattr(srv, "_hosted_mode", lambda: True)
    monkeypatch.setattr(db_module, "get_tenant_from_token_hash", _fake_tenant)


# ---------------------------------------------------------------------------
# 2. Flag OFF → unchanged per-process path, never writes the shared counter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_off_uses_per_process_and_leaves_counter_empty(db, monkeypatch):
    """With MERIDIAN_SHARED_RATE_LIMIT unset the decision uses the per-process
    window (blocks at the 4th of a patched budget-3 free plan) and NEVER writes
    a row into mcp_rate_counters."""
    monkeypatch.delenv("MERIDIAN_SHARED_RATE_LIMIT", raising=False)
    _install_fake_tenant(monkeypatch, plan="free", tid="tenant-free")
    monkeypatch.setitem(srv._TENANT_RL_PER_MINUTE, "free", 3)
    _reset_tenant_rate_limit()

    for _ in range(3):
        assert await srv._tenant_rate_limit_decision(_fake_req(db)) is None
    blocked = await srv._tenant_rate_limit_decision(_fake_req(db))
    assert getattr(blocked, "status_code", None) == 429

    # The shared-counter table must be untouched on the flag-off path.
    async with db.execute("SELECT COUNT(*) AS n FROM mcp_rate_counters") as cur:
        row = await cur.fetchone()
    assert row["n"] == 0


# ---------------------------------------------------------------------------
# 3. Flag ON → 429 once the SHARED count exceeds the plan budget
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_on_blocks_once_shared_count_exceeds_budget(db, monkeypatch):
    """With the flag on, exactly `budget` requests pass and the next 429s — and
    the block is driven by the row in mcp_rate_counters (the shared count)."""
    monkeypatch.setenv("MERIDIAN_SHARED_RATE_LIMIT", "1")
    _install_fake_tenant(monkeypatch, plan="free", tid="tenant-shared")
    monkeypatch.setitem(srv._TENANT_RL_PER_MINUTE, "free", 3)
    _reset_tenant_rate_limit()

    for _ in range(3):
        assert await srv._tenant_rate_limit_decision(_fake_req(db)) is None
    blocked = await srv._tenant_rate_limit_decision(_fake_req(db))
    assert getattr(blocked, "status_code", None) == 429

    # The shared count reflects every request (4 increments: 3 allowed + 1 over).
    async with db.execute(
        "SELECT count FROM mcp_rate_counters WHERE tenant_id = ?",
        ("tenant-shared",),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["count"] >= 4


@pytest.mark.asyncio
async def test_flag_on_shared_count_is_cross_instance(db, monkeypatch):
    """Two 'instances' (two requests sharing one DB but with the process-local
    over-limit cache cleared between them, simulating separate machines) agree on
    a single shared count: pre-loading the DB counter to the budget makes the
    very next request from a 'fresh' instance 429 immediately."""
    monkeypatch.setenv("MERIDIAN_SHARED_RATE_LIMIT", "1")
    _install_fake_tenant(monkeypatch, plan="free", tid="tenant-x")
    monkeypatch.setitem(srv._TENANT_RL_PER_MINUTE, "free", 5)
    _reset_tenant_rate_limit()

    import time as _t
    window = int(_t.time() // 60)
    # Simulate 5 requests already served by OTHER Fly instances this window.
    for _ in range(5):
        await db_module.increment_rate_counter(db, "tenant-x", window)

    # A "fresh" instance (empty per-process cache) sees the shared count and the
    # first request it handles pushes the total to 6 > 5 → blocked.
    blocked = await srv._tenant_rate_limit_decision(_fake_req(db, token="tok-x"))
    assert getattr(blocked, "status_code", None) == 429


# ---------------------------------------------------------------------------
# 4. Over-limit per-process cache short-circuits (no second DB round-trip)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_over_limit_cache_short_circuits_without_db(db, monkeypatch):
    """Once a tenant is known over budget this window, the next request in the
    same window is refused from the process-local cache WITHOUT hitting the DB
    increment helper again."""
    monkeypatch.setenv("MERIDIAN_SHARED_RATE_LIMIT", "1")
    _install_fake_tenant(monkeypatch, plan="free", tid="tenant-cache")
    monkeypatch.setitem(srv._TENANT_RL_PER_MINUTE, "free", 2)
    _reset_tenant_rate_limit()

    # Drive it over the budget (2 pass, 3rd blocks and populates the cache).
    for _ in range(2):
        assert await srv._tenant_rate_limit_decision(_fake_req(db)) is None
    first_block = await srv._tenant_rate_limit_decision(_fake_req(db))
    assert getattr(first_block, "status_code", None) == 429

    # Now spy on the DB helper: a further request in the same window must be
    # refused from the cache alone, never calling increment_rate_counter.
    calls = {"n": 0}
    real_increment = db_module.increment_rate_counter

    async def _counting_increment(*args, **kwargs):
        calls["n"] += 1
        return await real_increment(*args, **kwargs)

    monkeypatch.setattr(db_module, "increment_rate_counter", _counting_increment)

    second_block = await srv._tenant_rate_limit_decision(_fake_req(db))
    assert getattr(second_block, "status_code", None) == 429
    assert calls["n"] == 0  # short-circuited: no DB round-trip
