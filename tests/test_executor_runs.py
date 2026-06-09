"""Tests for executor_runs — DB layer, REST endpoints, and MCP tool."""

from __future__ import annotations

import asyncio
import json

import pytest

from meridian import db as db_module


def _setup_authed_project(client, project_name: str) -> tuple[str, dict]:
    """Create project + tenant + API token. Returns (project_id, mcp_headers)."""
    proj_r = client.post("/projects", json={"name": project_name})
    assert proj_r.status_code == 201
    pid = proj_r.json()["id"]

    async def _create_token():
        db = client.app.state.db
        tenant = await db_module.upsert_tenant(db, f"{project_name}@test.invalid")
        raw, _ = await db_module.create_api_token(db, tenant["id"])
        return raw

    token = asyncio.run(_create_token())
    return pid, {"Authorization": f"Bearer {token}"}


def _mcp_call(client, headers, name, arguments):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_executor_run(db):
    p = await db_module.create_project(db, "exec-run-test")
    s = await db_module.register_session(db, p["id"], "sess-1")
    run = await db_module.create_executor_run(db, s["id"], p["id"])
    assert run["id"]
    assert run["session_id"] == s["id"]
    assert run["project_id"] == p["id"]
    assert run["status"] == "running"
    assert run["task_count"] == 0
    assert run["transcript"] == ""
    assert run["ended_at"] is None


@pytest.mark.asyncio
async def test_append_executor_run_transcript(db):
    p = await db_module.create_project(db, "exec-run-test")
    s = await db_module.register_session(db, p["id"], "sess-1")
    await db_module.create_executor_run(db, s["id"], p["id"])
    await db_module.append_executor_run_transcript(db, s["id"], "did thing A")
    await db_module.append_executor_run_transcript(db, s["id"], "did thing B")
    run = await db_module.get_executor_run_by_session(db, s["id"])
    assert run is not None
    assert "did thing A" in run["transcript"]
    assert "did thing B" in run["transcript"]
    assert run["task_count"] == 2


@pytest.mark.asyncio
async def test_log_task_appends_to_transcript(db):
    p = await db_module.create_project(db, "exec-run-test")
    s = await db_module.register_session(db, p["id"], "sess-1")
    await db_module.create_executor_run(db, s["id"], p["id"])
    await db_module.log_task(db, s["id"], p["id"], "fixed the auth bug")
    run = await db_module.get_executor_run_by_session(db, s["id"])
    assert run is not None
    assert "fixed the auth bug" in run["transcript"]
    assert run["task_count"] == 1


@pytest.mark.asyncio
async def test_log_task_multiple_appends(db):
    p = await db_module.create_project(db, "exec-run-test")
    s = await db_module.register_session(db, p["id"], "sess-1")
    await db_module.create_executor_run(db, s["id"], p["id"])
    for i in range(5):
        await db_module.log_task(db, s["id"], p["id"], f"task {i}")
    run = await db_module.get_executor_run_by_session(db, s["id"])
    assert run is not None
    assert run["task_count"] == 5
    for i in range(5):
        assert f"task {i}" in run["transcript"]


@pytest.mark.asyncio
async def test_finalize_executor_run(db):
    p = await db_module.create_project(db, "exec-run-test")
    s = await db_module.register_session(db, p["id"], "sess-1")
    await db_module.create_executor_run(db, s["id"], p["id"])
    await db_module.finalize_executor_run(db, s["id"], status="done")
    run = await db_module.get_executor_run_by_session(db, s["id"])
    assert run is not None
    assert run["status"] == "done"
    assert run["ended_at"] is not None


@pytest.mark.asyncio
async def test_close_session_finalizes_run(db):
    p = await db_module.create_project(db, "exec-run-test")
    s = await db_module.register_session(db, p["id"], "sess-1")
    await db_module.create_executor_run(db, s["id"], p["id"])
    await db_module.close_session(db, s["id"])
    run = await db_module.get_executor_run_by_session(db, s["id"])
    assert run is not None
    assert run["status"] == "done"
    assert run["ended_at"] is not None


@pytest.mark.asyncio
async def test_get_executor_runs_list(db):
    p = await db_module.create_project(db, "exec-run-test")
    s1 = await db_module.register_session(db, p["id"], "sess-1")
    s2 = await db_module.register_session(db, p["id"], "sess-2")
    await db_module.create_executor_run(db, s1["id"], p["id"])
    await db_module.create_executor_run(db, s2["id"], p["id"])
    runs = await db_module.get_executor_runs(db, p["id"])
    assert len(runs) == 2
    assert all(r["project_id"] == p["id"] for r in runs)


@pytest.mark.asyncio
async def test_get_executor_runs_newest_first(db):
    p = await db_module.create_project(db, "exec-run-test")
    s1 = await db_module.register_session(db, p["id"], "sess-1")
    s2 = await db_module.register_session(db, p["id"], "sess-2")
    r1 = await db_module.create_executor_run(db, s1["id"], p["id"])
    # Force a later timestamp by directly updating started_at
    await db.execute(
        "UPDATE executor_runs SET started_at = '2030-01-02 00:00:00' WHERE id = ?",
        (r1["id"],)
    )
    r2 = await db_module.create_executor_run(db, s2["id"], p["id"])
    await db.execute(
        "UPDATE executor_runs SET started_at = '2030-01-03 00:00:00' WHERE id = ?",
        (r2["id"],)
    )
    await db.commit()
    runs = await db_module.get_executor_runs(db, p["id"])
    assert len(runs) == 2
    assert runs[0]["id"] == r2["id"]


@pytest.mark.asyncio
async def test_get_executor_run_by_id(db):
    p = await db_module.create_project(db, "exec-run-test")
    s = await db_module.register_session(db, p["id"], "sess-1")
    created = await db_module.create_executor_run(db, s["id"], p["id"])
    fetched = await db_module.get_executor_run(db, created["id"])
    assert fetched is not None
    assert fetched["id"] == created["id"]


@pytest.mark.asyncio
async def test_get_executor_run_not_found(db):
    result = await db_module.get_executor_run(db, "nonexistent-id")
    assert result is None


@pytest.mark.asyncio
async def test_append_transcript_no_run_is_noop(db):
    p = await db_module.create_project(db, "exec-run-test")
    s = await db_module.register_session(db, p["id"], "sess-1")
    # No run created — should not raise
    await db_module.append_executor_run_transcript(db, s["id"], "should be ignored")
    run = await db_module.get_executor_run_by_session(db, s["id"])
    assert run is None


@pytest.mark.asyncio
async def test_finalize_no_run_is_noop(db):
    p = await db_module.create_project(db, "exec-run-test")
    s = await db_module.register_session(db, p["id"], "sess-1")
    # No run created — should not raise
    await db_module.finalize_executor_run(db, s["id"])


@pytest.mark.asyncio
async def test_log_task_without_run_still_works(db):
    """log_task should not fail when no executor_run exists for the session."""
    p = await db_module.create_project(db, "exec-run-test")
    s = await db_module.register_session(db, p["id"], "sess-1")
    # No run created
    task = await db_module.log_task(db, s["id"], p["id"], "some task")
    assert task["description"] == "some task"


# ---------------------------------------------------------------------------
# REST endpoints via client
# ---------------------------------------------------------------------------


def test_runs_endpoint_empty(client):
    r = client.post("/projects", json={"name": "runs-test"})
    assert r.status_code == 201
    pid = r.json()["id"]
    r2 = client.get(f"/projects/{pid}/runs")
    assert r2.status_code == 200
    assert r2.json() == []


def test_runs_endpoint_after_start_session(client):
    r = client.post("/projects", json={"name": "runs-test"})
    assert r.status_code == 201
    pid = r.json()["id"]
    # start_session creates an executor_run
    r2 = client.post(f"/projects/{pid}/start-session",
                     json={"session_name": "test-sess"})
    assert r2.status_code == 200
    runs = client.get(f"/projects/{pid}/runs").json()
    assert len(runs) == 1
    assert runs[0]["status"] == "running"
    assert runs[0]["project_id"] == pid


def test_runs_endpoint_duration_computed(client):
    r = client.post("/projects", json={"name": "runs-test"})
    pid = r.json()["id"]
    r2 = client.post(f"/projects/{pid}/start-session",
                     json={"session_name": "test-sess"})
    sid = r2.json()["session_id"]
    # Close the session to finalize the run
    client.post(f"/sessions/{sid}/close")
    runs = client.get(f"/projects/{pid}/runs").json()
    assert len(runs) == 1
    # duration_s should be computed (0 or small positive int since finalized quickly)
    assert runs[0]["duration_s"] is not None


def test_run_by_id_endpoint(client):
    r = client.post("/projects", json={"name": "runs-test"})
    pid = r.json()["id"]
    client.post(f"/projects/{pid}/start-session", json={"session_name": "sess"})
    runs = client.get(f"/projects/{pid}/runs").json()
    assert runs
    run_id = runs[0]["id"]
    r2 = client.get(f"/projects/{pid}/runs/{run_id}")
    assert r2.status_code == 200
    data = r2.json()
    assert data["id"] == run_id
    assert "transcript" in data


def test_run_by_id_wrong_project_404(client):
    r1 = client.post("/projects", json={"name": "p1"})
    r2 = client.post("/projects", json={"name": "p2"})
    pid1 = r1.json()["id"]
    pid2 = r2.json()["id"]
    client.post(f"/projects/{pid1}/start-session", json={"session_name": "sess"})
    runs = client.get(f"/projects/{pid1}/runs").json()
    run_id = runs[0]["id"]
    # Looking up run from pid1 but using pid2 should 404
    r3 = client.get(f"/projects/{pid2}/runs/{run_id}")
    assert r3.status_code == 404


def test_run_transcript_accumulates_via_log_task(client):
    pid, headers = _setup_authed_project(client, "runs-log-test")
    r2 = client.post(f"/projects/{pid}/start-session", json={"session_name": "sess"})
    sid = r2.json()["session_id"]
    _mcp_call(client, headers, "log_task", {
        "session_id": sid, "project_id": pid, "description": "wrote test A"
    })
    _mcp_call(client, headers, "log_task", {
        "session_id": sid, "project_id": pid, "description": "wrote test B"
    })
    runs = client.get(f"/projects/{pid}/runs").json()
    run_id = runs[0]["id"]
    full = client.get(f"/projects/{pid}/runs/{run_id}").json()
    assert "wrote test A" in full["transcript"]
    assert "wrote test B" in full["transcript"]
    assert full["task_count"] == 2


# ---------------------------------------------------------------------------
# MCP tool: get_session_log
# ---------------------------------------------------------------------------


def test_mcp_get_session_log(client):
    pid, headers = _setup_authed_project(client, "runs-mcp-test")
    r2 = client.post(f"/projects/{pid}/start-session", json={"session_name": "sess"})
    sid = r2.json()["session_id"]
    _mcp_call(client, headers, "log_task", {
        "session_id": sid, "project_id": pid, "description": "MCP tool test task"
    })
    resp = _mcp_call(client, headers, "get_session_log", {"session_id": sid})
    assert resp.status_code == 200
    result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert result["session_id"] == sid
    assert "MCP tool test task" in result["transcript"]
    assert result["task_count"] == 1


def test_mcp_get_session_log_no_run(client):
    _, headers = _setup_authed_project(client, "runs-mcp-norun-test")
    resp = _mcp_call(client, headers, "get_session_log", {"session_id": "no-such-session"})
    assert resp.status_code == 200
    result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "error" in result
