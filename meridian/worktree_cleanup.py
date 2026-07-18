"""Real on-disk git worktree cleanup — meridian/worktree_cleanup.py

a03c0eeb — ``DELETE /projects/{id}/worktrees/{worktree_id}`` (``delete_worktree``)
previously only flipped ``active_worktrees.removed_at`` in the DB; nothing
confirmed the worktree directory (and its ``git worktree`` registration) was
actually removed from disk. Tonight's megasprint alone spawned 13+3+5
worktrees across batches — if DB bookkeeping and disk state silently
diverge because an executor skipped (or crashed before) the real
``git worktree remove``, orphaned worktree dirs accumulate across every
sprint. This module adds the real removal, called from two places:

  1. ``delete_worktree`` — best-effort real removal at the moment the caller
     reports a worktree done, instead of only trusting the caller already
     ran the command itself.
  2. ``sweep_stale_worktrees`` — a periodic catch-all pass (wired into the
     server's existing auto-summary loop) that reclaims worktrees whose
     owning sprint item/session reached a terminal state but whose
     directory never got cleaned up at all (``delete_worktree`` never
     called — dead session, crashed executor, etc).

Scope, deliberately: self-hosted single-tenant mode ONLY. Per the
local-fs-access architectural law in ``meridian/_deps.py``
(``require_local_fs_access``, workspace decision 0dedff91), a hosted
multi-tenant server has no access to a caller's machine — silently
attempting ``git worktree remove`` there would run against the *server's*
filesystem, not the caller's, either no-op'ing uselessly or deleting the
wrong thing. Callers of this module MUST gate on ``not _hosted_mode()``
before invoking it (kept out of these functions so they stay trivially
unit-testable without patching env vars). Self-hosted Meridian runs the
server process from inside the very repo it coordinates — the same
precedent ``_refresh_claude_md_current_state`` relies on for its
``_REPO_ROOT``-relative writes — and every worktree path registered by
``claim_sprint_item`` (``.claude/worktrees/{session}`` or
``../{repo}-worktree-{item}``) is created relative to that same repo.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def resolve_worktree_disk_path(repo_root: Path, wt_path: str) -> Path:
    """Resolve a registered worktree ``path`` (often relative, e.g.
    ``.claude/worktrees/abcd1234`` or ``../repo-worktree-abcd1234``) against
    *repo_root* — the repo the Meridian server process itself is running
    from. Absolute paths pass through unchanged."""
    p = Path(wt_path)
    if p.is_absolute():
        return p
    return (repo_root / p).resolve()


def remove_worktree_on_disk(repo_root: Path, wt_path: str) -> dict[str, Any]:
    """Best-effort REAL disk removal of a git worktree. Never raises.

    Returns ``{"attempted": bool, "removed": bool, "detail": str}``.
    ``removed=True`` also covers the case where the directory was already
    gone (nothing left to clean up on disk, regardless of who removed it —
    the executor may genuinely have run ``git worktree remove`` itself
    already, which is fine).
    """
    try:
        abs_path = resolve_worktree_disk_path(repo_root, wt_path)
    except Exception as exc:  # noqa: BLE001 — never let path resolution crash a caller
        return {
            "attempted": False,
            "removed": False,
            "detail": f"path resolution failed: {exc}",
        }

    if not abs_path.exists():
        return {"attempted": False, "removed": True, "detail": "already absent on disk"}

    git_error = ""
    removed_by_git = False
    try:
        result = subprocess.run(
            ["git", "worktree", "remove", str(abs_path), "--force"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        git_error = (result.stderr or "").strip()
        removed_by_git = result.returncode == 0 and not abs_path.exists()
    except Exception as exc:  # noqa: BLE001 — subprocess failures must not propagate
        git_error = str(exc)

    if removed_by_git:
        return {"attempted": True, "removed": True, "detail": "git worktree remove"}

    # Fallback: manual rmtree + prune the now-stale registration. Covers a
    # dirty/locked worktree or an admin dir `git worktree remove` refuses to
    # touch even with --force.
    try:
        if abs_path.exists():
            shutil.rmtree(abs_path, ignore_errors=True)
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "worktree_cleanup: fallback removal failed for %s: %s", abs_path, exc
        )
        return {
            "attempted": True,
            "removed": not abs_path.exists(),
            "detail": f"fallback failed: {exc}",
        }

    removed = not abs_path.exists()
    if not removed:
        logger.warning(
            "worktree_cleanup: could not remove %s from disk (git: %s)",
            abs_path, git_error,
        )
    return {
        "attempted": True,
        "removed": removed,
        "detail": git_error or "fallback rmtree+prune",
    }


async def sweep_stale_worktrees(
    db: Any,
    repo_root: Path,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Periodic pass: real disk cleanup for worktrees whose sprint item (or
    session) has reached a terminal state but whose directory is still
    marked active in the DB — catches everything the on-delete path in
    ``delete_worktree`` missed (executor never called the endpoint, session
    crashed mid-cleanup, etc).

    Callers must gate on ``not _hosted_mode()`` before calling this — see
    the module docstring.
    """
    from . import db as db_module  # noqa: PLC0415 — avoid import cycle at module load

    candidates = await db_module.list_worktrees_pending_cleanup(db, project_id)
    swept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for wt in candidates:
        outcome = remove_worktree_on_disk(repo_root, wt["path"])
        if outcome["removed"]:
            await db_module.remove_worktree(db, wt["id"])
            swept.append({"id": wt["id"], "path": wt["path"]})
        else:
            skipped.append(
                {"id": wt["id"], "path": wt["path"], "detail": outcome["detail"]}
            )
    return {
        "swept": swept,
        "skipped": skipped,
        "swept_count": len(swept),
        "skipped_count": len(skipped),
    }
