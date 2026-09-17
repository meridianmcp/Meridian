"""71f597b7 (decision 9ce6420e) -- git-common-dir lockfile PreToolUse hook.

Extends worktree_guard.ps1/.sh (see tests/test_worktree_guard.py for the
pre-existing worktree-BOUNDARY behavior, which this change does not alter).

The boundary check alone stops a session from editing a file OUTSIDE its own
claimed worktree, but does nothing about two LIVE sessions that each stay
within their own worktree (or the main tree) and happen to edit the SAME
repo-relative file at the same time -- claim_file/claim_symbol are DB
bookkeeping with zero local-disk enforcement power. This tests the actual
hook BEHAVIOR added to close that gap:

1. First touch of a repo-relative path by a session with an attributable
   session_id creates a small JSON lockfile under this checkout's shared
   `git rev-parse --git-common-dir` and allows the call (exit 0).
2. The SAME session re-editing that path is always allowed (exit 0) and
   refreshes the lock's timestamp.
3. A DIFFERENT session touching the same path while the lock is live is
   BLOCKED (exit 2), naming the other session id.
4. A lock older than the 2-hour staleness threshold (mirrors
   meridian/db/locks.py's _FILE_LOCK_TTL_HOURS) is silently reclaimed rather
   than blocking forever.
5. The check applies to BOTH a worktree session and a main-tree session (the
   git-common-dir is shared by construction across every worktree of one
   clone, main tree included).
6. Fails open (never blocks) when session_id is absent, when git can't
   resolve a common dir, or on any lock-bookkeeping I/O error -- consistent
   with every other guard hook's fail-open philosophy in this repo.
7. The pre-existing worktree-boundary behavior (a3984d96) is completely
   unaffected: this section never fires when the boundary check already
   blocks (exit 2) or when the file lies outside the project dir entirely.

Both worktree_guard.sh (behaviorally tested here against a real `bash`) and
worktree_guard.ps1 (structurally verified everywhere, behaviorally tested
too when a real PowerShell is available) are covered -- kept in sync per
decision 9ce6420e's "update BOTH mirrors" instruction.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest


def _find_git_common_dir() -> Path:
    """Return this checkout's real shared git-common-dir, however this test
    file's own worktree happens to be laid out.

    FIXED during landing (2026-09-17): the original version of this helper
    (copied from tests/test_worktree_guard.py's own pre-existing pattern)
    guessed the main repo root by string-matching a `.claude/worktrees/<name>`
    path convention -- which only holds for THIS project's own
    Workflow-dispatched agent worktrees. It silently returns the wrong
    directory (this worktree's own root, whose `.git` is a plain FILE, not a
    directory) for any worktree at a different location -- confirmed live:
    a landing worktree under a scratch temp directory (not under
    `.claude/worktrees/`) made every `_lock_path_for` assertion fail, not
    because the hook was wrong, but because this test's own path-guessing
    heuristic was. decision 9ce6420e itself documents this repo having 19
    real worktrees, several NOT under `.claude/worktrees/` at all (e.g.
    `.codex/worktrees/c556`) -- so the guess was never fully general even
    for this repo's own topology.

    Fix: just ask git, the same way the hook itself does
    (`git -C "$project_dir" rev-parse --git-common-dir`, worktree_guard.sh
    line ~142) -- confirmed live this always returns an ABSOLUTE path for a
    linked worktree, correctly, for any worktree location, with zero
    path-guessing needed.
    """
    result = subprocess.run(
        ["git", "-C", str(Path(__file__).resolve().parent.parent), "rev-parse", "--git-common-dir"],
        capture_output=True, text=True, check=True,
    )
    return Path(result.stdout.strip()).resolve()


_WORKTREE_ROOT = Path(__file__).resolve().parent.parent
_HOOK_SH = _WORKTREE_ROOT / ".claude" / "hooks" / "worktree_guard.sh"
_HOOK_PS1 = _WORKTREE_ROOT / ".claude" / "hooks" / "worktree_guard.ps1"

# The actual checkout's shared git-common-dir -- every worktree of this clone
# (main tree included) resolves to the SAME directory, which is exactly the
# property under test. Locks land under here, in a dedicated subdirectory,
# and every test cleans up the specific lock file(s) it creates.
_GIT_COMMON_DIR = _find_git_common_dir()
_LOCK_ROOT = _GIT_COMMON_DIR / "meridian-locks"

# The main (non-worktree) tree's own working directory -- by git's own
# convention, `.git` always lives directly inside it, so this is simply the
# common-dir's parent. Used by the "does the lock check apply to a
# main-tree session too" scenarios, which need a real working-tree path
# (unlike _LOCK_ROOT, which is a `.git`-internal path).
_MAIN_TREE_ROOT = _GIT_COMMON_DIR.parent

# A SYNTHETIC worktree path matching the pre-existing a3984d96 boundary
# check's own detection convention (a `.claude/worktrees/<name>` substring --
# see worktree_guard.sh's own comment: "If CLAUDE_PROJECT_DIR does NOT
# contain '.claude/worktrees/' the session is in the main tree -- fail
# open"). That detection heuristic is a PRE-EXISTING, out-of-scope-for-71f597b7
# limitation (it doesn't recognize this repo's OTHER real worktree
# conventions either, e.g. `.codex/worktrees/` -- decision 9ce6420e's own
# text notes this). It means passing this test file's own REAL location as
# `claude_project_dir` only exercises the boundary check correctly when the
# test file happens to live under `.claude/worktrees/` itself. Mirrors
# tests/test_worktree_guard.py's own `_WORKTREE_DIR` fixture exactly, so the
# "boundary check still works unmodified" tests below verify the REAL
# detection convention regardless of where this test file itself is run
# from (a landing/review worktree elsewhere, an agent's own
# `.claude/worktrees/wf_*` worktree, or anywhere else).
_SYNTHETIC_WORKTREE_DIR = str(_MAIN_TREE_ROOT / ".claude" / "worktrees" / "test-fixture-worktree-lock")


def _find_git_capable_bash() -> str | None:
    """Prefer a git-capable bash over whatever `bash` happens to resolve to.

    A bare `shutil.which("bash")` can resolve to Windows' own WSL launcher
    stub (commonly C:\\WINDOWS\\system32\\bash.exe) ahead of a real Git-for-
    Windows install on PATH. Confirmed live in this repo's own dev
    environment: WSL's bundled Linux git cannot read this repo's (Windows-
    git-authored) worktree gitdir pointer at all -- "C:/..." is not a POSIX
    absolute path, so it gets mis-parsed as relative and `git -C <worktree>
    rev-parse --git-common-dir` silently fails, independent of anything this
    hook does. That is a pre-existing environment/git-binary interop gap
    outside this sprint item's scope (the hook already fails open when git
    can't resolve anything -- see test_lock_check_fails_open_when_git_unusable
    below), not a bug in the lock logic itself -- so prefer a bash whose git
    actually understands this checkout when one is available, the same way a
    developer's real Git-for-Windows install would be used in practice.
    """
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        shutil.which("bash"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


_BASH = _find_git_capable_bash()

_needs_bash = pytest.mark.skipif(
    not _HOOK_SH.exists() or not _BASH,
    reason="worktree_guard.sh or a usable bash is unavailable",
)

_POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe") or shutil.which("pwsh")
_needs_powershell = pytest.mark.skipif(
    not _HOOK_PS1.exists() or not _POWERSHELL,
    reason="worktree_guard.ps1 or a PowerShell interpreter is unavailable",
)

# Windows NTSTATUS crash exit codes seen under heavy xdist (-n auto) contention
# (same allowlist as test_worktree_guard.py, for the same reason).
_WIN_CRASH_CODES = frozenset(
    {
        0xC0000005 & 0xFFFFFFFF,
        0xC000007B & 0xFFFFFFFF,
        0xC0000135 & 0xFFFFFFFF,
        0xC0000142 & 0xFFFFFFFF,
        0xC000013A & 0xFFFFFFFF,
        3221225773,
    }
)


def _make_payload(tool: str, file_path: str, session_id: str | None) -> str:
    obj = {"tool_name": tool, "tool_input": {"file_path": file_path}}
    if session_id is not None:
        obj["session_id"] = session_id
    return json.dumps(obj)


def _lock_path_for(rel_name: str) -> Path:
    return _LOCK_ROOT / f"{rel_name}.lock"


def _cleanup_lock(rel_name: str) -> None:
    p = _lock_path_for(rel_name)
    try:
        p.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# bash (worktree_guard.sh) -- behavioral tests, primary deliverable.
# ---------------------------------------------------------------------------


def _run_sh_once(
    payload: str, *, claude_project_dir: str | None
) -> subprocess.CompletedProcess:
    if claude_project_dir is not None:
        safe = claude_project_dir.replace("'", "'\\''")
        setup = f"export CLAUDE_PROJECT_DIR='{safe}'; "
    else:
        setup = "unset CLAUDE_PROJECT_DIR; "
    cmd = setup + "exec bash .claude/hooks/worktree_guard.sh"
    r = subprocess.run(
        [_BASH, "-c", cmd],
        input=payload.encode("utf-8"),
        cwd=str(_WORKTREE_ROOT),
        capture_output=True,
        timeout=15,
    )
    return subprocess.CompletedProcess(
        r.args, r.returncode,
        stdout=(r.stdout or b"").decode("utf-8", "replace"),
        stderr=(r.stderr or b"").decode("utf-8", "replace"),
    )


def _run_sh(payload: str, *, claude_project_dir: str | None) -> subprocess.CompletedProcess:
    last: subprocess.CompletedProcess | None = None
    for _ in range(3):
        try:
            last = _run_sh_once(payload, claude_project_dir=claude_project_dir)
        except subprocess.TimeoutExpired:
            continue
        if last.returncode in _WIN_CRASH_CODES:
            continue
        return last
    assert last is not None, "bash never produced a result (all attempts crashed)"
    return last


@_needs_bash
def test_sh_first_touch_creates_lock_and_allows():
    rel = "_test_71f597b7_first_touch.py"
    _cleanup_lock(rel)
    try:
        target = str(_WORKTREE_ROOT / rel)
        payload = _make_payload("Edit", target, "session-A")
        r = _run_sh(payload, claude_project_dir=str(_WORKTREE_ROOT))
        assert r.returncode == 0, f"first touch must be allowed: {r.stderr}"

        lock = _lock_path_for(rel)
        assert lock.exists(), "first touch must create a lockfile under git-common-dir"
        data = json.loads(lock.read_text(encoding="utf-8"))
        assert data["session_id"] == "session-A"
        assert data["path"] == rel
    finally:
        _cleanup_lock(rel)


@_needs_bash
def test_sh_same_session_reedit_allowed_and_refreshes():
    rel = "_test_71f597b7_same_session.py"
    _cleanup_lock(rel)
    try:
        target = str(_WORKTREE_ROOT / rel)
        payload = _make_payload("Edit", target, "session-A")
        r1 = _run_sh(payload, claude_project_dir=str(_WORKTREE_ROOT))
        assert r1.returncode == 0
        lock = _lock_path_for(rel)
        first_mtime = lock.stat().st_mtime

        time.sleep(1.1)  # ensure a detectable mtime change on refresh
        r2 = _run_sh(payload, claude_project_dir=str(_WORKTREE_ROOT))
        assert r2.returncode == 0, "same session re-editing its own file must be allowed"
        assert lock.stat().st_mtime >= first_mtime, "refresh must not go backward"
    finally:
        _cleanup_lock(rel)


@_needs_bash
def test_sh_blocks_different_live_session():
    rel = "_test_71f597b7_cross_session.py"
    _cleanup_lock(rel)
    try:
        target = str(_WORKTREE_ROOT / rel)
        payload_a = _make_payload("Edit", target, "session-A")
        payload_b = _make_payload("Edit", target, "session-B")

        r1 = _run_sh(payload_a, claude_project_dir=str(_WORKTREE_ROOT))
        assert r1.returncode == 0

        r2 = _run_sh(payload_b, claude_project_dir=str(_WORKTREE_ROOT))
        assert r2.returncode == 2, "a different live session must be blocked"
        assert "71f597b7" in r2.stderr, "error message must cite the item id"
        assert "session-A" in r2.stderr, "error message must name the other session"
        assert rel in r2.stderr, "error message must name the repo-relative path"
    finally:
        _cleanup_lock(rel)


@_needs_bash
def test_sh_reclaims_stale_lock():
    rel = "_test_71f597b7_stale.py"
    _cleanup_lock(rel)
    try:
        target = str(_WORKTREE_ROOT / rel)
        payload_a = _make_payload("Edit", target, "session-A")
        r1 = _run_sh(payload_a, claude_project_dir=str(_WORKTREE_ROOT))
        assert r1.returncode == 0

        lock = _lock_path_for(rel)
        stale_time = time.time() - (3 * 60 * 60)  # 3h old, past the 2h threshold
        os.utime(lock, (stale_time, stale_time))

        payload_b = _make_payload("Edit", target, "session-B")
        r2 = _run_sh(payload_b, claude_project_dir=str(_WORKTREE_ROOT))
        assert r2.returncode == 0, "a stale lock must be silently reclaimed, not block"
        data = json.loads(lock.read_text(encoding="utf-8"))
        assert data["session_id"] == "session-B"
    finally:
        _cleanup_lock(rel)


@_needs_bash
def test_sh_fails_open_without_session_id():
    rel = "_test_71f597b7_no_session_id.py"
    _cleanup_lock(rel)
    try:
        target = str(_WORKTREE_ROOT / rel)
        payload = _make_payload("Edit", target, None)  # no session_id field at all
        r = _run_sh(payload, claude_project_dir=str(_WORKTREE_ROOT))
        assert r.returncode == 0, "must fail open when session_id is absent"
        assert not _lock_path_for(rel).exists(), "must not create a lock with no owner"
    finally:
        _cleanup_lock(rel)


@_needs_bash
def test_sh_lock_check_applies_to_main_tree_session_too():
    """A main-tree session (no .claude/worktrees/ marker) is unrestricted by
    the BOUNDARY check, but still participates in the SAME lock as a worktree
    session, since both share one git-common-dir. Uses a synthetic filename
    under the real main repo root -- it need not exist on disk."""
    rel = "_test_71f597b7_main_tree.py"
    _cleanup_lock(rel)
    try:
        target = str(_MAIN_TREE_ROOT / rel)
        payload_a = _make_payload("Edit", target, "session-A")
        r1 = _run_sh(payload_a, claude_project_dir=str(_MAIN_TREE_ROOT))
        assert r1.returncode == 0, "main-tree session's own first touch must be allowed"
        assert _lock_path_for(rel).exists()

        payload_b = _make_payload("Edit", target, "session-B")
        r2 = _run_sh(payload_b, claude_project_dir=str(_MAIN_TREE_ROOT))
        assert r2.returncode == 2, (
            "a second live session (even another main-tree session) touching "
            "the same repo-relative path must be blocked"
        )
    finally:
        _cleanup_lock(rel)


@_needs_bash
def test_sh_lock_check_fails_open_when_git_unusable():
    """CLAUDE_PROJECT_DIR pointing at a directory `git -C` can't resolve
    (not a real git worktree at all) must fail open on the LOCK check --
    never trap the executor over lock bookkeeping infrastructure."""
    rel = "_test_71f597b7_no_git.py"
    _cleanup_lock(rel)
    fake_dir = str(_WORKTREE_ROOT / ".claude" / "worktrees" / "not-a-real-worktree-71f597b7")
    try:
        target = fake_dir + "/" + rel
        payload = _make_payload("Edit", target, "session-A")
        r = _run_sh(payload, claude_project_dir=fake_dir)
        assert r.returncode == 0, "must fail open when git-common-dir cannot be resolved"
    finally:
        _cleanup_lock(rel)


@_needs_bash
def test_sh_boundary_block_takes_precedence_over_lock_check():
    """When the pre-existing worktree-boundary check already blocks (exit 2,
    a3984d96), the new lock section must never run at all -- confirmed by
    checking no lockfile gets created for the (rejected) main-tree path."""
    rel = "conftest.py"  # tests/conftest.py already exists in the main tree
    main_file = str(_MAIN_TREE_ROOT / "tests" / rel)
    _cleanup_lock(rel)
    try:
        payload = _make_payload("Edit", main_file, "session-A")
        r = _run_sh(payload, claude_project_dir=_SYNTHETIC_WORKTREE_DIR)
        assert r.returncode == 2
        assert "a3984d96" in r.stderr
        assert not _lock_path_for(rel).exists(), (
            "the lock section must not run when the boundary check already blocked"
        )
    finally:
        _cleanup_lock(rel)


# ---------------------------------------------------------------------------
# PowerShell (worktree_guard.ps1) -- structural + (when available) behavioral.
# ---------------------------------------------------------------------------


def test_ps1_contains_lock_logic_matching_sh():
    """Structural sync check: both mirrors must carry the same item id, the
    same git-common-dir resolution call, and the same staleness threshold --
    decision 9ce6420e requires keeping them behaviorally identical."""
    ps1_text = _HOOK_PS1.read_text(encoding="utf-8")
    sh_text = _HOOK_SH.read_text(encoding="utf-8")
    assert "71f597b7" in ps1_text and "71f597b7" in sh_text
    assert "rev-parse" in ps1_text and "rev-parse" in sh_text
    assert "--git-common-dir" in ps1_text and "--git-common-dir" in sh_text
    assert "meridian-locks" in ps1_text and "meridian-locks" in sh_text
    assert "session_id" in ps1_text and "session_id" in sh_text
    assert "staleThresholdHours = 2" in ps1_text
    assert "stale_threshold_secs=7200" in sh_text


def _run_ps1_once(
    payload: str, *, claude_project_dir: str | None
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if claude_project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = claude_project_dir
    else:
        env.pop("CLAUDE_PROJECT_DIR", None)
    r = subprocess.run(
        [_POWERSHELL, "-NoProfile", "-NonInteractive", "-File",
         ".claude\\hooks\\worktree_guard.ps1"],
        input=payload.encode("utf-8"),
        cwd=str(_WORKTREE_ROOT),
        capture_output=True,
        timeout=20,
        env=env,
    )
    return subprocess.CompletedProcess(
        r.args, r.returncode,
        stdout=(r.stdout or b"").decode("utf-8", "replace"),
        stderr=(r.stderr or b"").decode("utf-8", "replace"),
    )


def _run_ps1(payload: str, *, claude_project_dir: str | None) -> subprocess.CompletedProcess:
    last: subprocess.CompletedProcess | None = None
    for _ in range(3):
        try:
            last = _run_ps1_once(payload, claude_project_dir=claude_project_dir)
        except subprocess.TimeoutExpired:
            continue
        if last.returncode in _WIN_CRASH_CODES:
            continue
        return last
    assert last is not None, "powershell never produced a result (all attempts crashed)"
    return last


@_needs_powershell
def test_ps1_first_touch_then_cross_session_block_then_stale_reclaim():
    """One consolidated PowerShell behavioral test (acquire -> same-session
    refresh -> cross-session block -> stale reclaim) mirroring the separate
    bash tests above, to keep a real end-to-end check on the .ps1 mirror
    without spawning a PowerShell process per scenario."""
    rel = "_test_71f597b7_ps1_flow.py"
    _cleanup_lock(rel)
    try:
        target = str(_WORKTREE_ROOT / rel)
        payload_a = _make_payload("Edit", target, "session-A")
        payload_b = _make_payload("Edit", target, "session-B")

        r1 = _run_ps1(payload_a, claude_project_dir=str(_WORKTREE_ROOT))
        assert r1.returncode == 0, f"first touch must be allowed: {r1.stderr}"
        lock = _lock_path_for(rel)
        assert lock.exists()
        assert json.loads(lock.read_text(encoding="utf-8"))["session_id"] == "session-A"

        r2 = _run_ps1(payload_a, claude_project_dir=str(_WORKTREE_ROOT))
        assert r2.returncode == 0, "same session re-edit must be allowed"

        r3 = _run_ps1(payload_b, claude_project_dir=str(_WORKTREE_ROOT))
        assert r3.returncode == 2, "a different live session must be blocked"
        assert "71f597b7" in r3.stderr
        assert "session-A" in r3.stderr

        stale_time = time.time() - (3 * 60 * 60)
        os.utime(lock, (stale_time, stale_time))
        r4 = _run_ps1(payload_b, claude_project_dir=str(_WORKTREE_ROOT))
        assert r4.returncode == 0, "a stale lock must be reclaimed, not block forever"
        assert json.loads(lock.read_text(encoding="utf-8"))["session_id"] == "session-B"
    finally:
        _cleanup_lock(rel)


@_needs_powershell
def test_ps1_fails_open_without_session_id():
    rel = "_test_71f597b7_ps1_no_session.py"
    _cleanup_lock(rel)
    try:
        target = str(_WORKTREE_ROOT / rel)
        payload = _make_payload("Edit", target, None)
        r = _run_ps1(payload, claude_project_dir=str(_WORKTREE_ROOT))
        assert r.returncode == 0
        assert not _lock_path_for(rel).exists()
    finally:
        _cleanup_lock(rel)


@_needs_powershell
def test_ps1_boundary_block_still_works_unmodified():
    """The pre-existing a3984d96 boundary behavior must be byte-for-byte
    unaffected by this change when run through real PowerShell."""
    main_file = str(_MAIN_TREE_ROOT / "tests" / "conftest.py")
    payload = _make_payload("Edit", main_file, "session-A")
    r = _run_ps1(payload, claude_project_dir=_SYNTHETIC_WORKTREE_DIR)
    assert r.returncode == 2
    assert "a3984d96" in r.stderr
