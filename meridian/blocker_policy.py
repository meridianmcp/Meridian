"""Typed blocker triage: classification + configurable run policy (b108f2e0).

Observed failure this module fixes: a sprint item titled as a CRITICAL
tenant-isolation security item, but with empty notes, no reproduction, and no
code pointers, correctly made an executor refuse to invent scope for it -- but
the executor then incorrectly stopped the ENTIRE run instead of quarantining
just that one item and continuing independent work.

This module is pure logic -- no DB, no network, no model call -- mirroring
``meridian.capability_manifest``'s "foundation only" design: it classifies a
sprint item's blocker (if any) from item data plus caller-supplied signals,
and combines a batch of per-item classifications into a whole-run decision
(what to quarantine, what dependents to pause, whether the whole run must
fail closed, and why). DB persistence (the project-configurable
``executor_blocker_policy`` setting) and live-signal gathering (prospecting
receipts, capability availability, dependency status) are layered on top in
``meridian.db.sprint_items`` -- see :func:`evaluate_board_blockers` and
:func:`classify_and_evaluate` below for the pure combinators those callers
use.

Taxonomy (exactly 8 typed kinds -- see ``BLOCKER_KINDS``):

* ``needs_prospecting`` -- the item declares resources (touches_resources /
  tool_requirements) but no code-intel prospecting evidence exists yet for it.
* ``needs_scope`` -- the item has NO evidence at all to act on: blank notes,
  no declared resources, no tool requirements. This is the exact incident
  case above -- a bare CRITICAL title is deliberately NOT enough evidence by
  itself (see :func:`classify_item_blocker`).
* ``optional_tool_unavailable`` -- a non-required tool/capability the item
  wanted is unavailable and no approved fallback exists.
* ``dependency_blocked`` -- the item's own dependency chain is not yet
  satisfied (computed by the caller and passed in explicitly).
* ``verified_security`` -- an EXPLICIT, caller-verified tenant/data-boundary
  or active-credential security exposure. Never inferred from a title/
  priority alone.
* ``integrity_corruption`` -- EXPLICIT, caller-verified DOCX/canonical-write
  integrity corruption.
* ``human_action`` -- the item is blocked on a real-world action outside
  Meridian, or explicitly flagged ``require_human_review``.
* ``run_global_blocker`` -- an explicit project/admin run-stop directive.

Fail-closed kinds (``FAIL_CLOSED_KINDS``): ``verified_security``,
``integrity_corruption``, and ``run_global_blocker`` ALWAYS stop the whole
run, regardless of the configured policy -- these three (plus "a required
capability with no approved fallback", which is already handled by
``capability_contract.build_capability_contract``'s own
``executable``/``executable_reasons`` fail-closed logic and is intentionally
NOT re-implemented here) are the explicit fail-closed exceptions called out
in the sprint-item spec. Every other kind is safe to quarantine-and-continue.

Policy values (``VALID_POLICIES``, default ``DEFAULT_POLICY``):

* ``quarantine_continue`` (default, safest) -- quarantine only the blocked
  item(s) and their dependency closure; every other item stays eligible.
* ``auto_prospect`` -- same closure computation, but signals the caller
  should attempt automatic re-prospecting for ``needs_prospecting`` items
  before treating them as quarantined.
* ``item_stop`` -- quarantine only the item scope; no auto-prospect routing.
* ``run_stop`` -- a conservative, explicit admin policy: ANY blocked item
  (not just a fail-closed one) halts the whole run.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------

BLOCKER_KIND_NEEDS_PROSPECTING = "needs_prospecting"
BLOCKER_KIND_NEEDS_SCOPE = "needs_scope"
BLOCKER_KIND_OPTIONAL_TOOL_UNAVAILABLE = "optional_tool_unavailable"
BLOCKER_KIND_DEPENDENCY_BLOCKED = "dependency_blocked"
BLOCKER_KIND_VERIFIED_SECURITY = "verified_security"
BLOCKER_KIND_INTEGRITY_CORRUPTION = "integrity_corruption"
BLOCKER_KIND_HUMAN_ACTION = "human_action"
BLOCKER_KIND_RUN_GLOBAL_BLOCKER = "run_global_blocker"

BLOCKER_KINDS = frozenset({
    BLOCKER_KIND_NEEDS_PROSPECTING,
    BLOCKER_KIND_NEEDS_SCOPE,
    BLOCKER_KIND_OPTIONAL_TOOL_UNAVAILABLE,
    BLOCKER_KIND_DEPENDENCY_BLOCKED,
    BLOCKER_KIND_VERIFIED_SECURITY,
    BLOCKER_KIND_INTEGRITY_CORRUPTION,
    BLOCKER_KIND_HUMAN_ACTION,
    BLOCKER_KIND_RUN_GLOBAL_BLOCKER,
})

# Kinds that ALWAYS force a whole-run stop, regardless of the configured
# policy -- see module docstring for why these three (and only these three;
# the fourth fail-closed case from the spec, a required capability with no
# fallback, is already handled by capability_contract's own executable flag).
FAIL_CLOSED_KINDS = frozenset({
    BLOCKER_KIND_VERIFIED_SECURITY,
    BLOCKER_KIND_INTEGRITY_CORRUPTION,
    BLOCKER_KIND_RUN_GLOBAL_BLOCKER,
})

# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

POLICY_QUARANTINE_CONTINUE = "quarantine_continue"
POLICY_AUTO_PROSPECT = "auto_prospect"
POLICY_ITEM_STOP = "item_stop"
POLICY_RUN_STOP = "run_stop"

VALID_POLICIES = frozenset({
    POLICY_QUARANTINE_CONTINUE,
    POLICY_AUTO_PROSPECT,
    POLICY_ITEM_STOP,
    POLICY_RUN_STOP,
})

# Safe default: never stop an otherwise-executable run over one bad item.
DEFAULT_POLICY = POLICY_QUARANTINE_CONTINUE


class BlockerPolicyError(ValueError):
    """Raised on an invalid policy value or malformed classification input."""


def normalize_policy(value: Any) -> str:
    """Validate + normalize an ``executor_blocker_policy`` value.

    ``None`` normalizes to :data:`DEFAULT_POLICY` (a project that never
    configured one gets the safe default, never an error -- mirrors
    ``capability_manifest``'s "no manifest -> empty profile, never an
    error" contract). Any other non-member value raises
    :class:`BlockerPolicyError` deterministically.
    """
    if value is None:
        return DEFAULT_POLICY
    if not isinstance(value, str) or not value.strip():
        raise BlockerPolicyError("executor_blocker_policy must be a non-empty string")
    normalized = value.strip().lower()
    if normalized not in VALID_POLICIES:
        raise BlockerPolicyError(
            f"executor_blocker_policy must be one of {sorted(VALID_POLICIES)}, got {value!r}"
        )
    return normalized


def is_fail_closed(kind: "str | None") -> bool:
    """True iff ``kind`` is one of the three kinds that always halt the run."""
    return kind in FAIL_CLOSED_KINDS


# ---------------------------------------------------------------------------
# Per-item evidence + classification
# ---------------------------------------------------------------------------

def _truthy_collection(raw: Any) -> bool:
    """True iff ``raw`` is a non-empty list/tuple, or a JSON-list-shaped
    string that isn't the empty/null spellings ``sprint_items`` stores for
    "nothing declared" (``None``, ``""``, ``"[]"``, ``"null"``).
    """
    if raw is None:
        return False
    if isinstance(raw, (list, tuple, set)):
        return len(raw) > 0
    if isinstance(raw, str):
        return raw.strip() not in ("", "[]", "null")
    return bool(raw)


def scope_evidence(item: dict[str, Any]) -> dict[str, Any]:
    """Compute the raw scope-evidence signals for one sprint item dict.

    Deliberately narrow: a bare title (however urgent) is NEVER evidence.
    Evidence is exactly one of: non-blank ``notes``, a declared
    ``touches_resources`` list, a declared ``tool_requirements`` list, or a
    single legacy ``required_tool`` pin. ``has_scope_evidence`` is the OR of
    all four -- an item with even one of these is presumed to have SOME
    actionable scope; an item with NONE of them is the exact "empty CRITICAL
    item" incident this module exists to fix.
    """
    notes = item.get("notes")
    has_notes = bool(notes) and bool(str(notes).strip())
    has_resources = _truthy_collection(item.get("touches_resources"))
    has_tool_requirements = _truthy_collection(item.get("tool_requirements")) or bool(
        item.get("required_tool")
    )
    return {
        "has_notes": has_notes,
        "has_resources": has_resources,
        "has_tool_requirements": has_tool_requirements,
        "has_scope_evidence": has_notes or has_resources or has_tool_requirements,
    }


def classify_item_blocker(
    item: dict[str, Any],
    *,
    prospected: "bool | None" = None,
    tool_unavailable: bool = False,
    has_approved_fallback: bool = True,
    dependency_blocked: bool = False,
    require_human_review: "bool | None" = None,
    verified_security: bool = False,
    integrity_corruption: bool = False,
    run_global_blocker: bool = False,
) -> dict[str, Any]:
    """Classify ONE sprint item's blocker kind, or ``None`` if it isn't blocked.

    Every keyword argument is an EXPLICIT signal the caller has already
    verified -- this function never infers ``verified_security`` /
    ``integrity_corruption`` / ``run_global_blocker`` / ``human_action`` from
    item content (title, priority, etc.) by itself. That is the core fix:
    "do not treat a bare CRITICAL title as verified evidence."

    Priority order (first match wins) -- fail-closed signals outrank
    everything else, then explicit human/dependency/tool signals, then the
    evidence-based scope check:

    1. ``run_global_blocker`` -> ``run_global_blocker``
    2. ``verified_security`` -> ``verified_security``
    3. ``integrity_corruption`` -> ``integrity_corruption``
    4. ``require_human_review`` (explicit, or ``milestone_type == "human"``)
       -> ``human_action``
    5. ``dependency_blocked`` -> ``dependency_blocked``
    6. ``tool_unavailable and not has_approved_fallback`` ->
       ``optional_tool_unavailable``
    7. no scope evidence at all -> ``needs_scope``
    8. declares resources but ``prospected is False`` -> ``needs_prospecting``
    9. otherwise -> not blocked (``kind`` is ``None``)

    Returns ``{"item_id", "kind", "fail_closed", "evidence"}``.
    """
    if not isinstance(item, dict):
        raise BlockerPolicyError("item must be a dict")

    evidence = scope_evidence(item)

    kind: "str | None"
    if run_global_blocker:
        kind = BLOCKER_KIND_RUN_GLOBAL_BLOCKER
    elif verified_security:
        kind = BLOCKER_KIND_VERIFIED_SECURITY
    elif integrity_corruption:
        kind = BLOCKER_KIND_INTEGRITY_CORRUPTION
    elif require_human_review or (item.get("milestone_type") == "human"):
        kind = BLOCKER_KIND_HUMAN_ACTION
    elif dependency_blocked:
        kind = BLOCKER_KIND_DEPENDENCY_BLOCKED
    elif tool_unavailable and not has_approved_fallback:
        kind = BLOCKER_KIND_OPTIONAL_TOOL_UNAVAILABLE
    elif not evidence["has_scope_evidence"]:
        kind = BLOCKER_KIND_NEEDS_SCOPE
    elif evidence["has_resources"] and prospected is False:
        kind = BLOCKER_KIND_NEEDS_PROSPECTING
    else:
        kind = None

    return {
        "item_id": item.get("id"),
        "kind": kind,
        "fail_closed": is_fail_closed(kind),
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# Dependency / resource closure
# ---------------------------------------------------------------------------

def compute_dependent_closure(
    items: "list[dict[str, Any]]", blocked_ids: "list[str] | set[str]"
) -> dict[str, list[str]]:
    """For each id in ``blocked_ids``, return the sorted ids of every item
    that transitively depends on it via the single-parent ``depends_on``
    chain sprint_items uses (``item.depends_on`` is one parent id, not a
    list -- see ``meridian.db.sprint_items.add_sprint_item``).

    Only items present in ``items`` are walked; a ``depends_on`` pointing
    outside the given set is simply a dead end (no dependent to report). A
    dependent that is ITSELF already in ``blocked_ids`` is excluded from its
    ancestor's list (it is reported under its own key instead, not
    double-counted under an ancestor's).
    """
    blocked = set(blocked_ids)
    children: dict[str, list[str]] = {}
    for it in items:
        dep = it.get("depends_on")
        iid = it.get("id")
        if dep and iid:
            children.setdefault(dep, []).append(iid)

    result: dict[str, list[str]] = {}
    for bid in blocked:
        seen: set[str] = set()
        stack = list(children.get(bid, []))
        while stack:
            cur = stack.pop()
            if cur in seen or cur in blocked:
                continue
            seen.add(cur)
            stack.extend(children.get(cur, []))
        result[bid] = sorted(seen)
    return result


# ---------------------------------------------------------------------------
# Whole-run evaluation
# ---------------------------------------------------------------------------

def evaluate_board_blockers(
    items: "list[dict[str, Any]]",
    classifications: "dict[str, dict[str, Any]]",
    *,
    policy: "str | None" = DEFAULT_POLICY,
) -> dict[str, Any]:
    """Combine per-item :func:`classify_item_blocker` results into one
    whole-run decision.

    ``items`` is the full live (non-done) board -- used only for dependency-
    closure lookups (:func:`compute_dependent_closure`). ``classifications``
    maps item id -> the dict :func:`classify_item_blocker` returned for it
    (items with ``kind=None`` are simply not blocked and contribute nothing).

    Run-stop rule: a fail-closed classification (``verified_security`` /
    ``integrity_corruption`` / ``run_global_blocker``) ALWAYS halts the run,
    regardless of ``policy``. Independently, an explicit
    ``policy="run_stop"`` halts the run for ANY blocked item, even a
    quarantine-safe one -- that is what makes ``run_stop`` a deliberately
    stricter, opt-in admin policy rather than the default.

    Returns a dict with: ``policy``, ``blocked_item_ids``,
    ``classifications`` (id -> kind), ``evidence_status`` (id -> evidence
    dict), ``fail_closed_item_ids``, ``skipped_dependents`` (id -> dependent
    ids paused because that id is blocked), ``quarantined_item_ids`` (same
    as ``blocked_item_ids`` -- kept as a distinct, explicitly-named field
    since that's the state a board/dashboard cares about), ``eligible_item_ids``
    (every other live item id -- empty when ``run_stop`` is True),
    ``run_stop``, ``run_stop_reason``, and a human-readable
    ``continuation_rationale``.
    """
    policy = normalize_policy(policy)
    blocked = {
        iid: c for iid, c in (classifications or {}).items() if c and c.get("kind")
    }
    blocked_ids = sorted(blocked)

    fail_closed_ids = sorted(iid for iid, c in blocked.items() if c.get("fail_closed"))

    run_stop = False
    run_stop_reason: "str | None" = None
    if fail_closed_ids:
        run_stop = True
        kinds = sorted({blocked[i]["kind"] for i in fail_closed_ids})
        run_stop_reason = (
            "fail_closed_blocker:" + ",".join(kinds)
            + ";items:" + ",".join(fail_closed_ids)
        )
    elif policy == POLICY_RUN_STOP and blocked_ids:
        run_stop = True
        run_stop_reason = "explicit_project_run_stop_policy"

    dependents = compute_dependent_closure(items, blocked_ids)
    all_skipped = sorted({d for ids in dependents.values() for d in ids})

    all_ids = {it.get("id") for it in items if it.get("id")}
    if run_stop:
        eligible_ids: list[str] = []
    else:
        eligible_ids = sorted(all_ids - set(blocked_ids) - set(all_skipped))

    if run_stop:
        rationale = (
            f"Run halted ({run_stop_reason}): {len(blocked_ids)} item(s) blocked, "
            f"{len(all_skipped)} dependent(s) paused."
        )
    elif blocked_ids:
        detail = ", ".join(f"{iid}:{blocked[iid]['kind']}" for iid in blocked_ids)
        rationale = (
            f"policy={policy}: quarantined {len(blocked_ids)} item(s) ({detail}); "
            f"{len(all_skipped)} dependent(s) paused; "
            f"{len(eligible_ids)} item(s) remain eligible and continue."
        )
    else:
        rationale = "no blocked items; full board eligible."

    return {
        "policy": policy,
        "blocked_item_ids": blocked_ids,
        "classifications": {iid: blocked[iid]["kind"] for iid in blocked_ids},
        "evidence_status": {iid: blocked[iid]["evidence"] for iid in blocked_ids},
        "fail_closed_item_ids": fail_closed_ids,
        "skipped_dependents": dependents,
        "quarantined_item_ids": blocked_ids,
        "eligible_item_ids": eligible_ids,
        "run_stop": run_stop,
        "run_stop_reason": run_stop_reason,
        "continuation_rationale": rationale,
    }


def classify_and_evaluate(
    items: "list[dict[str, Any]]",
    *,
    signals: "dict[str, dict[str, Any]] | None" = None,
    policy: "str | None" = DEFAULT_POLICY,
) -> dict[str, Any]:
    """High-level entry point: classify every item in ``items`` (using the
    per-item override ``signals`` when given) and evaluate the whole-run
    decision in one call.

    ``signals`` maps item id -> a dict of :func:`classify_item_blocker`
    keyword overrides (``prospected``, ``tool_unavailable``,
    ``has_approved_fallback``, ``dependency_blocked``,
    ``require_human_review``, ``verified_security``, ``integrity_corruption``,
    ``run_global_blocker``). An item with no entry classifies purely from its
    own data (evidence-based ``needs_scope``/``needs_prospecting`` checks
    only -- never a fail-closed or dependency/tool kind, since those require
    an explicit signal by design). This keeps the default, no-signals call
    (the common case: a caller just wants "which items are under-scoped")
    idempotent and side-effect-free -- see the module docstring's design
    note on why an all-empty-evidence item is a low-false-positive bar.
    """
    signals = signals or {}
    classifications: dict[str, dict[str, Any]] = {}
    for it in items:
        iid = it.get("id")
        if not iid:
            continue
        sig = signals.get(iid) or {}
        classifications[iid] = classify_item_blocker(
            it,
            prospected=sig.get("prospected"),
            tool_unavailable=bool(sig.get("tool_unavailable")),
            has_approved_fallback=sig.get("has_approved_fallback", True),
            dependency_blocked=bool(sig.get("dependency_blocked")),
            require_human_review=sig.get("require_human_review"),
            verified_security=bool(sig.get("verified_security")),
            integrity_corruption=bool(sig.get("integrity_corruption")),
            run_global_blocker=bool(sig.get("run_global_blocker")),
        )
    return evaluate_board_blockers(items, classifications, policy=policy)
