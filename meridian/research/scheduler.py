"""f6627d83 — the provider-neutral research job scheduler: the layer that
binds a :mod:`meridian.research.providers.base` ``JobProvider`` to an
existing :mod:`meridian.db.experiment_model` ``RunAttempt``.

REUSE, NOT REINVENTION (acceptance criterion 1): this module owns NO state
of its own — no second queue table, no second ledger. Every function here
reads/writes the SAME ``research_run_attempts`` rows
:mod:`meridian.db.experiment_model` already defines, via its existing
``transition_attempt``/``get_attempt`` functions. A job's provider handle is
recorded in the attempt's existing ``provenance_ref`` JSON field (acceptance
criterion 6: "a job attempt can emit a canonical run/provenance receipt ...
without claiming completion when the provider is partial or unknown") —
there is no separate ``jobs`` table.

"JUST RUN IT" AS BOUNDED EXECUTION:

* :func:`submit_run_attempt` validates the spec against the provider's
  declared :class:`~meridian.research.providers.base.ProviderCapabilities`
  BEFORE calling ``provider.submit`` (e.g. requesting a GPU from a provider
  that doesn't support one is rejected up front, not discovered later as a
  confusing runtime failure), and enforces ``spec.budget_usd`` via
  :class:`BudgetExceeded`.
* The attempt is transitioned ``queued -> running`` ONLY AFTER
  ``provider.submit`` returns successfully. If ``submit`` raises — including
  a timeout — the attempt is left ``queued``, untouched: resubmitting with
  the SAME ``idempotency_key`` is always safe. This is "no false success
  after a submit timeout" made concrete.
* :func:`poll_run_attempt` maps a provider's observed
  :class:`~meridian.research.providers.base.JobStatus` onto the attempt's
  next transition. An illegal transition (the provider reports something
  "behind" what Meridian already knows, e.g. a stale ``queued`` after the
  attempt is already ``running``) is treated as a benign no-op — the poll
  returns the attempt UNCHANGED rather than raising, since a provider's
  eventually-consistent status feed disagreeing with already-known-fresher
  state is not itself an error condition.
* :func:`check_attempt_timeout` is the "no false success after a submit
  timeout" guarantee extended to the RUNNING side: an attempt that has
  exceeded ``resources.timeout_seconds`` is transitioned to ``'unknown'``
  (never guessed as ``'failed'``) and — best-effort, if the provider
  supports it — asked to cancel.
* :func:`should_retry` classifies whether a terminal failure is worth
  retrying (transient infra/timeout/preemption/oom/unknown VS a genuine
  user/dependency error retrying will not fix), bounded by
  ``resources.max_retries``.

RESTART RECOVERY composes directly with item 1's
:func:`meridian.db.experiment_model.reconcile_stale_attempts` — nothing
here reimplements it. An attempt reconciled to ``'unknown'`` after a
restart can still be resolved forward by a later :func:`poll_run_attempt`
call (``unknown`` accepts a transition to any real outcome — see
``meridian.experiment_model``'s transition table).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from meridian.db.experiment_model import get_attempt, transition_attempt
from meridian.experiment_model import validate_attempt_transition
from meridian.research.providers.base import (
    JobHandle,
    JobProvider,
    JobSpec,
    UnsupportedOperation,
)

#: Failure classes worth retrying (transient) vs. not (retrying a genuine
#: user/dependency error just reproduces the same failure).
_RETRYABLE_FAILURE_CLASSES = frozenset({"infra_error", "timeout", "preempted", "oom", "unknown"})


class BudgetExceeded(ValueError):
    """Raised by :func:`submit_run_attempt` when ``spec.budget_usd`` would
    be exceeded. Carries the two figures that triggered it."""

    def __init__(self, budget_usd: float, spent_usd: float):
        self.budget_usd = budget_usd
        self.spent_usd = spent_usd
        super().__init__(
            f"budget exceeded: spent_usd={spent_usd} >= budget_usd={budget_usd}"
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _handle_from_provenance(provenance_ref: "dict[str, Any] | None") -> "JobHandle | None":
    if not isinstance(provenance_ref, dict) or "job_handle" not in provenance_ref:
        return None
    raw = provenance_ref["job_handle"]
    try:
        return JobHandle(
            provider=raw["provider"],
            external_id=raw["external_id"],
            idempotency_key=raw["idempotency_key"],
            submitted_at=raw["submitted_at"],
        )
    except (KeyError, TypeError):
        return None


def _handle_to_provenance(handle: JobHandle) -> "dict[str, Any]":
    return {
        "job_handle": {
            "provider": handle.provider,
            "external_id": handle.external_id,
            "idempotency_key": handle.idempotency_key,
            "submitted_at": handle.submitted_at,
        }
    }


async def submit_run_attempt(
    db,
    project_id: str,
    attempt_id: str,
    provider: JobProvider,
    spec: JobSpec,
    *,
    spent_usd: float = 0.0,
) -> dict[str, Any]:
    """Validate ``spec`` against ``provider``'s capabilities and budget,
    submit it, and transition the attempt ``queued -> running`` recording
    the returned :class:`JobHandle` in the attempt's ``provenance_ref``.

    Idempotent: if the attempt is already ``running`` with a
    ``provenance_ref`` job handle whose ``idempotency_key`` matches
    ``spec.idempotency_key``, returns the attempt UNCHANGED rather than
    resubmitting — a caller retrying after losing the response to a prior
    successful submit gets the same answer, not a second job.

    Raises ``ValueError`` if the attempt isn't in ``queued`` state (and
    isn't the idempotent-replay case above), if ``spec`` asks for a
    capability ``provider`` doesn't declare (e.g. a GPU from a provider with
    ``supports_gpu=False``), or :class:`BudgetExceeded` if
    ``spec.budget_usd`` would be exceeded. ``provider.submit`` raising
    (including a timeout) propagates AS-IS and leaves the attempt ``queued``
    — see the module docstring.
    """
    attempt = await get_attempt(db, project_id, attempt_id)
    if attempt is None:
        raise ValueError(f"run attempt {attempt_id!r} not found in project {project_id!r}")

    if attempt["status"] == "running":
        existing_handle = _handle_from_provenance(attempt.get("provenance_ref"))
        if existing_handle is not None and existing_handle.idempotency_key == spec.idempotency_key:
            return attempt
        raise ValueError(
            f"run attempt {attempt_id!r} is already 'running' under a different submission"
        )
    if attempt["status"] != "queued":
        raise ValueError(
            f"run attempt {attempt_id!r} must be 'queued' to submit, is {attempt['status']!r}"
        )

    caps = provider.capabilities()
    if spec.resources.gpu_count > 0 and not caps.supports_gpu:
        raise ValueError(f"provider {provider.name!r} does not support GPU resources (supports_gpu=False)")

    if spec.budget_usd is not None and spent_usd >= spec.budget_usd:
        raise BudgetExceeded(spec.budget_usd, spent_usd)

    handle = await provider.submit(spec)

    return await transition_attempt(
        db, project_id, attempt_id, "running",
        provenance_ref=_handle_to_provenance(handle),
    )


async def poll_run_attempt(db, project_id: str, attempt_id: str, provider: JobProvider) -> dict[str, Any]:
    """Poll ``provider`` for the attempt's job and apply the resulting
    transition. A provider status that would be an ILLEGAL transition from
    the attempt's current state (a stale/out-of-order observation) is a
    benign no-op: the attempt is returned unchanged, not an error.

    An unclassified failure (``JobStatus.failure_class is None`` while
    ``state`` is ``'failed'``/``'crashed'``) defaults to failure_class
    ``'unknown'`` — every provider observation of a failure ends up with
    SOME classification, even when the provider itself couldn't supply one.
    """
    attempt = await get_attempt(db, project_id, attempt_id)
    if attempt is None:
        raise ValueError(f"run attempt {attempt_id!r} not found in project {project_id!r}")

    handle = _handle_from_provenance(attempt.get("provenance_ref"))
    if handle is None:
        raise ValueError(f"run attempt {attempt_id!r} has no recorded job handle to poll")

    job_status = await provider.status(handle)

    try:
        validate_attempt_transition(attempt["status"], job_status.state)
    except ValueError:
        return attempt  # stale/out-of-order observation — benign no-op

    kwargs: dict[str, Any] = {}
    if job_status.state in ("failed", "crashed"):
        kwargs["failure_class"] = job_status.failure_class or "unknown"
    if job_status.detail is not None:
        kwargs["error_message"] = job_status.detail
    if job_status.output_refs:
        kwargs["artifact_refs"] = list(job_status.output_refs)

    return await transition_attempt(db, project_id, attempt_id, job_status.state, **kwargs)


async def cancel_run_attempt(db, project_id: str, attempt_id: str, provider: JobProvider) -> dict[str, Any]:
    """Cancel a live attempt. Fails closed (raises
    :class:`~meridian.research.providers.base.UnsupportedOperation`) if the
    provider doesn't support cancellation — never silently treats the
    attempt as cancelled when the provider itself cannot confirm it.

    Checks the attempt's OWN status BEFORE calling ``provider.cancel`` (same
    guard :func:`check_attempt_timeout` already applies): an attempt Meridian
    already knows is terminal is rejected with ``ValueError`` up front,
    rather than issuing a live cancel call (e.g. RunPod ``stop_pod``/
    ``terminate_pod``) against a job that has already finished.
    """
    attempt = await get_attempt(db, project_id, attempt_id)
    if attempt is None:
        raise ValueError(f"run attempt {attempt_id!r} not found in project {project_id!r}")
    if attempt["status"] not in ("queued", "running"):
        raise ValueError(
            f"cannot cancel run attempt {attempt_id!r}: already terminal ({attempt['status']!r})"
        )

    handle = _handle_from_provenance(attempt.get("provenance_ref"))
    if handle is None:
        raise ValueError(f"run attempt {attempt_id!r} has no recorded job handle to cancel")

    job_status = await provider.cancel(handle)  # raises UnsupportedOperation if unsupported
    return await transition_attempt(db, project_id, attempt_id, job_status.state)


async def check_attempt_timeout(
    db,
    project_id: str,
    attempt_id: str,
    *,
    timeout_seconds: "int | None",
    provider: "JobProvider | None" = None,
) -> "dict[str, Any] | None":
    """If the attempt is ``queued``/``running`` and has exceeded
    ``timeout_seconds`` since its ``started_at`` (or ``created_at`` if it
    never started), transition it to ``'unknown'`` — a timeout is neither a
    false success nor a guessed failure — and, best-effort, ask ``provider``
    to cancel it (any :class:`UnsupportedOperation`/error from that is
    swallowed; the truthful ``'unknown'`` transition is what matters).
    Returns the updated attempt, or ``None`` if no timeout applied
    (``timeout_seconds`` is ``None``, or the attempt hasn't exceeded it).
    """
    if timeout_seconds is None:
        return None
    attempt = await get_attempt(db, project_id, attempt_id)
    if attempt is None:
        raise ValueError(f"run attempt {attempt_id!r} not found in project {project_id!r}")
    if attempt["status"] not in ("queued", "running"):
        return None

    reference = attempt.get("started_at") or attempt.get("created_at")
    if not reference:
        return None
    reference_dt = datetime.strptime(reference, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - reference_dt).total_seconds()
    if elapsed < timeout_seconds:
        return None

    if provider is not None:
        handle = _handle_from_provenance(attempt.get("provenance_ref"))
        if handle is not None:
            try:
                await provider.cancel(handle)
            except Exception:  # noqa: BLE001 — best-effort; the 'unknown' transition below is what matters
                pass

    return await transition_attempt(db, project_id, attempt_id, "unknown")


def should_retry(attempt: "dict[str, Any]", *, max_retries: int) -> bool:
    """Whether a terminal, failed/crashed attempt is worth retrying:
    ``attempt_number <= max_retries`` (so ``max_retries=0`` means no retry
    at all) AND its ``failure_class`` looks transient (see
    :data:`_RETRYABLE_FAILURE_CLASSES`) rather than a genuine user/
    dependency error a retry cannot fix. Returns ``False`` for any
    non-terminal or non-failure status — there is nothing to retry."""
    if attempt["status"] not in ("failed", "crashed"):
        return False
    if attempt["attempt_number"] > max_retries:
        return False
    return attempt.get("failure_class") in _RETRYABLE_FAILURE_CLASSES


def next_retry_delay_seconds(attempt_number: int, *, base: float = 2.0, cap: float = 300.0) -> float:
    """Exponential backoff: ``base * 2**(attempt_number - 1)``, capped at
    ``cap`` seconds. ``attempt_number`` is 1-based (matches
    ``RunAttempt.attempt_number``)."""
    return min(cap, base * (2 ** max(0, attempt_number - 1)))
