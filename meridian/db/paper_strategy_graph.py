"""81b5491b — SCHEMA: paper_strategy_graph — argument-layer nodes and
rhetorical edges.

See :mod:`meridian.paper_strategy` for the closed vocabularies
(``NODE_TYPES``/``EDGE_TYPES``/``NODE_STATUSES``), edge directionality
documentation, and the "why a separate graph from research_graph" rationale.
This module is the persistence layer on top of that: two tables, dual-backend
(SQLite + Postgres, mirrored in ``pg_adapter._migrate_pg_paper_strategy_graph``),
following the exact append-only-supersession / typed-enum / idempotent-insert
conventions already established by ``meridian.db.research_graph`` and
``meridian.db.decision_evidence`` — not reinvented.

SCHEMA
------

``paper_strategy_nodes`` — one row per (family, version) of one
argument-layer node:

* ``family_id`` is the STABLE identity of one logical argument element
  across edits (mirrors ``research_nodes.identity_key`` / ``profile_layers``'
  ``revision`` counter). A brand-new node's ``family_id`` equals its own
  ``id`` — no separate "create a family first" step.
* ``version`` is a per-``family_id`` monotonic counter starting at 1,
  enforced unique together with ``(project_id, family_id)``.
* ``status`` carries the human-approval gate: every node is created
  ``draft``; :func:`approve_strategy_node` / :func:`reject_strategy_node`
  move a draft to a terminal ``approved``/``rejected`` decision;
  :func:`supersede_strategy_node` retires the CURRENT row of a family
  (``draft`` or ``approved`` only — not ``rejected``/``superseded``) to
  ``superseded`` and appends the next version, atomically, in one call.
  Nothing is ever hard-deleted — the full history stays queryable via
  :func:`list_strategy_node_versions`.
* ``document_ref`` is a nullable, free-text pointer to the manuscript
  section this argument element concerns (a ``doc_store`` element id, a
  section slug, or plain text) — deliberately NOT a typed pointer/foreign
  key, matching ``decisions_pinned.code_anchor``'s "coarse anchor" weight
  class rather than the fuller ``sprint_item_pointers`` primitive, which
  would be over-engineering for a bare schema item.

``paper_strategy_edges`` — one row per rhetorical relation between two
EXACT node rows (``from_node_id``/``to_node_id`` are real primary keys the
caller already resolved — simpler than ``research_edges``'s
identity-key-with-unresolved-endpoint machinery, which is deliberately out
of scope here: a bare schema item gets basic constructors/validators, not a
second BFS-capable graph engine). Idempotent on the exact ``(project_id,
edge_kind, from_node_id, to_node_id)`` tuple. Both endpoints must already
exist in the same ``project_id``; a self-loop (``from_node_id ==
to_node_id``) is rejected before any write.

WRITE SEMANTICS
---------------

* :func:`create_strategy_node` starts a brand-new, independent node family
  at version 1, status ``draft``.
* :func:`approve_strategy_node` / :func:`reject_strategy_node` are the
  human-approval gate: both require the node to currently be ``draft``
  (approving/rejecting an already-decided or retired row is rejected with
  ``ValueError`` — decide once, then supersede if the framing needs to
  change).
* :func:`supersede_strategy_node` is the explicit, atomic "new version of
  an existing argument": it takes an EXACT, caller-supplied
  ``old_node_id`` (never a search/best-match result, mirroring
  ``decision_evidence.supersede_decision_evidence``'s safety contract),
  inherits any field the caller omits from the old row, and in one call
  inserts the new ``draft`` row AND flips the old row to
  ``status='superseded'`` before the same ``await db.commit()``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite

# Shared helpers from the parent db package — available at import time
# because this module is imported at the BOTTOM of db/__init__.py, after
# these names are already defined. Mirrors db.research_graph's identical
# pattern (see that module's own docstring note).
from meridian.db import _new_id, _row_to_dict
from meridian.paper_strategy import validate_edge_kind, validate_node_type


def _now_iso() -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS' — matches decision_evidence's cross-dialect-
    safe timestamp convention (computed in Python, not a SQL now()/
    datetime('now') call — see the project's now() vs clock_timestamp() note)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _is_unique_violation(exc: BaseException) -> bool:
    """Heuristic: does ``exc`` look like a UNIQUE/duplicate-key violation?

    Matches sqlite3's ``UNIQUE constraint failed`` and psycopg3's
    ``UniqueViolation`` (``duplicate key value violates unique
    constraint``). Never raises. Duplicated per-module by existing
    convention (see ``research_graph._is_unique_violation``)."""
    msg = str(exc).lower()
    return "unique" in msg or "duplicate key" in msg


# ---------------------------------------------------------------------------
# Migration — guarded, idempotent, not inline in either base schema literal
# (the 2026-07-04 outage rule: no unguarded CREATE INDEX on a
# migration-added column/table in CREATE_TABLES/CREATE_TABLES_CORE).
# Mirrored on Postgres by pg_adapter._migrate_pg_paper_strategy_graph.
# ---------------------------------------------------------------------------


async def _migrate_paper_strategy_graph(db: aiosqlite.Connection) -> None:
    """81b5491b — create paper_strategy_nodes / paper_strategy_edges if absent."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS paper_strategy_nodes (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            family_id TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            node_type TEXT NOT NULL CHECK (node_type IN (
                'thesis', 'claim', 'counter_claim', 'evidence', 'warrant',
                'rebuttal', 'concession', 'motivation', 'framing_note'
            )),
            document_ref TEXT,
            statement TEXT NOT NULL,
            rationale TEXT,
            status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
                'draft', 'approved', 'rejected', 'superseded'
            )),
            approved_by TEXT,
            approved_at TEXT,
            rejection_reason TEXT,
            supersedes_id TEXT,
            superseded_by TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT
        )"""
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_strategy_nodes_family_version "
        "ON paper_strategy_nodes(project_id, family_id, version)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_strategy_nodes_project "
        "ON paper_strategy_nodes(project_id, status)"
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS paper_strategy_edges (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            edge_kind TEXT NOT NULL CHECK (edge_kind IN (
                'supports', 'rebuts', 'concedes', 'qualifies', 'motivates',
                'contrasts', 'elaborates', 'restates'
            )),
            from_node_id TEXT NOT NULL,
            to_node_id TEXT NOT NULL,
            label TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_strategy_edges_unique "
        "ON paper_strategy_edges(project_id, edge_kind, from_node_id, to_node_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_strategy_edges_from "
        "ON paper_strategy_edges(project_id, from_node_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_strategy_edges_to "
        "ON paper_strategy_edges(project_id, to_node_id)"
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


async def create_strategy_node(
    db: aiosqlite.Connection,
    project_id: str,
    node_type: str,
    statement: str,
    *,
    document_ref: "str | None" = None,
    rationale: "str | None" = None,
    created_by: "str | None" = None,
    family_id: "str | None" = None,
    version: int = 1,
    supersedes_id: "str | None" = None,
) -> dict[str, Any]:
    """Insert one argument-layer node row in ``status='draft'``. Returns the
    stored row.

    ``family_id``/``version``/``supersedes_id`` are advanced parameters used
    internally by :func:`supersede_strategy_node` to append the NEXT version
    of an EXISTING node family rather than starting a brand new one — prefer
    calling :func:`supersede_strategy_node` directly when editing an
    existing node; this function's default call (all three omitted) always
    starts a brand new, independent argument-layer node family at version 1
    (``family_id`` defaults to the freshly generated row id).

    Raises ``ValueError`` on an unknown ``node_type``, a blank ``statement``,
    or a ``statement``/``rationale`` that looks like a secret (fail-closed,
    matching every other DB write path that persists caller-supplied text).
    """
    node_type = validate_node_type(node_type)
    statement = (statement or "").strip()
    if not statement:
        raise ValueError("create_strategy_node requires a non-empty statement")
    from meridian.secret_redaction import check_for_secrets  # noqa: PLC0415
    check_for_secrets(statement, context="paper strategy node statement")
    if rationale is not None:
        check_for_secrets(rationale, context="paper strategy node rationale")
    if version < 1:
        raise ValueError(f"version must be >= 1, got {version!r}")

    nid = _new_id()
    resolved_family_id = family_id or nid
    await db.execute(
        "INSERT INTO paper_strategy_nodes "
        "(id, project_id, family_id, version, node_type, document_ref, "
        "statement, rationale, status, supersedes_id, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
        (
            nid, project_id, resolved_family_id, version, node_type,
            document_ref, statement, rationale, supersedes_id, created_by,
        ),
    )
    if supersedes_id:
        await db.execute(
            "UPDATE paper_strategy_nodes SET status = 'superseded', "
            "superseded_by = ?, updated_at = ? "
            "WHERE id = ? AND project_id = ?",
            (nid, _now_iso(), supersedes_id, project_id),
        )
    await db.commit()
    created = await get_strategy_node(db, project_id, nid)
    assert created is not None  # just written
    return created


async def get_strategy_node(
    db: aiosqlite.Connection, project_id: str, node_id: str
) -> "dict[str, Any] | None":
    """Fetch one paper_strategy_nodes row by id, scoped to project_id."""
    async with db.execute(
        "SELECT * FROM paper_strategy_nodes WHERE id = ? AND project_id = ?",
        (node_id, project_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def list_strategy_node_versions(
    db: aiosqlite.Connection, project_id: str, family_id: str
) -> list[dict[str, Any]]:
    """Every row (any status) for this node family, oldest first — the full,
    append-only revision history. Nothing this module writes is ever hard
    deleted, so this is always the complete story."""
    family_id = (family_id or "").strip()
    if not family_id:
        raise ValueError("list_strategy_node_versions requires a non-empty family_id")
    async with db.execute(
        "SELECT * FROM paper_strategy_nodes WHERE project_id = ? AND family_id = ? "
        "ORDER BY version ASC",
        (project_id, family_id),
    ) as cur:
        rows = await cur.fetchall()
    return [n for n in (_row_to_dict(r) for r in rows) if n is not None]


async def get_current_strategy_node(
    db: aiosqlite.Connection, project_id: str, family_id: str
) -> "dict[str, Any] | None":
    """The highest-``version`` non-``superseded`` row for this family, or
    ``None`` if the family doesn't exist. ``draft``, ``approved``, and
    ``rejected`` are all "current" states for this purpose — only
    ``superseded`` means a strictly newer version has replaced this row."""
    family_id = (family_id or "").strip()
    if not family_id:
        raise ValueError("get_current_strategy_node requires a non-empty family_id")
    async with db.execute(
        "SELECT * FROM paper_strategy_nodes WHERE project_id = ? AND family_id = ? "
        "AND status != 'superseded' ORDER BY version DESC LIMIT 1",
        (project_id, family_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def approve_strategy_node(
    db: aiosqlite.Connection, project_id: str, node_id: str, approved_by: str
) -> dict[str, Any]:
    """Human-approval gate: mark one ``draft`` node ``approved``.

    ``approved_by`` (a human identity, e.g. a name/email/handle) is
    required and non-empty — an approval with no attributed approver is not
    a real approval. Raises ``ValueError`` if the node doesn't exist in
    ``project_id`` or is not currently ``draft`` (an already-decided or
    retired row cannot be re-approved; supersede it to change the framing
    instead).
    """
    approved_by = (approved_by or "").strip()
    if not approved_by:
        raise ValueError("approve_strategy_node requires a non-empty approved_by")
    node = await get_strategy_node(db, project_id, node_id)
    if node is None:
        raise ValueError(
            f"paper strategy node {node_id!r} not found in project {project_id!r}"
        )
    if node["status"] != "draft":
        raise ValueError(
            f"paper strategy node {node_id!r} has status {node['status']!r} — "
            "only a 'draft' node can be approved"
        )
    now = _now_iso()
    await db.execute(
        "UPDATE paper_strategy_nodes SET status = 'approved', approved_by = ?, "
        "approved_at = ?, updated_at = ? WHERE id = ? AND project_id = ?",
        (approved_by, now, now, node_id, project_id),
    )
    await db.commit()
    updated = await get_strategy_node(db, project_id, node_id)
    assert updated is not None
    return updated


async def reject_strategy_node(
    db: aiosqlite.Connection, project_id: str, node_id: str, reason: str
) -> dict[str, Any]:
    """Human-approval gate: mark one ``draft`` node ``rejected`` (the
    proposed framing was considered and turned down — distinct from
    ``superseded``, which means a replacement version exists). Never
    deletes; the row stays in history.

    ``reason`` is required and non-empty. Raises ``ValueError`` if the node
    doesn't exist in ``project_id`` or is not currently ``draft``.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("reject_strategy_node requires a non-empty reason")
    node = await get_strategy_node(db, project_id, node_id)
    if node is None:
        raise ValueError(
            f"paper strategy node {node_id!r} not found in project {project_id!r}"
        )
    if node["status"] != "draft":
        raise ValueError(
            f"paper strategy node {node_id!r} has status {node['status']!r} — "
            "only a 'draft' node can be rejected"
        )
    from meridian.secret_redaction import check_for_secrets  # noqa: PLC0415
    check_for_secrets(reason, context="paper strategy node rejection reason")
    now = _now_iso()
    await db.execute(
        "UPDATE paper_strategy_nodes SET status = 'rejected', rejection_reason = ?, "
        "updated_at = ? WHERE id = ? AND project_id = ?",
        (reason, now, node_id, project_id),
    )
    await db.commit()
    updated = await get_strategy_node(db, project_id, node_id)
    assert updated is not None
    return updated


async def supersede_strategy_node(
    db: aiosqlite.Connection,
    project_id: str,
    old_node_id: str,
    *,
    node_type: "str | None" = None,
    statement: "str | None" = None,
    document_ref: "str | None" = None,
    rationale: "str | None" = None,
    created_by: "str | None" = None,
) -> dict[str, Any]:
    """Atomic supersede: an EXACT, caller-supplied ``old_node_id`` (a real
    primary key — never a search/best-match result, same safety contract as
    ``decision_evidence.supersede_decision_evidence``) is retired and the
    next version of the SAME family is created, in one call.

    Every keyword defaults to the old row's value when omitted — a caller
    changing just the ``statement`` doesn't have to re-supply everything
    else. Raises ``ValueError`` if ``old_node_id`` doesn't exist in
    ``project_id``, or is already ``rejected``/``superseded`` (supersede the
    CURRENT ``draft``/``approved`` row, not a retired or decided-against
    one — prevents building a branching, ambiguous version history).
    """
    old = await get_strategy_node(db, project_id, old_node_id)
    if old is None:
        raise ValueError(
            f"paper strategy node {old_node_id!r} not found in project {project_id!r}"
        )
    if old["status"] in ("superseded", "rejected"):
        raise ValueError(
            f"paper strategy node {old_node_id!r} has status {old['status']!r} — "
            "supersede the CURRENT draft/approved node, not a retired one"
        )
    return await create_strategy_node(
        db, project_id,
        node_type if node_type is not None else old["node_type"],
        statement if statement is not None else old["statement"],
        document_ref=document_ref if document_ref is not None else old.get("document_ref"),
        rationale=rationale if rationale is not None else old.get("rationale"),
        created_by=created_by,
        family_id=old["family_id"],
        version=old["version"] + 1,
        supersedes_id=old_node_id,
    )


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------


async def _find_edge_by_natural_key(
    db: aiosqlite.Connection,
    project_id: str,
    edge_kind: str,
    from_node_id: str,
    to_node_id: str,
) -> "dict[str, Any] | None":
    async with db.execute(
        "SELECT * FROM paper_strategy_edges WHERE project_id = ? AND edge_kind = ? "
        "AND from_node_id = ? AND to_node_id = ?",
        (project_id, edge_kind, from_node_id, to_node_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def create_strategy_edge(
    db: aiosqlite.Connection,
    project_id: str,
    edge_kind: str,
    from_node_id: str,
    to_node_id: str,
    *,
    label: "str | None" = None,
    created_by: "str | None" = None,
) -> dict[str, Any]:
    """Append one rhetorical edge between two EXACT, already-existing node
    rows. Idempotent on the exact ``(project_id, edge_kind, from_node_id,
    to_node_id)`` tuple.

    Raises ``ValueError`` on an unknown ``edge_kind``, a blank
    ``from_node_id``/``to_node_id``, a self-loop (``from_node_id ==
    to_node_id``), either endpoint not existing in ``project_id``, or a
    ``label`` that looks like a secret.
    """
    edge_kind = validate_edge_kind(edge_kind)
    from_node_id = (from_node_id or "").strip()
    to_node_id = (to_node_id or "").strip()
    if not from_node_id or not to_node_id:
        raise ValueError(
            "create_strategy_edge requires non-empty from_node_id/to_node_id"
        )
    if from_node_id == to_node_id:
        raise ValueError(
            f"cannot create a {edge_kind!r} edge from a node to itself "
            f"({from_node_id})"
        )
    if await get_strategy_node(db, project_id, from_node_id) is None:
        raise ValueError(
            f"paper strategy node {from_node_id!r} not found in project {project_id!r}"
        )
    if await get_strategy_node(db, project_id, to_node_id) is None:
        raise ValueError(
            f"paper strategy node {to_node_id!r} not found in project {project_id!r}"
        )
    if label is not None:
        from meridian.secret_redaction import check_for_secrets  # noqa: PLC0415
        check_for_secrets(label, context="paper strategy edge label")

    existing = await _find_edge_by_natural_key(db, project_id, edge_kind, from_node_id, to_node_id)
    if existing is not None:
        return existing

    eid = _new_id()
    try:
        await db.execute(
            "INSERT INTO paper_strategy_edges "
            "(id, project_id, edge_kind, from_node_id, to_node_id, label, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (eid, project_id, edge_kind, from_node_id, to_node_id, label, created_by),
        )
    except Exception as exc:  # noqa: BLE001 — classified below
        if _is_unique_violation(exc):
            # Lost a create race against another caller writing the SAME
            # (project, edge_kind, from, to) tuple — hand back the winner.
            winner = await _find_edge_by_natural_key(
                db, project_id, edge_kind, from_node_id, to_node_id
            )
            if winner is not None:
                return winner
        raise
    await db.commit()
    created = await _find_edge_by_natural_key(db, project_id, edge_kind, from_node_id, to_node_id)
    return created or {"id": eid}


async def get_strategy_edges_for_node(
    db: aiosqlite.Connection,
    project_id: str,
    node_id: str,
    *,
    role: "str | None" = None,
) -> list[dict[str, Any]]:
    """Raw edges touching ``node_id``. ``role`` narrows to ``'from'`` or
    ``'to'``; omitted (default) returns edges where the node appears on
    EITHER side, mirroring ``research_graph.get_edges_for_identity``."""
    node_id = (node_id or "").strip()
    if not node_id:
        raise ValueError("get_strategy_edges_for_node requires a non-empty node_id")
    if role == "from":
        sql = (
            "SELECT * FROM paper_strategy_edges WHERE project_id = ? "
            "AND from_node_id = ? ORDER BY created_at ASC, id ASC"
        )
        params: tuple[Any, ...] = (project_id, node_id)
    elif role == "to":
        sql = (
            "SELECT * FROM paper_strategy_edges WHERE project_id = ? "
            "AND to_node_id = ? ORDER BY created_at ASC, id ASC"
        )
        params = (project_id, node_id)
    elif role is None:
        sql = (
            "SELECT * FROM paper_strategy_edges WHERE project_id = ? "
            "AND (from_node_id = ? OR to_node_id = ?) "
            "ORDER BY created_at ASC, id ASC"
        )
        params = (project_id, node_id, node_id)
    else:
        raise ValueError(f"role must be None, 'from', or 'to', got {role!r}")
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [e for e in (_row_to_dict(r) for r in rows) if e is not None]
