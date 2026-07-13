"""Tests for 890046a2 — time-based stall detection in analyze_sprint.

The bug: analyze_sprint's stall list relied solely on the persisted
stall_count column, which is only incremented when a session is explicitly
archived/closed with items still claimed.  Sessions abandoned by closing a
chat window without a clean archive call never get stall_count bumped, so
items could sit in_progress for days and never appear in the stalls list.

Fix: a second "time" path flags any item whose claimed_at is older than
_SPRINT_STALL_FLAG_HOURS (4 h), regardless of stall_count.  Each entry now
carries a ``reason`` field: "counter", "time", or "both".
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from meridian import db as db_module
from meridian.db import _SPRINT_STALL_FLAG_HOURS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _old_claimed_at(hours_ago: float) -> str:
    """Return a claimed_at value older than the stall threshold."""
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _fresh_claimed_at(hours_ago: float = 0.5) -> str:
    """Return a claimed_at value well within the stall threshold."""
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def _make_in_progress(db, project_id, title="work"):
    """Add a sprint item and claim it (sets status=in_progress, claimed_at=now)."""
    item = await db_module.add_sprint_item(db, project_id, "v1", title)
    await db_module.claim_sprint_item(db, project_id, item["id"])
    return await db_module.get_sprint_item(db, item["id"])


# ---------------------------------------------------------------------------
# (a) stall_count=0 but claimed_at older than threshold → flagged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_time_based_stall_no_counter(db):
    """890046a2(a) — item with stall_count=0 and old claimed_at IS in stalls."""
    p = await db_module.create_project(db, "tbsn")
    item = await _make_in_progress(db, p["id"], "old-work")

    # Back-date claimed_at to beyond the threshold without touching stall_count.
    old_ts = _old_claimed_at(_SPRINT_STALL_FLAG_HOURS + 1)
    await db.execute(
        "UPDATE sprint_items SET claimed_at = ? WHERE id = ?",
        (old_ts, item["id"]),
    )

    brief = await db_module.analyze_sprint(db, p["id"])
    ids = [s["id"] for s in brief["stalls"]]
    assert item["id"] in ids, "Expected old claimed-at item in stalls"

    entry = next(s for s in brief["stalls"] if s["id"] == item["id"])
    assert entry["stall_count"] == 0, "stall_count should still be 0"
    assert entry["reason"] == "time", f"Expected reason='time', got {entry['reason']!r}"


# ---------------------------------------------------------------------------
# (b) fresh claimed_at + stall_count=0 → NOT flagged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fresh_claim_not_flagged(db):
    """890046a2(b) — item with fresh claimed_at and stall_count=0 is NOT in stalls."""
    p = await db_module.create_project(db, "fcnf")
    item = await _make_in_progress(db, p["id"], "active-work")

    # Explicitly confirm claimed_at is recent (it will be, from claim_sprint_item).
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["claimed_at"] is not None

    brief = await db_module.analyze_sprint(db, p["id"])
    ids = [s["id"] for s in brief["stalls"]]
    assert item["id"] not in ids, "Fresh in-flight item should NOT be in stalls"


# ---------------------------------------------------------------------------
# (c) stall_count>0 path still works (regression guard)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_counter_path_still_works(db):
    """890046a2(c) — existing stall_count>0 detection path is unchanged."""
    p = await db_module.create_project(db, "cpsw")
    item = await _make_in_progress(db, p["id"], "stalled-counter")

    # Set stall_count > 0 while keeping claimed_at recent (within threshold).
    fresh_ts = _fresh_claimed_at(0.1)
    await db.execute(
        "UPDATE sprint_items SET stall_count = 1, claimed_at = ? WHERE id = ?",
        (fresh_ts, item["id"]),
    )

    brief = await db_module.analyze_sprint(db, p["id"])
    ids = [s["id"] for s in brief["stalls"]]
    assert item["id"] in ids, "stall_count=1 item must appear in stalls"

    entry = next(s for s in brief["stalls"] if s["id"] == item["id"])
    assert entry["stall_count"] == 1
    assert entry["reason"] == "counter", f"Expected reason='counter', got {entry['reason']!r}"


# ---------------------------------------------------------------------------
# (d) reason field correctly distinguishes paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reason_both_when_counter_and_time(db):
    """890046a2(d) — reason='both' when stall_count>0 AND claimed_at is old."""
    p = await db_module.create_project(db, "rbtc")
    item = await _make_in_progress(db, p["id"], "double-stall")

    old_ts = _old_claimed_at(_SPRINT_STALL_FLAG_HOURS + 2)
    await db.execute(
        "UPDATE sprint_items SET stall_count = 2, claimed_at = ? WHERE id = ?",
        (old_ts, item["id"]),
    )

    brief = await db_module.analyze_sprint(db, p["id"])
    entry = next((s for s in brief["stalls"] if s["id"] == item["id"]), None)
    assert entry is not None, "Item with stall_count>0 and old claimed_at must be in stalls"
    assert entry["reason"] == "both", f"Expected reason='both', got {entry['reason']!r}"
    assert entry["stall_count"] == 2


@pytest.mark.asyncio
async def test_reason_fields_distinct_for_mixed_items(db):
    """890046a2(d) — multiple stalls in one sprint show distinct reasons."""
    p = await db_module.create_project(db, "rfdi")

    # Item 1: old claimed_at, stall_count=0 → reason "time"
    item_time = await _make_in_progress(db, p["id"], "time-stall")
    old_ts = _old_claimed_at(_SPRINT_STALL_FLAG_HOURS + 3)
    await db.execute(
        "UPDATE sprint_items SET claimed_at = ? WHERE id = ?",
        (old_ts, item_time["id"]),
    )

    # Item 2: fresh claimed_at, stall_count=1 → reason "counter"
    item_counter = await _make_in_progress(db, p["id"], "counter-stall")
    fresh_ts = _fresh_claimed_at(0.2)
    await db.execute(
        "UPDATE sprint_items SET stall_count = 1, claimed_at = ? WHERE id = ?",
        (fresh_ts, item_counter["id"]),
    )

    # Item 3: fresh claimed_at, stall_count=0 → NOT in stalls
    item_ok = await _make_in_progress(db, p["id"], "ok-work")

    brief = await db_module.analyze_sprint(db, p["id"])
    by_id = {s["id"]: s for s in brief["stalls"]}

    assert item_time["id"] in by_id, "Time-stalled item must appear"
    assert by_id[item_time["id"]]["reason"] == "time"

    assert item_counter["id"] in by_id, "Counter-stalled item must appear"
    assert by_id[item_counter["id"]]["reason"] == "counter"

    assert item_ok["id"] not in by_id, "Fresh active item must NOT appear"
