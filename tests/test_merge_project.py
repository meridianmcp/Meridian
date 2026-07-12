"""d6bd60e0 — merge_project: re-parent a phantom-duplicate project's items.

Covers the db-layer ``merge_project`` mechanism (re-parent every project-scoped
child row from source -> target via pure UPDATEs, soft-archive the source, never
delete), its guards (self-merge / unknown project -> {error}, no mutation), and
one dispatch-through-``_dispatch_mcp_tool`` smoke test.

These tests build + exercise the mechanism ONLY on a throwaway in-memory SQLite
DB. They never touch a real project — the live merge of real data stays a human
decision.
"""

from __future__ import annotations

import pytest

from meridian import db as db_module


async def _seed_source(db, project_id, session_id):
    """Seed a source project with sprint_items + decisions + task_log rows."""
    await db_module.add_sprint_item(db, project_id, "v1", "first item")
    await db_module.add_sprint_item(db, project_id, "v1", "second item")
    await db_module.add_sprint_item(db, project_id, "v1", "third item")
    await db_module.pin_decision(db, project_id, "Use psycopg3", "asyncpg DLL issues")
    await db_module.pin_decision(db, project_id, "Use pixi", "reproducible envs")
    await db_module.log_task(db, session_id, project_id, "did a thing")
    await db_module.log_task(db, session_id, project_id, "did another thing")


async def _count(db, table, project_id):
    async with db.execute(
        f"SELECT COUNT(*) AS n FROM {table} WHERE project_id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    # row is a dict (aiosqlite Row / pg adapter) — read positionally-agnostic.
    return row["n"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]


@pytest.mark.asyncio
async def test_merge_reparents_all_seeded_rows(db):
    source = await db_module.create_project(db, "source-dup")
    target = await db_module.create_project(db, "target-keep")
    sess = await db_module.register_session(db, source["id"], "seed-sess")

    await _seed_source(db, source["id"], sess["id"])

    # Pre-conditions: rows live under the source, target is empty.
    assert await _count(db, "sprint_items", source["id"]) == 3
    assert await _count(db, "decisions_pinned", source["id"]) == 2
    assert await _count(db, "task_log", source["id"]) == 2
    assert await _count(db, "sprint_items", target["id"]) == 0
    assert await _count(db, "decisions_pinned", target["id"]) == 0
    assert await _count(db, "task_log", target["id"]) == 0

    result = await db_module.merge_project(db, source["id"], target["id"])

    # No error dict; well-formed success payload.
    assert "error" not in result
    assert result["source_project_id"] == source["id"]
    assert result["target_project_id"] == target["id"]
    assert result["source_archived"] is True

    # Every seeded row now carries the target's project_id.
    assert await _count(db, "sprint_items", target["id"]) == 3
    assert await _count(db, "decisions_pinned", target["id"]) == 2
    assert await _count(db, "task_log", target["id"]) == 2
    # …and none remain under the source.
    assert await _count(db, "sprint_items", source["id"]) == 0
    assert await _count(db, "decisions_pinned", source["id"]) == 0
    assert await _count(db, "task_log", source["id"]) == 0

    # The session row also re-parented (sessions carry a project_id FK).
    assert await _count(db, "sessions", target["id"]) == 1
    assert await _count(db, "sessions", source["id"]) == 0

    # Returned 'moved' counts are exact for the seeded tables.
    assert result["moved"]["sprint_items"] == 3
    assert result["moved"]["decisions_pinned"] == 2
    assert result["moved"]["task_log"] == 2
    assert result["moved"]["sessions"] == 1
    # Untouched project-scoped tables report a zero move (present, not skipped).
    assert result["moved"]["insights"] == 0
    assert result["moved"]["project_notes"] == 0
    assert result["moved"]["hitl_requests"] == 0


@pytest.mark.asyncio
async def test_source_archived_not_deleted(db):
    source = await db_module.create_project(db, "phantom-source")
    target = await db_module.create_project(db, "canonical-target")
    sess = await db_module.register_session(db, source["id"], "seed-sess")
    await _seed_source(db, source["id"], sess["id"])

    result = await db_module.merge_project(db, source["id"], target["id"])
    assert result["source_archived"] is True

    # Source project row STILL EXISTS (soft-archived, never hard-deleted).
    src = await db_module.get_project(db, source["id"])
    assert src is not None
    assert src["name"].startswith("[merged] ")
    assert "phantom-source" in src["name"]
    assert src["status"] == "archived"

    # The rows themselves are intact — they moved, they were not dropped.
    assert await _count(db, "sprint_items", target["id"]) == 3
    total_items = await _count(db, "sprint_items", source["id"]) + await _count(
        db, "sprint_items", target["id"]
    )
    assert total_items == 3


@pytest.mark.asyncio
async def test_archive_source_false_leaves_source_untouched(db):
    source = await db_module.create_project(db, "src-keepname")
    target = await db_module.create_project(db, "tgt-keepname")
    sess = await db_module.register_session(db, source["id"], "seed-sess")
    await _seed_source(db, source["id"], sess["id"])

    result = await db_module.merge_project(
        db, source["id"], target["id"], archive_source=False
    )
    assert result["source_archived"] is False

    # Rows still re-parented …
    assert await _count(db, "sprint_items", target["id"]) == 3
    # … but the source project name/status are unchanged.
    src = await db_module.get_project(db, source["id"])
    assert src is not None
    assert src["name"] == "src-keepname"
    assert src["status"] == "active"


@pytest.mark.asyncio
async def test_self_merge_returns_error_and_mutates_nothing(db):
    project = await db_module.create_project(db, "only-project")
    sess = await db_module.register_session(db, project["id"], "seed-sess")
    await _seed_source(db, project["id"], sess["id"])

    result = await db_module.merge_project(db, project["id"], project["id"])
    assert "error" in result
    assert "same" in result["error"].lower()

    # No exception, no mutation: rows and name untouched.
    assert await _count(db, "sprint_items", project["id"]) == 3
    assert await _count(db, "decisions_pinned", project["id"]) == 2
    proj = await db_module.get_project(db, project["id"])
    assert proj["name"] == "only-project"
    assert proj["status"] == "active"


@pytest.mark.asyncio
async def test_unknown_project_returns_error_and_mutates_nothing(db):
    real = await db_module.create_project(db, "real-project")
    sess = await db_module.register_session(db, real["id"], "seed-sess")
    await _seed_source(db, real["id"], sess["id"])

    # Unknown SOURCE.
    r1 = await db_module.merge_project(db, "does-not-exist", real["id"])
    assert "error" in r1
    assert "source" in r1["error"].lower()

    # Unknown TARGET.
    r2 = await db_module.merge_project(db, real["id"], "does-not-exist")
    assert "error" in r2
    assert "target" in r2["error"].lower()

    # Neither error path mutated the real project's rows.
    assert await _count(db, "sprint_items", real["id"]) == 3
    assert await _count(db, "decisions_pinned", real["id"]) == 2
    assert await _count(db, "task_log", real["id"]) == 2
    proj = await db_module.get_project(db, real["id"])
    assert proj["name"] == "real-project"
    assert proj["status"] == "active"


@pytest.mark.asyncio
async def test_merge_project_registered_in_mcp_tools_list():
    """merge_project is registered, mutating (not read-only), and requires both ids."""
    from meridian.mcp_tools import _MCP_TOOLS_LIST, _READ_ONLY_TOOLS

    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    assert "merge_project" in by_name
    tool = by_name["merge_project"]
    props = tool["inputSchema"]["properties"]
    assert "source_project_id" in props
    assert "target_project_id" in props
    assert "archive_source" in props
    required = set(tool["inputSchema"]["required"])
    assert required == {"source_project_id", "target_project_id"}
    # It is a MUTATING tool, never read-only.
    assert "merge_project" not in _READ_ONLY_TOOLS


@pytest.mark.asyncio
async def test_merge_project_dispatch_smoke(db):
    """Dispatch merge_project through the real _dispatch_mcp_tool entrypoint."""
    from meridian import server as srv

    source = await db_module.create_project(db, "dispatch-src")
    target = await db_module.create_project(db, "dispatch-tgt")
    sess = await db_module.register_session(db, source["id"], "seed-sess")
    await _seed_source(db, source["id"], sess["id"])

    result = await srv._dispatch_mcp_tool(
        "merge_project",
        {"source_project_id": source["id"], "target_project_id": target["id"]},
        db,
        "/tmp",
    )
    assert isinstance(result, dict)
    assert "error" not in result
    assert result["moved"]["sprint_items"] == 3
    assert result["moved"]["decisions_pinned"] == 2
    assert result["source_archived"] is True
    assert await _count(db, "sprint_items", target["id"]) == 3


@pytest.mark.asyncio
async def test_merge_project_dispatch_self_merge_error(db):
    """Self-merge through dispatch returns the {error} dict verbatim, no raise."""
    from meridian import server as srv

    project = await db_module.create_project(db, "dispatch-selfmerge")

    result = await srv._dispatch_mcp_tool(
        "merge_project",
        {"source_project_id": project["id"], "target_project_id": project["id"]},
        db,
        "/tmp",
    )
    assert isinstance(result, dict)
    assert "error" in result
