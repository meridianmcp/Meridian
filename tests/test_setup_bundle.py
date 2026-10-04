from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from meridian import setup_bundle
from meridian.__main__ import main as meridian_main
from meridian.tray_main import main as tray_main
from meridian.tunnel_main import main as tunnel_main


def test_server_entries_are_repo_scoped_and_keep_credentials_out(tmp_path):
    entries = setup_bundle._server_entries(
        tmp_path,
        "claude-code",
        meridian_mode="hosted",
        meridian_url="https://usemeridian.us",
        windows=True,
    )

    assert len(entries) == 6
    assert all(name.startswith(setup_bundle.MANAGED_PREFIX) for name in entries)
    assert entries[setup_bundle._server_name("meridian", tmp_path)] == {
        "command": "cmd",
        "args": ["/c", "npx", "-y", "mcp-remote", "https://usemeridian.us/mcp"],
    }
    serena = entries[setup_bundle._server_name("serena", tmp_path)]
    assert serena["cwd"] == str(tmp_path.resolve())
    assert "--context" in serena["args"] and "claude-code" in serena["args"]
    assert "BEARER_TOKEN" not in json.dumps(entries)
    assert "secret" not in json.dumps(entries).lower()


def test_project_json_merge_is_idempotent_and_uninstall_preserves_other_servers(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    config = repo / ".mcp.json"
    config.write_text(json.dumps({"mcpServers": {"personal": {"command": "keep"}}}), encoding="utf-8")

    first = setup_bundle.configure_bundle([repo], ["claude-code"], windows=False)
    assert any(line.startswith("Updated ") for line in first)
    merged = json.loads(config.read_text(encoding="utf-8"))
    assert merged["mcpServers"]["personal"] == {"command": "keep"}
    assert len(merged["mcpServers"]) == 7

    second = setup_bundle.configure_bundle([repo], ["claude-code"], windows=False)
    assert second == ["No config changes needed; the bundle is already in the requested state."]

    setup_bundle.configure_bundle([repo], ["claude-code"], uninstall=True, windows=False)
    assert json.loads(config.read_text(encoding="utf-8")) == {"mcpServers": {"personal": {"command": "keep"}}}


def test_conflicting_server_is_left_untouched(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    config = repo / ".mcp.json"
    managed_name = setup_bundle._server_name("meridian", repo.resolve())
    original = {"mcpServers": {managed_name: {"command": "human-owned"}}}
    config.write_text(json.dumps(original), encoding="utf-8")

    with pytest.raises(setup_bundle.SetupBundleError, match="already has different server config"):
        setup_bundle.configure_bundle([repo], ["claude-code"], windows=False)

    assert json.loads(config.read_text(encoding="utf-8")) == original


def test_dry_run_does_not_create_host_config_directories(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()

    result = setup_bundle.configure_bundle([repo], ["cursor"], dry_run=True, windows=False)

    assert result[0].startswith("Would update ")
    assert not (repo / ".cursor").exists()


def test_codex_toml_merge_preserves_existing_config_and_uninstalls_own_tables(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    config = repo / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('# user comment\n[mcp_servers.custom]\ncommand = "custom"\n', encoding="utf-8")

    first = setup_bundle.configure_bundle([repo], ["codex"], windows=False)
    assert any(line.startswith("Updated ") for line in first)
    merged = tomllib.loads(config.read_text(encoding="utf-8"))
    assert merged["mcp_servers"]["custom"] == {"command": "custom"}
    assert len(merged["mcp_servers"]) == 7
    assert merged["mcp_servers"][setup_bundle._server_name("meridian", repo.resolve())] == {
        "url": "https://usemeridian.us/mcp",
        "bearer_token_env_var": "BEARER_TOKEN",
    }
    assert setup_bundle.configure_bundle([repo], ["codex"], windows=False)[0].startswith("No config changes")

    setup_bundle.configure_bundle([repo], ["codex"], uninstall=True, windows=False)
    remaining = tomllib.loads(config.read_text(encoding="utf-8"))
    assert remaining == {"mcp_servers": {"custom": {"command": "custom"}}}
    assert "# user comment" in config.read_text(encoding="utf-8")


def test_desktop_config_supports_multiple_roots_and_keeps_existing_entries(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    desktop = tmp_path / "claude_desktop_config.json"
    desktop.write_text(json.dumps({"mcpServers": {"personal": {"command": "keep"}}}), encoding="utf-8")

    setup_bundle.configure_bundle(
        [first, second], ["claude-desktop"], windows=False, desktop_path=desktop
    )
    servers = json.loads(desktop.read_text(encoding="utf-8"))["mcpServers"]
    assert servers["personal"] == {"command": "keep"}
    assert len(servers) == 13
    first_serena = servers[setup_bundle._server_name("serena", first.resolve())]
    second_serena = servers[setup_bundle._server_name("serena", second.resolve())]
    assert first_serena["cwd"] == str(first.resolve())
    assert second_serena["cwd"] == str(second.resolve())

    setup_bundle.configure_bundle(
        [first], ["claude-desktop"], uninstall=True, windows=False, desktop_path=desktop
    )
    remaining = json.loads(desktop.read_text(encoding="utf-8"))["mcpServers"]
    assert len(remaining) == 7
    assert "personal" in remaining
    assert setup_bundle._server_name("serena", second.resolve()) in remaining


def test_cli_accepts_multiple_hosts_and_meridian_entry_dispatches(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "project"
    repo.mkdir()
    assert setup_bundle.cli_main(
        ["--repo", str(repo), "--hosts", "claude-code", "cursor", "--dry-run"]
    ) == 0
    output = capsys.readouterr().out
    assert ".mcp.json" in output
    assert ".cursor" in output

    called = []
    monkeypatch.setattr(setup_bundle, "cli_main", lambda argv: called.append(argv) or 17)
    assert meridian_main(["setup", "--dry-run"]) == 17
    assert called == [["--dry-run"]]


def test_tray_binary_routes_setup_to_shared_cli(monkeypatch):
    called = []
    monkeypatch.setattr(setup_bundle, "cli_main", lambda argv: called.append(argv) or 23)

    assert tray_main(["setup", "--uninstall"]) == 23
    assert called == [["--uninstall"]]


def test_tunnel_binary_routes_setup_to_shared_cli(monkeypatch):
    called = []
    monkeypatch.setattr(setup_bundle, "cli_main", lambda argv: called.append(argv) or 29)

    assert tunnel_main(["setup", "--dry-run"]) == 29
    assert called == [["--dry-run"]]


def test_invalid_meridian_url_is_rejected_without_writing(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()

    with pytest.raises(setup_bundle.SetupBundleError, match="Meridian URL"):
        setup_bundle.configure_bundle(
            [repo], ["claude-code"], meridian_url="https://user:token@example.com/mcp", windows=False
        )
    assert not (repo / ".mcp.json").exists()


def test_both_installers_run_bundle_setup_for_the_invoking_repository():
    root = Path(__file__).resolve().parents[1]
    shell_installer = (root / "scripts" / "install.sh").read_text(encoding="utf-8")
    linux_launcher_installer = (root / "scripts" / "install_linux_launcher.sh").read_text(encoding="utf-8")
    windows_installer = (root / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    assert 'TARGET_REPO="$(pwd -P)"' in shell_installer
    assert "uv tool run --from meridian-server meridian setup --repo \"$TARGET_REPO\"" in shell_installer
    assert "pixi run python -m meridian setup --repo \"$TARGET_REPO\"" in shell_installer
    assert 'TARGET_REPO="$(pwd -P)"' in linux_launcher_installer
    assert '"$MERIDIAN_BIN" setup --repo "$TARGET_REPO"' in linux_launcher_installer
    assert "$TargetRepo = (Get-Location).Path" in windows_installer
    assert windows_installer.count("setup --repo $TargetRepo") >= 3
