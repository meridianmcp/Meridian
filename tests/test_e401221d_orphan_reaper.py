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

f7084ed0 -- deterministic, tree-safe, opt-out cleanup. Additional coverage:
7. ``_identity_matches`` -- pure PID+create_time ownership check.
8. ``reap_orphans`` ownership revalidation -- PID reuse / already-exited
   candidates are safe no-ops (mocked process snapshots only, never a real
   OS process).
9. Expanded target-name coverage (cmd/uv/uvx/conhost) and the WebView2
   denylist -- ``_proc_name_is_target``.
10. ``_psutil_kill_tree`` / ``_psutil_collect_tree`` -- tree-safe kill on
    both platforms, exercised ONLY against a fake ``psutil`` module injected
    via ``monkeypatch.setitem(sys.modules, "psutil", ...)`` (the same
    technique already used for ``tunnel_client``'s equivalent process-kill
    functions in tests/test_tunnel_client.py) -- never a real, disposable, or
    otherwise genuine OS process.
11. ``seed_orphan_reaper_hook`` disabled-by-default + enabled-state
    preserved across idempotent refresh.
12. ``set_orphan_reaper_enabled`` / ``remove_orphan_reaper_artifacts`` --
    the dashboard-toggle backend and its "delete hook files immediately"
    behavior.
13. ``GET``/``POST /projects/{id}/orphan_reaper(/toggle)`` -- the dashboard
    REST routes in ``meridian/server.py``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from meridian import db as db_module
from meridian import orphan_reaper
from meridian import worktree_cleanup


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
# 2a. _identity_matches -- pure PID+create_time ownership check
# ---------------------------------------------------------------------------


def test_identity_matches_current_none_is_always_false():
    assert orphan_reaper._identity_matches({"name": "pixi.exe"}, None) is False


def test_identity_matches_name_mismatch_is_false():
    candidate = {"name": "pixi.exe", "create_time": 1000.0}
    current = {"name": "explorer.exe", "create_time": 1000.0}
    assert orphan_reaper._identity_matches(candidate, current) is False


def test_identity_matches_create_time_mismatch_is_false():
    """Same name, but the pid's start time has changed -- the OS reassigned
    this pid to a different process (or the original one restarted) since
    the candidate was first observed."""
    candidate = {"name": "python.exe", "create_time": 1000.0}
    current = {"name": "python.exe", "create_time": 5000.0}
    assert orphan_reaper._identity_matches(candidate, current) is False


def test_identity_matches_create_time_within_tolerance_is_true():
    candidate = {"name": "python.exe", "create_time": 1000.25}
    current = {"name": "python.exe", "create_time": 1000.4}
    assert orphan_reaper._identity_matches(candidate, current) is True


def test_identity_matches_matching_name_and_create_time_is_true():
    candidate = {"name": "pixi.exe", "create_time": 42.0}
    current = {"name": "pixi.exe", "create_time": 42.0}
    assert orphan_reaper._identity_matches(candidate, current) is True


def test_identity_matches_missing_create_time_on_either_side_falls_back_to_name():
    """Nothing to contradict -- must not reject just because create_time
    wasn't captured on one (or either) side."""
    assert orphan_reaper._identity_matches(
        {"name": "pixi.exe"}, {"name": "pixi.exe", "create_time": 42.0}
    ) is True
    assert orphan_reaper._identity_matches(
        {"name": "pixi.exe", "create_time": 42.0}, {"name": "pixi.exe"}
    ) is True
    assert orphan_reaper._identity_matches(
        {"name": "pixi.exe"}, {"name": "pixi.exe"}
    ) is True


def test_identity_matches_missing_name_on_either_side_falls_back_to_create_time():
    assert orphan_reaper._identity_matches(
        {"create_time": 1.0}, {"name": "pixi.exe", "create_time": 1.0}
    ) is True


# ---------------------------------------------------------------------------
# 2b. reap_orphans ownership revalidation -- PID reuse / already-exited
#     candidates are safe no-ops. All process snapshots below are plain
#     dicts fed through the process_iter injection point -- no real OS
#     process is ever spawned, signalled, or even looked up.
# ---------------------------------------------------------------------------


def test_reap_orphans_skips_pid_reused_by_unrelated_process():
    """The OS reassigned pid 111 to an unrelated (non-target-name) process
    between the initial scan and the kill -- must be a safe no-op, not a
    kill, even though the ORIGINAL scan matched it correctly."""
    calls = iter([
        _fake_processes(),  # initial scan (list_orphan_candidates)
        [  # revalidation snapshot immediately before killing
            {"pid": 111, "name": "explorer.exe", "cwd": r"C:\Users\someone", "cmdline": "explorer.exe"},
            {"pid": 222, "name": "python.exe", "cwd": _DEAD_PATH + "\\meridian", "cmdline": "python -m meridian"},
        ],
    ])
    killed_pids: list[int] = []

    def _fake_kill(pid: int) -> bool:
        killed_pids.append(pid)
        return True

    result = orphan_reaper.reap_orphans(
        [_DEAD_PATH], process_iter=lambda: next(calls), kill_fn=_fake_kill
    )
    assert 111 not in killed_pids
    assert 222 in killed_pids
    mismatch = [s for s in result["skipped"] if s["pid"] == 111]
    assert len(mismatch) == 1
    assert mismatch[0]["reason"] == "identity_mismatch"


def test_reap_orphans_skips_already_exited_candidate():
    """pid 222 exited on its own between the scan and the kill -- absent
    from the revalidation snapshot entirely -- must be recorded as
    already_gone, never passed to kill_fn."""
    calls = iter([
        _fake_processes(),
        [
            {"pid": 111, "name": "pixi.exe", "cwd": _DEAD_PATH, "cmdline": "pixi run python -m meridian --mcp"},
            # pid 222 is simply gone from this snapshot.
        ],
    ])
    killed_pids: list[int] = []

    def _fake_kill(pid: int) -> bool:
        killed_pids.append(pid)
        return True

    result = orphan_reaper.reap_orphans(
        [_DEAD_PATH], process_iter=lambda: next(calls), kill_fn=_fake_kill
    )
    assert killed_pids == [111]
    already_gone = [s for s in result["skipped"] if s["pid"] == 222]
    assert len(already_gone) == 1
    assert already_gone[0]["reason"] == "already_gone"


def test_reap_orphans_revalidation_failure_fails_closed_never_kills():
    """If the revalidation snapshot itself can't be taken, every candidate
    must be treated as unconfirmed -- fail closed, never fall back to
    killing blindly off the (now possibly stale) initial scan."""
    calls = iter([_fake_processes()])

    def _iter():
        try:
            return next(calls)
        except StopIteration:
            raise RuntimeError("psutil exploded on the revalidation pass")

    killed_pids: list[int] = []

    def _fake_kill(pid: int) -> bool:
        killed_pids.append(pid)
        return True

    result = orphan_reaper.reap_orphans([_DEAD_PATH], process_iter=_iter, kill_fn=_fake_kill)
    assert killed_pids == []
    assert result["killed_count"] == 0
    assert result["skipped_count"] == 2
    assert all(s["reason"] == "already_gone" for s in result["skipped"])


def test_reap_orphans_dry_run_skips_revalidation_snapshot_entirely():
    """A dry run must never even attempt the revalidation snapshot -- it
    signals nothing, so there is nothing to protect against a race for."""
    calls_made = {"n": 0}

    def _iter():
        calls_made["n"] += 1
        return _fake_processes()

    result = orphan_reaper.reap_orphans([_DEAD_PATH], process_iter=_iter, dry_run=True)
    # Exactly one call: the initial scan inside list_orphan_candidates.
    assert calls_made["n"] == 1
    assert result["skipped_count"] == 2
    assert all(s["reason"] == "dry_run" for s in result["skipped"])


# ---------------------------------------------------------------------------
# 2c. Expanded target-name coverage (cmd/uv/uvx/conhost) + WebView2 denylist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["cmd.exe", "uv.exe", "uvx.exe", "conhost.exe"])
def test_proc_name_is_target_covers_expanded_family(name):
    """f7084ed0 -- previously-missed process families this item's
    prospecting flagged as coverage gaps."""
    assert orphan_reaper._proc_name_is_target(name) is True


def test_proc_name_is_target_excludes_webview2_even_if_name_matches():
    # Contrived name containing "node" (a real target substring) to prove
    # the denylist wins even when a target substring would otherwise match.
    assert orphan_reaper._proc_name_is_target("msedgewebview2_node_helper.exe") is False


def test_list_orphan_candidates_reaps_cmd_and_conhost_rooted_in_dead_path():
    def _procs():
        return [
            {"pid": 555, "name": "cmd.exe", "cwd": _DEAD_PATH, "cmdline": "cmd.exe /c pixi run test"},
            {"pid": 666, "name": "conhost.exe", "cwd": _DEAD_PATH, "cmdline": "conhost.exe 0x4"},
            {"pid": 777, "name": "uvx.exe", "cwd": _DEAD_PATH, "cmdline": "uvx run something"},
        ]

    candidates = orphan_reaper.list_orphan_candidates([_DEAD_PATH], process_iter=_procs)
    assert {c["pid"] for c in candidates} == {555, 666, 777}


# ---------------------------------------------------------------------------
# 2d. _psutil_kill_tree / _psutil_collect_tree -- tree-safe kill, exercised
#     ONLY against a fake `psutil` module injected via
#     monkeypatch.setitem(sys.modules, "psutil", ...) -- the same technique
#     tests/test_tunnel_client.py already uses for the equivalent real-kill
#     functions there. NEVER a real, disposable, or otherwise genuine OS
#     process is spawned or signalled by any test in this file.
# ---------------------------------------------------------------------------


def _install_fake_psutil(monkeypatch, *, children_by_pid=None, alive_pids=None):
    """Mirrors tests/test_tunnel_client.py::_install_fake_psutil. Returns a
    dict of call-log lists (terminate/kill per pid) the test can assert on.
    `alive_pids` (mutable set) lets a test simulate a process actually going
    away after terminate()/kill() -- pid_exists() consults it live."""
    import types

    children_by_pid = children_by_pid or {}
    if alive_pids is None:
        alive_pids = set(children_by_pid) | {p for kids in children_by_pid.values() for p in kids}
    calls: dict[str, list[int]] = {"terminate": [], "kill": []}

    fake_psutil = types.ModuleType("psutil")

    class _FakeProc:
        def __init__(self, pid):
            if pid not in alive_pids:
                raise ProcessLookupError(f"no such process: {pid}")
            self.pid = pid

        def _descendant_pids(self):
            direct = [p for p in children_by_pid.get(self.pid, []) if p in alive_pids]
            out = list(direct)
            for p in direct:
                out.extend(_FakeProc(p)._descendant_pids())
            return out

        def children(self, recursive=False):
            pids = self._descendant_pids() if recursive else [
                p for p in children_by_pid.get(self.pid, []) if p in alive_pids
            ]
            return [_FakeProc(p) for p in pids]

        def terminate(self):
            calls["terminate"].append(self.pid)
            alive_pids.discard(self.pid)

        def kill(self):
            calls["kill"].append(self.pid)
            alive_pids.discard(self.pid)

    fake_psutil.Process = _FakeProc
    fake_psutil.pid_exists = lambda pid: pid in alive_pids
    # Mirrors real psutil.wait_procs: a proc whose pid is no longer alive
    # (terminate()/kill() already removed it above) is "gone", not "alive" --
    # so a graceful terminate() that actually worked does NOT force the
    # kill() escalation path.
    fake_psutil.wait_procs = lambda procs, timeout=None: (
        [p for p in procs if p.pid not in alive_pids],
        [p for p in procs if p.pid in alive_pids],
    )

    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    return calls, alive_pids


def test_psutil_collect_tree_returns_pid_plus_recursive_children(monkeypatch):
    _install_fake_psutil(monkeypatch, children_by_pid={100: [101, 102], 101: [103]})
    tree = orphan_reaper._psutil_collect_tree(100)
    assert set(tree) == {100, 101, 102, 103}


def test_psutil_collect_tree_degrades_to_bare_pid_on_lookup_failure(monkeypatch):
    _install_fake_psutil(monkeypatch, children_by_pid={})  # pid 999 doesn't exist
    assert orphan_reaper._psutil_collect_tree(999) == [999]


def test_psutil_kill_tree_windows_uses_taskkill_f_t(monkeypatch):
    monkeypatch.setattr(orphan_reaper.sys, "platform", "win32")
    _install_fake_psutil(monkeypatch, alive_pids=set())  # pid_exists -> False after kill
    run_calls = []

    def _fake_run(argv, **kw):
        run_calls.append(argv)
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(orphan_reaper.subprocess, "run", _fake_run)
    assert orphan_reaper._psutil_kill_tree(4242) is True
    assert run_calls == [["taskkill", "/F", "/T", "/PID", "4242"]]


def test_psutil_kill_tree_posix_terminates_whole_family(monkeypatch):
    monkeypatch.setattr(orphan_reaper.sys, "platform", "linux")
    calls, alive = _install_fake_psutil(monkeypatch, children_by_pid={100: [101, 102]})
    assert orphan_reaper._psutil_kill_tree(100) is True
    # All three -- parent and both children -- were terminated (graceful first).
    assert set(calls["terminate"]) == {100, 101, 102}
    assert calls["kill"] == []  # graceful termination was enough
    assert alive == set()


def test_psutil_kill_tree_posix_escalates_to_kill_for_stragglers(monkeypatch):
    monkeypatch.setattr(orphan_reaper.sys, "platform", "linux")
    import types

    fake_psutil = types.ModuleType("psutil")
    calls: dict[str, list[int]] = {"terminate": [], "kill": []}
    alive_after_kill = {200}  # pretend pid 200 never actually dies -- pid_exists still True

    class _FakeProc:
        def __init__(self, pid):
            self.pid = pid

        def children(self, recursive=False):
            return []

        def terminate(self):
            calls["terminate"].append(self.pid)

        def kill(self):
            calls["kill"].append(self.pid)

    fake_psutil.Process = _FakeProc
    # wait_procs always reports everyone still alive -- forces the kill() escalation path.
    fake_psutil.wait_procs = lambda procs, timeout=None: ([], list(procs))
    fake_psutil.pid_exists = lambda pid: pid in alive_after_kill
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert orphan_reaper._psutil_kill_tree(200) is False  # still alive -> not confirmed gone
    assert calls["terminate"] == [200]
    assert calls["kill"] == [200]


def test_psutil_kill_tree_missing_psutil_returns_false(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _fail_psutil_import(name, *a, **kw):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fail_psutil_import)
    assert orphan_reaper._psutil_kill_tree(1) is False


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
    # f7084ed0 -- disabled by default: cleanup is opt-in, never silently
    # active the moment a project first runs generate_handoff.
    assert bool(hook["enabled"]) is False
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
async def test_seed_orphan_reaper_hook_refresh_preserves_enabled_state(db):
    """f7084ed0 -- a refresh (script-body update on an already-existing row)
    must never clobber whatever enabled state a human (or
    set_orphan_reaper_enabled) previously set. Only set_orphan_reaper_enabled
    is allowed to change it."""
    p = await db_module.create_project(db, "orphan-reaper-enabled-preserve-test")
    first = await orphan_reaper.seed_orphan_reaper_hook(db, p["id"], url="http://localhost:1111")
    assert bool(first["enabled"]) is False

    # Simulate a human turning it on via the dashboard toggle.
    await db_module.update_custom_hook(db, p["id"], first["id"], enabled=True)

    refreshed = await orphan_reaper.seed_orphan_reaper_hook(db, p["id"], url="http://localhost:2222")
    assert refreshed["id"] == first["id"]
    assert "http://localhost:2222" in refreshed["script_sh"]
    assert bool(refreshed["enabled"]) is True


@pytest.mark.asyncio
async def test_seed_orphan_reaper_hook_never_uses_reserved_sprint_guard_slug(db):
    """The reserved-slug guard in add_custom_hook must never fire for our
    hook -- it registers under its own 'orphan_reaper' slug, not
    'sprint_guard'."""
    p = await db_module.create_project(db, "orphan-reaper-slug-test")
    hook = await orphan_reaper.seed_orphan_reaper_hook(db, p["id"])
    assert hook["slug"] != "sprint_guard"


# ---------------------------------------------------------------------------
# 4a. set_orphan_reaper_enabled -- the ONLY path that flips the hook's
#     enabled flag; backs the dashboard toggle.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_orphan_reaper_enabled_creates_disabled_row_then_enables(db):
    p = await db_module.create_project(db, "orphan-reaper-toggle-enable-test")
    # No hook row exists yet for this project at all.
    assert await db_module.get_custom_hooks(db, p["id"]) == []

    hook = await orphan_reaper.set_orphan_reaper_enabled(db, p["id"], True)
    assert bool(hook["enabled"]) is True
    assert hook["slug"] == orphan_reaper.HOOK_NAME

    all_hooks = await db_module.get_custom_hooks(db, p["id"])
    assert len(all_hooks) == 1
    assert bool(all_hooks[0]["enabled"]) is True


@pytest.mark.asyncio
async def test_set_orphan_reaper_enabled_toggles_existing_row_off(db):
    p = await db_module.create_project(db, "orphan-reaper-toggle-disable-test")
    await orphan_reaper.set_orphan_reaper_enabled(db, p["id"], True)

    hook = await orphan_reaper.set_orphan_reaper_enabled(db, p["id"], False)
    assert bool(hook["enabled"]) is False

    all_hooks = await db_module.get_custom_hooks(db, p["id"])
    assert len(all_hooks) == 1  # still one row -- toggled, not deleted/recreated
    assert bool(all_hooks[0]["enabled"]) is False


@pytest.mark.asyncio
async def test_set_orphan_reaper_enabled_is_a_noop_when_already_at_desired_state(db):
    p = await db_module.create_project(db, "orphan-reaper-toggle-noop-test")
    first = await orphan_reaper.set_orphan_reaper_enabled(db, p["id"], False)
    second = await orphan_reaper.set_orphan_reaper_enabled(db, p["id"], False)
    assert first["id"] == second["id"]
    assert bool(second["enabled"]) is False


# ---------------------------------------------------------------------------
# 4b. remove_orphan_reaper_artifacts -- pure filesystem cleanup, no DB/OS
#     process involvement at all.
# ---------------------------------------------------------------------------


def test_remove_orphan_reaper_artifacts_deletes_present_files(tmp_path):
    hooks_dir = tmp_path / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "orphan_reaper.sh").write_text("# stub", encoding="utf-8")
    (hooks_dir / "orphan_reaper.ps1").write_text("# stub", encoding="utf-8")
    (hooks_dir / "orphan_reaper_body.ps1").write_text("# stub", encoding="utf-8")
    (hooks_dir / "sprint_guard.sh").write_text("# unrelated -- must survive", encoding="utf-8")

    removed = orphan_reaper.remove_orphan_reaper_artifacts(hooks_dir)

    assert set(removed) == {"orphan_reaper.sh", "orphan_reaper.ps1", "orphan_reaper_body.ps1"}
    assert not (hooks_dir / "orphan_reaper.sh").exists()
    assert not (hooks_dir / "orphan_reaper.ps1").exists()
    assert not (hooks_dir / "orphan_reaper_body.ps1").exists()
    assert (hooks_dir / "sprint_guard.sh").exists()  # never touched


def test_remove_orphan_reaper_artifacts_missing_files_is_safe_noop(tmp_path):
    hooks_dir = tmp_path / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    assert orphan_reaper.remove_orphan_reaper_artifacts(hooks_dir) == []


def test_remove_orphan_reaper_artifacts_missing_dir_is_safe_noop(tmp_path):
    assert orphan_reaper.remove_orphan_reaper_artifacts(tmp_path / "does-not-exist") == []


# ---------------------------------------------------------------------------
# 4c. GET/POST /projects/{id}/orphan_reaper(/toggle) -- dashboard REST routes
# ---------------------------------------------------------------------------


def test_orphan_reaper_status_route_defaults_unregistered_and_disabled(client):
    project = client.post("/projects", json={"name": "orphan-status-default"}).json()
    r = client.get(f"/projects/{project['id']}/orphan_reaper")
    assert r.status_code == 200
    body = r.json()
    assert body == {"registered": False, "enabled": False}


def test_orphan_reaper_toggle_route_enables_and_status_reflects_it(client):
    project = client.post("/projects", json={"name": "orphan-toggle-enable"}).json()
    pid = project["id"]

    r = client.post(f"/projects/{pid}/orphan_reaper/toggle", json={"enabled": True})
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    assert r.json()["removed_files"] == []

    status = client.get(f"/projects/{pid}/orphan_reaper").json()
    assert status == {"registered": True, "enabled": True}


def test_orphan_reaper_toggle_route_disable_without_repo_path_removes_nothing(client):
    """No executor_config.repo_path configured for this project -- disabling
    must still succeed (DB flag flips) but there's no known .claude/hooks
    directory to clean up on this machine."""
    project = client.post("/projects", json={"name": "orphan-toggle-disable-norepo"}).json()
    pid = project["id"]
    client.post(f"/projects/{pid}/orphan_reaper/toggle", json={"enabled": True})

    r = client.post(f"/projects/{pid}/orphan_reaper/toggle", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert r.json()["removed_files"] == []

    status = client.get(f"/projects/{pid}/orphan_reaper").json()
    assert status == {"registered": True, "enabled": False}


def test_orphan_reaper_toggle_route_disable_removes_hook_files_immediately(client, tmp_path):
    """f7084ed0's core dashboard-toggle promise: disabling deletes the
    already-written hook files right away, not on the next generate_handoff."""
    repo_dir = tmp_path / "myrepo"
    hooks_dir = repo_dir / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "orphan_reaper.sh").write_text("# stub", encoding="utf-8")
    (hooks_dir / "orphan_reaper.ps1").write_text("# stub", encoding="utf-8")

    project = client.post("/projects", json={"name": "orphan-toggle-disable-removes"}).json()
    pid = project["id"]
    r = client.patch(f"/projects/{pid}/settings", json={
        "executor_config": {"repo_path": str(repo_dir)}
    })
    assert r.status_code == 200
    client.post(f"/projects/{pid}/orphan_reaper/toggle", json={"enabled": True})

    r = client.post(f"/projects/{pid}/orphan_reaper/toggle", json={"enabled": False})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert set(body["removed_files"]) == {"orphan_reaper.sh", "orphan_reaper.ps1"}
    assert not (hooks_dir / "orphan_reaper.sh").exists()
    assert not (hooks_dir / "orphan_reaper.ps1").exists()


def test_orphan_reaper_status_route_404_for_unknown_project(client):
    r = client.get("/projects/does-not-exist/orphan_reaper")
    assert r.status_code == 404


def test_orphan_reaper_toggle_route_404_for_unknown_project(client):
    r = client.post("/projects/does-not-exist/orphan_reaper/toggle", json={"enabled": True})
    assert r.status_code == 404


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


def test_main_dry_run_prints_machine_readable_json(monkeypatch, capsys):
    """f7084ed0 -- a dry run's result must be printed as parseable JSON on
    stdout (not just a human sentence on stderr like a real run), so a human
    or the dashboard preview can consume exactly what would have been
    touched."""
    monkeypatch.setattr(
        orphan_reaper, "fetch_dead_worktree_paths", lambda *a, **k: [_DEAD_PATH]
    )
    fake_result = {
        "candidates_count": 1,
        "killed": [],
        "skipped": [{"pid": 111, "name": "pixi.exe", "reason": "dry_run"}],
        "killed_count": 0,
        "skipped_count": 1,
    }
    monkeypatch.setattr(orphan_reaper, "reap_orphans", lambda *a, **k: fake_result)

    rc = orphan_reaper.main(["--project-id", "proj1", "--url", "http://x", "--dry-run"])
    assert rc == 0

    import json as _json
    out = capsys.readouterr().out
    parsed = _json.loads(out)
    assert parsed == fake_result


# ---------------------------------------------------------------------------
# 7. find_temp_output_candidates -- pure filesystem walk / naming prefilter
# ---------------------------------------------------------------------------


def test_find_temp_output_candidates_matches_naming_conventions(tmp_path):
    root = tmp_path / "deadwt"
    root.mkdir()
    (root / "results.csv").write_text("keep")
    (root / "results_backup.csv").write_text("candidate")
    (root / "data.csv.bak").write_text("candidate")
    (root / "notes.txt~").write_text("candidate")
    (root / "run_old_2.json").write_text("candidate")
    nested = root / "nested"
    nested.mkdir()
    (nested / "figure_stale.png").write_text("candidate")
    (nested / "figure.png").write_text("keep")

    candidates = orphan_reaper.find_temp_output_candidates([str(root)])
    names = {os.path.basename(p) for p in candidates}
    assert names == {
        "results_backup.csv",
        "data.csv.bak",
        "notes.txt~",
        "run_old_2.json",
        "figure_stale.png",
    }


def test_find_temp_output_candidates_missing_dir_contributes_nothing():
    assert orphan_reaper.find_temp_output_candidates([r"C:\does\not\exist"]) == []


def test_find_temp_output_candidates_empty_dead_paths():
    assert orphan_reaper.find_temp_output_candidates([]) == []


# ---------------------------------------------------------------------------
# 8. worktree_cleanup quarantine engine -- manifest / archive / restore / purge
# ---------------------------------------------------------------------------


def _eligible_if_backup(path: str) -> dict:
    """Fake ownership_check: only "*_backup.*" paths are eligible -- mirrors
    the shape ``provenance.classify_temp_output_ownership`` returns."""
    if "backup" in os.path.basename(path):
        return {"eligible": True, "reason": "looks like a backup copy"}
    return {"eligible": False, "reason": "not a backup copy"}


def test_build_quarantine_manifest_fails_closed_without_ownership_check(tmp_path):
    f = tmp_path / "results_backup.csv"
    f.write_text("hello")
    manifest = worktree_cleanup.build_quarantine_manifest(
        [str(f)], archive_root=str(tmp_path / "archive")
    )
    assert manifest["eligible_count"] == 0
    assert manifest["entries"][0]["eligible"] is False
    assert "no ownership_check supplied" in manifest["entries"][0]["reason"]


def test_build_quarantine_manifest_computes_hash_size_and_archive_path(tmp_path):
    f = tmp_path / "results_backup.csv"
    f.write_text("hello world")
    manifest = worktree_cleanup.build_quarantine_manifest(
        [str(f)], archive_root=str(tmp_path / "archive"), ownership_check=_eligible_if_backup
    )
    entry = manifest["entries"][0]
    assert entry["eligible"] is True
    assert entry["exists"] is True
    assert entry["size"] == len("hello world")
    assert entry["content_hash"] is not None
    assert entry["archive_path"].startswith(str(tmp_path / "archive"))
    assert entry["archive_path"].endswith("results_backup.csv")
    assert entry["restore_destination"] == str(f)
    # Dry-run: nothing on disk actually moved.
    assert f.is_file()


def test_build_quarantine_manifest_missing_file_has_no_hash(tmp_path):
    missing = tmp_path / "gone_backup.csv"
    manifest = worktree_cleanup.build_quarantine_manifest(
        [str(missing)], archive_root=str(tmp_path / "archive"), ownership_check=_eligible_if_backup
    )
    entry = manifest["entries"][0]
    assert entry["exists"] is False
    assert entry["content_hash"] is None
    assert entry["archive_path"] is None


def test_build_quarantine_manifest_rejects_canonical_files(tmp_path):
    f = tmp_path / "results.csv"
    f.write_text("hello")
    manifest = worktree_cleanup.build_quarantine_manifest(
        [str(f)], archive_root=str(tmp_path / "archive"), ownership_check=_eligible_if_backup
    )
    assert manifest["entries"][0]["eligible"] is False


def test_quarantine_temp_outputs_moves_only_eligible_existing_entries(tmp_path):
    keep = tmp_path / "results.csv"
    keep.write_text("canonical")
    backup = tmp_path / "results_backup.csv"
    backup.write_text("archival copy")
    archive_root = tmp_path / "archive"

    manifest = worktree_cleanup.build_quarantine_manifest(
        [str(keep), str(backup)], archive_root=str(archive_root), ownership_check=_eligible_if_backup
    )
    result = worktree_cleanup.quarantine_temp_outputs(manifest)

    assert result["moved_count"] == 1
    assert result["skipped_count"] == 1
    assert not backup.exists()  # moved out, reversible via restore -- never deleted
    assert keep.exists()  # canonical file untouched
    moved_entry = result["moved"][0]
    assert moved_entry["archived"] is True
    assert Path(moved_entry["archive_path"]).read_text() == "archival copy"


def test_quarantine_temp_outputs_skips_source_that_no_longer_exists(tmp_path):
    backup = tmp_path / "results_backup.csv"
    backup.write_text("archival copy")
    manifest = worktree_cleanup.build_quarantine_manifest(
        [str(backup)], archive_root=str(tmp_path / "archive"), ownership_check=_eligible_if_backup
    )
    backup.unlink()  # vanished between dry-run and the real move

    result = worktree_cleanup.quarantine_temp_outputs(manifest)
    assert result["moved_count"] == 0
    assert result["skipped_count"] == 1
    assert result["skipped"][0]["quarantine_skip_reason"] == "source no longer exists"


def test_restore_quarantined_output_round_trips(tmp_path):
    backup = tmp_path / "results_backup.csv"
    backup.write_text("archival copy")
    archive_root = tmp_path / "archive"
    manifest = worktree_cleanup.build_quarantine_manifest(
        [str(backup)], archive_root=str(archive_root), ownership_check=_eligible_if_backup
    )
    quarantined = worktree_cleanup.quarantine_temp_outputs(manifest)
    entry = quarantined["moved"][0]
    assert not backup.exists()

    outcome = worktree_cleanup.restore_quarantined_output(entry)
    assert outcome["restored"] is True
    assert backup.is_file()
    assert backup.read_text() == "archival copy"
    assert not Path(entry["archive_path"]).exists()


def test_restore_quarantined_output_refuses_on_hash_mismatch(tmp_path):
    backup = tmp_path / "results_backup.csv"
    backup.write_text("archival copy")
    archive_root = tmp_path / "archive"
    manifest = worktree_cleanup.build_quarantine_manifest(
        [str(backup)], archive_root=str(archive_root), ownership_check=_eligible_if_backup
    )
    quarantined = worktree_cleanup.quarantine_temp_outputs(manifest)
    entry = quarantined["moved"][0]

    # Simulate corruption/tampering of the archived file after quarantine.
    Path(entry["archive_path"]).write_text("tampered content")

    outcome = worktree_cleanup.restore_quarantined_output(entry)
    assert outcome["restored"] is False
    assert "hash mismatch" in outcome["reason"]
    assert Path(entry["archive_path"]).exists()  # never moved on a failed check


def test_restore_quarantined_output_refuses_to_overwrite_different_file(tmp_path):
    backup = tmp_path / "results_backup.csv"
    backup.write_text("archival copy")
    archive_root = tmp_path / "archive"
    manifest = worktree_cleanup.build_quarantine_manifest(
        [str(backup)], archive_root=str(archive_root), ownership_check=_eligible_if_backup
    )
    quarantined = worktree_cleanup.quarantine_temp_outputs(manifest)
    entry = quarantined["moved"][0]

    # A different file has since appeared at the original location.
    backup.write_text("an unrelated new file")

    outcome = worktree_cleanup.restore_quarantined_output(entry)
    assert outcome["restored"] is False
    assert "refusing to overwrite" in outcome["reason"]
    assert backup.read_text() == "an unrelated new file"


def test_purge_quarantined_output_fails_closed_without_verify_provenance(tmp_path):
    backup = tmp_path / "results_backup.csv"
    backup.write_text("archival copy")
    archive_root = tmp_path / "archive"
    manifest = worktree_cleanup.build_quarantine_manifest(
        [str(backup)], archive_root=str(archive_root), ownership_check=_eligible_if_backup
    )
    quarantined = worktree_cleanup.quarantine_temp_outputs(manifest)
    entry = quarantined["moved"][0]

    outcome = worktree_cleanup.purge_quarantined_output(entry)
    assert outcome["purged"] is False
    assert "verify_provenance" in outcome["reason"]
    assert Path(entry["archive_path"]).exists()


def test_purge_quarantined_output_deletes_when_reverified_eligible(tmp_path):
    backup = tmp_path / "results_backup.csv"
    backup.write_text("archival copy")
    archive_root = tmp_path / "archive"
    manifest = worktree_cleanup.build_quarantine_manifest(
        [str(backup)], archive_root=str(archive_root), ownership_check=_eligible_if_backup
    )
    quarantined = worktree_cleanup.quarantine_temp_outputs(manifest)
    entry = quarantined["moved"][0]

    outcome = worktree_cleanup.purge_quarantined_output(
        entry, verify_provenance=_eligible_if_backup
    )
    assert outcome["purged"] is True
    assert not Path(entry["archive_path"]).exists()


def test_purge_quarantined_output_refuses_when_reverification_says_ineligible(tmp_path):
    backup = tmp_path / "results_backup.csv"
    backup.write_text("archival copy")
    archive_root = tmp_path / "archive"
    manifest = worktree_cleanup.build_quarantine_manifest(
        [str(backup)], archive_root=str(archive_root), ownership_check=_eligible_if_backup
    )
    quarantined = worktree_cleanup.quarantine_temp_outputs(manifest)
    entry = quarantined["moved"][0]

    outcome = worktree_cleanup.purge_quarantined_output(
        entry, verify_provenance=lambda p: {"eligible": False, "reason": "reclassified as canonical"}
    )
    assert outcome["purged"] is False
    assert "no longer eligible" in outcome["reason"]
    assert Path(entry["archive_path"]).exists()  # never deleted


def test_purge_quarantined_output_refuses_on_hash_mismatch(tmp_path):
    backup = tmp_path / "results_backup.csv"
    backup.write_text("archival copy")
    archive_root = tmp_path / "archive"
    manifest = worktree_cleanup.build_quarantine_manifest(
        [str(backup)], archive_root=str(archive_root), ownership_check=_eligible_if_backup
    )
    quarantined = worktree_cleanup.quarantine_temp_outputs(manifest)
    entry = quarantined["moved"][0]
    Path(entry["archive_path"]).write_text("tampered content")

    outcome = worktree_cleanup.purge_quarantined_output(
        entry, verify_provenance=_eligible_if_backup
    )
    assert outcome["purged"] is False
    assert "hash mismatch" in outcome["reason"]


# ---------------------------------------------------------------------------
# 9. main() --quarantine-outputs wiring -- advisory dry-run only, never moves
# ---------------------------------------------------------------------------


def test_main_quarantine_outputs_flag_prints_dry_run_manifest(monkeypatch, tmp_path, capsys):
    dead_root = tmp_path / "deadwt"
    dead_root.mkdir()
    (dead_root / "results_backup.csv").write_text("candidate")
    (dead_root / "results.csv").write_text("keep")

    monkeypatch.setattr(
        orphan_reaper, "fetch_dead_worktree_paths", lambda *a, **k: [str(dead_root)]
    )
    monkeypatch.setattr(
        orphan_reaper,
        "reap_orphans",
        lambda *a, **k: {"killed_count": 0, "skipped_count": 0, "candidates_count": 0, "killed": [], "skipped": []},
    )

    rc = orphan_reaper.main(
        [
            "--project-id", "proj1",
            "--url", "http://x",
            "--quarantine-outputs",
            "--archive-root", str(tmp_path / "archive"),
        ]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "temp-output quarantine dry-run" in err
    # No ownership_check is wired at the CLI level -- fail-closed default
    # means the dry-run always reports 0 eligible, matching the module's
    # "never guess ownership" contract.
    assert "found 0 eligible file(s) of 1 candidate(s)" in err
    # Never actually moves anything from this CLI entry point.
    assert (dead_root / "results_backup.csv").is_file()


def test_main_quarantine_outputs_flag_silent_when_no_candidates(monkeypatch, tmp_path, capsys):
    dead_root = tmp_path / "deadwt"
    dead_root.mkdir()
    (dead_root / "results.csv").write_text("keep")

    monkeypatch.setattr(
        orphan_reaper, "fetch_dead_worktree_paths", lambda *a, **k: [str(dead_root)]
    )
    monkeypatch.setattr(
        orphan_reaper,
        "reap_orphans",
        lambda *a, **k: {"killed_count": 0, "skipped_count": 0, "candidates_count": 0, "killed": [], "skipped": []},
    )

    rc = orphan_reaper.main(
        ["--project-id", "proj1", "--url", "http://x", "--quarantine-outputs"]
    )
    assert rc == 0
    assert "quarantine" not in capsys.readouterr().err


def test_main_without_quarantine_flag_never_calls_worktree_cleanup(monkeypatch, tmp_path):
    dead_root = tmp_path / "deadwt"
    dead_root.mkdir()
    (dead_root / "results_backup.csv").write_text("candidate")

    monkeypatch.setattr(
        orphan_reaper, "fetch_dead_worktree_paths", lambda *a, **k: [str(dead_root)]
    )
    monkeypatch.setattr(
        orphan_reaper,
        "reap_orphans",
        lambda *a, **k: {"killed_count": 0, "skipped_count": 0, "candidates_count": 0, "killed": [], "skipped": []},
    )
    calls = []
    monkeypatch.setattr(
        worktree_cleanup,
        "build_quarantine_manifest",
        lambda *a, **k: calls.append((a, k)),
    )

    rc = orphan_reaper.main(["--project-id", "proj1", "--url", "http://x"])
    assert rc == 0
    assert calls == []
