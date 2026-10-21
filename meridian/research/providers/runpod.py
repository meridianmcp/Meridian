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

:class:`RunPodClientProtocol` IS DELIBERATELY MINIMAL: find a pod by name,
create, get, stop and terminate a pod. This is an adapter-defined seam, not a
claim about RunPod's full API surface. :class:`meridian.research.providers.
runpod_client.RunPodClient` satisfies it directly, so wiring a real client
needs no extra wrapper.

DUPLICATE-CREATE GUARD (best-effort until verified against a live account): a
pod's name is derived from the job's idempotency key (:func:`_pod_name_for`).
:meth:`RunPodProvider.submit` looks that name up on RunPod BEFORE creating a
pod, so a create that timed out after RunPod accepted it should not produce a
second pod, even from a new process. That depends on the list call seeing a pod
created moments earlier (read-after-write consistency), which is not verified.
The in-process handle map is only a fast path. If the lookup itself fails,
submit raises rather than creating.

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

import hashlib
from datetime import datetime, timezone
from typing import Any, NoReturn, Protocol

from meridian.exception_chain import raise_unchained
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
    test's fake) implements these five methods; nothing else is required.

    All five are SYNCHRONOUS by design (RunPod's own SDK is a synchronous
    REST wrapper) — the adapter's async methods call them directly. A real
    integration wrapping a slow network client may want to run these via
    ``asyncio.to_thread``; that is the wrapper's concern, not this
    Protocol's.
    """

    def find_pod_by_name(self, name: str) -> "dict[str, Any] | None":
        ...

    def create_pod(
        self,
        *,
        name: str,
        image_name: str,
        env: "dict[str, str]",
        gpu_type_id: "str | None",
        gpu_count: int,
        command: "tuple[str, ...]",
    ) -> "dict[str, Any]":
        ...

    def get_pod(self, pod_id: str) -> "dict[str, Any]":
        ...

    def stop_pod(self, pod_id: str) -> "dict[str, Any]":
        ...

    def terminate_pod(self, pod_id: str) -> "dict[str, Any]":
        ...


#: The v2 spec's Pod ``status`` values (its PodStatus enum) onto
#: meridian.experiment_model.ATTEMPT_STATUSES. Every spec value has an entry.
#: :func:`_map_runpod_status` maps any other value, including a missing one, to
#: 'unknown'. 'launching' is not an ATTEMPT_STATUSES state, so STARTING is 'queued'.
_STATUS_MAP: "dict[str, str]" = {
    "PROVISIONING": "queued",  # spec: pod is being allocated
    "STARTING": "queued",  # spec: container is starting, before the job runs
    "RUNNING": "running",  # spec: container is healthy
    "EXITED": "unknown",  # spec: container exited; the spec declares no exit code
    "ERROR": "failed",  # spec: unrecoverable container error; the spec does not say whose fault it is
    "TERMINATED": "unknown",  # spec: pod permanently deleted; the spec does not say why
}
#: JobStatus.detail for an 'unknown' pod, keyed by its spec status. A missing or
#: unrecognised status gets _UNRECOGNISED_STATUS_DETAIL instead.
_UNKNOWN_DETAIL_BY_STATUS: "dict[str, str]" = {
    "EXITED": "runpod reports the pod exited and the v2 spec declares no exit code, so the outcome is unknown",
    "TERMINATED": "runpod reports the pod was terminated and the v2 API does not say why, so the outcome is unknown",
}
_UNRECOGNISED_STATUS_DETAIL = "runpod pod status is missing or not a v2 PodStatus value, so the outcome is unknown"
#: Pod statuses for which the spec lists no stop action. The podAction description
#: lists stop only for PROVISIONING, STARTING and RUNNING, and a stop request that is
#: not valid for the current status returns 409. cancel() skips the stop call for these.
_STOP_REJECTED_STATUSES: frozenset[str] = frozenset({"EXITED", "ERROR", "TERMINATED"})


def _raw_status(raw: Any) -> str:
    """The upper-cased ``status`` of a ``get_pod`` payload, or ``""`` when it is
    missing or the payload is not an object. Only the spec's ``status`` field is
    read."""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("status") or "").upper()


def _map_runpod_status(raw: Any) -> tuple[str, "str | None"]:
    """Map a raw ``get_pod`` payload to ``(attempt_state, failure_class)``.

    Reads only the Pod ``status`` field and never raises. Each of the six v2
    PodStatus values maps explicitly (see :data:`_STATUS_MAP`):

    * PROVISIONING and STARTING map to ``queued``. RUNNING maps to ``running``.
    * EXITED maps to ``unknown``. The v2 spec declares no exit code, so an
      exited pod is never ``succeeded`` or ``failed``, and no ``runtime`` field
      is read to decide otherwise.
    * ERROR maps to ``failed``. The spec calls it an unrecoverable container
      error and does not say whose fault it is, so no failure class is set. The
      scheduler then records ``unknown``, its default for an unclassified failure.
    * TERMINATED maps to ``unknown``. The pod is permanently deleted and the spec
      does not say why, so it is not reported as ``cancelled``.

    A missing, non-object or unrecognised status maps to ``('unknown', None)``.
    That includes the values the spec does not define, such as ``desiredStatus``,
    ``CREATED``, ``PENDING``, ``FAILED`` and ``COMPLETED``. ``failure_class`` is
    therefore always ``None`` here, because the spec gives no basis for
    classifying any state.
    """
    mapped = _STATUS_MAP.get(_raw_status(raw))
    if mapped is None:
        return "unknown", None
    return mapped, None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _pod_name_for(idempotency_key: str) -> str:
    """The RunPod pod name for a job's idempotency key.

    A SHA-256 digest, not the raw key and not a sanitized substring: a digest
    is charset-safe, and two distinct keys can never map to the same name
    through sanitization. The name is the duplicate-create guard's lookup key.

    Pods created before this scheme were named ``meridian-{attempt_id}``. The
    guard looks up only the digest name, so it does not find a pod created under
    the old name, and resubmitting the same key can create a second pod for that
    job.
    """
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
    return f"meridian-{digest}"


def _unmapped_create_field(field_name: str, reason: str) -> NoReturn:
    """Fail closed for a JobSpec/ResourceRequest field this adapter does not map
    to a create-body field. Raising here (before any lookup or create call)
    means the attempt stays queued, not silently run without the requested
    resource. ``reason`` says why and must not claim the RunPod spec lacks a
    field."""
    raise NotImplementedError(f"RunPod create_pod: {field_name} {reason}; refusing to drop it silently")


class RunPodProvider(JobProvider):
    """RunPod adapter. Requires an injected :class:`RunPodClientProtocol` —
    see the module docstring for why there is no default client."""

    name = "runpod"

    def __init__(self, client: RunPodClientProtocol) -> None:
        if client is None:
            raise ValueError("RunPodProvider requires an injected client (see RunPodClientProtocol)")
        self._client = client
        # idempotency_key -> handle. A same-process fast path only: the
        # cross-process duplicate guard is the name lookup in submit().
        self._handles: "dict[str, JobHandle]" = {}

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
        """Submit ``spec`` as one RunPod pod, idempotently on its key.

        Refuses, before any lookup or create call, a spec that cannot be sent as
        a valid create body:

        * ``resources.gpu_count`` below 1, or no ``resources.gpu_type``. The v2
          spec requires exactly one of ``gpu`` or ``cpu``, and CPU is not mapped,
          so the default (no GPU) fails closed here instead of sending a body
          with neither.
        * CPU sizing, memory, or inputs. None of these is mapped to a create-body
          field yet. The v2 spec defines a CPU create object, but this adapter does
          not resolve its flavor id. ``spec.resources.timeout_seconds`` is not a
          create field either. Nothing in production calls
          :func:`meridian.research.scheduler.check_attempt_timeout`, so no code path
          enforces the timeout today.

        Idempotency: a repeat in this process returns the cached handle.
        Otherwise the pod is looked up by its key-derived name
        (:func:`_pod_name_for`), and a pod is created only when none carries that
        name. The lookup pages through the whole pod listing and matches the name
        exactly, in any status. So resubmitting a key whose pod is EXITED or ERROR
        returns that pod's handle and creates nothing. A TERMINATED pod is not
        matched: the spec's deletePod description says a deleted pod no longer
        appears in listPods, so a resubmit after terminate creates a new pod.
        Callers must mint a fresh idempotency key per attempt.

        A failed lookup raises rather than creating. Two processes racing the
        same key can still both create, because RunPod does not make the lookup
        and the create atomic.
        """
        existing = self._handles.get(spec.idempotency_key)
        if existing is not None:
            return existing

        if spec.resources.cpu is not None:
            _unmapped_create_field(
                "cpu",
                "is not yet mapped to the create body (CreateCpuConfig needs a flavor id "
                "from GET /v2/catalog/cpus, which this adapter does not resolve)",
            )
        if spec.resources.memory_gb is not None:
            _unmapped_create_field("memory_gb", "is not mapped to a create-body field in this adapter")
        if spec.inputs:
            _unmapped_create_field("inputs", "is not mapped to a create-body field in this adapter")
        if not spec.image:
            raise ValueError("runpod submit requires spec.image (templateId is not supported)")
        gpu_count = spec.resources.gpu_count
        if gpu_count < 0:
            raise ValueError("gpu_count must be >= 0")  # before any lookup, so no request is made
        if gpu_count == 0:
            # Exactly one of gpu or cpu is required, and CPU was refused above, so no body could be valid.
            raise ValueError(
                "runpod submit requires resources.gpu_count >= 1: the v2 create body needs exactly one of "
                "gpu or cpu, and this adapter does not map cpu"
            )
        gpu_type_id = spec.resources.gpu_type
        if not gpu_type_id:
            raise ValueError("runpod submit with gpu_count > 0 requires resources.gpu_type")

        pod_name = _pod_name_for(spec.idempotency_key)
        # Failures are recorded inside the except blocks and raised after them (see
        # raise_unchained), so the client exception is not left in __context__.
        failure: "str | None" = None
        try:
            found = self._client.find_pod_by_name(pod_name)
        except Exception as exc:  # noqa: BLE001 — cannot rule out a duplicate, so do not create
            failure = (
                "runpod find_pod_by_name failed; not creating a pod without ruling out a duplicate: "
                f"{redact(str(exc))}"
            )
        if failure is not None:
            raise_unchained(RuntimeError(failure))

        raw: Any = found
        if raw is None:
            try:
                raw = self._client.create_pod(
                    name=pod_name,
                    image_name=spec.image,
                    env=dict(spec.env),
                    gpu_type_id=gpu_type_id,
                    gpu_count=gpu_count,
                    command=tuple(spec.command),
                )
            except Exception as exc:  # noqa: BLE001 — provider errors are foreign, sanitize before propagating
                failure = f"runpod create_pod failed: {redact(str(exc))}"
            if failure is not None:
                raise_unchained(RuntimeError(failure))

        pod_id = str(raw.get("id") or "") if isinstance(raw, dict) else ""
        if not pod_id:
            raise RuntimeError("runpod returned no pod id for this job")

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
        detail = None
        if state == "unknown":
            detail = _UNKNOWN_DETAIL_BY_STATUS.get(_raw_status(raw), _UNRECOGNISED_STATUS_DETAIL)
        return JobStatus(state=state, detail=detail, failure_class=failure_class)

    async def cancel(self, handle: JobHandle) -> JobStatus:
        """Cancel the job by terminating its pod. Terminate is irreversible: the
        spec's deletePod releases the pod's compute and destroys any persistent
        host-local storage (a network volume is only detached). A stopped pod
        keeps its disk until terminate runs, so this call is what releases it.

        The pod's status is read first. The stop call is skipped when that status
        is EXITED, ERROR or TERMINATED, because the spec lists no stop action for
        those statuses and a stop request would return 409. The pod then goes
        straight to terminate. If the status read itself fails, stop is attempted
        as before.

        Both calls that are made are always attempted, so a failed stop does not
        stop the terminate. Returns ``cancelled`` only when every call made
        succeeds. Otherwise it returns ``unknown`` with a detail naming each failed
        step. A call succeeds when RunPod returns no error. The settled pod state
        is read by :meth:`status`, not inferred from these calls.
        """
        if not self.capabilities().can_cancel:
            raise UnsupportedOperation(self.name, "cancel")  # pragma: no cover — always True here
        failures: list[str] = []
        try:
            pod_status = _raw_status(self._client.get_pod(handle.external_id))
        except Exception:  # noqa: BLE001 — an unreadable status is not a cancel failure; stop is then attempted
            pod_status = ""
        if pod_status not in _STOP_REJECTED_STATUSES:
            try:
                self._client.stop_pod(handle.external_id)
            except Exception as exc:  # noqa: BLE001 — sanitize before propagating
                failures.append(f"stop_pod failed: {redact(str(exc))}")
        try:
            self._client.terminate_pod(handle.external_id)
        except Exception as exc:  # noqa: BLE001 — sanitize before propagating
            failures.append(f"terminate_pod failed: {redact(str(exc))}")
        if failures:
            return JobStatus(state="unknown", detail="runpod cancel incomplete: " + "; ".join(failures))
        return JobStatus(state="cancelled")
