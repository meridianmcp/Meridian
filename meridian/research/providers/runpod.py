"""f6627d83 — the RunPod adapter: the first concrete cloud provider behind
the :mod:`meridian.research.providers.base` contract, not a second product
boundary (see that module's and :mod:`meridian.research.scheduler`'s
docstrings).

CREDENTIAL / OPT-IN BOUNDARY: this module never constructs a real RunPod
client itself and never reads credentials from the environment. A
:class:`RunPodProvider` REQUIRES an injected client (see
:class:`RunPodClientProtocol`) — there is no default/fallback path that
would activate real credentials or make a network call. This is what makes
"RunPod provisioning is opt-in only and must not create pods, spend
credits, ... during tests or local dry-run" true by construction: a test
(or a caller who simply never wires up a real client) injects a fake
implementing :class:`RunPodClientProtocol`, and NOTHING in this module can
reach the network on its own.

:class:`RunPodClientProtocol` IS DELIBERATELY MINIMAL: create/get/stop/
terminate a pod. This is an adapter-defined seam, not a claim about RunPod's
exact SDK surface — wiring a real client in later means writing a thin
wrapper that satisfies this Protocol, not changing this module.

BOUNDED POLLING: :meth:`RunPodProvider.status` makes exactly ONE client
round-trip and returns — it never loops or blocks waiting for a terminal
state. Polling cadence, timeout enforcement, and retry/backoff are the
scheduler's responsibility (:mod:`meridian.research.scheduler`), not this
adapter's.

SECRET-SAFE ERRORS: any exception text or raw client payload that ends up
in a :class:`~meridian.research.providers.base.JobStatus.detail` is passed
through :func:`meridian.secret_redaction.redact` first — a fake client that
raises an exception EMBEDDING something secret-shaped must never leak it
into stored provenance.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from meridian.research.providers.base import (
    JobHandle,
    JobProvider,
    JobSpec,
    JobStatus,
    ProviderCapabilities,
    UnsupportedOperation,
)
from meridian.secret_redaction import redact


class RunPodClientProtocol(Protocol):
    """The minimal injectable seam this adapter needs. A real client (or a
    test's fake) implements these four methods; nothing else is required.

    All four are SYNCHRONOUS by design (RunPod's own SDK is a synchronous
    REST wrapper) — the adapter's async methods call them directly. A real
    integration wrapping a slow network client may want to run these via
    ``asyncio.to_thread``; that is the wrapper's concern, not this
    Protocol's.
    """

    def create_pod(self, *, name: str, image_name: str, env: "dict[str, str]", gpu_type_id: "str | None") -> "dict[str, Any]":
        ...

    def get_pod(self, pod_id: str) -> "dict[str, Any]":
        ...

    def stop_pod(self, pod_id: str) -> "dict[str, Any]":
        ...

    def terminate_pod(self, pod_id: str) -> "dict[str, Any]":
        ...


#: Raw RunPod ``desiredStatus``/``status`` values this adapter recognizes,
#: mapped onto meridian.experiment_model.ATTEMPT_STATUSES. Anything not in
#: this table maps to 'unknown' — see _map_runpod_status.
_STATUS_MAP: "dict[str, str]" = {
    "CREATED": "queued",
    "PENDING": "queued",
    "RUNNING": "running",
    "EXITED": "succeeded",  # refined by exit code — see _map_runpod_status
    "COMPLETED": "succeeded",
    "TERMINATED": "cancelled",
    "FAILED": "failed",
}


def _map_runpod_status(raw: "dict[str, Any]") -> tuple[str, "str | None"]:
    """Map a raw ``get_pod`` payload to ``(attempt_state, failure_class)``.

    ``failure_class`` is only ever non-``None`` when ``attempt_state`` is
    ``'failed'``/``'crashed'`` — matches
    ``meridian.experiment_model``'s transition contract. Never raises: an
    unrecognized raw status maps to ``('unknown', None)`` rather than
    guessing.
    """
    raw_status = str(raw.get("desiredStatus") or raw.get("status") or "").upper()
    exit_code = (raw.get("runtime") or {}).get("container", {}).get("exitCode")
    termination_reason = str(raw.get("terminationReason") or "").lower()

    if raw_status == "EXITED":
        if exit_code in (0, None):
            return "succeeded", None
        if exit_code == 137:
            return "crashed", "oom"
        return "failed", "user_error"

    if raw_status == "TERMINATED":
        if termination_reason == "preempted":
            return "crashed", "preempted"
        return "cancelled", None

    if raw_status == "FAILED":
        if termination_reason == "timeout":
            return "failed", "timeout"
        if termination_reason:
            return "failed", "dependency_error"
        return "failed", "infra_error"

    mapped = _STATUS_MAP.get(raw_status)
    if mapped is None:
        return "unknown", None
    return mapped, None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class RunPodProvider(JobProvider):
    """RunPod adapter. Requires an injected :class:`RunPodClientProtocol` —
    see the module docstring for why there is no default client."""

    name = "runpod"

    def __init__(self, client: RunPodClientProtocol) -> None:
        if client is None:
            raise ValueError("RunPodProvider requires an injected client (see RunPodClientProtocol)")
        self._client = client
        self._handles: "dict[str, JobHandle]" = {}  # idempotency_key -> handle

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            can_cancel=True,
            can_stream_logs=False,
            can_fetch_artifacts=False,
            supports_retries=True,
            supports_gpu=True,
            supports_spot=True,
            supports_preemption_signal=True,
            supports_networking_config=False,
        )

    async def submit(self, spec: JobSpec) -> JobHandle:
        existing = self._handles.get(spec.idempotency_key)
        if existing is not None:
            return existing

        gpu_type_id = spec.resources.gpu_type if spec.resources.gpu_count else None
        try:
            raw = self._client.create_pod(
                name=f"meridian-{spec.attempt_id}",
                image_name=spec.image or "",
                env=dict(spec.env),
                gpu_type_id=gpu_type_id,
            )
        except Exception as exc:  # noqa: BLE001 — provider errors are foreign, sanitize before propagating
            raise RuntimeError(f"runpod create_pod failed: {redact(str(exc))}") from None

        pod_id = str(raw.get("id") or "")
        if not pod_id:
            raise RuntimeError("runpod create_pod returned no pod id")

        handle = JobHandle(
            provider=self.name,
            external_id=pod_id,
            idempotency_key=spec.idempotency_key,
            submitted_at=_now_iso(),
        )
        self._handles[spec.idempotency_key] = handle
        return handle

    async def status(self, handle: JobHandle) -> JobStatus:
        try:
            raw = self._client.get_pod(handle.external_id)
        except Exception as exc:  # noqa: BLE001 — a polling failure is 'unknown', not 'failed'
            return JobStatus(state="unknown", detail=f"runpod get_pod failed: {redact(str(exc))}")

        state, failure_class = _map_runpod_status(raw)
        return JobStatus(state=state, failure_class=failure_class)

    async def cancel(self, handle: JobHandle) -> JobStatus:
        if not self.capabilities().can_cancel:
            raise UnsupportedOperation(self.name, "cancel")  # pragma: no cover — always True here
        try:
            self._client.stop_pod(handle.external_id)
            self._client.terminate_pod(handle.external_id)
        except Exception as exc:  # noqa: BLE001 — sanitize before propagating
            return JobStatus(state="unknown", detail=f"runpod cancel failed: {redact(str(exc))}")
        return JobStatus(state="cancelled")
