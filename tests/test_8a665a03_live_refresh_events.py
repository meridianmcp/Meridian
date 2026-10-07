"""8a665a03 -- every mutation publishes one WebSocket event.

Owner bug: permanently deleting a Backburner item in the dashboard's Queue tab did not
live-refresh. The DELETE route ran a raw ``DELETE FROM sprint_items`` and published
nothing, and the client had no handler that would have repainted the Queue tab anyway.
The audit that followed found the same shape -- a write that changes what a list view
shows but never tells it -- for sprint-item edits and splits, Devlog task deletes, note
edits/deletes, pinned-decision edits/deletes and session close/idle.

Each test here fails on the pre-fix code (no event is queued) and pins the event's
type and payload, because the client routes on them (see handleWsEvent in
meridian/static/dashboard.ts and meridian/static/live-refresh.test.ts).
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import pytest

from meridian import db as db_module


def _drain(q: asyncio.Queue) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while True:
        try:
            out.append(q.get_nowait())
        except asyncio.QueueEmpty:
            return out


def _of_type(events: list[dict[str, Any]], type_: str) -> list[dict[str, Any]]:
    return [e for e in events if e.get("type") == type_]


class _Subscription:
    """Subscribe to a project's WebSocket event stream for the duration of a block."""

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self.q = db_module.subscribe_tasks(project_id)

    def __enter__(self) -> "_Subscription":
        return self

    def __exit__(self, *exc: object) -> None:
        db_module.unsubscribe_tasks(self.project_id, self.q)

    def events(self) -> list[dict[str, Any]]:
        return _drain(self.q)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _make_project(client) -> str:
    r = client.post("/projects", json={"name": f"live-{os.urandom(4).hex()}"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_item(client, pid: str, title: str = "Task", version: str = "v1.0") -> dict[str, Any]:
    r = client.post(f"/projects/{pid}/sprint-items", json={"title": title, "version": version})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1. DELETE /projects/{pid}/sprint-items/{id} -- the owner-reported bug
# ---------------------------------------------------------------------------


def test_delete_sprint_item_route_publishes_sprint_item_deleted(client):
    pid = _make_project(client)
    item = _make_item(client, pid, "backburner me")
    with _Subscription(pid) as sub:
        r = client.delete(f"/projects/{pid}/sprint-items/{item['id']}")
        assert r.status_code == 204
        events = sub.events()
    deleted = _of_type(events, "sprint_item_deleted")
    assert deleted == [{"type": "sprint_item_deleted", "project_id": pid, "item_id": item["id"]}]
    assert not any(i["id"] == item["id"] for i in client.get(f"/projects/{pid}/sprint-items").json())


def test_delete_sprint_item_route_busts_the_sprint_items_cache(client):
    pid = _make_project(client)
    item = _make_item(client, pid)
    db_module._SPRINT_ITEMS_CACHE[pid] = (time.monotonic(), [{"id": item["id"]}])
    try:
        assert client.delete(f"/projects/{pid}/sprint-items/{item['id']}").status_code == 204
        assert pid not in db_module._SPRINT_ITEMS_CACHE
    finally:
        db_module._SPRINT_ITEMS_CACHE.pop(pid, None)


def test_delete_of_an_unknown_item_is_still_204_and_publishes_nothing(client):
    pid = _make_project(client)
    with _Subscription(pid) as sub:
        assert client.delete(f"/projects/{pid}/sprint-items/does-not-exist").status_code == 204
        assert _of_type(sub.events(), "sprint_item_deleted") == []


def test_delete_through_another_projects_url_removes_nothing_and_publishes_nothing(client):
    a = _make_project(client)
    b = _make_project(client)
    item = _make_item(client, a)
    with _Subscription(a) as sub_a, _Subscription(b) as sub_b:
        assert client.delete(f"/projects/{b}/sprint-items/{item['id']}").status_code == 204
        assert _of_type(sub_a.events(), "sprint_item_deleted") == []
        assert _of_type(sub_b.events(), "sprint_item_deleted") == []
    assert any(i["id"] == item["id"] for i in client.get(f"/projects/{a}/sprint-items").json())


@pytest.mark.asyncio
async def test_delete_sprint_item_function_reports_whether_a_row_was_removed(db):
    p = await db_module.create_project(db, "live-del-fn")
    other = await db_module.create_project(db, "live-del-fn-other")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "to delete")
    assert await db_module.delete_sprint_item(db, other["id"], item["id"]) is False
    assert await db_module.delete_sprint_item(db, p["id"], item["id"]) is True
    assert await db_module.delete_sprint_item(db, p["id"], item["id"]) is False


# ---------------------------------------------------------------------------
# 2. PATCH /projects/{pid}/sprint-items/{id} -- edits, notes, resources, feedback
# ---------------------------------------------------------------------------


def test_patch_sprint_item_fields_publishes_sprint_item_updated(client):
    pid = _make_project(client)
    item = _make_item(client, pid, "old title")
    with _Subscription(pid) as sub:
        r = client.patch(f"/projects/{pid}/sprint-items/{item['id']}", json={"title": "new title", "notes": "n"})
        assert r.status_code == 200
        updated = _of_type(sub.events(), "sprint_item_updated")
    assert len(updated) == 1
    assert updated[0]["item_id"] == item["id"]
    assert set(updated[0]["fields"]) == {"title", "notes"}
    # The payload names the changed fields, never their values (notes can be long).
    assert "n" not in updated[0].values() and "new title" not in updated[0].values()


def test_patch_with_a_status_change_publishes_one_event_not_two(client):
    pid = _make_project(client)
    item = _make_item(client, pid)
    with _Subscription(pid) as sub:
        r = client.patch(f"/projects/{pid}/sprint-items/{item['id']}", json={"title": "t2", "status": "pending"})
        assert r.status_code == 200
        updated = _of_type(sub.events(), "sprint_item_updated")
    assert len(updated) == 1
    assert updated[0]["status"] == "pending"


def test_patch_that_changes_nothing_publishes_nothing(client):
    pid = _make_project(client)
    item = _make_item(client, pid)
    with _Subscription(pid) as sub:
        r = client.patch(f"/projects/{pid}/sprint-items/{item['id']}", json={})
        assert r.status_code == 200
        assert sub.events() == []


@pytest.mark.asyncio
async def test_patch_sprint_item_busts_the_sprint_items_cache(db):
    p = await db_module.create_project(db, "live-patch-cache")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "cached")
    db_module._SPRINT_ITEMS_CACHE[p["id"]] = (time.monotonic(), [])
    try:
        await db_module.patch_sprint_item(db, p["id"], item["id"], title="renamed")
        assert p["id"] not in db_module._SPRINT_ITEMS_CACHE
    finally:
        db_module._SPRINT_ITEMS_CACHE.pop(p["id"], None)


# ---------------------------------------------------------------------------
# 3. split_sprint_item -- the children are raw INSERTs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_split_publishes_the_new_children_after_they_exist(db):
    p = await db_module.create_project(db, "live-split")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "big item")
    q = db_module.subscribe_tasks(p["id"])
    try:
        children = await db_module.split_sprint_item(db, p["id"], item["id"], ["part a", "part b"])
        events = _drain(q)
    finally:
        db_module.unsubscribe_tasks(p["id"], q)
    added = _of_type(events, "sprint_item_added")
    assert len(added) == 1
    assert added[0]["split_from"] == item["id"]
    assert added[0]["item_ids"] == [c["id"] for c in children]
    # Ordering: the original's skip event comes first, the children's announcement last.
    types = [e["type"] for e in events]
    assert types.index("sprint_item_updated") < types.index("sprint_item_added")


# ---------------------------------------------------------------------------
# 4. DELETE /tasks/{id} -- Devlog hard delete
# ---------------------------------------------------------------------------


def _make_task(client, pid: str) -> dict[str, Any]:
    sess = client.post("/sessions/register", json={"project_id": pid, "name": "live-sess"}).json()
    r = client.post("/tasks", json={"session_id": sess["id"], "project_id": pid, "description": "did work", "status": "done"})
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_delete_task_route_publishes_task_deleted(client):
    pid = _make_project(client)
    task = _make_task(client, pid)
    with _Subscription(pid) as sub:
        assert client.delete(f"/tasks/{task['id']}").status_code == 204
        events = sub.events()
    assert _of_type(events, "task_deleted") == [
        {"type": "task_deleted", "project_id": pid, "task_id": task["id"]}
    ]


def test_delete_of_an_unknown_task_is_204_and_silent(client):
    pid = _make_project(client)
    with _Subscription(pid) as sub:
        assert client.delete("/tasks/does-not-exist").status_code == 204
        assert _of_type(sub.events(), "task_deleted") == []


# ---------------------------------------------------------------------------
# 5. notes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_project_note_publishes_note_updated(db):
    p = await db_module.create_project(db, "live-note-upd")
    note = await db_module.add_project_note(db, p["id"], "t", "body")
    q = db_module.subscribe_tasks(p["id"])
    try:
        _drain(q)  # drop the note_added from the setup call
        await db_module.update_project_note(db, note["id"], body="edited")
        events = _drain(q)
    finally:
        db_module.unsubscribe_tasks(p["id"], q)
    assert events == [{"type": "note_updated", "project_id": p["id"], "note_id": note["id"]}]


@pytest.mark.asyncio
async def test_no_op_note_update_and_a_foreign_project_bind_publish_nothing(db):
    p = await db_module.create_project(db, "live-note-noop")
    other = await db_module.create_project(db, "live-note-noop-other")
    note = await db_module.add_project_note(db, p["id"], "t", "body")
    q = db_module.subscribe_tasks(p["id"])
    try:
        _drain(q)
        await db_module.update_project_note(db, note["id"])  # nothing to change
        await db_module.update_project_note(db, note["id"], body="x", project_id=other["id"])
        assert _drain(q) == []
    finally:
        db_module.unsubscribe_tasks(p["id"], q)


@pytest.mark.asyncio
async def test_delete_project_note_publishes_note_deleted_with_or_without_a_project_id(db):
    p = await db_module.create_project(db, "live-note-del")
    n1 = await db_module.add_project_note(db, p["id"], "one", "b")
    n2 = await db_module.add_project_note(db, p["id"], "two", "b")
    q = db_module.subscribe_tasks(p["id"])
    try:
        _drain(q)
        assert await db_module.delete_project_note(db, n1["id"], project_id=p["id"]) is True
        assert await db_module.delete_project_note(db, n2["id"]) is True  # the MCP path names no project
        events = _drain(q)
    finally:
        db_module.unsubscribe_tasks(p["id"], q)
    assert events == [
        {"type": "note_deleted", "project_id": p["id"], "note_id": n1["id"]},
        {"type": "note_deleted", "project_id": p["id"], "note_id": n2["id"]},
    ]


@pytest.mark.asyncio
async def test_failed_note_delete_publishes_nothing(db):
    p = await db_module.create_project(db, "live-note-del-miss")
    other = await db_module.create_project(db, "live-note-del-miss-other")
    note = await db_module.add_project_note(db, p["id"], "t", "b")
    q = db_module.subscribe_tasks(p["id"])
    try:
        _drain(q)
        assert await db_module.delete_project_note(db, note["id"], project_id=other["id"]) is False
        assert await db_module.delete_project_note(db, "no-such-note") is False
        assert _drain(q) == []
    finally:
        db_module.unsubscribe_tasks(p["id"], q)


def test_note_routes_publish_through_http(client):
    pid = _make_project(client)
    note = client.post(f"/projects/{pid}/notes", json={"title": "t", "body": "b"}).json()
    with _Subscription(pid) as sub:
        r = client.patch(f"/projects/{pid}/notes/{note['id']}", json={"body": "edited"})
        assert r.status_code == 200
        assert _of_type(sub.events(), "note_updated")[0]["note_id"] == note["id"]
        assert client.delete(f"/projects/{pid}/notes/{note['id']}").status_code in (200, 204)
        assert _of_type(sub.events(), "note_deleted")[0]["note_id"] == note["id"]


# ---------------------------------------------------------------------------
# 6. pinned decisions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_pinned_decision_publishes_decision_updated(db):
    p = await db_module.create_project(db, "live-dec-upd")
    dec = await db_module.pin_decision(db, p["id"], "t", "body")
    q = db_module.subscribe_tasks(p["id"])
    try:
        _drain(q)  # decision_pinned from setup
        await db_module.update_pinned_decision(db, dec["id"], priority="urgent")
        events = _drain(q)
    finally:
        db_module.unsubscribe_tasks(p["id"], q)
    assert events == [{"type": "decision_updated", "project_id": p["id"], "decision_id": dec["id"]}]


@pytest.mark.asyncio
async def test_decision_update_that_changes_nothing_or_is_foreign_publishes_nothing(db):
    p = await db_module.create_project(db, "live-dec-noop")
    other = await db_module.create_project(db, "live-dec-noop-other")
    dec = await db_module.pin_decision(db, p["id"], "t", "body")
    q = db_module.subscribe_tasks(p["id"])
    try:
        _drain(q)
        await db_module.update_pinned_decision(db, dec["id"])
        await db_module.update_pinned_decision(db, dec["id"], body="x", project_id=other["id"])
        assert _drain(q) == []
    finally:
        db_module.unsubscribe_tasks(p["id"], q)


@pytest.mark.asyncio
async def test_delete_pinned_decision_publishes_decision_deleted_with_or_without_a_project_id(db):
    p = await db_module.create_project(db, "live-dec-del")
    d1 = await db_module.pin_decision(db, p["id"], "one", "b")
    d2 = await db_module.pin_decision(db, p["id"], "two", "b")
    q = db_module.subscribe_tasks(p["id"])
    try:
        _drain(q)
        assert await db_module.delete_pinned_decision(db, d1["id"], project_id=p["id"]) is True
        assert await db_module.delete_pinned_decision(db, d2["id"]) is True  # MCP archive_decision names no project
        events = _drain(q)
    finally:
        db_module.unsubscribe_tasks(p["id"], q)
    assert events == [
        {"type": "decision_deleted", "project_id": p["id"], "decision_id": d1["id"]},
        {"type": "decision_deleted", "project_id": p["id"], "decision_id": d2["id"]},
    ]


@pytest.mark.asyncio
async def test_failed_decision_delete_publishes_nothing(db):
    p = await db_module.create_project(db, "live-dec-del-miss")
    other = await db_module.create_project(db, "live-dec-del-miss-other")
    dec = await db_module.pin_decision(db, p["id"], "t", "b")
    q = db_module.subscribe_tasks(p["id"])
    try:
        _drain(q)
        assert await db_module.delete_pinned_decision(db, dec["id"], project_id=other["id"]) is False
        assert await db_module.delete_pinned_decision(db, "no-such-decision") is False
        assert _drain(q) == []
    finally:
        db_module.unsubscribe_tasks(p["id"], q)


def test_decision_routes_publish_through_http(client):
    pid = _make_project(client)
    dec = client.post(f"/projects/{pid}/decisions-pinned", json={"title": "t", "body": "b"}).json()
    with _Subscription(pid) as sub:
        r = client.patch(f"/projects/{pid}/decisions-pinned/{dec['id']}", json={"priority": "urgent"})
        assert r.status_code == 200
        assert _of_type(sub.events(), "decision_updated")[0]["decision_id"] == dec["id"]
        assert client.delete(f"/projects/{pid}/decisions-pinned/{dec['id']}").status_code in (200, 204)
        assert _of_type(sub.events(), "decision_deleted")[0]["decision_id"] == dec["id"]


# ---------------------------------------------------------------------------
# 7. sessions
# ---------------------------------------------------------------------------


def test_patch_session_publishes_session_updated(client):
    pid = _make_project(client)
    sess = client.post("/sessions/register", json={"project_id": pid, "name": "live-s"}).json()
    with _Subscription(pid) as sub:
        r = client.patch(f"/sessions/{sess['id']}", json={"status": "idle"})
        assert r.status_code == 200
        events = _of_type(sub.events(), "session_updated")
    assert events == [{"type": "session_updated", "project_id": pid, "session_id": sess["id"], "status": "idle"}]


@pytest.mark.asyncio
async def test_close_session_publishes_session_updated(db):
    p = await db_module.create_project(db, "live-sess-close")
    sess = await db_module.register_session(db, p["id"], "closer")
    q = db_module.subscribe_tasks(p["id"])
    try:
        _drain(q)  # session_started from setup
        await db_module.close_session(db, sess["id"])
        events = _of_type(_drain(q), "session_updated")
    finally:
        db_module.unsubscribe_tasks(p["id"], q)
    assert events == [{"type": "session_updated", "project_id": p["id"], "session_id": sess["id"], "status": "closed"}]
