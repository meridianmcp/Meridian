"""Tests for the local cache (meridian_desktop/local_cache.py). Pure Python, no Qt.

Every config file is in pytest's temp dir, and every program lookup is a fake, so no real Claude Desktop or Codex config
is read or written. The formats are the ones the module documents: Claude Desktop JSON mcpServers, and Codex TOML
[mcp_servers.<name>] tables.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from meridian_desktop import tool_catalog
from meridian_desktop.local_cache import (
    CLAUDE_DESKTOP,
    CODEX,
    MARK_BEGIN,
    MARK_END,
    LocalCacheError,
    apply_local_cache,
    default_claude_desktop_config,
    default_codex_config,
    plan_claude_desktop,
    plan_codex,
    plan_local_cache,
    sync_local_cache,
    write_plan,
)

ALL_FOUND = {
    "codebase-memory-mcp": "C:/tools/codebase-memory-mcp.exe",
    "uvx": "C:/tools/uvx.exe",
    "npx": "npx",
}
CLAUDE_NAMES = {"codebase-memory-mcp", "meridian-extract", "meridian-outputs", "meridian-docs", "meridian-latex", "mcp-debugger"}
CODEX_NAMES = {"codebase-memory-mcp", "serena-local", "meridian-outputs", "meridian-docs", "meridian-latex", "mcp-debugger"}


def finder(found: dict[str, str]):
    """A stand-in for find_command: returns the mapped path, or None, and never looks at the real PATH."""

    def resolve(name, fallbacks=()):
        return found.get(name)

    return resolve


@pytest.fixture
def core(tmp_path):
    """A Meridian core checkout with the two extension folders the outputs and docs servers run from."""
    root = tmp_path / "core"
    (root / "extensions" / "meridian-outputs").mkdir(parents=True)
    (root / "extensions" / "meridian-docs").mkdir(parents=True)
    return root


@pytest.fixture
def paths(tmp_path):
    claude = tmp_path / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    codex = tmp_path / "home" / ".codex" / "config.toml"
    return claude, codex


def _plan(paths, core, *, disabled=(), found=ALL_FOUND, home=None, require_folder=False):
    """The dry run with the test's own temp paths. Folders are not required here, so the file is created in any case."""
    claude, codex = paths
    return plan_local_cache(
        disabled=disabled,
        claude_path=claude,
        codex_path=codex,
        core_checkout=str(core),
        resolve=finder(found),
        home=home if home is not None else Path("C:/no-such-home"),
        require_folder=require_folder,
    )


def _claude(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _codex(path: Path) -> dict:
    # utf-8-sig drops a leading byte order mark, which tomllib does not accept. The module keeps the mark in the file.
    return tomllib.loads(path.read_text(encoding="utf-8-sig"))


# ------------------------------------------------------------------ the dry run

def test_dry_run_writes_nothing(paths, core):
    claude, codex = paths
    plan = _plan(paths, core)
    assert not claude.exists() and not codex.exists()
    assert all(file.will_write for file in plan.files)


def test_full_build_names_the_six_servers_in_both_clients(paths, core):
    plan = _plan(paths, core)
    apply_local_cache(plan)
    claude, codex = paths
    assert set(_claude(claude)["mcpServers"]) == CLAUDE_NAMES
    assert set(_codex(codex)["mcp_servers"]) == CODEX_NAMES


def test_serena_uses_each_clients_own_name_and_context(paths, core):
    apply_local_cache(_plan(paths, core))
    claude, codex = paths
    desktop = _claude(claude)["mcpServers"]["meridian-extract"]["args"]
    cli = _codex(codex)["mcp_servers"]["serena-local"]["args"]
    assert desktop[desktop.index("--context") + 1] == "claude-code"
    assert cli[cli.index("--context") + 1] == "codex"
    assert "serena-agent==1.5.3" in desktop and "start-mcp-server" in desktop


def test_outputs_and_docs_run_from_the_core_checkout(paths, core):
    apply_local_cache(_plan(paths, core))
    claude, _ = paths
    servers = _claude(claude)["mcpServers"]
    assert servers["meridian-outputs"]["args"][1] == str(core / "extensions" / "meridian-outputs")
    assert servers["meridian-docs"]["args"][-1] == "meridian-docs-mcp"


def test_debug_and_latex_use_the_npm_launchers_from_the_core_catalogue(paths, core):
    apply_local_cache(_plan(paths, core))
    claude, _ = paths
    servers = _claude(claude)["mcpServers"]
    assert servers["mcp-debugger"] == {"command": "npx", "args": ["-y", "@debugmcp/mcp-debugger@0.26.0"]}
    assert servers["meridian-latex"]["args"] == ["-y", "--package", "@meridianmcp/mcp@0.1.9", "meridian-latex", "mcp"]


def test_the_codex_file_is_one_managed_block_at_the_end(paths, core):
    apply_local_cache(_plan(paths, core))
    _, codex = paths
    text = codex.read_text(encoding="utf-8")
    assert text.count(MARK_BEGIN) == 1 and text.count(MARK_END) == 1
    assert text.rstrip().endswith(MARK_END)


# ------------------------------------------------------------------ backups and atomic writes

def test_an_existing_claude_file_is_backed_up_before_it_is_changed(paths, core):
    claude, _ = paths
    claude.parent.mkdir(parents=True)
    original = '{"preferences": {"theme": "dark"}, "mcpServers": {"other": {"command": "x"}}}\n'
    claude.write_text(original, encoding="utf-8")
    plan = _plan(paths, core)
    backups = apply_local_cache(plan)
    backup_files = list(claude.parent.glob("claude_desktop_config.json.meridian-desktop-backup-*"))
    assert len(backup_files) == 1
    assert backup_files[0].read_text(encoding="utf-8") == original
    assert any("backup at" in line for line in backups)


def test_writes_leave_no_temporary_files_behind(paths, core):
    apply_local_cache(_plan(paths, core))
    claude, codex = paths
    leftovers = [p for p in claude.parent.iterdir() if p.name.endswith(".tmp")] + [
        p for p in codex.parent.iterdir() if p.name.endswith(".tmp")
    ]
    assert leftovers == []


def test_a_second_run_changes_nothing_and_makes_no_backup(paths, core):
    apply_local_cache(_plan(paths, core))
    plan = _plan(paths, core)
    assert not any(file.will_write for file in plan.files)
    lines = apply_local_cache(plan)
    assert all("already up to date" in line for line in lines)
    claude, codex = paths
    assert not list(claude.parent.glob("*.meridian-desktop-backup-*"))
    assert not list(codex.parent.glob("*.meridian-desktop-backup-*"))


def test_a_file_that_changed_after_planning_is_not_overwritten(paths, core):
    claude, _ = paths
    plan = _plan(paths, core)
    claude.parent.mkdir(parents=True)
    claude.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    with pytest.raises(LocalCacheError, match="changed while the local cache was planned"):
        write_plan(next(f for f in plan.files if f.client == CLAUDE_DESKTOP))
    assert claude.read_text(encoding="utf-8") == '{"mcpServers": {}}\n'


# ------------------------------------------------------------------ Claude Desktop JSON

def test_claude_keeps_other_servers_and_top_level_keys(paths, core):
    claude, _ = paths
    claude.parent.mkdir(parents=True)
    claude.write_text(
        json.dumps({"preferences": {"theme": "dark"}, "mcpServers": {"mine": {"command": "node", "args": ["a.js"]}}}),
        encoding="utf-8",
    )
    apply_local_cache(_plan(paths, core))
    doc = _claude(claude)
    assert doc["preferences"] == {"theme": "dark"}
    assert doc["mcpServers"]["mine"] == {"command": "node", "args": ["a.js"]}
    assert CLAUDE_NAMES <= set(doc["mcpServers"])


def test_claude_refuses_an_invalid_json_file_and_leaves_it_alone(paths, core):
    claude, _ = paths
    claude.parent.mkdir(parents=True)
    claude.write_text("{ not json", encoding="utf-8")
    plan = _plan(paths, core)
    desktop = next(f for f in plan.files if f.client == CLAUDE_DESKTOP)
    assert desktop.error is not None and "not valid JSON" in desktop.error
    assert desktop.will_write is False
    lines = apply_local_cache(plan)
    assert claude.read_text(encoding="utf-8") == "{ not json"
    assert any("not written" in line and "Claude Desktop" in line for line in lines)


def test_claude_refuses_a_non_object_mcp_servers_value(paths, core):
    claude, _ = paths
    claude.parent.mkdir(parents=True)
    claude.write_text('{"mcpServers": []}', encoding="utf-8")
    desktop = next(f for f in _plan(paths, core).files if f.client == CLAUDE_DESKTOP)
    assert desktop.error is not None and "non-object mcpServers" in desktop.error


def test_claude_has_no_per_tool_filter_so_a_partly_off_server_is_written_whole_and_noted(paths, core):
    plan = _plan(paths, core, disabled=["code:find_symbol"])
    desktop = next(f for f in plan.files if f.client == CLAUDE_DESKTOP)
    assert any("cannot hide single tools" in note for note in desktop.notes)
    apply_local_cache(plan)
    claude, _ = paths
    assert "meridian-extract" in _claude(claude)["mcpServers"]


# ------------------------------------------------------------------ Codex TOML

def test_codex_switches_off_single_tools_with_disabled_tools(paths, core):
    apply_local_cache(_plan(paths, core, disabled=["code:find_symbol", "code:search_graph"]))
    _, codex = paths
    servers = _codex(codex)["mcp_servers"]
    assert servers["serena-local"]["disabled_tools"] == ["find_symbol"]
    assert servers["codebase-memory-mcp"]["disabled_tools"] == ["search_graph"]
    assert "disabled_tools" not in servers["meridian-docs"]


def test_codex_server_with_every_tool_off_is_not_written(paths, core):
    apply_local_cache(_plan(paths, core, disabled=tool_catalog.group_keys("debug")))
    claude, codex = paths
    assert "mcp-debugger" not in _codex(codex)["mcp_servers"]
    assert "mcp-debugger" not in _claude(claude)["mcpServers"]


def test_a_whole_group_off_removes_its_entries_on_the_next_run(paths, core):
    apply_local_cache(_plan(paths, core))
    apply_local_cache(_plan(paths, core, disabled=tool_catalog.group_keys("latex")))
    claude, codex = paths
    assert "meridian-latex" not in _claude(claude)["mcpServers"]
    assert "meridian-latex" not in _codex(codex)["mcp_servers"]
    assert "mcp-debugger" in _codex(codex)["mcp_servers"]


def test_codex_keeps_unowned_tables_and_top_level_keys_byte_for_byte(paths, core):
    _, codex = paths
    codex.parent.mkdir(parents=True)
    user_text = 'model = "gpt-5"\n\n[mcp_servers.custom]\ncommand = "my-tool"\nargs = ["--x"]\n'
    codex.write_text(user_text, encoding="utf-8")
    apply_local_cache(_plan(paths, core))
    text = codex.read_text(encoding="utf-8")
    assert text.startswith(user_text.rstrip("\n")), "the user's own text must come first, unchanged"
    parsed = _codex(codex)
    assert parsed["model"] == "gpt-5"
    assert parsed["mcp_servers"]["custom"] == {"command": "my-tool", "args": ["--x"]}


def test_codex_keeps_a_crlf_line_ending_on_every_line(paths, core):
    _, codex = paths
    codex.parent.mkdir(parents=True)
    codex.write_bytes(b'model = "gpt-5"\r\n\r\n[mcp_servers.custom]\r\ncommand = "my-tool"\r\n')
    apply_local_cache(_plan(paths, core))
    raw = codex.read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n"), "no bare LF may appear in a CRLF file"


def test_codex_keeps_a_utf8_bom(paths, core):
    _, codex = paths
    codex.parent.mkdir(parents=True)
    codex.write_bytes(b'\xef\xbb\xbfmodel = "gpt-5"\n')
    apply_local_cache(_plan(paths, core))
    assert codex.read_bytes().startswith(b"\xef\xbb\xbf")
    assert _codex(codex)["model"] == "gpt-5"


def test_codex_refuses_an_invalid_toml_file(paths, core):
    _, codex = paths
    codex.parent.mkdir(parents=True)
    codex.write_text('model = "unclosed\n', encoding="utf-8")
    plan = _plan(paths, core)
    file_plan = next(f for f in plan.files if f.client == CODEX)
    assert file_plan.error is not None and "not valid TOML" in file_plan.error
    assert codex.read_text(encoding="utf-8") == 'model = "unclosed\n'


def test_damaged_block_markers_are_refused(paths, core):
    _, codex = paths
    codex.parent.mkdir(parents=True)
    codex.write_text(f"{MARK_BEGIN}\n[mcp_servers.x]\ncommand = \"a\"\n", encoding="utf-8")
    file_plan = next(f for f in _plan(paths, core).files if f.client == CODEX)
    assert file_plan.error is not None and "markers" in file_plan.error


def test_a_table_the_core_wrote_outside_the_block_is_left_alone_and_noted(paths, core):
    _, codex = paths
    codex.parent.mkdir(parents=True)
    core_text = (
        "# >>> meridian managed mcp servers (generated by meridian.client_setup; do not edit) >>>\n"
        "[mcp_servers.codebase-memory-mcp]\n"
        'command = "C:/core/codebase-memory-mcp.exe"\n'
        'args = ["--ui=false"]\n'
        "# <<< meridian managed mcp servers <<<\n"
    )
    codex.write_text(core_text, encoding="utf-8")
    plan = _plan(paths, core)
    file_plan = next(f for f in plan.files if f.client == CODEX)
    assert any("codebase-memory-mcp is left as it is" in note for note in file_plan.notes)
    apply_local_cache(plan)
    parsed = _codex(codex)
    assert parsed["mcp_servers"]["codebase-memory-mcp"]["command"] == "C:/core/codebase-memory-mcp.exe"
    assert codex.read_text(encoding="utf-8").count("[mcp_servers.codebase-memory-mcp]") == 1


def test_codex_home_moves_the_codex_config(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom-codex"))
    assert default_codex_config() == tmp_path / "custom-codex" / "config.toml"


def test_claude_default_path_on_windows_is_appdata_claude(tmp_path):
    env = {"APPDATA": str(tmp_path / "Roaming")}
    assert default_claude_desktop_config(env, platform="win32") == tmp_path / "Roaming" / "Claude" / "claude_desktop_config.json"


# ------------------------------------------------------------------ missing prerequisites

def test_a_missing_core_checkout_skips_outputs_and_docs_with_a_note(paths):
    plan = _plan(paths, core=Path("C:/no-such-core"))
    notes = plan.notes
    assert any("meridian-outputs is not written" in note for note in notes)
    assert any("meridian-docs is not written" in note for note in notes)
    apply_local_cache(plan)
    claude, codex = paths
    assert "meridian-outputs" not in _claude(claude)["mcpServers"]
    assert "meridian-docs" not in _codex(codex)["mcp_servers"]


def test_missing_codebase_memory_and_uvx_are_skipped_with_notes(paths, core):
    plan = _plan(paths, core, found={"npx": "npx"})
    notes = " ".join(plan.notes)
    assert "codebase-memory-mcp was not found" in notes
    assert "uvx was not found" in notes
    apply_local_cache(plan)
    claude, _ = paths
    servers = _claude(claude)["mcpServers"]
    assert "codebase-memory-mcp" not in servers and "meridian-extract" not in servers
    assert "mcp-debugger" in servers


def test_missing_npx_skips_debug_and_latex(paths, core):
    plan = _plan(paths, core, found={"uvx": "C:/tools/uvx.exe", "codebase-memory-mcp": "C:/x/cbm.exe"})
    notes = " ".join(plan.notes)
    assert "mcp-debugger is not written: npx was not found" in notes
    assert "meridian-latex is not written: npx was not found" in notes


def test_a_server_with_all_tools_off_gets_a_note(paths, core):
    plan = _plan(paths, core, disabled=tool_catalog.group_keys("docs"))
    assert any("every one of its tools is switched off" in note for note in plan.notes)


# ------------------------------------------------------------------ sync and the default paths

def test_a_missing_app_folder_is_skipped_at_a_real_start_and_creates_nothing(paths, core):
    claude, codex = paths
    plan = plan_local_cache(
        claude_path=claude,
        codex_path=codex,
        core_checkout=str(core),
        resolve=finder(ALL_FOUND),
        home=Path("C:/no-such-home"),
    )
    assert all(file.skipped and not file.will_write for file in plan.files)
    lines = apply_local_cache(plan)
    assert not claude.parent.exists() and not codex.parent.exists()
    assert all("does not look set up on this computer" in line for line in lines)


def test_sync_local_cache_writes_and_returns_plain_lines(paths, core):
    claude, codex = paths
    lines = sync_local_cache(
        disabled=(),
        claude_path=claude,
        codex_path=codex,
        core_checkout=str(core),
        resolve=finder(ALL_FOUND),
        home=Path("C:/no-such-home"),
        require_folder=False,
    )
    assert claude.exists() and codex.exists()
    assert any("Claude Desktop: written" in line for line in lines)
    assert any("Codex: written" in line for line in lines)


def test_plan_plain_functions_have_no_side_effects_when_called_directly(paths, core):
    claude, codex = paths
    plan_claude_desktop(claude, core_checkout=str(core), resolve=finder(ALL_FOUND), home=Path("C:/no-such-home"))
    plan_codex(codex, core_checkout=str(core), resolve=finder(ALL_FOUND), home=Path("C:/no-such-home"))
    assert not claude.exists() and not codex.exists()
