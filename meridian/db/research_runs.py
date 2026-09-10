"""Durable project-scoped bounded ephemeral research runs (a5343387).

Mirrors :mod:`meridian.db.external_jobs`'s exact pattern (read that file
first): a self-contained module, its own local ``_row_to_dict``, ``uuid``
used directly for id generation (no import from ``meridian.db.__init__``).
Unlike ``external_jobs``/``external_job_events``, there is no companion
event-log table here -- a research run is bounded and short-lived by design
(``turn_budget`` + ``expires_at``), so there is no "resume across a
long-running external process" need external_jobs exists to solve. A
single row per run, updated in place, is sufficient.

See :mod:`meridian.research_run` for field validation and, IMPORTANT, its
module docstring's drift note on why the underlying SQL table is named
``scratch_research_runs`` rather than ``research_runs`` (that name is
already taken by :mod:`meridian.db.experiment_model`'s unrelated ML-style
experiment-tracking table).
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from meridian import research_run as model

_RUN_COLUMNS = (
    "id", "project_id", "creator_session_id", "mode", "allowed_paths_json",
    "repository_id", "status", "turn_budget", "started_at", "expires_at",
    "completed_at", "result_receipt_json", "disposition", "created_at", "updated_at",
)


def _row_to_dict(row: Any) -> "dict[str, Any] | None":
    if row is None:
        return None
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(zip(_RUN_COLUMNS, row))


def _decode_run(row: Any) -> "dict[str, Any] | None":
    result = _row_to_dict(row)
    if result is None:
        return None
    raw_paths = result.pop("allowed_paths_json", None)
    if isinstance(raw_paths, list):
        result["allowed_paths"] = raw_paths
    else:
        try:
            result["allowed_paths"] = json.loads(raw_paths) if raw_paths else []
        except (TypeError, ValueError):
            result["allowed_paths"] = []
    raw_receipt = result.pop("result_receipt_json", None)
    if isinstance(raw_receipt, dict):
        result["result_receipt"] = raw_receipt
    else:
        try:
            result["result_receipt"] = json.loads(raw_receipt) if raw_receipt else None
        except (TypeError, ValueError):
            result["result_receipt"] = None
    return result


async def _require_session(db: Any, project_id: str, session_id: str) -> None:
    if not session_id:
        raise ValueError("session_id is required for research-run writes")
    async with db.execute(
        "SELECT id FROM sessions WHERE id = ? AND project_id = ?",
        (session_id, project_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise ValueError(f"session {session_id!r} does not belong to project {project_id!r}")


async def _find(db: Any, project_id: str, run_id: str) -> "dict[str, Any] | None":
    async with db.execute(
        f"SELECT {', '.join(_RUN_COLUMNS)} FROM scratch_research_runs "
        "WHERE project_id = ? AND id = ?",
        (project_id, run_id),
    ) as cur:
        return _decode_run(await cur.fetchone())


async def get_research_run(
    db: Any, project_id: str, *, run_id: str
) -> "dict[str, Any] | None":
    """Read one project-scoped research run. ``None`` when not found."""
    return await _find(db, project_id, run_id)


async def list_research_runs(
    db: Any,
    project_id: str,
    *,
    include_terminal: bool = False,
    status: "str | None" = None,
    limit: int = 100,
) -> "list[dict[str, Any]]":
    """List a project's research runs, newest-started first.

    By default terminal runs (completed/failed/abandoned/expired) are
    omitted, mirroring ``list_external_jobs``'s own default -- a fresh
    session sees only runs that are still live.
    """
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if not include_terminal:
        placeholders = ", ".join("?" for _ in model.RESEARCH_RUN_TERMINAL_STATUSES)
        clauses.append(f"status NOT IN ({placeholders})")
        params.extend(sorted(model.RESEARCH_RUN_TERMINAL_STATUSES))
    if status is not None:
        clauses.append("status = ?")
        params.append(model.validate_run_status(status))
    limit = max(1, min(int(limit), 500))
    params.append(limit)
    async with db.execute(
        f"SELECT {', '.join(_RUN_COLUMNS)} FROM scratch_research_runs "
        f"WHERE {' AND '.join(clauses)} ORDER BY started_at DESC, id DESC LIMIT ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [run for row in rows if (run := _decode_run(row)) is not None]


async def start_research_run(
    db: Any,
    project_id: str,
    session_id: str,
    *,
    mode: str,
    repository_id: str,
    allowed_paths: "list[str] | None" = None,
    turn_budget: int,
    ttl_seconds: "int | None" = None,
    is_isolated_worktree: bool = False,
) -> dict[str, Any]:
    """Create a new active research run.

    ``is_isolated_worktree`` is an explicit, caller-attested precondition
    (never inferred) required for ``mode="isolated_write"`` -- mirrors this
    module's "explicit, never inferred" convention for ``disposition``. A
    read-only run needs no such attestation and no ``claim_file``.
    """
    await _require_session(db, project_id, session_id)
    fields = model.validate_run_fields(
        mode=mode, repository_id=repository_id, allowed_paths=allowed_paths,
        turn_budget=turn_budget, ttl_seconds=ttl_seconds,
    )
    if fields["mode"] == "isolated_write" and not is_isolated_worktree:
        raise ValueError(
            "isolated_write mode requires is_isolated_worktree=True -- confirm the "
            "session is operating in an isolated git worktree (never the shared "
            "working tree) before starting an isolated-write research run"
        )

    run_id = str(uuid.uuid4())
    now = model.utcnow_iso()
    expires_at = model.compute_expires_at(now, fields["ttl_seconds"])
    await db.execute(
        "INSERT INTO scratch_research_runs "
        "(id, project_id, creator_session_id, mode, allowed_paths_json, repository_id, "
        "status, turn_budget, started_at, expires_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
        (
            run_id, project_id, session_id, fields["mode"],
            json.dumps(fields["allowed_paths"], ensure_ascii=False, sort_keys=True),
            fields["repository_id"], fields["turn_budget"], now, expires_at, now, now,
        ),
    )
    await db.commit()
    created = await _find(db, project_id, run_id)
    assert created is not None  # just written
    return created


async def update_research_run(
    db: Any,
    project_id: str,
    session_id: str,
    *,
    run_id: str,
    status: str,
    receipt: "dict[str, Any] | None" = None,
    disposition: "str | None" = None,
) -> dict[str, Any]:
    """Transition a run's status (e.g. to ``failed``/``abandoned``).

    A terminal run cannot be reopened or silently replaced -- mirrors
    ``external_jobs.update_external_job``'s own terminal guard exactly.
    Use :func:`complete_research_run` for the success/idempotent completion
    path instead of calling this with ``status="completed"``.
    """
    run = await _find(db, project_id, run_id)
    if run is None:
        raise ValueError(f"research run {run_id!r} not found in project {project_id!r}")
    if run["status"] in model.RESEARCH_RUN_TERMINAL_STATUSES:
        raise ValueError(
            f"terminal research run {run_id!r} cannot transition from "
            f"{run['status']!r} to a new status"
        )
    await _require_session(db, project_id, session_id)
    validated_status = model.validate_run_status(status)

    now = model.utcnow_iso()
    updates: dict[str, Any] = {"status": validated_status, "updated_at": now}
    if validated_status in model.RESEARCH_RUN_TERMINAL_STATUSES:
        updates["completed_at"] = now
        if receipt is not None or disposition is not None:
            updates["result_receipt_json"] = json.dumps(
                model.validate_result_receipt(receipt), ensure_ascii=False, sort_keys=True,
            )
            updates["disposition"] = model.validate_disposition(disposition)

    assignments = ", ".join(f"{column} = ?" for column in updates)
    await db.execute(
        f"UPDATE scratch_research_runs SET {assignments} WHERE project_id = ? AND id = ?",
        [*updates.values(), project_id, run_id],
    )
    await db.commit()
    updated = await _find(db, project_id, run_id)
    assert updated is not None
    return updated


async def complete_research_run(
    db: Any,
    project_id: str,
    session_id: str,
    *,
    run_id: str,
    receipt: "dict[str, Any] | None",
    disposition: str,
) -> dict[str, Any]:
    """Terminal success completion with a compact, byte-bounded receipt.

    Idempotent on an already-terminal run (any of
    ``RESEARCH_RUN_TERMINAL_STATUSES``, not only ``completed``): returns the
    EXISTING terminal record unchanged rather than erroring or re-writing
    over it, so a retried/duplicated ``complete_research_run`` call from a
    flaky caller is always safe.
    """
    run = await _find(db, project_id, run_id)
    if run is None:
        raise ValueError(f"research run {run_id!r} not found in project {project_id!r}")
    if run["status"] in model.RESEARCH_RUN_TERMINAL_STATUSES:
        return run

    await _require_session(db, project_id, session_id)
    validated_receipt = model.validate_result_receipt(receipt)
    validated_disposition = model.validate_disposition(disposition)

    now = model.utcnow_iso()
    await db.execute(
        "UPDATE scratch_research_runs SET status = 'completed', completed_at = ?, "
        "result_receipt_json = ?, disposition = ?, updated_at = ? "
        "WHERE project_id = ? AND id = ?",
        (
            now,
            json.dumps(validated_receipt, ensure_ascii=False, sort_keys=True),
            validated_disposition, now, project_id, run_id,
        ),
    )
    await db.commit()
    updated = await _find(db, project_id, run_id)
    assert updated is not None
    return updated


async def expire_stale_runs(db: Any, project_id: str) -> int:
    """Transition any ``active`` run whose ``expires_at`` has passed to
    ``expired``. Returns the count of runs expired. Idempotent: a run
    already expired (or otherwise terminal) is never touched twice."""
    now = model.utcnow_iso()
    async with db.execute(
        "SELECT id FROM scratch_research_runs "
        "WHERE project_id = ? AND status = 'active' AND expires_at < ?",
        (project_id, now),
    ) as cur:
        rows = await cur.fetchall()
    ids = [row["id"] if hasattr(row, "keys") else row[0] for row in rows]
    if not ids:
        return 0
    placeholders = ", ".join("?" for _ in ids)
    await db.execute(
        f"UPDATE scratch_research_runs SET status = 'expired', completed_at = ?, "
        f"updated_at = ? WHERE project_id = ? AND id IN ({placeholders})",
        [now, now, project_id, *ids],
    )
    await db.commit()
    return len(ids)


# ---------------------------------------------------------------------------
# Promotion (a5343387) -- explicit, caller-driven only. See this module's
# top-level docstring / the sprint item brief for why a "finding" (a durable,
# addressable project note, kind='finding') is the promotion TARGET chosen
# here rather than a proposal update or a formal sprint item:
#
#   - A research run's receipt is already a compact SUMMARY of a bounded,
#     often-exploratory probe -- exactly the shape save_finding's existing
#     phase-agnostic capture primitive is FOR ("turns a finding from web/
#     arxiv/code/conversation into a durable, addressable note with
#     provenance" -- meridian/db/__init__.py's save_finding docstring).
#   - It requires no new schema, no sprint-board interaction, and no
#     touches_resources/quality-gate machinery a probe was explicitly meant
#     to bypass -- promoting to a formal sprint item would reintroduce
#     exactly the ceremony this ADJACENT primitive exists to avoid for
#     everyday probes.
#   - A finding is still fully discoverable (get_findings / get_notes with
#     tag='finding') and durable, satisfying "creates a finding, proposal
#     update, or formal sprint item" without over-committing a disposable
#     probe's result to the formal planner/sprint workflow the rest of this
#     item deliberately leaves untouched.
#
# A caller that decides a promoted run's content actually warrants a formal
# sprint item or workspace proposal can create one directly (add_sprint_item
# / add_workspace_proposal) using the returned finding as its source --
# promote_research_run does not preclude that, it just doesn't presume it.
# ---------------------------------------------------------------------------

def _summarize_receipt_for_promotion(run: dict[str, Any]) -> str:
    receipt = run.get("result_receipt") or {}
    lines = [
        f"Promoted research run {run['id']} "
        f"(mode={run.get('mode')}, repository_id={run.get('repository_id')}, "
        f"status={run.get('status')}).",
    ]
    summary = receipt.get("result_summary")
    lines.append(f"Result: {summary}" if summary else "Result: (no summary provided)")
    if receipt.get("files_touched"):
        lines.append("Files touched: " + ", ".join(receipt["files_touched"][:20]))
    if receipt.get("commands_run"):
        lines.append("Commands run: " + ", ".join(receipt["commands_run"][:10]))
    if receipt.get("artifact_references"):
        lines.append("Artifact references: " + ", ".join(receipt["artifact_references"][:20]))
    if receipt.get("failure_reason"):
        lines.append(f"Failure reason: {receipt['failure_reason']}")
    return "\n".join(lines)


async def promote_research_run(
    db: Any,
    project_id: str,
    session_id: "str | None",
    *,
    run_id: str,
) -> dict[str, Any]:
    """Explicitly promote a completed, ``disposition='promote'`` research
    run into a durable project finding (see the module-level rationale
    above for why a finding is the chosen target type).

    Raises ``ValueError`` when the run doesn't exist in this project, or
    when its stored ``disposition`` is not ``'promote'`` -- promotion is
    never inferred from a run merely being terminal/successful.
    """
    run = await _find(db, project_id, run_id)
    if run is None:
        raise ValueError(f"research run {run_id!r} not found in project {project_id!r}")
    if run.get("disposition") != "promote":
        raise ValueError(
            f"research run {run_id!r} has disposition {run.get('disposition')!r}; "
            "promote_research_run requires disposition='promote', set explicitly "
            "at completion time via complete_research_run"
        )

    from meridian import db as db_module  # noqa: PLC0415 -- avoid a package-init cycle

    finding = await db_module.save_finding(
        db, project_id, _summarize_receipt_for_promotion(run), source_type="code",
    )
    return {"run_id": run_id, "finding": finding}
