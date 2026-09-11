"""Durable project-scoped Experiment Registry (W1-M, item 3f6b8715).

Mirrors :mod:`meridian.db.external_jobs`'s exact pattern (read that file
first): a self-contained module, its own local ``_row_to_dict``, ``uuid``
used directly for id generation (no import from ``meridian.db.__init__``,
avoiding a package-init import cycle).

See :mod:`meridian.experiment` for field validation and, IMPORTANT, its
module docstring's notes on how this registry coexists with the
pre-existing ``experiments`` table (:mod:`meridian.db.experiment_model`,
4376e655) and on the deliberate deviations from the sprint-item brief.

HARD INVARIANT (dedicated test: tests/test_experiments.py)
--------------------------------------------------------------------------
No run may ever reach a terminal status (``completed``/``abandoned``/
``expired``) without a corresponding ``experiment_events`` row recorded for
that ``run_id``. Every terminal-transition function below
(:func:`complete_experiment_run`, :func:`expire_stale_runs`) unconditionally
writes one as part of the SAME call -- never left to a caller to remember.
Silent abandonment of a run is not allowed.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from meridian import experiment as model

_EXPERIMENT_COLUMNS = (
    "id", "project_id", "name", "config_template", "created_by",
    "hypothesis", "status", "creator_session_id", "created_at", "updated_at",
)
_RUN_COLUMNS = (
    "id", "experiment_id", "project_id", "repository_id", "worktree_id",
    "status", "trial_label", "outcome_summary", "disposition",
    "pivot_parent_run_id", "resource_profile_json", "result_receipt_json",
    "creator_session_id", "started_at", "completed_at", "expires_at",
    "created_at", "updated_at",
)
_ARTIFACT_COLUMNS = (
    "id", "project_id", "experiment_run_id", "logical_path", "content_hash",
    "artifact_role", "host_visibility", "last_verified_at", "created_at",
)
_EVENT_COLUMNS = (
    "id", "experiment_id", "run_id", "event_type", "label", "body",
    "artifact_ids_json", "created_by_session_id", "created_at",
)


def _row_to_dict(row: Any, columns: "tuple[str, ...]") -> "dict[str, Any] | None":
    if row is None:
        return None
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(zip(columns, row))


def _json_loads(raw: "str | None") -> Any:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _json_dumps(value: "Any | None") -> "str | None":
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if value is not None else None


def _decode_experiment(row: Any) -> "dict[str, Any] | None":
    result = _row_to_dict(row, _EXPERIMENT_COLUMNS)
    if result is None:
        return None
    result["config_template"] = _json_loads(result.get("config_template"))
    return result


def _decode_run(row: Any) -> "dict[str, Any] | None":
    result = _row_to_dict(row, _RUN_COLUMNS)
    if result is None:
        return None
    result["resource_profile"] = _json_loads(result.pop("resource_profile_json", None))
    result["result_receipt"] = _json_loads(result.pop("result_receipt_json", None))
    return result


def _decode_artifact(row: Any) -> "dict[str, Any] | None":
    return _row_to_dict(row, _ARTIFACT_COLUMNS)


def _decode_event(row: Any) -> "dict[str, Any] | None":
    result = _row_to_dict(row, _EVENT_COLUMNS)
    if result is None:
        return None
    result["artifact_ids"] = _json_loads(result.pop("artifact_ids_json", None)) or []
    return result


async def _require_session(db: Any, project_id: str, session_id: "str | None") -> None:
    if not session_id:
        raise ValueError("session_id is required for experiment-registry writes")
    async with db.execute(
        "SELECT id FROM sessions WHERE id = ? AND project_id = ?",
        (session_id, project_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise ValueError(f"session {session_id!r} does not belong to project {project_id!r}")


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------


async def create_experiment(
    db: Any,
    project_id: str,
    session_id: "str | None",
    *,
    name: str,
    hypothesis: "str | None" = None,
) -> "dict[str, Any]":
    """Create a new experiment scoped to ``project_id``.

    Writes ONLY the new columns (``hypothesis``, ``status``,
    ``creator_session_id``) plus the identity columns shared with the
    pre-existing table -- ``config_template``/``created_by`` are left NULL,
    exactly as meridian.experiment_model's own create_experiment leaves
    ``hypothesis``/``status``/``creator_session_id`` NULL. Neither interface
    ever overwrites the other's columns.
    """
    project_id = (project_id or "").strip()
    if not project_id:
        raise ValueError("create_experiment requires a non-empty project_id")
    validated_name = model.validate_experiment_name(name)
    validated_hypothesis = model.validate_hypothesis(hypothesis)
    if session_id:
        await _require_session(db, project_id, session_id)

    eid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO experiments (id, project_id, name, hypothesis, status, creator_session_id) "
        "VALUES (?, ?, ?, ?, 'active', ?)",
        (eid, project_id, validated_name, validated_hypothesis, session_id),
    )
    await db.commit()
    created = await get_experiment(db, project_id, experiment_id=eid)
    assert created is not None  # just written
    return created


async def get_experiment(
    db: Any, project_id: str, *, experiment_id: str
) -> "dict[str, Any] | None":
    """Fetch one experiment by id, scoped to ``project_id``."""
    async with db.execute(
        f"SELECT {', '.join(_EXPERIMENT_COLUMNS)} FROM experiments "
        "WHERE id = ? AND project_id = ?",
        (experiment_id, project_id),
    ) as cur:
        return _decode_experiment(await cur.fetchone())


async def list_experiments(
    db: Any, project_id: str, *, status: "str | None" = None, limit: int = 100
) -> "list[dict[str, Any]]":
    """List a project's experiments, newest first."""
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if status is not None:
        status_norm = status.strip().lower() if isinstance(status, str) else ""
        if status_norm not in model.EXPERIMENT_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(model.EXPERIMENT_STATUSES)}, got {status!r}"
            )
        clauses.append("status = ?")
        params.append(status_norm)
    limit = max(1, min(int(limit), 500))
    params.append(limit)
    async with db.execute(
        f"SELECT {', '.join(_EXPERIMENT_COLUMNS)} FROM experiments "
        f"WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, id DESC LIMIT ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [e for row in rows if (e := _decode_experiment(row)) is not None]


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


async def _find_run(db: Any, project_id: str, run_id: str) -> "dict[str, Any] | None":
    async with db.execute(
        f"SELECT {', '.join(_RUN_COLUMNS)} FROM experiment_runs "
        "WHERE id = ? AND project_id = ?",
        (run_id, project_id),
    ) as cur:
        return _decode_run(await cur.fetchone())


async def get_experiment_run(
    db: Any, project_id: str, *, run_id: str
) -> "dict[str, Any] | None":
    """Fetch one run by id, scoped to ``project_id``."""
    return await _find_run(db, project_id, run_id)


async def list_experiment_runs(
    db: Any,
    project_id: str,
    *,
    experiment_id: "str | None" = None,
    status: "str | None" = None,
    limit: int = 100,
) -> "list[dict[str, Any]]":
    """List a project's experiment runs, newest-started first."""
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if experiment_id is not None:
        clauses.append("experiment_id = ?")
        params.append(experiment_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(model.validate_run_status(status))
    limit = max(1, min(int(limit), 500))
    params.append(limit)
    async with db.execute(
        f"SELECT {', '.join(_RUN_COLUMNS)} FROM experiment_runs "
        f"WHERE {' AND '.join(clauses)} ORDER BY started_at DESC, id DESC LIMIT ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [r for row in rows if (r := _decode_run(row)) is not None]


async def _insert_event(
    db: Any,
    *,
    experiment_id: str,
    run_id: "str | None",
    event_type: str,
    label: "str | None",
    body: "str | None",
    artifact_ids: "list[str] | None" = None,
    created_by_session_id: "str | None" = None,
) -> "dict[str, Any]":
    """Shared low-level event insert -- used by BOTH the auto-skeleton writes
    (pivot/dead_end/breakthrough, unconditional on every terminal/pivot
    transition) and the manual enrichment path (record_experiment_event).
    Does NOT commit -- callers batch this with their own row update/insert
    and commit once, so a run transition and its mandatory event write land
    atomically from the caller's perspective."""
    eid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO experiment_events "
        "(id, experiment_id, run_id, event_type, label, body, artifact_ids_json, "
        "created_by_session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            eid, experiment_id, run_id, event_type, label, body,
            _json_dumps(artifact_ids) if artifact_ids else None, created_by_session_id,
        ),
    )
    async with db.execute(
        f"SELECT {', '.join(_EVENT_COLUMNS)} FROM experiment_events WHERE id = ?",
        (eid,),
    ) as cur:
        row = await cur.fetchone()
    created = _decode_event(row)
    assert created is not None
    return created


async def start_experiment_run(
    db: Any,
    project_id: str,
    session_id: str,
    *,
    experiment_id: str,
    trial_label: "str | None" = None,
    pivot_parent_run_id: "str | None" = None,
    resource_profile: "dict[str, Any] | None" = None,
    ttl_seconds: "int | None" = None,
    repository_id: "str | None" = None,
    worktree_id: "str | None" = None,
) -> "dict[str, Any]":
    """Create a new active experiment run.

    ``repository_id``/``worktree_id`` are optional additions beyond the
    sprint-item brief's literal signature -- see meridian.experiment's
    module docstring, deviation 2, for why: the table declares both columns
    but the brief's own signature never accepts them.

    When ``pivot_parent_run_id`` is given, the parent run MUST exist in this
    project AND belong to the SAME experiment (pivoting is a new trial of
    the same open question, not a cross-experiment jump) -- raises
    ``ValueError`` otherwise. On success, this function ALSO auto-writes an
    ``experiment_events`` row ``{event_type: 'pivot', label: 'auto', body:
    f'pivoted from run {pivot_parent_run_id}'}`` on the NEW run, inside this
    same function, non-optional, committed together with the run insert.
    """
    project_id = (project_id or "").strip()
    experiment_id = (experiment_id or "").strip()
    if not project_id:
        raise ValueError("start_experiment_run requires a non-empty project_id")
    if not experiment_id:
        raise ValueError("start_experiment_run requires a non-empty experiment_id")
    await _require_session(db, project_id, session_id)

    experiment = await get_experiment(db, project_id, experiment_id=experiment_id)
    if experiment is None:
        raise ValueError(f"experiment {experiment_id!r} not found in project {project_id!r}")

    fields = model.validate_run_fields(
        trial_label=trial_label, resource_profile=resource_profile, ttl_seconds=ttl_seconds,
    )

    parent_run: "dict[str, Any] | None" = None
    if pivot_parent_run_id:
        parent_run = await _find_run(db, project_id, pivot_parent_run_id)
        if parent_run is None:
            raise ValueError(
                f"pivot_parent_run_id {pivot_parent_run_id!r} not found in project {project_id!r}"
            )
        if parent_run["experiment_id"] != experiment_id:
            raise ValueError(
                f"pivot_parent_run_id {pivot_parent_run_id!r} belongs to experiment "
                f"{parent_run['experiment_id']!r}, not {experiment_id!r} -- a pivot "
                "must stay within the same experiment"
            )

    run_id = str(uuid.uuid4())
    now = model.utcnow_iso()
    expires_at = model.compute_expires_at(now, fields["ttl_seconds"])
    await db.execute(
        "INSERT INTO experiment_runs "
        "(id, experiment_id, project_id, repository_id, worktree_id, status, "
        "trial_label, pivot_parent_run_id, resource_profile_json, creator_session_id, "
        "started_at, expires_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id, experiment_id, project_id, repository_id, worktree_id,
            fields["trial_label"], pivot_parent_run_id,
            _json_dumps(fields["resource_profile"]), session_id,
            now, expires_at, now, now,
        ),
    )
    if pivot_parent_run_id:
        await _insert_event(
            db,
            experiment_id=experiment_id,
            run_id=run_id,
            event_type="pivot",
            label="auto",
            body=f"pivoted from run {pivot_parent_run_id}",
            created_by_session_id=session_id,
        )
    await db.commit()
    created = await _find_run(db, project_id, run_id)
    assert created is not None  # just written
    return created


async def complete_experiment_run(
    db: Any,
    project_id: str,
    session_id: str,
    *,
    run_id: str,
    outcome_summary: str,
    disposition: str,
    result_receipt: "dict[str, Any] | None" = None,
    status: str = "completed",
) -> "dict[str, Any]":
    """Finalize a run as ``completed`` or ``abandoned`` (see
    meridian.experiment's module docstring, deviation 3, for why ``status``
    is accepted here at all).

    ``outcome_summary``/``disposition`` are validated FIRST, UNCONDITIONALLY
    -- raises ``ValueError`` for a missing/empty outcome_summary or a
    missing disposition on EVERY call, including a retry against an
    already-terminal run (idempotency below never bypasses this). Only once
    both validate does an already-terminal run short-circuit: returns the
    EXISTING terminal record unchanged (matching
    meridian.db.research_runs.complete_research_run's own idempotent
    convention), so a retried/duplicated call from a flaky caller is safe.

    HARD INVARIANT: this function ALWAYS writes an experiment_events row for
    a run that newly reaches a terminal status here -- an explicit
    ``status='abandoned'``, or an outcome_summary matching 'dead end'/
    'failed' (case-insensitive substring), auto-writes
    ``{event_type: 'dead_end', label: 'auto', body: outcome_summary}``, in
    the SAME transaction as the status update.
    """
    project_id = (project_id or "").strip()
    # Validate FIRST, unconditionally -- see docstring above.
    validated_summary = model.validate_outcome_summary(outcome_summary, required=True)
    validated_disposition = model.validate_disposition(disposition, required=True)
    validated_status = model.validate_completion_status(status)
    assert validated_summary is not None and validated_disposition is not None

    run = await _find_run(db, project_id, run_id)
    if run is None:
        raise ValueError(f"experiment run {run_id!r} not found in project {project_id!r}")
    if run["status"] in model.RUN_TERMINAL_STATUSES:
        return run

    await _require_session(db, project_id, session_id)
    validated_receipt = model.validate_result_receipt(result_receipt)

    now = model.utcnow_iso()
    await db.execute(
        "UPDATE experiment_runs SET status = ?, outcome_summary = ?, disposition = ?, "
        "result_receipt_json = ?, completed_at = ?, updated_at = ? "
        "WHERE project_id = ? AND id = ?",
        (
            validated_status, validated_summary, validated_disposition,
            _json_dumps(validated_receipt), now, now, project_id, run_id,
        ),
    )
    # HARD INVARIANT: EVERY terminal transition writes an experiment_events
    # row, unconditionally -- 'dead_end' when the dead-end trigger fires,
    # 'milestone' otherwise. The sprint-item brief's complete_experiment_run
    # bullet only spells out the dead_end trigger explicitly, but its own
    # separately-stated HARD INVARIANT ("no run should ever reach a terminal
    # status ... without a corresponding experiment_events row ... every
    # terminal-transition code path above already auto-writes one") is
    # unconditional and governs here -- a clean 'keep' completion is a
    # terminal transition too, so it gets a 'milestone' skeleton event
    # rather than silently reaching 'completed' with zero event trail.
    if model.is_dead_end_outcome(validated_status, validated_summary):
        await _insert_event(
            db,
            experiment_id=run["experiment_id"],
            run_id=run_id,
            event_type="dead_end",
            label="auto",
            body=validated_summary,
            created_by_session_id=session_id,
        )
    else:
        await _insert_event(
            db,
            experiment_id=run["experiment_id"],
            run_id=run_id,
            event_type="milestone",
            label="auto",
            body=validated_summary,
            created_by_session_id=session_id,
        )
    await db.commit()
    updated = await _find_run(db, project_id, run_id)
    assert updated is not None
    return updated


async def promote_experiment_run(
    db: Any, project_id: str, session_id: "str | None", *, run_id: str
) -> "dict[str, Any]":
    """Explicitly promote a run whose stored ``disposition`` is already
    ``'promote'`` (set at completion time) -- raises ``ValueError``
    otherwise, mirroring
    meridian.db.research_runs.promote_research_run's exact pattern.
    Always auto-writes an experiment_events row ``{event_type:
    'breakthrough', label: 'auto', body: outcome_summary}``.
    """
    project_id = (project_id or "").strip()
    run = await _find_run(db, project_id, run_id)
    if run is None:
        raise ValueError(f"experiment run {run_id!r} not found in project {project_id!r}")
    if run.get("disposition") != "promote":
        raise ValueError(
            f"experiment run {run_id!r} has disposition {run.get('disposition')!r}; "
            "promote_experiment_run requires disposition='promote', set explicitly "
            "at completion time via complete_experiment_run"
        )

    event = await _insert_event(
        db,
        experiment_id=run["experiment_id"],
        run_id=run_id,
        event_type="breakthrough",
        label="auto",
        body=run.get("outcome_summary"),
        created_by_session_id=session_id,
    )
    await db.commit()
    return {"run_id": run_id, "run": run, "event": event}


async def expire_stale_runs(db: Any, project_id: str) -> int:
    """Transition any ``active`` run whose ``expires_at`` has passed to
    ``expired``. Returns the count of runs expired. Idempotent: a run
    already expired (or otherwise terminal) is never touched twice. A run
    started without a ``ttl_seconds`` (``expires_at`` is NULL) never
    auto-expires -- SQL ``expires_at < now`` is false for NULL.

    HARD INVARIANT: auto-writes an experiment_events row ``{event_type:
    'dead_end', label: 'expired', body: 'expired without explicit
    completion'}`` for EACH run expired here, one per run, in the same
    transaction as the status update.
    """
    project_id = (project_id or "").strip()
    now = model.utcnow_iso()
    async with db.execute(
        "SELECT id, experiment_id FROM experiment_runs "
        "WHERE project_id = ? AND status = 'active' "
        "AND expires_at IS NOT NULL AND expires_at < ?",
        (project_id, now),
    ) as cur:
        rows = await cur.fetchall()
    stale = [
        (row["id"], row["experiment_id"]) if hasattr(row, "keys") else (row[0], row[1])
        for row in rows
    ]
    if not stale:
        return 0

    for run_id, experiment_id in stale:
        await db.execute(
            "UPDATE experiment_runs SET status = 'expired', completed_at = ?, "
            "updated_at = ? WHERE project_id = ? AND id = ?",
            (now, now, project_id, run_id),
        )
        await _insert_event(
            db,
            experiment_id=experiment_id,
            run_id=run_id,
            event_type="dead_end",
            label="expired",
            body="expired without explicit completion",
            created_by_session_id=None,
        )
    await db.commit()
    return len(stale)


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


async def register_run_artifact(
    db: Any,
    project_id: str,
    session_id: "str | None",
    *,
    run_id: str,
    logical_path: str,
    content_hash: "str | None" = None,
    artifact_role: "str | None" = None,
) -> "dict[str, Any]":
    """Register a project-relative artifact against a run. Rejects an
    absolute path or a secret-shaped value -- reuses
    capability_manifest._ABSOLUTE_PATH_RE / secret_redaction.check_for_secrets
    via meridian.experiment.validate_logical_path, exactly like
    meridian.research_run does for its own allowed_paths."""
    project_id = (project_id or "").strip()
    run = await _find_run(db, project_id, run_id)
    if run is None:
        raise ValueError(f"experiment run {run_id!r} not found in project {project_id!r}")
    if session_id:
        await _require_session(db, project_id, session_id)

    validated_path = model.validate_logical_path(logical_path)
    validated_hash = model.validate_content_hash(content_hash)
    validated_role = model.validate_artifact_role(artifact_role)

    aid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO run_artifacts "
        "(id, project_id, experiment_run_id, logical_path, content_hash, artifact_role) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (aid, project_id, run_id, validated_path, validated_hash, validated_role),
    )
    await db.commit()
    async with db.execute(
        f"SELECT {', '.join(_ARTIFACT_COLUMNS)} FROM run_artifacts WHERE id = ?",
        (aid,),
    ) as cur:
        row = await cur.fetchone()
    created = _decode_artifact(row)
    assert created is not None
    return created


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


async def record_experiment_event(
    db: Any,
    project_id: str,
    session_id: "str | None",
    *,
    experiment_id: str,
    run_id: "str | None" = None,
    event_type: str,
    label: "str | None" = None,
    body: "str | None" = None,
    artifact_ids: "list[str] | None" = None,
) -> "dict[str, Any]":
    """Manual/enrichment event-recording path -- separate from the
    auto-skeleton writes in start_experiment_run/complete_experiment_run/
    promote_experiment_run/expire_stale_runs, which happen unconditionally
    regardless of whether a caller ever calls this. Both paths write through
    the same underlying table and coexist freely (an auto 'pivot' row and a
    later manual 'note' row on the same run are both just rows)."""
    project_id = (project_id or "").strip()
    experiment_id = (experiment_id or "").strip()
    if not experiment_id:
        raise ValueError("record_experiment_event requires a non-empty experiment_id")
    experiment = await get_experiment(db, project_id, experiment_id=experiment_id)
    if experiment is None:
        raise ValueError(f"experiment {experiment_id!r} not found in project {project_id!r}")

    if run_id:
        run = await _find_run(db, project_id, run_id)
        if run is None:
            raise ValueError(f"experiment run {run_id!r} not found in project {project_id!r}")
        if run["experiment_id"] != experiment_id:
            raise ValueError(
                f"run {run_id!r} belongs to experiment {run['experiment_id']!r}, "
                f"not {experiment_id!r}"
            )
    if session_id:
        await _require_session(db, project_id, session_id)

    validated_type = model.validate_event_type(event_type)
    validated_label = model.validate_label(label)
    validated_body = model.validate_body(body)
    validated_artifact_ids = model.validate_artifact_ids(artifact_ids)

    event = await _insert_event(
        db,
        experiment_id=experiment_id,
        run_id=run_id,
        event_type=validated_type,
        label=validated_label,
        body=validated_body,
        artifact_ids=validated_artifact_ids,
        created_by_session_id=session_id,
    )
    await db.commit()
    return event


async def get_experiment_events(
    db: Any,
    project_id: str,
    *,
    experiment_id: str,
    run_id: "str | None" = None,
    limit: int = 200,
) -> "list[dict[str, Any]]":
    """List an experiment's events, oldest first. Optionally scoped to one
    run_id."""
    project_id = (project_id or "").strip()
    experiment = await get_experiment(db, project_id, experiment_id=experiment_id)
    if experiment is None:
        raise ValueError(f"experiment {experiment_id!r} not found in project {project_id!r}")

    clauses = ["experiment_id = ?"]
    params: list[Any] = [experiment_id]
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    limit = max(1, min(int(limit), 1000))
    params.append(limit)
    async with db.execute(
        f"SELECT {', '.join(_EVENT_COLUMNS)} FROM experiment_events "
        f"WHERE {' AND '.join(clauses)} ORDER BY created_at ASC, id ASC LIMIT ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [e for row in rows if (e := _decode_event(row)) is not None]
