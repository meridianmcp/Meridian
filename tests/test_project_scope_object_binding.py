"""RT-TI-003 .. RT-TI-006 — a note, decision, session or task must belong to the
project the caller named, and a project-scoped caller must stay inside their scope
on routes and tools that reach a project through an object id.

Pinned decision 6fe5210c makes per-request enforcement airtight: a project-scoped
member is refused on any other project even by direct id. The
``project_scope_enforcement`` middleware only sees ``/projects/{uuid}/...`` paths
and the MCP gate only sees a ``project_id`` argument, so these tests pin the gaps
that were left: mutations by object id alone, ``/sessions`` and ``/tasks`` routes,
and the MCP discovery tools.
"""
from __future__ import annotations

import os

import pytest

import meridian.server  # noqa: F401 -- load the server before handler to avoid its import cycle
from meridian import db as db_module
from meridian import _deps
from meridian.mcp import handler as mcp_handler


async def _project(db, name: str) -> str:
    return (await db_module.create_project(db, name))["id"]


# ---------------------------------------------------------------------------
# 1. DB layer: the optional project binding
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_note_update_and_delete_are_bound_to_the_named_project(db):
    a = await _project(db, "bind-note-a")
    b = await _project(db, "bind-note-b")
    note = await db_module.add_project_note(db, a, "title", "body")

    # Naming the wrong project behaves exactly like a missing note.
    assert await db_module.update_project_note(db, note["id"], body="hijacked", project_id=b) is None
    assert await db_module.delete_project_note(db, note["id"], project_id=b) is False
    assert (await db_module.get_project_note(db, note["id"]))["body"] == "body"

    # The owning project, and a caller that names none, still work as before.
    assert (await db_module.update_project_note(db, note["id"], body="edited", project_id=a))["body"] == "edited"
    assert (await db_module.update_project_note(db, note["id"], body="again"))["body"] == "again"
    assert await db_module.delete_project_note(db, note["id"], project_id=a) is True


@pytest.mark.asyncio
async def test_decision_update_supersede_and_delete_are_bound_to_the_named_project(db):
    a = await _project(db, "bind-dec-a")
    b = await _project(db, "bind-dec-b")
    dec = await db_module.pin_decision(db, a, "title", "body")

    assert await db_module.update_pinned_decision(db, dec["id"], body="hijacked", project_id=b) is None
    with pytest.raises(ValueError, match="decision not found"):
        await db_module.supersede_pinned_decision(db, dec["id"], "t2", "b2", project_id=b)
    assert await db_module.delete_pinned_decision(db, dec["id"], project_id=b) is False
    survivor = await db_module.get_pinned_decision(db, dec["id"])
    assert survivor["body"] == "body" and survivor["status"] == "active"

    assert (await db_module.update_pinned_decision(db, dec["id"], body="edited", project_id=a))["body"] == "edited"
    successor = await db_module.supersede_pinned_decision(db, dec["id"], "t2", "b2", project_id=a)
    assert successor["project_id"] == a
    # (The superseded original is the deletable row: it points at its successor,
    # not the other way round.)
    assert await db_module.delete_pinned_decision(db, dec["id"], project_id=a) is True


# ---------------------------------------------------------------------------
# 2. HTTP routes: the URL's project must own the object
# ---------------------------------------------------------------------------

def _make_project(client) -> str:
    r = client.post("/projects", json={"name": f"scope-{os.urandom(4).hex()}"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_note_routes_refuse_a_note_owned_by_another_project(client):
    a, b = _make_project(client), _make_project(client)
    note = client.post(f"/projects/{a}/notes", json={"title": "t", "body": "keep me"}).json()

    assert client.patch(f"/projects/{b}/notes/{note['id']}", json={"body": "hijacked"}).status_code == 404
    assert client.delete(f"/projects/{b}/notes/{note['id']}").status_code == 404
    # Still intact and still editable through its own project.
    assert client.patch(f"/projects/{a}/notes/{note['id']}", json={"body": "edited"}).status_code == 200
    assert client.delete(f"/projects/{a}/notes/{note['id']}").status_code == 204


def test_decision_routes_refuse_a_decision_owned_by_another_project(client):
    a, b = _make_project(client), _make_project(client)
    dec = client.post(f"/projects/{a}/decisions-pinned", json={"title": "t", "body": "keep me"}).json()
    url_b = f"/projects/{b}/decisions-pinned/{dec['id']}"
    url_a = f"/projects/{a}/decisions-pinned/{dec['id']}"

    assert client.patch(url_b, json={"body": "hijacked"}).status_code == 404
    assert client.patch(url_b, json={"new_title": "t2", "new_body": "b2"}).status_code == 404
    assert client.delete(url_b).status_code == 404
    assert client.patch(url_a, json={"body": "edited"}).status_code == 200
    assert client.delete(url_a).status_code == 204


# ---------------------------------------------------------------------------
# 3. /sessions and /tasks routes for a project-scoped caller
# ---------------------------------------------------------------------------

def _scope_to(monkeypatch, project_ids):
    async def _fake(request):
        return project_ids

    monkeypatch.setattr(_deps, "_scoped_project_ids_for_request", _fake)


def test_session_and_task_routes_refuse_a_project_outside_the_callers_scope(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    # Created while unscoped (owner), then the caller becomes scoped to `mine` only.
    foreign_session = client.post("/sessions/register", json={"project_id": theirs, "name": "s"}).json()
    foreign_task = client.post(
        "/tasks",
        json={"session_id": foreign_session["id"], "project_id": theirs, "description": "keep", "status": "done"},
    ).json()
    own_session = client.post("/sessions/register", json={"project_id": mine, "name": "mine"}).json()

    _scope_to(monkeypatch, [mine])
    sid, tid = foreign_session["id"], foreign_task["id"]
    assert client.post("/sessions/register", json={"project_id": theirs, "name": "x"}).status_code == 403
    assert client.get(f"/sessions/{sid}/notes").status_code == 403
    assert client.patch(f"/sessions/{sid}", json={"status": "idle"}).status_code == 403
    assert client.post(f"/sessions/{sid}/heartbeat").status_code == 403
    assert client.post(f"/sessions/{sid}/close").status_code == 403
    assert client.post(
        "/tasks", json={"session_id": sid, "project_id": theirs, "description": "x", "status": "done"}
    ).status_code == 403
    assert client.patch(f"/tasks/{tid}", json={"description": "hijacked"}).status_code == 403
    assert client.delete(f"/tasks/{tid}").status_code == 403

    # Their own project still works.
    own = own_session["id"]
    assert client.get(f"/sessions/{own}/notes").status_code == 200
    assert client.patch(f"/sessions/{own}", json={"status": "idle"}).status_code == 200
    assert client.post(f"/sessions/{own}/heartbeat").status_code in (200, 404)  # 404 only if auto-idle closed it

    # Unscoped again: nothing was damaged by the refused calls.
    _scope_to(monkeypatch, None)
    assert client.patch(f"/tasks/{tid}", json={"description": "still here"}).status_code == 200


def test_unscoped_callers_are_unaffected_on_session_and_task_routes(client, monkeypatch):
    _scope_to(monkeypatch, None)
    pid = _make_project(client)
    session = client.post("/sessions/register", json={"project_id": pid, "name": "s"}).json()
    task = client.post(
        "/tasks", json={"session_id": session["id"], "project_id": pid, "description": "t", "status": "done"}
    ).json()
    assert client.patch(f"/sessions/{session['id']}", json={"status": "idle"}).status_code == 200
    assert client.patch(f"/tasks/{task['id']}", json={"description": "t2"}).status_code == 200
    assert client.delete(f"/tasks/{task['id']}").status_code == 204


# ---------------------------------------------------------------------------
# 4. MCP dispatch: object-id tools and discovery tools for a scoped caller
# ---------------------------------------------------------------------------

async def _dispatch(db, tmp_path, name, args, scoped=None):
    return await mcp_handler._dispatch_mcp_tool(
        name, args, db, str(tmp_path), tenant=None, scoped_project_ids=scoped,
    )


@pytest.mark.asyncio
async def test_mcp_object_id_tools_bind_to_the_named_project(db, tmp_path):
    a = await _project(db, "mcp-bind-a")
    b = await _project(db, "mcp-bind-b")
    note = await db_module.add_project_note(db, a, "t", "keep me")
    dec = await db_module.pin_decision(db, a, "t", "keep me")

    with pytest.raises(ValueError, match="note not found"):
        await _dispatch(db, tmp_path, "delete_note", {"note_id": note["id"], "project_id": b})
    # No project named: the historical idempotent answer for an unknown note is unchanged.
    assert (await _dispatch(db, tmp_path, "delete_note", {"note_id": "no-such-note"})) == {"deleted": False}
    with pytest.raises(ValueError, match="decision not found"):
        await _dispatch(db, tmp_path, "update_decision", {"decision_id": dec["id"], "project_id": b, "body": "x"})
    with pytest.raises(ValueError, match="decision not found"):
        await _dispatch(db, tmp_path, "archive_decision", {"decision_id": dec["id"], "project_id": b})
    assert await db_module.get_project_note(db, note["id"]) is not None
    assert (await db_module.get_pinned_decision(db, dec["id"]))["body"] == "keep me"

    # The owning project works, and so does a call that names no project (unscoped caller).
    assert (await _dispatch(db, tmp_path, "update_decision", {"decision_id": dec["id"], "project_id": a, "body": "ok"}))["body"] == "ok"
    assert (await _dispatch(db, tmp_path, "delete_note", {"note_id": note["id"]})) == {"deleted": True}


@pytest.mark.asyncio
async def test_mcp_object_id_tools_check_the_objects_project_for_a_scoped_caller(db, tmp_path):
    mine = await _project(db, "mcp-scope-mine")
    theirs = await _project(db, "mcp-scope-theirs")
    note = await db_module.add_project_note(db, theirs, "t", "keep me")
    dec = await db_module.pin_decision(db, theirs, "t", "keep me")
    scoped = [mine]

    # These tools carry no project_id, so the argument gate alone sees nothing to refuse.
    for name, args in (
        ("delete_note", {"note_id": note["id"]}),
        ("update_decision", {"decision_id": dec["id"], "body": "hijacked"}),
        ("archive_decision", {"decision_id": dec["id"]}),
    ):
        with pytest.raises(ValueError, match="outside your access scope"):
            await _dispatch(db, tmp_path, name, args, scoped=scoped)
    assert await db_module.get_project_note(db, note["id"]) is not None
    assert (await db_module.get_pinned_decision(db, dec["id"]))["body"] == "keep me"

    # Objects in the caller's own project remain reachable.
    own_note = await db_module.add_project_note(db, mine, "t", "mine")
    assert (await _dispatch(db, tmp_path, "delete_note", {"note_id": own_note["id"]}, scoped=scoped)) == {"deleted": True}


@pytest.mark.asyncio
async def test_mcp_discovery_tools_only_show_a_scoped_callers_projects(db, tmp_path):
    mine = await _project(db, "disc-mine")
    theirs = await _project(db, "disc-theirs")

    everyone = await _dispatch(db, tmp_path, "list_projects", {})
    assert {mine, theirs} <= {p["id"] for p in everyone}

    visible = await _dispatch(db, tmp_path, "list_projects", {}, scoped=[mine])
    assert [p["id"] for p in visible] == [mine]

    found = await _dispatch(db, tmp_path, "get_project_by_name", {"name": "disc-mine"}, scoped=[mine])
    assert found["id"] == mine
    with pytest.raises(ValueError, match="no project found matching 'disc-theirs'"):
        await _dispatch(db, tmp_path, "get_project_by_name", {"name": "disc-theirs"}, scoped=[mine])
    # Same answer as a project that never existed.
    with pytest.raises(ValueError, match="no project found matching 'disc-nope'"):
        await _dispatch(db, tmp_path, "get_project_by_name", {"name": "disc-nope"}, scoped=[mine])


@pytest.mark.asyncio
async def test_a_caller_supplied_scope_list_is_never_trusted(db, tmp_path):
    mine = await _project(db, "spoof-mine")
    other = await _project(db, "spoof-other")
    # An unscoped caller that sends its own `_scoped_project_ids` gets the normal, full listing:
    # the key is stripped by the dispatcher, not honoured.
    listing = await _dispatch(db, tmp_path, "list_projects", {"_scoped_project_ids": [mine]})
    assert {mine, other} <= {p["id"] for p in listing}
    # A scoped caller cannot widen its scope the same way.
    narrow = await _dispatch(db, tmp_path, "list_projects", {"_scoped_project_ids": [mine, other]}, scoped=[mine])
    assert [p["id"] for p in narrow] == [mine]


# ---------------------------------------------------------------------------
# 5. RT-TI-007: the public OpenAI diagnostics route must not leak tenant tunnel state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_openai_diagnostics_hides_the_live_tunnel_flag_from_other_callers(monkeypatch):
    import json

    from meridian.routes import tunnel as tunnel_mod

    class _Req:
        async def json(self):
            return {}

    async def _call(caller):
        async def _resolve(request):
            return caller

        monkeypatch.setattr(tunnel_mod, "_get_tenant_from_request", _resolve)
        response = await tunnel_mod.openai_tunnel_diagnostics("tenant-with-tunnel", _Req())
        return json.loads(response.body)["meridian_tunnel"]["active"]

    monkeypatch.setitem(tunnel_mod._tunnel_sockets, "tenant-with-tunnel", object())

    # Hosted: only the owning tenant sees the live flag; anyone else gets "unknown" (null).
    monkeypatch.setattr(tunnel_mod, "_hosted_mode", lambda: True)
    assert await _call({"id": "tenant-with-tunnel"}) is True
    assert await _call(None) is None
    assert await _call({"id": "some-other-tenant"}) is None

    # Self-hosted / local mode keeps reporting the real state to its single user.
    monkeypatch.setattr(tunnel_mod, "_hosted_mode", lambda: False)
    assert await _call(None) is True
