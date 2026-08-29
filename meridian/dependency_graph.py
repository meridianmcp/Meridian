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


# ---------------------------------------------------------------------------
# 83a7586d — fan-out/fan-in dependency FRONTIER (machine-readable barrier
# representation), additive alongside everything above.
#
# THE GAP: every function above (and every existing consumer of
# ``depends_on`` across the codebase — ``_topo_depth_map``,
# ``get_blocking_dependency_for_sprint_item``, ``get_sprint_items``'s
# ``show_blocked`` filter, ``_dependency_frontier_snapshot``,
# ``executor_contract._resolve_dependency_state``) treats this schema as a
# FUNCTIONAL graph: exactly one ``depends_on`` predecessor per node, full
# stop. That is correct and unchanged for a legacy single-parent chain, but
# it has no way to express a genuine fan-in "barrier" item — one that must
# not be claimed until SEVERAL predecessors are all terminal (a join/gate
# node converging multiple upstream branches).
#
# THE REPRESENTATION (purely additive — no DB migration, no new column):
# ``depends_on`` keeps its existing TEXT shape. A legacy row stores a bare
# id (``"abc123"``) exactly as before — completely unaffected. A fan-in
# barrier item stores a JSON array of ids in that SAME column
# (``'["abc123", "def456", "ghi789"]'``) — see :func:`parse_predecessor_ids`
# / :func:`encode_predecessor_ids`. Every reader in THIS module (and the new
# claim-time gate / planner check built on it — see
# ``meridian.db.sprint_items.get_dependency_frontier``) understands both
# shapes transparently; every reader NOT updated for this item (dashboard
# TS, ``executor_contract.py``, etc.) simply fails CLOSED on a fan-in row —
# a JSON-array string is never a valid item id, so the legacy single-lookup
# code path just doesn't find a match and treats it as blocked/unknown,
# never as falsely satisfied. Safe by construction, not by discipline.
#
# THE FRONTIER (distinct from a macro-wave DISPLAY label): "ready" here
# means "every declared predecessor is ACTUALLY terminal right now" — the
# real dependency-topology answer. A macro-wave/batch grouping (see
# ``meridian.db.sprint_items.get_parallelizable_groups`` /
# ``pack_groups_into_macro_waves``) is a resource-conflict-free PRESENTATION
# packing computed independently; it must never be read as a claimability
# proof for a fan-in item's dependency edges. This module stays a DB-free
# leaf: :func:`evaluate_frontier` takes a caller-supplied predecessor lookup
# (the async DB-aware fetch — including this project's autonomous
# stale-claim reconciliation for a blocking in_progress predecessor — lives
# in ``meridian.db.sprint_items.get_dependency_frontier``), and
# :func:`compute_frontier` is the pure whole-board convenience wrapper for
# tests / callers that already hold a full item snapshot in memory.
# ---------------------------------------------------------------------------

#: Sprint-item statuses considered terminal for dependency-satisfaction
#: purposes. Mirrors ``meridian.db.sprint_items.get_sprint_items``'s own
#: ``_terminal`` set (``show_blocked=False`` filter) exactly — kept as one
#: named constant here so every frontier consumer agrees on the same set.
TERMINAL_SPRINT_STATUSES = frozenset({"done", "skipped", "failed", "pushed"})


def parse_predecessor_ids(depends_on: Any) -> list[str]:
    """Return every predecessor id a ``depends_on`` value declares.

    Three input shapes, all handled transparently:

      * Falsy (``None``, ``""``) — no dependency: returns ``[]``.
      * A bare string that is NOT a JSON array (every legacy row, e.g.
        ``"abc123"``) — a single-parent edge: returns ``["abc123"]``,
        byte-for-byte the same "one predecessor" meaning every existing
        single-parent reader already assumes.
      * A JSON array string (``'["a", "b", "c"]'``) or an actual
        ``list``/``tuple``/``set`` — a fan-out/fan-in BARRIER declaration:
        returns every non-empty id, de-duplicated, order preserved.

    Never raises: a malformed JSON-ish string (starts with ``[`` but fails
    to parse, or parses to something that isn't a list) falls back to
    treating the whole string as a single literal id — the same fail-closed
    posture as any other unrecognized id (it simply won't match a real item,
    so a caller's lookup treats it as an unresolved/missing predecessor
    rather than silently dropping the dependency entirely).
    """
    if not depends_on:
        return []
    if isinstance(depends_on, (list, tuple, set)):
        raw_ids: list[Any] = list(depends_on)
    elif isinstance(depends_on, str):
        s = depends_on.strip()
        if not s:
            return []
        raw_ids = None
        if s.startswith("["):
            try:
                parsed = json.loads(s)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, list):
                raw_ids = parsed
        if raw_ids is None:
            raw_ids = [s]
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_ids:
        iid = str(raw).strip()
        if iid and iid not in seen:
            seen.add(iid)
            out.append(iid)
    return out


def encode_predecessor_ids(ids: "Any") -> "str | None":
    """Inverse of :func:`parse_predecessor_ids`.

    Encodes a predecessor-id sequence back into the ``depends_on`` TEXT
    shape: zero ids -> ``None`` (no dependency); exactly one id -> that bare
    id string (stays byte-for-byte legacy-compatible, e.g. for
    ``update_sprint_item``/``patch_sprint_item`` callers that only ever set
    a single parent); two or more -> a JSON array string (the fan-in barrier
    shape). De-duplicates, preserving first-seen order.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in (ids or []):
        iid = str(raw).strip()
        if iid and iid not in seen:
            seen.add(iid)
            out.append(iid)
    if not out:
        return None
    if len(out) == 1:
        return out[0]
    return json.dumps(out)


def evaluate_frontier(
    item: dict[str, Any],
    predecessor_lookup: dict[str, "dict[str, Any] | None"],
    *,
    terminal_statuses: "frozenset[str] | set[str] | None" = None,
) -> dict[str, Any]:
    """Pure single-item frontier evaluation.

    ``predecessor_lookup`` is a caller-supplied ``{predecessor_id:
    item_dict_or_None}`` mapping for every id ``item``'s ``depends_on`` edge
    declares (see :func:`parse_predecessor_ids`) — this function never looks
    beyond what it's given, so it works identically whether the caller built
    the lookup from a live DB fetch (``meridian.db.sprint_items.
    get_dependency_frontier``) or a hand-built test fixture / in-memory
    board snapshot (:func:`compute_frontier`).

    Returns::

        {
          "predecessor_ids": [...],   # every declared predecessor id, in order (0, 1, or N)
          "ready": bool,               # True iff EVERY predecessor is satisfied
                                        # (vacuously True when there are none)
          "blocking": [                # unsatisfied predecessors, in declared order
              {"id": ..., "status": ..., "reason": ...}, ...
          ],
          "predecessor_statuses": {id: status_or_None},
        }

    A predecessor absent from ``predecessor_lookup`` (or mapped to ``None``)
    is treated as ``status=None`` and reported as blocking with
    ``status="missing"`` — the fan-in-aware generalization of
    ``get_blocking_dependency_for_sprint_item``'s existing "(missing sprint
    item)" convention for a dangling single-parent edge.

    A ``"failed"`` predecessor is satisfied UNLESS ``item``'s own
    ``failure_mode`` is ``"stop"`` (default ``"continue"`` when unset) —
    reused verbatim from the SAME carve-out already applied independently by
    ``get_sprint_items(show_blocked=False)``, ``get_parallelizable_groups``,
    and ``executor_contract._resolve_dependency_state`` (never re-derived a
    fourth time with a risk of drifting from the other three).
    """
    terms = terminal_statuses if terminal_statuses is not None else TERMINAL_SPRINT_STATUSES
    failure_mode = item.get("failure_mode") or "continue"
    predecessor_ids = parse_predecessor_ids(item.get("depends_on"))
    statuses: dict[str, "str | None"] = {}
    blocking: list[dict[str, Any]] = []
    for pid in predecessor_ids:
        parent = predecessor_lookup.get(pid)
        status = parent.get("status") if isinstance(parent, dict) else None
        statuses[pid] = status
        if status is None:
            blocking.append({"id": pid, "status": "missing", "reason": "predecessor not found"})
            continue
        if status not in terms:
            blocking.append({"id": pid, "status": status, "reason": "not yet terminal"})
            continue
        if status == "failed" and failure_mode == "stop":
            blocking.append({
                "id": pid, "status": status,
                "reason": "predecessor failed and failure_mode='stop'",
            })
            continue
        # satisfied: done / skipped / pushed, or failed with failure_mode='continue'
    return {
        "predecessor_ids": predecessor_ids,
        "ready": not blocking,
        "blocking": blocking,
        "predecessor_statuses": statuses,
    }


def compute_frontier(
    items: list[dict[str, Any]],
    *,
    terminal_statuses: "frozenset[str] | set[str] | None" = None,
) -> dict[str, dict[str, Any]]:
    """Whole-board convenience wrapper around :func:`evaluate_frontier`.

    Builds the predecessor lookup FROM ``items`` itself — no DB access, this
    module stays a leaf — so this is only accurate when ``items`` already
    contains every predecessor a caller cares about (e.g. a full live
    ``get_sprint_items`` scan). A predecessor id not present in ``items`` is
    reported as ``status="missing"``/blocking, exactly as
    :func:`evaluate_frontier` documents — this is the right behavior for a
    genuinely-deleted/foreign id, but means a CALLER that passes only a
    filtered subset (e.g. "pending items only", where an already-DONE
    predecessor has been filtered OUT precisely because it finished) will
    see that predecessor reported as missing rather than satisfied. Use
    ``meridian.db.sprint_items.get_dependency_frontier`` (live DB lookups)
    instead of this function whenever the item set at hand might not
    include every real predecessor.

    Returns ``{item_id: evaluate_frontier(...)}`` for every item that has an
    ``id``.
    """
    by_id = {it["id"]: it for it in items if it.get("id")}
    return {
        iid: evaluate_frontier(it, by_id, terminal_statuses=terminal_statuses)
        for iid, it in by_id.items()
    }
