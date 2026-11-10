"""GET /compute/runpod/check and GET /compute/runpod/pods: the RunPod key check and
pod listing over HTTP. Each route calls the same handler as its MCP tool
(runpod_check_key, runpod_list_pods) and returns the same JSON.

The routes resolve the default credential chain only (env:RUNPOD_API_KEY, then the
keyring entry, then runpodctl:default). They take no credential_ref parameter. A
query string is written to access logs, so a raw key pasted into one would be
persisted there. An explicit reference is an MCP argument, which travels in the
JSON-RPC body instead.

Status: 200 for every completed check, including ok false (the body's error says
why). 403 is returned before any credential is read in these cases:

* Self-hosted: a caller that is not on loopback (127.0.0.1 or ::1), or a request that
  carries a forwarding header (X-Forwarded-For, X-Forwarded-Host, X-Forwarded-Proto,
  Forwarded or X-Real-IP). The error code is ``local_only`` and the handler is not
  called. This is the rule routes/zotero.py applies. A request relayed by a local
  proxy arrives from loopback, so the forwarding headers are refused as well. Without
  this gate, anyone who can reach the port could list pods and trigger billing reads.
* Hosted: the handler refuses (error code ``hosted_refused``). The loopback rule does
  not apply on hosted, so that path is unchanged. On a hosted deployment the app-wide
  route gate (meridian/hosted_route_gate.py) also answers anonymous callers of this
  path before the handler runs.

The pod listing returns one page of up to 1000 pods. Cluster member pods are not
included: RunPod's includeClusterPods parameter defaults to false, and the client
never sends it.
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..compute_catalog import OffersCache, QueryError, build_offers_view, parse_offers_query
from ..mcp.handlers.runpod_tools import (
    _check_payload,
    _error,
    _hosted_mode,
    _list_payload,
    handle_runpod_check_key,
    handle_runpod_list_pods,
)
from ..research.providers.runpod_catalog import RUNPOD_PROVIDER, RunPodOffersSource

router = APIRouter()

_HOSTED_ERROR_CODE = "hosted_refused"
_LOCAL_ONLY_ERROR_CODE = "local_only"
_OFFERS_HOSTED_MESSAGE = "GPU offers are available on local installs only."
# GPU offers read the RunPod key from the environment only (decided 2026-11-10).
# The MCP tools keep the full chain (env, keyring, runpodctl); this route does not.
OFFERS_CREDENTIAL_REFS: tuple[str, ...] = ("env:RUNPOD_API_KEY",)

_offers_cache_lock = threading.Lock()
_offers_cache_instance: OffersCache | None = None

# The rule routes/zotero.py applies inline. There is no shared helper for it yet, so the
# same two sets are kept here.
_LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1"})
_FORWARDING_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "forwarded",
    "x-real-ip",
)


def _as_response(result: dict[str, Any]) -> JSONResponse:
    error = result.get("error")
    refused = isinstance(error, dict) and error.get("code") == _HOSTED_ERROR_CODE
    return JSONResponse(result, status_code=403 if refused else 200)


def _refuse_remote_caller(
    request: Request, payload: Callable[[str | None, dict[str, Any]], dict[str, Any]],
) -> JSONResponse | None:
    """Return a 403 for a self-hosted caller that is not on loopback or whose request was
    relayed by a proxy, or None when the caller may go on. The 403 body has the same shape
    as the tool's result. On hosted this always returns None, so the handler's own hosted
    refusal answers, exactly as before."""
    if _hosted_mode():
        return None
    if request.client is None or request.client.host not in _LOOPBACK_CLIENTS:
        message = "The RunPod routes accept requests only from this machine."
    elif any(name in request.headers for name in _FORWARDING_HEADERS):
        message = "The RunPod routes refuse requests relayed by a proxy."
    else:
        return None
    return JSONResponse(
        payload(None, _error(_LOCAL_ONLY_ERROR_CODE, message)), status_code=403,
    )


@router.get("/compute/runpod/check")
async def runpod_check_key_route(request: Request) -> JSONResponse:
    """Check that the default RunPod credential works (one billing call, one pod-listing call)."""
    refused = _refuse_remote_caller(request, _check_payload)
    if refused is not None:
        return refused
    return _as_response(await handle_runpod_check_key({}))


@router.get("/compute/runpod/pods")
async def runpod_list_pods_route(request: Request) -> JSONResponse:
    """List one page (up to 1000) of the account's RunPod pods as summaries.

    Cluster member pods are not included: RunPod's includeClusterPods defaults to false and is not sent."""
    refused = _refuse_remote_caller(request, _list_payload)
    if refused is not None:
        return refused
    return _as_response(await handle_runpod_list_pods({}))


def _offers_cache() -> OffersCache:
    """The process-wide offers cache, built on first use with the env-only offers credential ref."""
    global _offers_cache_instance
    with _offers_cache_lock:
        if _offers_cache_instance is None:
            source = RunPodOffersSource(credential_refs=OFFERS_CREDENTIAL_REFS, hosted=False)
            _offers_cache_instance = OffersCache({RUNPOD_PROVIDER: source})
        return _offers_cache_instance


def _offers_payload(kind: "str | None", error: dict[str, Any]) -> dict[str, Any]:
    """The body for a refused or invalid offers request. It has the same keys as a successful one."""
    return {
        "ok": False,
        "status": "error",
        "credential_source": kind,
        "error": error,
        "providers": [],
        "offers": [],
        "count": 0,
        "gpu_models": [],
        "filters_applied": {},
    }


@router.get("/compute/offers")
async def compute_offers_route(request: Request) -> JSONResponse:
    """GPU offers from the live RunPod catalog, filtered and sorted server-side.

    Read only: nothing is created, stopped or billed. Prices come from RunPod's list prices, and stock comes from
    the per-cloud availability reads. Results come from an in-process cache (five minutes by default). A failed
    refresh serves the last good data with status ``stale``. A missing key gives status ``not_configured``. Both
    are HTTP 200, so the dashboard can show each state.

    Refusals: a caller that is not on loopback, or whose request was relayed by a proxy, gets 403 ``local_only``.
    On hosted, the route gives 403 ``hosted_refused`` before any credential is read. Any unknown query parameter
    gives 400 ``unknown_parameter``, and any invalid value gives 400 ``invalid_filter``.
    """
    refused = _refuse_remote_caller(request, _offers_payload)
    if refused is not None:
        return refused
    if _hosted_mode():
        return JSONResponse(
            _offers_payload(None, _error(_HOSTED_ERROR_CODE, _OFFERS_HOSTED_MESSAGE)), status_code=403,
        )
    try:
        query = parse_offers_query(
            {name: request.query_params.getlist(name) for name in request.query_params.keys()}
        )
    except QueryError as exc:
        return JSONResponse(_offers_payload(None, _error(exc.code, exc.message)), status_code=400)
    snapshot = await asyncio.to_thread(_offers_cache().get, query.provider)
    view = build_offers_view([snapshot], query)
    view["credential_source"] = snapshot.credential_source
    return JSONResponse(view, status_code=200)
