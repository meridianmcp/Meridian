"""Durable, project-scoped receipts for figure-slot-manifest reconciliation
results (follow-up to sprint item 5cc3d745 "W31-C", this item d44e7692).

WHY THIS EXISTS: ``tools.meridian_fallbacks.figure_slot_manifest.
reconcile_slot_manifest`` returns a fail-closed verdict
(``MANIFEST_COMPLETE``/``MANIFEST_INCOMPLETE``/``MANIFEST_CONTRADICTORY``)
describing whether a figure-slot promotion batch's manifest is safe to
promote -- but that verdict, as returned, is a plain in-memory dict. Nothing
about running ``reconcile_slot_manifest`` persists a durable record that the
check happened, what it found, or which sprint item it was run for. A
receiving executor (or a human reviewing a handoff) had no way to see, after
the fact, whether a given item's figure-slot work had ever been reconciled
at all, let alone what the result was.

This module closes that gap with the exact same pattern
``meridian/code_intel_receipt.py`` already established for code-intel
prospecting receipts: a durable, project-scoped row written to the already-
migrated ``action_audit_log`` table (see ``meridian/db/workspace.py``'s
``record_action_audit_event``/``get_action_audit_log``) -- no new migration,
no SQLite/Postgres parity work needed, since both backends already carry
that table.

**Read-back, not enforcement.** Unlike ``code_intel_receipt.py``'s
completion-time gate, this module does not (yet) block anything -- it only
records a reconciliation result (:func:`record_slot_manifest_receipt`) and
looks one back up by project + sprint-item id
(:func:`find_recent_slot_manifest_receipt`). ``meridian/handoff.py``'s
``build_slot_manifest_readiness_for_handoff`` uses the lookup half to surface
"does this pending item have a recorded slot-manifest reconciliation
receipt, and what did it say" as an additive readiness signal in
``generate_handoff`` -- see that function's own docstring for the exact
shape.

**No item-id column on action_audit_log.** Same constraint
``code_intel_receipt.py`` already works around: ``get_action_audit_log``
filters only on ``tenant_id``/``project_id``/``event_type``/``created_at``,
with no per-item column. Per-item matching is therefore done CLIENT-SIDE
against each row's JSON ``detail`` field (``detail["item_id"]``), scanning
newest-first up to a bounded number of rows -- exactly the technique
``find_recent_prospect_receipt_with_context`` uses for the same reason.

**Known, deliberate scope limits (documented, not silently omitted):**

* There is, as of this module's introduction, no production call site that
  actually invokes :func:`record_slot_manifest_receipt` after a real
  ``reconcile_slot_manifest`` run -- ``reconcile_slot_manifest`` /
  ``transactional_merge.promote()`` currently have zero callers anywhere in
  ``meridian/`` or any MCP tool (confirmed by a full grep at the time this
  module was written). Wiring an actual producer is real, valuable follow-up
  work, but is out of this item's declared scope -- see the sprint item's
  own notes.
* ``build_slot_manifest_readiness_for_handoff`` (in ``meridian/handoff.py``)
  is, like ``build_promotion_readiness_for_handoff`` before it, wired into
  ``generate_handoff`` for ``mode in {"full", "delta"}`` only, and its
  ``slot_manifest_readiness`` out-param is not (yet) threaded through any of
  the three live MCP/HTTP transports (``mcp/handler.py``,
  ``mcp/stdio_handler.py``, ``routes/handoff.py``) -- the same gap
  ``promotion_readiness`` itself already has today (verified by reading all
  three call sites). A direct/programmatic caller of ``generate_handoff``
  sees the signal; a transport-mediated MCP/HTTP caller does not, yet.
"""
from __future__ import annotations

import json
from typing import Any

from . import db as db_module

#: event_type recorded in action_audit_log for a slot-manifest reconciliation
#: receipt.
RECEIPT_EVENT_TYPE = "slot_manifest_reconciliation_receipt"

#: Default number of most-recent rows to scan when looking up a receipt for
#: one specific item_id (see the module docstring's "no item-id column" note).
_DEFAULT_SCAN_LIMIT = 50


def _receipt_detail(row: "dict[str, Any]") -> "dict[str, Any]":
    """Best-effort JSON-decode of a stored receipt row's ``detail`` field.
    Never raises -- an unparsable/missing detail is treated as empty, not a
    crash, matching ``code_intel_receipt._receipt_detail``'s own posture.
    """
    try:
        parsed = json.loads((row or {}).get("detail") or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def receipt_verdict(row: "dict[str, Any] | None") -> "str | None":
    """Public accessor: the ``verdict`` string stored on a receipt row (one
    of ``reconcile_slot_manifest``'s ``MANIFEST_COMPLETE``/
    ``MANIFEST_INCOMPLETE``/``MANIFEST_CONTRADICTORY`` values), or ``None``
    for a missing/unparsable row or one with no recorded verdict. Exists so
    callers outside this module (e.g. ``meridian.handoff.
    build_slot_manifest_readiness_for_handoff``) never need to reach into
    the private ``_receipt_detail``/raw ``detail`` JSON shape directly.
    """
    if not row:
        return None
    verdict = _receipt_detail(row).get("verdict")
    return verdict if isinstance(verdict, str) else None


async def record_slot_manifest_receipt(
    db: Any,
    *,
    project_id: str,
    item_id: str,
    reconciliation: "dict[str, Any]",
    tenant_id: "str | None" = None,
    actor: "str | None" = None,
) -> "dict[str, Any] | None":
    """Persist ONE ``reconcile_slot_manifest()`` result as a durable receipt.

    ``reconciliation`` is the plain dict ``reconcile_slot_manifest`` itself
    returns (``{"verdict", "reasons", "expected_slot_ids", "buckets",
    "unclassified_slot_ids", "duplicate_assignments", "unknown_slot_ids",
    "structural_errors", "counts", "schema_version"}``) -- this function
    stores every one of those fields verbatim in the receipt's JSON
    ``detail`` alongside the ``item_id`` it belongs to, so a later reader
    (:func:`find_recent_slot_manifest_receipt`, or
    ``meridian.handoff.build_slot_manifest_readiness_for_handoff``) can see
    exactly what was found, not just a bare verdict string.

    Raises :class:`TypeError` when ``reconciliation`` is not a mapping at
    all -- a caller programming error (passing something other than
    ``reconcile_slot_manifest``'s own return value), not a data problem.
    Otherwise best-effort and fully guarded, matching
    ``code_intel_receipt.record_prospect_receipt``'s contract exactly: a
    receipt-write failure must never break the caller's already-completed
    reconciliation. Returns the stored row, or ``None`` when nothing could
    be written (no ``project_id``/``item_id`` to attribute it to, or an
    unexpected DB error).
    """
    if not project_id or not item_id:
        return None
    if not isinstance(reconciliation, dict):
        raise TypeError(
            "reconciliation must be the dict returned by "
            "tools.meridian_fallbacks.figure_slot_manifest.reconcile_slot_manifest, "
            f"got {type(reconciliation).__name__!r}"
        )
    try:
        detail = json.dumps({
            "item_id": item_id,
            "schema_version": reconciliation.get("schema_version"),
            "verdict": reconciliation.get("verdict"),
            "reasons": reconciliation.get("reasons"),
            "expected_slot_ids": reconciliation.get("expected_slot_ids"),
            "unclassified_slot_ids": reconciliation.get("unclassified_slot_ids"),
            "duplicate_assignments": reconciliation.get("duplicate_assignments"),
            "unknown_slot_ids": reconciliation.get("unknown_slot_ids"),
            "structural_errors": reconciliation.get("structural_errors"),
            "counts": reconciliation.get("counts"),
        })
        return await db_module.record_action_audit_event(
            db, RECEIPT_EVENT_TYPE,
            tenant_id=tenant_id, project_id=project_id,
            actor=actor, detail=detail,
        )
    except Exception:  # noqa: BLE001 -- logging must never break the caller's reconciliation
        return None


async def find_recent_slot_manifest_receipt(
    db: Any,
    *,
    project_id: str,
    item_id: str,
    tenant_id: "str | None" = None,
    since: "str | None" = None,
    scan_limit: int = _DEFAULT_SCAN_LIMIT,
) -> "dict[str, Any] | None":
    """Return the newest slot-manifest reconciliation receipt recorded for
    ``(project_id, item_id)``, or ``None`` when no receipt exists (either
    for this project at all, or specifically for this item id, or none
    recorded no earlier than ``since``).

    ``since`` is an inclusive lower bound on the receipt's ``created_at``
    (same string-comparable ``YYYY-MM-DD HH:MM:SS`` form the rest of this
    codebase's timestamps use) -- typically an item's own ``claimed_at``, so
    a receipt from a stale, earlier pass at the item does not count as
    evidence for the CURRENT claim. Mirrors
    ``code_intel_receipt.find_recent_prospect_receipt``'s own freshness
    contract.

    ``action_audit_log`` has no ``item_id`` column (see the module
    docstring), so this fetches up to ``scan_limit`` most-recent rows of
    :data:`RECEIPT_EVENT_TYPE` for the project (newest first, via
    ``get_action_audit_log``'s own ``ORDER BY created_at DESC``) and returns
    the first whose stored ``detail["item_id"]`` matches -- never raises for
    an unverifiable/DB-error condition, that degrades to ``None`` (no
    receipt found) rather than crashing a caller like
    ``build_slot_manifest_readiness_for_handoff`` that must never let a
    best-effort lookup break a mandatory handoff.
    """
    if not project_id or not item_id:
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
        if _receipt_detail(row).get("item_id") == item_id:
            return row
    return None
