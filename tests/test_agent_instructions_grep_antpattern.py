"""Regression test for 443aa32a.

A live Claude Code executor session (item 0ff8b982) used zero pre-indexed
code-intel calls — only raw grep/glob, including 4 consecutive failed grep
patterns before locating a dispatch branch that a single find_symbol call would
have found immediately.

The v10 update adds:
  1. An explicit "grep/glob NEVER as first step" rule inside MANDATORY CODE INTEL
     PROTOCOL, naming the exact anti-pattern (consecutive failing grep attempts).
  2. A concrete before/after example showing the wrong approach vs. the right one.
  3. A "STOP — use find_symbol" trigger that fires when the reader reaches for grep.
"""
from meridian.agent_defaults import (
    DEFAULT_AGENT_INSTRUCTIONS,
    AGENT_INSTRUCTIONS_STANDARD_VERSION,
    parse_standard_version,
)


def test_grep_never_as_first_step_rule_present():
    """The explicit grep/glob-never-first rule must appear in the instructions."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    lowered = text.lower()
    # The "grep/glob NEVER" heading or equivalent strong language must be present.
    assert "never as first step" in lowered or "never as first" in lowered, (
        "DEFAULT_AGENT_INSTRUCTIONS must contain explicit 'never as first step' "
        "language about grep/glob in the MANDATORY CODE INTEL PROTOCOL section."
    )


def test_grep_antpattern_appears_inside_code_intel_protocol():
    """The grep anti-pattern guidance must live inside MANDATORY CODE INTEL PROTOCOL,
    not somewhere else in the document."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    protocol_start = text.index("MANDATORY CODE INTEL PROTOCOL")
    protocol_and_after = text[protocol_start:]
    lowered = protocol_and_after.lower()
    assert "grep" in lowered, (
        "The grep anti-pattern guidance must appear inside / after the "
        "MANDATORY CODE INTEL PROTOCOL heading."
    )
    assert "never as first step" in lowered or "never as first" in lowered


def test_before_after_example_present():
    """The anti-pattern and correct alternative must both be described."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    # The anti-pattern (consecutive failing greps) must be described.
    assert "consecutive" in text.lower() or "anti-pattern" in text.lower(), (
        "DEFAULT_AGENT_INSTRUCTIONS must describe the consecutive-failing-grep "
        "anti-pattern."
    )
    # The correct alternative (prospect_symbol) must be named.
    assert "prospect_symbol" in text, (
        "DEFAULT_AGENT_INSTRUCTIONS must name prospect_symbol as the correct "
        "code-intel-first approach."
    )


def test_consecutive_failing_greps_described():
    """The example must describe the specific failure mode: multiple consecutive
    grep attempts before finding a symbol (the real incident)."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    lowered = text.lower()
    # The incident description or example must reference multiple/consecutive failures.
    assert (
        "consecutive" in lowered
        or "4 consecutive" in lowered
        or ("grep" in lowered and "results" in lowered)
    ), (
        "Instructions must describe the consecutive-failing-grep anti-pattern."
    )


def test_stop_use_find_symbol_trigger():
    """The instructions must include a clear trigger: if you reach for grep to find
    a symbol, STOP and use code-intel tools instead."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    # Look for the explicit STOP trigger.
    assert "STOP" in text, (
        "Instructions must include a 'STOP' trigger when the reader reaches for "
        "grep/glob to locate a symbol."
    )
    assert "find_symbol" in text, (
        "The STOP trigger must name find_symbol as the correct alternative."
    )


def test_grep_permitted_after_code_intel_confirms_path():
    """The updated rule must clarify when grep/glob IS still acceptable
    (after code-intel confirms a path, or for non-symbol content)."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    lowered = text.lower()
    # The instructions must say grep is OK after code-intel, not absolutely forbidden.
    assert "after" in lowered, (
        "Instructions must clarify grep/glob is acceptable after code-intel "
        "confirms a file path."
    )
    # Must mention non-symbol content as a valid grep use case.
    assert "non-symbol" in lowered or "config values" in lowered or "data files" in lowered


def test_standard_version_bumped_to_10():
    assert AGENT_INSTRUCTIONS_STANDARD_VERSION >= 10, (
        f"AGENT_INSTRUCTIONS_STANDARD_VERSION is {AGENT_INSTRUCTIONS_STANDARD_VERSION}; "
        "must be at least 10 for the 443aa32a grep-antpattern addition."
    )
    expected_marker = f"meridian-executor-standard: v{AGENT_INSTRUCTIONS_STANDARD_VERSION}"
    assert expected_marker in DEFAULT_AGENT_INSTRUCTIONS


def test_version_marker_matches_constant():
    """The embedded <!-- meridian-executor-standard: vN --> marker must match
    AGENT_INSTRUCTIONS_STANDARD_VERSION exactly."""
    embedded = parse_standard_version(DEFAULT_AGENT_INSTRUCTIONS)
    assert embedded == AGENT_INSTRUCTIONS_STANDARD_VERSION, (
        f"Embedded marker v{embedded} != constant v{AGENT_INSTRUCTIONS_STANDARD_VERSION}; "
        "bump AGENT_INSTRUCTIONS_STANDARD_VERSION and update the marker together."
    )
