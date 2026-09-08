"""6d109127 -- durable persistence for SCHEMA: structural_patch, a proposed
manuscript structural edit behind a human approval gate. See
:mod:`meridian.structural_patch` for the closed ``PATCH_OPERATIONS``/
``PATCH_STATUSES`` vocabularies and transition rules this module enforces --
this is the persistence layer built on top of that pure module, mirroring
:mod:`meridian.db.experiment_model`'s identical split from
:mod:`meridian.experiment_model`.

SCHEMA
------

One table, ``structural_patches`` -- a single-table, state-machine shape
(mirrors ``meridian/db/decision_evidence.py``'s single-table-with-lifecycle
convention, not the heavier multi-table + append-only-event-log shape of
``meridian/db/external_jobs.py``; a structural patch has no children and no
need for a separate event history table at this schema-only stage).

``document_id`` / ``target_element_id`` are soft references to
:mod:`meridian.doc_store`'s ``doc_documents`` / ``doc_elements`` rows --
NOT SQL foreign keys, because ``doc_store`` deliberately owns its own schema
outside this module's migration machinery (see doc_store.py's own docstring:
"Schema (owned by this store -- NOT part of db.CREATE_TABLES / migrations)").
``project_id`` / ``proposed_by_session_id`` DO carry real foreign keys since
``projects``/``sessions`` are core tables this migration can safely assume
exist.

No CHECK constraint on ``operation``/``status``: SQLite cannot ALTER a CHECK
constraint in place (see ``migrations.py``'s ``_migrate_workspace_proposals``
for the full-table-rebuild this forces when a vocabulary later grows), so --
mirroring ``decision_evidence``'s and ``verification_runs``' equally
evolving-vocabulary tables -- validation is enforced in Python
(:mod:`meridian.structural_patch`) once, here, rather than duplicated as a
DB constraint that would need a disruptive rebuild the next time the
approval-gate vocabulary grows (e.g. a future ``needs_revision`` status).

HUMAN APPROVAL GATE -- enforced, not decorative
-------------------------------------------------

:func:`transition_structural_patch` requires a non-empty
``decided_by_human_id`` on any transition INTO ``approved``/``rejected`` (and
rejects it being supplied for any other transition) -- exactly mirroring
``meridian.db.experiment_model.transition_attempt``'s ``failure_class``
requirement on transitions into ``failed``/``crashed``. This is what makes
"human approval gate" a real, checked invariant of this schema rather than
just a status string a caller could set unattributed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from meridian.db import _new_id, _row_to_dict
from meridian.structural_patch import (
    validate_content_hash,
    validate_operation,
    validate_payload,
    validate_rationale,
    validate_status,
    validate_transition,
)

_PATCH_COLUMNS = (
    "id", "project_id", "document_id", "target_element_id", "operation",
    "payload", "rationale", "base_content_hash", "status",
    "proposed_by_session_id", "decided_by_human_id", "decision_note",
    "decided_at", "applied_at", "supersedes_patch_id", "created_at",
    "updated_at",
)


def _now_iso() -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS' -- matches
    ``meridian.db.experiment_model``'s / ``meridian.db.decision_evidence``'s
    identical cross-dialect-safe timestamp convention (computed in Python,
    never a SQL ``now()``/``datetime('now')`` call -- see the project's own
    now() vs clock_timestamp() note)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Migration -- guarded, idempotent (2026-07-04 outage rule: no unguarded
# CREATE INDEX on a migration-added column/table in CREATE_TABLES/
# CREATE_TABLES_CORE). Mirrored on Postgres by
# pg_adapter._migrate_pg_structural_patch.
# ---------------------------------------------------------------------------


async def _migrate_structural_patch(db: aiosqlite.Connection) -> None:
    """6d109127 -- create ``structural_patches`` if absent."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS structural_patches (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            document_id TEXT NOT NULL,
            target_element_id TEXT,
            operation TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            rationale TEXT,
            base_content_hash TEXT,
            status TEXT NOT NULL DEFAULT 'proposed',
            proposed_by_session_id TEXT NOT NULL REFERENCES sessions(id),
            decided_by_human_id TEXT,
            decision_note TEXT,
            decided_at TEXT,
            applied_at TEXT,
            supersedes_patch_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_structural_patches_project_status "
        "ON structural_patches(project_id, status, created_at DESC)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_structural_patches_document "
        "ON structural_patches(document_id, status)"
    )
    await db.commit()


def _row_to_patch(row: Any) -> "dict[str, Any] | None":
    d = _row_to_dict(row)
    if d is None:
        return None
    raw = d.get("payload")
    if isinstance(raw, dict):
        d["payload"] = raw
    else:
        try:
            d["payload"] = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            d["payload"] = {}
    return d


async def _require_session(db: Any, project_id: str, session_id: str) -> None:
    async with db.execute(
        "SELECT id FROM sessions WHERE id = ? AND project_id = ?",
        (session_id, project_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise ValueError(
            f"session {session_id!r} does not belong to project {project_id!r}"
        )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def create_structural_patch(
    db: aiosqlite.Connection,
    project_id: str,
    session_id: str,
    *,
    document_id: str,
    operation: str,
    target_element_id: "str | None" = None,
    payload: "dict[str, Any] | None" = None,
    rationale: "str | None" = None,
    base_content_hash: "str | None" = None,
    supersedes_patch_id: "str | None" = None,
) -> dict[str, Any]:
    """Propose a new structural_patch. Always created in status
    ``'proposed'`` -- there is no way to create a patch already
    approved/applied; every patch must pass through the human approval gate
    (see :func:`transition_structural_patch`).

    ``session_id`` must already belong to ``project_id`` (raises
    ``ValueError`` otherwise, same cross-project-write rejection convention
    as ``meridian.db.external_jobs``). When ``supersedes_patch_id`` is
    given, it must name an existing patch in the SAME project -- this
    function does NOT itself transition that prior patch to ``superseded``
    (a separate, explicit ``transition_structural_patch(..., 'superseded')``
    call does that); linking and superseding are deliberately two calls, not
    one bundled side effect.
    """
    project_id = (project_id or "").strip()
    session_id = (session_id or "").strip()
    document_id = (document_id or "").strip()
    if not project_id:
        raise ValueError("create_structural_patch requires a non-empty project_id")
    if not session_id:
        raise ValueError("create_structural_patch requires a non-empty session_id")
    if not document_id:
        raise ValueError("create_structural_patch requires a non-empty document_id")

    await _require_session(db, project_id, session_id)

    op = validate_operation(operation)
    payload_value = validate_payload(payload)
    rationale_value = validate_rationale(rationale)
    hash_value = validate_content_hash(base_content_hash)
    target = target_element_id.strip() if isinstance(target_element_id, str) else None
    target = target or None
    supersedes = (
        supersedes_patch_id.strip() if isinstance(supersedes_patch_id, str) else None
    )
    supersedes = supersedes or None

    if supersedes is not None:
        prior = await get_structural_patch(db, project_id, supersedes)
        if prior is None:
            raise ValueError(
                f"supersedes_patch_id {supersedes!r} not found in project "
                f"{project_id!r}"
            )

    patch_id = _new_id()
    await db.execute(
        "INSERT INTO structural_patches "
        "(id, project_id, document_id, target_element_id, operation, payload, "
        "rationale, base_content_hash, status, proposed_by_session_id, "
        "supersedes_patch_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)",
        (
            patch_id, project_id, document_id, target, op,
            json.dumps(payload_value, ensure_ascii=False, sort_keys=True),
            rationale_value, hash_value, session_id, supersedes,
        ),
    )
    await db.commit()
    created = await get_structural_patch(db, project_id, patch_id)
    assert created is not None  # just written
    return created


async def get_structural_patch(
    db: aiosqlite.Connection, project_id: str, patch_id: str
) -> "dict[str, Any] | None":
    """Fetch one structural_patch by id, scoped to ``project_id`` -- a
    cross-project lookup (right id, wrong project) returns ``None``, exactly
    like a nonexistent id, never leaking the row's existence to the wrong
    tenant."""
    async with db.execute(
        f"SELECT {', '.join(_PATCH_COLUMNS)} FROM structural_patches "
        "WHERE id = ? AND project_id = ?",
        (patch_id, project_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_patch(row)


async def list_structural_patches(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    document_id: "str | None" = None,
    status: "str | None" = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List a project's structural patches, newest-proposed first.

    ``limit`` is clamped to [1, 500] so a caller cannot accidentally pull an
    unbounded result set (mirrors ``meridian.db.external_jobs.list_external_jobs``
    / ``meridian.db.verification_runs.list_verification_runs``).
    """
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if document_id is not None:
        clauses.append("document_id = ?")
        params.append(document_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(validate_status(status))
    limit = max(1, min(int(limit), 500))
    params.append(limit)
    async with db.execute(
        f"SELECT {', '.join(_PATCH_COLUMNS)} FROM structural_patches "
        f"WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, id DESC LIMIT ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [p for p in (_row_to_patch(r) for r in rows) if p is not None]


async def transition_structural_patch(
    db: aiosqlite.Connection,
    project_id: str,
    patch_id: str,
    new_status: str,
    *,
    decided_by_human_id: "str | None" = None,
    decision_note: "str | None" = None,
) -> dict[str, Any]:
    """Validate and apply ``current -> new_status`` on a structural_patch
    (see :func:`meridian.structural_patch.validate_transition` for the legal
    transition table). Idempotent: transitioning to the patch's CURRENT
    status is always a no-op success, never a ``ValueError``.

    ``decided_by_human_id`` is REQUIRED when ``new_status`` is ``'approved'``
    or ``'rejected'`` and REJECTED (must be omitted/``None``) otherwise -- a
    patch that isn't being decided has no decider to record. This is the
    human approval gate's actual enforcement point (see this module's
    docstring). ``decided_at`` is stamped fresh on every transition into
    ``'approved'``/``'rejected'`` (re-deciding an already-decided patch,
    e.g. approved -> rejected, records a new decision timestamp, not the
    original one). ``applied_at`` is stamped the first time a patch enters
    ``'applied'``.
    """
    project_id = (project_id or "").strip()
    current = await get_structural_patch(db, project_id, patch_id)
    if current is None:
        raise ValueError(
            f"structural patch {patch_id!r} not found in project {project_id!r}"
        )

    validated_status = validate_transition(current["status"], new_status)

    if validated_status in ("approved", "rejected"):
        if not decided_by_human_id and current["status"] == validated_status:
            # Idempotent self-transition: reuse the existing decider rather
            # than demanding the caller re-supply it -- a true no-op must
            # never raise.
            decided_by_human_id = current.get("decided_by_human_id")
        if not decided_by_human_id:
            raise ValueError(
                f"transitioning to {validated_status!r} requires "
                "decided_by_human_id -- the human approval gate must record "
                "who decided"
            )
        decided_by_human_id = decided_by_human_id.strip() or None
        if not decided_by_human_id:
            raise ValueError(
                f"transitioning to {validated_status!r} requires a non-empty "
                "decided_by_human_id"
            )
    elif decided_by_human_id is not None:
        raise ValueError(
            "decided_by_human_id is only valid when transitioning to "
            f"'approved' or 'rejected', not {validated_status!r}"
        )

    note_value = validate_rationale(decision_note) if decision_note is not None else None

    now = _now_iso()
    set_clauses = ["status = ?", "updated_at = ?"]
    params: list[Any] = [validated_status, now]

    if validated_status in ("approved", "rejected"):
        set_clauses.append("decided_at = ?")
        params.append(now)
        set_clauses.append("decided_by_human_id = ?")
        params.append(decided_by_human_id)
    if note_value is not None:
        set_clauses.append("decision_note = ?")
        params.append(note_value)
    if validated_status == "applied" and current.get("applied_at") is None:
        set_clauses.append("applied_at = ?")
        params.append(now)

    params.extend([patch_id, project_id])
    await db.execute(
        f"UPDATE structural_patches SET {', '.join(set_clauses)} "
        "WHERE id = ? AND project_id = ?",
        params,
    )
    await db.commit()
    updated = await get_structural_patch(db, project_id, patch_id)
    assert updated is not None
    return updated
