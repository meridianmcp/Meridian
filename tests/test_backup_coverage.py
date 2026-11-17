"""Tests for ``meridian/db/backup_coverage.py``: read-only per-project backup
coverage built on the existing ``object_sync_state`` record.

Covers: a project with no records, all-synced coverage with the latest
``synced_at`` chosen by parsed time (mixed formats), a non-synced part
reported with its state and error, unparseable timestamps ignored, project
scoping, the all-projects listing, and the guarantee that the report only
reads (it never creates a sync-state row).
"""
from __future__ import annotations

from meridian import db as db_module
from meridian.db.backup_coverage import (
    UNRECORDED_DB_PARTS,
    _parse_ts,
    get_project_backup_coverage,
    list_project_backup_coverage,
)
from meridian.db.object_sync_state import list_object_sync_states


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


def _hash(char: str) -> str:
    return "sha256:" + char * 64


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

def test_parse_ts_handles_missing_and_empty_values():
    assert _parse_ts(None) is None
    assert _parse_ts("") is None
    assert _parse_ts("   ") is None


def test_parse_ts_accepts_z_suffix_and_naive_values_as_utc():
    zulu = _parse_ts("2026-11-05T09:00:00.000Z")
    naive = _parse_ts("2026-11-05 09:00:00")
    assert zulu is not None and zulu.tzinfo is not None
    assert naive is not None and naive.tzinfo is not None
    assert zulu == naive


def test_parse_ts_ignores_unparseable_values():
    assert _parse_ts("not-a-timestamp") is None


# ---------------------------------------------------------------------------
# Per-project coverage
# ---------------------------------------------------------------------------

async def test_project_with_no_records_is_not_backed_up(db):
    proj_id = await _project(db, "backup-cov-empty")

    report = await get_project_backup_coverage(db, proj_id)

    assert report["project_id"] == proj_id
    assert report["coverage_scope"] == "artifact_content"
    assert report["backed_up"] is False
    assert report["last_backup_at"] is None
    assert report["artifact_parts_recorded"] == 0
    assert report["parts_not_backed_up"] == []
    assert report["parts_without_backup_record"] == list(UNRECORDED_DB_PARTS)


async def test_all_synced_parts_report_backed_up_with_latest_time(db):
    proj_id = await _project(db, "backup-cov-synced")
    await db_module.mark_object_synced(
        db, proj_id, _hash("a"), remote_key="k/a", remote_etag="e1",
        synced_at="2026-11-01T10:00:00.000Z",
    )
    await db_module.mark_object_synced(
        db, proj_id, _hash("b"), remote_key="k/b", remote_etag="e2",
        synced_at="2026-11-05T09:00:00.000Z",
    )

    report = await get_project_backup_coverage(db, proj_id)

    assert report["backed_up"] is True
    assert report["last_backup_at"] == "2026-11-05T09:00:00.000Z"
    assert report["artifact_parts_recorded"] == 2
    assert report["parts_not_backed_up"] == []


async def test_last_backup_time_is_chosen_by_parsed_value_across_formats(db):
    """The naive SQLite-style stamp is newer than the Z-suffixed one, so it
    must win even though its string sorts lower. The older row arriving last
    must not overwrite the latest time."""
    proj_id = await _project(db, "backup-cov-latest")
    await db_module.mark_object_synced(
        db, proj_id, _hash("1"), remote_key="k1", remote_etag="e",
        synced_at="2026-11-05T09:00:00.000Z",
    )
    await db_module.mark_object_synced(
        db, proj_id, _hash("2"), remote_key="k2", remote_etag="e",
        synced_at="2026-11-06 08:00:00",
    )
    await db_module.mark_object_synced(
        db, proj_id, _hash("3"), remote_key="k3", remote_etag="e",
        synced_at="2026-11-01T10:00:00.000Z",
    )

    report = await get_project_backup_coverage(db, proj_id)

    assert report["backed_up"] is True
    assert report["last_backup_at"] == "2026-11-06 08:00:00"


async def test_unparseable_synced_at_still_counts_as_synced_but_sets_no_time(db):
    proj_id = await _project(db, "backup-cov-badts")
    await db_module.mark_object_synced(
        db, proj_id, _hash("4"), remote_key="k4", remote_etag="e",
        synced_at="garbage",
    )

    report = await get_project_backup_coverage(db, proj_id)

    assert report["backed_up"] is True
    assert report["last_backup_at"] is None


async def test_non_synced_part_is_listed_and_blocks_backed_up(db):
    proj_id = await _project(db, "backup-cov-partial")
    await db_module.mark_object_synced(
        db, proj_id, _hash("5"), remote_key="k5", remote_etag="e",
        synced_at="2026-11-04T12:00:00.000Z",
    )
    await db_module.mark_object_queued_sync(db, proj_id, _hash("6"))
    await db_module.mark_object_sync_failed(db, proj_id, _hash("6"), error="timeout")
    await db_module.mark_object_local_only(
        db, proj_id, _hash("7"), artifact_class="export_bundle",
    )

    report = await get_project_backup_coverage(db, proj_id)

    assert report["backed_up"] is False
    assert report["last_backup_at"] == "2026-11-04T12:00:00.000Z"
    assert report["artifact_parts_recorded"] == 3
    by_hash = {part["content_hash"]: part for part in report["parts_not_backed_up"]}
    assert set(by_hash) == {_hash("6"), _hash("7")}
    assert by_hash[_hash("6")]["state"] == "sync_failed"
    assert by_hash[_hash("6")]["last_error"] == "timeout"
    assert by_hash[_hash("7")]["state"] == "local_only"
    assert by_hash[_hash("7")]["artifact_class"] == "export_bundle"
    assert by_hash[_hash("7")]["part"] == "artifact"


async def test_only_unsynced_parts_is_not_backed_up_and_has_no_time(db):
    proj_id = await _project(db, "backup-cov-none-synced")
    await db_module.mark_object_queued_sync(db, proj_id, _hash("8"))
    await db_module.mark_object_unavailable(db, proj_id, _hash("8"), error="401")

    report = await get_project_backup_coverage(db, proj_id)

    assert report["backed_up"] is False
    assert report["last_backup_at"] is None
    assert [p["state"] for p in report["parts_not_backed_up"]] == ["unavailable"]


# ---------------------------------------------------------------------------
# Scoping, listing and read-only behaviour
# ---------------------------------------------------------------------------

async def test_coverage_is_project_scoped(db):
    proj_a = await _project(db, "backup-cov-scope-a")
    proj_b = await _project(db, "backup-cov-scope-b")
    await db_module.mark_object_synced(
        db, proj_a, _hash("9"), remote_key="ka", remote_etag="e",
        synced_at="2026-11-02T00:00:00.000Z",
    )
    await db_module.mark_object_local_only(db, proj_b, _hash("0"))

    report_a = await get_project_backup_coverage(db, proj_a)
    report_b = await get_project_backup_coverage(db, proj_b)

    assert report_a["backed_up"] is True
    assert report_b["backed_up"] is False
    assert report_b["artifact_parts_recorded"] == 1


async def test_list_covers_every_project_including_ones_with_no_records(db):
    proj_synced = await _project(db, "backup-cov-list-synced")
    proj_empty = await _project(db, "backup-cov-list-empty")
    await db_module.mark_object_synced(
        db, proj_synced, _hash("c"), remote_key="kc", remote_etag="e",
        synced_at="2026-11-03T00:00:00.000Z",
    )

    reports = await list_project_backup_coverage(db)
    by_id = {report["project_id"]: report for report in reports}

    assert proj_synced in by_id
    assert proj_empty in by_id
    assert len(by_id) == len(reports)
    assert by_id[proj_synced]["backed_up"] is True
    assert by_id[proj_synced]["last_backup_at"] == "2026-11-03T00:00:00.000Z"
    assert by_id[proj_empty]["backed_up"] is False
    assert by_id[proj_empty]["artifact_parts_recorded"] == 0


async def test_reading_coverage_never_writes_a_sync_state_row(db):
    proj_id = await _project(db, "backup-cov-readonly")

    await get_project_backup_coverage(db, proj_id)
    await list_project_backup_coverage(db)

    assert await list_object_sync_states(db, proj_id) == []
    assert await db_module.get_object_sync_state(db, proj_id, _hash("d")) is None
