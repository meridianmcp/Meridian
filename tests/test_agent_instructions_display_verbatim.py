"""Regression test for f318c7e3.

generate_handoff already returns paste-ready plain text in its `content` field
(code-fence markers stripped server-side, per 642b1818) — confirmed server-side
correct, not a bug. The gap was purely behavioral: neither the tool description
nor agent_instructions explicitly told a calling session to DISPLAY that text —
a session could narrate "handoff generated" without ever pasting it. This is
instructions-only, no server logic changed.
"""
from meridian.agent_defaults import (
    DEFAULT_AGENT_INSTRUCTIONS,
    AGENT_INSTRUCTIONS_STANDARD_VERSION,
    parse_standard_version,
)
from meridian.mcp_tools import _MCP_TOOLS_LIST


def test_before_ending_section_says_display_verbatim():
    text = DEFAULT_AGENT_INSTRUCTIONS
    lowered = text.lower()
    section_start = text.index("## Before ending")
    section = text[section_start:text.index("## Secrets hygiene")]
    assert "verbatim" in section.lower()
    assert "content" in section
    assert "checkpoint" in section
    assert "generate_handoff" in section


def test_narrating_without_pasting_explicitly_discouraged():
    text = DEFAULT_AGENT_INSTRUCTIONS
    section_start = text.index("## Before ending")
    section = text[section_start:text.index("## Secrets hygiene")]
    assert "do not just narrate" in section.lower()


def test_generate_handoff_tool_description_says_display_verbatim():
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    desc = by_name["generate_handoff"]["description"]
    assert "verbatim" in desc.lower()
    assert "content" in desc.lower()


def test_standard_version_bumped_to_9():
    assert AGENT_INSTRUCTIONS_STANDARD_VERSION >= 9
    expected_marker = f"meridian-executor-standard: v{AGENT_INSTRUCTIONS_STANDARD_VERSION}"
    assert expected_marker in DEFAULT_AGENT_INSTRUCTIONS


def test_version_marker_matches_constant():
    embedded = parse_standard_version(DEFAULT_AGENT_INSTRUCTIONS)
    assert embedded == AGENT_INSTRUCTIONS_STANDARD_VERSION, (
        f"Embedded marker v{embedded} != constant v{AGENT_INSTRUCTIONS_STANDARD_VERSION}; "
        "bump AGENT_INSTRUCTIONS_STANDARD_VERSION and update the marker together."
    )
