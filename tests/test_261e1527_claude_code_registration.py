"""261e1527 -- meridian_connect.py and Claude Code.

Found by the 2026-09-27 install/tunnel audit:

- On Windows the installer wrote the Claude Code hooks to
  ``%APPDATA%\\Claude\\settings.json``. Claude Code reads
  ``%USERPROFILE%\\.claude\\settings.json`` (Claude Code settings docs, checked
  2026-09-28; ``CLAUDE_CONFIG_DIR`` relocates it) -- ``%APPDATA%\\Claude`` is
  Claude Desktop's folder -- so the hooks were dead.
- It replaced the user's whole SessionStart/Stop hook lists, and any settings
  file that failed to parse was silently rewritten with ONLY the hooks.
- Nothing registered the Meridian MCP server with Claude Code.

The real ``claude`` CLI is never run here: ``subprocess.run`` is stubbed for
every test in this module. Everything uses temp dirs and fixture values.
"""
from __future__ import annotations

import importlib.util
import json
import platform
import shutil
import sys
import types
from pathlib import Path

import pytest

# On Windows the first platform.system() call shells out to `ver` through
# subprocess; warm that cache now so the tests below, which stub
# subprocess.run, don't record it as a call.
platform.system()

_CONNECT_PY = Path(__file__).resolve().parent.parent / "scripts" / "meridian_connect.py"
_FAKE = "sk_meridian_faketoken_not_real_0000"  # noqa: S105 -- fixture value
_URL = "https://meridian.example.test"
_START = f"curl -s -X POST -K \"/h/hook_auth.conf\" '{_URL}/hooks/session-start' | jq -r '.x'"
_STOP = f"curl -s -X POST -K \"/h/hook_auth.conf\" '{_URL}/hooks/stop' >/dev/null 2>&1"
_OLD_START = "curl -s -X POST -H 'Authorization: Bearer old' 'https://old.example.test/hooks/session-start'"
_OLD_STOP = "curl -s -X POST -H 'Authorization: Bearer old' 'https://old.example.test/hooks/stop'"


def _load():
    spec = importlib.util.spec_from_file_location("meridian_connect_261e1527", _CONNECT_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod(monkeypatch):
    m = _load()

    def _never(*_a, **_k):  # the real claude / icacls must never be spawned by these tests
        raise AssertionError("subprocess.run was called without being stubbed")

    monkeypatch.setattr(m.subprocess, "run", _never)
    return m


# ---------------------------------------------------------------------------
# hooks land in the file Claude Code reads
# ---------------------------------------------------------------------------

def test_settings_path_is_dot_claude_under_the_user_profile_even_on_windows(mod, tmp_path, monkeypatch):
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    for system in ("Windows", "Linux", "Darwin"):
        monkeypatch.setattr(mod, "platform", types.SimpleNamespace(system=lambda s=system: s))
        got = mod._settings_path()
        assert got == home / ".claude" / "settings.json", system
        assert appdata not in got.parents, "must not be Claude Desktop's %APPDATA%\\Claude folder"


def test_settings_path_honours_claude_config_dir(mod, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert mod._settings_path() == tmp_path / "cfg" / "settings.json"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "   ")
    assert mod._settings_path().name == "settings.json" and mod._settings_path().parent.name == ".claude"


# ---------------------------------------------------------------------------
# hooks are merged, not overwritten
# ---------------------------------------------------------------------------

USER_SETTINGS = {
    "model": "opus",
    "permissions": {"allow": ["Bash(git status)"]},
    "env": {"FOO": "bar"},
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo pre"}]}],
        "SessionStart": [
            {"matcher": "startup", "hooks": [{"type": "command", "command": "echo my own session hook"}]},
            {"matcher": "", "hooks": [
                {"type": "command", "command": "echo shares a group with meridian"},
                {"type": "command", "command": _OLD_START},
            ]},
            {"matcher": "", "hooks": [{"type": "command", "command": _OLD_START}]},
        ],
        "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "echo my stop hook"}]}],
    },
}


def _hook_commands(settings, event):
    return [h["command"] for g in settings["hooks"][event] for h in g["hooks"]]


def test_merge_preserves_everything_that_is_not_meridians(mod, tmp_path):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps(USER_SETTINGS, indent=2), encoding="utf-8")

    ok, notes = mod._configure_claude_hooks(path, _START, _STOP)
    assert ok, notes
    out = json.loads(path.read_text(encoding="utf-8"))

    for key in ("model", "permissions", "env"):
        assert out[key] == USER_SETTINGS[key]
    assert out["hooks"]["PreToolUse"] == USER_SETTINGS["hooks"]["PreToolUse"]

    start = _hook_commands(out, "SessionStart")
    assert "echo my own session hook" in start
    assert "echo shares a group with meridian" in start, "a user's hook sharing a group with ours must survive"
    assert _OLD_START not in start, "Meridian's stale hook is replaced"
    assert start.count(_START) == 1

    stop = _hook_commands(out, "Stop")
    assert "echo my stop hook" in stop and stop.count(_STOP) == 1
    # the group that held only Meridian's old hook is gone, not left empty
    assert all(g["hooks"] for g in out["hooks"]["SessionStart"])


def test_merge_is_idempotent(mod, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(USER_SETTINGS, indent=2), encoding="utf-8")
    assert mod._configure_claude_hooks(path, _START, _STOP)[0]
    first = path.read_text(encoding="utf-8")
    assert mod._configure_claude_hooks(path, _START, _STOP)[0]
    assert path.read_text(encoding="utf-8") == first
    assert first.count(_START.replace("\\", "\\\\").replace('"', '\\"')) == 1


def test_first_write_keeps_a_pristine_backup_once(mod, tmp_path):
    path = tmp_path / "settings.json"
    original = json.dumps(USER_SETTINGS, indent=2)
    path.write_text(original, encoding="utf-8")
    mod._configure_claude_hooks(path, _START, _STOP)
    backup = tmp_path / "settings.json.meridian-bak"
    assert backup.read_text(encoding="utf-8") == original
    mod._configure_claude_hooks(path, _START, _STOP)
    assert backup.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("*.meridian-tmp"))


@pytest.mark.parametrize(
    "content",
    [
        '{\n  // comment\n  "hooks": {}\n}',
        "{ not json",
        "[1, 2]",
        '{"hooks": []}',
        '{"hooks": {"SessionStart": {"oops": true}}}',
        '{"hooks": {"Stop": "nope"}}',
    ],
    ids=["jsonc", "broken", "not-object", "hooks-not-object", "event-not-list", "stop-not-list"],
)
def test_unparseable_or_unexpected_settings_are_never_overwritten(mod, tmp_path, content):
    """The old code fell back to {} and rewrote the whole file with only the hooks."""
    path = tmp_path / "settings.json"
    path.write_text(content, encoding="utf-8")
    ok, notes = mod._configure_claude_hooks(path, _START, _STOP)
    assert ok is False and any("WARNING" in n for n in notes)
    assert path.read_text(encoding="utf-8") == content
    assert not (tmp_path / "settings.json.meridian-bak").exists()


def test_creates_settings_and_parent_dir_when_missing(mod, tmp_path):
    path = tmp_path / "fresh" / ".claude" / "settings.json"
    assert mod._configure_claude_hooks(path, _START, _STOP)[0]
    out = json.loads(path.read_text(encoding="utf-8"))
    assert _hook_commands(out, "SessionStart") == [_START]
    assert _hook_commands(out, "Stop") == [_STOP]
    assert not (tmp_path / "fresh" / ".claude" / "settings.json.meridian-bak").exists()


def test_empty_settings_file_is_treated_as_empty_object(mod, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("  \n", encoding="utf-8")
    assert mod._configure_claude_hooks(path, _START, _STOP)[0]
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {"hooks"}


def test_formatting_and_non_ascii_survive_the_merge(mod, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{\n\t"statusLine": "caf\u00e9 \u2713"\n}\n', encoding="utf-8")
    assert mod._configure_claude_hooks(path, _START, _STOP)[0]
    text = path.read_text(encoding="utf-8")
    assert '\n\t"statusLine": "caf\u00e9 \u2713"' in text, "tab indent + non-ASCII characters preserved"


# ---------------------------------------------------------------------------
# MCP registration through the documented CLI
# ---------------------------------------------------------------------------

def _recording_run(monkeypatch, mod, *, returncode=0, stdout="", stderr="", raises=None):
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if raises:
            raise raises
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    return calls


def test_registration_uses_claude_mcp_add_with_header_after_name_and_url(mod, monkeypatch):
    calls = _recording_run(monkeypatch, mod)
    ok, notes = mod._register_claude_mcp(_URL, _FAKE, "/usr/bin/claude")
    assert ok and any("registered" in n for n in notes)
    assert calls == [[
        "/usr/bin/claude", "mcp", "add", "--transport", "http", "--scope", "user",
        "meridian", f"{_URL}/mcp", "--header", f"Authorization: Bearer {_FAKE}",
    ]]
    cmd = calls[0]
    # --header is variadic in Claude's CLI: it must come after <name> <url>
    assert cmd.index("--header") > cmd.index(f"{_URL}/mcp") > cmd.index("meridian")
    assert _FAKE not in "\n".join(notes)


def test_registration_without_a_token_sends_no_header(mod, monkeypatch):
    calls = _recording_run(monkeypatch, mod)
    assert mod._register_claude_mcp("http://localhost:7878", "", "claude")[0]
    assert "--header" not in calls[0] and calls[0][-1] == "http://localhost:7878/mcp"


def test_missing_cli_prints_exact_manual_instructions_and_runs_nothing(mod, monkeypatch):
    calls = _recording_run(monkeypatch, mod)
    ok, notes = mod._register_claude_mcp(_URL, _FAKE, None)
    assert ok is False and calls == []
    text = "\n".join(notes)
    assert f"claude mcp add --transport http --scope user meridian {_URL}/mcp" in text
    assert "<your-meridian-token>" in text and _FAKE not in text


def test_existing_registration_is_left_alone(mod, monkeypatch):
    calls = _recording_run(
        monkeypatch, mod, returncode=1, stderr="MCP server meridian already exists in user config"
    )
    ok, notes = mod._register_claude_mcp(_URL, _FAKE, "claude")
    assert ok is True and len(calls) == 1, "no remove/replace guesswork -- only the one documented add"
    assert any("already registered" in n for n in notes)


def test_failed_registration_reports_manual_command_and_never_echoes_the_token(mod, monkeypatch):
    _recording_run(monkeypatch, mod, returncode=2, stderr=f"boom while sending Bearer {_FAKE} to server")
    ok, notes = mod._register_claude_mcp(_URL, _FAKE, "claude")
    assert ok is False
    text = "\n".join(notes)
    assert "exit 2" in text and "claude mcp add --transport http --scope user meridian" in text
    assert _FAKE not in text and "<redacted>" in text


def test_unstartable_cli_falls_back_to_manual_instructions(mod, monkeypatch):
    _recording_run(monkeypatch, mod, raises=OSError("boom"))
    ok, notes = mod._register_claude_mcp(_URL, _FAKE, "claude")
    assert ok is False and any("Run: claude mcp add" in n for n in notes)


def test_cmd_shim_with_shell_metacharacters_is_not_run(mod, monkeypatch):
    calls = _recording_run(monkeypatch, mod)
    monkeypatch.setattr(mod, "platform", types.SimpleNamespace(system=lambda: "Windows"))
    ok, notes = mod._register_claude_mcp("https://x.example.test/?a=1&b=2", _FAKE, "C:/npm/claude.cmd")
    assert ok is False and calls == []
    assert any("cmd.exe" in n for n in notes)
    # a plain .cmd shim with safe arguments is fine
    assert mod._register_claude_mcp(_URL, _FAKE, "C:/npm/claude.cmd")[0] and len(calls) == 1


# ---------------------------------------------------------------------------
# main() end to end on a temp home
# ---------------------------------------------------------------------------

@pytest.fixture()
def run_main(mod, tmp_path, monkeypatch):
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    (home / ".claude").mkdir(parents=True)
    appdata.mkdir(parents=True)
    for var in ("MERIDIAN_TOKEN", "MERIDIAN_API_KEY", "BEARER_TOKEN", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: home))
    monkeypatch.chdir(tmp_path)

    def fake_http(method, url, *, token="", body=None, timeout=10):
        if url.endswith("/auth/me"):
            return {"email": "tester@example.test"}
        if url.endswith("/auth/tokens"):
            return {"token": "sk_meridian_permanent_fixture"}
        return {}

    real_which = shutil.which
    state = {"claude": "C:/fake-npm/claude.cmd"}
    monkeypatch.setattr(mod, "_http", fake_http)
    monkeypatch.setattr(mod, "_restrict_to_owner", lambda p: True)
    monkeypatch.setattr(
        mod.shutil, "which",
        lambda c, *a, **k: state["claude"] if c == "claude" else (None if c in ("codex", "cursor") else real_which(c, *a, **k)),
    )
    monkeypatch.setattr(mod.webbrowser, "open", lambda *_a, **_k: True)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(sys, "argv", ["meridian_connect.py", "--url", _URL, "--token", "sk_meridian_cli_fixture"])
    return mod, home, appdata, state


def test_main_writes_hooks_to_dot_claude_merges_them_and_registers_the_mcp_server(run_main, monkeypatch, capsys):
    mod, home, appdata, _state = run_main
    settings = home / ".claude" / "settings.json"
    settings.write_text(json.dumps(USER_SETTINGS, indent=2), encoding="utf-8")
    calls = _recording_run(monkeypatch, mod)

    assert mod.main() == 0

    out = json.loads(settings.read_text(encoding="utf-8"))
    assert out["model"] == "opus" and out["permissions"] == USER_SETTINGS["permissions"]
    assert "echo my own session hook" in _hook_commands(out, "SessionStart")
    assert any("/hooks/session-start" in c for c in _hook_commands(out, "SessionStart"))
    assert not (appdata / "Claude").exists(), "nothing may be written to %APPDATA%\\Claude"

    assert len(calls) == 1 and calls[0][1:4] == ["mcp", "add", "--transport"]
    assert calls[0][-1] == "Authorization: Bearer sk_meridian_permanent_fixture"
    captured = capsys.readouterr()
    assert "sk_meridian_permanent_fixture" not in captured.out + captured.err
    assert "registered the 'meridian' MCP server" in captured.out


def test_main_without_the_cli_still_writes_hooks_and_prints_manual_steps(run_main, monkeypatch, capsys):
    mod, home, _appdata, state = run_main
    state["claude"] = None
    settings = home / ".claude" / "settings.json"
    settings.write_text("{}", encoding="utf-8")  # Claude Code "detected" via its settings file
    calls = _recording_run(monkeypatch, mod)

    assert mod.main() == 0

    assert calls == []
    assert "hooks" in json.loads(settings.read_text(encoding="utf-8"))
    out = capsys.readouterr().out
    assert "claude mcp add --transport http --scope user meridian" in out
    assert "sk_meridian_permanent_fixture" not in out


def test_main_leaves_broken_settings_alone_but_still_attempts_registration(run_main, monkeypatch, capsys):
    mod, home, _appdata, _state = run_main
    settings = home / ".claude" / "settings.json"
    settings.write_text("{ // not json", encoding="utf-8")
    calls = _recording_run(monkeypatch, mod)

    assert mod.main() == 0

    assert settings.read_text(encoding="utf-8") == "{ // not json"
    assert "WARNING" in capsys.readouterr().out
    assert len(calls) == 1
