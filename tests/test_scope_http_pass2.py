"""RT-TI-005 wave 2, pass 2 -- fixes for what the independent verifier found in H1-H7.

* F-H1  POST /projects/{pid}/tasks/release (and the claim route next to it) accepted a task
        or session id of ANOTHER project: a scoped caller could pair its own project with
        a foreign task id plus the claimant's session id and reset that task and its
        sprint item.
* F-H2  session-type profile layers are keyed by a session id (one session = one project)
        and were never checked: GET/PUT/DELETE/clone, the GET /profile-layers listing and
        GET /projects/{pid}/effective-profile?session_id=.
* F-H3  GET /profile-layers resolves the scope only when it can contain a project or
        session layer; the shared 403 helper lives in _deps.py; routes/projects.py reaches
        the resolver through ``_deps`` like every other route (so one patch point covers all).

Scope simulation follows tests/test_scope_http_wave2.py: ``_deps._scoped_project_ids_for_request``
is patched, and every refusal re-reads the foreign object as an unscoped caller.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi import HTTPException

import meridian.server  # noqa: F401 -- load the server first (import-cycle ordering, as in wave 1)
from meridian import _deps
from meridian import db as db_module

_OUT_OF_SCOPE = "Project is outside your access scope."


def _make_project(client, prefix: str = "p2") -> str:
    r = client.post("/projects", json={"name": f"{prefix}-{os.urandom(4).hex()}"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _scope_to(monkeypatch, project_ids):
    async def _fake(request):
        return project_ids

    monkeypatch.setattr(_deps, "_scoped_project_ids_for_request", _fake)


def _register_session(client, project_id: str, name: str = "s") -> str:
    r = client.post("/sessions/register", json={"project_id": project_id, "name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _db_read(client, sql: str, params: tuple = ()) -> dict:
    db = client.app.state.db

    async def _go():
        async with db.execute(sql, params) as cur:
            return await cur.fetchone()

    row = asyncio.run(_go())
    assert row is not None
    return dict(row)


def _db_write(client, sql: str, params: tuple = ()) -> None:
    db = client.app.state.db

    async def _go():
        await db.execute(sql, params)
        await db.commit()

    asyncio.run(_go())


def _claimed_sprint_task(client, project_id: str, session_id: str, title: str = "work") -> tuple[str, str]:
    """A sprint item of ``project_id`` claimed by ``session_id``: returns (task_id, item_id)."""
    item = client.post(
        f"/projects/{project_id}/sprint-items", json={"version": "v1", "title": title}
    ).json()
    claim = client.post(
        f"/projects/{project_id}/tasks/claim",
        json={"task_id": item["id"], "session_id": session_id},
    )
    assert claim.status_code == 200 and claim.json()["claimed"] is True, claim.text
    return claim.json()["task_id"], item["id"]


def _task_and_item(client, task_id: str, item_id: str) -> tuple[dict, dict]:
    return (
        _db_read(client, "SELECT status, claimed_by, claimed_at FROM task_log WHERE id = ?", (task_id,)),
        _db_read(client, "SELECT status FROM sprint_items WHERE id = ?", (item_id,)),
    )


# ---------------------------------------------------------------------------
# F-H1. POST /projects/{pid}/tasks/release is bound to the path project
# ---------------------------------------------------------------------------

def test_release_cannot_reset_a_task_of_another_project(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    my_session, their_session = _register_session(client, mine), _register_session(client, theirs)
    foreign_task, foreign_item = _claimed_sprint_task(client, theirs, their_session, "their work")

    before_task, before_item = _task_and_item(client, foreign_task, foreign_item)
    assert before_task["status"] == "in_progress" and before_task["claimed_by"] == their_session
    assert before_item["status"] == "in_progress"

    # A project-scoped caller: its OWN project in the path + the foreign task id + the
    # claimant's session id. Answered like a task this session does not hold.
    _scope_to(monkeypatch, [mine])
    refused = client.post(
        f"/projects/{mine}/tasks/release",
        json={"task_id": foreign_task, "session_id": their_session},
    )
    # (the app renders every 404 as its HTML error page, so compare the bodies, not a detail)
    assert refused.status_code == 404
    ghost = client.post(
        f"/projects/{mine}/tasks/release",
        json={"task_id": "no-such-task", "session_id": their_session},
    )
    assert ghost.status_code == 404 and ghost.text == refused.text

    # The foreign task and its linked sprint item are exactly as they were.
    assert _task_and_item(client, foreign_task, foreign_item) == (before_task, before_item)

    # The binding is an integrity rule, not a scope rule: an unscoped caller naming the
    # wrong project gets the same refusal, and the task is still untouched.
    _scope_to(monkeypatch, None)
    assert client.post(
        f"/projects/{mine}/tasks/release",
        json={"task_id": foreign_task, "session_id": their_session},
    ).status_code == 404
    assert _task_and_item(client, foreign_task, foreign_item) == (before_task, before_item)

    # Each project can still release its own task, scoped or not.
    own_task, own_item = _claimed_sprint_task(client, mine, my_session, "my work")
    _scope_to(monkeypatch, [mine])
    mine_released = client.post(
        f"/projects/{mine}/tasks/release", json={"task_id": own_task, "session_id": my_session}
    )
    assert mine_released.status_code == 200 and mine_released.json()["released"] is True
    task_row, item_row = _task_and_item(client, own_task, own_item)
    assert (task_row["status"], task_row["claimed_by"], item_row["status"]) == ("pending", None, "pending")
    _scope_to(monkeypatch, None)
    theirs_released = client.post(
        f"/projects/{theirs}/tasks/release", json={"task_id": foreign_task, "session_id": their_session}
    )
    assert theirs_released.status_code == 200
    task_row, item_row = _task_and_item(client, foreign_task, foreign_item)
    assert (task_row["status"], task_row["claimed_by"], item_row["status"]) == ("pending", None, "pending")


@pytest.mark.asyncio
async def test_release_task_accepts_an_optional_project_binding(db):
    a = (await db_module.create_project(db, "p2-rel-a"))["id"]
    b = (await db_module.create_project(db, "p2-rel-b"))["id"]
    sess = await db_module.register_session(db, a, "p2-rel-sess")
    task = await db_module.log_task(db, sess["id"], a, "claimed work", "pending")
    assert await db_module.claim_task(db, task["id"], sess["id"]) is not None

    # Another project's id: nothing matches, nothing changes.
    assert await db_module.release_task(db, task["id"], sess["id"], project_id=b) is False
    held = await db_module.get_task(db, task["id"])
    assert held["status"] == "in_progress" and held["claimed_by"] == sess["id"]

    # Without a project the historical behaviour holds (stdio / direct callers).
    assert await db_module.release_task(db, task["id"], sess["id"]) is True
    # And with the right project it releases, then reports "not held" the second time.
    assert await db_module.claim_task(db, task["id"], sess["id"]) is not None
    assert await db_module.release_task(db, task["id"], sess["id"], project_id=a) is True
    assert await db_module.release_task(db, task["id"], sess["id"], project_id=a) is False


def test_claim_refuses_a_session_of_another_project(client, monkeypatch):
    """Sibling of the release hole: body.session_id is stored as claimed_by and joined
    back to the session's name / human_id by GET /projects/{pid}/tasks."""
    mine, theirs = _make_project(client), _make_project(client)
    my_session = _register_session(client, mine, "mine")
    their_session = _register_session(client, theirs, "their-secret-session-name")
    item = client.post(f"/projects/{mine}/sprint-items", json={"version": "v1", "title": "claim me"}).json()
    task, other_task = (
        client.post("/tasks", json={
            "session_id": my_session, "project_id": mine, "description": desc, "status": "pending",
        }).json()
        for desc in ("pending one", "pending two")
    )
    last_seen_before = _db_read(client, "SELECT last_seen FROM sessions WHERE id = ?", (their_session,))

    _scope_to(monkeypatch, [mine])
    for target in (item["id"], task["id"]):
        refused = client.post(
            f"/projects/{mine}/tasks/claim", json={"task_id": target, "session_id": their_session}
        )
        assert refused.status_code == 404

    # Nothing was claimed or written on the strength of the foreign session id.
    assert _db_read(client, "SELECT claimed_by FROM task_log WHERE id = ?", (task["id"],))["claimed_by"] is None
    assert _db_read(client, "SELECT status FROM sprint_items WHERE id = ?", (item["id"],))["status"] != "in_progress"
    assert _db_read(client, "SELECT COUNT(*) AS n FROM task_log WHERE session_id = ?", (their_session,))["n"] == 0
    assert _db_read(
        client, "SELECT last_seen FROM sessions WHERE id = ?", (their_session,)
    ) == last_seen_before
    listed = client.get(f"/projects/{mine}/tasks").json()
    assert all(not t.get("claimed_by_session_name") for t in listed)

    # Not a scope-only rule: an unscoped caller is refused the same way ...
    _scope_to(monkeypatch, None)
    assert client.post(
        f"/projects/{mine}/tasks/claim", json={"task_id": task["id"], "session_id": their_session}
    ).status_code == 404
    # ... while the project's own session, and an id that was never registered (not a
    # session of ANOTHER project), still claim exactly as before.
    ok = client.post(
        f"/projects/{mine}/tasks/claim", json={"task_id": task["id"], "session_id": my_session}
    )
    assert ok.status_code == 200 and ok.json()["claimed"] is True
    legacy = client.post(
        f"/projects/{mine}/tasks/claim", json={"task_id": other_task["id"], "session_id": "never-registered"}
    )
    assert legacy.status_code == 200 and legacy.json()["claimed"] is True


def test_live_task_feed_never_joins_a_session_of_another_project(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    my_session = _register_session(client, mine, "mine")
    their_session = _register_session(client, theirs, "their-secret-session-name")
    own = client.post("/tasks", json={
        "session_id": my_session, "project_id": mine, "description": "mine", "status": "done",
    }).json()
    # A legacy / unbound-writer row: a task of MY project that points at THEIR session.
    _db_write(
        client,
        "INSERT INTO task_log (id, session_id, project_id, description, status) VALUES (?, ?, ?, ?, ?)",
        ("p2-legacy-task", their_session, mine, "legacy row", "done"),
    )

    _scope_to(monkeypatch, [mine])
    foreign_feed = client.get(f"/projects/{mine}/sessions/{their_session}/tasks/live")
    assert foreign_feed.status_code == 200
    rows = foreign_feed.json()
    assert [r["id"] for r in rows] == ["p2-legacy-task"]
    assert rows[0]["session_name"] is None and rows[0]["human_id"] is None
    assert "their-secret-session-name" not in foreign_feed.text

    # The feed of the project's own session still carries its name.
    own_feed = client.get(f"/projects/{mine}/sessions/{my_session}/tasks/live").json()
    assert [r["id"] for r in own_feed] == [own["id"]]
    assert own_feed[0]["session_name"] == "mine"


# ---------------------------------------------------------------------------
# F-H2. session-type profile layers
# ---------------------------------------------------------------------------

_FIELDS = {"tool_priority_map": {"code_search": "Serena: find_symbol"}}
_EDITED = {"tool_priority_map": {"code_search": "Overwritten"}}


def _seed_session_layers(client, mine: str, theirs: str) -> dict:
    s = {
        "mine": _register_session(client, mine, "mine"),
        "theirs": _register_session(client, theirs, "theirs"),
    }
    for sid in s.values():
        assert client.put(f"/profile-layers/session/{sid}", json={"fields": _FIELDS}).status_code == 200
    return s


def test_session_profile_layers_of_other_projects_are_refused_for_a_scoped_caller(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    s = _seed_session_layers(client, mine, theirs)
    foreign = f"/profile-layers/session/{s['theirs']}"

    _scope_to(monkeypatch, [mine])
    assert client.get(foreign).status_code == 403
    assert client.put(foreign, json={"fields": _EDITED}).status_code == 403
    assert client.delete(foreign).status_code == 403
    # The scope type is normalised the way the db layer does it, and the id is stripped.
    assert client.get(f"/profile-layers/SESSION/{s['theirs']}").status_code == 403
    assert client.get(f"/profile-layers/%20session%20/{s['theirs']}").status_code == 403
    assert client.get(f"/profile-layers/session/%20{s['theirs']}%20").status_code == 403
    # An unknown session id is the SAME 403 (no existence oracle) ...
    for resp in (
        client.get("/profile-layers/session/no-such-session"),
        client.put("/profile-layers/session/no-such-session", json={"fields": _EDITED}),
    ):
        assert resp.status_code == 403 and resp.json()["detail"] == _OUT_OF_SCOPE
    # Clone: both ends are checked, for session and mixed project/session layers.
    for src, dst in (
        (("session", s["mine"]), ("session", s["theirs"])),
        (("session", s["theirs"]), ("session", s["mine"])),
        (("session", s["theirs"]), ("workspace", "singleton")),
        (("session", s["mine"]), ("project", theirs)),
        (("project", mine), ("session", s["theirs"])),
    ):
        refused = client.post(
            f"/profile-layers/{src[0]}/{src[1]}/clone",
            json={"target_scope_type": dst[0], "target_scope_id": dst[1]},
        )
        assert refused.status_code == 403, (src, dst, refused.text)

    # Their own session layer, and the non-project layer types, are unchanged.
    assert client.get(f"/profile-layers/session/{s['mine']}").status_code == 200
    assert client.put(f"/profile-layers/session/{s['mine']}", json={"fields": _EDITED}).status_code == 200
    assert client.get("/profile-layers/workspace/singleton").status_code == 200
    assert client.post(
        f"/profile-layers/session/{s['mine']}/clone",
        json={"target_scope_type": "workspace", "target_scope_id": "copy-of-mine"},
    ).status_code == 200

    # The refused calls changed nothing: the foreign layer is intact and still revision 1.
    _scope_to(monkeypatch, None)
    still = client.get(foreign).json()
    assert still["fields"] == _FIELDS and still["revision"] == 1
    assert client.get("/profile-layers/session/no-such-session").json()["revision"] == 0
    # Unscoped callers keep full access to every session layer.
    assert client.put(foreign, json={"fields": _EDITED}).status_code == 200
    assert client.delete(foreign).status_code == 200


def test_profile_layer_listing_filters_session_layers_of_other_projects(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    s = _seed_session_layers(client, mine, theirs)
    assert client.put("/profile-layers/workspace/singleton", json={"fields": {"auto_worktrees": 1}}).status_code == 200
    # A layer that belongs to no session at all is not in anyone's scope.
    assert client.put("/profile-layers/session/ghost-session", json={"fields": _FIELDS}).status_code == 200

    _scope_to(monkeypatch, [mine])
    listed = client.get("/profile-layers").json()
    assert {r["scope_id"] for r in listed if r["scope_type"] == "session"} == {s["mine"]}
    assert any(r["scope_type"] == "workspace" for r in listed)
    assert {r["scope_id"] for r in client.get("/profile-layers", params={"scope_type": "session"}).json()} == {s["mine"]}
    # An empty scope sees no session layer at all.
    _scope_to(monkeypatch, [])
    assert [r for r in client.get("/profile-layers").json() if r["scope_type"] == "session"] == []

    _scope_to(monkeypatch, None)
    everything = {r["scope_id"] for r in client.get("/profile-layers", params={"scope_type": "session"}).json()}
    assert everything == {s["mine"], s["theirs"], "ghost-session"}


def test_effective_profile_refuses_a_session_of_another_project_for_a_scoped_caller(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    s = _seed_session_layers(client, mine, theirs)
    base = f"/projects/{mine}/effective-profile"

    _scope_to(monkeypatch, [mine])
    # In-scope project + a session of ANOTHER project: that session's layer must not be merged in.
    foreign = client.get(base, params={"session_id": s["theirs"]})
    assert foreign.status_code == 403 and foreign.json()["detail"] == _OUT_OF_SCOPE
    # An unknown session is the same 403; the project's own session, and no session, are fine.
    assert client.get(base, params={"session_id": "no-such-session"}).status_code == 403
    own = client.get(base, params={"session_id": s["mine"]})
    assert own.status_code == 200 and own.json()["session_id"] == s["mine"]
    assert client.get(base).status_code == 200

    # A scoped caller that owns BOTH projects still may not mix them: the session has to
    # belong to the project in the path, not merely to some project in scope.
    _scope_to(monkeypatch, [mine, theirs])
    assert client.get(base, params={"session_id": s["theirs"]}).status_code == 403

    # Unscoped callers are unchanged (the db layer never checked the pairing).
    _scope_to(monkeypatch, None)
    assert client.get(base, params={"session_id": s["theirs"]}).status_code == 200


def test_session_layer_scope_lookups_are_paid_only_by_scoped_callers(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)
    s = _seed_session_layers(client, mine, theirs)
    lookups: list[str] = []
    real = _deps._session_project_id

    async def _spy(request, session_id):
        lookups.append(session_id)
        return await real(request, session_id)

    monkeypatch.setattr(_deps, "_session_project_id", _spy)

    _scope_to(monkeypatch, None)
    assert client.get(f"/profile-layers/session/{s['theirs']}").status_code == 200
    assert client.get(f"/projects/{mine}/effective-profile", params={"session_id": s["mine"]}).status_code == 200
    assert lookups == []

    _scope_to(monkeypatch, [mine])
    assert client.get(f"/profile-layers/session/{s['mine']}").status_code == 200
    assert client.get(f"/projects/{mine}/effective-profile", params={"session_id": s["mine"]}).status_code == 200
    assert lookups == [s["mine"], s["mine"]]


# ---------------------------------------------------------------------------
# F-H3. GET /profile-layers resolves the scope only when it is needed
# ---------------------------------------------------------------------------

def test_profile_layer_listing_resolves_the_scope_only_for_project_and_session_layers(client, monkeypatch):
    calls: list[int] = []

    async def _counting(request):
        calls.append(1)
        return None

    monkeypatch.setattr(_deps, "_scoped_project_ids_for_request", _counting)

    # Layer types that are not project-keyed never need the scope (one auth-DB query saved).
    for scope_type in ("workspace", "user", "hosted_default", "WORKSPACE"):
        assert client.get("/profile-layers", params={"scope_type": scope_type}).status_code == 200
    assert calls == []
    # An invalid scope type is still a 400 and never reaches the resolver.
    assert client.get("/profile-layers", params={"scope_type": "bogus"}).status_code == 400
    assert calls == []

    # Everything that can contain a project or session layer still resolves it, once.
    for scope_type in (None, "project", "session", " Project "):
        before = len(calls)
        params = {} if scope_type is None else {"scope_type": scope_type}
        assert client.get("/profile-layers", params=params).status_code == 200
        assert len(calls) == before + 1, scope_type


# ---------------------------------------------------------------------------
# F-H3. shared helpers live in _deps.py; routes/projects.py uses one import style
# ---------------------------------------------------------------------------

def test_deny_unless_in_scope_lives_in_deps_with_the_shared_403():
    # Unscoped (None): nothing to enforce, whatever the id.
    _deps._deny_unless_in_scope(None, "any-project")
    _deps._deny_unless_in_scope(None, None)
    # Scoped: in scope passes, foreign and "no such object" (None) are the same 403.
    _deps._deny_unless_in_scope(["a", "b"], "a")
    for project_id in ("c", None):
        with pytest.raises(HTTPException) as exc:
            _deps._deny_unless_in_scope(["a", "b"], project_id)
        assert exc.value.status_code == 403 and exc.value.detail == _OUT_OF_SCOPE
    with pytest.raises(HTTPException):
        _deps._deny_unless_in_scope([], "a")


def test_route_modules_share_the_deps_helpers_instead_of_importing_each_other():
    from meridian.routes import hitl, projects, sessions, settings, tasks

    assert sessions._deny_unless_in_scope is _deps._deny_unless_in_scope
    assert hitl._deny_unless_in_scope is _deps._deny_unless_in_scope
    assert tasks._deny_unless_in_scope is _deps._deny_unless_in_scope
    assert sessions._session_project_id is _deps._session_project_id
    # projects.py reaches the resolver ONLY through the _deps module (no by-name copy that a
    # single patch point would miss), and settings.py never reaches into a sibling router.
    assert not hasattr(projects, "_scoped_project_ids_for_request")
    assert not hasattr(settings, "_session_project_id")


def test_project_listing_follows_the_same_scope_patch_point_as_the_other_routes(client, monkeypatch):
    mine, theirs = _make_project(client), _make_project(client)

    _scope_to(monkeypatch, [mine])
    assert {p["id"] for p in client.get("/projects").json()} == {mine}

    _scope_to(monkeypatch, None)
    assert {mine, theirs} <= {p["id"] for p in client.get("/projects").json()}


# ---------------------------------------------------------------------------
# F-H4. docs/api-reference.md documents the new rules
# ---------------------------------------------------------------------------

def test_api_reference_documents_the_scope_rules_and_new_404s():
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "api-reference.md").read_text(encoding="utf-8")
    assert "## Access Scope (hosted workspaces)" in doc
    # The /team/summary rule for scoped callers.
    assert "`GET /team/summary` requires a `project_id` inside the scope" in doc
    # The three new 404s (plus the claim sibling), each as a binding for every caller.
    assert "`POST /tasks` -- the `session_id` must belong to `project_id`" in doc
    assert "`DELETE /projects/{project_id}/worktrees/{worktree_id}` -- a worktree of another project is a `404`" in doc
    assert "`POST /projects/{project_id}/tasks/release` -- a task of another project is a `404`" in doc
    assert "`POST /projects/{project_id}/tasks/claim` -- a `session_id` that belongs to another project" in doc
    # And the fail-closed 503.
    assert '503 {"detail": "scope check unavailable"}' in doc
