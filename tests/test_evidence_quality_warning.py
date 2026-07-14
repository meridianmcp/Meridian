"""fd2800ae — evidence quality warning heuristic for complete_sprint_item.

Tests the non-blocking heuristic that fires an ``evidence_quality_warning``
when a required_notes item is completed with evidence text that matches
structural over-fit patterns (single test name only, too short, single-test
pass claim with no structural context).

Design invariants verified here:
  - The heuristic is NEVER a hard block: the item always completes (status=done).
  - A linked task_id bypasses text heuristics entirely (task log is the evidence).
  - Items without required_notes never get the warning regardless of notes text.
  - The warning is conservative: clear, substantive evidence never triggers it.
"""
from __future__ import annotations

import pytest
import aiosqlite

import meridian.db as db_module
from meridian.db.sprint_items import _check_evidence_quality


# ---------------------------------------------------------------------------
# Unit tests for the pure heuristic function
# ---------------------------------------------------------------------------

class TestCheckEvidenceQuality:
    """Pure unit tests for _check_evidence_quality — no DB needed."""

    def test_empty_string_returns_none(self):
        assert _check_evidence_quality("") is None

    def test_none_like_returns_none(self):
        # Only called with non-empty evidence in practice, but must not raise.
        assert _check_evidence_quality("   ") is None

    def test_too_short_triggers_warning(self):
        warning = _check_evidence_quality("test passed")
        assert warning is not None
        assert "short" in warning.lower()

    def test_exactly_at_min_length_boundary(self):
        # 30 chars exactly should NOT trigger the length rule.
        text = "a" * 30
        # It might trigger another rule if it matches a pattern, but length rule
        # specifically should not fire when len >= 30.
        warning = _check_evidence_quality(text)
        # Either no warning OR a warning from a different rule (not "short").
        if warning is not None:
            assert "short" not in warning.lower()

    def test_single_test_name_no_file_ref_triggers_warning(self):
        """Only a test_ reference with no file/module/func mention fires rule 2."""
        warning = _check_evidence_quality(
            "test_fix_calculation_overflow now passes as expected after the change"
        )
        assert warning is not None
        assert "test_fix_calculation_overflow" in warning

    def test_single_test_name_with_file_ref_does_not_trigger(self):
        """When a file is also mentioned, the narrowness concern is addressed."""
        warning = _check_evidence_quality(
            "Fixed overflow in math_utils.py; test_fix_calculation_overflow now passes"
        )
        assert warning is None

    def test_single_test_name_with_module_ref_does_not_trigger(self):
        """When a known module name is present, the evidence is not single-test-only."""
        warning = _check_evidence_quality(
            "Updated meridian handler; test_complete_item now passes as expected"
        )
        assert warning is None

    def test_single_test_name_with_def_ref_does_not_trigger(self):
        """'def foo()' reference means a function was described."""
        warning = _check_evidence_quality(
            "Rewrote def calculate() to handle edge cases; test_calculate passes"
        )
        assert warning is None

    def test_multiple_test_names_does_not_trigger_rule2(self):
        """Rule 2 only fires on exactly ONE test_ reference (single-test-only)."""
        warning = _check_evidence_quality(
            "test_foo and test_bar and test_baz all pass after the refactor"
        )
        # May still hit rule 3 or others, but rule 2 should not fire
        # (multiple tests mentioned = not the single-test-only pattern).
        # We verify rule 2 phrasing is absent.
        if warning is not None:
            assert "references a single test function" not in warning

    def test_one_test_passed_no_file_triggers_rule3(self):
        """'1 test passed' with no structural context is an over-fit signal."""
        warning = _check_evidence_quality(
            "Ran the suite and 1 test passed after updating the return value"
        )
        assert warning is not None

    def test_one_test_passed_with_file_ref_does_not_trigger_rule3(self):
        """File reference alongside 'test passed' means context is present."""
        warning = _check_evidence_quality(
            "Fixed overflow in math_utils.py: 1 test passed in the relevant suite"
        )
        assert warning is None

    def test_the_test_passed_no_context_triggers_rule3(self):
        """'the test passed' with no mention of files/modules is an over-fit signal."""
        warning = _check_evidence_quality(
            "Applied the change and the test passed as expected after the fix"
        )
        assert warning is not None

    def test_one_test_passed_with_module_does_not_trigger_rule3(self):
        """Module reference alongside '1 test passed' provides structural context."""
        warning = _check_evidence_quality(
            "Patched meridian handler: 1 test passed in the full run"
        )
        assert warning is None

    def test_substantive_evidence_no_warning(self):
        """Clear, substantive evidence describing file + mechanism should not warn."""
        warning = _check_evidence_quality(
            "Fixed overflow bug in math_utils.py calculate() function by clamping "
            "intermediate value to INT_MAX. Full test suite (pytest -n auto) passes "
            "with 350+ tests green."
        )
        assert warning is None

    def test_evidence_mentioning_commit_and_file_no_warning(self):
        """Commit reference + file path is clearly structural."""
        warning = _check_evidence_quality(
            "Shipped fix in db/sprint_items.py; committed abc1234 to dev; "
            "pixi run test shows 352 passed."
        )
        assert warning is None


# ---------------------------------------------------------------------------
# Integration tests using the real DB (via the shared `db` fixture)
# ---------------------------------------------------------------------------

@pytest.fixture
async def proj_and_item(db):
    """Create a project and a required_notes sprint item."""
    project = await db_module.create_project(db, "evidence-quality-test")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Implement the auth fix")
    await db_module.patch_sprint_item(db, project["id"], item["id"], required_notes=True)
    return project, item


@pytest.mark.asyncio
async def test_overfit_evidence_warns_but_does_not_block(db, proj_and_item):
    """fd2800ae core invariant: over-fit evidence triggers warning but item still completes."""
    project, item = proj_and_item
    # Claim the item so it is in_progress.
    await db_module.claim_sprint_item(db, project["id"], item["id"])
    # Short, single-test-only evidence: triggers rule 2.
    # Single-test-only notes (rule 2): long enough to pass rule 1, but only
    # mentions a single test_ name with no file/module/function context.
    result = await db_module.complete_sprint_item(
        db, project["id"], item["id"],
        notes="test_auth_redirect passes now as expected after the update",
    )
    assert result is not None
    assert result["status"] == "done", "Completion must succeed even with weak evidence"
    assert "evidence_quality_warning" in result, "Warning must be present for over-fit evidence"
    assert "test_auth_redirect" in result["evidence_quality_warning"]


@pytest.mark.asyncio
async def test_substantive_evidence_no_warning(db, proj_and_item):
    """Good evidence: file + mechanism described → no warning."""
    project, item = proj_and_item
    await db_module.claim_sprint_item(db, project["id"], item["id"])
    result = await db_module.complete_sprint_item(
        db, project["id"], item["id"],
        notes=(
            "Fixed redirect handling in auth.py by returning the correct 302 status. "
            "Full suite (pixi run test) passes — 352 tests green."
        ),
    )
    assert result is not None
    assert result["status"] == "done"
    assert "evidence_quality_warning" not in result, "Substantive evidence must not warn"


@pytest.mark.asyncio
async def test_task_id_bypasses_text_heuristic(db, proj_and_item):
    """A linked task_id is treated as structural evidence — heuristic skipped."""
    project, item = proj_and_item
    sess = await db_module.register_session(db, project["id"], "test-session")
    task = await db_module.log_task(db, sess["id"], project["id"], "did the work")
    await db_module.claim_sprint_item(db, project["id"], item["id"])
    # Even if notes text alone would trigger a warning, task_id bypasses it.
    result = await db_module.complete_sprint_item(
        db, project["id"], item["id"],
        task_id=task["id"],
        notes="test passed",  # would trigger rule 3 alone
    )
    assert result is not None
    assert result["status"] == "done"
    assert "evidence_quality_warning" not in result, "task_id must bypass text heuristic"


@pytest.mark.asyncio
async def test_no_required_notes_no_warning(db):
    """Items without required_notes never get the warning regardless of notes text."""
    project = await db_module.create_project(db, "no-gate-proj")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Simple task")
    # required_notes is NOT set — heuristic should never run.
    await db_module.claim_sprint_item(db, project["id"], item["id"])
    result = await db_module.complete_sprint_item(
        db, project["id"], item["id"],
        notes="test passed",  # would trigger a warning if required_notes were set
    )
    assert result is not None
    assert result["status"] == "done"
    assert "evidence_quality_warning" not in result, "Non-gated items must never warn"


@pytest.mark.asyncio
async def test_absence_of_evidence_still_raises(db, proj_and_item):
    """Hard gate (SprintItemEvidenceRequired) must still fire when no evidence at all."""
    project, item = proj_and_item
    await db_module.claim_sprint_item(db, project["id"], item["id"])
    with pytest.raises(db_module.SprintItemEvidenceRequired):
        await db_module.complete_sprint_item(db, project["id"], item["id"])


@pytest.mark.asyncio
async def test_too_short_notes_warns(db, proj_and_item):
    """Short evidence text triggers the length rule."""
    project, item = proj_and_item
    await db_module.claim_sprint_item(db, project["id"], item["id"])
    result = await db_module.complete_sprint_item(
        db, project["id"], item["id"],
        notes="fixed it",  # < 30 chars, no mechanism described
    )
    assert result is not None
    assert result["status"] == "done"
    assert "evidence_quality_warning" in result
    assert "short" in result["evidence_quality_warning"].lower()


@pytest.mark.asyncio
async def test_existing_stored_notes_used_as_evidence(db):
    """Notes already stored on the item count as evidence for the heuristic."""
    project = await db_module.create_project(db, "stored-notes-proj")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Stored notes task")
    await db_module.patch_sprint_item(
        db, project["id"], item["id"],
        required_notes=True,
        notes=(
            "Fixed the overflow in math_utils.py: clamped intermediate values. "
            "Full pixi test run shows 352 tests green after this change."
        ),
    )
    await db_module.claim_sprint_item(db, project["id"], item["id"])
    # Complete with no new notes — stored notes should satisfy both existence gate
    # AND quality heuristic.
    result = await db_module.complete_sprint_item(db, project["id"], item["id"])
    assert result is not None
    assert result["status"] == "done"
    assert "evidence_quality_warning" not in result


@pytest.mark.asyncio
async def test_single_test_passed_no_context_warns(db, proj_and_item):
    """Rule 3: 'the test passed' with no file/module context fires a warning."""
    project, item = proj_and_item
    await db_module.claim_sprint_item(db, project["id"], item["id"])
    result = await db_module.complete_sprint_item(
        db, project["id"], item["id"],
        notes="Applied the change and the test passed after my fix was applied",
    )
    assert result is not None
    assert result["status"] == "done"
    assert "evidence_quality_warning" in result
