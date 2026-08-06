"""9154aa9a — durable executor_report / planner_checkpoint records.

Why this exists: an executor session finishing (or blocking on) a batch of
sprint items previously had no first-class, DURABLE, non-executable record
of what actually happened — which items completed/failed/were skipped,
which commits/resources changed, whether tests passed, which tools were
available/degraded, what artifacts were produced, what blocked the run, and
what a planner should do next. ``generate_handoff`` renders an EXECUTABLE
continuation contract (a fresh ``/goal``); ``_build_continue_payload``
renders a compact executor resume block. Neither is an authoritative
completion REPORT a planner can read conversationally, and neither carries
correction/supersession lineage the way ``handoff_corrections`` does for
handoff bodies (see ``meridian.handoff.record_handoff_correction``).

This module is the pure-CRUD data layer for that report: one row per
submitted report, optionally chained to a prior report it corrects via
``parent_report_id`` (never rewriting the parent's row — the parent is
marked ``status='superseded'`` and kept verbatim for audit, mirroring
``handoff_corrections``' own auto-supersede-siblings discipline). Business
validation (cross-project/version checks, unresolved sprint-item pointers,
"is there any evidence at all") and the planner-promotion step that turns an
ACCEPTED report into a fresh executable handoff both live in
``meridian.handoff`` (``record_executor_report`` / ``accept_executor_report``)
— this module never calls ``generate_handoff`` and never touches
``sprint_items`` itself, so recording or correcting a report can NEVER
silently create or mutate executable sprint scope; only the explicit
planner-promotion step in ``meridian.handoff`` does that, and even then only
by rendering a handoff, never by writing sprint items directly.

Fields:
  id                        — UUID primary key
  project_id                — owning project
  version                   — sprint-version bucket this report is scoped to
  session_id                — the executor session that submitted this report
  source_handoff_id         — the ``handoffs.id`` this report is reporting
                               against/resuming from, if any
  board_revision_hash       — ``board_snapshot.build_board_snapshot``'s
                               revision hash captured AT REPORT TIME (see
                               ``meridian.handoff.record_executor_report``)
  item_outcomes              — JSON list of {item_id, status, summary, ...}
  changed_resources          — JSON list of file/symbol resource strings
  commits                    — JSON list of commit sha/message records
  tests                      — JSON object ({cmd, exit_code, passed, failed,
                               summary}) or null
  tool_availability           — JSON list of {tool, status, fallback_used}
  artifact_evidence           — JSON (arbitrary shape) or null
  blockers                    — JSON list of {item_id, reason, classification}
  unresolved_questions         — JSON list of strings
  recommended_next_actions      — JSON list of strings
  status                      — submitted | accepted | superseded
  parent_report_id            — the report THIS report corrects, if any
                                (self-referencing; NULL for a first-class
                                report) — correction lineage without rewriting
                                history, exactly like
                                ``handoff_corrections.source_handoff_id``.
  correction_reason            — free-text reason this report supersedes
                                its parent, when ``parent_report_id`` is set
  report_hash                  — deterministic sha256 over the report's
                                content fields (see :func:`canonical_report_hash`)
                                — two independently-submitted reports with
                                byte-identical evidence hash identically
  accepted_handoff_id           — the fresh ``handoffs.id`` produced by the
                                planner-promotion step, once accepted
  accepted_at / accepted_by      — when/who promoted this report
  idempotency_key                — optional caller-supplied dedup key; a
                                UNIQUE partial index makes a retried
                                ``create_executor_report`` call with the same
                                key return the existing row instead of a
                                duplicate insert
  created_at / updated_at

CREATE_TABLES covers fresh DBs; ``_migrate_executor_reports_table`` is the
upgrade path for existing ones. Every index lives INSIDE this guarded
migration (CREATE INDEX IF NOT EXISTS), never inline in a CREATE_TABLES
literal — an unguarded inline index on a migration-added table crashes
startup on a DB predating it (the 2026-07-04 outage trap documented in
AGENTS.md). Idempotent. Mirrored in
``pg_adapter._migrate_pg_executor_reports``.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import aiosqlite

from meridian.db import _new_id, _row_to_dict  # noqa: PLC0415

#: Every valid status an executor_report row may carry.
EXECUTOR_REPORT_STATUSES: frozenset[str] = frozenset({
    "submitted",   # recorded, awaiting planner review — the default
    "accepted",    # a planner explicitly promoted this report to a fresh handoff
    "superseded",  # a later correction (parent_report_id) replaced this report
})

_JSON_LIST_FIELDS: tuple[str, ...] = (
    "item_outcomes",
    "changed_resources",
    "commits",
    "tool_availability",
    "blockers",
    "unresolved_questions",
    "recommended_next_actions",
)
_JSON_OPTIONAL_FIELDS: tuple[str, ...] = ("tests", "artifact_evidence")


def _dump_json_list(value: Any) -> str:
    """Canonical JSON for a list-shaped report field. ``None``/non-list input
    degrades to ``'[]'`` rather than raising — these columns are NOT NULL
    DEFAULT '[]'."""
    return json.dumps(value if isinstance(value, list) else [], sort_keys=True, default=str)


def _dump_json_or_none(value: Any) -> "str | None":
    """Canonical JSON for a nullable report field, or None to store SQL NULL."""
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, default=str)


def _load_json_or_default(raw: Any, default: Any) -> Any:
    """Decode a stored report JSON column; already-decoded (dict/list, e.g. a
    Postgres JSONB-mapped driver) passes through unchanged. Malformed or
    missing input degrades to ``default`` rather than raising — this is a
    read path, never a validation gate."""
    if raw is None:
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def canonical_report_hash(payload: dict[str, Any]) -> str:
    """Deterministic ``sha256:<hex>`` over a report's CONTENT fields only —
    excludes ``id``/``status``/timestamps/lineage bookkeeping (the same
    "content, not identity/bookkeeping" discipline
    ``board_snapshot._compute_revision_hash`` uses for sprint items) — so two
    independently-submitted reports carrying byte-identical evidence hash
    identically, and the hash is stable across a status transition
    (submitted -> accepted) that changes no content field.
    """
    tracked = {
        k: payload.get(k)
        for k in (
            "project_id", "version", "session_id", "source_handoff_id",
            "board_revision_hash", *_JSON_LIST_FIELDS, *_JSON_OPTIONAL_FIELDS,
        )
    }
    canonical = json.dumps(tracked, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _deserialize_report_row(row: "dict[str, Any] | None") -> "dict[str, Any] | None":
    """Decode every JSON-shaped column on a raw ``executor_reports`` row."""
    if row is None:
        return None
    out = dict(row)
    for field in _JSON_LIST_FIELDS:
        out[field] = _load_json_or_default(out.get(field), [])
    for field in _JSON_OPTIONAL_FIELDS:
        out[field] = _load_json_or_default(out.get(field), None)
    return out


async def _migrate_executor_reports_table(db: aiosqlite.Connection) -> None:
    """9154aa9a — create ``executor_reports`` on existing SQLite DBs.

    Guarded migration (no inline CREATE INDEX in the unguarded base schema
    literals — 2026-07-04 outage rule): the table + its indexes are created
    here so existing DBs pick them up on the first startup after deploy.
    Idempotent. Mirrors ``pg_adapter._migrate_pg_executor_reports``.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS executor_reports (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            version TEXT,
            session_id TEXT,
            source_handoff_id TEXT,
            board_revision_hash TEXT,
            item_outcomes TEXT NOT NULL DEFAULT '[]',
            changed_resources TEXT NOT NULL DEFAULT '[]',
            commits TEXT NOT NULL DEFAULT '[]',
            tests TEXT,
            tool_availability TEXT NOT NULL DEFAULT '[]',
            artifact_evidence TEXT,
            blockers TEXT NOT NULL DEFAULT '[]',
            unresolved_questions TEXT NOT NULL DEFAULT '[]',
            recommended_next_actions TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'submitted'
                CHECK (status IN ('submitted','accepted','superseded')),
            parent_report_id TEXT,
            correction_reason TEXT,
            report_hash TEXT,
            accepted_handoff_id TEXT,
            accepted_at TEXT,
            accepted_by TEXT,
            idempotency_key TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_executor_reports_project "
        "ON executor_reports(project_id, created_at DESC)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_executor_reports_project_version "
        "ON executor_reports(project_id, version)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_executor_reports_parent "
        "ON executor_reports(parent_report_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_executor_reports_source_handoff "
        "ON executor_reports(source_handoff_id)"
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_executor_reports_idempotency "
        "ON executor_reports(project_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )
    await db.commit()


async def create_executor_report(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    version: "str | None" = None,
    session_id: "str | None" = None,
    source_handoff_id: "str | None" = None,
    board_revision_hash: "str | None" = None,
    item_outcomes: "list[dict[str, Any]] | None" = None,
    changed_resources: "list[Any] | None" = None,
    commits: "list[Any] | None" = None,
    tests: "dict[str, Any] | None" = None,
    tool_availability: "list[dict[str, Any]] | None" = None,
    artifact_evidence: Any = None,
    blockers: "list[dict[str, Any]] | None" = None,
    unresolved_questions: "list[Any] | None" = None,
    recommended_next_actions: "list[Any] | None" = None,
    parent_report_id: "str | None" = None,
    correction_reason: "str | None" = None,
    idempotency_key: "str | None" = None,
) -> dict[str, Any]:
    """Insert one ``executor_reports`` row (pure data layer — no business
    validation; see ``meridian.handoff.record_executor_report`` for the
    fail-closed checks: project existence, cross-project/version parent
    linkage, unresolved sprint-item pointers).

    ``idempotency_key`` — when given and a PRIOR call already recorded a
    report with the same ``(project_id, idempotency_key)`` pair, that
    existing row is returned UNCHANGED — safe to retry after a network blip.

    ``parent_report_id`` — when given, the parent row is marked
    ``status='superseded'`` as part of THIS same call (never deleted or
    rewritten — its content stays available for audit), so a reader walking
    ``parent_report_id`` chains never has to disambiguate between two
    simultaneously "live" reports for the same lineage.
    """
    if idempotency_key:
        async with db.execute(
            "SELECT * FROM executor_reports WHERE project_id = ? AND idempotency_key = ?",
            (project_id, idempotency_key),
        ) as cur:
            existing = await cur.fetchone()
        if existing is not None:
            return _deserialize_report_row(_row_to_dict(existing))

    payload = {
        "project_id": project_id,
        "version": version,
        "session_id": session_id,
        "source_handoff_id": source_handoff_id,
        "board_revision_hash": board_revision_hash,
        "item_outcomes": item_outcomes or [],
        "changed_resources": changed_resources or [],
        "commits": commits or [],
        "tests": tests,
        "tool_availability": tool_availability or [],
        "artifact_evidence": artifact_evidence,
        "blockers": blockers or [],
        "unresolved_questions": unresolved_questions or [],
        "recommended_next_actions": recommended_next_actions or [],
    }
    report_hash = canonical_report_hash(payload)

    now_expr = "now()" if hasattr(db, "_pool") else "datetime('now')"
    if parent_report_id:
        await db.execute(
            f"UPDATE executor_reports SET status = 'superseded', updated_at = {now_expr} "
            "WHERE id = ? AND status != 'superseded'",
            (parent_report_id,),
        )

    rid = _new_id()
    await db.execute(
        "INSERT INTO executor_reports ("
        "id, project_id, version, session_id, source_handoff_id, "
        "board_revision_hash, item_outcomes, changed_resources, commits, "
        "tests, tool_availability, artifact_evidence, blockers, "
        "unresolved_questions, recommended_next_actions, status, "
        "parent_report_id, correction_reason, report_hash, idempotency_key"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitted', ?, ?, ?, ?)",
        (
            rid, project_id, version, session_id, source_handoff_id,
            board_revision_hash, _dump_json_list(item_outcomes),
            _dump_json_list(changed_resources), _dump_json_list(commits),
            _dump_json_or_none(tests), _dump_json_list(tool_availability),
            _dump_json_or_none(artifact_evidence), _dump_json_list(blockers),
            _dump_json_list(unresolved_questions),
            _dump_json_list(recommended_next_actions),
            parent_report_id, correction_reason, report_hash, idempotency_key,
        ),
    )
    await db.commit()
    created = await get_executor_report(db, rid)
    assert created is not None  # just inserted
    return created


async def get_executor_report(
    db: aiosqlite.Connection, report_id: str,
) -> "dict[str, Any] | None":
    """Fetch a single ``executor_reports`` row by id, JSON fields decoded."""
    async with db.execute(
        "SELECT * FROM executor_reports WHERE id = ?", (report_id,),
    ) as cur:
        row = await cur.fetchone()
    return _deserialize_report_row(_row_to_dict(row))


async def list_executor_reports(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    version: "str | None" = None,
    session_id: "str | None" = None,
    status: "str | None" = None,
    parent_report_id: "str | None" = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List reports for a project, newest first (JSON fields decoded).

    Every status is included by default (superseded too) — matching the
    AGENTS.md b763d2ba guidance to never silently narrow to a "live-only"
    view when a caller hasn't explicitly asked for one.
    """
    limit = max(1, min(int(limit or 20), 200))
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if version is not None:
        clauses.append("version = ?")
        params.append(version)
    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if parent_report_id is not None:
        clauses.append("parent_report_id = ?")
        params.append(parent_report_id)
    params.append(limit)
    sql = (
        f"SELECT * FROM executor_reports WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at DESC, id DESC LIMIT ?"
    )
    async with db.execute(sql, tuple(params)) as cur:
        rows = await cur.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = _deserialize_report_row(_row_to_dict(r))
        if d is not None:
            out.append(d)
    return out


async def update_executor_report_status(
    db: aiosqlite.Connection,
    report_id: str,
    status: str,
    *,
    reason: "str | None" = None,
) -> "dict[str, Any] | None":
    """Explicit status transition for a recorded report (draft escape hatch
    — mirrors ``handoff.update_handoff_correction_status``: any of the three
    statuses may be set directly, not a state machine with disallowed
    transitions). ``reason`` is stored as the report's ``correction_reason``
    for audit when transitioning to ``superseded`` outside the automatic
    ``parent_report_id`` supersede path. Returns the updated row, or
    ``None`` if ``report_id`` doesn't exist.
    """
    if status not in EXECUTOR_REPORT_STATUSES:
        raise ValueError(
            f"Invalid executor-report status {status!r}. "
            f"Valid: {sorted(EXECUTOR_REPORT_STATUSES)}"
        )
    now_expr = "now()" if hasattr(db, "_pool") else "datetime('now')"
    if reason is not None:
        await db.execute(
            f"UPDATE executor_reports SET status = ?, correction_reason = ?, "
            f"updated_at = {now_expr} WHERE id = ?",
            (status, reason, report_id),
        )
    else:
        await db.execute(
            f"UPDATE executor_reports SET status = ?, updated_at = {now_expr} WHERE id = ?",
            (status, report_id),
        )
    await db.commit()
    return await get_executor_report(db, report_id)


async def mark_executor_report_accepted(
    db: aiosqlite.Connection,
    report_id: str,
    *,
    accepted_handoff_id: "str | None",
    accepted_by: "str | None" = None,
) -> dict[str, Any]:
    """Stamp a report ``accepted`` with the fresh handoff id the
    planner-promotion step produced. This is the ONLY function that writes
    ``accepted_handoff_id``/``accepted_at`` — call it exactly once, right
    after ``generate_handoff`` returns (see
    ``meridian.handoff.accept_executor_report``). Raises ``ValueError``
    (never silently drops or overwrites evidence) when:

    * ``report_id`` does not name an existing report — nothing to accept.
    * the report was already accepted — a durable acceptance record is
      never silently overwritten by a second call.
    """
    report = await get_executor_report(db, report_id)
    if report is None:
        raise ValueError(
            f"executor report {report_id!r} not found — cannot accept a "
            "report that was never recorded."
        )
    if report.get("accepted_handoff_id"):
        raise ValueError(
            f"executor report {report_id!r} was already accepted (handoff "
            f"{report['accepted_handoff_id']!r} at {report.get('accepted_at')!r}) "
            "— refusing to silently overwrite a durable acceptance record."
        )
    now_expr = "now()" if hasattr(db, "_pool") else "datetime('now')"
    await db.execute(
        f"UPDATE executor_reports SET status = 'accepted', "
        f"accepted_handoff_id = ?, accepted_by = ?, accepted_at = {now_expr}, "
        f"updated_at = {now_expr} WHERE id = ?",
        (accepted_handoff_id, accepted_by, report_id),
    )
    await db.commit()
    updated = await get_executor_report(db, report_id)
    assert updated is not None
    return updated
