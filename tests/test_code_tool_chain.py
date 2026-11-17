"""Tests for the code-search tool selector (meridian.code_tool_chain)."""
from __future__ import annotations

import itertools

import pytest

from meridian import code_tool_chain as ctc
from meridian.code_tool_chain import (
    DEFAULT_SIZE_THRESHOLD_BYTES,
    TOOL_BM25,
    TOOL_CODEBASE_MEMORY,
    TOOL_REGEX,
    TOOL_SERENA,
    choose_code_search_tool,
)

THRESHOLD = 1000  # small threshold so the tests stay readable


def _expected(size: int, index: bool, serena: bool, bm25: bool, threshold: int) -> str:
    """Independent restatement of the documented order, used by the matrix test."""
    if size < threshold:
        return TOOL_REGEX
    if index:
        return TOOL_CODEBASE_MEMORY
    if serena:
        return TOOL_SERENA
    if bm25:
        return TOOL_BM25
    return TOOL_REGEX


# --- below the threshold: regex is the first choice ---------------------------

def test_below_threshold_prefers_regex_over_everything_available():
    tool, reason = choose_code_search_tool(
        indexed_bytes=THRESHOLD - 1,
        has_codebase_index=True,
        has_serena=True,
        has_bm25=True,
        size_threshold_bytes=THRESHOLD,
    )
    assert tool == TOOL_REGEX
    assert "below" in reason
    assert str(THRESHOLD) in reason


def test_below_threshold_with_index_only_still_regex():
    tool, _ = choose_code_search_tool(
        indexed_bytes=0,
        has_codebase_index=True,
        has_serena=False,
        has_bm25=False,
        size_threshold_bytes=THRESHOLD,
    )
    assert tool == TOOL_REGEX


def test_below_threshold_with_no_backends_is_regex():
    tool, _ = choose_code_search_tool(
        indexed_bytes=10,
        has_codebase_index=False,
        has_serena=False,
        has_bm25=False,
        size_threshold_bytes=THRESHOLD,
    )
    assert tool == TOOL_REGEX


# --- at or above the threshold: codebase-memory first when its index exists ---

def test_at_threshold_with_index_chooses_codebase_memory():
    tool, reason = choose_code_search_tool(
        indexed_bytes=THRESHOLD,
        has_codebase_index=True,
        has_serena=False,
        has_bm25=False,
        size_threshold_bytes=THRESHOLD,
    )
    assert tool == TOOL_CODEBASE_MEMORY
    assert "codebase-memory index exists" in reason


def test_above_threshold_with_index_beats_serena_and_bm25():
    tool, _ = choose_code_search_tool(
        indexed_bytes=THRESHOLD * 10,
        has_codebase_index=True,
        has_serena=True,
        has_bm25=True,
        size_threshold_bytes=THRESHOLD,
    )
    assert tool == TOOL_CODEBASE_MEMORY


def test_one_byte_below_threshold_is_not_large_even_with_index():
    tool, _ = choose_code_search_tool(
        indexed_bytes=THRESHOLD - 1,
        has_codebase_index=True,
        has_serena=True,
        has_bm25=True,
        size_threshold_bytes=THRESHOLD,
    )
    assert tool == TOOL_REGEX


# --- at or above the threshold, no codebase index: Serena, then BM25, then regex

def test_above_threshold_no_index_chooses_serena_even_with_bm25():
    tool, reason = choose_code_search_tool(
        indexed_bytes=THRESHOLD + 1,
        has_codebase_index=False,
        has_serena=True,
        has_bm25=True,
        size_threshold_bytes=THRESHOLD,
    )
    assert tool == TOOL_SERENA
    assert "Serena is available" in reason


def test_above_threshold_no_index_no_serena_chooses_bm25():
    tool, reason = choose_code_search_tool(
        indexed_bytes=THRESHOLD + 1,
        has_codebase_index=False,
        has_serena=False,
        has_bm25=True,
        size_threshold_bytes=THRESHOLD,
    )
    assert tool == TOOL_BM25
    assert "BM25 is available" in reason


def test_above_threshold_no_index_no_serena_no_bm25_falls_back_to_regex():
    tool, reason = choose_code_search_tool(
        indexed_bytes=THRESHOLD + 1,
        has_codebase_index=False,
        has_serena=False,
        has_bm25=False,
        size_threshold_bytes=THRESHOLD,
    )
    assert tool == TOOL_REGEX
    assert "no index or search backend is available" in reason


def test_no_index_at_all_with_serena_only_chooses_serena():
    # "No index at all" (no codebase-memory index) still allows Serena above the threshold.
    tool, _ = choose_code_search_tool(
        indexed_bytes=THRESHOLD,
        has_codebase_index=False,
        has_serena=True,
        has_bm25=False,
        size_threshold_bytes=THRESHOLD,
    )
    assert tool == TOOL_SERENA


# --- default and edge thresholds ----------------------------------------------

def test_default_threshold_is_50_mib():
    assert DEFAULT_SIZE_THRESHOLD_BYTES == 50 * 1024 * 1024


def test_default_threshold_boundary_with_index():
    below, _ = choose_code_search_tool(
        indexed_bytes=DEFAULT_SIZE_THRESHOLD_BYTES - 1,
        has_codebase_index=True,
        has_serena=False,
        has_bm25=False,
    )
    at, _ = choose_code_search_tool(
        indexed_bytes=DEFAULT_SIZE_THRESHOLD_BYTES,
        has_codebase_index=True,
        has_serena=False,
        has_bm25=False,
    )
    assert below == TOOL_REGEX
    assert at == TOOL_CODEBASE_MEMORY


def test_threshold_zero_makes_empty_tree_large():
    tool, _ = choose_code_search_tool(
        indexed_bytes=0,
        has_codebase_index=True,
        has_serena=False,
        has_bm25=False,
        size_threshold_bytes=0,
    )
    assert tool == TOOL_CODEBASE_MEMORY


def test_threshold_zero_with_no_index_uses_serena():
    tool, _ = choose_code_search_tool(
        indexed_bytes=0,
        has_codebase_index=False,
        has_serena=True,
        has_bm25=False,
        size_threshold_bytes=0,
    )
    assert tool == TOOL_SERENA


# --- exhaustive matrix over availability flags and both sides of the threshold

@pytest.mark.parametrize(
    ("size", "index", "serena", "bm25"),
    [
        (size, index, serena, bm25)
        for size in (THRESHOLD - 1, THRESHOLD, THRESHOLD + 1)
        for index, serena, bm25 in itertools.product((False, True), repeat=3)
    ],
)
def test_matrix_matches_documented_order(size, index, serena, bm25):
    tool, reason = choose_code_search_tool(
        indexed_bytes=size,
        has_codebase_index=index,
        has_serena=serena,
        has_bm25=bm25,
        size_threshold_bytes=THRESHOLD,
    )
    assert tool == _expected(size, index, serena, bm25, THRESHOLD)
    assert tool in {TOOL_CODEBASE_MEMORY, TOOL_SERENA, TOOL_BM25, TOOL_REGEX}
    assert isinstance(reason, str) and reason


def test_return_value_is_a_two_tuple_of_strings():
    result = choose_code_search_tool(
        indexed_bytes=1,
        has_codebase_index=False,
        has_serena=False,
        has_bm25=False,
    )
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert all(isinstance(part, str) for part in result)


def test_module_exposes_documented_tool_names():
    assert ctc.TOOL_CODEBASE_MEMORY == "codebase-memory"
    assert ctc.TOOL_SERENA == "serena"
    assert ctc.TOOL_BM25 == "bm25"
    assert ctc.TOOL_REGEX == "regex"


# --- input validation -----------------------------------------------------------

@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"indexed_bytes": -1, "size_threshold_bytes": THRESHOLD}, ValueError),
        ({"indexed_bytes": 1, "size_threshold_bytes": -1}, ValueError),
        ({"indexed_bytes": 1.5, "size_threshold_bytes": THRESHOLD}, TypeError),
        ({"indexed_bytes": "10", "size_threshold_bytes": THRESHOLD}, TypeError),
        ({"indexed_bytes": True, "size_threshold_bytes": THRESHOLD}, TypeError),
        ({"indexed_bytes": 1, "size_threshold_bytes": 2.0}, TypeError),
        ({"indexed_bytes": 1, "size_threshold_bytes": False}, TypeError),
    ],
)
def test_invalid_sizes_are_rejected(kwargs, error):
    with pytest.raises(error):
        choose_code_search_tool(
            has_codebase_index=True,
            has_serena=True,
            has_bm25=True,
            **kwargs,
        )
