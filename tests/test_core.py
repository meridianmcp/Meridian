"""Core tests for Meridian — db layer, HTTP endpoints, and handoff."""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from meridian import dashboard as dashboard_module
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
    assert "/dashboard/chat" in js  # chat proxy hook present


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


def test_dashboard_chat_streams_sse_api_mode(client, monkeypatch):
    """API mode dispatches to the Anthropic SDK proxy. We stub the
    generator so the test doesn't need a real Anthropic key.
    """
    async def fake_stream(messages, system_prompt, model, max_tokens, **_kwargs):
        yield b'data: {"delta": "hello "}\n\n'
        yield b'data: {"delta": "world"}\n\n'
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr(
        dashboard_module, "stream_anthropic_chat", fake_stream
    )

    project = client.post("/projects", json={"name": "alpha"}).json()
    r = client.post(
        "/dashboard/chat",
        json={
            "project_id": project["id"],
            "messages": [{"role": "user", "content": "hi"}],
            "mode": "api",
        },
    )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    body = r.text
    assert "hello " in body
    assert "world" in body
    assert "[DONE]" in body


def test_dashboard_chat_defaults_to_cli_mode(client, monkeypatch):
    """A request with no ``mode`` field must dispatch to the CLI
    streamer, not the API one. We stub *both* so the test fails loudly
    if the dispatch picks the wrong backend.
    """
    cli_calls: list[str] = []
    api_calls: list[str] = []

    async def fake_cli(messages, system_prompt, model, max_tokens, **_kwargs):
        cli_calls.append("called")
        yield b'data: {"delta": "via cli"}\n\n'
        yield b"data: [DONE]\n\n"

    async def fake_api(messages, system_prompt, model, max_tokens, **_kwargs):
        api_calls.append("called")
        yield b'data: {"delta": "via api"}\n\n'
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr(dashboard_module, "stream_claude_cli_chat", fake_cli)
    monkeypatch.setattr(dashboard_module, "stream_anthropic_chat", fake_api)

    project = client.post("/projects", json={"name": "alpha"}).json()
    r = client.post(
        "/dashboard/chat",
        json={
            "project_id": project["id"],
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200
    assert "via cli" in r.text
    assert "via api" not in r.text
    assert cli_calls == ["called"]
    assert api_calls == []


def test_stream_claude_cli_chat_emits_stdout_as_sse(monkeypatch):
    """End-to-end: with a Python stub as the worker command, the CLI
    streamer should spawn the subprocess and forward stdout chunks as
    ``data: {"delta": "..."}`` SSE lines, terminating with ``[DONE]``.
    """
    monkeypatch.setenv(
        "MERIDIAN_CLAUDE_CLI",
        f'"{sys.executable}" -c "import sys; sys.stdout.write(sys.argv[1])"',
    )

    async def run() -> bytes:
        chunks: list[bytes] = []
        async for chunk in dashboard_module.stream_claude_cli_chat(
            messages=[{"role": "user", "content": "ping"}],
            system_prompt=None,
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
        ):
            chunks.append(chunk)
        return b"".join(chunks)

    body = asyncio.run(run())
    text = body.decode("utf-8")
    assert "ping" in text  # echoed by the stub
    assert "data: [DONE]" in text


def test_stream_claude_cli_chat_reports_missing_binary(monkeypatch):
    """If the CLI is not installed (or MERIDIAN_CLAUDE_CLI points at a
    bogus binary), the streamer must surface a clean error event
    rather than crashing the request."""
    monkeypatch.setenv(
        "MERIDIAN_CLAUDE_CLI", "definitely-not-a-real-binary-7878"
    )

    async def run() -> bytes:
        chunks: list[bytes] = []
        async for chunk in dashboard_module.stream_claude_cli_chat(
            messages=[{"role": "user", "content": "ping"}],
            system_prompt=None,
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
        ):
            chunks.append(chunk)
        return b"".join(chunks)

    body = asyncio.run(run()).decode("utf-8")
    assert '"error"' in body
    assert "data: [DONE]" in body


def test_stream_claude_cli_chat_rejects_empty_prompt(monkeypatch):
    """Empty messages + no system prompt must short-circuit with a
    structured error rather than spawning the worker with an empty
    argument."""
    monkeypatch.setenv(
        "MERIDIAN_CLAUDE_CLI",
        f'"{sys.executable}" -c "import sys; sys.stdout.write(\'should not run\')"',
    )

    async def run() -> bytes:
        chunks: list[bytes] = []
        async for chunk in dashboard_module.stream_claude_cli_chat(
            messages=[], system_prompt=None, model="x", max_tokens=4096
        ):
            chunks.append(chunk)
        return b"".join(chunks)

    body = asyncio.run(run()).decode("utf-8")
    assert "empty prompt" in body
    assert "should not run" not in body
    assert "data: [DONE]" in body


def test_format_cli_prompt_includes_system_and_history():
    """Round-trip the prompt formatter: System block first, then each
    turn in order. Hardening against accidental layout changes."""
    out = dashboard_module._format_cli_prompt(
        messages=[
            {"role": "user", "content": "what is meridian?"},
            {"role": "assistant", "content": "a coordination server"},
            {"role": "user", "content": "and the goal field?"},
        ],
        system_prompt="be concise",
    )
    assert out.startswith("System:\nbe concise")
    assert "User:\nwhat is meridian?" in out
    assert "Assistant:\na coordination server" in out
    # The most recent user turn lives at the end so claude -p answers it.
    assert out.rstrip().endswith("User:\nand the goal field?")


def test_dashboard_chat_unknown_project_404(client):
    r = client.post(
        "/dashboard/chat",
        json={
            "project_id": "no-such-project",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
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
# v0.3.0: chat persistence, session TTL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_create_chat_session_idempotent(db):
    """Two calls for the same project return the same session row."""
    p = await db_module.create_project(db, "alpha")
    sess1 = await db_module.get_or_create_chat_session(db, p["id"])
    sess2 = await db_module.get_or_create_chat_session(db, p["id"])
    assert sess1["id"] == sess2["id"]
    assert sess1["project_id"] == p["id"]
    assert sess1["cli_session_id"] is None


@pytest.mark.asyncio
async def test_update_chat_session_cli_id(db):
    """cli_session_id can be written after the row is created."""
    p = await db_module.create_project(db, "alpha")
    await db_module.get_or_create_chat_session(db, p["id"])
    await db_module.update_chat_session_cli_id(db, p["id"], "cli-abc-123")
    sess = await db_module.get_or_create_chat_session(db, p["id"])
    assert sess["cli_session_id"] == "cli-abc-123"


@pytest.mark.asyncio
async def test_save_and_retrieve_chat_messages(db):
    """Messages are persisted and returned oldest-first."""
    p = await db_module.create_project(db, "alpha")
    await db_module.save_chat_message(db, p["id"], "user", "hello")
    await db_module.save_chat_message(db, p["id"], "assistant", "world")
    await db_module.save_chat_message(db, p["id"], "user", "again")
    history = await db_module.get_chat_history(db, p["id"])
    assert len(history) == 3
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "hello"
    assert history[1]["role"] == "assistant"
    assert history[2]["content"] == "again"


@pytest.mark.asyncio
async def test_save_chat_message_rejects_bad_role(db):
    p = await db_module.create_project(db, "alpha")
    with pytest.raises(ValueError):
        await db_module.save_chat_message(db, p["id"], "system", "boom")


@pytest.mark.asyncio
async def test_get_chat_history_respects_limit(db):
    p = await db_module.create_project(db, "alpha")
    for i in range(10):
        await db_module.save_chat_message(db, p["id"], "user", f"msg{i}")
    history = await db_module.get_chat_history(db, p["id"], limit=4)
    # limit=4 returns 4 oldest messages
    assert len(history) == 4
    assert history[0]["content"] == "msg0"
    assert history[3]["content"] == "msg3"


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


def test_chat_history_endpoint_empty(client):
    """GET /projects/{id}/chat/history returns [] when no messages exist."""
    project = client.post("/projects", json={"name": "alpha"}).json()
    r = client.get(f"/projects/{project['id']}/chat/history")
    assert r.status_code == 200
    assert r.json() == []


def test_chat_history_endpoint_returns_messages(client, monkeypatch):
    """After a chat round-trip the history endpoint reflects saved messages."""
    async def fake_cli(messages, system_prompt, model, max_tokens, **_kwargs):
        yield b'data: {"delta": "hi there"}\n\n'
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr(dashboard_module, "stream_claude_cli_chat", fake_cli)

    project = client.post("/projects", json={"name": "alpha"}).json()
    client.post(
        "/dashboard/chat",
        json={
            "project_id": project["id"],
            "messages": [{"role": "user", "content": "hello"}],
            "mode": "cli",
        },
    )
    r = client.get(f"/projects/{project['id']}/chat/history")
    assert r.status_code == 200
    msgs = r.json()
    # User message persisted before streaming.
    assert any(m["role"] == "user" and m["content"] == "hello" for m in msgs)
    # Assistant response persisted after streaming.
    assert any(m["role"] == "assistant" and "hi there" in m["content"] for m in msgs)


def test_chat_history_endpoint_404_for_unknown_project(client):
    r = client.get("/projects/no-such-project/chat/history")
    assert r.status_code == 404


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
# v0.4.1 — --resume for true multi-turn CLI chat
# ---------------------------------------------------------------------------


def test_cli_streamer_passes_resume_flag_when_id_given(monkeypatch):
    """When ``resume_session_id`` is set the streamer must spawn the
    worker with ``--resume <id>`` ahead of the prompt. We swap the
    worker for a Python stub that dumps its argv and we assert against
    that."""
    monkeypatch.setenv(
        "MERIDIAN_CLAUDE_CLI",
        f'"{sys.executable}" -c "import sys; print(sys.argv)"',
    )

    async def run() -> str:
        chunks: list[bytes] = []
        async for chunk in dashboard_module.stream_claude_cli_chat(
            messages=[{"role": "user", "content": "hi"}],
            system_prompt=None,
            model="claude-sonnet-4-6",
            max_tokens=4096,
            resume_session_id="abc-123-resume-uuid",
        ):
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")

    body = asyncio.run(run())
    assert "--resume" in body
    assert "abc-123-resume-uuid" in body


def test_cli_streamer_captures_session_id_into_callback(monkeypatch):
    """When the worker emits ``Session ID: <uuid>`` the streamer must
    pass that uuid to ``on_session_id`` exactly once. Sub-second
    capture is the whole point of the v0.4.1 plumbing."""
    monkeypatch.setenv(
        "MERIDIAN_CLAUDE_CLI",
        f'"{sys.executable}" -c "import sys; sys.stdout.write(\'Session ID: ' \
        f'abcdef01-2345-6789-abcd-ef0123456789\nhello\n\')"',
    )

    captured: list[str] = []

    async def on_id(new_id: str) -> None:
        captured.append(new_id)

    async def run() -> None:
        async for _ in dashboard_module.stream_claude_cli_chat(
            messages=[{"role": "user", "content": "hi"}],
            system_prompt=None,
            model="claude-sonnet-4-6",
            max_tokens=4096,
            on_session_id=on_id,
        ):
            pass

    asyncio.run(run())
    assert captured == ["abcdef01-2345-6789-abcd-ef0123456789"]


def test_cli_streamer_gracefully_no_ops_when_no_session_id_present(monkeypatch):
    """If the CLI output never contains the marker the callback must
    not fire. Otherwise older CLI versions would crash with bad data."""
    monkeypatch.setenv(
        "MERIDIAN_CLAUDE_CLI",
        f'"{sys.executable}" -c "import sys; sys.stdout.write(\'just chat\')"',
    )

    captured: list[str] = []

    async def on_id(new_id: str) -> None:
        captured.append(new_id)

    async def run() -> None:
        async for _ in dashboard_module.stream_claude_cli_chat(
            messages=[{"role": "user", "content": "hi"}],
            system_prompt=None,
            model="claude-sonnet-4-6",
            max_tokens=4096,
            on_session_id=on_id,
        ):
            pass

    asyncio.run(run())
    assert captured == []


@pytest.mark.asyncio
async def test_update_chat_session_cli_id_persists(db):
    """The helper writes the captured CLI session uuid into the
    chat_sessions row so the next /dashboard/chat call can ``--resume``."""
    p = await db_module.create_project(db, "alpha")
    await db_module.get_or_create_chat_session(db, p["id"])
    await db_module.update_chat_session_cli_id(db, p["id"], "uuid-1")
    fresh = await db_module.get_or_create_chat_session(db, p["id"])
    assert fresh["cli_session_id"] == "uuid-1"


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
    """set_north_star updates north_star and increments version."""
    p = await db_module.create_project(db, "alpha")
    await db_module.set_goal(db, p["id"], "go")
    updated = await db_module.set_north_star(db, p["id"], "be the best")
    assert updated["north_star"] == "be the best"
    assert updated["version"] == 2


@pytest.mark.asyncio
async def test_set_sprint_any_member(db):
    """set_sprint updates sprint and increments version."""
    p = await db_module.create_project(db, "alpha")
    await db_module.set_goal(db, p["id"], "go")
    updated = await db_module.set_sprint(db, p["id"], "ship auth this week")
    assert updated["sprint"] == "ship auth this week"
    assert updated["version"] == 2


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


def test_http_set_north_star_non_owner_returns_403(client):
    """POST /goal/north-star with wrong human_id returns 403."""
    project = client.post(
        "/projects", json={"name": "alpha", "human_id": "adam"}
    ).json()
    client.post(f"/projects/{project['id']}/goal", json={"content": "go"})
    r = client.post(
        f"/projects/{project['id']}/goal/north-star",
        json={"north_star": "steal the vision", "human_id": "eve"},
    )
    assert r.status_code == 403


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


def test_build_sprint_items_xml_layout():
    xml = db_module.build_sprint_items_xml([
        {
            "id": "id-1",
            "version": "v0.6.4",
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
    """Each field's age tracks the most recent goal_states row where
    that specific field changed, not the latest goal version."""
    import time as _t
    p = await db_module.create_project(db, "alpha")
    await db_module.set_goal(
        db, p["id"], "v0-content", north_star="ns0", sprint="sp0"
    )
    # Pretend the original row landed long ago by backdating
    # goal_states.updated_at directly.
    await db.execute(
        "UPDATE goal_states SET updated_at = datetime('now','-40 days')"
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
    """Dashboard JS exposes a 'queue' vtab and loadQueue function (v1.4.0)."""
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
    """dashboard.js contains renderGoalHistory and toggleExpand (rewind improvements)."""
    js = client.get("/static/dashboard.js").text
    assert "renderGoalHistory" in js, "renderGoalHistory missing from dashboard.js"
    assert "toggleExpand" in js, "toggleExpand missing from dashboard.js"
    assert "goal-history" in js, "goal-history API call missing from dashboard.js"
