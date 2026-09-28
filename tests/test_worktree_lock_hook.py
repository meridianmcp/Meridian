"""71f597b7 (decision 9ce6420e), revised by 55d48d69 fix round 1 -- the same-file lock
in worktree_guard.ps1/.sh.

History: the lock was keyed on the repo-relative path in the clone's shared
``git rev-parse --git-common-dir``, lived 2 hours, was never released when a
session ended, and blocked (exit 2). That was harmless while every registered
PowerShell hook failed to start; once the launcher fix (9a4442a1) made the exit 2
real, the verification run showed it blocking parallel worktree agents editing their
OWN copies of a file, a new session after /clear, and any session after a finished
one -- with only /hooks or disableAllHooks as an escape.

The revised contract (tested here against a throwaway git repository with linked
worktrees -- nothing is written into this checkout's own .git):

1. The lock lives under THIS CHECKOUT's own git dir (``git rev-parse --git-dir``):
   the main tree's ``.git``, or ``.git/worktrees/<name>`` for a linked worktree. So
   sessions in different worktrees (or a worktree and the main tree) never see each
   other's locks.
2. It is WARN-ONLY: another session's edit of the same file in the same working
   tree within the last 15 minutes yields exit 0 plus an additionalContext warning
   naming that session; the edit is never blocked.
3. The same session re-editing is silent; an older lock is taken over silently.
4. No session_id / no usable git -> nothing recorded, exit 0.
5. The worktree-BOUNDARY block (a3984d96) is unchanged and runs first.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HOOK_SH = _REPO / ".claude" / "hooks" / "worktree_guard.sh"
_HOOK_PS1 = _REPO / ".claude" / "hooks" / "worktree_guard.ps1"

pytestmark = [pytest.mark.subprocess_isolated, pytest.mark.timeout(400)]


def _find_git_capable_bash() -> str | None:
    """Prefer Git for Windows' bash over the WSL launcher stub (whose Linux git
    cannot read a Windows-authored worktree gitdir)."""
    for c in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe", shutil.which("bash")):
        if c and Path(c).exists():
            return c
    return None


_BASH = _find_git_capable_bash()
_POWERSHELL = (shutil.which("powershell") or shutil.which("powershell.exe")) if os.name == "nt" else None
_WIN_CRASH_CODES = frozenset({0xC0000005, 0xC000007B, 0xC0000135, 0xC0000142, 0xC000013A, 3221225773})

SHELLS = [
    pytest.param("sh", marks=pytest.mark.skipif(not _BASH or shutil.which("git") is None, reason="bash/git unavailable")),
    pytest.param("ps1", marks=pytest.mark.skipif(not _POWERSHELL or shutil.which("git") is None,
                                                 reason="Windows PowerShell/git unavailable")),
]


def _git(*args: str, cwd: Path) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, timeout=60)
    return r.stdout.strip()


@pytest.fixture
def clone(tmp_path):
    """<tmp>/wt repo/main (a space in the path on purpose) + two linked worktrees."""
    main = tmp_path / "wt repo" / "main"
    main.mkdir(parents=True)
    _git("init", "-q", cwd=main)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init", cwd=main)
    wts = {}
    for name in ("wt1", "wt2"):
        wt = main / ".claude" / "worktrees" / name
        _git("worktree", "add", "-q", "--detach", str(wt), cwd=main)
        wts[name] = wt
    return main, wts


def _payload(tool: str, file_path: str, session_id: str | None) -> str:
    obj = {"tool_name": tool, "tool_input": {"file_path": file_path}}
    if session_id is not None:
        obj["session_id"] = session_id
    return json.dumps(obj)


def _run(shell: str, payload: str, project_dir: str, tmp_path: Path) -> subprocess.CompletedProcess:
    keep = ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "PATHEXT", "COMSPEC", "MSYSTEM")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update({"CLAUDE_PROJECT_DIR": project_dir, "USERPROFILE": str(tmp_path / "home"), "HOME": str(tmp_path / "home"),
                "LOCALAPPDATA": str(tmp_path / "lad"), "TEMP": str(tmp_path / "tmp"), "TMP": str(tmp_path / "tmp")})
    argv = ([_BASH, str(_HOOK_SH)] if shell == "sh" else
            [_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(_HOOK_PS1)])
    last = None
    for _ in range(3):
        try:
            last = subprocess.run(argv, input=payload.encode("utf-8"), capture_output=True, env=env, timeout=60)
        except subprocess.TimeoutExpired:
            continue
        if (last.returncode & 0xFFFFFFFF) in _WIN_CRASH_CODES:
            continue
        return subprocess.CompletedProcess(argv, last.returncode, last.stdout.decode("utf-8", "replace"),
                                           last.stderr.decode("utf-8", "replace"))
    pytest.skip("the shell never completed (host contention)")


def _warning(r: subprocess.CompletedProcess) -> str | None:
    if not r.stdout.strip():
        return None
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def _git_dir(checkout: Path) -> Path:
    raw = _git("rev-parse", "--git-dir", cwd=checkout)
    p = Path(raw)
    return (p if p.is_absolute() else checkout / p).resolve()


@pytest.mark.parametrize("shell", SHELLS)
def test_lock_lives_in_the_checkouts_own_git_dir(shell, clone, tmp_path):
    main, wts = clone
    for checkout in (main, wts["wt1"]):
        r = _run(shell, _payload("Edit", str(checkout / "pkg" / "a.py"), "session-A"), str(checkout), tmp_path)
        assert r.returncode == 0 and not r.stdout.strip(), r.stderr
        lock = _git_dir(checkout) / "meridian-locks" / "pkg" / "a.py.lock"
        assert lock.is_file(), lock
        data = json.loads(lock.read_text(encoding="utf-8"))
        assert data["session_id"] == "session-A" and data["path"] == "pkg/a.py"
    assert _git_dir(wts["wt1"]) != _git_dir(main)


@pytest.mark.parametrize("shell", SHELLS)
def test_other_session_same_checkout_is_warned_not_blocked(shell, clone, tmp_path):
    main, _wts = clone
    target = str(main / "pkg" / "b.py")
    assert _run(shell, _payload("Edit", target, "session-A"), str(main), tmp_path).returncode == 0
    r = _run(shell, _payload("Edit", target, "session-A"), str(main), tmp_path)
    assert r.returncode == 0 and _warning(r) is None, "the same session re-editing is silent"
    r = _run(shell, _payload("Edit", target, "session-B"), str(main), tmp_path)
    assert r.returncode == 0, "never exit 2"
    w = _warning(r) or ""
    assert "71f597b7" in w and "session-A" in w and "pkg/b.py" in w and "warning only" in w
    lock = _git_dir(main) / "meridian-locks" / "pkg" / "b.py.lock"
    assert json.loads(lock.read_text(encoding="utf-8"))["session_id"] == "session-B", "the warned edit takes the lock"


@pytest.mark.parametrize("shell", SHELLS)
def test_sibling_worktrees_never_see_each_others_locks(shell, clone, tmp_path):
    _main, wts = clone
    r = _run(shell, _payload("Edit", str(wts["wt1"] / "tests" / "conftest.py"), "S-wt1"), str(wts["wt1"]), tmp_path)
    assert r.returncode == 0
    r = _run(shell, _payload("Edit", str(wts["wt2"] / "tests" / "conftest.py"), "S-wt2"), str(wts["wt2"]), tmp_path)
    assert r.returncode == 0 and _warning(r) is None, r.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_old_lock_is_taken_over_silently(shell, clone, tmp_path):
    main, _wts = clone
    target = str(main / "c.py")
    assert _run(shell, _payload("Edit", target, "session-A"), str(main), tmp_path).returncode == 0
    lock = _git_dir(main) / "meridian-locks" / "c.py.lock"
    old = time.time() - 16 * 60  # past the 15 minute window
    os.utime(lock, (old, old))
    r = _run(shell, _payload("Edit", target, "session-after-clear"), str(main), tmp_path)
    assert r.returncode == 0 and _warning(r) is None
    assert json.loads(lock.read_text(encoding="utf-8"))["session_id"] == "session-after-clear"


@pytest.mark.parametrize("shell", SHELLS)
def test_no_session_id_or_no_git_records_nothing(shell, clone, tmp_path):
    main, _wts = clone
    r = _run(shell, _payload("Edit", str(main / "d.py"), None), str(main), tmp_path)
    assert r.returncode == 0
    assert not (_git_dir(main) / "meridian-locks" / "d.py.lock").exists()
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    r = _run(shell, _payload("Edit", str(plain / "e.py"), "session-A"), str(plain), tmp_path)
    assert r.returncode == 0 and not r.stdout.strip()


@pytest.mark.parametrize("shell", SHELLS)
def test_boundary_block_runs_first_and_records_no_lock(shell, clone, tmp_path):
    main, wts = clone
    r = _run(shell, _payload("Edit", str(main / "tests" / "conftest.py"), "session-A"), str(wts["wt1"]), tmp_path)
    assert r.returncode == 2 and "a3984d96" in r.stderr
    assert not (_git_dir(wts["wt1"]) / "meridian-locks").exists()
    assert not (_git_dir(main) / "meridian-locks").exists()


def test_ps1_and_sh_carry_the_same_lock_contract():
    ps1_text = _HOOK_PS1.read_text(encoding="utf-8")
    sh_text = _HOOK_SH.read_text(encoding="utf-8")
    for needle in ("71f597b7", "rev-parse", "--git-dir", "meridian-locks", "session_id", "warning only"):
        assert needle in ps1_text and needle in sh_text, needle
    assert "--git-common-dir" not in ps1_text.split("$ErrorActionPreference", 1)[1]
    assert "--git-common-dir" not in sh_text.split("set -uo pipefail", 1)[1]
    assert "$windowMinutes = 15" in ps1_text and "window_secs=900" in sh_text
    assert "exit 2" not in ps1_text.split("per-checkout same-file lock", 1)[1], "the lock section never blocks"
    assert "exit 2" not in sh_text.split("per-checkout same-file lock", 1)[1], "the lock section never blocks"
