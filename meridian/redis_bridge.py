"""0bfde7ad — Redis push augmentation for session_messages.

AUGMENTS, does not replace, the existing Postgres/SQLite-backed
send_message/receive_messages primitives (d3a3a01d). The DB row remains the
durable, authoritative record; this module is a best-effort push notification
on top so a listening subscriber can be woken the instant a message is sent
instead of polling receive_messages.

Deployed-app path only (pinned decision 5710635f — reconciled against the
229441bc/2ad938a0 "no new infra" precedents: neither applies here, since push
notification is a genuine capability gap Postgres has no native equivalent
for, not a case of duplicating something Postgres already solves well).
meridian-hosted reaches its Fly-provisioned Redis over Fly's private network;
MERIDIAN_REDIS_URL is not expected to be set for local/self-hosted use, and
every function here degrades to a safe no-op when it's absent.

Never raises. A Redis outage or misconfiguration must never break the
underlying DB write in send_message — that's the whole point of "augment,
not replace".

342dd15f — per-tenant Redis command budget (Upstash cost guard):
  Tier 1 — WARNING  at 500 000 commands (~$1.00 Upstash cost): dashboard
            banner + email; idempotent via notification_prefs blob.
  Tier 2 — DISABLE  at 1 000 000 commands (~$2.00): publish_session_message
            returns False immediately so the call falls back to Postgres
            polling, exactly as if MERIDIAN_REDIS_URL were not set.
  Tier 3 — ADMIN ALERT at 2 000 000 commands (~$4.00): structurally
            unreachable if Tier 2 is enforced; crossing it signals a bug in
            the Tier 2 gate. Fires a real admin-facing alert via the existing
            MERIDIAN_ADMIN_NTFY_URL / ADMIN_EMAIL paths.

2cf57fde — runtime health / cache-effectiveness / Neon-avoidance diagnostics.
Follow-up to investigation 686ab70f (finding 9db59816, decision b6b1a0f5).
This module tracks lightweight, per-process, in-memory counters (never a
secret, never MERIDIAN_REDIS_URL itself) that :func:`get_redis_runtime_diagnostics`
assembles into one safe snapshot, surfaced via the existing
``GET /tunnel/diagnostics/{tenant_id}`` route and ``get_tunnel_diagnostics``
MCP tool (both already call meridian.routes.tunnel.build_tunnel_diagnostics).
Deliberately does NOT implement a Redis-backed read-through cache itself —
that is a separate sprint item's scope. The ``cache.redis_cache`` counters
below (record_cache_hit/miss/set/invalidation) are wired for that future
cache to call; until something calls them they honestly read zero with
``active: False`` rather than fabricating savings. ``cache.local_process_cache``
reports the genuinely active Neon-avoidance mechanism that exists TODAY: the
process-local TTL cache in meridian/db/sprint_items.py.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from typing import Any, AsyncIterator

logger = logging.getLogger("meridian.redis_bridge")

_redis_client: Any = None
_redis_unavailable: bool = False

# ---------------------------------------------------------------------------
# 2cf57fde — runtime diagnostics state (per-process, in-memory, non-secret).
#
# Never persisted, never cross-instance-aggregated — mirrors every other
# per-process counter in this codebase (e.g. the a1d75ff3 sprint-items cache,
# build_tunnel_diagnostics's server_routing_cache/process_leases). A
# multi-instance deployment reports these independently per instance.
# ---------------------------------------------------------------------------
_LATENCY_SAMPLE_MAX = 50

_redis_diag_state: dict[str, Any] = {
    "connection_generation": 0,
    "publish_attempts": 0,
    "publish_successes": 0,
    "publish_failures": 0,
    "publish_fallback_unconfigured": 0,  # get_redis_client() returned None
    "publish_fallback_budget": 0,        # Tier-2/3 budget short-circuit
    "last_error_class": None,
    "last_error_at": None,
    "last_success_at": None,
    # 2cf57fde round-2 fix — a pure client-CONSTRUCTION failure (bad URL,
    # missing/incompatible redis-py, etc.) is tracked HERE, deliberately
    # separate from last_error_class/last_error_at above. Those two fields
    # are reserved for genuine publish-ATTEMPT evidence (see the precedence
    # comment in get_redis_runtime_diagnostics and
    # test_diagnostics_publish_failure_records_last_error_class /
    # test_diagnostics_reset_clears_all_counters, which assert last_error_class
    # reflects a publish failure). Keeping construction failures out of those
    # fields is what makes the "construction_failed" availability state
    # reachable at all -- see get_redis_client()'s except block.
    "last_construction_error_class": None,
    "last_construction_error_at": None,
    "latency_ms_samples": deque(maxlen=_LATENCY_SAMPLE_MAX),
    # Counters for a future Redis-backed read-through cache (NOT implemented
    # by this item) — see record_cache_hit/miss/set/invalidation below.
    "cache_hits": 0,
    "cache_misses": 0,
    "cache_sets": 0,
    "cache_invalidations": 0,
}

# ---------------------------------------------------------------------------
# 342dd15f — Upstash cost-guard: per-tenant Redis command budget thresholds.
#
# Upstash pricing: $0.20 / 100 000 commands → $1 = 500 000 commands.
# These constants define the three enforcement tiers in raw command counts.
# ---------------------------------------------------------------------------

#: Tier 1 — WARNING threshold (~$1.00 Upstash cost).
REDIS_BUDGET_WARN_COMMANDS: int = 500_000

#: Tier 2 — DISABLE threshold (~$2.00 Upstash cost). publish_session_message
#: returns False immediately once the tenant's counter reaches this level so
#: they fall back to Postgres polling.  Zero new failure mode — reuses the
#: existing "Redis unconfigured" fallback path.
REDIS_BUDGET_DISABLE_COMMANDS: int = 1_000_000

#: Tier 3 — ABSOLUTE BACKSTOP (~$4.00).  Should be structurally unreachable
#: if Tier 2 works correctly. Crossing it fires an admin alert.
REDIS_BUDGET_ADMIN_ALERT_COMMANDS: int = 2_000_000


def _channel_for(session_id: str) -> str:
    return f"meridian:messages:{session_id}"


def reset_redis_client_cache() -> None:
    """Test helper — clear the cached client/failure flag between tests.

    2cf57fde — also resets the runtime-diagnostics counters (connection
    generation, pub/sub attempt counts, latency samples, cache counters,
    last error) so tests get a clean, hermetic diagnostics snapshot along
    with the clean client cache. Existing callers of this fixture-style
    reset are unaffected — the additional state is purely new.
    """
    global _redis_client, _redis_unavailable
    _redis_client = None
    _redis_unavailable = False
    _redis_diag_state.update({
        "connection_generation": 0,
        "publish_attempts": 0,
        "publish_successes": 0,
        "publish_failures": 0,
        "publish_fallback_unconfigured": 0,
        "publish_fallback_budget": 0,
        "last_error_class": None,
        "last_error_at": None,
        "last_success_at": None,
        "last_construction_error_class": None,
        "last_construction_error_at": None,
        "cache_hits": 0,
        "cache_misses": 0,
        "cache_sets": 0,
        "cache_invalidations": 0,
    })
    _redis_diag_state["latency_ms_samples"].clear()


async def get_redis_client() -> Any | None:
    """Lazily create (and cache) an async Redis client from MERIDIAN_REDIS_URL.

    Returns None when the env var is unset, the ``redis`` package isn't
    installed, or the client can't be constructed — callers must treat None
    as "push augmentation unavailable, fall back to DB-only" and continue
    normally. Construction failures are cached (``_redis_unavailable``) so a
    misconfigured URL doesn't retry on every single send_message call.
    """
    global _redis_client, _redis_unavailable
    if _redis_client is not None:
        return _redis_client
    if _redis_unavailable:
        return None
    url = os.environ.get("MERIDIAN_REDIS_URL")
    if not url:
        return None
    try:
        import redis.asyncio as redis_asyncio  # noqa: PLC0415

        _redis_client = redis_asyncio.from_url(url, decode_responses=True)
        # 2cf57fde — a freshly constructed client is a new "connection
        # generation/epoch" for diagnostics purposes (covers both first
        # connect and reconnect-after-reset_redis_client_cache()).
        _redis_diag_state["connection_generation"] += 1
        return _redis_client
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis_bridge: could not construct Redis client, disabling push augmentation", exc_info=True)
        _redis_unavailable = True
        # 2cf57fde round-2 fix — record this under the dedicated
        # construction-error fields, NOT last_error_class/last_error_at.
        # Those are reserved for genuine publish-ATTEMPT evidence (see the
        # precedence comment in get_redis_runtime_diagnostics); conflating
        # the two here was the root cause of "construction_failed" being
        # unreachable dead code, since a construction failure would always
        # populate last_error_at and get shadowed by the publish-evidence
        # branch below.
        _redis_diag_state["last_construction_error_class"] = type(exc).__name__
        _redis_diag_state["last_construction_error_at"] = time.time()
        return None


async def publish_session_message(
    to_session_id: str,
    message: dict[str, Any],
    *,
    tenant_id: str | None = None,
    db: Any | None = None,
) -> bool:
    """Best-effort push: publish ``message`` (JSON-encoded) to the recipient
    session's channel. Returns True on a real publish, False in every
    no-op/failure case. NEVER raises — callers (send_message) must not have
    their own DB-write success depend on this.

    342dd15f — optional ``tenant_id`` + ``db`` enable per-tenant Upstash cost
    enforcement. When supplied, the function:
      1. Checks the tenant's current-month redis_commands_used counter.
      2. Returns False immediately (Tier 2 / DISABLE) if the counter is at or
         above REDIS_BUDGET_DISABLE_COMMANDS (~$2 / 1 M commands) — same
         fallback path as "Redis not configured".
      3. Fires an admin alert (Tier 3) if the counter is at or above
         REDIS_BUDGET_ADMIN_ALERT_COMMANDS (~$4 / 2 M commands); this
         indicates a bug in the Tier 2 gate since calls shouldn't reach here.
      4. On a successful publish, atomically increments redis_commands_used.

    When tenant_id/db are absent (self-hosted / local mode) the check is
    skipped entirely and the function behaves as before.
    """
    client = await get_redis_client()
    if client is None:
        # 2cf57fde — distinguish "never configured / construction failed"
        # fallbacks from budget-driven fallbacks below, so diagnostics can
        # tell the two apart.
        _redis_diag_state["publish_fallback_unconfigured"] += 1
        return False

    # --- 342dd15f budget enforcement (hosted only) ---------------------------
    if tenant_id is not None and db is not None:
        try:
            used = await _get_redis_commands_used(db, tenant_id)
            if used >= REDIS_BUDGET_DISABLE_COMMANDS:
                if used >= REDIS_BUDGET_ADMIN_ALERT_COMMANDS:
                    # Tier 3 — structurally unreachable; Tier 2 gate failed.
                    _fire_admin_alert_background(tenant_id, used)
                logger.info(
                    "redis_bridge: tenant %s at Redis command budget limit "
                    "(%d/%d), skipping publish",
                    tenant_id, used, REDIS_BUDGET_DISABLE_COMMANDS,
                )
                _redis_diag_state["publish_fallback_budget"] += 1
                return False
        except Exception:  # noqa: BLE001
            # Budget check must never block the call — on any error, proceed.
            logger.warning("redis_bridge: budget check failed, proceeding", exc_info=True)

    _redis_diag_state["publish_attempts"] += 1
    _publish_started = time.monotonic()
    try:
        await client.publish(_channel_for(to_session_id), json.dumps(message, default=str))
        latency_ms = (time.monotonic() - _publish_started) * 1000.0
        _redis_diag_state["latency_ms_samples"].append(latency_ms)
        _redis_diag_state["publish_successes"] += 1
        _redis_diag_state["last_success_at"] = time.time()
        # --- Increment command counter (best-effort) -------------------------
        if tenant_id is not None and db is not None:
            try:
                await _increment_redis_commands(db, tenant_id)
            except Exception:  # noqa: BLE001
                pass  # counter update failure must never affect the publish result
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis_bridge: publish failed, message remains available via receive_messages polling", exc_info=True)
        _redis_diag_state["publish_failures"] += 1
        _redis_diag_state["last_error_class"] = type(exc).__name__
        _redis_diag_state["last_error_at"] = time.time()
        return False


async def _get_redis_commands_used(db: Any, tenant_id: str) -> int:
    """Read the current-month redis_commands_used counter for a tenant.

    Returns 0 on any error (safe-open: prefer under-counting to blocking).
    Supports both dict-row (Postgres) and aiosqlite Row (SQLite) result types.
    """
    try:
        async with db.execute(
            "SELECT redis_commands_used FROM tenants WHERE id = ?",
            (tenant_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return 0
        val = row["redis_commands_used"] if isinstance(row, dict) else row[0]
        return int(val or 0)
    except Exception:  # noqa: BLE001
        return 0


async def _increment_redis_commands(db: Any, tenant_id: str) -> None:
    """Atomically increment redis_commands_used by 1 for a tenant."""
    await db.execute(
        "UPDATE tenants SET redis_commands_used = COALESCE(redis_commands_used, 0) + 1 "
        "WHERE id = ?",
        (tenant_id,),
    )
    # aiosqlite needs an explicit commit; psycopg3 is autocommit so this is a no-op there.
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        pass


def _fire_admin_alert_background(tenant_id: str, used: int) -> None:
    """Fire a Tier-3 admin alert via ntfy + Resend email.

    Runs via asyncio.create_task (fire-and-forget) so the calling publish path
    is not blocked on network I/O. Uses the same MERIDIAN_ADMIN_NTFY_URL /
    ADMIN_EMAIL + RESEND_API_KEY env-var paths as error_alerting.py.
    """
    import asyncio  # noqa: PLC0415

    async def _alert() -> None:
        title = (
            f"[Meridian] Redis Tier-3 budget breach: "
            f"tenant {tenant_id} at {used:,} commands"
        )
        body = (
            f"Tenant {tenant_id} has issued {used:,} Redis PUBLISH commands "
            f"this billing month (Tier-3 absolute ceiling = "
            f"{REDIS_BUDGET_ADMIN_ALERT_COMMANDS:,} commands / ~$4.00 Upstash "
            f"cost). The Tier-2 DISABLE gate "
            f"({REDIS_BUDGET_DISABLE_COMMANDS:,} commands / ~$2.00) should have "
            f"blocked further publishes before reaching this threshold — "
            f"crossing Tier-3 indicates the gate itself may be failing. "
            f"Investigate immediately.\n\n"
            f"Source: meridian redis_bridge Tier-3 admin alert (342dd15f)."
        )
        try:
            ntfy_url = os.environ.get("MERIDIAN_ADMIN_NTFY_URL", "").strip()
            if ntfy_url:
                import httpx  # noqa: PLC0415
                target = ntfy_url if "://" in ntfy_url else f"https://ntfy.sh/{ntfy_url.lstrip('/')}"
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(
                        target,
                        content=body.encode(),
                        headers={
                            "Title": title,
                            "Priority": "urgent",
                            "Tags": "rotating_light,meridian-redis-budget",
                        },
                    )
        except Exception:  # noqa: BLE001
            logger.warning("redis_bridge: Tier-3 ntfy alert failed", exc_info=True)

        try:
            admin_email = os.environ.get("ADMIN_EMAIL", "").strip()
            api_key = os.environ.get("RESEND_API_KEY", "").strip()
            if admin_email and api_key:
                from_addr = os.environ.get(
                    "MERIDIAN_FROM_EMAIL", "Meridian <noreply@usemeridian.us>"
                )
                import httpx  # noqa: PLC0415
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(
                        "https://api.resend.com/emails",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "from": from_addr,
                            "to": [admin_email],
                            "subject": title,
                            "text": body,
                        },
                    )
        except Exception:  # noqa: BLE001
            logger.warning("redis_bridge: Tier-3 email alert failed", exc_info=True)

    try:
        asyncio.create_task(_alert())
    except RuntimeError:
        pass  # no running event loop — test context; alert is best-effort


async def subscribe_session_messages(session_id: str) -> AsyncIterator[dict[str, Any]]:
    """Real listener helper: yields decoded message dicts as they're pushed
    to ``session_id``'s channel. Used by a live subscriber process/script —
    NOT called from the request/response path (send_message/receive_messages
    stay synchronous DB operations; this is purely for a process that wants
    to be woken instead of polling).

    Yields nothing (returns immediately) if Redis isn't configured — callers
    that want push behavior should treat an immediately-exhausted iterator as
    "no push available here, poll receive_messages instead".
    """
    client = await get_redis_client()
    if client is None:
        return
    pubsub = client.pubsub()
    try:
        await pubsub.subscribe(_channel_for(session_id))
        async for raw in pubsub.listen():
            if raw.get("type") != "message":
                continue
            try:
                yield json.loads(raw["data"])
            except (json.JSONDecodeError, TypeError):
                continue
    finally:
        await pubsub.unsubscribe(_channel_for(session_id))
        await pubsub.aclose()


# ---------------------------------------------------------------------------
# 2cf57fde — cache-effectiveness counters for a FUTURE Redis-backed
# read-through cache (board/handoff/diagnostics keys per decision b6b1a0f5).
#
# This module does not itself implement that cache — these are the counter
# hooks it should call once it exists. Until something calls them, they stay
# at zero and get_redis_runtime_diagnostics reports cache.redis_cache.active
# as False, so this diagnostics surface never overclaims Neon/Postgres
# savings that aren't real yet.
# ---------------------------------------------------------------------------

def record_cache_hit() -> None:
    """A read-through Redis cache lookup was served from Redis (DB read avoided)."""
    _redis_diag_state["cache_hits"] += 1


def record_cache_miss() -> None:
    """A read-through Redis cache lookup missed and fell through to the DB."""
    _redis_diag_state["cache_misses"] += 1


def record_cache_set() -> None:
    """A value was written into the Redis cache after a DB read."""
    _redis_diag_state["cache_sets"] += 1


def record_cache_invalidation() -> None:
    """A cache entry was actively invalidated (or, per decision b6b1a0f5's
    content-addressed key design, superseded by a new revision-hash key)."""
    _redis_diag_state["cache_invalidations"] += 1


def _local_process_cache_diagnostics() -> dict[str, Any]:
    """Best-effort snapshot of the genuinely active, already-measurable
    process-local Neon-avoidance cache in meridian/db/sprint_items.py.

    Lazy/late import to avoid a circular import — meridian.db already does a
    lazy import of THIS module (see meridian/db/__init__.py's
    ``from .. import redis_bridge as _redis_bridge  # noqa: PLC0415``), so
    importing meridian.db back at module load time here would deadlock the
    import graph. Never raises — degrades to an unavailable-shaped stub.
    """
    try:
        from .db import sprint_items as _sprint_items_module  # noqa: PLC0415

        return _sprint_items_module.get_sprint_items_cache_diagnostics()
    except Exception:  # noqa: BLE001 — diagnostics must never break on this
        return {"available": False}


def get_redis_runtime_diagnostics(tenant: "dict | None" = None) -> dict[str, Any]:
    """Safe, non-secret Redis runtime-health / cache-effectiveness /
    Neon-avoidance snapshot (2cf57fde; follow-up to investigation 686ab70f,
    finding 9db59816, decision b6b1a0f5).

    Synchronous and side-effect-free — reads only in-process counters plus
    (optionally) the already-fetched ``tenant`` row's own
    ``redis_commands_used`` column, so it is cheap enough to embed in
    :func:`meridian.routes.tunnel.build_tunnel_diagnostics` (itself sync, and
    called on every diagnostics request) without an extra DB round-trip. This
    NEVER performs a live PING/round-trip — ``availability`` is inferred from
    the last recorded success/failure, not a fresh network probe, so calling
    this can never add latency or block on a Redis outage.

    NEVER includes MERIDIAN_REDIS_URL or any credential — only a boolean
    ``configured`` flag derived from whether the env var is set.

    Per-process, not cross-instance: ``connection_generation`` and the
    pub/sub counters reflect only THIS server process — same documented
    limitation as build_tunnel_diagnostics's other per-process fields
    (server_routing_cache, process_leases) and the a1d75ff3 sprint-items
    cache. A multi-instance Fly deployment reports these independently per
    instance; there is no cross-instance aggregation here.

    Distinguishes Pub/Sub push from read-through caching, per the item's
    explicit requirement: ``pubsub`` covers publish_session_message traffic
    (0bfde7ad); ``cache.redis_cache`` covers a Redis-backed read-through
    cache, which this item deliberately does NOT implement (separate sprint
    scope) — the counters exist and are wired for that future cache to call
    (record_cache_hit/miss/set/invalidation above) but read zero with
    ``active: False`` until it lands. ``cache.local_process_cache`` reports
    the mechanism that IS active and measurable today: the process-local TTL
    cache in meridian/db/sprint_items.py.

    ``budget`` (342dd15f tier counters) is per-tenant and only populated when
    a ``tenant`` dict is supplied (hosted mode, e.g. from
    ``_get_tenant_from_request``) — self-hosted/no-tenant callers get
    ``budget: None``, matching every other tenant-scoped diagnostics field.
    """
    configured = bool(os.environ.get("MERIDIAN_REDIS_URL"))
    diag = _redis_diag_state
    last_success = diag["last_success_at"]
    last_error = diag["last_error_at"]

    if not configured:
        availability = "unconfigured"
    elif last_success is not None or last_error is not None:
        # Real evidence from at least one actual publish attempt takes
        # priority over internal client-construction bookkeeping below —
        # this is both more truthful (what actually happened beats what a
        # cache variable holds) and robust to callers/tests that replace
        # get_redis_client() wholesale (this codebase's own established
        # mocking convention — see test_0bfde7ad_redis_push_augmentation.py
        # / test_342dd15f_redis_budget.py), which would otherwise leave
        # _redis_client permanently None despite genuine publish activity.
        if last_error is None:
            availability = "reachable"
        elif last_success is None:
            availability = "unreachable"
        elif last_success >= last_error:
            # >= (not strictly >): an exact tie -- a failure immediately
            # followed by a success within float-clock precision -- means
            # the most recent evidence IS a success, so it must not read as
            # "degraded". Secondary finding, 2cf57fde round-2 fix.
            availability = "reachable"
        else:
            availability = "degraded"
    elif _redis_unavailable:
        # 2cf57fde round-2 fix — reachable now that construction failures no
        # longer touch last_error_at/last_success_at (see get_redis_client()
        # and the dedicated last_construction_error_* fields above/below).
        availability = "construction_failed"
    elif _redis_client is None:
        availability = "idle"  # configured, but no publish attempted yet
    else:
        availability = "connected_unverified"

    samples = list(diag["latency_ms_samples"])
    latency = {
        "sample_count": len(samples),
        "avg_ms": (sum(samples) / len(samples)) if samples else None,
        "max_ms": max(samples) if samples else None,
        "min_ms": min(samples) if samples else None,
    }

    budget = None
    if tenant is not None:
        used = int(tenant.get("redis_commands_used") or 0)
        if used >= REDIS_BUDGET_ADMIN_ALERT_COMMANDS:
            tier = "admin_alert"
        elif used >= REDIS_BUDGET_DISABLE_COMMANDS:
            tier = "disabled"
        elif used >= REDIS_BUDGET_WARN_COMMANDS:
            tier = "warn"
        else:
            tier = "ok"
        budget = {
            "commands_used": used,
            "tier": tier,
            "warn_threshold": REDIS_BUDGET_WARN_COMMANDS,
            "disable_threshold": REDIS_BUDGET_DISABLE_COMMANDS,
            "admin_alert_threshold": REDIS_BUDGET_ADMIN_ALERT_COMMANDS,
        }

    return {
        "configured": configured,
        "availability": availability,
        "connection_generation": diag["connection_generation"],
        "pubsub": {
            "publish_attempts": diag["publish_attempts"],
            "publish_successes": diag["publish_successes"],
            "publish_failures": diag["publish_failures"],
            "fallback_unconfigured_count": diag["publish_fallback_unconfigured"],
            "fallback_budget_count": diag["publish_fallback_budget"],
            "latency_ms": latency,
        },
        "cache": {
            "redis_cache": {
                "active": False,
                "hits": diag["cache_hits"],
                "misses": diag["cache_misses"],
                "sets": diag["cache_sets"],
                "invalidations": diag["cache_invalidations"],
                "reason": (
                    "no Redis-backed read-through cache is wired yet (separate "
                    "sprint scope); counters are live and will populate once "
                    "that cache calls record_cache_hit/miss/set/invalidation"
                ),
            },
            "local_process_cache": _local_process_cache_diagnostics(),
        },
        "last_error_class": diag["last_error_class"],
        "last_error_age_seconds": (
            (time.time() - last_error) if last_error is not None else None
        ),
        # 2cf57fde round-2 fix — separate from last_error_class/last_error_age
        # above (which stay reserved for publish-ATTEMPT evidence): this is
        # the actual exception class/age from a get_redis_client()
        # CONSTRUCTION failure (bad MERIDIAN_REDIS_URL, missing/incompatible
        # redis-py, etc.), so an operator seeing availability ==
        # "construction_failed" gets the real config-vs-network distinction
        # this diagnostics surface exists to provide, not just the label.
        "last_construction_error_class": diag["last_construction_error_class"],
        "last_construction_error_age_seconds": (
            (time.time() - diag["last_construction_error_at"])
            if diag["last_construction_error_at"] is not None else None
        ),
        "budget": budget,
    }
