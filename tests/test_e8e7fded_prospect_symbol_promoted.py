"""Regression test for e8e7fded.

Root cause confirmed via direct code read: prospect_symbol_impl
(meridian/prospect.py) already implements a robust three-rung fallback chain
(codebase__search_graph -> Serena extractor__find_symbol/find_declaration ->
search_code_semantic) -- and prospect_symbol's own MCP tool description
already says "Use this instead of calling codebase__search_graph directly".

But the injected MANDATORY CODE INTEL PROTOCOL text in agent_defaults.py never
named prospect_symbol at all: it told executors to call search_graph directly
and manually remember to cross-check with Serena on a miss (the b2d312b1
rule). That's exactly the "prose isn't enforcement" pattern -- a reliable,
already-built structural fallback existed, but the injected guidance routed
callers around it. The v12 rewording fixes this by naming prospect_symbol as
the PRIMARY tool for symbol/function/class lookups.
"""
from meridian.agent_defaults import (
    DEFAULT_AGENT_INSTRUCTIONS,
    AGENT_INSTRUCTIONS_STANDARD_VERSION,
    parse_standard_version,
)


def test_prospect_symbol_named_in_mandatory_protocol():
    """prospect_symbol must be named inside the MANDATORY CODE INTEL PROTOCOL
    section, not just somewhere else in the doc."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    protocol_start = text.index("MANDATORY CODE INTEL PROTOCOL")
    protocol_and_after = text[protocol_start:]
    assert "prospect_symbol" in protocol_and_after


def test_prospect_symbol_recommended_before_search_graph():
    """The protocol must tell executors to call prospect_symbol FIRST, not
    search_graph directly, for symbol lookups."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    lowered = text.lower()
    assert "prospect_symbol" in text
    assert "first" in lowered
    # The explicit "not search_graph directly" framing must be present
    # (whitespace-normalized since the source wraps the phrase across lines).
    normalized = " ".join(lowered.split())
    assert "not `search_graph` directly" in normalized


def test_prospect_symbol_is_described_as_finding_immediately():
    """prospect_symbol must be described in the MANDATORY CODE INTEL PROTOCOL
    section as finding a symbol in one call (as opposed to repeated grep attempts)."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    protocol_start = text.index("MANDATORY CODE INTEL PROTOCOL")
    protocol_and_after = text[protocol_start:]
    # prospect_symbol must be named and must be described as the immediate alternative.
    assert "prospect_symbol" in protocol_and_after
    # The section must convey that a single call replaces multiple grep attempts.
    lowered = protocol_and_after.lower()
    assert "immediately" in lowered or "single" in lowered or "one call" in lowered


def test_search_graph_crosscheck_rule_still_present_as_fallback():
    """The original b2d312b1 manual cross-check rule must remain, for the case
    where prospect_symbol itself is unavailable and a caller falls back to a
    direct search_graph call."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    assert "search_graph cross-check rule" in text
    assert "extractor__get_symbols_overview" in text
    assert "extractor__find_declaration" in text


def test_standard_version_bumped_to_12():
    assert AGENT_INSTRUCTIONS_STANDARD_VERSION >= 12
    expected_marker = f"meridian-executor-standard: v{AGENT_INSTRUCTIONS_STANDARD_VERSION}"
    assert expected_marker in DEFAULT_AGENT_INSTRUCTIONS


def test_version_marker_matches_constant():
    embedded = parse_standard_version(DEFAULT_AGENT_INSTRUCTIONS)
    assert embedded == AGENT_INSTRUCTIONS_STANDARD_VERSION, (
        f"Embedded marker v{embedded} != constant v{AGENT_INSTRUCTIONS_STANDARD_VERSION}; "
        "bump AGENT_INSTRUCTIONS_STANDARD_VERSION and update the marker together."
    )
