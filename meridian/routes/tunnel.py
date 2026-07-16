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
import os
import time
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from .. import db as db_module
from .._deps import _hosted_mode, _get_tenant_from_request, _db
from ..tunnel_plugins import (
    normalize_plugins_config, resolve_plugins, resolve_custom_plugins, builtin_names,
)

router = APIRouter()
_log = logging.getLogger(__name__)

_PROXY_TIMEOUT = 30.0

# 1d021501 — per-slot in-flight budget (bulkhead). Each tunnel slot already has
# its own socket + pending-futures registry + a 30s per-request deadline
# (_PROXY_TIMEOUT), so a hung call on one slot cannot block an independent slot.
# What was missing was a CAP on how many requests may be in flight against a
# single (tenant, slot) at once: a slow/wedged backend could otherwise let its
# pending-futures dict grow without bound. This semaphore bounds the blast radius
# — once a slot has _MAX_SLOT_INFLIGHT requests waiting, further calls to THAT
# slot fail fast (503) instead of piling on, while every other slot is untouched.
def _max_slot_inflight() -> int:
    raw = os.environ.get("MERIDIAN_MAX_SLOT_INFLIGHT", "").strip()
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    return 8


# Time a saturated-slot caller waits for a free in-flight slot before giving up
# fast. Short on purpose: the point is to fail fast, not to queue.
_SLOT_ACQUIRE_TIMEOUT = 1.0

_slot_inflight: dict[str, asyncio.Semaphore] = {}


def _slot_semaphore(label: str, tenant_id: str) -> asyncio.Semaphore:
    """The per-(slot,tenant) in-flight semaphore, created on first use.

    Keyed by slot label + tenant so one tenant saturating a slot can't starve
    another tenant's same-named slot, and one slot's saturation never touches a
    different slot."""
    key = f"{label}:{tenant_id}"
    sem = _slot_inflight.get(key)
    if sem is None:
        sem = asyncio.Semaphore(_max_slot_inflight())
        _slot_inflight[key] = sem
    return sem

# Per-process in-memory registry: tenant_id → active WebSocket
_tunnel_sockets: dict[str, WebSocket] = {}
_tunnel_code_sockets: dict[str, WebSocket] = {}
_tunnel_extract_sockets: dict[str, WebSocket] = {}
_tunnel_ppt_sockets: dict[str, WebSocket] = {}
_tunnel_word_sockets: dict[str, WebSocket] = {}
_tunnel_dc_sockets: dict[str, WebSocket] = {}
# 9665538a — meridian-docs slot (DOCX intelligence via `uvx meridian-docs`).
_tunnel_docs_sockets: dict[str, WebSocket] = {}

# 39c117b1 — zotero-mcp slot (citation resolution via `uvx zotero-mcp`).
_tunnel_zotero_sockets: dict[str, WebSocket] = {}

# 469d89b4 — meridian-outputs slot (local BM25 outputs index via meridian-outputs-mcp).
_tunnel_outputs_sockets: dict[str, WebSocket] = {}

# 4d9ad87b — active repo per tenant, updated whenever set_active_repo is called.
# Enables call_tunnel_tool to inject X-Meridian-Repo-Path so the SerenaDaemonPool
# routes each tools/call to the correct per-repo daemon without a set_active_repo
# prelude on every request.
_tenant_active_repo: dict[str, str] = {}

# Correlation maps: request_id → Future that resolves when client responds
_pending_reqs: dict[str, asyncio.Future[dict]] = {}
_pending_code_reqs: dict[str, asyncio.Future[dict]] = {}
_pending_extract_reqs: dict[str, asyncio.Future[dict]] = {}
_pending_ppt_reqs: dict[str, asyncio.Future[dict]] = {}
_pending_word_reqs: dict[str, asyncio.Future[dict]] = {}
_pending_dc_reqs: dict[str, asyncio.Future[dict]] = {}
_pending_docs_reqs: dict[str, asyncio.Future[dict]] = {}
_pending_zotero_reqs: dict[str, asyncio.Future[dict]] = {}
_pending_outputs_reqs: dict[str, asyncio.Future[dict]] = {}

# 0e973e52 — run_verification: per-request futures for run_cmd control messages
# sent over the FS WebSocket. The FS receive loop resolves these when the client
# sends back a {"type": "run_cmd_result", "id": "...", ...} message. Keyed by
# correlation id (uuid4), independent of _pending_reqs (HTTP proxy futures).
_pending_run_cmd_reqs: dict[str, "asyncio.Future[dict]"] = {}

# 8fb69d54 — 4 pre-allocated custom tunnel slots (p0-p3, ports 8814-8817) so
# custom plugins get real server routes and appear in the claude.ai connector
# (closes ecf5b8c6). Registries are keyed by slot label, mirroring word/dc.
_CUSTOM_SLOTS = ("p0", "p1", "p2", "p3")
_tunnel_custom_sockets: dict[str, dict[str, WebSocket]] = {s: {} for s in _CUSTOM_SLOTS}
_pending_custom_reqs: dict[str, dict[str, "asyncio.Future[dict]"]] = {s: {} for s in _CUSTOM_SLOTS}

# d71ba2e7 — per-tenant core-slot health: tenant_id → {slot_label: healthy}.
# Absent ⇒ assumed healthy. The client sends a
# ``{"type":"plugin_status","slot":...,"healthy":false}`` message over a slot's
# WebSocket when its proxy spawns but doesn't actually serve (pre-flight
# tools/list fails, d71ba2e7) or when the watchdog exhausts retries (a3410a9c).
# ``list_tunnel_tools`` then suppresses that slot's tools instead of advertising
# tools that 503 on first call.
_slot_health: dict[str, dict[str, bool]] = {}

# 16e02240 — monotonic timestamp (time.monotonic()) at which each slot was last
# marked UNHEALTHY: tenant_id → {slot: ts}. A slot suppressed by
# ``_record_slot_health`` otherwise stays dark until the CLIENT sends a fresh
# healthy plugin_status — a transient hiccup with no follow-up recovery report
# leaves the slot excluded from every tools/list forever. This map lets
# ``_slot_is_healthy`` do an OPTIMISTIC re-probe: once the suppression is older
# than ``_slot_unhealthy_ttl()`` seconds, stop suppressing so the next tools/list
# re-advertises the slot and a real tools/call re-tests it (a still-broken slot
# will simply report unhealthy again, re-arming the timer).
_slot_unhealthy_since: dict[str, dict[str, float]] = {}


def _slot_unhealthy_ttl() -> float:
    """Seconds a slot stays suppressed before an optimistic re-probe (16e02240).

    Configurable via ``MERIDIAN_SLOT_UNHEALTHY_TTL`` (default 120s). A value <= 0
    disables the timeout (a slot stays suppressed until an explicit healthy report,
    the pre-16e02240 behaviour)."""
    raw = os.environ.get("MERIDIAN_SLOT_UNHEALTHY_TTL", "").strip()
    if raw:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return 120.0


# 9a8645c1 — optional per-slot diagnostic for an unhealthy slot:
# tenant_id → {slot: {"reason": str, "detail": str}}. Lets the dashboard show an
# actionable warning (e.g. Serena access-denied + fix hint) instead of a silent
# dead dot. Cleared when the slot recovers or the WS disconnects.
_slot_status_detail: dict[str, dict[str, dict[str, str]]] = {}

# 54ddd609 — tenants whose tunnel tool set changed (a slot recovered
# unhealthy->healthy) since the last time a `tools/list` was served. The MCP
# `/mcp` endpoint is a STATELESS HTTP request/response transport (see
# meridian/mcp/handler.py) — the server holds NO persistent push channel to a
# connected claude.ai / Claude Desktop MCP session, so it cannot itself emit an
# unsolicited `notifications/tools/list_changed` frame to that session. The
# smallest correct thing is therefore two-fold:
#   1. Invalidate the per-tenant routing cache so the NEXT tools/list (or a cold
#      tools/call) re-discovers the recovered slot's tools instead of serving a
#      stale set that omits them (list_tunnel_tools skips unhealthy slots).
#   2. Record a pending "list_changed" marker per tenant. Any surface that DOES
#      hold a live channel to the session (the tunnel WS relay, the dashboard's
#      status poll) can drain this to emit / trigger a re-query. `list_tunnel_tools`
#      itself clears the marker once it re-aggregates, so a client that re-lists
#      after the marker was set observes the recovered tools.
# This is the standard MCP mechanism for "the tool set changed" reduced to what a
# stateless transport can actually guarantee: the recovered tools become visible
# on the very next tools/list rather than staying invisible until a full reconnect.
_tools_list_changed_pending: set[str] = set()


def notify_tools_list_changed(tenant_id: str) -> None:
    """Signal that a tenant's aggregated tunnel tool set changed (54ddd609).

    Called on a slot RECOVERY (unhealthy->healthy) so the recovered slot's tools
    stop being invisible to an already-connected MCP session that cached the old
    (failed / empty) tools/list. Because the `/mcp` transport is stateless (no
    server->session push channel), this does the smallest correct thing:

      * drops the cached ``_tunnel_tool_routes`` for the tenant, forcing the next
        ``tools/list`` / cold ``tools/call`` to re-discover the recovered slot; and
      * marks the tenant pending in ``_tools_list_changed_pending`` — the MCP
        ``notifications/tools/list_changed`` signal, held until a surface that can
        reach the session drains it (or the next ``tools/list`` re-aggregates).

    Idempotent and cheap; safe to call on every healthy report (a no-op transition
    that wasn't actually a recovery simply re-marks a tenant that will re-list to
    the same tool set).
    """
    _tunnel_tool_routes.pop(tenant_id, None)
    _tools_list_changed_pending.add(tenant_id)


def consume_tools_list_changed(tenant_id: str) -> bool:
    """Return + clear the pending tools/list_changed marker for a tenant (54ddd609).

    True means a slot recovered since this was last consumed, so the caller should
    (re)advertise / re-query tools. Draining here is what lets a re-list observe the
    recovery exactly once."""
    if tenant_id in _tools_list_changed_pending:
        _tools_list_changed_pending.discard(tenant_id)
        return True
    return False


def _record_slot_health(
    tenant_id: str, slot: str, healthy: bool,
    reason: "str | None" = None, detail: "str | None" = None,
) -> None:
    """Record a core slot's health for a tenant (from a plugin_status message).
    9a8645c1 — when unhealthy, also stash an optional reason/detail; healthy
    clears any prior diagnostic.

    54ddd609 — detect a RECOVERY (unhealthy->healthy transition) and fire
    :func:`notify_tools_list_changed` so the recovered slot's tools become visible
    to an already-connected MCP session (which cached the old tools/list) on its
    next tools/list, instead of staying hidden until a full tunnel reconnect. The
    prior state MUST be read before we overwrite it below."""
    if not slot:
        return
    was_unhealthy = not _slot_is_healthy(tenant_id, slot)
    _slot_health.setdefault(tenant_id, {})[slot] = bool(healthy)
    if healthy:
        _slot_status_detail.get(tenant_id, {}).pop(slot, None)
        # 16e02240 — clear the suppression timestamp so a future unhealthy report
        # starts a fresh TTL window rather than inheriting a stale one.
        _clear_slot_unhealthy_since(tenant_id, slot)
        # RECOVERY: this slot was suppressed and is now serving again. Its tools
        # were dropped from the last aggregation (list_tunnel_tools skips unhealthy
        # slots), so trigger the tools/list_changed path to un-hide them.
        if was_unhealthy:
            notify_tools_list_changed(tenant_id)
    else:
        # 16e02240 — (re)arm the optimistic re-probe timer. Only stamp on the
        # healthy->unhealthy transition so a repeated unhealthy report doesn't keep
        # pushing the re-probe window out indefinitely (the slot would then stay
        # dark despite the TTL). ``was_unhealthy`` is the prior state read above.
        if not was_unhealthy:
            _slot_unhealthy_since.setdefault(tenant_id, {})[slot] = time.monotonic()
        if reason or detail:
            _slot_status_detail.setdefault(tenant_id, {})[slot] = {
                "reason": reason or "unhealthy",
                "detail": detail or "",
            }


def _slot_is_healthy(tenant_id: str, slot: str) -> bool:
    """True unless a plugin_status message marked this slot unhealthy.

    16e02240 — a slot marked unhealthy is only suppressed for up to
    ``_slot_unhealthy_ttl()`` seconds. Past that window we OPTIMISTICALLY treat it
    as healthy again so the next tools/list re-advertises it and a real call
    re-tests it — otherwise a transient hiccup with no follow-up healthy report
    would keep the slot dark forever. A still-broken slot re-reports unhealthy on
    its next failed call, re-arming the timer."""
    if _slot_health.get(tenant_id, {}).get(slot, True):
        return True
    # Recorded unhealthy — has the suppression aged past the re-probe TTL?
    ttl = _slot_unhealthy_ttl()
    if ttl <= 0:
        return False  # TTL disabled: stay suppressed until an explicit recovery.
    since = _slot_unhealthy_since.get(tenant_id, {}).get(slot)
    if since is None:
        # Marked unhealthy but no timestamp (defensive) — don't pin it dark forever.
        return True
    return (time.monotonic() - since) >= ttl


def _clear_slot_unhealthy_since(tenant_id: str, slot: "str | None" = None) -> None:
    """Drop a slot's unhealthy-since timestamp (or all of a tenant's) — 16e02240."""
    if slot is None:
        _slot_unhealthy_since.pop(tenant_id, None)
        return
    stamps = _slot_unhealthy_since.get(tenant_id)
    if stamps is not None:
        stamps.pop(slot, None)
        if not stamps:
            _slot_unhealthy_since.pop(tenant_id, None)


def _clear_slot_health(tenant_id: str, slot: "str | None" = None) -> None:
    """Drop a slot's health (or all of a tenant's) — called on WS disconnect so a
    fresh reconnect starts from the assumed-healthy default."""
    if slot is None:
        _slot_health.pop(tenant_id, None)
        _slot_status_detail.pop(tenant_id, None)
        _clear_slot_unhealthy_since(tenant_id)  # 16e02240
        _tools_list_changed_pending.discard(tenant_id)  # 54ddd609
        return
    slots = _slot_health.get(tenant_id)
    if slots is not None:
        slots.pop(slot, None)
        if not slots:
            _slot_health.pop(tenant_id, None)
    _clear_slot_unhealthy_since(tenant_id, slot)  # 16e02240
    _det = _slot_status_detail.get(tenant_id)
    if _det is not None:
        _det.pop(slot, None)
        if not _det:
            _slot_status_detail.pop(tenant_id, None)


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
    _tunnel_tool_routes.pop(tenant_id, None)  # 4331f9cd — reconnect: rebuild tool routes
    # af5b5739 — record THIS Fly instance as the socket owner so a request that
    # lands on a sibling instance can Fly-replay to us (no-op off Fly).
    owner_instance = record_tenant_owner_instance(tenant_id)
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

            if msg_type == "plugin_status":
                # d71ba2e7 — client reports this slot's live health (+ 9a8645c1 reason).
                _record_slot_health(
                    tenant_id, msg.get("slot") or "fs", msg.get("healthy", True),
                    reason=msg.get("reason"), detail=msg.get("detail"),
                )
                continue

            if msg_type == "response":
                req_id = msg.get("id")
                fut = _pending_reqs.get(req_id)
                if fut is not None and not fut.done():
                    fut.set_result(msg)

            # 0e973e52 — run_verification: client sends back run_cmd results
            # over the same FS socket using a separate message type so these
            # never collide with HTTP-proxy "response" futures.
            if msg_type == "run_cmd_result":
                req_id = msg.get("id")
                fut_rc = _pending_run_cmd_reqs.get(req_id)
                if fut_rc is not None and not fut_rc.done():
                    fut_rc.set_result(msg)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        _log.debug("tunnel: tenant %s disconnected: %s", tenant_id[:8], exc)
    finally:
        _tunnel_sockets.pop(tenant_id, None)
        _clear_slot_health(tenant_id, "fs")
        # af5b5739 — forget our ownership claim only if it's still ours (a newer
        # connection on another instance may already have re-claimed the tenant).
        clear_tenant_owner_instance(tenant_id, owner_instance)
        if not has_active_tunnel(tenant_id):
            _tunnel_tool_routes.pop(tenant_id, None)  # 4331f9cd
        # Cancel any in-flight proxy requests for this tenant
        for fut in list(_pending_reqs.values()):
            if not fut.done():
                fut.cancel()
        # 0e973e52 — cancel any in-flight run_cmd requests when FS socket drops
        for fut_rc in list(_pending_run_cmd_reqs.values()):
            if not fut_rc.done():
                fut_rc.cancel()
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
    # af5b5739 / 5f02a21c — record THIS Fly instance as the owner so a sibling
    # instance that misses can Fly-replay to us (no-op off Fly). af5b5739 wired
    # this only for the FS slot (tunnel_ws); this is the equivalent for code.
    owner_instance = record_tenant_owner_instance(tenant_id)
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
            if msg_type == "plugin_status":
                _record_slot_health(
                    tenant_id, msg.get("slot") or "code", msg.get("healthy", True),
                    reason=msg.get("reason"), detail=msg.get("detail"),
                )
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
        _clear_slot_health(tenant_id, "code")
        # af5b5739 / 5f02a21c — release ownership only if still ours.
        clear_tenant_owner_instance(tenant_id, owner_instance)
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
    # af5b5739 / 5f02a21c — record THIS Fly instance as the owner so a sibling
    # instance that misses an extract request can Fly-replay to us (no-op off
    # Fly). af5b5739 wired this only for the FS slot; this is the extract fix.
    owner_instance = record_tenant_owner_instance(tenant_id)
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
            if msg_type == "plugin_status":
                _record_slot_health(
                    tenant_id, msg.get("slot") or "extract", msg.get("healthy", True),
                    reason=msg.get("reason"), detail=msg.get("detail"),
                )
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
        _clear_slot_health(tenant_id, "extract")
        # af5b5739 / 5f02a21c — release ownership only if still ours.
        clear_tenant_owner_instance(tenant_id, owner_instance)
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
    # 4331f9cd — a (re)connect may change the slot's tool set; drop the cached
    # routes so the next tools/list rebuilds them for this tenant.
    _tunnel_tool_routes.pop(tenant_id, None)
    # af5b5739 / 5f02a21c — record THIS Fly instance as the owner so a sibling
    # instance that gets a request for this slot can Fly-replay to us. af5b5739
    # only wired this for the FS slot (tunnel_ws); _serve_tunnel_ws covers ppt,
    # word, dc, docs, zotero, and custom slots (p0-p3).
    owner_instance = record_tenant_owner_instance(tenant_id)
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
            if msg_type == "plugin_status":
                # a898710a — dc/ppt/word slots report health too; record it so a
                # failed pre-flight surfaces as "unhealthy" (not "inactive"), and
                # a later healthy report clears the diagnostic.
                _record_slot_health(
                    tenant_id, msg.get("slot") or label, msg.get("healthy", True),
                    reason=msg.get("reason"), detail=msg.get("detail"),
                )
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
        _clear_slot_health(tenant_id, label)
        # 4331f9cd — slot dropped; if no tunnel remains, drop cached routes so the
        # next tools/list rebuilds cleanly.
        if not has_active_tunnel(tenant_id):
            _tunnel_tool_routes.pop(tenant_id, None)
        # af5b5739 / 5f02a21c — release ownership only if still ours.
        clear_tenant_owner_instance(tenant_id, owner_instance)
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
    """Hold open a WebSocket for one tenant's docx-mcp proxy."""
    await _serve_tunnel_ws(ws, tenant_id, _tunnel_word_sockets, _pending_word_reqs, "word")


@router.websocket("/tunnel-dc/{tenant_id}")
async def tunnel_dc_ws(ws: WebSocket, tenant_id: str) -> None:
    """Hold open a WebSocket for one tenant's desktop-commander proxy."""
    await _serve_tunnel_ws(ws, tenant_id, _tunnel_dc_sockets, _pending_dc_reqs, "dc")


@router.websocket("/tunnel-docs/{tenant_id}")
async def tunnel_docs_ws(ws: WebSocket, tenant_id: str) -> None:
    """Hold open a WebSocket for one tenant's meridian-docs proxy."""
    await _serve_tunnel_ws(ws, tenant_id, _tunnel_docs_sockets, _pending_docs_reqs, "docs")


@router.websocket("/tunnel-zotero/{tenant_id}")
async def tunnel_zotero_ws(ws: WebSocket, tenant_id: str) -> None:
    """Hold open a WebSocket for one tenant's zotero-mcp proxy."""
    await _serve_tunnel_ws(ws, tenant_id, _tunnel_zotero_sockets, _pending_zotero_reqs, "zotero")


@router.websocket("/tunnel-outputs/{tenant_id}")
async def tunnel_outputs_ws(ws: WebSocket, tenant_id: str) -> None:
    """469d89b4 — Hold open a WebSocket for one tenant's meridian-outputs proxy."""
    await _serve_tunnel_ws(ws, tenant_id, _tunnel_outputs_sockets, _pending_outputs_reqs, "outputs")


# 8fb69d54 — register the 4 custom-slot WebSocket routes (/tunnel-p0 … /tunnel-p3)
# so a custom plugin bound to a slot gets a real server route.
def _make_custom_slot_ws(_slot: str):
    async def _ws(ws: WebSocket, tenant_id: str) -> None:
        await _serve_tunnel_ws(
            ws, tenant_id,
            _tunnel_custom_sockets[_slot], _pending_custom_reqs[_slot], _slot,
        )
    return _ws


for _cs in _CUSTOM_SLOTS:
    router.websocket(f"/tunnel-{_cs}/{{tenant_id}}")(_make_custom_slot_ws(_cs))


# ---------------------------------------------------------------------------
# /tunnel/active-repo  — control: update pool's default repo_path at runtime
# ---------------------------------------------------------------------------

class _ActiveRepoBody:
    """Thin data holder for POST /tunnel/active-repo (avoids Pydantic import cost)."""
    __slots__ = ("repo_path",)

    def __init__(self, repo_path: str) -> None:
        self.repo_path = repo_path


@router.post("/tunnel/active-repo")
async def set_tunnel_active_repo(request: Request) -> Response:
    """Send a set_active_repo control message to the tenant's extract WebSocket.

    The tunnel client's _run_extract_pool_connection handles this message and
    updates SerenaPool.default_repo_path at runtime — no tunnel restart needed.
    Returns 503 when no extract tunnel is connected, 404 when auth fails.
    """
    if not _hosted_mode():
        return Response(
            content='{"error":"tunnel requires hosted mode"}',
            status_code=503,
            media_type="application/json",
        )

    auth_header = request.headers.get("authorization", "")
    raw_token = auth_header[len("Bearer "):].strip() if auth_header.lower().startswith("bearer ") else auth_header
    if not raw_token:
        raw_token = request.query_params.get("token", "")
    auth_db = request.app.state.db
    tenant = await _resolve_tenant_from_token(auth_db, raw_token)
    if tenant is None:
        return Response(
            content='{"error":"invalid token"}',
            status_code=401,
            media_type="application/json",
        )
    if not _is_tunnel_allowed(tenant):
        return Response(
            content='{"error":"tunnel requires Pro plan"}',
            status_code=403,
            media_type="application/json",
        )

    try:
        body = await request.json()
        repo_path = str(body.get("repo_path") or "").strip()
    except Exception:
        return Response(
            content='{"error":"invalid JSON body — expected {repo_path: str}"}',
            status_code=400,
            media_type="application/json",
        )
    if not repo_path:
        return Response(
            content='{"error":"repo_path is required"}',
            status_code=400,
            media_type="application/json",
        )

    tenant_id = tenant["id"]
    ws = _tunnel_extract_sockets.get(tenant_id)
    if ws is None:
        return Response(
            content='{"status":"not_connected","message":"no active extract tunnel — start meridian --tunnel first"}',
            status_code=503,
            media_type="application/json",
        )

    try:
        await ws.send_json({"type": "set_active_repo", "repo_path": repo_path})
    except Exception as exc:
        _log.debug("set_active_repo: send error for %s: %s", tenant_id[:8], exc)
        return Response(
            content=json.dumps({"status": "error", "message": str(exc)}),
            status_code=502,
            media_type="application/json",
        )

    return Response(
        content=json.dumps({"status": "ok", "repo_path": repo_path}),
        status_code=200,
        media_type="application/json",
    )


async def send_active_repo_control(tenant_id: str, repo_path: str) -> dict[str, str]:
    """Send set_active_repo over the extract WebSocket for a tenant.

    Called by the MCP handler (no HTTP round-trip needed). Returns a status dict
    with ``status`` of ``"ok"``, ``"not_connected"``, or ``"error"``.

    4d9ad87b — always updates _tenant_active_repo so subsequent call_tunnel_tool
    calls can inject X-Meridian-Repo-Path without a separate set_active_repo.
    """
    _tenant_active_repo[tenant_id] = repo_path
    ws = _tunnel_extract_sockets.get(tenant_id)
    if ws is None:
        return {"status": "not_connected", "message": "no active extract tunnel — start meridian --tunnel first"}
    try:
        await ws.send_json({"type": "set_active_repo", "repo_path": repo_path})
        return {"status": "ok", "repo_path": repo_path}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": str(exc)}


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
        # af5b5739 — in-memory MISS on this instance. If a DIFFERENT Fly instance is
        # known to own this tenant's socket, replay the request there so Fly routes
        # it to the machine that can actually serve it. No-op off Fly / unknown
        # owner (falls through to the legible 503 below).
        replay = fly_replay_target_for_id(tenant_id)
        if replay is not None:
            return fly_replay_response(replay)
        return Response(
            content=f'{{"error":"{label} tunnel not connected"}}',
            status_code=503,
            media_type="application/json",
        )

    # 1d021501 — per-slot in-flight budget: bound how many requests may be waiting
    # on THIS (slot, tenant) at once. If the slot is already saturated (its backend
    # is slow/wedged), fail fast instead of growing its pending-futures dict — the
    # blast radius stays inside this one slot; every other slot is unaffected.
    sem = _slot_semaphore(label, tenant_id)
    try:
        await asyncio.wait_for(sem.acquire(), timeout=_SLOT_ACQUIRE_TIMEOUT)
    except asyncio.TimeoutError:
        return Response(
            content=f'{{"error":"{label} tunnel slot saturated — too many requests in flight"}}',
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
        sem.release()

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
    """Proxy requests to the tenant's docx-mcp server over the word tunnel."""
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


@router.get("/dc/mcp/{tenant_id}")
@router.post("/dc/mcp/{tenant_id}")
@router.options("/dc/mcp/{tenant_id}")
async def dc_mcp_proxy(tenant_id: str, request: Request) -> Response:
    """Proxy requests to the tenant's desktop-commander over the dc tunnel."""
    prefix = f"/dc/mcp/{tenant_id}"
    local_path = request.url.path[len(prefix):] or "/"
    return await _office_proxy(tenant_id, local_path, request,
                               _tunnel_dc_sockets, _pending_dc_reqs, "dc")


@router.get("/dc/mcp/{tenant_id}/{rest:path}")
@router.post("/dc/mcp/{tenant_id}/{rest:path}")
@router.options("/dc/mcp/{tenant_id}/{rest:path}")
async def dc_mcp_proxy_subpath(tenant_id: str, rest: str, request: Request) -> Response:
    """Same as dc_mcp_proxy but for sub-paths."""
    local_path = f"/{rest}" if rest else "/"
    return await _office_proxy(tenant_id, local_path, request,
                               _tunnel_dc_sockets, _pending_dc_reqs, "dc")


@router.get("/docs/mcp/{tenant_id}")
@router.post("/docs/mcp/{tenant_id}")
@router.options("/docs/mcp/{tenant_id}")
async def docs_mcp_proxy(tenant_id: str, request: Request) -> Response:
    """Proxy requests to the tenant's meridian-docs server over the docs tunnel."""
    prefix = f"/docs/mcp/{tenant_id}"
    local_path = request.url.path[len(prefix):] or "/"
    return await _office_proxy(tenant_id, local_path, request,
                               _tunnel_docs_sockets, _pending_docs_reqs, "docs")


@router.get("/docs/mcp/{tenant_id}/{rest:path}")
@router.post("/docs/mcp/{tenant_id}/{rest:path}")
@router.options("/docs/mcp/{tenant_id}/{rest:path}")
async def docs_mcp_proxy_subpath(tenant_id: str, rest: str, request: Request) -> Response:
    """Same as docs_mcp_proxy but for sub-paths."""
    local_path = f"/{rest}" if rest else "/"
    return await _office_proxy(tenant_id, local_path, request,
                               _tunnel_docs_sockets, _pending_docs_reqs, "docs")


@router.get("/zotero/mcp/{tenant_id}")
@router.post("/zotero/mcp/{tenant_id}")
@router.options("/zotero/mcp/{tenant_id}")
async def zotero_mcp_proxy(tenant_id: str, request: Request) -> Response:
    """Proxy requests to the tenant's zotero-mcp server over the zotero tunnel."""
    prefix = f"/zotero/mcp/{tenant_id}"
    local_path = request.url.path[len(prefix):] or "/"
    return await _office_proxy(tenant_id, local_path, request,
                               _tunnel_zotero_sockets, _pending_zotero_reqs, "zotero")


@router.get("/zotero/mcp/{tenant_id}/{rest:path}")
@router.post("/zotero/mcp/{tenant_id}/{rest:path}")
@router.options("/zotero/mcp/{tenant_id}/{rest:path}")
async def zotero_mcp_proxy_subpath(tenant_id: str, rest: str, request: Request) -> Response:
    """Same as zotero_mcp_proxy but for sub-paths."""
    local_path = f"/{rest}" if rest else "/"
    return await _office_proxy(tenant_id, local_path, request,
                               _tunnel_zotero_sockets, _pending_zotero_reqs, "zotero")


@router.get("/outputs/mcp/{tenant_id}")
@router.post("/outputs/mcp/{tenant_id}")
@router.options("/outputs/mcp/{tenant_id}")
async def outputs_mcp_proxy(tenant_id: str, request: Request) -> Response:
    """469d89b4 — Proxy requests to the tenant's meridian-outputs server over the outputs tunnel."""
    prefix = f"/outputs/mcp/{tenant_id}"
    local_path = request.url.path[len(prefix):] or "/"
    return await _office_proxy(tenant_id, local_path, request,
                               _tunnel_outputs_sockets, _pending_outputs_reqs, "outputs")


@router.get("/outputs/mcp/{tenant_id}/{rest:path}")
@router.post("/outputs/mcp/{tenant_id}/{rest:path}")
@router.options("/outputs/mcp/{tenant_id}/{rest:path}")
async def outputs_mcp_proxy_subpath(tenant_id: str, rest: str, request: Request) -> Response:
    """Same as outputs_mcp_proxy but for sub-paths."""
    local_path = f"/{rest}" if rest else "/"
    return await _office_proxy(tenant_id, local_path, request,
                               _tunnel_outputs_sockets, _pending_outputs_reqs, "outputs")


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
        "dc_active": tenant_id in _tunnel_dc_sockets,
        "docs_active": tenant_id in _tunnel_docs_sockets,
        "zotero_active": tenant_id in _tunnel_zotero_sockets,
        "outputs_active": tenant_id in _tunnel_outputs_sockets,
        # d71ba2e7 — slots the client reported unhealthy (pre-flight tools/list
        # failed / watchdog gave up). Absent slot ⇒ assumed healthy. Dashboard
        # renders these as a degraded status dot.
        "slot_health": dict(_slot_health.get(tenant_id, {})),
        # 9a8645c1 — per-slot diagnostic (reason + actionable detail) for an
        # unhealthy slot, so the dashboard shows a warning badge with a fix hint.
        "slot_status": {
            k: dict(v) for k, v in _slot_status_detail.get(tenant_id, {}).items()
        },
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
            "plugins": resolve_plugins(None), "custom": [], "config": {},
            "active": {"fs": False, "code": False, "extract": False, "ppt": False, "word": False, "dc": False, "docs": False, "zotero": False, "outputs": False, **{s: False for s in _CUSTOM_SLOTS}},
            "plan": "free",
        })
    # 8660d701 — per-machine config. ?hostname=X scopes to that machine's config
    # (from tunnel_plugins_by_host), falling back to the per-tenant default. No
    # hostname → the default config (back-compat with the single-machine dashboard).
    from ..tunnel_plugins import select_host_config, parse_plugins_by_host
    hostname = (request.query_params.get("hostname") or "").strip() or None
    by_host = parse_plugins_by_host(tenant.get("tunnel_plugins_by_host"))
    parsed = select_host_config(
        _parse_plugins_json(tenant.get("tunnel_plugins")),
        tenant.get("tunnel_plugins_by_host"),
        hostname,
    )
    tid = tenant.get("id")
    # Machines the dashboard can offer per-machine config for: those with a saved
    # per-host config + machines that registered hook tokens (registered_hostnames).
    _hosts = set(by_host.keys())
    try:
        for _m in await db_module.list_registered_hostnames(request.app.state.db, tid):
            if _m.get("hostname"):
                _hosts.add(str(_m["hostname"]))
    except Exception:  # noqa: BLE001 — host listing is best-effort
        pass
    return _json_response({
        "plugins": resolve_plugins(parsed),
        # User-defined (non-built-in) plugins, so the dashboard can render and
        # edit existing custom entries. LOCAL-ONLY — no server route involved.
        "custom": resolve_custom_plugins(parsed),
        "config": parsed if isinstance(parsed, (dict, list)) else {},
        # 8660d701 — per-machine config UI: the selected machine + the machines the
        # dashboard can pick from (configured + registered), and which already have
        # an explicit per-host config saved.
        "hostname": hostname,
        "hosts": sorted(_hosts),
        "configured_hosts": sorted(by_host.keys()),
        "active": {
            # On Fly.io multi-instance, the WebSocket may be held by a different
            # instance (in-memory miss). Fall back to tenant.tunnel_active (DB flag
            # set on connect, cleared on disconnect) so status stays correct.
            "fs": (tid in _tunnel_sockets) or bool(tenant.get("tunnel_active")),
            "code": tid in _tunnel_code_sockets,
            "extract": tid in _tunnel_extract_sockets,
            "ppt": tid in _tunnel_ppt_sockets,
            "word": tid in _tunnel_word_sockets,
            "dc": tid in _tunnel_dc_sockets,
            "docs": tid in _tunnel_docs_sockets,
            "zotero": tid in _tunnel_zotero_sockets,
            "outputs": tid in _tunnel_outputs_sockets,
            **{s: tid in _tunnel_custom_sockets[s] for s in _CUSTOM_SLOTS},
        },
        # The tunnel (and thus this section) is Pro/admin-only; the dashboard
        # uses this to gate the Tunnel Plugins card.
        "plan": tenant.get("plan") or "free",
        "is_admin": bool(tenant.get("is_internal")),
        # 9a8645c1 — per-slot health + diagnostic so the dashboard can render a
        # warning badge (e.g. Serena access-denied) on a degraded slot's row.
        "slot_health": dict(_slot_health.get(tid, {})),
        "slot_status": {
            k: dict(v) for k, v in _slot_status_detail.get(tid, {}).items()
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
    # 8660d701 — ?hostname=X persists this config for that machine only
    # (tunnel_plugins_by_host); without a hostname it writes the per-tenant default
    # (tunnel_plugins), preserving the legacy single-machine behaviour.
    hostname = (request.query_params.get("hostname") or "").strip() or None
    # tenants lives in the control-plane DB (app.state.db), not the tenant's
    # data-plane DB — mirror the WS handler's update_tenant(tunnel_active=...).
    if hostname:
        from ..tunnel_plugins import parse_plugins_by_host
        by_host = parse_plugins_by_host(tenant.get("tunnel_plugins_by_host"))
        if normalized:
            by_host[hostname] = normalized
        else:
            by_host.pop(hostname, None)  # empty → clear this machine's override
        stored_by_host = json.dumps(by_host) if by_host else None
        await db_module.update_tenant(
            request.app.state.db, tenant["id"], tunnel_plugins_by_host=stored_by_host)
    else:
        stored = json.dumps(normalized) if normalized else None
        await db_module.update_tenant(
            request.app.state.db, tenant["id"], tunnel_plugins=stored)
    return _json_response({
        "ok": True,
        "hostname": hostname,
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


# ---------------------------------------------------------------------------
# Custom-plugin add / remove — sprint item 9811d04c
# Persists a tenant's *chosen* custom plugins (the "install this" path the browse
# UI dead-ended on). Storage reuses the existing per-tenant ``tunnel_plugins``
# JSON blob (or ``tunnel_plugins_by_host`` when ?hostname=X) — a custom plugin is
# just an extra list entry whose name is not a built-in, so no new table/column
# and no migration. resolve_custom_plugins already reads these entries.
# ---------------------------------------------------------------------------

def _config_as_entry_list(parsed: Any) -> list[dict]:
    """Normalize a stored tunnel_plugins config into an ordered list of entries.

    The store round-trips as either the dict-keyed form or the list form; the
    add/remove handlers work on the list form (so we can append/filter and keep
    custom entries' ``port``/``command`` intact) before re-normalizing to persist.
    """
    from ..tunnel_plugins import _iter_plugin_items
    return _iter_plugin_items(parsed)


async def _store_tunnel_config(request: Request, tenant: dict, hostname: str | None,
                               entries: list[dict]) -> Any:
    """Persist a full tunnel-plugins config (list of entries) for a tenant/machine.

    Mirrors put_tunnel_plugins' storage: with ``hostname`` it writes that
    machine's slice of ``tunnel_plugins_by_host``; otherwise the per-tenant
    ``tunnel_plugins`` default. Returns the normalized config that was stored.
    """
    normalized = normalize_plugins_config(entries)
    if hostname:
        from ..tunnel_plugins import parse_plugins_by_host
        by_host = parse_plugins_by_host(tenant.get("tunnel_plugins_by_host"))
        if normalized:
            by_host[hostname] = normalized
        else:
            by_host.pop(hostname, None)
        stored_by_host = json.dumps(by_host) if by_host else None
        await db_module.update_tenant(
            request.app.state.db, tenant["id"], tunnel_plugins_by_host=stored_by_host)
    else:
        stored = json.dumps(normalized) if normalized else None
        await db_module.update_tenant(
            request.app.state.db, tenant["id"], tunnel_plugins=stored)
    return normalized


def _current_tunnel_config(tenant: dict, hostname: str | None) -> Any:
    """The effective stored config (parsed) for a tenant/machine — no defaults."""
    from ..tunnel_plugins import select_host_config
    return select_host_config(
        _parse_plugins_json(tenant.get("tunnel_plugins")),
        tenant.get("tunnel_plugins_by_host"),
        hostname,
    )


@router.post("/tunnel/plugins/custom")
async def add_custom_plugin(request: Request) -> Response:
    """9811d04c — persist a chosen custom plugin into the tenant's tunnel config.

    Body: ``{"name": str, "command": str|list, "port"?: int, "env"?: {..}}``.
    ``?hostname=X`` scopes it to that machine (else the per-tenant default). The
    name is validated (safe charset, no built-in slot/plugin collision) and the
    command must be non-empty; a missing/blank port is auto-assigned. Merges into
    the existing config (reusing the ``tunnel_plugins`` JSON blob — no new table)
    so ``resolve_custom_plugins`` picks it up on the next tunnel (re)start.
    Returns the updated ``custom`` list.
    """
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        return _json_response({"error": "authentication required"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _json_response({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return _json_response({"error": "expected a JSON object"}, status_code=400)

    hostname = (request.query_params.get("hostname") or "").strip() or None
    parsed = _current_tunnel_config(tenant, hostname)
    entries = _config_as_entry_list(parsed)

    from ..tunnel_plugins import validate_custom_plugin
    name = str(body.get("name") or "").strip()
    # Reject a duplicate custom name up front (case-insensitive) so add is
    # idempotent-safe and doesn't silently replace an existing entry.
    existing_custom = resolve_custom_plugins(parsed)
    if name and any(c["name"].lower() == name.lower() for c in existing_custom):
        return _json_response(
            {"error": f"a custom plugin named '{name}' already exists"}, status_code=409)
    existing_ports = [c["port"] for c in existing_custom]
    entry, err = validate_custom_plugin(
        name, body.get("command"), body.get("port"),
        existing_ports=existing_ports, env=body.get("env"),
    )
    if err is not None:
        return _json_response({"error": err}, status_code=400)

    entries.append(entry)
    await _store_tunnel_config(request, tenant, hostname, entries)
    # Re-read the stored config so the returned custom list reflects exactly what
    # persisted (and would resolve at tunnel spawn).
    stored = _current_tunnel_config(
        await _get_tenant_from_request(request) or tenant, hostname)
    return _json_response({
        "ok": True,
        "hostname": hostname,
        "added": entry,
        "custom": resolve_custom_plugins(stored),
    })


@router.delete("/tunnel/plugins/custom")
async def remove_custom_plugin(request: Request) -> Response:
    """9811d04c — remove a persisted custom plugin by name.

    Name comes from ``?name=`` or a ``{"name": ...}`` JSON body. ``?hostname=X``
    scopes to that machine. Built-in slot overrides are left untouched (only a
    non-built-in entry with a matching name is dropped). Returns the updated
    ``custom`` list. Removing a name that isn't a custom plugin is a no-op 404.
    """
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        return _json_response({"error": "authentication required"}, status_code=401)

    name = (request.query_params.get("name") or "").strip()
    if not name:
        try:
            body = await request.json()
            if isinstance(body, dict):
                name = str(body.get("name") or "").strip()
        except Exception:  # noqa: BLE001 — no/invalid body; name may still be in query
            pass
    if not name:
        return _json_response({"error": "name is required"}, status_code=400)

    from ..tunnel_plugins import is_reserved_custom_name
    if is_reserved_custom_name(name):
        return _json_response(
            {"error": "cannot remove a built-in slot via this route"}, status_code=400)

    hostname = (request.query_params.get("hostname") or "").strip() or None
    parsed = _current_tunnel_config(tenant, hostname)
    entries = _config_as_entry_list(parsed)

    builtin = set(builtin_names())
    kept: list[dict] = []
    removed = False
    for it in entries:
        it_name = str(it.get("name") or "").strip()
        # Only drop a *custom* (non-built-in) entry matching the name — a built-in
        # slot override that happens to be present is preserved.
        if it_name.lower() == name.lower() and it_name not in builtin:
            removed = True
            continue
        kept.append(it)
    if not removed:
        return _json_response(
            {"error": f"no custom plugin named '{name}'"}, status_code=404)

    await _store_tunnel_config(request, tenant, hostname, kept)
    stored = _current_tunnel_config(
        await _get_tenant_from_request(request) or tenant, hostname)
    return _json_response({
        "ok": True,
        "hostname": hostname,
        "removed": name,
        "custom": resolve_custom_plugins(stored),
    })


# ---------------------------------------------------------------------------
# Plugin install / uninstall — sprint item 56cb5d33
# Three-state lifecycle: not_installed / installed_inactive / active.
# POST /tunnel/plugins/install  — run uvx/npx install (self-hosted mode).
# POST /tunnel/plugins/uninstall — remove from config + kill process.
# GET  /tunnel/plugins/check    — detect if a command binary is available.
# ---------------------------------------------------------------------------

@router.get("/tunnel/plugins/check")
async def check_plugin_installed(request: Request) -> Response:
    """Check whether a plugin binary is available on the server's PATH.

    Query params:
      command — the install command string (e.g. "uvx mcp-server-fetch")

    Returns {"installed": bool, "command": str}. Self-hosted only — on hosted
    deployments the binary lives on the user's machine (via the tunnel client),
    so this endpoint reports the server-side availability.
    """
    import shutil
    command = request.query_params.get("command", "").strip()
    if not command:
        return _json_response({"error": "command required"}, status_code=400)
    # Extract the binary name (first word of command, stripping uvx/npx wrappers)
    parts = command.split()
    binary = parts[0] if parts else ""
    # For uvx/npx launchers, the real package is the second arg
    if binary in ("uvx", "npx") and len(parts) >= 2:
        # uvx mcp-server-fetch → check if uvx is available (uvx handles the rest)
        binary = parts[0]
    found = shutil.which(binary) is not None
    return _json_response({"installed": found, "command": command, "binary": binary})


_ALLOWED_LAUNCHERS = frozenset(["uvx", "npx"])

@router.post("/tunnel/plugins/install")
async def install_plugin(request: Request) -> Response:
    """Run a plugin install command on the server machine (self-hosted deployments).

    Body: {"command": "uvx mcp-server-fetch"}

    Validates that the command starts with uvx or npx to prevent arbitrary
    execution. Returns {"ok": bool, "output": str}.

    In hosted mode the server and user machine are different — this endpoint
    still runs but installs on the server (not the user's machine). The
    dashboard shows a copy-to-clipboard fallback for hosted users.
    """
    import asyncio
    import sys
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _json_response({"error": "invalid JSON body"}, status_code=400)
    command = (body.get("command") or "").strip()
    if not command:
        return _json_response({"error": "command required"}, status_code=400)
    parts = command.split()
    launcher = parts[0] if parts else ""
    if launcher not in _ALLOWED_LAUNCHERS:
        return _json_response(
            {"error": f"launcher '{launcher}' not allowed; must be uvx or npx"},
            status_code=400,
        )
    try:
        proc = await asyncio.create_subprocess_exec(
            *parts,
            "--help",  # dry-run: uvx/npx --help downloads the package without running the server
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        ok = proc.returncode == 0
        output = (_stdout + _stderr).decode(errors="replace")[:2000]
        return _json_response({"ok": ok, "returncode": proc.returncode, "output": output})
    except asyncio.TimeoutError:
        return _json_response({"ok": False, "output": "install timed out (60s)"})
    except Exception as exc:  # noqa: BLE001
        return _json_response({"ok": False, "output": str(exc)})


# ---------------------------------------------------------------------------
# MCP Registry proxy — sprint item 9b288b91
# Proxies GET registry.modelcontextprotocol.io/v0/servers to avoid CORS.
# Returns a normalised subset: id, name, description, install_command, homepage.
# Cursor-based pagination: pass ?cursor=<opaque> to fetch the next page.
# ---------------------------------------------------------------------------

_REGISTRY_BASE = "https://registry.modelcontextprotocol.io/v0/servers"
_REGISTRY_TIMEOUT = 8  # seconds
# Per-process response cache: "{limit}:{cursor}" → (response_dict, monotonic_ts)
# Serves stale data on upstream failure so the registry never appears empty just
# because Fly.io egress is briefly unreachable.
_registry_cache: dict[str, tuple[dict, float]] = {}
_REGISTRY_CACHE_TTL = 3600  # seconds


def _extract_install_command(server: dict) -> str:
    """Best-effort extraction of a runnable install command from a registry entry.

    The MCP registry uses a packages[] array with package_arguments. We prefer
    uvx (uv) over npx in that order, then fall back to any available runtime.
    """
    packages = server.get("packages") or []
    # Preference order: uv → npm → any
    _pref = {"uv": 0, "npm": 1}
    best = None
    best_score = 999
    for pkg in packages:
        runtime = (pkg.get("runtime") or "").lower()
        score = _pref.get(runtime, 2)
        if score < best_score:
            best_score = score
            best = pkg
    if not best:
        return ""
    runtime = (best.get("runtime") or "").lower()
    name = best.get("name") or ""
    args = best.get("package_arguments") or []
    # Build a string representation: uvx <name> [args] or npx -y <name> [args]
    if runtime == "uv":
        cmd_parts = ["uvx", name] + [str(a) for a in args if a]
    elif runtime == "npm":
        cmd_parts = ["npx", "-y", name] + [str(a) for a in args if a]
    else:
        cmd_parts = [name] + [str(a) for a in args if a]
    return " ".join(p for p in cmd_parts if p)


@router.get("/tunnel/registry")
async def get_mcp_registry(request: Request) -> Response:
    """Proxy the official MCP Registry API to avoid browser CORS restrictions.

    Returns a page of MCP servers with: id, name, description, install_command,
    homepage. Pass ?cursor=<token> for subsequent pages and ?limit=N (default 20,
    max 50). Requires no auth — the registry is public.
    """
    limit = min(int(request.query_params.get("limit", 20)), 50)
    cursor = request.query_params.get("cursor", "")

    params: dict[str, str] = {"limit": str(limit)}
    if cursor:
        params["cursor"] = cursor

    cache_key = f"{limit}:{cursor}"

    try:
        # 9dde426f — async HTTP so a slow upstream registry can never block the
        # event loop (a synchronous urllib.urlopen here stalled the server for
        # ALL tenants up to _REGISTRY_TIMEOUT seconds). Matches every other
        # server-side outbound call, which use httpx.AsyncClient.
        async with httpx.AsyncClient(timeout=_REGISTRY_TIMEOUT) as http:
            resp = await http.get(
                _REGISTRY_BASE,
                params=params,
                headers={"Accept": "application/json", "User-Agent": "Meridian/1.0"},
            )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        _log.warning("MCP registry fetch failed: %s", exc)
        # Return cached data if available — prevents the browse section from
        # appearing empty just because Fly.io egress is temporarily unreachable.
        cached = _registry_cache.get(cache_key)
        if cached and (time.monotonic() - cached[1]) < _REGISTRY_CACHE_TTL:
            _log.info("MCP registry: serving cached response (age=%.0fs)", time.monotonic() - cached[1])
            return _json_response({**cached[0], "cached": True})
        # No cache — 503 so api() throws and the client falls back to curated list.
        return _json_response(
            {"error": f"registry unavailable: {exc}", "servers": [], "next_cursor": None},
            status_code=503,
        )

    servers_raw = data.get("servers") or []
    servers_out = []
    for s in servers_raw:
        install_cmd = _extract_install_command(s)
        homepage = ""
        if s.get("source_code_location"):
            homepage = s["source_code_location"].get("url", "")
        if not homepage:
            homepage = s.get("homepage", "") or ""
        servers_out.append({
            "id": s.get("id") or "",
            "name": s.get("name") or s.get("id") or "",
            "description": (s.get("description") or "")[:200],
            "install_command": install_cmd,
            "homepage": homepage,
        })

    result = {
        "servers": servers_out,
        "next_cursor": data.get("nextCursor") or data.get("next_cursor") or None,
    }
    _registry_cache[cache_key] = (result, time.monotonic())
    return _json_response(result)


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


def _union_repo_paths(projects: list[dict]) -> list[str]:
    """Collect a deduped, order-preserved list of executor_config.repo_path across
    project rows. These are the dirs the tunnel is *implicitly* trusted to access —
    used by the client for silent auto-add when a filesystem call is denied for a
    path within one of these dirs."""
    paths: list[str] = []
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
        rp = raw.get("repo_path")
        if isinstance(rp, str) and rp.strip() and rp.strip() not in seen:
            seen.add(rp.strip())
            paths.append(rp.strip())
    return paths


def _first_serena_repo_path(projects: list[dict]) -> str:
    """First non-empty ``executor_config.serena_repo_path`` across project rows.

    b970fe07 — the tunnel is tenant-scoped but Serena's default ``--project`` is a
    single path, so we take the first project that sets one (order-preserved by
    ``list_projects``). executor_config may be a JSON string or dict. Returns ``""``
    when no project configures it — the client then keeps today's cwd default.
    """
    for p in projects:
        raw = p.get("executor_config")
        if isinstance(raw, str) and raw.strip():
            try:
                raw = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
        if not isinstance(raw, dict):
            continue
        sp = raw.get("serena_repo_path")
        if isinstance(sp, str) and sp.strip():
            return sp.strip()
    return ""


def _union_codebase_code_dirs(projects: list[dict]) -> list[str]:
    """Deduped, order-preserved union of ``executor_config.codebase_code_dirs``.

    b970fe07 — dirs the code-intel slot (codebase-memory-mcp) auto-indexes at
    tunnel start, unioned across the tenant's projects (mirrors
    ``_union_filesystem_roots``). executor_config may be a JSON string or dict.
    """
    dirs: list[str] = []
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
        for d in (raw.get("codebase_code_dirs") or []):
            if isinstance(d, str) and d.strip() and d.strip() not in seen:
                seen.add(d.strip())
                dirs.append(d.strip())
    return dirs


@router.get("/tunnel/filesystem-roots")
async def get_tunnel_filesystem_roots(request: Request) -> Response:
    """Return the directories the tunnel's filesystem connector may read.

    The dashboard stores these per-project under ``executor_config.filesystem_roots``
    (Settings → Executor Config). The tunnel is tenant-scoped, so this unions the
    roots across all of the tenant's projects. An empty list means the client
    falls back to the user's home directory (current default behaviour). Best-effort:
    any DB error yields an empty list rather than failing the tunnel.

    Also returns ``known_repo_paths`` — the union of ``executor_config.repo_path``
    across all projects. The tunnel client uses these as implicitly-trusted roots
    for the silent path auto-add (item 3 of dynamic-fs-roots feature).

    b970fe07 — additionally returns ``serena_repo_path`` (first non-empty
    ``executor_config.serena_repo_path`` across projects — Serena's default
    ``--project``) and ``codebase_code_dirs`` (deduped union of
    ``executor_config.codebase_code_dirs`` — the code-intel slot's auto-index
    dirs). Both are fall-back-safe: the client applies them only when the
    corresponding CLI flag (``--repo`` / ``--code-dir``) is absent.
    """
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        return _json_response({
            "filesystem_roots": [], "known_repo_paths": [],
            "serena_repo_path": "", "codebase_code_dirs": [],
        })
    try:
        db = await _db(request)
        projects = await db_module.list_projects(db)
    except Exception:  # noqa: BLE001 — unprovisioned/unreachable DB → defaults
        projects = []
    return _json_response({
        "filesystem_roots": _union_filesystem_roots(projects),
        "known_repo_paths": _union_repo_paths(projects),
        # b970fe07 — Serena default repo + code-intel index dirs.
        "serena_repo_path": _first_serena_repo_path(projects),
        "codebase_code_dirs": _union_codebase_code_dirs(projects),
    })


async def send_add_fs_roots_control(tenant_id: str, roots: list[str]) -> dict[str, str]:
    """Send add_fs_roots over the main (fs) WebSocket for a tenant.

    Called by the MCP handler after start_session detects a new repo_path.
    Returns a status dict with ``status`` of ``"ok"``, ``"not_connected"``, or
    ``"error"``. Non-blocking best-effort — callers should never let this fail
    the start_session response.
    """
    ws = _tunnel_sockets.get(tenant_id)
    if ws is None:
        return {"status": "not_connected", "message": "no active fs tunnel"}
    try:
        await ws.send_json({"type": "add_fs_roots", "roots": roots})
        return {"status": "ok", "roots": roots}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": str(exc)}


# live-fs-roots — full-list REPLACE control for the fs slot. add_fs_roots only
# ADDS dirs to the running proxy, so a *removal* needs a way to shrink the served
# set live. This sends the complete new root list; the client rebuilds the fs
# proxy's allowed-dirs to EXACTLY these and respawns. Mirrors
# :func:`send_add_fs_roots_control` (same socket, same best-effort contract).
async def send_set_fs_roots_control(tenant_id: str, roots: list[str]) -> dict[str, str]:
    """Send set_fs_roots (full-list replace) over the fs WebSocket for a tenant.

    Called after a root is removed from the persisted config so the change goes
    live without a tunnel restart. Returns a status dict with ``status`` of
    ``"ok"``, ``"not_connected"``, or ``"error"``. Best-effort — never let this
    fail the calling request.
    """
    ws = _tunnel_sockets.get(tenant_id)
    if ws is None:
        return {"status": "not_connected", "message": "no active fs tunnel"}
    try:
        await ws.send_json({"type": "set_fs_roots", "roots": roots})
        return {"status": "ok", "roots": roots}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": str(exc)}


# 0e973e52 — run_verification: timeout for waiting on the client's run_cmd_result.
# The test suite may be slow (minutes); default 300 s is generous but bounded.
# Configurable via MERIDIAN_RUN_CMD_TIMEOUT for power users.
def _run_cmd_timeout() -> float:
    raw = os.environ.get("MERIDIAN_RUN_CMD_TIMEOUT", "").strip()
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    return 300.0


async def send_run_cmd_control(
    tenant_id: str,
    cmd: str | list[str],
    cwd: str | None = None,
) -> dict:
    """0e973e52 — send a run_cmd control message over the FS WebSocket.

    Sends ``{"type": "run_cmd", "id": "<uuid>", "cmd": ..., "cwd": ...}`` to the
    tenant's local tunnel client and awaits a ``{"type": "run_cmd_result", ...}``
    reply. Returns a structured result dict::

        {
            "exit_code": int,
            "passed": int | None,    # parsed from pytest/pixi output
            "failed": int | None,
            "stdout_tail": str,
            "stderr_tail": str,
            "timed_out": bool,       # True when client didn't reply in time
            "status": "ok" | "not_connected" | "error" | "timeout",
        }

    Requires an active FS tunnel (``_tunnel_sockets``). If no tunnel is connected
    returns ``{"status": "not_connected", ...}`` so callers can surface an honest
    "not configured" message instead of a spurious error.
    """
    ws = _tunnel_sockets.get(tenant_id)
    if ws is None:
        return {
            "status": "not_connected",
            "message": "tunnel not connected — run `meridian --tunnel` first",
            "exit_code": None,
            "passed": None,
            "failed": None,
            "stdout_tail": "",
            "stderr_tail": "",
        }

    req_id = str(uuid.uuid4())
    loop = asyncio.get_event_loop()
    fut: asyncio.Future[dict] = loop.create_future()
    _pending_run_cmd_reqs[req_id] = fut

    try:
        await ws.send_json({
            "type": "run_cmd",
            "id": req_id,
            "cmd": cmd,
            "cwd": cwd or "",
        })
        result = await asyncio.wait_for(fut, timeout=_run_cmd_timeout())
    except asyncio.TimeoutError:
        _pending_run_cmd_reqs.pop(req_id, None)
        return {
            "status": "timeout",
            "message": f"test command timed out after {_run_cmd_timeout():.0f}s",
            "exit_code": None,
            "passed": None,
            "failed": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "timed_out": True,
        }
    except Exception as exc:  # noqa: BLE001
        _pending_run_cmd_reqs.pop(req_id, None)
        return {
            "status": "error",
            "message": str(exc),
            "exit_code": None,
            "passed": None,
            "failed": None,
            "stdout_tail": "",
            "stderr_tail": "",
        }
    finally:
        _pending_run_cmd_reqs.pop(req_id, None)

    # The client sends back the structured result directly.
    return result


# live-fs-roots — POST/DELETE /tunnel/filesystem-roots: add or remove a single
# served root LIVE from the dashboard. The tunnel is tenant-scoped and the served
# set is the UNION of ``executor_config.filesystem_roots`` across the tenant's
# projects (see :func:`_union_filesystem_roots`). So:
#   * ADD persists the new path onto ONE project (the newest, matching how the
#     GET/union path treats projects) then pushes it live with add_fs_roots.
#   * DELETE strips the path from EVERY project that lists it (it could live on
#     any of them) then pushes the full new union with set_fs_roots.
# Normalization reuses the client's ``_normalize_path_arg`` so a pasted/quoted
# path persists byte-identically to what the client will serve.

def _normalize_root(path: Any) -> str:
    """Normalize an incoming root path exactly like the tunnel client does.

    Strips surrounding whitespace and matched surrounding quotes (a pasted
    ``'"C:\\Users\\me\\My Docs"'`` becomes ``C:\\Users\\me\\My Docs``). Reuses
    :func:`meridian.tunnel_client._normalize_path_arg` so the server-persisted
    value matches the client-served dir exactly. Non-str input → ``""``.
    """
    if not isinstance(path, str):
        return ""
    from ..tunnel_client import _normalize_path_arg
    return _normalize_path_arg(path)


async def _persist_add_filesystem_root(
    db: Any, projects: list[dict], path: str
) -> list[str]:
    """Append *path* to the newest project's ``executor_config.filesystem_roots``
    (deduped, order preserved) and persist it. Returns the updated tenant-wide
    UNION of filesystem roots. If the path is already served nothing is written.
    """
    if path in _union_filesystem_roots(projects):
        return _union_filesystem_roots(projects)
    if not projects:
        # No project to attach the root to — nothing to persist. The union is
        # necessarily empty; the caller still pushes the path live so the running
        # tunnel serves it this session.
        return []
    target = projects[0]  # newest (list_projects orders created_at DESC)
    cfg = target.get("executor_config")
    if isinstance(cfg, str) and cfg.strip():
        try:
            cfg = json.loads(cfg)
        except Exception:  # noqa: BLE001
            cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    roots = [r for r in (cfg.get("filesystem_roots") or []) if isinstance(r, str)]
    if path not in roots:
        roots.append(path)
    cfg["filesystem_roots"] = roots
    await db_module.set_executor_config(db, target["id"], cfg)
    # Reflect the write in our in-memory copy so the recomputed union is current.
    target["executor_config"] = cfg
    return _union_filesystem_roots(projects)


async def _persist_remove_filesystem_root(
    db: Any, projects: list[dict], path: str
) -> list[str]:
    """Remove *path* from EVERY project whose ``filesystem_roots`` lists it and
    persist each changed project. Returns the updated tenant-wide UNION.
    """
    for p in projects:
        cfg = p.get("executor_config")
        if isinstance(cfg, str) and cfg.strip():
            try:
                cfg = json.loads(cfg)
            except Exception:  # noqa: BLE001
                continue
        if not isinstance(cfg, dict):
            continue
        roots = [r for r in (cfg.get("filesystem_roots") or []) if isinstance(r, str)]
        # Drop the target path (trim each stored value so a whitespace-padded
        # entry still matches, mirroring the union's own trimming).
        kept = [r for r in roots if r.strip() != path]
        if len(kept) != len(roots):
            cfg["filesystem_roots"] = kept
            await db_module.set_executor_config(db, p["id"], cfg)
            p["executor_config"] = cfg
    return _union_filesystem_roots(projects)


@router.post("/tunnel/filesystem-roots")
async def add_tunnel_filesystem_root(request: Request) -> Response:
    """live-fs-roots — add a served filesystem root and push it live.

    Body: ``{"path": "..."}`` (``"root"`` is also accepted). The path is
    normalized (surrounding quotes/whitespace stripped) and rejected if empty.
    It is appended to the tenant's persisted ``executor_config.filesystem_roots``
    (deduped, order preserved), then pushed to the running tunnel via
    ``add_fs_roots`` so the connector serves it WITHOUT a restart. Returns the
    updated union of ``roots`` plus ``live`` (the control-send status).
    """
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        return _json_response({"error": "authentication required"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _json_response({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return _json_response({"error": "expected a JSON object"}, status_code=400)
    path = _normalize_root(body.get("path") if body.get("path") is not None else body.get("root"))
    if not path:
        return _json_response({"error": "path is required"}, status_code=400)
    try:
        db = await _db(request)
        projects = await db_module.list_projects(db)
        roots = await _persist_add_filesystem_root(db, projects, path)
    except Exception as exc:  # noqa: BLE001
        return _json_response({"error": f"could not persist root: {exc}"}, status_code=500)
    live = await send_add_fs_roots_control(tenant["id"], [path])
    return _json_response({"roots": roots, "live": live})


@router.delete("/tunnel/filesystem-roots")
async def remove_tunnel_filesystem_root(request: Request) -> Response:
    """live-fs-roots — remove a served filesystem root and apply it live.

    Path comes from a ``{"path": ...}`` JSON body (``"root"`` accepted too) or a
    ``?path=`` query param. It is normalized then stripped from the tenant's
    persisted ``executor_config.filesystem_roots`` across all projects. Because
    ``add_fs_roots`` only ADDS, removal is pushed live with ``set_fs_roots`` (a
    full-list replace) so the running connector stops serving it WITHOUT a
    restart. Returns the updated union of ``roots`` plus ``live`` status.
    """
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        return _json_response({"error": "authentication required"}, status_code=401)
    path = _normalize_root(request.query_params.get("path"))
    if not path:
        try:
            body = await request.json()
            if isinstance(body, dict):
                path = _normalize_root(
                    body.get("path") if body.get("path") is not None else body.get("root")
                )
        except Exception:  # noqa: BLE001 — no/invalid body; path may be in query
            pass
    if not path:
        return _json_response({"error": "path is required"}, status_code=400)
    try:
        db = await _db(request)
        projects = await db_module.list_projects(db)
        roots = await _persist_remove_filesystem_root(db, projects, path)
    except Exception as exc:  # noqa: BLE001
        return _json_response({"error": f"could not persist removal: {exc}"}, status_code=500)
    live = await send_set_fs_roots_control(tenant["id"], roots)
    return _json_response({"roots": roots, "live": live})


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

_TUNNEL_LABELS = ("fs", "code", "extract", "ppt", "word", "dc", "docs", "zotero", "outputs") + _CUSTOM_SLOTS

# Human-readable connector prefix shown to Claude in tool names (e.g.
# "filesystem:read_file" instead of "fs:read_file"). The routing cache still
# maps the prefixed name back to the internal slot label, and call_tunnel_tool
# strips the prefix before forwarding, so the display name is cosmetic.
SLOT_DISPLAY_NAMES = {
    "fs": "filesystem",
    "code": "codebase",
    "extract": "extractor",
    "ppt": "powerpoint",
    "word": "word",
    "dc": "desktop-commander",
    "docs": "meridian-docs",
    "zotero": "zotero-mcp",
    # 469d89b4 — outputs slot: tools namespaced as "meridian-outputs__search_outputs" etc.
    "outputs": "meridian-outputs",
    "p0": "custom-p0",
    "p1": "custom-p1",
    "p2": "custom-p2",
    "p3": "custom-p3",
}


def _display_pretty(display: str) -> str:
    """Human label for a slot display name shown in tool titles:
    ``desktop-commander`` → ``Desktop Commander``, ``filesystem`` → ``Filesystem``."""
    return display.replace("-", " ").replace("_", " ").title()


def _namespace_source_title(title: Any, src: str) -> "str | None":
    """Prefix a bare tool title with its connector source (``Filesystem: Read File``).

    Returns the namespaced title, or ``None`` when nothing should change (blank /
    non-string title, or one already carrying the ``"{src}: "`` prefix). The
    idempotency guard matches the EXACT ``"{src}: "`` prefix — not a loose
    ``startswith(src)`` — so a legitimate title that merely begins with the source
    word (e.g. the word slot's "Word count") is still namespaced, while a
    re-listed already-namespaced title is left alone.
    """
    if not isinstance(title, str) or not title.strip():
        return None
    if title.startswith(f"{src}: "):
        return None
    return f"{src}: {title}"


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
# Names are connector-namespaced by list_tunnel_tools using SLOT_DISPLAY_NAMES,
# so these are the filesystem connector's read tools — "filesystem__read_file" /
# "filesystem__read_multiple_files". (codebase__read_file etc. would be a different
# server and is intentionally not matched.)
_READ_TOOLS_TO_REWRITE = frozenset({"filesystem__read_file", "filesystem__read_multiple_files"})


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
    if label == "dc":
        return _tunnel_dc_sockets, _pending_dc_reqs
    if label == "docs":
        return _tunnel_docs_sockets, _pending_docs_reqs
    if label == "zotero":
        return _tunnel_zotero_sockets, _pending_zotero_reqs
    if label == "outputs":
        return _tunnel_outputs_sockets, _pending_outputs_reqs
    if label in _tunnel_custom_sockets:  # 8fb69d54 — custom slots p0-p3
        return _tunnel_custom_sockets[label], _pending_custom_reqs[label]
    return _tunnel_extract_sockets, _pending_extract_reqs


def has_active_tunnel(tenant_id: str) -> bool:
    """True if the tenant has at least one live tunnel socket (any kind)."""
    return (
        tenant_id in _tunnel_sockets
        or tenant_id in _tunnel_code_sockets
        or tenant_id in _tunnel_extract_sockets
        or tenant_id in _tunnel_ppt_sockets
        or tenant_id in _tunnel_word_sockets
        or tenant_id in _tunnel_dc_sockets
        or tenant_id in _tunnel_docs_sockets
        or tenant_id in _tunnel_zotero_sockets
        or tenant_id in _tunnel_outputs_sockets
        or any(tenant_id in s for s in _tunnel_custom_sockets.values())
    )


def tunnel_cross_instance_miss(tenant: "dict | None") -> bool:
    """a19538fe — True when the control-plane DB says this tenant's tunnel is
    active but THIS Fly instance holds no socket for it.

    Tunnel socket state is a per-process in-memory dict, so on Fly.io
    multi-instance a request the load balancer routes to a sibling instance sees
    an in-memory MISS even though the tunnel is genuinely open on another
    machine. ``tenant.tunnel_active`` is a DB flag set on connect / cleared on
    disconnect (the same flag get_tunnel_plugins already falls back to for the
    status display), so ``DB-active AND in-memory-miss`` is exactly the
    cross-instance case. Callers use this to fail LEGIBLY ("held by another
    instance, retry / reconnect") instead of a misleading "not connected" /
    "unknown tool". It does NOT fix routing — it makes the miss honest (the real
    fix, Fly-replay instance affinity, is af5b5739 per decision 229441bc)."""
    if not tenant:
        return False
    return bool(tenant.get("tunnel_active")) and not has_active_tunnel(
        tenant.get("id", "")
    )


# a19538fe — user-facing message for the cross-instance miss, shared by the
# MCP tool-call path and the HTTP proxy path so both report it identically.
CROSS_INSTANCE_MISS_MESSAGE = (
    "your Meridian tunnel is connected, but to a different server instance than "
    "the one handling this request — retry (it may route to the right one), or "
    "restart your local `meridian --tunnel` if this persists"
)


# af5b5739 / decision 229441bc — cross-instance tunnel routing via Fly-replay.
#
# The tunnel socket registry (``_tunnel_sockets`` &c.) is per-PROCESS in-memory, so
# on Fly.io multi-machine a request the edge routes to a sibling instance sees an
# in-memory MISS even though the tunnel is genuinely open on another machine
# (``tunnel_cross_instance_miss``). a19538fe made that miss *legible*; this makes it
# *routable*: when a request lands on the wrong instance, we ask Fly to REPLAY it on
# the instance that actually holds the socket by returning a ``fly-replay`` response
# header of the form ``instance=<machine-id>`` (Fly's documented replay mechanism —
# the edge re-dispatches the same request to the named machine).
#
# We capture the owning instance id on WS connect into a module-level map keyed by
# tenant (decision 229441bc picked a tenant->instance-id column over Redis; a
# module-level map is the lowest-risk unit-testable shape and mirrors the existing
# per-process socket registries — a future PR can promote it to the DB column for
# survival across the owning instance's own restart). ``FLY_ALLOC_ID`` (v1 apps) and
# ``FLY_MACHINE_ID`` (Machines) are the two env vars Fly exposes for the current
# instance id; we read whichever is set.
_tenant_owner_instance: dict[str, str] = {}


def _fly_instance_id() -> "str | None":
    """This Fly instance's id (or None when not running on Fly).

    Fly exposes the current machine id as ``FLY_MACHINE_ID`` (Machines platform) and
    the legacy allocation id as ``FLY_ALLOC_ID``. Prefer the machine id — it's the
    value ``fly-replay: instance=<id>`` targets — and fall back to the alloc id."""
    for var in ("FLY_MACHINE_ID", "FLY_ALLOC_ID"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return None


def record_tenant_owner_instance(tenant_id: str) -> "str | None":
    """Record THIS instance as the owner of ``tenant_id``'s tunnel socket (af5b5739).

    Called on WS connect. No-op (returns None) when not on Fly — there is then only
    one instance and cross-instance routing is meaningless. Returns the recorded
    instance id so callers/tests can assert it."""
    inst = _fly_instance_id()
    if inst:
        _tenant_owner_instance[tenant_id] = inst
    return inst


def clear_tenant_owner_instance(tenant_id: str, instance_id: "str | None" = None) -> None:
    """Forget the owning instance for ``tenant_id`` (af5b5739), called on WS close.

    When ``instance_id`` is given, only clear if it still matches — so a stale
    disconnect from an already-replaced connection can't erase the new owner's claim
    (mirrors the socket-eviction guard on reconnect)."""
    if instance_id is None or _tenant_owner_instance.get(tenant_id) == instance_id:
        _tenant_owner_instance.pop(tenant_id, None)


def tenant_owner_instance(tenant_id: str) -> "str | None":
    """The Fly instance id known to own ``tenant_id``'s tunnel socket, or None."""
    return _tenant_owner_instance.get(tenant_id)


def fly_replay_target(tenant: "dict | None") -> "str | None":
    """The ``fly-replay`` header VALUE to re-route a cross-instance miss (af5b5739).

    Returns ``"instance=<id>"`` when, for this tenant, THIS instance has no live
    socket (in-memory miss) but a DIFFERENT Fly instance is known to own one — i.e.
    the request should be replayed there. Returns None (a no-op) when:

      * not running on Fly (no self instance id), or
      * this instance already holds the socket (no miss — handle it locally), or
      * no owning instance is known for the tenant, or
      * the known owner IS this instance (replaying to ourselves would loop).

    The caller sets ``fly-replay: <value>`` on the response and Fly re-dispatches the
    original request to the named machine, which does hold the socket."""
    if not tenant:
        return None
    self_inst = _fly_instance_id()
    if not self_inst:
        return None  # not on Fly → single instance, nothing to replay to
    tenant_id = tenant.get("id", "")
    if not tenant_id or has_active_tunnel(tenant_id):
        return None  # we hold it (or no id) → serve locally, don't replay
    owner = _tenant_owner_instance.get(tenant_id)
    if not owner or owner == self_inst:
        return None  # unknown owner, or owner is us (would loop)
    return f"instance={owner}"


def fly_replay_target_for_id(tenant_id: str) -> "str | None":
    """``fly_replay_target`` keyed by tenant id (af5b5739).

    The HTTP proxy path (`_do_proxy`) only has the tenant id, not the full tenant
    dict, so it resolves the replay target here. Same guards as
    :func:`fly_replay_target`: no-op off Fly, when we hold the socket, when the
    owner is unknown, or when the owner is us."""
    return fly_replay_target({"id": tenant_id})


# af5b5739 — response header Fly reads to re-dispatch a request to another machine.
FLY_REPLAY_HEADER = "fly-replay"


def fly_replay_response(target: str) -> Response:
    """Build the response that carries the ``fly-replay`` header (af5b5739).

    Fly's edge intercepts the header and REPLAYS the original request on the named
    instance, so the body/status here are only what a client sees if the replay
    somehow doesn't happen — hence a legible 503 + message rather than a misleading
    success."""
    return Response(
        content=('{"status":"replaying","message":'
                 f'"{CROSS_INSTANCE_MISS_MESSAGE}"}}').encode(),
        status_code=503,
        media_type="application/json",
        headers={FLY_REPLAY_HEADER: target},
    )


def active_tunnel_tenant_ids() -> "set[str]":
    """4b698ea5 — every tenant_id that currently holds ≥1 live tunnel socket.

    A live tunnel is proof the tenant's local ``meridian --tunnel`` binary is
    running — a liveness signal that is independent of whether any Meridian tool
    was called. The keepalive loop iterates these each tick to hold the tenant's
    live session fresh through long stretches of non-Meridian work.
    """
    ids: set[str] = set()
    ids.update(_tunnel_sockets)
    ids.update(_tunnel_code_sockets)
    ids.update(_tunnel_extract_sockets)
    ids.update(_tunnel_ppt_sockets)
    ids.update(_tunnel_word_sockets)
    ids.update(_tunnel_dc_sockets)
    ids.update(_tunnel_docs_sockets)
    ids.update(_tunnel_zotero_sockets)
    ids.update(_tunnel_outputs_sockets)
    for s in _tunnel_custom_sockets.values():
        ids.update(s)
    return ids


async def keepalive_tunnel_sessions(app: Any) -> "list[str]":
    """4b698ea5 — passive liveness pass: for each tenant with a live tunnel,
    refresh ``last_seen`` on that tenant's most-recently-active session.

    Returns the list of session ids refreshed (for tests / observability).

    Safety / cost:
      * Only tenants whose project DB is ALREADY cached are touched — we never
        provision or open a fresh DB connection from this background loop.
      * A tenant with no cached DB has made no MCP call, so it has no session to
        keep alive yet; it is simply skipped until it does.
      * Every tenant is isolated in its own try/except so one bad DB can't stall
        the sweep for the others.
    """
    from .._deps import _tenant_db_cache  # noqa: PLC0415

    refreshed: list[str] = []
    for tenant_id in active_tunnel_tenant_ids():
        conn = _tenant_db_cache.get(tenant_id)
        if conn is None:
            continue  # no MCP activity yet → nothing to keep alive
        try:
            sid = await db_module.touch_latest_active_session(conn)
            if sid:
                refreshed.append(sid)
        except Exception:  # noqa: BLE001 — a failed bump must not kill the loop
            _log.debug("tunnel keepalive: touch failed for tenant %s", tenant_id[:8])
    return refreshed


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
    repo_path: str | None = None,
) -> dict | None:
    """Send one JSON-RPC request to a tunneled MCP server and parse the reply.

    4d9ad87b — ``repo_path`` is forwarded as ``X-Meridian-Repo-Path`` so the
    tunnel's SerenaDaemonPool can route code-intel requests to the correct
    per-repo daemon without the server knowing about the pool directly.
    """
    sockets, pending = _label_maps(label)
    if tenant_id not in sockets:
        return None
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": "meridian-bridge",
        "method": method,
        "params": params or {},
    }).encode()
    headers: dict[str, str] = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    if repo_path:
        headers["x-meridian-repo-path"] = repo_path
    resp = await _do_proxy(
        tenant_id, "POST", "/mcp", "", headers, body, sockets, pending, label,
    )
    if resp.status_code >= 400:
        return None
    return _parse_mcp_payload(resp.body)


async def _fetch_slot_tools(tenant_id: str, label: str) -> "tuple[str, list[dict]]":
    """Fetch tools/list for one tunnel slot with retries. Returns (label, tools)."""
    sockets, _ = _label_maps(label)
    if tenant_id not in sockets:
        return label, []
    tools: list[dict] = []
    for _attempt in range(4):  # initial try + up to 3 retries
        try:
            resp = await asyncio.wait_for(
                _tunnel_jsonrpc(tenant_id, label, "tools/list", {}),
                timeout=3.0,
            )
            tools = ((resp.get("result") or {}).get("tools")) or [] if resp else []
        except Exception as exc:  # noqa: BLE001
            _log.debug("tunnel %s tools/list failed for %s: %s", label, tenant_id[:8], exc)
            tools = []
        if tools:
            break
        if _attempt < 3:
            await asyncio.sleep(0.5)
    return label, tools


async def list_tunnel_tools(
    tenant_id: str, reserved_names: "frozenset[str] | set[str]" = frozenset(),
) -> list[dict]:
    """Aggregate tools from every active tunnel and refresh the routing cache.

    Each tool name is namespaced by its connector slot — ``codebase__trace_path``,
    ``extractor__get_symbols_tool``, ``filesystem__read_file`` — so Claude can tell the
    connectors apart and tool search surfaces them by connector. The prefixed
    name is what's advertised and what the routing cache is keyed by;
    ``call_tunnel_tool`` strips the prefix before forwarding to the tunnel.
    Because prefixed names can't collide with native/GitHub (bare) tool names,
    ``reserved_names`` no longer needs to drop them — it's kept for forward
    compatibility but matched against the prefixed name (a no-op in practice).
    Slots are fetched in parallel so one slow/dead slot can't block the others.

    Each surfaced tool is shaped so claude.ai's Tool Permissions screen lists it
    as a distinct, source-labelled entry: the ``name`` is slot-prefixed (so no two
    slots collide), and BOTH title carriers the permission UI reads —
    ``annotations.title`` and top-level ``title`` — are namespaced with the
    connector source so two slots exposing the same bare title (e.g. two
    "Read File"s) don't render as indistinguishable duplicates.
    """
    aggregated: list[dict] = []
    routes: dict[str, str] = {}
    # d71ba2e7 — skip slots a plugin_status message marked unhealthy so we never
    # advertise tools that would 503 on first call.
    healthy_labels = [l for l in _TUNNEL_LABELS if _slot_is_healthy(tenant_id, l)]
    slot_results = await asyncio.gather(
        *[_fetch_slot_tools(tenant_id, label) for label in healthy_labels]
    )
    for label, tools in slot_results:
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name")
            if not name:
                continue
            display = SLOT_DISPLAY_NAMES.get(label, label)
            prefixed = f"{display}__{name}"
            if prefixed in reserved_names or prefixed in routes:
                continue
            tool_copy = dict(tool)
            tool_copy["name"] = prefixed
            # connector-source — claude.ai's tool-permission UI resolves a tool's
            # display title as ``annotations.title`` → top-level ``title`` →
            # humanized name (per the MCP spec's title precedence). The prefixed
            # NAME already carries the source (codebase__search_graph →
            # "Codebase search graph"), but a slot whose inner server advertises a
            # bare tool title (filesystem's "Read File", serena, desktop-commander)
            # would display WITHOUT its plugin source — and two slots exposing the
            # same bare title (e.g. two "Read File"s) look duplicated / are hard to
            # tell apart on the permission screen. Namespace BOTH title carriers so
            # every slot indicates its connector consistently. Older servers put
            # their human label in ``annotations.title`` (ToolAnnotations), so that
            # field must be namespaced too, not just top-level ``title``. Guarded
            # against double-prefixing; nested inputSchema param titles are untouched.
            _src = _display_pretty(display)
            _new_title = _namespace_source_title(tool.get("title"), _src)
            if _new_title is not None:
                tool_copy["title"] = _new_title
            # ``annotations`` is shallow-shared via dict(tool); copy it before
            # editing so we never mutate the inner server's advertised object.
            _annot = tool.get("annotations")
            if isinstance(_annot, dict):
                _annot_title = _namespace_source_title(_annot.get("title"), _src)
                if _annot_title is not None:
                    annot_copy = dict(_annot)
                    annot_copy["title"] = _annot_title
                    tool_copy["annotations"] = annot_copy
            routes[prefixed] = label  # route back via the internal slot label
            aggregated.append(_rewrite_tool_description(tool_copy))
    if routes:
        _tunnel_tool_routes[tenant_id] = routes
    elif not has_active_tunnel(tenant_id):
        _tunnel_tool_routes.pop(tenant_id, None)
    # 54ddd609 — this (re)aggregation reflects the current live slot health, so a
    # pending tools/list_changed marker (set on a slot recovery) is now satisfied:
    # the caller is observing the recovered tools. Drain it so it fires only once.
    _tools_list_changed_pending.discard(tenant_id)
    return aggregated


# ---------------------------------------------------------------------------
# 73d233e4 — concurrent-write protection for the word/office (docx) tunnel path.
#
# The word slot (docx-mcp) is a pure network relay: two agents editing DIFFERENT
# sections of the same .docx over the tunnel silently overwrite each other, because
# a .docx is a zip container with NO partial write — every mutating tool re-saves
# the whole file, so last-save-wins. There is no native cross-agent lock for a
# tunneled Office document.
#
# Meridian already owns the vendor-neutral coordination primitive for exactly this:
# file claims (claim_file / get_file_claims) + the pure evaluate_claim_guard
# decision core. The fix wires the word write path into that machinery — before a
# MUTATING word tool is relayed, we consult the target document's live claims and
# REFUSE the write when another live session holds a conflicting write/symbol claim
# on it. A read tool, or a write on a document nobody else has claimed, passes
# through untouched. Keyed on the target document path so two agents on two
# different .docx files never contend.
# ---------------------------------------------------------------------------

# docx-mcp mutating tools re-serialize the whole zip container (no partial write),
# so every one of these is a last-save-wins hazard under concurrent editing. Names
# are the bare docx-mcp tool names (the connector prefix is stripped before this
# runs). Kept as a curated allowlist of *known* writers rather than a "not in a
# read set" heuristic so a new read tool can never be mis-flagged as a writer.
_WORD_WRITE_TOOLS = frozenset({
    "create_document",
    "save_document",
    "add_paragraph",
    "add_heading",
    "add_table",
    "add_image",
    "add_picture",
    "add_page_break",
    "add_break",
    "add_list",
    "add_bullet_list",
    "add_numbered_list",
    "set_paragraph_text",
    "edit_paragraph",
    "update_paragraph",
    "replace_text",
    "replace_paragraph",
    "delete_paragraph",
    "insert_paragraph",
    "set_heading",
    "set_style",
    "apply_style",
    "set_cell_text",
    "update_cell",
    "merge_cells",
    "set_header",
    "set_footer",
    "set_properties",
    "set_document_properties",
    "convert_document",
    "copy_document",
})

# Argument keys a docx-mcp tool uses to name its target document, in priority order.
_WORD_DOC_ARG_KEYS = ("filename", "file_path", "path", "document", "document_path",
                      "doc_path", "output_path", "target")


def _word_write_target(name: str, arguments: "dict | None") -> "str | None":
    """Return the target document path for a MUTATING word-slot tool, else None.

    ``name`` is the BARE (prefix-stripped) docx-mcp tool name. Returns None for a
    read-only tool, an unknown tool, or a writer whose target document path can't
    be found in ``arguments`` (fail-open — we can't guard what we can't identify).
    """
    bare = name.split("__", 1)[1] if "__" in name else name
    if bare not in _WORD_WRITE_TOOLS:
        return None
    if not isinstance(arguments, dict):
        return None
    for key in _WORD_DOC_ARG_KEYS:
        val = arguments.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


async def check_word_write_conflict(
    db: Any,
    tenant_id: str,
    name: str,
    arguments: "dict | None",
    session_id: "str | None" = None,
) -> "dict | None":
    """Consult file claims before relaying a MUTATING word-slot tool (73d233e4).

    Returns a conflict verdict dict ``{"blocked": True, "document": path,
    "holder": <session>, "reason": <reason>, "message": <human text>}`` when
    another live session holds a conflicting write/symbol claim on the target
    document, else ``None`` (clear — the relay may proceed).

    Coordination reuses the existing primitives: :func:`get_file_claims` for the
    live claims on the document and :func:`evaluate_claim_guard` (the vendor-neutral
    decision core) for the allow/block decision, keyed on ``session_id`` (the caller
    treated as claim-less when unknown, so ANY other session's write claim conflicts).

    Fail-open by design — a missing db, an unidentifiable target, or a claim-lookup
    error degrades to None (no block). This guard SURFACES a conflict; the .docx is
    still a real last-save-wins hazard, so a caller that wants a hard gate should
    claim_file the document first.
    """
    target = _word_write_target(name, arguments)
    if not target or db is None:
        return None
    try:
        claims = await db_module.get_file_claims(db, target)
    except Exception as exc:  # noqa: BLE001 — never wedge a write on a lookup error
        _log.debug("word write-guard: get_file_claims(%s) failed: %s", target, exc)
        return None
    from ..claim_guard import evaluate_claim_guard  # noqa: PLC0415
    verdict = evaluate_claim_guard(claims, session_id or "", mode="write")
    if verdict.get("allow", True):
        return None
    holder = verdict.get("holder")
    reason = verdict.get("reason") or "write_locked"
    return {
        "blocked": True,
        "document": target,
        "holder": holder,
        "reason": reason,
        "message": (
            f"Concurrent-write conflict on {target}: session {holder} holds a live "
            f"{reason} claim. A .docx is a zip container with no partial write, so a "
            f"relayed edit would silently overwrite the other session's work "
            f"(last-save-wins). Coordinate or wait for the claim to release, then "
            f"retry. To edit in parallel, claim_file the document first."
        ),
    }


# 7ef712a8 — code-intel graph tools identify a project by the *local repo-path
# slug* (e.g. "C-Users-13144-Documents-Meridian-repository"), NOT the Meridian
# planning-project name (e.g. "meridian-build"). A session naturally passes the
# planning name first, gets a bare "project not found", and may abandon
# code-intel. These are the code-slot (codebase-memory-mcp) tools that take a
# project identifier; when one 404s on the project we enrich the error with the
# slug explanation + the list of actually-indexed identifiers (closest first).
_CODE_INTEL_PROJECT_TOOLS = frozenset({
    "search_graph", "query_graph", "trace_path", "get_architecture",
    "get_graph_schema", "search_code", "get_code_snippet", "detect_changes",
    "index_status", "ingest_traces", "manage_adr", "delete_project",
})


def _is_project_not_found_error(msg: str) -> bool:
    """Heuristic: does an error message look like a code-intel project lookup miss?

    The codebase-memory server phrases this a few ways ("project not found",
    "no project ... found", "unknown project", "project '<slug>' does not
    exist"). Match loosely on the (project + not-found) signal so we enrich the
    right failures without hijacking unrelated errors (e.g. a query syntax error).
    """
    low = (msg or "").lower()
    if "project" not in low:
        return False
    return any(
        phrase in low
        for phrase in (
            "not found", "no such", "unknown project", "does not exist",
            "no project", "not indexed", "isn't indexed", "is not indexed",
        )
    )


def _closest_project_ids(wanted: str, available: "list[str]") -> "list[str]":
    """Order ``available`` project identifiers by similarity to ``wanted``.

    Uses difflib so the caller's likely-intended slug floats to the top of the
    hint. Pure string ranking — never raises, returns ``available`` order on any
    degenerate input.
    """
    if not wanted or not available:
        return list(available)
    import difflib  # noqa: PLC0415 — only needed on the error path
    scored = sorted(
        available,
        key=lambda p: difflib.SequenceMatcher(None, wanted.lower(), p.lower()).ratio(),
        reverse=True,
    )
    return scored


async def _list_indexed_project_ids(tenant_id: str) -> "list[str]":
    """Best-effort: fetch the code-intel server's indexed project identifiers.

    Calls the codebase ``list_projects`` tool over the same tunnel and pulls out
    each project's identifier (``id`` / ``project_id`` / ``name`` / ``slug``,
    whichever the server returns). Never raises — returns ``[]`` on any failure so
    error enrichment can't itself throw.
    """
    try:
        result = await call_tunnel_tool(tenant_id, "codebase__list_projects", {})
    except Exception:  # noqa: BLE001 — enrichment must never mask the real error
        return []
    payload = _extract_graph_matches(result)
    projects: Any = payload
    if isinstance(payload, dict):
        projects = (
            payload.get("projects")
            or payload.get("results")
            or payload.get("items")
            or []
        )
    ids: list[str] = []
    if isinstance(projects, list):
        for proj in projects:
            if isinstance(proj, str):
                ids.append(proj)
            elif isinstance(proj, dict):
                ident = (
                    proj.get("id")
                    or proj.get("project_id")
                    or proj.get("name")
                    or proj.get("slug")
                    or proj.get("path")
                )
                if ident:
                    ids.append(str(ident))
    # De-dupe, preserve order.
    seen: set[str] = set()
    return [i for i in ids if not (i in seen or seen.add(i))]


async def _enrich_code_intel_project_error(
    tenant_id: str, name: str, arguments: dict | None, original_msg: str,
) -> str:
    """Turn a bare code-intel "project not found" into an actionable hint.

    Explains that the identifier is the *local repo-path slug*, not the Meridian
    planning-project name, and lists the actually-indexed identifiers (closest
    match to what the caller passed floated to the top). Returns the original
    message unchanged if enrichment doesn't apply or can't add anything.
    """
    bare = name.split("__", 1)[1] if "__" in name else name
    if bare not in _CODE_INTEL_PROJECT_TOOLS:
        return original_msg
    if not _is_project_not_found_error(original_msg):
        return original_msg
    wanted = ""
    if isinstance(arguments, dict):
        wanted = str(
            arguments.get("project_id")
            or arguments.get("project")
            or arguments.get("project_name")
            or ""
        ).strip()
    available = await _list_indexed_project_ids(tenant_id)
    ranked = _closest_project_ids(wanted, available)
    hint = (
        f"{original_msg}\n\n"
        "Note: code-intel graph tools identify a project by its LOCAL REPO-PATH "
        "slug (e.g. \"C-Users-13144-Documents-Meridian-repository\"), NOT the "
        "Meridian planning-project name (e.g. \"meridian-build\")."
    )
    if wanted:
        hint += f" You passed {wanted!r}."
    if ranked:
        shown = ranked[:10]
        hint += "\n\nAvailable indexed project identifiers:\n" + "\n".join(
            f"  - {p}" for p in shown
        )
        if len(ranked) > len(shown):
            hint += f"\n  ... and {len(ranked) - len(shown)} more"
        hint += (
            f"\n\nRetry with the closest match, e.g. project_id={ranked[0]!r}."
        )
    else:
        hint += (
            "\n\nNo indexed projects were reported — call "
            "codebase__list_projects to see what is available, or "
            "codebase__index_repository to index this repo first."
        )
    return hint


# caf95f81 — get_code_snippet truncation detection.
#
# The external codebase-memory-mcp server returns start_line/end_line in its
# get_code_snippet response, but silently truncates the source text when a function
# is very long (confirmed bug: a 114-line function returned ~40 lines short with
# NO indicator).  Meridian cannot patch the plugin directly — same class of
# limitation as 19b3259e / 7ef712a8.  Instead, after a successful get_code_snippet
# call, we inspect the result, compute the expected line count from the declared
# range, count the actual lines in the source text, and attach a clear
# ``truncation_warning`` key when the snippet is meaningfully short.
#
# The source text lives inside the MCP content-block envelope:
#   result["content"][0]["text"] → JSON string → {start_line, end_line, source/code/snippet}
# We probe multiple plausible source-field names in priority order.  Fail-open:
# any missing / malformed / unexpected field silently skips enrichment — the
# caller still gets the (possibly truncated) result unchanged.

# Candidate field names for the source text, in probe order.
_CODE_SNIPPET_SOURCE_FIELDS = ("source", "code", "snippet", "content", "text")

# Slack for trailing-newline edge cases.  A snippet that is only 1 line short
# may simply lack a terminal newline; we only warn when genuinely short by more
# than this many lines.
_TRUNCATION_SLACK = 2


def _check_code_snippet_truncation(result: Any) -> Any:
    """caf95f81 — attach a truncation_warning to a get_code_snippet result when
    the returned source text is meaningfully shorter than the declared line range.

    The result is the MCP ``tools/call`` result object (``{"content": [...]}``)
    returned by ``call_tunnel_tool``.  The warning is added as a top-level
    ``truncation_warning`` key so callers can detect it without re-parsing the
    content blocks.  Returns the result unchanged (no copy) if no truncation is
    detected — including all fail-open cases (missing fields, wrong types,
    parse errors, zero-length range).  Never raises.
    """
    try:
        if not isinstance(result, dict):
            return result
        content = result.get("content")
        if not isinstance(content, list) or not content:
            return result
        # Probe the first text block for the JSON payload.
        payload: Any = None
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            raw_text = block.get("text") or ""
            if not raw_text:
                continue
            try:
                payload = json.loads(raw_text)
            except Exception:  # noqa: BLE001 — non-JSON text block: skip
                continue
            break  # use the first parseable text block
        if not isinstance(payload, dict):
            return result
        # Extract start_line / end_line from the JSON payload.
        start_line = payload.get("start_line")
        end_line = payload.get("end_line")
        if not isinstance(start_line, int) or not isinstance(end_line, int):
            return result
        expected_lines = end_line - start_line + 1
        if expected_lines <= 0:
            return result  # degenerate range — skip
        # Probe the source text field.
        source_text: str | None = None
        for field in _CODE_SNIPPET_SOURCE_FIELDS:
            candidate = payload.get(field)
            if isinstance(candidate, str):
                source_text = candidate
                break
        if source_text is None:
            return result
        # Count lines in the source text.  splitlines() handles \r\n and \r
        # correctly.  An empty string → 0 lines.
        actual_lines = len(source_text.splitlines())
        if (expected_lines - actual_lines) > _TRUNCATION_SLACK:
            result = dict(result)  # shallow copy — don't mutate the original
            result["truncation_warning"] = (
                f"get_code_snippet returned a truncated snippet: expected "
                f"{expected_lines} lines (start_line={start_line}, "
                f"end_line={end_line}) but the source field contains only "
                f"{actual_lines} lines. The tail of the function/block is "
                f"missing. Re-fetch the missing portion via a direct file read "
                f"(e.g. filesystem__read_file with offset={start_line + actual_lines - 1} "
                f"limit={expected_lines - actual_lines + _TRUNCATION_SLACK}) to "
                f"get the complete source."
            )
    except Exception:  # noqa: BLE001 — enrichment must never mask the real result
        pass
    return result


# 2ce5bc76 — code-graph staleness fingerprinting. The codebase-memory-mcp graph
# index goes stale after edits without reliably re-indexing (confirmed failures:
# wrong line numbers, zero results for existing symbols). We store a per-
# (tenant, project_id) fingerprint at index_repository time and compare it on
# each search_graph call. A mismatch injects a clear staleness warning in the
# result so the caller can see it was wrong rather than silently trusting it.
#
# Fingerprint = the git commit hash OR the "indexed_at" timestamp surfaced by
# index_repository / index_status responses — whichever is present. We can't
# call into the external plugin's internals (same class as 7ef712a8/19b3259e),
# so we extract it from the MCP result envelope.
_code_graph_fingerprints: dict[str, str] = {}  # "{tenant_id}:{project_id}" → fingerprint


def _extract_graph_fingerprint(result: Any) -> "str | None":
    """Pull the staleness fingerprint from a code-intel MCP result.

    Looks for ``git_commit``, ``commit_hash``, ``indexed_commit``,
    ``indexed_at``, or ``commit`` in the unwrapped payload — whatever the
    codebase-memory-mcp server chooses to surface. Returns None when nothing
    identifiable is present.
    """
    payload = _extract_graph_matches(result) if isinstance(result, dict) else result
    if not isinstance(payload, dict):
        return None
    _keys = ("git_commit", "commit_hash", "indexed_commit", "commit", "indexed_at", "last_indexed_at")
    for k in _keys:
        v = payload.get(k)
        if v and isinstance(v, str):
            return v.strip()
    # Also look one level down inside a "status" or "index" sub-dict.
    for sub_key in ("status", "index", "metadata"):
        sub = payload.get(sub_key)
        if isinstance(sub, dict):
            for k in _keys:
                v = sub.get(k)
                if v and isinstance(v, str):
                    return v.strip()
    return None


def _graph_fingerprint_key(tenant_id: str, arguments: "dict | None") -> "str | None":
    """The cache key for the staleness fingerprint given a call's arguments.

    Returns ``"{tenant_id}:{project_id}"`` when a project_id is present, or
    ``"{tenant_id}:*"`` as a fallback (single-project tenants / no id passed).
    """
    project_id = ""
    if isinstance(arguments, dict):
        project_id = str(
            arguments.get("project_id")
            or arguments.get("project")
            or arguments.get("project_name")
            or ""
        ).strip()
    return f"{tenant_id}:{project_id or '*'}"


async def _fetch_graph_current_fingerprint(tenant_id: str, arguments: "dict | None") -> "str | None":
    """Best-effort: ask the code-intel server for the current index fingerprint.

    Calls ``codebase__index_status`` (passing the same project_id/project
    arguments the caller used) and extracts the fingerprint from the response.
    Never raises — returns None on any failure so staleness detection degrades
    silently rather than breaking the search call.
    """
    try:
        status_args: dict[str, Any] = {}
        if isinstance(arguments, dict):
            for k in ("project_id", "project", "project_name"):
                if arguments.get(k):
                    status_args[k] = arguments[k]
                    break
        result = await call_tunnel_tool(tenant_id, "codebase__index_status", status_args)
        return _extract_graph_fingerprint(result)
    except Exception:  # noqa: BLE001 — staleness detection must never raise
        return None


async def _annotate_graph_result_staleness(
    tenant_id: str,
    name: str,
    arguments: "dict | None",
    result: "dict | None",
) -> "dict | None":
    """2ce5bc76 — inject a ``_graph_staleness`` warning when the code graph is stale.

    Called after a successful ``search_graph`` call. If the stored fingerprint
    for this (tenant, project_id) differs from the current one fetched via
    ``index_status``, wraps the result in a dict that carries a
    ``_graph_staleness`` warning so the caller can see the index is stale and
    cross-check with Serena. When fingerprints match (or either is unknown),
    the result passes through unchanged. Fail-open — any error returns the
    original result untouched.

    9033914e — when a project-specific fingerprint is absent, fall back to the
    wildcard ``{tenant_id}:*`` key.  ``index_repository`` is always called
    without a ``project_id`` argument (it takes ``repo_path`` instead), so it
    always stores the fingerprint under the wildcard key regardless of which
    project_id a subsequent ``search_graph`` call uses.  Without this fallback,
    the staleness guard silently fires when fingerprints DO diverge because the
    project-specific key is never populated from the index_repository run.
    """
    bare = name.split("__", 1)[1] if "__" in name else name
    if bare != "search_graph":
        return result
    if result is None:
        return result
    try:
        fkey = _graph_fingerprint_key(tenant_id, arguments)
        stored = _code_graph_fingerprints.get(fkey)
        # 9033914e — if no project-specific fingerprint, fall back to the
        # wildcard key that index_repository always writes to.  This bridges
        # the index_repository (no project_id → "*" key) → search_graph (has
        # project_id → project-specific key) gap so staleness detection fires
        # correctly instead of silently doing nothing.
        if not stored:
            wildcard_key = f"{tenant_id}:*"
            if wildcard_key != fkey:
                stored = _code_graph_fingerprints.get(wildcard_key)
        # Only fetch the current fingerprint when we have a stored baseline to
        # compare against. Without a stored fingerprint there's nothing to diff,
        # and we avoid an extra codebase__index_status round-trip on every
        # search_graph call (important: it prevents spurious side-effects in
        # tests and on hot paths where no index_repository has been run yet).
        if not stored:
            return result
        current = await _fetch_graph_current_fingerprint(tenant_id, arguments)
        if current and stored != current:
            # Stale! Surface the warning in the result without destroying it.
            # The MCP result envelope is a dict with "content": [...]; we add an
            # extra ``_graph_staleness`` field to carry the diagnostic so the
            # caller can see it without having to parse the content array.
            enriched = dict(result)
            enriched["_graph_staleness"] = {
                "stale": True,
                "reason": "index-fingerprint-mismatch",
                "stored_fingerprint": stored,
                "current_fingerprint": current,
                "warning": (
                    "The code graph index appears STALE: the fingerprint at "
                    "last index_repository differs from the current index "
                    f"({stored!r} -> {current!r}). Line numbers and symbol "
                    "locations in these results may be wrong (confirmed real "
                    "failures: off-by-440-lines, zero results for existing "
                    "symbols). Cross-check with extractor__find_symbol or "
                    "extractor__find_declaration before trusting line ranges. "
                    "Re-run codebase__index_repository to refresh the graph."
                ),
            }
            return enriched
        # Fingerprints match (or current is unknown) — update the stored value
        # with the current reading for future comparisons.
        if current:
            _code_graph_fingerprints[fkey] = current
    except Exception:  # noqa: BLE001 — never break a search call for staleness bookkeeping
        pass
    return result


async def call_tunnel_tool(
    tenant_id: str, name: str, arguments: dict | None,
    repo_path: str | None = None,
    *,
    db: Any = None,
    session_id: "str | None" = None,
) -> dict | None:
    """Route a ``tools/call`` to the tunnel that owns ``name``.

    Returns the MCP ``result`` object (with ``content``) on success, or None if
    no active tunnel exposes a tool by that name. Raises on a tunnel-reported
    JSON-RPC error so the caller can surface it as a normal MCP error.

    4d9ad87b — ``repo_path`` (explicit or from _tenant_active_repo cache) is
    forwarded as ``X-Meridian-Repo-Path`` so the SerenaDaemonPool on the tunnel
    client routes code-intel requests to the correct per-repo Serena daemon.

    73d233e4 — when a MUTATING word-slot (docx) tool is being relayed and ``db``
    is supplied, consult the target document's live file claims first and RAISE a
    concurrent-write conflict (surfaced to the caller as an MCP error) when another
    live session holds a conflicting write/symbol claim on it. A .docx is a zip
    container with no partial write, so two relayed edits silently last-save-wins
    each other; the guard turns that silent stomp into a legible, coordinatable
    error. Fail-open — no db / unidentifiable target / lookup error passes through
    unchanged.

    2ce5bc76 — when ``index_repository`` succeeds, its fingerprint is stored per
    (tenant, project_id) so future ``search_graph`` calls can detect a stale
    index and inject a ``_graph_staleness`` warning instead of silently returning
    wrong line numbers.
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
    # Filesystem tools require absolute paths. Relative paths resolve against
    # the mcp-proxy CWD (usually the home dir) — not the intended repo root —
    # causing confusing "file not found" errors. Catch them early and return a
    # clear message so the caller knows what to fix.
    if label == "fs" and arguments:
        _PATH_KEYS = ("path", "paths", "source", "destination")
        for _key in _PATH_KEYS:
            _val = arguments.get(_key)
            if _val is None:
                continue
            _candidates = [_val] if isinstance(_val, str) else (_val if isinstance(_val, list) else [])
            for _p in _candidates:
                if not isinstance(_p, str) or not _p:
                    continue
                import os.path as _osp
                if not _osp.isabs(_p) and not (len(_p) >= 2 and _p[1] == ":"):
                    raise RuntimeError(
                        f"filesystem tools require absolute paths — got relative path {_p!r}. "
                        f"Use a full path, e.g. C:\\\\Users\\\\13144\\\\Documents\\\\...\\\\{_p}"
                    )
    # 73d233e4 — concurrent-write protection on the word/office (docx) path. A
    # tunneled .docx write has no partial-write; without this, two sessions editing
    # the same document silently overwrite each other (last-save-wins). Before
    # relaying a MUTATING word tool, consult the target document's live claims and
    # refuse when another live session holds a conflicting write/symbol claim.
    if label == "word":
        conflict = await check_word_write_conflict(
            db, tenant_id, name, arguments, session_id=session_id,
        )
        if conflict is not None:
            raise RuntimeError(conflict["message"])
    # 4d9ad87b — resolve repo_path: explicit arg wins, then cached value from the
    # last send_active_repo_control call for this tenant.
    effective_repo_path = repo_path or _tenant_active_repo.get(tenant_id)
    # The tunneled server only knows the bare tool name; strip the connector prefix.
    bare_name = name.split("__", 1)[1] if "__" in name else name
    resp = await _tunnel_jsonrpc(
        tenant_id, label, "tools/call",
        {"name": bare_name, "arguments": arguments or {}},
        repo_path=effective_repo_path,
    )
    if not resp:
        return None
    err = resp.get("error")
    if err:
        _msg = str(err.get("message") if isinstance(err, dict) else err)
        # 7ef712a8 — a code-intel graph tool that misses on the project id gets a
        # slug-vs-planning-name hint + the list of indexed identifiers so the
        # caller can retry instead of abandoning code-intel. Only the code slot's
        # project-taking tools are eligible; enrichment never itself raises.
        if label == "code":
            try:
                _msg = await _enrich_code_intel_project_error(
                    tenant_id, name, arguments, _msg
                )
            except Exception:  # noqa: BLE001 — surface the original error regardless
                pass
        raise RuntimeError(_msg)
    result = resp.get("result")
    # caf95f81 — detect silent truncation in get_code_snippet responses from the
    # code slot.  When the declared line range (start_line/end_line) is
    # meaningfully larger than the actual source line count, attach a
    # truncation_warning key so the caller knows to re-fetch the missing tail.
    # Pure enrichment — never mutates the original result, never raises.
    if label == "code" and bare_name == "get_code_snippet":
        result = _check_code_snippet_truncation(result)
    # 2ce5bc76 — staleness fingerprinting for the code graph.
    # (a) index_repository success → capture the fingerprint for future comparison.
    # (b) search_graph success → compare against stored fingerprint and inject a
    #     staleness warning in the result when the index appears out of date.
    if label == "code":
        bare = name.split("__", 1)[1] if "__" in name else name
        if bare == "index_repository" and result is not None:
            try:
                fp = _extract_graph_fingerprint(result)
                if fp:
                    fkey = _graph_fingerprint_key(tenant_id, arguments)
                    _code_graph_fingerprints[fkey] = fp
            except Exception:  # noqa: BLE001 — fingerprint capture is best-effort
                pass
        elif bare == "search_graph":
            try:
                result = await _annotate_graph_result_staleness(
                    tenant_id, name, arguments, result
                )
            except Exception:  # noqa: BLE001 — staleness annotation is best-effort
                pass
    return result


def _extract_graph_matches(result: Any) -> Any:
    """Unwrap an MCP ``tools/call`` result into a plain match payload.

    Code-intel ``search_graph`` returns its JSON payload inside the MCP
    ``content[].text`` envelope. Pull the first text block and json-decode it so
    the handoff enrichment layer (``_coerce_match_list``) sees the raw
    ``{"results": [...]}`` dict it already understands. Anything unexpected is
    returned untouched — the caller degrades to no pointers.
    """
    if not isinstance(result, dict):
        return result
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text") or ""
                try:
                    return json.loads(text)
                except Exception:  # noqa: BLE001 — non-JSON text is not a match set
                    return text
    return result


def build_graph_searcher(tenant_id: str | None):
    """Return an async code-graph searcher bound to a tenant's tunnel, or None.

    4cfaecc2 — this is the concrete wiring behind handoff.py's
    ``_resolve_graph_searcher`` seam. When the tenant has an active tunnel
    exposing the code-intel ``search_graph`` tool, the returned coroutine issues
    a ``tools/call`` over the tunnel and normalises the result. When no tunnel is
    active, returns None so enrichment stays a no-op. The searcher itself never
    raises — any tunnel/parse failure yields ``None`` for that query.
    """
    if not tenant_id or not has_active_tunnel(tenant_id):
        return None

    async def _search(query: str) -> Any:
        try:
            result = await call_tunnel_tool(
                tenant_id, "codebase__search_graph", {"query": query, "limit": 3},
            )
        except Exception:  # noqa: BLE001 — best-effort enrichment, never fatal
            return None
        return _extract_graph_matches(result)

    return _search
