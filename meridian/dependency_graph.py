"""Canonical sprint-item dependency-graph cycle detection (05553946).

Systematic chaining gap found during the executor-contract audit: existing
work validates stale serialized ``depends_on`` ids at HANDOFF-RENDER time
(``meridian.db.board_snapshot.find_stale_reference_ids``, ee8a6af1 — missing /
merged-away edges are deliberately tolerated at write time and only fail
closed when a handoff is generated), and ``bb6c23fb`` resumes execution
against a pinned board snapshot. Neither of those addresses acyclicity: the
one existing chain walker, ``meridian.db.sprint_items._topo_depth_map``,
documents its own limitation plainly — "Cycles are broken by treating the
back-edge target as depth 0" — i.e. a chain walker that silently stops the
moment it revisits an id, with no signal to the caller that anything was
wrong. A two-item cycle (A depends_on B, then B retroactively set to depend
on A via ``patch_sprint_item``) was previously accepted silently and would
have made both items permanently unclaimable-by-dependency with no
diagnostic anywhere.

This module is intentionally a leaf: no ``aiosqlite``/DB import, so it has no
opinion on how a caller obtained ``items`` (a live ``get_sprint_items`` scan,
a hand-built list in a test, or a board snapshot's ``items``). Every function
here is pure and synchronous.

Two capabilities:

  * :func:`find_dependency_cycle` — walks the (single-parent) ``depends_on``
    chain from every item and returns the FULL cycle path the first time one
    is found (e.g. ``["b", "c", "a", "b"]``), never merely "gave up because
    we've seen this id before" with no detail. Supports ``proposed_edge`` so
    a caller can validate a NOT-YET-PERSISTED edit before writing it.
  * :func:`compute_dependency_graph_digest` — a deterministic sha256 digest
    over every item's ``(id, depends_on)`` edge, for staleness detection: two
    calls produce the same digest iff and only if the dependency edge set is
    unchanged (item added/removed or any ``depends_on`` value changed flips
    it). Deliberately narrower than
    ``meridian.db.board_snapshot._compute_revision_hash`` (which also tracks
    ``status``/``touches_resources``/``pointers``) — this digest exists
    specifically for "did the SHAPE of the dependency graph change", not "did
    the board change at all".

:class:`DependencyCycleError` (a ``ValueError`` subclass, like every other
sprint-item validation error in this codebase) is raised by
``meridian.db.sprint_items.patch_sprint_item`` when a caller's requested
``depends_on`` edit would close a cycle — a self-dependency (item_id ==
depends_on) is treated as a cycle of length one and reuses the same class /
same ``reason="cycle"`` for a single, consistent contract, rather than the
two independent code paths (an inline self-loop check plus no longer-cycle
check at all) that existed before this item.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


class DependencyCycleError(ValueError):
    """A prospective ``depends_on`` edit would create (or already sits inside) a cycle.

    ``reason`` is always ``"cycle"`` (kept as an attribute, not just baked into
    the message, so callers can branch on it without string-matching).
    ``cycle_path`` is the full closed path, e.g. ``["a", "b", "c", "a"]`` —
    reading left to right: a depends_on b, b depends_on c, c depends_on a
    again. A self-dependency renders as ``["a", "a"]``.
    """

    reason = "cycle"

    def __init__(self, cycle_path: list[str]):
        self.cycle_path = list(cycle_path)
        path_desc = " -> ".join(self.cycle_path)
        super().__init__(
            f"depends_on edit would create a dependency cycle: {path_desc}"
        )


def find_dependency_cycle(
    items: list[dict[str, Any]],
    *,
    proposed_edge: tuple[str, str | None] | None = None,
) -> list[str] | None:
    """Return the full cycle path if the ``depends_on`` graph over ``items`` has one.

    ``items`` is any iterable of dicts with at least ``id`` and
    ``depends_on`` keys (a live sprint-item row, a board-snapshot item, or a
    hand-built test fixture — all three shapes work unchanged). Each item may
    declare at most one ``depends_on`` parent (this schema's dependency graph
    is a functional graph — one outgoing edge per node — never a DAG with
    multiple parents), so a cycle, if one exists, is a simple loop reachable
    by following ``depends_on`` repeatedly from any node inside it.

    ``proposed_edge`` optionally overlays a NOT-YET-PERSISTED
    ``(item_id, depends_on)`` pair on top of ``items`` before walking — this
    is how a caller validates a prospective edit (e.g. inside
    ``patch_sprint_item``) without first writing it to the DB. Pass
    ``depends_on=None`` (or falsy) in the tuple to simulate CLEARING the
    edge — this can never introduce a cycle, so it always returns whatever
    the pre-existing graph already contained (normally ``None``).

    Returns ``None`` when the graph (with the overlay applied, if any) is
    acyclic. Returns the full closed path (``path[0] == path[-1]``) starting
    from the lowest-sorted item id that participates in a cycle, so the
    result is deterministic across repeated calls on the same input — the
    same graph never reports two different "first" cycles depending on dict
    iteration order.

    A dangling edge (``depends_on`` pointing at an id absent from ``items``)
    is NOT a cycle — this function only reports genuine loops; missing/
    cross-project/merged-away targets are a different, already-covered
    concern (see ``meridian.db.board_snapshot.find_stale_reference_ids``).
    """
    by_id: dict[str, str | None] = {}
    for it in items:
        iid = it.get("id")
        if not iid:
            continue
        by_id[iid] = it.get("depends_on") or None

    if proposed_edge is not None:
        src, dst = proposed_edge
        if src:
            by_id[src] = dst or None

    def _cycle_from(start: str) -> list[str] | None:
        path = [start]
        seen = {start}
        cur = by_id.get(start)
        while cur:
            if cur in seen:
                cycle_start = path.index(cur)
                return [*path[cycle_start:], cur]
            if cur not in by_id:
                return None  # dangling edge — not a cycle, not our concern here
            path.append(cur)
            seen.add(cur)
            cur = by_id.get(cur)
        return None

    for iid in sorted(by_id):
        cyc = _cycle_from(iid)
        if cyc:
            return cyc
    return None


def compute_dependency_graph_digest(items: list[dict[str, Any]]) -> str:
    """Deterministic ``sha256:<hex>`` digest over every item's ``(id, depends_on)`` edge.

    Order-independent (the payload is sorted by id before hashing), so two
    calls over the same edge set produce the same digest regardless of the
    order ``items`` was iterated in. Items without an ``id`` are skipped.
    Changes iff an item was added/removed from ``items`` or any surviving
    item's ``depends_on`` value changed — nothing else (title, status,
    touches_resources, ...) affects it. Intended for cheap "did the
    dependency graph's SHAPE change since I last looked" comparisons (e.g.
    ``assign_sprint_waves`` staleness diagnostics), not as a substitute for
    ``meridian.db.board_snapshot``'s broader board revision hash.
    """
    payload = sorted(
        (
            {"id": it.get("id"), "depends_on": it.get("depends_on") or None}
            for it in items
            if it.get("id")
        ),
        key=lambda d: d["id"],
    )
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(blob.encode('utf-8')).hexdigest()}"


def find_all_dependency_cycles(items: list[dict[str, Any]]) -> list[list[str]]:
    """Return every distinct cycle present in the ``depends_on`` graph over ``items``.

    Unlike :func:`find_dependency_cycle` (which stops at the first cycle
    found — the right contract for a single prospective-edit check),
    this enumerates ALL cycles so a board-wide diagnostic (e.g.
    ``assign_sprint_waves``) can report every broken chain in one pass rather
    than surfacing only the first and hiding the rest behind it. Cycles are
    deduplicated by their node set (a cycle discovered starting from any of
    its own members is the same cycle) and returned sorted by their first
    (lowest-sorted) member id for deterministic output.
    """
    by_id: dict[str, str | None] = {
        it["id"]: (it.get("depends_on") or None) for it in items if it.get("id")
    }
    found: list[list[str]] = []
    seen_node_sets: set[frozenset[str]] = set()

    for start in sorted(by_id):
        path = [start]
        seen = {start}
        cur = by_id.get(start)
        while cur:
            if cur in seen:
                cycle_start = path.index(cur)
                cycle = [*path[cycle_start:], cur]
                node_set = frozenset(cycle[:-1])
                if node_set not in seen_node_sets:
                    seen_node_sets.add(node_set)
                    found.append(cycle)
                break
            if cur not in by_id:
                break
            path.append(cur)
            seen.add(cur)
            cur = by_id.get(cur)

    found.sort(key=lambda c: c[0])
    return found
