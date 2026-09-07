"""4376e655 — durable persistence for the Experiment/Run/RunAttempt state
model. See :mod:`meridian.experiment_model` for the closed vocabularies
(``ATTEMPT_STATUSES``/``FAILURE_CLASSES``), transition rules, and the
parameters fingerprint helper every write here is built on top of. This
module is the persistence layer: three tables, dual-backend (SQLite +
Postgres, mirrored in ``pg_adapter._migrate_pg_experiment_model``),
following the exact append-only / typed-enum / idempotent-insert
conventions already established by ``meridian.db.research_graph`` and
``meridian.db.proposal_lineage`` — not reinvented.

SCHEMA
------

``experiments`` — a named research question; many runs belong to one.

``research_runs`` — one logical execution of an experiment with one fixed
parameter set. ``id`` is the run's IMMUTABLE identity. An optional
``idempotency_key`` (unique per ``(project_id, experiment_id)`` when given —
plain SQL NULL semantics mean multiple key-less runs never collide) is what
lets a caller retry a submission without risking a duplicate run: calling
:func:`create_run` again with the SAME key returns the SAME row. ``status``
is deliberately NOT a column on this table — see :func:`get_run`.

``research_run_attempts`` — one concrete attempt (1, 2, 3, ... after
retries) to execute a run. This is where real outcome state lives:
``status``, ``failure_class``, ``checkpoint_ref``, ``artifact_refs``,
``provenance_ref``. :func:`create_attempt` numbers attempts via the same
``COALESCE(MAX(...), 0) + 1`` idiom as
``research_graph._next_node_sequence``, with a bounded retry loop against
the ``(run_id, attempt_number)`` unique index to survive a concurrent-create
race rather than trusting the read-then-write gap.

DERIVED RUN STATUS — "re-derive live state, don't replay stale text"
----------------------------------------------------------------------

A run's overall status is never stored as its own authoritative column. It
is always the status of its latest (highest ``attempt_number``) attempt,
computed fresh on every :func:`get_run` call, or ``'queued'`` when zero
attempts exist yet. This is the acceptance criterion's "restart recovery and
handoff serialization re-derive live state rather than replaying stale
text" made concrete: there is no cached run-status column that could ever
drift from the attempts that are the actual source of truth.

RESTART RECOVERY
-----------------

:func:`reconcile_stale_attempts` is the truthful answer to "the server
restarted — what happened to attempts that were queued/running before it
went down?" It transitions any attempt whose last observed activity
(``last_heartbeat_at``, falling back to ``started_at``/``created_at``) is
older than a threshold to ``'unknown'`` — never guessed as ``'failed'`` —
matching the acceptance criterion that partial/crashed/unknown outcomes
"remain truthful". A later caller that learns the real outcome reconciles
``'unknown'`` forward via :func:`transition_attempt` (see
``meridian.experiment_model``'s transition table: ``unknown`` is the one
non-terminal state that accepts a transition to any real outcome).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from meridian.db import _new_id, _row_to_dict
from meridian.experiment_model import (
    ATTEMPT_TERMINAL_STATUSES,
    FAILURE_CLASSES,
    params_fingerprint,
    validate_attempt_status,
    validate_attempt_transition,
    validate_failure_class,
)

_MAX_ATTEMPT_NUMBER_RETRIES = 5


def _now_iso() -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS' — matches research_graph's cross-dialect-
    safe timestamp convention (computed in Python, not a SQL now()/
    datetime('now') call)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _is_unique_violation(exc: BaseException) -> bool:
    """Heuristic: does ``exc`` look like a UNIQUE/duplicate-key violation?

    Matches sqlite3's ``UNIQUE constraint failed`` and psycopg3's
    ``UniqueViolation``. Never raises. Duplicated per-module by existing
    convention (see ``research_graph._is_unique_violation``)."""
    msg = str(exc).lower()
    return "unique" in msg or "duplicate key" in msg


def _json_dumps(value: "Any | None") -> "str | None":
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if value is not None else None


def _json_loads(raw: "str | None") -> "Any | None":
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Migration — guarded, idempotent (2026-07-04 outage rule: no unguarded
# CREATE INDEX on a migration-added column/table in CREATE_TABLES/
# CREATE_TABLES_CORE). Mirrored on Postgres by
# pg_adapter._migrate_pg_experiment_model.
# ---------------------------------------------------------------------------


async def _migrate_experiment_model(db: aiosqlite.Connection) -> None:
    """4376e655 — create experiments / research_runs / research_run_attempts
    if absent."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS experiments (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT,
            config_template TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_experiments_project ON experiments(project_id)"
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS research_runs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            experiment_id TEXT NOT NULL,
            idempotency_key TEXT,
            params TEXT,
            params_fingerprint TEXT,
            source_revision TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT
        )"""
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_research_runs_idempotency "
        "ON research_runs(project_id, experiment_id, idempotency_key)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_runs_experiment ON research_runs(experiment_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_runs_project ON research_runs(project_id)"
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS research_run_attempts (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
                'queued', 'running', 'succeeded', 'failed', 'cancelled', 'crashed', 'unknown'
            )),
            failure_class TEXT CHECK (failure_class IS NULL OR failure_class IN (
                'user_error', 'infra_error', 'timeout', 'oom', 'preempted',
                'dependency_error', 'unknown'
            )),
            error_message TEXT,
            checkpoint_ref TEXT,
            artifact_refs TEXT,
            provenance_ref TEXT,
            started_at TEXT,
            ended_at TEXT,
            last_heartbeat_at TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT
        )"""
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_run_attempts_number "
        "ON research_run_attempts(run_id, attempt_number)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_attempts_run ON research_run_attempts(run_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_attempts_project ON research_run_attempts(project_id)"
    )
    await db.commit()


def _row_to_experiment(row: Any) -> "dict[str, Any] | None":
    d = _row_to_dict(row)
    if d is None:
        return None
    d["config_template"] = _json_loads(d.get("config_template"))
    return d


def _row_to_run(row: Any) -> "dict[str, Any] | None":
    d = _row_to_dict(row)
    if d is None:
        return None
    d["params"] = _json_loads(d.get("params"))
    return d


def _row_to_attempt(row: Any) -> "dict[str, Any] | None":
    d = _row_to_dict(row)
    if d is None:
        return None
    d["checkpoint_ref"] = _json_loads(d.get("checkpoint_ref"))
    d["artifact_refs"] = _json_loads(d.get("artifact_refs"))
    d["provenance_ref"] = _json_loads(d.get("provenance_ref"))
    return d


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------


async def create_experiment(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    name: "str | None" = None,
    config_template: "dict[str, Any] | None" = None,
    created_by: "str | None" = None,
) -> dict[str, Any]:
    """Create a new experiment scoped to ``project_id``.

    Not idempotent on ``name`` (an experiment has no natural key — a
    project can have many experiments sharing a display name); callers that
    need idempotent creation should look up an existing experiment id
    first (e.g. via a caller-side cache) rather than relying on ``name``.
    """
    project_id = (project_id or "").strip()
    if not project_id:
        raise ValueError("create_experiment requires a non-empty project_id")
    if name is not None:
        from meridian.secret_redaction import check_for_secrets  # noqa: PLC0415
        check_for_secrets(name, context="experiment name")

    eid = _new_id()
    await db.execute(
        "INSERT INTO experiments (id, project_id, name, config_template, created_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (eid, project_id, name, _json_dumps(config_template), created_by),
    )
    await db.commit()
    created = await get_experiment(db, project_id, eid)
    assert created is not None  # just written
    return created


async def get_experiment(
    db: aiosqlite.Connection, project_id: str, experiment_id: str
) -> "dict[str, Any] | None":
    """Fetch one experiment by id, scoped to ``project_id`` — a cross-project
    lookup (right id, wrong project) returns ``None``, exactly like a
    nonexistent id, never leaking the row's existence to the wrong tenant."""
    async with db.execute(
        "SELECT * FROM experiments WHERE id = ? AND project_id = ?",
        (experiment_id, project_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_experiment(row)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


async def create_run(
    db: aiosqlite.Connection,
    project_id: str,
    experiment_id: str,
    *,
    params: "dict[str, Any] | None" = None,
    source_revision: "str | None" = None,
    idempotency_key: "str | None" = None,
    created_by: "str | None" = None,
) -> dict[str, Any]:
    """Create a new run under ``experiment_id`` (must belong to
    ``project_id`` — raises ``ValueError`` otherwise, rejecting a
    cross-project write before anything is inserted).

    Idempotent on ``(project_id, experiment_id, idempotency_key)`` when
    ``idempotency_key`` is given: a repeat call with the SAME key returns
    the EXISTING run untouched — this is the "retries create distinct
    attempts without duplicating a run" contract. A caller retrying a
    submission should reuse the same ``idempotency_key`` and then call
    :func:`create_attempt` on the returned run, not call this function
    expecting a fresh run each time. Omitting ``idempotency_key`` always
    creates a new run (no dedup key to collide on).
    """
    project_id = (project_id or "").strip()
    experiment_id = (experiment_id or "").strip()
    if not project_id:
        raise ValueError("create_run requires a non-empty project_id")
    if not experiment_id:
        raise ValueError("create_run requires a non-empty experiment_id")

    experiment = await get_experiment(db, project_id, experiment_id)
    if experiment is None:
        raise ValueError(
            f"experiment {experiment_id!r} not found in project {project_id!r}"
        )

    idempotency_key = idempotency_key.strip() if isinstance(idempotency_key, str) else None
    idempotency_key = idempotency_key or None

    if idempotency_key is not None:
        existing = await _find_run_by_idempotency_key(db, project_id, experiment_id, idempotency_key)
        if existing is not None:
            return existing

    rid = _new_id()
    fingerprint = params_fingerprint(params)
    try:
        await db.execute(
            "INSERT INTO research_runs "
            "(id, project_id, experiment_id, idempotency_key, params, params_fingerprint, "
            "source_revision, attempt_count, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (
                rid, project_id, experiment_id, idempotency_key, _json_dumps(params),
                fingerprint, source_revision, created_by,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — classified below
        if idempotency_key is not None and _is_unique_violation(exc):
            # Lost a create race against another caller submitting the SAME
            # idempotency key — nothing of ours was written, hand back the winner.
            winner = await _find_run_by_idempotency_key(db, project_id, experiment_id, idempotency_key)
            if winner is not None:
                return winner
        raise
    await db.commit()
    created = await get_run(db, project_id, rid)
    assert created is not None  # just written
    return created


async def _find_run_by_idempotency_key(
    db: aiosqlite.Connection, project_id: str, experiment_id: str, idempotency_key: str
) -> "dict[str, Any] | None":
    async with db.execute(
        "SELECT id FROM research_runs WHERE project_id = ? AND experiment_id = ? "
        "AND idempotency_key = ?",
        (project_id, experiment_id, idempotency_key),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return await get_run(db, project_id, row["id"])


async def get_run(
    db: aiosqlite.Connection, project_id: str, run_id: str
) -> "dict[str, Any] | None":
    """Fetch one run by id, scoped to ``project_id``, with ``status`` and
    ``latest_attempt`` computed LIVE from ``research_run_attempts`` — never
    read from a cached column. ``status`` is ``'queued'`` when the run has
    no attempts yet."""
    async with db.execute(
        "SELECT * FROM research_runs WHERE id = ? AND project_id = ?",
        (run_id, project_id),
    ) as cur:
        row = await cur.fetchone()
    run = _row_to_run(row)
    if run is None:
        return None
    latest = await _get_latest_attempt(db, project_id, run_id)
    run["status"] = latest["status"] if latest is not None else "queued"
    run["latest_attempt"] = latest
    return run


async def _get_latest_attempt(
    db: aiosqlite.Connection, project_id: str, run_id: str
) -> "dict[str, Any] | None":
    async with db.execute(
        "SELECT * FROM research_run_attempts WHERE project_id = ? AND run_id = ? "
        "ORDER BY attempt_number DESC LIMIT 1",
        (project_id, run_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_attempt(row)


# ---------------------------------------------------------------------------
# Run attempts
# ---------------------------------------------------------------------------


async def create_attempt(
    db: aiosqlite.Connection,
    project_id: str,
    run_id: str,
    *,
    created_by: "str | None" = None,
) -> dict[str, Any]:
    """Create the next attempt (``attempt_number`` = previous max + 1, or 1
    for the first attempt) under ``run_id`` — must belong to ``project_id``.

    Starts in status ``'queued'``. Numbered via a bounded retry loop against
    the ``(run_id, attempt_number)`` unique index: a lost race against a
    concurrent :func:`create_attempt` call for the SAME run re-reads the
    current max and retries, up to :data:`_MAX_ATTEMPT_NUMBER_RETRIES`
    times, rather than trusting the read-then-write gap to never race.
    """
    project_id = (project_id or "").strip()
    run_id = (run_id or "").strip()
    run = await get_run(db, project_id, run_id)
    if run is None:
        raise ValueError(f"research run {run_id!r} not found in project {project_id!r}")

    last_exc: "Exception | None" = None
    for _ in range(_MAX_ATTEMPT_NUMBER_RETRIES):
        next_number = await _next_attempt_number(db, run_id)
        aid = _new_id()
        try:
            await db.execute(
                "INSERT INTO research_run_attempts "
                "(id, run_id, project_id, attempt_number, status, created_by) "
                "VALUES (?, ?, ?, ?, 'queued', ?)",
                (aid, run_id, project_id, next_number, created_by),
            )
        except Exception as exc:  # noqa: BLE001 — classified below
            if _is_unique_violation(exc):
                last_exc = exc
                continue
            raise
        await db.execute(
            "UPDATE research_runs SET attempt_count = attempt_count + 1, updated_at = ? "
            "WHERE id = ? AND project_id = ?",
            (_now_iso(), run_id, project_id),
        )
        await db.commit()
        created = await get_attempt(db, project_id, aid)
        assert created is not None  # just written
        return created
    raise RuntimeError(
        f"create_attempt: exhausted {_MAX_ATTEMPT_NUMBER_RETRIES} retries numbering an "
        f"attempt for run {run_id!r}"
    ) from last_exc


async def _next_attempt_number(db: aiosqlite.Connection, run_id: str) -> int:
    """Mirrors ``research_graph._next_node_sequence``'s identical
    ``COALESCE(MAX(...), 0) + 1`` pattern."""
    async with db.execute(
        "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS next_n "
        "FROM research_run_attempts WHERE run_id = ?",
        (run_id,),
    ) as cur:
        row = await cur.fetchone()
    return int((row["next_n"] if row is not None else 1) or 1)


async def get_attempt(
    db: aiosqlite.Connection, project_id: str, attempt_id: str
) -> "dict[str, Any] | None":
    """Fetch one attempt by id, scoped to ``project_id``."""
    async with db.execute(
        "SELECT * FROM research_run_attempts WHERE id = ? AND project_id = ?",
        (attempt_id, project_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_attempt(row)


async def list_run_attempts(
    db: aiosqlite.Connection, project_id: str, run_id: str
) -> list[dict[str, Any]]:
    """Every attempt for ``run_id``, oldest first — the full retry history."""
    async with db.execute(
        "SELECT * FROM research_run_attempts WHERE project_id = ? AND run_id = ? "
        "ORDER BY attempt_number ASC",
        (project_id, run_id),
    ) as cur:
        rows = await cur.fetchall()
    return [a for a in (_row_to_attempt(r) for r in rows) if a is not None]


async def transition_attempt(
    db: aiosqlite.Connection,
    project_id: str,
    attempt_id: str,
    new_status: str,
    *,
    failure_class: "str | None" = None,
    error_message: "str | None" = None,
    checkpoint_ref: "dict[str, Any] | None" = None,
    artifact_refs: "list[Any] | None" = None,
    provenance_ref: "dict[str, Any] | None" = None,
) -> dict[str, Any]:
    """Validate and apply ``current -> new_status`` on an attempt (see
    ``meridian.experiment_model.validate_attempt_transition`` for the legal
    transition table). Idempotent: transitioning to the attempt's CURRENT
    status is always a no-op success, never a ``ValueError``.

    ``failure_class`` is REQUIRED when ``new_status`` is ``'failed'`` or
    ``'crashed'`` and REJECTED (must be omitted/``None``) otherwise — an
    attempt that never failed has no failure to classify. ``started_at`` is
    stamped the first time an attempt enters ``'running'``; ``ended_at`` is
    stamped on any transition INTO a terminal status (see
    :data:`meridian.experiment_model.ATTEMPT_TERMINAL_STATUSES`).
    ``checkpoint_ref``/``artifact_refs``/``provenance_ref`` are optional and,
    when given, REPLACE the attempt's stored value (not merged) — pass the
    full desired value each time.
    """
    project_id = (project_id or "").strip()
    current = await get_attempt(db, project_id, attempt_id)
    if current is None:
        raise ValueError(f"run attempt {attempt_id!r} not found in project {project_id!r}")

    validated_status = validate_attempt_transition(current["status"], new_status)

    if validated_status in ("failed", "crashed"):
        if failure_class is None and current["status"] == validated_status:
            # Idempotent self-transition (failed -> failed / crashed -> crashed):
            # reuse the existing classification rather than demanding the
            # caller re-supply it — a true no-op must never raise.
            failure_class = current.get("failure_class")
        if failure_class is None:
            raise ValueError(
                f"transitioning to {validated_status!r} requires a failure_class "
                f"(one of {sorted(FAILURE_CLASSES)})"
            )
        failure_class = validate_failure_class(failure_class)
    elif failure_class is not None:
        raise ValueError(
            f"failure_class is only valid when transitioning to 'failed' or 'crashed', "
            f"not {validated_status!r}"
        )

    if error_message is not None:
        from meridian.secret_redaction import check_for_secrets  # noqa: PLC0415
        check_for_secrets(error_message, context="run attempt error_message")

    now = _now_iso()
    set_clauses = ["status = ?", "updated_at = ?"]
    params: list[Any] = [validated_status, now]

    # failure_class is cleared whenever we move OFF a failed/crashed status
    # (e.g. a reconciled 'unknown' -> 'succeeded' should not keep a stale
    # failure classification lying around).
    set_clauses.append("failure_class = ?")
    params.append(failure_class)

    if error_message is not None:
        set_clauses.append("error_message = ?")
        params.append(error_message)
    if checkpoint_ref is not None:
        set_clauses.append("checkpoint_ref = ?")
        params.append(_json_dumps(checkpoint_ref))
    if artifact_refs is not None:
        set_clauses.append("artifact_refs = ?")
        params.append(_json_dumps(artifact_refs))
    if provenance_ref is not None:
        set_clauses.append("provenance_ref = ?")
        params.append(_json_dumps(provenance_ref))
    if validated_status == "running" and current.get("started_at") is None:
        set_clauses.append("started_at = ?")
        params.append(now)
    if validated_status in ATTEMPT_TERMINAL_STATUSES and current.get("ended_at") is None:
        set_clauses.append("ended_at = ?")
        params.append(now)

    params.extend([attempt_id, project_id])
    await db.execute(
        f"UPDATE research_run_attempts SET {', '.join(set_clauses)} "
        f"WHERE id = ? AND project_id = ?",
        params,
    )
    await db.commit()
    updated = await get_attempt(db, project_id, attempt_id)
    assert updated is not None
    return updated


async def heartbeat_attempt(
    db: aiosqlite.Connection, project_id: str, attempt_id: str
) -> dict[str, Any]:
    """Bump ``last_heartbeat_at`` on a live (queued/running) attempt without
    changing its status — the liveness signal :func:`reconcile_stale_attempts`
    checks against. Raises ``ValueError`` if the attempt is already terminal
    (a finished attempt has nothing left to heartbeat)."""
    project_id = (project_id or "").strip()
    current = await get_attempt(db, project_id, attempt_id)
    if current is None:
        raise ValueError(f"run attempt {attempt_id!r} not found in project {project_id!r}")
    if current["status"] in ATTEMPT_TERMINAL_STATUSES:
        raise ValueError(
            f"cannot heartbeat attempt {attempt_id!r}: already terminal ({current['status']!r})"
        )
    now = _now_iso()
    await db.execute(
        "UPDATE research_run_attempts SET last_heartbeat_at = ?, updated_at = ? "
        "WHERE id = ? AND project_id = ?",
        (now, now, attempt_id, project_id),
    )
    await db.commit()
    updated = await get_attempt(db, project_id, attempt_id)
    assert updated is not None
    return updated


async def reconcile_stale_attempts(
    db: aiosqlite.Connection, project_id: str, *, stale_after_seconds: int = 900
) -> list[dict[str, Any]]:
    """Restart recovery: transition every ``queued``/``running`` attempt in
    ``project_id`` whose last observed activity is older than
    ``stale_after_seconds`` to ``'unknown'`` — truthfully reporting "we no
    longer know what happened to this" instead of guessing ``'failed'``.

    "Last observed activity" is ``COALESCE(last_heartbeat_at, started_at,
    created_at)`` — an attempt that never got a heartbeat still ages out
    based on when it was created/started, so a process that died before
    ever heartbeating doesn't stay ``'running'`` forever. Returns the list
    of reconciled attempts (empty if nothing was stale). Safe to call
    repeatedly (e.g. on every server startup): an attempt already resolved
    to a terminal status is never touched.
    """
    project_id = (project_id or "").strip()
    cutoff = datetime.now(timezone.utc).timestamp() - stale_after_seconds
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    async with db.execute(
        "SELECT id FROM research_run_attempts WHERE project_id = ? "
        "AND status IN ('queued', 'running') "
        "AND COALESCE(last_heartbeat_at, started_at, created_at) < ?",
        (project_id, cutoff_iso),
    ) as cur:
        rows = await cur.fetchall()

    reconciled: list[dict[str, Any]] = []
    for row in rows:
        updated = await transition_attempt(db, project_id, row["id"], "unknown")
        reconciled.append(updated)
    return reconciled
