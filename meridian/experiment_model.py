"""4376e655 — the provider-neutral Experiment/Run/RunAttempt state model.

Meridian's "research OS" needs one durable, project-scoped identity for a
research job that is completely independent of WHERE that job executes (a
local dry-run, RunPod, Slurm, a researcher's own laptop). This module is the
LEAF half of that contract: closed vocabularies, state-transition rules, and
pure helpers with no DB import — mirroring :mod:`meridian.research_graph`'s
own "no opinion on how a caller obtained data" split from
:mod:`meridian.db.experiment_model` (the persistence layer built on top of
this one).

Three levels of identity, deliberately kept separate:

* :class:`Experiment` — a named research question / project of work. Many
  runs over time belong to one experiment (e.g. "does dropout=0.3 help").
* :class:`Run` (``research_runs``) — one logical execution of an experiment
  with one fixed set of parameters. A run's identity (its ``id``) is
  IMMUTABLE and never reused; retrying a failed run does not create a new
  run row (see ``idempotency_key`` below) — it creates a new
  :class:`RunAttempt` under the SAME run.
* :class:`RunAttempt` (``research_run_attempts``) — one concrete attempt to
  execute a run (attempt 1, attempt 2 after a retry, ...). This is where
  actual outcome state lives: ``status``, ``failure_class``,
  ``checkpoint_ref``, ``artifact_refs``, ``provenance_ref``. A run's
  overall status is ALWAYS derived from its latest attempt (see
  :mod:`meridian.db.experiment_model`'s ``get_run``) — never independently
  stored — so restart recovery and handoff serialization re-derive live
  state instead of replaying a possibly-stale cached column.

This module does not talk to a provider, a scheduler, or a queue — see the
follow-on EXP-SCHEDULER / EXP-DELEGATED sprint items for that. It also does
not duplicate :mod:`meridian.research_graph`'s node/edge graph or the
``meridian-outputs`` extension's artifact registry/provenance systems: a
:class:`RunAttempt`'s ``provenance_ref``/``artifact_refs`` are OPAQUE
pointers into those existing systems (e.g. a research_graph ``run`` node's
identity key via :func:`meridian.research_graph.run_identity_key`, or an
artifact registry output id), never a second copy of their data.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

#: Every state a :class:`RunAttempt` can be in. ``unknown`` is a first-class
#: outcome (not an error) for the case restart recovery cares about most:
#: an attempt whose true fate can no longer be observed (the process that
#: was tracking it died, a heartbeat went stale) — reported truthfully
#: rather than guessed as ``failed``.
ATTEMPT_STATUSES: frozenset[str] = frozenset(
    {"queued", "running", "succeeded", "failed", "cancelled", "crashed", "unknown"}
)

#: Statuses that represent "this attempt is done and will not change again
#: on its own" — reached only via an explicit transition, and (except via
#: reconciliation, see :data:`_ALLOWED_ATTEMPT_TRANSITIONS`) never left.
ATTEMPT_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "failed", "cancelled", "crashed"}
)

#: Closed failure-classification vocabulary. Required on a transition INTO
#: ``failed``/``crashed`` (a caller must say why), forbidden otherwise (an
#: attempt that never failed has no failure class to report).
FAILURE_CLASSES: frozenset[str] = frozenset(
    {"user_error", "infra_error", "timeout", "oom", "preempted", "dependency_error", "unknown"}
)

#: Legal ``from_status -> {to_status, ...}`` transitions. A transition to the
#: SAME status is always included (idempotent no-op — a caller retrying a
#: transition call after a network blip must not get a ValueError). Terminal
#: statuses transition only to themselves, with one deliberate exception:
#: ``unknown`` (itself not terminal) can be RECONCILED into any real outcome
#: once it becomes observable again — restart recovery marks a
#: since-abandoned attempt ``unknown``, and a later reconciliation pass (or a
#: human) can resolve it to what actually happened.
_ALLOWED_ATTEMPT_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"queued", "running", "cancelled", "unknown"}),
    "running": frozenset({"running", "succeeded", "failed", "crashed", "cancelled", "unknown"}),
    "unknown": frozenset({"unknown", "queued", "running", "succeeded", "failed", "crashed", "cancelled"}),
    "succeeded": frozenset({"succeeded"}),
    "failed": frozenset({"failed"}),
    "cancelled": frozenset({"cancelled"}),
    "crashed": frozenset({"crashed"}),
}


def validate_attempt_status(raw: object) -> str:
    """Return ``raw`` stripped/lowercased if it's one of :data:`ATTEMPT_STATUSES`.

    Raises ``ValueError`` naming the full closed set otherwise — mirrors
    ``meridian.research_graph.validate_node_type``'s exact contract.
    """
    value = (raw or "").strip().lower() if isinstance(raw, str) else ""
    if value not in ATTEMPT_STATUSES:
        raise ValueError(f"status must be one of {sorted(ATTEMPT_STATUSES)}, got {raw!r}")
    return value


def validate_failure_class(raw: object) -> str:
    """Return ``raw`` stripped/lowercased if it's one of :data:`FAILURE_CLASSES`.

    Raises ``ValueError`` naming the full closed set otherwise.
    """
    value = (raw or "").strip().lower() if isinstance(raw, str) else ""
    if value not in FAILURE_CLASSES:
        raise ValueError(f"failure_class must be one of {sorted(FAILURE_CLASSES)}, got {raw!r}")
    return value


def validate_attempt_transition(current: str, new_status: str) -> str:
    """Validate that ``current -> new_status`` is a legal attempt transition.

    Returns the normalized ``new_status`` on success. Raises ``ValueError``
    (naming both states and the allowed destinations) on an illegal jump —
    e.g. ``succeeded -> running`` (a terminal outcome does not un-happen) or
    a hop straight from ``queued`` to ``succeeded`` (must pass through
    ``running`` first, or be explicitly reconciled from ``unknown``).
    """
    current = validate_attempt_status(current)
    new_status = validate_attempt_status(new_status)
    allowed = _ALLOWED_ATTEMPT_TRANSITIONS.get(current, frozenset())
    if new_status not in allowed:
        raise ValueError(
            f"illegal attempt transition {current!r} -> {new_status!r}; "
            f"from {current!r} only {sorted(allowed)} is allowed"
        )
    return new_status


def is_terminal_status(status: str) -> bool:
    """True when ``status`` is one of :data:`ATTEMPT_TERMINAL_STATUSES`."""
    return validate_attempt_status(status) in ATTEMPT_TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Parameters/config fingerprint
# ---------------------------------------------------------------------------


def params_fingerprint(params: "dict[str, Any] | None") -> "str | None":
    """A deterministic ``sha256:...`` fingerprint of a run's parameters.

    Canonical JSON (``sort_keys=True``, ``ensure_ascii=False``) so the SAME
    logical parameter set always hashes to the SAME fingerprint regardless
    of key insertion order — mirrors ``meridian.research_graph``'s own
    canonical-JSON convention for ``external_ref``. Returns ``None`` for
    ``None``/empty input (a run with no parameters has no fingerprint to
    report, not a fingerprint of ``"{}"``).
    """
    if not params:
        return None
    canonical = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
