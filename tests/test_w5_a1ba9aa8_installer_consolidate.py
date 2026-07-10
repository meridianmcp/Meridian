"""Installer consolidation (a1ba9aa8).

install.ps1 is the SINGLE client-connector entry point. The hooks-install logic
lives inline in its -Component hooks path (previously fetched from a standalone
scripts/hooks_install.ps1), and scripts/hooks_install.ps1 is now a thin
backward-compat shim that fetches install.ps1 and runs it with -Component hooks
so the old `irm .../hooks_install.ps1 | iex` curl path keeps working.

These checks are behavior-preserving guards for the LIVE irm|iex install path:

  * both scripts are pure ASCII and parse clean (0 errors) -- validated via
    [System.Management.Automation.Language.Parser]::ParseFile when a PowerShell
    host is available, and unconditionally via a byte-level ASCII scan;
  * install.ps1 exposes the unified -Component switch INCLUDING the hooks path,
    which runs the RFC 8628 device flow inline (no fetch of hooks_install.ps1);
  * the default -Component 'both' behavior is preserved (binary + hooks);
  * the old hooks_install.ps1 path still resolves to the hooks install -- now as
    a shim that delegates to install.ps1 -Component hooks, with NO fetch loop.

Unit-level only: static source inspection + one in-process TestClient route
check. No real servers/ports/network/sleeps.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INSTALL_PS1 = _REPO_ROOT / "install.ps1"
_HOOKS_INSTALL_PS1 = _REPO_ROOT / "scripts" / "hooks_install.ps1"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# ASCII + clean-parse: both scripts. .ps1 files MUST stay pure ASCII (PS 5.1
# reads BOM-less files as cp1252, so smart quotes / em-dashes corrupt the parse).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [_INSTALL_PS1, _HOOKS_INSTALL_PS1], ids=lambda p: p.name)
def test_installer_script_is_pure_ascii(path: Path):
    raw = path.read_bytes()
    non_ascii = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    assert not non_ascii, (
        f"{path.name} contains non-ASCII byte(s) at offsets "
        f"{[i for i, _ in non_ascii[:10]]}; keep .ps1 pure ASCII (use -- and "
        f"straight quotes)"
    )
    # No BOM either (the Edit tool writes BOM-less UTF-8; assert we kept it so).
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{path.name} must not start with a UTF-8 BOM"


def _powershell_exe() -> str | None:
    for exe in ("pwsh", "powershell"):
        found = shutil.which(exe)
        if found:
            return found
    return None


@pytest.mark.parametrize("path", [_INSTALL_PS1, _HOOKS_INSTALL_PS1], ids=lambda p: p.name)
def test_installer_script_parses_with_zero_errors(path: Path):
    """Validate via [Parser]::ParseFile -> 0 errors, when a PS host is present.

    Skipped (not failed) on hosts without PowerShell (e.g. Linux CI); the
    ASCII scan above still runs there, and the PS parse runs on Windows.
    """
    ps = _powershell_exe()
    if ps is None:
        pytest.skip("no PowerShell host available to run Parser::ParseFile")

    script = (
        "$ErrorActionPreference='Stop';"
        "$t=$null;$e=$null;"
        f"[System.Management.Automation.Language.Parser]::ParseFile('{path}',"
        "[ref]$t,[ref]$e) | Out-Null;"
        "if ($e.Count -gt 0) { $e | ForEach-Object { [Console]::Error.WriteLine($_.Message) }; exit $e.Count } "
        "else { exit 0 }"
    )
    proc = subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert proc.returncode == 0, (
        f"{path.name} failed to parse cleanly ({proc.returncode} error(s)):\n{proc.stderr}"
    )


# ---------------------------------------------------------------------------
# install.ps1 -- the single entry point exposing the unified -Component switch.
# ---------------------------------------------------------------------------

def test_install_ps1_exposes_component_switch_with_hooks():
    src = _read(_INSTALL_PS1)
    # A validated -Component parameter covering all four modes incl. hooks.
    assert "[ValidateSet('binary', 'hooks', 'both', 'custom')]" in src
    assert "[string]$Component = 'both'" in src, "default must remain 'both' (behavior-preserving)"
    # The switch resolves each mode; hooks flips $installHooks.
    assert "switch ($Component)" in src
    assert "'hooks'  { $installHooks  = $true }" in src
    assert "'both'   { $installBinary = $true; $installHooks = $true }" in src


def test_install_ps1_default_both_preserves_binary_and_hooks():
    """The default (no -Component / 'both') must still install BOTH components."""
    src = _read(_INSTALL_PS1)
    # Both component guards exist and are driven by the resolved flags.
    assert "if ($installBinary) {" in src
    assert "if ($installHooks) {" in src
    # The binary component still downloads + runs meridian-connect (unchanged path).
    assert "& $dest @binaryArgs" in src


def test_install_ps1_hooks_component_runs_device_flow_inline():
    """The -Component hooks path runs the RFC 8628 device flow INLINE.

    Post-consolidation the hooks auth lives in install.ps1, reusing the same
    device-flow + cached-token helpers as the binary component -- it does NOT
    shell out to hooks_install.ps1.
    """
    src = _read(_INSTALL_PS1)
    hooks_idx = src.index("if ($installHooks) {")
    hooks_block = src[hooks_idx:]
    # Inline keyless auth: reuses the shared helpers, honours env + cached token.
    assert "Get-MeridianDeviceToken -MeridianUrl $targetUrl" in hooks_block
    assert "Get-MeridianCachedToken -MeridianUrl $targetUrl" in hooks_block
    assert "MERIDIAN_TOKEN" in hooks_block
    assert "sk_meridian_" in hooks_block
    # The device-flow endpoints + helper are defined in install.ps1 as a whole.
    assert "/oauth/device" in src
    assert "/oauth/token" in src
    assert "function Get-MeridianDeviceToken" in src


def test_install_ps1_hooks_component_does_not_fetch_hooks_install():
    """LOOP GUARD: install.ps1's hooks path must not fetch hooks_install.ps1.

    hooks_install.ps1 now fetches install.ps1; if install.ps1 fetched
    hooks_install.ps1 back, the two would fetch each other forever. Inlining the
    hooks logic is what prevents that -- assert no such fetch remains in the
    executable code (comments may still mention the file by name).
    """
    src = _read(_INSTALL_PS1)
    code_lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    code = "\n".join(code_lines)
    assert "hooks_install.ps1" not in code, (
        "install.ps1 executable code must not reference hooks_install.ps1 "
        "(would create a fetch loop with the shim)"
    )
    # And specifically no Invoke-RestMethod/Invoke-Expression fetch of it.
    assert "/hooks_install.ps1" not in code


# ---------------------------------------------------------------------------
# scripts/hooks_install.ps1 -- thin backward-compat shim.
# ---------------------------------------------------------------------------

def test_hooks_install_ps1_is_thin_shim_to_install_component_hooks():
    src = _read(_HOOKS_INSTALL_PS1)
    # Fetches install.ps1 from the resolved server and runs it with -Component hooks.
    assert "/install.ps1" in src
    assert "-Component hooks" in src
    assert "Invoke-RestMethod" in src
    # Runs the fetched script as a scriptblock so the named -Component param binds
    # (bare `iex $script` cannot accept parameters).
    assert "[scriptblock]::Create(" in src
    # It must NOT re-implement the device flow -- that now lives in install.ps1.
    assert "/oauth/device" not in src
    assert "/oauth/token" not in src
    assert "urn:ietf:params:oauth:grant-type:device_code" not in src
    # Still resolves a server URL (preserves the old $MeridianUrl contract).
    assert "MeridianUrl" in src
    assert "usemeridian.us" in src


def test_hooks_install_ps1_shim_is_short():
    """A shim, not a second full installer -- keep it small so logic can't drift
    back into it (the real logic must live only in install.ps1)."""
    src = _read(_HOOKS_INSTALL_PS1)
    non_comment = [
        ln for ln in src.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    assert len(non_comment) < 25, (
        f"hooks_install.ps1 shim has {len(non_comment)} code lines; it should be "
        f"a thin delegation to install.ps1 -Component hooks"
    )


# ---------------------------------------------------------------------------
# Route serving -- the LIVE irm|iex paths for both scripts still resolve.
# In-process TestClient (no real port/network).
# ---------------------------------------------------------------------------

def test_install_ps1_route_serves_consolidated_installer(client):
    r = client.get("/install.ps1")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    # The served script is the consolidated entry point with the -Component switch.
    assert "meridian-connect" in r.text
    assert "-Component" in r.text or "$Component" in r.text
    assert "/oauth/device" in r.text


def test_hooks_install_ps1_route_still_resolves_to_hooks_install(client):
    r = client.get("/hooks_install.ps1")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    # Old path still resolves -- now as a shim delegating to install.ps1 hooks.
    assert "install.ps1" in r.text
    assert "-Component hooks" in r.text
