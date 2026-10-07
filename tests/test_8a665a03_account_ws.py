"""8a665a03 -- the page's own WebSocket (``/ws-account``) for project-LIST events.

Independent verification found a live hole in the first two passes: the dashboard opens one
socket per project TAB (``/ws/{project_id}``), so with no tab open -- the last one closed, a
brand-new account with no project yet -- it held no socket at all and a project created,
renamed, merged or deleted from another tab, an agent or the API never reached it until a
reload. ``/ws-account`` is the socket that exists with or without a tab.

What is pinned here:

* the route end to end on a real TestClient: with NO project socket open, creating and
  deleting a project reaches the account socket (self-hosted), and the frame names no project;
* hosted mode applies ``ws_project``'s auth gate (4bea8629): no credential is refused with
  ``code=4401`` and never reaches the broadcaster, a resolved tenant is served on ITS OWN
  database, and the dashboard's workspace switch (``?workspace=``) is honoured only for a
  workspace-wide member -- never for a stranger or a project-scoped member;
* hosted end to end with a real bearer token: an admin tenant's socket receives the event its
  own HTTP create publishes.

The isolation of the stream itself (a socket on one database never hears another database's
changes) is in tests/test_8a665a03_project_and_sweep_events.py next to the publishers.
"""
from __future__ import annotations

import asyncio
import json
import types

import pytest
from starlette.websockets import WebSocketDisconnect

import meridian.server as srv
from meridian import db as db_module


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Self-hosted, real handshake
# ---------------------------------------------------------------------------


def test_account_socket_hears_a_project_created_with_no_project_socket_open(client):
    """The verifier's repro: zero tabs (so zero /ws/{id} sockets) and a create from elsewhere."""
    with client.websocket_connect("/ws-account") as ws:
        r = client.post("/projects", json={"name": "account-ws-secret-name"})
        assert r.status_code == 201
        frame = ws.receive_text()
    assert json.loads(frame) == {"type": "projects_changed", "change": "created"}
    # A nudge to refetch GET /projects, which applies the caller's own scoping: no name, no id.
    assert "account-ws-secret-name" not in frame
    assert r.json()["id"] not in frame


def test_account_socket_hears_a_delete_and_a_rename(client):
    p = client.post("/projects", json={"name": "account-ws-delete-me"}).json()
    with client.websocket_connect("/ws-account") as ws:
        assert client.post(f"/projects/{p['id']}/rename", json={"name": "account-ws-renamed"}).status_code == 200
        renamed = json.loads(ws.receive_text())
        assert client.delete(f"/projects/{p['id']}").status_code == 204
        changes = [renamed["change"], json.loads(ws.receive_text())["change"]]
    assert changes == ["renamed", "deleted"]


def test_account_socket_never_carries_project_data(client):
    """Only account-level events travel on it: a task / sprint / note event for a project does
    not, even when that project's own socket would receive it."""
    p = client.post("/projects", json={"name": "account-ws-no-leak"}).json()
    with client.websocket_connect("/ws-account") as ws:
        client.post(f"/projects/{p['id']}/sprint-items", json={"title": "secret sprint title", "version": "v1"})
        client.post(f"/projects/{p['id']}/notes", json={"body": "secret note body", "title": "t"})
        # Provoke one account event after them: it must be the FIRST frame, so nothing
        # project-scoped was queued in between.
        client.post("/projects", json={"name": "account-ws-marker"})
        assert json.loads(ws.receive_text())["type"] == "projects_changed"


def test_two_sockets_each_hear_the_change_and_a_closed_one_is_dropped(client):
    with client.websocket_connect("/ws-account") as a, client.websocket_connect("/ws-account") as b:
        client.post("/projects", json={"name": "account-ws-fanout"})
        assert json.loads(a.receive_text())["change"] == "created"
        assert json.loads(b.receive_text())["change"] == "created"
    # Both closed: the next change must not raise on the dead sockets (and registers cleanly).
    assert client.post("/projects", json={"name": "account-ws-after-close"}).status_code == 201


# ---------------------------------------------------------------------------
# 2. The hosted auth gate, with the collaborators stubbed (mirrors test_4bea8629)
# ---------------------------------------------------------------------------


class _FakeWS:
    def __init__(self, headers=None, query=None, db_sentinel=None, broadcaster=None):
        self.headers = headers or {}
        self.cookies = {}
        self.query_params = query or {}
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
        self.served_db = None

    async def serve_account(self, ws, database):
        self.served_db = database


_TENANT_A = "acct-tenant-a"
_TENANT_B = "acct-tenant-b"        # owns a workspace tenant A was invited to
_TENANT_C = "acct-tenant-c"        # owns a workspace tenant A is NOT a member of
_TENANT_D = "acct-tenant-d"        # owns a workspace tenant A has a project-scoped seat in


def _patch_hosted(monkeypatch, memberships):
    monkeypatch.setattr(srv, "_hosted_mode", lambda: True)
    tokens = {"tok-a": {"id": _TENANT_A, "email": "a@example.com"}}

    async def fake_get_tenant(ws, **kwargs):
        auth = ws.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return None
        return tokens.get(auth[len("Bearer "):])

    async def fake_open_tenant_db(ws, tenant_id):
        return f"db-of-{tenant_id}"  # sentinel "database handle"

    async def fake_workspaces(auth_db, email):
        assert auth_db == "auth-db" and email == "a@example.com"
        return memberships

    monkeypatch.setattr(srv, "_get_tenant_from_request", fake_get_tenant)
    monkeypatch.setattr(srv, "_open_tenant_db_by_id", fake_open_tenant_db)
    monkeypatch.setattr(srv.db_module, "get_workspaces_for_email", fake_workspaces)


def _serve(headers=None, query=None):
    broadcaster = _StubBroadcaster()
    ws = _FakeWS(headers=headers, query=query, db_sentinel="auth-db", broadcaster=broadcaster)
    asyncio.run(srv.ws_account(ws))
    return ws, broadcaster


_MEMBERSHIPS = [
    {"tenant_id": _TENANT_B, "role": "editor", "project_id": None},
    {"tenant_id": _TENANT_D, "role": "viewer", "project_id": "only-this-project"},
]


def test_account_ws_requires_auth_in_hosted_mode(monkeypatch):
    _patch_hosted(monkeypatch, _MEMBERSHIPS)
    ws, broadcaster = _serve()
    assert ws.accepted is True
    assert ws.closed_with == (4401, "authentication required")
    assert broadcaster.served_db is None, "an unauthenticated caller must never reach the stream"


def test_account_ws_serves_the_callers_own_database(monkeypatch):
    _patch_hosted(monkeypatch, _MEMBERSHIPS)
    ws, broadcaster = _serve(headers={"authorization": "Bearer tok-a"})
    assert ws.closed_with is None
    assert broadcaster.served_db == f"db-of-{_TENANT_A}"


def test_account_ws_honours_the_workspace_switch_for_a_workspace_wide_member(monkeypatch):
    _patch_hosted(monkeypatch, _MEMBERSHIPS)
    ws, broadcaster = _serve(headers={"authorization": "Bearer tok-a"}, query={"workspace": _TENANT_B})
    assert ws.closed_with is None
    assert broadcaster.served_db == f"db-of-{_TENANT_B}"


def test_account_ws_refuses_a_workspace_the_caller_is_not_a_member_of(monkeypatch):
    _patch_hosted(monkeypatch, _MEMBERSHIPS)
    ws, broadcaster = _serve(headers={"authorization": "Bearer tok-a"}, query={"workspace": _TENANT_C})
    assert ws.closed_with is not None and ws.closed_with[0] == 4401
    assert broadcaster.served_db is None, "naming another tenant's id must not subscribe to its stream"


def test_account_ws_refuses_the_whole_workspace_stream_to_a_project_scoped_member(monkeypatch):
    """A project-scoped seat sees one project through GET /projects; the stream that says
    'some project was created / deleted in this workspace' is not theirs."""
    _patch_hosted(monkeypatch, _MEMBERSHIPS)
    ws, broadcaster = _serve(headers={"authorization": "Bearer tok-a"}, query={"workspace": _TENANT_D})
    assert ws.closed_with is not None and ws.closed_with[0] == 4401
    assert broadcaster.served_db is None


def test_account_ws_naming_your_own_tenant_as_workspace_needs_no_membership(monkeypatch):
    _patch_hosted(monkeypatch, [])
    ws, broadcaster = _serve(headers={"authorization": "Bearer tok-a"}, query={"workspace": _TENANT_A})
    assert ws.closed_with is None
    assert broadcaster.served_db == f"db-of-{_TENANT_A}"


def test_account_ws_closes_when_the_tenant_database_is_not_provisioned(monkeypatch):
    from fastapi import HTTPException

    _patch_hosted(monkeypatch, [])

    async def unprovisioned(ws, tenant_id):
        raise HTTPException(status_code=503, detail="tenant database not provisioned")

    monkeypatch.setattr(srv, "_open_tenant_db_by_id", unprovisioned)
    ws, broadcaster = _serve(headers={"authorization": "Bearer tok-a"})
    assert ws.closed_with == (4401, "invalid tenant")
    assert broadcaster.served_db is None


def test_account_ws_self_hosted_mode_has_no_tenant_gate(monkeypatch):
    monkeypatch.setattr(srv, "_hosted_mode", lambda: False)

    async def fail_if_called(*a, **k):
        raise AssertionError("_get_tenant_from_request must not be called in self-hosted mode")

    monkeypatch.setattr(srv, "_get_tenant_from_request", fail_if_called)
    broadcaster = _StubBroadcaster()
    ws = _FakeWS(db_sentinel="the-one-database", broadcaster=broadcaster)
    asyncio.run(srv.ws_account(ws))
    assert ws.closed_with is None
    assert broadcaster.served_db == "the-one-database"


# ---------------------------------------------------------------------------
# 3. Hosted, real handshake and real bearer token (admin tenant -> shared auth DB)
# ---------------------------------------------------------------------------


def _make_hosted_client(monkeypatch, tmp_path):
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("MERIDIAN_AUTH_DB", "")

    import importlib
    from fastapi.testclient import TestClient

    return TestClient(importlib.reload(srv).app)


async def _admin_token(database, email):
    tenant = await db_module.upsert_tenant(database, email)
    await db_module.update_tenant(database, tenant["id"], plan="admin")
    raw, _row = await db_module.create_api_token(database, tenant["id"], label="t")
    return raw


async def _plain_token(database, email):
    tenant = await db_module.upsert_tenant(database, email)
    raw, _row = await db_module.create_api_token(database, tenant["id"], label="t")
    return raw


def test_account_ws_e2e_hosted_unauthenticated_and_unprovisioned_tenants_are_refused(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        database = client.app.state.db
        no_db_token = _run(_plain_token(database, "acct-no-db@example.com"))

        with client.websocket_connect("/ws-account") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
            assert exc.value.code == 4401

        # Authenticated, but has no database of its own yet: nothing to stream.
        with client.websocket_connect(
            "/ws-account", headers={"Authorization": f"Bearer {no_db_token}"},
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
            assert exc.value.code == 4401


def test_account_ws_e2e_hosted_tenant_hears_its_own_project_list_change(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        token = _run(_admin_token(client.app.state.db, "acct-owner@example.com"))
        hdr = {"Authorization": f"Bearer {token}"}
        with client.websocket_connect("/ws-account", headers=hdr) as ws:
            r = client.post("/projects", json={"name": "acct-hosted-created"}, headers=hdr)
            assert r.status_code == 201
            assert json.loads(ws.receive_text()) == {"type": "projects_changed", "change": "created"}


# ---------------------------------------------------------------------------
# 4. Client wiring that a unit test cannot reach (init is an IIFE)
# ---------------------------------------------------------------------------


def _dashboard_ts() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "meridian" / "static" / "dashboard.ts").read_text(
        encoding="utf-8"
    ).replace("\r", "")


def test_init_opens_the_account_socket_before_the_empty_account_wizard():
    """The wizard branch RETURNS before any tab is restored: an account with no project is
    exactly the dashboard that must hear its first project being created, so the socket has
    to be opened above that return, and not for the demo (no signed-in tenant to serve)."""
    import re

    ts = _dashboard_ts()
    m = re.search(r"^\(async function init\(\) \{.*?^\}\)\(\);", ts, re.S | re.M)
    assert m, "init() not found in meridian/static/dashboard.ts"
    body = m.group(0)
    assert "if (!isDemoMode()) connectAccountWs();" in body
    assert body.index("connectAccountWs()") < body.index("ez-wizard"), (
        "connectAccountWs() must run before the empty-account wizard returns"
    )
    assert body.index("await loadProjects();") < body.index("connectAccountWs()"), (
        "the workspace the list was loaded for must be known before the socket names it"
    )


def test_the_workspace_switcher_repoints_the_account_socket():
    ts = _dashboard_ts()
    start = ts.index("sel.onchange = async () => {")
    handler = ts[start: start + 1500]
    assert handler.index("state.activeWorkspaceTenantId =") < handler.index("connectAccountWs();") < handler.index(
        "await loadProjects();"
    )


def test_account_ws_e2e_hosted_workspace_param_naming_a_stranger_is_refused(monkeypatch, tmp_path):
    """Real handshake: a valid tenant may not subscribe to someone else's project list by
    putting that tenant's id in ?workspace=."""
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        database = client.app.state.db
        token = _run(_admin_token(database, "acct-snoop@example.com"))
        victim = _run(db_module.upsert_tenant(database, "acct-victim@example.com"))
        with client.websocket_connect(
            f"/ws-account?workspace={victim['id']}", headers={"Authorization": f"Bearer {token}"},
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
            assert exc.value.code == 4401
