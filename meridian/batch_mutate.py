"""133bfff6 -- batch_mutate: transport-facing wrapper around
meridian.db.batch_management.execute_mixed_mutation_batch.

Mirrors the shape of meridian.batch_ops (627187b8's transport-facing wrapper
around execute_batch) for the SAME reason: request-shape validation
(``mode``/``idempotency_key`` REQUIRED, no silent defaults) lives in ONE
place so a future second transport (HTTP route, stdio) can share it
verbatim rather than re-deriving slightly different wording.

Unlike ``execute_batch``, ``batch_mutate`` has no ``operation`` concept --
each entry names its own ``"kind"`` (``"sprint_item_pointer"``,
``"sprint_item_update"``, or -- PROFILE-7 77369699 -- ``"profile_layer"``,
an upsert of one ``(scope_type, scope_id)`` layer in the PROFILE-1/PROFILE-2
layered profile contract), enforced by
:func:`meridian.db.batch_management.execute_mixed_mutation_batch` itself
(see :data:`meridian.db.batch_management.MIXED_MUTATION_ENTRY_KINDS`).
"""
from __future__ import annotations

import uuid
from typing import Any

from .db import batch_management

#: Re-exported for transport convenience.
BATCH_MODES = batch_management.BATCH_MODES
DEFAULT_MAX_BATCH_ENTRIES = batch_management.DEFAULT_MAX_BATCH_ENTRIES
MIXED_MUTATION_ENTRY_KINDS = batch_management.MIXED_MUTATION_ENTRY_KINDS


class BatchMutateRequestError(ValueError):
    """A transport-level request malformation -- missing/unknown ``mode`` or
    a missing ``idempotency_key`` KEY (its VALUE may be ``None``/``""``).
    Distinct from :class:`batch_management.BatchEngineError`, which fires
    once the request reaches the engine itself (bad/empty/oversized
    ``entries``). Both are ``ValueError`` subclasses.
    """


def validate_batch_mutate_request_shape(payload: "dict[str, Any]") -> None:
    """Enforce batch_mutate's request-shape rules (mirrors
    ``batch_ops.validate_batch_request_shape`` minus the ``operation`` check,
    which does not exist here -- see the module docstring).

    Checks (raises :class:`BatchMutateRequestError` with a client-safe
    message on the first failure):

    * ``mode`` is present and is one of ``batch_management.BATCH_MODES``.
    * ``idempotency_key`` is a key that exists in ``payload`` -- its VALUE
      may be ``None``/``""`` (an explicit opt-out), but the key itself must
      not be simply absent.

    Deliberately does NOT check ``project_id``/``entries`` here -- same
    reasoning as ``batch_ops``: those get the standard sprint-tool wording
    at the handler level.
    """
    mode = payload.get("mode")
    if not mode:
        raise BatchMutateRequestError(
            f"mode is required and must be one of {BATCH_MODES} -- every caller "
            "must explicitly choose all_or_nothing or best_effort; there is no "
            "default at this layer"
        )
    if mode not in BATCH_MODES:
        raise BatchMutateRequestError(f"mode must be one of {BATCH_MODES}, got {mode!r}")
    if "idempotency_key" not in payload:
        raise BatchMutateRequestError(
            "idempotency_key is required -- pass a real key to make retries "
            "safe, or explicitly pass null (or an empty string) to acknowledge "
            "you are opting out of idempotency protection for this call"
        )


def _rollback_status(result: "batch_management.BatchResult") -> str:
    """``"rejected"`` (all_or_nothing, nothing was ever written),
    ``"rolled_back"`` (all_or_nothing, a mutation failed partway through and
    every prior entry in THIS call was compensated), or ``"none"`` (no
    rollback happened -- ``ok``/``partial`` results, or a bare ``best_effort``
    failure where nothing needed compensating since ``best_effort`` never
    rolls back).
    """
    if result.status == "rejected":
        return "rejected"
    if any(r.status == "rolled_back" for r in result.results):
        return "rolled_back"
    return "none"


async def batch_mutate(
    db: Any,
    *,
    project_id: str,
    entries: "list[dict[str, Any]]",
    mode: str,
    idempotency_key: "str | None",
    tenant_id: "str | None" = None,
    actor: "str | None" = None,
    session_id: "str | None" = None,
    max_entries: int = DEFAULT_MAX_BATCH_ENTRIES,
    request_id: "str | None" = None,
) -> "dict[str, Any]":
    """The ONE function a transport calls for ``batch_mutate``.

    Delegates entirely to
    :func:`batch_management.execute_mixed_mutation_batch` (never duplicates
    its validate/apply/compensate logic) and reshapes the returned
    :class:`batch_management.BatchResult` into the response contract this
    item's acceptance criteria ask for: stable per-entry results, a
    ``committed_count``, a ``failures`` list, a ``rollback_status``, and a
    ``request_id`` (generated with :func:`uuid.uuid4` when the caller does
    not supply one, so every call -- including ones that fail validation
    downstream -- can still be correlated in logs/telemetry).

    Raises :class:`batch_management.BatchEngineError` for a call-level
    contract violation (bad ``mode``, empty/oversized ``entries``). Does NOT
    call :func:`validate_batch_mutate_request_shape` itself -- callers are
    expected to call that first against the raw request payload.
    """
    rid = request_id or str(uuid.uuid4())
    result = await batch_management.execute_mixed_mutation_batch(
        db,
        project_id=project_id,
        entries=entries,
        mode=mode,
        idempotency_key=idempotency_key or None,
        tenant_id=tenant_id,
        actor=actor,
        session_id=session_id,
        max_entries=max_entries,
    )
    out = result.to_dict()
    out["request_id"] = rid
    out["committed_count"] = sum(1 for r in result.results if r.status == "ok")
    out["failures"] = [r.to_dict() for r in result.results if r.status == "error"]
    out["rollback_status"] = _rollback_status(result)
    return out
