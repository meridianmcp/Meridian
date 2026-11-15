"""The local cache's plan for the owner's custom connectors (meridian_desktop/local_cache.py). Pure Python, no Qt.

Every config file is in pytest's temp dir and every program lookup is a fake, so no real Claude Desktop or Codex config is read
or written. The custom connectors come from custom_connectors.cache_entries, as the desktop passes them at a start.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from meridian_desktop import custom_connectors as cc
from meridian_desktop.local_cache import (
    CUSTOM_PREFIX,
    CustomCache,
    ServerSpec,
    apply_local_cache,
    plan_local_cache,
    sync_local_cache,
)

BUILTIN_FOUND = {
    "codebase-memory-mcp": "C:/tools/codebase-memory-mcp.exe",
    "uvx": "C:/tools/uvx.exe",
    "npx": "npx",
}
NOTES_CLI = "C:/tools/notes-cli.exe"
CUSTOM_NAME = CUSTOM_PREFIX + "notes"


def finder(found: dict[str, str]):
    def resolve(name, fallbacks=()):
        return found.get(name)

    return resolve


@pytest.fixture
def paths(tmp_path):
    claude = tmp_path / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    codex = tmp_path / "home" / ".codex" / "config.toml"
    return claude, codex


def _cache(*, args=("--serve",)) -> CustomCache:
    """The custom cache the desktop builds for one enabled stdio connector named notes."""
    raw = [{"name": "notes", "kind": "copy", "transport": "stdio", "command": "notes-cli", "args": list(args),
            "enabled": True, "tunnel_exposed": False}]
    return cc.cache_entries(raw, resolve=finder({"notes-cli": NOTES_CLI}))


def _plan(paths, custom: CustomCache, *, disabled=()):
    claude, codex = paths
    return plan_local_cache(
        disabled=disabled,
        claude_path=claude,
        codex_path=codex,
        core_checkout="",
        resolve=finder(BUILTIN_FOUND),
        home=Path("C:/no-such-home"),
        require_folder=False,
        custom=custom,
    )


def _claude(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _codex(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8-sig"))


def _sync(paths, custom: CustomCache) -> list[str]:
    claude, codex = paths
    return sync_local_cache(
        claude_path=claude,
        codex_path=codex,
        core_checkout="",
        resolve=finder(BUILTIN_FOUND),
        home=Path("C:/no-such-home"),
        require_folder=False,
        custom=custom,
    )


# ------------------------------------------------------------------ what gets written

def test_an_enabled_custom_stdio_connector_is_written_to_both_clients(paths):
    _sync(paths, _cache())
    claude, codex = paths
    assert _claude(claude)["mcpServers"][CUSTOM_NAME] == {"command": NOTES_CLI, "args": ["--serve"]}
    assert _codex(codex)["mcp_servers"][CUSTOM_NAME]["command"] == NOTES_CLI
    assert _codex(codex)["mcp_servers"][CUSTOM_NAME]["args"] == ["--serve"]


def test_the_custom_entry_is_written_inside_the_codex_managed_block(paths):
    _sync(paths, _cache())
    _, codex = paths
    text = codex.read_text(encoding="utf-8")
    block_start = text.index("# >>> meridian-desktop local cache")
    block_end = text.index("# <<< meridian-desktop local cache <<<")
    assert block_start < text.index(f"[mcp_servers.{CUSTOM_NAME}]") < block_end


def test_a_connector_whose_command_is_not_found_is_not_written_and_is_noted(paths):
    raw = [{"name": "notes", "kind": "copy", "transport": "stdio", "command": "notes-cli", "args": [],
            "enabled": True, "tunnel_exposed": False}]
    plan = _plan(paths, cc.cache_entries(raw, resolve=finder({})))
    assert all(CUSTOM_NAME not in file.text for file in plan.files)
    assert any(f"{CUSTOM_NAME} is not written" in note and "not found" in note for note in plan.notes)


def test_a_custom_entry_is_not_written_with_its_tunnel_flag_set_differently(paths):
    """The tunnel flag is for the bridge only. The local cache writes the same entry either way."""
    plain = _plan(paths, _cache())
    raw = [{"name": "notes", "kind": "copy", "transport": "stdio", "command": "notes-cli", "args": ["--serve"],
            "enabled": True, "tunnel_exposed": True}]
    exposed = _plan(paths, cc.cache_entries(raw, resolve=finder({"notes-cli": NOTES_CLI})))
    assert plain.files[0].text == exposed.files[0].text
    assert plain.files[1].text == exposed.files[1].text


# ------------------------------------------------------------------ disabled and removed entries

def test_a_disabled_connector_is_not_written(paths):
    raw = [{"name": "notes", "kind": "copy", "transport": "stdio", "command": "notes-cli", "args": [],
            "enabled": False, "tunnel_exposed": False}]
    _sync(paths, cc.cache_entries(raw, resolve=finder({"notes-cli": NOTES_CLI})))
    claude, codex = paths
    assert CUSTOM_NAME not in _claude(claude)["mcpServers"]
    assert CUSTOM_NAME not in _codex(codex)["mcp_servers"]


def test_a_removed_connector_is_taken_out_of_both_clients_on_the_next_run(paths):
    _sync(paths, _cache())
    claude, codex = paths
    assert CUSTOM_NAME in _claude(claude)["mcpServers"]
    _sync(paths, CustomCache())  # the owner removed it from the list
    assert CUSTOM_NAME not in _claude(claude)["mcpServers"]
    assert CUSTOM_NAME not in _codex(codex)["mcp_servers"]


def test_a_disabled_connector_is_removed_on_the_next_run(paths):
    _sync(paths, _cache())
    claude, codex = paths
    _sync(paths, _plan_disabled_cache())
    assert CUSTOM_NAME not in _claude(claude)["mcpServers"]
    assert CUSTOM_NAME not in _codex(codex)["mcp_servers"]


def _plan_disabled_cache() -> CustomCache:
    raw = [{"name": "notes", "kind": "copy", "transport": "stdio", "command": "notes-cli", "args": ["--serve"],
            "enabled": False, "tunnel_exposed": False}]
    return cc.cache_entries(raw, resolve=finder({"notes-cli": NOTES_CLI}))


# ------------------------------------------------------------------ the owner's own entries are left alone

def test_a_hand_written_claude_entry_is_kept_when_a_custom_connector_is_removed(paths):
    claude, _ = paths
    claude.parent.mkdir(parents=True)
    claude.write_text(json.dumps({"mcpServers": {"github": {"command": "gh-mcp", "args": []}}}), encoding="utf-8")
    _sync(paths, _cache())
    _sync(paths, CustomCache())
    servers = _claude(claude)["mcpServers"]
    assert servers["github"] == {"command": "gh-mcp", "args": []}
    assert CUSTOM_NAME not in servers


def test_a_hand_written_codex_table_outside_the_block_is_kept_when_a_custom_connector_is_removed(paths):
    _, codex = paths
    codex.parent.mkdir(parents=True)
    codex.write_text('[mcp_servers.my-own]\ncommand = "own-mcp"\nargs = []\n', encoding="utf-8")
    _sync(paths, _cache())
    _sync(paths, CustomCache())
    assert _codex(codex)["mcp_servers"]["my-own"] == {"command": "own-mcp", "args": []}


def test_a_custom_name_already_written_outside_the_block_is_left_as_it_is_and_noted(paths):
    _, codex = paths
    codex.parent.mkdir(parents=True)
    codex.write_text(f'[mcp_servers.{CUSTOM_NAME}]\ncommand = "old"\nargs = []\n', encoding="utf-8")
    plan = _plan(paths, _cache())
    assert any(f"{CUSTOM_NAME} is left as it is" in note for note in plan.notes)


# ------------------------------------------------------------------ the dry run, backups and repeat runs

def test_the_dry_run_writes_nothing_and_plans_the_custom_entry_as_added(paths):
    claude, codex = paths
    plan = _plan(paths, _cache())
    assert not claude.exists() and not codex.exists()
    assert all(file.will_write for file in plan.files)
    assert CUSTOM_NAME in plan.files[0].added


def test_a_change_to_a_custom_connector_is_backed_up_before_it_is_written(paths):
    _sync(paths, _cache())
    lines = _sync(paths, _cache(args=("--serve", "--verbose")))
    claude, _ = paths
    assert any("backup at" in line and "meridian-desktop-backup-" in line for line in lines)
    assert "--verbose" in _claude(claude)["mcpServers"][CUSTOM_NAME]["args"]
    assert list(claude.parent.glob("claude_desktop_config.json.meridian-desktop-backup-*"))


def test_a_second_run_with_no_change_writes_nothing(paths):
    _sync(paths, _cache())
    plan = _plan(paths, _cache())
    assert not any(file.will_write for file in plan.files)


def test_a_custom_note_is_shown_once_even_though_both_clients_would_note_it(paths):
    raw = [{"name": "remote", "kind": "copy", "transport": "http", "url": "https://example.com/mcp",
            "enabled": True, "tunnel_exposed": False}]
    plan = _plan(paths, cc.cache_entries(raw, resolve=finder({})))
    matching = [note for note in plan.notes if "meridian-custom-remote is not written" in note]
    assert len(matching) == 1


# ------------------------------------------------------------------ the spec the cache builds

def test_the_custom_spec_is_a_stdio_entry_with_the_resolved_command_and_its_args():
    cache = _cache()
    assert cache.specs == (
        ServerSpec(server="custom:notes", name=CUSTOM_NAME, command=NOTES_CLI, args=("--serve",)),
    )


def test_an_enabled_custom_entry_never_takes_a_built_in_server_name(paths):
    """A custom name is always prefixed, so it can never replace one of Meridian's own servers."""
    raw = [{"name": "codebase-memory-mcp", "kind": "copy", "transport": "stdio", "command": "notes-cli", "args": [],
            "enabled": True, "tunnel_exposed": False}]
    _sync(paths, cc.cache_entries(raw, resolve=finder({"notes-cli": NOTES_CLI, **BUILTIN_FOUND})))
    claude, _ = paths
    servers = _claude(claude)["mcpServers"]
    assert "codebase-memory-mcp" in servers
    assert servers["codebase-memory-mcp"]["command"] == BUILTIN_FOUND["codebase-memory-mcp"]
    assert "meridian-custom-codebase-memory-mcp" in servers
