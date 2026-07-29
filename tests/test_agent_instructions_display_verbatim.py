"""Regression tests for f318c7e3, 5234877f, and a5e8aa74.

a5e8aa74 (latest): generate_handoff content is delivered EXACTLY as rendered —
no Markdown code fence, header, or blockquote wrapping of any kind. This
reverses 5234877f's four-backtick fence: that wrapping made verbatim
forwarding impossible, since a receiving session had to strip the fence
before the /goal block was paste-ready — defeating the point of a "display
verbatim" contract. The tool description now tells callers there is no
wrapping to work around, and to add none of their own either. One shared
helper (``meridian.handoff.format_handoff_mcp_content``) is called by every
transport (MCP handler, stdio handler, HTTP route) so the contract cannot
drift between them again.

5234877f (superseded): generate_handoff content used to be delivered
pre-wrapped in a single 4-backtick code fence so it rendered as one
copy-pasteable block regardless of how any MCP client handled surrounding
markdown. This replaced the 642b1818 strip-fences approach, which could not
prevent callers from adding their own wrappers on top.

f318c7e3 (prior): instructions-only fix that told a calling session to DISPLAY the
content field verbatim.  Server logic changed further in 5234877f, then a5e8aa74.
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


def test_generate_handoff_tool_description_says_raw_unwrapped(monkeypatch):
    """a5e8aa74 — the tool description must tell callers that content is delivered
    EXACTLY as rendered (no fence, no header, no blockquote — server-side or
    caller-side), reversing the 5234877f pre-fenced contract."""
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    desc = by_name["generate_handoff"]["description"]
    desc_lower = desc.lower()
    # Must state the field is raw / has no fence added.
    assert "no markdown code fence" in desc_lower or "no fence" in desc_lower or (
        "raw" in desc_lower and "fence" in desc_lower
    ), (
        "tool description must state content is delivered raw/unfenced (a5e8aa74)"
    )
    # Must still tell callers not to add their own wrappers on the calling side.
    assert "do not add" in desc_lower or "do not" in desc_lower, (
        "tool description must tell callers not to add extra wrappers"
    )
    # The old 642b1818 "strips code-fence markers" claim is still outdated.
    assert "strips code-fence markers" not in desc, (
        "tool description must not say 'strips code-fence markers'"
    )
    # Must not claim content arrives pre-wrapped in a fence anymore.
    assert "delivers content pre-wrapped" not in desc_lower, (
        "tool description must not claim content is pre-wrapped in a fence "
        "(a5e8aa74 removed the 5234877f fence wrapping)"
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
