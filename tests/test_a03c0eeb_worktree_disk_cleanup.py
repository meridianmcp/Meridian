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

from pathlib import Path

import pytest

from meridian import db as db_module
from meridian import worktree_cleanup


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
    """A directory that exists but isn't a real `git worktree` registration
    (git worktree remove will fail on it) must still be reclaimed via the
    rmtree fallback -- never leaves the caller with a leaked directory just
    because it wasn't a proper worktree entry."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    wt_dir = repo_root / ".claude" / "worktrees" / "fake"
    wt_dir.mkdir(parents=True)
    (wt_dir / "marker.txt").write_text("leftover")

    result = worktree_cleanup.remove_worktree_on_disk(repo_root, ".claude/worktrees/fake")

    assert result["attempted"] is True
    assert result["removed"] is True
    assert not wt_dir.exists()


def test_resolve_worktree_disk_path_relative_and_absolute(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    rel = worktree_cleanup.resolve_worktree_disk_path(repo_root, ".claude/worktrees/abc")
    assert rel == (repo_root / ".claude" / "worktrees" / "abc").resolve()

    abs_path = tmp_path / "elsewhere"
    resolved = worktree_cleanup.resolve_worktree_disk_path(repo_root, str(abs_path))
    assert resolved == abs_path


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
    repo_root.mkdir()

    p = await db_module.create_project(db, "wt-sweep-test")
    session = await db_module.register_session(db, p["id"], "wt-sweep-sess")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "sweep me")

    wt_dir = repo_root / ".claude" / "worktrees" / "sweepme"
    wt_dir.mkdir(parents=True)
    (wt_dir / "leftover.txt").write_text("stale")

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


def test_delete_worktree_skips_disk_removal_when_hosted(client, monkeypatch):
    import meridian.worktree_cleanup as wc_module

    monkeypatch.setenv("MERIDIAN_HOSTED", "1")

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
    fake_repo_root.mkdir()
    monkeypatch.setattr(srv, "_REPO_ROOT", fake_repo_root)

    proj = client.post("/projects", json={"name": "wt-sweep-endpoint-test"}).json()
    pid = proj["id"]
    sess = client.post(f"/projects/{pid}/start-session", json={"session_name": "wt-sweep-ep-sess"}).json()
    sid = sess["session_id"]

    wt_dir = fake_repo_root / ".claude" / "worktrees" / "sweependpoint"
    wt_dir.mkdir(parents=True)

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
    proj = client.post("/projects", json={"name": "wt-sweep-hosted-test"}).json()
    pid = proj["id"]
    r = client.post(f"/projects/{pid}/worktrees/sweep")
    assert r.status_code == 200
    assert r.json() == {"swept_count": 0, "skipped_count": 0, "hosted": True}
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)


def test_sweep_endpoint_404_unknown_project(client):
    r = client.post("/projects/does-not-exist/worktrees/sweep")
    assert r.status_code == 404
