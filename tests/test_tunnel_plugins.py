"""Tests for the tunnel plugin registry (meridian/tunnel_plugins.py).

The registry is the 3-slot model: three built-in transport slots (fs/code/extract)
whose command, enabled state, port, and descriptions can be overridden per-tenant.
These tests cover config normalization and the merge-over-defaults resolution.
"""
from __future__ import annotations

from meridian import tunnel_plugins as tp


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def test_resolve_defaults_returns_builtins_in_order():
    plugins = tp.resolve_plugins(None)
    assert [p["name"] for p in plugins] == [
        "filesystem", "code-intel", "code-extractor", "powerpoint", "word"
    ]
    assert [p["slot"] for p in plugins] == ["fs", "code", "extract", "ppt", "word"]
    # The three code/fs slots default ON with no command override; the two Office
    # slots default OFF with a built-in uvx command.
    by_name = {p["name"]: p for p in plugins}
    assert all(by_name[n]["enabled"] for n in ("filesystem", "code-intel", "code-extractor"))
    assert all(by_name[n]["command"] is None for n in ("filesystem", "code-intel", "code-extractor"))
    assert by_name["powerpoint"]["enabled"] is False
    assert by_name["word"]["enabled"] is False
    assert by_name["powerpoint"]["command"] == ["uvx", "powerpoint-mcp"]
    assert by_name["word"]["env"] == {"MCP_AUTHOR": "Adam", "MCP_AUTHOR_INITIALS": "AC"}


def test_resolve_empty_config_matches_defaults():
    assert tp.resolve_plugins({}) == tp.resolve_plugins(None)
    assert tp.resolve_plugins([]) == tp.resolve_plugins(None)


def test_active_plugins_filters_disabled():
    # Office slots are off by default, so only the three code/fs slots are active.
    assert [p["name"] for p in tp.active_plugins(None)] == [
        "filesystem", "code-intel", "code-extractor"
    ]
    # Disabling another slot drops it; enabling word adds it.
    cfg = {"code-extractor": {"enabled": False}, "word": {"enabled": True}}
    assert [p["name"] for p in tp.active_plugins(cfg)] == [
        "filesystem", "code-intel", "word"
    ]


def test_plugin_by_slot():
    assert tp.plugin_by_slot(None, "code")["name"] == "code-intel"
    assert tp.plugin_by_slot(None, "nope") is None


# ---------------------------------------------------------------------------
# Command override — the headline use case (code-intel → codegraph)
# ---------------------------------------------------------------------------

def test_override_code_intel_command_string():
    cfg = {"code-intel": {"command": "codegraph --stdio"}}
    code = tp.plugin_by_slot(cfg, "code")
    assert code["command"] == ["codegraph", "--stdio"]
    # Slot/url_prefix stay fixed — the swap reuses the /code transport.
    assert code["slot"] == "code"
    assert code["url_prefix"] == "/code"


def test_override_command_list_form():
    cfg = {"code-intel": {"command": ["codegraph", "--root", "."]}}
    assert tp.plugin_by_slot(cfg, "code")["command"] == ["codegraph", "--root", "."]


def test_override_port_and_description():
    cfg = {"code-intel": {"port": 9999, "description": "CodeGraph"}}
    code = tp.plugin_by_slot(cfg, "code")
    assert code["port"] == 9999
    assert code["description"] == "CodeGraph"


def test_builtin_slot_cannot_be_moved():
    # An attempt to move code-intel to the fs slot is ignored (slots are fixed).
    cfg = {"code-intel": {"slot": "fs"}}
    code = tp.plugin_by_slot(cfg, "code")
    assert code is not None and code["name"] == "code-intel"
    # fs slot still belongs to filesystem.
    assert tp.plugin_by_slot(cfg, "fs")["name"] == "filesystem"


# ---------------------------------------------------------------------------
# normalize_plugins_config — input shapes + hardening
# ---------------------------------------------------------------------------

def test_normalize_accepts_list_form():
    raw = [{"name": "code-intel", "enabled": False, "command": "x y"}]
    norm = tp.normalize_plugins_config(raw)
    assert norm == {"code-intel": {"enabled": False, "command": ["x", "y"]}}


def test_normalize_dict_form_with_none_value():
    assert tp.normalize_plugins_config({"code-intel": None}) == {"code-intel": {}}


def test_normalize_drops_empty_and_bad_command():
    norm = tp.normalize_plugins_config({"code-intel": {"command": "   "}})
    assert "command" not in norm["code-intel"]
    norm2 = tp.normalize_plugins_config({"code-intel": {"command": 123}})
    assert "command" not in norm2["code-intel"]


def test_normalize_bool_is_not_a_port():
    # bool is a subclass of int — must not be accepted as a port.
    norm = tp.normalize_plugins_config({"code-intel": {"port": True}})
    assert "port" not in norm["code-intel"]


def test_normalize_garbage_returns_empty():
    assert tp.normalize_plugins_config("nonsense") == {}
    assert tp.normalize_plugins_config(42) == {}
    assert tp.normalize_plugins_config(None) == {}


def test_description_overrides_normalized_to_strings():
    cfg = {"filesystem": {"description_overrides": {"read_file": "use sparingly"}}}
    fs = tp.plugin_by_slot(cfg, "fs")
    assert fs["description_overrides"] == {"read_file": "use sparingly"}


def test_builtin_names_helper():
    assert tp.builtin_names() == (
        "filesystem", "code-intel", "code-extractor", "powerpoint", "word"
    )


def test_office_plugins_enableable_and_overridable():
    cfg = {"powerpoint": {"enabled": True, "port": 9000},
           "word": {"enabled": True, "command": "uvx word-mcp-live --debug"}}
    ppt = tp.plugin_by_slot(cfg, "ppt")
    word = tp.plugin_by_slot(cfg, "word")
    assert ppt["enabled"] is True and ppt["port"] == 9000
    assert word["enabled"] is True and word["command"] == ["uvx", "word-mcp-live", "--debug"]
    # word keeps its default env unless overridden.
    assert word["env"] == {"MCP_AUTHOR": "Adam", "MCP_AUTHOR_INITIALS": "AC"}
