"""b558892a — the durable research artifact graph: typed nodes/edges linking
claims, citations, code, runs, outputs, documents, and executor decisions.

See :mod:`meridian.research_graph` for the closed vocabularies
(``NODE_TYPES``/``EDGE_TYPES``), edge directionality documentation, and the
identity-key builders every node's ``identity_key`` should be built with.
This module is the persistence layer on top of that: two tables,
dual-backend (SQLite + Postgres, mirrored in
``pg_adapter._migrate_pg_research_graph``), following the exact append-only
/ typed-enum / idempotent-insert conventions already established by
``meridian.db.proposal_lineage`` and ``meridian.db.decision_evidence`` — not
reinvented.

SCHEMA
------

``research_nodes`` — one row per (identity, revision) fact:

* ``identity_key`` is the STABLE identity of the underlying source (a file
  path, a registered output, a DOCX element, a citation key, a run id, a
  pinned decision id, or a caller-minted claim id — see
  ``meridian.research_graph``'s identity-key builders). It never changes
  across revisions of the same source.
* ``revision`` is that source's version marker AT THIS ROW (a content hash,
  a git SHA, a version string — whatever is natural for the source type).
  Nullable: not every source type has a meaningful revision concept.
* ``seq`` is a per-``(project_id, node_type, identity_key)`` monotonic
  counter (mirrors ``proposal_lineage``'s ``sequence`` field) — the
  deterministic "which of this identity's rows is newest" tiebreaker used
  by :func:`get_current_node`, robust even when two rows share a
  wall-clock-identical ``created_at``.
* ``status`` is ``active`` or ``superseded`` — nothing is ever hard
  deleted. A ``superseded`` row's ``superseded_by`` names the row that
  replaced it; a row created via :func:`replace_node_revision` additionally
  carries ``supersedes_id`` pointing back.

WRITE SEMANTICS — "append-only or transactionally replaceable"
----------------------------------------------------------------

Two ways to write, both real and both exercised by tests:

* :func:`create_node` is a PURE APPEND: idempotent on the exact
  ``(project_id, node_type, identity_key, revision)`` tuple (a repeat call
  with the same identity+revision returns the existing row, never a
  duplicate), but two DIFFERENT revisions of the same identity both persist
  as separate ``active`` rows side by side — a genuine, unmanaged append-only
  history. This is the right call when a caller just wants to RECORD a fact
  ("this identity, at this revision, exists") without asserting anything
  about what came before.
* :func:`replace_node_revision` is the explicit TRANSACTIONALLY REPLACEABLE
  operation: it takes an EXACT, caller-supplied ``old_node_id`` (a real
  primary key — never a search/best-match result, mirroring
  ``decision_evidence``'s safety contract) and, in one call, inserts the new
  active row AND flips the old row to ``status='superseded'`` before the
  same ``await db.commit()`` — an atomic supersede, not two independent
  writes a crash could tear apart.

Both paths funnel through :func:`get_current_node`, which always returns the
highest-``seq`` ``active`` row for an identity — so even a caller that only
ever uses plain ``create_node`` (never bothering to supersede) gets a sane
"what's current" answer; ``replace_node_revision`` additionally leaves an
explicit historical annotation of what replaced what.

UNRESOLVED EDGES
----------------

``research_edges`` rows are created against STABLE identities
(``from_node_type``/``from_identity_key``, ``to_node_type``/
``to_identity_key``), not specific node ids — an edge can be declared before
either endpoint has ever been ingested as a node (e.g. a claim cites a
Zotero key nobody has run ``ingest_document``/``resolve_citation_ref`` on
yet). :func:`create_edge` resolves each side to the CURRENT node for that
identity if one exists (``from_node_id``/``to_node_id``); when one is
missing the edge is stored anyway with that column ``NULL`` — an
"unresolved" edge, never silently dropped. :func:`get_unresolved_edges`
surfaces every edge with at least one ``NULL`` endpoint. Creating a node
whose identity matches a pending edge's ``NULL`` side auto-resolves it (see
:func:`_resolve_pending_edges_for_identity`, called from every write path)
— the moment the target shows up, the edge catches up without a separate
reconciliation pass.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

# Shared helpers from the parent db package — available at import time
# because this module is imported at the BOTTOM of db/__init__.py, after
# these names are already defined. Mirrors db.proposal_lineage's identical
# pattern (see that module's own docstring note).
from meridian.db import _new_id, _row_to_dict
from meridian.research_graph import (
    ARTIFACT_DOCUMENT_LINEAGE_EDGE_KINDS,
    CLAIM_EVIDENCE_EDGE_KINDS,
    EDGE_TYPES,
    validate_edge_kind,
    validate_node_type,
)

_NODE_STATUSES = frozenset({"active", "superseded"})


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
    convention (see ``proposal_lineage._is_lineage_unique_violation``)."""
    msg = str(exc).lower()
    return "unique" in msg or "duplicate key" in msg


# ---------------------------------------------------------------------------
# Migration — guarded, idempotent, not inline in either base schema literal
# (the 2026-07-04 outage rule: no unguarded CREATE INDEX on a
# migration-added column/table in CREATE_TABLES/CREATE_TABLES_CORE).
# Mirrored on Postgres by pg_adapter._migrate_pg_research_graph.
# ---------------------------------------------------------------------------


async def _migrate_research_graph(db: aiosqlite.Connection) -> None:
    """b558892a — create research_nodes / research_edges if absent."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS research_nodes (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            node_type TEXT NOT NULL CHECK (node_type IN (
                'claim', 'citation', 'code', 'run', 'output', 'document', 'decision'
            )),
            identity_key TEXT NOT NULL,
            external_ref TEXT,
            revision TEXT,
            title TEXT,
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded')),
            seq INTEGER NOT NULL DEFAULT 1,
            supersedes_id TEXT,
            superseded_by TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT
        )"""
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_research_nodes_identity_revision "
        "ON research_nodes(project_id, node_type, identity_key, COALESCE(revision, ''))"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_nodes_identity "
        "ON research_nodes(project_id, node_type, identity_key)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_nodes_project "
        "ON research_nodes(project_id)"
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS research_edges (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            edge_kind TEXT NOT NULL CHECK (edge_kind IN (
                'supports', 'contradicts', 'evidences', 'cites', 'produces',
                'derived_from', 'documents', 'implements', 'references'
            )),
            from_node_type TEXT NOT NULL,
            from_identity_key TEXT NOT NULL,
            from_node_id TEXT,
            to_node_type TEXT NOT NULL,
            to_identity_key TEXT NOT NULL,
            to_node_id TEXT,
            label TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at TEXT
        )"""
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_research_edges_unique "
        "ON research_edges(project_id, edge_kind, from_node_type, from_identity_key, "
        "to_node_type, to_identity_key)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_edges_from "
        "ON research_edges(project_id, from_node_type, from_identity_key)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_edges_to "
        "ON research_edges(project_id, to_node_type, to_identity_key)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_edges_unresolved "
        "ON research_edges(project_id, from_node_id, to_node_id)"
    )
    await db.commit()


def _row_to_node(row: Any) -> "dict[str, Any] | None":
    """Row -> dict with the JSON ``external_ref`` column deserialized back
    into a dict (mirrors decision_evidence's deserialize-on-read contract
    for its ``pointer`` column)."""
    d = _row_to_dict(row)
    if d is None:
        return None
    raw_ref = d.get("external_ref")
    if isinstance(raw_ref, str):
        try:
            d["external_ref"] = json.loads(raw_ref)
        except (ValueError, TypeError):
            d["external_ref"] = None
    return d


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


async def _find_node_by_natural_key(
    db: aiosqlite.Connection,
    project_id: str,
    node_type: str,
    identity_key: str,
    revision: "str | None",
) -> "dict[str, Any] | None":
    async with db.execute(
        "SELECT * FROM research_nodes WHERE project_id = ? AND node_type = ? "
        "AND identity_key = ? AND COALESCE(revision, '') = COALESCE(?, '')",
        (project_id, node_type, identity_key, revision),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_node(row)


async def _next_node_sequence(
    db: aiosqlite.Connection, project_id: str, node_type: str, identity_key: str
) -> int:
    """Per-``(project_id, node_type, identity_key)`` monotonic counter —
    mirrors ``proposal_lineage._next_lineage_sequence``'s identical
    ``COALESCE(MAX(seq), 0) + 1`` pattern."""
    async with db.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM research_nodes "
        "WHERE project_id = ? AND node_type = ? AND identity_key = ?",
        (project_id, node_type, identity_key),
    ) as cur:
        row = await cur.fetchone()
    return int((row["next_seq"] if row is not None else 1) or 1)


async def create_node(
    db: aiosqlite.Connection,
    project_id: str,
    node_type: str,
    identity_key: str,
    *,
    external_ref: "dict[str, Any] | None" = None,
    revision: "str | None" = None,
    title: "str | None" = None,
    created_by: "str | None" = None,
    supersedes_id: "str | None" = None,
) -> dict[str, Any]:
    """Append one typed node row. Returns the stored (or already-existing) row.

    Idempotent on the exact ``(project_id, node_type, identity_key,
    revision)`` tuple — a repeat call with the SAME identity+revision
    returns the existing row, never a duplicate. A DIFFERENT ``revision`` of
    the SAME ``identity_key`` is always a NEW row (this is the append-only
    "just record it" path — see the module docstring's write-semantics
    section; use :func:`replace_node_revision` for an explicit atomic
    supersede instead).

    ``supersedes_id`` (optional): when given, the OLD row (an EXACT id) is
    atomically flipped to ``status='superseded'`` in the SAME commit as this
    insert — mirrors ``decision_evidence.create_decision_evidence``'s
    identical parameter. Prefer calling :func:`replace_node_revision`, which
    validates the old row first; this parameter exists so that function can
    share this insert logic without duplicating it. Also honored on the
    idempotent short-circuit path (the exact revision already existed as a
    separate row) so a caller "replacing" back to a previously-seen revision
    still gets the old row retired.

    Raises ``ValueError`` on an unknown ``node_type``, a blank
    ``identity_key``, or a ``title`` that looks like a secret (fail-closed,
    matching every other DB write path that persists caller-supplied text).
    """
    node_type = validate_node_type(node_type)
    identity_key = (identity_key or "").strip()
    if not identity_key:
        raise ValueError("create_node requires a non-empty identity_key")
    if title is not None:
        from meridian.secret_redaction import check_for_secrets  # noqa: PLC0415
        check_for_secrets(title, context="research graph node title")

    norm_revision: "str | None" = revision.strip() if isinstance(revision, str) else revision
    if isinstance(norm_revision, str) and not norm_revision:
        norm_revision = None
    external_ref_json = (
        json.dumps(external_ref, ensure_ascii=False, sort_keys=True)
        if external_ref is not None else None
    )

    existing = await _find_node_by_natural_key(db, project_id, node_type, identity_key, norm_revision)
    if existing is not None:
        if supersedes_id and supersedes_id != existing["id"]:
            await db.execute(
                "UPDATE research_nodes SET status = 'superseded', superseded_by = ?, "
                "updated_at = ? WHERE id = ? AND project_id = ? AND status != 'superseded'",
                (existing["id"], _now_iso(), supersedes_id, project_id),
            )
            await db.commit()
        return existing

    seq = await _next_node_sequence(db, project_id, node_type, identity_key)
    nid = _new_id()
    try:
        await db.execute(
            "INSERT INTO research_nodes "
            "(id, project_id, node_type, identity_key, external_ref, revision, "
            "title, status, seq, supersedes_id, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (
                nid, project_id, node_type, identity_key, external_ref_json,
                norm_revision, title, seq, supersedes_id, created_by,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — classified below
        if _is_unique_violation(exc):
            # Lost a create race against another caller writing the SAME
            # (project, node_type, identity_key, revision) tuple — nothing of
            # ours was written, so just hand back the winner.
            winner = await _find_node_by_natural_key(
                db, project_id, node_type, identity_key, norm_revision
            )
            if winner is not None:
                return winner
        raise

    if supersedes_id:
        await db.execute(
            "UPDATE research_nodes SET status = 'superseded', superseded_by = ?, "
            "updated_at = ? WHERE id = ? AND project_id = ?",
            (nid, _now_iso(), supersedes_id, project_id),
        )
    await db.commit()

    await _resolve_pending_edges_for_identity(db, project_id, node_type, identity_key, nid)

    created = await get_node(db, project_id, nid)
    assert created is not None  # just written
    return created


async def replace_node_revision(
    db: aiosqlite.Connection,
    project_id: str,
    old_node_id: str,
    *,
    external_ref: "dict[str, Any] | None" = None,
    revision: "str | None" = None,
    title: "str | None" = None,
    created_by: "str | None" = None,
) -> dict[str, Any]:
    """Atomic supersede: an EXACT, caller-supplied ``old_node_id`` (a real
    primary key — never a search/best-match result, same safety contract as
    ``decision_evidence.supersede_decision_evidence``) is retired and a new
    active revision of the SAME identity is created, in one call.

    ``external_ref``/``title`` default to the old row's values when omitted
    (a caller replacing just the revision doesn't have to re-supply
    everything). Raises ``ValueError`` if ``old_node_id`` doesn't exist in
    ``project_id``, or is already ``superseded`` (supersede the CURRENT row,
    not a retired one — prevents building a branching, ambiguous
    supersession chain).
    """
    old = await get_node(db, project_id, old_node_id)
    if old is None:
        raise ValueError(
            f"research node {old_node_id!r} not found in project {project_id!r}"
        )
    if old.get("status") == "superseded":
        raise ValueError(
            f"research node {old_node_id!r} is already superseded by "
            f"{old.get('superseded_by')!r} — replace the CURRENT node, not "
            "a retired revision"
        )
    return await create_node(
        db, project_id, old["node_type"], old["identity_key"],
        external_ref=external_ref if external_ref is not None else old.get("external_ref"),
        revision=revision,
        title=title if title is not None else old.get("title"),
        created_by=created_by,
        supersedes_id=old_node_id,
    )


async def get_node(
    db: aiosqlite.Connection, project_id: str, node_id: str
) -> "dict[str, Any] | None":
    """Fetch one research_nodes row by id, scoped to project_id."""
    async with db.execute(
        "SELECT * FROM research_nodes WHERE id = ? AND project_id = ?",
        (node_id, project_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_node(row)


async def get_current_node(
    db: aiosqlite.Connection, project_id: str, node_type: str, identity_key: str
) -> "dict[str, Any] | None":
    """The highest-``seq`` ``active`` row for this identity, or ``None`` if
    no active node has ever been created for it. See the module docstring's
    write-semantics section for why ``seq`` (not ``created_at``) is the
    deterministic tiebreaker."""
    node_type = validate_node_type(node_type)
    identity_key = (identity_key or "").strip()
    async with db.execute(
        "SELECT * FROM research_nodes WHERE project_id = ? AND node_type = ? "
        "AND identity_key = ? AND status = 'active' ORDER BY seq DESC LIMIT 1",
        (project_id, node_type, identity_key),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_node(row)


async def list_node_revisions(
    db: aiosqlite.Connection, project_id: str, node_type: str, identity_key: str
) -> list[dict[str, Any]]:
    """Every row (active AND superseded) for this identity, oldest first —
    the full, append-only revision history. Nothing this module writes is
    ever hard-deleted, so this is always the complete story."""
    node_type = validate_node_type(node_type)
    identity_key = (identity_key or "").strip()
    async with db.execute(
        "SELECT * FROM research_nodes WHERE project_id = ? AND node_type = ? "
        "AND identity_key = ? ORDER BY seq ASC",
        (project_id, node_type, identity_key),
    ) as cur:
        rows = await cur.fetchall()
    return [n for n in (_row_to_node(r) for r in rows) if n is not None]


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------


def _extract_ref(ref: Any, *, what: str) -> tuple[str, str]:
    """Validate a ``{node_type, identity_key}`` edge endpoint reference;
    return the normalized ``(node_type, identity_key)`` pair."""
    if not isinstance(ref, dict):
        raise ValueError(f"{what} must be an object with node_type/identity_key")
    node_type = validate_node_type(ref.get("node_type"))
    identity_key = ref.get("identity_key")
    identity_key = identity_key.strip() if isinstance(identity_key, str) else ""
    if not identity_key:
        raise ValueError(f"{what} requires a non-empty identity_key")
    return node_type, identity_key


async def _find_edge_by_natural_key(
    db: aiosqlite.Connection,
    project_id: str,
    edge_kind: str,
    from_type: str,
    from_key: str,
    to_type: str,
    to_key: str,
) -> "dict[str, Any] | None":
    async with db.execute(
        "SELECT * FROM research_edges WHERE project_id = ? AND edge_kind = ? "
        "AND from_node_type = ? AND from_identity_key = ? "
        "AND to_node_type = ? AND to_identity_key = ?",
        (project_id, edge_kind, from_type, from_key, to_type, to_key),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def _resolve_pending_edges_for_identity(
    db: aiosqlite.Connection,
    project_id: str,
    node_type: str,
    identity_key: str,
    node_id: str,
) -> None:
    """Auto-resolve any edge that names ``(node_type, identity_key)`` on a
    side that is still ``NULL`` (the target didn't exist as a node yet when
    the edge was declared) — called from every node write path so a
    previously-unresolved edge catches up the moment its target appears,
    with no separate reconciliation pass required."""
    now = _now_iso()
    await db.execute(
        "UPDATE research_edges SET from_node_id = ?, "
        "resolved_at = CASE WHEN to_node_id IS NOT NULL THEN ? ELSE resolved_at END "
        "WHERE project_id = ? AND from_node_type = ? AND from_identity_key = ? "
        "AND from_node_id IS NULL",
        (node_id, now, project_id, node_type, identity_key),
    )
    await db.execute(
        "UPDATE research_edges SET to_node_id = ?, "
        "resolved_at = CASE WHEN from_node_id IS NOT NULL THEN ? ELSE resolved_at END "
        "WHERE project_id = ? AND to_node_type = ? AND to_identity_key = ? "
        "AND to_node_id IS NULL",
        (node_id, now, project_id, node_type, identity_key),
    )
    await db.commit()


async def create_edge(
    db: aiosqlite.Connection,
    project_id: str,
    edge_kind: str,
    from_ref: dict[str, Any],
    to_ref: dict[str, Any],
    *,
    label: "str | None" = None,
    created_by: "str | None" = None,
) -> dict[str, Any]:
    """Append one typed edge. ``from_ref``/``to_ref`` are STABLE identity
    references (``{"node_type": ..., "identity_key": ...}``), not specific
    node ids — see the module docstring's "unresolved edges" section.
    Idempotent on the exact ``(project_id, edge_kind, from_ref, to_ref)``
    tuple. Raises ``ValueError`` on an unknown ``edge_kind``/``node_type``,
    a blank ``identity_key`` on either side, a self-loop (identical
    ``node_type``+``identity_key`` on both sides), or a ``label`` that looks
    like a secret.

    Each side is resolved to its CURRENT node (:func:`get_current_node`) if
    one exists; when one doesn't, that column is stored ``NULL`` (an
    unresolved edge — never rejected, never silently dropped) and
    ``resolved_at`` stays ``NULL`` until both sides eventually resolve
    (either now or via :func:`_resolve_pending_edges_for_identity` the next
    time a matching node is created).
    """
    edge_kind = validate_edge_kind(edge_kind)
    from_type, from_key = _extract_ref(from_ref, what="from_ref")
    to_type, to_key = _extract_ref(to_ref, what="to_ref")
    if from_type == to_type and from_key == to_key:
        raise ValueError(
            f"cannot create a {edge_kind!r} edge from a node to itself "
            f"({from_type}:{from_key})"
        )
    if label is not None:
        from meridian.secret_redaction import check_for_secrets  # noqa: PLC0415
        check_for_secrets(label, context="research graph edge label")

    existing = await _find_edge_by_natural_key(
        db, project_id, edge_kind, from_type, from_key, to_type, to_key
    )
    if existing is not None:
        return existing

    from_node = await get_current_node(db, project_id, from_type, from_key)
    to_node = await get_current_node(db, project_id, to_type, to_key)
    now = _now_iso()
    resolved_at = now if (from_node is not None and to_node is not None) else None

    eid = _new_id()
    try:
        await db.execute(
            "INSERT INTO research_edges "
            "(id, project_id, edge_kind, from_node_type, from_identity_key, from_node_id, "
            "to_node_type, to_identity_key, to_node_id, label, created_by, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                eid, project_id, edge_kind, from_type, from_key,
                from_node["id"] if from_node else None,
                to_type, to_key,
                to_node["id"] if to_node else None,
                label, created_by, resolved_at,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — classified below
        if _is_unique_violation(exc):
            winner = await _find_edge_by_natural_key(
                db, project_id, edge_kind, from_type, from_key, to_type, to_key
            )
            if winner is not None:
                return winner
        raise
    await db.commit()
    created = await _find_edge_by_natural_key(
        db, project_id, edge_kind, from_type, from_key, to_type, to_key
    )
    return created or {"id": eid}


async def get_unresolved_edges(
    db: aiosqlite.Connection, project_id: str, *, edge_kind: "str | None" = None
) -> list[dict[str, Any]]:
    """Every edge with at least one ``NULL`` endpoint — the target node
    hasn't been ingested yet. Optionally narrowed to one ``edge_kind``."""
    if edge_kind is not None:
        edge_kind = validate_edge_kind(edge_kind)
        sql = (
            "SELECT * FROM research_edges WHERE project_id = ? AND edge_kind = ? "
            "AND (from_node_id IS NULL OR to_node_id IS NULL) "
            "ORDER BY created_at ASC, id ASC"
        )
        params: tuple[Any, ...] = (project_id, edge_kind)
    else:
        sql = (
            "SELECT * FROM research_edges WHERE project_id = ? "
            "AND (from_node_id IS NULL OR to_node_id IS NULL) "
            "ORDER BY created_at ASC, id ASC"
        )
        params = (project_id,)
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [e for e in (_row_to_dict(r) for r in rows) if e is not None]


async def get_edges_for_identity(
    db: aiosqlite.Connection,
    project_id: str,
    node_type: str,
    identity_key: str,
    *,
    role: "str | None" = None,
) -> list[dict[str, Any]]:
    """Raw edges touching ``(node_type, identity_key)``. ``role`` narrows to
    ``'from'`` or ``'to'``; omitted (default) returns edges where the
    identity appears on EITHER side, mirroring
    ``proposal_lineage.get_proposal_lineage_links``."""
    node_type = validate_node_type(node_type)
    identity_key = (identity_key or "").strip()
    if role == "from":
        sql = (
            "SELECT * FROM research_edges WHERE project_id = ? AND from_node_type = ? "
            "AND from_identity_key = ? ORDER BY created_at ASC, id ASC"
        )
        params: tuple[Any, ...] = (project_id, node_type, identity_key)
    elif role == "to":
        sql = (
            "SELECT * FROM research_edges WHERE project_id = ? AND to_node_type = ? "
            "AND to_identity_key = ? ORDER BY created_at ASC, id ASC"
        )
        params = (project_id, node_type, identity_key)
    elif role is None:
        sql = (
            "SELECT * FROM research_edges WHERE project_id = ? AND "
            "((from_node_type = ? AND from_identity_key = ?) OR "
            "(to_node_type = ? AND to_identity_key = ?)) "
            "ORDER BY created_at ASC, id ASC"
        )
        params = (project_id, node_type, identity_key, node_type, identity_key)
    else:
        raise ValueError(f"role must be None, 'from', or 'to', got {role!r}")
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [e for e in (_row_to_dict(r) for r in rows) if e is not None]


# ---------------------------------------------------------------------------
# claim-to-evidence
# ---------------------------------------------------------------------------


async def get_claim_evidence(
    db: aiosqlite.Connection,
    project_id: str,
    claim_identity_key: str,
    *,
    edge_kinds: "tuple[str, ...] | None" = None,
) -> list[dict[str, Any]]:
    """All edges asserting evidence for/against ``claim_identity_key``
    (``edge_kind`` in :data:`meridian.research_graph.CLAIM_EVIDENCE_EDGE_KINDS`
    by default — ``supports``/``contradicts``/``evidences``, all of which
    point evidence -> claim). Each returned dict carries the raw edge fields
    PLUS ``evidence_node`` (the resolved evidence node, or ``None`` when the
    edge is unresolved) and ``resolved`` (bool) — an unresolved edge is
    included, not silently dropped, matching the module's "expose unresolved
    edges" contract.
    """
    claim_identity_key = (claim_identity_key or "").strip()
    if not claim_identity_key:
        raise ValueError("get_claim_evidence requires a non-empty claim_identity_key")
    kinds = tuple(validate_edge_kind(k) for k in (edge_kinds or CLAIM_EVIDENCE_EDGE_KINDS))
    placeholders = ", ".join("?" for _ in kinds)
    async with db.execute(
        f"SELECT * FROM research_edges WHERE project_id = ? AND to_node_type = 'claim' "
        f"AND to_identity_key = ? AND edge_kind IN ({placeholders}) "
        f"ORDER BY created_at ASC, id ASC",
        [project_id, claim_identity_key, *kinds],
    ) as cur:
        rows = await cur.fetchall()
    edges = [e for e in (_row_to_dict(r) for r in rows) if e is not None]
    enriched: list[dict[str, Any]] = []
    for edge in edges:
        evidence_node = None
        if edge.get("from_node_id"):
            evidence_node = await get_node(db, project_id, edge["from_node_id"])
        enriched.append({**edge, "evidence_node": evidence_node, "resolved": evidence_node is not None})
    return enriched


# ---------------------------------------------------------------------------
# artifact-to-document lineage
# ---------------------------------------------------------------------------

_MAX_LINEAGE_HOPS_DEFAULT = 50


async def get_lineage_subgraph(
    db: aiosqlite.Connection,
    project_id: str,
    node_type: str,
    identity_key: str,
    *,
    edge_kinds: "tuple[str, ...] | None" = None,
    direction: str = "forward",
    max_hops: int = _MAX_LINEAGE_HOPS_DEFAULT,
) -> dict[str, Any]:
    """BFS over ``research_edges`` starting at ``(node_type, identity_key)``,
    returning every node/edge reachable within ``max_hops``.

    ``direction='forward'`` follows edges from their ``from_`` side to their
    ``to_`` side (the natural production direction: run -> produces ->
    output -> documents -> document). ``direction='backward'`` walks the
    other way (starting from a document and tracing back to what produced
    it). ``edge_kinds`` restricts which edge kinds are followed (defaults to
    every :data:`meridian.research_graph.EDGE_TYPES` value).

    Unlike a proposal's single-parent ancestor chain, this graph is a
    general many-to-many DAG (a run can produce many outputs; an output can
    document many documents), so the result is the full reachable
    SUBGRAPH — ``{"nodes": [...], "edges": [...]}`` — not one linear path.
    Nodes are resolved via :func:`get_current_node` (so a superseded
    revision along the way is transparently reported as its current
    replacement); an identity with no node ever created for it is simply
    absent from ``nodes`` (its edges still appear in ``edges``).
    """
    node_type = validate_node_type(node_type)
    identity_key = (identity_key or "").strip()
    if not identity_key:
        raise ValueError("get_lineage_subgraph requires a non-empty identity_key")
    if direction not in ("forward", "backward"):
        raise ValueError(f"direction must be 'forward' or 'backward', got {direction!r}")
    kinds = tuple(validate_edge_kind(k) for k in (edge_kinds or sorted(EDGE_TYPES)))
    placeholders = ", ".join("?" for _ in kinds)

    start_ref = (node_type, identity_key)
    visited_refs: set[tuple[str, str]] = {start_ref}
    frontier: list[tuple[str, str]] = [start_ref]
    collected_edges: list[dict[str, Any]] = []
    seen_edge_ids: set[str] = set()
    hops = 0

    while frontier and hops < max_hops:
        hops += 1
        next_frontier: list[tuple[str, str]] = []
        for cur_type, cur_key in frontier:
            if direction == "forward":
                sql = (
                    f"SELECT * FROM research_edges WHERE project_id = ? "
                    f"AND from_node_type = ? AND from_identity_key = ? "
                    f"AND edge_kind IN ({placeholders})"
                )
            else:
                sql = (
                    f"SELECT * FROM research_edges WHERE project_id = ? "
                    f"AND to_node_type = ? AND to_identity_key = ? "
                    f"AND edge_kind IN ({placeholders})"
                )
            async with db.execute(sql, [project_id, cur_type, cur_key, *kinds]) as cur:
                rows = await cur.fetchall()
            for row in rows:
                edge = _row_to_dict(row)
                if edge is None:
                    continue
                if edge["id"] not in seen_edge_ids:
                    seen_edge_ids.add(edge["id"])
                    collected_edges.append(edge)
                nxt = (
                    (edge["to_node_type"], edge["to_identity_key"]) if direction == "forward"
                    else (edge["from_node_type"], edge["from_identity_key"])
                )
                if nxt not in visited_refs:
                    visited_refs.add(nxt)
                    next_frontier.append(nxt)
        frontier = next_frontier

    nodes: list[dict[str, Any]] = []
    for n_type, n_key in sorted(visited_refs):
        n = await get_current_node(db, project_id, n_type, n_key)
        if n is not None:
            nodes.append(n)
    collected_edges.sort(key=lambda e: (e.get("created_at") or "", e.get("id") or ""))
    return {"nodes": nodes, "edges": collected_edges}


async def get_artifact_document_lineage(
    db: aiosqlite.Connection,
    project_id: str,
    node_type: str,
    identity_key: str,
    *,
    max_hops: int = _MAX_LINEAGE_HOPS_DEFAULT,
) -> dict[str, Any]:
    """Convenience: the forward lineage subgraph from an artifact
    (typically ``code``/``run``/``output``) toward the document(s) it ends
    up documented in — the sprint item's "artifact-to-document lineage"
    query. Equivalent to :func:`get_lineage_subgraph` restricted to
    :data:`meridian.research_graph.ARTIFACT_DOCUMENT_LINEAGE_EDGE_KINDS`
    (``produces``/``derived_from``/``documents``) in the ``forward``
    direction.
    """
    return await get_lineage_subgraph(
        db, project_id, node_type, identity_key,
        edge_kinds=ARTIFACT_DOCUMENT_LINEAGE_EDGE_KINDS,
        direction="forward",
        max_hops=max_hops,
    )
