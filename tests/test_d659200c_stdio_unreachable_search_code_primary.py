"""Regression test for d659200c.

Root cause: when a claude.ai browser session switches away from Claude Desktop
(which has the local --tunnel process running), the STDIO-based code-intel tools
(desktop-commander, meridian-code, meridian-extractor) all vanish from the tool
list because they are local processes unreachable from a browser. GitHub-backed
search_code is the only reachable code-search alternative.

The executor instructions (agent_defaults.py) previously framed GitHub search as
a "last resort fallback" and did not explain how to detect which context you are
in. This left a session in the browser-only context stuck: the grep/glob rule
says "don't grep", but the only alternative named was local tools that don't
exist in that context.

v14 fixes this by:
1. Adding a "how to tell which context you're in" checklist.
2. Naming search_code as PRIMARY (not fallback) in the browser/no-tunnel case.
3. Adding an explicit table mapping connection mode to available tools.
4. Documenting that local STDIO tools are genuinely absent (not deferred) in
   that context — so a discovery search failure means absence, not a loading lag.
"""

from meridian.agent_defaults import (
    DEFAULT_AGENT_INSTRUCTIONS,
    AGENT_INSTRUCTIONS_STANDARD_VERSION,
    parse_standard_version,
)


def test_standard_version_bumped_to_14():
    """AGENT_INSTRUCTIONS_STANDARD_VERSION must be at least 14 and the embedded
    marker must match."""
    assert AGENT_INSTRUCTIONS_STANDARD_VERSION >= 14
    expected_marker = f"meridian-executor-standard: v{AGENT_INSTRUCTIONS_STANDARD_VERSION}"
    assert expected_marker in DEFAULT_AGENT_INSTRUCTIONS


def test_version_marker_matches_constant():
    """The embedded <!-- meridian-executor-standard: vN --> marker must equal the
    Python constant — they must be bumped together."""
    embedded = parse_standard_version(DEFAULT_AGENT_INSTRUCTIONS)
    assert embedded == AGENT_INSTRUCTIONS_STANDARD_VERSION, (
        f"Embedded marker v{embedded} != constant v{AGENT_INSTRUCTIONS_STANDARD_VERSION}; "
        "bump AGENT_INSTRUCTIONS_STANDARD_VERSION and update the marker together."
    )


def test_search_code_named_as_primary_in_browser_no_tunnel_case():
    """The instructions must explicitly name search_code as the PRIMARY option
    (not just a fallback) when local STDIO tools are unreachable."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    # The phrase must appear with 'PRIMARY' framing near search_code
    normalized = " ".join(text.split())
    assert "search_code" in text
    # Both concepts must appear: search_code + PRIMARY within the browser/no-tunnel section
    assert "PRIMARY" in text
    # The browser/no-tunnel case must explicitly mention search_code as primary
    browser_start = text.lower().find("browser/no-tunnel")
    assert browser_start != -1, "Missing 'browser/no-tunnel' section in instructions"
    browser_section = text[browser_start : browser_start + 1000]
    assert "search_code" in browser_section, (
        "search_code must be named inside the browser/no-tunnel section"
    )
    assert "PRIMARY" in browser_section or "primary" in browser_section, (
        "search_code must be framed as PRIMARY in the browser/no-tunnel section"
    )


def test_stdio_tools_named_as_genuinely_unreachable():
    """The instructions must explain that desktop-commander/meridian-code/
    meridian-extractor are STDIO processes that require a running tunnel and
    are genuinely absent (not just deferred) in browser sessions."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    assert "STDIO" in text or "stdio" in text.lower()
    assert "desktop-commander" in text
    assert "meridian-code" in text
    assert "meridian-extractor" in text
    # Must convey the tunnel dependency
    assert "--tunnel" in text


def test_how_to_tell_context_section_present():
    """Instructions must include a runtime checklist for detecting which tool
    context the session is in."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    # The "how to tell" guidance must be present
    lowered = text.lower()
    assert "how to tell" in lowered, (
        "Instructions must include a 'how to tell' detection checklist"
    )
    # Must reference prospect_symbol or find_symbol as the probe
    assert "prospect_symbol" in text or "find_symbol" in text


def test_connection_mode_table_or_list_present():
    """Instructions must document which tools are reachable in which connection
    mode (a table or equivalent enumeration)."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    # Must document the browser-without-tunnel case explicitly
    assert "without" in text.lower() or "WITHOUT" in text
    # Must cover both tunnel and no-tunnel cases
    assert "tunnel" in text.lower()


def test_grep_glob_rule_still_applies_in_no_tunnel_context():
    """The 'grep/glob NEVER as first step' rule must still hold even in the
    browser/no-tunnel context — search_code is the substitute, not grep."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    browser_start = text.lower().find("browser/no-tunnel")
    assert browser_start != -1
    browser_section = text[browser_start : browser_start + 1200]
    # The section should say grep/glob still applies or redirect to search_code
    lowered_section = browser_section.lower()
    assert "grep" in lowered_section or "last resort" in lowered_section, (
        "The browser/no-tunnel section must still address the grep/glob constraint"
    )


def test_search_commits_named_as_supplemental_option():
    """search_commits should be named as a supplemental option for history
    queries in the browser/no-tunnel context."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    browser_start = text.lower().find("browser/no-tunnel")
    assert browser_start != -1
    browser_section = text[browser_start : browser_start + 1200]
    assert "search_commits" in browser_section, (
        "search_commits must be named in the browser/no-tunnel section for history queries"
    )


def test_code_intel_section_explains_exception_for_stdio():
    """The 'Code intelligence' section must distinguish deferred-but-available
    tools from genuinely-unreachable STDIO tools."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    ci_start = text.find("## Code intelligence")
    assert ci_start != -1, "Missing '## Code intelligence' section"
    # Use a larger window (3500 chars) to capture the full section including the
    # STDIO exception bullet which follows the detection table and how-to checklist.
    ci_section = text[ci_start : ci_start + 3500]
    assert "STDIO" in ci_section or "stdio" in ci_section.lower(), (
        "Code intelligence section must explain the STDIO tool distinction"
    )
    # Must distinguish genuinely absent vs deferred
    assert "genuinely absent" in ci_section or "genuinely" in ci_section
