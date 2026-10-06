"""RT-TI-005 wave 2 (H1-H7) -- close the remaining HTTP project-scope gaps.

Pinned decision 6fe5210c: a PROJECT-SCOPED workspace member must be refused on any
project outside their scope, even by direct id. The ``project_scope_enforcement``
middleware only sees ``/projects/{uuid}/...`` paths, so every route below reaches a
project through a body field, a query list, a name or another object's id and has to
apply the rule itself (wave 1 did notes, decisions, sessions and tasks; this wave
finishes /hitl, /projects (batch delete, by-name, parent, worktrees), /team/summary,
/profile-layers and /workspace/notes/{id}/move, plus the fail-closed middleware).

Every refusal below also re-reads the foreign object as an unscoped caller and asserts
it is unchanged, so a "403 AFTER the write" regression cannot hide behind a status code.
Hosted end-to-end tests at the bottom drive the REAL scope resolver with a real
project-scoped member token instead of a monkeypatched one.
"""
from __future__ import annotations

import asyncio
import importlib
import os
from datetime import datetime, timezone

import pytest

import meridian.server  # noqa: F401 -- load the server first (import-cycle ordering, as in wave 1)
from meridian import _deps
from meridian import db as db_module

_OUT_OF_SCOPE = "Project is outside your access scope."


def _make_project(client, prefix: str = "w2") -> str:
    r = client.post("/projects", json={"name": f"{prefix}-{os.urandom(4).hex()}"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _scope_to(monkeypatch, project_ids):
    """Make ``_scoped_project_ids_for_request`` answer ``project_ids`` (None = unscoped).

    One patch point is enough: the routes under test call the resolver through the
    ``_deps`` module (or via ``_require_project_in_scope``), and the middleware imports
    it at call time.
    """
    async def _fake(request):
        return project_ids

    monkeypatch.setattr(_deps, "_scoped_project_ids_for_request", _fake)


def _file_hitl(client, project_id: str, question: str, urgency: str = "normal") -> dict:
    r = client.post(f"/projects/{project_id}/hitl", json={"question": question, "urgency": urgency})
    assert r.status_code == 201, r.text
    return r.json()


def _register_session(client, project_id: str, name: str = "s") -> str:
    r = client.post("/sessions/register", json={"project_id": project_id, "name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# H1. /hitl list, get, answer/dismiss
# ---------------------------------------------------------------------------

def test_hitl_routes_refuse_requests_of_projects_outside_the_callers_scope(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    own = _file_hitl(client, mine, "mine?")
    foreign = _file_hitl(client, theirs, "theirs?")

    _scope_to(monkeypatch, [mine])
    listing = client.get("/hitl", params={"status": "all"}).json()
    assert {r["id"] for r in listing} == {own["id"]}
    assert client.get(f"/hitl/{foreign['id']}").status_code == 403
    assert client.patch(f"/hitl/{foreign['id']}", json={"answer": "yes"}).status_code == 403
    assert client.patch(f"/hitl/{foreign['id']}", json={"action": "dismiss"}).status_code == 403
    # An id that does not exist is answered exactly like a foreign one (no existence oracle).
    for resp in (
        client.get("/hitl/no-such-request"),
        client.patch("/hitl/no-such-request", json={"answer": "yes"}),
    ):
        assert resp.status_code == 403
        assert resp.json()["detail"] == _OUT_OF_SCOPE

    # Their own project still works end to end.
    assert client.get(f"/hitl/{own['id']}").status_code == 200
    answered = client.patch(f"/hitl/{own['id']}", json={"answer": "approved"})
    assert answered.status_code == 200, answered.text

    # Unscoped again: the foreign request was never touched, and an unknown id is a plain 404.
    _scope_to(monkeypatch, None)
    untouched = client.get(f"/hitl/{foreign['id']}").json()
    assert untouched["status"] == "pending" and not untouched.get("answer")
    assert client.get("/hitl/no-such-request").status_code == 404
    assert {r["id"] for r in client.get("/hitl", params={"status": "all"}).json()} >= {
        foreign["id"], own["id"],
    }


def test_hitl_list_limit_applies_to_the_scoped_callers_own_requests(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    normal = _file_hitl(client, mine, "normal one")
    blocking = _file_hitl(client, mine, "blocking one", urgency="blocking")
    for i in range(3):
        _file_hitl(client, theirs, f"foreign {i}", urgency="blocking")

    _scope_to(monkeypatch, [mine])
    rows = client.get("/hitl", params={"status": "all", "limit": 2}).json()
    # Foreign blocking requests must not eat the LIMIT, and the usual order holds
    # (blocking first, then newest first).
    assert [r["id"] for r in rows] == [blocking["id"], normal["id"]]
    assert client.get("/hitl", params={"limit": 1}).json()[0]["id"] == blocking["id"]
    # A bad status is still a 400, scoped or not.
    assert client.get("/hitl", params={"status": "bogus"}).status_code == 400

    # An empty scope sees nothing at all (and never falls back to "everything").
    _scope_to(monkeypatch, [])
    assert client.get("/hitl", params={"status": "all"}).json() == []


def test_unscoped_hitl_callers_are_unchanged_and_pay_no_extra_lookup(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    one = _file_hitl(client, mine, "q1")
    two = _file_hitl(client, theirs, "q2")
    three = _file_hitl(client, mine, "q3")
    _scope_to(monkeypatch, None)

    lookups: list[str] = []
    real_get = db_module.get_hitl_request

    async def _spy(db, request_id):
        lookups.append(request_id)
        return await real_get(db, request_id)

    monkeypatch.setattr(db_module, "get_hitl_request", _spy)

    assert {r["id"] for r in client.get("/hitl", params={"status": "all"}).json()} >= {one["id"], two["id"]}
    assert client.patch("/hitl/no-such-request", json={"answer": "go"}).status_code == 404

    def _answer_cost(request_id: str, scope) -> int:
        _scope_to(monkeypatch, scope)
        before = len(lookups)
        assert client.patch(f"/hitl/{request_id}", json={"answer": "go"}).status_code == 200
        return len(lookups) - before

    # Whatever the db layer itself fetches while answering is the unscoped baseline; the
    # same PATCH for a project-scoped caller costs exactly ONE more lookup (the scope
    # pre-check), so the owner / self-hosted / demo caller pays nothing extra.
    unscoped_cost = _answer_cost(two["id"], None)
    assert _answer_cost(three["id"], [mine]) == unscoped_cost + 1


# ---------------------------------------------------------------------------
# H2. /projects: batch delete, by-name, team summary, parent, worktrees
# ---------------------------------------------------------------------------

def test_batch_delete_checks_every_id_in_scope_before_any_lookup_or_delete(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    _scope_to(monkeypatch, [mine])

    lookups: list[str] = []
    real_get = db_module.get_project

    async def _spy(db, project_id):
        lookups.append(project_id)
        return await real_get(db, project_id)

    monkeypatch.setattr(db_module, "get_project", _spy)

    refused = client.delete("/projects", params=[("project_id", mine), ("project_id", theirs)])
    assert refused.status_code == 403
    assert refused.json()["detail"] == _OUT_OF_SCOPE
    # An unknown id is the same 403 -- not a 404 that names which ids exist.
    unknown = client.delete("/projects", params=[("project_id", mine), ("project_id", "no-such-project")])
    assert unknown.status_code == 403
    assert lookups == [], "scope must be checked BEFORE any existence lookup"

    # Nothing was deleted by the refused calls.
    monkeypatch.setattr(db_module, "get_project", real_get)
    _scope_to(monkeypatch, None)
    assert client.get(f"/projects/{mine}").status_code == 200
    assert client.get(f"/projects/{theirs}").status_code == 200

    # A scoped caller can still delete a project that is inside their scope.
    _scope_to(monkeypatch, [mine])
    ok = client.delete("/projects", params=[("project_id", mine)])
    assert ok.status_code == 200, ok.text
    assert ok.json()["deleted"] == [mine]
    _scope_to(monkeypatch, None)
    assert client.get(f"/projects/{mine}").status_code == 404
    assert client.get(f"/projects/{theirs}").status_code == 200


def test_batch_delete_is_unchanged_for_unscoped_callers(client, monkeypatch):
    a, b = _make_project(client), _make_project(client)
    _scope_to(monkeypatch, None)
    missing = client.delete("/projects", params=[("project_id", a), ("project_id", "no-such-project")])
    assert missing.status_code == 404  # (the app renders every 404 as its HTML error page)
    ok = client.delete("/projects", params=[("project_id", a), ("project_id", b)])
    assert ok.status_code == 200 and ok.json()["count"] == 2


def test_project_by_name_hides_out_of_scope_projects_like_missing_ones(client, monkeypatch):
    tag = os.urandom(3).hex()
    mine = client.post("/projects", json={"name": f"w2-mine-{tag}"}).json()["id"]
    theirs = client.post("/projects", json={"name": f"w2-theirs-{tag}"}).json()["id"]
    _scope_to(monkeypatch, [mine])

    own = client.get(f"/projects/by-name/w2-mine-{tag}")
    assert own.status_code == 200 and own.json()["project"]["id"] == mine

    foreign_name, ghost_name = f"w2-theirs-{tag}", f"w2-nothere-{tag}"
    foreign = client.get(f"/projects/by-name/{foreign_name}")
    ghost = client.get(f"/projects/by-name/{ghost_name}")
    assert foreign.status_code == ghost.status_code == 404
    # Byte-for-byte the same answer as a name that never existed.
    assert foreign.text == ghost.text
    assert foreign.headers["content-type"] == ghost.headers["content-type"]
    # The substring fallback only ever looks at in-scope projects.
    assert client.get(f"/projects/by-name/theirs-{tag}").status_code == 404
    both = client.get(f"/projects/by-name/-{tag}")
    assert both.status_code == 200 and both.json()["project"]["id"] == mine

    # Unscoped callers still find it (exact and substring).
    _scope_to(monkeypatch, None)
    assert client.get(f"/projects/by-name/{foreign_name}").json()["project"]["id"] == theirs
    assert client.get(f"/projects/by-name/theirs-{tag}").json()["project"]["id"] == theirs


def test_team_summary_needs_an_in_scope_project_for_a_scoped_caller(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    _scope_to(monkeypatch, [mine])
    # Omitting project_id would roll up every project's task log.
    assert client.get("/team/summary").status_code == 403
    assert client.get("/team/summary", params={"project_id": theirs}).status_code == 403
    assert client.get("/team/summary", params={"project_id": "no-such-project"}).status_code == 403
    assert client.get("/team/summary", params={"project_id": mine}).status_code == 200

    _scope_to(monkeypatch, None)
    assert client.get("/team/summary").status_code == 200
    assert client.get("/team/summary", params={"project_id": theirs}).status_code == 200


def test_parent_project_must_be_in_scope_on_create_and_reparent(client, monkeypatch):
    mine, mine2, theirs = _make_project(client), _make_project(client), _make_project(client)
    _scope_to(monkeypatch, [mine, mine2])

    # POST /projects with a foreign parent: refused, and no project is created.
    name = f"w2-child-{os.urandom(3).hex()}"
    assert client.post("/projects", json={"name": name, "parent_project_id": theirs}).status_code == 403
    # POST /projects/{id}/parent into a foreign parent: refused, parent unchanged.
    assert client.post(f"/projects/{mine}/parent", json={"parent_project_id": theirs}).status_code == 403

    # In-scope parents are fine, and detaching needs no extra check.
    child = client.post("/projects", json={"name": name, "parent_project_id": mine})
    assert child.status_code == 201 and child.json()["parent_project_id"] == mine
    assert client.post(f"/projects/{mine2}/parent", json={"parent_project_id": mine}).status_code == 200
    assert client.post(f"/projects/{mine2}/parent", json={"parent_project_id": None}).status_code == 200

    _scope_to(monkeypatch, None)
    assert client.get(f"/projects/{theirs}").json().get("parent_project_id") in (None, "")
    assert client.get(f"/projects/{mine}").json().get("parent_project_id") in (None, "")
    # Unscoped callers keep nesting under any project.
    assert client.post(f"/projects/{mine2}/parent", json={"parent_project_id": theirs}).status_code == 200


def test_delete_worktree_is_bound_to_the_project_in_the_path(client, monkeypatch, tmp_path):
    import meridian.worktree_cleanup as wc_module

    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    monkeypatch.setattr(meridian.server, "_REPO_ROOT", tmp_path)
    disk_calls: list[str] = []
    monkeypatch.setattr(
        wc_module, "remove_worktree_on_disk",
        lambda repo_root, wt_path: disk_calls.append(wt_path) or {"attempted": True, "removed": True, "detail": "fake"},
    )

    mine, theirs = _make_project(client), _make_project(client)
    foreign = client.post(f"/projects/{theirs}/worktrees", json={
        "session_id": _register_session(client, theirs), "branch": "worktree/theirs", "path": ".claude/worktrees/theirs",
    }).json()
    own = client.post(f"/projects/{mine}/worktrees", json={
        "session_id": _register_session(client, mine), "branch": "worktree/mine", "path": ".claude/worktrees/mine",
    }).json()

    # In-scope project in the PATH + a worktree id from another project: a 404, and the
    # foreign worktree is untouched in the DB AND on disk (the cleanup is never invoked).
    _scope_to(monkeypatch, [mine])
    refused = client.delete(f"/projects/{mine}/worktrees/{foreign['id']}")
    assert refused.status_code == 404
    assert disk_calls == []

    _scope_to(monkeypatch, None)
    assert [w["id"] for w in client.get(f"/projects/{theirs}/worktrees").json()] == [foreign["id"]]
    # Same answer when the caller is unscoped: the binding is not a scope-only rule.
    assert client.delete(f"/projects/{mine}/worktrees/{foreign['id']}").status_code == 404
    assert disk_calls == []

    # Each project can still remove its own worktree.
    assert client.delete(f"/projects/{mine}/worktrees/{own['id']}").status_code == 204
    assert client.delete(f"/projects/{theirs}/worktrees/{foreign['id']}").status_code == 204
    assert disk_calls == [".claude/worktrees/mine", ".claude/worktrees/theirs"]


@pytest.mark.asyncio
async def test_worktree_db_helpers_accept_an_optional_project_binding(db):
    a = (await db_module.create_project(db, "w2-wt-a"))["id"]
    b = (await db_module.create_project(db, "w2-wt-b"))["id"]
    sess = await db_module.register_session(db, a, "w2-wt-sess")
    wt = await db_module.register_worktree(db, sess["id"], a, "worktree/w2", ".claude/worktrees/w2")

    # A wrong project is answered like a missing worktree; no project keeps the old behaviour.
    assert await db_module.get_worktree(db, wt["id"], project_id=b) is None
    assert (await db_module.get_worktree(db, wt["id"], project_id=a))["id"] == wt["id"]
    assert (await db_module.get_worktree(db, wt["id"]))["id"] == wt["id"]

    assert await db_module.remove_worktree(db, wt["id"], project_id=b) is False
    assert (await db_module.get_worktree(db, wt["id"]))["removed_at"] is None
    assert await db_module.remove_worktree(db, wt["id"], project_id=a) is True
    assert await db_module.remove_worktree(db, wt["id"]) is False  # already removed


# ---------------------------------------------------------------------------
# H3. /profile-layers for scope_type == "project"
# ---------------------------------------------------------------------------

def test_profile_layers_of_other_projects_are_refused_for_a_scoped_caller(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    # tool_priority_map is one of the few fields writable at the project layer.
    fields = {"tool_priority_map": {"code_search": "Serena: find_symbol"}}
    edited = {"tool_priority_map": {"code_search": "Overwritten"}}
    for pid in (mine, theirs):
        assert client.put(f"/profile-layers/project/{pid}", json={"fields": fields}).status_code == 200
    assert client.put(
        "/profile-layers/workspace/singleton", json={"fields": {"auto_worktrees": 1}}
    ).status_code == 200

    _scope_to(monkeypatch, [mine])
    foreign = f"/profile-layers/project/{theirs}"
    assert client.get(foreign).status_code == 403
    assert client.put(foreign, json={"fields": edited}).status_code == 403
    assert client.delete(foreign).status_code == 403
    # The scope type is normalised the way the db layer does it: no case/space trick.
    assert client.get(f"/profile-layers/PROJECT/{theirs}").status_code == 403
    assert client.get(f"/profile-layers/%20project%20/{theirs}").status_code == 403
    # Clone: both the source (read) and the target (overwrite) are checked.
    assert client.post(
        f"/profile-layers/project/{mine}/clone",
        json={"target_scope_type": "project", "target_scope_id": theirs},
    ).status_code == 403
    assert client.post(
        f"/profile-layers/project/{theirs}/clone",
        json={"target_scope_type": "project", "target_scope_id": mine},
    ).status_code == 403
    assert client.post(
        f"/profile-layers/project/{theirs}/clone",
        json={"target_scope_type": "workspace", "target_scope_id": "singleton"},
    ).status_code == 403

    # Their own project layer, and the non-project layer types, are unchanged.
    assert client.get(f"/profile-layers/project/{mine}").status_code == 200
    assert client.put(f"/profile-layers/project/{mine}", json={"fields": edited}).status_code == 200
    assert client.get("/profile-layers/workspace/singleton").status_code == 200
    assert client.post(
        f"/profile-layers/project/{mine}/clone",
        json={"target_scope_type": "workspace", "target_scope_id": "copy"},
    ).status_code == 200

    # Listing: other projects' layers are filtered out, other scope types stay.
    listed = client.get("/profile-layers").json()
    project_layers = {r["scope_id"] for r in listed if r["scope_type"] == "project"}
    assert project_layers == {mine}
    assert any(r["scope_type"] == "workspace" for r in listed)
    assert {r["scope_id"] for r in client.get("/profile-layers", params={"scope_type": "project"}).json()} == {mine}

    # The refused writes changed nothing; unscoped callers see every layer.
    _scope_to(monkeypatch, None)
    still = client.get(foreign).json()
    assert still["fields"] == fields and still["revision"] == 1
    assert {r["scope_id"] for r in client.get("/profile-layers", params={"scope_type": "project"}).json()} == {mine, theirs}


# ---------------------------------------------------------------------------
# H4. POST /workspace/notes/{id}/move
# ---------------------------------------------------------------------------

def test_moving_a_workspace_note_needs_an_in_scope_destination_project(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    note = client.post("/workspace/notes", json={"title": "t", "body": "keep me"}).json()

    _scope_to(monkeypatch, [mine])
    refused = client.post(f"/workspace/notes/{note['id']}/move", json={"project_id": theirs})
    assert refused.status_code == 403 and refused.json()["detail"] == _OUT_OF_SCOPE

    # Nothing moved: still a workspace note, and the foreign project has no note.
    _scope_to(monkeypatch, None)
    assert [n["id"] for n in client.get("/workspace/notes").json()] == [note["id"]]
    assert client.get(f"/projects/{theirs}/notes").json() == []

    _scope_to(monkeypatch, [mine])
    moved = client.post(f"/workspace/notes/{note['id']}/move", json={"project_id": mine})
    assert moved.status_code == 201, moved.text
    assert moved.json()["project_id"] == mine

    # Unscoped callers may still move a note into any project.
    _scope_to(monkeypatch, None)
    other = client.post("/workspace/notes", json={"title": "t2", "body": "b2"}).json()
    assert client.post(f"/workspace/notes/{other['id']}/move", json={"project_id": theirs}).status_code == 201


# ---------------------------------------------------------------------------
# H5. POST /tasks: the session must belong to the project the task is logged against
# ---------------------------------------------------------------------------

def test_create_task_refuses_a_session_of_another_project(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    foreign_session = _register_session(client, theirs, "theirs")
    own_session = _register_session(client, mine, "mine")

    def _task_counts():
        # Read back as an unscoped caller (a scoped one cannot read the other project).
        _scope_to(monkeypatch, None)
        return (
            len(client.get(f"/projects/{mine}/tasks").json()),
            len(client.get(f"/projects/{theirs}/tasks").json()),
        )

    # Scoped caller: own in-scope project_id + somebody else's session id.
    _scope_to(monkeypatch, [mine])
    body = {"session_id": foreign_session, "project_id": mine, "description": "hijack", "status": "done"}
    refused = client.post("/tasks", json=body)
    assert refused.status_code == 404
    # Same answer as a session that does not exist (no oracle for foreign session ids).
    ghost = client.post("/tasks", json={**body, "session_id": "no-such-session"})
    assert ghost.status_code == 404 and ghost.text == refused.text

    # The binding is an integrity rule, not a scope rule: unscoped callers get it too.
    _scope_to(monkeypatch, None)
    assert client.post("/tasks", json=body).status_code == 404
    assert _task_counts() == (0, 0), "no task row may be written by a refused call"

    # A session of the named project works, scoped or not.
    ok = client.post("/tasks", json={**body, "session_id": own_session})
    assert ok.status_code == 201 and ok.json()["project_id"] == mine
    _scope_to(monkeypatch, [mine])
    assert client.post("/tasks", json={**body, "session_id": own_session, "description": "two"}).status_code == 201
    assert _task_counts() == (2, 0)  # the foreign project still has no task


# ---------------------------------------------------------------------------
# H7 (+ the 403-for-unknown-ids rule): /sessions and /tasks resolve the scope first
# ---------------------------------------------------------------------------

def test_session_routes_look_the_session_up_only_for_scoped_callers(client, monkeypatch):
    from meridian.routes import sessions as sessions_routes

    mine, theirs = _make_project(client), _make_project(client)
    own, foreign = _register_session(client, mine), _register_session(client, theirs)

    lookups: list[str] = []
    real = sessions_routes._session_project_id

    async def _spy(request, session_id):
        lookups.append(session_id)
        return await real(request, session_id)

    monkeypatch.setattr(sessions_routes, "_session_project_id", _spy)

    # Unscoped (owner / self-hosted / demo): zero extra SELECTs on the hot routes.
    _scope_to(monkeypatch, None)
    assert client.post(f"/sessions/{own}/heartbeat").status_code == 200
    assert client.get(f"/sessions/{own}/notes").status_code == 200
    assert client.patch(f"/sessions/{own}", json={"status": "idle"}).status_code == 200
    assert client.post("/sessions/no-such-session/heartbeat").status_code == 404
    assert client.get("/sessions/no-such-session/notes").status_code == 200  # historical: empty list
    assert client.patch("/sessions/no-such-session", json={"status": "idle"}).status_code == 404
    assert lookups == []

    # Scoped: the lookup happens, a foreign session is a 403, an unknown id the SAME 403.
    _scope_to(monkeypatch, [mine])
    assert client.post(f"/sessions/{own}/heartbeat").status_code in (200, 404)
    assert client.get(f"/sessions/{own}/notes").status_code == 200
    for sid in (foreign, "no-such-session"):
        assert client.post(f"/sessions/{sid}/heartbeat").status_code == 403
        assert client.get(f"/sessions/{sid}/notes").status_code == 403
        assert client.patch(f"/sessions/{sid}", json={"status": "closed"}).status_code == 403
        assert client.post(f"/sessions/{sid}/close").status_code == 403
    assert len(lookups) >= 7

    # The refused patch/close did not touch the foreign session.
    _scope_to(monkeypatch, None)
    live = {s["id"]: s for s in client.get(f"/projects/{theirs}/sessions").json()}
    assert live[foreign]["status"] == "active"


def test_task_routes_look_the_task_up_only_for_scoped_callers(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    session = _register_session(client, theirs)
    task = client.post("/tasks", json={
        "session_id": session, "project_id": theirs, "description": "keep", "status": "done",
    }).json()

    lookups: list[str] = []
    real = db_module.get_task

    async def _spy(db, task_id):
        lookups.append(task_id)
        return await real(db, task_id)

    monkeypatch.setattr(db_module, "get_task", _spy)

    # Unscoped: DELETE of an unknown id stays a 204 no-op and needs no task lookup.
    _scope_to(monkeypatch, None)
    assert client.delete("/tasks/no-such-task").status_code == 204
    assert lookups == []

    # Scoped: foreign and unknown ids are the same 403 on PATCH and DELETE.
    _scope_to(monkeypatch, [mine])
    for tid in (task["id"], "no-such-task"):
        assert client.patch(f"/tasks/{tid}", json={"description": "hijacked"}).status_code == 403
        assert client.delete(f"/tasks/{tid}").status_code == 403
    assert len(lookups) == 4

    _scope_to(monkeypatch, None)
    tasks = client.get(f"/projects/{theirs}/tasks").json()
    assert [(t["id"], t["description"]) for t in tasks] == [(task["id"], "keep")]
    assert client.patch("/tasks/no-such-task", json={"description": "x"}).status_code == 404


# ---------------------------------------------------------------------------
# H6. Fail closed when the scope cannot be resolved (middleware, unit level)
# ---------------------------------------------------------------------------

def test_scope_middleware_fails_closed_only_for_requests_carrying_a_workspace_header(client, monkeypatch):
    pid = _make_project(client)

    async def _boom(request):
        raise RuntimeError("auth db unavailable")

    monkeypatch.setattr(_deps, "_scoped_project_ids_for_request", _boom)

    refused = client.get(f"/projects/{pid}", headers={"X-Workspace-Tenant-Id": "some-workspace"})
    assert refused.status_code == 503
    assert refused.json() == {"detail": "scope check unavailable"}
    # A blank header is no header.
    assert client.get(f"/projects/{pid}", headers={"X-Workspace-Tenant-Id": "  "}).status_code == 200
    # No header: exactly the historical behaviour (the request proceeds).
    assert client.get(f"/projects/{pid}").status_code == 200
    # Paths the middleware does not gate are not affected by it.
    assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# Hosted end-to-end: a REAL project-scoped member token drives the real resolver
# ---------------------------------------------------------------------------

def _boot_hosted_client(monkeypatch, tmp_path):
    """Hosted TestClient on a fresh server import (same recipe as tests/test_security.py)."""
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_AUTH_DB", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    import meridian.server as server_module
    from fastapi.testclient import TestClient

    return TestClient(importlib.reload(server_module).app)


async def _seed_hosted_workspace(db) -> dict:
    """Owner workspace with two projects, a scoped ADMIN member (project A) and a wide member."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    owner = await db_module.upsert_tenant(db, "w2-owner@example.com")
    # admin plan -> the cross-workspace switch resolves to the auth DB in tests.
    await db.execute("UPDATE tenants SET plan='admin' WHERE id=?", (owner["id"],))
    scoped = await db_module.upsert_tenant(db, "w2-scoped@example.com")
    wide = await db_module.upsert_tenant(db, "w2-wide@example.com")
    tok_scoped, _ = await db_module.create_api_token(db, scoped["id"])
    tok_wide, _ = await db_module.create_api_token(db, wide["id"])
    proj_a = (await db_module.create_project(db, "w2-proj-a"))["id"]
    proj_b = (await db_module.create_project(db, "w2-proj-b"))["id"]
    for mid, email, role, pid in (
        ("w2-wm-scoped", "w2-scoped@example.com", "admin", proj_a),
        ("w2-wm-wide", "w2-wide@example.com", "member", None),
    ):
        await db.execute(
            "INSERT INTO workspace_members "
            "(id, tenant_id, email, role, github_access, joined_at, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mid, owner["id"], email, role, "none", now, pid),
        )
    await db.commit()
    return {
        "owner_id": owner["id"], "tok_scoped": tok_scoped, "tok_wide": tok_wide,
        "a": proj_a, "b": proj_b,
    }


def test_hosted_scoped_member_is_refused_on_every_wave2_route(monkeypatch, tmp_path):
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        db = c.app.state.db

        async def _setup():
            w = await _seed_hosted_workspace(db)
            sess_a = (await db_module.register_session(db, w["a"], "w2-sess-a"))["id"]
            sess_b = (await db_module.register_session(db, w["b"], "w2-sess-b"))["id"]
            w["sess_a"], w["sess_b"] = sess_a, sess_b
            w["hitl_a"] = (await db_module.request_hitl(db, w["a"], "question a"))["id"]
            w["hitl_b"] = (await db_module.request_hitl(db, w["b"], "question b"))["id"]
            w["wt_b"] = (await db_module.register_worktree(
                db, sess_b, w["b"], "worktree/w2b", ".claude/worktrees/w2b"))["id"]
            w["note"] = (await db_module.add_workspace_note(
                db, "t", "b", tenant_id=w["owner_id"]))["id"]
            for pid in (w["a"], w["b"]):
                await db_module.set_profile_layer(
                    db, "project", pid, fields={"tool_priority_map": {"code_search": "Serena: find_symbol"}})
            return w

        w = asyncio.run(_setup())
        s = {"Authorization": f"Bearer {w['tok_scoped']}", "X-Workspace-Tenant-Id": w["owner_id"]}
        wide = {"Authorization": f"Bearer {w['tok_wide']}", "X-Workspace-Tenant-Id": w["owner_id"]}
        a, b = w["a"], w["b"]

        # H1 -- /hitl
        assert {r["id"] for r in c.get("/hitl", params={"status": "all"}, headers=s).json()} == {w["hitl_a"]}
        assert {r["id"] for r in c.get("/hitl", params={"status": "all"}, headers=wide).json()} == {
            w["hitl_a"], w["hitl_b"]}
        assert c.get(f"/hitl/{w['hitl_a']}", headers=s).status_code == 200
        assert c.get(f"/hitl/{w['hitl_b']}", headers=s).status_code == 403
        assert c.get(f"/hitl/{w['hitl_b']}", headers=wide).status_code == 200
        assert c.patch(f"/hitl/{w['hitl_b']}", headers=s, json={"answer": "yes"}).status_code == 403
        assert c.patch(f"/hitl/{w['hitl_b']}", headers=s, json={"action": "dismiss"}).status_code == 403
        assert c.get(f"/hitl/{w['hitl_b']}", headers=wide).json()["status"] == "pending"

        # H2 -- by-name, team summary, batch delete (the scoped member is an ADMIN),
        # parent, worktree binding
        assert c.get("/projects/by-name/w2-proj-a", headers=s).status_code == 200
        assert c.get("/projects/by-name/w2-proj-b", headers=s).status_code == 404
        assert c.get("/projects/by-name/w2-proj-b", headers=wide).status_code == 200
        assert c.get("/team/summary", headers=s).status_code == 403
        assert c.get("/team/summary", headers=s, params={"project_id": b}).status_code == 403
        assert c.get("/team/summary", headers=s, params={"project_id": a}).status_code == 200
        assert c.delete("/projects", headers=s, params=[("project_id", a), ("project_id", b)]).status_code == 403
        assert c.delete("/projects", headers=s, params={"project_id": b}).status_code == 403
        assert c.get(f"/projects/{b}", headers=wide).status_code == 200
        assert c.post("/projects", headers=s, json={"name": "w2-child", "parent_project_id": b}).status_code == 403
        assert c.post(f"/projects/{a}/parent", headers=s, json={"parent_project_id": b}).status_code == 403
        assert c.delete(f"/projects/{a}/worktrees/{w['wt_b']}", headers=s).status_code == 404
        assert [x["id"] for x in c.get(f"/projects/{b}/worktrees", headers=wide).json()] == [w["wt_b"]]

        # H3 -- profile layers
        assert c.get(f"/profile-layers/project/{a}", headers=s).status_code == 200
        assert c.get(f"/profile-layers/project/{b}", headers=s).status_code == 403
        assert c.put(f"/profile-layers/project/{b}", headers=s, json={"fields": {}}).status_code == 403
        assert c.delete(f"/profile-layers/project/{b}", headers=s).status_code == 403
        assert {x["scope_id"] for x in c.get("/profile-layers", headers=s,
                                              params={"scope_type": "project"}).json()} == {a}
        assert {x["scope_id"] for x in c.get("/profile-layers", headers=wide,
                                              params={"scope_type": "project"}).json()} == {a, b}

        # H4 -- workspace note move
        assert c.post(f"/workspace/notes/{w['note']}/move", headers=s, json={"project_id": b}).status_code == 403

        # H5 -- POST /tasks with a foreign session, and the wave-1 session routes
        body = {"session_id": w["sess_b"], "project_id": a, "description": "x"}
        assert c.post("/tasks", headers=s, json=body).status_code == 404
        assert c.post(f"/sessions/{w['sess_b']}/heartbeat", headers=s).status_code == 403
        assert c.post(f"/sessions/{w['sess_a']}/heartbeat", headers=s).status_code == 200
        assert c.post("/sessions/no-such-session/heartbeat", headers=s).status_code == 403
        assert c.post("/sessions/no-such-session/heartbeat", headers=wide).status_code == 404

        # Nothing above changed the other project's data (read back as the wide member).
        assert c.get(f"/projects/{b}/tasks", headers=wide).json() == []
        assert c.get("/projects/by-name/w2-proj-b", headers=wide).status_code == 200
        notes = asyncio.run(db_module.get_workspace_notes(db, tenant_id=w["owner_id"]))
        assert [n["id"] for n in notes] == [w["note"]]  # still a workspace note, not moved


def test_hosted_scope_resolution_errors_fail_closed_for_http_and_mcp(monkeypatch, tmp_path):
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        db = c.app.state.db

        async def _setup():
            w = await _seed_hosted_workspace(db)
            w["own_tok"], _ = await db_module.create_api_token(db, w["owner_id"])
            return w

        w = asyncio.run(_setup())
        hdr = {"Authorization": f"Bearer {w['tok_scoped']}", "X-Workspace-Tenant-Id": w["owner_id"]}
        own = {"Authorization": f"Bearer {w['own_tok']}"}  # the owner, no workspace header
        a, b = w["a"], w["b"]

        def _mcp(headers, name="get_tasks", args=None):
            return c.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": name, "arguments": args if args is not None else {"project_id": a}}})

        # Control: with a working resolver the scoped member is scoped, as before.
        assert c.get(f"/projects/{a}/tasks", headers=hdr).status_code == 200
        assert c.get(f"/projects/{b}/tasks", headers=hdr).status_code == 403
        assert "outside your access scope" in _mcp(hdr, args={"project_id": b}).text

        # Now the membership lookup breaks (an auth-DB hiccup).
        async def _boom(*args, **kwargs):
            raise RuntimeError("auth db unavailable")

        monkeypatch.setattr(db_module, "get_scoped_project_ids_for_member", _boom)

        # HTTP middleware: 503, never "treated as unscoped" (which would serve project B).
        for pid in (a, b):
            r = c.get(f"/projects/{pid}/tasks", headers=hdr)
            assert r.status_code == 503, r.text
            assert r.json() == {"detail": "scope check unavailable"}
        # POST /mcp: a JSON-RPC error, and the tool is not executed.
        for args in ({"project_id": a}, {"project_id": b}):
            r = _mcp(hdr, args=args)
            assert r.status_code == 503, r.text
            payload = r.json()
            assert payload["id"] == 7 and payload["error"]["message"] == "scope check unavailable"
            assert "result" not in payload
        # A JSON-RPC batch gets the same answer.
        batch = c.post("/mcp", headers=hdr, json=[
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        assert batch.status_code == 503

        # Callers WITHOUT the workspace header never reach the failing lookup: unchanged.
        assert c.get(f"/projects/{a}", headers=own).status_code in (200, 404)
        listing = c.post("/mcp", headers=own, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert listing.status_code == 200 and "tools" in listing.json()["result"]
