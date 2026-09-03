"""Tests for meridian/repo_scope.py — explicit repo-scope guard (ba31dedf).

Covers:
  * validate_repo_scope() — the filesystem-resolving validator used by
    tunnel_client.run_tunnel (local process, real Path.home() access).
  * looks_like_bare_home_directory() — the string-shape heuristic used by
    mcp/handler.py's set_active_repo (server-side, no real filesystem access
    to the remote caller's home directory).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from meridian.repo_scope import (
    RepoScopeError,
    looks_like_bare_home_directory,
    validate_repo_scope,
)


# ---------------------------------------------------------------------------
# validate_repo_scope()
# ---------------------------------------------------------------------------

class TestValidateRepoScope:
    def test_ordinary_project_subdirectory_is_accepted(self, tmp_path):
        home = tmp_path / "home" / "me"
        home.mkdir(parents=True)
        project = home / "Documents" / "myproject"
        project.mkdir(parents=True)
        resolved = validate_repo_scope(str(project), home=home)
        assert resolved == project.resolve()

    def test_bare_home_directory_is_rejected(self, tmp_path):
        home = tmp_path / "home" / "me"
        home.mkdir(parents=True)
        with pytest.raises(RepoScopeError, match="home directory"):
            validate_repo_scope(str(home), home=home)

    def test_ancestor_of_home_is_rejected(self, tmp_path):
        """A filesystem root that encompasses the whole home tree (e.g. the
        Windows drive root / a Users directory) must never be silently
        accepted -- that's an even broader scope than the home dir itself."""
        home = tmp_path / "Users" / "me"
        home.mkdir(parents=True)
        ancestor = tmp_path / "Users"
        with pytest.raises(RepoScopeError, match="home directory"):
            validate_repo_scope(str(ancestor), home=home)

    def test_deep_descendant_of_home_is_never_rejected(self, tmp_path):
        """Sanity check for the asymmetry: home/a/b/c is a DESCENDANT of home,
        not an ancestor -- must be treated as an ordinary project path."""
        home = tmp_path / "home" / "me"
        deep = home / "a" / "b" / "c" / "project"
        deep.mkdir(parents=True)
        resolved = validate_repo_scope(str(deep), home=home)
        assert resolved == deep.resolve()

    def test_missing_repo_path_and_cwd_is_ambiguous(self):
        with pytest.raises(RepoScopeError, match="ambiguous"):
            validate_repo_scope(None, cwd=None)

    def test_falls_back_to_cwd_when_repo_path_absent(self, tmp_path):
        home = tmp_path / "home" / "me"
        home.mkdir(parents=True)
        project = home / "project"
        project.mkdir()
        resolved = validate_repo_scope(None, cwd=str(project), home=home)
        assert resolved == project.resolve()

    def test_cwd_defaulting_to_home_is_rejected(self, tmp_path):
        """The concrete bug this closes: `run_tunnel` used to default an unset
        --repo to Path.cwd() with zero rejection -- if that cwd IS the home
        directory (a bare shell session), it must fail closed, not silently
        scope to the whole home tree."""
        home = tmp_path / "home" / "me"
        home.mkdir(parents=True)
        with pytest.raises(RepoScopeError, match="home directory"):
            validate_repo_scope(None, cwd=str(home), home=home)

    def test_registered_repo_path_match_is_accepted(self, tmp_path):
        home = tmp_path / "home" / "me"
        project = home / "project"
        project.mkdir(parents=True)
        resolved = validate_repo_scope(
            str(project), home=home, registered_repo_path=str(project)
        )
        assert resolved == project.resolve()

    def test_registered_repo_path_subdirectory_is_accepted(self, tmp_path):
        home = tmp_path / "home" / "me"
        project = home / "project"
        sub = project / "packages" / "core"
        sub.mkdir(parents=True)
        resolved = validate_repo_scope(
            str(sub), home=home, registered_repo_path=str(project)
        )
        assert resolved == sub.resolve()

    def test_registered_repo_path_mismatch_is_rejected(self, tmp_path):
        """Cross-project scope mismatch fails closed."""
        home = tmp_path / "home" / "me"
        project_a = home / "project-a"
        project_b = home / "project-b"
        project_a.mkdir(parents=True)
        project_b.mkdir(parents=True)
        with pytest.raises(RepoScopeError, match="cross-project scope mismatch"):
            validate_repo_scope(
                str(project_a), home=home, registered_repo_path=str(project_b)
            )

    def test_returns_a_resolved_path_object(self, tmp_path):
        home = tmp_path / "home" / "me"
        project = home / "project"
        project.mkdir(parents=True)
        resolved = validate_repo_scope(str(project), home=home)
        assert isinstance(resolved, Path)
        assert resolved.is_absolute()


# ---------------------------------------------------------------------------
# looks_like_bare_home_directory()
# ---------------------------------------------------------------------------

class TestLooksLikeBareHomeDirectory:
    @pytest.mark.parametrize("path", [
        "C:\\Users\\me",
        "C:\\Users\\me\\",
        "C:/Users/me",
        "/home/me",
        "/home/me/",
        "/Users/me",
        "/root",
        "/root/",
    ])
    def test_bare_home_shapes_are_flagged(self, path):
        assert looks_like_bare_home_directory(path), f"expected {path!r} to be flagged"

    @pytest.mark.parametrize("path", [
        "C:\\Users\\me\\project",
        "C:\\Users\\me\\Documents\\Meridian\\repository",
        "/home/me/project",
        "/home/me/Documents/myproject",
        "/Users/me/code/thing",
        "/root/project",
        "",
        None,
        "relative/project/path",
        "C:\\Projects\\myapp",
        "/opt/myapp",
    ])
    def test_project_subdirectories_and_unrelated_paths_are_not_flagged(self, path):
        assert not looks_like_bare_home_directory(path), f"did not expect {path!r} to be flagged"

    def test_matches_the_exact_bug_from_meridian_connect(self):
        """meridian_connect.py's local fallback used to unconditionally
        `cd "$HOME"` -- confirm the heuristic actually catches a realistic
        $HOME-shaped string on both platforms it targets."""
        assert looks_like_bare_home_directory("C:\\Users\\adam")
        assert looks_like_bare_home_directory("/home/adam")

    def test_matches_the_set_active_repo_worktree_id_rigor(self):
        """set_active_repo's plain repo_path branch should reject a bare home
        directory the same way its worktree_id branch is validated -- this is
        the string a real MCP caller could plausibly (accidentally) pass."""
        assert looks_like_bare_home_directory("/home/user")
        assert not looks_like_bare_home_directory("/home/user/repo")
        assert not looks_like_bare_home_directory("/home/user/myrepo")
