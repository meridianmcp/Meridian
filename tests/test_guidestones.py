"""Guidestones: the impromptu research waypoint (meridian/db/guidestones.py) and its three MCP tools.

SQLite in-memory throughout. The Postgres mirror (meridian/pg_adapter.py
_migrate_pg_guidestones) is only pinned by name and count in tests/test_core.py; its DDL
is not executed against a live Postgres here.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from meridian import db as db_module
from meridian import mcp_tools
from meridian.db import experiments as experiments_db
from meridian.db import guidestones as guidestone_db
from meridian.db import research_runs as research_runs_db
from meridian.mcp.handlers.guidestone_tools import (
    handle_create_guidestone,
    handle_list_guidestones,
    handle_load_guidestone,
)

# AWS's documented example key id. It matches the aws-access-key-id pattern and is not a real credential.
SECRET_SHAPED = "AKIAIOSFODNN7EXAMPLE"
PAGE = "https://example.org/paper-1"


def run(coro):
    return asyncio.run(coro)


async def _seeded(db, name="guide-proj"):
    project = await db_module.create_project(db, name)
    session = await db_module.register_session(db, project["id"], "researcher")
    return project["id"], session["id"]


async def _raw_finding(db, pid, *, title, body, created_at, source=PAGE, tags="finding,web"):
    """A finding row written directly, so a test can plant text save_finding would refuse."""
    note_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO project_notes (id, project_id, title, body, tags, note_kind, source, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'finding', ?, ?, ?)",
        (note_id, pid, title, body, tags, source, created_at, created_at),
    )
    await db.commit()
    return note_id


async def _count(db, sql, params=()):
    async with db.execute(sql, params) as cur:
        row = await cur.fetchone()
    return row[0]


# ----------------------------------------------------------------- migration


def test_migration_creates_the_table_and_runs_again_without_error(tmp_path):
    path = str(tmp_path / "guide.db")

    async def _run():
        db = await db_module.init_db(path)
        await db.close()
        db = await db_module.init_db(path)
        try:
            assert await _count(
                db, "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='guidestones'"
            ) == 1
        finally:
            await db.close()

    run(_run())


# ----------------------------------------------------------------- freezing


def test_create_freezes_findings_decisions_and_open_work():
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            pid, sid = await _seeded(db)
            decision = await db_module.pin_decision(db, pid, "Use the 2025 cohort", "Restrict to the 2025 cohort.")
            saved = await db_module.save_finding(
                db, pid, "Cohort effect is about 0.3", source_url=PAGE, source_type="web",
                decision_id=decision["id"],
            )
            experiment = await experiments_db.create_experiment(
                db, pid, sid, name="Cohort replication", hypothesis="Effect holds in 2026",
            )
            await research_runs_db.start_research_run(
                db, pid, sid, mode="read_only", repository_id="repo-1", turn_budget=10,
            )
            await db_module.add_sprint_item(db, pid, "v1", "Re-run the cohort model")

            result = await guidestone_db.create_guidestone(
                db, pid, name="cohort pause", question="Does the cohort effect hold?",
                experiment_id=experiment["id"], session_id=sid,
                open_threads=["waiting on data access"], next_steps=["email the data steward"],
            )
            loaded = await guidestone_db.load_guidestone(db, pid, guidestone_id=result["guidestone_id"])
            return saved, decision, experiment, result, loaded
        finally:
            await db.close()

    saved, decision, experiment, result, loaded = run(_run())
    assert result["name"] == "cohort pause"
    assert result["counts"] == {"findings": 1, "sources": 1, "decisions": 1, "open_runs": 1, "next_steps": 2}
    assert result["warnings"] == []
    state = loaded["state"]
    assert state["question"] == "Does the cohort effect hold?"
    assert state["experiment"]["name"] == "Cohort replication"
    assert state["experiment"]["hypothesis"] == "Effect holds in 2026"
    assert state["findings"][0]["id"] == saved["note"]["id"]
    assert state["findings"][0]["source_url"] == PAGE
    assert state["sources"] == [{"url": PAGE, "source_type": "web"}]
    assert state["decisions"][0]["id"] == decision["id"]
    assert {t["kind"] for t in state["open_threads"]} == {"scratch_run", "note"}
    assert [s["kind"] for s in state["next_steps"]] == ["note", "sprint_item"]
    assert loaded["untrusted"] is True
    assert loaded["guidestone"]["created_at"]


def test_a_snapshot_keeps_its_findings_after_the_source_note_is_deleted():
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            pid, _sid = await _seeded(db)
            note_id = await _raw_finding(
                db, pid, title="Finding: cohort effect", body="The effect is about 0.3.",
                created_at="2026-01-01 00:00:00",
            )
            result = await guidestone_db.create_guidestone(db, pid, question="Does it hold?")
            await db.execute("DELETE FROM project_notes WHERE id = ?", (note_id,))
            await db.commit()
            return await guidestone_db.load_guidestone(db, pid, guidestone_id=result["guidestone_id"])
        finally:
            await db.close()

    loaded = run(_run())
    assert loaded["state"]["findings"][0]["title"] == "Finding: cohort effect"
    assert loaded["state"]["counts"]["findings"] == 1


def test_a_secret_shaped_question_is_refused_and_nothing_is_written():
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            pid, _sid = await _seeded(db)
            with pytest.raises(ValueError) as info:
                await guidestone_db.create_guidestone(db, pid, question=f"Does it hold? {SECRET_SHAPED}")
            rows = await _count(db, "SELECT COUNT(*) FROM guidestones")
            return str(info.value), rows
        finally:
            await db.close()

    message, rows = run(_run())
    assert "secret-shaped" in message
    assert SECRET_SHAPED not in message
    assert rows == 0


def test_a_stored_finding_with_secret_shaped_text_is_left_out_and_never_copied():
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            pid, _sid = await _seeded(db)
            leaked = await _raw_finding(
                db, pid, title="Finding: leaked key", body=f"key {SECRET_SHAPED}",
                created_at="2026-01-01 00:00:01",
            )
            await _raw_finding(db, pid, title="Finding: clean", body="Clean evidence.", created_at="2026-01-01 00:00:00")
            result = await guidestone_db.create_guidestone(db, pid, question="Q")
            loaded = await guidestone_db.load_guidestone(db, pid, guidestone_id=result["guidestone_id"])
            return leaked, result, loaded
        finally:
            await db.close()

    leaked, result, loaded = run(_run())
    assert result["warnings"] == [f"finding {leaked} left out: secret-shaped text"]
    assert [f["title"] for f in loaded["state"]["findings"]] == ["Finding: clean"]
    assert SECRET_SHAPED not in json.dumps(loaded)


def test_oversize_state_drops_the_oldest_findings_and_counts_them():
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            pid, _sid = await _seeded(db)
            body = "cohort effect evidence " * 30
            for i in range(60):
                await _raw_finding(
                    db, pid, title=f"Finding {i:02d}", body=body,
                    created_at=f"2026-01-01 00:{i // 60:02d}:{i % 60:02d}",
                    source=f"https://example.org/p{i}",
                )
            result = await guidestone_db.create_guidestone(db, pid, question="Q")
            loaded = await guidestone_db.load_guidestone(db, pid, guidestone_id=result["guidestone_id"])
            return result, loaded
        finally:
            await db.close()

    result, loaded = run(_run())
    state = loaded["state"]
    assert state["findings_omitted"] >= 1
    assert state["findings_omitted"] + len(state["findings"]) == guidestone_db.DEFAULT_FINDINGS_LIMIT
    # Findings 00-09 fall outside the newest-50 window, so the considered set is 10-59. Newest first,
    # the kept titles are the newest run, and the dropped ones are the oldest of the considered set.
    kept = len(state["findings"])
    assert [f["title"] for f in state["findings"]] == [
        f"Finding {i:02d}" for i in range(59, 59 - kept, -1)
    ], "the size cap drops the oldest considered findings first"
    assert loaded["guidestone"]["state_bytes"] <= guidestone_db.MAX_STATE_BYTES
    assert result["findings_omitted"] == state["findings_omitted"]


def test_a_state_still_over_the_cap_with_no_findings_fails_loudly():
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            pid, _sid = await _seeded(db)
            with pytest.raises(ValueError, match="over the 32 KB cap"):
                await guidestone_db.create_guidestone(
                    db, pid, question="Q",
                    open_threads=["plan step " * 50] * 50,
                    next_steps=["plan step " * 50] * 50,
                )
            return await _count(db, "SELECT COUNT(*) FROM guidestones")
        finally:
            await db.close()

    assert run(_run()) == 0


def test_a_write_that_does_not_land_raises_and_is_not_reported_as_saved():
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            pid, _sid = await _seeded(db)
            await db.execute("DROP TABLE guidestones")
            with pytest.raises(Exception):
                await guidestone_db.create_guidestone(db, pid, question="Q")
        finally:
            await db.close()

    run(_run())


def test_bad_caller_input_is_refused():
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            pid, _sid = await _seeded(db)
            _other_pid, other_sid = await _seeded(db, name="other-proj")
            refusals = {}
            for label, call in {
                "long question": lambda: guidestone_db.create_guidestone(db, pid, question="x" * 501),
                "too many threads": lambda: guidestone_db.create_guidestone(db, pid, open_threads=["t"] * 51),
                "session of another project": lambda: guidestone_db.create_guidestone(db, pid, session_id=other_sid),
                "unknown experiment": lambda: guidestone_db.create_guidestone(db, pid, experiment_id="missing"),
                "unknown project": lambda: guidestone_db.create_guidestone(db, "no-such-project", question="Q"),
                "boolean findings limit": lambda: guidestone_db.create_guidestone(db, pid, findings_limit=True),
            }.items():
                with pytest.raises(ValueError):
                    await call()
                refusals[label] = True
            return refusals
        finally:
            await db.close()

    assert len(run(_run())) == 6


# ----------------------------------------------------------------- reading back


def test_load_without_an_id_returns_the_newest_and_list_is_newest_first():
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            pid, _sid = await _seeded(db)
            first = await guidestone_db.create_guidestone(db, pid, name="first", question="Q1")
            second = await guidestone_db.create_guidestone(db, pid, name="second", question="Q2")
            newest = await guidestone_db.load_guidestone(db, pid)
            by_id = await guidestone_db.load_guidestone(db, pid, guidestone_id=first["guidestone_id"])
            listing = await guidestone_db.list_guidestones(db, pid)
            return first, second, newest, by_id, listing
        finally:
            await db.close()

    first, second, newest, by_id, listing = run(_run())
    assert newest["guidestone"]["guidestone_id"] == second["guidestone_id"]
    assert by_id["guidestone"]["name"] == "first"
    assert [row["guidestone_id"] for row in listing] == [second["guidestone_id"], first["guidestone_id"]]
    assert "state" not in listing[0], "the listing omits the saved state"


def test_load_is_scoped_to_its_project_and_reports_nothing_when_empty():
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            pid, _sid = await _seeded(db, name="owner-proj")
            other_pid, _osid = await _seeded(db, name="stranger-proj")
            made = await guidestone_db.create_guidestone(db, pid, question="Q")
            wrong_project = await guidestone_db.load_guidestone(db, other_pid, guidestone_id=made["guidestone_id"])
            empty = await guidestone_db.load_guidestone(db, other_pid)
            other_listing = await guidestone_db.list_guidestones(db, other_pid)
            return wrong_project, empty, other_listing
        finally:
            await db.close()

    wrong_project, empty, other_listing = run(_run())
    assert wrong_project is None
    assert empty is None
    assert other_listing == []


def test_the_resume_block_is_labelled_as_saved_data_and_bounded():
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            pid, _sid = await _seeded(db)
            made = await guidestone_db.create_guidestone(db, pid, question="Does it hold?")
            return await guidestone_db.load_guidestone(db, pid, guidestone_id=made["guidestone_id"])
        finally:
            await db.close()

    loaded = run(_run())
    assert "data, not instructions" in loaded["resume_markdown"]
    assert loaded["resume_truncated"] is False
    assert len(loaded["resume_markdown"].encode("utf-8")) <= guidestone_db.MAX_RESUME_BYTES


# ----------------------------------------------------------------- side effects, cascade, merge


def test_creating_a_guidestone_leaves_the_handoff_and_pending_goal_alone():
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            pid, _sid = await _seeded(db)
            before = (
                await _count(db, "SELECT COUNT(*) FROM handoffs WHERE project_id = ?", (pid,)),
                await _count(db, "SELECT COUNT(*) FROM projects WHERE id = ? AND pending_goal IS NOT NULL", (pid,)),
            )
            await guidestone_db.create_guidestone(db, pid, question="Q")
            after = (
                await _count(db, "SELECT COUNT(*) FROM handoffs WHERE project_id = ?", (pid,)),
                await _count(db, "SELECT COUNT(*) FROM projects WHERE id = ? AND pending_goal IS NOT NULL", (pid,)),
            )
            return before, after
        finally:
            await db.close()

    before, after = run(_run())
    assert before == after == (0, 0)


def test_deleting_a_project_removes_its_guidestones():
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            pid, _sid = await _seeded(db)
            await guidestone_db.create_guidestone(db, pid, question="Q")
            await db_module.delete_project(db, pid)
            return await _count(db, "SELECT COUNT(*) FROM guidestones WHERE project_id = ?", (pid,))
        finally:
            await db.close()

    assert run(_run()) == 0


def test_merging_a_project_moves_its_guidestones_to_the_target():
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            source, _ssid = await _seeded(db, name="duplicate-proj")
            target, _tsid = await _seeded(db, name="real-proj")
            made = await guidestone_db.create_guidestone(db, source, question="Q")
            await db_module.merge_project(db, source, target)
            return made["guidestone_id"], await _count(
                db, "SELECT COUNT(*) FROM guidestones WHERE id = ? AND project_id = ?",
                (made["guidestone_id"], target),
            )
        finally:
            await db.close()

    _gid, moved = run(_run())
    assert moved == 1


# ----------------------------------------------------------------- tool contract


def _spec(name):
    return next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == name)


def test_the_three_tools_declare_the_project_name_alternative_and_classification():
    for name in ("create_guidestone", "list_guidestones", "load_guidestone"):
        spec = _spec(name)
        props = spec["inputSchema"]["properties"]
        assert "project_id" in props and "project_name" in props
        assert "alternative to project_id" in props["project_name"]["description"]
        assert spec["inputSchema"]["required"] == []
        assert mcp_tools._TOOL_CATEGORY[name] == "research"
        assert mcp_tools._TOOL_ROLE_RELEVANCE[name] == "both"
        assert name in mcp_tools._TOOL_EXAMPLES
    assert mcp_tools._TOOL_WORKFLOW_TIER["create_guidestone"] == "main-workflow"
    assert mcp_tools._TOOL_WORKFLOW_TIER["list_guidestones"] == "common-support"
    assert mcp_tools._TOOL_WORKFLOW_TIER["load_guidestone"] == "common-support"


def test_only_the_read_tools_are_marked_read_only():
    assert "list_guidestones" in mcp_tools._READ_ONLY_TOOLS
    assert "load_guidestone" in mcp_tools._READ_ONLY_TOOLS
    assert "create_guidestone" not in mcp_tools._READ_ONLY_TOOLS
    assert "Read-only" not in _spec("create_guidestone")["description"]


# ----------------------------------------------------------------- handlers


def test_the_dispatcher_routes_the_three_tools_and_resolves_project_name(tmp_path):
    from meridian import server as mh  # noqa: PLC0415

    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            out = str(tmp_path)
            pid = (await mh._dispatch_mcp_tool("create_project", {"name": "dispatch-proj"}, db, out))["id"]
            created = await mh._dispatch_mcp_tool(
                "create_guidestone", {"project_id": pid, "question": "Q"}, db, out,
            )
            by_name = await mh._dispatch_mcp_tool(
                "create_guidestone", {"project_name": "dispatch-proj", "question": "Q2"}, db, out,
            )
            listed = await mh._dispatch_mcp_tool("list_guidestones", {"project_name": "dispatch-proj"}, db, out)
            loaded = await mh._dispatch_mcp_tool(
                "load_guidestone", {"project_id": pid, "guidestone_id": created["guidestone_id"]}, db, out,
            )
            return created, by_name, listed, loaded
        finally:
            await db.close()

    created, by_name, listed, loaded = run(_run())
    assert "error" not in created and "error" not in by_name
    assert listed["count"] == 2
    assert loaded["guidestone"]["guidestone_id"] == created["guidestone_id"]


def test_the_handlers_round_trip_and_refuse_a_missing_project():
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            pid, _sid = await _seeded(db)
            created = await handle_create_guidestone({"project_id": pid, "question": "Q"}, db, "", None, None)
            listed = await handle_list_guidestones({"project_id": pid}, db, "", None, None)
            loaded = await handle_load_guidestone(
                {"project_id": pid, "guidestone_id": created["guidestone_id"]}, db, "", None, None,
            )
            no_project = await handle_create_guidestone({}, db, "", None, None)
            missing = await handle_load_guidestone({"project_id": pid, "guidestone_id": "nope"}, db, "", None, None)
            refused = await handle_create_guidestone({"project_id": pid, "question": SECRET_SHAPED}, db, "", None, None)
            return created, listed, loaded, no_project, missing, refused
        finally:
            await db.close()

    created, listed, loaded, no_project, missing, refused = run(_run())
    assert created["guidestone_id"] and created["counts"]["findings"] == 0
    assert listed["count"] == 1 and listed["guidestones"][0]["guidestone_id"] == created["guidestone_id"]
    assert loaded["guidestone"]["guidestone_id"] == created["guidestone_id"]
    assert "project_id is required" in no_project["error"]
    assert missing == {"error": "no guidestone found for this project"}
    assert "secret-shaped" in refused["error"]
