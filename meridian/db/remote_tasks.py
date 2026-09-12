"""Durable project-scoped Remote Task register (W1-E, item 32d3d5de).

Mirrors :mod:`meridian.db.external_jobs`'s pattern exactly (read that module
first): own ``_row_to_dict``, direct ``uuid`` usage, no import from
``meridian.db`` (``meridian.db.__init__``) at all -- this module is imported
BY the handlers layer, never the other way around, so importing the package
``__init__`` here would risk a cycle the same way external_jobs.py avoids it.

Unlike external_jobs.py (which only ever RECORDS an observation the caller
already made against some external system), this register's
:func:`start_remote_task` / :func:`get_remote_task_status` actually perform
the SSH round trip themselves -- see :mod:`meridian.remote_ssh` for the
transport and :mod:`meridian.remote_exec` for the validation + the pure
script-building/parsing this module glues together with persistence.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from meridian import remote_exec as model
from meridian import remote_ssh

_JOB_COLUMNS = (
    "id", "project_id", "session_id", "sprint_item_id", "host", "command",
    "pid", "status", "log_path", "log_tail", "cost_estimate",
    "started_at", "completed_at", "last_heartbeat_at",
    "created_at", "updated_at",
)


def _row_to_dict(row: Any) -> "dict[str, Any] | None":
    if row is None:
        return None
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(zip(_JOB_COLUMNS, row))


async def _require_session(db: Any, project_id: str, session_id: str) -> None:
    if not session_id:
        raise ValueError("session_id is required for remote task writes")
    async with db.execute(
        "SELECT id FROM sessions WHERE id = ? AND project_id = ?",
        (session_id, project_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise ValueError(f"session {session_id!r} does not belong to project {project_id!r}")


async def _find(db: Any, project_id: str, job_id: str) -> "dict[str, Any] | None":
    async with db.execute(
        f"SELECT {', '.join(_JOB_COLUMNS)} FROM remote_tasks "
        "WHERE project_id = ? AND id = ?",
        (project_id, job_id),
    ) as cur:
        return _row_to_dict(await cur.fetchone())


def _elapsed_seconds(job: dict[str, Any]) -> float:
    """Wall-clock seconds from ``started_at`` to ``completed_at`` (or now, if
    still live). Never raises on a malformed/missing timestamp -- returns
    0.0 instead, since this is a display convenience, not a correctness-
    critical value."""
    started_raw = job.get("started_at")
    ended_raw = job.get("completed_at") or model.utcnow_iso()
    try:
        started = datetime.fromisoformat(started_raw)
        ended = datetime.fromisoformat(ended_raw)
    except (TypeError, ValueError):
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=timezone.utc)
    return max(0.0, (ended - started).total_seconds())


async def start_remote_task(
    db: Any,
    project_id: str,
    session_id: str,
    *,
    host: str,
    command: str,
    sprint_item_id: "str | None" = None,
    ttl_seconds: "int | None" = None,
    ssh_runner: "remote_ssh.SSHRunner | None" = None,
    launch_timeout: "float | None" = None,
) -> dict[str, Any]:
    """Launch ``command`` on ``host`` over a short-lived SSH connection used
    ONLY to start the job (never held open), persist a durable record, and
    return quickly regardless of how long the underlying job itself runs.

    ``ssh_runner`` defaults to :func:`meridian.remote_ssh.run_ssh_command`;
    tests inject a fake matching the same signature so this can be exercised
    with no real SSH server (see module docstring / tests/test_remote_tasks.py).
    """
    await _require_session(db, project_id, session_id)
    validated = model.validate_start_fields(
        host=host, command=command, ttl_seconds=ttl_seconds
    )
    job_id = str(uuid.uuid4())
    job_dir = model.build_job_dir(job_id)
    script = model.build_launch_script(job_id, validated["command"])
    runner = ssh_runner or remote_ssh.run_ssh_command
    timeout = (
        launch_timeout if launch_timeout is not None
        else remote_ssh.DEFAULT_LAUNCH_TIMEOUT_SECONDS
    )
    result = await runner(validated["host"], script, timeout=timeout)

    now = model.utcnow_iso()
    pid: "int | None" = None
    log_path: "str | None" = f"{job_dir}/stdout.log"
    if result.connected and result.returncode == 0:
        status = "running"
        pid = model.parse_launch_pid(result.stdout)
    elif result.connected:
        # SSH itself worked, but the launch script exited nonzero (e.g. the
        # remote $HOME is read-only, bash is missing) -- the job never
        # actually started, so there is nothing to poll for later.
        status = "terminated-unexpectedly"
        log_path = None
    else:
        # Could not even establish the launching connection -- we genuinely
        # don't know whether anything ran.
        status = "unknown"
        log_path = None

    await db.execute(
        "INSERT INTO remote_tasks "
        "(id, project_id, session_id, sprint_item_id, host, command, pid, "
        "status, log_path, log_tail, cost_estimate, started_at, completed_at, "
        "last_heartbeat_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id, project_id, session_id, sprint_item_id,
            validated["host"], validated["command"], pid, status, log_path,
            None, None, now, None, now, now, now,
        ),
    )
    await db.commit()
    job = await _find(db, project_id, job_id)
    assert job is not None
    return {
        "job": job,
        "ttl_seconds": validated["ttl_seconds"],
        "launch": {
            "connected": result.connected,
            "returncode": result.returncode,
            "error": result.error,
            "stderr_tail": model.truncate_log_tail(result.stderr),
        },
    }


async def get_remote_task_status(
    db: Any,
    project_id: str,
    job_id: str,
    *,
    ssh_runner: "remote_ssh.SSHRunner | None" = None,
    check_timeout: "float | None" = None,
) -> dict[str, Any]:
    """Open a FRESH SSH connection (never the launching one) and determine
    the job's current status, in the exact priority order the sprint item
    specifies -- see :func:`meridian.remote_exec.build_status_check_script`
    for the remote-side logic and
    :func:`meridian.remote_exec.map_check_marker_to_status` for the mapping.

    When the connection itself cannot be established, this NEVER downgrades
    an already-terminal job (a completed job doesn't become "connection
    lost" just because it can't be reached afterward) -- otherwise it writes
    ``connection-lost-but-possibly-still-running``, explicitly distinct from
    any failure status, per the sprint item.
    """
    job = await _find(db, project_id, job_id)
    if job is None:
        raise ValueError("remote task not found in this project")

    script = model.build_status_check_script(job_id)
    runner = ssh_runner or remote_ssh.run_ssh_command
    timeout = (
        check_timeout if check_timeout is not None
        else remote_ssh.DEFAULT_STATUS_TIMEOUT_SECONDS
    )
    result = await runner(job["host"], script, timeout=timeout)
    now = model.utcnow_iso()

    updates: dict[str, Any] = {"updated_at": now}
    if not result.connected:
        if job["status"] in model.REMOTE_TASK_TERMINAL_STATUSES:
            new_status = job["status"]
        else:
            new_status = "connection-lost-but-possibly-still-running"
        updates["status"] = new_status
        # last_heartbeat_at is deliberately NOT refreshed here -- we did not
        # actually observe the job, only failed to reach the host.
    else:
        parsed = model.parse_status_check_output(result.stdout)
        new_status = model.map_check_marker_to_status(
            parsed["marker"], parsed["exit_code"]
        )
        updates["status"] = new_status
        updates["last_heartbeat_at"] = now
        if parsed["log_tail"]:
            updates["log_tail"] = parsed["log_tail"]
        if new_status in model.REMOTE_TASK_TERMINAL_STATUSES:
            updates["completed_at"] = job.get("completed_at") or now

    assignments = ", ".join(f"{column} = ?" for column in updates)
    await db.execute(
        f"UPDATE remote_tasks SET {assignments} WHERE project_id = ? AND id = ?",
        [*updates.values(), project_id, job_id],
    )
    await db.commit()
    updated = await _find(db, project_id, job_id)
    assert updated is not None
    return {
        "job": updated,
        "elapsed_seconds": _elapsed_seconds(updated),
        "connected": result.connected,
        "ssh_error": result.error,
    }


async def list_remote_tasks(
    db: Any,
    project_id: str,
    *,
    session_id: "str | None" = None,
    include_terminal: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read the project's persisted remote-task register. No SSH calls are
    made here -- this reports last-known (persisted) state only, matching
    the sprint item's explicit "no background poller" scope: a caller that
    wants a live re-check calls :func:`get_remote_task_status` for a
    specific job."""
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if not include_terminal:
        placeholders = ", ".join("?" for _ in model.REMOTE_TASK_TERMINAL_STATUSES)
        clauses.append(f"status NOT IN ({placeholders})")
        params.extend(sorted(model.REMOTE_TASK_TERMINAL_STATUSES))
    limit = max(1, min(int(limit), 500))
    params.append(limit)
    async with db.execute(
        f"SELECT {', '.join(_JOB_COLUMNS)} FROM remote_tasks "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY last_heartbeat_at DESC, id DESC LIMIT ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [job for row in rows if (job := _row_to_dict(row)) is not None]
