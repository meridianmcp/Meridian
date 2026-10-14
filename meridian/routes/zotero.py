"""POST /zotero/connect: a logged-in user asks Meridian to connect to Zotero.

Each request runs :func:`meridian.zotero_connect.connect` once. It runs on a
worker thread because Zotero can hold the request open while the user answers
its approval dialog. The route needs an explicit ``{"confirm": true}`` JSON body,
so a page that is not Meridian cannot start the dialog with a plain form post.
The 60-second limit lives in ``connect()``, so it holds across requests and
processes.

Auth follows the rest of the dashboard. A hosted deployment requires a logged-in
tenant, and the default-deny gate in ``hosted_route_gate`` also applies to this
path. A self-hosted server has no tenant concept and is local-only by design.
On the hosted tier ``connect`` runs on the server, which cannot reach a
workstation's Zotero, so it reports ``unreachable``.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .. import zotero_connect
from .._deps import _get_tenant_from_request, _hosted_mode

router = APIRouter()

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


@router.post("/zotero/connect")
async def zotero_connect_route(request: Request) -> JSONResponse:
    """Run one Zotero approval attempt for a logged-in user who explicitly confirmed it."""
    if _hosted_mode() and await _get_tenant_from_request(request) is None:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
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
