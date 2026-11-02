"""prose_check MCP tool: schema, dispatch registration, error results, and the rule file."""

import asyncio
import inspect

import meridian.server  # noqa: F401 - imported before the handler to avoid an import cycle
from meridian import mcp_tools
from meridian.mcp import handler as mh
from meridian.mcp.handlers.prose_tools import handle_prose_check
from meridian.prose import RULE_FILE_NAME

ROUTING_TEXT = "We ship fast, reliable, and cheap."


def _schema() -> dict:
    (entry,) = [tool for tool in mcp_tools._MCP_TOOLS_LIST if tool["name"] == "prose_check"]
    return entry["inputSchema"]


def _call(args: dict, data_dir) -> dict:
    return asyncio.run(mh._handle_session_tools("prose_check", args, None, str(data_dir), None, None))


def test_schema_requires_text_and_lists_the_four_presets():
    schema = _schema()
    assert schema["required"] == ["text"]
    assert schema["properties"]["preset"]["enum"] == ["general", "academic", "overleaf", "docx"]
    assert schema["properties"]["include_custom_rules"]["type"] == "boolean"


def test_tool_is_registered_in_dispatch_and_is_read_only():
    assert inspect.iscoroutinefunction(handle_prose_check)
    assert "prose_check" in mcp_tools._READ_ONLY_TOOLS
    result = _call({"text": ROUTING_TEXT}, "")
    assert "error" not in result
    assert any(item["kind"] == "rule" for item in result["flags"])


def test_one_call_returns_flags_with_the_default_preset_applied(tmp_path):
    result = _call({"text": ROUTING_TEXT}, tmp_path)
    assert result["preset"] == "general"
    assert any(item["group"] == "triad_lists" for item in result["flags"])


def test_preset_argument_is_applied(tmp_path):
    result = _call({"text": ROUTING_TEXT, "preset": "academic"}, tmp_path)
    assert result["preset"] == "academic"
    assert not any(item["group"] == "triad_lists" for item in result["flags"])


def test_missing_or_blank_text_is_an_error_not_a_crash(tmp_path):
    assert "error" in _call({}, tmp_path)
    assert "error" in _call({"text": "   "}, tmp_path)


def test_unknown_preset_is_an_error_naming_the_choices(tmp_path):
    result = _call({"text": ROUTING_TEXT, "preset": "strict"}, tmp_path)
    assert "unknown preset" in result["error"]
    assert "general" in result["error"]


def test_oversize_text_is_an_error(tmp_path):
    result = _call({"text": "a" * 200_001}, tmp_path)
    assert "longer than" in result["error"]


def test_owner_rule_file_is_applied_on_every_call(tmp_path):
    (tmp_path / RULE_FILE_NAME).write_text(
        '[[rule]]\nid = "avoid-synergy"\nkind = "phrase"\npattern = "synergy"\n'
        'label = "Team wording"\n',
        encoding="utf-8",
    )
    result = _call({"text": "Some synergy here."}, tmp_path)
    assert result["custom_rules_applied"] == ["avoid-synergy"]
    assert any(item["source"] == "custom" for item in result["flags"])


def test_include_custom_rules_false_ignores_the_owner_file(tmp_path):
    (tmp_path / RULE_FILE_NAME).write_text(
        '[[rule]]\nid = "avoid-synergy"\nkind = "phrase"\npattern = "synergy"\n',
        encoding="utf-8",
    )
    result = _call({"text": "Some synergy here.", "include_custom_rules": False}, tmp_path)
    assert result["custom_rules_loaded"] == 0
    assert not any(item["source"] == "custom" for item in result["flags"])


def test_broken_rule_file_returns_an_error_that_names_the_file(tmp_path):
    (tmp_path / RULE_FILE_NAME).write_text("this is = = not toml", encoding="utf-8")
    result = _call({"text": ROUTING_TEXT}, tmp_path)
    assert result["rule_file"] == RULE_FILE_NAME
    assert "not valid TOML" in result["error"]


def test_rule_file_with_an_unknown_preset_returns_an_error(tmp_path):
    (tmp_path / RULE_FILE_NAME).write_text(
        '[[rule]]\nid = "x"\nkind = "phrase"\npattern = "y"\npresets = ["nope"]\n',
        encoding="utf-8",
    )
    result = _call({"text": ROUTING_TEXT}, tmp_path)
    assert "unknown preset" in result["error"]


def test_no_data_dir_means_no_owner_rules():
    result = asyncio.run(handle_prose_check({"text": ROUTING_TEXT}, None, "", None, None))
    assert result["custom_rules_loaded"] == 0
