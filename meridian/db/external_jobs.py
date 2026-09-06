"""Durable project-scoped external-job register.

The register is intentionally separate from research run attempts: it tracks
any long-running external work a user needs to resume, while the research
adapter retains its stricter experiment lifecycle contract.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from meridian import external_job_register as model

_UNSET = object()
_JOB_COLUMNS = (
    "id", "project_id", "job_key", "provider", "external_id", "status", "phase",
    "check_hint", "resume_hint", "resource_hint", "next_check_at", "detail",
    "metadata_json", "created_by_session_id", "updated_by_session_id", "started_at",
    "last_observed_at", "completed_at", "created_at", "updated_at",
)


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(zip(_JOB_COLUMNS, row))


def _decode_job(row: Any) -> dict[str, Any] | None:
    result = _row_to_dict(row)
    if result is None:
        return None
    raw = result.pop("metadata_json", None)
    if isinstance(raw, dict):
        result["metadata"] = raw
    else:
        try:
            result["metadata"] = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            result["metadata"] = {}
    return result


async def _require_session(db: Any, project_id: str, session_id: str) -> None:
    if not session_id:
        raise ValueError("session_id is required for external-job writes")
    async with db.execute(
        "SELECT id FROM sessions WHERE id = ? AND project_id = ?",
        (session_id, project_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise ValueError(f"session {session_id!r} does not belong to project {project_id!r}")


async def _find(
    db: Any, project_id: str, *, job_id: str | None = None, job_key: str | None = None
) -> dict[str, Any] | None:
    if bool(job_id) == bool(job_key):
        raise ValueError("pass exactly one of job_id or job_key")
    column, value = ("id", job_id) if job_id else ("job_key", job_key)
    async with db.execute(
        f"SELECT {', '.join(_JOB_COLUMNS)} FROM external_jobs "
        f"WHERE project_id = ? AND {column} = ?",
        (project_id, value),
    ) as cur:
        return _decode_job(await cur.fetchone())


def _event_snapshot(job: dict[str, Any]) -> str:
    return json.dumps(job, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


async def _append_event(
    db: Any, job: dict[str, Any], session_id: str, event_kind: str, detail: str | None = None
) -> None:
    await db.execute(
        "INSERT INTO external_job_events "
        "(id, project_id, external_job_id, session_id, event_kind, status, phase, detail, snapshot_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()), job["project_id"], job["id"], session_id,
            event_kind, job["status"], job.get("phase"), detail, _event_snapshot(job),
            model.utcnow_iso(),
        ),
    )


async def get_external_job(
    db: Any,
    project_id: str,
    *,
    job_id: str | None = None,
    job_key: str | None = None,
    include_history: bool = True,
) -> dict[str, Any] | None:
    job = await _find(db, project_id, job_id=job_id, job_key=job_key)
    if job is None or not include_history:
        return job
    async with db.execute(
        "SELECT id, session_id, event_kind, status, phase, detail, snapshot_json, created_at "
        "FROM external_job_events WHERE project_id = ? AND external_job_id = ? "
        "ORDER BY created_at ASC, id ASC LIMIT 200",
        (project_id, job["id"]),
    ) as cur:
        rows = await cur.fetchall()
    history = []
    for row in rows:
        if hasattr(row, "keys"):
            event = {key: row[key] for key in row.keys()}
        else:
            event = dict(zip(("id", "session_id", "event_kind", "status", "phase", "detail", "snapshot_json", "created_at"), row))
        raw = event.pop("snapshot_json", None)
        try:
            event["snapshot"] = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            event["snapshot"] = {}
        history.append(event)
    job["history"] = history
    return job


async def list_external_jobs(
    db: Any,
    project_id: str,
    *,
    include_terminal: bool = False,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if not include_terminal:
        clauses.append("status NOT IN ('succeeded', 'failed', 'canceled')")
    if status is not None:
        clauses.append("status = ?")
        params.append(model.validate_external_status(status))
    limit = max(1, min(int(limit), 500))
    params.append(limit)
    async with db.execute(
        f"SELECT {', '.join(_JOB_COLUMNS)} FROM external_jobs "
        f"WHERE {' AND '.join(clauses)} ORDER BY last_observed_at DESC, id DESC LIMIT ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [job for row in rows if (job := _decode_job(row)) is not None]


async def register_external_job(
    db: Any,
    project_id: str,
    session_id: str,
    *,
    job_key: str,
    provider: str,
    external_id: str,
    status: str = "running",
    phase: str | None = None,
    check_hint: str | None = None,
    resume_hint: str | None = None,
    resource_hint: str | None = None,
    next_check_at: str | None = None,
    detail: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    await _require_session(db, project_id, session_id)
    key, provider_name, external = model.validate_job_identity(
        job_key=job_key, provider=provider, external_id=external_id
    )
    fields = model.validate_job_fields(
        status=status, phase=phase, check_hint=check_hint, resume_hint=resume_hint,
        resource_hint=resource_hint, next_check_at=next_check_at, detail=detail,
        metadata=metadata,
    )
    existing = await _find(db, project_id, job_key=key)
    if existing is not None:
        if existing["provider"] != provider_name or existing["external_id"] != external:
            raise ValueError(
                f"external job key {key!r} already identifies a different external job"
            )
        return await update_external_job(
            db, project_id, session_id, job_key=key, status=fields["status"],
            phase=fields["phase"], check_hint=fields["check_hint"],
            resume_hint=fields["resume_hint"], resource_hint=fields["resource_hint"],
            next_check_at=fields["next_check_at"], detail=fields["detail"],
            metadata=fields["metadata"], event_kind="reaffirmed",
        )

    now = model.utcnow_iso()
    completed_at = now if fields["status"] in model.EXTERNAL_JOB_TERMINAL_STATUSES else None
    await db.execute(
        "INSERT INTO external_jobs "
        "(id, project_id, job_key, provider, external_id, status, phase, check_hint, resume_hint, "
        "resource_hint, next_check_at, detail, metadata_json, created_by_session_id, "
        "updated_by_session_id, started_at, last_observed_at, completed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()), project_id, key, provider_name, external,
            fields["status"], fields["phase"], fields["check_hint"], fields["resume_hint"],
            fields["resource_hint"], fields["next_check_at"], fields["detail"],
            json.dumps(fields["metadata"], ensure_ascii=False, sort_keys=True),
            session_id, session_id, now, now, completed_at,
        ),
    )
    job = await _find(db, project_id, job_key=key)
    assert job is not None
    await _append_event(db, job, session_id, "registered", fields["detail"])
    await db.commit()
    return job


async def update_external_job(
    db: Any,
    project_id: str,
    session_id: str,
    *,
    job_id: str | None = None,
    job_key: str | None = None,
    status: object = _UNSET,
    phase: object = _UNSET,
    check_hint: object = _UNSET,
    resume_hint: object = _UNSET,
    resource_hint: object = _UNSET,
    next_check_at: object = _UNSET,
    detail: object = _UNSET,
    metadata: object = _UNSET,
    event_kind: str = "observed",
) -> dict[str, Any]:
    await _require_session(db, project_id, session_id)
    job = await _find(db, project_id, job_id=job_id, job_key=job_key)
    if job is None:
        raise ValueError("external job not found in this project")

    changes: dict[str, Any] = {}
    if status is not _UNSET:
        changes["status"] = model.validate_external_status(status)
    for name, value in (
        ("phase", phase), ("check_hint", check_hint), ("resume_hint", resume_hint),
        ("resource_hint", resource_hint), ("next_check_at", next_check_at), ("detail", detail),
    ):
        if value is not _UNSET:
            changes[name] = model._validate_text(
                value, field=name, max_chars=4_000 if name == "detail" else 2_000,
                reject_local_path=True,
            )
    if metadata is not _UNSET:
        changes["metadata"] = model.validate_metadata(metadata)
    new_status = changes.get("status", job["status"])
    if job["status"] in model.EXTERNAL_JOB_TERMINAL_STATUSES and new_status != job["status"]:
        raise ValueError(
            f"terminal external job {job['job_key']!r} cannot transition from "
            f"{job['status']!r} to {new_status!r}"
        )
    if not changes:
        raise ValueError("at least one external job field is required")

    now = model.utcnow_iso()
    updates: dict[str, Any] = {
        "updated_by_session_id": session_id,
        "last_observed_at": now,
        "updated_at": now,
    }
    for name, value in changes.items():
        updates["metadata_json" if name == "metadata" else name] = (
            json.dumps(value, ensure_ascii=False, sort_keys=True) if name == "metadata" else value
        )
    if new_status in model.EXTERNAL_JOB_TERMINAL_STATUSES:
        updates["completed_at"] = job.get("completed_at") or now
    elif job["status"] not in model.EXTERNAL_JOB_TERMINAL_STATUSES:
        updates["completed_at"] = None

    assignments = ", ".join(f"{column} = ?" for column in updates)
    await db.execute(
        f"UPDATE external_jobs SET {assignments} WHERE project_id = ? AND id = ?",
        [*updates.values(), project_id, job["id"]],
    )
    updated = await _find(db, project_id, job_id=job["id"])
    assert updated is not None
    await _append_event(db, updated, session_id, event_kind, updated.get("detail"))
    await db.commit()
    return updated


async def complete_external_job(
    db: Any,
    project_id: str,
    session_id: str,
    *,
    job_id: str | None = None,
    job_key: str | None = None,
    status: str = "succeeded",
    detail: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    terminal = model.validate_external_status(status)
    if terminal not in model.EXTERNAL_JOB_TERMINAL_STATUSES:
        raise ValueError("complete_external_job status must be terminal")
    kwargs: dict[str, Any] = {"status": terminal, "detail": detail, "event_kind": "completed"}
    if metadata is not None:
        kwargs["metadata"] = metadata
    return await update_external_job(
        db, project_id, session_id, job_id=job_id, job_key=job_key, **kwargs
    )
