"""W1-K -- Derivative-document (DOCX) provenance tracking.

Mirrors :mod:`meridian.db.research_runs`'s exact pattern (read that file
first): a self-contained module, its own local ``_row_to_dict``, ``uuid``
used directly for id generation (no import from ``meridian.db.__init__``,
avoiding a package-init import cycle).

Three operations, one table (``docx_derivatives``):

* :func:`register_docx_derivative` -- record that ``derivative_path`` was
  generated from ``source_path`` at a known content state (a caller-supplied
  ``source_content_hash``), plus optional generating-tool/timestamp
  metadata. Always creates a NEW row with ``status='candidate'`` -- distinct
  registrations of the same source/derivative pair over time are all
  legitimate (re-renders, retries, competing candidates) and are never
  merged or overwritten.
* :func:`verify_docx_diff` -- given a registered derivative and the CALLER's
  freshly-computed current hash(es) of the on-disk documents, report whether
  the derivative is stale relative to its source (the source's current
  content hash no longer matches the hash recorded at registration time).
  Persists the verdict onto the row (``last_verified_at`` /
  ``last_verify_is_stale`` / ``last_verify_reason``) as a lightweight audit
  trail, but never changes ``status`` itself -- staleness is advisory
  information a caller (or :func:`promote_docx_candidate`) can act on, not an
  automatic state transition.
* :func:`promote_docx_candidate` -- explicitly promote a ``'candidate'``
  derivative to be THE accepted derivative for its ``source_path``,
  demoting whatever derivative previously held that role (if any) to
  ``'superseded'`` in the same transaction. Idempotent on an already-accepted
  row (a repeat call returns the existing state unchanged, never an error or
  a duplicate transition) -- mirrors
  :func:`meridian.db.research_runs.promote_research_run`'s idempotency-guard
  precedent; the candidate/accepted/superseded state machine plus the
  unconditional supersede-on-promote step mirrors
  :func:`meridian.db.experiments.promote_experiment_run`'s
  real-state-transition-with-audit-trail precedent.

See :mod:`meridian.docx_derivative` for field validation, and IMPORTANT, its
module docstring's note on why source/derivative paths are treated as
opaque, non-project-relative strings and why content hashes are never
computed server-side.
"""
from __future__ import annotations

import uuid
from typing import Any

from meridian import docx_derivative as model

_DERIVATIVE_COLUMNS = (
    "id", "project_id", "creator_session_id", "source_path", "derivative_path",
    "source_content_hash", "derivative_content_hash", "generating_tool",
    "generated_at", "status", "notes",
    "last_verified_at", "last_verify_is_stale", "last_verify_reason",
    "promoted_at", "promoted_by_session_id",
    "superseded_at", "superseded_by_derivative_id",
    "created_at", "updated_at",
)


def _row_to_dict(row: Any) -> "dict[str, Any] | None":
    if row is None:
        return None
    if hasattr(row, "keys"):
        result = {key: row[key] for key in row.keys()}
    else:
        result = dict(zip(_DERIVATIVE_COLUMNS, row))
    raw_stale = result.get("last_verify_is_stale")
    if raw_stale is not None:
        result["last_verify_is_stale"] = bool(raw_stale)
    return result


async def _require_session(db: Any, project_id: str, session_id: str) -> None:
    if not session_id:
        raise ValueError("session_id is required for docx-derivative writes")
    async with db.execute(
        "SELECT id FROM sessions WHERE id = ? AND project_id = ?",
        (session_id, project_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise ValueError(f"session {session_id!r} does not belong to project {project_id!r}")


async def _find(db: Any, project_id: str, derivative_id: str) -> "dict[str, Any] | None":
    async with db.execute(
        f"SELECT {', '.join(_DERIVATIVE_COLUMNS)} FROM docx_derivatives "
        "WHERE project_id = ? AND id = ?",
        (project_id, derivative_id),
    ) as cur:
        return _row_to_dict(await cur.fetchone())


async def _find_accepted_for_source(
    db: Any, project_id: str, source_path: str, *, exclude_id: "str | None" = None,
) -> "dict[str, Any] | None":
    async with db.execute(
        f"SELECT {', '.join(_DERIVATIVE_COLUMNS)} FROM docx_derivatives "
        "WHERE project_id = ? AND source_path = ? AND status = 'accepted'",
        (project_id, source_path),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        decoded = _row_to_dict(row)
        if decoded is not None and decoded["id"] != exclude_id:
            return decoded
    return None


async def get_docx_derivative(
    db: Any, project_id: str, *, derivative_id: str
) -> "dict[str, Any] | None":
    """Read one project-scoped docx derivative by id. ``None`` when not found."""
    return await _find(db, project_id, derivative_id)


async def list_docx_derivatives(
    db: Any,
    project_id: str,
    *,
    source_path: "str | None" = None,
    status: "str | None" = None,
    limit: int = 100,
) -> "list[dict[str, Any]]":
    """List a project's docx derivatives, newest-registered first. Optionally
    scoped to one ``source_path`` and/or one ``status``."""
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if source_path:
        clauses.append("source_path = ?")
        params.append(source_path)
    if status is not None:
        clauses.append("status = ?")
        params.append(model.validate_status(status))
    limit = max(1, min(int(limit), 500))
    params.append(limit)
    async with db.execute(
        f"SELECT {', '.join(_DERIVATIVE_COLUMNS)} FROM docx_derivatives "
        f"WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, id DESC LIMIT ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [d for row in rows if (d := _row_to_dict(row)) is not None]


async def register_docx_derivative(
    db: Any,
    project_id: str,
    session_id: str,
    *,
    source_path: str,
    derivative_path: str,
    source_content_hash: str,
    derivative_content_hash: "str | None" = None,
    generating_tool: "str | None" = None,
    generated_at: "str | None" = None,
    notes: "str | None" = None,
) -> dict[str, Any]:
    """Register a new docx derivative (see module docstring). Always a fresh
    ``status='candidate'`` row -- never updates an existing registration."""
    await _require_session(db, project_id, session_id)
    fields = model.validate_register_fields(
        source_path=source_path,
        derivative_path=derivative_path,
        source_content_hash=source_content_hash,
        derivative_content_hash=derivative_content_hash,
        generating_tool=generating_tool,
        generated_at=generated_at,
        notes=notes,
    )

    derivative_id = str(uuid.uuid4())
    now = model.utcnow_iso()
    await db.execute(
        "INSERT INTO docx_derivatives "
        "(id, project_id, creator_session_id, source_path, derivative_path, "
        "source_content_hash, derivative_content_hash, generating_tool, "
        "generated_at, status, notes, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?)",
        (
            derivative_id, project_id, session_id,
            fields["source_path"], fields["derivative_path"],
            fields["source_content_hash"], fields["derivative_content_hash"],
            fields["generating_tool"], fields["generated_at"], fields["notes"],
            now, now,
        ),
    )
    await db.commit()
    created = await _find(db, project_id, derivative_id)
    assert created is not None  # just written
    return created


async def verify_docx_diff(
    db: Any,
    project_id: str,
    *,
    derivative_id: str,
    current_source_content_hash: str,
    current_derivative_content_hash: "str | None" = None,
) -> dict[str, Any]:
    """Compare a registered derivative's recorded hashes against the
    CALLER-supplied current on-disk hash(es) and report staleness.

    Raises ``ValueError`` when the derivative doesn't exist in this project.
    Never raises on a "stale" verdict -- staleness is a normal, expected
    result, not an error condition. Persists the verdict onto the row as a
    lightweight audit trail (see module docstring); never changes ``status``.
    """
    derivative = await _find(db, project_id, derivative_id)
    if derivative is None:
        raise ValueError(f"docx derivative {derivative_id!r} not found in project {project_id!r}")

    validated_source_hash = model.validate_content_hash(
        current_source_content_hash, field="current_source_content_hash", required=True,
    )
    validated_derivative_hash = model.validate_content_hash(
        current_derivative_content_hash, field="current_derivative_content_hash", required=False,
    )

    source_changed = validated_source_hash != derivative["source_content_hash"]
    derivative_changed: "bool | None" = None
    if validated_derivative_hash is not None and derivative["derivative_content_hash"] is not None:
        derivative_changed = validated_derivative_hash != derivative["derivative_content_hash"]

    is_stale = source_changed
    if is_stale:
        reason = (
            "source document has changed since this derivative was generated "
            "(current source_content_hash does not match the hash recorded at "
            "registration time)"
        )
    elif derivative_changed:
        reason = (
            "source is unchanged, but the derivative's own current content hash "
            "no longer matches the hash recorded at registration time -- the "
            "derivative may have been edited or regenerated out of band"
        )
    else:
        reason = "source content hash matches the hash recorded at registration time"

    now = model.utcnow_iso()
    await db.execute(
        "UPDATE docx_derivatives SET last_verified_at = ?, last_verify_is_stale = ?, "
        "last_verify_reason = ?, updated_at = ? WHERE project_id = ? AND id = ?",
        (now, 1 if is_stale else 0, reason, now, project_id, derivative_id),
    )
    await db.commit()
    updated = await _find(db, project_id, derivative_id)
    assert updated is not None
    return {
        "derivative_id": derivative_id,
        "is_stale": is_stale,
        "source_changed": source_changed,
        "derivative_changed": derivative_changed,
        "reason": reason,
        "derivative": updated,
    }


async def promote_docx_candidate(
    db: Any,
    project_id: str,
    session_id: "str | None",
    *,
    derivative_id: str,
) -> dict[str, Any]:
    """Explicitly promote a ``'candidate'`` derivative to ``'accepted'`` for
    its ``source_path``, superseding whatever derivative previously held
    that role (see module docstring for the full state-machine rationale).

    Raises ``ValueError`` when the derivative doesn't exist in this project,
    or when its status is ``'superseded'`` (a superseded derivative can never
    be resurrected -- register a fresh candidate instead).

    Idempotent on an already-``'accepted'`` row (W1-K, mirrors
    ``promote_research_run``'s exact idempotency-guard pattern): a second
    ``promote_docx_candidate`` call on the SAME derivative_id returns the
    EXISTING accepted state unchanged (``idempotent_retry=True``) rather than
    erroring or re-running the supersede step a second time.
    """
    derivative = await _find(db, project_id, derivative_id)
    if derivative is None:
        raise ValueError(f"docx derivative {derivative_id!r} not found in project {project_id!r}")

    if derivative["status"] == "accepted":
        return {
            "derivative_id": derivative_id,
            "derivative": derivative,
            "superseded_derivative_id": None,
            "idempotent_retry": True,
        }
    if derivative["status"] != model.PROMOTABLE_STATUS:
        raise ValueError(
            f"docx derivative {derivative_id!r} has status {derivative['status']!r}; "
            f"promote_docx_candidate requires status={model.PROMOTABLE_STATUS!r} "
            "-- a superseded derivative cannot be re-promoted, register a fresh "
            "candidate instead"
        )
    if session_id:
        await _require_session(db, project_id, session_id)

    now = model.utcnow_iso()
    previously_accepted = await _find_accepted_for_source(
        db, project_id, derivative["source_path"], exclude_id=derivative_id,
    )
    superseded_id = None
    if previously_accepted is not None:
        superseded_id = previously_accepted["id"]
        await db.execute(
            "UPDATE docx_derivatives SET status = 'superseded', superseded_at = ?, "
            "superseded_by_derivative_id = ?, updated_at = ? WHERE project_id = ? AND id = ?",
            (now, derivative_id, now, project_id, superseded_id),
        )

    await db.execute(
        "UPDATE docx_derivatives SET status = 'accepted', promoted_at = ?, "
        "promoted_by_session_id = ?, updated_at = ? WHERE project_id = ? AND id = ?",
        (now, session_id, now, project_id, derivative_id),
    )
    await db.commit()
    updated = await _find(db, project_id, derivative_id)
    assert updated is not None
    return {
        "derivative_id": derivative_id,
        "derivative": updated,
        "superseded_derivative_id": superseded_id,
        "idempotent_retry": False,
    }
