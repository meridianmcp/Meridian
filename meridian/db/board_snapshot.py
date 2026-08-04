"""Canonical expanded sprint-board snapshots, revisions, and resume diffs (ef665ef8).

Why this exists (incident writeup): a paused/resumed multi-agent wave had its
handoff view lag the live board — a session pasted an old sprint-item list,
another session had already changed statuses, and there was no way to tell
"is this handoff manifest current" from "is it stale" without ambiguity.

This module gives callers (``generate_handoff`` today; a future ``resume_wave``
tool per the sprint-item spec) three primitives:

  1. :func:`build_board_snapshot` — a canonical, byte-stable, expanded snapshot
     of the NON-DONE sprint board (every item whose ``status`` is not
     ``'done'`` — deliberately literal: failed/skipped/pushed items stay
     visible in the snapshot too, since a resumed session needs to see how a
     dependency chain actually resolved, not just what's still runnable).
  2. :func:`record_board_snapshot_revision` — persists a monotonic revision
     counter (``board_snapshot_revisions`` table) that increments exactly
     when :func:`build_board_snapshot`'s ``revision_hash`` changes, so a
     caller can tell "newer" (higher counter) from merely "different" (a
     changed hash with no ordering guarantee, e.g. two racing snapshots built
     from divergent in-memory reads).
  3. :func:`diff_board_snapshots` — a pure-Python, DB-free diff between two
     previously-built snapshots: added items, removed items (items that left
     the non-done set — typically completed, but also deleted), and per-item
     field changes restricted to the same four tracked fields the revision
     hash is sensitive to (``status``, ``depends_on``, ``touches_resources``,
     ``pointers``). This is the "resume delta" a paused/resumed session (or a
     future ``resume_wave`` tool) needs to reconcile a stale manifest against
     the live board.

Design decisions (documented per the sprint-item spec):

* **Ordering key**: ``(version, added_at, id)``. ``version`` buckets items
  into their sprint logically; ``added_at`` gives temporal order within a
  version; ``id`` is the final deterministic tiebreaker — required because
  SQLite's ``datetime('now')`` is only second-granularity, so two items added
  within the same second have identical ``added_at`` and would otherwise sort
  in whatever order the DB happens to return them (see the identical
  reasoning in :func:`meridian.db.sprint_items.get_sprint_item_pointers`).
  All three components are cast to ``str`` before comparison so ``None``
  values (a missing ``added_at`` on a legacy row) sort deterministically
  instead of raising a ``TypeError`` on a ``None``-vs-``str`` comparison.

* **Byte-stability**: :func:`build_board_snapshot` returns ONLY data already
  stored in the DB (item columns + pointer rows) — no wall-clock field (no
  "generated_at") is ever included in the returned structure, so
  ``json.dumps(snapshot, sort_keys=True, ...)`` is byte-identical across two
  calls with no intervening board change, without needing a separate
  "comparable payload" carve-out. Callers that want a capture timestamp
  should record one alongside the snapshot at the call site, not inside it.

* **Revision hash**: ``sha256`` over the canonical JSON serialization
  (``sort_keys=True``, compact separators, ``ensure_ascii=True``) of a
  TRACKED SUBSET of each item — ``id`` (identity) plus exactly the four
  fields item 3 of the spec calls out: ``status``, ``depends_on``,
  ``touches_resources``, ``pointers``. Deliberately excludes ``title``,
  ``notes``, ``wave``, ``priority``, etc. — the hash (and the diff below)
  must change if and only if status/dependency/resource/pointer state
  changes, not on cosmetic edits. The full item dict (including the
  non-tracked descriptive fields) is still returned in ``items`` for human/
  display consumption; only the hash input is narrowed.

* **Revision counter**: persisted in a new ``board_snapshot_revisions`` table
  (one row per distinct hash seen for a ``(project_id, version_filter)``
  bucket) rather than derived, because a genuinely monotonic "newer vs
  different" ordering that survives process restarts and is comparable
  across concurrent sessions requires storage — an in-memory or
  hash-only design can tell "different" but never "newer". Recording is an
  explicit, separate, opt-in call (:func:`record_board_snapshot_revision`)
  so :func:`build_board_snapshot` itself stays a pure read with no DB writes
  and is safe to call as often as needed (e.g. from a read-only diff check)
  without growing the revisions table.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import aiosqlite

# Shared helpers + sprint-item accessors from the parent db package — available
# at import time because board_snapshot.py is imported at the bottom of
# db/__init__.py, AFTER sprint_items.py (which defines/binds these names).
from meridian.db import (  # noqa: PLC0415
    _new_id,
    _row_to_dict,
    get_sprint_items,
    get_sprint_item_pointers,
    parse_touches_resources,
)


# Fields whose change is (a) what the revision hash is sensitive to and
# (b) what diff_board_snapshots reports as a per-item "change". Kept as a
# single shared tuple so the hash and the diff can never drift apart.
_TRACKED_FIELDS: tuple[str, ...] = ("status", "depends_on", "touches_resources", "pointers")


def canonical_json(obj: Any) -> str:
    """Deterministic JSON serialization used for both hashing and byte-stability checks.

    Sorted keys, compact separators, ASCII-safe, and ``default=str`` so any
    stray non-JSON-native value (should never occur here, but defensively)
    degrades to a stable string rather than raising.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _tracked_item_payload(item: dict[str, Any]) -> dict[str, Any]:
    """Extract the identity + tracked-field subset of a snapshot item used for hashing/diffing."""
    payload: dict[str, Any] = {"id": item.get("id")}
    for field in _TRACKED_FIELDS:
        payload[field] = item.get(field)
    return payload


def _compute_revision_hash(items: list[dict[str, Any]]) -> str:
    """sha256 of the canonical JSON of every item's tracked-field subset.

    Item order in ``items`` is already deterministic (see the ordering-key
    docs above), so this changes if and only if an item was added/removed
    from the non-done set, or one of the four tracked fields changed value
    on a surviving item.
    """
    payload = [_tracked_item_payload(it) for it in items]
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


async def build_board_snapshot(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    version: str | None = None,
) -> dict[str, Any]:
    """Build a canonical, byte-stable, expanded snapshot of the non-done sprint board.

    "Non-done" is deliberately literal: every item whose ``status`` is not
    exactly ``'done'`` is included (pending/todo/in_progress/
    provisional_complete/indeterminate/failed/skipped/pushed) — a resumed
    session needs to see how a dependency chain actually resolved (a failed
    or skipped parent), not just the items still awaiting a claim.

    Returns a dict with:
      - ``project_id``, ``version_filter`` (the ``version`` argument, echoed
        back so a caller can tell which bucket this snapshot covers),
      - ``ordering`` — a string documenting the sort key used,
      - ``item_count``,
      - ``items`` — the expanded per-item list, sorted by
        ``(version, added_at, id)``; each item has: id, title, status,
        version, priority, added_at, depends_on, touches_resources (parsed
        list), pointers (list of {id, source_type, targets, label} sorted by
        pointer id — mirrors get_sprint_item_pointers' own byte-stable
        ordering), wave, blocker_kind, milestone_type, track,
        prospect_bypass,
      - ``revision_hash`` — see :func:`_compute_revision_hash`.

    No field in the returned structure is derived from "now" at call time, so
    two calls with no intervening board change produce byte-identical
    ``canonical_json(snapshot)`` output.
    """
    raw_items = await get_sprint_items(db, project_id, version=version)
    raw_items = [it for it in raw_items if (it.get("status") or "") != "done"]
    raw_items.sort(
        key=lambda it: (
            str(it.get("version") or ""),
            str(it.get("added_at") or ""),
            str(it.get("id") or ""),
        )
    )

    items: list[dict[str, Any]] = []
    for it in raw_items:
        pointer_rows = await get_sprint_item_pointers(db, it["id"])
        pointers = [
            {
                "id": p.get("id"),
                "source_type": p.get("source_type"),
                "targets": p.get("targets"),
                "label": p.get("label"),
            }
            for p in sorted(pointer_rows, key=lambda p: str(p.get("id") or ""))
        ]
        items.append({
            "id": it.get("id"),
            "title": it.get("title"),
            "status": it.get("status"),
            "version": it.get("version"),
            "priority": it.get("priority") or "normal",
            "added_at": it.get("added_at"),
            "depends_on": it.get("depends_on"),
            "touches_resources": parse_touches_resources(it.get("touches_resources")),
            "pointers": pointers,
            "wave": it.get("wave"),
            "blocker_kind": it.get("blocker_kind"),
            "milestone_type": it.get("milestone_type"),
            "track": it.get("track"),
            "prospect_bypass": bool(it.get("prospect_bypass") or 0),
        })

    return {
        "project_id": project_id,
        "version_filter": version,
        "ordering": "version,added_at,id",
        "item_count": len(items),
        "items": items,
        "revision_hash": _compute_revision_hash(items),
    }


def diff_board_snapshots(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Compute the resume delta between two previously-built board snapshots.

    Pure Python, no DB access — both arguments are dicts as returned by
    :func:`build_board_snapshot` (or reconstructed from stored JSON, e.g. a
    handoff record). Short-circuits on matching ``revision_hash`` (cheap,
    exact equality check) before falling back to a full item-level diff.

    Returns:
      - ``changed`` — False when the two snapshots' revision hashes match
        (nothing to report); True otherwise.
      - ``added`` — full item dicts present in ``current`` but not
        ``previous`` (by id) — e.g. a planner injected a new item mid-run.
      - ``removed`` — full item dicts present in ``previous`` but not
        ``current`` — typically an item that completed (left the non-done
        set) or was deleted/merged away.
      - ``changed_items`` — ``[{"id": ..., "changes": {field: {"old":
        ..., "new": ...}}}]`` for items present in both snapshots whose
        tracked fields (status, depends_on, touches_resources, pointers)
        differ. Non-tracked field edits (title, notes, wave, priority, ...)
        are intentionally NOT reported here — they don't affect the revision
        hash either, by design (see module docstring).
      - ``unchanged_count`` — items present in both snapshots with no
        tracked-field difference.
      - ``previous_revision_hash`` / ``current_revision_hash`` — echoed back
        for the caller's own bookkeeping.
    """
    if not isinstance(previous, dict) or not isinstance(current, dict):
        raise ValueError("diff_board_snapshots requires two snapshot dicts")

    prev_items = previous.get("items") or []
    curr_items = current.get("items") or []
    prev_hash = previous.get("revision_hash")
    curr_hash = current.get("revision_hash")

    if prev_hash is not None and prev_hash == curr_hash:
        return {
            "changed": False,
            "added": [],
            "removed": [],
            "changed_items": [],
            "unchanged_count": len(curr_items),
            "previous_revision_hash": prev_hash,
            "current_revision_hash": curr_hash,
        }

    prev_by_id = {it["id"]: it for it in prev_items if it.get("id")}
    curr_by_id = {it["id"]: it for it in curr_items if it.get("id")}
    added_ids = sorted(set(curr_by_id) - set(prev_by_id))
    removed_ids = sorted(set(prev_by_id) - set(curr_by_id))
    common_ids = sorted(set(curr_by_id) & set(prev_by_id))

    changed_items: list[dict[str, Any]] = []
    for iid in common_ids:
        old = prev_by_id[iid]
        new = curr_by_id[iid]
        field_changes: dict[str, Any] = {}
        for field in _TRACKED_FIELDS:
            if old.get(field) != new.get(field):
                field_changes[field] = {"old": old.get(field), "new": new.get(field)}
        if field_changes:
            changed_items.append({"id": iid, "changes": field_changes})

    return {
        "changed": True,
        "added": [curr_by_id[i] for i in added_ids],
        "removed": [prev_by_id[i] for i in removed_ids],
        "changed_items": changed_items,
        "unchanged_count": len(common_ids) - len(changed_items),
        "previous_revision_hash": prev_hash,
        "current_revision_hash": curr_hash,
    }


async def compute_scope_diff(
    db: aiosqlite.Connection,
    project_id: str,
    requested_item_ids: "list[str] | None",
    *,
    version: str | None = None,
) -> dict[str, Any]:
    """3af86d28 — requested-vs-emitted scope diff for a corrective handoff.

    Given the item ids a handoff's ORIGINAL /goal block requested (captured
    at generation time, or reconstructed from the pasted block), compares
    them against the LIVE non-done board for the same ``(project_id,
    version)`` bucket via :func:`build_board_snapshot` — the same canonical
    snapshot every other resume/staleness primitive in this module uses, so
    this can never disagree with e.g. :func:`meridian.db.wave_resume.check_wave_resume`
    about what's actually live.

    Returns:
      - ``requested_item_ids`` — the input, deduped and sorted.
      - ``emitted_item_ids`` — requested ids still present on the live
        non-done board (the handoff's scope is still valid for these).
      - ``dropped_item_ids`` — requested ids no longer on the live non-done
        board (completed, deleted, or otherwise resolved since the source
        handoff was rendered — the scope drifted for these).
      - ``live_revision_hash`` — the live snapshot's revision hash, echoed
        back for the caller's own bookkeeping.

    Pure read, no DB writes. This is a convenience for computing the
    ``requested_scope``/``emitted`` comparison a corrective handoff records
    (see ``meridian.handoff.record_handoff_correction``'s ``requested_scope``
    parameter) — callers may pass this dict straight through, or compute
    their own shape; nothing here is mandatory.
    """
    live = await build_board_snapshot(db, project_id, version=version)
    live_ids = {it["id"] for it in (live.get("items") or []) if it.get("id")}
    requested = {i for i in (requested_item_ids or []) if i}
    return {
        "requested_item_ids": sorted(requested),
        "emitted_item_ids": sorted(requested & live_ids),
        "dropped_item_ids": sorted(requested - live_ids),
        "live_revision_hash": live.get("revision_hash"),
    }


async def get_latest_board_snapshot_revision(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    version: str | None = None,
) -> dict[str, Any] | None:
    """Return the most recently recorded revision row for a project/version bucket, or None."""
    version_filter = version or ""
    async with db.execute(
        "SELECT * FROM board_snapshot_revisions WHERE project_id = ? AND version_filter = ? "
        "ORDER BY revision_counter DESC LIMIT 1",
        (project_id, version_filter),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def record_board_snapshot_revision(
    db: aiosqlite.Connection,
    project_id: str,
    snapshot: dict[str, Any],
    *,
    version: str | None = None,
) -> dict[str, Any]:
    """Persist ``snapshot``'s revision hash, incrementing the monotonic counter iff it changed.

    Idempotent no-op (returns the existing counter, ``is_new=False``) when
    the latest recorded hash for this ``(project_id, version)`` bucket
    already matches ``snapshot["revision_hash"]`` — repeated calls with no
    intervening board change never grow the table. ``version`` should match
    whatever ``version`` :func:`build_board_snapshot` was called with (the
    snapshot's own ``version_filter`` is used if the caller omits it).

    Returns ``{"revision_counter": int, "revision_hash": str, "is_new": bool}``.
    """
    effective_version = version if version is not None else snapshot.get("version_filter")
    version_filter = effective_version or ""
    revision_hash = snapshot["revision_hash"]

    latest = await get_latest_board_snapshot_revision(db, project_id, version=effective_version)
    if latest and latest.get("revision_hash") == revision_hash:
        return {
            "revision_counter": latest["revision_counter"],
            "revision_hash": revision_hash,
            "is_new": False,
        }

    new_counter = int(latest["revision_counter"]) + 1 if latest else 1
    row_id = _new_id()
    await db.execute(
        "INSERT INTO board_snapshot_revisions "
        "(id, project_id, version_filter, revision_hash, revision_counter, item_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (row_id, project_id, version_filter, revision_hash, new_counter, snapshot.get("item_count") or 0),
    )
    await db.commit()
    return {
        "revision_counter": new_counter,
        "revision_hash": revision_hash,
        "is_new": True,
    }
