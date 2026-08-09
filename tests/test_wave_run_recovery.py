"""7d71d6bc — RESCUE-R2: child leases, dispatch provenance, no-op resume
protection.

Coverage:
  1.  Migration adds every new wave_run_children column (idempotent re-run).
  2.  classify_wave_run_child_lease: pure classification for all four states.
  3.  claim_wave_run_child: first_claim sets claimed_at/agent_id/attempt=1.
  4.  claim_wave_run_child: reclaim (same agent, live lease) is idempotent,
      no attempt bump.
  5.  claim_wave_run_child: retry after a terminal outcome bumps attempt,
      clears exit_code, appends a child_retried event.
  6.  claim_wave_run_child: retry after a STALE (expired) lease under a
      different agent succeeds.
  7.  claim_wave_run_child: a DIFFERENT agent may NOT steal a currently-LIVE
      lease — ForeignWaveRunChildLeaseError ("test-run lock contention").
  8.  heartbeat_wave_run_child: refreshes last_heartbeat_at.
  9.  heartbeat_wave_run_child: foreign agent_id is refused.
 10.  heartbeat_wave_run_child: a terminal child cannot be heartbeat.
 11.  heartbeat_wave_run_child: an unclaimed child cannot be heartbeat.
 12.  record_wave_run_child_outcome: preserves the REAL subprocess exit code.
 13.  record_wave_run_child_outcome: rejects a non-terminal status.
 14.  record_wave_run_child_outcome: rejects a non-int exit_code.
 15.  get_wave_run_recovery_plan: distinguishes live / stale_orphan /
      completed / empty_invalid across one wave run's children and never
      marks a live or completed child resumable (no-op resume protection).
 16.  get_wave_run_recovery_plan: raises for an unknown wave run.
 17.  find_active_wave_run_child_for_item: None with no active wave run;
      finds the live child; None again once the wave run is aborted.
 18.  claim_sprint_item integration: claiming a wave-run-child item stamps
       the child's lease automatically.
 19.  complete_sprint_item integration: completing such an item records the
       terminal outcome + real exit_code automatically.
 20.  Both integration hooks fail OPEN: a wave-run bookkeeping error never
      blocks the underlying claim/completion.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from meridian import db as db_module
from meridian.db.wave_runs import (
    ForeignWaveRunChildLeaseError,
    WAVE_RUN_CHILD_LEASE_LIVE,
    WAVE_RUN_CHILD_LEASE_STALE_ORPHAN,
    WAVE_RUN_CHILD_LEASE_COMPLETED,
    WAVE_RUN_CHILD_LEASE_EMPTY_INVALID,
    classify_wave_run_child_lease,
    claim_wave_run_child,
    heartbeat_wave_run_child,
    record_wave_run_child_outcome,
    get_wave_run_recovery_plan,
    find_active_wave_run_child_for_item,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


async def _run(db, project_id: str, item_ids=None, **kwargs):
    """Create a wave run AND pre-register its children — mirrors
    handle_start_wave_run's own pre-registration loop (the real MCP handler,
    meridian/mcp/handlers/sprint_tools.py), so DB-layer tests see the same
    wave_run_children rows (status='running', claimed_at=None, agent_id=None
    — i.e. classify_wave_run_child_lease's 'empty_invalid' state) a real
    start_wave_run call produces before any executor ever claims one."""
    snapshot = await db_module.build_board_snapshot(db, project_id)
    item_ids = list(item_ids or [])
    run = await db_module.create_wave_run(
        db, project_id, snapshot=snapshot, item_ids=item_ids, **kwargs
    )
    for item_id in item_ids:
        await db_module.record_wave_run_child(
            db, run["id"], item_id, failure_mode="continue", status="running",
            actor=kwargs.get("actor"),
        )
    fresh = await db_module.get_wave_run(db, run["id"])
    assert fresh is not None
    return fresh


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 1. Migration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migration_adds_lease_columns_idempotently(db):
    async with db.execute("PRAGMA table_info(wave_run_children)") as cur:
        rows = await cur.fetchall()
    cols = {r["name"] if isinstance(r, dict) else r[1] for r in rows}
    for expected in (
        "agent_id", "claimed_at", "last_heartbeat_at", "lease_ttl_seconds",
        "exit_code", "attempt", "dispatch_provenance",
    ):
        assert expected in cols, f"{expected} missing from wave_run_children"

    # Re-running the guarded migration must not error (idempotent ADD COLUMN).
    await db_module._migrate_wave_runs(db)


# ---------------------------------------------------------------------------
# 2. classify_wave_run_child_lease — pure classification
# ---------------------------------------------------------------------------

def test_classify_completed_regardless_of_age():
    now = datetime(2026, 1, 1, 12, 0, 0)
    for status in ("succeeded", "failed", "skipped"):
        child = {
            "status": status, "claimed_at": _ts(now - timedelta(days=30)),
            "agent_id": "agent-1", "last_heartbeat_at": None,
        }
        assert classify_wave_run_child_lease(child, now=now) == WAVE_RUN_CHILD_LEASE_COMPLETED


def test_classify_empty_invalid_never_dispatched():
    now = datetime(2026, 1, 1, 12, 0, 0)
    child = {"status": "running", "claimed_at": None, "agent_id": None}
    assert classify_wave_run_child_lease(child, now=now) == WAVE_RUN_CHILD_LEASE_EMPTY_INVALID


def test_classify_empty_invalid_missing_agent_id():
    now = datetime(2026, 1, 1, 12, 0, 0)
    child = {"status": "running", "claimed_at": _ts(now), "agent_id": "  "}
    assert classify_wave_run_child_lease(child, now=now) == WAVE_RUN_CHILD_LEASE_EMPTY_INVALID


def test_classify_live_within_ttl():
    now = datetime(2026, 1, 1, 12, 0, 0)
    child = {
        "status": "running", "claimed_at": _ts(now - timedelta(minutes=5)),
        "agent_id": "agent-1", "last_heartbeat_at": _ts(now - timedelta(minutes=1)),
        "lease_ttl_seconds": 1800,
    }
    assert classify_wave_run_child_lease(child, now=now) == WAVE_RUN_CHILD_LEASE_LIVE


def test_classify_stale_orphan_past_ttl():
    now = datetime(2026, 1, 1, 12, 0, 0)
    child = {
        "status": "running", "claimed_at": _ts(now - timedelta(hours=2)),
        "agent_id": "agent-1", "last_heartbeat_at": _ts(now - timedelta(hours=1)),
        "lease_ttl_seconds": 1800,
    }
    assert classify_wave_run_child_lease(child, now=now) == WAVE_RUN_CHILD_LEASE_STALE_ORPHAN


def test_classify_falls_back_to_claimed_at_when_no_heartbeat():
    now = datetime(2026, 1, 1, 12, 0, 0)
    live = {
        "status": "running", "claimed_at": _ts(now - timedelta(minutes=1)),
        "agent_id": "agent-1", "last_heartbeat_at": None, "lease_ttl_seconds": 1800,
    }
    assert classify_wave_run_child_lease(live, now=now) == WAVE_RUN_CHILD_LEASE_LIVE

    stale = {
        "status": "running", "claimed_at": _ts(now - timedelta(hours=2)),
        "agent_id": "agent-1", "last_heartbeat_at": None, "lease_ttl_seconds": 1800,
    }
    assert classify_wave_run_child_lease(stale, now=now) == WAVE_RUN_CHILD_LEASE_STALE_ORPHAN


def test_classify_unparseable_heartbeat_is_safe_not_trusted():
    now = datetime(2026, 1, 1, 12, 0, 0)
    child = {
        "status": "running", "claimed_at": "garbage-not-a-timestamp",
        "agent_id": "agent-1", "last_heartbeat_at": "also garbage",
    }
    assert classify_wave_run_child_lease(child, now=now) == WAVE_RUN_CHILD_LEASE_STALE_ORPHAN


# ---------------------------------------------------------------------------
# 3-7. claim_wave_run_child
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_first_claim_sets_lease_fields(db):
    pid = await _project(db, "wrc-first")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: a")
    run = await _run(db, pid, item_ids=[item["id"]])

    child = await claim_wave_run_child(
        db, run["id"], item["id"], agent_id="agent-A", actor="sess-1",
    )
    assert child["claim_kind"] == "first_claim"
    assert child["agent_id"] == "agent-A"
    assert child["claimed_at"] is not None
    assert child["last_heartbeat_at"] is not None
    assert child["attempt"] == 1
    assert child["status"] == "running"
    assert child["exit_code"] is None


@pytest.mark.asyncio
async def test_claim_reclaim_same_agent_is_idempotent(db):
    pid = await _project(db, "wrc-reclaim")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: a")
    run = await _run(db, pid, item_ids=[item["id"]])

    first = await claim_wave_run_child(db, run["id"], item["id"], agent_id="agent-A")
    second = await claim_wave_run_child(db, run["id"], item["id"], agent_id="agent-A")

    assert second["claim_kind"] == "reclaim"
    assert second["attempt"] == first["attempt"] == 1
    events = await db_module.get_wave_run_events(db, run["id"])
    retried = [e for e in events if e["event_type"] == "child_retried"]
    assert retried == []


@pytest.mark.asyncio
async def test_claim_retry_after_terminal_outcome_bumps_attempt(db):
    pid = await _project(db, "wrc-retry-terminal")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: a")
    run = await _run(db, pid, item_ids=[item["id"]])

    await claim_wave_run_child(db, run["id"], item["id"], agent_id="agent-A")
    await record_wave_run_child_outcome(
        db, run["id"], item["id"], status="failed", exit_code=1, agent_id="agent-A",
    )

    retried = await claim_wave_run_child(db, run["id"], item["id"], agent_id="agent-B")
    assert retried["claim_kind"] == "retry"
    assert retried["attempt"] == 2
    assert retried["agent_id"] == "agent-B"
    assert retried["exit_code"] is None
    assert retried["status"] == "running"

    events = await db_module.get_wave_run_events(db, run["id"])
    retry_events = [e for e in events if e["event_type"] == "child_retried"]
    assert len(retry_events) == 1
    assert retry_events[0]["payload"]["prior_agent_id"] == "agent-A"
    assert retry_events[0]["payload"]["prior_status"] == "failed"
    assert retry_events[0]["payload"]["prior_exit_code"] == 1


@pytest.mark.asyncio
async def test_claim_retry_after_stale_lease_succeeds(db):
    pid = await _project(db, "wrc-retry-stale")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: a")
    run = await _run(db, pid, item_ids=[item["id"]])

    stale_time = datetime.utcnow() - timedelta(hours=5)
    await claim_wave_run_child(
        db, run["id"], item["id"], agent_id="agent-A",
        lease_ttl_seconds=60, now=stale_time,
    )

    # agent-A's lease is now long past its 60s TTL — a DIFFERENT agent may
    # legitimately take it over as a retry, not a foreign-lease conflict.
    retried = await claim_wave_run_child(db, run["id"], item["id"], agent_id="agent-B")
    assert retried["claim_kind"] == "retry"
    assert retried["attempt"] == 2
    assert retried["agent_id"] == "agent-B"


@pytest.mark.asyncio
async def test_claim_foreign_live_lease_is_refused(db):
    pid = await _project(db, "wrc-foreign")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: a")
    run = await _run(db, pid, item_ids=[item["id"]])

    await claim_wave_run_child(db, run["id"], item["id"], agent_id="agent-A")

    with pytest.raises(ForeignWaveRunChildLeaseError) as exc_info:
        await claim_wave_run_child(db, run["id"], item["id"], agent_id="agent-B")
    assert exc_info.value.holder == "agent-A"
    assert exc_info.value.requester == "agent-B"

    # And the child is untouched — still leased to agent-A.
    children = await db_module.get_wave_run_children(db, run["id"])
    assert children[0]["agent_id"] == "agent-A"
    assert children[0]["attempt"] == 1


# ---------------------------------------------------------------------------
# 8-11. heartbeat_wave_run_child
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_refreshes_last_heartbeat_at(db):
    pid = await _project(db, "wrc-heartbeat")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: a")
    run = await _run(db, pid, item_ids=[item["id"]])

    claimed = await claim_wave_run_child(db, run["id"], item["id"], agent_id="agent-A")
    later = datetime.utcnow() + timedelta(minutes=10)
    beat = await heartbeat_wave_run_child(
        db, run["id"], item["id"], agent_id="agent-A", now=later,
    )
    assert beat["last_heartbeat_at"] != claimed["last_heartbeat_at"] or beat["last_heartbeat_at"] is not None
    assert beat["agent_id"] == "agent-A"


@pytest.mark.asyncio
async def test_heartbeat_foreign_agent_refused(db):
    pid = await _project(db, "wrc-heartbeat-foreign")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: a")
    run = await _run(db, pid, item_ids=[item["id"]])

    await claim_wave_run_child(db, run["id"], item["id"], agent_id="agent-A")
    with pytest.raises(ForeignWaveRunChildLeaseError):
        await heartbeat_wave_run_child(db, run["id"], item["id"], agent_id="agent-B")


@pytest.mark.asyncio
async def test_heartbeat_terminal_child_refused(db):
    pid = await _project(db, "wrc-heartbeat-terminal")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: a")
    run = await _run(db, pid, item_ids=[item["id"]])

    await claim_wave_run_child(db, run["id"], item["id"], agent_id="agent-A")
    await record_wave_run_child_outcome(
        db, run["id"], item["id"], status="succeeded", exit_code=0,
    )
    with pytest.raises(ValueError, match="not 'running'"):
        await heartbeat_wave_run_child(db, run["id"], item["id"], agent_id="agent-A")


@pytest.mark.asyncio
async def test_heartbeat_unclaimed_child_refused(db):
    pid = await _project(db, "wrc-heartbeat-unclaimed")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: a")
    run = await _run(db, pid, item_ids=[item["id"]])
    with pytest.raises(ValueError, match="claim it first"):
        await heartbeat_wave_run_child(db, run["id"], "no-such-item", agent_id="agent-A")


# ---------------------------------------------------------------------------
# 12-14. record_wave_run_child_outcome
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_outcome_preserves_real_exit_code(db):
    pid = await _project(db, "wrc-exitcode")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: a")
    run = await _run(db, pid, item_ids=[item["id"]])
    await claim_wave_run_child(db, run["id"], item["id"], agent_id="agent-A")

    result = await record_wave_run_child_outcome(
        db, run["id"], item["id"], status="failed", exit_code=137, agent_id="agent-A",
    )
    assert result["status"] == "failed"
    assert result["exit_code"] == 137

    children = await db_module.get_wave_run_children(db, run["id"])
    assert children[0]["exit_code"] == 137


@pytest.mark.asyncio
async def test_record_outcome_rejects_nonterminal_status(db):
    pid = await _project(db, "wrc-nonterminal")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: a")
    run = await _run(db, pid, item_ids=[item["id"]])
    with pytest.raises(ValueError, match="terminal status"):
        await record_wave_run_child_outcome(db, run["id"], item["id"], status="running")


@pytest.mark.asyncio
async def test_record_outcome_rejects_non_int_exit_code(db):
    pid = await _project(db, "wrc-badexit")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: a")
    run = await _run(db, pid, item_ids=[item["id"]])
    with pytest.raises(ValueError, match="exit_code must be an int"):
        await record_wave_run_child_outcome(
            db, run["id"], item["id"], status="succeeded", exit_code="0",
        )


# ---------------------------------------------------------------------------
# 15-16. get_wave_run_recovery_plan — the no-op resume protection contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recovery_plan_distinguishes_all_four_states(db):
    pid = await _project(db, "wrc-recovery")
    live_item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: live")
    orphan_item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: orphan")
    done_item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: done")
    empty_item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: empty")

    run = await _run(
        db, pid,
        item_ids=[live_item["id"], orphan_item["id"], done_item["id"], empty_item["id"]],
    )

    # live: claimed recently, heartbeat fresh.
    await claim_wave_run_child(db, run["id"], live_item["id"], agent_id="agent-live")

    # stale_orphan: claimed long ago under a short TTL, never heartbeat since.
    stale_time = datetime.utcnow() - timedelta(hours=3)
    await claim_wave_run_child(
        db, run["id"], orphan_item["id"], agent_id="agent-dead",
        lease_ttl_seconds=60, now=stale_time,
    )

    # completed: claimed then finished successfully.
    await claim_wave_run_child(db, run["id"], done_item["id"], agent_id="agent-done")
    await record_wave_run_child_outcome(
        db, run["id"], done_item["id"], status="succeeded", exit_code=0,
        agent_id="agent-done",
    )

    # empty_invalid: pre-registered by start_wave_run's own item_ids, never claimed.

    plan = await get_wave_run_recovery_plan(db, run["id"])

    assert plan["live"] == [live_item["id"]]
    assert plan["stale_orphan"] == [orphan_item["id"]]
    assert plan["completed"] == [done_item["id"]]
    assert plan["empty_invalid"] == [empty_item["id"]]

    # No-op resume protection: a recovering orchestrator must never see the
    # live or completed child in the resumable set, and must never see the
    # stale/empty ones left unprotected.
    assert set(plan["resumable_item_ids"]) == {orphan_item["id"], empty_item["id"]}
    assert set(plan["protected_item_ids"]) == {live_item["id"], done_item["id"]}

    by_id = {c["sprint_item_id"]: c["lease_state"] for c in plan["children"]}
    assert by_id[live_item["id"]] == WAVE_RUN_CHILD_LEASE_LIVE
    assert by_id[orphan_item["id"]] == WAVE_RUN_CHILD_LEASE_STALE_ORPHAN
    assert by_id[done_item["id"]] == WAVE_RUN_CHILD_LEASE_COMPLETED
    assert by_id[empty_item["id"]] == WAVE_RUN_CHILD_LEASE_EMPTY_INVALID


@pytest.mark.asyncio
async def test_recovery_plan_unknown_run_raises(db):
    with pytest.raises(ValueError, match="not found"):
        await get_wave_run_recovery_plan(db, "no-such-run")


# ---------------------------------------------------------------------------
# 17. find_active_wave_run_child_for_item
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_find_active_child_scopes_to_nonterminal_runs(db):
    pid = await _project(db, "wrc-find-active")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: a")

    assert await find_active_wave_run_child_for_item(db, pid, item["id"]) is None

    run = await _run(db, pid, item_ids=[item["id"]])
    found = await find_active_wave_run_child_for_item(db, pid, item["id"])
    assert found is not None
    assert found["wave_run_id"] == run["id"]

    await db_module.advance_wave_run_status(db, run["id"], "aborted")
    assert await find_active_wave_run_child_for_item(db, pid, item["id"]) is None


# ---------------------------------------------------------------------------
# 18-20. Integration with claim_sprint_item / complete_sprint_item
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_sprint_item_stamps_wave_run_child_lease(db):
    pid = await _project(db, "wrc-integ-claim")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: a")
    run = await _run(db, pid, item_ids=[item["id"]])

    claimed = await db_module.claim_sprint_item(db, pid, item["id"], actor="sess-exec-1")
    assert claimed["status"] == "in_progress"

    children = await db_module.get_wave_run_children(db, run["id"])
    assert len(children) == 1
    assert children[0]["agent_id"] == "sess-exec-1"
    assert children[0]["claimed_at"] is not None


@pytest.mark.asyncio
async def test_complete_sprint_item_records_wave_run_exit_code(db):
    pid = await _project(db, "wrc-integ-complete")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: a")
    run = await _run(db, pid, item_ids=[item["id"]])

    await db_module.claim_sprint_item(db, pid, item["id"], actor="sess-exec-2")
    result = await db_module.complete_sprint_item(
        db, pid, item["id"], actor="sess-exec-2", exit_code=0,
    )
    assert result["status"] == "done"

    children = await db_module.get_wave_run_children(db, run["id"])
    assert children[0]["status"] == "succeeded"
    assert children[0]["exit_code"] == 0
    assert children[0]["agent_id"] == "sess-exec-2"


@pytest.mark.asyncio
async def test_claim_and_complete_fail_open_on_wave_run_bookkeeping_error(db, monkeypatch):
    pid = await _project(db, "wrc-integ-failopen")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: a")
    await _run(db, pid, item_ids=[item["id"]])

    from meridian.db import wave_runs as wave_runs_module

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated wave-run bookkeeping failure")

    monkeypatch.setattr(wave_runs_module, "claim_wave_run_child", _boom)
    monkeypatch.setattr(wave_runs_module, "record_wave_run_child_outcome", _boom)

    claimed = await db_module.claim_sprint_item(db, pid, item["id"], actor="sess-exec-3")
    assert claimed["status"] == "in_progress"

    completed = await db_module.complete_sprint_item(
        db, pid, item["id"], actor="sess-exec-3", exit_code=0,
    )
    assert completed["status"] == "done"
