"""77a29c8b — Diagnostic tasks (kind=blocked/found) surface in compact/delta/starter
paths even when they would not appear in the regular recent_tasks[:3] slice.

Tests cover:
  1. _collect_diagnostic_tasks pure helper
  2. _render_delta_handoff includes diagnostic block when entries exist
  3. _render_delta_handoff omits block when entries are absent
  4. _render_starter_handoff includes diagnostic block when entries exist
  5. generate_handoff(mode='delta') surfaces blocked/found in output
  6. generate_handoff(mode='starter') surfaces blocked/found in output
  7. compact start_session orientation carries recent_diagnostic_tasks field
     (exercised via the server TestClient)

All tests use synthetic/hardcoded values — no __file__/cwd-derived paths —
to avoid the test-origin-worktree flake documented in the sprint notes.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


# ---------------------------------------------------------------------------
# 1. Pure helper: _collect_diagnostic_tasks
# ---------------------------------------------------------------------------

def test_collect_diagnostic_tasks_filters_blocked_found():
    tasks = [
        {"kind": "shipped", "description": "Shipped auth fix"},
        {"kind": "blocked", "description": "CI red: missing import"},
        {"kind": "found", "description": "Gotcha: psycopg3 async cursor"},
        {"kind": "decided", "description": "Use psycopg3"},
        {"kind": "blocked", "description": "Test gate failed on migration 72"},
    ]
    result = handoff_module._collect_diagnostic_tasks(tasks)
    kinds = [t["kind"] for t in result]
    assert all(k in ("blocked", "found") for k in kinds)
    assert len(result) <= 3


def test_collect_diagnostic_tasks_empty_when_none_match():
    tasks = [
        {"kind": "shipped", "description": "Shipped auth fix"},
        {"kind": "decided", "description": "Use psycopg3"},
    ]
    result = handoff_module._collect_diagnostic_tasks(tasks)
    assert result == []


def test_collect_diagnostic_tasks_respects_limit():
    tasks = [
        {"kind": "blocked", "description": f"Gate failure {i}"}
        for i in range(10)
    ]
    result = handoff_module._collect_diagnostic_tasks(tasks, limit=3)
    assert len(result) == 3


def test_collect_diagnostic_tasks_null_kind_excluded():
    tasks = [
        {"kind": None, "description": "No kind"},
        {"kind": "", "description": "Empty kind"},
        {"kind": "blocked", "description": "A real gate failure"},
    ]
    result = handoff_module._collect_diagnostic_tasks(tasks)
    assert len(result) == 1
    assert result[0]["description"] == "A real gate failure"


# ---------------------------------------------------------------------------
# 2-3. _render_delta_handoff — with and without diagnostic_tasks
# ---------------------------------------------------------------------------

_FAKE_PROJECT = {"id": "proj-diag-test", "name": "DiagProject"}


def test_render_delta_handoff_includes_diagnostics():
    diag = [
        {"kind": "blocked", "description": "pixi run test -n auto fails on migration 72"},
        {"kind": "found", "description": "psycopg3 needs autocommit=True"},
    ]
    content = handoff_module._render_delta_handoff(
        _FAKE_PROJECT,
        generated_at="2026-01-01T00:00:00",
        completed_items=[],
        in_progress_items=[],
        pending_sprint_items=[],
        quick_start_goal="/goal\n<role>executor</role>",
        diagnostic_tasks=diag,
    )
    assert "Recent diagnostics (blocked/found):" in content
    assert "[BLOCKED]" in content
    assert "migration 72" in content
    assert "[FOUND]" in content
    assert "autocommit=True" in content


def test_render_delta_handoff_omits_diagnostic_block_when_empty():
    content = handoff_module._render_delta_handoff(
        _FAKE_PROJECT,
        generated_at="2026-01-01T00:00:00",
        completed_items=[],
        in_progress_items=[],
        pending_sprint_items=[],
        quick_start_goal="/goal\n<role>executor</role>",
        diagnostic_tasks=[],
    )
    assert "Recent diagnostics" not in content


def test_render_delta_handoff_omits_diagnostic_block_when_none():
    content = handoff_module._render_delta_handoff(
        _FAKE_PROJECT,
        generated_at="2026-01-01T00:00:00",
        completed_items=[],
        in_progress_items=[],
        pending_sprint_items=[],
        quick_start_goal="/goal\n<role>executor</role>",
        # diagnostic_tasks not passed (defaults to None)
    )
    assert "Recent diagnostics" not in content


# ---------------------------------------------------------------------------
# 4. _render_starter_handoff — with diagnostic_tasks
# ---------------------------------------------------------------------------

def test_render_starter_handoff_includes_diagnostics():
    diag = [
        {"kind": "blocked", "description": "Test suite fails: test_pg_migration_registry count wrong"},
    ]
    content = handoff_module._render_starter_handoff(
        _FAKE_PROJECT,
        completed_items=[],
        pending_items=[],
        quick_start_goal="/goal\n<role>executor</role>",
        diagnostic_tasks=diag,
    )
    assert "Recent diagnostics (blocked/found):" in content
    assert "[BLOCKED]" in content
    assert "pg_migration_registry" in content


def test_render_starter_handoff_omits_diagnostic_block_when_empty():
    content = handoff_module._render_starter_handoff(
        _FAKE_PROJECT,
        completed_items=[],
        pending_items=[],
        quick_start_goal="/goal\n<role>executor</role>",
        diagnostic_tasks=[],
    )
    assert "Recent diagnostics" not in content


# ---------------------------------------------------------------------------
# 5. generate_handoff(mode='delta') end-to-end: blocked task in output
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_handoff_delta_surfaces_blocked_task(db, tmp_path):
    """A blocked task logged before generate_handoff(mode='delta') appears in output."""
    p = await db_module.create_project(db, "diag-delta-proj")
    await db_module.set_goal(db, p["id"], "fix the gate")
    s = await db_module.register_session(db, p["id"], "diag-sess")
    # Log shipped tasks first so the blocked entry is NOT in the top-3 recent_tasks.
    for i in range(4):
        await db_module.log_task(
            db, s["id"], p["id"], f"Shipped thing {i}", kind="shipped"
        )
    await db_module.log_task(
        db, s["id"], p["id"],
        "GATE FAILURE: pixi run test shows 350 but 4 new tests need count bump",
        kind="blocked",
    )
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id="diag-delta-sess",
    )
    assert "Recent diagnostics (blocked/found):" in content
    assert "[BLOCKED]" in content
    assert "GATE FAILURE" in content


@pytest.mark.asyncio
async def test_generate_handoff_delta_surfaces_found_task(db, tmp_path):
    """A found-kind task appears in the diagnostics section of delta output."""
    p = await db_module.create_project(db, "diag-found-proj")
    await db_module.set_goal(db, p["id"], "investigate the bug")
    s = await db_module.register_session(db, p["id"], "found-sess")
    await db_module.log_task(
        db, s["id"], p["id"],
        "FOUND: psycopg3 LIKE patterns need %% not % for literal percent",
        kind="found",
    )
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id="diag-found-sess",
    )
    assert "Recent diagnostics (blocked/found):" in content
    assert "[FOUND]" in content


@pytest.mark.asyncio
async def test_generate_handoff_delta_no_diagnostics_section_when_none(db, tmp_path):
    """Delta output has no diagnostics section when no blocked/found tasks exist."""
    p = await db_module.create_project(db, "diag-clean-proj")
    await db_module.set_goal(db, p["id"], "clean run")
    s = await db_module.register_session(db, p["id"], "clean-sess")
    await db_module.log_task(db, s["id"], p["id"], "Shipped auth fix", kind="shipped")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id="diag-clean-sess",
    )
    assert "Recent diagnostics" not in content


# ---------------------------------------------------------------------------
# 6. generate_handoff(mode='starter') end-to-end: blocked task in output
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_handoff_starter_surfaces_blocked_task(db, tmp_path):
    """A blocked task logged before generate_handoff(mode='starter') appears in output."""
    p = await db_module.create_project(db, "diag-starter-proj")
    await db_module.set_goal(db, p["id"], "fix the gate")
    s = await db_module.register_session(db, p["id"], "starter-sess")
    await db_module.log_task(
        db, s["id"], p["id"],
        "BLOCKED: CI failing on coverage gate — 83%% vs 85%% required",
        kind="blocked",
    )
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="starter",
    )
    assert "Recent diagnostics (blocked/found):" in content
    assert "[BLOCKED]" in content
    assert "CI failing" in content


# ---------------------------------------------------------------------------
# 7. compact start_session orientation: recent_diagnostic_tasks field
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compact_orientation_includes_diagnostic_tasks_field(db, tmp_path):
    """The compact start_session payload carries recent_diagnostic_tasks.

    Exercises the in-process _start_session_composite path directly via the
    db fixture so we can log tasks with kind='blocked' without going through
    the HTTP layer (the /tasks endpoint doesn't expose the kind param).
    """
    from meridian import server as server_module

    p = await db_module.create_project(db, "compact-diag-proj-2")
    await db_module.set_goal(db, p["id"], "ship the fix")
    s = await db_module.register_session(db, p["id"], "compact-initial-sess")

    # Log shipped tasks first (they should NOT appear in diagnostic list).
    for i in range(4):
        await db_module.log_task(db, s["id"], p["id"], f"Shipped thing {i}", kind="shipped")

    # Log a blocked task — this IS the diagnostic signal.
    await db_module.log_task(
        db, s["id"], p["id"],
        "BLOCKED: test suite red — missing migration name in pg_adapter _PG_MIGRATIONS_CORE",
        kind="blocked",
    )

    # Call the composite helper directly (compact=True is the default).
    payload = await server_module._start_session_composite(
        db,
        p["id"],
        session_name="resumed-compact",
        data_dir=str(tmp_path),
        compact=True,
    )
    assert "recent_diagnostic_tasks" in payload
    diag = payload["recent_diagnostic_tasks"]
    assert isinstance(diag, list)
    # The blocked task we logged should appear.
    kinds = [t.get("kind") for t in diag]
    assert "blocked" in kinds
    # Descriptions should be present and bounded.
    for entry in diag:
        desc = entry.get("description", "")
        assert len(desc) <= 200
