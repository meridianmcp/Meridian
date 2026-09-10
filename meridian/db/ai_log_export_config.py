"""R2-G — durable, project-scoped config + resumable watermark state for the
OPTIONAL AI-log -> OTel/Langfuse export adapter (:mod:`meridian.ai_log_otel_export`).

BINDING ARCHITECTURAL DECISION (pinned 03112002) this module implements a
consequence of, NOT a rewrite of: ``meridian.ai_log``/``meridian.db.ai_log``'s
``ExecutionEvent`` stream is Meridian's canonical record of agent activity.
An external observability tool (self-hosted Langfuse, an OTel collector) may
consume an EXPORT of that stream later; it must never become a second source
of truth. This table therefore stores nothing that ``ai_log_events`` doesn't
already durably have — it is pure EXPORT BOOKKEEPING: per-project sink
config (is export on, where does it send, under what service name) plus a
resumable watermark (how far has a prior export pass gotten) and simple
retry/status bookkeeping. Deleting this table's row for a project and
starting over changes nothing about what Meridian itself knows — it only
means the next export pass re-sends from the beginning.

Mirrors the minimal single-table, create/upsert-lifecycle shape of
``meridian/db/object_sync_state.py`` (1d34c076) — see that module's
docstring for the ``sync_failed`` (transient, retry-eligible) vs
``unavailable`` (categorical — dependency/config/endpoint down, not solved
by retrying the same request) distinction this module reuses verbatim for
:data:`AI_LOG_EXPORT_STATES`. Dual-migrated: SQLite here, Postgres in
``pg_adapter._migrate_pg_ai_log_export_config``.

SECRETS / PROVENANCE (non-negotiable, per AGENTS.md's capability-manifest
provenance rule and this item's own notes): this table is project-shared,
multi-machine, durable metadata — exactly the kind of surface a bearer
token/API key must NEVER land in. ``otlp_endpoint``/``service_name`` are
free-text fields a project owner controls, so :func:`set_ai_log_export_config`
validates them with the SAME checks this codebase already uses for shared
config surfaces, reused rather than reimplemented:
  * :func:`meridian.secret_redaction.check_for_secrets` — the DB write-path
    secret gate already used by ``sprint_items.notes`` / ``task_log.description``
    / ``decisions_pinned.body`` / ``project_notes.body`` / ``ai_log_events.payload``.
  * :mod:`meridian.capability_manifest`'s ``_ABSOLUTE_PATH_RE`` (rejects a
    machine-local absolute path) and ``_SECRET_LIKE_RE`` (additionally
    catches ``://user:pass@host`` embedded credentials, ``bearer <token>``
    prose, and ``api_key=``/``password=`` assignments that
    ``check_for_secrets``'s known-key-shape patterns don't target).
There is deliberately NO column anywhere in this table for a bearer token or
API key — the actual OTLP auth header always comes from an environment
variable at send time (``MERIDIAN_AI_LOG_OTEL_HEADERS`` / the standard
``OTEL_EXPORTER_OTLP_HEADERS``), read directly by
:mod:`meridian.ai_log_otel_export` and never persisted here. This is a
structural guarantee, not just a validation one: even a caller who bypasses
:func:`set_ai_log_export_config`'s validation (a hypothetical future bug)
has no column to put a token in.
"""
from __future__ import annotations

import json
from typing import Any

import aiosqlite

from meridian.db import _new_id, _row_to_dict  # noqa: PLC0415
from meridian.db.ai_log import _deserialize_event_row  # noqa: PLC0415

#: Mirrors object_sync_state.OBJECT_SYNC_STATES' transient/categorical split.
#: 'disabled' — feature/project not opted in (the default; nothing has ever
#:               been attempted). 'unavailable' — CATEGORICAL: dependency not
#:               installed, or no endpoint configured; retrying the exact
#:               same call will not help. 'sync_failed' — TRANSIENT: a
#:               network/timeout/5xx during send; retry-eligible.
#: 'degraded' — SOME but not all of the fetched batch was sent (a later
#:               chunk failed after retries; the watermark still advanced
#:               past every chunk that DID send, so this is honest partial
#:               progress, not silent data loss). 'sent' — the full fetched
#:               batch sent successfully. 'sending' — an interim, non-
#:               terminal marker written after each individually-successful
#:               chunk mid-run (mirrors 'queued_sync' in object_sync_state:
#:               a real in-progress state, not stuck-forever — a crash here
#:               just means the next attempt resumes past whatever already
#:               sent). 'idle' — enabled and healthy, but nothing new to
#:               export on the last attempt. 'error' — an unexpected
#:               exception was caught (see this module's own "never raises"
#:               contract) — never allowed to propagate.
AI_LOG_EXPORT_STATES: frozenset[str] = frozenset({
    "disabled", "unavailable", "sync_failed", "degraded", "sent", "sending", "idle", "error",
})

#: States a retry sweep may reasonably re-attempt from — mirrors
#: object_sync_state.RETRY_ELIGIBLE_STATES.
RETRY_ELIGIBLE_STATES: frozenset[str] = frozenset({"sync_failed", "degraded"})

_VALID_PROTOCOLS: frozenset[str] = frozenset({"otlp_http", "langfuse_otlp"})


async def _migrate_ai_log_export_config(db: aiosqlite.Connection) -> None:
    """R2-G — create ``ai_log_export_config`` on both fresh and existing
    SQLite DBs. Idempotent (CREATE TABLE/INDEX IF NOT EXISTS) — no separate
    CREATE_TABLES literal entry needed, same convention as
    ``db.ai_log``/``db.object_sync_state`` (see their module docstrings).
    Mirrored in ``pg_adapter._migrate_pg_ai_log_export_config``.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS ai_log_export_config (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            sink TEXT NOT NULL DEFAULT 'otel_otlp',
            enabled INTEGER,
            otlp_endpoint TEXT,
            protocol TEXT NOT NULL DEFAULT 'otlp_http',
            service_name TEXT,
            langfuse_compat INTEGER NOT NULL DEFAULT 0,
            last_exported_recorded_at TEXT,
            last_exported_event_id TEXT,
            last_exported_ids_at_watermark TEXT,
            status TEXT NOT NULL DEFAULT 'disabled',
            last_error TEXT,
            last_attempt_at TEXT,
            last_success_at TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_log_export_config_project "
        "ON ai_log_export_config(project_id, sink)"
    )
    await db.commit()


def _validate_no_secret_or_path(value: "str | None", *, label: str) -> None:
    """Reuses (never reimplements) this codebase's two existing shared-config
    safety gates. Raises ``ValueError`` on any match — fail-closed, same
    posture as ``check_for_secrets`` itself."""
    if not value:
        return
    from meridian.secret_redaction import check_for_secrets  # noqa: PLC0415
    check_for_secrets(value, context=f"ai_log_export_config.{label}")
    # capability_manifest's regexes are private-by-convention (leading
    # underscore) but explicitly designated for cross-module reuse by this
    # item's own instructions — see this module's docstring.
    from meridian.capability_manifest import (  # noqa: PLC0415
        _ABSOLUTE_PATH_RE, _SECRET_LIKE_RE,
    )
    if _ABSOLUTE_PATH_RE.search(value):
        raise ValueError(
            f"ai_log_export_config.{label} must not be a machine-local "
            f"absolute path (this is project-shared, multi-machine state): {value!r}"
        )
    if _SECRET_LIKE_RE.search(value):
        raise ValueError(
            f"ai_log_export_config.{label} must not contain a secret-shaped "
            "value (embedded URL credentials, a bearer token, an api_key= or "
            "password= assignment). An OTLP auth header belongs in the "
            "MERIDIAN_AI_LOG_OTEL_HEADERS environment variable, never in "
            "persisted project config."
        )


async def get_ai_log_export_config(
    db: aiosqlite.Connection, project_id: str,
) -> "dict[str, Any] | None":
    """The export config/watermark row for *project_id*, or ``None`` if
    export has never been configured/attempted for it (the common case —
    nothing here changes behavior for a project that never touches this
    surface)."""
    if not project_id:
        raise ValueError("project_id is required")
    async with db.execute(
        "SELECT * FROM ai_log_export_config WHERE project_id = ? AND sink = 'otel_otlp'",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def set_ai_log_export_config(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    enabled: "bool | None" = None,
    otlp_endpoint: "str | None" = None,
    protocol: "str | None" = None,
    service_name: "str | None" = None,
    langfuse_compat: "bool | None" = None,
) -> "dict[str, Any]":
    """Upsert per-project export config. Every parameter left ``None`` keeps
    its existing stored value unchanged (or the column default, on first
    insert) — mirrors ``db.object_sync_state``'s ``COALESCE``-based partial
    upsert. Only ``otlp_endpoint``/``service_name`` are validated (the only
    free-text fields here — see this module's docstring); ``enabled``/
    ``langfuse_compat`` are booleans and ``protocol`` is a closed enum,
    none of which admit a secret shape.

    Raises ``ValueError`` for: an invalid ``protocol``; an ``otlp_endpoint``
    that isn't ``http://``/``https://``; or either free-text field failing
    the shared secret/path checks (see :func:`_validate_no_secret_or_path`).
    Never inserts/updates a row in any of those cases.
    """
    if not project_id:
        raise ValueError("project_id is required")
    if protocol is not None and protocol not in _VALID_PROTOCOLS:
        raise ValueError(f"protocol must be one of {sorted(_VALID_PROTOCOLS)}, got {protocol!r}")
    _validate_no_secret_or_path(otlp_endpoint, label="otlp_endpoint")
    _validate_no_secret_or_path(service_name, label="service_name")
    if otlp_endpoint and not (otlp_endpoint.startswith("http://") or otlp_endpoint.startswith("https://")):
        raise ValueError("otlp_endpoint must be an http:// or https:// URL")

    _enabled_val = None if enabled is None else (1 if enabled else 0)
    _langfuse_val = None if langfuse_compat is None else (1 if langfuse_compat else 0)

    existing = await get_ai_log_export_config(db, project_id)
    if existing is None:
        await db.execute(
            "INSERT INTO ai_log_export_config "
            "(id, project_id, sink, enabled, otlp_endpoint, protocol, service_name, langfuse_compat) "
            "VALUES (?, ?, 'otel_otlp', ?, ?, ?, ?, ?)",
            (
                _new_id(), project_id, _enabled_val, otlp_endpoint,
                protocol or "otlp_http", service_name, _langfuse_val or 0,
            ),
        )
    else:
        await db.execute(
            "UPDATE ai_log_export_config SET "
            "enabled = COALESCE(?, enabled), "
            "otlp_endpoint = COALESCE(?, otlp_endpoint), "
            "protocol = COALESCE(?, protocol), "
            "service_name = COALESCE(?, service_name), "
            "langfuse_compat = COALESCE(?, langfuse_compat), "
            "updated_at = datetime('now') "
            "WHERE project_id = ? AND sink = 'otel_otlp'",
            (
                _enabled_val, otlp_endpoint, protocol, service_name, _langfuse_val,
                project_id,
            ),
        )
    await db.commit()
    updated = await get_ai_log_export_config(db, project_id)
    assert updated is not None  # just written
    return updated


async def record_export_attempt(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    status: str,
    last_error: "str | None" = None,
    bump_retry: bool = False,
    reset_retry: bool = False,
) -> "dict[str, Any]":
    """Record the outcome of one export attempt (success or failure) —
    always ensures a row exists first (a project that has never called
    :func:`set_ai_log_export_config` can still accrue attempt history once
    export actually runs, e.g. purely env-var-driven config with no
    per-project override)."""
    if status not in AI_LOG_EXPORT_STATES:
        raise ValueError(f"Invalid export status {status!r}. Valid: {sorted(AI_LOG_EXPORT_STATES)}")
    existing = await get_ai_log_export_config(db, project_id)
    if existing is None:
        await db.execute(
            "INSERT INTO ai_log_export_config (id, project_id, sink, status, last_error, "
            "last_attempt_at, retry_count) "
            "VALUES (?, ?, 'otel_otlp', ?, ?, datetime('now'), ?)",
            (_new_id(), project_id, status, last_error, 1 if bump_retry else 0),
        )
    else:
        new_retry = existing.get("retry_count") or 0
        if bump_retry:
            new_retry += 1
        if reset_retry:
            new_retry = 0
        await db.execute(
            "UPDATE ai_log_export_config SET status = ?, last_error = ?, "
            "last_attempt_at = datetime('now'), retry_count = ?, updated_at = datetime('now') "
            "WHERE project_id = ? AND sink = 'otel_otlp'",
            (status, last_error, new_retry, project_id),
        )
    await db.commit()
    updated = await get_ai_log_export_config(db, project_id)
    assert updated is not None
    return updated


async def record_export_success(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    last_exported_recorded_at: str,
    last_exported_event_id: str,
    exported_ids_at_watermark: "list[str] | None" = None,
    status: str = "sent",
) -> "dict[str, Any]":
    """Advance the resumable watermark after a batch/chunk sends
    successfully. ``status`` defaults to ``'sent'`` (the whole fetched batch
    went through) but a caller mid-batch with more chunks pending may pass
    an interim value; :func:`meridian.ai_log_otel_export.run_otel_export` is
    the only caller and always finishes with a terminal status.

    ``exported_ids_at_watermark`` — the FULL set of ``ai_log_events.id``
    values already sent that share ``last_exported_recorded_at`` exactly
    (see :func:`fetch_new_events_for_export`'s docstring for why a single
    ``last_exported_event_id`` cannot, on its own, safely gate a resume when
    ``recorded_at`` collides across rows). ``None`` leaves the stored value
    unchanged (matches this function's other COALESCE-style partial-update
    fields) — callers that DO track it (only
    :mod:`meridian.ai_log_otel_export` today) must always pass the complete,
    up-to-date set for the CURRENT watermark timestamp, not a delta.
    """
    if status not in AI_LOG_EXPORT_STATES:
        raise ValueError(f"Invalid export status {status!r}. Valid: {sorted(AI_LOG_EXPORT_STATES)}")
    ids_json = json.dumps(list(exported_ids_at_watermark)) if exported_ids_at_watermark is not None else None
    existing = await get_ai_log_export_config(db, project_id)
    if existing is None:
        await db.execute(
            "INSERT INTO ai_log_export_config "
            "(id, project_id, sink, status, last_exported_recorded_at, last_exported_event_id, "
            "last_exported_ids_at_watermark, last_success_at, last_error, retry_count) "
            "VALUES (?, ?, 'otel_otlp', ?, ?, ?, ?, datetime('now'), NULL, 0)",
            (
                _new_id(), project_id, status, last_exported_recorded_at,
                last_exported_event_id, ids_json,
            ),
        )
    else:
        await db.execute(
            "UPDATE ai_log_export_config SET status = ?, last_exported_recorded_at = ?, "
            "last_exported_event_id = ?, "
            "last_exported_ids_at_watermark = COALESCE(?, last_exported_ids_at_watermark), "
            "last_success_at = datetime('now'), "
            "last_error = NULL, retry_count = 0, updated_at = datetime('now') "
            "WHERE project_id = ? AND sink = 'otel_otlp'",
            (status, last_exported_recorded_at, last_exported_event_id, ids_json, project_id),
        )
    await db.commit()
    updated = await get_ai_log_export_config(db, project_id)
    assert updated is not None
    return updated


def decode_exported_ids_at_watermark(raw: "str | None") -> "list[str]":
    """Decode the ``last_exported_ids_at_watermark`` JSON column (or a
    missing/malformed value) into a plain list — never raises. Shared by
    :mod:`meridian.ai_log_otel_export` so the JSON encoding stays this
    module's own private concern."""
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in decoded] if isinstance(decoded, list) else []


async def fetch_new_events_for_export(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    after_recorded_at: "str | None",
    after_event_ids: "list[str] | None",
    limit: int,
) -> "list[dict[str, Any]]":
    """Bounded, ASCENDING, resumable-cursor read of ``ai_log_events`` for
    *project_id* — the "bounded queue" the export path pulls from.

    Deliberately NOT :func:`meridian.db.ai_log.list_events`/``search_events``
    (both order newest-recorded-first, right for browsing, wrong for a
    forward-progressing export cursor) and NOT
    :func:`meridian.db.ai_log.build_run_timeline` (its own truncation policy
    keeps the NEWEST events and drops the oldest when over its limit — the
    opposite of what a durable, gap-free export cursor needs: every event
    must eventually be sent, in order, none silently dropped for being
    "old").

    ``after_event_ids`` — EVERY id already exported that shares
    ``after_recorded_at`` exactly, not just the single most-recently-sent
    one. ``ai_log_events.id`` is a random ``uuid4()`` (see
    :func:`meridian.db._new_id`), so its lexicographic order carries no
    relationship to insertion order; a single-id ``id > after_event_id``
    cursor (this function's first shipped version) can permanently DROP a
    sibling row minted in the same wall-clock second whose id happens to
    sort below the watermark id — SQLite's ``datetime('now')`` default is
    only second-precision (see ``db.ai_log``'s own module docstring on this
    column), so same-second collisions are common, not a rare edge case,
    under any real burst of tool-call activity. Excluding the full
    already-sent set for that exact timestamp instead of comparing a single
    id closes that gap: a same-second row is only ever skipped once it is
    actually IN the exclusion set (i.e. already sent), never because of
    where its random id happens to sort.

    Read-only against ``ai_log_events`` — this module and
    :mod:`meridian.ai_log_otel_export` never write to that table (the
    binding decision this module's docstring opens with: Meridian's own DB
    stays authoritative, this is an export adapter only).

    ``limit`` is the caller's already-clamped batch size (see
    :mod:`meridian.ai_log_otel_export`'s ``MERIDIAN_AI_LOG_OTEL_BATCH_SIZE``)
    — this function does not re-clamp, so a caller must clamp first.
    """
    if not project_id:
        raise ValueError("project_id is required")
    if after_recorded_at is None:
        sql = (
            "SELECT * FROM ai_log_events WHERE project_id = ? "
            "ORDER BY recorded_at ASC, id ASC LIMIT ?"
        )
        params: tuple[Any, ...] = (project_id, limit)
    else:
        _ids = [i for i in (after_event_ids or []) if i]
        if _ids:
            placeholders = ", ".join("?" for _ in _ids)
            sql = (
                "SELECT * FROM ai_log_events WHERE project_id = ? "
                f"AND (recorded_at > ? OR (recorded_at = ? AND id NOT IN ({placeholders}))) "
                "ORDER BY recorded_at ASC, id ASC LIMIT ?"
            )
            params = (project_id, after_recorded_at, after_recorded_at, *_ids, limit)
        else:
            # Defensive fallback for an inconsistent/legacy watermark (a
            # recorded_at with no accompanying id set) -- prefer a possible
            # duplicate re-send over ever silently dropping a same-second
            # row, matching this whole module's "no silent gaps" contract.
            sql = (
                "SELECT * FROM ai_log_events WHERE project_id = ? "
                "AND recorded_at >= ? "
                "ORDER BY recorded_at ASC, id ASC LIMIT ?"
            )
            params = (project_id, after_recorded_at, limit)
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = _deserialize_event_row(_row_to_dict(r))
        if d is not None:
            out.append(d)
    return out
