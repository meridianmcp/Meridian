"""1d34c076 (build milestone; investigation 549e66c6 §6) — durable sync-state
tracking for the optional, inactive-by-default object-storage backend
(:mod:`meridian.object_store`).

Why this exists: because local content stored by
:mod:`meridian.artifact_store` is addressed by ``sha256`` and immutable,
a CONTENT object itself can never go stale — new bytes produce a new hash
and a new object, never a mutation. This table therefore tracks, per
``(project_id, content_hash)``, whether that content has ever been synced
to a remote object-storage backend, and if not, why not.

STATE MACHINE (investigation §6)
---------------------------------
::

    local_only  --enqueue-->  queued_sync  --PUT succeeds-->  synced
    queued_sync --PUT fails (network/timeout/5xx)-->           sync_failed
    queued_sync --backend unconfigured/auth error-->           unavailable
    sync_failed --retry (backoff-eligible)-->                  queued_sync
    unavailable --capability restored, re-enqueue sweep-->     queued_sync

``sync_failed`` (transient — network/5xx/``ConditionalRequestConflictError``,
retry) is deliberately distinct from ``unavailable`` (categorical —
auth/config/endpoint down, not solved by retrying the same request) — this
maps directly onto ``AGENTS.md``'s ``required``/``optional``/``degraded_ok``
capability-manifest contract: an ``unavailable`` object-storage capability
marked ``optional`` degrades to "stay local_only, note why, keep working"
rather than blocking anything.

``remote_stale`` is included in :data:`OBJECT_SYNC_STATES` for forward
compatibility with a future MUTABLE "pointer" key (e.g. "latest handoff
bundle for project X") — it deliberately does NOT apply to a content-hash
row in this table today; nothing in this module transitions a row to
``remote_stale`` yet (investigation §6's explicit modeling point: this must
stay a distinct concept from content sync state, never conflated).

This state is coordination METADATA — small, relational, queryable — and
therefore correctly lives in the database, not the bucket, exactly like
every other authoritative table in this repo (never object storage itself).

Mirrors the minimal single-table shape of
``meridian/db/vector_index_state.py`` (e1475682) — a create/upsert
lifecycle over one table, no children, dual-migrated (SQLite here,
Postgres in ``pg_adapter._migrate_pg_object_sync_state``).
"""
from __future__ import annotations

from typing import Any

import aiosqlite

from meridian.db import _new_id, _row_to_dict  # noqa: PLC0415

#: Every state this table's rows can be in. 'remote_stale' is reserved for
#: a future mutable-pointer feature — see module docstring; no function
#: below ever sets it.
OBJECT_SYNC_STATES: frozenset[str] = frozenset({
    "local_only", "queued_sync", "synced", "remote_stale",
    "sync_failed", "unavailable",
})

#: States from which a retry/re-enqueue sweep may move a row back to
#: 'queued_sync' (investigation §6 state machine).
RETRY_ELIGIBLE_STATES: frozenset[str] = frozenset({"sync_failed", "unavailable"})


async def _migrate_object_sync_state(db: aiosqlite.Connection) -> None:
    """1d34c076 — create ``object_sync_state`` on existing SQLite DBs.

    Guarded migration (no inline CREATE INDEX in the unguarded base schema
    literals — 2026-07-04 outage rule): the table + its unique index are
    created here so existing DBs pick them up on the first startup after
    deploy. Idempotent. Mirrors pg_adapter._migrate_pg_object_sync_state.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS object_sync_state (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            content_hash TEXT NOT NULL,
            backend TEXT NOT NULL DEFAULT 'local',
            artifact_class TEXT,
            state TEXT NOT NULL DEFAULT 'local_only',
            remote_key TEXT,
            remote_etag TEXT,
            queued_at TEXT,
            synced_at TEXT,
            last_error TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_object_sync_state_project_hash "
        "ON object_sync_state(project_id, content_hash)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_object_sync_state_state "
        "ON object_sync_state(state)"
    )
    await db.commit()


async def get_object_sync_state(
    db: aiosqlite.Connection, project_id: str, content_hash: str,
) -> "dict[str, Any] | None":
    """Return the sync-state row for ``(project_id, content_hash)``, or
    ``None`` if this content has never been recorded (i.e. it is purely
    ``local_only`` and no one has ever tried to sync it)."""
    async with db.execute(
        "SELECT * FROM object_sync_state WHERE project_id = ? AND content_hash = ?",
        (project_id, content_hash),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def list_object_sync_states(
    db: aiosqlite.Connection, project_id: str, *, state: "str | None" = None,
) -> "list[dict[str, Any]]":
    """All recorded sync-state rows for a project, optionally filtered to
    one ``state``, most-recently-updated first."""
    if state is not None and state not in OBJECT_SYNC_STATES:
        raise ValueError(
            f"Invalid object-sync state {state!r}. Valid: {sorted(OBJECT_SYNC_STATES)}"
        )
    if state is None:
        async with db.execute(
            "SELECT * FROM object_sync_state WHERE project_id = ? "
            "ORDER BY updated_at DESC, id DESC",
            (project_id,),
        ) as cur:
            rows = await cur.fetchall()
    else:
        async with db.execute(
            "SELECT * FROM object_sync_state WHERE project_id = ? AND state = ? "
            "ORDER BY updated_at DESC, id DESC",
            (project_id, state),
        ) as cur:
            rows = await cur.fetchall()
    return [r for r in (_row_to_dict(row) for row in rows) if r is not None]


async def list_retry_eligible(
    db: aiosqlite.Connection, project_id: str,
) -> "list[dict[str, Any]]":
    """Rows in ``sync_failed`` or ``unavailable`` for a project — the set a
    retry/re-enqueue sweep should consider (investigation §6: "a manual
    re-enqueue path... exists for unavailable rows once a human confirms
    the backend is reachable again — never an automatic tight retry loop
    against a confirmed-down endpoint"). This function only SELECTS; it
    never itself transitions a row."""
    placeholders = ",".join("?" for _ in RETRY_ELIGIBLE_STATES)
    async with db.execute(
        f"SELECT * FROM object_sync_state WHERE project_id = ? "
        f"AND state IN ({placeholders}) ORDER BY updated_at ASC, id ASC",
        (project_id, *sorted(RETRY_ELIGIBLE_STATES)),
    ) as cur:
        rows = await cur.fetchall()
    return [r for r in (_row_to_dict(row) for row in rows) if r is not None]


async def _upsert_state(
    db: aiosqlite.Connection,
    project_id: str,
    content_hash: str,
    *,
    state: str,
    backend: "str | None" = None,
    artifact_class: "str | None" = None,
    remote_key: "str | None" = None,
    remote_etag: "str | None" = None,
    queued_at: "str | None" = None,
    synced_at: "str | None" = None,
    last_error: "str | None" = None,
    bump_retry: bool = False,
    reset_retry: bool = False,
) -> "dict[str, Any]":
    if state not in OBJECT_SYNC_STATES:
        raise ValueError(
            f"Invalid object-sync state {state!r}. Valid: {sorted(OBJECT_SYNC_STATES)}"
        )
    existing = await get_object_sync_state(db, project_id, content_hash)
    if existing is None:
        row_id = _new_id()
        await db.execute(
            "INSERT INTO object_sync_state "
            "(id, project_id, content_hash, backend, artifact_class, state, "
            "remote_key, remote_etag, queued_at, synced_at, last_error, retry_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id, project_id, content_hash,
                backend or "local", artifact_class, state,
                remote_key, remote_etag, queued_at, synced_at, last_error,
                1 if bump_retry else 0,
            ),
        )
    else:
        new_retry = existing.get("retry_count") or 0
        if bump_retry:
            new_retry += 1
        if reset_retry:
            new_retry = 0
        await db.execute(
            "UPDATE object_sync_state SET "
            "backend = COALESCE(?, backend), "
            "artifact_class = COALESCE(?, artifact_class), "
            "state = ?, "
            "remote_key = COALESCE(?, remote_key), "
            "remote_etag = COALESCE(?, remote_etag), "
            "queued_at = COALESCE(?, queued_at), "
            "synced_at = COALESCE(?, synced_at), "
            "last_error = ?, "
            "retry_count = ?, "
            "updated_at = datetime('now') "
            "WHERE project_id = ? AND content_hash = ?",
            (
                backend, artifact_class, state, remote_key, remote_etag,
                queued_at, synced_at, last_error, new_retry,
                project_id, content_hash,
            ),
        )
    await db.commit()
    updated = await get_object_sync_state(db, project_id, content_hash)
    assert updated is not None  # just written
    return updated


async def mark_local_only(
    db: aiosqlite.Connection,
    project_id: str,
    content_hash: str,
    *,
    backend: str = "local",
    artifact_class: "str | None" = None,
) -> "dict[str, Any]":
    """Record that *content_hash* exists locally (via
    :mod:`meridian.artifact_store`) and has not been queued for remote
    sync. Safe to call unconditionally after every local store — it is
    the natural starting state and this function is idempotent (does not
    reset an already-``queued_sync``/``synced`` row backward)."""
    existing = await get_object_sync_state(db, project_id, content_hash)
    if existing is not None:
        return existing
    return await _upsert_state(
        db, project_id, content_hash,
        state="local_only", backend=backend, artifact_class=artifact_class,
    )


async def mark_queued_sync(
    db: aiosqlite.Connection, project_id: str, content_hash: str,
    *, backend: "str | None" = None, queued_at: "str | None" = None,
) -> "dict[str, Any]":
    """Transition to ``queued_sync`` — a sync attempt for this content is
    about to be (or has just been) enqueued."""
    return await _upsert_state(
        db, project_id, content_hash, state="queued_sync",
        backend=backend, queued_at=queued_at or _now_iso(),
    )


async def mark_synced(
    db: aiosqlite.Connection, project_id: str, content_hash: str,
    *, remote_key: str, remote_etag: "str | None" = None,
    backend: "str | None" = None, synced_at: "str | None" = None,
) -> "dict[str, Any]":
    """Transition to ``synced`` — the remote ``PUT`` succeeded. Clears
    ``last_error`` and resets ``retry_count`` to 0 (a successful sync
    fully resolves any prior failure history for this content)."""
    return await _upsert_state(
        db, project_id, content_hash, state="synced",
        backend=backend, remote_key=remote_key, remote_etag=remote_etag,
        synced_at=synced_at or _now_iso(), last_error=None, reset_retry=True,
    )


async def mark_sync_failed(
    db: aiosqlite.Connection, project_id: str, content_hash: str,
    *, error: str, backend: "str | None" = None,
) -> "dict[str, Any]":
    """Transition to ``sync_failed`` — TRANSIENT (network/timeout/5xx/
    conditional-request-conflict). Retry-eligible; bumps ``retry_count``
    so backoff can be keyed on it."""
    return await _upsert_state(
        db, project_id, content_hash, state="sync_failed",
        backend=backend, last_error=error, bump_retry=True,
    )


async def mark_unavailable(
    db: aiosqlite.Connection, project_id: str, content_hash: str,
    *, error: str, backend: "str | None" = None,
) -> "dict[str, Any]":
    """Transition to ``unavailable`` — CATEGORICAL (auth/config/endpoint
    down). Not solved by retrying the same request; a human must confirm
    the backend is reachable again before a re-enqueue sweep should touch
    this row (investigation §6)."""
    return await _upsert_state(
        db, project_id, content_hash, state="unavailable",
        backend=backend, last_error=error, bump_retry=True,
    )


def _now_iso() -> str:
    from datetime import datetime, timezone
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
