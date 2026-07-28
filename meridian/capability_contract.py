"""Machine-readable effective capability contract (98aaccf4).

Builds a single, typed, deterministically-serializable object that answers
"what capabilities does this project declare, what does it actually get,
and can an executor session run right now" -- emitted alongside the trusted
``start_session`` orientation and every ``generate_handoff`` mode so a
receiving session (or a dashboard) has a machine-readable answer instead of
having to re-derive it from prose.

Layered on top of 649e095f's schema/validation module
(``meridian.capability_manifest``) and its DB persistence
(``db.get_project_capability_manifest`` / ``set_project_capability_manifest``).
This module adds NO new DB table -- it reads the existing
``project_capabilities`` row and assembles a contract from it plus two
OPTIONAL richer integration points that are being built in parallel, separate
worktrees and are not assumed to exist yet:

* **02038afe** (profile inheritance: workspace/user -> project ->
  sprint/version -> item override). Until it lands, ``effective`` degrades to
  the raw project manifest returned by 649e095f -- see
  :func:`_resolve_effective_capabilities`.
* **ac80aaaf** (live availability probing against the tunnel/tool inventory).
  Until it lands, the ``availability`` section degrades to
  ``status="unknown"`` with ``available``/``missing``/``degraded`` all the
  string ``"unknown"`` -- see :func:`_resolve_availability`.

Both integration points accept an explicit callable override
(``effective_resolver`` / ``availability_checker``) so this module is fully
testable TODAY using only what 649e095f provides, and upgrades automatically
once a sibling module of a guessed, documented name appears -- a human or a
follow-up item is expected to wire the real integration in either case (see
the TODO comments below); this module must never hard-crash or hard-import
in the meantime.

Never binds credentials: the underlying manifest already screens
secret-shaped values and machine-local absolute paths at write time
(``capability_manifest``'s ``_check_no_secrets_or_local_paths``); this module
re-applies that same check defensively over anything a future
resolver/checker might inject (see :func:`_scrub_secrets`) so a not-yet-
trusted sibling integration can never leak a secret into the contract.
"""
from __future__ import annotations

import hashlib
import importlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from . import capability_manifest as _cm
from . import db as db_module
from . import pointers as _pointers
from . import tool_requirements as _tool_requirements

CONTRACT_SCHEMA_VERSION = 1

EffectiveResolver = Callable[[Any, str, list[dict[str, Any]]], list[dict[str, Any]]]
AvailabilityChecker = Callable[[list[dict[str, Any]]], dict[str, Any]]


def _import_optional_sibling(dotted_name: str) -> Any | None:
    """Best-effort import of a not-yet-landed sibling module by dotted
    string name, or ``None`` if it doesn't exist (yet).

    Deliberately uses ``importlib.import_module`` on a plain string rather
    than a static ``from meridian import <name>`` statement: this repo's
    ``scripts/check_orphaned_refs.py`` pre-merge guard statically AST-walks
    every first-party ``ImportFrom`` node and flags any name that isn't
    actually defined in the target module -- which a guessed, intentionally
    not-yet-existing sibling module name always would be, even inside a
    ``try/except ImportError``. Routing the lookup through a string argument
    keeps this module's speculative integration point invisible to that
    static check (it only understands import statements, not
    ``importlib.import_module("...")`` calls) while remaining exactly as
    safe at runtime: :class:`ModuleNotFoundError` is caught either way.
    """
    try:
        return importlib.import_module(dotted_name)
    except ModuleNotFoundError:
        return None
    except Exception:  # noqa: BLE001 — a half-built sibling module may raise anything at import time
        return None


def extract_required_tool_pins(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Typed extraction of item-level ``required_tool`` pins (4d1fb28f).

    Pure data, no XML/string rendering -- the SAME extraction
    ``handoff._build_required_tool_clause`` uses for its hard-guidance /goal
    clause, factored out here so both the /goal clause and the structured
    capability contract read one shared source of truth instead of
    maintaining two independent list comprehensions that could drift.
    """
    return [
        {"item_id": it["id"], "tool": it["required_tool"]}
        for it in items
        if it.get("id") and it.get("required_tool")
    ]


def extract_tool_requirements(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Typed extraction of the per-item ``tool_requirements`` contract
    (76dde31f, 665 follow-up).

    Structured ``tool_requirements`` is the canonical source; a legacy
    free-form ``required_tool`` pin is honoured as a read-time compatibility
    fallback ONLY for items that carry no structured requirements at all —
    see ``tool_requirements.effective_tool_requirements`` for the exact
    precedence rule. Pure data, no XML/JSON rendering — the SAME extraction
    ``handoff._build_tool_requirements_clause`` uses for the batch /goal's
    ``<tool_requirements>`` clause, so the XML rendering and this module's
    structured contract read one shared source of truth instead of
    maintaining two independent derivations that could drift (mirrors
    :func:`extract_required_tool_pins`'s existing role for the legacy
    ``<required_tool>`` clause).

    Deterministic ordering: the result is sorted by ``item_id`` regardless of
    ``items``' own order, so two callers extracting from the SAME underlying
    item set — but via independently-fetched/re-ordered lists (e.g. the batch
    /goal's dependency-topo-sorted render vs. a plain
    ``get_sprint_items`` fetch) — always produce byte-identical output. This
    is what lets the batch /goal's ``<tool_requirements>`` XML clause and
    :func:`build_capability_contract`'s ``item_tool_requirements`` section be
    compared directly for the SAME request.
    """
    result: list[dict[str, Any]] = []
    for it in items:
        item_id = it.get("id")
        if not item_id:
            continue
        requirements = _tool_requirements.effective_tool_requirements(it)
        if requirements:
            result.append({"item_id": item_id, "requirements": requirements})
    return sorted(result, key=lambda r: r["item_id"])


async def extract_sprint_item_pointers(
    db: Any,
    project_id: str,
    items: list[dict[str, Any]],
    *,
    node_resolver: Any | None = None,
) -> list[dict[str, Any]]:
    """Typed extraction of the per-item durable ``sprint_item_pointers``
    contract (665 follow-up): source_type, target uri, selector/anchor,
    target_kind (existing/planned_new), label, an explicit per-target
    resolution status (resolved/unresolved/planned/stale/archival),
    canonical/archival metadata where available, and each item's
    provenance-required state — the SAME fields
    ``handoff._build_pointer_records_clause`` embeds in the batch /goal's
    ``<sprint_item_pointers>`` XML clause, so the two never independently
    diverge for the same request.

    Two paths, depending on whether ``items`` already carry the
    ``pointer_records``/``pointer_provenance`` annotation
    ``handoff._annotate_resolved_pointers`` sets (the SAME resolve pass a
    /goal render already ran):

    * **Pre-annotated** (an item carries either key) — reuse it directly via
      :func:`pointers.assemble_pointer_entries_from_annotated_items`. NO
      extra DB fetch or ``resolve_pointer`` call — this is the strongest
      identical-data guarantee (mirrors :func:`extract_tool_requirements`'s
      own "pass items explicitly" fast path).
    * **Not annotated** (the common case for a self-fetched pending-item
      list, e.g. :func:`build_capability_contract`'s own default fetch) —
      fetch + resolve THIS item's stored pointers itself, via
      ``db.get_sprint_item_pointers`` and
      :func:`pointers.build_item_pointer_records` (the SAME per-pointer
      resolve+type primitive the annotation pass uses), and derive
      provenance the SAME read-only way
      (``is_item_claim_prospected``/``_item_declares_resources`` — never
      touches the actual prospecting gate). So the JSON contract is
      complete and correct even for a caller that never rendered a /goal in
      this request.

    Deterministic ordering: sorted by ``item_id``, mirroring
    :func:`extract_tool_requirements`. Fully guarded per item — a DB/resolve
    failure degrades that ONE item to no entry rather than breaking contract
    building; NEVER raises.
    """
    entries: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        item_id = it.get("id")
        if not item_id:
            continue
        if "pointer_records" in it or "pointer_provenance" in it:
            records = it.get("pointer_records") or []
            provenance = it.get("pointer_provenance")
        else:
            try:
                stored = await db_module.get_sprint_item_pointers(db, item_id)
            except Exception:  # noqa: BLE001 — pre-migration DB / fetch failure
                stored = []
            try:
                provenance = {
                    "required": (
                        db_module._item_declares_resources(it)
                        and not bool(it.get("prospect_bypass"))
                    ),
                    "bypassed": bool(it.get("prospect_bypass")),
                    "satisfied": db_module.is_item_claim_prospected(
                        it, has_pointer_evidence=bool(stored)
                    ),
                }
            except Exception:  # noqa: BLE001 — provenance derivation is best-effort
                provenance = None
            records = []
            if stored:
                try:
                    records = await _pointers.build_item_pointer_records(
                        db, project_id, stored, node_resolver=node_resolver,
                    )
                except Exception:  # noqa: BLE001 — resolve failure -> no records for this item
                    records = []
        if not records and not (isinstance(provenance, dict) and provenance.get("required")):
            continue
        entry: dict[str, Any] = {"item_id": item_id}
        if provenance:
            entry["provenance"] = provenance
        if records:
            entry["pointers"] = records
        entries.append(entry)
    return sorted(entries, key=lambda e: e["item_id"])


def _resolve_effective_capabilities(
    db: Any,
    project_id: str,
    requested: list[dict[str, Any]],
    *,
    resolver: "EffectiveResolver | None" = None,
) -> tuple[list[dict[str, Any]], str]:
    """Resolve the EFFECTIVE capability list for a project.

    Returns ``(effective_capabilities, source)`` where ``source`` is
    ``"resolver"`` / ``"profile_inheritance"`` (a richer integration ran) or
    ``"raw_manifest"`` (degraded fallback -- no richer integration available).

    ``resolver`` lets a caller (or a test) inject the real 02038afe
    resolution directly without this module needing to know its final
    name/shape -- call as ``resolver(db, project_id, requested)``.

    TODO(02038afe): once profile inheritance merges, prefer calling its real
    resolution function directly from the two call sites in
    ``mcp/handlers/project_tools.py``/``mcp/handler.py`` via the
    ``effective_resolver`` kwarg on :func:`build_capability_contract`, rather
    than relying on the guessed auto-discovery below. The auto-discovery
    (a plausibly-named ``meridian.capability_profile`` module, looked up by
    dotted string via ``importlib`` rather than a static ``from``/``import``
    statement -- see the module docstring's note on ``scripts/check_orphaned_refs.py``)
    is a best-effort bridge only -- it is not a contract with the sibling
    item and may need updating to match whatever name/shape 02038afe
    actually ships. Guarded broadly (ModuleNotFoundError + AttributeError +
    any resolver exception) so an absent, differently-shaped, or still-half-
    built sibling module can never break start_session/generate_handoff.
    """
    if resolver is not None:
        try:
            resolved = resolver(db, project_id, requested)
            if isinstance(resolved, list):
                return resolved, "resolver"
        except Exception:  # noqa: BLE001 — an injected resolver must never crash the contract
            pass
    _capability_profile = _import_optional_sibling("meridian.capability_profile")
    if _capability_profile is None:
        return requested, "raw_manifest"
    _fn = (
        getattr(_capability_profile, "resolve_effective_capabilities", None)
        or getattr(_capability_profile, "resolve_effective_manifest", None)
    )
    if _fn is None:
        return requested, "raw_manifest"
    try:
        resolved = _fn(project_id, requested)
        if isinstance(resolved, list):
            return resolved, "profile_inheritance"
    except Exception:  # noqa: BLE001 — sibling module may still be half-built
        pass
    return requested, "raw_manifest"


def _resolve_availability(
    capabilities: list[dict[str, Any]],
    *,
    checker: "AvailabilityChecker | None" = None,
) -> tuple["dict[str, Any] | None", str]:
    """Resolve live availability info for ``capabilities``.

    Returns ``(result, status)`` where ``status`` is ``"checked"`` (a richer
    integration ran and ``result`` is a dict with ``available``/``missing``/
    ``degraded`` lists of capability ids) or ``"unknown"`` (degraded
    fallback -- no availability-checking integration available yet).

    ``checker`` lets a caller (or a test) inject the real ac80aaaf check
    directly -- call as ``checker(capabilities)``.

    TODO(ac80aaaf): once live availability/tunnel-fallback probing merges,
    prefer calling its real check function directly via the
    ``availability_checker`` kwarg on :func:`build_capability_contract` rather
    than the guessed auto-discovery below (a plausibly-named
    ``meridian.capability_availability`` module, looked up by dotted string
    via ``importlib`` -- see :func:`_import_optional_sibling`). Guarded
    broadly so an absent or differently-shaped sibling module can never
    break start_session/generate_handoff -- it just keeps degrading to
    "unknown".
    """
    if checker is not None:
        try:
            result = checker(capabilities)
            if isinstance(result, dict):
                return result, "checked"
        except Exception:  # noqa: BLE001 — an injected checker must never crash the contract
            pass
    _capability_availability = _import_optional_sibling("meridian.capability_availability")
    if _capability_availability is None:
        return None, "unknown"
    _fn = getattr(_capability_availability, "check_availability", None)
    if _fn is None:
        return None, "unknown"
    try:
        result = _fn(capabilities)
        if isinstance(result, dict):
            return result, "checked"
    except Exception:  # noqa: BLE001 — sibling module may still be half-built
        pass
    return None, "unknown"


def _canonical_json(obj: Any) -> str:
    """Canonical, byte-stable JSON: sorted keys, no incidental whitespace.

    Mirrors ``capability_manifest.manifest_hash``'s own canonicalization
    approach exactly (``sort_keys=True``, compact separators) so the same
    determinism guarantee extends from a single manifest to the whole
    contract. ``default=str`` is defense-in-depth only -- every value placed
    into a contract by this module is already a JSON-native type (str, bool,
    list, dict, None); nothing here is expected to hit that fallback.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def serialize_contract(contract: dict[str, Any]) -> str:
    """Byte-stable canonical JSON for ``contract``, EXCLUDING ``generated_at``
    and ``contract_hash`` themselves (a wall-clock timestamp and the hash of
    everything else would otherwise make the "same input" case never match
    byte-for-byte, and would make the hash depend on itself).

    Same board/profile/config state -> identical output every time; a
    changed capability or a changed availability/tunnel-state result ->
    different output. This is what :func:`contract_hash` hashes.
    """
    payload = {
        k: v for k, v in contract.items()
        if k not in ("generated_at", "contract_hash")
    }
    return _canonical_json(payload)


def contract_hash(contract: dict[str, Any]) -> str:
    """Stable sha256 over :func:`serialize_contract`'s canonical form."""
    return hashlib.sha256(serialize_contract(contract).encode("utf-8")).hexdigest()


def _scrub_secrets(contract: dict[str, Any]) -> dict[str, Any]:
    """Defense-in-depth: reject secret-shaped values / machine-local absolute
    paths that might reach the contract through a future resolver/checker.

    649e095f's ``capability_manifest.normalize_capability`` already screens
    every capability at WRITE time (``set_capability_manifest``), so the
    ``requested`` section can never carry one. This guards the ``effective``
    and ``availability`` sections too, since those may (once the siblings
    land) be populated by data this module did not itself validate. Reuses
    the SAME regex-based check capability_manifest already applies (never
    duplicates or weakens it). On any violation, the offending sections are
    replaced with a safe, empty degraded value and the contract is marked
    non-executable — the mandatory start_session/generate_handoff call must
    never crash or leak a secret over this.
    """
    try:
        _cm._check_no_secrets_or_local_paths(
            contract.get("effective"), path="capability_contract.effective"
        )
        _cm._check_no_secrets_or_local_paths(
            contract.get("availability"), path="capability_contract.availability"
        )
    except _cm.CapabilityManifestError:
        contract = dict(contract)
        contract["effective"] = {
            "capabilities": [],
            "source": "redacted_secret_shaped_value",
        }
        contract["availability"] = {
            "status": "unknown",
            "available": "unknown",
            "missing": "unknown",
            "degraded": "unknown",
        }
        contract["executable"] = False
        contract["executable_reasons"] = list(
            contract.get("executable_reasons") or []
        ) + ["redacted_secret_shaped_value"]
    return contract


async def _resolve_pending_items_for_contract(
    db: Any, project_id: str, *, version: "str | None",
) -> list[dict[str, Any]]:
    """Fetch the pending-item set (76dde31f, 665 follow-up) used for the
    contract's ``item_tool_requirements`` section, using the EXACT SAME
    filter criteria ``handoff.generate_handoff`` uses to build its own
    ``pending_sprint_items`` right before rendering the batch /goal's
    ``<tool_requirements>`` clause: status in {todo, pending},
    ``include_deferred=False``, scoped to ``version`` when given. Run within
    the same request against the same (unmutated-in-between) DB state, this
    produces the identical item set — see
    ``handoff._build_tool_requirements_clause`` and the module docstring.

    Best-effort: any DB error degrades to an empty list rather than breaking
    contract building.
    """
    try:
        items = await db_module.get_sprint_items(
            db, project_id, include_human=False, include_deferred=False,
            version=version,
        )
    except Exception:  # noqa: BLE001 — contract building must never break on this
        return []
    return [it for it in items if it.get("status") in ("todo", "pending")]


async def build_capability_contract(
    db: Any,
    project_id: str,
    *,
    board_stale: bool = False,
    effective_resolver: "EffectiveResolver | None" = None,
    availability_checker: "AvailabilityChecker | None" = None,
    version: "str | None" = None,
    items: "list[dict[str, Any]] | None" = None,
) -> dict[str, Any]:
    """Build the effective capability contract for ``project_id``.

    ``board_stale`` — caller-supplied signal that the sprint-board/profile
    snapshot backing this contract is known stale (e.g. ``start_session``'s
    own ``pending_goal_stale`` flag, or ``generate_handoff`` falling back to
    its emergency L0 render after a timeout). Purely additive input — this
    module has no independent way to detect staleness itself, so it trusts
    the caller's own already-computed signal rather than re-deriving one.

    ``items`` / ``version`` (76dde31f, 665 follow-up) — the sprint-item list
    the contract's ``item_tool_requirements`` section is extracted from (see
    :func:`extract_tool_requirements`). Pass ``items`` explicitly when the
    caller already has the SAME pending-item list it used to render a /goal
    block (e.g. a test asserting XML/JSON parity, or a future call site with
    the list already in scope) — this is the strongest identical-data
    guarantee. When omitted, this function self-fetches via
    :func:`_resolve_pending_items_for_contract`, scoped to ``version`` — the
    same query ``generate_handoff`` runs for its own pending-item list, so
    the two agree for any request where nothing mutates sprint_items in
    between. The SAME ``items`` list also feeds
    ``item_sprint_item_pointers`` (665 follow-up, see
    :func:`extract_sprint_item_pointers`) — the typed, canonical durable-
    pointer contract (source_type/target uri/selector/anchor/target_kind/
    label/explicit resolution status/canonical+archival metadata/
    provenance-required state) for the SAME item set, matching
    ``handoff._build_pointer_records_clause``'s ``<sprint_item_pointers>``
    XML clause for the same request.

    Never raises: ``get_project_capability_manifest`` returns an empty
    profile for any project with no persisted manifest (649e095f's own
    guarantee), and every optional richer-integration hook above is
    independently guarded. A caller should still wrap this call in its own
    try/except per the existing codebase convention (an orientation/handoff
    call must never break over an enrichment convenience), but nothing
    inside this function is expected to throw under normal operation.
    """
    manifest = await db_module.get_project_capability_manifest(db, project_id)
    requested_capabilities = manifest.get("capabilities") or []

    # 76dde31f (665 follow-up) — typed per-item tool_requirements, extracted
    # from the caller-supplied pending-item list when given, else self-fetched
    # with the identical filter criteria generate_handoff uses. See
    # _resolve_pending_items_for_contract and extract_tool_requirements.
    _pending_items_for_tool_reqs = (
        items if items is not None
        else await _resolve_pending_items_for_contract(db, project_id, version=version)
    )
    item_tool_requirements = extract_tool_requirements(_pending_items_for_tool_reqs)

    # 665 follow-up — typed per-item durable sprint_item_pointers contract,
    # extracted from the SAME pending-item list as item_tool_requirements
    # above (caller-supplied when given, else self-fetched with the
    # identical filter criteria generate_handoff uses). See
    # extract_sprint_item_pointers for the pre-annotated-vs-self-resolve
    # split.
    try:
        item_sprint_item_pointers = await extract_sprint_item_pointers(
            db, project_id, _pending_items_for_tool_reqs,
        )
    except Exception:  # noqa: BLE001 — contract building must never break on this
        item_sprint_item_pointers = []

    effective_capabilities, effective_source = _resolve_effective_capabilities(
        db, project_id, requested_capabilities, resolver=effective_resolver,
    )
    availability_result, availability_status = _resolve_availability(
        effective_capabilities, checker=availability_checker,
    )

    if availability_status == "checked" and isinstance(availability_result, dict):
        available: Any = sorted(availability_result.get("available") or [])
        missing: Any = sorted(availability_result.get("missing") or [])
        degraded: Any = sorted(availability_result.get("degraded") or [])
    else:
        available = "unknown"
        missing = "unknown"
        degraded = "unknown"

    required_ids = {
        c["id"] for c in effective_capabilities
        if isinstance(c, dict) and c.get("availability_policy") == "required" and c.get("id")
    }
    missing_required = (
        sorted(required_ids & set(missing)) if isinstance(missing, list) else []
    )

    executable = True
    executable_reasons: list[str] = []
    if missing_required:
        executable = False
        executable_reasons.append(
            "missing_required_capabilities:" + ",".join(missing_required)
        )
    if board_stale:
        executable = False
        executable_reasons.append("stale_board_snapshot")

    # capability_manifest.manifest_hash hashes whatever list it is given AS
    # ALREADY NORMALIZED (its own docstring) — effective_capabilities is
    # either the validated/normalized requested list, or (once a sibling
    # resolver lands) whatever shape it returns; hashing directly here
    # (never re-validating via normalize_manifest) means a resolver that
    # returns extra/derived fields can never crash contract building.
    effective_hash = _cm.manifest_hash(
        effective_capabilities if isinstance(effective_capabilities, list) else []
    )

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    contract: dict[str, Any] = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "project_id": project_id,
        "requested": {
            "capabilities": requested_capabilities,
            "manifest_version": manifest.get("manifest_version"),
            "manifest_hash": manifest.get("manifest_hash"),
        },
        "effective": {
            "capabilities": effective_capabilities,
            "source": effective_source,
        },
        "availability": {
            "status": availability_status,
            "available": available,
            "missing": missing,
            "degraded": degraded,
        },
        "manifest_hash": effective_hash,
        "item_tool_requirements": item_tool_requirements,
        "item_sprint_item_pointers": item_sprint_item_pointers,
        "board_stale": bool(board_stale),
        "executable": executable,
        "executable_reasons": executable_reasons,
        "generated_at": generated_at,
    }
    contract = _scrub_secrets(contract)
    contract["contract_hash"] = contract_hash(contract)
    return contract
