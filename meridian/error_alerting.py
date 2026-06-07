"""Item 39 — 5xx counter + admin alert when error rate exceeds threshold.

Process-local sliding window. The HTTP middleware in :mod:`meridian.server`
calls :func:`record_5xx` for every 5xx response (and unhandled exceptions),
and this module dispatches a best-effort admin alert (ntfy + email) once the
count over the window exceeds the threshold — subject to a cooldown so we
don't spam the admin during a sustained incident.

Env vars:
  - ``MERIDIAN_5XX_ALERT_THRESHOLD``  errors per window (default 10)
  - ``MERIDIAN_5XX_ALERT_WINDOW_SECS`` window length (default 300)
  - ``MERIDIAN_5XX_ALERT_COOLDOWN_SECS`` quiet period after an alert (default 900)
  - ``MERIDIAN_ADMIN_NTFY_URL``  ntfy.sh URL or bare topic (optional)
  - ``ADMIN_EMAIL``  recipient for Resend email (optional)
  - ``RESEND_API_KEY``  required for the email path
  - ``MERIDIAN_FROM_EMAIL``  defaults to ``Meridian <noreply@usemeridian.us>``
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from typing import Any, Callable

logger = logging.getLogger(__name__)

_5xx_events: deque[tuple[float, str, str | None, int]] = deque()
_lock = asyncio.Lock()
_last_alert_ts: float = 0.0

# Test hook — replaced by tests to capture alert dispatches without HTTP I/O.
_dispatch_hook: Callable[[dict[str, Any]], Any] | None = None


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except ValueError:
        return default


def _now() -> float:
    return time.monotonic()


async def record_5xx(route: str, tenant: str | None, status: int) -> None:
    """Record a 5xx response. Fires the admin alert when threshold + cooldown allow.

    Threshold breach dispatches via :func:`asyncio.create_task` so the calling
    request isn't blocked on network I/O to ntfy / Resend.
    """
    global _last_alert_ts
    threshold = _cfg_int("MERIDIAN_5XX_ALERT_THRESHOLD", 10)
    window = _cfg_int("MERIDIAN_5XX_ALERT_WINDOW_SECS", 300)
    cooldown = _cfg_int("MERIDIAN_5XX_ALERT_COOLDOWN_SECS", 900)
    now = _now()

    snapshot_count = 0
    should_fire = False
    async with _lock:
        _5xx_events.append((now, route, tenant, status))
        cutoff = now - window
        while _5xx_events and _5xx_events[0][0] < cutoff:
            _5xx_events.popleft()
        snapshot_count = len(_5xx_events)
        if snapshot_count >= threshold and (now - _last_alert_ts) >= cooldown:
            should_fire = True
            _last_alert_ts = now

    if should_fire:
        payload = {
            "count": snapshot_count,
            "window_secs": window,
            "last_route": route,
            "last_tenant": tenant,
            "last_status": status,
        }
        if _dispatch_hook is not None:
            try:
                result = _dispatch_hook(payload)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:  # noqa: BLE001
                logger.warning("5xx alert hook raised", exc_info=True)
        else:
            asyncio.create_task(_send_alert(payload))


async def _send_alert(payload: dict[str, Any]) -> None:
    title = (
        f"[Meridian] {payload['count']} 5xx in "
        f"{payload['window_secs'] // 60} min"
    )
    last_route = payload["last_route"]
    last_tenant = payload["last_tenant"]
    last_status = payload["last_status"]
    body = (
        f"{payload['count']} server errors (5xx) in the last "
        f"{payload['window_secs']} seconds.\n\n"
        f"Most recent: {last_status} on {last_route}"
        + (f" (tenant: {last_tenant})" if last_tenant else "")
        + "\n\nSource: meridian middleware error alerting."
    )

    ntfy_url = os.environ.get("MERIDIAN_ADMIN_NTFY_URL", "").strip()
    admin_email = os.environ.get("ADMIN_EMAIL", "").strip()

    tasks: list[Any] = []
    if ntfy_url:
        tasks.append(_send_ntfy(ntfy_url, title, body))
    if admin_email:
        tasks.append(_send_email(admin_email, title, body))

    if not tasks:
        logger.warning(
            "5xx threshold exceeded but no admin notifier configured "
            "(MERIDIAN_ADMIN_NTFY_URL / ADMIN_EMAIL)"
        )
        return
    await asyncio.gather(*tasks, return_exceptions=True)


async def _send_ntfy(url: str, title: str, body: str) -> None:
    try:
        import httpx  # noqa: PLC0415
        target = url if "://" in url else f"https://ntfy.sh/{url.lstrip('/')}"
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                target,
                content=body.encode(),
                headers={
                    "Title": title,
                    "Priority": "high",
                    "Tags": "warning,meridian-5xx",
                },
            )
    except Exception:  # noqa: BLE001
        logger.warning("ntfy 5xx alert post failed", exc_info=True)


async def _send_email(to_addr: str, subject: str, body: str) -> None:
    try:
        api_key = os.environ.get("RESEND_API_KEY", "").strip()
        if not api_key:
            return
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
                    "to": [to_addr],
                    "subject": subject,
                    "text": body,
                },
            )
    except Exception:  # noqa: BLE001
        logger.warning("admin email 5xx alert failed", exc_info=True)


def _reset_for_tests() -> None:
    """Test hook — clear the rolling window + cooldown timestamp."""
    global _last_alert_ts, _dispatch_hook
    _5xx_events.clear()
    _last_alert_ts = 0.0
    _dispatch_hook = None


def _set_dispatch_hook(hook: Callable[[dict[str, Any]], Any] | None) -> None:
    """Test hook — replace the network dispatcher with a callable that
    receives the payload dict. Reset via :func:`_reset_for_tests`.
    """
    global _dispatch_hook
    _dispatch_hook = hook
