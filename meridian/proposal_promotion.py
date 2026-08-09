"""ce4883f3 — configurable proposal-to-handoff planner workflow.

Orchestration layer over the EXISTING proposal lifecycle in
``meridian.db.workspace`` (``add_workspace_proposal`` /
``append_proposal_update`` / ``advance_workspace_proposal_status`` /
``promote_workspace_proposal``), the EXISTING wave/parallel-safety
computation in ``meridian.db.sprint_items.get_parallelizable_groups``, the
EXISTING pointer validator in ``meridian.pointers.validate_pointer``, the
EXISTING HITL mechanism in ``meridian.db.request_hitl``, and the EXISTING
handoff renderer in ``meridian.handoff.generate_handoff``. This module does
NOT reimplement any of those — it adds a DEPTH concept (how far to promote a
proposal in one call), a PREVIEW/COMMIT split (compute what would happen,
then commit only if the preview is still fresh), and DEVIATION/HITL routing
on top.

Standard proposal-to-execution contract (the item's own 8-part list):
  1. intake/source and scope             -> the proposal row itself (title,
                                             body, tags, source/actor on its
                                             'created' event).
  2. investigation findings + unresolved
     assumptions                          -> ``proposal_events`` rows with
                                             ``event_type='investigation_finding'``.
  3. exact code/docs/output pointers,
     existing vs planned_new              -> ``proposal_events`` rows with
                                             ``event_type='pointer_recorded'``,
                                             each validated via
                                             :func:`meridian.pointers.validate_pointer`.
  4. executor-ready sprint items          -> ``promote_workspace_proposal``.
  5. symbol/resource ownership + dynamic
     dependency frontier                  -> the created sprint item's
                                             ``touches_resources`` plus the
                                             wave preview below.
  6. wave gates / parallel-safe grouping  -> :func:`get_parallelizable_groups`
                                             (called UNMODIFIED) plus a local
                                             set-intersection of the
                                             prospective item's resources
                                             against each existing group.
  7. tools/fallbacks/tests/evidence/
     deploy+rollback criteria             -> OUT OF SCOPE for this module:
                                             these are sprint-item-authoring
                                             concerns (``update_sprint_item``'s
                                             ``tool_requirements``/
                                             ``planned_output``/test fields),
                                             not something proposal promotion
                                             establishes. Always reported as
                                             ``not_applicable`` in
                                             ``contract_status`` — an honest
                                             signal, not a silent claim of
                                             coverage this module doesn't
                                             provide.
  8. deviation block                      -> :data:`StalePreviewError` +
                                             ``deviation_auto_resolved`` /
                                             ``deviation_override`` /
                                             ``deviation_hitl_filed`` events
                                             recorded via
                                             ``append_proposal_update``, and
                                             the ``deviation``/``hitl_pending``
                                             fields on :func:`commit_proposal_promotion`'s
                                             return value.

Depths (:data:`PROMOTION_DEPTHS`) are cumulative — requesting a depth commits
every shallower depth's effects too:

    proposal -> investigation -> pointers -> sprint_items -> executable_handoff

Preview/commit hash contract (mirrors ``profile_contract._compute_generation_key``
/ ``db.profile_layers._content_hash``): :func:`preview_proposal_promotion`
returns a ``preview_hash`` — sha256 of the canonical JSON (sorted keys,
``(",", ":")`` separators) of the preview's content fields, EXCLUDING the
volatile ``computed_at`` timestamp and the hash itself.
:func:`commit_proposal_promotion` re-computes a FRESH preview at commit time
and requires the caller-supplied hash to match it exactly — a stale preview
(the proposal or the target project's pending board changed in between) is
rejected with :class:`StalePreviewError` rather than silently committed
against outdated information.

HITL routing (6 categories named verbatim by the item, in
:data:`_HITL_DEVIATION_CATEGORIES`): this module implements ONE narrow,
documented heuristic covering 3 of the 6 categories, run once a sprint item
exists (so its FINAL touches_resources/title/body are known):

  * ``production_deployment``   — the sprint item's touches_resources include
    a path matching a small deploy/production-infra keyword list (CI
    workflow files, ``fly.toml``, ``Dockerfile``, the live Postgres adapter).
  * ``tenant_security_boundary`` — touches_resources include a path matching
    a small auth/secret/credential/tenant/billing keyword list.
  * ``destructive_behavior``    — the proposal's own title+body match this
    codebase's EXISTING ``_HITL_DESTRUCTIVE_KEYWORDS`` list (reused from
    ``meridian.db``, not reinvented).

The remaining 3 categories (``scope``, ``ownership``, ``unresolved_assumption``)
have NO automated signal here — they require either an explicit caller-
supplied flag or human judgment that this module cannot infer purely from a
proposal's title/body/resources, and are a documented gap rather than a
guessed heuristic. A caller with an explicit non-empty ``override_reason``
bypasses a triggered HITL (audited via a ``deviation_override`` event) —
same acknowledge-and-proceed pattern as ``override_strict_evidence`` /
``override_code_intel_receipt`` elsewhere in this codebase.

Anything NOT in ``_HITL_DEVIATION_CATEGORIES`` — a normal retry, a lost race
against another caller (caught by the underlying race-safe
``advance_workspace_proposal_status``/``promote_workspace_proposal``/
``generate_handoff`` calls, which raise ``ValueError`` on a lost race), or a
tool fallback within the declared contract — gets an auditable
``deviation_auto_resolved`` event and a HONEST failure report (this commit
did NOT succeed as requested); it never silently retries or fabricates
success.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from typing import Any

from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import pointers as pointers_module
from meridian.db import workspace as workspace_db

# ---------------------------------------------------------------------------
# Depths
# ---------------------------------------------------------------------------

PROMOTION_DEPTHS: tuple[str, ...] = (
    "proposal",
    "investigation",
    "pointers",
    "sprint_items",
    "executable_handoff",
)
_DEPTH_RANK: dict[str, int] = {name: rank for rank, name in enumerate(PROMOTION_DEPTHS)}

# Conservative status -> minimum-satisfied-depth-rank mapping. Only 'raw',
# 'investigating', and 'promoted' get a nonzero rank: 'paused'/'rejected'/
# 'closed'/'superseded' fall back to rank 0 (NOT assumed to have reached
# investigation) because each is reachable directly from 'raw' too (see
# ``workspace._PROPOSAL_TRANSITIONS``) — under-claiming "already satisfied"
# is the safe direction; it just means commit proceeds and re-evaluates the
# real per-step conditions (e.g. the investigation transition step only
# fires ``if status == "raw"``, exactly mirroring promote's own guard).
_STATUS_MIN_DEPTH_RANK: dict[str, int] = {
    "raw": _DEPTH_RANK["proposal"],
    "investigating": _DEPTH_RANK["investigation"],
    "promoted": _DEPTH_RANK["sprint_items"],
}

# ---------------------------------------------------------------------------
# HITL deviation categories (verbatim from the item's own list)
# ---------------------------------------------------------------------------

_HITL_DEVIATION_CATEGORIES: frozenset[str] = frozenset({
    "scope",
    "ownership",
    "destructive_behavior",
    "tenant_security_boundary",
    "production_deployment",
    "unresolved_assumption",
})

# Anything outside the set above (normal retries, stale-claim reconciliation
# with objective evidence, tool fallback within the declared contract) is
# handled as an auditable warning with no HITL — see _record_race_deviation.
_NO_HITL_DEVIATION_CATEGORY = "race_retry"

# Narrow, documented resource-path keyword heuristics (production_deployment
# checked first since it is the more specific/severe signal — a deploy file
# that also happens to contain "auth" in its path, e.g. none realistically
# do, would still classify as production_deployment first).
_PRODUCTION_DEPLOY_RESOURCE_KEYWORDS: tuple[str, ...] = (
    "deploy.yml", "deploy.yaml", ".github/workflows", "fly.toml",
    "dockerfile", "pg_adapter.py",
)
_SECURITY_RESOURCE_KEYWORDS: tuple[str, ...] = (
    "auth", "security", "secret", "credential", "password", "token",
    "oauth", "tenant", "billing", "stripe", "encrypt", "permission",
)


class StalePreviewError(ValueError):
    """commit_proposal_promotion's caller-supplied preview_hash no longer
    matches a freshly recomputed preview for the same (proposal_id,
    project_id, depth, ...) arguments — the proposal or the target project's
    pending board changed between preview and commit. Re-preview and retry;
    nothing was written by the call that raised this."""


def _validate_depth(depth: Any) -> int:
    if depth not in _DEPTH_RANK:
        raise ValueError(
            f"Unknown promotion depth {depth!r}. Valid depths (shallow to "
            f"deep, each including every shallower depth's effects): "
            f"{', '.join(PROMOTION_DEPTHS)}"
        )
    return _DEPTH_RANK[depth]


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


async def _load_proposal(
    db: Any, proposal_id: str, tenant_id: str | None,
) -> dict[str, Any] | None:
    """Tenant-scoped single-proposal lookup — same scoping rule as every
    other workspace-proposal reader/writer (see ``workspace._ws_tenant_clause``,
    reused here via its ``meridian.db`` re-export rather than duplicated)."""
    scope, scope_params = db_module._ws_tenant_clause(tenant_id)
    scope_sql = f" AND {scope}" if scope else ""
    async with db.execute(
        f"SELECT * FROM workspace_proposals WHERE id = ?{scope_sql}",
        [proposal_id, *scope_params],
    ) as cur:
        row = await cur.fetchone()
    return db_module._row_to_dict(row) if row is not None else None


async def _load_proposal_events(db: Any, proposal_id: str) -> list[dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM proposal_events WHERE proposal_id = ? ORDER BY sequence ASC",
        (proposal_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [db_module._row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def _compute_contract_status(
    proposal: dict[str, Any], events: list[dict[str, Any]], requested_rank: int,
) -> dict[str, str]:
    """The 8-part contract, per-part status at the REQUESTED depth.

    Vocabulary:
      "present"           — already recorded/exists, nothing to do.
      "would_create"       — committing this depth WILL unconditionally
                              establish this part.
      "optional_at_commit" — applicable at this depth, not yet present, and
                              only created if the caller supplies the
                              relevant optional content at commit time
                              (investigation_findings / pointers — preview
                              does not accept either, so it cannot promise
                              these will be created, only that they COULD
                              be).
      "not_applicable"     — the requested depth doesn't reach this part, or
                              (tools/tests/evidence/rollback, deviation
                              block) this module never establishes it at
                              all — see the module docstring.
    """
    event_types = {e.get("event_type") for e in events}
    status = proposal.get("status") or "raw"

    def _optional(min_rank: int, present: bool) -> str:
        if requested_rank < min_rank:
            return "not_applicable"
        return "present" if present else "optional_at_commit"

    def _guaranteed(min_rank: int, present: bool) -> str:
        if requested_rank < min_rank:
            return "not_applicable"
        return "present" if present else "would_create"

    return {
        "intake_source_scope": "present",
        "investigation_findings": _optional(
            _DEPTH_RANK["investigation"], "investigation_finding" in event_types
        ),
        "pointers": _optional(
            _DEPTH_RANK["pointers"], "pointer_recorded" in event_types
        ),
        "sprint_items": _guaranteed(_DEPTH_RANK["sprint_items"], status == "promoted"),
        "ownership_dependency_frontier": _guaranteed(
            _DEPTH_RANK["sprint_items"], status == "promoted"
        ),
        "wave_gates": (
            "present" if requested_rank >= _DEPTH_RANK["sprint_items"] else "not_applicable"
        ),
        "tools_tests_evidence_rollback": "not_applicable",
        "deviation_block": "not_applicable",
    }


def _compute_wave_preview(
    groups_result: dict[str, Any], resource_candidates: list[str],
) -> dict[str, Any]:
    """Layered on top of the REAL, unmodified :func:`get_parallelizable_groups`
    result for the target project/version: set-intersect the prospective
    item's resources against each existing group's members. Never mutates or
    re-derives ``groups_result`` itself."""
    resources_set = set(resource_candidates or [])
    groups = groups_result.get("groups") or []
    if not resources_set:
        return {
            "declared_resources": [],
            "would_join": "new_singleton_group",
            "conflicts": [],
            "group_count": len(groups),
            "note": (
                "No touches_resources declared/inferred — mirrors "
                "get_parallelizable_groups' own undeclared-item handling: "
                "parallel safety can't be proven, so it would get its own "
                "singleton group rather than joining an existing one."
            ),
        }
    conflicts: list[dict[str, Any]] = []
    join_index: int | None = None
    for idx, group in enumerate(groups):
        conflicting_items: list[str] = []
        for item in group:
            item_resources = set(item.get("resources") or [])
            if item_resources & resources_set:
                conflicting_items.append(item.get("id"))
        if conflicting_items:
            conflicts.append({"group_index": idx, "conflicting_item_ids": conflicting_items})
        elif join_index is None:
            join_index = idx
    would_join = f"group_{join_index}" if join_index is not None else "new_trailing_group"
    return {
        "declared_resources": sorted(resources_set),
        "would_join": would_join,
        "conflicts": conflicts,
        "group_count": len(groups),
    }


def _hashable_preview(preview: dict[str, Any]) -> dict[str, Any]:
    """Stable subset used for the preview_hash — excludes the volatile
    ``computed_at`` timestamp and the hash field itself so a content-identical
    re-preview always hashes identically."""
    return {k: v for k, v in preview.items() if k not in ("preview_hash", "computed_at")}


def _compute_preview_hash(payload: dict[str, Any]) -> str:
    """Same convention as profile_contract._compute_generation_key /
    db.profile_layers._content_hash: canonical JSON (sorted keys, compact
    separators), sha256, ``sha256:`` prefix."""
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def preview_proposal_promotion(
    db: Any,
    proposal_id: str,
    project_id: str,
    depth: str,
    *,
    tenant_id: str | None = None,
    sprint_item_title: str | None = None,
    sprint_item_version: str | None = None,
    touches_resources: list[str] | None = None,
    infer_touches_resources: bool = True,
) -> dict[str, Any]:
    """Read-only: compute what :func:`commit_proposal_promotion` would do for
    ``depth``, without writing anything. Raises ``ValueError`` if the
    proposal is not found (or not visible under ``tenant_id``'s scope)."""
    requested_rank = _validate_depth(depth)
    proposal = await _load_proposal(db, proposal_id, tenant_id)
    if proposal is None:
        raise ValueError(f"Proposal '{proposal_id}' not found")

    status = proposal.get("status") or "raw"
    status_rank = _STATUS_MIN_DEPTH_RANK.get(status, 0)
    already_satisfied = status_rank >= requested_rank

    if already_satisfied:
        preview: dict[str, Any] = {
            "proposal_id": proposal_id,
            "project_id": project_id,
            "depth": depth,
            "already_satisfied": True,
            "reason": (
                f"Proposal '{proposal_id}' is already at status={status!r}, "
                f"which satisfies depth={depth!r} (rank {status_rank} >= "
                f"{requested_rank}). No further computation needed."
            ),
            "contract_status": None,
            "would_create": None,
            "wave_preview": None,
        }
        preview["preview_hash"] = _compute_preview_hash(_hashable_preview(preview))
        preview["computed_at"] = _utcnow_iso()
        return preview

    events = await _load_proposal_events(db, proposal_id)
    contract_status = _compute_contract_status(proposal, events, requested_rank)

    would_create: dict[str, Any] = {}
    if requested_rank >= _DEPTH_RANK["investigation"] and status == "raw":
        would_create["status_transition"] = {"from": "raw", "to": "investigating"}

    wave_preview: dict[str, Any] | None = None
    if requested_rank >= _DEPTH_RANK["sprint_items"]:
        title = sprint_item_title or proposal.get("title") or ""
        version = sprint_item_version or "current"
        proposal_body = proposal.get("body") or ""
        resource_candidates = touches_resources
        if resource_candidates is None and infer_touches_resources:
            resource_candidates = workspace_db._infer_touches_resources_from_proposal(
                title, proposal_body
            )
        resource_candidates = list(resource_candidates or [])
        would_create["sprint_item"] = {
            "title": title,
            "project_id": project_id,
            "version": version,
            "touches_resources": resource_candidates,
            "resource_scope_unset": not resource_candidates,
        }

        project = await db_module.get_project(db, project_id)
        if project is None:
            raise ValueError(f"Project '{project_id}' not found")

        groups_result = await db_module.get_parallelizable_groups(db, project_id, version)
        wave_preview = _compute_wave_preview(groups_result, resource_candidates)

    if requested_rank >= _DEPTH_RANK["executable_handoff"]:
        would_create["handoff"] = {
            "note": (
                "A handoff scoped to the newly-created (or reused) sprint "
                "item via generate_handoff(selected_item_ids=[...]) would be "
                "generated at commit time. Preview cannot render its exact "
                "content without the sprint item existing first."
            ),
        }

    preview = {
        "proposal_id": proposal_id,
        "project_id": project_id,
        "depth": depth,
        "already_satisfied": False,
        "contract_status": contract_status,
        "would_create": would_create,
        "wave_preview": wave_preview,
    }
    preview["preview_hash"] = _compute_preview_hash(_hashable_preview(preview))
    preview["computed_at"] = _utcnow_iso()
    return preview


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


def _classify_deviation(
    proposal_title: str, proposal_body: str, resources: list[str],
) -> str | None:
    """The one narrow, documented heuristic this module implements — see the
    module docstring's HITL routing section for exactly what it covers and
    what it deliberately does not."""
    lowered_resources = [r.lower() for r in resources]
    for r in lowered_resources:
        if any(kw in r for kw in _PRODUCTION_DEPLOY_RESOURCE_KEYWORDS):
            return "production_deployment"
    for r in lowered_resources:
        if any(kw in r for kw in _SECURITY_RESOURCE_KEYWORDS):
            return "tenant_security_boundary"
    combined_text = f"{proposal_title} {proposal_body}".lower()
    if any(kw in combined_text for kw in db_module._HITL_DESTRUCTIVE_KEYWORDS):
        return "destructive_behavior"
    return None


async def _record_race_deviation(
    db: Any,
    proposal_id: str,
    project_id: str,
    depth: str,
    tenant_id: str | None,
    actor: str | None,
    *,
    step: str,
    exc: Exception,
) -> dict[str, Any]:
    """A concurrent caller changed the proposal's state between our fresh-hash
    preview and this write step (or a downstream handoff-scope check
    rejected the selection) — the underlying race-safe function already
    caught it and raised. Per the item's own contract this is a normal
    retry/stale-claim category: NO HITL, just a durable auditable warning —
    and an honest failure report, since this commit did NOT succeed as
    requested."""
    try:
        await workspace_db.append_proposal_update(
            db, proposal_id,
            f"commit_proposal_promotion lost a race at step={step}: {exc}",
            event_type="deviation_auto_resolved",
            payload={
                "step": step, "depth": depth,
                "reason": str(exc), "category": _NO_HITL_DEVIATION_CATEGORY,
            },
            actor=actor, tenant_id=tenant_id,
        )
    except Exception:  # noqa: BLE001 — never mask the original race error
        pass
    return {
        "proposal_id": proposal_id,
        "project_id": project_id,
        "depth": depth,
        "already_satisfied": False,
        "committed": {},
        "deviation": {
            "category": _NO_HITL_DEVIATION_CATEGORY,
            "step": step,
            "message": str(exc),
            "hitl_required": False,
        },
        "hitl_pending": False,
        "hitl_request_id": None,
        "error": str(exc),
    }


async def _maybe_file_hitl_for_deviation(
    db: Any,
    project_id: str,
    proposal: dict[str, Any],
    sprint_item_id: str | None,
    committed: dict[str, Any],
    tenant_id: str | None,
    actor: str | None,
    depth: str,
    override_reason: str | None,
) -> dict[str, Any] | None:
    """Runs once the sprint item exists (so its FINAL touches_resources are
    known). Returns a hitl_pending=True result dict (remaining steps NOT
    executed) when the heuristic fires and no override was given; returns
    None (proceed normally) otherwise."""
    raw_resources = (committed.get("sprint_item") or {}).get("touches_resources")
    resources = db_module.parse_touches_resources(raw_resources)
    category = _classify_deviation(
        proposal.get("title") or "", proposal.get("body") or "", resources,
    )
    if category is None:
        return None
    proposal_id = proposal.get("id")

    if override_reason and override_reason.strip():
        try:
            await workspace_db.append_proposal_update(
                db, proposal_id,
                f"Deviation ({category}) overridden: {override_reason.strip()}",
                event_type="deviation_override",
                payload={
                    "category": category, "resources": resources,
                    "override_reason": override_reason.strip(), "depth": depth,
                },
                actor=actor, tenant_id=tenant_id,
            )
        except Exception:  # noqa: BLE001 — an audit-logging failure must
            pass            # never block the override it's merely recording.
        return None

    question = (
        f"Proposal '{proposal_id}' was promoted to sprint item "
        f"'{sprint_item_id}' whose touches_resources include "
        f"{category.replace('_', ' ')}-flagged path(s): {', '.join(resources)}. "
        f"This matches a HITL-triggering deviation category ({category}) per "
        "the proposal-promotion contract — confirm before any further "
        "automated promotion steps proceed."
    )
    hitl = await db_module.request_hitl(
        db, project_id,
        question=question,
        context=(
            f"proposal={proposal_id} sprint_item={sprint_item_id} "
            f"depth={depth} resources={resources}"
        ),
        kind="proposal_deviation",
        payload=json.dumps({
            "proposal_id": proposal_id, "sprint_item_id": sprint_item_id,
            "category": category, "resources": resources, "depth": depth,
        }),
        urgency="normal",
        # e43e6941 — require_human: the 3 categories this heuristic detects
        # are exactly the kind of "genuinely irreversible/destructive" cases
        # that convention reserves require_human for; auto-answer must never
        # wave through a tenant/security/production/destructive deviation.
        require_human=True,
    )
    try:
        await workspace_db.append_proposal_update(
            db, proposal_id,
            f"HITL filed for {category} deviation on promotion "
            f"(hitl_id={hitl.get('id')})",
            event_type="deviation_hitl_filed",
            payload={"category": category, "hitl_id": hitl.get("id"), "resources": resources},
            actor=actor, tenant_id=tenant_id,
        )
    except Exception:  # noqa: BLE001 — never mask the HITL result over an
        pass            # audit-logging failure.
    return {
        "proposal_id": proposal_id,
        "project_id": project_id,
        "depth": depth,
        "already_satisfied": False,
        "committed": committed,
        "deviation": {
            "category": category,
            "resources": resources,
            "hitl_required": True,
        },
        "hitl_pending": True,
        "hitl_request_id": hitl.get("id"),
    }


async def commit_proposal_promotion(
    db: Any,
    proposal_id: str,
    project_id: str,
    depth: str,
    preview_hash: str,
    *,
    tenant_id: str | None = None,
    actor: str | None = None,
    session_id: str | None = None,
    sprint_item_title: str | None = None,
    sprint_item_version: str | None = None,
    touches_resources: list[str] | None = None,
    infer_touches_resources: bool = True,
    investigation_findings: str | None = None,
    pointers: list[dict[str, Any]] | None = None,
    data_dir: str | None = None,
    override_reason: str | None = None,
) -> dict[str, Any]:
    """Commit a proposal's promotion through ``depth``, cumulative over every
    shallower depth. Requires a ``preview_hash`` from a just-computed
    :func:`preview_proposal_promotion` call with the SAME arguments — a
    mismatch (the proposal or target project state changed since) raises
    :class:`StalePreviewError` and writes nothing.

    ``investigation_findings``/``pointers`` supplied alongside an
    already-satisfied depth are NOT recorded (the call returns the
    idempotent no-op early) — call ``append_proposal_update`` directly, or
    request the specific depth that is not yet satisfied, to add
    supplementary content to an already-promoted/-investigating proposal.

    ``data_dir`` is required only for ``depth='executable_handoff'`` — it is
    ``generate_handoff``'s ``output_dir`` positional argument (the handoff
    render/persistence directory); pass the caller's own ``data_dir``.
    """
    requested_rank = _validate_depth(depth)

    fresh_preview = await preview_proposal_promotion(
        db, proposal_id, project_id, depth,
        tenant_id=tenant_id,
        sprint_item_title=sprint_item_title,
        sprint_item_version=sprint_item_version,
        touches_resources=touches_resources,
        infer_touches_resources=infer_touches_resources,
    )

    if fresh_preview["already_satisfied"]:
        return {
            "proposal_id": proposal_id,
            "project_id": project_id,
            "depth": depth,
            "already_satisfied": True,
            "committed": {},
            "deviation": None,
            "hitl_pending": False,
            "hitl_request_id": None,
            "message": fresh_preview.get("reason"),
        }

    if fresh_preview["preview_hash"] != preview_hash:
        raise StalePreviewError(
            f"preview_hash mismatch for proposal '{proposal_id}' at depth "
            f"'{depth}': the proposal or the target project's pending board "
            "changed since this preview was computed. Call "
            "preview_proposal_promotion again and retry commit with the "
            "fresh hash. Nothing was written."
        )

    # Validate pointer shapes BEFORE any write — fail closed on malformed
    # input rather than leaving a partial promotion behind.
    validated_pointers: list[dict[str, Any]] | None = None
    if pointers:
        validated_pointers = [pointers_module.validate_pointer(p) for p in pointers]

    proposal = await _load_proposal(db, proposal_id, tenant_id)
    if proposal is None:
        raise ValueError(f"Proposal '{proposal_id}' not found")
    status = proposal.get("status") or "raw"

    committed: dict[str, Any] = {}

    # --- depth >= investigation ---------------------------------------
    if requested_rank >= _DEPTH_RANK["investigation"]:
        if status == "raw":
            try:
                updated = await workspace_db.advance_workspace_proposal_status(
                    db, proposal_id, "investigating", tenant_id,
                )
            except ValueError as exc:
                return await _record_race_deviation(
                    db, proposal_id, project_id, depth, tenant_id, actor,
                    step="advance_to_investigating", exc=exc,
                )
            if updated is None:
                raise ValueError(f"Proposal '{proposal_id}' not found")
            status = updated.get("status") or status
            committed["status_transition"] = {"from": "raw", "to": status}
        if investigation_findings:
            event = await workspace_db.append_proposal_update(
                db, proposal_id, investigation_findings,
                event_type="investigation_finding",
                actor=actor, session_id=session_id, tenant_id=tenant_id,
            )
            committed["investigation_finding_event_id"] = (event or {}).get("id")

    # --- depth >= pointers -----------------------------------------------
    if requested_rank >= _DEPTH_RANK["pointers"] and validated_pointers:
        recorded_pointer_ids: list[str] = []
        for vp in validated_pointers:
            event = await workspace_db.append_proposal_update(
                db, proposal_id, json.dumps(vp, sort_keys=True),
                event_type="pointer_recorded",
                payload=vp, actor=actor, session_id=session_id, tenant_id=tenant_id,
            )
            if event:
                recorded_pointer_ids.append(event.get("id"))
        committed["pointer_event_ids"] = recorded_pointer_ids

    # --- depth >= sprint_items ---------------------------------------
    sprint_item_id: str | None = None
    if requested_rank >= _DEPTH_RANK["sprint_items"]:
        if status == "promoted":
            sprint_item_id = proposal.get("promoted_to_sprint_item_id")
            committed["sprint_item"] = {"id": sprint_item_id, "reused_existing": True}
        else:
            try:
                promo_result = await workspace_db.promote_workspace_proposal(
                    db, proposal_id, project_id,
                    sprint_item_title=sprint_item_title,
                    sprint_item_version=sprint_item_version,
                    tenant_id=tenant_id,
                    touches_resources=touches_resources,
                    infer_touches_resources=infer_touches_resources,
                )
            except ValueError as exc:
                return await _record_race_deviation(
                    db, proposal_id, project_id, depth, tenant_id, actor,
                    step="promote_to_sprint_item", exc=exc,
                )
            sprint_item_id = promo_result.get("sprint_item_id")
            committed["sprint_item"] = {
                "id": sprint_item_id,
                "title": promo_result.get("sprint_item_title"),
                "touches_resources": promo_result.get("sprint_item_touches_resources"),
                "reused_existing": False,
            }
            status = "promoted"

        hitl_result = await _maybe_file_hitl_for_deviation(
            db, project_id, proposal, sprint_item_id, committed,
            tenant_id, actor, depth, override_reason,
        )
        if hitl_result is not None:
            return hitl_result

    # --- depth == executable_handoff ----------------------------------
    if requested_rank >= _DEPTH_RANK["executable_handoff"]:
        if not sprint_item_id:
            raise ValueError(
                "executable_handoff depth requires a sprint item id; the "
                "sprint_items step above must have produced or reused one."
            )
        if not data_dir:
            raise ValueError(
                "commit_proposal_promotion(depth='executable_handoff') "
                "requires data_dir (generate_handoff's output_dir argument) "
                "— pass the caller's own data_dir through."
            )
        try:
            path, content, amended = await handoff_module.generate_handoff(
                db, project_id, data_dir,
                session_id=session_id,
                selected_item_ids=[sprint_item_id],
                skip_ai_summary=True,
            )
        except (
            handoff_module.HandoffSelectionError,
            handoff_module.HandoffScopeNonExecutable,
            handoff_module.HandoffEvidenceRequired,
        ) as exc:
            return await _record_race_deviation(
                db, proposal_id, project_id, depth, tenant_id, actor,
                step="generate_handoff", exc=exc,
            )
        committed["handoff"] = {
            "path": path, "amended": amended, "content_length": len(content or ""),
        }

    return {
        "proposal_id": proposal_id,
        "project_id": project_id,
        "depth": depth,
        "already_satisfied": False,
        "committed": committed,
        "deviation": None,
        "hitl_pending": False,
        "hitl_request_id": None,
    }
