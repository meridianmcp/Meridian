"""55d48d69 -- integration of the Meridian guard into this repo (dogfood).

Covers what only exists once the guard branches (core, shims, brief, cli) are
merged together:

1. The repo-tracked ``.claude/settings.json`` registers exactly the entries
   ``python -m meridian hooks install-guard`` would write (the file was
   generated with ``meridian/hook_settings_merge.py``), never matches Read,
   carries explicit timeouts, no longer registers the superseded
   code_intel_guard and widens the shell-only companion guards to the
   PowerShell tool.
2. The installer -> shim contract: the env vars ``install-guard`` prefixes to
   a hook command (``--mode advisory``, ``--scope user``) are the ones the
   decision core reads. (The shims' parity on these is pinned by the G0 rows
   of tests/fixtures/guard_cases.json.)
3. ``hook_paths`` guard diagnostics and the ``guard`` block of
   ``GET /hooks/diagnostics``.
4. The companion guards (secret_guard, dependency_install_guard,
   pkg_install_guard) really act on PowerShell-tool payloads, in both the
   .ps1 and the .sh variant.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from meridian import guard_core as gc
from meridian import hook_paths
from meridian import hook_settings_merge as hsm

_REPO = Path(__file__).resolve().parent.parent
_SETTINGS = _REPO / ".claude" / "settings.json"
_HOOKS = _REPO / ".claude" / "hooks"


def _settings() -> dict:
    return json.loads(_SETTINGS.read_text(encoding="utf-8"))


def _groups(event: str) -> list[dict]:
    return _settings()["hooks"].get(event) or []


def _group_for(event: str, needle: str) -> list[dict]:
    return [g for g in _groups(event) if needle in json.dumps(g.get("hooks", []))]


# ---------------------------------------------------------------------------
# 1. settings.json (dogfood install)
# ---------------------------------------------------------------------------


def test_settings_guard_entries_equal_the_installer_output():
    """The tracked file IS an install-guard result: merging the installer's
    desired entries into it again changes nothing."""
    data = _settings()
    desired = hsm.desired_entries(scope="project", mode="enforce", shell="powershell")
    merged, changed = hsm.merge_owned(data, desired)
    assert changed is False
    assert merged is data
    owned = hsm.owned_entries(data)
    assert sorted((e, m) for e, m, _h in owned) == sorted((e, m) for e, m, _h in desired)


def test_settings_guard_registration_shape():
    pre = _group_for("PreToolUse", "meridian_guard.ps1")
    post = _group_for("PostToolUse", "meridian_guard_post.ps1")
    start = _group_for("SessionStart", "meridian_guard_brief.ps1")
    sub = _group_for("SubagentStart", "meridian_guard_brief.ps1")
    assert [len(pre), len(post), len(start), len(sub)] == [1, 1, 1, 1]
    for group, timeout, script in (
        (pre, 3, "meridian_guard.ps1"),
        (post, 3, "meridian_guard_post.ps1"),
        (start, 10, "meridian_guard_brief.ps1"),
        (sub, 10, "meridian_guard_brief.ps1"),
    ):
        matching_hooks = [
            hook for hook in group[0]["hooks"]
            if script in hook.get("command", "")
        ]
        assert len(matching_hooks) == 1
        (hook,) = matching_hooks
        assert hook["type"] == "command"
        assert hook["shell"] == "powershell"
        # $env: form -- the bare $CLAUDE_PROJECT_DIR is an unset PowerShell variable
        # under Claude Code's -Command invocation (test_hook_registered_commands.py).
        assert hook["command"].startswith('& "$env:CLAUDE_PROJECT_DIR\\.claude\\hooks\\meridian_guard')
        assert hook["command"].endswith(hsm.PS_EXIT_SUFFIX)
        assert hook["timeout"] == timeout
        assert hook_paths.is_project_relative_command(hook["command"])
    assert start[0]["matcher"] == "startup|resume|clear|compact"
    assert sub[0]["matcher"] == "*"


def test_settings_guard_matchers_never_match_read_but_cover_every_rule():
    """Read is never matched (design: reading a located file is correct and
    Read is the hottest tool). Each rule family's tools are covered."""
    pre = _group_for("PreToolUse", "meridian_guard.ps1")[0]["matcher"]
    post = _group_for("PostToolUse", "meridian_guard_post.ps1")[0]["matcher"]
    for matcher in (pre, post):
        assert not re.fullmatch(matcher, "Read"), matcher
    for tool in (
        "Grep", "Glob",                                   # G1 / G2
        "Bash", "PowerShell", "Monitor",                  # G3 / G7 / G9
        "mcp__dc__start_process", "mcp__dc__start_search",  # G3 / G4
        "mcp__codebase-memory-mcp__search_graph",         # G5
        "mcp__codebase-memory__search_code",              # G5 (Meridian repo prefix)
        "Write", "Edit", "MultiEdit", "NotebookEdit",     # G6 / G9 / G10
        "mcp__dc__write_file", "mcp__meridian__patch_file",
        "mcp__serena__write_memory", "mcp__meridian-extract__edit_memory",  # G8
        "WebSearch", "WebFetch",                          # G11
    ):
        assert re.fullmatch(pre, tool), f"PreToolUse guard must match {tool}"
    for tool in (
        "WebSearch", "WebFetch",                          # G12
        "mcp__codebase-memory-mcp__search_code", "mcp__serena__find_symbol",  # G13
        "mcp__meridian__paper_search", "mcp__meridian__capture_research_finding",
        "mcp__meridian__start_session", "mcp__98ff5a3a-9b9d-4075-8d6e-306ff084c0eb__get_sprint_items",  # G14
    ):
        assert re.fullmatch(post, tool), f"PostToolUse guard must match {tool}"


def test_settings_every_timeout_is_explicit_and_at_most_ten_seconds_for_the_guard():
    for event in ("PreToolUse", "PostToolUse", "SessionStart", "SubagentStart"):
        for group in _groups(event):
            for hook in group.get("hooks", []):
                if "meridian_guard" in hook.get("command", ""):
                    assert 0 < hook["timeout"] <= 10


def test_settings_code_intel_guard_superseded_and_companions_widened():
    data = _settings()
    assert "code_intel_guard" not in json.dumps(data)
    for name in ("secret_guard", "dependency_install_guard", "pkg_install_guard"):
        (group,) = _group_for("PreToolUse", f"{name}.ps1")
        tools = group["matcher"].split("|")
        assert "Bash" in tools and "PowerShell" in tools, (name, group["matcher"])
    # 93d1e8f9 (55d48d69 confirm pass): secret_guard's matcher was intentionally
    # widened to include Write|Edit|MultiEdit -- without them, a real credential
    # VALUE could be written straight into a sensitive file (e.g. meridian.toml)
    # with zero interception, even though reading the same file was blocked.
    assert _group_for("PreToolUse", "secret_guard.ps1")[0]["matcher"] == "Read|Bash|PowerShell|Grep|Glob|Write|Edit|MultiEdit"


def test_settings_leaves_owner_switches_alone():
    """autoMemoryEnabled belongs to item 81f403aa (after an owner-approved
    import); nothing here may disable hooks or set the kill switch."""
    text = _SETTINGS.read_text(encoding="utf-8")
    for key in ("autoMemoryEnabled", "disableAllHooks", "MERIDIAN_GUARD"):
        assert key not in text, key
    compact = [g for g in _groups("SessionStart") if g.get("matcher") == "compact"]
    assert len(compact) == 1 and "post_compact_refresh.ps1" in json.dumps(compact)


def test_settings_every_registered_script_exists_and_resolves():
    for diag in hook_paths.diagnose_configured_hooks(_SETTINGS, repo_root=_REPO):
        assert diag["required"] is True, diag
        assert diag["status"] == hook_paths.STATUS_OK, diag


def test_code_intel_guard_scripts_kept_and_marked_deprecated():
    for ext in ("ps1", "sh"):
        path = _HOOKS / f"code_intel_guard.{ext}"
        assert path.is_file()
        assert "DEPRECATED (55d48d69)" in path.read_text(encoding="utf-8")[:700]
    (_HOOKS / "code_intel_guard.ps1").read_bytes().decode("ascii")


def test_gitignore_keeps_codex_out_of_code_intel_indexes():
    lines = [ln.strip() for ln in (_REPO / ".gitignore").read_text(encoding="utf-8-sig").splitlines()]
    assert ".codex/" in lines


def test_agents_md_documents_the_guard_and_its_kill_switch():
    text = (_REPO / "AGENTS.md").read_text(encoding="utf-8")
    start = text.index("## Meridian guard")
    section = text[start:text.index("\n## ", start + 1)]
    for needle in ("MERIDIAN_GUARD=off", "MERIDIAN_GUARD_DISABLE", "guard.off", "install-guard",
                   "updatedInput", "permissionDecision"):
        assert needle in section, needle


# ---------------------------------------------------------------------------
# 2. installer -> decision core contract
# ---------------------------------------------------------------------------

_PS_ENV_RE = re.compile(r"\$env:(\w+)='([^']*)'")
_SH_ENV_RE = re.compile(r"(?:^|;\s*|\s)([A-Z_]+)=(\S+)\s")


def _command_env(command: str) -> dict[str, str]:
    env = dict(_PS_ENV_RE.findall(command))
    env.update(_SH_ENV_RE.findall(command))
    return env


def test_installer_env_names_are_the_ones_the_core_reads():
    assert hsm.DEFAULT_MODE_ENV == gc.DEFAULT_MODE_ENV == "MERIDIAN_GUARD_DEFAULT_MODE"
    assert hsm.SCOPE_ENV == gc.SCOPE_ENV == "MERIDIAN_GUARD_SCOPE"
    assert gc.USER_SCOPE_RULES == {"G0", "G6", "G7", "G8", "G15", "G16"}


@pytest.mark.parametrize("shell", ["powershell", "bash"])
def test_mode_advisory_install_makes_the_core_advisory(shell, tmp_path):
    cmd = hsm.build_command(hsm.SHIM_PRE, scope="project", mode="advisory", shell=shell)
    env = _command_env(cmd)
    assert env == {"MERIDIAN_GUARD_DEFAULT_MODE": "advisory"}
    base = {"LOCALAPPDATA": str(tmp_path)}
    assert gc.guard_mode(base) == "enforce"
    assert gc.guard_mode({**base, **env}) == "advisory"
    # The owner's explicit MERIDIAN_GUARD still wins over the installed default.
    assert gc.guard_mode({**base, **env, "MERIDIAN_GUARD": "enforce"}) == "enforce"
    assert gc.guard_mode({**base, **env, "MERIDIAN_GUARD": "off"}) == "off"
    # Enforce installs add nothing.
    plain = hsm.build_command(hsm.SHIM_PRE, scope="project", mode="enforce", shell=shell)
    assert _command_env(plain) == {}


@pytest.mark.parametrize("shell", ["powershell", "bash"])
def test_user_scope_install_limits_the_core_to_g6_g8(shell, tmp_path):
    cmd = hsm.build_command(hsm.SHIM_PRE, scope="user", mode="enforce", shell=shell,
                            user_hooks_dir=tmp_path / "hooks")
    env = _command_env(cmd)
    assert env.get("MERIDIAN_GUARD_SCOPE") == "user"
    disabled = gc.disabled_rules(env)
    assert {"G6", "G7", "G8", "G15", "G16"}.isdisjoint(disabled)
    assert {"G1", "G2", "G3", "G4", "G5", "G9", "G10", "G11", "G12", "G13", "G14"} <= disabled
    # MERIDIAN_GUARD_DISABLE still composes with the scope.
    assert "G6" in gc.disabled_rules({**env, "MERIDIAN_GUARD_DISABLE": "G6"})
    assert gc.disabled_rules({"MERIDIAN_GUARD_SCOPE": "project"}) == set()


# ---------------------------------------------------------------------------
# 3. hook_paths guard diagnostics + GET /hooks/diagnostics
# ---------------------------------------------------------------------------


def _which(available: set[str]):
    return lambda name: (f"/usr/bin/{name}" if name in available else None)


def test_real_settings_guard_diagnostics_are_complete(monkeypatch):
    monkeypatch.setenv("MERIDIAN_GUARD_PYTHON", sys.executable)
    diags = hook_paths.diagnose_configured_hooks(
        _SETTINGS, repo_root=_REPO, which=_which({"powershell"})
    )
    guard = [d for d in diags if "guard" in d]
    assert sorted(d["guard"]["component"] for d in guard) == ["brief", "brief", "post", "pre"]
    for d in guard:
        assert d["guard"]["scope"] == "project"
        assert d["guard"]["installed_mode"] == "enforce"
        assert d["guard"]["runtime_ok"] is True, d
    assert all("guard" not in d for d in diags if "meridian_guard" not in d["command"])
    summary = hook_paths.summarize_guard(diags)
    assert summary == {
        "registered": True, "components": ["brief", "post", "pre"], "complete": True,
        "runtime_ok": True, "problems": [],
    }


def test_user_scope_command_diagnoses_the_shim_not_the_defer_check(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("meridian_guard.ps1", "meridian_guard_defer.ps1"):
        (hooks / name).write_text("# x", encoding="ascii")
    cmd = hsm.build_command(hsm.SHIM_PRE, scope="user", mode="advisory", shell="powershell",
                            user_hooks_dir=hooks)
    assert hook_paths.extract_script_path_token(cmd).endswith("meridian_guard_defer.ps1")
    assert hook_paths.guard_script_token(cmd).endswith("meridian_guard.ps1")
    diag = hook_paths.diagnose_guard_command(cmd, None, env={}, which=_which({"powershell"}))
    assert diag["component"] == "pre"
    assert diag["scope"] == "user"
    assert diag["installed_mode"] == "advisory"
    assert diag["runtime_ok"] is True, diag


def test_guard_runtime_missing_is_reported(tmp_path):
    hooks = tmp_path / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "meridian_guard.sh").write_text("#!/bin/sh\n", encoding="ascii")
    (hooks / "meridian_guard.ps1").write_text("# x", encoding="ascii")
    (hooks / "meridian_guard_brief.ps1").write_text("# x", encoding="ascii")
    sh_cmd = 'bash "$CLAUDE_PROJECT_DIR/.claude/hooks/meridian_guard.sh"'
    diag = hook_paths.diagnose_guard_command(sh_cmd, tmp_path, env={}, which=_which({"bash"}))
    assert diag["runtime_missing"] == ["awk", "meridian_guard.awk"]
    assert diag["runtime_ok"] is False
    ps_cmd = '& "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\meridian_guard.ps1"'
    diag = hook_paths.diagnose_guard_command(ps_cmd, tmp_path, env={}, which=_which(set()))
    assert diag["runtime_missing"] == ["powershell"]
    brief = '& "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\meridian_guard_brief.ps1"'
    diag = hook_paths.diagnose_guard_command(
        brief, tmp_path, env={"MERIDIAN_GUARD_PYTHON": str(tmp_path / "missing.exe")},
        which=_which({"powershell", "py"}),
    )
    assert diag["runtime_missing"] == ["python"], "an explicit MERIDIAN_GUARD_PYTHON never falls through"
    diag = hook_paths.diagnose_guard_command(
        '& "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\meridian_guard_post.ps1"', tmp_path,
        env={}, which=_which({"powershell"}),
    )
    assert diag["runtime_missing"] == ["script"]
    summary = hook_paths.summarize_guard([{"event": "PreToolUse", "guard": diag}])
    assert summary["runtime_ok"] is False and summary["complete"] is False
    assert summary["problems"] == ["PreToolUse: post missing script"]


def test_brief_python_resolution_order(tmp_path):
    gdir = tmp_path / "guard"
    gdir.mkdir()
    recorded = tmp_path / "recorded-python.exe"
    recorded.write_text("", encoding="ascii")
    (gdir / "config.json").write_text(json.dumps({"runtime": {"python": str(recorded)}}), encoding="utf-8")
    brief = '& "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\meridian_guard_brief.ps1"'
    diag = hook_paths.diagnose_guard_command(brief, tmp_path, guard_dir=gdir, env={}, which=_which({"py"}))
    assert (diag["python"], diag["python_source"]) == (str(recorded), "config.json")
    diag = hook_paths.diagnose_guard_command(brief, tmp_path, guard_dir=None, env={}, which=_which({"py"}))
    assert (diag["python"], diag["python_source"]) == ("/usr/bin/py", "py")


def test_hooks_diagnostics_route_reports_guard_status(client, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    hooks = repo / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    for name in ("meridian_guard.ps1", "meridian_guard_post.ps1", "meridian_guard_brief.ps1"):
        (hooks / name).write_text("# x", encoding="ascii")
    settings = {"hooks": {}}
    for event, matcher, hook in hsm.desired_entries(scope="project", mode="enforce", shell="powershell"):
        settings["hooks"].setdefault(event, []).append({"matcher": matcher, "hooks": [hook]})
    (repo / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setattr(hook_paths, "resolve_active_repo_root", lambda **kwargs: repo)
    monkeypatch.setattr(hook_paths.shutil, "which", _which({"powershell", "pwsh"}))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    monkeypatch.setenv("MERIDIAN_GUARD_DIR", str(tmp_path / "gdir"))
    monkeypatch.setenv("MERIDIAN_GUARD_PYTHON", sys.executable)
    monkeypatch.delenv("MERIDIAN_GUARD", raising=False)
    monkeypatch.setenv("MERIDIAN_GUARD_DISABLE", "G11,g3")

    body = client.get("/hooks/diagnostics").json()
    guard = body["guard"]
    assert guard["registered"] is True and guard["complete"] is True
    assert guard["runtime_ok"] is True, guard
    assert guard["mode"] == "enforce"
    assert guard["disabled_rules"] == ["G3", "G11"]
    assert guard["status"] == "enforce"
    assert body["missing_required_count"] == 0

    monkeypatch.setenv("MERIDIAN_GUARD", "advisory")
    assert client.get("/hooks/diagnostics").json()["guard"]["status"] == "advisory"
    monkeypatch.setenv("MERIDIAN_GUARD", "off")
    assert client.get("/hooks/diagnostics").json()["guard"]["status"] == "off"
    monkeypatch.delenv("MERIDIAN_GUARD", raising=False)
    (hooks / "meridian_guard_post.ps1").unlink()
    guard = client.get("/hooks/diagnostics").json()["guard"]
    assert guard["status"] == "runtime_missing"
    assert guard["problems"] == ["PostToolUse: post missing script"]


def test_hooks_diagnostics_route_guard_not_installed(client, tmp_path, monkeypatch):
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setattr(hook_paths, "resolve_active_repo_root", lambda **kwargs: bare)
    guard = client.get("/hooks/diagnostics").json()["guard"]
    assert guard["registered"] is False
    assert guard["status"] == "not_installed"


# ---------------------------------------------------------------------------
# 4. companion guards act on the PowerShell tool (subprocess)
# ---------------------------------------------------------------------------

_WIN_CRASH_CODES = frozenset({
    0xC0000005, 0xC000007B, 0xC0000135, 0xC0000142, 0xC000013A, 3221225773,
})


def _powershell_exe() -> str | None:
    for exe in ("pwsh", "powershell"):
        found = shutil.which(exe)
        if found:
            return found
    return None


def _run_companion(kind: str, ext: str, tool: str, command: str) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_name": tool, "tool_input": {"command": command}}).encode("utf-8")
    if ext == "ps1":
        argv = [_powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(_HOOKS / f"{kind}.ps1")]
    else:
        argv = ["bash", f".claude/hooks/{kind}.sh"]
    # A closed port: pkg_install_guard's registry check fails fast and fails open.
    env = dict(os.environ, MERIDIAN_URL="http://127.0.0.1:9")
    last = None
    for _ in range(3):
        try:
            last = subprocess.run(argv, input=payload, cwd=str(_REPO), capture_output=True,
                                  timeout=60, env=env)
        except subprocess.TimeoutExpired:
            continue
        if (last.returncode & 0xFFFFFFFF) in _WIN_CRASH_CODES:
            continue
        return last
    assert last is not None, "the hook never produced a result"
    return last


_SHELLS = [
    pytest.param("ps1", marks=pytest.mark.skipif(_powershell_exe() is None, reason="no PowerShell")),
    pytest.param("sh", marks=pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")),
]

_SECRET_BLOCK = [
    "Get-Content .env",
    "gc C:\\repo\\.env | Select-Object -First 3",
    "$x = Get-Content ./secrets/.env.local",
    "Get-ChildItem env:",
    "Get-ChildItem Env:\\",
    "ls env: | Sort-Object Name",
    "[Environment]::GetEnvironmentVariables()",
    "type C:\\Users\\me\\.ssh\\id_rsa",
    "printenv",
]
_SECRET_ALLOW = [
    "Set-Location C:\\repo; git status",
    "Set-Content -Path out.txt -Value hi",
    "Write-Output $env:MERIDIAN_URL",
    "Get-ChildItem env:PATH",
    "Get-Content README.md",
    "Get-Content .envrc",
    "Get-Content meridian/secret_redaction.py",
]


@pytest.mark.subprocess_isolated
@pytest.mark.parametrize("ext", _SHELLS)
def test_secret_guard_blocks_powershell_credential_reads(ext):
    for command in _SECRET_BLOCK:
        r = _run_companion("secret_guard", ext, "PowerShell", command)
        assert r.returncode == 2, (ext, command, r.stderr)
        assert b"BLOCKED PowerShell command" in r.stderr, (ext, command)


@pytest.mark.subprocess_isolated
@pytest.mark.parametrize("ext", _SHELLS)
def test_secret_guard_allows_ordinary_powershell(ext):
    """Set-Location / Set-Content must not trip the bash-only 'set' pattern."""
    for command in _SECRET_ALLOW:
        r = _run_companion("secret_guard", ext, "PowerShell", command)
        assert r.returncode == 0, (ext, command, r.stderr)


@pytest.mark.subprocess_isolated
@pytest.mark.parametrize("ext", _SHELLS)
def test_secret_guard_ps1_and_sh_still_block_bash_env_dump(ext):
    r = _run_companion("secret_guard", ext, "Bash", "cat .env")
    assert r.returncode == 2, (ext, r.stderr)


@pytest.mark.subprocess_isolated
@pytest.mark.parametrize("ext", _SHELLS)
def test_dependency_install_guard_covers_powershell(ext):
    r = _run_companion("dependency_install_guard", ext, "PowerShell", "pip install totally-unknown-pkg-zzz")
    assert r.returncode == 2, (ext, r.stderr)
    assert b"totally-unknown-pkg-zzz" in r.stderr
    for command in ("pip install fastapi", "pixi run test"):
        r = _run_companion("dependency_install_guard", ext, "PowerShell", command)
        assert r.returncode == 0, (ext, command, r.stderr)
    r = _run_companion("dependency_install_guard", ext, "Write", "pip install totally-unknown-pkg-zzz")
    assert r.returncode == 0, (ext, r.stderr)


@pytest.mark.subprocess_isolated
@pytest.mark.parametrize("ext", _SHELLS)
def test_pkg_install_guard_covers_powershell(ext):
    """With the registry endpoint unreachable the guard fails open, but it
    must have got past its tool filter to try: the stderr note proves it."""
    r = _run_companion("pkg_install_guard", ext, "PowerShell", "pip install totally-unknown-pkg-zzz")
    assert r.returncode == 0, (ext, r.stderr)
    assert b"registry check unavailable" in r.stderr, (ext, r.stderr)
    r = _run_companion("pkg_install_guard", ext, "Write", "pip install totally-unknown-pkg-zzz")
    assert r.returncode == 0 and b"registry check" not in r.stderr, (ext, r.stderr)
