"""24f5146d — docx promotion base-hash staleness wired into check_wave_resume.

New test file (distinct from tests/test_efaa918a_resume_wave.py, which
covers check_wave_resume's PRE-EXISTING board/dependency/wave-membership
staleness contract and is left untouched here). This file covers ONLY the
promotion-target extension this sprint item adds:

  1. meridian.db.wave_runs.get_pinned_promotion_targets — the durable,
     append-only-event-backed read of what create_wave_run(promotion_targets=...)
     pinned (base_sha256 per target, [] when none were ever pinned).
  2. meridian.db.wave_resume.check_wave_resume — the NEW docx promotion-
     target staleness check: a pinned target whose on-disk content changed
     since the wave was planned fails resume closed, with an actionable
     reason and a promotion_target_status entry, even though nothing about
     it moves board_snapshot's own tracked-field revision hash (sprint-item
     state, not filesystem state).
  3. Backward compatibility: a run created with NO promotion_targets sees
     ZERO behavior change — check_wave_resume's promotion_target_status is
     simply an empty list, never a new source of staleness.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import artifact_declaration as ad
from meridian.db import wave_runs as wave_runs_module
from meridian.db.wave_resume import WaveResumeStale


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


async def _run_with_snapshot(db, project_id: str, **kwargs):
    """Mirrors tests/test_efaa918a_resume_wave.py's own _run helper: a real
    board snapshot is required before check_wave_resume can do anything at
    all (a run with no snapshot pinned refuses resume outright — unrelated
    to the promotion-target extension this file covers)."""
    snapshot = await db_module.build_board_snapshot(db, project_id)
    return await db_module.create_wave_run(db, project_id, snapshot=snapshot, **kwargs)


# ---------------------------------------------------------------------------
# get_pinned_promotion_targets
# ---------------------------------------------------------------------------

async def test_get_pinned_promotion_targets_empty_when_none_pinned(db):
    pid = await _project(db, "wr-no-promotion")
    run = await _run_with_snapshot(db, pid)
    pinned = await wave_runs_module.get_pinned_promotion_targets(db, run["id"])
    assert pinned == []


async def test_get_pinned_promotion_targets_pins_base_hash_at_creation(db, tmp_path):
    target = tmp_path / "thesis.docx"
    target.write_bytes(b"original content")
    expected_hash = ad.compute_base_sha256(target)

    pid = await _project(db, "wr-pins-hash")
    run = await _run_with_snapshot(db, pid, promotion_targets=[str(target)])

    pinned = await wave_runs_module.get_pinned_promotion_targets(db, run["id"])
    assert len(pinned) == 1
    assert pinned[0]["target_docx_path"] == str(target)
    assert pinned[0]["base_sha256"] == expected_hash


async def test_get_pinned_promotion_targets_missing_file_pins_none_hash(db, tmp_path):
    """A target that does not exist yet (its first promotion op will CREATE
    it) pins base_sha256=None — an "unknown base" is a valid state, never a
    wave-creation failure. Mirrors PatchManifest.create_from_file's own
    semantics."""
    missing = tmp_path / "not-created-yet.docx"
    pid = await _project(db, "wr-pins-missing")
    run = await _run_with_snapshot(db, pid, promotion_targets=[str(missing)])

    pinned = await wave_runs_module.get_pinned_promotion_targets(db, run["id"])
    assert len(pinned) == 1
    assert pinned[0]["base_sha256"] is None


async def test_get_pinned_promotion_targets_pins_multiple_targets(db, tmp_path):
    t1 = tmp_path / "a.docx"
    t2 = tmp_path / "b.docx"
    t1.write_bytes(b"aaa")
    t2.write_bytes(b"bbb")

    pid = await _project(db, "wr-pins-multi")
    run = await _run_with_snapshot(db, pid, promotion_targets=[str(t1), str(t2)])

    pinned = await wave_runs_module.get_pinned_promotion_targets(db, run["id"])
    assert {p["target_docx_path"] for p in pinned} == {str(t1), str(t2)}


async def test_create_wave_run_without_promotion_targets_unaffected(db):
    """Backward compatibility: the parameter is entirely optional and a run
    created without it behaves identically to before this feature existed."""
    pid = await _project(db, "wr-no-promotion-param")
    run = await _run_with_snapshot(db, pid)
    assert run["status"] == "planned"
    assert await wave_runs_module.get_pinned_promotion_targets(db, run["id"]) == []


# ---------------------------------------------------------------------------
# check_wave_resume — docx promotion-target staleness
# ---------------------------------------------------------------------------

async def test_check_wave_resume_unaffected_when_no_promotion_targets(db):
    pid = await _project(db, "cwr-no-promotion")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: ordinary item")
    run = await _run_with_snapshot(db, pid, item_ids=[item["id"]])

    result = await db_module.check_wave_resume(db, run["id"])
    assert result["resumable"] is True
    assert result["promotion_target_status"] == []


async def test_check_wave_resume_unchanged_promotion_target_is_resumable(db, tmp_path):
    target = tmp_path / "thesis.docx"
    target.write_bytes(b"stable content")

    pid = await _project(db, "cwr-promotion-unchanged")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: promote figure")
    run = await _run_with_snapshot(
        db, pid, item_ids=[item["id"]], promotion_targets=[str(target)],
    )

    result = await db_module.check_wave_resume(db, run["id"])
    assert result["resumable"] is True
    assert len(result["promotion_target_status"]) == 1
    assert result["promotion_target_status"][0]["unchanged"] is True


async def test_check_wave_resume_changed_promotion_target_fails_closed(db, tmp_path):
    target = tmp_path / "thesis.docx"
    target.write_bytes(b"content at plan time")

    pid = await _project(db, "cwr-promotion-changed")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: promote figure")
    run = await _run_with_snapshot(
        db, pid, item_ids=[item["id"]], promotion_targets=[str(target)],
    )

    # The docx target changes on disk AFTER the wave was planned.
    target.write_bytes(b"content changed out from under the plan")

    with pytest.raises(WaveResumeStale) as excinfo:
        await db_module.check_wave_resume(db, run["id"])

    assert str(target) in str(excinfo.value)
    assert any("changed on disk" in r for r in excinfo.value.reasons)
    # The exception's resume_delta is still the (unrelated) board diff —
    # promotion-target staleness is reported via .reasons, board_snapshot's
    # own revision hash is untouched by a filesystem-only change.
    assert excinfo.value.resume_delta["changed"] is False


async def test_check_wave_resume_missing_promotion_target_fails_closed(db, tmp_path):
    """A target deleted since the wave was planned is also a real staleness
    signal (current hash None != pinned hash)."""
    target = tmp_path / "thesis.docx"
    target.write_bytes(b"content")

    pid = await _project(db, "cwr-promotion-deleted")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: promote figure")
    run = await _run_with_snapshot(
        db, pid, item_ids=[item["id"]], promotion_targets=[str(target)],
    )

    target.unlink()

    with pytest.raises(WaveResumeStale) as excinfo:
        await db_module.check_wave_resume(db, run["id"])
    assert any("changed on disk" in r for r in excinfo.value.reasons)


async def test_check_wave_resume_promotion_status_included_alongside_board_staleness(db, tmp_path):
    """Board staleness (an added item) and promotion-target staleness are
    BOTH reported together — one is not masked by the other."""
    target = tmp_path / "thesis.docx"
    target.write_bytes(b"content")

    pid = await _project(db, "cwr-both-stale")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: original item")
    run = await _run_with_snapshot(
        db, pid, item_ids=[item["id"]], promotion_targets=[str(target)],
    )

    new_item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: injected mid-wave")
    target.write_bytes(b"changed")

    with pytest.raises(WaveResumeStale) as excinfo:
        await db_module.check_wave_resume(db, run["id"])

    reasons_joined = " ".join(excinfo.value.reasons)
    assert new_item["id"] in reasons_joined
    assert str(target) in reasons_joined
    assert excinfo.value.resume_delta["changed"] is True
