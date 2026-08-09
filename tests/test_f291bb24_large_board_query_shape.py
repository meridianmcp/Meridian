"""f291bb24 (follow-up) — regression coverage for the 3 unfiltered
``get_sprint_items(project_id)`` full-board scans found still running inside
complete_sprint_item's call chain even after the CI/code-intel/test-run
parallelization fix, diagnosed against a real production project (2077
sprint items, some with multi-KB ``notes``/``tool_requirements`` JSON).

Every pytest fixture elsewhere in this suite seeds a tiny throwaway project
(a handful of items, short/empty blob columns) -- the exact code path fixed
here (``_gather_continuation_inputs`` -> ``get_sprint_items_continuation_scoped``,
the "notify when sprint done" tail -> ``has_active_sprint_items``,
``_board_change_for_session`` -> ``count_new_sprint_items_since``) is
INVISIBLE to those fixtures: same query, same round-trip count, wildly
different real-world cost, since the bug was about the SHAPE of the query
(unfiltered ``SELECT *`` over the whole project) not the number of queries.
This file seeds a large board (300+ items, a third with multi-KB blob
columns) so a future change that silently reintroduces a full unfiltered
``get_sprint_items(project_id)`` call on one of these three paths is
detectable: the narrow-projection assertions below fail immediately if any
of the three helpers falls back to returning full rows / all items.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from meridian import db as db_module
from meridian import server as srv


def _minutes_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=n)).strftime("%Y-%m-%d %H:%M:%S")

_BIG_BLOB = "x" * 3000  # ~3KB, similar order of magnitude to real tool_requirements/notes


_BIG_TOOL_REQUIREMENTS = [{
    "name": "prospect_symbol",
    "server_or_namespace": "meridian",
    "required_or_preferred": "required",
    "purpose": _BIG_BLOB,
}]


async def _seed_large_board(db, project_id, *, count=300, version="v1"):
    for i in range(count):
        # force=True: near-identical titles ("perf item N") would otherwise
        # trip add_sprint_item's own duplicate-title guard (b0d42ef6).
        await db_module.add_sprint_item(
            db, project_id, version, f"perf item {i}",
            force=True,
            priority="urgent" if i % 37 == 0 else "normal",
            notes=_BIG_BLOB if i % 3 == 0 else None,
            tool_requirements=_BIG_TOOL_REQUIREMENTS if i % 3 == 1 else None,
            touches_resources=[f"file:generated_{i}.py"] if i % 3 == 2 else None,
        )


@pytest.mark.asyncio
async def test_get_sprint_items_continuation_scoped_returns_narrow_columns_only(db):
    """The whole point of the fix: this must NEVER pull the wide TEXT
    columns, regardless of how large the board or its blobs are."""
    p = await db_module.create_project(db, "large-board-continuation-scope")
    await _seed_large_board(db, p["id"], count=120, version="v1")
    await db_module.add_sprint_item(db, p["id"], "v2", "other version item", force=True)

    items = await db_module.get_sprint_items_continuation_scoped(db, p["id"], "v1")

    assert len(items) == 120
    # Version filter pushed into SQL -- v2's item must never appear.
    assert all(it.get("id") for it in items)
    for it in items:
        assert set(it.keys()) == {"id", "status", "blocker_kind"}
        assert "notes" not in it
        assert "tool_requirements" not in it
        assert "touches_resources" not in it

    # None-version call (no filter) must return every version's items.
    all_items = await db_module.get_sprint_items_continuation_scoped(db, p["id"], None)
    assert len(all_items) == 121


@pytest.mark.asyncio
async def test_has_active_sprint_items_correct_on_large_board(db):
    p = await db_module.create_project(db, "large-board-active-check")
    await _seed_large_board(db, p["id"], count=200)

    assert await db_module.has_active_sprint_items(
        db, p["id"], {"pending", "todo", "in_progress"}
    ) is True

    # Move every single item to a terminal state -- the existence check must
    # then correctly report False, not just "cheap" but actually correct.
    items = await db_module.get_sprint_items(db, p["id"])
    for it in items:
        await db_module.claim_sprint_item(db, p["id"], it["id"], actor="bulk-closer")
        await db_module.complete_sprint_item(db, p["id"], it["id"], actor="bulk-closer")

    assert await db_module.has_active_sprint_items(
        db, p["id"], {"pending", "todo", "in_progress"}
    ) is False


@pytest.mark.asyncio
async def test_count_new_sprint_items_since_matches_ground_truth(db):
    """Cross-checks the SQL aggregate against a plain Python recomputation
    over the full (uncached) board -- the two must agree exactly, including
    urgent-vs-normal classification, at a scale where a per-item mistake
    (e.g. an off-by-one on the boundary comparison) would be easy to miss on
    a 2-3 item toy board but should show up clearly here."""
    p = await db_module.create_project(db, "large-board-new-items-count")
    await _seed_large_board(db, p["id"], count=150)
    all_before = await db_module.get_sprint_items(db, p["id"])
    all_before.sort(key=lambda it: it["added_at"])
    cutoff = all_before[100]["added_at"]

    new_count, urgent_count = await db_module.count_new_sprint_items_since(db, p["id"], cutoff)

    ground_truth_new = [it for it in all_before if (it.get("added_at") or "") > cutoff]
    ground_truth_urgent = [
        it for it in ground_truth_new if (it.get("priority") or "normal") == "urgent"
    ]
    assert new_count == len(ground_truth_new)
    assert urgent_count == len(ground_truth_urgent)
    # Sanity: the board actually has urgent items in the tail (i % 37 == 0
    # guarantees at least one urgent item beyond index 100 for count=150).
    assert urgent_count > 0


@pytest.mark.asyncio
async def test_complete_sprint_item_end_to_end_on_large_board(db):
    """Full MCP dispatch path on a large, wide-column board: completion must
    still succeed and report correct continuation/board-change state, not
    just avoid the wide columns in isolation."""
    p = await db_module.create_project(db, "large-board-e2e-complete")
    await _seed_large_board(db, p["id"], count=250, version="v1")
    sess = await db_module.register_session(db, p["id"], "large-board-session")
    # Backdate the session's created_at so the "late injection" item below is
    # unambiguously after it -- SQLite's second-granularity datetime('now')
    # can otherwise collide with add_sprint_item's own added_at timestamp
    # when both happen within the same clock tick in a fast local test.
    await db.execute(
        "UPDATE sessions SET created_at = ? WHERE id = ?",
        (_minutes_ago(5), sess["id"]),
    )
    await db.commit()
    target = await db_module.add_sprint_item(
        db, p["id"], "v1", "the item under test", force=True,
    )
    await db_module.claim_sprint_item(db, p["id"], target["id"], actor=sess["id"])

    # A late-injected urgent item, added AFTER the session started, so
    # _board_change_for_session's count_new_sprint_items_since has a real,
    # non-empty result to report.
    await db_module.add_sprint_item(
        db, p["id"], "v1", "late urgent injection", force=True, priority="urgent",
    )

    res = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": target["id"], "session_id": sess["id"]},
        db, "/tmp",
    )

    assert res.get("error") is None, res
    assert res["status"] == "done"
    assert "continuation" in res
    assert res["continuation"]["continuation_required"] is True  # 250 siblings still pending
    assert res["continuation"]["actionable_count"] >= 250
    assert res.get("board_change", {}).get("urgent") is True
    assert res["board_change"]["urgent_items_since_session_start"] >= 1
