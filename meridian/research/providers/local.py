"""f6627d83 — the deterministic local dry-run/in-process provider.

Exists so queueing, retry, cancellation, budgets, and provenance can be
tested end-to-end with NO tunnel, NO cloud account, and NO credentials —
"local operation is first-class" from the sprint item's architecture notes.
This is deliberately a pure in-memory state machine, not a real subprocess
runner: determinism (a test controls exactly when/how a job "finishes")
matters more here than fidelity to a real local-execution environment, and
nothing about the :class:`~meridian.research.providers.base.JobProvider`
contract requires a provider to shell out at all.

Outcome is controlled via ``JobSpec.provider_config["simulate"]``:

* omitted (default) — the job reports ``running`` on its first
  :meth:`status` poll after submission, then ``succeeded`` from the second
  poll onward (a fast, always-green job that still exercises a real
  queued/running/terminal poll loop, not a single-call shortcut).
* ``"fail"`` — reports ``running`` on the first poll, then ``failed`` with
  ``failure_class="user_error"`` from the second poll onward.
* ``"crash"`` — reports ``running`` on the first poll, then ``crashed``
  (``failure_class="infra_error"``) from the second poll onward.
* ``"hang"`` — stays ``running`` forever (never resolves on its own) — the
  shape a real timeout/budget test needs.

Submission is idempotent on ``spec.idempotency_key``: resubmitting the SAME
key returns the SAME :class:`JobHandle` and does not reset simulated
progress — mirrors the "no false success after a submit timeout" contract
at the provider layer, not just the scheduler layer.
"""
from __future__ import annotations

from datetime import datetime, timezone

from meridian.research.providers.base import (
    JobHandle,
    JobProvider,
    JobSpec,
    JobStatus,
    ProviderCapabilities,
    UnsupportedOperation,
)

_VALID_SIMULATIONS = frozenset({"succeed", "fail", "crash", "hang"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class LocalProvider(JobProvider):
    """Deterministic in-process provider. One instance's internal state is
    NOT shared across instances — construct one per scheduler/test scope
    (mirrors a real provider client's own connection-scoped state)."""

    name = "local"

    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}  # idempotency_key -> job state

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            can_cancel=True,
            can_stream_logs=False,
            can_fetch_artifacts=True,
            supports_retries=True,
            supports_gpu=False,
            supports_spot=False,
            supports_preemption_signal=False,
            supports_networking_config=False,
        )

    async def submit(self, spec: JobSpec) -> JobHandle:
        existing = self._jobs.get(spec.idempotency_key)
        if existing is not None:
            return existing["handle"]

        simulate = spec.provider_config.get("simulate", "succeed")
        if simulate not in _VALID_SIMULATIONS:
            raise ValueError(
                f"local provider: provider_config['simulate'] must be one of "
                f"{sorted(_VALID_SIMULATIONS)}, got {simulate!r}"
            )

        handle = JobHandle(
            provider=self.name,
            external_id=f"local-{spec.attempt_id}",
            idempotency_key=spec.idempotency_key,
            submitted_at=_now_iso(),
        )
        self._jobs[spec.idempotency_key] = {
            "handle": handle,
            "simulate": simulate,
            "polls": 0,
            "cancelled": False,
        }
        return handle

    async def status(self, handle: JobHandle) -> JobStatus:
        job = self._jobs.get(handle.idempotency_key)
        if job is None:
            return JobStatus(state="unknown", detail="local provider has no record of this job")
        if job["cancelled"]:
            return JobStatus(state="cancelled")

        job["polls"] += 1
        simulate = job["simulate"]
        if simulate == "hang":
            return JobStatus(state="running")
        if job["polls"] < 2:
            return JobStatus(state="running")
        if simulate == "succeed":
            return JobStatus(
                state="succeeded",
                output_refs=({"path": f"local:///tmp/{handle.external_id}/output"},),
            )
        if simulate == "fail":
            return JobStatus(state="failed", detail="simulated failure", failure_class="user_error")
        if simulate == "crash":
            return JobStatus(state="crashed", detail="simulated crash", failure_class="infra_error")
        return JobStatus(state="unknown")  # pragma: no cover — unreachable, _VALID_SIMULATIONS is exhaustive

    async def cancel(self, handle: JobHandle) -> JobStatus:
        if not self.capabilities().can_cancel:
            raise UnsupportedOperation(self.name, "cancel")  # pragma: no cover — always True here
        job = self._jobs.get(handle.idempotency_key)
        if job is None:
            return JobStatus(state="unknown", detail="local provider has no record of this job")
        job["cancelled"] = True
        return JobStatus(state="cancelled")

    async def fetch_artifacts(self, handle: JobHandle) -> "tuple[dict, ...]":
        job = self._jobs.get(handle.idempotency_key)
        if job is None or job["simulate"] != "succeed" or job["polls"] < 2:
            return ()  # not yet resolved to 'succeeded' — see status()'s poll threshold
        return ({"path": f"local:///tmp/{handle.external_id}/output"},)
