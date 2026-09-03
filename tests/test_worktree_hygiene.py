"""Tests for the repo-wide worktree hygiene command."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from meridian.worktree_hygiene import (
    DEFAULT_PROTECTED_PATTERNS,
    WorktreeRecord,
    inspect_orphan_directories,
    _validate_archive_root,
    apply_cleanup,
    build_cleanup_plan,
    inspect_worktrees,
    parse_worktree_porcelain,
)


def test_parse_porcelain_supports_branch_detached_and_locked() -> None:
    rows = parse_worktree_porcelain(
        """worktree C:/repo
HEAD abc123
branch refs/heads/dev

worktree C:/scratch/detached
HEAD def456
detached
locked reason
"""
    )

    assert rows == [
        {
            "path": "C:/repo",
            "head": "abc123",
            "branch": "refs/heads/dev",
            "locked": False,
        },
        {
            "path": "C:/scratch/detached",
            "head": "def456",
            "branch": None,
            "locked": True,
        },
    ]


def _record(
    path: str,
    *,
    branch: str | None = "refs/heads/worktree/test",
    dirty_count: int = 0,
    locked: bool = False,
    protected: bool = False,
    protected_reason: str | None = None,
    exists: bool = True,
) -> WorktreeRecord:
    return WorktreeRecord(
        path=path,
        head="abc123",
        branch=branch,
        exists=exists,
        dirty_count=dirty_count,
        locked=locked,
        protected=protected,
        protected_reason=protected_reason,
    )


def test_cleanup_plan_separates_safe_dirty_locked_and_protected() -> None:
    plan = build_cleanup_plan(
        [
            _record("C:/repo", protected=True, protected_reason="repository root"),
            _record("C:/clean"),
            _record("C:/dirty", dirty_count=2),
            _record("C:/locked", locked=True),
            _record(
                "C:/outputs",
                protected=True,
                protected_reason="protected pattern matched",
            ),
        ]
    )

    assert plan["safe_removable_count"] == 1
    assert [item["path"] for item in plan["removable"]] == ["C:/clean"]
    assert [item["path"] for item in plan["dirty_candidates"]] == ["C:/dirty"]
    assert [item["path"] for item in plan["locked"]] == ["C:/locked"]
    assert [item["path"] for item in plan["protected"]] == ["C:/outputs"]
    assert [item["path"] for item in plan["root"]] == ["C:/repo"]


def test_explicit_keep_path_is_respected() -> None:
    keep = str(Path("C:/keep").resolve())
    plan = build_cleanup_plan([_record(keep)], keep_paths=[keep])
    assert plan["safe_removable_count"] == 0
    assert plan["protected"][0]["keep_reason"] == "explicit keep path"


def test_default_protection_pattern_covers_outputs_and_docs_branches() -> None:
    assert any("outputs" in pattern for pattern in DEFAULT_PROTECTED_PATTERNS)
    assert any("docs" in pattern for pattern in DEFAULT_PROTECTED_PATTERNS)
    assert any("megasprint-clean" in pattern for pattern in DEFAULT_PROTECTED_PATTERNS)


def test_archive_root_must_be_outside_repo(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside the repository"):
        _validate_archive_root(tmp_path, tmp_path / "archive")


def test_record_serialization_is_json_safe() -> None:
    record = _record("C:/clean")
    assert json.loads(json.dumps(record.to_dict()))["path"] == "C:/clean"


def test_orphan_discovery_skips_registered_children(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    root.mkdir()
    registered = root / "registered"
    registered.mkdir()
    orphan = root / "orphan"
    orphan.mkdir()
    (orphan / "result.txt").write_text("result\n", encoding="utf-8")

    found = inspect_orphan_directories([root], known_paths=[registered])

    assert [item.path for item in found] == [str(orphan.resolve())]
    assert found[0].file_count == 1
    assert found[0].size_bytes == (orphan / "result.txt").stat().st_size


def test_orphan_plan_requires_explicit_ack_for_nonempty_directory(tmp_path: Path) -> None:
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    (orphan / "result.txt").write_text("result\n", encoding="utf-8")
    record = inspect_orphan_directories([tmp_path])[0]

    plan = build_cleanup_plan([], orphan_directories=[record])

    assert plan["safe_orphan_removable_count"] == 0
    assert plan["orphan_nonempty"][0]["path"] == str(orphan.resolve())


def test_apply_archives_dirty_tree_removes_registration_and_keeps_branch(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "hygiene-test"],
        check=True,
    )
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)

    worktree = tmp_path / "repo-worktree-test"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "cleanup-test", str(worktree)],
        check=True,
    )
    (worktree / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (worktree / "untracked.txt").write_text("keep me\n", encoding="utf-8")

    records = inspect_worktrees(repo, protected_patterns=())
    result = apply_cleanup(
        repo,
        records,
        archive_root=tmp_path / "archive",
        allow_dirty=True,
    )

    assert result["removed_count"] == 1
    assert result["skipped_count"] == 0
    assert not worktree.exists()
    assert subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", "refs/heads/cleanup-test"],
        check=False,
    ).returncode == 0
    archived = list((tmp_path / "archive").glob("*/untracked/untracked.txt"))
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == "keep me\n"
