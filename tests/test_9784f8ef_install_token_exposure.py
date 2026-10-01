"""9784f8ef -- install token exposure (install/tunnel audit 2026-09-27).

Four defects, one theme (a Meridian API token leaking or being invisible):

1. install.ps1 passed the token to meridian-connect.exe as ``--token <value>``,
   which is visible to every other process in a process listing. It now hands
   the token over through the MERIDIAN_TOKEN env var of the child process only.
2. scripts/install_tunnel.ps1 wrote a plaintext ``tunnel_launcher.ps1``
   containing the token, with no ACL. The launcher now holds no token (it reads
   a DPAPI-encrypted file) and both files are locked to the current user.
3. scripts/meridian_connect.py "hardened" hook_auth.conf with ``os.chmod`` --
   a no-op on Windows. It now uses icacls there.
4. The installers, the tunnel client and MCP configs disagreed on the token env
   var name (MERIDIAN_TOKEN vs MERIDIAN_API_KEY vs BEARER_TOKEN). All three are
   now accepted everywhere with one documented precedence.

Every value below is a fixture; nothing here reads a real token, runs an
installer, or touches the real home directory / user config.
"""
from __future__ import annotations

import base64
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_INSTALL_PS1 = _REPO / "install.ps1"
_INSTALL_TUNNEL_PS1 = _REPO / "scripts" / "install_tunnel.ps1"
_CONNECT_PY = _REPO / "scripts" / "meridian_connect.py"

_TOKEN_ENV_NAMES = ("MERIDIAN_TOKEN", "MERIDIAN_API_KEY", "BEARER_TOKEN")
_FAKE = "sk_meridian_faketoken_not_real_0000"  # noqa: S105 -- fixture value

_POWERSHELL = shutil.which("powershell")
_needs_ps = pytest.mark.skipif(_POWERSHELL is None, reason="Windows PowerShell not available")
_windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour")


def _load_connect():
    spec = importlib.util.spec_from_file_location("meridian_connect_9784f8ef", _CONNECT_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def clean_token_env(monkeypatch):
    for name in _TOKEN_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# 4. unified token env var names -- tunnel_client
# ---------------------------------------------------------------------------

def test_tunnel_client_token_env_vars_documented_precedence():
    from meridian import tunnel_client as tc

    assert tc.TOKEN_ENV_VARS == _TOKEN_ENV_NAMES


def test_tunnel_client_resolves_canonical_name(clean_token_env):
    from meridian import tunnel_client as tc

    clean_token_env.setenv("MERIDIAN_TOKEN", "sk_meridian_canonical")
    assert tc._resolve_token() == "sk_meridian_canonical"


def test_tunnel_client_canonical_name_beats_legacy_aliases(clean_token_env):
    from meridian import tunnel_client as tc

    clean_token_env.setenv("MERIDIAN_TOKEN", "sk_meridian_canonical")
    clean_token_env.setenv("MERIDIAN_API_KEY", "sk_meridian_legacy_key")
    clean_token_env.setenv("BEARER_TOKEN", "sk_meridian_legacy_bearer")
    assert tc._resolve_token() == "sk_meridian_canonical"


def test_tunnel_client_legacy_aliases_still_resolve_in_order(clean_token_env):
    from meridian import tunnel_client as tc

    clean_token_env.setenv("BEARER_TOKEN", "sk_meridian_legacy_bearer")
    assert tc._resolve_token() == "sk_meridian_legacy_bearer"
    clean_token_env.setenv("MERIDIAN_API_KEY", "sk_meridian_legacy_key")
    assert tc._resolve_token() == "sk_meridian_legacy_key"


def test_tunnel_client_cli_arg_beats_every_env_var(clean_token_env):
    from meridian import tunnel_client as tc

    for name in _TOKEN_ENV_NAMES:
        clean_token_env.setenv(name, "sk_meridian_from_env")
    assert tc._resolve_token("sk_meridian_from_cli") == "sk_meridian_from_cli"


def test_tunnel_client_skips_blank_env_values_and_strips_bearer(clean_token_env):
    from meridian import tunnel_client as tc

    clean_token_env.setenv("MERIDIAN_TOKEN", "   ")
    clean_token_env.setenv("MERIDIAN_API_KEY", "Bearer sk_meridian_pasted")
    assert tc._resolve_token() == "sk_meridian_pasted"


def test_tunnel_client_and_connect_and_installers_share_one_precedence():
    from meridian import tunnel_client as tc

    assert _load_connect().TOKEN_ENV_VARS == tc.TOKEN_ENV_VARS
    order = re.compile(r"@\('MERIDIAN_TOKEN',\s*'MERIDIAN_API_KEY',\s*'BEARER_TOKEN'\)")
    for script in (_INSTALL_PS1, _INSTALL_TUNNEL_PS1):
        assert order.search(script.read_text(encoding="utf-8")), (
            f"{script.name} must check MERIDIAN_TOKEN, MERIDIAN_API_KEY, BEARER_TOKEN in that order"
        )


# ---------------------------------------------------------------------------
# 1. meridian_connect.py accepts the env-var hand-off (so install.ps1 can stop
#    using --token)
# ---------------------------------------------------------------------------

def test_connect_token_from_env_precedence(clean_token_env):
    mod = _load_connect()
    assert mod._token_from_env() == ""
    clean_token_env.setenv("BEARER_TOKEN", "Bearer sk_meridian_c")
    assert mod._token_from_env() == "sk_meridian_c"
    clean_token_env.setenv("MERIDIAN_API_KEY", "sk_meridian_b")
    assert mod._token_from_env() == "sk_meridian_b"
    clean_token_env.setenv("MERIDIAN_TOKEN", "sk_meridian_a")
    assert mod._token_from_env() == "sk_meridian_a"


@pytest.fixture()
def connect_env(tmp_path, monkeypatch, clean_token_env):
    """Run meridian_connect.main() against a fully faked network and a temp home."""
    home = tmp_path / "home"
    (home / "AppData" / "Roaming").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.chdir(tmp_path)
    mod = _load_connect()
    calls: list[tuple[str, str, str]] = []

    def fake_http(method, url, *, token="", body=None, timeout=10):
        calls.append((method, url, token))
        if url.endswith("/auth/me"):
            return {"email": "tester@example.test"}
        if url.endswith("/auth/tokens"):
            return {"token": "sk_meridian_permanent_fixture"}
        return {}

    real_which = shutil.which

    def fake_which(cmd, *a, **k):
        # No Claude Code / Codex / Cursor "detected": only hook_auth.conf is written.
        if cmd in ("claude", "codex", "cursor"):
            return None
        return real_which(cmd, *a, **k)

    monkeypatch.setattr(mod, "_http", fake_http)
    monkeypatch.setattr(mod.shutil, "which", fake_which)
    monkeypatch.setattr(mod.webbrowser, "open", lambda *_a, **_k: True)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    return mod, calls, monkeypatch, home


def _run_main(mod, monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["meridian_connect.py", *argv])
    return mod.main()


def test_connect_hosted_install_takes_token_from_env_without_dash_dash_token(connect_env):
    mod, calls, monkeypatch, _home = connect_env
    monkeypatch.setenv("MERIDIAN_TOKEN", "sk_meridian_env_fixture")
    rc = _run_main(mod, monkeypatch, "--url", "https://meridian.example.test")
    assert rc == 0
    validated = [c for c in calls if c[1].endswith("/auth/me")]
    assert validated and validated[0][2] == "sk_meridian_env_fixture"


def test_connect_dash_dash_token_still_works_and_beats_env(connect_env):
    mod, calls, monkeypatch, _home = connect_env
    monkeypatch.setenv("MERIDIAN_TOKEN", "sk_meridian_env_fixture")
    rc = _run_main(mod, monkeypatch, "--url", "https://meridian.example.test", "--token", "sk_meridian_cli_fixture")
    assert rc == 0
    validated = [c for c in calls if c[1].endswith("/auth/me")]
    assert validated[0][2] == "sk_meridian_cli_fixture"


def test_connect_hosted_without_any_token_fails_with_actionable_message(connect_env, capsys):
    mod, _calls, monkeypatch, _home = connect_env
    rc = _run_main(mod, monkeypatch, "--url", "https://meridian.example.test")
    assert rc == 1
    assert "MERIDIAN_TOKEN" in capsys.readouterr().err


def test_connect_local_server_never_receives_an_env_token(connect_env):
    mod, calls, monkeypatch, _home = connect_env
    monkeypatch.setenv("MERIDIAN_TOKEN", "sk_meridian_env_fixture")
    rc = _run_main(mod, monkeypatch, "--url", "http://localhost:7878")
    assert rc == 0
    assert all(c[2] == "" for c in calls), "a hosted token must not be forwarded to a local server"


# ---------------------------------------------------------------------------
# 3. real owner-only permissions (chmod is a no-op on Windows)
# ---------------------------------------------------------------------------

def _windows_module(mod):
    return types.SimpleNamespace(system=lambda: "Windows")


def test_restrict_to_owner_uses_icacls_on_windows(tmp_path, monkeypatch):
    mod = _load_connect()
    target = tmp_path / "secret.conf"
    target.write_text("x", encoding="utf-8")
    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(mod, "platform", _windows_module(mod))
    monkeypatch.setattr(mod.shutil, "which", lambda name, *a, **k: "C:/Windows/System32/icacls.exe")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setenv("USERNAME", "fixtureuser")
    monkeypatch.setenv("USERDOMAIN", "FIXTUREHOST")
    assert mod._restrict_to_owner(target) is True
    cmd = seen["cmd"]
    assert cmd[0].endswith("icacls.exe") and cmd[1] == str(target)
    assert "/inheritance:r" in cmd, "inherited ACEs (Users/Everyone/...) must be dropped"
    assert "/grant:r" in cmd
    assert cmd[cmd.index("/grant:r") + 1] == "FIXTUREHOST\\fixtureuser:F"


def test_restrict_to_owner_grants_directory_inheritance_flags(tmp_path, monkeypatch):
    mod = _load_connect()
    seen: dict = {}
    monkeypatch.setattr(mod, "platform", _windows_module(mod))
    monkeypatch.setattr(mod.shutil, "which", lambda name, *a, **k: "icacls")
    monkeypatch.setattr(
        mod.subprocess, "run", lambda cmd, **kw: seen.setdefault("cmd", cmd) and types.SimpleNamespace(returncode=0)
    )
    monkeypatch.setenv("USERNAME", "u")
    monkeypatch.setenv("USERDOMAIN", "D")
    assert mod._restrict_to_owner(tmp_path) is True
    assert seen["cmd"][seen["cmd"].index("/grant:r") + 1] == "D\\u:(OI)(CI)F"


def test_restrict_to_owner_reports_failure_instead_of_raising(tmp_path, monkeypatch):
    mod = _load_connect()
    target = tmp_path / "secret.conf"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setattr(mod, "platform", _windows_module(mod))

    # icacls not installed
    monkeypatch.setattr(mod.shutil, "which", lambda name, *a, **k: None)
    assert mod._restrict_to_owner(target) is False

    # icacls exits non-zero
    monkeypatch.setattr(mod.shutil, "which", lambda name, *a, **k: "icacls")
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(returncode=5))
    assert mod._restrict_to_owner(target) is False

    # icacls cannot even be started
    def boom(cmd, **kw):
        raise OSError("cannot spawn")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert mod._restrict_to_owner(target) is False


def test_restrict_to_owner_uses_chmod_600_and_700_on_posix(tmp_path, monkeypatch):
    mod = _load_connect()
    modes: list[tuple[str, int]] = []
    monkeypatch.setattr(mod, "platform", types.SimpleNamespace(system=lambda: "Linux"))
    monkeypatch.setattr(mod.os, "chmod", lambda p, m: modes.append((str(p), m)))
    f = tmp_path / "f"
    f.write_text("x", encoding="utf-8")
    assert mod._restrict_to_owner(f) is True
    assert mod._restrict_to_owner(tmp_path) is True
    assert modes == [(str(f), 0o600), (str(tmp_path), 0o700)]


def test_write_private_file_locks_down_before_the_secret_lands(tmp_path, monkeypatch):
    """The token must never sit in a not-yet-restricted file."""
    mod = _load_connect()
    target = tmp_path / "hook_auth.conf"
    seen: dict = {}

    def fake_restrict(path):
        seen["content_at_restrict_time"] = Path(path).read_text(encoding="utf-8")
        return True

    monkeypatch.setattr(mod, "platform", _windows_module(mod))
    monkeypatch.setattr(mod, "_restrict_to_owner", fake_restrict)
    assert mod._write_private_file(target, f"secret {_FAKE}") is True
    assert seen["content_at_restrict_time"] == "", "ACL must be applied to the still-empty file"
    assert target.read_text(encoding="utf-8") == f"secret {_FAKE}"


def test_write_private_file_creates_posix_file_with_mode_600(tmp_path, monkeypatch):
    mod = _load_connect()
    target = tmp_path / "hook_auth.conf"
    modes: list[int] = []
    real_open = os.open

    def spy_open(path, flags, mode=0o777, **kw):
        if str(path) == str(target):  # ignore os.open calls made by pytest itself
            modes.append(mode)
        return real_open(path, flags, mode, **kw)

    monkeypatch.setattr(mod, "platform", types.SimpleNamespace(system=lambda: "Linux"))
    monkeypatch.setattr(mod.os, "open", spy_open)
    monkeypatch.setattr(mod, "_restrict_to_owner", lambda p: True)
    assert mod._write_private_file(target, "abc") is True
    assert modes == [0o600], "the file must be created owner-only, not chmod-ed afterwards"
    assert target.read_text(encoding="utf-8") == "abc"


def test_write_private_file_hardens_existing_posix_file_before_overwrite(tmp_path, monkeypatch):
    mod = _load_connect()
    target = tmp_path / "hook_auth.conf"
    target.write_text("previous contents", encoding="utf-8")
    monkeypatch.setattr(mod, "platform", types.SimpleNamespace(system=lambda: "Linux"))
    content_seen_during_hardening: list[str] = []

    def fake_restrict(path):
        content_seen_during_hardening.append(Path(path).read_text(encoding="utf-8"))
        return True

    monkeypatch.setattr(mod, "_restrict_to_owner", fake_restrict)
    assert mod._write_private_file(target, f"secret {_FAKE}") is True
    assert content_seen_during_hardening == ["previous contents"]
    assert target.read_text(encoding="utf-8") == f"secret {_FAKE}"


def test_write_private_file_does_not_write_if_existing_posix_file_cannot_be_hardened(
    tmp_path, monkeypatch
):
    mod = _load_connect()
    target = tmp_path / "hook_auth.conf"
    target.write_text("previous contents", encoding="utf-8")
    monkeypatch.setattr(mod, "platform", types.SimpleNamespace(system=lambda: "Linux"))
    monkeypatch.setattr(mod, "_restrict_to_owner", lambda path: False)

    assert mod._write_private_file(target, f"secret {_FAKE}") is False
    assert target.read_text(encoding="utf-8") == "previous contents"


def test_write_private_file_does_not_write_token_when_windows_acl_fails(
    tmp_path, monkeypatch,
):
    mod = _load_connect()
    target = tmp_path / "hook_auth.conf"
    monkeypatch.setattr(
        mod, "platform", types.SimpleNamespace(system=lambda: "Windows"),
    )
    monkeypatch.setattr(mod, "_restrict_to_owner", lambda path: False)

    assert mod._write_private_file(target, f"secret {_FAKE}") is False
    assert target.read_text(encoding="utf-8") == ""


def test_curl_header_config_fails_closed_when_hardening_fails(
    tmp_path, monkeypatch, capsys,
):
    mod = _load_connect()
    monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(mod, "_write_private_file", lambda path, text: False)
    cfg = mod._write_curl_header_config(_FAKE)
    assert cfg == ""
    err = capsys.readouterr().err
    assert "WARNING" in err and "hook_auth.conf" in err
    assert _FAKE not in err, "the warning must never echo the token"


def _icacls_aces(path: Path) -> list[str]:
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True, timeout=30).stdout
    return [ln for ln in out.splitlines() if re.search(r":\(", ln)]


@_windows_only
def test_curl_header_config_is_really_owner_only_on_windows(tmp_path, monkeypatch):
    """The old os.chmod(0o600) left the inherited ACL (Users/SYSTEM/Administrators)
    untouched on Windows. After the fix only the current user has an ACE and
    nothing is inherited."""
    mod = _load_connect()
    monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: tmp_path))
    cfg = Path(mod._write_curl_header_config(_FAKE))
    assert cfg.read_text(encoding="utf-8").startswith('header = "Authorization: Bearer ')
    aces = _icacls_aces(cfg)
    assert len(aces) == 1, f"expected a single owner-only ACE, got: {aces}"
    assert "(I)" not in aces[0], "inherited ACEs must be removed"
    assert os.environ["USERNAME"].lower() in aces[0].lower()


# ---------------------------------------------------------------------------
# 2. install.ps1 -- the token is never on the binary's command line
# ---------------------------------------------------------------------------

def _code_lines(script: Path) -> list[str]:
    return [ln for ln in script.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")]


def test_install_ps1_never_appends_the_token_to_the_binary_args():
    code = "\n".join(_code_lines(_INSTALL_PS1))
    assert "$binaryArgs += @('--token'" not in code
    offenders = [
        ln.strip()
        for ln in code.splitlines()
        if "--token" in ln
        # the only legitimate mentions are the lines that LIFT a caller-supplied
        # --token off the command line
        and "-eq '--token'" not in ln
        and "-like '--token=*'" not in ln
        and "'--token='.Length" not in ln
    ]
    assert not offenders, f"install.ps1 must not build a --token argument: {offenders}"


def test_install_ps1_hands_the_token_over_via_child_env_and_restores_it():
    src = _INSTALL_PS1.read_text(encoding="utf-8")
    run_idx = src.index("& $dest @binaryArgs")
    assert "$env:MERIDIAN_TOKEN = $childToken" in src[:run_idx], "env var must be set before launch"
    tail = src[run_idx:]
    assert "} finally {" in tail[:200], "the launch must sit in try/finally so the env is restored"
    assert "Remove-Item Env:\\MERIDIAN_TOKEN" in tail
    assert "$env:MERIDIAN_TOKEN = $prevMeridianToken" in tail


def _extract_between(text: str, start: str, end: str) -> str:
    s = text.index(start)
    return text[s : text.index(end, s)]


def _ps(script: str, env_extra: dict | None = None, timeout: int = 90) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _TOKEN_ENV_NAMES}
    env.update(env_extra or {})
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        [_POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def _load_ps_functions(script: Path, *names: str) -> str:
    """PowerShell prelude that defines ``names`` from ``script`` via the AST --
    the installer itself is never executed."""
    quoted = ",".join(f"'{n}'" for n in names)
    return (
        "$t=$null;$e=$null;"
        f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{script.as_posix()}',[ref]$t,[ref]$e);"
        "if($e){throw 'parse errors'};"
        f"$want=@({quoted});"
        "$ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]},$true)"
        " | Where-Object { $want -contains $_.Name }"
        " | ForEach-Object { Invoke-Expression $_.Extent.Text };\n"
    )


@pytest.mark.subprocess_isolated
@_needs_ps
@pytest.mark.parametrize(
    "passthrough",
    [
        "@('--url','https://meridian.example.test','--token','%s','--tunnel')" % _FAKE,
        "@('--url','https://meridian.example.test','--token=%s','--tunnel')" % _FAKE,
    ],
    ids=["space-form", "equals-form"],
)
def test_install_ps1_lifts_a_caller_supplied_token_off_the_command_line(passthrough):
    src = _INSTALL_PS1.read_text(encoding="utf-8")
    segment = _extract_between(
        src, "$binaryArgs = @()", "$hasToken = -not [string]::IsNullOrWhiteSpace($childToken)"
    )
    proc = _ps(
        f"$passthroughArgs = {passthrough}\n{segment}\n"
        "Write-Output ('ARGS=' + ($binaryArgs -join '|'))\nWrite-Output ('TOKEN=' + $childToken)\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert "ARGS=--url|https://meridian.example.test|--tunnel" in proc.stdout
    args_line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("ARGS="))
    assert _FAKE not in args_line and "--token" not in args_line
    assert f"TOKEN={_FAKE}" in proc.stdout


@pytest.mark.subprocess_isolated
@_needs_ps
def test_install_ps1_env_token_helper_honours_precedence_and_prefix():
    prelude = _load_ps_functions(_INSTALL_PS1, "Get-MeridianEnvToken")
    run = lambda env: _ps(prelude + "Write-Output ('GOT=' + (Get-MeridianEnvToken))", env)  # noqa: E731
    assert "GOT=sk_meridian_alias_key" in run({"MERIDIAN_API_KEY": "sk_meridian_alias_key"}).stdout
    both = run({"MERIDIAN_TOKEN": "sk_meridian_canon", "MERIDIAN_API_KEY": "sk_meridian_alias_key"}).stdout
    assert "GOT=sk_meridian_canon" in both
    assert "GOT=sk_meridian_bearer_x" in run({"BEARER_TOKEN": "Bearer sk_meridian_bearer_x"}).stdout
    # Not an sk_meridian_ token -> ignored, exactly like the pre-existing behaviour.
    assert run({"MERIDIAN_TOKEN": "not-a-meridian-token"}).stdout.strip().endswith("GOT=")


# ---------------------------------------------------------------------------
# 2. install_tunnel.ps1 -- no plaintext token, ACL'd launcher
# ---------------------------------------------------------------------------

def test_install_tunnel_ps1_no_longer_interpolates_a_plaintext_token():
    code = "\n".join(_code_lines(_INSTALL_TUNNEL_PS1))
    assert "$MeridianToken" not in code, "the plaintext token variable must be gone"
    assert 'MERIDIAN_API_KEY = "' not in code
    assert "ConvertFrom-SecureString" in code, "token must be stored DPAPI-encrypted"
    assert "icacls" in code, "files holding the token material must be ACL-restricted"
    assert "-AsSecureString" in code, "the interactive prompt must not echo the token"


def test_install_tunnel_ps1_is_still_ascii_and_keeps_the_npm_hint():
    raw = _INSTALL_TUNNEL_PS1.read_bytes()
    assert not [b for b in raw if b > 0x7F], "must stay pure ASCII for Windows PowerShell 5.1"
    assert b"npm i -g @meridianmcp/mcp" in raw, "7a45a55f npm hint must be preserved"


@pytest.mark.subprocess_isolated
@_needs_ps
def test_install_tunnel_env_token_helper_precedence():
    prelude = _load_ps_functions(_INSTALL_TUNNEL_PS1, "Get-MeridianEnvToken")
    run = lambda env: _ps(prelude + "Write-Output ('GOT=' + (Get-MeridianEnvToken))", env)  # noqa: E731
    assert "GOT=legacy-key" in run({"MERIDIAN_API_KEY": "legacy-key"}).stdout
    assert "GOT=canon" in run({"MERIDIAN_TOKEN": "canon", "MERIDIAN_API_KEY": "legacy-key"}).stdout
    assert "GOT=legacy-bearer" in run({"BEARER_TOKEN": "legacy-bearer"}).stdout


@pytest.mark.subprocess_isolated
@_needs_ps
def test_install_tunnel_launcher_text_holds_no_token_and_cannot_be_injected():
    prelude = _load_ps_functions(_INSTALL_TUNNEL_PS1, "ConvertTo-MeridianPsLiteral", "New-MeridianLauncherText")
    proc = _ps(
        prelude
        + "$repo = 'C:\\Users\\o''brien\\$(calc)\\repo'\n"
        + "$text = New-MeridianLauncherText -MeridianUrl 'https://meridian.example.test' "
        + "-TokenFile 'C:\\t\\tunnel_token.dat' -MeridianRepo $repo -MeridianExe 'C:\\bin\\meridian.exe'\n"
        + "$errs = $null; $tok = $null\n"
        + "[System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tok, [ref]$errs) | Out-Null\n"
        + "Write-Output ('PARSE_ERRORS=' + @($errs).Count)\n"
        + "Write-Output ('HAS_SK=' + ($text -match 'sk_meridian_'))\n"
        + "Write-Output ('HAS_SECURESTRING=' + ($text -match 'ConvertTo-SecureString'))\n"
        # PS single-quoted: the expected literal is  'C:\Users\o''brien\$(calc)\repo'
        # (embedded quote doubled, no expansion). Never use a double-quoted string
        # here -- "$(calc)" would be evaluated by PowerShell.
        + r"$expected = '''C:\Users\o''''brien\$(calc)\repo'''" + "\n"
        + "Write-Output ('LITERAL_OK=' + $text.Contains($expected))\n"
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "PARSE_ERRORS=0" in out
    assert "HAS_SK=False" in out
    assert "HAS_SECURESTRING=True" in out
    assert "LITERAL_OK=True" in out, "paths must be emitted as single-quoted literals (no $() expansion)"


@pytest.mark.subprocess_isolated
@_needs_ps
@_windows_only
def test_install_tunnel_dpapi_roundtrip_and_owner_only_acl(tmp_path):
    """End to end on the real Windows primitives (DPAPI + icacls) in a temp dir:
    the on-disk token file and launcher never contain the token, both are
    owner-only, and running the generated launcher exports the token to the
    tunnel process environment."""
    prelude = _load_ps_functions(
        _INSTALL_TUNNEL_PS1,
        "Set-MeridianOwnerOnlyAcl", "ConvertTo-MeridianPsLiteral", "New-MeridianLauncherText",
        "Write-MeridianOwnerOnlyFile",
    )
    tmp = str(tmp_path)
    proc = _ps(
        prelude
        + f"$tmp = '{tmp}'\n"
        + "$fake = Join-Path $tmp 'fake_meridian.cmd'\n"
        + "Set-Content -LiteralPath $fake -Encoding ASCII -Value '@echo RAN TOKEN=%MERIDIAN_TOKEN% KEY=%MERIDIAN_API_KEY% URL=%MERIDIAN_URL% ARGS=%*'\n"
        + f"$secure = ConvertTo-SecureString -String '{_FAKE}' -AsPlainText -Force\n"
        + "$blob = $secure | ConvertFrom-SecureString\n"
        + "$tokenFile = Join-Path $tmp 'tunnel_token.dat'\n"
        + "$h1 = Write-MeridianOwnerOnlyFile -Path $tokenFile -Content $blob\n"
        + "$text = New-MeridianLauncherText -MeridianUrl 'https://meridian.example.test' -TokenFile $tokenFile -MeridianRepo $tmp -MeridianExe $fake\n"
        + "$launcher = Join-Path $tmp 'tunnel_launcher.ps1'\n"
        + "$h2 = Write-MeridianOwnerOnlyFile -Path $launcher -Content $text\n"
        + "Write-Output ('HARDENED=' + $h1 + '/' + $h2)\n"
        + "Write-Output ('TOKENFILE_PLAINTEXT=' + ((Get-Content -Raw -LiteralPath $tokenFile) -like '*faketoken*'))\n"
        + "Write-Output ('LAUNCHER_PLAINTEXT=' + ((Get-Content -Raw -LiteralPath $launcher) -like '*faketoken*'))\n"
        + "Invoke-Expression (Get-Content -Raw -LiteralPath $launcher)\n"
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    out = proc.stdout
    assert "HARDENED=True/True" in out
    assert "TOKENFILE_PLAINTEXT=False" in out
    assert "LAUNCHER_PLAINTEXT=False" in out
    assert (
        f"RAN TOKEN={_FAKE} KEY={_FAKE} URL=https://meridian.example.test ARGS=--tunnel --repo" in out
    ), out
    for name in ("tunnel_token.dat", "tunnel_launcher.ps1"):
        aces = _icacls_aces(tmp_path / name)
        assert len(aces) == 1 and "(I)" not in aces[0], f"{name} must be owner-only: {aces}"
        assert os.environ["USERNAME"].lower() in aces[0].lower()
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name != "fake_meridian.cmd":
            assert _FAKE.encode() not in path.read_bytes(), f"plaintext token found in {path.name}"
