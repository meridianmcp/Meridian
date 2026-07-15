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
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator

logger = logging.getLogger("meridian.redis_bridge")

_redis_client: Any = None
_redis_unavailable: bool = False

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
    """Test helper — clear the cached client/failure flag between tests."""
    global _redis_client, _redis_unavailable
    _redis_client = None
    _redis_unavailable = False


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
        return _redis_client
    except Exception:  # noqa: BLE001
        logger.warning("redis_bridge: could not construct Redis client, disabling push augmentation", exc_info=True)
        _redis_unavailable = True
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
                return False
        except Exception:  # noqa: BLE001
            # Budget check must never block the call — on any error, proceed.
            logger.warning("redis_bridge: budget check failed, proceeding", exc_info=True)

    try:
        await client.publish(_channel_for(to_session_id), json.dumps(message, default=str))
        # --- Increment command counter (best-effort) -------------------------
        if tenant_id is not None and db is not None:
            try:
                await _increment_redis_commands(db, tenant_id)
            except Exception:  # noqa: BLE001
                pass  # counter update failure must never affect the publish result
        return True
    except Exception:  # noqa: BLE001
        logger.warning("redis_bridge: publish failed, message remains available via receive_messages polling", exc_info=True)
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
