"""Durable, project-scoped receipts for DOCX local-structure-pointer
freshness evidence (sprint item 8c047a44, "DOCS-R2-E: make convergence,
reindex, local-pointer, and manifest evidence explicit and generated").

WHY THIS EXISTS: ``extensions/meridian-docs/meridian_docs/docs_intel.py``'s
``get_structure_freshness`` / ``check_structure_staleness`` /
``get_local_structure_elements`` compute a live, deterministic verdict about
whether the ``para_id``-keyed local structural sidecar (``docx_headings`` /
``docx_figures`` / ``docx_tables``) is trustworthy -- ``indexed`` / complete
/ ``stale`` / ``trustworthy``, sha256-based, fail-closed via
``StructureIndexNotTrustworthyError`` when untrustworthy. Like
``tools.meridian_fallbacks.figure_slot_manifest.reconcile_slot_manifest``
before ``meridian/slot_manifest_receipt.py`` existed, that verdict is
returned as a plain dict and never durably recorded: nothing shows, after
the fact, whether a given sprint item's local-pointer work was ever checked
for staleness, let alone what it found.

**Deliberately does NOT import ``meridian_docs``/``docs_intel`` here, and
does NOT call ``get_structure_freshness`` itself.** ``docs_intel.py``'s own
module docstring states it is a "Pure library -- every function is
deterministic and unit-tested" with zero dependency on the Meridian server,
its DB, or any async runtime; ``meridian/outputs_indexer.py``'s sibling
``get_convergence_state`` is pinned by ``tests/test_outputs_convergence.py``
to stay "a pure, side-effect-free, lock-guarded read" for the exact same
reason -- a receipt-writing wrapper belongs in a CALLER, never merged into
the check function itself. ``meridian_docs`` is also a genuinely optional,
separately-installed extension with no ``pypi-dependencies`` entry in this
repo's own ``pixi.toml`` (see the "Testing extensions/meridian-docs locally"
project note) -- ``meridian/`` itself must never hard-depend on it being
importable just to load this receipt module.

This module instead persists a CALLER-SUPPLIED freshness/staleness result
dict, exactly as ``meridian/slot_manifest_receipt.py`` already does for
``figure_slot_manifest.reconcile_slot_manifest()``'s result: same
"read-back, not enforcement" posture, same ``action_audit_log`` storage, same
client-side ``detail["item_id"]`` scan (no per-item column on that table).
A caller that already has ``meridian_docs`` available (an MCP handler, or an
executor session that just ran ``get_structure_freshness`` /
``check_structure_staleness`` against a real sidecar) hands this module the
resulting dict; this module never inspects a ``.docx`` or a sidecar sqlite
file on its own.

**Known, deliberate scope limit (documented, not silently omitted), same
shape as ``slot_manifest_receipt.py``'s own:** there is, as of this module's
introduction, no production call site wiring a real
``get_structure_freshness``/``check_structure_staleness`` result into
:func:`record_structure_freshness_receipt` -- doing that safely means adding
a new call inside ``extensions/meridian-docs``'s own MCP handlers (a
~22,800-line file at the time of writing) or ``meridian/fallbacks/__init__.py``'s
guarded-``importlib`` delegation point, and verifying it against that
package's own large test suite. That wiring, and a completion-time GATE
(mirroring ``code_intel_receipt.verify_code_intel_prospecting``), are real,
valuable follow-up work, deliberately deferred out of this pass -- see this
item's own final report for the explicit scope decision.
"""
from __future__ import annotations

import json
from typing import Any

from . import db as db_module

#: event_type recorded in action_audit_log for a local-structure-pointer
#: freshness receipt.
RECEIPT_EVENT_TYPE = "docs_structure_freshness_receipt"

#: Default number of most-recent rows to scan when looking up a receipt for
#: one specific item_id (see the module docstring's "no item-id column" note).
_DEFAULT_SCAN_LIMIT = 50

#: Fields carried verbatim from a ``get_structure_freshness()`` result into a
#: receipt's ``detail``. ``check_structure_staleness()``'s narrower
#: ``{"stale", "source_path", "reason"}`` shape is a strict subset of these
#: keys, so the same recorder accepts either result dict unmodified.
_FRESHNESS_FIELDS = (
    "indexed", "complete", "stale", "trustworthy",
    "source_path", "source_sha256", "reason",
)


def _receipt_detail(row: "dict[str, Any]") -> "dict[str, Any]":
    """Best-effort JSON-decode of a stored receipt row's ``detail`` field.
    Never raises -- an unparsable/missing detail is treated as empty, matching
    every sibling receipt module's own posture (``code_intel_receipt``,
    ``slot_manifest_receipt``, ``code_index_receipt``).
    """
    try:
        parsed = json.loads((row or {}).get("detail") or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def receipt_trustworthy(row: "dict[str, Any] | None") -> "bool | None":
    """Public accessor: the ``trustworthy`` bool stored on a receipt row, or
    ``None`` for a missing/unparsable row, one with no recorded value, or one
    written from a bare ``check_structure_staleness()`` result (which never
    carries a ``trustworthy`` field -- only ``get_structure_freshness()``
    does). Exists so callers never need to reach into the private
    ``_receipt_detail``/raw ``detail`` JSON shape directly.
    """
    if not row:
        return None
    val = _receipt_detail(row).get("trustworthy")
    return val if isinstance(val, bool) else None


async def record_structure_freshness_receipt(
    db: Any,
    *,
    project_id: "str | None",
    item_id: "str | None",
    freshness: "dict[str, Any]",
    tenant_id: "str | None" = None,
    actor: "str | None" = None,
) -> "dict[str, Any] | None":
    """Persist ONE local-structure-pointer freshness/staleness result as a
    durable receipt.

    ``freshness`` is the plain dict returned by ``docs_intel.
    get_structure_freshness()`` (``{"indexed", "complete", "stale",
    "trustworthy", "source_path", "source_sha256", "reason"}``) or the
    narrower ``docs_intel.check_structure_staleness()``
    (``{"stale", "source_path", "reason"}``) -- this function stores
    whichever of :data:`_FRESHNESS_FIELDS` are present verbatim in the
    receipt's JSON ``detail`` alongside the ``item_id`` it belongs to, so a
    later reader (:func:`find_recent_structure_freshness_receipt`) can see
    exactly what was found, not just a bare boolean.

    Raises :class:`TypeError` when ``freshness`` is not a mapping at all --
    a caller programming error (passing something other than one of those
    two functions' own return values), not a data problem. Otherwise
    best-effort and fully guarded, matching
    ``slot_manifest_receipt.record_slot_manifest_receipt`` /
    ``code_index_receipt.record_reindex_scope_receipt``'s contract exactly:
    a receipt-write failure must never break the caller's already-completed
    freshness check. Returns the stored row, or ``None`` when nothing could
    be written (no ``project_id``/``item_id`` to attribute it to, or an
    unexpected DB error).
    """
    if not project_id or not item_id:
        return None
    if not isinstance(freshness, dict):
        raise TypeError(
            "freshness must be the dict returned by "
            "meridian_docs.docs_intel.get_structure_freshness (or "
            "check_structure_staleness), "
            f"got {type(freshness).__name__!r}"
        )
    try:
        detail: "dict[str, Any]" = {"item_id": item_id}
        for key in _FRESHNESS_FIELDS:
            if key in freshness:
                detail[key] = freshness.get(key)
        return await db_module.record_action_audit_event(
            db, RECEIPT_EVENT_TYPE,
            tenant_id=tenant_id, project_id=project_id,
            actor=actor, detail=json.dumps(detail),
        )
    except Exception:  # noqa: BLE001 -- logging must never break the caller's freshness check
        return None


async def find_recent_structure_freshness_receipt(
    db: Any,
    *,
    project_id: str,
    item_id: str,
    tenant_id: "str | None" = None,
    since: "str | None" = None,
    scan_limit: int = _DEFAULT_SCAN_LIMIT,
) -> "dict[str, Any] | None":
    """Return the newest local-structure-pointer freshness receipt recorded
    for ``(project_id, item_id)``, or ``None`` when no receipt exists
    (either for this project at all, or specifically for this item id, or
    none recorded no earlier than ``since``).

    ``since`` is an inclusive lower bound on the receipt's ``created_at``
    (same string-comparable ``YYYY-MM-DD HH:MM:SS`` form the rest of this
    codebase's timestamps use) -- typically an item's own ``claimed_at``, so
    a receipt from a stale, earlier pass at the item does not count as
    evidence for the CURRENT claim. Mirrors every sibling receipt module's
    own freshness contract.

    Never raises for an unverifiable/DB-error condition -- that degrades to
    ``None`` (no receipt found) rather than crashing a caller.
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
