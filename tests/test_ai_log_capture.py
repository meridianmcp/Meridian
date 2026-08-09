"""c5c3fc5f (Round 2, item R2-A) — wire canonical execution-event capture at
real boundaries.

SCOPE: this file tests the CAPTURE layer (meridian.session_tools) and its
real wiring, distinct from tests/test_ai_log_contract.py which covers the
CONTRACT (meridian.ai_log) and storage scaffold (meridian.db.ai_log) only —
see that file's own docstring. Coverage here:

  1. capture_event's never-raising contract: globally disabled (env var),
     missing project_id, and a storage-layer failure (secret-shaped payload
     rejected by meridian.secret_redaction) all degrade to a logged warning
     and None, never an exception — "disabled/failed sinks do not lose the
     local event receipt."
  2. Boundary helpers (capture_session_started/_ended, capture_tool_invoked/
     _completed, capture_process_registered/_released): correct event_type/
     actor_kind/payload_schema, and idempotency_key dedup (no duplicate
     events on a retried call for the same logical boundary instance).
  3. meridian.process_registry.register_process/release_process: the lease
     broker's own synchronous correctness is unaffected by a capture
     callback, in BOTH directions (capture succeeds; capture raises).
  4. Real wiring at meridian/mcp/handler.py::_dispatch_mcp_tool, end to end
     through the actual /mcp HTTP surface (mirrors
     tests/test_8c147109_session_activity.py's proven client-fixture
     pattern for the sibling activity-heartbeat side channel this capture
     block sits directly beside):
       * start_session (role=executor) and register_session each record one
         session.started event, keyed on the real session_id.
       * A subsequent executor-session tool call records a tool.completed
         event; a skip-listed tool (get_session_log) and a non-executor
         session's tool call do NOT.
       * Capture globally disabled via env var: the dispatch itself still
         succeeds (the session is created, the tool call still returns its
         normal result) even though zero ai_log_events rows are written —
         proving the "local event receipt" (the session/tool call's own
         result) is never lost when the sink is off.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from meridian import ai_log
from meridian import db as db_module
from meridian import process_registry
from meridian import session_tools


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


async def _events(db, project_id: str, **filters):
    return await db_module.list_events(db, project_id, **filters)


# ---------------------------------------------------------------------------
# 1. capture_event's never-raising contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_capture_event_records_a_real_row(db):
    pid = await _project(db, "cap-basic")
    result = await session_tools.capture_event(
        db, project_id=pid, event_type="tool.invoked", actor_kind="tool",
        actor_id="find_symbol", payload={"x": 1}, payload_schema="tool_call@1",
    )
    assert result is not None
    assert result["project_id"] == pid
    assert result["event_type"] == "tool.invoked"
    assert result["event_hash"].startswith("sha256:")  # canonical hash preserved
    rows = await _events(db, pid)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_capture_event_disabled_returns_none_and_writes_nothing(db, monkeypatch):
    monkeypatch.setenv("MERIDIAN_AI_LOG_CAPTURE_DISABLED", "true")
    pid = await _project(db, "cap-disabled")
    result = await session_tools.capture_event(
        db, project_id=pid, event_type="tool.invoked", actor_kind="tool",
    )
    assert result is None
    assert await _events(db, pid) == []


@pytest.mark.asyncio
async def test_capture_event_missing_project_id_returns_none_never_raises(db):
    result = await session_tools.capture_event(
        db, project_id=None, event_type="tool.invoked", actor_kind="tool",
    )
    assert result is None
    result2 = await session_tools.capture_event(
        db, project_id="", event_type="tool.invoked", actor_kind="tool",
    )
    assert result2 is None


@pytest.mark.asyncio
async def test_capture_event_storage_failure_swallowed_not_raised(db):
    """A secret-shaped payload makes meridian.db.ai_log.append_event raise
    ValueError (via secret_redaction.check_for_secrets) -- capture_event
    must swallow it, log, and return None rather than propagating, and must
    not have inserted a row."""
    pid = await _project(db, "cap-secret-leak")
    result = await session_tools.capture_event(
        db, project_id=pid, event_type="tool.invoked", actor_kind="tool",
        payload={"token": "sk-ant-" + ("a" * 40)},
    )
    assert result is None
    assert await _events(db, pid) == []


@pytest.mark.asyncio
async def test_capture_event_invalid_envelope_swallowed_not_raised(db):
    """A bad event_type (no namespace dot) makes ExecutionEvent construction
    raise ExecutionEventError inside append_event -- also swallowed."""
    pid = await _project(db, "cap-bad-envelope")
    result = await session_tools.capture_event(
        db, project_id=pid, event_type="not-namespaced", actor_kind="tool",
    )
    assert result is None
    assert await _events(db, pid) == []


def test_capture_enabled_default_true(monkeypatch):
    monkeypatch.delenv("MERIDIAN_AI_LOG_CAPTURE_DISABLED", raising=False)
    assert session_tools.capture_enabled() is True


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes"])
def test_capture_enabled_false_for_truthy_values(monkeypatch, value):
    monkeypatch.setenv("MERIDIAN_AI_LOG_CAPTURE_DISABLED", value)
    assert session_tools.capture_enabled() is False


# ---------------------------------------------------------------------------
# 2. Boundary helpers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_capture_session_started_shape_and_idempotency(db):
    pid = await _project(db, "cap-session-started")
    first = await session_tools.capture_session_started(
        db, project_id=pid, session_id="sess-1", human_id="adam",
        client="claude-code", role="executor",
    )
    assert first["event_type"] == "session.started"
    assert first["actor_kind"] == "session"
    assert first["session_id"] == "sess-1"
    assert first["payload"] == {
        "human_id": "adam", "client": "claude-code", "role": "executor",
    }
    assert first["payload_schema"] == "session_started@1"

    # A retried/duplicate call for the SAME session must not double-record.
    second = await session_tools.capture_session_started(
        db, project_id=pid, session_id="sess-1", human_id="adam",
        client="claude-code", role="executor",
    )
    assert second["id"] == first["id"]
    rows = await _events(db, pid, event_type="session.started")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_capture_session_ended_shape_and_idempotency(db):
    pid = await _project(db, "cap-session-ended")
    first = await session_tools.capture_session_ended(
        db, project_id=pid, session_id="sess-1", reason="handoff",
    )
    assert first["event_type"] == "session.ended"
    assert first["payload"] == {"reason": "handoff"}
    second = await session_tools.capture_session_ended(
        db, project_id=pid, session_id="sess-1", reason="handoff",
    )
    assert second["id"] == first["id"]


@pytest.mark.asyncio
async def test_capture_tool_invoked_and_completed_shape(db):
    pid = await _project(db, "cap-tool")
    invoked = await session_tools.capture_tool_invoked(
        db, project_id=pid, tool_name="get_sprint_items",
        correlation_id="corr-1", session_id="sess-1", args_summary="project_id=abc",
    )
    assert invoked["event_type"] == "tool.invoked"
    assert invoked["actor_kind"] == "tool"
    assert invoked["correlation_id"] == "corr-1"
    assert invoked["payload"]["tool"] == "get_sprint_items"

    completed = await session_tools.capture_tool_completed(
        db, project_id=pid, tool_name="get_sprint_items", ok=True,
        duration_ms=12.5, correlation_id="corr-1", parent_event_id=invoked["id"],
        session_id="sess-1",
    )
    assert completed["event_type"] == "tool.completed"
    assert completed["parent_event_id"] == invoked["id"]
    assert completed["payload"] == {
        "tool": "get_sprint_items", "ok": True, "duration_ms": 12.5,
        "error_type": None,
    }

    # No idempotency_key at this generic layer -- two calls both land.
    await session_tools.capture_tool_completed(
        db, project_id=pid, tool_name="get_sprint_items", ok=True,
        duration_ms=1.0, correlation_id="corr-1", session_id="sess-1",
    )
    rows = await _events(db, pid, event_type="tool.completed")
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_capture_process_registered_and_released_idempotency(db):
    pid = await _project(db, "cap-process")
    reg1 = await session_tools.capture_process_registered(
        db, project_id=pid, run_id="run-1", client="codex",
        executable="node", cwd="/tmp/repo",
    )
    assert reg1["event_type"] == "agent.registered"
    assert reg1["actor_kind"] == "system"
    assert reg1["payload"]["run_id"] == "run-1"
    reg2 = await session_tools.capture_process_registered(
        db, project_id=pid, run_id="run-1", client="codex",
    )
    assert reg2["id"] == reg1["id"]  # deduped by run_id

    rel1 = await session_tools.capture_process_released(
        db, project_id=pid, run_id="run-1", client="codex",
    )
    assert rel1["event_type"] == "agent.released"
    rel2 = await session_tools.capture_process_released(
        db, project_id=pid, run_id="run-1", client="codex",
    )
    assert rel2["id"] == rel1["id"]  # deduped by run_id


# ---------------------------------------------------------------------------
# 3. meridian.process_registry.register_process / release_process
# ---------------------------------------------------------------------------

@pytest.fixture
def broker(tmp_path):
    return process_registry.ProcessLeaseBroker(
        persist_path=tmp_path / "leases.json"
    )


@pytest.mark.asyncio
async def test_register_process_returns_real_lease_without_capture(broker):
    """No capture callable supplied -- behaves exactly like the pre-existing
    synchronous broker.register(), just awaitable."""
    lease = await process_registry.register_process(broker, "codex", 4242)
    assert lease.client == "codex"
    assert lease.pid == 4242
    assert broker.list_leases() == [lease]


@pytest.mark.asyncio
async def test_register_process_invokes_capture_with_the_real_lease(broker):
    seen = []

    async def _capture(lease):
        seen.append(lease)

    lease = await process_registry.register_process(
        broker, "codex", 4242, capture=_capture,
    )
    assert seen == [lease]


@pytest.mark.asyncio
async def test_register_process_capture_failure_does_not_break_registration(broker):
    """A raising capture callback must never prevent (or undo) the lease
    itself from being registered -- the lease IS the local receipt."""
    async def _boom(lease):
        raise RuntimeError("sink unavailable")

    lease = await process_registry.register_process(
        broker, "codex", 4242, capture=_boom,
    )
    assert lease.client == "codex"
    assert broker.list_leases() == [lease]  # registration still landed


@pytest.mark.asyncio
async def test_release_process_capture_failure_does_not_break_release(broker):
    lease = await process_registry.register_process(broker, "codex", 4242)

    async def _boom(released_lease):
        raise RuntimeError("sink unavailable")

    released = await process_registry.release_process(
        broker, "codex", lease.run_id, capture=_boom,
    )
    assert released.released is True
    assert broker.list_leases() == []  # released leases are excluded by default


@pytest.mark.asyncio
async def test_register_then_release_process_end_to_end_with_real_capture(db, broker):
    """Full pipeline: synchronous lease registration/release AND the
    corresponding ai_log events, wired via session_tools, all through the
    real register_process/release_process functions."""
    pid = await _project(db, "cap-process-e2e")

    lease = await process_registry.register_process(
        broker, "claude-code", 555, executable="node", cwd="/repo",
        capture=lambda lease: session_tools.capture_process_registered(
            db, project_id=pid, run_id=lease.run_id, client=lease.client,
            executable=lease.executable, cwd=lease.cwd,
        ),
    )
    assert lease.pid == 555
    registered_rows = await _events(db, pid, event_type="agent.registered")
    assert len(registered_rows) == 1
    assert registered_rows[0]["payload"]["run_id"] == lease.run_id

    released = await process_registry.release_process(
        broker, "claude-code", lease.run_id,
        capture=lambda leased: session_tools.capture_process_released(
            db, project_id=pid, run_id=leased.run_id, client=leased.client,
        ),
    )
    assert released.released is True
    released_rows = await _events(db, pid, event_type="agent.released")
    assert len(released_rows) == 1


# ---------------------------------------------------------------------------
# 4. Real wiring: meridian/mcp/handler.py::_dispatch_mcp_tool, end to end
# ---------------------------------------------------------------------------

def _mcp_call(client, name, arguments, headers=None):
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=headers or {},
    )


def _result(resp) -> dict:
    outer = resp.json()
    return json.loads(outer["result"]["content"][0]["text"])


def _setup_authed_project(client, project_name: str) -> "tuple[str, dict]":
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


def test_start_session_records_session_started_event(client):
    pid, headers = _setup_authed_project(client, "cap-e2e-start-session")
    r = _mcp_call(client, "start_session", {
        "project_id": pid, "session_name": "exec-sess", "role": "executor",
    }, headers)
    assert r.status_code == 200
    sid = _result(r)["session_id"]

    async def _fetch():
        return await db_module.list_events(
            client.app.state.db, pid, event_type="session.started",
        )
    rows = asyncio.run(_fetch())
    assert len(rows) == 1
    assert rows[0]["session_id"] == sid
    assert rows[0]["payload"]["role"] == "executor"


def test_register_session_records_session_started_event(client):
    pid, headers = _setup_authed_project(client, "cap-e2e-register-session")
    r = _mcp_call(client, "register_session", {
        "project_id": pid, "session_name": "manual-sess",
    }, headers)
    assert r.status_code == 200
    sid = _result(r)["id"]

    async def _fetch():
        return await db_module.list_events(
            client.app.state.db, pid, event_type="session.started",
        )
    rows = asyncio.run(_fetch())
    assert len(rows) == 1
    assert rows[0]["session_id"] == sid


def test_starting_the_same_session_twice_does_not_duplicate_the_event(client):
    """mode='continue' (or the 5-min heartbeat auto-continuation window)
    resolves to the SAME session_id -- session.started must still only be
    recorded once, proven end to end through the real dispatch path."""
    pid, headers = _setup_authed_project(client, "cap-e2e-continue")
    r1 = _mcp_call(client, "start_session", {
        "project_id": pid, "session_name": "exec-sess", "role": "executor",
    }, headers)
    sid1 = _result(r1)["session_id"]
    r2 = _mcp_call(client, "start_session", {
        "project_id": pid, "session_name": "exec-sess", "role": "executor",
        "mode": "continue",
    }, headers)
    sid2 = _result(r2)["session_id"]
    assert sid1 == sid2

    async def _fetch():
        return await db_module.list_events(
            client.app.state.db, pid, event_type="session.started",
        )
    rows = asyncio.run(_fetch())
    assert len(rows) == 1


def test_executor_tool_call_records_tool_completed_event(client):
    pid, headers = _setup_authed_project(client, "cap-e2e-tool")
    r = _mcp_call(client, "start_session", {
        "project_id": pid, "session_name": "exec-sess", "role": "executor",
    }, headers)
    sid = _result(r)["session_id"]

    _mcp_call(client, "get_sprint_items", {"session_id": sid, "project_id": pid}, headers)

    async def _fetch():
        return await db_module.list_events(
            client.app.state.db, pid, event_type="tool.completed",
        )
    rows = asyncio.run(_fetch())
    tool_names = [r["payload"]["tool"] for r in rows]
    assert "get_sprint_items" in tool_names
    hit = next(r for r in rows if r["payload"]["tool"] == "get_sprint_items")
    assert hit["session_id"] == sid
    assert hit["payload"]["ok"] is True
    assert isinstance(hit["payload"]["duration_ms"], (int, float))


def test_skip_listed_tool_does_not_record_tool_completed(client):
    pid, headers = _setup_authed_project(client, "cap-e2e-skip")
    r = _mcp_call(client, "start_session", {
        "project_id": pid, "session_name": "exec-sess", "role": "executor",
    }, headers)
    sid = _result(r)["session_id"]

    _mcp_call(client, "get_session_log", {"session_id": sid}, headers)

    async def _fetch():
        return await db_module.list_events(
            client.app.state.db, pid, event_type="tool.completed",
        )
    rows = asyncio.run(_fetch())
    tool_names = [r["payload"]["tool"] for r in rows]
    assert "get_session_log" not in tool_names


def test_non_executor_session_tool_call_does_not_record_tool_completed(client):
    """register_session (session_type=human, never added to
    _EXECUTOR_SESSIONS) is the non-executor control case."""
    pid, headers = _setup_authed_project(client, "cap-e2e-non-executor")
    r = _mcp_call(client, "register_session", {
        "project_id": pid, "session_name": "human-sess",
    }, headers)
    sid = _result(r)["id"]

    _mcp_call(client, "get_sprint_items", {"session_id": sid, "project_id": pid}, headers)

    async def _fetch():
        return await db_module.list_events(
            client.app.state.db, pid, event_type="tool.completed",
        )
    rows = asyncio.run(_fetch())
    assert rows == []


def test_capture_disabled_env_var_still_lets_start_session_succeed(client, monkeypatch):
    """The kill switch must never break the boundary operation itself --
    only suppress the secondary ai_log_events write."""
    monkeypatch.setenv("MERIDIAN_AI_LOG_CAPTURE_DISABLED", "true")
    pid, headers = _setup_authed_project(client, "cap-e2e-disabled")
    r = _mcp_call(client, "start_session", {
        "project_id": pid, "session_name": "exec-sess", "role": "executor",
    }, headers)
    assert r.status_code == 200
    sid = _result(r)["session_id"]
    assert sid  # the session itself was created successfully

    async def _fetch():
        return await db_module.list_events(client.app.state.db, pid)
    rows = asyncio.run(_fetch())
    assert rows == []  # capture was globally disabled -- no ai_log rows at all
