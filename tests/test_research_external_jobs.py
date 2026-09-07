"""Tests for sprint item 0b7bb873 — the delegated/observed external-job
escape hatch: meridian.research.external_jobs (ExternalJobRef/
ExternalRunReceipt, attach_external_job/mark_external_status/
import_external_manifest) and meridian.research.providers.delegated
(DelegatedProvider).

Focused, serial (no xdist) per this item's required_tool note. Covers the
acceptance-criterion scenarios explicitly: malformed receipts, duplicate
registration, unknown status, preemption, partial outputs, and manual
finalization.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian.research import external_jobs as ext
from meridian.research.providers.base import JobHandle, JobStatus, UnsupportedOperation
from meridian.research.providers.delegated import DelegatedProvider


async def _setup_queued_attempt(db, project_prefix):
    project = await db_module.create_project(db, project_prefix)
    experiment = await db_module.create_experiment(db, project["id"])
    run = await db_module.create_run(db, project["id"], experiment["id"])
    attempt = await db_module.create_attempt(db, project["id"], run["id"])
    return project, experiment, run, attempt


def _ref(**overrides):
    kwargs = dict(launcher="sbatch", external_id="slurm-123", idempotency_key="ext-1")
    kwargs.update(overrides)
    return ext.ExternalJobRef(**kwargs)


# ---------------------------------------------------------------------------
# Pure dataclasses — TaskTopology / ExternalJobRef / ExternalRunReceipt.
# ---------------------------------------------------------------------------


def test_validate_launcher_kind_accepts_all_and_rejects_unknown():
    for kind in ext.LAUNCHER_KINDS:
        assert ext.validate_launcher_kind(kind) == kind
    with pytest.raises(ValueError, match="launcher must be one of"):
        ext.validate_launcher_kind("bogus")


def test_task_topology_rejects_node_count_below_1():
    with pytest.raises(ValueError, match="node_count must be >= 1"):
        ext.TaskTopology(node_count=0)


def test_task_topology_requires_array_index_and_size_together():
    with pytest.raises(ValueError, match="array_index and array_size must be set together"):
        ext.TaskTopology(array_index=2)
    with pytest.raises(ValueError, match="array_index and array_size must be set together"):
        ext.TaskTopology(array_size=10)
    ext.TaskTopology(array_index=2, array_size=10)  # does not raise


def test_external_job_ref_requires_external_id():
    with pytest.raises(ValueError, match="non-empty external_id"):
        ext.ExternalJobRef(launcher="sbatch", external_id="", idempotency_key="k")


def test_external_job_ref_requires_idempotency_key():
    with pytest.raises(ValueError, match="non-empty idempotency_key"):
        ext.ExternalJobRef(launcher="sbatch", external_id="x", idempotency_key="")


def test_external_job_ref_rejects_invalid_launcher():
    with pytest.raises(ValueError, match="launcher must be one of"):
        ext.ExternalJobRef(launcher="bogus", external_id="x", idempotency_key="k")


def test_external_job_ref_rejects_secret_looking_launcher_reference():
    """Malformed receipt scenario: a launcher_reference embedding a secret
    must be rejected at construction, never persisted."""
    with pytest.raises(ValueError, match="Refusing to persist"):
        ext.ExternalJobRef(
            launcher="ssh", external_id="pid-1", idempotency_key="k",
            launcher_reference="ssh user@host 'export TOKEN=sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'",
        )


def test_external_job_ref_to_json_round_trips_topology():
    ref = _ref(topology=ext.TaskTopology(node_count=4, node_rank=2, array_index=1, array_size=8, parent_launcher_id="parent-1"))
    j = ref.to_json()
    assert j["topology"] == {
        "node_count": 4, "node_rank": 2, "array_size": 8, "array_index": 1, "parent_launcher_id": "parent-1",
    }
    assert j["launcher"] == "sbatch"
    assert j["external_id"] == "slurm-123"


def test_external_run_receipt_rejects_non_terminal_status():
    """Malformed receipt: 'running' is not something you import after the fact."""
    with pytest.raises(ValueError, match="must be terminal"):
        ext.ExternalRunReceipt(status="running")
    with pytest.raises(ValueError, match="must be terminal"):
        ext.ExternalRunReceipt(status="queued")


def test_external_run_receipt_accepts_unknown_status():
    receipt = ext.ExternalRunReceipt(status="unknown")
    assert receipt.status == "unknown"


def test_external_run_receipt_requires_failure_class_when_failed():
    with pytest.raises(ValueError, match="requires a failure_class"):
        ext.ExternalRunReceipt(status="failed")


def test_external_run_receipt_rejects_failure_class_when_not_failed():
    with pytest.raises(ValueError, match="only valid for status"):
        ext.ExternalRunReceipt(status="succeeded", failure_class="timeout")


def test_external_run_receipt_rejects_secret_looking_detail():
    with pytest.raises(ValueError, match="Refusing to persist"):
        ext.ExternalRunReceipt(
            status="succeeded",
            detail="uploaded with key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )


# ---------------------------------------------------------------------------
# attach_external_job — delegated mode, on the `db` fixture.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_external_job_transitions_queued_to_running(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "extj-1")
    updated = await ext.attach_external_job(db, attempt["project_id"], attempt["id"], _ref())
    assert updated["status"] == "running"
    assert updated["provenance_ref"]["external_job"]["launcher"] == "sbatch"
    assert updated["provenance_ref"]["external_job"]["external_id"] == "slurm-123"


@pytest.mark.asyncio
async def test_attach_external_job_is_idempotent_same_key(db):
    """Duplicate registration must not duplicate the run."""
    _, _, _, attempt = await _setup_queued_attempt(db, "extj-2")
    ref = _ref(idempotency_key="dup-key")
    first = await ext.attach_external_job(db, attempt["project_id"], attempt["id"], ref)
    second = await ext.attach_external_job(db, attempt["project_id"], attempt["id"], ref)
    assert first == second
    async with db.execute("SELECT COUNT(*) AS n FROM research_run_attempts WHERE run_id = ?", (attempt["run_id"],)) as cur:
        row = await cur.fetchone()
    assert int(row["n"]) == 1


@pytest.mark.asyncio
async def test_attach_external_job_rejects_different_key_while_running(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "extj-3")
    await ext.attach_external_job(db, attempt["project_id"], attempt["id"], _ref(idempotency_key="k1"))
    with pytest.raises(ValueError, match="already 'running'"):
        await ext.attach_external_job(db, attempt["project_id"], attempt["id"], _ref(idempotency_key="k2"))


@pytest.mark.asyncio
async def test_attach_external_job_rejects_when_not_queued(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "extj-4")
    await ext.attach_external_job(db, attempt["project_id"], attempt["id"], _ref())
    await ext.mark_external_status(db, attempt["project_id"], attempt["id"], "succeeded")
    with pytest.raises(ValueError, match="must be 'queued'"):
        await ext.attach_external_job(db, attempt["project_id"], attempt["id"], _ref(idempotency_key="other"))


@pytest.mark.asyncio
async def test_attach_external_job_rejects_cross_project(db):
    p1 = await db_module.create_project(db, "extj-5a")
    p2 = await db_module.create_project(db, "extj-5b")
    experiment = await db_module.create_experiment(db, p1["id"])
    run = await db_module.create_run(db, p1["id"], experiment["id"])
    attempt = await db_module.create_attempt(db, p1["id"], run["id"])
    with pytest.raises(ValueError, match="not found in project"):
        await ext.attach_external_job(db, p2["id"], attempt["id"], _ref())


# ---------------------------------------------------------------------------
# mark_external_status — polling / manual correction, preemption/requeue.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_external_status_requires_prior_attach(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "extj-6")
    with pytest.raises(ValueError, match="has no attached external job"):
        await ext.mark_external_status(db, attempt["project_id"], attempt["id"], "running")


@pytest.mark.asyncio
async def test_mark_external_status_updates_to_unknown(db):
    """Unknown-status scenario."""
    _, _, _, attempt = await _setup_queued_attempt(db, "extj-7")
    await ext.attach_external_job(db, attempt["project_id"], attempt["id"], _ref())
    updated = await ext.mark_external_status(db, attempt["project_id"], attempt["id"], "unknown")
    assert updated["status"] == "unknown"


@pytest.mark.asyncio
async def test_mark_external_status_preemption_then_requeue(db):
    """Preemption-then-requeue scenario: while the external scheduler is
    expected to requeue the SAME job, its true state is genuinely uncertain
    from Meridian's side — 'unknown', not the terminal 'crashed' (which
    only accepts failure_class='preempted' for a preemption the scheduler
    will NOT retry; see test_mark_external_status_terminal_preemption).
    'unknown' is the one non-terminal status that accepts a transition back
    to 'running' once the scheduler's own requeue resumes it — no new
    Meridian attempt is created."""
    _, _, _, attempt = await _setup_queued_attempt(db, "extj-8")
    await ext.attach_external_job(db, attempt["project_id"], attempt["id"], _ref())
    preempted = await ext.mark_external_status(
        db, attempt["project_id"], attempt["id"], "unknown",
        detail="preempted by external scheduler, awaiting requeue",
    )
    assert preempted["status"] == "unknown"

    resumed = await ext.mark_external_status(db, attempt["project_id"], attempt["id"], "running")
    assert resumed["status"] == "running"
    assert resumed["id"] == attempt["id"]  # same attempt, not a new one

    async with db.execute("SELECT COUNT(*) AS n FROM research_run_attempts WHERE run_id = ?", (attempt["run_id"],)) as cur:
        row = await cur.fetchone()
    assert int(row["n"]) == 1


@pytest.mark.asyncio
async def test_mark_external_status_terminal_preemption(db):
    """A preemption the external scheduler will NOT retry is a genuinely
    terminal 'crashed' outcome with failure_class='preempted' — unlike the
    requeue case above, there is no further transition expected."""
    _, _, _, attempt = await _setup_queued_attempt(db, "extj-8b")
    await ext.attach_external_job(db, attempt["project_id"], attempt["id"], _ref())
    terminal = await ext.mark_external_status(
        db, attempt["project_id"], attempt["id"], "crashed", failure_class="preempted",
    )
    assert terminal["status"] == "crashed"
    assert terminal["failure_class"] == "preempted"


@pytest.mark.asyncio
async def test_mark_external_status_rejects_illegal_transition(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "extj-9")
    await ext.attach_external_job(db, attempt["project_id"], attempt["id"], _ref())
    await ext.mark_external_status(db, attempt["project_id"], attempt["id"], "succeeded")
    with pytest.raises(ValueError, match="illegal attempt transition"):
        await ext.mark_external_status(db, attempt["project_id"], attempt["id"], "running")


# ---------------------------------------------------------------------------
# import_external_manifest — observed/manual mode.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_external_manifest_manual_finalization_succeeded(db):
    """Manual finalization scenario."""
    _, _, _, attempt = await _setup_queued_attempt(db, "extj-10")
    receipt = ext.ExternalRunReceipt(
        status="succeeded", output_refs=({"path": "s3://bucket/out.pt"},), finalized_by="researcher-adam",
    )
    finalized = await ext.import_external_manifest(db, attempt["project_id"], attempt["id"], receipt)
    assert finalized["status"] == "succeeded"
    assert finalized["artifact_refs"] == [{"path": "s3://bucket/out.pt"}]
    assert finalized["provenance_ref"]["external_manifest"]["finalized_by"] == "researcher-adam"


@pytest.mark.asyncio
async def test_import_external_manifest_partial_outputs_with_unknown_status(db):
    """Partial-outputs scenario: output_refs present, but status is
    explicitly 'unknown' — presence of files must never be read as success."""
    _, _, _, attempt = await _setup_queued_attempt(db, "extj-11")
    receipt = ext.ExternalRunReceipt(status="unknown", output_refs=({"path": "s3://bucket/partial.ckpt"},))
    finalized = await ext.import_external_manifest(db, attempt["project_id"], attempt["id"], receipt)
    assert finalized["status"] == "unknown"
    assert finalized["artifact_refs"] == [{"path": "s3://bucket/partial.ckpt"}]


@pytest.mark.asyncio
async def test_import_external_manifest_with_failure_and_classification(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "extj-12")
    receipt = ext.ExternalRunReceipt(status="failed", failure_class="oom", detail="killed by OOM killer")
    finalized = await ext.import_external_manifest(db, attempt["project_id"], attempt["id"], receipt)
    assert finalized["status"] == "failed"
    assert finalized["failure_class"] == "oom"
    assert finalized["error_message"] == "killed by OOM killer"


@pytest.mark.asyncio
async def test_import_external_manifest_rejects_when_already_terminal(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "extj-13")
    await ext.import_external_manifest(db, attempt["project_id"], attempt["id"], ext.ExternalRunReceipt(status="succeeded"))
    with pytest.raises(ValueError, match="already terminal"):
        await ext.import_external_manifest(db, attempt["project_id"], attempt["id"], ext.ExternalRunReceipt(status="succeeded"))


@pytest.mark.asyncio
async def test_import_external_manifest_from_queued_passes_through_running(db):
    """An observed job that was never attached (queued -> terminal
    directly) is routed through 'running' first, since that transition
    isn't legal directly from 'queued'."""
    _, _, _, attempt = await _setup_queued_attempt(db, "extj-14")
    assert attempt["status"] == "queued"
    finalized = await ext.import_external_manifest(db, attempt["project_id"], attempt["id"], ext.ExternalRunReceipt(status="succeeded"))
    assert finalized["status"] == "succeeded"


# ---------------------------------------------------------------------------
# Array / multi-node topology preserved across independent attempts.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_array_job_topology_preserved_across_independent_attempts(db):
    project = await db_module.create_project(db, "extj-15")
    experiment = await db_module.create_experiment(db, project["id"])

    task_refs = []
    for i in range(3):
        run = await db_module.create_run(db, project["id"], experiment["id"])
        attempt = await db_module.create_attempt(db, project["id"], run["id"])
        ref = ext.ExternalJobRef(
            launcher="sbatch", external_id=f"slurm-array-{i}", idempotency_key=f"array-key-{i}",
            topology=ext.TaskTopology(array_index=i, array_size=3, parent_launcher_id="slurm-array-parent"),
        )
        updated = await ext.attach_external_job(db, project["id"], attempt["id"], ref)
        task_refs.append(updated)

    for i, updated in enumerate(task_refs):
        topo = updated["provenance_ref"]["external_job"]["topology"]
        assert topo["array_index"] == i
        assert topo["array_size"] == 3
        assert topo["parent_launcher_id"] == "slurm-array-parent"


@pytest.mark.asyncio
async def test_multi_node_topology_preserves_node_rank(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "extj-16")
    ref = _ref(topology=ext.TaskTopology(node_count=8, node_rank=3))
    updated = await ext.attach_external_job(db, attempt["project_id"], attempt["id"], ref)
    topo = updated["provenance_ref"]["external_job"]["topology"]
    assert topo["node_count"] == 8
    assert topo["node_rank"] == 3


# ---------------------------------------------------------------------------
# DelegatedProvider — the JobProvider-conformant wrapper.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegated_provider_submit_always_raises():
    provider = DelegatedProvider()
    with pytest.raises(UnsupportedOperation, match="submit"):
        await provider.submit(None)


@pytest.mark.asyncio
async def test_delegated_provider_status_without_poller_returns_unknown():
    provider = DelegatedProvider()
    handle = JobHandle(provider="delegated", external_id="x", idempotency_key="k", submitted_at="now")
    status = await provider.status(handle)
    assert status.state == "unknown"


@pytest.mark.asyncio
async def test_delegated_provider_status_with_poller_returns_polled_result():
    async def poller(handle):
        return JobStatus(state="running")

    provider = DelegatedProvider(status_poller=poller)
    handle = JobHandle(provider="delegated", external_id="x", idempotency_key="k", submitted_at="now")
    status = await provider.status(handle)
    assert status.state == "running"


@pytest.mark.asyncio
async def test_delegated_provider_status_poller_exception_is_redacted():
    async def poller(handle):
        raise RuntimeError("failed with token sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

    provider = DelegatedProvider(status_poller=poller)
    handle = JobHandle(provider="delegated", external_id="x", idempotency_key="k", submitted_at="now")
    status = await provider.status(handle)
    assert status.state == "unknown"
    assert "sk-ant-api03" not in (status.detail or "")


@pytest.mark.asyncio
async def test_delegated_provider_cancel_without_fn_raises_unsupported():
    provider = DelegatedProvider()
    handle = JobHandle(provider="delegated", external_id="x", idempotency_key="k", submitted_at="now")
    with pytest.raises(UnsupportedOperation):
        await provider.cancel(handle)


@pytest.mark.asyncio
async def test_delegated_provider_cancel_with_fn_returns_result():
    async def cancel_fn(handle):
        return JobStatus(state="cancelled")

    provider = DelegatedProvider(cancel_fn=cancel_fn)
    handle = JobHandle(provider="delegated", external_id="x", idempotency_key="k", submitted_at="now")
    status = await provider.cancel(handle)
    assert status.state == "cancelled"


@pytest.mark.asyncio
async def test_delegated_provider_fetch_logs_without_fn_raises_unsupported():
    provider = DelegatedProvider()
    handle = JobHandle(provider="delegated", external_id="x", idempotency_key="k", submitted_at="now")
    with pytest.raises(UnsupportedOperation):
        await provider.fetch_logs(handle)


def test_delegated_provider_capabilities_reflect_injected_fns():
    bare = DelegatedProvider().capabilities()
    assert bare.can_cancel is False
    assert bare.can_stream_logs is False

    async def _noop(handle):
        return None

    wired = DelegatedProvider(cancel_fn=_noop, logs_fn=_noop).capabilities()
    assert wired.can_cancel is True
    assert wired.can_stream_logs is True
