"""Tests for f89d440f — enforced 'superseded' blocker_kind on claim_sprint_item.

Before this fix, "do not claim, this is superseded" only existed as prose in a
sprint item's notes field, which claim_sprint_item never reads. An item like
c2021725 could be correctly declined by one session and become claimable
again in the very next session, since nothing structural stopped it — the
same non-authoritative-notes pattern already known from b66c9168.

'manual' (2282a636) is deliberately left as a SOFT gate: it only excludes an
item from listing/wave-assignment surfaces, not from a direct claim by
item_id, because a human may legitimately hand an executor a manual-blocked
item's id once the real-world blocker has been cleared.

'superseded' is a HARD gate: claim_sprint_item refuses it outright even on a
direct claim by item_id, because a superseded item's id can reach an executor
through a stale goal block or prior session memory without ever appearing in
a fresh listing — exactly the failure mode this closes.

Tests cover:
  (a) claim_sprint_item refuses a blocker_kind='superseded' item (blocked dict).
  (b) the item is NOT actually claimed (stays pending, no claimed_at).
  (c) blocker_kind='manual' items are still directly claimable (regression —
      confirms the soft/hard distinction is preserved, not accidentally widened).
  (d) add_sprint_item accepts blocker_kind='superseded'.
  (e) update_sprint_item can set blocker_kind='superseded' on an existing item.
  (f) clearing blocker_kind via update_sprint_item makes the item claimable again.
  (g) an invalid blocker_kind value is still rejected (enum still enforced).
  (h) MCP dispatch surfaces the SUPERSEDED block.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import server as server_module

# _dispatch_mcp_tool is re-exported from server (importing it directly from
# meridian.mcp.handler at module top-level triggers a circular import).
_dispatch_mcp_tool = server_module._dispatch_mcp_tool


# ---------------------------------------------------------------------------
# Enforcement — claim_sprint_item
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_superseded_item_is_refused(db):
    """(a) A sprint item with blocker_kind='superseded' cannot be claimed directly."""
    p = await db_module.create_project(db, "superseded-refuse")
    pid = p["id"]
    item = await db_module.add_sprint_item(
        db, pid, "v1", "old DuckDB FTS approach",
        blocker_kind="superseded",
    )
    await db_module.patch_sprint_item(
        db, pid, item["id"], notes="SUPERSEDED — see workspace proposal de33589b",
    )
    result = await db_module.claim_sprint_item(db, pid, item["id"])
    assert isinstance(result, dict)
    assert result.get("blocked") is True
    assert result.get("error") == "SUPERSEDED"
    assert "reason" in result
    assert "de33589b" in result["reason"], (
        "reason should surface the item's notes so the caller knows what superseded it"
    )


@pytest.mark.asyncio
async def test_claim_superseded_item_stays_pending(db):
    """(b) A refused claim leaves the item pending — no claimed_at, no status flip."""
    p = await db_module.create_project(db, "superseded-stays-pending")
    pid = p["id"]
    item = await db_module.add_sprint_item(
        db, pid, "v1", "old approach", blocker_kind="superseded",
    )
    await db_module.claim_sprint_item(db, pid, item["id"])
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["status"] != "in_progress"
    assert stored.get("claimed_at") is None


@pytest.mark.asyncio
async def test_claim_manual_item_still_succeeds_directly(db):
    """(c) regression — blocker_kind='manual' remains a SOFT (listing-only) gate;
    a direct claim_sprint_item(item_id=...) call still succeeds, unlike 'superseded'.
    """
    p = await db_module.create_project(db, "manual-still-claimable")
    pid = p["id"]
    item = await db_module.add_sprint_item(
        db, pid, "v1", "configure PyPI trusted publisher", blocker_kind="manual",
    )
    result = await db_module.claim_sprint_item(db, pid, item["id"])
    assert result.get("blocked") is not True
    assert result.get("status") == "in_progress"


# ---------------------------------------------------------------------------
# add_sprint_item / update_sprint_item plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_sprint_item_accepts_superseded(db):
    """(d) add_sprint_item validates 'superseded' as a legal blocker_kind."""
    p = await db_module.create_project(db, "add-superseded")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "replaced item", blocker_kind="superseded",
    )
    assert item.get("blocker_kind") == "superseded"


@pytest.mark.asyncio
async def test_update_sprint_item_sets_superseded(db):
    """(e) update_sprint_item can mark a previously-ordinary item superseded."""
    p = await db_module.create_project(db, "update-superseded")
    pid = p["id"]
    item = await db_module.add_sprint_item(db, pid, "v1", "in-flight approach")
    assert item.get("blocker_kind") is None

    updated = await db_module.patch_sprint_item(
        db, pid, item["id"], blocker_kind="superseded",
    )
    assert updated.get("blocker_kind") == "superseded"

    result = await db_module.claim_sprint_item(db, pid, item["id"])
    assert result.get("blocked") is True
    assert result.get("error") == "SUPERSEDED"


@pytest.mark.asyncio
async def test_clearing_superseded_makes_item_claimable_again(db):
    """(f) A human clearing blocker_kind via update_sprint_item unblocks the item."""
    p = await db_module.create_project(db, "clear-superseded")
    pid = p["id"]
    item = await db_module.add_sprint_item(
        db, pid, "v1", "reconsidered approach", blocker_kind="superseded",
    )
    blocked = await db_module.claim_sprint_item(db, pid, item["id"])
    assert blocked.get("error") == "SUPERSEDED"

    await db_module.patch_sprint_item(db, pid, item["id"], blocker_kind="")
    cleared = await db_module.get_sprint_item(db, item["id"])
    assert not cleared.get("blocker_kind")

    result = await db_module.claim_sprint_item(db, pid, item["id"])
    assert result.get("blocked") is not True
    assert result.get("status") == "in_progress"


@pytest.mark.asyncio
async def test_invalid_blocker_kind_still_rejected(db):
    """(g) the enum guard still rejects unknown values after adding 'superseded'."""
    p = await db_module.create_project(db, "invalid-blocker-kind")
    with pytest.raises(ValueError, match="blocker_kind"):
        await db_module.add_sprint_item(
            db, p["id"], "v1", "bogus blocker", blocker_kind="not-a-real-kind",
        )


# ---------------------------------------------------------------------------
# MCP dispatch surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_add_and_claim_superseded(db, tmp_path):
    """(h) add_sprint_item via MCP stores blocker_kind='superseded'; claim_sprint_item
    via MCP returns the blocked dict instead of claiming."""
    p = await db_module.create_project(db, "mcp-superseded")
    pid = p["id"]
    added = await _dispatch_mcp_tool(
        "add_sprint_item",
        {
            "project_id": pid,
            "version": "v1",
            "title": "superseded via MCP",
            "blocker_kind": "superseded",
            "force": True,
        },
        db,
        str(tmp_path),
    )
    assert added.get("blocker_kind") == "superseded"

    claimed = await _dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": added["id"]},
        db,
        str(tmp_path),
    )
    assert claimed.get("blocked") is True
    assert claimed.get("error") == "SUPERSEDED"
