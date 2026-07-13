"""Tests for 5a85a78f — manual-item exclusion from get_parallelizable_groups.

Covers the three-signal _is_manual_sprint_item predicate as applied inside
get_parallelizable_groups and confirms analyze_sprint (which delegates to
get_parallelizable_groups for its parallelism sub-report) inherits the fix.

Four signal cases:
  (a) milestone_type='human'  — NEW gap being fixed; was previously passed through
  (b) blocker_kind='manual'   — regression: already excluded, must stay excluded
  (c) MANUAL-tagged title     — new gap being fixed (same as milestone_type gap)
  (d) human_id only (no manual signal) — 943afe1e non-regression: must NOT exclude
"""
from __future__ import annotations

import pytest

from meridian import db as db_module


# ---------------------------------------------------------------------------
# Unit tests for the _is_manual_sprint_item helper itself
# ---------------------------------------------------------------------------

def test_is_manual_sprint_item_milestone_type_human():
    assert db_module._is_manual_sprint_item({"milestone_type": "human"}) is True


def test_is_manual_sprint_item_blocker_kind_manual():
    assert db_module._is_manual_sprint_item({"blocker_kind": "manual"}) is True


def test_is_manual_sprint_item_manual_title():
    assert db_module._is_manual_sprint_item({"title": "MANUAL apply to Anthropic Fellows"}) is True


def test_is_manual_sprint_item_manual_title_case_insensitive():
    assert db_module._is_manual_sprint_item({"title": "manual upload screenshot"}) is True


def test_is_manual_sprint_item_manual_title_with_leading_whitespace():
    """Title with leading whitespace before MANUAL is still detected (lstrip)."""
    assert db_module._is_manual_sprint_item({"title": "  MANUAL post blog post"}) is True


def test_is_manual_sprint_item_human_id_alone_is_not_manual():
    """943afe1e — human_id alone must NOT classify an item as manual."""
    assert db_module._is_manual_sprint_item({
        "title": "FIX: add retry logic",
        "human_id": "user-abc",
        "milestone_type": "task",
        "blocker_kind": None,
    }) is False


def test_is_manual_sprint_item_ordinary_bug_fix():
    assert db_module._is_manual_sprint_item({
        "title": "BUG: handle empty queue",
        "milestone_type": "task",
    }) is False


def test_is_manual_sprint_item_non_dict_returns_false():
    assert db_module._is_manual_sprint_item(None) is False  # type: ignore[arg-type]
    assert db_module._is_manual_sprint_item("string") is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Integration tests: get_parallelizable_groups
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parallelizable_groups_excludes_milestone_type_human(db):
    """(a) — milestone_type='human' item must NOT appear in eligible groups."""
    p = await db_module.create_project(db, "pg-human")
    human_item = await db_module.add_sprint_item(
        db, p["id"], "v1", "MANUAL apply to Anthropic Fellows Program",
        milestone_type="human",
    )
    normal_item = await db_module.add_sprint_item(
        db, p["id"], "v1", "FIX: add retry logic",
    )
    res = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    eligible_ids = {it["id"] for g in res["groups"] for it in g}
    assert human_item["id"] not in eligible_ids, (
        "milestone_type='human' item should be excluded from eligible groups"
    )
    assert normal_item["id"] in eligible_ids, (
        "ordinary item should still be eligible"
    )


@pytest.mark.asyncio
async def test_parallelizable_groups_excludes_blocker_kind_manual(db):
    """(b) regression — blocker_kind='manual' item still excluded (unchanged behavior)."""
    p = await db_module.create_project(db, "pg-blocker")
    manual_item = await db_module.add_sprint_item(
        db, p["id"], "v1", "configure PyPI trusted publisher",
        blocker_kind="manual",
    )
    normal_item = await db_module.add_sprint_item(
        db, p["id"], "v1", "FEAT: add export endpoint",
    )
    res = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    eligible_ids = {it["id"] for g in res["groups"] for it in g}
    assert manual_item["id"] not in eligible_ids, (
        "blocker_kind='manual' item should still be excluded (regression)"
    )
    assert normal_item["id"] in eligible_ids


@pytest.mark.asyncio
async def test_parallelizable_groups_excludes_manual_title(db):
    """(c) — MANUAL-tagged title item must NOT appear in eligible groups."""
    p = await db_module.create_project(db, "pg-title")
    manual_item = await db_module.add_sprint_item(
        db, p["id"], "v1", "MANUAL capture product screenshots",
    )
    normal_item = await db_module.add_sprint_item(
        db, p["id"], "v1", "BUG: fix null pointer in auth",
    )
    res = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    eligible_ids = {it["id"] for g in res["groups"] for it in g}
    assert manual_item["id"] not in eligible_ids, (
        "MANUAL-tagged title item should be excluded from eligible groups"
    )
    assert normal_item["id"] in eligible_ids


@pytest.mark.asyncio
async def test_parallelizable_groups_includes_human_id_only(db):
    """(d) 943afe1e non-regression — human_id alone must NOT exclude an item."""
    p = await db_module.create_project(db, "pg-humanid")
    s = await db_module.register_session(db, p["id"], "sess")
    assigned_item = await db_module.add_sprint_item(
        db, p["id"], "v1", "FIX: update migration script",
        human_id=s["id"],
        milestone_type="task",
    )
    res = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    eligible_ids = {it["id"] for g in res["groups"] for it in g}
    assert assigned_item["id"] in eligible_ids, (
        "item with human_id but no manual signal must remain eligible (943afe1e)"
    )


@pytest.mark.asyncio
async def test_parallelizable_groups_all_manual_signals_empty(db):
    """A project with only manual items → no eligible groups, zero eligible_count."""
    p = await db_module.create_project(db, "pg-all-manual")
    await db_module.add_sprint_item(
        db, p["id"], "v1", "MANUAL install binary on laptop",
    )
    await db_module.add_sprint_item(
        db, p["id"], "v1", "configure prod creds",
        milestone_type="human",
    )
    await db_module.add_sprint_item(
        db, p["id"], "v1", "apply for PyPI publisher",
        blocker_kind="manual",
    )
    res = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    assert res["eligible_count"] == 0
    assert res["groups"] == []


# ---------------------------------------------------------------------------
# Integration tests: analyze_sprint (delegates to get_parallelizable_groups)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_analyze_sprint_excludes_milestone_type_human_from_eligible(db):
    """analyze_sprint's parallelism sub-report must not count human items as eligible."""
    p = await db_module.create_project(db, "as-human")
    await db_module.add_sprint_item(
        db, p["id"], "v1", "MANUAL publish blog post",
        milestone_type="human",
    )
    normal_item = await db_module.add_sprint_item(
        db, p["id"], "v1", "FIX: retry logic",
    )
    brief = await db_module.analyze_sprint(db, p["id"], version="v1")
    # Only the normal item should be eligible.
    assert brief["parallelism"]["eligible_count"] == 1
    group_item_ids = {
        it["id"] for g in brief["parallelism"]["groups"] for it in g
    }
    assert normal_item["id"] in group_item_ids


@pytest.mark.asyncio
async def test_analyze_sprint_excludes_manual_title_from_eligible(db):
    """analyze_sprint's parallelism sub-report must not count MANUAL-titled items."""
    p = await db_module.create_project(db, "as-title")
    await db_module.add_sprint_item(
        db, p["id"], "v1", "MANUAL apply to Anthropic Fellows Program",
    )
    normal_item = await db_module.add_sprint_item(
        db, p["id"], "v1", "FEAT: add export endpoint",
    )
    brief = await db_module.analyze_sprint(db, p["id"], version="v1")
    assert brief["parallelism"]["eligible_count"] == 1
    group_item_ids = {
        it["id"] for g in brief["parallelism"]["groups"] for it in g
    }
    assert normal_item["id"] in group_item_ids


@pytest.mark.asyncio
async def test_analyze_sprint_includes_human_id_only_in_eligible(db):
    """943afe1e — analyze_sprint must not drop items just because they have human_id."""
    p = await db_module.create_project(db, "as-humanid")
    s = await db_module.register_session(db, p["id"], "sess")
    assigned_item = await db_module.add_sprint_item(
        db, p["id"], "v1", "BUG: fix auth redirect",
        human_id=s["id"],
        milestone_type="task",
    )
    brief = await db_module.analyze_sprint(db, p["id"], version="v1")
    assert brief["parallelism"]["eligible_count"] == 1
    group_item_ids = {
        it["id"] for g in brief["parallelism"]["groups"] for it in g
    }
    assert assigned_item["id"] in group_item_ids
