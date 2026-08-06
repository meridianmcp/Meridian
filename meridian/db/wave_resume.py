"""FEAT (efaa918a): resume_wave staleness gating for a durable wave run.

Split from proposal e27d3453-438c-4849-9f63-78174128c007; depends on 2a654cb0
(:mod:`meridian.db.wave_runs`, merged) and ef665ef8
(:mod:`meridian.db.board_snapshot`, merged).

Why this exists (incident writeup, same family as 2a654cb0/ef665ef8): a wave
run's pinned board snapshot tells you WHAT the board looked like when the wave
was planned, and ``diff_board_snapshots`` tells you HOW the live board differs
today — but nothing wired those two primitives into a single "is it still
safe to resume this wave, and if not, exactly why" answer. Without it, a
resuming session either re-derives staleness by hand (easy to get wrong, per
the b763d2ba pending-only-query incident this whole family of fixes traces
back to) or — worse — just resumes on faith against a manifest that may no
longer describe the live board at all.

:func:`check_wave_resume` is that answer. It:

  1. Loads the wave run by id (:func:`meridian.db.wave_runs.get_wave_run`).
  2. Re-builds the LIVE board snapshot via
     :func:`meridian.db.board_snapshot.build_board_snapshot`, which — by
     construction — covers every NON-DONE status (pending, todo, in_progress,
     provisional_complete, indeterminate, failed, skipped, pushed), never just
     ``status='pending'``. This is deliberate and matches the b763d2ba
     incident writeup in AGENTS.md verbatim: a pending-only re-query would
     make an item a sibling session already claimed (now ``in_progress``)
     look like it vanished from the board, which is indistinguishable from a
     genuinely stale/spoofed manifest. Reusing ``build_board_snapshot``
     (rather than a bespoke re-query here) means this function inherits that
     correctness for free instead of re-litigating it.
  3. Diffs the pinned manifest against the live snapshot via
     :func:`meridian.db.board_snapshot.diff_board_snapshots` — reusing its
     ``added``/``removed``/``changed_items`` shape verbatim (the spec is
     explicit: don't invent a parallel diff format). This covers three of the
     four tracked fields the revision hash is sensitive to: ``status``,
     ``depends_on`` (dependency changes), ``touches_resources`` (resource
     changes), and ``pointers`` (evidence changes).
  4. ADDITIONALLY checks two fields ``board_snapshot`` deliberately excludes
     from its tracked-field hash (see that module's own docstring: "wave"
     and other descriptive fields are treated as cosmetic there) but that
     matter specifically for THIS wave run: whether an item's ``wave`` label
     changed (it may have been re-assigned to a different wave since this run
     was planned) and whether it was newly marked
     ``blocker_kind == 'superseded'`` (its whole premise was replaced — see
     f89d440f). Both are real staleness signals for a wave run even though
     they never move the board's revision hash, so they need their own
     comparison here rather than riding on ``diff_board_snapshots`` alone.
  5. Fails CLOSED (raises :class:`WaveResumeStale`, a ``ValueError``
     subclass carrying a machine-readable ``reasons`` list plus the raw
     ``resume_delta``) the moment ANY of the above finds a difference. This
     mirrors ``finalize_wave_run``'s own fail-closed posture: a genuinely
     unrelated board change elsewhere in the same (project_id, version)
     bucket still means "the board moved since this wave was planned" in the
     strict sense the manifest promises, so it is reported rather than
     silently ignored. Every reason names the specific item/field involved —
     never a generic "invalid" — per the spec's actionable-reason
     requirement.

Token/body-integrity verification (item 5 of the spec: binding a handoff
token to the canonical goal body) is DELIBERATELY NOT implemented in this
module. That lives in :mod:`meridian.handoff` (``mint_handoff_token`` /
``verify_handoff_token``) and is invoked from the ``resume_wave`` MCP handler
(:mod:`meridian.mcp.handlers.sprint_tools`) instead — importing
``meridian.handoff`` from a ``meridian.db.*`` module would be circular
(``handoff.py`` imports ``meridian.db`` at module scope), and the existing
handler-layer pattern (see ``handle_checkpoint``'s own local
``from .. import handoff as handoff_module_local``) already avoids exactly
that cycle.
"""
from __future__ import annotations

from typing import Any

import aiosqlite

from meridian.db.wave_runs import (
    get_wave_run,
    get_pinned_promotion_targets,
    WAVE_RUN_TERMINAL_STATUSES,
)
from meridian.db.board_snapshot import build_board_snapshot, diff_board_snapshots


class WaveResumeStale(ValueError):
    """Raised when a wave run's pinned manifest is stale relative to the live board.

    Subclasses ``ValueError`` so the existing ``except ValueError`` MCP-handler
    convention (see ``WaveRunFinalizationBlocked``'s own docstring) keeps
    working unchanged, while a caller that wants the specifics can catch this
    type and read :attr:`reasons` (a list of human-readable, actionable
    strings — one per stale item/field) and :attr:`resume_delta` (the raw
    ``diff_board_snapshots`` result).
    """

    def __init__(
        self, message: str, reasons: list[str], resume_delta: dict[str, Any],
    ):
        super().__init__(message)
        self.reasons = reasons
        self.resume_delta = resume_delta


def _describe_changed_field(item_id: str, field: str, delta: dict[str, Any]) -> str:
    """Render one tracked-field change from diff_board_snapshots as an actionable reason."""
    old, new = delta.get("old"), delta.get("new")
    if field == "pointers":
        old_n = len(old or [])
        new_n = len(new or [])
        if new_n < old_n:
            return (
                f"item {item_id} lost pointer evidence since this wave was "
                f"planned (had {old_n} pointer(s), now {new_n})"
            )
        return (
            f"item {item_id} pointer evidence changed since this wave was "
            f"planned (had {old_n} pointer(s), now {new_n})"
        )
    if field == "depends_on":
        return (
            f"item {item_id} dependency changed since this wave was planned "
            f"(was {old!r}, now {new!r})"
        )
    if field == "touches_resources":
        return (
            f"item {item_id} touched resources changed since this wave was "
            f"planned (was {old!r}, now {new!r})"
        )
    if field == "status":
        return (
            f"item {item_id} status changed since this wave was planned "
            f"(was {old!r}, now {new!r})"
        )
    return (
        f"item {item_id} field {field!r} changed since this wave was planned "
        f"(was {old!r}, now {new!r})"
    )


async def check_wave_resume(
    db: aiosqlite.Connection,
    wave_run_id: str,
) -> dict[str, Any]:
    """Check whether ``wave_run_id`` is safe to resume against the LIVE board.

    Returns ``{wave_run_id, resumable: True, status, resume_delta,
    pinned_revision_hash, live_revision_hash}`` when the manifest is current
    (no tracked-field diff, no wave-membership change, no newly-superseded
    item among the run's own item_ids).

    Raises :class:`WaveResumeStale` (a ``ValueError``) the moment ANY
    staleness is found, naming every specific reason. Raises a plain
    ``ValueError`` for a run that does not exist, is already terminal
    (``merged``/``aborted`` — see :data:`meridian.db.wave_runs.WAVE_RUN_TERMINAL_STATUSES`),
    or was created with no board snapshot pinned at all (staleness cannot be
    verified against nothing, so resume is refused rather than assumed safe).
    """
    run = await get_wave_run(db, wave_run_id)
    if run is None:
        raise ValueError(f"Wave run {wave_run_id!r} not found.")

    if run["status"] in WAVE_RUN_TERMINAL_STATUSES:
        raise ValueError(
            f"Wave run {wave_run_id!r} is already {run['status']!r} — "
            f"terminal runs cannot be resumed. Start a new wave run instead."
        )

    pinned = run.get("board_snapshot")
    if not isinstance(pinned, dict):
        raise ValueError(
            f"Wave run {wave_run_id!r} has no board snapshot pinned (it was "
            f"created without one via start_wave_run) — staleness cannot be "
            f"verified against nothing, so resume is refused. Re-plan the "
            f"wave with a snapshot pinned."
        )

    live = await build_board_snapshot(
        db, run["project_id"], version=run.get("version"),
    )
    diff = diff_board_snapshots(pinned, live)

    reasons: list[str] = []

    if diff["changed"]:
        for added in diff["added"]:
            reasons.append(
                f"item {added.get('id')} ({added.get('title', '')!r}) is newly "
                f"on the live board — not present in the pinned manifest; a "
                f"planner may have injected it mid-wave"
            )
        for removed in diff["removed"]:
            reasons.append(
                f"item {removed.get('id')} ({removed.get('title', '')!r}) has "
                f"left the live non-done board since this wave was planned "
                f"(completed, deleted, or otherwise resolved) — the manifest "
                f"is stale for this item"
            )
        for changed in diff["changed_items"]:
            iid = changed["id"]
            for field, delta in changed["changes"].items():
                reasons.append(_describe_changed_field(iid, field, delta))

    # Extra checks OUTSIDE the tracked-field hash (board_snapshot.py's own
    # docstring calls "wave" cosmetic there; f89d440f's blocker_kind is a
    # separate signal entirely) — but both matter for THIS wave run
    # specifically, so compare pinned vs live directly for its own item_ids
    # (falling back to every item in the pinned manifest when the run was
    # opened with no explicit item_ids).
    pinned_by_id = {it["id"]: it for it in (pinned.get("items") or []) if it.get("id")}
    live_by_id = {it["id"]: it for it in (live.get("items") or []) if it.get("id")}
    scope_ids = set(run.get("item_ids") or []) or set(pinned_by_id.keys())

    for iid in sorted(scope_ids):
        p_item = pinned_by_id.get(iid)
        l_item = live_by_id.get(iid)
        if p_item is None or l_item is None:
            continue  # already reported via added/removed above
        if p_item.get("wave") != l_item.get("wave"):
            reasons.append(
                f"item {iid} changed wave membership since this wave was "
                f"planned (was {p_item.get('wave')!r}, now {l_item.get('wave')!r})"
            )
        if (
            l_item.get("blocker_kind") == "superseded"
            and p_item.get("blocker_kind") != "superseded"
        ):
            reasons.append(
                f"item {iid} was marked blocker_kind='superseded' since this "
                f"wave was planned — its premise has been replaced by other "
                f"work; it cannot be resumed as-is (see the item's notes for "
                f"what superseded it)"
            )

    # 24f5146d — docx promotion-target staleness. A wave run that pinned
    # base-hash preconditions for one or more docx promotion targets (see
    # meridian.db.wave_runs.create_wave_run(promotion_targets=...)) is ALSO
    # stale — even though nothing about it moves board_snapshot's own
    # tracked-field revision hash, which covers sprint-item state, not
    # filesystem state — when the pinned target's on-disk content changed
    # since the wave was planned. Same fail-closed posture, same "name every
    # specific reason" discipline as every other check in this function.
    # Best-effort per target: a hashing failure never crashes resume-check
    # itself, it is folded into the mismatch (current_sha256=None) below.
    pinned_promotion_targets = await get_pinned_promotion_targets(db, wave_run_id)
    promotion_status: list[dict[str, Any]] = []
    if pinned_promotion_targets:
        from meridian import artifact_declaration as _artifact_declaration  # noqa: PLC0415

        for pin in pinned_promotion_targets:
            target = pin.get("target_docx_path")
            pinned_hash = pin.get("base_sha256")
            if not target:
                continue
            try:
                current_hash = _artifact_declaration.compute_base_sha256(target)
            except Exception:  # noqa: BLE001 — best-effort; never break resume-check
                current_hash = None
            unchanged = current_hash == pinned_hash
            promotion_status.append({
                "target_docx_path": target,
                "pinned_base_sha256": pinned_hash,
                "current_base_sha256": current_hash,
                "unchanged": unchanged,
            })
            if not unchanged:
                reasons.append(
                    f"docx promotion target {target} changed on disk since "
                    f"this wave was planned (base_sha256 was {pinned_hash!r}, "
                    f"now {current_hash!r}) — re-plan the promotion (a fresh "
                    "create_wave_run(promotion_targets=...) pin) or explicitly "
                    "accept the new base before resuming"
                )

    result = {
        "wave_run_id": wave_run_id,
        "resumable": not reasons,
        "status": run["status"],
        "resume_delta": diff,
        "pinned_revision_hash": pinned.get("revision_hash"),
        "live_revision_hash": live.get("revision_hash"),
        "promotion_target_status": promotion_status,
    }
    if reasons:
        joined = "; ".join(reasons)
        raise WaveResumeStale(
            f"Resume refused: wave run {wave_run_id!r} manifest is stale — {joined}",
            reasons,
            diff,
        )
    return result
