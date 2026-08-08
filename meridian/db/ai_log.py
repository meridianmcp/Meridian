"""9e83be4a (Round 1 proposal e143949d) — storage scaffold for the canonical
``ExecutionEvent`` contract defined in :mod:`meridian.ai_log`.

This module is a data layer for one table, ``ai_log_events`` — append-only
CONTENT by construction: no function here ever rewrites an existing row's
envelope/payload in place (mirrors ``db.action_audit_log`` /
``db.manual_issue_content_log``'s own "append-only by construction, not just
convention" discipline). It is deliberately NOT wired into anything: no
route, no MCP tool, no server-side hook calls :func:`append_event` today.
See :mod:`meridian.ai_log`'s module docstring ("SCOPE") for exactly what
sprint item 9e83be4a does and does not cover, and why (schema-first
investigation only, per Round 1 proposal e143949d) — the capture/ingestion
pipeline that WOULD call ``append_event`` from real request paths is
explicitly deferred to a future item, not this one.

CREATE TABLE/INDEX IF NOT EXISTS is already idempotent, so — mirroring
``db.decision_evidence`` / ``db.executor_reports`` / ``db.vector_index_state``
— there is no separate ``CREATE_TABLES`` base-literal entry: the guarded
migration below is called unconditionally from ``init_db``, so both a fresh
DB and an existing one pick up the table identically. No inline
``CREATE INDEX`` in an unguarded base schema literal (the 2026-06-13 /
2026-07-04 outage class documented in AGENTS.md) — every index lives inside
this guarded migration. Mirrored in
``pg_adapter._migrate_pg_ai_log_events``.

ea972129 (Round 1 proposal e143949d) — RETENTION + REDACTION
--------------------------------------------------------------
Sibling item ea972129 ("design local-first storage, retention, redaction,
and artifact persistence") adds two capabilities on top of the scaffold
above, without weakening "append-only CONTENT":

  * **Redaction (write-path gate).** :func:`append_event` now runs the
    payload through :func:`meridian.secret_redaction.check_for_secrets`
    before it is ever hashed or inserted — the same fail-closed DB
    write-path scrubbing already applied to ``sprint_items.notes`` /
    ``task_log.description`` / ``decisions_pinned.body`` /
    ``project_notes.body`` (see that module's docstring). ai_log payloads
    (arbitrary tool arguments, LLM request/response bodies) are the
    highest-risk surface in this table and were not covered by that list
    yet. A match raises ``ValueError`` — the caller must fix the leak at
    its source; this is a hard rejection, not a silent best-effort mask.
  * **Retention (bulk purge, not row mutation).** :func:`purge_events_before`
    deletes whole rows in bulk, scoped to one project, once they age past a
    caller-supplied cutoff. This does NOT contradict "append-only CONTENT"
    above: no function rewrites a *surviving* row's envelope/payload, ever.
    A scheduled/manual retention sweep removing whole old rows is a
    distinct, explicit, separately-audited operation from that guarantee —
    the same append-only-content / explicit-bulk-purge split
    ``db.locks.expire_file_locks`` already uses for a different table.

Large opaque blobs (a full tool result, an LLM body) referenced FROM a
payload rather than inlined into it are handled by the sibling module
:mod:`meridian.artifact_store` — see its own docstring for the local-first,
content-addressed design (and the Redis-read-acceleration design note).
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

    Raises ``ValueError`` (via :func:`meridian.secret_redaction.check_for_secrets`)
    if the payload contains a secret-shaped string — see this module's
    docstring's "ea972129 ... REDACTION" note. Also never inserts a row in
    that case.
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
    # ea972129 (Round 1 proposal e143949d) — redact-on-write gate: refuse to
    # durably store an event whose payload contains a secret-shaped string.
    # Scans the EXACT JSON string that will be written to the ``payload``
    # column, using the same canonical serialization as the INSERT below
    # (sort_keys=True, default=str) so what is scanned is what is stored.
    from meridian.secret_redaction import check_for_secrets  # noqa: PLC0415
    check_for_secrets(
        json.dumps(event.payload, sort_keys=True, default=str),
        context="ai_log event payload",
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


# ---------------------------------------------------------------------------
# ea972129 (Round 1 proposal e143949d) — retention
# ---------------------------------------------------------------------------

async def purge_events_before(
    db: aiosqlite.Connection,
    project_id: str,
    cutoff_recorded_at: str,
) -> int:
    """Delete every ``ai_log_events`` row for *project_id* whose
    ``recorded_at`` is strictly before *cutoff_recorded_at* (an ISO-8601 /
    ``datetime('now')``-shaped string — lexicographically comparable since
    the column is TEXT). Returns the number of rows deleted.

    This is the ONLY deletion path this module exposes — see the module
    docstring's "ea972129 ... RETENTION" note: no function here ever
    rewrites a surviving row's content, this one just removes whole rows in
    bulk once they age out. Always project-scoped (never a cross-project
    sweep in one call) — mirrors every other durable record in this
    codebase being project-scoped, and keeps one tenant's retention policy
    from ever touching another's data.
    """
    if not project_id:
        raise ValueError("project_id is required")
    if not cutoff_recorded_at:
        raise ValueError("cutoff_recorded_at is required")
    cursor = await db.execute(
        "DELETE FROM ai_log_events WHERE project_id = ? AND recorded_at < ?",
        (project_id, cutoff_recorded_at),
    )
    await db.commit()
    return max(cursor.rowcount or 0, 0)


# ---------------------------------------------------------------------------
# ea972129 (Round 1 proposal e143949d) — AiLogStore facade
# ---------------------------------------------------------------------------

class AiLogStore:
    """Project-scoped convenience facade over this module's free functions.

    Constructed with an already-open connection (aiosqlite or the pg
    adapter, exactly like :func:`append_event` et al. accept) and one
    ``project_id`` pinned for the lifetime of the instance — mirrors
    ``meridian.doc_store.DocStructureStore``'s "own the connection, expose
    project/document-scoped methods" convention.

    Adds NO new storage behavior beyond what
    :func:`append_event`/:func:`get_event`/:func:`list_events`/
    :func:`purge_events_before` already provide — this class exists purely
    so a caller doing several operations against ONE project (e.g. a
    retention sweep, a future export job) does not have to repeat
    ``project_id`` on every call. The module-level free functions remain
    the primary API and are unaffected by this class's existence.
    """

    def __init__(self, db: aiosqlite.Connection, project_id: str) -> None:
        if not project_id:
            raise ValueError("project_id is required")
        self._db = db
        self.project_id = project_id

    async def append(
        self, event_type: str, actor_kind: str, **kwargs: Any
    ) -> dict[str, Any]:
        """See :func:`append_event` (``project_id`` is already bound)."""
        return await append_event(self._db, self.project_id, event_type, actor_kind, **kwargs)

    async def get(self, event_id: str) -> "dict[str, Any] | None":
        """See :func:`get_event`."""
        return await get_event(self._db, event_id)

    async def list(self, **kwargs: Any) -> list[dict[str, Any]]:
        """See :func:`list_events` (``project_id`` is already bound)."""
        return await list_events(self._db, self.project_id, **kwargs)

    async def purge_older_than(self, cutoff_recorded_at: str) -> int:
        """See :func:`purge_events_before` (``project_id`` is already bound)."""
        return await purge_events_before(self._db, self.project_id, cutoff_recorded_at)
