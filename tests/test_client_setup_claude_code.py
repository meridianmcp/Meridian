"""Tests for the Claude Code user-scope MCP setup (``~/.claude.json``) in meridian.client_setup.

Every path is under ``tmp_path``. Nothing here reads or writes the real
``~/.claude.json``, and the CLI tests pass explicit ``--*-config`` paths for
every client so no real config is ever resolved.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from meridian import client_setup as cs

OWNED = {"codebase-memory-mcp", "meridian-extract", "meridian-docs", "meridian-outputs", "meridian-search"}
BACKUP_GLOB = ".claude.json.meridian-backup-*"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "Meridian"
    (root / "extensions" / "meridian-docs").mkdir(parents=True)
    (root / "extensions" / "meridian-outputs").mkdir(parents=True)
    (root / "meridian").mkdir()
    (root / "meridian" / "code_search.py").write_text("# stub\n", encoding="utf-8")
    return root


@pytest.fixture
def tools(tmp_path: Path) -> dict:
    uvx = tmp_path / "bin" / "uvx.exe"
    codebase = tmp_path / "bin" / "codebase-memory-mcp.exe"
    uvx.parent.mkdir(parents=True, exist_ok=True)
    uvx.write_text("", encoding="utf-8")
    codebase.write_text("", encoding="utf-8")
    return {"uvx": str(uvx), "codebase": str(codebase), "python_exe": sys.executable}


@pytest.fixture
def fake_tools(monkeypatch):
    monkeypatch.setattr(cs, "find_executable", lambda name, fallbacks=(): f"/fake/{name}")


def _run(mode, config, repo, tools, *, write=True):
    return cs.run_setup(
        mode,
        clients=("claude-code",),
        write=write,
        claude_path=config.parent / "unused-desktop.json",
        codex_path=config.parent / "unused.toml",
        repo_root=repo,
        python_exe=tools["python_exe"],
        uvx=tools["uvx"],
        codebase=tools["codebase"],
        claude_code_path=config,
    )[0]


def _backups(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.glob(BACKUP_GLOB))


def _existing_claude_file() -> dict:
    """Shaped like a real ~/.claude.json: Claude Code state next to a user-scope mcpServers table."""
    return {
        "numStartups": 42,
        "installMethod": "native",
        "userID": "user-id-keep",
        "oauthAccount": {"emailAddress": "someone@example.invalid", "accountUuid": "uuid-keep"},
        "tipsHistory": {"theme-tip": 3},
        "projects": {
            "C:/work/app": {
                "allowedTools": [],
                "history": ["keep this prompt"],
                "mcpServers": {
                    "project-only": {"type": "stdio", "command": "node", "args": ["p.js"], "env": {}},
                },
            },
        },
        "mcpServers": {
            "dc": {"type": "stdio", "command": "node", "args": ["dc.js"], "env": {}},
            "stripe": {"type": "http", "url": "https://mcp.example.invalid", "headers": {"X-Test": "keep"}},
            "codebase-memory-mcp": {"type": "stdio", "command": "stale.exe", "args": [], "env": {}},
        },
    }


def test_claude_code_is_a_client_and_owns_the_desktop_server_names():
    assert "claude-code" in cs.CLIENTS
    assert cs.owned_names("claude-code") == OWNED


def test_claude_code_specs_use_claude_code_serena_context_and_no_project_pin(repo, tools):
    specs = cs.build_specs("claude-code", repo, python_exe=tools["python_exe"],
                           uvx=tools["uvx"], codebase=tools["codebase"])

    assert {s.name for s in specs} == OWNED
    serena = next(s for s in specs if s.name == "meridian-extract")
    assert serena.args[serena.args.index("--context") + 1] == "claude-code"
    assert "--project" not in serena.args
    assert "--project-from-cwd" not in serena.args


def test_default_claude_code_config_is_dot_claude_json_in_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert cs.default_claude_code_config() == tmp_path / ".claude.json"


def test_new_file_gets_owned_entries_in_stdio_shape(repo, tools, tmp_path):
    config = tmp_path / "fresh" / ".claude.json"

    result = _run("apply", config, repo, tools)

    assert result["action"] == "written"
    assert result["backup"] is None
    assert set(result["changes"]["added"]) == OWNED
    assert _backups(config.parent) == []
    assert not (config.parent / ".claude.json.meridian-tmp").exists()
    document = json.loads(config.read_text(encoding="utf-8"))
    assert set(document) == {"mcpServers"}
    servers = document["mcpServers"]
    assert set(servers) == OWNED
    for entry in servers.values():
        assert list(entry) == ["type", "command", "args", "env"]
        assert entry["type"] == "stdio"
    assert servers["codebase-memory-mcp"] == {
        "type": "stdio", "command": tools["codebase"], "args": ["--ui=false"], "env": {},
    }
    assert servers["meridian-search"]["command"] == tools["python_exe"]
    assert servers["meridian-search"]["env"] == {"PYTHONPATH": str(repo)}
    serena_args = servers["meridian-extract"]["args"]
    assert serena_args[serena_args.index("--context") + 1] == "claude-code"


def test_other_keys_and_unowned_servers_survive_apply(repo, tools, tmp_path):
    config = tmp_path / ".claude.json"
    original = _existing_claude_file()
    config.write_text(json.dumps(original, indent=2), encoding="utf-8")

    result = _run("apply", config, repo, tools)

    assert result["action"] == "written"
    after = json.loads(config.read_text(encoding="utf-8"))
    assert set(after) == set(original)
    for key in original:
        if key != "mcpServers":
            assert after[key] == original[key], key
    assert after["mcpServers"]["dc"] == original["mcpServers"]["dc"]
    assert after["mcpServers"]["stripe"] == original["mcpServers"]["stripe"]
    assert after["mcpServers"]["codebase-memory-mcp"]["command"] == tools["codebase"]
    assert set(after["mcpServers"]) == OWNED | {"dc", "stripe"}
    assert result["changes"]["changed"] == ["codebase-memory-mcp"]
    assert set(result["changes"]["added"]) == OWNED - {"codebase-memory-mcp"}


def test_backup_holds_the_exact_pre_change_bytes(repo, tools, tmp_path):
    config = tmp_path / ".claude.json"
    original_bytes = json.dumps(_existing_claude_file(), indent=4).encode("utf-8")
    config.write_bytes(original_bytes)

    result = _run("apply", config, repo, tools)

    backup = Path(result["backup"])
    assert backup.parent == tmp_path
    assert backup.name.startswith(".claude.json.meridian-backup-")
    assert backup.read_bytes() == original_bytes
    assert config.read_bytes() != original_bytes


def test_reapply_is_idempotent_and_writes_no_second_backup(repo, tools, tmp_path):
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps(_existing_claude_file()), encoding="utf-8")
    _run("apply", config, repo, tools)
    once = config.read_bytes()
    backups_after_first = _backups(tmp_path)

    second = _run("apply", config, repo, tools)

    assert second["action"] == "unchanged"
    assert second["backup"] is None
    assert config.read_bytes() == once
    assert _backups(tmp_path) == backups_after_first
    assert len(backups_after_first) == 1


def test_remove_takes_out_only_owned_entries_and_keeps_the_rest(repo, tools, tmp_path):
    config = tmp_path / ".claude.json"
    original = _existing_claude_file()
    config.write_text(json.dumps(original), encoding="utf-8")
    _run("apply", config, repo, tools)

    removed = _run("remove", config, repo, tools)

    assert removed["action"] == "removed"
    assert removed["backup"] is not None
    after = json.loads(config.read_text(encoding="utf-8"))
    for key in original:
        if key != "mcpServers":
            assert after[key] == original[key], key
    assert set(after["mcpServers"]) == {"dc", "stripe"}
    assert after["mcpServers"]["dc"] == original["mcpServers"]["dc"]


def test_remove_on_a_missing_file_creates_nothing(repo, tools, tmp_path):
    config = tmp_path / ".claude.json"

    result = _run("remove", config, repo, tools)

    assert result["action"] == "unchanged"
    assert not config.exists()
    assert _backups(tmp_path) == []


def test_plan_writes_nothing_not_even_a_backup(repo, tools, tmp_path):
    config = tmp_path / ".claude.json"
    original_bytes = json.dumps(_existing_claude_file()).encode("utf-8")
    config.write_bytes(original_bytes)

    result = _run("apply", config, repo, tools, write=False)

    assert result["action"] == "would_write"
    assert result["backup"] is None
    assert config.read_bytes() == original_bytes
    assert _backups(tmp_path) == []


@pytest.mark.parametrize("mode", ["apply", "remove"])
@pytest.mark.parametrize("content", [
    "{not json",
    "[1, 2, 3]",
    '{"mcpServers": ["not", "an", "object"]}',
    '{"mcpServers": []}',
])
def test_unusable_file_is_refused_and_left_alone(repo, tools, tmp_path, mode, content):
    config = tmp_path / ".claude.json"
    config.write_text(content, encoding="utf-8")

    with pytest.raises(cs.SetupError):
        _run(mode, config, repo, tools)

    assert config.read_text(encoding="utf-8") == content
    assert _backups(tmp_path) == []


def test_write_refuses_when_claude_code_rewrote_the_file_after_planning(repo, tools, tmp_path):
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"numStartups": 1}), encoding="utf-8")
    specs = cs.build_specs("claude-code", repo, python_exe=tools["python_exe"],
                           uvx=tools["uvx"], codebase=tools["codebase"])
    plan = cs.plan_claude_code(config, specs, cs.owned_names("claude-code"))
    config.write_text(json.dumps({"numStartups": 2}), encoding="utf-8")

    with pytest.raises(cs.SetupError, match="changed while setup was running"):
        cs.write_claude_json(plan)

    assert json.loads(config.read_text(encoding="utf-8")) == {"numStartups": 2}
    assert _backups(tmp_path) == []


def test_claude_desktop_entries_do_not_gain_a_type_field(repo, tools, tmp_path):
    desktop = tmp_path / "claude_desktop_config.json"

    cs.run_setup(
        "apply",
        clients=("claude-desktop",),
        write=True,
        claude_path=desktop,
        codex_path=tmp_path / "unused.toml",
        repo_root=repo,
        python_exe=tools["python_exe"],
        uvx=tools["uvx"],
        codebase=tools["codebase"],
    )

    servers = json.loads(desktop.read_text(encoding="utf-8"))["mcpServers"]
    assert set(servers) == OWNED
    assert all("type" not in entry for entry in servers.values())


def test_run_setup_refuses_claude_code_without_a_path_before_writing_anything(repo, tools, tmp_path):
    codex = tmp_path / "config.toml"

    with pytest.raises(cs.SetupError):
        cs.run_setup(
            "apply",
            clients=("codex", "claude-code"),
            write=True,
            claude_path=tmp_path / "desktop.json",
            codex_path=codex,
            repo_root=repo,
            python_exe=tools["python_exe"],
            uvx=tools["uvx"],
            codebase=tools["codebase"],
        )

    assert not codex.exists()


def test_cli_all_includes_claude_code_and_plan_writes_nothing(tmp_path, fake_tools, capsys):
    desktop = tmp_path / "desktop.json"
    codex = tmp_path / "config.toml"
    claude_code = tmp_path / ".claude.json"

    code = cs.main(["plan", "--claude-config", str(desktop), "--codex-config", str(codex),
                    "--claude-code-config", str(claude_code)])

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["wrote"] is False
    assert [r["client"] for r in out["results"]] == ["claude-desktop", "codex", "claude-code"]
    entry = out["results"][2]
    assert entry["action"] == "would_write"
    assert set(entry["changes"]["added"]) == OWNED
    assert not any(p.exists() for p in (desktop, codex, claude_code))


def test_cli_apply_yes_writes_only_the_claude_code_file(tmp_path, fake_tools, capsys):
    desktop = tmp_path / "desktop.json"
    codex = tmp_path / "config.toml"
    claude_code = tmp_path / ".claude.json"
    claude_code.write_text(json.dumps(_existing_claude_file()), encoding="utf-8")

    code = cs.main(["apply", "--client", "claude-code", "--yes",
                    "--claude-config", str(desktop), "--codex-config", str(codex),
                    "--claude-code-config", str(claude_code)])

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["wrote"] is True
    assert [r["client"] for r in out["results"]] == ["claude-code"]
    assert out["results"][0]["action"] == "written"
    assert out["results"][0]["backup"]
    after = json.loads(claude_code.read_text(encoding="utf-8"))
    assert after["numStartups"] == 42
    assert set(after["mcpServers"]) == OWNED | {"dc", "stripe"}
    assert not desktop.exists() and not codex.exists()


def test_cli_apply_without_yes_writes_nothing(tmp_path, fake_tools, capsys):
    claude_code = tmp_path / ".claude.json"

    code = cs.main(["apply", "--client", "claude-code",
                    "--claude-config", str(tmp_path / "desktop.json"),
                    "--codex-config", str(tmp_path / "config.toml"),
                    "--claude-code-config", str(claude_code)])

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["results"][0]["action"] == "would_write"
    assert not claude_code.exists()


def test_cli_remove_yes_takes_out_only_owned_claude_code_entries(tmp_path, fake_tools, capsys):
    claude_code = tmp_path / ".claude.json"
    claude_code.write_text(json.dumps(_existing_claude_file()), encoding="utf-8")
    cs.main(["apply", "--client", "claude-code", "--yes", "--claude-code-config", str(claude_code),
             "--claude-config", str(tmp_path / "desktop.json"),
             "--codex-config", str(tmp_path / "config.toml")])
    capsys.readouterr()

    code = cs.main(["remove", "--client", "claude-code", "--yes", "--claude-code-config", str(claude_code),
                    "--claude-config", str(tmp_path / "desktop.json"),
                    "--codex-config", str(tmp_path / "config.toml")])

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["results"][0]["action"] == "removed"
    after = json.loads(claude_code.read_text(encoding="utf-8"))
    assert set(after["mcpServers"]) == {"dc", "stripe"}
    assert after["projects"] == _existing_claude_file()["projects"]
