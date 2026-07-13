"""Regression test for 44db89b3.

MANDATORY CODE INTEL PROTOCOL named search_graph/get_function_tool/get_code_snippet
(and, after b2d312b1, Serena's extractor__* tools) but never named search_code_semantic
— the designated fallback for fuzzy/conceptual/multi-occurrence queries that don't fit
an exact-symbol lookup. Confirmed live: a real executor session used raw grep
exclusively rather than search_code_semantic because the fallback was never encoded
as a rule, only an unwritten intention.

This also caught a SEPARATE, more consequential finding: this project's STORED
agent_instructions (the per-project DB copy actually injected by start_session) was
stuck at v4, missing the v5/v6/v7 improvements already landed in the codebase default.
generate_handoff's agent_instructions_stale() check exists specifically to catch this
class of drift; the fix for that is a one-time resync via set_agent_instructions, not
a code change (advisory-only by design, since some projects customize their copy).
"""
from meridian.agent_defaults import (
    DEFAULT_AGENT_INSTRUCTIONS,
    AGENT_INSTRUCTIONS_STANDARD_VERSION,
    parse_standard_version,
)


def test_search_code_semantic_named_as_fuzzy_fallback():
    text = DEFAULT_AGENT_INSTRUCTIONS
    lowered = text.lower()
    assert "search_code_semantic" in text
    # Must appear inside/near the MANDATORY CODE INTEL PROTOCOL section, not just
    # in the tool list elsewhere.
    protocol_start = text.index("MANDATORY CODE INTEL PROTOCOL")
    protocol_and_after = text[protocol_start:]
    assert "search_code_semantic" in protocol_and_after
    # Must describe it as the fallback for fuzzy / conceptual / multi-occurrence
    # queries, distinct from the exact-symbol tools.
    assert "fuzzy" in lowered or "conceptual" in lowered or "multi-occurrence" in lowered


def test_exact_symbol_tools_distinguished_from_fallback():
    text = DEFAULT_AGENT_INSTRUCTIONS
    assert "exact-symbol" in text.lower()


def test_standard_version_bumped_to_8():
    assert AGENT_INSTRUCTIONS_STANDARD_VERSION >= 8
    expected_marker = f"meridian-executor-standard: v{AGENT_INSTRUCTIONS_STANDARD_VERSION}"
    assert expected_marker in DEFAULT_AGENT_INSTRUCTIONS


def test_version_marker_matches_constant():
    embedded = parse_standard_version(DEFAULT_AGENT_INSTRUCTIONS)
    assert embedded == AGENT_INSTRUCTIONS_STANDARD_VERSION, (
        f"Embedded marker v{embedded} != constant v{AGENT_INSTRUCTIONS_STANDARD_VERSION}; "
        "bump AGENT_INSTRUCTIONS_STANDARD_VERSION and update the marker together."
    )


def test_grep_bash_not_first_recourse_still_present():
    """44db89b3's premise (grep/bash unguarded) was already fixed by b2d312b1
    tonight — lock that in too so this class of regression can't silently
    reappear if the two sections are ever edited independently."""
    lowered = DEFAULT_AGENT_INSTRUCTIONS.lower()
    assert "grep" in lowered
    assert "first recourse" in lowered
