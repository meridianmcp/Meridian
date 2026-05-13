"""Core tests for Meridian — db layer, HTTP endpoints, and handoff."""

from __future__ import annotations

import asyncio
import sys

import pytest

from meridian import db as db_module
from meridian import enqueue as enqueue_module
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


# ---------------------------------------------------------------------------
# Paid-tier: enqueue_claude_task
# ---------------------------------------------------------------------------


# Stub worker: a Python one-liner that echoes the prompt back. Used in place
# of the real `claude` CLI so tests don't depend on it being installed.
_OK_WORKER = [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])"]
_FAIL_WORKER = [
    sys.executable,
    "-c",
    "import sys; sys.stderr.write('boom'); sys.exit(2)",
]


@pytest.mark.asyncio
async def test_update_task_changes_status_and_description(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess")
    t = await db_module.log_task(db, s["id"], p["id"], "old", "pending")
    updated = await db_module.update_task(
        db, t["id"], status="done", description="new"
    )
    assert updated is not None
    assert updated["status"] == "done"
    assert updated["description"] == "new"


@pytest.mark.asyncio
async def test_update_task_rejects_bad_status(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess")
    t = await db_module.log_task(db, s["id"], p["id"], "x", "pending")
    with pytest.raises(ValueError):
        await db_module.update_task(db, t["id"], status="bogus")


@pytest.mark.asyncio
async def test_enqueue_returns_pending_then_completes(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess")
    task = await enqueue_module.enqueue_claude_task(
        db,
        s["id"],
        p["id"],
        "hello world",
        worker_argv=_OK_WORKER,
        wait=False,
    )
    assert task["status"] == "pending"
    assert task["description"].startswith(enqueue_module.PROMPT_PREFIX)

    # Drain pending background tasks. asyncio.create_task() schedules the
    # worker coroutine; yielding the loop lets it finish before we assert.
    for _ in range(20):
        await asyncio.sleep(0.05)
        latest = await db_module.get_task(db, task["id"])
        if latest and latest["status"] != "pending":
            break
    assert latest is not None
    assert latest["status"] == "done"
    assert "hello world" in latest["description"]
    assert latest["description"].startswith(enqueue_module.RESULT_PREFIX)


@pytest.mark.asyncio
async def test_enqueue_marks_failed_on_nonzero_exit(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess")
    task = await enqueue_module.enqueue_claude_task(
        db,
        s["id"],
        p["id"],
        "trigger failure",
        worker_argv=_FAIL_WORKER,
        wait=True,
    )
    assert task["status"] == "failed"
    assert "exit code 2" in task["description"]
    assert "boom" in task["description"]


@pytest.mark.asyncio
async def test_enqueue_marks_failed_when_command_missing(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess")
    task = await enqueue_module.enqueue_claude_task(
        db,
        s["id"],
        p["id"],
        "ignored",
        worker_argv=["definitely-not-a-real-binary-7878"],
        wait=True,
    )
    assert task["status"] == "failed"
    assert "not found" in task["description"]


@pytest.mark.asyncio
async def test_enqueue_rejects_empty_prompt(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess")
    with pytest.raises(ValueError):
        await enqueue_module.enqueue_claude_task(
            db, s["id"], p["id"], "   ", worker_argv=_OK_WORKER
        )


@pytest.mark.asyncio
async def test_enqueue_respects_timeout(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess")
    slow = [sys.executable, "-c", "import time; time.sleep(5)"]
    task = await enqueue_module.enqueue_claude_task(
        db,
        s["id"],
        p["id"],
        "will hang",
        worker_argv=slow,
        timeout=0.5,
        wait=True,
    )
    assert task["status"] == "failed"
    assert "timed out" in task["description"]


def test_enqueue_http_endpoint_returns_202(client, monkeypatch):
    # Force the worker command to the in-process Python stub so the
    # endpoint test doesn't depend on `claude` being on PATH.
    monkeypatch.setenv(
        "MERIDIAN_WORKER_CMD",
        f'"{sys.executable}" -c "import sys; sys.stdout.write(sys.argv[1])"',
    )
    project = client.post("/projects", json={"name": "alpha"}).json()
    sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "s1"},
    ).json()
    r = client.post(
        "/tasks/enqueue",
        json={
            "session_id": sess["id"],
            "project_id": project["id"],
            "prompt": "say hi",
        },
    )
    assert r.status_code == 202
    task = r.json()
    assert task["status"] == "pending"
    assert task["description"].startswith(enqueue_module.PROMPT_PREFIX)


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
