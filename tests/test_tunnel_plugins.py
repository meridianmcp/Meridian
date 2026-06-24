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
        "filesystem", "code-intel", "code-extractor", "powerpoint", "word", "desktop-commander"
    ]
    assert [p["slot"] for p in plugins] == ["fs", "code", "extract", "ppt", "word", "dc"]
    # The three code/fs slots default ON with no command override; Office slots
    # and desktop-commander default OFF.
    by_name = {p["name"]: p for p in plugins}
    assert all(by_name[n]["enabled"] for n in ("filesystem", "code-intel", "code-extractor"))
    # filesystem + code-intel use the client's platform default (command None);
    # code-extractor defaults to Serena (LSP symbol tools), {repo_path} expanded
    # to the served repo at spawn time.
    assert all(by_name[n]["command"] is None for n in ("filesystem", "code-intel"))
    assert by_name["code-extractor"]["command"] == tp.SERENA_EXTRACT_COMMAND
    assert by_name["powerpoint"]["enabled"] is False
    assert by_name["word"]["enabled"] is False
    assert by_name["desktop-commander"]["enabled"] is False
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
# expand_command — {repo_path} template substitution (Serena extract default)
# ---------------------------------------------------------------------------

def test_expand_command_substitutes_repo_path_in_serena_default():
    out = tp.expand_command(tp.SERENA_EXTRACT_COMMAND, repo_path="/home/me/proj")
    assert out == ["uvx", "--from", "serena-agent", "serena", "start-mcp-server",
                   "--context", "ide-assistant", "--open-web-dashboard", "false",
                   "--project", "/home/me/proj"]
    # The module-level constant must not be mutated by expansion.
    assert tp.SERENA_EXTRACT_COMMAND[-1] == "{repo_path}"


def test_serena_extract_default_pins_serena_agent_distribution():
    # Regression (ddda781b): the bare ``serena`` PyPI project has no ``serena``
    # console script, so uvx must install the ``serena-agent`` distribution and
    # run the ``serena`` entrypoint it provides.
    cmd = tp.SERENA_EXTRACT_COMMAND
    assert cmd[:4] == ["uvx", "--from", "serena-agent", "serena"]
    assert "serena" not in cmd[1:3]  # not invoked as a bare ``uvx serena``


def test_serena_extract_default_suppresses_web_dashboard():
    # Regression (a39c4a99): the tunnel runs Serena headless, so it must not pop a
    # browser tab to the web dashboard on every (re)start. The documented
    # ``--open-web-dashboard false`` flag (value as a separate token) overrides the
    # user's serena_config.yml.
    cmd = tp.SERENA_EXTRACT_COMMAND
    i = cmd.index("--open-web-dashboard")
    assert cmd[i + 1] == "false"


def test_expand_command_accepts_string_and_leaves_unknown_placeholders():
    out = tp.expand_command("tool --project {repo_path} --flag {other}", repo_path="/r")
    assert out == ["tool", "--project", "/r", "--flag", "{other}"]


def test_expand_command_none_and_empty_yield_none():
    assert tp.expand_command(None, repo_path="/r") is None
    assert tp.expand_command("   ", repo_path="/r") is None
    assert tp.expand_command([], repo_path="/r") is None


def test_expand_command_missing_repo_path_blanks_placeholder():
    assert tp.expand_command(["x", "{repo_path}"]) == ["x", ""]


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
        "filesystem", "code-intel", "code-extractor", "powerpoint", "word", "desktop-commander"
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


# ---------------------------------------------------------------------------
# Office auto-enable via binary detection (6c2b3562)
# ---------------------------------------------------------------------------

def test_detect_office_binaries_uses_which():
    found = tp.detect_office_binaries(
        which=lambda b: "/usr/bin/" + b if b == "powerpoint-mcp" else None
    )
    assert found == {"ppt"}


def test_detect_office_binaries_none_found():
    assert tp.detect_office_binaries(which=lambda b: None) == set()


def test_resolve_auto_enables_detected_office_slots():
    by_slot = {p["slot"]: p for p in tp.resolve_plugins(None, detected_slots={"ppt"})}
    assert by_slot["ppt"]["enabled"] is True       # detected → on
    assert by_slot["word"]["enabled"] is False      # not detected → stays off


def test_resolve_detection_respects_explicit_disable():
    cfg = {"powerpoint": {"enabled": False}}
    by_slot = {p["slot"]: p for p in tp.resolve_plugins(cfg, detected_slots={"ppt"})}
    assert by_slot["ppt"]["enabled"] is False       # explicit user choice wins


def test_resolve_detection_keeps_explicit_enable_without_binary():
    cfg = {"word": {"enabled": True}}
    by_slot = {p["slot"]: p for p in tp.resolve_plugins(cfg, detected_slots=set())}
    assert by_slot["word"]["enabled"] is True


# ---------------------------------------------------------------------------
# resolve_custom_plugins — user-defined LOCAL-ONLY plugins (ce84619d)
# ---------------------------------------------------------------------------

def test_custom_plugin_valid_entry_resolves():
    cfg = [{"name": "fetch", "command": "uvx mcp-server-fetch", "port": 8901}]
    custom = tp.resolve_custom_plugins(cfg)
    assert custom == [{
        "name": "fetch",
        "command": ["uvx", "mcp-server-fetch"],
        "port": 8901,
        "enabled": True,
        "builtin": False,
        "custom": True,
    }]


def test_custom_plugin_command_list_form_and_explicit_disabled():
    cfg = [{"name": "git", "command": ["uvx", "mcp-server-git"], "port": 9100, "enabled": False}]
    custom = tp.resolve_custom_plugins(cfg)
    assert len(custom) == 1
    assert custom[0]["command"] == ["uvx", "mcp-server-git"]
    assert custom[0]["enabled"] is False


def test_custom_plugin_builtin_named_entry_excluded():
    # A config entry named like a built-in is a slot override, never a custom plugin.
    cfg = [{"name": "code-intel", "command": "codegraph", "port": 9001},
           {"name": "filesystem", "command": "x", "port": 9002}]
    assert tp.resolve_custom_plugins(cfg) == []


def test_custom_plugin_empty_command_dropped():
    cfg = [{"name": "nocmd", "command": "   ", "port": 9001},
           {"name": "missing", "port": 9002}]
    assert tp.resolve_custom_plugins(cfg) == []


def test_custom_plugin_bad_ports_dropped():
    cfg = [
        {"name": "boolport", "command": "x", "port": True},      # bool is not a port
        {"name": "lowport", "command": "x", "port": 80},          # < 1024
        {"name": "highport", "command": "x", "port": 70000},      # > 65535
        {"name": "strport", "command": "x", "port": "8901"},      # not an int
    ]
    assert tp.resolve_custom_plugins(cfg) == []


def test_custom_plugin_builtin_default_ports_collision_dropped():
    # 8808–8813 belong to built-in slots; a custom proxy may not reuse them.
    for p in (8808, 8809, 8810, 8811, 8812, 8813):
        cfg = [{"name": f"c{p}", "command": "x", "port": p}]
        assert tp.resolve_custom_plugins(cfg) == [], f"port {p} should collide"


def test_custom_plugin_duplicate_names_deduped_first_wins():
    cfg = [
        {"name": "dup", "command": "first", "port": 9001},
        {"name": "dup", "command": "second", "port": 9002},
    ]
    custom = tp.resolve_custom_plugins(cfg)
    assert len(custom) == 1
    assert custom[0]["command"] == ["first"] and custom[0]["port"] == 9001


def test_custom_plugin_repo_path_left_intact_at_resolve_time():
    # {repo_path} is expanded by the client at spawn time, not here.
    cfg = [{"name": "serena2", "command": "uvx serena --project {repo_path}", "port": 9300}]
    custom = tp.resolve_custom_plugins(cfg)
    assert custom[0]["command"] == ["uvx", "serena", "--project", "{repo_path}"]


def test_custom_plugin_dict_form_config():
    # The dict-keyed config shape resolves custom plugins too.
    cfg = {"fetch": {"command": "uvx mcp-server-fetch", "port": 8901}}
    custom = tp.resolve_custom_plugins(cfg)
    assert len(custom) == 1 and custom[0]["name"] == "fetch"


def test_custom_plugin_empty_and_garbage_config():
    assert tp.resolve_custom_plugins(None) == []
    assert tp.resolve_custom_plugins({}) == []
    assert tp.resolve_custom_plugins("nonsense") == []


def test_custom_plugin_command_and_port_round_trip_through_normalize():
    # normalize_plugins_config must preserve a custom entry's command + port so
    # the config round-trips (PUT → store → GET → resolve_custom_plugins).
    norm = tp.normalize_plugins_config(
        [{"name": "fetch", "command": "uvx mcp-server-fetch", "port": 8901}]
    )
    assert norm["fetch"]["command"] == ["uvx", "mcp-server-fetch"]
    assert norm["fetch"]["port"] == 8901
