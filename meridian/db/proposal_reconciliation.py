"""4eedeef8 -- RECONCILE: DB-facing wiring for the legacy proposal-predecessor
audit and its opt-in migration path.

Pure classification lives in ``meridian.proposal_lineage_audit`` (no DB
access there -- mirrors ``meridian.provenance_authority``'s leaf-module
contract, unit-testable without a DB fixture). This module is the thin layer
that fetches real rows through the SAME read seams already exposed
(``get_workspace_proposals``, ``get_proposal_lineage_links``,
``get_proposal_links``) and is the ONLY place that calls
``link_proposal_lineage`` / ``link_proposal_evidence`` to actually persist a
migrated relation -- and only for candidates a caller has explicitly
reviewed and named via ``accept`` (see
``proposal_lineage_audit.plan_legacy_migration``).

Imported at the BOTTOM of db/__init__.py, immediately after ``.proposal_links``
and ``.proposal_lineage`` (this module composes both), mirroring every other
extracted submodule's import-ordering convention.
"""
from __future__ import annotations

from typing import Any

import aiosqlite

from meridian.proposal_lineage_audit import (
    audit_legacy_proposal_lineage as _classify_lineage,
    audit_promotion_evidence_backlinks as _classify_evidence,
    plan_legacy_migration as _plan_migration,
)

# Safety bound on how many pages of get_workspace_proposals a single audit
# call will fetch (page size is that function's own max clamp of 100) --
# mirrors the defensive "a normal case needs a handful, this only guards a
# pathological data set" bounds used throughout this codebase (e.g.
# proposal_lineage._MAX_LINEAGE_HOPS). 200 pages * 100 rows = up to 20,000
# proposals scanned before truncation is reported (never silently).
_MAX_PROPOSAL_SCAN_PAGES = 200
_PROPOSAL_PAGE_SIZE = 100


async def _fetch_all_proposals(
    db: aiosqlite.Connection,
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> "tuple[list[dict[str, Any]], bool]":
    """Page through ``get_workspace_proposals(status='all', ...)`` until
    exhausted (or :data:`_MAX_PROPOSAL_SCAN_PAGES` is hit). Returns
    ``(proposals, truncated)`` -- ``truncated=True`` means more rows exist
    than were scanned, reported explicitly rather than silently capped."""
    from meridian.db import get_workspace_proposals  # noqa: PLC0415

    out: list[dict[str, Any]] = []
    offset = 0
    truncated = True
    for _ in range(_MAX_PROPOSAL_SCAN_PAGES):
        page = await get_workspace_proposals(
            db, status="all", tenant_id=tenant_id, project_id=project_id,
            limit=_PROPOSAL_PAGE_SIZE, offset=offset,
        )
        out.extend(page)
        if len(page) < _PROPOSAL_PAGE_SIZE:
            truncated = False
            break
        offset += _PROPOSAL_PAGE_SIZE
    return out, truncated


async def audit_legacy_proposal_lineage(
    db: aiosqlite.Connection,
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> "dict[str, Any]":
    """Fetch every proposal (and its existing ``proposal_lineage`` edges) in
    scope and classify legacy free-text predecessor references against them.
    Read-only -- writes nothing. See
    ``meridian.proposal_lineage_audit.audit_legacy_proposal_lineage`` for the
    full report shape; this wrapper adds ``proposal_scan_truncated``.
    """
    from meridian.db import get_proposal_lineage_links  # noqa: PLC0415

    proposals, truncated = await _fetch_all_proposals(
        db, tenant_id=tenant_id, project_id=project_id,
    )
    existing_by_pid: dict[str, list[dict[str, Any]]] = {}
    for p in proposals:
        pid = p.get("id")
        if not pid:
            continue
        existing_by_pid[pid] = await get_proposal_lineage_links(
            db, pid, tenant_id=tenant_id,
        )
    report = _classify_lineage(proposals, existing_by_pid)
    report["proposal_scan_truncated"] = truncated
    return report


async def migrate_legacy_proposal_lineage(
    db: aiosqlite.Connection,
    *,
    accept: "list[str] | None" = None,
    tenant_id: str | None = None,
    project_id: str | None = None,
    actor: str | None = None,
    label: str | None = None,
    dry_run: bool = True,
) -> "dict[str, Any]":
    """Opt-in migration step for :func:`audit_legacy_proposal_lineage`.

    Re-runs the audit FRESH (never trusts a caller-supplied stale report --
    a concurrent caller may have already migrated or superseded a candidate
    since it was last reported), resolves ``accept`` against it via
    :func:`meridian.proposal_lineage_audit.plan_legacy_migration`, and --
    only when ``dry_run=False`` -- calls ``link_proposal_lineage`` for each
    resolved candidate.

    ``accept=None`` (the default) plans and applies nothing: this function
    is always safe to call with no arguments beyond ``db`` -- it degrades to
    a pure audit. ``dry_run=True`` (the default) reports what WOULD be
    applied without writing, even if ``accept`` names real candidates --
    both ``accept`` (naming which candidates) AND ``dry_run=False``
    (authorizing the write) are required before anything is persisted.

    Each resolved candidate is applied independently -- one failure (e.g. a
    genuine race where a concurrent caller's edge now makes this one a
    cycle) is recorded under ``errors`` and does not abort the rest.
    ``link_proposal_lineage`` is itself idempotent and tenant/cycle-checked,
    so a duplicate or unsafe accepted candidate is still safely rejected
    (or is a no-op) even if this function's own fresh-audit re-check somehow
    missed it (e.g. a same-instant concurrent write).

    Returns ``{"dry_run", "would_apply", "rejected_keys", "applied",
    "errors", "report"}`` -- ``report`` is the fresh audit this call is
    based on, included so a caller can see exactly what informed the
    decision without a second round-trip.
    """
    from meridian.db import link_proposal_lineage  # noqa: PLC0415

    report = await audit_legacy_proposal_lineage(
        db, tenant_id=tenant_id, project_id=project_id,
    )
    plan = _plan_migration(report, accept)

    applied: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if not dry_run:
        for cand in plan["to_apply"]:
            try:
                row = await link_proposal_lineage(
                    db,
                    cand["from_proposal_id"],
                    cand["to_proposal_id"],
                    cand["relation_type"],
                    tenant_id=tenant_id,
                    label=label,
                    actor=actor,
                )
                applied.append({
                    "candidate_key": cand["candidate_key"], "lineage": row,
                })
            except Exception as exc:  # noqa: BLE001 -- one bad candidate must never abort the rest
                errors.append({
                    "candidate_key": cand["candidate_key"], "error": str(exc),
                })

    return {
        "dry_run": dry_run,
        "would_apply": plan["to_apply"],
        "rejected_keys": plan["rejected_keys"],
        "applied": applied,
        "errors": errors,
        "report": report,
    }


async def audit_legacy_promotion_evidence(
    db: aiosqlite.Connection,
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> "dict[str, Any]":
    """Fetch every promoted proposal (and its existing
    ``proposal_evidence_links`` rows) in scope and classify missing
    promotion backlinks. Read-only. See
    ``meridian.proposal_lineage_audit.audit_promotion_evidence_backlinks``
    for the full report shape; this wrapper adds ``proposal_scan_truncated``.
    """
    from meridian.db import get_proposal_links  # noqa: PLC0415

    proposals, truncated = await _fetch_all_proposals(
        db, tenant_id=tenant_id, project_id=project_id,
    )
    existing_by_pid: dict[str, list[dict[str, Any]]] = {}
    for p in proposals:
        pid = p.get("id")
        si_id = p.get("promoted_to_sprint_item_id")
        proj = p.get("project_id")
        if not pid or not si_id or not proj:
            continue
        existing_by_pid[pid] = await get_proposal_links(db, proj, pid)
    report = _classify_evidence(proposals, existing_by_pid)
    report["proposal_scan_truncated"] = truncated
    return report


async def migrate_legacy_promotion_evidence(
    db: aiosqlite.Connection,
    *,
    accept: "list[str] | None" = None,
    tenant_id: str | None = None,
    project_id: str | None = None,
    actor: str | None = None,
    dry_run: bool = True,
) -> "dict[str, Any]":
    """Opt-in backfill step for :func:`audit_legacy_promotion_evidence`.

    Unlike :func:`migrate_legacy_proposal_lineage`, the underlying relation
    here is already explicit (``promoted_to_sprint_item_id``), not inferred
    from prose -- so when ``accept`` is omitted (``None``) and
    ``dry_run=False``, every current ``would_migrate`` candidate from a
    fresh audit is backfilled (mirrors
    ``meridian.db.sprint_items.reconcile_stale_claims``'s own "dry_run=False
    applies every classified-safe item" convention for a structural, not
    inferred, classification). Passing an explicit ``accept`` list still
    narrows to exactly those candidate_keys, for a caller that wants
    fine-grained control anyway.

    Returns the same shape as :func:`migrate_legacy_proposal_lineage`.
    """
    from meridian.db import link_proposal_evidence  # noqa: PLC0415

    report = await audit_legacy_promotion_evidence(
        db, tenant_id=tenant_id, project_id=project_id,
    )
    would_migrate = report.get("would_migrate") or []
    if accept is None:
        to_apply = list(would_migrate)
        rejected_keys: list[str] = []
    else:
        plan = _plan_migration(report, accept)
        to_apply = plan["to_apply"]
        rejected_keys = plan["rejected_keys"]

    applied: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if not dry_run:
        for cand in to_apply:
            try:
                row = await link_proposal_evidence(
                    db,
                    cand["project_id"],
                    cand["proposal_id"],
                    cand["entity_type"],
                    cand["entity_id"],
                    actor=actor,
                )
                applied.append({
                    "candidate_key": cand["candidate_key"], "link": row,
                })
            except Exception as exc:  # noqa: BLE001
                errors.append({
                    "candidate_key": cand["candidate_key"], "error": str(exc),
                })

    return {
        "dry_run": dry_run,
        "would_apply": to_apply,
        "rejected_keys": rejected_keys,
        "applied": applied,
        "errors": errors,
        "report": report,
    }


async def audit_legacy_proposal_references(
    db: aiosqlite.Connection,
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> "dict[str, Any]":
    """Convenience wrapper: run both audits and return one combined report,
    mirroring ``meridian.provenance_authority.classify_legacy_provenance_sources``'s
    identical "one combined wrapper over independent classifiers" shape.
    """
    lineage = await audit_legacy_proposal_lineage(
        db, tenant_id=tenant_id, project_id=project_id,
    )
    evidence = await audit_legacy_promotion_evidence(
        db, tenant_id=tenant_id, project_id=project_id,
    )
    return {
        "schema_version": lineage["schema_version"],
        "proposal_lineage": lineage,
        "promotion_evidence": evidence,
        "total_would_migrate": (
            len(lineage.get("would_migrate") or [])
            + len(evidence.get("would_migrate") or [])
        ),
    }
