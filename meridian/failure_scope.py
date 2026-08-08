"""Typed stop/continue scope policy across items, clusters, waves, runs,
sprints, and proposals (6e3f5e44).

Follow-up to proposal 54fba2fa-0d4a-4f0e-8282-cf9f4920417d and systemic
invalidation item cc3864bd-41b2-4819-b191-edaa73d22efc. Problem this module
fixes: the only structured failure-scope signal in the codebase today is
``sprint_items.failure_mode``, a bare ``'stop' | 'continue'`` enum
(:data:`meridian.db.wave_runs.WAVE_RUN_CHILD_FAILURE_MODES`) that answers
exactly one question -- "does a failed item block finalizing the wave run it
belongs to" -- and nothing else. It cannot express "pause just this
dependency subgraph", "isolate just this resource cluster", "abort this
whole wave", "abort the sprint version", or "a proposal investigation found
something but must not silently become executable work". Overloading the
two-value field to mean all of those at once is exactly what this item's
notes warn against.

This module is **pure logic** -- no DB, no network, no model call -- mirroring
``meridian.blocker_policy`` and ``meridian.capability_manifest``'s "foundation
only" design. It defines:

1. The typed scope taxonomy (:data:`FAILURE_SCOPES`) and action taxonomy
   (:data:`FAILURE_ACTIONS`) the item's notes ask for, with a conservative,
   deterministic default action per scope (:data:`DEFAULT_ACTION_BY_SCOPE`)
   so an un-configured project/item never silently escalates OR silently
   swallows a failure.
2. :func:`resolve_failure_scope` -- given a target's narrowest-to-widest
   containment chain and a pool of explicit ``FailureScopeDeclaration``
   dicts, resolves "from the narrowest explicit declaration, then parent
   scopes" exactly as the item spec requires, with fail-closed handling for:
   genuine same-scope conflicts (resolves to the MORE restrictive of the
   conflicting actions, never an arbitrary pick), stale declarations (a
   ``board_revision`` that does not match the caller's current expectation
   is excluded, not trusted), cross-project leakage (a declaration for a
   different ``project_id`` never influences this target, even if scope/key
   collide), and fail-closed reason codes (:data:`FAIL_CLOSED_REASON_CODES`
   -- e.g. ``verified_security``, ``systemic_integrity_failure`` -- which
   floor the resolved action to at least ``require_human_decision``
   regardless of what a declaration or default would otherwise pick;
   :func:`floor_action`).
3. :func:`resolve_proposal_failure_scope` -- the ``proposal`` scope is
   deliberately NOT part of an executing item's containment chain (a
   proposal is an investigation track, not a parent of item/wave/wave_run/
   sprint_version/project). Its default action is
   ``require_planner_review`` -- never ``continue_unaffected`` -- so an
   un-declared proposal failure can never silently create executable work,
   per the item spec.
4. Supersession/correction lineage, reusing the SAME pattern
   ``meridian.db.wave_runs`` already established for ``wave_run_events``
   (append a new row, set only ``supersedes``/``superseded_by`` -- the old
   row's body stays byte-identical forever): a corrective declaration whose
   ``supersedes`` names an earlier declaration's ``id`` replaces it in
   resolution; the superseded declaration is dropped from consideration but
   reported (never silently discarded) via ``superseded_declaration_ids``.
5. :func:`scope_action_from_legacy_failure_mode` -- a pure compatibility
   bridge from the existing ``sprint_items.failure_mode`` ``'stop'/'continue'``
   values to this module's typed vocabulary, so callers (and a future
   integration item) can translate the legacy field without re-deriving its
   semantics: ``'continue'`` -> ``(item, continue_unaffected)`` (unchanged
   behavior); ``'stop'`` -> ``(wave_run, pause_affected)`` (matches
   :func:`meridian.db.wave_runs.finalize_wave_run`'s ACTUAL existing
   contract precisely -- a stop-mode failure blocks finalization until
   resolved, it does not itself abort the run; escalating a legacy 'stop' to
   ``abort_run`` would silently change existing behavior).

Deliberate scope cut, matching ``meridian.blocker_policy``'s own documented
split ("DB persistence ... layered on top ... in meridian.db.sprint_items"):
this module does not persist declarations, does not compute a target's
containment chain from live board state, and does not itself transition any
``wave_runs``/``sprint_items``/``workspace`` row. Those are follow-up wiring
work for whichever item integrates this into
``meridian.db.wave_runs``/``meridian.db.board_snapshot``/``meridian.handoff``
-- deliberately NOT done here to avoid an unreviewable cross-cutting rewrite
of those five actively-contended files while cc3864bd (systemic wave-run
invalidation, the concrete wave_run-scope instance of exactly this policy)
is landing concurrently in the same files. The resolver here is what that
integration calls; it does not call it.
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Scope taxonomy
# ---------------------------------------------------------------------------

SCOPE_ITEM = "item"
SCOPE_DEPENDENCY_SUBGRAPH = "dependency_subgraph"
SCOPE_RESOURCE_CLUSTER = "resource_cluster"
SCOPE_WAVE = "wave"
SCOPE_WAVE_RUN = "wave_run"
SCOPE_SPRINT_VERSION = "sprint_version"
SCOPE_PROJECT = "project"
SCOPE_PROPOSAL = "proposal"

FAILURE_SCOPES: frozenset[str] = frozenset({
    SCOPE_ITEM,
    SCOPE_DEPENDENCY_SUBGRAPH,
    SCOPE_RESOURCE_CLUSTER,
    SCOPE_WAVE,
    SCOPE_WAVE_RUN,
    SCOPE_SPRINT_VERSION,
    SCOPE_PROJECT,
    SCOPE_PROPOSAL,
})

#: Containment order, narrowest first, for an EXECUTING item's target chain.
#: ``proposal`` is intentionally excluded -- it is a separate investigation
#: track, never a parent scope of an executing item (see module docstring
#: point 3 and :func:`resolve_proposal_failure_scope`).
SCOPE_ORDER: tuple[str, ...] = (
    SCOPE_ITEM,
    SCOPE_DEPENDENCY_SUBGRAPH,
    SCOPE_RESOURCE_CLUSTER,
    SCOPE_WAVE,
    SCOPE_WAVE_RUN,
    SCOPE_SPRINT_VERSION,
    SCOPE_PROJECT,
)

# ---------------------------------------------------------------------------
# Action taxonomy
# ---------------------------------------------------------------------------

ACTION_CONTINUE_UNAFFECTED = "continue_unaffected"
ACTION_PAUSE_AFFECTED = "pause_affected"
ACTION_BLOCK_FUTURE_CLAIMS = "block_future_claims"
ACTION_REQUIRE_PLANNER_REVIEW = "require_planner_review"
ACTION_REQUIRE_HUMAN_DECISION = "require_human_decision"
ACTION_ABORT_RUN = "abort_run"

FAILURE_ACTIONS: frozenset[str] = frozenset({
    ACTION_CONTINUE_UNAFFECTED,
    ACTION_PAUSE_AFFECTED,
    ACTION_BLOCK_FUTURE_CLAIMS,
    ACTION_REQUIRE_PLANNER_REVIEW,
    ACTION_REQUIRE_HUMAN_DECISION,
    ACTION_ABORT_RUN,
})

#: Deterministic total order used to fail-closed-resolve a genuine same-scope
#: conflict (two active, non-superseded declarations at the identical
#: (scope, key) disagreeing) and to apply :func:`floor_action`: higher is
#: more conservative (stops more). Never pick the first-seen of a conflict --
#: always the more restrictive.
ACTION_RESTRICTIVENESS: dict[str, int] = {
    ACTION_CONTINUE_UNAFFECTED: 0,
    ACTION_PAUSE_AFFECTED: 1,
    ACTION_BLOCK_FUTURE_CLAIMS: 2,
    ACTION_REQUIRE_PLANNER_REVIEW: 3,
    ACTION_REQUIRE_HUMAN_DECISION: 4,
    ACTION_ABORT_RUN: 5,
}

#: Conservative, deterministic default action per scope -- used ONLY when no
#: explicit declaration exists anywhere in a target's containment chain.
#: ``item`` defaults to the safest possible action (never stop the world over
#: one item, matching the existing ``failure_mode='continue'`` default and
#: ``blocker_policy.DEFAULT_POLICY``); ``proposal`` defaults to the most
#: conservative *review* action so an undeclared proposal failure can never
#: silently create executable work (item spec, point 3 above).
DEFAULT_ACTION_BY_SCOPE: dict[str, str] = {
    SCOPE_ITEM: ACTION_CONTINUE_UNAFFECTED,
    SCOPE_DEPENDENCY_SUBGRAPH: ACTION_PAUSE_AFFECTED,
    SCOPE_RESOURCE_CLUSTER: ACTION_PAUSE_AFFECTED,
    SCOPE_WAVE: ACTION_PAUSE_AFFECTED,
    SCOPE_WAVE_RUN: ACTION_PAUSE_AFFECTED,
    SCOPE_SPRINT_VERSION: ACTION_REQUIRE_PLANNER_REVIEW,
    SCOPE_PROJECT: ACTION_REQUIRE_HUMAN_DECISION,
    SCOPE_PROPOSAL: ACTION_REQUIRE_PLANNER_REVIEW,
}

#: Reason codes that are NEVER trusted to resolve to a permissive action,
#: regardless of what a declaration or the scope default says -- mirrors
#: ``meridian.blocker_policy.FAIL_CLOSED_KINDS`` exactly (same fail-closed
#: philosophy, same "explicit + caller-verified, never inferred from a
#: title" discipline: a caller sets one of these reason codes only when it
#: has already verified the condition, not because a title looked scary).
FAIL_CLOSED_REASON_CODES: frozenset[str] = frozenset({
    "verified_security",
    "integrity_corruption",
    "systemic_integrity_failure",
    "foundational_hypothesis_disproven",
    "cross_tenant_data_exposure",
})

#: Minimum action a fail-closed reason code floors resolution to. Every
#: fail-closed reason code shares the same floor: at least a human must see
#: it before anything proceeds. A declaration MAY still explicitly escalate
#: further (e.g. ``abort_run``, which outranks the floor) -- the floor only
#: ever raises, never lowers, the resolved action.
_FAIL_CLOSED_FLOOR: str = ACTION_REQUIRE_HUMAN_DECISION


class FailureScopeError(ValueError):
    """Raised on an invalid scope/action value or malformed declaration."""


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_scope(value: Any) -> str:
    """Validate + normalize a failure scope. Raises :class:`FailureScopeError`
    on anything not in :data:`FAILURE_SCOPES` -- unlike
    ``blocker_policy.normalize_policy``, there is no "None means a safe
    default" carve-out here: scope is a required, caller-supplied field
    identifying WHERE a declaration applies, not a configurable preference."""
    if not isinstance(value, str) or not value.strip():
        raise FailureScopeError("scope must be a non-empty string")
    normalized = value.strip().lower()
    if normalized not in FAILURE_SCOPES:
        raise FailureScopeError(
            f"scope must be one of {sorted(FAILURE_SCOPES)}, got {value!r}"
        )
    return normalized


def normalize_action(value: Any) -> str:
    """Validate + normalize a failure action. Raises :class:`FailureScopeError`
    on anything not in :data:`FAILURE_ACTIONS`."""
    if not isinstance(value, str) or not value.strip():
        raise FailureScopeError("action must be a non-empty string")
    normalized = value.strip().lower()
    if normalized not in FAILURE_ACTIONS:
        raise FailureScopeError(
            f"action must be one of {sorted(FAILURE_ACTIONS)}, got {value!r}"
        )
    return normalized


def is_fail_closed_reason(reason_code: "str | None") -> bool:
    """True iff ``reason_code`` always floors resolution to at least
    :data:`ACTION_REQUIRE_HUMAN_DECISION` (see :data:`FAIL_CLOSED_REASON_CODES`)."""
    return reason_code in FAIL_CLOSED_REASON_CODES


def floor_action(action: str, reason_code: "str | None") -> str:
    """Return ``action``, or :data:`_FAIL_CLOSED_FLOOR` if that is MORE
    restrictive than ``action`` and ``reason_code`` is fail-closed. Never
    lowers an already-more-restrictive explicit action (e.g. an explicit
    ``abort_run`` with a fail-closed reason code stays ``abort_run``)."""
    action = normalize_action(action)
    if not is_fail_closed_reason(reason_code):
        return action
    if ACTION_RESTRICTIVENESS[_FAIL_CLOSED_FLOOR] > ACTION_RESTRICTIVENESS[action]:
        return _FAIL_CLOSED_FLOOR
    return action


def _most_restrictive(actions: "list[str]") -> str:
    """Deterministic pick: the single most-restrictive action in ``actions``.
    Ties (impossible today since :data:`ACTION_RESTRICTIVENESS` is injective,
    kept as a defensive invariant) would fall back to the first in
    :data:`FAILURE_ACTIONS` iteration order for determinism."""
    return max(actions, key=lambda a: (ACTION_RESTRICTIVENESS[a], a))


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------

_REQUIRED_DECLARATION_FIELDS = ("id", "project_id", "scope", "key", "action")


def normalize_declaration(decl: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalize one declaration dict.

    Required: ``id`` (str), ``project_id`` (str), ``scope`` (one of
    :data:`FAILURE_SCOPES`), ``key`` (str -- the specific instance within
    that scope, e.g. an item id for ``scope='item'`` or a wave name for
    ``scope='wave'``), ``action`` (one of :data:`FAILURE_ACTIONS`).

    Optional, defaulted: ``reason_code`` (defaults to ``"unspecified"``,
    which is never in :data:`FAIL_CLOSED_REASON_CODES`), ``evidence_refs``
    (defaults to ``()``), ``actor``/``session_id``/``board_revision``/
    ``declared_at`` (default ``None``), ``supersedes`` (id of an earlier
    declaration this one corrects; default ``None``).

    Raises :class:`FailureScopeError` naming the exact missing/invalid field
    -- callers that need to tolerate a mixed pool of trusted and untrusted
    declarations should catch this per-declaration (see
    :func:`resolve_failure_scope`'s ``invalid_declarations`` reporting)
    rather than pre-filtering blindly.
    """
    if not isinstance(decl, dict):
        raise FailureScopeError("declaration must be a dict")
    missing = [f for f in _REQUIRED_DECLARATION_FIELDS if not decl.get(f)]
    if missing:
        raise FailureScopeError(
            f"declaration missing required field(s): {missing} in {decl!r}"
        )
    evidence_refs = decl.get("evidence_refs") or ()
    if isinstance(evidence_refs, (list, tuple, set)):
        evidence_refs = tuple(str(e) for e in evidence_refs)
    else:
        raise FailureScopeError(
            f"evidence_refs must be a list/tuple/set, got {evidence_refs!r}"
        )
    return {
        "id": str(decl["id"]),
        "project_id": str(decl["project_id"]),
        "scope": normalize_scope(decl["scope"]),
        "key": str(decl["key"]),
        "action": normalize_action(decl["action"]),
        "reason_code": str(decl.get("reason_code") or "unspecified"),
        "evidence_refs": evidence_refs,
        "actor": decl.get("actor"),
        "session_id": decl.get("session_id"),
        "board_revision": decl.get("board_revision"),
        "declared_at": decl.get("declared_at"),
        "supersedes": decl.get("supersedes"),
    }


def _partition_declarations(
    declarations: "list[dict[str, Any]]",
    *,
    project_id: str,
    expected_board_revision: "str | None",
) -> dict[str, list[dict[str, Any]]]:
    """Normalize every raw declaration and split into buckets:
    ``active`` (usable for resolution), ``invalid`` (malformed -- reported,
    never silently dropped), ``cross_project`` (different ``project_id`` --
    two-project isolation), ``stale`` (``board_revision`` set and mismatched
    -- never trusted), ``superseded`` (named by another active declaration's
    ``supersedes``).
    """
    normalized: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for raw in declarations or []:
        try:
            normalized.append(normalize_declaration(raw))
        except FailureScopeError as exc:
            invalid.append({"declaration": raw, "error": str(exc)})

    cross_project = [d for d in normalized if d["project_id"] != project_id]
    same_project = [d for d in normalized if d["project_id"] == project_id]

    if expected_board_revision is None:
        stale: list[dict[str, Any]] = []
        fresh = same_project
    else:
        stale = [
            d for d in same_project
            if d["board_revision"] is not None and d["board_revision"] != expected_board_revision
        ]
        stale_ids = {d["id"] for d in stale}
        fresh = [d for d in same_project if d["id"] not in stale_ids]

    superseded_ids = {d["supersedes"] for d in fresh if d.get("supersedes")}
    superseded = [d for d in fresh if d["id"] in superseded_ids]
    active = [d for d in fresh if d["id"] not in superseded_ids]

    return {
        "active": active,
        "invalid": invalid,
        "cross_project": cross_project,
        "stale": stale,
        "superseded": superseded,
    }


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve_failure_scope(
    declarations: "list[dict[str, Any]]",
    *,
    target_chain: "list[tuple[str, str | None]]",
    project_id: str,
    expected_board_revision: "str | None" = None,
) -> dict[str, Any]:
    """Resolve the effective failure-scope policy for one target.

    ``target_chain`` is the target's own containment chain, narrowest scope
    first -- e.g. for an item ``I`` in wave ``W`` of wave_run ``R`` at
    version ``V`` in project ``P`` with no dependency-subgraph/resource-
    cluster root: ``[("item", I), ("dependency_subgraph", None),
    ("resource_cluster", None), ("wave", W), ("wave_run", R),
    ("sprint_version", V), ("project", P)]``. A ``None`` key means "this
    target has no instance at that scope" and is skipped.

    Walks ``target_chain`` narrowest to widest. The first scope level with
    one or more ACTIVE declarations at its exact ``(scope, key)`` wins --
    "resolve from the narrowest explicit declaration, then parent scopes".
    Exactly one active declaration at that level resolves directly; two or
    more (a genuine unsuperseded conflict) resolve fail-closed to the MORE
    restrictive of the conflicting actions (:func:`_most_restrictive`), never
    an arbitrary pick, with ``conflict=True`` and every conflicting id named.

    If NO level has any declaration at all, falls back to
    :data:`DEFAULT_ACTION_BY_SCOPE` for the narrowest scope in
    ``target_chain`` (conservative, deterministic default -- never an error).

    Whatever action is arrived at (declared / conflict-resolved / default) is
    passed through :func:`floor_action` against the winning reason code (the
    default path uses ``"unspecified"``, which never floors) -- a fail-closed
    reason code can only ever raise the effective action, never lower it
    below what was declared.

    Returns a dict with: ``project_id``, ``resolved_scope``, ``resolved_key``,
    ``action``, ``reason_code``, ``evidence_refs``, ``source``
    (``"declared"``/``"conflict"``/``"default"``), ``declaration_id``
    (``None`` for ``"default"``), ``conflict``, ``conflicting_declaration_ids``,
    ``superseded_declaration_ids``, ``stale_declaration_ids``,
    ``cross_project_ignored_count``, ``invalid_declarations``.
    """
    if not target_chain:
        raise FailureScopeError("target_chain must be non-empty")

    buckets = _partition_declarations(
        declarations, project_id=project_id,
        expected_board_revision=expected_board_revision,
    )
    active = buckets["active"]

    for scope, key in target_chain:
        scope = normalize_scope(scope)
        if key is None:
            continue
        key = str(key)
        matches = [d for d in active if d["scope"] == scope and d["key"] == key]
        if not matches:
            continue

        if len(matches) == 1:
            winner = matches[0]
            source = "declared"
            conflict = False
            conflicting_ids: list[str] = []
            effective_action = floor_action(winner["action"], winner["reason_code"])
        else:
            # Floor EACH candidate's own action against ITS OWN reason code
            # first, then take the most restrictive of those effective
            # actions -- a fail-closed reason code on a declaration must
            # still floor the outcome even when that declaration's raw
            # (pre-floor) action is not the most restrictive of the group.
            effective_by_id = {
                m["id"]: floor_action(m["action"], m["reason_code"]) for m in matches
            }
            effective_action = _most_restrictive(list(effective_by_id.values()))
            # Deterministic winner among the tied-most-restrictive matches,
            # for stable declaration_id/reason_code/evidence_refs reporting.
            winner = min(
                (m for m in matches if effective_by_id[m["id"]] == effective_action),
                key=lambda m: m["id"],
            )
            source = "conflict"
            conflict = True
            conflicting_ids = sorted(m["id"] for m in matches)

        return {
            "project_id": project_id,
            "resolved_scope": scope,
            "resolved_key": key,
            "action": effective_action,
            "reason_code": winner["reason_code"],
            "evidence_refs": winner["evidence_refs"],
            "source": source,
            "declaration_id": winner["id"],
            "conflict": conflict,
            "conflicting_declaration_ids": conflicting_ids,
            "superseded_declaration_ids": sorted(d["id"] for d in buckets["superseded"]),
            "stale_declaration_ids": sorted(d["id"] for d in buckets["stale"]),
            "cross_project_ignored_count": len(buckets["cross_project"]),
            "invalid_declarations": buckets["invalid"],
        }

    # No declaration anywhere in the chain -- conservative deterministic
    # default, scoped to the narrowest level in the chain.
    default_scope = normalize_scope(target_chain[0][0])
    default_action = DEFAULT_ACTION_BY_SCOPE[default_scope]
    return {
        "project_id": project_id,
        "resolved_scope": default_scope,
        "resolved_key": target_chain[0][1],
        "action": default_action,
        "reason_code": "unspecified",
        "evidence_refs": (),
        "source": "default",
        "declaration_id": None,
        "conflict": False,
        "conflicting_declaration_ids": [],
        "superseded_declaration_ids": sorted(d["id"] for d in buckets["superseded"]),
        "stale_declaration_ids": sorted(d["id"] for d in buckets["stale"]),
        "cross_project_ignored_count": len(buckets["cross_project"]),
        "invalid_declarations": buckets["invalid"],
    }


def resolve_proposal_failure_scope(
    declarations: "list[dict[str, Any]]",
    *,
    proposal_id: str,
    project_id: str,
    expected_board_revision: "str | None" = None,
) -> dict[str, Any]:
    """Resolve the effective failure-scope policy for a proposal
    investigation. ``proposal`` is a standalone scope (see module docstring
    point 3) -- this is exactly :func:`resolve_failure_scope` with a
    single-level ``target_chain`` of ``[("proposal", proposal_id)]``, so the
    default (no declaration) always resolves to
    ``DEFAULT_ACTION_BY_SCOPE["proposal"] == "require_planner_review"``,
    never ``continue_unaffected`` -- an undeclared proposal failure can never
    silently become executable work. An EXPLICIT declaration (not silent, by
    definition) may still choose any action, including
    ``continue_unaffected``, if a planner has actually reviewed and decided
    that.
    """
    return resolve_failure_scope(
        declarations,
        target_chain=[(SCOPE_PROPOSAL, proposal_id)],
        project_id=project_id,
        expected_board_revision=expected_board_revision,
    )


# ---------------------------------------------------------------------------
# Legacy compatibility bridge
# ---------------------------------------------------------------------------

#: ``sprint_items.failure_mode`` / ``wave_runs.WAVE_RUN_CHILD_FAILURE_MODES``
#: valid values -- duplicated here (not imported) so this module has zero
#: dependency on ``meridian.db``, matching ``meridian.blocker_policy``'s own
#: "pure logic" isolation. Tests assert this stays in lockstep with
#: ``meridian.db.wave_runs.WAVE_RUN_CHILD_FAILURE_MODES``.
LEGACY_FAILURE_MODES: frozenset[str] = frozenset({"stop", "continue"})


def scope_action_from_legacy_failure_mode(failure_mode: "str | None") -> tuple[str, str]:
    """Translate the existing ``sprint_items.failure_mode`` value into this
    module's typed ``(scope, action)`` vocabulary, WITHOUT changing existing
    behavior:

    * ``None`` or ``'continue'`` (the column default) -> ``(item,
      continue_unaffected)``.
    * ``'stop'`` -> ``(wave_run, pause_affected)`` -- this matches
      :func:`meridian.db.wave_runs.finalize_wave_run`'s ACTUAL contract: a
      ``failure_mode='stop'`` child that fails blocks finalization
      (:class:`meridian.db.wave_runs.WaveRunFinalizationBlocked`) until
      resolved or the run is explicitly aborted -- it does not, by itself,
      abort the run. Mapping it to ``abort_run`` here would silently claim a
      stronger behavior than the code actually has.

    Raises :class:`FailureScopeError` for any other value.
    """
    normalized = (failure_mode or "continue").strip().lower()
    if normalized not in LEGACY_FAILURE_MODES:
        raise FailureScopeError(
            f"failure_mode must be one of {sorted(LEGACY_FAILURE_MODES)}, "
            f"got {failure_mode!r}"
        )
    if normalized == "stop":
        return (SCOPE_WAVE_RUN, ACTION_PAUSE_AFFECTED)
    return (SCOPE_ITEM, ACTION_CONTINUE_UNAFFECTED)
