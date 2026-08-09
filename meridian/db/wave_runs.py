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


#: Deterministic, closed set of reasons that may justify declaring a wave
#: run's FOUNDATIONAL HYPOTHESIS systemically invalid, as distinct from an
#: ordinary per-item failure (cc3864bd). Each corresponds 1:1 to one of the
#: four examples the sprint-item spec names: a required Wave-1 invariant
#: failing, a shared artifact/schema assumption being disproven, independent
#: verification finding the SAME premise wrong across multiple items, or a
#: safety/integrity gate failing. This is the deterministic-policy half of
#: "a systemic signal may be declared only from explicit evidence and a
#: deterministic policy... do not let an LLM guess alone abort a run" — see
#: :func:`validate_systemic_invalidation_evidence`, which refuses anything
#: outside this closed set.
SYSTEMIC_INVALIDATION_REASONS: frozenset[str] = frozenset({
    "wave1_invariant_failed",
    "shared_artifact_disproven",
    "cross_item_premise_wrong",
    "safety_integrity_gate_failed",
})


class SystemicInvalidationRejected(ValueError):
    """Raised when systemic-invalidation evidence fails the deterministic
    policy gate — see :data:`SYSTEMIC_INVALIDATION_REASONS` and
    :func:`validate_systemic_invalidation_evidence`. Never raised for an
    ordinary per-item failure; that path is :class:`WaveRunFinalizationBlocked`
    (a failed ``failure_mode='stop'`` child) or a plain ``continue``-mode
    failure, both entirely untouched by this feature.
    """


def validate_systemic_invalidation_evidence(evidence: Any) -> dict[str, Any]:
    """Fail-closed validation of systemic-invalidation evidence.

    Requires a ``dict`` with:

      * ``reason_code`` — one of :data:`SYSTEMIC_INVALIDATION_REASONS`.
      * ``basis`` — a non-blank string naming the CONCRETE evidence (which
        invariant failed, which items independently found the same wrong
        premise, etc.). A bare ``reason_code`` with no ``basis`` is exactly
        the "LLM guess alone" the sprint-item spec prohibits.

    ``affected_item_ids`` (optional) must be a list of id strings when
    given — the sprint items this evidence implicates, beyond whatever the
    wave run already recorded as its own children/``item_ids``.

    Returns the evidence dict with ``affected_item_ids`` normalized to a
    (possibly empty) list. Raises :class:`SystemicInvalidationRejected` on
    anything else — never silently coerces, defaults, or guesses a missing
    field, mirroring :func:`_validate_finalizer_evidence`'s fail-closed
    discipline for the (unrelated) finalization evidence contract.
    """
    if not isinstance(evidence, dict):
        raise SystemicInvalidationRejected(
            "Systemic invalidation requires an evidence dict with "
            "'reason_code' and 'basis' — a bare string, boolean, or "
            "self-report is rejected. This is the deterministic policy gate "
            "named in the spec: an unsupported guess is never sufficient "
            "grounds to abort a wave run and block its dependents."
        )
    reason_code = evidence.get("reason_code")
    if reason_code not in SYSTEMIC_INVALIDATION_REASONS:
        raise SystemicInvalidationRejected(
            "Systemic invalidation rejected: reason_code must be one of "
            f"{sorted(SYSTEMIC_INVALIDATION_REASONS)}, got {reason_code!r}. "
            "An ordinary per-item failure should use the existing "
            "failure_mode='stop'/'continue' contract instead of this path."
        )
    basis = evidence.get("basis")
    if not isinstance(basis, str) or not basis.strip():
        raise SystemicInvalidationRejected(
            "Systemic invalidation rejected: evidence.basis must be a "
            "non-blank string describing the concrete evidence (which "
            "invariant failed, which items independently found the same "
            "wrong premise, etc.) — a reason_code with no basis is exactly "
            "the unsupported guess this gate exists to refuse."
        )
    affected = evidence.get("affected_item_ids") or []
    if not isinstance(affected, list) or not all(isinstance(i, str) for i in affected):
        raise SystemicInvalidationRejected(
            "Systemic invalidation rejected: affected_item_ids must be a "
            "list of sprint-item id strings when given."
        )
    out = dict(evidence)
    out["affected_item_ids"] = list(affected)
    return out


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


def _hydrate_child(row: Any) -> dict[str, Any] | None:
    """Row -> dict with a wave_run_children row's JSON columns parsed.

    7d71d6bc — shared by every reader of ``wave_run_children`` (the
    pre-existing :func:`record_wave_run_child` / :func:`get_wave_run_children`
    as well as the child-lease functions below) so ``evidence`` and
    ``dispatch_provenance`` are parsed identically everywhere instead of each
    call site re-implementing the same two ``_loads_or_default`` calls.
    """
    if row is None:
        return None
    child = _row_to_dict(row)
    if child is None:
        return None
    child["evidence"] = _loads_or_default(child.get("evidence"), None)
    child["dispatch_provenance"] = _loads_or_default(child.get("dispatch_provenance"), None)
    return child


def _parse_ts(value: Any) -> "Any | None":
    """Small, self-contained timestamp parser for lease-age comparisons.

    Deliberately NOT imported from
    :func:`meridian.db.sprint_items._parse_deferral_ts` (which does the same
    job) — that is a private helper of a sibling module, and reaching into it
    would create the exact cross-module coupling this file's own module
    docstring already avoids elsewhere (see the lazy-import notes on
    ``record_board_snapshot_revision`` / ``artifact_declaration`` above).
    Accepts the DB's space-separated ``YYYY-MM-DD HH:MM:SS`` form or
    ISO-8601 (``T`` separator, optional trailing ``Z``/offset/fractional
    seconds). Returns ``None`` on anything empty/unparseable — callers MUST
    treat that as "unknown", never coerce it into "definitely stale" or
    "definitely live" (see :func:`classify_wave_run_child_lease`).
    """
    from datetime import datetime as _dt_cls, timezone as _tz_cls

    if value is None or value == "":
        return None
    if isinstance(value, _dt_cls):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        s = s.replace("Z", "+00:00")
        dt = None
        try:
            dt = _dt_cls.fromisoformat(s)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = _dt_cls.strptime(s, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(_tz_cls.utc).replace(tzinfo=None)
    return dt


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


async def get_pinned_promotion_targets(
    db: aiosqlite.Connection, wave_run_id: str,
) -> list[dict[str, Any]]:
    """24f5146d — the docx promotion targets + base-hash preconditions this
    wave run pinned at creation (see ``create_wave_run(promotion_targets=...)``),
    or ``[]`` when none were ever pinned.

    Reads the append-only ``promotion_precondition_pinned`` event this module
    writes — no separate table. If ``create_wave_run`` were ever called more
    than once with ``promotion_targets`` for the same run (not a supported
    flow today, but defensive), the LATEST such event wins, matching this
    module's "append a new event to correct" philosophy elsewhere.

    Each entry is ``{"target_docx_path": str, "base_sha256": str | None}``.
    Used by :func:`finalize_wave_run` (promotion-evidence gate) and by
    :func:`meridian.db.wave_resume.check_wave_resume` (docx-target staleness
    detection).
    """
    events = await get_wave_run_events(db, wave_run_id, include_superseded=False)
    pinned: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") != "promotion_precondition_pinned":
            continue
        payload = event.get("payload") or {}
        targets = payload.get("targets")
        if isinstance(targets, list):
            pinned = [t for t in targets if isinstance(t, dict)]
    return pinned


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
    promotion_targets: list[str] | None = None,
) -> dict[str, Any]:
    """Create a wave run in status ``planned`` and pin its board snapshot.

    ``snapshot`` should be a dict from
    :func:`meridian.db.board_snapshot.build_board_snapshot`. When supplied its
    ``revision_hash`` is stored alongside the full snapshot, and the monotonic
    revision counter is recorded via ``record_board_snapshot_revision`` so a
    resumed session can tell "newer board" from merely "different board".
    Passing no snapshot is allowed (a run can be created before the board is
    read) — ``revision_hash`` is then NULL and staleness cannot be checked.

    ``promotion_targets`` (24f5146d, OPTIONAL) — a list of docx target paths
    this wave's items may promote script-run output into. When given, each
    target's CURRENT on-disk base sha256
    (:func:`meridian.artifact_declaration.compute_base_sha256`) is computed
    ONCE, right now, and pinned as an append-only
    ``promotion_precondition_pinned`` event (see
    :func:`get_pinned_promotion_targets`) — no new table/column; this reuses
    the SAME append-only ``wave_run_events`` history the board snapshot's own
    staleness story already relies on. A run created with no
    ``promotion_targets`` behaves EXACTLY as before this parameter existed:
    :func:`finalize_wave_run` requires no promotion evidence at all for it.
    Hashing is best-effort — an unreadable/missing target pins
    ``base_sha256=None`` (mirrors ``PatchManifest.create_from_file``'s own
    "unknown base" semantics) rather than failing wave creation.

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

    # 24f5146d — pin base-hash preconditions for any declared docx promotion
    # targets. Lazy import: meridian.artifact_declaration has no dependency
    # on meridian.db, but importing it at module scope here would still be
    # an unnecessary hard coupling for a module (wave_runs.py) most callers
    # use with zero promotion involvement at all.
    if promotion_targets:
        from meridian import artifact_declaration as _artifact_declaration  # noqa: PLC0415

        pinned: list[dict[str, Any]] = []
        for target in promotion_targets:
            target_str = str(target)
            try:
                base_sha256 = _artifact_declaration.compute_base_sha256(target_str)
            except Exception:  # noqa: BLE001 — a hashing failure never blocks wave creation
                base_sha256 = None
            pinned.append({"target_docx_path": target_str, "base_sha256": base_sha256})
        await append_wave_run_event(
            db,
            run_id,
            "promotion_precondition_pinned",
            detail=(
                f"pinned base-hash preconditions for {len(pinned)} docx "
                "promotion target(s)"
            ),
            payload={"targets": pinned},
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


async def abort_wave_run_systemic(
    db: aiosqlite.Connection,
    wave_run_id: str,
    *,
    evidence: Any,
    actor: str | None = None,
) -> dict[str, Any]:
    """Fail-closed: abort a wave run because its FOUNDATIONAL HYPOTHESIS was
    systemically invalidated (cc3864bd) — distinct from an ordinary per-item
    failure, which stays on the existing :class:`WaveRunFinalizationBlocked` /
    ``failure_mode`` contract untouched by this function.

    Order of operations (deliberately mirrors :func:`finalize_wave_run`'s own
    documented order):

      1. **Evidence → validate** (:func:`validate_systemic_invalidation_evidence`).
         A bare guess is refused before anything else happens.
      2. **Already aborted for the SAME reason → idempotent replay.** Checked
         before any other terminal-state handling so a retry after a dropped
         connection is safe: returns the ORIGINAL result, writes no row,
         appends no event, re-blocks nothing. Aborted for a DIFFERENT reason
         (an ordinary abort, or a different systemic reason) is refused —
         terminal history is never silently reinterpreted.
      3. **Merged → refuse.** Finalized work is never retroactively marked
         invalid; start a new wave run for the corrected work instead.
      4. **Transition-table check** — 'aborted' must be reachable from the
         run's current status (see :data:`WAVE_RUN_TRANSITIONS`; every
         non-terminal status already permits it, so this is defense in depth
         against a future change to that table, not a live restriction today).
      5. **Compute preserved vs. affected.** A child whose recorded outcome is
         ``'succeeded'`` is PRESERVED — never touched, never blocked; that
         independent evidence survives the abort exactly as recorded. Every
         other item this run's own ``item_ids``/children name, plus any
         ``evidence.affected_item_ids`` the caller named explicitly, is
         AFFECTED.
      6. **Transition to 'aborted' + block affected items.** Blocking reuses
         the existing hard-gate mechanism
         (:func:`meridian.db.sprint_items.block_sprint_items_for_systemic_invalidation`,
         the same ``blocker_kind`` enforcement point ``claim_sprint_item``
         already uses for ``'superseded'``) — no new sprint-item status value,
         so no existing status-dependent invariant elsewhere changes shape.
         Project-isolated and idempotent by construction — see that
         function's own docstring.
      7. **Emit a non-executable executor-to-planner corrective report.**
         Reuses the existing durable ``executor_reports`` data layer (9154aa9a)
         via :func:`meridian.db.executor_reports.create_executor_report` —
         deliberately NOT a new parallel mechanism. The report's ``blockers``
         entry carries the full evidence; ``recommended_next_actions`` tells
         the planner explicitly to review the evidence, create a corrected
         board revision, and start a NEW wave run rather than resuming or
         mixing this aborted one with the fix. ``idempotency_key`` is
         ``f"systemic-invalidation:{wave_run_id}:{reason_code}"`` so a retried
         call (outside the already-aborted fast path — e.g. the DB write for
         the run itself landed but the caller's connection dropped before it
         saw the response) reuses the same report row instead of duplicating
         it.
      8. **Append exactly ONE 'systemic_invalidated' event** carrying the full
         payload (reason_code, basis, affected/preserved/blocked item ids,
         the executor_report id) — this is what makes step 2's idempotent
         replay possible on a later retry.

    Returns ``{aborted, already_aborted, wave_run_id, status, reason_code,
    basis, affected_item_ids, preserved_item_ids, blocked_sprint_item_ids,
    executor_report_id, executable, event_count}``. ``executable`` is always
    ``False`` — an aborted-for-systemic-reasons run is, by definition, not a
    thing any executor should resume work against.
    """
    validated = validate_systemic_invalidation_evidence(evidence)
    reason_code = validated["reason_code"]
    basis = validated["basis"]
    explicit_affected = validated["affected_item_ids"]

    run = await get_wave_run(db, wave_run_id)
    if run is None:
        raise ValueError(f"Wave run {wave_run_id!r} not found.")

    if run["status"] == "aborted":
        events = await get_wave_run_events(db, wave_run_id, include_superseded=False)
        prior_systemic = next(
            (e for e in reversed(events) if e.get("event_type") == "systemic_invalidated"),
            None,
        )
        prior_payload = (prior_systemic or {}).get("payload") or {}
        if (
            prior_systemic is not None
            and prior_payload.get("reason_code") == reason_code
            and prior_payload.get("basis") == basis
        ):
            all_events = await get_wave_run_events(db, wave_run_id)
            return {
                "aborted": True,
                "already_aborted": True,
                "wave_run_id": wave_run_id,
                "status": "aborted",
                "reason_code": reason_code,
                "basis": basis,
                "affected_item_ids": prior_payload.get("affected_item_ids", []),
                "preserved_item_ids": prior_payload.get("preserved_item_ids", []),
                "blocked_sprint_item_ids": prior_payload.get("blocked_sprint_item_ids", []),
                "executor_report_id": prior_payload.get("executor_report_id"),
                "executable": False,
                "event_count": len(all_events),
            }
        raise ValueError(
            f"Wave run {wave_run_id!r} is already terminal (status='aborted') "
            "for a different reason than the one given here "
            f"(prior systemic-invalidation event: {prior_systemic!r}). "
            "Aborted is terminal — a terminal run's history is never "
            "silently reinterpreted. Start a new wave run for the corrected "
            "work instead."
        )
    if run["status"] == "merged":
        raise ValueError(
            f"Wave run {wave_run_id!r} was already merged and cannot be "
            "retroactively marked systemically invalid. Merged is terminal "
            "— never resurrect or reinterpret finalized work."
        )

    current = run["status"]
    allowed = WAVE_RUN_TRANSITIONS.get(current, frozenset())
    if "aborted" not in allowed:
        raise ValueError(
            f"Cannot abort wave run {wave_run_id!r} from status {current!r}. "
            f"Allowed from {current!r}: {sorted(allowed) or '(none — terminal)'}."
        )

    children = await get_wave_run_children(db, wave_run_id)
    preserved_item_ids = sorted({
        c["sprint_item_id"] for c in children if c.get("status") == "succeeded"
    })
    run_item_ids = set(run.get("item_ids") or [])
    child_item_ids = {c["sprint_item_id"] for c in children}
    candidate_ids = (
        (run_item_ids | child_item_ids | set(explicit_affected))
        - set(preserved_item_ids)
    )
    affected_item_ids = sorted(candidate_ids)

    await db.execute(
        "UPDATE wave_runs SET status = 'aborted', updated_at = datetime('now') "
        "WHERE id = ?",
        (wave_run_id,),
    )
    await db.commit()

    # Lazy, direct submodule imports (not the `meridian.db` package aggregator)
    # — mirrors create_wave_run's own lazy-import precedent above, and avoids
    # any dependency on the aggregator's re-export list.
    from meridian.db.sprint_items import (  # noqa: PLC0415
        block_sprint_items_for_systemic_invalidation,
    )
    block_result = await block_sprint_items_for_systemic_invalidation(
        db,
        run["project_id"],
        affected_item_ids,
        wave_run_id=wave_run_id,
        reason_code=reason_code,
        basis=basis,
        actor=actor,
    )
    blocked_sprint_item_ids = block_result["blocked_item_ids"]

    from meridian.db.executor_reports import create_executor_report  # noqa: PLC0415
    report = await create_executor_report(
        db,
        run["project_id"],
        version=run.get("version"),
        session_id=actor,
        board_revision_hash=run.get("revision_hash"),
        blockers=[{
            "wave_run_id": wave_run_id,
            "reason_code": reason_code,
            "reason": basis,
            "classification": "systemic_invalidated_run",
            "affected_item_ids": affected_item_ids,
        }],
        recommended_next_actions=[
            "Review the evidence and the blocked sprint items it names.",
            "Create a corrected board revision addressing the invalidated "
            "premise before any blocked item is unblocked.",
            "Start a NEW wave run against the corrected revision — do not "
            "resume or mix this aborted run with the corrected work.",
        ],
        idempotency_key=f"systemic-invalidation:{wave_run_id}:{reason_code}",
    )

    payload = {
        "reason_code": reason_code,
        "basis": basis,
        "affected_item_ids": affected_item_ids,
        "preserved_item_ids": preserved_item_ids,
        "blocked_sprint_item_ids": blocked_sprint_item_ids,
        "executor_report_id": report.get("id"),
    }
    await append_wave_run_event(
        db,
        wave_run_id,
        "systemic_invalidated",
        from_status=current,
        to_status="aborted",
        detail=f"systemic invalidation ({reason_code}): {basis}",
        payload=payload,
        actor=actor,
    )

    events_after = await get_wave_run_events(db, wave_run_id)
    return {
        "aborted": True,
        "already_aborted": False,
        "wave_run_id": wave_run_id,
        "status": "aborted",
        "reason_code": reason_code,
        "basis": basis,
        "affected_item_ids": affected_item_ids,
        "preserved_item_ids": preserved_item_ids,
        "blocked_sprint_item_ids": blocked_sprint_item_ids,
        "executor_report_id": report.get("id"),
        "executable": False,
        "event_count": len(events_after),
    }


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
    child = _hydrate_child(row)
    assert child is not None
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
    return [c for c in (_hydrate_child(r) for r in rows) if c is not None]


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


def _validate_promotion_evidence(
    pinned_targets: list[dict[str, Any]], promotion_evidence: Any,
) -> None:
    """24f5146d — the docx-promotion half of the finalizer evidence contract.

    Deliberately symmetric to :func:`_validate_finalizer_evidence`: when this
    run pinned promotion targets at creation
    (``create_wave_run(promotion_targets=...)``), finalization requires REAL,
    individually successful ``apply_patch_manifest`` evidence for EVERY one
    of them — never a self-reported boolean, never inferred from "the wave's
    tests passed." A run that pinned NO promotion targets requires no
    ``promotion_evidence`` at all (zero behavior change for every caller that
    predates this feature — this is the "backward compatible, opt-in" rule
    the whole capability-manifest family already follows).

    ``promotion_evidence`` must be a ``dict`` keyed by ``target_docx_path``,
    each value the real
    ``tools.meridian_fallbacks.transactional_merge.MergeResult.to_dict()``
    from a non-dry-run ``apply_patch_manifest`` call: ``success is True``,
    ``dry_run`` falsy, and a non-empty ``final_sha256``. Raises ``ValueError``
    (fail closed, same convention as ``_validate_finalizer_evidence``) with an
    actionable message naming every offending target — never a generic
    "invalid".
    """
    if not pinned_targets:
        return
    if not isinstance(promotion_evidence, dict):
        raise ValueError(
            "Finalization rejected: this wave run pinned "
            f"{len(pinned_targets)} docx promotion target(s) at creation "
            "(create_wave_run(promotion_targets=...)), so finalize_wave_run "
            "requires promotion_evidence — a dict keyed by target_docx_path, "
            "each value the real "
            "tools.meridian_fallbacks.transactional_merge.MergeResult.to_dict() "
            "from a non-dry-run apply_patch_manifest call. A boolean, a "
            "self-report, or missing evidence is rejected."
        )

    missing: list[str] = []
    failed: list[tuple[str, str]] = []
    for pin in pinned_targets:
        target = pin.get("target_docx_path")
        entry = promotion_evidence.get(target)
        if not isinstance(entry, dict):
            missing.append(str(target))
            continue
        if entry.get("dry_run"):
            failed.append((str(target), "evidence is a dry_run result, not a committed apply"))
            continue
        if not entry.get("success"):
            failed.append((str(target), str(entry.get("error") or "success=False")))
            continue
        if not entry.get("final_sha256"):
            failed.append((str(target), "evidence is missing final_sha256"))

    if missing:
        raise ValueError(
            "Finalization rejected: promotion_evidence is missing an entry "
            f"for {len(missing)} pinned promotion target(s): {missing}. "
            "Apply the patch manifest for each pinned target "
            "(transactional_merge.apply_patch_manifest) and pass its real "
            "MergeResult before finalizing."
        )
    if failed:
        details = "; ".join(f"{t}: {reason}" for t, reason in failed)
        raise ValueError(
            f"Finalization rejected: promotion evidence indicates "
            f"{len(failed)} unsuccessful docx promotion(s) — {details}. Fix "
            "and re-apply before finalizing; a wave whose docx promotion "
            "failed (or was only dry-run) may not be merged."
        )


async def finalize_wave_run(
    db: aiosqlite.Connection,
    wave_run_id: str,
    *,
    evidence: Any = None,
    actor: str | None = None,
    expected_revision_hash: str | None = None,
    promotion_evidence: Any = None,
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
      5. **Docx promotion evidence → validate** (24f5146d, see
         :func:`_validate_promotion_evidence`) — ONLY when this run pinned
         promotion targets at creation (``create_wave_run(promotion_targets=...)``).
         A run with no pinned targets requires no ``promotion_evidence`` at
         all; zero behavior change from before this parameter existed.
      6. **Evidence → validate** (see :func:`_validate_finalizer_evidence`).
      7. Transition to ``merged`` and append exactly ONE ``finalized`` event,
         whose payload carries ``promotion_evidence`` alongside ``evidence``
         so the wave's provenance chain (plan -> pinned base hash -> applied
         merge result -> finalized) is durably closed in one place.

    Returns ``{finalized, already_finalized, wave_run_id, status, finalized_at,
    finalizer_evidence, promotion_evidence, pinned_promotion_targets,
    children_summary, event_count}``.
    """
    run = await get_wave_run(db, wave_run_id)
    if run is None:
        raise ValueError(f"Wave run {wave_run_id!r} not found.")

    children = await get_wave_run_children(db, wave_run_id)

    # 1. Idempotent replay — no new row, no new event.
    if run["status"] == "merged":
        events = await get_wave_run_events(db, wave_run_id)
        replayed_promotion_evidence = None
        for ev in reversed(events):
            if ev.get("event_type") == "finalized":
                replayed_promotion_evidence = (ev.get("payload") or {}).get(
                    "promotion_evidence"
                )
                break
        return {
            "finalized": True,
            "already_finalized": True,
            "wave_run_id": wave_run_id,
            "status": "merged",
            "finalized_at": run.get("finalized_at"),
            "finalizer_evidence": run.get("finalizer_evidence"),
            "promotion_evidence": replayed_promotion_evidence,
            "pinned_promotion_targets": await get_pinned_promotion_targets(db, wave_run_id),
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

    # 5. Docx promotion evidence contract (24f5146d) — opt-in: only enforced
    # when this run actually pinned promotion targets at creation.
    pinned_promotion_targets = await get_pinned_promotion_targets(db, wave_run_id)
    _validate_promotion_evidence(pinned_promotion_targets, promotion_evidence)

    # 6. Evidence contract (shared with complete_wave_gate, d2430713).
    _validate_finalizer_evidence(evidence)

    # 7. Transition + single finalized event.
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
        payload={
            "evidence": evidence,
            "promotion_evidence": promotion_evidence,
            "pinned_promotion_targets": pinned_promotion_targets,
        },
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
        "promotion_evidence": promotion_evidence,
        "pinned_promotion_targets": pinned_promotion_targets,
        "children_summary": _children_summary(children),
        "event_count": len(events),
    }


# ---------------------------------------------------------------------------
# 7d71d6bc — RESCUE-R2: child leases, dispatch provenance, no-op resume
# protection.
#
# Why this exists (rescue-sweep gap): 2a654cb0 gave a wave run's CHILDREN
# (one row per sprint item) a status/failure_mode/evidence — enough to block
# finalization on a stop-mode failure, but nothing that answers "which agent
# is doing this work, when did it start, is it still alive, and did its
# subprocess actually succeed." A wave run that crashes mid-flight (the
# orchestrating session dies, a worktree agent is killed) had no durable way
# to tell an EXISTING LIVE child (another agent is still working it — do not
# steal it) apart from a STALE ORPHAN (the claiming agent is gone — safe to
# re-dispatch), a COMPLETED child (already done — re-running it would
# silently duplicate work), or an EMPTY/INVALID dispatch (registered by
# start_wave_run but never actually claimed by anyone).
#
# The four functions below close that gap:
#   * claim_wave_run_child   — claim-before-work timestamp + agent identity
#                              + retry provenance (first_claim/reclaim/retry).
#   * heartbeat_wave_run_child — proves a claimed child's agent is still
#                              alive without re-claiming it.
#   * record_wave_run_child_outcome — terminal outcome INCLUDING the real
#                              subprocess exit code (never just a status
#                              string), a thin guarded wrapper over
#                              record_wave_run_child.
#   * get_wave_run_recovery_plan — the read-only classifier a crash-
#                              recovering orchestrator calls INSTEAD of
#                              blindly re-dispatching every item_ids entry.
#                              Never mutates state and never touches an OS
#                              process — it only ever answers "safe to
#                              re-dispatch: yes/no", never "go kill X".
#
# meridian.db.sprint_items.claim_sprint_item / complete_sprint_item call
# claim_wave_run_child / record_wave_run_child_outcome via a lazy,
# best-effort hook (never lets wave-run bookkeeping block or fail a claim or
# completion) whenever the item being claimed/completed is a live child of
# an ACTIVE (non-terminal) wave run — see find_active_wave_run_child_for_item
# below. A project that never calls start_wave_run sees zero behavior
# change: find_active_wave_run_child_for_item returns None immediately.
# ---------------------------------------------------------------------------

#: Default lease TTL for a wave-run child with no per-child override —
#: deliberately a different order of magnitude than
#: process_registry.DEFAULT_TTL_SECONDS (90s, tuned for a subprocess poll
#: loop): a wave-run child represents a whole sprint item's worth of agent
#: work, not a subprocess heartbeat cadence, so the default TTL is minutes.
WAVE_RUN_CHILD_DEFAULT_LEASE_TTL_SECONDS = 1800  # 30 minutes

#: The four resume classifications :func:`classify_wave_run_child_lease`
#: assigns to every child — see that function's docstring for the full
#: decision tree, and :func:`get_wave_run_recovery_plan` for how a
#: recovering orchestrator is meant to act on each one.
WAVE_RUN_CHILD_LEASE_LIVE = "live"
WAVE_RUN_CHILD_LEASE_STALE_ORPHAN = "stale_orphan"
WAVE_RUN_CHILD_LEASE_COMPLETED = "completed"
WAVE_RUN_CHILD_LEASE_EMPTY_INVALID = "empty_invalid"

WAVE_RUN_CHILD_LEASE_STATES: frozenset[str] = frozenset({
    WAVE_RUN_CHILD_LEASE_LIVE,
    WAVE_RUN_CHILD_LEASE_STALE_ORPHAN,
    WAVE_RUN_CHILD_LEASE_COMPLETED,
    WAVE_RUN_CHILD_LEASE_EMPTY_INVALID,
})

#: Terminal wave-run-child outcomes — mirrors WAVE_RUN_CHILD_STATUSES minus
#: 'running'. record_wave_run_child_outcome requires one of these; the
#: in-flight state is claim_wave_run_child/heartbeat_wave_run_child's job.
_WAVE_RUN_CHILD_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "succeeded", "failed", "skipped",
})


class ForeignWaveRunChildLeaseError(RuntimeError):
    """Raised when :func:`claim_wave_run_child` / :func:`heartbeat_wave_run_child`
    is called with an ``agent_id`` that does not match the CURRENT, LIVE
    lease holder for that child.

    This is the "test-run lock contention" guardrail named in the sprint
    item: two agents may not both believe they own the same wave-run child
    at once. The child-lease analogue of
    :class:`meridian.process_registry.ForeignLeaseError` — same philosophy
    (never auto-resolved, never silently taken over), different layer (a DB
    row, not an OS process). A genuinely dead peer is reaped by re-deriving
    from :func:`get_wave_run_recovery_plan`'s ``stale_orphan`` classification
    and re-claiming (which itself takes the "retry" branch once the lease
    is confirmed non-live), never by catching and ignoring this exception.
    """

    def __init__(
        self, wave_run_id: str, sprint_item_id: str, holder: "str | None", requester: str,
    ):
        self.wave_run_id = wave_run_id
        self.sprint_item_id = sprint_item_id
        self.holder = holder
        self.requester = requester
        super().__init__(
            f"Wave run {wave_run_id!r} child {sprint_item_id!r} is already "
            f"leased to agent_id {holder!r} (still live) — refusing to hand "
            f"it to {requester!r}. If {holder!r} genuinely crashed, call "
            "get_wave_run_recovery_plan to confirm a 'stale_orphan' "
            "classification before re-claiming."
        )


def classify_wave_run_child_lease(
    child: dict[str, Any],
    *,
    now: "Any | None" = None,
    default_ttl_seconds: int | None = None,
) -> str:
    """Classify ONE ``wave_run_children`` row's resume-safety state.

    The core "no-op resume protection" primitive (7d71d6bc). Pure and
    synchronous — no DB access, never raises; an unparseable/missing
    timestamp degrades toward the SAFER-for-review classification
    (``stale_orphan``, eligible for recovery review) rather than silently
    trusting a child as live forever.

    Returns one of:

    * :data:`WAVE_RUN_CHILD_LEASE_COMPLETED` — ``status`` is a terminal
      outcome (succeeded/failed/skipped), regardless of how old or fresh the
      row is. A recovering orchestrator must NEVER re-dispatch this child —
      that is exactly the "silently re-run completed work" failure this
      item exists to prevent.
    * :data:`WAVE_RUN_CHILD_LEASE_EMPTY_INVALID` — ``status='running'`` but
      the child was never actually dispatched: ``claimed_at`` is NULL (e.g.
      ``start_wave_run`` pre-registered it via ``record_wave_run_child`` and
      nothing has called :func:`claim_wave_run_child` yet), or ``agent_id``
      is missing/blank despite a ``claimed_at`` (a malformed/partial
      dispatch record). Safe to dispatch fresh — there is no live or
      completed work to protect.
    * :data:`WAVE_RUN_CHILD_LEASE_LIVE` — ``status='running'``, properly
      dispatched (``claimed_at`` AND ``agent_id`` both present), and the
      heartbeat signal (``last_heartbeat_at``, falling back to
      ``claimed_at`` when no explicit heartbeat was ever recorded) is
      within the lease TTL. An orchestrator must NOT re-dispatch this child
      — an agent may still be actively working it.
    * :data:`WAVE_RUN_CHILD_LEASE_STALE_ORPHAN` — properly dispatched, but
      the heartbeat signal has lapsed past the TTL (or is unparseable). The
      ONE state :func:`get_wave_run_recovery_plan` marks safe to
      re-dispatch — the claiming agent is presumed crashed.

    ``default_ttl_seconds`` is used only when the child itself carries no
    ``lease_ttl_seconds`` (the per-child value set at claim time always
    wins); both fall back to :data:`WAVE_RUN_CHILD_DEFAULT_LEASE_TTL_SECONDS`.
    """
    status = child.get("status")
    if status in WAVE_RUN_CHILD_STATUSES and status != "running":
        return WAVE_RUN_CHILD_LEASE_COMPLETED

    claimed_at = child.get("claimed_at")
    agent_id = str(child.get("agent_id") or "").strip()
    if not claimed_at or not agent_id:
        return WAVE_RUN_CHILD_LEASE_EMPTY_INVALID

    from datetime import datetime as _dt_cls

    now_dt = now or _dt_cls.utcnow()
    ttl = child.get("lease_ttl_seconds") or default_ttl_seconds \
        or WAVE_RUN_CHILD_DEFAULT_LEASE_TTL_SECONDS
    try:
        ttl = int(ttl)
    except (TypeError, ValueError):
        ttl = WAVE_RUN_CHILD_DEFAULT_LEASE_TTL_SECONDS

    heartbeat_dt = _parse_ts(child.get("last_heartbeat_at") or claimed_at)
    if heartbeat_dt is None:
        # Dispatched, but the heartbeat/claimed_at signal itself is garbage —
        # fail toward the reviewable classification, not toward "trust it".
        return WAVE_RUN_CHILD_LEASE_STALE_ORPHAN

    age_seconds = (now_dt - heartbeat_dt).total_seconds()
    if age_seconds > ttl:
        return WAVE_RUN_CHILD_LEASE_STALE_ORPHAN
    return WAVE_RUN_CHILD_LEASE_LIVE


async def find_active_wave_run_child_for_item(
    db: aiosqlite.Connection, project_id: str, sprint_item_id: str,
) -> dict[str, Any] | None:
    """Look up the most recent child row for ``sprint_item_id`` belonging to
    a NON-TERMINAL (not merged/aborted) wave run in ``project_id``.

    This is the lookup :func:`meridian.db.sprint_items.claim_sprint_item` /
    :func:`meridian.db.sprint_items.complete_sprint_item` use, best-effort
    and fail-open, to decide whether to touch child-lease bookkeeping at
    all. Returns ``None`` when the item isn't a child of any active wave run
    — the overwhelming majority of claims/completions in a project that
    never calls ``start_wave_run`` — so those callers see zero behavior
    change.
    """
    statuses = sorted(WAVE_RUN_TERMINAL_STATUSES)
    placeholders = ", ".join("?" for _ in statuses)
    async with db.execute(
        "SELECT wrc.* FROM wave_run_children wrc "
        "JOIN wave_runs wr ON wr.id = wrc.wave_run_id "
        f"WHERE wrc.sprint_item_id = ? AND wr.project_id = ? "
        f"AND wr.status NOT IN ({placeholders}) "
        "ORDER BY wrc.created_at DESC LIMIT 1",
        (sprint_item_id, project_id, *statuses),
    ) as cur:
        row = await cur.fetchone()
    return _hydrate_child(row)


async def claim_wave_run_child(
    db: aiosqlite.Connection,
    wave_run_id: str,
    sprint_item_id: str,
    *,
    agent_id: str,
    actor: str | None = None,
    lease_ttl_seconds: int | None = None,
    dispatch_provenance: Any = None,
    now: "Any | None" = None,
) -> dict[str, Any]:
    """Record that ``agent_id`` began (or resumed) work on this wave-run
    child NOW — the claim-before-work timestamp + agent/session identity
    half of the rescue-sweep gap (7d71d6bc).

    Three distinct outcomes, each returned with a ``claim_kind`` marker:

    * ``"first_claim"`` — the child was never actually dispatched: either
      the row didn't exist at all, or it was pre-registered with no
      dispatch info (``start_wave_run`` / ``record_wave_run_child`` — the
      :data:`WAVE_RUN_CHILD_LEASE_EMPTY_INVALID` classification). Sets
      ``claimed_at``/``last_heartbeat_at`` to now and records ``agent_id``;
      ``attempt`` is left at whatever it already was (normally 1) — nothing
      was ever live or completed, so there is no prior attempt to count.
    * ``"reclaim"`` — the SAME ``agent_id`` already holds this child's LIVE
      lease (see :func:`classify_wave_run_child_lease`). Idempotent —
      refreshes ``last_heartbeat_at`` only; ``attempt`` is NOT bumped. Lets
      a caller re-assert its own claim without corrupting retry provenance.
    * ``"retry"`` — the child's last recorded outcome was TERMINAL, or its
      lease had gone STALE under a different (or the same) ``agent_id``.
      ``attempt`` is incremented, ``exit_code`` is cleared, status resets to
      ``'running'``, and a ``child_retried`` event is appended to the wave
      run's append-only history recording the PRIOR agent_id/status/
      exit_code — the prior attempt's values are never silently discarded,
      only superseded going forward (mirrors this module's append-only
      correction philosophy elsewhere).

    Raises :class:`ForeignWaveRunChildLeaseError` when a DIFFERENT
    ``agent_id`` holds this child's CURRENTLY-LIVE lease — the "test-run
    lock contention" guardrail: two agents may not both believe they own the
    same child at once. Raises ``ValueError`` if the wave run itself does
    not exist, or if ``agent_id`` is blank.
    """
    from datetime import datetime as _dt_cls

    run = await get_wave_run(db, wave_run_id)
    if run is None:
        raise ValueError(f"Wave run {wave_run_id!r} not found.")
    agent_id = str(agent_id or "").strip()
    if not agent_id:
        raise ValueError("agent_id is required to claim a wave-run child.")

    now_dt = now or _dt_cls.utcnow()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    ttl = int(lease_ttl_seconds) if lease_ttl_seconds else WAVE_RUN_CHILD_DEFAULT_LEASE_TTL_SECONDS

    async with db.execute(
        "SELECT * FROM wave_run_children WHERE wave_run_id = ? AND sprint_item_id = ?",
        (wave_run_id, sprint_item_id),
    ) as cur:
        row = await cur.fetchone()
    existing = _hydrate_child(row)

    if existing is None:
        await db.execute(
            "INSERT INTO wave_run_children "
            "(id, wave_run_id, sprint_item_id, failure_mode, status, actor, "
            " agent_id, claimed_at, last_heartbeat_at, lease_ttl_seconds, "
            " attempt, dispatch_provenance) "
            "VALUES (?, ?, ?, 'continue', 'running', ?, ?, ?, ?, ?, 1, ?)",
            (
                _new_id(), wave_run_id, sprint_item_id, actor, agent_id,
                now_str, now_str, ttl, _json_or_none(dispatch_provenance),
            ),
        )
        await db.commit()
        await append_wave_run_event(
            db, wave_run_id, "child_claimed",
            detail=f"{sprint_item_id} claimed by {agent_id} (first_claim)",
            payload={
                "sprint_item_id": sprint_item_id, "agent_id": agent_id,
                "claim_kind": "first_claim", "attempt": 1,
            },
            actor=actor or agent_id,
        )
        claim_kind = "first_claim"
    else:
        lease_state = classify_wave_run_child_lease(existing, now=now_dt, default_ttl_seconds=ttl)
        existing_agent = existing.get("agent_id")
        existing_status = existing.get("status")

        if lease_state == WAVE_RUN_CHILD_LEASE_LIVE and existing_agent and existing_agent != agent_id:
            raise ForeignWaveRunChildLeaseError(
                wave_run_id, sprint_item_id, existing_agent, agent_id,
            )

        if lease_state == WAVE_RUN_CHILD_LEASE_LIVE and existing_agent == agent_id:
            # Same agent re-asserting its own still-live claim — treat as a
            # heartbeat refresh, not a retry. No attempt bump, no event spam.
            await db.execute(
                "UPDATE wave_run_children SET last_heartbeat_at = ?, "
                "lease_ttl_seconds = COALESCE(?, lease_ttl_seconds), "
                "dispatch_provenance = COALESCE(?, dispatch_provenance), "
                "updated_at = datetime('now') "
                "WHERE wave_run_id = ? AND sprint_item_id = ?",
                (
                    now_str, ttl, _json_or_none(dispatch_provenance),
                    wave_run_id, sprint_item_id,
                ),
            )
            await db.commit()
            claim_kind = "reclaim"
        elif lease_state == WAVE_RUN_CHILD_LEASE_EMPTY_INVALID:
            # The row was pre-registered (e.g. start_wave_run's own
            # item_ids loop via record_wave_run_child) but never actually
            # dispatched — no claimed_at, no agent_id. This IS a first
            # claim in every sense that matters (nothing was live, nothing
            # completed, there is no prior attempt to preserve provenance
            # for) even though the row already existed. attempt is left
            # untouched — bumping it here would fabricate retry history
            # for work that never actually started.
            await db.execute(
                "UPDATE wave_run_children SET status = 'running', agent_id = ?, "
                "claimed_at = ?, last_heartbeat_at = ?, lease_ttl_seconds = ?, "
                "dispatch_provenance = ?, actor = COALESCE(?, actor), "
                "updated_at = datetime('now') "
                "WHERE wave_run_id = ? AND sprint_item_id = ?",
                (
                    agent_id, now_str, now_str, ttl,
                    _json_or_none(dispatch_provenance), actor,
                    wave_run_id, sprint_item_id,
                ),
            )
            await db.commit()
            await append_wave_run_event(
                db, wave_run_id, "child_claimed",
                detail=f"{sprint_item_id} claimed by {agent_id} (first_claim)",
                payload={
                    "sprint_item_id": sprint_item_id, "agent_id": agent_id,
                    "claim_kind": "first_claim",
                    "attempt": int(existing.get("attempt") or 1),
                },
                actor=actor or agent_id,
            )
            claim_kind = "first_claim"
        else:
            # Terminal outcome, or a genuinely stale (non-live) lease under
            # any agent — both are legitimately re-claimable. Bump retry
            # provenance and preserve the PRIOR attempt's identity/outcome
            # in the append-only history.
            new_attempt = int(existing.get("attempt") or 1) + 1
            await db.execute(
                "UPDATE wave_run_children SET status = 'running', agent_id = ?, "
                "claimed_at = ?, last_heartbeat_at = ?, lease_ttl_seconds = ?, "
                "exit_code = NULL, attempt = ?, dispatch_provenance = ?, "
                "actor = COALESCE(?, actor), updated_at = datetime('now') "
                "WHERE wave_run_id = ? AND sprint_item_id = ?",
                (
                    agent_id, now_str, now_str, ttl, new_attempt,
                    _json_or_none(dispatch_provenance), actor,
                    wave_run_id, sprint_item_id,
                ),
            )
            await db.commit()
            await append_wave_run_event(
                db, wave_run_id, "child_retried",
                detail=(
                    f"{sprint_item_id} retried by {agent_id} (attempt "
                    f"{new_attempt}); prior: agent={existing_agent!r} "
                    f"status={existing_status!r} "
                    f"exit_code={existing.get('exit_code')!r} "
                    f"lease_state={lease_state!r}"
                ),
                payload={
                    "sprint_item_id": sprint_item_id, "agent_id": agent_id,
                    "claim_kind": "retry", "attempt": new_attempt,
                    "prior_agent_id": existing_agent,
                    "prior_status": existing_status,
                    "prior_exit_code": existing.get("exit_code"),
                    "lease_state_before_retry": lease_state,
                },
                actor=actor or agent_id,
            )
            claim_kind = "retry"

    async with db.execute(
        "SELECT * FROM wave_run_children WHERE wave_run_id = ? AND sprint_item_id = ?",
        (wave_run_id, sprint_item_id),
    ) as cur:
        row = await cur.fetchone()
    child = _hydrate_child(row)
    assert child is not None
    child["claim_kind"] = claim_kind
    return child


async def heartbeat_wave_run_child(
    db: aiosqlite.Connection,
    wave_run_id: str,
    sprint_item_id: str,
    *,
    agent_id: str,
    now: "Any | None" = None,
) -> dict[str, Any]:
    """Refresh a live child's ``last_heartbeat_at`` — proves ``agent_id`` is
    still working it, without going through the claim/retry decision tree.

    Raises ``ValueError`` if the child does not exist, or if its status is
    not ``'running'`` (a terminal child has nothing left to heartbeat — that
    would silently resurrect completed/failed work). Raises
    :class:`ForeignWaveRunChildLeaseError` if a DIFFERENT ``agent_id``
    currently holds the recorded lease — a dead agent's identity can never
    be hijacked by a heartbeat call; re-claim via :func:`claim_wave_run_child`
    instead (which itself decides retry-vs-refusal via the SAME lease
    classification this function's own guard reuses).
    """
    from datetime import datetime as _dt_cls

    async with db.execute(
        "SELECT * FROM wave_run_children WHERE wave_run_id = ? AND sprint_item_id = ?",
        (wave_run_id, sprint_item_id),
    ) as cur:
        row = await cur.fetchone()
    existing = _hydrate_child(row)
    if existing is None:
        raise ValueError(
            f"No child recorded for wave run {wave_run_id!r} / sprint item "
            f"{sprint_item_id!r} — claim it first via claim_wave_run_child."
        )
    if existing.get("status") != "running":
        raise ValueError(
            f"Cannot heartbeat wave-run child {sprint_item_id!r}: status is "
            f"{existing.get('status')!r}, not 'running' — a terminal child "
            "has nothing left to heartbeat."
        )
    existing_agent = existing.get("agent_id")
    agent_id = str(agent_id or "").strip()
    if existing_agent and existing_agent != agent_id:
        raise ForeignWaveRunChildLeaseError(
            wave_run_id, sprint_item_id, existing_agent, agent_id,
        )

    now_str = (now or _dt_cls.utcnow()).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE wave_run_children SET last_heartbeat_at = ?, "
        "updated_at = datetime('now') "
        "WHERE wave_run_id = ? AND sprint_item_id = ?",
        (now_str, wave_run_id, sprint_item_id),
    )
    await db.commit()

    async with db.execute(
        "SELECT * FROM wave_run_children WHERE wave_run_id = ? AND sprint_item_id = ?",
        (wave_run_id, sprint_item_id),
    ) as cur:
        row = await cur.fetchone()
    child = _hydrate_child(row)
    assert child is not None
    return child


async def record_wave_run_child_outcome(
    db: aiosqlite.Connection,
    wave_run_id: str,
    sprint_item_id: str,
    *,
    status: str,
    exit_code: int | None = None,
    evidence: Any = None,
    actor: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Record a wave-run child's TERMINAL outcome, preserving the real
    subprocess exit code when the dispatching agent ran the item's work as a
    subprocess — "preserve real subprocess exit codes" from the sprint-item
    spec, verbatim.

    A thin, guarded wrapper over :func:`record_wave_run_child`: ``status``
    must be one of :data:`_WAVE_RUN_CHILD_TERMINAL_STATUSES`
    (succeeded/failed/skipped) — raises ``ValueError`` for ``'running'``
    (use :func:`claim_wave_run_child` for the in-flight state instead; this
    function exists specifically for the "this child is DONE" half of the
    contract, so it is not a generic status setter). ``exit_code`` must be
    an ``int`` or ``None`` — never a string/bool, so "did the subprocess
    actually succeed" is never ambiguous between "no exit code captured"
    and "exit code was falsy".
    """
    if status not in _WAVE_RUN_CHILD_TERMINAL_STATUSES:
        raise ValueError(
            "record_wave_run_child_outcome requires a terminal status "
            f"({sorted(_WAVE_RUN_CHILD_TERMINAL_STATUSES)}), got {status!r}. "
            "Use claim_wave_run_child for the in-flight 'running' state."
        )
    if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
        raise ValueError(f"exit_code must be an int or None, got {exit_code!r}")

    run = await get_wave_run(db, wave_run_id)
    if run is None:
        raise ValueError(f"Wave run {wave_run_id!r} not found.")

    async with db.execute(
        "SELECT * FROM wave_run_children WHERE wave_run_id = ? AND sprint_item_id = ?",
        (wave_run_id, sprint_item_id),
    ) as cur:
        row = await cur.fetchone()
    existing = _row_to_dict(row)
    existing_failure_mode = (existing or {}).get("failure_mode") or "continue"

    await record_wave_run_child(
        db, wave_run_id, sprint_item_id,
        failure_mode=existing_failure_mode, status=status,
        evidence=evidence, actor=actor,
    )
    await db.execute(
        "UPDATE wave_run_children SET exit_code = ?, "
        "agent_id = COALESCE(?, agent_id), updated_at = datetime('now') "
        "WHERE wave_run_id = ? AND sprint_item_id = ?",
        (exit_code, agent_id, wave_run_id, sprint_item_id),
    )
    await db.commit()

    async with db.execute(
        "SELECT * FROM wave_run_children WHERE wave_run_id = ? AND sprint_item_id = ?",
        (wave_run_id, sprint_item_id),
    ) as cur:
        row = await cur.fetchone()
    result = _hydrate_child(row)
    assert result is not None
    return result


async def get_wave_run_recovery_plan(
    db: aiosqlite.Connection,
    wave_run_id: str,
    *,
    default_ttl_seconds: int | None = None,
    now: "Any | None" = None,
) -> dict[str, Any]:
    """The "no-op resume protection" entry point: classify EVERY child of
    ``wave_run_id`` via :func:`classify_wave_run_child_lease` and split them
    into actionable buckets for a crash-recovering orchestrator.

    Read-only — never mutates state, and never touches or kills any OS
    process. That guarantee is structural, not just a convention: this
    function has no access to process handles at all (it only reads DB
    rows), so "do not kill unrelated processes" holds by construction. If a
    caller decides a ``stale_orphan`` child's underlying OS process somehow
    survived its agent's crash, reconciling THAT is entirely the caller's
    responsibility via whatever process-management primitive fits its own
    context (e.g. :mod:`meridian.process_registry` for an externally
    registered lease) — this function only ever answers "safe to
    re-dispatch: yes/no", never "go kill X".

    Returns::

        {
          "wave_run_id": ...,
          "children": [ {...child fields..., "lease_state": <state>}, ... ],
          "live": [sprint_item_id, ...],
          "stale_orphan": [sprint_item_id, ...],
          "completed": [sprint_item_id, ...],
          "empty_invalid": [sprint_item_id, ...],
          "resumable_item_ids": [...],  # stale_orphan + empty_invalid —
                                         # safe to (re-)dispatch
          "protected_item_ids": [...],  # live + completed — must NOT be
                                         # (re-)dispatched
        }

    Raises ``ValueError`` if the wave run does not exist.
    """
    run = await get_wave_run(db, wave_run_id)
    if run is None:
        raise ValueError(f"Wave run {wave_run_id!r} not found.")

    children = await get_wave_run_children(db, wave_run_id)
    buckets: dict[str, list[Any]] = {state: [] for state in WAVE_RUN_CHILD_LEASE_STATES}
    annotated: list[dict[str, Any]] = []
    for child in children:
        state = classify_wave_run_child_lease(
            child, now=now, default_ttl_seconds=default_ttl_seconds,
        )
        buckets[state].append(child.get("sprint_item_id"))
        annotated_child = dict(child)
        annotated_child["lease_state"] = state
        annotated.append(annotated_child)

    return {
        "wave_run_id": wave_run_id,
        "children": annotated,
        "live": buckets[WAVE_RUN_CHILD_LEASE_LIVE],
        "stale_orphan": buckets[WAVE_RUN_CHILD_LEASE_STALE_ORPHAN],
        "completed": buckets[WAVE_RUN_CHILD_LEASE_COMPLETED],
        "empty_invalid": buckets[WAVE_RUN_CHILD_LEASE_EMPTY_INVALID],
        "resumable_item_ids": [
            *buckets[WAVE_RUN_CHILD_LEASE_STALE_ORPHAN],
            *buckets[WAVE_RUN_CHILD_LEASE_EMPTY_INVALID],
        ],
        "protected_item_ids": [
            *buckets[WAVE_RUN_CHILD_LEASE_LIVE],
            *buckets[WAVE_RUN_CHILD_LEASE_COMPLETED],
        ],
    }
