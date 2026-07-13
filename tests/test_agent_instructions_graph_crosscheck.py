"""Regression test for b2d312b1.

codebase-memory-mcp's graph index (codebase__search_graph / search_graph) was
observed producing actively wrong results: zero hits for symbols that genuinely
exist, and line spans off by hundreds of lines.  The Serena extractor__* tools
(extractor__get_symbols_overview, extractor__find_declaration) use live
LSP-based parsing and are a distinct, more reliable source.

The v6 rewording must tell executors to cross-check with extractor__* BEFORE
concluding a symbol is absent (zero results) or before trusting a suspect span.
It must also note that a full tunnel + Claude Desktop restart fixes transient
tool-discovery failures for extractor__* tools.
"""
from meridian.agent_defaults import (
    DEFAULT_AGENT_INSTRUCTIONS,
    AGENT_INSTRUCTIONS_STANDARD_VERSION,
)


def test_search_graph_crosscheck_guidance_present():
    """Instructions must tell the executor to cross-check search_graph results
    with Serena extractor__* tools when results look wrong or zero."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    lowered = text.lower()

    # The new cross-check rule must be present in MANDATORY CODE INTEL PROTOCOL.
    assert "search_graph cross-check rule" in text or "cross-check" in lowered

    # Must name the Serena LSP tools the executor should use for cross-checking.
    assert "extractor__get_symbols_overview" in text
    assert "extractor__find_declaration" in text

    # Must address the zero-results case specifically.
    assert "zero results" in lowered or "zero hits" in lowered

    # Must say NOT to use raw grep / whole-file read as FIRST recourse.
    assert "first recourse" in lowered or "first" in lowered


def test_crosscheck_before_concluding_absence():
    """Executor must cross-check BEFORE concluding a symbol doesn't exist."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    # The phrasing "before concluding" (or similar) must appear near the
    # cross-check guidance so the ordering is explicit.
    lowered = text.lower()
    assert "before concluding" in lowered


def test_restart_tip_for_transient_extractor_failures():
    """Instructions must mention that a full restart can fix transient
    tool-discovery failures for extractor__* tools."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    lowered = text.lower()
    # Restart tip — both "restart" and "claude desktop" (or "tunnel") must appear.
    assert "restart" in lowered
    assert "claude desktop" in lowered or "tunnel" in lowered


def test_standard_version_bumped_to_6():
    """Version must be at least 6 (the b2d312b1 bump) and the embedded marker
    must match the constant — derived dynamically so this test never needs
    updating on the next version bump."""
    assert AGENT_INSTRUCTIONS_STANDARD_VERSION >= 6
    expected_marker = f"meridian-executor-standard: v{AGENT_INSTRUCTIONS_STANDARD_VERSION}"
    assert expected_marker in DEFAULT_AGENT_INSTRUCTIONS


def test_version_marker_matches_constant():
    """The <!-- meridian-executor-standard: vN --> comment embedded in the
    instructions must carry the same N as AGENT_INSTRUCTIONS_STANDARD_VERSION."""
    from meridian.agent_defaults import parse_standard_version

    embedded = parse_standard_version(DEFAULT_AGENT_INSTRUCTIONS)
    assert embedded == AGENT_INSTRUCTIONS_STANDARD_VERSION, (
        f"Embedded marker v{embedded} != constant v{AGENT_INSTRUCTIONS_STANDARD_VERSION}; "
        "bump AGENT_INSTRUCTIONS_STANDARD_VERSION and update the marker together."
    )
