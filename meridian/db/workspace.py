"""Workspace-layer persistence functions — extracted from meridian/db/__init__.py.

This module contains all functions whose primary subject is the workspace layer:
tenant-global notes, decisions, proposals, sprint-items, settings (cross-project
backlog distinct from per-project sprint_items), and workspace member/invite
management.

Imported back into meridian.db via an explicit named re-export at the bottom of
db/__init__.py so all existing ``db_module.function_name()``-style call sites are
unaffected.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from typing import Any

import aiosqlite

# Shared helpers from the parent db package.  These are imported at the BOTTOM
# of db/__init__.py (after the parent module defines them), then re-used here.
# Using a lazy module-attribute import via the package __init__ avoids the
# circular-import that would occur if we imported at module top-level while
# db/__init__.py is still being initialised.
from meridian.db import (  # noqa: PLC0415
    _new_id,
    _row_to_dict,
    normalize_execution_mode,
    get_project,
    add_project_note,
    serialize_touches_resources,
    _unique_proposal_slug,
    _unique_proposal_nickname,
    _sprint_item_slug_base,
    _sprint_item_nickname_base,
)


# ---------------------------------------------------------------------------
# a56f0951 — touches_resources inference for promote_workspace_proposal.
#
# When a workspace proposal is promoted to a sprint item the created item had
# zero touches_resources by construction — same gap class as fba94f1a. This
# helper mirrors the keyword-match logic in handoff._annotate_touches_files:
# extract significant words from the proposal title + body, then match them
# against recently-changed files (git diff --name-only HEAD~3) to produce
# inferred:file:<path> resource identifiers.
#
# Returns a list of inferred resource strings (may be empty on no match or
# error). Safe to call from any context — never raises.
# ---------------------------------------------------------------------------

_PROPOSAL_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "in", "on", "at", "to", "for",
    "of", "is", "it", "fix", "add", "update", "remove", "change",
    "with", "from", "by", "via", "use", "set", "get", "put", "new",
    "this", "that", "into", "as", "be", "has", "was", "not", "no",
})


def _proposal_keywords(text: str) -> set[str]:
    """Extract significant lowercase keywords from a proposal title or body.

    Mirrors handoff._extract_keywords without its extra_stop parameter:
    returns 3+-char alphanumeric/underscore/dash/slash tokens excluding the
    common English stop-words."""
    words = re.findall(r"[a-z0-9_/-]{3,}", text.lower())
    return {w for w in words if w not in _PROPOSAL_STOP_WORDS}


def _infer_touches_resources_from_proposal(title: str, body: str) -> list[str]:
    """Infer inferred:file:<path> resource identifiers for a workspace proposal.

    Queries ``git diff --name-only HEAD~3`` for recently changed files, then
    keyword-matches the proposal's title+body against each path. Returns at
    most 10 ``inferred:file:<path>`` strings, or an empty list when no match
    is found or git is unavailable. Never raises."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~3"],
            capture_output=True, text=True, timeout=5,
        )
        changed_files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
    except Exception:  # noqa: BLE001
        return []
    if not changed_files:
        return []

    combined_text = f"{title} {body}"
    title_kws = _proposal_keywords(combined_text)
    if len(title_kws) < 2:
        return []

    matched: list[str] = []
    seen: set[str] = set()
    for fpath in changed_files:
        if fpath in seen:
            continue
        fname = os.path.basename(fpath)
        fname_stem = os.path.splitext(fname)[0]
        path_kws = _proposal_keywords(fpath.replace("/", " ").replace(".", " "))
        if fname_stem and len(fname_stem) >= 3 and fname_stem in title_kws:
            matched.append(fpath)
            seen.add(fpath)
        elif len(title_kws & path_kws) >= 2:
            matched.append(fpath)
            seen.add(fpath)
    return [f"inferred:file:{fpath}" for fpath in matched[:10]]


# ---------------------------------------------------------------------------
# v3.1 — workspace layer: tenant-global notes + decisions above projects
# ---------------------------------------------------------------------------


def _ws_tenant_clause(tenant_id: str | None) -> tuple[str, list[Any]]:
    """Return an ``AND (...)`` scope fragment + params for tenant isolation.

    When ``tenant_id`` is None (self-host / internal callers) returns ('', [])
    so behaviour is unchanged. When provided, rows owned by that tenant *or*
    pre-isolation rows (``tenant_id IS NULL``, only ever present on a dedicated
    per-tenant DB) match — see ``_migrate_workspace_tenant_isolation``.
    """
    if tenant_id is None:
        return "", []
    return "(tenant_id = ? OR tenant_id IS NULL)", [tenant_id]


async def add_workspace_note(
    db: aiosqlite.Connection,
    title: str,
    body: str,
    tags: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Insert a workspace_notes row. tags is comma-separated free-form.
    Workspace notes belong to the whole workspace, not a single project."""
    nid = _new_id()
    await db.execute(
        "INSERT INTO workspace_notes (id, title, body, tags, tenant_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (nid, title, body, tags, tenant_id),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM workspace_notes WHERE id = ?", (nid,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) or {"id": nid}


async def get_workspace_notes(
    db: aiosqlite.Connection,
    tag: str | None = None,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return workspace notes, newest first. Optional tag substring filter.
    Scoped to ``tenant_id`` when provided (hosted)."""
    clauses: list[str] = []
    params: list[Any] = []
    if tag:
        clauses.append("tags LIKE ?")
        params.append(f"%{tag}%")
    scope, scope_params = _ws_tenant_clause(tenant_id)
    if scope:
        clauses.append(scope)
        params.extend(scope_params)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    async with db.execute(
        f"SELECT * FROM workspace_notes{where} ORDER BY created_at DESC",
        params or None,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def delete_workspace_note(
    db: aiosqlite.Connection, note_id: str, tenant_id: str | None = None
) -> bool:
    """Hard-delete a workspace note. Returns True if a row was removed.
    Cannot delete another tenant's note when ``tenant_id`` is set."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    sql = "DELETE FROM workspace_notes WHERE id = ?" + (f" AND {scope}" if scope else "")
    async with db.execute(sql, [note_id, *scope_params]) as cur:
        rc = cur.rowcount or 0
    await db.commit()
    return rc > 0


async def move_workspace_note_to_project(
    db: aiosqlite.Connection,
    note_id: str,
    project_id: str,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Convert a workspace note into a project note on ``project_id``.

    Copies title/body/tags to a new project_notes row, then deletes the
    workspace note. Returns the new project note, or None if the workspace
    note was not found (or belongs to another tenant). Atomic: the delete
    only runs after the project note is created.
    """
    scope, scope_params = _ws_tenant_clause(tenant_id)
    sql = "SELECT * FROM workspace_notes WHERE id = ?" + (f" AND {scope}" if scope else "")
    async with db.execute(sql, [note_id, *scope_params]) as cur:
        row = await cur.fetchone()
    note = _row_to_dict(row) if row is not None else None
    if not note:
        return None
    # Guard against moving to a non-existent project.
    if await get_project(db, project_id) is None:
        return None
    created = await add_project_note(
        db,
        project_id,
        note.get("title") or "",
        note.get("body") or "",
        note.get("tags"),
    )
    await delete_workspace_note(db, note_id, tenant_id=tenant_id)
    return created


async def update_workspace_note(
    db: aiosqlite.Connection,
    note_id: str,
    title: str | None = None,
    body: str | None = None,
    tags: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Patch title/body/tags on an existing workspace note (tenant-scoped)."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    scope_sql = f" AND {scope}" if scope else ""
    sets, params = [], []
    if title is not None:
        sets.append("title = ?"); params.append(title)
    if body is not None:
        sets.append("body = ?"); params.append(body)
    if tags is not None:
        sets.append("tags = ?"); params.append(tags)
    if not sets:
        async with db.execute(
            f"SELECT * FROM workspace_notes WHERE id = ?{scope_sql}",
            [note_id, *scope_params],
        ) as cur:
            row = await cur.fetchone()
        return _row_to_dict(row)
    await db.execute(
        f"UPDATE workspace_notes SET {', '.join(sets)} WHERE id = ?{scope_sql}",
        [*params, note_id, *scope_params],
    )
    await db.commit()
    async with db.execute(
        f"SELECT * FROM workspace_notes WHERE id = ?{scope_sql}",
        [note_id, *scope_params],
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def pin_workspace_decision(
    db: aiosqlite.Connection,
    title: str,
    body: str,
    category: str = "TECHNICAL",
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Create a workspace-level pinned decision. category is free-text
    (STRATEGIC, TECHNICAL, PRODUCT, ...)."""
    did = _new_id()
    await db.execute(
        "INSERT INTO workspace_decisions (id, title, body, category, tenant_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (did, title, body, category, tenant_id),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM workspace_decisions WHERE id = ?", (did,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) or {"id": did}


async def get_workspace_decisions(
    db: aiosqlite.Connection,
    include_superseded: bool = False,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return workspace decisions, newest first. Active only by default.
    Scoped to ``tenant_id`` when provided (hosted)."""
    clauses: list[str] = []
    params: list[Any] = []
    if not include_superseded:
        clauses.append("status = 'active'")
    scope, scope_params = _ws_tenant_clause(tenant_id)
    if scope:
        clauses.append(scope)
        params.extend(scope_params)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    async with db.execute(
        f"SELECT * FROM workspace_decisions{where} ORDER BY created_at DESC",
        params or None,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def delete_workspace_decision(
    db: aiosqlite.Connection, decision_id: str, tenant_id: str | None = None
) -> bool:
    """Hard-delete a workspace decision. Returns True if a row was removed.
    Cannot delete another tenant's decision when ``tenant_id`` is set."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    sql = "DELETE FROM workspace_decisions WHERE id = ?" + (f" AND {scope}" if scope else "")
    async with db.execute(sql, [decision_id, *scope_params]) as cur:
        rc = cur.rowcount or 0
    await db.commit()
    return rc > 0


# --- Workspace proposals (human-only "drawer of inspiration") ---------------
# 5c4dcc0f — workspace-scoped (tenant_id) flash-of-insight capture. Distinct
# from workspace_notes (no lifecycle) and sprint_items (executor-claimable).
# status machine: raw → investigating → paused → promoted | rejected | closed.

_VALID_PROPOSAL_STATUSES = {
    "raw",
    "investigating",
    "paused",
    "promoted",
    "rejected",
    "closed",
    "superseded",
}
# 45c4c178 — "live" statuses: proposals still awaiting human triage. Terminal
# statuses (promoted/rejected/closed/superseded) are excluded from the default
# (status-omitted)
# view of get_workspace_proposals so the common "what's still open" query
# doesn't require the caller to already know to pass status= explicitly.
_LIVE_PROPOSAL_STATUSES = {"raw", "investigating", "paused"}
_PROPOSAL_TRANSITIONS: dict[str, set[str]] = {
    "raw": {"investigating", "paused", "rejected", "closed", "superseded"},
    "investigating": {
        "promoted", "paused", "rejected", "raw", "closed", "superseded",
    },
    "paused": {"investigating", "raw", "closed", "superseded"},
    "promoted": set(),   # terminal — use promote_workspace_proposal instead
    "rejected": {"raw"},  # allow un-reject back to raw
    "closed": {"raw"},  # reopening is represented by the raw state
    "superseded": set(),
}


# ---------------------------------------------------------------------------
# 867317f6 — transactional hardening for workspace proposal writes.
#
# Every proposal-mutating function below runs MULTIPLE statements (an insert
# plus an event-append, or a read-check plus a guarded update plus an
# event-append). On SQLite (aiosqlite, autocommit=False) that sequence is
# already one implicit transaction until the trailing ``db.commit()`` — a mid
# sequence exception leaves nothing committed. On Postgres
# (meridian.pg_adapter.PostgresConnection, autocommit=True — see its
# docstring) each ``db.execute()`` call grabs its OWN pooled connection and
# commits immediately; ``db.commit()``/``db.rollback()`` are no-ops there. A
# failure after the first statement of a multi-statement proposal write would
# otherwise leave a real partial-write state (e.g. a proposal row with no
# "created" event, or a promoted proposal with no linked sprint item).
#
# The fix mirrors the pattern already established in
# db/sprint_items.py::merge_sprint_items for the exact same Postgres
# autocommit gap: detect the backend via ``hasattr(db, "_pool")`` and, on
# failure, apply compensating statements to undo whatever already committed
# before re-raising. ``_is_proposal_schema_drift_error`` additionally
# classifies "missing table/column" failures — a partially-applied migration
# on this backend — into a deterministic, actionable ``ProposalSchemaError``
# instead of letting a raw driver exception (sqlite3.OperationalError /
# psycopg.errors.UndefinedColumn) leak out uninterpreted.
# ---------------------------------------------------------------------------


class ProposalSchemaError(RuntimeError):
    """Raised when a workspace_proposals / proposal_events write fails
    because the schema on THIS backend is only partially migrated (a missing
    table or column). Distinct from ``ValueError`` (bad caller input / an
    illegal state transition): this means the write itself could not
    complete safely. Any statements the operation already ran are
    compensated/rolled back before this is raised — see
    ``_undo_proposal_writes`` — so the caller never observes a partial row.
    """


def _is_proposal_schema_drift_error(exc: BaseException) -> bool:
    """Heuristic: does ``exc`` look like a missing table/column rather than
    a genuine data or logic error?

    Matches sqlite3's ``no such table`` / ``no such column`` message shape
    and psycopg3's ``UndefinedColumn`` / ``UndefinedTable`` (the exception
    class name and/or "... does not exist" text both appear in ``str(exc)``).
    Pure string heuristic — never raises, never imports a driver module
    (this backend may not have psycopg installed at all, e.g. a SQLite-only
    test environment)."""
    msg = str(exc).lower()
    return (
        "no such table" in msg
        or "no such column" in msg
        or "undefinedcolumn" in msg
        or "undefinedtable" in msg
        or ("column" in msg and "does not exist" in msg)
        or ("relation" in msg and "does not exist" in msg)
    )


def _is_proposal_unique_violation(exc: BaseException) -> bool:
    """Heuristic: does ``exc`` look like a UNIQUE/duplicate-key violation?

    Matches sqlite3's ``UNIQUE constraint failed`` and psycopg3's
    ``UniqueViolation`` (``duplicate key value violates unique
    constraint``). Never raises."""
    msg = str(exc).lower()
    return "unique" in msg or "duplicate key" in msg


async def _undo_proposal_writes(
    db: aiosqlite.Connection,
    compensations: "list[tuple[str, tuple]]",
) -> None:
    """Best-effort cleanup for a failed multi-statement proposal write.

    Applies ``compensations`` — ``(sql, params)`` pairs, in order, using the
    same ``?``-placeholder convention as the rest of this module — to undo
    whatever THIS call already wrote before the failure. Pass ``[]`` when
    nothing has been written yet (e.g. the very first statement of an
    operation failed).

    Used identically on BOTH backends — this deliberately does NOT call
    ``db.rollback()``. On Postgres (autocommit=True) that call is already a
    no-op. On SQLite (aiosqlite, autocommit=False) it looked like the right
    thing at first, but a real ``asyncio.gather`` concurrency test
    (test_workspace_proposals.py) proved it isn't: SQLite has exactly ONE
    implicit transaction per connection, and this module's own concurrency
    tests share ONE connection across coroutines (mirroring how a
    self-hosted server can share one connection across concurrent MCP
    calls). A losing caller's ``db.rollback()`` discarded a DIFFERENT,
    still-in-flight caller's uncommitted work on the SAME connection —
    turning "exactly one winner" into "sometimes zero winners". Each
    compensation here instead targets only the row(s) THIS call created (by
    id), so it can never touch a concurrent sibling's uncommitted rows —
    safe to run on a shared connection, and still correct on Postgres where
    every statement is independently committed anyway.

    Never raises: a failure while undoing a partial write must not mask the
    original error that triggered the cleanup.
    """
    for comp_sql, comp_params in compensations:
        try:
            await db.execute(comp_sql, comp_params)
        except Exception:  # noqa: BLE001 -- best-effort; never mask the real error
            pass


async def _find_proposal_by_idempotency_key(
    db: aiosqlite.Connection, idempotency_key: str, tenant_id: str | None,
) -> dict[str, Any] | None:
    """Look up an existing proposal by ``(tenant_id, idempotency_key)``.

    Scoped via ``COALESCE(tenant_id, '')`` so a NULL ``tenant_id`` (self-host)
    normalizes the same way the unique index does (see
    ``_migrate_workspace_proposals`` / ``_migrate_pg_workspace_proposals``),
    giving self-host a real duplicate-prevention guarantee too instead of
    only the best-effort pre-check every other tenant-scoped helper in this
    module gets via ``_ws_tenant_clause``'s "NULL matches everything" rule.
    """
    async with db.execute(
        "SELECT * FROM workspace_proposals WHERE idempotency_key = ? "
        "AND COALESCE(tenant_id, '') = COALESCE(?, '')",
        (idempotency_key, tenant_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) if row is not None else None


async def _append_proposal_event(
    db: aiosqlite.Connection,
    proposal_id: str,
    event_type: str,
    content: str = "",
    *,
    payload: Any = None,
    actor: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Append one immutable proposal event without committing the transaction."""
    async with db.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence "
        "FROM proposal_events WHERE proposal_id = ?",
        (proposal_id,),
    ) as cur:
        row = await cur.fetchone()
    sequence = int((row["next_sequence"] if row is not None else 1) or 1)
    event_id = _new_id()
    if payload is not None and not isinstance(payload, str):
        payload = json.dumps(payload, sort_keys=True)
    await db.execute(
        "INSERT INTO proposal_events "
        "(id, proposal_id, tenant_id, sequence, event_type, content, payload, "
        "actor, session_id, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            proposal_id,
            tenant_id,
            sequence,
            event_type,
            content,
            payload,
            actor,
            session_id,
            source,
        ),
    )
    async with db.execute(
        "SELECT * FROM proposal_events WHERE id = ?", (event_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) or {
        "id": event_id,
        "proposal_id": proposal_id,
        "sequence": sequence,
        "event_type": event_type,
    }


async def add_workspace_proposal(
    db: aiosqlite.Connection,
    title: str,
    body: str,
    tags: str | None = None,
    tenant_id: str | None = None,
    actor: str | None = None,
    session_id: str | None = None,
    source: str | None = "workspace",
    family_id: str | None = None,
    idempotency_key: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Insert a workspace_proposals row with status='raw'.

    Workspace-scoped by ``tenant_id`` (like ``add_workspace_note``). These are
    human-authored flashes of insight — NOT auto-claimable by executors.

    a8afd8f9 — ``project_id`` (optional) scopes this proposal to a specific
    project: ``scope_type`` is stored as ``'project'`` when given, else the
    unchanged default ``'workspace'``. When given, the target project must
    already exist (:class:`ValueError` otherwise, checked BEFORE any write —
    mirrors the existing project-existence check in
    ``promote_workspace_proposal``). Every existing caller that omits
    ``project_id`` gets byte-for-byte the same row shape as before this
    parameter existed.

    6fb48898 — a kebab-cased ``slug`` and a short memorable ``nickname`` are
    auto-generated from the title, unique per tenant scope, mirroring the
    sprint_items slug/nickname pattern (ae87699d).

    867317f6 — ``idempotency_key`` makes repeat calls safe to retry. When
    given and a PRIOR call already created a proposal with the same
    ``(tenant_id, idempotency_key)`` pair (see the unique partial index in
    ``_migrate_workspace_proposals`` / ``_migrate_pg_workspace_proposals``),
    that existing row is returned UNCHANGED instead of inserting a second
    one — no duplicate row, no duplicate "created" event. A genuine
    concurrent race (two callers passing the pre-check before either
    commits) is caught via the backing UNIQUE constraint and resolved the
    same way: the loser re-fetches and returns the winner's row rather than
    raising.

    Atomic: the proposal insert and its "created" event are compensated
    together on failure so a partially-applied migration (or any other
    mid-operation error) on Postgres never leaves an orphan proposal row
    with no event history — see the module-level note above
    ``ProposalSchemaError`` for why Postgres needs this and SQLite doesn't.
    Raises :class:`ProposalSchemaError` when the failure looks like a
    missing table/column; re-raises the original exception otherwise.
    """
    if idempotency_key:
        existing = await _find_proposal_by_idempotency_key(
            db, idempotency_key, tenant_id
        )
        if existing is not None:
            return existing

    if project_id and await get_project(db, project_id) is None:
        raise ValueError(f"Project '{project_id}' not found")
    scope_type = "project" if project_id else "workspace"

    pid = _new_id()
    # 6fb48898 — derive human-readable secondary keys from the title.
    _slug = await _unique_proposal_slug(
        db, tenant_id, _sprint_item_slug_base(title)
    )
    _nickname = await _unique_proposal_nickname(
        db, tenant_id, _sprint_item_nickname_base(title, pid)
    )
    try:
        await db.execute(
            "INSERT INTO workspace_proposals "
            "(id, title, body, tags, tenant_id, family_id, slug, nickname, "
            "idempotency_key, scope_type, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, title, body, tags, tenant_id, family_id, _slug, _nickname,
             idempotency_key, scope_type, project_id),
        )
    except Exception as exc:  # noqa: BLE001 — classified below
        if idempotency_key and _is_proposal_unique_violation(exc):
            # Lost a create race against another caller using the same key —
            # the failed INSERT wrote nothing (SQLite rejects a UNIQUE
            # violation as a no-op statement, and Postgres never applied it
            # either), so there is nothing of ours to compensate — just hand
            # back the winner's row.
            winner = await _find_proposal_by_idempotency_key(
                db, idempotency_key, tenant_id
            )
            if winner is not None:
                return winner
        await _undo_proposal_writes(db, [])
        if _is_proposal_schema_drift_error(exc):
            raise ProposalSchemaError(
                "add_workspace_proposal aborted: workspace_proposals schema "
                f"looks mid-migration on this backend ({exc}). No row was "
                "created; run pending migrations and retry."
            ) from exc
        raise

    try:
        await _append_proposal_event(
            db,
            pid,
            "created",
            body,
            payload={"title": title, "tags": tags},
            actor=actor,
            session_id=session_id,
            source=source,
            tenant_id=tenant_id,
        )
    except Exception as exc:  # noqa: BLE001 — classified below
        # The proposal insert above already committed on Postgres — undo it
        # so a retry doesn't see a phantom proposal with zero event history.
        await _undo_proposal_writes(
            db, [("DELETE FROM workspace_proposals WHERE id = ?", (pid,))]
        )
        if _is_proposal_schema_drift_error(exc):
            raise ProposalSchemaError(
                "add_workspace_proposal aborted: proposal_events schema "
                f"looks mid-migration on this backend ({exc}). The proposal "
                "row was rolled back; run pending migrations and retry."
            ) from exc
        raise

    await db.commit()
    async with db.execute(
        "SELECT * FROM workspace_proposals WHERE id = ?", (pid,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) or {"id": pid}


async def append_proposal_update(
    db: aiosqlite.Connection,
    proposal_id: str,
    content: str,
    event_type: str = "update",
    *,
    payload: Any = None,
    actor: str | None = None,
    session_id: str | None = None,
    source: str | None = "workspace",
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Append a structured proposal update and return the new event.

    Proposal rows are intentionally never edited for evidence or decisions.
    Each update becomes a new event carrying optional structured payload and
    provenance, which makes interrupted investigations resumable.

    Tenant-scoped: returns ``None`` (no read, no write) when ``proposal_id``
    does not resolve under ``tenant_id``'s scope, so a caller cannot append
    an event to another tenant's proposal.

    Atomic: the event insert and the ``last_activity_at`` bump are
    compensated together on failure (see ``ProposalSchemaError``) so
    Postgres never leaves an event on file whose parent proposal's
    ``last_activity_at`` wasn't bumped to match.
    """
    if not event_type.strip():
        raise ValueError("Proposal event_type cannot be blank")
    scope, scope_params = _ws_tenant_clause(tenant_id)
    scope_sql = f" AND {scope}" if scope else ""
    async with db.execute(
        f"SELECT id, tenant_id FROM workspace_proposals WHERE id = ?{scope_sql}",
        [proposal_id, *scope_params],
    ) as cur:
        proposal = await cur.fetchone()
    if proposal is None:
        return None
    event_tenant_id = (
        tenant_id
        if tenant_id is not None
        else (proposal["tenant_id"] if proposal is not None else None)
    )
    try:
        event = await _append_proposal_event(
            db,
            proposal_id,
            event_type.strip(),
            content,
            payload=payload,
            actor=actor,
            session_id=session_id,
            source=source,
            tenant_id=event_tenant_id,
        )
    except Exception as exc:  # noqa: BLE001 — classified below
        await _undo_proposal_writes(db, [])
        if _is_proposal_schema_drift_error(exc):
            raise ProposalSchemaError(
                "append_proposal_update aborted: proposal_events schema "
                f"looks mid-migration on this backend ({exc}). No event was "
                "recorded; run pending migrations and retry."
            ) from exc
        raise

    try:
        await db.execute(
            "UPDATE workspace_proposals SET last_activity_at = datetime('now') "
            f"WHERE id = ?{scope_sql}",
            [proposal_id, *scope_params],
        )
    except Exception as exc:  # noqa: BLE001 — classified below
        # The event above already committed on Postgres — undo it so a
        # retry doesn't see a stray event with no matching activity bump.
        await _undo_proposal_writes(
            db, [("DELETE FROM proposal_events WHERE id = ?", (event["id"],))]
        )
        if _is_proposal_schema_drift_error(exc):
            raise ProposalSchemaError(
                "append_proposal_update aborted: workspace_proposals schema "
                f"looks mid-migration on this backend ({exc}). The new "
                "event was rolled back; run pending migrations and retry."
            ) from exc
        raise

    await db.commit()
    return event


async def get_workspace_proposals(
    db: aiosqlite.Connection,
    status: str | None = None,
    tag: str | None = None,
    tenant_id: str | None = None,
    limit: int = 20,
    offset: int = 0,
    family_id: str | None = None,
    sort_by: str = "activity",
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return a bounded page of workspace proposals, newest first.

    Optional filters: ``status`` (raw/investigating/paused/promoted/rejected,
    closed, or superseded) and
    ``tag`` (substring match). Scoped to ``tenant_id`` when provided.

    a8afd8f9 — ``project_id`` (optional), when given, restricts the result to
    proposals scoped to exactly that project (``project_id = ?``); other
    projects' rows AND workspace-global rows are excluded. Omitted (the
    default) preserves the unchanged prior behavior — every proposal
    matching the other filters regardless of scope.

    When ``status`` is omitted, defaults to "live" proposals only (raw +
    investigating) — terminal proposals (promoted/rejected) are excluded so
    the default view reflects what's actually still open. Pass
    ``status="all"`` to fetch every status, or an explicit status value to
    filter to just that one (including "promoted"/"rejected").

    ``limit`` defaults to 20 and is clamped to 1..100 so proposal bodies cannot
    produce an unbounded MCP response. ``offset`` is a zero-based page cursor
    and is clamped to zero or greater. Both parameters follow ``tenant_id`` to
    preserve the existing positional calling convention.

    Same-second rows use the immutable ``created_seq`` insertion key as a
    stable secondary sort order on both SQLite and PostgreSQL."""
    clauses: list[str] = []
    params: list[Any] = []
    if status and status != "all":
        clauses.append("status = ?")
        params.append(status)
    elif not status:
        placeholders = ", ".join("?" for _ in _LIVE_PROPOSAL_STATUSES)
        clauses.append(f"status IN ({placeholders})")
        params.extend(sorted(_LIVE_PROPOSAL_STATUSES))
    if tag:
        clauses.append("tags LIKE ?")
        params.append(f"%{tag}%")
    if family_id:
        clauses.append("family_id = ?")
        params.append(family_id)
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    scope, scope_params = _ws_tenant_clause(tenant_id)
    if scope:
        clauses.append(scope)
        params.extend(scope_params)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    params.extend((limit, offset))
    order_by = (
        "COALESCE(last_activity_at, created_at) DESC, created_seq DESC"
        if sort_by in {"activity", "last_activity"}
        else "created_at DESC, created_seq DESC"
    )
    async with db.execute(
        f"SELECT * FROM workspace_proposals{where} "
        f"ORDER BY {order_by} LIMIT ? OFFSET ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def advance_workspace_proposal_status(
    db: aiosqlite.Connection,
    proposal_id: str,
    new_status: str,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Transition a proposal to ``new_status`` following the enforced state machine.

    Allowed transitions::

        raw         → investigating | paused | rejected | closed | superseded
        investigating → promoted | paused | rejected | raw | closed | superseded
        paused      → investigating | raw | closed | superseded
        promoted    → (terminal — use promote_workspace_proposal)
        rejected    → raw
        closed      → raw (reopened)
        superseded  → (terminal)

    Returns the updated row, or None if not found / wrong tenant.
    Raises ``ValueError`` on an invalid or disallowed transition, INCLUDING
    the case where a concurrent caller already changed the proposal's status
    between this call's read and its write (867317f6 — the write is guarded
    by ``WHERE status = <the status this call observed>``, so a lost race
    reports a clear error instead of silently clobbering whatever the other
    caller just set).

    Atomic: the status update and its transition event are compensated
    together on failure — see ``ProposalSchemaError``."""
    if new_status not in _VALID_PROPOSAL_STATUSES:
        raise ValueError(
            f"Invalid proposal status '{new_status}'. "
            f"Valid: {sorted(_VALID_PROPOSAL_STATUSES)}"
        )
    scope, scope_params = _ws_tenant_clause(tenant_id)
    scope_sql = f" AND {scope}" if scope else ""
    async with db.execute(
        f"SELECT * FROM workspace_proposals WHERE id = ?{scope_sql}",
        [proposal_id, *scope_params],
    ) as cur:
        row = await cur.fetchone()
    proposal = _row_to_dict(row) if row is not None else None
    if not proposal:
        return None
    current = proposal["status"]
    allowed = _PROPOSAL_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        raise ValueError(
            f"Cannot transition proposal from '{current}' to '{new_status}'. "
            f"Allowed from '{current}': {sorted(allowed) or '(none)'}"
        )
    # 867317f6 — atomic from-state guard: the read above is a classic
    # read-then-write race (identical shape to fa3e3331 in sprint_items.py).
    # Re-check `status = current` in the WHERE clause so a concurrent
    # transition that landed between the read and here is never silently
    # overwritten.
    cursor = await db.execute(
        f"UPDATE workspace_proposals SET status = ?, updated_at = datetime('now'), "
        f"last_activity_at = datetime('now') WHERE id = ? AND status = ?{scope_sql}",
        [new_status, proposal_id, current, *scope_params],
    )
    if cursor.rowcount == 0:
        # Nothing to compensate — the guarded UPDATE affected zero rows, so
        # this call never wrote anything (see _undo_proposal_writes for why
        # a blanket rollback() would be actively wrong here: it would erase
        # a concurrent WINNER's still-uncommitted work on a shared connection).
        async with db.execute(
            f"SELECT status FROM workspace_proposals WHERE id = ?{scope_sql}",
            [proposal_id, *scope_params],
        ) as cur:
            raced_row = await cur.fetchone()
        raced_status = raced_row["status"] if raced_row is not None else None
        raise ValueError(
            f"Cannot transition proposal from '{current}' to '{new_status}': "
            f"another caller already changed its status to "
            f"{raced_status!r} before this transition could commit. "
            "Re-fetch the proposal before retrying."
        )
    event_type = "resumed" if current == "paused" and new_status in {
        "raw", "investigating"
    } else "status_changed"
    try:
        await _append_proposal_event(
            db,
            proposal_id,
            event_type,
            f"{current} -> {new_status}",
            payload={"from": current, "to": new_status},
            tenant_id=(tenant_id if tenant_id is not None else proposal.get("tenant_id")),
        )
    except Exception as exc:  # noqa: BLE001 — classified below
        # The guarded status UPDATE above already committed on Postgres —
        # restore the prior status/timestamps so this failure never leaves
        # the proposal "transitioned" with no matching event on file.
        await _undo_proposal_writes(
            db,
            [(
                "UPDATE workspace_proposals SET status = ?, "
                "updated_at = ?, last_activity_at = ? WHERE id = ?",
                (
                    current,
                    proposal.get("updated_at"),
                    proposal.get("last_activity_at"),
                    proposal_id,
                ),
            )],
        )
        if _is_proposal_schema_drift_error(exc):
            raise ProposalSchemaError(
                "advance_workspace_proposal_status aborted: proposal_events "
                f"schema looks mid-migration on this backend ({exc}). The "
                "status change was rolled back; run pending migrations and "
                "retry."
            ) from exc
        raise
    await db.commit()
    async with db.execute(
        f"SELECT * FROM workspace_proposals WHERE id = ?{scope_sql}",
        [proposal_id, *scope_params],
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def promote_workspace_proposal(
    db: aiosqlite.Connection,
    proposal_id: str,
    project_id: str,
    sprint_item_title: str | None = None,
    sprint_item_version: str | None = None,
    tenant_id: str | None = None,
    touches_resources: list[str] | None = None,
    infer_touches_resources: bool = False,
    file_github_issue: bool = False,
    allow_project_transfer: bool = False,
    transfer_reason: str | None = None,
) -> dict[str, Any]:
    """Promote a proposal to a real sprint item and link the two.

    Creates a sprint item under ``project_id`` (using the proposal's title by
    default, overrideable via ``sprint_item_title``). Sets the proposal's
    status to 'promoted' and records ``promoted_to_sprint_item_id``.

    Raises ``ValueError`` if the proposal is not found, wrong tenant, is not
    in 'raw' or 'investigating' state (cannot promote rejected/promoted), or
    if a concurrent caller promoted (or otherwise transitioned) the SAME
    proposal between this call's read and its write (867317f6 — the promote
    UPDATE is guarded by ``WHERE status IN ('raw','investigating')``, so a
    lost race never creates a second, orphaned sprint item for one proposal).

    a8afd8f9 — project-scope mismatch guard: when the proposal carries a
    non-null ``project_id`` (it was created project-scoped) that differs
    from the ``project_id`` this call is promoting into, promotion is
    rejected with ``ValueError`` UNLESS ``allow_project_transfer=True`` is
    passed together with a non-empty ``transfer_reason`` — the override is
    recorded in the "promoted" event's payload (a durable ``proposal_events``
    row) rather than silently allowed. A proposal with no ``project_id``
    (every row created before this column existed, or explicitly workspace-
    scoped) has nothing to compare against, so this check never fires for it
    — promotion behaves exactly as it did before this guard existed.

    Atomic: the sprint-item insert, the promote UPDATE, and the "promoted"
    event are compensated together on failure — see ``ProposalSchemaError``.
    """
    if allow_project_transfer and not (transfer_reason or "").strip():
        raise ValueError(
            "allow_project_transfer=True requires a non-empty transfer_reason"
        )
    scope, scope_params = _ws_tenant_clause(tenant_id)
    scope_sql = f" AND {scope}" if scope else ""
    async with db.execute(
        f"SELECT * FROM workspace_proposals WHERE id = ?{scope_sql}",
        [proposal_id, *scope_params],
    ) as cur:
        row = await cur.fetchone()
    proposal = _row_to_dict(row) if row is not None else None
    if not proposal:
        raise ValueError(f"Proposal '{proposal_id}' not found")
    current = proposal["status"]
    if current not in ("raw", "investigating"):
        raise ValueError(
            f"Cannot promote a proposal in status '{current}'. "
            "Only 'raw' or 'investigating' proposals can be promoted."
        )
    # a8afd8f9 — project-scope mismatch guard (see docstring). Checked before
    # any write, alongside the other pre-flight validations above.
    proposal_project_id = proposal.get("project_id")
    project_transfer: dict[str, Any] | None = None
    if proposal_project_id and proposal_project_id != project_id:
        if not allow_project_transfer:
            raise ValueError(
                f"Cannot promote proposal '{proposal_id}': it is scoped to "
                f"project '{proposal_project_id}', not '{project_id}'. Pass "
                "allow_project_transfer=True with a transfer_reason to "
                "promote it into a different project anyway."
            )
        project_transfer = {
            "from_project_id": proposal_project_id,
            "to_project_id": project_id,
            "reason": (transfer_reason or "").strip(),
        }
    # Verify the target project exists.
    project = await get_project(db, project_id)
    if project is None:
        raise ValueError(f"Project '{project_id}' not found")
    title = sprint_item_title or proposal["title"]
    version = sprint_item_version or "current"
    # a56f0951 — infer touches_resources from proposal content so the created
    # sprint item is not silently bare (same gap class as fba94f1a).
    proposal_body = proposal.get("body") or ""
    resource_candidates = touches_resources
    if resource_candidates is None and infer_touches_resources:
        resource_candidates = _infer_touches_resources_from_proposal(
            title, proposal_body
        )
    resources_json: str | None = None
    item_notes: str | None = None
    if resource_candidates:
        try:
            resources_json = serialize_touches_resources(resource_candidates)
        except Exception:  # noqa: BLE001 — never block promotion
            resources_json = None
    if not resources_json:
        # No file match from git history. Flag the item explicitly so it
        # doesn't ship with a silently-empty touches_resources — mirrors the
        # pattern used for under-specified items that need manual scoping.
        item_notes = (
            "[resource-scope:unset] touches_resources could not be inferred "
            "from this proposal's content. Update touches_resources manually "
            "before claiming (e.g. file:meridian/... or db:migrations)."
        )
    # Create the sprint item with inferred resources (or a note flagging the gap).
    si_id = _new_id()
    try:
        await db.execute(
            "INSERT INTO sprint_items "
            "(id, project_id, version, title, status, touches_resources, notes) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (si_id, project_id, version, title, resources_json, item_notes),
        )
    except Exception as exc:  # noqa: BLE001 — classified below
        await _undo_proposal_writes(db, [])
        if _is_proposal_schema_drift_error(exc):
            raise ProposalSchemaError(
                "promote_workspace_proposal aborted: sprint_items schema "
                f"looks mid-migration on this backend ({exc}). No sprint "
                "item was created and the proposal was not promoted; run "
                "pending migrations and retry."
            ) from exc
        raise

    # 867317f6 — atomic from-state guard: two concurrent promote calls for
    # the SAME proposal must not both succeed (that would create two sprint
    # items — a duplicate/idempotency violation — with only one recorded on
    # the proposal). Re-check `status IN ('raw','investigating')` in the
    # UPDATE's WHERE clause itself; a lost race compensates by deleting the
    # sprint item this call just inserted, rather than leaving an orphan.
    cursor = await db.execute(
        f"UPDATE workspace_proposals "
        f"SET status = 'promoted', promoted_to_sprint_item_id = ?, "
        f"updated_at = datetime('now'), last_activity_at = datetime('now') "
        f"WHERE id = ? AND status IN ('raw', 'investigating'){scope_sql}",
        [si_id, proposal_id, *scope_params],
    )
    if cursor.rowcount == 0:
        await _undo_proposal_writes(
            db, [("DELETE FROM sprint_items WHERE id = ?", (si_id,))]
        )
        async with db.execute(
            f"SELECT status, promoted_to_sprint_item_id FROM workspace_proposals "
            f"WHERE id = ?{scope_sql}",
            [proposal_id, *scope_params],
        ) as cur:
            raced_row = await cur.fetchone()
        raced_status = raced_row["status"] if raced_row is not None else None
        raise ValueError(
            f"Cannot promote proposal '{proposal_id}': another caller already "
            f"changed its status to {raced_status!r} before this promotion "
            "could commit. No duplicate sprint item was created."
        )

    try:
        _promoted_payload: dict[str, Any] = {
            "sprint_item_id": si_id, "touches_resources": resource_candidates,
        }
        if project_transfer is not None:
            # a8afd8f9 — durable audit trail for a cross-project promotion
            # override, recorded on the SAME "promoted" event rather than as
            # a separate write (keeps the existing atomic-compensation
            # machinery below unchanged).
            _promoted_payload["project_transfer"] = project_transfer
        await _append_proposal_event(
            db,
            proposal_id,
            "promoted",
            f"Promoted to sprint item {si_id}",
            payload=_promoted_payload,
            tenant_id=(tenant_id if tenant_id is not None else proposal.get("tenant_id")),
        )
    except Exception as exc:  # noqa: BLE001 — classified below
        # Both the sprint item insert and the guarded promote UPDATE above
        # already committed on Postgres — undo both so a retry doesn't see
        # a "promoted" proposal with no event and no working link.
        await _undo_proposal_writes(
            db,
            [
                (
                    "UPDATE workspace_proposals SET status = ?, "
                    "promoted_to_sprint_item_id = NULL, updated_at = ?, "
                    "last_activity_at = ? WHERE id = ?",
                    (
                        current,
                        proposal.get("updated_at"),
                        proposal.get("last_activity_at"),
                        proposal_id,
                    ),
                ),
                ("DELETE FROM sprint_items WHERE id = ?", (si_id,)),
            ],
        )
        if _is_proposal_schema_drift_error(exc):
            raise ProposalSchemaError(
                "promote_workspace_proposal aborted: proposal_events schema "
                f"looks mid-migration on this backend ({exc}). The "
                "promotion was rolled back; run pending migrations and "
                "retry."
            ) from exc
        raise

    await db.commit()
    async with db.execute(
        f"SELECT * FROM workspace_proposals WHERE id = ?{scope_sql}",
        [proposal_id, *scope_params],
    ) as cur:
        row = await cur.fetchone()
    promoted_proposal = _row_to_dict(row)

    # 6cdc5df3 — first-class proposal-to-evidence linkage. promoted_to_sprint_item_id
    # (set above) is a single free column that only ever holds ONE id and has no
    # query path of its own; this writes the SAME relationship as a durable,
    # typed, queryable row in proposal_evidence_links so "what's linked to
    # proposal X" (get_proposal_evidence) can find this sprint item, and so
    # every FUTURE evidence linked to the same proposal_id (a note, a finding,
    # a decision, another sprint item, an artifact) composes with it. Lazy
    # import: link_proposal_evidence lives in meridian.db.proposal_links,
    # imported onto meridian.db AFTER this module — same pattern as the
    # request_hitl lazy import below.
    evidence_link: dict[str, Any] | None = None
    try:
        from meridian.db import link_proposal_evidence  # noqa: PLC0415

        evidence_link = await link_proposal_evidence(
            db, project_id, proposal_id, "sprint_item", si_id,
            label=title, actor=tenant_id,
        )
    except Exception:  # noqa: BLE001 — promotion itself must never be blocked
        # by an evidence-linking failure; promoted_to_sprint_item_id above
        # already recorded the canonical single link regardless.
        evidence_link = None

    # 3999d90f — conditional proposal-to-GitHub-issue workflow via HITL. When
    # the promoted proposal is code-related (the same inferred-resources
    # signal used for touches_resources above) AND the target project has a
    # GitHub repo connected, ask a human whether to also file a GitHub issue
    # for it. Fire-and-forget: request_hitl either auto-answers (safe/normal
    # mode) or sits pending for a human. Either way the answer is consumed by
    # _on_hitl_answered's 'proposal_github_issue' handler, which files the
    # issue via the GitHub tool and calls set_proposal_github_issue to store
    # the number/URL back on this proposal — never done inline here, since
    # this module has no GitHub API / tenant-PAT access.
    github_issue_hitl: dict[str, Any] | None = None
    github_repo = (project.get("github_repo") or "").strip()
    if file_github_issue and github_repo:
        # Lazy import: request_hitl lives in meridian.db (defined after this
        # module is imported by it) — see the identical pattern in
        # sprint_items.py's _maybe_file_chain_handoff.
        from meridian.db import request_hitl  # noqa: PLC0415

        yes_option = "Yes — file a GitHub issue"
        no_option = "No — skip"
        github_issue_hitl = await request_hitl(
            db, project_id,
            question=(
                f"Proposal '{title}' was promoted to sprint item {si_id} and looks "
                f"code-related (matched files in {github_repo}). Also file a GitHub issue for it?"
            ),
            context=(
                f"Proposal {proposal_id} -> sprint item {si_id}. "
                f"Repo: {github_repo}. If yes, an issue titled {title!r} is filed "
                f"and its number/URL are stored back on the proposal."
            ),
            urgency="normal",
            kind="proposal_github_issue",
            options=[yes_option, no_option],
            recommended=yes_option,
            payload=json.dumps({
                "proposal_id": proposal_id,
                "sprint_item_id": si_id,
                "project_id": project_id,
                "github_repo": github_repo,
                "issue_title": title,
                "issue_body": (
                    f"{proposal_body}\n\n---\nFiled from Meridian proposal "
                    f"{proposal_id} / sprint item {si_id}."
                ),
            }),
        )

    return {
        "proposal": promoted_proposal,
        "sprint_item_id": si_id,
        "sprint_item_title": title,
        "project_id": project_id,
        "sprint_item_touches_resources": resources_json,
        "sprint_item_notes": item_notes,
        "github_issue_hitl": github_issue_hitl,
        # 6cdc5df3 — the durable proposal_evidence_links row for this
        # promotion, or None if evidence-linking failed (never blocks promotion).
        "evidence_link": evidence_link,
    }


# ---------------------------------------------------------------------------
# 3f892ea6 — deterministic proposal intake blocks: provenance-preserving
# block parsing, idempotent ingest, and explicit sprint promotion.
#
# Distinct from promote_workspace_proposal above (which promotes the WHOLE
# proposal body, verbatim, as one sprint item): this pipeline splits a
# proposal's body into individually-addressable BLOCKS (paragraphs, with
# fenced/triple-quoted code kept intact as one block), attaches deterministic
# provenance (exact text, source line range, a sha256 content hash, and a
# stable per-(proposal, block) intake_key) to each, classifies each block's
# review route from an explicit marker tag, and lets a human promote
# individual blocks to sprint items one at a time — never automatically.
#
# parse_proposal_intake_blocks is a PURE function (no DB) so its provenance
# guarantees (exact text, byte-identical hash, deterministic key) are trivial
# to test in isolation. ingest_proposal_intake/get_proposal_intake_drafts/
# promote_intake_draft persist and act on its output.
# ---------------------------------------------------------------------------

# Recognized intake markers -> the review route a block is queued for. A
# block with no marker prefix (or an unrecognized bracketed tag, e.g.
# "[TODO]") is deliberately left unresolved (route=None) rather than guessed
# — an intake block only gets routed when a human/producer explicitly says so.
_INTAKE_MARKER_ROUTES: dict[str, str] = {
    "[MERIDIAN-DOCS]": "meridian_docs_review",
    "[SERENA]": "meridian_code_review",
    "[MERIDIAN-OUTPUTS]": "meridian_outputs_review",
    "[HUMAN]": "human_decision_review",
    "[SPRINT]": "sprint_item_review",
}

# Fenced/triple-quoted live-code spans (```...``` or '''...''') are excluded
# from task creation — a block is code and never gets a route or a promoted
# sprint item, but its exact text/hash/lines are still preserved for provenance.
_INTAKE_FENCE_OPENERS = ("```", "'''")

_INTAKE_CANDIDATE_ID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def parse_proposal_intake_blocks(proposal_id: str, body: str) -> list[dict[str, Any]]:
    """3f892ea6 — split a proposal body into deterministic, provenance-carrying
    blocks. Pure function: same (proposal_id, body) always yields identical
    output, no DB access.

    A block is a run of non-blank lines separated from its neighbours by one
    or more blank lines, EXCEPT that a fenced (```) or triple-quoted (''')
    span is treated as ONE block from its opening marker line through its
    closing marker line even if it contains blank lines internally — never
    split, and always flagged ``is_code=True`` (excluded from routing and
    candidate-id extraction downstream by ingest_proposal_intake).

    Each returned dict has:
      block_id         "b1", "b2", ... — 1-indexed, positional across both
                        code and non-code blocks.
      text              Exact block text (original lines rejoined with "\\n",
                        no leading/trailing blank lines) — the provenance
                        anchor for source_hash/line_start/line_end below.
      line_start/line_end  1-indexed, inclusive physical line range in body.
      source_hash        sha256 hex digest of ``text`` (utf-8).
      intake_key         sha256 hex digest of ``f"{proposal_id}::{block_id}"``
                        — deterministic for the SAME (proposal_id, block
                        position) across repeated parses of the SAME or a
                        revised body, but distinct across different
                        proposals. This is the stable identity ingest uses
                        to detect "same block position, body changed" vs.
                        "brand new block".
      route              One of the _INTAKE_MARKER_ROUTES values, or None
                        when the block has no recognized marker (or is code).
      candidate_ids       UUID-shaped substrings found in the block text.
      is_code             True for a fenced/triple-quoted span.
    """
    lines = (body or "").split("\n")
    blocks: list[dict[str, Any]] = []
    current_lines: list[str] = []
    current_start = 0
    in_fence = False
    fence_close = "```"

    def _finalize(end_line: int, is_code: bool) -> None:
        nonlocal current_lines
        if not current_lines:
            return
        text = "\n".join(current_lines)
        block_id = f"b{len(blocks) + 1}"
        stripped = text.strip()
        route: str | None = None
        if not is_code:
            for marker, route_name in _INTAKE_MARKER_ROUTES.items():
                if stripped.startswith(marker):
                    route = route_name
                    break
        blocks.append({
            "block_id": block_id,
            "text": text,
            "line_start": current_start,
            "line_end": end_line,
            "source_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "intake_key": hashlib.sha256(
                f"{proposal_id}::{block_id}".encode("utf-8")
            ).hexdigest(),
            "route": route,
            "candidate_ids": _INTAKE_CANDIDATE_ID_RE.findall(text),
            "is_code": is_code,
        })
        current_lines = []

    for idx, raw_line in enumerate(lines):
        line_no = idx + 1
        if in_fence:
            current_lines.append(raw_line)
            if raw_line.strip() == fence_close:
                in_fence = False
                _finalize(line_no, is_code=True)
            continue

        stripped_line = raw_line.strip()
        if stripped_line == "":
            if current_lines:
                _finalize(line_no - 1, is_code=False)
            continue

        if not current_lines:
            current_start = line_no
            if stripped_line.startswith(_INTAKE_FENCE_OPENERS):
                in_fence = True
                fence_close = "```" if stripped_line.startswith("```") else "'''"
        current_lines.append(raw_line)

    if current_lines:
        _finalize(len(lines), is_code=in_fence)

    return blocks


def _derive_intake_draft_title(text: str) -> str:
    """Derive a short sprint-item title from a promoted block's raw text:
    strip a leading marker tag, collapse to a single line, cap length."""
    stripped = (text or "").strip()
    for marker in _INTAKE_MARKER_ROUTES:
        if stripped.startswith(marker):
            stripped = stripped[len(marker):].strip()
            break
    first_line = stripped.splitlines()[0] if stripped else ""
    single_line = " ".join(first_line.split())
    return single_line[:200] or "Untitled proposal intake block"


async def _migrate_proposal_intake_drafts(db: aiosqlite.Connection) -> None:
    """3f892ea6 — proposal_intake_drafts: one row per parsed, non-code intake
    block, keyed on (proposal_id, block_id). Not present in the base
    CREATE_TABLES literal — this guarded migration is the only creation
    path, for either a fresh or an existing DB (2026-07-04 inline-index
    outage rule: no inline CREATE INDEX in the unguarded base schema).
    Mirrors ``pg_adapter._migrate_pg_proposal_intake_drafts``.

    ``position`` is a plain integer (parsed from the numeric suffix of
    ``block_id``) used purely for deterministic ORDER BY — block ids are
    NOT lexicographically sortable ("b10" < "b2" as strings) and row ids
    are random UUIDs, so neither can be used as an ordering key.

    The UNIQUE index on (proposal_id, block_id) is what makes
    ingest_proposal_intake's upsert-by-position logic correct: a second
    ingest of the same body updates the SAME row in place instead of
    inserting a duplicate.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS proposal_intake_drafts (
            id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            tenant_id TEXT,
            block_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            intake_key TEXT NOT NULL,
            text TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            route TEXT,
            candidate_ids TEXT NOT NULL DEFAULT '[]',
            is_code INTEGER NOT NULL DEFAULT 0,
            is_duplicate INTEGER NOT NULL DEFAULT 0,
            duplicate_of_block_id TEXT,
            revision INTEGER NOT NULL DEFAULT 1,
            history TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'draft',
            line_start INTEGER,
            line_end INTEGER,
            promoted_to_sprint_item_id TEXT,
            promoted_to_project_id TEXT,
            promoted_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_proposal_intake_drafts_block "
        "ON proposal_intake_drafts(proposal_id, block_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_proposal_intake_drafts_position "
        "ON proposal_intake_drafts(proposal_id, position)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_proposal_intake_drafts_promoted "
        "ON proposal_intake_drafts(promoted_to_sprint_item_id)"
    )
    await db.commit()


async def ingest_proposal_intake(
    db: aiosqlite.Connection, proposal_id: str,
) -> dict[str, Any]:
    """3f892ea6 — parse a proposal's CURRENT body into blocks and upsert one
    draft row per non-code block. NEVER creates a sprint item — ingest only
    ever produces drafts; promotion is always a separate, explicit step
    (see promote_intake_draft).

    Idempotent: re-ingesting an unchanged body updates nothing (block ids
    land in ``unchanged``). A block whose text changed since the last ingest
    is updated IN PLACE (same draft id, ``revision`` bumped, prior text
    appended to ``history``) rather than creating a second row — block
    identity is (proposal_id, block_id), not source_hash. A code block is
    parsed for provenance but never persisted as a draft (its block_id lands
    in ``excluded_code`` instead). Duplicate detection is scoped to this
    ingest's own non-code blocks: the FIRST occurrence of a given exact text
    is canonical, later occurrences are flagged ``is_duplicate`` and cannot
    be promoted (see promote_intake_draft).

    Raises ``ValueError`` if the proposal does not exist.

    Returns ``{"drafts": [...], "created": [...], "updated": [...],
    "unchanged": [...], "duplicates": [...], "excluded_code": [...]}`` —
    the block_id lists reflect what THIS call did; ``drafts`` is the full,
    ordered set of draft rows touched by this ingest.
    """
    async with db.execute(
        "SELECT * FROM workspace_proposals WHERE id = ?", (proposal_id,)
    ) as cur:
        row = await cur.fetchone()
    proposal = _row_to_dict(row)
    if proposal is None:
        raise ValueError(f"Proposal '{proposal_id}' not found")

    blocks = parse_proposal_intake_blocks(proposal_id, proposal.get("body") or "")

    # First occurrence of an exact text among this ingest's non-code blocks
    # is canonical; later ones are duplicates of it.
    seen_hashes: dict[str, str] = {}
    duplicate_of: dict[str, str] = {}
    for block in blocks:
        if block["is_code"]:
            continue
        h = block["source_hash"]
        if h in seen_hashes:
            duplicate_of[block["block_id"]] = seen_hashes[h]
        else:
            seen_hashes[h] = block["block_id"]

    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    duplicates: list[str] = []
    excluded_code: list[str] = []

    for block in blocks:
        block_id = block["block_id"]
        if block["is_code"]:
            excluded_code.append(block_id)
            continue

        is_duplicate = block_id in duplicate_of
        duplicate_of_block_id = duplicate_of.get(block_id)
        if is_duplicate:
            duplicates.append(block_id)

        async with db.execute(
            "SELECT * FROM proposal_intake_drafts "
            "WHERE proposal_id = ? AND block_id = ?",
            (proposal_id, block_id),
        ) as cur:
            existing_row = await cur.fetchone()
        existing = _row_to_dict(existing_row)

        candidate_ids_json = json.dumps(block["candidate_ids"])
        position = int(block_id[1:])

        if existing is None:
            draft_id = _new_id()
            await db.execute(
                "INSERT INTO proposal_intake_drafts "
                "(id, proposal_id, tenant_id, block_id, position, intake_key, "
                "text, source_hash, route, candidate_ids, is_code, "
                "is_duplicate, duplicate_of_block_id, revision, history, "
                "status, line_start, line_end) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 1, '[]', "
                "'draft', ?, ?)",
                (
                    draft_id, proposal_id, proposal.get("tenant_id"), block_id,
                    position, block["intake_key"], block["text"],
                    block["source_hash"], block["route"], candidate_ids_json,
                    1 if is_duplicate else 0, duplicate_of_block_id,
                    block["line_start"], block["line_end"],
                ),
            )
            created.append(block_id)
        elif existing["source_hash"] == block["source_hash"]:
            await db.execute(
                "UPDATE proposal_intake_drafts SET route = ?, "
                "candidate_ids = ?, is_duplicate = ?, "
                "duplicate_of_block_id = ?, line_start = ?, line_end = ? "
                "WHERE id = ?",
                (
                    block["route"], candidate_ids_json, 1 if is_duplicate else 0,
                    duplicate_of_block_id, block["line_start"], block["line_end"],
                    existing["id"],
                ),
            )
            unchanged.append(block_id)
        else:
            history = json.loads(existing.get("history") or "[]")
            history.append({
                "text": existing["text"],
                "source_hash": existing["source_hash"],
                "revision": existing["revision"],
                "replaced_at": existing.get("updated_at"),
            })
            await db.execute(
                "UPDATE proposal_intake_drafts SET text = ?, source_hash = ?, "
                "route = ?, candidate_ids = ?, is_duplicate = ?, "
                "duplicate_of_block_id = ?, line_start = ?, line_end = ?, "
                "revision = revision + 1, history = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (
                    block["text"], block["source_hash"], block["route"],
                    candidate_ids_json, 1 if is_duplicate else 0,
                    duplicate_of_block_id, block["line_start"], block["line_end"],
                    json.dumps(history), existing["id"],
                ),
            )
            updated.append(block_id)

    await db.commit()

    all_drafts = await get_proposal_intake_drafts(db, proposal_id)
    touched = set(created) | set(updated) | set(unchanged)
    result_drafts = [d for d in all_drafts if d["block_id"] in touched]

    return {
        "drafts": result_drafts,
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "duplicates": duplicates,
        "excluded_code": excluded_code,
    }


async def get_proposal_intake_drafts(
    db: aiosqlite.Connection, proposal_id: str,
) -> list[dict[str, Any]]:
    """3f892ea6 — every intake draft for one proposal, in deterministic
    block-position order (ORDER BY the numeric ``position`` column, NOT the
    lexicographic ``block_id`` string — "b10" must sort after "b2", not
    before it). ``candidate_ids``/``history`` are decoded back to Python
    lists.
    """
    async with db.execute(
        "SELECT * FROM proposal_intake_drafts WHERE proposal_id = ? "
        "ORDER BY position ASC",
        (proposal_id,),
    ) as cur:
        rows = await cur.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = _row_to_dict(r)
        if d is None:
            continue
        try:
            d["candidate_ids"] = json.loads(d.get("candidate_ids") or "[]")
        except (TypeError, ValueError):
            d["candidate_ids"] = []
        try:
            d["history"] = json.loads(d.get("history") or "[]")
        except (TypeError, ValueError):
            d["history"] = []
        out.append(d)
    return out


async def promote_intake_draft(
    db: aiosqlite.Connection,
    draft_id: str,
    project_id: str,
    title: str | None = None,
    version: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """3f892ea6 — explicitly promote ONE intake draft to a real sprint item.

    Never automatic (ingest_proposal_intake never calls this). Raises
    ``ValueError`` when: the draft does not exist; it was already promoted
    (message contains "already promoted"); it is flagged ``is_duplicate``
    (message contains "duplicate" — promote the canonical block instead);
    or ``project_id`` does not name a real project (message contains "not
    found").

    Durable backlink: records ``promoted_to_sprint_item_id`` /
    ``promoted_to_project_id`` / ``promoted_at`` on the draft row AND writes
    a ``proposal_evidence_links`` row (proposal -> sprint_item, scoped to
    ``project_id``) via ``link_proposal_evidence`` — mirrors
    ``promote_workspace_proposal``'s identical evidence-linking step.
    Evidence-linking failure never blocks the promotion itself (the
    draft-row backlink above is the single source of truth either way).

    Two DIFFERENT drafts of the SAME proposal may be promoted into two
    DIFFERENT projects; each lands as a fully separate, correctly-scoped
    sprint item (project_id is never inferred or shared across calls).
    """
    async with db.execute(
        "SELECT * FROM proposal_intake_drafts WHERE id = ?", (draft_id,)
    ) as cur:
        row = await cur.fetchone()
    draft = _row_to_dict(row)
    if draft is None:
        raise ValueError(f"Proposal intake draft '{draft_id}' not found")
    if draft.get("status") == "promoted":
        raise ValueError(
            f"Proposal intake draft '{draft_id}' is already promoted to "
            f"sprint item '{draft.get('promoted_to_sprint_item_id')}'"
        )
    if draft.get("is_duplicate"):
        raise ValueError(
            f"Proposal intake draft '{draft_id}' is a duplicate of block "
            f"'{draft.get('duplicate_of_block_id')}' and cannot be promoted "
            "directly — promote the canonical block instead."
        )

    project = await get_project(db, project_id)
    if project is None:
        raise ValueError(f"Project '{project_id}' not found")

    item_title = title or _derive_intake_draft_title(draft["text"])
    item_version = version or "current"

    si_id = _new_id()
    await db.execute(
        "INSERT INTO sprint_items "
        "(id, project_id, version, title, status, notes) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        (si_id, project_id, item_version, item_title, draft["text"]),
    )

    # 867317f6-style atomic from-state guard: a concurrent promote of the
    # SAME draft must not both succeed. A lost race compensates by deleting
    # the sprint item this call just inserted rather than leaving an orphan.
    cursor = await db.execute(
        "UPDATE proposal_intake_drafts SET status = 'promoted', "
        "promoted_to_sprint_item_id = ?, promoted_to_project_id = ?, "
        "promoted_at = datetime('now'), updated_at = datetime('now') "
        "WHERE id = ? AND status != 'promoted'",
        (si_id, project_id, draft_id),
    )
    if cursor.rowcount == 0:
        await db.execute("DELETE FROM sprint_items WHERE id = ?", (si_id,))
        await db.commit()
        raise ValueError(
            f"Proposal intake draft '{draft_id}' was promoted by another "
            "caller before this promotion could commit."
        )
    await db.commit()

    # Lazy import: link_proposal_evidence lives in meridian.db.proposal_links,
    # imported onto meridian.db AFTER this module — same pattern as
    # promote_workspace_proposal's identical lazy import above.
    evidence_link: dict[str, Any] | None = None
    try:
        from meridian.db import link_proposal_evidence  # noqa: PLC0415

        evidence_link = await link_proposal_evidence(
            db, project_id, draft["proposal_id"], "sprint_item", si_id,
            label=item_title, actor=actor,
        )
    except Exception:  # noqa: BLE001 — promotion itself must never be
        # blocked by an evidence-linking failure; the draft row's own
        # promoted_to_sprint_item_id above already recorded the canonical link.
        evidence_link = None

    return {
        "sprint_item_id": si_id,
        "draft_id": draft_id,
        "project_id": project_id,
        "sprint_item_title": item_title,
        "evidence_link": evidence_link,
    }


async def set_proposal_github_issue(
    db: aiosqlite.Connection,
    proposal_id: str,
    issue_number: int | None,
    issue_url: str | None,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """3999d90f — persist a filed GitHub issue's number/URL back onto a proposal.

    The write side of the conditional proposal-to-GitHub-issue HITL workflow:
    called by _on_hitl_answered (meridian/server.py) once a
    ``kind='proposal_github_issue'`` HITL is answered affirmatively and the
    issue has been created via the GitHub tool. Returns the updated proposal,
    or None if not found / wrong tenant scope."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    scope_sql = f" AND {scope}" if scope else ""
    await db.execute(
        f"UPDATE workspace_proposals SET github_issue_number = ?, "
        f"github_issue_url = ?, updated_at = datetime('now') "
        f"WHERE id = ?{scope_sql}",
        [issue_number, issue_url, proposal_id, *scope_params],
    )
    await db.commit()
    async with db.execute(
        f"SELECT * FROM workspace_proposals WHERE id = ?{scope_sql}",
        [proposal_id, *scope_params],
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def delete_workspace_proposal(
    db: aiosqlite.Connection, proposal_id: str, tenant_id: str | None = None
) -> bool:
    """Hard-delete a workspace proposal. Returns True if a row was removed."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    sql = "DELETE FROM workspace_proposals WHERE id = ?" + (f" AND {scope}" if scope else "")
    async with db.execute(sql, [proposal_id, *scope_params]) as cur:
        rc = cur.rowcount or 0
    await db.commit()
    return rc > 0


# --- Workspace sprint board (tenant-global personal backlog) ----------------
# A cross-project backlog that is NOT tied to any single project. Mirrors the
# useful subset of the per-project sprint_items shape but is keyed by tenant_id
# (see _ws_tenant_clause), exactly like workspace_notes / workspace_decisions.
# ``item_group`` is the cross-project bucket ('thesis'/'meridian'/'personal').

_VALID_WS_SPRINT_STATUSES = {
    "todo", "pending", "in_progress", "done", "skipped", "failed",
}


async def add_workspace_sprint_item(
    db: aiosqlite.Connection,
    title: str,
    item_group: str | None = None,
    human_id: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Append a ``todo`` item to the workspace-level personal backlog.

    ``item_group`` is the cross-project bucket the item lives under (e.g.
    'thesis' / 'meridian' / 'personal'); ``human_id`` attributes it to a
    person. Workspace sprint items belong to the whole workspace, not a single
    project, so there is no project_id. Scoped to ``tenant_id`` when provided
    (hosted); None on self-host."""
    iid = _new_id()
    # New items go to the end of their group (highest position + 1).
    scope, scope_params = _ws_tenant_clause(tenant_id)
    where = f" WHERE {scope}" if scope else ""
    async with db.execute(
        f"SELECT COALESCE(MAX(position), -1) + 1 AS next_pos "
        f"FROM workspace_sprint_items{where}",
        scope_params or None,
    ) as cur:
        prow = await cur.fetchone()
    next_pos = (prow["next_pos"] if isinstance(prow, dict) else prow[0]) or 0
    await db.execute(
        "INSERT INTO workspace_sprint_items "
        "(id, tenant_id, title, item_group, human_id, position) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (iid, tenant_id, title, item_group or None, human_id or None, next_pos),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM workspace_sprint_items WHERE id = ?", (iid,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) or {"id": iid}


async def get_workspace_sprint_items(
    db: aiosqlite.Connection,
    status: str | None = None,
    item_group: str | None = None,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """List workspace sprint items, grouped by ``item_group`` then position.

    Optional ``status`` and ``item_group`` filters. Scoped to ``tenant_id``
    when provided (hosted); None returns everything on self-host."""
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        if status not in _VALID_WS_SPRINT_STATUSES:
            raise ValueError(
                f"invalid workspace sprint-item status filter: {status!r}. "
                f"Valid: {sorted(_VALID_WS_SPRINT_STATUSES)}"
            )
        clauses.append("status = ?")
        params.append(status)
    if item_group is not None:
        clauses.append("item_group = ?")
        params.append(item_group)
    scope, scope_params = _ws_tenant_clause(tenant_id)
    if scope:
        clauses.append(scope)
        params.extend(scope_params)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    async with db.execute(
        f"SELECT * FROM workspace_sprint_items{where} "
        "ORDER BY item_group IS NULL, item_group ASC, position ASC, created_at ASC",
        params or None,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def update_workspace_sprint_item(
    db: aiosqlite.Connection,
    item_id: str,
    title: str | None = None,
    status: str | None = None,
    item_group: str | None = None,
    human_id: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Patch editable fields of a workspace sprint item (tenant-scoped).

    Editable: title, status, item_group, human_id. Only fields passed as
    non-None are changed. Pass an empty string to clear item_group/human_id.
    A terminal status of 'done'/'skipped'/'failed' stamps ``completed_at``;
    any other status clears it. Returns the updated item, or None if no row
    matched (unknown id, or another tenant's item)."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    scope_sql = f" AND {scope}" if scope else ""
    # workspace_sprint_items.completed_at/updated_at are TIMESTAMPTZ on Postgres;
    # see update_agent_task_status for why the shared datetime('now') form breaks
    # there (adapter-rewritten to a text-typed to_char(...) expression).
    now_expr = "now()" if hasattr(db, "_pool") else "datetime('now')"
    fields: list[str] = []
    values: list[Any] = []
    if title is not None:
        fields.append("title = ?")
        values.append(title)
    if status is not None:
        if status not in _VALID_WS_SPRINT_STATUSES:
            raise ValueError(f"invalid workspace sprint-item status: {status!r}")
        fields.append("status = ?")
        values.append(status)
        if status in {"done", "skipped", "failed"}:
            fields.append(f"completed_at = {now_expr}")
        else:
            fields.append("completed_at = NULL")
    if item_group is not None:
        fields.append("item_group = ?")
        values.append(item_group or None)
    if human_id is not None:
        fields.append("human_id = ?")
        values.append(human_id or None)
    if not fields:
        async with db.execute(
            f"SELECT * FROM workspace_sprint_items WHERE id = ?{scope_sql}",
            [item_id, *scope_params],
        ) as cur:
            row = await cur.fetchone()
        return _row_to_dict(row)
    fields.append(f"updated_at = {now_expr}")
    cursor = await db.execute(
        f"UPDATE workspace_sprint_items SET {', '.join(fields)} "
        f"WHERE id = ?{scope_sql}",
        [*values, item_id, *scope_params],
    )
    await db.commit()
    if cursor.rowcount == 0:
        return None
    async with db.execute(
        f"SELECT * FROM workspace_sprint_items WHERE id = ?{scope_sql}",
        [item_id, *scope_params],
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def complete_workspace_sprint_item(
    db: aiosqlite.Connection,
    item_id: str,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Mark a workspace sprint item ``done`` (stamps ``completed_at``).

    Returns the updated item, or None if no row matched (unknown id / wrong
    tenant)."""
    return await update_workspace_sprint_item(
        db, item_id, status="done", tenant_id=tenant_id
    )


_WORKSPACE_SETTINGS_ID = "singleton"

# 4ef6ce5e — claim_verification_mode: does a PostToolUse hook re-check
# claim_sprint_item/complete_sprint_item calls against live DB state before
# trusting the calling session's own narration? See
# db.migrations._migrate_workspace_claim_verification_mode for the full
# incident/design writeup; meridian/claim_verify.py is the hook's actual
# comparison logic; handoff.seed_claim_verification_hook wires it into
# .claude/hooks/ via the existing custom_hooks (273287cb) infra.
_VALID_CLAIM_VERIFICATION_MODES: frozenset[str] = frozenset({"off", "advisory", "strict"})

# 47ac68a0 — cap on the raw handoff_template value at WRITE time. Every
# block it interpolates at render time is already individually bounded
# (recent_tasks[:10], decisions[:10], pending_items[:30], notes[:10] — see
# handoff._render_custom_handoff), but the template string itself had no
# length bound anywhere on the write path (this function and the
# /workspace/settings PATCH route both did only `.strip() or None`). An
# arbitrarily large template was therefore persisted unbounded to
# workspace_settings and re-rendered unbounded into every full-mode
# handoff's `content` — format_handoff_mcp_content's wire-level max_bytes
# budget still caps what MCP itself returns, but generate_handoff's on-disk
# file write and the handoffs table/pending_goal persistence are explicitly
# NEVER bounded by that budget (see generate_handoff's own docstring), so an
# oversized template was stored and re-served unbounded outside the wire
# layer. 50_000 chars is generous for a genuine custom template (the
# rendered full-mode default handoff itself typically runs well under this)
# while still ruling out unbounded growth.
_HANDOFF_TEMPLATE_MAX_CHARS = 50_000


def _ws_settings_key(tenant_id: str | None) -> str:
    """Row key for the workspace_settings singleton.

    Hosted callers key by their tenant_id so internal accounts that share the
    control-plane DB get isolated settings; self-host keeps the legacy
    'singleton' row.
    """
    return tenant_id or _WORKSPACE_SETTINGS_ID


async def get_workspace_settings(
    db: aiosqlite.Connection, tenant_id: str | None = None
) -> dict[str, Any]:
    """Return the workspace-level settings (tenant-global defaults).

    Always returns a dict — defaults when no row has been written yet — so
    callers never have to None-check. One row per tenant (or the legacy
    'singleton' row in self-host).
    """
    _cols = (
        "SELECT hitl_auto_answer_default, sprint_name_default, display_name, "
        "log_task_sprint_nudge_threshold, handoff_template, "
        "execution_mode_default, code_intel_enabled_default, "
        "loop_enabled_default, auto_refresh_enabled, refresh_interval_turns, "
        "refresh_triggers, refresh_trigger_min_interval, handoff_inline_pointers, "
        "active_session_warning_minutes, manual_issue_screening_enabled, "
        "tool_priority_map, claim_verification_mode, updated_at "
        "FROM workspace_settings"
    )
    async with db.execute(
        f"{_cols} WHERE id = ?", (_ws_settings_key(tenant_id),)
    ) as cur:
        row = await cur.fetchone()
    if row is None and tenant_id is None:
        # Internal/self-host caller with no tenant context. On a dedicated
        # per-tenant DB the sole settings row is keyed by that tenant's id, so
        # fall back to it when there is exactly one row. The shared control-plane
        # DB has many rows, so this safely no-ops there (returns defaults).
        async with db.execute(f"{_cols} LIMIT 2") as cur:
            some = await cur.fetchall()
        if len(some) == 1:
            row = some[0]
    data = _row_to_dict(row) or {}
    # 0bf67524 — cascade defaults. None ⇒ "no workspace default set" (new
    # projects keep their own built-in default); a value ⇒ seed new projects.
    _emode = data.get("execution_mode_default")
    _ci_default = data.get("code_intel_enabled_default")
    # 76cf8bda — /loop auto-continue workspace default. Missing column/row ⇒
    # True (Meridian sessions default to auto-continue); a stored 0 turns it off.
    _loop_default = data.get("loop_enabled_default")
    # bf51b12e — planner context-refresh config. refresh_triggers is a JSON list
    # (NULL ⇒ None ⇒ hook uses its built-in default trigger set).
    _refresh_triggers_raw = data.get("refresh_triggers")
    _refresh_triggers: list[str] | None = None
    if _refresh_triggers_raw:
        try:
            _decoded = json.loads(_refresh_triggers_raw)
            if isinstance(_decoded, list):
                _refresh_triggers = [str(t) for t in _decoded]
        except Exception:  # noqa: BLE001 — malformed row ⇒ fall back to default
            _refresh_triggers = None
    _interval = data.get("refresh_interval_turns")
    # db0361bb — separate, smaller floor (in calls) that gates the
    # TRIGGER-branch nudge specifically (distinct from refresh_interval_turns,
    # which only gates the periodic fallback branch). NULL/missing ⇒ 3.
    _trigger_min_interval_raw = data.get("refresh_trigger_min_interval")
    # 36fea6ca — inline-resolved-pointers toggle. Missing column/row ⇒ True
    # (default on); a stored 0 keeps pointers DB-only in the handoff.
    _inline_ptrs = data.get("handoff_inline_pointers")
    # 6e0e5cea — configurable active-session warning window. NULL/missing ⇒
    # 10 minutes (matches the previous hardcoded constant). Minimum 1 minute.
    _asw_mins_raw = data.get("active_session_warning_minutes")
    _active_session_warning_minutes = max(1, int(_asw_mins_raw)) if _asw_mins_raw is not None else 10
    # 490e100d — workspace-level default MCP tool priority per semantic task
    # category (generalizes 4d1fb28f's per-item required_tool pin up one
    # level). NULL/missing/malformed ⇒ None ("no workspace default set");
    # a stored JSON object ⇒ the {category: tool} dict, non-string values
    # coerced to str so a stray non-string JSON value can't break callers.
    _tool_priority_map_raw = data.get("tool_priority_map")
    _tool_priority_map: dict[str, str] | None = None
    if _tool_priority_map_raw:
        try:
            _decoded_tpm = json.loads(_tool_priority_map_raw)
            if isinstance(_decoded_tpm, dict) and _decoded_tpm:
                _tool_priority_map = {
                    str(k): str(v) for k, v in _decoded_tpm.items() if k and v
                }
                if not _tool_priority_map:
                    _tool_priority_map = None
        except Exception:  # noqa: BLE001 — malformed row ⇒ no workspace default
            _tool_priority_map = None
    # 4ef6ce5e — claim_verification_mode. NOT NULL DEFAULT 'off' at the column
    # level, but a legacy row predating the column (or any unrecognized value
    # that slipped in some other way) falls back to 'off' too — fail toward
    # the no-enforcement, unchanged-behavior state rather than silently
    # enabling a blocking hook nobody asked for.
    _claim_verification_mode = data.get("claim_verification_mode")
    if _claim_verification_mode not in _VALID_CLAIM_VERIFICATION_MODES:
        _claim_verification_mode = "off"
    return {
        "hitl_auto_answer_default": bool(data.get("hitl_auto_answer_default")),
        "sprint_name_default": data.get("sprint_name_default"),
        "display_name": data.get("display_name"),
        "log_task_sprint_nudge_threshold": int(data["log_task_sprint_nudge_threshold"])
        if data.get("log_task_sprint_nudge_threshold") is not None
        else 5,
        "handoff_template": data.get("handoff_template"),
        "execution_mode_default": _emode if _emode in ("autonomous", "interactive") else None,
        "code_intel_enabled_default": (None if _ci_default is None else bool(_ci_default)),
        "loop_enabled_default": (True if _loop_default is None else bool(_loop_default)),
        "auto_refresh_enabled": bool(data.get("auto_refresh_enabled")),
        "refresh_interval_turns": (int(_interval) if _interval is not None else 10) or 10,
        "refresh_triggers": _refresh_triggers,
        "refresh_trigger_min_interval": max(
            1, int(_trigger_min_interval_raw) if _trigger_min_interval_raw is not None else 3
        ),
        "handoff_inline_pointers": (True if _inline_ptrs is None else bool(_inline_ptrs)),
        "active_session_warning_minutes": _active_session_warning_minutes,
        # 5dfe34b2 / cd495afa — OFF-by-default opt-in toggle. Deliberately NOT a
        # parameter of update_workspace_settings below: the ONLY writer is
        # set_manual_issue_screening_enabled, gated on a completed
        # require_human=True HITL (see that function's docstring). Surfaced here
        # alongside a human-readable risk label per the design spec's "label the
        # toggle's risk clearly" requirement.
        "manual_issue_screening_enabled": bool(data.get("manual_issue_screening_enabled")),
        "manual_issue_screening_risk_warning": _MANUAL_ISSUE_SCREENING_RISK_WARNING,
        "tool_priority_map": _tool_priority_map,
        "claim_verification_mode": _claim_verification_mode,
        "updated_at": data.get("updated_at"),
    }


async def update_workspace_settings(
    db: aiosqlite.Connection,
    *,
    hitl_auto_answer_default: bool | None = None,
    sprint_name_default: str | None = None,
    display_name: str | None = None,
    log_task_sprint_nudge_threshold: int | None = None,
    handoff_template: str | None = None,
    execution_mode_default: str | None = None,
    code_intel_enabled_default: "bool | int | str | None" = None,
    loop_enabled_default: "bool | int | str | None" = None,
    auto_refresh_enabled: "bool | int | str | None" = None,
    refresh_interval_turns: int | None = None,
    refresh_triggers: "list[str] | str | None" = None,
    refresh_trigger_min_interval: int | None = None,
    handoff_inline_pointers: "bool | int | str | None" = None,
    active_session_warning_minutes: int | None = None,
    tool_priority_map: "dict[str, str] | str | None" = None,
    claim_verification_mode: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Upsert the per-tenant workspace settings row and return the new values.

    ``refresh_trigger_min_interval`` (db0361bb) — minimum number of dispatch
    calls that must elapse since the last context-refresh fire before a
    trigger tool (add_insight, pin_decision, etc.) is allowed to fire another
    one. Distinct from ``refresh_interval_turns``, which only gates the
    periodic fallback branch — before this setting existed, the trigger
    branch had no rate limit at all, so back-to-back trigger calls each
    injected a fresh refresh with zero spacing. Minimum 1 call; default 3.

    Only the fields passed (non-None) are changed. ``sprint_name_default=""``
    or ``display_name=""`` explicitly clears that label. ``execution_mode_default=""``
    clears the execution-mode default (new projects revert to their own default);
    ``code_intel_enabled_default`` accepts a bool/0/1 or the string ``""`` to clear.

    ``handoff_template`` (7855e580; length bound 47ac68a0) — ``""``/whitespace-
    only clears the custom template (reverts to the server default full-mode
    render). Raises ``ValueError`` when the stripped value exceeds
    :data:`_HANDOFF_TEMPLATE_MAX_CHARS` — every block it interpolates
    (recent_tasks, decisions, pending_items, notes) is already bounded at
    render time, but the raw template itself previously had no bound at
    write time, so an oversized template could be persisted and re-rendered
    unbounded into every handoff (see :data:`_HANDOFF_TEMPLATE_MAX_CHARS`'s
    own comment for the full incident).

    ``claim_verification_mode`` (4ef6ce5e) — ``"off"`` / ``"advisory"`` /
    ``"strict"``, case-insensitive; ``""`` clears back to the default
    (``"off"``). Unlike ``manual_issue_screening_enabled``, this is a PLAIN
    parameter of this generic settings-patch function, not gated behind a
    separate HITL-approved writer: moving TOWARD ``"strict"`` REDUCES risk
    (it catches false-success narration rather than introducing a new
    untrusted-content surface), so it doesn't need the self-escalation-proof
    HITL gate that toggle has. Raises ``ValueError`` for any other non-empty
    string. See ``get_workspace_settings`` for what each mode does.

    ``tool_priority_map`` (490e100d) — workspace-level default MCP tool
    priority per semantic task category, generalizing 4d1fb28f's per-item
    ``required_tool`` pin up one level (e.g. ``{"code-reading": "Serena:
    find_symbol"}``). A ``dict`` is JSON-encoded; an empty dict or the string
    ``""`` clears the workspace default entirely (reverts to ordinary
    executor discretion / per-item pins only); a non-empty string is stored
    verbatim (assumed already JSON-encoded, mirrors ``refresh_triggers``).
    Rendered as a HARD, unconditional directive by
    ``handoff._build_quick_start_goal`` — not a soft hint — for every pending
    item whose title/notes match a configured category and that has no
    item-level ``required_tool`` override (the item-level pin always wins).
    """
    settings_key = _ws_settings_key(tenant_id)
    # Ensure the row exists before updating individual fields.
    await db.execute(
        "INSERT INTO workspace_settings (id, tenant_id) VALUES (?, ?) "
        "ON CONFLICT(id) DO NOTHING",
        (settings_key, tenant_id),
    )
    updates: list[str] = []
    params: list[Any] = []
    if hitl_auto_answer_default is not None:
        updates.append("hitl_auto_answer_default = ?")
        params.append(1 if hitl_auto_answer_default else 0)
    if sprint_name_default is not None:
        updates.append("sprint_name_default = ?")
        params.append(sprint_name_default or None)
    if display_name is not None:
        updates.append("display_name = ?")
        params.append(display_name.strip() or None)
    if log_task_sprint_nudge_threshold is not None:
        updates.append("log_task_sprint_nudge_threshold = ?")
        params.append(max(0, int(log_task_sprint_nudge_threshold)))
    if handoff_template is not None:
        # Empty string clears the custom template (reverts to server default).
        _template = handoff_template.strip() or None
        # 47ac68a0 — bound the raw template at write time; see
        # _HANDOFF_TEMPLATE_MAX_CHARS above for why this is needed even
        # though every block it interpolates is separately bounded already.
        if _template is not None and len(_template) > _HANDOFF_TEMPLATE_MAX_CHARS:
            raise ValueError(
                f"handoff_template must be at most {_HANDOFF_TEMPLATE_MAX_CHARS} "
                f"characters, got {len(_template)}"
            )
        updates.append("handoff_template = ?")
        params.append(_template)
    if execution_mode_default is not None:
        updates.append("execution_mode_default = ?")
        # Empty string clears the default; otherwise normalize to a valid posture.
        _emode = (execution_mode_default or "").strip().lower()
        params.append(normalize_execution_mode(_emode) if _emode else None)
    if code_intel_enabled_default is not None:
        updates.append("code_intel_enabled_default = ?")
        # "" clears; any truthy/1 → 1, falsey/0 → 0.
        if isinstance(code_intel_enabled_default, str) and not code_intel_enabled_default.strip():
            params.append(None)
        else:
            params.append(1 if code_intel_enabled_default and code_intel_enabled_default not in ("0", "false", "False") else 0)
    if loop_enabled_default is not None:
        # 76cf8bda — /loop auto-continue default. Truthy/1 → 1, falsey/0 → 0.
        updates.append("loop_enabled_default = ?")
        params.append(1 if loop_enabled_default and loop_enabled_default not in ("0", "false", "False") else 0)
    if auto_refresh_enabled is not None:
        # bf51b12e — planner context-refresh toggle. Truthy/1 → 1, falsey/0 → 0.
        updates.append("auto_refresh_enabled = ?")
        params.append(1 if auto_refresh_enabled and auto_refresh_enabled not in ("0", "false", "False") else 0)
    if refresh_interval_turns is not None:
        # At least 1 turn between interval-based refreshes.
        updates.append("refresh_interval_turns = ?")
        params.append(max(1, int(refresh_interval_turns)))
    if refresh_triggers is not None:
        # A list ⇒ JSON-encode; "" clears (revert to default trigger set);
        # any other string is stored verbatim (already JSON).
        updates.append("refresh_triggers = ?")
        if isinstance(refresh_triggers, list):
            params.append(json.dumps(refresh_triggers))
        elif isinstance(refresh_triggers, str):
            params.append(refresh_triggers.strip() or None)
        else:
            params.append(None)
    if refresh_trigger_min_interval is not None:
        # db0361bb — at least 1 call between trigger-branch refreshes.
        updates.append("refresh_trigger_min_interval = ?")
        params.append(max(1, int(refresh_trigger_min_interval)))
    if handoff_inline_pointers is not None:
        # 36fea6ca — inline resolved pointers in the handoff. Truthy/1 → 1,
        # falsey/0 (incl. the strings "0"/"false") → 0.
        updates.append("handoff_inline_pointers = ?")
        params.append(
            1 if handoff_inline_pointers and handoff_inline_pointers not in ("0", "false", "False") else 0
        )
    if active_session_warning_minutes is not None:
        # 6e0e5cea — configurable active-session warning window (minutes).
        # Minimum 1 minute; 0 or negative values are clamped to 1.
        updates.append("active_session_warning_minutes = ?")
        params.append(max(1, int(active_session_warning_minutes)))
    if tool_priority_map is not None:
        # 490e100d — workspace-level default tool priority per task category.
        # A dict ⇒ JSON-encode (empty dict clears); "" clears; any other
        # string is stored verbatim (already JSON, mirrors refresh_triggers).
        updates.append("tool_priority_map = ?")
        if isinstance(tool_priority_map, dict):
            params.append(json.dumps(tool_priority_map) if tool_priority_map else None)
        elif isinstance(tool_priority_map, str):
            params.append(tool_priority_map.strip() or None)
        else:
            params.append(None)
    if claim_verification_mode is not None:
        # 4ef6ce5e — "" clears back to the 'off' default; any other value
        # must be one of the three valid modes (case-insensitive).
        _cvm = (claim_verification_mode or "").strip().lower()
        if _cvm and _cvm not in _VALID_CLAIM_VERIFICATION_MODES:
            raise ValueError(
                f"claim_verification_mode must be one of "
                f"{sorted(_VALID_CLAIM_VERIFICATION_MODES)} or '' to clear, got "
                f"{claim_verification_mode!r}"
            )
        updates.append("claim_verification_mode = ?")
        params.append(_cvm or "off")
    if updates:
        from datetime import datetime, timezone
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        updates.append("updated_at = ?")
        params.append(now_ts)
        params.append(settings_key)
        await db.execute(
            f"UPDATE workspace_settings SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
    await db.commit()
    return await get_workspace_settings(db, tenant_id=tenant_id)


async def seed_workspace_settings_from_toml(db: aiosqlite.Connection) -> None:
    """1d69d5d9 — seed the self-host singleton workspace_settings row from
    meridian.toml (env > toml > default, via toml_config) on first boot. This is
    what makes the 46c83e55 toml readers actually take effect. No-op when the
    singleton row already exists (the DB is authoritative once configured) or
    when no self-host config (a meridian.toml file or MERIDIAN_* env var) is
    present. Fully guarded — a seed failure must never block server startup."""
    try:
        from .. import toml_config  # local: avoid import cycle at module load
        _has_cfg = toml_config.load_toml() is not None or any(
            os.environ.get(k) for k in (
                "MERIDIAN_AUTO_REFRESH", "MERIDIAN_REFRESH_INTERVAL_TURNS",
                "MERIDIAN_REFRESH_TRIGGERS", "MERIDIAN_LOOP_ENABLED",
                "MERIDIAN_MAX_TURNS", "MERIDIAN_FILESYSTEM_ROOTS",
            )
        )
        if not _has_cfg:
            return
        async with db.execute(
            "SELECT id FROM workspace_settings WHERE id = ?", ("singleton",)
        ) as cur:
            if await cur.fetchone() is not None:
                return  # already configured — the DB row wins over toml
        refresh = toml_config.get_context_refresh_config()
        defaults = toml_config.get_self_host_defaults()
        await update_workspace_settings(
            db,
            auto_refresh_enabled=bool(refresh.get("auto_refresh_enabled")),
            refresh_interval_turns=int(refresh.get("refresh_interval_turns") or 10),
            refresh_triggers=refresh.get("refresh_triggers"),
            loop_enabled_default=bool(defaults.get("loop_enabled_default", True)),
            tenant_id=None,
        )
    except Exception:  # noqa: BLE001 — seeding must never block startup
        pass


# ---------------------------------------------------------------------------
# 5dfe34b2 / cd495afa — manual-issue-screening toggle + action audit trail
#
# The toggle governs WHO can trigger a read of a manually-filed GitHub issue
# (an internal person choosing to enable it), not whether the CONTENT read is
# trustworthy — anyone on the internet can file a GitHub issue regardless of
# who flipped the toggle. Hardcoded protections must hold "just in case of
# internal hacking": (a) a compromised/malicious internal session or token
# must not be able to silently self-enable this mode, and (b) enabling it must
# not, on its own, make unscreened content actionable (see
# meridian.db.manual_issue_intel for the content-screening side of that).
# ---------------------------------------------------------------------------

_MANUAL_ISSUE_SCREENING_RISK_WARNING = (
    "RISK: enabling this lets Meridian's automated GitHub-issue comment/propose "
    "flow (never auto-close) act on issues filed directly on GitHub by ANYONE, "
    "not just issues Meridian itself created. Content is heuristically screened "
    "for prompt-injection shapes before use, but no screening technique is a "
    "complete mitigation (OWASP LLM01) — treat flagged/borderline content with "
    "human judgment, not blind trust."
)


async def record_action_audit_event(
    db: aiosqlite.Connection,
    event_type: str,
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
    actor: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """cd495afa / d86d70a5 — append a row to the action_audit_log.

    Append-only: this module never exposes an update/delete for this table.
    Used for toggle enable/disable events AND velocity/anomaly escalations
    (WHAT MERIDIAN DID — distinct from manual_issue_intel's raw-content log,
    which records WHAT MERIDIAN SAW). Never raises on a logging failure from
    the caller's perspective is NOT guaranteed here — callers that must not be
    blocked by audit-log trouble should wrap this in their own try/except (as
    e.g. the velocity-anomaly escalation path does), since a silently-dropped
    audit entry would defeat the point of an audit trail.
    """
    aid = _new_id()
    await db.execute(
        "INSERT INTO action_audit_log (id, tenant_id, project_id, event_type, actor, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (aid, tenant_id, project_id, event_type, actor, detail),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM action_audit_log WHERE id = ?", (aid,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) or {"id": aid}


async def get_action_audit_log(
    db: aiosqlite.Connection,
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
    event_type: str | None = None,
    since: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read the action_audit_log, newest first. All filters optional/AND'd.

    ``since`` is an inclusive lower bound on ``created_at`` (string-comparable
    ``YYYY-MM-DD HH:MM:SS`` form, matching this codebase's other TEXT
    timestamps) — used by the velocity/anomaly check to scope a time window.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if tenant_id is not None:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if event_type is not None:
        clauses.append("event_type = ?")
        params.append(event_type)
    if since is not None:
        clauses.append("created_at >= ?")
        params.append(since)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, int(limit)))
    async with db.execute(
        f"SELECT * FROM action_audit_log{where} ORDER BY created_at DESC LIMIT ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 0d95003f — generic cross-project quarantine mechanism.
#
# move_sprint_item_to_project / move_workspace_note_to_project (this module
# and sprint_items.py) handle "we know where a record belongs, move it
# there." Quarantine is the other half of the item's ask: "a record's
# project association looks WRONG or ambiguous, and no automated system
# (executor handoff, sprint-item completion, ...) should treat it as
# legitimately belonging to a project until a human/audited actor resolves
# it" — e.g. a sprint item whose depends_on crosses into another project
# (see find_cross_project_dependency_mismatches +
# audit_and_quarantine_sprint_item_dependency_mismatches in sprint_items.py),
# or a session/task/note/proposal/pointer surfaced with an unclear origin
# during a merge.
#
# Deliberately event-sourced over action_audit_log rather than a new
# stateful table: no schema migration required (event_type is free-text —
# see migrations._migrate_action_audit_log_table's docstring, "extensible to
# future action-audit entries"), tamper-evident (append-only, same guarantee
# the rest of the audit log already provides), and directly reuses
# record_action_audit_event / the action_audit_log table above instead of
# inventing parallel machinery. "Currently quarantined" for a given
# (record_type, record_id) key is derived purely by replaying its events in
# order: an entry is open if its most recent event is a quarantine event
# with no matching resolve event after it.
#
# Ordering note: action_audit_log's created_at has only whole-second
# precision (SQLite `datetime('now')` — see the postgres now() vs
# clock_timestamp() gap documented on _TS/_DATETIME_NOW_EXPR), which is not
# fine-grained enough to order a quarantine event and an immediate resolve
# of it (a realistic sequence in both tests and real usage). Each event's
# detail JSON therefore carries its own "_seq": time.monotonic_ns() —
# strictly increasing within this process, used ONLY as an ordering
# tiebreaker within one (record_type, record_id) key's own event list, never
# compared across processes or persisted as a wall-clock claim.
#
# Generic across record types by design: record_type is caller-supplied
# free text ("sprint_item", "session", "task", "note", "proposal",
# "pointer", "handoff_body", "generated_file", "redis_key", "index_shard",
# ...). This item's own remaining scope — the six record classes without a
# dedicated audited move/mismatch-scanner yet (sessions, tasks, notes,
# proposals/proposal-evidence, pointers, handoff-bodies, generated-files,
# Redis-keys, index-shards — see the item's own RESCUE-A note) — can each
# plug into THIS mechanism as soon as their own mismatch-DETECTION logic
# exists. That per-record-type detection (each record type's own "does this
# belong to project X" question needs its own schema-aware scan, the way
# find_cross_project_dependency_mismatches is specific to sprint_items'
# depends_on column) is the genuinely large remaining follow-up, not the
# quarantine bookkeeping itself — which is now one shared, tested primitive
# instead of needing six bespoke ones. Deliberately NOT attempted here for
# the same reason 4ce87a11 deferred them: each deserves dedicated design,
# not a rushed bundle.
# ---------------------------------------------------------------------------

CROSS_PROJECT_QUARANTINE_EVENT_TYPE = "cross_project_quarantine"
CROSS_PROJECT_QUARANTINE_RESOLVED_EVENT_TYPE = "cross_project_quarantine_resolved"

_VALID_QUARANTINE_RESOLUTIONS = frozenset({
    "moved", "dismissed_false_positive", "confirmed_correct_project",
})


async def _fetch_quarantine_events_grouped(
    db: aiosqlite.Connection,
) -> dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]]:
    """Read every quarantine/resolve action_audit_log row and group by
    (record_type, record_id), each group's events sorted ascending by the
    embedded "_seq" tiebreaker (0d95003f). Internal helper shared by the
    single-key and list-all read paths below."""
    async with db.execute(
        "SELECT * FROM action_audit_log WHERE event_type IN (?, ?) ORDER BY created_at ASC",
        (CROSS_PROJECT_QUARANTINE_EVENT_TYPE, CROSS_PROJECT_QUARANTINE_RESOLVED_EVENT_TYPE),
    ) as cur:
        rows = await cur.fetchall()
    grouped: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for row in rows:
        row_d = _row_to_dict(row)
        if row_d is None:
            continue
        try:
            detail = json.loads(row_d.get("detail") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        rt, rid = detail.get("record_type"), detail.get("record_id")
        if not rt or not rid:
            continue
        grouped.setdefault((rt, rid), []).append((row_d, detail))
    for key in grouped:
        grouped[key].sort(key=lambda pair: pair[1].get("_seq", 0))
    return grouped


def _quarantine_status_from_events(
    record_type: str,
    record_id: str,
    events: list[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    """Reduce one key's ordered (row, detail) event pairs to a current
    status dict (0d95003f). See get_cross_project_quarantine_status for the
    returned shape."""
    row, detail = events[-1]
    if row.get("event_type") == CROSS_PROJECT_QUARANTINE_RESOLVED_EVENT_TYPE:
        quarantine_row: dict[str, Any] | None = None
        quarantine_detail: dict[str, Any] | None = None
        for r, d in reversed(events[:-1]):
            if r.get("event_type") == CROSS_PROJECT_QUARANTINE_EVENT_TYPE:
                quarantine_row, quarantine_detail = r, d
                break
        return {
            "record_type": record_type,
            "record_id": record_id,
            "project_id": (quarantine_row or row).get("project_id"),
            "status": "resolved",
            "reason": (quarantine_detail or {}).get("reason"),
            "suspected_project_id": (quarantine_detail or {}).get("suspected_project_id"),
            "quarantined_at": quarantine_row.get("created_at") if quarantine_row else None,
            "quarantined_by": quarantine_row.get("actor") if quarantine_row else None,
            "resolution": detail.get("resolution"),
            "resolved_at": row.get("created_at"),
            "resolved_by": row.get("actor"),
            "note": detail.get("note"),
        }
    return {
        "record_type": record_type,
        "record_id": record_id,
        "project_id": row.get("project_id"),
        "status": "quarantined",
        "reason": detail.get("reason"),
        "suspected_project_id": detail.get("suspected_project_id"),
        "quarantined_at": row.get("created_at"),
        "quarantined_by": row.get("actor"),
        "resolution": None,
        "resolved_at": None,
        "resolved_by": None,
        "note": None,
    }


async def get_cross_project_quarantine_status(
    db: aiosqlite.Connection, record_type: str, record_id: str,
) -> dict[str, Any] | None:
    """Current quarantine status for (record_type, record_id), or None if
    this key has never been quarantined (0d95003f).

    Returns::

        {
            "record_type", "record_id", "project_id",
            "status": "quarantined" | "resolved",
            "reason", "suspected_project_id",
            "quarantined_at", "quarantined_by",
            "resolution", "resolved_at", "resolved_by", "note",
        }

    "status" is derived purely from replaying this key's own append-only
    events (see module docstring above) — never from any separate mutable
    state.
    """
    _record_type = (record_type or "").strip()
    _record_id = (record_id or "").strip()
    if not _record_type or not _record_id:
        return None
    grouped = await _fetch_quarantine_events_grouped(db)
    events = grouped.get((_record_type, _record_id))
    if not events:
        return None
    return _quarantine_status_from_events(_record_type, _record_id, events)


async def is_cross_project_quarantined(
    db: aiosqlite.Connection, record_type: str, record_id: str,
) -> bool:
    """Convenience boolean for callers that just need "should this record be
    treated as blocked from handoff/completion" (0d95003f) — e.g. a future
    executor-handoff or sprint-item-completion gate."""
    status = await get_cross_project_quarantine_status(db, record_type, record_id)
    return bool(status) and status.get("status") == "quarantined"


async def quarantine_cross_project_record(
    db: aiosqlite.Connection,
    record_type: str,
    record_id: str,
    project_id: str,
    *,
    reason: str,
    actor: str,
    suspected_project_id: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Flag (record_type, record_id) as an ambiguous/foreign cross-project
    reference for project_id, WITHOUT moving or deleting anything (0d95003f).

    Idempotent: if an OPEN quarantine entry already exists for this exact
    key, returns it unchanged rather than writing a duplicate event — a
    detection scan is safe to call this on every run.

    Returns ``{"quarantined": bool, "entry": dict | None, "error": str | None}``.
    Refuses (quarantined=False, entry=None, non-empty error) on empty
    record_type / record_id / reason / actor — an unattributed, unexplained
    quarantine is refused, mirroring move_sprint_item_to_project's identical
    guard.
    """
    _record_type = (record_type or "").strip()
    _record_id = (record_id or "").strip()
    _reason = (reason or "").strip()
    _actor = (actor or "").strip()
    if not _record_type or not _record_id:
        return {
            "quarantined": False, "entry": None,
            "error": "record_type and record_id are required and must be non-empty.",
        }
    if not _reason:
        return {"quarantined": False, "entry": None, "error": "reason is required and must be non-empty."}
    if not _actor:
        return {"quarantined": False, "entry": None, "error": "actor is required and must be non-empty."}

    existing = await get_cross_project_quarantine_status(db, _record_type, _record_id)
    if existing is not None and existing.get("status") == "quarantined":
        return {"quarantined": False, "entry": existing, "error": None}

    await record_action_audit_event(
        db, CROSS_PROJECT_QUARANTINE_EVENT_TYPE,
        tenant_id=tenant_id,
        project_id=project_id,
        actor=_actor,
        detail=json.dumps({
            "record_type": _record_type,
            "record_id": _record_id,
            "project_id": project_id,
            "suspected_project_id": suspected_project_id,
            "reason": _reason,
            "_seq": time.monotonic_ns(),
        }),
    )
    entry = await get_cross_project_quarantine_status(db, _record_type, _record_id)
    return {"quarantined": True, "entry": entry, "error": None}


async def resolve_cross_project_quarantine(
    db: aiosqlite.Connection,
    record_type: str,
    record_id: str,
    *,
    resolution: str,
    actor: str,
    note: str | None = None,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Close an OPEN quarantine entry for (record_type, record_id) (0d95003f).

    ``resolution`` must be one of ``_VALID_QUARANTINE_RESOLUTIONS``:

    * ``"moved"`` — an audited move (e.g. move_sprint_item_to_project)
      resolved the mismatch.
    * ``"dismissed_false_positive"`` — reviewed; the record's existing
      project association was actually correct.
    * ``"confirmed_correct_project"`` — reviewed and re-affirmed without
      moving anything.

    Returns ``{"resolved": bool, "entry": dict | None, "error": str | None}``.
    Refuses if there is no currently-open quarantine entry for this key
    (nothing to resolve), or resolution/actor are invalid/empty — mirrors
    move_sprint_item_to_project's non-empty-actor guard.
    """
    _record_type = (record_type or "").strip()
    _record_id = (record_id or "").strip()
    _resolution = (resolution or "").strip()
    _actor = (actor or "").strip()
    if _resolution not in _VALID_QUARANTINE_RESOLUTIONS:
        return {
            "resolved": False, "entry": None,
            "error": (
                f"resolution must be one of {sorted(_VALID_QUARANTINE_RESOLUTIONS)}, "
                f"got {resolution!r}."
            ),
        }
    if not _actor:
        return {"resolved": False, "entry": None, "error": "actor is required and must be non-empty."}

    current = await get_cross_project_quarantine_status(db, _record_type, _record_id)
    if current is None or current.get("status") != "quarantined":
        return {
            "resolved": False, "entry": current,
            "error": "no open quarantine entry found for this record.",
        }

    await record_action_audit_event(
        db, CROSS_PROJECT_QUARANTINE_RESOLVED_EVENT_TYPE,
        tenant_id=tenant_id,
        project_id=project_id if project_id is not None else current.get("project_id"),
        actor=_actor,
        detail=json.dumps({
            "record_type": _record_type,
            "record_id": _record_id,
            "resolution": _resolution,
            "note": note,
            "_seq": time.monotonic_ns(),
        }),
    )
    entry = await get_cross_project_quarantine_status(db, _record_type, _record_id)
    return {"resolved": True, "entry": entry, "error": None}


async def list_quarantined_cross_project_records(
    db: aiosqlite.Connection,
    *,
    project_id: str | None = None,
    record_type: str | None = None,
) -> list[dict[str, Any]]:
    """Read-only: every CURRENTLY-open quarantine entry, optionally filtered
    to a project and/or record_type (0d95003f). Ordered by quarantined_at
    then record_type/record_id for determinism. Never mutates anything."""
    grouped = await _fetch_quarantine_events_grouped(db)
    results: list[dict[str, Any]] = []
    for (rt, rid), events in grouped.items():
        status = _quarantine_status_from_events(rt, rid, events)
        if status.get("status") != "quarantined":
            continue
        if project_id is not None and status.get("project_id") != project_id:
            continue
        if record_type is not None and rt != record_type:
            continue
        results.append(status)
    results.sort(key=lambda e: (e.get("quarantined_at") or "", e["record_type"], e["record_id"]))
    return results


class ManualIssueScreeningToggleError(ValueError):
    """Raised when set_manual_issue_screening_enabled(True, ...) is called
    without a genuine, completed, require_human=True HITL approval backing
    it. Distinct exception type so callers can distinguish "you tried to
    self-escalate" from an ordinary validation error."""


async def set_manual_issue_screening_enabled(
    db: aiosqlite.Connection,
    enabled: bool,
    *,
    tenant_id: str | None = None,
    actor: str | None = None,
    hitl_id: str | None = None,
) -> dict[str, Any]:
    """cd495afa — the ONE and ONLY writer of
    workspace_settings.manual_issue_screening_enabled anywhere in this
    codebase. ``update_workspace_settings`` (the generic, MCP-exposed
    settings-patch function) deliberately does NOT accept this field as a
    parameter — there is no update_workspace_settings-style direct write path
    to this column at all.

    Enabling (``enabled=True``) requires ``hitl_id`` naming a HITL request
    that, at the moment this function is called, is ALL of:
      1. found (exists in hitl_requests),
      2. ``kind == 'manual_issue_screening_toggle'``,
      3. ``status == 'answered'``,
      4. its stored payload has ``require_human: true`` (the e43e6941
         guarantee — this was persisted at request_hitl() time and can never
         be forged after the fact by an answer),
      5. its ``answer`` affirms enabling (case-insensitively starts with
         'yes').
    Any failure raises :class:`ManualIssueScreeningToggleError` and writes
    NOTHING — there is no partial-enable state. Because ``require_human=True``
    HITLs are structurally exempt from Meridian's auto-answer machinery (see
    ``request_hitl`` / ``_hitl_should_auto_answer``), condition 3+4 together
    mean a genuine human reply is the only way condition 3 can ever become
    true for this ``kind`` — an autonomous/executor session or a compromised
    API token cannot manufacture an 'answered' row for a require_human HITL by
    itself.

    Disabling (``enabled=False``) has no HITL gate — turning the riskier mode
    OFF is the fail-safe direction — but every flip, either direction, is
    recorded to action_audit_log (cd495afa point 2) before returning.

    Returns the updated get_workspace_settings() dict.
    """
    settings_key = _ws_settings_key(tenant_id)
    if enabled:
        if not hitl_id:
            raise ManualIssueScreeningToggleError(
                "enabling manual_issue_screening requires hitl_id referencing a "
                "completed require_human=True HITL of "
                "kind='manual_issue_screening_toggle' — there is no direct-write path"
            )
        async with db.execute(
            "SELECT * FROM hitl_requests WHERE id = ?", (hitl_id,)
        ) as cur:
            row = await cur.fetchone()
        hitl = _row_to_dict(row)
        if hitl is None:
            raise ManualIssueScreeningToggleError(f"hitl request '{hitl_id}' not found")
        if hitl.get("kind") != "manual_issue_screening_toggle":
            raise ManualIssueScreeningToggleError(
                f"hitl request '{hitl_id}' is kind={hitl.get('kind')!r}, not "
                "'manual_issue_screening_toggle'"
            )
        if hitl.get("status") != "answered":
            raise ManualIssueScreeningToggleError(
                f"hitl request '{hitl_id}' is not answered (status={hitl.get('status')!r})"
            )
        try:
            _payload = json.loads(hitl.get("payload") or "{}")
        except (TypeError, ValueError):
            _payload = {}
        if not (isinstance(_payload, dict) and _payload.get("require_human") is True):
            raise ManualIssueScreeningToggleError(
                f"hitl request '{hitl_id}' was not filed with require_human=True — "
                "refusing to trust it as a self-escalation-proof approval"
            )
        _answer = (hitl.get("answer") or "").strip().lower()
        if not _answer.startswith("yes"):
            raise ManualIssueScreeningToggleError(
                f"hitl request '{hitl_id}' answer ({hitl.get('answer')!r}) does not "
                "affirm enabling"
            )
    # Ensure the settings row exists (same upsert pattern as update_workspace_settings).
    await db.execute(
        "INSERT INTO workspace_settings (id, tenant_id) VALUES (?, ?) "
        "ON CONFLICT(id) DO NOTHING",
        (settings_key, tenant_id),
    )
    await db.execute(
        "UPDATE workspace_settings SET manual_issue_screening_enabled = ? WHERE id = ?",
        (1 if enabled else 0, settings_key),
    )
    await db.commit()
    await record_action_audit_event(
        db,
        "manual_issue_screening_enabled" if enabled else "manual_issue_screening_disabled",
        tenant_id=tenant_id,
        actor=actor or (f"hitl:{hitl_id}" if hitl_id else None),
        detail=json.dumps({"hitl_id": hitl_id}) if hitl_id else None,
    )
    return await get_workspace_settings(db, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Workspace members — team invite flow
# ---------------------------------------------------------------------------


async def create_workspace_invite(
    db: aiosqlite.Connection,
    tenant_id: str,
    email: str,
    role: str,
    token_hash: str,
    *,
    github_access: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Insert a pending workspace member row (joined_at=NULL).

    G5.20 — ``github_access`` caps repo-touching MCP tools for this invitee.
    Defaults from role when omitted (viewer→none, member→read, admin/owner→write).

    d116642e — ``project_id`` scopes the invite to a single project. When
    ``None`` (default) the member is workspace-wide and sees every project
    (current behavior). When set, the member is project-scoped: listing-only
    scoping applies (they see only that project in listings). Airtight
    per-request access enforcement is deferred pending the product decision
    (pin b11c7cf6).
    """
    from .. import roles as _roles  # noqa: PLC0415
    mid = _new_id()
    if github_access is None:
        github_access = _roles.default_github_access_for_role(role)
    await db.execute(
        "INSERT INTO workspace_members "
        "(id, tenant_id, email, role, github_access, token_hash, project_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (mid, tenant_id, email, role, github_access, token_hash, project_id),
    )
    await db.commit()
    async with db.execute("SELECT * FROM workspace_members WHERE id = ?", (mid,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_workspace_invite_by_token_hash(
    db: aiosqlite.Connection,
    token_hash: str,
) -> dict[str, Any] | None:
    """Return a pending invite by token hash, or None if not found / already accepted."""
    async with db.execute(
        "SELECT * FROM workspace_members WHERE token_hash = ? AND joined_at IS NULL",
        (token_hash,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def accept_workspace_invite(
    db: aiosqlite.Connection,
    member_id: str,
) -> dict[str, Any] | None:
    """Mark an invite as accepted (set joined_at, clear token_hash)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE workspace_members SET joined_at = ?, token_hash = NULL WHERE id = ?",
        (now, member_id),
    )
    await db.commit()
    async with db.execute("SELECT * FROM workspace_members WHERE id = ?", (member_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_pending_invites_for_email(
    db: aiosqlite.Connection,
    email: str,
) -> list[dict[str, Any]]:
    """Return all pending workspace_members rows for email (joined_at IS NULL).

    Used at OAuth login to auto-accept invites sent before the user had an
    account (fbbe99af fallback).
    """
    async with db.execute(
        "SELECT * FROM workspace_members WHERE LOWER(email) = LOWER(?) AND joined_at IS NULL",
        (email,),
    ) as cur:
        rows = await cur.fetchall()
    return [r for r in (_row_to_dict(row) for row in rows) if r]


async def resolve_member_role(
    db: aiosqlite.Connection,
    tenant_id: str,
    email: str,
) -> tuple[str, str] | None:
    """G5.19 / G5.20 — return ``(role, github_access)`` for the given user
    in the given tenant's workspace.

    Order:
     1. If ``email`` matches the tenant row's own email → ('owner','write').
     2. Else if an accepted workspace_members row exists for this
        (tenant_id, email) → use the row's role + github_access.
     3. Else → None (caller should treat as 403 / not a member).
    """
    e = (email or "").strip().lower()
    if not e:
        return None
    async with db.execute(
        "SELECT email FROM tenants WHERE id = ?", (tenant_id,),
    ) as cur:
        tenant_row = await cur.fetchone()
    if tenant_row and (str(tenant_row["email"]).lower() == e):
        return ("owner", "write")
    async with db.execute(
        "SELECT role, github_access FROM workspace_members "
        "WHERE tenant_id = ? AND LOWER(email) = ? AND joined_at IS NOT NULL "
        "ORDER BY joined_at DESC LIMIT 1",
        (tenant_id, e),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return (
        (row["role"] or "member"),
        (row["github_access"] or "read"),
    )


async def workspace_member_accepted_for_email(
    db: aiosqlite.Connection,
    email: str,
) -> dict[str, Any] | None:
    """G5.22 — return an accepted workspace membership for ``email`` (joined,
    not pending), or None. Used by the OAuth callback to skip auto-Neon
    provisioning for invitees who already belong to someone else's workspace."""
    e = (email or "").strip().lower()
    if not e:
        return None
    async with db.execute(
        "SELECT * FROM workspace_members "
        "WHERE LOWER(email) = ? AND joined_at IS NOT NULL "
        "ORDER BY joined_at DESC LIMIT 1",
        (e,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_workspace_member_by_id(
    db: aiosqlite.Connection,
    member_id: str,
    tenant_id: str,
) -> dict[str, Any] | None:
    """Return a single workspace_members row scoped to tenant_id."""
    async with db.execute(
        "SELECT * FROM workspace_members WHERE id = ? AND tenant_id = ?",
        (member_id, tenant_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def refresh_workspace_invite_token(
    db: aiosqlite.Connection,
    member_id: str,
    tenant_id: str,
    token_hash: str,
) -> dict[str, Any] | None:
    """Replace the invite token for a pending member row."""
    await db.execute(
        "UPDATE workspace_members SET token_hash = ? WHERE id = ? AND tenant_id = ? AND joined_at IS NULL",
        (token_hash, member_id, tenant_id),
    )
    await db.commit()
    async with db.execute("SELECT * FROM workspace_members WHERE id = ?", (member_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def list_workspace_members(
    db: aiosqlite.Connection,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """Return all workspace members (pending and accepted) for a tenant."""
    async with db.execute(
        "SELECT * FROM workspace_members WHERE tenant_id = ? ORDER BY invited_at ASC",
        (tenant_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]


async def count_workspace_members(
    db: aiosqlite.Connection,
    tenant_id: str,
) -> int:
    """Return the total member count (pending + accepted) for a tenant."""
    async with db.execute(
        "SELECT COUNT(*) FROM workspace_members WHERE tenant_id = ?",
        (tenant_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return 0
    vals = list(row.values()) if hasattr(row, "values") else list(row)
    return int(vals[0]) if vals else 0


async def delete_workspace_member(
    db: aiosqlite.Connection,
    member_id: str,
    tenant_id: str,
) -> bool:
    """Remove a workspace member row. Returns True (no-op on missing row)."""
    await db.execute(
        "DELETE FROM workspace_members WHERE id = ? AND tenant_id = ?",
        (member_id, tenant_id),
    )
    await db.commit()
    return True


async def update_workspace_member(
    db: aiosqlite.Connection,
    member_id: str,
    tenant_id: str,
    *,
    role: str | None = None,
    github_access: str | None = None,
) -> dict[str, Any] | None:
    """v2.8 — update a member's role and/or github_access (admin-only edit).

    Only the fields passed (non-None) are changed. Scoped by tenant_id so a
    caller can never touch another workspace's rows. Returns the updated row,
    or None when no member matched.
    """
    updates: list[str] = []
    params: list[Any] = []
    if role is not None:
        updates.append("role = ?")
        params.append(role)
    if github_access is not None:
        updates.append("github_access = ?")
        params.append(github_access)
    if updates:
        params.extend([member_id, tenant_id])
        await db.execute(
            f"UPDATE workspace_members SET {', '.join(updates)} "
            "WHERE id = ? AND tenant_id = ?",
            tuple(params),
        )
        await db.commit()
    async with db.execute(
        "SELECT * FROM workspace_members WHERE id = ? AND tenant_id = ?",
        (member_id, tenant_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_workspaces_for_email(
    db: aiosqlite.Connection,
    email: str,
) -> list[dict[str, Any]]:
    """Return workspaces the email has been invited to (accepted rows only).

    Each row: {tenant_id, owner_email, role, github_access, project_id}.
    ``project_id`` (d116642e) is NULL for workspace-wide members and set for
    project-scoped members. Used to populate the workspace-switcher dropdown.
    """
    e = (email or "").strip().lower()
    if not e:
        return []
    async with db.execute(
        "SELECT wm.tenant_id, wm.role, wm.github_access, wm.project_id, "
        "t.email AS owner_email "
        "FROM workspace_members wm "
        "JOIN tenants t ON t.id = wm.tenant_id "
        "WHERE LOWER(wm.email) = ? AND wm.joined_at IS NOT NULL "
        "ORDER BY wm.invited_at ASC",
        (e,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]


async def get_scoped_project_ids_for_member(
    db: aiosqlite.Connection,
    tenant_id: str,
    email: str,
) -> list[str] | None:
    """d116642e — listing-only project scoping for a workspace member.

    Returns the set of project_ids an accepted member is scoped to within the
    given tenant's workspace, or ``None`` when the member is NOT project-scoped
    (i.e. has any workspace-wide row, or is not a member at all) — meaning they
    see every project.

    Semantics:
      - returns ``None``  → no scoping; caller lists all projects (default).
      - returns ``[...]`` → list only these project_ids in UI listings.

    NOTE: this is listing-only scoping. It does NOT block direct-by-ID access to
    other projects in the same workspace — airtight per-request enforcement is
    deferred pending the open product decision (pin b11c7cf6). Writes remain
    gated by role enforcement (393eed0a).
    """
    e = (email or "").strip().lower()
    if not e:
        return None
    async with db.execute(
        "SELECT project_id FROM workspace_members "
        "WHERE tenant_id = ? AND LOWER(email) = ? AND joined_at IS NOT NULL",
        (tenant_id, e),
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return None
    scoped: list[str] = []
    for r in rows:
        pid = r["project_id"] if isinstance(r, dict) else r[0]
        if pid is None:
            # Any workspace-wide membership row wins → no scoping.
            return None
        scoped.append(pid)
    return scoped
