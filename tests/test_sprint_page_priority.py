"""The paginated sprint list must rank by priority, the same as the full list.

get_sprint_items ranks urgent > high > normal > low, then oldest first. get_sprint_items_page
ordered by added_at alone, so a paged read showed old normal items ahead of newer urgent ones.
These tests pin the paged order to the full order, including across page boundaries.

Titles share no words on purpose: add_sprint_item refuses near-duplicate titles (>=60% word
overlap), which would drop an item from the seed and hide the ordering being tested.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module

EXPECTED_ORDER = [
    "Restore backups",   # urgent
    "Fix tooltip",       # high
    "Refactor parser",   # normal, added first
    "Patch login",       # normal, added second
    "Rotate logs",       # low
]


async def _seed_mixed_priorities(db):
    project = await db_module.create_project(db, "priority-page")
    pid = project["id"]
    await db_module.add_sprint_item(db, pid, "v0.2.x", "Refactor parser", priority="normal")
    await db_module.add_sprint_item(db, pid, "v0.2.x", "Rotate logs", priority="low")
    await db_module.add_sprint_item(db, pid, "v0.2.x", "Patch login", priority="normal")
    await db_module.add_sprint_item(db, pid, "v0.2.x", "Fix tooltip", priority="high")
    await db_module.add_sprint_item(db, pid, "v0.2.x", "Restore backups", priority="urgent")
    return pid


@pytest.mark.asyncio
async def test_paged_list_ranks_urgent_and_high_before_older_normal_items(db):
    pid = await _seed_mixed_priorities(db)
    items, total = await db_module.get_sprint_items_page(db, pid, status="pending", limit=10)
    assert total == 5
    assert [it["title"] for it in items] == EXPECTED_ORDER


@pytest.mark.asyncio
async def test_priority_order_holds_across_page_boundaries(db):
    pid = await _seed_mixed_priorities(db)
    seen: list[str] = []
    for offset in range(5):
        items, _ = await db_module.get_sprint_items_page(
            db, pid, status="pending", limit=1, offset=offset
        )
        seen.extend(it["title"] for it in items)
    assert seen == EXPECTED_ORDER


@pytest.mark.asyncio
async def test_paged_order_matches_the_full_list(db):
    pid = await _seed_mixed_priorities(db)
    full = await db_module.get_sprint_items(db, pid, status="pending")
    paged, _ = await db_module.get_sprint_items_page(db, pid, status="pending", limit=50)
    assert [it["id"] for it in paged] == [it["id"] for it in full]
    assert [it["title"] for it in full] == EXPECTED_ORDER
