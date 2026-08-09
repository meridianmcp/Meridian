"""8d52b620 — PROFILE-4: revision-keyed Redis read-through cache for profile
snapshots, effective-profile projections, capability/tool manifests, and
handoff/config projections.

Tests cover:
  - ProfileCacheKey validation: allowed namespaces only; forbidden
    (claims/leases/pointers/writes/completion state) namespaces always
    raise; missing/blank required fields raise; schema/resolver version
    coercion.
  - Cache miss -> authority called once, value cached, telemetry recorded.
  - Cache hit -> authority NOT called, value served from Redis, telemetry
    recorded (including the measured Neon-avoidance ratio).
  - A generation bump (new generation_key) is a plain cache miss by default
    (allow_stale_seconds=0) -> falls all the way through to authority,
    never silently serving stale data.
  - Opt-in bounded-staleness fallback: with allow_stale_seconds > 0 and a
    fresh "latest known good" pointer, a generation-miss becomes a
    stale_hit with ZERO authority calls.
  - A "latest known good" pointer older than allow_stale_seconds is not
    served — falls through to authority instead.
  - Redis unavailable (get_redis_client() -> None) falls back safely to
    authority (bypass_redis_unavailable), never raises.
  - A mid-flight Redis GET exception falls back safely to authority
    (bypass_error), never raises.
  - Per-tenant budget exhaustion (reusing redis_bridge's OWN
    redis_commands_used counter/tiers, not a new budget) skips Redis
    entirely and falls back to authority (bypass_budget_exhausted); under
    budget, the shared counter is incremented on every Redis command issued.
  - invalidate() deletes both the exact-generation key and the latest-known
    pointer; a subsequent stale-hit lookup after invalidate finds nothing.
  - TTLs are always bounded: an oversized ttl_seconds is clamped to
    MAX_TTL_SECONDS; a non-positive ttl_seconds is clamped to 1.
  - Malformed cached JSON is treated as a miss (never raises).
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from meridian import db as db_module
from meridian import profile_cache
from meridian import redis_bridge
from meridian.profile_cache import (
    ProfileCacheError,
    ProfileCacheKey,
    get_or_fetch,
    get_profile_cache_telemetry,
    invalidate,
    reset_profile_cache_telemetry,
)


# ---------------------------------------------------------------------------
# Fake Redis client
# ---------------------------------------------------------------------------


class _FakeRedisClient:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}
        self.get_calls = 0
        self.set_calls = 0
        self.delete_calls = 0
        self.fail_get = False
        self.fail_set = False

    async def get(self, key: str):
        self.get_calls += 1
        if self.fail_get:
            raise RuntimeError("simulated Redis GET failure")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.set_calls += 1
        if self.fail_set:
            raise RuntimeError("simulated Redis SET failure")
        self.store[key] = value
        self.ttls[key] = ex
        return True

    async def delete(self, key: str):
        self.delete_calls += 1
        existed = key in self.store
        self.store.pop(key, None)
        self.ttls.pop(key, None)
        return 1 if existed else 0


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    redis_bridge.reset_redis_client_cache()
    reset_profile_cache_telemetry()
    monkeypatch.delenv("MERIDIAN_REDIS_URL", raising=False)
    yield
    redis_bridge.reset_redis_client_cache()
    reset_profile_cache_telemetry()


def _key(**overrides) -> ProfileCacheKey:
    fields = dict(
        namespace="effective_profile",
        scope_type="project",
        scope_id="proj-1",
        profile_id="profile-1",
        generation_key="gen-abc",
        schema_version=1,
        resolver_version=1,
    )
    fields.update(overrides)
    return ProfileCacheKey(**fields)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# ProfileCacheKey validation
# ---------------------------------------------------------------------------


def test_forbidden_namespace_always_rejected():
    for bad in ("claims", "leases", "pointers", "writes", "completion_state", "sprint_items"):
        with pytest.raises(ProfileCacheError):
            _key(namespace=bad)


def test_unknown_namespace_rejected():
    with pytest.raises(ProfileCacheError):
        _key(namespace="something_else_entirely")


def test_allowed_namespaces_accepted():
    for ns in (
        "profile_snapshot", "effective_profile", "capability_manifest",
        "tool_manifest", "handoff_projection",
    ):
        k = _key(namespace=ns)
        assert k.namespace == ns


@pytest.mark.parametrize("field_name", ["scope_type", "scope_id", "profile_id", "generation_key"])
def test_blank_required_field_rejected(field_name):
    with pytest.raises(ProfileCacheError):
        _key(**{field_name: "   "})


def test_non_integer_versions_rejected():
    with pytest.raises(ProfileCacheError):
        _key(schema_version="not-an-int")


def test_zero_or_negative_versions_rejected():
    with pytest.raises(ProfileCacheError):
        _key(schema_version=0)
    with pytest.raises(ProfileCacheError):
        _key(resolver_version=-1)


def test_redis_key_includes_all_freshness_coordinates():
    k = _key()
    rk = k.redis_key()
    assert "effective_profile" in rk
    assert "project" in rk and "proj-1" in rk
    assert "profile-1" in rk
    assert "gen-abc" in rk
    assert "v1" in rk and "r1" in rk


def test_latest_pointer_key_excludes_generation():
    k1 = _key(generation_key="gen-A")
    k2 = _key(generation_key="gen-B")
    assert k1.redis_key() != k2.redis_key()
    assert k1.latest_pointer_key() == k2.latest_pointer_key()


# ---------------------------------------------------------------------------
# Cache miss / hit
# ---------------------------------------------------------------------------


def test_miss_calls_authority_once_and_populates_cache(monkeypatch):
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    calls = {"n": 0}

    async def _authority():
        calls["n"] += 1
        return {"resolved": True}

    async def _go():
        key = _key()
        result = await get_or_fetch(key, _authority)
        assert result.outcome == "miss"
        assert result.source == "authority"
        assert result.value == {"resolved": True}
        assert calls["n"] == 1
        assert fake.store.get(key.redis_key()) is not None

        telemetry = get_profile_cache_telemetry()
        assert telemetry["misses"] == 1
        assert telemetry["hits"] == 0
        assert telemetry["authority_calls"] == 1
        assert telemetry["authority_calls_avoided"] == 0

    _run(_go())


def test_hit_never_calls_authority(monkeypatch):
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    calls = {"n": 0}

    async def _authority():
        calls["n"] += 1
        return {"resolved": True, "call": calls["n"]}

    async def _go():
        key = _key()
        first = await get_or_fetch(key, _authority)
        assert first.outcome == "miss"
        second = await get_or_fetch(key, _authority)
        assert second.outcome == "hit"
        assert second.source == "cache"
        assert second.value == {"resolved": True, "call": 1}
        assert calls["n"] == 1, "authority must not be called again on a cache hit"

        telemetry = get_profile_cache_telemetry()
        assert telemetry["hits"] == 1
        assert telemetry["misses"] == 1
        assert telemetry["authority_calls"] == 1
        assert telemetry["authority_calls_avoided"] == 1
        assert telemetry["neon_avoidance_ratio"] == pytest.approx(0.5)

    _run(_go())


def test_generation_bump_is_plain_miss_by_default(monkeypatch):
    """A new generation_key must NOT reuse the old cached entry, and by
    default (allow_stale_seconds=0) must NOT serve a stale fallback either —
    it always goes all the way through to authority."""
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    calls = {"n": 0}

    async def _authority():
        calls["n"] += 1
        return {"gen": calls["n"]}

    async def _go():
        key_v1 = _key(generation_key="gen-1")
        r1 = await get_or_fetch(key_v1, _authority)
        assert r1.outcome == "miss"
        assert r1.value == {"gen": 1}

        key_v2 = _key(generation_key="gen-2")
        r2 = await get_or_fetch(key_v2, _authority)
        assert r2.outcome == "miss", "a new generation must never be served from the old cache entry"
        assert r2.value == {"gen": 2}
        assert calls["n"] == 2

    _run(_go())


# ---------------------------------------------------------------------------
# Opt-in bounded-staleness fallback
# ---------------------------------------------------------------------------


def test_stale_hit_when_within_grace_window(monkeypatch):
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    calls = {"n": 0}

    async def _authority():
        calls["n"] += 1
        return {"gen": calls["n"]}

    async def _go():
        key_v1 = _key(generation_key="gen-1")
        await get_or_fetch(key_v1, _authority)
        assert calls["n"] == 1

        key_v2 = _key(generation_key="gen-2")
        result = await get_or_fetch(key_v2, _authority, allow_stale_seconds=3600)
        assert result.outcome == "stale_hit"
        assert result.stale is True
        assert result.source == "stale_cache"
        assert result.value == {"gen": 1}, "stale fallback must serve the last known good value"
        assert result.cache_generation_key == "gen-1"
        assert result.requested_generation_key == "gen-2"
        assert calls["n"] == 1, "authority must not be called when a valid stale entry is served"

        telemetry = get_profile_cache_telemetry()
        assert telemetry["stale_hits"] == 1
        assert telemetry["authority_calls_avoided"] == 1

    _run(_go())


def test_stale_fallback_not_served_when_older_than_grace_window(monkeypatch):
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    calls = {"n": 0}

    async def _authority():
        calls["n"] += 1
        return {"gen": calls["n"]}

    async def _go():
        key_v1 = _key(generation_key="gen-1")
        await get_or_fetch(key_v1, _authority)

        # Manually age the "latest known good" pointer past the grace window.
        pointer_key = key_v1.latest_pointer_key()
        payload = json.loads(fake.store[pointer_key])
        payload["stored_at"] = time.time() - 999999
        fake.store[pointer_key] = json.dumps(payload)

        key_v2 = _key(generation_key="gen-2")
        result = await get_or_fetch(key_v2, _authority, allow_stale_seconds=10)
        assert result.outcome == "miss"
        assert calls["n"] == 2, "an expired stale pointer must fall through to authority"

    _run(_go())


# ---------------------------------------------------------------------------
# Redis outage / runtime errors
# ---------------------------------------------------------------------------


def test_redis_unavailable_falls_back_to_authority(monkeypatch):
    async def _fake_get_client():
        return None

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    calls = {"n": 0}

    async def _authority():
        calls["n"] += 1
        return {"resolved": True}

    async def _go():
        result = await get_or_fetch(_key(), _authority)
        assert result.outcome == "bypass_redis_unavailable"
        assert result.value == {"resolved": True}
        assert calls["n"] == 1

        telemetry = get_profile_cache_telemetry()
        assert telemetry["fallback_redis_unavailable"] == 1

    _run(_go())


def test_redis_get_error_falls_back_to_authority_without_raising(monkeypatch):
    fake = _FakeRedisClient()
    fake.fail_get = True

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    calls = {"n": 0}

    async def _authority():
        calls["n"] += 1
        return {"resolved": True}

    async def _go():
        result = await get_or_fetch(_key(), _authority)
        assert result.outcome == "bypass_error"
        assert result.value == {"resolved": True}
        assert calls["n"] == 1

        telemetry = get_profile_cache_telemetry()
        assert telemetry["fallback_error"] == 1

    _run(_go())


def test_malformed_cached_payload_treated_as_miss(monkeypatch):
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    calls = {"n": 0}

    async def _authority():
        calls["n"] += 1
        return {"resolved": True}

    async def _go():
        key = _key()
        fake.store[key.redis_key()] = "{not valid json"
        result = await get_or_fetch(key, _authority)
        assert result.outcome == "miss"
        assert result.value == {"resolved": True}
        assert calls["n"] == 1

    _run(_go())


# ---------------------------------------------------------------------------
# Per-tenant budget enforcement (reuses redis_bridge's OWN counter/tiers)
# ---------------------------------------------------------------------------


def test_budget_exhausted_skips_redis_entirely(monkeypatch):
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    calls = {"n": 0}

    async def _authority():
        calls["n"] += 1
        return {"resolved": True}

    async def _go():
        db = await db_module.init_db(":memory:")
        try:
            await db.execute(
                "INSERT INTO tenants (id, email, plan, redis_commands_used, notification_prefs) "
                "VALUES (?, ?, ?, ?, ?)",
                ("t-over", "over@test.com", "standard",
                 redis_bridge.REDIS_BUDGET_DISABLE_COMMANDS, "{}"),
            )
            await db.commit()

            result = await get_or_fetch(_key(), _authority, tenant_id="t-over", db=db)
            assert result.outcome == "bypass_budget_exhausted"
            assert calls["n"] == 1
            assert fake.get_calls == 0, "Redis must not be touched at all when over budget"
            assert fake.set_calls == 0

            telemetry = get_profile_cache_telemetry()
            assert telemetry["fallback_budget_exhausted"] == 1
        finally:
            await db.close()

    _run(_go())


def test_under_budget_increments_shared_counter(monkeypatch):
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    async def _authority():
        return {"resolved": True}

    async def _go():
        db = await db_module.init_db(":memory:")
        try:
            await db.execute(
                "INSERT INTO tenants (id, email, plan, redis_commands_used, notification_prefs) "
                "VALUES (?, ?, ?, ?, ?)",
                ("t-under", "under@test.com", "standard", 10, "{}"),
            )
            await db.commit()

            result = await get_or_fetch(_key(), _authority, tenant_id="t-under", db=db)
            assert result.outcome == "miss"

            async with db.execute(
                "SELECT redis_commands_used FROM tenants WHERE id = ?", ("t-under",)
            ) as cur:
                row = await cur.fetchone()
            used = row["redis_commands_used"] if isinstance(row, dict) else row[0]
            # GET (miss) + SET (exact) + SET (latest pointer) = 3 commands.
            assert int(used) == 13
        finally:
            await db.close()

    _run(_go())


# ---------------------------------------------------------------------------
# invalidate()
# ---------------------------------------------------------------------------


def test_invalidate_clears_exact_and_latest_keys(monkeypatch):
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    async def _authority():
        return {"resolved": True}

    async def _go():
        key = _key()
        await get_or_fetch(key, _authority)
        assert key.redis_key() in fake.store
        assert key.latest_pointer_key() in fake.store

        ok = await invalidate(key)
        assert ok is True
        assert key.redis_key() not in fake.store
        assert key.latest_pointer_key() not in fake.store

        telemetry = get_profile_cache_telemetry()
        assert telemetry["invalidations"] == 1

        # A subsequent stale-hit attempt must find nothing now.
        key_v2 = _key(generation_key="gen-2")
        calls = {"n": 0}

        async def _authority2():
            calls["n"] += 1
            return {"resolved": "v2"}

        result = await get_or_fetch(key_v2, _authority2, allow_stale_seconds=3600)
        assert result.outcome == "miss"
        assert calls["n"] == 1

    _run(_go())


def test_invalidate_returns_false_when_redis_unavailable(monkeypatch):
    async def _fake_get_client():
        return None

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    async def _go():
        ok = await invalidate(_key())
        assert ok is False

    _run(_go())


# ---------------------------------------------------------------------------
# Bounded TTLs
# ---------------------------------------------------------------------------


def test_ttl_clamped_to_maximum(monkeypatch):
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    async def _authority():
        return {"resolved": True}

    async def _go():
        key = _key()
        await get_or_fetch(key, _authority, ttl_seconds=999_999_999)
        assert fake.ttls[key.redis_key()] == profile_cache.MAX_TTL_SECONDS

    _run(_go())


def test_ttl_clamped_to_minimum_of_one(monkeypatch):
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    async def _authority():
        return {"resolved": True}

    async def _go():
        key = _key()
        await get_or_fetch(key, _authority, ttl_seconds=0)
        assert fake.ttls[key.redis_key()] == 1

    _run(_go())


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_non_callable_authority_raises_immediately(monkeypatch):
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    async def _go():
        with pytest.raises(ProfileCacheError):
            await get_or_fetch(_key(), "not-callable")  # type: ignore[arg-type]

    _run(_go())


def test_telemetry_reset_clears_all_counters(monkeypatch):
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    async def _authority():
        return {"resolved": True}

    async def _go():
        await get_or_fetch(_key(), _authority)
        assert get_profile_cache_telemetry()["misses"] == 1
        reset_profile_cache_telemetry()
        telemetry = get_profile_cache_telemetry()
        assert telemetry["misses"] == 0
        assert telemetry["hits"] == 0
        assert telemetry["neon_avoidance_ratio"] is None

    _run(_go())
