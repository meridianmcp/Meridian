"""Static checks for install.ps1 download hardening (sprint 738f7cf7).

install.ps1 must hard-fail (exit 1) when the meridian-connect download fails,
rather than silently continuing to run a missing/empty binary.
"""
from __future__ import annotations

from pathlib import Path

_INSTALL_PS1 = Path(__file__).resolve().parent.parent / "install.ps1"


def _src() -> str:
    return _INSTALL_PS1.read_text(encoding="utf-8")


def test_install_ps1_retries_then_verifies_download():
    src = _src()
    # Retries before giving up.
    assert "maxAttempts" in src and "for ($attempt" in src
    # Verifies the downloaded file actually exists AND is non-zero in size.
    assert "Test-Path $dest" in src
    assert ".Length -gt 0" in src


def test_install_ps1_hard_errors_and_aborts_before_running_binary():
    src = _src()
    # Hard-fail: a clear message + non-zero exit.
    assert "Write-Error" in src
    assert "exit 1" in src
    # The binary only runs after a verified download — the failure guard and the
    # exit must precede the line that executes the installer, so a missing binary
    # never reaches the interactive prompts.
    assert "& $dest @args" in src
    guard_idx = src.index("if (-not $downloaded)")
    run_idx = src.index("& $dest @args")
    assert guard_idx < run_idx, "download-failure guard must precede running the binary"


# ---------------------------------------------------------------------------
# install-windows.ps1 — standalone meridian.exe installer (fe41fba7)
# ---------------------------------------------------------------------------

_INSTALL_WINDOWS_PS1 = (
    Path(__file__).resolve().parent.parent / "scripts" / "install-windows.ps1"
)


def test_install_windows_ps1_installs_meridian_exe_to_local_bin():
    src = _INSTALL_WINDOWS_PS1.read_text(encoding="utf-8")
    # Downloads the flat meridian.exe release asset into ~/.local/bin.
    assert "releases/latest/download/meridian.exe" in src
    assert ".local\\bin" in src
    # Same download hardening as install.ps1: retries + non-empty verification.
    assert "maxAttempts" in src and "for ($attempt" in src
    assert ".Length -gt 0" in src
    assert "Write-Error" in src and "exit 1" in src


def test_install_windows_ps1_adds_path_without_setx():
    src = _INSTALL_WINDOWS_PS1.read_text(encoding="utf-8")
    # Persistent user PATH via SetEnvironmentVariable — NOT setx, which truncates
    # PATH at 1024 chars and can corrupt it.
    assert "SetEnvironmentVariable" in src
    # No actual setx invocation (ignore comment lines that explain why we avoid it).
    code_lines = [ln.strip() for ln in src.splitlines() if not ln.strip().startswith("#")]
    assert not any("setx" in ln.lower() for ln in code_lines)


def test_install_windows_ps1_route_serves_script(client):
    r = client.get("/install-windows.ps1")
    assert r.status_code == 200
    assert "meridian.exe" in r.text
    assert "SetEnvironmentVariable" in r.text
    assert r.headers["content-type"].startswith("text/plain")


# ---------------------------------------------------------------------------
# hooks_install.ps1 — RFC 8628 device-flow auth for the hooks installer (e9f18530)
# ---------------------------------------------------------------------------

_HOOKS_INSTALL_PS1 = (
    Path(__file__).resolve().parent.parent / "scripts" / "hooks_install.ps1"
)


def test_hooks_install_ps1_uses_device_grant_not_static_key():
    src = _HOOKS_INSTALL_PS1.read_text(encoding="utf-8")
    # Hits the RFC 8628 device + token endpoints...
    assert "/oauth/device" in src
    assert "/oauth/token" in src
    assert "urn:ietf:params:oauth:grant-type:device_code" in src
    # ...prints the user_code + verification URL and polls for the token.
    assert "user_code" in src
    assert "verification_uri" in src
    assert "access_token" in src
    # Honors the RFC 8628 poll-control signals.
    assert "slow_down" in src
    assert "access_denied" in src
    assert "expired_token" in src
    # It must NOT prompt the user to paste a static API key.
    assert "Paste" not in src


def test_hooks_install_ps1_has_existing_token_fallback():
    src = _HOOKS_INSTALL_PS1.read_text(encoding="utf-8")
    # Fallback/comment path when a token is already present.
    assert "MERIDIAN_TOKEN" in src


def test_hooks_install_ps1_route_serves_script(client):
    r = client.get("/hooks_install.ps1")
    assert r.status_code == 200
    assert "/oauth/device" in r.text
    assert "device_code" in r.text
    assert r.headers["content-type"].startswith("text/plain")
