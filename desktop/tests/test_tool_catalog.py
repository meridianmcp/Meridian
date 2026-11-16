"""Tests for the tool catalog (meridian_desktop/tool_catalog.py) and the saved disabled_tools setting.

Pure Python, no Qt. Settings files live in pytest's temp dir, never the real one (conftest).
"""

from __future__ import annotations

import json

from meridian_desktop import tool_catalog
from meridian_desktop.settings_store import Settings, SettingsStore

EXPECTED_GROUPS = ("code", "outputs", "docs", "latex", "debug")


def test_the_five_groups_are_listed_in_order_and_chat_files_is_not_one():
    assert tool_catalog.group_ids() == EXPECTED_GROUPS
    assert "chat-files" not in tool_catalog.group_ids()
    assert "fs" not in tool_catalog.group_ids()


def test_every_group_has_servers_and_every_server_has_tools():
    for group in tool_catalog.GROUPS:
        assert group.servers, group.id
        for server in group.servers:
            assert server.tools, server.id
            assert len(set(server.tools)) == len(server.tools), f"duplicate tool in {server.id}"


def test_code_group_holds_codebase_memory_and_serena():
    code = next(group for group in tool_catalog.GROUPS if group.id == "code")
    assert [server.id for server in code.servers] == ["codebase", "serena"]


def test_server_ids_are_unique_and_map_to_their_group():
    ids = [server.id for group in tool_catalog.GROUPS for server in group.servers]
    assert len(ids) == len(set(ids))
    assert tool_catalog.group_of_server("codebase") == "code"
    assert tool_catalog.group_of_server("serena") == "code"
    assert tool_catalog.group_of_server("debug") == "debug"


def test_tool_keys_are_unique_across_groups_even_for_a_shared_tool_name():
    # record_provenance is in both outputs and latex. The group in the key keeps the two apart.
    assert tool_catalog.tool_key("outputs", "record_provenance") != tool_catalog.tool_key("latex", "record_provenance")
    keys = tool_catalog.all_tool_keys()
    assert len(keys) == len(set(keys))
    assert all(key.count(tool_catalog.KEY_SEPARATOR) == 1 for key in keys)


def test_server_disabled_tools_returns_only_switched_off_tools_in_server_order():
    off = {tool_catalog.tool_key("code", "find_symbol"), tool_catalog.tool_key("code", "search_graph")}
    assert tool_catalog.server_disabled_tools("serena", off) == ("find_symbol",)
    assert tool_catalog.server_disabled_tools("codebase", off) == ("search_graph",)


def test_an_unknown_key_switches_nothing_off():
    assert tool_catalog.server_disabled_tools("serena", {"code:no_such_tool"}) == ()
    assert tool_catalog.server_disabled_tools("docs", {"outputs:list_provenance"}) == ()


def test_normalise_strips_dedupes_and_sorts():
    assert tool_catalog.normalise([" b:x ", "a:y", "b:x", ""]) == ("a:y", "b:x")


def test_group_keys_cover_the_whole_group():
    keys = tool_catalog.group_keys("debug")
    assert len(keys) == len(tool_catalog.server_tools("debug"))
    assert all(key.startswith("debug:") for key in keys)


def test_disabled_tools_is_on_by_default_and_absent_from_the_file(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(store.load().settings)
    assert store.load().settings.disabled_tools == ()
    assert "disabled_tools" not in json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))


def test_disabled_tools_round_trips_through_the_settings_file(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    keys = ("code:find_symbol", "debug:step_over")
    store.save(Settings(disabled_tools=keys))
    assert json.loads(path.read_text(encoding="utf-8"))["disabled_tools"] == list(keys)
    reloaded = SettingsStore(path).load()
    assert reloaded.settings.disabled_tools == keys
    assert reloaded.warning is None


def test_a_non_list_disabled_tools_is_ignored_with_a_warning(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"disabled_tools": "code:find_symbol"}), encoding="utf-8")
    result = SettingsStore(path).load()
    assert result.settings.disabled_tools == ()
    assert result.warning is not None and "disabled_tools" in result.warning


def test_a_list_with_a_blank_or_non_text_entry_is_ignored_with_a_warning(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"disabled_tools": ["code:find_symbol", 3]}), encoding="utf-8")
    result = SettingsStore(path).load()
    assert result.settings.disabled_tools == ()
    assert result.warning is not None


def test_duplicate_entries_in_the_file_are_read_once(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"disabled_tools": ["code:a", "code:a", " code:b "]}), encoding="utf-8")
    assert SettingsStore(path).load().settings.disabled_tools == ("code:a", "code:b")


def test_clearing_every_switch_off_removes_the_key_again(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.save(Settings(disabled_tools=("code:find_symbol",)))
    store.save(Settings())
    assert "disabled_tools" not in json.loads(path.read_text(encoding="utf-8"))
