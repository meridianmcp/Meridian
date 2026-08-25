"""1d34c076 (build milestone; investigation 549e66c6 §6) — tests for
``meridian/db/object_sync_state.py``: the durable sync-state table for the
optional, inactive-by-default object-storage backend.

Covers: migration idempotency (SQLite path, run via the ``db`` fixture's
``init_db`` and re-run directly), the local_only -> queued_sync -> synced
happy path, sync_failed vs. unavailable as distinct states with retry
bookkeeping, project scoping, and list_retry_eligible.

Postgres coverage: ``meridian.pg_adapter._migrate_pg_object_sync_state`` is
exercised implicitly by every Postgres CI run through ``init_pg_db`` (same
mechanism as every other migration in ``_PG_MIGRATIONS_LATE``) and by
``test_pg_migration_registry_matches_historical_order`` in
``tests/test_core.py``, which pins its exact position in the registry.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian.db.object_sync_state import (
    OBJECT_SYNC_STATES,
    RETRY_ELIGIBLE_STATES,
    _migrate_object_sync_state,
)


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

async def test_migration_is_idempotent(db):
    # init_db already ran the migration once; re-running must be a no-op.
    await _migrate_object_sync_state(db)
    await _migrate_object_sync_state(db)


async def test_states_and_retry_eligible_constants_are_consistent():
    assert RETRY_ELIGIBLE_STATES <= OBJECT_SYNC_STATES
    assert RETRY_ELIGIBLE_STATES == {"sync_failed", "unavailable"}
    assert OBJECT_SYNC_STATES == {
        "local_only", "queued_sync", "synced", "remote_stale",
        "sync_failed", "unavailable",
    }


# ---------------------------------------------------------------------------
# Happy path: local_only -> queued_sync -> synced
# ---------------------------------------------------------------------------

async def test_get_returns_none_when_never_recorded(db):
    proj_id = await _project(db, "sync-state-proj-1")
    row = await db_module.get_object_sync_state(db, proj_id, "sha256:" + "a" * 64)
    assert row is None


async def test_mark_local_only_creates_row(db):
    proj_id = await _project(db, "sync-state-proj-2")
    content_hash = "sha256:" + "b" * 64

    row = await db_module.mark_object_local_only(
        db, proj_id, content_hash, artifact_class="ai_log_artifact",
    )
    assert row["state"] == "local_only"
    assert row["project_id"] == proj_id
    assert row["content_hash"] == content_hash
    assert row["artifact_class"] == "ai_log_artifact"
    assert row["retry_count"] == 0


async def test_mark_local_only_is_idempotent_and_does_not_regress_state(db):
    """Calling mark_local_only again after a later transition must NOT
    reset progress backward — local_only is only the natural starting
    state, never re-asserted over a further-along row."""
    proj_id = await _project(db, "sync-state-proj-3")
    content_hash = "sha256:" + "c" * 64

    await db_module.mark_object_local_only(db, proj_id, content_hash)
    await db_module.mark_object_synced(
        db, proj_id, content_hash, remote_key="remote/key/1", remote_etag="etag-1",
    )
    # Re-calling mark_local_only must be a no-op — the row is already synced.
    row = await db_module.mark_object_local_only(db, proj_id, content_hash)
    assert row["state"] == "synced"


async def test_full_happy_path_transitions(db):
    proj_id = await _project(db, "sync-state-proj-4")
    content_hash = "sha256:" + "d" * 64

    row = await db_module.mark_object_local_only(db, proj_id, content_hash, artifact_class="export_bundle")
    assert row["state"] == "local_only"

    row = await db_module.mark_object_queued_sync(db, proj_id, content_hash, backend="fake_s3")
    assert row["state"] == "queued_sync"
    assert row["queued_at"] is not None
    assert row["backend"] == "fake_s3"

    row = await db_module.mark_object_synced(
        db, proj_id, content_hash, remote_key="proj/export_bundle/dd/dddd", remote_etag="sha256:deadbeef",
    )
    assert row["state"] == "synced"
    assert row["synced_at"] is not None
    assert row["remote_key"] == "proj/export_bundle/dd/dddd"
    assert row["remote_etag"] == "sha256:deadbeef"
    # artifact_class set at local_only time is preserved through transitions
    # (COALESCE keeps it since later calls pass None).
    assert row["artifact_class"] == "export_bundle"


# ---------------------------------------------------------------------------
# sync_failed (transient) vs. unavailable (categorical) — distinct states
# ---------------------------------------------------------------------------

async def test_sync_failed_is_transient_and_bumps_retry_count(db):
    proj_id = await _project(db, "sync-state-proj-5")
    content_hash = "sha256:" + "e" * 64
    await db_module.mark_object_queued_sync(db, proj_id, content_hash)

    row = await db_module.mark_object_sync_failed(db, proj_id, content_hash, error="timeout")
    assert row["state"] == "sync_failed"
    assert row["last_error"] == "timeout"
    assert row["retry_count"] == 1

    row = await db_module.mark_object_sync_failed(db, proj_id, content_hash, error="timeout again")
    assert row["retry_count"] == 2
    assert row["last_error"] == "timeout again"


async def test_unavailable_is_categorical_and_distinct_from_sync_failed(db):
    proj_id = await _project(db, "sync-state-proj-6")
    content_hash = "sha256:" + "f" * 64
    await db_module.mark_object_queued_sync(db, proj_id, content_hash)

    row = await db_module.mark_object_unavailable(db, proj_id, content_hash, error="401 auth failed")
    assert row["state"] == "unavailable"
    assert row["last_error"] == "401 auth failed"
    assert row["state"] != "sync_failed"


async def test_successful_sync_clears_error_and_resets_retry_count(db):
    """A successful sync fully resolves any prior failure history for this
    content — retry_count and last_error must not linger after recovery."""
    proj_id = await _project(db, "sync-state-proj-7")
    content_hash = "sha256:" + "1" * 64
    await db_module.mark_object_queued_sync(db, proj_id, content_hash)
    await db_module.mark_object_sync_failed(db, proj_id, content_hash, error="boom")
    await db_module.mark_object_sync_failed(db, proj_id, content_hash, error="boom again")

    row = await db_module.mark_object_synced(
        db, proj_id, content_hash, remote_key="k", remote_etag="e",
    )
    assert row["state"] == "synced"
    assert row["retry_count"] == 0
    assert row["last_error"] is None


async def test_invalid_state_rejected(db):
    proj_id = await _project(db, "sync-state-proj-8")
    with pytest.raises(ValueError):
        await db_module.list_object_sync_states(db, proj_id, state="not_a_real_state")


# ---------------------------------------------------------------------------
# Project scoping
# ---------------------------------------------------------------------------

async def test_list_is_project_scoped(db):
    proj_a = await _project(db, "sync-state-scope-a")
    proj_b = await _project(db, "sync-state-scope-b")
    await db_module.mark_object_local_only(db, proj_a, "sha256:" + "2" * 64)
    await db_module.mark_object_local_only(db, proj_b, "sha256:" + "3" * 64)
    await db_module.mark_object_local_only(db, proj_b, "sha256:" + "4" * 64)

    rows_a = await db_module.list_object_sync_states(db, proj_a)
    rows_b = await db_module.list_object_sync_states(db, proj_b)
    assert len(rows_a) == 1
    assert len(rows_b) == 2


async def test_list_filtered_by_state(db):
    proj_id = await _project(db, "sync-state-filter")
    await db_module.mark_object_local_only(db, proj_id, "sha256:" + "5" * 64)
    await db_module.mark_object_queued_sync(db, proj_id, "sha256:" + "6" * 64)
    await db_module.mark_object_queued_sync(db, proj_id, "sha256:" + "7" * 64)

    local_only_rows = await db_module.list_object_sync_states(db, proj_id, state="local_only")
    queued_rows = await db_module.list_object_sync_states(db, proj_id, state="queued_sync")
    assert len(local_only_rows) == 1
    assert len(queued_rows) == 2


# ---------------------------------------------------------------------------
# Retry-eligible sweep set
# ---------------------------------------------------------------------------

async def test_list_retry_eligible_includes_sync_failed_and_unavailable_only(db):
    proj_id = await _project(db, "sync-state-retry")
    h_local = "sha256:" + "8" * 64
    h_failed = "sha256:" + "9" * 64
    h_unavail = "sha256:" + "0" * 64
    h_synced = "sha256:" + "a" * 63 + "b"

    await db_module.mark_object_local_only(db, proj_id, h_local)
    await db_module.mark_object_queued_sync(db, proj_id, h_failed)
    await db_module.mark_object_sync_failed(db, proj_id, h_failed, error="e1")
    await db_module.mark_object_queued_sync(db, proj_id, h_unavail)
    await db_module.mark_object_unavailable(db, proj_id, h_unavail, error="e2")
    await db_module.mark_object_queued_sync(db, proj_id, h_synced)
    await db_module.mark_object_synced(db, proj_id, h_synced, remote_key="k", remote_etag="e")

    eligible = await db_module.list_object_sync_retry_eligible(db, proj_id)
    eligible_hashes = {r["content_hash"] for r in eligible}
    assert eligible_hashes == {h_failed, h_unavail}


async def test_list_retry_eligible_is_project_scoped(db):
    proj_a = await _project(db, "sync-state-retry-a")
    proj_b = await _project(db, "sync-state-retry-b")
    h = "sha256:" + "c" * 64
    await db_module.mark_object_queued_sync(db, proj_a, h)
    await db_module.mark_object_sync_failed(db, proj_a, h, error="e")

    eligible_a = await db_module.list_object_sync_retry_eligible(db, proj_a)
    eligible_b = await db_module.list_object_sync_retry_eligible(db, proj_b)
    assert len(eligible_a) == 1
    assert len(eligible_b) == 0
