"""15610335 — keep Pixi environments and generated caches outside git
worktrees; retention/cleanup for what's left behind.

Confirmed live on this machine (2026-08-04, pixi 0.67.0): ``detached-
environments`` is NOT a ``pixi.toml`` manifest key — adding it under
``[workspace]`` silently no-ops (``pixi info --json`` comes back with
``project_info: null``, no error, exit 0). It is a genuine Pixi CONFIG key
(``pixi config set --global detached-environments <value>``), written to
Pixi's own config file hierarchy (system/global/local — global is
``~/.pixi/config.toml`` on this machine, OUTSIDE any git repo). ``PIXI_HOME``
only controls where the ``pixi`` *binary* installs (``~/.pixi/bin``); it has
no effect on where per-project workspace environments materialize.

Once configured, every project's ``default`` environment resolves under
``<detached-environments-root>/<project-name>-<hash>/envs/<env-name>``,
where ``<hash>`` is Pixi's own internal hash of the resolved project
(manifest) path — confirmed via ``environments_info[].prefix`` in
``pixi info --json``. Two worktrees of the SAME repo (same project ``name``,
different ``pixi.toml`` path) resolve to two DIFFERENT keyed directories —
i.e. detached environments are still effectively keyed by original project
path, exactly as the sprint item notes warn. This is precisely why a
retention policy is needed: ``git worktree remove`` (and this repo's own
``worktree_cleanup.remove_worktree_on_disk``) only ever touches the
git-tracked worktree directory — it has no knowledge of, and never touches,
this external keyed directory. Without something reclaiming it, a deleted
worktree's ~1GB detached environment simply lives on forever.

Since Pixi's hashing scheme is an internal implementation detail (not
documented, not something to reverse-engineer and hard-code), this module
takes the same approach as the rest of this codebase's Meridian-managed
state: resolve the real prefix once via ``pixi info --json`` (see
:func:`resolve_pixi_env_prefix`) and record it — both durably in
``meridian.db.worktrees`` (the primary, DB-backed registry) and, for
defense-in-depth / a DB-independent sweep, via a small marker file written
directly into the resolved directory (see :func:`write_worktree_env_marker`
/ :func:`discover_stale_external_envs`).

Scope: self-hosted only, matching ``meridian/worktree_cleanup.py``'s own
documented scope (local-fs-access architectural law, ``meridian/_deps.py``
``require_local_fs_access``, workspace decision 0dedff91) — a hosted
multi-tenant server has no access to a caller's machine.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Generated-cache classification — worktree-local cleanup planning
# ---------------------------------------------------------------------------

#: Top-level directory names inside a worktree that are ALWAYS
#: regeneratable build/dependency/test caches, never real source. Deliberately
#: conservative (exact-name match only, no globbing) so this can never
#: accidentally classify a real source directory as reclaimable.
GENERATED_CACHE_DIR_NAMES: frozenset[str] = frozenset({
    ".pixi",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "htmlcov",
    ".hypothesis",
    "dist",
    "build",
    ".eggs",
})

CLASSIFICATION_SOURCE = "source"
CLASSIFICATION_GENERATED_CACHE = "generated_cache"
CLASSIFICATION_EMPTY_STUB = "empty_stub"


def classify_worktree_entry(entry: Path) -> str:
    """Classify one immediate child of a worktree root.

    Returns ``generated_cache`` for a known regeneratable cache directory
    name (``.pixi``, ``node_modules``, ``__pycache__``, ...), ``empty_stub``
    for a directory that exists but contains nothing, else ``source`` — the
    conservative default for anything not positively identified as
    regeneratable. Never raises: an unreadable entry (permission error, race
    where it vanished) is treated as ``source`` (never a cleanup target)
    rather than guessed at.
    """
    try:
        if entry.name in GENERATED_CACHE_DIR_NAMES:
            return CLASSIFICATION_GENERATED_CACHE
        if entry.is_dir():
            if not any(entry.iterdir()):
                return CLASSIFICATION_EMPTY_STUB
    except OSError:
        return CLASSIFICATION_SOURCE
    return CLASSIFICATION_SOURCE


def _entry_size_bytes(entry: Path) -> int:
    """Best-effort recursive size. Never raises — permission errors or a
    vanished file mid-walk just stop counting that branch."""
    try:
        if entry.is_file():
            return entry.stat().st_size
        if not entry.is_dir():
            return 0
    except OSError:
        return 0
    total = 0
    try:
        for child in entry.rglob("*"):
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def plan_worktree_cache_cleanup(worktree_root: "str | Path") -> dict[str, Any]:
    """Dry-run: classify every immediate child of *worktree_root* and total
    up how many bytes are reclaimable. NEVER deletes anything — this is the
    planning half; see :func:`execute_worktree_cache_cleanup` for the
    guarded mutation.

    Returns ``{"worktree_root": str, "entries": [...], "reclaimable_bytes": int}``.
    An entry is ``{"name", "path", "classification", "size_bytes"}``.
    A missing/non-directory *worktree_root* returns an empty plan rather
    than raising (mirrors ``worktree_cleanup.remove_worktree_on_disk``'s
    "already absent = fine" posture).
    """
    root = Path(worktree_root)
    entries: list[dict[str, Any]] = []
    reclaimable = 0
    if root.is_dir():
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError:
            children = []
        for child in children:
            classification = classify_worktree_entry(child)
            size = _entry_size_bytes(child)
            if classification in (CLASSIFICATION_GENERATED_CACHE, CLASSIFICATION_EMPTY_STUB):
                reclaimable += size
            entries.append({
                "name": child.name,
                "path": str(child),
                "classification": classification,
                "size_bytes": size,
            })
    return {
        "worktree_root": str(root),
        "entries": entries,
        "reclaimable_bytes": reclaimable,
    }


def _pid_is_alive(pid: int) -> bool:
    """Liveness probe. Mirrors ``worktree_cleanup._pid_is_alive``'s exact
    catch tuple (kept as a small, independent copy here rather than an
    import — this module and worktree_cleanup.py cover distinct concerns
    and neither should have to import the other just for a five-line
    liveness check)."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def execute_worktree_cache_cleanup(
    plan: dict[str, Any],
    *,
    confirm: bool = False,
    owner_pid: "int | None" = None,
) -> dict[str, Any]:
    """Guarded, idempotent execution of a plan from
    :func:`plan_worktree_cache_cleanup`.

    Three gates, all of which must pass before anything on disk is touched:

    1. **Active lease** — if *owner_pid* is given and still alive, refuse
       entirely (the worktree may still be in active use; deleting caches
       out from under a live process risks corrupting an in-flight build/
       test run). Mirrors ``worktree_cleanup.validate_worktree_cleanup_target``'s
       PID-liveness gate.
    2. **Confirm** — ``confirm=False`` (the default) never mutates anything;
       callers must opt in explicitly, matching this module's "dry-run
       first" contract.
    3. **Path containment** — each entry's path must resolve to a real
       direct child of ``plan["worktree_root"]``; anything else (a crafted
       or corrupted plan, a symlink escape) is skipped, never deleted. No
       blind recursive deletion.

    Only entries classified ``generated_cache`` or ``empty_stub`` are ever
    removed — ``source`` entries are always skipped regardless of confirm.
    Removal is idempotent: an already-absent path counts as removed, not an
    error.
    """
    if owner_pid is not None and _pid_is_alive(owner_pid):
        return {
            "executed": False,
            "reason": "PROCESS_STILL_LIVE",
            "removed": [],
            "skipped": [],
            "removed_bytes": 0,
        }
    if not confirm:
        return {
            "executed": False,
            "reason": "CONFIRM_REQUIRED",
            "removed": [],
            "skipped": [],
            "removed_bytes": 0,
        }

    try:
        root = Path(plan["worktree_root"]).resolve()
    except (OSError, KeyError, TypeError) as exc:
        return {
            "executed": False,
            "reason": f"INVALID_PLAN: {exc}",
            "removed": [],
            "skipped": [],
            "removed_bytes": 0,
        }

    removed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    removed_bytes = 0
    for raw_entry in plan.get("entries", []):
        entry = dict(raw_entry)
        if entry.get("classification") not in (
            CLASSIFICATION_GENERATED_CACHE, CLASSIFICATION_EMPTY_STUB,
        ):
            skipped.append({**entry, "reason": "not_a_cleanup_target"})
            continue
        try:
            candidate = Path(entry["path"]).resolve()
            candidate.relative_to(root)
        except (OSError, KeyError, ValueError):
            skipped.append({**entry, "reason": "PATH_CONTAINMENT_FAILED"})
            continue
        if not candidate.exists():
            removed.append({**entry, "detail": "already absent"})
            continue
        try:
            if candidate.is_dir():
                shutil.rmtree(candidate, ignore_errors=True)
            else:
                candidate.unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001 — a single removal failure must not abort the batch
            skipped.append({**entry, "reason": f"removal_failed: {exc}"})
            continue
        if candidate.exists():
            skipped.append({**entry, "reason": "removal_incomplete"})
            continue
        removed.append({**entry, "detail": "removed"})
        removed_bytes += int(entry.get("size_bytes") or 0)

    return {
        "executed": True,
        "reason": None,
        "removed": removed,
        "skipped": skipped,
        "removed_bytes": removed_bytes,
    }


# ---------------------------------------------------------------------------
# Detached-environments configuration (global Pixi config, not pixi.toml)
# ---------------------------------------------------------------------------


def default_detached_environments_root() -> Path:
    """Cross-platform default external root for Pixi detached environments —
    ``~/.pixi/workspace-envs`` (matches the sprint-item notes' illustrative
    example, ``C:/Users/13144/.pixi/workspace-envs``, generalized via
    ``Path.home()`` rather than hard-coded so this is portable across
    machines/users/OSes)."""
    return Path.home() / ".pixi" / "workspace-envs"


def ensure_detached_environments_configured(
    value: "str | bool | None" = None,
    *,
    runner: "Callable[[list[str]], subprocess.CompletedProcess] | None" = None,
) -> dict[str, Any]:
    """Idempotently configure Pixi's GLOBAL ``detached-environments`` setting
    via ``pixi config set --global detached-environments <value>``.

    This is deliberately a machine/user-level action (Pixi's global config,
    ``~/.pixi/config.toml`` on this machine) rather than something baked
    into the repo's checked-in ``pixi.toml`` — see the module docstring for
    why ``pixi.toml`` has no such manifest key. *value* defaults to
    :func:`default_detached_environments_root`; pass ``True`` to instead use
    Pixi's own cache-dir-based default location (equivalent to
    ``pixi config set --global detached-environments true``).

    *runner* is the test-injection point (defaults to a real
    ``subprocess.run``); never raises — a missing ``pixi`` binary or any
    other failure is reported via the returned dict, not an exception,
    matching this module's other best-effort helpers.
    """
    if value is None:
        value = str(default_detached_environments_root())
    cmd_value = "true" if value is True else str(value)
    if runner is None:
        def runner(cmd: "list[str]") -> subprocess.CompletedProcess:  # noqa: ANN001
            return subprocess.run(  # noqa: S603, S607 — trusted, fixed argv, no shell
                cmd, capture_output=True, text=True, timeout=30,
            )
    try:
        result = runner([
            "pixi", "config", "set", "--global", "detached-environments", cmd_value,
        ])
    except Exception as exc:  # noqa: BLE001 — missing binary, timeout, etc.
        return {"configured": False, "value": cmd_value, "detail": f"pixi config set failed: {exc}"}
    ok = getattr(result, "returncode", 1) == 0
    detail = ((result.stdout or "") + (result.stderr or "")).strip()
    return {"configured": ok, "value": cmd_value, "detail": detail or ("ok" if ok else "non-zero exit")}


def resolve_pixi_env_prefix(
    cwd: "str | Path",
    *,
    env_name: str = "default",
    runner: "Callable[[list[str], Path], subprocess.CompletedProcess] | None" = None,
) -> "str | None":
    """Resolve the real, on-disk prefix Pixi materializes environment
    *env_name* to for the project rooted at *cwd*, by shelling out to
    ``pixi info --json`` (confirmed live: this is a fast, read-only command
    — it does not trigger a solve/install).

    Returns ``None`` on any failure (no ``pixi`` binary, non-project
    directory, malformed JSON, environment not found) — best-effort,
    matching every other helper in this module. *runner* is the test-
    injection point.
    """
    if runner is None:
        def runner(cmd: "list[str]", c: Path) -> subprocess.CompletedProcess:  # noqa: ANN001
            return subprocess.run(  # noqa: S603, S607
                cmd, cwd=str(c), capture_output=True, text=True, timeout=30,
            )
    try:
        result = runner(["pixi", "info", "--json"], Path(cwd))
    except Exception:  # noqa: BLE001
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError):
        return None
    for env in data.get("environments_info") or []:
        if env.get("name") == env_name:
            prefix = env.get("prefix")
            return str(prefix) if prefix else None
    return None


# ---------------------------------------------------------------------------
# Stale external-environment discovery + reclaim (marker-file based)
# ---------------------------------------------------------------------------

#: Written into the resolved, project-name-hash directory Pixi creates under
#: the detached-environments root (the PARENT of the env's own `envs/<name>`
#: prefix) — NOT inside the env prefix itself, so it survives Pixi re-solving
#: or recreating the `envs/` subdirectory. Records the worktree filesystem
#: path that owns it, so a later sweep can identify orphans without
#: reverse-engineering Pixi's internal hashing scheme.
WORKTREE_MARKER_FILENAME = ".meridian-worktree-path"


def _norm_worktree_path(p: str) -> str:
    """Pure string normalization for path comparison — forward slashes,
    lowercased. Deliberately no filesystem resolution: a dead worktree
    directory may no longer exist on disk by the time this runs. Mirrors
    ``orphan_reaper._norm_path`` (kept as an independent copy, same
    rationale as ``_pid_is_alive`` above)."""
    return p.replace("\\", "/").lower()


def write_worktree_env_marker(env_project_root: "str | Path", worktree_path: str) -> None:
    """Best-effort: record which worktree owns *env_project_root* (the
    resolved project-name-hash directory under the detached-environments
    root). Never raises."""
    try:
        root = Path(env_project_root)
        root.mkdir(parents=True, exist_ok=True)
        (root / WORKTREE_MARKER_FILENAME).write_text(worktree_path, encoding="utf-8")
    except OSError:
        logger.warning(
            "pixi_env_retention: failed to write worktree marker under %s",
            env_project_root, exc_info=True,
        )


def read_worktree_env_marker(env_project_root: "str | Path") -> "str | None":
    """Read back a marker written by :func:`write_worktree_env_marker`, or
    ``None`` if absent/unreadable. Never raises."""
    try:
        marker = Path(env_project_root) / WORKTREE_MARKER_FILENAME
        if not marker.is_file():
            return None
        text = marker.read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None


def discover_stale_external_envs(
    external_root: "str | Path",
    active_worktree_paths: "set[str] | list[str]",
) -> list[dict[str, Any]]:
    """Immediate subdirectories of *external_root* whose marker file points
    at a worktree path NOT present in *active_worktree_paths* — i.e.
    environments whose owning worktree is confirmed gone.

    A subdirectory with no marker at all is skipped (unknown provenance —
    NEVER auto-reclaimed; only marker-confirmed-dead entries are ever
    reported as candidates, matching the "no blind recursive deletion"
    contract). Returns ``[]`` (never raises) if *external_root* doesn't
    exist.
    """
    root = Path(external_root)
    if not root.is_dir():
        return []
    active_norm = {_norm_worktree_path(p) for p in active_worktree_paths if p}
    stale: list[dict[str, Any]] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return []
    for child in children:
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        marker = read_worktree_env_marker(child)
        if marker is None:
            continue
        if _norm_worktree_path(marker) not in active_norm:
            stale.append({"path": str(child), "worktree_path": marker})
    return stale


def find_external_envs_for_dead_worktrees(
    external_root: "str | Path",
    dead_paths: "list[str]",
) -> list[dict[str, Any]]:
    """Immediate subdirectories of *external_root* whose marker file exactly
    matches (after normalization) one of *dead_paths*.

    The precise, positive-match counterpart to :func:`discover_stale_external_envs`
    (which works from the "keep" set): this one works from an already-
    confirmed "dead" set — mirrors ``orphan_reaper.process_belongs_to_dead_worktree``'s
    own matching semantics, so callers (``orphan_reaper.reclaim_stale_pixi_envs``)
    only ever act on paths they already know are dead, never guess from
    absence.
    """
    root = Path(external_root)
    if not dead_paths or not root.is_dir():
        return []
    norm_dead = {_norm_worktree_path(p) for p in dead_paths if p}
    out: list[dict[str, Any]] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return []
    for child in children:
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        marker = read_worktree_env_marker(child)
        if marker is None:
            continue
        if _norm_worktree_path(marker) in norm_dead:
            out.append({"path": str(child), "worktree_path": marker})
    return out


def reclaim_external_env(
    env_root: "str | Path",
    external_root: "str | Path",
    *,
    confirm: bool = False,
) -> dict[str, Any]:
    """Guarded, idempotent removal of one external environment directory.

    Path-containment gate: *env_root* must resolve to somewhere INSIDE
    *external_root* — a caller-supplied or racy path pointing anywhere else
    is refused outright, never touched. ``confirm=False`` (the default) is
    dry-run only. An already-absent directory counts as successfully
    removed (idempotent — calling this twice on the same target is safe).
    """
    try:
        resolved_root = Path(external_root).resolve()
        resolved_env = Path(env_root).resolve()
    except OSError as exc:
        return {"attempted": False, "removed": False, "detail": f"path resolution failed: {exc}"}
    try:
        resolved_env.relative_to(resolved_root)
    except ValueError:
        return {
            "attempted": False,
            "removed": False,
            "detail": "refusing: env_root is not contained within external_root",
        }
    if not resolved_env.exists():
        return {"attempted": False, "removed": True, "detail": "already absent"}
    if not confirm:
        return {"attempted": False, "removed": False, "detail": "confirm=False -- dry run only"}
    try:
        shutil.rmtree(resolved_env, ignore_errors=True)
    except OSError as exc:  # noqa: BLE001
        return {"attempted": True, "removed": not resolved_env.exists(), "detail": str(exc)}
    removed = not resolved_env.exists()
    return {"attempted": True, "removed": removed, "detail": "rmtree" if removed else "rmtree incomplete"}


def _cli_configure(argv: "list[str] | None" = None) -> int:
    """``python -m meridian.pixi_env_retention --configure [--path P]`` —
    one-time-per-machine setup entry point, kept as a thin CLI so it can be
    run manually or wired into a hook/task without needing server.py
    changes. Always exits 0 on success, 1 on failure; prints the outcome."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Configure Pixi's global detached-environments setting (15610335).",
    )
    parser.add_argument("--configure", action="store_true", help="Apply the config (required).")
    parser.add_argument("--path", default=None, help="Override the external root (default: ~/.pixi/workspace-envs).")
    args = parser.parse_args(argv)
    if not args.configure:
        parser.print_help()
        return 1
    result = ensure_detached_environments_configured(args.path)
    print(json.dumps(result), file=sys.stderr)
    return 0 if result["configured"] else 1


if __name__ == "__main__":
    raise SystemExit(_cli_configure())
