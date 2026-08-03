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

5fe3502e — this file also covers the STRICT, fail-closed counterpart:
meridian.sprint_evidence_guard.verify_strict_completion_evidence. Unlike
_check_stored_evidence (always advisory, above), the strict guard is what
meridian.mcp.handlers.sprint_tools.handle_complete_sprint_item calls ONLY
when a caller opts in (strict_evidence=true or the item's own
require_strict_evidence flag) — see test_cov_handler.py for the full-stack
(MCP dispatch) coverage of that opt-in wiring and the default-unchanged
guarantee. This file exercises the typed-state logic directly: ABSENT vs
INVALID vs STALE vs WRONG_WORKTREE vs UNCLAIMED_EDIT are distinct codes, and
an override is refused without a reason but audited when one is given.
"""
from __future__ import annotations

import subprocess

import pytest

import meridian.db as db_module
from meridian.db.sprint_items import _check_stored_evidence
from meridian.sprint_evidence_guard import (
    EVIDENCE_ABSENT,
    EVIDENCE_INVALID,
    EVIDENCE_STALE,
    UNCLAIMED_EDIT,
    WRONG_WORKTREE,
    record_strict_evidence_override,
    verify_strict_completion_evidence,
)


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


# ---------------------------------------------------------------------------
# 5fe3502e — strict, fail-closed evidence gate (meridian.sprint_evidence_guard)
# ---------------------------------------------------------------------------

class TestStrictEvidenceAbsentAndInvalid:
    @pytest.mark.asyncio
    async def test_absent_when_nothing_declared_at_all(self, db):
        """No touches_resources, no task_id, no notes anywhere -> EVIDENCE_ABSENT,
        and it is the ONLY error (nothing else to check once evidence is absent)."""
        project = await db_module.create_project(db, "strict-absent")
        item = await db_module.add_sprint_item(db, project["id"], "v1", "bare item")
        claimed = await db_module.claim_sprint_item(db, project["id"], item["id"], actor="s1")

        result = await verify_strict_completion_evidence(
            db, None, project["id"], item["id"], claimed,
        )
        assert result["ok"] is False
        codes = [e["code"] for e in result["errors"]]
        assert codes == [EVIDENCE_ABSENT]

    @pytest.mark.asyncio
    async def test_not_absent_when_only_notes_argument_given(self, db):
        """A notes= argument alone (no stored evidence) is enough to clear ABSENT."""
        project = await db_module.create_project(db, "strict-notes-only")
        item = await db_module.add_sprint_item(db, project["id"], "v1", "notes item")
        claimed = await db_module.claim_sprint_item(db, project["id"], item["id"], actor="s1")

        result = await verify_strict_completion_evidence(
            db, None, project["id"], item["id"], claimed, notes="shipped it, verified manually",
        )
        codes = [e["code"] for e in result["errors"]]
        assert EVIDENCE_ABSENT not in codes

    @pytest.mark.asyncio
    async def test_invalid_when_declared_file_does_not_exist(self, db, tmp_path, monkeypatch):
        """touches_resources declares a file that doesn't exist anywhere -> INVALID,
        distinct from ABSENT (something WAS declared, it just doesn't resolve)."""
        monkeypatch.chdir(tmp_path)
        project = await db_module.create_project(db, "strict-invalid-file")
        item = await db_module.add_sprint_item(db, project["id"], "v1", "bad file item")
        claimed = await db_module.claim_sprint_item(db, project["id"], item["id"], actor="s1")
        await db_module.patch_sprint_item(
            db, project["id"], item["id"],
            touches_resources=["file:definitely_not_here_xyz.py"],
        )
        claimed = await db_module.get_sprint_item(db, item["id"])

        result = await verify_strict_completion_evidence(
            db, None, project["id"], item["id"], claimed,
        )
        assert result["ok"] is False
        codes = {e["code"] for e in result["errors"]}
        assert codes == {EVIDENCE_INVALID}

    @pytest.mark.asyncio
    async def test_invalid_when_task_id_unresolvable(self, db):
        """A task_id that resolves to no task_log row -> INVALID."""
        project = await db_module.create_project(db, "strict-invalid-task")
        item = await db_module.add_sprint_item(db, project["id"], "v1", "bad task item")
        claimed = await db_module.claim_sprint_item(db, project["id"], item["id"], actor="s1")

        result = await verify_strict_completion_evidence(
            db, None, project["id"], item["id"], claimed,
            task_id="task-id-that-does-not-exist",
        )
        assert result["ok"] is False
        codes = {e["code"] for e in result["errors"]}
        assert codes == {EVIDENCE_INVALID}

    @pytest.mark.asyncio
    async def test_ok_when_file_evidence_exists_and_is_fresh(self, db, tmp_path, monkeypatch):
        """A real, freshly-modified declared file -> ok, zero errors."""
        monkeypatch.chdir(tmp_path)
        project = await db_module.create_project(db, "strict-ok-fresh-file")
        item = await db_module.add_sprint_item(db, project["id"], "v1", "good file item")
        claimed = await db_module.claim_sprint_item(db, project["id"], item["id"], actor="s1")
        (tmp_path / "evidence.py").write_text("x = 1\n")
        await db_module.patch_sprint_item(
            db, project["id"], item["id"], touches_resources=["file:evidence.py"],
        )
        claimed = await db_module.get_sprint_item(db, item["id"])

        result = await verify_strict_completion_evidence(
            db, None, project["id"], item["id"], claimed,
        )
        assert result["ok"] is True
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_ok_when_task_id_logged_after_claim(self, db):
        """A task logged AFTER the claim (not before it) -> ok, no STALE."""
        project = await db_module.create_project(db, "strict-ok-fresh-task")
        sess = await db_module.register_session(db, project["id"], "sess-fresh")
        item = await db_module.add_sprint_item(db, project["id"], "v1", "fresh task item")
        claimed = await db_module.claim_sprint_item(db, project["id"], item["id"], actor=sess["id"])
        task = await db_module.log_task(db, sess["id"], project["id"], "did the real work")

        result = await verify_strict_completion_evidence(
            db, None, project["id"], item["id"], claimed, task_id=task["id"],
        )
        assert result["ok"] is True
        assert result["errors"] == []


class TestStrictEvidenceStale:
    @pytest.mark.asyncio
    async def test_stale_when_file_mtime_predates_claim(self, db, tmp_path, monkeypatch):
        """A declared file that exists but was last modified BEFORE the claim
        began -> EVIDENCE_STALE, distinct from INVALID (it DOES exist)."""
        import os
        import time
        from datetime import datetime, timedelta

        monkeypatch.chdir(tmp_path)
        old_file = tmp_path / "old_evidence.py"
        old_file.write_text("x = 1\n")
        # Backdate the file's mtime well before "now".
        old_epoch = time.time() - 3600
        os.utime(old_file, (old_epoch, old_epoch))

        project = await db_module.create_project(db, "strict-stale-file")
        item = await db_module.add_sprint_item(db, project["id"], "v1", "stale file item")
        claimed = await db_module.claim_sprint_item(db, project["id"], item["id"], actor="s1")
        await db_module.patch_sprint_item(
            db, project["id"], item["id"], touches_resources=["file:old_evidence.py"],
        )
        claimed = await db_module.get_sprint_item(db, item["id"])
        assert claimed.get("claimed_at")  # sanity: claim actually landed

        result = await verify_strict_completion_evidence(
            db, None, project["id"], item["id"], claimed,
        )
        assert result["ok"] is False
        codes = {e["code"] for e in result["errors"]}
        assert codes == {EVIDENCE_STALE}

    @pytest.mark.asyncio
    async def test_stale_when_task_predates_claim(self, db):
        """A linked task_id logged BEFORE the item was claimed -> EVIDENCE_STALE
        (leftover evidence from an earlier pass at the item)."""
        project = await db_module.create_project(db, "strict-stale-task")
        sess = await db_module.register_session(db, project["id"], "sess-stale")
        task = await db_module.log_task(db, sess["id"], project["id"], "old, unrelated work")
        # Backdate the task's created_at to well before any claim could exist.
        await db.execute(
            "UPDATE task_log SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
            (task["id"],),
        )
        await db.commit()

        item = await db_module.add_sprint_item(db, project["id"], "v1", "stale task item")
        claimed = await db_module.claim_sprint_item(db, project["id"], item["id"], actor=sess["id"])

        result = await verify_strict_completion_evidence(
            db, None, project["id"], item["id"], claimed, task_id=task["id"],
        )
        assert result["ok"] is False
        codes = {e["code"] for e in result["errors"]}
        assert codes == {EVIDENCE_STALE}


class TestStrictEvidenceWrongWorktreeAndUnclaimedEdit:
    @pytest.mark.asyncio
    async def test_wrong_worktree_when_evidence_only_in_main_checkout(self, db, tmp_path):
        """Declared evidence exists relative to repo_root (the main checkout)
        but NOT inside the session's own registered worktree -> WRONG_WORKTREE.
        Uses a real file that also exists relative to the actual process cwd
        (this repo's root, where pytest runs from) so EVIDENCE_INVALID does
        NOT also fire — isolating the WRONG_WORKTREE signal."""
        from pathlib import Path

        project = await db_module.create_project(db, "strict-wrong-worktree")
        sess = await db_module.register_session(db, project["id"], "sess-wt")
        item = await db_module.add_sprint_item(db, project["id"], "v1", "worktree-mismatch item")
        claimed = await db_module.claim_sprint_item(db, project["id"], item["id"], actor=sess["id"])
        await db_module.patch_sprint_item(
            db, project["id"], item["id"],
            touches_resources=["file:meridian/db/sprint_items.py"],
        )
        claimed = await db_module.get_sprint_item(db, item["id"])

        empty_worktree = tmp_path / "empty-worktree"
        empty_worktree.mkdir()
        await db_module.register_worktree(
            db, sess["id"], project["id"], "sprint/mismatch", str(empty_worktree),
            item_id=item["id"],
        )

        result = await verify_strict_completion_evidence(
            db, Path.cwd(), project["id"], item["id"], claimed,
            session_id=sess["id"],
        )
        codes = {e["code"] for e in result["errors"]}
        assert WRONG_WORKTREE in codes

    @pytest.mark.asyncio
    async def test_no_wrong_worktree_when_evidence_is_inside_registered_worktree(self, db, tmp_path):
        """The mirror-image case: the declared file DOES exist inside the
        session's own registered worktree -> no WRONG_WORKTREE."""
        from pathlib import Path

        project = await db_module.create_project(db, "strict-right-worktree")
        sess = await db_module.register_session(db, project["id"], "sess-wt-ok")
        item = await db_module.add_sprint_item(db, project["id"], "v1", "worktree-match item")
        claimed = await db_module.claim_sprint_item(db, project["id"], item["id"], actor=sess["id"])
        await db_module.patch_sprint_item(
            db, project["id"], item["id"], touches_resources=["file:evidence.py"],
        )
        claimed = await db_module.get_sprint_item(db, item["id"])

        real_worktree = tmp_path / "real-worktree"
        real_worktree.mkdir()
        (real_worktree / "evidence.py").write_text("x = 1\n")
        await db_module.register_worktree(
            db, sess["id"], project["id"], "sprint/match", str(real_worktree),
            item_id=item["id"],
        )

        result = await verify_strict_completion_evidence(
            db, Path.cwd(), project["id"], item["id"], claimed,
            session_id=sess["id"],
        )
        codes = {e["code"] for e in result["errors"]}
        assert WRONG_WORKTREE not in codes

    @pytest.mark.asyncio
    async def test_unclaimed_edit_detected_via_real_git_repo(self, db, tmp_path):
        """A file modified in a real git checkout without a claim_file lock
        held by the completing session -> UNCLAIMED_EDIT."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "tracked.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "tracked.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        (repo / "tracked.py").write_text("x = 2\n")  # modified, never claimed

        project = await db_module.create_project(db, "strict-unclaimed-edit")
        sess = await db_module.register_session(db, project["id"], "sess-unclaimed")
        item = await db_module.add_sprint_item(db, project["id"], "v1", "unclaimed edit item")
        claimed = await db_module.claim_sprint_item(db, project["id"], item["id"], actor=sess["id"])

        result = await verify_strict_completion_evidence(
            db, repo, project["id"], item["id"], claimed,
            notes="did the work", session_id=sess["id"],
        )
        codes = {e["code"] for e in result["errors"]}
        assert UNCLAIMED_EDIT in codes

    @pytest.mark.asyncio
    async def test_no_unclaimed_edit_when_file_was_claimed(self, db, tmp_path):
        """The mirror-image case: the modified file WAS claimed via claim_file
        by the completing session -> no UNCLAIMED_EDIT."""
        repo = tmp_path / "repo2"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "tracked.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "tracked.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        (repo / "tracked.py").write_text("x = 2\n")

        project = await db_module.create_project(db, "strict-claimed-edit")
        sess = await db_module.register_session(db, project["id"], "sess-claimed")
        item = await db_module.add_sprint_item(db, project["id"], "v1", "claimed edit item")
        claimed = await db_module.claim_sprint_item(db, project["id"], item["id"], actor=sess["id"])
        await db_module.claim_file(db, "tracked.py", sess["id"])

        result = await verify_strict_completion_evidence(
            db, repo, project["id"], item["id"], claimed,
            notes="did the work", session_id=sess["id"],
        )
        codes = {e["code"] for e in result["errors"]}
        assert UNCLAIMED_EDIT not in codes

    @pytest.mark.asyncio
    async def test_skipped_not_failed_when_repo_root_is_none(self, db):
        """Hosted mode (repo_root=None): WRONG_WORKTREE/UNCLAIMED_EDIT are
        SKIPPED (unverifiable), never reported as failures — mirrors
        worktree_merge_guard's hosted-mode posture exactly."""
        project = await db_module.create_project(db, "strict-hosted-skip")
        sess = await db_module.register_session(db, project["id"], "sess-hosted")
        item = await db_module.add_sprint_item(db, project["id"], "v1", "hosted item")
        claimed = await db_module.claim_sprint_item(db, project["id"], item["id"], actor=sess["id"])

        result = await verify_strict_completion_evidence(
            db, None, project["id"], item["id"], claimed,
            notes="shipped", session_id=sess["id"],
        )
        codes = {e["code"] for e in result["errors"]}
        assert WRONG_WORKTREE not in codes
        assert UNCLAIMED_EDIT not in codes
        assert result["ok"] is True


class TestStrictEvidenceOverrideAudit:
    @pytest.mark.asyncio
    async def test_override_requires_non_empty_reason(self, db):
        """5fe3502e point 3 — an override with no reason is refused outright,
        never silently accepted (an override can never be the silent default)."""
        project = await db_module.create_project(db, "strict-override-noreason")
        item = await db_module.add_sprint_item(db, project["id"], "v1", "needs override")

        with pytest.raises(ValueError):
            await record_strict_evidence_override(
                db, project["id"], item["id"],
                actor="some-session", reason="", errors=[{"code": EVIDENCE_ABSENT}],
            )
        with pytest.raises(ValueError):
            await record_strict_evidence_override(
                db, project["id"], item["id"],
                actor="some-session", reason="   ", errors=[{"code": EVIDENCE_ABSENT}],
            )

    @pytest.mark.asyncio
    async def test_override_with_reason_is_audited(self, db):
        """A valid override (non-empty reason) writes a durable, queryable
        action_audit_log row recording WHO (actor), WHEN (created_at), and
        WHY (reason, inside detail)."""
        project = await db_module.create_project(db, "strict-override-audited")
        item = await db_module.add_sprint_item(db, project["id"], "v1", "override me")

        audit_row = await record_strict_evidence_override(
            db, project["id"], item["id"],
            actor="executor-session-1",
            reason="verified manually outside the declared evidence paths",
            errors=[{"code": EVIDENCE_ABSENT, "message": "nothing declared"}],
        )
        assert audit_row["actor"] == "executor-session-1"
        assert audit_row["project_id"] == project["id"]
        assert audit_row["event_type"] == "sprint_item_strict_evidence_override"
        assert "verified manually" in audit_row["detail"]
        assert "EVIDENCE_ABSENT" in audit_row["detail"]

        log = await db_module.get_action_audit_log(
            db, project_id=project["id"], event_type="sprint_item_strict_evidence_override",
        )
        assert len(log) == 1
        assert log[0]["id"] == audit_row["id"]
