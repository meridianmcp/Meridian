"""POST /zotero/connect: a logged-in user asks Meridian to connect to Zotero.

Each request runs :func:`meridian.zotero_connect.connect` once. It runs on a
worker thread because Zotero can hold the request open while the user answers
its approval dialog. The route needs an explicit ``{"confirm": true}`` JSON body,
so a page that is not Meridian cannot start the dialog with a plain form post.
The 60-second limit lives in ``connect()``, so it holds across requests and
processes.

Auth follows the rest of the dashboard. A hosted deployment requires a logged-in
tenant, and the default-deny gate in ``hosted_route_gate`` also applies to this
path. A self-hosted server has no tenant concept. It answers 403 unless
``MERIDIAN_ZOTERO_CONNECT_ENABLED`` is 1, true or yes (the route is off by
default). With the switch on, it accepts only a loopback client (127.0.0.1 or
::1) and answers any other caller with 403 before ``connect`` runs. It also
answers 403 to any request that carries a forwarding header
(X-Forwarded-For, X-Forwarded-Host, X-Forwarded-Proto, Forwarded or
X-Real-IP): a request relayed by a proxy is not a local use of this route, and
those headers are how a remote caller could pose as loopback. On the hosted tier
``connect`` runs on the server, which cannot reach a workstation's Zotero, so it
reports ``unreachable``. Hosted requests always arrive through the platform proxy,
so the forwarding-header refusal does not apply there.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .. import zotero_connect
from .._deps import _get_tenant_from_request, _hosted_mode

router = APIRouter()

_LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1"})

# D2: the self-hosted route is opt-in. Hosted mode does not read this switch.
_ENABLE_ENV = "MERIDIAN_ZOTERO_CONNECT_ENABLED"
_ENABLE_VALUES = ("1", "true", "yes")

# Lower-case; Starlette's Headers lookup is case-insensitive.
_FORWARDING_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "forwarded",
    "x-real-ip",
)

_HTTP_STATUS_BY_RESULT = {
    "connected": 200,
    "cooldown": 429,
    "rate_limited": 429,
    "denied": 403,
    "local_api_disabled": 503,
    "unreachable": 503,
    "timeout": 504,
    "store_unavailable": 500,
    "error": 502,
}


def _self_hosted_connect_enabled() -> bool:
    """True only when the self-hosted switch is 1, true or yes (case and surrounding space ignored)."""
    return os.environ.get(_ENABLE_ENV, "").strip().lower() in _ENABLE_VALUES


@router.post("/zotero/connect")
async def zotero_connect_route(request: Request) -> JSONResponse:
    """Run one Zotero approval attempt for a logged-in user who explicitly confirmed it."""
    if _hosted_mode():
        if await _get_tenant_from_request(request) is None:
            return JSONResponse({"error": "not authenticated"}, status_code=401)
    elif not _self_hosted_connect_enabled():
        return JSONResponse(
            {"error": f"connect is off on this server: set {_ENABLE_ENV}=1 to turn it on"},
            status_code=403,
        )
    elif request.client is None or request.client.host not in _LOOPBACK_CLIENTS:
        return JSONResponse({"error": "connect runs only from this machine"}, status_code=403)
    elif any(name in request.headers for name in _FORWARDING_HEADERS):
        return JSONResponse({"error": "connect refuses requests relayed by a proxy"}, status_code=403)
    if not request.headers.get("content-type", "").lower().startswith("application/json"):
        return JSONResponse({"error": "send a JSON body with confirm set to true"}, status_code=400)
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or body.get("confirm") is not True:
        return JSONResponse(
            {"error": "confirm must be true: only a user can start Zotero's approval dialog"},
            status_code=400,
        )
    result = await run_in_threadpool(zotero_connect.connect)
    headers = {"Retry-After": str(result["retry_after_s"])} if "retry_after_s" in result else None
    return JSONResponse(
        result,
        status_code=_HTTP_STATUS_BY_RESULT.get(result["status"], 502),
        headers=headers,
    )
