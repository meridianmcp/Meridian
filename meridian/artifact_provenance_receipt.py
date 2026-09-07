"""Durable, project-scoped receipts for artifact-provenance resolutions
(sprint item b1fee417 "W31-A", follow-up to 6d02f343 / 6b657a8b).

WHY THIS EXISTS: ``meridian_outputs.provenance.bind_artifact_provenance``
(the extension's fail-closed docx-write gate) and
``meridian.mcp.handlers.notes_decisions.handle_audit_figure_table_provenance``
(this codebase's own whole-document audit tool, powered by
``meridian.outputs_indexer.resolve_output_with_fallback``) each resolve a
structural artifact (a docx figure or table) to its generating
output/script and classify the result -- but neither persists a durable
record that the resolution happened, what it found, or which document/item
it was run for. Every result is a plain dict, returned once and gone. A
receiving executor (or a human reviewing a handoff) had no way to see,
after the fact, whether a given artifact's provenance had ever actually
been resolved, let alone what that resolution found.

This module closes that gap with the exact same pattern
``meridian/code_intel_receipt.py`` and ``meridian/slot_manifest_receipt.py``
already established: a durable, project-scoped row written to the
already-migrated ``action_audit_log`` table (see
``meridian/db/workspace.py``'s ``record_action_audit_event``/
``get_action_audit_log``) -- no new migration, no SQLite/Postgres parity
work needed, since both backends already carry that table.

**Receipt shape mirrors ``bind_artifact_provenance``'s own per-binding
contract** (see ``extensions/meridian-outputs/meridian_outputs/
provenance.py::bind_artifact_provenance``'s docstring for the authoritative
definition of each field): one receipt per artifact resolution, carrying
``status`` (``resolved``/``hash_mismatch``/``orphaned``/``unresolved``),
``match_type`` (``exact``/``basename``/``None``), ``generating_script`` (the
GENERATOR half of media-to-output-to-generator provenance -- see W31-A's own
fix in ``provenance.py::_bind_one_artifact``, which stopped silently
discarding this field), and ``resolved_sha256``. ``record_artifact_
provenance_receipts_batch`` writes one such row per entry in a
``bind_artifact_provenance``-shaped ``bindings`` list in one call, matching
"record a receipt whenever bind_artifact_provenance runs for a real write"
from this item's own discovery brief.

**No item-id column on action_audit_log.** Same constraint
``code_intel_receipt.py``/``slot_manifest_receipt.py`` already work around:
``get_action_audit_log`` filters only on
``tenant_id``/``project_id``/``event_type``/``created_at``, with no per-
artifact column. Per-artifact matching is therefore done CLIENT-SIDE against
each row's JSON ``detail`` field (``detail["artifact_id"]``), scanning
newest-first up to a bounded number of rows -- exactly the technique
``find_recent_prospect_receipt_with_context``/``find_recent_slot_manifest_
receipt`` use for the same reason.

**Wired into a real, reachable call site (unlike ``slot_manifest_
receipt.py``'s deliberately deferred wiring for its own item).** ``meridian.
mcp.handlers.notes_decisions.handle_audit_figure_table_provenance`` is a
real, live MCP tool in CORE meridian with direct ``db``/``project_id``
access -- unlike ``extensions/meridian-outputs`` and
``extensions/meridian-docs``, which are both standalone, independently
installable MCP server packages with NO dependency on ``meridian`` core
and therefore no access to ``action_audit_log`` at all (confirmed: neither
package's ``pyproject.toml`` depends on ``meridian``, and neither module
ever imports ``meridian.db``). That handler already computes, for every
figure and table in a document, exactly the resolved-status/match-type/
generating-script/hash verdict this module's receipt schema was designed to
carry -- see that handler's own call site for the ``ok``/``mismatch``/
``orphan``/``ambiguous``/``unresolved`` -> ``resolved``/``hash_mismatch``/
``orphaned``/``unresolved`` status-vocabulary translation this module's
receipt uses (the SAME translation is documented here so the two vocabularies
never drift silently out of sync -- see :data:`AUDIT_STATUS_TO_BINDING_STATUS`).

**Known, deliberate scope limit (documented, not silently omitted):**
``meridian_outputs.provenance.bind_artifact_provenance`` itself -- the
extension's own fail-closed docx-write gate, and
``extensions/meridian-docs``'s parallel, independently-reimplemented gate in
``docs_intel.py`` -- run in a SEPARATE, standalone process with no access to
this module or to ``action_audit_log`` (see above). Wiring an actual
producer call from either extension would require either (a) giving a
currently dependency-free, independently-installable extension a new hard
dependency on meridian core's DB layer -- a real architectural change well
outside this item's scope -- or (b) proxying every real docx write through
a core-side chokepoint that does not exist yet, since neither extension's
public docx-mutating tool surface (``insert_caption``, ``relocate_figure``,
...) forwards an ``artifact_provenance`` argument today (confirmed via a
repo-wide grep: zero hits). That wiring is real, valuable follow-up work --
tracked in this item's own commit/task-log notes -- but is out of THIS
item's declared scope, exactly the same "documented, not silently omitted"
posture ``slot_manifest_receipt.py``'s own module docstring already
establishes as accepted precedent in this codebase.
"""
from __future__ import annotations

import json
from typing import Any

from . import db as db_module

#: event_type recorded in action_audit_log for an artifact-provenance
#: resolution receipt.
RECEIPT_EVENT_TYPE = "artifact_provenance_receipt"

#: Default number of most-recent rows to scan when looking up a receipt for
#: one specific artifact_id (see the module docstring's "no item-id column"
#: note).
_DEFAULT_SCAN_LIMIT = 50

#: The four canonical statuses ``bind_artifact_provenance`` classifies a
#: binding into -- see that function's own docstring for the authoritative
#: definition of each. Any other value passed to
#: :func:`record_artifact_provenance_receipt` is stored as-is (this module
#: does not itself enforce the enum), but callers translating from a
#: different vocabulary (e.g. the audit tool's own ok/mismatch/orphan/
#: ambiguous/unresolved) should map onto one of these four first.
RESOLVED = "resolved"
HASH_MISMATCH = "hash_mismatch"
ORPHANED = "orphaned"
UNRESOLVED = "unresolved"
BINDING_STATUSES = (RESOLVED, HASH_MISMATCH, ORPHANED, UNRESOLVED)

#: The status-vocabulary translation from ``handle_audit_figure_table_
#: provenance``'s own five-way ok/ambiguous/orphan/mismatch/unresolved
#: report (see that handler's docstring in
#: ``meridian/mcp/handlers/notes_decisions.py`` for what each means) onto
#: ``bind_artifact_provenance``'s four-way resolved/hash_mismatch/orphaned/
#: unresolved classification, so a caller wiring the audit tool's own
#: per-figure/per-table entries into a receipt does not have to re-derive
#: this mapping (or drift from it) independently. ``ambiguous`` maps to
#: ``unresolved`` because ``bind_artifact_provenance`` itself classifies an
#: ambiguous multi-candidate basename match as UNRESOLVED, never a status of
#: its own -- see that function's own docstring.
AUDIT_STATUS_TO_BINDING_STATUS: "dict[str, str]" = {
    "ok": RESOLVED,
    "mismatch": HASH_MISMATCH,
    "orphan": ORPHANED,
    "ambiguous": UNRESOLVED,
    "unresolved": UNRESOLVED,
}


def _receipt_detail(row: "dict[str, Any]") -> "dict[str, Any]":
    """Best-effort JSON-decode of a stored receipt row's ``detail`` field.
    Never raises -- an unparsable/missing detail is treated as empty, not a
    crash, matching ``code_intel_receipt``/``slot_manifest_receipt``'s own
    posture.
    """
    try:
        parsed = json.loads((row or {}).get("detail") or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def receipt_status(row: "dict[str, Any] | None") -> "str | None":
    """Public accessor: the ``status`` string stored on a receipt row (one
    of :data:`BINDING_STATUSES`), or ``None`` for a missing/unparsable row
    or one with no recorded status. Exists so callers outside this module
    never need to reach into the private ``_receipt_detail``/raw ``detail``
    JSON shape directly.
    """
    if not row:
        return None
    status = _receipt_detail(row).get("status")
    return status if isinstance(status, str) else None


async def record_artifact_provenance_receipt(
    db: Any,
    *,
    project_id: str,
    artifact_id: str,
    kind: "str | None" = None,
    canonical_path: "str | None" = None,
    status: str,
    match_type: "str | None" = None,
    evidence: "str | None" = None,
    generating_script: "str | None" = None,
    resolved_sha256: "str | None" = None,
    reason: "str | None" = None,
    item_id: "str | None" = None,
    document_id: "str | None" = None,
    tenant_id: "str | None" = None,
    actor: "str | None" = None,
) -> "dict[str, Any] | None":
    """Persist ONE artifact-provenance resolution as a durable receipt.

    Mirrors ``code_intel_receipt.record_prospect_receipt``/
    ``slot_manifest_receipt.record_slot_manifest_receipt``'s contract
    exactly: best-effort and fully guarded -- a receipt-write failure must
    never break the caller's already-completed resolution. Returns the
    stored row, or ``None`` when nothing could be written (no
    ``project_id``/``artifact_id`` to attribute it to, or an unexpected DB
    error).

    ``item_id``/``document_id`` are both optional and independent --
    whichever identifying anchor the caller has on hand (a sprint item id
    for a write-gate caller, a document id for the audit-tool caller) is
    stored so :func:`find_recent_artifact_provenance_receipt` (scoped by
    ``artifact_id``) and a future item/document-scoped lookup both have
    something to match against.
    """
    if not project_id or not artifact_id:
        return None
    try:
        detail = json.dumps({
            "artifact_id": artifact_id,
            "kind": kind,
            "canonical_path": canonical_path,
            "status": status,
            "match_type": match_type,
            "evidence": evidence,
            "generating_script": generating_script,
            "resolved_sha256": resolved_sha256,
            "reason": reason,
            "item_id": item_id,
            "document_id": document_id,
        })
        return await db_module.record_action_audit_event(
            db, RECEIPT_EVENT_TYPE,
            tenant_id=tenant_id, project_id=project_id,
            actor=actor, detail=detail,
        )
    except Exception:  # noqa: BLE001 -- logging must never break the caller's resolution
        return None


async def record_artifact_provenance_receipts_batch(
    db: Any,
    *,
    project_id: str,
    bindings: "list[dict[str, Any]]",
    item_id: "str | None" = None,
    document_id: "str | None" = None,
    tenant_id: "str | None" = None,
    actor: "str | None" = None,
) -> "list[dict[str, Any]]":
    """Record one receipt per entry in a ``bind_artifact_provenance``-shaped
    ``bindings`` list (``{"artifact_id", "kind", "canonical_path", "status",
    "match_type", "evidence", "generating_script", "resolved_sha256",
    "reason"}`` -- extra/missing keys are tolerated, defaulting to ``None``).

    This is the "record a receipt whenever bind_artifact_provenance runs for
    a real write" entry point this item's discovery brief asks for. A
    binding missing ``artifact_id`` is skipped (mirrors ``record_artifact_
    provenance_receipt``'s own no-op for a missing identity) rather than
    raising, since one malformed entry must never abort receipts for the
    rest of the batch. Returns the list of rows actually written (shorter
    than ``bindings`` when any entries were skipped or failed to write).
    """
    written: "list[dict[str, Any]]" = []
    for binding in bindings or []:
        if not isinstance(binding, dict):
            continue
        row = await record_artifact_provenance_receipt(
            db,
            project_id=project_id,
            artifact_id=binding.get("artifact_id"),
            kind=binding.get("kind"),
            canonical_path=binding.get("canonical_path"),
            status=binding.get("status"),
            match_type=binding.get("match_type"),
            evidence=binding.get("evidence"),
            generating_script=binding.get("generating_script"),
            resolved_sha256=binding.get("resolved_sha256"),
            reason=binding.get("reason"),
            item_id=item_id,
            document_id=document_id,
            tenant_id=tenant_id,
            actor=actor,
        )
        if row is not None:
            written.append(row)
    return written


async def find_recent_artifact_provenance_receipt(
    db: Any,
    *,
    project_id: str,
    artifact_id: str,
    tenant_id: "str | None" = None,
    since: "str | None" = None,
    scan_limit: int = _DEFAULT_SCAN_LIMIT,
) -> "dict[str, Any] | None":
    """Return the newest artifact-provenance receipt recorded for
    ``(project_id, artifact_id)``, or ``None`` when no receipt exists
    (either for this project at all, or specifically for this artifact id,
    or none recorded no earlier than ``since``).

    ``since`` is an inclusive lower bound on the receipt's ``created_at``
    (same string-comparable ``YYYY-MM-DD HH:MM:SS`` form the rest of this
    codebase's timestamps use) -- typically an item's own ``claimed_at`` or
    a document audit's start time, so a receipt from a stale, earlier pass
    does not count as evidence for the CURRENT resolution. Mirrors
    ``code_intel_receipt.find_recent_prospect_receipt``/``slot_manifest_
    receipt.find_recent_slot_manifest_receipt``'s own freshness contract.

    ``action_audit_log`` has no ``artifact_id`` column (see the module
    docstring), so this fetches up to ``scan_limit`` most-recent rows of
    :data:`RECEIPT_EVENT_TYPE` for the project (newest first, via
    ``get_action_audit_log``'s own ``ORDER BY created_at DESC``) and returns
    the first whose stored ``detail["artifact_id"]`` matches -- never raises
    for an unverifiable/DB-error condition, that degrades to ``None`` (no
    receipt found) rather than crashing a caller that must never let a
    best-effort lookup break a mandatory path.
    """
    if not project_id or not artifact_id:
        return None
    try:
        rows = await db_module.get_action_audit_log(
            db, project_id=project_id, tenant_id=tenant_id,
            event_type=RECEIPT_EVENT_TYPE, since=since,
            limit=max(1, int(scan_limit)),
        )
    except Exception:  # noqa: BLE001 -- an unverifiable lookup must never wedge the caller
        return None
    for row in rows:
        if _receipt_detail(row).get("artifact_id") == artifact_id:
            return row
    return None


async def find_recent_artifact_provenance_receipts_for_document(
    db: Any,
    *,
    project_id: str,
    document_id: str,
    tenant_id: "str | None" = None,
    since: "str | None" = None,
    scan_limit: int = _DEFAULT_SCAN_LIMIT,
) -> "list[dict[str, Any]]":
    """Return every receipt recorded for ``(project_id, document_id)``,
    newest first -- the document-scoped counterpart to
    :func:`find_recent_artifact_provenance_receipt`'s single-artifact
    lookup, for a caller (e.g. a handoff-readiness summary) that wants
    "every artifact this document's last audit resolved", not just one.

    Never raises: an unverifiable/DB-error condition degrades to ``[]``
    (no receipts found), same posture as every other lookup in this module.
    """
    if not project_id or not document_id:
        return []
    try:
        rows = await db_module.get_action_audit_log(
            db, project_id=project_id, tenant_id=tenant_id,
            event_type=RECEIPT_EVENT_TYPE, since=since,
            limit=max(1, int(scan_limit)),
        )
    except Exception:  # noqa: BLE001
        return []
    return [row for row in rows if _receipt_detail(row).get("document_id") == document_id]
