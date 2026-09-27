"""Tests for meridian.hook_settings_merge -- the idempotent Claude Code
settings.json merger behind ``python -m meridian hooks install-guard``
(sprint item 55d48d69). Temp dirs only: home, guard dir and shim source are
always injected, so the real ~/.claude, %LOCALAPPDATA% and repo settings are
never touched."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from meridian import hook_paths
from meridian import hook_settings_merge as hsm
from meridian.__main__ import _dispatch_subcommand, main as meridian_main

REPO_ROOT = Path(__file__).resolve().parent.parent

# A byte-exact copy of the repo-tracked .claude/settings.json layout (4-space
# indent, shell=powershell, $CLAUDE_PROJECT_DIR commands) so round-trip
# exactness is tested against the real house style, independent of whatever
# the tracked file contains at test time.
REPO_STYLE_SETTINGS = """{
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "AskUserQuestion",
                "hooks": [
                    {
                        "type": "command",
                        "shell": "powershell",
                        "command": "& \\"$CLAUDE_PROJECT_DIR\\\\.claude\\\\hooks\\\\hitl_guard.ps1\\""
                    }
                ]
            },
            {
                "matcher": "Read|Bash|Grep|Glob",
                "hooks": [
                    {
                        "type": "command",
                        "shell": "powershell",
                        "command": "& \\"$CLAUDE_PROJECT_DIR\\\\.claude\\\\hooks\\\\secret_guard.ps1\\""
                    }
                ]
            }
        ],
        "SessionStart": [
            {
                "matcher": "compact",
                "hooks": [
                    {
                        "type": "command",
                        "shell": "powershell",
                        "command": "& \\"$CLAUDE_PROJECT_DIR\\\\.claude\\\\hooks\\\\post_compact_refresh.ps1\\""
                    }
                ]
            }
        ]
    },
    "permissions": {
        "allow": [
            "Bash(*)",
            "Read(*)"
        ]
    }
}
"""

# The user-global layout on this machine: third-party codebase-memory-mcp
# hooks, 2-space indent, unrelated top-level keys.
USER_STYLE_SETTINGS = {
    "model": "sonnet",
    "hooks": {
        "PreToolUse": [
            {"matcher": "Grep|Glob", "hooks": [{"type": "command", "command": "~/.claude/hooks/cbm-code-discovery-gate", "timeout": 5}]}
        ],
        "SessionStart": [
            {"matcher": m, "hooks": [{"type": "command", "command": "~/.claude/hooks/cbm-session-reminder"}]}
            for m in ("startup", "resume", "clear", "compact")
        ],
        "SubagentStart": [
            {"matcher": "*", "hooks": [{"type": "command", "command": "~/.claude/hooks/cbm-subagent-reminder"}]}
        ],
    },
    "skipDangerousModePermissionPrompt": True,
}


def _full(matcher: str, tool: str) -> bool:
    return re.fullmatch(f"(?:{matcher})", tool) is not None


@pytest.fixture()
def shim_dir(tmp_path: Path) -> Path:
    d = tmp_path / "shim_src"
    d.mkdir()
    for base in (hsm.SHIM_PRE, hsm.SHIM_POST, hsm.SHIM_BRIEF):
        (d / f"{base}.ps1").write_text(f"# {base} test shim\nexit 0\n", encoding="ascii")
        (d / f"{base}.sh").write_text(f"#!/usr/bin/env bash\n# {base} test shim\nexit 0\n", encoding="ascii")
    (d / "meridian_guard_lib.ps1").write_text("# helper library\n", encoding="ascii")
    (d / "unrelated_guard.ps1").write_text("# not ours\n", encoding="ascii")
    return d


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    return {hsm.GUARD_DIR_ENV: str(tmp_path / "guard")}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "target_repo"
    (r / ".claude").mkdir(parents=True)
    (r / ".claude" / "settings.json").write_text(REPO_STYLE_SETTINGS, encoding="utf-8")
    return r


def _install(repo: Path, tmp_path: Path, shim_dir: Path, env: dict[str, str], **kw):
    plan = hsm.plan_install(
        repo,
        scope=kw.pop("scope", "project"),
        mode=kw.pop("mode", "enforce"),
        shell=kw.pop("shell", "powershell"),
        home=kw.pop("home", tmp_path / "home"),
        gdir=Path(env[hsm.GUARD_DIR_ENV]),
        shim_source=shim_dir,
        env=env,
        **kw,
    )
    return plan


# ---------------------------------------------------------------------------
# Matchers, commands, timeouts (structural)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("matcher", [hsm.PRE_MATCHER_PROJECT, hsm.PRE_MATCHER_USER, hsm.POST_MATCHER])
def test_no_matcher_matches_read(matcher):
    re.compile(matcher)
    assert re.search(matcher, "Read") is None
    assert not _full(matcher, "Read")


@pytest.mark.parametrize(
    "tool",
    [
        "Grep", "Glob", "Bash", "PowerShell", "Monitor", "Write", "Edit", "MultiEdit",
        "NotebookEdit", "WebSearch", "WebFetch", "mcp__dc__start_process",
        "mcp__dc__interact_with_process", "mcp__dc__start_search", "mcp__dc__write_file",
        "mcp__dc__edit_block", "mcp__dc__move_file", "mcp__codebase-memory-mcp__search_graph",
        "mcp__codebase-memory__search_code", "mcp__serena__write_memory",
        "mcp__meridian-extract__edit_memory", "mcp__serena__rename_memory",
        "mcp__meridian__patch_file",
    ],
)
def test_project_pre_matcher_covers_every_rule_tool(tool):
    assert _full(hsm.PRE_MATCHER_PROJECT, tool)


@pytest.mark.parametrize(
    "tool",
    ["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "PowerShell", "Monitor",
     "mcp__dc__write_file", "mcp__dc__start_process", "mcp__serena__write_memory",
     "mcp__meridian__patch_file"],
)
def test_user_pre_matcher_covers_memory_write_rules(tool):
    assert _full(hsm.PRE_MATCHER_USER, tool)


@pytest.mark.parametrize(
    "tool",
    ["Grep", "Glob", "WebSearch", "WebFetch", "mcp__codebase-memory-mcp__search_graph",
     "mcp__dc__start_search", "mcp__serena__read_memory", "mcp__serena__list_memories"],
)
def test_user_pre_matcher_is_limited_to_g6_g8(tool):
    assert not _full(hsm.PRE_MATCHER_USER, tool)


@pytest.mark.parametrize(
    "tool",
    ["WebFetch", "WebSearch", "mcp__codebase-memory-mcp__search_code", "mcp__serena__find_symbol",
     "mcp__98ff5a3a-9b9d-4075-8d6e-306ff084c0eb__start_session", "mcp__meridian__claim_sprint_item",
     "mcp__meridian__capture_research_finding", "mcp__meridian__get_sprint_items"],
)
def test_post_matcher_covers_receipts_and_quarantine(tool):
    assert _full(hsm.POST_MATCHER, tool)


def test_post_matcher_skips_file_tools():
    for tool in ("Read", "Edit", "Write", "Bash"):
        assert not _full(hsm.POST_MATCHER, tool)


@pytest.mark.parametrize("scope", hsm.SCOPES)
@pytest.mark.parametrize("shell", hsm.SHELLS)
def test_explicit_timeouts(scope, shell, tmp_path):
    entries = hsm.desired_entries(scope=scope, mode="enforce", shell=shell, user_hooks_dir=tmp_path)
    for event, _matcher, hook in entries:
        assert hook["timeout"] == hsm.TIMEOUTS[event]
        assert hook["timeout"] <= 10
    events = [e for e, _m, _h in entries]
    if scope == "project":
        assert events == ["PreToolUse", "PostToolUse", "SessionStart", "SubagentStart"]
    else:
        assert events == ["PreToolUse", "SessionStart", "SubagentStart"]
    assert hsm.TIMEOUTS["PreToolUse"] == hsm.TIMEOUTS["PostToolUse"] == 3
    assert hsm.TIMEOUTS["SessionStart"] == hsm.TIMEOUTS["SubagentStart"] == 10


def test_project_command_is_the_repo_house_form():
    cmd = hsm.build_command("meridian_guard", scope="project", mode="enforce", shell="powershell")
    # $env: (the bare $CLAUDE_PROJECT_DIR is an unset PowerShell variable under
    # Claude Code's -Command invocation) + the exit-code suffix.
    assert cmd == (
        '& "$env:CLAUDE_PROJECT_DIR\\.claude\\hooks\\meridian_guard.ps1"'
        "; if ($?) { exit 0 }; if ($LASTEXITCODE) { exit $LASTEXITCODE }; exit 1"
    )
    assert cmd.endswith(hsm.PS_EXIT_SUFFIX)
    # hook_paths classifies it as a REQUIRED project hook and extracts the script.
    assert hook_paths.is_project_relative_command(cmd)
    assert hook_paths.extract_script_path_token(cmd).endswith("meridian_guard.ps1")
    bash = hsm.build_command("meridian_guard_post", scope="project", mode="enforce", shell="bash")
    assert bash == 'bash "$CLAUDE_PROJECT_DIR/.claude/hooks/meridian_guard_post.sh"'


def test_advisory_mode_is_a_low_precedence_env_prefix():
    ps = hsm.build_command("meridian_guard", scope="project", mode="advisory", shell="powershell")
    assert ps.startswith("$env:MERIDIAN_GUARD_DEFAULT_MODE='advisory'; & ")
    sh = hsm.build_command("meridian_guard", scope="project", mode="advisory", shell="bash")
    assert sh.startswith("MERIDIAN_GUARD_DEFAULT_MODE=advisory bash ")
    # The owner's kill switch variable is never set by an installed command.
    for cmd in (ps, sh):
        assert "MERIDIAN_GUARD=" not in cmd and "MERIDIAN_GUARD'" not in cmd


def test_user_scope_command_defers_then_runs_shim(tmp_path):
    hooks = tmp_path / "home" / ".claude" / "hooks"
    ps = hsm.build_command("meridian_guard", scope="user", mode="enforce", shell="powershell", user_hooks_dir=hooks)
    assert ps.startswith("if (& ")
    assert "meridian_guard_defer.ps1" in ps and "{ exit 0 }" in ps
    assert "$env:MERIDIAN_GUARD_SCOPE='user'" in ps
    assert ps.endswith('meridian_guard.ps1"' + hsm.PS_EXIT_SUFFIX)
    assert "CLAUDE_PROJECT_DIR" not in ps
    sh = hsm.build_command("meridian_guard_brief", scope="user", mode="advisory", shell="bash", user_hooks_dir=hooks)
    assert "meridian_guard_defer.sh\" && exit 0; MERIDIAN_GUARD_SCOPE=user MERIDIAN_GUARD_DEFAULT_MODE=advisory bash" in sh
    assert "\\" not in sh


def test_build_command_rejects_unknown_values(tmp_path):
    with pytest.raises(hsm.GuardInstallError):
        hsm.build_command("meridian_guard", scope="global", mode="enforce", shell="bash")
    with pytest.raises(hsm.GuardInstallError):
        hsm.build_command("meridian_guard", scope="project", mode="strict", shell="bash")
    with pytest.raises(hsm.GuardInstallError):
        hsm.build_command("meridian_guard", scope="project", mode="enforce", shell="zsh")


def test_quoting_escapes_expansion_characters():
    assert hsm._ps_quote("C:\\a$b`c") == '"C:\\a`$b``c"'
    assert hsm._sh_quote("C:\\a$b") == '"C:/a\\$b"'


def test_module_never_references_global_installer_or_automemory_write():
    src = Path(hsm.__file__).read_text(encoding="utf-8")
    assert "hooks.ps1" not in src and "hooks.sh" not in src
    assert '"autoMemoryEnabled"' not in src and "['autoMemoryEnabled']" not in src


def test_generated_defer_scripts_are_ascii():
    hsm.DEFER_PS1.encode("ascii")
    hsm.DEFER_SH.encode("ascii")
    assert "stdin" in hsm.DEFER_PS1  # documents that it never reads stdin
    assert "ReadToEnd" not in hsm.DEFER_PS1 and "$input" not in hsm.DEFER_PS1


# ---------------------------------------------------------------------------
# Project scope install / idempotency / uninstall
# ---------------------------------------------------------------------------


def test_install_preserves_every_other_entry_and_registers_owned(repo, tmp_path, shim_dir, env):
    original = json.loads(REPO_STYLE_SETTINGS)
    plan = _install(repo, tmp_path, shim_dir, env, repair_launchers=False)
    assert plan.changed
    hsm.apply_plan(plan)
    after = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
    stripped, removed = hsm.remove_owned(after)
    assert removed == 4
    assert stripped == original  # every non-owned entry, in order, untouched
    owned = hsm.owned_entries(after)
    assert hsm._canonical(owned) == hsm._canonical(
        hsm.desired_entries(scope="project", mode="enforce", shell="powershell")
    )
    assert "autoMemoryEnabled" not in after
    # Unrelated original bytes survive: 4-space indent, key order.
    text = (repo / ".claude" / "settings.json").read_text(encoding="utf-8")
    assert text.startswith('{\n    "hooks": {\n        "PreToolUse": [\n            {\n                "matcher": "AskUserQuestion"')
    assert text.endswith("}\n")


def test_install_copies_only_guard_shims_and_writes_marker(repo, tmp_path, shim_dir, env):
    hsm.apply_plan(_install(repo, tmp_path, shim_dir, env))
    hooks = repo / ".claude" / "hooks"
    names = sorted(p.name for p in hooks.iterdir())
    assert "unrelated_guard.ps1" not in names
    for base in (hsm.SHIM_PRE, hsm.SHIM_POST, hsm.SHIM_BRIEF):
        assert (hooks / f"{base}.ps1").read_bytes() == (shim_dir / f"{base}.ps1").read_bytes()
        assert (hooks / f"{base}.sh").is_file()
    assert (hooks / "meridian_guard_lib.ps1").is_file()
    marker = json.loads((hooks / hsm.INSTALL_MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["scope"] == "project" and marker["mode"] == "enforce"
    assert set(marker["copied_files"]) >= {"meridian_guard.ps1", "meridian_guard_lib.ps1"}
    assert "project_id" not in marker  # never in repo files
    # Registered project-scope scripts resolve for the existing diagnostics.
    diags = hook_paths.diagnose_configured_hooks(repo / ".claude" / "settings.json", repo_root=repo)
    guard = [d for d in diags if "meridian_guard" in d["command"]]
    assert len(guard) == 4 and all(d["status"] == hook_paths.STATUS_OK for d in guard)


def test_install_is_idempotent(repo, tmp_path, shim_dir, env):
    hsm.apply_plan(_install(repo, tmp_path, shim_dir, env))
    settings = repo / ".claude" / "settings.json"
    before = settings.read_bytes()
    backups_before = sorted((tmp_path / "guard" / "backups").iterdir())
    second = _install(repo, tmp_path, shim_dir, env)
    assert not second.changed, second.render()
    assert "no changes" in second.render()
    hsm.apply_plan(second)
    assert settings.read_bytes() == before
    assert sorted((tmp_path / "guard" / "backups").iterdir()) == backups_before


def test_idempotent_even_when_user_moved_owned_entries(repo, tmp_path, shim_dir, env):
    hsm.apply_plan(_install(repo, tmp_path, shim_dir, env))
    path = repo / ".claude" / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["hooks"]["PreToolUse"].reverse()
    path.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")
    assert not _install(repo, tmp_path, shim_dir, env).changed


def test_mode_switch_updates_in_place_without_duplicates(repo, tmp_path, shim_dir, env):
    hsm.apply_plan(_install(repo, tmp_path, shim_dir, env, mode="enforce"))
    hsm.apply_plan(_install(repo, tmp_path, shim_dir, env, mode="advisory"))
    data = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
    owned = hsm.owned_entries(data)
    assert len(owned) == 4
    assert all(h["command"].startswith("$env:MERIDIAN_GUARD_DEFAULT_MODE='advisory'") for _e, _m, h in owned)
    config = json.loads((tmp_path / "guard" / "config.json").read_text(encoding="utf-8"))
    (record,) = config["installs"].values()
    assert record["mode"] == "advisory"


def test_backup_is_written_before_change(repo, tmp_path, shim_dir, env):
    original = (repo / ".claude" / "settings.json").read_bytes()
    log = hsm.apply_plan(_install(repo, tmp_path, shim_dir, env))
    backups = list((tmp_path / "guard" / "backups").iterdir())
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert log[0].startswith("backup ")
    # Backups live outside the repo, so they never show up in git status.
    assert not any(p.suffix == ".bak" for p in (repo / ".claude").rglob("*"))


def test_uninstall_restores_exact_bytes_and_removes_copies(repo, tmp_path, shim_dir, env):
    original = (repo / ".claude" / "settings.json").read_bytes()
    hsm.apply_plan(_install(repo, tmp_path, shim_dir, env, repair_launchers=False))
    plan = hsm.plan_uninstall(repo, scope="project", home=tmp_path / "home", gdir=tmp_path / "guard", env=env)
    assert plan.changed
    hsm.apply_plan(plan)
    assert (repo / ".claude" / "settings.json").read_bytes() == original
    hooks = repo / ".claude" / "hooks"
    assert [p.name for p in hooks.iterdir()] == []
    config = json.loads((tmp_path / "guard" / "config.json").read_text(encoding="utf-8"))
    assert config["installs"] == {}
    # A second uninstall is a no-op.
    assert not hsm.plan_uninstall(repo, scope="project", home=tmp_path / "home", gdir=tmp_path / "guard", env=env).changed


# ---------------------------------------------------------------------------
# PowerShell launcher repair (legacy, non-owned entries)
# ---------------------------------------------------------------------------

_LEGACY_BARE = '& "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\secret_guard.ps1"'
_FIXED = '& "$env:CLAUDE_PROJECT_DIR\\.claude\\hooks\\secret_guard.ps1"' + hsm.PS_EXIT_SUFFIX


@pytest.mark.parametrize("command,expected", [
    (_LEGACY_BARE, _FIXED),
    ('& "${CLAUDE_PROJECT_DIR}\\.claude\\hooks\\secret_guard.ps1"', _FIXED),
    ('& "$env:CLAUDE_PROJECT_DIR\\.claude\\hooks\\secret_guard.ps1"', _FIXED),
    ('  & "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\secret_guard.ps1" ; ', _FIXED),
    (_FIXED, _FIXED),  # idempotent
    # absolute single-script launcher: only the exit-code suffix is added
    ('& "C:\\Users\\me\\.claude\\hooks\\x.ps1"', '& "C:\\Users\\me\\.claude\\hooks\\x.ps1"' + hsm.PS_EXIT_SUFFIX),
    # other shapes: only the bare variable is replaced, nothing appended
    ("Write-Output $CLAUDE_PROJECT_DIR", "Write-Output $env:CLAUDE_PROJECT_DIR"),
    ("Write-Output hi", "Write-Output hi"),
])
def test_repair_powershell_command(command, expected):
    assert hsm.repair_powershell_command(command) == expected
    assert hsm.repair_powershell_command(expected) == expected


def test_repair_launchers_touches_only_powershell_commands():
    data = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Read", "hooks": [
                    {"type": "command", "shell": "powershell", "command": _LEGACY_BARE, "timeout": 7},
                    {"type": "command", "command": 'bash "$CLAUDE_PROJECT_DIR/.claude/hooks/x.sh"'},
                    {"type": "command", "shell": "powershell",
                     "command": '& "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\meridian_guard.ps1"'},
                ]},
            ],
        },
        "permissions": {"allow": ["Bash(*)"]},
    }
    new, repaired = hsm.repair_powershell_launchers(data)
    assert repaired == [(_LEGACY_BARE, _FIXED)]
    first, bash_hook, owned = new["hooks"]["PreToolUse"][0]["hooks"]
    assert first == {"type": "command", "shell": "powershell", "command": _FIXED, "timeout": 7}
    assert bash_hook == data["hooks"]["PreToolUse"][0]["hooks"][1]  # bash reads the env var itself
    assert owned == data["hooks"]["PreToolUse"][0]["hooks"][2]  # owned: merge_owned's job
    assert new["permissions"] == data["permissions"]
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == _LEGACY_BARE  # input not mutated
    _new2, owned_too = hsm.repair_powershell_launchers(data, include_owned=True)
    assert len(owned_too) == 2
    assert hsm.repair_powershell_launchers({"permissions": {}}) == ({"permissions": {}}, [])


def test_install_repairs_legacy_launchers_by_default(repo, tmp_path, shim_dir, env):
    plan = _install(repo, tmp_path, shim_dir, env)
    (change,) = plan.settings_changes
    assert "repair 3 PowerShell hook launchers" in change.reason
    assert sum("launcher repaired in" in m for m in plan.messages) == 3
    hsm.apply_plan(plan)
    after = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
    stripped, _removed = hsm.remove_owned(after)
    expected = json.loads(REPO_STYLE_SETTINGS)
    for groups in expected["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                hook["command"] = hsm.repair_powershell_command(hook["command"])
    assert stripped == expected  # only the command strings changed, nothing else
    for groups in after["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                assert "$env:CLAUDE_PROJECT_DIR" in hook["command"]
                assert hook["command"].endswith(hsm.PS_EXIT_SUFFIX)
    # idempotent, and uninstall keeps the (correct) repaired legacy entries
    assert not _install(repo, tmp_path, shim_dir, env).changed
    hsm.apply_plan(hsm.plan_uninstall(repo, scope="project", home=tmp_path / "home", gdir=tmp_path / "guard", env=env))
    assert json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8")) == expected


def test_install_repairs_settings_local_json_too(repo, tmp_path, shim_dir, env):
    local = repo / ".claude" / "settings.local.json"
    local.write_text(json.dumps({"hooks": {"Stop": [{"matcher": "", "hooks": [
        {"type": "command", "shell": "powershell", "command": _LEGACY_BARE}]}]}}, indent=2) + "\n", encoding="utf-8")
    plan = _install(repo, tmp_path, shim_dir, env)
    by_path = {ch.path: ch for ch in plan.settings_changes}
    assert "repair 1 PowerShell hook launcher" in by_path[local].reason
    hsm.apply_plan(plan)
    data = json.loads(local.read_text(encoding="utf-8"))
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == _FIXED
    no_repair = _install(repo, tmp_path, shim_dir, env, repair_launchers=False)
    assert local not in {ch.path for ch in no_repair.settings_changes}


def test_user_scope_install_repairs_user_settings(tmp_path, shim_dir, env):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    user_settings = home / ".claude" / "settings.json"
    user_settings.write_text(json.dumps({"hooks": {"Stop": [{"matcher": "", "hooks": [
        {"type": "command", "shell": "powershell", "command": '& "C:\\Users\\me\\.claude\\hooks\\stop.ps1"'}]}]}},
        indent=2) + "\n", encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()
    plan = _install(proj, tmp_path, shim_dir, env, scope="user", home=home)
    (change,) = plan.settings_changes
    assert "register user-scope" in change.reason and "repair 1 PowerShell hook launcher" in change.reason
    hsm.apply_plan(plan)
    data = json.loads(user_settings.read_text(encoding="utf-8"))
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"].endswith(hsm.PS_EXIT_SUFFIX)
    for _ev, _m, hook in hsm.owned_entries(data):
        assert hook["command"].endswith(hsm.PS_EXIT_SUFFIX)


def test_cli_dry_run_lists_launcher_repairs_and_flag_opts_out(repo, tmp_path, shim_dir, monkeypatch):
    monkeypatch.setenv(hsm.GUARD_DIR_ENV, str(tmp_path / "guard"))
    base = ["install-guard", "--repo", str(repo), "--dry-run", "--shell", "powershell", "--shim-dir", str(shim_dir)]
    out = io.StringIO()
    assert hsm.cli_main(base, stdout=out) == 0
    assert out.getvalue().count("launcher repaired in") == 3
    out = io.StringIO()
    assert hsm.cli_main(base + ["--no-repair-launchers"], stdout=out) == 0
    assert "launcher repaired in" not in out.getvalue()


def test_uninstall_keeps_shims_modified_after_install(repo, tmp_path, shim_dir, env):
    hsm.apply_plan(_install(repo, tmp_path, shim_dir, env))
    edited = repo / ".claude" / "hooks" / "meridian_guard.ps1"
    edited.write_text("# locally edited\n", encoding="ascii")
    plan = hsm.plan_uninstall(repo, scope="project", home=tmp_path / "home", gdir=tmp_path / "guard", env=env)
    hsm.apply_plan(plan)
    assert edited.is_file()
    assert any("modified since" in m for m in plan.messages)


def test_uninstall_leaves_shared_group_members(repo, tmp_path, env):
    path = repo / ".claude" / "settings.json"
    data = json.loads(REPO_STYLE_SETTINGS)
    data["hooks"]["PreToolUse"][0]["hooks"].append(
        {"type": "command", "command": '& "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\meridian_guard.ps1"'}
    )
    path.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")
    hsm.apply_plan(hsm.plan_uninstall(repo, gdir=tmp_path / "guard", home=tmp_path / "home", env=env))
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(REPO_STYLE_SETTINGS)


def test_install_into_missing_settings_creates_and_uninstall_empties(tmp_path, shim_dir, env):
    repo = tmp_path / "bare"
    repo.mkdir()
    hsm.apply_plan(_install(repo, tmp_path, shim_dir, env, shell="bash"))
    data = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert set(data) == {"hooks"}
    assert all("shell" not in h for _e, _m, h in hsm.owned_entries(data))
    assert not (tmp_path / "guard" / "backups").exists()  # nothing to back up
    hsm.apply_plan(hsm.plan_uninstall(repo, gdir=tmp_path / "guard", home=tmp_path / "home", env=env))
    assert json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8")) == {}


def test_install_removes_duplicate_registration_from_settings_local(repo, tmp_path, shim_dir, env):
    local = repo / ".claude" / "settings.local.json"
    local.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Bash(git status)"]},
                "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                    {"type": "command", "command": '& "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\meridian_guard.ps1"'}
                ]}]},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    plan = _install(repo, tmp_path, shim_dir, env)
    assert any(ch.path == local for ch in plan.settings_changes)
    hsm.apply_plan(plan)
    assert json.loads(local.read_text(encoding="utf-8")) == {"permissions": {"allow": ["Bash(git status)"]}}


def test_preserves_automemory_setting_untouched(repo, tmp_path, shim_dir, env):
    path = repo / ".claude" / "settings.json"
    data = json.loads(REPO_STYLE_SETTINGS)
    data["autoMemoryEnabled"] = True
    path.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")
    hsm.apply_plan(_install(repo, tmp_path, shim_dir, env))
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["autoMemoryEnabled"] is True


def test_crlf_and_bom_are_preserved(tmp_path, shim_dir, env):
    repo = tmp_path / "crlf"
    (repo / ".claude").mkdir(parents=True)
    text = json.dumps({"permissions": {"allow": ["Read(*)"]}}, indent=2).replace("\n", "\r\n") + "\r\n"
    path = repo / ".claude" / "settings.json"
    path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    hsm.apply_plan(_install(repo, tmp_path, shim_dir, env))
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    hsm.apply_plan(hsm.plan_uninstall(repo, gdir=tmp_path / "guard", home=tmp_path / "home", env=env))
    assert path.read_bytes() == b"\xef\xbb\xbf" + text.encode("utf-8")


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[1, 2]",
        '{"hooks": []}',
        '{"hooks": {"PreToolUse": {}}}',
        '{"hooks": {"PreToolUse": ["x"]}}',
        '{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": "nope"}]}}',
    ],
)
def test_malformed_settings_abort_without_writing(tmp_path, shim_dir, env, content):
    repo = tmp_path / "bad"
    (repo / ".claude").mkdir(parents=True)
    path = repo / ".claude" / "settings.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(hsm.GuardInstallError):
        _install(repo, tmp_path, shim_dir, env)
    with pytest.raises(hsm.GuardInstallError):
        hsm.plan_uninstall(repo, gdir=tmp_path / "guard", home=tmp_path / "home", env=env)
    assert path.read_text(encoding="utf-8") == content
    assert not (tmp_path / "guard").exists()
    assert not (repo / ".claude" / "hooks").exists()


def test_invalid_utf8_settings_abort(tmp_path, shim_dir, env):
    repo = tmp_path / "bin"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_bytes(b'{"a": "\xff"}')
    with pytest.raises(hsm.GuardInstallError, match="UTF-8"):
        _install(repo, tmp_path, shim_dir, env)


def test_empty_settings_file_is_treated_as_empty_object(tmp_path, shim_dir, env):
    repo = tmp_path / "empty"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text("  \n", encoding="utf-8")
    hsm.apply_plan(_install(repo, tmp_path, shim_dir, env))
    assert len(hsm.owned_entries(json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8")))) == 4


@pytest.fixture()
def no_default_shims(tmp_path, monkeypatch):
    """Point the packaged/checkout shim candidates at an empty fake tree so a
    test does not pick up real shims once they are committed to .claude/hooks."""
    fake = tmp_path / "fake_pkg" / "meridian"
    fake.mkdir(parents=True)
    monkeypatch.setattr(hsm, "__file__", str(fake / "hook_settings_merge.py"))


def test_missing_shims_is_an_error(tmp_path, env, no_default_shims):
    repo = tmp_path / "noshim"
    repo.mkdir()
    empty = tmp_path / "empty_src"
    empty.mkdir()
    with pytest.raises(hsm.GuardInstallError, match="not found"):
        hsm.plan_install(repo, shell="powershell", gdir=tmp_path / "guard", shim_source=empty,
                         env={**env, hsm.SHIM_DIR_ENV: str(empty)}, home=tmp_path / "home")


def test_non_ascii_ps1_shim_is_refused(repo, tmp_path, shim_dir, env):
    (shim_dir / "meridian_guard.ps1").write_bytes("# em dash \u2014\n".encode("utf-8"))
    with pytest.raises(hsm.GuardInstallError, match="ASCII"):
        _install(repo, tmp_path, shim_dir, env)


def test_dogfood_repo_shims_are_never_copied_or_deleted(tmp_path, env):
    repo = tmp_path / "meridian_checkout"
    hooks = repo / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    for base in (hsm.SHIM_PRE, hsm.SHIM_POST, hsm.SHIM_BRIEF):
        (hooks / f"{base}.ps1").write_text("exit 0\n", encoding="ascii")
    plan = hsm.plan_install(repo, shell="powershell", gdir=tmp_path / "guard", shim_source=hooks,
                            env=env, home=tmp_path / "home")
    assert not any(op.kind == "copy" for op in plan.file_ops)
    assert not any(op.path.name == hsm.INSTALL_MARKER_NAME for op in plan.file_ops)
    assert any("dogfood" in m for m in plan.messages)
    hsm.apply_plan(plan)
    hsm.apply_plan(hsm.plan_uninstall(repo, gdir=tmp_path / "guard", home=tmp_path / "home", env=env))
    assert sorted(p.name for p in hooks.iterdir()) == ["meridian_guard.ps1", "meridian_guard_brief.ps1", "meridian_guard_post.ps1"]


def test_shims_already_in_target_are_used_when_no_source(tmp_path, env, no_default_shims):
    repo = tmp_path / "prestaged"
    hooks = repo / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    for base in (hsm.SHIM_PRE, hsm.SHIM_POST, hsm.SHIM_BRIEF):
        (hooks / f"{base}.sh").write_text("exit 0\n", encoding="ascii")
    empty = tmp_path / "nothing"
    empty.mkdir()
    plan = hsm.plan_install(repo, shell="bash", gdir=tmp_path / "guard", shim_source=empty,
                            env={**env, hsm.SHIM_DIR_ENV: str(empty)}, home=tmp_path / "home")
    assert any("already present" in m for m in plan.messages)
    assert plan.settings_changes


def test_project_id_recorded_in_guard_config_only(repo, tmp_path, shim_dir, env):
    (repo / "CLAUDE.local.md").write_text(
        "Project ID: 5787cc92-ba7d-4788-b17c-28ab7938b839\n", encoding="utf-8"
    )
    hsm.apply_plan(_install(repo, tmp_path, shim_dir, env))
    config = json.loads((tmp_path / "guard" / "config.json").read_text(encoding="utf-8"))
    (key, record), = config["installs"].items()
    assert key.startswith("project:")
    assert record["project_id"] == "5787cc92-ba7d-4788-b17c-28ab7938b839"
    assert record["project_id_source"] == "CLAUDE.local.md"
    assert config["runtime"]["python"] == sys.executable
    settings_text = (repo / ".claude" / "settings.json").read_text(encoding="utf-8")
    assert "5787cc92" not in settings_text


def test_resolve_repo_project_id_reads_only_project_section(tmp_path):
    repo = tmp_path / "toml_repo"
    repo.mkdir()
    (repo / "meridian.toml").write_text(
        '[default]\nconnection = "x"\n[connections.prod]\nproject_id = "not-this-one"\n'
        '[project]\n# comment\nproject_id = "abc-123"\n',
        encoding="utf-8",
    )
    assert hsm.resolve_repo_project_id(repo) == ("abc-123", "meridian.toml")
    assert hsm.resolve_repo_project_id(repo, {"MERIDIAN_PROJECT_ID": "env-id"}) == ("env-id", "env")
    other = tmp_path / "none"
    other.mkdir()
    assert hsm.resolve_repo_project_id(other) == (None, "unresolved")


def test_guard_dir_resolution():
    assert hsm.guard_dir({hsm.GUARD_DIR_ENV: "/x/y"}) == Path("/x/y")
    assert hsm.guard_dir({"LOCALAPPDATA": "/la"}) == Path("/la") / "meridian" / "guard"
    assert hsm.guard_dir({"XDG_STATE_HOME": "/xs"}) == Path("/xs") / "meridian" / "guard"
    assert hsm.guard_dir({}).parts[-2:] == ("meridian", "guard")


# ---------------------------------------------------------------------------
# User scope + dedupe
# ---------------------------------------------------------------------------


def _write_user_settings(home: Path) -> Path:
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(USER_STYLE_SETTINGS, indent=2) + "\n", encoding="utf-8")
    return path


def test_user_scope_preserves_cbm_hooks_and_installs_only_g6_g8_and_brief(repo, tmp_path, shim_dir, env):
    home = tmp_path / "home"
    path = _write_user_settings(home)
    original = path.read_bytes()
    plan = _install(repo, tmp_path, shim_dir, env, scope="user", home=home)
    hsm.apply_plan(plan)
    data = json.loads(path.read_text(encoding="utf-8"))
    stripped, removed = hsm.remove_owned(data)
    assert removed == 3
    assert stripped == USER_STYLE_SETTINGS
    owned = hsm.owned_entries(data)
    assert {e for e, _m, _h in owned} == {"PreToolUse", "SessionStart", "SubagentStart"}
    pre = [m for e, m, _h in owned if e == "PreToolUse"]
    assert pre == [hsm.PRE_MATCHER_USER]
    hooks = home / ".claude" / "hooks"
    assert (hooks / "meridian_guard_defer.ps1").read_text(encoding="ascii") == hsm.DEFER_PS1
    assert (hooks / "meridian_guard_defer.sh").is_file()
    assert (hooks / "meridian_guard.ps1").is_file() and (hooks / "meridian_guard_brief.ps1").is_file()
    # Idempotent, then an exact uninstall.
    assert not _install(repo, tmp_path, shim_dir, env, scope="user", home=home).changed
    hsm.apply_plan(hsm.plan_uninstall(repo, scope="user", home=home, gdir=tmp_path / "guard", env=env))
    assert path.read_bytes() == original
    assert not any(p.name.startswith("meridian_guard") for p in hooks.iterdir())


def test_user_scope_notes_project_guard_dedupe(repo, tmp_path, shim_dir, env):
    home = tmp_path / "home"
    _write_user_settings(home)
    hsm.apply_plan(_install(repo, tmp_path, shim_dir, env))  # project-scope guard in repo
    plan = _install(repo, tmp_path, shim_dir, env, scope="user", home=home)
    assert any("defer" in m for m in plan.messages)


def test_user_scope_falls_back_to_repo_shims_then_errors(tmp_path, env, no_default_shims):
    home = tmp_path / "home"
    repo = tmp_path / "src_repo"
    hooks = repo / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    empty = tmp_path / "void"
    empty.mkdir()
    kw = dict(scope="user", shell="powershell", home=home, gdir=tmp_path / "guard",
              shim_source=empty, env={**env, hsm.SHIM_DIR_ENV: str(empty)})
    with pytest.raises(hsm.GuardInstallError, match="--scope user"):
        hsm.plan_install(repo, **kw)
    for base in (hsm.SHIM_PRE, hsm.SHIM_BRIEF):
        (hooks / f"{base}.ps1").write_text("exit 0\n", encoding="ascii")
    plan = hsm.plan_install(repo, **kw)
    copies = sorted(op.path.name for op in plan.file_ops if op.kind == "copy")
    assert copies == ["meridian_guard.ps1", "meridian_guard_brief.ps1"]


def test_invalid_arguments_are_rejected(tmp_path, env):
    for kw in ({"scope": "global"}, {"mode": "strict"}, {"shell": "zsh"}):
        with pytest.raises(hsm.GuardInstallError):
            hsm.plan_install(tmp_path, gdir=tmp_path / "g", env=env, **kw)
    with pytest.raises(hsm.GuardInstallError):
        hsm.plan_uninstall(tmp_path, scope="global", gdir=tmp_path / "g", env=env)
    with pytest.raises(hsm.GuardInstallError):
        hsm.plan_uninstall(tmp_path / "missing", gdir=tmp_path / "g", env=env)


def test_tab_indented_settings_keep_tabs(tmp_path, shim_dir, env):
    repo = tmp_path / "tabs"
    (repo / ".claude").mkdir(parents=True)
    path = repo / ".claude" / "settings.json"
    original = json.dumps({"permissions": {"allow": ["Read(*)"]}}, indent="\t") + "\n"
    path.write_text(original, encoding="utf-8")
    hsm.apply_plan(_install(repo, tmp_path, shim_dir, env))
    assert '\n\t"hooks": {' in path.read_text(encoding="utf-8")
    hsm.apply_plan(hsm.plan_uninstall(repo, gdir=tmp_path / "guard", home=tmp_path / "home", env=env))
    assert path.read_text(encoding="utf-8") == original


def test_backup_names_never_collide(repo, tmp_path, shim_dir, env):
    import datetime as dt

    fixed = dt.datetime(2026, 9, 26, 12, 0, 0, tzinfo=dt.timezone.utc)
    hsm.apply_plan(_install(repo, tmp_path, shim_dir, env), now=fixed)
    hsm.apply_plan(hsm.plan_uninstall(repo, gdir=tmp_path / "guard", home=tmp_path / "home", env=env), now=fixed)
    backups = sorted(p.name for p in (tmp_path / "guard" / "backups").iterdir())
    assert len(backups) == 2 and backups[1] == backups[0] + ".1"


def test_has_project_scope_guard_ignores_user_scope_entries(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    user_cmd = hsm.build_command("meridian_guard", scope="user", mode="enforce", shell="powershell",
                                 user_hooks_dir=tmp_path / "h")
    (proj / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": user_cmd}]}]}}),
        encoding="utf-8",
    )
    assert not hsm.has_project_scope_guard(proj)  # e.g. CLAUDE_PROJECT_DIR == home
    (proj / ".claude" / "settings.local.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
            "command": hsm.build_command("meridian_guard", scope="project", mode="enforce", shell="bash")}]}]}}),
        encoding="utf-8",
    )
    assert hsm.has_project_scope_guard(proj)
    (proj / ".claude" / "settings.json").write_text("{bad", encoding="utf-8")
    assert hsm.has_project_scope_guard(proj)  # malformed file skipped, local still counts


def _powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


@pytest.mark.subprocess_isolated
@pytest.mark.skipif(_powershell() is None, reason="PowerShell not available")
def test_defer_ps1_parses_cleanly(tmp_path):
    path = tmp_path / "meridian_guard_defer.ps1"
    path.write_text(hsm.DEFER_PS1, encoding="ascii")
    cmd = (
        "$e=$null; [System.Management.Automation.Language.Parser]::ParseFile("
        f"'{path}',[ref]$null,[ref]$e) | Out-Null; $e.Count"
    )
    out = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-Command", cmd],
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "0", out.stderr


def _user_scope_fixture(tmp_path: Path, ext: str) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / f"meridian_guard_defer.{ext}").write_text(hsm.DEFER_PS1 if ext == "ps1" else hsm.DEFER_SH, encoding="ascii")
    if ext == "ps1":
        (hooks / "meridian_guard.ps1").write_text(
            "$raw = [Console]::In.ReadToEnd()\n"
            "[Console]::Out.Write('RAN scope=' + $env:MERIDIAN_GUARD_SCOPE + ' payload=' + $raw.Trim())\n"
            "exit 0\n",
            encoding="ascii",
        )
    else:
        (hooks / "meridian_guard.sh").write_text(
            '#!/usr/bin/env bash\nraw="$(cat)"\nprintf "RAN scope=%s payload=%s" "$MERIDIAN_GUARD_SCOPE" "$raw"\n',
            encoding="ascii",
        )
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    return hooks, proj, home


def _register_project_guard(proj: Path) -> None:
    (proj / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
            "command": hsm.build_command("meridian_guard", scope="project", mode="enforce", shell="powershell")}]}]}},
            indent=2),
        encoding="utf-8",
    )


@pytest.mark.subprocess_isolated
@pytest.mark.skipif(_powershell() is None, reason="PowerShell not available")
def test_user_scope_powershell_command_runs_shim_then_defers(tmp_path):
    hooks, proj, _home = _user_scope_fixture(tmp_path, "ps1")
    cmd = hsm.build_command("meridian_guard", scope="user", mode="enforce", shell="powershell", user_hooks_dir=hooks)
    run_env = {**os.environ, "CLAUDE_PROJECT_DIR": str(proj)}
    argv = [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", cmd]
    first = subprocess.run(argv, input='{"tool_name":"Write"}', capture_output=True, text=True, timeout=90, env=run_env)
    assert first.returncode == 0, first.stderr
    assert first.stdout == 'RAN scope=user payload={"tool_name":"Write"}'
    _register_project_guard(proj)
    second = subprocess.run(argv, input='{"tool_name":"Write"}', capture_output=True, text=True, timeout=90, env=run_env)
    assert second.returncode == 0, second.stderr
    assert second.stdout == ""


def _native_bash() -> str | None:
    """Absolute path of a bash that understands this OS's paths. A bare
    ``bash`` argv on Windows resolves through System32 to WSL bash, where
    ``C:/...`` paths do not exist, so the resolved Git Bash path is used and a
    System32 (WSL) bash is skipped."""
    path = shutil.which("bash")
    if path is None:
        return None
    if sys.platform == "win32" and "system32" in path.lower():
        return None
    return path


@pytest.mark.subprocess_isolated
@pytest.mark.skipif(_native_bash() is None, reason="native (non-WSL) bash not available")
def test_user_scope_bash_command_runs_shim_then_defers(tmp_path):
    hooks, proj, _home = _user_scope_fixture(tmp_path, "sh")
    cmd = hsm.build_command("meridian_guard", scope="user", mode="enforce", shell="bash", user_hooks_dir=hooks)
    proj_posix = str(proj).replace("\\", "/")
    script = f'export CLAUDE_PROJECT_DIR="{proj_posix}"; {cmd}'
    bash = _native_bash()
    first = subprocess.run([bash, "-c", script], input='{"tool_name":"Write"}', capture_output=True, text=True, timeout=90)
    assert first.returncode == 0, first.stderr
    assert first.stdout == 'RAN scope=user payload={"tool_name":"Write"}'
    _register_project_guard(proj)
    second = subprocess.run([bash, "-c", script], input='{"tool_name":"Write"}', capture_output=True, text=True, timeout=90)
    assert second.returncode == 0, second.stderr
    assert second.stdout == ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_dry_run_writes_nothing(repo, tmp_path, shim_dir, monkeypatch):
    monkeypatch.setenv(hsm.GUARD_DIR_ENV, str(tmp_path / "guard"))
    before = (repo / ".claude" / "settings.json").read_bytes()
    out, err = io.StringIO(), io.StringIO()
    rc = hsm.cli_main(
        ["install-guard", "--repo", str(repo), "--dry-run", "--shell", "powershell", "--shim-dir", str(shim_dir)],
        stdout=out, stderr=err,
    )
    assert rc == 0, err.getvalue()
    text = out.getvalue()
    assert text.startswith("[dry-run] nothing written")
    assert "+" in text and "meridian_guard.ps1" in text and "--- " in text
    assert (repo / ".claude" / "settings.json").read_bytes() == before
    assert not (repo / ".claude" / "hooks").exists()
    assert not (tmp_path / "guard").exists()


def test_cli_install_uninstall_roundtrip(repo, tmp_path, shim_dir, monkeypatch):
    monkeypatch.setenv(hsm.GUARD_DIR_ENV, str(tmp_path / "guard"))
    before = (repo / ".claude" / "settings.json").read_bytes()
    out = io.StringIO()
    assert hsm.cli_main(["install-guard", "--repo", str(repo), "--shell", "bash", "--mode", "advisory",
                         "--shim-dir", str(shim_dir), "--no-repair-launchers"], stdout=out) == 0
    assert "wrote" in out.getvalue()
    out2 = io.StringIO()
    assert hsm.cli_main(["install-guard", "--repo", str(repo), "--shell", "bash", "--mode", "advisory",
                         "--shim-dir", str(shim_dir), "--no-repair-launchers"], stdout=out2) == 0
    assert "no changes" in out2.getvalue()
    assert hsm.cli_main(["install-guard", "--repo", str(repo), "--uninstall"], stdout=io.StringIO()) == 0
    assert (repo / ".claude" / "settings.json").read_bytes() == before


def test_cli_reports_errors_with_exit_1(tmp_path, monkeypatch):
    monkeypatch.setenv(hsm.GUARD_DIR_ENV, str(tmp_path / "guard"))
    err = io.StringIO()
    assert hsm.cli_main(["install-guard", "--repo", str(tmp_path / "missing")], stderr=err) == 1
    assert "not a directory" in err.getvalue()


def test_main_dispatches_hooks_subcommand(repo, tmp_path, shim_dir, monkeypatch, capsys):
    monkeypatch.setenv(hsm.GUARD_DIR_ENV, str(tmp_path / "guard"))
    rc = meridian_main(["hooks", "install-guard", "--repo", str(repo), "--dry-run", "--shim-dir", str(shim_dir)])
    assert rc == 0
    assert "[dry-run]" in capsys.readouterr().out


def test_dispatch_leaves_server_flags_alone():
    assert _dispatch_subcommand([]) is None
    assert _dispatch_subcommand(["--port", "7999"]) is None
    assert _dispatch_subcommand(["--mcp"]) is None
    assert _dispatch_subcommand(["--host", "0.0.0.0", "hooks"]) is None
