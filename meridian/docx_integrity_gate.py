"""DOCX integrity gate for ``generate_handoff`` (d09c29fe).

Composes three DOCX-integrity primitives that landed earlier this same
megasprint into ONE per-artifact verdict -- it never re-derives their logic:

* **93cd9798** -- ``extensions/meridian-docs/meridian_docs/render_gate.py::
  check_render_capability`` -- a tri-state (``rendered`` /
  ``unavailable-with-reason`` / ``failed``) visual-render verification for
  one ``.docx``.
* **4efc63fd** -- the sibling module's ``docs_intel.py::audit_equation_style``
  -- structured (never free-text) equation-style findings for one ``.docx``.
* **dccc2311** -- the same module's hardened write-transaction verification
  (``DocxWriteVerificationError`` / a structural ``manifest_hash``). That
  machinery is WRITE-time only and nothing durably persists its output
  anywhere this gate could read (see ``docs_intel._atomic_write_docx_bytes``),
  so this module composes the *concept* instead: a read-time provenance
  fingerprint (:func:`_compute_provenance_manifest`) built from the sibling
  module's public, read-only ``read_document_snapshot`` -- never the private
  write-path helpers.
* **6cdc5df3** -- ``meridian.db.proposal_links`` -- proposal-to-evidence
  linkage. A proposal's ``artifact`` evidence links (free-form entity ids,
  commonly a produced ``.docx`` path -- see that module's own docstring) are
  a first-class SOURCE of "which DOCX artifacts does this handoff cover",
  alongside a pending sprint item's own durable pointers.

Mirrors the existing soft ``ready``/``executable`` machine-readable pattern
(``capability_contract.py``, ``executor_contract.py``,
``meridian.pointers.evaluate_artifact_pointer_policy``) rather than inventing
a new one: every artifact gets an ``executable``-style ``ready`` verdict, and
the gate as a whole exposes ``executable`` + ``executable_reasons`` exactly
like :func:`meridian.capability_contract.build_capability_contract`.

Package-boundary note (why every check is injectable + best-effort-imported):
``extensions/meridian-docs`` is an independently installable extension (like
``meridian-codeindex`` / ``meridian-outputs``) -- it is NOT a
``[pypi-dependencies]`` entry of the core ``meridian`` package (see
``pixi.toml``), so ``import meridian_docs`` legitimately fails in many
deployments (a self-hosted checkout that never ran
``pip install -e ./extensions/meridian-docs``, most CI runs, this repo's own
dev pixi env at the time this module was written). This module therefore
resolves the sibling the SAME guarded, dotted-string ``importlib`` way
``capability_contract._import_optional_sibling`` resolves ITS not-always-
present siblings (see that function's docstring for exactly why a plain
``from``/``import`` statement is avoided -- this repo's orphaned-refs
pre-merge guard statically AST-walks ``ImportFrom`` nodes), and degrades to
``available=False`` -- never a block -- when nothing resolves. "Can't
confirm" must never manufacture a finding; only a CONFIRMED problem
(``render_status == "failed"``, or a real, non-empty equation-style finding
list) ever sets ``unresolved``.

Discovery of "which artifacts does this handoff cover" (two sources, neither
reinvented -- both already-durable):

1. Each pending sprint item's own durable pointers
   (``db.get_sprint_item_pointers`` -- the ``sprint_item_pointers`` table,
   2976e168): any target whose ``uri`` names a ``.docx`` file.
2. Proposal-evidence ``artifact`` links (6cdc5df3): any link whose free-form
   ``entity_id`` names a ``.docx`` file. This is this item's specific
   tie-in point -- a DOCX output linked as evidence to a proposal is exactly
   the kind of artifact this gate must cover, not just an item's own
   declared pointer.

"Required" (able to flip the overall gate's ``executable`` to ``False``) is
resolved from the SAME per-item policy field
``pointers.evaluate_artifact_pointer_policy`` already gates on --
``artifact_declaration.effective_artifact_policy(item)["artifact_pointer_check"]
== "strict"`` -- never a second, independent policy knob. A proposal-evidence
artifact with no directly-linked sprint item inherits "required" from any
sibling sprint item the SAME proposal links (a proposal whose own item wants
strict checking should hold its produced artifact to the same bar); with no
sprint item at all it falls back to the module default ("warn" -> not
required), matching ``artifact_declaration.default_artifact_policy()``.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Callable

from . import artifact_declaration as _artifact_declaration
from . import db as db_module

GATE_SCHEMA_VERSION = 1

_DOCX_EXT = ".docx"

# Non-local uri prefixes -- mirrors meridian.pointers._NON_LOCAL_URI_PREFIXES
# exactly (a .docx uri using one of these schemes is not a filesystem path
# this gate can open). Duplicated as a plain tuple rather than importing the
# private name cross-module for a single narrow check; keep in sync with
# pointers.py if that list ever changes.
_NON_LOCAL_URI_PREFIXES = ("zotero:", "finding:", "doc:", "mailto:")

# Bounds -- the 23e20656 lesson (an earlier item in this same megasprint
# shipped an executor_contract iteration with NO size cap and inflated
# generate_handoff's response by 95KB+ on a board with dozens of pending
# items). Every list this module returns is bounded; a *_capped counter
# always reports what was left out rather than silently truncating.
_MAX_CANDIDATE_ITEMS = 30          # pending items scanned for pointer-derived candidates
_MAX_CHECKED_ARTIFACTS = 8         # distinct .docx artifacts actually live-probed
_MAX_FINDINGS_PER_ARTIFACT = 20    # equation-style findings kept verbatim per artifact

# Defense-in-depth timeout for a single sync checker call. render_gate's own
# soffice backend already enforces a 60s subprocess timeout internally and
# converts a timeout into its own "failed" status -- but the Windows Word-COM
# backend (_word_com_render) has NO internal timeout at all, so a hung COM
# call could otherwise stall this (best-effort, non-mandatory) gate
# indefinitely. A timeout here degrades to "could not confirm in time" --
# never treated as a confirmed finding, matching the "can't confirm never
# blocks" rule everywhere else in this module.
_CHECK_TIMEOUT_SECONDS = 15.0

RenderChecker = Callable[[str], dict[str, Any]]
EquationAuditor = Callable[[str], dict[str, Any]]
SnapshotReader = Callable[[str], dict[str, Any]]


# ---------------------------------------------------------------------------
# Optional-sibling resolution -- mirrors capability_contract._import_optional_sibling.
# ---------------------------------------------------------------------------

def _import_optional_meridian_docs_submodule(name: str) -> Any | None:
    """Best-effort import of ``meridian_docs.<name>``.

    Returns ``None`` on ``ModuleNotFoundError`` (the extension is not
    installed in this environment -- the common case, see module docstring)
    or any other import-time failure (a half-installed/broken extension).
    Never propagates -- this module must degrade cleanly, never break the
    mandatory handoff paths that consume it.
    """
    try:
        return importlib.import_module(f"meridian_docs.{name}")
    except ModuleNotFoundError:
        return None
    except Exception:  # noqa: BLE001 -- a broken sibling must never break this gate
        return None


def default_render_checker() -> "RenderChecker | None":
    mod = _import_optional_meridian_docs_submodule("render_gate")
    fn = getattr(mod, "check_render_capability", None) if mod else None
    return fn if callable(fn) else None


def default_equation_auditor() -> "EquationAuditor | None":
    mod = _import_optional_meridian_docs_submodule("docs_intel")
    fn = getattr(mod, "audit_equation_style", None) if mod else None
    return fn if callable(fn) else None


def default_snapshot_reader() -> "SnapshotReader | None":
    mod = _import_optional_meridian_docs_submodule("docs_intel")
    fn = getattr(mod, "read_document_snapshot", None) if mod else None
    return fn if callable(fn) else None


async def _call_checker_bounded(
    checker: "Callable[[str], dict[str, Any]] | None", path: str,
) -> "dict[str, Any] | None":
    """Run a sync checker off the event loop, bounded by
    :data:`_CHECK_TIMEOUT_SECONDS`. Returns ``None`` when there is no checker,
    the call raised, or it did not return in time -- callers treat ``None``
    as "could not confirm", never as a finding."""
    if checker is None:
        return None
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(checker, path), timeout=_CHECK_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 -- includes asyncio.TimeoutError; a checker must never break the gate
        return None
    return result if isinstance(result, dict) else None


# ---------------------------------------------------------------------------
# Candidate discovery.
# ---------------------------------------------------------------------------

def _looks_like_local_docx_uri(candidate: Any) -> bool:
    """True when ``candidate`` looks like a local ``.docx`` filesystem path
    (not a URL or a scheme reference) -- mirrors
    ``pointers._looks_like_local_path`` narrowed to the ``.docx`` suffix this
    module cares about."""
    if not isinstance(candidate, str):
        return False
    text = candidate.strip()
    if not text or "://" in text:
        return False
    lowered = text.lower()
    if lowered.startswith(_NON_LOCAL_URI_PREFIXES):
        return False
    return lowered.endswith(_DOCX_EXT)


async def _collect_item_candidates(
    db: Any, items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Returns ``(candidates, items_scanned)`` where each candidate is
    ``{docx_path, item_id, source: "sprint_item_pointer"}``.

    Bounded to :data:`_MAX_CANDIDATE_ITEMS` items (one ``get_sprint_item_pointers``
    round trip each) -- a board with dozens of pending items must not turn
    this best-effort gate into dozens of extra DB queries. Fully guarded per
    item: one item's lookup failure skips just that item.
    """
    candidates: list[dict[str, Any]] = []
    scanned = 0
    for item in items[:_MAX_CANDIDATE_ITEMS]:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        scanned += 1
        try:
            stored_pointers = await db_module.get_sprint_item_pointers(db, item["id"])
        except Exception:  # noqa: BLE001 -- best-effort discovery
            continue
        seen: set[str] = set()
        for ptr in stored_pointers or []:
            if not isinstance(ptr, dict):
                continue
            for target in ptr.get("targets") or []:
                if not isinstance(target, dict):
                    continue
                uri = target.get("uri")
                if _looks_like_local_docx_uri(uri) and uri not in seen:
                    seen.add(uri)
                    candidates.append({
                        "docx_path": uri,
                        "item_id": item["id"],
                        "source": "sprint_item_pointer",
                    })
    return candidates, scanned


def _collect_proposal_artifact_candidates(
    proposal_evidence: "list[dict[str, Any]] | None",
) -> list[dict[str, Any]]:
    """Returns one candidate dict per proposal-evidence ``artifact`` link
    whose ``entity_id`` names a ``.docx`` file:
    ``{docx_path, proposal_id, label, link_id, source: "proposal_evidence_artifact",
    proposal_requires_strict}`` -- the last field is ``True`` when ANY
    sprint_item evidence linked to the SAME proposal resolves to a
    ``"strict"`` artifact policy (see module docstring's "required"
    inheritance rule).
    """
    out: list[dict[str, Any]] = []
    for bundle in proposal_evidence or []:
        if not isinstance(bundle, dict):
            continue
        proposal_id = bundle.get("proposal_id")
        requires_strict = False
        for si in bundle.get("sprint_items") or []:
            if not isinstance(si, dict):
                continue
            try:
                policy = _artifact_declaration.effective_artifact_policy(si)
            except Exception:  # noqa: BLE001
                policy = _artifact_declaration.default_artifact_policy()
            if policy.get("artifact_pointer_check") == "strict":
                requires_strict = True
                break
        for art in bundle.get("artifacts") or []:
            if not isinstance(art, dict):
                continue
            eid = art.get("entity_id")
            if not _looks_like_local_docx_uri(eid):
                continue
            out.append({
                "docx_path": eid,
                "proposal_id": proposal_id,
                "label": art.get("label"),
                "link_id": art.get("link_id"),
                "source": "proposal_evidence_artifact",
                "proposal_requires_strict": requires_strict,
            })
    return out


def _resolve_item_required(item: "dict[str, Any] | None") -> bool:
    """True iff this item's effective artifact policy is ``"strict"`` --
    the SAME field ``pointers.evaluate_artifact_pointer_policy`` gates on,
    never a second policy knob (see module docstring)."""
    if not isinstance(item, dict):
        return False
    try:
        policy = _artifact_declaration.effective_artifact_policy(item)
    except Exception:  # noqa: BLE001
        policy = _artifact_declaration.default_artifact_policy()
    return policy.get("artifact_pointer_check") == "strict"


# ---------------------------------------------------------------------------
# Provenance manifest -- the read-time fingerprint standing in for dccc2311's
# write-time manifest_hash concept (see module docstring for why the actual
# write-path helper cannot be reused here).
# ---------------------------------------------------------------------------

def _compute_provenance_manifest(snapshot: "dict[str, Any] | None") -> "dict[str, Any] | None":
    """Build a deterministic provenance fingerprint from a
    ``read_document_snapshot`` result. ``None`` when the snapshot is missing
    or itself reports an error -- never fabricates a manifest for a document
    that could not actually be read."""
    if not isinstance(snapshot, dict) or snapshot.get("error"):
        return None
    xml_parts = snapshot.get("xml_parts")
    xml_parts = sorted(xml_parts) if isinstance(xml_parts, list) else []
    payload = {
        "byte_size": snapshot.get("byte_size"),
        "paragraph_count": snapshot.get("paragraph_count"),
        "heading_count": snapshot.get("heading_count"),
        "xml_part_count": len(xml_parts),
    }
    canonical = json.dumps({**payload, "xml_parts": xml_parts}, sort_keys=True, default=str)
    payload["manifest_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return payload


# ---------------------------------------------------------------------------
# Per-artifact evaluation.
# ---------------------------------------------------------------------------

async def _evaluate_artifact(
    candidate: dict[str, Any],
    *,
    required: bool,
    render_checker: "RenderChecker | None",
    equation_auditor: "EquationAuditor | None",
    snapshot_reader: "SnapshotReader | None",
) -> dict[str, Any]:
    path = candidate["docx_path"]
    entry: dict[str, Any] = {
        "docx_path": path,
        "source": candidate["source"],
        "item_id": candidate.get("item_id"),
        "proposal_id": candidate.get("proposal_id"),
        "label": candidate.get("label"),
        "required": required,
    }
    exists = False
    try:
        exists = os.path.isfile(path)
    except OSError:
        exists = False
    entry["exists_on_disk"] = exists

    render_status = None
    render_reason = None
    render_backend = None
    equation_audit: "dict[str, Any] | None" = None
    provenance_manifest: "dict[str, Any] | None" = None
    unresolved_reasons: list[str] = []

    if exists:
        render_result = await _call_checker_bounded(render_checker, path)
        if isinstance(render_result, dict):
            render_status = render_result.get("status")
            render_reason = render_result.get("reason")
            render_backend = render_result.get("backend")
            if render_status == "failed":
                unresolved_reasons.append(f"render_failed:{render_reason or 'unknown'}")

        equation_result = await _call_checker_bounded(equation_auditor, path)
        if isinstance(equation_result, dict) and not equation_result.get("error"):
            finding_count = equation_result.get("finding_count") or 0
            findings = equation_result.get("findings") or []
            equation_audit = {
                "equation_count": equation_result.get("equation_count"),
                "finding_count": finding_count,
                "findings_by_type": equation_result.get("findings_by_type"),
                "findings": findings[:_MAX_FINDINGS_PER_ARTIFACT],
                "findings_capped": max(0, len(findings) - _MAX_FINDINGS_PER_ARTIFACT),
            }
            if finding_count:
                unresolved_reasons.append(f"equation_style_findings:{finding_count}")

        snapshot = await _call_checker_bounded(snapshot_reader, path)
        provenance_manifest = _compute_provenance_manifest(snapshot)

    entry["render_status"] = render_status
    entry["render_reason"] = render_reason
    entry["render_backend"] = render_backend
    entry["equation_audit"] = equation_audit
    entry["provenance_manifest"] = provenance_manifest
    entry["unresolved"] = bool(unresolved_reasons)
    entry["unresolved_reasons"] = unresolved_reasons
    entry["ready"] = not (required and entry["unresolved"])
    return entry


# ---------------------------------------------------------------------------
# The builder.
# ---------------------------------------------------------------------------

async def build_docx_integrity_gate(
    db: Any,
    project_id: str,
    pending_items: "list[dict[str, Any]] | None" = None,
    *,
    proposal_evidence: "list[dict[str, Any]] | None" = None,
    render_checker: "RenderChecker | None" = None,
    equation_auditor: "EquationAuditor | None" = None,
    snapshot_reader: "SnapshotReader | None" = None,
    max_checked_artifacts: int = _MAX_CHECKED_ARTIFACTS,
) -> dict[str, Any]:
    """Build the DOCX-integrity gate for ``project_id``.

    ``pending_items`` -- the SAME pending sprint-item list a caller already
    has (mirrors ``capability_contract.build_capability_contract``'s ``items``
    kwarg discipline); ``None`` self-fetches via the identical filter
    criteria ``generate_handoff`` uses (status in {todo, pending},
    ``include_deferred=False``).

    ``proposal_evidence`` -- the SAME list
    :func:`meridian.handoff.build_proposal_evidence_for_handoff` already
    produces for this handoff; pass it through so this gate never re-fetches
    or re-derives proposal linkage independently (6cdc5df3 tie-in). ``None``
    simply means no proposal-evidence artifacts are considered (NOT an
    error -- a caller that hasn't computed proposal_evidence yet still gets a
    valid, item-pointer-only gate).

    ``render_checker`` / ``equation_auditor`` / ``snapshot_reader`` --
    injectable overrides for the three ``meridian_docs`` primitives (tests
    inject stubs; a caller with the real extension installed may pass the
    real functions directly). ``None`` (the default) resolves each via
    best-effort optional import -- see :func:`default_render_checker` et al.

    Never raises: every DB call, every checker invocation, and every policy
    resolution is individually guarded. A caller should still wrap this in
    its own try/except per this codebase's existing convention (an
    orientation/handoff call must never break over an enrichment
    convenience) -- see
    ``meridian.handoff.build_docx_integrity_gate_for_handoff``.
    """
    if pending_items is None:
        try:
            items = await db_module.get_sprint_items(
                db, project_id, include_human=False, include_deferred=False,
            )
            pending_items = [it for it in items if it.get("status") in ("todo", "pending")]
        except Exception:  # noqa: BLE001
            pending_items = []

    items_by_id = {
        it["id"]: it for it in pending_items if isinstance(it, dict) and it.get("id")
    }

    render_checker = render_checker if render_checker is not None else default_render_checker()
    equation_auditor = (
        equation_auditor if equation_auditor is not None else default_equation_auditor()
    )
    snapshot_reader = (
        snapshot_reader if snapshot_reader is not None else default_snapshot_reader()
    )
    available = any((render_checker, equation_auditor, snapshot_reader))

    item_candidates, items_scanned = await _collect_item_candidates(db, pending_items)
    proposal_candidates = _collect_proposal_artifact_candidates(proposal_evidence)

    # De-duplicate by docx_path: an item-pointer candidate and a proposal-
    # evidence candidate naming the SAME file collapse into one evaluation
    # (union of required-ness, both provenance tags kept) rather than
    # probing the same artifact twice.
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for cand in item_candidates + proposal_candidates:
        path = cand["docx_path"]
        if path not in merged:
            merged[path] = dict(cand)
            order.append(path)
        else:
            existing = merged[path]
            # Keep the richer provenance: item_id/proposal_id from whichever
            # candidate carries them, preferring the first-seen non-null value.
            for key in ("item_id", "proposal_id", "label", "link_id"):
                if not existing.get(key) and cand.get(key):
                    existing[key] = cand[key]
            if cand.get("proposal_requires_strict"):
                existing["proposal_requires_strict"] = True

    total_candidates = len(order)
    checked_paths = order[:max_checked_artifacts]
    skipped_candidates = max(0, total_candidates - len(checked_paths))

    checked_artifacts: list[dict[str, Any]] = []
    unresolved_required_count = 0
    executable_reasons: list[str] = []

    for path in checked_paths:
        cand = merged[path]
        item = items_by_id.get(cand.get("item_id")) if cand.get("item_id") else None
        required = _resolve_item_required(item) or bool(cand.get("proposal_requires_strict"))
        entry = await _evaluate_artifact(
            cand,
            required=required,
            render_checker=render_checker,
            equation_auditor=equation_auditor,
            snapshot_reader=snapshot_reader,
        )
        checked_artifacts.append(entry)
        if required and entry["unresolved"]:
            unresolved_required_count += 1
            executable_reasons.append(
                f"docx_integrity_unresolved:{entry.get('item_id') or entry.get('proposal_id') or path}"
            )

    executable = unresolved_required_count == 0

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "schema_version": GATE_SCHEMA_VERSION,
        "project_id": project_id,
        "available": available,
        "items_scanned": items_scanned,
        "checked_artifacts": checked_artifacts,
        "candidate_count": total_candidates,
        "skipped_candidates": skipped_candidates,
        "unresolved_required_count": unresolved_required_count,
        "executable": executable,
        "executable_reasons": executable_reasons,
        "generated_at": generated_at,
    }


# ---------------------------------------------------------------------------
# f6912e2d — RECIPE_CHECK_REGISTRY: the EXACT function/tool each
# ``artifact_declaration.artifact_recipe.checks`` flag names.
#
# The acceptance criteria for f6912e2d require a document/Outputs item's
# recipe to name its structural / Word-COM-render / Outputs hash-provenance
# checks EXACTLY — never "some structural check, somewhere". This module
# already HOSTS two of those three verification classes (this file composes
# the render-status + equation-style + provenance-manifest verdict every
# discovered .docx artifact gets); the registry below is the single,
# reviewable place mapping a recipe's boolean flag to the real, importable
# reference that flag stands for, so a caller (or a human auditing a
# declared recipe) never has to guess which function a ``True`` flag means.
#
# Deliberately NOT wired into build_docx_integrity_gate's own execution
# above: that gate already runs ``check_render_capability`` (both backends)
# uniformly for every discovered artifact regardless of what any one item's
# recipe declares, and changing that per-item would risk this file's
# existing, already-tested behavior (test_handoff_docx_integrity_gate.py) —
# out of this item's touches_resources scope to touch. This registry is
# read-only declaration metadata a caller MAY cross-reference; it never
# changes which checks actually run today.
# ---------------------------------------------------------------------------

RECIPE_CHECK_REGISTRY: dict[str, str] = {
    "structural_check_required": (
        "extensions/meridian-docs/meridian_docs/render_gate.py::"
        "verify_promotion_readiness (its structural_check block) — cheap, "
        "read-only paragraph/media/table-count comparison; never mutates "
        "either file"
    ),
    "word_com_render_check_required": (
        "extensions/meridian-docs/meridian_docs/render_gate.py::"
        "check_word_com_render_receipt — the Word-COM-ONLY three-state "
        "render check (rendered/unavailable-with-reason/failed); "
        "deliberately narrower than check_render_capability, which also "
        "accepts a LibreOffice/soffice render as a general capability "
        "signal"
    ),
    "outputs_provenance_check_required": (
        "meridian-outputs MCP server (extensions/meridian-outputs) — "
        "record_provenance / get_provenance_status / get_provenance tools; "
        "see extensions/meridian-outputs/README.md's tool table"
    ),
}


def describe_required_checks(item: dict[str, Any]) -> dict[str, Any]:
    """For ONE sprint item, resolve its declared
    ``artifact_declaration.artifact_recipe.checks`` flags (if any) into the
    EXACT check reference each ``True`` flag names, via
    :data:`RECIPE_CHECK_REGISTRY`.

    Returns ``{"declared": bool, "required": {flag: registry_entry, ...}}``
    — ``declared=False`` (empty ``required``) when the item has no
    ``artifact_recipe`` at all, mirroring this module's existing
    "can't confirm never fabricates a finding" convention. Pure/sync, never
    raises — a malformed ``item`` degrades to ``declared=False`` exactly
    like ``artifact_declaration.effective_artifact_recipe`` itself does for
    a malformed/missing field.
    """
    try:
        recipe = _artifact_declaration.effective_artifact_recipe(item)
    except Exception:  # noqa: BLE001 — must never raise
        recipe = None
    if not recipe:
        return {"declared": False, "required": {}}
    checks = recipe.get("checks") or {}
    required = {
        flag: RECIPE_CHECK_REGISTRY[flag]
        for flag in RECIPE_CHECK_REGISTRY
        if checks.get(flag)
    }
    return {"declared": True, "required": required}
