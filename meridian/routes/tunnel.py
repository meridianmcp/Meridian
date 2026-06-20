"""Pro permanent tunnel — WebSocket relay + HTTP proxy endpoints.

Architecture:
  - Binary client runs `meridian --tunnel`, opens wss://usemeridian.us/tunnel/{tenant_id}
  - Server registers the socket in _tunnel_sockets, marks tenant.tunnel_active=1
  - When a request hits /fs/mcp/{tenant_id}, server forwards it over the socket
    using JSON request/response correlation (UUID per request)
  - Client receives the JSON request, proxies it to local mcp-proxy on 8808,
    sends back a JSON response with the same correlation ID
  - Pro-only feature ($50/mo). Free/Standard: 403.

Protocol (server → client):
  {"type": "request", "id": "<uuid>", "method": "GET",
   "path": "/sse", "query": "?...", "headers": {...}, "body": null}

Protocol (client → server):
  {"type": "response", "id": "<uuid>", "status": 200,
   "headers": {"content-type": "text/event-stream"}, "body": "<b64>"}
  {"type": "ping"}  ← client keepalive, ignored by server
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from .. import db as db_module
from .._deps import _hosted_mode

router = APIRouter()
_log = logging.getLogger(__name__)

_PROXY_TIMEOUT = 30.0

# Per-process in-memory registry: tenant_id → active WebSocket
_tunnel_sockets: dict[str, WebSocket] = {}
_tunnel_code_sockets: dict[str, WebSocket] = {}
_tunnel_extract_sockets: dict[str, WebSocket] = {}

# Correlation maps: request_id → Future that resolves when client responds
_pending_reqs: dict[str, asyncio.Future[dict]] = {}
_pending_code_reqs: dict[str, asyncio.Future[dict]] = {}
_pending_extract_reqs: dict[str, asyncio.Future[dict]] = {}


def _is_tunnel_allowed(tenant: dict) -> bool:
    """Return True for Pro, admin, and internal tenants."""
    plan = tenant.get("plan") or "free"
    return plan in ("pro", "admin") or bool(tenant.get("is_internal"))


async def _resolve_tenant_from_token(auth_db: Any, token: str | None) -> dict | None:
    """Resolve a tenant from a sk_meridian_ bearer token. Returns None if invalid."""
    if not token:
        return None
    token = token.strip()
    if token.startswith("Bearer "):
        token = token[7:].strip()
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return await db_module.get_tenant_from_token_hash(auth_db, token_hash)


# ---------------------------------------------------------------------------
# /tunnel/{tenant_id}  — WebSocket: binary client holds this open
# ---------------------------------------------------------------------------

@router.websocket("/tunnel/{tenant_id}")
async def tunnel_ws(ws: WebSocket, tenant_id: str) -> None:
    """Hold open a WebSocket for one Pro tenant's local binary.

    Auth: Authorization header or ?token= query param containing a valid
    sk_meridian_... API token belonging to the specified tenant_id.
    """
    if not _hosted_mode():
        await ws.close(code=4403, reason="tunnel requires hosted mode")
        return

    auth_db = ws.app.state.db

    # Accept the socket early so we can send a close frame with a reason.
    await ws.accept()

    # Authenticate: header first, then query param
    auth_header = ws.headers.get("authorization", "")
    token_param = ws.query_params.get("token", "")
    raw_token = auth_header or token_param

    tenant = await _resolve_tenant_from_token(auth_db, raw_token)
    if tenant is None or tenant.get("id") != tenant_id:
        await ws.close(code=4401, reason="invalid or mismatched token")
        return

    if not _is_tunnel_allowed(tenant):
        await ws.close(code=4403, reason="tunnel requires Pro plan")
        return

    # Evict any stale socket for this tenant (e.g. binary restarted)
    old_ws = _tunnel_sockets.pop(tenant_id, None)
    if old_ws is not None:
        try:
            await old_ws.close(code=4000, reason="replaced by new connection")
        except Exception:
            pass

    _tunnel_sockets[tenant_id] = ws
    try:
        await db_module.update_tenant(auth_db, tenant_id, tunnel_active=1)
    except Exception:
        pass

    _log.info("tunnel: tenant %s connected", tenant_id[:8])

    try:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=120.0)
            except asyncio.TimeoutError:
                # Send keepalive ping
                try:
                    await ws.send_json({"type": "ping"})
                except Exception:
                    break
                continue

            if not isinstance(msg, dict):
                continue

            msg_type = msg.get("type")
            if msg_type == "ping":
                continue

            if msg_type == "response":
                req_id = msg.get("id")
                fut = _pending_reqs.get(req_id)
                if fut is not None and not fut.done():
                    fut.set_result(msg)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        _log.debug("tunnel: tenant %s disconnected: %s", tenant_id[:8], exc)
    finally:
        _tunnel_sockets.pop(tenant_id, None)
        # Cancel any in-flight proxy requests for this tenant
        for fut in list(_pending_reqs.values()):
            if not fut.done():
                fut.cancel()
        try:
            await db_module.update_tenant(auth_db, tenant_id, tunnel_active=0)
        except Exception:
            pass
        _log.info("tunnel: tenant %s disconnected", tenant_id[:8])


# ---------------------------------------------------------------------------
# /tunnel-code/{tenant_id}  — WebSocket: codebase-memory-mcp tunnel
# ---------------------------------------------------------------------------

@router.websocket("/tunnel-code/{tenant_id}")
async def tunnel_code_ws(ws: WebSocket, tenant_id: str) -> None:
    """Hold open a WebSocket for one tenant's codebase-memory-mcp proxy.

    Mirrors /tunnel/{tenant_id} but routes through _tunnel_code_sockets so
    filesystem and code-intel tunnels are independent. Auth rules are identical.
    """
    if not _hosted_mode():
        await ws.close(code=4403, reason="tunnel requires hosted mode")
        return

    auth_db = ws.app.state.db
    await ws.accept()

    auth_header = ws.headers.get("authorization", "")
    token_param = ws.query_params.get("token", "")
    raw_token = auth_header or token_param

    tenant = await _resolve_tenant_from_token(auth_db, raw_token)
    if tenant is None or tenant.get("id") != tenant_id:
        await ws.close(code=4401, reason="invalid or mismatched token")
        return

    if not _is_tunnel_allowed(tenant):
        await ws.close(code=4403, reason="tunnel requires Pro plan")
        return

    old_ws = _tunnel_code_sockets.pop(tenant_id, None)
    if old_ws is not None:
        try:
            await old_ws.close(code=4000, reason="replaced by new connection")
        except Exception:
            pass

    _tunnel_code_sockets[tenant_id] = ws
    _log.info("tunnel-code: tenant %s connected", tenant_id[:8])

    try:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=120.0)
            except asyncio.TimeoutError:
                try:
                    await ws.send_json({"type": "ping"})
                except Exception:
                    break
                continue

            if not isinstance(msg, dict):
                continue
            msg_type = msg.get("type")
            if msg_type == "ping":
                continue
            if msg_type == "response":
                req_id = msg.get("id")
                fut = _pending_code_reqs.get(req_id)
                if fut is not None and not fut.done():
                    fut.set_result(msg)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        _log.debug("tunnel-code: tenant %s disconnected: %s", tenant_id[:8], exc)
    finally:
        _tunnel_code_sockets.pop(tenant_id, None)
        for fut in list(_pending_code_reqs.values()):
            if not fut.done():
                fut.cancel()
        _log.info("tunnel-code: tenant %s disconnected", tenant_id[:8])


# ---------------------------------------------------------------------------
# /tunnel-extract/{tenant_id}  — WebSocket: mcp-server-code-extractor tunnel
# ---------------------------------------------------------------------------

@router.websocket("/tunnel-extract/{tenant_id}")
async def tunnel_extract_ws(ws: WebSocket, tenant_id: str) -> None:
    """Hold open a WebSocket for one tenant's mcp-server-code-extractor proxy.

    Mirrors /tunnel-code/{tenant_id}. Auth rules are identical.
    """
    if not _hosted_mode():
        await ws.close(code=4403, reason="tunnel requires hosted mode")
        return

    auth_db = ws.app.state.db
    await ws.accept()

    auth_header = ws.headers.get("authorization", "")
    token_param = ws.query_params.get("token", "")
    raw_token = auth_header or token_param

    tenant = await _resolve_tenant_from_token(auth_db, raw_token)
    if tenant is None or tenant.get("id") != tenant_id:
        await ws.close(code=4401, reason="invalid or mismatched token")
        return

    if not _is_tunnel_allowed(tenant):
        await ws.close(code=4403, reason="tunnel requires Pro plan")
        return

    old_ws = _tunnel_extract_sockets.pop(tenant_id, None)
    if old_ws is not None:
        try:
            await old_ws.close(code=4000, reason="replaced by new connection")
        except Exception:
            pass

    _tunnel_extract_sockets[tenant_id] = ws
    _log.info("tunnel-extract: tenant %s connected", tenant_id[:8])

    try:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=120.0)
            except asyncio.TimeoutError:
                try:
                    await ws.send_json({"type": "ping"})
                except Exception:
                    break
                continue

            if not isinstance(msg, dict):
                continue
            msg_type = msg.get("type")
            if msg_type == "ping":
                continue
            if msg_type == "response":
                req_id = msg.get("id")
                fut = _pending_extract_reqs.get(req_id)
                if fut is not None and not fut.done():
                    fut.set_result(msg)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        _log.debug("tunnel-extract: tenant %s disconnected: %s", tenant_id[:8], exc)
    finally:
        _tunnel_extract_sockets.pop(tenant_id, None)
        for fut in list(_pending_extract_reqs.values()):
            if not fut.done():
                fut.cancel()
        _log.info("tunnel-extract: tenant %s disconnected", tenant_id[:8])


# ---------------------------------------------------------------------------
# Shared proxy helper
# ---------------------------------------------------------------------------

async def _do_proxy(
    tenant_id: str,
    method: str,
    path: str,
    query: str,
    headers: dict[str, str],
    body_bytes: bytes | None,
    sockets: dict[str, WebSocket],
    pending: dict[str, "asyncio.Future[dict]"],
    label: str,
) -> Response:
    """Forward one HTTP request over the given tunnel socket and return the response."""
    ws = sockets.get(tenant_id)
    if ws is None:
        return Response(
            content=f'{{"error":"{label} tunnel not connected"}}',
            status_code=503,
            media_type="application/json",
        )

    req_id = str(uuid.uuid4())
    loop = asyncio.get_event_loop()
    fut: asyncio.Future[dict] = loop.create_future()
    pending[req_id] = fut

    body_b64: str | None = None
    if body_bytes:
        body_b64 = base64.b64encode(body_bytes).decode()

    payload: dict[str, Any] = {
        "type": "request",
        "id": req_id,
        "method": method,
        "path": path,
        "query": query,
        "headers": headers,
        "body": body_b64,
    }

    try:
        await ws.send_json(payload)
        resp_msg = await asyncio.wait_for(fut, timeout=_PROXY_TIMEOUT)
    except asyncio.TimeoutError:
        pending.pop(req_id, None)
        return Response(
            content='{"error":"tunnel timeout"}',
            status_code=504,
            media_type="application/json",
        )
    except Exception as exc:
        pending.pop(req_id, None)
        _log.debug("%s proxy error for %s: %s", label, tenant_id[:8], exc)
        return Response(
            content='{"error":"tunnel error"}',
            status_code=502,
            media_type="application/json",
        )
    finally:
        pending.pop(req_id, None)

    status = int(resp_msg.get("status", 502))
    resp_headers = resp_msg.get("headers") or {}
    resp_body_b64 = resp_msg.get("body") or ""
    try:
        resp_body = base64.b64decode(resp_body_b64) if resp_body_b64 else b""
    except Exception:
        resp_body = b""

    _hop = {"transfer-encoding", "connection", "keep-alive", "content-length"}
    safe_headers: dict[str, str] = {
        k: v for k, v in resp_headers.items() if k.lower() not in _hop
    }
    return Response(content=resp_body, status_code=status, headers=safe_headers)


# ---------------------------------------------------------------------------
# /fs/mcp/{tenant_id}  — HTTP: proxies through the filesystem tunnel socket
# ---------------------------------------------------------------------------

async def _proxy_request(
    tenant_id: str,
    method: str,
    path: str,
    query: str,
    headers: dict[str, str],
    body_bytes: bytes | None,
) -> Response:
    return await _do_proxy(tenant_id, method, path, query, headers, body_bytes,
                           _tunnel_sockets, _pending_reqs, "fs")


@router.get("/fs/mcp/{tenant_id}")
@router.post("/fs/mcp/{tenant_id}")
@router.options("/fs/mcp/{tenant_id}")
async def fs_mcp_proxy(tenant_id: str, request: Request) -> Response:
    """Proxy a GET/POST/OPTIONS request through the tenant's active tunnel socket.

    This is the permanent URL users add to claude.ai as a filesystem MCP connector.
    Requires an active tunnel connection from the tenant's local `meridian --tunnel`
    process. Returns 503 when no tunnel is open.
    """
    if not _hosted_mode():
        return Response(
            content='{"error":"tunnel requires hosted mode"}',
            status_code=503,
            media_type="application/json",
        )

    body_bytes = await request.body()
    # Forward a safe subset of request headers; strip host/auth to avoid loops
    _skip = {"host", "authorization", "cookie", "x-forwarded-for"}
    fwd_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _skip
    }

    path = request.url.path
    # Strip the /fs/mcp/{tenant_id} prefix so the local proxy sees a clean path
    prefix = f"/fs/mcp/{tenant_id}"
    local_path = path[len(prefix):] or "/"

    query = str(request.url.query)

    return await _proxy_request(
        tenant_id=tenant_id,
        method=request.method,
        path=local_path,
        query=query,
        headers=fwd_headers,
        body_bytes=body_bytes or None,
    )


# ---------------------------------------------------------------------------
# /fs/mcp/{tenant_id}/{rest:path}  — catch sub-paths like /sse, /message, etc.
# ---------------------------------------------------------------------------

@router.get("/fs/mcp/{tenant_id}/{rest:path}")
@router.post("/fs/mcp/{tenant_id}/{rest:path}")
@router.options("/fs/mcp/{tenant_id}/{rest:path}")
async def fs_mcp_proxy_subpath(tenant_id: str, rest: str, request: Request) -> Response:
    """Same as fs_mcp_proxy but for sub-paths under the MCP root."""
    if not _hosted_mode():
        return Response(
            content='{"error":"tunnel requires hosted mode"}',
            status_code=503,
            media_type="application/json",
        )

    body_bytes = await request.body()
    _skip = {"host", "authorization", "cookie", "x-forwarded-for"}
    fwd_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _skip
    }

    local_path = f"/{rest}" if rest else "/"
    query = str(request.url.query)

    return await _proxy_request(
        tenant_id=tenant_id,
        method=request.method,
        path=local_path,
        query=query,
        headers=fwd_headers,
        body_bytes=body_bytes or None,
    )


# ---------------------------------------------------------------------------
# /code/mcp/{tenant_id}  — HTTP: proxies through the codebase-memory-mcp tunnel
# ---------------------------------------------------------------------------

def _fwd_headers(request: Request) -> dict[str, str]:
    _skip = {"host", "authorization", "cookie", "x-forwarded-for"}
    return {k: v for k, v in request.headers.items() if k.lower() not in _skip}


async def _code_proxy(tenant_id: str, local_path: str, request: Request) -> Response:
    if not _hosted_mode():
        return Response(
            content='{"error":"tunnel requires hosted mode"}',
            status_code=503,
            media_type="application/json",
        )
    body_bytes = await request.body()
    return await _do_proxy(
        tenant_id=tenant_id,
        method=request.method,
        path=local_path,
        query=str(request.url.query),
        headers=_fwd_headers(request),
        body_bytes=body_bytes or None,
        sockets=_tunnel_code_sockets,
        pending=_pending_code_reqs,
        label="code",
    )


@router.get("/code/mcp/{tenant_id}")
@router.post("/code/mcp/{tenant_id}")
@router.options("/code/mcp/{tenant_id}")
async def code_mcp_proxy(tenant_id: str, request: Request) -> Response:
    """Proxy requests to the tenant's codebase-memory-mcp over the code tunnel."""
    prefix = f"/code/mcp/{tenant_id}"
    local_path = request.url.path[len(prefix):] or "/"
    return await _code_proxy(tenant_id, local_path, request)


@router.get("/code/mcp/{tenant_id}/{rest:path}")
@router.post("/code/mcp/{tenant_id}/{rest:path}")
@router.options("/code/mcp/{tenant_id}/{rest:path}")
async def code_mcp_proxy_subpath(tenant_id: str, rest: str, request: Request) -> Response:
    """Same as code_mcp_proxy but for sub-paths."""
    local_path = f"/{rest}" if rest else "/"
    return await _code_proxy(tenant_id, local_path, request)


# ---------------------------------------------------------------------------
# /extract/mcp/{tenant_id}  — HTTP: proxies through the code-extractor tunnel
# ---------------------------------------------------------------------------

async def _extract_proxy(tenant_id: str, local_path: str, request: Request) -> Response:
    if not _hosted_mode():
        return Response(
            content='{"error":"tunnel requires hosted mode"}',
            status_code=503,
            media_type="application/json",
        )
    body_bytes = await request.body()
    return await _do_proxy(
        tenant_id=tenant_id,
        method=request.method,
        path=local_path,
        query=str(request.url.query),
        headers=_fwd_headers(request),
        body_bytes=body_bytes or None,
        sockets=_tunnel_extract_sockets,
        pending=_pending_extract_reqs,
        label="extract",
    )


@router.get("/extract/mcp/{tenant_id}")
@router.post("/extract/mcp/{tenant_id}")
@router.options("/extract/mcp/{tenant_id}")
async def extract_mcp_proxy(tenant_id: str, request: Request) -> Response:
    """Proxy requests to the tenant's mcp-server-code-extractor over the extract tunnel."""
    prefix = f"/extract/mcp/{tenant_id}"
    local_path = request.url.path[len(prefix):] or "/"
    return await _extract_proxy(tenant_id, local_path, request)


@router.get("/extract/mcp/{tenant_id}/{rest:path}")
@router.post("/extract/mcp/{tenant_id}/{rest:path}")
@router.options("/extract/mcp/{tenant_id}/{rest:path}")
async def extract_mcp_proxy_subpath(tenant_id: str, rest: str, request: Request) -> Response:
    """Same as extract_mcp_proxy but for sub-paths."""
    local_path = f"/{rest}" if rest else "/"
    return await _extract_proxy(tenant_id, local_path, request)


# ---------------------------------------------------------------------------
# GET /tunnel/status/{tenant_id}  — lightweight status check (no auth required)
# ---------------------------------------------------------------------------

@router.get("/tunnel/status/{tenant_id}")
async def tunnel_status(tenant_id: str) -> dict:
    """Return whether the tenant currently has an active tunnel socket."""
    return {
        "tenant_id": tenant_id,
        "active": tenant_id in _tunnel_sockets,
        "code_active": tenant_id in _tunnel_code_sockets,
        "extract_active": tenant_id in _tunnel_extract_sockets,
    }
