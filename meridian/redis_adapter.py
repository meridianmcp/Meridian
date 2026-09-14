"""37dd1004 (W1-N) — thin, generic Redis adapter for fast/ephemeral state.

SCOPE
-----
A sibling to :mod:`meridian.redis_bridge` (session-message Pub/Sub) and
:mod:`meridian.profile_cache` (revision-keyed profile read-through cache):
this module is the generic get/set/delete/exists surface neither of those
narrowly-scoped modules exposes. It never constructs its own Redis client —
per :mod:`meridian.profile_cache`'s own stated rule ("the ONLY Redis client
this module ever constructs is ``redis_bridge.get_redis_client()``"), this
module reuses that exact same lazily-constructed, cached client. There is
still only ever ONE Redis client/connection-cache/failure-flag in the whole
process, regardless of how many modules call into it.

GRACEFUL DEGRADATION (non-negotiable — matches redis_bridge.py's own
contract verbatim)
--------------------------------------------------------------------------
Every function below NEVER raises for a configuration or network problem:
unset ``MERIDIAN_REDIS_URL``, a missing/incompatible ``redis`` package, a
connection failure, or an exception mid-call all degrade to the same
"unavailable" shape a caller already has to handle for the empty case —
``None`` for a read, ``False`` for a write/delete/exists check. Callers
must treat Redis as a pure speed optimization for ephemeral state, never a
source of correctness: something backed by Postgres/SQLite (or simply
recomputed) must remain the durable answer. The ONLY thing that raises here
is a genuine programmer error — a malformed namespace or key shape, or a
non-string value passed to :func:`adapter_set` — exactly like
``build_object_key`` in :mod:`meridian.object_store` raises synchronously,
before any I/O, for a malformed key component.

KEY NAMESPACING
----------------
Every key is namespaced ``meridian:adapter:{namespace}:{key}`` — callers
supply a short, fixed, code-controlled ``namespace`` (never free-form
caller-supplied text used as a namespace) so two callers can never
accidentally collide, and so an operator inspecting a live Redis instance
can immediately tell which subsystem wrote a given key. This mirrors
``profile_cache.py``'s ``ProfileCacheKey`` allow-list discipline, scaled
down for a generic adapter that has no fixed set of callers to enumerate
up front.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from meridian import redis_bridge

logger = logging.getLogger("meridian.redis_adapter")

# Namespace: lowercase, starts with a letter, letters/digits/underscore only.
_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# Key: any non-empty run of characters that are safe to embed in a Redis key
# and safe to log/display (no whitespace/control chars, no colons — colons
# are the namespace separator, so a caller-supplied key could otherwise
# forge a different namespace segment).
_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

_KEY_PREFIX = "meridian:adapter"


class RedisAdapterError(ValueError):
    """Raised ONLY for a programmer error (malformed namespace/key/value
    shape) — never for a Redis outage or misconfiguration, which always
    degrades to a safe no-op/None/False return instead. Kept as a
    ``ValueError`` subclass so it composes with this codebase's established
    "MCP handlers catch ValueError and return {error}" convention without
    requiring every caller to special-case it."""


def _build_key(namespace: str, key: str) -> str:
    if not isinstance(namespace, str) or not _NAMESPACE_RE.match(namespace):
        raise RedisAdapterError(
            f"namespace {namespace!r} must be lowercase letters/digits/underscore, "
            "starting with a letter"
        )
    if not isinstance(key, str) or not key or not _KEY_RE.match(key):
        raise RedisAdapterError(
            f"key {key!r} must be a non-empty string of letters, digits, '.', "
            "'_' or '-' only"
        )
    return f"{_KEY_PREFIX}:{namespace}:{key}"


def is_redis_configured() -> bool:
    """Non-secret configured check — mirrors
    ``redis_bridge.get_redis_runtime_diagnostics``'s own ``configured`` flag:
    reads only whether ``MERIDIAN_REDIS_URL`` is set, never its value."""
    return bool(os.environ.get("MERIDIAN_REDIS_URL"))


async def adapter_get(namespace: str, key: str) -> "str | None":
    """Best-effort read of a namespaced key.

    Returns ``None`` when Redis is unconfigured, unreachable, the key is
    absent, or any error occurs mid-call — these cases are deliberately
    indistinguishable to the caller (matching
    ``redis_bridge.get_redis_client()``'s own "None means unavailable, not
    necessarily absent" contract). A caller that needs to tell "definitely
    absent" apart from "Redis degraded" must not rely on Redis as its
    source of truth in the first place.
    """
    full_key = _build_key(namespace, key)
    client = await redis_bridge.get_redis_client()
    if client is None:
        return None
    try:
        return await client.get(full_key)
    except Exception:  # noqa: BLE001 — graceful degradation, never crash the caller
        logger.warning("redis_adapter: get failed, degrading to unavailable", exc_info=True)
        return None


async def adapter_set(
    namespace: str, key: str, value: str, *, ex: "int | None" = None,
) -> bool:
    """Best-effort write of a namespaced key, with an optional TTL in
    seconds (``ex``). Returns ``False`` (never raises) for any
    configuration/network/backend failure — a caller must not depend on
    this succeeding for CORRECTNESS, only for speed. Raises
    :class:`RedisAdapterError` synchronously, before any I/O, if ``value``
    is not a string or ``ex`` is not a positive integer — a programmer
    error, not a runtime degradation.
    """
    if not isinstance(value, str):
        raise RedisAdapterError("value must be a string")
    if ex is not None and (isinstance(ex, bool) or not isinstance(ex, int) or ex <= 0):
        raise RedisAdapterError("ex must be a positive integer number of seconds")
    full_key = _build_key(namespace, key)
    client = await redis_bridge.get_redis_client()
    if client is None:
        return False
    try:
        if ex is not None:
            await client.set(full_key, value, ex=ex)
        else:
            await client.set(full_key, value)
        return True
    except Exception:  # noqa: BLE001 — graceful degradation, never crash the caller
        logger.warning("redis_adapter: set failed, degrading to unavailable", exc_info=True)
        return False


async def adapter_delete(namespace: str, key: str) -> bool:
    """Best-effort delete. Returns ``True`` only when a real deletion
    happened; ``False`` for "already absent", "Redis unconfigured", and
    "Redis failed" alike — all three are safe no-ops from the caller's
    perspective. Never raises for a runtime failure."""
    full_key = _build_key(namespace, key)
    client = await redis_bridge.get_redis_client()
    if client is None:
        return False
    try:
        deleted = await client.delete(full_key)
        return bool(deleted)
    except Exception:  # noqa: BLE001 — graceful degradation, never crash the caller
        logger.warning("redis_adapter: delete failed, degrading to unavailable", exc_info=True)
        return False


async def adapter_exists(namespace: str, key: str) -> bool:
    """Best-effort existence check. Returns ``False`` for "absent",
    "Redis unconfigured", and "Redis failed" alike — never raises for a
    runtime failure."""
    full_key = _build_key(namespace, key)
    client = await redis_bridge.get_redis_client()
    if client is None:
        return False
    try:
        return bool(await client.exists(full_key))
    except Exception:  # noqa: BLE001 — graceful degradation, never crash the caller
        logger.warning("redis_adapter: exists failed, degrading to unavailable", exc_info=True)
        return False


def get_redis_adapter_diagnostics() -> "dict[str, Any]":
    """Safe, non-secret snapshot for this adapter specifically — thin
    wrapper around ``redis_bridge.get_redis_runtime_diagnostics`` (the
    single shared source of truth for connection/availability state) so a
    caller only interested in whether the generic adapter surface is usable
    doesn't need to know redis_bridge exists. Never performs a live network
    probe — see that function's own docstring for why."""
    diag = redis_bridge.get_redis_runtime_diagnostics()
    return {
        "configured": diag["configured"],
        "availability": diag["availability"],
        "key_prefix": _KEY_PREFIX,
    }
