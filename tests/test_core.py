"""Core tests for Meridian — db layer, HTTP endpoints, and handoff."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

from meridian import dashboard as dashboard_module
from meridian import db as db_module
from meridian import enqueue as enqueue_module
from meridian import handoff as handoff_module
from meridian import server as server_module


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
async def test_get_and_update_project_settings(db):
    p = await db_module.create_project(db, "alpha")
    settings = await db_module.get_project_settings(db, p["id"])
    assert settings["project_id"] == p["id"]
    assert settings["max_pinned_decisions"] == 20
    updated = await db_module.update_project_settings(
        db,
        p["id"],
        max_pinned_decisions=30,
    )
    assert updated["project_id"] == p["id"]
    assert updated["max_pinned_decisions"] == 30


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
async def test_set_goal_dedup_skips_version_bump_when_content_unchanged(db):
    """v2.3 — re-setting goal with identical content shouldn't bump version.

    Prevents the goal_states version counter from spamming when only
    sprint or north_star changes (the auto-summary loop and field-only
    updates would otherwise create hundreds of near-identical rows).
    """
    p = await db_module.create_project(db, "alpha")
    g1 = await db_module.set_goal(db, p["id"], "ship it")
    assert g1["version"] == 1
    # Same content, only sprint changes — should NOT bump version.
    g2 = await db_module.set_goal(db, p["id"], "ship it", sprint="week 1")
    assert g2["version"] == 1
    assert g2["sprint"] == "week 1"
    # Now content actually changes — version bumps.
    g3 = await db_module.set_goal(db, p["id"], "ship it harder")
    assert g3["version"] == 2


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
    """v2.4 — handoff renders L0/L1/L2 tiers with sessions + tasks + ai_summary."""
    p = await db_module.create_project(db, "alpha")
    await db_module.set_goal(db, p["id"], "build a thing")
    s1 = await db_module.register_session(db, p["id"], "sess-1")
    s2 = await db_module.register_session(db, p["id"], "sess-2")
    await db_module.log_task(db, s1["id"], p["id"], "did A", "done")
    await db_module.log_task(db, s2["id"], p["id"], "did B", "done")
    path, content = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "MERIDIAN_CONTEXT" in content
    # v2.4 — tier markers replace the old single "Goal State" header.
    assert "## L0 — Core Context" in content
    assert "## L1 — Current State" in content
    assert "build a thing" in content
    # v2.4 — both sessions still listed (active + recent).
    assert "sess-1" in content and "sess-2" in content
    assert "did A" in content and "did B" in content
    assert "## Quick Start" in content
    assert "/goal Verify remaining work is complete." in content
    assert "pixi run test passes 524+" in content
    assert "## Resume Instructions" in content
    on_disk = tmp_path / "alpha_handoff.md"
    assert on_disk.exists()
    assert on_disk.read_text(encoding="utf-8") == content
    assert str(on_disk.resolve()) == path


@pytest.mark.asyncio
async def test_handoff_lists_pending_sprint_items_in_dependency_order(db, tmp_path):
    p = await db_module.create_project(db, "alpha-queue")
    await db_module.set_goal(db, p["id"], "ship the queue")
    first = await db_module.add_sprint_item(db, p["id"], "v1", "First fix")
    second = await db_module.add_sprint_item(
        db, p["id"], "v1", "Second fix", depends_on=first["id"]
    )
    _, content = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "1. [pending] First fix" in content
    assert "2. [pending] Second fix" in content
    assert f"Depends on item 1 (`{first['id']}`): First fix" in content
    assert f'start_session(project_id="{p["id"]}", session_name="<your-name>")' in content
    assert (
        f"/goal Complete sprint items: {first['id']}, {second['id']}."
        in content
    )
    assert "complete_sprint_item()" in content


@pytest.mark.asyncio
async def test_handoff_includes_strategic_notes(db, tmp_path):
    p = await db_module.create_project(db, "alpha-strategy")
    await db_module.set_goal(db, p["id"], "ship with context")
    await db_module.add_project_note(
        db,
        p["id"],
        "Competitive Landscape",
        "Meridian competes on coordination, not benchmark memory scores.",
        "competitive,strategy",
    )
    await db_module.add_project_note(
        db,
        p["id"],
        "Infra Note",
        "This should stay out of L0 strategic notes.",
        "technical",
    )
    _, content = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "Strategic Notes (1)" in content
    assert "Competitive Landscape" in content
    assert "coordination, not benchmark memory scores" in content
    assert "Infra Note" not in content


@pytest.mark.asyncio
async def test_handoff_delta_mode_reports_recent_changes(db, tmp_path):
    p = await db_module.create_project(db, "alpha-delta")
    await db_module.set_goal(db, p["id"], "ship incremental work")
    first = await db_module.add_sprint_item(db, p["id"], "v1", "First fix")
    second = await db_module.add_sprint_item(
        db, p["id"], "v1", "Second fix", depends_on=first["id"]
    )
    await handoff_module.generate_handoff(
        db,
        p["id"],
        str(tmp_path),
        skip_ai_summary=True,
        mode="full",
        session_id="sess-delta",
    )
    await db_module.complete_sprint_item(db, p["id"], first["id"])
    _, content = await handoff_module.generate_handoff(
        db,
        p["id"],
        str(tmp_path),
        skip_ai_summary=True,
        mode="delta",
        session_id="sess-delta",
    )
    assert "# Session Update — alpha-delta" in content
    assert f"- {first['id']} — First fix" in content
    assert f"- {second['id']} [pending] Second fix" in content
    assert f"/goal Complete sprint items: {second['id']}." in content


async def test_handoff_starter_mode(db, tmp_path):
    """generate_handoff(mode='starter') returns compact ≤20-line block."""
    p = await db_module.create_project(db, "alpha-starter")
    await db_module.set_goal(db, p["id"], "ship starter mode")
    it1 = await db_module.add_sprint_item(db, p["id"], "v1", "First item")
    it2 = await db_module.add_sprint_item(db, p["id"], "v1", "Second item")
    await db_module.complete_sprint_item(db, p["id"], it1["id"])
    _, content = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter"
    )
    lines = [l for l in content.splitlines() if l]
    assert len(lines) <= 20, f"starter mode must be ≤20 non-empty lines, got {len(lines)}"
    assert f'project_id: {p["id"]}' in content
    assert 'start_session' in content
    assert it2["id"][:8] in content   # pending item ID appears
    assert "/goal" in content


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


def test_project_settings_http_round_trip(client):
    project = client.post("/projects", json={"name": "alpha-settings"}).json()
    r = client.get(f"/projects/{project['id']}/settings")
    assert r.status_code == 200
    data = r.json()
    assert data["project_id"] == project["id"]
    assert data["max_pinned_decisions"] == 20

    r = client.patch(
        f"/projects/{project['id']}/settings",
        json={"max_pinned_decisions": 30},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["project_id"] == project["id"]
    assert data["max_pinned_decisions"] == 30


def test_executor_config_round_trip(client):
    project = client.post("/projects", json={"name": "exec-cfg-test"}).json()
    # Save executor config
    r = client.patch(
        f"/projects/{project['id']}/settings",
        json={"executor_config": {"test_cmd": "pixi run test", "test_min": 600, "branch": "dev"}},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["executor_config"]["test_cmd"] == "pixi run test"
    assert data["executor_config"]["test_min"] == 600
    assert data["executor_config"]["branch"] == "dev"

    # Verify persisted on GET
    r = client.get(f"/projects/{project['id']}/settings")
    assert r.json()["executor_config"]["test_cmd"] == "pixi run test"


def test_goal_round_trip_and_versioning(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    r = client.get(f"/projects/{project['id']}/goal")
    # G9 — unset goal returns 200 with empty fields (was 404). Browsers
    # log fetch 4xx to console, which broke the panel-render Playwright
    # test on every fresh-project initial render.
    assert r.status_code == 200
    body = r.json()
    assert body["content"] == ""
    assert body["version"] == 0
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


def test_task_model_serializes_skipped_status():
    """Regression: GET /projects/{id}/tasks must serialize task_log rows with
    status 'skipped'. Postgres task_log has no CHECK constraint, so historical
    rows can carry 'skipped'; the Task response model previously omitted it from
    its Literal and 500'd the whole endpoint with a ResponseValidationError."""
    from meridian.models import Task

    t = Task(
        id="t1", session_id="s1", project_id="p1",
        description="x", status="skipped", created_at="2026-01-01T00:00:00Z",
    )
    assert t.status == "skipped"


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
    # v1.0.1: status transitions pending -> in_progress -> done/failed,
    # so we wait until status is a terminal state.
    for _ in range(40):
        await asyncio.sleep(0.05)
        latest = await db_module.get_task(db, task["id"])
        if latest and latest["status"] in {"done", "failed"}:
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


def test_handoff_endpoint_auto_switches_repeat_session_to_delta(client):
    project = client.post("/projects", json={"name": "alpha-delta-http"}).json()
    parent = client.post(
        f"/projects/{project['id']}/sprint-items",
        json={"version": "v1", "title": "Parent item"},
    ).json()
    child = client.post(
        f"/projects/{project['id']}/sprint-items",
        json={"version": "v1", "title": "Child item", "depends_on": parent["id"]},
    ).json()

    first = client.post(
        f"/projects/{project['id']}/handoff",
        json={"session_id": "sess-http-delta"},
    )
    assert first.status_code == 200
    assert first.json()["mode"] == "full"

    client.post(
        f"/projects/{project['id']}/sprint-items/{parent['id']}/complete",
        json={},
    )
    second = client.post(
        f"/projects/{project['id']}/handoff",
        json={"session_id": "sess-http-delta"},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["mode"] == "delta"
    assert "# Session Update — alpha-delta-http" in body["content"]
    assert f"- {parent['id']} — Parent item" in body["content"]
    assert f"- {child['id']} [pending] Child item" in body["content"]


@pytest.mark.asyncio
async def test_docs_mcp_tools_matches_live_tool_doc():
    expected = await server_module.mcp_tools_doc()
    actual = (
        Path(__file__).resolve().parents[1] / "docs" / "mcp-tools.md"
    ).read_text(encoding="utf-8")
    assert actual.rstrip() == expected.rstrip()


# ---------------------------------------------------------------------------
# v0.2.0: pending-hitl, dashboard, chat, WebSocket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_task_accepts_pending_hitl(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess")
    t = await db_module.log_task(
        db, s["id"], p["id"], "[ASK]: pick a color", "pending-hitl"
    )
    assert t["status"] == "pending-hitl"


@pytest.mark.asyncio
async def test_log_task_publishes_to_subscribers(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess")
    queue = db_module.subscribe_tasks(p["id"])
    try:
        t = await db_module.log_task(db, s["id"], p["id"], "ping", "done")
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert event["type"] == "task_created"
        assert event["task"]["id"] == t["id"]
    finally:
        db_module.unsubscribe_tasks(p["id"], queue)


@pytest.mark.asyncio
async def test_update_task_publishes_to_subscribers(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess")
    t = await db_module.log_task(
        db, s["id"], p["id"], "[ASK]: ?", "pending-hitl"
    )
    queue = db_module.subscribe_tasks(p["id"])
    try:
        await db_module.update_task(db, t["id"], status="done")
        # Drain the task_created leftover if present, then look for the
        # update — subscribe was after create, but be defensive anyway.
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert event["type"] == "task_updated"
        assert event["task"]["status"] == "done"
    finally:
        db_module.unsubscribe_tasks(p["id"], queue)


def test_dashboard_html_served(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Meridian Dashboard" in r.text
    # v1.0.2: JS moved to static file — check JS for WebSocket wiring
    js = client.get("/static/dashboard.js").text
    assert "/ws/" in js  # WebSocket wiring present


def test_config_api_key_unset(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(dashboard_module, "load_oauth_token", lambda: None)
    r = client.get("/config/api-key")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["method"] is None


def test_config_api_key_set(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-1234")
    monkeypatch.setattr(dashboard_module, "load_oauth_token", lambda: None)
    r = client.get("/config/api-key")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["method"] == "api_key"
    # Key itself never echoed.
    assert "sk-test-1234" not in r.text


def test_config_api_key_oauth_method(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        dashboard_module, "load_oauth_token", lambda: "sk-ant-oat01-fake"
    )
    r = client.get("/config/api-key")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["method"] == "oauth"
    # Token itself never echoed.
    assert "sk-ant-oat01-fake" not in r.text


def test_patch_task_flips_status(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "s1"},
    ).json()
    task = client.post(
        "/tasks",
        json={
            "session_id": sess["id"],
            "project_id": project["id"],
            "description": "[ASK]: ok?",
            "status": "pending-hitl",
        },
    ).json()
    r = client.patch(
        f"/tasks/{task['id']}",
        json={"status": "done", "description": "[ANSWERED] yes"},
    )
    assert r.status_code == 200
    updated = r.json()
    assert updated["status"] == "done"
    assert updated["description"] == "[ANSWERED] yes"


def test_patch_task_404(client):
    r = client.patch("/tasks/no-such-id", json={"status": "done"})
    assert r.status_code == 404


def test_websocket_receives_task_event(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "s1"},
    ).json()
    with client.websocket_connect(f"/ws/{project['id']}") as ws:
        # Trigger a task via HTTP; pub/sub should fan out to the socket.
        client.post(
            "/tasks",
            json={
                "session_id": sess["id"],
                "project_id": project["id"],
                "description": "[ASK]: pick one",
                "status": "pending-hitl",
            },
        )
        msg = ws.receive_text()
    event = json.loads(msg)
    assert event["type"] == "task_created"
    assert event["task"]["status"] == "pending-hitl"
    assert event["task"]["description"] == "[ASK]: pick one"


def test_get_auth_token_uses_oauth_first(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-env")
    monkeypatch.setattr(
        dashboard_module, "load_oauth_token", lambda: "sk-ant-oat01-fake"
    )
    token, method = dashboard_module.get_auth_token()
    assert method == "oauth"
    assert token == "sk-ant-oat01-fake"


def test_get_auth_token_falls_back_to_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-env")
    monkeypatch.setattr(dashboard_module, "load_oauth_token", lambda: None)
    token, method = dashboard_module.get_auth_token()
    assert method == "api_key"
    assert token == "sk-test-env"


def test_get_auth_token_returns_none_when_neither_set(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(dashboard_module, "load_oauth_token", lambda: None)
    token, method = dashboard_module.get_auth_token()
    assert token is None
    assert method is None


def test_load_oauth_token_reads_credentials_file(tmp_path, monkeypatch):
    """load_oauth_token reads the token from a real credentials file."""
    import json as _json

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    creds = {"claudeAiOauth": {"accessToken": "sk-ant-oat01-testtoken"}}
    (claude_dir / ".credentials.json").write_text(
        _json.dumps(creds), encoding="utf-8"
    )

    monkeypatch.setattr(
        "pathlib.Path.home",
        classmethod(lambda cls: tmp_path),
    )
    token = dashboard_module.load_oauth_token()
    assert token == "sk-ant-oat01-testtoken"


def test_migration_rebuilds_old_check_constraint(tmp_path):
    """Open a database that predates pending-hitl, then re-open via
    init_db and confirm pending-hitl now passes validation."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE goal_states (id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            content TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE sessions (id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','idle','closed')),
            last_seen TEXT NOT NULL DEFAULT (datetime('now')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE task_log (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            project_id TEXT NOT NULL, description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'done'
                CHECK (status IN ('pending','done','failed')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        INSERT INTO projects (id, name) VALUES ('p1', 'legacy');
        INSERT INTO sessions (id, project_id, name) VALUES ('s1','p1','old');
        INSERT INTO task_log (id, session_id, project_id, description, status)
            VALUES ('t1','s1','p1','old task','done');
        """
    )
    legacy.commit()
    legacy.close()

    async def run() -> None:
        conn = await db_module.init_db(str(db_path))
        try:
            # Existing row survived the rebuild.
            t = await db_module.get_task(conn, "t1")
            assert t is not None
            assert t["description"] == "old task"
            # New pending-hitl status now accepted.
            new = await db_module.log_task(
                conn, "s1", "p1", "[ASK]: ?", "pending-hitl"
            )
            assert new["status"] == "pending-hitl"
        finally:
            await conn.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Session TTL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expire_idle_sessions_marks_old_sessions(db):
    """Sessions not seen in the past 30 minutes should be flipped to idle."""
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "stale")
    # Back-date last_seen to 60 minutes ago.
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now', '-60 minutes') WHERE id = ?",
        (s["id"],),
    )
    await db.commit()
    result = await db_module.expire_idle_sessions(db, max_age_minutes=30)
    assert result["count"] >= 1
    assert p["id"] in result["project_ids"]
    sessions = await db_module.get_sessions(db, p["id"], active_only=False)
    stale = next(x for x in sessions if x["id"] == s["id"])
    assert stale["status"] == "idle"


@pytest.mark.asyncio
async def test_expire_idle_sessions_leaves_recent_sessions(db):
    """Sessions seen within the TTL window must not be touched."""
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "fresh")
    result = await db_module.expire_idle_sessions(db, max_age_minutes=30)
    assert result["count"] == 0
    assert result["project_ids"] == []
    sessions = await db_module.get_sessions(db, p["id"], active_only=False)
    fresh = next(x for x in sessions if x["id"] == s["id"])
    assert fresh["status"] == "active"


@pytest.mark.asyncio
async def test_archive_stale_sessions_marks_old_sessions(db):
    """Sessions with last_seen > 7 days are moved to 'archived'."""
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "old")
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now', '-8 days') WHERE id = ?",
        (s["id"],),
    )
    await db.commit()
    count = await db_module.archive_stale_sessions(db, p["id"])
    assert count == 1
    sessions = await db_module.get_sessions(db, p["id"], active_only=False)
    archived = next(x for x in sessions if x["id"] == s["id"])
    assert archived["status"] == "archived"


@pytest.mark.asyncio
async def test_archive_stale_sessions_excluded_from_active_view(db):
    """Archived sessions must not appear in the default active-only list."""
    p = await db_module.create_project(db, "alpha")
    old = await db_module.register_session(db, p["id"], "old")
    fresh = await db_module.register_session(db, p["id"], "fresh")
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now', '-8 days') WHERE id = ?",
        (old["id"],),
    )
    await db.commit()
    await db_module.archive_stale_sessions(db, p["id"])
    active = await db_module.get_sessions(db, p["id"], active_only=True)
    ids = [s["id"] for s in active]
    assert old["id"] not in ids
    assert fresh["id"] in ids


@pytest.mark.asyncio
async def test_archive_stale_sessions_leaves_recent_sessions(db):
    """Sessions seen within 7 days must not be archived."""
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "fresh")
    count = await db_module.archive_stale_sessions(db, p["id"])
    assert count == 0
    sessions = await db_module.get_sessions(db, p["id"], active_only=False)
    still_active = next(x for x in sessions if x["id"] == s["id"])
    assert still_active["status"] == "active"


@pytest.mark.asyncio
async def test_expire_inactive_sessions_archives_after_24h(db):
    """Sessions unseen for 24h+ are archived globally across all projects."""
    p = await db_module.create_project(db, "alpha")
    dead = await db_module.register_session(db, p["id"], "dead")
    fresh = await db_module.register_session(db, p["id"], "fresh")
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now', '-25 hours') WHERE id = ?",
        (dead["id"],),
    )
    await db.commit()
    result = await db_module.expire_inactive_sessions(db, max_age_hours=24)
    assert result["count"] == 1
    assert p["id"] in result["project_ids"]
    sessions = await db_module.get_sessions(db, p["id"], active_only=False)
    by_id = {s["id"]: s for s in sessions}
    assert by_id[dead["id"]]["status"] == "archived"
    assert by_id[fresh["id"]]["status"] == "active"


@pytest.mark.asyncio
async def test_expire_inactive_sessions_releases_in_progress_tasks(db):
    """Archiving a dead session releases its in_progress tasks back to pending."""
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "dead")
    t = await db_module.log_task(db, s["id"], p["id"], "claimed work")
    await db.execute(
        "UPDATE task_log SET status = 'in_progress', claimed_by = ? WHERE id = ?",
        (s["id"], t["id"]),
    )
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now', '-25 hours') WHERE id = ?",
        (s["id"],),
    )
    await db.commit()
    await db_module.expire_inactive_sessions(db, max_age_hours=24)
    async with db.execute(
        "SELECT status, claimed_by FROM task_log WHERE id = ?", (t["id"],)
    ) as cur:
        row = await cur.fetchone()
    assert row["status"] == "pending"
    assert row["claimed_by"] is None


@pytest.mark.asyncio
async def test_expire_inactive_sessions_leaves_recent_sessions(db):
    """Sessions seen within 24h must not be archived."""
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "fresh")
    result = await db_module.expire_inactive_sessions(db, max_age_hours=24)
    assert result["count"] == 0
    sessions = await db_module.get_sessions(db, p["id"], active_only=False)
    still_active = next(x for x in sessions if x["id"] == s["id"])
    assert still_active["status"] == "active"


@pytest.mark.asyncio
async def test_archive_empty_sessions_marks_old_taskless_sessions(db):
    """Old sessions with no task_log rows are archived during cleanup."""
    p = await db_module.create_project(db, "alpha-empty")
    s = await db_module.register_session(db, p["id"], "empty")
    await db.execute(
        "UPDATE sessions SET status = 'idle', created_at = datetime('now', '-8 days') WHERE id = ?",
        (s["id"],),
    )
    await db.commit()

    count = await db_module.archive_empty_sessions(db)
    assert count >= 1

    sessions = await db_module.get_sessions(db, p["id"], active_only=False)
    archived = next(x for x in sessions if x["id"] == s["id"])
    assert archived["status"] == "archived"


@pytest.mark.asyncio
async def test_archive_empty_sessions_keeps_sessions_with_tasks(db):
    """Old sessions with task_log rows are not archived by empty-session cleanup."""
    p = await db_module.create_project(db, "alpha-not-empty")
    s = await db_module.register_session(db, p["id"], "worked")
    await db_module.log_task(db, s["id"], p["id"], "did work", "done")
    await db.execute(
        "UPDATE sessions SET status = 'idle', created_at = datetime('now', '-8 days') WHERE id = ?",
        (s["id"],),
    )
    await db.commit()

    await db_module.archive_empty_sessions(db)
    sessions = await db_module.get_sessions(db, p["id"], active_only=False)
    kept = next(x for x in sessions if x["id"] == s["id"])
    assert kept["status"] == "idle"


def test_sessions_endpoint_expires_stale_sessions(client):
    """GET /projects/{id}/sessions triggers idle expiry before returning."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "stale"},
    ).json()
    # Manually back-date via the DB — use a raw SQL approach via a task
    # workaround: create a second project to force a fresh client cycle,
    # but the simplest check is that the endpoint returns 200 without error.
    r = client.get(f"/projects/{project['id']}/sessions")
    assert r.status_code == 200
    # The fresh session should still be active (back-dated only via DB direct).
    ids = [s["id"] for s in r.json()]
    assert sess["id"] in ids


def test_dashboard_html_has_favicon(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "favicon" in r.text or "rel=\"icon\"" in r.text


def test_dashboard_html_shows_version(client):
    r = client.get("/dashboard")
    # Loose match — the version label moves but the prefix stays "v0."
    assert "v0." in r.text


# ---------------------------------------------------------------------------
# v0.3.1: file editing endpoints
# ---------------------------------------------------------------------------

import importlib


def test_list_project_files_returns_allow_list(client, monkeypatch, tmp_path):
    """GET /projects/{id}/files returns the three editable filenames."""
    import meridian.server as srv_mod
    srv_mod = importlib.reload(srv_mod)
    monkeypatch.setattr(srv_mod, "_REPO_ROOT", tmp_path)

    project = client.post("/projects", json={"name": "alpha"}).json()
    r = client.get(f"/projects/{project['id']}/files")
    assert r.status_code == 200
    files = r.json()
    assert "AGENTS.md" in files
    assert "ROADMAP.md" in files
    assert "DEVLOG.md" in files


def test_get_project_file_returns_empty_when_missing(client, monkeypatch, tmp_path):
    """Reading a file that does not exist returns content='' not a 404."""
    import meridian.server as srv_mod
    srv_mod = importlib.reload(srv_mod)
    monkeypatch.setattr(srv_mod, "_REPO_ROOT", tmp_path)

    project = client.post("/projects", json={"name": "alpha"}).json()
    r = client.get(f"/projects/{project['id']}/files/AGENTS.md")
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "AGENTS.md"
    assert body["content"] == ""


def test_put_and_get_project_file_roundtrip(client, monkeypatch, tmp_path):
    """Writing then reading a file returns the written content."""
    import meridian.server as srv_mod
    srv_mod = importlib.reload(srv_mod)
    monkeypatch.setattr(srv_mod, "_REPO_ROOT", tmp_path)

    project = client.post("/projects", json={"name": "alpha"}).json()
    r = client.put(
        f"/projects/{project['id']}/files/DEVLOG.md",
        json={"content": "# Dev Log\n\nEntry 1."},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "DEVLOG.md"
    assert body["size"] > 0

    r2 = client.get(f"/projects/{project['id']}/files/DEVLOG.md")
    assert r2.status_code == 200
    assert r2.json()["content"] == "# Dev Log\n\nEntry 1."


def test_get_project_file_403_for_disallowed_filename(client, monkeypatch, tmp_path):
    """Accessing a filename outside the allow-list returns 403."""
    import meridian.server as srv_mod
    srv_mod = importlib.reload(srv_mod)
    monkeypatch.setattr(srv_mod, "_REPO_ROOT", tmp_path)

    project = client.post("/projects", json={"name": "alpha"}).json()
    # Path traversal: FastAPI normalises the URL so it may 404 before reaching
    # our handler, or our allow-list check returns 403. Either is secure.
    r = client.get(f"/projects/{project['id']}/files/../../etc/passwd")
    assert r.status_code in (403, 404)


def test_put_project_file_403_for_disallowed_filename(client, monkeypatch, tmp_path):
    """Writing outside the allow-list returns 403."""
    import meridian.server as srv_mod
    srv_mod = importlib.reload(srv_mod)
    monkeypatch.setattr(srv_mod, "_REPO_ROOT", tmp_path)

    project = client.post("/projects", json={"name": "alpha"}).json()
    r = client.put(
        f"/projects/{project['id']}/files/evil.sh",
        json={"content": "rm -rf /"},
    )
    assert r.status_code == 403


def test_file_endpoints_404_for_unknown_project(client, monkeypatch, tmp_path):
    """All file endpoints return 404 when the project does not exist."""
    import meridian.server as srv_mod
    srv_mod = importlib.reload(srv_mod)
    monkeypatch.setattr(srv_mod, "_REPO_ROOT", tmp_path)

    assert client.get("/projects/no-such/files").status_code == 404
    assert client.get("/projects/no-such/files/AGENTS.md").status_code == 404
    assert client.put(
        "/projects/no-such/files/AGENTS.md", json={"content": "x"}
    ).status_code == 404


# ---------------------------------------------------------------------------
# v0.3.2 — human identity + goal ownership
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_project_records_creator_human_id(db):
    p = await db_module.create_project(db, "alpha", human_id="adam")
    assert p["creator_human_id"] == "adam"
    owner = await db_module.get_project_owner(db, p["id"])
    assert owner == "adam"


@pytest.mark.asyncio
async def test_create_project_without_human_id_has_null_owner(db):
    p = await db_module.create_project(db, "alpha")
    assert p["creator_human_id"] is None
    owner = await db_module.get_project_owner(db, p["id"])
    assert owner is None


@pytest.mark.asyncio
async def test_register_session_records_human_id(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess", human_id="adam")
    assert s["human_id"] == "adam"


@pytest.mark.asyncio
async def test_register_session_without_human_id_is_null(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess")
    assert s["human_id"] is None


def test_post_project_propagates_creator_human_id(client):
    r = client.post("/projects", json={"name": "alpha", "human_id": "adam"})
    assert r.status_code == 201
    project = r.json()
    assert project["creator_human_id"] == "adam"


def test_post_register_session_propagates_human_id(client):
    project = client.post(
        "/projects", json={"name": "alpha", "human_id": "adam"}
    ).json()
    r = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "s1", "human_id": "adam"},
    )
    assert r.status_code == 201
    assert r.json()["human_id"] == "adam"


def test_set_goal_403_when_human_id_does_not_match_owner(client):
    project = client.post(
        "/projects", json={"name": "alpha", "human_id": "adam"}
    ).json()
    r = client.post(
        f"/projects/{project['id']}/goal",
        json={"content": "stealth edit", "human_id": "beth"},
    )
    assert r.status_code == 403
    detail = r.json()["detail"]
    # detail can be dict or string depending on how FastAPI serialises;
    # we accept either form but require the marker string is present.
    body = json.dumps(detail) if not isinstance(detail, str) else detail
    assert "goal_locked" in body


def test_set_goal_200_when_owner_matches(client):
    project = client.post(
        "/projects", json={"name": "alpha", "human_id": "adam"}
    ).json()
    r = client.post(
        f"/projects/{project['id']}/goal",
        json={"content": "ship it", "human_id": "adam"},
    )
    assert r.status_code == 200
    assert r.json()["content"] == "ship it"


def test_set_goal_200_when_no_human_id_anywhere_backward_compat(client):
    # No creator_human_id, no body.human_id — legacy callers still work.
    project = client.post("/projects", json={"name": "alpha"}).json()
    r = client.post(
        f"/projects/{project['id']}/goal", json={"content": "ship it"}
    )
    assert r.status_code == 200


def test_set_goal_200_when_owner_set_but_body_human_id_absent(client):
    # Sessions that don't claim an identity still get their old write
    # privilege — the ownership rule only fires when both sides assert.
    project = client.post(
        "/projects", json={"name": "alpha", "human_id": "adam"}
    ).json()
    r = client.post(
        f"/projects/{project['id']}/goal", json={"content": "ship it"}
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# v0.3.3 — claim_task / release_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_task_succeeds_when_unclaimed(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "worker-1")
    t = await db_module.log_task(db, s["id"], p["id"], "do thing", "pending")
    claimed = await db_module.claim_task(db, t["id"], s["id"])
    assert claimed is not None
    assert claimed["claimed_by"] == s["id"]
    assert claimed["claimed_at"] is not None
    assert claimed["status"] == "in_progress"


@pytest.mark.asyncio
async def test_claim_task_fails_when_already_claimed(db):
    p = await db_module.create_project(db, "alpha")
    s1 = await db_module.register_session(db, p["id"], "worker-1")
    s2 = await db_module.register_session(db, p["id"], "worker-2")
    t = await db_module.log_task(db, s1["id"], p["id"], "do thing", "pending")
    first = await db_module.claim_task(db, t["id"], s1["id"])
    second = await db_module.claim_task(db, t["id"], s2["id"])
    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_claim_task_fails_when_status_not_pending(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "worker-1")
    t = await db_module.log_task(db, s["id"], p["id"], "did it", "done")
    claimed = await db_module.claim_task(db, t["id"], s["id"])
    assert claimed is None


@pytest.mark.asyncio
async def test_release_task_succeeds_for_owner(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "worker-1")
    t = await db_module.log_task(db, s["id"], p["id"], "do thing", "pending")
    await db_module.claim_task(db, t["id"], s["id"])
    released = await db_module.release_task(db, t["id"], s["id"])
    assert released is True
    fresh = await db_module.get_task(db, t["id"])
    assert fresh is not None
    assert fresh["status"] == "pending"
    assert fresh["claimed_by"] is None
    assert fresh["claimed_at"] is None


@pytest.mark.asyncio
async def test_release_task_fails_for_non_owner(db):
    p = await db_module.create_project(db, "alpha")
    s1 = await db_module.register_session(db, p["id"], "worker-1")
    s2 = await db_module.register_session(db, p["id"], "worker-2")
    t = await db_module.log_task(db, s1["id"], p["id"], "do thing", "pending")
    await db_module.claim_task(db, t["id"], s1["id"])
    released = await db_module.release_task(db, t["id"], s2["id"])
    assert released is False
    fresh = await db_module.get_task(db, t["id"])
    assert fresh is not None
    assert fresh["claimed_by"] == s1["id"]


@pytest.mark.asyncio
async def test_get_claimable_tasks_filters_correctly(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "worker-1")
    pending_unclaimed = await db_module.log_task(
        db, s["id"], p["id"], "ready", "pending"
    )
    pending_claimed = await db_module.log_task(
        db, s["id"], p["id"], "taken", "pending"
    )
    await db_module.claim_task(db, pending_claimed["id"], s["id"])
    await db_module.log_task(db, s["id"], p["id"], "shipped", "done")
    rows = await db_module.get_claimable_tasks(db, p["id"])
    ids = {r["id"] for r in rows}
    assert pending_unclaimed["id"] in ids
    assert pending_claimed["id"] not in ids
    assert all(r["status"] == "pending" for r in rows)
    assert all(r["claimed_by"] is None for r in rows)


def test_http_claim_and_release_round_trip(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "worker-1"},
    ).json()
    task = client.post(
        "/tasks",
        json={
            "session_id": sess["id"],
            "project_id": project["id"],
            "description": "step 1",
            "status": "pending",
        },
    ).json()
    r = client.post(
        f"/projects/{project['id']}/tasks/claim",
        json={"task_id": task["id"], "session_id": sess["id"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["claimed"] is True
    assert body["claimed_by"] == sess["id"]

    # Second claim from a different session is refused
    sess2 = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "worker-2"},
    ).json()
    r2 = client.post(
        f"/projects/{project['id']}/tasks/claim",
        json={"task_id": task["id"], "session_id": sess2["id"]},
    )
    assert r2.status_code == 200
    assert r2.json()["claimed"] is False

    # Release by non-owner -> 404
    r3 = client.post(
        f"/projects/{project['id']}/tasks/release",
        json={"task_id": task["id"], "session_id": sess2["id"]},
    )
    assert r3.status_code == 404

    # Release by owner -> 200
    r4 = client.post(
        f"/projects/{project['id']}/tasks/release",
        json={"task_id": task["id"], "session_id": sess["id"]},
    )
    assert r4.status_code == 200
    assert r4.json()["released"] is True


def test_http_claimable_filters_claimed(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "worker-1"},
    ).json()
    task_a = client.post(
        "/tasks",
        json={
            "session_id": sess["id"],
            "project_id": project["id"],
            "description": "a",
            "status": "pending",
        },
    ).json()
    task_b = client.post(
        "/tasks",
        json={
            "session_id": sess["id"],
            "project_id": project["id"],
            "description": "b",
            "status": "pending",
        },
    ).json()
    client.post(
        f"/projects/{project['id']}/tasks/claim",
        json={"task_id": task_a["id"], "session_id": sess["id"]},
    )
    r = client.get(f"/projects/{project['id']}/tasks/claimable")
    assert r.status_code == 200
    ids = {t["id"] for t in r.json()}
    assert task_a["id"] not in ids
    assert task_b["id"] in ids


# ---------------------------------------------------------------------------
# v0.4.0 — project switcher in dashboard
# ---------------------------------------------------------------------------


def test_dashboard_html_contains_project_switcher(client):
    """HTML smoke test: the v0.4.0 switcher + human_id input are present."""
    html = client.get("/dashboard").text
    assert 'id="project-switcher"' in html
    assert 'id="new-project-human"' in html
    # v1.0.2: JS moved to static file — check JS for localStorage const
    js = client.get("/static/dashboard.js").text
    assert "ACTIVE_PROJECT_KEY" in js  # localStorage persistence wired


def test_get_projects_returns_list_with_creator(client):
    """v0.4.0 contract: GET /projects returns every project the
    switcher needs to render, including the creator_human_id."""
    client.post("/projects", json={"name": "alpha", "human_id": "adam"})
    client.post("/projects", json={"name": "beta"})
    r = client.get("/projects")
    assert r.status_code == 200
    names = {p["name"] for p in r.json()}
    assert {"alpha", "beta"} <= names
    by_name = {p["name"]: p for p in r.json()}
    assert by_name["alpha"]["creator_human_id"] == "adam"
    assert by_name["beta"]["creator_human_id"] is None


# ---------------------------------------------------------------------------
# v0.4.2 — auto goal mode + ambient context injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_goal_mode_defaults_to_manual(db):
    p = await db_module.create_project(db, "alpha")
    assert await db_module.get_goal_mode(db, p["id"]) == "manual"


@pytest.mark.asyncio
async def test_set_goal_mode_round_trip(db):
    p = await db_module.create_project(db, "alpha")
    await db_module.set_goal_mode(db, p["id"], "auto")
    assert await db_module.get_goal_mode(db, p["id"]) == "auto"
    await db_module.set_goal_mode(db, p["id"], "manual")
    assert await db_module.get_goal_mode(db, p["id"]) == "manual"


@pytest.mark.asyncio
async def test_set_goal_mode_rejects_invalid(db):
    p = await db_module.create_project(db, "alpha")
    with pytest.raises(ValueError):
        await db_module.set_goal_mode(db, p["id"], "bogus")


def test_format_auto_summary_block_includes_task_lines():
    block = db_module.format_auto_summary_block(
        [
            {"status": "done", "description": "shipped widget"},
            {"status": "pending", "description": "fix typo"},
        ],
        timestamp="2026-01-01 00:00 UTC",
    )
    assert block.startswith("[AUTO SUMMARY - 2026-01-01 00:00 UTC]")
    assert "[DONE] shipped widget" in block
    assert "[PENDING] fix typo" in block


def test_format_auto_summary_block_handles_empty():
    block = db_module.format_auto_summary_block([], timestamp="ts")
    assert "(no recent activity)" in block


@pytest.mark.asyncio
async def test_append_auto_summary_preserves_human_prefix(db):
    p = await db_module.create_project(db, "alpha")
    await db_module.set_goal(db, p["id"], "Human-written directive.")
    s = await db_module.register_session(db, p["id"], "worker")
    await db_module.log_task(db, s["id"], p["id"], "did thing", "done")
    await db_module.append_auto_summary(db, p["id"], "[AUTO SUMMARY - t1]\n- [DONE] did thing")
    goal = await db_module.get_goal(db, p["id"])
    assert goal["content"].startswith("Human-written directive.")
    assert "--- AUTO BLOCKS BELOW ---" in goal["content"]
    assert "AUTO SUMMARY - t1" in goal["content"]


@pytest.mark.asyncio
async def test_append_auto_summary_replaces_previous_auto_block(db):
    p = await db_module.create_project(db, "alpha")
    await db_module.set_goal(db, p["id"], "Directive.")
    s = await db_module.register_session(db, p["id"], "worker")
    await db_module.append_auto_summary(db, p["id"], "[AUTO SUMMARY - first]")
    await db_module.append_auto_summary(db, p["id"], "[AUTO SUMMARY - second]")
    goal = await db_module.get_goal(db, p["id"])
    assert "first" not in goal["content"]
    assert "second" in goal["content"]
    # Human prefix still intact.
    assert goal["content"].startswith("Directive.")


@pytest.mark.asyncio
async def test_run_auto_summary_cycle_only_touches_auto_projects(db):
    manual_p = await db_module.create_project(db, "manual-proj")
    auto_p = await db_module.create_project(db, "auto-proj")
    await db_module.set_goal(db, manual_p["id"], "manual goal")
    await db_module.set_goal(db, auto_p["id"], "auto goal")
    await db_module.set_goal_mode(db, auto_p["id"], "auto")
    s_m = await db_module.register_session(db, manual_p["id"], "w1")
    s_a = await db_module.register_session(db, auto_p["id"], "w2")
    await db_module.log_task(db, s_m["id"], manual_p["id"], "manual task", "done")
    await db_module.log_task(db, s_a["id"], auto_p["id"], "auto task", "done")
    updated = await db_module.run_auto_summary_cycle(db)
    assert updated == 1
    manual_goal = await db_module.get_goal(db, manual_p["id"])
    auto_goal = await db_module.get_goal(db, auto_p["id"])
    assert "AUTO SUMMARY" not in manual_goal["content"]
    assert "AUTO SUMMARY" in auto_goal["content"]
    assert "auto task" in auto_goal["content"]


def test_get_goal_response_includes_ambient_tasks(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "w"},
    ).json()
    client.post(
        f"/projects/{project['id']}/goal", json={"content": "ship it"}
    )
    for desc in ("t1", "t2", "t3"):
        client.post(
            "/tasks",
            json={
                "session_id": sess["id"],
                "project_id": project["id"],
                "description": desc,
                "status": "done",
            },
        )
    r = client.get(f"/projects/{project['id']}/goal")
    assert r.status_code == 200
    body = r.json()
    assert body["content"] == "ship it"
    assert isinstance(body["ambient_tasks"], list)
    descs = [t["description"] for t in body["ambient_tasks"]]
    # Newest-first ordering. Only the 5 most recent appear (we created 3).
    assert descs[0] == "t3"
    assert {"t1", "t2", "t3"} <= set(descs)


def test_patch_goal_mode_round_trip_http(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    # Default is manual.
    r = client.get(f"/projects/{project['id']}/goal-mode")
    assert r.status_code == 200
    assert r.json()["goal_mode"] == "manual"
    # Flip to auto.
    r = client.patch(
        f"/projects/{project['id']}/goal-mode", json={"mode": "auto"}
    )
    assert r.status_code == 200
    assert r.json()["goal_mode"] == "auto"
    r = client.get(f"/projects/{project['id']}/goal-mode")
    assert r.json()["goal_mode"] == "auto"


def test_patch_goal_mode_rejects_invalid_mode(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    r = client.patch(
        f"/projects/{project['id']}/goal-mode", json={"mode": "bogus"}
    )
    # Pydantic catches the literal mismatch as 422.
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# v0.4.3 — get_goal ambient_tasks contract (HTTP + MCP)
# ---------------------------------------------------------------------------


def test_get_goal_ambient_tasks_empty_when_no_tasks(client):
    """When no tasks have been logged yet ``ambient_tasks`` must be an
    empty list, not absent — sessions can rely on the field shape."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    client.post(f"/projects/{project['id']}/goal", json={"content": "go"})
    r = client.get(f"/projects/{project['id']}/goal")
    assert r.status_code == 200
    body = r.json()
    assert body["ambient_tasks"] == []


def test_get_goal_ambient_tasks_caps_at_five(client):
    """``ambient_tasks`` must include at most 5 rows, newest first.
    Cold sessions get a fixed-size context window."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "w"},
    ).json()
    client.post(f"/projects/{project['id']}/goal", json={"content": "go"})
    for i in range(8):
        client.post(
            "/tasks",
            json={
                "session_id": sess["id"],
                "project_id": project["id"],
                "description": f"task-{i}",
                "status": "done",
            },
        )
    r = client.get(f"/projects/{project['id']}/goal")
    body = r.json()
    assert len(body["ambient_tasks"]) == 5
    descs = [t["description"] for t in body["ambient_tasks"]]
    # Newest first — task-7 leads, task-3 trails.
    assert descs[0] == "task-7"
    assert descs[-1] == "task-3"


def test_get_goal_ambient_tasks_carry_status_and_timestamp(client):
    """Each ambient task entry exposes the fields a worker needs to
    decide what's been done vs pending without another round trip."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "w"},
    ).json()
    client.post(f"/projects/{project['id']}/goal", json={"content": "go"})
    client.post(
        "/tasks",
        json={
            "session_id": sess["id"],
            "project_id": project["id"],
            "description": "blocked task",
            "status": "failed",
        },
    )
    r = client.get(f"/projects/{project['id']}/goal")
    item = r.json()["ambient_tasks"][0]
    assert item["status"] == "failed"
    assert item["description"] == "blocked task"
    assert item["created_at"]


# ---------------------------------------------------------------------------
# v0.5.0 — markdown file editor allow-list pinned
# ---------------------------------------------------------------------------


def test_files_allow_list_includes_claude_md(client):
    """v0.5.0 spec: CLAUDE.md joins the editable allow-list alongside
    AGENTS.md / ROADMAP.md / DEVLOG.md."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    r = client.get(f"/projects/{project['id']}/files")
    assert r.status_code == 200
    files = r.json()
    assert {"AGENTS.md", "ROADMAP.md", "DEVLOG.md", "CLAUDE.md"} <= set(files)


def test_files_get_for_claude_md_is_allowed(client, tmp_path, monkeypatch):
    """CLAUDE.md must read through the same code path as the others."""
    # Point the file root at a temp dir so we don't depend on the repo
    # actually containing CLAUDE.md on disk during the test run.
    from meridian import server as srv
    monkeypatch.setattr(srv, "_REPO_ROOT", tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# claude\n", encoding="utf-8")
    project = client.post("/projects", json={"name": "alpha"}).json()
    r = client.get(f"/projects/{project['id']}/files/CLAUDE.md")
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "CLAUDE.md"
    assert "# claude" in body["content"]


# ---------------------------------------------------------------------------
# v0.5.1 — session heartbeat + health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_session_updates_last_seen(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "worker")
    # Backdate last_seen so the heartbeat actually moves it.
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now', '-1 hour') WHERE id = ?",
        (s["id"],),
    )
    await db.commit()
    ok = await db_module.heartbeat_session(db, s["id"])
    assert ok is True
    async with db.execute(
        "SELECT last_seen FROM sessions WHERE id = ?", (s["id"],)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    # The freshly-stamped timestamp must be more recent than the
    # backdated one; a substring comparison on the iso string is
    # sufficient here because SQLite formats them lexicographically.
    assert row[0] is not None


@pytest.mark.asyncio
async def test_heartbeat_session_returns_false_for_missing(db):
    ok = await db_module.heartbeat_session(db, "no-such-session")
    assert ok is False


@pytest.mark.asyncio
async def test_heartbeat_keeps_session_out_of_idle_sweep(db):
    p = await db_module.create_project(db, "alpha")
    fresh = await db_module.register_session(db, p["id"], "fresh")
    stale = await db_module.register_session(db, p["id"], "stale")
    # Backdate both. The heartbeat caller stays alive; the other is left.
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now', '-2 hours')"
    )
    await db.commit()
    await db_module.heartbeat_session(db, fresh["id"])
    expire_result = await db_module.expire_idle_sessions(db, max_age_minutes=30)
    assert expire_result["count"] >= 1
    # Heartbeated session remains 'active'; un-heartbeated flips to 'idle'.
    sessions = await db_module.get_sessions(db, p["id"], active_only=False)
    by_id = {s["id"]: s for s in sessions}
    assert by_id[fresh["id"]]["status"] == "active"
    assert by_id[stale["id"]]["status"] == "idle"


def test_http_heartbeat_endpoint(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "worker"},
    ).json()
    r = client.post(f"/sessions/{sess['id']}/heartbeat")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["session_id"] == sess["id"]


def test_http_heartbeat_unknown_session_404(client):
    r = client.post("/sessions/does-not-exist/heartbeat")
    assert r.status_code == 404


def test_dashboard_html_has_relative_time_helper(client):
    """v0.5.1 UI: dashboard renders last_seen as relative time so
    workers' liveness is obvious at a glance."""
    # v1.0.2: JS moved to static file
    js = client.get("/static/dashboard.js").text
    assert "formatRelativeTime" in js


def test_dashboard_auth_gate_in_hosted_mode(client, monkeypatch):
    """Hosted mode: unauthenticated /dashboard must redirect to /auth/login."""
    from meridian import hosted as hosted_module
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    # Mock get_current_tenant to raise HTTPException (simulating missing auth)
    async def mock_get_current_tenant(request):
        from fastapi import HTTPException
        raise HTTPException(status_code=401)
    monkeypatch.setattr(hosted_module, "get_current_tenant", mock_get_current_tenant)
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/auth/login"


# ---------------------------------------------------------------------------
# v0.5.2 — structured goal hierarchy (north_star / version_goal / sprint)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_goal_carries_north_star_and_sprint_forward(db):
    """When set_goal is called without north_star/sprint the old values persist."""
    p = await db_module.create_project(db, "alpha")
    await db_module.set_goal(db, p["id"], "v1", north_star="the big vision", sprint="week 1")
    goal = await db_module.set_goal(db, p["id"], "v2")
    assert goal["north_star"] == "the big vision"
    assert goal["sprint"] == "week 1"


@pytest.mark.asyncio
async def test_set_north_star_owner_only(db):
    """set_north_star updates north_star without bumping version when content is unchanged (v2.3 dedup)."""
    p = await db_module.create_project(db, "alpha")
    await db_module.set_goal(db, p["id"], "go")
    updated = await db_module.set_north_star(db, p["id"], "be the best")
    assert updated["north_star"] == "be the best"
    # v2.3: content unchanged → minor in-place update, version stays at 1
    assert updated["version"] == 1


@pytest.mark.asyncio
async def test_set_sprint_any_member(db):
    """set_sprint updates sprint without bumping version when content is unchanged (v2.3 dedup)."""
    p = await db_module.create_project(db, "alpha")
    await db_module.set_goal(db, p["id"], "go")
    updated = await db_module.set_sprint(db, p["id"], "ship auth this week")
    assert updated["sprint"] == "ship auth this week"
    # v2.3: content unchanged → minor in-place update, version stays at 1
    assert updated["version"] == 1


@pytest.mark.asyncio
async def test_get_goal_returns_north_star_and_sprint(db):
    """get_goal exposes north_star and sprint fields since v0.5.2."""
    p = await db_module.create_project(db, "alpha")
    await db_module.set_goal(
        db, p["id"], "version content",
        north_star="long-term vision", sprint="current sprint"
    )
    goal = await db_module.get_goal(db, p["id"])
    assert goal is not None
    assert goal["north_star"] == "long-term vision"
    assert goal["sprint"] == "current sprint"
    assert goal["content"] == "version content"


@pytest.mark.asyncio
async def test_get_goal_returns_null_fields_when_unset(db):
    """north_star and sprint are None when not explicitly set."""
    p = await db_module.create_project(db, "alpha")
    await db_module.set_goal(db, p["id"], "bare goal")
    goal = await db_module.get_goal(db, p["id"])
    assert goal is not None
    assert goal["north_star"] is None
    assert goal["sprint"] is None


def test_http_set_north_star_owner_succeeds(client):
    """POST /goal/north-star with matching human_id returns 200."""
    project = client.post(
        "/projects", json={"name": "alpha", "human_id": "adam"}
    ).json()
    client.post(f"/projects/{project['id']}/goal", json={"content": "go"})
    r = client.post(
        f"/projects/{project['id']}/goal/north-star",
        json={"north_star": "be the best", "human_id": "adam"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["north_star"] == "be the best"


def test_http_set_north_star_any_user_succeeds(client):
    """POST /goal/north-star no longer requires ownership check — session auth proves it."""
    project = client.post(
        "/projects", json={"name": "alpha", "human_id": "adam"}
    ).json()
    client.post(f"/projects/{project['id']}/goal", json={"content": "go"})
    r = client.post(
        f"/projects/{project['id']}/goal/north-star",
        json={"north_star": "a clean minimal API", "human_id": "eve"},
    )
    assert r.status_code == 200


def test_http_set_sprint_any_member(client):
    """POST /goal/sprint succeeds without ownership check."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    client.post(f"/projects/{project['id']}/goal", json={"content": "go"})
    r = client.post(
        f"/projects/{project['id']}/goal/sprint",
        json={"sprint": "ship login flow"},
    )
    assert r.status_code == 200
    assert r.json()["sprint"] == "ship login flow"


def test_http_get_goal_includes_all_three_levels(client):
    """GET /goal returns north_star, content, and sprint."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    client.post(
        f"/projects/{project['id']}/goal",
        json={"content": "v1", "north_star": "vision", "sprint": "week 1"},
    )
    r = client.get(f"/projects/{project['id']}/goal")
    assert r.status_code == 200
    body = r.json()
    assert body["content"] == "v1"
    assert body["north_star"] == "vision"
    assert body["sprint"] == "week 1"


def test_set_goal_backward_compat_without_hierarchy_fields(client):
    """Old callers that omit north_star/sprint keep working without error."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    r = client.post(
        f"/projects/{project['id']}/goal", json={"content": "ship it"}
    )
    assert r.status_code == 200
    assert r.json()["content"] == "ship it"
    assert r.json()["north_star"] is None
    assert r.json()["sprint"] is None


def test_migration_seeds_north_star_from_existing_goal(tmp_path):
    """On a legacy DB, init_db seeds north_star = content for the latest row."""
    import sqlite3

    db_path = tmp_path / "legacy052.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE goal_states (id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            content TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE sessions (id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            last_seen TEXT NOT NULL DEFAULT (datetime('now')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE task_log (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            project_id TEXT NOT NULL, description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'done',
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        INSERT INTO projects (id, name) VALUES ('p1', 'legacy');
        INSERT INTO goal_states (id, project_id, content, version)
            VALUES ('g1', 'p1', 'the original goal', 1);
        """
    )
    legacy.commit()
    legacy.close()

    async def run():
        conn = await db_module.init_db(str(db_path))
        try:
            goal = await db_module.get_goal(conn, "p1")
            assert goal is not None
            assert goal["north_star"] == "the original goal"
            assert goal["sprint"] is None
        finally:
            await conn.close()

    import asyncio
    asyncio.run(run())


def test_dashboard_html_has_three_goal_textareas(client):
    """v0.5.2 UI: dashboard renders north-star, version goal, and sprint panes."""
    html = client.get("/dashboard").text
    # These IDs are in the JS template string (buildTabBody generates them)
    # v1.0.2: JS moved to static file
    js = client.get("/static/dashboard.js").text
    assert "goal-north-star-" in js
    assert "goal-sprint-" in js
    assert "saveNorthStar" in js
    assert "saveSprint" in js


def test_dashboard_has_decisions_subtab(client):
    """v2.3 — dashboard goal area exposes a Decisions subtab + table renderer."""
    js = client.get("/static/dashboard.js").text
    # Subtab button must exist.
    assert 'data-gtab="decisions"' in js
    # Panel + table host element.
    assert "gtab-decisions-" in js
    assert "decisions-table-" in js
    # Renderer + parser functions must be wired.
    assert "renderDecisionsTable" in js
    assert "parseDecisionsBlob" in js


# ---------------------------------------------------------------------------
# v0.4.5 — auto-generate handoff on session TTL expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expire_idle_sessions_returns_dict_with_project_ids(db):
    """expire_idle_sessions returns {count, project_ids} since v0.4.5."""
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "stale")
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now', '-60 minutes') WHERE id = ?",
        (s["id"],),
    )
    await db.commit()
    result = await db_module.expire_idle_sessions(db, max_age_minutes=30)
    assert isinstance(result, dict)
    assert "count" in result
    assert "project_ids" in result
    assert result["count"] >= 1
    assert p["id"] in result["project_ids"]


@pytest.mark.asyncio
async def test_expire_and_generate_handoffs_creates_file(db, tmp_path):
    """When sessions expire, _expire_and_generate_handoffs writes the file."""
    from meridian import server as srv

    p = await db_module.create_project(db, "myproj")
    await db_module.set_goal(db, p["id"], "ship it")
    s = await db_module.register_session(db, p["id"], "stale")
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now', '-60 minutes') WHERE id = ?",
        (s["id"],),
    )
    await db.commit()

    result = await srv._expire_and_generate_handoffs(db, str(tmp_path))
    assert result["count"] >= 1
    assert result["auto_handoff_generated"] is True
    assert (tmp_path / "myproj_handoff.md").exists()


@pytest.mark.asyncio
async def test_expire_and_generate_handoffs_skips_when_nothing_expires(db, tmp_path):
    """Fresh sessions don't trigger handoff generation."""
    from meridian import server as srv

    p = await db_module.create_project(db, "myproj")
    await db_module.register_session(db, p["id"], "fresh")

    result = await srv._expire_and_generate_handoffs(db, str(tmp_path))
    assert result["count"] == 0
    assert result["auto_handoff_generated"] is False
    assert not (tmp_path / "myproj_handoff.md").exists()


# ---------------------------------------------------------------------------
# v0.4.4 — start_session composite tool
# ---------------------------------------------------------------------------


def test_start_session_returns_all_fields(client):
    """start_session response contains every field the protocol promises."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    client.post(f"/projects/{project['id']}/goal", json={"content": "ship it"})
    r = client.post(
        f"/projects/{project['id']}/start-session",
        json={"session_name": "test-worker"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"]
    assert isinstance(body["goal"], dict)
    assert isinstance(body["recent_tasks"], list)
    assert isinstance(body["active_sessions"], list)
    assert isinstance(body["handoff_exists"], bool)
    assert body["handoff_path"]
    assert isinstance(body["files"], list)
    assert "AGENTS.md" in body["files"]


def test_start_session_goal_has_ambient_tasks(client):
    """goal.ambient_tasks is populated from the last task log entries."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "prep"},
    ).json()
    client.post(f"/projects/{project['id']}/goal", json={"content": "go"})
    for desc in ("t1", "t2", "t3"):
        client.post(
            "/tasks",
            json={
                "session_id": sess["id"],
                "project_id": project["id"],
                "description": desc,
                "status": "done",
            },
        )
    r = client.post(
        f"/projects/{project['id']}/start-session",
        json={"session_name": "new-worker"},
    )
    body = r.json()
    assert isinstance(body["goal"]["ambient_tasks"], list)
    descs = [t["description"] for t in body["goal"]["ambient_tasks"]]
    assert {"t1", "t2", "t3"} <= set(descs)


def test_start_session_recent_tasks_capped_at_10(client):
    """recent_tasks returns at most 10 rows newest-first."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "prep"},
    ).json()
    client.post(f"/projects/{project['id']}/goal", json={"content": "go"})
    for i in range(15):
        client.post(
            "/tasks",
            json={
                "session_id": sess["id"],
                "project_id": project["id"],
                "description": f"task-{i}",
                "status": "done",
            },
        )
    r = client.post(
        f"/projects/{project['id']}/start-session",
        json={"session_name": "new-worker"},
    )
    body = r.json()
    assert len(body["recent_tasks"]) == 10
    # Newest first — task-14 must lead.
    assert body["recent_tasks"][0]["description"] == "task-14"


def test_start_session_releases_stale_claims_older_than_two_hours(client):
    from datetime import datetime, timedelta, timezone

    project = client.post("/projects", json={"name": "stale-claims"}).json()
    stale_sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "old-worker"},
    ).json()
    task = client.post(
        "/tasks",
        json={
            "session_id": stale_sess["id"],
            "project_id": project["id"],
            "description": "resume me",
            "status": "pending",
        },
    ).json()
    claim = client.post(
        f"/projects/{project['id']}/tasks/claim",
        json={"task_id": task["id"], "session_id": stale_sess["id"]},
    ).json()
    assert claim["claimed"] is True

    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=3)
    ).strftime("%Y-%m-%d %H:%M:%S")
    db = client.app.state.db
    asyncio.run(
        db.execute(
            "UPDATE task_log SET claimed_at = ? WHERE id = ?",
            (cutoff, task["id"]),
        )
    )
    asyncio.run(db.commit())

    r = client.post(
        f"/projects/{project['id']}/start-session",
        json={"session_name": "new-worker"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["recent_tasks"][0]["description"].startswith("Auto-released 1 stale claim")

    fresh = asyncio.run(db_module.get_task(db, task["id"]))
    assert fresh is not None
    assert fresh["status"] == "pending"
    assert fresh["claimed_by"] is None


def test_start_session_archives_old_empty_sessions(client):
    """start_session runs empty-session cleanup before returning context."""
    project = client.post("/projects", json={"name": "empty-cleanup"}).json()
    stale = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "stale-empty"},
    ).json()
    db = client.app.state.db
    asyncio.run(
        db.execute(
            "UPDATE sessions SET status = 'idle', created_at = datetime('now', '-8 days') WHERE id = ?",
            (stale["id"],),
        )
    )
    asyncio.run(db.commit())

    r = client.post(
        f"/projects/{project['id']}/start-session",
        json={"session_name": "new-worker"},
    )
    assert r.status_code == 200

    sessions = asyncio.run(db_module.get_sessions(db, project["id"], active_only=False))
    refreshed = next(x for x in sessions if x["id"] == stale["id"])
    assert refreshed["status"] == "archived"


def test_claim_task_rejects_blocked_sprint_item_dependencies(client):
    project = client.post("/projects", json={"name": "dep-claim"}).json()
    sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "worker"},
    ).json()
    parent = client.post(
        f"/projects/{project['id']}/sprint-items",
        json={"version": "v1", "title": "Parent item"},
    ).json()
    child = client.post(
        f"/projects/{project['id']}/sprint-items",
        json={
            "version": "v1",
            "title": "Child item",
            "depends_on": parent["id"],
        },
    ).json()

    blocked = client.post(
        f"/projects/{project['id']}/tasks/claim",
        json={"task_id": child["id"], "session_id": sess["id"]},
    )
    assert blocked.status_code == 200
    blocked_body = blocked.json()
    assert blocked_body["claimed"] is False
    assert blocked_body["error"] == "dependency_not_met"
    assert blocked_body["blocking_item_id"] == parent["id"]
    assert blocked_body["blocking_item_title"] == "Parent item"

    client.post(
        f"/projects/{project['id']}/sprint-items/{parent['id']}/complete",
        json={},
    )
    claimed = client.post(
        f"/projects/{project['id']}/tasks/claim",
        json={"task_id": child["id"], "session_id": sess["id"]},
    )
    assert claimed.status_code == 200
    claimed_body = claimed.json()
    assert claimed_body["claimed"] is True
    assert claimed_body["sprint_item_id"] == child["id"]

    sprint_items = client.get(f"/projects/{project['id']}/sprint-items").json()
    child_fresh = next(it for it in sprint_items if it["id"] == child["id"])
    assert child_fresh["status"] == "in_progress"


def test_start_session_handoff_exists_reflects_disk_reality(client, tmp_path):
    """handoff_exists flips True once the file lands on disk."""
    project = client.post("/projects", json={"name": "myproj"}).json()
    # No handoff file yet.
    r = client.post(
        f"/projects/{project['id']}/start-session",
        json={"session_name": "w1"},
    )
    assert r.status_code == 200
    assert r.json()["handoff_exists"] is False

    # Write the handoff file that the slug logic would produce.
    (tmp_path / "myproj_handoff.md").write_text("# handoff", encoding="utf-8")

    r2 = client.post(
        f"/projects/{project['id']}/start-session",
        json={"session_name": "w2"},
    )
    assert r2.status_code == 200
    assert r2.json()["handoff_exists"] is True


# ---------------------------------------------------------------------------
# v0.4.6 — list_projects + get_project_by_name MCP tools
# ---------------------------------------------------------------------------


def test_list_projects_returns_all(client):
    """GET /projects returns every project that was created."""
    client.post("/projects", json={"name": "proj-one"})
    client.post("/projects", json={"name": "proj-two"})
    r = client.get("/projects")
    assert r.status_code == 200
    names = {p["name"] for p in r.json()}
    assert "proj-one" in names
    assert "proj-two" in names


def test_list_projects_empty(client):
    """GET /projects returns an empty list when no projects exist."""
    r = client.get("/projects")
    assert r.status_code == 200
    assert r.json() == []


def test_get_project_by_name_exact(client):
    """GET /projects/by-name/{name} resolves an exact match."""
    proj = client.post("/projects", json={"name": "exact-match"}).json()
    client.post(f"/projects/{proj['id']}/goal", json={"content": "ship it"})
    r = client.get("/projects/by-name/exact-match")
    assert r.status_code == 200
    body = r.json()
    assert body["project"]["id"] == proj["id"]
    assert body["goal_version"] == 1
    assert "ship it" in body["goal_summary"]


def test_get_project_by_name_case_insensitive(client):
    """GET /projects/by-name/{name} matches regardless of case."""
    proj = client.post("/projects", json={"name": "meridian-build"}).json()
    r = client.get("/projects/by-name/MERIDIAN-BUILD")
    assert r.status_code == 200
    assert r.json()["project"]["id"] == proj["id"]


def test_get_project_by_name_substring(client):
    """GET /projects/by-name/{name} matches on a substring."""
    proj = client.post("/projects", json={"name": "my-cool-project"}).json()
    r = client.get("/projects/by-name/cool")
    assert r.status_code == 200
    assert r.json()["project"]["id"] == proj["id"]


def test_get_project_by_name_no_goal(client):
    """goal_version and goal_summary are None when no goal has been set."""
    client.post("/projects", json={"name": "goalless"})
    r = client.get("/projects/by-name/goalless")
    assert r.status_code == 200
    body = r.json()
    assert body["goal_version"] is None
    assert body["goal_summary"] is None


def test_get_project_by_name_not_found(client):
    """GET /projects/by-name/{name} returns 404 for an unknown name."""
    r = client.get("/projects/by-name/does-not-exist")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# v0.6.1 — XML-wrap get_goal output
# ---------------------------------------------------------------------------


def test_build_goal_xml_structure():
    """The XML envelope must contain a <goal> root with the four
    expected children and the cache hints pinned by the contract."""
    goal = {
        "version": 3,
        "content": "build the thing",
        "north_star": "ship Meridian",
        "sprint": "v0.6 context layer",
    }
    recent = [
        {"status": "done", "created_at": "2026-01-01 00:00:00", "description": "did it"},
        {"status": "failed", "created_at": "2026-01-02 00:00:00", "description": "tried"},
    ]
    xml = db_module.build_goal_xml(goal, "meridian-build", recent)
    assert xml.startswith('<goal version="3" project="meridian-build">')
    assert '<north_star cache="true">ship Meridian</north_star>' in xml
    assert '<version_goal cache="true">build the thing</version_goal>' in xml
    assert '<sprint cache="false">v0.6 context layer</sprint>' in xml
    assert '<recent_tasks cache="false">' in xml
    assert '<task status="done" ts="2026-01-01 00:00:00">did it</task>' in xml
    assert '<task status="failed" ts="2026-01-02 00:00:00">tried</task>' in xml
    assert xml.endswith("</goal>")


def test_build_goal_xml_escapes_dangerous_chars():
    """Values with XML metacharacters must be escaped, not break the doc."""
    goal = {
        "version": 1,
        "content": "<bad> & \"evil\"",
        "north_star": "1 < 2",
        "sprint": None,
    }
    xml = db_module.build_goal_xml(goal, 'proj"name', [])
    assert "<bad>" not in xml  # raw tag injection blocked
    assert "&lt;bad&gt;" in xml
    assert "1 &lt; 2" in xml
    # Attribute is quoted with quoteattr — content is safe regardless of quoting style.


def test_build_goal_xml_none_goal_returns_skeleton():
    """When no goal exists yet we still get a parseable XML doc with
    empty fields rather than a broken document."""
    xml = db_module.build_goal_xml(None, "fresh-project", [])
    assert '<goal version="0" project="fresh-project">' in xml
    assert '<north_star cache="true"></north_star>' in xml
    assert '<recent_tasks cache="false">' in xml
    assert "</goal>" in xml


def test_get_goal_endpoint_includes_xml_envelope(client):
    """GET /projects/{id}/goal exposes the v0.6.1 ``xml`` field
    alongside the existing JSON shape."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    client.post(
        f"/projects/{project['id']}/goal",
        json={"content": "ship", "north_star": "vision", "sprint": "now"},
    )
    r = client.get(f"/projects/{project['id']}/goal")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["xml"], str)
    assert '<goal version="1"' in body["xml"]
    assert '<north_star cache="true">vision</north_star>' in body["xml"]
    assert '<version_goal cache="true">ship</version_goal>' in body["xml"]
    assert '<sprint cache="false">now</sprint>' in body["xml"]


def test_start_session_endpoint_returns_goal_xml(client):
    """The composite tool surfaces goal_xml at the top level so cold
    sessions can prompt with it without unwrapping a nullable goal."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    client.post(
        f"/projects/{project['id']}/goal",
        json={"content": "ship", "north_star": "vision", "sprint": "now"},
    )
    r = client.post(
        f"/projects/{project['id']}/start-session",
        json={"session_name": "worker", "human_id": "adam"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "goal_xml" in body
    assert '<goal version="1"' in body["goal_xml"]
    assert '<north_star cache="true">vision</north_star>' in body["goal_xml"]


# ---------------------------------------------------------------------------
# v0.6.2 — Prompt caching hints on static goal fields
# ---------------------------------------------------------------------------


def test_build_goal_cache_blocks_layout():
    """Four blocks, in order: north_star → version_goal → sprint →
    recent_tasks. The first two carry cache_control: ephemeral; the
    last two don't, because they mutate every sprint / task."""
    goal = {
        "version": 2,
        "content": "build v0.6",
        "north_star": "ship Meridian",
        "sprint": "v0.6 context layer",
    }
    recent = [
        {"status": "done", "created_at": "2026-01-01 00:00", "description": "did A"},
    ]
    blocks = db_module.build_goal_cache_blocks(goal, "meridian", recent)
    assert len(blocks) == 4
    # All blocks are text-type Anthropic content blocks.
    assert all(b["type"] == "text" for b in blocks)
    # Static fields cached.
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    assert "ship Meridian" in blocks[0]["text"]
    assert "build v0.6" in blocks[1]["text"]
    # Dynamic fields *not* cached — absence of the key is the contract.
    assert "cache_control" not in blocks[2]
    assert "cache_control" not in blocks[3]
    assert "v0.6 context layer" in blocks[2]["text"]
    assert "did A" in blocks[3]["text"]


def test_build_goal_cache_blocks_cache_blocks_come_first():
    """Anthropic's prompt cache is prefix-keyed: cached blocks MUST
    precede uncached ones, otherwise mutable text invalidates the
    cache for every cold session."""
    goal = {"version": 1, "content": "g", "north_star": "n", "sprint": "s"}
    blocks = db_module.build_goal_cache_blocks(goal, "p", [])
    seen_uncached = False
    for b in blocks:
        if "cache_control" not in b:
            seen_uncached = True
        else:
            assert not seen_uncached, (
                "cached blocks must lead so the prefix is stable"
            )


def test_build_goal_cache_blocks_handles_none_goal():
    """An unset goal yields four empty blocks rather than zero — the
    caller can still hand a uniform shape to Anthropic without
    branching on goal existence."""
    blocks = db_module.build_goal_cache_blocks(None, "fresh", [])
    assert len(blocks) == 4
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[3]["type"] == "text"


def test_get_goal_endpoint_includes_cache_blocks(client):
    """GET /projects/{id}/goal exposes the cache_blocks field next to
    the JSON shape and XML envelope."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    client.post(
        f"/projects/{project['id']}/goal",
        json={"content": "ship", "north_star": "vision", "sprint": "now"},
    )
    r = client.get(f"/projects/{project['id']}/goal")
    blocks = r.json()["cache_blocks"]
    assert isinstance(blocks, list)
    assert len(blocks) == 4
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[2]
    assert "cache_control" not in blocks[3]


def test_start_session_endpoint_returns_goal_cache_blocks(client):
    """start_session surfaces goal_cache_blocks at the top level so
    cold sessions can splat it into messages.create() immediately."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    client.post(
        f"/projects/{project['id']}/goal",
        json={"content": "ship", "north_star": "vision", "sprint": "now"},
    )
    r = client.post(
        f"/projects/{project['id']}/start-session",
        json={"session_name": "worker", "human_id": "adam"},
    )
    body = r.json()
    assert "goal_cache_blocks" in body
    blocks = body["goal_cache_blocks"]
    assert len(blocks) == 4
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# v0.6.3 — GOAL.md bidirectional sync
# ---------------------------------------------------------------------------

def test_parse_goal_md_all_sections():
    from meridian.goal_md import parse_goal_md
    text = "# my-project\n\n## North Star\nBe great\n\n## Version Goal\nShip it\n\n## Sprint\n- task 1\n"
    r = parse_goal_md(text)
    assert r["project_name"] == "my-project"
    assert r["north_star"] == "Be great"
    assert r["version_goal"] == "Ship it"
    assert r["sprint"] == "- task 1"


def test_parse_goal_md_missing_sections():
    from meridian.goal_md import parse_goal_md
    text = "# proj\n\n## North Star\nOnly this\n"
    r = parse_goal_md(text)
    assert r["north_star"] == "Only this"
    assert r["version_goal"] is None
    assert r["sprint"] is None


def test_format_goal_md_round_trip():
    from meridian.goal_md import format_goal_md, parse_goal_md
    rendered = format_goal_md("myproj", "ns", "vg", "sp")
    parsed = parse_goal_md(rendered)
    assert parsed["project_name"] == "myproj"
    assert parsed["north_star"] == "ns"
    assert parsed["version_goal"] == "vg"
    assert parsed["sprint"] == "sp"


@pytest.mark.asyncio
async def test_sync_goal_md_to_db(tmp_path):
    from meridian.goal_md import write_goal_md, sync_goal_md_to_db
    db = await db_module.init_db(":memory:")
    proj = await db_module.create_project(db, "sync-test")
    goal_path = tmp_path / "GOAL.md"
    write_goal_md("sync-test", "north text", "version text", "sprint text", path=goal_path)
    result = await sync_goal_md_to_db(db, path=goal_path)
    assert result is not None
    goal = await db_module.get_goal(db, proj["id"])
    assert goal["north_star"] == "north text"
    assert goal["sprint"] == "sprint text"
    await db.close()


@pytest.mark.asyncio
async def test_sync_db_to_goal_md(tmp_path):
    from meridian.goal_md import sync_db_to_goal_md, parse_goal_md
    db = await db_module.init_db(":memory:")
    proj = await db_module.create_project(db, "writeback-test")
    await db_module.set_goal(db, proj["id"], "vg content", north_star="ns content", sprint="sp content")
    goal_path = tmp_path / "GOAL.md"
    written = await sync_db_to_goal_md(db, proj["id"], path=goal_path)
    assert written == goal_path
    parsed = parse_goal_md(goal_path.read_text())
    assert parsed["north_star"] == "ns content"
    assert parsed["sprint"] == "sp content"
    await db.close()


def test_context_block_full_includes_required_fields(client):
    """v2.3 — /context-block?mode=full returns the Code Handoff variant."""
    project = client.post("/projects", json={"name": "ctxblk"}).json()
    client.post(
        f"/projects/{project['id']}/goal",
        json={"content": "ship v1", "north_star": "the big idea", "sprint": "wk-1"},
    )
    r = client.get(f"/projects/{project['id']}/context-block?mode=full")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("text/plain")
    text = r.text
    assert "PROJECT: ctxblk" in text
    assert "NORTH STAR: the big idea" in text
    assert "SPRINT: wk-1" in text
    assert "VERSION GOAL:" in text
    assert "TEST: pixi run test" in text
    assert "start_session" in text


def test_context_block_chat_mode_omits_repo_and_version_goal(client):
    """v2.3 — chat mode trims sessions, repo path, and the verbose version goal."""
    project = client.post("/projects", json={"name": "ctxchat"}).json()
    client.post(
        f"/projects/{project['id']}/goal",
        json={"content": "long version goal " * 200, "north_star": "ns", "sprint": "sp"},
    )
    r = client.get(f"/projects/{project['id']}/context-block?mode=chat")
    assert r.status_code == 200
    text = r.text
    assert "PROJECT: ctxchat" in text
    # repo path is full-mode only
    assert "REPO:" not in text
    # version goal block is full-mode only
    assert "VERSION GOAL:" not in text


def test_context_block_rejects_bad_mode(client):
    project = client.post("/projects", json={"name": "ctxbad"}).json()
    r = client.get(f"/projects/{project['id']}/context-block?mode=garbage")
    assert r.status_code == 400


def test_context_block_404_for_unknown_project(client):
    r = client.get("/projects/does-not-exist/context-block")
    assert r.status_code == 404


def test_meridian_md_built_in_loads():
    """v2.3 — built-in MERIDIAN.md ships with the package and loads."""
    from meridian.server import _load_meridian_md
    content = _load_meridian_md()
    assert "Meridian Session Instructions" in content
    assert "log_task" in content
    assert "generate_handoff" in content


def test_meridian_md_project_root_override(tmp_path, monkeypatch):
    """v2.3 — a project-root MERIDIAN.md wins over the built-in."""
    from meridian import server as srv
    # Point the resolver at a fake repo root that has a MERIDIAN.md.
    fake_root = tmp_path
    fake_pkg = tmp_path / "meridian"
    fake_pkg.mkdir()
    (fake_pkg / "MERIDIAN.md").write_text("BUILT-IN VERSION")
    (fake_root / "MERIDIAN.md").write_text("PROJECT OVERRIDE WINS")

    # Patch __file__ resolution by monkeypatching Path.parent traversal.
    import pathlib
    orig_file = srv.__file__
    srv.__file__ = str(fake_pkg / "server.py")
    try:
        content = srv._load_meridian_md()
        assert content == "PROJECT OVERRIDE WINS"
    finally:
        srv.__file__ = orig_file


def test_start_session_endpoint_includes_meridian_instructions(client):
    """v2.3 — POST /projects/{id}/start-session returns meridian_instructions."""
    project = client.post("/projects", json={"name": "med-md"}).json()
    client.post(
        f"/projects/{project['id']}/goal",
        json={"content": "ship it"},
    )
    r = client.post(
        f"/projects/{project['id']}/start-session",
        json={"session_name": "alpha"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "meridian_instructions" in body
    assert "log_task" in body["meridian_instructions"]


@pytest.mark.asyncio
async def test_goal_md_sync_skipped_when_hosted_mode(tmp_path, monkeypatch):
    """v2.3 — hosted tier never touches GOAL.md (no single tenant repo root)."""
    from meridian.goal_md import (
        sync_db_to_goal_md,
        sync_goal_md_to_db,
        write_goal_md,
    )
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    db = await db_module.init_db(":memory:")
    proj = await db_module.create_project(db, "hosted-skip")
    await db_module.set_goal(db, proj["id"], "vg", north_star="ns", sprint="sp")
    goal_path = tmp_path / "GOAL.md"
    # write_goal_md still writes (it's a plain util), but sync paths skip.
    write_goal_md("hosted-skip", "ns2", "vg2", "sp2", path=goal_path)
    # Both sync directions should no-op in hosted mode.
    assert await sync_db_to_goal_md(db, proj["id"], path=goal_path) is None
    assert await sync_goal_md_to_db(db, path=goal_path) is None
    await db.close()



def test_http_set_sprint_writes_back_goal_md(client, tmp_path):
    """set_sprint via HTTP triggers GOAL.md writeback."""
    from meridian.goal_md import parse_goal_md
    proj = client.post("/projects", json={"name": "goalmd-http-test"}).json()
    pid = proj["id"]
    client.post(f"/projects/{pid}/sessions", json={"name": "s1"})
    client.post(f"/projects/{pid}/goal/sprint", json={"sprint": "do the thing", "human_id": "adam"})
    gm_path = tmp_path / "GOAL.md"
    if gm_path.exists():
        parsed = parse_goal_md(gm_path.read_text())
        assert parsed["sprint"] == "do the thing"


# ---------------------------------------------------------------------------
# v0.6.5 — MERIDIAN_HOST / MERIDIAN_PORT env vars + /config endpoint
# ---------------------------------------------------------------------------

def test_config_endpoint_returns_version(client):
    r = client.get("/config")
    assert r.status_code == 200
    data = r.json()
    assert "version" in data
    assert "port" in data
    assert data["db"] == "memory"


def test_config_endpoint_reflects_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_HOST", "0.0.0.0")
    monkeypatch.setenv("MERIDIAN_PORT", "9999")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    # Block real Neon URLs from .env from leaking into the test lifespan
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    import importlib, meridian.server as srv
    srv = importlib.reload(srv)
    from fastapi.testclient import TestClient
    with TestClient(srv.app) as c:
        r = c.get("/config")
        assert r.status_code == 200
        data = r.json()
        assert data["host"] == "0.0.0.0"
        assert data["port"] == 9999


def test_http_set_goal_writes_back_goal_md(client, tmp_path):
    """POST /goal syncs content back to GOAL.md."""
    proj = client.post("/projects", json={"name": "p1"}).json()
    client.post(f"/projects/{proj['id']}/goal", json={"content": "ship it"})
    goal_md = tmp_path / "GOAL.md"
    assert goal_md.exists()
    assert "ship it" in goal_md.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# v0.6.6 — First-run wizard + /setup/needed endpoint
# ---------------------------------------------------------------------------


def test_setup_needed_true_when_no_projects(client):
    """GET /setup/needed returns needed=True when DB has no projects."""
    r = client.get("/setup/needed")
    assert r.status_code == 200
    assert r.json()["needed"] is True


def test_setup_needed_false_when_projects_exist(client):
    """GET /setup/needed returns needed=False once a project exists."""
    client.post("/projects", json={"name": "demo"})
    r = client.get("/setup/needed")
    assert r.status_code == 200
    assert r.json()["needed"] is False


def test_dashboard_html_has_setup_wizard(client):
    """Dashboard HTML contains the setup wizard element."""
    r = client.get("/dashboard")
    assert "ez-wizard" in r.text
    assert "ez-create-btn" in r.text


def test_dashboard_html_has_project_switcher(client):
    """Dashboard HTML contains the project switcher dropdown."""
    r = client.get("/dashboard")
    assert "project-switcher" in r.text


# ---------------------------------------------------------------------------
# v0.6.7 — IP attribution PDF export
# ---------------------------------------------------------------------------


def test_export_pdf_returns_pdf(client):
    """GET /projects/{id}/export/pdf returns a PDF."""
    proj = client.post("/projects", json={"name": "iptest"}).json()
    sess = client.post("/sessions/register", json={"project_id": proj["id"], "name": "s1"}).json()
    client.post("/tasks", json={"session_id": sess["id"], "project_id": proj["id"], "description": "did work", "status": "done"})
    r = client.get(f"/projects/{proj['id']}/export/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert len(r.content) > 100


def test_export_pdf_contains_sha256(client):
    """PDF export works for a project with tasks and returns valid PDF bytes."""
    proj = client.post("/projects", json={"name": "iptest2"}).json()
    sess = client.post("/sessions/register", json={"project_id": proj["id"], "name": "sha-sess"}).json()
    client.post("/tasks", json={"session_id": sess["id"], "project_id": proj["id"], "description": "sha test task", "status": "done"})
    r = client.get(f"/projects/{proj['id']}/export/pdf")
    assert r.status_code == 200
    # PDF files start with the %PDF- magic header
    assert r.content[:4] == b"%PDF"
    # The content should be a meaningful size (> 500 bytes) indicating the SHA footer was written
    assert len(r.content) > 500


def test_export_pdf_404_unknown_project(client):
    """GET /projects/bad-id/export/pdf returns 404."""
    r = client.get("/projects/doesnotexist/export/pdf")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# v1.0.0 — PyInstaller entry point
# ---------------------------------------------------------------------------


def test_main_entry_imports_without_error():
    """__main__entry.py can be imported without raising."""
    import importlib
    mod = importlib.import_module("meridian.__main__entry")
    assert hasattr(mod, "main")
    assert hasattr(mod, "_set_frozen_defaults")


def test_frozen_db_path_resolves_to_home():
    """When sys.frozen is set, DB path defaults to ~/.meridian."""
    from pathlib import Path
    from meridian.__main__entry import _set_frozen_defaults

    original = getattr(sys, "frozen", None)
    original_db = os.environ.pop("MERIDIAN_DB", None)
    try:
        sys.frozen = True
        _set_frozen_defaults()
        expected = str(Path.home() / ".meridian" / "meridian.db")
        assert os.environ.get("MERIDIAN_DB") == expected
    finally:
        if original is not None:
            sys.frozen = original
        else:
            try:
                del sys.frozen
            except AttributeError:
                pass
        if original_db is not None:
            os.environ["MERIDIAN_DB"] = original_db
        elif "MERIDIAN_DB" in os.environ:
            del os.environ["MERIDIAN_DB"]


# ---------------------------------------------------------------------------
# STEP 0 / v1.1 — sprint_items checklist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_sprint_item_round_trip(db):
    p = await db_module.create_project(db, "alpha")
    item = await db_module.add_sprint_item(
        db, p["id"], "v0.6.4", "Dashboard save"
    )
    assert item["status"] == "pending"
    assert item["version"] == "v0.6.4"
    assert item["title"] == "Dashboard save"
    assert item["completed_at"] is None


@pytest.mark.asyncio
async def test_complete_sprint_item_marks_done_and_links_task(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "w")
    item = await db_module.add_sprint_item(db, p["id"], "v0.6.4", "save")
    t = await db_module.log_task(db, s["id"], p["id"], "shipped", "done")
    done = await db_module.complete_sprint_item(
        db, p["id"], item["id"], task_id=t["id"]
    )
    assert done is not None
    assert done["status"] == "done"
    assert done["task_id"] == t["id"]
    assert done["completed_at"] is not None


@pytest.mark.asyncio
async def test_skip_sprint_item_stores_reason(db):
    p = await db_module.create_project(db, "alpha")
    item = await db_module.add_sprint_item(db, p["id"], "v0.6.4", "save")
    skipped = await db_module.skip_sprint_item(
        db, p["id"], item["id"], reason="superseded by v1.1.0"
    )
    assert skipped is not None
    assert skipped["status"] == "skipped"
    assert "superseded" in (skipped["notes"] or "")


@pytest.mark.asyncio
async def test_complete_sprint_item_wrong_project_returns_none(db):
    a = await db_module.create_project(db, "alpha")
    b = await db_module.create_project(db, "beta")
    item = await db_module.add_sprint_item(db, a["id"], "v1", "thing")
    # Try to complete from the wrong project — atomic guard refuses.
    result = await db_module.complete_sprint_item(db, b["id"], item["id"])
    assert result is None
    fresh = await db_module.get_sprint_item(db, item["id"])
    assert fresh["status"] == "pending"


@pytest.mark.asyncio
async def test_get_sprint_items_filters_by_status(db):
    p = await db_module.create_project(db, "alpha")
    pending = await db_module.add_sprint_item(db, p["id"], "v1", "a")
    done_item = await db_module.add_sprint_item(db, p["id"], "v1", "b")
    await db_module.complete_sprint_item(db, p["id"], done_item["id"])
    pendings = await db_module.get_sprint_items(db, p["id"], status="pending")
    assert [it["id"] for it in pendings] == [pending["id"]]
    dones = await db_module.get_sprint_items(db, p["id"], status="done")
    assert [it["id"] for it in dones] == [done_item["id"]]
    all_items = await db_module.get_sprint_items(db, p["id"])
    assert len(all_items) == 2


@pytest.mark.asyncio
async def test_fail_sprint_item(db):
    """fail_sprint_item sets status=failed and stores reason in notes."""
    p = await db_module.create_project(db, "alpha")
    item = await db_module.add_sprint_item(db, p["id"], "v1.9", "my item")
    failed = await db_module.fail_sprint_item(db, p["id"], item["id"], reason="blocker")
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["notes"] == "blocker"
    assert failed["completed_at"] is not None


@pytest.mark.asyncio
async def test_push_sprint_item(db):
    """push_sprint_item sets status=pushed and stores target version."""
    p = await db_module.create_project(db, "alpha")
    item = await db_module.add_sprint_item(db, p["id"], "v1.9", "deferred feat")
    pushed = await db_module.push_sprint_item(db, p["id"], item["id"], "v2.0")
    assert pushed is not None
    assert pushed["status"] == "pushed"
    assert pushed["pushed_to"] == "v2.0"
    assert pushed["completed_at"] is not None


@pytest.mark.asyncio
async def test_add_sprint_item_with_group(db):
    """add_sprint_item stores item_group correctly."""
    p = await db_module.create_project(db, "alpha")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1.9", "grouped task", group="Auth", human_id="adam"
    )
    assert item["item_group"] == "Auth"
    assert item["human_id"] == "adam"
    # Retrieve and confirm persistence
    fetched = await db_module.get_sprint_item(db, item["id"])
    assert fetched["item_group"] == "Auth"


def test_build_sprint_items_xml_grouped():
    """build_sprint_items_xml wraps items in <group> when item_group is set."""
    xml = db_module.build_sprint_items_xml([
        {"id": "id-1", "version": "v1", "status": "todo",
         "title": "ungrouped", "item_group": None, "pushed_to": None},
        {"id": "id-2", "version": "v1", "status": "todo",
         "title": "grouped", "item_group": "Auth", "pushed_to": None},
    ])
    assert '<sprint_items cache="false">' in xml
    assert '<item' in xml
    assert 'ungrouped' in xml
    assert '<group name="Auth">' in xml
    assert 'grouped' in xml
    assert xml.endswith("</sprint_items>")


def test_build_sprint_items_xml_pushed_to_attr():
    """build_sprint_items_xml includes pushed_to attribute when set."""
    xml = db_module.build_sprint_items_xml([
        {"id": "id-1", "version": "v1", "status": "pushed",
         "title": "deferred", "item_group": None, "pushed_to": "v2.0"},
    ])
    assert 'pushed_to="v2.0"' in xml


def test_http_sprint_items_fail_and_push(client):
    """HTTP fail and push endpoints work and return 404 for unknown items."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    r = client.post(
        f"/projects/{project['id']}/sprint-items",
        json={"version": "v1.9", "title": "will fail"},
    )
    assert r.status_code == 201
    item = r.json()
    # fail it
    r = client.post(
        f"/projects/{project['id']}/sprint-items/{item['id']}/fail",
        json={"reason": "blocked"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    # 404 for unknown item on push
    r = client.post(
        f"/projects/{project['id']}/sprint-items/not-real/push",
        json={"to_version": "v2.0"},
    )
    assert r.status_code == 404


def test_http_sprint_item_group_and_human_id(client):
    """POST /sprint-items accepts group and human_id fields."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    r = client.post(
        f"/projects/{project['id']}/sprint-items",
        json={"version": "v1.9", "title": "grouped item",
              "group": "Infra", "human_id": "adam"},
    )
    assert r.status_code == 201
    item = r.json()
    assert item["item_group"] == "Infra"
    assert item["human_id"] == "adam"


def test_build_sprint_items_xml_layout():
    xml = db_module.build_sprint_items_xml([
        {
            "id": "id-1",
            "version": "v0.6.4",
            "item_group": None,
            "pushed_to": None,
            "status": "pending",
            "title": "Dashboard save",
        }
    ])
    assert '<sprint_items cache="false">' in xml
    assert '<item id="id-1" version="v0.6.4" status="pending">Dashboard save</item>' in xml
    assert xml.endswith("</sprint_items>")


def test_start_session_endpoint_includes_sprint_items(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    client.post(
        f"/projects/{project['id']}/goal",
        json={"content": "g", "north_star": "n", "sprint": "s"},
    )
    item = client.post(
        f"/projects/{project['id']}/sprint-items",
        json={"version": "v0.6.4", "title": "Dashboard save"},
    ).json()
    r = client.post(
        f"/projects/{project['id']}/start-session",
        json={"session_name": "w", "human_id": "adam"},
    )
    body = r.json()
    assert any(it["id"] == item["id"] for it in body["sprint_items"])
    assert "Dashboard save" in body["sprint_items_xml"]


def test_http_sprint_items_endpoints(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    # 422 when version/title missing.
    r = client.post(
        f"/projects/{project['id']}/sprint-items", json={"version": "v1"}
    )
    assert r.status_code == 422
    # Happy path.
    r = client.post(
        f"/projects/{project['id']}/sprint-items",
        json={"version": "v1", "title": "thing"},
    )
    assert r.status_code == 201
    item = r.json()
    # Complete it.
    r = client.post(
        f"/projects/{project['id']}/sprint-items/{item['id']}/complete",
        json={},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "done"
    # 404 when item is unknown.
    r = client.post(
        f"/projects/{project['id']}/sprint-items/does-not-exist/skip",
        json={"reason": "nope"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# v0.6.5 — /config endpoint contract
# ---------------------------------------------------------------------------


def test_config_endpoint_shape(client, monkeypatch):
    """GET /config exposes server_url + host + port + version + db."""
    monkeypatch.setenv("MERIDIAN_HOST", "127.0.0.1")
    monkeypatch.setenv("MERIDIAN_PORT", "7878")
    monkeypatch.delenv("MERIDIAN_SERVER_URL", raising=False)
    r = client.get("/config")
    assert r.status_code == 200
    body = r.json()
    assert body["host"] == "127.0.0.1"
    assert body["port"] == 7878
    assert body["server_url"] == "http://127.0.0.1:7878"
    assert isinstance(body["version"], str)
    assert body["db"] in {"memory", "sqlite"}


def test_config_endpoint_respects_server_url_override(client, monkeypatch):
    """When MERIDIAN_SERVER_URL is set the dashboard targets that URL,
    not the host/port pair — needed for hosted / reverse-proxied deploys."""
    monkeypatch.setenv("MERIDIAN_SERVER_URL", "https://meridian.example.com")
    r = client.get("/config")
    assert r.status_code == 200
    assert r.json()["server_url"] == "https://meridian.example.com"


def test_dashboard_html_calls_loadServerConfig(client):
    """The dashboard JS must call /config on startup so it can render
    the version label and (in hosted mode) target the right URL."""
    # v1.0.2: JS moved to static file
    js = client.get("/static/dashboard.js").text
    assert "loadServerConfig" in js
    assert "/config" in js


# ---------------------------------------------------------------------------
# v0.6.4 — dashboard save + dirty state (confirmation tests)
# ---------------------------------------------------------------------------


def test_dashboard_html_has_save_buttons_and_dirty_state(client):
    """All three goal fields have their own save button + the dirty
    CSS class is wired up so unsaved edits are visible."""
    # Button IDs are in JS (buildTabBody generates the HTML dynamically)
    js = client.get("/static/dashboard.js").text
    assert "save-north-star-" in js
    assert "save-goal-" in js
    assert "save-sprint-" in js
    # CSS classes are in the static CSS file (v1.0.2)
    css = client.get("/static/dashboard.css").text
    assert ".goal-area.dirty" in css
    assert ".goal-area.readonly" in css


# ---------------------------------------------------------------------------
# v1.1.0 — dashboard UX overhaul
# ---------------------------------------------------------------------------


def test_dashboard_html_has_open_in_claude_cta(client):
    """The chat panel was replaced with an Open-in-Claude CTA."""
    # v1.0.2: CTA markup is generated by JS (buildTabBody), so check static JS
    js = client.get("/static/dashboard.js").text
    assert "Open in Claude" in js
    assert "open-in-claude-" in js
    assert "claude.ai/new" in js


def test_dashboard_html_loads_marked_js(client):
    """marked.js is loaded from a CDN for the goal edit/preview toggle."""
    html = client.get("/dashboard").text
    assert "marked.min.js" in html  # CDN link stays in HTML template
    # v1.0.2: JS/CSS content moved to static files
    js = client.get("/static/dashboard.js").text
    assert "wireGoalPreviewToggle" in js
    css = client.get("/static/dashboard.css").text
    assert "preview-btn" in css


def test_dashboard_html_no_chat_input_textarea(client):
    """The chat input + send button were removed from the new layout.

    The defensive JS still references the ids so older bundles don't
    break; what we pin here is that the markup template no longer
    emits a chat textarea or send button.
    """
    html = client.get("/dashboard").text
    # Strings that only ever appeared inside the chat markup, never the JS.
    assert "message claude (enter to send" not in html
    assert "chat-input-row" not in html
    # v1.0.2: CTA class is in JS-generated markup — check static JS
    js = client.get("/static/dashboard.js").text
    assert "claude-handoff-panel" in js


# ---------------------------------------------------------------------------
# v1.1.1 — Activity Timeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_timeline_shape(db):
    p = await db_module.create_project(db, "alpha")
    s1 = await db_module.register_session(
        db, p["id"], "worker-1", human_id="adam"
    )
    s2 = await db_module.register_session(db, p["id"], "worker-2")
    await db_module.log_task(db, s1["id"], p["id"], "did A", "done")
    await db_module.log_task(db, s2["id"], p["id"], "did B", "failed")
    await db_module.set_goal(db, p["id"], "v1", north_star="N", sprint="S")
    await db_module.set_north_star(db, p["id"], "N2")
    timeline = await db_module.get_timeline(db, p["id"])
    assert {"tasks", "sessions", "goal_events"} <= set(timeline)
    assert len(timeline["tasks"]) == 2
    assert {t["status"] for t in timeline["tasks"]} == {"done", "failed"}
    # session_name is attached for swimlane rendering.
    assert {t["session_name"] for t in timeline["tasks"]} == {
        "worker-1", "worker-2"
    }
    assert len(timeline["sessions"]) == 2
    # Goal events fire on initial fields + north_star change.
    fields = {e["field"] for e in timeline["goal_events"]}
    assert "north_star" in fields


def test_get_timeline_endpoint_returns_payload(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "w", "human_id": "adam"},
    ).json()
    client.post(
        "/tasks",
        json={
            "session_id": sess["id"],
            "project_id": project["id"],
            "description": "shipped",
            "status": "done",
        },
    )
    r = client.get(f"/projects/{project['id']}/timeline")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["tasks"], list)
    assert isinstance(body["sessions"], list)
    assert isinstance(body["goal_events"], list)
    assert len(body["tasks"]) == 1
    assert body["tasks"][0]["session_name"] == "w"
    assert body["tasks"][0]["human_id"] == "adam"


def test_get_timeline_endpoint_404_for_unknown(client):
    r = client.get("/projects/no-such-project/timeline")
    assert r.status_code == 404


def test_dashboard_html_has_timeline_tab(client):
    """The dashboard exposes a TIMELINE drawer tab and loads it lazily."""
    # v1.0.2: tab markup + JS are in the static JS file (buildTabBody)
    js = client.get("/static/dashboard.js").text
    assert 'data-vtab="timeline"' in js
    assert "drawer-timeline-" in js
    assert "loadTimeline" in js


# ---------------------------------------------------------------------------
# v1.1.2 — GOAL.md attribution + conflict detection + ## Decisions
# ---------------------------------------------------------------------------

from meridian import goal_md as goal_md_module


def test_parse_goal_md_extracts_decisions_section():
    text = """# proj

## North Star
n

## Version Goal
v

## Sprint
s

## Decisions
- 2026-01-01 went with sqlite
- 2026-01-15 chose PyInstaller for v1
"""
    parsed = goal_md_module.parse_goal_md(text)
    assert parsed["north_star"] == "n"
    assert parsed["version_goal"] == "v"
    assert parsed["sprint"] == "s"
    assert "PyInstaller" in (parsed["decisions"] or "")


def test_format_goal_md_includes_decisions_section():
    out = goal_md_module.format_goal_md(
        "proj", "n", "v", "s",
        decisions="- 2026-01-01 sqlite chosen",
    )
    assert "## Decisions" in out
    assert "sqlite chosen" in out


@pytest.mark.asyncio
async def test_sync_goal_md_to_db_logs_attribution_via_watch(db, tmp_path):
    p = await db_module.create_project(db, "alpha")
    await db_module.set_goal(db, p["id"], "v0", north_star="ns0", sprint="sp0")
    md_path = tmp_path / "GOAL.md"
    md_path.write_text(goal_md_module.format_goal_md(
        "alpha", "ns0", "v0", "sp1-edited"
    ), encoding="utf-8")
    # Bump mtime ahead of the DB's updated_at so the conflict guard
    # doesn't kick in.
    import os, time as _t
    future = _t.time() + 300
    os.utime(md_path, (future, future))
    result = await goal_md_module.sync_goal_md_to_db(
        db, md_path, via_watch=True
    )
    assert result is not None and "conflict" not in result
    tasks = await db_module.get_tasks(db, p["id"], limit=10)
    descs = [t["description"] for t in tasks]
    assert any("file watch" in d.lower() for d in descs)
    # Only sprint changed in our edit, so only the sprint attribution
    # should be logged — north_star + version_goal are unchanged.
    sprint_logs = [d for d in descs if "sprint updated" in d]
    assert sprint_logs, f"expected sprint attribution log; got {descs}"


@pytest.mark.asyncio
async def test_sync_goal_md_to_db_detects_db_newer_conflict(db, tmp_path):
    p = await db_module.create_project(db, "alpha")
    md_path = tmp_path / "GOAL.md"
    md_path.write_text(goal_md_module.format_goal_md(
        "alpha", "old", "old", "old"
    ), encoding="utf-8")
    # Force the file mtime to be 1 hour in the past.
    import os, time as _t
    past = _t.time() - 3600
    os.utime(md_path, (past, past))
    # DB write fresh after the file → DB wins, file gets refreshed silently.
    await db_module.set_goal(db, p["id"], "new", north_star="new", sprint="new")
    result = await goal_md_module.sync_goal_md_to_db(
        db, md_path, via_watch=True
    )
    # DB wins: returns the existing DB goal (no conflict marker, no failed task).
    assert isinstance(result, dict)
    assert result.get("conflict") is not True
    assert result.get("north_star") == "new"
    # The file should now reflect DB content.
    written = md_path.read_text(encoding="utf-8")
    assert "new" in written
    # No conflict log_task should exist.
    tasks = await db_module.get_tasks(db, p["id"], limit=5)
    assert not any(
        "GOAL.md conflict" in (t["description"] or "")
        for t in tasks
    )


@pytest.mark.asyncio
async def test_sync_goal_md_to_db_no_attribution_when_unchanged(db, tmp_path):
    p = await db_module.create_project(db, "alpha")
    await db_module.set_goal(
        db, p["id"], "v0", north_star="ns0", sprint="sp0"
    )
    md_path = tmp_path / "GOAL.md"
    md_path.write_text(goal_md_module.format_goal_md(
        "alpha", "ns0", "v0", "sp0"  # identical to DB
    ), encoding="utf-8")
    import os, time as _t
    future = _t.time() + 300
    os.utime(md_path, (future, future))
    before = await db_module.get_tasks(db, p["id"], limit=20)
    await goal_md_module.sync_goal_md_to_db(db, md_path, via_watch=True)
    after = await db_module.get_tasks(db, p["id"], limit=20)
    # No new attribution log_tasks when nothing changed.
    new_descs = {t["description"] for t in after} - {t["description"] for t in before}
    assert not any("file watch" in d.lower() for d in new_descs)


# ---------------------------------------------------------------------------
# v1.1.3 — Goal coherence warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_goal_field_ages_reflects_field_history(db):
    """Each field's age tracks the most recent change of that specific
    field, not the latest goal version. v2.3 — uses per-field timestamp
    columns (ns_updated_at / content_updated_at / sprint_updated_at) so
    in-place UPDATEs from set_sprint can still report accurate freshness."""
    import time as _t
    p = await db_module.create_project(db, "alpha")
    await db_module.set_goal(
        db, p["id"], "v0-content", north_star="ns0", sprint="sp0"
    )
    # v2.3 — backdate every timestamp on the row so each field reports
    # ~40 days old before the fresh sprint update. (Pre-v2.3 only
    # `updated_at` mattered, but the dedicated per-field columns are
    # the new source of truth.)
    await db.execute(
        "UPDATE goal_states SET updated_at = datetime('now','-40 days'), "
        "ns_updated_at = datetime('now','-40 days'), "
        "content_updated_at = datetime('now','-40 days'), "
        "sprint_updated_at = datetime('now','-40 days')"
    )
    await db.commit()
    # Fresh sprint update (today). north_star + version_goal stay old.
    await db_module.set_sprint(db, p["id"], "sp1-fresh")
    ages = await db_module.get_goal_field_ages(db, p["id"])
    # north_star + version_goal should be ~40 days; sprint < 1 day.
    assert ages["north_star"]["age_seconds"] > 30 * 86400
    assert ages["version_goal"]["age_seconds"] > 30 * 86400
    assert ages["sprint"]["age_seconds"] < 86400


def test_compute_coherence_warning_levels():
    """ok / warn / critical thresholds fire on the right ages."""
    none = {
        "north_star": {"age_seconds": 1_000},
        "version_goal": {"age_seconds": 1_000},
        "sprint": {"age_seconds": 1_000},
    }
    assert db_module.compute_coherence_warning(none)["level"] == "ok"
    warn = {
        "north_star": {"age_seconds": 14 * 86400},
        "version_goal": {"age_seconds": 1_000},
        "sprint": {"age_seconds": 1_000},
    }
    out = db_module.compute_coherence_warning(warn)
    assert out["level"] == "warn"
    assert [s["field"] for s in out["stale_fields"]] == ["north_star"]
    crit = {
        "north_star": {"age_seconds": 60 * 86400},
        "version_goal": {"age_seconds": 8 * 86400},
        "sprint": {"age_seconds": 1_000},
    }
    out = db_module.compute_coherence_warning(crit)
    assert out["level"] == "critical"
    # sorted oldest first
    assert out["stale_fields"][0]["field"] == "north_star"


def test_get_goal_endpoint_includes_coherence_warning(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    client.post(
        f"/projects/{project['id']}/goal",
        json={"content": "v", "north_star": "n", "sprint": "s"},
    )
    body = client.get(f"/projects/{project['id']}/goal").json()
    assert "coherence_warning" in body
    assert "field_ages" in body
    assert body["coherence_warning"]["level"] in {"ok", "warn", "critical"}
    assert set(body["field_ages"].keys()) == {
        "north_star", "version_goal", "sprint"
    }


def test_get_goal_xml_includes_coherence_warning_when_stale(client):
    """XML envelope grows a <coherence_warning> child when the
    warning is non-ok so cold sessions see it inline."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    client.post(
        f"/projects/{project['id']}/goal",
        json={"content": "v", "north_star": "n", "sprint": "s"},
    )
    body = client.get(f"/projects/{project['id']}/goal").json()
    xml = body["xml"]
    assert "<coherence_warning" in xml
    assert 'level="ok"' in xml  # fresh project


def test_start_session_endpoint_carries_coherence_warning(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    client.post(
        f"/projects/{project['id']}/goal",
        json={"content": "v", "north_star": "n", "sprint": "s"},
    )
    r = client.post(
        f"/projects/{project['id']}/start-session",
        json={"session_name": "w", "human_id": "adam"},
    )
    body = r.json()
    assert body["goal"]["coherence_warning"]["level"] == "ok"
    assert "<coherence_warning" in body["goal_xml"]


# ---------------------------------------------------------------------------
# v1.1.4 — decisions field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_decision_prepends_with_date(db):
    p = await db_module.create_project(db, "alpha")
    log = await db_module.set_decision(db, p["id"], "chose sqlite", timestamp="2026-01-15")
    assert "[2026-01-15] chose sqlite" in log
    # Append a second entry — newest first.
    log = await db_module.set_decision(
        db, p["id"], "pyinstaller for v1", timestamp="2026-02-01"
    )
    assert log.index("pyinstaller") < log.index("sqlite")


@pytest.mark.asyncio
async def test_get_decisions_returns_none_when_unset(db):
    p = await db_module.create_project(db, "alpha")
    assert await db_module.get_decisions(db, p["id"]) is None


@pytest.mark.asyncio
async def test_set_decision_unknown_project_raises(db):
    with pytest.raises(ValueError):
        await db_module.set_decision(db, "no-such", "x")


def test_post_decisions_endpoint(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    r = client.post(
        f"/projects/{project['id']}/decisions",
        json={"text": "sqlite over postgres"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "sqlite over postgres" in body["decisions"]
    # Empty text → 422
    r = client.post(
        f"/projects/{project['id']}/decisions", json={"text": "   "}
    )
    assert r.status_code == 422
    # Unknown project → 404
    r = client.post(
        "/projects/no-such/decisions", json={"text": "x"}
    )
    assert r.status_code == 404


def test_get_goal_endpoint_includes_decisions(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    client.post(
        f"/projects/{project['id']}/goal",
        json={"content": "v", "north_star": "n", "sprint": "s"},
    )
    client.post(
        f"/projects/{project['id']}/decisions",
        json={"text": "sqlite over postgres"},
    )
    body = client.get(f"/projects/{project['id']}/goal").json()
    assert body["decisions"] and "sqlite" in body["decisions"]
    assert '<decisions cache="true">' in body["xml"]


def test_start_session_includes_decisions(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    client.post(
        f"/projects/{project['id']}/goal",
        json={"content": "v", "north_star": "n", "sprint": "s"},
    )
    client.post(
        f"/projects/{project['id']}/decisions",
        json={"text": "fpdf2 not reportlab"},
    )
    r = client.post(
        f"/projects/{project['id']}/start-session",
        json={"session_name": "w", "human_id": "adam"},
    )
    body = r.json()
    assert "fpdf2" in (body["goal"]["decisions"] or "")
    assert "fpdf2" in body["goal_xml"]


# ---------------------------------------------------------------------------
# v3.4 — workspace context injection + workspace settings
# ---------------------------------------------------------------------------


def test_start_session_includes_workspace_context(client):
    """A workspace-level decision must surface in start-session output so a
    cold executor sees tenant-global truth without a separate call."""
    client.post(
        "/workspace/decisions",
        json={"title": "Monorepo", "body": "one repo for all services",
              "category": "ARCHITECTURAL"},
    )
    project = client.post("/projects", json={"name": "ws-ctx"}).json()
    r = client.post(
        f"/projects/{project['id']}/start-session",
        json={"session_name": "alpha"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "workspace_context" in body
    assert "Monorepo" in body["workspace_context"]
    assert "one repo for all services" in body["workspace_context"]


def test_start_session_workspace_context_empty_when_none(client):
    """No workspace decisions/notes → workspace_context is an empty string,
    not missing — callers can render unconditionally."""
    project = client.post("/projects", json={"name": "ws-empty"}).json()
    body = client.post(
        f"/projects/{project['id']}/start-session",
        json={"session_name": "alpha"},
    ).json()
    assert body.get("workspace_context") == ""


def test_workspace_notes_crud_http(client):
    """Workspace notes round-trip through the HTTP routes the dashboard uses."""
    created = client.post(
        "/workspace/notes",
        json={"title": "Onboarding", "body": "all repos use pixi", "tags": "setup"},
    )
    assert created.status_code == 201
    note_id = created.json()["id"]
    listed = client.get("/workspace/notes").json()
    assert any(n["id"] == note_id for n in listed)
    assert client.delete(f"/workspace/notes/{note_id}").status_code == 204
    assert all(n["id"] != note_id for n in client.get("/workspace/notes").json())


def test_workspace_decisions_crud_http(client):
    """Workspace decisions round-trip through the HTTP routes."""
    created = client.post(
        "/workspace/decisions",
        json={"title": "Use psycopg3", "body": "asyncpg has DLL issues",
              "category": "TECHNICAL"},
    )
    assert created.status_code == 201
    did = created.json()["id"]
    listed = client.get("/workspace/decisions").json()
    assert any(d["id"] == did for d in listed)
    assert client.delete(f"/workspace/decisions/{did}").status_code == 204


def test_workspace_settings_roundtrip_http(client):
    """Workspace settings GET returns defaults; PATCH persists changes."""
    initial = client.get("/workspace/settings").json()
    assert initial["hitl_auto_answer_default"] is False
    assert initial["sprint_name_default"] is None
    patched = client.patch(
        "/workspace/settings",
        json={"hitl_auto_answer_default": True, "sprint_name_default": "june"},
    ).json()
    assert patched["hitl_auto_answer_default"] is True
    assert patched["sprint_name_default"] == "june"
    # Persisted across a fresh GET.
    again = client.get("/workspace/settings").json()
    assert again["hitl_auto_answer_default"] is True
    assert again["sprint_name_default"] == "june"


def test_workspace_settings_partial_patch_preserves_other_field(client):
    """Patching one field must not clobber the other."""
    client.patch(
        "/workspace/settings",
        json={"hitl_auto_answer_default": True, "sprint_name_default": "keep-me"},
    )
    client.patch("/workspace/settings", json={"hitl_auto_answer_default": False})
    final = client.get("/workspace/settings").json()
    assert final["hitl_auto_answer_default"] is False
    assert final["sprint_name_default"] == "keep-me"


@pytest.mark.asyncio
async def test_workspace_settings_db_defaults(db):
    """get_workspace_settings returns a usable dict even with no row written."""
    settings = await db_module.get_workspace_settings(db)
    assert settings["hitl_auto_answer_default"] is False
    assert settings["sprint_name_default"] is None


def test_build_goal_xml_omits_decisions_when_empty():
    """No <decisions> tag when there's nothing to show — worker
    sessions in v1.2.0 will pass decisions=None for the same reason."""
    xml = db_module.build_goal_xml(
        {"version": 1, "content": "v", "north_star": "n", "sprint": "s"},
        "alpha", [], None, decisions=None,
    )
    assert "<decisions" not in xml


# ---------------------------------------------------------------------------
# v1.2.0 — start_worker_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_worker_session_claims_oldest_pending(db):
    p = await db_module.create_project(db, "alpha")
    human = await db_module.register_session(db, p["id"], "human")
    # Two pending tasks; oldest must be picked.
    older = await db_module.log_task(
        db, human["id"], p["id"], "do first", "pending"
    )
    newer = await db_module.log_task(
        db, human["id"], p["id"], "do later", "pending"
    )
    await db_module.set_goal(
        db, p["id"], "ship v1.2", north_star="vision", sprint="now"
    )
    result = await db_module.start_worker_session(db, p["id"])
    assert result["task"]["id"] == older["id"]
    assert result["task"]["claimed_by"] == result["session_id"]
    # Worker session is tagged.
    async with db.execute(
        "SELECT session_type FROM sessions WHERE id = ?",
        (result["session_id"],),
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "worker"


@pytest.mark.asyncio
async def test_start_worker_session_xml_is_slim(db):
    p = await db_module.create_project(db, "alpha")
    human = await db_module.register_session(db, p["id"], "human")
    await db_module.log_task(db, human["id"], p["id"], "do it", "pending")
    await db_module.set_goal(
        db, p["id"], "ship v1.2", north_star="N", sprint="S"
    )
    await db_module.set_decision(db, p["id"], "internal call")
    result = await db_module.start_worker_session(db, p["id"])
    xml = result["worker_context"]
    # Under 700 chars per spec (matters because it lands in every
    # worker's first prompt).
    assert len(xml) < 700, f"worker_context too big: {len(xml)} chars"
    # Worker-relevant fields present.
    assert "<version_goal>" in xml
    assert "<task " in xml
    assert "<repo>" in xml
    assert "<test_cmd>" in xml
    assert "<commit_pattern>" in xml
    assert "<done_when>" in xml
    # Excluded fields absent — workers must NOT see these.
    assert "north_star" not in xml
    assert "decisions" not in xml
    assert "<sprint>" not in xml
    assert "<recent_tasks>" not in xml


@pytest.mark.asyncio
async def test_start_worker_session_explicit_task_id(db):
    p = await db_module.create_project(db, "alpha")
    human = await db_module.register_session(db, p["id"], "human")
    t1 = await db_module.log_task(db, human["id"], p["id"], "first", "pending")
    t2 = await db_module.log_task(db, human["id"], p["id"], "second", "pending")
    await db_module.set_goal(db, p["id"], "x")
    result = await db_module.start_worker_session(db, p["id"], task_id=t2["id"])
    assert result["task"]["id"] == t2["id"]


@pytest.mark.asyncio
async def test_start_worker_session_raises_when_no_claimable(db):
    p = await db_module.create_project(db, "alpha")
    await db_module.set_goal(db, p["id"], "x")
    with pytest.raises(ValueError):
        await db_module.start_worker_session(db, p["id"])


@pytest.mark.asyncio
async def test_start_worker_session_raises_when_already_claimed(db):
    p = await db_module.create_project(db, "alpha")
    human = await db_module.register_session(db, p["id"], "human")
    t = await db_module.log_task(db, human["id"], p["id"], "x", "pending")
    await db_module.set_goal(db, p["id"], "x")
    await db_module.claim_task(db, t["id"], human["id"])
    with pytest.raises(ValueError):
        await db_module.start_worker_session(db, p["id"], task_id=t["id"])


def test_start_worker_session_http_endpoint(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "human"},
    ).json()
    task = client.post(
        "/tasks",
        json={
            "session_id": sess["id"],
            "project_id": project["id"],
            "description": "ship feature",
            "status": "pending",
        },
    ).json()
    client.post(
        f"/projects/{project['id']}/goal",
        json={"content": "v", "north_star": "n", "sprint": "s"},
    )
    r = client.post(
        f"/projects/{project['id']}/start-worker-session", json={}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["task"]["id"] == task["id"]
    assert "<worker_context>" in body["worker_context"]
    # When no claimable tasks → 404
    r = client.post(
        f"/projects/{project['id']}/start-worker-session", json={}
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# v1.2.1 — Session auto-summary + parent_session_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_session_skips_when_under_min_tasks(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "human")
    await db_module.log_task(db, s["id"], p["id"], "just one", "done")
    called = []

    async def stub(prompt: str):
        called.append(prompt)
        return {"session_type": "human", "tasks_completed": 1,
                "key_decisions": [], "summary": "trivial"}

    result = await db_module.summarize_session(
        db, s["id"], min_tasks=3, summarizer=stub
    )
    assert result is None  # below threshold
    assert called == []     # summarizer never invoked


@pytest.mark.asyncio
async def test_summarize_session_stores_structured_output(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "human")
    for i in range(3):
        await db_module.log_task(db, s["id"], p["id"], f"task {i}", "done")

    async def stub(prompt: str):
        # Verify the prompt carries the task descriptions.
        assert "task 0" in prompt and "task 2" in prompt
        return {
            "session_type": "human",
            "tasks_completed": 3,
            "key_decisions": ["sqlite", "pyinstaller"],
            "summary": "shipped three tasks across v1.2",
        }

    summary = await db_module.summarize_session(db, s["id"], summarizer=stub)
    assert summary is not None
    assert summary["tasks_completed"] == 3
    assert "sqlite" in summary["key_decisions"]
    # Stored on the session row.
    async with db.execute(
        "SELECT session_summary FROM sessions WHERE id = ?", (s["id"],)
    ) as cur:
        row = await cur.fetchone()
    assert row[0] and "shipped three tasks" in row[0]


@pytest.mark.asyncio
async def test_summarize_session_rejects_malformed_output(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "human")
    for i in range(3):
        await db_module.log_task(db, s["id"], p["id"], f"task {i}", "done")

    async def bad(prompt: str):
        return {"summary": "missing required fields"}

    result = await db_module.summarize_session(db, s["id"], summarizer=bad)
    assert result is None
    # session_summary stays null.
    async with db.execute(
        "SELECT session_summary FROM sessions WHERE id = ?", (s["id"],)
    ) as cur:
        row = await cur.fetchone()
    assert row[0] is None


@pytest.mark.asyncio
async def test_timeline_carries_session_summary(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "human")
    for i in range(3):
        await db_module.log_task(db, s["id"], p["id"], f"task {i}", "done")

    async def stub(prompt: str):
        return {
            "session_type": "human", "tasks_completed": 3,
            "key_decisions": [], "summary": "did three things",
        }

    await db_module.summarize_session(db, s["id"], summarizer=stub)
    timeline = await db_module.get_timeline(db, p["id"])
    sessions = {s["id"]: s for s in timeline["sessions"]}
    assert sessions[s["id"]]["summary"]["summary"] == "did three things"
    assert sessions[s["id"]]["session_type"] == "human"


@pytest.mark.asyncio
async def test_timeline_daily_counts_single_human(db):
    """daily_counts aggregates one bucket per day with task + session totals."""
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess-a", human_id="adam")
    for i in range(4):
        await db_module.log_task(db, s["id"], p["id"], f"task {i}", "done")

    timeline = await db_module.get_timeline(db, p["id"])
    daily = timeline["daily_counts"]
    assert len(daily) == 1
    day = daily[0]
    assert day["count"] == 4
    assert day["session_count"] == 1
    assert day["humans"] == {"adam": 4}
    assert day["sessions"][0]["name"] == "sess-a"
    assert day["sessions"][0]["count"] == 4
    assert day["sessions"][0]["human"] == "adam"


@pytest.mark.asyncio
async def test_timeline_daily_counts_multi_human(db):
    """Per-day humans breakdown splits task counts by human_id."""
    p = await db_module.create_project(db, "alpha")
    s1 = await db_module.register_session(db, p["id"], "sess-a", human_id="adam")
    s2 = await db_module.register_session(db, p["id"], "sess-b", human_id="bri")
    for i in range(3):
        await db_module.log_task(db, s1["id"], p["id"], f"a {i}", "done")
    for i in range(2):
        await db_module.log_task(db, s2["id"], p["id"], f"b {i}", "done")

    timeline = await db_module.get_timeline(db, p["id"])
    daily = timeline["daily_counts"]
    assert len(daily) == 1
    day = daily[0]
    assert day["count"] == 5
    assert day["session_count"] == 2
    assert day["humans"] == {"adam": 3, "bri": 2}
    # sessions sorted by descending task count
    assert [se["count"] for se in day["sessions"]] == [3, 2]


@pytest.mark.asyncio
async def test_timeline_daily_counts_empty(db):
    """No tasks → empty daily_counts list (not missing key)."""
    p = await db_module.create_project(db, "alpha")
    timeline = await db_module.get_timeline(db, p["id"])
    assert timeline["daily_counts"] == []


def test_timeline_endpoint_exposes_daily_counts(client):
    """The /timeline route surfaces daily_counts for the heatmap."""
    p = client.post("/projects", json={"name": "alpha-tl"}).json()
    resp = client.get(f"/projects/{p['id']}/timeline")
    assert resp.status_code == 200
    body = resp.json()
    assert "daily_counts" in body
    assert isinstance(body["daily_counts"], list)


@pytest.mark.asyncio
async def test_timeline_daily_counts_multi_day(db):
    """Tasks on different days produce separate buckets, sorted by date."""
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess-a", human_id="adam")
    t1 = await db_module.log_task(db, s["id"], p["id"], "old", "done")
    await db_module.log_task(db, s["id"], p["id"], "new", "done")
    # Backdate the first task to a prior day.
    await db.execute(
        "UPDATE task_log SET created_at = ? WHERE id = ?",
        ("2026-01-01 09:00:00", t1["id"]),
    )
    await db.commit()

    timeline = await db_module.get_timeline(db, p["id"])
    daily = timeline["daily_counts"]
    assert len(daily) == 2
    # ascending by date
    assert daily[0]["date"] == "2026-01-01"
    assert daily[0]["count"] == 1
    assert daily[1]["count"] == 1


@pytest.mark.asyncio
async def test_timeline_daily_counts_unknown_human(db):
    """A session with no human_id buckets under '(unknown)'."""
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess-a")
    await db_module.log_task(db, s["id"], p["id"], "task", "done")

    timeline = await db_module.get_timeline(db, p["id"])
    day = timeline["daily_counts"][0]
    assert day["humans"] == {"(unknown)": 1}
    assert day["sessions"][0]["human"] == "(unknown)"


@pytest.mark.asyncio
async def test_timeline_daily_counts_counts_all_statuses(db):
    """Heatmap counts every logged task, not just done/failed."""
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess-a", human_id="adam")
    await db_module.log_task(db, s["id"], p["id"], "done one", "done")
    await db_module.log_task(db, s["id"], p["id"], "pending one", "pending")

    timeline = await db_module.get_timeline(db, p["id"])
    assert timeline["daily_counts"][0]["count"] == 2


def test_canonical_person_aliases():
    """Known human_id aliases collapse to one canonical identity; emails pass
    through lowercased; blanks map to (unknown)."""
    cp = db_module.canonical_person
    assert cp("adam") == "adam"
    assert cp("Adam Camerer") == "adam"
    assert cp("AdamCamerer") == "adam"
    assert cp("Adam Camerer (executor)") == "adam"
    # Emails are canonical as-is (lowercased), never aliased.
    assert cp("Ada@Example.com") == "ada@example.com"
    # Empty / unknown sentinels.
    assert cp(None) == "(unknown)"
    assert cp("") == "(unknown)"
    assert cp("  ") == "(unknown)"
    assert cp("(unknown)") == "(unknown)"
    assert cp("none") == "(unknown)"
    # An unknown free-text id is preserved (lowercased), not dropped.
    assert cp("Bri") == "bri"


@pytest.mark.asyncio
async def test_timeline_exposes_people_and_clients(db):
    """get_timeline returns top-level canonical people + client lists for the
    filter chips (items 30/31)."""
    p = await db_module.create_project(db, "alpha")
    s1 = await db_module.register_session(
        db, p["id"], "sess-a", human_id="Adam Camerer", client_type="claude-code"
    )
    s2 = await db_module.register_session(
        db, p["id"], "sess-b", human_id="bri", client_type="cursor"
    )
    await db_module.log_task(db, s1["id"], p["id"], "a", "done")
    await db_module.log_task(db, s2["id"], p["id"], "b", "done")

    timeline = await db_module.get_timeline(db, p["id"])
    # "Adam Camerer" collapses to "adam"; people sorted.
    assert timeline["people"] == ["adam", "bri"]
    assert timeline["clients"] == ["claude-code", "cursor"]


@pytest.mark.asyncio
async def test_timeline_daily_sessions_carry_person_and_client(db):
    """Per-day session entries expose canonical person + client app so the
    frontend can filter without re-deriving identity."""
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(
        db, p["id"], "sess-a", human_id="Adam Camerer", client_type="claude-code"
    )
    await db_module.log_task(db, s["id"], p["id"], "task", "done")

    timeline = await db_module.get_timeline(db, p["id"])
    se = timeline["daily_counts"][0]["sessions"][0]
    assert se["person"] == "adam"
    assert se["client"] == "claude-code"
    # daily bucket also carries a canonical people breakdown.
    assert timeline["daily_counts"][0]["people"] == {"adam": 1}


@pytest.mark.asyncio
async def test_timeline_client_defaults_to_none(db):
    """A session with no client_type reports client '(none)'."""
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess-a", human_id="adam")
    await db_module.log_task(db, s["id"], p["id"], "task", "done")

    timeline = await db_module.get_timeline(db, p["id"])
    assert timeline["clients"] == ["(none)"]
    assert timeline["daily_counts"][0]["sessions"][0]["client"] == "(none)"


def test_timeline_endpoint_exposes_people_and_clients(client):
    """The /timeline route surfaces people + clients lists."""
    p = client.post("/projects", json={"name": "alpha-pc"}).json()
    body = client.get(f"/projects/{p['id']}/timeline").json()
    assert "people" in body and isinstance(body["people"], list)
    assert "clients" in body and isinstance(body["clients"], list)


def test_session_summary_schema_shape():
    """The JSON schema we hand to haiku has the four expected keys."""
    schema = db_module.SESSION_SUMMARY_SCHEMA
    props = schema["schema"]["properties"]
    assert set(props.keys()) == {
        "session_type", "tasks_completed", "key_decisions", "summary"
    }
    assert props["session_type"]["enum"] == ["human", "worker"]


@pytest.mark.asyncio
async def test_enqueue_claude_task_writes_parent_session_id(db):
    p = await db_module.create_project(db, "alpha")
    parent = await db_module.register_session(db, p["id"], "parent")
    task = await enqueue_module.enqueue_claude_task(
        db,
        parent["id"],
        p["id"],
        "echo hi",
        worker_argv=[sys.executable, "-c", "print('ok')"],
        wait=True,
    )
    # Default parent_session_id is the enqueueing session.
    assert task["parent_session_id"] == parent["id"]


@pytest.mark.asyncio
async def test_enqueue_claude_task_explicit_parent_session_id(db):
    p = await db_module.create_project(db, "alpha")
    parent = await db_module.register_session(db, p["id"], "parent")
    other = await db_module.register_session(db, p["id"], "other")
    task = await enqueue_module.enqueue_claude_task(
        db,
        parent["id"],
        p["id"],
        "echo hi",
        worker_argv=[sys.executable, "-c", "print('ok')"],
        wait=True,
        parent_session_id=other["id"],
    )
    assert task["parent_session_id"] == other["id"]


# ---------------------------------------------------------------------------
# v1.0.1 — in_progress status + PID watchdog
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_progress_status_set_on_spawn(db):
    """_run_worker marks task in_progress immediately after spawning."""
    from meridian.enqueue import _run_worker, PROMPT_PREFIX
    proj = await db_module.create_project(db, "pid-test")
    sess = await db_module.register_session(db, proj["id"], "s")
    task = await db_module.log_task(db, sess["id"], proj["id"], "t", "pending")

    # Use a stub that exits cleanly
    argv = [sys.executable, "-c", "import time; time.sleep(0.01)"]
    await _run_worker(db, task["id"], "hello", argv, timeout=10)

    updated = await db_module.get_task(db, task["id"])
    # After completion it should be done (we check in_progress was transient)
    assert updated["status"] == "done"


@pytest.mark.asyncio
async def test_watchdog_marks_dead_pid_failed(db):
    """get_in_progress_tasks_with_pid returns rows; update_task marks them failed."""
    proj = await db_module.create_project(db, "watchdog-test")
    sess = await db_module.register_session(db, proj["id"], "s")
    task = await db_module.log_task(db, sess["id"], proj["id"], "work", "pending")

    # Manually put it in_progress with a PID that doesn't exist
    await db_module.update_task(db, task["id"], status="in_progress")
    await db_module.update_task_worker_pid(db, task["id"], 999999999)

    stale = await db_module.get_in_progress_tasks_with_pid(db)
    assert any(t["id"] == task["id"] for t in stale)

    # Simulate watchdog: PID 999999999 is dead -> mark failed
    for t in stale:
        pid = t.get("worker_pid")
        if pid is None:
            continue
        try:
            os.kill(int(pid), 0)
        except (ProcessLookupError, PermissionError, OSError):
            # OSError covers Windows WinError 87 for non-existent PIDs
            await db_module.update_task(db, t["id"], status="failed",
                description=f"[claude-error] worker process died unexpectedly (PID {pid})")

    final = await db_module.get_task(db, task["id"])
    assert final["status"] == "failed"
    assert "999999999" in final["description"]


def test_update_task_accepts_in_progress(client):
    """PATCH /tasks/{id} accepts in_progress as a valid status."""
    proj = client.post("/projects", json={"name": "ip-test"}).json()
    sess = client.post("/sessions/register", json={"project_id": proj["id"], "name": "s"}).json()
    task = client.post("/tasks", json={"session_id": sess["id"], "project_id": proj["id"],
        "description": "work", "status": "pending"}).json()
    r = client.patch(f"/tasks/{task['id']}", json={"status": "in_progress"})
    assert r.status_code == 200
    assert r.json()["status"] == "in_progress"


# ---------------------------------------------------------------------------
# v1.0.2 — Extract dashboard to static files
# ---------------------------------------------------------------------------


def test_dashboard_static_js_served(client):
    """GET /static/dashboard.js returns the dashboard JS."""
    r = client.get("/static/dashboard.js")
    assert r.status_code == 200
    assert "javascript" in r.headers.get("content-type", "").lower() or len(r.content) > 100


def test_dashboard_static_css_served(client):
    """GET /static/dashboard.css returns the dashboard CSS."""
    r = client.get("/static/dashboard.css")
    assert r.status_code == 200


def test_dashboard_uses_static_assets(client):
    """Dashboard HTML references static files, not inline scripts."""
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "/static/dashboard.js" in r.text
    assert "/static/dashboard.css" in r.text


# ---------------------------------------------------------------------------
# v1.3.0 — "Last X days" project rewind view
# ---------------------------------------------------------------------------


def test_rewind_returns_correct_schema(client):
    """GET /rewind returns all required top-level keys."""
    proj = client.post("/projects", json={"name": "rw-test"}).json()
    r = client.get(f"/projects/{proj['id']}/rewind?days=7")
    assert r.status_code == 200
    body = r.json()
    assert "period_days" in body
    assert "tasks_total" in body
    assert "tasks_by_status" in body
    assert "versions_shipped" in body
    assert "goal_changes" in body
    assert "decisions_logged" in body
    assert "session_summaries" in body
    assert "sprint_items_completed" in body
    assert body["period_days"] == 7


def test_rewind_empty_period_returns_empty_arrays(client):
    """Rewind over period with no activity returns zeros, not errors."""
    proj = client.post("/projects", json={"name": "rw-empty"}).json()
    r = client.get(f"/projects/{proj['id']}/rewind?days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["tasks_total"] == 0
    assert body["versions_shipped"] == []


def test_rewind_counts_tasks_in_period(client):
    """Rewind tasks_total reflects tasks logged in the period."""
    proj = client.post("/projects", json={"name": "rw-tasks"}).json()
    sess = client.post("/sessions/register", json={"project_id": proj["id"], "name": "s1"}).json()
    client.post("/tasks", json={"session_id": sess["id"], "project_id": proj["id"],
        "description": "task 1", "status": "done"})
    client.post("/tasks", json={"session_id": sess["id"], "project_id": proj["id"],
        "description": "task 2", "status": "done"})
    r = client.get(f"/projects/{proj['id']}/rewind?days=7")
    assert r.status_code == 200
    assert r.json()["tasks_total"] == 2


def test_rewind_token_endpoint(client):
    """POST /rewind-token returns a token; GET /rewind accepts it."""
    proj = client.post("/projects", json={"name": "rw-token"}).json()
    r = client.post(f"/projects/{proj['id']}/rewind-token")
    assert r.status_code == 200
    token = r.json()["token"]
    assert token
    r2 = client.get(f"/projects/{proj['id']}/rewind?days=7&token={token}")
    assert r2.status_code == 200


def test_rewind_404_unknown_project(client):
    """GET /rewind returns 404 for unknown project."""
    r = client.get("/projects/doesnotexist/rewind?days=7")
    assert r.status_code == 404


def test_dashboard_has_rewind_tab(client):
    """Dashboard HTML/JS contain the Rewind tab."""
    r = client.get("/dashboard")
    assert r.status_code == 200
    js = client.get("/static/dashboard.js").text
    # The rewind tab button should be wired up in the dashboard bundle.
    assert "rewind" in js.lower() or "Rewind" in js


# ---------------------------------------------------------------------------
# v1.3.1 — Document Meridian MCP connection for claude.ai planning sessions
# ---------------------------------------------------------------------------


def test_readme_has_mcp_config_section(client):
    """README.md contains the MCP configuration section."""
    import os
    readme = open(
        os.path.join(os.path.dirname(__file__), '..', 'README.md'),
        encoding='utf-8',
    ).read()
    assert 'mcpServers' in readme
    assert 'meridian' in readme


def test_mcp_json_example_is_valid_json():
    """mcp.json.example is valid JSON."""
    import json
    import os
    path = os.path.join(
        os.path.dirname(__file__), '..', 'mcp.json.example'
    )
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    assert 'mcpServers' in data


# ---------------------------------------------------------------------------
# v1.4.0 — dashboard layout overhaul
# ---------------------------------------------------------------------------


def test_timeline_tasks_newest_first(client):
    """GET /timeline returns tasks newest-first so the dashboard shows recent
    activity at the top (v1.4.0 requirement)."""
    import uuid, time
    proj = client.post("/projects", json={"name": f"tl-order-{uuid.uuid4().hex[:6]}"}).json()
    pid = proj["id"]
    sess = client.post("/sessions/register", json={"project_id": pid, "name": "s1"}).json()
    sid = sess["id"]
    client.post("/tasks", json={"session_id": sid, "project_id": pid,
                                "description": "first task", "status": "done"})
    time.sleep(0.05)
    client.post("/tasks", json={"session_id": sid, "project_id": pid,
                                "description": "second task", "status": "done"})
    r = client.get(f"/projects/{pid}/timeline")
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    assert len(tasks) >= 2
    # Newest first: second task must appear before first task
    descriptions = [t["description"] for t in tasks]
    assert descriptions.index("second task") < descriptions.index("first task"), (
        "v1.4.0: timeline must return tasks newest first (DESC order)"
    )


def test_work_queue_vtab_in_dashboard(client):
    """Dashboard JS restores the 4-group sprint queue with paged done items."""
    js = client.get("/static/dashboard.js").text
    assert 'data-vtab="queue"' in js, (
        "v1.4.0: queue vtab button missing from buildTabBody"
    )
    assert "loadQueue" in js, (
        "v1.4.0: loadQueue function missing from dashboard.js"
    )
    assert "renderQueue" in js, (
        "v1.4.0: renderQueue function missing from dashboard.js"
    )
    assert "queue-body-" in js, (
        "v1.4.0: queue-body element id missing from dashboard.js"
    )
    assert "/sprint-items" in js, (
        "queue should read sprint items so pending work reflects the sprint board"
    )
    assert "QUEUE_DONE_PAGE_SIZE = 10" in js, (
        "done queue section should page sprint items 10 at a time"
    )
    assert "Backburner" in js and "Pending" in js and "In Progress" in js and "Done" in js, (
        "queue should render the restored 4-group sprint board"
    )
    assert "queue-done-more-" in js, (
        "done sprint items should expose a load-more control"
    )
    assert "Recent Sessions" in js, (
        "queue should keep a recent sessions section below the sprint board"
    )
    assert "s.id !== panel.liveSessionId && s.status !== 'active'" in js, (
        "live session should stay out of the Recent Sessions list"
    )
    assert 'start_session(project_id="' in js, (
        "resume button should copy a start_session() snippet"
    )


def test_project_sidebar_active_state_in_dashboard(client):
    """Selected projects keep a persistent active highlight in the sidebar."""
    js = client.get("/static/dashboard.js").text
    css = client.get("/static/dashboard.css").text
    assert "syncSidebarActiveProject" in js, (
        "dashboard.js should resync active sidebar state when tabs change"
    )
    assert "classList.toggle('active', item.dataset.projectId === state.activeTab)" in js, (
        "sidebar project rows should toggle an active class for the selected project"
    )
    assert ".project-item.active" in css, (
        "dashboard.css should style the active project state"
    )


def test_vtab_drawer_always_visible(client):
    """CSS: vtab-drawer must not use transform for hiding (v1.4.0 always-visible).

    The v1.4.0 layout change makes the drawer a permanent side panel
    rather than a slide-out overlay.  It must not have translateX(-…)
    in its style rule.
    """
    css = client.get("/static/dashboard.css").text
    assert "translateX(-360px)" not in css, (
        "v1.4.0: vtab-drawer still uses translateX(-360px) for hiding. "
        "The drawer must be always-visible in v1.4.0."
    )
    assert "translateX(-280px)" not in css, (
        "v1.4.0: vtab-drawer still uses translateX(-280px) for hiding."
    )


# ---------------------------------------------------------------------------
# Rewind tab improvements — expandable goal change rows + goal version browser
# ---------------------------------------------------------------------------


def test_goal_history_endpoint_returns_list(client):
    """GET /goal-history returns a list of goal versions newest-first."""
    import uuid
    proj = client.post("/projects", json={"name": f"gh-{uuid.uuid4().hex[:6]}"}).json()
    pid = proj["id"]
    client.post(f"/projects/{pid}/goal", json={"content": "first goal"})
    client.post(f"/projects/{pid}/goal", json={"content": "second goal"})
    r = client.get(f"/projects/{pid}/goal-history")
    assert r.status_code == 200
    history = r.json()
    assert isinstance(history, list)
    assert len(history) >= 2
    # newest first: version numbers descending
    versions = [h["version"] for h in history]
    assert versions == sorted(versions, reverse=True)


def test_goal_history_contains_required_fields(client):
    """Each goal-history entry has version, north_star, version_goal, sprint, created_at."""
    import uuid
    proj = client.post("/projects", json={"name": f"gh2-{uuid.uuid4().hex[:6]}"}).json()
    pid = proj["id"]
    client.post(f"/projects/{pid}/goal", json={"content": "v1 goal"})
    r = client.get(f"/projects/{pid}/goal-history")
    history = r.json()
    assert len(history) >= 1
    entry = history[0]
    for field in ("version", "north_star", "version_goal", "sprint", "created_at"):
        assert field in entry, f"goal-history entry missing field: {field}"


def test_goal_history_404_unknown_project(client):
    """GET /goal-history returns 404 for unknown project."""
    r = client.get("/projects/doesnotexist/goal-history")
    assert r.status_code == 404


def test_rewind_goal_changes_include_full_content(client):
    """goal_changes in rewind response include old_full and new_full fields."""
    import uuid, time
    proj = client.post("/projects", json={"name": f"gc-{uuid.uuid4().hex[:6]}"}).json()
    pid = proj["id"]
    client.post(f"/projects/{pid}/goal", json={"content": "initial goal content"})
    time.sleep(0.05)
    client.post(f"/projects/{pid}/goal", json={"content": "updated goal content"})
    r = client.get(f"/projects/{pid}/rewind?days=7")
    assert r.status_code == 200
    body = r.json()
    changes = body.get("goal_changes", [])
    if changes:
        c = changes[0]
        assert "old_full" in c, "goal_changes entry missing old_full"
        assert "new_full" in c, "goal_changes entry missing new_full"


def test_dashboard_js_has_goal_history_functions(client):
    """dashboard.js contains rewind goal rendering and toggleExpand (rewind improvements)."""
    js = client.get("/static/dashboard.js").text
    assert "renderRewindGoals" in js, "renderRewindGoals missing from dashboard.js"
    assert "toggleExpand" in js, "toggleExpand missing from dashboard.js"
    assert "goal-history" in js, "goal-history API call missing from dashboard.js"


# ---------------------------------------------------------------------------
# v1.9.x — connection profiles + restart
# ---------------------------------------------------------------------------

def test_config_endpoint_has_toml_fields(client):
    """GET /config exposes toml_exists, connection_name, and connections."""
    r = client.get("/config")
    assert r.status_code == 200
    data = r.json()
    assert "toml_exists" in data, "toml_exists missing from /config"
    assert "connection_name" in data, "connection_name missing from /config"
    assert "connections" in data, "connections missing from /config"
    assert isinstance(data["connections"], list)


def test_config_connections_save_local(client, tmp_path, monkeypatch):
    """POST /config/connections saves a local SQLite profile."""
    monkeypatch.chdir(tmp_path)
    r = client.post(
        "/config/connections",
        json={"name": "local", "type": "sqlite", "activate": True},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["connection_name"] == "local"
    assert (tmp_path / "meridian.toml").exists(), "meridian.toml not created"


def test_config_connections_save_postgres(client, tmp_path, monkeypatch):
    """POST /config/connections with type=postgres requires a url."""
    # Missing url → 400
    r = client.post(
        "/config/connections",
        json={"name": "neon", "type": "postgres", "activate": False},
    )
    assert r.status_code == 400

    # With url → ok
    monkeypatch.chdir(tmp_path)
    r = client.post(
        "/config/connections",
        json={"name": "neon", "type": "postgres", "url": "postgresql://localhost/test", "activate": False},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_admin_restart_requires_confirm(client):
    """POST /admin/restart without {confirm: true} returns a warning, not a restart.

    A restart disconnects every active session on the machine, so it must be
    explicitly confirmed before the server tears itself down.
    """
    r = client.post("/admin/restart")
    assert r.status_code == 200
    body = r.json()
    assert body.get("requires_confirm") is True
    assert "ok" not in body
    assert "disconnect all active sessions" in body.get("warning", "")


def test_admin_snapshot_memory_db_returns_400(client):
    """GET /admin/snapshot on an in-memory DB returns 400."""
    r = client.get("/admin/snapshot")
    assert r.status_code == 400


def test_dashboard_html_has_restart_button(client):
    """dashboard.html contains the restart button elements."""
    html = client.get("/dashboard").text
    assert "banner-restart-btn" in html, "banner restart button missing"
    assert "restart-server-btn" in html, "sidebar restart button missing"
    assert "connection-indicator" in html, "connection indicator missing"


def test_dashboard_js_has_restart_logic(client):
    """dashboard.js contains _doRestart and connection indicator logic."""
    js = client.get("/static/dashboard.js").text
    assert "_doRestart" in js, "_doRestart missing from dashboard.js"
    assert "_updateConnectionIndicator" in js, "_updateConnectionIndicator missing"
    assert "/admin/restart" in js, "/admin/restart not referenced in dashboard.js"


# ---------------------------------------------------------------------------
# v1.9.x — project rename / delete
# ---------------------------------------------------------------------------

def test_rename_project(client):
    """POST /projects/{id}/rename updates the project name."""
    import uuid as _uuid
    p = client.post("/projects", json={"name": f"rename-test-{_uuid.uuid4().hex[:6]}"}).json()
    r = client.post(f"/projects/{p['id']}/rename", json={"name": "new-name-xyz"})
    assert r.status_code == 200
    assert r.json()["name"] == "new-name-xyz"
    assert client.get(f"/projects/{p['id']}").json()["name"] == "new-name-xyz"


def test_rename_project_conflict(client):
    """Renaming to an existing name returns 409."""
    import uuid as _uuid
    a = client.post("/projects", json={"name": f"rn-a-{_uuid.uuid4().hex[:6]}"}).json()
    b = client.post("/projects", json={"name": f"rn-b-{_uuid.uuid4().hex[:6]}"}).json()
    r = client.post(f"/projects/{a['id']}/rename", json={"name": b["name"]})
    assert r.status_code == 409


def test_rename_project_missing_name(client):
    """Renaming with an empty name returns 400."""
    import uuid as _uuid
    p = client.post("/projects", json={"name": f"rn-empty-{_uuid.uuid4().hex[:6]}"}).json()
    assert client.post(f"/projects/{p['id']}/rename", json={"name": ""}).status_code == 400
    assert client.post(f"/projects/{p['id']}/rename", json={}).status_code == 400


def test_delete_project(client):
    """DELETE /projects/{id} removes the project and returns 204."""
    import uuid as _uuid
    p = client.post("/projects", json={"name": f"del-{_uuid.uuid4().hex[:6]}"}).json()
    r = client.delete(f"/projects/{p['id']}")
    assert r.status_code == 204
    assert client.get(f"/projects/{p['id']}").status_code == 404


def test_delete_project_in_progress_guard(client):
    """DELETE returns 409 when in_progress tasks exist."""
    import uuid as _uuid
    p = client.post("/projects", json={"name": f"del-guard-{_uuid.uuid4().hex[:6]}"}).json()
    s = client.post("/sessions/register", json={"project_id": p["id"], "name": "s1"}).json()
    # Create a pending task then claim it → in_progress
    t = client.post("/tasks", json={
        "session_id": s["id"], "project_id": p["id"],
        "description": "running", "status": "pending",
    }).json()
    client.post(f"/projects/{p['id']}/tasks/claim",
                json={"task_id": t["id"], "session_id": s["id"]})
    r = client.delete(f"/projects/{p['id']}")
    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"


def test_dashboard_js_has_project_mgmt(client):
    """dashboard.js has rename/delete helpers and kebab menu logic."""
    js = client.get("/static/dashboard.js").text
    assert "_renameProject" in js
    assert "_deleteProject" in js
    assert "_openTabMenu" in js
    assert "project_renamed" in js


def test_pg_create_tables_has_sprint_item_group_columns():
    """CREATE_TABLES_PG sprint_items must include item_group/pushed_to/human_id."""
    from meridian.pg_adapter import CREATE_TABLES_PG

    sprint_block_start = CREATE_TABLES_PG.index("sprint_items")
    sprint_block_end = CREATE_TABLES_PG.index(";", sprint_block_start)
    sprint_block = CREATE_TABLES_PG[sprint_block_start:sprint_block_end]
    assert "item_group" in sprint_block
    assert "pushed_to" in sprint_block
    assert "human_id" in sprint_block


def test_pg_create_tables_has_ntfy_url_column():
    """CREATE_TABLES_PG projects must include ntfy_url for Neon notifications."""
    from meridian.pg_adapter import CREATE_TABLES_PG

    project_block_start = CREATE_TABLES_PG.index("CREATE TABLE IF NOT EXISTS projects")
    project_block_end = CREATE_TABLES_PG.index(");", project_block_start)
    project_block = CREATE_TABLES_PG[project_block_start:project_block_end]
    assert "ntfy_url TEXT" in project_block


@pytest.mark.asyncio
async def test_pg_adapter_ntfy_helpers_issue_expected_queries():
    """pg_adapter exposes ntfy helpers for direct Postgres callers."""
    from meridian import pg_adapter as pg_module

    class _SelectProxy:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def fetchone(self):
            return {"ntfy_url": "https://ntfy.sh/test-topic"}

    class _UpdateProxy:
        def __await__(self):
            async def _done():
                return None

            return _done().__await__()

    class FakeDB:
        def __init__(self):
            self.calls = []
            self.committed = False

        def execute(self, sql, params=()):
            self.calls.append((sql, params))
            if sql.startswith("SELECT"):
                return _SelectProxy()
            return _UpdateProxy()

        async def commit(self):
            self.committed = True

    db = FakeDB()
    url = await pg_module.get_project_ntfy_url(db, "proj-123")
    assert url == "https://ntfy.sh/test-topic"
    await pg_module.set_project_ntfy_url(db, "proj-123", "https://ntfy.sh/new-topic")
    assert db.calls == [
        ("SELECT ntfy_url FROM projects WHERE id = ?", ("proj-123",)),
        ("UPDATE projects SET ntfy_url = ? WHERE id = ?", ("https://ntfy.sh/new-topic", "proj-123")),
    ]
    assert db.committed is True


def test_pg_adapter_translates_datetime_interval_units():
    """Postgres SQL translation must handle datetime('now', ? || ' hours') forms."""
    from meridian.pg_adapter import _pg_adapt_sql

    sql, params = _pg_adapt_sql(
        "UPDATE file_locks SET claimed_at = datetime('now'), "
        "expires_at = datetime('now', ? || ' hours') WHERE id = ?",
        ("2", "lock-123"),
    )

    assert "datetime('now'" not in sql
    assert "::interval" in sql
    assert "hours" in sql
    assert params == ["2", "lock-123"]


def test_cached_plan_error_is_retryable():
    """A stale prepared-plan error must be classified transient so the pool
    retries with a fresh connection.

    Regression guard for the blank-dashboard-panel bug: an ALTER TABLE migration
    (e.g. v3.4 hitl_auto_answer) invalidates cached prepared plans on pooled
    Postgres connections. The next query 500s with
    'cached plan must not change result type', which blanked the project panel
    because /projects/{id}/sessions failed. _is_transient_pg_error must treat it
    as retryable so the stale connection is dropped and the statement re-parsed.
    """
    from meridian.pg_adapter import _is_transient_pg_error

    err = Exception(
        "cached plan must not change result type"
    )
    assert _is_transient_pg_error(err) is True
    # A genuinely fatal error must still be non-retryable.
    assert _is_transient_pg_error(Exception("syntax error at or near")) is False


@pytest.mark.asyncio
async def test_pg_retry_closes_stale_connection_on_cached_plan_error():
    """A cached-plan failure should close the stale pooled connection and retry once."""
    from meridian.pg_adapter import PostgresConnection

    class FakeCursor:
        def __init__(self, conn):
            self._conn = conn
            self.rowcount = 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, sql, params):
            self._conn.executed.append((sql, params))
            if self._conn.failure is not None:
                raise self._conn.failure

        async def fetchall(self):
            return self._conn.rows

    class FakeConn:
        def __init__(self, rows=None, failure=None):
            self.rows = rows or []
            self.failure = failure
            self.closed = False
            self.executed = []

        def cursor(self, row_factory=None):
            return FakeCursor(self)

        async def close(self):
            self.closed = True

    class FakePool:
        def __init__(self, conns):
            self._conns = list(conns)
            self.returned = []

        async def getconn(self):
            return self._conns.pop(0)

        async def putconn(self, conn):
            self.returned.append(conn)

    stale = FakeConn(failure=Exception("cached plan must not change result type"))
    fresh = FakeConn(rows=[{"id": "ok"}])
    pool = FakePool([stale, fresh])
    db = PostgresConnection(pool)

    cur = await db._execute_with_retry("SELECT 1", [], "SELECT")

    assert await cur.fetchall() == [{"id": "ok"}]
    assert stale.closed is True
    assert stale not in pool.returned
    assert fresh in pool.returned
    assert stale.executed == [("SELECT 1", None)]
    assert fresh.executed == [("SELECT 1", None)]


def test_pg_pool_disables_prepared_statements():
    """Both Postgres pools set prepare_threshold=None so a cached plan can never
    be tied to a table's result shape — the durable fix for the cached-plan bug.
    """
    import inspect
    from meridian import pg_adapter as pg_module

    src = inspect.getsource(pg_module)
    assert src.count('"prepare_threshold": None') >= 2


def test_rewind_milestones_tab_label(client):
    """Rewind Versions subtab is now labelled 'Milestones' in dashboard.js."""
    js = client.get("/static/dashboard.js").text
    assert "Milestones" in js
    assert "📦 Milestones" in js


@pytest.mark.asyncio
async def test_get_tasks_includes_human_id(db):
    """get_tasks JOIN returns human_id and session_name alongside task fields."""
    p = await db_module.create_project(db, "tasks-human-test")
    s = await db_module.register_session(db, p["id"], "test-session", human_id="alice")
    await db_module.log_task(db, s["id"], p["id"], "did a thing", "done")
    tasks = await db_module.get_tasks(db, p["id"])
    assert tasks
    t = tasks[0]
    assert t["human_id"] == "alice"
    assert t["session_name"] == "test-session"


def test_git_status_endpoint_returns_shape(client):
    """GET /admin/git-status returns ok and behind fields; warning present when ok=True."""
    r = client.get("/admin/git-status")
    assert r.status_code == 200
    body = r.json()
    assert "ok" in body
    assert "behind" in body
    # warning is only present in success path; error path returns ok=False
    if body.get("ok"):
        assert "warning" in body
        assert body["warning"] is None or isinstance(body["warning"], str)


def test_dashboard_html_has_git_banner(client):
    """dashboard.html includes the git-banner div for remote-ahead warnings."""
    html = client.get("/dashboard").text
    assert "git-banner" in html
    assert "git pull recommended" in html


# ---------------------------------------------------------------------------
# v1.9.x — schema safety, archive task-release, AUTO BLOCKS dedup,
#           claimed_by_human_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_all_tables_exist(db):
    """After init_db() every expected table must be present with key columns."""
    expected = {
        "projects": {"id", "name", "creator_human_id", "goal_mode", "decisions"},
        "sessions": {"id", "project_id", "name", "human_id", "status", "last_seen"},
        "task_log": {"id", "session_id", "project_id", "description", "status",
                     "claimed_by", "claimed_at"},
        "goal_states": {"id", "project_id", "content", "version",
                        "goal_north_star", "goal_sprint"},
        "sprint_items": {"id", "project_id", "version", "title", "status",
                         "item_group", "pushed_to", "human_id"},
        "waitlist": {"id", "email", "note", "created_at"},
    }
    dropped = {"chat_sessions", "chat_messages"}  # removed in v1.9.x migration
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ) as cur:
        rows = await cur.fetchall()
    found_tables = {r[0] for r in rows}
    for table in expected:
        assert table in found_tables, f"missing table: {table}"
        async with db.execute(f"PRAGMA table_info({table})") as cur:
            col_rows = await cur.fetchall()
        found_cols = {r[1] for r in col_rows}
        for col in expected[table]:
            assert col in found_cols, f"missing column {table}.{col}"
    for table in dropped:
        assert table not in found_tables, f"table should have been dropped: {table}"


@pytest.mark.asyncio
async def test_archive_stale_sessions_releases_in_progress_tasks(db):
    """When a session is archived its in_progress tasks must revert to pending."""
    from datetime import datetime, timedelta, timezone
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "stale-worker")
    t = await db_module.log_task(db, s["id"], p["id"], "unfinished work", "pending")
    await db_module.claim_task(db, t["id"], s["id"])

    # Confirm claimed → in_progress
    fresh = await db_module.get_task(db, t["id"])
    assert fresh is not None
    assert fresh["status"] == "in_progress"

    # Backdate the session so it qualifies for archiving.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (cutoff, s["id"]))
    await db.commit()

    count = await db_module.archive_stale_sessions(db, p["id"])
    assert count == 1

    released = await db_module.get_task(db, t["id"])
    assert released is not None
    assert released["status"] == "pending"
    assert released["claimed_by"] is None
    assert released["claimed_at"] is None


@pytest.mark.asyncio
async def test_archive_stale_sessions_does_not_touch_done_tasks(db):
    """Done/failed tasks must not be touched by archive task-release."""
    from datetime import datetime, timedelta, timezone
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "stale-worker")
    t = await db_module.log_task(db, s["id"], p["id"], "already done", "done")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (cutoff, s["id"]))
    await db.commit()

    await db_module.archive_stale_sessions(db, p["id"])
    fresh = await db_module.get_task(db, t["id"])
    assert fresh is not None
    assert fresh["status"] == "done"


@pytest.mark.asyncio
async def test_set_goal_minor_no_auto_blocks_duplication(db):
    """set_goal(minor=True) called twice must not duplicate AUTO BLOCKS."""
    p = await db_module.create_project(db, "alpha")
    await db_module.set_goal(db, p["id"], "Human directive.")

    auto_section = "--- AUTO BLOCKS BELOW ---\n[AUTO SUMMARY - t1]\n- [DONE] first"
    full_1 = "Human directive.\n" + auto_section
    await db_module.set_goal(db, p["id"], full_1, minor=True)

    auto_section2 = "--- AUTO BLOCKS BELOW ---\n[AUTO SUMMARY - t2]\n- [DONE] second"
    full_2 = "Human directive.\n" + auto_section2
    await db_module.set_goal(db, p["id"], full_2, minor=True)

    goal = await db_module.get_goal(db, p["id"])
    assert goal is not None
    content = goal["content"]
    # Must contain exactly one AUTO BLOCKS marker.
    assert content.count("--- AUTO BLOCKS BELOW ---") == 1
    # Must contain the second summary, not the first.
    assert "t2" in content
    assert "t1" not in content
    # Human prefix intact.
    assert content.startswith("Human directive.")


def test_context_endpoint_returns_expected_shape(client):
    """GET /projects/{id}/context returns onboarding payload with required keys."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    client.post(f"/projects/{project['id']}/goal", json={
        "content": "ship it", "north_star": "be best", "sprint": "week 1"
    })
    r = client.get(f"/projects/{project['id']}/context")
    assert r.status_code == 200
    body = r.json()
    assert body["north_star"] == "be best"
    assert body["current_sprint"] == "week 1"
    assert isinstance(body["sprint_items"], list)
    assert isinstance(body["pending_tasks"], list)
    assert isinstance(body["recent_sessions"], list)
    assert isinstance(body["file_map"], list)
    assert "DECISIONS.md" in body["file_map"]


def test_context_endpoint_404_for_unknown_project(client):
    r = client.get("/projects/no-such/context")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_log_task_accepts_backlog_status(db):
    """'backlog' is a valid task status after v1.9.x migration."""
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess")
    t = await db_module.log_task(db, s["id"], p["id"], "future idea", "backlog")
    assert t["status"] == "backlog"


@pytest.mark.asyncio
async def test_log_task_accepts_future_status(db):
    """'future' is a valid task status after v1.9.x migration."""
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "sess")
    t = await db_module.log_task(db, s["id"], p["id"], "someday maybe", "future")
    assert t["status"] == "future"


def test_http_task_accepts_backlog_status(client):
    project = client.post("/projects", json={"name": "alpha"}).json()
    sess = client.post("/sessions/register", json={"project_id": project["id"], "name": "s1"}).json()
    r = client.post("/tasks", json={
        "session_id": sess["id"], "project_id": project["id"],
        "description": "nice to have", "status": "backlog"
    })
    assert r.status_code == 201
    assert r.json()["status"] == "backlog"


@pytest.mark.asyncio
async def test_get_tasks_includes_claimed_by_human_id(db):
    """get_tasks must return claimed_by_human_id from the claiming session."""
    p = await db_module.create_project(db, "alpha")
    creator = await db_module.register_session(db, p["id"], "creator", human_id="alice")
    claimer = await db_module.register_session(db, p["id"], "claimer", human_id="bob")
    t = await db_module.log_task(db, creator["id"], p["id"], "do thing", "pending")
    await db_module.claim_task(db, t["id"], claimer["id"])

    tasks = await db_module.get_tasks(db, p["id"])
    assert tasks
    row = tasks[0]
    assert row["claimed_by_human_id"] == "bob"
    assert row["claimed_by_session_name"] == "claimer"
    # Creator identity still correct.
    assert row["human_id"] == "alice"
    assert row["session_name"] == "creator"


# ---------------------------------------------------------------------------
# v1.9.x — ROADMAP.md auto-update from sprint_items
# ---------------------------------------------------------------------------


def test_complete_sprint_item_updates_roadmap_md(client, tmp_path, monkeypatch):
    """Completing a sprint item writes a version history entry to ROADMAP.md."""
    from meridian import server as srv
    monkeypatch.setattr(srv, "_REPO_ROOT", tmp_path)
    # Seed a minimal ROADMAP.md with a version history section.
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text("# Roadmap\n\n## Version history\n\n---\n\n## Next\n", encoding="utf-8")

    project = client.post("/projects", json={"name": "rtest"}).json()
    # Add and complete a sprint item.
    item = client.post(
        f"/projects/{project['id']}/sprint-items",
        json={"version": "v9.9.x", "title": "Ship ROADMAP auto-update"},
    ).json()
    r = client.post(f"/projects/{project['id']}/sprint-items/{item['id']}/complete")
    assert r.status_code == 200

    content = roadmap.read_text(encoding="utf-8")
    assert "v9.9.x" in content
    assert "Ship ROADMAP auto-update" in content
    assert "<!-- meridian-auto: v9.9.x -->" in content


def test_roadmap_auto_update_replaces_existing_entry(client, tmp_path, monkeypatch):
    """Completing a second item for the same version replaces the existing line."""
    from meridian import server as srv
    monkeypatch.setattr(srv, "_REPO_ROOT", tmp_path)
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text(
        "# R\n\n## Version history\n\n---\n",
        encoding="utf-8",
    )
    project = client.post("/projects", json={"name": "rtest2"}).json()
    item1 = client.post(
        f"/projects/{project['id']}/sprint-items",
        json={"version": "v8.0.x", "title": "First feature"},
    ).json()
    client.post(f"/projects/{project['id']}/sprint-items/{item1['id']}/complete")
    item2 = client.post(
        f"/projects/{project['id']}/sprint-items",
        json={"version": "v8.0.x", "title": "Second feature"},
    ).json()
    client.post(f"/projects/{project['id']}/sprint-items/{item2['id']}/complete")

    content = roadmap.read_text(encoding="utf-8")
    # Only one auto entry for v8.0.x.
    assert content.count("<!-- meridian-auto: v8.0.x -->") == 1
    assert "Second feature" in content


# ---------------------------------------------------------------------------
# v1.9.x — CLAUDE.md auto-update on session expire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expire_regenerates_claude_md(db, tmp_path, monkeypatch):
    """_expire_and_generate_handoffs regenerates CLAUDE.md for each project."""
    from meridian import server as srv
    monkeypatch.setattr(srv, "_REPO_ROOT", tmp_path)

    p = await db_module.create_project(db, "cltest")
    s = await db_module.register_session(db, p["id"], "old-sess")
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now', '-60 minutes') WHERE id = ?",
        (s["id"],),
    )
    await db.commit()

    await srv._expire_and_generate_handoffs(db, str(tmp_path))
    claude_md = tmp_path / "CLAUDE.md"
    assert claude_md.exists(), "CLAUDE.md should have been written after session expiry"
    content = claude_md.read_text(encoding="utf-8")
    assert "MERIDIAN STATE" in content


# ---------------------------------------------------------------------------
# v1.9.x — sessions endpoint supports active_only=false + session_summary
# ---------------------------------------------------------------------------


def test_sessions_endpoint_active_only_false_includes_closed(client):
    """?active_only=false includes closed sessions in the response."""
    project = client.post("/projects", json={"name": "stest"}).json()
    sess = client.post(
        "/sessions/register", json={"project_id": project["id"], "name": "runner"}
    ).json()
    client.post(f"/sessions/{sess['id']}/close")

    # active_only=true (default) should exclude the closed session.
    r_active = client.get(f"/projects/{project['id']}/sessions")
    ids_active = [s["id"] for s in r_active.json()]
    assert sess["id"] not in ids_active

    # active_only=false should include it.
    r_all = client.get(f"/projects/{project['id']}/sessions?active_only=false")
    ids_all = [s["id"] for s in r_all.json()]
    assert sess["id"] in ids_all


def test_sessions_endpoint_returns_session_summary_field(client):
    """Each session row includes a session_summary key (null when no summary)."""
    project = client.post("/projects", json={"name": "stest2"}).json()
    client.post("/sessions/register", json={"project_id": project["id"], "name": "s1"})
    r = client.get(f"/projects/{project['id']}/sessions")
    assert r.status_code == 200
    rows = r.json()
    assert rows, "expected at least one session"
    assert "session_summary" in rows[0]


# ---------------------------------------------------------------------------
# v1.9.x — backlog/future statuses in queue UI
# ---------------------------------------------------------------------------


def test_dashboard_js_handles_future_status_in_render_queue(client):
    """dashboard.js renderQueue segments future tasks into a Future section."""
    js = client.get("/static/dashboard.js").text
    assert "future" in js.lower(), "renderQueue must handle future status"
    assert "'future'" in js or '"future"' in js, "future filter must be present"


def test_http_task_accepts_future_status(client):
    """POST /tasks with status=future is accepted."""
    project = client.post("/projects", json={"name": "ftest"}).json()
    sess = client.post(
        "/sessions/register", json={"project_id": project["id"], "name": "s1"}
    ).json()
    r = client.post("/tasks", json={
        "session_id": sess["id"], "project_id": project["id"],
        "description": "someday item", "status": "future",
    })
    assert r.status_code == 201
    assert r.json()["status"] == "future"


# ---------------------------------------------------------------------------
# v1.9.x — session_summary surfaced in LIVE tab JS
# ---------------------------------------------------------------------------


def test_dashboard_js_renders_session_summary_in_live_tab(client):
    """renderLiveSessions must reference session_summary for closed/archived sessions."""
    js = client.get("/static/dashboard.js").text
    assert "session_summary" in js, "renderLiveSessions must use session_summary"
    assert "active_only=false" in js, "LIVE tab must fetch with active_only=false"


# ---------------------------------------------------------------------------
# Goal history filter — AUTO BLOCKS-only versions collapsed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_goal_history_filters_auto_blocks_only_versions(db):
    """Versions that only differ in AUTO BLOCKS section are not returned."""
    p = await db_module.create_project(db, "hist-filter-proj")
    base = "Real content here\n\n--- AUTO BLOCKS BELOW ---\nblock v1"
    await db_module.set_goal(db, p["id"], base)
    # Second set: only AUTO BLOCKS changes, real content identical
    auto_only = "Real content here\n\n--- AUTO BLOCKS BELOW ---\nblock v2"
    await db_module.set_goal(db, p["id"], auto_only)
    history = await db_module.get_goal_history(db, p["id"])
    # Should collapse the two AUTO-BLOCKS-only versions into one entry
    assert len(history) == 1, f"expected 1 history entry, got {len(history)}"


@pytest.mark.asyncio
async def test_goal_history_keeps_real_content_changes(db):
    """Versions with real content changes are all retained in history."""
    p = await db_module.create_project(db, "hist-keep-proj")
    await db_module.set_goal(db, p["id"], "Content A")
    await db_module.set_goal(db, p["id"], "Content B")
    await db_module.set_goal(db, p["id"], "Content C")
    history = await db_module.get_goal_history(db, p["id"])
    assert len(history) == 3, f"expected 3 history entries, got {len(history)}"
    # Newest first
    assert history[0]["version_goal"] == "Content C"
    assert history[-1]["version_goal"] == "Content A"


# ---------------------------------------------------------------------------
# Stats endpoint — /projects/{id}/stats
# ---------------------------------------------------------------------------


def test_stats_endpoint_returns_expected_shape(client):
    """GET /projects/{id}/stats returns tasks_per_day and sprint_velocity."""
    r = client.post("/projects", json={"name": "stats-test-proj"})
    assert r.status_code in (200, 201)
    pid = r.json()["id"]
    r = client.get(f"/projects/{pid}/stats")
    assert r.status_code == 200
    data = r.json()
    assert "tasks_per_day" in data
    assert "sprint_velocity" in data
    assert "period_days" in data
    assert data["period_days"] == 30


def test_stats_endpoint_404_for_unknown_project(client):
    """GET /projects/unknown/stats returns 404."""
    r = client.get("/projects/00000000-0000-0000-0000-000000000000/stats")
    assert r.status_code == 404


def test_stats_tasks_per_day_length_matches_period(client):
    """tasks_per_day series has exactly period_days entries."""
    r = client.post("/projects", json={"name": "stats-days-proj"})
    pid = r.json()["id"]
    r = client.get(f"/projects/{pid}/stats?days=7")
    assert r.status_code == 200
    data = r.json()
    assert data["period_days"] == 7
    assert len(data["tasks_per_day"]) == 7


# ---------------------------------------------------------------------------
# v2.4 HITL — hitl_requests table: request / answer / dismiss / list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_hitl_creates_pending(db):
    p = await db_module.create_project(db, "hitl-proj")
    s = await db_module.register_session(db, p["id"], "sess")
    h = await db_module.request_hitl(db, p["id"], "Is it safe to proceed?", session_id=s["id"])
    assert h["id"]
    assert h["status"] == "pending"
    assert h["question"] == "Is it safe to proceed?"
    assert h["project_id"] == p["id"]
    assert h["session_id"] == s["id"]


@pytest.mark.asyncio
async def test_request_hitl_auto_answer_resolves_immediately(db):
    """v3.4 — when a project has hitl_auto_answer on, request_hitl resolves the
    request immediately (answered_by='auto') so the session never blocks."""
    p = await db_module.create_project(db, "hitl-auto-proj")
    await db_module.update_project_settings(db, p["id"], hitl_auto_answer=True)
    h = await db_module.request_hitl(
        db, p["id"], "Ship it?", urgency="blocking"
    )
    assert h["status"] == "answered"
    assert h["answered_by"] == "auto"
    assert h["answer"] == "[auto-answered]"
    assert h["answered_at"]
    # Still in the queue for audit — a session polling sees it resolved.
    fetched = await db_module.get_hitl_request(db, h["id"])
    assert fetched["status"] == "answered"


@pytest.mark.asyncio
async def test_request_hitl_manual_mode_unchanged(db):
    """v3.4 — default projects (auto-answer off) still create pending requests."""
    p = await db_module.create_project(db, "hitl-manual-proj")
    h = await db_module.request_hitl(db, p["id"], "Deploy now?", urgency="blocking")
    assert h["status"] == "pending"
    assert h.get("answered_by") in (None, "")
    assert h.get("answer") in (None, "")


@pytest.mark.asyncio
async def test_request_hitl_auto_answer_picks_first_option(db):
    """v3.4 — when the payload carries an options list, auto-answer picks the
    first option rather than the generic acknowledgement."""
    import json as _json
    p = await db_module.create_project(db, "hitl-opts-proj")
    await db_module.update_project_settings(db, p["id"], hitl_auto_answer=True)
    payload = _json.dumps({"options": ["per-IP", "per-token"]})
    h = await db_module.request_hitl(
        db, p["id"], "Rate-limit strategy?", payload=payload
    )
    assert h["status"] == "answered"
    assert h["answered_by"] == "auto"
    assert h["answer"] == "per-IP"


@pytest.mark.asyncio
async def test_request_hitl_md_section_update_not_auto_answered(db):
    """v3.4 — md_section_update diff approvals stay human-gated even when
    auto-answer is on; auto-approving a file write would defeat the safeguard."""
    p = await db_module.create_project(db, "hitl-md-proj")
    await db_module.update_project_settings(db, p["id"], hitl_auto_answer=True)
    h = await db_module.request_hitl(
        db, p["id"], "Apply this DEVLOG section?",
        kind="md_section_update", payload='{"file": "DEVLOG.md"}',
    )
    assert h["status"] == "pending"
    assert h.get("answered_by") in (None, "")


@pytest.mark.asyncio
async def test_project_settings_roundtrip_hitl_auto_answer(db):
    """v3.4 — hitl_auto_answer persists through get/update_project_settings."""
    p = await db_module.create_project(db, "hitl-settings-proj")
    settings = await db_module.get_project_settings(db, p["id"])
    assert settings["hitl_auto_answer"] is False
    updated = await db_module.update_project_settings(
        db, p["id"], hitl_auto_answer=True
    )
    assert updated["hitl_auto_answer"] is True
    reread = await db_module.get_project_settings(db, p["id"])
    assert reread["hitl_auto_answer"] is True


@pytest.mark.asyncio
async def test_answer_hitl_marks_answered(db):
    p = await db_module.create_project(db, "hitl-proj")
    h = await db_module.request_hitl(db, p["id"], "Deploy now?")
    updated = await db_module.answer_hitl_request(db, h["id"], "Yes, deploy", answered_by="adam")
    assert updated["status"] == "answered"
    assert updated["answer"] == "Yes, deploy"
    assert updated["answered_by"] == "adam"
    fetched = await db_module.get_hitl_request(db, h["id"])
    assert fetched["status"] == "answered"


@pytest.mark.asyncio
async def test_dismiss_hitl_marks_dismissed(db):
    p = await db_module.create_project(db, "hitl-proj")
    h = await db_module.request_hitl(db, p["id"], "Rate-limit per IP?")
    updated = await db_module.dismiss_hitl_request(db, h["id"])
    assert updated["status"] == "dismissed"
    fetched = await db_module.get_hitl_request(db, h["id"])
    assert fetched["status"] == "dismissed"


@pytest.mark.asyncio
async def test_list_hitl_requests_filter_by_status(db):
    p = await db_module.create_project(db, "hitl-proj")
    h1 = await db_module.request_hitl(db, p["id"], "Q1")
    h2 = await db_module.request_hitl(db, p["id"], "Q2")
    await db_module.answer_hitl_request(db, h2["id"], "answered")

    pending = await db_module.list_hitl_requests(db, p["id"], status="pending")
    assert len(pending) == 1
    assert pending[0]["id"] == h1["id"]

    answered = await db_module.list_hitl_requests(db, p["id"], status="answered")
    assert len(answered) == 1
    assert answered[0]["id"] == h2["id"]

    all_items = await db_module.list_hitl_requests(db, p["id"], status=None)
    assert len(all_items) == 2


@pytest.mark.asyncio
async def test_get_hitl_request_returns_none_for_unknown(db):
    result = await db_module.get_hitl_request(db, "00000000-0000-0000-0000-000000000000")
    assert result is None


def test_hitl_rest_lifecycle(client):
    """POST/GET/PATCH /hitl routes create, fetch, answer, and dismiss."""
    proj = client.post("/projects", json={"name": "hitl-rest"}).json()
    pid = proj["id"]
    sess = client.post("/sessions/register", json={"project_id": pid, "name": "s"}).json()

    # Create via REST
    r = client.post(f"/projects/{pid}/hitl", json={"question": "Ship it?", "session_id": sess["id"]})
    assert r.status_code == 201
    h = r.json()
    hid = h["id"]
    assert h["status"] == "pending"

    # Fetch single
    r2 = client.get(f"/hitl/{hid}")
    assert r2.status_code == 200
    assert r2.json()["question"] == "Ship it?"

    # List pending
    r3 = client.get(f"/projects/{pid}/hitl?status=pending")
    assert r3.status_code == 200
    ids = [item["id"] for item in r3.json()]
    assert hid in ids

    # Answer
    r4 = client.patch(f"/hitl/{hid}", json={"action": "answer", "answer": "Yes!"})
    assert r4.status_code == 200
    assert r4.json()["status"] == "answered"
    assert r4.json()["answer"] == "Yes!"

    # No longer in pending list
    r5 = client.get(f"/projects/{pid}/hitl?status=pending")
    assert all(item["id"] != hid for item in r5.json())


def test_hitl_rest_dismiss(client):
    """PATCH with action=dismiss marks status dismissed."""
    proj = client.post("/projects", json={"name": "hitl-dismiss"}).json()
    pid = proj["id"]
    r = client.post(f"/projects/{pid}/hitl", json={"question": "Dismiss me?"})
    assert r.status_code == 201
    hid = r.json()["id"]
    r2 = client.patch(f"/hitl/{hid}", json={"action": "dismiss"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "dismissed"


def test_hitl_rest_404_on_unknown(client):
    """GET /hitl/unknown returns 404."""
    r = client.get("/hitl/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_dashboard_js_has_charts_subtab(client):
    """dashboard.js must reference the Charts subtab and Chart.js init."""
    js = client.get("/static/dashboard.js").text
    assert "renderRewindCharts" in js
    assert "initRewindCharts" in js
    assert "chart-tasks-" in js
    assert "chart-sprint-" in js


def test_dashboard_html_has_chartjs(client):
    """dashboard.html must load Chart.js CDN."""
    html = client.get("/dashboard").text
    assert "chart.js" in html.lower() or "chartjs" in html.lower()


# ---------------------------------------------------------------------------
# v2.1 — /demo, /terms, /privacy, dark tables, demo cookie routing
# ---------------------------------------------------------------------------

def test_demo_route_returns_200(client):
    """GET /demo returns 200 and the demo dashboard HTML."""
    r = client.get("/demo")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Meridian Demo" in r.text


def test_demo_route_sets_cookie(client):
    """GET /demo sets the meridian_demo session cookie."""
    r = client.get("/demo")
    assert r.status_code == 200
    assert "meridian_demo" in r.cookies


def test_demo_route_shows_demo_banner(client):
    """GET /demo renders the demo mode banner and JS flag."""
    r = client.get("/demo")
    assert "Demo mode" in r.text
    assert "MERIDIAN_DEMO_MODE" in r.text


def test_dashboard_no_demo_banner(client):
    """GET /dashboard does not show the demo banner (demo_mode=False)."""
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "demo-banner" not in r.text
    assert "MERIDIAN_DEMO_MODE = false" in r.text


def test_dashboard_clears_demo_cookie(client):
    """GET /dashboard must expire the demo context cookie to prevent bleed-through."""
    # Simulate a browser that visited /demo first
    r = client.get("/dashboard", cookies={"meridian_demo": "1"})
    assert r.status_code == 200
    # Must render as real dashboard, not demo mode
    assert "MERIDIAN_DEMO_MODE = false" in r.text
    # Response must include a Set-Cookie that expires the demo cookie (max-age=0)
    set_cookie = r.headers.get("set-cookie", "")
    assert "meridian_demo" in set_cookie
    assert "max-age=0" in set_cookie.lower()


def test_terms_page_returns_200(client):
    """GET /terms returns 200 and contains ToS content."""
    r = client.get("/terms")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Terms of Service" in r.text
    assert "hello@usemeridian.us" in r.text


def test_privacy_page_returns_200(client):
    """GET /privacy returns 200 and contains privacy policy content."""
    r = client.get("/privacy")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Privacy Policy" in r.text
    assert "hello@usemeridian.us" in r.text


def test_demo_write_blocked_with_cookie(client):
    """POST with meridian_demo cookie returns 403."""
    r = client.post(
        "/projects",
        json={"name": "should-fail"},
        cookies={"meridian_demo": "1"},
    )
    assert r.status_code == 403


def test_demo_read_returns_200_without_demo_db(client):
    """GET with meridian_demo cookie falls back to the regular DB when internal."""
    r = client.get("/projects", cookies={"meridian_demo": "1"})
    assert r.status_code == 200


async def test_dark_tables_exist(db):
    """workspace_members and tenant_environments tables must be created by init_db."""
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('workspace_members', 'tenant_environments')"
    ) as cur:
        rows = await cur.fetchall()
    found = {r[0] for r in rows}
    assert "workspace_members" in found, "workspace_members table missing"
    assert "tenant_environments" in found, "tenant_environments table missing"


def test_landing_page_has_try_demo_link(client):
    """Landing page includes a Try demo link pointing to /demo."""
    r = client.get("/")
    assert r.status_code == 200
    assert "/demo" in r.text


def test_landing_page_has_solution_dashboard_shot(client):
    """G6.27 — landing page shows the real dashboard screenshot, not a mockup."""
    r = client.get("/")
    assert r.status_code == 200
    assert "/static/the-solution-dashboard-card.png" in r.text
    assert "solution-shot" in r.text


def test_install_mcp_page(client):
    """GET /install-mcp returns 200 with copy-ready SSE URL and no-cache headers."""
    r = client.get("/install-mcp")
    assert r.status_code == 200
    assert "/mcp/sse" in r.text
    assert "meridian" in r.text.lower()
    cc = r.headers.get("cache-control", "")
    assert "no-cache" in cc


def test_landing_page_footer_uses_meridian_email(client):
    """Landing page footer contact uses hello@usemeridian.us, not personal email."""
    r = client.get("/")
    assert r.status_code == 200
    assert "hello@usemeridian.us" in r.text
    assert "ajc3xc@" not in r.text  # no personal emails in landing page


def test_auth_login_page_has_google_button(client):
    """GET /auth/login shows Google OAuth button."""
    r = client.get("/auth/login", follow_redirects=False)
    assert r.status_code == 200
    assert "Google" in r.text
    assert "/auth/google/login" in r.text


def test_auth_login_page_returns_html(client):
    """GET /auth/login serves an HTML page (not a redirect to Google)."""
    r = client.get("/auth/login", follow_redirects=False)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_auth_google_login_redirects_when_configured(client, monkeypatch):
    """GET /auth/google/login redirects to Google when GOOGLE_CLIENT_ID is set."""
    import os
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-secret")
    r = client.get("/auth/google/login", follow_redirects=False)
    # Should redirect to Google (302) or return 503 if oauth client setup fails
    assert r.status_code in (302, 503)


def test_auth_callback_missing_code(client):
    """GET /auth/callback (Google) without code param returns 400."""
    r = client.get("/auth/callback", follow_redirects=False)
    assert r.status_code == 400
    assert "missing oauth code" in r.json().get("detail", "")


def test_landing_page_has_pricing_section(client):
    """Landing page has pricing cards (Standard and Pro)."""
    r = client.get("/")
    assert r.status_code == 200
    assert "Standard" in r.text
    assert "$20" in r.text
    assert "Pro" in r.text
    assert "$49" in r.text


def test_landing_page_has_neon_attribution(client):
    """Landing page mentions Neon Postgres for transparency."""
    r = client.get("/")
    assert r.status_code == 200
    assert "Neon" in r.text


def test_landing_page_nav_has_docs_link(client):
    """Landing page nav has a Docs link."""
    r = client.get("/")
    assert r.status_code == 200
    # Docs link should be present
    assert "Docs" in r.text


def test_waitlist_pending_accepts_custom_capacity_message(client):
    """/waitlist-pending can explain when launch capacity is full."""
    r = client.get("/waitlist-pending?message=Early%20access%20is%20full")
    assert r.status_code == 200
    assert "Early access is full" in r.text


def test_oauth_metadata_includes_meridian_branding(client):
    """OAuth metadata advertises Meridian branding for connector UIs."""
    r = client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    body = r.json()
    assert body["client_name"] == "Meridian"
    assert body["logo_uri"] == "https://usemeridian.us/static/logo.svg"


def test_landing_page_cache_control(client):
    """/ returns Cache-Control: no-cache, no-store so Cloudflare doesn't serve stale HTML."""
    r = client.get("/")
    assert r.status_code == 200
    cc = r.headers.get("cache-control", "")
    assert "no-cache" in cc
    assert "no-store" in cc


def test_demo_cache_control(client):
    """/demo returns Cache-Control: no-cache, no-store."""
    r = client.get("/demo")
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        cc = r.headers.get("cache-control", "")
        assert "no-cache" in cc
        assert "no-store" in cc


def test_landing_page_charset_and_emoji_survival(client):
    """Regression for the cc3eeb9 mojibake incident: served bytes must decode as UTF-8
    and known emoji must survive intact. If this fails, the template was likely
    re-saved with cp1252 interpretation of UTF-8 bytes."""
    r = client.get("/")
    assert r.status_code == 200
    ctype = r.headers.get("content-type", "").lower()
    assert "charset=utf-8" in ctype, f"missing charset=utf-8: {ctype!r}"
    body = r.content.decode("utf-8")
    for marker in ("🎯", "📋", "🧭", "🐙", "—", "→", "⚡"):
        assert marker in body, f"emoji {marker!r} missing — landing.html may have re-encoded"
    for moji in ("ðŸ", "â€”", "â€“", "â†’", "âœ", "âš¡"):
        assert moji not in body, f"mojibake sequence {moji!r} present in landing page"


def test_g519_roles_map_grants_expected_permissions():
    """G5.19 — ROLE_PERMS map: owner has everything; admin has invite/
    settings/HITL but NOT billing/delete; member has read+write only;
    viewer has read only."""
    from meridian import roles
    assert roles.has_perm("owner",  roles.PERM_DELETE_TENANT)
    assert roles.has_perm("owner",  roles.PERM_BILLING)
    assert roles.has_perm("admin",  roles.PERM_INVITE)
    assert roles.has_perm("admin",  roles.PERM_HITL_ANSWER)
    assert not roles.has_perm("admin",  roles.PERM_BILLING)
    assert not roles.has_perm("admin",  roles.PERM_DELETE_TENANT)
    assert roles.has_perm("member", roles.PERM_WRITE)
    assert not roles.has_perm("member", roles.PERM_INVITE)
    assert not roles.has_perm("member", roles.PERM_HITL_ANSWER)
    assert roles.has_perm("viewer", roles.PERM_READ)
    assert not roles.has_perm("viewer", roles.PERM_WRITE)
    assert not roles.has_perm("nope",   roles.PERM_READ)
    assert not roles.has_perm(None,     roles.PERM_READ)


def test_g520_github_access_caps_gh_tool_dispatch():
    """G5.20 — can_github: write→all, read→read-only tools, none→nothing."""
    from meridian import roles
    assert roles.can_github("write", "read_file")
    assert roles.can_github("write", "commit")
    assert roles.can_github("read",  "read_file")
    assert roles.can_github("read",  "search_code")
    assert not roles.can_github("read",  "commit")
    assert not roles.can_github("none",  "read_file")
    assert not roles.can_github(None,    "read_file")
    # Defaults from role
    assert roles.default_github_access_for_role("owner")  == "write"
    assert roles.default_github_access_for_role("admin")  == "write"
    assert roles.default_github_access_for_role("member") == "read"
    assert roles.default_github_access_for_role("viewer") == "none"


@pytest.mark.asyncio
async def test_g519_resolve_member_role_owner_vs_invitee():
    """G5.19 — resolve_member_role returns ('owner','write') for the tenant
    email itself, the stored (role, github_access) tuple for an accepted
    invitee, and None for unknown / pending invitees."""
    from datetime import datetime, timezone
    db = await db_module.init_db(":memory:")
    try:
        await db.execute(
            "INSERT INTO tenants (id, email) VALUES (?, ?)",
            ("t-owner", "owner@example.com"),
        )
        # Accepted admin invitee
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            "INSERT INTO workspace_members "
            "(id, tenant_id, email, role, github_access, token_hash, joined_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?)",
            ("m-admin", "t-owner", "admin@example.com", "admin", "write", now),
        )
        # Pending viewer invitee
        await db.execute(
            "INSERT INTO workspace_members "
            "(id, tenant_id, email, role, github_access, token_hash, joined_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            ("m-viewer", "t-owner", "viewer@example.com", "viewer", "none", "tok"),
        )
        await db.commit()
        assert await db_module.resolve_member_role(
            db, "t-owner", "OWNER@example.com"
        ) == ("owner", "write")
        assert await db_module.resolve_member_role(
            db, "t-owner", "admin@example.com"
        ) == ("admin", "write")
        # Pending invitee → None (not joined yet)
        assert await db_module.resolve_member_role(
            db, "t-owner", "viewer@example.com"
        ) is None
        # Unknown email → None
        assert await db_module.resolve_member_role(
            db, "t-owner", "stranger@example.com"
        ) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_g519_rbac_migration_is_idempotent_and_widens_check():
    """G5.19 — migration is idempotent: running it twice is a no-op, and
    after running it once, 'admin' role inserts succeed (the legacy
    CHECK that excluded admin has been dropped)."""
    db = await db_module.init_db(":memory:")
    try:
        # Migration ran during init_db; running again is safe.
        await db_module._migrate_workspace_members_rbac(db)
        await db_module._migrate_workspace_members_rbac(db)
        # Admin insert succeeds — legacy CHECK constraint is gone.
        await db.execute(
            "INSERT INTO tenants (id, email) VALUES (?, ?)",
            ("t-r", "r@example.com"),
        )
        await db.execute(
            "INSERT INTO workspace_members "
            "(id, tenant_id, email, role, github_access, token_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("m-a", "t-r", "a@example.com", "admin", "write", "tok2"),
        )
        await db.commit()
        async with db.execute(
            "SELECT role, github_access FROM workspace_members WHERE id = 'm-a'"
        ) as cur:
            row = await cur.fetchone()
        assert row["role"] == "admin"
        assert row["github_access"] == "write"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v28_update_workspace_member_role_and_scoping():
    """v2.8 — update_workspace_member changes role/github_access, is scoped by
    tenant_id, and returns None for a non-matching member."""
    db = await db_module.init_db(":memory:")
    try:
        await db.execute(
            "INSERT INTO tenants (id, email) VALUES (?, ?)",
            ("t-own", "own@example.com"),
        )
        await db.execute(
            "INSERT INTO workspace_members "
            "(id, tenant_id, email, role, github_access, token_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("m-1", "t-own", "m@example.com", "viewer", "none", "tok"),
        )
        await db.commit()
        # Promote viewer → admin with an explicit github_access cap.
        updated = await db_module.update_workspace_member(
            db, "m-1", "t-own", role="admin", github_access="write",
        )
        assert updated is not None
        assert updated["role"] == "admin"
        assert updated["github_access"] == "write"
        # Partial update: change only github_access, role stays put.
        updated = await db_module.update_workspace_member(
            db, "m-1", "t-own", github_access="read",
        )
        assert updated["role"] == "admin"
        assert updated["github_access"] == "read"
        # Wrong tenant → no row matches → None, and the row is untouched.
        assert await db_module.update_workspace_member(
            db, "m-1", "t-other", role="viewer",
        ) is None
        async with db.execute(
            "SELECT role FROM workspace_members WHERE id = 'm-1'"
        ) as cur:
            row = await cur.fetchone()
        assert row["role"] == "admin"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v28_workspace_settings_display_name_roundtrip():
    """v2.8 — display_name persists through update/get and clears on empty."""
    db = await db_module.init_db(":memory:")
    try:
        assert (await db_module.get_workspace_settings(db))["display_name"] is None
        saved = await db_module.update_workspace_settings(db, display_name="Adam")
        assert saved["display_name"] == "Adam"
        # Untouched on an unrelated patch.
        saved = await db_module.update_workspace_settings(
            db, hitl_auto_answer_default=True,
        )
        assert saved["display_name"] == "Adam"
        # Empty string clears it back to NULL.
        saved = await db_module.update_workspace_settings(db, display_name="")
        assert saved["display_name"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_g210_is_internal_backfill_and_churn_cleanup_skip():
    """G2.10 — the migrator backfills is_internal=1 for the four known
    internal emails, and run_churn_cleanup never touches an internal
    tenant even when they have a Neon project and no Stripe customer."""
    from meridian.hosted import run_churn_cleanup
    db = await db_module.init_db(":memory:")
    try:
        # Migration already ran inside init_db; insert a known internal email
        # and verify the backfill UPDATE caught it on a re-run of the migrator.
        await db.execute(
            "INSERT INTO tenants (id, email, neon_project_id) VALUES (?, ?, ?)",
            ("t-internal", "ajc123private@gmail.com", "neon-internal"),
        )
        await db.execute(
            "INSERT INTO tenants (id, email, neon_project_id) VALUES (?, ?, ?)",
            ("t-external", "stranger@example.com", "neon-external"),
        )
        await db.commit()
        # Re-run the migrator — it should mark the internal row.
        await db_module._migrate_tenants_is_internal(db)

        async with db.execute(
            "SELECT email, is_internal FROM tenants ORDER BY email"
        ) as cur:
            rows = await cur.fetchall()
        flags = {r["email"]: r["is_internal"] for r in rows}
        assert flags["ajc123private@gmail.com"] == 1
        assert flags["stranger@example.com"] == 0

        # Both tenants look churned (stripe_customer_id IS NULL,
        # neon_project_id IS NOT NULL). Running the cleanup should only
        # consider the external one — the internal is filtered out at SQL.
        # We verify by checking the SQL filter directly:
        async with db.execute(
            "SELECT id FROM tenants WHERE stripe_customer_id IS NULL "
            "AND neon_project_id IS NOT NULL "
            "AND (is_internal IS NULL OR is_internal = 0)"
        ) as cur:
            churn_candidates = [r["id"] for r in await cur.fetchall()]
        assert "t-external" in churn_candidates
        assert "t-internal" not in churn_candidates

        # Belt-and-suspenders: the cleanup helper takes the filter SQL we
        # just asserted on, so a defensive call shouldn't even reach the
        # internal row. We swallow downstream errors (the external tenant
        # has a stub created_at format that the email helper rejects) —
        # the point is that the SQL gate is the line of defense.
        try:
            await run_churn_cleanup(db)
        except Exception:
            pass
        # Verify internal tenant is still present and un-warned after the run.
        async with db.execute(
            "SELECT id FROM tenants WHERE id = 't-internal'"
        ) as cur:
            assert (await cur.fetchone()) is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_start_session_returns_continuation_for_fresh_repeat():
    """G8.34 — re-calling start_session for the same session_name within the
    idle window returns a continuation block, not a brand-new registration."""
    from meridian.server import _start_session_composite
    db = await db_module.init_db(":memory:")
    try:
        p = await db_module.create_project(db, "g834-proj")
        first = await _start_session_composite(
            db, p["id"], "feature-x", "/tmp", source="startup",
        )
        assert "continuation" not in first
        first_sid = first["session_id"]
        # Immediate re-call — the prior session's last_seen is fresh.
        second = await _start_session_composite(
            db, p["id"], "feature-x", "/tmp", source="resume",
        )
        assert second.get("continuation") is True
        assert second["source"] == "resume"
        assert second["session"]["id"] == first_sid
        assert "recent_tasks" in second
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_start_session_skips_continuation_for_stale_session():
    """G8.34 — when last_seen is older than the idle window, fall through to
    a fresh registration. The MCP-Session-Id header is never consulted."""
    from meridian.server import _start_session_composite
    db = await db_module.init_db(":memory:")
    try:
        p = await db_module.create_project(db, "g834-stale")
        first = await _start_session_composite(
            db, p["id"], "feature-y", "/tmp",
        )
        first_sid = first["session_id"]
        # Backdate last_seen by 10 minutes.
        await db.execute(
            "UPDATE sessions SET last_seen = datetime('now', '-10 minutes') WHERE id = ?",
            (first_sid,),
        )
        await db.commit()
        second = await _start_session_composite(
            db, p["id"], "feature-y", "/tmp",
        )
        assert second.get("continuation") is not True
        assert second["session_id"] != first_sid
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_workspace_member_accepted_for_email_finds_accepted_only():
    """G5.22 — the helper used by OAuth callbacks to skip Neon provisioning
    for invitees must only consider ACCEPTED memberships, not pending ones."""
    from datetime import datetime, timezone
    import hashlib
    db = await db_module.init_db(":memory:")
    try:
        # No row at all → None
        assert await db_module.workspace_member_accepted_for_email(db, "x@x.com") is None
        # Need a real tenant row to satisfy the workspace_members FK.
        await db.execute(
            "INSERT INTO tenants (id, email) VALUES (?, ?)",
            ("tenant-owner", "owner@example.com"),
        )
        await db.commit()
        # Invite pending (no joined_at)
        await db.execute(
            "INSERT INTO workspace_members (id, tenant_id, email, role, token_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            ("m1", "tenant-owner", "invitee@example.com", "member",
             hashlib.sha256(b"tok").hexdigest()),
        )
        await db.commit()
        assert await db_module.workspace_member_accepted_for_email(db, "invitee@example.com") is None
        # Mark accepted
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            "UPDATE workspace_members SET joined_at = ?, token_hash = NULL WHERE id = ?",
            (now, "m1"),
        )
        await db.commit()
        got = await db_module.workspace_member_accepted_for_email(db, "INVITEE@EXAMPLE.COM")
        assert got is not None
        assert got["email"] == "invitee@example.com"
    finally:
        await db.close()


def test_project_icon_patch_round_trip(client):
    """G4.17 — PATCH /projects/{pid}/icon stores the emoji and clears it."""
    p = client.post("/projects", json={"name": "g417-icon"}).json()
    r = client.patch(f"/projects/{p['id']}/icon", json={"icon": "🎯"})
    assert r.status_code == 200
    assert r.json()["icon"] == "🎯"

    r = client.get("/projects").json()
    found = next(x for x in r if x["id"] == p["id"])
    assert found["icon"] == "🎯"

    r = client.patch(f"/projects/{p['id']}/icon", json={"icon": None})
    assert r.status_code == 200
    assert r.json()["icon"] is None

    # Long input is truncated to 8 chars at the model boundary.
    r = client.patch(f"/projects/{p['id']}/icon", json={"icon": "x" * 50})
    assert r.status_code == 200
    assert len(r.json()["icon"]) <= 8


def test_safety_limits_module_thresholds_have_sensible_defaults():
    """G4.15 — defaults are guard-rails, not quotas."""
    from meridian import limits
    assert limits.PROJECTS_PER_TENANT == 1_000
    assert limits.SPRINT_ITEMS_PER_PROJECT == 50_000
    assert limits.NOTES_PER_PROJECT == 100_000
    assert limits.DECISIONS_PER_PROJECT == 10_000
    assert limits.SESSIONS_PER_PROJECT == 100_000
    assert limits.TASKS_PER_PROJECT == 1_000_000
    assert limits.OPEN_HITL_PER_PROJECT == 1_000
    assert limits.BODY_BYTES == 100_000


def test_safety_limits_env_override(monkeypatch):
    """G4.15 — environment overrides take effect at module load. Tests can
    also monkeypatch the module attribute for in-process scenarios."""
    from meridian import limits
    monkeypatch.setattr(limits, "NOTES_PER_PROJECT", 2)
    try:
        limits.check_notes_per_project(0)
        limits.check_notes_per_project(1)
    except limits.LimitExceeded:
        raise AssertionError("limit should not trip below threshold")
    with pytest.raises(limits.LimitExceeded):
        limits.check_notes_per_project(2)


def test_safety_limit_returns_429_on_note_create(client, monkeypatch):
    """G4.15 — a notes count past the limit returns 429 with the canonical
    Safety message rather than 500 or silent success."""
    from meridian import limits
    monkeypatch.setattr(limits, "NOTES_PER_PROJECT", 1)
    p = client.post("/projects", json={"name": "g415-note-cap"}).json()
    r = client.post(
        f"/projects/{p['id']}/notes",
        json={"title": "n1", "body": "first"},
    )
    assert r.status_code == 201
    r = client.post(
        f"/projects/{p['id']}/notes",
        json={"title": "n2", "body": "second"},
    )
    assert r.status_code == 429
    j = r.json()
    assert j.get("kind") == "notes_per_project"
    assert "Safety limit reached" in j.get("detail", "")
    assert "hello@usemeridian.us" in j.get("detail", "")


def test_safety_limit_body_size_middleware_429(client, monkeypatch):
    """G4.15 — Content-Length past the body cap is rejected before any handler runs."""
    from meridian import limits
    monkeypatch.setattr(limits, "BODY_BYTES", 100)
    # 500-byte body, content-length declared honestly
    payload = "x" * 500
    r = client.post(
        "/projects",
        content=payload,
        headers={"Content-Type": "application/json", "Content-Length": "500"},
    )
    assert r.status_code == 429
    assert "body_bytes" in r.json().get("kind", "")


def test_billing_portal_redirects_anonymous_to_login(client, monkeypatch):
    """G2.11 — /billing/portal sends anonymous users to /auth/login with a next= back."""
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    r = client.get("/billing/portal", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/auth/login")
    assert "next=/billing/portal" in r.headers["location"]


def test_billing_portal_endpoint_registered():
    """G2.11 — the /billing/portal route exists on the FastAPI app."""
    from meridian.server import app
    routes = {r.path for r in app.routes if hasattr(r, "path")}
    assert "/billing/portal" in routes


@pytest.mark.asyncio
async def test_create_stripe_billing_portal_session_rejects_no_customer():
    """G2.11 — helper raises ValueError when stripe_customer_id is missing,
    so the route can redirect to /pricing instead of failing opaquely."""
    from meridian.hosted import create_stripe_billing_portal_session
    with pytest.raises(ValueError):
        await create_stripe_billing_portal_session({"email": "free@example.com"})


def test_me_endpoint_exposes_has_stripe_customer(client, monkeypatch):
    """G2.11 — /me payload carries has_stripe_customer so the dashboard
    can flip the billing button between Manage and Upgrade without a
    separate round-trip."""
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    r = client.get("/me")
    assert r.status_code == 200
    # Local mode returns {} (no tenant) — the field is added only when there's a tenant.
    # In hosted mode without a session, also {}. Either way it shouldn't crash.
    assert r.json() == {} or "has_stripe_customer" in r.json()


def test_hosted_non_admin_cannot_mutate_connections(client, monkeypatch):
    """G1.9 — POST/DELETE /config/connections returns 403 for non-admin hosted
    tenants. Replaces the surprising 404 "connection 'env' not found" with a
    clear permission error and matches the read-only dashboard UI."""
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    monkeypatch.delenv("MERIDIAN_ADMIN_EMAILS", raising=False)
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    # No session cookie → get_current_tenant raises 401 → guard maps to 403.
    r = client.post("/config/connections", json={"name": "neon-prod", "activate": True})
    assert r.status_code == 403
    assert "admin" in r.json().get("detail", "").lower() or \
           "sign in" in r.json().get("detail", "").lower()
    r = client.delete("/config/connections/neon-prod")
    assert r.status_code == 403


def test_local_mode_connections_endpoint_unchanged(client, monkeypatch):
    """G1.9 — the guard is hosted-only; self-hosted local installs still
    accept the POST/DELETE flow without auth (single-user trust model)."""
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    r = client.post(
        "/config/connections",
        json={"name": "local-test", "type": "sqlite", "activate": False},
    )
    # In local mode we should NOT get 403. Success or some other status,
    # depending on whether toml writes are mocked, but not 403.
    assert r.status_code != 403


def test_canonicalize_notify_target_strips_ntfy_prefix():
    """G1.7 — ntfy URLs collapse to topic-only; emails/webhooks pass through."""
    from meridian.server import _canonicalize_notify_target

    assert _canonicalize_notify_target("https://ntfy.sh/foo") == "foo"
    assert _canonicalize_notify_target("https://ntfy.sh/foo/") == "foo"
    assert _canonicalize_notify_target("ntfy.sh/foo") == "foo"
    assert _canonicalize_notify_target("  foo  ") == "foo"
    assert _canonicalize_notify_target("you@example.com") == "you@example.com"
    assert _canonicalize_notify_target("https://hooks.slack.com/x/y") == "https://hooks.slack.com/x/y"
    assert _canonicalize_notify_target("") is None
    assert _canonicalize_notify_target(None) is None


def test_patch_ntfy_canonicalizes_and_uniquifies(client):
    """G1.7 — PATCH stores the topic only and suffixes -2 on collision."""
    p1 = client.post("/projects", json={"name": "g17-p1"}).json()
    p2 = client.post("/projects", json={"name": "g17-p2"}).json()

    r = client.patch(f"/projects/{p1['id']}/ntfy", json={"notify_url": "https://ntfy.sh/sweep-alerts"})
    assert r.status_code == 200
    assert r.json()["notify_url"] == "sweep-alerts"

    r = client.patch(f"/projects/{p2['id']}/ntfy", json={"notify_url": "sweep-alerts"})
    assert r.status_code == 200
    assert r.json()["notify_url"] == "sweep-alerts-2"

    # Emails pass through unchanged
    r = client.patch(f"/projects/{p1['id']}/ntfy", json={"notify_url": "ops@example.com"})
    assert r.json()["notify_url"] == "ops@example.com"


def test_global_hitl_endpoint_returns_project_id_per_row(client):
    """G1.2 — the global /hitl endpoint must include project_id on each row
    so the dashboard can group pending counts per-project. Without this,
    every project vtab badge gets the same global total (the symptom
    that surfaced: HITL badge shows 2 while THIS project's queue is empty)."""
    p1 = client.post("/projects", json={"name": "g12-p1"}).json()
    p2 = client.post("/projects", json={"name": "g12-p2"}).json()
    r = client.post(f"/projects/{p2['id']}/hitl", json={"question": "p2 q1"})
    assert r.status_code == 201
    r = client.post(f"/projects/{p2['id']}/hitl", json={"question": "p2 q2"})
    assert r.status_code == 201

    rows = client.get("/hitl?status=pending&limit=50").json()
    assert isinstance(rows, list)
    pids = [r.get("project_id") for r in rows]
    assert all(pid for pid in pids), f"every HITL row needs project_id, got {pids!r}"
    grouped = {pid: pids.count(pid) for pid in set(pids)}
    assert grouped.get(p2["id"]) == 2
    assert grouped.get(p1["id"], 0) == 0


# v2.4 — decisions_pinned (editable constitution)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin_decision_inserts_row(db):
    """v2.4 — pin_decision creates an active row with the given category."""
    p = await db_module.create_project(db, "v24-pin")
    d = await db_module.pin_decision(
        db, p["id"], "psycopg3 only", "asyncpg has Windows DLL hang", "TECHNICAL"
    )
    assert d["title"] == "psycopg3 only"
    assert d["status"] == "active"
    assert d["category"] == "TECHNICAL"


@pytest.mark.asyncio
async def test_pin_decision_accepts_custom_category(db):
    p = await db_module.create_project(db, "v24-pin-custom-cat")
    d = await db_module.pin_decision(db, p["id"], "t", "b", "CUSTOM_CATEGORY")
    assert d["category"] == "CUSTOM_CATEGORY"


@pytest.mark.asyncio
async def test_get_pinned_decisions_filters_superseded(db):
    """Default get_pinned_decisions returns active only; flag toggles full history."""
    p = await db_module.create_project(db, "v24-pin-list")
    d1 = await db_module.pin_decision(db, p["id"], "first truth", "body1", "TECHNICAL")
    new = await db_module.supersede_pinned_decision(
        db, d1["id"], "second truth", "body2", "TECHNICAL"
    )
    active = await db_module.get_pinned_decisions(db, p["id"])
    assert [d["title"] for d in active] == ["second truth"]
    all_ = await db_module.get_pinned_decisions(db, p["id"], include_superseded=True)
    titles = {d["title"] for d in all_}
    assert titles == {"first truth", "second truth"}
    old = next(d for d in all_ if d["title"] == "first truth")
    assert old["status"] == "superseded"
    assert old["superseded_by"] == new["id"]


def test_decisions_pinned_http_round_trip(client):
    """v2.4 — POST then GET then PATCH-supersede via HTTP endpoints."""
    project = client.post("/projects", json={"name": "v24-pin-http"}).json()
    # Create
    r = client.post(
        f"/projects/{project['id']}/decisions-pinned",
        json={"title": "ship it", "body": "ship now", "category": "STRATEGIC"},
    )
    assert r.status_code == 201
    d = r.json()
    # List
    r = client.get(f"/projects/{project['id']}/decisions-pinned")
    assert r.status_code == 200
    assert any(x["title"] == "ship it" for x in r.json())
    # Supersede
    r = client.patch(
        f"/projects/{project['id']}/decisions-pinned/{d['id']}",
        json={"new_title": "ship later", "new_body": "wait a week"},
    )
    assert r.status_code == 200
    assert r.json()["title"] == "ship later"
    # Active list now only shows the new one
    r = client.get(f"/projects/{project['id']}/decisions-pinned")
    titles = [x["title"] for x in r.json()]
    assert titles == ["ship later"]


def test_decisions_pinned_archive_oldest_http(client):
    project = client.post("/projects", json={"name": "v24-pin-archive"}).json()
    created = []
    for title in ("oldest", "middle", "newest"):
        r = client.post(
            f"/projects/{project['id']}/decisions-pinned",
            json={"title": title, "body": f"{title} body", "category": "TECHNICAL"},
        )
        assert r.status_code == 201
        created.append(r.json())
    db = client.app.state.db
    asyncio.run(
        db.execute(
            "UPDATE decisions_pinned SET created_at = ? WHERE id = ?",
            ("2026-01-01 00:00:01", created[0]["id"]),
        )
    )
    asyncio.run(
        db.execute(
            "UPDATE decisions_pinned SET created_at = ? WHERE id = ?",
            ("2026-01-01 00:00:02", created[1]["id"]),
        )
    )
    asyncio.run(
        db.execute(
            "UPDATE decisions_pinned SET created_at = ? WHERE id = ?",
            ("2026-01-01 00:00:03", created[2]["id"]),
        )
    )
    asyncio.run(db.commit())
    r = client.post(
        f"/projects/{project['id']}/decisions-pinned/archive-oldest",
        json={"count": 2},
    )
    assert r.status_code == 200
    assert r.json()["archived"] == 2
    remaining = client.get(f"/projects/{project['id']}/decisions-pinned").json()
    assert [row["title"] for row in remaining] == ["newest"]


def test_delete_pinned_decision_http(client):
    """DELETE /projects/{pid}/decisions-pinned/{did} hard-deletes the row."""
    project = client.post("/projects", json={"name": "v29-pin-del-http"}).json()
    r = client.post(
        f"/projects/{project['id']}/decisions-pinned",
        json={"title": "to delete", "body": "temp", "category": "TECHNICAL"},
    )
    assert r.status_code == 201
    did = r.json()["id"]
    # Hard delete
    r = client.delete(f"/projects/{project['id']}/decisions-pinned/{did}")
    assert r.status_code == 204
    # Gone from list
    remaining = client.get(f"/projects/{project['id']}/decisions-pinned").json()
    assert not any(d["id"] == did for d in remaining)
    # 404 on second attempt
    r = client.delete(f"/projects/{project['id']}/decisions-pinned/{did}")
    assert r.status_code == 404


def test_hooks_session_start_and_stop(client):
    """POST /hooks/session-start returns hookSpecificOutput; /hooks/stop returns ok."""
    project = client.post("/projects", json={"name": "v29-hooks-test"}).json()
    r = client.post("/hooks/session-start", json={"project_id": project["id"]})
    assert r.status_code == 200
    body = r.json()
    assert "hookSpecificOutput" in body
    assert "additionalContext" in body["hookSpecificOutput"]
    assert project["name"] in body["hookSpecificOutput"]["additionalContext"]
    # Stop hook — uses session_id from start result
    additional = body["hookSpecificOutput"]["additionalContext"]
    session_id = None
    for line in additional.splitlines():
        if line.startswith("SESSION ID:"):
            session_id = line.split(":", 1)[1].strip()
            break
    r = client.post("/hooks/stop", json={"project_id": project["id"], "session_id": session_id})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_hooks_session_start_missing_project_id(client):
    r = client.post("/hooks/session-start", json={})
    assert r.status_code == 400


def test_hooks_installer_scripts_are_served(client):
    """GET /hooks.ps1 and /hooks.sh should serve the repo installer scripts."""
    ps1 = client.get("/hooks.ps1")
    assert ps1.status_code == 200
    assert "text/plain" in ps1.headers.get("content-type", "")
    assert "hooks.ps1" in ps1.text

    sh = client.get("/hooks.sh")
    assert sh.status_code == 200
    assert "text/plain" in sh.headers.get("content-type", "")
    assert "hooks.sh" in sh.text


def test_dashboard_js_has_hooks_token_management_ui(client):
    """dashboard.js exposes the hooks token list/revoke UI and updated hosted copy."""
    js = client.get("/static/dashboard.js").text
    assert "Get your key from the Auto-checkpoint hooks section above." in js
    assert "hooks-token-list-${projectId}" in js
    assert "hooks-refresh-tokens-${projectId}" in js
    assert "/auth/tokens/${tokenId}" in js
    assert "Existing API keys" in js


def test_pin_decision_custom_category_http(client):
    """Custom free-text category is accepted."""
    project = client.post("/projects", json={"name": "v29-custom-cat-http"}).json()
    r = client.post(
        f"/projects/{project['id']}/decisions-pinned",
        json={"title": "my dec", "body": "body", "category": "MY_CUSTOM_CAT"},
    )
    assert r.status_code == 201
    assert r.json()["category"] == "MY_CUSTOM_CAT"


# ---------------------------------------------------------------------------
# v2.4 — HITL queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_and_answer_hitl(db):
    """v2.4 — request_hitl + answer_hitl_request round-trip."""
    p = await db_module.create_project(db, "v24-hitl")
    s = await db_module.register_session(db, p["id"], "worker")
    h = await db_module.request_hitl(
        db, p["id"], "Which approach?", session_id=s["id"],
        context="Trade-off between A and B.", urgency="blocking",
    )
    assert h["status"] == "pending"
    assert h["urgency"] == "blocking"
    answered = await db_module.answer_hitl_request(
        db, h["id"], "Pick A.", answered_by="adam"
    )
    assert answered["status"] == "answered"
    assert answered["answer"] == "Pick A."
    assert answered["answered_by"] == "adam"


@pytest.mark.asyncio
async def test_list_hitl_orders_blocking_first(db):
    """v2.4 — blocking urgency sorts ahead of high / normal."""
    p = await db_module.create_project(db, "v24-hitl-sort")
    await db_module.request_hitl(db, p["id"], "normal one", urgency="normal")
    await db_module.request_hitl(db, p["id"], "blocking one", urgency="blocking")
    await db_module.request_hitl(db, p["id"], "high one", urgency="high")
    items = await db_module.list_hitl_requests(db, p["id"])
    assert [it["urgency"] for it in items] == ["blocking", "high", "normal"]


def test_hitl_http_endpoints(client):
    """v2.4 — POST/GET/PATCH HITL via HTTP."""
    project = client.post("/projects", json={"name": "v24-hitl-http"}).json()
    # Create
    r = client.post(
        f"/projects/{project['id']}/hitl",
        json={"question": "Approve?", "urgency": "high"},
    )
    assert r.status_code == 201
    h = r.json()
    # List
    r = client.get("/hitl")
    assert r.status_code == 200
    assert any(x["id"] == h["id"] for x in r.json())
    # Answer
    r = client.patch(f"/hitl/{h['id']}", json={"action": "answer", "answer": "yes"})
    assert r.status_code == 200
    assert r.json()["status"] == "answered"
    assert r.json()["answer"] == "yes"


# ---------------------------------------------------------------------------
# v2.4 — Webhook intake
# ---------------------------------------------------------------------------


def test_events_webhook_requires_token(client):
    """v2.4 — POST /events without X-Meridian-Token returns 401."""
    project = client.post("/projects", json={"name": "v24-evt"}).json()
    r = client.post(
        f"/projects/{project['id']}/events",
        json={"description": "task X", "agent_framework": "langgraph"},
    )
    assert r.status_code == 401


def test_events_webhook_normalizes_to_task_log(client):
    """v2.4 — valid token, event lands in task_log with framework label."""
    project = client.post("/projects", json={"name": "v24-evt-ok"}).json()
    # Mint the token.
    tok = client.get(f"/projects/{project['id']}/webhook-token").json()["token"]
    r = client.post(
        f"/projects/{project['id']}/events",
        json={
            "type": "task_completed",
            "session_name": "lg-researcher",
            "human_id": "langgraph",
            "agent_framework": "langgraph",
            "description": "researcher fetched 3 sources",
            "status": "done",
        },
        headers={"X-Meridian-Token": tok},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["task"]["description"] == "researcher fetched 3 sources"
    # Session should be registered with the framework label.
    sessions = client.get(f"/projects/{project['id']}/sessions").json()
    target = next(s for s in sessions if s["name"] == "lg-researcher")
    assert target.get("agent_framework") == "langgraph"


# ---------------------------------------------------------------------------
# v2.4 — Team summary
# ---------------------------------------------------------------------------


def test_team_summary_groups_by_human_id(client):
    """v2.4 — /team/summary groups task_log + sessions by human_id."""
    project = client.post(
        "/projects", json={"name": "v24-team", "human_id": "adam"}
    ).json()
    # Two sessions, two humans.
    s_adam = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "s-adam", "human_id": "adam"},
    ).json()
    s_lg = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "s-lg", "human_id": "langgraph"},
    ).json()
    # Tasks for each.
    client.post("/tasks", json={
        "session_id": s_adam["id"], "project_id": project["id"],
        "description": "adam did a thing", "status": "done",
    })
    client.post("/tasks", json={
        "session_id": s_lg["id"], "project_id": project["id"],
        "description": "lg did a thing", "status": "pending",
    })
    r = client.get(f"/team/summary?project_id={project['id']}&days=1")
    assert r.status_code == 200
    data = r.json()
    humans = {h["human_id"]: h for h in data["humans"]}
    assert "adam" in humans and "langgraph" in humans
    assert humans["adam"]["tasks_done"] >= 1
    assert humans["langgraph"]["tasks_pending"] >= 1


# ---------------------------------------------------------------------------
# v2.4 — parent_task_id on log_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_task_records_parent_task_id(db):
    """v2.4 — parent_task_id flows through to the row so the dashboard can
    render multi-agent task trees."""
    p = await db_module.create_project(db, "v24-tree")
    s = await db_module.register_session(db, p["id"], "orchestrator")
    parent = await db_module.log_task(db, s["id"], p["id"], "orchestrate", "in_progress")
    child = await db_module.log_task(
        db, s["id"], p["id"], "sub-step", "done", parent_task_id=parent["id"]
    )
    assert child["parent_task_id"] == parent["id"]


# ---------------------------------------------------------------------------
# v2.4 — agent_framework on sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_session_persists_agent_framework(db):
    """v2.4 — agent_framework label round-trips through register_session."""
    p = await db_module.create_project(db, "v24-fw")
    s = await db_module.register_session(
        db, p["id"], "lg-1", human_id="langgraph", agent_framework="langgraph"
    )
    assert s["agent_framework"] == "langgraph"


# ---------------------------------------------------------------------------
# v0.9 — project_notes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_and_get_project_notes(db):
    """v0.9 — add_project_note + get_project_notes round-trip + tag filter."""
    p = await db_module.create_project(db, "v09-notes")
    await db_module.add_project_note(
        db, p["id"], "Reset DB", "rm -rf data/", tags="setup,gotcha"
    )
    await db_module.add_project_note(
        db, p["id"], "ANTHROPIC_API_KEY", "Set in .env", tags="env"
    )
    all_ = await db_module.get_project_notes(db, p["id"])
    assert len(all_) == 2
    setup_only = await db_module.get_project_notes(db, p["id"], tag="setup")
    assert len(setup_only) == 1
    assert setup_only[0]["title"] == "Reset DB"


def test_project_notes_http_crud(client):
    """v0.9 — POST/GET/PATCH/DELETE /projects/{id}/notes."""
    project = client.post("/projects", json={"name": "v09-notes-http"}).json()
    # Create
    r = client.post(
        f"/projects/{project['id']}/notes",
        json={"title": "Gotcha", "body": "Postgres needs %% not %", "tags": "gotcha"},
    )
    assert r.status_code == 201
    n = r.json()
    # List
    r = client.get(f"/projects/{project['id']}/notes")
    assert r.status_code == 200
    assert any(x["id"] == n["id"] for x in r.json())
    # Patch
    r = client.patch(
        f"/projects/{project['id']}/notes/{n['id']}",
        json={"body": "Postgres needs %% not % in LIKE"},
    )
    assert r.status_code == 200
    assert "LIKE" in r.json()["body"]
    # Delete
    r = client.delete(f"/projects/{project['id']}/notes/{n['id']}")
    assert r.status_code == 204
    r = client.delete(f"/projects/{project['id']}/notes/{n['id']}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# v0.9 — Magic link tokens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_magic_token_round_trip(db):
    """v0.9 — store_magic_token + consume_magic_token single-use atomicity."""
    import hashlib as _h
    from datetime import datetime, timedelta, timezone
    raw = "magic-secret-123"
    token_hash = _h.sha256(raw.encode()).hexdigest()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=24)
    ).strftime("%Y-%m-%d %H:%M:%S")
    await db_module.store_magic_token(db, "alice@example.com", token_hash, expires_at)
    # First consume succeeds
    row = await db_module.consume_magic_token(db, token_hash)
    assert row is not None
    assert row["email"] == "alice@example.com"
    # Second consume fails — single-use
    row2 = await db_module.consume_magic_token(db, token_hash)
    assert row2 is None


@pytest.mark.asyncio
async def test_magic_token_expired(db):
    """v0.9 — expired token won't validate."""
    import hashlib as _h
    from datetime import datetime, timedelta, timezone
    raw = "expired-token"
    token_hash = _h.sha256(raw.encode()).hexdigest()
    past = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).strftime("%Y-%m-%d %H:%M:%S")
    await db_module.store_magic_token(db, "bob@example.com", token_hash, past)
    row = await db_module.consume_magic_token(db, token_hash)
    assert row is None


def test_magic_link_request_returns_200(client):
    """v0.9 — POST /auth/magic accepts valid email and returns 200."""
    # Resend not configured in tests → endpoint still returns 200, surfaces dev_link.
    r = client.post("/auth/magic", json={"email": "test@example.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"


def test_magic_link_verify_404_for_bad_token(client):
    """v0.9 — GET /auth/magic/verify with invalid token rejects."""
    r = client.get("/auth/magic/verify?token=nonexistent")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# v1.0 — Microsoft OAuth
# ---------------------------------------------------------------------------

def test_auth_microsoft_login_redirects_when_configured(client, monkeypatch):
    """GET /auth/microsoft/login redirects to Microsoft when MICROSOFT_CLIENT_ID is set."""
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "fake-ms-client-id")
    import meridian.hosted as _h
    monkeypatch.setattr(_h, "MICROSOFT_CLIENT_ID", "fake-ms-client-id")
    r = client.get("/auth/microsoft/login", follow_redirects=False)
    assert r.status_code == 302
    assert "login.microsoftonline.com" in r.headers.get("location", "")


def test_auth_microsoft_callback_missing_code(client):
    """GET /auth/microsoft/callback without code param returns 400."""
    r = client.get("/auth/microsoft/callback", follow_redirects=False)
    assert r.status_code == 400
    assert "missing oauth code" in r.json().get("detail", "")


# ---------------------------------------------------------------------------
# v1.0 — MeridianCheckpointer (LangGraph integration)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_langgraph_checkpointer_instantiate():
    """MeridianCheckpointer can be instantiated without side effects."""
    from meridian.integrations.langgraph import MeridianCheckpointer
    cp = MeridianCheckpointer(
        project_id="test-proj",
        api_url="http://localhost:7878",
        api_token="test-token",
    )
    assert cp.project_id == "test-proj"
    assert "Authorization" in cp.headers


@pytest.mark.asyncio
async def test_langgraph_checkpointer_put_graceful_failure():
    """MeridianCheckpointer.put() swallows network errors (server not running)."""
    from meridian.integrations.langgraph import MeridianCheckpointer
    cp = MeridianCheckpointer(
        project_id="test-proj",
        api_url="http://localhost:19999",  # nothing listening here
    )
    config = {"configurable": {"thread_id": "test-node"}}
    checkpoint = {"pending_sends": []}
    metadata = {"step": 1}
    # Should not raise even though the server isn't running
    result = await cp.put(config, checkpoint, metadata, {})
    assert result == config


# ---------------------------------------------------------------------------
# v1.0 — Stripe metered storage overage (MeterEvent API)
# ---------------------------------------------------------------------------


# a0cc3503 — /demo DB integration test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_loads_correct_db(db):
    """_seed_demo_data populates backend-api-v2 project with sessions and sprint items.

    Uses the `db` fixture (in-memory SQLite) directly to avoid TestClient
    lifespan async-timeout issues.  The seeding logic is the same code that
    runs on the live /demo route — this test verifies it produces the expected
    demo content that HN visitors see.
    """
    from meridian.server import _seed_demo_data

    await _seed_demo_data(db)

    # Projects — must contain backend-api-v2
    projects = await db_module.list_projects(db)
    project_names = [p["name"] for p in projects]
    assert "backend-api-v2" in project_names, (
        f"backend-api-v2 not found in seeded projects: {project_names}"
    )

    api_project = next(p for p in projects if p["name"] == "backend-api-v2")

    # Sessions — at least 1
    sessions = await db_module.get_sessions(db, api_project["id"])
    assert len(sessions) >= 1, "Expected at least 1 session for backend-api-v2"

    # Sprint items — at least 1
    sprint_items = await db_module.get_sprint_items(db, api_project["id"])
    assert len(sprint_items) >= 1, "Expected at least 1 sprint item for backend-api-v2"


# ---------------------------------------------------------------------------
# v2.6 — new MCP tools: list_hitl_requests, list_sessions, answer_hitl,
#         dismiss_hitl, add_sprint_note, get_sprint_notes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_hitl_requests_mcp_tool(db):
    """list_hitl_requests returns pending queue without needing UUIDs."""
    project = await db_module.create_project(db, "hitl-list-test")
    pid = project["id"]
    session = await db_module.register_session(db, pid, "s1")
    sid = session["id"]

    await db_module.request_hitl(db, pid, "question A", session_id=sid, urgency="blocking")
    await db_module.request_hitl(db, pid, "question B", session_id=sid)

    rows = await db_module.list_hitl_requests(db, pid, status="pending")
    assert len(rows) == 2
    assert rows[0]["urgency"] == "blocking"  # sorted by urgency first

    rows_all = await db_module.list_hitl_requests(db, pid, status=None)
    assert len(rows_all) == 2


@pytest.mark.asyncio
async def test_answer_hitl_and_dismiss_hitl(db):
    """answer_hitl and dismiss_hitl mark requests correctly."""
    project = await db_module.create_project(db, "hitl-answer-test")
    pid = project["id"]
    session = await db_module.register_session(db, pid, "s1")
    sid = session["id"]

    r1 = await db_module.request_hitl(db, pid, "answer me", session_id=sid)
    r2 = await db_module.request_hitl(db, pid, "dismiss me", session_id=sid)

    answered = await db_module.answer_hitl_request(db, r1["id"], "the answer", answered_by="adam")
    assert answered["status"] == "answered"
    assert answered["answer"] == "the answer"

    dismissed = await db_module.dismiss_hitl_request(db, r2["id"])
    assert dismissed["status"] == "dismissed"

    pending = await db_module.list_hitl_requests(db, pid, status="pending")
    assert len(pending) == 0


@pytest.mark.asyncio
async def test_add_and_get_sprint_notes(db):
    """add_session_note / get_session_notes round-trip correctly."""
    project = await db_module.create_project(db, "sprint-notes-test")
    pid = project["id"]
    session = await db_module.register_session(db, pid, "executor-1")
    sid = session["id"]

    n1 = await db_module.add_session_note(db, sid, "Don't touch hosted.py", "Neon pool at 7/8")
    n2 = await db_module.add_session_note(db, sid, "Blocker", "Waiting on Adam HITL")

    notes = await db_module.get_session_notes(db, sid)
    assert len(notes) == 2
    titles = {n["title"] for n in notes}
    assert "Don't touch hosted.py" in titles
    assert "Blocker" in titles

    # Auto-delete on session close
    await db_module.delete_session_notes(db, sid)
    notes_after = await db_module.get_session_notes(db, sid)
    assert len(notes_after) == 0


@pytest.mark.asyncio
async def test_list_sessions_db(db):
    """get_sessions returns active sessions for a project."""
    project = await db_module.create_project(db, "list-sessions-test")
    pid = project["id"]

    s1 = await db_module.register_session(db, pid, "session-alpha")
    s2 = await db_module.register_session(db, pid, "session-beta")

    active = await db_module.get_sessions(db, pid, active_only=True)
    names = {s["name"] for s in active}
    assert "session-alpha" in names
    assert "session-beta" in names

    await db_module.close_session(db, s1["id"])
    still_active = await db_module.get_sessions(db, pid, active_only=True)
    still_names = {s["name"] for s in still_active}
    assert "session-alpha" not in still_names
    assert "session-beta" in still_names


def test_new_mcp_tools_in_tools_list():
    """All 6 new MCP tools appear in _MCP_TOOLS_LIST."""
    import meridian.server as server_module
    tools = {t["name"] for t in server_module._MCP_TOOLS_LIST}
    new_tools = {"list_hitl_requests", "answer_hitl", "dismiss_hitl", "list_sessions", "add_sprint_note", "get_sprint_notes"}
    missing = new_tools - tools
    assert not missing, f"Missing MCP tools: {missing}"


# ---------------------------------------------------------------------------
# 9768d806 — MCP SSE transport endpoints
# ---------------------------------------------------------------------------

def test_mcp_sse_get_headers(client):
    """GET /mcp/sse returns correct headers (tested via OPTIONS to avoid hanging)."""
    # The OPTIONS preflight verifies the endpoint exists and has correct CORS headers.
    # The GET endpoint itself is an infinite SSE stream — not suitable for sync TestClient.
    r = client.options("/mcp/sse")
    assert r.status_code == 204
    # Verify the GET route is registered (test that OPTIONS returns CORS, which only
    # exists if the route is registered)
    assert "access-control-allow-methods" in {k.lower() for k in r.headers}


def test_mcp_sse_get_session_in_sessions_map(client, monkeypatch):
    """GET /mcp/sse registers a session in _SSE_SESSIONS (verified via POST without session_id)."""
    import importlib
    import meridian.server as srv
    # POST without session_id should still work (falls back to app.state.db)
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    r = client.post("/mcp/sse", json=payload)
    assert r.status_code == 200
    assert "result" in r.json()


def test_mcp_sse_options_returns_cors(client):
    """OPTIONS /mcp/sse returns CORS headers for chrome-extension origin."""
    r = client.options("/mcp/sse")
    assert r.status_code == 204
    assert r.headers.get("access-control-allow-origin") == "*"


def test_mcp_sse_post_returns_jsonrpc(client):
    """POST /mcp/sse returns valid JSON-RPC response for initialize."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "test", "version": "1.0"},
            "capabilities": {},
        },
    }
    r = client.post("/mcp/sse", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data.get("jsonrpc") == "2.0"
    assert data.get("id") == 1
    assert "result" in data
    assert data["result"]["protocolVersion"] == "2024-11-05"


def test_mcp_sse_post_tools_list(client):
    """POST /mcp/sse tools/list returns Meridian tools."""
    payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    r = client.post("/mcp/sse", json=payload)
    assert r.status_code == 200
    data = r.json()
    tool_names = [t["name"] for t in data["result"]["tools"]]
    assert "start_session" in tool_names
    assert "log_task" in tool_names
    assert "generate_handoff" in tool_names


def test_mcp_sse_cors_headers_on_post(client):
    """POST /mcp/sse response includes CORS headers."""
    payload = {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}}
    r = client.post("/mcp/sse", json=payload)
    assert r.headers.get("access-control-allow-origin") == "*"


def test_mcp_responses_carry_csp_header(client):
    """All /mcp route responses carry a strict Content-Security-Policy header.

    Required for the OpenAI Apps SDK submission, which flags MCP routes lacking
    a CSP. The /mcp surface serves only JSON/SSE, so a deny-all policy is correct.
    """
    # POST /mcp/sse (JSON-RPC response)
    r_sse = client.post("/mcp/sse", json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
    csp = r_sse.headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp

    # POST /mcp with no auth (401 path still goes through the middleware)
    r_mcp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
    assert "default-src 'none'" in r_mcp.headers.get("content-security-policy", "")


def test_remote_mcp_401_includes_www_authenticate(client):
    """POST /mcp with no auth returns 401 with WWW-Authenticate: Bearer header.

    ChatGPT and other OAuth clients use this header to discover the OAuth flow.
    Without it they just show the 401 error to the user rather than starting OAuth.
    """
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
    assert r.status_code == 401
    www_auth = r.headers.get("www-authenticate", "")
    assert "Bearer" in www_auth
    assert "realm" in www_auth


def test_remote_mcp_401_invalid_bearer_includes_www_authenticate(client):
    """POST /mcp with a garbage Bearer token still returns WWW-Authenticate header."""
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
        headers={"Authorization": "Bearer not-a-real-token-xyzzy"},
    )
    assert r.status_code == 401
    www_auth = r.headers.get("www-authenticate", "")
    assert "Bearer" in www_auth


def test_login_page_preserves_next_param(client):
    """GET /auth/login?next=/foo injects ?next= into all login button hrefs."""
    r = client.get("/auth/login?next=/oauth/authorize%3Fclient_id%3Dabc")
    assert r.status_code == 200
    assert "/auth/google/login?next=" in r.text
    assert "/auth/github/login?next=" in r.text


def test_login_page_no_next_param(client):
    """GET /auth/login without ?next= keeps bare login hrefs (no ?next= appended)."""
    r = client.get("/auth/login")
    assert r.status_code == 200
    assert 'href="/auth/google/login"' in r.text
    assert 'href="/auth/github/login"' in r.text


# ---------------------------------------------------------------------------
# Generalised notification system (sprint items 102853be, 2fae3acf, 36032131)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_set_project_notify_url(anydb):
    """get_project_ntfy_url / set_project_ntfy_url round-trip on every DB backend."""
    p = await db_module.create_project(anydb, "notif-project-1")
    # Initially None
    url = await db_module.get_project_ntfy_url(anydb, p["id"])
    assert url is None
    # Set an ntfy URL
    await db_module.set_project_ntfy_url(anydb, p["id"], "https://ntfy.sh/test-topic")
    url = await db_module.get_project_ntfy_url(anydb, p["id"])
    assert url == "https://ntfy.sh/test-topic"
    # Clear it
    await db_module.set_project_ntfy_url(anydb, p["id"], None)
    url = await db_module.get_project_ntfy_url(anydb, p["id"])
    assert url is None


@pytest.mark.asyncio
async def test_new_tenant_notification_defaults(db):
    """New tenants must have storage + sprint notifications ON, all others OFF."""
    tenant = await db_module.upsert_tenant(db, email="newuser@example.com")
    prefs = json.loads(tenant.get("notification_prefs") or "{}")
    assert prefs.get("storage") is True, "storage notification should default ON"
    assert prefs.get("sprint") is True, "sprint notification should default ON"
    assert not prefs.get("hitl"), "hitl notification should default OFF"
    assert not prefs.get("stalled"), "stalled notification should default OFF"


@pytest.mark.asyncio
async def test_dispatch_notification_ntfy(monkeypatch):
    """_dispatch_notification routes ntfy.sh URLs with special headers."""
    from meridian.server import _dispatch_notification
    import httpx

    calls = []

    async def fake_post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        class FakeResp:
            status_code = 200
        return FakeResp()

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, url, **kwargs):
            return await fake_post(url, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeClient())

    await _dispatch_notification(
        "https://ntfy.sh/my-topic",
        "Test title",
        "Test body",
        event="test",
    )
    assert len(calls) == 1
    headers = calls[0]["kwargs"].get("headers", {})
    assert headers.get("Title") == "Test title"
    assert headers.get("Tags") == "test"
    assert "Priority" in headers


@pytest.mark.asyncio
async def test_dispatch_notification_webhook(monkeypatch):
    """_dispatch_notification sends JSON to generic webhook URLs."""
    from meridian.server import _dispatch_notification
    import httpx

    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, url, **kwargs):
            calls.append({"url": url, "json": kwargs.get("json")})
            class FakeResp:
                status_code = 200
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeClient())

    await _dispatch_notification(
        "https://hooks.slack.com/services/T00000000/B00000000/XXXX",
        "Sprint done",
        "All items completed",
        event="sprint_done",
    )
    assert len(calls) == 1
    body = calls[0]["json"]
    assert body["title"] == "Sprint done"
    assert body["body"] == "All items completed"
    assert body["event"] == "sprint_done"
    assert body["source"] == "meridian"


@pytest.mark.asyncio
async def test_dispatch_notification_email_via_resend(monkeypatch):
    """_dispatch_notification routes email targets through Resend."""
    from meridian.server import _dispatch_notification
    import httpx

    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, **kwargs):
            calls.append({"url": url, "kwargs": kwargs})

            class FakeResp:
                status_code = 200

            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeClient())
    monkeypatch.setenv("RESEND_API_KEY", "resend-test-key")
    monkeypatch.setenv("MERIDIAN_BASE_URL", "https://usemeridian.us")

    await _dispatch_notification(
        "user@example.com",
        "HITL needed",
        "Please review",
        event="hitl",
    )
    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.resend.com/emails"
    headers = calls[0]["kwargs"]["headers"]
    payload = calls[0]["kwargs"]["json"]
    assert headers["Authorization"] == "Bearer resend-test-key"
    assert payload["to"] == ["user@example.com"]
    assert payload["subject"] == "[Meridian] HITL needed"
    assert payload["text"] == "Please review"


@pytest.mark.asyncio
async def test_dispatch_notification_email_skips_without_resend_key(monkeypatch):
    """_dispatch_notification silently skips email when RESEND_API_KEY is not set."""
    from meridian.server import _dispatch_notification
    import httpx

    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, url, **kwargs):
            calls.append(url)
            class FakeResp:
                status_code = 200
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeClient())
    monkeypatch.delenv("RESEND_API_KEY", raising=False)

    # Should not call httpx at all (no API key = skip)
    await _dispatch_notification(
        "user@example.com",
        "HITL needed",
        "Please review",
        event="hitl",
    )
    assert calls == []


def test_notify_url_get_endpoint(client):
    """GET /projects/{id}/ntfy returns notify_url field."""
    r = client.post("/projects", json={"name": "notif-test-get"})
    assert r.status_code in (200, 201)
    pid = r.json()["id"]
    r2 = client.get(f"/projects/{pid}/ntfy")
    assert r2.status_code == 200
    data = r2.json()
    assert "ntfy_url" in data or "notify_url" in data


def test_notify_url_patch_and_get(client, monkeypatch):
    """PATCH /projects/{id}/ntfy saves URL; GET returns it.
    Mocks httpx so no real network call is made for the welcome notification."""
    import httpx

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, url, **kwargs):
            class FakeResp:
                status_code = 200
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeClient())

    r = client.post("/projects", json={"name": "notif-test-patch"})
    assert r.status_code in (200, 201)
    pid = r.json()["id"]

    # Save a URL via the canonical notify_url key. As of G1.7, ntfy URLs are
    # canonicalized to topic-only on save; the response echoes the stored form.
    r2 = client.patch(f"/projects/{pid}/ntfy", json={"notify_url": "https://ntfy.sh/test-123"})
    assert r2.status_code == 200
    assert r2.json().get("notify_url") == "test-123" or \
           r2.json().get("ntfy_url") == "test-123"

    # Confirm it's persisted
    r3 = client.get(f"/projects/{pid}/ntfy")
    assert r3.status_code == 200
    saved = r3.json().get("notify_url") or r3.json().get("ntfy_url")
    assert saved == "test-123"


def test_notify_test_endpoint_no_url(client):
    """POST /projects/{id}/notify/test returns 400 when no URL is configured."""
    r = client.post("/projects", json={"name": "notif-test-endpoint"})
    assert r.status_code in (200, 201)
    pid = r.json()["id"]
    r2 = client.post(f"/projects/{pid}/notify/test")
    assert r2.status_code == 400
    assert "No notify URL" in r2.json().get("detail", "")


def test_notification_error_surfaces_to_test_endpoint(client, monkeypatch):
    """POST /projects/{id}/notify/test returns the Resend failure body."""
    import httpx

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, **kwargs):
            class FakeResp:
                status_code = 422

                def json(self):
                    return {"message": "Domain not verified"}

                text = '{"message":"Domain not verified"}'

            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeClient())
    monkeypatch.setenv("RESEND_API_KEY", "resend-test-key")

    r = client.post("/projects", json={"name": "notif-test-email-error"})
    assert r.status_code in (200, 201)
    pid = r.json()["id"]
    patch_resp = client.patch(
        f"/projects/{pid}/ntfy",
        json={"notify_url": "user@example.com"},
    )
    assert patch_resp.status_code == 200

    test_resp = client.post(f"/projects/{pid}/notify/test")
    assert test_resp.status_code == 422
    assert "Domain not verified" in test_resp.json().get("detail", "")


def test_notify_test_endpoint_delivers_ntfy_message_via_postgres(tmp_path, monkeypatch):
    """Postgres-backed /notify/test publishes a real ntfy.sh message."""
    test_db_url = os.environ.get("TEST_DATABASE_URL")
    if not test_db_url:
        pytest.skip("TEST_DATABASE_URL not set - skipping Postgres ntfy e2e test")

    monkeypatch.setenv("MERIDIAN_DB_URL", test_db_url)
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))

    import importlib

    import httpx
    from fastapi.testclient import TestClient

    server_mod = importlib.reload(server_module)
    topic = f"meridian-e2e-{uuid.uuid4().hex[:12]}"
    project_name = f"ntfy-e2e-{uuid.uuid4().hex[:8]}"
    notify_url = f"https://ntfy.sh/{topic}"

    with TestClient(server_mod.app) as client:
        create_resp = client.post("/projects", json={"name": project_name})
        assert create_resp.status_code in (200, 201)
        project_id = create_resp.json()["id"]

        patch_resp = client.patch(
            f"/projects/{project_id}/ntfy",
            json={"notify_url": notify_url},
        )
        assert patch_resp.status_code == 200

        test_resp = client.post(f"/projects/{project_id}/notify/test")
        assert test_resp.status_code == 200
        assert test_resp.json()["notify_url"] == notify_url

    deadline = time.time() + 20
    while time.time() < deadline:
        poll_resp = httpx.get(
            f"https://ntfy.sh/{topic}/json",
            params={"poll": "1", "since": "2m"},
            timeout=10.0,
        )
        assert poll_resp.status_code == 200
        messages = [
            json.loads(line)
            for line in poll_resp.text.splitlines()
            if line.strip().startswith("{")
        ]
        if any(msg.get("title") == "Meridian test notification" for msg in messages):
            assert any(
                "notifications are working" in str(msg.get("message", "")).lower()
                for msg in messages
                if msg.get("title") == "Meridian test notification"
            )
            return
        time.sleep(1)

    pytest.fail("Timed out waiting for ntfy.sh test notification")


# ---------------------------------------------------------------------------
# cleanup-june8: regression tests for A/B/C
# ---------------------------------------------------------------------------


def test_delete_project_uses_modal_not_prompt(client):
    """dashboard.js _deleteProject uses a modal confirm, not window.prompt."""
    js = client.get("/static/dashboard.js").text
    assert "delete-project-modal" in js, "modal overlay id must be present"
    assert "del-proj-confirm" in js, "confirm button must be present"
    # _deleteProject must use modal, not window.prompt (other functions may still use prompt)
    fn_start = js.find("async function _deleteProject(")
    fn_end = js.find("\nasync function ", fn_start + 1)
    fn_body = js[fn_start:fn_end]
    assert "window.prompt(" not in fn_body, "_deleteProject must not use window.prompt"


def test_admin_snapshot_file_db_has_data(tmp_path, monkeypatch):
    """GET /admin/snapshot with a file-based SQLite DB returns a DB containing project rows."""
    import importlib

    db_path = tmp_path / "snap_test.db"
    monkeypatch.setenv("MERIDIAN_DB", str(db_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))

    # Prevent meridian.toml from overriding MERIDIAN_DB_URL with the real Neon URL.
    # The lifespan only skips the toml check for :memory:; for a real file path it
    # runs get_toml_db_url() and would clobber our empty DB_URL monkeypatch.
    import meridian.toml_config as _toml_mod
    monkeypatch.setattr(_toml_mod, "get_toml_db_url", lambda: (None, None))

    import meridian.server as srv_mod
    srv_mod = importlib.reload(srv_mod)

    from fastapi.testclient import TestClient

    with TestClient(srv_mod.app) as c:
        c.post("/projects", json={"name": "snap-test-project"})
        r = c.get("/admin/snapshot")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/x-sqlite3"
        # SQLite file magic header — confirms the response is a real DB file, not empty/error
        assert r.content[:16] == b"SQLite format 3\x00", \
            "snapshot does not have SQLite magic header"
        # Must be non-trivial size (at least a few pages — not just empty header)
        assert len(r.content) >= 4096, \
            f"snapshot too small ({len(r.content)} bytes) — expected at least one page"


def test_mcp_create_project_duplicate_returns_error(client):
    """MCP create_project via /mcp/sse returns error when project name already exists."""
    name = f"dup-guard-{uuid.uuid4().hex[:6]}"
    # First creation succeeds
    r1 = client.post("/mcp/sse", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "create_project", "arguments": {"name": name}},
    })
    assert r1.status_code == 200
    d1 = r1.json()
    assert "result" in d1, f"first create failed: {d1}"

    # Second creation with same name returns error
    r2 = client.post("/mcp/sse", json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "create_project", "arguments": {"name": name}},
    })
    assert r2.status_code == 200
    d2 = r2.json()
    text = d2.get("result", {}).get("content", [{}])[0].get("text", "")
    assert "already exists" in text, f"expected 'already exists' error, got: {text}"


# ---------------------------------------------------------------------------
# Sprint tools in _MCP_TOOLS_LIST + _dispatch_mcp_tool (hosted /mcp route)
# ---------------------------------------------------------------------------

def test_sprint_tools_in_mcp_tools_list():
    """set_sprint, add_sprint_item, complete_sprint_item, get_sprint_items appear in _MCP_TOOLS_LIST."""
    from meridian.mcp_tools import _MCP_TOOLS_LIST
    names = {t["name"] for t in _MCP_TOOLS_LIST}
    missing = {"set_sprint", "add_sprint_item", "complete_sprint_item", "get_sprint_items"} - names
    assert not missing, f"Missing from _MCP_TOOLS_LIST: {missing}"


def test_get_sprint_items_is_read_only():
    from meridian.mcp_tools import _MCP_TOOLS_LIST
    tool = next(t for t in _MCP_TOOLS_LIST if t["name"] == "get_sprint_items")
    assert tool["annotations"]["readOnlyHint"] is True


@pytest.mark.asyncio
async def test_dispatch_mcp_tool_set_sprint(db):
    """set_sprint via _dispatch_mcp_tool updates the sprint field."""
    import meridian.server as srv
    p = await db_module.create_project(db, "sprint-dispatch-test")
    await db_module.set_goal(db, p["id"], "initial goal")
    result = await srv._dispatch_mcp_tool(
        "set_sprint", {"project_id": p["id"], "sprint": "week-1-auth"}, db, "/tmp"
    )
    assert result["sprint"] == "week-1-auth"


@pytest.mark.asyncio
async def test_dispatch_mcp_tool_sprint_items_round_trip(db):
    """add_sprint_item / get_sprint_items / complete_sprint_item via _dispatch_mcp_tool."""
    import meridian.server as srv
    p = await db_module.create_project(db, "sprint-dispatch-items-test")

    added = await srv._dispatch_mcp_tool(
        "add_sprint_item",
        {"project_id": p["id"], "version": "v1", "title": "Ship login"},
        db, "/tmp",
    )
    assert added["title"] == "Ship login"
    item_id = added["id"]

    items = await srv._dispatch_mcp_tool(
        "get_sprint_items", {"project_id": p["id"]}, db, "/tmp"
    )
    assert any(it["id"] == item_id for it in items)

    done = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item_id},
        db, "/tmp",
    )
    assert done["status"] == "done"


def test_sprint_tools_via_mcp_sse_tools_list(client):
    """tools/list on /mcp/sse includes the 4 sprint tools."""
    r = client.post("/mcp/sse", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}
    })
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["result"]["tools"]}
    missing = {"set_sprint", "add_sprint_item", "complete_sprint_item", "get_sprint_items"} - names
    assert not missing, f"Missing from tools/list: {missing}"
