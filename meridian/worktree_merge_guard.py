"""Pre-merge/completion validation for git worktrees — meridian/worktree_merge_guard.py

eb2e44f8 — a worktree's immutable base manifest (``db.worktree_manifest``:
repo identity, base branch, base SHA, owning sprint item) is only useful if
something actually checks the worktree's REAL git state against it before a
merge/completion is allowed to proceed. This module is that check.

:func:`validate_worktree_merge` never raises for an expected validation
failure — every rejection reason (missing manifest, dirty tree, diverged
HEAD, stale manifest) comes back as a structured entry in the result's
``errors`` list so a caller can surface a clean, machine-readable rejection
instead of letting an exception turn into a raw 500. Only a genuinely
unexpected error (e.g. a DB failure) propagates.

Design choices, made explicit per this sprint item's spec:

* **Ancestry, not equality.** The gate checks
  ``git merge-base --is-ancestor <base_sha> <HEAD>`` rather than
  ``HEAD == base_sha``. The expected, legitimate case is a worktree with
  NEW commits layered on top of its recorded base — an equality check would
  reject that every time. What must be rejected is a HEAD where base_sha is
  NOT an ancestor at all: a rebase, a hard reset, or a checkout of an
  unrelated branch. "Ancestor-of" is the correct middle ground between a too
  -strict equality check and a too-loose "skip ancestry entirely" check.
* **Staleness is time-based (wall clock since manifest creation), not a
  commit-count diff against origin/dev.** A commit-count comparison
  (``git rev-list --count base_sha..origin/dev``) depends on the local
  ``origin/dev`` ref being freshly fetched, which this module cannot
  guarantee — a stale local ref would silently under-report divergence.
  Wall-clock age of the manifest is always knowable and monotonic. Default
  threshold is 24 hours: long enough to comfortably span one working
  session/day without forcing needless worktree churn, short enough that a
  worktree left open across a multi-day gap (during which origin/dev has
  likely moved a lot in an active megasprint repo) gets flagged for a fresh
  claim instead of an increasingly-blind merge attempt.
* **Self-hosted only for the git-level checks (HEAD/dirty/ancestry).** Per
  the local-fs-access architectural law already established for
  ``worktree_cleanup`` (workspace decision 0dedff91) a hosted multi-tenant
  server has no access to a caller's own checkout. Callers pass
  ``repo_root=None`` in that case; this module then validates only what is
  knowable without filesystem access — manifest presence and staleness —
  and reports the git-level checks as skipped rather than pretending to
  have verified them.
"""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import worktree_cleanup

logger = logging.getLogger(__name__)

DEFAULT_STALE_AFTER_HOURS = 24.0


def _git(cwd: Path, args: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def get_worktree_head(repo_root: Path, wt_path: str) -> str | None:
    """Current HEAD commit SHA of the worktree, or None if it can't be
    determined (missing directory, not a git checkout, git failure). Never
    raises."""
    try:
        abs_path = worktree_cleanup.resolve_worktree_disk_path(repo_root, wt_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("worktree_merge_guard: path resolution failed for %s: %s", wt_path, exc)
        return None
    if not abs_path.exists():
        return None
    try:
        result = _git(abs_path, ["rev-parse", "HEAD"])
    except Exception as exc:  # noqa: BLE001 — subprocess failures must not propagate
        logger.warning("worktree_merge_guard: rev-parse HEAD failed for %s: %s", abs_path, exc)
        return None
    if result.returncode != 0:
        return None
    sha = (result.stdout or "").strip()
    return sha or None


def is_worktree_dirty(repo_root: Path, wt_path: str) -> bool | None:
    """True if the worktree has uncommitted changes, False if clean, None if
    it can't be determined. Callers must treat None as "fail closed" — see
    validate_worktree_merge."""
    try:
        abs_path = worktree_cleanup.resolve_worktree_disk_path(repo_root, wt_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("worktree_merge_guard: path resolution failed for %s: %s", wt_path, exc)
        return None
    if not abs_path.exists():
        return None
    try:
        result = _git(abs_path, ["status", "--porcelain"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("worktree_merge_guard: status --porcelain failed for %s: %s", abs_path, exc)
        return None
    if result.returncode != 0:
        return None
    return bool((result.stdout or "").strip())


def is_ancestor(repo_root: Path, ancestor_sha: str, descendant_sha: str) -> bool | None:
    """True if ancestor_sha is an ancestor of (or equal to) descendant_sha,
    False if it definitively is not (rebase/reset/divergence), None if it
    can't be determined (e.g. one of the SHAs isn't reachable in this
    checkout)."""
    try:
        result = _git(repo_root, ["merge-base", "--is-ancestor", ancestor_sha, descendant_sha])
    except Exception as exc:  # noqa: BLE001
        logger.warning("worktree_merge_guard: merge-base --is-ancestor failed: %s", exc)
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None  # git exits >1 (often 128) when a SHA is unknown to this repo


def manifest_is_stale(
    manifest: dict[str, Any], *, stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS
) -> bool:
    """True when the manifest's ``created_at`` is older than the threshold.

    Never raises: an unparseable/missing timestamp is treated as "not stale"
    (the safer default — we don't manufacture a rejection out of a formatting
    quirk; the ancestry/dirty checks are the real safety net).
    """
    created_raw = manifest.get("created_at")
    if not created_raw:
        return False
    try:
        created = datetime.fromisoformat(str(created_raw).replace(" ", "T"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600.0
    return age_hours > stale_after_hours


async def validate_worktree_merge(
    db: Any,
    repo_root: Path | None,
    worktree_id: str,
    *,
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS,
) -> dict[str, Any]:
    """The pre-merge/completion gate.

    Returns ``{"ok": bool, "worktree_id": ..., "manifest": dict|None,
    "head_sha": str|None, "errors": [{"code": ..., "message": ...}, ...]}``.

    ``ok`` is True only when there are zero entries in ``errors``. Checked,
    in order: the worktree row exists; it has a persisted base manifest
    (skips the rest if not — nothing to validate against); the manifest
    isn't stale; and (self-hosted / repo_root given only) the worktree isn't
    dirty and its recorded base_sha is an ancestor of its current HEAD.
    """
    from . import db as db_module  # noqa: PLC0415 — avoid import cycle at module load

    wt = await db_module.get_worktree(db, worktree_id)
    if wt is None:
        return {
            "ok": False,
            "worktree_id": worktree_id,
            "manifest": None,
            "head_sha": None,
            "errors": [
                {"code": "WORKTREE_NOT_FOUND", "message": f"no active_worktrees row for {worktree_id}"}
            ],
        }

    manifest = await db_module.get_worktree_manifest(db, worktree_id)
    if manifest is None:
        return {
            "ok": False,
            "worktree_id": worktree_id,
            "manifest": None,
            "head_sha": None,
            "errors": [
                {
                    "code": "NO_MANIFEST",
                    "message": "worktree has no persisted base manifest — cannot validate",
                }
            ],
        }

    errors: list[dict[str, str]] = []
    if manifest_is_stale(manifest, stale_after_hours=stale_after_hours):
        errors.append(
            {
                "code": "STALE_MANIFEST",
                "message": (
                    f"base manifest is older than {stale_after_hours}h "
                    f"(created_at={manifest.get('created_at')}); origin/dev may "
                    "have diverged too far for a straightforward merge — reclaim "
                    "a fresh worktree instead of merging this one"
                ),
            }
        )

    head_sha: str | None = None
    if repo_root is not None:
        head_sha = get_worktree_head(repo_root, wt["path"])
        if head_sha is None:
            errors.append(
                {
                    "code": "HEAD_UNRESOLVABLE",
                    "message": f"could not determine current HEAD for worktree at {wt['path']}",
                }
            )
        else:
            dirty = is_worktree_dirty(repo_root, wt["path"])
            if dirty is None:
                errors.append(
                    {
                        "code": "DIRTY_CHECK_FAILED",
                        "message": "could not determine whether the worktree has uncommitted changes",
                    }
                )
            elif dirty:
                errors.append(
                    {
                        "code": "DIRTY_WORKTREE",
                        "message": "worktree has uncommitted changes — commit or stash before merging",
                    }
                )

            ancestor_ok = is_ancestor(repo_root, manifest["base_sha"], head_sha)
            if ancestor_ok is None:
                errors.append(
                    {
                        "code": "ANCESTRY_UNRESOLVABLE",
                        "message": (
                            f"could not verify ancestry of base_sha={manifest['base_sha']} "
                            f"in HEAD={head_sha}"
                        ),
                    }
                )
            elif not ancestor_ok:
                errors.append(
                    {
                        "code": "HEAD_MISMATCH",
                        "message": (
                            f"recorded base_sha={manifest['base_sha']} is not an ancestor "
                            f"of current HEAD={head_sha} — worktree was rebased, reset, or "
                            "otherwise diverged from its recorded base"
                        ),
                    }
                )
    # else: repo_root is None (hosted / no local FS access) — git-level
    # checks are skipped; only manifest presence + staleness were checkable.
    # This is a documented limitation, not a silent pass — the caller
    # receives head_sha=None and no HEAD_MISMATCH/DIRTY_WORKTREE entries so
    # it can tell the difference between "verified clean" and "unverifiable".

    return {
        "ok": not errors,
        "worktree_id": worktree_id,
        "manifest": manifest,
        "head_sha": head_sha,
        "errors": errors,
    }
