"""a03c0eeb -- worktree disk cleanup enforcement.

`delete_worktree` used to only flip `active_worktrees.removed_at` in the DB
without ever confirming the directory was actually removed from disk. These
tests cover the real fix:

1. `worktree_cleanup.remove_worktree_on_disk` -- unit-level real removal
   (git-worktree-remove path + rmtree fallback), never raises.
2. `db_module.list_worktrees_pending_cleanup` -- finds worktrees whose owning
   sprint item/session reached a terminal state but are still marked active.
3. `worktree_cleanup.sweep_stale_worktrees` -- the periodic sweep wires (1)
   and (2) together and actually reclaims the directories + DB rows.
4. `DELETE /projects/{id}/worktrees/{id}` -- self-hosted now performs a real
   best-effort disk removal in addition to the DB update; hosted mode stays
   DB-only (no filesystem access to the caller's machine).
5. `POST /projects/{id}/worktrees/sweep` -- on-demand trigger used by the
   Stop-hook sprint guard's post-integration cleanup call.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from meridian import db as db_module
from meridian import worktree_cleanup


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


# ---------------------------------------------------------------------------
# 1. remove_worktree_on_disk — unit level
# ---------------------------------------------------------------------------


def test_remove_worktree_on_disk_missing_path_is_a_noop(tmp_path: Path):
    """Nothing on disk (already cleaned up, or never materialized) counts as
    'removed' with no attempt made — a harmless no-op, not an error."""
    result = worktree_cleanup.remove_worktree_on_disk(tmp_path, "does-not-exist")
    assert result["attempted"] is False
    assert result["removed"] is True


def test_remove_worktree_on_disk_falls_back_to_rmtree(tmp_path: Path):
    """Rewritten by gh-cleanup-refuse-dirty-disk-removal. The rmtree fallback
    now runs only after a clean status check. A worktree-shaped directory
    holding an untracked file is refused before any git or rmtree call, and
    the file survives."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    wt_dir = repo_root / ".claude" / "worktrees" / "fake"
    _init_repo_with_commit(wt_dir)
    (wt_dir / "marker.txt").write_text("uncommitted")

    result = worktree_cleanup.remove_worktree_on_disk(repo_root, ".claude/worktrees/fake")

    assert result["attempted"] is False
    assert result["removed"] is False
    assert result["detail"] == "DIRTY_WORKTREE"
    assert (wt_dir / "marker.txt").read_text() == "uncommitted"


def test_remove_worktree_on_disk_reclaims_a_clean_unregistered_directory(tmp_path: Path):
    """A clean directory that git never registered as a worktree is reclaimed
    by the rmtree fallback, which runs only after the clean check passes. A
    repo with no commits is git-clean and holds no read-only object files, so
    the fallback can actually delete it."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    wt_dir = repo_root / ".claude" / "worktrees" / "unregistered"
    wt_dir.mkdir(parents=True)
    _git(wt_dir, "init", "-q")

    result = worktree_cleanup.remove_worktree_on_disk(repo_root, ".claude/worktrees/unregistered")

    assert result["attempted"] is True
    assert result["removed"] is True
    assert not wt_dir.exists()


def test_remove_worktree_on_disk_removes_a_clean_worktree(tmp_path: Path):
    """A clean, git-registered worktree is still removed. The git call no
    longer passes --force, so git's own dirty check stays in force and a
    clean tree is removed by `git worktree remove` itself."""
    repo_root = tmp_path / "repo"
    _init_repo_with_commit(repo_root)
    wt_dir = repo_root / ".claude" / "worktrees" / "clean"
    wt_dir.parent.mkdir(parents=True)
    _git(repo_root, "worktree", "add", "-q", str(wt_dir), "-b", "worktree/clean")

    result = worktree_cleanup.remove_worktree_on_disk(repo_root, ".claude/worktrees/clean")

    assert result == {"attempted": True, "removed": True, "detail": "git worktree remove"}
    assert not wt_dir.exists()


def test_remove_worktree_on_disk_refuses_a_dirty_linked_worktree(tmp_path: Path):
    """A registered worktree with an untracked file is refused before git is
    asked to remove it, so the file survives."""
    repo_root = tmp_path / "repo"
    _init_repo_with_commit(repo_root)
    wt_dir = repo_root / ".claude" / "worktrees" / "dirty"
    wt_dir.parent.mkdir(parents=True)
    _git(repo_root, "worktree", "add", "-q", str(wt_dir), "-b", "worktree/dirty")
    (wt_dir / "scratch.txt").write_text("untracked work")

    result = worktree_cleanup.remove_worktree_on_disk(repo_root, ".claude/worktrees/dirty")

    assert result == {"attempted": False, "removed": False, "detail": "DIRTY_WORKTREE"}
    assert (wt_dir / "scratch.txt").read_text() == "untracked work"


def test_remove_worktree_on_disk_refuses_an_unverifiable_plain_directory(tmp_path: Path):
    """A worktree-shaped directory that is not a git work tree at all cannot
    prove it is clean, so it is refused and its contents survive."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    wt_dir = repo_root / ".claude" / "worktrees" / "loose"
    wt_dir.mkdir(parents=True)
    (wt_dir / "marker.txt").write_text("only copy")

    result = worktree_cleanup.remove_worktree_on_disk(repo_root, ".claude/worktrees/loose")

    assert result == {"attempted": False, "removed": False, "detail": "DIRTY_WORKTREE"}
    assert (wt_dir / "marker.txt").read_text() == "only copy"


def test_remove_worktree_on_disk_refuses_a_plain_dir_inside_an_enclosing_repo(tmp_path: Path):
    """A plain directory nested in another repo is not its own work tree. Its
    contents are git-ignored, so a status run from inside it would report the
    enclosing repo as clean. The toplevel check must refuse it anyway."""
    repo_root = tmp_path / "repo"
    _init_repo_with_commit(repo_root)
    (repo_root / ".git" / "info" / "exclude").write_text(".claude/\n")
    plain = repo_root / ".claude" / "worktrees" / "plain"
    plain.mkdir(parents=True)
    (plain / "marker.txt").write_text("only copy")

    result = worktree_cleanup.remove_worktree_on_disk(repo_root, ".claude/worktrees/plain")

    assert result == {"attempted": False, "removed": False, "detail": "DIRTY_WORKTREE"}
    assert (plain / "marker.txt").read_text() == "only copy"


# ---------------------------------------------------------------------------
# 1b. validate_worktree_cleanup_target / remove_worktree_on_disk_guarded
# (the identity + liveness gate in front of the disk-mutating call)
# ---------------------------------------------------------------------------


def test_validate_worktree_cleanup_target_passes_with_no_recorded_pid():
    row = {"id": "wt1", "path": ".claude/worktrees/x", "pid": None}
    result = worktree_cleanup.validate_worktree_cleanup_target(row, expected_worktree_id="wt1")
    assert result == {"ok": True, "reason": None, "detail": None}


def test_validate_worktree_cleanup_target_rejects_a_missing_row():
    result = worktree_cleanup.validate_worktree_cleanup_target(None, expected_worktree_id="wt1")
    assert result["ok"] is False
    assert result["reason"] == "NOT_FOUND"


def test_validate_worktree_cleanup_target_rejects_id_and_path_mismatch():
    row = {"id": "other", "path": ".claude/worktrees/x"}
    by_id = worktree_cleanup.validate_worktree_cleanup_target(row, expected_worktree_id="wt1")
    assert by_id["reason"] == "ID_MISMATCH"

    row = {"id": "wt1", "path": ".claude/worktrees/x"}
    by_path = worktree_cleanup.validate_worktree_cleanup_target(
        row, expected_worktree_id="wt1", expected_path=".claude/worktrees/y",
    )
    assert by_path["reason"] == "PATH_MISMATCH"


def test_validate_worktree_cleanup_target_refuses_a_live_owner_pid(monkeypatch):
    monkeypatch.setattr(worktree_cleanup, "_pid_is_alive", lambda pid: pid == 4242)
    row = {"id": "wt1", "path": ".claude/worktrees/x", "pid": 4242}
    result = worktree_cleanup.validate_worktree_cleanup_target(row, expected_worktree_id="wt1")
    assert result["ok"] is False
    assert result["reason"] == "PROCESS_STILL_LIVE"


def test_validate_worktree_cleanup_target_ignores_an_unparseable_pid(monkeypatch):
    monkeypatch.setattr(worktree_cleanup, "_pid_is_alive", lambda pid: True)
    row = {"id": "wt1", "path": ".claude/worktrees/x", "pid": "not-a-pid"}
    result = worktree_cleanup.validate_worktree_cleanup_target(row, expected_worktree_id="wt1")
    assert result["ok"] is True


def test_remove_worktree_on_disk_guarded_refuses_a_live_pid_without_touching_disk(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(worktree_cleanup, "_pid_is_alive", lambda pid: True)
    repo_root = tmp_path / "repo"
    wt_dir = repo_root / ".claude" / "worktrees" / "live"
    wt_dir.mkdir(parents=True)
    (wt_dir / "marker.txt").write_text("in use")
    row = {"id": "wt1", "path": ".claude/worktrees/live", "pid": 7}

    result = worktree_cleanup.remove_worktree_on_disk_guarded(
        repo_root, row, expected_worktree_id="wt1",
    )

    assert result["guard_ok"] is False
    assert result["reason"] == "PROCESS_STILL_LIVE"
    assert result["removed"] is False
    assert (wt_dir / "marker.txt").read_text() == "in use"


def test_remove_worktree_on_disk_guarded_passes_through_to_the_dirty_gate(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(worktree_cleanup, "_pid_is_alive", lambda pid: False)
    repo_root = tmp_path / "repo"
    _init_repo_with_commit(repo_root)
    wt_dir = repo_root / ".claude" / "worktrees" / "guarded"
    wt_dir.parent.mkdir(parents=True)
    _git(repo_root, "worktree", "add", "-q", str(wt_dir), "-b", "worktree/guarded")
    (wt_dir / "untracked.txt").write_text("unsaved")
    row = {"id": "wt1", "path": ".claude/worktrees/guarded", "pid": None}

    result = worktree_cleanup.remove_worktree_on_disk_guarded(
        repo_root, row, expected_worktree_id="wt1",
    )

    assert result["guard_ok"] is True
    assert result["attempted"] is False
    assert result["removed"] is False
    assert result["detail"] == "DIRTY_WORKTREE"
    assert (wt_dir / "untracked.txt").read_text() == "unsaved"


def test_is_provably_clean_refuses_when_status_errors(tmp_path: Path, monkeypatch):
    """A git work tree whose status command fails cannot prove it is clean."""
    wt = tmp_path / "wt"
    _init_repo_with_commit(wt)
    real_git_in = worktree_cleanup._git_in

    def status_fails(abs_path, *args):
        if "status" in args:
            return subprocess.CompletedProcess(list(args), 128, "", "fatal: index is corrupt")
        return real_git_in(abs_path, *args)

    monkeypatch.setattr(worktree_cleanup, "_git_in", status_fails)
    assert worktree_cleanup._is_provably_clean(wt) is False


def test_is_provably_clean_refuses_when_git_cannot_run(tmp_path: Path, monkeypatch):
    """If git itself cannot be launched, nothing is provably clean."""
    def git_missing(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(worktree_cleanup, "_git_in", git_missing)
    assert worktree_cleanup._is_provably_clean(tmp_path) is False


def test_resolve_worktree_disk_path_relative_and_absolute(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    rel = worktree_cleanup.resolve_worktree_disk_path(repo_root, ".claude/worktrees/abc")
    assert rel == (repo_root / ".claude" / "worktrees" / "abc").resolve()

    abs_path = tmp_path / "elsewhere"
    resolved = worktree_cleanup.resolve_worktree_disk_path(repo_root, str(abs_path))
    assert resolved == abs_path


# ---------------------------------------------------------------------------
# 1a. looks_like_worktree_path / remove_worktree_on_disk refusal --
# 2026-08-04 incident regression: an ``active_worktrees`` row registered
# with path="meridian" (a plain source-directory name, not a real worktree
# path) must never reach shutil.rmtree just because it exists on disk and
# `git worktree remove` fails on it. This is the exact shape of the bug: a
# path that is a REAL, EXISTING subdirectory of repo_root that happens not
# to be a worktree at all -- distinct from test_remove_worktree_on_disk_
# falls_back_to_rmtree's ".claude/worktrees/fake" case, which IS a worktree
# location by convention even though git never registered it.
# ---------------------------------------------------------------------------


def test_looks_like_worktree_path_accepts_the_documented_conventions(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    assert worktree_cleanup.looks_like_worktree_path(
        repo_root, repo_root / ".claude" / "worktrees" / "abcd1234"
    ) is True
    sibling = tmp_path / "repo-worktree-abcd1234"
    assert worktree_cleanup.looks_like_worktree_path(repo_root, sibling) is True


def test_looks_like_worktree_path_rejects_repo_root_itself(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    assert worktree_cleanup.looks_like_worktree_path(repo_root, repo_root.resolve()) is False


def test_looks_like_worktree_path_rejects_a_real_source_subdirectory(tmp_path: Path):
    """The exact 2026-08-04 shape: a plain child directory name (e.g. a
    package source tree) is neither under .claude/worktrees/ nor a sibling
    matching the {repo}-worktree-... pattern."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    source_dir = repo_root / "meridian"
    source_dir.mkdir()
    assert worktree_cleanup.looks_like_worktree_path(repo_root, source_dir) is False


def test_looks_like_worktree_path_rejects_unrelated_sibling(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    unrelated = tmp_path / "some-other-project"
    assert worktree_cleanup.looks_like_worktree_path(repo_root, unrelated) is False


def test_remove_worktree_on_disk_refuses_a_real_source_subdirectory(tmp_path: Path):
    """End-to-end regression for the 2026-08-04 incident: remove_worktree_on_
    disk must refuse to touch a path shaped like real repo source, even
    though it exists on disk and `git worktree remove` will fail on it (the
    exact condition that previously fell through to a blind rmtree)."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    source_dir = repo_root / "meridian"
    source_dir.mkdir()
    (source_dir / "server.py").write_text("# real source file")

    result = worktree_cleanup.remove_worktree_on_disk(repo_root, "meridian")

    assert result["attempted"] is False
    assert result["removed"] is False
    assert result["detail"] == "NOT_A_WORKTREE_PATH"
    assert source_dir.exists()
    assert (source_dir / "server.py").exists()


# ---------------------------------------------------------------------------
# 2. list_worktrees_pending_cleanup — DB level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_worktrees_pending_cleanup_finds_terminal_item(db):
    p = await db_module.create_project(db, "wt-cleanup-item-test")
    session = await db_module.register_session(db, p["id"], "wt-cleanup-sess")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")

    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/abc12345", ".claude/worktrees/abc12345",
        item_id=item["id"],
    )

    # Not yet eligible — item still pending.
    pending = await db_module.list_worktrees_pending_cleanup(db, p["id"])
    assert wt["id"] not in {w["id"] for w in pending}

    # Mark the item done (merged/integrated) — now it's a real cleanup candidate.
    await db.execute(
        "UPDATE sprint_items SET status = 'done' WHERE id = ?", (item["id"],),
    )
    await db.commit()

    pending_after = await db_module.list_worktrees_pending_cleanup(db, p["id"])
    ids = {w["id"] for w in pending_after}
    assert wt["id"] in ids


@pytest.mark.asyncio
async def test_list_worktrees_pending_cleanup_finds_closed_session(db):
    p = await db_module.create_project(db, "wt-cleanup-session-test")
    session = await db_module.register_session(db, p["id"], "wt-cleanup-sess-2")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/def67890", ".claude/worktrees/def67890",
    )

    pending = await db_module.list_worktrees_pending_cleanup(db, p["id"])
    assert wt["id"] not in {w["id"] for w in pending}

    await db_module.close_session(db, session["id"])

    pending_after = await db_module.list_worktrees_pending_cleanup(db, p["id"])
    assert wt["id"] in {w["id"] for w in pending_after}


@pytest.mark.asyncio
async def test_list_worktrees_pending_cleanup_excludes_already_removed(db):
    p = await db_module.create_project(db, "wt-cleanup-removed-test")
    session = await db_module.register_session(db, p["id"], "wt-cleanup-sess-3")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/ghi11111", ".claude/worktrees/ghi11111",
    )
    await db_module.close_session(db, session["id"])
    await db_module.remove_worktree(db, wt["id"])

    pending = await db_module.list_worktrees_pending_cleanup(db, p["id"])
    assert wt["id"] not in {w["id"] for w in pending}


# ---------------------------------------------------------------------------
# 3. sweep_stale_worktrees — wires DB query + real disk removal together
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_stale_worktrees_reclaims_disk_and_db(db, tmp_path: Path):
    repo_root = tmp_path / "repo"
    _init_repo_with_commit(repo_root)

    p = await db_module.create_project(db, "wt-sweep-test")
    session = await db_module.register_session(db, p["id"], "wt-sweep-sess")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "sweep me")

    # A clean, git-registered worktree: committed content only, no uncommitted files.
    wt_dir = repo_root / ".claude" / "worktrees" / "sweepme"
    wt_dir.parent.mkdir(parents=True)
    _git(repo_root, "worktree", "add", "-q", str(wt_dir), "-b", "worktree/sweepme")

    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/sweepme", ".claude/worktrees/sweepme",
        item_id=item["id"],
    )
    await db.execute(
        "UPDATE sprint_items SET status = 'done' WHERE id = ?", (item["id"],),
    )
    await db.commit()

    result = await worktree_cleanup.sweep_stale_worktrees(db, repo_root, p["id"])

    assert result["swept_count"] == 1
    assert not wt_dir.exists()

    active_after = await db_module.list_active_worktrees(db, p["id"])
    assert active_after == []
    row = await db_module.get_worktree(db, wt["id"])
    assert row["removed_at"] is not None


@pytest.mark.asyncio
async def test_sweep_stale_worktrees_keeps_a_dirty_terminal_worktree(db, tmp_path: Path):
    """A terminal worktree holding an untracked file is reported under skipped
    with reason DIRTY_WORKTREE. Its directory and file survive, and its DB row
    stays active so a later sweep can retry it."""
    repo_root = tmp_path / "repo"
    _init_repo_with_commit(repo_root)

    p = await db_module.create_project(db, "wt-sweep-dirty-test")
    session = await db_module.register_session(db, p["id"], "wt-sweep-dirty-sess")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "dirty sweep")

    wt_dir = repo_root / ".claude" / "worktrees" / "dirtysweep"
    wt_dir.parent.mkdir(parents=True)
    _git(repo_root, "worktree", "add", "-q", str(wt_dir), "-b", "worktree/dirtysweep")
    (wt_dir / "untracked_output.txt").write_text("unsaved work")

    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/dirtysweep", ".claude/worktrees/dirtysweep",
        item_id=item["id"],
    )
    await db.execute(
        "UPDATE sprint_items SET status = 'done' WHERE id = ?", (item["id"],),
    )
    await db.commit()

    result = await worktree_cleanup.sweep_stale_worktrees(db, repo_root, p["id"])

    assert result["swept_count"] == 0
    assert result["skipped_count"] == 1
    assert result["skipped"][0]["id"] == wt["id"]
    assert result["skipped"][0]["reason"] == "DIRTY_WORKTREE"
    assert wt_dir.exists()
    assert (wt_dir / "untracked_output.txt").read_text() == "unsaved work"

    active_after = await db_module.list_active_worktrees(db, p["id"])
    assert [w["id"] for w in active_after] == [wt["id"]]


@pytest.mark.asyncio
async def test_sweep_stale_worktrees_is_noop_when_nothing_eligible(db, tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    p = await db_module.create_project(db, "wt-sweep-noop-test")
    session = await db_module.register_session(db, p["id"], "wt-sweep-noop-sess")
    await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/stillbusy", ".claude/worktrees/stillbusy",
    )

    result = await worktree_cleanup.sweep_stale_worktrees(db, repo_root, p["id"])
    assert result["swept_count"] == 0

    active_after = await db_module.list_active_worktrees(db, p["id"])
    assert len(active_after) == 1


# ---------------------------------------------------------------------------
# 4. DELETE /projects/{id}/worktrees/{id} — real disk removal wired in
# ---------------------------------------------------------------------------


def test_delete_worktree_removes_from_disk_when_self_hosted(client, monkeypatch, tmp_path: Path):
    import meridian.server as srv
    import meridian.worktree_cleanup as wc_module

    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)

    fake_repo_root = tmp_path / "repo"
    fake_repo_root.mkdir()
    monkeypatch.setattr(srv, "_REPO_ROOT", fake_repo_root)

    calls = []

    def fake_remove(repo_root, wt_path):
        calls.append((repo_root, wt_path))
        return {"attempted": True, "removed": True, "detail": "faked"}

    # delete_worktree does `from .. import worktree_cleanup` lazily inside the
    # handler, which binds to this same module object already in sys.modules
    # — patching the attribute here is visible to that call.
    monkeypatch.setattr(wc_module, "remove_worktree_on_disk", fake_remove)

    proj = client.post("/projects", json={"name": "wt-delete-disk-test"}).json()
    pid = proj["id"]
    sess = client.post(f"/projects/{pid}/start-session", json={"session_name": "wt-del-sess"}).json()
    sid = sess["session_id"]

    r2 = client.post(f"/projects/{pid}/worktrees", json={
        "session_id": sid,
        "branch": "worktree/disktest",
        "path": ".claude/worktrees/disktest",
    })
    assert r2.status_code == 201
    wt = r2.json()

    r3 = client.delete(f"/projects/{pid}/worktrees/{wt['id']}")
    assert r3.status_code == 204

    assert len(calls) == 1
    assert calls[0][0] == fake_repo_root
    assert calls[0][1] == ".claude/worktrees/disktest"


def _hosted_admin_login(client, monkeypatch) -> None:
    """ece2ac0a — hosted mode no longer serves anonymous callers from the
    shared control-plane DB (``_db()`` now 401s), so a hosted-mode test must
    authenticate. An ``admin``-plan tenant with no dedicated DB resolves to
    that same in-memory control-plane DB, which keeps these tests hermetic.
    """
    import asyncio

    from meridian import _deps
    from meridian.hosted import _make_session_cookie

    db = client.app.state.db
    tenant = asyncio.run(db_module.upsert_tenant(db, "wt-hosted-admin@example.com"))
    asyncio.run(db_module.update_tenant(db, tenant["id"], plan="admin"))
    session = asyncio.run(
        db_module.create_user_session(db, tenant["id"], "2099-01-01 00:00:00")
    )
    monkeypatch.setitem(_deps._tenant_db_cache, tenant["id"], db)
    client.cookies.set("meridian_session", _make_session_cookie(session["id"]))


def test_delete_worktree_skips_disk_removal_when_hosted(client, monkeypatch):
    import meridian.worktree_cleanup as wc_module

    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    _hosted_admin_login(client, monkeypatch)

    calls = []
    monkeypatch.setattr(
        wc_module, "remove_worktree_on_disk",
        lambda *a, **k: calls.append((a, k)) or {"attempted": True, "removed": True, "detail": "x"},
    )

    proj = client.post("/projects", json={"name": "wt-delete-hosted-test"}).json()
    pid = proj["id"]
    sess = client.post(f"/projects/{pid}/start-session", json={"session_name": "wt-del-hosted-sess"}).json()
    sid = sess["session_id"]

    r2 = client.post(f"/projects/{pid}/worktrees", json={
        "session_id": sid,
        "branch": "worktree/hostedtest",
        "path": ".claude/worktrees/hostedtest",
    })
    wt = r2.json()

    r3 = client.delete(f"/projects/{pid}/worktrees/{wt['id']}")
    assert r3.status_code == 204
    assert calls == []  # never touched a filesystem path on hosted mode

    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)


# ---------------------------------------------------------------------------
# 5. POST /projects/{id}/worktrees/sweep — on-demand trigger
# ---------------------------------------------------------------------------


def test_sweep_endpoint_reclaims_terminal_worktrees(client, monkeypatch, tmp_path: Path):
    import meridian.server as srv

    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    fake_repo_root = tmp_path / "repo"
    _init_repo_with_commit(fake_repo_root)
    monkeypatch.setattr(srv, "_REPO_ROOT", fake_repo_root)

    proj = client.post("/projects", json={"name": "wt-sweep-endpoint-test"}).json()
    pid = proj["id"]
    sess = client.post(f"/projects/{pid}/start-session", json={"session_name": "wt-sweep-ep-sess"}).json()
    sid = sess["session_id"]

    wt_dir = fake_repo_root / ".claude" / "worktrees" / "sweependpoint"
    wt_dir.parent.mkdir(parents=True)
    _git(fake_repo_root, "worktree", "add", "-q", str(wt_dir), "-b", "worktree/sweependpoint")

    r2 = client.post(f"/projects/{pid}/worktrees", json={
        "session_id": sid,
        "branch": "worktree/sweependpoint",
        "path": ".claude/worktrees/sweependpoint",
    })
    wt = r2.json()

    # Close the session so the worktree becomes a cleanup candidate without
    # ever calling DELETE (simulating a session that ended without cleanup).
    close_r = client.post(f"/sessions/{sid}/close")
    assert close_r.status_code == 200

    r3 = client.post(f"/projects/{pid}/worktrees/sweep")
    assert r3.status_code == 200
    body = r3.json()
    assert body["hosted"] is False
    assert body["swept_count"] == 1
    assert not wt_dir.exists()

    active_after = client.get(f"/projects/{pid}/worktrees").json()
    assert active_after == []


def test_sweep_endpoint_noop_when_hosted(client, monkeypatch):
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    _hosted_admin_login(client, monkeypatch)
    proj = client.post("/projects", json={"name": "wt-sweep-hosted-test"}).json()
    pid = proj["id"]
    r = client.post(f"/projects/{pid}/worktrees/sweep")
    assert r.status_code == 200
    assert r.json() == {"swept_count": 0, "skipped_count": 0, "hosted": True}
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)


def test_sweep_endpoint_404_unknown_project(client):
    r = client.post("/projects/does-not-exist/worktrees/sweep")
    assert r.status_code == 404
