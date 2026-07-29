"""Durable wave-run state, append/supersede history, and idempotent finalization (2a654cb0).

Why this exists (incident writeup): a paused multi-agent wave had no durable
identity. When the orchestrating session died mid-wave there was nothing on
disk that said *which* wave was running, *what board* it was running against,
*which children* had already finished, or *whether* the wave had been
finalized. Resuming meant re-deriving all of it from prose in a handoff — and
a retried finalization silently double-completed work because "already
finalized" was not a state anyone could query.

This module gives callers three durable artefacts:

  1. **The wave run** (``wave_runs``) — an immutable ``wave_run_id`` plus an
     explicitly enumerated status (:data:`WAVE_RUN_STATUSES`) advanced only
     through :func:`advance_wave_run_status`, which rejects any transition
     not in :data:`WAVE_RUN_TRANSITIONS`. The run pins the canonical board
     snapshot it was planned against (``board_snapshot`` + ``revision_hash``
     + ``revision_counter``, produced by :mod:`meridian.db.board_snapshot`,
     ef665ef8) so a resumed session can tell "the board I was planned against"
     from "the board as it is now".

  2. **The history** (``wave_run_events``) — strictly append-only, with a
     monotonic per-run ``seq``. Events are NEVER updated or deleted. A
     correction is expressed by *superseding*: :func:`supersede_wave_run_event`
     appends a NEW event describing the correction and sets only the
     ``superseded_by`` pointer column on the old row. The superseded event's
     own body (``event_type``/``detail``/``payload``) stays byte-identical
     forever, so the history remains a faithful record of what was believed at
     each point — which is exactly what a post-mortem of a paused wave needs.

  3. **The children** (``wave_run_children``) — one row per sprint item in the
     wave, carrying that item's ``failure_mode`` ('stop' | 'continue') and
     outcome. This is what makes the stop-mode contract enforceable rather
     than advisory: :func:`finalize_wave_run` REFUSES to finalize while any
     ``failure_mode='stop'`` child is in ``status='failed'``.

Design decisions (documented per the sprint-item spec):

* **Why a state machine and not a pair of booleans.** The seven live statuses
  are not decoration — each is a genuinely different resume action.
  ``paused`` resumes by re-running; ``rebase_required`` resumes only after the
  worktree is rebased; ``awaiting_human`` must NOT be auto-resumed at all;
  ``ready_to_resume`` is the only state a resume tool may pick up
  unattended. Collapsing them into "paused + a reason string" is what made
  the original incident un-automatable. Transitions are enforced in one
  place (:data:`WAVE_RUN_TRANSITIONS`) rather than checked ad hoc at call
  sites, following the ``advance_workspace_proposal_status`` precedent in
  :mod:`meridian.db.workspace`.

* **Why finalization is idempotent rather than guarded by a UNIQUE index.**
  A UNIQUE constraint would make the second finalize *raise*, and a retry
  after a dropped connection is not an error — the caller genuinely does not
  know whether its first call landed. So finalizing an already-``merged`` run
  returns the ORIGINAL result with ``already_finalized=True``, writes no row,
  and appends no event. The event count is the observable proof: it is
  identical after the second call. Retry-safety is the whole point; a raise
  would push the retry logic onto every caller.

* **Why evidence validation is shared with complete_wave_gate.** The finalizer
  evidence contract is deliberately the SAME one d2430713 established for
  wave gates: the real structured ``run_verification`` payload, with
  ``status == 'ok'`` and ``exit_code == 0``. A self-reported boolean is
  rejected. Two different "did the tests pass" contracts in one codebase is
  how one of them ends up being the weak one.

* **Why degraded-tool provenance lives on the run.** When Serena or the code
  graph is unavailable, the work still happens — but the *evidence quality*
  of everything produced in that window is lower, and a later reader has no
  way to know. :func:`record_degraded_tool` stamps that fact onto the run
  itself (and into the append-only history), so a resumed session can see
  "this wave's prospecting ran without Serena" instead of silently trusting
  it. Deduplicated on ``(tool, reason)`` so a tool that degrades on every
  call in a loop records one entry, not thousands.
"""
from __future__ import annotations

import json
from typing import Any

import aiosqlite

# Shared helpers from the parent db package — available at import time because
# wave_runs.py is imported at the bottom of db/__init__.py, AFTER the helper
# definitions (same pattern as board_snapshot.py, ef665ef8).
from meridian.db import (  # noqa: PLC0415
    _new_id,
    _row_to_dict,
)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

#: Every valid wave-run status. See the module docstring for why each live
#: status is a distinct resume action rather than a reason string.
WAVE_RUN_STATUSES: frozenset[str] = frozenset({
    "planned",           # created, board snapshot pinned, nothing dispatched yet
    "running",           # children dispatched / in flight
    "paused",            # deliberately halted; resumable by re-running
    "awaiting_human",    # blocked on a human decision — must NOT auto-resume
    "rebase_required",   # base moved under the wave; rebase before resuming
    "ready_to_resume",   # cleared to be picked up unattended by a resume tool
    "merged",            # finalized (terminal)
    "aborted",           # abandoned (terminal)
})

#: Terminal statuses — no outgoing transitions.
WAVE_RUN_TERMINAL_STATUSES: frozenset[str] = frozenset({"merged", "aborted"})

#: The enforced transition table. Any (from, to) pair not listed here is
#: rejected by :func:`advance_wave_run_status` with a ValueError naming the
#: allowed set — the same shape as ``_PROPOSAL_TRANSITIONS`` in
#: :mod:`meridian.db.workspace`.
WAVE_RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"running", "aborted"}),
    "running": frozenset({
        "paused", "awaiting_human", "rebase_required",
        "ready_to_resume", "merged", "aborted",
    }),
    "paused": frozenset({
        "running", "ready_to_resume", "rebase_required", "awaiting_human", "aborted",
    }),
    "awaiting_human": frozenset({
        "running", "paused", "rebase_required", "ready_to_resume", "aborted",
    }),
    "rebase_required": frozenset({
        "ready_to_resume", "awaiting_human", "paused", "aborted",
    }),
    # ready_to_resume may go straight to merged: a wave whose children all
    # finished while the orchestrator was away is finalizable without a
    # pointless round-trip back through 'running'.
    "ready_to_resume": frozenset({"running", "paused", "merged", "aborted"}),
    "merged": frozenset(),
    "aborted": frozenset(),
}

#: Valid child failure modes. Mirrors the sprint_items.failure_mode column.
WAVE_RUN_CHILD_FAILURE_MODES: frozenset[str] = frozenset({"stop", "continue"})

#: Valid child outcome statuses.
WAVE_RUN_CHILD_STATUSES: frozenset[str] = frozenset({
    "running", "succeeded", "failed", "skipped",
})


class WaveRunFinalizationBlocked(ValueError):
    """Raised when a ``failure_mode='stop'`` child has failed.

    Subclasses ValueError so existing ``except ValueError`` handlers (the MCP
    handler convention in this codebase) keep working unchanged, while callers
    that care can catch this specific type and read
    :attr:`blocking_children` to report exactly which items block the merge.
    """

    def __init__(self, message: str, blocking_children: list[dict[str, Any]]):
        super().__init__(message)
        self.blocking_children = blocking_children


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _json_or_none(value: Any) -> str | None:
    """Serialize to JSON, or return None for None. Never raises on odd values."""
    if value is None:
        return None
    return json.dumps(value, default=str)


def _loads_or_default(raw: Any, default: Any) -> Any:
    """Parse a stored JSON column, falling back to ``default`` on any garbage.

    Stored JSON is written only by this module, but a hand-edited row or a
    partially-migrated DB must not crash a read path — a wave run whose
    degraded_tools column is unreadable is still a wave run.
    """
    if raw in (None, ""):
        return default
    if not isinstance(raw, (str, bytes, bytearray)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _hydrate_run(row: Any) -> dict[str, Any] | None:
    """Row -> dict with the JSON columns parsed into real Python structures."""
    if row is None:
        return None
    run = _row_to_dict(row)
    if run is None:
        return None
    run["board_snapshot"] = _loads_or_default(run.get("board_snapshot"), None)
    run["item_ids"] = _loads_or_default(run.get("item_ids"), [])
    run["degraded_tools"] = _loads_or_default(run.get("degraded_tools"), [])
    run["finalizer_evidence"] = _loads_or_default(run.get("finalizer_evidence"), None)
    return run


def _hydrate_event(row: Any) -> dict[str, Any] | None:
    """Row -> dict with the event payload parsed."""
    if row is None:
        return None
    event = _row_to_dict(row)
    if event is None:
        return None
    event["payload"] = _loads_or_default(event.get("payload"), None)
    return event


async def _next_event_seq(db: aiosqlite.Connection, wave_run_id: str) -> int:
    """Next monotonic per-run event sequence number (1-based).

    Derived from MAX(seq) rather than a counter column so it stays correct if
    an event is inserted by any other code path; the UNIQUE(wave_run_id, seq)
    constraint is what actually guarantees no two events ever share a slot.
    """
    async with db.execute(
        "SELECT MAX(seq) FROM wave_run_events WHERE wave_run_id = ?",
        (wave_run_id,),
    ) as cur:
        row = await cur.fetchone()
    current = None
    if row is not None:
        current = row[0] if not isinstance(row, dict) else list(row.values())[0]
    return int(current or 0) + 1


# ---------------------------------------------------------------------------
# Append-only history
# ---------------------------------------------------------------------------

async def append_wave_run_event(
    db: aiosqlite.Connection,
    wave_run_id: str,
    event_type: str,
    *,
    from_status: str | None = None,
    to_status: str | None = None,
    detail: str | None = None,
    payload: Any = None,
    actor: str | None = None,
    supersedes: str | None = None,
) -> dict[str, Any]:
    """Append one immutable event to a wave run's history.

    The ONLY way rows enter ``wave_run_events``. Nothing in this module ever
    UPDATEs an event's body or DELETEs an event — see
    :func:`supersede_wave_run_event` for how corrections are expressed.

    Returns the created event dict (payload already parsed).
    """
    event_id = _new_id()
    seq = await _next_event_seq(db, wave_run_id)
    await db.execute(
        "INSERT INTO wave_run_events "
        "(id, wave_run_id, seq, event_type, from_status, to_status, detail, "
        " payload, actor, supersedes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            wave_run_id,
            seq,
            event_type,
            from_status,
            to_status,
            detail,
            _json_or_none(payload),
            actor,
            supersedes,
        ),
    )
    await db.commit()
    return {
        "id": event_id,
        "wave_run_id": wave_run_id,
        "seq": seq,
        "event_type": event_type,
        "from_status": from_status,
        "to_status": to_status,
        "detail": detail,
        "payload": payload,
        "actor": actor,
        "supersedes": supersedes,
        "superseded_by": None,
    }


async def get_wave_run_events(
    db: aiosqlite.Connection,
    wave_run_id: str,
    *,
    include_superseded: bool = True,
) -> list[dict[str, Any]]:
    """Return a wave run's history in ``seq`` order (oldest first).

    ``include_superseded=False`` hides events that a later event corrected —
    useful for rendering "what is believed now". The default keeps them,
    because the audit trail is the point.
    """
    sql = "SELECT * FROM wave_run_events WHERE wave_run_id = ?"
    if not include_superseded:
        sql += " AND superseded_by IS NULL"
    sql += " ORDER BY seq"
    async with db.execute(sql, (wave_run_id,)) as cur:
        rows = await cur.fetchall()
    return [e for e in (_hydrate_event(r) for r in rows) if e is not None]


async def supersede_wave_run_event(
    db: aiosqlite.Connection,
    wave_run_id: str,
    event_id: str,
    *,
    detail: str | None = None,
    payload: Any = None,
    actor: str | None = None,
    event_type: str = "superseded",
) -> dict[str, Any]:
    """Correct an earlier event by APPENDING a superseding one.

    The superseded event's body is never touched — only its ``superseded_by``
    pointer is set, to the id of the new event. This keeps the history a
    faithful record of what was believed at each point in time while still
    letting a reader follow the pointer to the correction.

    Raises ValueError if the target event does not exist on this run, or if it
    has already been superseded (a correction chain must be linear — two
    concurrent corrections of the same event would make "what is believed now"
    ambiguous, which is the exact failure this whole module exists to remove).
    """
    async with db.execute(
        "SELECT * FROM wave_run_events WHERE id = ? AND wave_run_id = ?",
        (event_id, wave_run_id),
    ) as cur:
        row = await cur.fetchone()
    target = _hydrate_event(row)
    if target is None:
        raise ValueError(
            f"Event {event_id!r} not found on wave run {wave_run_id!r}."
        )
    if target.get("superseded_by"):
        raise ValueError(
            f"Event {event_id!r} has already been superseded by "
            f"{target['superseded_by']!r}. Supersede the newest event in the "
            f"chain, not an already-corrected one."
        )

    new_event = await append_wave_run_event(
        db,
        wave_run_id,
        event_type,
        from_status=target.get("from_status"),
        to_status=target.get("to_status"),
        detail=detail,
        payload=payload,
        actor=actor,
        supersedes=event_id,
    )
    await db.execute(
        "UPDATE wave_run_events SET superseded_by = ? WHERE id = ?",
        (new_event["id"], event_id),
    )
    await db.commit()
    return new_event


# ---------------------------------------------------------------------------
# Wave runs
# ---------------------------------------------------------------------------

async def create_wave_run(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    version: str | None = None,
    wave_label: str | None = None,
    snapshot: dict[str, Any] | None = None,
    item_ids: list[str] | None = None,
    degraded_tools: list[dict[str, Any]] | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Create a wave run in status ``planned`` and pin its board snapshot.

    ``snapshot`` should be a dict from
    :func:`meridian.db.board_snapshot.build_board_snapshot`. When supplied its
    ``revision_hash`` is stored alongside the full snapshot, and the monotonic
    revision counter is recorded via ``record_board_snapshot_revision`` so a
    resumed session can tell "newer board" from merely "different board".
    Passing no snapshot is allowed (a run can be created before the board is
    read) — ``revision_hash`` is then NULL and staleness cannot be checked.

    The returned ``id`` is the immutable ``wave_run_id``: nothing in this
    module ever changes it, and it is the join key for events and children.
    """
    run_id = _new_id()
    revision_hash = None
    revision_counter = None
    if snapshot is not None:
        revision_hash = snapshot.get("revision_hash")
        # Imported lazily: board_snapshot imports from the db package too, and
        # a module-level import here would depend on db/__init__ import order.
        from meridian.db import record_board_snapshot_revision  # noqa: PLC0415
        try:
            recorded = await record_board_snapshot_revision(
                db, project_id, snapshot, version=version,
            )
            revision_counter = recorded.get("revision_counter")
        except Exception:  # pragma: no cover - revision bookkeeping is advisory
            # A run whose counter could not be recorded is still a valid run;
            # the hash alone still detects change, just not ordering.
            revision_counter = None

    await db.execute(
        "INSERT INTO wave_runs "
        "(id, project_id, version, wave_label, status, board_snapshot, "
        " revision_hash, revision_counter, item_ids, degraded_tools, actor) "
        "VALUES (?, ?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            project_id,
            version,
            wave_label,
            _json_or_none(snapshot),
            revision_hash,
            revision_counter,
            json.dumps(list(item_ids or [])),
            json.dumps(list(degraded_tools or [])),
            actor,
        ),
    )
    await db.commit()

    await append_wave_run_event(
        db,
        run_id,
        "created",
        to_status="planned",
        detail=f"wave run created for project {project_id}",
        payload={
            "version": version,
            "wave_label": wave_label,
            "item_ids": list(item_ids or []),
            "revision_hash": revision_hash,
            "revision_counter": revision_counter,
        },
        actor=actor,
    )

    run = await get_wave_run(db, run_id)
    assert run is not None  # just inserted
    return run


async def get_wave_run(
    db: aiosqlite.Connection, wave_run_id: str,
) -> dict[str, Any] | None:
    """Return one wave run with its JSON columns parsed, or None if absent."""
    async with db.execute(
        "SELECT * FROM wave_runs WHERE id = ?", (wave_run_id,),
    ) as cur:
        row = await cur.fetchone()
    return _hydrate_run(row)


async def list_wave_runs(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    status: str | None = None,
    version: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List a project's wave runs, newest first.

    ``status`` and ``version`` are optional filters. ``limit`` is clamped to
    [1, 500] so a caller cannot accidentally pull an unbounded result set.
    """
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if status:
        clauses.append("status = ?")
        params.append(status)
    if version:
        clauses.append("version = ?")
        params.append(version)
    params.append(max(1, min(int(limit), 500)))
    async with db.execute(
        f"SELECT * FROM wave_runs WHERE {' AND '.join(clauses)} "
        f"ORDER BY created_at DESC, id DESC LIMIT ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [r for r in (_hydrate_run(row) for row in rows) if r is not None]


async def advance_wave_run_status(
    db: aiosqlite.Connection,
    wave_run_id: str,
    new_status: str,
    *,
    actor: str | None = None,
    detail: str | None = None,
    payload: Any = None,
) -> dict[str, Any]:
    """Transition a wave run, enforcing :data:`WAVE_RUN_TRANSITIONS`.

    Appends exactly one ``status_changed`` event (``event_type='resumed'``
    when leaving a halted state for ``running``/``ready_to_resume``, mirroring
    ``advance_workspace_proposal_status``'s own resumed-event convention).

    Raises ValueError on an unknown status, an unknown run, or a transition
    the table forbids — including any transition out of a terminal status.
    """
    if new_status not in WAVE_RUN_STATUSES:
        raise ValueError(
            f"Invalid wave-run status {new_status!r}. "
            f"Valid: {sorted(WAVE_RUN_STATUSES)}"
        )
    run = await get_wave_run(db, wave_run_id)
    if run is None:
        raise ValueError(f"Wave run {wave_run_id!r} not found.")

    current = run["status"]
    allowed = WAVE_RUN_TRANSITIONS.get(current, frozenset())
    if new_status not in allowed:
        raise ValueError(
            f"Cannot transition wave run {wave_run_id!r} from {current!r} to "
            f"{new_status!r}. Allowed from {current!r}: "
            f"{sorted(allowed) or '(none — terminal)'}."
        )

    await db.execute(
        "UPDATE wave_runs SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (new_status, wave_run_id),
    )
    await db.commit()

    event_type = (
        "resumed"
        if current in {"paused", "awaiting_human", "rebase_required"}
        and new_status in {"running", "ready_to_resume"}
        else "status_changed"
    )
    await append_wave_run_event(
        db,
        wave_run_id,
        event_type,
        from_status=current,
        to_status=new_status,
        detail=detail or f"{current} -> {new_status}",
        payload=payload,
        actor=actor,
    )

    updated = await get_wave_run(db, wave_run_id)
    assert updated is not None  # existed a statement ago
    return updated


async def record_degraded_tool(
    db: aiosqlite.Connection,
    wave_run_id: str,
    tool: str,
    reason: str,
    *,
    fallback: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Stamp "this wave ran with ``tool`` degraded/unavailable" onto the run.

    Appended to the run's ``degraded_tools`` list AND to the append-only
    history. Deduplicated on ``(tool, reason)`` so a tool that degrades on
    every call in a loop contributes one entry, not thousands — the second
    call with the same pair is a no-op and appends no event.

    Returns the updated run.
    """
    run = await get_wave_run(db, wave_run_id)
    if run is None:
        raise ValueError(f"Wave run {wave_run_id!r} not found.")

    entries = list(run.get("degraded_tools") or [])
    for existing in entries:
        if (
            isinstance(existing, dict)
            and existing.get("tool") == tool
            and existing.get("reason") == reason
        ):
            return run  # already recorded — no duplicate entry, no duplicate event

    entry = {"tool": tool, "reason": reason, "fallback": fallback}
    entries.append(entry)
    await db.execute(
        "UPDATE wave_runs SET degraded_tools = ?, updated_at = datetime('now') "
        "WHERE id = ?",
        (json.dumps(entries), wave_run_id),
    )
    await db.commit()

    await append_wave_run_event(
        db,
        wave_run_id,
        "tool_degraded",
        detail=f"{tool} degraded: {reason}",
        payload=entry,
        actor=actor,
    )

    updated = await get_wave_run(db, wave_run_id)
    assert updated is not None
    return updated


# ---------------------------------------------------------------------------
# Children
# ---------------------------------------------------------------------------

async def record_wave_run_child(
    db: aiosqlite.Connection,
    wave_run_id: str,
    sprint_item_id: str,
    *,
    failure_mode: str = "continue",
    status: str = "running",
    evidence: Any = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Upsert one child (sprint item) of a wave run and record the change.

    Idempotent per ``(wave_run_id, sprint_item_id)``: calling again updates
    the existing row rather than inserting a duplicate. A call that changes
    nothing appends no event, so a polling orchestrator does not flood the
    history.

    ``failure_mode='stop'`` is the contract :func:`finalize_wave_run`
    enforces: if such a child ends ``status='failed'``, the wave cannot be
    finalized until that is resolved.
    """
    if failure_mode not in WAVE_RUN_CHILD_FAILURE_MODES:
        raise ValueError(
            f"Invalid failure_mode {failure_mode!r}. "
            f"Valid: {sorted(WAVE_RUN_CHILD_FAILURE_MODES)}"
        )
    if status not in WAVE_RUN_CHILD_STATUSES:
        raise ValueError(
            f"Invalid child status {status!r}. "
            f"Valid: {sorted(WAVE_RUN_CHILD_STATUSES)}"
        )
    run = await get_wave_run(db, wave_run_id)
    if run is None:
        raise ValueError(f"Wave run {wave_run_id!r} not found.")

    async with db.execute(
        "SELECT * FROM wave_run_children WHERE wave_run_id = ? AND sprint_item_id = ?",
        (wave_run_id, sprint_item_id),
    ) as cur:
        row = await cur.fetchone()
    existing = _row_to_dict(row) if row is not None else None

    if existing is None:
        await db.execute(
            "INSERT INTO wave_run_children "
            "(id, wave_run_id, sprint_item_id, failure_mode, status, evidence, actor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _new_id(),
                wave_run_id,
                sprint_item_id,
                failure_mode,
                status,
                _json_or_none(evidence),
                actor,
            ),
        )
        await db.commit()
        await append_wave_run_event(
            db,
            wave_run_id,
            "child_recorded",
            detail=f"{sprint_item_id} -> {status} (failure_mode={failure_mode})",
            payload={
                "sprint_item_id": sprint_item_id,
                "status": status,
                "failure_mode": failure_mode,
            },
            actor=actor,
        )
    elif existing.get("status") != status or existing.get("failure_mode") != failure_mode:
        await db.execute(
            "UPDATE wave_run_children SET failure_mode = ?, status = ?, "
            "evidence = COALESCE(?, evidence), actor = COALESCE(?, actor), "
            "updated_at = datetime('now') "
            "WHERE wave_run_id = ? AND sprint_item_id = ?",
            (
                failure_mode,
                status,
                _json_or_none(evidence),
                actor,
                wave_run_id,
                sprint_item_id,
            ),
        )
        await db.commit()
        await append_wave_run_event(
            db,
            wave_run_id,
            "child_status_changed",
            detail=(
                f"{sprint_item_id}: {existing.get('status')} -> {status} "
                f"(failure_mode={failure_mode})"
            ),
            payload={
                "sprint_item_id": sprint_item_id,
                "from": existing.get("status"),
                "to": status,
                "failure_mode": failure_mode,
            },
            actor=actor,
        )

    async with db.execute(
        "SELECT * FROM wave_run_children WHERE wave_run_id = ? AND sprint_item_id = ?",
        (wave_run_id, sprint_item_id),
    ) as cur:
        row = await cur.fetchone()
    child = _row_to_dict(row)
    assert child is not None
    child["evidence"] = _loads_or_default(child.get("evidence"), None)
    return child


async def get_wave_run_children(
    db: aiosqlite.Connection, wave_run_id: str,
) -> list[dict[str, Any]]:
    """Return a wave run's children, oldest first, with evidence parsed."""
    async with db.execute(
        "SELECT * FROM wave_run_children WHERE wave_run_id = ? "
        "ORDER BY created_at, sprint_item_id",
        (wave_run_id,),
    ) as cur:
        rows = await cur.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        child = _row_to_dict(row)
        if child is None:
            continue
        child["evidence"] = _loads_or_default(child.get("evidence"), None)
        out.append(child)
    return out


def _blocking_children(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Children that block finalization: failure_mode='stop' AND status='failed'."""
    return [
        c for c in children
        if c.get("failure_mode") == "stop" and c.get("status") == "failed"
    ]


# ---------------------------------------------------------------------------
# Finalization
# ---------------------------------------------------------------------------

def _validate_finalizer_evidence(evidence: Any) -> tuple[Any, Any]:
    """Validate finalizer evidence against the d2430713 run_verification contract.

    Deliberately the SAME contract ``complete_wave_gate`` enforces — the real
    structured ``run_verification`` payload with ``status == 'ok'`` and
    ``exit_code == 0``. Returns ``(status, exit_code)``; raises ValueError with
    an actionable diagnostic otherwise.
    """
    if not isinstance(evidence, dict):
        raise ValueError(
            "finalize_wave_run requires an evidence dict (the full result from "
            "run_verification). Pass the dict directly — a boolean or a "
            "self-report is rejected."
        )
    status = evidence.get("status")
    exit_code = evidence.get("exit_code")
    if status == "not_configured":
        raise ValueError(
            "Finalization rejected: run_verification returned "
            "status='not_configured' — no test_cmd is set for this project. "
            "Configure executor_config.test_cmd via set_executor_config, run "
            "run_verification, and pass its real result."
        )
    if status == "not_connected":
        raise ValueError(
            "Finalization rejected: run_verification returned "
            "status='not_connected' — the tunnel is not active. Start "
            "meridian --tunnel locally, run run_verification so it executes "
            "the REAL test suite, and pass its result."
        )
    if status == "error":
        raise ValueError(
            f"Finalization rejected: run_verification returned status='error' — "
            f"the test runner itself crashed or was not found. Fix the command, "
            f"re-run run_verification, and pass its result. Payload: {evidence!r}"
        )
    if status != "ok":
        raise ValueError(
            f"Finalization rejected: evidence.status must be 'ok' but got "
            f"{status!r}. Only a genuinely successful run_verification result "
            f"(status='ok', exit_code=0) can finalize a wave run."
        )
    if exit_code != 0:
        raise ValueError(
            f"Finalization rejected: evidence.exit_code must be 0 but got "
            f"{exit_code!r} (failed={evidence.get('failed')!r}). Fix the "
            f"failures, re-run run_verification, and finalize with that result."
        )
    return status, exit_code


def _children_summary(children: list[dict[str, Any]]) -> dict[str, int]:
    """Count children by outcome status, for the finalization result."""
    summary = {s: 0 for s in sorted(WAVE_RUN_CHILD_STATUSES)}
    for child in children:
        key = str(child.get("status") or "")
        if key in summary:
            summary[key] += 1
    return summary


async def finalize_wave_run(
    db: aiosqlite.Connection,
    wave_run_id: str,
    *,
    evidence: Any = None,
    actor: str | None = None,
    expected_revision_hash: str | None = None,
) -> dict[str, Any]:
    """Finalize a wave run — idempotently — moving it to ``merged``.

    Order of checks matters and is deliberate:

      1. **Already merged → idempotent return.** Checked FIRST, before evidence
         validation, so a retry after a dropped connection succeeds even if the
         caller no longer has the original evidence payload to hand. Returns the
         stored ``finalizer_evidence`` and ``already_finalized=True``; writes no
         row and appends NO event. ``event_count`` in the result is the
         observable proof — it is identical across the retry.
      2. **Aborted → refuse.** An aborted run is terminal in the other
         direction; finalizing it would resurrect abandoned work.
      3. **Stale board → refuse.** When ``expected_revision_hash`` is supplied
         and does not match the hash pinned at creation, the caller is holding a
         manifest built against a different board. Fail closed with both hashes
         named, rather than merging against state the caller never saw.
      4. **Failed stop-mode child → refuse** (:class:`WaveRunFinalizationBlocked`,
         a ValueError subclass carrying ``blocking_children``). This is the
         enforcement point that makes ``failure_mode='stop'`` a real contract.
      5. **Evidence → validate** (see :func:`_validate_finalizer_evidence`).
      6. Transition to ``merged`` and append exactly ONE ``finalized`` event.

    Returns ``{finalized, already_finalized, wave_run_id, status, finalized_at,
    finalizer_evidence, children_summary, event_count}``.
    """
    run = await get_wave_run(db, wave_run_id)
    if run is None:
        raise ValueError(f"Wave run {wave_run_id!r} not found.")

    children = await get_wave_run_children(db, wave_run_id)

    # 1. Idempotent replay — no new row, no new event.
    if run["status"] == "merged":
        events = await get_wave_run_events(db, wave_run_id)
        return {
            "finalized": True,
            "already_finalized": True,
            "wave_run_id": wave_run_id,
            "status": "merged",
            "finalized_at": run.get("finalized_at"),
            "finalizer_evidence": run.get("finalizer_evidence"),
            "children_summary": _children_summary(children),
            "event_count": len(events),
        }

    # 2. Terminal in the other direction.
    if run["status"] == "aborted":
        raise ValueError(
            f"Wave run {wave_run_id!r} was aborted and cannot be finalized. "
            f"Aborted is terminal — start a new wave run instead."
        )

    # 3. Stale-manifest gate.
    if expected_revision_hash is not None:
        actual = run.get("revision_hash")
        if actual != expected_revision_hash:
            raise ValueError(
                f"Finalization rejected: stale board manifest. This wave run was "
                f"planned against revision_hash={actual!r} but the caller expected "
                f"{expected_revision_hash!r}. Re-read the board "
                f"(build_board_snapshot) and reconcile before finalizing."
            )

    # 4. Stop-mode contract.
    blocking = _blocking_children(children)
    if blocking:
        ids = ", ".join(str(c.get("sprint_item_id")) for c in blocking)
        raise WaveRunFinalizationBlocked(
            f"Finalization blocked: {len(blocking)} failure_mode='stop' child"
            f"{'ren' if len(blocking) != 1 else ''} failed ({ids}). Resolve or "
            f"re-run the failed item(s), or abort the wave run — a stop-mode "
            f"failure may not be merged past.",
            blocking,
        )

    # 5. Evidence contract (shared with complete_wave_gate, d2430713).
    _validate_finalizer_evidence(evidence)

    # 6. Transition + single finalized event.
    current = run["status"]
    allowed = WAVE_RUN_TRANSITIONS.get(current, frozenset())
    if "merged" not in allowed:
        raise ValueError(
            f"Cannot finalize wave run {wave_run_id!r} from status {current!r}. "
            f"Finalization is only valid from "
            f"{sorted(s for s, t in WAVE_RUN_TRANSITIONS.items() if 'merged' in t)}."
        )

    await db.execute(
        "UPDATE wave_runs SET status = 'merged', finalizer_evidence = ?, "
        "finalized_at = datetime('now'), updated_at = datetime('now') "
        "WHERE id = ?",
        (_json_or_none(evidence), wave_run_id),
    )
    await db.commit()

    await append_wave_run_event(
        db,
        wave_run_id,
        "finalized",
        from_status=current,
        to_status="merged",
        detail=f"wave run finalized ({current} -> merged)",
        payload={"evidence": evidence},
        actor=actor,
    )

    merged = await get_wave_run(db, wave_run_id)
    assert merged is not None
    events = await get_wave_run_events(db, wave_run_id)
    return {
        "finalized": True,
        "already_finalized": False,
        "wave_run_id": wave_run_id,
        "status": "merged",
        "finalized_at": merged.get("finalized_at"),
        "finalizer_evidence": merged.get("finalizer_evidence"),
        "children_summary": _children_summary(children),
        "event_count": len(events),
    }
