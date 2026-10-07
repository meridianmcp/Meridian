"""8a665a03 follow-up -- the writers the first pass of the live-view rule missed.

The first pass (tests/test_8a665a03_live_refresh_events.py) covered the dashboard's REST
routes. Independent verification then found the same bug class on paths no dashboard
button reaches and no route-level scan can see:

* the PROJECT LIST: creating / deleting / merging / renaming a project told no other open
  dashboard (``projects_changed`` / ``project_deleted`` / ``project_merged``);
* sprint-item writers that are only reachable from MCP tools, background sweeps or batch
  rollback (``add_subtask``, ``move_sprint_item_to_project``, ``clear_stale_claim_metadata``,
  ``link_sprint_item_github_issue``, a batch rollback's compensating delete, the stale-session
  sweeps, ``release_task``, a claim's resource amendment, the handoff's inferred resources).

Each test fails on the code before this change (nothing is queued) and pins the event's type
and payload, because the client routes on them (``handleWsEvent`` / ``handleAccountEvent`` in
``meridian/static/dashboard.ts`` and ``meridian/static/live-refresh.test.ts``).

The project LIST rides the ACCOUNT stream (``db.subscribe_account`` -> ``/ws-account``), not
the per-project streams: a dashboard with no project tab open holds no per-project socket, so
the first version of this announcement (fanned out to the projects' own streams) never reached
exactly the dashboards that needed it -- the last tab closed, a brand-new account.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace
from typing import Any

import pytest

from meridian import db as db_module
from meridian.db import batch_management as bm


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


class _AccountSubscription:
    """Subscribe to the account stream (project-list events) of a database for a block."""

    def __init__(self, database: Any) -> None:
        self.database = database
        self.q = db_module.subscribe_account(database)

    def __enter__(self) -> "_AccountSubscription":
        return self

    def __exit__(self, *exc: object) -> None:
        db_module.unsubscribe_account(self.database, self.q)

    def events(self) -> list[dict[str, Any]]:
        return _drain(self.q)


def _prime_cache(project_id: str) -> None:
    db_module._SPRINT_ITEMS_CACHE[project_id] = (time.monotonic(), [])


def _cache_busted(project_id: str) -> bool:
    busted = project_id not in db_module._SPRINT_ITEMS_CACHE
    db_module._SPRINT_ITEMS_CACHE.pop(project_id, None)
    return busted


# ---------------------------------------------------------------------------
# 1. The project list: create / delete / merge / rename / reparent / status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_project_announces_projects_changed_on_the_account_stream(db):
    """Subscribed BEFORE any project exists: the empty account is the dashboard that has no
    project tab and so no per-project socket, and it must still hear the first project."""
    with _AccountSubscription(db) as acct:
        created = await db_module.create_project(db, "list-first-secret-name")
        events = acct.events()
    assert events == [{"type": "projects_changed", "change": "created"}]
    # The event is a nudge to refetch GET /projects (which applies the caller's scoping):
    # it carries neither the new project's name nor its id.
    assert "list-first-secret-name" not in json.dumps(events)
    assert created["id"] not in json.dumps(events)


@pytest.mark.asyncio
async def test_projects_changed_is_not_also_published_on_the_project_streams(db):
    """One carrier per event: the page's account socket delivers the project list, so a page
    with N project tabs does not hear every change N + 1 times."""
    first = await db_module.create_project(db, "carrier-first")
    with _Subscription(first["id"]) as sub, _AccountSubscription(db) as acct:
        await db_module.create_project(db, "carrier-second")
        await db_module.rename_project(db, first["id"], "carrier-first-renamed")
        assert _of_type(sub.events(), "projects_changed") == []
        assert [e["change"] for e in acct.events()] == ["created", "renamed"]


@pytest.mark.asyncio
async def test_projects_changed_never_reaches_a_socket_reading_another_database(db):
    """On hosted Meridian every tenant's listeners share one process. The account stream is
    keyed by the DATABASE the caller reads, so a socket on another tenant's database hears
    nothing -- unlike publish_global -- and vice versa."""
    other = await db_module.init_db(":memory:")
    try:
        with _AccountSubscription(db) as mine, _AccountSubscription(other) as theirs:
            first = await db_module.create_project(db, "scope-first")
            await db_module.rename_project(db, first["id"], "scope-first-renamed")
            assert [e["change"] for e in mine.events()] == ["created", "renamed"]
            assert theirs.events() == []
            await db_module.create_project(other, "their-project")
            assert [e["change"] for e in theirs.events()] == ["created"]
            assert mine.events() == []
    finally:
        await other.close()


@pytest.mark.asyncio
async def test_account_stream_registry_unsubscribes_and_is_safe_to_unsubscribe_twice(db):
    """A closed socket must stop receiving (and stop keeping the database object alive)."""
    q = db_module.subscribe_account(db)
    db_module.unsubscribe_account(db, q)
    db_module.unsubscribe_account(db, q)  # idempotent
    await db_module.create_project(db, "after-unsubscribe")
    assert _drain(q) == []
    assert id(db) not in db_module._ACCOUNT_LISTENERS


@pytest.mark.asyncio
async def test_a_wedged_account_socket_never_blocks_or_breaks_the_writer(db):
    """A queue that refuses the event (a socket that stopped reading) drops it and warns;
    the mutation itself still succeeds."""

    class _Full(asyncio.Queue):
        def put_nowait(self, item):  # noqa: D401 - test double
            raise asyncio.QueueFull

    wedged = _Full()
    db_module._ACCOUNT_LISTENERS.setdefault(id(db), (db, set()))[1].add(wedged)
    try:
        created = await db_module.create_project(db, "wedged-socket")
        assert created["name"] == "wedged-socket"
    finally:
        db_module.unsubscribe_account(db, wedged)


def test_create_project_route_publishes_projects_changed(client):
    with _AccountSubscription(client.app.state.db) as acct:
        r = client.post("/projects", json={"name": f"list-{os.urandom(3).hex()}"})
        assert r.status_code == 201
        assert [e["change"] for e in acct.events()] == ["created"]


def test_delete_project_route_tells_the_deleted_stream_and_the_survivors(client):
    keep = client.post("/projects", json={"name": f"keep-{os.urandom(3).hex()}"}).json()
    gone = client.post("/projects", json={"name": f"gone-{os.urandom(3).hex()}"}).json()
    with (
        _Subscription(keep["id"]) as sub_keep,
        _Subscription(gone["id"]) as sub_gone,
        _AccountSubscription(client.app.state.db) as acct,
    ):
        assert client.delete(f"/projects/{gone['id']}").status_code == 204
        keep_events = sub_keep.events()
        gone_events = sub_gone.events()
        account_events = acct.events()
    # Its own tab learns the project is gone (the account stream cannot say WHICH project:
    # the event names none, so the tab showing it closes on this one)...
    assert _of_type(gone_events, "project_deleted") == [{"type": "project_deleted", "project_id": gone["id"]}]
    # ...and every page, tab or no tab, refetches the list.
    assert account_events == [{"type": "projects_changed", "change": "deleted"}]
    assert _of_type(keep_events, "project_deleted") == []
    assert _of_type(keep_events, "projects_changed") == []


def test_batch_delete_route_announces_every_deleted_project(client):
    keep = client.post("/projects", json={"name": f"bkeep-{os.urandom(3).hex()}"}).json()
    a = client.post("/projects", json={"name": f"ba-{os.urandom(3).hex()}"}).json()
    b = client.post("/projects", json={"name": f"bb-{os.urandom(3).hex()}"}).json()
    assert keep["id"]
    with (
        _Subscription(a["id"]) as sub_a,
        _Subscription(b["id"]) as sub_b,
        _AccountSubscription(client.app.state.db) as acct,
    ):
        r = client.delete(f"/projects?project_id={a['id']}&project_id={b['id']}")
        assert r.status_code == 200
        assert [e["project_id"] for e in _of_type(sub_a.events(), "project_deleted")] == [a["id"]]
        assert [e["project_id"] for e in _of_type(sub_b.events(), "project_deleted")] == [b["id"]]
        assert len(_of_type(acct.events(), "projects_changed")) == 1


@pytest.mark.asyncio
async def test_a_refused_delete_publishes_nothing(db):
    """An in_progress task refuses the delete: the project stays and every view is as it was."""
    p = await db_module.create_project(db, "busy-project")
    other = await db_module.create_project(db, "busy-bystander")
    s = await db_module.register_session(db, p["id"], "busy-s")
    task = await db_module.log_task(db, s["id"], p["id"], "in flight", "pending")
    await db_module.claim_task(db, task["id"], s["id"])
    with _Subscription(p["id"]) as sub, _Subscription(other["id"]) as sub_other, _AccountSubscription(db) as acct:
        _drain(sub.q)
        _drain(sub_other.q)
        _drain(acct.q)
        with pytest.raises(ValueError):
            await db_module.delete_project(db, p["id"])
        assert sub.events() == [] and sub_other.events() == [] and acct.events() == []
    assert await db_module.get_project(db, p["id"]) is not None


@pytest.mark.asyncio
async def test_mcp_path_create_rename_reparent_status_all_announce(db):
    """The MCP tools call the db functions directly (no route), so the announcement has to
    live at the db layer."""
    anchor = await db_module.create_project(db, "mcp-anchor")
    other = await db_module.create_project(db, "mcp-other")
    child = await db_module.create_project(db, "mcp-child")
    assert anchor["id"]
    with _AccountSubscription(db) as acct:
        await db_module.rename_project(db, other["id"], "mcp-other-renamed")
        await db_module.set_parent_project(db, child["id"], other["id"])
        await db_module.set_project_status(db, other["id"], status="parked")
        await db_module.set_project_status(db, other["id"])  # nothing to change: silent
        changes = [e["change"] for e in _of_type(acct.events(), "projects_changed")]
    assert changes == ["renamed", "reparented", "organization"]


@pytest.mark.asyncio
async def test_merge_project_resyncs_both_streams_and_busts_both_caches(db):
    src = await db_module.create_project(db, "merge-src")
    tgt = await db_module.create_project(db, "merge-tgt")
    await db_module.add_sprint_item(db, src["id"], "v1", "moves with the merge")
    _prime_cache(src["id"])
    _prime_cache(tgt["id"])
    with _Subscription(src["id"]) as sub_src, _Subscription(tgt["id"]) as sub_tgt, _AccountSubscription(db) as acct:
        result = await db_module.merge_project(db, src["id"], tgt["id"])
        assert "error" not in result
        src_events = sub_src.events()
        tgt_events = sub_tgt.events()
        account_events = acct.events()
    for pid, events in ((src["id"], src_events), (tgt["id"], tgt_events)):
        assert _of_type(events, "project_merged") == [
            {
                "type": "project_merged", "project_id": pid,
                "source_project_id": src["id"], "target_project_id": tgt["id"],
            }
        ]
    assert [e["change"] for e in _of_type(account_events, "projects_changed")] == ["merged"]
    assert _cache_busted(src["id"]) and _cache_busted(tgt["id"])


@pytest.mark.asyncio
async def test_a_refused_merge_publishes_nothing(db):
    p = await db_module.create_project(db, "merge-self")
    with _Subscription(p["id"]) as sub, _AccountSubscription(db) as acct:
        assert "error" in await db_module.merge_project(db, p["id"], p["id"])
        assert "error" in await db_module.merge_project(db, p["id"], "no-such-project")
        assert sub.events() == [] and acct.events() == []


@pytest.mark.asyncio
async def test_set_decision_announces_goal_updated_so_the_decisions_table_refreshes(db):
    """The Goal tab's Decisions table renders projects.decisions through refreshGoal; an entry
    added by an MCP session (set_decision) was announced to nobody."""
    p = await db_module.create_project(db, "decision-log")
    with _Subscription(p["id"]) as sub:
        await db_module.set_decision(db, p["id"], "use the append-only log")
        events = sub.events()
        with pytest.raises(ValueError):
            await db_module.set_decision(db, "no-such-project", "x")
        assert sub.events() == []
    assert events == [{"type": "goal_updated", "project_id": p["id"], "field": "decisions"}]


# ---------------------------------------------------------------------------
# 2. Sprint-item writers only MCP tools / sweeps reach
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_subtask_publishes_sprint_item_added(db):
    p = await db_module.create_project(db, "sub-add")
    parent = await db_module.add_sprint_item(db, p["id"], "v1", "parent item")
    _prime_cache(p["id"])
    with _Subscription(p["id"]) as sub:
        child = await db_module.add_subtask(db, p["id"], parent["id"], "child task")
        events = sub.events()
    assert events == [
        {"type": "sprint_item_added", "project_id": p["id"], "item_id": child["id"], "parent_id": parent["id"]}
    ]
    assert _cache_busted(p["id"])


@pytest.mark.asyncio
async def test_add_subtask_that_is_refused_publishes_nothing(db):
    p = await db_module.create_project(db, "sub-refused")
    with _Subscription(p["id"]) as sub:
        with pytest.raises(ValueError):
            await db_module.add_subtask(db, p["id"], "no-such-parent", "child")
        assert sub.events() == []


@pytest.mark.asyncio
async def test_move_sprint_item_to_project_announces_a_delete_and_an_add(db):
    src = await db_module.create_project(db, "move-src")
    dst = await db_module.create_project(db, "move-dst")
    item = await db_module.add_sprint_item(db, src["id"], "v1", "reclassify me")
    _prime_cache(src["id"])
    _prime_cache(dst["id"])
    with _Subscription(src["id"]) as sub_src, _Subscription(dst["id"]) as sub_dst:
        result = await db_module.move_sprint_item_to_project(
            db, item["id"], src["id"], dst["id"], actor="tester", reason="wrong board",
        )
        assert result["moved"] is True
        src_events, dst_events = sub_src.events(), sub_dst.events()
    assert src_events == [
        {"type": "sprint_item_deleted", "project_id": src["id"], "item_id": item["id"], "moved_to": dst["id"]}
    ]
    assert dst_events == [
        {"type": "sprint_item_added", "project_id": dst["id"], "item_id": item["id"], "moved_from": src["id"]}
    ]
    assert _cache_busted(src["id"]) and _cache_busted(dst["id"])


@pytest.mark.asyncio
async def test_move_that_does_nothing_publishes_nothing(db):
    src = await db_module.create_project(db, "move-noop-src")
    dst = await db_module.create_project(db, "move-noop-dst")
    item = await db_module.add_sprint_item(db, src["id"], "v1", "stay put")
    with _Subscription(src["id"]) as sub_src, _Subscription(dst["id"]) as sub_dst:
        await db_module.move_sprint_item_to_project(db, item["id"], src["id"], dst["id"], actor="", reason="r")
        await db_module.move_sprint_item_to_project(db, item["id"], dst["id"], src["id"], actor="a", reason="stale source")
        await db_module.move_sprint_item_to_project(db, item["id"], src["id"], src["id"], actor="a", reason="same")
        assert sub_src.events() == [] and sub_dst.events() == []


@pytest.mark.asyncio
async def test_clear_stale_claim_metadata_publishes_sprint_item_updated(db):
    p = await db_module.create_project(db, "stale-meta")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "has a ghost claim")
    await db.execute(
        "UPDATE sprint_items SET actor = 'ghost', claimed_at = datetime('now') WHERE id = ?", (item["id"],),
    )
    await db.commit()
    _prime_cache(p["id"])
    with _Subscription(p["id"]) as sub:
        await db_module.clear_stale_claim_metadata(db, p["id"], item["id"], actor="op")
        events = sub.events()
        # Already clean: an auditless no-op, so also silent.
        await db_module.clear_stale_claim_metadata(db, p["id"], item["id"], actor="op")
        assert sub.events() == []
    assert events == [
        {"type": "sprint_item_updated", "project_id": p["id"], "item_id": item["id"], "fields": ["claimed_at", "actor"]}
    ]
    assert _cache_busted(p["id"])


@pytest.mark.asyncio
async def test_link_sprint_item_github_issue_publishes_sprint_item_updated(db):
    p = await db_module.create_project(db, "gh-link")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "linked to an issue")
    with _Subscription(p["id"]) as sub:
        updated = await db_module.link_sprint_item_github_issue(
            db, p["id"], item["id"], 42, "https://github.com/o/r/issues/42", "manual",
        )
        events = sub.events()
        assert await db_module.link_sprint_item_github_issue(db, p["id"], "no-such-item", 1, None, "manual") is None
        assert sub.events() == []
    assert updated is not None and updated["github_issue_number"] == 42
    assert [e["item_id"] for e in _of_type(events, "sprint_item_updated")] == [item["id"]]


@pytest.mark.asyncio
async def test_batch_rollback_announces_the_compensating_delete(db):
    """create -> (second entry trips the duplicate guard) -> compensate: a dashboard that
    refetched on the create's sprint_item_added must be told the row is gone again."""
    p = await db_module.create_project(db, "batch-rollback-events")
    await db_module.add_sprint_item(db, p["id"], "v1", "Refactor the parser")
    with _Subscription(p["id"]) as sub:
        result = await bm.execute_batch(
            db, project_id=p["id"], entry_kind="sprint_item",
            entries=[
                {"action": "create", "title": "Totally unrelated item", "version": "v1"},
                {"action": "create", "title": "Refactor the parser", "version": "v1"},
            ],
            mode="all_or_nothing",
        )
        events = sub.events()
    assert result.status == "failed" and result.results[0].status == "rolled_back"
    added = _of_type(events, "sprint_item_added")
    deleted = _of_type(events, "sprint_item_deleted")
    assert len(added) == 1 and len(deleted) == 1
    assert deleted[0]["item_id"] == added[0]["item_id"]
    assert [e["type"] for e in events].index("sprint_item_added") < [e["type"] for e in events].index("sprint_item_deleted")


# ---------------------------------------------------------------------------
# 3. Stale-session / stale-claim sweeps and release_task
# ---------------------------------------------------------------------------


async def _claimed_task_with_item(db, project_id: str, session_id: str, title: str = "swept work"):
    item = await db_module.add_sprint_item(db, project_id, "v1", title)
    task = await db_module.log_task(db, session_id, project_id, f"working on {title}", "pending", sprint_item_id=item["id"])
    await db_module.claim_task(db, task["id"], session_id)
    await db_module.claim_sprint_item(db, project_id, item["id"])
    return item, task


@pytest.mark.asyncio
async def test_archive_stale_sessions_announces_released_claims_and_the_archive(db):
    p = await db_module.create_project(db, "sweep-archive")
    s = await db_module.register_session(db, p["id"], "stale-worker")
    item, task = await _claimed_task_with_item(db, p["id"], s["id"])
    await db.execute("UPDATE sessions SET last_seen = datetime('now', '-8 days') WHERE id = ?", (s["id"],))
    await db.commit()
    _prime_cache(p["id"])
    with _Subscription(p["id"]) as sub:
        assert await db_module.archive_stale_sessions(db, p["id"]) == 1
        events = sub.events()
    items = _of_type(events, "sprint_item_updated")
    assert len(items) == 1
    assert items[0]["item_ids"] == [item["id"]] and items[0]["status"] == "pending"
    assert [e["task"]["id"] for e in _of_type(events, "task_updated")] == [task["id"]]
    assert _of_type(events, "task_updated")[0]["task"]["status"] == "pending"
    assert _of_type(events, "session_updated") == [
        {"type": "session_updated", "project_id": p["id"], "status": "archived", "count": 1}
    ]
    assert _cache_busted(p["id"])


@pytest.mark.asyncio
async def test_archive_stale_sessions_with_nothing_stale_is_silent(db):
    p = await db_module.create_project(db, "sweep-archive-quiet")
    await db_module.register_session(db, p["id"], "fresh")
    with _Subscription(p["id"]) as sub:
        _drain(sub.q)
        assert await db_module.archive_stale_sessions(db, p["id"]) == 0
        assert sub.events() == []


@pytest.mark.asyncio
async def test_release_stale_task_claims_announces_the_item_and_the_task(db):
    p = await db_module.create_project(db, "sweep-release")
    s = await db_module.register_session(db, p["id"], "slow-worker")
    item, task = await _claimed_task_with_item(db, p["id"], s["id"])
    await db.execute("UPDATE task_log SET claimed_at = datetime('now', '-3 hours') WHERE id = ?", (task["id"],))
    await db.commit()
    _prime_cache(p["id"])
    with _Subscription(p["id"]) as sub:
        assert await db_module.release_stale_task_claims(db, p["id"]) == 1
        events = sub.events()
    assert [e["item_ids"] for e in _of_type(events, "sprint_item_updated")] == [[item["id"]]]
    assert [e["task"]["id"] for e in _of_type(events, "task_updated")] == [task["id"]]
    assert _cache_busted(p["id"])
    with _Subscription(p["id"]) as sub:
        assert await db_module.release_stale_task_claims(db, p["id"]) == 0
        assert sub.events() == []


@pytest.mark.asyncio
async def test_release_task_announces_the_linked_sprint_item_too(db):
    p = await db_module.create_project(db, "sweep-release-task")
    s = await db_module.register_session(db, p["id"], "releaser")
    item, task = await _claimed_task_with_item(db, p["id"], s["id"])
    _prime_cache(p["id"])
    with _Subscription(p["id"]) as sub:
        assert await db_module.release_task(db, task["id"], s["id"]) is True
        events = sub.events()
        assert await db_module.release_task(db, task["id"], "someone-else") is False
        assert sub.events() == []
    assert _of_type(events, "sprint_item_updated") == [
        {"type": "sprint_item_updated", "project_id": p["id"], "item_id": item["id"], "status": "pending"}
    ]
    assert len(_of_type(events, "task_updated")) == 1
    assert _cache_busted(p["id"])


@pytest.mark.asyncio
async def test_expire_idle_sessions_announces_session_updated_per_project(db):
    p = await db_module.create_project(db, "sweep-idle")
    quiet = await db_module.create_project(db, "sweep-idle-quiet")
    s = await db_module.register_session(db, p["id"], "going-idle")
    await db_module.register_session(db, quiet["id"], "still-active")
    await db.execute("UPDATE sessions SET last_seen = datetime('now', '-60 minutes') WHERE id = ?", (s["id"],))
    await db.commit()
    with _Subscription(p["id"]) as sub, _Subscription(quiet["id"]) as sub_quiet:
        _drain(sub.q)
        _drain(sub_quiet.q)
        await db_module.expire_idle_sessions(db, max_age_minutes=30)
        assert _of_type(sub.events(), "session_updated") == [
            {"type": "session_updated", "project_id": p["id"], "status": "idle"}
        ]
        assert sub_quiet.events() == []


@pytest.mark.asyncio
async def test_expire_inactive_sessions_announces_the_released_task_and_the_archive(db):
    p = await db_module.create_project(db, "sweep-inactive")
    s = await db_module.register_session(db, p["id"], "dead")
    task = await db_module.log_task(db, s["id"], p["id"], "claimed work", "pending")
    await db_module.claim_task(db, task["id"], s["id"])
    await db.execute("UPDATE sessions SET last_seen = datetime('now', '-25 hours') WHERE id = ?", (s["id"],))
    await db.commit()
    with _Subscription(p["id"]) as sub:
        _drain(sub.q)
        await db_module.expire_inactive_sessions(db, max_age_hours=24)
        events = sub.events()
    assert [e["task"]["id"] for e in _of_type(events, "task_updated")] == [task["id"]]
    assert _of_type(events, "session_updated") == [
        {"type": "session_updated", "project_id": p["id"], "status": "archived"}
    ]


@pytest.mark.asyncio
async def test_resource_amendment_on_a_claim_announces_the_item(db):
    p = await db_module.create_project(db, "amend-events")
    s = await db_module.register_session(db, p["id"], "claimer")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "amended item")
    await db.execute(
        "UPDATE sprint_items SET status = 'in_progress', actor = ?, touches_resources = ? WHERE id = ?",
        (s["id"], json.dumps(["file:meridian/server.py"]), item["id"]),
    )
    await db.commit()
    _prime_cache(p["id"])
    with _Subscription(p["id"]) as sub:
        # already declared: nothing amended, nothing published
        assert await db_module._amend_sprint_item_resources_for_session(db, s["id"], "file:meridian/server.py") is None
        assert sub.events() == []
        result = await db_module._amend_sprint_item_resources_for_session(db, s["id"], "file:meridian/new_module.py")
        events = sub.events()
    assert result is not None
    assert events == [
        {
            "type": "sprint_item_updated", "project_id": p["id"], "item_id": item["id"],
            "fields": ["touches_resources", "resources_amended"],
        }
    ]
    assert _cache_busted(p["id"])


@pytest.mark.asyncio
async def test_handoff_inferred_resources_are_announced_once_per_pass(db, monkeypatch):
    from meridian import handoff

    p = await db_module.create_project(db, "inferred-events")
    a = await db_module.add_sprint_item(db, p["id"], "v1", "Improve widget exporter performance")
    b = await db_module.add_sprint_item(db, p["id"], "v1", "Document the zebra crossing helper")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a_, **k_: SimpleNamespace(stdout="meridian/widget/exporter.py\n", returncode=0),
    )
    pending = [dict(a), dict(b)]
    _prime_cache(p["id"])
    with _Subscription(p["id"]) as sub:
        out = await handoff._annotate_touches_files(db, p["id"], pending)
        events = sub.events()
        assert await handoff._annotate_touches_files(db, p["id"], out) is out  # nothing left to infer
        assert sub.events() == []
    assert [i["id"] for i in out if i.get("touches_resources")] == [a["id"]]
    assert events == [
        {
            "type": "sprint_item_updated", "project_id": p["id"],
            "item_ids": [a["id"]], "fields": ["touches_resources"],
        }
    ]
    assert _cache_busted(p["id"])
