"""Unified per-item executor_contract (23e20656, 665 follow-up).

Composes ONE canonical, deterministically-hashable object per sprint item
from state that already exists in three separate places:

* **76dde31f** typed ``tool_requirements`` (:mod:`meridian.tool_requirements`)
  -- required vs preferred tools, fallbacks, call templates.
* **4e44139e** typed sprint-item pointers (:mod:`meridian.pointers` /
  :func:`meridian.capability_contract.extract_sprint_item_pointers`) --
  resolution status + provenance for every durable pointer on the item.
* **2f9cb288/b7308039** artifact declarations (:mod:`meridian.artifact_declaration`)
  -- the item's declared output (``artifact_kind`` / ``planned_output`` /
  ``artifact_policy``).

...plus dependency/wave/gate state and completion-gate state read straight
off :mod:`meridian.db.sprint_items` (never re-derived independently):
:func:`meridian.db.get_blocking_dependency_for_sprint_item`,
:func:`meridian.db._get_blocking_wave_gate`, :func:`meridian.db.get_wave_gate_configs`,
:func:`meridian.db.is_item_claim_prospected` / :func:`meridian.db._item_declares_resources`,
and the ``required_notes`` / ``require_verification`` completion gates
``complete_sprint_item`` itself enforces.

Why this module exists: without it, "what tool must I use, is it actually
available right now, what's the output supposed to look like, am I even
allowed to claim this yet, and how do I know I'm done" was answered by
re-deriving overlapping logic in at least three places (the batch /goal's
XML clauses in ``handoff.py``, ``capability_contract.py``'s JSON section, and
whatever prose a human happened to write in the item's notes) that could
silently drift from each other and from what ``claim_sprint_item`` /
``complete_sprint_item`` actually enforce. This module builds the object
ONCE; :func:`to_json`, :func:`render_xml_clause`, and :func:`render_text` are
pure, read-only PROJECTIONS of that same object -- none of them re-fetch or
re-derive a single field.

Tool availability (ac80aaaf): a tool_requirements entry uses the free-text
convention ``server_or_namespace: name`` (e.g. ``"Serena: find_symbol"``),
while :mod:`meridian.capability_availability`'s live-inventory classifier
uses the ``plugin__tool`` convention. :func:`_bridge_tool_ref` bridges the
two -- a documented, best-effort convention, NOT a contract with either
sibling module (mirrors the disclaimer in ``capability_contract.py``'s own
sibling-integration notes). Every required tool_requirements entry is
classified via :func:`meridian.capability_availability.evaluate_capability_availability`
(reused, not reimplemented -- this is the SAME fallback-rescue logic
``check_capability_availability`` applies to capability-manifest entries).
Absent a live tunnel inventory (the default -- building one requires tenant/
tunnel state this module deliberately does not depend on, see
``mcp/handlers/project_tools.py::_build_live_inventory``), only Meridian's
own always-in-process built-in tools resolve to a real verdict; everything
else degrades to ``"unknown"`` -- exactly capability_contract.py's existing
"can't confirm -> unknown, never silently available" philosophy. A caller
that already has a live inventory (or wants to inject a fake one for a
test) may pass ``tool_inventory=`` or ``tool_availability_checker=``.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from . import artifact_declaration as _artifact_declaration
from . import capability_availability as _capability_availability
from . import capability_contract as _capability_contract
from . import db as db_module
from . import tool_requirements as _tool_requirements

EXECUTOR_CONTRACT_SCHEMA_VERSION = 1

ToolAvailabilityChecker = Callable[[list[dict[str, Any]]], dict[Any, dict[str, Any]]]


# ---------------------------------------------------------------------------
# Canonical serialization / hashing -- same discipline as capability_contract.py
# and tool_requirements.py (sorted keys, compact separators, no wall-clock
# fields inside the hashed payload).
# ---------------------------------------------------------------------------

def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def serialize_executor_contract(contract: dict[str, Any]) -> str:
    """Byte-stable canonical JSON for ``contract``, EXCLUDING ``generated_at``
    and ``contract_hash`` themselves -- mirrors
    ``capability_contract.serialize_contract`` exactly, for the exact same
    reason: a wall-clock timestamp and the hash of everything else would
    otherwise make "same live item state" never hash identically twice."""
    payload = {
        k: v for k, v in contract.items()
        if k not in ("generated_at", "contract_hash")
    }
    return _canonical_json(payload)


def executor_contract_hash(contract: dict[str, Any]) -> str:
    """Stable sha256 over :func:`serialize_executor_contract`'s canonical form."""
    return hashlib.sha256(serialize_executor_contract(contract).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Tool availability -- bridges tool_requirements' "Server: name" convention
# onto capability_availability's "plugin__tool" classifier, reusing (never
# reimplementing) evaluate_capability_availability's fallback-rescue logic.
# ---------------------------------------------------------------------------

def _default_builtin_tool_names() -> frozenset[str]:
    """Meridian's own always-in-process MCP tool names, for the default
    (no live tunnel) availability inventory. Lazy-imported (mirrors
    ``mcp/handlers/project_tools.py::_build_live_inventory``'s own inline
    import) so importing this module never pulls in the full tool-schema
    list at module load time. Never raises: any import failure degrades to
    an empty set (every tool_requirements entry then classifies as
    ``"unknown"`` rather than crashing contract building)."""
    try:
        from .mcp_tools import _MCP_TOOLS_LIST  # noqa: PLC0415
        from .tool_manifest import build_tool_manifest  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return frozenset()
    try:
        manifest = build_tool_manifest(_MCP_TOOLS_LIST)
        return frozenset(
            t["name"] for t in manifest.get("tools", [])
            if isinstance(t, dict) and t.get("name")
        )
    except Exception:  # noqa: BLE001
        return frozenset()


def _bridge_tool_ref(server_or_namespace: "str | None", name: "str | None") -> str:
    """Bridge a tool_requirements ``(server_or_namespace, name)`` pair onto a
    capability_availability-style tool_ref.

    ``"meridian"`` / ``"legacy"`` / an empty namespace resolve to the BARE
    name (Meridian's own built-in tools, and un-namespaced legacy
    ``required_tool`` pins, are matched against ``builtin_tools`` directly --
    see :func:`meridian.capability_availability.classify_tool`). Any other
    namespace joins as ``"<namespace>__<name>"``, the plugin/slot convention.
    This is a best-effort, documented bridge -- NOT a contract with either
    sibling module's own naming choices.
    """
    server = (server_or_namespace or "").strip()
    bare = (name or "").strip()
    if not bare:
        return ""
    if not server or server.lower() in ("legacy", "meridian"):
        return bare
    return f"{server}__{bare}"


def _bridge_fallback_ref(raw: str) -> str:
    """Same bridge as :func:`_bridge_tool_ref`, applied to a free-form
    ``fallback`` string (``tool_requirements`` fallbacks are plain strings,
    not ``(server, name)`` pairs -- split on the first ``":"`` when present,
    pass through unchanged otherwise)."""
    text = (raw or "").strip()
    if not text:
        return ""
    if ":" in text:
        head, _, tail = text.partition(":")
        bridged = _bridge_tool_ref(head, tail)
        return bridged or text
    return text


def _default_tool_inventory() -> dict[str, Any]:
    return {
        "tunnel_reachable": False,
        "builtin_tools": _default_builtin_tool_names(),
        "plugins": {},
        "stdio_registry": {},
    }


def _requirement_key(requirement: dict[str, Any]) -> tuple[str, str]:
    return (requirement.get("server_or_namespace") or "", requirement.get("name") or "")


def default_tool_availability(
    requirements: list[dict[str, Any]], *, inventory: "dict[str, Any] | None" = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Classify every requirement's availability via
    :func:`meridian.capability_availability.evaluate_capability_availability`,
    keyed by ``(server_or_namespace, name)``.

    Each requirement is converted into a capability-manifest-shaped pseudo
    entry (``id``, ``required_tools``, ``fallback_chain``,
    ``availability_policy``) so the SAME fallback-rescue algorithm ac80aaaf
    already ships applies here too -- never a second, independent
    implementation of "try the fallback chain in order." ``required_or_preferred
    == "preferred"`` maps to ``availability_policy="optional"``.

    Guarded per-requirement: a classification failure degrades that ONE
    requirement to ``status="unknown"`` rather than breaking contract
    building. Never raises.
    """
    inv = inventory if isinstance(inventory, dict) else _default_tool_inventory()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for req in requirements or []:
        if not isinstance(req, dict):
            continue
        key = _requirement_key(req)
        ref = _bridge_tool_ref(req.get("server_or_namespace"), req.get("name"))
        fallback_refs = [
            f for f in (_bridge_fallback_ref(fb) for fb in req.get("fallback") or []) if f
        ]
        pseudo_capability = {
            "id": f"{req.get('server_or_namespace')}: {req.get('name')}",
            "required_tools": [ref] if ref else [],
            "fallback_chain": fallback_refs,
            "availability_policy": (
                "required" if req.get("required_or_preferred") == "required" else "optional"
            ),
        }
        try:
            out[key] = _capability_availability.evaluate_capability_availability(
                pseudo_capability, inv,
            )
        except Exception:  # noqa: BLE001 — classification must never break the contract
            out[key] = {
                "capability_id": pseudo_capability["id"],
                "availability_policy": pseudo_capability["availability_policy"],
                "status": _capability_availability.STATUS_UNKNOWN,
                "required_tools": [],
                "fallback_used": None,
            }
    return out


# ---------------------------------------------------------------------------
# allowed_tools / forbidden_tools -- semantic tool instructions, distinct
# from touches_resources' scheduling-only resource ids (see `scheduling` in
# the built contract).
# ---------------------------------------------------------------------------

def _build_tool_sections(
    requirements: list[dict[str, Any]],
    availability_by_key: dict[tuple[str, str], dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Returns ``(allowed_tools, forbidden_tools)``.

    ``allowed_tools`` lists EVERY declared requirement (required AND
    preferred) with its availability annotated -- preserves the required-vs-
    preferred distinction verbatim (never collapses the two).

    ``forbidden_tools`` is deliberately narrow: only a ``required`` entry
    whose resolved availability status is ``"missing"`` (confirmed
    unavailable AFTER the fallback chain was tried and rescued nothing) is
    forbidden -- an executor must not waste a turn attempting it. Mirrors
    ``capability_contract``'s own tested rule that an ``"unknown"`` (can't
    confirm) status never forces a hard block, and that a ``"degraded"``
    (fallback rescued it) status is a green light on the fallback, not a
    block -- see :func:`meridian.capability_availability.evaluate_capability_availability`.
    """
    allowed: list[dict[str, Any]] = []
    forbidden: list[dict[str, Any]] = []
    for req in requirements:
        key = _requirement_key(req)
        avail = availability_by_key.get(key) or {
            "status": _capability_availability.STATUS_UNKNOWN, "fallback_used": None,
        }
        status = avail.get("status", _capability_availability.STATUS_UNKNOWN)
        entry = {
            "name": req.get("name"),
            "server_or_namespace": req.get("server_or_namespace"),
            "required_or_preferred": req.get("required_or_preferred"),
            "purpose": req.get("purpose"),
            "call_template": req.get("call_template"),
            "fallback": list(req.get("fallback") or []),
            "risk_class": _tool_requirements.requirement_risk_class(req),
            "availability_status": status,
            "fallback_used": avail.get("fallback_used"),
        }
        allowed.append(entry)
        if req.get("required_or_preferred") == "required" and status == _capability_availability.STATUS_MISSING:
            forbidden.append({
                "name": req.get("name"),
                "server_or_namespace": req.get("server_or_namespace"),
                "reason": (
                    "required tool unavailable; fallback chain exhausted"
                    if req.get("fallback")
                    else "required tool unavailable; no fallback declared"
                ),
            })
    allowed.sort(key=lambda e: (e["server_or_namespace"] or "", e["name"] or ""))
    forbidden.sort(key=lambda e: (e["server_or_namespace"] or "", e["name"] or ""))
    return allowed, forbidden


# ---------------------------------------------------------------------------
# dependency / wave / gate state -- read straight off the SAME functions
# claim_sprint_item's own structural gates use; never re-derived.
# ---------------------------------------------------------------------------

def _trim_item_ref(item: "dict[str, Any] | None") -> "dict[str, Any] | None":
    if not isinstance(item, dict):
        return None
    return {"id": item.get("id"), "title": item.get("title"), "status": item.get("status")}


def _trim_gate_config(cfg: "dict[str, Any] | None") -> "dict[str, Any] | None":
    if not isinstance(cfg, dict):
        return None
    return {
        "wave_start": cfg.get("wave_start"),
        "wave_end": cfg.get("wave_end"),
        "actions": cfg.get("actions") or [],
        "gate_passed": bool(cfg.get("gate_passed")),
    }


async def _resolve_dependency_state(
    db: Any, item: dict[str, Any],
) -> dict[str, Any]:
    """Mirrors ``get_blocking_dependency_for_sprint_item`` +
    ``get_parallelizable_groups``'s own failure_mode='continue' carve-out
    (a failed parent does not block a child whose ``failure_mode`` is
    'continue', the default) -- reused, not re-derived independently."""
    try:
        blocking = await db_module.get_blocking_dependency_for_sprint_item(db, item.get("id"))
    except Exception:  # noqa: BLE001 — dependency state is best-effort
        blocking = None
    satisfied = blocking is None
    if blocking is not None and blocking.get("status") == "failed":
        if (item.get("failure_mode") or "continue") == "continue":
            satisfied = True
    return {
        "depends_on": item.get("depends_on"),
        "failure_mode": item.get("failure_mode") or "continue",
        "blocking_item": _trim_item_ref(blocking) if not satisfied else None,
        "satisfied": satisfied,
    }


async def _resolve_wave_gate_state(
    db: Any, project_id: str, item_wave: "str | None",
    *, wave_gate_configs: "list[dict[str, Any]] | None" = None,
) -> tuple["dict[str, Any] | None", "dict[str, Any] | None"]:
    """Returns ``(gate_blocking, gate_after)``.

    ``gate_blocking`` -- the configured-but-unpassed wave gate boundary
    STRICTLY BEFORE this item's wave (the same structural check
    ``claim_sprint_item`` runs via ``db._get_blocking_wave_gate`` -- called
    directly here, never re-implemented).

    ``gate_after`` -- the configured wave gate whose boundary IS this item's
    own wave (``wave_end == item_wave``): once this item (and its wave-mates)
    finish, this gate must pass before any later wave may proceed. Computed
    from a caller-supplied or self-fetched ``get_wave_gate_configs`` list
    (the same read path ``handoff.py``'s ``<excluded_wave_gate_pending>``
    clause uses) via ``_split_wave_label`` equality -- distinct semantics
    from ``gate_blocking`` (strictly-before vs. exactly-at), so it is
    computed separately rather than trying to squeeze both out of
    ``_get_blocking_wave_gate``, which only ever answers the "before" question.
    """
    try:
        gate_blocking = await db_module._get_blocking_wave_gate(db, project_id, item_wave)
    except Exception:  # noqa: BLE001 — wave-gate state is best-effort
        gate_blocking = None

    gate_after: "dict[str, Any] | None" = None
    if item_wave:
        configs = wave_gate_configs
        if configs is None:
            try:
                configs = await db_module.get_wave_gate_configs(db, project_id)
            except Exception:  # noqa: BLE001
                configs = []
        item_prefix, item_num = db_module._split_wave_label(item_wave)
        if item_num is not None:
            for cfg in configs or []:
                cfg_prefix, cfg_num = db_module._split_wave_label(cfg.get("wave_end"))
                if cfg_num is not None and cfg_prefix == item_prefix and cfg_num == item_num:
                    gate_after = cfg
                    break
    return _trim_gate_config(gate_blocking), _trim_gate_config(gate_after)


# ---------------------------------------------------------------------------
# completion_checks -- the SAME gates complete_sprint_item structurally
# enforces (required_notes, require_verification, the unprospected-resources
# gate), read via the same shared helpers claim/complete already use.
# ---------------------------------------------------------------------------

async def _resolve_completion_checks(
    db: Any, project_id: str, item: dict[str, Any],
) -> dict[str, Any]:
    item_id = item.get("id")
    evidence_present = bool((item.get("notes") or "").strip() or item.get("task_id"))

    verification_on_file = None
    if item.get("require_verification"):
        try:
            verification_on_file = await db_module.get_latest_sprint_item_verification(
                db, project_id, item_id,
            )
        except Exception:  # noqa: BLE001
            verification_on_file = None

    try:
        evidence_ids = await db_module.get_pointer_evidence_item_ids(db, [item_id])
    except Exception:  # noqa: BLE001
        evidence_ids = None
    # Fail-open (mirrors handoff._build_quick_start_goal's own convention): a
    # failed/omitted lookup is treated as "has evidence" so a transient DB
    # hiccup never manufactures a false unprospected block.
    has_pointer_evidence = True if evidence_ids is None else item_id in evidence_ids
    declares_resources = db_module._item_declares_resources(item)
    prospected = db_module.is_item_claim_prospected(
        item, has_pointer_evidence=has_pointer_evidence,
    )

    return {
        "required_notes": bool(item.get("required_notes")),
        "required_notes_satisfied": (
            True if not item.get("required_notes") else evidence_present
        ),
        "require_verification": bool(item.get("require_verification")),
        # Whether an on-file PASS verdict currently exists. Deliberately does
        # NOT evaluate the same-session-self-report exclusion
        # complete_sprint_item also applies -- that check needs the
        # COMPLETING session's own actor identity, which does not exist yet
        # at contract-build time (this contract is built before completion,
        # for any future completer).
        "require_verification_satisfied": (
            True if not item.get("require_verification")
            else bool(verification_on_file and verification_on_file.get("verdict") == "pass")
        ),
        "verification_on_file": (
            {
                "verdict": verification_on_file.get("verdict"),
                "verifier_session_id": verification_on_file.get("verifier_session_id"),
                "notes": verification_on_file.get("notes"),
                "created_at": verification_on_file.get("created_at"),
            }
            if verification_on_file else None
        ),
        "prospecting": {
            "declares_resources": declares_resources,
            "has_pointer_evidence": has_pointer_evidence,
            "prospected": prospected,
            "prospect_bypass": bool(item.get("prospect_bypass")),
        },
    }


# ---------------------------------------------------------------------------
# ordered steps -- a pure function of everything already composed above; no
# independent re-derivation.
# ---------------------------------------------------------------------------

def _build_ordered_steps(
    *,
    dependency: dict[str, Any],
    gate_blocking: "dict[str, Any] | None",
    pointers_entry: "dict[str, Any] | None",
    allowed_tools: list[dict[str, Any]],
    output_requirements: dict[str, Any],
    completion_checks: dict[str, Any],
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []

    def _add(kind: str, description: str, **extra: Any) -> None:
        entry = {"order": len(steps) + 1, "kind": kind, "description": description}
        entry.update(extra)
        steps.append(entry)

    if not dependency["satisfied"]:
        _add(
            "blocked",
            f"Wait for dependency {dependency.get('depends_on')} to reach status "
            "'done' before this item can be claimed.",
        )
    if gate_blocking is not None:
        _add(
            "blocked",
            f"Wave gate at {gate_blocking.get('wave_end')} must pass "
            "(complete_wave_gate) before this item is claimable.",
        )
    if not completion_checks["prospecting"]["prospected"]:
        _add(
            "blocked",
            "This item declares touches_resources but has no durable pointer "
            "evidence yet — claim_sprint_item will refuse it as UNPROSPECTED "
            "until pointer evidence is recorded or a human sets prospect_bypass.",
        )
    if pointers_entry and pointers_entry.get("pointers"):
        _add(
            "pointer_review",
            f"Review the {len(pointers_entry['pointers'])} durable pointer "
            "target(s) already attached to this item before editing.",
        )
    for tool in allowed_tools:
        if tool.get("required_or_preferred") != "required":
            continue
        _add(
            "tool_call",
            f"Use {tool['server_or_namespace']}: {tool['name']} — {tool['purpose']}",
            tool={
                "name": tool["name"],
                "server_or_namespace": tool["server_or_namespace"],
                "call_template": tool.get("call_template"),
            },
        )
    if output_requirements.get("planned_output"):
        _add(
            "output_write",
            "Produce the declared planned_output artifact at its pointer target.",
        )
    if completion_checks["required_notes"]:
        _add(
            "completion_check",
            "Record notes describing what shipped / how it was verified "
            "(required_notes is set — complete_sprint_item refuses without it).",
        )
    if completion_checks["require_verification"]:
        _add(
            "completion_check",
            "Obtain an independent fresh-session verification PASS "
            "(require_verification is set — complete_sprint_item refuses without it).",
        )
    _add("finish", "Call complete_sprint_item(item_id, project_id).")
    return steps


# ---------------------------------------------------------------------------
# The builder.
# ---------------------------------------------------------------------------

async def build_executor_contract(
    db: Any,
    project_id: str,
    item: dict[str, Any],
    *,
    version: "str | None" = None,
    execution_mode: "str | None" = None,
    tool_availability_checker: "ToolAvailabilityChecker | None" = None,
    tool_inventory: "dict[str, Any] | None" = None,
    node_resolver: "Any | None" = None,
    wave_gate_configs: "list[dict[str, Any]] | None" = None,
    project: "dict[str, Any] | None" = None,
) -> dict[str, Any]:
    """Build the canonical executor_contract for ONE sprint item.

    ``item`` is a full sprint-item dict (as returned by
    ``db.get_sprint_item`` / ``db.get_sprint_items``) -- passed explicitly so
    a caller already iterating a pending-item list never re-fetches it (the
    same "pass items explicitly when you have them" discipline
    ``capability_contract.build_capability_contract`` uses for its own
    ``items`` kwarg).

    ``version`` -- the SCOPE this contract was requested for (e.g. a
    version-scoped ``/goal``); distinct from the item's own ``version``
    field, which is always taken from ``item['version']`` regardless. Surfaced
    under ``scope.requested_version``.

    ``wave_gate_configs`` / ``project`` -- optional pre-fetched state so a
    caller building contracts for many items in one project does not repeat
    the same two project-scoped queries per item (mirrors
    ``capability_contract``'s own "pass what you already have" pattern).

    Never raises: every DB-backed sub-step is individually guarded and
    degrades to an empty/unknown value rather than breaking the mandatory
    handoff/session paths this feeds. A caller should still wrap this in its
    own try/except per the existing codebase convention.
    """
    item_id = item.get("id")

    # 76dde31f -- typed tool_requirements (structured column, else the legacy
    # required_tool read-time bridge). Canonical source: tool_requirements.py.
    requirements = _tool_requirements.effective_tool_requirements(item)
    if tool_availability_checker is not None:
        try:
            availability_by_key = tool_availability_checker(requirements)
            if not isinstance(availability_by_key, dict):
                availability_by_key = {}
        except Exception:  # noqa: BLE001 — an injected checker must never crash the contract
            availability_by_key = {}
    else:
        availability_by_key = default_tool_availability(requirements, inventory=tool_inventory)
    allowed_tools, forbidden_tools = _build_tool_sections(requirements, availability_by_key)

    # 4e44139e/665 follow-up -- typed pointer resolution + provenance, reused
    # verbatim from capability_contract's own per-item extraction (the SAME
    # function the project-wide JSON contract calls) so the two never drift.
    try:
        pointer_entries = await _capability_contract.extract_sprint_item_pointers(
            db, project_id, [item], node_resolver=node_resolver,
        )
    except Exception:  # noqa: BLE001
        pointer_entries = []
    pointers_entry = pointer_entries[0] if pointer_entries else None

    # 2f9cb288/b7308039 -- declared output.
    output_requirements = {
        "artifact_kind": _artifact_declaration.effective_artifact_kind(item),
        "planned_output": _artifact_declaration.effective_planned_output(item),
        "policy": _artifact_declaration.effective_artifact_policy(item),
        "declared": _artifact_declaration.has_artifact_declaration(item),
    }

    # Scheduling-only touches_resources -- kept STRUCTURALLY SEPARATE from the
    # semantic tool_requirements above (allowed_tools/forbidden_tools). These
    # answer two different questions: "what files/symbols does this item
    # touch, for parallel-conflict batching" vs. "what tool must the executor
    # call, and is it available."
    scheduling = {
        "touches_resources": db_module.parse_touches_resources(item.get("touches_resources")),
    }

    dependency = await _resolve_dependency_state(db, item)
    gate_blocking, gate_after = await _resolve_wave_gate_state(
        db, project_id, item.get("wave"), wave_gate_configs=wave_gate_configs,
    )
    completion_checks = await _resolve_completion_checks(db, project_id, item)

    executable = True
    executable_reasons: list[str] = []
    if forbidden_tools:
        executable = False
        executable_reasons.append(
            "missing_required_tools:" + ",".join(
                f"{t['server_or_namespace']}: {t['name']}" for t in forbidden_tools
            )
        )
    if not dependency["satisfied"]:
        executable = False
        executable_reasons.append(f"blocked_on_dependency:{dependency.get('depends_on')}")
    if gate_blocking is not None:
        executable = False
        executable_reasons.append(f"wave_gate_pending:{gate_blocking.get('wave_end')}")
    if not completion_checks["prospecting"]["prospected"]:
        executable = False
        executable_reasons.append("unprospected_resources")

    steps = _build_ordered_steps(
        dependency=dependency,
        gate_blocking=gate_blocking,
        pointers_entry=pointers_entry,
        allowed_tools=allowed_tools,
        output_requirements=output_requirements,
        completion_checks=completion_checks,
    )

    if execution_mode is not None:
        resolved_mode = db_module.normalize_execution_mode(execution_mode)
    else:
        _project = project
        if _project is None:
            try:
                _project = await db_module.get_project(db, project_id)
            except Exception:  # noqa: BLE001
                _project = None
        resolved_mode = db_module.normalize_execution_mode(
            (_project or {}).get("execution_mode")
        )

    from datetime import datetime, timezone  # noqa: PLC0415

    contract: dict[str, Any] = {
        "schema_version": EXECUTOR_CONTRACT_SCHEMA_VERSION,
        "item_id": item_id,
        "version": item.get("version"),
        "scope": {
            "project_id": project_id,
            "requested_version": version,
            "wave": item.get("wave"),
            "track": item.get("track"),
            "milestone_type": item.get("milestone_type"),
            "priority": item.get("priority"),
        },
        "mode": resolved_mode,
        "allowed_tools": allowed_tools,
        "forbidden_tools": forbidden_tools,
        "scheduling": scheduling,
        "steps": steps,
        "gate_after": gate_after,
        "gate_blocking": gate_blocking,
        "dependency": dependency,
        "output_requirements": output_requirements,
        "pointers": pointers_entry,
        "completion_checks": completion_checks,
        "executable": executable,
        "executable_reasons": executable_reasons,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    contract["contract_hash"] = executor_contract_hash(contract)
    return contract


async def build_executor_contract_for_item_id(
    db: Any, project_id: str, item_id: str, **kwargs: Any,
) -> "dict[str, Any] | None":
    """Convenience wrapper: fetch the item, then :func:`build_executor_contract`.

    Returns ``None`` when the item does not exist or belongs to a different
    project -- never raises."""
    item = await db_module.get_sprint_item(db, item_id)
    if item is None or item.get("project_id") != project_id:
        return None
    return await build_executor_contract(db, project_id, item, **kwargs)


# ---------------------------------------------------------------------------
# Projections -- JSON / XML clause / human text. Every renderer below is a
# PURE function of an already-built ``contract`` dict: none of them fetch,
# resolve, or recompute a single field. This is what keeps the machine-
# readable and human-readable surfaces from independently re-deriving (and
# silently drifting from) the same information.
# ---------------------------------------------------------------------------

def to_json(contract: dict[str, Any]) -> str:
    """Canonical JSON projection (including ``generated_at``/``contract_hash``,
    unlike :func:`serialize_executor_contract` which excludes them for
    hashing purposes)."""
    return _canonical_json(contract)


def render_xml_clause(contract: dict[str, Any]) -> str:
    """A ``<executor_contract>`` XML clause suitable for embedding in a
    rendered /goal, built ONLY from fields already present on ``contract``."""
    from xml.sax.saxutils import escape as _xml_escape  # noqa: PLC0415

    lines = [
        f'<executor_contract item_id="{_xml_escape(str(contract.get("item_id") or ""))}" '
        f'mode="{_xml_escape(str(contract.get("mode") or ""))}" '
        f'executable="{"true" if contract.get("executable") else "false"}" '
        f'contract_hash="{_xml_escape(str(contract.get("contract_hash") or ""))}">',
    ]
    for reason in contract.get("executable_reasons") or []:
        lines.append(f"  <blocked_reason>{_xml_escape(str(reason))}</blocked_reason>")
    for tool in contract.get("allowed_tools") or []:
        _tool_label = f"{tool.get('server_or_namespace')}: {tool.get('name')}"
        lines.append(
            f'  <allowed_tool required_or_preferred="{_xml_escape(str(tool.get("required_or_preferred") or ""))}" '
            f'availability="{_xml_escape(str(tool.get("availability_status") or ""))}">'
            f'{_xml_escape(_tool_label)}'
            "</allowed_tool>"
        )
    for tool in contract.get("forbidden_tools") or []:
        _tool_label = f"{tool.get('server_or_namespace')}: {tool.get('name')}"
        lines.append(
            "  <forbidden_tool>"
            f'{_xml_escape(_tool_label)}'
            "</forbidden_tool>"
        )
    for step in contract.get("steps") or []:
        lines.append(
            f'  <step order="{step.get("order")}" kind="{_xml_escape(str(step.get("kind") or ""))}">'
            f'{_xml_escape(str(step.get("description") or ""))}</step>'
        )
    gate_after = contract.get("gate_after")
    if gate_after:
        lines.append(
            f'  <gate_after wave_end="{_xml_escape(str(gate_after.get("wave_end") or ""))}" '
            f'gate_passed="{"true" if gate_after.get("gate_passed") else "false"}" />'
        )
    lines.append("</executor_contract>")
    return "\n".join(lines)


def render_text(contract: dict[str, Any]) -> str:
    """Human-readable projection of ``contract`` -- a plain-text summary an
    executor (or a human reviewing a handoff) can read directly."""
    lines = [
        f"Executor contract — item {contract.get('item_id')} "
        f"(version={contract.get('version')}, mode={contract.get('mode')})",
    ]
    if not contract.get("executable"):
        lines.append(
            "NOT EXECUTABLE right now: " + "; ".join(contract.get("executable_reasons") or [])
        )
    allowed = contract.get("allowed_tools") or []
    if allowed:
        lines.append("Tools:")
        for tool in allowed:
            lines.append(
                f"  - [{tool.get('required_or_preferred')}] "
                f"{tool.get('server_or_namespace')}: {tool.get('name')} — "
                f"{tool.get('purpose')} (availability: {tool.get('availability_status')})"
            )
    forbidden = contract.get("forbidden_tools") or []
    if forbidden:
        lines.append("Do NOT rely on (confirmed unavailable):")
        for tool in forbidden:
            lines.append(
                f"  - {tool.get('server_or_namespace')}: {tool.get('name')} — {tool.get('reason')}"
            )
    steps = contract.get("steps") or []
    if steps:
        lines.append("Steps:")
        for step in steps:
            lines.append(f"  {step.get('order')}. {step.get('description')}")
    gate_after = contract.get("gate_after")
    if gate_after:
        lines.append(
            f"After this item's wave, gate '{gate_after.get('wave_end')}' must pass "
            f"(already passed: {gate_after.get('gate_passed')})."
        )
    return "\n".join(lines)
