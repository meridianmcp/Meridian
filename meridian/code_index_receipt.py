"""Durable, project-scoped receipts for bounded reindex-scope evidence
(sprint item 8c047a44, "DOCS-R2-E: make convergence, reindex, local-pointer,
and manifest evidence explicit and generated").

WHY THIS EXISTS: ``meridian.code_index.compute_bounded_reindex_scope`` (c95d0c12)
is a pure, non-recursive pre-flight classifier for an ``index_repository(...)``
call -- it reports how many nested worktree copies live under a repo root and
whether a full-root reindex is ``safe`` or should be narrowed to a
``recommended_repo_path`` instead. It is genuinely useful evidence (this exact
check is what the 2026-08-05 502 postmortem needed and did not have), but as
written it only ever returns a plain in-memory dict to whoever calls it --
nothing about calling it leaves a durable trace that the check happened, for
which project/item, or what it found.

This module closes that gap with the SAME pattern already established twice
in this codebase for exactly this kind of "a pure check function exists, but
nothing durably records that it ran" gap:
``meridian/code_intel_receipt.py`` (a8c0f3b7, code-intel prospecting) ->
``meridian/handoff_receipt.py`` (1b7eb437, handoff-token verification) ->
``meridian/slot_manifest_receipt.py`` (d44e7692, figure-slot-manifest
reconciliation). A durable, project-scoped row is written to the
already-migrated ``action_audit_log`` table (``meridian/db/workspace.py``'s
``record_action_audit_event`` / ``get_action_audit_log``) -- no new
migration, no SQLite/Postgres parity work needed, since both backends already
carry that table.

**Read-back, not enforcement.** Like ``slot_manifest_receipt.py``, this
module does not (yet) block anything -- it only records a reindex-scope
result (:func:`record_reindex_scope_receipt`, or the convenience
:func:`generate_reindex_scope_receipt` that computes AND records in one
call -- the literal "explicit and generated" evidence this item asks for)
and looks one back up by project + sprint-item id
(:func:`find_recent_reindex_scope_receipt`). Wiring a completion-time GATE
(mirroring ``code_intel_receipt.verify_code_intel_prospecting``'s
capability-manifest-gated block/warn posture) is real, valuable follow-up
work, deliberately deferred -- see this item's own final report for the
explicit scope decision, matching ``slot_manifest_receipt.py``'s own
precedent of shipping the receipt layer before a production call site or a
completion gate exists for it.

**No item-id column on action_audit_log.** Same constraint every sibling
receipt module already works around: ``get_action_audit_log`` filters only on
``tenant_id``/``project_id``/``event_type``/``created_at``, with no per-item
column. Per-item matching is therefore done CLIENT-SIDE against each row's
JSON ``detail`` field (``detail["item_id"]``), scanning newest-first up to a
bounded number of rows -- exactly ``find_recent_slot_manifest_receipt``'s
technique.
"""
from __future__ import annotations

import json
from typing import Any

from . import code_index as _code_index
from . import db as db_module

#: event_type recorded in action_audit_log for a reindex-scope receipt.
RECEIPT_EVENT_TYPE = "reindex_scope_receipt"

#: Default number of most-recent rows to scan when looking up a receipt for
#: one specific item_id (see the module docstring's "no item-id column" note).
_DEFAULT_SCAN_LIMIT = 50


def _receipt_detail(row: "dict[str, Any]") -> "dict[str, Any]":
    """Best-effort JSON-decode of a stored receipt row's ``detail`` field.
    Never raises -- an unparsable/missing detail is treated as empty, matching
    ``code_intel_receipt._receipt_detail`` / ``slot_manifest_receipt.
    _receipt_detail``'s own posture.
    """
    try:
        parsed = json.loads((row or {}).get("detail") or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def receipt_safe(row: "dict[str, Any] | None") -> "bool | None":
    """Public accessor: the ``safe`` bool stored on a receipt row (whether
    ``compute_bounded_reindex_scope`` judged a full-root reindex safe), or
    ``None`` for a missing/unparsable row or one with no recorded value.
    Exists so callers outside this module never need to reach into the
    private ``_receipt_detail``/raw ``detail`` JSON shape directly.
    """
    if not row:
        return None
    val = _receipt_detail(row).get("safe")
    return val if isinstance(val, bool) else None


async def record_reindex_scope_receipt(
    db: Any,
    *,
    project_id: "str | None",
    item_id: "str | None",
    scope: "dict[str, Any]",
    tenant_id: "str | None" = None,
    actor: "str | None" = None,
) -> "dict[str, Any] | None":
    """Persist ONE ``compute_bounded_reindex_scope()`` result as a durable receipt.

    ``scope`` is the plain dict ``compute_bounded_reindex_scope`` itself
    returns (``{"repo_path", "excluded_paths", "nested_worktree_count",
    "safe", "recommended_repo_path"}``) -- this function stores every one of
    those fields verbatim in the receipt's JSON ``detail`` alongside the
    ``item_id`` it belongs to, so a later reader
    (:func:`find_recent_reindex_scope_receipt`) can see exactly what was
    found, not just a bare boolean.

    Raises :class:`TypeError` when ``scope`` is not a mapping at all -- a
    caller programming error (passing something other than
    ``compute_bounded_reindex_scope``'s own return value), not a data
    problem. Otherwise best-effort and fully guarded, matching
    ``slot_manifest_receipt.record_slot_manifest_receipt``'s contract
    exactly: a receipt-write failure must never break the caller's
    already-completed scope check. Returns the stored row, or ``None`` when
    nothing could be written (no ``project_id``/``item_id`` to attribute it
    to, or an unexpected DB error).
    """
    if not project_id or not item_id:
        return None
    if not isinstance(scope, dict):
        raise TypeError(
            "scope must be the dict returned by "
            "meridian.code_index.compute_bounded_reindex_scope, "
            f"got {type(scope).__name__!r}"
        )
    try:
        detail = json.dumps({
            "item_id": item_id,
            "repo_path": scope.get("repo_path"),
            "excluded_paths": scope.get("excluded_paths"),
            "nested_worktree_count": scope.get("nested_worktree_count"),
            "safe": scope.get("safe"),
            "recommended_repo_path": scope.get("recommended_repo_path"),
        })
        return await db_module.record_action_audit_event(
            db, RECEIPT_EVENT_TYPE,
            tenant_id=tenant_id, project_id=project_id,
            actor=actor, detail=detail,
        )
    except Exception:  # noqa: BLE001 -- logging must never break the caller's scope check
        return None


async def generate_reindex_scope_receipt(
    db: Any,
    *,
    project_id: "str | None",
    item_id: "str | None",
    repo_path: str,
    worktree_threshold: int = 5,
    tenant_id: "str | None" = None,
    actor: "str | None" = None,
) -> "dict[str, Any]":
    """Compute AND durably record reindex-scope evidence in one call -- the
    "explicit and generated" entry point this item asks for, rather than
    requiring every caller to compute the scope and remember to persist it
    separately.

    Returns ``{"scope": <compute_bounded_reindex_scope() dict>, "receipt":
    <stored row, or None>}``. Never raises for a missing ``project_id``/
    ``item_id`` (the receipt is simply not written, mirroring
    :func:`record_reindex_scope_receipt`'s own no-op contract) -- the
    computed ``scope`` is always returned regardless, since
    ``compute_bounded_reindex_scope`` itself never raises.
    """
    scope = _code_index.compute_bounded_reindex_scope(
        repo_path, worktree_threshold=worktree_threshold,
    )
    receipt = await record_reindex_scope_receipt(
        db, project_id=project_id, item_id=item_id, scope=scope,
        tenant_id=tenant_id, actor=actor,
    )
    return {"scope": scope, "receipt": receipt}


async def find_recent_reindex_scope_receipt(
    db: Any,
    *,
    project_id: str,
    item_id: str,
    tenant_id: "str | None" = None,
    since: "str | None" = None,
    scan_limit: int = _DEFAULT_SCAN_LIMIT,
) -> "dict[str, Any] | None":
    """Return the newest reindex-scope receipt recorded for ``(project_id,
    item_id)``, or ``None`` when no receipt exists (either for this project
    at all, or specifically for this item id, or none recorded no earlier
    than ``since``).

    ``since`` is an inclusive lower bound on the receipt's ``created_at``
    (same string-comparable ``YYYY-MM-DD HH:MM:SS`` form the rest of this
    codebase's timestamps use) -- typically an item's own ``claimed_at``, so
    a receipt from a stale, earlier pass at the item does not count as
    evidence for the CURRENT claim. Mirrors
    ``slot_manifest_receipt.find_recent_slot_manifest_receipt``'s own
    freshness contract.

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
