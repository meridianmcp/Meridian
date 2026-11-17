"""DELETE /projects/{id}/worktrees/{id} keeps a row active while its directory is
still on disk.

The route used to ignore the outcome of the guarded disk removal and mark the
row removed regardless. A refused removal (a dirty tree, or an owning process
that is still alive) left the directory behind while the row said "removed",
and the periodic sweep only selects active rows, so it never retried it. These
tests pin the fixed behaviour: a refused or failed removal answers 409 and
leaves the row active, while a clean removal still answers 204 and removes the
row.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Git identity and signing off, so commits made by these fixtures never depend
# on the developer's global git config.
_GIT_ARGS = [
    "-c", "user.name=test",
    "-c", "user.email=test@example.invalid",
    "-c", "commit.gpgsign=false",
]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *_GIT_ARGS, *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    )


def _init_repo_with_commit(path: Path) -> None:
    """A standalone git repo with one committed file and a clean status."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    (path / "tracked.txt").write_text("committed")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-q", "-m", "init")


def _setup(client, monkeypatch, repo_root: Path, name: str) -> tuple[str, str]:
    """Point the server at *repo_root*, then create a project and one session."""
    import meridian.server as srv

    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    monkeypatch.setattr(srv, "_REPO_ROOT", repo_root)
    proj_id = client.post("/projects", json={"name": name}).json()["id"]
    sess = client.post(
        f"/projects/{proj_id}/start-session", json={"session_name": f"{name}-sess"}
    ).json()
    return proj_id, sess["session_id"]


def _git_worktree(repo_root: Path, name: str) -> Path:
    """A real, git-registered worktree at the convention path the server uses."""
    wt_dir = repo_root / ".claude" / "worktrees" / name
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "worktree", "add", "-q", str(wt_dir), "-b", f"worktree/{name}")
    return wt_dir


def _register(client, proj_id: str, sess_id: str, name: str, owner_pid: int | None = None) -> dict:
    body = {
        "session_id": sess_id,
        "branch": f"worktree/{name}",
        "path": f".claude/worktrees/{name}",
    }
    if owner_pid is not None:
        body["pid"] = owner_pid
    r = client.post(f"/projects/{proj_id}/worktrees", json=body)
    assert r.status_code == 201
    return r.json()


def _active_ids(client, proj_id: str) -> list[str]:
    return [w["id"] for w in client.get(f"/projects/{proj_id}/worktrees").json()]


def test_delete_dirty_worktree_returns_409_and_keeps_the_row_active(client, monkeypatch, tmp_path: Path):
    repo_root = tmp_path / "repo"
    _init_repo_with_commit(repo_root)
    proj_id, sess_id = _setup(client, monkeypatch, repo_root, "wt-del-dirty")
    wt_dir = _git_worktree(repo_root, "dirty")
    (wt_dir / "unsaved.txt").write_text("unsaved work")
    wt = _register(client, proj_id, sess_id, "dirty")

    r = client.delete(f"/projects/{proj_id}/worktrees/{wt['id']}")

    assert r.status_code == 409
    assert "DIRTY_WORKTREE" in r.json()["detail"]
    assert (wt_dir / "unsaved.txt").read_text() == "unsaved work"
    assert wt["id"] in _active_ids(client, proj_id)


def test_delete_clean_worktree_still_returns_204_and_removes_the_row(client, monkeypatch, tmp_path: Path):
    repo_root = tmp_path / "repo"
    _init_repo_with_commit(repo_root)
    proj_id, sess_id = _setup(client, monkeypatch, repo_root, "wt-del-clean")
    wt_dir = _git_worktree(repo_root, "clean")
    wt = _register(client, proj_id, sess_id, "clean")

    r = client.delete(f"/projects/{proj_id}/worktrees/{wt['id']}")

    assert r.status_code == 204
    assert not wt_dir.exists()
    assert wt["id"] not in _active_ids(client, proj_id)
    # Once removed, the row is gone for good: a second DELETE is a 404.
    assert client.delete(f"/projects/{proj_id}/worktrees/{wt['id']}").status_code == 404


def test_delete_worktree_with_a_live_owner_returns_409_and_keeps_the_row_active(
    client, monkeypatch, tmp_path: Path
):
    repo_root = tmp_path / "repo"
    _init_repo_with_commit(repo_root)
    proj_id, sess_id = _setup(client, monkeypatch, repo_root, "wt-del-live")
    wt_dir = _git_worktree(repo_root, "owned")
    # The test process is alive, so the guard refuses the disk removal (guard_ok=False).
    wt = _register(client, proj_id, sess_id, "owned", owner_pid=os.getpid())

    r = client.delete(f"/projects/{proj_id}/worktrees/{wt['id']}")

    assert r.status_code == 409
    assert "PROCESS_STILL_LIVE" in r.json()["detail"]
    assert wt_dir.exists()
    assert wt["id"] in _active_ids(client, proj_id)


def test_failed_disk_removal_returns_409_and_keeps_the_row_active(client, monkeypatch, tmp_path: Path):
    import meridian.worktree_cleanup as wc_module

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    proj_id, sess_id = _setup(client, monkeypatch, repo_root, "wt-del-failed")
    monkeypatch.setattr(
        wc_module, "remove_worktree_on_disk",
        lambda repo_root, wt_path: {"attempted": True, "removed": False, "detail": "rmtree failed"},
    )
    wt = _register(client, proj_id, sess_id, "stuck")

    r = client.delete(f"/projects/{proj_id}/worktrees/{wt['id']}")

    assert r.status_code == 409
    assert "rmtree failed" in r.json()["detail"]
    assert wt["id"] in _active_ids(client, proj_id)
