"""6cdc5df3 — proposal-to-evidence linkage: durable, typed, queryable.

Before this module the only way to associate a note / finding / sprint item /
decision / artifact with a "proposal" (an investigation or effort that
produces several of those at once) was an INFORMAL convention: bake the
proposal's short id into a free-text field, most commonly ``item_group`` on
one or more sprint items (e.g. ``"artifact-integrity-b7308039"``,
``"meridian-docs-integrity"`` with a proposal id like ``6de68dda`` or
``b7308039`` as a prefix). That convention has no schema, no validation, and
no query path — "show me everything linked to proposal b7308039" required a
manual grep across item_group strings, and could never reach a note, a
finding, a decision, or an artifact at all (none of those tables have an
item_group column).

``proposal_evidence_links`` is the first-class replacement: ONE small table
linking an arbitrary durable ``proposal_id`` (typically a
``workspace_proposals.id`` — see ``db.workspace.promote_workspace_proposal``,
which now writes one of these automatically on every promotion — but any
stable string works, so a short reference id like ``b7308039`` cited in a
note or a /goal block is just as linkable) to any number of
notes/findings/sprint_items/decisions/artifacts. Mirrors the existing
"generic pointer primitive" precedent (``sprint_item_pointers``,
2976e168) in spirit: one table, a type discriminator, no per-domain columns.

Public surface:
  * :func:`link_proposal_evidence` — create one typed link (idempotent).
  * :func:`unlink_proposal_evidence` — remove one link by id.
  * :func:`get_proposal_links` — raw link rows for one proposal.
  * :func:`get_proposal_evidence` — HYDRATED evidence for one proposal: every
    linked note/finding/sprint_item/decision resolved into its real row,
    plus free-form artifact links, in one call. This is what answers
    "what's linked to proposal X" from a handoff read path
    (``meridian.handoff.build_proposal_evidence_for_handoff``).
  * :func:`get_proposal_ids_for_project` — which proposal ids have evidence
    in a project, most-recently-active first.

Imported at the BOTTOM of db/__init__.py (after every table/function this
module reads is already defined), mirroring db.docx_merge and every other
extracted submodule.
"""
from __future__ import annotations

from typing import Any

import aiosqlite

# Shared helpers from the parent db package — available at import time
# because this module is imported at the bottom of db/__init__.py, after
# these names are already defined. Mirrors db.docx_merge's identical pattern.
from meridian.db import (  # noqa: PLC0415
    _new_id,
    _row_to_dict,
    _publish_project_event,
)

# Every supported evidence kind. 'artifact' is deliberately NOT a key in
# _PROPOSAL_ENTITY_TABLE below — there is no single canonical "artifacts"
# table in this schema (a produced artifact might be a docx output, a
# provenance-tracked file, a figure — many subsystems, no one table), so an
# artifact link's entity_id is a free-form identifier (a path, an output id,
# whatever the caller has) described by the link's own ``label`` instead of
# being resolved against a row.
_VALID_PROPOSAL_ENTITY_TYPES = ("note", "finding", "sprint_item", "decision", "artifact")

# entity_type -> (table, id_column). Every one of these tables carries a
# real ``project_id`` column (verified against CREATE_TABLES / the owning
# migration), so a link can be validated to belong to the SAME project as
# the evidence it claims to reference — never a dangling or cross-project id.
_PROPOSAL_ENTITY_TABLE: dict[str, str] = {
    "note": "project_notes",
    "finding": "session_findings",
    "sprint_item": "sprint_items",
    "decision": "decisions_pinned",
}

# entity_type -> plural bucket key used by get_proposal_evidence's return dict.
_PROPOSAL_ENTITY_BUCKET: dict[str, str] = {
    "note": "notes",
    "finding": "findings",
    "sprint_item": "sprint_items",
    "decision": "decisions",
    "artifact": "artifacts",
}


async def _migrate_proposal_evidence_links(db: aiosqlite.Connection) -> None:
    """6cdc5df3 — create proposal_evidence_links if absent.

    Guarded migration (no inline CREATE INDEX in the unguarded base schema
    literal — 2026-07-04 outage rule): the table AND its indexes live here,
    called unconditionally on every startup (idempotent ``IF NOT EXISTS``),
    so both a fresh DB and an upgrading existing DB pick it up. Mirrors
    ``db.docx_merge._migrate_docx_merge_manifests`` (table not present in the
    base CREATE_TABLES literal at all — this guarded migration is the only
    creation path, for either a fresh or an existing DB). Mirrored on the
    Postgres side by ``pg_adapter._migrate_pg_proposal_evidence_links``.

    The UNIQUE index on (project_id, proposal_id, entity_type, entity_id) is
    what makes :func:`link_proposal_evidence` idempotent via
    ``ON CONFLICT ... DO NOTHING`` — the same race-safe pattern already used
    by ``db.docx_merge.record_merge_result``.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS proposal_evidence_links (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            proposal_id TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            label TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_proposal_evidence_links_unique "
        "ON proposal_evidence_links(project_id, proposal_id, entity_type, entity_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_proposal_evidence_links_proposal "
        "ON proposal_evidence_links(project_id, proposal_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_proposal_evidence_links_entity "
        "ON proposal_evidence_links(entity_type, entity_id)"
    )
    await db.commit()


async def link_proposal_evidence(
    db: aiosqlite.Connection,
    project_id: str,
    proposal_id: str,
    entity_type: str,
    entity_id: str,
    *,
    label: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """6cdc5df3 — persist a durable, typed link between a proposal and one
    piece of evidence. Returns the stored (or already-existing) link row.

    ``entity_type`` must be one of :data:`_VALID_PROPOSAL_ENTITY_TYPES`
    (``note`` | ``finding`` | ``sprint_item`` | ``decision`` | ``artifact``).

    For every type EXCEPT ``artifact``, ``entity_id`` MUST name a real row in
    that entity's own table, AND that row must belong to ``project_id`` —
    raises ``ValueError`` otherwise. This is deliberate: a link that could
    silently point at a deleted or cross-project row would be exactly the
    kind of unverifiable reference this feature replaces. ``artifact`` has no
    single canonical table in this schema (an artifact may be a docx output,
    a provenance-tracked file, a generated figure — several subsystems, no
    one row to check), so its ``entity_id`` is a free-form identifier (a
    path, an output id, a pointer) and only "non-empty string" is enforced;
    pass a descriptive ``label`` so the link is still human-legible.

    Idempotent: linking the same ``(project_id, proposal_id, entity_type,
    entity_id)`` tuple twice is a safe no-op — the second call returns the
    SAME stored row rather than raising or creating a duplicate (UNIQUE-
    index-backed ``ON CONFLICT DO NOTHING``, mirroring
    ``db.docx_merge.record_merge_result``).
    """
    entity_type = (entity_type or "").strip().lower()
    if entity_type not in _VALID_PROPOSAL_ENTITY_TYPES:
        raise ValueError(
            f"entity_type must be one of {_VALID_PROPOSAL_ENTITY_TYPES}, got {entity_type!r}"
        )
    proposal_id = (proposal_id or "").strip()
    if not proposal_id:
        raise ValueError("proposal_id must be a non-empty string")
    entity_id = (entity_id or "").strip()
    if not entity_id:
        raise ValueError("entity_id must be a non-empty string")
    if not project_id:
        raise ValueError("project_id must be a non-empty string")
    if label is not None:
        from meridian.secret_redaction import check_for_secrets  # noqa: PLC0415
        check_for_secrets(label, context="proposal evidence label")

    if entity_type != "artifact":
        table = _PROPOSAL_ENTITY_TABLE[entity_type]
        # Table name comes from the closed _PROPOSAL_ENTITY_TABLE mapping
        # above, never user input, so this f-string interpolation is safe.
        async with db.execute(
            f"SELECT project_id FROM {table} WHERE id = ?", (entity_id,)
        ) as cur:
            row = await cur.fetchone()
        row_d = _row_to_dict(row)
        if row_d is None:
            raise ValueError(f"{entity_type} '{entity_id}' does not exist")
        if row_d.get("project_id") != project_id:
            raise ValueError(
                f"{entity_type} '{entity_id}' belongs to a different project "
                f"than '{project_id}'"
            )

    lid = _new_id()
    await db.execute(
        "INSERT INTO proposal_evidence_links "
        "(id, project_id, proposal_id, entity_type, entity_id, label, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (project_id, proposal_id, entity_type, entity_id) DO NOTHING",
        (lid, project_id, proposal_id, entity_type, entity_id, label, actor),
    )
    await db.commit()
    # Re-select rather than trust `lid`: a concurrent caller may have won the
    # UNIQUE index between our check and this insert (same race-safety
    # re-check pattern as record_merge_result), so the row returned is always
    # the one actually stored.
    async with db.execute(
        "SELECT * FROM proposal_evidence_links "
        "WHERE project_id = ? AND proposal_id = ? AND entity_type = ? AND entity_id = ?",
        (project_id, proposal_id, entity_type, entity_id),
    ) as cur:
        row = await cur.fetchone()
    result = _row_to_dict(row) or {"id": lid}
    _publish_project_event(project_id, "proposal_evidence_linked", {
        "proposal_id": proposal_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
    })
    return result


async def unlink_proposal_evidence(db: aiosqlite.Connection, link_id: str) -> bool:
    """6cdc5df3 — delete one evidence link by id. Returns True if a row was removed."""
    async with db.execute(
        "SELECT 1 FROM proposal_evidence_links WHERE id = ?", (link_id,)
    ) as cur:
        existed = await cur.fetchone() is not None
    await db.execute(
        "DELETE FROM proposal_evidence_links WHERE id = ?", (link_id,)
    )
    await db.commit()
    return existed


async def get_proposal_links(
    db: aiosqlite.Connection, project_id: str, proposal_id: str,
) -> list[dict[str, Any]]:
    """6cdc5df3 — raw link rows for one proposal, ordered by id ASC (see
    ``get_sprint_item_pointers`` for why ``id`` alone, not ``created_at``, is
    the deterministic sort key across both SQLite and Postgres).
    """
    async with db.execute(
        "SELECT * FROM proposal_evidence_links "
        "WHERE project_id = ? AND proposal_id = ? ORDER BY id ASC",
        (project_id, proposal_id),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


# 537a7cef — get_proposal_evidence hydrates EVERY linked note/finding/
# sprint_item/decision/artifact into its full stored row with no per-bucket
# bound at all (only the outer proposal-COUNT is capped, by
# handoff.build_proposal_evidence_for_handoff's own limit=10 default — that
# bounds how many PROPOSALS get evidence-hydrated, not how much any ONE
# proposal's evidence weighs). A proposal linked to hundreds of notes/
# findings would pay one full-row SELECT per link with no bound, the same
# class of unbounded-with-fan-in growth capability_contract's per-item
# sections needed 248c0bb9/537a7cef's caps for. Default is well above any
# proposal exercised by this codebase's own tests (single digits) or any
# realistically-scoped real proposal.
_DEFAULT_MAX_PROPOSAL_BUCKET_ITEMS = 50


async def get_proposal_evidence(
    db: aiosqlite.Connection, project_id: str, proposal_id: str,
    *, max_bucket_items: int = _DEFAULT_MAX_PROPOSAL_BUCKET_ITEMS,
) -> dict[str, Any]:
    """6cdc5df3 — answer "what's linked to proposal X", fully hydrated.

    Resolves every link row into the real underlying entity: notes/findings/
    sprint_items/decisions come back as their full stored row (plus
    ``_proposal_link_id`` / ``_proposal_link_label`` so the caller can still
    see the link's own metadata); artifacts — which have no backing table —
    come back as ``{"link_id", "entity_id", "label", "created_at"}``.

    A link whose target row has since been hard-deleted is surfaced under
    ``unresolved`` (never silently dropped) so a stale link is visible rather
    than invisible.

    ``max_bucket_items`` (537a7cef) — caps how many entries EACH of the five
    buckets (notes/findings/sprint_items/decisions/artifacts) hydrates,
    applied over links in their existing deterministic ``id ASC`` order
    (see :func:`get_proposal_links`). A link beyond its bucket's cap is
    skipped BEFORE the per-entity SELECT (or the label lookup, for
    artifacts) — bounding both response size and DB round-trips, not just
    the former. Never a silent drop: the returned ``bucket_truncated`` dict
    carries one ``{truncated, total_candidates, included}`` marker per
    bucket (the SAME shape ``capability_contract._cap_contract_list`` uses),
    and ``link_count`` always reports the TRUE total link count regardless
    of any bucket's truncation. A link skipped for being over its bucket's
    cap is counted in that bucket's ``total_candidates`` but — because it
    was never hydrated — cannot also be checked for staleness, so it will
    not appear in ``unresolved`` even if its target has been deleted; that
    is visible via the count discrepancy in ``bucket_truncated``, not
    itemized. A proposal at or under the cap in every bucket (every
    existing test, and any realistically-scoped real proposal) is
    byte-identical to the pre-cap output.

    Returns::

        {
            "proposal_id": ..., "project_id": ...,
            "notes": [...], "findings": [...], "sprint_items": [...],
            "decisions": [...], "artifacts": [...],
            "link_count": <int>, "unresolved": [...],
            "bucket_truncated": {"notes": {...}, "findings": {...}, ...},
        }
    """
    links = await get_proposal_links(db, project_id, proposal_id)
    buckets: dict[str, list[dict[str, Any]]] = {
        "notes": [], "findings": [], "sprint_items": [], "decisions": [], "artifacts": [],
    }
    bucket_candidates: dict[str, int] = dict.fromkeys(buckets, 0)
    cap = max(0, int(max_bucket_items or 0))
    unresolved: list[dict[str, Any]] = []
    for link in links:
        etype = link.get("entity_type")
        eid = link.get("entity_id")
        bucket_name = _PROPOSAL_ENTITY_BUCKET.get(etype or "")
        if bucket_name is None:
            continue  # unknown/legacy entity_type — skip rather than crash
        bucket_candidates[bucket_name] += 1
        if len(buckets[bucket_name]) >= cap:
            # Over this bucket's cap — skip the hydration query entirely
            # (bounds DB round-trips, not just response size). Still
            # reflected in bucket_candidates above, so never silent.
            continue
        if etype == "artifact":
            buckets["artifacts"].append({
                "link_id": link.get("id"),
                "entity_id": eid,
                "label": link.get("label"),
                "created_at": link.get("created_at"),
            })
            continue
        table = _PROPOSAL_ENTITY_TABLE[etype]
        async with db.execute(f"SELECT * FROM {table} WHERE id = ?", (eid,)) as cur:
            row = await cur.fetchone()
        hydrated = _row_to_dict(row)
        if hydrated is None:
            unresolved.append(link)
            continue
        hydrated = dict(hydrated)
        hydrated["_proposal_link_id"] = link.get("id")
        hydrated["_proposal_link_label"] = link.get("label")
        buckets[bucket_name].append(hydrated)
    bucket_truncated = {
        name: {
            "truncated": bucket_candidates[name] > len(buckets[name]),
            "total_candidates": bucket_candidates[name],
            "included": len(buckets[name]),
        }
        for name in buckets
    }
    return {
        "proposal_id": proposal_id,
        "project_id": project_id,
        **buckets,
        "link_count": len(links),
        "unresolved": unresolved,
        "bucket_truncated": bucket_truncated,
    }


async def get_proposal_ids_for_project(
    db: aiosqlite.Connection, project_id: str, limit: int = 20,
) -> list[str]:
    """6cdc5df3 — distinct proposal ids with >=1 evidence link in this
    project, most-recently-linked first. Powers
    ``meridian.handoff.build_proposal_evidence_for_handoff`` so a handoff can
    surface active proposals without the caller needing to already know
    which proposal ids exist.
    """
    async with db.execute(
        "SELECT proposal_id, MAX(created_at) AS last_at FROM proposal_evidence_links "
        "WHERE project_id = ? GROUP BY proposal_id ORDER BY last_at DESC LIMIT ?",
        (project_id, int(limit)),
    ) as cur:
        rows = await cur.fetchall()
    out: list[str] = []
    for r in rows:
        d = _row_to_dict(r) or {}
        pid = d.get("proposal_id")
        if pid:
            out.append(pid)
    return out
