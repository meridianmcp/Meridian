"""15610335 -- keep Pixi environments and generated caches outside git
worktrees; worktree storage retention and cleanup.

Covers:
1. `pixi_env_retention.classify_worktree_entry` / `plan_worktree_cache_cleanup`
   -- cleanup planning (dry-run, never mutates).
2. `pixi_env_retention.execute_worktree_cache_cleanup` -- active lease
   protection (live PID refuses), path containment (never deletes outside
   worktree_root), idempotent removal.
3. `pixi_env_retention.ensure_detached_environments_configured` /
   `resolve_pixi_env_prefix` -- injected-runner subprocess wrappers.
4. `pixi_env_retention.write_worktree_env_marker` / `discover_stale_external_envs`
   / `find_external_envs_for_dead_worktrees` -- stale external-environment
   discovery.
5. `pixi_env_retention.reclaim_external_env` -- idempotent, path-contained
   removal.
6. `meridian.db.worktrees` -- register/get/list/mark-reclaimed registry.
7. `orphan_reaper.reclaim_stale_pixi_envs` -- wires (4)+(5) using the same
   dead_paths orphan_reaper already fetches for process reaping.
8. `tunnel_client._resolve_stable_pixi_cache_env` -- PIXI_CACHE_DIR
   resolution, merged into the central `_spawn_with_cache_retry` choke point.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from meridian import db as db_module
from meridian import orphan_reaper
from meridian import pixi_env_retention as per


# ---------------------------------------------------------------------------
# 1. classify_worktree_entry / plan_worktree_cache_cleanup
# ---------------------------------------------------------------------------


def test_classify_worktree_entry_generated_cache_names(tmp_path: Path):
    for name in (".pixi", "node_modules", "__pycache__", ".pytest_cache"):
        d = tmp_path / name
        d.mkdir()
        (d / "junk.bin").write_bytes(b"x" * 100)
        assert per.classify_worktree_entry(d) == per.CLASSIFICATION_GENERATED_CACHE


def test_classify_worktree_entry_empty_dir_is_empty_stub(tmp_path: Path):
    d = tmp_path / "empty_leftover"
    d.mkdir()
    assert per.classify_worktree_entry(d) == per.CLASSIFICATION_EMPTY_STUB


def test_classify_worktree_entry_real_content_is_source(tmp_path: Path):
    d = tmp_path / "meridian"
    d.mkdir()
    (d / "server.py").write_text("# real source")
    assert per.classify_worktree_entry(d) == per.CLASSIFICATION_SOURCE


def test_classify_worktree_entry_plain_file_is_source(tmp_path: Path):
    f = tmp_path / "README.md"
    f.write_text("real file")
    assert per.classify_worktree_entry(f) == per.CLASSIFICATION_SOURCE


def test_plan_worktree_cache_cleanup_classifies_all_children(tmp_path: Path):
    root = tmp_path / "worktree"
    root.mkdir()
    (root / ".pixi").mkdir()
    (root / ".pixi" / "envs").mkdir()
    (root / ".pixi" / "envs" / "big.bin").write_bytes(b"x" * 4096)
    (root / "node_modules").mkdir()
    (root / "empty_stub").mkdir()
    src = root / "meridian"
    src.mkdir()
    (src / "server.py").write_text("real")

    plan = per.plan_worktree_cache_cleanup(root)
    by_name = {e["name"]: e for e in plan["entries"]}

    assert by_name[".pixi"]["classification"] == per.CLASSIFICATION_GENERATED_CACHE
    assert by_name[".pixi"]["size_bytes"] == 4096
    assert by_name["node_modules"]["classification"] == per.CLASSIFICATION_GENERATED_CACHE
    assert by_name["empty_stub"]["classification"] == per.CLASSIFICATION_EMPTY_STUB
    assert by_name["meridian"]["classification"] == per.CLASSIFICATION_SOURCE
    # reclaimable_bytes only counts generated_cache + empty_stub, never source.
    assert plan["reclaimable_bytes"] == 4096


def test_plan_worktree_cache_cleanup_missing_root_is_empty_plan(tmp_path: Path):
    plan = per.plan_worktree_cache_cleanup(tmp_path / "does-not-exist")
    assert plan["entries"] == []
    assert plan["reclaimable_bytes"] == 0


def test_plan_worktree_cache_cleanup_never_deletes_anything(tmp_path: Path):
    root = tmp_path / "worktree"
    root.mkdir()
    (root / ".pixi").mkdir()
    per.plan_worktree_cache_cleanup(root)
    assert (root / ".pixi").exists()


# ---------------------------------------------------------------------------
# 2. execute_worktree_cache_cleanup -- lease protection, containment, idempotent
# ---------------------------------------------------------------------------


def _plan_with_pixi_dir(root: Path) -> dict:
    (root / ".pixi").mkdir()
    (root / ".pixi" / "f.bin").write_bytes(b"x" * 10)
    src = root / "meridian"
    src.mkdir()
    (src / "server.py").write_text("real")
    return per.plan_worktree_cache_cleanup(root)


def test_execute_cleanup_requires_confirm(tmp_path: Path):
    root = tmp_path / "worktree"
    root.mkdir()
    plan = _plan_with_pixi_dir(root)

    result = per.execute_worktree_cache_cleanup(plan, confirm=False)
    assert result["executed"] is False
    assert result["reason"] == "CONFIRM_REQUIRED"
    assert (root / ".pixi").exists()  # never touched


def test_execute_cleanup_refuses_when_owner_pid_alive(tmp_path: Path):
    root = tmp_path / "worktree"
    root.mkdir()
    plan = _plan_with_pixi_dir(root)

    result = per.execute_worktree_cache_cleanup(plan, confirm=True, owner_pid=os.getpid())
    assert result["executed"] is False
    assert result["reason"] == "PROCESS_STILL_LIVE"
    assert (root / ".pixi").exists()


def test_execute_cleanup_proceeds_when_owner_pid_dead(tmp_path: Path):
    root = tmp_path / "worktree"
    root.mkdir()
    plan = _plan_with_pixi_dir(root)

    # A PID vanishingly unlikely to be alive on any real machine.
    result = per.execute_worktree_cache_cleanup(plan, confirm=True, owner_pid=999_999_999)
    assert result["executed"] is True
    assert not (root / ".pixi").exists()
    assert (root / "meridian").exists()  # source untouched


def test_execute_cleanup_removes_generated_cache_never_source(tmp_path: Path):
    root = tmp_path / "worktree"
    root.mkdir()
    plan = _plan_with_pixi_dir(root)

    result = per.execute_worktree_cache_cleanup(plan, confirm=True)
    assert result["executed"] is True
    assert not (root / ".pixi").exists()
    assert (root / "meridian" / "server.py").exists()
    removed_names = {e["name"] for e in result["removed"]}
    assert removed_names == {".pixi"}
    assert result["removed_bytes"] == 10


def test_execute_cleanup_path_containment_rejects_escaped_entry(tmp_path: Path):
    root = tmp_path / "worktree"
    root.mkdir()
    outside = tmp_path / "outside_target"
    outside.mkdir()
    (outside / "sensitive.txt").write_text("do not delete me")

    # A crafted/corrupted plan entry pointing OUTSIDE worktree_root.
    plan = {
        "worktree_root": str(root),
        "entries": [
            {
                "name": "outside_target",
                "path": str(outside),
                "classification": per.CLASSIFICATION_GENERATED_CACHE,
                "size_bytes": 0,
            },
        ],
    }
    result = per.execute_worktree_cache_cleanup(plan, confirm=True)
    assert result["executed"] is True
    assert result["removed"] == []
    assert result["skipped"][0]["reason"] == "PATH_CONTAINMENT_FAILED"
    assert outside.exists()
    assert (outside / "sensitive.txt").exists()


def test_execute_cleanup_never_removes_source_classification(tmp_path: Path):
    root = tmp_path / "worktree"
    root.mkdir()
    src = root / "meridian"
    src.mkdir()
    (src / "server.py").write_text("real")
    plan = per.plan_worktree_cache_cleanup(root)

    result = per.execute_worktree_cache_cleanup(plan, confirm=True)
    assert result["removed"] == []
    assert result["skipped"][0]["reason"] == "not_a_cleanup_target"
    assert (src / "server.py").exists()


def test_execute_cleanup_missing_path_is_idempotent_success(tmp_path: Path):
    root = tmp_path / "worktree"
    root.mkdir()
    plan = {
        "worktree_root": str(root),
        "entries": [
            {
                "name": ".pixi",
                "path": str(root / ".pixi"),
                "classification": per.CLASSIFICATION_GENERATED_CACHE,
                "size_bytes": 0,
            },
        ],
    }
    result = per.execute_worktree_cache_cleanup(plan, confirm=True)
    assert result["removed"][0]["detail"] == "already absent"

    # Calling it again is still a clean success -- idempotent.
    result2 = per.execute_worktree_cache_cleanup(plan, confirm=True)
    assert result2["removed"][0]["detail"] == "already absent"


# ---------------------------------------------------------------------------
# 3. ensure_detached_environments_configured / resolve_pixi_env_prefix
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_ensure_detached_environments_configured_success(tmp_path: Path):
    calls = []

    def fake_runner(cmd):
        calls.append(cmd)
        return _FakeCompletedProcess(returncode=0, stdout="ok")

    result = per.ensure_detached_environments_configured(str(tmp_path / "envs"), runner=fake_runner)
    assert result["configured"] is True
    assert calls[0] == [
        "pixi", "config", "set", "--global", "detached-environments", str(tmp_path / "envs"),
    ]


def test_ensure_detached_environments_configured_true_uses_literal_true():
    calls = []

    def fake_runner(cmd):
        calls.append(cmd)
        return _FakeCompletedProcess(returncode=0)

    per.ensure_detached_environments_configured(True, runner=fake_runner)
    assert calls[0][-1] == "true"


def test_ensure_detached_environments_configured_defaults_to_home_path():
    calls = []

    def fake_runner(cmd):
        calls.append(cmd)
        return _FakeCompletedProcess(returncode=0)

    per.ensure_detached_environments_configured(runner=fake_runner)
    assert calls[0][-1] == str(per.default_detached_environments_root())


def test_ensure_detached_environments_configured_failure_never_raises():
    def fake_runner(cmd):
        raise FileNotFoundError("pixi not on PATH")

    result = per.ensure_detached_environments_configured("x", runner=fake_runner)
    assert result["configured"] is False
    assert "pixi not on PATH" in result["detail"]


def test_ensure_detached_environments_configured_nonzero_exit_reported():
    def fake_runner(cmd):
        return _FakeCompletedProcess(returncode=1, stderr="boom")

    result = per.ensure_detached_environments_configured("x", runner=fake_runner)
    assert result["configured"] is False
    assert "boom" in result["detail"]


def test_resolve_pixi_env_prefix_parses_environments_info(tmp_path: Path):
    payload = (
        '{"environments_info": [{"name": "default", "prefix": "C:/envs/meridian-123/envs/default"}]}'
    )

    def fake_runner(cmd, cwd):
        return _FakeCompletedProcess(returncode=0, stdout=payload)

    prefix = per.resolve_pixi_env_prefix(tmp_path, runner=fake_runner)
    assert prefix == "C:/envs/meridian-123/envs/default"


def test_resolve_pixi_env_prefix_env_not_found_returns_none(tmp_path: Path):
    payload = '{"environments_info": [{"name": "other", "prefix": "C:/x"}]}'

    def fake_runner(cmd, cwd):
        return _FakeCompletedProcess(returncode=0, stdout=payload)

    assert per.resolve_pixi_env_prefix(tmp_path, runner=fake_runner) is None


def test_resolve_pixi_env_prefix_nonzero_exit_returns_none(tmp_path: Path):
    def fake_runner(cmd, cwd):
        return _FakeCompletedProcess(returncode=1, stderr="no manifest found")

    assert per.resolve_pixi_env_prefix(tmp_path, runner=fake_runner) is None


def test_resolve_pixi_env_prefix_malformed_json_returns_none(tmp_path: Path):
    def fake_runner(cmd, cwd):
        return _FakeCompletedProcess(returncode=0, stdout="not json")

    assert per.resolve_pixi_env_prefix(tmp_path, runner=fake_runner) is None


def test_resolve_pixi_env_prefix_runner_exception_returns_none(tmp_path: Path):
    def fake_runner(cmd, cwd):
        raise TimeoutError("pixi hung")

    assert per.resolve_pixi_env_prefix(tmp_path, runner=fake_runner) is None


# ---------------------------------------------------------------------------
# 4. marker + discovery -- stale external-environment discovery
# ---------------------------------------------------------------------------


def test_write_and_read_worktree_env_marker(tmp_path: Path):
    env_root = tmp_path / "meridian-abc123"
    per.write_worktree_env_marker(env_root, r"C:\repo\.claude\worktrees\abc123")
    assert per.read_worktree_env_marker(env_root) == r"C:\repo\.claude\worktrees\abc123"


def test_read_worktree_env_marker_missing_returns_none(tmp_path: Path):
    assert per.read_worktree_env_marker(tmp_path / "no-marker-here") is None


def test_discover_stale_external_envs_finds_dead_and_skips_active(tmp_path: Path):
    external_root = tmp_path / "workspace-envs"
    dead_env = external_root / "meridian-dead111"
    active_env = external_root / "meridian-active222"
    per.write_worktree_env_marker(dead_env, r"C:\repo\.claude\worktrees\dead111")
    per.write_worktree_env_marker(active_env, r"C:\repo\.claude\worktrees\active222")

    stale = per.discover_stale_external_envs(
        external_root, active_worktree_paths={r"C:\repo\.claude\worktrees\active222"},
    )
    assert len(stale) == 1
    assert stale[0]["worktree_path"] == r"C:\repo\.claude\worktrees\dead111"


def test_discover_stale_external_envs_ignores_unmarked_dirs(tmp_path: Path):
    external_root = tmp_path / "workspace-envs"
    unmarked = external_root / "mystery-dir"
    unmarked.mkdir(parents=True)

    stale = per.discover_stale_external_envs(external_root, active_worktree_paths=set())
    assert stale == []  # unknown provenance -- never auto-reclaimed


def test_discover_stale_external_envs_missing_root_returns_empty(tmp_path: Path):
    assert per.discover_stale_external_envs(tmp_path / "nope", set()) == []


def test_find_external_envs_for_dead_worktrees_exact_match(tmp_path: Path):
    external_root = tmp_path / "workspace-envs"
    dead_path = r"C:\repo\.claude\worktrees\dead999"
    env_dir = external_root / "meridian-dead999"
    per.write_worktree_env_marker(env_dir, dead_path)

    found = per.find_external_envs_for_dead_worktrees(external_root, [dead_path])
    assert len(found) == 1
    assert found[0]["path"] == str(env_dir)


def test_find_external_envs_for_dead_worktrees_no_dead_paths_returns_empty(tmp_path: Path):
    external_root = tmp_path / "workspace-envs"
    per.write_worktree_env_marker(external_root / "x", "some/path")
    assert per.find_external_envs_for_dead_worktrees(external_root, []) == []


# ---------------------------------------------------------------------------
# 5. reclaim_external_env -- path containment + idempotent removal
# ---------------------------------------------------------------------------


def test_reclaim_external_env_dry_run_never_deletes(tmp_path: Path):
    external_root = tmp_path / "workspace-envs"
    env_dir = external_root / "meridian-x"
    env_dir.mkdir(parents=True)
    (env_dir / "envs.bin").write_bytes(b"x")

    result = per.reclaim_external_env(env_dir, external_root, confirm=False)
    assert result["removed"] is False
    assert env_dir.exists()


def test_reclaim_external_env_removes_when_confirmed(tmp_path: Path):
    external_root = tmp_path / "workspace-envs"
    env_dir = external_root / "meridian-x"
    env_dir.mkdir(parents=True)
    (env_dir / "envs.bin").write_bytes(b"x")

    result = per.reclaim_external_env(env_dir, external_root, confirm=True)
    assert result["removed"] is True
    assert not env_dir.exists()


def test_reclaim_external_env_idempotent_second_call(tmp_path: Path):
    external_root = tmp_path / "workspace-envs"
    env_dir = external_root / "meridian-x"
    env_dir.mkdir(parents=True)

    first = per.reclaim_external_env(env_dir, external_root, confirm=True)
    second = per.reclaim_external_env(env_dir, external_root, confirm=True)
    assert first["removed"] is True
    assert second["removed"] is True
    assert second["detail"] == "already absent"


def test_reclaim_external_env_refuses_outside_external_root(tmp_path: Path):
    external_root = tmp_path / "workspace-envs"
    external_root.mkdir()
    outside = tmp_path / "not_under_root"
    outside.mkdir()
    (outside / "important.txt").write_text("keep me")

    result = per.reclaim_external_env(outside, external_root, confirm=True)
    assert result["removed"] is False
    assert "not contained" in result["detail"]
    assert outside.exists()
    assert (outside / "important.txt").exists()


# ---------------------------------------------------------------------------
# 6. meridian.db.worktrees -- registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_and_get_pixi_env_root(db):
    p = await db_module.create_project(db, "pixi-env-root-test")
    session = await db_module.register_session(db, p["id"], "pixi-env-sess")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/pixi1", ".claude/worktrees/pixi1",
    )

    row = await db_module.register_pixi_env_root(
        db, wt["id"], p["id"], "C:/Users/x/.pixi/workspace-envs/meridian-999/envs/default",
    )
    assert row["worktree_id"] == wt["id"]
    assert row["reclaimed_at"] is None

    fetched = await db_module.get_pixi_env_root_for_worktree(db, wt["id"])
    assert fetched["id"] == row["id"]


@pytest.mark.asyncio
async def test_get_pixi_env_root_for_worktree_none_when_never_registered(db):
    p = await db_module.create_project(db, "pixi-env-root-none-test")
    session = await db_module.register_session(db, p["id"], "pixi-env-sess-2")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/pixi2", ".claude/worktrees/pixi2",
    )
    assert await db_module.get_pixi_env_root_for_worktree(db, wt["id"]) is None


@pytest.mark.asyncio
async def test_list_unreclaimed_pixi_env_roots_only_for_removed_worktrees(db):
    p = await db_module.create_project(db, "pixi-env-unreclaimed-test")
    session = await db_module.register_session(db, p["id"], "pixi-env-sess-3")

    wt_active = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/pixiA", ".claude/worktrees/pixiA",
    )
    wt_removed = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/pixiB", ".claude/worktrees/pixiB",
    )
    await db_module.register_pixi_env_root(db, wt_active["id"], p["id"], "C:/envs/A")
    root_b = await db_module.register_pixi_env_root(db, wt_removed["id"], p["id"], "C:/envs/B")

    # Neither worktree removed yet -- nothing is a reclaim candidate.
    pending = await db_module.list_unreclaimed_pixi_env_roots(db, p["id"])
    assert pending == []

    await db_module.remove_worktree(db, wt_removed["id"])

    pending_after = await db_module.list_unreclaimed_pixi_env_roots(db, p["id"])
    ids = {r["id"] for r in pending_after}
    assert root_b["id"] in ids
    assert len(pending_after) == 1


@pytest.mark.asyncio
async def test_mark_pixi_env_root_reclaimed_is_idempotent(db):
    p = await db_module.create_project(db, "pixi-env-reclaim-test")
    session = await db_module.register_session(db, p["id"], "pixi-env-sess-4")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/pixiC", ".claude/worktrees/pixiC",
    )
    row = await db_module.register_pixi_env_root(db, wt["id"], p["id"], "C:/envs/C")

    first = await db_module.mark_pixi_env_root_reclaimed(db, row["id"])
    second = await db_module.mark_pixi_env_root_reclaimed(db, row["id"])
    assert first is True
    assert second is False  # already reclaimed -- no-op, not an error

    assert await db_module.get_pixi_env_root_for_worktree(db, wt["id"]) is None


# ---------------------------------------------------------------------------
# 7. orphan_reaper.reclaim_stale_pixi_envs
# ---------------------------------------------------------------------------

_DEAD_PATH = r"C:\repo\.claude\worktrees\deadsess"


def test_reclaim_stale_pixi_envs_removes_matching_env(tmp_path: Path):
    external_root = tmp_path / "workspace-envs"
    env_dir = external_root / "meridian-deadsess"
    per.write_worktree_env_marker(env_dir, _DEAD_PATH)

    result = orphan_reaper.reclaim_stale_pixi_envs([_DEAD_PATH], pixi_env_root=str(external_root))
    assert result["reclaimed_count"] == 1
    assert not env_dir.exists()


def test_reclaim_stale_pixi_envs_dry_run_never_deletes(tmp_path: Path):
    external_root = tmp_path / "workspace-envs"
    env_dir = external_root / "meridian-deadsess"
    per.write_worktree_env_marker(env_dir, _DEAD_PATH)

    result = orphan_reaper.reclaim_stale_pixi_envs(
        [_DEAD_PATH], pixi_env_root=str(external_root), dry_run=True,
    )
    assert result["reclaimed_count"] == 0
    assert result["skipped_count"] == 1
    assert env_dir.exists()


def test_reclaim_stale_pixi_envs_no_candidates_is_noop(tmp_path: Path):
    external_root = tmp_path / "workspace-envs"
    result = orphan_reaper.reclaim_stale_pixi_envs([_DEAD_PATH], pixi_env_root=str(external_root))
    assert result == {
        "candidates_count": 0, "reclaimed_count": 0, "skipped_count": 0,
        "reclaimed": [], "skipped": [],
    }


def test_reclaim_stale_pixi_envs_never_touches_unrelated_env(tmp_path: Path):
    external_root = tmp_path / "workspace-envs"
    unrelated = external_root / "meridian-stillalive"
    per.write_worktree_env_marker(unrelated, r"C:\repo\.claude\worktrees\stillalive")

    orphan_reaper.reclaim_stale_pixi_envs([_DEAD_PATH], pixi_env_root=str(external_root))
    assert unrelated.exists()


def test_main_also_reclaims_pixi_envs(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        orphan_reaper, "fetch_dead_worktree_paths", lambda *a, **k: [_DEAD_PATH]
    )
    monkeypatch.setattr(
        orphan_reaper, "reap_orphans",
        lambda *a, **k: {"killed_count": 0, "skipped_count": 0, "candidates_count": 0, "killed": [], "skipped": []},
    )
    called = {}

    def fake_reclaim(dead_paths, pixi_env_root=None, dry_run=False):
        called["dead_paths"] = dead_paths
        called["dry_run"] = dry_run
        return {"candidates_count": 0, "reclaimed_count": 0, "skipped_count": 0, "reclaimed": [], "skipped": []}

    monkeypatch.setattr(orphan_reaper, "reclaim_stale_pixi_envs", fake_reclaim)
    rc = orphan_reaper.main(["--project-id", "proj1", "--url", "http://x"])
    assert rc == 0
    assert called["dead_paths"] == [_DEAD_PATH]
    assert called["dry_run"] is False


# ---------------------------------------------------------------------------
# 8. tunnel_client PIXI_CACHE_DIR resolution
# ---------------------------------------------------------------------------


def test_resolve_stable_pixi_cache_env_sets_pixi_cache_dir(monkeypatch, tmp_path: Path):
    import meridian.tunnel_client as tc

    monkeypatch.setattr(tc, "_stable_pixi_cache_env_cache", None)
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))

    result = tc._resolve_stable_pixi_cache_env()
    assert "PIXI_CACHE_DIR" in result
    assert result["PIXI_CACHE_DIR"].startswith(str(tmp_path))


def test_resolve_stable_pixi_cache_env_is_memoized(monkeypatch, tmp_path: Path):
    import meridian.tunnel_client as tc

    monkeypatch.setattr(tc, "_stable_pixi_cache_env_cache", None)
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))

    first = tc._resolve_stable_pixi_cache_env()
    # Even if home() would resolve differently now, the cached value wins.
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path / "different"))
    second = tc._resolve_stable_pixi_cache_env()
    assert first == second


def test_resolve_stable_pixi_cache_env_failure_returns_empty(monkeypatch):
    import meridian.tunnel_client as tc

    monkeypatch.setattr(tc, "_stable_pixi_cache_env_cache", None)

    def _boom():
        raise OSError("no home dir")

    monkeypatch.setattr(tc.Path, "home", staticmethod(_boom))
    assert tc._resolve_stable_pixi_cache_env() == {}


def test_spawn_with_cache_retry_merges_pixi_cache_dir(monkeypatch, tmp_path: Path):
    import meridian.tunnel_client as tc

    monkeypatch.setattr(tc, "_stable_cache_env_cache", {})
    monkeypatch.setattr(tc, "_stable_pixi_cache_env_cache", {"PIXI_CACHE_DIR": str(tmp_path / "pixicache")})
    monkeypatch.setattr(tc, "_scoped_cache_clear", lambda cmd, label="": False)

    captured = {}

    class _FakeProc:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(cmd, env=None, **kwargs):
        captured["env"] = env
        return _FakeProc()

    monkeypatch.setattr(tc.subprocess, "Popen", fake_popen)

    tc._spawn_with_cache_retry(["pixi", "run", "python"], None, "test")
    assert captured["env"]["PIXI_CACHE_DIR"] == str(tmp_path / "pixicache")
