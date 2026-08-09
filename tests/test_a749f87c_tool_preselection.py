"""Tests for a749f87c — deterministic, role/context-based tool pre-selection.

Two parts:
  1. Tool tag schema validity  — every tool in _MCP_TOOLS_LIST has a valid
     category and role_relevance value stamped onto it.
  2. _select_active_tool_set computation — covering:
     (a) executor role narrows the set vs. all-tools
     (b) no-role / empty case doesn't crash and returns all tools
     (c) keyword extraction from a real /goal-shaped string picks the right
         category (e.g. 'code search' → code-intel in the active set)
     (d) planner role returns planner-relevant tools and excludes
         executor-only tools
     (e) keyword expansion adds a category not in the default set
"""
from __future__ import annotations

import pytest

from meridian.mcp_tools import (
    _KEYWORD_CATEGORY_AFFINITY,
    _MCP_TOOLS_LIST,
    _TOOL_CATEGORY,
    _TOOL_ROLE_RELEVANCE,
    _select_active_tool_set,
    match_categories_by_keywords,
)


# ---------------------------------------------------------------------------
# Part 1 — Tool tag schema validity
# ---------------------------------------------------------------------------

VALID_ROLE_RELEVANCE = {"executor", "planner", "both"}

VALID_CATEGORIES = {
    "session", "sprint-management", "project", "notes", "decisions",
    "hitl", "workspace", "code-intel", "docx", "file-locking",
    "parallel-coord", "analysis", "plugin", "config", "research", "other",
}


def test_all_tools_have_category_stamped() -> None:
    """a749f87c(1a) — every tool in _MCP_TOOLS_LIST has 'category' stamped."""
    missing = [t["name"] for t in _MCP_TOOLS_LIST if "category" not in t]
    assert not missing, f"Tools missing 'category': {missing}"


def test_all_tools_have_role_relevance_stamped() -> None:
    """a749f87c(1b) — every tool in _MCP_TOOLS_LIST has 'role_relevance' stamped."""
    missing = [t["name"] for t in _MCP_TOOLS_LIST if "role_relevance" not in t]
    assert not missing, f"Tools missing 'role_relevance': {missing}"


def test_all_category_values_are_valid() -> None:
    """a749f87c(1c) — category values are from the declared vocabulary."""
    bad = [
        (t["name"], t["category"])
        for t in _MCP_TOOLS_LIST
        if t.get("category") not in VALID_CATEGORIES
    ]
    assert not bad, f"Tools with unknown category: {bad}"


def test_all_role_relevance_values_are_valid() -> None:
    """a749f87c(1d) — role_relevance values are executor|planner|both."""
    bad = [
        (t["name"], t["role_relevance"])
        for t in _MCP_TOOLS_LIST
        if t.get("role_relevance") not in VALID_ROLE_RELEVANCE
    ]
    assert not bad, f"Tools with unknown role_relevance: {bad}"


def test_no_tool_missing_from_category_dict() -> None:
    """a749f87c(1e) — every tool name is declared in _TOOL_CATEGORY (or falls back to 'other')."""
    # Tools not in _TOOL_CATEGORY fall back to "other" in the stamp loop.
    # That's allowed by design, but we at least confirm all tools have a
    # stamped category (covered by test_all_tools_have_category_stamped).
    # Here we additionally assert no tool has an empty-string category.
    bad = [t["name"] for t in _MCP_TOOLS_LIST if not t.get("category")]
    assert not bad, f"Tools with empty category: {bad}"


def test_core_tools_present_in_category_dict() -> None:
    """a749f87c(1f) — spot-check that key tools have the expected category."""
    spot = {
        "claim_sprint_item":  "sprint-management",
        "log_task":           "session",
        "generate_handoff":   "session",
        "claim_file":         "file-locking",
        "search_code_semantic": "code-intel",
        "prospect_symbol":    "code-intel",
        "paper_search":       "research",
        "get_planning_brief": "sprint-management",
        "pin_decision":       "decisions",
        "request_hitl":       "hitl",
    }
    for tool_name, expected_cat in spot.items():
        actual = _TOOL_CATEGORY.get(tool_name)
        assert actual == expected_cat, (
            f"{tool_name}: expected category={expected_cat!r}, got {actual!r}"
        )


def test_core_tools_role_relevance() -> None:
    """a749f87c(1g) — spot-check role_relevance for key tools."""
    spot = {
        "claim_sprint_item":  "executor",
        "get_planning_brief": "planner",
        "start_session":      "both",
        "log_task":           "executor",
        "paper_search":       "planner",
        "request_hitl":       "both",
    }
    for tool_name, expected_rel in spot.items():
        actual = _TOOL_ROLE_RELEVANCE.get(tool_name)
        assert actual == expected_rel, (
            f"{tool_name}: expected role_relevance={expected_rel!r}, got {actual!r}"
        )


# ---------------------------------------------------------------------------
# Part 2 — _select_active_tool_set computation
# ---------------------------------------------------------------------------

def test_no_role_returns_all_tools() -> None:
    """a749f87c(2a) — no role / empty role returns ALL tools (no exclusions)."""
    result = _select_active_tool_set(None)
    all_names = {t["name"] for t in _MCP_TOOLS_LIST}
    assert result["mode"] == "deterministic"
    assert set(result["active_tools"]) == all_names
    assert result["excluded_tools"] == []


def test_empty_string_role_returns_all_tools() -> None:
    """a749f87c(2b) — empty string role doesn't crash and returns all tools."""
    result = _select_active_tool_set("")
    all_names = {t["name"] for t in _MCP_TOOLS_LIST}
    assert set(result["active_tools"]) == all_names
    assert result["excluded_tools"] == []


def test_executor_role_narrows_set() -> None:
    """a749f87c(2c) — executor role excludes planner-only tools in default categories."""
    result = _select_active_tool_set("executor")
    assert result["mode"] == "deterministic"
    assert result["role"] == "executor"

    active = set(result["active_tools"])
    excluded = set(result["excluded_tools"])

    # Must be a strict subset of all tools
    all_names = {t["name"] for t in _MCP_TOOLS_LIST}
    assert active | excluded == all_names, "active + excluded must equal all tools"
    assert len(active) < len(all_names), "executor role must narrow the set"

    # Planner-only tools in CATEGORIES that are in executor's default set must
    # be excluded (e.g. set_goal/set_sprint are planner-only in "project" category
    # which IS in executor defaults, so they must be excluded).
    from meridian.mcp_tools import _EXECUTOR_DEFAULT_CATEGORIES
    planner_only_in_executor_defaults = {
        t["name"] for t in _MCP_TOOLS_LIST
        if _TOOL_ROLE_RELEVANCE.get(t["name"]) == "planner"
        and _TOOL_CATEGORY.get(t["name"], "other") in _EXECUTOR_DEFAULT_CATEGORIES
    }
    overlap = active & planner_only_in_executor_defaults
    assert not overlap, (
        f"Planner-only tools in executor-default categories appeared in "
        f"executor active set: {overlap}"
    )

    # Executor-only tools in executor-default categories MUST be active.
    # (Tools in non-default categories like docx/code-intel only appear after
    # keyword expansion — they're correctly absent from a default executor set.)
    from meridian.mcp_tools import _EXECUTOR_DEFAULT_CATEGORIES as _EXD
    executor_only_in_defaults = {
        t["name"] for t in _MCP_TOOLS_LIST
        if _TOOL_ROLE_RELEVANCE.get(t["name"]) == "executor"
        and _TOOL_CATEGORY.get(t["name"], "other") in _EXD
    }
    missing_exec = executor_only_in_defaults - active
    assert not missing_exec, (
        f"Executor-only tools in executor-default categories missing from active set: "
        f"{missing_exec}"
    )


def test_planner_role_excludes_executor_tools() -> None:
    """a749f87c(2d) — planner role excludes executor-only tools in default categories."""
    result = _select_active_tool_set("planner")
    assert result["role"] == "planner"

    active = set(result["active_tools"])

    # Executor-only tools in categories that ARE in planner defaults must be
    # excluded (e.g. log_task/checkpoint/generate_handoff are executor-only in
    # "session" which IS in planner defaults, so they must be excluded).
    from meridian.mcp_tools import _PLANNER_DEFAULT_CATEGORIES
    executor_only_in_planner_defaults = {
        t["name"] for t in _MCP_TOOLS_LIST
        if _TOOL_ROLE_RELEVANCE.get(t["name"]) == "executor"
        and _TOOL_CATEGORY.get(t["name"], "other") in _PLANNER_DEFAULT_CATEGORIES
    }
    overlap = active & executor_only_in_planner_defaults
    assert not overlap, (
        f"Executor-only tools in planner-default categories appeared in "
        f"planner active set: {overlap}"
    )

    # Planner-only tools in planner's default categories MUST be active.
    planner_only_in_planner_defaults = {
        t["name"] for t in _MCP_TOOLS_LIST
        if _TOOL_ROLE_RELEVANCE.get(t["name"]) == "planner"
        and _TOOL_CATEGORY.get(t["name"], "other") in _PLANNER_DEFAULT_CATEGORIES
    }
    missing_plan = planner_only_in_planner_defaults - active
    assert not missing_plan, (
        f"Planner-only tools in planner defaults missing from active set: {missing_plan}"
    )


def test_keyword_expansion_adds_code_intel() -> None:
    """a749f87c(2e) — 'code search' in goal text expands executor set to include code-intel."""
    # Without keyword: executor default should have code-intel? Let's check.
    result_no_kw = _select_active_tool_set("executor")
    # With keyword: goal mentions code/codebase/search.
    goal = "Refactor the codebase search index and fix the semantic code-intel graph"
    result_with_kw = _select_active_tool_set("executor", goal)

    # The keyword expansion must have fired.
    assert result_with_kw["mode"] == "deterministic"
    # code-intel must be in active_categories after keyword expansion.
    assert "code-intel" in result_with_kw["active_categories"], (
        f"Expected 'code-intel' in active_categories, got: {result_with_kw['active_categories']}"
    )
    # prospect_symbol is a code-intel tool.
    assert "prospect_symbol" in result_with_kw["active_tools"], (
        "Expected 'prospect_symbol' in active_tools after code-intel keyword expansion"
    )
    assert result_with_kw["keyword_signals"], (
        "Expected at least one keyword_signal from the goal text"
    )


def test_keyword_expansion_adds_docx() -> None:
    """a749f87c(2f) — 'thesis docx' in goal text adds docx category for executor."""
    goal = "Update the thesis docx chapter with new equations and figure captions"
    result = _select_active_tool_set("executor", goal)
    assert "docx" in result["active_categories"]
    assert "index_equation" in result["active_tools"]
    assert "find_similar_figure" in result["active_tools"]


def test_keyword_expansion_adds_research_for_executor() -> None:
    """a749f87c(2g) — 'arxiv paper' in goal text adds research category for executor.

    When a category is keyword-expanded (not in the role's default set), all
    tools in that category are included regardless of their declared
    role_relevance — the user explicitly requested this domain via the /goal.
    """
    goal = "Search arxiv for literature on deterministic MCP tool pre-selection"
    result = _select_active_tool_set("executor", goal)
    assert "research" in result["active_categories"], (
        "Expected 'research' in active_categories after arxiv/paper keyword"
    )
    # paper_search is planner-only by declared role, but 'research' was keyword-
    # expanded from the /goal text — so keyword expansion overrides role_relevance.
    assert "paper_search" in result["active_tools"], (
        "paper_search should be included when 'research' category is keyword-expanded "
        "(keyword expansion overrides role_relevance for non-default categories)"
    )


def test_active_tool_set_no_crash_empty_goal() -> None:
    """a749f87c(2h) — empty/None goal text doesn't crash the selector."""
    r1 = _select_active_tool_set("executor", None)
    r2 = _select_active_tool_set("executor", "")
    assert r1["mode"] == "deterministic"
    assert r2["mode"] == "deterministic"
    # No keyword signals when goal is empty.
    assert r1["keyword_signals"] == []
    assert r2["keyword_signals"] == []


def test_active_plus_excluded_equals_all_tools_for_executor() -> None:
    """a749f87c(2i) — active_tools + excluded_tools is exactly the full tool set."""
    result = _select_active_tool_set("executor")
    all_names = {t["name"] for t in _MCP_TOOLS_LIST}
    combined = set(result["active_tools"]) | set(result["excluded_tools"])
    assert combined == all_names, (
        f"active + excluded != all tools. diff: {all_names.symmetric_difference(combined)}"
    )


def test_active_plus_excluded_equals_all_tools_for_planner() -> None:
    """a749f87c(2j) — same partition check for planner role."""
    result = _select_active_tool_set("planner")
    all_names = {t["name"] for t in _MCP_TOOLS_LIST}
    combined = set(result["active_tools"]) | set(result["excluded_tools"])
    assert combined == all_names, (
        f"active + excluded != all tools. diff: {all_names.symmetric_difference(combined)}"
    )


def test_session_tools_always_included_for_executor() -> None:
    """a749f87c(2k) — core session tools are always in executor active_tools."""
    result = _select_active_tool_set("executor")
    active = set(result["active_tools"])
    # These are "both" role + session category — always included.
    must_have = {"start_session", "generate_handoff", "log_task", "checkpoint"}
    missing = must_have - active
    assert not missing, f"Core session tools missing from executor set: {missing}"


def test_hitl_tools_always_included_for_executor() -> None:
    """a749f87c(2l) — HITL tools are in executor active_tools (hitl is core category)."""
    result = _select_active_tool_set("executor")
    active = set(result["active_tools"])
    assert "request_hitl" in active
    assert "get_hitl_request" in active


def test_returned_structure_fields() -> None:
    """a749f87c(2m) — return dict has all expected keys for any role."""
    for role in (None, "executor", "planner", "unknown"):
        result = _select_active_tool_set(role)
        for key in ("role", "active_categories", "active_tools", "excluded_tools",
                    "keyword_signals", "mode"):
            assert key in result, f"Missing key {key!r} for role={role!r}"
        assert isinstance(result["active_tools"], list)
        assert isinstance(result["excluded_tools"], list)
        assert isinstance(result["active_categories"], list)
        assert isinstance(result["keyword_signals"], list)
        assert result["mode"] == "deterministic"


# ---------------------------------------------------------------------------
# f30bbd89 — reproducible-tie-breaking baseline for the CURRENT deterministic
# router, established alongside the "rag-semantic-tool-routing" design write-up
# (see the INVESTIGATE f30bbd89 comment block at the end of meridian/mcp_tools.py).
# _select_active_tool_set has no scores and therefore no notion of a "tie" the
# way meridian.semantic_search.score_confidence does — inclusion is boolean
# category membership. These tests lock in that (i) repeated calls on
# identical input are byte-identical (list order included, not just set
# equality) and (ii) list ordering always follows _MCP_TOOLS_LIST's own fixed
# declaration order rather than any dict/set iteration order — the concrete
# baseline a future scored/ranked semantic router must not regress on.
# ---------------------------------------------------------------------------

def test_select_active_tool_set_is_deterministic_across_repeated_calls() -> None:
    """f30bbd89 — identical (role, goal_text) input must yield an identical
    result dict (including list ORDER, not just set membership) on every
    call — no hidden randomness (e.g. dict/set iteration order) anywhere in
    the pipeline."""
    goal = "Refactor the codebase search index and update the thesis docx"
    for role in (None, "", "executor", "planner", "unknown"):
        first = _select_active_tool_set(role, goal)
        second = _select_active_tool_set(role, goal)
        assert first == second, f"non-deterministic result for role={role!r}"
        assert first["active_tools"] == second["active_tools"]
        assert first["excluded_tools"] == second["excluded_tools"]
        assert first["keyword_signals"] == second["keyword_signals"]


def test_select_active_tool_set_active_tools_follow_declared_list_order() -> None:
    """f30bbd89 — active_tools/excluded_tools preserve _MCP_TOOLS_LIST's own
    fixed declaration order (a stable filter of that list), not e.g.
    alphabetical or set-derived order — so two callers filtering the same
    role never see the same tools in a different order."""
    result = _select_active_tool_set("executor")
    all_names_in_order = [t["name"] for t in _MCP_TOOLS_LIST]
    active_set = set(result["active_tools"])
    expected_order = [n for n in all_names_in_order if n in active_set]
    assert result["active_tools"] == expected_order


# ---------------------------------------------------------------------------
# e5a7ce7f (decision 2a3a3882, finding 5569beca) — match_categories_by_keywords:
# the SAME keyword -> category-affinity primitive _select_active_tool_set uses
# inline above, generalized as a standalone function so a caller outside
# Meridian's own tool set (meridian.tool_routing) can reuse it with an
# arbitrary affinity mapping. See that function's own docstring for why it is
# additive (does not replace _select_active_tool_set's inline loop).
# ---------------------------------------------------------------------------


def test_match_categories_by_keywords_empty_text_returns_empty() -> None:
    matched, signals = match_categories_by_keywords(None, _KEYWORD_CATEGORY_AFFINITY)
    assert matched == set()
    assert signals == []
    matched, signals = match_categories_by_keywords("", _KEYWORD_CATEGORY_AFFINITY)
    assert matched == set()
    assert signals == []


def test_match_categories_by_keywords_matches_meridians_own_affinity() -> None:
    matched, signals = match_categories_by_keywords(
        "refactor the codebase search index", _KEYWORD_CATEGORY_AFFINITY
    )
    assert "code-intel" in matched
    assert signals, "expected at least one matched keyword"
    assert set(signals) <= {"refactor", "codebase", "search"}


def test_match_categories_by_keywords_no_match_returns_empty() -> None:
    matched, signals = match_categories_by_keywords(
        "the weather today is sunny", _KEYWORD_CATEGORY_AFFINITY
    )
    assert matched == set()
    assert signals == []


def test_match_categories_by_keywords_works_beyond_merdians_own_tools() -> None:
    """The generalization gap this function closes: an arbitrary caller-
    supplied affinity mapping (not _KEYWORD_CATEGORY_AFFINITY / _TOOL_CATEGORY)
    works identically."""
    custom_affinity = {"deploy": "infra", "kubernetes": "infra", "invoice": "billing"}
    matched, signals = match_categories_by_keywords(
        "deploy the new kubernetes manifest", custom_affinity
    )
    assert matched == {"infra"}
    assert set(signals) == {"deploy", "kubernetes"}


def test_match_categories_by_keywords_deterministic_across_repeated_calls() -> None:
    text = "refactor the codebase search index and update the thesis docx"
    first = match_categories_by_keywords(text, _KEYWORD_CATEGORY_AFFINITY)
    second = match_categories_by_keywords(text, _KEYWORD_CATEGORY_AFFINITY)
    assert first == second


def test_match_categories_by_keywords_superset_of_select_active_tool_set_expansion() -> None:
    """Every category _select_active_tool_set added via keyword expansion for
    an executor is also found by the generalized matcher over the same text
    and the same affinity mapping — the generalization is a strict
    superset/consistent view of the original inline loop's matching
    decisions (it just doesn't do the original's per-category dedup)."""
    text = "refactor the codebase search index and update the thesis docx"
    result = _select_active_tool_set("executor", text)
    from meridian.mcp_tools import _EXECUTOR_DEFAULT_CATEGORIES
    expanded_by_original = set(result["active_categories"]) - _EXECUTOR_DEFAULT_CATEGORIES
    matched, _ = match_categories_by_keywords(text, _KEYWORD_CATEGORY_AFFINITY)
    assert expanded_by_original <= matched
