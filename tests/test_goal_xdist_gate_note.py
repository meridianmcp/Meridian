"""Tests for 2a06a840 — xdist INTERNALERROR guidance in the megasprint /goal.

When pixi run test -n 3 produces a pytest-xdist worker crash (INTERNALERROR)
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
    goal = h._build_quick_start_goal([{**_ITEM, "title": "FEAT: implementation"}])
    assert "<test_gate_note>" in goal, (
        "<test_gate_note> block missing from _build_quick_start_goal output"
    )


def test_xdist_gate_note_mentions_internalerror():
    """Guidance must explicitly mention the INTERNALERROR marker."""
    goal = h._build_quick_start_goal([{**_ITEM, "title": "FEAT: implementation"}])
    assert "INTERNALERROR" in goal


def test_xdist_gate_note_mentions_stash_steps():
    """Triage steps (stash, isolate, restore) must be present in order."""
    goal = h._build_quick_start_goal([{**_ITEM, "title": "FEAT: implementation"}])
    # Check all three numbered steps appear
    assert "git stash" in goal
    assert "no:xdist" in goal
    assert "git stash pop" in goal


def test_xdist_gate_note_mentions_serial_fallback():
    """The guidance must recommend re-running without the configured
    parallelism flag as the fallback (6cfdabd7 -- this no longer hardcodes
    "-n 3": the fallback command is derived from the EFFECTIVE test_cmd)."""
    goal = h._build_quick_start_goal([{**_ITEM, "title": "FEAT: implementation"}])
    assert "WITHOUT the configured parallelism flag" in goal
    assert "timeout=60" in goal


def test_xdist_gate_note_no_stale_default_parallelism_flag():
    """6cfdabd7 regression guard: the default (no executor_config.test_cmd
    configured) /goal must NOT assert a hardcoded "-n 3" anywhere -- that
    was the exact staleness bug (pixi.toml's own `test` task moved to
    "-n auto" long before the /goal text was updated to match)."""
    goal = h._build_quick_start_goal([{**_ITEM, "title": "FEAT: implementation"}])
    assert "-n 3" not in goal


def test_xdist_gate_note_uses_configured_test_cmd():
    """When a project has a configured test_cmd (e.g. via set_executor_config),
    the gate note's first mention AND the "rerun without parallelism" fallback
    must both reflect that EXACT command, not a hardcoded default."""
    goal = h._build_quick_start_goal(
        [{**_ITEM, "title": "FEAT: implementation"}],
        test_cmd="pixi run test -n auto",
    )
    assert "`pixi run test -n auto`" in goal
    assert "-n 3" not in goal
    # The "rerun without parallelism" fallback strips the -n flag from the
    # ACTUAL configured command rather than asserting a second,
    # independently hardcoded command.
    assert "WITHOUT the configured parallelism flag (`pixi run test`)" in goal
    # The machine-readable companion tag agrees with the prose.
    assert 'test_cmd="pixi run test -n auto"' in goal
    assert 'parallelism="-n auto"' in goal


def test_xdist_gate_note_is_closed_tag():
    """The XML tag must be properly closed — malformed XML would confuse parsers."""
    goal = h._build_quick_start_goal([{**_ITEM, "title": "FEAT: implementation"}])
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
    goal = h._build_quick_start_goal([{**_ITEM, "title": "FEAT: implementation"}])
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
        [{**_ITEM, "title": "FEAT: implementation"}],
        execution_mode=execution_mode,
        completion_mode=completion_mode,
    )
    assert "<test_gate_note>" in goal, (
        f"<test_gate_note> missing for execution_mode={execution_mode!r}, "
        f"completion_mode={completion_mode!r}"
    )
