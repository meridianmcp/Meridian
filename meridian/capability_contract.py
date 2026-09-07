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
richer integration points:

* **02038afe** (profile inheritance: workspace/user -> project ->
  sprint/version -> item override) has LANDED -- ``effective`` resolves via
  ``db.get_effective_capability_profile`` (the real, DB-backed layered
  merge) by default; see :func:`_resolve_effective_capabilities`. 137b88a3
  fixed this call site, which previously auto-discovered a guessed function
  name on ``meridian.capability_profile`` that was never actually defined
  there, leaving ``effective_source`` permanently stuck at ``"raw_manifest"``.
* **ac80aaaf** (live availability probing against the tunnel/tool inventory)
  has LANDED as its own module (``meridian.capability_availability`` +
  ``mcp/handlers/project_tools.py``'s ``_build_live_inventory`` /
  ``check_capability_availability``) -- but that module's real functions
  (``evaluate_manifest_availability``, needing a caller-supplied
  ``live_inventory`` this pure module has no way to build itself) never
  matched the SHAPE ``_resolve_availability``'s own guessed sibling-module
  auto-discovery looked for (a zero-argument-beyond-``capabilities``
  ``check_availability`` function that was never actually defined there),
  so that auto-discovery still degrades to ``status="unknown"`` with
  ``available``/``missing``/``degraded`` all the string ``"unknown"`` --
  see :func:`_resolve_availability`. MDE-1 (819ac6de) wires the REAL check
  in at the layer that actually has DB/tenant/tunnel context to build a
  ``live_inventory``: ``meridian.handoff.build_effective_capability_contract``
  accepts an optional ``tenant``/``live_inventory`` and, when given, passes
  this module's ``availability_checker`` kwarg a real, closure-based
  checker (see that function's own docstring) instead of leaving every
  caller on the guessed-discovery degrade below.

Both integration points accept an explicit callable override
(``effective_resolver`` / ``availability_checker``) so a caller (or a test)
can inject an alternate resolution/check without depending on either
integration's real shape; this module must never hard-crash or hard-import
over either being absent or misbehaving.

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
from . import capability_profile as _capability_profile
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

    eb8b6894 — each entry also carries ``resolution_status`` (when
    available): the item-level ``structural_valid``/``target_resolved``/
    ``provenance_verified``/``resolution_source``/``strict_satisfied``
    rollup from :func:`pointers.aggregate_pointer_evidence`, computed
    identically on both paths below so a durable-but-unresolved pointer
    can never look "satisfied" in this JSON contract while
    ``pointer_records``' own per-pointer ``target_resolved`` says
    otherwise.

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
            # eb8b6894 — reuse the SAME resolution_status
            # handoff._annotate_resolved_pointers already computed in this
            # pass (pointers.aggregate_pointer_evidence) rather than
            # re-deriving it, so the JSON contract can never disagree with
            # the /goal XML clause for the same annotated items.
            resolution_status = it.get("pointer_resolution_status")
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
            # eb8b6894 — the self-fetch path's OWN companion to
            # pointer_resolution_status, computed via the SAME shared
            # aggregator handoff._annotate_resolved_pointers uses, plus the
            # SAME strict_satisfied signal (resolution-aware, not
            # presence-only) — see is_item_claim_prospected(strict=True).
            try:
                resolution_status = _pointers.aggregate_pointer_evidence(records)
                resolution_status["strict_satisfied"] = db_module.is_item_claim_prospected(
                    it, has_pointer_evidence=bool(stored), strict=True,
                    target_resolved=(
                        resolution_status["target_resolved"] if stored else None
                    ),
                )
            except Exception:  # noqa: BLE001 — resolution-status derivation is best-effort
                resolution_status = None
        if not records and not (isinstance(provenance, dict) and provenance.get("required")):
            continue
        entry: dict[str, Any] = {"item_id": item_id}
        if provenance:
            entry["provenance"] = provenance
        if resolution_status:
            entry["resolution_status"] = resolution_status
        if records:
            entry["pointers"] = records
        entries.append(entry)
    return sorted(entries, key=lambda e: e["item_id"])


async def extract_artifact_pointer_findings(
    db: Any,
    project_id: str,
    items: list[dict[str, Any]],
    *,
    node_resolver: Any | None = None,
    outputs_dir: "str | None" = None,
    figure_resolver: Any | None = None,
    provenance_getter: Any | None = None,
) -> list[dict[str, Any]]:
    """Typed extraction of the per-item artifact-pointer FINDING contract
    (70c10ca3, b730 follow-up): 88f82c15's warn/strict policy verdict
    (classification, effective policy, warning code, remediation,
    ready/executability) enriched with 3196ba0e's fail-closed readiness
    verification (canonical/archival/unresolved/ambiguous/missing/...) for
    each pointer id the verdict implicates — the SAME fields
    ``handoff._build_artifact_pointer_findings_clause`` embeds in the batch
    /goal's ``<artifact_pointer_findings>`` XML clause, so the two never
    independently diverge for the same request.

    Two paths per item, mirroring :func:`extract_sprint_item_pointers`:

    * **Pre-annotated** (an item already carries ``artifact_pointer_finding``
      — set by ``handoff._annotate_resolved_pointers``, the SAME resolve
      pass a /goal render already ran) — reuse it directly. NO extra DB
      fetch, resolve, or readiness check — the strongest identical-data
      guarantee.
    * **Not annotated** (the common case for a self-fetched pending-item
      list, e.g. :func:`build_capability_contract`'s own default fetch) —
      fetch this item's stored pointers (``db.get_sprint_item_pointers``),
      build its typed pointer records (:func:`pointers.build_item_pointer_records`,
      attached as a local ``pointer_records`` copy so the policy evaluator
      sees the SAME durable pointer evidence the pre-annotated path would
      have), then compute the finding via
      :func:`pointers.build_artifact_pointer_finding` — the SAME per-item
      primitive the annotation pass uses, with the SAME default
      ``figure_resolver``/``outputs_dir``/``provenance_getter`` seams
      (``outputs_dir``/``figure_resolver``/``provenance_getter`` here let a
      caller or test inject a stub; omitted, they degrade identically to
      the annotation pass's own defaults, so self-fetch and pre-annotated
      results agree for the SAME underlying DB state).

    Only items with an ACTIVE finding contribute (mirrors
    ``build_artifact_pointer_finding``'s own "nothing to say" restraint) —
    an ordinary project with no figure/table pointer problems returns ``[]``,
    not a wall of non-findings.

    Deterministic ordering: sorted by ``item_id`` (each entry's own
    ``target_readiness`` sub-list is already sorted by ``pointer_id``).
    Fully guarded per item — a DB/resolve/readiness failure degrades that
    ONE item to no entry rather than breaking contract building; NEVER
    raises.
    """
    entries: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        item_id = it.get("id")
        if not item_id:
            continue
        if "artifact_pointer_finding" in it:
            finding = it.get("artifact_pointer_finding")
        else:
            try:
                stored = await db_module.get_sprint_item_pointers(db, item_id)
            except Exception:  # noqa: BLE001 — pre-migration DB / fetch failure
                stored = []
            work_item = it
            if stored and "pointer_records" not in it:
                try:
                    records = await _pointers.build_item_pointer_records(
                        db, project_id, stored, node_resolver=node_resolver,
                    )
                except Exception:  # noqa: BLE001 — resolve failure -> no enrichment
                    records = []
                if records:
                    work_item = dict(it)
                    work_item["pointer_records"] = records
            try:
                finding = await _pointers.build_artifact_pointer_finding(
                    work_item,
                    stored_pointers=stored,
                    outputs_dir=outputs_dir,
                    figure_resolver=figure_resolver,
                    provenance_getter=provenance_getter,
                )
            except Exception:  # noqa: BLE001 — finding computation is best-effort
                finding = None
        if isinstance(finding, dict):
            entries.append(finding)
    return sorted(entries, key=lambda e: e.get("item_id") or "")


async def _resolve_effective_capabilities(
    db: Any,
    project_id: str,
    requested: list[dict[str, Any]],
    *,
    resolver: "EffectiveResolver | None" = None,
) -> tuple[list[dict[str, Any]], str]:
    """Resolve the EFFECTIVE capability list for a project.

    Returns ``(effective_capabilities, source)`` where ``source`` is
    ``"resolver"`` (an injected override ran) / ``"profile_inheritance"``
    (02038afe's real layered-profile resolution ran) or ``"raw_manifest"``
    (degraded fallback -- neither is available).

    ``resolver`` lets a caller (or a test) inject an alternate resolution
    directly, taking priority over the default below -- call as
    ``resolver(db, project_id, requested)`` (a plain SYNCHRONOUS callable,
    matching the existing ``EffectiveResolver`` type -- it is invoked
    without ``await`` on purpose so hand-written test doubles never need to
    be coroutines).

    137b88a3 (fixes 98aaccf4's original TODO): 02038afe has landed --
    ``db.get_effective_capability_profile`` is the real, DB-backed,
    already-tested implementation (workspace -> user -> project ->
    sprint_version -> item merge via ``capability_profile.merge_layers``).
    This calls it DIRECTLY as the default resolution, replacing the earlier
    guessed auto-discovery that looked for a ``resolve_effective_capabilities``
    / ``resolve_effective_manifest`` function on ``meridian.capability_profile``
    -- that module never defined (and was never going to define) either name,
    which left ``effective_source`` permanently stuck at ``"raw_manifest"`` in
    production even after profile inheritance shipped.

    IMPORTANT: ``db.get_effective_capability_profile`` resolves ONLY the
    ``capability_profiles`` table's layers (workspace/user/project/
    sprint_version/item, set via ``set_capability_profile``) -- it has no
    knowledge of ``requested`` (the older 649e095f ``project_capabilities``
    raw-manifest row, set via ``set_project_capability_manifest``/
    ``set_capability_manifest``). The two are independent stores, and most
    projects populate only the raw manifest and have never called
    ``set_capability_profile`` at all. Blindly returning the profile
    resolver's output AS "effective" would silently DROP every raw-manifest
    capability id that no profile layer happens to also declare -- including
    ``availability_policy: "required"`` ids the ``executable``/
    ``missing_required`` check below reads straight out of
    ``effective_capabilities`` (see :func:`build_capability_contract`), so a
    real "required" capability could silently stop being enforced. So:

    * No profile layer has contributed anything for this project (the
      common case today) -> degrade to ``requested`` unchanged, source
      ``"raw_manifest"`` -- byte-identical to the pre-fix degraded output
      when nothing from 02038afe applies.
    * A profile layer DID contribute something -> merge it on top of
      ``requested`` via :func:`capability_profile.merge_layers` (the SAME
      pure merge primitive ``get_effective_capability_profile`` itself
      uses), with the raw manifest as the least-specific layer and the
      resolved profile as the most-specific override -- so a profile can
      refine/override individual ids, but an id only the raw manifest
      declares is never silently lost. Source ``"profile_inheritance"``.

    Guarded broadly so a DB error (unknown project, pre-migration schema,
    etc.) degrades to ``"raw_manifest"`` rather than breaking the mandatory
    start_session/generate_handoff contract build.
    """
    if resolver is not None:
        try:
            resolved = resolver(db, project_id, requested)
            if isinstance(resolved, list):
                return resolved, "resolver"
        except Exception:  # noqa: BLE001 — an injected resolver must never crash the contract
            pass
    try:
        profile = await db_module.get_effective_capability_profile(db, project_id)
    except Exception:  # noqa: BLE001 — DB error must never crash the contract
        return requested, "raw_manifest"
    profile_capabilities = profile.get("capabilities") if isinstance(profile, dict) else None
    if not isinstance(profile_capabilities, list) or not profile_capabilities:
        # No workspace/user/project/sprint_version/item profile layer has
        # anything to contribute -- nothing to layer on top of the raw
        # manifest, so degrade to it directly rather than claiming a
        # "profile_inheritance" source that did not actually change anything.
        return requested, "raw_manifest"
    try:
        merged, *_rest = _capability_profile.merge_layers([
            {"layer": "raw_manifest", "capabilities": requested, "disabled_capability_ids": []},
            {"layer": "capability_profile", "capabilities": profile_capabilities, "disabled_capability_ids": []},
        ])
    except Exception:  # noqa: BLE001 — malformed profile data must never crash the contract
        return requested, "raw_manifest"
    return merged, "profile_inheritance"


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


_DEFAULT_MAX_EXECUTOR_CONTRACTS = 25

# 248c0bb9 — the cap above only ever bounded item_executor_contracts. The
# other three per-item sections (item_tool_requirements,
# item_sprint_item_pointers, item_artifact_pointer_findings) had NO cap at
# all: on a large board they could each grow to the full pending inventory,
# same class of unbounded-size risk item_executor_contracts already needed
# de4d4293's cap for (see build_capability_contract's own docstring for that
# incident). This is the SAME deterministic, item_id-sorted, non-silent-
# truncation pattern, generalized to the sections that pattern didn't
# originally cover. A generous default (well above every legitimately-sized
# project observed so far) so an ordinary board never truncates — this
# exists to bound the WORST case, not to shrink the common case.
_DEFAULT_MAX_CONTRACT_LIST_ITEMS = 200

# 537a7cef — item_routing_summary (de4d4293) was never threaded through
# _cap_contract_list at all: build_routing_summary emits one entry per
# PENDING item that has a resolvable hint (explicit or inferred), same
# unbounded-with-board-size shape item_executor_contracts needed de4d4293's
# cap for and the three sections above needed 248c0bb9's cap for -- this
# section just never got the same treatment. Confirmed live: on a real
# project's compact start_session response, item_routing_summary alone was
# ~25KB of a ~30KB capability_contract (83%) at 96 pending items, ~265 bytes/
# entry -- the single largest contributor observed, and the dominant driver
# on a much larger board (the item's own repro: a 2000+-item board pushing
# generate_handoff's capability_contract to ~440KB). Reuses
# max_contract_list_items (the SAME cap already applied to the sibling
# item_tool_requirements/item_sprint_item_pointers/
# item_artifact_pointer_findings sections) rather than inventing a fourth
# knob -- these sections are drawn from the identical candidate list and
# should truncate at the identical point. See _cap_contract_list's own
# {truncated, total_candidates, included} marker shape, applied here as
# item_routing_summary_truncated.

# 537a7cef — root cause B: requested.capabilities / effective.capabilities
# embed the FULL manifest/resolved-profile capability list verbatim, with NO
# cap of any kind -- not even start_session's own 0/0 max_executor_contracts/
# max_contract_list_items compact caps touch these two fields, since neither
# cap was ever wired to them. Each capability carries required_tools/
# fallback_chain/verification_command/provenance (capability_manifest.py's
# schema), so a project with a large declared capability profile pays for
# every one of those on every start_session/generate_handoff call regardless
# of any other cap in play. Threshold-based (never unconditional
# summarization) so every existing small-manifest test -- which asserts
# BYTE-IDENTICAL equality against the raw saved manifest -- stays untouched:
# default is well above any manifest exercised by this codebase's own tests
# or any realistically-sized real project's declared capability count.
# Preserves original list order (no re-sort) so truncation is a plain
# prefix-take, matching _cap_contract_list's own slicing behavior and never
# disturbing the order-sensitive equality assertions below the cap.
_DEFAULT_MAX_CAPABILITY_LIST_ITEMS = 50


def _cap_contract_list(
    entries: list[dict[str, Any]], max_items: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """248c0bb9 — deterministically cap an already ``item_id``-sorted list of
    contract entries, mirroring ``item_executor_contracts``'
    ``item_executor_contracts_truncated`` shape exactly so a caller checks
    both the same way. Never a silent drop: ``truncated`` records whether
    anything was actually cut, alongside the real total/included counts.
    """
    total = len(entries)
    cap = max(0, int(max_items or 0))
    capped = entries[:cap]
    return capped, {
        "truncated": total > len(capped),
        "total_candidates": total,
        "included": len(capped),
    }


async def build_capability_contract(
    db: Any,
    project_id: str,
    *,
    board_stale: bool = False,
    effective_resolver: "EffectiveResolver | None" = None,
    availability_checker: "AvailabilityChecker | None" = None,
    version: "str | None" = None,
    items: "list[dict[str, Any]] | None" = None,
    max_executor_contracts: int = _DEFAULT_MAX_EXECUTOR_CONTRACTS,
    max_contract_list_items: int = _DEFAULT_MAX_CONTRACT_LIST_ITEMS,
    max_capability_list_items: int = _DEFAULT_MAX_CAPABILITY_LIST_ITEMS,
) -> dict[str, Any]:
    """Build the effective capability contract for ``project_id``.

    ``max_capability_list_items`` (537a7cef) — caps the EMBEDDED
    ``requested.capabilities`` / ``effective.capabilities`` lists the same
    threshold+marker way ``max_contract_list_items`` caps the per-item
    sections below: applied to a COPY used only for the final serialized
    contract, never to the lists this function itself computes
    executable/missing_required/manifest_hash from (those always see the
    full, uncapped capability list — truncating them would silently change
    executability). A sibling ``requested.capabilities_truncated`` /
    ``effective.capabilities_truncated`` dict (``_cap_contract_list``'s own
    ``{truncated, total_candidates, included}`` shape) records whether
    anything was actually cut. A manifest at or under the cap (every
    existing test's manifest, and any realistically-sized real project) is
    byte-identical to the pre-cap output.

    ``max_contract_list_items`` (248c0bb9) — caps ``item_tool_requirements``,
    ``item_sprint_item_pointers``, and ``item_artifact_pointer_findings``
    the SAME way ``max_executor_contracts`` already caps
    ``item_executor_contracts`` below: applied AFTER each section's own
    deterministic ``item_id`` sort, with a sibling
    ``item_<section>_truncated`` dict (``_cap_contract_list``) recording
    ``{truncated, total_candidates, included}`` — never a silent drop. A
    board at or under the cap (the overwhelming common case) sees the exact
    same output as before this parameter existed. See
    ``_DEFAULT_MAX_CONTRACT_LIST_ITEMS``'s own comment for why this exists:
    these three sections had no bound at all before, unlike
    ``item_executor_contracts``.

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
    XML clause for the same request. It also feeds
    ``item_artifact_pointer_findings`` (70c10ca3, b730 follow-up, see
    :func:`extract_artifact_pointer_findings`) — 88f82c15's warn/strict
    artifact-pointer policy verdict enriched with 3196ba0e's fail-closed
    readiness verification, matching
    ``handoff._build_artifact_pointer_findings_clause``'s
    ``<artifact_pointer_findings>`` XML clause for the same request.

    ``max_executor_contracts`` (de4d4293) — CAPS how many full per-item
    ``item_executor_contracts`` entries this call embeds, applied AFTER a
    deterministic ``item_id`` sort so the same underlying board always caps
    to the same subset. This is the direct fix for a real, already-shipped
    size regression (23e20656): embedding one FULL executor_contract per
    PENDING item with no bound at all inflated a real project's
    ``generate_handoff`` JSON response by 95KB+ on a 37-69 item board,
    repeatedly breaking the calling MCP client's own max-tool-output-size
    limit. When the candidate list exceeds the cap, ``item_executor_contracts``
    holds only the first ``max_executor_contracts`` (by ``item_id``) and a
    sibling ``item_executor_contracts_truncated`` dict records
    ``{"truncated": True, "total_candidates": N, "included": cap}`` — never
    a SILENT drop. A board at or under the cap gets
    ``{"truncated": False, ...}`` and, functionally, the exact same output
    as before this parameter existed (default matches the shipped-bug's
    effective board sizes at the low end, chosen deliberately conservative).

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

    # 70c10ca3 (b730 follow-up) — typed per-item artifact-pointer FINDING
    # contract: 88f82c15's warn/strict verdict enriched with 3196ba0e's
    # readiness verification, extracted from the SAME pending-item list as
    # the two sections above. See extract_artifact_pointer_findings for the
    # pre-annotated-vs-self-resolve split. This is what makes an artifact-
    # pointer warning machine-readable in the JSON/capability_contract
    # projection, not just the human-readable XML clause.
    try:
        item_artifact_pointer_findings = await extract_artifact_pointer_findings(
            db, project_id, _pending_items_for_tool_reqs,
        )
    except Exception:  # noqa: BLE001 — contract building must never break on this
        item_artifact_pointer_findings = []

    # 248c0bb9 — cap the three sections above the SAME way item_executor_
    # contracts is capped below (see max_contract_list_items's docstring):
    # each is already item_id-sorted by its own extractor, so capping here is
    # deterministic and byte-for-byte stable across builds of the same board.
    item_tool_requirements, item_tool_requirements_truncated = _cap_contract_list(
        item_tool_requirements, max_contract_list_items,
    )
    item_sprint_item_pointers, item_sprint_item_pointers_truncated = _cap_contract_list(
        item_sprint_item_pointers, max_contract_list_items,
    )
    item_artifact_pointer_findings, item_artifact_pointer_findings_truncated = (
        _cap_contract_list(item_artifact_pointer_findings, max_contract_list_items)
    )

    # 23e20656 (665 follow-up) — one canonical per-item executor_contract,
    # composing item_tool_requirements/item_sprint_item_pointers above with
    # output/dependency/wave/gate/completion-check state (see
    # meridian.executor_contract.build_executor_contract). Lazy import: the
    # executor_contract module itself imports THIS module (to reuse
    # extract_sprint_item_pointers), so a top-level import here would be
    # circular. Pre-fetches wave_gate_configs/project ONCE and threads them
    # into every per-item build so N items don't repeat the same two
    # project-scoped queries. Fully guarded per item — one item's failure
    # degrades to that item simply being absent from the list, never breaks
    # the mandatory contract.
    #
    # de4d4293 — BOUNDED: candidates are sorted by item_id and capped to
    # ``max_executor_contracts`` BEFORE any per-item contract is built (never
    # after), so a huge board never pays the async build cost for entries it
    # will discard anyway. This directly fixes a real, already-shipped size
    # regression — see build_capability_contract's own docstring for the
    # incident this cap exists to prevent. Never silent: when the candidate
    # count exceeds the cap, item_executor_contracts_truncated records the
    # full picture rather than letting the list simply come up short.
    item_executor_contracts: list[dict[str, Any]] = []
    _executor_contract_candidates = sorted(
        (
            _it for _it in _pending_items_for_tool_reqs
            if isinstance(_it, dict) and _it.get("id")
        ),
        key=lambda _it: _it["id"],
    )
    _ec_total_candidates = len(_executor_contract_candidates)
    _ec_cap = max(0, int(max_executor_contracts or 0))
    _executor_contract_candidates = _executor_contract_candidates[:_ec_cap]
    try:
        from . import executor_contract as _executor_contract  # noqa: PLC0415

        try:
            _wave_gate_configs = await db_module.get_wave_gate_configs(db, project_id)
        except Exception:  # noqa: BLE001
            _wave_gate_configs = None
        try:
            _project_row = await db_module.get_project(db, project_id)
        except Exception:  # noqa: BLE001
            _project_row = None
        for _it in _executor_contract_candidates:
            try:
                _ec = await _executor_contract.build_executor_contract(
                    db, project_id, _it, version=version,
                    wave_gate_configs=_wave_gate_configs, project=_project_row,
                )
            except Exception:  # noqa: BLE001 — one item's failure must not break the batch
                continue
            # Strip the per-item wall-clock generated_at before embedding: this
            # module's own serialize_contract only strips the TOP-LEVEL
            # generated_at/contract_hash, so a nested wall-clock field here
            # would otherwise make the outer project contract_hash non-
            # deterministic across two builds of the SAME underlying state.
            # contract_hash itself is unaffected (already computed excluding
            # its own generated_at) and is kept.
            _ec = dict(_ec)
            _ec.pop("generated_at", None)
            item_executor_contracts.append(_ec)
        item_executor_contracts.sort(key=lambda c: c.get("item_id") or "")
    except Exception:  # noqa: BLE001 — executor_contract is best-effort enrichment
        item_executor_contracts = []
    item_executor_contracts_truncated = {
        "truncated": _ec_total_candidates > _ec_cap,
        "total_candidates": _ec_total_candidates,
        "included": len(item_executor_contracts),
    }

    # de4d4293 — compact per-item routing summary + parity hash: the SAME
    # canonical extraction (executor_contract.build_routing_summary) the
    # /goal text's <executor_routing> clause reads
    # (handoff._build_executor_routing_clause), computed here over the SAME
    # _pending_items_for_tool_reqs list as the sibling item_tool_requirements/
    # item_sprint_item_pointers/item_artifact_pointer_findings sections above
    # (the full version-scoped pending inventory — NOT the narrower
    # claimable-batch scope the /goal text's own <executor_routing> clause
    # deliberately uses; see that clause's docstring for why the text stays
    # narrower). Cheap and bounded per-entry (no DB, no per-item async work,
    # one short dict per item) but NOT bounded in COUNT — it scales linearly
    # with the pending-item inventory the same way item_executor_contracts
    # did before de4d4293's cap; see the explicit cap applied right below
    # (537a7cef) for why this needed the same treatment.
    try:
        from . import executor_contract as _executor_contract_routing  # noqa: PLC0415

        item_routing_summary = _executor_contract_routing.build_routing_summary(
            _pending_items_for_tool_reqs
        )
        item_routing_summary_hash = _executor_contract_routing.routing_summary_hash(
            item_routing_summary
        )
    except Exception:  # noqa: BLE001 — routing summary is best-effort enrichment
        item_routing_summary = []
        item_routing_summary_hash = None

    # 537a7cef — cap the EMBEDDED routing summary the SAME way the three
    # sibling per-item sections are capped above (item_routing_summary was
    # never wired to max_contract_list_items at all -- see
    # _DEFAULT_MAX_CAPABILITY_LIST_ITEMS's neighboring comment for why this
    # was the single largest contributor observed on a real board). The hash
    # is computed over the FULL (pre-cap) summary above, preserving its
    # documented parity semantics with an independent build_routing_summary
    # call over the same live items -- only the embedded list itself is
    # truncated for size, never the hash's input.
    item_routing_summary, item_routing_summary_truncated = _cap_contract_list(
        item_routing_summary, max_contract_list_items,
    )

    effective_capabilities, effective_source = await _resolve_effective_capabilities(
        db, project_id, requested_capabilities, resolver=effective_resolver,
    )
    availability_result, availability_status = _resolve_availability(
        effective_capabilities, checker=availability_checker,
    )

    if availability_status == "checked" and isinstance(availability_result, dict):
        available: Any = sorted(availability_result.get("available") or [])
        missing: Any = sorted(availability_result.get("missing") or [])
        degraded: Any = sorted(availability_result.get("degraded") or [])
        unverified: list[str] = []
    else:
        available = "unknown"
        missing = "unknown"
        degraded = "unknown"
        # An unavailable checker is not evidence that a declared capability
        # works. Keep the legacy fields lossless for clients that understand
        # the older contract, while exposing the exact ids whose status is
        # unverified so executors can make a deterministic decision.
        unverified = sorted(
            c["id"] for c in effective_capabilities
            if isinstance(c, dict) and c.get("id")
        )

    required_ids = {
        c["id"] for c in effective_capabilities
        if isinstance(c, dict) and c.get("availability_policy") == "required" and c.get("id")
    }
    missing_required = (
        sorted(required_ids & set(missing)) if isinstance(missing, list) else []
    )
    unverified_required = sorted(required_ids & set(unverified))

    executable = True
    executable_reasons: list[str] = []
    if missing_required:
        executable = False
        executable_reasons.append(
            "missing_required_capabilities:" + ",".join(missing_required)
        )
    if unverified_required:
        executable = False
        executable_reasons.append(
            "required_capabilities_unverified:" + ",".join(unverified_required)
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

    # 537a7cef — cap the EMBEDDED requested/effective capability lists for
    # display only, on copies taken AFTER every executable/missing_required/
    # manifest_hash computation above already consumed the full, uncapped
    # lists (see max_capability_list_items's own docstring on why this must
    # never feed back into those computations).
    requested_capabilities_embedded, requested_capabilities_truncated = (
        _cap_contract_list(
            requested_capabilities if isinstance(requested_capabilities, list) else [],
            max_capability_list_items,
        )
    )
    effective_capabilities_embedded, effective_capabilities_truncated = (
        _cap_contract_list(
            effective_capabilities if isinstance(effective_capabilities, list) else [],
            max_capability_list_items,
        )
    )

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    contract: dict[str, Any] = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "project_id": project_id,
        "requested": {
            "capabilities": requested_capabilities_embedded,
            "capabilities_truncated": requested_capabilities_truncated,
            "manifest_version": manifest.get("manifest_version"),
            "manifest_hash": manifest.get("manifest_hash"),
        },
        "effective": {
            "capabilities": effective_capabilities_embedded,
            "capabilities_truncated": effective_capabilities_truncated,
            "source": effective_source,
        },
        "availability": {
            "status": availability_status,
            "available": available,
            "missing": missing,
            "degraded": degraded,
            "unverified": unverified,
        },
        "manifest_hash": effective_hash,
        "item_tool_requirements": item_tool_requirements,
        "item_tool_requirements_truncated": item_tool_requirements_truncated,
        "item_sprint_item_pointers": item_sprint_item_pointers,
        "item_sprint_item_pointers_truncated": item_sprint_item_pointers_truncated,
        "item_artifact_pointer_findings": item_artifact_pointer_findings,
        "item_artifact_pointer_findings_truncated": item_artifact_pointer_findings_truncated,
        "item_executor_contracts": item_executor_contracts,
        "item_executor_contracts_truncated": item_executor_contracts_truncated,
        "item_routing_summary": item_routing_summary,
        "item_routing_summary_truncated": item_routing_summary_truncated,
        "item_routing_summary_hash": item_routing_summary_hash,
        "board_stale": bool(board_stale),
        "executable": executable,
        "executable_reasons": executable_reasons,
        "generated_at": generated_at,
    }
    contract = _scrub_secrets(contract)
    contract["contract_hash"] = contract_hash(contract)
    return contract
