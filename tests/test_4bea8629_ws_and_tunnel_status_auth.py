"""Regression tests for sprint item 4bea8629.

SECURITY: two confirmed, unauthenticated cross-tenant leaks on hosted
Meridian (``MERIDIAN_HOSTED=1``) — NOT the 9 MCP-proxy families already
fixed by 5de3d422 (see tests/test_tunnel_routes.py for that coverage).

1. ``GET /ws/{project_id}`` (``meridian/server.py``'s ``ws_project``) had NO
   auth check at all. ``meridian.db._TASK_LISTENERS`` (what
   ``db_module.subscribe_tasks`` registers against) is a process-wide,
   in-process pub/sub keyed ONLY by ``project_id`` — not per-tenant — so on
   hosted Meridian (one process serving many tenants) anyone who knew or
   guessed a ``project_id`` could open this socket and stream that
   project's live task-log events, regardless of which tenant actually
   owns it. Fixed by resolving the caller's own tenant
   (``_get_tenant_from_request``) and confirming that tenant's OWN
   database actually has a project with this id (``_open_tenant_db_by_id``
   + ``db_module.get_project``) before ever reaching
   ``WebSocketBroadcaster.serve`` — rejecting with ``code=4401``, mirroring
   ``routes/tunnel.py``'s ``tunnel_ws`` convention.

2. ``GET /tunnel/status/{tenant_id}`` (``meridian/routes/tunnel.py``) also
   had NO auth at all, leaking live slot health/config/inflight-request
   state for ANY ``tenant_id`` named in the URL. Fixed by hard-requiring
   the caller's own resolved tenant match the path's ``tenant_id`` in
   hosted mode — mirroring the sibling, already-authenticated
   ``/tunnel/diagnostics/{tenant_id}``/``/tunnel/launch-matrix/{tenant_id}``
   routes. ``/tunnel/openai/diagnostics/{tenant_id}`` is intentionally
   unauthenticated per its own docstring and is untouched by this fix.

Self-hosted (``MERIDIAN_HOSTED`` unset) has no tenant concept and must stay
byte-for-byte unaffected for both routes — existing tests
(``tests/test_core.py``'s ``test_websocket_receives_task_event`` and
``test_tunnel_status_returns_inactive_for_unknown_tenant``) already cover
that and must keep passing unmodified.
"""
from __future__ import annotations

import asyncio
import types

import pytest
from starlette.websockets import WebSocketDisconnect

import meridian.server as srv
from meridian.routes import tunnel as tn


def _run(coro):
    return asyncio.run(coro)


def _make_hosted_client(monkeypatch, tmp_path):
    """Hosted-mode TestClient backed by an in-memory auth DB.

    Mirrors tests/test_tunnel_routes.py's and tests/test_v2_hosted.py's
    helper of the same name/shape.
    """
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    # No dedicated Neon URL for the admin-plan fallback below.
    monkeypatch.setenv("MERIDIAN_AUTH_DB", "")

    import importlib
    from fastapi.testclient import TestClient

    server_module = importlib.reload(srv)
    return TestClient(server_module.app)


async def _new_tenant_token(db, email: str) -> str:
    from meridian import db as db_module

    tenant = await db_module.upsert_tenant(db, email)
    raw, _row = await db_module.create_api_token(db, tenant["id"], label="t")
    return raw


async def _new_admin_tenant_with_project(db, email: str, project_name: str):
    """An admin-plan tenant (so ``_open_tenant_db_by_id`` legitimately falls
    back to the shared auth DB — the real resolver path, see
    tests/test_v2_hosted.py's ``test_hooks_accept_valid_bearer_for_admin_tenant``)
    that owns a real project created in that same DB."""
    from meridian import db as db_module

    tenant = await db_module.upsert_tenant(db, email)
    await db_module.update_tenant(db, tenant["id"], plan="admin")
    raw_token, _row = await db_module.create_api_token(db, tenant["id"], label="t")
    project = await db_module.create_project(db, project_name)
    return tenant, raw_token, project


# ---------------------------------------------------------------------------
# 1. GET /tunnel/status/{tenant_id}
# ---------------------------------------------------------------------------


def test_tunnel_status_requires_auth_in_hosted_mode(monkeypatch, tmp_path):
    """No credential at all: previously a plain 200 leaking full status."""
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        raw_token = _run(_new_tenant_token(client.app.state.db, "ts-victim@example.com"))
        tenant_id = client.get(
            "/me", headers={"Authorization": f"Bearer {raw_token}"}
        ).json()["tenant_id"]

        r = client.get(f"/tunnel/status/{tenant_id}")
        assert r.status_code == 401


def test_tunnel_status_rejects_cross_tenant_credential(monkeypatch, tmp_path):
    """A DIFFERENT tenant's otherwise-valid credential must not read the
    named tenant_id's status just because the URL names it."""
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        victim_token = _run(_new_tenant_token(client.app.state.db, "ts-victim2@example.com"))
        victim_id = client.get(
            "/me", headers={"Authorization": f"Bearer {victim_token}"}
        ).json()["tenant_id"]
        attacker_token = _run(_new_tenant_token(client.app.state.db, "ts-attacker@example.com"))

        r = client.get(
            f"/tunnel/status/{victim_id}",
            headers={"Authorization": f"Bearer {attacker_token}"},
        )
        assert r.status_code == 401


def test_tunnel_status_allows_owning_tenant(monkeypatch, tmp_path):
    """The owning tenant's own credential must still succeed."""
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        raw_token = _run(_new_tenant_token(client.app.state.db, "ts-owner@example.com"))
        hdr = {"Authorization": f"Bearer {raw_token}"}
        tenant_id = client.get("/me", headers=hdr).json()["tenant_id"]

        r = client.get(f"/tunnel/status/{tenant_id}", headers=hdr)
        assert r.status_code == 200
        body = r.json()
        assert body["tenant_id"] == tenant_id
        assert body["active"] is False


def test_tunnel_status_self_hosted_mode_unaffected(client):
    """Self-hosted (the plain, non-hosted ``client`` fixture) has no tenant
    concept and must stay exactly as before: no auth required."""
    r = client.get("/tunnel/status/no-such-tenant")
    assert r.status_code == 200
    assert r.json()["tenant_id"] == "no-such-tenant"


def test_tunnel_status_direct_call_bypasses_auth_gate(monkeypatch):
    """A direct, non-HTTP coroutine call (request=None) — the existing
    plain-coroutine call sites in test_slot_reprobe.py/test_tunnel_bridge.py/
    test_w5_9665538a_meridian_docs_slot.py — must keep working even with
    hosted mode monkeypatched True: the auth gate only applies to real HTTP
    calls (Request is not None), never to internal callers with none."""
    monkeypatch.setattr(tn, "_hosted_mode", lambda: True)

    async def fail_if_called(*a, **k):
        raise AssertionError("_get_tenant_from_request must not be called when request is None")

    monkeypatch.setattr(tn, "_get_tenant_from_request", fail_if_called)
    result = asyncio.run(tn.tunnel_status("direct-call-tid"))
    assert result["tenant_id"] == "direct-call-tid"


# ---------------------------------------------------------------------------
# 2. WebSocket /ws/{project_id}
# ---------------------------------------------------------------------------


class _FakeWS:
    """Minimal Starlette-WebSocket-shaped stand-in for exercising
    ``ws_project``'s auth gate directly, without a live network handshake.

    A real Starlette ``WebSocket`` is an ``HTTPConnection`` — the same base
    class ``Request`` derives from — so it already has ``.cookies``,
    ``.headers``, ``.state``, and ``.app``; this fake reproduces just enough
    of that surface for ``_get_tenant_from_request`` (called duck-typed on a
    WebSocket, see ``ws_project``'s own docstring) to work unmodified.
    """

    def __init__(self, headers=None, db_sentinel=None, broadcaster=None):
        self.headers = headers or {}
        self.cookies = {}
        self.state = types.SimpleNamespace()
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(db=db_sentinel, ws_broadcaster=broadcaster)
        )
        self.accepted = False
        self.closed_with = None

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        self.closed_with = (code, reason)


class _StubBroadcaster:
    def __init__(self):
        self.served_with = None

    async def serve(self, ws, project_id):
        self.served_with = project_id


_TENANT_A = "ws-tenant-a"
_TENANT_B = "ws-tenant-b"
_OWNED_PROJECT = "ws-project-owned-by-a"


def _patch_ws_project_auth(monkeypatch):
    """Stub the three collaborators ``ws_project`` calls in hosted mode so
    its tenant-ownership check can be exercised without a real hosted DB —
    mirrors test_tunnel_routes.py's ``_patch_proxy_auth``/``_patch_ws_auth``
    for the 5de3d422 proxy-route fix."""
    monkeypatch.setattr(srv, "_hosted_mode", lambda: True)

    tokens = {"tok-a": _TENANT_A, "tok-b": _TENANT_B}

    async def fake_get_tenant(ws, **kwargs):
        auth = ws.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return None
        tenant_id = tokens.get(auth[len("Bearer "):])
        return {"id": tenant_id} if tenant_id else None

    async def fake_open_tenant_db(ws, tenant_id):
        return tenant_id  # sentinel "db handle" — just the tenant id itself

    async def fake_get_project(tenant_db, project_id):
        if tenant_db == _TENANT_A and project_id == _OWNED_PROJECT:
            return {"id": project_id}
        return None

    monkeypatch.setattr(srv, "_get_tenant_from_request", fake_get_tenant)
    monkeypatch.setattr(srv, "_open_tenant_db_by_id", fake_open_tenant_db)
    monkeypatch.setattr(srv.db_module, "get_project", fake_get_project)


def test_ws_project_requires_auth_in_hosted_mode(monkeypatch):
    _patch_ws_project_auth(monkeypatch)
    broadcaster = _StubBroadcaster()
    ws = _FakeWS(broadcaster=broadcaster)

    asyncio.run(srv.ws_project(ws, _OWNED_PROJECT))

    assert ws.accepted is True
    assert ws.closed_with == (4401, "authentication required")
    assert broadcaster.served_with is None, (
        "an unauthenticated caller must never reach the broadcaster/leak events"
    )


def test_ws_project_rejects_non_owning_tenant(monkeypatch):
    """Tenant B is a genuinely authenticated caller — just not the owner of
    this project_id — and must still be rejected."""
    _patch_ws_project_auth(monkeypatch)
    broadcaster = _StubBroadcaster()
    ws = _FakeWS(headers={"authorization": "Bearer tok-b"}, broadcaster=broadcaster)

    asyncio.run(srv.ws_project(ws, _OWNED_PROJECT))

    assert ws.closed_with is not None and ws.closed_with[0] == 4401
    assert broadcaster.served_with is None, (
        "a non-owning tenant's valid credential must not reach the broadcaster "
        "just because it knows/guesses this project_id"
    )


def test_ws_project_allows_owning_tenant(monkeypatch):
    _patch_ws_project_auth(monkeypatch)
    broadcaster = _StubBroadcaster()
    ws = _FakeWS(headers={"authorization": "Bearer tok-a"}, broadcaster=broadcaster)

    asyncio.run(srv.ws_project(ws, _OWNED_PROJECT))

    assert ws.closed_with is None, "the owning tenant's own credential must not be rejected"
    assert broadcaster.served_with == _OWNED_PROJECT, (
        "the auth gate must not become a dead end for legitimate use"
    )


def test_ws_project_self_hosted_mode_unaffected(monkeypatch):
    """Self-hosted (single-user) mode has no tenant concept: the new auth
    gate must not even run there."""
    monkeypatch.setattr(srv, "_hosted_mode", lambda: False)

    async def fail_if_called(*a, **k):
        raise AssertionError("_get_tenant_from_request must not be called in self-hosted mode")

    monkeypatch.setattr(srv, "_get_tenant_from_request", fail_if_called)
    broadcaster = _StubBroadcaster()
    ws = _FakeWS(broadcaster=broadcaster)

    asyncio.run(srv.ws_project(ws, _OWNED_PROJECT))

    assert ws.accepted is True
    assert ws.closed_with is None
    assert broadcaster.served_with == _OWNED_PROJECT


# ---------------------------------------------------------------------------
# 2b. End-to-end: real hosted TestClient, real WebSocket handshake, real
# tenant/project DB resolution (no internals monkeypatched).
# ---------------------------------------------------------------------------


def test_ws_project_e2e_unauthenticated_and_cross_tenant_rejected(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        db = client.app.state.db
        owner, owner_token, project = _run(
            _new_admin_tenant_with_project(db, "ws-e2e-owner@example.com", "ws-e2e-owned")
        )
        other_token = _run(_new_tenant_token(db, "ws-e2e-other@example.com"))

        # No credential at all.
        with client.websocket_connect(f"/ws/{project['id']}") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
            assert exc.value.code == 4401

        # A different, genuinely-authenticated tenant's credential.
        with client.websocket_connect(
            f"/ws/{project['id']}",
            headers={"Authorization": f"Bearer {other_token}"},
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
            assert exc.value.code == 4401


def test_ws_project_e2e_owning_tenant_streams_real_event(monkeypatch, tmp_path):
    """Beyond 'not rejected': the owning tenant's authenticated connection
    must actually receive real task-log events end-to-end."""
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        db = client.app.state.db
        owner, owner_token, project = _run(
            _new_admin_tenant_with_project(db, "ws-e2e-owner2@example.com", "ws-e2e-owned2")
        )
        hdr = {"Authorization": f"Bearer {owner_token}"}

        async def _make_session():
            from meridian import db as db_module
            return await db_module.register_session(db, project["id"], "ws-e2e-session")

        sess = _run(_make_session())

        with client.websocket_connect(
            f"/ws/{project['id']}", headers=hdr,
        ) as ws:
            client.post(
                "/tasks",
                json={
                    "session_id": sess["id"],
                    "project_id": project["id"],
                    "description": "[ASK]: pick one",
                    "status": "pending-hitl",
                },
                headers=hdr,
            )
            msg = ws.receive_text()

        import json as json_module
        event = json_module.loads(msg)
        assert event["type"] == "task_created"
        assert event["task"]["description"] == "[ASK]: pick one"
