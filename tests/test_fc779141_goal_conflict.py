"""fc779141 -- optimistic concurrency for the three goal fields.

The Goal tab used to save the version goal / north star / current focus
last-write-wins, so a person editing in the dashboard and an agent calling
set_goal / set_north_star / set_sprint silently overwrote each other.  A caller
may now send the per-field ``updated_at`` stamp its edit was based on
(``expected_updated_at``); a stale stamp returns HTTP 409 with the CURRENT value
and changes nothing, while omitting it keeps the historical last-write-wins
behaviour exactly.  These tests pin both halves plus the ``goal_updated`` event
payload the dashboard uses to say who changed what.

Stamps have one-second resolution, so tests that need "someone else wrote since
I loaded" backdate the stored stamps (``_backdate``) instead of sleeping.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import meridian.server  # noqa: F401 -- import first to avoid the handler/server import cycle
from meridian import db as db_module
from meridian.mcp import handler as mh

OLD = "2020-01-01 00:00:00"
STALE = "2000-01-01 00:00:00"


async def _backdate(db, project_id: str) -> None:
    """Pretend every goal field was last written long ago."""
    await db.execute(
        "UPDATE goal_states SET updated_at = ?, ns_updated_at = ?, "
        "content_updated_at = ?, sprint_updated_at = ? WHERE project_id = ?",
        (OLD, OLD, OLD, OLD, project_id),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# db layer
# ---------------------------------------------------------------------------


def test_goal_conflict_is_not_a_value_error():
    # The routes and MCP handlers catch ValueError for "no goal set yet" (422);
    # a conflict must never be swallowed by that handler.
    assert not issubclass(db_module.GoalConflict, ValueError)


async def test_field_stamps_of_a_missing_goal_are_empty(db):
    assert db_module.goal_field_stamps(None) == {
        "version_goal": "", "north_star": "", "sprint": "",
    }
    p = await db_module.create_project(db, "stamps-empty")
    assert db_module.goal_field_stamps(await db_module.get_goal(db, p["id"])) == {
        "version_goal": "", "north_star": "", "sprint": "",
    }


async def test_stale_north_star_stamp_conflicts_and_writes_nothing(db):
    p = await db_module.create_project(db, "ns-conflict")
    pid = p["id"]
    await db_module.set_goal(db, pid, "g", north_star="ns0")
    await _backdate(db, pid)
    stamp = db_module.goal_field_stamps(await db_module.get_goal(db, pid))["north_star"]
    assert stamp == OLD

    # An agent changes the north star while the person is still editing.
    await db_module.set_north_star(db, pid, "ns-agent")

    with pytest.raises(db_module.GoalConflict) as ei:
        await db_module.set_north_star(db, pid, "ns-mine", expected_updated_at=stamp)
    detail = db_module.goal_conflict_detail(ei.value)
    assert detail["error"] == "goal_conflict"
    assert detail["field"] == "north_star"
    assert detail["expected_updated_at"] == OLD
    assert detail["current"]["value"] == "ns-agent"
    assert detail["current"]["updated_at"] != OLD
    assert set(detail["field_updated_at"]) == {"version_goal", "north_star", "sprint"}

    # Nothing was written by the refused call.
    assert (await db_module.get_goal(db, pid))["north_star"] == "ns-agent"


async def test_stale_sprint_stamp_conflicts_and_writes_nothing(db):
    p = await db_module.create_project(db, "sp-conflict")
    pid = p["id"]
    await db_module.set_goal(db, pid, "g", sprint="s0")
    await _backdate(db, pid)
    await db_module.set_sprint(db, pid, "s-agent")

    with pytest.raises(db_module.GoalConflict) as ei:
        await db_module.set_sprint(db, pid, "s-mine", expected_updated_at=OLD)
    assert ei.value.field == "sprint"
    assert db_module.goal_conflict_detail(ei.value)["current"]["value"] == "s-agent"
    assert (await db_module.get_goal(db, pid))["sprint"] == "s-agent"


async def test_stale_version_goal_stamp_conflicts_and_keeps_the_version(db):
    p = await db_module.create_project(db, "vg-conflict")
    pid = p["id"]
    await db_module.set_goal(db, pid, "v1 original")
    await _backdate(db, pid)
    agent = await db_module.set_goal(db, pid, "v2 by an agent")

    with pytest.raises(db_module.GoalConflict) as ei:
        await db_module.set_goal(
            db, pid, "v2 by a person", expected_updated_at={"version_goal": OLD},
        )
    detail = db_module.goal_conflict_detail(ei.value)
    assert detail["field"] == "version_goal"
    assert detail["current"]["value"] == "v2 by an agent"
    assert detail["current"]["version"] == agent["version"]

    after = await db_module.get_goal(db, pid)
    assert after["content"] == "v2 by an agent"
    assert after["version"] == agent["version"]  # no new row, no version bump


async def test_matching_stamp_is_accepted_and_returns_fresh_stamps(db):
    p = await db_module.create_project(db, "match")
    pid = p["id"]
    await db_module.set_goal(db, pid, "g", north_star="ns0")
    await _backdate(db, pid)
    stamp = db_module.goal_field_stamps(await db_module.get_goal(db, pid))["north_star"]

    saved = await db_module.set_north_star(db, pid, "ns1", expected_updated_at=stamp)
    assert saved["north_star"] == "ns1"
    assert saved["field_updated_at"]["north_star"] != stamp
    assert saved["field_updated_at"] == db_module.goal_field_stamps(
        await db_module.get_goal(db, pid)
    )


async def test_a_change_to_another_field_does_not_conflict(db):
    p = await db_module.create_project(db, "other-field")
    pid = p["id"]
    await db_module.set_goal(db, pid, "g", north_star="ns0", sprint="s0")
    await _backdate(db, pid)
    await db_module.set_sprint(db, pid, "s-agent")  # touches only the sprint

    # The north star stamp is unchanged, so a north star save from the old base
    # is not a conflict (per-field stamps, not one row-wide token).
    saved = await db_module.set_north_star(db, pid, "ns1", expected_updated_at=OLD)
    assert saved["north_star"] == "ns1"
    assert saved["sprint"] == "s-agent"


async def test_without_a_stamp_the_last_write_still_wins(db):
    p = await db_module.create_project(db, "lww")
    pid = p["id"]
    await db_module.set_goal(db, pid, "g1", north_star="a", sprint="x")
    await _backdate(db, pid)
    await db_module.set_goal(db, pid, "g2", north_star="b", sprint="y")
    # A caller that never heard of stamps overwrites freely, exactly as before...
    last = await db_module.set_goal(db, pid, "g3", north_star="c", sprint="z")
    await db_module.set_north_star(db, pid, "d")
    await db_module.set_sprint(db, pid, "w")
    goal = await db_module.get_goal(db, pid)
    assert (goal["content"], goal["north_star"], goal["sprint"]) == ("g3", "d", "w")
    # ...and now gets the stamps it could have used back, additively.
    assert set(last["field_updated_at"]) == {"version_goal", "north_star", "sprint"}


async def test_unknown_field_name_is_rejected(db):
    p = await db_module.create_project(db, "bad-field")
    with pytest.raises(ValueError):
        await db_module.set_goal(db, p["id"], "g", expected_updated_at={"nope": "x"})


async def test_two_saves_from_one_base_have_exactly_one_winner(db):
    p = await db_module.create_project(db, "race")
    pid = p["id"]
    await db_module.set_goal(db, pid, "g", north_star="ns0")
    await _backdate(db, pid)

    results = await asyncio.gather(
        db_module.set_north_star(db, pid, "first", expected_updated_at=OLD),
        db_module.set_north_star(db, pid, "second", expected_updated_at=OLD),
        return_exceptions=True,
    )
    winners = [r for r in results if isinstance(r, dict)]
    losers = [r for r in results if isinstance(r, db_module.GoalConflict)]
    assert len(winners) == 1 and len(losers) == 1
    assert (await db_module.get_goal(db, pid))["north_star"] == winners[0]["north_star"]


async def test_north_star_save_cannot_revert_a_concurrent_version_goal_save(db):
    # set_north_star re-writes the stored content it just read; without the
    # per-project write lock a version-goal save landing between the read and
    # the write was silently reverted by this stale copy.
    p = await db_module.create_project(db, "revert")
    pid = p["id"]
    await db_module.set_goal(db, pid, "old content", north_star="ns0")

    await asyncio.gather(
        db_module.set_goal(db, pid, "new content"),
        db_module.set_north_star(db, pid, "ns1"),
    )
    goal = await db_module.get_goal(db, pid)
    assert goal["north_star"] == "ns1"
    assert goal["content"] == "new content"


async def test_goal_updated_event_names_changed_fields_and_actor(db):
    p = await db_module.create_project(db, "event")
    pid = p["id"]
    q = db_module.subscribe_tasks(pid)
    try:
        await db_module.set_goal(
            db, pid, "v1", north_star="ns", actor=db_module.goal_actor("mcp"),
        )
        first = q.get_nowait()
        assert first["type"] == "goal_updated"
        assert first["changed_fields"] == ["version_goal", "north_star"]
        assert first["changed_by"] == {"kind": "agent", "source": "mcp", "id": None}
        assert first["version"] == 1  # the pre-existing key is kept
        assert first["updated_at"]
        assert set(first["field_updated_at"]) == {"version_goal", "north_star", "sprint"}

        await db_module.set_sprint(
            db, pid, "focus", actor=db_module.goal_actor("dashboard", "adam"),
        )
        second = q.get_nowait()
        assert second["changed_fields"] == ["sprint"]
        assert second["changed_by"] == {"kind": "human", "source": "dashboard", "id": "adam"}

        # A write that does not say who made it still reports what changed.
        await db_module.set_north_star(db, pid, "ns2")
        third = q.get_nowait()
        assert third["changed_fields"] == ["north_star"]
        assert third["changed_by"] is None
    finally:
        db_module.unsubscribe_tasks(pid, q)


def test_goal_actor_maps_sources_to_kinds():
    assert db_module.goal_actor(None) is None
    assert db_module.goal_actor("dashboard")["kind"] == "human"
    assert db_module.goal_actor("goal_md")["kind"] == "human"
    assert db_module.goal_actor("mcp")["kind"] == "agent"
    assert db_module.goal_actor("api")["kind"] == "unknown"
    assert db_module.goal_actor("MCP ")["source"] == "mcp"
    assert db_module.goal_actor(None, "sess-1") == {"kind": "unknown", "source": None, "id": "sess-1"}
    # Display-only labels are bounded, never trusted for length.
    assert len(db_module.goal_actor("x" * 500, "y" * 500)["source"]) <= 32
    assert len(db_module.goal_actor("x", "y" * 500)["id"]) <= 128


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


def _project(client, name: str) -> str:
    r = client.post("/projects", json={"name": name})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_get_goal_exposes_the_field_stamps(client):
    pid = _project(client, "http-stamps")
    empty = client.get(f"/projects/{pid}/goal").json()
    assert empty["field_updated_at"] == {"version_goal": "", "north_star": "", "sprint": ""}

    client.post(f"/projects/{pid}/goal", json={"content": "go", "north_star": "ns", "sprint": "s"})
    body = client.get(f"/projects/{pid}/goal").json()
    assert set(body["field_updated_at"]) == {"version_goal", "north_star", "sprint"}
    assert all(body["field_updated_at"].values())


def test_http_north_star_409_returns_the_current_value(client):
    pid = _project(client, "http-ns-409")
    client.post(f"/projects/{pid}/goal", json={"content": "go", "north_star": "theirs"})

    r = client.post(
        f"/projects/{pid}/goal/north-star",
        json={"north_star": "mine", "human_id": "adam", "expected_updated_at": STALE},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "goal_conflict"
    assert detail["field"] == "north_star"
    assert detail["current"]["value"] == "theirs"
    assert detail["expected_updated_at"] == STALE
    assert detail["current"]["updated_at"]
    assert client.get(f"/projects/{pid}/goal").json()["north_star"] == "theirs"


def test_http_sprint_409_returns_the_current_value(client):
    pid = _project(client, "http-sp-409")
    client.post(f"/projects/{pid}/goal", json={"content": "go", "sprint": "theirs"})

    r = client.post(
        f"/projects/{pid}/goal/sprint",
        json={"sprint": "mine", "expected_updated_at": STALE},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["field"] == "sprint"
    assert detail["current"]["value"] == "theirs"
    assert client.get(f"/projects/{pid}/goal").json()["sprint"] == "theirs"


def test_http_version_goal_409_returns_the_current_value(client):
    pid = _project(client, "http-vg-409")
    client.post(f"/projects/{pid}/goal", json={"content": "theirs"})

    r = client.post(
        f"/projects/{pid}/goal",
        json={"content": "mine", "expected_updated_at": STALE, "source": "dashboard"},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["field"] == "version_goal"
    assert detail["current"]["value"] == "theirs"
    assert client.get(f"/projects/{pid}/goal").json()["content"] == "theirs"


def test_http_fresh_stamp_saves_and_returns_new_stamps(client):
    pid = _project(client, "http-fresh")
    client.post(f"/projects/{pid}/goal", json={"content": "go", "north_star": "ns0", "sprint": "s0"})
    stamps = client.get(f"/projects/{pid}/goal").json()["field_updated_at"]

    r = client.post(
        f"/projects/{pid}/goal/north-star",
        json={"north_star": "ns1", "human_id": "adam",
              "expected_updated_at": stamps["north_star"], "source": "dashboard"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["north_star"] == "ns1"
    assert set(r.json()["field_updated_at"]) == {"version_goal", "north_star", "sprint"}

    r = client.post(
        f"/projects/{pid}/goal/sprint",
        json={"sprint": "s1", "expected_updated_at": stamps["sprint"]},
    )
    assert r.status_code == 200, r.text

    r = client.post(
        f"/projects/{pid}/goal",
        json={"content": "go 2", "expected_updated_at": stamps["version_goal"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["content"] == "go 2"


def test_http_empty_stamp_guards_a_project_with_no_goal_yet(client):
    pid = _project(client, "http-empty-stamp")
    first = client.post(f"/projects/{pid}/goal", json={"content": "one", "expected_updated_at": ""})
    assert first.status_code == 200, first.text
    # A second writer that also saw "no goal yet" is now stale.
    second = client.post(f"/projects/{pid}/goal", json={"content": "two", "expected_updated_at": ""})
    assert second.status_code == 409
    assert second.json()["detail"]["current"]["value"] == "one"

    # "Set the goal first" is still a 422, never mistaken for a conflict, even
    # when the caller sent a stamp.
    bare = _project(client, "http-no-goal")
    r = client.post(
        f"/projects/{bare}/goal/north-star",
        json={"north_star": "x", "human_id": "adam", "expected_updated_at": STALE},
    )
    assert r.status_code == 422


def test_http_without_a_stamp_the_last_write_wins_and_nothing_is_409(client):
    pid = _project(client, "http-lww")
    client.post(f"/projects/{pid}/goal", json={"content": "a", "north_star": "n1", "sprint": "s1"})
    r1 = client.post(f"/projects/{pid}/goal/north-star", json={"north_star": "n2", "human_id": "x"})
    r2 = client.post(f"/projects/{pid}/goal/sprint", json={"sprint": "s2"})
    r3 = client.post(f"/projects/{pid}/goal", json={"content": "b"})
    assert (r1.status_code, r2.status_code, r3.status_code) == (200, 200, 200)
    assert "field_updated_at" in r3.json()  # the additive field is present
    goal = client.get(f"/projects/{pid}/goal").json()
    assert (goal["content"], goal["north_star"], goal["sprint"]) == ("b", "n2", "s2")


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _tool(db, name, args):
    return _run(mh._dispatch_mcp_tool(name, args, db, "/tmp"))


def test_mcp_tools_accept_a_stamp_and_report_a_conflict():
    db = _run(db_module.init_db(":memory:"))
    try:
        pid = _tool(db, "create_project", {"name": "mcp-conflict"})["id"]
        _tool(db, "set_goal", {"project_id": pid, "content": "g"})
        got = _tool(db, "get_goal", {"project_id": pid})
        assert set(got["field_updated_at"]) == {"version_goal", "north_star", "sprint"}

        for tool, field, args in (
            ("set_goal", "version_goal", {"content": "mine"}),
            ("set_north_star", "north_star", {"north_star": "mine"}),
            ("set_sprint", "sprint", {"sprint": "mine", "force": True}),
        ):
            out = _tool(db, tool, {"project_id": pid, "expected_updated_at": STALE, **args})
            assert out["error"] == "goal_conflict", (tool, out)
            assert out["field"] == field
            assert "current" in out

        # Nothing was written by the three refused calls.
        after = _tool(db, "get_goal", {"project_id": pid})
        assert after["content"] == "g"
        assert after["north_star"] is None and after["sprint"] is None
    finally:
        _run(db.close())


def test_mcp_tools_without_a_stamp_keep_last_write_wins():
    db = _run(db_module.init_db(":memory:"))
    try:
        pid = _tool(db, "create_project", {"name": "mcp-lww"})["id"]
        _tool(db, "set_goal", {"project_id": pid, "content": "g"})
        _tool(db, "set_north_star", {"project_id": pid, "north_star": "n"})
        _tool(db, "set_sprint", {"project_id": pid, "sprint": "s", "force": True})
        goal = _tool(db, "get_goal", {"project_id": pid})
        assert (goal["content"], goal["north_star"], goal["sprint"]) == ("g", "n", "s")

        # The stamps from get_goal are accepted back by the same tools.
        stamps = goal["field_updated_at"]
        ok = _tool(db, "set_north_star", {
            "project_id": pid, "north_star": "n2", "expected_updated_at": stamps["north_star"],
        })
        assert ok["north_star"] == "n2"
    finally:
        _run(db.close())


def test_stdio_transport_mirrors_the_stamp_handling():
    # build_mcp_server()'s call_tool is a closure the suite cannot drive directly (the
    # same reason other stdio parity tests scan its source): all three goal writes must
    # pass the optional stamp through and turn a conflict into the same error dict the
    # hosted handlers return, and get_goal must hand out the stamps.
    src = (Path(__file__).parent.parent / "meridian" / "mcp" / "stdio_handler.py").read_text(
        encoding="utf-8"
    )
    assert src.count("except db_module.GoalConflict as exc:") == 3
    assert src.count("db_module.goal_conflict_detail(exc)") == 3
    assert src.count('arguments.get("expected_updated_at")') >= 3
    assert 'goal["field_updated_at"] = db_module.goal_field_stamps(goal)' in src


def test_mcp_tool_schemas_advertise_the_optional_stamp():
    from meridian import mcp_tools

    by_name = {t["name"]: t for t in mcp_tools._MCP_TOOLS_LIST}
    for name in ("set_goal", "set_north_star", "set_sprint"):
        schema = by_name[name]["inputSchema"]
        assert "expected_updated_at" in schema["properties"], name
        assert "expected_updated_at" not in schema.get("required", []), name


# ---------------------------------------------------------------------------
# Dashboard wiring (source scan, like tests/test_ui.py: the bundle is built
# from these files and the Playwright suite cannot run in every environment)
# ---------------------------------------------------------------------------


def _func_body(src: str, signature: str) -> str:
    """Text of the function whose declaration starts with ``signature`` (up to the next top-level function)."""
    start = src.index(signature)
    nxt = src.find("\nasync function ", start + len(signature))
    nxt2 = src.find("\nfunction ", start + len(signature))
    ends = [e for e in (nxt, nxt2) if e != -1]
    return src[start:min(ends) if ends else len(src)]


def test_refresh_goal_no_longer_overwrites_the_editors_directly():
    from dashboard_src import dashboard_source

    js = dashboard_source()
    body = _func_body(js, "async function refreshGoal(")
    # Server data reaches all three fields only through GoalField.applyServer...
    assert body.count(".applyServer(") == 3
    # ...and the old unconditional overwrites / dirty-class clearing are gone from the
    # path where a field is registered (the plain assignments left are the no-controller fallback).
    assert "ta.value = body.slice(editStart)" not in body
    assert "classList.remove('dirty')" not in body
    # A failed load must not blank a field being edited.
    assert "isEditing()" in body


def test_goal_saves_send_the_stamp_and_label_themselves_as_the_dashboard():
    from dashboard_src import dashboard_source

    js = dashboard_source()
    init = _func_body(js, "function initGoalFields(")
    assert init.count("expected_updated_at") >= 3  # version goal, north star, current focus
    assert "source: 'dashboard'" in init
    # The three save entry points are thin wrappers over the controllers.
    for fn, key in (("saveGoal", "version_goal"), ("saveNorthStar", "north_star"), ("saveSprint", "sprint")):
        assert f"getGoalField(projectId, '{key}')" in _func_body(js, f"async function {fn}(")


def test_goal_events_leave_guards_and_bar_styles_are_wired():
    from dashboard_src import dashboard_source

    js = dashboard_source()
    assert "noteGoalEvent(projectId, event);" in js
    assert "guardGoalLeave(project.id" in js          # leaving the Goal vtab
    assert "guardGoalLeave(t.id" in js                # closing the project tab
    assert "installGoalUnloadGuard()" in js           # closing the page
    css = (Path(__file__).parent.parent / "meridian" / "static" / "dashboard.css").read_text(encoding="utf-8")
    for needle in (".goal-conflict-bar", ".gcd-mine", ".gcd-theirs", ".goal-leave-dialog", ".goal-area.save-failed"):
        assert needle in css, needle
