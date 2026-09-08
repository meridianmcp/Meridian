"""Tests for sprint item 943786c9 — make starter handoff preview counts and
dependency-chain scope unambiguous.

Planning item based on the confirmed 524e73e6 follow-up finding: in starter
mode, the compact preview can show only the first three pending items while
the executable handoff contains five. The full goal block was already
present (524e73e6's own ``<dependency_waves>``/``<executor_item_ids>`` tags
inside ``_build_quick_start_goal``), but the STARTER PREVIEW prose above it
(``_render_starter_handoff``) still used truncated titles and an unqualified
"Done: (none)" that read as an omission rather than a bounded preview.

This file covers what 943786c9 closes on top of that:

1. The preview header now states actual counts plainly: "N pending in this
   handoff scope; previewing M" — for both N > 3 (previously a parenthetical
   "top 3 of N" footnote) and N <= 3 (previously a bare "# Pending" heading
   with no count at all, which read as authoritative).
2. The completed-items line is now "Done in this handoff scope: ..." instead
   of an unqualified "Done: ...", so an empty scope reads as "nothing
   completed in this render's scope," not "nothing has ever shipped."
3. Whatever slice of the pending list IS previewed is split into an
   active/claimable dependency wave ("## Claimable now") and a downstream
   blocked wave ("## Blocked (downstream — waiting on a predecessor)") —
   sourced from the SAME ``frontier_ready``/``frontier_blocking_predecessors``
   annotation 83a7586d/524e73e6 already attach to every pending item inside
   ``_build_quick_start_goal``, so it can never disagree with the goal
   block's own ``<dependency_waves>`` tag.
4. A five-item linear dependency-chain fixture proving no pending item is
   silently omitted: all 5 ids are still discoverable somewhere in the
   rendered starter content (via quick_start_goal's full batch), even though
   only 3 are previewed, and the header honestly says so.
5. A resource-conflict-only (no dependency) fixture proving this new
   wave-split is never confused with resource-conflict batching (524e73e6's
   own distinction, re-confirmed at the starter-preview layer specifically).
"""

from __future__ import annotations

import re

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


def _extract_executor_item_ids(text: str) -> list[str]:
    m = re.search(
        r'<executor_item_ids count="\d+">([^<]*)</executor_item_ids>', text,
    )
    assert m is not None, "expected an <executor_item_ids> tag in the goal block"
    return [i for i in m.group(1).split(",") if i]


# ---------------------------------------------------------------------------
# 1 & 2. Header wording — actual counts, "in this handoff scope" labeling.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_header_states_actual_counts_when_more_than_three_pending(
    db, tmp_path,
):
    p = await db_module.create_project(db, "943786c9-counts-more-than-3")
    for i in range(5):
        await db_module.add_sprint_item(
            db, p["id"], "v1", f"independent item {i}", force=True,
        )
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter",
    )
    assert "5 pending in this handoff scope; previewing 3" in content


@pytest.mark.asyncio
async def test_preview_header_states_actual_counts_when_three_or_fewer_pending(
    db, tmp_path,
):
    p = await db_module.create_project(db, "943786c9-counts-le-3")
    await db_module.add_sprint_item(db, p["id"], "v1", "only item")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter",
    )
    # Previously a bare "# Pending" heading with NO count at all — now always
    # states both numbers, even when they're equal.
    assert "1 pending in this handoff scope; previewing 1" in content


@pytest.mark.asyncio
async def test_preview_header_states_zero_when_no_pending_items(db, tmp_path):
    p = await db_module.create_project(db, "943786c9-counts-zero")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter",
    )
    assert "0 pending in this handoff scope; previewing 0" in content


@pytest.mark.asyncio
async def test_done_label_is_scoped_when_completed_items_exist(db, tmp_path):
    p = await db_module.create_project(db, "943786c9-done-scoped-nonempty")
    it1 = await db_module.add_sprint_item(db, p["id"], "v1", "shipped item")
    await db_module.complete_sprint_item(db, p["id"], it1["id"])
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter",
    )
    assert "Done in this handoff scope: shipped item" in content
    assert "\nDone:" not in content  # the old, unqualified label is fully gone


@pytest.mark.asyncio
async def test_done_label_is_scoped_when_no_completed_items(db, tmp_path):
    p = await db_module.create_project(db, "943786c9-done-scoped-empty")
    await db_module.add_sprint_item(db, p["id"], "v1", "an item")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter",
    )
    assert "Done in this handoff scope: (none)" in content


# ---------------------------------------------------------------------------
# 3 & 4. Active/claimable wave shown separately from downstream blocked
# waves, plus the five-item dependency-chain no-silent-omission fixture.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_five_item_dependency_chain_no_pending_item_silently_omitted(
    db, tmp_path,
):
    """Linear chain item1 -> item2 -> item3 -> item4 -> item5 (each depends on
    the previous). Only item1 is claimable right now; items 2-5 are all
    downstream-blocked. The starter preview only shows 3, but every one of
    the 5 ids must still be discoverable in the rendered content (via
    quick_start_goal's full batch) — none silently omitted."""
    p = await db_module.create_project(db, "943786c9-five-item-chain")
    items = []
    prev_id = None
    for i in range(5):
        it = await db_module.add_sprint_item(
            db, p["id"], "v1", f"chain item {i}",
            depends_on=prev_id, force=True,
        )
        items.append(it)
        prev_id = it["id"]

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter",
    )

    # Honest scope header.
    assert "5 pending in this handoff scope; previewing 3" in content

    # No pending item silently omitted: every id appears SOMEWHERE in the
    # rendered content (the full batch inside quick_start_goal), and the
    # goal block's own <executor_item_ids> manifest lists all 5 in order.
    for it in items:
        assert it["id"] in content, f"{it['id']} missing from starter content entirely"
    executor_ids = _extract_executor_item_ids(content)
    assert executor_ids == [it["id"] for it in items]

    # Wave separation: item 0 (root) is the active/claimable wave; items 1
    # and 2 (the next two in topological order, i.e. what's actually
    # previewed) are downstream-blocked on their own immediate predecessor.
    assert "## Claimable now" in content
    assert "## Blocked (downstream" in content
    claimable_idx = content.index("## Claimable now")
    blocked_idx = content.index("## Blocked (downstream")
    assert claimable_idx < blocked_idx, "claimable wave must render before blocked wave"

    claimable_section = content[claimable_idx:blocked_idx]
    assert items[0]["id"][:8] in claimable_section
    assert items[1]["id"][:8] not in claimable_section
    assert items[2]["id"][:8] not in claimable_section

    # The blocked section runs from its own header to the next blank-line-
    # delimited section (diagnostics / the /goal block) — just slice to the
    # goal block's own start for a clean bound.
    goal_idx = content.index("/goal") if "/goal" in content else len(content)
    blocked_section = content[blocked_idx:goal_idx]
    assert items[1]["id"][:8] in blocked_section
    assert items[2]["id"][:8] in blocked_section
    assert f"blocked on {items[0]['id'][:8]}" in blocked_section
    assert f"blocked on {items[1]['id'][:8]}" in blocked_section


@pytest.mark.asyncio
async def test_flat_board_never_renders_wave_headers(db, tmp_path):
    """No depends_on anywhere -> every previewed item is frontier_ready ->
    the preview stays the plain numbered list, with neither wave header
    rendered (byte-for-byte-equivalent-in-spirit to the pre-943786c9 shape
    for the common, dependency-free case)."""
    p = await db_module.create_project(db, "943786c9-flat-board")
    for i in range(2):
        await db_module.add_sprint_item(db, p["id"], "v1", f"flat item {i}", force=True)
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter",
    )
    assert "## Claimable now" not in content
    assert "## Blocked (downstream" not in content


@pytest.mark.asyncio
async def test_resource_conflict_alone_never_triggers_wave_split(db, tmp_path):
    """524e73e6 already proved resource-conflict batching (touches_resources
    overlap) is a DISTINCT concept from a real dependency boundary at the
    quick_start_goal/<dependency_waves> layer; this re-confirms the same
    distinction at the starter-preview wave-split layer specifically — two
    items that merely conflict on a resource (no depends_on between them)
    must never be rendered as one claimable + one downstream-blocked."""
    p = await db_module.create_project(db, "943786c9-resource-conflict-only")
    await db_module.add_sprint_item(
        db, p["id"], "v1", "resource item one",
        touches_resources=["file:shared.py"], prospect_bypass=True,
    )
    await db_module.add_sprint_item(
        db, p["id"], "v1", "resource item two (conflicts with one)",
        touches_resources=["file:shared.py"], prospect_bypass=True, force=True,
    )
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter",
    )
    assert "2 pending in this handoff scope; previewing 2" in content
    assert "## Claimable now" not in content
    assert "## Blocked (downstream" not in content


@pytest.mark.asyncio
async def test_fan_in_join_item_rendered_as_blocked_in_preview(db, tmp_path):
    """A fan-in item (depends on BOTH predecessors) that lands inside the
    top-3 preview window must be labeled blocked, naming both blockers."""
    p = await db_module.create_project(db, "943786c9-fan-in-preview")
    p1 = await db_module.add_sprint_item(db, p["id"], "v1", "predecessor one", force=True)
    p2 = await db_module.add_sprint_item(db, p["id"], "v1", "predecessor two", force=True)
    join = await db_module.add_sprint_item(
        db, p["id"], "v1", "join item",
        depends_on=f'["{p1["id"]}", "{p2["id"]}"]',
    )
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter",
    )
    assert "3 pending in this handoff scope; previewing 3" in content
    assert "## Claimable now" in content
    assert "## Blocked (downstream" in content
    blocked_idx = content.index("## Blocked (downstream")
    goal_idx = content.index("/goal") if "/goal" in content else len(content)
    blocked_section = content[blocked_idx:goal_idx]
    assert join["id"][:8] in blocked_section
    assert p1["id"][:8] in blocked_section
    assert p2["id"][:8] in blocked_section
