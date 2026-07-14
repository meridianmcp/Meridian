"""a3984d96 -- PreToolUse worktree guard structurally blocks file-edit tool calls
when the target path is outside the session's claimed worktree.

Near-miss incident: an executor accidentally edited tests/conftest.py in the main repo
tree instead of its own worktree, caught only by luck. This tests the ACTUAL hook
BEHAVIOR:

1. The hook (worktree_guard.sh via bash) -- primary deliverable.
   - Blocks Edit/Write/MultiEdit/NotebookEdit (exit 2) when CLAUDE_PROJECT_DIR is a
     worktree path (.claude/worktrees/<name>/) and the target file_path is NOT under
     that worktree.
   - Fails open (exit 0) when CLAUDE_PROJECT_DIR is the main tree (no worktrees marker).
   - Fails open (exit 0) when CLAUDE_PROJECT_DIR is not set.
   - Fails open (exit 0) when file_path IS under the claimed worktree.
   - Fails open (exit 0) on any other tool (passthrough).
   - Fails open (exit 0) on garbage/missing stdin.

2. settings.json actually registers the hook under PreToolUse with matcher
   "Edit|Write|MultiEdit|NotebookEdit" -- structural wiring, not just file presence.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

def _find_main_repo() -> Path:
    """Return the main repo root even when this test file lives inside a worktree.

    Worktrees sit at {main_repo}/.claude/worktrees/{name}/.  If this file
    resolves to a path that matches that pattern, walk 3 levels up to reach
    the main repo.  Otherwise, assume we are already in the main repo.
    """
    here = Path(__file__).resolve()
    # here is tests/test_worktree_guard.py
    candidate = here.parent.parent  # one level above tests/
    parts = candidate.parts
    # Check for the pattern .../.claude/worktrees/<name>
    try:
        wt_idx = next(
            i for i, p in enumerate(parts) if p == ".claude"
            and i + 1 < len(parts) and parts[i + 1] == "worktrees"
        )
        # candidate is the worktree root; main repo is 3 levels up
        return Path(*parts[:wt_idx])
    except StopIteration:
        return candidate  # already in the main repo


_REPO = _find_main_repo()
_WORKTREE_ROOT = Path(__file__).resolve().parent.parent  # this worktree
_HOOK_SH = _WORKTREE_ROOT / ".claude" / "hooks" / "worktree_guard.sh"
# settings.json lives next to the hook; use _WORKTREE_ROOT's copy (which is the
# one this sprint item modifies) when running inside a worktree, otherwise the
# main repo copy.
_SETTINGS = _WORKTREE_ROOT / ".claude" / "settings.json"

# A real worktree path that exists in this repo (the one we're running in).
_WORKTREE_DIR = str(_WORKTREE_ROOT)
# A file inside the worktree (in the worktree's own subdirectory).
_WORKTREE_FILE = str(_WORKTREE_ROOT / "meridian" / "server.py")
# A file in the main tree (NOT in any worktree).
_MAIN_FILE = str(_REPO / "tests" / "conftest.py")
# A file in a sibling worktree (different from this one).
_OTHER_WORKTREE_FILE = str(
    _REPO / ".claude" / "worktrees" / "wf_a1d5f1db-630-1" / "meridian" / "server.py"
)

_needs_bash = pytest.mark.skipif(
    not _HOOK_SH.exists() or shutil.which("bash") is None,
    reason="worktree_guard.sh or bash unavailable",
)

# Windows NTSTATUS crash exit codes seen under heavy xdist (-n auto) contention.
_WIN_CRASH_CODES = frozenset(
    {
        0xC0000005 & 0xFFFFFFFF,  # ACCESS_VIOLATION
        0xC000007B & 0xFFFFFFFF,  # INVALID_IMAGE_FORMAT
        0xC0000135 & 0xFFFFFFFF,  # DLL_NOT_FOUND
        0xC0000142 & 0xFFFFFFFF,  # DLL_INIT_FAILED
        0xC000013A & 0xFFFFFFFF,  # CONTROL_C_EXIT / kill
        3221225773,               # observed under -n auto
    }
)


def _run_hook_once(
    payload: str, *, claude_project_dir: str | None = None
) -> subprocess.CompletedProcess:
    """Run worktree_guard.sh from cwd=worktree root.

    MSYS2/Git-Bash path conversion mangles Windows paths set via subprocess env=dict
    (the colon in C:/ gets treated as a PATH separator).  The workaround -- used by
    test_code_intel_guard.py for MERIDIAN_URL -- is to export the variable INSIDE
    the bash command string so the shell sees it as a literal string, not an env var
    passed through MSYS path conversion.  When claude_project_dir is None we
    explicitly unset CLAUDE_PROJECT_DIR inside bash.
    """
    if claude_project_dir is not None:
        # Single-quote the value so bash takes it verbatim (no expansion, no conversion).
        # Use Python's replace to escape any single-quotes inside the path (rare).
        safe = claude_project_dir.replace("'", "'\\''")
        setup = f"export CLAUDE_PROJECT_DIR='{safe}'; "
    else:
        setup = "unset CLAUDE_PROJECT_DIR; "

    # The hook lives in _WORKTREE_ROOT/.claude/hooks/, use _WORKTREE_ROOT as cwd.
    cmd = setup + "exec bash .claude/hooks/worktree_guard.sh"
    r = subprocess.run(
        ["bash", "-c", cmd],
        input=payload.encode("utf-8"),
        cwd=str(_WORKTREE_ROOT),
        capture_output=True,
        timeout=15,
    )
    return subprocess.CompletedProcess(
        r.args,
        r.returncode,
        stdout=(r.stdout or b"").decode("utf-8", "replace"),
        stderr=(r.stderr or b"").decode("utf-8", "replace"),
    )


def _run_hook(
    payload: str, *, claude_project_dir: str | None = None
) -> subprocess.CompletedProcess:
    """Retry on Windows subprocess-teardown crashes (harness artifact, not hook)."""
    last: subprocess.CompletedProcess | None = None
    for _ in range(3):
        try:
            last = _run_hook_once(payload, claude_project_dir=claude_project_dir)
        except subprocess.TimeoutExpired:
            continue
        if last.returncode in _WIN_CRASH_CODES:
            continue
        return last
    assert last is not None, "git-bash never produced a result (all attempts crashed)"
    return last


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_payload(tool: str, file_path: str) -> str:
    return json.dumps({"tool_name": tool, "tool_input": {"file_path": file_path}})


# ---------------------------------------------------------------------------
# Tests: fails open when no CLAUDE_PROJECT_DIR
# ---------------------------------------------------------------------------

@_needs_bash
@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_hook_fails_open_when_no_project_dir(tool):
    """Without CLAUDE_PROJECT_DIR the hook can't know the context -- must fail open."""
    payload = _make_payload(tool, _MAIN_FILE)
    r = _run_hook(payload, claude_project_dir=None)
    assert r.returncode == 0, f"{tool}: must fail open when CLAUDE_PROJECT_DIR is absent"


# ---------------------------------------------------------------------------
# Tests: fails open when session is in the main tree (not a worktree)
# ---------------------------------------------------------------------------

@_needs_bash
@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_hook_fails_open_when_main_tree_session(tool):
    """Main-tree session (CLAUDE_PROJECT_DIR has no .claude/worktrees/) -- fail open."""
    payload = _make_payload(tool, _MAIN_FILE)
    r = _run_hook(payload, claude_project_dir=str(_REPO))
    assert r.returncode == 0, f"{tool}: main-tree session must not be restricted"


# ---------------------------------------------------------------------------
# Tests: fails open when file IS inside the claimed worktree
# ---------------------------------------------------------------------------

@_needs_bash
@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_hook_allows_edit_inside_claimed_worktree(tool):
    """File inside CLAUDE_PROJECT_DIR worktree -- must be allowed (exit 0)."""
    payload = _make_payload(tool, _WORKTREE_FILE)
    r = _run_hook(payload, claude_project_dir=_WORKTREE_DIR)
    assert r.returncode == 0, f"{tool}: edits inside own worktree must be allowed"


# ---------------------------------------------------------------------------
# Tests: blocks when file is in the main tree (worktree session)
# ---------------------------------------------------------------------------

@_needs_bash
@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_hook_blocks_edit_of_main_tree_file_from_worktree_session(tool):
    """Worktree session editing a file in the main tree -- must block (exit 2)."""
    payload = _make_payload(tool, _MAIN_FILE)
    r = _run_hook(payload, claude_project_dir=_WORKTREE_DIR)
    assert r.returncode == 2, (
        f"{tool}: editing main-tree file from a worktree session must be blocked"
    )
    assert "a3984d96" in r.stderr, "error message must cite the item id"
    assert tool in r.stderr, "error message must name the blocked tool"
    assert _WORKTREE_DIR in r.stderr or ".claude/worktrees/" in r.stderr, (
        "error message must name the expected worktree"
    )


# ---------------------------------------------------------------------------
# Tests: blocks when file is in a different worktree
# ---------------------------------------------------------------------------

@_needs_bash
@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_hook_blocks_edit_of_sibling_worktree_file(tool):
    """Worktree session editing a file in a sibling worktree -- must block (exit 2)."""
    payload = _make_payload(tool, _OTHER_WORKTREE_FILE)
    r = _run_hook(payload, claude_project_dir=_WORKTREE_DIR)
    assert r.returncode == 2, (
        f"{tool}: editing a sibling worktree's file must be blocked"
    )
    assert "a3984d96" in r.stderr


# ---------------------------------------------------------------------------
# Tests: passes through all non-edit tools
# ---------------------------------------------------------------------------

@_needs_bash
@pytest.mark.parametrize(
    "tool",
    ["Bash", "Read", "Grep", "Glob", "AskUserQuestion", "find_symbol",
     "search_graph", "mcp__meridian__claim_file"],
)
def test_hook_allows_all_non_edit_tools(tool):
    """Non-edit tools must never be blocked by the worktree guard."""
    payload = _make_payload(tool, _MAIN_FILE)
    r = _run_hook(payload, claude_project_dir=_WORKTREE_DIR)
    assert r.returncode == 0, f"{tool} must not be blocked by the worktree guard"


# ---------------------------------------------------------------------------
# Tests: fails open on garbage / missing stdin
# ---------------------------------------------------------------------------

@_needs_bash
@pytest.mark.parametrize("payload", ["", "not json at all", "{}", '{"foo":"bar"}'])
def test_hook_fails_open_on_unparseable(payload):
    """Malformed or missing stdin: hook must never trap the executor."""
    r = _run_hook(payload, claude_project_dir=_WORKTREE_DIR)
    assert r.returncode == 0, "must fail open on unparseable payload"


# ---------------------------------------------------------------------------
# Tests: path prefix edge cases
# ---------------------------------------------------------------------------

@_needs_bash
def test_hook_does_not_false_positive_on_prefix_name():
    """Worktree 'wf_foo' must not allow edits to 'wf_foo_extra' (a different tree)."""
    # Simulate a worktree whose name is a prefix of another: wf_abc vs wf_abcdef
    fake_worktree = str(_REPO / ".claude" / "worktrees" / "wf_abc")
    # A file that is in wf_abcdef (not wf_abc) -- should be blocked
    other_file = str(_REPO / ".claude" / "worktrees" / "wf_abcdef" / "some_file.py")
    payload = _make_payload("Edit", other_file)
    r = _run_hook(payload, claude_project_dir=fake_worktree)
    assert r.returncode == 2, (
        "must not allow prefix-name match (wf_abcdef is not inside wf_abc)"
    )


@_needs_bash
def test_hook_allows_windows_style_paths():
    """Backslash-separated file_path should be normalized and allowed if inside worktree."""
    # Construct a backslash path to a file inside the worktree.
    win_path = _WORKTREE_FILE.replace("/", "\\")
    payload = _make_payload("Edit", win_path)
    r = _run_hook(payload, claude_project_dir=_WORKTREE_DIR)
    assert r.returncode == 0, "Windows-style paths inside worktree must be allowed"


@_needs_bash
def test_hook_blocks_windows_style_path_outside_worktree():
    """Backslash-separated file_path pointing to the main tree must still be blocked."""
    win_path = _MAIN_FILE.replace("/", "\\")
    payload = _make_payload("Write", win_path)
    r = _run_hook(payload, claude_project_dir=_WORKTREE_DIR)
    assert r.returncode == 2, (
        "Windows-style paths outside the worktree must still be blocked"
    )


# ---------------------------------------------------------------------------
# Test: settings.json actually wires the guard
# ---------------------------------------------------------------------------

def test_settings_wires_edit_write_matcher():
    """The hook must be registered -- structural wiring, not just file presence."""
    cfg = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    pre = cfg.get("hooks", {}).get("PreToolUse", [])
    entry = next(
        (e for e in pre if e.get("matcher") == "Edit|Write|MultiEdit|NotebookEdit"), None
    )
    assert entry is not None, "PreToolUse must have an Edit|Write|MultiEdit|NotebookEdit matcher entry"
    cmds = " ".join(h.get("command", "") for h in entry.get("hooks", []))
    assert "worktree_guard" in cmds, (
        "the Edit|Write matcher must run worktree_guard"
    )
