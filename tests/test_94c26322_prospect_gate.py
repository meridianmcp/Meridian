"""94c26322 — tests for the prospecting safety gate.

Three surfaces are tested:
1. _build_quick_start_goal excludes unprospected items (no evidence, no bypass)
   and emits a visible <excluded_unprospected> tag in the /goal output.
2. _build_quick_start_goal includes unprospected items when prospect_bypass=True.
3. claim_sprint_item returns a structured UNPROSPECTED blocked dict for
   unprospected items, mirroring the DEFERRED gate (dec69708).
4. claim_sprint_item allows the claim when prospect_bypass=True.
5. patch_sprint_item wires through the prospect_bypass field.
6. Migration: sprint_items gains a prospect_bypass column defaulting to 0.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from typing import Any

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import meridian.db as db_module
from meridian.handoff import _build_quick_start_goal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_item(
    iid: str,
    title: str,
    *,
    prospect_status: str | None = None,
    code_pointers: list | None = None,
    pointers: list | None = None,
    prospect_bypass: bool = False,
    version: str = "v1",
    status: str = "pending",
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": iid,
        "version": version,
        "status": status,
        "title": title,
        "prospect_bypass": 1 if prospect_bypass else 0,
    }
    if prospect_status is not None:
        d["prospect_status"] = prospect_status
    if code_pointers is not None:
        d["code_pointers"] = code_pointers
    if pointers is not None:
        d["pointers"] = pointers
    return d


def _run(coro: Any) -> Any:
    # asyncio.run() (not get_event_loop().run_until_complete()) — the latter can
    # return a stale/closed loop when this file shares an xdist worker process
    # with other pytest-asyncio-managed test files, leaving dangling
    # "coroutine was never awaited" warnings to surface in unrelated tests.
    return asyncio.run(coro)


async def _make_db() -> Any:
    tmp = tempfile.mktemp(suffix=".db")
    return await db_module.init_db(tmp)


# ---------------------------------------------------------------------------
# 1. Goal gate: unprospected items excluded + visible tag
# ---------------------------------------------------------------------------

def test_goal_gate_excludes_unprospected_item():
    """An item that went through enrichment and got no_match (no evidence, no bypass)
    must be excluded from the /goal's claimable ids but appears in the excluded tag.

    94c26322 — the gate fires for items where enrichment ran and explicitly found
    nothing (prospect_status in no_match/error/no_query), not for plain DB rows
    that simply haven't been enriched yet.
    """
    items = [
        # Enriched and found no match — this is the primary gap the gate closes.
        _make_item("id-no-evidence", "Fix login", prospect_status="no_match"),
    ]
    goal = _build_quick_start_goal(items)
    # Must appear in the exclusion tag (so it's visible to a human reviewing the /goal)
    assert '<excluded_unprospected' in goal
    assert 'count="1"' in goal
    assert "id-no-evidence" in goal  # appears in the exclusion tag
    # Must NOT appear in the claimable sprint_items section
    items_clause = goal.split('<sprint_items>')[1].split('</sprint_items>')[0] if '<sprint_items>' in goal else ""
    assert "id-no-evidence" not in items_clause


def test_goal_gate_includes_prospected_item():
    """A prospected item (prospect_status='prospected') IS included."""
    items = [
        _make_item("id-good", "Fix auth", prospect_status="prospected",
                   code_pointers=[{"file": "auth.py"}]),
    ]
    goal = _build_quick_start_goal(items)
    assert "id-good" in goal
    assert '<excluded_unprospected' not in goal


def test_goal_gate_includes_cached_item():
    """A cached item (prior pointer reused) IS included."""
    items = [
        _make_item("id-cached", "Update routes", prospect_status="cached",
                   code_pointers=[{"file": "routes.py"}]),
    ]
    goal = _build_quick_start_goal(items)
    assert "id-cached" in goal
    assert '<excluded_unprospected' not in goal


def test_goal_gate_includes_item_with_code_pointers_no_status():
    """An item with code_pointers but no prospect_status IS included (evidence exists)."""
    items = [
        _make_item("id-ptr", "Refactor DB", code_pointers=[{"file": "db.py"}]),
    ]
    goal = _build_quick_start_goal(items)
    assert "id-ptr" in goal
    assert '<excluded_unprospected' not in goal


def test_goal_gate_includes_item_with_generic_pointers():
    """An item with pointers (docs/generic) but no code_pointers IS included."""
    items = [
        _make_item("id-gen", "Write docs", pointers=[{"uri": "doc://x"}]),
    ]
    goal = _build_quick_start_goal(items)
    assert "id-gen" in goal
    assert '<excluded_unprospected' not in goal


def test_goal_gate_bypassed_with_prospect_bypass():
    """An item with no_match status AND prospect_bypass=True IS included (bypass wins)."""
    items = [
        # Enriched, got no_match, BUT human explicitly bypassed it — should be included.
        _make_item("id-bypass", "Unusual task", prospect_status="no_match",
                   prospect_bypass=True),
    ]
    goal = _build_quick_start_goal(items)
    assert "id-bypass" in goal
    assert '<excluded_unprospected' not in goal


def test_goal_gate_mixed_items_selective_exclusion():
    """Mixed list: only enrichment-failed, non-bypassed items are excluded."""
    items = [
        # Enriched, got no_match → excluded (this is the gap the gate closes)
        _make_item("id-excl", "No evidence", prospect_status="no_match"),
        _make_item("id-incl", "Has evidence",           # included (prospected + code_pointers)
                   prospect_status="prospected",
                   code_pointers=[{"file": "db.py"}]),
        _make_item("id-bypass", "Bypassed",             # included via bypass
                   prospect_bypass=True),
        _make_item("id-skip", "Review logs",            # included (skipped_cap is deliberate)
                   prospect_status="skipped_cap"),      # skipped_cap = NOT unprospected
    ]
    goal = _build_quick_start_goal(items)
    # Claimable items section must include id-incl, id-bypass, id-skip
    items_clause = goal.split('<sprint_items>')[1].split('</sprint_items>')[0] if '<sprint_items>' in goal else ""
    assert "id-excl" not in items_clause, "id-excl must be excluded from claimable list"
    assert "id-incl" in items_clause, "id-incl (prospected) must be included"
    assert "id-bypass" in items_clause, "id-bypass (bypass set) must be included"
    assert "id-skip" in items_clause, "id-skip (skipped_cap) must be included"
    # Only id-excl was excluded
    assert 'count="1"' in goal
    # id-excl must appear in the exclusion tag
    exc_section = goal.split('<excluded_unprospected')[1].split('</excluded_unprospected>')[0] if '<excluded_unprospected' in goal else ""
    assert "id-excl" in exc_section


def test_goal_gate_excluded_tag_contains_item_ids():
    """The <excluded_unprospected> tag must list the excluded item ids."""
    items = [
        # Both enriched and got explicit failure status → excluded
        _make_item("aaa-111", "Item A", prospect_status="no_match"),
        _make_item("bbb-222", "Item B", prospect_status="no_query"),
    ]
    goal = _build_quick_start_goal(items)
    # Both excluded: tag should contain both ids
    assert "aaa-111" in goal
    assert "bbb-222" in goal
    # Both must appear in the exclusion tag region
    exc_section = goal.split('<excluded_unprospected')[1].split('</excluded_unprospected>')[0]
    assert "aaa-111" in exc_section
    assert "bbb-222" in exc_section


def test_goal_gate_no_excluded_tag_when_none_excluded():
    """No <excluded_unprospected> tag is emitted when all items have evidence."""
    items = [
        _make_item("id-ok", "Prospected", prospect_status="prospected",
                   code_pointers=[{"file": "server.py"}]),
    ]
    goal = _build_quick_start_goal(items)
    assert '<excluded_unprospected' not in goal


def test_goal_gate_empty_board_after_exclusion():
    """When all items are excluded, the empty-board branch is returned WITH the tag."""
    items = [
        # Both enriched with explicit failure → excluded
        _make_item("id-x1", "Item X1", prospect_status="no_match"),
        _make_item("id-x2", "Item X2", prospect_status="error"),
    ]
    goal = _build_quick_start_goal(items)
    # The exclusion tag must be present with both ids
    assert '<excluded_unprospected count="2">' in goal
    # Item ids must appear ONLY in the exclusion region (not in any claimable list)
    assert '<sprint_items>' not in goal, "No claimable items section when all are excluded"
    # Both ids appear in the exclusion tag
    exc_section = goal.split('<excluded_unprospected')[1].split('</excluded_unprospected>')[0]
    assert "id-x1" in exc_section
    assert "id-x2" in exc_section


def test_goal_gate_no_match_status_is_unprospected():
    """prospect_status='no_match' has no evidence — must be excluded."""
    items = [
        _make_item("id-nm", "No match", prospect_status="no_match"),
    ]
    goal = _build_quick_start_goal(items)
    assert '<excluded_unprospected' in goal
    # Must not appear in any claimable items section
    assert '<sprint_items>' not in goal
    # Must appear in the exclusion tag
    exc_section = goal.split('<excluded_unprospected')[1].split('</excluded_unprospected>')[0]
    assert "id-nm" in exc_section


def test_goal_gate_skipped_manual_not_excluded():
    """Items with skipped_manual prospect_status are intentional skips — NOT excluded."""
    items = [
        _make_item("id-sm", "Talk to advisor", prospect_status="skipped_manual"),
    ]
    goal = _build_quick_start_goal(items)
    # skipped_manual is not flagged as unprospected, so it passes the gate
    assert '<excluded_unprospected' not in goal


# ---------------------------------------------------------------------------
# 2. claim_sprint_item gate: unprospected => blocked dict
# ---------------------------------------------------------------------------

def test_claim_blocks_unprospected_item():
    """claim_sprint_item returns a blocked dict for an item that DECLARED
    real code-touching resources but has no durable pointer evidence.

    An item with NO declared touches_resources was never a prospecting
    candidate in the first place (nothing for add_sprint_item's inline
    prospecting to attempt) and is intentionally NOT gated — see
    test_claim_allows_item_without_declared_resources below.
    """
    async def run():
        db = await _make_db()
        proj = await db_module.create_project(db, "Test Project")
        pid = proj["id"]
        item = await db_module.add_sprint_item(
            db, pid, "v1", "Unprospected task",
            touches_resources=["file:meridian/nonexistent_module_xyz.py:no_such_symbol"],
        )
        iid = item["id"]
        # Item declared a resource but has no code_pointers / pointers -> unprospected
        result = await db_module.claim_sprint_item(db, pid, iid)
        assert isinstance(result, dict)
        assert result.get("blocked") is True
        assert result.get("error") == "UNPROSPECTED"
        assert "prospect_bypass" in result.get("reason", "")
        # Item must still be pending (not claimed)
        refetched = await db_module.get_sprint_item(db, iid)
        assert refetched["status"] == "pending"
        await db.close()

    _run(run())


def test_claim_allows_item_without_declared_resources():
    """claim_sprint_item does NOT gate an item that declared no touches_resources.

    Such an item was never a prospecting candidate in the first place (manual
    tasks, proposals, docs-only work, or anything filed without explicit
    file/route/tool targets) -- gating it would block the overwhelming
    majority of ordinary items, not just genuinely-risky unprospected ones.
    """
    async def run():
        db = await _make_db()
        proj = await db_module.create_project(db, "Test Project")
        pid = proj["id"]
        item = await db_module.add_sprint_item(db, pid, "v1", "No resources declared")
        iid = item["id"]
        result = await db_module.claim_sprint_item(db, pid, iid)
        assert isinstance(result, dict)
        assert not result.get("blocked")
        assert result.get("status") == "in_progress"
        await db.close()

    _run(run())


def test_claim_allows_bypassed_unprospected_item():
    """claim_sprint_item succeeds when prospect_bypass=True even without evidence."""
    async def run():
        db = await _make_db()
        proj = await db_module.create_project(db, "Test Project")
        pid = proj["id"]
        item = await db_module.add_sprint_item(db, pid, "v1", "Bypassed task")
        iid = item["id"]
        # Set the bypass flag (human override)
        await db_module.patch_sprint_item(db, pid, iid, prospect_bypass=True)
        result = await db_module.claim_sprint_item(db, pid, iid)
        assert isinstance(result, dict)
        # Must NOT be a blocked dict
        assert not result.get("blocked")
        assert result.get("status") == "in_progress"
        await db.close()

    _run(run())


def test_claim_allows_item_with_durable_pointer():
    """claim_sprint_item succeeds for an item with a durable sprint_item_pointer."""
    async def run():
        db = await _make_db()
        proj = await db_module.create_project(db, "Test Project")
        pid = proj["id"]
        item = await db_module.add_sprint_item(db, pid, "v1", "Durable pointer task",
                                               touches_resources=["file:meridian/server.py"])
        iid = item["id"]
        # Add a durable pointer (this is the durable evidence the claim gate checks).
        await db_module.add_sprint_item_pointer(
            db, pid, iid, "code",
            [{"uri": "file:meridian/server.py", "selector": {"type": "range",
              "start_line": 1, "end_line": 10}}],
        )
        result = await db_module.claim_sprint_item(db, pid, iid)
        assert isinstance(result, dict)
        assert not result.get("blocked")
        assert result.get("status") == "in_progress"
        await db.close()

    _run(run())


# ---------------------------------------------------------------------------
# 3. patch_sprint_item wires prospect_bypass through
# ---------------------------------------------------------------------------

def test_patch_sprint_item_sets_prospect_bypass():
    """patch_sprint_item correctly writes prospect_bypass to the DB."""
    async def run():
        db = await _make_db()
        proj = await db_module.create_project(db, "Test Project")
        pid = proj["id"]
        item = await db_module.add_sprint_item(db, pid, "v1", "Test item")
        iid = item["id"]
        # Default: bypass is 0
        refetched = await db_module.get_sprint_item(db, iid)
        assert int(refetched.get("prospect_bypass") or 0) == 0
        # Set to True
        updated = await db_module.patch_sprint_item(db, pid, iid, prospect_bypass=True)
        assert int(updated.get("prospect_bypass") or 0) == 1
        # Clear back to False
        cleared = await db_module.patch_sprint_item(db, pid, iid, prospect_bypass=False)
        assert int(cleared.get("prospect_bypass") or 0) == 0
        await db.close()

    _run(run())


def test_patch_sprint_item_unset_leaves_prospect_bypass_unchanged():
    """Omitting prospect_bypass from patch_sprint_item leaves the stored value untouched."""
    async def run():
        db = await _make_db()
        proj = await db_module.create_project(db, "Test Project")
        pid = proj["id"]
        item = await db_module.add_sprint_item(db, pid, "v1", "Test item")
        iid = item["id"]
        # Set to True
        await db_module.patch_sprint_item(db, pid, iid, prospect_bypass=True)
        # Update something else (title) — prospect_bypass must not be touched
        updated = await db_module.patch_sprint_item(db, pid, iid, title="New title")
        assert int(updated.get("prospect_bypass") or 0) == 1
        await db.close()

    _run(run())


# ---------------------------------------------------------------------------
# 4. Migration: prospect_bypass column exists with correct default
# ---------------------------------------------------------------------------

def test_migration_prospect_bypass_column_exists():
    """After init_db, sprint_items has a prospect_bypass column defaulting to 0."""
    async def run():
        db = await _make_db()
        proj = await db_module.create_project(db, "Test Project")
        pid = proj["id"]
        item = await db_module.add_sprint_item(db, pid, "v1", "Check column")
        iid = item["id"]
        fetched = await db_module.get_sprint_item(db, iid)
        # Column must exist (not raise KeyError, not be absent)
        assert "prospect_bypass" in fetched
        assert int(fetched["prospect_bypass"] or 0) == 0
        await db.close()

    _run(run())
