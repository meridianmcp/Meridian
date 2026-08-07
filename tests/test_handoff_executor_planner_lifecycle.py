"""Tests for 0d0cada7 — lease-local scheduler contract, handoff-rendering half.

meridian/db/sprint_items.py (get_parallelizable_groups / claim_parallel_batch),
meridian/db/locks.py (claim_symbol / release_symbol), meridian/mcp/handler.py
(_sprint_item_resource_claim_gate), and meridian/db/__init__.py (request_hitl)
each gained additive scheduler-lease diagnostics (plan_generation,
resource_blocked, claim_granularity, lease_expiry, wait_reason, retry_after,
blocker_context) — see tests/test_resource_locks.py and
tests/test_sprint_item_waves.py for that coverage. This file covers the other
half of the contract: the executor/planner LIFECYCLE surface —
meridian/handoff.py's new ``_build_scheduler_lease_clause`` and its wiring
into ``_build_quick_start_goal`` / ``generate_handoff``.

Motivating incident (from the sprint item's own notes): a live v0.2.6 run
had one session actively executing exactly ONE item while its broader
"planned backlog" made every OTHER authorized item look blocked for hours;
the executor emitted a native clarification instead of recording a Meridian
blocker or recomputing the residual work. The goal-string guidance added
here is what tells a receiving executor to poll with bounded backoff and
recompute via get_parallelizable_groups instead of repeating that mistake.
"""
from __future__ import annotations

import json

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


# ---------------------------------------------------------------------------
# _build_scheduler_lease_clause — unit-level (no DB), pinning the exact
# additive/backward-compatible contract the docstring promises.
# ---------------------------------------------------------------------------


def test_scheduler_lease_clause_empty_when_parallel_groups_none():
    assert handoff_module._build_scheduler_lease_clause(None) == ""


def test_scheduler_lease_clause_empty_when_parallel_groups_empty_dict():
    assert handoff_module._build_scheduler_lease_clause({}) == ""


def test_scheduler_lease_clause_empty_when_neither_field_present():
    """A hand-built dict from a pre-0d0cada7 code path (or an older cached
    fixture) carries neither key — must degrade to '', not KeyError."""
    assert handoff_module._build_scheduler_lease_clause({"groups": [], "blocked": []}) == ""


def test_scheduler_lease_clause_renders_plan_generation_tag():
    clause = handoff_module._build_scheduler_lease_clause({"plan_generation": "abc123deadbeef"})
    assert '<plan_generation value="abc123deadbeef" />' in clause
    assert "<resource_contention>" not in clause


def test_scheduler_lease_clause_xml_escapes_plan_generation():
    clause = handoff_module._build_scheduler_lease_clause({"plan_generation": 'a"b<c>'})
    assert '"' not in clause.split('value="', 1)[1].split('"', 1)[0].replace("&quot;", "")
    assert "&quot;" in clause  # the embedded quote was escaped, not left raw


def test_scheduler_lease_clause_renders_resource_contention_with_poll_guidance():
    clause = handoff_module._build_scheduler_lease_clause({
        "resource_blocked": [
            {
                "id": "item-1", "resource": "file:a.py",
                "holder_session_id": "holder-session-abcdef", "retry_after": 42,
            },
        ],
    })
    assert "<resource_contention>" in clause
    assert "</resource_contention>" in clause
    assert "item-1" in clause
    assert "file:a.py" in clause
    assert "42" in clause
    # The core behavioral guidance the incident's postmortem calls for.
    assert "do not open a native clarification" in clause
    assert "scheduler_blocker" in clause
    assert "get_parallelizable_groups" in clause


def test_scheduler_lease_clause_renders_both_fields_together():
    clause = handoff_module._build_scheduler_lease_clause({
        "plan_generation": "gen1",
        "resource_blocked": [{"id": "x", "resource": "file:x.py", "retry_after": 10}],
    })
    assert '<plan_generation value="gen1" />' in clause
    assert "<resource_contention>" in clause


# ---------------------------------------------------------------------------
# End-to-end via generate_handoff(mode="goal") — the clause is wired into the
# REAL /goal string, using the SAME get_parallelizable_groups() call
# generate_handoff already makes (no new call site, no new parameter).
# ---------------------------------------------------------------------------


def _sprint_items_tag_body(content: str) -> str:
    start = content.rindex("<sprint_items>") + len("<sprint_items>")
    end = content.index("</sprint_items>", start)
    return content[start:end]


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_includes_plan_generation(db, tmp_path):
    p = await db_module.create_project(db, "0d0cada7-goal-plan-generation")
    await db_module.add_sprint_item(db, p["id"], "v1", "solo item", prospect_bypass=True)
    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    assert "<plan_generation value=" in content


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_no_contention_when_nothing_locked(db, tmp_path):
    """The common case: nothing external is held, so the goal must NOT claim
    resource contention that doesn't exist."""
    p = await db_module.create_project(db, "0d0cada7-goal-no-contention")
    await db_module.add_sprint_item(db, p["id"], "v1", "free item", prospect_bypass=True)
    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    assert "<resource_contention>" not in content


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_surfaces_resource_contention(db, tmp_path):
    """The exact incident shape: an item is dependency-satisfied (claimable
    per the wave/dependency logic) but a DIFFERENT live session already
    holds its declared file. The /goal must say so explicitly and tell the
    executor to poll, not escalate."""
    p = await db_module.create_project(db, "0d0cada7-goal-contention")
    pid = p["id"]
    other = await db_module.register_session(db, pid, "other-live-session")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "touches an externally locked file",
        touches_resources=["file:contended.py"], prospect_bypass=True,
    )
    pre = await db_module.claim_file(db, "contended.py", other["id"])
    assert pre["claimed"] is True

    _path, content, _amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    assert "<resource_contention>" in content
    assert item["id"] in content
    assert "do not open a native clarification" in content
    # The item itself is still listed as claimable work, not silently dropped.
    assert item["id"] in _sprint_items_tag_body(content)


# ---------------------------------------------------------------------------
# request_hitl blocker_context — the "genuine conflict is persisted and
# visible through Meridian HITL/blocker APIs" acceptance criterion, exercised
# through the SAME live() lookup a scheduler-blocker caller would use to
# build blocker_context (get_parallelizable_groups' resource_blocked entry).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_blocker_hitl_round_trips_resource_blocked_entry(db):
    """An executor that sees a resource_blocked entry from
    get_parallelizable_groups can hand it straight to request_hitl's
    blocker_context and get a durable, queryable record back — the
    'structured Meridian blocker event' the contract calls for, distinct
    from an untracked native stop."""
    p = await db_module.create_project(db, "0d0cada7-scheduler-blocker-roundtrip")
    pid = p["id"]
    holder = await db_module.register_session(db, pid, "holder")
    waiting = await db_module.register_session(db, pid, "waiting")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "contended item", touches_resources=["file:hot.py"],
    )
    pre = await db_module.claim_file(db, "hot.py", holder["id"])
    assert pre["claimed"] is True

    groups = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert groups["resource_blocked_count"] == 1
    entry = dict(groups["resource_blocked"][0])
    entry["item_id"] = entry["id"]  # request_hitl's blocker field is "item_id"
    entry["plan_generation"] = groups["plan_generation"]

    row = await db_module.request_hitl(
        db, pid, f"{item['id']} is waiting on a live resource lock",
        session_id=waiting["id"], kind="scheduler_blocker", blocker_context=entry,
    )
    assert row["kind"] == "scheduler_blocker"
    blocker = json.loads(row["payload"])["blocker"]
    assert blocker["item_id"] == item["id"]
    assert blocker["holder_session_id"] == holder["id"]
    assert blocker["wait_reason"] == "resource_locked"
    assert blocker["plan_generation"] == groups["plan_generation"]

    # Visible via the listing API too, not just the immediate return value.
    pending = await db_module.list_hitl_requests(db, pid, status="pending")
    assert any(r["id"] == row["id"] for r in pending)
