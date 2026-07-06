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
    # never reaches the interactive prompts. (The invocation now forwards a copied
    # arg array, @binaryArgs, so the device-flow token can be appended — 73b65117.)
    assert "& $dest @binaryArgs" in src
    guard_idx = src.index("if (-not $downloaded)")
    run_idx = src.index("& $dest @binaryArgs")
    assert guard_idx < run_idx, "download-failure guard must precede running the binary"


# ---------------------------------------------------------------------------
# 50d2664d — installers print the exact release version being installed
# ---------------------------------------------------------------------------

def test_install_ps1_prints_release_version():
    src = _src()
    # Resolves the "latest" tag via the releases API (same tag as
    # releases/latest/download) and prints it before/after the download.
    assert "api.github.com/repos/$repo/releases/latest" in src
    assert "tag_name" in src
    assert "$releaseTag" in src
    assert "Installing Meridian Connect" in src


def test_install_windows_ps1_prints_release_version():
    src = _INSTALL_WINDOWS_PS1.read_text(encoding="utf-8")
    assert "api.github.com/repos/meridianmcp/Meridian/releases/latest" in src
    assert "tag_name" in src
    assert "$releaseTag" in src


def test_install_sh_prints_release_version():
    src = (Path(__file__).resolve().parent.parent / "install.sh").read_text(encoding="utf-8")
    assert "api.github.com/repos/meridianmcp/Meridian/releases/latest" in src
    assert "tag_name" in src
    # No jq dependency — parses the tag with sed (check for an actual jq pipe, not
    # the word "jq" which appears in the explanatory comment).
    assert "| jq" not in src
    assert "sed -n" in src


# ---------------------------------------------------------------------------
# 73b65117 — install.ps1 acquires a token via the RFC 8628 device flow (reusing
# the same /oauth/device + /oauth/token infra as hooks_install.ps1), so
# `irm ... | iex` completes without a TTY paste.
# ---------------------------------------------------------------------------

def test_install_ps1_uses_device_flow_for_keyless_auth():
    src = _src()
    assert "/oauth/device" in src
    assert "/oauth/token" in src
    assert "urn:ietf:params:oauth:grant-type:device_code" in src
    assert "Get-MeridianDeviceToken" in src
    # Honors the RFC 8628 poll-control signals.
    assert "slow_down" in src
    assert "access_denied" in src
    assert "expired_token" in src


def test_install_ps1_injects_device_token_and_skips_when_supplied():
    src = _src()
    # The minted token is forwarded to the binary as --token.
    assert "$binaryArgs += @('--token'" in src
    # Skips the device flow when a token/env is already present or target is local.
    assert "MERIDIAN_TOKEN" in src
    assert "$hasToken" in src
    assert "$isLocal" in src


def test_install_ps1_route_still_serves_script(client):
    r = client.get("/install.ps1")
    assert r.status_code == 200
    assert "meridian-connect" in r.text
    assert "/oauth/device" in r.text
    assert r.headers["content-type"].startswith("text/plain")


# ---------------------------------------------------------------------------
# f73810d5 / 3ac13517 — the compiled tunnel binaries MUST use the Windows
# SelectorEventLoop *policy*, not DefaultEventLoopPolicy() (= ProactorEventLoop on
# Windows). Setting the policy wrong shipped a live psycopg_pool.PoolTimeout: the
# fix lived only in __main__.py and was never mirrored into the two binary entry
# points. These source-inspection checks are platform-independent (they never
# touch the Windows-only stdlib symbol) so they run green on Linux CI too.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent


# Match the actual policy CALL, not comment text (the comments explain why
# DefaultEventLoopPolicy is wrong, so a bare substring check would false-positive).
_GOOD_POLICY = "set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())"
_BAD_POLICY = "set_event_loop_policy(asyncio.DefaultEventLoopPolicy())"


def test_tunnel_main_uses_selector_event_loop_policy():
    src = (_REPO_ROOT / "meridian" / "tunnel_main.py").read_text(encoding="utf-8")
    assert _GOOD_POLICY in src
    assert _BAD_POLICY not in src


def test_meridian_connect_uses_selector_event_loop_policy():
    src = (_REPO_ROOT / "scripts" / "meridian_connect.py").read_text(encoding="utf-8")
    assert _GOOD_POLICY in src
    assert _BAD_POLICY not in src


def test_main_entry_uses_selector_event_loop_policy():
    # The already-correct reference implementation — pin it so a future edit can't
    # regress the one entry point that was right all along.
    src = (_REPO_ROOT / "meridian" / "__main__.py").read_text(encoding="utf-8")
    assert _GOOD_POLICY in src
    assert _BAD_POLICY not in src


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
