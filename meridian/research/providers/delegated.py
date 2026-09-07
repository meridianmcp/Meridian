"""0b7bb873 — the delegated ``JobProvider`` wrapper: symmetric access to an
already-attached external job (see :mod:`meridian.research.external_jobs`
for how a job actually GETS attached — this module never submits one).

WHY submit() ALWAYS RAISES: a delegated job is, by definition, one the user
submitted through their own launcher — Meridian attaching to it
(:func:`meridian.research.external_jobs.attach_external_job`) is a
different operation from :class:`~meridian.research.providers.base.
JobProvider.submit`, which means "Meridian, please launch this." Exposing a
``submit`` that secretly does something else would violate the base
contract's meaning; :meth:`DelegatedProvider.submit` raises
:class:`~meridian.research.providers.base.UnsupportedOperation`
unconditionally instead.

OPTIONAL POLL/CANCEL/LOGS, TRUTHFULLY DEGRADED: many delegated launchers
(a plain SSH script, an ad-hoc cluster wrapper) have no API Meridian could
poll even in principle. :class:`DelegatedProvider` takes OPTIONAL injected
callables for status/cancel/logs; when one is omitted, the corresponding
operation degrades to the truthful "unavailable" answer — ``status``
returns ``state='unknown'`` (never guesses ``'failed'``), ``cancel``/
``fetch_logs`` raise ``UnsupportedOperation`` (the SAME fail-closed
contract :class:`~meridian.research.providers.base.JobProvider`'s base
class already uses for f6627d83's RunPod/local adapters) — never an error
that implies the job itself failed.
"""
from __future__ import annotations

from meridian.research.providers.base import (
    JobHandle,
    JobProvider,
    JobSpec,
    JobStatus,
    ProviderCapabilities,
    UnsupportedOperation,
)
from meridian.secret_redaction import redact


class DelegatedProvider(JobProvider):
    """See module docstring. ``status_poller``/``cancel_fn``/``logs_fn`` are
    optional ``async def (handle: JobHandle) -> ...`` callables a caller
    injects when a given launcher DOES expose a way to check on/cancel/read
    logs for an already-submitted job (e.g. shelling out to ``squeue``/
    ``scancel`` for Slurm); omit whichever ones aren't available for the
    launcher in play."""

    name = "delegated"

    def __init__(self, *, status_poller=None, cancel_fn=None, logs_fn=None) -> None:
        self._status_poller = status_poller
        self._cancel_fn = cancel_fn
        self._logs_fn = logs_fn

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            can_cancel=self._cancel_fn is not None,
            can_stream_logs=self._logs_fn is not None,
            can_fetch_artifacts=False,
            supports_retries=False,  # retries, if any, are performed BY the external scheduler
            supports_gpu=True,
            supports_spot=True,
            supports_preemption_signal=True,
            supports_networking_config=True,  # topology is externally managed, not Meridian's to configure
        )

    async def submit(self, spec: JobSpec) -> JobHandle:
        raise UnsupportedOperation(
            self.name,
            "submit (delegated jobs are attached via meridian.research.external_jobs.attach_external_job, never submitted)",
        )

    async def status(self, handle: JobHandle) -> JobStatus:
        if self._status_poller is None:
            return JobStatus(state="unknown", detail="delegated: no status poller configured for this launcher")
        try:
            return await self._status_poller(handle)
        except Exception as exc:  # noqa: BLE001 — a poll failure is 'unknown', not 'failed'
            return JobStatus(state="unknown", detail=f"delegated status poll failed: {redact(str(exc))}")

    async def cancel(self, handle: JobHandle) -> JobStatus:
        if self._cancel_fn is None:
            raise UnsupportedOperation(self.name, "cancel")
        try:
            return await self._cancel_fn(handle)
        except Exception as exc:  # noqa: BLE001 — sanitize before propagating
            return JobStatus(state="unknown", detail=f"delegated cancel failed: {redact(str(exc))}")

    async def fetch_logs(self, handle: JobHandle) -> "dict":
        if self._logs_fn is None:
            raise UnsupportedOperation(self.name, "fetch_logs")
        return await self._logs_fn(handle)
