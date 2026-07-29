"""eb2e44f8 — immutable wave base manifests for git worktrees.

Persists, per registered worktree (``active_worktrees`` row), an IMMUTABLE
record of the repo identity, base branch, base commit SHA, and owning sprint
item it was created for. This is the ground truth later checked by
``meridian.worktree_merge_guard.validate_worktree_merge`` before a
merge/completion is allowed to proceed (see that module for the actual git
validation logic — HEAD ancestry, dirty-tree, staleness).

Immutability contract: :func:`persist_worktree_manifest` refuses to overwrite
an existing manifest for a ``worktree_id`` — a second call without
``force=True`` raises ``ValueError``. Passing ``force=True`` performs an
explicit, AUDITED replacement: the prior row is marked ``superseded_at`` /
``superseded_reason`` (never deleted) and a fresh row becomes the new active
manifest. This is enforced twice — once in Python (the check-then-act above)
and once at the schema level via a partial unique index
(``idx_wave_base_manifests_active``, ``WHERE superseded_at IS NULL``) so even
a caller that bypasses this module's Python API cannot silently create two
simultaneously-active manifests for the same worktree.
"""
from __future__ import annotations

from typing import Any

import aiosqlite

from meridian.db import _new_id, _row_to_dict  # noqa: PLC0415


async def _migrate_wave_base_manifests(db: aiosqlite.Connection) -> None:
    """eb2e44f8 — create ``wave_base_manifests`` and add ``active_worktrees.pid``.

    Guarded migration (no inline CREATE INDEX in the unguarded base schema
    literals — 2026-07-04 outage rule): the table + its indexes are created
    here so existing DBs pick them up on the first startup after deploy.

    ``active_worktrees.pid`` (the OS PID of the process that created the
    worktree) is bolted on here rather than getting its own migration
    function — it is tightly coupled to this same sprint item (the cleanup
    guard in ``worktree_cleanup.validate_worktree_cleanup_target`` reads it)
    and keeping the two together avoids a second near-duplicate migration.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS wave_base_manifests (
            id TEXT PRIMARY KEY,
            worktree_id TEXT NOT NULL REFERENCES active_worktrees(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            session_id TEXT NOT NULL,
            item_id TEXT,
            repo_identity TEXT NOT NULL,
            base_branch TEXT NOT NULL,
            base_sha TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            superseded_at TEXT,
            superseded_reason TEXT
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_wave_base_manifests_worktree "
        "ON wave_base_manifests(worktree_id)"
    )
    # Partial unique index — the schema-level half of the immutability
    # contract described in the module docstring. Only one row per
    # worktree_id may have superseded_at IS NULL at a time.
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_wave_base_manifests_active "
        "ON wave_base_manifests(worktree_id) WHERE superseded_at IS NULL"
    )
    from .migrations import _migrate_add_column_if_missing  # noqa: PLC0415
    await _migrate_add_column_if_missing(db, "active_worktrees", "pid", "INTEGER")
    await db.commit()


async def persist_worktree_manifest(
    db: aiosqlite.Connection,
    worktree_id: str,
    project_id: str,
    session_id: str,
    item_id: str | None,
    repo_identity: str,
    base_branch: str,
    base_sha: str,
    *,
    force: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    """Persist the immutable base manifest for a worktree.

    Raises ``ValueError`` if an active (non-superseded) manifest already
    exists for ``worktree_id`` and ``force`` is not True — see the module
    docstring for the immutability contract this enforces. When ``force=True``
    is passed deliberately, the existing manifest is marked superseded (with
    ``reason``, defaulting to a generic note) rather than deleted, so the
    replacement is auditable via :func:`get_worktree_manifest_history`.
    """
    existing = await get_worktree_manifest(db, worktree_id)
    if existing is not None:
        if not force:
            raise ValueError(
                f"worktree {worktree_id!r} already has an immutable base "
                f"manifest (id={existing['id']}); pass force=True with a "
                "reason to explicitly supersede it instead of silently "
                "overwriting."
            )
        await db.execute(
            "UPDATE wave_base_manifests SET superseded_at = datetime('now'), "
            "superseded_reason = ? WHERE id = ?",
            (reason or "force-replaced", existing["id"]),
        )
    mid = _new_id()
    await db.execute(
        "INSERT INTO wave_base_manifests "
        "(id, worktree_id, project_id, session_id, item_id, repo_identity, "
        "base_branch, base_sha) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            mid, worktree_id, project_id, session_id, item_id,
            repo_identity, base_branch, base_sha,
        ),
    )
    await db.commit()
    result = await get_worktree_manifest(db, worktree_id)
    assert result is not None  # just inserted; must be findable
    return result


async def get_worktree_manifest(
    db: aiosqlite.Connection,
    worktree_id: str,
) -> dict[str, Any] | None:
    """Return the ACTIVE (non-superseded) manifest for a worktree, or None."""
    async with db.execute(
        "SELECT * FROM wave_base_manifests "
        "WHERE worktree_id = ? AND superseded_at IS NULL",
        (worktree_id,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) if row is not None else None


async def get_worktree_manifest_history(
    db: aiosqlite.Connection,
    worktree_id: str,
) -> list[dict[str, Any]]:
    """Every manifest row (including superseded ones) for a worktree, newest
    first — the audit trail behind an explicit ``force=True`` replacement."""
    async with db.execute(
        "SELECT * FROM wave_base_manifests WHERE worktree_id = ? "
        "ORDER BY created_at DESC",
        (worktree_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]
