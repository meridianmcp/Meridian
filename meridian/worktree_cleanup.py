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

import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable

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


def looks_like_worktree_path(repo_root: Path, abs_path: Path) -> bool:
    """True iff *abs_path* matches this codebase's OWN documented worktree
    placement convention — the one ``claim_sprint_item`` actually uses (see
    this module's docstring): either nested under ``repo_root/.claude/
    worktrees/`` or a sibling of *repo_root* named ``{repo_root.name}-
    worktree-...``. Never true for *repo_root* itself, and never true for an
    arbitrary path elsewhere in (or outside) the repo — in particular never
    true for a real source subdirectory like ``repo_root/meridian``, which
    matches neither shape.

    Deliberately NOT a ``git worktree list`` membership check: a worktree
    directory that exists on disk but was never fully registered with git
    (interrupted `git worktree add`, or hand-created scaffolding) still
    needs to be reclaimable via the rmtree fallback below — see
    ``test_remove_worktree_on_disk_falls_back_to_rmtree``. Path SHAPE, not
    git's own bookkeeping, is what actually distinguishes "somewhere a
    worktree is allowed to live" from "the repo's own source tree" — the
    gap that let a test-registered ``path: "meridian"`` row delete this
    repo's own source package (2026-08-04 incident; see
    tests/test_a03c0eeb_worktree_disk_cleanup.py's regression coverage for
    this function).
    """
    try:
        repo_root_resolved = repo_root.resolve()
    except OSError:
        return False
    if abs_path == repo_root_resolved:
        return False
    claude_worktrees = repo_root_resolved / ".claude" / "worktrees"
    try:
        abs_path.relative_to(claude_worktrees)
        return True
    except ValueError:
        pass
    if abs_path.parent == repo_root_resolved.parent:
        return abs_path.name.startswith(f"{repo_root_resolved.name}-worktree-")
    return False


def remove_worktree_on_disk(repo_root: Path, wt_path: str) -> dict[str, Any]:
    """Best-effort REAL disk removal of a git worktree. Never raises.

    Returns ``{"attempted": bool, "removed": bool, "detail": str}``.
    ``removed=True`` also covers the case where the directory was already
    gone (nothing left to clean up on disk, regardless of who removed it —
    the executor may genuinely have run ``git worktree remove`` itself
    already, which is fine).

    Refuses (``attempted=False``) before touching disk at all when
    *abs_path* does not match this codebase's own worktree placement
    convention (see :func:`looks_like_worktree_path`) — checked BEFORE the
    ``git worktree remove`` attempt below, not just as a fallback gate, so
    a path outside that convention can never reach either removal strategy.
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

    if not looks_like_worktree_path(repo_root, abs_path):
        logger.warning(
            "worktree_cleanup: refusing disk removal for %s — does not match "
            "the worktree placement convention under %s", abs_path, repo_root,
        )
        return {
            "attempted": False,
            "removed": False,
            "detail": "NOT_A_WORKTREE_PATH",
        }

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


def _pid_is_alive(pid: int) -> bool:
    """Liveness check for a recorded worktree-owner PID.

    Mirrors the exact catch tuple the existing task_log PID watchdog uses
    (meridian/server.py's ``_auto_summary_loop``) so this repo has ONE
    liveness-check convention, not two subtly different ones:
    ``os.kill(pid, 0)`` signals nothing (signal 0), it only probes whether
    the OS will let us address the PID at all. ``ProcessLookupError`` means
    the PID is genuinely gone; ``PermissionError``/other ``OSError`` (incl.
    Windows' WinError 87 for a non-existent PID) are both treated the same
    as "not alive" here, matching the established repo-wide precedent rather
    than inventing a new interpretation for this one call site.
    """
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def validate_worktree_cleanup_target(
    wt_row: dict[str, Any] | None,
    *,
    expected_worktree_id: str,
    expected_path: str | None = None,
) -> dict[str, Any]:
    """Pre-disk-mutation sanity + liveness gate (eb2e44f8, acceptance point 4).

    Re-validates a worktree row immediately before its directory is removed
    from disk, closing two TOCTOU-shaped risk windows this module's history
    already flagged: 13+ worktrees spawned in a single megasprint night, plus
    the 2026-07-14 stray-process incident where a session's owning process
    outlived its Meridian session record entirely.

    1. **Identity** — the row actually being acted on still matches the id
       (and, when supplied, the path) the caller resolved moments earlier.
       Catches a race where the underlying row was reused/rewritten between
       listing candidates and acting on them.
    2. **Liveness** — if the row recorded an owning PID at registration time
       (``active_worktrees.pid``), that process must no longer be alive. A
       live PID means SOME process may still be reading/writing inside that
       directory even though Meridian's own bookkeeping considers the
       worktree terminal — deleting out from under it risks corrupting an
       in-flight git operation.

    Returns ``{"ok": bool, "reason": str | None, "detail": str | None}``.
    A worktree with no recorded PID (``pid`` is ``None``) always passes the
    liveness check — this mirrors the fail-open-on-absent-data posture used
    everywhere else in this module (e.g. a missing directory counts as
    "already removed", not an error); only a KNOWN-live PID blocks cleanup.
    """
    if wt_row is None:
        return {
            "ok": False,
            "reason": "NOT_FOUND",
            "detail": f"no active_worktrees row for id {expected_worktree_id}",
        }
    if wt_row.get("id") != expected_worktree_id:
        return {
            "ok": False,
            "reason": "ID_MISMATCH",
            "detail": f"expected worktree id {expected_worktree_id}, row has {wt_row.get('id')}",
        }
    if expected_path is not None and wt_row.get("path") != expected_path:
        return {
            "ok": False,
            "reason": "PATH_MISMATCH",
            "detail": f"expected path {expected_path!r}, row has {wt_row.get('path')!r}",
        }
    pid = wt_row.get("pid")
    if pid is not None:
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            pid_int = None
        if pid_int is not None and _pid_is_alive(pid_int):
            return {
                "ok": False,
                "reason": "PROCESS_STILL_LIVE",
                "detail": f"pid {pid_int} is still running — refusing to remove its worktree from disk",
            }
    return {"ok": True, "reason": None, "detail": None}


def remove_worktree_on_disk_guarded(
    repo_root: Path,
    wt_row: dict[str, Any],
    *,
    expected_worktree_id: str,
) -> dict[str, Any]:
    """Guarded wrapper around :func:`remove_worktree_on_disk`.

    Runs :func:`validate_worktree_cleanup_target` first; only calls the real
    disk-mutating removal when the guard passes. On guard failure, returns
    ``{"attempted": False, "removed": False, "guard_ok": False, "reason":
    ..., "detail": ...}`` instead of touching the filesystem at all — the
    caller decides how to surface that (skip-and-retry-later for the
    periodic sweep, best-effort-log for the on-demand DELETE route).
    """
    guard = validate_worktree_cleanup_target(
        wt_row, expected_worktree_id=expected_worktree_id
    )
    if not guard["ok"]:
        logger.warning(
            "worktree_cleanup: refusing disk removal for %s (%s): %s",
            expected_worktree_id, guard["reason"], guard["detail"],
        )
        return {
            "attempted": False,
            "removed": False,
            "guard_ok": False,
            "reason": guard["reason"],
            "detail": guard["detail"],
        }
    outcome = remove_worktree_on_disk(repo_root, wt_row["path"])
    outcome = dict(outcome)
    outcome["guard_ok"] = True
    return outcome


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
        # eb2e44f8 — guarded: re-validates identity + PID liveness immediately
        # before the real disk mutation, so a worktree whose owning process is
        # somehow still alive (the exact 2026-07-14 stray-process scenario)
        # never gets nuked out from under it just because its DB row looks
        # terminal.
        outcome = remove_worktree_on_disk_guarded(
            repo_root, wt, expected_worktree_id=wt["id"]
        )
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


# ---------------------------------------------------------------------------
# 2ae3f011 -- reversible temp-output quarantine (dry-run manifest, archive
# move, restore, provenance-checked purge).
#
# Cleanup so far (this module's ``sweep_stale_worktrees`` /
# ``remove_worktree_on_disk_guarded`` above) has only ever known how to do
# ONE thing to a dead worktree directory: remove it wholesale. That is fine
# for the worktree scaffolding itself, but a dead worktree can also contain
# genuinely valuable TEMPORARY OUTPUT files (data/figures a script dropped
# during the run) that ``meridian_outputs``' classification/provenance
# systems (``extensions/meridian-outputs/meridian_outputs/classify.py``,
# ``provenance.py``, ``provenance_status.py``) already know how to tell apart
# from canonical/user files -- but nothing acted on that signal safely.
# Straight deletion is a one-way door; this adds a REVERSIBLE middle step.
#
# Deliberately has NO hard import of ``extensions.meridian_outputs`` --  that
# package is not on this pixi env's dependency graph (see pixi.toml's
# 52cbe5d8 comment: its own tests import it straight off sys.path, nothing
# here pulls it in for the main ``pixi run test`` env). Ownership/provenance
# confirmation is always an INJECTED callable (``ownership_check`` /
# ``verify_provenance``), same pattern ``orphan_reaper.py`` already uses for
# ``process_iter``/``kill_fn`` -- keeps this module importable and testable
# with zero optional dependencies, while a real caller (the meridian-outputs
# MCP tool surface, via ``provenance.classify_temp_output_ownership``) can
# supply the real check. Omitting the callable is NOT "skip the check" --
# every function below fails closed (nothing is ever quarantined/deleted)
# when no classifier is supplied, matching this module's existing
# fail-closed posture (``validate_worktree_cleanup_target`` above).
#
# Three operations, each independently safe:
#   1. build_quarantine_manifest -- DRY-RUN ONLY. Reads file metadata/hashes
#      it never mutates disk beyond that.
#   2. quarantine_temp_outputs   -- ARCHIVE MOVE (shutil.move, never
#      os.remove/unlink) of eligible entries from a manifest into
#      ``archive_root``. Reversible by construction.
#   3. restore_quarantined_output / purge_quarantined_output -- the two ways
#      a quarantined file's story ends: moved back (restore, always
#      integrity-checked) or actually deleted (purge, the ONLY real delete
#      in this module, only ever applied to a file already sitting in the
#      archive, and only after re-verifying BOTH restore-integrity (hash)
#      AND provenance/ownership immediately before deleting).
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str | None:
    """Best-effort SHA-256 content hash. Returns ``None`` (never raises) on
    any read failure -- a hash failure downgrades one manifest row to
    un-hashed rather than aborting the whole scan/operation."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _default_ownership_check(path: str) -> dict[str, Any]:
    """Fail-closed default used whenever no real ``ownership_check`` is
    injected: every path is ineligible. Quarantine must never start moving
    files just because a caller forgot to wire a real classifier."""
    return {
        "eligible": False,
        "reason": "no ownership_check supplied -- refusing to guess ownership",
    }


def build_quarantine_manifest(
    paths: Iterable[str],
    *,
    archive_root: str | os.PathLike[str],
    ownership_check: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """DRY-RUN: build one manifest entry per path in *paths* -- never moves,
    modifies, or deletes anything on disk beyond reading file metadata/bytes
    to compute a hash.

    Each entry carries: ``path``, ``exists``, ``eligible`` (per
    *ownership_check*), ``reason``, ``size``, ``content_hash``,
    ``archive_path`` (where a real ``quarantine_temp_outputs`` call would
    move it -- hash-prefixed so two same-named files from different
    directories never collide in the archive), and ``restore_destination``
    (always the original ``path`` -- what a later ``restore_quarantined_output``
    would move it back to).

    *ownership_check* is the confirmation gate: given a path, returns
    ``{"eligible": bool, "reason": str, ...}``. Only an explicit
    ``eligible=True`` makes an entry a real quarantine candidate --
    omitting the callable (or it raising) fails an entry closed, never open.
    Never raises: one bad path or a raising classifier degrades that single
    entry, the rest of the batch still completes.
    """
    archive_root_path = Path(archive_root)
    check = ownership_check or _default_ownership_check
    entries: list[dict[str, Any]] = []
    for raw in paths:
        p = Path(raw)
        try:
            ownership = dict(check(str(p)))
        except Exception as exc:  # noqa: BLE001 -- one bad classifier call must not sink the scan
            ownership = {"eligible": False, "reason": f"ownership_check raised: {exc}"}
        eligible = bool(ownership.get("eligible"))
        exists = p.is_file()
        size = p.stat().st_size if exists else None
        content_hash = _sha256_file(p) if exists else None
        archive_name = f"{content_hash or 'unhashed'}__{p.name}" if exists else None
        archive_path = str(archive_root_path / archive_name) if archive_name else None
        entries.append(
            {
                "path": str(p),
                "exists": exists,
                "eligible": eligible,
                "reason": ownership.get("reason", ""),
                "size": size,
                "content_hash": content_hash,
                "archive_path": archive_path,
                "restore_destination": str(p),
            }
        )
    return {
        "archive_root": str(archive_root_path),
        "total": len(entries),
        "eligible_count": sum(1 for e in entries if e["eligible"]),
        "entries": entries,
    }


def quarantine_temp_outputs(manifest: dict[str, Any]) -> dict[str, Any]:
    """ARCHIVE MOVE (``shutil.move`` -- never ``os.remove``/``unlink``): for
    every ``eligible`` entry in *manifest* (as produced by
    :func:`build_quarantine_manifest`) whose source file still exists, moves
    it from its original path into its manifest-computed ``archive_path``,
    creating the archive directory tree as needed. Reversible by
    construction -- see :func:`restore_quarantined_output`.

    Re-checks existence/eligibility at call time rather than blindly
    trusting the (possibly stale) manifest snapshot. Never raises: a single
    file's move failure is recorded per-entry (``skipped``) and does not
    abort the batch, same "one bad record doesn't sink the scan" posture as
    ``orphan_reaper.list_orphan_candidates``.
    """
    moved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for entry in manifest.get("entries", []):
        if not entry.get("eligible"):
            skipped.append({**entry, "quarantine_skip_reason": "not eligible"})
            continue
        if not entry.get("archive_path"):
            skipped.append({**entry, "quarantine_skip_reason": "no archive_path computed"})
            continue
        src = Path(entry["path"])
        if not src.is_file():
            skipped.append({**entry, "quarantine_skip_reason": "source no longer exists"})
            continue
        dst = Path(entry["archive_path"])
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        except OSError as exc:  # noqa: BLE001 -- a single move failure must not abort the batch
            skipped.append({**entry, "quarantine_skip_reason": f"move failed: {exc}"})
            continue
        moved.append({**entry, "archived": dst.is_file()})
    return {
        "archive_root": manifest.get("archive_root"),
        "moved_count": len(moved),
        "skipped_count": len(skipped),
        "moved": moved,
        "skipped": skipped,
    }


def restore_quarantined_output(entry: dict[str, Any]) -> dict[str, Any]:
    """Reversal of :func:`quarantine_temp_outputs`: moves an archived file
    (``entry["archive_path"]``) back to ``entry["restore_destination"]``.

    Two safety checks before ever moving anything:
      1. **Integrity** -- the archived file's current content hash must
         still match ``entry["content_hash"]`` recorded at quarantine time
         (catches corruption, or an unrelated file having ended up at that
         archive path since).
      2. **No silent overwrite** -- if the restore destination already
         exists and holds DIFFERENT content, this refuses rather than
         clobbering whatever now lives there (it may be a new file created
         since quarantine). A destination that already holds
         byte-identical content is treated as "already restored", not an
         error (idempotent re-run after a partial failure).

    Never raises. Returns ``{"restored": bool, "reason": str}``.
    """
    archive_path = Path(entry["archive_path"]) if entry.get("archive_path") else None
    dest = Path(entry["restore_destination"]) if entry.get("restore_destination") else None
    if archive_path is None or dest is None:
        return {"restored": False, "reason": "entry missing archive_path/restore_destination"}
    if not archive_path.is_file():
        return {"restored": False, "reason": "archived file not found"}

    current_hash = _sha256_file(archive_path)
    recorded_hash = entry.get("content_hash")
    if recorded_hash is not None and current_hash != recorded_hash:
        return {
            "restored": False,
            "reason": "content hash mismatch -- archived file may have been altered since quarantine",
        }

    if dest.exists():
        dest_hash = _sha256_file(dest) if dest.is_file() else None
        if dest_hash != current_hash:
            return {
                "restored": False,
                "reason": "restore destination already occupied by a different file -- refusing to overwrite",
            }
        return {"restored": True, "reason": "destination already holds identical content"}

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(archive_path), str(dest))
    except OSError as exc:  # noqa: BLE001
        return {"restored": False, "reason": f"move failed: {exc}"}
    return {"restored": True, "reason": "restored from archive"}


def purge_quarantined_output(
    entry: dict[str, Any],
    *,
    verify_provenance: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The ONLY real delete in this module -- and only ever applied to a
    file already sitting in the archive (``entry["archive_path"]``), never
    a live/original path.

    Two re-checks immediately before deletion, BOTH must pass:
      1. **Integrity** -- same hash check as :func:`restore_quarantined_output`:
         the archived content must still match what was recorded at
         quarantine time.
      2. **Provenance/ownership, re-verified now** -- *verify_provenance* is
         called with the entry's ORIGINAL ``path`` and must return
         ``eligible=True`` again. This is deliberately a fresh check, not a
         re-read of the manifest's stored verdict: a file correctly
         quarantined earlier may since have been reclassified (e.g. it
         turned out to be the canonical copy after all, or a human restored
         and started using it). Omitting *verify_provenance* is NOT "skip
         the check" -- it fails closed, same as
         :func:`_default_ownership_check` above.

    Never raises. Returns ``{"purged": bool, "reason": str}``.
    """
    archive_path = Path(entry["archive_path"]) if entry.get("archive_path") else None
    if archive_path is None or not archive_path.is_file():
        return {"purged": False, "reason": "archived file not found"}

    current_hash = _sha256_file(archive_path)
    recorded_hash = entry.get("content_hash")
    if recorded_hash is not None and current_hash != recorded_hash:
        return {"purged": False, "reason": "content hash mismatch -- refusing to delete"}

    if verify_provenance is None:
        return {
            "purged": False,
            "reason": "no verify_provenance supplied -- refusing to guess ownership before deletion",
        }
    try:
        check = dict(verify_provenance(entry["path"]))
    except Exception as exc:  # noqa: BLE001 -- a raising classifier must not crash the caller
        return {"purged": False, "reason": f"verify_provenance raised: {exc}"}
    if not check.get("eligible"):
        return {
            "purged": False,
            "reason": f"no longer eligible for deletion: {check.get('reason', '')}",
        }

    try:
        archive_path.unlink()
    except OSError as exc:  # noqa: BLE001
        return {"purged": False, "reason": f"delete failed: {exc}"}
    return {"purged": True, "reason": "verified restore-integrity and provenance before deletion"}
