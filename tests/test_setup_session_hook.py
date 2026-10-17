"""Setup does not duplicate a server the repo already has, and the per-session hook installs, replaces, and removes only itself.

Every path is a temporary folder. The hook tests pass an explicit settings file, so the real ~/.claude is never touched.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from meridian import setup_bundle

ROOT = Path(setup_bundle.__file__).resolve().parents[1]
PIXI = "C:/tools/pixi/pixi.exe"


def _managed_tools(names) -> set[str]:
    return {setup_bundle._tool_of(name) for name in names if setup_bundle._tool_of(name)}


def test_a_repo_with_its_own_meridian_latex_and_outputs_servers_gets_no_duplicates(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    user = {
        "meridian": {"type": "http", "url": "https://usemeridian.us/mcp", "headers": {"Authorization": "Bearer test-token"}},
        "meridian-latex": {"command": "node", "args": ["C:/tools/meridian-latex-stable/engine/dist/mcp-server.js"]},
        "meridian-outputs-local": {"command": "uvx", "args": ["--from", "C:/src/meridian-outputs", "meridian-outputs-mcp"]},
    }
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": user}), encoding="utf-8")

    setup_bundle.configure_bundle([repo], ["claude-code"], windows=False)

    servers = json.loads((repo / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    for name, entry in user.items():
        assert servers[name] == entry, f"the user's own {name} entry must not change"
    managed = [name for name in servers if name.startswith(setup_bundle.MANAGED_PREFIX)]
    # Meridian, LaTeX and outputs are already present, so setup adds only the tools that are missing.
    assert _managed_tools(managed) == {"serena", "codebase_memory", "docs"}


def test_codex_skips_meridian_when_the_repo_already_has_a_meridian_table(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".codex").mkdir(parents=True)
    (repo / ".codex" / "config.toml").write_text(
        '[mcp_servers.meridian]\nurl = "https://usemeridian.us/mcp"\n', encoding="utf-8",
    )

    setup_bundle.configure_bundle([repo], ["codex"], windows=False)

    servers = tomllib.loads((repo / ".codex" / "config.toml").read_text(encoding="utf-8"))["mcp_servers"]
    assert servers["meridian"] == {"url": "https://usemeridian.us/mcp"}
    managed = [name for name in servers if name.startswith(setup_bundle.MANAGED_PREFIX)]
    assert "meridian" not in _managed_tools(managed)
    assert {"serena", "codebase_memory", "docs", "outputs", "latex"} <= _managed_tools(managed)


def test_a_fresh_repo_still_gets_every_managed_server(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    setup_bundle.configure_bundle([repo], ["claude-code"], windows=False)

    servers = json.loads((repo / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    managed = [name for name in servers if name.startswith(setup_bundle.MANAGED_PREFIX)]
    assert _managed_tools(managed) == set(setup_bundle.TOOLS)


def test_the_session_hook_runs_the_guarded_script_and_always_exits_zero():
    windows = setup_bundle.session_hook_command(ROOT, PIXI, windows=True)
    assert windows.startswith("powershell -NoProfile -ExecutionPolicy Bypass -File ")
    assert f'"{(ROOT / "scripts" / "session-setup.ps1").as_posix()}"' in windows
    # The Windows command is still recognized as this command's, so a reinstall replaces it and uninstall removes it.
    assert any(marker in windows for marker in setup_bundle.SESSION_HOOK_MARKERS)

    script = (ROOT / "scripts" / "session-setup.ps1").read_text(encoding="utf-8")
    assert "Test-Path -LiteralPath (Join-Path $repo '.git')" in script
    assert "python -m meridian setup --repo $repo" in script
    assert "Push-Location -LiteralPath $checkout" in script
    assert script.rstrip().endswith("exit 0")

    posix = setup_bundle.session_hook_command(ROOT, "/usr/bin/pixi", windows=False)
    assert posix.startswith('[ -d "$CLAUDE_PROJECT_DIR/.git" ]')
    assert posix.endswith("; true")
    assert setup_bundle.SESSION_HOOK_MARKER in posix
    assert f'(cd "{ROOT.as_posix()}" && ' in posix


def test_the_session_hook_keeps_other_hooks_and_is_replaced_and_removed_alone(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    original = {
        "model": "sonnet",
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}],
            "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "brief"}]}],
        },
    }
    settings.write_text(json.dumps(original), encoding="utf-8")

    assert setup_bundle.install_session_hook(settings, root=ROOT, pixi=PIXI, windows=True) == "written"
    installed = json.loads(settings.read_text(encoding="utf-8"))
    assert installed["model"] == "sonnet"
    assert installed["hooks"]["PreToolUse"] == original["hooks"]["PreToolUse"]
    assert installed["hooks"]["SessionStart"][0] == original["hooks"]["SessionStart"][0]

    assert setup_bundle.install_session_hook(settings, root=ROOT, pixi=PIXI, windows=True) == "unchanged"
    # Pointing the hook at another checkout replaces it rather than adding a second one.
    other = tmp_path / "other-checkout"
    other.mkdir()
    (other / "pixi.toml").write_text("", encoding="utf-8")
    assert setup_bundle.install_session_hook(settings, root=other, pixi=PIXI, windows=True) == "written"
    replaced = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    ours = [h for g in replaced for h in g["hooks"] if setup_bundle._is_session_hook(h)]
    assert len(ours) == 1
    assert other.as_posix() in ours[0]["command"]

    assert setup_bundle.uninstall_session_hook(settings) == "removed"
    assert json.loads(settings.read_text(encoding="utf-8")) == original
    assert (settings.parent / (settings.name + setup_bundle.SESSION_HOOK_BACKUP_SUFFIX)).is_file()


def test_the_session_hook_creates_a_missing_settings_file(tmp_path):
    settings = tmp_path / "new" / "settings.json"
    assert setup_bundle.install_session_hook(settings, root=ROOT, pixi=PIXI, windows=False) == "written"
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert [h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]] == [
        setup_bundle.session_hook_command(ROOT, PIXI, windows=False),
    ]


def test_a_malformed_hooks_value_is_refused_and_the_file_is_left_untouched(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": ["not", "a", "map"]}), encoding="utf-8")
    before = settings.read_bytes()
    with pytest.raises(setup_bundle.SetupBundleError):
        setup_bundle.install_session_hook(settings, root=ROOT, pixi=PIXI, windows=True)
    assert settings.read_bytes() == before


def test_a_cursor_server_named_or_hosted_for_a_tool_covers_that_tool(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".cursor").mkdir(parents=True)
    user = {
        "codebase-memory": {"url": "https://usemeridian.us/code/mcp/abc/mcp"},
        "serena": {"url": "https://usemeridian.us/extract/mcp/abc/mcp"},
    }
    (repo / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": user}), encoding="utf-8")

    setup_bundle.configure_bundle([repo], ["cursor"], windows=False)

    servers = json.loads((repo / ".cursor" / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    for name, entry in user.items():
        assert servers[name] == entry
    managed = [name for name in servers if name.startswith(setup_bundle.MANAGED_PREFIX)]
    # Serena and code memory are already there; the rest of the bundle is added.
    assert _managed_tools(managed) == {"meridian", "docs", "outputs", "latex"}


CODEX_CONFIG_BEFORE = """model = "gpt-5"

[hooks]
session_start = "echo hi"

[mcp_servers.meridian]
url = "https://usemeridian.us/mcp"
"""


def test_the_codex_hook_is_a_marked_block_that_leaves_the_rest_of_the_config_alone(tmp_path):
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(CODEX_CONFIG_BEFORE, encoding="utf-8")
    command = setup_bundle.session_hook_command(ROOT, PIXI, windows=True)

    assert setup_bundle.install_codex_session_hook(config, command=command) == "written"
    after = tomllib.loads(config.read_text(encoding="utf-8"))
    assert after["model"] == "gpt-5"
    assert after["hooks"]["session_start"] == "echo hi"
    assert after["mcp_servers"]["meridian"] == {"url": "https://usemeridian.us/mcp"}
    assert after["hooks"]["SessionStart"] == [
        {"matcher": setup_bundle.SESSION_HOOK_MATCHER, "hooks": [{"type": "command", "command": command}]},
    ]

    assert setup_bundle.install_codex_session_hook(config, command=command) == "unchanged"
    newer = setup_bundle.session_hook_command(Path("D:/other-checkout"), PIXI, windows=True)
    assert setup_bundle.install_codex_session_hook(config, command=newer) == "written"
    assert config.read_text(encoding="utf-8").count(setup_bundle.CODEX_HOOK_BEGIN) == 1

    assert setup_bundle.uninstall_codex_session_hook(config) == "removed"
    assert tomllib.loads(config.read_text(encoding="utf-8")) == tomllib.loads(CODEX_CONFIG_BEFORE)
    assert (config.parent / (config.name + setup_bundle.SESSION_HOOK_BACKUP_SUFFIX)).is_file()


def test_a_missing_codex_config_is_created_with_only_the_hook(tmp_path):
    config = tmp_path / "new" / "config.toml"
    command = setup_bundle.session_hook_command(ROOT, PIXI, windows=True)
    assert setup_bundle.install_codex_session_hook(config, command=command) == "written"
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == command


def test_the_hook_refuses_a_root_without_a_pixi_manifest(tmp_path):
    settings = tmp_path / "settings.json"
    with pytest.raises(setup_bundle.SetupBundleError):
        setup_bundle.install_session_hook(settings, root=tmp_path, pixi=PIXI, windows=True)
    assert not settings.exists()
