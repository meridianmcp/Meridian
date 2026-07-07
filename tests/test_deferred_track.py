"""Tests for ENFORCED sprint-item deferral + track (dec69708).

A pinned "we decided to defer the paper-track" decision is only text — nothing
structurally stops an executor from claiming a deferred item. dec69708 adds a
real, enforced deferral: a ``deferred_until`` timestamp on a sprint item that
``claim_sprint_item`` REFUSES to claim while it is in the future, plus a
``track`` lane column. These tests exercise:

* the schema columns exist after init_db,
* claiming a future-deferred item is refused (blocked dict, item stays pending),
* claiming after the date / with no deferral / with a cleared deferral works,
* add_sprint_item + update_sprint_item can set/clear deferred_until and track,
* the MCP dispatch surfaces the block and forwards the fields.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from meridian import db as db_module
from meridian import server as server_module

# _dispatch_mcp_tool is re-exported from server (importing it directly from
# meridian.mcp.handler at module top-level triggers a circular import).
_dispatch_mcp_tool = server_module._dispatch_mcp_tool


def _future_iso(hours: int = 48) -> str:
    """An ISO-8601 timestamp `hours` in the future (UTC, no tz suffix)."""
    return (datetime.utcnow() + timedelta(hours=hours)).isoformat()


def _past_iso(hours: int = 48) -> str:
    """An ISO-8601 timestamp `hours` in the past (UTC, no tz suffix)."""
    return (datetime.utcnow() - timedelta(hours=hours)).isoformat()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_has_deferral_columns(db):
    """deferred_until + track exist on sprint_items after init_db."""
    async with db.execute("PRAGMA table_info(sprint_items)") as cur:
        rows = await cur.fetchall()
    cols = {(r["name"] if isinstance(r, dict) else r[1]) for r in rows}
    assert "deferred_until" in cols
    assert "track" in cols


@pytest.mark.asyncio
async def test_deferral_migration_is_idempotent(db):
    """Re-running the SQLite migration is a safe no-op (columns already there)."""
    from meridian.db import migrations as _mig

    # Migration already ran inside init_db; running again must not raise.
    await _mig._migrate_sprint_item_deferral(db)
    async with db.execute("PRAGMA table_info(sprint_items)") as cur:
        rows = await cur.fetchall()
    cols = {(r["name"] if isinstance(r, dict) else r[1]) for r in rows}
    assert {"deferred_until", "track"} <= cols


# ---------------------------------------------------------------------------
# Enforcement — claim_sprint_item
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_future_deferred_item_is_refused(db):
    """A sprint item deferred to the future cannot be claimed."""
    p = await db_module.create_project(db, "defer-future")
    pid = p["id"]
    item = await db_module.add_sprint_item(
        db, pid, "v1", "write the paper", deferred_until=_future_iso(72),
        track="paper",
    )
    result = await db_module.claim_sprint_item(db, pid, item["id"])
    assert isinstance(result, dict)
    assert result.get("blocked") is True
    assert result.get("error") == "DEFERRED"
    assert result.get("deferred_until") == item["deferred_until"]
    assert result.get("track") == "paper"
    assert "reason" in result
    # The item was NOT claimed — it stays pending/todo, no claimed_at stamped.
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["status"] != "in_progress"
    assert stored.get("claimed_at") is None


@pytest.mark.asyncio
async def test_claim_past_deferred_item_succeeds(db):
    """Once the deferral date has passed, the item claims normally."""
    p = await db_module.create_project(db, "defer-past")
    pid = p["id"]
    item = await db_module.add_sprint_item(
        db, pid, "v1", "ship the release", deferred_until=_past_iso(1),
    )
    result = await db_module.claim_sprint_item(db, pid, item["id"])
    assert isinstance(result, dict)
    assert result.get("blocked") is not True
    assert result.get("status") == "in_progress"
    assert result.get("claimed_at") is not None


@pytest.mark.asyncio
async def test_claim_undeferred_item_succeeds(db):
    """An item with no deferral claims normally (default behaviour unchanged)."""
    p = await db_module.create_project(db, "defer-none")
    pid = p["id"]
    item = await db_module.add_sprint_item(db, pid, "v1", "normal task")
    assert item.get("deferred_until") is None
    result = await db_module.claim_sprint_item(db, pid, item["id"])
    assert result.get("status") == "in_progress"


@pytest.mark.asyncio
async def test_deferral_accepts_space_separated_db_format(db):
    """A DB-style 'YYYY-MM-DD HH:MM:SS' future timestamp is also enforced."""
    p = await db_module.create_project(db, "defer-dbfmt")
    pid = p["id"]
    future = (datetime.utcnow() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "later task", deferred_until=future,
    )
    result = await db_module.claim_sprint_item(db, pid, item["id"])
    assert result.get("blocked") is True


@pytest.mark.asyncio
async def test_deferral_accepts_tz_aware_iso(db):
    """A tz-aware ISO timestamp (trailing Z / offset) in the future is enforced."""
    p = await db_module.create_project(db, "defer-tz")
    pid = p["id"]
    future = (
        datetime.now(timezone.utc) + timedelta(hours=24)
    ).isoformat().replace("+00:00", "Z")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "tz task", deferred_until=future,
    )
    result = await db_module.claim_sprint_item(db, pid, item["id"])
    assert result.get("blocked") is True


@pytest.mark.asyncio
async def test_garbage_deferral_fails_open(db):
    """An unparseable deferred_until must NOT wedge the board — claim succeeds."""
    p = await db_module.create_project(db, "defer-garbage")
    pid = p["id"]
    item = await db_module.add_sprint_item(
        db, pid, "v1", "garbage-deferral", deferred_until="not-a-timestamp",
    )
    result = await db_module.claim_sprint_item(db, pid, item["id"])
    assert result.get("status") == "in_progress"


# ---------------------------------------------------------------------------
# update_sprint_item — set + clear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_sets_and_then_clears_deferral(db):
    """patch_sprint_item can set a deferral (blocks the claim) and clear it
    (unblocks the claim)."""
    p = await db_module.create_project(db, "defer-patch")
    pid = p["id"]
    item = await db_module.add_sprint_item(db, pid, "v1", "patchable")
    # Set a future deferral.
    await db_module.patch_sprint_item(
        db, pid, item["id"], deferred_until=_future_iso(48), track="paper",
    )
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["deferred_until"] is not None
    assert stored["track"] == "paper"
    blocked = await db_module.claim_sprint_item(db, pid, item["id"])
    assert blocked.get("blocked") is True

    # Clear the deferral with an empty string → claimable again.
    await db_module.patch_sprint_item(db, pid, item["id"], deferred_until="")
    cleared = await db_module.get_sprint_item(db, item["id"])
    assert cleared["deferred_until"] is None
    ok = await db_module.claim_sprint_item(db, pid, item["id"])
    assert ok.get("status") == "in_progress"


@pytest.mark.asyncio
async def test_patch_omitting_deferral_leaves_it_unchanged(db):
    """Omitting deferred_until from a patch must not clobber the stored value."""
    p = await db_module.create_project(db, "defer-omit")
    pid = p["id"]
    fut = _future_iso(48)
    item = await db_module.add_sprint_item(
        db, pid, "v1", "omit-test", deferred_until=fut,
    )
    # Patch an unrelated field; deferral must survive.
    await db_module.patch_sprint_item(db, pid, item["id"], notes="touched")
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["deferred_until"] == fut
    assert stored["notes"] == "touched"


# ---------------------------------------------------------------------------
# MCP dispatch surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_add_and_claim_deferred(db, tmp_path):
    """add_sprint_item via MCP stores the deferral; claim_sprint_item via MCP
    returns the blocked dict instead of claiming."""
    p = await db_module.create_project(db, "mcp-defer")
    pid = p["id"]
    added = await _dispatch_mcp_tool(
        "add_sprint_item",
        {
            "project_id": pid,
            "version": "v1",
            "title": "paper-track item",
            "deferred_until": _future_iso(72),
            "track": "paper",
            "force": True,
        },
        db,
        str(tmp_path),
    )
    assert added.get("deferred_until") is not None
    assert added.get("track") == "paper"

    claimed = await _dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": added["id"]},
        db,
        str(tmp_path),
    )
    assert claimed.get("blocked") is True
    assert claimed.get("error") == "DEFERRED"
    # No worktree plumbing leaked into a blocked response.
    assert "worktree_suggested" not in claimed


@pytest.mark.asyncio
async def test_mcp_update_clears_deferral_then_claim_succeeds(db, tmp_path):
    """update_sprint_item via MCP can clear the deferral so the item claims."""
    p = await db_module.create_project(db, "mcp-clear")
    pid = p["id"]
    item = await db_module.add_sprint_item(
        db, pid, "v1", "clearable", deferred_until=_future_iso(48),
    )
    # Clear via MCP dispatch (empty string).
    updated = await _dispatch_mcp_tool(
        "update_sprint_item",
        {"project_id": pid, "item_id": item["id"], "deferred_until": ""},
        db,
        str(tmp_path),
    )
    assert updated.get("deferred_until") is None
    claimed = await _dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": item["id"]},
        db,
        str(tmp_path),
    )
    assert claimed.get("status") == "in_progress"
