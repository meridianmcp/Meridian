"""15610335 — external Pixi detached-environment registry, per worktree.

Companion to ``meridian.pixi_env_retention`` (the actual filesystem-facing
logic: dry-run cache-cleanup planning, guarded/idempotent removal, and
stale-external-environment discovery) and to ``meridian.orphan_reaper``
(wires the two together for the Stop-hook cleanup pass). This module is the
durable, DB-backed half: it records WHERE each worktree's detached Pixi
environment actually landed on disk once resolved (via
``pixi_env_retention.resolve_pixi_env_prefix``, itself a thin wrapper over
``pixi info --json``), so a later sweep can find and reclaim it without
having to reverse-engineer Pixi's internal per-project hashing scheme.

Why this table needs to exist at all: Pixi's ``detached-environments``
config (set at the *global*, machine-level scope — see
``pixi_env_retention.ensure_detached_environments_configured`` and the
comment block in ``pixi.toml`` — NOT a ``pixi.toml`` manifest key; confirmed
live that ``detached-environments`` under ``[workspace]`` silently
no-ops with ``project_info`` coming back ``null``) moves each worktree's
~1GB ``.pixi`` environment OUTSIDE the git-tracked worktree tree, into a
directory keyed by a hash of the resolved project (manifest) path — e.g.
``<root>/meridian-9641266968162128105/envs/default`` (confirmed live via
``pixi info --json``'s ``environments_info[].prefix``). ``git worktree
remove`` (and this repo's own ``worktree_cleanup.remove_worktree_on_disk``)
only ever touches the git-tracked worktree directory itself — it has no
knowledge of, and never touches, this external keyed directory. Without a
registry mapping worktree_id -> external root, a deleted worktree's
detached environment would simply never be reclaimed, defeating the whole
point of moving it out of the tree in the first place (the environments
just pile up externally instead of internally).
"""
from __future__ import annotations

from typing import Any

import aiosqlite

from meridian.db import _new_id, _row_to_dict  # noqa: PLC0415


async def _migrate_pixi_env_roots(db: aiosqlite.Connection) -> None:
    """15610335 — create ``pixi_env_roots``.

    Guarded migration (no inline CREATE INDEX in the unguarded base schema
    literals — 2026-07-04 outage rule): the table + its indexes are created
    here so existing DBs pick it up on the first startup after deploy.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS pixi_env_roots (
            id TEXT PRIMARY KEY,
            worktree_id TEXT NOT NULL REFERENCES active_worktrees(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            root_path TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            reclaimed_at TEXT
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_pixi_env_roots_worktree "
        "ON pixi_env_roots(worktree_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_pixi_env_roots_project "
        "ON pixi_env_roots(project_id, reclaimed_at)"
    )
    await db.commit()


async def register_pixi_env_root(
    db: aiosqlite.Connection,
    worktree_id: str,
    project_id: str,
    root_path: str,
) -> dict[str, Any]:
    """Record where worktree *worktree_id*'s detached Pixi environment
    resolved to on disk. Returns the inserted row.

    Not uniqueness-enforced at the schema level (mirrors ``register_worktree``
    itself, which has no such gate either): re-registering the same
    ``worktree_id`` after a later ``pixi install`` re-resolves its prefix is
    a legitimate refresh, not an error. Callers that want the single current
    root should use :func:`get_pixi_env_root_for_worktree`, which already
    orders by recency.
    """
    rid = _new_id()
    await db.execute(
        "INSERT INTO pixi_env_roots (id, worktree_id, project_id, root_path) "
        "VALUES (?, ?, ?, ?)",
        (rid, worktree_id, project_id, root_path),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM pixi_env_roots WHERE id = ?", (rid,)
    ) as cur:
        row = await cur.fetchone()
    result = _row_to_dict(row)
    assert result is not None  # just inserted; must be findable
    return result


async def get_pixi_env_root_for_worktree(
    db: aiosqlite.Connection,
    worktree_id: str,
) -> dict[str, Any] | None:
    """Return the most recent unreclaimed env-root row for a worktree, or
    ``None`` if it never had one registered (or all were already reclaimed)."""
    async with db.execute(
        "SELECT * FROM pixi_env_roots WHERE worktree_id = ? "
        "AND reclaimed_at IS NULL ORDER BY created_at DESC LIMIT 1",
        (worktree_id,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) if row is not None else None


async def list_unreclaimed_pixi_env_roots(
    db: aiosqlite.Connection,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Reclaim candidates: env roots whose owning worktree has been removed
    (``active_worktrees.removed_at IS NOT NULL``) but the external directory
    itself hasn't been marked reclaimed yet.

    Pass ``project_id`` to scope to one project; omit to sweep every project
    (mirrors ``db.list_worktrees_pending_cleanup``'s a03c0eeb precedent).
    """
    where = ["per.reclaimed_at IS NULL", "aw.removed_at IS NOT NULL"]
    params: list[Any] = []
    if project_id is not None:
        where.append("per.project_id = ?")
        params.append(project_id)
    query = (
        "SELECT per.* FROM pixi_env_roots per "
        "JOIN active_worktrees aw ON aw.id = per.worktree_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY per.created_at ASC"
    )
    async with db.execute(query, params or None) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def mark_pixi_env_root_reclaimed(
    db: aiosqlite.Connection,
    root_id: str,
) -> bool:
    """Mark one env-root row reclaimed. Returns True when a row was updated
    (idempotent: reclaiming an already-reclaimed row is a no-op, not an
    error — mirrors ``remove_worktree``'s ``removed_at IS NULL`` guard)."""
    cursor = await db.execute(
        "UPDATE pixi_env_roots SET reclaimed_at = datetime('now') "
        "WHERE id = ? AND reclaimed_at IS NULL",
        (root_id,),
    )
    await db.commit()
    return (cursor.rowcount or 0) > 0
