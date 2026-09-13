"""proposal_to_handoff orchestration command (73499c59).

One MCP-callable command that takes an existing workspace/project proposal
and, in a single call, decomposes it into real sprint items, attaches a
durable pointer from each created item back to the source proposal, and
returns a typed :class:`ProposalRunReceipt` recording exactly what happened
-- then (unless the caller opts out) feeds the newly created items straight
into :func:`meridian.handoff.generate_handoff` so the resulting handoff scopes
itself to this proposal-run.

Call template (this item's own declared ``tool_requirements``):

    proposal_to_handoff -> add_sprint_item -> add_sprint_item_pointer -> generate_handoff

This module is deliberately a thin ORCHESTRATOR, not a reimplementation of
any of the primitives it calls:

* Each entry in the caller-supplied ``items`` list becomes one real sprint
  item via :func:`meridian.db.sprint_items.add_sprint_item` -- the SAME
  function ``add_sprint_item`` (the MCP tool) calls, including its existing
  60%-word-overlap duplicate guard, ``touches_resources``/``tool_requirements``/
  ``planned_output`` validation, and slug/nickname generation. A duplicate hit
  (``{"error": "duplicate", ...}``, ``add_sprint_item``'s own soft-reject shape
  -- see its docstring) is recorded on the receipt as a skipped item, never
  raised or silently dropped.
* Each successfully created item gets exactly one durable pointer back to the
  source proposal via :func:`meridian.db.sprint_items.add_sprint_item_pointer`
  (the SAME function ``add_sprint_item_pointer`` the MCP tool calls), reusing
  the existing generic pointer primitive in :mod:`meridian.pointers`. The
  pointer targets a ``node_id`` selector carrying the proposal's own id --
  this module does NOT add a new selector type to :mod:`meridian.pointers`
  (out of this item's declared ``touches_resources``); ``node_id`` is the
  existing selector shape closest to "reference an internal record by id",
  and the pointer's non-local ``uri`` scheme means the ``target_kind``
  filesystem check never applies to it (see :mod:`meridian.pointers` module
  docstring).
* The executable/non-executable determination and the live HITL-gate list on
  the receipt are a DIRECT passthrough of
  :func:`meridian.handoff.build_proposal_run_scope` -- the SAME unified
  proposal-run-scope contract every handoff mode (starter/goal/delta/full)
  already builds internally via :func:`meridian.handoff.generate_handoff`'s
  ``proposal_scope`` out-param. This module never recomputes or second-guesses
  that verdict; it only relays it, so a receipt's ``executable`` field can
  never drift from what the accompanying handoff itself declares.
* The one piece of NEW judgment this module adds is deciding whether the
  proposal-run as a whole should raise a HITL gate for a destructive/
  security/production-sensitive decomposition -- and even that reuses
  :func:`meridian.proposal_promotion._classify_deviation` (the existing,
  already-tested 3-category heuristic from the sibling proposal-promotion
  orchestrator, ce4883f3) rather than a second, potentially-diverging
  implementation of the same classification.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
from typing import Any

from meridian import db as db_module
from meridian import handoff as handoff_module

#: Re-run against a real workspace/project proposal's title+body+combined
#: touches_resources -- see module docstring. Imported, not reimplemented, so
#: this orchestrator and ce4883f3's commit_proposal_promotion can never
#: silently disagree about what counts as a HITL-worthy deviation.
from meridian.proposal_promotion import _classify_deviation  # noqa: PLC0415


@dataclasses.dataclass(frozen=True)
class ProposalRunReceipt:
    """Typed record of exactly what one ``proposal_to_handoff`` call did.

    Every id-bearing field is project-scoped (this call's own ``project_id``)
    -- there is no cross-project aggregation here, matching every other
    project-scoped tool in this codebase.

    ``executable`` / ``executable_reasons`` / ``hitl_gates`` are a verbatim
    passthrough of :func:`meridian.handoff.build_proposal_run_scope` (via
    ``generate_handoff``'s ``proposal_scope`` out-param) for the handoff this
    call generated -- see the module docstring. When no handoff was generated
    (``items`` produced zero real sprint items, or the caller passed
    ``skip_handoff=True``), ``executable`` is ``False`` and
    ``executable_reasons`` names why (``"no_items_created"`` /
    ``"handoff_skipped"``) rather than being silently absent.

    ``hitl_filed`` is the ONE HITL request this specific call raised (if the
    combined decomposition matched a deviation category and no
    ``override_reason`` was given) -- distinct from ``hitl_gates``, which is
    every LIVE pending HITL request the accompanying handoff surfaced
    (including, once it commits, this same one).
    """

    proposal_id: str
    project_id: str
    generated_at: str
    created_item_ids: list[str]
    skipped_items: list[dict[str, Any]]
    pointer_ids: dict[str, str]
    pointer_errors: dict[str, str]
    hitl_gates: list[dict[str, Any]]
    hitl_filed: "dict[str, Any] | None"
    deviation_category: "str | None"
    deviation_override_reason: "str | None"
    executable: bool
    executable_reasons: list[str]
    handoff_path: "str | None"
    handoff_content_length: "int | None"
    handoff_amended: "bool | None"
    handoff_error: "str | None"
    proposal_scope_hash: "str | None"

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form -- what the MCP handler actually returns."""
        return dataclasses.asdict(self)


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


async def _load_proposal(
    db: Any, proposal_id: str, tenant_id: "str | None",
) -> "dict[str, Any] | None":
    """Tenant-scoped single-proposal lookup.

    ``get_workspace_proposals`` (the public listing function) has no
    single-id filter, so this mirrors the exact same raw-SQL-plus-
    ``_ws_tenant_clause`` pattern :func:`meridian.proposal_promotion._load_proposal`
    already established for the identical need -- not a second business-logic
    path, just the same lookup idiom every tenant-scoped single-row read in
    this codebase uses.
    """
    scope, scope_params = db_module._ws_tenant_clause(tenant_id)
    scope_sql = f" AND {scope}" if scope else ""
    async with db.execute(
        f"SELECT * FROM workspace_proposals WHERE id = ?{scope_sql}",
        [proposal_id, *scope_params],
    ) as cur:
        row = await cur.fetchone()
    return db_module._row_to_dict(row) if row is not None else None


def _resolve_depends_on(
    raw: Any, created_ids_by_index: "list[str | None]",
) -> Any:
    """Resolve a ``depends_on`` spec value that references a SIBLING entry in
    THIS SAME ``items`` batch by its (already-processed) index, e.g.
    ``"$0"`` -> the real sprint-item id created for ``items[0]``.

    A plain id (anything not matching ``"$<int>"``) passes through unchanged
    -- ``add_sprint_item``'s own ``depends_on`` contract is untouched, this
    only substitutes a batch-local placeholder before delegating to it.
    Only BACKWARD references (an already-created earlier entry) resolve; an
    out-of-range, forward, or unparseable ``$``-reference is left as-is so it
    surfaces naturally (a non-existent depends_on id) rather than being
    silently swallowed.
    """
    if isinstance(raw, str) and raw.startswith("$"):
        try:
            idx = int(raw[1:])
        except ValueError:
            return raw
        if 0 <= idx < len(created_ids_by_index) and created_ids_by_index[idx]:
            return created_ids_by_index[idx]
        return raw
    return raw


async def proposal_to_handoff(
    db: Any,
    project_id: str,
    proposal_id: str,
    items: "list[dict[str, Any]]",
    output_dir: str,
    *,
    tenant_id: "str | None" = None,
    session_id: "str | None" = None,
    actor: "str | None" = None,
    version: "str | None" = None,
    mode: str = "goal",
    skip_handoff: bool = False,
    force: bool = False,
    override_reason: "str | None" = None,
) -> ProposalRunReceipt:
    """Decompose ``proposal_id`` into real sprint items and (by default)
    generate a handoff scoped to exactly those items.

    ``items`` -- a non-empty list of specs, each at minimum ``{"title": str}``
    plus any subset of :func:`meridian.db.sprint_items.add_sprint_item`'s own
    keyword arguments (``group``, ``notes``, ``touches_resources``,
    ``priority``, ``track``, ``wave``, ``tool_requirements``,
    ``planned_output``, ``artifact_policy``, ``depends_on``, ...). A spec's
    ``depends_on`` may reference an EARLIER entry in this same list via
    ``"$<index>"`` (see :func:`_resolve_depends_on`) instead of a real id, so
    a decomposition can declare its own internal dependency order in one
    call. Raises ``ValueError`` if ``items`` is empty, the proposal is not
    found (or not visible under ``tenant_id``'s scope), or ``project_id``
    does not resolve -- fails BEFORE any write, mirroring
    ``promote_workspace_proposal_with_children``'s own upfront validation.

    A per-entry failure (missing title, or ``add_sprint_item``'s own
    duplicate-title guard) is recorded in the receipt's ``skipped_items`` and
    does NOT abort the batch -- every other entry is still attempted, same
    "honest partial success" posture as this module's docstring describes.
    Once created, an item is never rolled back by a later step's failure
    (matching every other proposal-promotion helper in this codebase): a
    pointer-attach failure is recorded in ``pointer_errors`` and a
    handoff-generation failure is recorded in ``handoff_error``, but neither
    un-creates the sprint items already committed.

    ``version`` is the fallback sprint-version bucket for an item spec that
    doesn't declare its own; omitted entirely (no spec value, no ``version``
    argument) falls back to ``add_sprint_item``'s own default (``"current"``).

    ``mode``/``output_dir``/``session_id`` thread straight through to
    :func:`meridian.handoff.generate_handoff` (``output_dir`` is that
    function's positional ``output_dir`` -- pass the caller's own
    ``data_dir``). Set ``skip_handoff=True`` to create the items and pointers
    without generating a handoff for this call (the receipt still reports
    ``executable=False``, reason ``"handoff_skipped"``, rather than a
    default/omitted value).

    ``override_reason`` — a non-empty string acknowledges and bypasses a
    triggered HITL deviation gate (see :mod:`meridian.proposal_promotion`'s
    module docstring for the same acknowledge-and-proceed convention); the
    override is recorded as a durable ``deviation_override`` proposal event.
    """
    if not items:
        raise ValueError(
            "proposal_to_handoff requires at least one entry in `items` -- "
            "each becomes a real sprint item decomposed from the proposal. "
            "Call add_sprint_item directly for a single flat item with no "
            "proposal-run receipt."
        )

    proposal = await _load_proposal(db, proposal_id, tenant_id)
    if proposal is None:
        raise ValueError(f"Proposal '{proposal_id}' not found")

    project = await db_module.get_project(db, project_id)
    if project is None:
        raise ValueError(f"Project '{project_id}' not found")

    proposal_title = proposal.get("title") or ""
    proposal_body = proposal.get("body") or ""

    created_item_ids: list[str] = []
    created_ids_by_index: "list[str | None]" = [None] * len(items)
    skipped_items: list[dict[str, Any]] = []
    pointer_ids: dict[str, str] = {}
    pointer_errors: dict[str, str] = {}
    all_resources: list[str] = []

    for idx, spec in enumerate(items):
        if not isinstance(spec, dict):
            skipped_items.append({
                "index": idx, "reason": "invalid_spec",
                "message": f"items[{idx}] must be an object, got {type(spec).__name__}",
            })
            continue
        title = (spec.get("title") or "").strip()
        if not title:
            skipped_items.append({
                "index": idx, "reason": "missing_title",
                "message": f"items[{idx}] is missing a non-blank 'title'",
            })
            continue
        item_version = spec.get("version") or version or "current"
        try:
            result = await db_module.add_sprint_item(
                db, project_id, item_version, title,
                group=spec.get("group"),
                notes=spec.get("notes"),
                human_id=spec.get("human_id"),
                depends_on=_resolve_depends_on(spec.get("depends_on"), created_ids_by_index),
                failure_mode=spec.get("failure_mode"),
                milestone_type=spec.get("milestone_type", "task"),
                touches_resources=spec.get("touches_resources"),
                force=bool(spec.get("force", force)),
                track=spec.get("track"),
                priority=spec.get("priority"),
                wave=spec.get("wave"),
                sprint_name=spec.get("sprint_name"),
                required_tool=spec.get("required_tool"),
                tool_requirements=spec.get("tool_requirements"),
                artifact_kind=spec.get("artifact_kind"),
                planned_output=spec.get("planned_output"),
                artifact_policy=spec.get("artifact_policy"),
            )
        except ValueError as exc:
            # Malformed touches_resources/tool_requirements/planned_output/
            # priority/blocker_kind -- add_sprint_item's own validation.
            # Surface, don't crash the rest of the batch.
            skipped_items.append({
                "index": idx, "title": title, "reason": "invalid",
                "message": str(exc),
            })
            continue
        if isinstance(result, dict) and result.get("error"):
            # add_sprint_item's own soft duplicate-guard rejection.
            skipped_items.append({
                "index": idx, "title": title,
                "reason": result.get("error"),
                "message": result.get("message"),
                "existing": result.get("existing"),
            })
            continue
        item_id = result["id"]
        created_item_ids.append(item_id)
        created_ids_by_index[idx] = item_id
        all_resources.extend(db_module.parse_touches_resources(result.get("touches_resources")))

        try:
            pointer = await db_module.add_sprint_item_pointer(
                db, project_id, item_id, "proposal",
                [{
                    "uri": f"meridian://workspace_proposal/{proposal_id}",
                    "selector": {"type": "node_id", "id": proposal_id},
                    "target_kind": "existing",
                }],
                label=f"Source proposal: {proposal_title}"[:500],
            )
            pointer_ids[item_id] = pointer["id"]
        except Exception as exc:  # noqa: BLE001 -- best-effort: the item itself is
            pointer_errors[item_id] = str(exc)  # already committed; never un-create it.

    # --- HITL deviation gate ---------------------------------------------
    hitl_filed: "dict[str, Any] | None" = None
    deviation_category: "str | None" = None
    deviation_override_reason: "str | None" = None
    if created_item_ids:
        deviation_category = _classify_deviation(proposal_title, proposal_body, all_resources)
        if deviation_category:
            if override_reason and override_reason.strip():
                deviation_override_reason = override_reason.strip()
                try:
                    await db_module.append_proposal_update(
                        db, proposal_id,
                        f"proposal_to_handoff deviation ({deviation_category}) "
                        f"overridden: {deviation_override_reason}",
                        event_type="deviation_override",
                        payload={
                            "category": deviation_category, "resources": all_resources,
                            "override_reason": deviation_override_reason,
                            "sprint_item_ids": created_item_ids,
                        },
                        actor=actor, session_id=session_id, tenant_id=tenant_id,
                    )
                except Exception:  # noqa: BLE001 -- audit-logging must never block
                    pass
            else:
                question = (
                    f"proposal_to_handoff decomposed proposal '{proposal_id}' into "
                    f"{len(created_item_ids)} sprint item(s) whose combined "
                    f"touches_resources/title/body match a HITL-triggering "
                    f"deviation category ({deviation_category}). Confirm before "
                    "treating this proposal-run as executable."
                )
                try:
                    hitl_filed = await db_module.request_hitl(
                        db, project_id, question=question,
                        context=(
                            f"proposal={proposal_id} items={created_item_ids} "
                            f"category={deviation_category}"
                        ),
                        session_id=session_id,
                        kind="proposal_deviation",
                        payload=json.dumps({
                            "proposal_id": proposal_id,
                            "sprint_item_ids": created_item_ids,
                            "category": deviation_category,
                            "resources": all_resources,
                        }),
                        urgency="normal",
                        # Mirrors ce4883f3's own require_human=True for exactly
                        # these 3 categories -- never auto-answerable.
                        require_human=True,
                    )
                except Exception:  # noqa: BLE001 -- never fail the whole call over
                    hitl_filed = None  # a HITL-filing hiccup; the items already exist.
                if hitl_filed:
                    try:
                        await db_module.append_proposal_update(
                            db, proposal_id,
                            f"proposal_to_handoff filed HITL for {deviation_category} "
                            f"deviation (hitl_id={hitl_filed.get('id')})",
                            event_type="deviation_hitl_filed",
                            payload={
                                "category": deviation_category,
                                "hitl_id": hitl_filed.get("id"),
                                "sprint_item_ids": created_item_ids,
                            },
                            actor=actor, session_id=session_id, tenant_id=tenant_id,
                        )
                    except Exception:  # noqa: BLE001
                        pass

    # --- Handoff -----------------------------------------------------------
    handoff_path: "str | None" = None
    handoff_content_length: "int | None" = None
    handoff_amended: "bool | None" = None
    handoff_error: "str | None" = None
    hitl_gates: list[dict[str, Any]] = []
    proposal_scope_hash: "str | None" = None

    if not created_item_ids:
        executable = False
        executable_reasons = ["no_items_created"]
    elif skip_handoff:
        executable = False
        executable_reasons = ["handoff_skipped"]
    else:
        scope_out: dict[str, Any] = {}
        try:
            path, content, amended = await handoff_module.generate_handoff(
                db, project_id, output_dir,
                mode=mode,
                session_id=session_id,
                selected_item_ids=created_item_ids,
                skip_ai_summary=True,
                proposal_scope=scope_out,
            )
        except (
            handoff_module.HandoffSelectionError,
            handoff_module.HandoffScopeNonExecutable,
            handoff_module.HandoffEvidenceRequired,
        ) as exc:
            # Fails CLOSED, honest failure report -- mirrors
            # commit_proposal_promotion's own _record_race_deviation posture:
            # the sprint items/pointers created above are NOT rolled back
            # (this is a downstream rendering failure, not a proposal-state
            # race), but this run is definitely not executable.
            executable = False
            executable_reasons = [type(exc).__name__]
            handoff_error = str(exc)
        else:
            handoff_path = path
            handoff_content_length = len(content or "")
            handoff_amended = amended
            executable = bool(scope_out.get("executable"))
            executable_reasons = list(scope_out.get("executable_reasons") or [])
            hitl_gates = list(scope_out.get("hitl_gates") or [])
            proposal_scope_hash = scope_out.get("content_hash")

    return ProposalRunReceipt(
        proposal_id=proposal_id,
        project_id=project_id,
        generated_at=_utcnow_iso(),
        created_item_ids=created_item_ids,
        skipped_items=skipped_items,
        pointer_ids=pointer_ids,
        pointer_errors=pointer_errors,
        hitl_gates=hitl_gates,
        hitl_filed=hitl_filed,
        deviation_category=deviation_category,
        deviation_override_reason=deviation_override_reason,
        executable=executable,
        executable_reasons=executable_reasons,
        handoff_path=handoff_path,
        handoff_content_length=handoff_content_length,
        handoff_amended=handoff_amended,
        handoff_error=handoff_error,
        proposal_scope_hash=proposal_scope_hash,
    )
