"""Static checks for install.ps1 download hardening (sprint 738f7cf7).

install.ps1 must hard-fail (exit 1) when the meridian-connect download fails,
rather than silently continuing to run a missing/empty binary.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

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
    # 9784f8ef: the minted token reaches the binary through the MERIDIAN_TOKEN env
    # var of the child process -- NOT as a --token argument, which is visible in
    # process listings (see tests/test_9784f8ef_install_token_exposure.py).
    assert "$binaryArgs += @('--token'" not in src
    assert "$env:MERIDIAN_TOKEN = $childToken" in src
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


def test_tunnel_main_does_not_force_selector_event_loop_policy():
    """7b457c55 — REVERSED from this test's own prior assertion (was: MUST
    force WindowsSelectorEventLoopPolicy). tunnel_main.py is the slim,
    tunnel-ONLY PyInstaller entry point (no --mcp/--server dispatch exists in
    it at all); forcing SelectorEventLoopPolicy here — originally needed for
    a psycopg_pool issue that predates this module's later split into a
    pure, psycopg-free tunnel-only entry point — broke every
    run_cmd/run_verification call (asyncio.create_subprocess_exec/_shell
    raises a bare NotImplementedError on Windows' SelectorEventLoop; see
    docs/meridian-local-runner-tunnel-investigation-2026-08-31.md). This
    entry point now matches meridian/__main__.py's own --tunnel carve-out
    (see test_main_entry_uses_selector_event_loop_policy's docstring below —
    that assertion is unchanged: __main__.py's non-tunnel/--mcp dispatch
    still needs SelectorEventLoop for psycopg3, and its --tunnel branch
    already skipped forcing it, which is exactly the behavior mirrored here).
    """
    src = (_REPO_ROOT / "meridian" / "tunnel_main.py").read_text(encoding="utf-8")
    assert _GOOD_POLICY not in src
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


def test_install_windows_tray_offers_local_zotero_setup_without_cli_credentials():
    src = _INSTALL_WINDOWS_PS1.read_text(encoding="utf-8")
    assert "[switch]$ConfigureZotero" in src
    assert "Configure your local Zotero connection now? [y/N]" in src
    assert "& $dest --configure-zotero" in src
    assert "configure it later from the Meridian tray menu" in src
    assert "ZOTERO_API_KEY" not in src


def test_install_windows_ps1_route_serves_script(client):
    r = client.get("/install-windows.ps1")
    assert r.status_code == 200
    assert "meridian.exe" in r.text
    assert "SetEnvironmentVariable" in r.text
    assert r.headers["content-type"].startswith("text/plain")


# ---------------------------------------------------------------------------
# cf8a90ec -- install-windows.ps1 -Tray installs meridian-tray.exe, the built
# and tested Windows tray/GUI binary that was never reachable through any
# documented install path before this.
# ---------------------------------------------------------------------------

def test_install_windows_ps1_exposes_tray_switch():
    src = _INSTALL_WINDOWS_PS1.read_text(encoding="utf-8")
    assert "[switch]$Tray" in src
    assert "if ($Tray) {" in src


def test_install_windows_ps1_autostart_is_explicit_and_tray_only():
    src = _install_windows_ps1_src()
    assert "[switch]$Autostart" in src
    assert "if ($Autostart -and -not $Tray)" in src
    assert '"HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run"' in src
    assert '$command = \'"{0}"\' -f $TargetPath' in src

    tray_start = src.index("if ($Tray) {")
    tray_block = src[tray_start:]
    assert "if ($Autostart) {" in tray_block
    assert "Enable-MeridianAutostart -TargetPath $dest" in tray_block


def test_install_windows_ps1_uninstall_removes_optional_autostart_entry():
    src = _install_windows_ps1_src()
    uninstall_idx = src.index("if ($Uninstall) {")
    exit_idx = src.index("exit 0", uninstall_idx)
    uninstall_block = src[uninstall_idx:exit_idx]
    assert "Remove-MeridianAutostart" in uninstall_block


def test_install_windows_ps1_tray_downloads_meridian_tray_exe():
    src = _INSTALL_WINDOWS_PS1.read_text(encoding="utf-8")
    tray_idx = src.index("if ($Tray) {")
    # The tray branch must return (exit 0) before falling through to the
    # meridian.exe / uv path below -- otherwise -Tray would install both.
    exit_idx = src.index("exit 0", tray_idx)
    tray_block = src[tray_idx:exit_idx]
    assert "releases/latest/download/meridian-tray.exe" in tray_block
    assert 'Join-Path $env:USERPROFILE ".local\\bin"' in tray_block
    assert 'Join-Path $binDir "meridian-tray.exe"' in tray_block
    # Same download hardening as every other installer path in this repo.
    assert "maxAttempts" in tray_block and "for ($attempt" in tray_block
    assert ".Length -gt 0" in tray_block
    assert "Write-Error" in tray_block and "exit 1" in tray_block


def test_install_windows_ps1_tray_branch_precedes_uv_path():
    """The -Tray branch must exit before the uv/meridian.exe logic below it --
    confirms -Tray and the default path are mutually exclusive, not both-run."""
    src = _INSTALL_WINDOWS_PS1.read_text(encoding="utf-8")
    tray_idx = src.index("if ($Tray) {")
    # The real invocation (not just a comment mentioning the same phrase --
    # the -Tray branch's own preamble comment explains it does NOT use uv,
    # which contains this same substring earlier in the file).
    uv_idx = src.index("& uv tool install meridian-server")
    assert tray_idx < uv_idx
    exit_idx = src.index("exit 0", tray_idx)
    assert exit_idx < uv_idx, "-Tray branch must exit before reaching the uv/meridian.exe path"


def test_install_windows_ps1_default_behavior_unchanged_without_tray():
    """Behavior-preserving: everything below the -Tray branch (the pre-existing
    uv-then-binary-fallback flow for meridian.exe) is untouched."""
    src = _INSTALL_WINDOWS_PS1.read_text(encoding="utf-8")
    assert "uv tool install meridian-server" in src
    assert "releases/latest/download/meridian.exe" in src
    assert 'Join-Path $binDir "meridian.exe"' in src


def test_install_windows_ps1_route_serves_tray_switch(client):
    r = client.get("/install-windows.ps1")
    assert r.status_code == 200
    assert "[switch]$Tray" in r.text
    assert "meridian-tray.exe" in r.text


def test_install_ps1_points_to_tray_option():
    """Discoverability only (no functional change to install.ps1 itself): a
    hosted-tunnel-client user who actually wants the self-hosted tray/GUI app
    is pointed at the right script rather than left to find it on their own."""
    src = _src()
    assert "install-windows.ps1 -Tray" in src


# ---------------------------------------------------------------------------
# hooks_install.ps1 — thin backward-compat shim to install.ps1 -Component hooks
# (a1ba9aa8). The RFC 8628 device-flow auth that used to live standalone here
# (e9f18530) now lives inline in install.ps1's -Component hooks path; this file
# is kept only so the old `irm .../hooks_install.ps1 | iex` curl path resolves.
# The device-flow assertions moved to test_w5_a1ba9aa8_installer_consolidate.py.
# ---------------------------------------------------------------------------

_HOOKS_INSTALL_PS1 = (
    Path(__file__).resolve().parent.parent / "scripts" / "hooks_install.ps1"
)


def test_hooks_install_ps1_is_shim_to_install_component_hooks():
    src = _HOOKS_INSTALL_PS1.read_text(encoding="utf-8")
    # Post-consolidation: fetches install.ps1 and runs it with -Component hooks
    # rather than re-implementing the device flow.
    assert "/install.ps1" in src
    assert "-Component hooks" in src
    # It must NOT re-run the device flow itself (that lives in install.ps1 now).
    assert "/oauth/device" not in src
    # It must NOT prompt the user to paste a static API key.
    assert "Paste" not in src


def test_hooks_install_ps1_route_serves_shim(client):
    r = client.get("/hooks_install.ps1")
    assert r.status_code == 200
    # The served shim points back at the consolidated installer.
    assert "install.ps1" in r.text
    assert "-Component hooks" in r.text
    assert r.headers["content-type"].startswith("text/plain")


# ---------------------------------------------------------------------------
# cee295bd — reuse an existing valid local token before any auth flow
# ---------------------------------------------------------------------------

def test_install_ps1_reuses_cached_token_before_device_flow():
    src = _src()
    # A dedicated cached-token reader exists and mirrors the client cache shape
    # (~/.meridian/config.json -> tunnel_token, base_url match + expiry + prefix).
    assert "function Get-MeridianCachedToken" in src
    assert "config.json" in src and "tunnel_token" in src
    assert "expires_at" in src
    assert "sk_meridian_" in src
    # The cached-token check must run BEFORE the browser device flow, and skip it
    # when a valid token is found.
    cache_call = src.index("Get-MeridianCachedToken -MeridianUrl $targetUrl")
    device_call = src.index("Get-MeridianDeviceToken -MeridianUrl $targetUrl")
    assert cache_call < device_call, "cached-token check must precede the device flow"
    # Using a cached token sets $hasToken so the device-flow block is skipped.
    assert "$hasToken = $true" in src[cache_call:device_call]


# ---------------------------------------------------------------------------
# ba31dedf — meridian_connect.py credential-leak fixes:
#   1. the SessionStart/Stop hook commands must never carry the raw token as a
#      literal substring (settings.json is a file people paste into bug
#      reports; a `curl -H 'Authorization: Bearer <token>'` argv also
#      re-exposes the token to `ps`/Task Manager on EVERY hook firing).
#   2. the local self-hosted health-check fallback must never default to
#      $HOME -- it must be explicitly configured via MERIDIAN_LOCAL_REPO, and
#      must refuse a bare home directory even when one is configured.
# ---------------------------------------------------------------------------
import importlib.util as _importlib_util


def _load_meridian_connect():
    path = Path(__file__).resolve().parent.parent / "scripts" / "meridian_connect.py"
    spec = _importlib_util.spec_from_file_location("meridian_connect", path)
    mod = _importlib_util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestWriteCurlHeaderConfig:
    def test_empty_token_returns_empty_string(self):
        mod = _load_meridian_connect()
        assert mod._write_curl_header_config("") == ""

    def test_real_token_never_appears_in_hook_command_construction(self, tmp_path, monkeypatch):
        """The exact bug class this fixes: build the hook command the way
        main() does and assert the token is nowhere in the resulting string --
        only a reference to a local config file."""
        mod = _load_meridian_connect()
        monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: tmp_path))
        fake_token = "sk_meridian_faketoken_not_real_1234567890"  # noqa: S105 -- fixture value, never a live secret
        cfg_path = mod._write_curl_header_config(fake_token)
        assert cfg_path, "a real token must produce a config file path"
        auth_flag = f' -K "{cfg_path}"'
        start_cmd = f"curl -s -X POST{auth_flag} -H 'Content-Type: application/json' '.../hooks/session-start'"
        assert fake_token not in start_cmd, "the raw token must never be a literal substring of the hook command"
        # The token DOES live in the local config file curl reads -- that's the point
        # (equivalent to ~/.netrc), just never in argv/settings.json.
        written = Path(cfg_path).read_text(encoding="utf-8")
        assert fake_token in written
        assert written.startswith('header = "Authorization: Bearer ')

    def test_config_file_written_under_dot_meridian_home_dir(self, tmp_path, monkeypatch):
        mod = _load_meridian_connect()
        monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: tmp_path))
        cfg_path = mod._write_curl_header_config("sk_meridian_faketoken_not_real")  # noqa: S105
        assert Path(cfg_path).parent == tmp_path / ".meridian"


class TestLocalRepoHint:
    def test_unset_env_var_returns_empty(self, monkeypatch):
        mod = _load_meridian_connect()
        monkeypatch.delenv("MERIDIAN_LOCAL_REPO", raising=False)
        assert mod._local_repo_hint() == ""

    def test_explicit_repo_path_is_used(self, tmp_path, monkeypatch):
        mod = _load_meridian_connect()
        monkeypatch.setenv("MERIDIAN_LOCAL_REPO", str(tmp_path))
        assert mod._local_repo_hint() == str(tmp_path.resolve())

    def test_bare_home_directory_is_refused_even_if_configured(self, tmp_path, monkeypatch):
        """The exact bug this closes: meridian_connect.py used to unconditionally
        `cd "$HOME"` -- even an EXPLICIT MERIDIAN_LOCAL_REPO=$HOME must still be
        refused, never silently accepted as a project scope."""
        mod = _load_meridian_connect()
        monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("MERIDIAN_LOCAL_REPO", str(tmp_path))
        assert mod._local_repo_hint() == ""

    def test_start_cmd_skips_fallback_entirely_when_hint_unset(self, monkeypatch):
        """Never guess $HOME: with no configured hint, the local-fallback branch
        in main()'s command-building logic must be skippable (no local repo
        candidate at all), not silently substitute $HOME."""
        mod = _load_meridian_connect()
        monkeypatch.delenv("MERIDIAN_LOCAL_REPO", raising=False)
        assert mod._local_repo_hint() == ""
        # The historical bug: the literal string '$HOME' baked into a fallback
        # shell command. Confirm the source no longer contains the old
        # unconditional pattern.
        src = (Path(__file__).resolve().parent.parent / "scripts" / "meridian_connect.py").read_text(
            encoding="utf-8"
        )
        assert 'cd \\"$HOME\\"' not in src, "must not unconditionally fall back to $HOME"


# ---------------------------------------------------------------------------
# GUI installer: -Tray gains a Start Menu shortcut + Add/Remove Programs
# registration (Settings > Apps > Installed apps), and a new -Uninstall
# switch reverses both plus removes meridian-tray.exe. HKCU-only, no admin
# rights -- matches this script's and install.ps1's existing no-admin
# philosophy. Deliberately does NOT touch ~/.local\bin or the user PATH: that
# directory is shared with unrelated tools (uv, serena, ...), unlike
# install.ps1's dedicated $env:APPDATA\meridian directory.
# ---------------------------------------------------------------------------


def _install_windows_ps1_src() -> str:
    return _INSTALL_WINDOWS_PS1.read_text(encoding="utf-8")


def test_install_windows_ps1_is_pure_ascii_no_bom():
    """b0d28a61 fixed 5 pre-existing em-dashes here but never added a
    regression guard (that commit's own message flags this file "was never
    covered by the repo's existing ASCII-enforcement test"). PowerShell 5.1
    reads a BOM-less file as cp1252, so any non-ASCII byte silently corrupts
    characters and can break the parser."""
    raw = _INSTALL_WINDOWS_PS1.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "install-windows.ps1 must not have a UTF-8 BOM"
    assert not raw.startswith(b"\xff\xfe") and not raw.startswith(b"\xfe\xff"), (
        "install-windows.ps1 must not be UTF-16"
    )
    non_ascii = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    assert not non_ascii, f"install-windows.ps1 has non-ASCII bytes at {non_ascii[:10]}"


def _powershell_exe() -> str | None:
    for exe in ("pwsh", "powershell"):
        found = shutil.which(exe)
        if found:
            return found
    return None


def test_install_windows_ps1_parses_with_zero_errors():
    """install.ps1 has this check (test_install_ps1_parses_with_zero_errors);
    install-windows.ps1 never did despite being just as user-facing."""
    ps = _powershell_exe()
    if ps is None:
        pytest.skip("no PowerShell interpreter available on this host")
    ps_script = (
        "$tokens=$null;$errors=$null;"
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{_INSTALL_WINDOWS_PS1.as_posix()}',[ref]$tokens,[ref]$errors)|Out-Null;"
        "if($errors){$errors|ForEach-Object{Write-Output $_.Message};exit 1}"
        "else{Write-Output 'PARSE_OK';exit 0}"
    )
    proc = subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-Command", ps_script],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert proc.returncode == 0, f"install-windows.ps1 failed to parse:\n{proc.stdout}\n{proc.stderr}"
    assert "PARSE_OK" in proc.stdout


def test_install_windows_ps1_exposes_uninstall_switch():
    src = _install_windows_ps1_src()
    assert "[switch]$Uninstall" in src
    assert "if ($Uninstall) {" in src


def test_install_windows_ps1_uninstall_short_circuits_before_tray_and_uv():
    """-Uninstall must be handled before -Tray / uv / meridian.exe logic --
    it is a standalone action, not a modifier of a normal install run."""
    src = _install_windows_ps1_src()
    uninstall_idx = src.index("if ($Uninstall) {")
    tray_idx = src.index("if ($Tray) {")
    uv_idx = src.index("& uv tool install meridian-server")
    assert uninstall_idx < tray_idx, "-Uninstall must be checked before the -Tray branch"
    assert uninstall_idx < uv_idx, "-Uninstall must be checked before the uv/meridian.exe path"


def test_install_windows_ps1_uninstall_never_touches_path_or_bindir_removal():
    """Deliberate design choice, not an oversight: ~/.local\\bin is a SHARED
    per-user bin directory (uv.exe/serena.exe/etc. commonly live there too),
    unlike install.ps1's Meridian-exclusive $env:APPDATA\\meridian. Stripping
    it from PATH or deleting the directory on uninstall could silently break
    unrelated tools, so the -Uninstall block must never do either."""
    src = _install_windows_ps1_src()
    uninstall_idx = src.index("if ($Uninstall) {")
    exit_idx = src.index("exit 0", uninstall_idx)
    uninstall_block = src[uninstall_idx:exit_idx]
    assert "SetEnvironmentVariable" not in uninstall_block
    # Only two specific, narrow file removals happen in this block -- the
    # tray exe and the persisted uninstaller-script copy -- never a bare
    # directory removal of $binDir itself.
    assert "Remove-Item -LiteralPath $exePath" in uninstall_block
    assert "Remove-Item -LiteralPath $uninstallerCopy" in uninstall_block
    assert "Remove-Item -Recurse -Force -Path $binDir" not in uninstall_block
    assert "Remove-Item -LiteralPath $binDir" not in uninstall_block


def test_install_windows_ps1_uninstall_is_idempotent():
    """Every removal in the -Uninstall block must check existence first, and
    each check must be independently guarded so a partial prior removal (or a
    second -Uninstall run) never errors out partway through."""
    src = _install_windows_ps1_src()
    uninstall_idx = src.index("if ($Uninstall) {")
    exit_idx = src.index("exit 0", uninstall_idx)
    uninstall_block = src[uninstall_idx:exit_idx]
    assert "Test-Path -LiteralPath $exePath" in uninstall_block
    assert "Remove-MeridianStartMenuShortcut" in uninstall_block
    assert "Remove-MeridianUninstallEntry" in uninstall_block
    assert "Test-Path -LiteralPath $uninstallerCopy" in uninstall_block
    # $ErrorActionPreference is "Stop" for the whole script, so the exe
    # removal (the one Remove-Item most likely to fail -- e.g. the process is
    # still running) must be try/caught rather than left to propagate and
    # abort the rest of the uninstall sequence.
    exe_removal_idx = uninstall_block.index("Remove-Item -LiteralPath $exePath")
    preceding = uninstall_block[:exe_removal_idx]
    assert preceding.rstrip().endswith("try {"), (
        "the exe removal must be inside its own try block so a failure "
        "(e.g. the process is still running) doesn't abort the rest of -Uninstall"
    )


def test_install_windows_ps1_start_menu_shortcut_targets_current_user_only():
    """Never the all-users Start Menu (%ProgramData%) -- matches the
    no-admin-required philosophy this script and install.ps1 both document."""
    src = _install_windows_ps1_src()
    assert "function New-MeridianStartMenuShortcut" in src
    assert 'Join-Path $env:APPDATA "Microsoft\\Windows\\Start Menu\\Programs\\Meridian.lnk"' in src
    assert "ProgramData" not in src
    assert "AllUsersStartMenu" not in src
    # Uses the standard PowerShell-native COM approach, no extra dependency.
    assert "New-Object -ComObject WScript.Shell" in src


def test_install_windows_ps1_start_menu_shortcut_points_at_tray_exe():
    src = _install_windows_ps1_src()
    fn_idx = src.index("function New-MeridianStartMenuShortcut")
    fn_end = src.index("\nfunction ", fn_idx + 1)
    fn_body = src[fn_idx:fn_end]
    assert "$shortcut.TargetPath = $TargetPath" in fn_body
    assert "$shortcut.Save()" in fn_body
    # Called from the -Tray branch with $dest -- the meridian-tray.exe path.
    tray_idx = src.index("if ($Tray) {")
    exit_idx = src.index("exit 0", tray_idx)
    tray_block = src[tray_idx:exit_idx]
    assert "New-MeridianStartMenuShortcut -TargetPath $dest" in tray_block


def test_install_windows_ps1_registers_hkcu_uninstall_entry_with_documented_values():
    """The real, current Microsoft-documented Uninstall registry key
    convention (Settings > Apps / classic Add-or-Remove-Programs reads both
    HKLM and HKCU under Software\\Microsoft\\Windows\\CurrentVersion\\
    Uninstall\\<key>): DisplayName, DisplayVersion, Publisher,
    UninstallString, InstallLocation, DisplayIcon, EstimatedSize are the
    documented value names this installer sets. HKCU only -- no admin."""
    src = _install_windows_ps1_src()
    assert (
        '$MeridianUninstallKeyPath = "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Meridian"'
        in src
    )
    assert "function Register-MeridianUninstallEntry" in src
    for name in (
        "DisplayName",
        "DisplayVersion",
        "Publisher",
        "UninstallString",
        "InstallLocation",
        "DisplayIcon",
        "EstimatedSize",
        "NoModify",
        "NoRepair",
    ):
        assert name in src, f"missing documented Uninstall registry value {name}"
    # EstimatedSize/NoModify/NoRepair are REG_DWORD, not REG_SZ -- confirmed
    # via the type-dispatch in Register-MeridianUninstallEntry.
    assert '"DWord"' in src and '"String"' in src


def test_install_windows_ps1_never_uses_hklm():
    """Regression guard for the no-admin-required philosophy: this script
    must never write to HKLM (that would require admin rights, a real
    regression from the current design). Matches "HKLM:" (an actual registry
    path prefix), not the bare word -- comments explaining why HKCU is used
    instead legitimately mention "HKLM" in prose."""
    src = _install_windows_ps1_src()
    assert "HKLM:" not in src


def test_install_windows_ps1_uninstall_entry_wired_into_normal_tray_install():
    """Task requirement: the shortcut + registry registration happen as part
    of a normal `-Tray` install, not as a separate opt-in step."""
    src = _install_windows_ps1_src()
    tray_idx = src.index("if ($Tray) {")
    exit_idx = src.index("exit 0", tray_idx)
    tray_block = src[tray_idx:exit_idx]
    assert "New-MeridianStartMenuShortcut -TargetPath $dest" in tray_block
    assert "Register-MeridianUninstallEntry -ExePath $dest -InstallDir $binDir" in tray_block
    # Both calls happen after the download is verified, never before.
    downloaded_idx = tray_block.index("if (-not $downloaded)")
    shortcut_idx = tray_block.index("New-MeridianStartMenuShortcut -TargetPath $dest")
    assert downloaded_idx < shortcut_idx


def test_install_windows_ps1_uninstaller_copy_persists_for_later_invocation():
    """UninstallString must point at something that still exists whenever the
    user later clicks Uninstall -- including when the original install ran
    via `irm | iex` with no local .ps1 file at all. Save-MeridianUninstallerCopy
    prefers copying the actually-running local file and falls back to
    re-downloading a fresh copy from the canonical URL."""
    src = _install_windows_ps1_src()
    assert "function Save-MeridianUninstallerCopy" in src
    fn_idx = src.index("function Save-MeridianUninstallerCopy")
    fn_end = src.index("\nfunction ", fn_idx + 1)
    fn_body = src[fn_idx:fn_end]
    assert "Copy-Item -LiteralPath $LocalSourcePath" in fn_body
    assert "Invoke-WebRequest -Uri $MeridianTrayInstallerSelfUrl" in fn_body
    # The UninstallString invokes the persisted copy with -Uninstall.
    assert '-File `"$uninstallerPath`" -Uninstall' in src


def test_install_windows_ps1_route_serves_uninstall_switch(client):
    r = client.get("/install-windows.ps1")
    assert r.status_code == 200
    assert "[switch]$Uninstall" in r.text
    assert "Register-MeridianUninstallEntry" in r.text
