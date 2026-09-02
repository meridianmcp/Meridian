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
