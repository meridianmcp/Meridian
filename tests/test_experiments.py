"""Tests for sprint item 3f6b8715 — W1-M Experiment Registry.

Mirrors tests/test_research_run.py's structure: model-layer validation
tests (no DB), db-layer lifecycle tests (the `db` fixture), and an
MCP-dispatch-level smoke test.

HARD INVARIANT under test throughout this file: a run must never reach a
terminal status (completed/abandoned/expired) without a corresponding
experiment_events row for that run_id. Every terminal-transition test below
queries experiment_events afterward and asserts >=1 matching row.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import experiment as model
from meridian.db import experiments as exp_db
import meridian.mcp_tools as mcp_tools
import meridian.server as server


async def _session(db, prefix: str):
    project = await db_module.create_project(db, prefix)
    session = await db_module.register_session(db, project["id"], f"{prefix}-session")
    return project, session


# ---------------------------------------------------------------------------
# Model-layer validation (no DB)
# ---------------------------------------------------------------------------


def test_validate_outcome_summary_rejects_none_and_empty():
    with pytest.raises(model.ExperimentError, match="outcome_summary is required"):
        model.validate_outcome_summary(None)
    with pytest.raises(model.ExperimentError, match="outcome_summary is required"):
        model.validate_outcome_summary("   ")
    assert model.validate_outcome_summary(None, required=False) is None
    assert model.validate_outcome_summary("Confirmed the hypothesis.") == "Confirmed the hypothesis."


def test_validate_disposition_rejects_none_and_unknown():
    with pytest.raises(model.ExperimentError, match="disposition is required"):
        model.validate_disposition(None)
    assert model.validate_disposition(None, required=False) is None
    with pytest.raises(model.ExperimentError, match="disposition must be one of"):
        model.validate_disposition("archive")
    assert model.validate_disposition("Promote") == "promote"


def test_validate_result_receipt_rejects_over_32kb_deterministically():
    """3f6b8715 — REJECT (raise), never silently truncate, once the encoded
    receipt exceeds the 32KB cap, matching meridian.research_run's own
    established convention for exactly this situation."""
    huge = {"blob": "x" * 40_000}
    with pytest.raises(model.ExperimentError, match="32768-byte cap|32_768-byte cap"):
        model.validate_result_receipt(huge)
    small = model.validate_result_receipt({"summary": "ok"})
    assert small == {"summary": "ok"}
    assert model.validate_result_receipt(None) is None


def test_validate_logical_path_rejects_absolute_and_traversal():
    with pytest.raises(model.ExperimentError, match="absolute path"):
        model.validate_logical_path("C:\\Users\\adam\\out.csv")
    with pytest.raises(model.ExperimentError, match="absolute path"):
        model.validate_logical_path("/etc/passwd")
    with pytest.raises(model.ExperimentError, match="escape the project root"):
        model.validate_logical_path("../outside/file.csv")
    assert model.validate_logical_path("outputs\\fig1.png") == "outputs/fig1.png"


def test_validate_event_type_and_artifact_role_closed_vocab():
    with pytest.raises(model.ExperimentError, match="event_type must be one of"):
        model.validate_event_type("oops")
    assert model.validate_event_type("Dead_End") == "dead_end"
    with pytest.raises(model.ExperimentError, match="artifact_role must be one of"):
        model.validate_artifact_role("binary")
    assert model.validate_artifact_role(None) is None
    assert model.validate_artifact_role("Figure") == "figure"


def test_is_dead_end_outcome_detects_abandoned_and_text_markers():
    assert model.is_dead_end_outcome("abandoned", "irrelevant text") is True
    assert model.is_dead_end_outcome("completed", "This was a Dead End.") is True
    assert model.is_dead_end_outcome("completed", "The build FAILED under load.") is True
    assert model.is_dead_end_outcome("completed", "Confirmed the hypothesis.") is False


def test_compute_expires_at_and_is_run_expired_handle_none_ttl():
    started = model.utcnow_iso()
    assert model.compute_expires_at(started, None) is None
    assert model.is_run_expired(None) is False
    expires = model.compute_expires_at(started, 60)
    assert expires is not None
    assert model.is_run_expired("2000-01-01T00:00:00") is True


# ---------------------------------------------------------------------------
# DB-layer lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_lifecycle_create_start_complete_keep_and_verify_event(db):
    project, session = await _session(db, "exp-lifecycle")
    experiment = await exp_db.create_experiment(
        db, project["id"], session["id"], name="Faster cold start", hypothesis="Caching helps.",
    )
    assert experiment["status"] == "active"
    assert experiment["hypothesis"] == "Caching helps."

    run = await exp_db.start_experiment_run(
        db, project["id"], session["id"],
        experiment_id=experiment["id"], trial_label="trial-1",
    )
    assert run["status"] == "active"
    assert run["outcome_summary"] is None
    assert run["disposition"] is None

    completed = await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=run["id"], outcome_summary="Cache cut startup by 40%.", disposition="keep",
    )
    assert completed["status"] == "completed"
    assert completed["disposition"] == "keep"
    assert completed["completed_at"]

    # HARD INVARIANT: a matching experiment_events row exists for this run.
    events = await exp_db.get_experiment_events(db, project["id"], experiment_id=experiment["id"])
    run_events = [e for e in events if e["run_id"] == run["id"]]
    assert len(run_events) >= 1
    # A "keep" completion with a non-dead-end summary should NOT be
    # misclassified as a dead_end.
    assert all(e["event_type"] != "dead_end" for e in run_events)


@pytest.mark.asyncio
async def test_abandoned_run_auto_writes_dead_end_event(db):
    project, session = await _session(db, "exp-abandoned")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Dead end probe")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])

    completed = await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=run["id"], outcome_summary="Gave up after three tries.",
        disposition="discard", status="abandoned",
    )
    assert completed["status"] == "abandoned"

    events = await exp_db.get_experiment_events(
        db, project["id"], experiment_id=experiment["id"], run_id=run["id"],
    )
    dead_ends = [e for e in events if e["event_type"] == "dead_end"]
    assert len(dead_ends) == 1
    assert dead_ends[0]["label"] == "auto"
    assert dead_ends[0]["body"] == "Gave up after three tries."


@pytest.mark.asyncio
async def test_completed_run_with_failed_text_also_auto_writes_dead_end(db):
    """A 'completed' status run whose outcome_summary itself signals failure
    (case-insensitive 'dead end'/'failed' substring) must ALSO auto-write a
    dead_end event -- not only an explicit status='abandoned'."""
    project, session = await _session(db, "exp-failed-text")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Text-flagged dead end")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])

    completed = await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=run["id"], outcome_summary="The migration FAILED under concurrent load.",
        disposition="discard",
    )
    assert completed["status"] == "completed"

    events = await exp_db.get_experiment_events(
        db, project["id"], experiment_id=experiment["id"], run_id=run["id"],
    )
    assert any(e["event_type"] == "dead_end" for e in events)


@pytest.mark.asyncio
async def test_pivot_auto_writes_pivot_event_on_new_run(db):
    project, session = await _session(db, "exp-pivot")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Pivot probe")
    parent = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])
    await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=parent["id"], outcome_summary="Approach A didn't pan out.", disposition="discard",
    )

    child = await exp_db.start_experiment_run(
        db, project["id"], session["id"],
        experiment_id=experiment["id"], pivot_parent_run_id=parent["id"], trial_label="approach-b",
    )
    assert child["pivot_parent_run_id"] == parent["id"]

    events = await exp_db.get_experiment_events(
        db, project["id"], experiment_id=experiment["id"], run_id=child["id"],
    )
    pivots = [e for e in events if e["event_type"] == "pivot"]
    assert len(pivots) == 1
    assert pivots[0]["label"] == "auto"
    assert parent["id"] in pivots[0]["body"]


@pytest.mark.asyncio
async def test_pivot_parent_must_belong_to_same_experiment(db):
    project, session = await _session(db, "exp-pivot-cross")
    exp_a = await exp_db.create_experiment(db, project["id"], session["id"], name="A")
    exp_b = await exp_db.create_experiment(db, project["id"], session["id"], name="B")
    run_a = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=exp_a["id"])

    with pytest.raises(ValueError, match="must stay within the same experiment"):
        await exp_db.start_experiment_run(
            db, project["id"], session["id"],
            experiment_id=exp_b["id"], pivot_parent_run_id=run_a["id"],
        )


@pytest.mark.asyncio
async def test_pivot_tree_structure_start_complete_start_pivot_complete(db):
    """3f6b8715 acceptance case: start -> complete(discard) -> start(pivot
    from parent) -> complete(keep)."""
    project, session = await _session(db, "exp-pivot-tree")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Tree probe")

    trial_1 = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])
    trial_1 = await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=trial_1["id"], outcome_summary="Dead end with the first formula.", disposition="discard",
    )
    assert trial_1["status"] == "completed"

    trial_2 = await exp_db.start_experiment_run(
        db, project["id"], session["id"],
        experiment_id=experiment["id"], pivot_parent_run_id=trial_1["id"],
    )
    trial_2 = await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=trial_2["id"], outcome_summary="Second formula worked.", disposition="keep",
    )
    assert trial_2["status"] == "completed"
    assert trial_2["pivot_parent_run_id"] == trial_1["id"]

    runs = await exp_db.list_experiment_runs(db, project["id"], experiment_id=experiment["id"])
    assert {r["id"] for r in runs} == {trial_1["id"], trial_2["id"]}

    # Both terminal transitions left an event trail (invariant).
    all_events = await exp_db.get_experiment_events(db, project["id"], experiment_id=experiment["id"])
    assert any(e["run_id"] == trial_1["id"] and e["event_type"] == "dead_end" for e in all_events)
    assert any(e["run_id"] == trial_2["id"] and e["event_type"] == "pivot" for e in all_events)


@pytest.mark.asyncio
async def test_promote_auto_writes_breakthrough_event(db):
    project, session = await _session(db, "exp-promote")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Promote probe")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])
    await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=run["id"], outcome_summary="This is the breakthrough result.", disposition="promote",
    )

    result = await exp_db.promote_experiment_run(db, project["id"], session["id"], run_id=run["id"])
    assert result["run_id"] == run["id"]
    assert result["event"]["event_type"] == "breakthrough"
    assert result["event"]["body"] == "This is the breakthrough result."

    events = await exp_db.get_experiment_events(
        db, project["id"], experiment_id=experiment["id"], run_id=run["id"],
    )
    assert any(e["event_type"] == "breakthrough" for e in events)


@pytest.mark.asyncio
async def test_promote_requires_promote_disposition(db):
    project, session = await _session(db, "exp-promote-guard")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Guard probe")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])
    await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=run["id"], outcome_summary="Nothing special.", disposition="discard",
    )
    with pytest.raises(ValueError, match="requires disposition='promote'"):
        await exp_db.promote_experiment_run(db, project["id"], session["id"], run_id=run["id"])


@pytest.mark.asyncio
async def test_expire_stale_runs_auto_writes_dead_end_per_expired_run(db):
    project, session = await _session(db, "exp-expiry")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Expiry probe")
    run = await exp_db.start_experiment_run(
        db, project["id"], session["id"], experiment_id=experiment["id"], ttl_seconds=60,
    )
    # Force expiry into the past directly (no real wait in a unit test).
    await db.execute(
        "UPDATE experiment_runs SET expires_at = '2000-01-01T00:00:00' WHERE id = ?",
        (run["id"],),
    )
    await db.commit()

    other_project, other_session = await _session(db, "exp-expiry-other")
    other_experiment = await exp_db.create_experiment(db, other_project["id"], other_session["id"], name="Other")
    still_fresh = await exp_db.start_experiment_run(
        db, other_project["id"], other_session["id"], experiment_id=other_experiment["id"], ttl_seconds=3600,
    )

    count = await exp_db.expire_stale_runs(db, project["id"])
    assert count == 1
    expired = await exp_db.get_experiment_run(db, project["id"], run_id=run["id"])
    assert expired["status"] == "expired"
    assert expired["completed_at"]

    events = await exp_db.get_experiment_events(
        db, project["id"], experiment_id=experiment["id"], run_id=run["id"],
    )
    dead_ends = [e for e in events if e["event_type"] == "dead_end" and e["label"] == "expired"]
    assert len(dead_ends) == 1
    assert dead_ends[0]["body"] == "expired without explicit completion"

    # Scoped to project_id — the other project's fresh run is untouched.
    other_count = await exp_db.expire_stale_runs(db, other_project["id"])
    assert other_count == 0
    still = await exp_db.get_experiment_run(db, other_project["id"], run_id=still_fresh["id"])
    assert still["status"] == "active"

    # Idempotent: expiring again finds nothing left to expire.
    assert await exp_db.expire_stale_runs(db, project["id"]) == 0


@pytest.mark.asyncio
async def test_run_without_ttl_never_auto_expires(db):
    project, session = await _session(db, "exp-no-ttl")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="No TTL probe")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])
    assert run["expires_at"] is None

    count = await exp_db.expire_stale_runs(db, project["id"])
    assert count == 0
    still = await exp_db.get_experiment_run(db, project["id"], run_id=run["id"])
    assert still["status"] == "active"


# ---------------------------------------------------------------------------
# HARD INVARIANT — every terminal transition leaves >=1 experiment_events row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invariant_every_terminal_transition_has_an_event(db):
    """3f6b8715 HARD INVARIANT: query experiment_events after EVERY terminal
    transition path (complete->keep, complete->abandoned,
    complete->completed-with-dead-end-text, expire) and assert >=1 row, with
    no exceptions raised anywhere in the sequence."""
    project, session = await _session(db, "exp-invariant")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Invariant probe")

    def _events_for(run_id):
        return run_id

    checked_run_ids: list[str] = []

    # Path 1: complete -> keep (non-dead-end text).
    r1 = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])
    r1 = await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=r1["id"], outcome_summary="Clean success.", disposition="keep",
    )
    checked_run_ids.append(r1["id"])

    # Path 2: complete -> explicit abandoned.
    r2 = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])
    r2 = await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=r2["id"], outcome_summary="Giving up here.", disposition="discard", status="abandoned",
    )
    checked_run_ids.append(r2["id"])

    # Path 3: complete -> 'completed' but outcome text flags a dead end.
    r3 = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])
    r3 = await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=r3["id"], outcome_summary="This was a dead end after all.", disposition="discard",
    )
    checked_run_ids.append(r3["id"])

    # Path 4: expire_stale_runs.
    r4 = await exp_db.start_experiment_run(
        db, project["id"], session["id"], experiment_id=experiment["id"], ttl_seconds=60,
    )
    await db.execute(
        "UPDATE experiment_runs SET expires_at = '2000-01-01T00:00:00' WHERE id = ?", (r4["id"],),
    )
    await db.commit()
    assert await exp_db.expire_stale_runs(db, project["id"]) == 1
    checked_run_ids.append(r4["id"])

    for run_id in checked_run_ids:
        run = await exp_db.get_experiment_run(db, project["id"], run_id=run_id)
        assert run["status"] in model.RUN_TERMINAL_STATUSES
        run_events = await exp_db.get_experiment_events(
            db, project["id"], experiment_id=experiment["id"], run_id=run_id,
        )
        assert len(run_events) >= 1, f"run {run_id} reached terminal status with NO experiment_events row"


# ---------------------------------------------------------------------------
# Required-field rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_run_outcome_summary_none_raises_valueerror(db):
    project, session = await _session(db, "exp-reject-summary")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Reject probe")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])
    with pytest.raises(ValueError, match="outcome_summary is required"):
        await exp_db.complete_experiment_run(
            db, project["id"], session["id"],
            run_id=run["id"], outcome_summary=None, disposition="keep",
        )
    # The run must remain active — a rejected completion is not a partial one.
    unchanged = await exp_db.get_experiment_run(db, project["id"], run_id=run["id"])
    assert unchanged["status"] == "active"


@pytest.mark.asyncio
async def test_complete_run_disposition_none_raises_valueerror(db):
    project, session = await _session(db, "exp-reject-disposition")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Reject probe 2")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])
    with pytest.raises(ValueError, match="disposition is required"):
        await exp_db.complete_experiment_run(
            db, project["id"], session["id"],
            run_id=run["id"], outcome_summary="Some outcome.", disposition=None,
        )
    unchanged = await exp_db.get_experiment_run(db, project["id"], run_id=run["id"])
    assert unchanged["status"] == "active"


@pytest.mark.asyncio
async def test_complete_run_validates_even_on_already_terminal_run(db):
    """Validation must be UNCONDITIONAL — even a retry against an
    already-terminal run still rejects a missing outcome_summary/disposition
    rather than silently short-circuiting past validation."""
    project, session = await _session(db, "exp-reject-terminal-retry")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Retry probe")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])
    await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=run["id"], outcome_summary="First completion.", disposition="keep",
    )
    with pytest.raises(ValueError, match="outcome_summary is required"):
        await exp_db.complete_experiment_run(
            db, project["id"], session["id"],
            run_id=run["id"], outcome_summary=None, disposition="keep",
        )
    # A VALID retry, however, is idempotent — returns the original state.
    retried = await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=run["id"], outcome_summary="A different summary.", disposition="discard",
    )
    assert retried["outcome_summary"] == "First completion."
    assert retried["disposition"] == "keep"


@pytest.mark.asyncio
async def test_oversized_result_receipt_raises_valueerror(db):
    project, session = await _session(db, "exp-oversized-receipt")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Receipt probe")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])
    with pytest.raises(ValueError, match="32768-byte cap|32_768-byte cap"):
        await exp_db.complete_experiment_run(
            db, project["id"], session["id"],
            run_id=run["id"], outcome_summary="fine", disposition="keep",
            result_receipt={"blob": "x" * 40_000},
        )
    unchanged = await exp_db.get_experiment_run(db, project["id"], run_id=run["id"])
    assert unchanged["status"] == "active"


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_run_artifact_rejects_absolute_paths(db):
    project, session = await _session(db, "exp-artifact-abs")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Artifact probe")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])

    with pytest.raises(ValueError, match="absolute path"):
        await exp_db.register_run_artifact(
            db, project["id"], session["id"],
            run_id=run["id"], logical_path="C:\\Users\\adam\\figure.png",
        )
    with pytest.raises(ValueError, match="absolute path"):
        await exp_db.register_run_artifact(
            db, project["id"], session["id"],
            run_id=run["id"], logical_path="/tmp/figure.png",
        )

    artifact = await exp_db.register_run_artifact(
        db, project["id"], session["id"],
        run_id=run["id"], logical_path="outputs/figure.png",
        artifact_role="figure", content_hash="abc123",
    )
    assert artifact["logical_path"] == "outputs/figure.png"
    assert artifact["artifact_role"] == "figure"
    assert artifact["host_visibility"] == "local"


@pytest.mark.asyncio
async def test_register_run_artifact_rejects_unknown_run(db):
    project, session = await _session(db, "exp-artifact-no-run")
    with pytest.raises(ValueError, match="not found"):
        await exp_db.register_run_artifact(
            db, project["id"], session["id"],
            run_id="no-such-run", logical_path="outputs/x.csv",
        )


# ---------------------------------------------------------------------------
# Events — manual enrichment coexists with auto-skeleton writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_experiment_event_enrichment_coexists_with_auto_writes(db):
    project, session = await _session(db, "exp-events-coexist")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Events probe")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])

    # Manual, experiment-level note (no run_id).
    note_event = await exp_db.record_experiment_event(
        db, project["id"], session["id"],
        experiment_id=experiment["id"], event_type="note", body="Started reading the literature.",
    )
    assert note_event["run_id"] is None

    # Manual, run-scoped milestone alongside the auto pivot/dead_end machinery.
    milestone = await exp_db.record_experiment_event(
        db, project["id"], session["id"],
        experiment_id=experiment["id"], run_id=run["id"],
        event_type="milestone", label="checkpoint", body="Halfway through the sweep.",
        artifact_ids=["artifact-1", "artifact-2"],
    )
    assert milestone["artifact_ids"] == ["artifact-1", "artifact-2"]

    await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=run["id"], outcome_summary="Dead end — the sweep found nothing.", disposition="discard",
    )

    all_events = await exp_db.get_experiment_events(db, project["id"], experiment_id=experiment["id"])
    types = {e["event_type"] for e in all_events}
    # Both manual events AND the auto-written dead_end coexist.
    assert {"note", "milestone", "dead_end"} <= types
    assert len(all_events) == 3

    run_scoped = await exp_db.get_experiment_events(
        db, project["id"], experiment_id=experiment["id"], run_id=run["id"],
    )
    assert {e["event_type"] for e in run_scoped} == {"milestone", "dead_end"}


@pytest.mark.asyncio
async def test_record_experiment_event_rejects_run_from_different_experiment(db):
    project, session = await _session(db, "exp-events-cross")
    exp_a = await exp_db.create_experiment(db, project["id"], session["id"], name="A")
    exp_b = await exp_db.create_experiment(db, project["id"], session["id"], name="B")
    run_a = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=exp_a["id"])

    with pytest.raises(ValueError, match="belongs to experiment"):
        await exp_db.record_experiment_event(
            db, project["id"], session["id"],
            experiment_id=exp_b["id"], run_id=run_a["id"], event_type="note",
        )


@pytest.mark.asyncio
async def test_list_experiments_and_get_scope_by_project(db):
    project, session = await _session(db, "exp-scope-a")
    other_project, other_session = await _session(db, "exp-scope-b")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Scoped")

    assert await exp_db.get_experiment(db, other_project["id"], experiment_id=experiment["id"]) is None
    listed = await exp_db.list_experiments(db, project["id"])
    assert [e["id"] for e in listed] == [experiment["id"]]
    assert await exp_db.list_experiments(db, other_project["id"]) == []


# ---------------------------------------------------------------------------
# MCP dispatch level
# ---------------------------------------------------------------------------


def test_mcp_tools_advertise_experiment_registry_surface():
    names = {tool["name"] for tool in mcp_tools._MCP_TOOLS_LIST}
    assert {
        "create_experiment", "get_experiment", "list_experiments",
        "start_experiment_run", "complete_experiment_run", "promote_experiment_run",
        "get_experiment_run", "list_experiment_runs", "register_run_artifact",
        "record_experiment_event", "get_experiment_events",
    } <= names


@pytest.mark.asyncio
async def test_mcp_dispatch_create_start_complete_get_list(db, tmp_path):
    project, session = await _session(db, "exp-mcp")
    created = await server._dispatch_mcp_tool(
        "create_experiment",
        {"project_id": project["id"], "session_id": session["id"], "name": "MCP probe"},
        db, str(tmp_path),
    )
    experiment_id = created["experiment"]["id"]

    started = await server._dispatch_mcp_tool(
        "start_experiment_run",
        {"project_id": project["id"], "session_id": session["id"], "experiment_id": experiment_id},
        db, str(tmp_path),
    )
    run_id = started["run"]["id"]
    assert started["run"]["status"] == "active"

    completed = await server._dispatch_mcp_tool(
        "complete_experiment_run",
        {
            "project_id": project["id"], "session_id": session["id"], "run_id": run_id,
            "outcome_summary": "MCP dispatch worked end to end.", "disposition": "keep",
        },
        db, str(tmp_path),
    )
    assert completed["run"]["status"] == "completed"

    events = await server._dispatch_mcp_tool(
        "get_experiment_events",
        {"project_id": project["id"], "experiment_id": experiment_id},
        db, str(tmp_path),
    )
    assert events["count"] >= 1

    missing = await server._dispatch_mcp_tool(
        "get_experiment_run", {"project_id": project["id"], "run_id": "no-such-run"}, db, str(tmp_path),
    )
    assert "error" in missing

    rejected = await server._dispatch_mcp_tool(
        "complete_experiment_run",
        {
            "project_id": project["id"], "session_id": session["id"], "run_id": run_id,
            "outcome_summary": None, "disposition": "keep",
        },
        db, str(tmp_path),
    )
    assert "error" in rejected
