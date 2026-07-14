"""Tests for 2a06a840 — xdist INTERNALERROR guidance in the megasprint /goal.

When pixi run test -n auto produces a pytest-xdist worker crash (INTERNALERROR)
rather than a normal per-test FAILED line, executors must not misattribute the
infra flake as a real regression. _build_quick_start_goal now injects a
<test_gate_note> block with explicit triage steps so this is documented inline
in the /goal template rather than requiring ad-hoc improvisation.

These tests assert the guidance text is present and structurally sound in the
generated /goal output.
"""

from __future__ import annotations

import pytest

from meridian import handoff as h


_ITEM = {"id": "aabbccdd-0000-0000-0000-000000000001", "version": None}


# ---------------------------------------------------------------------------
# Presence checks: <test_gate_note> must appear in items-path /goal output
# ---------------------------------------------------------------------------


def test_xdist_gate_note_present_in_items_path():
    """The <test_gate_note> tag must appear when there are pending sprint items."""
    goal = h._build_quick_start_goal([_ITEM])
    assert "<test_gate_note>" in goal, (
        "<test_gate_note> block missing from _build_quick_start_goal output"
    )


def test_xdist_gate_note_mentions_internalerror():
    """Guidance must explicitly mention the INTERNALERROR marker."""
    goal = h._build_quick_start_goal([_ITEM])
    assert "INTERNALERROR" in goal


def test_xdist_gate_note_mentions_stash_steps():
    """Triage steps (stash, isolate, restore) must be present in order."""
    goal = h._build_quick_start_goal([_ITEM])
    # Check all three numbered steps appear
    assert "git stash" in goal
    assert "no:xdist" in goal
    assert "git stash pop" in goal


def test_xdist_gate_note_mentions_serial_fallback():
    """The guidance must recommend re-running without -n auto as the fallback."""
    goal = h._build_quick_start_goal([_ITEM])
    # Both forms of the recommendation should be present
    assert "WITHOUT -n auto" in goal or "without -n auto" in goal
    assert "timeout=60" in goal


def test_xdist_gate_note_is_closed_tag():
    """The XML tag must be properly closed — malformed XML would confuse parsers."""
    goal = h._build_quick_start_goal([_ITEM])
    assert "</test_gate_note>" in goal


# ---------------------------------------------------------------------------
# Empty-board path: the note is only relevant when items are present,
# but it should not break the empty-board path either.
# ---------------------------------------------------------------------------


def test_empty_board_path_still_works():
    """_build_quick_start_goal([]) must not raise and still produces a /goal."""
    goal = h._build_quick_start_goal([])
    assert goal.startswith("/goal\n") or goal.startswith("/loop /goal\n")


# ---------------------------------------------------------------------------
# Structure: <test_gate_note> must come after </completion_criteria>
# ---------------------------------------------------------------------------


def test_xdist_gate_note_after_completion_criteria():
    """The test_gate_note must appear after the completion_criteria tag."""
    goal = h._build_quick_start_goal([_ITEM])
    cc_pos = goal.find("</completion_criteria>")
    gate_pos = goal.find("<test_gate_note>")
    assert cc_pos != -1, "</completion_criteria> not found in goal"
    assert gate_pos != -1, "<test_gate_note> not found in goal"
    assert gate_pos > cc_pos, (
        "<test_gate_note> must appear after </completion_criteria> in goal output; "
        f"completion_criteria ends at {cc_pos}, gate_note starts at {gate_pos}"
    )


# ---------------------------------------------------------------------------
# Parameterised: note survives all meaningful execution_mode / completion_mode
# combinations (since _not_done_until and hitl_clause vary by mode).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("execution_mode", ["autonomous", "interactive"])
@pytest.mark.parametrize("completion_mode", ["strict", "lenient"])
def test_xdist_gate_note_across_modes(execution_mode: str, completion_mode: str):
    """The <test_gate_note> must appear regardless of execution/completion mode."""
    goal = h._build_quick_start_goal(
        [_ITEM],
        execution_mode=execution_mode,
        completion_mode=completion_mode,
    )
    assert "<test_gate_note>" in goal, (
        f"<test_gate_note> missing for execution_mode={execution_mode!r}, "
        f"completion_mode={completion_mode!r}"
    )
