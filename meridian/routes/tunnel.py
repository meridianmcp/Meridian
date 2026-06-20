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
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from .. import db as db_module
from .._deps import _hosted_mode, _get_tenant_from_request, _db
from ..tunnel_plugins import normalize_plugins_config, resolve_plugins

router = APIRouter()
_log = logging.getLogger(__name__)

_PROXY_TIMEOUT = 30.0

# Per-process in-memory registry: tenant_id → active WebSocket
_tunnel_sockets: dict[str, WebSocket] = {}
_tunnel_code_sockets: dict[str, WebSocket] = {}
_tunnel_extract_sockets: dict[str, WebSocket] = {}
_tunnel_ppt_sockets: dict[str, WebSocket] = {}
_tunnel_word_sockets: dict[str, WebSocket] = {}

# Correlation maps: request_id → Future that resolves when client responds
_pending_reqs: dict[str, asyncio.Future[dict]] = {}
_pending_code_reqs: dict[str, asyncio.Future[dict]] = {}
_pending_extract_reqs: dict[str, asyncio.Future[dict]] = {}
_pending_ppt_reqs: dict[str, asyncio.Future[dict]] = {}
_pending_word_reqs: dict[str, asyncio.Future[dict]] = {}


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
# /tunnel-ppt/{tenant_id} and /tunnel-word/{tenant_id}  — Office MCP tunnels
# ---------------------------------------------------------------------------
#
# These mirror /tunnel-extract exactly. They share one helper to avoid
# duplicating the auth + receive-loop body a fourth and fifth time.

async def _serve_tunnel_ws(
    ws: WebSocket,
    tenant_id: str,
    sockets: dict[str, WebSocket],
    pending: dict[str, "asyncio.Future[dict]"],
    label: str,
) -> None:
    """Auth a tunnel WebSocket, register it, and relay client responses.

    Identical protocol to tunnel_extract_ws — the per-slot socket and pending
    registries are passed in so one body serves any Office/code slot.
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

    old_ws = sockets.pop(tenant_id, None)
    if old_ws is not None:
        try:
            await old_ws.close(code=4000, reason="replaced by new connection")
        except Exception:
            pass

    sockets[tenant_id] = ws
    _log.info("tunnel-%s: tenant %s connected", label, tenant_id[:8])

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
                fut = pending.get(req_id)
                if fut is not None and not fut.done():
                    fut.set_result(msg)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        _log.debug("tunnel-%s: tenant %s disconnected: %s", label, tenant_id[:8], exc)
    finally:
        sockets.pop(tenant_id, None)
        for fut in list(pending.values()):
            if not fut.done():
                fut.cancel()
        _log.info("tunnel-%s: tenant %s disconnected", label, tenant_id[:8])


@router.websocket("/tunnel-ppt/{tenant_id}")
async def tunnel_ppt_ws(ws: WebSocket, tenant_id: str) -> None:
    """Hold open a WebSocket for one tenant's powerpoint-mcp proxy."""
    await _serve_tunnel_ws(ws, tenant_id, _tunnel_ppt_sockets, _pending_ppt_reqs, "ppt")


@router.websocket("/tunnel-word/{tenant_id}")
async def tunnel_word_ws(ws: WebSocket, tenant_id: str) -> None:
    """Hold open a WebSocket for one tenant's word-mcp-live proxy."""
    await _serve_tunnel_ws(ws, tenant_id, _tunnel_word_sockets, _pending_word_reqs, "word")


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
# /ppt/mcp/{tenant_id} and /word/mcp/{tenant_id}  — Office MCP HTTP proxies
# ---------------------------------------------------------------------------

async def _office_proxy(
    tenant_id: str, local_path: str, request: Request,
    sockets: dict[str, WebSocket], pending: dict, label: str,
) -> Response:
    """Forward an HTTP request through an Office (ppt/word) tunnel socket."""
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
        sockets=sockets,
        pending=pending,
        label=label,
    )


@router.get("/ppt/mcp/{tenant_id}")
@router.post("/ppt/mcp/{tenant_id}")
@router.options("/ppt/mcp/{tenant_id}")
async def ppt_mcp_proxy(tenant_id: str, request: Request) -> Response:
    """Proxy requests to the tenant's powerpoint-mcp over the ppt tunnel."""
    prefix = f"/ppt/mcp/{tenant_id}"
    local_path = request.url.path[len(prefix):] or "/"
    return await _office_proxy(tenant_id, local_path, request,
                               _tunnel_ppt_sockets, _pending_ppt_reqs, "ppt")


@router.get("/ppt/mcp/{tenant_id}/{rest:path}")
@router.post("/ppt/mcp/{tenant_id}/{rest:path}")
@router.options("/ppt/mcp/{tenant_id}/{rest:path}")
async def ppt_mcp_proxy_subpath(tenant_id: str, rest: str, request: Request) -> Response:
    """Same as ppt_mcp_proxy but for sub-paths."""
    local_path = f"/{rest}" if rest else "/"
    return await _office_proxy(tenant_id, local_path, request,
                               _tunnel_ppt_sockets, _pending_ppt_reqs, "ppt")


@router.get("/word/mcp/{tenant_id}")
@router.post("/word/mcp/{tenant_id}")
@router.options("/word/mcp/{tenant_id}")
async def word_mcp_proxy(tenant_id: str, request: Request) -> Response:
    """Proxy requests to the tenant's word-mcp-live over the word tunnel."""
    prefix = f"/word/mcp/{tenant_id}"
    local_path = request.url.path[len(prefix):] or "/"
    return await _office_proxy(tenant_id, local_path, request,
                               _tunnel_word_sockets, _pending_word_reqs, "word")


@router.get("/word/mcp/{tenant_id}/{rest:path}")
@router.post("/word/mcp/{tenant_id}/{rest:path}")
@router.options("/word/mcp/{tenant_id}/{rest:path}")
async def word_mcp_proxy_subpath(tenant_id: str, rest: str, request: Request) -> Response:
    """Same as word_mcp_proxy but for sub-paths."""
    local_path = f"/{rest}" if rest else "/"
    return await _office_proxy(tenant_id, local_path, request,
                               _tunnel_word_sockets, _pending_word_reqs, "word")


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
        "ppt_active": tenant_id in _tunnel_ppt_sockets,
        "word_active": tenant_id in _tunnel_word_sockets,
    }


# ---------------------------------------------------------------------------
# Tunnel plugin registry — per-tenant config (dashboard Settings → Tunnel Plugins)
# ---------------------------------------------------------------------------

def _parse_plugins_json(raw: Any) -> Any:
    """Parse a stored tunnel_plugins JSON string into Python, tolerating junk."""
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001 — malformed config → treat as defaults
            return None
    return raw


@router.get("/tunnel/plugins")
async def get_tunnel_plugins(request: Request) -> Response:
    """Return the current tenant's resolved tunnel plugins + raw override config.

    The dashboard's Settings → Tunnel Plugins section renders ``plugins`` (the
    three slots with overrides applied) and round-trips ``config`` (the raw
    per-tenant overrides). Live tunnel sockets feed the per-slot status dots.
    """
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        return _json_response({
            "plugins": resolve_plugins(None), "config": {},
            "active": {"fs": False, "code": False, "extract": False},
        })
    parsed = _parse_plugins_json(tenant.get("tunnel_plugins"))
    tid = tenant.get("id")
    return _json_response({
        "plugins": resolve_plugins(parsed),
        "config": parsed if isinstance(parsed, (dict, list)) else {},
        "active": {
            "fs": tid in _tunnel_sockets,
            "code": tid in _tunnel_code_sockets,
            "extract": tid in _tunnel_extract_sockets,
            "ppt": tid in _tunnel_ppt_sockets,
            "word": tid in _tunnel_word_sockets,
        },
    })


@router.put("/tunnel/plugins")
async def put_tunnel_plugins(request: Request) -> Response:
    """Persist the tenant's tunnel plugin overrides (Settings → Tunnel Plugins).

    Accepts ``{"config": <overrides>}`` or a bare overrides dict/list. The config
    is normalized before storage; an empty result clears overrides (NULL → the
    built-in defaults). Takes effect the next time `meridian --tunnel` (re)starts.
    """
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        return _json_response({"error": "authentication required"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _json_response({"error": "invalid JSON body"}, status_code=400)
    raw = body.get("config") if isinstance(body, dict) and "config" in body else body
    normalized = normalize_plugins_config(raw)
    stored = json.dumps(normalized) if normalized else None
    # tenants lives in the control-plane DB (app.state.db), not the tenant's
    # data-plane DB — mirror the WS handler's update_tenant(tunnel_active=...).
    await db_module.update_tenant(request.app.state.db, tenant["id"], tunnel_plugins=stored)
    return _json_response({
        "ok": True,
        "plugins": resolve_plugins(normalized),
        "config": normalized,
    })


def _json_response(payload: dict, status_code: int = 200) -> Response:
    """Small JSON Response helper (keeps these handlers free of FastAPI magic)."""
    return Response(
        content=json.dumps(payload),
        status_code=status_code,
        media_type="application/json",
    )


def _union_filesystem_roots(projects: list[dict]) -> list[str]:
    """Collect a deduped, order-preserved list of executor_config.filesystem_roots
    across the given project rows. executor_config may be a JSON string or dict."""
    roots: list[str] = []
    seen: set[str] = set()
    for p in projects:
        raw = p.get("executor_config")
        if isinstance(raw, str) and raw.strip():
            try:
                raw = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
        if not isinstance(raw, dict):
            continue
        for r in (raw.get("filesystem_roots") or []):
            if isinstance(r, str) and r.strip() and r.strip() not in seen:
                seen.add(r.strip())
                roots.append(r.strip())
    return roots


@router.get("/tunnel/filesystem-roots")
async def get_tunnel_filesystem_roots(request: Request) -> Response:
    """Return the directories the tunnel's filesystem connector may read.

    The dashboard stores these per-project under ``executor_config.filesystem_roots``
    (Settings → Executor Config). The tunnel is tenant-scoped, so this unions the
    roots across all of the tenant's projects. An empty list means the client
    falls back to the user's home directory (current default behaviour). Best-effort:
    any DB error yields an empty list rather than failing the tunnel.
    """
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        return _json_response({"filesystem_roots": []})
    try:
        db = await _db(request)
        projects = await db_module.list_projects(db)
    except Exception:  # noqa: BLE001 — unprovisioned/unreachable DB → defaults
        projects = []
    return _json_response({"filesystem_roots": _union_filesystem_roots(projects)})


# ---------------------------------------------------------------------------
# Single-connector bridge — surface fs/code/extractor tools through /mcp
# ---------------------------------------------------------------------------
#
# When a tenant has a live tunnel, the Meridian remote-MCP endpoint aggregates
# the tunneled servers' tools into its own ``tools/list`` and routes matching
# ``tools/call`` requests back over the tunnel. The user's existing single
# Meridian connector therefore gains filesystem / code-intel / extractor tools
# automatically — no extra connectors to add.
#
# Routing is keyed by tool name. The cache below maps tenant_id → {tool: label}
# so a stateless ``tools/call`` can find the owning tunnel without re-listing.
# Cold/missing entries trigger a one-shot re-discovery via ``list_tunnel_tools``.

_TUNNEL_LABELS = ("fs", "code", "extract", "ppt", "word")

# Per-process routing cache: tenant_id → {tool_name: tunnel_label}
_tunnel_tool_routes: dict[str, dict[str, str]] = {}

# Phase 3 — server-side tool-description rewriting. When the bridge aggregates a
# tenant's tunneled tools, the raw filesystem read tools (read_file /
# read_multiple_files) get a code-intel-first directive prepended to their
# description so every client is steered toward graph queries for source code
# without any per-project agent_instructions change. Self-describing and
# conditional ("if those tools are available"), so it is safe to apply always.
_CODE_INTEL_FIRST_GUIDANCE = (
    "IMPORTANT: For source code files (.py .js .ts .go .rs .java .cpp .c .rb etc), "
    "you MUST call search_graph or get_function_tool FIRST if those tools are "
    "available. Use read_file only for non-code files (documents, spreadsheets, "
    "config files, data) or as a last resort when code intel tools are absent."
)

# Tools whose descriptions get the code-intel-first prefix at the bridge.
_READ_TOOLS_TO_REWRITE = frozenset({"read_file", "read_multiple_files"})


def _rewrite_tool_description(tool: dict) -> dict:
    """Prepend the code-intel-first directive to raw file-read tool descriptions.

    Returns a shallow copy with the rewritten ``description`` for the targeted
    tools; all other tools are returned unchanged. Idempotent — the guidance is
    not added twice if it is already present.
    """
    name = tool.get("name")
    if name not in _READ_TOOLS_TO_REWRITE:
        return tool
    desc = tool.get("description") or ""
    if _CODE_INTEL_FIRST_GUIDANCE in desc:
        return tool
    rewritten = dict(tool)
    rewritten["description"] = (
        f"{_CODE_INTEL_FIRST_GUIDANCE}\n\n{desc}".rstrip()
        if desc else _CODE_INTEL_FIRST_GUIDANCE
    )
    return rewritten


def _label_maps(label: str) -> "tuple[dict[str, WebSocket], dict[str, asyncio.Future[dict]]]":
    """Return the (sockets, pending) registries for a tunnel label."""
    if label == "fs":
        return _tunnel_sockets, _pending_reqs
    if label == "code":
        return _tunnel_code_sockets, _pending_code_reqs
    if label == "ppt":
        return _tunnel_ppt_sockets, _pending_ppt_reqs
    if label == "word":
        return _tunnel_word_sockets, _pending_word_reqs
    return _tunnel_extract_sockets, _pending_extract_reqs


def has_active_tunnel(tenant_id: str) -> bool:
    """True if the tenant has at least one live tunnel socket (any kind)."""
    return (
        tenant_id in _tunnel_sockets
        or tenant_id in _tunnel_code_sockets
        or tenant_id in _tunnel_extract_sockets
        or tenant_id in _tunnel_ppt_sockets
        or tenant_id in _tunnel_word_sockets
    )


def _parse_mcp_payload(raw: bytes | None) -> dict | None:
    """Parse an MCP JSON-RPC response body that may be plain JSON or SSE-framed.

    mcp-proxy's Streamable HTTP transport returns either ``application/json``
    or an SSE stream (``data: {...}`` lines). Return the last JSON-RPC object
    that parses, or None.
    """
    if not raw:
        return None
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return None
    if not text.startswith("{") and "data:" in text:
        result: dict | None = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    parsed = json.loads(payload)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(parsed, dict):
                    result = parsed
        return result
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001
        return None
    return parsed if isinstance(parsed, dict) else None


async def _tunnel_jsonrpc(
    tenant_id: str, label: str, method: str, params: dict | None,
) -> dict | None:
    """Send one JSON-RPC request to a tunneled MCP server and parse the reply."""
    sockets, pending = _label_maps(label)
    if tenant_id not in sockets:
        return None
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": "meridian-bridge",
        "method": method,
        "params": params or {},
    }).encode()
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    resp = await _do_proxy(
        tenant_id, "POST", "/mcp", "", headers, body, sockets, pending, label,
    )
    if resp.status_code >= 400:
        return None
    return _parse_mcp_payload(resp.body)


async def list_tunnel_tools(
    tenant_id: str, reserved_names: "frozenset[str] | set[str]" = frozenset(),
) -> list[dict]:
    """Aggregate tools from every active tunnel and refresh the routing cache.

    Tools whose name is already taken by a native or GitHub tool (``reserved_names``)
    are skipped so the merged ``tools/list`` has no duplicates and native tools
    always win at call time.
    """
    aggregated: list[dict] = []
    routes: dict[str, str] = {}
    for label in _TUNNEL_LABELS:
        sockets, _ = _label_maps(label)
        if tenant_id not in sockets:
            continue
        try:
            resp = await _tunnel_jsonrpc(tenant_id, label, "tools/list", {})
        except Exception as exc:  # noqa: BLE001
            _log.debug("tunnel %s tools/list failed for %s: %s", label, tenant_id[:8], exc)
            resp = None
        if not resp:
            continue
        tools = ((resp.get("result") or {}).get("tools")) or []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name")
            if not name or name in reserved_names or name in routes:
                continue
            routes[name] = label
            aggregated.append(_rewrite_tool_description(tool))
    if routes:
        _tunnel_tool_routes[tenant_id] = routes
    elif not has_active_tunnel(tenant_id):
        _tunnel_tool_routes.pop(tenant_id, None)
    return aggregated


async def call_tunnel_tool(
    tenant_id: str, name: str, arguments: dict | None,
) -> dict | None:
    """Route a ``tools/call`` to the tunnel that owns ``name``.

    Returns the MCP ``result`` object (with ``content``) on success, or None if
    no active tunnel exposes a tool by that name. Raises on a tunnel-reported
    JSON-RPC error so the caller can surface it as a normal MCP error.
    """
    label = (_tunnel_tool_routes.get(tenant_id) or {}).get(name)
    if label is None:
        # Cold cache (e.g. client skipped tools/list, or a different worker) —
        # discover once, then retry the lookup.
        await list_tunnel_tools(tenant_id)
        label = (_tunnel_tool_routes.get(tenant_id) or {}).get(name)
    if label is None:
        return None
    sockets, _ = _label_maps(label)
    if tenant_id not in sockets:
        return None
    resp = await _tunnel_jsonrpc(
        tenant_id, label, "tools/call",
        {"name": name, "arguments": arguments or {}},
    )
    if not resp:
        return None
    err = resp.get("error")
    if err:
        raise RuntimeError(str(err.get("message") if isinstance(err, dict) else err))
    return resp.get("result")
