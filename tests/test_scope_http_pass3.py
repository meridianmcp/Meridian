"""RT-TI-005 wave 2, pass 3 -- fixes for what the independent verifier found after pass 2.

Every hole below was reproduced against a hosted setup with a project-scoped ADMIN member of
project A (browser session cookie + ``X-Workspace-Tenant-Id`` = the owner's tenant) reaching
project B of the same workspace through an id that is NOT in the ``/projects/{uuid}`` path:

* F-D1  POST /hooks/session-start and POST /hooks/stop: the body ``project_id`` (and the stop hook's
        ``session_id``) was used with no scope check, and project auto-routing listed every project.
* F-D2  GET /export/my-data: ``project_db=await _db(request)`` honours the workspace header, so a
        scoped member downloaded every project of the workspace. (A shadowed duplicate handler in
        server.py was removed.)
* F-D3  GET /settings/mcp-config (and the tunnel routes that call ``list_projects`` on the workspace
        DB: launch-matrix and filesystem-roots GET/POST/DELETE) listed / wrote every project.
* F-D4  POST /projects/{pid}/sprint-batch ``notes``: an entry's (or the batch default) session id was
        never bound to the path project, so notes were planted into a session of project B.
* F-D5  POST /projects/{pid}/handoff/corrections: ``source_handoff_id`` was looked up with no project
        binding and ``invalidate_handoff`` updated by id alone, so a handoff of project B could be
        invalidated and regenerated from project A.
* F-D6  cheap items: POST /projects/{pid}/worktrees and POST /projects/{pid}/hitl stored a foreign
        session id (the worktree list then joined its NAME back), and a >400-row session-layer
        listing mutation survivor.

Scope simulation follows tests/test_scope_http_wave2.py (``_deps._scoped_project_ids_for_request`` is
patched); the hosted end-to-end tests use the real resolver with real cookie sessions.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
from datetime import datetime, timezone

import pytest
from starlette.requests import Request

import meridian.server  # noqa: F401 -- load the server first (import-cycle ordering, as in wave 1)
from meridian import _deps
from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import server as server_module
from meridian.db import batch_management as bm

_OUT_OF_SCOPE = "Project is outside your access scope."


# ---------------------------------------------------------------------------
# helpers (self-hosted ``client`` + simulated scope)
# ---------------------------------------------------------------------------

def _make_project(client, prefix: str = "p3") -> str:
    r = client.post("/projects", json={"name": f"{prefix}-{os.urandom(4).hex()}"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _project_name(client, project_id: str) -> str:
    return client.get(f"/projects/{project_id}").json()["name"]


def _scope_to(monkeypatch, project_ids):
    async def _fake(request):
        return project_ids

    monkeypatch.setattr(_deps, "_scoped_project_ids_for_request", _fake)


def _register_session(client, project_id: str, name: str = "s") -> str:
    r = client.post("/sessions/register", json={"project_id": project_id, "name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _db_read_all(client, sql: str, params: tuple = ()) -> list[dict]:
    db = client.app.state.db

    async def _go():
        async with db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    return asyncio.run(_go())


def _count(client, sql: str, params: tuple = ()) -> int:
    return int(next(iter(_db_read_all(client, sql, params)[0].values())))


def _db_write(client, sql: str, params: tuple = ()) -> None:
    db = client.app.state.db

    async def _go():
        await db.execute(sql, params)
        await db.commit()

    asyncio.run(_go())


def _set_executor_config(client, project_id: str, cfg: dict) -> None:
    asyncio.run(db_module.set_executor_config(client.app.state.db, project_id, cfg))


# ---------------------------------------------------------------------------
# F-D1. /hooks/session-start and /hooks/stop hold a project-scoped caller to their scope
# ---------------------------------------------------------------------------

def test_session_start_hook_refuses_a_project_outside_the_scope(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    client.post(f"/projects/{theirs}/sprint-items", json={"version": "v1", "title": "B-SECRET-ITEM-TITLE"})
    their_name, my_name = _project_name(client, theirs), _project_name(client, mine)
    sessions_before = _count(client, "SELECT COUNT(*) FROM sessions WHERE project_id = ?", (theirs,))

    _scope_to(monkeypatch, [mine])
    refused = client.post("/hooks/session-start", json={"project_id": theirs, "session_name": "x"})
    assert refused.status_code == 403
    assert refused.json()["detail"] == _OUT_OF_SCOPE
    assert "B-SECRET-ITEM-TITLE" not in refused.text and their_name not in refused.text
    # An unknown id is answered exactly like a foreign one (existence is not revealed).
    unknown = client.post("/hooks/session-start", json={"project_id": "no-such-project", "session_name": "x"})
    assert unknown.status_code == 403 and unknown.json() == refused.json()
    # ... and the refused calls created no session in the foreign project.
    assert _count(client, "SELECT COUNT(*) FROM sessions WHERE project_id = ?", (theirs,)) == sessions_before

    # The caller's own project still works and never mentions the other one.
    own = client.post("/hooks/session-start", json={"project_id": mine, "session_name": "x"})
    assert own.status_code == 200, own.text
    context = own.json()["hookSpecificOutput"]["additionalContext"]
    assert my_name in context and their_name not in context and "B-SECRET" not in context

    # Owner / workspace-wide / self-hosted / demo (no scope): unchanged -- any project is served.
    _scope_to(monkeypatch, None)
    wide = client.post("/hooks/session-start", json={"project_id": theirs, "session_name": "x"})
    assert wide.status_code == 200, wide.text
    wide_context = wide.json()["hookSpecificOutput"]["additionalContext"]
    assert their_name in wide_context and "B-SECRET-ITEM-TITLE" in wide_context


def test_session_start_hook_auto_routing_only_considers_in_scope_projects(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    their_name, my_name = _project_name(client, theirs), _project_name(client, mine)
    _set_executor_config(client, theirs, {"repo_paths": [{"hostname": "h-a", "cwd": "C:/work/theirs"}]})
    payload = {"cwd": "C:/work/theirs", "hostname": "h-a", "session_name": "auto"}

    # Unscoped: the cwd matches THEIR project's registered path, so it routes there (unchanged).
    _scope_to(monkeypatch, None)
    routed = client.post("/hooks/session-start", json=payload)
    assert routed.status_code == 200, routed.text
    assert their_name in routed.json()["hookSpecificOutput"]["additionalContext"]

    # Scoped to MINE: the other project is not even a candidate -- the single in-scope project
    # is the unambiguous route and nothing of the other project is in the answer.
    _scope_to(monkeypatch, [mine])
    scoped = client.post("/hooks/session-start", json=payload)
    assert scoped.status_code == 200, scoped.text
    context = scoped.json()["hookSpecificOutput"]["additionalContext"]
    assert my_name in context and their_name not in context

    # Nothing in scope at all: answered like a workspace with no projects.
    _scope_to(monkeypatch, [])
    empty = client.post("/hooks/session-start", json=payload)
    assert empty.status_code == 400 and "no projects found" in empty.text


def test_session_start_hook_project_select_hitl_lists_only_in_scope_projects(client, monkeypatch):
    mine, mine2, theirs = _make_project(client), _make_project(client), _make_project(client)
    _scope_to(monkeypatch, [mine, mine2])
    r = client.post("/hooks/session-start", json={"cwd": "C:/nowhere", "hostname": "h-z", "session_name": "x"})
    assert r.status_code == 200, r.text
    assert r.json()["hookSpecificOutput"]["additionalContext"] == ""  # waits for the human to pick
    rows = _db_read_all(
        client, "SELECT project_id, payload FROM hitl_requests WHERE kind = 'hook_project_select'"
    )
    assert len(rows) == 1
    listed = {p["id"] for p in json.loads(rows[0]["payload"])["projects"]}
    assert listed == {mine, mine2}  # never the foreign project's id or name
    assert rows[0]["project_id"] in {mine, mine2}


def test_stop_hook_refuses_a_project_or_session_outside_the_scope(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    my_session, their_session = _register_session(client, mine), _register_session(client, theirs)
    handoffs_before = _count(client, "SELECT COUNT(*) FROM handoffs WHERE project_id = ?", (theirs,))

    _scope_to(monkeypatch, [mine])
    for body in (
        {"project_id": theirs, "session_id": their_session},   # a foreign project
        {"project_id": mine, "session_id": their_session},     # an in-scope project, foreign session
        {"project_id": mine, "session_id": "no-such-session"},  # unknown session: same answer
        {"project_id": "no-such-project"},                      # unknown project: same answer
    ):
        r = client.post("/hooks/stop", json=body)
        assert r.status_code == 403, (body, r.text)
        assert r.json()["detail"] == _OUT_OF_SCOPE
    # The refused calls wrote nothing into the foreign project.
    assert _count(client, "SELECT COUNT(*) FROM handoffs WHERE project_id = ?", (theirs,)) == handoffs_before

    own = client.post("/hooks/stop", json={"project_id": mine, "session_id": my_session})
    assert own.status_code == 200 and own.json()["ok"] is True, own.text

    # Unscoped: the same cross-project body is served exactly as before (no behaviour change).
    _scope_to(monkeypatch, None)
    for body in (
        {"project_id": theirs, "session_id": their_session},
        {"project_id": mine, "session_id": their_session},
    ):
        r = client.post("/hooks/stop", json=body)
        assert r.status_code == 200 and r.json()["ok"] is True, (body, r.text)


@pytest.mark.asyncio
async def test_hook_project_resolver_only_routes_to_allowed_projects(db):
    a = await db_module.create_project(db, "p3-hook-a")
    b = await db_module.create_project(db, "p3-hook-b")
    await db_module.set_executor_config(
        db, b["id"], {"repo_paths": [{"hostname": "h", "cwd": "C:/work/b"}]}
    )
    resolve = server_module._resolve_hook_project_id
    assert await resolve(db, "C:/work/b", "h") == b["id"]  # unscoped: cwd wins, unchanged
    assert await resolve(db, "C:/work/b", "h", allowed_project_ids=[a["id"], b["id"]]) == b["id"]
    # Scoped to A only: B is not a candidate; A is the single remaining project.
    assert await resolve(db, "C:/work/b", "h", allowed_project_ids=[a["id"]]) == a["id"]
    assert await resolve(db, "C:/work/b", "h", allowed_project_ids=[]) is None


def _bare_request(headers: dict[str, str]) -> Request:
    return Request({
        "type": "http", "method": "POST", "path": "/hooks/stop", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    })


@pytest.mark.asyncio
async def test_hook_scope_resolver_skips_bearer_callers_and_fails_closed_on_the_header(monkeypatch):
    calls: list[int] = []

    async def _scoped(request):
        calls.append(1)
        return ["only-project"]

    monkeypatch.setattr(_deps, "_scoped_project_ids_for_request", _scoped)
    hook_scope = server_module._hook_scoped_project_ids

    # A Bearer caller is routed to the token's OWN tenant DB: the scope of someone else's
    # workspace says nothing about it, so it is never resolved (and never applied).
    bearer = _bare_request({"Authorization": "Bearer sk_meridian_x", "X-Workspace-Tenant-Id": "ws"})
    assert await hook_scope(bearer) is None and calls == []
    assert await hook_scope(_bare_request({"X-Workspace-Tenant-Id": "ws"})) == ["only-project"]

    async def _boom(request):
        raise RuntimeError("auth db down")

    monkeypatch.setattr(_deps, "_scoped_project_ids_for_request", _boom)
    from fastapi import HTTPException

    # A caller that carries the workspace header and whose scope cannot be resolved is refused
    # (503), never treated as unscoped; without the header nobody can be scoped.
    with pytest.raises(HTTPException) as exc_info:
        await hook_scope(_bare_request({"X-Workspace-Tenant-Id": "ws"}))
    assert exc_info.value.status_code == 503
    assert await hook_scope(_bare_request({})) is None


# ---------------------------------------------------------------------------
# helpers (hosted end-to-end: real cookie sessions, real resolver)
# ---------------------------------------------------------------------------

def _boot_hosted_client(monkeypatch, tmp_path):
    """Hosted TestClient on a fresh server import (same recipe as tests/test_cov_route_export.py)."""
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    monkeypatch.setenv("MERIDIAN_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_AUTH_DB", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    from fastapi.testclient import TestClient

    srv = importlib.reload(server_module)
    _deps._reset_limiter_counts()  # the 3/minute cap on /export/my-data must not bleed across tests
    return TestClient(srv.app)


async def _seed_hosted_workspace(db, tag: str) -> dict:
    """Owner workspace: project A (older) and B (newest), a project-scoped ADMIN member of A and a
    workspace-wide ADMIN member. Returns the ids plus a session cookie for each identity."""
    from meridian.hosted import _make_session_cookie

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    owner = await db_module.upsert_tenant(db, f"{tag}-owner@example.com")
    # admin plan -> the cross-workspace switch resolves to the auth DB in tests.
    await db.execute("UPDATE tenants SET plan='admin' WHERE id=?", (owner["id"],))
    scoped = await db_module.upsert_tenant(db, f"{tag}-scoped@example.com")
    wide = await db_module.upsert_tenant(db, f"{tag}-wide@example.com")
    proj_a = (await db_module.create_project(db, f"{tag}-proj-a"))["id"]
    await db.execute("UPDATE projects SET created_at = '2020-01-01 00:00:00' WHERE id = ?", (proj_a,))
    proj_b = (await db_module.create_project(db, f"{tag}-proj-b"))["id"]
    for mid, email, pid in (
        (f"{tag}-wm-scoped", f"{tag}-scoped@example.com", proj_a),
        (f"{tag}-wm-wide", f"{tag}-wide@example.com", None),
    ):
        await db.execute(
            "INSERT INTO workspace_members "
            "(id, tenant_id, email, role, github_access, joined_at, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mid, owner["id"], email, "admin", "none", now, pid),
        )
    await db.commit()
    cookies = {}
    for key, tenant in (("scoped", scoped), ("wide", wide), ("owner", owner)):
        session = await db_module.create_user_session(db, tenant["id"], "2099-01-01 00:00:00")
        cookies[key] = _make_session_cookie(session["id"])
    return {"owner_id": owner["id"], "a": proj_a, "b": proj_b, "cookies": cookies,
            "tag": tag, "headers": {"X-Workspace-Tenant-Id": owner["id"]}}


def _as(client, w: dict, who: str) -> dict:
    """Switch the client's session cookie to ``who`` and return the workspace header for it."""
    client.cookies.clear()
    client.cookies.set("meridian_session", w["cookies"][who])
    return {} if who == "owner" else dict(w["headers"])


def test_hosted_hooks_hold_a_cookie_scoped_member_to_their_scope(monkeypatch, tmp_path):
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        db = c.app.state.db

        async def _setup():
            w = await _seed_hosted_workspace(db, "d1")
            await db_module.add_sprint_item(db, w["b"], "v1", "B-SECRET-ITEM-TITLE")
            w["sess_a"] = (await db_module.register_session(db, w["a"], "d1-sess-a"))["id"]
            w["sess_b"] = (await db_module.register_session(db, w["b"], "d1-sess-b"))["id"]
            return w

        w = asyncio.run(_setup())
        a, b = w["a"], w["b"]

        h = _as(c, w, "scoped")
        before = asyncio.run(db_module.get_sessions(db, b))
        refused = c.post("/hooks/session-start", headers=h, json={"project_id": b, "session_name": "x"})
        assert refused.status_code == 403 and "B-SECRET" not in refused.text, refused.text
        assert len(asyncio.run(db_module.get_sessions(db, b))) == len(before)  # no session created in B
        stop_b = c.post("/hooks/stop", headers=h, json={"project_id": b, "session_id": w["sess_b"]})
        assert stop_b.status_code == 403
        stop_mixed = c.post("/hooks/stop", headers=h, json={"project_id": a, "session_id": w["sess_b"]})
        assert stop_mixed.status_code == 403
        assert asyncio.run(db_module.get_handoffs(db, b)) == []  # nothing was written for B
        own = c.post("/hooks/session-start", headers=h, json={"project_id": a, "session_name": "x"})
        assert own.status_code == 200 and "B-SECRET" not in own.text
        assert c.post("/hooks/stop", headers=h, json={"project_id": a, "session_id": w["sess_a"]}).status_code == 200

        # A workspace-wide member (same header, no scope) is served project B exactly as before.
        h = _as(c, w, "wide")
        wide = c.post("/hooks/session-start", headers=h, json={"project_id": b, "session_name": "x"})
        assert wide.status_code == 200 and "B-SECRET-ITEM-TITLE" in wide.text


# ---------------------------------------------------------------------------
# F-D2. GET /export/my-data
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_export_tenant_data_can_be_restricted_to_projects(db):
    owner = await db_module.upsert_tenant(db, "p3-export-owner@example.com")
    a = (await db_module.create_project(db, "p3-export-a"))["id"]
    b = (await db_module.create_project(db, "p3-export-b"))["id"]
    await db_module.add_project_note(db, a, "a-note", "A-BODY")
    await db_module.add_project_note(db, b, "b-note", "B-SECRET-BODY")
    await db_module.add_workspace_note(db, "ws-note", "WS-BODY", tenant_id=owner["id"])

    everything = await db_module.export_tenant_data(db, owner["id"])  # the default: unchanged
    assert {p["id"] for p in everything["projects"]} == {a, b}
    assert [n["title"] for n in everything["workspace_notes"]] == ["ws-note"]

    only_a = await db_module.export_tenant_data(db, owner["id"], project_ids=[a])
    assert [p["id"] for p in only_a["projects"]] == [a]
    assert "B-SECRET-BODY" not in json.dumps(only_a, default=str)
    assert only_a["workspace_notes"] == [] and only_a["workspace_decisions"] == []
    assert only_a["tenant"]["id"] == owner["id"]  # the caller's own account rows stay

    nothing = await db_module.export_tenant_data(db, owner["id"], project_ids=[])
    assert nothing["projects"] == [] and nothing["workspace_notes"] == []


def test_the_export_route_has_one_handler_and_it_is_scope_aware():
    handlers = [
        r.endpoint for r in server_module.app.routes
        if getattr(r, "path", None) == "/export/my-data"
    ]
    assert [h.__module__ for h in handlers] == ["meridian.routes.export"]  # the shadowed copy is gone
    assert not hasattr(server_module, "export_my_data")


def test_hosted_export_gives_a_scoped_member_only_their_projects(monkeypatch, tmp_path):
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        db = c.app.state.db

        async def _setup():
            w = await _seed_hosted_workspace(db, "d2")
            await db_module.add_project_note(db, w["a"], "a-note", "A-BODY")
            await db_module.add_project_note(db, w["b"], "b-note", "B-SECRET-BODY")
            await db_module.add_workspace_note(db, "ws-note", "WS-SECRET", tenant_id=w["owner_id"])
            return w

        w = asyncio.run(_setup())

        _deps._reset_limiter_counts()
        h = _as(c, w, "scoped")
        scoped = c.get("/export/my-data", headers=h)
        assert scoped.status_code == 200, scoped.text
        exported = scoped.json()
        assert [p["id"] for p in exported["projects"]] == [w["a"]]
        assert "B-SECRET-BODY" not in scoped.text and w["b"] not in scoped.text
        assert exported["workspace_notes"] == [] and exported["workspace_decisions"] == []

        # A workspace-wide member and the owner (own workspace, no header) are unchanged.
        _deps._reset_limiter_counts()
        h = _as(c, w, "wide")
        wide = c.get("/export/my-data", headers=h).json()
        assert {p["id"] for p in wide["projects"]} == {w["a"], w["b"]}
        assert [n["title"] for n in wide["workspace_notes"]] == ["ws-note"]
        _deps._reset_limiter_counts()
        h = _as(c, w, "owner")
        owner = c.get("/export/my-data", headers=h).json()
        assert {p["id"] for p in owner["projects"]} == {w["a"], w["b"]}
        assert [n["title"] for n in owner["workspace_notes"]] == ["ws-note"]


# ---------------------------------------------------------------------------
# F-D3. GET /settings/mcp-config and the tunnel routes that list the workspace's projects
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_projects_in_scope_filters_only_for_scoped_callers(db, monkeypatch):
    a = (await db_module.create_project(db, "p3-scope-a"))["id"]
    b = (await db_module.create_project(db, "p3-scope-b"))["id"]
    seen = {"scope": None}

    async def _scoped(request):
        return seen["scope"]

    monkeypatch.setattr(_deps, "_scoped_project_ids_for_request", _scoped)
    assert {p["id"] for p in await _deps._projects_in_scope(object(), db)} == {a, b}  # None: unchanged
    seen["scope"] = [a]
    assert [p["id"] for p in await _deps._projects_in_scope(object(), db)] == [a]
    seen["scope"] = []
    assert await _deps._projects_in_scope(object(), db) == []

    async def _boom(request):
        raise RuntimeError("auth db down")

    monkeypatch.setattr(_deps, "_scoped_project_ids_for_request", _boom)
    with pytest.raises(RuntimeError):  # fails closed: never the unfiltered list
        await _deps._projects_in_scope(object(), db)


def test_hosted_mcp_config_and_tunnel_routes_only_touch_in_scope_projects(monkeypatch, tmp_path):
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        db = c.app.state.db

        async def _setup():
            w = await _seed_hosted_workspace(db, "d3")
            await db_module.set_executor_config(
                db, w["a"], {"filesystem_roots": ["/a-only", "/shared"], "repo_path": "/repos/a"})
            await db_module.set_executor_config(
                db, w["b"], {"filesystem_roots": ["/b-secret", "/shared"], "repo_path": "/repos/b-secret"})
            return w

        w = asyncio.run(_setup())
        a, b = w["a"], w["b"]

        async def _exec_roots(pid):
            cfg = (await db_module.get_project(db, pid))["executor_config"]
            cfg = json.loads(cfg) if isinstance(cfg, str) else (cfg or {})
            return sorted(cfg.get("filesystem_roots") or [])

        # --- GET /settings/mcp-config
        hs = _as(c, w, "scoped")
        cfg = c.get("/settings/mcp-config", headers=hs)
        assert cfg.status_code == 200, cfg.text
        assert [p["id"] for p in cfg.json()["projects"]] == [a]
        assert b not in cfg.text and "d3-proj-b" not in cfg.text
        hw = _as(c, w, "wide")
        assert {p["id"] for p in c.get("/settings/mcp-config", headers=hw).json()["projects"]} == {a, b}
        ho = _as(c, w, "owner")
        assert {p["id"] for p in c.get("/settings/mcp-config", headers=ho).json()["projects"]} == {a, b}

        # --- GET /tunnel/launch-matrix and GET /tunnel/filesystem-roots (built from every project's config)
        hs = _as(c, w, "scoped")
        matrix = c.get(f"/tunnel/launch-matrix/{w['owner_id']}", headers=hs)
        assert matrix.status_code == 200, matrix.text
        assert {r["project_id"] for r in matrix.json()["rows"]} == {a}
        assert "b-secret" not in matrix.text
        roots = c.get("/tunnel/filesystem-roots", headers=hs).json()
        assert roots["filesystem_roots"] == ["/a-only", "/shared"]
        assert roots["known_repo_paths"] == ["/repos/a"]
        assert "b-secret" not in json.dumps(roots)
        hw = _as(c, w, "wide")
        assert {r["project_id"] for r in c.get(
            f"/tunnel/launch-matrix/{w['owner_id']}", headers=hw).json()["rows"]} == {a, b}
        assert set(c.get("/tunnel/filesystem-roots", headers=hw).json()["filesystem_roots"]) == {
            "/a-only", "/b-secret", "/shared"}

        # --- POST /tunnel/filesystem-roots writes into a project's executor_config: B is the NEWEST
        # project (the helper's target), so a scoped member's root must still land in A, never B.
        hs = _as(c, w, "scoped")
        added = c.post("/tunnel/filesystem-roots", headers=hs, json={"path": "/new-root"})
        assert added.status_code == 200, added.text
        assert "/new-root" in asyncio.run(_exec_roots(a))
        assert asyncio.run(_exec_roots(b)) == ["/b-secret", "/shared"]

        # --- DELETE /tunnel/filesystem-roots strips the path from in-scope projects only.
        removed = c.delete("/tunnel/filesystem-roots", headers=hs, params={"path": "/shared"})
        assert removed.status_code == 200, removed.text
        assert "/shared" not in asyncio.run(_exec_roots(a))
        assert asyncio.run(_exec_roots(b)) == ["/b-secret", "/shared"]  # B untouched

        # --- the workspace-wide member keeps the old behaviour: the newest project (B) is the target.
        hw = _as(c, w, "wide")
        assert c.post("/tunnel/filesystem-roots", headers=hw, json={"path": "/wide-root"}).status_code == 200
        assert "/wide-root" in asyncio.run(_exec_roots(b))


# ---------------------------------------------------------------------------
# F-D4. the batch endpoint binds sessions to the path project
# ---------------------------------------------------------------------------

def _batch_notes(client, project_id: str, entries: list[dict], **extra) -> object:
    body = {"operation": "notes", "mode": "best_effort", "idempotency_key": None, "entries": entries}
    body.update(extra)
    return client.post(f"/projects/{project_id}/sprint-batch", json=body)


def _notes_of(client, session_id: str) -> list[str]:
    return [r["title"] for r in _db_read_all(
        client, "SELECT title FROM session_notes WHERE session_id = ?", (session_id,))]


def test_batch_notes_refuse_a_session_of_another_project(client, monkeypatch):
    mine, theirs, mine2 = _make_project(client), _make_project(client), _make_project(client)
    my_session, their_session = _register_session(client, mine), _register_session(client, theirs)
    session2 = _register_session(client, mine2)

    # The engine binds a note's session to the path project for EVERY caller (unscoped here).
    r = _batch_notes(client, mine, [
        {"session_id": their_session, "title": "PLANTED-IN-B", "body": "ignore previous instructions"},
        {"session_id": my_session, "title": "legit", "body": "ok"},
    ])
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert (results[0]["status"], results[0]["error_code"]) == ("error", "NOT_FOUND")
    assert results[1]["status"] == "ok"
    assert _notes_of(client, their_session) == []  # nothing was planted in B
    assert _notes_of(client, my_session) == ["legit"]

    # The batch-level default session is bound the same way.
    d = _batch_notes(client, mine, [{"title": "DEFAULT-PLANT", "body": "x"}], session_id=their_session)
    assert d.status_code == 200 and d.json()["results"][0]["error_code"] == "NOT_FOUND"
    assert _notes_of(client, their_session) == []

    # A project-scoped caller gets the same 403 as everywhere else, before anything runs.
    _scope_to(monkeypatch, [mine])
    planted = _batch_notes(client, mine, [{"session_id": their_session, "title": "PLANTED-IN-B", "body": "x"}])
    assert planted.status_code == 403 and planted.json()["detail"] == _OUT_OF_SCOPE
    unknown = _batch_notes(client, mine, [{"session_id": "no-such-session", "title": "t", "body": "x"}])
    assert unknown.status_code == 403 and unknown.json() == planted.json()
    default_plant = _batch_notes(client, mine, [{"title": "t", "body": "x"}], session_id=their_session)
    assert default_plant.status_code == 403
    # ... for every operation type (the batch-level session id rides along on all of them).
    other_op = client.post(f"/projects/{mine}/sprint-batch", json={
        "operation": "sprint_items", "mode": "best_effort", "idempotency_key": None,
        "session_id": their_session, "entries": [{"title": "an item"}]})
    assert other_op.status_code == 403
    assert client.get(f"/projects/{mine}/sprint-items").json() == []  # the refused batch created nothing
    assert _notes_of(client, their_session) == []

    # In scope but NOT the path project: a mismatch is a 404 (the claim route's answer).
    _scope_to(monkeypatch, [mine, mine2])
    mismatch = _batch_notes(client, mine, [{"session_id": session2, "title": "t", "body": "x"}])
    assert mismatch.status_code == 404
    assert _notes_of(client, session2) == []

    # The caller's own session still works, scoped or not.
    ok = _batch_notes(client, mine, [{"session_id": my_session, "title": "fine", "body": "ok"}])
    assert ok.status_code == 200 and ok.json()["results"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_batch_note_engine_binds_the_session_to_the_project(db):
    a = (await db_module.create_project(db, "p3-batch-a"))["id"]
    b = (await db_module.create_project(db, "p3-batch-b"))["id"]
    sess_a = (await db_module.register_session(db, a, "sa"))["id"]
    sess_b = (await db_module.register_session(db, b, "sb"))["id"]

    # all_or_nothing: the foreign entry rejects the whole batch before any write.
    rejected = await bm.execute_batch(
        db, project_id=a, entry_kind="sprint_note", mode="all_or_nothing",
        entries=[
            {"session_id": sess_a, "title": "mine", "body": "x"},
            {"session_id": sess_b, "title": "theirs", "body": "x"},
        ],
    )
    assert rejected.status == "rejected"
    assert rejected.results[1].error_code == bm.ERROR_NOT_FOUND
    assert await db_module.get_session_notes(db, sess_a) == []
    assert await db_module.get_session_notes(db, sess_b) == []

    # The matching project still writes.
    ok = await bm.execute_batch(
        db, project_id=a, entry_kind="sprint_note", mode="best_effort",
        entries=[{"session_id": sess_a, "title": "mine", "body": "x"}],
    )
    assert ok.status == "ok"
    # An id that names no session is not the binding's business (the foreign key refuses it, as
    # before): it must NOT be reported as a project mismatch.
    orphan = await bm.execute_batch(
        db, project_id=a, entry_kind="sprint_note", mode="best_effort",
        entries=[{"session_id": "attribution-only", "title": "orphan", "body": "x"}],
    )
    assert orphan.results[0].error_code != bm.ERROR_NOT_FOUND


# ---------------------------------------------------------------------------
# F-D5. handoff corrections are bound to the path project
# ---------------------------------------------------------------------------

async def _two_projects_with_handoffs(db):
    a = (await db_module.create_project(db, "p3-hand-a"))["id"]
    b = (await db_module.create_project(db, "p3-hand-b"))["id"]
    ha = await db_module.record_handoff(db, a, "full", "BODY-OF-A")
    hb = await db_module.record_handoff(db, b, "full", "SECRET-BODY-OF-B")
    return a, b, ha["id"], hb["id"]


@pytest.mark.asyncio
async def test_get_handoff_can_be_bound_to_a_project(db):
    a, b, ha, hb = await _two_projects_with_handoffs(db)
    assert (await db_module.get_handoff(db, hb))["project_id"] == b  # unbound: unchanged
    assert (await db_module.get_handoff(db, hb, project_id=b))["id"] == hb
    assert await db_module.get_handoff(db, hb, project_id=a) is None  # mismatch == not found
    assert await db_module.get_handoff(db, "no-such-handoff", project_id=a) is None


@pytest.mark.asyncio
async def test_a_correction_cannot_name_or_invalidate_a_handoff_of_another_project(db, tmp_path):
    a, b, ha, hb = await _two_projects_with_handoffs(db)

    with pytest.raises(handoff_module.HandoffCorrectionError) as foreign:
        await handoff_module.record_handoff_correction(
            db, a, source_handoff_id=hb, blocker_classification="evidence_invalid",
        )
    with pytest.raises(handoff_module.HandoffCorrectionError) as unknown:
        await handoff_module.record_handoff_correction(
            db, a, source_handoff_id="no-such-handoff", blocker_classification="evidence_invalid",
        )
    # a handoff of another project reads exactly like an unknown id (the message names the id given)
    assert str(foreign.value).replace(hb, "X") == str(unknown.value).replace("no-such-handoff", "X")
    assert await handoff_module.list_handoff_corrections(db, a) == []  # nothing was recorded
    assert not (await db_module.get_handoff(db, hb))["invalidated"]

    # invalidate_handoff: a project binding makes the UPDATE a no-op for a foreign handoff ...
    assert await handoff_module.invalidate_handoff(db, hb, reason="x", project_id=a) is None
    assert not (await db_module.get_handoff(db, hb))["invalidated"]
    # ... and still works for the owning project, and unbound (historical callers) as before.
    mine = await handoff_module.invalidate_handoff(db, ha, reason="mine", project_id=a)
    assert mine["invalidated"] and mine["invalidated_reason"] == "mine"
    unbound = await handoff_module.invalidate_handoff(db, hb, reason="unbound")
    assert unbound["invalidated"]


@pytest.mark.asyncio
async def test_regenerate_never_invalidates_a_handoff_of_another_project(db, tmp_path):
    """Defence in depth: even a correction row that already names a foreign source (recorded before
    the binding existed) cannot invalidate that source when it is regenerated."""
    a, b, ha, hb = await _two_projects_with_handoffs(db)
    corr = await handoff_module.record_handoff_correction(
        db, a, source_handoff_id=ha, blocker_classification="scope_stale",
    )
    await db.execute("UPDATE handoff_corrections SET source_handoff_id = ? WHERE id = ?", (hb, corr["id"]))
    await db.commit()
    await handoff_module.regenerate_handoff_correction(db, a, corr["id"], str(tmp_path), mode="full")
    assert not (await db_module.get_handoff(db, hb))["invalidated"]  # B's handoff is untouched


def test_handoff_corrections_route_refuses_a_foreign_source_handoff(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    foreign = asyncio.run(db_module.record_handoff(client.app.state.db, theirs, "full", "SECRET-BODY-OF-B"))
    own = asyncio.run(db_module.record_handoff(client.app.state.db, mine, "full", "BODY-OF-A"))

    for scope in ([mine], None):  # an integrity rule: scoped and unscoped callers alike
        _scope_to(monkeypatch, scope)
        r = client.post(f"/projects/{mine}/handoff/corrections", json={
            "source_handoff_id": foreign["id"], "blocker_classification": "evidence_invalid",
            "regenerate": True,
        })
        assert r.status_code == 422, r.text
        assert "source_body_hash" not in r.text  # no oracle on the foreign body
        row = _db_read_all(client, "SELECT invalidated FROM handoffs WHERE id = ?", (foreign["id"],))[0]
        assert not row["invalidated"]
        assert _count(client, "SELECT COUNT(*) FROM handoff_corrections WHERE project_id = ?", (mine,)) == 0

    # The project's own handoff can still be corrected (and, regenerated, invalidated).
    recorded = client.post(f"/projects/{mine}/handoff/corrections", json={
        "source_handoff_id": own["id"], "blocker_classification": "evidence_invalid"})
    assert recorded.status_code == 200 and recorded.json()["correction"]["source_handoff_id"] == own["id"]


# ---------------------------------------------------------------------------
# F-D6. worktree create / hitl create session bindings, the worktree list join, >400 session layers
# ---------------------------------------------------------------------------

def test_create_worktree_refuses_a_session_or_item_of_another_project(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    my_session, their_session = _register_session(client, mine), _register_session(client, theirs, "B-SECRET-SESSION-NAME")
    their_item = client.post(f"/projects/{theirs}/sprint-items", json={"version": "v1", "title": "B item"}).json()["id"]
    body = {"branch": "worktree/x", "path": ".claude/worktrees/x"}

    for scope in ([mine], None):  # an integrity rule: the same answer scoped or not
        _scope_to(monkeypatch, scope)
        by_session = client.post(f"/projects/{mine}/worktrees", json={**body, "session_id": their_session})
        assert by_session.status_code == 404, by_session.text
        by_item = client.post(
            f"/projects/{mine}/worktrees", json={**body, "session_id": my_session, "item_id": their_item})
        assert by_item.status_code == 404, by_item.text
    assert client.get(f"/projects/{mine}/worktrees").json() == []  # nothing was registered
    assert "B-SECRET-SESSION-NAME" not in client.get(f"/projects/{mine}/worktrees").text

    ok = client.post(f"/projects/{mine}/worktrees", json={**body, "session_id": my_session})
    assert ok.status_code == 201, ok.text


@pytest.mark.asyncio
async def test_worktree_list_joins_a_session_only_within_its_own_project(db):
    a = (await db_module.create_project(db, "p3-wt-a"))["id"]
    b = (await db_module.create_project(db, "p3-wt-b"))["id"]
    sess_a = (await db_module.register_session(db, a, "A-session"))["id"]
    sess_b = (await db_module.register_session(db, b, "B-SECRET-SESSION-NAME"))["id"]
    # a row that (wrongly) carries another project's session id, e.g. written before the binding
    await db_module.register_worktree(db, sess_b, a, "worktree/x", ".claude/worktrees/x")
    await db_module.register_worktree(db, sess_a, a, "worktree/y", ".claude/worktrees/y")
    rows = {r["branch"]: r for r in await db_module.list_active_worktrees(db, a)}
    assert rows["worktree/x"]["session_name"] is None  # the foreign session's NAME does not surface
    assert rows["worktree/y"]["session_name"] == "A-session"


def test_create_hitl_refuses_a_session_of_another_project(client):
    mine, theirs = _make_project(client), _make_project(client)
    their_session, my_session = _register_session(client, theirs), _register_session(client, mine)
    refused = client.post(f"/projects/{mine}/hitl", json={"question": "q?", "session_id": their_session})
    assert refused.status_code == 404
    assert _count(client, "SELECT COUNT(*) FROM hitl_requests WHERE project_id = ?", (mine,)) == 0
    assert client.post(f"/projects/{mine}/hitl", json={"question": "q?", "session_id": my_session}).status_code == 201
    # a request with no session at all keeps working as before
    assert client.post(f"/projects/{mine}/hitl", json={"question": "q2?"}).status_code == 201


def test_session_layer_listing_filters_beyond_the_first_lookup_chunk(client, monkeypatch):
    """Mutation survivor from the pass-2 review: the in-scope session lookup runs in chunks of 400
    ids, so a listing with more than 400 session layers must still return every in-scope one."""
    mine, theirs = _make_project(client), _make_project(client)
    foreign_session = _register_session(client, theirs)
    db = client.app.state.db

    async def _seed() -> list[str]:
        ids = []
        for i in range(430):
            sid = f"p3-bulk-{i:04d}"
            await db.execute(
                "INSERT INTO sessions (id, project_id, name, status) VALUES (?, ?, ?, 'active')",
                (sid, mine, f"bulk-{i}"),
            )
            ids.append(sid)
        await db.commit()
        for sid in ids + [foreign_session]:
            await db_module.set_profile_layer(
                db, "session", sid, fields={"tool_priority_map": {"code_search": "Serena: find_symbol"}})
        return ids

    mine_ids = asyncio.run(_seed())
    _scope_to(monkeypatch, [mine])
    listed = client.get("/profile-layers", params={"scope_type": "session"})
    assert listed.status_code == 200
    got = {x["scope_id"] for x in listed.json()}
    assert got == set(mine_ids)  # all 430 in-scope layers (two lookup chunks), none of the foreign one
    assert foreign_session not in got
