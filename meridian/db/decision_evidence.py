"""9149e132 — typed, code-linked decision evidence with deterministic
planning retrieval.

Meridian already has ``decisions_pinned`` (the "constitution": title/body/
category/priority/assumption) and ``decisions_pinned.code_anchor`` — a single
nullable free-text file path, surfaced to ``claim_file`` for that path. That
is a coarse, single-target, unstructured anchor: it cannot express "this
decision rests on THIS symbol, at THIS revision, plus these OTHER targets",
carries no assumptions/evidence/confidence/applicability-scope of its own,
and has no supersession lineage independent of the decision it is attached
to.

This module adds a SEPARATE, typed evidence layer: each ``decision_evidence``
row links ONE pinned decision to a durable, structured pointer (the SAME
generic pointer primitive :mod:`meridian.pointers` already uses for
``sprint_item_pointers`` — reused here verbatim via
:func:`meridian.pointers.validate_pointer`, NOT reinvented), plus:

* ``evidence`` — the searchable text describing what was found (this is the
  ``planning_search`` body column for this source type).
* ``assumptions`` — optional free text: what this evidence assumes but does
  not itself verify.
* ``applicability_scope`` — optional free text/JSON: where this evidence is
  known to apply (e.g. a file-pattern, a version range).
* ``confidence`` — optional ``0.0..1.0``. ADVISORY ONLY: see the module-level
  safety note below — this field, and any semantic/vector score computed
  from it, NEVER gates a mutation or a pointer resolution.
* ``status`` (active | superseded | reversed) + ``supersedes_id`` /
  ``superseded_by`` — an append-only supersession chain (mirrors
  ``decisions_pinned.status``/``superseded_by`` exactly), plus a distinct
  ``reversed`` state (the evidence turned out to be wrong, as opposed to
  merely replaced by newer evidence) with a required ``reversal_reason``.
  Nothing is ever deleted — history stays queryable via ``include_superseded``.

Rows are keyed to ``project_id`` (never omitted from a query — see every
function below) so cross-project isolation is structural, not a filter a
caller can forget to apply.

Registered into ``planning_search`` via a ``_PLANNING_SOURCE_SPECS["decision_evidence"]``
entry (see ``meridian/db/__init__.py``) — the SAME generic
lexical-only (FTS5 BM25 / Postgres tsvector) retrieval path every other
source type already uses. No separate index, no separate query logic here.

---------------------------------------------------------------------------
SAFETY CONTRACT (non-negotiable — see planning_search's optional
``rerank_semantic`` and the safety tests in tests/test_new_v25.py):

Semantic/vector similarity (when :mod:`meridian.semantic_search` is enabled)
may be layered ON TOP of an already-lexically-retrieved result set to
RE-ORDER it (never to add rows lexical search did not already find — see
planning_search's own docstring for exactly how that boundary is enforced).
Nothing in this module accepts a similarity score as authorization for a
write: every mutation function below (``supersede_decision_evidence``,
``reverse_decision_evidence``) takes an EXACT ``evidence_id`` — a real
database primary key the caller already has — never a search query, a
"best match", or anything resolved by ranking. There is no code path from
"a semantic search returned X with score Y" to "row X got superseded/
reversed/mutated". confidence is stored purely as caller-supplied metadata
about the evidence itself; it is never read by any function in this module
to decide what to do.
---------------------------------------------------------------------------

Mirrors the minimal single-table CRUD shape already established by
``meridian/db/vector_index_state.py`` and ``meridian/db/executor_reports.py``
— a create/update lifecycle over one table, no state machine, no children.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from meridian.db import _new_id, _row_to_dict
from meridian.pointers import validate_pointer

#: The lifecycle states a decision_evidence row can be in. 'active' is the
#: default and the only status planning_search surfaces unless a caller
#: explicitly asks for a broader status filter (mirrors decisions_pinned's
#: own "status defaults to active-only" convention — see
#: db._planning_pg_source_results / db._planning_sqlite_source_results).
DECISION_EVIDENCE_STATUSES: frozenset[str] = frozenset(
    {"active", "superseded", "reversed"}
)


def _now_iso() -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS' — matches update_pinned_decision's own
    cross-dialect-safe timestamp convention (computed in Python, not via a
    SQL now()/datetime('now') call, which differ in behaviour across SQLite
    and Postgres — see the project's own now() vs clock_timestamp() note)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def _migrate_decision_evidence(db: aiosqlite.Connection) -> None:
    """9149e132 — create ``decision_evidence`` on existing SQLite DBs.

    Guarded migration (no inline CREATE INDEX in the unguarded base schema
    literals — 2026-07-04 outage rule): the table + its indexes are created
    here, called unconditionally from init_db, so both fresh and existing
    DBs pick it up identically (CREATE TABLE/INDEX IF NOT EXISTS is already
    idempotent — no separate CREATE_TABLES literal entry needed, mirroring
    vector_index_state.py / executor_reports.py). Idempotent. Mirrored in
    pg_adapter._migrate_pg_decision_evidence.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_evidence (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            decision_id TEXT NOT NULL,
            version TEXT,
            pointer TEXT NOT NULL,
            evidence TEXT NOT NULL,
            assumptions TEXT,
            applicability_scope TEXT,
            confidence REAL,
            status TEXT NOT NULL DEFAULT 'active',
            supersedes_id TEXT,
            superseded_by TEXT,
            reversal_reason TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_decision_evidence_decision "
        "ON decision_evidence(decision_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_decision_evidence_project "
        "ON decision_evidence(project_id)"
    )
    await db.commit()


def _row_to_evidence(row: Any) -> "dict[str, Any] | None":
    """Row -> dict with the JSON ``pointer`` column deserialized back into a
    dict (mirrors ``meridian.pointers.row_to_pointer``'s deserialize-on-read
    contract for ``sprint_item_pointers``)."""
    d = _row_to_dict(row)
    if d is None:
        return None
    raw_pointer = d.get("pointer")
    if isinstance(raw_pointer, str):
        try:
            d["pointer"] = json.loads(raw_pointer)
        except (ValueError, TypeError):
            d["pointer"] = None
    return d


async def create_decision_evidence(
    db: aiosqlite.Connection,
    project_id: str,
    decision_id: str,
    pointer: dict[str, Any],
    evidence: str,
    *,
    assumptions: str | None = None,
    applicability_scope: str | None = None,
    confidence: float | None = None,
    version: str | None = None,
    supersedes_id: str | None = None,
) -> dict[str, Any]:
    """Validate + persist ONE typed decision-evidence link. Returns the row.

    ``pointer`` is the SAME ``{source_type, targets:[{uri, selector,
    subSelector?, target_kind?}], label?}`` shape
    :func:`meridian.pointers.validate_pointer` already enforces for sprint
    item pointers — reused verbatim, not reinvented (raises ``ValueError``
    — actually :class:`meridian.pointers.PointerValidationError`, a
    ``ValueError`` subclass — on a malformed pointer BEFORE any write).

    ``confidence``, when given, is clamped to ``[0.0, 1.0]``. It is stored
    as caller-supplied metadata ONLY — see the module docstring's safety
    contract: nothing in this module (or in planning_search's optional
    semantic rerank) ever reads ``confidence`` or a similarity score to
    decide what to retrieve or what to mutate.

    ``supersedes_id`` (optional): when given, the OLD row is atomically
    flipped to ``status='superseded'`` with ``superseded_by`` pointing at
    the freshly-created row, in the SAME call — mirrors
    ``supersede_pinned_decision``'s atomic old-row-retire pattern. Prefer
    calling :func:`supersede_decision_evidence` instead, which validates the
    old row actually exists (and belongs to this project) before creating
    the replacement; this parameter exists so
    :func:`supersede_decision_evidence` can share this function's insert
    logic rather than duplicating it.
    """
    normalized_pointer = validate_pointer(pointer)  # raises PointerValidationError
    if confidence is not None:
        confidence = max(0.0, min(1.0, float(confidence)))
    eid = _new_id()
    pointer_json = json.dumps(normalized_pointer, ensure_ascii=False, sort_keys=True)
    await db.execute(
        "INSERT INTO decision_evidence "
        "(id, project_id, decision_id, version, pointer, evidence, "
        "assumptions, applicability_scope, confidence, status, supersedes_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
        (
            eid, project_id, decision_id, version, pointer_json, evidence,
            assumptions, applicability_scope, confidence, supersedes_id,
        ),
    )
    if supersedes_id:
        await db.execute(
            "UPDATE decision_evidence SET status = 'superseded', "
            "superseded_by = ?, updated_at = ? "
            "WHERE id = ? AND project_id = ?",
            (eid, _now_iso(), supersedes_id, project_id),
        )
    await db.commit()
    created = await get_decision_evidence(db, eid, project_id=project_id)
    assert created is not None  # just written
    return created


async def get_decision_evidence(
    db: aiosqlite.Connection,
    evidence_id: str,
    *,
    project_id: str | None = None,
) -> dict[str, Any] | None:
    """Fetch one decision_evidence row by id.

    ``project_id`` (optional) enforces tenant/project isolation at the read
    layer when supplied — a caller checking ownership (e.g.
    :func:`supersede_decision_evidence`) always passes it, so a row from a
    DIFFERENT project can never be read back as if it belonged to this one.
    """
    if project_id is not None:
        async with db.execute(
            "SELECT * FROM decision_evidence WHERE id = ? AND project_id = ?",
            (evidence_id, project_id),
        ) as cur:
            row = await cur.fetchone()
    else:
        async with db.execute(
            "SELECT * FROM decision_evidence WHERE id = ?", (evidence_id,)
        ) as cur:
            row = await cur.fetchone()
    return _row_to_evidence(row)


async def list_decision_evidence(
    db: aiosqlite.Connection,
    project_id: str,
    decision_id: str,
    *,
    include_superseded: bool = False,
) -> list[dict[str, Any]]:
    """All evidence rows for ONE decision, newest first.

    Always scoped to ``project_id`` (isolation) AND ``decision_id``.
    Defaults to ``status='active'`` only; pass ``include_superseded=True``
    to see the full supersession/reversal history (nothing is ever hard
    deleted by this module).
    """
    if include_superseded:
        sql = (
            "SELECT * FROM decision_evidence "
            "WHERE project_id = ? AND decision_id = ? "
            "ORDER BY created_at DESC, id DESC"
        )
        params: tuple[Any, ...] = (project_id, decision_id)
    else:
        sql = (
            "SELECT * FROM decision_evidence "
            "WHERE project_id = ? AND decision_id = ? AND status = 'active' "
            "ORDER BY created_at DESC, id DESC"
        )
        params = (project_id, decision_id)
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [e for e in (_row_to_evidence(r) for r in rows) if e is not None]


async def supersede_decision_evidence(
    db: aiosqlite.Connection,
    project_id: str,
    old_evidence_id: str,
    pointer: dict[str, Any],
    evidence: str,
    *,
    assumptions: str | None = None,
    applicability_scope: str | None = None,
    confidence: float | None = None,
) -> dict[str, Any]:
    """Atomic supersede: create a new active evidence row and retire the old
    one (mirrors ``supersede_pinned_decision``).

    ``old_evidence_id`` MUST be an EXACT, caller-supplied primary key — see
    the module docstring's safety contract. This function does not accept
    (and has no parameter for) a search query, a similarity score, or a
    "best match" result; there is no way to reach this function from a
    ranked/semantic result without the caller having already resolved the
    exact row id themselves.

    Raises ``ValueError`` if ``old_evidence_id`` does not exist in
    ``project_id`` — supersession can never silently create an orphaned
    chain link or reach across a project boundary.
    """
    old = await get_decision_evidence(db, old_evidence_id, project_id=project_id)
    if old is None:
        raise ValueError(
            f"decision_evidence {old_evidence_id!r} not found in project "
            f"{project_id!r}"
        )
    return await create_decision_evidence(
        db, project_id, old["decision_id"], pointer, evidence,
        assumptions=assumptions,
        applicability_scope=applicability_scope,
        confidence=confidence,
        version=old.get("version"),
        supersedes_id=old_evidence_id,
    )


async def reverse_decision_evidence(
    db: aiosqlite.Connection,
    project_id: str,
    evidence_id: str,
    reason: str,
) -> dict[str, Any]:
    """Mark one evidence row ``reversed`` (it turned out to be wrong/
    disproven — distinct from ``superseded``, which just means "replaced by
    newer evidence"). Never deletes; the row stays in history.

    ``evidence_id`` MUST be an EXACT, caller-supplied primary key (same
    contract as :func:`supersede_decision_evidence` — see the module
    docstring's safety note). ``reason`` is required and non-empty.

    Raises ``ValueError`` if the row does not exist in ``project_id``, or if
    ``reason`` is blank.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("reverse_decision_evidence requires a non-empty reason")
    existing = await get_decision_evidence(db, evidence_id, project_id=project_id)
    if existing is None:
        raise ValueError(
            f"decision_evidence {evidence_id!r} not found in project "
            f"{project_id!r}"
        )
    await db.execute(
        "UPDATE decision_evidence SET status = 'reversed', reversal_reason = ?, "
        "updated_at = ? WHERE id = ? AND project_id = ?",
        (reason, _now_iso(), evidence_id, project_id),
    )
    await db.commit()
    updated = await get_decision_evidence(db, evidence_id, project_id=project_id)
    assert updated is not None
    return updated
