"""Tests for the structural-degradation patch-counter signal (356d6ac8).

Covers:
- patch_count increments on each exclusive write claim
- read claims do NOT increment the counter
- get_structural_degradation_warnings returns files >= threshold with no refactor flag
- files below threshold are not flagged
- refactor_flagged = 1 suppresses the warning
- flag_file_refactor sets the flag and clears the warning
- custom threshold parameter is respected
- multiple files tracked independently per session
- multiple sessions tracked independently
"""
from __future__ import annotations

import pytest

from meridian import db as db_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_session(db, name: str = "test-session") -> tuple[str, str]:
    """Create a project + session, return (project_id, session_id)."""
    p = await db_module.create_project(db, name)
    s = await db_module.register_session(db, p["id"], name)
    return p["id"], s["id"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_write_claim_increments_patch_count(db) -> None:
    """Each exclusive write claim on the same file increments patch_count."""
    _, session_id = await _make_session(db, "patch-count-test")

    await db_module.claim_file(db, "meridian/server.py", session_id, mode="write")
    await db_module.release_file(db, "meridian/server.py", session_id)
    await db_module.claim_file(db, "meridian/server.py", session_id, mode="write")
    await db_module.release_file(db, "meridian/server.py", session_id)
    await db_module.claim_file(db, "meridian/server.py", session_id, mode="write")

    async with db.execute(
        "SELECT patch_count FROM file_patch_counters "
        "WHERE session_id = ? AND file_path = ?",
        (session_id, "meridian/server.py"),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    count = row["patch_count"] if isinstance(row, dict) else row[0]
    assert count == 3


async def test_read_claim_does_not_increment(db) -> None:
    """Shared read claims do NOT increment the structural-degradation counter."""
    _, session_id = await _make_session(db, "read-claim-test")

    await db_module.claim_file(db, "meridian/db/__init__.py", session_id, mode="read")
    await db_module.release_file(db, "meridian/db/__init__.py", session_id)
    await db_module.claim_file(db, "meridian/db/__init__.py", session_id, mode="read")
    await db_module.release_file(db, "meridian/db/__init__.py", session_id)

    async with db.execute(
        "SELECT patch_count FROM file_patch_counters "
        "WHERE session_id = ? AND file_path = ?",
        (session_id, "meridian/db/__init__.py"),
    ) as cur:
        row = await cur.fetchone()
    # No row at all, or patch_count = 0, both acceptable — reads don't count.
    if row is not None:
        count = row["patch_count"] if isinstance(row, dict) else row[0]
        assert count == 0


async def test_degradation_warning_at_threshold(db) -> None:
    """get_structural_degradation_warnings returns files that hit the threshold."""
    _, session_id = await _make_session(db, "deg-warn-test")
    threshold = 3

    # Claim the file threshold times.
    for _ in range(threshold):
        await db_module.claim_file(db, "meridian/server.py", session_id, mode="write")
        await db_module.release_file(db, "meridian/server.py", session_id)

    warnings = await db_module.get_structural_degradation_warnings(
        db, session_id, threshold=threshold
    )
    assert len(warnings) == 1
    w = warnings[0]
    assert w["file_path"] == "meridian/server.py"
    assert w["patch_count"] == threshold
    assert w["refactor_flagged"] is False
    assert "warning" in w
    assert "meridian/server.py" in w["warning"]


async def test_below_threshold_no_warning(db) -> None:
    """Files claimed fewer times than the threshold are not flagged."""
    _, session_id = await _make_session(db, "below-thresh-test")
    threshold = 3

    for _ in range(threshold - 1):
        await db_module.claim_file(db, "meridian/db/locks.py", session_id, mode="write")
        await db_module.release_file(db, "meridian/db/locks.py", session_id)

    warnings = await db_module.get_structural_degradation_warnings(
        db, session_id, threshold=threshold
    )
    assert warnings == []


async def test_refactor_flag_suppresses_warning(db) -> None:
    """After flag_file_refactor, the file no longer appears in degradation warnings."""
    _, session_id = await _make_session(db, "refactor-flag-test")
    threshold = 3

    for _ in range(threshold + 1):
        await db_module.claim_file(db, "meridian/server.py", session_id, mode="write")
        await db_module.release_file(db, "meridian/server.py", session_id)

    # Confirm it IS flagged before the refactor signal.
    before = await db_module.get_structural_degradation_warnings(
        db, session_id, threshold=threshold
    )
    assert len(before) == 1

    # Signal the deliberate refactor.
    result = await db_module.flag_file_refactor(db, session_id, "meridian/server.py")
    assert result is not None
    # The returned row should have refactor_flagged = 1/True.
    assert result.get("refactor_flagged") in (1, True)

    # Now the warning should be gone.
    after = await db_module.get_structural_degradation_warnings(
        db, session_id, threshold=threshold
    )
    assert after == []


async def test_flag_file_refactor_idempotent(db) -> None:
    """flag_file_refactor is safe to call multiple times and creates the row if absent."""
    _, session_id = await _make_session(db, "refactor-idempotent-test")

    # Call without any prior claim — should create the row.
    r1 = await db_module.flag_file_refactor(db, session_id, "meridian/db/locks.py")
    assert r1 is not None
    assert r1.get("refactor_flagged") in (1, True)

    # Call again — should be a no-op with the same result.
    r2 = await db_module.flag_file_refactor(db, session_id, "meridian/db/locks.py")
    assert r2.get("refactor_flagged") in (1, True)


async def test_multiple_files_tracked_independently(db) -> None:
    """patch_count is tracked per (session, file); distinct files don't interfere."""
    _, session_id = await _make_session(db, "multi-file-test")
    threshold = 2

    # Claim file-A 3 times, file-B once.
    for _ in range(3):
        await db_module.claim_file(db, "meridian/server.py", session_id, mode="write")
        await db_module.release_file(db, "meridian/server.py", session_id)

    await db_module.claim_file(db, "meridian/db/locks.py", session_id, mode="write")
    await db_module.release_file(db, "meridian/db/locks.py", session_id)

    warnings = await db_module.get_structural_degradation_warnings(
        db, session_id, threshold=threshold
    )
    # Only server.py should be flagged (3 >= 2); db/locks.py has only 1.
    flagged_paths = {w["file_path"] for w in warnings}
    assert "meridian/server.py" in flagged_paths
    assert "meridian/db/locks.py" not in flagged_paths


async def test_multiple_sessions_tracked_independently(db) -> None:
    """Patch counters are scoped to session_id; session A doesn't bleed into session B."""
    p = await db_module.create_project(db, "multi-session-test")
    s_a = await db_module.register_session(db, p["id"], "session-a")
    s_b = await db_module.register_session(db, p["id"], "session-b")
    threshold = 2

    # Session A claims the file 3 times; session B claims it only once.
    for _ in range(3):
        await db_module.claim_file(db, "meridian/server.py", s_a["id"], mode="write")
        await db_module.release_file(db, "meridian/server.py", s_a["id"])

    await db_module.claim_file(db, "meridian/server.py", s_b["id"], mode="write")
    await db_module.release_file(db, "meridian/server.py", s_b["id"])

    warnings_a = await db_module.get_structural_degradation_warnings(
        db, s_a["id"], threshold=threshold
    )
    warnings_b = await db_module.get_structural_degradation_warnings(
        db, s_b["id"], threshold=threshold
    )

    assert len(warnings_a) == 1  # session A is flagged
    assert warnings_b == []       # session B is not


async def test_custom_threshold_respected(db) -> None:
    """The threshold parameter controls when a file is flagged."""
    _, session_id = await _make_session(db, "custom-thresh-test")

    for _ in range(5):
        await db_module.claim_file(db, "meridian/server.py", session_id, mode="write")
        await db_module.release_file(db, "meridian/server.py", session_id)

    # Threshold=6 — not yet flagged.
    assert await db_module.get_structural_degradation_warnings(
        db, session_id, threshold=6
    ) == []

    # Threshold=5 — just at the boundary, should be flagged.
    warnings = await db_module.get_structural_degradation_warnings(
        db, session_id, threshold=5
    )
    assert len(warnings) == 1


async def test_migration_creates_table(db) -> None:
    """The file_patch_counters table is present after init_db (migration guard)."""
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='file_patch_counters'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None, "file_patch_counters table should exist after init_db"


async def test_write_claim_blocked_does_not_increment(db) -> None:
    """A blocked write claim (another session holds the lock) must NOT increment."""
    p = await db_module.create_project(db, "blocked-claim-test")
    s_owner = await db_module.register_session(db, p["id"], "owner")
    s_other = await db_module.register_session(db, p["id"], "other")

    # Owner claims the file.
    await db_module.claim_file(db, "meridian/server.py", s_owner["id"], mode="write")

    # Other session's claim is blocked.
    result = await db_module.claim_file(db, "meridian/server.py", s_other["id"], mode="write")
    assert result.get("claimed") is False

    # Other session's patch_count should be 0 (or no row).
    async with db.execute(
        "SELECT patch_count FROM file_patch_counters "
        "WHERE session_id = ? AND file_path = ?",
        (s_other["id"], "meridian/server.py"),
    ) as cur:
        row = await cur.fetchone()
    if row is not None:
        count = row["patch_count"] if isinstance(row, dict) else row[0]
        assert count == 0
