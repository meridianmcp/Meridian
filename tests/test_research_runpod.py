"""Tests for sprint item f6627d83's RunPod adapter
(meridian.research.providers.runpod) — state mapping, bounded polling
(one round-trip per status() call), retry classification, cancellation, and
secret-safe errors. Every test injects a FAKE client; no live RunPod API
call is made, matching the item's acceptance criterion.

Focused, serial (no xdist) per this item's required_tool note.
"""
from __future__ import annotations

import pytest

from meridian.research.providers.base import JobHandle, JobSpec, UnsupportedOperation
from meridian.research.providers.runpod import RunPodProvider, _map_runpod_status


class _FakeRunPodClient:
    """Records every call; returns/raises whatever the test configures."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.create_pod_result: dict = {"id": "pod-123"}
        self.create_pod_error: "Exception | None" = None
        self.get_pod_result: dict = {"desiredStatus": "RUNNING"}
        self.get_pod_error: "Exception | None" = None
        self.stop_terminate_error: "Exception | None" = None

    def create_pod(self, **kwargs):
        self.calls.append(("create_pod", (), kwargs))
        if self.create_pod_error:
            raise self.create_pod_error
        return self.create_pod_result

    def get_pod(self, pod_id):
        self.calls.append(("get_pod", (pod_id,), {}))
        if self.get_pod_error:
            raise self.get_pod_error
        return self.get_pod_result

    def stop_pod(self, pod_id):
        self.calls.append(("stop_pod", (pod_id,), {}))
        if self.stop_terminate_error:
            raise self.stop_terminate_error
        return {}

    def terminate_pod(self, pod_id):
        self.calls.append(("terminate_pod", (pod_id,), {}))
        if self.stop_terminate_error:
            raise self.stop_terminate_error
        return {}


def _spec(**overrides):
    kwargs = dict(
        project_id="p", experiment_id="e", run_id="r", attempt_id="a",
        idempotency_key="job-1", command=("python", "train.py"),
    )
    kwargs.update(overrides)
    return JobSpec(**kwargs)


def test_requires_injected_client():
    with pytest.raises(ValueError, match="requires an injected client"):
        RunPodProvider(None)


def test_capabilities():
    caps = RunPodProvider(_FakeRunPodClient()).capabilities()
    assert caps.can_cancel is True
    assert caps.supports_gpu is True
    assert caps.supports_spot is True
    assert caps.can_stream_logs is False
    assert caps.can_fetch_artifacts is False


@pytest.mark.asyncio
async def test_submit_calls_create_pod_and_returns_handle():
    client = _FakeRunPodClient()
    provider = RunPodProvider(client)
    handle = await provider.submit(_spec(image="my-image"))
    assert handle.provider == "runpod"
    assert handle.external_id == "pod-123"
    assert client.calls[0][0] == "create_pod"
    assert client.calls[0][2]["image_name"] == "my-image"


@pytest.mark.asyncio
async def test_submit_passes_gpu_type_when_gpu_requested():
    from meridian.research.providers.base import ResourceRequest

    client = _FakeRunPodClient()
    provider = RunPodProvider(client)
    await provider.submit(_spec(resources=ResourceRequest(gpu_count=1, gpu_type="A100")))
    assert client.calls[0][2]["gpu_type_id"] == "A100"


@pytest.mark.asyncio
async def test_submit_omits_gpu_type_when_no_gpu_requested():
    client = _FakeRunPodClient()
    provider = RunPodProvider(client)
    await provider.submit(_spec())
    assert client.calls[0][2]["gpu_type_id"] is None


@pytest.mark.asyncio
async def test_submit_is_idempotent_same_key_does_not_call_create_pod_twice():
    client = _FakeRunPodClient()
    provider = RunPodProvider(client)
    spec = _spec(idempotency_key="same-key")
    first = await provider.submit(spec)
    second = await provider.submit(spec)
    assert first == second
    assert len([c for c in client.calls if c[0] == "create_pod"]) == 1


@pytest.mark.asyncio
async def test_submit_raises_when_no_pod_id_returned():
    client = _FakeRunPodClient()
    client.create_pod_result = {}
    provider = RunPodProvider(client)
    with pytest.raises(RuntimeError, match="no pod id"):
        await provider.submit(_spec())


@pytest.mark.asyncio
async def test_submit_redacts_secret_shaped_client_error():
    client = _FakeRunPodClient()
    client.create_pod_error = RuntimeError(
        "auth failed with key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    provider = RunPodProvider(client)
    with pytest.raises(RuntimeError) as exc_info:
        await provider.submit(_spec())
    assert "sk-ant-api03" not in str(exc_info.value)
    assert "REDACTED" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Status / state mapping — one bounded round-trip per call.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_maps_running():
    client = _FakeRunPodClient()
    client.get_pod_result = {"desiredStatus": "RUNNING"}
    provider = RunPodProvider(client)
    handle = JobHandle(provider="runpod", external_id="pod-1", idempotency_key="k", submitted_at="now")
    status = await provider.status(handle)
    assert status.state == "running"
    assert len(client.calls) == 1  # exactly one round-trip


@pytest.mark.asyncio
async def test_status_maps_exited_zero_to_succeeded():
    client = _FakeRunPodClient()
    client.get_pod_result = {"desiredStatus": "EXITED", "runtime": {"container": {"exitCode": 0}}}
    status = await RunPodProvider(client).status(
        JobHandle(provider="runpod", external_id="p", idempotency_key="k", submitted_at="now")
    )
    assert status.state == "succeeded"
    assert status.failure_class is None


@pytest.mark.asyncio
async def test_status_maps_exited_nonzero_to_failed_user_error():
    client = _FakeRunPodClient()
    client.get_pod_result = {"desiredStatus": "EXITED", "runtime": {"container": {"exitCode": 1}}}
    status = await RunPodProvider(client).status(
        JobHandle(provider="runpod", external_id="p", idempotency_key="k", submitted_at="now")
    )
    assert status.state == "failed"
    assert status.failure_class == "user_error"


@pytest.mark.asyncio
async def test_status_maps_exited_137_to_crashed_oom():
    client = _FakeRunPodClient()
    client.get_pod_result = {"desiredStatus": "EXITED", "runtime": {"container": {"exitCode": 137}}}
    status = await RunPodProvider(client).status(
        JobHandle(provider="runpod", external_id="p", idempotency_key="k", submitted_at="now")
    )
    assert status.state == "crashed"
    assert status.failure_class == "oom"


@pytest.mark.asyncio
async def test_status_maps_terminated_to_cancelled():
    client = _FakeRunPodClient()
    client.get_pod_result = {"desiredStatus": "TERMINATED"}
    status = await RunPodProvider(client).status(
        JobHandle(provider="runpod", external_id="p", idempotency_key="k", submitted_at="now")
    )
    assert status.state == "cancelled"


@pytest.mark.asyncio
async def test_status_maps_terminated_preempted_to_crashed_preempted():
    client = _FakeRunPodClient()
    client.get_pod_result = {"desiredStatus": "TERMINATED", "terminationReason": "preempted"}
    status = await RunPodProvider(client).status(
        JobHandle(provider="runpod", external_id="p", idempotency_key="k", submitted_at="now")
    )
    assert status.state == "crashed"
    assert status.failure_class == "preempted"


@pytest.mark.asyncio
async def test_status_maps_failed_with_timeout_reason():
    client = _FakeRunPodClient()
    client.get_pod_result = {"desiredStatus": "FAILED", "terminationReason": "timeout"}
    status = await RunPodProvider(client).status(
        JobHandle(provider="runpod", external_id="p", idempotency_key="k", submitted_at="now")
    )
    assert status.state == "failed"
    assert status.failure_class == "timeout"


@pytest.mark.asyncio
async def test_status_maps_unrecognized_raw_status_to_unknown():
    client = _FakeRunPodClient()
    client.get_pod_result = {"desiredStatus": "SOMETHING_NEW_RUNPOD_ADDED"}
    status = await RunPodProvider(client).status(
        JobHandle(provider="runpod", external_id="p", idempotency_key="k", submitted_at="now")
    )
    assert status.state == "unknown"
    assert status.failure_class is None


@pytest.mark.asyncio
async def test_status_returns_unknown_and_redacted_detail_on_client_exception():
    client = _FakeRunPodClient()
    client.get_pod_error = RuntimeError("token=sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    status = await RunPodProvider(client).status(
        JobHandle(provider="runpod", external_id="p", idempotency_key="k", submitted_at="now")
    )
    assert status.state == "unknown"
    assert "sk-ant-api03" not in (status.detail or "")
    assert "REDACTED" in (status.detail or "")


def test_map_runpod_status_pure_function_unit():
    # Direct unit coverage of the mapping table beyond the async wrapper.
    assert _map_runpod_status({"desiredStatus": "CREATED"}) == ("queued", None)
    assert _map_runpod_status({"desiredStatus": "PENDING"}) == ("queued", None)
    assert _map_runpod_status({}) == ("unknown", None)


# ---------------------------------------------------------------------------
# Cancellation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_calls_stop_and_terminate():
    client = _FakeRunPodClient()
    provider = RunPodProvider(client)
    handle = JobHandle(provider="runpod", external_id="pod-1", idempotency_key="k", submitted_at="now")
    status = await provider.cancel(handle)
    assert status.state == "cancelled"
    assert [c[0] for c in client.calls] == ["stop_pod", "terminate_pod"]
    assert client.calls[0][1] == ("pod-1",)


@pytest.mark.asyncio
async def test_cancel_returns_unknown_and_redacted_detail_on_client_exception():
    client = _FakeRunPodClient()
    client.stop_terminate_error = RuntimeError("secret sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    provider = RunPodProvider(client)
    handle = JobHandle(provider="runpod", external_id="pod-1", idempotency_key="k", submitted_at="now")
    status = await provider.cancel(handle)
    assert status.state == "unknown"
    assert "sk-ant-api03" not in (status.detail or "")


@pytest.mark.asyncio
async def test_cancel_always_supported():
    # capabilities().can_cancel is always True for this provider, so the
    # base class's fail-closed UnsupportedOperation path is unreachable here.
    provider = RunPodProvider(_FakeRunPodClient())
    assert provider.capabilities().can_cancel is True
    try:
        await provider.cancel(JobHandle(provider="runpod", external_id="p", idempotency_key="k", submitted_at="now"))
    except UnsupportedOperation:
        pytest.fail("RunPodProvider.cancel must not raise UnsupportedOperation — it declares can_cancel=True")
