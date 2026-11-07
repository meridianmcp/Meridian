"""GET /workspace/overview: the local, read-only workspace overview.

Repos, codebase-memory indexes, sessions, projects, guard state and tunnel signals
for this machine. The document is built by :func:`meridian.workspace_overview.build_overview`.

Self-hosted only. Hosted deployments get 404 from this route. The app-wide hosted gate
(``meridian/hosted_route_gate.py``) answers anonymous hosted callers with 401 before
this route runs. Self-hosted callers must reach this process directly from the same
machine: the client must be loopback, no forwarding header may be present, and the
``Host`` header must name loopback. That last check is what stops DNS rebinding.
``Sec-Fetch-Site`` and ``Origin``, when sent, must also be same-origin or loopback.
The shared checks live in ``meridian/routes/_local_only.py``.

The response is ``Cache-Control: no-store``.
"""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import _deps
from .._deps import _db
from ..workspace_overview import build_overview
from ._local_only import (  # noqa: F401 - re-exported: callers and tests import the checks from here
    FORWARDED_HEADERS,
    is_loopback_host,
    is_loopback_host_header,
    is_loopback_origin,
    require_local_request,
)

router = APIRouter()
_REFUSED_MESSAGE = "The workspace overview is only available from this machine."


async def require_local_only(request: Request) -> None:
    """Hosted: 404. Self-hosted: the request must be a direct local request."""
    if _deps._hosted_mode():
        raise HTTPException(status_code=404)
    await require_local_request(request, _REFUSED_MESSAGE)


@router.get("/workspace/overview", dependencies=[Depends(require_local_only)])
async def get_workspace_overview(request: Request) -> JSONResponse:
    """Read-only local overview of repos, indexes, sessions, projects and guard state."""
    db = await _db(request)
    body = await build_overview(db, env=dict(os.environ), now=time.time())
    return JSONResponse(body, headers={"Cache-Control": "no-store"})
