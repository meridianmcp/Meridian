"""962d4480 -- PATCH /hitl/{id} publishes hitl_answered / hitl_dismissed on the project stream.

Other open dashboards repaint their HITL panels on these events. Before this change the answer
and the dismissal were silent (listed in SILENT_ROUTES), so another screen kept the card, and any
answer typed into it, until the next 10 s poll.
"""
from __future__ import annotations

import asyncio

from meridian import db as db_module


def _drain(queue: asyncio.Queue) -> list[dict]:
    events: list[dict] = []
    while True:
        try:
            events.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return events


def _hitl_events(queue: asyncio.Queue) -> list[dict]:
    return [e for e in _drain(queue) if str(e.get("type", "")).startswith("hitl_")]


def _pending_request(client, pid: str) -> str:
    r = client.post(f"/projects/{pid}/hitl", json={"question": "Ship it?"})
    assert r.status_code == 201
    return r.json()["id"]


def test_answer_publishes_hitl_answered_on_the_project_stream(client):
    pid = client.post("/projects", json={"name": "hitl-events-answer"}).json()["id"]
    hid = _pending_request(client, pid)
    queue = db_module.subscribe_tasks(pid)
    try:
        r = client.patch(f"/hitl/{hid}", json={"action": "answer", "answer": "Yes"})
        assert r.status_code == 200
        events = _hitl_events(queue)
    finally:
        db_module.unsubscribe_tasks(pid, queue)
    assert events == [{"type": "hitl_answered", "project_id": pid, "hitl_id": hid}]


def test_dismiss_publishes_hitl_dismissed_on_the_project_stream(client):
    pid = client.post("/projects", json={"name": "hitl-events-dismiss"}).json()["id"]
    hid = _pending_request(client, pid)
    queue = db_module.subscribe_tasks(pid)
    try:
        r = client.patch(f"/hitl/{hid}", json={"action": "dismiss"})
        assert r.status_code == 200
        events = _hitl_events(queue)
    finally:
        db_module.unsubscribe_tasks(pid, queue)
    assert events == [{"type": "hitl_dismissed", "project_id": pid, "hitl_id": hid}]


def test_the_event_reaches_only_the_requests_own_project(client):
    pid = client.post("/projects", json={"name": "hitl-events-own"}).json()["id"]
    other = client.post("/projects", json={"name": "hitl-events-other"}).json()["id"]
    hid = _pending_request(client, pid)
    other_queue = db_module.subscribe_tasks(other)
    try:
        assert client.patch(f"/hitl/{hid}", json={"action": "answer", "answer": "Yes"}).status_code == 200
        assert _hitl_events(other_queue) == []
    finally:
        db_module.unsubscribe_tasks(other, other_queue)


def test_an_unknown_request_publishes_nothing(client):
    pid = client.post("/projects", json={"name": "hitl-events-missing"}).json()["id"]
    queue = db_module.subscribe_tasks(pid)
    try:
        assert client.patch("/hitl/no-such-request", json={"action": "dismiss"}).status_code == 404
        assert client.patch("/hitl/no-such-request", json={"action": "answer", "answer": "x"}).status_code == 404
        assert _hitl_events(queue) == []
    finally:
        db_module.unsubscribe_tasks(pid, queue)
