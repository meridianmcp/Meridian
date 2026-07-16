"""Regression guard for sprint item 32369a8c.

Audited DEFAULT_AGENT_INSTRUCTIONS against the 'lean CLAUDE.md / progressive
disclosure' principle. The trim removed illustrative WRONG/RIGHT grep example
blocks and redundant multi-context walkthroughs from the Code intelligence and
MANDATORY CODE INTEL PROTOCOL sections, while keeping every actual rule
statement intact.

This test:
1. Asserts that essential rule keywords/phrases still appear in
   DEFAULT_AGENT_INSTRUCTIONS post-trim.
2. Asserts that the total character length is below a recorded baseline
   (16309 chars before the trim), as a lightweight regression guard against
   the file creeping back up.
"""
from meridian.agent_defaults import DEFAULT_AGENT_INSTRUCTIONS

# Baseline char count before the 32369a8c trim.
_BEFORE_TRIM_CHAR_COUNT = 16309

# Upper bound: must be meaningfully smaller than the pre-trim baseline.
# Allows ~100 chars of future minor edits before the guard would need updating.
_MAX_ALLOWED_CHARS = _BEFORE_TRIM_CHAR_COUNT - 400


def test_length_decreased_after_trim():
    """Total character count must be meaningfully smaller than the pre-trim baseline."""
    current = len(DEFAULT_AGENT_INSTRUCTIONS)
    assert current <= _MAX_ALLOWED_CHARS, (
        f"DEFAULT_AGENT_INSTRUCTIONS is {current} chars, but the post-trim guard "
        f"requires <= {_MAX_ALLOWED_CHARS} (baseline was {_BEFORE_TRIM_CHAR_COUNT}). "
        "If you intentionally expanded the instructions, update _MAX_ALLOWED_CHARS "
        "in this test and document what was added."
    )


def test_prospect_symbol_present():
    """prospect_symbol must appear — it is the primary tool rule for symbol lookups."""
    assert "prospect_symbol" in DEFAULT_AGENT_INSTRUCTIONS


def test_code_intel_guard_present():
    """code_intel_guard must appear — the PreToolUse hook that structurally enforces
    the code-intel rule (aeba8a80). This is a pinned decision that must stay inline."""
    assert "code_intel_guard" in DEFAULT_AGENT_INSTRUCTIONS


def test_aeba8a80_item_id_present():
    """The aeba8a80 hook item id must remain — it ties the structural enforcement
    note to the decision record."""
    assert "aeba8a80" in DEFAULT_AGENT_INSTRUCTIONS


def test_grep_never_first_step_rule_present():
    """The 'never as first step' rule must remain for grep/glob."""
    assert "never as first step" in DEFAULT_AGENT_INSTRUCTIONS.lower()


def test_stop_trigger_present():
    """The STOP trigger for reaching for grep must remain."""
    assert "STOP" in DEFAULT_AGENT_INSTRUCTIONS


def test_prospect_symbol_in_mandatory_protocol():
    """prospect_symbol must appear inside the MANDATORY CODE INTEL PROTOCOL section."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    protocol_start = text.index("MANDATORY CODE INTEL PROTOCOL")
    assert "prospect_symbol" in text[protocol_start:]


def test_search_graph_crosscheck_rule_present():
    """The search_graph cross-check rule must remain as a fallback guidance."""
    assert "search_graph cross-check rule" in DEFAULT_AGENT_INSTRUCTIONS


def test_extractor_tools_named():
    """extractor__get_symbols_overview and extractor__find_declaration must remain."""
    assert "extractor__get_symbols_overview" in DEFAULT_AGENT_INSTRUCTIONS
    assert "extractor__find_declaration" in DEFAULT_AGENT_INSTRUCTIONS


def test_search_code_semantic_fallback_present():
    """search_code_semantic must remain as the fuzzy/conceptual query fallback."""
    assert "search_code_semantic" in DEFAULT_AGENT_INSTRUCTIONS


def test_structural_enforcement_note_present():
    """The PreToolUse hook structural enforcement note (exit 2, block) must remain."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    assert "PreToolUse" in text or "exit 2" in text
    # Must convey that this is a hard block, not a soft warning.
    assert "not a soft warning" in text or "will be cancelled" in text or "exit 2" in text


def test_cross_check_before_concluding_absent():
    """The rule to cross-check with extractor__* before concluding a symbol absent
    must remain — it is a non-negotiable rule statement, not illustrative text."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    lowered = text.lower()
    assert "before concluding" in lowered
