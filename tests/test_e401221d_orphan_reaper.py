"""e401221d -- Meridian-side mitigation for Claude Code's dangling-process bug.

Covers:
1. ``process_belongs_to_dead_worktree`` -- pure path/cmdline matching rule.
2. ``list_orphan_candidates`` / ``reap_orphans`` -- process-listing and
   kill are injected (mocked), never touch real OS processes.
3. ``fetch_dead_worktree_paths`` -- HTTP fetch, mocked (no real network).
4. ``seed_orphan_reaper_hook`` -- registers via the 273287cb custom_hooks
   infra (``db.add_custom_hook``/``update_custom_hook``), idempotent, never
   writes anything into sprint_guard.{sh,ps1}.
5. ``GET /projects/{id}/worktrees/pending_cleanup`` -- the read-only route
   the hook's HTTP fetch hits, wired to ``db.list_worktrees_pending_cleanup``
   (a03c0eeb).
6. ``main()`` -- CLI entry point invoked by the registered hook scripts;
   always exits 0 even when the fetch/reap steps raise.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import orphan_reaper


# ---------------------------------------------------------------------------
# 1. process_belongs_to_dead_worktree -- pure matching rule
# ---------------------------------------------------------------------------


def test_process_belongs_to_dead_worktree_exact_cwd_match():
    dead = [r"C:\repo\.claude\worktrees\abc123"]
    matched = orphan_reaper.process_belongs_to_dead_worktree(
        r"C:\repo\.claude\worktrees\abc123", None, dead
    )
    assert matched == dead[0]


def test_process_belongs_to_dead_worktree_nested_cwd_match():
    dead = [r"C:\repo\.claude\worktrees\abc123"]
    matched = orphan_reaper.process_belongs_to_dead_worktree(
        r"C:\repo\.claude\worktrees\abc123\meridian", None, dead
    )
    assert matched == dead[0]


def test_process_belongs_to_dead_worktree_cmdline_substring_match():
    dead = [r"C:\repo\.claude\worktrees\abc123"]
    matched = orphan_reaper.process_belongs_to_dead_worktree(
        None, r"python C:\repo\.claude\worktrees\abc123\script.py", dead
    )
    assert matched == dead[0]


def test_process_belongs_to_dead_worktree_no_match():
    dead = [r"C:\repo\.claude\worktrees\abc123"]
    matched = orphan_reaper.process_belongs_to_dead_worktree(
        r"C:\repo\.claude\worktrees\other456", "node server.js", dead
    )
    assert matched is None


def test_process_belongs_to_dead_worktree_empty_inputs():
    assert orphan_reaper.process_belongs_to_dead_worktree(None, None, ["x"]) is None
    assert orphan_reaper.process_belongs_to_dead_worktree("C:\\x", "y", []) is None


# ---------------------------------------------------------------------------
# 2. list_orphan_candidates / reap_orphans -- mocked process listing + kill
# ---------------------------------------------------------------------------

_DEAD_PATH = r"C:\repo\.claude\worktrees\deadsess"


def _fake_processes():
    return [
        # A pixi process rooted in the dead worktree -- should be reaped.
        {"pid": 111, "name": "pixi.exe", "cwd": _DEAD_PATH, "cmdline": "pixi run python -m meridian --mcp"},
        # A python process rooted in the dead worktree -- should be reaped.
        {"pid": 222, "name": "python.exe", "cwd": _DEAD_PATH + "\\meridian", "cmdline": "python -m meridian"},
        # A node process NOT rooted in the dead worktree -- must be left alone.
        {"pid": 333, "name": "node.exe", "cwd": r"C:\repo\other-worktree", "cmdline": "node server.js"},
        # A pixi process rooted in the dead worktree but wrong name family is fine --
        # explorer.exe is neither pixi/python/node -- must be ignored regardless of cwd.
        {"pid": 444, "name": "explorer.exe", "cwd": _DEAD_PATH, "cmdline": "explorer.exe"},
    ]


def test_list_orphan_candidates_filters_by_name_and_path():
    candidates = orphan_reaper.list_orphan_candidates(
        [_DEAD_PATH], process_iter=_fake_processes
    )
    pids = {c["pid"] for c in candidates}
    assert pids == {111, 222}


def test_list_orphan_candidates_no_dead_paths_returns_empty():
    assert orphan_reaper.list_orphan_candidates([], process_iter=_fake_processes) == []


def test_list_orphan_candidates_process_iter_failure_degrades_to_empty():
    def _boom():
        raise RuntimeError("psutil exploded")

    assert orphan_reaper.list_orphan_candidates([_DEAD_PATH], process_iter=_boom) == []


def test_reap_orphans_kills_only_matching_processes_via_injected_kill_fn():
    killed_pids: list[int] = []

    def _fake_kill(pid: int) -> bool:
        killed_pids.append(pid)
        return True

    result = orphan_reaper.reap_orphans(
        [_DEAD_PATH], process_iter=_fake_processes, kill_fn=_fake_kill
    )
    assert result["candidates_count"] == 2
    assert result["killed_count"] == 2
    assert result["skipped_count"] == 0
    assert set(killed_pids) == {111, 222}
    # Never touched the unrelated node process or the non-target explorer.exe.
    assert 333 not in killed_pids
    assert 444 not in killed_pids


def test_reap_orphans_dry_run_never_calls_kill_fn():
    calls: list[int] = []

    def _fake_kill(pid: int) -> bool:
        calls.append(pid)
        return True

    result = orphan_reaper.reap_orphans(
        [_DEAD_PATH], process_iter=_fake_processes, kill_fn=_fake_kill, dry_run=True
    )
    assert calls == []
    assert result["killed_count"] == 0
    assert result["skipped_count"] == 2
    assert all(s["reason"] == "dry_run" for s in result["skipped"])


def test_reap_orphans_kill_failure_is_recorded_not_raised():
    def _fake_kill(pid: int) -> bool:
        return False

    result = orphan_reaper.reap_orphans(
        [_DEAD_PATH], process_iter=_fake_processes, kill_fn=_fake_kill
    )
    assert result["killed_count"] == 0
    assert result["skipped_count"] == 2
    assert all(s["reason"] == "kill_failed" for s in result["skipped"])


def test_reap_orphans_kill_fn_exception_is_swallowed_and_skipped():
    def _fake_kill(pid: int) -> bool:
        raise OSError("access denied")

    result = orphan_reaper.reap_orphans(
        [_DEAD_PATH], process_iter=_fake_processes, kill_fn=_fake_kill
    )
    assert result["killed_count"] == 0
    assert result["skipped_count"] == 2


# ---------------------------------------------------------------------------
# 3. fetch_dead_worktree_paths -- mocked HTTP, no real network
# ---------------------------------------------------------------------------


def test_fetch_dead_worktree_paths_parses_path_list(monkeypatch):
    import io
    import json

    payload = json.dumps(
        [{"id": "w1", "path": r"C:\repo\.claude\worktrees\w1"}, {"id": "w2", "path": r"C:\repo\.claude\worktrees\w2"}]
    ).encode("utf-8")

    class _FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return payload

    def _fake_urlopen(url, timeout=5.0):
        assert "/worktrees/pending_cleanup" in url
        return _FakeResp()

    monkeypatch.setattr(orphan_reaper.urllib.request, "urlopen", _fake_urlopen)
    paths = orphan_reaper.fetch_dead_worktree_paths("http://localhost:7878", "proj1")
    assert paths == [r"C:\repo\.claude\worktrees\w1", r"C:\repo\.claude\worktrees\w2"]


def test_fetch_dead_worktree_paths_network_error_returns_empty(monkeypatch):
    def _fake_urlopen(url, timeout=5.0):
        raise OSError("connection refused")

    monkeypatch.setattr(orphan_reaper.urllib.request, "urlopen", _fake_urlopen)
    assert orphan_reaper.fetch_dead_worktree_paths("http://localhost:7878", "proj1") == []


def test_fetch_dead_worktree_paths_malformed_json_returns_empty(monkeypatch):
    class _FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return b"not json"

    monkeypatch.setattr(orphan_reaper.urllib.request, "urlopen", lambda url, timeout=5.0: _FakeResp())
    assert orphan_reaper.fetch_dead_worktree_paths("http://localhost:7878", "proj1") == []


# ---------------------------------------------------------------------------
# 4. seed_orphan_reaper_hook -- registers via 273287cb custom_hooks infra
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_orphan_reaper_hook_creates_stop_hook(db):
    p = await db_module.create_project(db, "orphan-reaper-seed-test")
    hook = await orphan_reaper.seed_orphan_reaper_hook(db, p["id"], url="http://localhost:9999")

    assert hook["slug"] == orphan_reaper.HOOK_NAME
    assert hook["event"] == "Stop"
    assert bool(hook["blocking"]) is False
    assert hook["enabled"]
    assert p["id"] in hook["script_sh"]
    assert "http://localhost:9999" in hook["script_sh"]
    assert "meridian.orphan_reaper" in hook["script_sh"]
    assert "meridian.orphan_reaper" in hook["script_ps1"]

    all_hooks = await db_module.get_custom_hooks(db, p["id"])
    assert len(all_hooks) == 1


@pytest.mark.asyncio
async def test_seed_orphan_reaper_hook_is_idempotent_and_refreshes_script(db):
    p = await db_module.create_project(db, "orphan-reaper-idempotent-test")
    first = await orphan_reaper.seed_orphan_reaper_hook(db, p["id"], url="http://localhost:1111")
    second = await orphan_reaper.seed_orphan_reaper_hook(db, p["id"], url="http://localhost:2222")

    assert first["id"] == second["id"]
    assert "http://localhost:2222" in second["script_sh"]

    all_hooks = await db_module.get_custom_hooks(db, p["id"])
    assert len(all_hooks) == 1


@pytest.mark.asyncio
async def test_seed_orphan_reaper_hook_never_uses_reserved_sprint_guard_slug(db):
    """The reserved-slug guard in add_custom_hook must never fire for our
    hook -- it registers under its own 'orphan_reaper' slug, not
    'sprint_guard'."""
    p = await db_module.create_project(db, "orphan-reaper-slug-test")
    hook = await orphan_reaper.seed_orphan_reaper_hook(db, p["id"])
    assert hook["slug"] != "sprint_guard"


# ---------------------------------------------------------------------------
# 5. GET /projects/{id}/worktrees/pending_cleanup
# ---------------------------------------------------------------------------


def test_pending_cleanup_route_empty_when_no_dead_worktrees(client):
    project = client.post("/projects", json={"name": "pending-cleanup-empty"}).json()
    r = client.get(f"/projects/{project['id']}/worktrees/pending_cleanup")
    assert r.status_code == 200
    assert r.json() == []


def _session_id_from_start(start_json: dict) -> str | None:
    """Pull the full SESSION ID out of a /hooks/session-start response
    (mirrors the identically-named helper in tests/test_core.py)."""
    ctx = start_json["hookSpecificOutput"]["additionalContext"]
    for line in ctx.splitlines():
        if line.startswith("SESSION ID:"):
            return line.split(":", 1)[1].strip()
    return None


def test_pending_cleanup_route_lists_worktree_with_terminal_sprint_item(client):
    project = client.post("/projects", json={"name": "pending-cleanup-terminal-item"}).json()
    start = client.post("/hooks/session-start", json={"project_id": project["id"]}).json()
    session_id = _session_id_from_start(start)
    assert session_id

    item = client.post(
        f"/projects/{project['id']}/sprint-items",
        json={"version": "v1", "title": "do the thing"},
    )
    assert item.status_code == 201
    item_id = item.json()["id"]

    wt = client.post(
        f"/projects/{project['id']}/worktrees",
        json={
            "session_id": session_id,
            "branch": "worktree/dead123",
            "path": ".claude/worktrees/dead123",
            "item_id": item_id,
        },
    ).json()

    # Not yet pending cleanup -- item still pending.
    r = client.get(f"/projects/{project['id']}/worktrees/pending_cleanup")
    assert wt["id"] not in {w["id"] for w in r.json()}

    # Mark the item done -- now it's a dead-worktree candidate the hook's
    # HTTP fetch should surface.
    complete = client.post(f"/projects/{project['id']}/sprint-items/{item_id}/complete")
    assert complete.status_code == 200

    r2 = client.get(f"/projects/{project['id']}/worktrees/pending_cleanup")
    ids = {w["id"]: w for w in r2.json()}
    assert wt["id"] in ids
    assert ids[wt["id"]]["path"] == ".claude/worktrees/dead123"


def test_pending_cleanup_route_404_for_unknown_project(client):
    r = client.get("/projects/does-not-exist/worktrees/pending_cleanup")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 6. main() -- CLI entry point, always exits 0
# ---------------------------------------------------------------------------


def test_main_returns_zero_when_no_dead_worktrees(monkeypatch):
    monkeypatch.setattr(orphan_reaper, "fetch_dead_worktree_paths", lambda *a, **k: [])
    rc = orphan_reaper.main(["--project-id", "proj1", "--url", "http://x"])
    assert rc == 0


def test_main_returns_zero_and_reaps_when_dead_worktrees_present(monkeypatch):
    monkeypatch.setattr(
        orphan_reaper, "fetch_dead_worktree_paths", lambda *a, **k: [_DEAD_PATH]
    )
    called = {}

    def _fake_reap(dead_paths, dry_run=False, **kwargs):
        called["dead_paths"] = dead_paths
        called["dry_run"] = dry_run
        return {"killed_count": 2, "skipped_count": 0, "candidates_count": 2, "killed": [], "skipped": []}

    monkeypatch.setattr(orphan_reaper, "reap_orphans", _fake_reap)
    rc = orphan_reaper.main(["--project-id", "proj1", "--url", "http://x"])
    assert rc == 0
    assert called["dead_paths"] == [_DEAD_PATH]
    assert called["dry_run"] is False


def test_main_swallows_unexpected_exceptions_and_still_returns_zero(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("network on fire")

    monkeypatch.setattr(orphan_reaper, "fetch_dead_worktree_paths", _boom)
    rc = orphan_reaper.main(["--project-id", "proj1", "--url", "http://x"])
    assert rc == 0


def test_main_dry_run_flag_is_forwarded(monkeypatch):
    monkeypatch.setattr(
        orphan_reaper, "fetch_dead_worktree_paths", lambda *a, **k: [_DEAD_PATH]
    )
    called = {}

    def _fake_reap(dead_paths, dry_run=False, **kwargs):
        called["dry_run"] = dry_run
        return {"killed_count": 0, "skipped_count": 0, "candidates_count": 0, "killed": [], "skipped": []}

    monkeypatch.setattr(orphan_reaper, "reap_orphans", _fake_reap)
    orphan_reaper.main(["--project-id", "proj1", "--url", "http://x", "--dry-run"])
    assert called["dry_run"] is True
