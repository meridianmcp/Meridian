"""Guidestone contract tests added after adversarial review (meridian/db/guidestones.py).

Each test pins one stated contract point or one reviewed defect: secret screening on
every copied field, the caps and what they report, decisions derived from kept findings,
clamping, id type checks, the write read-back, the MCP stdio surface, and project delete.
Uses the shared async ``db`` fixture from tests/conftest.py.
"""
from __future__ import annotations

import json
import uuid

import pytest

from meridian import db as db_module
from meridian.db import experiments as experiments_db
from meridian.db import guidestones as guidestone_db
from meridian.db import research_runs as research_runs_db

# AWS's documented example key id. It matches the aws-access-key-id pattern and is not a real credential.
SECRET_SHAPED = "AKIAIOSFODNN7EXAMPLE"
PAGE = "https://example.org/paper-1"


async def _seeded(db, name="contract-proj"):
    project = await db_module.create_project(db, name)
    session = await db_module.register_session(db, project["id"], "researcher")
    return project["id"], session["id"]


async def _raw_finding(db, pid, *, title, body="Evidence.", created_at, source=PAGE, tags="finding,web"):
    note_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO project_notes (id, project_id, title, body, tags, note_kind, source, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'finding', ?, ?, ?)",
        (note_id, pid, title, body, tags, source, created_at, created_at),
    )
    await db.commit()
    return note_id


async def _loaded(db, pid, result):
    return await guidestone_db.load_guidestone(db, pid, guidestone_id=result["guidestone_id"])


@pytest.mark.asyncio
async def test_a_secret_shaped_experiment_name_is_left_out_with_a_warning(db):
    pid, sid = await _seeded(db)
    experiment = await experiments_db.create_experiment(db, pid, sid, name="Clean name", hypothesis=None)
    await db.execute("UPDATE experiments SET name = ? WHERE id = ?", (SECRET_SHAPED, experiment["id"]))
    await db.commit()

    result = await guidestone_db.create_guidestone(db, pid, question="Q", experiment_id=experiment["id"])
    loaded = await _loaded(db, pid, result)

    assert loaded["state"]["experiment"]["name"] is None
    assert "experiment name left out: secret-shaped text" in result["warnings"]
    assert SECRET_SHAPED not in json.dumps(loaded)


@pytest.mark.asyncio
async def test_a_secret_shaped_source_type_tag_is_dropped_and_the_finding_kept(db):
    pid, _sid = await _seeded(db)
    await _raw_finding(
        db, pid, title="Finding: tagged", created_at="2026-01-01 00:00:00",
        tags=f"finding,{SECRET_SHAPED}",
    )

    result = await guidestone_db.create_guidestone(db, pid, question="Q")
    loaded = await _loaded(db, pid, result)

    assert loaded["state"]["findings"][0]["title"] == "Finding: tagged"
    assert loaded["state"]["findings"][0]["source_type"] is None
    assert SECRET_SHAPED not in json.dumps(loaded)
    assert any("source type left out" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_a_secret_shaped_sprint_version_is_left_out_with_a_warning(db):
    pid, _sid = await _seeded(db)
    item = await db_module.add_sprint_item(db, pid, "v1", "Plain title")
    await db.execute("UPDATE sprint_items SET version = ? WHERE id = ?", (SECRET_SHAPED, item["id"]))
    await db.commit()

    result = await guidestone_db.create_guidestone(db, pid, question="Q")
    loaded = await _loaded(db, pid, result)

    assert loaded["state"]["next_steps"] == []
    assert f"sprint item {item['id']} left out: secret-shaped text" in result["warnings"]
    assert SECRET_SHAPED not in json.dumps(loaded)


@pytest.mark.asyncio
async def test_an_indeterminate_sprint_item_stays_in_next_steps(db):
    pid, _sid = await _seeded(db)
    done = await db_module.add_sprint_item(db, pid, "v1", "Finished work")
    check = await db_module.add_sprint_item(db, pid, "v1", "Outcome unknown, needs a human look")
    await db.execute("UPDATE sprint_items SET status = 'done' WHERE id = ?", (done["id"],))
    await db.execute("UPDATE sprint_items SET status = 'indeterminate' WHERE id = ?", (check["id"],))
    await db.commit()

    result = await guidestone_db.create_guidestone(db, pid, question="Q")
    loaded = await _loaded(db, pid, result)

    ids = [step["id"] for step in loaded["state"]["next_steps"] if step["kind"] == "sprint_item"]
    assert ids == [check["id"]]


@pytest.mark.asyncio
async def test_more_than_fifty_open_sprint_items_is_reported_not_silently_cut(db):
    pid, _sid = await _seeded(db)
    for index in range(51):
        # add_sprint_item refuses near-duplicate titles (>= 60% shared words), so each title is unique hex.
        await db_module.add_sprint_item(db, pid, "v1", uuid.uuid4().hex)

    result = await guidestone_db.create_guidestone(db, pid, question="Q")
    loaded = await _loaded(db, pid, result)

    assert len([s for s in loaded["state"]["next_steps"] if s["kind"] == "sprint_item"]) == 50
    assert any(w.startswith("more than 50 open sprint items") for w in result["warnings"])


@pytest.mark.asyncio
async def test_expired_and_completed_scratch_runs_are_left_out_and_a_live_run_is_kept(db):
    pid, sid = await _seeded(db)
    common = dict(mode="read_only", repository_id="repo-1", turn_budget=10)
    expired = await research_runs_db.start_research_run(db, pid, sid, **common)
    completed = await research_runs_db.start_research_run(db, pid, sid, **common)
    live = await research_runs_db.start_research_run(db, pid, sid, **common)
    await db.execute("UPDATE scratch_research_runs SET expires_at = ? WHERE id = ?",
                     ("2000-01-01T00:00:00+00:00", expired["id"]))
    await db.execute("UPDATE scratch_research_runs SET status = 'completed' WHERE id = ?", (completed["id"],))
    await db.commit()

    result = await guidestone_db.create_guidestone(db, pid, question="Q")
    loaded = await _loaded(db, pid, result)

    runs = [t["id"] for t in loaded["state"]["open_threads"] if t["kind"] == "scratch_run"]
    assert runs == [live["id"]]
    assert any("expired scratch run" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_an_open_experiment_run_is_copied_as_an_open_thread(db):
    pid, sid = await _seeded(db)
    experiment = await experiments_db.create_experiment(db, pid, sid, name="Replication", hypothesis=None)
    run = await experiments_db.start_experiment_run(db, pid, sid, experiment_id=experiment["id"])

    result = await guidestone_db.create_guidestone(db, pid, question="Q")
    loaded = await _loaded(db, pid, result)

    assert {"kind": "experiment_run", "id": run["id"]}.items() <= next(
        t for t in loaded["state"]["open_threads"] if t["kind"] == "experiment_run"
    ).items()
    assert loaded["state"]["counts"]["open_runs"] == 1


@pytest.mark.asyncio
async def test_findings_beyond_the_limit_are_counted_and_warned_not_dropped_silently(db):
    pid, _sid = await _seeded(db)
    for i in range(60):
        await _raw_finding(
            db, pid, title=f"Finding {i:02d}", created_at=f"2026-01-01 00:{i // 60:02d}:{i % 60:02d}",
            source=f"https://example.org/p{i}",
        )

    result = await guidestone_db.create_guidestone(db, pid, question="Q")
    loaded = await _loaded(db, pid, result)

    assert result["findings_beyond_limit"] == 10
    assert loaded["state"]["findings_beyond_limit"] == 10
    assert loaded["state"]["findings_omitted"] == 0
    assert any("beyond the findings limit" in w for w in result["warnings"])
    assert "10 beyond the findings limit" in loaded["resume_markdown"]


@pytest.mark.asyncio
async def test_findings_limit_is_clamped_to_one_through_two_hundred(db):
    pid, _sid = await _seeded(db)
    for i in range(3):
        await _raw_finding(db, pid, title=f"Finding {i}", created_at=f"2026-01-01 00:00:0{i}",
                           source=f"https://example.org/c{i}")

    low = await guidestone_db.create_guidestone(db, pid, question="Q", findings_limit=0)
    high = await guidestone_db.create_guidestone(db, pid, question="Q", findings_limit=999)

    assert low["findings_beyond_limit"] == 2
    assert high["findings_beyond_limit"] == 0


@pytest.mark.asyncio
async def test_list_limit_is_clamped_to_one_through_one_hundred(db):
    pid, _sid = await _seeded(db)
    for _ in range(105):
        await guidestone_db.create_guidestone(db, pid, question="Q")

    assert len(await guidestone_db.list_guidestones(db, pid, limit=500)) == 100
    assert len(await guidestone_db.list_guidestones(db, pid, limit=0)) == 1
    assert len(await guidestone_db.list_guidestones(db, pid)) == guidestone_db.DEFAULT_LIST_LIMIT


@pytest.mark.asyncio
async def test_a_decision_tag_naming_a_missing_decision_is_reported(db):
    pid, _sid = await _seeded(db)
    decision = await db_module.pin_decision(db, pid, "Use the 2025 cohort", "Restrict to 2025.")
    await db_module.save_finding(db, pid, "Cohort effect is about 0.3", source_url=PAGE, source_type="web",
                                 decision_id=decision["id"])
    await db.execute("DELETE FROM decisions_pinned WHERE id = ?", (decision["id"],))
    await db.commit()

    result = await guidestone_db.create_guidestone(db, pid, question="Q")
    loaded = await _loaded(db, pid, result)

    assert loaded["state"]["decisions"] == []
    assert any(f"decision {decision['id']} referenced by a finding is not in this project" in w
               for w in result["warnings"])


@pytest.mark.asyncio
async def test_a_secret_shaped_decision_title_is_left_out_with_a_warning(db):
    pid, _sid = await _seeded(db)
    decision = await db_module.pin_decision(db, pid, "Plain title", "Plain body.")
    await db.execute("UPDATE decisions_pinned SET title = ? WHERE id = ?", (SECRET_SHAPED, decision["id"]))
    await db.commit()
    await db_module.save_finding(db, pid, "Cohort effect is about 0.3", source_url=PAGE, source_type="web",
                                 decision_id=decision["id"])

    result = await guidestone_db.create_guidestone(db, pid, question="Q")
    loaded = await _loaded(db, pid, result)

    assert loaded["state"]["decisions"] == []
    assert f"decision {decision['id']} left out: secret-shaped text" in result["warnings"]
    assert SECRET_SHAPED not in json.dumps(loaded)


@pytest.mark.asyncio
async def test_a_finding_with_a_secret_shaped_source_url_is_left_out(db):
    pid, _sid = await _seeded(db)
    note_id = await _raw_finding(db, pid, title="Finding: url", created_at="2026-01-01 00:00:00",
                                 source=f"https://example.org/?k={SECRET_SHAPED}")

    result = await guidestone_db.create_guidestone(db, pid, question="Q")
    loaded = await _loaded(db, pid, result)

    assert loaded["state"]["findings"] == []
    assert f"finding {note_id} left out: secret-shaped text" in result["warnings"]
    assert SECRET_SHAPED not in json.dumps(loaded)


@pytest.mark.asyncio
async def test_no_question_and_no_experiment_is_reported_as_question_missing(db):
    pid, _sid = await _seeded(db)
    result = await guidestone_db.create_guidestone(db, pid)
    assert "question_missing" in result["warnings"]


@pytest.mark.asyncio
async def test_two_findings_on_one_url_are_one_source(db):
    pid, _sid = await _seeded(db)
    await _raw_finding(db, pid, title="Finding: a", created_at="2026-01-01 00:00:00")
    await _raw_finding(db, pid, title="Finding: b", created_at="2026-01-01 00:00:01")

    result = await guidestone_db.create_guidestone(db, pid, question="Q")
    loaded = await _loaded(db, pid, result)

    assert len(loaded["state"]["findings"]) == 2
    assert loaded["state"]["sources"] == [{"url": PAGE, "source_type": "web"}]


@pytest.mark.asyncio
async def test_a_default_name_is_a_utc_timestamp(db):
    pid, _sid = await _seeded(db)
    result = await guidestone_db.create_guidestone(db, pid, question="Q")
    assert result["name"].startswith("guidestone ") and result["name"].endswith(" UTC")


@pytest.mark.asyncio
async def test_a_long_resume_is_cut_inside_its_bound_and_says_so(db):
    pid, _sid = await _seeded(db)
    for i in range(50):
        await _raw_finding(
            db, pid, title=f"Finding {i:02d} " + "evidence " * 40, body="Short.",
            created_at=f"2026-01-01 00:{i // 60:02d}:{i % 60:02d}", source=f"https://example.org/long{i}",
        )

    result = await guidestone_db.create_guidestone(db, pid, question="Q")
    loaded = await _loaded(db, pid, result)

    assert loaded["resume_truncated"] is True
    assert len(loaded["resume_markdown"].encode("utf-8")) <= guidestone_db.MAX_RESUME_BYTES
    assert "[resume text cut at 16 KB" in loaded["resume_markdown"]


@pytest.mark.asyncio
async def test_a_non_string_id_is_refused_and_zero_is_not_treated_as_absent(db):
    pid, sid = await _seeded(db)
    experiment = await experiments_db.create_experiment(db, pid, sid, name="E", hypothesis=None)
    with pytest.raises(ValueError):
        await guidestone_db.create_guidestone(db, pid, experiment_id=0)
    with pytest.raises(ValueError):
        await guidestone_db.create_guidestone(db, pid, session_id=["x"])
    with pytest.raises(ValueError):
        await guidestone_db.load_guidestone(db, pid, guidestone_id=["x"])
    made = await guidestone_db.create_guidestone(db, pid, question="Q", experiment_id=experiment["id"])
    blank = await guidestone_db.load_guidestone(db, pid, guidestone_id="   ")
    assert blank["guidestone"]["guidestone_id"] == made["guidestone_id"], "a blank id reads as no id: the newest"


@pytest.mark.asyncio
async def test_the_frozen_state_carries_no_project_id_so_a_merge_cannot_leave_a_stale_one(db):
    pid, _sid = await _seeded(db)
    result = await guidestone_db.create_guidestone(db, pid, question="Q")
    loaded = await _loaded(db, pid, result)
    assert "project_id" not in loaded["state"]


@pytest.mark.asyncio
async def test_creating_a_guidestone_leaves_a_set_goal_and_existing_handoffs_untouched(db):
    pid, _sid = await _seeded(db)
    await db.execute("UPDATE projects SET pending_goal = ? WHERE id = ?", ("GOAL-TEXT-SENTINEL", pid))
    await db.execute(
        "INSERT INTO handoffs (id, project_id, session_id, mode, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("handoff-sentinel", pid, None, "full", "HANDOFF-BODY-SENTINEL", "2026-01-01 00:00:00"),
    )
    await db.commit()

    await guidestone_db.create_guidestone(db, pid, question="Q")

    async with db.execute("SELECT pending_goal FROM projects WHERE id = ?", (pid,)) as cur:
        assert (await cur.fetchone())[0] == "GOAL-TEXT-SENTINEL"
    async with db.execute("SELECT body FROM handoffs WHERE project_id = ?", (pid,)) as cur:
        rows = await cur.fetchall()
    assert [tuple(r)[0] for r in rows] == ["HANDOFF-BODY-SENTINEL"]


@pytest.mark.asyncio
async def test_a_failed_readback_raises_and_is_never_reported_as_saved(db, monkeypatch):
    pid, _sid = await _seeded(db)
    real_execute = db.execute

    class _Pending:
        def __await__(self):
            return iter(())

    def dropping_execute(sql, params=()):
        if sql.lstrip().startswith("INSERT INTO guidestones"):
            return _Pending()  # the insert silently does not happen
        return real_execute(sql, params)

    monkeypatch.setattr(db, "execute", dropping_execute)
    with pytest.raises(RuntimeError, match="was not saved"):
        await guidestone_db.create_guidestone(db, pid, question="Q")


@pytest.mark.asyncio
async def test_deleting_a_project_removes_its_snapshots_even_with_foreign_keys_off(db):
    pid, _sid = await _seeded(db)
    await guidestone_db.create_guidestone(db, pid, question="Q")
    await db.execute("PRAGMA foreign_keys = OFF")
    await db_module.delete_project(db, pid)
    async with db.execute("SELECT COUNT(*) FROM guidestones WHERE project_id = ?", (pid,)) as cur:
        assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_the_three_tools_are_advertised_and_routed_over_stdio(db, monkeypatch):
    import mcp.types as mcp_types

    import meridian.server as server_module

    async def _return_db(*_a, **_k):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    server, _run_stdio = server_module.build_mcp_server()

    listed = await server.request_handlers[mcp_types.ListToolsRequest](mcp_types.ListToolsRequest())
    names = {tool.name for tool in listed.root.tools}
    assert {"create_guidestone", "list_guidestones", "load_guidestone"} <= names

    pid, _sid = await _seeded(db, name="stdio-proj")
    called = await server.request_handlers[mcp_types.CallToolRequest](
        mcp_types.CallToolRequest(
            params=mcp_types.CallToolRequestParams(
                name="create_guidestone", arguments={"project_id": pid, "question": "Q"},
            )
        )
    )
    payload = json.loads(called.root.content[0].text)
    assert payload.get("guidestone_id"), payload


@pytest.mark.asyncio
async def test_an_experiment_from_another_project_is_refused_as_not_found(db):
    """The proof behind the scope-guard 'bound' exemption for (create_guidestone, experiment_id)."""
    pid, _sid = await _seeded(db, name="owner-proj")
    other_pid, other_sid = await _seeded(db, name="stranger-proj")
    foreign = await experiments_db.create_experiment(db, other_pid, other_sid, name="Foreign", hypothesis=None)
    with pytest.raises(ValueError, match="not found in this project"):
        await guidestone_db.create_guidestone(db, pid, question="Q", experiment_id=foreign["id"])
    async with db.execute("SELECT COUNT(*) FROM guidestones WHERE project_id = ?", (pid,)) as cur:
        assert (await cur.fetchone())[0] == 0
