"""f6627d83 — the provider-neutral job contract: JobSpec / JobHandle /
JobStatus / ProviderCapabilities, and the ``JobProvider`` base class every
adapter (:mod:`meridian.research.providers.local`,
:mod:`meridian.research.providers.runpod`, and any future SkyPilot/Parsl/
Slurm/LSF/AWS/SSH/HPC adapter) implements.

REUSE, NOT REINVENTION: this module has NO opinion on identity or
persistence. A :class:`JobSpec` names an existing ``project_id`` /
``experiment_id`` / ``run_id`` / ``attempt_id`` from
:mod:`meridian.db.experiment_model` — it does not mint a second identity
system, a second queue table, or a second artifact ledger. See
:mod:`meridian.research.scheduler` for the layer that actually binds a
provider's observed :class:`JobStatus` back onto a
:class:`meridian.db.experiment_model` ``RunAttempt`` via
``transition_attempt``.

NO PROVIDER-SPECIFIC FIELDS ON JobSpec: RunPod (or Slurm, or SkyPilot, or
anything else) -specific configuration lives ENTIRELY in
``JobSpec.provider_config`` — an opaque dict only the target adapter
interprets. Every other field on :class:`JobSpec` is meaningful to every
adapter (even if a given adapter ignores some of them, e.g. the local
provider ignoring ``image``).

CAPABILITY-GATED, FAIL-CLOSED OPERATIONS: :class:`ProviderCapabilities`
declares what an adapter can actually do. :class:`JobProvider`'s base
``cancel``/``fetch_logs``/``fetch_artifacts`` methods check the relevant
capability flag and raise :class:`UnsupportedOperation` when it's False —
a caller never has to guess whether an operation is safe to attempt, and an
adapter that hasn't implemented an operation yet cannot silently no-op as if
it succeeded. Adapters override these methods only when they DO support the
operation; the fail-closed default lives here, once.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from meridian.experiment_model import ATTEMPT_STATUSES, FAILURE_CLASSES

#: The exact same closed vocabulary RunAttempt uses (see
#: meridian.experiment_model.ATTEMPT_STATUSES) — a provider's observed job
#: state maps 1:1 onto an attempt's status, never a second parallel
#: vocabulary a caller would have to translate between.
JOB_STATES: frozenset[str] = ATTEMPT_STATUSES


class UnsupportedOperation(NotImplementedError):
    """Raised when a caller invokes a :class:`JobProvider` operation the
    provider's :class:`ProviderCapabilities` declares it does not support.
    A ``NotImplementedError`` subclass so ``except NotImplementedError``
    keeps working for a caller that hasn't been updated to catch this
    specific type, while a caller that wants the capability context can
    catch this type and read :attr:`operation`/:attr:`provider`."""

    def __init__(self, provider: str, operation: str):
        self.provider = provider
        self.operation = operation
        super().__init__(
            f"provider {provider!r} does not support {operation!r} "
            f"(see its capabilities())"
        )


@dataclass(frozen=True)
class ResourceRequest:
    """Provider-neutral resource/timeout/budget ask. Every field is
    OPTIONAL and advisory to a given provider — the local dry-run provider
    ignores cpu/memory/gpu entirely; ``timeout_seconds`` and ``max_retries``
    are honored by :mod:`meridian.research.scheduler` regardless of
    provider, since bounded execution is a scheduler-level guarantee, not a
    per-provider one."""

    cpu: "float | None" = None
    memory_gb: "float | None" = None
    gpu_count: int = 0
    gpu_type: "str | None" = None
    timeout_seconds: "int | None" = None
    max_retries: int = 0


@dataclass(frozen=True)
class JobSpec:
    """Provider-neutral description of one unit of work to submit.

    ``idempotency_key`` is REQUIRED (not optional): every provider's
    :meth:`JobProvider.submit` must be safe to call twice with the same key
    and return the SAME :class:`JobHandle` both times — "no false success
    after a submit timeout" depends on this (a caller that times out
    waiting for a submit response can safely resubmit).

    ``provider_config`` is the ONLY place provider-specific configuration
    belongs (acceptance criterion: "no RunPod-specific fields leaking into
    the core JobSpec"). A provider that doesn't recognize a key in it
    should ignore that key, not raise.
    """

    project_id: str
    experiment_id: str
    run_id: str
    attempt_id: str
    idempotency_key: str
    command: "tuple[str, ...]"
    image: "str | None" = None
    env: "dict[str, str]" = field(default_factory=dict)
    inputs: "tuple[dict[str, Any], ...]" = ()
    resources: ResourceRequest = field(default_factory=ResourceRequest)
    budget_usd: "float | None" = None
    provider_config: "dict[str, Any]" = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (self.idempotency_key or "").strip():
            raise ValueError("JobSpec requires a non-empty idempotency_key")
        if not self.command:
            raise ValueError("JobSpec requires a non-empty command")


@dataclass(frozen=True)
class JobHandle:
    """A provider's own reference to a submitted job — opaque to everything
    except that same provider. ``external_id`` is whatever the provider
    calls it (a pod id, a job id, a PID) and is never interpreted outside
    the adapter that minted it."""

    provider: str
    external_id: str
    idempotency_key: str
    submitted_at: str


@dataclass(frozen=True)
class JobStatus:
    """One provider status observation. ``state`` MUST be one of
    :data:`JOB_STATES`. ``state='unknown'`` is a first-class, truthful
    answer (see :mod:`meridian.experiment_model`'s module docstring) — a
    provider that cannot currently determine a job's real state should
    return this rather than guessing.

    ``failure_class``, when the provider can classify a failure (see e.g.
    ``runpod._map_runpod_status``), MUST be one of
    ``meridian.experiment_model.FAILURE_CLASSES`` and is only meaningful
    when ``state`` is ``'failed'``/``'crashed'``. A provider that cannot
    classify a failure it observed should leave this ``None`` — the
    scheduler (:mod:`meridian.research.scheduler`) defaults an
    unclassified failure to ``'unknown'`` when it applies the transition,
    rather than every provider having to know that fallback exists.

    ``provider_raw`` is the provider's own raw status payload, kept ONLY
    for diagnostics — a caller persisting it must redact it first (see
    :mod:`meridian.secret_redaction`); no provider adapter in this package
    persists it directly.
    """

    state: str
    detail: "str | None" = None
    failure_class: "str | None" = None
    logs_ref: "dict[str, Any] | None" = None
    output_refs: "tuple[dict[str, Any], ...]" = ()
    provider_raw: "dict[str, Any] | None" = None

    def __post_init__(self) -> None:
        if self.state not in JOB_STATES:
            raise ValueError(f"JobStatus.state must be one of {sorted(JOB_STATES)}, got {self.state!r}")
        if self.failure_class is not None and self.failure_class not in FAILURE_CLASSES:
            raise ValueError(
                f"JobStatus.failure_class must be one of {sorted(FAILURE_CLASSES)} or None, "
                f"got {self.failure_class!r}"
            )


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a provider can actually do. Every operation
    :class:`JobProvider` exposes beyond ``submit``/``status`` is gated on
    one of these flags — see the module docstring's "fail-closed" note."""

    can_cancel: bool = False
    can_stream_logs: bool = False
    can_fetch_artifacts: bool = False
    supports_retries: bool = False
    supports_gpu: bool = False
    supports_spot: bool = False
    supports_preemption_signal: bool = False
    supports_networking_config: bool = False


class JobProvider(abc.ABC):
    """Base class every provider adapter implements. ``name`` identifies the
    provider in stored :class:`JobHandle`/attempt provenance records (e.g.
    ``"local"``, ``"runpod"``) — set it as a class attribute on a subclass.
    """

    name: str = "unset"

    @abc.abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """Return this provider's fixed capability declaration."""

    @abc.abstractmethod
    async def submit(self, spec: JobSpec) -> JobHandle:
        """Submit ``spec`` for execution. MUST be idempotent on
        ``spec.idempotency_key`` (see :class:`JobSpec`'s docstring)."""

    @abc.abstractmethod
    async def status(self, handle: JobHandle) -> JobStatus:
        """Return the current observed status of ``handle``. Return
        ``JobStatus(state='unknown', ...)`` rather than raising when the
        real state genuinely cannot be determined right now — a transient
        polling failure is not the same fact as "the job failed"."""

    async def cancel(self, handle: JobHandle) -> JobStatus:
        """Request cancellation. Raises :class:`UnsupportedOperation` unless
        :meth:`capabilities` declares ``can_cancel=True`` — override this in
        a subclass that actually supports it (calling ``super().cancel()``
        first is NOT required; the base implementation exists purely to
        supply this fail-closed default)."""
        if not self.capabilities().can_cancel:
            raise UnsupportedOperation(self.name, "cancel")
        raise NotImplementedError(
            f"provider {self.name!r} declares can_cancel=True but did not override cancel()"
        )

    async def fetch_logs(self, handle: JobHandle) -> "dict[str, Any]":
        """Fetch a logs reference. Raises :class:`UnsupportedOperation`
        unless :meth:`capabilities` declares ``can_stream_logs=True``."""
        if not self.capabilities().can_stream_logs:
            raise UnsupportedOperation(self.name, "fetch_logs")
        raise NotImplementedError(
            f"provider {self.name!r} declares can_stream_logs=True but did not override fetch_logs()"
        )

    async def fetch_artifacts(self, handle: JobHandle) -> "tuple[dict[str, Any], ...]":
        """Fetch output/artifact references. Raises
        :class:`UnsupportedOperation` unless :meth:`capabilities` declares
        ``can_fetch_artifacts=True``."""
        if not self.capabilities().can_fetch_artifacts:
            raise UnsupportedOperation(self.name, "fetch_artifacts")
        raise NotImplementedError(
            f"provider {self.name!r} declares can_fetch_artifacts=True but did not override fetch_artifacts()"
        )
