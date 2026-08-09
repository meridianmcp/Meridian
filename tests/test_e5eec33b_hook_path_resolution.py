"""e5eec33b -- cross-platform active-repository resolver for generated and
configured Claude Code hooks.

Reproduced 2026-08-07 in a Claude executor: PreToolUse hook commands in
.claude/settings.json expanded to a bare, drive-root-relative fragment
("\\.claude\\hooks\\secret_guard.ps1") because CLAUDE_PROJECT_DIR was empty
in the launcher shell -- even though the repo-local hook genuinely exists.
Separately, a user-global hook (~/.claude/hooks/meridian-stop.ps1, written
once per machine by hooks.ps1) can be legitimately absent and must be
classified as an optional no-op, not a confusing failure.

Covers meridian.hook_paths:
1. normalize_wsl_path -- WSL /mnt/c/... -> C:/..., matches
   meridian.server._normalize_hook_cwd_path byte-for-byte (shared impl).
2. resolve_active_repo_root -- empty CLAUDE_PROJECT_DIR, worktree cwd,
   nested cwd walk-up, WSL-style CLAUDE_PROJECT_DIR, never collapses to a
   bare ".claude"-relative fragment.
3. resolve_repo_root_for_handoff -- validates + normalizes a stored
   executor_config.repo_path (used by handoff._write_sprint_guard_hooks).
4. resolve_configured_hook_command / diagnose_configured_hooks --
   required-vs-optional classification, structured status, missing-optional
   is a silent no-op, missing-required surfaces a clear diagnostic.
5. Generated <-> configured hook parity -- what
   handoff._write_sprint_guard_hooks generates on disk resolves "ok"
   through the same resolver a live settings.json command would use.
6. Real repo .claude/settings.json -- every currently-configured
   $CLAUDE_PROJECT_DIR-scoped hook actually resolves against this repo's
   real root (regression guard against re-introducing the invocation bug).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import hook_paths
from meridian import server as server_module

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. normalize_wsl_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/mnt/c/Users/me/repo", "C:/Users/me/repo"),
        ("/mnt/c/Users/me/repo/", "C:/Users/me/repo"),
        ("/mnt/d", "D:"),  # bare drive root -- trailing "/" stripped like any other path
        (r"C:\Users\me\repo", "C:/Users/me/repo"),
        (r"C:\Users\me\repo\\", "C:/Users/me/repo"),
        ("", ""),
        (None, ""),
        ("/home/me/repo", "/home/me/repo"),
    ],
)
def test_normalize_wsl_path(raw, expected):
    assert hook_paths.normalize_wsl_path(raw) == expected


def test_normalize_wsl_path_matches_server_delegate():
    """server._normalize_hook_cwd_path now delegates to hook_paths -- assert
    they stay byte-for-byte identical across representative inputs."""
    for raw in ("/mnt/c/Users/x/repo/", r"C:\a\b\c", "", "/mnt/e/foo/bar"):
        assert server_module._normalize_hook_cwd_path(raw) == hook_paths.normalize_wsl_path(raw)


# ---------------------------------------------------------------------------
# 2. resolve_active_repo_root
# ---------------------------------------------------------------------------


def test_resolve_active_repo_root_uses_claude_project_dir_when_present():
    # Compare as Path objects, not raw strings -- Path.__str__ normalizes to
    # the platform's native separator (backslash on Windows), so a
    # forward-slash string literal would never match on Windows even when
    # the resolution is correct.
    root = hook_paths.resolve_active_repo_root(claude_project_dir=r"C:\Users\me\repo")
    assert root == Path("C:/Users/me/repo")


def test_resolve_active_repo_root_normalizes_wsl_claude_project_dir():
    root = hook_paths.resolve_active_repo_root(claude_project_dir="/mnt/c/Users/me/repo")
    assert root == Path("C:/Users/me/repo")


def test_resolve_active_repo_root_empty_env_falls_back_to_session_project_root():
    root = hook_paths.resolve_active_repo_root(
        claude_project_dir="", session_project_root=r"D:\projects\repo"
    )
    assert root == Path("D:/projects/repo")


def test_resolve_active_repo_root_empty_env_falls_back_to_cwd_walk(tmp_path):
    """The core regression: CLAUDE_PROJECT_DIR empty must never collapse to a
    bare root-relative ".claude" fragment -- it must derive a real root from
    cwd instead."""
    repo_root = tmp_path / "repo"
    (repo_root / ".claude").mkdir(parents=True)
    nested = repo_root / "sub" / "dir"
    nested.mkdir(parents=True)

    resolved = hook_paths.resolve_active_repo_root(claude_project_dir="", cwd=str(nested))
    assert resolved == repo_root.resolve()
    # Never a bare/relative fragment like ".claude" or "\.claude".
    resolved_str = str(resolved)
    assert not resolved_str.startswith(".claude")
    assert not resolved_str.startswith("\\.claude")
    assert not resolved_str.startswith("/.claude")


def test_resolve_active_repo_root_worktree_cwd_stays_at_worktree_root(tmp_path):
    """A worktree checkout has its OWN .claude directory. cwd = the worktree
    root itself must resolve to that worktree, not walk further up into a
    parent/main checkout that also happens to have a .claude dir."""
    main_repo = tmp_path / "main"
    (main_repo / ".claude").mkdir(parents=True)
    worktree = main_repo / ".claude" / "worktrees" / "agent-xyz"
    (worktree / ".claude").mkdir(parents=True)

    resolved = hook_paths.resolve_active_repo_root(claude_project_dir="", cwd=str(worktree))
    assert resolved == worktree.resolve()


def test_resolve_active_repo_root_returns_absolute_path_even_with_no_relevant_marker(tmp_path):
    """No CLAUDE_PROJECT_DIR -- must never crash and must never return a bare
    root-relative ".claude" fragment.

    Deliberately does NOT assert exactly which ancestor comes back: tmp_path
    lives under the real user's home directory, which itself may have its
    own ``.claude`` marker (it does, in this environment) -- and the walk-up
    correctly stopping there is CORRECT behavior, not a bug. The invariant
    under test is narrower and environment-independent: always a real,
    absolute path, never a bare/relative ".claude"-prefixed fragment.
    """
    isolated = tmp_path / "no_claude_here"
    isolated.mkdir()
    resolved = hook_paths.resolve_active_repo_root(claude_project_dir="", cwd=str(isolated))
    assert resolved is not None
    assert resolved.is_absolute()
    resolved_str = str(resolved)
    assert not resolved_str.startswith(".claude")
    assert not resolved_str.lstrip("\\/").startswith(".claude")


def test_resolve_active_repo_root_blank_session_project_root_is_ignored():
    # A blank/whitespace session_project_root must not be treated as real
    # signal -- falls through to the cwd walk instead of resolving to "".
    root = hook_paths.resolve_active_repo_root(
        claude_project_dir="", session_project_root="   ", cwd=str(_REPO_ROOT)
    )
    assert root is not None
    assert str(root) != ""


# ---------------------------------------------------------------------------
# 3. resolve_repo_root_for_handoff
# ---------------------------------------------------------------------------


def test_resolve_repo_root_for_handoff_valid_repo(tmp_path):
    (tmp_path / ".claude").mkdir()
    resolved = hook_paths.resolve_repo_root_for_handoff(str(tmp_path))
    assert resolved == tmp_path


def test_resolve_repo_root_for_handoff_missing_claude_dir_returns_none(tmp_path):
    # tmp_path exists but has no .claude directory.
    assert hook_paths.resolve_repo_root_for_handoff(str(tmp_path)) is None


def test_resolve_repo_root_for_handoff_blank_returns_none():
    assert hook_paths.resolve_repo_root_for_handoff("") is None
    assert hook_paths.resolve_repo_root_for_handoff("   ") is None


def test_resolve_repo_root_for_handoff_nonexistent_path_returns_none():
    assert hook_paths.resolve_repo_root_for_handoff(r"Z:\does\not\exist\anywhere") is None


@pytest.mark.skipif(sys.platform != "win32", reason="WSL drive-letter round-trip is Windows-specific")
def test_resolve_repo_root_for_handoff_normalizes_wsl_style_path(tmp_path):
    """A repo_path recorded from a WSL/Linux session (/mnt/<drive>/...) must
    still resolve correctly when read back on native Windows."""
    (tmp_path / ".claude").mkdir()
    drive = str(tmp_path)[0].lower()
    rest = str(tmp_path)[2:].replace("\\", "/").strip("/")
    wsl_style = f"/mnt/{drive}/{rest}"
    resolved = hook_paths.resolve_repo_root_for_handoff(wsl_style)
    assert resolved is not None
    assert resolved.resolve() == tmp_path.resolve()


# ---------------------------------------------------------------------------
# 4. resolve_configured_hook_command / diagnose_configured_hooks
# ---------------------------------------------------------------------------


def test_required_hook_resolves_ok_when_script_exists(tmp_path):
    (tmp_path / ".claude" / "hooks").mkdir(parents=True)
    (tmp_path / ".claude" / "hooks" / "secret_guard.ps1").write_text("# ok", encoding="utf-8")
    command = '& "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\secret_guard.ps1"'
    diag = hook_paths.resolve_configured_hook_command(command, tmp_path)
    assert diag["required"] is True
    assert diag["exists"] is True
    assert diag["status"] == hook_paths.STATUS_OK


def test_required_hook_missing_script_is_missing_required(tmp_path):
    (tmp_path / ".claude" / "hooks").mkdir(parents=True)
    command = '& "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\secret_guard.ps1"'
    diag = hook_paths.resolve_configured_hook_command(command, tmp_path)
    assert diag["required"] is True
    assert diag["exists"] is False
    assert diag["status"] == hook_paths.STATUS_MISSING_REQUIRED


def test_required_hook_with_no_repo_root_is_unresolvable():
    command = '& "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\secret_guard.ps1"'
    diag = hook_paths.resolve_configured_hook_command(command, None)
    assert diag["required"] is True
    assert diag["status"] == hook_paths.STATUS_UNRESOLVABLE


def test_optional_global_hook_missing_is_silent_noop_not_error(tmp_path):
    """The exact 2026-08-07 case: C:\\Users\\...\\meridian-stop.ps1 genuinely
    absent must classify as an optional no-op, never a confusing failure."""
    missing_global = tmp_path / "home" / ".claude" / "hooks" / "meridian-stop.ps1"
    command = f'& "{missing_global}"'
    diag = hook_paths.resolve_configured_hook_command(command, tmp_path)
    assert diag["required"] is False
    assert diag["exists"] is False
    assert diag["status"] == hook_paths.STATUS_OPTIONAL_ABSENT


def test_optional_global_hook_present_resolves_ok(tmp_path):
    home_hooks = tmp_path / "home" / ".claude" / "hooks"
    home_hooks.mkdir(parents=True)
    script = home_hooks / "meridian-start.ps1"
    script.write_text("# ok", encoding="utf-8")
    command = f'& "{script}"'
    diag = hook_paths.resolve_configured_hook_command(command, tmp_path)
    assert diag["required"] is False
    assert diag["status"] == hook_paths.STATUS_OK


def test_command_with_no_script_token_is_unresolvable():
    diag = hook_paths.resolve_configured_hook_command("echo hello", None)
    assert diag["status"] == hook_paths.STATUS_UNRESOLVABLE
    assert diag["script_token"] is None


def test_diagnose_configured_hooks_classifies_mixed_settings(tmp_path):
    repo_root = tmp_path / "repo"
    hooks_dir = repo_root / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "secret_guard.ps1").write_text("# ok", encoding="utf-8")

    missing_global = tmp_path / "home" / ".claude" / "hooks" / "meridian-stop.ps1"
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Read",
                    "hooks": [
                        {
                            "type": "command",
                            "command": '& "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\secret_guard.ps1"',
                        }
                    ],
                },
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": '& "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\dependency_install_guard.ps1"',
                        }
                    ],
                },
            ],
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": f'& "{missing_global}"'}],
                }
            ],
        }
    }
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    results = hook_paths.diagnose_configured_hooks(settings_path, repo_root=repo_root)
    by_event = {(r["event"], r["required"]): r for r in results}

    assert by_event[("PreToolUse", True)]["status"] in (
        hook_paths.STATUS_OK,
        hook_paths.STATUS_MISSING_REQUIRED,
    )
    # secret_guard.ps1 exists -> ok; dependency_install_guard.ps1 does not -> missing_required.
    statuses = {r["status"] for r in results if r["required"]}
    assert hook_paths.STATUS_OK in statuses
    assert hook_paths.STATUS_MISSING_REQUIRED in statuses

    stop_diag = next(r for r in results if r["event"] == "Stop")
    assert stop_diag["required"] is False
    assert stop_diag["status"] == hook_paths.STATUS_OPTIONAL_ABSENT


def test_diagnose_configured_hooks_unreadable_settings_returns_empty_list(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    assert hook_paths.diagnose_configured_hooks(missing, repo_root=tmp_path) == []


def test_diagnose_configured_hooks_malformed_json_returns_empty_list(tmp_path):
    bad = tmp_path / "settings.json"
    bad.write_text("{not json", encoding="utf-8")
    assert hook_paths.diagnose_configured_hooks(bad, repo_root=tmp_path) == []


# ---------------------------------------------------------------------------
# 5. Generated <-> configured hook parity
# ---------------------------------------------------------------------------


def test_generated_sprint_guard_hooks_resolve_ok_via_configured_style_command(tmp_path):
    """What handoff._write_sprint_guard_hooks actually writes to disk must
    resolve "ok" through the same resolver a live settings.json command
    would use -- proving generated and configured hooks agree."""

    async def _run():
        db = await db_module.init_db(":memory:")
        await handoff_module._write_sprint_guard_hooks(db, "parity-proj", root=tmp_path)

    asyncio.run(_run())

    for filename in ("sprint_guard.ps1", "sprint_guard.sh"):
        command = f'& "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\{filename}"'
        diag = hook_paths.resolve_configured_hook_command(command, tmp_path)
        assert diag["status"] == hook_paths.STATUS_OK, f"{filename}: {diag}"
        assert diag["required"] is True


# ---------------------------------------------------------------------------
# 6. Real repo .claude/settings.json -- regression guard
# ---------------------------------------------------------------------------


def test_real_repo_settings_json_required_hooks_all_resolve_ok():
    """Every $CLAUDE_PROJECT_DIR-scoped hook actually configured in this
    repo's own .claude/settings.json must resolve against this repo's real
    root -- guards against re-introducing the 2026-08-07 invocation bug."""
    settings_path = _REPO_ROOT / ".claude" / "settings.json"
    if not settings_path.exists():
        pytest.skip("no .claude/settings.json in this checkout")

    results = hook_paths.diagnose_configured_hooks(settings_path, repo_root=_REPO_ROOT)
    required = [r for r in results if r["required"]]
    assert required, "expected at least one $CLAUDE_PROJECT_DIR-scoped hook in settings.json"
    for diag in required:
        assert diag["status"] == hook_paths.STATUS_OK, diag


def test_real_repo_settings_json_global_hooks_classified_optional():
    """The user-global meridian-start/meridian-stop Stop/SessionStart hooks
    use a hardcoded absolute path outside the repo -- must classify as
    optional, regardless of whether they currently exist on this machine."""
    settings_path = _REPO_ROOT / ".claude" / "settings.json"
    if not settings_path.exists():
        pytest.skip("no .claude/settings.json in this checkout")

    results = hook_paths.diagnose_configured_hooks(settings_path, repo_root=_REPO_ROOT)
    global_hooks = [
        r for r in results
        if r["script_token"] and "meridian-start" in r["script_token"].lower()
        or (r["script_token"] and "meridian-stop" in r["script_token"].lower())
    ]
    for diag in global_hooks:
        assert diag["required"] is False
        assert diag["status"] in (hook_paths.STATUS_OK, hook_paths.STATUS_OPTIONAL_ABSENT)
