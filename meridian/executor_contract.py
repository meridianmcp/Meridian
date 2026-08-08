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
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from . import artifact_declaration as _artifact_declaration
from . import capability_availability as _capability_availability
from . import capability_contract as _capability_contract
from . import capability_manifest as _capability_manifest
from . import db as db_module
from . import tool_discovery as _tool_discovery
from . import tool_requirements as _tool_requirements

EXECUTOR_CONTRACT_SCHEMA_VERSION = 2

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
# Default routing lookup (de4d4293) -- a COMPACT, deterministic per-item
# tool-routing HINT for the common case a "confirmed handoff gap" describes:
# a sprint item with no explicit ``tool_requirements``/``required_tool`` pin
# at all. Distinct from -- and deliberately much lighter than -- the full
# :func:`build_executor_contract`: this answers ONLY "which tool should an
# executor reach for FIRST," never executability/dependency/gate/completion-
# check state. Explicit ``tool_requirements`` ALWAYS wins when present (see
# :func:`build_routing_hint`) -- this table is purely a fallback.
#
# This is intentionally NOT a second, parallel per-item CONTRACT mechanism:
# it reuses the exact same canonical read
# (:func:`meridian.tool_requirements.effective_tool_requirements`)
# :func:`build_executor_contract` itself uses for ``allowed_tools``/
# ``forbidden_tools``, and never computes executability, dependencies, gates,
# or completion checks -- those stay the sole responsibility of the full
# contract. See ``handoff._build_executor_routing_clause`` for the bounded
# (CLAIMABLE-batch-only) /goal text projection of this data, and
# ``capability_contract.build_capability_contract``'s ``item_routing_summary``
# for the structured JSON twin -- both read this SAME extraction so neither
# can independently drift from the other.
# ---------------------------------------------------------------------------

_DEFAULT_ROUTING_CATEGORIES: tuple[dict[str, Any], ...] = (
    {
        "category": "orchestration",
        "keywords": frozenset({
            "claim sprint", "sprint item", "parallelize", "parallel group",
            "wave gate", "orchestrat", "claim_file", "claim_resource",
            "claim_symbol",
        }),
        "server_or_namespace": "meridian",
        "name": "claim_sprint_item",
        "purpose": "orchestration/claims — claim, batch, or gate sprint work",
        "fallback": ["meridian: get_parallelizable_groups"],
    },
    {
        "category": "code_investigation",
        "keywords": frozenset({
            "investigate", "trace", "understand", "locate", "inspect",
            "audit", "explore", "find the", "search the code", "root cause",
        }),
        "server_or_namespace": "Serena",
        "name": "find_symbol",
        "purpose": "code investigation — locate and read the relevant symbol(s)",
        "fallback": ["codebase-memory: search_graph"],
    },
    {
        "category": "handoff",
        "keywords": frozenset({
            "handoff", "generate_handoff", "mode parity", "/goal", "goal block",
        }),
        "server_or_namespace": "meridian",
        "name": "generate_handoff",
        "purpose": "handoff work — render/verify a generate_handoff mode",
        "fallback": [],
    },
    {
        "category": "tunnel_verification",
        "keywords": frozenset({
            "tunnel", "verification", "verify the", "run_verification",
            "deploy gate", "smoke test",
        }),
        "server_or_namespace": "meridian",
        "name": "run_verification",
        "purpose": "tunnel verification — run the configured verification command",
        "fallback": ["meridian: run_cmd"],
    },
    {
        "category": "docx",
        "keywords": frozenset({
            "docx", "word document", ".docx", "region claim", "meridian-docs",
        }),
        "server_or_namespace": "meridian-docs",
        "name": "claim_docx_region",
        "purpose": "DOCX work — claim a document region before writing",
        "fallback": ["meridian-outputs: record_provenance"],
    },
    {
        # 92ac025c — the ONE deterministic signal for "this item is clearly
        # about an external library/framework's own published docs", per the
        # RESEARCH ROUTING PROTOCOL's own ordering (agent_defaults.py):
        # exact pointers and local structure always come first; this only
        # ever fires as a PREFERRED hint (never required_or_preferred=
        # "required" — see infer_default_routing_category's own docstring),
        # and the item's own local-code work is never skipped in favor of it.
        # Deliberately narrow, specific-phrase keywords (not generic words
        # like "docs" or "api") to avoid false-positiving against an ordinary
        # code-investigation or docx item that happens to mention "library".
        "category": "documentation",
        "keywords": frozenset({
            "framework docs", "library docs", "third-party library",
            "external library", "package documentation", "context7",
            "framework documentation", "library documentation",
        }),
        "server_or_namespace": "context7",
        "name": "resolve-library-id",
        "purpose": (
            "external library/framework docs — resolve-library-id then "
            "query-docs for version-pinned upstream documentation"
        ),
        "fallback": ["meridian: github_search", "meridian: paper_search"],
    },
)


def infer_default_routing_category(item: dict[str, Any]) -> "dict[str, Any] | None":
    """Best-effort, deterministic keyword match against
    :data:`_DEFAULT_ROUTING_CATEGORIES`, in table order (first match wins --
    an item whose text matches two categories' keywords always resolves to
    the SAME one across calls, never ambiguous). Searches the item's title +
    notes, lowercased. Returns a ``tool_requirements``-shaped dict
    (``required_or_preferred`` is always ``"preferred"`` -- an INFERRED
    default is never a hard block the way an explicit pin can be) or
    ``None`` when nothing matches. Pure, synchronous, no DB -- safe to call
    from a text renderer with no async plumbing.
    """
    haystack = " ".join([
        str(item.get("title") or ""), str(item.get("notes") or ""),
    ]).lower()
    if not haystack.strip():
        return None
    for cat in _DEFAULT_ROUTING_CATEGORIES:
        if any(kw in haystack for kw in cat["keywords"]):
            return {
                "name": cat["name"],
                "server_or_namespace": cat["server_or_namespace"],
                "required_or_preferred": "preferred",
                "purpose": cat["purpose"],
                "call_template": None,
                "fallback": list(cat["fallback"]),
                "availability_check": None,
                "verification": None,
                "routing_category": cat["category"],
            }
    return None


def build_routing_hint(item: dict[str, Any]) -> "dict[str, Any] | None":
    """One compact routing hint for ``item``: explicit ``tool_requirements``/
    ``required_tool`` (via
    :func:`meridian.tool_requirements.effective_tool_requirements` -- the
    SAME canonical read :func:`build_executor_contract` itself uses for
    ``allowed_tools``) when present, else
    :func:`infer_default_routing_category`'s best-effort default. Returns
    ``None`` when the item has no id, or neither source resolves anything --
    never fabricates a hint out of nothing.
    """
    item_id = item.get("id")
    if not item_id:
        return None
    requirements = _tool_requirements.effective_tool_requirements(item)
    if requirements:
        primary = next(
            (r for r in requirements if r.get("required_or_preferred") == "required"),
            requirements[0],
        )
        source = "explicit"
    else:
        primary = infer_default_routing_category(item)
        if primary is None:
            return None
        source = "inferred"
    return {
        "item_id": item_id,
        "server_or_namespace": primary.get("server_or_namespace"),
        "name": primary.get("name"),
        "required_or_preferred": primary.get("required_or_preferred"),
        "purpose": primary.get("purpose"),
        "source": source,
    }


def build_routing_summary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic (sorted by ``item_id``), compact per-item routing
    summary for ``items`` -- the canonical source BOTH the /goal text's
    ``<executor_routing>`` clause (``handoff._build_executor_routing_clause``)
    and the structured ``capability_contract.item_routing_summary`` field
    read, so the two never independently drift. Only items with a
    resolvable hint (explicit or inferred) are included -- an item with
    nothing to say contributes nothing, mirroring the sibling ``extract_*``
    helpers' own "nothing to report" restraint. Non-dict entries are
    skipped rather than raising.
    """
    hints = [
        h for h in (build_routing_hint(it) for it in items if isinstance(it, dict))
        if h is not None
    ]
    return sorted(hints, key=lambda h: h["item_id"])


def routing_summary_hash(summary: list[dict[str, Any]]) -> str:
    """Stable sha256 over ``summary``'s canonical JSON -- lets a TEXT
    projection and a structured JSON projection of the SAME summary prove
    byte-for-byte parity without either having to embed the other's full
    body."""
    return hashlib.sha256(_canonical_json(summary).encode("utf-8")).hexdigest()


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
    version: "str | None" = None,
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

    ``version`` (ed8e4524, pass the item's own ``item.get("version")``) keeps
    this in lockstep with ``claim_sprint_item``'s wave-gate scoping: a
    version-scoped config (``configure_wave_gate(..., version=...)``) only
    counts as this item's ``gate_after`` when its stored version matches;
    an unscoped (NULL) config remains project-wide and matches regardless of
    the item's own version, exactly the pre-ed8e4524 behavior.
    """
    try:
        gate_blocking = await db_module._get_blocking_wave_gate(
            db, project_id, item_wave, version=version,
        )
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
                cfg_version = cfg.get("version")
                if cfg_version is not None and cfg_version != version:
                    continue  # version-scoped config that isn't THIS item's version
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
    session_id: "str | None" = None,
    tenant: "dict[str, Any] | None" = None,
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

    # 86b36617 -- compiled ToolSearch-style discovery request + required/
    # preferred availability & fallback telemetry + the pre-edit
    # codebase-memory/Serena receipt gate, composed into ONE stable-shaped
    # object (requested/selected/first_call/availability/fallback/receipt).
    # Reuses the SAME availability_by_key just computed above for
    # allowed_tools/forbidden_tools (never a second, independently-computed
    # classification that could disagree with it). Guarded: a failure here
    # must never break the mandatory contract this feeds.
    try:
        tool_discovery = await _tool_discovery.build_tool_discovery_state(
            db, project_id, item,
            availability_by_key=availability_by_key,
            session_id=session_id, tenant=tenant,
        )
    except Exception:  # noqa: BLE001 — tool_discovery is best-effort enrichment
        tool_discovery = None

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
        version=item.get("version"),
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
        # cb00889c (bounded handoff profiles) — carried on the contract itself
        # (cheap, always available on `item`) so render_xml_clause's compact
        # projection can name the item without re-fetching/re-deriving
        # anything — every projection stays a PURE function of this object.
        "title": item.get("title"),
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
        # 86b36617 -- compiled discovery request + availability/fallback
        # telemetry + pre-edit receipt gate. Deliberately NOT folded into the
        # top-level `executable`/`executable_reasons` above: those reflect
        # the pre-existing "can this item be claimed" gate (dependency/wave
        # gate/missing required tool). `tool_discovery.executable` is a
        # SEPARATE, explicitly-surfaced discovery-side signal — see
        # tool_discovery.py's module docstring for why this stays distinct
        # from meridian.code_intel_receipt's completion-time gate.
        "tool_discovery": tool_discovery,
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


async def summarize_contract_for_report(
    db: Any, project_id: str, item_id: str, **kwargs: Any,
) -> "dict[str, Any] | None":
    """9154aa9a -- compact per-item contract summary for embedding into an
    executor_report item_outcome entry (see
    ``meridian.handoff.record_executor_report``'s ``enrich_contract_hashes``
    kwarg) -- durable provenance tying a reported outcome to the EXACT
    executor_contract (by hash) the executor was working against, without
    needing to persist the full contract object into every report row.

    Returns ``None`` when the item does not exist or belongs to a different
    project (mirrors :func:`build_executor_contract_for_item_id`) -- never
    raises."""
    contract = await build_executor_contract_for_item_id(
        db, project_id, item_id, **kwargs,
    )
    if contract is None:
        return None
    return {
        "item_id": contract.get("item_id"),
        "contract_hash": contract.get("contract_hash"),
        "executable": contract.get("executable"),
        "generated_at": contract.get("generated_at"),
    }


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


def render_xml_clause(contract: dict[str, Any], *, compact: bool = False) -> str:
    """A ``<executor_contract>`` XML clause suitable for embedding in a
    rendered /goal, built ONLY from fields already present on ``contract``.

    ``compact`` (cb00889c, bounded handoff profiles) — when True, renders a
    single self-closing element carrying ONLY the bounded, budget-safe
    surface: ``item_id``, ``title``, scope (``requested_version``/``wave``/
    ``track``/``milestone_type``/``priority``), ``contract_hash``, durable
    pointer ids, and ``executable`` status — the "is this item executable and
    what does it durably point at" summary a batch /goal can embed per item
    without the size risk of the full tool/step/gate detail below (the exact
    per-item bloat class 23e20656/de4d4293 already had to cap elsewhere via
    ``max_executor_contracts``). It deliberately omits allowed/forbidden
    tools, steps, gates, dependency, and completion-check detail — fetch that
    on demand via the FULL projection: ``render_xml_clause(contract)`` (the
    default, ``compact=False``, byte-for-byte unchanged from before this
    parameter existed), :func:`to_json`, or :func:`render_text` — all pure
    projections of this SAME already-built ``contract``, no re-fetch. The
    function-level default stays ``compact=False`` so every existing caller
    keeps its exact prior (full) output; a caller wanting the bounded profile
    opts in explicitly.
    """
    from xml.sax.saxutils import escape as _xml_escape  # noqa: PLC0415

    if compact:
        _scope = contract.get("scope") or {}
        _pointers_entry = contract.get("pointers") or {}
        _pointer_ids = [
            str(_p["id"])
            for _p in (_pointers_entry.get("pointers") or [])
            if isinstance(_p, dict) and _p.get("id")
        ]
        return (
            '<executor_contract compact="true" '
            f'item_id="{_xml_escape(str(contract.get("item_id") or ""))}" '
            f'title="{_xml_escape(str(contract.get("title") or ""))}" '
            f'executable="{"true" if contract.get("executable") else "false"}" '
            f'contract_hash="{_xml_escape(str(contract.get("contract_hash") or ""))}" '
            f'requested_version="{_xml_escape(str(_scope.get("requested_version") or ""))}" '
            f'wave="{_xml_escape(str(_scope.get("wave") or ""))}" '
            f'track="{_xml_escape(str(_scope.get("track") or ""))}" '
            f'milestone_type="{_xml_escape(str(_scope.get("milestone_type") or ""))}" '
            f'priority="{_xml_escape(str(_scope.get("priority") or ""))}" '
            f'pointer_ids="{_xml_escape(",".join(_pointer_ids))}" />'
        )

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


# ===========================================================================
# 3b3020ac -- hash-pinned scientific execution manifests, per-worker
# completion records, and fail-closed aggregation.
#
# A GENERIC, reusable contract for long parallel scientific reruns and other
# fan-out jobs -- narrower than, and deliberately non-duplicative of, the
# 24f5146d docx-promotion work in :mod:`meridian.artifact_declaration`
# (``compute_base_sha256`` / ``check_promotion_preconditions`` / the merger
# lock) and :mod:`meridian.db.wave_runs` (``promotion_precondition_pinned`` /
# ``_validate_promotion_evidence``). This section REUSES that work's exact
# philosophy -- deterministic sha256 hashing, a ``{ok/ready, reason, ...}``
# fail-closed verdict shape, "unknown/matching base is trivially unchanged"
# semantics -- for a DIFFERENT, more general problem: a scientific rerun
# fans out across MANY workers (not one docx target), each of which must
# report a structurally-valid, hash-pinned completion record before an
# aggregator can trust the run as done.
#
# Deliberately THESIS-ALGORITHM-FREE: this module knows nothing about
# images/branches/ground-truth as scientific concepts. ``expected_counts``
# is an opaque caller-supplied ``{label: int}`` dict and ``output_schema``
# domains are caller-named -- the only thing enforced here is the STRUCTURE
# of the contract (hash pinning, fail-closed aggregation), never a specific
# pipeline's semantics. A thesis-project caller imports this module and
# supplies its own paths/counts/schema; see the module-level docstring's own
# discipline (pure validation, no DB, no network) -- this whole section is
# equally DB-free and synchronous, so it is trivially importable from a
# caller that has never touched Meridian's own database.
#
# Persistence is intentionally NOT owned here (mirrors ``pointers.py``'s own
# "injectable seam, no core-local store" stance for provenance): "one
# immutable manifest per run" is enforced via :func:`check_manifest_immutable`,
# a pure yes/no gate a caller's OWN storage layer calls before writing --
# never a table this module creates itself.
# ===========================================================================

EXECUTION_MANIFEST_SCHEMA_VERSION = 1

_MANIFEST_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

WORKER_STATUS_COMPLETE = "complete"
WORKER_STATUS_FAILED = "failed"
WORKER_STATUS_PARTIAL = "partial"
WORKER_STATUS_SKIPPED = "skipped"
WORKER_STATUSES = frozenset({
    WORKER_STATUS_COMPLETE, WORKER_STATUS_FAILED,
    WORKER_STATUS_PARTIAL, WORKER_STATUS_SKIPPED,
})

# Raw-domain vs DSE-domain (Design Space Exploration) schema tagging --
# every domain an ``output_schema`` declares must be tagged with one of
# these two kinds (see :func:`_validate_output_schema`); the tag is NOT
# optional once a caller declares ANY domains at all, so a per-worker
# record's outputs can never be silently ambiguous about which family of
# array it belongs to.
OUTPUT_DOMAIN_RAW = "raw"
OUTPUT_DOMAIN_DSE = "dse"
_KNOWN_OUTPUT_DOMAIN_KINDS = frozenset({OUTPUT_DOMAIN_RAW, OUTPUT_DOMAIN_DSE})

AGGREGATION_STATUS_COMPLETE = "complete"
AGGREGATION_STATUS_PARTIAL = "partial"
AGGREGATION_STATUS_FAILED = "failed"


class ExecutionManifestError(ValueError):
    """Raised when an execution manifest or worker completion record fails
    schema/safety validation."""


# ---------------------------------------------------------------------------
# Hashing / identity utilities -- mirror artifact_declaration.compute_base_sha256's
# "sha256 over current on-disk bytes, None for a missing file is a valid
# state, never an error" discipline, extended from ONE file to a whole set.
# ---------------------------------------------------------------------------

def hash_file_set(paths: list[str]) -> "dict[str, str | None]":
    """sha256 of each path's CURRENT on-disk bytes, keyed by the path as
    given. ``None`` for a missing/unreadable file is a valid state (e.g. an
    input the run has not fetched yet), never an error -- the exact same
    "unknown base" semantics :func:`meridian.artifact_declaration.compute_base_sha256`
    already documents, applied per-file across a whole set. Never raises."""
    out: "dict[str, str | None]" = {}
    for raw in paths or []:
        p = str(raw)
        try:
            path_obj = Path(p)
            if not path_obj.is_file():
                out[p] = None
                continue
            out[p] = hashlib.sha256(path_obj.read_bytes()).hexdigest()
        except OSError:
            out[p] = None
    return out


def aggregate_file_set_hash(file_hashes: "dict[str, str | None]") -> str:
    """ONE deterministic sha256 over a whole ``{path: hash}`` set (dict keys
    are sorted before hashing, so caller insertion order never matters) --
    the file-SET identity hash, distinct from any single file's own hash.
    Used for both the runner/source identity hash and the input file-set
    hash (:func:`build_execution_manifest`)."""
    return hashlib.sha256(
        _canonical_json(dict(sorted(file_hashes.items()))).encode("utf-8")
    ).hexdigest()


def capture_git_state(
    repo_dir: str, *, run: "Callable[[list[str]], str | None] | None" = None,
) -> dict[str, Any]:
    """Best-effort ``{"head": <sha>|None, "dirty_files": [...]}`` for
    ``repo_dir``.

    ``run`` is an injectable ``argv -> stdout|None`` seam (tests stub it; no
    real git binary/repo required to exercise this function) -- the default
    shells out to git via :mod:`subprocess`, guarded and timeout-bounded.
    ANY failure (not a repo, git missing, timeout, non-zero exit) degrades
    to ``{"head": None, "dirty_files": []}`` -- a best-effort identity
    fingerprint, never a hard error blocking manifest construction. Never
    raises.
    """
    def _default_run(argv: list[str]) -> "str | None":
        try:
            result = subprocess.run(
                argv, cwd=repo_dir, capture_output=True, text=True,
                timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    runner = run or _default_run
    head_out = runner(["git", "rev-parse", "HEAD"])
    head = head_out.strip() if head_out else None
    status_out = runner(["git", "status", "--porcelain"])
    dirty_files: list[str] = []
    if status_out:
        for line in status_out.splitlines():
            line = line.rstrip("\n")
            if len(line) > 3:
                dirty_files.append(line[3:].strip())
    return {"head": head, "dirty_files": sorted(set(f for f in dirty_files if f))}


# ---------------------------------------------------------------------------
# output_schema -- raw-domain vs DSE-domain explicit tagging.
# ---------------------------------------------------------------------------

def _validate_output_schema(raw: "dict[str, Any] | None") -> dict[str, Any]:
    """Normalize+validate a manifest's ``output_schema``. ``None``/absent
    normalizes to ``{"version": None, "domains": {}}`` -- no domains
    declared is a valid, backward-compatible state (domain enforcement in
    :func:`aggregate_worker_completions` is then simply skipped, mirroring
    this codebase's pervasive "absent means unknown, never retroactively
    enforced" convention). Once ANY domain is declared, each one MUST carry
    an explicit ``kind`` in {"raw", "dse"} -- raises
    :class:`ExecutionManifestError` otherwise; tagging is not optional once
    opted into."""
    if raw is None:
        return {"version": None, "domains": {}}
    if not isinstance(raw, dict):
        raise ExecutionManifestError("output_schema must be an object")
    domains_raw = raw.get("domains") or {}
    if not isinstance(domains_raw, dict):
        raise ExecutionManifestError("output_schema.domains must be an object")
    domains: dict[str, Any] = {}
    for name, spec in domains_raw.items():
        if not isinstance(spec, dict) or spec.get("kind") not in _KNOWN_OUTPUT_DOMAIN_KINDS:
            raise ExecutionManifestError(
                f"output_schema.domains[{name!r}] must declare kind in "
                f"{sorted(_KNOWN_OUTPUT_DOMAIN_KINDS)} -- raw vs DSE schema "
                "tagging is mandatory once any domain is declared"
            )
        fields = spec.get("fields")
        if fields is not None and not (
            isinstance(fields, list) and all(isinstance(f, str) for f in fields)
        ):
            raise ExecutionManifestError(
                f"output_schema.domains[{name!r}].fields must be a list of strings"
            )
        domains[str(name)] = {"kind": spec["kind"], "fields": list(fields) if fields else []}
    return {"version": raw.get("version"), "domains": domains}


# ---------------------------------------------------------------------------
# The manifest -- ONE immutable, hash-pinned object per (project_id,
# version, run_id).
# ---------------------------------------------------------------------------

def build_execution_manifest(
    *,
    project_id: str,
    version: str,
    run_id: str,
    runner_name: str,
    sprint_item_id: "str | None" = None,
    decision_ids: "list[str] | None" = None,
    runner_source_paths: "list[str] | None" = None,
    input_paths: "list[str] | None" = None,
    git_head: "str | None" = None,
    git_dirty_files: "list[str] | None" = None,
    python_version: "str | None" = None,
    pixi_env_fingerprint: "str | None" = None,
    runtime_fingerprint_extra: "dict[str, Any] | None" = None,
    config_fingerprint: "dict[str, Any] | None" = None,
    expected_counts: "dict[str, int] | None" = None,
    output_schema: "dict[str, Any] | None" = None,
    allow_partial: bool = False,
    hash_files: "Callable[[list[str]], dict[str, Any]] | None" = None,
) -> dict[str, Any]:
    """Build ONE project/version-scoped, hash-pinned execution manifest for
    a fan-out scientific rerun (or any other batch job).

    Captures every field the sprint spec calls for:

    * ``runner_identity`` -- ``runner_name`` plus per-file sha256 of
      ``runner_source_paths`` (:func:`hash_file_set`) and their combined
      ``source_set_hash`` (:func:`aggregate_file_set_hash`).
    * ``input_identity`` -- the SAME hashing applied to ``input_paths``
      (the input file-set/content hashes).
    * ``git_state`` -- caller-supplied ``git_head``/``git_dirty_files``
      (see :func:`capture_git_state` for a ready-made best-effort capture
      the caller can run in ITS OWN repo before calling this function --
      Meridian's own repo is not necessarily where the scientific run
      lives, so this function never shells out to git itself).
    * ``runtime_fingerprint`` / ``config_fingerprint`` -- Python/pixi/
      runtime and effective config/environment fingerprints. Screened via
      :func:`meridian.capability_manifest._check_no_secrets_or_local_paths`
      (REUSED, never reimplemented) so no secret-shaped string can land in
      shared manifest state -- "without secrets" per the sprint spec.
      Deliberately scoped to just these two sub-objects: ``runner_source_paths``
      / ``input_paths`` / ``git_dirty_files`` are LEGITIMATELY real
      filesystem paths (that is their entire purpose, and they are hashed,
      never stored raw as secrets would be) -- screening those would break
      the manifest's actual job, so only the fingerprint sub-objects (which
      have no business containing a path or a credential) are screened.
    * ``expected_counts`` -- an opaque ``{label: non-negative int}`` dict
      (e.g. ``{"images": 100, "branches": 12, "ground_truth": 50}``) --
      deliberately caller-named, never a fixed scientific vocabulary this
      module would have to understand.
    * ``output_schema`` -- see :func:`_validate_output_schema` (raw vs DSE
      domain tagging).
    * ``lineage`` -- ``sprint_item_id`` / ``decision_ids`` (sorted).
    * ``allow_partial`` -- whether :func:`aggregate_worker_completions` may
      ever report a valid failure-stage-subset ``"partial"`` verdict for
      this run, distinct from full production data.

    ``hash_files`` is an injectable ``paths -> {path: hash|None}`` seam
    (defaults to :func:`hash_file_set`) so tests never touch the real
    filesystem.

    Raises :class:`ExecutionManifestError` on any schema/safety violation.
    The returned dict carries ``manifest_hash`` -- a stable sha256 over
    every field EXCEPT ``created_at``/``manifest_hash`` themselves (mirrors
    :func:`executor_contract_hash`'s own wall-clock exclusion exactly, via
    the SAME :func:`_canonical_json`) -- and ``created_at``, a wall-clock
    timestamp carried for humans/audit but never hashed.
    """
    if not project_id or not str(project_id).strip():
        raise ExecutionManifestError("project_id is required")
    if not version or not str(version).strip():
        raise ExecutionManifestError("version is required")
    if not run_id or not str(run_id).strip():
        raise ExecutionManifestError("run_id is required")
    if not runner_name or not str(runner_name).strip():
        raise ExecutionManifestError("runner_name is required")

    hasher = hash_files or hash_file_set
    runner_hashes = hasher(list(runner_source_paths or []))
    input_hashes = hasher(list(input_paths or []))

    expected_counts_normalized: dict[str, int] = {}
    for k, v in (expected_counts or {}).items():
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ExecutionManifestError(
                f"expected_counts[{k!r}] must be a non-negative int, got {v!r}"
            )
        expected_counts_normalized[str(k)] = v

    runtime_fingerprint: dict[str, Any] = {
        "python_version": python_version,
        "pixi_env_fingerprint": pixi_env_fingerprint,
        **(runtime_fingerprint_extra or {}),
    }
    config_fp: dict[str, Any] = dict(config_fingerprint or {})

    try:
        _capability_manifest._check_no_secrets_or_local_paths(
            runtime_fingerprint, path="runtime_fingerprint",
        )
        _capability_manifest._check_no_secrets_or_local_paths(
            config_fp, path="config_fingerprint",
        )
    except _capability_manifest.CapabilityManifestError as exc:
        raise ExecutionManifestError(str(exc)) from exc

    from datetime import datetime, timezone  # noqa: PLC0415

    manifest: dict[str, Any] = {
        "schema_version": EXECUTION_MANIFEST_SCHEMA_VERSION,
        "run_id": str(run_id).strip(),
        "scope": {"project_id": project_id, "version": version},
        "lineage": {
            "sprint_item_id": sprint_item_id,
            "decision_ids": sorted(str(d) for d in (decision_ids or [])),
        },
        "runner_identity": {
            "name": runner_name.strip(),
            "source_hashes": dict(sorted(runner_hashes.items())),
            "source_set_hash": aggregate_file_set_hash(runner_hashes),
        },
        "input_identity": {
            "file_hashes": dict(sorted(input_hashes.items())),
            "file_set_hash": aggregate_file_set_hash(input_hashes),
        },
        "git_state": {
            "head": git_head,
            "dirty_files": sorted(set(git_dirty_files or [])),
        },
        "runtime_fingerprint": runtime_fingerprint,
        "config_fingerprint": config_fp,
        "expected_counts": expected_counts_normalized,
        "output_schema": _validate_output_schema(output_schema),
        "allow_partial": bool(allow_partial),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    manifest["manifest_hash"] = execution_manifest_hash(manifest)
    return manifest


def execution_manifest_hash(manifest: dict[str, Any]) -> str:
    """Stable sha256 over ``manifest``, EXCLUDING ``created_at``/
    ``manifest_hash`` themselves -- see :func:`build_execution_manifest`."""
    payload = {
        k: v for k, v in manifest.items() if k not in ("created_at", "manifest_hash")
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def check_manifest_immutable(
    existing_manifest: "dict[str, Any] | None", new_manifest: dict[str, Any],
) -> "tuple[bool, str | None]":
    """Fail-closed precondition for a caller's OWN persistence layer: is it
    safe to write ``new_manifest``, given whatever manifest (if any) is
    already on file for this ``run_id``/``project_id``/``version`` scope?

    This module does not own storage (see the module section docstring) --
    a caller's persistence layer supplies ``existing_manifest`` (its own
    prior read for the SAME scope) and this is the ONE shared gate so "one
    immutable manifest per run" is enforced identically everywhere it is
    checked, never re-derived ad hoc per call site.

    Returns ``(True, None)`` when there is no existing manifest yet (first
    write) OR the existing manifest's ``manifest_hash`` matches
    ``new_manifest``'s (an idempotent re-write of the SAME content is safe
    -- mirrors ``check_promotion_preconditions``'s own "matching hash is
    trivially fine" rule). Returns ``(False, reason)`` when a DIFFERENT
    manifest already exists for this run -- a caller must never silently
    overwrite it; a genuinely different run needs a NEW ``run_id``.
    """
    if existing_manifest is None:
        return True, None
    existing_scope = existing_manifest.get("scope") or {}
    new_scope = new_manifest.get("scope") or {}
    if (
        existing_manifest.get("run_id") != new_manifest.get("run_id")
        or existing_scope.get("project_id") != new_scope.get("project_id")
        or existing_scope.get("version") != new_scope.get("version")
    ):
        return False, (
            "existing_manifest is for a different run_id/project_id/version "
            "scope -- check_manifest_immutable must be called with the prior "
            "manifest for the SAME run, not an unrelated one"
        )
    if existing_manifest.get("manifest_hash") == new_manifest.get("manifest_hash"):
        return True, None
    return False, (
        f"a manifest already exists for run_id={new_manifest.get('run_id')!r} "
        f"with a DIFFERENT hash ({existing_manifest.get('manifest_hash')!r} != "
        f"{new_manifest.get('manifest_hash')!r}) -- manifests are immutable "
        "per run; plan a NEW run_id instead of mutating this one"
    )


# ---------------------------------------------------------------------------
# Per-worker completion records.
# ---------------------------------------------------------------------------

_WORKER_RECORD_REQUIRED_FIELDS = (
    "worker_id", "manifest_hash", "run_id", "status", "expected_keys",
    "row_counts", "output_hashes",
)


def build_worker_completion_record(
    manifest: dict[str, Any],
    *,
    worker_id: str,
    status: str,
    input_hash: "str | None" = None,
    config_hash: "str | None" = None,
    expected_keys: "list[str] | None" = None,
    row_counts: "dict[str, int] | None" = None,
    output_hashes: "dict[str, str] | None" = None,
    domain: "str | None" = None,
    error: "str | None" = None,
) -> dict[str, Any]:
    """Build ONE structurally-valid worker completion record, bound to
    ``manifest`` via its ``manifest_hash`` -- the hash a downstream
    aggregator/skip-existing check cross-references (:func:`check_skip_existing_worker`,
    :func:`aggregate_worker_completions`) so a record can never silently be
    reused against a DIFFERENT manifest than the one it was built for.

    ``status`` must be one of :data:`WORKER_STATUSES`. ``output_hashes`` is
    a ``{path: sha256_hex}`` map -- every value must be a valid 64-char hex
    digest. ``domain`` (optional) names which of the manifest's declared
    ``output_schema`` domains (raw/dse) this record's outputs belong to --
    schema-mismatch detection happens at aggregation time
    (:func:`aggregate_worker_completions`), not here (a single record has
    no way to know the full domain set without the manifest's context,
    which it already carries via ``manifest_hash`` alone -- re-validating
    domain membership here would require threading the full manifest object
    through every call site; the aggregator is the ONE place that already
    has both sides in scope).

    Raises :class:`ExecutionManifestError` on any schema violation. The
    returned dict carries ``record_hash`` -- a stable sha256 over the whole
    record (via :func:`_canonical_json`), useful as a durable dedup/audit
    key independent of ``manifest_hash`` alone.
    """
    if not worker_id or not str(worker_id).strip():
        raise ExecutionManifestError("worker_id is required")
    if status not in WORKER_STATUSES:
        raise ExecutionManifestError(
            f"status must be one of {sorted(WORKER_STATUSES)}, got {status!r}"
        )
    row_counts_normalized: dict[str, int] = {}
    for k, v in (row_counts or {}).items():
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ExecutionManifestError(
                f"row_counts[{k!r}] must be a non-negative int, got {v!r}"
            )
        row_counts_normalized[str(k)] = v
    output_hashes_normalized: dict[str, str] = {}
    for path, h in (output_hashes or {}).items():
        if not isinstance(h, str) or not _MANIFEST_SHA256_HEX_RE.match(h.strip().lower()):
            raise ExecutionManifestError(
                f"output_hashes[{path!r}] must be a 64-char hex sha256, got {h!r}"
            )
        output_hashes_normalized[str(path)] = h.strip().lower()

    record: dict[str, Any] = {
        "worker_id": str(worker_id).strip(),
        "manifest_hash": manifest.get("manifest_hash"),
        "run_id": manifest.get("run_id"),
        "status": status,
        "input_hash": input_hash,
        "config_hash": config_hash,
        "expected_keys": sorted(str(k) for k in (expected_keys or [])),
        "row_counts": row_counts_normalized,
        "output_hashes": dict(sorted(output_hashes_normalized.items())),
        "domain": domain,
        "error": error,
    }
    record["record_hash"] = hashlib.sha256(
        _canonical_json(record).encode("utf-8")
    ).hexdigest()
    return record


def validate_worker_completion_record(record: Any) -> "tuple[bool, str | None]":
    """Pure structural/shape validation for an ALREADY-BUILT worker
    completion record (e.g. one read back from a caller's own storage) --
    mirrors :func:`meridian.pointers.check_structural_validity`'s role for
    pointers exactly: shape/schema only, no manifest/hash MATCHING here
    (that is :func:`check_skip_existing_worker`'s job, which calls this
    function FIRST as one of its own gates). Never raises.

    Returns ``(True, None)`` when well-formed, ``(False, <reason>)``
    otherwise.
    """
    if not isinstance(record, dict):
        return False, "worker completion record must be an object"
    for field in _WORKER_RECORD_REQUIRED_FIELDS:
        if field not in record:
            return False, f"worker completion record missing required field: {field}"
    if record.get("status") not in WORKER_STATUSES:
        return False, (
            f"status must be one of {sorted(WORKER_STATUSES)}, got "
            f"{record.get('status')!r}"
        )
    if not isinstance(record.get("worker_id"), str) or not record["worker_id"].strip():
        return False, "worker_id must be a non-empty string"
    if not isinstance(record.get("manifest_hash"), str) or not record["manifest_hash"].strip():
        return False, "manifest_hash must be a non-empty string"
    if not isinstance(record.get("expected_keys"), list):
        return False, "expected_keys must be a list"
    if not isinstance(record.get("row_counts"), dict):
        return False, "row_counts must be an object"
    for k, v in record["row_counts"].items():
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            return False, f"row_counts[{k!r}] must be a non-negative int"
    output_hashes = record.get("output_hashes")
    if not isinstance(output_hashes, dict):
        return False, "output_hashes must be an object"
    for path, h in output_hashes.items():
        if not isinstance(h, str) or not _MANIFEST_SHA256_HEX_RE.match(h.strip().lower()):
            return False, f"output_hashes[{path!r}] is not a valid sha256 hex digest"
    return True, None


def check_skip_existing_worker(
    manifest: dict[str, Any],
    existing_record: "dict[str, Any] | None",
    *,
    current_input_hash: "str | None" = None,
    current_config_hash: "str | None" = None,
) -> "tuple[bool, str]":
    """Fail-closed skip-existing gate: "Only allow skip-existing when an
    existing worker record matches the current manifest/config/input hashes
    and passes structural validation" (sprint spec, verbatim).

    Returns ``(True, reason)`` ONLY when ALL of the following hold:

    * ``existing_record`` is present and passes :func:`validate_worker_completion_record`.
    * ``existing_record["status"] == "complete"`` -- skip-existing only ever
      makes sense over a worker that already genuinely SUCCEEDED; skipping
      a prior failed/partial/skipped attempt would silently accept
      incomplete work as done.
    * ``existing_record["manifest_hash"] == manifest["manifest_hash"]`` --
      the manifest (runner identity, inputs, config fingerprint, ...) has
      not changed since this worker last completed.
    * ``existing_record["input_hash"] == current_input_hash`` and
      ``existing_record["config_hash"] == current_config_hash`` -- this
      SPECIFIC worker's own input/config also has not changed (a worker's
      input can change even when the run-level manifest hash does not, if
      the manifest's inputs are run-wide but each worker consumes a slice
      of them).

    Any single mismatch returns ``(False, <specific reason>)`` -- fail
    closed, and always WITH a reason (never a bare "no"), since a re-run
    needs to know exactly why a stale record could not be trusted. Never
    raises.
    """
    if existing_record is None:
        return False, "no existing worker record on file -- nothing to skip"
    valid, err = validate_worker_completion_record(existing_record)
    if not valid:
        return False, f"existing worker record failed structural validation: {err}"
    if existing_record.get("status") != WORKER_STATUS_COMPLETE:
        return False, (
            f"existing worker record has status={existing_record.get('status')!r}, "
            "not 'complete' -- only a genuinely completed prior attempt can be skipped"
        )
    if existing_record.get("manifest_hash") != manifest.get("manifest_hash"):
        return False, (
            "existing worker record's manifest_hash does not match the "
            "CURRENT manifest -- the manifest changed since this worker last "
            "ran (different inputs/config/runner identity); re-run required"
        )
    if existing_record.get("input_hash") != current_input_hash:
        return False, (
            f"existing worker record's input_hash "
            f"({existing_record.get('input_hash')!r}) does not match the "
            f"current input_hash ({current_input_hash!r}) -- this worker's "
            "input changed since it last completed; re-run required"
        )
    if existing_record.get("config_hash") != current_config_hash:
        return False, (
            f"existing worker record's config_hash "
            f"({existing_record.get('config_hash')!r}) does not match the "
            f"current config_hash ({current_config_hash!r}) -- this worker's "
            "config changed since it last completed; re-run required"
        )
    return True, (
        "existing worker record matches the current manifest/config/input "
        "hashes and is structurally valid -- safe to skip"
    )


# ---------------------------------------------------------------------------
# Fail-closed aggregation.
# ---------------------------------------------------------------------------

def aggregate_worker_completions(
    manifest: dict[str, Any],
    records: "list[dict[str, Any]]",
    *,
    expected_worker_ids: "list[str]",
) -> dict[str, Any]:
    """Fail-closed aggregation over one run's per-worker completion records.

    Fails closed (``ok=False``, ``status="failed"``) on ANY of:

    * **missing** -- an id in ``expected_worker_ids`` with no record at all.
    * **duplicate** -- the SAME ``worker_id`` submitting more than one
      record (a race in a parallel rerun) -- regardless of whether the
      duplicates agree; two submissions for one worker is itself the
      violation.
    * **structurally invalid** -- a record failing
      :func:`validate_worker_completion_record`.
    * **hash-mismatched** -- a record's ``manifest_hash`` does not match
      ``manifest["manifest_hash"]`` (it was built against a stale/different
      manifest).
    * **schema-mismatched** -- ``manifest`` declares ``output_schema``
      domains but a record's ``domain`` is not one of them (only enforced
      when the manifest actually declared domains -- see
      :func:`_validate_output_schema`).
    * **empty** -- a record reporting ``status="complete"`` with NO output
      hashes and all-zero row counts (claims success but produced nothing).
    * **partial without opt-in** -- any ``status="partial"`` record when
      ``manifest["allow_partial"]`` is ``False``.
    * **no expectation declared** -- ``expected_worker_ids`` is empty. This
      is never vacuously "complete" (mirrors
      :func:`meridian.pointers._summarize_target_resolution`'s own "empty is
      never vacuously ready" rule) -- a caller must always declare how many
      workers this run should have produced.

    Distinguishes FULL PRODUCTION DATA from a VALID FAILURE-STAGE SUBSET:
    when ``manifest["allow_partial"]`` is ``True`` and the ONLY issues are
    missing/partial workers (no duplicate/hash-mismatch/schema-mismatch/
    empty/invalid records), the aggregation succeeds (``ok=True``) with
    ``status="partial"`` and ``is_full_production=False`` -- a legitimate,
    explicitly-accepted subset, never silently indistinguishable from a
    genuinely complete run (``status="complete"``, ``is_full_production=True``).

    Keeps raw-domain versus DSE-domain arrays explicitly schema-tagged in
    the returned ``domains`` rollup (each entry carries the manifest's own
    declared ``kind``).

    Exposes deterministic diagnostics: ``expected_count``/``observed_count``
    and a single, deterministically-chosen ``first_failing_worker`` (the
    lexicographically-first worker id across every problem category --
    stable across repeated calls with the SAME input, never order-of-
    iteration dependent).

    Never raises -- every violation is reported in the returned dict, never
    an exception (this is meant to run as a completion/provenance GATE, not
    something a caller wraps in try/except).
    """
    expected = sorted({str(w) for w in (expected_worker_ids or [])})
    manifest_hash = manifest.get("manifest_hash")
    allow_partial = bool(manifest.get("allow_partial"))
    domains_declared: dict[str, Any] = (manifest.get("output_schema") or {}).get("domains") or {}

    seen_worker_ids: dict[str, int] = {}
    structurally_invalid: list[str] = []
    structurally_invalid_worker_ids: set[str] = set()
    duplicate_workers: set[str] = set()
    valid_records: dict[str, dict[str, Any]] = {}

    for rec in records or []:
        if not isinstance(rec, dict):
            structurally_invalid.append("<malformed non-dict record>")
            continue
        raw_wid = rec.get("worker_id")
        wid_key = str(raw_wid) if isinstance(raw_wid, str) and raw_wid.strip() else "<missing worker_id>"
        seen_worker_ids[wid_key] = seen_worker_ids.get(wid_key, 0) + 1
        if seen_worker_ids[wid_key] > 1:
            duplicate_workers.add(wid_key)
        valid, err = validate_worker_completion_record(rec)
        if not valid:
            structurally_invalid.append(f"{wid_key}: {err}")
            structurally_invalid_worker_ids.add(wid_key)
            continue
        valid_records[wid_key] = rec

    observed_ids = sorted(seen_worker_ids)
    missing_workers = sorted(set(expected) - set(observed_ids))

    hash_mismatched: list[str] = []
    schema_mismatched: list[str] = []
    empty_output: list[str] = []
    partial_workers: list[str] = []
    counts_by_status: dict[str, int] = {s: 0 for s in sorted(WORKER_STATUSES)}
    domain_rollup: dict[str, dict[str, Any]] = {
        name: {"kind": spec.get("kind"), "complete_workers": 0, "rows": 0}
        for name, spec in domains_declared.items()
    }

    for wid_key, rec in valid_records.items():
        status = rec.get("status")
        counts_by_status[status] = counts_by_status.get(status, 0) + 1
        if rec.get("manifest_hash") != manifest_hash:
            hash_mismatched.append(wid_key)
        domain = rec.get("domain")
        if domains_declared:
            if domain not in domains_declared:
                schema_mismatched.append(wid_key)
            elif status == WORKER_STATUS_COMPLETE:
                domain_rollup[domain]["complete_workers"] += 1
                domain_rollup[domain]["rows"] += sum(rec.get("row_counts", {}).values())
        if status == WORKER_STATUS_COMPLETE:
            total_rows = sum(rec.get("row_counts", {}).values())
            if not rec.get("output_hashes") and total_rows == 0:
                empty_output.append(wid_key)
        if status == WORKER_STATUS_PARTIAL:
            partial_workers.append(wid_key)

    hash_mismatched.sort()
    schema_mismatched.sort()
    empty_output.sort()
    partial_workers.sort()

    has_hard_violation = bool(
        duplicate_workers or hash_mismatched or schema_mismatched
        or empty_output or structurally_invalid
    )

    problem_workers = sorted(
        set(missing_workers) | duplicate_workers | set(hash_mismatched)
        | set(schema_mismatched) | set(empty_output) | structurally_invalid_worker_ids
        | (set(partial_workers) if not allow_partial else set())
    )
    first_failing_worker = problem_workers[0] if problem_workers else None

    if not expected:
        status_verdict = AGGREGATION_STATUS_FAILED
        ok = False
        is_full_production = False
        reason = (
            "expected_worker_ids is empty -- aggregation refuses to guess how "
            "many workers this run should have produced (fail closed, never "
            "vacuously complete); declare the full expected worker id set"
        )
    elif not has_hard_violation and not missing_workers and not partial_workers:
        status_verdict = AGGREGATION_STATUS_COMPLETE
        ok = True
        is_full_production = True
        reason = (
            "every expected worker completed with matching hashes and no "
            "integrity violations -- full production data"
        )
    elif not has_hard_violation and allow_partial and valid_records and (missing_workers or partial_workers):
        status_verdict = AGGREGATION_STATUS_PARTIAL
        ok = True
        is_full_production = False
        reason = (
            f"allow_partial=True and this run has {len(missing_workers)} "
            f"missing / {len(partial_workers)} partial worker(s) with no "
            "integrity violations -- accepted as a VALID failure-stage "
            "subset, distinct from full production data"
        )
    else:
        status_verdict = AGGREGATION_STATUS_FAILED
        ok = False
        is_full_production = False
        reasons: list[str] = []
        if missing_workers:
            reasons.append(f"{len(missing_workers)} missing worker(s): {missing_workers}")
        if duplicate_workers:
            reasons.append(
                f"{len(duplicate_workers)} duplicate worker submission(s): "
                f"{sorted(duplicate_workers)}"
            )
        if hash_mismatched:
            reasons.append(f"{len(hash_mismatched)} hash-mismatched worker(s): {hash_mismatched}")
        if schema_mismatched:
            reasons.append(
                f"{len(schema_mismatched)} schema-mismatched worker(s): {schema_mismatched}"
            )
        if empty_output:
            reasons.append(
                f"{len(empty_output)} worker(s) reported complete with empty output: {empty_output}"
            )
        if structurally_invalid:
            reasons.append(
                f"{len(structurally_invalid)} structurally invalid record(s): {structurally_invalid}"
            )
        if partial_workers and not allow_partial:
            reasons.append(
                f"{len(partial_workers)} partial worker(s) but manifest.allow_partial "
                f"is False: {partial_workers}"
            )
        if not reasons:
            reasons.append("aggregation could not reach a complete/partial verdict")
        reason = "; ".join(reasons)

    return {
        "ok": ok,
        "status": status_verdict,
        "is_full_production": is_full_production,
        "reason": reason,
        "run_id": manifest.get("run_id"),
        "manifest_hash": manifest_hash,
        "expected_count": len(expected),
        "observed_count": len(observed_ids),
        "counts_by_status": counts_by_status,
        "missing_workers": missing_workers,
        "duplicate_workers": sorted(duplicate_workers),
        "hash_mismatched_workers": hash_mismatched,
        "schema_mismatched_workers": schema_mismatched,
        "empty_output_workers": empty_output,
        "partial_workers": partial_workers,
        "structurally_invalid_records": structurally_invalid,
        "first_failing_worker": first_failing_worker,
        "domains": domain_rollup,
        "worker_records": valid_records,
    }


def summarize_execution_manifest_aggregation(aggregation: dict[str, Any]) -> dict[str, Any]:
    """Compact projection of an :func:`aggregate_worker_completions` result
    -- mirrors :func:`summarize_contract_for_report`'s role for executor
    contracts: small enough to embed in a report/handoff row, still carries
    the hash + fail-closed verdict a downstream completion/provenance gate
    needs without re-deriving anything."""
    return {
        "run_id": aggregation.get("run_id"),
        "manifest_hash": aggregation.get("manifest_hash"),
        "status": aggregation.get("status"),
        "ok": aggregation.get("ok"),
        "is_full_production": aggregation.get("is_full_production"),
        "expected_count": aggregation.get("expected_count"),
        "observed_count": aggregation.get("observed_count"),
        "first_failing_worker": aggregation.get("first_failing_worker"),
    }
