"""Revision-keyed Redis read-through cache for profile snapshots (PROFILE-4, 8d52b620).

Sprint item 8d52b620-5c3c-4d25-b377-3082206d1924. Builds on the PROFILE-1
contract (62c41508) and reuses two already-shipped precedents rather than
inventing new ones:

  * :mod:`meridian.redis_bridge` — the ONLY Redis client this module ever
    constructs is ``redis_bridge.get_redis_client()``. Never a new client,
    never a new per-tenant command budget: the WARN/DISABLE/ADMIN_ALERT
    tiers and the ``redis_commands_used`` counter are the exact same ones
    ``publish_session_message`` already draws from (342dd15f).
  * :mod:`meridian.db.board_snapshot` — the "content-addressed key means no
    explicit invalidation path" idea (generation_key baked into the cache
    key) mirrors that module's revision-hash discipline.

Scope boundary (non-negotiable, per the PROFILE-1 contract's "Neon vs Redis
boundary" section): Neon/Postgres is authoritative for every profile row,
revision ledger, and lifecycle state. Redis is ONLY a read-through cache for
immutable/versioned profile snapshots, effective-profile projections,
capability/tool manifests, and handoff/config projections — see
``ALLOWED_NAMESPACES`` below. Redis NEVER stores claims, leases, pointers,
writes, or completion state; ``ProfileCacheKey`` refuses to construct a key
outside the allow-list.

Decoupling from sibling PROFILE items (explicit design constraint from this
item's own notes): items 2/3 (Neon persistence + resolver) are running in
parallel and, as of this item's implementation, ``meridian/profile_contract.py``
and the ``profile_layers``/``profile_layer_revisions`` tables do not exist yet
(confirmed by reading 62c41508's own notes: the Pydantic contract fixtures
were deliberately NOT implemented in that item, "per no storage yet scope").
This module therefore never imports anything from a not-yet-existing sibling
module. Instead the "fetch from authority" side of the read-through cache is
an injected zero-arg async callable (see :class:`AuthorityFetch`) that the
CALLER binds to whatever real Neon-backed resolver eventually exists — this
module has zero compile-time or runtime coupling to that resolver's shape.

Design summary:

  * :class:`ProfileCacheKey` — the full cache key: namespace (restricted to
    the allow-list), scope_type/scope_id (tenant/workspace/user/project/
    session — whatever scope the caller's layer uses), profile_id, and the
    three freshness coordinates the PROFILE-1 contract calls out explicitly:
    ``generation_key``, ``schema_version``, ``resolver_version``. Because
    ``generation_key`` is part of the physical Redis key, a write (which
    bumps generation) is automatically a cache-miss-inducing new key — no
    separate invalidation ledger needed for the common case, exactly as the
    contract specifies.
  * :func:`get_or_fetch` — the read-through entry point. On an exact-key
    cache hit, returns the cached value with zero authority calls. On a
    miss, outage, or budget exhaustion, calls the injected
    ``fetch_from_authority`` callback and (when Redis is healthy and under
    budget) best-effort populates the cache for next time. Every fallback
    path is safe-open: this module NEVER raises for a Redis-side fault —
    only a malformed key/callable (a programmer error) raises synchronously
    at construction time.
  * Bounded-staleness fallback (opt-in via ``allow_stale_seconds``): a
    secondary "latest known good" pointer key (same coordinates, minus
    ``generation_key``) lets a caller who explicitly accepts a bounded
    staleness window get a ``stale_hit`` instead of an authority round trip
    when Redis is healthy but the exact generation isn't cached (e.g. right
    after a write, before the new generation has been populated once). This
    is opt-in and OFF by default (``allow_stale_seconds=0``): a caller that
    doesn't ask for it always either gets a fresh cache hit or falls all the
    way through to Neon, matching "stale generation ... fall back safely to
    Neon" as the conservative default.
  * :func:`invalidate` — explicit purge of both the exact key and the
    "latest known good" pointer for a key's coordinates. Not required for
    correctness (content-addressing already makes stale exact keys
    unreachable), but lets a writer proactively clear the stale-fallback
    pointer the instant it bumps generation, per the item notes' "writes
    bump generation and publish invalidation."
  * Telemetry (:func:`get_profile_cache_telemetry`) — hit / stale_hit / miss
    / invalidation counters, three distinct fallback-reason counters (redis
    unavailable, budget exhausted, runtime error), separately measured cache
    vs. authority latency, and a MEASURED (not assumed) Neon-avoidance ratio:
    ``authority_calls_avoided / (authority_calls_avoided + authority_calls)``.

Bounded TTLs: every cache write is written with an explicit ``ex=`` TTL,
clamped to ``MAX_TTL_SECONDS``. Nothing this module ever writes to Redis is
unbounded.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from meridian import redis_bridge as _redis_bridge

logger = logging.getLogger("meridian.profile_cache")

# ---------------------------------------------------------------------------
# Namespaces — the ONLY kinds of data this cache may ever hold. Directly
# enumerates the four categories PROFILE-4's item notes name explicitly.
# ---------------------------------------------------------------------------
NAMESPACE_PROFILE_SNAPSHOT = "profile_snapshot"      # immutable/versioned profile snapshots (e.g. hosted_default active row)
NAMESPACE_EFFECTIVE_PROFILE = "effective_profile"    # resolve_effective_profile projections
NAMESPACE_CAPABILITY_MANIFEST = "capability_manifest"  # capability manifest projections
NAMESPACE_TOOL_MANIFEST = "tool_manifest"            # tool/priority-map manifest projections
NAMESPACE_HANDOFF_PROJECTION = "handoff_projection"  # handoff/config projections

ALLOWED_NAMESPACES = frozenset({
    NAMESPACE_PROFILE_SNAPSHOT,
    NAMESPACE_EFFECTIVE_PROFILE,
    NAMESPACE_CAPABILITY_MANIFEST,
    NAMESPACE_TOOL_MANIFEST,
    NAMESPACE_HANDOFF_PROJECTION,
})

# Purely for a clearer error message on the mistake this cache exists to
# prevent — these are never allowed regardless of what's in ALLOWED_NAMESPACES.
_FORBIDDEN_NAMESPACES = frozenset({
    "claim", "claims", "lease", "leases", "pointer", "pointers",
    "write", "writes", "completion", "completion_state",
    "sprint_item", "sprint_items", "file_claim", "symbol_claim",
})

#: Default TTL for an exact-generation cache entry.
DEFAULT_TTL_SECONDS = 300
#: Default TTL for the "latest known good" bounded-staleness pointer — longer
#: than the exact-generation TTL on purpose (it's the fallback of last resort
#: before Neon, so it should outlive the entry it shadows).
DEFAULT_STALE_POINTER_TTL_SECONDS = 900
#: Hard ceiling any caller-supplied TTL is clamped to. Nothing this module
#: writes is ever unbounded.
MAX_TTL_SECONDS = 3600


class ProfileCacheError(ValueError):
    """Raised on an invalid cache key or configuration.

    Never raised for a Redis-side runtime fault (outage, timeout, malformed
    payload) — those always fall back to the authority callback instead.
    """


class AuthorityFetch(Protocol):
    """Injectable "fetch from authority" callback.

    Zero-arg, async, returns the JSON-serializable value to cache. The
    caller closes over whatever scope context (project_id, tenant_id, a
    Neon connection, ...) it needs to actually resolve the value — this
    module never calls into a specific storage layer itself, so it has no
    compile-time or runtime coupling to PROFILE-2/3's persistence functions
    (which may not exist yet in a given deployment).
    """

    async def __call__(self) -> Any: ...  # pragma: no cover - structural typing only


# A plain Callable is accepted too (Protocol above is documentation/typing only).
AuthorityFetchCallable = Callable[[], Awaitable[Any]]


@dataclass(frozen=True)
class ProfileCacheKey:
    """Full read-through cache key.

    Key coordinates per the PROFILE-1 contract: scope (tenant/project or
    workspace/user — whatever scope_type/scope_id the caller's layer uses),
    profile id, revision/generation (``generation_key``), schema version,
    and resolver version. ``namespace`` additionally restricts WHAT kind of
    projection this is, enforced against ``ALLOWED_NAMESPACES``.
    """

    namespace: str
    scope_type: str
    scope_id: str
    profile_id: str
    generation_key: str
    schema_version: int
    resolver_version: int

    def __post_init__(self) -> None:
        ns = str(self.namespace or "").strip().lower()
        if not ns:
            raise ProfileCacheError("namespace is required")
        if ns in _FORBIDDEN_NAMESPACES:
            raise ProfileCacheError(
                f"namespace {ns!r} must never be cached in profile_cache — "
                "claims/leases/pointers/writes/completion state stay Neon-only"
            )
        if ns not in ALLOWED_NAMESPACES:
            raise ProfileCacheError(
                f"namespace must be one of {sorted(ALLOWED_NAMESPACES)}, got {ns!r}"
            )
        object.__setattr__(self, "namespace", ns)

        scope_type = str(self.scope_type or "").strip().lower()
        if not scope_type:
            raise ProfileCacheError("scope_type is required")
        object.__setattr__(self, "scope_type", scope_type)

        scope_id = str(self.scope_id or "").strip()
        if not scope_id:
            raise ProfileCacheError("scope_id is required")
        object.__setattr__(self, "scope_id", scope_id)

        profile_id = str(self.profile_id or "").strip()
        if not profile_id:
            raise ProfileCacheError("profile_id is required")
        object.__setattr__(self, "profile_id", profile_id)

        generation_key = str(self.generation_key or "").strip()
        if not generation_key:
            raise ProfileCacheError("generation_key is required")
        object.__setattr__(self, "generation_key", generation_key)

        try:
            schema_version = int(self.schema_version)
            resolver_version = int(self.resolver_version)
        except (TypeError, ValueError) as exc:
            raise ProfileCacheError("schema_version/resolver_version must be integers") from exc
        if schema_version < 1 or resolver_version < 1:
            raise ProfileCacheError("schema_version/resolver_version must both be >= 1")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "resolver_version", resolver_version)

    def redis_key(self) -> str:
        """Exact-generation key — content-addressed, so a new generation is a new key."""
        return (
            f"meridian:profile_cache:v{self.schema_version}:r{self.resolver_version}:"
            f"{self.namespace}:{self.scope_type}:{self.scope_id}:{self.profile_id}:"
            f"{self.generation_key}"
        )

    def latest_pointer_key(self) -> str:
        """"Latest known good" key — deliberately excludes generation_key.

        Used only for the opt-in bounded-staleness fallback in
        :func:`get_or_fetch`; never consulted unless the caller passes
        ``allow_stale_seconds > 0``.
        """
        return (
            f"meridian:profile_cache:latest:v{self.schema_version}:r{self.resolver_version}:"
            f"{self.namespace}:{self.scope_type}:{self.scope_id}:{self.profile_id}"
        )


@dataclass(frozen=True)
class ProfileCacheResult:
    """Outcome of a single :func:`get_or_fetch` call."""

    value: Any
    #: "hit" | "stale_hit" | "miss" | "bypass_redis_unavailable" |
    #: "bypass_budget_exhausted" | "bypass_error"
    outcome: str
    #: "cache" | "stale_cache" | "authority"
    source: str
    stale: bool
    #: generation_key actually behind the returned value (may differ from
    #: ``requested_generation_key`` on a stale_hit).
    cache_generation_key: str | None
    requested_generation_key: str
    #: Redis round-trip latency in ms for the lookup that produced this
    #: result (0.0 when Redis was never consulted, e.g. bypass_* outcomes).
    latency_ms: float


@dataclass
class _Telemetry:
    hits: int = 0
    stale_hits: int = 0
    misses: int = 0
    invalidations: int = 0
    fallback_redis_unavailable: int = 0
    fallback_budget_exhausted: int = 0
    fallback_error: int = 0
    authority_calls: int = 0
    authority_calls_avoided: int = 0
    cache_latency_ms_total: float = 0.0
    cache_latency_samples: int = 0
    authority_latency_ms_total: float = 0.0
    authority_latency_samples: int = 0


_telemetry = _Telemetry()


def reset_profile_cache_telemetry() -> None:
    """Test helper — reset all counters between tests."""
    global _telemetry
    _telemetry = _Telemetry()


def get_profile_cache_telemetry() -> dict[str, Any]:
    """Snapshot of hit/miss/stale/invalidation/fallback counters, latency
    averages, and a MEASURED Neon-avoidance ratio (never assumed/estimated).
    """
    t = _telemetry
    avoided = t.authority_calls_avoided
    made = t.authority_calls
    denom = avoided + made
    return {
        "hits": t.hits,
        "stale_hits": t.stale_hits,
        "misses": t.misses,
        "invalidations": t.invalidations,
        "fallback_redis_unavailable": t.fallback_redis_unavailable,
        "fallback_budget_exhausted": t.fallback_budget_exhausted,
        "fallback_error": t.fallback_error,
        "authority_calls": made,
        "authority_calls_avoided": avoided,
        "neon_avoidance_ratio": (avoided / denom) if denom else None,
        "avg_cache_latency_ms": (
            t.cache_latency_ms_total / t.cache_latency_samples if t.cache_latency_samples else None
        ),
        "avg_authority_latency_ms": (
            t.authority_latency_ms_total / t.authority_latency_samples
            if t.authority_latency_samples else None
        ),
        "total_lookups": t.hits + t.stale_hits + t.misses,
    }


def _clamp_ttl(seconds: int) -> int:
    if seconds is None or seconds <= 0:
        return 1
    return min(int(seconds), MAX_TTL_SECONDS)


async def _budget_allows_redis(tenant_id: str | None, db: Any | None) -> bool:
    """Tier-2 (DISABLE) gate, reusing redis_bridge's own tenant counter — the
    SAME budget publish_session_message draws from, not a parallel one.

    Safe-open on any error (a budget-check fault must never block a cache
    lookup that would otherwise succeed). ``tenant_id``/``db`` absent
    (self-hosted / local mode) skips the check entirely, same contract as
    redis_bridge.publish_session_message.
    """
    if tenant_id is None or db is None:
        return True
    try:
        used = await _redis_bridge._get_redis_commands_used(db, tenant_id)  # noqa: SLF001
    except Exception:  # noqa: BLE001
        logger.warning("profile_cache: budget check failed, proceeding (safe-open)", exc_info=True)
        return True
    if used >= _redis_bridge.REDIS_BUDGET_DISABLE_COMMANDS:
        if used >= _redis_bridge.REDIS_BUDGET_ADMIN_ALERT_COMMANDS:
            # Tier 3 — structurally unreachable if Tier 2 works; mirrors
            # redis_bridge.publish_session_message's own escalation.
            _redis_bridge._fire_admin_alert_background(tenant_id, used)  # noqa: SLF001
        return False
    return True


async def _note_command(tenant_id: str | None, db: Any | None) -> None:
    """Best-effort increment of the shared per-tenant Redis command counter."""
    if tenant_id is None or db is None:
        return
    try:
        await _redis_bridge._increment_redis_commands(db, tenant_id)  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass  # counter-update failure must never affect the caller's result


def _make_result(
    value: Any,
    *,
    outcome: str,
    source: str,
    stale: bool,
    cache_generation_key: str | None,
    requested_generation_key: str,
    latency_ms: float,
) -> ProfileCacheResult:
    return ProfileCacheResult(
        value=value,
        outcome=outcome,
        source=source,
        stale=stale,
        cache_generation_key=cache_generation_key,
        requested_generation_key=requested_generation_key,
        latency_ms=latency_ms,
    )


async def get_or_fetch(
    key: ProfileCacheKey,
    fetch_from_authority: AuthorityFetchCallable,
    *,
    tenant_id: str | None = None,
    db: Any | None = None,
    ttl_seconds: int | None = None,
    allow_stale_seconds: int = 0,
) -> ProfileCacheResult:
    """Read-through cache lookup. NEVER raises for a Redis-side fault.

    Order of operations:
      1. Redis unavailable (unconfigured, or a previously failed client
         construction) -> straight to authority. ``bypass_redis_unavailable``.
      2. Tenant over the shared Tier-2 command budget -> straight to
         authority, without touching Redis at all (that's the whole point
         of the budget gate). ``bypass_budget_exhausted``.
      3. Exact-generation GET. A hit returns immediately, zero authority
         calls. ``hit``.
      4. On an exact miss, if ``allow_stale_seconds > 0``, consult the
         "latest known good" pointer; a sufficiently-recent stale entry is
         returned as ``stale_hit`` (still zero authority calls) — this is
         opt-in and off by default.
      5. True miss (or stale entry too old / absent) -> call
         ``fetch_from_authority``, then best-effort repopulate the cache.
         ``miss``.
      6. Any Redis GET/SET exception mid-flight -> log, fall back to
         authority for THIS call. ``bypass_error``.

    ``tenant_id``/``db`` are optional; when both are absent (self-hosted /
    local mode) the budget check is skipped entirely, matching
    redis_bridge.publish_session_message's own contract.
    """
    if not callable(fetch_from_authority):
        raise ProfileCacheError("fetch_from_authority must be a callable")

    ttl = _clamp_ttl(ttl_seconds if ttl_seconds is not None else DEFAULT_TTL_SECONDS)
    pointer_ttl = _clamp_ttl(
        max(ttl, DEFAULT_STALE_POINTER_TTL_SECONDS) if allow_stale_seconds else ttl
    )

    async def _call_authority() -> Any:
        start = time.monotonic()
        try:
            return await fetch_from_authority()
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            _telemetry.authority_latency_ms_total += elapsed_ms
            _telemetry.authority_latency_samples += 1
            _telemetry.authority_calls += 1

    client = await _redis_bridge.get_redis_client()
    if client is None:
        _telemetry.fallback_redis_unavailable += 1
        value = await _call_authority()
        return _make_result(
            value, outcome="bypass_redis_unavailable", source="authority", stale=False,
            cache_generation_key=None, requested_generation_key=key.generation_key,
            latency_ms=0.0,
        )

    if not await _budget_allows_redis(tenant_id, db):
        _telemetry.fallback_budget_exhausted += 1
        value = await _call_authority()
        return _make_result(
            value, outcome="bypass_budget_exhausted", source="authority", stale=False,
            cache_generation_key=None, requested_generation_key=key.generation_key,
            latency_ms=0.0,
        )

    start = time.monotonic()
    try:
        raw = await client.get(key.redis_key())
        await _note_command(tenant_id, db)
    except Exception:  # noqa: BLE001
        logger.warning("profile_cache: GET failed, falling back to authority", exc_info=True)
        _telemetry.fallback_error += 1
        value = await _call_authority()
        return _make_result(
            value, outcome="bypass_error", source="authority", stale=False,
            cache_generation_key=None, requested_generation_key=key.generation_key,
            latency_ms=(time.monotonic() - start) * 1000.0,
        )
    cache_latency_ms = (time.monotonic() - start) * 1000.0
    _telemetry.cache_latency_ms_total += cache_latency_ms
    _telemetry.cache_latency_samples += 1

    if raw is not None:
        try:
            payload = json.loads(raw)
            value = payload["value"]
        except Exception:  # noqa: BLE001
            logger.warning("profile_cache: malformed cached payload, treating as miss", exc_info=True)
        else:
            _telemetry.hits += 1
            _telemetry.authority_calls_avoided += 1
            return _make_result(
                value, outcome="hit", source="cache", stale=False,
                cache_generation_key=key.generation_key,
                requested_generation_key=key.generation_key,
                latency_ms=cache_latency_ms,
            )

    # Exact miss (or malformed entry treated as one). Try the bounded-staleness
    # fallback only if the caller explicitly opted in.
    if allow_stale_seconds > 0:
        try:
            raw_latest = await client.get(key.latest_pointer_key())
            await _note_command(tenant_id, db)
        except Exception:  # noqa: BLE001
            raw_latest = None
        if raw_latest is not None:
            try:
                latest_payload = json.loads(raw_latest)
                stored_at = float(latest_payload["stored_at"])
                age = time.time() - stored_at
            except Exception:  # noqa: BLE001
                age = None
            if age is not None and age <= allow_stale_seconds:
                _telemetry.stale_hits += 1
                _telemetry.authority_calls_avoided += 1
                return _make_result(
                    latest_payload["value"], outcome="stale_hit", source="stale_cache", stale=True,
                    cache_generation_key=latest_payload.get("generation_key"),
                    requested_generation_key=key.generation_key,
                    latency_ms=cache_latency_ms,
                )

    _telemetry.misses += 1
    value = await _call_authority()
    await _store(client, key, value, ttl=ttl, pointer_ttl=pointer_ttl, tenant_id=tenant_id, db=db)
    return _make_result(
        value, outcome="miss", source="authority", stale=False,
        cache_generation_key=key.generation_key,
        requested_generation_key=key.generation_key,
        latency_ms=cache_latency_ms,
    )


async def _store(
    client: Any,
    key: ProfileCacheKey,
    value: Any,
    *,
    ttl: int,
    pointer_ttl: int,
    tenant_id: str | None,
    db: Any | None,
) -> None:
    """Best-effort cache population. Never raises — a failed SET only means
    the NEXT lookup also falls back to authority, not that this one does.
    """
    payload = json.dumps(
        {"value": value, "generation_key": key.generation_key, "stored_at": time.time()},
        default=str,
    )
    try:
        await client.set(key.redis_key(), payload, ex=ttl)
        await _note_command(tenant_id, db)
    except Exception:  # noqa: BLE001
        logger.warning("profile_cache: SET (exact key) failed, value not cached", exc_info=True)
        return
    try:
        await client.set(key.latest_pointer_key(), payload, ex=pointer_ttl)
        await _note_command(tenant_id, db)
    except Exception:  # noqa: BLE001
        logger.warning("profile_cache: SET (latest pointer) failed", exc_info=True)


async def invalidate(
    key: ProfileCacheKey,
    *,
    tenant_id: str | None = None,
    db: Any | None = None,
) -> bool:
    """Explicit purge of both the exact-generation key and the "latest known
    good" pointer for ``key``'s coordinates.

    Not required for correctness against the exact-generation key itself
    (content-addressing already makes a stale exact key unreachable once
    generation_key changes) — this exists so a writer can proactively clear
    the bounded-staleness fallback pointer the instant it bumps generation,
    per the item notes' "writes bump generation and publish invalidation."

    Returns False (without raising) when Redis is unavailable or the delete
    fails; True on a completed (even if no-op) delete attempt.
    """
    client = await _redis_bridge.get_redis_client()
    if client is None:
        return False
    try:
        await client.delete(key.redis_key())
        await _note_command(tenant_id, db)
        await client.delete(key.latest_pointer_key())
        await _note_command(tenant_id, db)
    except Exception:  # noqa: BLE001
        logger.warning("profile_cache: invalidate failed", exc_info=True)
        return False
    _telemetry.invalidations += 1
    return True
