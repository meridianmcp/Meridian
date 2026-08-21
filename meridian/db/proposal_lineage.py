"""5a744f81 — first-class proposal lineage: typed parent/successor links
between ``workspace_proposals`` rows.

Before this module a proposal had exactly one informal way to reference
another proposal: baking a short id into free text (title/body/tags), or —
for a promoted proposal — the single ``promoted_to_sprint_item_id`` column
(a proposal -> sprint item link, not a proposal -> proposal one). Neither
gives a durable, typed, queryable answer to "which proposal did THIS one
come from, and how" (a rewrite? a fork? a duplicate report? a reply?).

``proposal_lineage`` is the first-class replacement for that gap, ADDITIVE
and layered entirely on top of the existing proposal system:

  * Explicit parent/related-proposal identity: every row names a
    ``from_proposal_id`` (the newer/"this" proposal) and a
    ``to_proposal_id`` (the proposal it relates to).
  * A closed, typed ``relation_type`` enum (see :data:`VALID_RELATION_TYPES`):
    supersedes | refines | forks | continues | duplicates | responds_to.
  * Deterministic version/sequence metadata: each row's ``sequence`` is a
    per-``to_proposal_id`` monotonic counter (mirrors
    ``db.workspace._append_proposal_event``'s per-proposal ``sequence``
    pattern), so "who has related to proposal X, and in what order" is a
    stable, queryable fact. :func:`get_proposal_ancestors` additionally
    walks the graph to produce one deterministic ordered chain.
  * Tenant/workspace scoping: a relation is validated to stay within a
    single tenant (see :func:`link_proposal_lineage`) and never crosses a
    tenant/workspace boundary, mirroring ``workspace_proposals.tenant_id``
    and ``db.workspace._ws_tenant_clause``'s NULL-matches-legacy-rows rule.
  * Idempotency + uniqueness: the same ``(tenant_id, from_proposal_id,
    to_proposal_id, relation_type)`` tuple can only ever exist once — a
    repeat call is a no-op that returns the existing row, never a duplicate
    insert. Mirrors ``db.workspace.add_workspace_proposal``'s
    ``idempotency_key`` race-handling pattern (pre-check, insert, catch a
    UNIQUE violation on a lost race, re-fetch the winner).
  * Cycle prevention: creating an edge that would let the graph loop back
    on itself (directly, e.g. A -> A, or transitively, e.g. A supersedes B
    supersedes A) is rejected with ``ValueError`` before anything is
    written.

This module does NOT touch, reinterpret, or depend on the meaning of
``proposal_events``, ``workspace_proposals.family_id``,
``proposal_evidence_links``, or ``promote_workspace_proposal`` — those keep
their EXACT existing behaviour. ``family_id`` in particular remains a plain
compatibility grouping field; it is never read or written by this module.
Proposal-to-evidence links (``db.proposal_links``, a proposal pointing at a
note/finding/sprint_item/decision/artifact) are a different relationship
entirely from proposal-to-PROPOSAL lineage — this module is new and
sits alongside that one, not on top of it.

Public surface:
  * :func:`link_proposal_lineage` — create one typed lineage relation
    (idempotent, cycle- and tenant-checked).
  * :func:`unlink_proposal_lineage` — remove one relation by id.
  * :func:`get_proposal_lineage_links` — raw relation rows touching one
    proposal (either endpoint).
  * :func:`get_proposal_successors` — relations where a proposal is the
    ``to_proposal_id`` (i.e. its known successors), in deterministic
    ``sequence`` order.
  * :func:`get_proposal_ancestors` — the single deterministic parent chain
    walking backwards (``from_proposal_id`` -> ``to_proposal_id``) from a
    proposal to its root.

Imported at the BOTTOM of db/__init__.py (after workspace_proposals /
``_ws_tenant_clause`` are already defined), mirroring db.proposal_links.
"""
from __future__ import annotations

from typing import Any

import aiosqlite

# Shared helpers from the parent db package — available at import time
# because this module is imported at the bottom of db/__init__.py, after
# these names are already defined. Mirrors db.proposal_links's identical
# pattern (see that module's own docstring note).
from meridian.db import (  # noqa: PLC0415
    _new_id,
    _row_to_dict,
    _ws_tenant_clause,
)

# Closed set of relation types this schema accepts. Order here is the
# canonical documentation order (also used verbatim in error messages).
VALID_RELATION_TYPES: tuple[str, ...] = (
    "supersedes",
    "refines",
    "forks",
    "continues",
    "duplicates",
    "responds_to",
)
_VALID_RELATION_TYPES = frozenset(VALID_RELATION_TYPES)

# Safety bound for the graph walks in _lineage_reaches / get_proposal_ancestors
# — a real lineage chain is expected to be a handful of hops; this only exists
# to guarantee termination against a pathological/corrupted data set.
_MAX_LINEAGE_HOPS = 2000


async def _migrate_proposal_lineage(db: aiosqlite.Connection) -> None:
    """5a744f81 — create proposal_lineage if absent.

    Guarded migration (no inline CREATE INDEX in the unguarded base schema
    literal — the 2026-07-04 outage rule): the table AND its indexes live
    here, called unconditionally on every startup (idempotent
    ``IF NOT EXISTS``), so both a fresh DB and an upgrading existing DB pick
    it up. Mirrors ``db.proposal_links._migrate_proposal_evidence_links``
    exactly — not present in either base ``CREATE_TABLES`` literal, this
    guarded migration is the only creation path on SQLite. Mirrored on the
    Postgres side by ``pg_adapter._migrate_pg_proposal_lineage``.

    The UNIQUE index on ``(COALESCE(tenant_id, ''), from_proposal_id,
    to_proposal_id, relation_type)`` is what makes
    :func:`link_proposal_lineage` idempotent, and is also the schema-level
    half of the uniqueness contract for the relation model. It is
    COALESCE-normalized (not a plain column index) so self-host rows
    (``tenant_id`` always NULL) get a REAL duplicate-prevention guarantee
    too — a plain multi-column UNIQUE index never treats two NULLs as
    equal, mirroring ``idx_workspace_proposals_idempotency`` in
    ``db/migrations.py``.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS proposal_lineage (
            id TEXT PRIMARY KEY,
            tenant_id TEXT,
            from_proposal_id TEXT NOT NULL,
            to_proposal_id TEXT NOT NULL,
            relation_type TEXT NOT NULL CHECK (relation_type IN (
                'supersedes', 'refines', 'forks', 'continues',
                'duplicates', 'responds_to'
            )),
            sequence INTEGER NOT NULL DEFAULT 1,
            label TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_proposal_lineage_unique "
        "ON proposal_lineage(COALESCE(tenant_id, ''), from_proposal_id, "
        "to_proposal_id, relation_type)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_proposal_lineage_from "
        "ON proposal_lineage(from_proposal_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_proposal_lineage_to "
        "ON proposal_lineage(to_proposal_id)"
    )
    await db.commit()


def _is_lineage_unique_violation(exc: BaseException) -> bool:
    """Heuristic: does ``exc`` look like a UNIQUE/duplicate-key violation?

    Matches sqlite3's ``UNIQUE constraint failed`` and psycopg3's
    ``UniqueViolation`` (``duplicate key value violates unique
    constraint``). Never raises. Mirrors
    ``db.workspace._is_proposal_unique_violation`` (a private helper of a
    sibling module — duplicated rather than imported, matching this
    codebase's existing convention of one small copy per module, e.g.
    ``db.batch_management._is_unique_violation``)."""
    msg = str(exc).lower()
    return "unique" in msg or "duplicate key" in msg


async def _get_proposal_row(
    db: aiosqlite.Connection, proposal_id: str
) -> dict[str, Any] | None:
    """Minimal existence + tenant lookup for one workspace_proposals row."""
    async with db.execute(
        "SELECT id, tenant_id FROM workspace_proposals WHERE id = ?",
        (proposal_id,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def _find_lineage_relation(
    db: aiosqlite.Connection,
    tenant_id: str | None,
    from_proposal_id: str,
    to_proposal_id: str,
    relation_type: str,
) -> dict[str, Any] | None:
    """Look up an existing relation by its full natural key, COALESCE-
    normalized on ``tenant_id`` the same way the unique index is (so a NULL
    tenant_id on self-host matches like-for-like instead of "no two NULLs
    are equal")."""
    async with db.execute(
        "SELECT * FROM proposal_lineage WHERE COALESCE(tenant_id, '') = "
        "COALESCE(?, '') AND from_proposal_id = ? AND to_proposal_id = ? "
        "AND relation_type = ?",
        (tenant_id, from_proposal_id, to_proposal_id, relation_type),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def _next_lineage_sequence(db: aiosqlite.Connection, to_proposal_id: str) -> int:
    """Per-``to_proposal_id`` monotonic counter — the Nth relation recorded
    against this proposal, in creation order. Mirrors
    ``db.workspace._append_proposal_event``'s identical
    ``COALESCE(MAX(sequence), 0) + 1`` pattern (scoped there to
    ``proposal_id``, here to ``to_proposal_id``)."""
    async with db.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence "
        "FROM proposal_lineage WHERE to_proposal_id = ?",
        (to_proposal_id,),
    ) as cur:
        row = await cur.fetchone()
    return int((row["next_sequence"] if row is not None else 1) or 1)


async def _lineage_reaches(
    db: aiosqlite.Connection, start_id: str, target_id: str
) -> bool:
    """Breadth-first search over EXISTING ``from_proposal_id ->
    to_proposal_id`` edges (every relation_type pooled together — a
    lineage graph must stay acyclic regardless of which specific relation
    types make up the cycle): can ``start_id`` reach ``target_id`` by
    following edges in that direction?

    Used by :func:`link_proposal_lineage` to decide whether inserting a NEW
    edge ``from_proposal_id -> to_proposal_id`` would close a cycle: that is
    true exactly when ``to_proposal_id`` can already reach
    ``from_proposal_id`` by the existing graph — i.e. this call is made with
    ``start_id=to_proposal_id, target_id=from_proposal_id``.
    """
    if start_id == target_id:
        return True
    visited = {start_id}
    frontier = [start_id]
    hops = 0
    while frontier and hops < _MAX_LINEAGE_HOPS:
        hops += 1
        placeholders = ", ".join("?" for _ in frontier)
        async with db.execute(
            f"SELECT to_proposal_id FROM proposal_lineage "
            f"WHERE from_proposal_id IN ({placeholders})",
            frontier,
        ) as cur:
            rows = await cur.fetchall()
        next_frontier: list[str] = []
        for r in rows:
            d = _row_to_dict(r) or {}
            nxt = d.get("to_proposal_id")
            if not nxt or nxt in visited:
                continue
            if nxt == target_id:
                return True
            visited.add(nxt)
            next_frontier.append(nxt)
        frontier = next_frontier
    return False


async def link_proposal_lineage(
    db: aiosqlite.Connection,
    from_proposal_id: str,
    to_proposal_id: str,
    relation_type: str,
    *,
    tenant_id: str | None = None,
    label: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """5a744f81 — persist a durable, typed lineage relation between two
    ``workspace_proposals`` rows. Returns the stored (or already-existing)
    relation row.

    ``from_proposal_id`` is the newer/"this" proposal; ``to_proposal_id`` is
    the proposal it relates to (its parent in the relation). ``relation_type``
    must be one of :data:`VALID_RELATION_TYPES`.

    Validation, in order:

    1. Both ids must be non-empty and distinct — a proposal cannot relate to
       itself (``ValueError``).
    2. Both proposals must exist in ``workspace_proposals`` (``ValueError``
       otherwise — never silently links a dangling id).
    3. Both proposals must belong to the SAME tenant (comparing their stored
       ``tenant_id`` directly) — cross-tenant links are refused
       (``ValueError``). When the caller also passes ``tenant_id`` (the
       authenticated scope, e.g. from an MCP handler), each endpoint's
       stored tenant_id — when not NULL/legacy — must additionally match
       that scope, so a caller cannot link two same-tenant-as-each-other
       proposals that both belong to a DIFFERENT tenant than the one it is
       authenticated as.
    4. Idempotency: if a relation with the exact same
       ``(tenant_id, from_proposal_id, to_proposal_id, relation_type)``
       already exists, that row is returned UNCHANGED — no duplicate write.
       A genuine concurrent race (two callers passing the pre-check before
       either commits) is caught via the backing UNIQUE constraint and
       resolved the same way: the loser re-fetches and returns the winner's
       row rather than raising.
    5. Cycle prevention: if ``to_proposal_id`` can already reach
       ``from_proposal_id`` through existing relations (of ANY type), this
       new edge would close a cycle — rejected with ``ValueError`` before
       anything is written. This is what makes "A supersedes B supersedes A"
       impossible, and also blocks longer indirect cycles the same way.

    The new row's ``sequence`` is assigned automatically — see
    :func:`_next_lineage_sequence`.
    """
    relation_type = (relation_type or "").strip().lower()
    if relation_type not in _VALID_RELATION_TYPES:
        raise ValueError(
            f"relation_type must be one of {VALID_RELATION_TYPES}, "
            f"got {relation_type!r}"
        )
    from_proposal_id = (from_proposal_id or "").strip()
    to_proposal_id = (to_proposal_id or "").strip()
    if not from_proposal_id:
        raise ValueError("from_proposal_id must be a non-empty string")
    if not to_proposal_id:
        raise ValueError("to_proposal_id must be a non-empty string")
    if from_proposal_id == to_proposal_id:
        raise ValueError(
            f"Proposal '{from_proposal_id}' cannot have a lineage relation "
            "to itself"
        )
    if label is not None:
        from meridian.secret_redaction import check_for_secrets  # noqa: PLC0415
        check_for_secrets(label, context="proposal lineage label")

    from_row = await _get_proposal_row(db, from_proposal_id)
    if from_row is None:
        raise ValueError(f"proposal '{from_proposal_id}' does not exist")
    to_row = await _get_proposal_row(db, to_proposal_id)
    if to_row is None:
        raise ValueError(f"proposal '{to_proposal_id}' does not exist")

    from_tenant = from_row.get("tenant_id")
    to_tenant = to_row.get("tenant_id")
    if from_tenant != to_tenant:
        raise ValueError(
            f"Cannot link proposal '{from_proposal_id}' (tenant "
            f"{from_tenant!r}) to '{to_proposal_id}' (tenant {to_tenant!r}): "
            "lineage relations must not cross tenant/workspace boundaries"
        )
    if tenant_id is not None:
        for role, row_tenant, pid in (
            ("from_proposal_id", from_tenant, from_proposal_id),
            ("to_proposal_id", to_tenant, to_proposal_id),
        ):
            # A NULL stored tenant_id is a pre-isolation/self-host row and
            # matches any scope, mirroring _ws_tenant_clause's NULL-matches-
            # everything rule. A non-NULL stored tenant_id that disagrees
            # with the authenticated scope is a hard reject.
            if row_tenant is not None and row_tenant != tenant_id:
                raise ValueError(
                    f"{role} '{pid}' belongs to a different tenant than the "
                    "requesting scope — cannot create a cross-tenant lineage "
                    "relation"
                )

    # Caller-provided tenant_id (the authenticated scope) wins over the
    # rows' own stored tenant_id, mirroring the identical precedence used by
    # db.workspace.advance_workspace_proposal_status's _append_proposal_event
    # call. from_tenant == to_tenant is already guaranteed above, so either
    # is an equivalent fallback.
    effective_tenant_id = tenant_id if tenant_id is not None else from_tenant

    existing = await _find_lineage_relation(
        db, effective_tenant_id, from_proposal_id, to_proposal_id, relation_type
    )
    if existing is not None:
        return existing

    if await _lineage_reaches(db, to_proposal_id, from_proposal_id):
        raise ValueError(
            f"Cannot link '{from_proposal_id}' --{relation_type}--> "
            f"'{to_proposal_id}': '{to_proposal_id}' can already reach "
            f"'{from_proposal_id}' through existing proposal lineage — this "
            "relation would create a cycle"
        )

    sequence = await _next_lineage_sequence(db, to_proposal_id)
    lid = _new_id()
    try:
        await db.execute(
            "INSERT INTO proposal_lineage "
            "(id, tenant_id, from_proposal_id, to_proposal_id, relation_type, "
            "sequence, label, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lid,
                effective_tenant_id,
                from_proposal_id,
                to_proposal_id,
                relation_type,
                sequence,
                label,
                actor,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — classified below
        if _is_lineage_unique_violation(exc):
            # Lost a create race against another caller linking the SAME
            # (tenant, from, to, relation_type) tuple — nothing of ours was
            # written (a UNIQUE violation rejects the whole INSERT
            # statement on both backends), so just hand back the winner.
            winner = await _find_lineage_relation(
                db, effective_tenant_id, from_proposal_id, to_proposal_id,
                relation_type,
            )
            if winner is not None:
                return winner
        raise
    await db.commit()
    row = await _find_lineage_relation(
        db, effective_tenant_id, from_proposal_id, to_proposal_id, relation_type
    )
    return row or {"id": lid}


async def unlink_proposal_lineage(db: aiosqlite.Connection, link_id: str) -> bool:
    """5a744f81 — delete one lineage relation by id. Returns True if a row
    was removed. Mirrors ``db.proposal_links.unlink_proposal_evidence``."""
    async with db.execute(
        "SELECT 1 FROM proposal_lineage WHERE id = ?", (link_id,)
    ) as cur:
        existed = await cur.fetchone() is not None
    await db.execute("DELETE FROM proposal_lineage WHERE id = ?", (link_id,))
    await db.commit()
    return existed


async def get_proposal_lineage_links(
    db: aiosqlite.Connection, proposal_id: str, tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """5a744f81 — raw lineage rows touching ``proposal_id`` in EITHER role
    (as the newer ``from_proposal_id`` or the related ``to_proposal_id``),
    ordered by id ASC (see ``get_proposal_links`` for why ``id`` alone,
    not ``created_at``, is the deterministic sort key across both SQLite
    and Postgres)."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    scope_sql = f" AND {scope}" if scope else ""
    async with db.execute(
        f"SELECT * FROM proposal_lineage WHERE "
        f"(from_proposal_id = ? OR to_proposal_id = ?){scope_sql} "
        f"ORDER BY id ASC",
        [proposal_id, proposal_id, *scope_params],
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def get_proposal_successors(
    db: aiosqlite.Connection, proposal_id: str, tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """5a744f81 — relations where ``proposal_id`` is the ``to_proposal_id``
    (i.e. its known successors — proposals that supersede/refine/fork/
    continue/duplicate/respond to it), in deterministic ``sequence`` order
    (the order those relations were recorded)."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    scope_sql = f" AND {scope}" if scope else ""
    async with db.execute(
        f"SELECT * FROM proposal_lineage WHERE to_proposal_id = ?{scope_sql} "
        f"ORDER BY sequence ASC, id ASC",
        [proposal_id, *scope_params],
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def get_proposal_ancestors(
    db: aiosqlite.Connection, proposal_id: str, tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """5a744f81 — walk backwards (``from_proposal_id -> to_proposal_id``)
    from ``proposal_id`` to reconstruct ONE deterministic ordered parent
    chain, nearest ancestor first.

    A proposal may have MORE than one outgoing relation (e.g. it both
    "forks" one proposal and "responds_to" another) — every such edge is
    still visible via :func:`get_proposal_lineage_links`, but this walk
    follows only the earliest-recorded relation at each step (lowest
    ``sequence``, then lowest ``id``) so the returned chain is always a
    single deterministic path rather than an ambiguous branching set.
    Terminates at a proposal with no further outgoing relation, or —
    defensively — if it revisits a proposal already in the chain (the
    cycle guard in :func:`link_proposal_lineage` should make that
    unreachable via normal writes, but this walk never trusts that as a
    hard guarantee against, e.g., a row written directly by a future
    schema migration)."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    scope_sql = f" AND {scope}" if scope else ""
    chain: list[dict[str, Any]] = []
    visited = {proposal_id}
    current = proposal_id
    hops = 0
    while hops < _MAX_LINEAGE_HOPS:
        hops += 1
        async with db.execute(
            f"SELECT * FROM proposal_lineage WHERE from_proposal_id = ?"
            f"{scope_sql} ORDER BY sequence ASC, id ASC LIMIT 1",
            [current, *scope_params],
        ) as cur:
            row = await cur.fetchone()
        d = _row_to_dict(row)
        if d is None:
            break
        nxt = d.get("to_proposal_id")
        if not nxt or nxt in visited:
            break
        chain.append(d)
        visited.add(nxt)
        current = nxt
    return chain
