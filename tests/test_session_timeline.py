"""Per-executor-session timeline (1e1bd6b0).

The crux is the derived outcome: 'stopped-ambiguously' (the session ended while a
claimed item was still in_progress — a silent stop) vs 'failed' (the item errored)
vs 'done'. Reuses existing session timestamps + sprint_items.actor/status only.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module


@pytest.mark.asyncio
async def test_get_executor_session_timeline_derives_outcomes(db):
    p = await db_module.create_project(db, "stl")
    s_closed = await db_module.register_session(db, p["id"], "sess-closed")
    s_active = await db_module.register_session(db, p["id"], "sess-active")
    await db.execute("UPDATE sessions SET status='closed' WHERE id=?", (s_closed["id"],))

    i_done = await db_module.add_sprint_item(db, p["id"], "v1", "wire the auth flow", group="alpha", force=True)
    i_failed = await db_module.add_sprint_item(db, p["id"], "v1", "migrate the billing schema", group="alpha", force=True)
    i_stopped = await db_module.add_sprint_item(db, p["id"], "v1", "refactor the parser", group="beta", force=True)
    i_running = await db_module.add_sprint_item(db, p["id"], "v1", "index the codebase", group="beta", force=True)
    await db.execute("UPDATE sprint_items SET actor=?, status='done' WHERE id=?", (s_closed["id"], i_done["id"]))
    await db.execute("UPDATE sprint_items SET actor=?, status='failed' WHERE id=?", (s_closed["id"], i_failed["id"]))
    await db.execute("UPDATE sprint_items SET actor=?, status='in_progress' WHERE id=?", (s_closed["id"], i_stopped["id"]))
    await db.execute("UPDATE sprint_items SET actor=?, status='in_progress' WHERE id=?", (s_active["id"], i_running["id"]))
    await db.commit()

    tl = await db_module.get_executor_session_timeline(db, p["id"])
    by_id = {s["id"]: s for s in tl["sessions"]}
    closed, active = by_id[s_closed["id"]], by_id[s_active["id"]]

    outcomes = {it["title"]: it["outcome"] for g in closed["groups"] for it in g["items"]}
    assert outcomes["wire the auth flow"] == "done"
    assert outcomes["migrate the billing schema"] == "failed"
    # The load-bearing distinction: an in_progress item on a CLOSED session is a
    # silent stop, not a failure.
    assert outcomes["refactor the parser"] == "stopped-ambiguously"
    assert closed["outcome_counts"]["stopped_ambiguously"] == 1
    assert closed["outcome_counts"]["done"] == 1 and closed["outcome_counts"]["failed"] == 1
    assert closed["ended_at"] is not None  # closed session has an end time
    assert {g["item_group"] for g in closed["groups"]} == {"alpha", "beta"}  # grouped by item_group

    # Same in_progress status on an ACTIVE session is NOT ambiguous — it's ongoing.
    a_outcomes = {it["title"]: it["outcome"] for g in active["groups"] for it in g["items"]}
    assert a_outcomes["index the codebase"] == "in_progress"
    assert active["ended_at"] is None


def test_session_item_outcome_unit():
    f = db_module._session_item_outcome
    assert f("done", "closed") == "done"
    assert f("failed", "active") == "failed"
    assert f("in_progress", "closed") == "stopped-ambiguously"
    assert f("in_progress", "active") == "in_progress"
    assert f("pushed", "closed") == "done"
    assert f("skipped", "closed") == "skipped"


def test_session_timeline_endpoint(client):
    pid = client.post("/projects", json={"name": "stl-ep"}).json()["id"]
    r = client.get(f"/projects/{pid}/session-timeline")
    assert r.status_code == 200
    body = r.json()
    assert body["project_id"] == pid and "sessions" in body
    assert client.get("/projects/does-not-exist/session-timeline").status_code == 404
