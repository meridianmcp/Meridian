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
