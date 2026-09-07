"""dcf78192 — FOLLOW-UP: completion reliability after the event-loop unblock
(f291bb24), bounded critical-path recovery.

Scope of this file (see the sprint item's discovery brief for the full
phase-by-phase trace of what was already fixed by prior items vs. what
remained genuinely open):

1. Wave-run child terminal-outcome bookkeeping
   (``meridian.db.sprint_items._record_wave_run_completion``) was the one
   post-commit advisory step in ``complete_sprint_item`` with NO wall-clock
   bound, unlike every sibling advisory phase in the same function. Now
   bounded under ``_ADVISORY_PHASE_TIMEOUT_S`` exactly like its siblings.

2. The pre-commit worktree merge-validation gate
   (``meridian.worktree_merge_guard.validate_worktree_merge``) made up to
   three SEQUENTIAL 20s-timeout git subprocess calls even after f291bb24
   fixed the event-loop-blocking bug (moving each call to a worker thread
   did not remove the sequencing). ``get_worktree_head`` and
   ``is_worktree_dirty`` are independent of each other's RESULT and now run
   concurrently; only ``is_ancestor`` genuinely depends on ``head_sha`` and
   still runs after.

3. The call site of that gate (``meridian/mcp/handlers/sprint_tools.py``)
   had no outer bound at all — worst case it alone could exceed the entire
   45s ``complete_sprint_item`` dispatch budget. Now wrapped in a bounded
   ``asyncio.wait_for`` that fails CLOSED on timeout (a new
   ``WORKTREE_MERGE_VALIDATION_TIMEOUT`` error), matching pinned decision
   f983b41f's framing of worktree validation as a "genuine gate" (not
   advisory work eligible for the fail-open/deferred treatment every other
   phase in this function gets) and matching this gate's own pre-existing
   philosophy of failing closed on every other "couldn't verify" outcome it
   can produce (HEAD_UNRESOLVABLE, DIRTY_CHECK_FAILED,
   ANCESTRY_UNRESOLVABLE all already block rather than skip).

4. A genuine concurrent-load reproduction: N *distinct* sprint items, each
   with its own session/worktree/manifest, completed concurrently through
   the REAL dispatch path (``server._dispatch_mcp_tool``, not the bare
   db-layer function) so the 45s wrapper and correlation-id phase registry
   are genuinely exercised — the scenario the item's notes describe as
   "fine solo, bad under concurrent load."

Deliberately NOT covered here (see the discovery brief / final report for
the explicit scope-reduction rationale): the pre-commit CI/code-intel/
test-run ``asyncio.gather`` (sprint_tools.py, ~line 1677) has no outer
``wait_for`` either, but it is already internally bounded (~15s total) and
touches the exact code
``tests/test_f291bb24_completion_concurrency.py::test_ci_code_intel_test_run_checks_run_concurrently``
asserts specific timings against — left as a documented follow-up, not
bundled into this pass.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from unittest.mock import patch

import pytest

from meridian import db as db_module
from meridian import server as srv
from meridian import worktree_merge_guard as _merge_guard_mod
from meridian.db import sprint_items as sprint_items_module
from meridian.mcp import handler as handler_module
from meridian.mcp.handlers import sprint_tools as sprint_tools_module


# ---------------------------------------------------------------------------
# Small, self-contained git helpers (mirrors tests/test_worktree_guard.py's
# own helpers — duplicated rather than cross-imported since tests/ is not a
# package here).
# ---------------------------------------------------------------------------

def _run_git_cmd(args: list, cwd: str) -> None:
    subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


def _git_commit(repo_dir: str, filename: str, content: str) -> str:
    with open(os.path.join(repo_dir, filename), "w", encoding="utf-8") as f:
        f.write(content)
    _run_git_cmd(["add", "-A"], repo_dir)
    _run_git_cmd(["commit", "-m", f"add {filename}"], repo_dir)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir,
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _init_git_repo(repo_dir: str) -> None:
    _run_git_cmd(["init"], repo_dir)


def _blocking_subprocess_run_stub(*, delay: float):
    """Stand-in for subprocess.run: sleeps `delay` seconds (a real, blocking
    time.sleep) then returns a plausible, always-clean CompletedProcess —
    HEAD resolves, the tree is never dirty, and every ancestry check passes.
    Mirrors test_worktree_guard.py's helper of the same name/shape."""

    def _stub(cmd, **kwargs):
        time.sleep(delay)
        args = list(cmd[1:]) if cmd and cmd[0] == "git" else list(cmd)
        if args[:2] == ["rev-parse", "HEAD"]:
            stdout = ("b" * 40) + "\n"
        else:
            stdout = ""  # clean `status --porcelain`; `merge-base` success
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

    return _stub


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


# ---------------------------------------------------------------------------
# 1. Wave-run bookkeeping is now bounded — a hang there must not block (or
#    even meaningfully delay) an already-committed completion.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wave_run_bookkeeping_hang_does_not_block_completion(db, monkeypatch):
    """A wave_runs DB round-trip that hangs well past
    _ADVISORY_PHASE_TIMEOUT_S must not stall complete_sprint_item's response
    — the status write already committed before this step ever runs, and
    this step must now defer (advisory_work_deferred=True), not stall,
    exactly like its already-bounded siblings (_run_post_commit_side_effects,
    the continuation-state gather)."""
    pid = await _project(db, "wrc-hang-nowedge")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: hang-safe wave bookkeeping")

    snapshot = await db_module.build_board_snapshot(db, pid)
    run = await db_module.create_wave_run(db, pid, snapshot=snapshot, item_ids=[item["id"]])
    await db_module.record_wave_run_child(
        db, run["id"], item["id"], failure_mode="continue", status="running",
    )

    await db_module.claim_sprint_item(db, pid, item["id"], actor="sess-hang-1")

    from meridian.db import wave_runs as wave_runs_module

    _timeout_s = sprint_items_module._ADVISORY_PHASE_TIMEOUT_S
    _hang_s = _timeout_s + 3.0  # comfortably longer than the bound

    async def _hang(*args, **kwargs):
        await asyncio.sleep(_hang_s)
        raise AssertionError("should have been cancelled by the outer wait_for")

    monkeypatch.setattr(wave_runs_module, "find_active_wave_run_child_for_item", _hang)

    start = time.monotonic()
    result = await db_module.complete_sprint_item(
        db, pid, item["id"], actor="sess-hang-1", exit_code=0,
    )
    elapsed = time.monotonic() - start

    assert result is not None
    assert result["status"] == "done"
    assert result["completion_outcome"] == "committed"
    assert result.get("advisory_work_deferred") is True
    # Must land near the bound, nowhere near the full hang duration.
    assert elapsed < _hang_s, (
        f"took {elapsed:.2f}s -- the {_hang_s:.1f}s hang was not bounded"
    )
    assert elapsed < _timeout_s + 2.0, (
        f"took {elapsed:.2f}s -- expected to land near the "
        f"{_timeout_s:.1f}s advisory bound, not drift far past it"
    )

    reloaded = await db_module.get_sprint_item(db, item["id"])
    assert reloaded["status"] == "done"


# ---------------------------------------------------------------------------
# 2. The outer bound on validate_worktree_merge fails CLOSED, within budget.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worktree_merge_validation_slow_git_fails_closed_within_bound(
    db, tmp_path, monkeypatch,
):
    """A validate_worktree_merge call stuck behind slow git subprocess calls
    must not be allowed to consume the whole 45s dispatch budget (worst case
    pre-fix: up to 3 sequential 20s-timeout calls = 60s, already over
    budget). The call site now wraps it in its own bounded wait_for and
    fails CLOSED on timeout -- a fast, structured, retriable rejection
    instead of an unbounded stall or a silent skip of the gate."""
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    # Shrink the bound so this test itself stays fast; the mechanism under
    # test (wait_for + fail-closed-on-TimeoutError) is timeout-magnitude
    # independent.
    monkeypatch.setattr(sprint_tools_module, "_MERGE_VALIDATION_TIMEOUT_S", 0.2)

    repo = tmp_path / "repo-slow-git"
    repo.mkdir()
    _init_git_repo(str(repo))
    sha1 = _git_commit(str(repo), "a.txt", "one")
    monkeypatch.setattr(srv, "_REPO_ROOT", repo)

    p = await db_module.create_project(db, "wt-e2e-slowgit")
    session = await db_module.register_session(db, p["id"], "wt-e2e-slowgit-sess")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "e2e slow-git item", prospect_bypass=True,
    )
    await db_module.claim_sprint_item(db, p["id"], item["id"])
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/e2eslowgit1", ".", item_id=item["id"],
    )
    await db_module.persist_worktree_manifest(
        db, wt["id"], p["id"], session["id"], item["id"], "repo", "dev", sha1,
    )

    with patch(
        "meridian.worktree_merge_guard.subprocess.run",
        side_effect=_blocking_subprocess_run_stub(delay=0.5),
    ):
        start = time.monotonic()
        result = await srv._dispatch_mcp_tool(
            "complete_sprint_item",
            {"project_id": p["id"], "item_id": item["id"], "session_id": session["id"]},
            db, "/tmp",
        )
        elapsed = time.monotonic() - start

    assert result.get("error") == "WORKTREE_MERGE_VALIDATION_TIMEOUT"
    assert result["worktree_id"] == wt["id"]
    assert elapsed < 2.0, f"took {elapsed:.2f}s -- expected to fail closed near the 0.2s bound"

    reloaded = await db_module.get_sprint_item(db, item["id"])
    assert reloaded["status"] != "done"


# ---------------------------------------------------------------------------
# 3. Parallelizing get_worktree_head/is_worktree_dirty must not change which
#    errors fire when both a dirty tree AND a divergence are present at once.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worktree_merge_validation_parallel_checks_preserve_error_set(db, tmp_path):
    """DIRTY_WORKTREE and HEAD_MISMATCH must both still fire together when
    the worktree is simultaneously dirty AND diverged, now that
    get_worktree_head/is_worktree_dirty run concurrently (dcf78192) instead
    of strictly sequentially -- the dirty probe must not be silently
    dropped just because it no longer waits on head-resolution first."""
    repo = tmp_path / "repo-dirty-and-diverged"
    repo.mkdir()
    _init_git_repo(str(repo))
    sha1 = _git_commit(str(repo), "a.txt", "one")
    _git_commit(str(repo), "b.txt", "two")
    sha3 = _git_commit(str(repo), "c.txt", "three")  # HEAD when the manifest was written

    _run_git_cmd(["reset", "--hard", sha1], str(repo))
    _git_commit(str(repo), "d.txt", "four")  # new, unrelated-to-sha3 HEAD
    (repo / "uncommitted.txt").write_text("wip", encoding="utf-8")  # + dirty tree

    p = await db_module.create_project(db, "wt-merge-dirty-diverged")
    session = await db_module.register_session(db, p["id"], "wt-merge-dirty-diverged-sess")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/dirtydiverged1", ".",
    )
    await db_module.persist_worktree_manifest(
        db, wt["id"], p["id"], session["id"], None, "repo", "dev", sha3,
    )

    result = await _merge_guard_mod.validate_worktree_merge(db, repo, wt["id"])

    assert result["ok"] is False
    codes = {e["code"] for e in result["errors"]}
    assert codes == {"DIRTY_WORKTREE", "HEAD_MISMATCH"}, codes


# ---------------------------------------------------------------------------
# 4. Concurrent-stress test: N *distinct* items, real dispatch path, must
#    all land comfortably under the 45s dispatch budget with zero timeouts.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_completions_across_distinct_items_stay_under_dispatch_budget(
    db, tmp_path, monkeypatch,
):
    """The scenario the item's notes describe: fine solo, bad under
    concurrent load. N distinct items, each with its OWN session + active
    worktree + persisted manifest (the realistic "every session works in an
    isolated worktree" shape), completed CONCURRENTLY through the real
    server._dispatch_mcp_tool path -- not test_f291bb24_completion_
    concurrency.py's same-item race (already covered there) -- with each
    worktree's git calls given a small, realistic delay (simulating disk/
    subprocess contention across many concurrent worktrees) rather than
    mocking validate_worktree_merge away entirely.

    Asserts every completion: (a) finishes well under the 45s dispatch
    budget, (b) reaches completion_outcome='committed' with no error,
    (c) has a get_completion_attempt(correlation_id) phase history whose
    terminal phase is 'committed' -- zero COMPLETE_SPRINT_ITEM_TIMEOUT
    responses.
    """
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    monkeypatch.setattr(srv, "_REPO_ROOT", tmp_path)

    n = 8
    p = await db_module.create_project(db, "wt-stress-distinct-items")

    entries = []
    for i in range(n):
        session = await db_module.register_session(db, p["id"], f"stress-sess-{i}")
        item = await db_module.add_sprint_item(
            db, p["id"], "v1", f"stress item {i}", prospect_bypass=True, force=True,
        )
        await db_module.claim_sprint_item(db, p["id"], item["id"])
        wt = await db_module.register_worktree(
            db, session["id"], p["id"], f"worktree/stress{i}", ".", item_id=item["id"],
        )
        await db_module.persist_worktree_manifest(
            db, wt["id"], p["id"], session["id"], item["id"],
            "repo", "dev", "a" * 40,
        )
        entries.append((item["id"], session["id"], f"stress-corr-{i}"))

    with patch(
        "meridian.worktree_merge_guard.subprocess.run",
        # Realistic small per-call delay, not zero -- simulates genuine disk/
        # subprocess contention across many concurrently-active worktrees
        # without making the test itself slow.
        side_effect=_blocking_subprocess_run_stub(delay=0.05),
    ):
        start = time.monotonic()
        results = await asyncio.gather(*[
            srv._dispatch_mcp_tool(
                "complete_sprint_item",
                {
                    "project_id": p["id"], "item_id": item_id,
                    "session_id": session_id, "correlation_id": corr_id,
                },
                db, "/tmp",
            )
            for (item_id, session_id, corr_id) in entries
        ])
        elapsed = time.monotonic() - start

    # Comfortably under the 45s dispatch budget -- with the fixes in place
    # this should land in well under a second even with N=8 concurrent
    # worktree validations each doing 2 concurrent + 1 sequential git call.
    assert elapsed < 10.0, (
        f"{n} concurrent distinct-item completions took {elapsed:.2f}s -- "
        "expected comfortably under the 45s dispatch budget"
    )

    for (item_id, _session_id, corr_id), result in zip(entries, results):
        assert result.get("error") is None, result
        assert result["status"] == "done"
        assert result["completion_outcome"] == "committed"

        attempt = handler_module.get_completion_attempt(corr_id)
        assert attempt is not None, f"no completion-attempt record for {corr_id}"
        assert attempt["latest_phase"] == "committed", attempt
        assert result.get("error") != "COMPLETE_SPRINT_ITEM_TIMEOUT"

        reloaded = await db_module.get_sprint_item(db, item_id)
        assert reloaded["status"] == "done"
