"""d2539453 — lint_finding: structured, version-pinned paper/manuscript audit
output.

Meridian's manuscript/paper-editorial tooling (extensions/meridian-docs —
see ``docs_intel.audit_document`` / ``audit_equation_style`` /
``get_document_review``) already computes structured "finding" dicts
(``{type, category, severity, detail, ...}``) on every audit call, but those
results are ephemeral: read-only, returned to the caller, and gone once the
process forgets them. This module is the durable, dual-backend (SQLite +
Postgres, mirrored in ``pg_adapter._migrate_pg_lint_finding``) persistence
layer for that same finding shape, so a paper audit's output can be stored,
triaged by a human across sessions, and revisited later — following the
append-only-row / typed-status / idempotent-migration conventions already
established by ``meridian.db.decisions_pinned`` (``pin_decision`` et al., in
``meridian/db/__init__.py``) and ``meridian.db.experiment_model``.

SCHEMA
------

``lint_findings`` — one row per individual audit finding.

TWO-AXIS VERSION PINNING (the item's headline requirement)
-----------------------------------------------------------

A lint finding is only meaningful relative to (a) the exact document content
it was computed against, and (b) the exact rule logic that computed it — both
can change out from under a stored finding, independently:

* ``source_fingerprint`` (NOT NULL) — a content hash of the audited document
  at the moment this finding was generated, in the SAME sha256-hex-of-exact-
  bytes form as ``docs_intel._source_fingerprint`` / ``doc_store``'s
  ``compute_content_hash`` / ``expected_content_hash`` gate. This is the
  "which VERSION OF THE DOCUMENT" pin. It is deliberately NOT re-validated or
  refreshed by any write in this module — see :func:`check_finding_freshness`
  for the read-time, fail-open comparison a caller runs against a freshly
  computed current fingerprint (mirrors ``doc_store._docx_staleness_check``'s
  "advisory only, never raises, never blocks" contract exactly).
* ``linter_version`` (NOT NULL) — a free-text version string (semver or a
  content/rule-set hash) identifying the exact rule logic that produced this
  finding. This is the "which VERSION OF THE RULES" pin: a linter's checks
  can be tightened, loosened, or fixed over time, and a finding recorded
  under an old ruleset should be distinguishable from one re-verified under
  a current one, without conflating that with the document itself changing.

Both pins are opaque strings from this module's point of view — computing or
comparing them is the caller's/linter's responsibility (exactly how
``source_revision``/``params_fingerprint`` are opaque to ``research_runs``).

STATUS — human triage, not automated enforcement
--------------------------------------------------

``status`` is a small, closed human-triage lifecycle (``open`` ->
``acknowledged``/``resolved``/``dismissed``), matching
``decisions_pinned.status``'s (``active``/``superseded``) and
``hitl_requests.status``'s (``pending``/``answered``/``dismissed``) shape.
This is deliberately NOT a blocking gate on anything else in Meridian (no
completion check reads this table) — it exists so a human reviewing paper-
audit output has somewhere durable to record "I looked at this and it's
fine" / "I looked at this and fixed it" / "not applicable here", across
sessions. A real enforcement gate (e.g. "sprint item completion blocked on
open error-severity findings") is a follow-up, not part of this schema-only
item.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-------------------------------------------

No MCP tool surface, no HTTP route, no dashboard UI — those are a separate,
larger feature built on top of this schema. No automatic re-linting, no
staleness-driven status transitions (a finding never silently flips to some
"stale" status on its own — see :func:`check_finding_freshness`'s doc_store-
style "compute live, never cache" rationale above). No cross-store DB-level
FOREIGN KEY to ``doc_store``'s ``doc_documents`` table: ``doc_store`` owns
its own schema/connection lifecycle independently of ``db.CREATE_TABLES`` /
this module's migrations (see ``doc_store.py``'s own "Schema (owned by this
store...)" header) — ``document_id`` is a plain nullable TEXT, validated at
the app layer by whatever future caller wires the two together, the same
convention already used for ``workspace_proposals.project_id`` (see
``db.migrations._migrate_proposal_project_scope``'s docstring).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from meridian.db import _new_id, _row_to_dict

# Closed vocabularies — kept here (not a separate meridian.lint_finding pure-
# vocab module) because, unlike experiment_model's attempt-status transition
# table, there is no transition logic to validate: status moves freely
# between any of these four values (a human can always re-open, re-dismiss,
# etc.), so a flat membership check is the whole contract.
LINT_FINDING_SEVERITIES: frozenset[str] = frozenset({"error", "warning", "info"})
LINT_FINDING_STATUSES: frozenset[str] = frozenset(
    {"open", "acknowledged", "resolved", "dismissed"}
)
_TERMINAL_STATUSES: frozenset[str] = frozenset({"resolved", "dismissed"})


def _now_iso() -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS' — matches research_graph/experiment_model's
    cross-dialect-safe timestamp convention (computed in Python, not a SQL
    now()/datetime('now') call)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _json_dumps(value: "Any | None") -> "str | None":
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if value is not None else None


def _json_loads(raw: "str | None") -> "Any | None":
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def validate_lint_finding_severity(severity: str) -> str:
    """Normalize + validate a severity value. Raises ``ValueError`` naming
    the closed set on an unrecognized value — never silently coerces."""
    normalized = (severity or "").strip().lower()
    if normalized not in LINT_FINDING_SEVERITIES:
        raise ValueError(
            f"severity must be one of {sorted(LINT_FINDING_SEVERITIES)}, got {severity!r}"
        )
    return normalized


def validate_lint_finding_status(status: str) -> str:
    """Normalize + validate a status value. Raises ``ValueError`` naming the
    closed set on an unrecognized value — never silently coerces."""
    normalized = (status or "").strip().lower()
    if normalized not in LINT_FINDING_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(LINT_FINDING_STATUSES)}, got {status!r}"
        )
    return normalized


# ---------------------------------------------------------------------------
# Migration — guarded, idempotent (2026-07-04 outage rule: no unguarded
# CREATE INDEX on a migration-added column/table in CREATE_TABLES/
# CREATE_TABLES_CORE). Mirrored on Postgres by
# pg_adapter._migrate_pg_lint_finding.
# ---------------------------------------------------------------------------


async def _migrate_lint_finding(db: aiosqlite.Connection) -> None:
    """d2539453 — create ``lint_findings`` if absent."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS lint_findings (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            document_id TEXT,
            audit_run_id TEXT,
            linter_name TEXT NOT NULL,
            linter_version TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            category TEXT NOT NULL,
            finding_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'warning'
                CHECK (severity IN ('error', 'warning', 'info')),
            message TEXT NOT NULL,
            location TEXT,
            detail TEXT,
            status TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'acknowledged', 'resolved', 'dismissed')),
            resolved_by TEXT,
            resolution_note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at TEXT
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_lint_findings_project "
        "ON lint_findings(project_id, status)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_lint_findings_document "
        "ON lint_findings(document_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_lint_findings_audit_run "
        "ON lint_findings(audit_run_id)"
    )
    await db.commit()


def _row_to_lint_finding(row: Any) -> "dict[str, Any] | None":
    d = _row_to_dict(row)
    if d is None:
        return None
    d["location"] = _json_loads(d.get("location"))
    d["detail"] = _json_loads(d.get("detail"))
    return d


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def create_lint_finding(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    linter_name: str,
    linter_version: str,
    source_fingerprint: str,
    category: str,
    finding_type: str,
    message: str,
    severity: str = "warning",
    document_id: "str | None" = None,
    audit_run_id: "str | None" = None,
    location: "dict[str, Any] | None" = None,
    detail: "dict[str, Any] | None" = None,
) -> dict[str, Any]:
    """Record one structured, version-pinned paper-audit finding.

    ``project_id`` has a DB-level FOREIGN KEY (``ON DELETE CASCADE``) — an
    unknown project raises an integrity error from the database itself,
    matching ``decisions_pinned``'s convention of leaning on the FK rather
    than a duplicate existence check (unlike ``experiment_model.create_run``,
    which validates its *cross-table* ``experiment_id`` explicitly because
    that reference has no DB-level FK to lean on).

    Every other required field is a plain string checked for non-empty
    content here (a DB ``NOT NULL`` only rejects ``NULL``, not ``''``) —
    ``linter_name``, ``linter_version``, ``source_fingerprint``, ``category``,
    ``finding_type``, ``message``. ``severity`` is validated against
    :data:`LINT_FINDING_SEVERITIES` (default ``'warning'``).
    """
    project_id = (project_id or "").strip()
    if not project_id:
        raise ValueError("create_lint_finding requires a non-empty project_id")

    required = {
        "linter_name": linter_name,
        "linter_version": linter_version,
        "source_fingerprint": source_fingerprint,
        "category": category,
        "finding_type": finding_type,
        "message": message,
    }
    cleaned: dict[str, str] = {}
    for field_name, value in required.items():
        stripped = (value or "").strip()
        if not stripped:
            raise ValueError(f"create_lint_finding requires a non-empty {field_name}")
        cleaned[field_name] = stripped

    severity = validate_lint_finding_severity(severity)

    from meridian.secret_redaction import check_for_secrets  # noqa: PLC0415
    check_for_secrets(cleaned["message"], context="lint finding message")

    document_id = (document_id or "").strip() or None
    audit_run_id = (audit_run_id or "").strip() or None

    fid = _new_id()
    await db.execute(
        "INSERT INTO lint_findings "
        "(id, project_id, document_id, audit_run_id, linter_name, linter_version, "
        "source_fingerprint, category, finding_type, severity, message, location, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fid, project_id, document_id, audit_run_id,
            cleaned["linter_name"], cleaned["linter_version"], cleaned["source_fingerprint"],
            cleaned["category"], cleaned["finding_type"], severity, cleaned["message"],
            _json_dumps(location), _json_dumps(detail),
        ),
    )
    await db.commit()
    created = await get_lint_finding(db, project_id, fid)
    assert created is not None  # just written
    return created


async def get_lint_finding(
    db: aiosqlite.Connection, project_id: str, finding_id: str
) -> "dict[str, Any] | None":
    """Fetch one finding by id, scoped to ``project_id`` — a cross-project
    lookup (right id, wrong project) returns ``None``, exactly like a
    nonexistent id, never leaking the row's existence to the wrong tenant."""
    async with db.execute(
        "SELECT * FROM lint_findings WHERE id = ? AND project_id = ?",
        (finding_id, project_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_lint_finding(row)


async def list_lint_findings(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    document_id: "str | None" = None,
    audit_run_id: "str | None" = None,
    status: "str | None" = None,
    severity: "str | None" = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """List findings scoped to ``project_id``, newest first, with optional
    equality filters on ``document_id`` / ``audit_run_id`` / ``status`` /
    ``severity``. ``limit`` caps the result (default 200) — this is a bare
    listing helper, not a paginated API; callers needing real pagination
    should add it when the MCP/HTTP surface for this schema is built."""
    project_id = (project_id or "").strip()
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if document_id is not None:
        clauses.append("document_id = ?")
        params.append(document_id)
    if audit_run_id is not None:
        clauses.append("audit_run_id = ?")
        params.append(audit_run_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(validate_lint_finding_status(status))
    if severity is not None:
        clauses.append("severity = ?")
        params.append(validate_lint_finding_severity(severity))
    params.append(int(limit))
    async with db.execute(
        f"SELECT * FROM lint_findings WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at DESC LIMIT ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [f for f in (_row_to_lint_finding(r) for r in rows) if f is not None]


async def set_lint_finding_status(
    db: aiosqlite.Connection,
    project_id: str,
    finding_id: str,
    status: str,
    *,
    resolved_by: "str | None" = None,
    resolution_note: "str | None" = None,
) -> dict[str, Any]:
    """Move a finding's human-triage ``status`` (see :data:`LINT_FINDING_STATUSES`).

    Idempotent: setting a finding to its CURRENT status is always a no-op
    success (matching ``experiment_model.transition_attempt``'s identical
    idempotent-self-transition contract), never a ``ValueError`` — there is
    no illegal transition in this flat vocabulary, only an unrecognized one.
    ``resolved_at`` is stamped the first time a finding enters a terminal
    status (``resolved``/``dismissed``) and cleared when it moves back to
    ``open``/``acknowledged``.
    """
    project_id = (project_id or "").strip()
    current = await get_lint_finding(db, project_id, finding_id)
    if current is None:
        raise ValueError(f"lint finding {finding_id!r} not found in project {project_id!r}")

    validated_status = validate_lint_finding_status(status)

    if resolution_note is not None:
        from meridian.secret_redaction import check_for_secrets  # noqa: PLC0415
        check_for_secrets(resolution_note, context="lint finding resolution_note")

    now = _now_iso()
    set_clauses = ["status = ?", "updated_at = ?"]
    params: list[Any] = [validated_status, now]

    if resolved_by is not None:
        set_clauses.append("resolved_by = ?")
        params.append(resolved_by)
    if resolution_note is not None:
        set_clauses.append("resolution_note = ?")
        params.append(resolution_note)

    if validated_status in _TERMINAL_STATUSES and current.get("resolved_at") is None:
        set_clauses.append("resolved_at = ?")
        params.append(now)
    elif validated_status not in _TERMINAL_STATUSES and current.get("resolved_at") is not None:
        set_clauses.append("resolved_at = ?")
        params.append(None)

    params.extend([finding_id, project_id])
    await db.execute(
        f"UPDATE lint_findings SET {', '.join(set_clauses)} WHERE id = ? AND project_id = ?",
        params,
    )
    await db.commit()
    updated = await get_lint_finding(db, project_id, finding_id)
    assert updated is not None
    return updated


def check_finding_freshness(
    finding: "dict[str, Any]", current_source_fingerprint: "str | None"
) -> "dict[str, Any] | None":
    """Advisory, read-only comparison of a stored finding's pinned
    ``source_fingerprint`` against a freshly computed one for the same
    document. Mirrors ``doc_store._docx_staleness_check``'s contract
    exactly: never raises, never mutates the row, and FAILS OPEN (returns
    ``None``) whenever there isn't enough evidence to conclude staleness —
    including when the caller has no current fingerprint to compare (e.g.
    the document could not be re-read). Returns ``None`` when fresh/unknown,
    or a dict describing the mismatch when the pinned version has drifted.

    This is deliberately a pure function, not a DB write: staleness here is
    ALWAYS computed live against a fingerprint the caller supplies, never
    cached as a stored status value that could itself drift from the truth
    (the same "re-derive, don't replay stale text" principle documented on
    ``meridian.db.experiment_model.get_run``).
    """
    stored = finding.get("source_fingerprint")
    if not stored or not current_source_fingerprint:
        return None
    if stored == current_source_fingerprint:
        return None
    return {
        "stale": True,
        "reason": (
            "the audited document's content has changed since this finding was "
            "generated -- it may no longer apply, or may need to be re-verified "
            "against the current text."
        ),
        "pinned_source_fingerprint": stored,
        "current_source_fingerprint": current_source_fingerprint,
    }
