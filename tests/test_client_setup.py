"""Tests for the Claude Desktop and Codex MCP setup (meridian.client_setup)."""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest

from meridian import client_setup as cs


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


def _run(mode, client, repo, tools, claude_path=None, codex_path=None, write=True):
    return cs.run_setup(
        mode,
        clients=(client,),
        write=write,
        claude_path=claude_path or Path("unused.json"),
        codex_path=codex_path or Path("unused.toml"),
        repo_root=repo,
        python_exe=tools["python_exe"],
        uvx=tools["uvx"],
        codebase=tools["codebase"],
    )


def test_serena_is_not_pinned_to_a_project_and_uses_client_context(repo, tools):
    for client, context in (("claude-desktop", "claude-code"), ("codex", "codex")):
        specs = cs.build_specs(client, repo, python_exe=tools["python_exe"],
                               uvx=tools["uvx"], codebase=tools["codebase"])
        serena = next(s for s in specs if s.name == cs.CLIENT_SERVER_NAMES[client]["serena"])
        assert "--project" not in serena.args
        assert "--project-from-cwd" not in serena.args
        assert serena.args[serena.args.index("--context") + 1] == context


def test_search_server_uses_this_checkout_and_pythonpath(repo, tools):
    specs = cs.build_specs("codex", repo, python_exe=tools["python_exe"],
                           uvx=tools["uvx"], codebase=tools["codebase"])
    search = next(s for s in specs if s.name == "meridian-search")
    assert search.command == tools["python_exe"]
    assert search.args == ["-m", "meridian.code_search"]
    assert search.env == {"PYTHONPATH": str(repo)}


def test_build_specs_rejects_missing_sources(tmp_path, tools):
    with pytest.raises(cs.SetupError):
        cs.build_specs("codex", tmp_path, python_exe="py", uvx=tools["uvx"], codebase=tools["codebase"])


def test_build_specs_rejects_unknown_client(repo, tools):
    with pytest.raises(cs.SetupError):
        cs.build_specs("cursor", repo, python_exe="py", uvx=tools["uvx"], codebase=tools["codebase"])


def test_claude_apply_keeps_unowned_entries_byte_for_byte(repo, tools, tmp_path):
    config = tmp_path / "claude_desktop_config.json"
    original = {
        "preferences": {"theme": "dark"},
        "mcpServers": {
            "dc": {"command": "node", "args": ["dc.js"]},
            "stripe": {"command": "npx", "env": {"STRIPE_KEY": "sk_test_keep_me"}},
            "codebase-memory-mcp": {"command": "stale.exe"},
        },
    }
    config.write_text(json.dumps(original, indent=2), encoding="utf-8")

    first = _run("apply", "claude-desktop", repo, tools, claude_path=config)[0]

    assert first["action"] == "written"
    assert first["backup"] and Path(first["backup"]).exists()
    after = json.loads(config.read_text(encoding="utf-8"))
    assert after["preferences"] == original["preferences"]
    assert after["mcpServers"]["dc"] == original["mcpServers"]["dc"]
    assert after["mcpServers"]["stripe"] == original["mcpServers"]["stripe"]
    assert after["mcpServers"]["codebase-memory-mcp"]["command"] == tools["codebase"]
    assert set(first["changes"]["added"]) == {"meridian-extract", "meridian-docs",
                                              "meridian-outputs", "meridian-search"}
    assert first["changes"]["changed"] == ["codebase-memory-mcp"]


def test_claude_apply_is_idempotent(repo, tools, tmp_path):
    config = tmp_path / "claude_desktop_config.json"
    _run("apply", "claude-desktop", repo, tools, claude_path=config)
    once = config.read_text(encoding="utf-8")

    second = _run("apply", "claude-desktop", repo, tools, claude_path=config)[0]

    assert second["action"] == "unchanged"
    assert second["backup"] is None
    assert config.read_text(encoding="utf-8") == once


def test_claude_remove_takes_out_only_owned_entries(repo, tools, tmp_path):
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(json.dumps({"mcpServers": {"dc": {"command": "node"}}}), encoding="utf-8")
    _run("apply", "claude-desktop", repo, tools, claude_path=config)

    removed = _run("remove", "claude-desktop", repo, tools, claude_path=config)[0]

    assert removed["action"] == "removed"
    servers = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]
    assert set(servers) == {"dc"}


def test_claude_refuses_invalid_json_without_touching_it(repo, tools, tmp_path):
    config = tmp_path / "claude_desktop_config.json"
    config.write_text("{not json", encoding="utf-8")

    with pytest.raises(cs.SetupError):
        _run("apply", "claude-desktop", repo, tools, claude_path=config)
    assert config.read_text(encoding="utf-8") == "{not json"


def test_plan_writes_nothing(repo, tools, tmp_path):
    config = tmp_path / "claude_desktop_config.json"
    codex = tmp_path / "config.toml"
    config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    codex.write_text('model = "gpt-5"\n', encoding="utf-8")

    claude = _run("apply", "claude-desktop", repo, tools, claude_path=config, write=False)[0]
    toml = _run("apply", "codex", repo, tools, codex_path=codex, write=False)[0]

    assert claude["action"] == "would_write" and toml["action"] == "would_write"
    assert json.loads(config.read_text(encoding="utf-8")) == {"mcpServers": {}}
    assert codex.read_text(encoding="utf-8") == 'model = "gpt-5"\n'


CODEX_EXISTING = """\
model = "gpt-5"

[mcp_servers.Neon]
command = "npx"
args = ["-y", "neon-mcp"]

[mcp_servers.Neon.http_headers]
Authorization = "Bearer keep-me"

[mcp_servers.codebase-memory-mcp]
command = "C:/old/codebase.exe"

[mcp_servers.codebase-memory-mcp.tools.search_graph]
approval = "always"

[mcp_servers.serena-local]
command = "C:/old/serena.exe"
args = ["start-mcp-server", "--project-from-cwd", "--context", "desktop-app"]
enabled = false
"""


def test_codex_apply_keeps_unowned_tables_and_owned_subtables(repo, tools, tmp_path):
    codex = tmp_path / "config.toml"
    codex.write_text(CODEX_EXISTING, encoding="utf-8")

    result = _run("apply", "codex", repo, tools, codex_path=codex)[0]

    assert result["action"] == "written"
    parsed = tomllib.loads(codex.read_text(encoding="utf-8"))
    assert parsed["model"] == "gpt-5"
    assert parsed["mcp_servers"]["Neon"]["http_headers"]["Authorization"] == "Bearer keep-me"
    assert parsed["mcp_servers"]["codebase-memory-mcp"]["tools"]["search_graph"]["approval"] == "always"
    assert parsed["mcp_servers"]["codebase-memory-mcp"]["command"] == tools["codebase"]
    serena = parsed["mcp_servers"]["serena-local"]
    assert serena["command"] == tools["uvx"]
    assert "--project-from-cwd" not in serena["args"]
    assert "--project" not in serena["args"]
    assert "enabled" not in serena


def test_codex_apply_is_idempotent(repo, tools, tmp_path):
    codex = tmp_path / "config.toml"
    codex.write_text(CODEX_EXISTING, encoding="utf-8")
    _run("apply", "codex", repo, tools, codex_path=codex)
    once = codex.read_text(encoding="utf-8")

    again = _run("apply", "codex", repo, tools, codex_path=codex)[0]

    assert again["action"] == "unchanged"
    assert codex.read_text(encoding="utf-8") == once


def test_codex_remove_restores_unowned_content(repo, tools, tmp_path):
    codex = tmp_path / "config.toml"
    codex.write_text(CODEX_EXISTING, encoding="utf-8")
    _run("apply", "codex", repo, tools, codex_path=codex)

    removed = _run("remove", "codex", repo, tools, codex_path=codex)[0]

    assert removed["action"] == "removed"
    parsed = tomllib.loads(codex.read_text(encoding="utf-8"))
    assert parsed["model"] == "gpt-5"
    assert parsed["mcp_servers"]["Neon"]["command"] == "npx"
    assert "meridian managed" not in codex.read_text(encoding="utf-8")
    assert "serena-local" not in parsed["mcp_servers"]
    assert "meridian-search" not in parsed["mcp_servers"]


def test_codex_creates_missing_config(repo, tools, tmp_path):
    codex = tmp_path / "fresh" / "config.toml"

    result = _run("apply", "codex", repo, tools, codex_path=codex)[0]

    assert result["action"] == "written"
    assert result["backup"] is None
    parsed = tomllib.loads(codex.read_text(encoding="utf-8"))
    assert set(parsed["mcp_servers"]) == {"codebase-memory-mcp", "serena-local",
                                          "meridian-docs", "meridian-outputs", "meridian-search"}


def test_codex_search_env_round_trips_windows_paths(repo, tools, tmp_path):
    codex = tmp_path / "config.toml"
    _run("apply", "codex", repo, tools, codex_path=codex)

    parsed = tomllib.loads(codex.read_text(encoding="utf-8"))

    assert parsed["mcp_servers"]["meridian-search"]["env"]["PYTHONPATH"] == str(repo)


def test_codex_refuses_invalid_toml(repo, tools, tmp_path):
    codex = tmp_path / "config.toml"
    codex.write_text("this is = = not toml [[\n", encoding="utf-8")

    with pytest.raises(cs.SetupError):
        _run("apply", "codex", repo, tools, codex_path=codex)
    assert codex.read_text(encoding="utf-8") == "this is = = not toml [[\n"


def test_codex_reports_when_a_disabled_owned_server_is_re_enabled(repo, tools, tmp_path):
    codex = tmp_path / "config.toml"
    codex.write_text(CODEX_EXISTING, encoding="utf-8")

    result = _run("apply", "codex", repo, tools, codex_path=codex)[0]

    assert result["re_enabled"] == ["serena-local"]


def test_codex_keeps_unmanaged_settings_of_owned_server(repo, tools, tmp_path):
    codex = tmp_path / "config.toml"
    codex.write_text(
        '[mcp_servers.codebase-memory-mcp]\ncommand = "x"\n'
        "startup_timeout_sec = 30\n"
        'cwd = "C:/somewhere"\n',
        encoding="utf-8",
    )

    _run("apply", "codex", repo, tools, codex_path=codex)

    server = tomllib.loads(codex.read_text(encoding="utf-8"))["mcp_servers"]["codebase-memory-mcp"]
    assert server["command"] == tools["codebase"]
    assert server["startup_timeout_sec"] == 30
    assert server["cwd"] == "C:/somewhere"


def test_validate_codex_flags_changed_unowned_server(tmp_path):
    old = '[mcp_servers.Neon]\ncommand = "npx"\n'
    new = '[mcp_servers.Neon]\ncommand = "other"\n'

    with pytest.raises(cs.SetupError):
        cs.validate_codex(old, new, owned={"meridian-search"})


def test_find_executable_uses_fallback_when_not_on_path(tmp_path, monkeypatch):
    fallback = tmp_path / "uvx.exe"
    fallback.write_text("", encoding="utf-8")
    monkeypatch.setattr(cs.shutil, "which", lambda name: None)

    assert cs.find_executable("uvx", (fallback,)) == str(fallback)
    with pytest.raises(cs.SetupError):
        cs.find_executable("uvx", (tmp_path / "missing.exe",))


def test_default_paths_follow_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert cs.default_codex_config({"CODEX_HOME": str(tmp_path)}) == tmp_path / "config.toml"
    assert cs.default_claude_desktop_config({"APPDATA": str(tmp_path)}) == (
        tmp_path / "Claude" / "claude_desktop_config.json"
    )


def test_cli_plan_is_a_dry_run(tmp_path, monkeypatch, capsys):
    config = tmp_path / "claude_desktop_config.json"
    codex = tmp_path / "config.toml"
    claude_code = tmp_path / ".claude.json"
    monkeypatch.setattr(cs, "find_executable", lambda name, fallbacks=(): f"/fake/{name}")

    code = cs.main(["plan", "--claude-config", str(config), "--codex-config", str(codex),
                    "--claude-code-config", str(claude_code)])

    assert code == 0
    assert not config.exists() and not codex.exists() and not claude_code.exists()
    out = capsys.readouterr()
    assert '"would_write"' in out.out
    assert "Nothing written" in out.err
