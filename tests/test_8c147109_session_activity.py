"""Tests for 8c147109 — session_activity ring-buffer heartbeat feed.

Covers:
- DB layer: record_session_activity / get_session_activity
- Ring-buffer pruning (50-entry cap)
- get_session_log MCP tool includes recent_activity
- get_session_activity MCP tool (standalone)
- Activity is recorded by _dispatch_mcp_tool for executor sessions
- Activity is NOT recorded for skip-listed tools (get_session_log, etc.)
"""
from __future__ import annotations

import asyncio
import json

import pytest

from meridian import db as db_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mcp_call(client, name, arguments, headers=None):
    """POST a tools/call to /mcp and return the response."""
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=headers or {},
    )


def _result(resp) -> dict:
    """Parse the JSON-RPC result payload from a /mcp response."""
    outer = resp.json()
    return json.loads(outer["result"]["content"][0]["text"])


def _setup_authed_project(client, project_name: str) -> tuple[str, dict]:
    """Create a project + tenant + API token.  Returns (project_id, mcp_headers)."""
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


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_and_get_session_activity(db):
    """record_session_activity persists entries, get_session_activity retrieves them."""
    p = await db_module.create_project(db, "sa-db-test")
    s = await db_module.register_session(db, p["id"], "sess-1")

    await db_module.record_session_activity(db, s["id"], "claim_sprint_item", "item_id=abc")
    await db_module.record_session_activity(db, s["id"], "read_file", "path=meridian/server.py")

    entries = await db_module.get_session_activity(db, s["id"])
    assert len(entries) == 2
    # Newest first
    assert entries[0]["tool_name"] == "read_file"
    assert entries[1]["tool_name"] == "claim_sprint_item"
    assert entries[0]["session_id"] == s["id"]


@pytest.mark.asyncio
async def test_get_session_activity_empty(db):
    """get_session_activity returns [] when no entries exist."""
    p = await db_module.create_project(db, "sa-empty-test")
    s = await db_module.register_session(db, p["id"], "sess-1")
    entries = await db_module.get_session_activity(db, s["id"])
    assert entries == []


@pytest.mark.asyncio
async def test_session_activity_ring_buffer_cap(db):
    """Ring-buffer prunes to the last 50 entries per session."""
    p = await db_module.create_project(db, "sa-ring-test")
    s = await db_module.register_session(db, p["id"], "sess-1")

    # Insert 60 entries (10 beyond the cap)
    for i in range(60):
        await db_module.record_session_activity(
            db, s["id"], f"tool_{i}", f"call {i}"
        )

    entries = await db_module.get_session_activity(db, s["id"], limit=100)
    assert len(entries) == 50
    # Newest 50 survive — tool_59 through tool_10
    tool_names = {e["tool_name"] for e in entries}
    assert "tool_59" in tool_names
    assert "tool_10" in tool_names
    # Oldest 10 pruned
    assert "tool_0" not in tool_names
    assert "tool_9" not in tool_names


@pytest.mark.asyncio
async def test_session_activity_limit_param(db):
    """get_session_activity respects the limit parameter."""
    p = await db_module.create_project(db, "sa-limit-test")
    s = await db_module.register_session(db, p["id"], "sess-1")

    for i in range(10):
        await db_module.record_session_activity(db, s["id"], f"tool_{i}", f"desc {i}")

    entries = await db_module.get_session_activity(db, s["id"], limit=3)
    assert len(entries) == 3


@pytest.mark.asyncio
async def test_session_activity_summary_truncated(db):
    """record_session_activity truncates summary to 200 chars."""
    p = await db_module.create_project(db, "sa-trunc-test")
    s = await db_module.register_session(db, p["id"], "sess-1")
    long_summary = "x" * 300
    await db_module.record_session_activity(db, s["id"], "some_tool", long_summary)
    entries = await db_module.get_session_activity(db, s["id"])
    assert len(entries) == 1
    assert len(entries[0]["summary"]) == 200


@pytest.mark.asyncio
async def test_session_activity_isolated_per_session(db):
    """Each session has its own ring-buffer — they don't interfere."""
    p = await db_module.create_project(db, "sa-iso-test")
    s1 = await db_module.register_session(db, p["id"], "sess-1")
    s2 = await db_module.register_session(db, p["id"], "sess-2")

    await db_module.record_session_activity(db, s1["id"], "tool_a", "desc a")
    await db_module.record_session_activity(db, s2["id"], "tool_b", "desc b")

    e1 = await db_module.get_session_activity(db, s1["id"])
    e2 = await db_module.get_session_activity(db, s2["id"])

    assert len(e1) == 1 and e1[0]["tool_name"] == "tool_a"
    assert len(e2) == 1 and e2[0]["tool_name"] == "tool_b"


# ---------------------------------------------------------------------------
# MCP tool: get_session_log includes recent_activity
# ---------------------------------------------------------------------------


def test_get_session_log_includes_activity(client):
    """get_session_log response includes recent_activity field."""
    pid, headers = _setup_authed_project(client, "sa-log-test")
    r2 = client.post(f"/projects/{pid}/start-session", json={"session_name": "sess"})
    sid = r2.json()["session_id"]

    # Log a task (also appends to transcript)
    _mcp_call(client, "log_task", {
        "session_id": sid, "project_id": pid, "description": "did something"
    }, headers)

    resp = _mcp_call(client, "get_session_log", {"session_id": sid}, headers)
    assert resp.status_code == 200
    result = _result(resp)

    assert "recent_activity" in result
    assert isinstance(result["recent_activity"], list)
    assert "activity_note" in result
    # task_count and transcript still present
    assert result["task_count"] == 1
    assert "did something" in result["transcript"]


def test_get_session_log_activity_field_present_even_when_empty(client):
    """recent_activity is present (as empty list) when no activity has been recorded."""
    pid, headers = _setup_authed_project(client, "sa-log-empty-test")
    r2 = client.post(f"/projects/{pid}/start-session", json={"session_name": "sess"})
    sid = r2.json()["session_id"]

    resp = _mcp_call(client, "get_session_log", {"session_id": sid}, headers)
    assert resp.status_code == 200
    result = _result(resp)
    assert "recent_activity" in result
    assert isinstance(result["recent_activity"], list)


# ---------------------------------------------------------------------------
# MCP tool: get_session_activity (standalone)
# ---------------------------------------------------------------------------


def test_mcp_get_session_activity_empty(client):
    """get_session_activity returns count:0 when session has no activity."""
    pid, headers = _setup_authed_project(client, "sa-mcp-empty")
    r2 = client.post(f"/projects/{pid}/start-session", json={"session_name": "sess"})
    sid = r2.json()["session_id"]

    resp = _mcp_call(client, "get_session_activity", {"session_id": sid}, headers)
    assert resp.status_code == 200
    result = _result(resp)
    assert result["session_id"] == sid
    assert result["count"] == 0
    assert result["activity"] == []


def test_mcp_get_session_activity_with_entries(client):
    """get_session_activity returns recorded entries."""
    pid, headers = _setup_authed_project(client, "sa-mcp-entries")
    r2 = client.post(f"/projects/{pid}/start-session", json={"session_name": "sess"})
    sid = r2.json()["session_id"]

    # Directly insert activity via the DB function
    async def _insert():
        db = client.app.state.db
        await db_module.record_session_activity(db, sid, "claim_sprint_item", "item_id=xyz")
        await db_module.record_session_activity(db, sid, "read_file", "path=foo.py")

    asyncio.run(_insert())

    resp = _mcp_call(client, "get_session_activity", {"session_id": sid}, headers)
    assert resp.status_code == 200
    result = _result(resp)
    assert result["count"] == 2
    tool_names = [e["tool_name"] for e in result["activity"]]
    assert "read_file" in tool_names
    assert "claim_sprint_item" in tool_names


def test_mcp_get_session_activity_limit(client):
    """get_session_activity respects the limit argument (capped at 50)."""
    pid, headers = _setup_authed_project(client, "sa-mcp-limit")
    r2 = client.post(f"/projects/{pid}/start-session", json={"session_name": "sess"})
    sid = r2.json()["session_id"]

    async def _insert():
        db = client.app.state.db
        for i in range(10):
            await db_module.record_session_activity(db, sid, f"tool_{i}", f"desc {i}")

    asyncio.run(_insert())

    resp = _mcp_call(
        client, "get_session_activity", {"session_id": sid, "limit": 3}, headers
    )
    result = _result(resp)
    assert result["count"] == 3


def test_mcp_get_session_activity_in_readonly_tools(client):
    """get_session_activity is registered as a read-only tool (works with a read-only token)."""
    from meridian.mcp_tools import _READ_ONLY_TOOLS
    assert "get_session_activity" in _READ_ONLY_TOOLS


# ---------------------------------------------------------------------------
# Activity recording by _dispatch_mcp_tool
# ---------------------------------------------------------------------------


def test_activity_recorded_for_executor_session(client):
    """The MCP dispatcher records activity for executor sessions automatically.

    Uses the MCP start_session tool with role=executor so the session ID is
    registered in _EXECUTOR_SESSIONS (the in-process dict); the REST
    /start-session endpoint bypasses MCP dispatch and would NOT register it.
    """
    pid, headers = _setup_authed_project(client, "sa-dispatch-test")
    # Use the MCP tool so role=executor triggers _EXECUTOR_SESSIONS.add()
    r2 = _mcp_call(client, "start_session", {
        "project_id": pid, "session_name": "exec-sess", "role": "executor"
    }, headers)
    assert r2.status_code == 200
    body = r2.json()
    sid = json.loads(body["result"]["content"][0]["text"]).get("session_id")
    assert sid, "start_session should return session_id"

    # Make a tool call that should trigger activity recording
    _mcp_call(client, "get_sprint_items", {"session_id": sid, "project_id": pid}, headers)

    resp = _mcp_call(client, "get_session_activity", {"session_id": sid}, headers)
    result = _result(resp)
    # At least one activity entry should be present (get_sprint_items)
    assert result["count"] >= 1
    tool_names = [e["tool_name"] for e in result["activity"]]
    assert "get_sprint_items" in tool_names


def test_activity_not_recorded_for_skip_tools(client):
    """get_session_log and get_session_activity are NOT recorded as activity entries."""
    pid, headers = _setup_authed_project(client, "sa-skip-test")
    r2 = _mcp_call(client, "start_session", {
        "project_id": pid, "session_name": "exec-sess", "role": "executor"
    }, headers)
    sid = json.loads(r2.json()["result"]["content"][0]["text"]).get("session_id")

    # Call get_session_log first (should not be recorded)
    _mcp_call(client, "get_session_log", {"session_id": sid}, headers)
    # Call get_session_activity (should not be recorded)
    _mcp_call(client, "get_session_activity", {"session_id": sid}, headers)

    resp = _mcp_call(client, "get_session_activity", {"session_id": sid}, headers)
    result = _result(resp)
    tool_names = [e["tool_name"] for e in result["activity"]]
    assert "get_session_log" not in tool_names
    assert "get_session_activity" not in tool_names
