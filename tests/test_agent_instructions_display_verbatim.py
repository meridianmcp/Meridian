"""Regression tests for f318c7e3 and 5234877f.

5234877f (latest): generate_handoff content is now delivered pre-wrapped in a
single 4-backtick code fence so it renders as one copy-pasteable block regardless
of how any MCP client handles surrounding markdown.  The tool description is updated
to tell callers to output the field value verbatim — no extra headers/blockquotes.
This replaces the 642b1818 strip-fences approach, which could not prevent callers
from adding their own wrappers on top.

f318c7e3 (prior): instructions-only fix that told a calling session to DISPLAY the
content field verbatim.  Server logic changed further in 5234877f.
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


def test_generate_handoff_tool_description_says_pre_fenced(monkeypatch):
    """5234877f — the tool description must tell callers that content is delivered
    pre-wrapped in a single 4-backtick fence (not stripped), so they do not add
    extra headers/blockquotes around it."""
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    desc = by_name["generate_handoff"]["description"]
    # Must mention the fencing approach (5234877f replaced the strip approach).
    assert "4-backtick" in desc or "fence" in desc.lower(), (
        "tool description must mention the pre-fenced delivery (5234877f)"
    )
    # Must NOT tell callers to add extra wrappers.
    desc_lower = desc.lower()
    # The new description says "Do NOT add extra headers, blockquotes, or fences".
    assert "do not add" in desc_lower or "do not" in desc_lower, (
        "tool description must tell callers not to add extra wrappers"
    )
    # The old 642b1818 "strips code-fence markers" claim is now outdated.
    assert "strips code-fence markers" not in desc, (
        "tool description must not say 'strips code-fence markers' — 5234877f "
        "changed the approach to fence-wrapping"
    )


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
