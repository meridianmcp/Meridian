"""e1475682 — durable metadata for the backend-neutral vector-index contract.

Why this exists: :mod:`meridian_codeindex.vector_index` (extracted package,
see e1475682) defines a backend-neutral vector-index contract — DuckDB VSS
first, an optional pgvector-on-Neon backend gated by measured benchmark
evidence, degrading to BM25/lexical when neither is available. That module
is deliberately host-agnostic (no Meridian import — see its own docstring)
and keeps everything in-process/in-memory. This module is the Meridian-side
persistence layer: one row per ``(project_id, scope)`` recording

* which backend is *active* (``duckdb_vss`` / ``pgvector`` / ``bm25``),
* the embedding model/version and vector dimension that produced it,
* a ``source_fingerprint`` (see
  ``meridian_codeindex.vector_index.content_fingerprint``) so a caller can
  detect the persisted index has gone stale relative to the live corpus,
* an incrementing ``revision`` bumped on every state change, and
* the last ``benchmark_evidence`` (JSON) + decision reason that justified
  (or refused) turning pgvector on — the audit trail the item's notes
  require: "Do not introduce pgvector merely because it exists; require
  measured recall, latency, memory, and cost evidence."

``pgvector_enabled`` is the only field a caller should read to decide
whether it may route real queries at the shared Postgres backend — and it is
only ever flipped ``True`` by :func:`record_vector_backend_benchmark`, never
by :func:`upsert_vector_index_state` (which only records "here is the
backend I'm actually using right now", not a decision to enable a new one).

Mirrors the minimal single-table shape of ``meridian/db/verification_runs.py``
(525d86bb) and ``meridian/db/batch_claim.py`` (22cad9b8) — a create/update
lifecycle over one table, no state machine, no children.
"""
from __future__ import annotations

import json
from typing import Any

import aiosqlite

from meridian.db import _new_id, _row_to_dict  # noqa: PLC0415

#: Every backend name this table (and the vector_index contract) knows about.
#: 'bm25' records the honest degraded state — no vector backend is active.
VECTOR_INDEX_BACKENDS: frozenset[str] = frozenset({"bm25", "duckdb_vss", "pgvector"})

DEFAULT_SCOPE = "default"


async def _migrate_vector_index_state(db: aiosqlite.Connection) -> None:
    """e1475682 — create ``vector_index_state`` on existing SQLite DBs.

    Guarded migration (no inline CREATE INDEX in the unguarded base schema
    literals — 2026-07-04 outage rule): the table + its unique index are
    created here so existing DBs pick them up on the first startup after
    deploy. Idempotent. Mirrors
    pg_adapter._migrate_pg_vector_index_state.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS vector_index_state (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            scope TEXT NOT NULL DEFAULT 'default',
            backend TEXT NOT NULL DEFAULT 'bm25',
            embedding_model TEXT,
            embedding_version TEXT,
            dimension INTEGER,
            source_fingerprint TEXT,
            revision INTEGER NOT NULL DEFAULT 1,
            pgvector_enabled INTEGER NOT NULL DEFAULT 0,
            benchmark_evidence TEXT,
            benchmark_decision_reason TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_vector_index_state_scope "
        "ON vector_index_state(project_id, scope)"
    )
    await db.commit()


async def get_vector_index_state(
    db: aiosqlite.Connection, project_id: str, scope: str = DEFAULT_SCOPE,
) -> dict[str, Any] | None:
    """Return the persisted vector-index state for ``(project_id, scope)``,
    or ``None`` if this scope has never been recorded."""
    async with db.execute(
        "SELECT * FROM vector_index_state WHERE project_id = ? AND scope = ?",
        (project_id, scope),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def list_vector_index_states(
    db: aiosqlite.Connection, project_id: str,
) -> list[dict[str, Any]]:
    """All recorded vector-index scopes for a project, most-recently-updated
    first."""
    async with db.execute(
        "SELECT * FROM vector_index_state WHERE project_id = ? "
        "ORDER BY updated_at DESC, id DESC",
        (project_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [r for r in (_row_to_dict(row) for row in rows) if r is not None]


async def upsert_vector_index_state(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    scope: str = DEFAULT_SCOPE,
    backend: str = "bm25",
    embedding_model: str | None = None,
    embedding_version: str | None = None,
    dimension: int | None = None,
    source_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Record "this is the backend/embedding actually in use right now" for
    ``(project_id, scope)``.

    Creates the row on first call (``revision=1``); every subsequent call
    bumps ``revision`` by one, so a caller can tell an index was rebuilt (new
    ``source_fingerprint``) even when the backend name didn't change.  Never
    touches ``pgvector_enabled`` / ``benchmark_evidence`` — those are owned
    exclusively by :func:`record_vector_backend_benchmark`, so merely
    re-indexing on the currently-active backend can never silently flip the
    pgvector gate open.

    Raises ``ValueError`` for a ``backend`` outside :data:`VECTOR_INDEX_BACKENDS`
    — an unrecognized backend name is exactly the kind of ambiguous state this
    table exists to make impossible to record.
    """
    if backend not in VECTOR_INDEX_BACKENDS:
        raise ValueError(
            f"Invalid vector-index backend {backend!r}. "
            f"Valid: {sorted(VECTOR_INDEX_BACKENDS)}"
        )
    existing = await get_vector_index_state(db, project_id, scope)
    if existing is None:
        row_id = _new_id()
        await db.execute(
            "INSERT INTO vector_index_state "
            "(id, project_id, scope, backend, embedding_model, "
            "embedding_version, dimension, source_fingerprint, revision) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                row_id, project_id, scope, backend, embedding_model,
                embedding_version, dimension, source_fingerprint,
            ),
        )
    else:
        await db.execute(
            "UPDATE vector_index_state SET backend = ?, embedding_model = ?, "
            "embedding_version = ?, dimension = ?, source_fingerprint = ?, "
            "revision = revision + 1, updated_at = datetime('now') "
            "WHERE project_id = ? AND scope = ?",
            (
                backend, embedding_model, embedding_version, dimension,
                source_fingerprint, project_id, scope,
            ),
        )
    await db.commit()
    updated = await get_vector_index_state(db, project_id, scope)
    assert updated is not None  # just written
    return updated


async def record_vector_backend_benchmark(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    scope: str = DEFAULT_SCOPE,
    evidence: dict[str, Any],
    pgvector_enabled: bool,
    reason: str,
) -> dict[str, Any]:
    """Persist a benchmark comparison's evidence + decision.

    ``evidence`` is expected to be the JSON-ready dict
    :func:`meridian_codeindex.vector_index.compare_candidates` returns (or an
    equivalent — this function does not validate its shape beyond requiring
    it to be JSON-serializable), stored verbatim as the audit trail behind
    ``pgvector_enabled``. Bumps ``revision``. Raises ``ValueError`` if no
    :func:`upsert_vector_index_state` call has ever recorded this
    ``(project_id, scope)`` — a benchmark decision needs something to decide
    *about*; recording one for a scope with no active-backend row on file
    would be evidence with nothing to anchor it to.
    """
    existing = await get_vector_index_state(db, project_id, scope)
    if existing is None:
        raise ValueError(
            f"no vector_index_state row for project_id={project_id!r} "
            f"scope={scope!r} — call upsert_vector_index_state first to "
            "record the active backend before recording benchmark evidence."
        )
    await db.execute(
        "UPDATE vector_index_state SET pgvector_enabled = ?, "
        "benchmark_evidence = ?, benchmark_decision_reason = ?, "
        "revision = revision + 1, updated_at = datetime('now') "
        "WHERE project_id = ? AND scope = ?",
        (
            1 if pgvector_enabled else 0,
            json.dumps(evidence),
            reason,
            project_id, scope,
        ),
    )
    await db.commit()
    updated = await get_vector_index_state(db, project_id, scope)
    assert updated is not None
    return updated
