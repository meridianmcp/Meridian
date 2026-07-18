"""08c355c2 — staleness detection for the legacy goal_states.sprint free-text field.

generate_handoff was surfacing months-old sprint text verbatim (confirmed live on
Camerer_MS_Graduation_2026), equal weight to current data, because nothing clears
or supersedes goal.sprint once sprint-item tracking takes over.

Fix: _sprint_stale_days() detects when the field is stale (>= 30 days old and the
project has sprint items), and both the readiness block and the L1 template section
demote/warn about the stale text rather than showing it at equal weight.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Pure helper: _sprint_stale_days
# ---------------------------------------------------------------------------

def test_sprint_stale_days_none_when_no_items():
    """If the project has no sprint items, the free-text sprint field is still
    the only signal — never demote it regardless of age."""
    old_ts = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d %H:%M:%S")
    goal = {"sprint": "old sprint text", "sprint_updated_at": old_ts}
    assert handoff_module._sprint_stale_days(goal, has_sprint_items=False) is None


def test_sprint_stale_days_none_when_sprint_empty():
    """Empty/None sprint field — nothing to demote."""
    old_ts = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d %H:%M:%S")
    goal_none = {"sprint": None, "sprint_updated_at": old_ts}
    goal_empty = {"sprint": "", "sprint_updated_at": old_ts}
    assert handoff_module._sprint_stale_days(goal_none, has_sprint_items=True) is None
    assert handoff_module._sprint_stale_days(goal_empty, has_sprint_items=True) is None


def test_sprint_stale_days_none_when_no_timestamp():
    """Pre-migration goal row without sprint_updated_at: fail-safe, don't demote."""
    goal = {"sprint": "old sprint text"}  # no sprint_updated_at
    assert handoff_module._sprint_stale_days(goal, has_sprint_items=True) is None


def test_sprint_stale_days_none_when_fresh():
    """Sprint field updated recently — within the 30-day window — not stale."""
    fresh_ts = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    goal = {"sprint": "current sprint", "sprint_updated_at": fresh_ts}
    assert handoff_module._sprint_stale_days(goal, has_sprint_items=True) is None


def test_sprint_stale_days_returns_days_when_old():
    """Sprint field old enough (>= 30 days) with items → returns the age in days."""
    old_ts = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d %H:%M:%S")
    goal = {"sprint": "v0.1 sprint", "sprint_updated_at": old_ts}
    result = handoff_module._sprint_stale_days(goal, has_sprint_items=True)
    assert result is not None
    assert result >= 45


def test_sprint_stale_days_exactly_at_threshold():
    """Exactly 30 days old — on the boundary, should be treated as stale."""
    threshold_ts = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    goal = {"sprint": "sprint text", "sprint_updated_at": threshold_ts}
    result = handoff_module._sprint_stale_days(goal, has_sprint_items=True)
    assert result is not None
    assert result >= 30


def test_sprint_stale_days_29_days_not_stale():
    """29 days old — just within the fresh window, must NOT be demoted."""
    recent_ts = (datetime.now() - timedelta(days=29)).strftime("%Y-%m-%d %H:%M:%S")
    goal = {"sprint": "sprint text", "sprint_updated_at": recent_ts}
    assert handoff_module._sprint_stale_days(goal, has_sprint_items=True) is None


def test_sprint_stale_days_falls_back_to_updated_at():
    """When sprint_updated_at is absent, falls back to updated_at for the age check."""
    old_ts = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
    goal = {"sprint": "v0.1 sprint", "updated_at": old_ts}  # no sprint_updated_at
    result = handoff_module._sprint_stale_days(goal, has_sprint_items=True)
    assert result is not None
    assert result >= 60


def test_sprint_stale_days_none_goal():
    """None goal dict — graceful no-op."""
    assert handoff_module._sprint_stale_days(None, has_sprint_items=True) is None


def test_sprint_stale_days_unparseable_timestamp():
    """Unparseable timestamp — fail-safe: don't demote."""
    goal = {"sprint": "sprint text", "sprint_updated_at": "not-a-date"}
    assert handoff_module._sprint_stale_days(goal, has_sprint_items=True) is None


# ---------------------------------------------------------------------------
# _build_readiness_block with sprint_stale_days
# ---------------------------------------------------------------------------

def test_readiness_block_stale_sprint_shows_warning():
    """When sprint_stale_days is set, the readiness block carries the age warning
    instead of the plain 'Sprint: ...' line."""
    block = handoff_module._build_readiness_block(
        sprint="old sprint from months ago",
        pending_count=3,
        decisions_count=2,
        sprint_stale_days=90,
    )
    assert "STALE" in block
    assert "90d old" in block
    assert "superseded by sprint items" in block
    assert "old sprint from months ago" in block
    # The plain '✓ Sprint:' check mark must NOT appear on the stale path
    assert "✓ Sprint:" not in block


def test_readiness_block_fresh_sprint_normal():
    """When sprint_stale_days is None, the readiness block shows the normal line."""
    block = handoff_module._build_readiness_block(
        sprint="current sprint v0.2",
        pending_count=2,
        decisions_count=1,
        sprint_stale_days=None,
    )
    assert "Sprint: current sprint v0.2" in block
    assert "STALE" not in block


def test_readiness_block_default_no_stale_arg():
    """Backward compat: omitting sprint_stale_days defaults to None (no warning)."""
    block = handoff_module._build_readiness_block(
        sprint="sprint-v1",
        pending_count=1,
        decisions_count=0,
    )
    assert "Sprint: sprint-v1" in block
    assert "STALE" not in block


# ---------------------------------------------------------------------------
# Integration: generate_handoff emits staleness warning in full mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_handoff_stale_sprint_warns_in_content(db, tmp_path):
    """08c355c2 — When a project has sprint items AND the goal.sprint field is old,
    the rendered handoff replaces the verbatim sprint text with a staleness warning."""
    p = await db_module.create_project(db, "alpha-stale-sprint")
    # Set a sprint field that is 60 days old by patching sprint_updated_at directly
    await db_module.set_goal(db, p["id"], "ship v0.2", sprint="v0.1 old sprint text")
    # Manually backdate sprint_updated_at to simulate a months-old stale field
    old_ts = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
    async with db.execute(
        "UPDATE goal_states SET sprint_updated_at = ? WHERE project_id = ?",
        (old_ts, p["id"]),
    ):
        pass
    await db.commit()
    # Add a sprint item so the project is using item tracking
    s = await db_module.register_session(db, p["id"], "sess-stale")
    await db_module.add_sprint_item(db, p["id"], "v0.2", "Fix the bug")

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )

    # The staleness warning must appear in both the readiness block and the L1 section
    assert "STALE" in content
    assert "superseded by sprint items" in content
    # The old sprint text should still be visible (demoted, not hidden entirely)
    assert "v0.1 old sprint text" in content
    # The simple "Sprint: v0.1 ..." line without a warning must NOT appear
    assert "**Sprint:** v0.1 old sprint text" not in content


@pytest.mark.asyncio
async def test_generate_handoff_fresh_sprint_no_staleness_warning(db, tmp_path):
    """08c355c2 — A sprint field updated recently (< 30 days) must NOT carry a
    staleness warning even when sprint items exist."""
    p = await db_module.create_project(db, "alpha-fresh-sprint")
    await db_module.set_goal(db, p["id"], "ship v0.2", sprint="v0.2 active sprint")
    # sprint_updated_at is set to now by set_goal — no backdating needed
    s = await db_module.register_session(db, p["id"], "sess-fresh")
    await db_module.add_sprint_item(db, p["id"], "v0.2", "Implement feature X")

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )

    assert "STALE" not in content
    assert "Sprint: v0.2 active sprint" in content


@pytest.mark.asyncio
async def test_generate_handoff_no_items_sprint_shown_verbatim(db, tmp_path):
    """08c355c2 — When a project has NO sprint items, the free-text sprint field is
    always shown verbatim (no staleness check), regardless of age."""
    p = await db_module.create_project(db, "alpha-no-items-sprint")
    await db_module.set_goal(db, p["id"], "early days", sprint="v0.0 initial sprint")
    # Backdate sprint_updated_at
    old_ts = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d %H:%M:%S")
    async with db.execute(
        "UPDATE goal_states SET sprint_updated_at = ? WHERE project_id = ?",
        (old_ts, p["id"]),
    ):
        pass
    await db.commit()
    # No sprint items added

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )

    # No items → sprint text shown at full weight, no staleness warning
    assert "STALE" not in content
    assert "Sprint: v0.0 initial sprint" in content
