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

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

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
_WORKTREE_ROOT = Path(__file__).resolve().parent.parent  # actual cwd this test runs from
_HOOK_SH = _WORKTREE_ROOT / ".claude" / "hooks" / "worktree_guard.sh"
# settings.json lives next to the hook; use _WORKTREE_ROOT's copy (which is the
# one this sprint item modifies) when running inside a worktree, otherwise the
# main repo copy.
_SETTINGS = _WORKTREE_ROOT / ".claude" / "settings.json"

# A SIMULATED claimed-worktree path for the hook's CLAUDE_PROJECT_DIR input. This must
# be synthetic and independent of _WORKTREE_ROOT (where the test file itself physically
# lives) -- deriving it from _WORKTREE_ROOT made these tests pass only when run from
# inside a real .claude/worktrees/<name>/ checkout and silently fail-open (false pass on
# the "allow" tests, hard fail on the "block" tests) once merged into the main tree,
# since _WORKTREE_ROOT there has no .claude/worktrees/ segment. The hook only does
# string matching, not filesystem existence checks, so this path need not exist.
_WORKTREE_DIR = str(_REPO / ".claude" / "worktrees" / "test-fixture-worktree")
# A file inside the simulated worktree.
_WORKTREE_FILE = str(Path(_WORKTREE_DIR) / "meridian" / "server.py")
# A file in the main tree (NOT in any worktree).
_MAIN_FILE = str(_REPO / "tests" / "conftest.py")
# A file in a sibling worktree (different from this one, and not tied to any specific
# transient batch/workflow worktree name).
_OTHER_WORKTREE_FILE = str(
    _REPO / ".claude" / "worktrees" / "test-fixture-sibling-worktree" / "meridian" / "server.py"
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


# ===========================================================================
# eb2e44f8 -- immutable wave base manifests + merge-time / cleanup-time
# validation guard. Unlike the bash-hook tests above (a3984d96, which guard
# EDIT tool calls against a claimed worktree), this section guards the
# server-side worktree lifecycle itself: the base manifest persisted per
# worktree, the pre-merge/completion validation checked against it, and the
# path/PID liveness gate that runs before a worktree directory is ever
# deleted from disk. Placed in this same file because it is, at heart, the
# same category of protection -- "don't let a worktree operation act on the
# wrong target" -- just enforced in Python against real state instead of in
# bash against a JSON tool-call payload.
# ===========================================================================

from meridian import db as db_module
from meridian import worktree_cleanup as _wt_cleanup_mod
from meridian import worktree_merge_guard as _merge_guard_mod


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


# ---------------------------------------------------------------------------
# 1. Immutable manifest persistence (db.worktree_manifest)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_worktree_manifest_stores_all_fields(db):
    p = await db_module.create_project(db, "wt-manifest-basic")
    session = await db_module.register_session(db, p["id"], "wt-manifest-sess")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "manifest item", prospect_bypass=True)
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/manifest1", ".claude/worktrees/manifest1",
        item_id=item["id"],
    )

    manifest = await db_module.persist_worktree_manifest(
        db, wt["id"], p["id"], session["id"], item["id"],
        "meridian-repo", "dev", "a" * 40,
    )
    assert manifest["worktree_id"] == wt["id"]
    assert manifest["repo_identity"] == "meridian-repo"
    assert manifest["base_branch"] == "dev"
    assert manifest["base_sha"] == "a" * 40
    assert manifest["item_id"] == item["id"]
    assert manifest["superseded_at"] is None

    fetched = await db_module.get_worktree_manifest(db, wt["id"])
    assert fetched["id"] == manifest["id"]


@pytest.mark.asyncio
async def test_persist_worktree_manifest_is_immutable_without_force(db):
    """A second persist for the SAME worktree_id must be rejected, not
    silently overwrite the first -- the core acceptance criterion."""
    p = await db_module.create_project(db, "wt-manifest-immutable")
    session = await db_module.register_session(db, p["id"], "wt-manifest-immutable-sess")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/immutable1", ".claude/worktrees/immutable1",
    )
    await db_module.persist_worktree_manifest(
        db, wt["id"], p["id"], session["id"], None, "repo-a", "dev", "a" * 40,
    )

    with pytest.raises(ValueError):
        await db_module.persist_worktree_manifest(
            db, wt["id"], p["id"], session["id"], None, "repo-b", "dev", "b" * 40,
        )

    # The original manifest must be untouched.
    still_active = await db_module.get_worktree_manifest(db, wt["id"])
    assert still_active["repo_identity"] == "repo-a"
    assert still_active["base_sha"] == "a" * 40


@pytest.mark.asyncio
async def test_persist_worktree_manifest_force_supersedes_with_audit_trail(db):
    """force=True performs an explicit, AUDITED replacement -- the old row
    is marked superseded (with a reason), never deleted."""
    p = await db_module.create_project(db, "wt-manifest-supersede")
    session = await db_module.register_session(db, p["id"], "wt-manifest-supersede-sess")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/supersede1", ".claude/worktrees/supersede1",
    )
    first = await db_module.persist_worktree_manifest(
        db, wt["id"], p["id"], session["id"], None, "repo-a", "dev", "a" * 40,
    )
    second = await db_module.persist_worktree_manifest(
        db, wt["id"], p["id"], session["id"], None, "repo-a", "dev", "c" * 40,
        force=True, reason="worktree was reset to a new base",
    )
    assert second["id"] != first["id"]
    assert second["base_sha"] == "c" * 40

    active = await db_module.get_worktree_manifest(db, wt["id"])
    assert active["id"] == second["id"]

    history = await db_module.get_worktree_manifest_history(db, wt["id"])
    assert len(history) == 2
    by_id = {row["id"]: row for row in history}
    assert by_id[first["id"]]["superseded_at"] is not None
    assert by_id[first["id"]]["superseded_reason"] == "worktree was reset to a new base"
    assert by_id[second["id"]]["superseded_at"] is None


# ---------------------------------------------------------------------------
# 2 + 3. validate_worktree_merge -- pre-merge/completion validation gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_worktree_merge_ok_when_clean_and_ancestor(db, tmp_path):
    repo = tmp_path / "repo-ok"
    repo.mkdir()
    _init_git_repo(str(repo))
    sha1 = _git_commit(str(repo), "a.txt", "one")
    sha2 = _git_commit(str(repo), "b.txt", "two")

    p = await db_module.create_project(db, "wt-merge-ok")
    session = await db_module.register_session(db, p["id"], "wt-merge-ok-sess")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/ok1", ".",
    )
    await db_module.persist_worktree_manifest(
        db, wt["id"], p["id"], session["id"], None, "repo", "dev", sha1,
    )

    result = await _merge_guard_mod.validate_worktree_merge(db, repo, wt["id"])
    assert result["ok"] is True, result["errors"]
    assert result["head_sha"] == sha2
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_validate_worktree_merge_rejects_dirty_worktree(db, tmp_path):
    repo = tmp_path / "repo-dirty"
    repo.mkdir()
    _init_git_repo(str(repo))
    sha1 = _git_commit(str(repo), "a.txt", "one")
    # Uncommitted change -- never committed.
    (repo / "uncommitted.txt").write_text("wip", encoding="utf-8")

    p = await db_module.create_project(db, "wt-merge-dirty")
    session = await db_module.register_session(db, p["id"], "wt-merge-dirty-sess")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/dirty1", ".",
    )
    await db_module.persist_worktree_manifest(
        db, wt["id"], p["id"], session["id"], None, "repo", "dev", sha1,
    )

    result = await _merge_guard_mod.validate_worktree_merge(db, repo, wt["id"])
    assert result["ok"] is False
    codes = {e["code"] for e in result["errors"]}
    assert "DIRTY_WORKTREE" in codes


@pytest.mark.asyncio
async def test_validate_worktree_merge_rejects_head_mismatch_after_divergence(db, tmp_path):
    """base_sha recorded at manifest time must be an ancestor of current
    HEAD. A reset that abandons the recorded base (simulating a rebase or
    hard reset) must be rejected."""
    repo = tmp_path / "repo-diverged"
    repo.mkdir()
    _init_git_repo(str(repo))
    sha1 = _git_commit(str(repo), "a.txt", "one")
    _git_commit(str(repo), "b.txt", "two")
    sha3 = _git_commit(str(repo), "c.txt", "three")  # this was HEAD when the manifest was written

    # Simulate a hard reset that abandons sha3's line of history entirely.
    _run_git_cmd(["reset", "--hard", sha1], str(repo))
    sha4 = _git_commit(str(repo), "d.txt", "four")  # new, unrelated-to-sha3 HEAD

    p = await db_module.create_project(db, "wt-merge-diverged")
    session = await db_module.register_session(db, p["id"], "wt-merge-diverged-sess")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/diverged1", ".",
    )
    await db_module.persist_worktree_manifest(
        db, wt["id"], p["id"], session["id"], None, "repo", "dev", sha3,
    )

    result = await _merge_guard_mod.validate_worktree_merge(db, repo, wt["id"])
    assert result["ok"] is False
    assert result["head_sha"] == sha4
    codes = {e["code"] for e in result["errors"]}
    assert "HEAD_MISMATCH" in codes


@pytest.mark.asyncio
async def test_validate_worktree_merge_rejects_stale_manifest(db, tmp_path):
    repo = tmp_path / "repo-stale"
    repo.mkdir()
    _init_git_repo(str(repo))
    sha1 = _git_commit(str(repo), "a.txt", "one")

    p = await db_module.create_project(db, "wt-merge-stale")
    session = await db_module.register_session(db, p["id"], "wt-merge-stale-sess")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/stale1", ".",
    )
    await db_module.persist_worktree_manifest(
        db, wt["id"], p["id"], session["id"], None, "repo", "dev", sha1,
    )
    # Back-date the manifest well past the staleness threshold.
    await db.execute(
        "UPDATE wave_base_manifests SET created_at = datetime('now', '-48 hours') "
        "WHERE worktree_id = ?",
        (wt["id"],),
    )
    await db.commit()

    result = await _merge_guard_mod.validate_worktree_merge(
        db, repo, wt["id"], stale_after_hours=24.0,
    )
    assert result["ok"] is False
    codes = {e["code"] for e in result["errors"]}
    assert "STALE_MANIFEST" in codes


@pytest.mark.asyncio
async def test_validate_worktree_merge_no_manifest_is_rejected(db, tmp_path):
    repo = tmp_path / "repo-nomanifest"
    repo.mkdir()
    _init_git_repo(str(repo))
    _git_commit(str(repo), "a.txt", "one")

    p = await db_module.create_project(db, "wt-merge-nomanifest")
    session = await db_module.register_session(db, p["id"], "wt-merge-nomanifest-sess")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/nomanifest1", ".",
    )

    result = await _merge_guard_mod.validate_worktree_merge(db, repo, wt["id"])
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "NO_MANIFEST"


@pytest.mark.asyncio
async def test_validate_worktree_merge_unknown_worktree_id_is_rejected(db, tmp_path):
    result = await _merge_guard_mod.validate_worktree_merge(
        db, tmp_path, "no-such-worktree-id",
    )
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "WORKTREE_NOT_FOUND"


@pytest.mark.asyncio
async def test_validate_worktree_merge_hosted_skips_git_checks_but_checks_manifest(db, tmp_path):
    """repo_root=None (hosted / no local FS access) must still catch a
    missing or stale manifest -- only the git-level checks are skipped."""
    p = await db_module.create_project(db, "wt-merge-hosted")
    session = await db_module.register_session(db, p["id"], "wt-merge-hosted-sess")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/hosted1", ".",
    )
    await db_module.persist_worktree_manifest(
        db, wt["id"], p["id"], session["id"], None, "repo", "dev", "a" * 40,
    )

    result = await _merge_guard_mod.validate_worktree_merge(db, None, wt["id"])
    assert result["ok"] is True
    assert result["head_sha"] is None


# ---------------------------------------------------------------------------
# 3b. f291bb24 investigation -- git subprocess calls must not block the
# event loop. Before this fix, _git() called subprocess.run() directly
# inline on the event loop thread: a single validate_worktree_merge call
# (up to 3 sequential git invocations) froze EVERY concurrently-scheduled
# coroutine server-wide for the duration, not just the caller's own request
# -- the structural reason concurrent-load timeouts were reproducible while
# solo/serial testing looked fine.
#
# Both tests below patch the underlying (synchronous) subprocess.run with a
# real, fixed-duration sleep so the discrimination between "blocked" and
# "not blocked" is deterministic rather than dependent on how fast a real
# `git` happens to run on the machine. Each was confirmed, during review, to
# actually FAIL when _git() is reverted to a plain synchronous
# subprocess.run() call (i.e. these are real regression tests, not
# placebos that would pass either way).
# ---------------------------------------------------------------------------


def _blocking_subprocess_run_stub(*, delay: float):
    """Stand-in for subprocess.run: sleeps `delay` seconds (a REAL, blocking
    time.sleep -- this is what a slow git invocation looks like from the
    caller's perspective) then returns a plausible CompletedProcess."""

    def _stub(cmd, **kwargs):
        time.sleep(delay)
        args = list(cmd[1:]) if cmd and cmd[0] == "git" else list(cmd)
        if args[:2] == ["rev-parse", "HEAD"]:
            stdout = ("b" * 40) + "\n"
        else:
            stdout = ""  # clean `status --porcelain`; ancestor `merge-base`
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

    return _stub


@pytest.mark.asyncio
async def test_git_helper_does_not_block_event_loop(tmp_path):
    """A slow git invocation must not stall an unrelated concurrent task.

    Patches subprocess.run with a real, deterministic 0.3s delay and races
    it against a concurrent asyncio.sleep(0.05) task. If _git() blocks the
    event loop (the pre-fix behavior), NOTHING else can run until the
    blocking call returns, so the sleep task's timer cannot fire until
    AFTER the git call finishes. With the fix (asyncio.to_thread moving the
    blocking call to a worker thread), the sleep task fires on its own
    schedule, well before the slower git call completes.
    """
    delay = 0.3
    sleep_fired_at = None
    git_finished_at = None

    async def _mark_sleep():
        nonlocal sleep_fired_at
        await asyncio.sleep(0.05)
        sleep_fired_at = time.monotonic()

    async def _timed_git_call():
        nonlocal git_finished_at
        await _merge_guard_mod._git(tmp_path, ["status"], timeout=20)
        git_finished_at = time.monotonic()

    with patch(
        "meridian.worktree_merge_guard.subprocess.run",
        side_effect=_blocking_subprocess_run_stub(delay=delay),
    ):
        start = time.monotonic()
        git_task = asyncio.create_task(_timed_git_call())
        sleep_task = asyncio.create_task(_mark_sleep())
        await asyncio.gather(git_task, sleep_task)

    assert sleep_fired_at is not None
    assert git_finished_at is not None
    assert sleep_fired_at < git_finished_at, (
        f"sleep fired at {sleep_fired_at - start:.3f}s but the 0.3s git call "
        f"didn't finish until {git_finished_at - start:.3f}s later -- the "
        "event loop was blocked (sleep couldn't run until git returned)"
    )
    assert sleep_fired_at - start < delay / 2


@pytest.mark.asyncio
async def test_validate_worktree_merge_runs_concurrently_with_other_coroutines(db, tmp_path):
    """End-to-end proof at the validate_worktree_merge level: a concurrent,
    unrelated coroutine must make MEASURABLE progress WHILE
    validate_worktree_merge (which internally awaits _git up to three
    times) is still running -- not merely complete afterward without
    deadlocking, which a fully-blocking implementation would also do."""
    repo = tmp_path / "repo-concurrent"
    repo.mkdir()
    _init_git_repo(str(repo))
    sha1 = _git_commit(str(repo), "a.txt", "one")
    _git_commit(str(repo), "b.txt", "two")

    p = await db_module.create_project(db, "wt-merge-concurrent")
    session = await db_module.register_session(db, p["id"], "wt-merge-concurrent-sess")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/concurrent1", ".",
    )
    await db_module.persist_worktree_manifest(
        db, wt["id"], p["id"], session["id"], None, "repo", "dev", sha1,
    )

    delay = 0.3  # x3 sequential _git() calls inside validate_worktree_merge
    ticks_during_validate = 0
    validate_done = asyncio.Event()

    async def _ticker():
        nonlocal ticks_during_validate
        while not validate_done.is_set():
            await asyncio.sleep(0.02)
            if not validate_done.is_set():
                ticks_during_validate += 1

    with patch(
        "meridian.worktree_merge_guard.subprocess.run",
        side_effect=_blocking_subprocess_run_stub(delay=delay),
    ):
        ticker_task = asyncio.create_task(_ticker())
        result = await _merge_guard_mod.validate_worktree_merge(db, repo, wt["id"])
        validate_done.set()
        await ticker_task

    assert result["ok"] is True
    # ~0.9s of wall-clock git-call time at a 20ms ticker interval means a
    # non-blocking implementation should rack up ~40+ ticks DURING the
    # call. A blocking implementation starves the ticker entirely until
    # validate_worktree_merge returns and validate_done is set on the very
    # next line -- landing at (or near) zero ticks during the call.
    assert ticks_during_validate >= 5, (
        f"only {ticks_during_validate} ticker ticks landed while "
        "validate_worktree_merge was in flight -- event loop was likely blocked"
    )


# ---------------------------------------------------------------------------
# 4. Cleanup guard -- validate path/PID before real disk removal
# ---------------------------------------------------------------------------


def test_cleanup_guard_rejects_missing_row():
    result = _wt_cleanup_mod.validate_worktree_cleanup_target(
        None, expected_worktree_id="wt-does-not-exist",
    )
    assert result["ok"] is False
    assert result["reason"] == "NOT_FOUND"


def test_cleanup_guard_rejects_id_mismatch():
    row = {"id": "wt-real", "path": ".claude/worktrees/real", "pid": None}
    result = _wt_cleanup_mod.validate_worktree_cleanup_target(
        row, expected_worktree_id="wt-other",
    )
    assert result["ok"] is False
    assert result["reason"] == "ID_MISMATCH"


def test_cleanup_guard_rejects_path_mismatch():
    row = {"id": "wt-real", "path": ".claude/worktrees/real", "pid": None}
    result = _wt_cleanup_mod.validate_worktree_cleanup_target(
        row, expected_worktree_id="wt-real", expected_path=".claude/worktrees/WRONG",
    )
    assert result["ok"] is False
    assert result["reason"] == "PATH_MISMATCH"


def test_cleanup_guard_allows_when_no_pid_recorded():
    row = {"id": "wt-real", "path": ".claude/worktrees/real", "pid": None}
    result = _wt_cleanup_mod.validate_worktree_cleanup_target(
        row, expected_worktree_id="wt-real",
    )
    assert result["ok"] is True


def test_cleanup_guard_rejects_when_pid_still_alive():
    """A live PID (the current test process itself) must block cleanup."""
    row = {"id": "wt-real", "path": ".claude/worktrees/real", "pid": os.getpid()}
    result = _wt_cleanup_mod.validate_worktree_cleanup_target(
        row, expected_worktree_id="wt-real",
    )
    assert result["ok"] is False
    assert result["reason"] == "PROCESS_STILL_LIVE"
    assert result["detail"] and str(os.getpid()) in result["detail"]


def test_cleanup_guard_allows_when_pid_is_dead(monkeypatch):
    """A recorded PID that's no longer running must NOT block cleanup."""
    def _dead(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(_wt_cleanup_mod.os, "kill", _dead)
    row = {"id": "wt-real", "path": ".claude/worktrees/real", "pid": 999999}
    result = _wt_cleanup_mod.validate_worktree_cleanup_target(
        row, expected_worktree_id="wt-real",
    )
    assert result["ok"] is True


def test_remove_worktree_on_disk_guarded_skips_disk_mutation_when_pid_alive(monkeypatch, tmp_path):
    """The guarded wrapper must never even ATTEMPT a disk removal when the
    guard rejects it -- confirms the destructive call is truly gated, not
    just logged around."""
    called = []
    monkeypatch.setattr(
        _wt_cleanup_mod, "remove_worktree_on_disk",
        lambda *a, **k: called.append((a, k)) or {"attempted": True, "removed": True, "detail": "x"},
    )
    row = {"id": "wt-live", "path": "some/path", "pid": os.getpid()}
    outcome = _wt_cleanup_mod.remove_worktree_on_disk_guarded(
        tmp_path, row, expected_worktree_id="wt-live",
    )
    assert outcome["attempted"] is False
    assert outcome["removed"] is False
    assert outcome["guard_ok"] is False
    assert outcome["reason"] == "PROCESS_STILL_LIVE"
    assert called == []  # the real disk-mutating function was never invoked


def test_remove_worktree_on_disk_guarded_proceeds_when_guard_passes(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(
        _wt_cleanup_mod, "remove_worktree_on_disk",
        lambda *a, **k: called.append((a, k)) or {"attempted": True, "removed": True, "detail": "x"},
    )
    row = {"id": "wt-idle", "path": "some/path", "pid": None}
    outcome = _wt_cleanup_mod.remove_worktree_on_disk_guarded(
        tmp_path, row, expected_worktree_id="wt-idle",
    )
    assert outcome["removed"] is True
    assert outcome["guard_ok"] is True
    assert len(called) == 1


# ---------------------------------------------------------------------------
# End-to-end: complete_sprint_item hard-rejects at merge time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_sprint_item_rejects_when_worktree_fails_merge_validation(db, tmp_path, monkeypatch):
    """The wired gate (acceptance points 2+3): when a worktree has a
    persisted base manifest and it fails validation (here: dirty tree),
    complete_sprint_item must hard-reject with a structured error and leave
    the item NOT done -- not just fire the pre-existing advisory HITL."""
    import meridian.server as srv

    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)

    repo = tmp_path / "repo-e2e-dirty"
    repo.mkdir()
    _init_git_repo(str(repo))
    sha1 = _git_commit(str(repo), "a.txt", "one")
    (repo / "uncommitted.txt").write_text("wip", encoding="utf-8")
    monkeypatch.setattr(srv, "_REPO_ROOT", repo)

    p = await db_module.create_project(db, "wt-e2e-dirty")
    session = await db_module.register_session(db, p["id"], "wt-e2e-dirty-sess")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "e2e dirty item", prospect_bypass=True,
    )
    await db_module.claim_sprint_item(db, p["id"], item["id"])
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/e2edirty1", ".", item_id=item["id"],
    )
    await db_module.persist_worktree_manifest(
        db, wt["id"], p["id"], session["id"], item["id"], "repo", "dev", sha1,
    )

    result = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"], "session_id": session["id"]},
        db, "/tmp",
    )

    assert result.get("error") == "WORKTREE_MERGE_BLOCKED"
    assert result["worktree_id"] == wt["id"]
    codes = {e["code"] for e in result["validation"]["errors"]}
    assert "DIRTY_WORKTREE" in codes

    reloaded = await db_module.get_sprint_item(db, item["id"])
    assert reloaded["status"] != "done"


@pytest.mark.asyncio
async def test_complete_sprint_item_proceeds_when_no_manifest_exists(db, tmp_path, monkeypatch):
    """Backward compatibility: a worktree that never opted into a base
    manifest (the common case for every worktree registered before this
    sprint item) must NOT be retroactively blocked -- completion proceeds
    exactly like before (advisory HITL only)."""
    import meridian.server as srv

    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    monkeypatch.setattr(srv, "_REPO_ROOT", tmp_path)

    p = await db_module.create_project(db, "wt-e2e-nomanifest")
    session = await db_module.register_session(db, p["id"], "wt-e2e-nomanifest-sess")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "e2e no-manifest item", prospect_bypass=True,
    )
    await db_module.claim_sprint_item(db, p["id"], item["id"])
    await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/e2enomanifest1", ".claude/worktrees/e2enomanifest1",
        item_id=item["id"],
    )

    result = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"], "session_id": session["id"]},
        db, "/tmp",
    )

    assert result.get("error") is None
    assert result["status"] == "done"
