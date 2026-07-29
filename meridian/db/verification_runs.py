"""525d86bb — durable synchronous verification-run lifecycle records.

Why this exists: ``run_verification`` dispatches a test command over the FS
tunnel and awaits the REAL synchronous result of the wrapped process
(``tunnel_client._handle_run_cmd`` -> ``routes.tunnel.send_run_cmd_control``).
Before this module, that result lived only as an in-memory dict handed back
to whichever MCP caller happened to be awaiting it — there was no durable,
queryable record that "this command ran, here is exactly what happened."
A crashed session, a disconnected dashboard, or a later auditor had no way to
answer "did the last verification run actually pass, and when."

This module gives ``run_verification`` two operations that together form the
whole lifecycle:

  1. :func:`create_verification_run` — called BEFORE the command is
     dispatched. Records ``project_id``, ``command``, ``cwd``, ``worktree``,
     and ``started_at``, with ``status='running'``.
  2. :func:`complete_verification_run` — called immediately after the ONE
     real synchronous wait resolves (``send_run_cmd_control``'s return
     value), never from any other code path. Records ``ended_at``,
     ``exit_code``, ``status``, ``passed``/``failed``, and a stdout/stderr
     log artifact.

Enforced invariants (the acceptance criteria this module exists to satisfy):

* **No detached monitor can report completion.** There is exactly one way to
  mark a run complete — :func:`complete_verification_run` — and it is only
  ever called by ``run_verification`` right after awaiting the real
  ``send_run_cmd_control`` result inline (see meridian/mcp/handler.py). There
  is no polling loop, no background task, and no second writer: a run that
  has not been synchronously awaited to completion simply has no completion
  row to consume, ever.
* **Rejects missing/ambiguous evidence.** :func:`complete_verification_run`
  raises ``ValueError`` when: the run id does not exist (nothing to
  complete — missing evidence); the run was already completed (a durable
  completion record is not silently overwritten by a second call); or the
  caller claims ``status='ok'`` without a concrete integer ``exit_code`` (a
  claimed success with no real exit code on file is exactly the kind of
  self-contradictory evidence this table exists to make impossible to
  smuggle through).

Mirrors the minimal single-table shape of ``meridian/db/batch_claim.py``
(22cad9b8) rather than the heavier append-only-history + state-machine shape
of ``wave_runs.py`` — a verification run has no intermediate states to
transition through and no children; it is created once, running, and
completed exactly once.
"""
from __future__ import annotations

from typing import Any

import aiosqlite

from meridian.db import _new_id, _row_to_dict  # noqa: PLC0415

#: Every valid terminal (or in-flight) status a verification run may carry.
#: Mirrors the status vocabulary already returned by
#: ``routes.tunnel.send_run_cmd_control`` / ``tunnel_client._handle_run_cmd``.
VERIFICATION_RUN_STATUSES: frozenset[str] = frozenset({
    "running",        # in-flight — set by create_verification_run
    "ok",              # command executed; exit_code is the real result
    "error",           # spawn/transport failure — never a real exit_code
    "timeout",         # bounded wait expired — never a real exit_code
    "not_connected",   # no active tunnel — command never dispatched
    "not_configured",  # no test_cmd configured — command never dispatched
})

#: Statuses that claim the command actually ran and produced a result. Only
#: these require a concrete integer exit_code — the others are honest
#: "nothing to report" statuses where exit_code=None is itself correct.
_STATUSES_REQUIRING_EXIT_CODE: frozenset[str] = frozenset({"ok"})


async def _migrate_verification_runs(db: aiosqlite.Connection) -> None:
    """525d86bb — create ``verification_runs`` on existing SQLite DBs.

    Guarded migration (no inline CREATE INDEX in the unguarded base schema
    literals — 2026-07-04 outage rule): the table + its index are created
    here so existing DBs pick them up on the first startup after deploy.
    Idempotent. Mirrors pg_adapter._migrate_pg_verification_runs.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS verification_runs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            command TEXT NOT NULL,
            cwd TEXT,
            worktree TEXT,
            actor TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            exit_code INTEGER,
            passed INTEGER,
            failed INTEGER,
            stdout_tail TEXT,
            stderr_tail TEXT,
            message TEXT,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            ended_at TEXT
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_verification_runs_project "
        "ON verification_runs(project_id, started_at DESC)"
    )
    await db.commit()


async def create_verification_run(
    db: aiosqlite.Connection,
    project_id: str,
    command: str,
    *,
    cwd: str | None = None,
    worktree: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Record that a synchronous verification command is about to run.

    Inserted with ``status='running'`` and no ``exit_code``/``ended_at`` —
    those are only ever written by :func:`complete_verification_run`, and
    only once, right after the real synchronous wait on the process resolves.
    """
    run_id = _new_id()
    await db.execute(
        "INSERT INTO verification_runs "
        "(id, project_id, command, cwd, worktree, actor, status) "
        "VALUES (?, ?, ?, ?, ?, ?, 'running')",
        (run_id, project_id, command, cwd, worktree, actor),
    )
    await db.commit()
    run = await get_verification_run(db, run_id)
    assert run is not None  # just inserted
    return run


async def get_verification_run(
    db: aiosqlite.Connection, run_id: str,
) -> dict[str, Any] | None:
    """Return one verification run by id, or None if it does not exist."""
    async with db.execute(
        "SELECT * FROM verification_runs WHERE id = ?", (run_id,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def list_verification_runs(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List a project's verification runs, newest-started first.

    ``limit`` is clamped to [1, 500] so a caller cannot accidentally pull an
    unbounded result set.
    """
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if status:
        clauses.append("status = ?")
        params.append(status)
    params.append(max(1, min(int(limit), 500)))
    async with db.execute(
        f"SELECT * FROM verification_runs WHERE {' AND '.join(clauses)} "
        f"ORDER BY started_at DESC, id DESC LIMIT ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [r for r in (_row_to_dict(row) for row in rows) if r is not None]


async def complete_verification_run(
    db: aiosqlite.Connection,
    run_id: str,
    *,
    status: str,
    exit_code: int | None = None,
    passed: int | None = None,
    failed: int | None = None,
    stdout_tail: str = "",
    stderr_tail: str = "",
    message: str | None = None,
) -> dict[str, Any]:
    """Consume the persisted run record and stamp its real, final outcome.

    This is the ONLY function in this module that writes ``ended_at`` — call
    it exactly once, immediately after the real synchronous
    ``send_run_cmd_control`` result is in hand. Raises ``ValueError`` (never
    silently drops or fabricates evidence) when:

    * ``run_id`` does not name an existing run — nothing was ever created to
      complete, i.e. missing evidence.
    * the run was already completed — a durable completion record is never
      silently overwritten by a second call (start a new run instead).
    * ``status`` is not one of :data:`VERIFICATION_RUN_STATUSES`.
    * ``status='ok'`` (a claimed successful execution) is paired with an
      ``exit_code`` that is not a concrete ``int`` — ambiguous evidence: a
      "success" with no real exit code on file is exactly what this table
      exists to make impossible to record as complete.
    """
    if status not in VERIFICATION_RUN_STATUSES:
        raise ValueError(
            f"Invalid verification-run status {status!r}. "
            f"Valid: {sorted(VERIFICATION_RUN_STATUSES)}"
        )
    run = await get_verification_run(db, run_id)
    if run is None:
        raise ValueError(
            f"verification run {run_id!r} not found — cannot complete a run "
            "that was never created. Missing evidence."
        )
    if run.get("ended_at") is not None:
        raise ValueError(
            f"verification run {run_id!r} was already completed at "
            f"{run['ended_at']!r} — refusing to silently overwrite a durable "
            "completion record. Start a new run via create_verification_run "
            "instead of re-completing this one."
        )
    # exit_code=True/False would silently pass `isinstance(x, int)` below
    # (bool is an int subclass) but is never a real process exit code —
    # reject it up front, whenever it's supplied at all, to keep the
    # evidence unambiguous.
    if isinstance(exit_code, bool):
        raise ValueError(
            f"Ambiguous evidence: exit_code must be a real integer exit "
            f"status, got a bool ({exit_code!r})."
        )
    if status in _STATUSES_REQUIRING_EXIT_CODE and not isinstance(exit_code, int):
        raise ValueError(
            f"Ambiguous evidence: status={status!r} claims the command "
            f"actually ran, which requires a concrete integer exit_code — "
            f"got {exit_code!r}. A claimed success/failure with no real "
            "exit_code on file cannot be recorded as complete."
        )

    await db.execute(
        "UPDATE verification_runs SET status = ?, exit_code = ?, passed = ?, "
        "failed = ?, stdout_tail = ?, stderr_tail = ?, message = ?, "
        "ended_at = datetime('now') WHERE id = ?",
        (
            status, exit_code, passed, failed,
            stdout_tail or "", stderr_tail or "", message,
            run_id,
        ),
    )
    await db.commit()
    updated = await get_verification_run(db, run_id)
    assert updated is not None
    return updated
