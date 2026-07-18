"""1ec33edf — refile of abb7c388 (original shipped only on a worktree branch
that never merged into dev — confirmed unfixed and redone here).

Automated post-completion verification for complete_sprint_item(): a real,
mechanical evidence check — not just required_notes text presence. Tests
:func:`_check_stored_evidence`, which checks that evidence declared on a
sprint item (touches_resources file/symbol paths, a linked task_id, or file
paths mentioned in notes) actually exists on disk / in the DB before the item
is marked done.

Design invariants verified here:
  - ADVISORY ONLY: completion always succeeds (status=done) even when the
    stored-evidence check fires a warning.
  - Runs for EVERY completion, not just required_notes-gated ones.
  - Fail-open: absence of touches_resources / task_id / file-shaped notes is
    NOT itself suspicious and never warns.
  - A declared touches_resources file that does not exist on disk triggers
    ``stored_evidence_warning``; one that does exist does not.
  - A task_id that resolves to no task_log row triggers a warning; one that
    resolves to a real row does not.
"""
from __future__ import annotations

import pytest

import meridian.db as db_module
from meridian.db.sprint_items import _check_stored_evidence


# ---------------------------------------------------------------------------
# Unit tests for the pure-ish heuristic function (DB needed for task_id check)
# ---------------------------------------------------------------------------

class TestCheckStoredEvidenceUnit:
    @pytest.mark.asyncio
    async def test_no_evidence_declared_no_warning(self, db):
        """An item with no touches_resources, no task_id, and no notes never warns."""
        item = {"touches_resources": None, "task_id": None, "notes": None}
        warning = await _check_stored_evidence(db, item, None, None)
        assert warning is None

    @pytest.mark.asyncio
    async def test_declared_file_that_exists_no_warning(self, db):
        """A touches_resources file: entry pointing at a real file does not warn."""
        item = {
            "touches_resources": '["file:meridian/db/sprint_items.py"]',
            "task_id": None,
            "notes": None,
        }
        warning = await _check_stored_evidence(db, item, None, None)
        assert warning is None

    @pytest.mark.asyncio
    async def test_declared_file_that_does_not_exist_warns(self, db):
        """All declared touches_resources files missing from disk fires a warning."""
        item = {
            "touches_resources": '["file:meridian/definitely_does_not_exist_xyz123.py"]',
            "task_id": None,
            "notes": None,
        }
        warning = await _check_stored_evidence(db, item, None, None)
        assert warning is not None
        assert "cannot be found on disk" in warning

    @pytest.mark.asyncio
    async def test_declared_symbol_path_that_exists_no_warning(self, db):
        """symbol: resource ids extract the path portion for the existence check."""
        item = {
            "touches_resources": '["symbol:meridian/db/sprint_items.py::complete_sprint_item"]',
            "task_id": None,
            "notes": None,
        }
        warning = await _check_stored_evidence(db, item, None, None)
        assert warning is None

    @pytest.mark.asyncio
    async def test_mixed_existing_and_missing_declared_files_no_warning(self, db):
        """At least one declared path existing is enough — fail-open on partial evidence."""
        item = {
            "touches_resources": (
                '["file:meridian/db/sprint_items.py", '
                '"file:meridian/does_not_exist_at_all.py"]'
            ),
            "task_id": None,
            "notes": None,
        }
        warning = await _check_stored_evidence(db, item, None, None)
        assert warning is None

    @pytest.mark.asyncio
    async def test_task_id_argument_resolving_to_real_row_no_warning(self, db):
        project = await db_module.create_project(db, "stored-evidence-task-ok")
        sess = await db_module.register_session(db, project["id"], "sess-1")
        task = await db_module.log_task(db, sess["id"], project["id"], "did real work")
        item = {"touches_resources": None, "task_id": None, "notes": None}
        warning = await _check_stored_evidence(db, item, task["id"], None)
        assert warning is None

    @pytest.mark.asyncio
    async def test_task_id_resolving_to_nothing_warns(self, db):
        item = {"touches_resources": None, "task_id": None, "notes": None}
        warning = await _check_stored_evidence(db, item, "task-id-that-does-not-exist", None)
        assert warning is not None
        assert "no matching task_log row" in warning

    @pytest.mark.asyncio
    async def test_stored_task_id_on_item_resolving_to_nothing_warns(self, db):
        """A task_id already stored on the item (not just the argument) is also checked."""
        item = {"touches_resources": None, "task_id": "stale-task-id-xyz", "notes": None}
        warning = await _check_stored_evidence(db, item, None, None)
        assert warning is not None

    @pytest.mark.asyncio
    async def test_notes_mentioning_existing_path_no_warning(self, db):
        item = {"touches_resources": None, "task_id": None, "notes": None}
        warning = await _check_stored_evidence(
            db, item, None, "Fixed the bug in meridian/db/sprint_items.py directly"
        )
        assert warning is None

    @pytest.mark.asyncio
    async def test_notes_mentioning_only_missing_paths_warns(self, db):
        item = {"touches_resources": None, "task_id": None, "notes": None}
        warning = await _check_stored_evidence(
            db, item, None, "Fixed the bug in meridian/totally_fake_module_xyz.py directly"
        )
        assert warning is not None
        assert "cannot be found on disk" in warning

    @pytest.mark.asyncio
    async def test_notes_with_no_plausible_path_no_warning(self, db):
        """Bare notes with no file-shaped tokens never warns (nothing to check)."""
        item = {"touches_resources": None, "task_id": None, "notes": None}
        warning = await _check_stored_evidence(
            db, item, None, "Refactored the validation logic and re-ran the suite"
        )
        assert warning is None

    @pytest.mark.asyncio
    async def test_never_raises_on_garbage_touches_resources(self, db):
        """Fail-open: malformed touches_resources must not raise."""
        item = {"touches_resources": "{not valid json[[[", "task_id": None, "notes": None}
        warning = await _check_stored_evidence(db, item, None, None)
        # Must not raise; a warning either way is acceptable, but no exception.
        assert warning is None or isinstance(warning, str)


# ---------------------------------------------------------------------------
# Integration tests via complete_sprint_item — end to end
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_sprint_item_warns_but_does_not_block_missing_file(db):
    """Core invariant: a missing declared file warns but completion still succeeds."""
    project = await db_module.create_project(db, "stored-evidence-e2e-missing")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Do a thing")
    await db_module.patch_sprint_item(
        db, project["id"], item["id"],
        touches_resources=["file:meridian/nope_not_real_xyz.py"],
    )
    await db_module.claim_sprint_item(db, project["id"], item["id"])
    result = await db_module.complete_sprint_item(
        db, project["id"], item["id"], notes="shipped it",
    )
    assert result is not None
    assert result["status"] == "done"
    assert "stored_evidence_warning" in result
    assert "cannot be found on disk" in result["stored_evidence_warning"]


@pytest.mark.asyncio
async def test_complete_sprint_item_no_warning_for_real_file(db):
    """A declared file that genuinely exists produces no warning."""
    project = await db_module.create_project(db, "stored-evidence-e2e-real")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Do a real thing")
    await db_module.patch_sprint_item(
        db, project["id"], item["id"],
        touches_resources=["file:meridian/db/sprint_items.py"],
    )
    await db_module.claim_sprint_item(db, project["id"], item["id"])
    result = await db_module.complete_sprint_item(
        db, project["id"], item["id"], notes="shipped it for real",
    )
    assert result is not None
    assert result["status"] == "done"
    assert "stored_evidence_warning" not in result


@pytest.mark.asyncio
async def test_complete_sprint_item_no_touches_resources_no_warning(db):
    """Items with no declared resources, task_id, or file-shaped notes never warn."""
    project = await db_module.create_project(db, "stored-evidence-e2e-none")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Simple untracked task")
    await db_module.claim_sprint_item(db, project["id"], item["id"])
    result = await db_module.complete_sprint_item(
        db, project["id"], item["id"], notes="Just did it, nothing fancy",
    )
    assert result is not None
    assert result["status"] == "done"
    assert "stored_evidence_warning" not in result


@pytest.mark.asyncio
async def test_complete_sprint_item_runs_check_even_without_required_notes(db):
    """The stored-evidence check runs for ALL completions, not only required_notes ones."""
    project = await db_module.create_project(db, "stored-evidence-e2e-ungated")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Ungated task")
    await db_module.patch_sprint_item(
        db, project["id"], item["id"],
        touches_resources=["file:meridian/still_not_real_xyz.py"],
    )
    # required_notes is NOT set, so no evidence is required at all — but the
    # stored-evidence warning must still fire since touches_resources is bogus.
    await db_module.claim_sprint_item(db, project["id"], item["id"])
    result = await db_module.complete_sprint_item(db, project["id"], item["id"])
    assert result is not None
    assert result["status"] == "done"
    assert "stored_evidence_warning" in result


@pytest.mark.asyncio
async def test_complete_sprint_item_both_warnings_can_coexist(db):
    """evidence_quality_warning and stored_evidence_warning can both fire together."""
    project = await db_module.create_project(db, "stored-evidence-e2e-both")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Gated task")
    await db_module.patch_sprint_item(
        db, project["id"], item["id"],
        required_notes=True,
        touches_resources=["file:meridian/nope_still_fake_xyz.py"],
    )
    await db_module.claim_sprint_item(db, project["id"], item["id"])
    result = await db_module.complete_sprint_item(
        db, project["id"], item["id"],
        notes="test passed",  # short + single-claim -> triggers evidence_quality_warning too
    )
    assert result is not None
    assert result["status"] == "done"
    assert "stored_evidence_warning" in result
    assert "evidence_quality_warning" in result


@pytest.mark.asyncio
async def test_complete_sprint_item_valid_task_id_no_warning(db):
    """A task_id that resolves to a real task_log row produces no warning."""
    project = await db_module.create_project(db, "stored-evidence-e2e-taskid")
    sess = await db_module.register_session(db, project["id"], "sess-e2e")
    task = await db_module.log_task(db, sess["id"], project["id"], "did the real work")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Task-linked item")
    await db_module.claim_sprint_item(db, project["id"], item["id"])
    result = await db_module.complete_sprint_item(
        db, project["id"], item["id"], task_id=task["id"],
    )
    assert result is not None
    assert result["status"] == "done"
    assert "stored_evidence_warning" not in result
