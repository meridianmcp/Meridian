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

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..mcp.handlers.runpod_tools import (
    _check_payload,
    _error,
    _hosted_mode,
    _list_payload,
    handle_runpod_check_key,
    handle_runpod_list_pods,
)

router = APIRouter()

_HOSTED_ERROR_CODE = "hosted_refused"
_LOCAL_ONLY_ERROR_CODE = "local_only"

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
