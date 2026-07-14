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
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator

logger = logging.getLogger("meridian.redis_bridge")

_redis_client: Any = None
_redis_unavailable: bool = False


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


async def publish_session_message(to_session_id: str, message: dict[str, Any]) -> bool:
    """Best-effort push: publish ``message`` (JSON-encoded) to the recipient
    session's channel. Returns True on a real publish, False in every
    no-op/failure case. NEVER raises — callers (send_message) must not have
    their own DB-write success depend on this."""
    client = await get_redis_client()
    if client is None:
        return False
    try:
        await client.publish(_channel_for(to_session_id), json.dumps(message, default=str))
        return True
    except Exception:  # noqa: BLE001
        logger.warning("redis_bridge: publish failed, message remains available via receive_messages polling", exc_info=True)
        return False


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
