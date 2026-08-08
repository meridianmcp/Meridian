"""31a4a9c8 -- PreToolUse dependency/package install verification guard.

REFILED (original 23f21820 never shipped). Real threat class named by the May
2026 CISA/NSA/Five Eyes joint advisory: an autonomous coding agent installing
an unverified/typosquatted package via pip/npm/uvx, running arbitrary
setup/postinstall code before any human sees the package name.

This tests the ACTUAL hook BEHAVIOR (dependency_install_guard.sh via bash --
mirrors test_secret_redaction.py / test_worktree_guard.py):

1. Allows pip/npm installs of packages already declared in this repo's own
   manifests (pyproject.toml, package.json).
2. Blocks (exit 2) pip/npm/uvx installs naming a package not in those
   manifests and not in the .claude/hooks/verified_packages.txt allowlist.
3. Allows manifest-only installs (`pip install -r requirements.txt`, bare
   `npm install`, `npm ci`, local/editable installs) unconditionally.
4. Allows once a package is added to the durable verified_packages.txt allowlist.
5. Fails open on non-Bash tools, non-install commands, and garbage stdin.
6. settings.json actually registers the hook under PreToolUse with matcher
   "Bash" -- structural wiring, not just file presence.
7. dependency_install_guard.ps1 exists and is pure ASCII (Windows runtime).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HOOK_SH = _REPO / ".claude" / "hooks" / "dependency_install_guard.sh"
_HOOK_PS1 = _REPO / ".claude" / "hooks" / "dependency_install_guard.ps1"
_ALLOWLIST = _REPO / ".claude" / "hooks" / "verified_packages.txt"
_SETTINGS = _REPO / ".claude" / "settings.json"

_needs_bash = pytest.mark.skipif(
    not _HOOK_SH.exists() or shutil.which("bash") is None,
    reason="dependency_install_guard.sh or bash unavailable",
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


def _run_hook_once(payload: str) -> subprocess.CompletedProcess:
    """Run dependency_install_guard.sh from cwd=repo root."""
    r = subprocess.run(
        ["bash", ".claude/hooks/dependency_install_guard.sh"],
        input=payload.encode("utf-8"),
        cwd=str(_REPO),
        capture_output=True,
        timeout=30,
    )
    return subprocess.CompletedProcess(
        r.args,
        r.returncode,
        stdout=(r.stdout or b"").decode("utf-8", "replace"),
        stderr=(r.stderr or b"").decode("utf-8", "replace"),
    )


def _run_hook(payload: str) -> subprocess.CompletedProcess:
    """Retry on Windows subprocess-teardown crashes (harness artifact, not hook)."""
    last: subprocess.CompletedProcess | None = None
    for _ in range(3):
        try:
            last = _run_hook_once(payload)
        except subprocess.TimeoutExpired:
            continue
        if last.returncode in _WIN_CRASH_CODES:
            continue
        return last
    assert last is not None, "bash never produced a result (all attempts crashed)"
    return last


def _bash_payload(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


# ---------------------------------------------------------------------------
# Fail-open passthrough cases
# ---------------------------------------------------------------------------

@_needs_bash
def test_non_bash_tool_passes_through():
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "foo.py"}})
    r = _run_hook(payload)
    assert r.returncode == 0


@_needs_bash
def test_garbage_stdin_fails_open():
    r = _run_hook("not json at all {{{")
    assert r.returncode == 0


@_needs_bash
def test_empty_stdin_fails_open():
    r = _run_hook("")
    assert r.returncode == 0


@_needs_bash
def test_unrelated_bash_command_passes_through():
    r = _run_hook(_bash_payload("ls -la && git status"))
    assert r.returncode == 0


# ---------------------------------------------------------------------------
# Manifest-declared packages -- always allowed
# ---------------------------------------------------------------------------

@_needs_bash
def test_pip_install_known_pyproject_dependency_allowed():
    # 'fastapi' is a real dependency declared in pyproject.toml.
    r = _run_hook(_bash_payload("pip install fastapi"))
    assert r.returncode == 0


@_needs_bash
def test_pip_install_known_dependency_with_version_specifier_allowed():
    r = _run_hook(_bash_payload("pip install fastapi>=0.115"))
    assert r.returncode == 0


@_needs_bash
def test_npm_install_known_package_json_dependency_allowed():
    # 'cytoscape' is a real dependency declared in package.json.
    r = _run_hook(_bash_payload("npm install cytoscape"))
    assert r.returncode == 0


@_needs_bash
def test_npm_install_scoped_known_dependency_allowed():
    # '@testing-library/jest-dom' is a real devDependency in package.json.
    r = _run_hook(_bash_payload("npm install @testing-library/jest-dom@6.6.0"))
    assert r.returncode == 0


# ---------------------------------------------------------------------------
# Manifest-only installs -- always allowed regardless of package identity
# ---------------------------------------------------------------------------

@_needs_bash
def test_pip_install_requirements_file_allowed():
    r = _run_hook(_bash_payload("pip install -r requirements.txt"))
    assert r.returncode == 0


@_needs_bash
def test_pip_install_editable_local_allowed():
    r = _run_hook(_bash_payload("pip install -e ."))
    assert r.returncode == 0


@_needs_bash
def test_npm_install_bare_allowed():
    r = _run_hook(_bash_payload("npm install"))
    assert r.returncode == 0


@_needs_bash
def test_npm_ci_allowed():
    r = _run_hook(_bash_payload("npm ci"))
    assert r.returncode == 0


# ---------------------------------------------------------------------------
# Unknown packages -- blocked
# ---------------------------------------------------------------------------

@_needs_bash
def test_pip_install_unknown_package_blocked():
    r = _run_hook(_bash_payload("pip install totally-unheard-of-pkg-9f2b31a4"))
    assert r.returncode == 2
    assert "totally-unheard-of-pkg-9f2b31a4" in r.stderr
    assert "31a4a9c8" in r.stderr


@_needs_bash
def test_npm_install_unknown_package_blocked():
    r = _run_hook(_bash_payload("npm install totally-unheard-of-pkg-9f2b31a4"))
    assert r.returncode == 2
    assert "totally-unheard-of-pkg-9f2b31a4" in r.stderr


@_needs_bash
def test_uvx_unknown_package_blocked():
    r = _run_hook(_bash_payload("uvx totally-unheard-of-pkg-9f2b31a4"))
    assert r.returncode == 2
    assert "totally-unheard-of-pkg-9f2b31a4" in r.stderr


@_needs_bash
def test_pip_install_unknown_package_chained_with_other_commands_blocked():
    r = _run_hook(_bash_payload("cd repo && pip install totally-unheard-of-pkg-9f2b31a4 && pytest"))
    assert r.returncode == 2


@_needs_bash
def test_pip_install_direct_url_blocked():
    # Direct VCS/URL installs bypass the registry entirely -- must also verify.
    r = _run_hook(_bash_payload("pip install git+https://github.com/example/totally-unheard-of-pkg.git"))
    assert r.returncode == 2


@_needs_bash
def test_block_message_names_unblock_path():
    r = _run_hook(_bash_payload("pip install totally-unheard-of-pkg-9f2b31a4"))
    assert r.returncode == 2
    assert "verified_packages.txt" in r.stderr
    assert "request_hitl" in r.stderr


# ---------------------------------------------------------------------------
# Allowlist mechanism -- appending unblocks a previously-unknown package
# ---------------------------------------------------------------------------

@_needs_bash
def test_allowlisted_package_is_allowed():
    marker_pkg = "test-fixture-verified-pkg-31a4a9c8"
    original = _ALLOWLIST.read_text(encoding="utf-8") if _ALLOWLIST.exists() else ""
    try:
        # Confirm it's blocked BEFORE allowlisting.
        r_before = _run_hook(_bash_payload(f"pip install {marker_pkg}"))
        assert r_before.returncode == 2

        _ALLOWLIST.write_text(original + f"\n{marker_pkg}\n", encoding="utf-8")

        r_after = _run_hook(_bash_payload(f"pip install {marker_pkg}"))
        assert r_after.returncode == 0
    finally:
        _ALLOWLIST.write_text(original, encoding="utf-8")


# ---------------------------------------------------------------------------
# Structural wiring
# ---------------------------------------------------------------------------

def test_hook_files_exist():
    assert _HOOK_SH.exists(), "dependency_install_guard.sh must exist in .claude/hooks/"
    assert _HOOK_PS1.exists(), "dependency_install_guard.ps1 must exist in .claude/hooks/"
    assert _ALLOWLIST.exists(), "verified_packages.txt allowlist must exist in .claude/hooks/"


def test_dependency_install_guard_ps1_is_pure_ascii():
    """PS 5.1 reads BOM-less UTF-8 as cp1252; non-ASCII bytes corrupt the parser."""
    raw = _HOOK_PS1.read_bytes()
    non_ascii = [i for i, b in enumerate(raw) if b >= 128]
    assert not non_ascii, (
        f"dependency_install_guard.ps1 must be pure ASCII. "
        f"Non-ASCII bytes at offsets: {non_ascii[:5]}"
    )


def test_settings_json_registers_bash_hook():
    settings = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    pre_tool_use = settings["hooks"]["PreToolUse"]
    matches = [
        entry
        for entry in pre_tool_use
        if entry.get("matcher") == "Bash"
        for hook in entry.get("hooks", [])
        if "dependency_install_guard.ps1" in hook.get("command", "")
    ]
    assert matches, "settings.json must register dependency_install_guard.ps1 under PreToolUse matcher 'Bash'"


# ---------------------------------------------------------------------------
# Active-repository hook-path resolution (e5eec33b)
#
# Reproduced 2026-08-07: this exact hook's configured command
# ("$CLAUDE_PROJECT_DIR\.claude\hooks\dependency_install_guard.ps1")
# collapsed to a bare, unresolvable fragment when CLAUDE_PROJECT_DIR was
# empty in the launcher shell, even though dependency_install_guard.ps1
# genuinely exists on disk. See meridian.hook_paths for the resolver.
# ---------------------------------------------------------------------------

def test_dependency_install_guard_command_is_project_scoped_required_hook():
    from meridian import hook_paths

    settings = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    command = next(
        hook.get("command", "")
        for entry in settings["hooks"]["PreToolUse"]
        if entry.get("matcher") == "Bash"
        for hook in entry.get("hooks", [])
        if "dependency_install_guard.ps1" in hook.get("command", "")
    )
    assert hook_paths.is_project_relative_command(command) is True


def test_dependency_install_guard_command_resolves_ok_against_real_repo_root():
    """Generated <-> configured parity for this specific hook: the script
    _write_sprint_guard_hooks-adjacent tooling expects on disk is exactly
    what the configured settings.json command resolves to, against the
    real repo root -- never a bare root-relative path."""
    from meridian import hook_paths

    settings = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    command = next(
        hook.get("command", "")
        for entry in settings["hooks"]["PreToolUse"]
        if entry.get("matcher") == "Bash"
        for hook in entry.get("hooks", [])
        if "dependency_install_guard.ps1" in hook.get("command", "")
    )
    diag = hook_paths.resolve_configured_hook_command(command, _REPO)
    assert diag["status"] == hook_paths.STATUS_OK, diag
    assert diag["resolved_path"] == str(_HOOK_PS1)


def test_dependency_install_guard_resolves_via_cwd_when_claude_project_dir_empty():
    """The core regression, exercised end-to-end for this hook: empty
    CLAUDE_PROJECT_DIR must fall back to a real repo root derived from cwd
    (this test's own repo checkout), not collapse to "\\.claude\\hooks\\...".
    """
    from meridian import hook_paths

    repo_root = hook_paths.resolve_active_repo_root(claude_project_dir="", cwd=str(_REPO))
    assert repo_root is not None
    command = '& "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\dependency_install_guard.ps1"'
    diag = hook_paths.resolve_configured_hook_command(command, repo_root)
    assert diag["status"] == hook_paths.STATUS_OK, diag
