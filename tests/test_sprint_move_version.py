"""0c30b989 -- moving a sprint item to another version.

The dashboard arrow button used to ask for a version with window.prompt and
DEFER the item (status 'pushed', own version unchanged, hidden in the
Backburner). The new operation MOVES it: only ``version`` changes, the item
stays pending under the target version's group, its title is never touched,
and the server states the version it used. The legacy deferral
(push_sprint_item / POST .../push) is unchanged and pinned here too.
"""
from __future__ import annotations

import json

import pytest

from meridian import db as db_module
from meridian.db import sprint_items as si_mod


async def _project(db, name="move-version"):
    return await db_module.create_project(db, name)


async def _item(db, pid, title="Add rate limiting", version="v2.1", **kw):
    return await db_module.add_sprint_item(db, pid, version, title, **kw)


async def _moves(db, pid):
    rows = await db_module.get_action_audit_log(
        db, project_id=pid,
        event_type=db_module.SPRINT_ITEM_VERSION_MOVED_AUDIT_EVENT,
    )
    return [{**r, "detail": json.loads(r["detail"])} for r in rows]


# ---------------------------------------------------------------------------
# next / specific
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start,expected",
    [
        ("v2.1", "v2.2"),   # the owner's own examples
        ("v2.2", "v2.3"),
        ("v2.9", "v2.10"),
        ("2.1", "2.2"),
        ("v2", "v3"),
        ("v0.2.x", "v0.3.x"),
        ("1.0.0", "1.0.1"),
    ],
)
async def test_move_next_computes_and_reports_the_version(db, start, expected):
    p = await _project(db)
    item = await _item(db, p["id"], version=start)
    out = await db_module.move_sprint_item_to_version(
        db, p["id"], item["id"], use_next=True
    )
    assert out["to_version"] == expected
    assert out["from_version"] == start
    assert out["via"] == "next"
    assert out["unchanged"] is False
    # Verified by re-reading, not just echoing the request.
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["version"] == expected
    assert out["item"]["version"] == expected


async def test_move_to_a_specific_version(db):
    p = await _project(db)
    item = await _item(db, p["id"], version="v2.1")
    out = await db_module.move_sprint_item_to_version(
        db, p["id"], item["id"], to_version="  v3.0  "
    )
    assert out["to_version"] == "v3.0"  # trimmed
    assert out["via"] == "specific"
    assert (await db_module.get_sprint_item(db, item["id"]))["version"] == "v3.0"


async def test_move_next_refuses_to_guess_and_changes_nothing(db):
    p = await _project(db)
    item = await _item(db, p["id"], version="current sprint v0.2")
    with pytest.raises(db_module.NextVersionUnavailable) as ei:
        await db_module.move_sprint_item_to_version(
            db, p["id"], item["id"], use_next=True
        )
    assert ei.value.current_version == "current sprint v0.2"
    assert (await db_module.get_sprint_item(db, item["id"]))["version"] == "current sprint v0.2"
    assert await _moves(db, p["id"]) == []


@pytest.mark.parametrize("kwargs", [{}, {"use_next": True, "to_version": "v9"}])
async def test_move_needs_exactly_one_of_next_or_version(db, kwargs):
    p = await _project(db)
    item = await _item(db, p["id"])
    with pytest.raises(ValueError, match="exactly one"):
        await db_module.move_sprint_item_to_version(db, p["id"], item["id"], **kwargs)


@pytest.mark.parametrize(
    "bad", ["", "   ", "v2\u0000", "v2\n.1", "v2​", "x" * 65]
)
async def test_move_rejects_unusable_version_labels(db, bad):
    p = await _project(db)
    item = await _item(db, p["id"])
    with pytest.raises(ValueError):
        await db_module.move_sprint_item_to_version(
            db, p["id"], item["id"], to_version=bad
        )
    assert (await db_module.get_sprint_item(db, item["id"]))["version"] == "v2.1"


# ---------------------------------------------------------------------------
# what a move must NOT touch
# ---------------------------------------------------------------------------


async def test_move_never_touches_the_title_or_the_defer_fields(db):
    p = await _project(db)
    title = "Ship the édition -- v2.1 polish (final)"
    item = await _item(db, p["id"], title=title, version="v2.1", group="Perf")
    await db_module.move_sprint_item_to_version(db, p["id"], item["id"], use_next=True)
    after = await db_module.get_sprint_item(db, item["id"])
    assert after["title"] == title
    assert after["status"] == "pending"
    assert after["pushed_to"] is None
    assert after["completed_at"] is None
    assert after["item_group"] == "Perf"


async def test_move_keeps_status_and_claim_of_an_in_progress_item(db):
    p = await _project(db)
    item = await _item(db, p["id"])
    claimed = await db_module.claim_sprint_item(db, p["id"], item["id"], actor="sess-1")
    out = await db_module.move_sprint_item_to_version(
        db, p["id"], item["id"], use_next=True
    )
    assert out["item"]["status"] == "in_progress"
    assert out["item"]["claimed_at"] == claimed["claimed_at"]
    assert out["item"]["actor"] == "sess-1"


@pytest.mark.parametrize("terminal", ["done", "skipped", "failed", "pushed"])
async def test_move_refuses_finished_or_deferred_items(db, terminal):
    p = await _project(db)
    item = await _item(db, p["id"], title=f"item {terminal}")
    if terminal == "done":
        await db_module.complete_sprint_item(db, p["id"], item["id"])
    elif terminal == "skipped":
        await db_module.skip_sprint_item(db, p["id"], item["id"])
    elif terminal == "failed":
        await db_module.fail_sprint_item(db, p["id"], item["id"], reason="x")
    else:
        await db_module.push_sprint_item(db, p["id"], item["id"], "v9")
    with pytest.raises(db_module.SprintItemStatusRace):
        await db_module.move_sprint_item_to_version(
            db, p["id"], item["id"], use_next=True
        )
    assert (await db_module.get_sprint_item(db, item["id"]))["version"] == "v2.1"


async def test_move_is_scoped_to_the_project(db):
    a = await _project(db, "a")
    b = await _project(db, "b")
    item = await _item(db, a["id"])
    assert await db_module.move_sprint_item_to_version(
        db, b["id"], item["id"], use_next=True
    ) is None
    assert await db_module.move_sprint_item_to_version(
        db, a["id"], "does-not-exist", use_next=True
    ) is None
    assert (await db_module.get_sprint_item(db, item["id"]))["version"] == "v2.1"


# ---------------------------------------------------------------------------
# idempotency / races
# ---------------------------------------------------------------------------


async def test_replayed_next_with_expected_version_moves_only_once(db):
    p = await _project(db)
    item = await _item(db, p["id"], version="v2.1")
    first = await db_module.move_sprint_item_to_version(
        db, p["id"], item["id"], use_next=True, expected_version="v2.1"
    )
    assert first["to_version"] == "v2.2"
    # The same request arriving again (a client retry after a timeout).
    with pytest.raises(db_module.SprintItemVersionConflict) as ei:
        await db_module.move_sprint_item_to_version(
            db, p["id"], item["id"], use_next=True, expected_version="v2.1"
        )
    assert ei.value.current_version == "v2.2"
    assert (await db_module.get_sprint_item(db, item["id"]))["version"] == "v2.2"
    assert len(await _moves(db, p["id"])) == 1


async def test_move_to_the_version_it_already_has_is_a_quiet_noop(db, monkeypatch):
    p = await _project(db)
    item = await _item(db, p["id"], version="v2.1")
    events = []
    monkeypatch.setattr(si_mod, "_publish_project_event", lambda *a, **k: events.append(a))
    out = await db_module.move_sprint_item_to_version(
        db, p["id"], item["id"], to_version="v2.1"
    )
    assert out["unchanged"] is True
    assert out["to_version"] == "v2.1"
    assert events == []
    assert await _moves(db, p["id"]) == []


async def test_concurrent_version_change_is_detected_by_the_write_guard(db, monkeypatch):
    """The read-then-write window: another caller moves the item after our read."""
    p = await _project(db)
    item = await _item(db, p["id"], version="v2.1")
    real_get = si_mod.get_sprint_item
    calls = {"n": 0}

    async def racing_get(conn, item_id):
        row = await real_get(conn, item_id)
        calls["n"] += 1
        if calls["n"] == 1:  # right after the first read, someone else moves it
            await conn.execute(
                "UPDATE sprint_items SET version = 'v7' WHERE id = ?", (item_id,)
            )
            await conn.commit()
        return row

    monkeypatch.setattr(si_mod, "get_sprint_item", racing_get)
    with pytest.raises(db_module.SprintItemVersionConflict):
        await db_module.move_sprint_item_to_version(
            db, p["id"], item["id"], to_version="v3.0"
        )
    monkeypatch.undo()
    assert (await db_module.get_sprint_item(db, item["id"]))["version"] == "v7"


# ---------------------------------------------------------------------------
# subtasks travel with the parent
# ---------------------------------------------------------------------------


async def test_subtasks_still_in_the_old_version_move_with_the_parent(db):
    p = await _project(db)
    parent = await _item(db, p["id"], title="Parent epic", version="v1.0")
    child = await db_module.add_subtask(db, p["id"], parent["id"], "child alpha")
    grandchild = await db_module.add_subtask(db, p["id"], child["id"], "grandchild beta")
    elsewhere = await db_module.add_subtask(db, p["id"], parent["id"], "child elsewhere")
    await db_module.patch_sprint_item(db, p["id"], elsewhere["id"], version="v1.5")
    finished = await db_module.add_subtask(db, p["id"], parent["id"], "child finished")
    await db_module.complete_sprint_item(db, p["id"], finished["id"])

    out = await db_module.move_sprint_item_to_version(
        db, p["id"], parent["id"], use_next=True
    )
    assert out["to_version"] == "v1.1"
    assert sorted(out["moved_children"]) == sorted([child["id"], grandchild["id"]])

    version_of = {
        i["id"]: i["version"] for i in await db_module.get_sprint_items(db, p["id"])
    }
    assert version_of[parent["id"]] == "v1.1"
    assert version_of[child["id"]] == "v1.1"
    assert version_of[grandchild["id"]] == "v1.1"
    assert version_of[elsewhere["id"]] == "v1.5"   # placed elsewhere on purpose
    assert version_of[finished["id"]] == "v1.0"    # finished work stays put
    # Titles of every item are untouched.
    titles = {i["id"]: i["title"] for i in await db_module.get_sprint_items(db, p["id"])}
    assert titles[parent["id"]] == "Parent epic"
    assert titles[child["id"]] == "child alpha"


# ---------------------------------------------------------------------------
# history, live event, cache
# ---------------------------------------------------------------------------


async def test_move_writes_a_history_entry(db):
    p = await _project(db)
    item = await _item(db, p["id"], version="v2.1")
    child = await db_module.add_subtask(db, p["id"], item["id"], "kid")
    out = await db_module.move_sprint_item_to_version(
        db, p["id"], item["id"], use_next=True, actor="rest-api", tenant_id=None
    )
    assert out["history_recorded"] is True
    (entry,) = await _moves(db, p["id"])
    assert entry["actor"] == "rest-api"
    assert entry["detail"] == {
        "item_id": item["id"],
        "from_version": "v2.1",
        "to_version": "v2.2",
        "via": "next",
        "moved_children": [child["id"]],
    }


async def test_history_failure_does_not_undo_the_move(db, monkeypatch):
    p = await _project(db)
    item = await _item(db, p["id"], version="v2.1")

    async def broken(*a, **k):
        raise RuntimeError("audit table unavailable")

    monkeypatch.setattr(db_module, "record_action_audit_event", broken)
    out = await db_module.move_sprint_item_to_version(
        db, p["id"], item["id"], use_next=True
    )
    assert out["history_recorded"] is False
    assert (await db_module.get_sprint_item(db, item["id"]))["version"] == "v2.2"


async def test_move_publishes_a_live_event_and_busts_the_cache(db, monkeypatch):
    p = await _project(db)
    item = await _item(db, p["id"], version="v2.1")
    # Warm the per-project cache with the old version.
    warm = await db_module.get_sprint_items_cached(db, p["id"])
    assert warm[0]["version"] == "v2.1"
    events = []
    monkeypatch.setattr(
        si_mod, "_publish_project_event", lambda pid, kind, data: events.append((pid, kind, data))
    )
    await db_module.move_sprint_item_to_version(db, p["id"], item["id"], use_next=True)
    assert events == [
        (p["id"], "sprint_item_updated",
         {"item_id": item["id"], "status": "pending", "version": "v2.2"})
    ]
    fresh = await db_module.get_sprint_items_cached(db, p["id"])
    assert fresh[0]["version"] == "v2.2"


# ---------------------------------------------------------------------------
# bulk
# ---------------------------------------------------------------------------


async def test_bulk_move_reports_one_outcome_per_item(db):
    p = await _project(db)
    a = await _item(db, p["id"], title="alpha feature", version="v2.1")
    b = await _item(db, p["id"], title="bravo feature", version="v2.4")
    done = await _item(db, p["id"], title="charlie feature", version="v2.1")
    await db_module.complete_sprint_item(db, p["id"], done["id"])
    odd = await _item(db, p["id"], title="delta feature", version="current sprint v0.2")

    results = await db_module.move_sprint_items_to_version(
        db, p["id"],
        [a["id"], b["id"], done["id"], odd["id"], "ghost", a["id"]],  # a repeated
        use_next=True,
    )
    by_id = {r["item_id"]: r for r in results}
    assert [r["item_id"] for r in results] == [a["id"], b["id"], done["id"], odd["id"], "ghost"]
    assert by_id[a["id"]]["ok"] and by_id[a["id"]]["to_version"] == "v2.2"
    assert by_id[b["id"]]["ok"] and by_id[b["id"]]["to_version"] == "v2.5"  # its OWN next
    assert by_id[done["id"]]["error"] == "status_conflict"
    assert by_id[odd["id"]]["error"] == "next_version_unavailable"
    assert by_id["ghost"]["error"] == "not_found"
    assert (await db_module.get_sprint_item(db, a["id"]))["version"] == "v2.2"


async def test_bulk_move_has_a_size_cap(db):
    p = await _project(db)
    with pytest.raises(ValueError, match="at most"):
        await db_module.move_sprint_items_to_version(
            db, p["id"], [f"id-{i}" for i in range(db_module.MAX_BULK_MOVE_ITEMS + 1)],
            use_next=True,
        )


# ---------------------------------------------------------------------------
# the legacy deferral is unchanged
# ---------------------------------------------------------------------------


async def test_legacy_push_is_unchanged(db):
    """push_sprint_item still DEFERS: terminal 'pushed', pushed_to recorded, the
    item's own version and title untouched, and no version-move history."""
    p = await _project(db)
    item = await _item(db, p["id"], title="deferred feature", version="v1.9")
    pushed = await db_module.push_sprint_item(db, p["id"], item["id"], "v2.0")
    assert pushed["status"] == "pushed"
    assert pushed["pushed_to"] == "v2.0"
    assert pushed["version"] == "v1.9"
    assert pushed["title"] == "deferred feature"
    assert pushed["completed_at"] is not None
    # Exactly the three defer columns differ from the row before the push.
    assert {k for k in item if item[k] != pushed[k]} == {
        "status", "pushed_to", "completed_at"
    }
    assert await _moves(db, p["id"]) == []
    # A pushed item is not an active item: it cannot be pushed again.
    with pytest.raises(db_module.SprintItemStatusRace):
        await db_module.push_sprint_item(db, p["id"], item["id"], "v2.1")
    with pytest.raises(ValueError, match="to_version is required"):
        await db_module.push_sprint_item(db, p["id"], item["id"], "")


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


def _rest_project(client, name="move-rest"):
    r = client.post("/projects", json={"name": name})
    assert r.status_code == 201
    return r.json()["id"]


def _rest_item(client, pid, title="Add rate limiting", version="v2.1"):
    r = client.post(f"/projects/{pid}/sprint-items", json={"title": title, "version": version})
    assert r.status_code == 201
    return r.json()


def test_rest_move_next_lands_the_item_under_the_new_version(client):
    pid = _rest_project(client)
    item = _rest_item(client, pid, "Add rate limiting", "v2.1")
    r = client.post(
        f"/projects/{pid}/sprint-items/{item['id']}/move",
        json={"next": True, "expected_version": "v2.1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["to_version"] == "v2.2"
    assert body["from_version"] == "v2.1"
    assert body["via"] == "next"
    assert body["unchanged"] is False
    assert body["item"]["version"] == "v2.2"
    assert body["item"]["title"] == "Add rate limiting"
    assert body["item"]["status"] == "pending"
    # What the Live board / Queue read back: the item is in the v2.2 group.
    listed = {i["id"]: i for i in client.get(f"/projects/{pid}/sprint-items").json()}
    assert listed[item["id"]]["version"] == "v2.2"
    assert listed[item["id"]]["status"] == "pending"
    assert listed[item["id"]]["title"] == "Add rate limiting"


def test_rest_move_to_specific_version(client):
    pid = _rest_project(client)
    item = _rest_item(client, pid)
    r = client.post(
        f"/projects/{pid}/sprint-items/{item['id']}/move", json={"to_version": "v3.0"}
    )
    assert r.status_code == 200
    assert r.json()["to_version"] == "v3.0"
    assert r.json()["via"] == "specific"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"next": False},
        {"next": "true"},
        {"next": True, "to_version": "v3"},
        {"to_version": 3},
        {"to_version": ""},
        {"to_version": "v3\u0000"},
        {"to_version": "x" * 65},
        {"next": True, "expected_version": 2},
    ],
)
def test_rest_move_rejects_malformed_requests(client, body):
    pid = _rest_project(client)
    item = _rest_item(client, pid)
    r = client.post(f"/projects/{pid}/sprint-items/{item['id']}/move", json=body)
    assert r.status_code == 422, r.text
    assert client.get(f"/projects/{pid}/sprint-items/{item['id']}").json()["version"] == "v2.1"


def test_rest_move_next_on_an_unparseable_version_is_a_coded_422(client):
    pid = _rest_project(client)
    item = _rest_item(client, pid, "odd", "current sprint v0.2")
    r = client.post(f"/projects/{pid}/sprint-items/{item['id']}/move", json={"next": True})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "next_version_unavailable"
    assert detail["current_version"] == "current sprint v0.2"
    # ...and the explicit path still works for that same item.
    r2 = client.post(
        f"/projects/{pid}/sprint-items/{item['id']}/move", json={"to_version": "v2.3"}
    )
    assert r2.status_code == 200


def test_rest_move_conflicts_and_missing_items(client):
    pid = _rest_project(client)
    other = _rest_project(client, "other-project")
    item = _rest_item(client, pid)
    # stale expected_version
    r = client.post(
        f"/projects/{pid}/sprint-items/{item['id']}/move",
        json={"next": True, "expected_version": "v0.9"},
    )
    assert r.status_code == 409
    # finished item
    client.post(f"/projects/{pid}/sprint-items/{item['id']}/complete")
    r = client.post(f"/projects/{pid}/sprint-items/{item['id']}/move", json={"next": True})
    assert r.status_code == 409
    # unknown item / wrong project
    assert client.post(
        f"/projects/{pid}/sprint-items/nope/move", json={"next": True}
    ).status_code == 404
    live = _rest_item(client, pid, "second item")
    assert client.post(
        f"/projects/{other}/sprint-items/{live['id']}/move", json={"next": True}
    ).status_code == 404


def test_rest_bulk_move(client):
    pid = _rest_project(client)
    a = _rest_item(client, pid, "alpha feature", "v2.1")
    b = _rest_item(client, pid, "bravo feature", "v2.4")
    r = client.post(
        f"/projects/{pid}/sprint-items/move",
        json={"item_ids": [a["id"], b["id"], "ghost"], "next": True},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["moved"], out["failed"], out["unchanged"]) == (2, 1, 0)
    by_id = {x["item_id"]: x for x in out["results"]}
    assert by_id[a["id"]]["to_version"] == "v2.2"
    assert by_id[b["id"]]["to_version"] == "v2.5"
    assert by_id["ghost"]["error"] == "not_found"


@pytest.mark.parametrize(
    "body",
    [
        {"next": True},
        {"item_ids": [], "next": True},
        {"item_ids": "abc", "next": True},
        {"item_ids": ["a", 3], "next": True},
        {"item_ids": ["a"]},
        {"item_ids": ["a"], "next": True, "to_version": "v3"},
        {"item_ids": ["a"], "to_version": "v3\u0000"},
        {"item_ids": [f"i{n}" for n in range(101)], "next": True},
    ],
)
def test_rest_bulk_move_validation(client, body):
    pid = _rest_project(client)
    r = client.post(f"/projects/{pid}/sprint-items/move", json=body)
    assert r.status_code == 422, r.text


def test_rest_push_is_byte_for_byte_the_legacy_deferral(client):
    """The old arrow button's endpoint must answer exactly as it always did."""
    pid = _rest_project(client)
    item = _rest_item(client, pid, "deferred feature", "v1.9")
    r = client.post(
        f"/projects/{pid}/sprint-items/{item['id']}/push", json={"to_version": "v2.0"}
    )
    assert r.status_code == 200
    pushed = r.json()
    # Characterisation of the legacy defer: against the row as it was before the
    # push, EXACTLY these three columns change and nothing else -- in particular
    # not version, title, slug or notes. (Recorded from the pre-0c30b989 code.)
    assert set(pushed) == set(item)
    changed = {k for k in item if item[k] != pushed[k]}
    assert changed == {"status", "pushed_to", "completed_at"}
    assert pushed["status"] == "pushed"
    assert pushed["pushed_to"] == "v2.0"
    assert pushed["completed_at"] is not None
    assert pushed["version"] == "v1.9"
    # Validation behaviour is unchanged as well.
    assert client.post(
        f"/projects/{pid}/sprint-items/{item['id']}/push", json={}
    ).status_code == 422
    assert client.post(
        f"/projects/{pid}/sprint-items/{item['id']}/push", json={"to_version": "v3.0"}
    ).status_code == 409  # already pushed
    assert client.post(
        f"/projects/{pid}/sprint-items/nope/push", json={"to_version": "v2.0"}
    ).status_code == 404


def test_patch_version_still_works_and_move_agrees_with_it(client):
    """update_sprint_item(version=...) is the agent-side equivalent of a move to a
    specific version: same resulting row."""
    pid = _rest_project(client)
    a = _rest_item(client, pid, "alpha feature", "v2.1")
    b = _rest_item(client, pid, "bravo feature", "v2.1")
    client.patch(f"/projects/{pid}/sprint-items/{a['id']}", json={"version": "v2.2"})
    client.post(f"/projects/{pid}/sprint-items/{b['id']}/move", json={"to_version": "v2.2"})
    rows = {i["id"]: i for i in client.get(f"/projects/{pid}/sprint-items").json()}
    for key in ("version", "status", "pushed_to", "completed_at"):
        assert rows[a["id"]][key] == rows[b["id"]][key], key


# ---------------------------------------------------------------------------
# MCP parity: what agents already have
# ---------------------------------------------------------------------------


async def test_mcp_update_sprint_item_version_is_the_agent_side_move(db):
    """An agent moves an item with update_sprint_item(version=...): same resulting
    row as the dashboard's move (version changes; status, title and defer fields
    do not)."""
    from meridian.mcp.handlers.sprint_tools import handle_update_sprint_item

    p = await _project(db)
    item = await _item(db, p["id"], title="agent-moved item", version="v2.1")
    out = await handle_update_sprint_item(
        {"project_id": p["id"], "item_id": item["id"], "version": "v2.2"},
        db, "", None, None,
    )
    assert out["version"] == "v2.2"
    assert out["status"] == "pending"
    assert out["title"] == "agent-moved item"
    assert out["pushed_to"] is None and out["completed_at"] is None


def test_stdio_push_tool_description_points_agents_at_the_move_path():
    """push_sprint_item stays the deferral; its description now says how to move
    an item without deferring it."""
    from pathlib import Path

    src = (Path(__file__).parent.parent / "meridian" / "mcp" / "stdio_handler.py").read_text(
        encoding="utf-8"
    )
    start = src.index('name="push_sprint_item"')
    block = src[start : start + 1200]
    assert "DEFERS the item" in block
    assert "update_sprint_item(version=...)" in block
