"""2a654cb0 — durable wave-run state, append/supersede history, idempotent finalization.

Coverage:
  1.  create_wave_run pins the board snapshot + revision hash and opens 'planned'.
  2.  The immutable wave_run_id survives every transition.
  3.  Valid transitions advance; invalid transitions raise; terminal states are terminal.
  4.  Leaving a halted state for running/ready_to_resume emits a 'resumed' event.
  5.  Event history is append-only with a monotonic per-run seq.
  6.  supersede_wave_run_event appends a correction and never mutates the old body.
  7.  Double-superseding the same event is refused (correction chains stay linear).
  8.  include_superseded=False hides corrected events.
  9.  Degraded-tool provenance is recorded and deduplicated on (tool, reason).
 10.  Children upsert idempotently and only emit an event on a real change.
 11.  Finalization happy path -> merged, exactly one 'finalized' event.
 12.  Retried finalization is idempotent: no duplicate completion, no duplicate events.
 13.  A failed failure_mode='stop' child blocks finalization; 'continue' does not.
 14.  Finalizer evidence must be a real run_verification result.
 15.  A stale expected_revision_hash fails closed.
 16.  An aborted run cannot be finalized.
 17.  Migration creates all three tables; PG registry includes the mirror.
 18.  MCP tools are registered and dispatch end-to-end.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import handoff as h
from meridian import server as srv
from meridian.db.wave_runs import WaveRunFinalizationBlocked
from meridian.mcp_tools import (
    _MCP_TOOLS_LIST,
    _TOOL_CATEGORY,
    _TOOL_EXAMPLES,
    _TOOL_ROLE_RELEVANCE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GOOD_EVIDENCE = {
    "status": "ok",
    "exit_code": 0,
    "passed": 1780,
    "failed": 0,
    "stdout_tail": "1780 passed in 92.1s",
}


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


async def _run(db, project_id: str, **kwargs):
    """Create a wave run with a real board snapshot pinned."""
    snapshot = await db_module.build_board_snapshot(db, project_id)
    return await db_module.create_wave_run(
        db, project_id, snapshot=snapshot, **kwargs
    )


# ---------------------------------------------------------------------------
# 1-2. Creation, pinned snapshot, immutable id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_pins_snapshot_and_opens_planned(db):
    pid = await _project(db, "wr-create")
    await db_module.add_sprint_item(db, pid, "v9.9.9", "FEAT: something")

    snapshot = await db_module.build_board_snapshot(db, pid)
    run = await db_module.create_wave_run(
        db, pid, version="v9.9.9", wave_label="wave-1",
        snapshot=snapshot, item_ids=["a", "b"], actor="sess-1",
    )

    assert run["status"] == "planned"
    assert run["revision_hash"] == snapshot["revision_hash"]
    # The monotonic counter starts at 1 for a bucket's first recorded revision.
    assert run["revision_counter"] == 1
    assert run["item_ids"] == ["a", "b"]
    assert run["degraded_tools"] == []
    assert run["finalized_at"] is None
    # The full snapshot is pinned, not just its hash.
    assert run["board_snapshot"]["revision_hash"] == snapshot["revision_hash"]

    events = await db_module.get_wave_run_events(db, run["id"])
    assert [e["event_type"] for e in events] == ["created"]
    assert events[0]["to_status"] == "planned"
    assert events[0]["seq"] == 1


@pytest.mark.asyncio
async def test_create_without_snapshot_is_allowed(db):
    """A run may be opened before the board is read — hash is then NULL."""
    pid = await _project(db, "wr-no-snap")
    run = await db_module.create_wave_run(db, pid)
    assert run["status"] == "planned"
    assert run["revision_hash"] is None
    assert run["board_snapshot"] is None


@pytest.mark.asyncio
async def test_wave_run_id_is_immutable_across_transitions(db):
    pid = await _project(db, "wr-immutable")
    run = await _run(db, pid)
    original_id = run["id"]

    for target in ("running", "paused", "ready_to_resume", "running"):
        run = await db_module.advance_wave_run_status(db, original_id, target)
        assert run["id"] == original_id

    assert (await db_module.get_wave_run(db, original_id))["id"] == original_id


# ---------------------------------------------------------------------------
# 3-4. State machine
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_transition_advances_and_logs(db):
    pid = await _project(db, "wr-transition-ok")
    run = await _run(db, pid)

    updated = await db_module.advance_wave_run_status(
        db, run["id"], "running", actor="sess-1", detail="dispatched 3 agents",
    )
    assert updated["status"] == "running"

    events = await db_module.get_wave_run_events(db, run["id"])
    assert events[-1]["event_type"] == "status_changed"
    assert events[-1]["from_status"] == "planned"
    assert events[-1]["to_status"] == "running"
    assert events[-1]["detail"] == "dispatched 3 agents"
    assert events[-1]["actor"] == "sess-1"


@pytest.mark.asyncio
async def test_invalid_transition_is_rejected(db):
    pid = await _project(db, "wr-transition-bad")
    run = await _run(db, pid)

    # planned -> paused is not a legal edge: nothing has been dispatched yet.
    with pytest.raises(ValueError, match="Cannot transition"):
        await db_module.advance_wave_run_status(db, run["id"], "paused")

    # Status unchanged and no event written by the rejected attempt.
    assert (await db_module.get_wave_run(db, run["id"]))["status"] == "planned"
    assert len(await db_module.get_wave_run_events(db, run["id"])) == 1


@pytest.mark.asyncio
async def test_unknown_status_is_rejected(db):
    pid = await _project(db, "wr-unknown-status")
    run = await _run(db, pid)
    with pytest.raises(ValueError, match="Invalid wave-run status"):
        await db_module.advance_wave_run_status(db, run["id"], "banana")


@pytest.mark.asyncio
async def test_terminal_statuses_have_no_exit(db):
    pid = await _project(db, "wr-terminal")
    run = await _run(db, pid)
    await db_module.advance_wave_run_status(db, run["id"], "aborted")

    for target in ("running", "paused", "merged", "ready_to_resume"):
        with pytest.raises(ValueError, match="terminal"):
            await db_module.advance_wave_run_status(db, run["id"], target)


@pytest.mark.asyncio
async def test_leaving_halted_state_emits_resumed_event(db):
    pid = await _project(db, "wr-resumed")
    run = await _run(db, pid)
    await db_module.advance_wave_run_status(db, run["id"], "running")
    await db_module.advance_wave_run_status(db, run["id"], "paused")
    await db_module.advance_wave_run_status(db, run["id"], "running")

    events = await db_module.get_wave_run_events(db, run["id"])
    assert events[-1]["event_type"] == "resumed"
    assert events[-1]["from_status"] == "paused"

    # awaiting_human -> ready_to_resume is also a resume.
    await db_module.advance_wave_run_status(db, run["id"], "awaiting_human")
    await db_module.advance_wave_run_status(db, run["id"], "ready_to_resume")
    events = await db_module.get_wave_run_events(db, run["id"])
    assert events[-1]["event_type"] == "resumed"


@pytest.mark.asyncio
async def test_advance_unknown_run_raises(db):
    with pytest.raises(ValueError, match="not found"):
        await db_module.advance_wave_run_status(db, "no-such-run", "running")


# ---------------------------------------------------------------------------
# 5-8. Append-only history and supersede
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_event_seq_is_monotonic(db):
    pid = await _project(db, "wr-seq")
    run = await _run(db, pid)
    for i in range(5):
        await db_module.append_wave_run_event(db, run["id"], f"note-{i}")

    events = await db_module.get_wave_run_events(db, run["id"])
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))


@pytest.mark.asyncio
async def test_supersede_appends_and_leaves_original_body_intact(db):
    pid = await _project(db, "wr-supersede")
    run = await _run(db, pid)
    original = await db_module.append_wave_run_event(
        db, run["id"], "finding", detail="serena unavailable",
        payload={"confidence": "low"},
    )

    correction = await db_module.supersede_wave_run_event(
        db, run["id"], original["id"],
        detail="serena was actually reachable; earlier probe was wrong",
        payload={"confidence": "high"}, actor="sess-2",
    )

    events = await db_module.get_wave_run_events(db, run["id"])
    by_id = {e["id"]: e for e in events}

    old = by_id[original["id"]]
    # The superseded event's BODY is untouched — only the pointer moved.
    assert old["event_type"] == "finding"
    assert old["detail"] == "serena unavailable"
    assert old["payload"] == {"confidence": "low"}
    assert old["superseded_by"] == correction["id"]

    new = by_id[correction["id"]]
    assert new["supersedes"] == original["id"]
    assert new["payload"] == {"confidence": "high"}
    assert new["seq"] > old["seq"]


@pytest.mark.asyncio
async def test_double_supersede_is_refused(db):
    pid = await _project(db, "wr-double-supersede")
    run = await _run(db, pid)
    original = await db_module.append_wave_run_event(db, run["id"], "finding")
    await db_module.supersede_wave_run_event(db, run["id"], original["id"])

    with pytest.raises(ValueError, match="already been superseded"):
        await db_module.supersede_wave_run_event(db, run["id"], original["id"])


@pytest.mark.asyncio
async def test_supersede_unknown_event_raises(db):
    pid = await _project(db, "wr-supersede-missing")
    run = await _run(db, pid)
    with pytest.raises(ValueError, match="not found"):
        await db_module.supersede_wave_run_event(db, run["id"], "no-such-event")


@pytest.mark.asyncio
async def test_include_superseded_false_hides_corrected_events(db):
    pid = await _project(db, "wr-hide-superseded")
    run = await _run(db, pid)
    original = await db_module.append_wave_run_event(db, run["id"], "finding")
    await db_module.supersede_wave_run_event(db, run["id"], original["id"])

    live = await db_module.get_wave_run_events(db, run["id"], include_superseded=False)
    assert original["id"] not in {e["id"] for e in live}
    everything = await db_module.get_wave_run_events(db, run["id"])
    assert original["id"] in {e["id"] for e in everything}


# ---------------------------------------------------------------------------
# 9. Degraded-tool provenance
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_degraded_tool_recorded_and_deduplicated(db):
    pid = await _project(db, "wr-degraded")
    run = await _run(db, pid)

    updated = await db_module.record_degraded_tool(
        db, run["id"], "serena", "code-extractor tunnel inactive",
        fallback="grep + Read",
    )
    assert updated["degraded_tools"] == [
        {"tool": "serena", "reason": "code-extractor tunnel inactive",
         "fallback": "grep + Read"},
    ]
    events_after_first = await db_module.get_wave_run_events(db, run["id"])
    assert events_after_first[-1]["event_type"] == "tool_degraded"

    # Same (tool, reason) again: no duplicate entry AND no duplicate event.
    again = await db_module.record_degraded_tool(
        db, run["id"], "serena", "code-extractor tunnel inactive",
    )
    assert len(again["degraded_tools"]) == 1
    assert len(await db_module.get_wave_run_events(db, run["id"])) == len(
        events_after_first
    )

    # A different reason IS a distinct fact and is recorded.
    third = await db_module.record_degraded_tool(
        db, run["id"], "serena", "index stale",
    )
    assert len(third["degraded_tools"]) == 2


# ---------------------------------------------------------------------------
# 10. Children
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_child_upsert_is_idempotent_and_quiet(db):
    pid = await _project(db, "wr-children")
    run = await _run(db, pid)

    await db_module.record_wave_run_child(
        db, run["id"], "item-1", failure_mode="stop", status="running",
    )
    baseline = len(await db_module.get_wave_run_events(db, run["id"]))

    # Re-recording an unchanged child writes no event (a polling orchestrator
    # must not flood the history).
    await db_module.record_wave_run_child(
        db, run["id"], "item-1", failure_mode="stop", status="running",
    )
    assert len(await db_module.get_wave_run_events(db, run["id"])) == baseline
    assert len(await db_module.get_wave_run_children(db, run["id"])) == 1

    # A real change does emit one event.
    child = await db_module.record_wave_run_child(
        db, run["id"], "item-1", failure_mode="stop", status="succeeded",
        evidence={"commit": "abc123"},
    )
    assert child["status"] == "succeeded"
    assert child["evidence"] == {"commit": "abc123"}
    events = await db_module.get_wave_run_events(db, run["id"])
    assert len(events) == baseline + 1
    assert events[-1]["event_type"] == "child_status_changed"


@pytest.mark.asyncio
async def test_child_validates_inputs(db):
    pid = await _project(db, "wr-child-validate")
    run = await _run(db, pid)

    with pytest.raises(ValueError, match="Invalid failure_mode"):
        await db_module.record_wave_run_child(
            db, run["id"], "item-1", failure_mode="explode",
        )
    with pytest.raises(ValueError, match="Invalid child status"):
        await db_module.record_wave_run_child(
            db, run["id"], "item-1", status="vibes",
        )
    with pytest.raises(ValueError, match="not found"):
        await db_module.record_wave_run_child(db, "no-such-run", "item-1")


# ---------------------------------------------------------------------------
# 11-12. Finalization + idempotency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finalize_happy_path(db):
    pid = await _project(db, "wr-finalize")
    run = await _run(db, pid)
    await db_module.advance_wave_run_status(db, run["id"], "running")
    await db_module.record_wave_run_child(
        db, run["id"], "item-1", failure_mode="stop", status="succeeded",
    )

    result = await db_module.finalize_wave_run(
        db, run["id"], evidence=_GOOD_EVIDENCE, actor="sess-1",
    )
    assert result["finalized"] is True
    assert result["already_finalized"] is False
    assert result["status"] == "merged"
    assert result["finalized_at"] is not None
    assert result["children_summary"]["succeeded"] == 1

    stored = await db_module.get_wave_run(db, run["id"])
    assert stored["status"] == "merged"
    assert stored["finalizer_evidence"] == _GOOD_EVIDENCE

    events = await db_module.get_wave_run_events(db, run["id"])
    assert [e["event_type"] for e in events].count("finalized") == 1


@pytest.mark.asyncio
async def test_finalize_from_ready_to_resume(db):
    """A wave whose children all finished while the orchestrator was away
    finalizes without a pointless round-trip back through 'running'."""
    pid = await _project(db, "wr-finalize-ready")
    run = await _run(db, pid)
    await db_module.advance_wave_run_status(db, run["id"], "running")
    await db_module.advance_wave_run_status(db, run["id"], "paused")
    await db_module.advance_wave_run_status(db, run["id"], "ready_to_resume")

    result = await db_module.finalize_wave_run(db, run["id"], evidence=_GOOD_EVIDENCE)
    assert result["status"] == "merged"


@pytest.mark.asyncio
async def test_finalize_is_idempotent_on_retry(db):
    """The acceptance criterion: retrying finalization duplicates nothing."""
    pid = await _project(db, "wr-idempotent")
    run = await _run(db, pid)
    await db_module.advance_wave_run_status(db, run["id"], "running")

    first = await db_module.finalize_wave_run(db, run["id"], evidence=_GOOD_EVIDENCE)
    events_after_first = await db_module.get_wave_run_events(db, run["id"])

    # Retry — deliberately WITHOUT the evidence, as a caller replaying after a
    # dropped connection would.
    second = await db_module.finalize_wave_run(db, run["id"])

    assert second["finalized"] is True
    assert second["already_finalized"] is True
    assert second["finalized_at"] == first["finalized_at"]
    assert second["finalizer_evidence"] == _GOOD_EVIDENCE
    assert second["event_count"] == first["event_count"] == len(events_after_first)

    # No duplicate event rows, and the run itself is unchanged.
    events_after_second = await db_module.get_wave_run_events(db, run["id"])
    assert [e["id"] for e in events_after_second] == [
        e["id"] for e in events_after_first
    ]
    assert [e["event_type"] for e in events_after_second].count("finalized") == 1


@pytest.mark.asyncio
async def test_finalize_unknown_run_raises(db):
    with pytest.raises(ValueError, match="not found"):
        await db_module.finalize_wave_run(db, "no-such-run", evidence=_GOOD_EVIDENCE)


# ---------------------------------------------------------------------------
# 13. Stop-mode children block finalization
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failed_stop_child_blocks_finalization(db):
    pid = await _project(db, "wr-stop-blocks")
    run = await _run(db, pid)
    await db_module.advance_wave_run_status(db, run["id"], "running")
    await db_module.record_wave_run_child(
        db, run["id"], "item-ok", failure_mode="continue", status="succeeded",
    )
    await db_module.record_wave_run_child(
        db, run["id"], "item-bad", failure_mode="stop", status="failed",
    )

    with pytest.raises(WaveRunFinalizationBlocked) as excinfo:
        await db_module.finalize_wave_run(db, run["id"], evidence=_GOOD_EVIDENCE)

    assert [c["sprint_item_id"] for c in excinfo.value.blocking_children] == ["item-bad"]
    # WaveRunFinalizationBlocked is a ValueError so existing handlers still work.
    assert isinstance(excinfo.value, ValueError)
    # Nothing was merged.
    assert (await db_module.get_wave_run(db, run["id"]))["status"] == "running"


@pytest.mark.asyncio
async def test_failed_continue_child_does_not_block(db):
    pid = await _project(db, "wr-continue-ok")
    run = await _run(db, pid)
    await db_module.advance_wave_run_status(db, run["id"], "running")
    await db_module.record_wave_run_child(
        db, run["id"], "item-bad", failure_mode="continue", status="failed",
    )

    result = await db_module.finalize_wave_run(db, run["id"], evidence=_GOOD_EVIDENCE)
    assert result["status"] == "merged"
    assert result["children_summary"]["failed"] == 1


@pytest.mark.asyncio
async def test_stop_child_unblocks_once_resolved(db):
    pid = await _project(db, "wr-stop-resolved")
    run = await _run(db, pid)
    await db_module.advance_wave_run_status(db, run["id"], "running")
    await db_module.record_wave_run_child(
        db, run["id"], "item-bad", failure_mode="stop", status="failed",
    )
    with pytest.raises(WaveRunFinalizationBlocked):
        await db_module.finalize_wave_run(db, run["id"], evidence=_GOOD_EVIDENCE)

    await db_module.record_wave_run_child(
        db, run["id"], "item-bad", failure_mode="stop", status="succeeded",
    )
    result = await db_module.finalize_wave_run(db, run["id"], evidence=_GOOD_EVIDENCE)
    assert result["status"] == "merged"


# ---------------------------------------------------------------------------
# 14. Evidence contract (shared with complete_wave_gate, d2430713)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("evidence,expected", [
    (None, "requires an evidence dict"),
    (True, "requires an evidence dict"),
    ("all good", "requires an evidence dict"),
    ({"status": "not_configured"}, "not_configured"),
    ({"status": "not_connected"}, "not_connected"),
    ({"status": "error", "exit_code": 1}, "status='error'"),
    ({"status": "self_reported", "exit_code": 0}, "must be 'ok'"),
    ({"status": "ok", "exit_code": 1, "failed": 3}, "exit_code must be 0"),
])
async def test_finalize_rejects_bad_evidence(db, evidence, expected):
    pid = await _project(db, f"wr-evidence-{abs(hash(expected)) % 10000}")
    run = await _run(db, pid)
    await db_module.advance_wave_run_status(db, run["id"], "running")

    with pytest.raises(ValueError) as excinfo:
        await db_module.finalize_wave_run(db, run["id"], evidence=evidence)
    assert expected in str(excinfo.value)
    assert (await db_module.get_wave_run(db, run["id"]))["status"] == "running"


# ---------------------------------------------------------------------------
# 15-16. Stale manifest / aborted run
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_revision_hash_fails_closed(db):
    pid = await _project(db, "wr-stale")
    run = await _run(db, pid)
    await db_module.advance_wave_run_status(db, run["id"], "running")

    with pytest.raises(ValueError, match="stale board manifest"):
        await db_module.finalize_wave_run(
            db, run["id"], evidence=_GOOD_EVIDENCE,
            expected_revision_hash="sha256:something-else",
        )
    assert (await db_module.get_wave_run(db, run["id"]))["status"] == "running"

    # The matching hash goes through.
    result = await db_module.finalize_wave_run(
        db, run["id"], evidence=_GOOD_EVIDENCE,
        expected_revision_hash=run["revision_hash"],
    )
    assert result["status"] == "merged"


@pytest.mark.asyncio
async def test_aborted_run_cannot_be_finalized(db):
    pid = await _project(db, "wr-aborted")
    run = await _run(db, pid)
    await db_module.advance_wave_run_status(db, run["id"], "aborted")

    with pytest.raises(ValueError, match="aborted and cannot be finalized"):
        await db_module.finalize_wave_run(db, run["id"], evidence=_GOOD_EVIDENCE)


@pytest.mark.asyncio
async def test_list_wave_runs_filters(db):
    pid = await _project(db, "wr-list")
    a = await _run(db, pid, version="v1")
    b = await _run(db, pid, version="v2")
    await db_module.advance_wave_run_status(db, b["id"], "running")

    assert {r["id"] for r in await db_module.list_wave_runs(db, pid)} == {a["id"], b["id"]}
    running = await db_module.list_wave_runs(db, pid, status="running")
    assert [r["id"] for r in running] == [b["id"]]
    v1 = await db_module.list_wave_runs(db, pid, version="v1")
    assert [r["id"] for r in v1] == [a["id"]]


# ---------------------------------------------------------------------------
# 17. Migrations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migration_creates_all_three_tables(db):
    for table in ("wave_runs", "wave_run_events", "wave_run_children"):
        async with db.execute(f"SELECT COUNT(*) FROM {table}") as cur:
            row = await cur.fetchone()
        assert row is not None, f"{table} was not created by init_db"


def test_pg_migration_registered_in_late_registry():
    """_migrate_pg_wave_runs MUST be in the LATE registry — it runs on every DB,
    self-hosted and hosted alike, not just hosted tenants."""
    from meridian import pg_adapter as pg_module

    late = [f.__name__ for f in pg_module._PG_MIGRATIONS_LATE]
    assert "_migrate_pg_wave_runs" in late


def test_sqlite_migration_exported():
    """The star-import in db/__init__ only picks up names listed in __all__."""
    from meridian.db import migrations as migrations_module

    assert "_migrate_wave_runs" in migrations_module.__all__
    assert hasattr(db_module, "_migrate_wave_runs")


# ---------------------------------------------------------------------------
# 18. MCP registration + dispatch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_name", ["start_wave_run", "finalize_wave_run"])
def test_tool_registered_with_full_metadata(tool_name):
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    assert tool_name in by_name, f"{tool_name} missing from _MCP_TOOLS_LIST"

    tool = by_name[tool_name]
    assert tool["description"]
    assert tool["inputSchema"]["type"] == "object"
    assert tool_name in _TOOL_EXAMPLES
    assert f"{tool_name}(" in _TOOL_EXAMPLES[tool_name]
    assert _TOOL_CATEGORY.get(tool_name) == "sprint-management"
    assert _TOOL_ROLE_RELEVANCE.get(tool_name) == "executor"


def test_finalize_requires_wave_run_id_in_schema():
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    schema = by_name["finalize_wave_run"]["inputSchema"]
    assert schema["required"] == ["wave_run_id"]
    assert "expected_revision_hash" in schema["properties"]


@pytest.mark.asyncio
async def test_mcp_start_then_finalize_round_trip(db):
    pid = await _project(db, "wr-mcp-roundtrip")
    started = await srv._dispatch_mcp_tool(
        "start_wave_run",
        {
            "project_id": pid,
            "version": "v0.2.5",
            "wave_label": "wave-2",
            "item_ids": ["item-a", "item-b"],
            "failure_modes": {"item-a": "stop"},
            "degraded_tools": [
                {"tool": "serena", "reason": "tunnel inactive", "fallback": "grep"},
            ],
            "actor": "sess-mcp",
        },
        db, "/tmp",
    )
    assert "error" not in started
    wave_run_id = started["wave_run_id"]
    assert started["revision_hash"]
    assert {c["sprint_item_id"] for c in started["children"]} == {"item-a", "item-b"}
    modes = {c["sprint_item_id"]: c["failure_mode"] for c in started["children"]}
    assert modes["item-a"] == "stop"
    assert modes["item-b"] == "continue"
    assert started["run"]["degraded_tools"][0]["tool"] == "serena"

    await db_module.advance_wave_run_status(db, wave_run_id, "running")

    # A failed stop child blocks via the MCP surface too, with named ids.
    await db_module.record_wave_run_child(
        db, wave_run_id, "item-a", failure_mode="stop", status="failed",
    )
    blocked = await srv._dispatch_mcp_tool(
        "finalize_wave_run",
        {"wave_run_id": wave_run_id, "evidence": _GOOD_EVIDENCE},
        db, "/tmp",
    )
    assert blocked["finalized"] is False
    assert [b["sprint_item_id"] for b in blocked["blocked_by"]] == ["item-a"]

    # Resolve it and finalize for real, then replay.
    await db_module.record_wave_run_child(
        db, wave_run_id, "item-a", failure_mode="stop", status="succeeded",
    )
    ok = await srv._dispatch_mcp_tool(
        "finalize_wave_run",
        {"wave_run_id": wave_run_id, "evidence": _GOOD_EVIDENCE},
        db, "/tmp",
    )
    assert ok["already_finalized"] is False

    replay = await srv._dispatch_mcp_tool(
        "finalize_wave_run", {"wave_run_id": wave_run_id}, db, "/tmp",
    )
    assert replay["already_finalized"] is True
    assert replay["event_count"] == ok["event_count"]


@pytest.mark.asyncio
async def test_mcp_finalize_requires_wave_run_id(db):
    result = await srv._dispatch_mcp_tool("finalize_wave_run", {}, db, "/tmp")
    assert "wave_run_id" in result.get("error", "")


@pytest.mark.asyncio
async def test_mcp_start_requires_project(db):
    result = await srv._dispatch_mcp_tool("start_wave_run", {}, db, "/tmp")
    assert "project_id" in result.get("error", "")


# ---------------------------------------------------------------------------
# dcfbe55c — macro-wave projection: separate the visible macro-wave CAP from
# the conflict-safe internal claim batches get_parallelizable_groups computes.
#
# This is a distinct feature from the durable wave-run state machine covered
# above (sharing the "wave" name only) -- it's a deterministic, PRESENTATION-
# ONLY packing of get_parallelizable_groups' conflict-free "groups" into at
# most N (default 3, range 1-3) display waves, so a human/executor reading a
# /goal isn't confronted with 8-10 confusing "batch" entries when the live
# board happens to color that many genuinely-parallel-safe groups. It never
# changes which items are safe to run together -- claim_sprint_item's real
# resource-lock enforcement (unmodified here) remains the actual safety
# mechanism regardless of how this projection displays things.
#
# Coverage:
#   1. pack_groups_into_macro_waves: empty input, no-op when within cap,
#      contiguous-chunk compression when over cap, clamping, and the
#      never-merge-items-within-a-batch safety invariant.
#   2. _clamp_macro_wave_count direct unit coverage.
#   3. get_parallelizable_groups: default cap is 3, cap is configurable and
#      clamped end-to-end, and "groups" (the authoritative conflict-free
#      partition) is byte-for-byte identical regardless of the requested cap.
#   4. handoff._build_quick_start_goal: macro-wave framing renders only when
#      it genuinely compresses the display; a hand-built dict with no
#      "macro_waves" key (legacy callers, e.g. test_core.py's existing
#      coverage) or one that doesn't compress falls straight through to the
#      original flat per-batch rendering, unchanged; leftover/blocked-item
#      handling is preserved under the new framing.
#   5. handoff._requested_macro_wave_count_from_settings: default + clamping.
# ---------------------------------------------------------------------------

# --- 1. pack_groups_into_macro_waves -----------------------------------------

def test_pack_groups_into_macro_waves_empty_input():
    assert db_module.sprint_items.pack_groups_into_macro_waves([]) == []
    assert db_module.sprint_items.pack_groups_into_macro_waves([], 2) == []


def test_pack_groups_into_macro_waves_noop_when_within_cap():
    """len(groups) <= cap: every group already gets its own macro wave — the
    common small-board case, and a byte-for-byte no-op projection."""
    groups = [[{"id": "a"}], [{"id": "b"}, {"id": "c"}]]
    waves = db_module.sprint_items.pack_groups_into_macro_waves(groups, 3)
    assert len(waves) == 2
    assert waves[0]["batches"] == [groups[0]]
    assert waves[1]["batches"] == [groups[1]]
    assert waves[0]["item_count"] == 1
    assert waves[1]["item_count"] == 2
    assert waves[0]["batch_count"] == 1 and waves[1]["batch_count"] == 1


def test_pack_groups_into_macro_waves_compresses_when_over_cap():
    """7 conflict-free groups packed into 3 macro waves via CONTIGUOUS
    chunking: sizes [3, 2, 2] (divmod(7, 3) = (2, 1), the first `extra`
    waves get one more group). Order is preserved end to end."""
    groups = [[{"id": f"g{i}"}] for i in range(7)]
    waves = db_module.sprint_items.pack_groups_into_macro_waves(groups, 3)
    assert len(waves) == 3
    assert [w["batch_count"] for w in waves] == [3, 2, 2]
    assert [w["item_count"] for w in waves] == [3, 2, 2]
    # Concatenating each wave's batches in order reproduces `groups` exactly.
    flattened = [b for w in waves for b in w["batches"]]
    assert flattened == groups


def test_pack_groups_into_macro_waves_never_merges_groups_together():
    """Claim-safety invariant: packing NEVER merges two distinct
    conflict-free groups' items into a single flat batch — each original
    group survives, unmerged, as its own entry in some wave's "batches"."""
    groups = [[{"id": "a"}, {"id": "b"}], [{"id": "c"}]]
    waves = db_module.sprint_items.pack_groups_into_macro_waves(groups, 1)
    assert len(waves) == 1
    assert waves[0]["batches"] == groups  # both groups preserved, unmerged
    assert waves[0]["batch_count"] == 2
    assert waves[0]["item_count"] == 3


@pytest.mark.parametrize(
    "requested,expected_wave_count",
    [(0, 1), (-5, 1), (1, 1), (2, 2), (3, 3), (4, 3), (100, 3), (None, 3), ("bogus", 3)],
)
def test_pack_groups_into_macro_waves_clamps_requested_count(requested, expected_wave_count):
    groups = [[{"id": f"g{i}"}] for i in range(6)]
    waves = db_module.sprint_items.pack_groups_into_macro_waves(groups, requested)
    assert len(waves) == expected_wave_count
    assert sum(w["item_count"] for w in waves) == 6


# --- 2. _clamp_macro_wave_count -----------------------------------------

def test_clamp_macro_wave_count_direct():
    clamp = db_module.sprint_items._clamp_macro_wave_count
    assert clamp(None) == 3
    assert clamp(3) == 3
    assert clamp(1) == 1
    assert clamp(0) == 1
    assert clamp(-1) == 1
    assert clamp(4) == 3
    assert clamp(100) == 3
    assert clamp("2") == 2
    assert clamp("bogus") == 3


# --- 3. get_parallelizable_groups end-to-end (real DB) -----------------------

@pytest.mark.asyncio
async def test_get_parallelizable_groups_default_macro_wave_cap_is_three(db):
    pid = await _project(db, "mw-default")
    # Same shared resource on every item forces one singleton group per item
    # (they all pairwise conflict) — a simple, deterministic way to produce
    # many groups for a macro-wave packing test.
    for i in range(5):
        await db_module.add_sprint_item(
            db, pid, "v1", f"FEAT: item {i}", touches_resources=["file:shared.py"],
            force=True,  # titles differ only by number -- bypass dup detection
        )
    res = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert res["group_count"] == 5
    assert res["requested_macro_wave_count"] == 3
    assert res["macro_wave_count"] == 3
    assert sum(w["item_count"] for w in res["macro_waves"]) == 5
    # The authoritative conflict-free partition is untouched by the cap.
    assert len(res["groups"]) == res["group_count"] == 5


@pytest.mark.asyncio
async def test_get_parallelizable_groups_macro_wave_cap_is_configurable(db):
    pid = await _project(db, "mw-configurable")
    for i in range(6):
        await db_module.add_sprint_item(
            db, pid, "v1", f"FEAT: item {i}", touches_resources=["file:shared.py"],
            force=True,  # titles differ only by number -- bypass dup detection
        )
    res1 = await db_module.get_parallelizable_groups(
        db, pid, version="v1", requested_macro_wave_count=1,
    )
    assert res1["requested_macro_wave_count"] == 1
    assert res1["macro_wave_count"] == 1
    assert res1["macro_waves"][0]["batch_count"] == 6

    res2 = await db_module.get_parallelizable_groups(
        db, pid, version="v1", requested_macro_wave_count=10,
    )
    # Out-of-range request clamps to the [1, 3] ceiling.
    assert res2["requested_macro_wave_count"] == 3
    assert res2["macro_wave_count"] == 3

    # dcfbe55c's core contract: the underlying claim-safety partition
    # ("groups"/"group_count"/"blocked") is IDENTICAL regardless of the
    # macro-wave cap — this is a presentation layer only, never a
    # claim-safety waiver.
    assert res1["groups"] == res2["groups"]
    assert res1["group_count"] == res2["group_count"] == 6
    assert res1["blocked"] == res2["blocked"]


@pytest.mark.asyncio
async def test_get_parallelizable_groups_macro_waves_empty_when_no_groups(db):
    pid = await _project(db, "mw-empty")
    res = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert res["groups"] == []
    assert res["macro_waves"] == []
    assert res["macro_wave_count"] == 0
    assert res["requested_macro_wave_count"] == 3  # default still reported


# --- 4. handoff._build_quick_start_goal macro-wave framing -------------------

def test_build_quick_start_goal_macro_wave_framing_when_compressed():
    """Compressed macro-waves preserve the full ID set.

    Each ID intentionally appears once in the authoritative executor manifest
    and once in the human-readable batch prose.
    """
    items = [{"id": f"i{n}", "version": None} for n in range(6)]
    groups = [
        [{"id": "i0"}, {"id": "i1"}], [{"id": "i2"}], [{"id": "i3"}],
        [{"id": "i4"}], [{"id": "i5"}],
    ]
    macro_waves = [
        {"batches": groups[0:2], "batch_count": 2, "item_count": 3},
        {"batches": groups[2:4], "batch_count": 2, "item_count": 2},
        {"batches": groups[4:5], "batch_count": 1, "item_count": 1},
    ]
    parallel_groups = {
        "group_count": 5, "groups": groups, "macro_waves": macro_waves,
        "requested_macro_wave_count": 3, "macro_wave_count": 3, "blocked": [],
    }
    goal = h._build_quick_start_goal(items, parallel_groups=parallel_groups)
    assert "macro-wave" in goal
    assert "presentation only" in goal
    assert "Wave 1 [batch 1: i0, i1; batch 2: i2]" in goal
    assert "Wave 2 [batch 3: i3; batch 4: i4]" in goal
    assert "Wave 3 [batch 5: i5]" in goal
    for iid in ("i0", "i1", "i2", "i3", "i4", "i5"):
        assert goal.count(iid) == 2  # manifest + presentation prose


def test_build_quick_start_goal_falls_back_to_flat_when_no_compression():
    """macro_waves present but len(macro_waves) == len(groups) (nothing to
    compress, e.g. a small board under the cap) must render IDENTICALLY to
    the pre-existing flat "batch N: ..." format — no "Wave" framing at all."""
    items = [{"id": "a1", "version": None}, {"id": "b2", "version": None}, {"id": "c3", "version": None}]
    groups = [[{"id": "a1"}, {"id": "b2"}], [{"id": "c3"}]]
    macro_waves = [
        {"batches": [groups[0]], "batch_count": 1, "item_count": 2},
        {"batches": [groups[1]], "batch_count": 1, "item_count": 1},
    ]
    parallel_groups = {
        "group_count": 2, "groups": groups, "macro_waves": macro_waves,
        "requested_macro_wave_count": 3, "macro_wave_count": 2, "blocked": [],
    }
    goal = h._build_quick_start_goal(items, parallel_groups=parallel_groups)
    assert "Wave 1" not in goal
    assert "macro-wave" not in goal
    assert "resource-conflict-free batches" in goal
    assert "batch 1: a1, b2" in goal
    assert "batch 2: c3" in goal


def test_build_quick_start_goal_legacy_dict_without_macro_waves_key_unaffected():
    """No "macro_waves" key at all (e.g. a caller/test built before this
    feature existed, like test_core.py's pre-existing coverage) must behave
    exactly as before — the flat rendering, with no crash and no "Wave"
    framing. Two groups (one with >1 item) so the batches path genuinely
    engages."""
    items = [{"id": "a1", "version": None}, {"id": "b2", "version": None}, {"id": "c3", "version": None}]
    parallel_groups = {
        "group_count": 2,
        "groups": [[{"id": "a1"}, {"id": "b2"}], [{"id": "c3"}]],
        "blocked": [],
    }
    goal = h._build_quick_start_goal(items, parallel_groups=parallel_groups)
    assert "Wave 1" not in goal
    assert "macro-wave" not in goal
    assert "batch 1: a1, b2" in goal
    assert "batch 2: c3" in goal


def test_build_quick_start_goal_macro_wave_framing_preserves_leftover_handling():
    """The leftover/blocked-item cross-reference logic (a1996fbf) must keep
    working identically whether or not macro-wave framing is engaged.

    83a7586d — 'blocked1' declares a blocker (``zzz9999``) that is NOT part
    of this goal's own item list at all — exactly the "truly external"
    dependency case the new fan-out/fan-in frontier gate hard-excludes
    (structured ``<excluded_dependency_not_satisfied>`` tag) BEFORE the
    macro-wave/leftover machinery below ever runs, superseding the older
    inline "blocked1 blocked on zzz9999" free-text framing a1996fbf added
    for this exact scenario (still-relevant for an item kept because its
    blocker IS in this same batch — see the fan-in dependency-closure
    coverage in test_83a7586d_dependency_frontier.py). Wave/batch framing
    itself is unaffected — that's what this test still exists to pin.
    """
    items = [
        {"id": "blocked1", "version": None},
        {"id": "a1", "version": None}, {"id": "b2", "version": None},
        {"id": "c3", "version": None}, {"id": "d4", "version": None},
        {"id": "e5", "version": None},
    ]
    groups = [
        [{"id": "a1"}, {"id": "b2"}], [{"id": "c3"}], [{"id": "d4"}], [{"id": "e5"}],
    ]
    macro_waves = [
        {"batches": groups[0:2], "batch_count": 2, "item_count": 3},
        {"batches": groups[2:4], "batch_count": 2, "item_count": 2},
    ]
    parallel_groups = {
        "group_count": 4, "groups": groups, "macro_waves": macro_waves,
        "requested_macro_wave_count": 2, "macro_wave_count": 2,
        "blocked": [
            {"id": "blocked1", "title": "x", "depends_on": "zzz9999", "blocked_by_status": "pending"},
        ],
    }
    goal = h._build_quick_start_goal(items, parallel_groups=parallel_groups)
    assert "Wave 1 [batch 1: a1, b2; batch 2: c3]" in goal
    assert "Wave 2 [batch 3: d4; batch 4: e5]" in goal
    # 83a7586d — structured exclusion, not the old inline "blocked on" prose:
    # blocked1 is named in the machine-readable tag (never silently dropped)
    # but the two Wave/batch strings above (the claimable listing) already
    # prove it's absent from there.
    assert '<excluded_dependency_not_satisfied count="1">blocked1</excluded_dependency_not_satisfied>' in goal


@pytest.mark.asyncio
async def test_build_quick_start_goal_end_to_end_from_real_db_macro_waves(db):
    """The real macro-wave pipeline preserves every ID in the manifest."""
    pid = await _project(db, "mw-e2e")
    # 6 items share one resource (6 singleton groups); one more item touches
    # a disjoint resource and first-fits into group 0, giving it 2 items so
    # _has_parallel's "at least one genuinely-parallel group" gate is met.
    ids = []
    for i in range(6):
        it = await db_module.add_sprint_item(
            db, pid, "v1", f"FEAT: shared {i}", touches_resources=["file:shared.py"],
            force=True,  # titles differ only by number -- bypass dup detection
        )
        ids.append(it["id"])
    extra = await db_module.add_sprint_item(
        db, pid, "v1", "FEAT: unique", touches_resources=["file:unique.py"],
    )
    ids.append(extra["id"])

    res = await db_module.get_parallelizable_groups(
        db, pid, version="v1", requested_macro_wave_count=3,
    )
    assert res["group_count"] == 6
    assert res["macro_wave_count"] == 3
    assert any(len(g) > 1 for g in res["groups"])  # the fan-out condition

    pending_items = [{"id": iid, "version": "v1"} for iid in ids]
    goal = h._build_quick_start_goal(
        pending_items, version="v1", parallel_groups=res,
    )
    assert "macro-wave" in goal
    assert "Wave 1" in goal
    for iid in ids:
        assert goal.count(iid) == 2  # manifest + presentation prose


# --- 5. _requested_macro_wave_count_from_settings ----------------------------

def test_requested_macro_wave_count_from_settings_default():
    assert h._requested_macro_wave_count_from_settings(None) == 3
    assert h._requested_macro_wave_count_from_settings({}) == 3
    assert h._requested_macro_wave_count_from_settings({"executor_config": {}}) == 3
    assert h._requested_macro_wave_count_from_settings({"executor_config": None}) == 3


@pytest.mark.parametrize(
    "raw,expected",
    [
        (1, 1), (2, 2), (3, 3),
        (0, 1), (-4, 1),
        (4, 3), (99, 3),
        ("2", 2), ("bogus", 3),
        (None, 3),
    ],
)
def test_requested_macro_wave_count_from_settings_clamps(raw, expected):
    settings = {"executor_config": {"requested_macro_wave_count": raw}}
    assert h._requested_macro_wave_count_from_settings(settings) == expected
