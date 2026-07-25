"""Regression tests for bounded workspace-proposal pagination (abde3b36)."""

from __future__ import annotations

import pytest

import meridian.db as db_module
from meridian.mcp.handlers import notes_decisions as notes_decisions_handler
from meridian.mcp_tools import _MCP_TOOLS_LIST


async def _seed_proposals(
    db,
    count: int,
    *,
    tenant_id: str | None = None,
) -> None:
    """Create proposals with deterministic, strictly increasing timestamps."""
    for index in range(count):
        proposal = await db_module.add_workspace_proposal(
            db,
            f"proposal-{index}",
            f"body-{index}",
            tenant_id=tenant_id,
        )
        timestamp = (
            f"2026-01-01T{index // 3600:02d}:"
            f"{(index // 60) % 60:02d}:{index % 60:02d}.000000+00:00"
        )
        await db.execute(
            "UPDATE workspace_proposals SET created_at = ? WHERE id = ?",
            (timestamp, proposal["id"]),
        )


@pytest.mark.asyncio
async def test_get_workspace_proposals_is_bounded_and_pages_newest_first(db):
    await _seed_proposals(db, 105)

    default_page = await db_module.get_workspace_proposals(db)
    assert len(default_page) == 20
    assert [row["title"] for row in default_page] == [
        f"proposal-{index}" for index in range(104, 84, -1)
    ]

    first_page = await db_module.get_workspace_proposals(db, limit=7, offset=0)
    second_page = await db_module.get_workspace_proposals(db, limit=7, offset=7)
    assert [row["title"] for row in first_page] == [
        f"proposal-{index}" for index in range(104, 97, -1)
    ]
    assert [row["title"] for row in second_page] == [
        f"proposal-{index}" for index in range(97, 90, -1)
    ]
    assert {row["id"] for row in first_page}.isdisjoint(
        {row["id"] for row in second_page}
    )

    assert len(await db_module.get_workspace_proposals(db, limit=10_000)) == 100
    clamped = await db_module.get_workspace_proposals(db, limit=0, offset=-8)
    assert [row["title"] for row in clamped] == ["proposal-104"]


@pytest.mark.asyncio
async def test_get_workspace_proposals_keeps_positional_filters_compatible(db):
    await _seed_proposals(db, 8)

    page = await db_module.get_workspace_proposals(db, "raw", None, None, 3, 2)

    assert [row["title"] for row in page] == [
        "proposal-5",
        "proposal-4",
        "proposal-3",
    ]


@pytest.mark.asyncio
async def test_get_workspace_proposals_handler_forwards_limit_and_offset(db):
    await _seed_proposals(db, 10, tenant_id="tenant-page")

    page = await notes_decisions_handler.handle_get_workspace_proposals(
        {"status": "raw", "limit": 4, "offset": 3},
        db,
        ".",
        None,
        "tenant-page",
    )

    assert [row["title"] for row in page] == [
        "proposal-6",
        "proposal-5",
        "proposal-4",
        "proposal-3",
    ]


def test_get_workspace_proposals_schema_exposes_bounded_pagination():
    tool = next(
        item for item in _MCP_TOOLS_LIST
        if item["name"] == "get_workspace_proposals"
    )
    properties = tool["inputSchema"]["properties"]

    assert properties["limit"]["minimum"] == 1
    assert properties["limit"]["maximum"] == 100
    assert properties["offset"]["minimum"] == 0
