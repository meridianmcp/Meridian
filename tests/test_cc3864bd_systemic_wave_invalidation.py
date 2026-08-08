"""cc3864bd — fail closed on systemic wave-run invalidation and replan after
foundational hypothesis failure.

Distinguishes ORDINARY per-item failure (the pre-existing
``finalize_wave_run`` / ``failure_mode='stop'|'continue'`` contract,
untouched by this feature) from a SYSTEMIC invalidation of the run's
foundational hypothesis (``meridian.db.wave_runs.abort_wave_run_systemic``),
which:

  * may only be declared from explicit, deterministic-policy evidence
    (:data:`meridian.db.wave_runs.SYSTEMIC_INVALIDATION_REASONS`) — never an
    unsupported guess;
  * atomically marks the wave run 'aborted' (terminal — resume/finalize both
    already refuse a terminal run via the pre-existing transition table and
    ``finalize_wave_run``'s own terminal check);
  * preserves already-``succeeded`` sibling children untouched;
  * marks every other affected pending/in-flight sprint item
    ``blocker_kind='systemic_invalidated_run'`` — a claim-time hard gate
    (``claim_sprint_item``), same enforcement point as ``'superseded'``;
  * emits a non-executable executor-to-planner corrective report via the
    existing ``executor_reports`` data layer (9154aa9a);
  * is idempotent on retry with identical evidence, and refuses to silently
    reinterpret a run that is already terminal for a different reason.

Coverage:
  1.  Ordinary continue-mode child failure does not block finalization (regression).
  2.  Ordinary stop-mode child failure blocks finalization (regression).
  3.  Evidence validation is fail-closed: missing/bad reason_code, missing basis,
      non-dict, bad affected_item_ids.
  4.  Systemic invalidation aborts the run and marks it non-executable.
  5.  Independent sibling (succeeded child) evidence is preserved, never blocked.
  6.  Affected pending/dependent items are hard-blocked from claim_sprint_item.
  7.  A non-executable executor_report is durably recorded with the evidence.
  8.  Idempotent abort: retrying with identical evidence replays, no duplicate
      event/report/blocking writes.
  9.  A run already aborted for a DIFFERENT reason refuses a second abort call.
  10. A merged run refuses systemic abort — finalized work is never resurrected.
  11. Stale-run resume rejection: an aborted run cannot transition to running/
      ready_to_resume/merged (pre-existing transition table, exercised here).
  12. Two-project isolation: a caller-supplied affected_item_ids entry from a
      DIFFERENT project is never blocked, and is reported as skipped.
  13. block_sprint_items_for_systemic_invalidation is itself idempotent.
  14. MCP handler (handle_abort_wave_run_systemic) success + evidence-rejection
      + not-found paths.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian.db.wave_runs import (
    SYSTEMIC_INVALIDATION_REASONS,
    SystemicInvalidationRejected,
    WaveRunFinalizationBlocked,
    abort_wave_run_systemic,
    validate_systemic_invalidation_evidence,
)
from meridian.db.sprint_items import block_sprint_items_for_systemic_invalidation
from meridian.mcp.handlers.sprint_tools import handle_abort_wave_run_systemic

_GOOD_EVIDENCE = {
    "reason_code": "wave1_invariant_failed",
    "basis": "Wave-1 invariant 'schema migration count matches CI' failed on 3 independent runs.",
}

_GOOD_FINALIZER_EVIDENCE = {
    "status": "ok",
    "exit_code": 0,
    "passed": 42,
    "failed": 0,
}


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


async def _run_with_items(db, project_id: str, n: int = 2, **kwargs):
    item_ids = []
    for i in range(n):
        item = await db_module.add_sprint_item(
            db, project_id, "v1", f"item-{i}", prospect_bypass=True,
        )
        item_ids.append(item["id"])
    snapshot = await db_module.build_board_snapshot(db, project_id)
    run = await db_module.create_wave_run(
        db, project_id, version="v1", snapshot=snapshot, item_ids=item_ids, **kwargs,
    )
    # create_wave_run opens in 'planned'; move it to 'running' so both
    # finalize_wave_run (only valid from running/ready_to_resume) and
    # abort_wave_run_systemic exercise the common "wave is actually in
    # flight" case rather than the less-interesting 'planned' state.
    run = await db_module.advance_wave_run_status(db, run["id"], "running")
    return run, item_ids


# ---------------------------------------------------------------------------
# 1-2. Ordinary per-item failure — regression, pre-existing contract untouched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ordinary_continue_mode_failure_does_not_block_finalization(db):
    pid = await _project(db, "cc-ordinary-continue")
    run, item_ids = await _run_with_items(db, pid, n=2)
    await db_module.record_wave_run_child(
        db, run["id"], item_ids[0], failure_mode="continue", status="failed",
    )
    await db_module.record_wave_run_child(
        db, run["id"], item_ids[1], failure_mode="continue", status="succeeded",
    )
    result = await db_module.finalize_wave_run(db, run["id"], evidence=_GOOD_FINALIZER_EVIDENCE)
    assert result["finalized"] is True
    assert result["status"] == "merged"


@pytest.mark.asyncio
async def test_ordinary_stop_mode_failure_blocks_finalization(db):
    pid = await _project(db, "cc-ordinary-stop")
    run, item_ids = await _run_with_items(db, pid, n=2)
    await db_module.record_wave_run_child(
        db, run["id"], item_ids[0], failure_mode="stop", status="failed",
    )
    with pytest.raises(WaveRunFinalizationBlocked) as exc_info:
        await db_module.finalize_wave_run(db, run["id"], evidence=_GOOD_FINALIZER_EVIDENCE)
    assert item_ids[0] in [c["sprint_item_id"] for c in exc_info.value.blocking_children]
    # Ordinary failure never touches blocker_kind or aborts the run.
    fresh_run = await db_module.get_wave_run(db, run["id"])
    assert fresh_run["status"] == "running"
    fresh_item = await db_module.get_sprint_item(db, item_ids[0])
    assert not (fresh_item.get("blocker_kind") or "")


# ---------------------------------------------------------------------------
# 3. Evidence validation — deterministic policy gate, fail closed
# ---------------------------------------------------------------------------


def test_evidence_requires_dict():
    with pytest.raises(SystemicInvalidationRejected):
        validate_systemic_invalidation_evidence("wave1_invariant_failed")


def test_evidence_requires_known_reason_code():
    with pytest.raises(SystemicInvalidationRejected):
        validate_systemic_invalidation_evidence({
            "reason_code": "i_have_a_bad_feeling", "basis": "vibes",
        })


def test_evidence_requires_nonblank_basis():
    with pytest.raises(SystemicInvalidationRejected):
        validate_systemic_invalidation_evidence({
            "reason_code": "wave1_invariant_failed", "basis": "   ",
        })


def test_evidence_rejects_bad_affected_item_ids_shape():
    with pytest.raises(SystemicInvalidationRejected):
        validate_systemic_invalidation_evidence({
            "reason_code": "wave1_invariant_failed",
            "basis": "real evidence",
            "affected_item_ids": "not-a-list",
        })


def test_evidence_accepts_every_declared_reason_code():
    for code in SYSTEMIC_INVALIDATION_REASONS:
        out = validate_systemic_invalidation_evidence({
            "reason_code": code, "basis": "concrete evidence text",
        })
        assert out["reason_code"] == code
        assert out["affected_item_ids"] == []


# ---------------------------------------------------------------------------
# 4-7. Systemic invalidation: abort, preserve, block, corrective report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_systemic_invalidation_aborts_run_and_is_non_executable(db):
    pid = await _project(db, "cc-systemic-abort")
    run, item_ids = await _run_with_items(db, pid, n=3)

    result = await abort_wave_run_systemic(
        db, run["id"], evidence=_GOOD_EVIDENCE, actor="tester",
    )
    assert result["aborted"] is True
    assert result["already_aborted"] is False
    assert result["executable"] is False
    assert result["status"] == "aborted"
    assert result["reason_code"] == "wave1_invariant_failed"

    fresh_run = await db_module.get_wave_run(db, run["id"])
    assert fresh_run["status"] == "aborted"

    events = await db_module.get_wave_run_events(db, run["id"])
    assert "systemic_invalidated" in [e["event_type"] for e in events]


@pytest.mark.asyncio
async def test_succeeded_sibling_evidence_is_preserved_not_blocked(db):
    pid = await _project(db, "cc-preserve-sibling")
    run, item_ids = await _run_with_items(db, pid, n=2)
    succeeded_id, pending_id = item_ids

    await db_module.record_wave_run_child(
        db, run["id"], succeeded_id, failure_mode="continue", status="succeeded",
    )

    result = await abort_wave_run_systemic(db, run["id"], evidence=_GOOD_EVIDENCE)
    assert succeeded_id in result["preserved_item_ids"]
    assert succeeded_id not in result["blocked_sprint_item_ids"]
    assert pending_id in result["blocked_sprint_item_ids"]

    preserved_item = await db_module.get_sprint_item(db, succeeded_id)
    assert not (preserved_item.get("blocker_kind") or "")


@pytest.mark.asyncio
async def test_affected_items_are_hard_blocked_from_claim(db):
    pid = await _project(db, "cc-claim-gate")
    run, item_ids = await _run_with_items(db, pid, n=1)
    item_id = item_ids[0]

    await abort_wave_run_systemic(db, run["id"], evidence=_GOOD_EVIDENCE)

    claimed = await db_module.claim_sprint_item(db, pid, item_id)
    assert isinstance(claimed, dict)
    assert claimed.get("blocked") is True
    assert claimed.get("error") == "SYSTEMIC_INVALIDATED_RUN"


@pytest.mark.asyncio
async def test_corrective_report_is_durably_recorded(db):
    pid = await _project(db, "cc-corrective-report")
    run, item_ids = await _run_with_items(db, pid, n=1)

    result = await abort_wave_run_systemic(db, run["id"], evidence=_GOOD_EVIDENCE, actor="tester")
    report_id = result["executor_report_id"]
    assert report_id

    report = await db_module.get_executor_report(db, report_id)
    assert report is not None
    assert report["project_id"] == pid
    assert report["status"] == "submitted"
    assert report["blockers"][0]["classification"] == "systemic_invalidated_run"
    assert report["blockers"][0]["reason_code"] == "wave1_invariant_failed"
    assert any("corrected board revision" in a for a in report["recommended_next_actions"])


# ---------------------------------------------------------------------------
# 8-10. Idempotency + terminal-state discipline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_abort_same_evidence_replays_without_new_writes(db):
    pid = await _project(db, "cc-idempotent-abort")
    run, item_ids = await _run_with_items(db, pid, n=1)

    first = await abort_wave_run_systemic(db, run["id"], evidence=_GOOD_EVIDENCE)
    events_after_first = await db_module.get_wave_run_events(db, run["id"])

    second = await abort_wave_run_systemic(db, run["id"], evidence=_GOOD_EVIDENCE)
    events_after_second = await db_module.get_wave_run_events(db, run["id"])

    assert second["already_aborted"] is True
    assert second["executor_report_id"] == first["executor_report_id"]
    assert second["blocked_sprint_item_ids"] == first["blocked_sprint_item_ids"]
    assert len(events_after_second) == len(events_after_first)


@pytest.mark.asyncio
async def test_second_abort_with_different_reason_is_refused(db):
    pid = await _project(db, "cc-different-reason-refused")
    run, item_ids = await _run_with_items(db, pid, n=1)
    await abort_wave_run_systemic(db, run["id"], evidence=_GOOD_EVIDENCE)

    with pytest.raises(ValueError):
        await abort_wave_run_systemic(
            db, run["id"],
            evidence={
                "reason_code": "safety_integrity_gate_failed",
                "basis": "a different, unrelated failure",
            },
        )


@pytest.mark.asyncio
async def test_merged_run_refuses_systemic_abort(db):
    pid = await _project(db, "cc-merged-refuses-abort")
    run, item_ids = await _run_with_items(db, pid, n=1)
    await db_module.finalize_wave_run(db, run["id"], evidence=_GOOD_FINALIZER_EVIDENCE)

    with pytest.raises(ValueError):
        await abort_wave_run_systemic(db, run["id"], evidence=_GOOD_EVIDENCE)


# ---------------------------------------------------------------------------
# 11. Stale-run resume rejection (pre-existing transition table, exercised
#     here to confirm this feature does not accidentally weaken it)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aborted_run_cannot_resume_or_finalize(db):
    pid = await _project(db, "cc-stale-resume-rejected")
    run, item_ids = await _run_with_items(db, pid, n=1)
    await abort_wave_run_systemic(db, run["id"], evidence=_GOOD_EVIDENCE)

    with pytest.raises(ValueError):
        await db_module.advance_wave_run_status(db, run["id"], "running")
    with pytest.raises(ValueError):
        await db_module.advance_wave_run_status(db, run["id"], "ready_to_resume")
    with pytest.raises(ValueError):
        await db_module.finalize_wave_run(db, run["id"], evidence=_GOOD_FINALIZER_EVIDENCE)


# ---------------------------------------------------------------------------
# 12-13. Two-project isolation + block-function idempotency, direct
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_project_isolation_never_blocks_foreign_item(db):
    pid_a = await _project(db, "cc-isolation-a")
    pid_b = await _project(db, "cc-isolation-b")
    run_a, items_a = await _run_with_items(db, pid_a, n=1)
    foreign_item = await db_module.add_sprint_item(
        db, pid_b, "v1", "foreign item", prospect_bypass=True,
    )
    foreign_id = foreign_item["id"]

    evidence = dict(_GOOD_EVIDENCE)
    evidence["affected_item_ids"] = [foreign_id]
    result = await abort_wave_run_systemic(db, run_a["id"], evidence=evidence)

    assert foreign_id not in result["blocked_sprint_item_ids"]
    foreign_item = await db_module.get_sprint_item(db, foreign_id)
    assert not (foreign_item.get("blocker_kind") or "")


@pytest.mark.asyncio
async def test_block_helper_is_idempotent_directly(db):
    pid = await _project(db, "cc-block-helper-idempotent")
    item = await db_module.add_sprint_item(db, pid, "v1", "solo", prospect_bypass=True)
    item_id = item["id"]

    first = await block_sprint_items_for_systemic_invalidation(
        db, pid, [item_id],
        wave_run_id="fake-run", reason_code="wave1_invariant_failed", basis="x",
    )
    second = await block_sprint_items_for_systemic_invalidation(
        db, pid, [item_id],
        wave_run_id="fake-run", reason_code="wave1_invariant_failed", basis="x",
    )
    assert first["blocked_item_ids"] == [item_id]
    assert second["blocked_item_ids"] == [item_id]


@pytest.mark.asyncio
async def test_block_helper_preserves_done_items(db):
    pid = await _project(db, "cc-block-helper-preserves-done")
    item = await db_module.add_sprint_item(db, pid, "v1", "done item", prospect_bypass=True)
    item_id = item["id"]
    await db_module.claim_sprint_item(db, pid, item_id)
    await db_module.complete_sprint_item(db, pid, item_id)

    result = await block_sprint_items_for_systemic_invalidation(
        db, pid, [item_id],
        wave_run_id="fake-run", reason_code="wave1_invariant_failed", basis="x",
    )
    assert result["preserved_item_ids"] == [item_id]
    assert result["blocked_item_ids"] == []
    item = await db_module.get_sprint_item(db, item_id)
    assert not (item.get("blocker_kind") or "")


# ---------------------------------------------------------------------------
# 14. MCP handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_success_path(db):
    pid = await _project(db, "cc-handler-success")
    run, item_ids = await _run_with_items(db, pid, n=1)

    result = await handle_abort_wave_run_systemic(
        {"wave_run_id": run["id"], "evidence": _GOOD_EVIDENCE, "actor": "tester"},
        db, "/tmp", None, None,
    )
    assert result["aborted"] is True
    assert result["status"] == "aborted"


@pytest.mark.asyncio
async def test_handler_rejects_bad_evidence(db):
    pid = await _project(db, "cc-handler-bad-evidence")
    run, item_ids = await _run_with_items(db, pid, n=1)

    result = await handle_abort_wave_run_systemic(
        {"wave_run_id": run["id"], "evidence": {"reason_code": "nonsense"}},
        db, "/tmp", None, None,
    )
    assert result.get("aborted") is False
    assert "error" in result


@pytest.mark.asyncio
async def test_handler_requires_wave_run_id(db):
    result = await handle_abort_wave_run_systemic(
        {"evidence": _GOOD_EVIDENCE}, db, "/tmp", None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_handler_unknown_wave_run(db):
    result = await handle_abort_wave_run_systemic(
        {"wave_run_id": "does-not-exist", "evidence": _GOOD_EVIDENCE},
        db, "/tmp", None, None,
    )
    assert result.get("aborted") is False
    assert "error" in result
