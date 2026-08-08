"""9e83be4a (Round 1 proposal e143949d) — storage scaffold for the canonical
``ExecutionEvent`` contract defined in :mod:`meridian.ai_log`.

This module is a pure-CRUD data layer for one table, ``ai_log_events`` —
append-only by construction (no update/delete helper is provided anywhere in
this module, mirroring ``db.action_audit_log`` / ``db.manual_issue_content_log``'s
own "append-only by construction, not just convention" discipline). It is
deliberately NOT wired into anything: no route, no MCP tool, no server-side
hook calls :func:`append_event` today. See :mod:`meridian.ai_log`'s module
docstring ("SCOPE") for exactly what this sprint item (9e83be4a) does and
does not cover, and why (schema-first investigation only, per Round 1
proposal e143949d) — the capture/ingestion pipeline that WOULD call
``append_event`` from real request paths is explicitly deferred to a future
item, not this one.

CREATE TABLE/INDEX IF NOT EXISTS is already idempotent, so — mirroring
``db.decision_evidence`` / ``db.executor_reports`` / ``db.vector_index_state``
— there is no separate ``CREATE_TABLES`` base-literal entry: the guarded
migration below is called unconditionally from ``init_db``, so both a fresh
DB and an existing one pick up the table identically. No inline
``CREATE INDEX`` in an unguarded base schema literal (the 2026-06-13 /
2026-07-04 outage class documented in AGENTS.md) — every index lives inside
this guarded migration. Mirrored in
``pg_adapter._migrate_pg_ai_log_events``.
"""
from __future__ import annotations

import json
from typing import Any

import aiosqlite

from meridian.ai_log import EVENT_SCHEMA_VERSION, ExecutionEvent
from meridian.db import _new_id, _row_to_dict


async def _migrate_ai_log_events_table(db: aiosqlite.Connection) -> None:
    """9e83be4a — create ``ai_log_events`` on both fresh and existing SQLite
    DBs (see module docstring: no separate CREATE_TABLES entry needed).

    Columns mirror :class:`meridian.ai_log.ExecutionEvent`'s envelope
    exactly (see that module's docstring for the field-by-field rationale),
    plus one storage-only column not part of the portable envelope:
    ``recorded_at`` — wall-clock UTC time this row was durably appended,
    DB-assigned via column DEFAULT (never computed in Python and passed in,
    so it is never subject to this repo's now()-vs-clock_timestamp()
    single-transaction staleness trap — see
    ``pg_adapter._TS``/AGENTS.md's Postgres timestamp note; the Postgres
    mirror of this table uses that same ``_TS`` expression for its default).

    Idempotent (CREATE TABLE/INDEX IF NOT EXISTS). Mirrored in
    ``pg_adapter._migrate_pg_ai_log_events``.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS ai_log_events (
            id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            project_id TEXT NOT NULL,
            session_id TEXT,
            tenant_id TEXT,
            actor_kind TEXT NOT NULL,
            actor_id TEXT,
            correlation_id TEXT,
            parent_event_id TEXT,
            source TEXT,
            payload TEXT NOT NULL DEFAULT '{}',
            payload_schema TEXT,
            occurred_at TEXT NOT NULL,
            idempotency_key TEXT,
            event_hash TEXT,
            recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_log_events_project "
        "ON ai_log_events(project_id, recorded_at DESC)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_log_events_session "
        "ON ai_log_events(session_id, recorded_at DESC)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_log_events_type "
        "ON ai_log_events(project_id, event_type, recorded_at DESC)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_log_events_correlation "
        "ON ai_log_events(correlation_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_log_events_parent "
        "ON ai_log_events(parent_event_id)"
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_log_events_idempotency "
        "ON ai_log_events(project_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )
    await db.commit()


def _deserialize_event_row(row: "dict[str, Any] | None") -> "dict[str, Any] | None":
    """Decode the JSON ``payload`` column on a raw ``ai_log_events`` row.
    Malformed/missing JSON degrades to ``{}`` rather than raising — this is
    a read path, never a validation gate (mirrors
    ``db.executor_reports._load_json_or_default``)."""
    if row is None:
        return None
    out = dict(row)
    raw_payload = out.get("payload")
    if isinstance(raw_payload, (dict, list)):
        pass  # already decoded (e.g. a JSONB-mapped driver)
    elif raw_payload is None:
        out["payload"] = {}
    else:
        try:
            out["payload"] = json.loads(raw_payload)
        except (TypeError, ValueError):
            out["payload"] = {}
    return out


async def append_event(
    db: aiosqlite.Connection,
    project_id: str,
    event_type: str,
    actor_kind: str,
    *,
    actor_id: "str | None" = None,
    session_id: "str | None" = None,
    tenant_id: "str | None" = None,
    correlation_id: "str | None" = None,
    parent_event_id: "str | None" = None,
    source: "str | None" = None,
    payload: "dict[str, Any] | None" = None,
    payload_schema: "str | None" = None,
    occurred_at: "str | None" = None,
    schema_version: int = EVENT_SCHEMA_VERSION,
    idempotency_key: "str | None" = None,
) -> dict[str, Any]:
    """Insert one ``ai_log_events`` row and return it. Pure data-layer
    primitive — no capture/ingestion wiring calls this (see module
    docstring). Validates the envelope via
    :mod:`meridian.ai_log`'s validators before ever touching the DB, so a
    malformed event never reaches storage.

    ``idempotency_key`` — when given and a PRIOR call already recorded an
    event with the same ``(project_id, idempotency_key)`` pair, that
    existing row is returned UNCHANGED — safe to retry after a network blip
    (mirrors ``db.executor_reports.create_executor_report``).

    ``occurred_at`` — when omitted, defaults to "now" (UTC ISO-8601) at
    construction time, exactly like
    :class:`meridian.ai_log.ExecutionEvent`'s own default.

    Raises :class:`meridian.ai_log.ExecutionEventError` for an invalid
    ``event_type``/``actor_kind``/``payload``/``schema_version`` — never
    inserts a row that would fail :mod:`meridian.ai_log`'s own contract.
    """
    if idempotency_key:
        async with db.execute(
            "SELECT * FROM ai_log_events WHERE project_id = ? AND idempotency_key = ?",
            (project_id, idempotency_key),
        ) as cur:
            existing = await cur.fetchone()
        if existing is not None:
            return _deserialize_event_row(_row_to_dict(existing))

    # Reuse meridian.ai_log's own ExecutionEvent construction so this
    # function can never insert a row its own contract module would reject
    # — the dataclass's __post_init__ raises ExecutionEventError for any
    # structural problem (bad event_type/actor_kind/payload/schema_version/
    # missing project_id) before a single DB statement runs.
    event = ExecutionEvent(
        project_id=project_id,
        event_type=event_type,
        actor_kind=actor_kind,
        actor_id=actor_id,
        session_id=session_id,
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        parent_event_id=parent_event_id,
        source=source,
        payload=payload or {},
        payload_schema=payload_schema,
        idempotency_key=idempotency_key,
        occurred_at=occurred_at,
        schema_version=schema_version,
    )
    event_hash = event.content_hash()

    rid = _new_id()
    await db.execute(
        "INSERT INTO ai_log_events ("
        "id, schema_version, event_type, project_id, session_id, tenant_id, "
        "actor_kind, actor_id, correlation_id, parent_event_id, source, "
        "payload, payload_schema, occurred_at, idempotency_key, event_hash"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rid, event.schema_version, event.event_type, event.project_id,
            event.session_id, event.tenant_id, event.actor_kind, event.actor_id,
            event.correlation_id, event.parent_event_id, event.source,
            json.dumps(event.payload, sort_keys=True, default=str),
            event.payload_schema, event.occurred_at, event.idempotency_key,
            event_hash,
        ),
    )
    await db.commit()
    created = await get_event(db, rid)
    assert created is not None  # just inserted
    return created


async def get_event(db: aiosqlite.Connection, event_id: str) -> "dict[str, Any] | None":
    """Fetch a single ``ai_log_events`` row by id, payload JSON decoded."""
    async with db.execute(
        "SELECT * FROM ai_log_events WHERE id = ?", (event_id,),
    ) as cur:
        row = await cur.fetchone()
    return _deserialize_event_row(_row_to_dict(row))


async def list_events(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    session_id: "str | None" = None,
    event_type: "str | None" = None,
    correlation_id: "str | None" = None,
    parent_event_id: "str | None" = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List events for a project, newest-recorded first (payload JSON
    decoded). Every event is included — this table has no status/lifecycle
    to filter on (append-only, no supersession, unlike
    ``executor_reports``/``decision_evidence``)."""
    limit = max(1, min(int(limit or 50), 500))
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    if event_type is not None:
        clauses.append("event_type = ?")
        params.append(event_type)
    if correlation_id is not None:
        clauses.append("correlation_id = ?")
        params.append(correlation_id)
    if parent_event_id is not None:
        clauses.append("parent_event_id = ?")
        params.append(parent_event_id)
    params.append(limit)
    sql = (
        f"SELECT * FROM ai_log_events WHERE {' AND '.join(clauses)} "
        "ORDER BY recorded_at DESC, id DESC LIMIT ?"
    )
    async with db.execute(sql, tuple(params)) as cur:
        rows = await cur.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = _deserialize_event_row(_row_to_dict(r))
        if d is not None:
            out.append(d)
    return out
