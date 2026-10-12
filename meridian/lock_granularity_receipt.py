"""Durable lock-granularity/fallback receipt for resource claims (54d2c2af).

The gap this closes: :func:`meridian.mcp.handler._sprint_item_resource_claim_gate`
reports what grain of lock it actually acquired (``symbol`` vs the widened
``coarse`` whole-file grain) and *why* it widened (``fallback_reason``) only in
the ephemeral response payload of that one ``claim_sprint_item`` call. There is
no durable record after the fact -- a human (or a later session) auditing
"did this item's parallel-write protection actually hold real symbol-range
granularity, or did it silently widen to a whole file?" has nothing to query
except a stale response nobody kept, or scattered server logs.

This module writes ONE durable, machine-checkable row per resource processed
by the gate, using the SAME append-only ``action_audit_log`` table (and the
same ``record_action_audit_event`` / ``get_action_audit_log`` primitives)
:mod:`meridian.code_intel_receipt` already established for exactly this kind
of "durable receipt, no new migration" pattern -- deliberately mirrored here
rather than inventing a parallel mechanism.

Unlike :mod:`meridian.code_intel_receipt`, this receipt is NOT gated behind a
capability-manifest opt-in: recording it is a cheap, best-effort, always-on
side effect of the resource-lock gate itself (same posture as
``meridian.db.locks.release_symbol``'s best-effort ``resource_released``
project-event publish) -- there is no "old projects broken by this existing"
concern because nothing about claim behavior changes by writing the receipt;
only the STRICT rejection behavior (54d2c2af, see
``_sprint_item_resource_claim_gate``'s ``strict_resource_locking`` /
``allow_file_fallback`` parameters) is opt-in.

Best-effort and fully guarded: a receipt-write failure must NEVER break the
underlying claim call that already succeeded or failed on its own merits.
"""
from __future__ import annotations

import json
from typing import Any

from . import db as db_module

#: event_type recorded in action_audit_log for a lock-granularity receipt.
RECEIPT_EVENT_TYPE = "resource_lock_granularity_receipt"


async def record_lock_granularity_receipt(
    db: Any,
    *,
    tenant_id: "str | None",
    project_id: "str | None",
    session_id: "str | None",
    item_id: "str | None",
    resource: str,
    requested_granularity: str,
    achieved_granularity: str,
    reason: "str | None" = None,
    approved: "bool | None" = None,
) -> "dict[str, Any] | None":
    """Write ONE durable lock-granularity receipt to ``action_audit_log``.

    ``requested_granularity`` is what the item's ``touches_resources``
    declaration asked for (currently always ``"symbol"`` -- the only grain
    that can be affected by a fallback; a plain ``file:`` entry always gets
    exactly what it asked for and is not receipted here). ``achieved_granularity``
    is what was actually acquired: ``"symbol"`` (real AST-resolved range lock),
    ``"coarse"`` (widened to a whole-file lock), or ``"unresolved"``/``"rejected"``
    (no lock acquired for this resource at all -- see the caller's ``ok`` field
    for whether that rejected the whole claim). ``reason`` carries the
    ``fallback_reason``/rejection cause (e.g. ``"no_source_supplied"``,
    ``"unparseable"``, ``"symbol_not_found"``, ``"ambiguous_symbol"``).
    ``approved`` is ``True``/``False`` when a fallback happened under explicit
    ``allow_file_fallback=True`` approval (54d2c2af strict mode), ``None`` when
    the call wasn't in strict mode at all (fallback allowed implicitly, the
    pre-54d2c2af default behavior).

    Returns the stored row, or ``None`` when nothing could be written (no
    ``project_id`` to attribute it to, or an unexpected DB error) -- never
    raises.
    """
    if not project_id:
        return None
    try:
        detail = json.dumps({
            "item_id": item_id,
            "resource": resource,
            "requested_granularity": requested_granularity,
            "achieved_granularity": achieved_granularity,
            "reason": reason,
            "fallback_approved": approved,
        })
        return await db_module.record_action_audit_event(
            db, RECEIPT_EVENT_TYPE,
            tenant_id=tenant_id, project_id=project_id,
            actor=session_id or None, detail=detail,
        )
    except Exception:  # noqa: BLE001 -- logging must never break the caller's claim
        return None


async def get_lock_granularity_receipts(
    db: Any,
    *,
    project_id: str,
    tenant_id: "str | None" = None,
    item_id: "str | None" = None,
    session_id: "str | None" = None,
    since: "str | None" = None,
    limit: int = 50,
) -> "list[dict[str, Any]]":
    """Read back lock-granularity receipts, newest first.

    Read-only, best-effort: an unverifiable/broken audit log yields ``[]``
    rather than raising, matching :func:`meridian.code_intel_receipt.
    find_recent_prospect_receipt`'s posture. ``item_id``/``session_id`` filter
    client-side on the JSON ``detail`` payload (the shared ``action_audit_log``
    table itself only indexes ``project_id``/``tenant_id``/``event_type``/
    ``created_at``, same as every other receipt built on this table).
    """
    try:
        rows = await db_module.get_action_audit_log(
            db, project_id=project_id, tenant_id=tenant_id,
            event_type=RECEIPT_EVENT_TYPE, since=since, limit=limit,
        )
    except Exception:  # noqa: BLE001 -- an unverifiable check must never wedge the caller
        return []
    out: "list[dict[str, Any]]" = []
    for row in rows:
        try:
            detail = json.loads(row.get("detail") or "{}")
        except Exception:  # noqa: BLE001
            detail = {}
        if item_id and detail.get("item_id") != item_id:
            continue
        if session_id and row.get("actor") != session_id:
            continue
        merged = dict(row)
        merged.update(detail)
        out.append(merged)
    return out
