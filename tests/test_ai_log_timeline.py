"""79491e26 (R2-F) — reconstruct deterministic run timelines and
planner/executor corrective handoffs from durable state.

SCOPE: this file tests the additions this sprint item layers on top of the
9e83be4a/ea972129 ai_log scaffold (tests/test_ai_log_contract.py and
tests/test_ai_log_retention.py cover that scaffold itself and are NOT
duplicated here):

  1. meridian.db.ai_log.build_run_timeline — deterministic occurred_at-order
     reconstruction (distinct from list_events' recorded_at-arrival-order
     browsing), project/session/correlation scoping, since_occurred_at
     bounding, and newest-kept truncation.
  2. meridian.db.ai_log.AiLogStore.timeline — the facade delegate.
  3. meridian.handoff.build_run_timeline_for_handoff — the compact,
     payload-free projection embedded in a handoff / MCP response, and its
     best-effort (never-raises) contract.
  4. meridian.handoff._render_delta_handoff — the new optional
     ``<run_timeline>`` tag, additive (omitted when None).
  5. meridian.handoff.generate_handoff(mode="delta") — end-to-end wiring:
     durable ai_log events show up in the rendered delta body.
  6. meridian.routes.handoff.record_handoff_correction_endpoint — a
     corrective handoff durably traces a ``handoff.correction_recorded`` /
     ``handoff.correction_regenerated`` ai_log event (best-effort — an
     ai_log write failure never blocks the correction itself), so a
     planner/executor corrective handoff is reconstructible from the same
     durable timeline instead of only from the handoff_corrections row.
  7. meridian.mcp.handler._handle_task_tools("generate_handoff", ...) — the
     ``run_timeline`` field on the MCP tool's response.

It deliberately does NOT cover the capture/ingestion pipeline that decides
WHEN/WHERE append_event gets called from ordinary request paths (sibling
item c5c3fc5f's job) or artifact-lifecycle/retention (ea972129 /
tests/test_ai_log_retention.py).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian.db import ai_log as ai_log_module
from meridian.routes import handoff as handoff_routes_module


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


async def _seed_handoff(db, name: str, tmp_path):
    """Create a project with a goal + one FRESH generated handoff row.
    Returns (project_id, handoff_row). Mirrors the _seed_handoff helper used
    in tests/test_cov_handoff.py and tests/test_handoff_amend_vs_fresh.py."""
    pid = await _project(db, name)
    await db_module.set_goal(db, pid, "ship it", sprint="s1")
    await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True,
    )
    rows = await db_module.get_handoffs(db, pid, limit=1)
    return pid, rows[0]


class _FakeRequest:
    """Minimal stand-in for starlette.Request satisfying exactly what
    meridian._deps._db()/_data_dir() read — the same hand-rolled fake-request
    pattern already used elsewhere in this suite (e.g.
    tests/test_cov_route_export.py, tests/test_tunnel_diagnostics.py) rather
    than paying for a full TestClient/ASGI round trip to exercise one route
    function directly. Setting ``state._db_conn`` up front makes ``_db()``
    return it immediately (its own memoization fast path), so no cookie/
    tenant machinery is ever touched."""

    def __init__(self, db, data_dir: str, body: dict):
        self.state = SimpleNamespace(_db_conn=db)
        self.app = SimpleNamespace(state=SimpleNamespace(db=db, data_dir=data_dir))
        self.cookies: dict = {}
        self._body = body

    async def json(self):
        return self._body


# ---------------------------------------------------------------------------
# 1. db.ai_log.build_run_timeline
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_run_timeline_orders_ascending_by_occurred_at(db):
    pid = await _project(db, "ai-log-timeline-order")
    await db_module.append_event(
        db, pid, "session.started", "session",
        occurred_at="2026-01-01T00:00:03.000Z",
    )
    await db_module.append_event(
        db, pid, "tool.invoked", "tool",
        occurred_at="2026-01-01T00:00:01.000Z",
    )
    await db_module.append_event(
        db, pid, "tool.completed", "tool",
        occurred_at="2026-01-01T00:00:02.000Z",
    )

    timeline = await ai_log_module.build_run_timeline(db, pid)

    assert [e["event_type"] for e in timeline] == [
        "tool.invoked", "tool.completed", "session.started",
    ]
    assert [e["occurred_at"] for e in timeline] == sorted(
        e["occurred_at"] for e in timeline
    )


@pytest.mark.asyncio
async def test_build_run_timeline_is_deterministic_across_repeated_calls(db):
    pid = await _project(db, "ai-log-timeline-deterministic")
    for _ in range(4):
        await db_module.append_event(
            db, pid, "tool.invoked", "tool",
            occurred_at="2026-01-01T00:00:00.000Z",  # identical timestamp
        )

    first = await ai_log_module.build_run_timeline(db, pid)
    second = await ai_log_module.build_run_timeline(db, pid)

    assert [e["id"] for e in first] == [e["id"] for e in second]
    assert len(first) == 4


@pytest.mark.asyncio
async def test_build_run_timeline_filters_by_session_id(db):
    pid = await _project(db, "ai-log-timeline-session")
    await db_module.append_event(
        db, pid, "session.started", "session", session_id="s1",
    )
    await db_module.append_event(
        db, pid, "session.started", "session", session_id="s2",
    )

    timeline = await ai_log_module.build_run_timeline(db, pid, session_id="s1")

    assert len(timeline) == 1
    assert timeline[0]["session_id"] == "s1"


@pytest.mark.asyncio
async def test_build_run_timeline_filters_by_correlation_id(db):
    pid = await _project(db, "ai-log-timeline-correlation")
    await db_module.append_event(
        db, pid, "tool.invoked", "tool", correlation_id="run-a",
    )
    await db_module.append_event(
        db, pid, "tool.invoked", "tool", correlation_id="run-b",
    )

    timeline = await ai_log_module.build_run_timeline(
        db, pid, correlation_id="run-a",
    )

    assert len(timeline) == 1
    assert timeline[0]["correlation_id"] == "run-a"


@pytest.mark.asyncio
async def test_build_run_timeline_since_occurred_at_is_inclusive(db):
    pid = await _project(db, "ai-log-timeline-since")
    await db_module.append_event(
        db, pid, "a.one", "system", occurred_at="2026-01-01T00:00:00.000Z",
    )
    await db_module.append_event(
        db, pid, "a.two", "system", occurred_at="2026-01-02T00:00:00.000Z",
    )

    timeline = await ai_log_module.build_run_timeline(
        db, pid, since_occurred_at="2026-01-02T00:00:00.000Z",
    )

    assert [e["event_type"] for e in timeline] == ["a.two"]


@pytest.mark.asyncio
async def test_build_run_timeline_limit_keeps_newest_events(db):
    pid = await _project(db, "ai-log-timeline-limit")
    for i in range(5):
        await db_module.append_event(
            db, pid, "a.tick", "system",
            occurred_at=f"2026-01-01T00:00:0{i}.000Z",
        )

    timeline = await ai_log_module.build_run_timeline(db, pid, limit=2)

    # Truncation drops the OLDEST first; the surviving window stays ordered
    # ascending, so this is the newest 2 of 5 ticks, in order.
    assert [e["occurred_at"] for e in timeline] == [
        "2026-01-01T00:00:03.000Z", "2026-01-01T00:00:04.000Z",
    ]


@pytest.mark.asyncio
async def test_build_run_timeline_is_project_scoped(db):
    pid_a = await _project(db, "ai-log-timeline-scope-a")
    pid_b = await _project(db, "ai-log-timeline-scope-b")
    await db_module.append_event(db, pid_a, "a.one", "system")
    await db_module.append_event(db, pid_b, "b.one", "system")

    timeline_a = await ai_log_module.build_run_timeline(db, pid_a)

    assert len(timeline_a) == 1
    assert timeline_a[0]["event_type"] == "a.one"


@pytest.mark.asyncio
async def test_build_run_timeline_empty_when_no_events(db):
    pid = await _project(db, "ai-log-timeline-empty")
    assert await ai_log_module.build_run_timeline(db, pid) == []


# ---------------------------------------------------------------------------
# 2. AiLogStore.timeline facade
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ai_log_store_timeline_delegates_correctly(db):
    pid = await _project(db, "ai-log-timeline-facade")
    store = db_module.AiLogStore(db, pid)
    await store.append("session.started", "session", occurred_at="2026-01-01T00:00:01.000Z")
    await store.append("session.ended", "session", occurred_at="2026-01-01T00:00:02.000Z")

    timeline = await store.timeline()

    assert [e["event_type"] for e in timeline] == ["session.started", "session.ended"]


# ---------------------------------------------------------------------------
# 3. handoff.build_run_timeline_for_handoff
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_run_timeline_for_handoff_none_when_no_events(db):
    pid = await _project(db, "ai-log-timeline-handoff-empty")
    assert await handoff_module.build_run_timeline_for_handoff(db, pid) is None


@pytest.mark.asyncio
async def test_build_run_timeline_for_handoff_compact_projection(db):
    pid = await _project(db, "ai-log-timeline-handoff-compact")
    await db_module.append_event(
        db, pid, "tool.invoked", "tool",
        source="mcp", payload={"symbol": "should_not_leak"},
    )

    result = await handoff_module.build_run_timeline_for_handoff(db, pid)

    assert result is not None
    assert result["schema_version"] == 1
    assert result["project_id"] == pid
    assert result["event_count"] == 1
    event = result["events"][0]
    assert event["event_type"] == "tool.invoked"
    assert event["source"] == "mcp"
    assert "payload" not in event  # compact projection omits payload content
    assert "should_not_leak" not in str(result)


@pytest.mark.asyncio
async def test_build_run_timeline_for_handoff_degrades_to_none_on_error(db, monkeypatch):
    pid = await _project(db, "ai-log-timeline-handoff-degrade")

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated ai_log read failure")

    monkeypatch.setattr(ai_log_module, "build_run_timeline", _boom)

    assert await handoff_module.build_run_timeline_for_handoff(db, pid) is None


# ---------------------------------------------------------------------------
# 4. _render_delta_handoff <run_timeline> tag
# ---------------------------------------------------------------------------

_DELTA_KWARGS = dict(
    generated_at="2026-01-01T00:00:00Z",
    completed_items=[],
    in_progress_items=[],
    pending_sprint_items=[],
    quick_start_goal="do the thing",
)


def test_render_delta_handoff_omits_run_timeline_tag_when_none():
    content = handoff_module._render_delta_handoff(
        {"id": "p1", "name": "proj"}, **_DELTA_KWARGS,
    )
    assert "<run_timeline>" not in content


def test_render_delta_handoff_embeds_run_timeline_tag_when_given():
    timeline = {
        "schema_version": 1, "project_id": "p1", "session_id": None,
        "correlation_id": None, "event_count": 1,
        "events": [{"event_type": "tool.invoked", "occurred_at": "2026-01-01T00:00:00.000Z"}],
    }
    content = handoff_module._render_delta_handoff(
        {"id": "p1", "name": "proj"}, run_timeline=timeline, **_DELTA_KWARGS,
    )
    assert "<run_timeline>" in content
    assert "</run_timeline>" in content
    assert '"event_type":"tool.invoked"' in content


# ---------------------------------------------------------------------------
# 5. generate_handoff(mode="delta") end-to-end wiring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_handoff_delta_embeds_run_timeline_from_durable_events(db, tmp_path):
    pid = await _project(db, "ai-log-timeline-delta-e2e")
    await db_module.set_goal(db, pid, "ship it", sprint="s1")
    await db_module.append_event(
        db, pid, "session.started", "session", source="mcp",
    )

    _, content, _ = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="delta",
    )

    assert "<run_timeline>" in content
    assert '"event_type":"session.started"' in content


@pytest.mark.asyncio
async def test_generate_handoff_delta_omits_run_timeline_when_no_durable_events(db, tmp_path):
    pid = await _project(db, "ai-log-timeline-delta-empty")
    await db_module.set_goal(db, pid, "ship it", sprint="s1")

    _, content, _ = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="delta",
    )

    assert "<run_timeline>" not in content


# ---------------------------------------------------------------------------
# 6. routes.handoff.record_handoff_correction_endpoint durable trace
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_handoff_correction_endpoint_logs_durable_event(db, tmp_path):
    pid, h = await _seed_handoff(db, "ai-log-timeline-corr-record", tmp_path)
    req = _FakeRequest(
        db, str(tmp_path),
        {
            "source_handoff_id": h["id"],
            "blocker_classification": "pointer_unresolved",
            "session_id": "sess-1",
        },
    )

    resp = await handoff_routes_module.record_handoff_correction_endpoint(pid, req)

    assert resp["regenerated"] is False
    timeline = await ai_log_module.build_run_timeline(db, pid)
    assert len(timeline) == 1
    event = timeline[0]
    assert event["event_type"] == "handoff.correction_recorded"
    assert event["session_id"] == "sess-1"
    assert event["correlation_id"] == h["id"]
    assert event["payload"]["correction_id"] == resp["correction"]["id"]
    assert event["payload"]["blocker_classification"] == "pointer_unresolved"


@pytest.mark.asyncio
async def test_record_handoff_correction_endpoint_with_regenerate_logs_both_events(db, tmp_path):
    pid, h = await _seed_handoff(db, "ai-log-timeline-corr-regen", tmp_path)
    req = _FakeRequest(
        db, str(tmp_path),
        {
            "source_handoff_id": h["id"],
            "blocker_classification": "other",
            "regenerate": True,
        },
    )

    resp = await handoff_routes_module.record_handoff_correction_endpoint(pid, req)

    assert resp["regenerated"] is True
    timeline = await ai_log_module.build_run_timeline(db, pid)
    event_types = [e["event_type"] for e in timeline]
    assert event_types == ["handoff.correction_recorded", "handoff.correction_regenerated"]
    # Both events share the source handoff id as their correlation id, so a
    # reader can group the full correction lifecycle on one timeline.
    assert {e["correlation_id"] for e in timeline} == {h["id"]}
    assert timeline[1]["payload"]["new_handoff_id"]


@pytest.mark.asyncio
async def test_record_handoff_correction_endpoint_survives_ai_log_write_failure(db, tmp_path, monkeypatch):
    pid, h = await _seed_handoff(db, "ai-log-timeline-corr-failopen", tmp_path)

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated ai_log write failure")

    monkeypatch.setattr(ai_log_module.AiLogStore, "append", _boom)

    req = _FakeRequest(
        db, str(tmp_path),
        {"source_handoff_id": h["id"], "blocker_classification": "other"},
    )
    resp = await handoff_routes_module.record_handoff_correction_endpoint(pid, req)

    # The correction itself must still succeed even though ai_log logging
    # blew up — best-effort, never blocks the endpoint's real contract.
    assert resp["regenerated"] is False
    assert resp["correction"]["source_handoff_id"] == h["id"]


@pytest.mark.asyncio
async def test_record_handoff_correction_endpoint_rejects_invalid_source_without_logging(db, tmp_path):
    pid = await _project(db, "ai-log-timeline-corr-invalid")
    req = _FakeRequest(
        db, str(tmp_path),
        {"source_handoff_id": "nope", "blocker_classification": "other"},
    )

    with pytest.raises(Exception):  # HTTPException — never reaches the ai_log append
        await handoff_routes_module.record_handoff_correction_endpoint(pid, req)

    assert await ai_log_module.build_run_timeline(db, pid) == []


# ---------------------------------------------------------------------------
# 7. mcp.handler generate_handoff dispatch — run_timeline field
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_dispatch_generate_handoff_surfaces_run_timeline_field(db, tmp_path):
    import meridian.server  # noqa: F401 — load the server before handler to avoid its import cycle
    from meridian.mcp import handler as mcp_handler

    pid = await _project(db, "ai-log-timeline-mcp")
    await db_module.set_goal(db, pid, "ship it", sprint="s1")
    await db_module.append_event(db, pid, "session.started", "session", source="mcp")

    result = await mcp_handler._handle_task_tools(
        "generate_handoff",
        {"project_id": pid, "mode": "delta"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )

    assert result["run_timeline"] is not None
    assert result["run_timeline"]["event_count"] == 1
    assert result["run_timeline"]["events"][0]["event_type"] == "session.started"
    assert "<run_timeline>" in result["content"]


@pytest.mark.asyncio
async def test_mcp_dispatch_generate_handoff_run_timeline_none_when_no_events(db, tmp_path):
    import meridian.server  # noqa: F401
    from meridian.mcp import handler as mcp_handler

    pid = await _project(db, "ai-log-timeline-mcp-empty")
    await db_module.set_goal(db, pid, "ship it", sprint="s1")

    result = await mcp_handler._handle_task_tools(
        "generate_handoff",
        {"project_id": pid, "mode": "full"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )

    assert result["run_timeline"] is None
