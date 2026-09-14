"""37dd1004 (W1-N) — tests for :mod:`meridian.redis_adapter`: the generic
get/set/delete/exists surface built on top of
``redis_bridge.get_redis_client()`` (the ONLY Redis client this adapter --
or any Redis-touching module in this repo, per profile_cache.py's own
established rule -- ever constructs).

Mirrors tests/test_0bfde7ad_redis_push_augmentation.py's fake-client
pattern: never a real network connection, only a fake object monkeypatched
in via ``redis_bridge.get_redis_client``.
"""
from __future__ import annotations

import asyncio

import pytest

from meridian import redis_adapter
from meridian import redis_bridge


@pytest.fixture(autouse=True)
def _reset_redis_bridge_cache(monkeypatch):
    """Hermetic: no leaked client/failure-flag state between tests, and
    MERIDIAN_REDIS_URL is unset by default regardless of the real
    environment."""
    redis_bridge.reset_redis_client_cache()
    monkeypatch.delenv("MERIDIAN_REDIS_URL", raising=False)
    yield
    redis_bridge.reset_redis_client_cache()


class _FakeRedisClient:
    """In-memory stand-in for redis.asyncio.Redis's generic KV surface."""

    def __init__(self):
        self._store: dict[str, str] = {}
        self._ttls: dict[str, int] = {}

    async def get(self, key: str):
        return self._store.get(key)

    async def set(self, key: str, value: str, *, ex: "int | None" = None):
        self._store[key] = value
        if ex is not None:
            self._ttls[key] = ex
        return True

    async def delete(self, key: str) -> int:
        existed = key in self._store
        self._store.pop(key, None)
        self._ttls.pop(key, None)
        return 1 if existed else 0

    async def exists(self, key: str) -> int:
        return 1 if key in self._store else 0


class _FailingRedisClient:
    """Simulates a Redis outage -- every call raises."""

    async def get(self, key: str):
        raise ConnectionError("simulated Redis outage")

    async def set(self, key: str, value: str, *, ex=None):
        raise ConnectionError("simulated Redis outage")

    async def delete(self, key: str):
        raise ConnectionError("simulated Redis outage")

    async def exists(self, key: str):
        raise ConnectionError("simulated Redis outage")


def _install_fake_client(monkeypatch, client):
    async def _fake_get_client():
        return client

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)


# ---------------------------------------------------------------------------
# Key/namespace validation (programmer errors -- these DO raise)
# ---------------------------------------------------------------------------


def test_build_key_rejects_bad_namespace():
    with pytest.raises(redis_adapter.RedisAdapterError, match="namespace"):
        redis_adapter._build_key("Bad-Namespace", "k1")
    with pytest.raises(redis_adapter.RedisAdapterError, match="namespace"):
        redis_adapter._build_key("1starts_with_digit", "k1")


def test_build_key_rejects_bad_key():
    with pytest.raises(redis_adapter.RedisAdapterError, match="key"):
        redis_adapter._build_key("cache", "")
    with pytest.raises(redis_adapter.RedisAdapterError, match="key"):
        redis_adapter._build_key("cache", "has a space")
    with pytest.raises(redis_adapter.RedisAdapterError, match="key"):
        redis_adapter._build_key("cache", "colon:injection")


def test_build_key_shape_is_namespaced():
    assert redis_adapter._build_key("cache", "k1") == "meridian:adapter:cache:k1"


def test_adapter_set_rejects_non_string_value():
    async def _run():
        with pytest.raises(redis_adapter.RedisAdapterError, match="value must be a string"):
            await redis_adapter.adapter_set("cache", "k1", 123)  # type: ignore[arg-type]
    asyncio.run(_run())


def test_adapter_set_rejects_bad_ex():
    async def _run():
        with pytest.raises(redis_adapter.RedisAdapterError, match="ex must be"):
            await redis_adapter.adapter_set("cache", "k1", "v", ex=0)
        with pytest.raises(redis_adapter.RedisAdapterError, match="ex must be"):
            await redis_adapter.adapter_set("cache", "k1", "v", ex=-5)
        with pytest.raises(redis_adapter.RedisAdapterError, match="ex must be"):
            await redis_adapter.adapter_set("cache", "k1", "v", ex=True)  # type: ignore[arg-type]
    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Graceful degradation -- unconfigured (no MERIDIAN_REDIS_URL)
# ---------------------------------------------------------------------------


def test_all_ops_degrade_gracefully_when_unconfigured():
    async def _run():
        assert await redis_adapter.adapter_get("cache", "k1") is None
        assert await redis_adapter.adapter_set("cache", "k1", "v1") is False
        assert await redis_adapter.adapter_delete("cache", "k1") is False
        assert await redis_adapter.adapter_exists("cache", "k1") is False
    asyncio.run(_run())


def test_is_redis_configured_reflects_env_var(monkeypatch):
    monkeypatch.delenv("MERIDIAN_REDIS_URL", raising=False)
    assert redis_adapter.is_redis_configured() is False
    monkeypatch.setenv("MERIDIAN_REDIS_URL", "redis://localhost:6379/0")
    assert redis_adapter.is_redis_configured() is True


# ---------------------------------------------------------------------------
# Real (fake-client) round trip
# ---------------------------------------------------------------------------


def test_set_get_delete_exists_round_trip(monkeypatch):
    fake = _FakeRedisClient()
    _install_fake_client(monkeypatch, fake)

    async def _run():
        assert await redis_adapter.adapter_exists("cache", "k1") is False
        assert await redis_adapter.adapter_set("cache", "k1", "hello") is True
        assert await redis_adapter.adapter_get("cache", "k1") == "hello"
        assert await redis_adapter.adapter_exists("cache", "k1") is True
        assert await redis_adapter.adapter_delete("cache", "k1") is True
        assert await redis_adapter.adapter_get("cache", "k1") is None
        # Deleting an already-absent key is a safe no-op, not an error.
        assert await redis_adapter.adapter_delete("cache", "k1") is False
    asyncio.run(_run())


def test_set_with_ttl_forwards_ex_to_client(monkeypatch):
    fake = _FakeRedisClient()
    _install_fake_client(monkeypatch, fake)

    async def _run():
        assert await redis_adapter.adapter_set("cache", "k1", "v1", ex=30) is True
        assert fake._ttls["meridian:adapter:cache:k1"] == 30
    asyncio.run(_run())


def test_two_namespaces_never_collide(monkeypatch):
    fake = _FakeRedisClient()
    _install_fake_client(monkeypatch, fake)

    async def _run():
        await redis_adapter.adapter_set("cache_a", "k1", "value-a")
        await redis_adapter.adapter_set("cache_b", "k1", "value-b")
        assert await redis_adapter.adapter_get("cache_a", "k1") == "value-a"
        assert await redis_adapter.adapter_get("cache_b", "k1") == "value-b"
    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Graceful degradation -- backend outage (configured, but every call raises)
# ---------------------------------------------------------------------------


def test_all_ops_degrade_gracefully_on_backend_failure(monkeypatch):
    _install_fake_client(monkeypatch, _FailingRedisClient())

    async def _run():
        assert await redis_adapter.adapter_get("cache", "k1") is None
        assert await redis_adapter.adapter_set("cache", "k1", "v1") is False
        assert await redis_adapter.adapter_delete("cache", "k1") is False
        assert await redis_adapter.adapter_exists("cache", "k1") is False
    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_diagnostics_reflect_shared_redis_bridge_state(monkeypatch):
    diag = redis_adapter.get_redis_adapter_diagnostics()
    assert diag["configured"] is False
    assert diag["availability"] == "unconfigured"
    assert diag["key_prefix"] == "meridian:adapter"

    monkeypatch.setenv("MERIDIAN_REDIS_URL", "redis://localhost:6379/0")
    diag2 = redis_adapter.get_redis_adapter_diagnostics()
    assert diag2["configured"] is True
