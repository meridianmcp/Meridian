"""Tests for sprint item f6627d83's RunPod adapter
(meridian.research.providers.runpod) — state mapping, bounded polling (one
round-trip per status() call), retry classification, cancellation,
secret-safe errors, null-safe status parsing, and the duplicate-create guard.
Every test injects a FAKE client; no live RunPod API call is made, matching
the item's acceptance criterion.

Focused, serial (no xdist) per this item's required_tool note.
"""
from __future__ import annotations

import pytest

from meridian.research.providers.base import JobHandle, JobSpec, ResourceRequest, UnsupportedOperation
from meridian.research.providers.runpod import RunPodProvider, _map_runpod_status, _pod_name_for

_SECRET_SHAPED = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


class _FakeRunPodClient:
    """Records every call; returns/raises whatever the test configures.

    ``pods_by_name`` models RunPod's name index: a successful create_pod stores
    the pod under its name, so find_pod_by_name sees it on a later submit, even
    from a fresh provider instance (a new process with an empty handle map).
    """

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.create_pod_result: dict = {"id": "pod-123"}
        self.create_pod_error: "Exception | None" = None
        # RunPod accepted the create, but the client saw an error (e.g. a timeout).
        self.create_accepted_then_error: "Exception | None" = None
        self.find_pod_error: "Exception | None" = None
        self.pods_by_name: dict[str, dict] = {}
        self.get_pod_result: dict = {"id": "pod-123", "status": "RUNNING"}
        self.get_pod_error: "Exception | None" = None
        self.stop_terminate_error: "Exception | None" = None
        self.stop_error: "Exception | None" = None
        self.terminate_error: "Exception | None" = None

    def find_pod_by_name(self, name):
        self.calls.append(("find_pod_by_name", (name,), {}))
        if self.find_pod_error:
            raise self.find_pod_error
        return self.pods_by_name.get(name)

    def create_pod(self, **kwargs):
        self.calls.append(("create_pod", (), kwargs))
        if self.create_pod_error:
            raise self.create_pod_error
        pod = {**self.create_pod_result, "name": kwargs["name"]}
        self.pods_by_name[kwargs["name"]] = pod
        if self.create_accepted_then_error:
            raise self.create_accepted_then_error
        return pod

    def get_pod(self, pod_id):
        self.calls.append(("get_pod", (pod_id,), {}))
        if self.get_pod_error:
            raise self.get_pod_error
        return self.get_pod_result

    def stop_pod(self, pod_id):
        self.calls.append(("stop_pod", (pod_id,), {}))
        for error in (self.stop_error, self.stop_terminate_error):
            if error:
                raise error
        return {}

    def terminate_pod(self, pod_id):
        self.calls.append(("terminate_pod", (pod_id,), {}))
        for error in (self.terminate_error, self.stop_terminate_error):
            if error:
                raise error
        return {}


def _spec(**overrides):
    # One GPU by default: the adapter refuses a spec with no GPU (see
    # test_submit_refuses_zero_gpu_count_before_any_request), so every other submit test needs one.
    kwargs = dict(
        project_id="p", experiment_id="e", run_id="r", attempt_id="a",
        idempotency_key="job-1", command=("python", "train.py"), image="runpod/test:latest",
        resources=ResourceRequest(gpu_count=1, gpu_type="NVIDIA A100 80GB"),
    )
    kwargs.update(overrides)
    return JobSpec(**kwargs)


def _handle(external_id: str = "p") -> JobHandle:
    return JobHandle(provider="runpod", external_id=external_id, idempotency_key="k", submitted_at="now")


def _create_calls(client: _FakeRunPodClient) -> list:
    return [c for c in client.calls if c[0] == "create_pod"]


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
    (create,) = _create_calls(client)
    assert create[2]["image_name"] == "my-image"


@pytest.mark.asyncio
async def test_submit_passes_gpu_type_when_gpu_requested():
    from meridian.research.providers.base import ResourceRequest

    client = _FakeRunPodClient()
    provider = RunPodProvider(client)
    await provider.submit(_spec(resources=ResourceRequest(gpu_count=1, gpu_type="A100")))
    (create,) = _create_calls(client)
    assert create[2]["gpu_type_id"] == "A100"
    assert create[2]["gpu_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("resources", [ResourceRequest(), ResourceRequest(gpu_type="A100")])
async def test_submit_refuses_zero_gpu_count_before_any_request(resources):
    # The v2 create body needs exactly one of gpu or cpu, and CPU is not mapped. A spec with no
    # GPU must fail closed rather than send a body with neither, so nothing is looked up or created.
    client = _FakeRunPodClient()
    with pytest.raises(ValueError, match="gpu_count >= 1"):
        await RunPodProvider(client).submit(_spec(resources=resources))
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("pod_status", ["EXITED", "ERROR"])
async def test_submit_returns_handle_of_existing_pod_in_any_status_and_creates_nothing(pod_status):
    # find_pod_by_name matches on name in any status, so a resubmit of the same key returns the old pod.
    client = _FakeRunPodClient()
    name = _pod_name_for("job-old")
    client.pods_by_name[name] = {"id": "pod-old", "name": name, "status": pod_status}
    handle = await RunPodProvider(client).submit(_spec(idempotency_key="job-old"))
    assert handle.external_id == "pod-old"
    assert _create_calls(client) == []


@pytest.mark.asyncio
async def test_submit_passes_command_env_and_gpu_count_through_create():
    client = _FakeRunPodClient()
    provider = RunPodProvider(client)
    await provider.submit(
        _spec(
            command=("python", "train.py", "--epochs", "3"),
            env={"WANDB_MODE": "offline"},
            resources=ResourceRequest(gpu_count=2, gpu_type="NVIDIA A100 80GB"),
        )
    )
    (create,) = _create_calls(client)
    kwargs = create[2]
    assert kwargs["command"] == ("python", "train.py", "--epochs", "3")
    assert kwargs["env"] == {"WANDB_MODE": "offline"}
    assert kwargs["gpu_count"] == 2
    assert kwargs["gpu_type_id"] == "NVIDIA A100 80GB"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"resources": ResourceRequest(cpu=4.0)},
        {"resources": ResourceRequest(gpu_count=1, gpu_type="A100", memory_gb=32.0)},
        {"inputs": ({"uri": "s3://bucket/key"},)},
    ],
)
async def test_submit_refuses_fields_with_no_mapped_create_field_instead_of_dropping_them(overrides):
    client = _FakeRunPodClient()
    with pytest.raises(NotImplementedError, match="refusing to drop it silently"):
        await RunPodProvider(client).submit(_spec(**overrides))
    assert client.calls == []  # nothing looked up, nothing created


@pytest.mark.asyncio
async def test_cpu_refusal_says_not_yet_mapped_and_never_claims_the_spec_lacks_the_field():
    # The v2 spec does define a CPU create object (CreateCpuConfig), so the refusal
    # must not say the spec lacks one. It says the field is not mapped yet.
    client = _FakeRunPodClient()
    with pytest.raises(NotImplementedError) as exc_info:
        await RunPodProvider(client).submit(_spec(resources=ResourceRequest(cpu=4.0)))
    message = str(exc_info.value)
    assert "not yet mapped" in message
    assert "no confirmed" not in message
    assert "does not confirm" not in message
    assert client.calls == []


@pytest.mark.asyncio
async def test_submit_requires_image():
    client = _FakeRunPodClient()
    with pytest.raises(ValueError, match="requires spec.image"):
        await RunPodProvider(client).submit(_spec(image=None))
    assert client.calls == []


@pytest.mark.asyncio
async def test_submit_requires_gpu_type_when_gpu_count_set():
    client = _FakeRunPodClient()
    with pytest.raises(ValueError, match="requires resources.gpu_type"):
        await RunPodProvider(client).submit(_spec(resources=ResourceRequest(gpu_count=1)))
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("gpu_type", [None, "A100"])
async def test_negative_gpu_count_raises_before_any_lookup_or_create(gpu_type):
    client = _FakeRunPodClient()
    with pytest.raises(ValueError, match="gpu_count must be >= 0"):
        await RunPodProvider(client).submit(_spec(resources=ResourceRequest(gpu_count=-1, gpu_type=gpu_type)))
    assert client.calls == []  # no duplicate lookup and no create


@pytest.mark.asyncio
async def test_submit_is_idempotent_same_key_does_not_call_create_pod_twice():
    client = _FakeRunPodClient()
    provider = RunPodProvider(client)
    spec = _spec(idempotency_key="same-key")
    first = await provider.submit(spec)
    second = await provider.submit(spec)
    assert first == second
    assert len(_create_calls(client)) == 1


@pytest.mark.asyncio
async def test_same_process_repeat_is_answered_from_handle_map_without_any_lookup():
    client = _FakeRunPodClient()
    provider = RunPodProvider(client)
    first = await provider.submit(_spec(idempotency_key="k-map"))
    client.calls.clear()
    second = await provider.submit(_spec(idempotency_key="k-map"))
    assert second == first
    assert client.calls == []


@pytest.mark.asyncio
async def test_submit_reuses_pod_already_created_under_same_key_without_create():
    client = _FakeRunPodClient()
    existing_name = _pod_name_for("job-1")
    client.pods_by_name[existing_name] = {"id": "pod-existing", "name": existing_name}
    handle = await RunPodProvider(client).submit(_spec(idempotency_key="job-1"))
    assert handle.external_id == "pod-existing"
    assert _create_calls(client) == []


@pytest.mark.asyncio
async def test_timeout_after_runpod_accepted_create_does_not_duplicate_on_resubmit():
    client = _FakeRunPodClient()
    client.create_accepted_then_error = TimeoutError("read timed out")
    with pytest.raises(RuntimeError, match="create_pod failed"):
        await RunPodProvider(client).submit(_spec(idempotency_key="job-timeout"))
    # A fresh provider (new process: empty handle map) resubmits the same key.
    client.create_accepted_then_error = None
    handle = await RunPodProvider(client).submit(_spec(idempotency_key="job-timeout"))
    assert handle.external_id == "pod-123"
    assert len(_create_calls(client)) == 1


@pytest.mark.asyncio
async def test_submit_does_not_create_when_lookup_fails():
    client = _FakeRunPodClient()
    client.find_pod_error = RuntimeError(f"network down, token={_SECRET_SHAPED}")
    with pytest.raises(RuntimeError, match="not creating a pod") as exc_info:
        await RunPodProvider(client).submit(_spec())
    assert _create_calls(client) == []
    assert _SECRET_SHAPED not in str(exc_info.value)


def test_pod_name_is_a_digest_so_distinct_keys_never_merge():
    # Sanitizing the raw key would map these two distinct keys to one name.
    assert _pod_name_for("run/1") != _pod_name_for("run_1")
    assert _pod_name_for("job-1") == _pod_name_for("job-1")
    assert _pod_name_for("job-1").startswith("meridian-")
    assert all(ch.isalnum() or ch == "-" for ch in _pod_name_for("a b/c:d"))


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
    client.create_pod_error = RuntimeError(f"auth failed with key {_SECRET_SHAPED}")
    provider = RunPodProvider(client)
    with pytest.raises(RuntimeError) as exc_info:
        await provider.submit(_spec())
    assert "sk-ant-api03" not in str(exc_info.value)
    assert "REDACTED" in str(exc_info.value)


def _chain_links(exc: BaseException) -> list[BaseException]:
    """``exc`` and every exception reachable from it through ``__context__`` or ``__cause__``."""
    links: list[BaseException] = []
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if any(current is seen for seen in links):
            continue
        links.append(current)
        pending.extend(link for link in (current.__context__, current.__cause__) if link is not None)
    return links


def _assert_chain_free_and_secret_free(exc: BaseException, secret: str) -> None:
    """The raised error has no chained exception, and no exception in any chain shows the secret."""
    assert exc.__context__ is None
    assert exc.__cause__ is None
    for link in _chain_links(exc):
        texts = [str(link), repr(link), repr(link.args), *map(str, link.args)]
        assert not any(secret in text for text in texts), f"{type(link).__name__} in the chain carries the secret"


@pytest.mark.asyncio
async def test_submit_lookup_failure_is_raised_chain_free_and_secret_free():
    # The client exception echoes a key-shaped value. It must not stay reachable through __context__.
    client = _FakeRunPodClient()
    client.find_pod_error = RuntimeError(f"network down, token={_SECRET_SHAPED}")
    with pytest.raises(RuntimeError, match="not creating a pod") as exc_info:
        await RunPodProvider(client).submit(_spec())
    _assert_chain_free_and_secret_free(exc_info.value, _SECRET_SHAPED)
    assert _create_calls(client) == []


@pytest.mark.asyncio
async def test_submit_create_failure_is_raised_chain_free_and_secret_free():
    client = _FakeRunPodClient()
    client.create_pod_error = RuntimeError(f"auth failed with key {_SECRET_SHAPED}")
    with pytest.raises(RuntimeError, match="create_pod failed") as exc_info:
        await RunPodProvider(client).submit(_spec())
    _assert_chain_free_and_secret_free(exc_info.value, _SECRET_SHAPED)


# ---------------------------------------------------------------------------
# Status / state mapping — one bounded round-trip per call.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_maps_running():
    client = _FakeRunPodClient()
    client.get_pod_result = {"id": "pod-1", "status": "RUNNING"}
    provider = RunPodProvider(client)
    handle = JobHandle(provider="runpod", external_id="pod-1", idempotency_key="k", submitted_at="now")
    status = await provider.status(handle)
    assert status.state == "running"
    assert len(client.calls) == 1  # exactly one round-trip


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 1, 137])
async def test_exited_pod_is_unknown_even_when_a_runtime_exit_code_is_present(exit_code):
    # The v2 spec declares no exit code, so a runtime.container.exitCode field is never read.
    client = _FakeRunPodClient()
    client.get_pod_result = {"id": "p", "status": "EXITED", "runtime": {"container": {"exitCode": exit_code}}}
    status = await RunPodProvider(client).status(
        JobHandle(provider="runpod", external_id="p", idempotency_key="k", submitted_at="now")
    )
    assert status.state == "unknown"
    assert status.failure_class is None


@pytest.mark.asyncio
async def test_status_maps_terminated_to_unknown_not_cancelled():
    # The pod was permanently deleted and the spec does not say why. It is not reported as
    # 'cancelled', which would claim that this run's own cancel did it.
    client = _FakeRunPodClient()
    client.get_pod_result = {"id": "p", "status": "TERMINATED"}
    status = await RunPodProvider(client).status(
        JobHandle(provider="runpod", external_id="p", idempotency_key="k", submitted_at="now")
    )
    assert status.state == "unknown"
    assert status.failure_class is None
    assert "does not say why" in (status.detail or "")


@pytest.mark.asyncio
async def test_terminated_pod_ignores_an_undeclared_termination_reason():
    client = _FakeRunPodClient()
    client.get_pod_result = {"id": "p", "status": "TERMINATED", "terminationReason": "preempted"}
    status = await RunPodProvider(client).status(
        JobHandle(provider="runpod", external_id="p", idempotency_key="k", submitted_at="now")
    )
    assert status.state == "unknown"
    assert status.failure_class is None


@pytest.mark.asyncio
async def test_failed_is_not_a_spec_status_so_it_is_unknown():
    client = _FakeRunPodClient()
    client.get_pod_result = {"id": "p", "status": "FAILED", "terminationReason": "timeout"}
    status = await RunPodProvider(client).status(
        JobHandle(provider="runpod", external_id="p", idempotency_key="k", submitted_at="now")
    )
    assert status.state == "unknown"
    assert status.failure_class is None


@pytest.mark.asyncio
async def test_status_maps_unrecognized_raw_status_to_unknown():
    client = _FakeRunPodClient()
    client.get_pod_result = {"id": "p", "status": "SOMETHING_NEW_RUNPOD_ADDED"}
    status = await RunPodProvider(client).status(
        JobHandle(provider="runpod", external_id="p", idempotency_key="k", submitted_at="now")
    )
    assert status.state == "unknown"
    assert status.failure_class is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pod_status, expected_state, expected_failure",
    [
        # Every value of the v2 spec's PodStatus enum, read from the Pod ``status`` field.
        ("PROVISIONING", "queued", None),  # spec: pod is being allocated
        ("STARTING", "queued", None),  # spec: container is starting
        ("RUNNING", "running", None),  # spec: container is healthy
        ("EXITED", "unknown", None),  # spec: container exited; the spec declares no exit code
        ("ERROR", "failed", None),  # spec: unrecoverable container error; the cause is not stated
        ("TERMINATED", "unknown", None),  # spec: pod permanently deleted; the spec does not say why
    ],
)
async def test_every_spec_pod_status_value_is_mapped_explicitly(pod_status, expected_state, expected_failure):
    client = _FakeRunPodClient()
    client.get_pod_result = {"id": "p", "status": pod_status}
    status = await RunPodProvider(client).status(_handle("p"))
    assert status.state == expected_state
    assert status.failure_class == expected_failure


@pytest.mark.asyncio
async def test_exited_pod_detail_says_the_api_reports_no_exit_code():
    client = _FakeRunPodClient()
    client.get_pod_result = {"id": "p", "status": "EXITED"}
    status = await RunPodProvider(client).status(_handle("p"))
    assert status.state == "unknown"
    assert status.failure_class is None
    assert "no exit code" in (status.detail or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [{"id": "p"}, {"id": "p", "status": None}, {"id": "p", "status": ""}, {"id": "p", "status": "SOMETHING_NEW"}],
)
async def test_missing_or_unrecognised_status_is_unknown_with_a_reason(payload):
    client = _FakeRunPodClient()
    client.get_pod_result = payload
    status = await RunPodProvider(client).status(_handle("p"))
    assert status.state == "unknown"
    assert status.failure_class is None
    assert status.detail  # a reason is always given


def test_desired_status_is_not_read():
    # ``desiredStatus`` is not in the v2 spec, so a payload that carries only it has no status.
    assert _map_runpod_status({"status": "RUNNING", "desiredStatus": "EXITED"}) == ("running", None)
    assert _map_runpod_status({"desiredStatus": "RUNNING"}) == ("unknown", None)


@pytest.mark.parametrize("raw_status", ["COMPLETED", "CREATED", "PENDING", "FAILED"])
def test_status_values_outside_the_spec_enum_are_unknown(raw_status):
    # None of these is in the v2 PodStatus enum, so none is mapped. COMPLETED was never mapped to 'succeeded'.
    assert _map_runpod_status({"status": raw_status}) == ("unknown", None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, expected_state, expected_failure",
    [
        ({"status": "EXITED"}, "unknown", None),
        ({"status": "EXITED", "runtime": None}, "unknown", None),
        ({"status": "EXITED", "runtime": {"container": None}}, "unknown", None),
        ({"status": "EXITED", "runtime": "not-an-object"}, "unknown", None),
        ({"status": "RUNNING", "runtime": None}, "running", None),
        ({"status": "RUNNING", "runtime": {"container": None}}, "running", None),
        ({"status": "RUNNING", "runtime": {"uptime": None}}, "running", None),
        ({"status": "ERROR", "runtime": None}, "failed", None),
        ({"status": "STARTING", "runtime": None}, "queued", None),
        ({"status": "FAILED", "runtime": None}, "unknown", None),
    ],
)
async def test_status_is_null_safe_and_never_raises_on_null_runtime_fields(payload, expected_state, expected_failure):
    client = _FakeRunPodClient()
    client.get_pod_result = payload
    status = await RunPodProvider(client).status(
        JobHandle(provider="runpod", external_id="p", idempotency_key="k", submitted_at="now")
    )
    assert status.state == expected_state
    assert status.failure_class == expected_failure


@pytest.mark.asyncio
async def test_status_returns_unknown_when_get_pod_returns_no_object():
    client = _FakeRunPodClient()
    client.get_pod_result = None  # type: ignore[assignment]
    status = await RunPodProvider(client).status(_handle("p"))
    assert status.state == "unknown"


@pytest.mark.asyncio
async def test_status_returns_unknown_and_redacted_detail_on_client_exception():
    client = _FakeRunPodClient()
    client.get_pod_error = RuntimeError(f"token={_SECRET_SHAPED}")
    status = await RunPodProvider(client).status(
        JobHandle(provider="runpod", external_id="p", idempotency_key="k", submitted_at="now")
    )
    assert status.state == "unknown"
    assert "sk-ant-api03" not in (status.detail or "")
    assert "REDACTED" in (status.detail or "")


def test_map_runpod_status_pure_function_unit():
    # Direct unit coverage of the mapping table beyond the async wrapper.
    assert _map_runpod_status({"status": "STARTING"}) == ("queued", None)
    assert _map_runpod_status({"status": "ERROR"}) == ("failed", None)
    assert _map_runpod_status({}) == ("unknown", None)


@pytest.mark.parametrize("raw", [None, [], "EXITED", 0])
def test_map_runpod_status_non_object_payload_is_unknown(raw):
    assert _map_runpod_status(raw) == ("unknown", None)


# ---------------------------------------------------------------------------
# Cancellation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_calls_stop_and_terminate_for_a_running_pod():
    client = _FakeRunPodClient()
    provider = RunPodProvider(client)
    handle = JobHandle(provider="runpod", external_id="pod-1", idempotency_key="k", submitted_at="now")
    status = await provider.cancel(handle)
    assert status.state == "cancelled"
    assert status.detail is None
    assert [c[0] for c in client.calls] == ["get_pod", "stop_pod", "terminate_pod"]
    assert client.calls[1][1] == ("pod-1",)


@pytest.mark.asyncio
@pytest.mark.parametrize("pod_status", ["EXITED", "ERROR", "TERMINATED"])
async def test_cancel_skips_stop_for_a_pod_whose_status_allows_no_stop(pod_status):
    # The spec lists no stop action for these statuses, so a stop request would return 409.
    client = _FakeRunPodClient()
    client.get_pod_result = {"id": "pod-1", "status": pod_status}
    status = await RunPodProvider(client).cancel(_handle("pod-1"))
    assert status.state == "cancelled"
    assert [c[0] for c in client.calls] == ["get_pod", "terminate_pod"]
    assert client.calls[1][1] == ("pod-1",)


@pytest.mark.asyncio
async def test_cancel_reports_unknown_when_terminate_fails_after_skipping_stop():
    client = _FakeRunPodClient()
    client.get_pod_result = {"id": "pod-1", "status": "EXITED"}
    client.terminate_error = RuntimeError("terminate refused")
    status = await RunPodProvider(client).cancel(_handle("pod-1"))
    assert status.state == "unknown"
    assert "terminate_pod failed" in (status.detail or "")
    assert "stop_pod" not in (status.detail or "")
    assert [c[0] for c in client.calls] == ["get_pod", "terminate_pod"]


@pytest.mark.asyncio
async def test_cancel_still_attempts_stop_when_the_status_read_fails():
    # An unreadable status is not a reason to skip stop. The status read's own failure is not
    # reported, because the stop and terminate outcomes decide the cancel result.
    client = _FakeRunPodClient()
    client.get_pod_error = RuntimeError("get_pod down")
    status = await RunPodProvider(client).cancel(_handle("pod-1"))
    assert status.state == "cancelled"
    assert status.detail is None
    assert [c[0] for c in client.calls] == ["get_pod", "stop_pod", "terminate_pod"]


@pytest.mark.asyncio
async def test_cancel_reports_unknown_naming_failed_stop_and_still_attempts_terminate():
    client = _FakeRunPodClient()
    client.stop_error = RuntimeError("stop refused")
    status = await RunPodProvider(client).cancel(_handle("pod-1"))
    assert status.state == "unknown"
    assert "stop_pod failed" in (status.detail or "")
    assert "terminate_pod" not in (status.detail or "")
    assert [c[0] for c in client.calls] == ["get_pod", "stop_pod", "terminate_pod"]


@pytest.mark.asyncio
async def test_cancel_reports_unknown_naming_failed_terminate_only():
    client = _FakeRunPodClient()
    client.terminate_error = RuntimeError("terminate refused")
    status = await RunPodProvider(client).cancel(_handle("pod-1"))
    assert status.state == "unknown"
    assert "terminate_pod failed" in (status.detail or "")
    assert "stop_pod failed" not in (status.detail or "")


@pytest.mark.asyncio
async def test_cancel_returns_unknown_and_redacted_detail_on_client_exception():
    client = _FakeRunPodClient()
    client.stop_terminate_error = RuntimeError(f"secret {_SECRET_SHAPED}")
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
