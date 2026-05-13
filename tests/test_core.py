"""Core tests for Meridian — db layer, HTTP endpoints, and handoff."""

from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


# ---------------------------------------------------------------------------
# db.py — direct
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_project(db):
    p = await db_module.create_project(db, "alpha")
    assert p["name"] == "alpha"
    assert p["id"]
    fetched = await db_module.get_project(db, p["id"])
    assert fetched is not None
    assert fetched["id"] == p["id"]


@pytest.mark.asyncio
async def test_get_project_by_name_and_list(db):
    await db_module.create_project(db, "alpha")
    await db_module.create_project(db, "beta")
    by_name = await db_module.get_project_by_name(db, "beta")
    assert by_name is not None
    assert by_name["name"] == "beta"
    all_projects = await db_module.list_projects(db)
    assert {p["name"] for p in all_projects} == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_set_goal_versions_increment(db):
    p = await db_module.create_project(db, "alpha")
    g1 = await db_module.set_goal(db, p["id"], "first")
    g2 = await db_module.set_goal(db, p["id"], "second")
    g3 = await db_module.set_goal(db, p["id"], {"goal": "third"})
    assert g1["version"] == 1
    assert g2["version"] == 2
    assert g3["version"] == 3
    assert g3["content"] == {"goal": "third"}


@pytest.mark.asyncio
async def test_get_goal_returns_none_when_unset(db):
    p = await db_module.create_project(db, "alpha")
    assert await db_module.get_goal(db, p["id"]) is None


@pytest.mark.asyncio
async def test_register_and_close_session(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess-1")
    assert s["status"] == "active"
    await db_module.close_session(db, s["id"])
    active = await db_module.get_sessions(db, p["id"], active_only=True)
    assert s["id"] not in {x["id"] for x in active}
    all_sessions = await db_module.get_sessions(db, p["id"], active_only=False)
    assert s["id"] in {x["id"] for x in all_sessions}


@pytest.mark.asyncio
async def test_log_task_and_get_tasks_newest_first(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess-1")
    t1 = await db_module.log_task(db, s["id"], p["id"], "first", "done")
    t2 = await db_module.log_task(db, s["id"], p["id"], "second", "done")
    t3 = await db_module.log_task(db, s["id"], p["id"], "third", "failed")
    tasks = await db_module.get_tasks(db, p["id"], limit=10)
    assert [t["id"] for t in tasks] == [t3["id"], t2["id"], t1["id"]]
    assert tasks[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_log_task_rejects_bad_status(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess-1")
    with pytest.raises(ValueError):
        await db_module.log_task(db, s["id"], p["id"], "x", "bogus")


@pytest.mark.asyncio
async def test_handoff_generates_clean_markdown(db, tmp_path):
    p = await db_module.create_project(db, "alpha")
    await db_module.set_goal(db, p["id"], "build a thing")
    s1 = await db_module.register_session(db, p["id"], "sess-1")
    s2 = await db_module.register_session(db, p["id"], "sess-2")
    await db_module.log_task(db, s1["id"], p["id"], "did A", "done")
    await db_module.log_task(db, s2["id"], p["id"], "did B", "done")
    path, content = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path)
    )
    assert "MERIDIAN_CONTEXT" in content
    assert "## Goal State (v1)" in content
    assert "build a thing" in content
    assert "## Active Sessions (2)" in content
    assert "sess-1" in content and "sess-2" in content
    assert "## Recent Task Log" in content
    assert "did A" in content and "did B" in content
    assert "## Resume Instructions" in content
    on_disk = tmp_path / "alpha_handoff.md"
    assert on_disk.exists()
    assert on_disk.read_text(encoding="utf-8") == content
    assert str(on_disk.resolve()) == path


# ---------------------------------------------------------------------------
# FastAPI endpoints
# ---------------------------------------------------------------------------


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_create_and_list_projects(client):
    r = client.post("/projects", json={"name": "alpha"})
    assert r.status_code == 201
    project = r.json()
    r = client.get("/projects")
    assert r.status_code == 200
    assert any(p["id"] == project["id"] for p in r.json())


def test_duplicate_project_returns_409(client):
    client.post("/projects", json={"name": "alpha"})
    r = client.post("/projects", json={"name": "alpha"})
    assert r.status_code == 409


def test_get_unknown_project_returns_404(client):
    r = client.get("/projects/does-not-exist")
    assert r.status_code == 404


def test_goal_round_trip_and_versioning(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    r = client.get(f"/projects/{project['id']}/goal")
    assert r.status_code == 404  # unset goal
    r = client.post(
        f"/projects/{project['id']}/goal", json={"content": "go"}
    )
    assert r.status_code == 200
    assert r.json()["version"] == 1
    r = client.post(
        f"/projects/{project['id']}/goal", json={"content": "go again"}
    )
    assert r.json()["version"] == 2
    r = client.get(f"/projects/{project['id']}/goal")
    assert r.json()["content"] == "go again"


def test_session_and_task_round_trip(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "s1"},
    ).json()
    r = client.post(
        "/tasks",
        json={
            "session_id": sess["id"],
            "project_id": project["id"],
            "description": "did a thing",
        },
    )
    assert r.status_code == 201
    r = client.get(f"/projects/{project['id']}/tasks")
    assert r.status_code == 200
    assert len(r.json()) == 1
    r = client.get(f"/projects/{project['id']}/sessions")
    assert any(s["id"] == sess["id"] for s in r.json())


def test_task_for_unknown_project_returns_404(client):
    r = client.post(
        "/tasks",
        json={
            "session_id": "x",
            "project_id": "nope",
            "description": "y",
        },
    )
    assert r.status_code == 404


def test_close_session(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "s1"},
    ).json()
    r = client.post(f"/sessions/{sess['id']}/close")
    assert r.status_code == 200
    sessions = client.get(f"/projects/{project['id']}/sessions").json()
    assert sess["id"] not in {s["id"] for s in sessions}


def test_handoff_endpoint(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    client.post(
        f"/projects/{project['id']}/goal", json={"content": "ship it"}
    )
    sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "s1"},
    ).json()
    client.post(
        "/tasks",
        json={
            "session_id": sess["id"],
            "project_id": project["id"],
            "description": "step 1",
        },
    )
    r = client.post(f"/projects/{project['id']}/handoff")
    assert r.status_code == 200
    body = r.json()
    assert "MERIDIAN_CONTEXT" in body["content"]
    assert "ship it" in body["content"]
    assert "step 1" in body["content"]
    assert body["path"].endswith("alpha_handoff.md")
