"""Safe inventory and cleanup for locally registered Git worktrees.

Meridian's database-owned cleanup path handles worktrees created by a
Meridian sprint item.  This module covers the larger local failure mode:
detached Codex/Claude worktrees, abandoned verification trees, and other
registrations that outlive their session row.

The command is intentionally dry-run first.  Applying a plan requires an
explicit archive directory; dirty trees require the additional explicit
``--allow-dirty`` flag.  Protected patterns keep document, output, OOXML,
paper, and dissertation worktrees out of generic cleanup.  Branches are
never deleted by this module.

Usage::

    python -m meridian.worktree_hygiene --repo-root .
    python -m meridian.worktree_hygiene --repo-root . --apply \
        --archive-dir E:/MeridianData/worktree-quarantine
    python -m meridian.worktree_hygiene --repo-root . --apply \
        --archive-dir E:/MeridianData/worktree-quarantine --allow-dirty
    python -m meridian.worktree_hygiene --repo-root . --orphan-root \
        C:/Users/me/.codex/worktrees --json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


# Deliberately matches a semantic path/branch component, not the word
# ``docs`` inside ``Documents``.  The defaults are conservative because the
# cleanup command is repo-wide and document/output worktrees are explicitly
# valuable even when their owning session is no longer visible.
DEFAULT_PROTECTED_PATTERNS: tuple[str, ...] = (
    r"(?i)(?:^|[/\\_-])(docs|outputs|ooxml|paper|dissertation|megasprint-clean)(?:[/\\_-]|$)",
)


@dataclass(frozen=True)
class WorktreeRecord:
    """A point-in-time record from ``git worktree list --porcelain``."""

    path: str
    head: str
    branch: str | None
    exists: bool
    dirty_count: int
    locked: bool
    protected: bool
    protected_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OrphanDirectoryRecord:
    """A directory under an operator-supplied worktree scratch root."""

    path: str
    root: str
    exists: bool
    file_count: int
    size_bytes: int
    protected: bool
    protected_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_worktree_porcelain(output: str) -> list[dict[str, Any]]:
    """Parse Git's porcelain worktree listing without depending on ordering."""

    rows: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("worktree "):
            if current is not None:
                rows.append(current)
            current = {
                "path": line[len("worktree ") :],
                "head": "",
                "branch": None,
                "locked": False,
            }
            continue
        if current is None:
            continue
        if line.startswith("HEAD "):
            current["head"] = line[len("HEAD ") :]
        elif line.startswith("branch "):
            current["branch"] = line[len("branch ") :]
        elif line == "detached":
            current["branch"] = None
        elif line == "locked" or line.startswith("locked "):
            current["locked"] = True
    if current is not None:
        rows.append(current)
    return rows


def _run_git(repo_root: Path, args: Sequence[str], *, text: bool = True) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        check=False,
        text=text,
    )


def _status_count(path: Path) -> int:
    result = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain=v1"],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        return 0
    return len([line for line in result.stdout.splitlines() if line.strip()])


def _protected_reason(
    path: str,
    branch: str | None,
    patterns: Iterable[str],
) -> str | None:
    for pattern in patterns:
        matcher = re.compile(pattern)
        if matcher.search(path) or (branch and matcher.search(branch)):
            return f"protected pattern matched: {pattern}"
    return None


def inspect_worktrees(
    repo_root: Path,
    *,
    protected_patterns: Sequence[str] = DEFAULT_PROTECTED_PATTERNS,
) -> list[WorktreeRecord]:
    """Return a fresh inventory, including dirty and protected state."""

    repo_root = repo_root.resolve()
    listing = _run_git(repo_root, ["worktree", "list", "--porcelain"])
    if listing.returncode != 0:
        raise RuntimeError(listing.stderr.strip() or "git worktree list failed")

    records: list[WorktreeRecord] = []
    for row in parse_worktree_porcelain(listing.stdout):
        path = Path(row["path"]).resolve()
        exists = path.exists()
        dirty_count = _status_count(path) if exists else 0
        reason = _protected_reason(str(path), row.get("branch"), protected_patterns)
        if path == repo_root:
            reason = "repository root"
        records.append(
            WorktreeRecord(
                path=str(path),
                head=str(row.get("head") or ""),
                branch=row.get("branch"),
                exists=exists,
                dirty_count=dirty_count,
                locked=bool(row.get("locked")),
                protected=reason is not None,
                protected_reason=reason,
            )
        )
    return records


def inspect_orphan_directories(
    roots: Iterable[str | Path],
    *,
    known_paths: Iterable[str] = (),
    protected_patterns: Sequence[str] = DEFAULT_PROTECTED_PATTERNS,
) -> list[OrphanDirectoryRecord]:
    """Find unregistered child directories under explicitly named roots.

    ``roots`` is intentionally caller-supplied.  A local `.codex/worktrees`
    directory can contain another repository's scratch state, so Meridian
    must not infer that every app-level scratch directory belongs to this
    repository.  Registered paths are excluded even if their names do not
    match a protected pattern.
    """

    known = {str(Path(path).resolve()) for path in known_paths}
    found: list[OrphanDirectoryRecord] = []
    for raw_root in roots:
        root = Path(raw_root).resolve()
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if not child.is_dir():
                continue
            path = child.resolve()
            if str(path) in known:
                continue
            files = [item for item in path.rglob("*") if item.is_file()]
            reason = _protected_reason(str(path), None, protected_patterns)
            found.append(
                OrphanDirectoryRecord(
                    path=str(path),
                    root=str(root),
                    exists=True,
                    file_count=len(files),
                    size_bytes=sum(item.stat().st_size for item in files),
                    protected=reason is not None,
                    protected_reason=reason,
                )
            )
    return found


def build_cleanup_plan(
    records: Sequence[WorktreeRecord],
    *,
    keep_paths: Iterable[str] = (),
    orphan_directories: Sequence[OrphanDirectoryRecord] = (),
) -> dict[str, Any]:
    """Classify records without changing disk state.

    ``removable`` contains only existing, unprotected, unlocked, clean
    worktrees.  Dirty records are separated into ``dirty_candidates`` and
    require an explicit archive plus ``allow_dirty`` when applying.
    """

    keep = {str(Path(path).resolve()) for path in keep_paths}
    root: list[dict[str, Any]] = []
    protected: list[dict[str, Any]] = []
    locked: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    dirty: list[dict[str, Any]] = []
    removable: list[dict[str, Any]] = []
    orphan_protected: list[dict[str, Any]] = []
    orphan_empty: list[dict[str, Any]] = []
    orphan_nonempty: list[dict[str, Any]] = []

    for record in records:
        item = record.to_dict()
        if record.protected or record.path in keep:
            item["keep_reason"] = record.protected_reason or "explicit keep path"
            (root if record.protected_reason == "repository root" else protected).append(item)
        elif record.locked:
            locked.append(item)
        elif not record.exists:
            missing.append(item)
        elif record.dirty_count:
            dirty.append(item)
        else:
            removable.append(item)

    for orphan in orphan_directories:
        item = orphan.to_dict()
        if orphan.protected or orphan.path in keep:
            item["keep_reason"] = orphan.protected_reason or "explicit keep path"
            orphan_protected.append(item)
        elif orphan.file_count:
            orphan_nonempty.append(item)
        else:
            orphan_empty.append(item)

    return {
        "total": len(records),
        "root": root,
        "protected": protected,
        "locked": locked,
        "missing": missing,
        "dirty_candidates": dirty,
        "removable": removable,
        "safe_removable_count": len(removable),
        "dirty_candidate_count": len(dirty),
        "orphan_total": len(orphan_directories),
        "orphan_protected": orphan_protected,
        "orphan_empty": orphan_empty,
        "orphan_nonempty": orphan_nonempty,
        "safe_orphan_removable_count": len(orphan_empty),
    }


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def archive_worktree_snapshot(
    repo_root: Path,
    record: WorktreeRecord,
    archive_root: Path,
    *,
    archive_name: str,
) -> Path:
    """Archive status, binary-safe diffs, and untracked files for one tree."""

    destination = archive_root / archive_name
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "metadata.json").write_text(
        json.dumps(record.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    if not record.exists:
        return destination

    status = _run_git(Path(record.path), ["status", "--porcelain=v1"])
    (destination / "status.txt").write_text(status.stdout, encoding="utf-8")
    for name, args in (
        ("unstaged.diff", ["diff", "--binary"]),
        ("staged.diff", ["diff", "--cached", "--binary"]),
    ):
        diff = _run_git(Path(record.path), args, text=False)
        _write_bytes(destination / name, diff.stdout or b"")

    untracked = _run_git(
        Path(record.path), ["ls-files", "--others", "--exclude-standard"]
    )
    for relative in untracked.stdout.splitlines():
        source = Path(record.path) / relative
        if source.is_file():
            target = destination / "untracked" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return destination


def archive_orphan_snapshot(
    orphan: OrphanDirectoryRecord,
    archive_root: Path,
    *,
    archive_name: str,
) -> Path:
    """Archive an unregistered scratch directory before removing it."""

    destination = archive_root / "orphans" / archive_name
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "metadata.json").write_text(
        json.dumps(orphan.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    source = Path(orphan.path)
    if source.exists() and orphan.file_count:
        shutil.copytree(source, destination / "files", dirs_exist_ok=True)
    return destination


def _validate_archive_root(repo_root: Path, archive_root: Path) -> Path:
    repo_root = repo_root.resolve()
    archive_root = archive_root.resolve()
    if archive_root == repo_root:
        raise ValueError("archive directory must not be the repository root")
    try:
        archive_root.relative_to(repo_root)
    except ValueError:
        return archive_root
    raise ValueError("archive directory must be outside the repository")


def apply_cleanup(
    repo_root: Path,
    records: Sequence[WorktreeRecord],
    *,
    archive_root: Path,
    keep_paths: Iterable[str] = (),
    allow_dirty: bool = False,
    orphan_directories: Sequence[OrphanDirectoryRecord] = (),
    allow_orphans: bool = False,
) -> dict[str, Any]:
    """Archive and remove the current plan; never delete branch refs."""

    repo_root = repo_root.resolve()
    archive_root = _validate_archive_root(repo_root, archive_root)
    plan = build_cleanup_plan(
        records, keep_paths=keep_paths, orphan_directories=orphan_directories
    )
    archive_root.mkdir(parents=True, exist_ok=True)
    removed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    candidates = list(plan["removable"])
    if allow_dirty:
        candidates.extend(plan["dirty_candidates"])
    else:
        skipped.extend(
            {**item, "reason": "dirty; rerun with --allow-dirty"}
            for item in plan["dirty_candidates"]
        )

    # Children first avoids a parent-directory removal invalidating a nested
    # worktree registration before it gets its own snapshot.
    candidates.sort(key=lambda item: len(item["path"]), reverse=True)
    for index, item in enumerate(candidates, start=1):
        record = WorktreeRecord(**{key: item[key] for key in WorktreeRecord.__dataclass_fields__})
        archive_name = f"{index:04d}-{re.sub(r'[^A-Za-z0-9._-]+', '_', record.path).strip('_')}"
        archive_worktree_snapshot(
            repo_root, record, archive_root, archive_name=archive_name
        )
        result = _run_git(repo_root, ["worktree", "remove", "--force", record.path])
        if result.returncode == 0 and not Path(record.path).exists():
            removed.append({"path": record.path, "branch": record.branch})
        else:
            skipped.append(
                {
                    **item,
                    "reason": result.stderr.strip() or "git worktree remove failed",
                }
            )

    orphan_skipped = [
        {**item, "reason": "non-empty orphan; rerun with --allow-orphans"}
        for item in plan["orphan_nonempty"]
        if not allow_orphans
    ]
    orphan_candidates = list(plan["orphan_empty"])
    if allow_orphans:
        orphan_candidates.extend(plan["orphan_nonempty"])
    orphan_removed: list[dict[str, Any]] = []
    for index, item in enumerate(
        sorted(orphan_candidates, key=lambda value: len(value["path"]), reverse=True),
        start=1,
    ):
        orphan = OrphanDirectoryRecord(
            **{key: item[key] for key in OrphanDirectoryRecord.__dataclass_fields__}
        )
        archive_orphan_snapshot(
            orphan,
            archive_root,
            archive_name=f"{index:04d}-{re.sub(r'[^A-Za-z0-9._-]+', '_', orphan.path).strip('_')}",
        )
        source = Path(orphan.path)
        try:
            if orphan.file_count:
                shutil.rmtree(source)
            else:
                source.rmdir()
        except OSError as exc:
            orphan_skipped.append({**item, "reason": str(exc)})
        else:
            if not source.exists():
                orphan_removed.append({"path": orphan.path, "root": orphan.root})
            else:
                orphan_skipped.append({**item, "reason": "directory still exists"})

    skipped.extend(orphan_skipped)

    prune = _run_git(repo_root, ["worktree", "prune"])
    return {
        "archive_root": str(archive_root),
        "removed": removed,
        "skipped": skipped,
        "removed_count": len(removed),
        "skipped_count": len(skipped),
        "orphan_removed": orphan_removed,
        "orphan_removed_count": len(orphan_removed),
        "prune_returncode": prune.returncode,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".", help="Git repository to inspect")
    parser.add_argument(
        "--apply", action="store_true", help="Apply the dry-run plan"
    )
    parser.add_argument(
        "--archive-dir",
        help="Required with --apply; archive outside the repository",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow archived dirty worktrees to be removed",
    )
    parser.add_argument(
        "--keep-path", action="append", default=[], help="Additional path to retain"
    )
    parser.add_argument(
        "--orphan-root",
        action="append",
        default=[],
        help="Explicit scratch root whose unregistered child directories should be inventoried",
    )
    parser.add_argument(
        "--allow-orphans",
        action="store_true",
        help="Allow archived unregistered scratch directories to be removed",
    )
    parser.add_argument(
        "--protect-pattern",
        action="append",
        default=[],
        help="Additional regex protecting a path or branch",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.allow_dirty and not args.apply:
        _parser().error("--allow-dirty requires --apply")
    if args.allow_orphans and not args.apply:
        _parser().error("--allow-orphans requires --apply")
    if args.apply and not args.archive_dir:
        _parser().error("--apply requires --archive-dir outside the repository")

    repo_root = Path(args.repo_root).resolve()
    patterns = (*DEFAULT_PROTECTED_PATTERNS, *args.protect_pattern)
    records = inspect_worktrees(repo_root, protected_patterns=patterns)
    orphan_roots = args.orphan_root or [str(repo_root / ".claude" / "worktrees")]
    orphan_directories = inspect_orphan_directories(
        orphan_roots,
        known_paths=(record.path for record in records),
        protected_patterns=patterns,
    )
    result: dict[str, Any] = build_cleanup_plan(
        records,
        keep_paths=args.keep_path,
        orphan_directories=orphan_directories,
    )
    if args.apply:
        result = apply_cleanup(
            repo_root,
            records,
            archive_root=Path(args.archive_dir),
            keep_paths=args.keep_path,
            allow_dirty=args.allow_dirty,
            orphan_directories=orphan_directories,
            allow_orphans=args.allow_orphans,
        )
    result["repo_root"] = str(repo_root)
    result["mode"] = "apply" if args.apply else "dry-run"
    output = json.dumps(result, indent=2, sort_keys=True)
    if args.json:
        print(output)
    else:
        print(output)
    return 0 if not result.get("skipped") else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
