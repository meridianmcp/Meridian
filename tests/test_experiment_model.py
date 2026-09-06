"""Tests for sprint item 4376e655 — the provider-neutral Experiment/Run/
RunAttempt state model.

Covers:
  * meridian.experiment_model — the pure closed-vocabulary/transition/
    fingerprint layer.
  * meridian.db.experiment_model — the persistence layer (create_experiment/
    create_run/create_attempt/transition_attempt/heartbeat_attempt/
    reconcile_stale_attempts/get_run), on SQLite via the `db` fixture.

Focused, serial (no xdist) per this item's required_tool note — these tests
share no external state and are safe to run with `-p no:xdist` as instructed
for RAM-constrained executor environments.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from meridian import db as db_module
from meridian import experiment_model as em


# ---------------------------------------------------------------------------
# meridian.experiment_model — pure vocabulary, transitions, fingerprint.
# ---------------------------------------------------------------------------


def test_attempt_statuses_and_terminal_set_are_documented():
    assert em.ATTEMPT_STATUSES == {
        "queued", "running", "succeeded", "failed", "cancelled", "crashed", "unknown",
    }
    assert em.ATTEMPT_TERMINAL_STATUSES == {"succeeded", "failed", "cancelled", "crashed"}
    assert em.ATTEMPT_TERMINAL_STATUSES <= em.ATTEMPT_STATUSES
    assert "unknown" not in em.ATTEMPT_TERMINAL_STATUSES


def test_validate_attempt_status_accepts_all_and_rejects_unknown():
    for status in em.ATTEMPT_STATUSES:
        assert em.validate_attempt_status(status) == status
        assert em.validate_attempt_status(status.upper()) == status
    with pytest.raises(ValueError, match="status must be one of"):
        em.validate_attempt_status("bogus")


def test_validate_failure_class_accepts_all_and_rejects_unknown():
    for fc in em.FAILURE_CLASSES:
        assert em.validate_failure_class(fc) == fc
    with pytest.raises(ValueError, match="failure_class must be one of"):
        em.validate_failure_class("bogus-class")


def test_transition_same_status_is_always_a_noop():
    for status in em.ATTEMPT_STATUSES:
        assert em.validate_attempt_transition(status, status) == status


def test_legal_transitions_queued_to_running_to_succeeded():
    assert em.validate_attempt_transition("queued", "running") == "running"
    assert em.validate_attempt_transition("running", "succeeded") == "succeeded"


def test_illegal_transition_from_terminal_status_is_rejected():
    with pytest.raises(ValueError, match="illegal attempt transition 'succeeded' -> 'running'"):
        em.validate_attempt_transition("succeeded", "running")
    with pytest.raises(ValueError, match="illegal attempt transition"):
        em.validate_attempt_transition("failed", "succeeded")


def test_illegal_transition_skipping_running_is_rejected():
    with pytest.raises(ValueError, match="illegal attempt transition 'queued' -> 'succeeded'"):
        em.validate_attempt_transition("queued", "succeeded")


def test_unknown_can_reconcile_to_any_real_outcome():
    for target in ("queued", "running", "succeeded", "failed", "crashed", "cancelled"):
        assert em.validate_attempt_transition("unknown", target) == target


def test_is_terminal_status():
    assert em.is_terminal_status("succeeded") is True
    assert em.is_terminal_status("crashed") is True
    assert em.is_terminal_status("running") is False
    assert em.is_terminal_status("unknown") is False


def test_params_fingerprint_none_for_empty():
    assert em.params_fingerprint(None) is None
    assert em.params_fingerprint({}) is None


def test_params_fingerprint_deterministic_regardless_of_key_order():
    a = em.params_fingerprint({"lr": 0.1, "batch_size": 32})
    b = em.params_fingerprint({"batch_size": 32, "lr": 0.1})
    assert a == b
    assert a.startswith("sha256:")


def test_params_fingerprint_differs_for_different_params():
    a = em.params_fingerprint({"lr": 0.1})
    b = em.params_fingerprint({"lr": 0.2})
    assert a != b


# ---------------------------------------------------------------------------
# meridian.db.experiment_model — persistence, on the `db` fixture (real
# init_db startup chain — proves the migration is actually wired in, not
# just directly callable).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_experiment_model_wired_into_full_init_db(db):
    project = await db_module.create_project(db, "exp-wiring")
    experiment = await db_module.create_experiment(db, project["id"], name="baseline sweep")
    assert experiment["project_id"] == project["id"]
    run = await db_module.create_run(db, project["id"], experiment["id"], params={"lr": 0.1})
    assert run["status"] == "queued"


@pytest.mark.asyncio
async def test_research_runs_table_stores_no_status_column(db):
    """Structural guarantee behind 'restart recovery re-derives live state
    rather than replaying stale text': there is no status column on
    research_runs AT ALL for a cached value to ever drift from the
    attempts that are the actual source of truth."""
    async with db.execute("PRAGMA table_info(research_runs)") as cur:
        cols = {row["name"] for row in await cur.fetchall()}
    assert "status" not in cols


@pytest.mark.asyncio
async def test_create_experiment_rejects_secret_looking_name(db):
    project = await db_module.create_project(db, "exp-1")
    with pytest.raises(ValueError, match="Refusing to persist"):
        await db_module.create_experiment(
            db, project["id"],
            name="sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )


@pytest.mark.asyncio
async def test_get_experiment_cross_project_returns_none(db):
    p1 = await db_module.create_project(db, "exp-2a")
    p2 = await db_module.create_project(db, "exp-2b")
    experiment = await db_module.create_experiment(db, p1["id"], name="p1-only")
    assert await db_module.get_experiment(db, p2["id"], experiment["id"]) is None
    assert await db_module.get_experiment(db, p1["id"], experiment["id"]) is not None


@pytest.mark.asyncio
async def test_create_run_rejects_experiment_not_in_project(db):
    p1 = await db_module.create_project(db, "exp-3a")
    p2 = await db_module.create_project(db, "exp-3b")
    experiment = await db_module.create_experiment(db, p1["id"])
    with pytest.raises(ValueError, match="not found in project"):
        await db_module.create_run(db, p2["id"], experiment["id"], params={})


@pytest.mark.asyncio
async def test_create_run_computes_params_fingerprint(db):
    project = await db_module.create_project(db, "exp-4")
    experiment = await db_module.create_experiment(db, project["id"])
    run = await db_module.create_run(db, project["id"], experiment["id"], params={"lr": 0.1})
    assert run["params_fingerprint"] == em.params_fingerprint({"lr": 0.1})


@pytest.mark.asyncio
async def test_create_run_same_idempotency_key_returns_same_run_not_a_duplicate(db):
    """The literal acceptance criterion: retries must not duplicate a run."""
    project = await db_module.create_project(db, "exp-5")
    experiment = await db_module.create_experiment(db, project["id"])
    first = await db_module.create_run(
        db, project["id"], experiment["id"], params={"lr": 0.1}, idempotency_key="submit-1",
    )
    second = await db_module.create_run(
        db, project["id"], experiment["id"], params={"lr": 0.9}, idempotency_key="submit-1",
    )
    assert first["id"] == second["id"]
    # The FIRST call's params win — a repeat submission is a no-op, not an update.
    assert second["params"] == {"lr": 0.1}
    async with db.execute(
        "SELECT COUNT(*) AS n FROM research_runs WHERE experiment_id = ?", (experiment["id"],)
    ) as cur:
        row = await cur.fetchone()
    assert int(row["n"]) == 1


@pytest.mark.asyncio
async def test_create_run_without_idempotency_key_always_creates_new_run(db):
    project = await db_module.create_project(db, "exp-6")
    experiment = await db_module.create_experiment(db, project["id"])
    first = await db_module.create_run(db, project["id"], experiment["id"])
    second = await db_module.create_run(db, project["id"], experiment["id"])
    assert first["id"] != second["id"]


@pytest.mark.asyncio
async def test_create_run_different_idempotency_keys_are_distinct_runs(db):
    project = await db_module.create_project(db, "exp-7")
    experiment = await db_module.create_experiment(db, project["id"])
    first = await db_module.create_run(db, project["id"], experiment["id"], idempotency_key="a")
    second = await db_module.create_run(db, project["id"], experiment["id"], idempotency_key="b")
    assert first["id"] != second["id"]


@pytest.mark.asyncio
async def test_get_run_status_is_queued_with_no_attempts(db):
    project = await db_module.create_project(db, "exp-8")
    experiment = await db_module.create_experiment(db, project["id"])
    run = await db_module.create_run(db, project["id"], experiment["id"])
    fetched = await db_module.get_run(db, project["id"], run["id"])
    assert fetched["status"] == "queued"
    assert fetched["latest_attempt"] is None


@pytest.mark.asyncio
async def test_create_attempt_rejects_run_not_in_project(db):
    p1 = await db_module.create_project(db, "exp-9a")
    p2 = await db_module.create_project(db, "exp-9b")
    experiment = await db_module.create_experiment(db, p1["id"])
    run = await db_module.create_run(db, p1["id"], experiment["id"])
    with pytest.raises(ValueError, match="not found in project"):
        await db_module.create_attempt(db, p2["id"], run["id"])


@pytest.mark.asyncio
async def test_create_attempt_numbers_increment_and_dont_duplicate_the_run(db):
    """The other half of 'retries create distinct attempts without
    duplicating a run': multiple attempts, ONE run row throughout."""
    project = await db_module.create_project(db, "exp-10")
    experiment = await db_module.create_experiment(db, project["id"])
    run = await db_module.create_run(db, project["id"], experiment["id"])

    a1 = await db_module.create_attempt(db, project["id"], run["id"])
    a2 = await db_module.create_attempt(db, project["id"], run["id"])
    a3 = await db_module.create_attempt(db, project["id"], run["id"])
    assert [a1["attempt_number"], a2["attempt_number"], a3["attempt_number"]] == [1, 2, 3]
    assert len({a1["id"], a2["id"], a3["id"]}) == 3

    async with db.execute(
        "SELECT COUNT(*) AS n FROM research_runs WHERE id = ?", (run["id"],)
    ) as cur:
        row = await cur.fetchone()
    assert int(row["n"]) == 1

    reloaded = await db_module.get_run(db, project["id"], run["id"])
    assert reloaded["attempt_count"] == 3
    assert reloaded["latest_attempt"]["id"] == a3["id"]


@pytest.mark.asyncio
async def test_get_run_status_derives_from_latest_attempt(db):
    project = await db_module.create_project(db, "exp-11")
    experiment = await db_module.create_experiment(db, project["id"])
    run = await db_module.create_run(db, project["id"], experiment["id"])
    attempt = await db_module.create_attempt(db, project["id"], run["id"])

    await db_module.transition_attempt(db, project["id"], attempt["id"], "running")
    assert (await db_module.get_run(db, project["id"], run["id"]))["status"] == "running"

    await db_module.transition_attempt(db, project["id"], attempt["id"], "succeeded")
    assert (await db_module.get_run(db, project["id"], run["id"]))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_transition_attempt_same_status_is_idempotent(db):
    project = await db_module.create_project(db, "exp-12")
    experiment = await db_module.create_experiment(db, project["id"])
    run = await db_module.create_run(db, project["id"], experiment["id"])
    attempt = await db_module.create_attempt(db, project["id"], run["id"])
    once = await db_module.transition_attempt(db, project["id"], attempt["id"], "queued")
    twice = await db_module.transition_attempt(db, project["id"], attempt["id"], "queued")
    assert once["status"] == twice["status"] == "queued"


@pytest.mark.asyncio
async def test_transition_attempt_rejects_illegal_jump(db):
    project = await db_module.create_project(db, "exp-13")
    experiment = await db_module.create_experiment(db, project["id"])
    run = await db_module.create_run(db, project["id"], experiment["id"])
    attempt = await db_module.create_attempt(db, project["id"], run["id"])
    with pytest.raises(ValueError, match="illegal attempt transition"):
        await db_module.transition_attempt(db, project["id"], attempt["id"], "succeeded")


@pytest.mark.asyncio
async def test_transition_to_failed_requires_failure_class(db):
    project = await db_module.create_project(db, "exp-14")
    experiment = await db_module.create_experiment(db, project["id"])
    run = await db_module.create_run(db, project["id"], experiment["id"])
    attempt = await db_module.create_attempt(db, project["id"], run["id"])
    await db_module.transition_attempt(db, project["id"], attempt["id"], "running")
    with pytest.raises(ValueError, match="requires a failure_class"):
        await db_module.transition_attempt(db, project["id"], attempt["id"], "failed")


@pytest.mark.asyncio
async def test_failure_class_rejected_on_non_failure_transition(db):
    project = await db_module.create_project(db, "exp-15")
    experiment = await db_module.create_experiment(db, project["id"])
    run = await db_module.create_run(db, project["id"], experiment["id"])
    attempt = await db_module.create_attempt(db, project["id"], run["id"])
    with pytest.raises(ValueError, match="only valid when transitioning to"):
        await db_module.transition_attempt(
            db, project["id"], attempt["id"], "running", failure_class="timeout",
        )


@pytest.mark.asyncio
async def test_transition_to_failed_with_classification_stamps_ended_at(db):
    project = await db_module.create_project(db, "exp-16")
    experiment = await db_module.create_experiment(db, project["id"])
    run = await db_module.create_run(db, project["id"], experiment["id"])
    attempt = await db_module.create_attempt(db, project["id"], run["id"])
    await db_module.transition_attempt(db, project["id"], attempt["id"], "running")
    assert (await db_module.get_attempt(db, project["id"], attempt["id"]))["started_at"] is not None

    failed = await db_module.transition_attempt(
        db, project["id"], attempt["id"], "failed",
        failure_class="oom", error_message="CUDA out of memory",
    )
    assert failed["failure_class"] == "oom"
    assert failed["error_message"] == "CUDA out of memory"
    assert failed["ended_at"] is not None


@pytest.mark.asyncio
async def test_transition_to_failed_is_idempotent_without_repassing_failure_class(db):
    """A true no-op (failed -> failed) must never raise, even if the caller
    omits failure_class the second time — it reuses the stored classification."""
    project = await db_module.create_project(db, "exp-17b")
    experiment = await db_module.create_experiment(db, project["id"])
    run = await db_module.create_run(db, project["id"], experiment["id"])
    attempt = await db_module.create_attempt(db, project["id"], run["id"])
    await db_module.transition_attempt(db, project["id"], attempt["id"], "running")
    first = await db_module.transition_attempt(
        db, project["id"], attempt["id"], "failed", failure_class="timeout",
    )
    second = await db_module.transition_attempt(db, project["id"], attempt["id"], "failed")
    assert second["status"] == "failed"
    assert second["failure_class"] == "timeout" == first["failure_class"]


@pytest.mark.asyncio
async def test_transition_rejects_secret_looking_error_message(db):
    project = await db_module.create_project(db, "exp-17")
    experiment = await db_module.create_experiment(db, project["id"])
    run = await db_module.create_run(db, project["id"], experiment["id"])
    attempt = await db_module.create_attempt(db, project["id"], run["id"])
    await db_module.transition_attempt(db, project["id"], attempt["id"], "running")
    with pytest.raises(ValueError, match="Refusing to persist"):
        await db_module.transition_attempt(
            db, project["id"], attempt["id"], "failed", failure_class="infra_error",
            error_message="sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )


@pytest.mark.asyncio
async def test_transition_round_trips_checkpoint_and_artifact_and_provenance_refs(db):
    project = await db_module.create_project(db, "exp-18")
    experiment = await db_module.create_experiment(db, project["id"])
    run = await db_module.create_run(db, project["id"], experiment["id"])
    attempt = await db_module.create_attempt(db, project["id"], run["id"])
    await db_module.transition_attempt(db, project["id"], attempt["id"], "running")
    updated = await db_module.transition_attempt(
        db, project["id"], attempt["id"], "succeeded",
        checkpoint_ref={"path": "s3://bucket/ckpt-100.pt"},
        artifact_refs=[{"output_id": "out-1"}, {"output_id": "out-2"}],
        provenance_ref={"node_type": "run", "identity_key": attempt["id"]},
    )
    reloaded = await db_module.get_attempt(db, project["id"], attempt["id"])
    assert reloaded["checkpoint_ref"] == {"path": "s3://bucket/ckpt-100.pt"}
    assert reloaded["artifact_refs"] == [{"output_id": "out-1"}, {"output_id": "out-2"}]
    assert reloaded["provenance_ref"] == {"node_type": "run", "identity_key": attempt["id"]}
    assert updated["status"] == "succeeded"


@pytest.mark.asyncio
async def test_heartbeat_attempt_updates_timestamp(db):
    project = await db_module.create_project(db, "exp-19")
    experiment = await db_module.create_experiment(db, project["id"])
    run = await db_module.create_run(db, project["id"], experiment["id"])
    attempt = await db_module.create_attempt(db, project["id"], run["id"])
    assert attempt["last_heartbeat_at"] is None
    beat = await db_module.heartbeat_attempt(db, project["id"], attempt["id"])
    assert beat["last_heartbeat_at"] is not None


@pytest.mark.asyncio
async def test_heartbeat_rejects_terminal_attempt(db):
    project = await db_module.create_project(db, "exp-20")
    experiment = await db_module.create_experiment(db, project["id"])
    run = await db_module.create_run(db, project["id"], experiment["id"])
    attempt = await db_module.create_attempt(db, project["id"], run["id"])
    await db_module.transition_attempt(db, project["id"], attempt["id"], "running")
    await db_module.transition_attempt(db, project["id"], attempt["id"], "cancelled")
    with pytest.raises(ValueError, match="already terminal"):
        await db_module.heartbeat_attempt(db, project["id"], attempt["id"])


@pytest.mark.asyncio
async def test_reconcile_stale_attempts_marks_stale_running_as_unknown(db):
    project = await db_module.create_project(db, "exp-21")
    experiment = await db_module.create_experiment(db, project["id"])
    run = await db_module.create_run(db, project["id"], experiment["id"])
    attempt = await db_module.create_attempt(db, project["id"], run["id"])
    await db_module.transition_attempt(db, project["id"], attempt["id"], "running")

    # Backdate started_at (and clear any heartbeat) to simulate a process
    # that died 20 minutes ago without ever heartbeating.
    stale_time = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE research_run_attempts SET started_at = ?, last_heartbeat_at = NULL WHERE id = ?",
        (stale_time, attempt["id"]),
    )
    await db.commit()

    reconciled = await db_module.reconcile_stale_attempts(db, project["id"], stale_after_seconds=900)
    assert len(reconciled) == 1
    assert reconciled[0]["id"] == attempt["id"]
    assert reconciled[0]["status"] == "unknown"

    run_after = await db_module.get_run(db, project["id"], run["id"])
    assert run_after["status"] == "unknown"


@pytest.mark.asyncio
async def test_reconcile_stale_attempts_leaves_fresh_and_terminal_attempts_alone(db):
    project = await db_module.create_project(db, "exp-22")
    experiment = await db_module.create_experiment(db, project["id"])

    fresh_run = await db_module.create_run(db, project["id"], experiment["id"])
    fresh_attempt = await db_module.create_attempt(db, project["id"], fresh_run["id"])
    await db_module.transition_attempt(db, project["id"], fresh_attempt["id"], "running")
    await db_module.heartbeat_attempt(db, project["id"], fresh_attempt["id"])

    done_run = await db_module.create_run(db, project["id"], experiment["id"])
    done_attempt = await db_module.create_attempt(db, project["id"], done_run["id"])
    await db_module.transition_attempt(db, project["id"], done_attempt["id"], "running")
    await db_module.transition_attempt(db, project["id"], done_attempt["id"], "succeeded")
    old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE research_run_attempts SET created_at = ?, started_at = ?, ended_at = ? WHERE id = ?",
        (old_time, old_time, old_time, done_attempt["id"]),
    )
    await db.commit()

    reconciled = await db_module.reconcile_stale_attempts(db, project["id"], stale_after_seconds=900)
    assert reconciled == []
    assert (await db_module.get_attempt(db, project["id"], fresh_attempt["id"]))["status"] == "running"
    assert (await db_module.get_attempt(db, project["id"], done_attempt["id"]))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_reconciled_unknown_attempt_can_be_resolved_to_real_outcome(db):
    """Restart recovery isn't a dead end: an 'unknown' attempt can later be
    reconciled to what actually happened."""
    project = await db_module.create_project(db, "exp-23")
    experiment = await db_module.create_experiment(db, project["id"])
    run = await db_module.create_run(db, project["id"], experiment["id"])
    attempt = await db_module.create_attempt(db, project["id"], run["id"])
    await db_module.transition_attempt(db, project["id"], attempt["id"], "running")

    stale_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE research_run_attempts SET started_at = ? WHERE id = ?", (stale_time, attempt["id"]),
    )
    await db.commit()
    await db_module.reconcile_stale_attempts(db, project["id"], stale_after_seconds=900)

    resolved = await db_module.transition_attempt(db, project["id"], attempt["id"], "succeeded")
    assert resolved["status"] == "succeeded"
