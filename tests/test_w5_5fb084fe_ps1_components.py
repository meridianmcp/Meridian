"""Static + parse checks for install.ps1 component selection (sprint 5fb084fe).

install.ps1 must support a `-Component` parameter accepting
binary | hooks | both | custom (default: both), with real per-component
behavior and a confirmation/summary line stating what will be installed.

These checks are platform-independent source assertions plus, when a PowerShell
interpreter is available, a real `[Parser]::ParseFile` parse (0 errors). The
parse step is skipped cleanly on hosts without powershell/pwsh so CI stays green
on Linux. The file must also remain pure ASCII (PS 5.1 reads BOM-less files as
cp1252, so any non-ASCII byte corrupts the parser).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_INSTALL_PS1 = Path(__file__).resolve().parent.parent / "install.ps1"


def _src() -> str:
    return _INSTALL_PS1.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# ASCII / encoding hygiene
# ---------------------------------------------------------------------------

def test_install_ps1_is_pure_ascii_no_bom():
    raw = _INSTALL_PS1.read_bytes()
    # No UTF-8/UTF-16 BOM (PS 5.1 tolerates BOM-less ASCII; a BOM or non-ASCII
    # bytes corrupt under the cp1252 fallback).
    assert not raw.startswith(b"\xef\xbb\xbf"), "install.ps1 must not have a UTF-8 BOM"
    assert not raw.startswith(b"\xff\xfe") and not raw.startswith(b"\xfe\xff"), (
        "install.ps1 must not be UTF-16"
    )
    non_ascii = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    assert not non_ascii, f"install.ps1 has non-ASCII bytes at {non_ascii[:10]}"


# ---------------------------------------------------------------------------
# -Component parameter + per-component branches exist
# ---------------------------------------------------------------------------

def test_install_ps1_declares_component_param_with_validate_set():
    src = _src()
    # A top-level param() block declaring -Component with the exact ValidateSet.
    assert "param(" in src
    assert "[string]$Component" in src
    assert "ValidateSet('binary', 'hooks', 'both', 'custom')" in src
    # Default is 'both' (the old install-everything behavior).
    assert "$Component = 'both'" in src


def test_install_ps1_has_real_per_component_branches():
    src = _src()
    # A switch over $Component that flips distinct per-component install flags.
    assert "switch ($Component)" in src
    for label in ("'binary'", "'hooks'", "'both'", "'custom'"):
        assert label in src, f"missing {label} branch in the component switch"
    # Real behavior: the two components are gated behind their own booleans.
    assert "$installBinary" in src
    assert "$installHooks" in src
    assert "if ($installBinary)" in src
    assert "if ($installHooks)" in src


def test_install_ps1_custom_is_interactive_with_safe_fallback():
    src = _src()
    # 'custom' prompts per component...
    assert "Read-Host" in src
    # ...and falls back to a non-interactive default rather than dead-ending
    # when there is no console (piped `iex`).
    assert "IsInputRedirected" in src


def test_install_ps1_prints_install_summary_line():
    src = _src()
    # A confirmation/summary line naming what will be installed.
    assert "install plan" in src.lower()
    assert "$componentList" in src
    # It reflects the actual components in human-readable form.
    assert "meridian-connect tunnel binary" in src
    assert "Meridian session hooks" in src


def test_install_ps1_hooks_component_delegates_to_hooks_installer():
    src = _src()
    # The hooks component installs the real Meridian session hooks by fetching
    # hooks_install.ps1 from the resolved target server (not a stub).
    assert "hooks_install.ps1" in src
    assert "$installHooks" in src
    # It uses the resolved target URL, so self-hosted servers work too.
    assert "$targetUrl" in src


def test_install_ps1_binary_phase_is_guarded_not_always_run():
    src = _src()
    # The download/PATH/run of meridian-connect must be conditional on the binary
    # component being selected -- the whole point of the item (no longer forced).
    guard_idx = src.index("if ($installBinary)")
    run_idx = src.index("& $dest @binaryArgs")
    assert guard_idx < run_idx, (
        "the binary download/run must sit inside the $installBinary guard"
    )


def test_install_ps1_nothing_selected_exits_cleanly():
    src = _src()
    # custom with everything declined -> no-op, clean exit (not a crash / not a
    # silent full install).
    assert "Nothing selected to install" in src


# ---------------------------------------------------------------------------
# Real PowerShell parse (skipped when no interpreter is present)
# ---------------------------------------------------------------------------

def _powershell_exe() -> str | None:
    for exe in ("pwsh", "powershell"):
        found = shutil.which(exe)
        if found:
            return found
    return None


def test_install_ps1_parses_with_zero_errors():
    ps = _powershell_exe()
    if ps is None:
        pytest.skip("no PowerShell interpreter available on this host")
    # Parse the file via the PowerShell AST parser and assert 0 syntax errors.
    ps_script = (
        "$tokens=$null;$errors=$null;"
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{_INSTALL_PS1.as_posix()}',[ref]$tokens,[ref]$errors)|Out-Null;"
        "if($errors){$errors|ForEach-Object{Write-Output $_.Message};exit 1}"
        "else{Write-Output 'PARSE_OK';exit 0}"
    )
    proc = subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-Command", ps_script],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert proc.returncode == 0, f"install.ps1 failed to parse:\n{proc.stdout}\n{proc.stderr}"
    assert "PARSE_OK" in proc.stdout
