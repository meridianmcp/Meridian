"""Request timing for the per-tenant / per-Neon-pool telemetry (item 1b2fbebe).

A pure ASGI middleware (not ``BaseHTTPMiddleware``: that buffers responses and would interfere
with the SSE and streaming routes). It measures the time from the request arriving to the
response *headers* being sent, which is the handler's own work including its database
queries, and is therefore the latency a noisy neighbour on a shared Neon endpoint would
inflate. Streaming bodies and long-lived SSE connections are deliberately not included.

``_deps._db`` stashes ``(tenant_id, neon_project_id)`` on ``request.state`` once it has
resolved a hosted tenant's database. Requests that never resolve one (static files, public
pages, health probes, self-hosted mode) leave nothing to attribute and are not recorded.

Telemetry must never affect a request: everything here after the downstream app is called is
wrapped so that a failure is swallowed, and the response is always forwarded unchanged.
"""

from __future__ import annotations

import time

from .pool_telemetry import record_request

# Key under scope["state"] (== request.state._pool_attr) written by _deps._note_pool_tenant.
POOL_ATTR_KEY = "_pool_attr"


class PoolTimingMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        recorded = False

        async def timed_send(message) -> None:
            nonlocal recorded
            if not recorded and message.get("type") == "http.response.start":
                recorded = True
                try:
                    attr = (scope.get("state") or {}).get(POOL_ATTR_KEY)
                    if attr:
                        tenant_id, pool_project_id = attr
                        record_request(
                            tenant_id,
                            pool_project_id,
                            duration_ms=(time.perf_counter() - started) * 1000.0,
                            error=int(message.get("status", 0)) >= 500,
                        )
                except Exception:
                    pass
            await send(message)

        await self.app(scope, receive, timed_send)
