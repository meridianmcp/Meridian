"""Tests for sprint item f6627d83 — the provider-neutral research job
scheduler: JobSpec/JobHandle/JobStatus/ProviderCapabilities
(meridian.research.providers.base), the deterministic local provider
(meridian.research.providers.local), and the scheduler orchestration layer
(meridian.research.scheduler) that binds a provider to an existing
meridian.db.experiment_model RunAttempt.

Focused, serial (no xdist) per this item's required_tool note.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian.research import scheduler as sched
from meridian.research.providers.base import (
    JobHandle,
    JobProvider,
    JobSpec,
    JobStatus,
    ProviderCapabilities,
    ResourceRequest,
    UnsupportedOperation,
)
from meridian.research.providers.local import LocalProvider


def _spec(attempt, *, idempotency_key="job-1", **overrides):
    kwargs = dict(
        project_id=attempt["project_id"],
        experiment_id="unused",  # not read by the provider layer; only identity fields matter here
        run_id=attempt["run_id"],
        attempt_id=attempt["id"],
        idempotency_key=idempotency_key,
        command=("echo", "hello"),
    )
    kwargs.update(overrides)
    return JobSpec(**kwargs)


async def _setup_queued_attempt(db, project_prefix):
    project = await db_module.create_project(db, project_prefix)
    experiment = await db_module.create_experiment(db, project["id"])
    run = await db_module.create_run(db, project["id"], experiment["id"])
    attempt = await db_module.create_attempt(db, project["id"], run["id"])
    return project, experiment, run, attempt


class _NoCancelProvider(JobProvider):
    name = "no-cancel"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()  # everything False

    async def submit(self, spec: JobSpec) -> JobHandle:
        return JobHandle(provider=self.name, external_id="x", idempotency_key=spec.idempotency_key, submitted_at="2026-01-01 00:00:00")

    async def status(self, handle: JobHandle) -> JobStatus:
        return JobStatus(state="running")


class _FlakySubmitProvider(LocalProvider):
    """A LocalProvider that raises on the NEXT submit() call, then behaves
    normally — simulates a submit timeout for retry-safety tests."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next = False

    async def submit(self, spec: JobSpec) -> JobHandle:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated submit timeout")
        return await super().submit(spec)


# ---------------------------------------------------------------------------
# meridian.research.providers.base — pure contract shapes.
# ---------------------------------------------------------------------------


def test_jobspec_requires_idempotency_key():
    with pytest.raises(ValueError, match="non-empty idempotency_key"):
        JobSpec(
            project_id="p", experiment_id="e", run_id="r", attempt_id="a",
            idempotency_key="", command=("echo",),
        )


def test_jobspec_requires_command():
    with pytest.raises(ValueError, match="non-empty command"):
        JobSpec(
            project_id="p", experiment_id="e", run_id="r", attempt_id="a",
            idempotency_key="k", command=(),
        )


def test_jobstatus_rejects_invalid_state():
    with pytest.raises(ValueError, match="JobStatus.state must be one of"):
        JobStatus(state="bogus")


def test_jobstatus_rejects_invalid_failure_class():
    with pytest.raises(ValueError, match="JobStatus.failure_class must be one of"):
        JobStatus(state="failed", failure_class="bogus")


def test_jobstatus_allows_none_failure_class_on_failure():
    status = JobStatus(state="failed")
    assert status.failure_class is None


def test_provider_capabilities_defaults_all_false():
    caps = ProviderCapabilities()
    assert caps.can_cancel is False
    assert caps.can_stream_logs is False
    assert caps.can_fetch_artifacts is False
    assert caps.supports_gpu is False


@pytest.mark.asyncio
async def test_base_cancel_raises_unsupported_when_capability_false():
    provider = _NoCancelProvider()
    with pytest.raises(UnsupportedOperation, match="does not support 'cancel'"):
        await provider.cancel(JobHandle(provider="x", external_id="1", idempotency_key="k", submitted_at="now"))


# ---------------------------------------------------------------------------
# LocalProvider — deterministic dry-run.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_provider_capabilities():
    caps = LocalProvider().capabilities()
    assert caps.can_cancel is True
    assert caps.supports_gpu is False


@pytest.mark.asyncio
async def test_local_submit_is_idempotent_same_key_returns_same_handle():
    provider = LocalProvider()
    spec = JobSpec(project_id="p", experiment_id="e", run_id="r", attempt_id="a", idempotency_key="k1", command=("echo",))
    first = await provider.submit(spec)
    second = await provider.submit(spec)
    assert first == second


@pytest.mark.asyncio
async def test_local_submit_rejects_invalid_simulate():
    provider = LocalProvider()
    spec = JobSpec(
        project_id="p", experiment_id="e", run_id="r", attempt_id="a", idempotency_key="k2",
        command=("echo",), provider_config={"simulate": "bogus"},
    )
    with pytest.raises(ValueError, match="simulate"):
        await provider.submit(spec)


@pytest.mark.asyncio
async def test_local_status_unknown_for_unrecorded_handle():
    provider = LocalProvider()
    handle = JobHandle(provider="local", external_id="ghost", idempotency_key="never-submitted", submitted_at="now")
    status = await provider.status(handle)
    assert status.state == "unknown"


@pytest.mark.asyncio
async def test_local_default_succeeds_after_first_poll():
    provider = LocalProvider()
    spec = JobSpec(project_id="p", experiment_id="e", run_id="r", attempt_id="a", idempotency_key="k3", command=("echo",))
    handle = await provider.submit(spec)
    assert (await provider.status(handle)).state == "running"
    final = await provider.status(handle)
    assert final.state == "succeeded"
    assert final.output_refs


@pytest.mark.asyncio
async def test_local_fail_simulation():
    provider = LocalProvider()
    spec = JobSpec(
        project_id="p", experiment_id="e", run_id="r", attempt_id="a", idempotency_key="k4",
        command=("echo",), provider_config={"simulate": "fail"},
    )
    handle = await provider.submit(spec)
    await provider.status(handle)
    final = await provider.status(handle)
    assert final.state == "failed"
    assert final.failure_class == "user_error"


@pytest.mark.asyncio
async def test_local_crash_simulation():
    provider = LocalProvider()
    spec = JobSpec(
        project_id="p", experiment_id="e", run_id="r", attempt_id="a", idempotency_key="k5",
        command=("echo",), provider_config={"simulate": "crash"},
    )
    handle = await provider.submit(spec)
    await provider.status(handle)
    final = await provider.status(handle)
    assert final.state == "crashed"
    assert final.failure_class == "infra_error"


@pytest.mark.asyncio
async def test_local_hang_stays_running_forever():
    provider = LocalProvider()
    spec = JobSpec(
        project_id="p", experiment_id="e", run_id="r", attempt_id="a", idempotency_key="k6",
        command=("echo",), provider_config={"simulate": "hang"},
    )
    handle = await provider.submit(spec)
    for _ in range(5):
        assert (await provider.status(handle)).state == "running"


@pytest.mark.asyncio
async def test_local_cancel_transitions_to_cancelled():
    provider = LocalProvider()
    spec = JobSpec(
        project_id="p", experiment_id="e", run_id="r", attempt_id="a", idempotency_key="k7",
        command=("echo",), provider_config={"simulate": "hang"},
    )
    handle = await provider.submit(spec)
    cancelled = await provider.cancel(handle)
    assert cancelled.state == "cancelled"
    assert (await provider.status(handle)).state == "cancelled"


@pytest.mark.asyncio
async def test_local_fetch_artifacts_only_after_success():
    provider = LocalProvider()
    spec = JobSpec(project_id="p", experiment_id="e", run_id="r", attempt_id="a", idempotency_key="k8", command=("echo",))
    handle = await provider.submit(spec)
    assert await provider.fetch_artifacts(handle) == ()
    await provider.status(handle)
    await provider.status(handle)
    assert await provider.fetch_artifacts(handle) != ()


# ---------------------------------------------------------------------------
# meridian.research.scheduler — orchestration on the `db` fixture.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_run_attempt_transitions_queued_to_running_and_records_handle(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "sched-1")
    provider = LocalProvider()
    spec = _spec(attempt)
    updated = await sched.submit_run_attempt(db, attempt["project_id"], attempt["id"], provider, spec)
    assert updated["status"] == "running"
    assert updated["provenance_ref"]["job_handle"]["provider"] == "local"


@pytest.mark.asyncio
async def test_submit_run_attempt_is_idempotent_same_key(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "sched-2")
    provider = LocalProvider()
    spec = _spec(attempt, idempotency_key="same-key")
    first = await sched.submit_run_attempt(db, attempt["project_id"], attempt["id"], provider, spec)
    second = await sched.submit_run_attempt(db, attempt["project_id"], attempt["id"], provider, spec)
    assert first == second


@pytest.mark.asyncio
async def test_submit_run_attempt_rejects_different_key_while_running(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "sched-3")
    provider = LocalProvider()
    await sched.submit_run_attempt(db, attempt["project_id"], attempt["id"], provider, _spec(attempt, idempotency_key="k1"))
    with pytest.raises(ValueError, match="already 'running'"):
        await sched.submit_run_attempt(
            db, attempt["project_id"], attempt["id"], provider, _spec(attempt, idempotency_key="k2"),
        )


@pytest.mark.asyncio
async def test_submit_run_attempt_rejects_gpu_when_unsupported(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "sched-4")
    provider = LocalProvider()  # supports_gpu=False
    spec = _spec(attempt, resources=ResourceRequest(gpu_count=1))
    with pytest.raises(ValueError, match="does not support GPU"):
        await sched.submit_run_attempt(db, attempt["project_id"], attempt["id"], provider, spec)
    reloaded = await db_module.get_attempt(db, attempt["project_id"], attempt["id"])
    assert reloaded["status"] == "queued"  # rejected before touching the attempt


@pytest.mark.asyncio
async def test_submit_run_attempt_enforces_budget(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "sched-5")
    provider = LocalProvider()
    spec = _spec(attempt, budget_usd=10.0)
    with pytest.raises(sched.BudgetExceeded):
        await sched.submit_run_attempt(db, attempt["project_id"], attempt["id"], provider, spec, spent_usd=10.0)


@pytest.mark.asyncio
async def test_submit_run_attempt_leaves_attempt_queued_on_provider_error_and_retry_succeeds(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "sched-6")
    provider = _FlakySubmitProvider()
    provider.fail_next = True
    spec = _spec(attempt, idempotency_key="retry-key")

    with pytest.raises(RuntimeError, match="simulated submit timeout"):
        await sched.submit_run_attempt(db, attempt["project_id"], attempt["id"], provider, spec)

    reloaded = await db_module.get_attempt(db, attempt["project_id"], attempt["id"])
    assert reloaded["status"] == "queued"  # untouched by the failed submit

    # Retrying with the SAME idempotency_key now succeeds.
    updated = await sched.submit_run_attempt(db, attempt["project_id"], attempt["id"], provider, spec)
    assert updated["status"] == "running"


@pytest.mark.asyncio
async def test_poll_run_attempt_transitions_to_succeeded_and_records_artifact_refs(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "sched-7")
    provider = LocalProvider()
    await sched.submit_run_attempt(db, attempt["project_id"], attempt["id"], provider, _spec(attempt))
    await sched.poll_run_attempt(db, attempt["project_id"], attempt["id"], provider)  # still running
    final = await sched.poll_run_attempt(db, attempt["project_id"], attempt["id"], provider)
    assert final["status"] == "succeeded"
    assert final["artifact_refs"]


@pytest.mark.asyncio
async def test_poll_run_attempt_defaults_unclassified_failure_to_unknown(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "sched-8")

    class _UnclassifiedFailureProvider(LocalProvider):
        async def status(self, handle):
            return JobStatus(state="failed")  # no failure_class supplied

    provider = _UnclassifiedFailureProvider()
    await sched.submit_run_attempt(db, attempt["project_id"], attempt["id"], provider, _spec(attempt))
    final = await sched.poll_run_attempt(db, attempt["project_id"], attempt["id"], provider)
    assert final["status"] == "failed"
    assert final["failure_class"] == "unknown"


@pytest.mark.asyncio
async def test_poll_run_attempt_is_noop_on_illegal_transition(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "sched-9")
    provider = LocalProvider()
    await sched.submit_run_attempt(db, attempt["project_id"], attempt["id"], provider, _spec(attempt))
    await sched.poll_run_attempt(db, attempt["project_id"], attempt["id"], provider)
    succeeded = await sched.poll_run_attempt(db, attempt["project_id"], attempt["id"], provider)
    assert succeeded["status"] == "succeeded"

    class _StaleProvider(LocalProvider):
        async def status(self, handle):
            return JobStatus(state="running")  # stale — attempt is already terminal

    stale_provider = _StaleProvider()
    unchanged = await sched.poll_run_attempt(db, attempt["project_id"], attempt["id"], stale_provider)
    assert unchanged["status"] == "succeeded"  # unchanged, not an error


@pytest.mark.asyncio
async def test_cancel_run_attempt_transitions_to_cancelled(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "sched-10")
    provider = LocalProvider()
    spec = _spec(attempt, provider_config={"simulate": "hang"})
    await sched.submit_run_attempt(db, attempt["project_id"], attempt["id"], provider, spec)
    cancelled = await sched.cancel_run_attempt(db, attempt["project_id"], attempt["id"], provider)
    assert cancelled["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_run_attempt_rejects_already_terminal_attempt_without_calling_provider(db):
    """A cancel request against an attempt Meridian already knows finished
    must be rejected BEFORE any live provider.cancel() call — never issue a
    real stop/terminate call against a job that's already done."""
    _, _, _, attempt = await _setup_queued_attempt(db, "sched-11b")

    class _CancelTrackingProvider(LocalProvider):
        def __init__(self):
            super().__init__()
            self.cancel_calls = 0

        async def cancel(self, handle):
            self.cancel_calls += 1
            return await super().cancel(handle)

    provider = _CancelTrackingProvider()
    await sched.submit_run_attempt(db, attempt["project_id"], attempt["id"], provider, _spec(attempt))
    await sched.poll_run_attempt(db, attempt["project_id"], attempt["id"], provider)
    succeeded = await sched.poll_run_attempt(db, attempt["project_id"], attempt["id"], provider)
    assert succeeded["status"] == "succeeded"

    with pytest.raises(ValueError, match="already terminal"):
        await sched.cancel_run_attempt(db, attempt["project_id"], attempt["id"], provider)
    assert provider.cancel_calls == 0

    reloaded = await db_module.get_attempt(db, attempt["project_id"], attempt["id"])
    assert reloaded["status"] == "succeeded"  # untouched


@pytest.mark.asyncio
async def test_cancel_run_attempt_raises_when_unsupported(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "sched-11")
    provider = _NoCancelProvider()
    await sched.submit_run_attempt(db, attempt["project_id"], attempt["id"], provider, _spec(attempt))
    with pytest.raises(UnsupportedOperation):
        await sched.cancel_run_attempt(db, attempt["project_id"], attempt["id"], provider)


@pytest.mark.asyncio
async def test_check_attempt_timeout_transitions_to_unknown_when_exceeded(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "sched-12")
    provider = LocalProvider()
    spec = _spec(attempt, provider_config={"simulate": "hang"})
    await sched.submit_run_attempt(db, attempt["project_id"], attempt["id"], provider, spec)

    stale_time = "2000-01-01 00:00:00"
    await db.execute(
        "UPDATE research_run_attempts SET started_at = ? WHERE id = ?", (stale_time, attempt["id"]),
    )
    await db.commit()

    timed_out = await sched.check_attempt_timeout(
        db, attempt["project_id"], attempt["id"], timeout_seconds=60, provider=provider,
    )
    assert timed_out["status"] == "unknown"


@pytest.mark.asyncio
async def test_check_attempt_timeout_noop_when_not_exceeded(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "sched-13")
    provider = LocalProvider()
    await sched.submit_run_attempt(db, attempt["project_id"], attempt["id"], provider, _spec(attempt, provider_config={"simulate": "hang"}))
    result = await sched.check_attempt_timeout(db, attempt["project_id"], attempt["id"], timeout_seconds=3600)
    assert result is None


@pytest.mark.asyncio
async def test_check_attempt_timeout_noop_when_none(db):
    _, _, _, attempt = await _setup_queued_attempt(db, "sched-14")
    result = await sched.check_attempt_timeout(db, attempt["project_id"], attempt["id"], timeout_seconds=None)
    assert result is None


def test_should_retry_true_for_transient_failure_within_max_retries():
    attempt = {"status": "failed", "attempt_number": 1, "failure_class": "infra_error"}
    assert sched.should_retry(attempt, max_retries=2) is True


def test_should_retry_false_for_user_error():
    attempt = {"status": "failed", "attempt_number": 1, "failure_class": "user_error"}
    assert sched.should_retry(attempt, max_retries=5) is False


def test_should_retry_false_when_attempt_number_exceeds_max_retries():
    attempt = {"status": "crashed", "attempt_number": 3, "failure_class": "oom"}
    assert sched.should_retry(attempt, max_retries=2) is False


def test_should_retry_false_for_non_terminal_status():
    attempt = {"status": "running", "attempt_number": 1, "failure_class": None}
    assert sched.should_retry(attempt, max_retries=5) is False


def test_next_retry_delay_seconds_exponential_with_cap():
    assert sched.next_retry_delay_seconds(1) == 2.0
    assert sched.next_retry_delay_seconds(2) == 4.0
    assert sched.next_retry_delay_seconds(3) == 8.0
    assert sched.next_retry_delay_seconds(20) == 300.0  # capped


@pytest.mark.asyncio
async def test_restart_recovery_then_poll_resolves_unknown_attempt(db):
    """Composes item 1's reconcile_stale_attempts with the scheduler's
    poll_run_attempt — restart recovery is not reimplemented here."""
    _, _, _, attempt = await _setup_queued_attempt(db, "sched-15")
    provider = LocalProvider()
    await sched.submit_run_attempt(db, attempt["project_id"], attempt["id"], provider, _spec(attempt, provider_config={"simulate": "hang"}))

    stale_time = "2000-01-01 00:00:00"
    await db.execute(
        "UPDATE research_run_attempts SET started_at = ? WHERE id = ?", (stale_time, attempt["id"]),
    )
    await db.commit()
    reconciled = await db_module.reconcile_stale_attempts(db, attempt["project_id"], stale_after_seconds=900)
    assert reconciled[0]["status"] == "unknown"

    class _NowSucceededProvider(LocalProvider):
        async def status(self, handle):
            return JobStatus(state="succeeded")

    resolver = _NowSucceededProvider()
    resolved = await sched.poll_run_attempt(db, attempt["project_id"], attempt["id"], resolver)
    assert resolved["status"] == "succeeded"
