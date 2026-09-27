"""Races in the whole-file lock hand-off 0efacc22 introduced, and malformed
``coarse_lock_files`` values (both from an independent adversarial review).

F1. When a same-session item that needs a file only through a
``symbol:<path>::<sym>`` declaration is still ``in_progress``, releasing the
item that locked the file keeps the lock and HANDS IT OFF — records the file in
that sibling's ``coarse_lock_files`` — so the sibling frees it when it leaves.
0efacc22 wrote that hand-off as a read-then-write with no compare-and-swap,
skipped it when the sibling had left meanwhile, and read the releasing item's
own ``coarse_lock_files`` before its (unguarded) status transition. So:

* two concurrent releases handing off to the same sibling lost one hand-off
  (both read ``NULL``, the second write won), and that file stayed locked by
  the session after every item was released;
* a sibling that left between the sibling check and the hand-off write left
  the lock to nobody (both items pending, lock still held, other sessions
  blocked) — likewise a sibling transferred to another session, which the
  hand-off then wrote into anyway;
* a hand-off landing between a sibling's read and its own leaving transition
  was ignored by that sibling's release (stale snapshot).

Invariant pinned here, under every interleaving tried: once every item that
needed file X has left the session's ``in_progress`` set, the session holds no
lock on X on their account; while one still needs X, the lock is not freed.
Interleavings are forced with monkeypatched hooks and asyncio.Events, never
sleeps.

F2. ``_coarse_lock_paths`` only guarded ``json.loads``: a value parsing to a
non-list made release/transfer raise after the item was already pending (lock
stranded, no audit row), and a JSON string was iterated character by character.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

import meridian.server  # noqa: F401 — import first to avoid the handler/server import cycle
from meridian import db as db_module
from meridian import server as srv
from meridian.db import sprint_items as sprint_items_module

X = "pkg/x.py"
Y = "pkg/y.py"
STALE = {"classification": "stale", "reasons": ["test"], "signals": {}}
ROUNDS = 10  # x2 orders = 20 concurrent-release runs per release path


async def _item(db, pid: str, title: str, resources: list[str]) -> str:
    item = await db_module.add_sprint_item(
        db, pid, "v1", title, touches_resources=resources, prospect_bypass=True,
    )
    assert "id" in item, item
    return item["id"]


async def _session(db, pid: str, name: str = "worker") -> str:
    return (await db_module.register_session(db, pid, name))["id"]


async def _mcp(db, name: str, args: dict):
    return await srv._dispatch_mcp_tool(name, args, db, "/tmp")


async def _claim(db, pid: str, item_id: str, session_id: str) -> dict:
    """MCP claim with no source: symbol: resources widen to a whole-file lock."""
    result = await _mcp(db, "claim_sprint_item", {
        "project_id": pid, "item_id": item_id, "session_id": session_id,
    })
    assert result.get("status") == "in_progress", result
    return result


async def _release_db(db, pid: str, item_id: str, session_id: str) -> dict:
    result = await db_module.release_sprint_item_claim(db, pid, item_id, session_id)
    assert result and not result.get("blocked"), result
    return result


async def _release_mcp(db, pid: str, item_id: str, session_id: str) -> dict:
    result = await _mcp(db, "release_sprint_item_claim", {
        "project_id": pid, "item_id": item_id, "session_id": session_id, "reason": "test",
    })
    assert not result.get("blocked") and "error" not in result, result
    return result


async def _holder(db, path: str):
    return ((await db_module.get_file_claims(db, path)).get("file_lock") or {}).get("session_id")


async def _coarse(db, item_id: str) -> list[str]:
    raw = (await db_module.get_sprint_item(db, item_id)).get("coarse_lock_files")
    return json.loads(raw) if raw else []


async def _status(db, item_id: str) -> str:
    return (await db_module.get_sprint_item(db, item_id))["status"]


async def _other_session_can_claim(db, pid: str, path: str) -> dict:
    other = await _session(db, pid, "other")
    probe = await _item(db, pid, "Harden the upload path", [f"file:{path}"])
    return await _mcp(db, "claim_sprint_item", {
        "project_id": pid, "item_id": probe, "session_id": other,
    })


# ---------------------------------------------------------------------------
# F1 — two concurrent releases handing off to the same sibling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("release", [_release_db, _release_mcp], ids=["db", "mcp"])
async def test_concurrent_releases_never_lose_a_hand_off(db, release):
    """A=file:X and C=file:Y are released concurrently (asyncio.gather, both
    orders, repeated) while B=symbol:X::s + symbol:Y::t — claimed last, so its
    coarse widenings onto A's and C's locks are not its own — is in_progress."""
    for round_ in range(ROUNDS):
        for order in ("a-first", "c-first"):
            pid = (await db_module.create_project(db, f"handoff-race-{order}-{round_}"))["id"]
            sess = await _session(db, pid)
            a = await _item(db, pid, "Refactor the config parser", [f"file:{X}"])
            c = await _item(db, pid, "Harden the upload path", [f"file:{Y}"])
            b = await _item(
                db, pid, "Add retry telemetry to uploads", [f"symbol:{X}::s", f"symbol:{Y}::t"],
            )
            for item_id in (a, c, b):
                await _claim(db, pid, item_id, sess)
            assert await _coarse(db, b) == []

            first, second = (a, c) if order == "a-first" else (c, a)
            await asyncio.gather(
                release(db, pid, first, sess), release(db, pid, second, sess),
            )

            where = f"round {round_} {order}"
            assert await _holder(db, X) == sess, where
            assert await _holder(db, Y) == sess, where
            assert await _coarse(db, b) == [X, Y], f"{where}: a hand-off was lost"

            await release(db, pid, b, sess)

            assert await _holder(db, X) is None, f"{where}: X leaked"
            assert await _holder(db, Y) is None, f"{where}: Y leaked"


# ---------------------------------------------------------------------------
# F1 — the sibling leaves inside the releaser's hand-off window
# ---------------------------------------------------------------------------

async def _b_leaves_by_release(db, pid, b, sess, _receiver):
    await _release_db(db, pid, b, sess)


async def _b_leaves_by_stale_reset(db, pid, b, sess, _receiver):
    assert await sprint_items_module._reset_stale_claim(db, pid, b, STALE) is not None


async def _b_leaves_by_transfer(db, pid, b, sess, receiver):
    moved = await db_module.transfer_sprint_item_claim(
        db, pid, b, sess, "bob", to_session_id=receiver,
    )
    assert not moved.get("blocked"), moved


@pytest.mark.parametrize(
    "b_leaves",
    [_b_leaves_by_release, _b_leaves_by_stale_reset, _b_leaves_by_transfer],
    ids=["release", "stale-reset", "transfer"],
)
async def test_sibling_leaving_inside_the_hand_off_window_does_not_strand_the_lock(
    db, monkeypatch, b_leaves,
):
    """A=file:X, B=symbol:X::s (claimed second: X is not B's). A's release
    keeps X for B; B then leaves the session's in_progress set AFTER A's
    sibling check but BEFORE A's hand-off is written."""
    pid = (await db_module.create_project(db, "handoff-window"))["id"]
    sess = await _session(db, pid)
    receiver = await _session(db, pid, "receiver")
    a = await _item(db, pid, "Refactor the config parser", [f"file:{X}"])
    b = await _item(db, pid, "Add retry telemetry to uploads", [f"symbol:{X}::s"])
    await _claim(db, pid, a, sess)
    await _claim(db, pid, b, sess)

    window_open = asyncio.Event()
    b_left = asyncio.Event()
    original = sprint_items_module._apply_file_lock_handoffs

    async def _paused_hand_off(db_, siblings):
        if siblings.handoffs and not window_open.is_set():
            window_open.set()
            await b_left.wait()
        return await original(db_, siblings)

    monkeypatch.setattr(sprint_items_module, "_apply_file_lock_handoffs", _paused_hand_off)

    async def _b_in_window():
        await window_open.wait()
        try:
            await b_leaves(db, pid, b, sess, receiver)
        finally:
            b_left.set()

    released_a, _ = await asyncio.wait_for(
        asyncio.gather(_release_db(db, pid, a, sess), _b_in_window()), timeout=60,
    )
    assert window_open.is_set(), "the hand-off window was never reached"

    assert await _status(db, a) == "pending"
    assert await _holder(db, X) != sess, "X stays locked by the session with no item needing it"
    assert f"file:{X}" in released_a["released_resources"]
    assert "kept_for_sibling_items" not in released_a
    if b_leaves is _b_leaves_by_transfer:
        # B lives on under the receiver; the session it left holds nothing.
        await _release_db(db, pid, b, receiver)
    assert await _holder(db, X) is None
    claimed = await _other_session_can_claim(db, pid, X)
    assert claimed.get("status") == "in_progress", claimed


# ---------------------------------------------------------------------------
# F1 — the hand-off lands between the sibling's read and its own transition
# ---------------------------------------------------------------------------

async def test_hand_off_landing_before_the_siblings_release_transition_is_honoured(
    db, monkeypatch,
):
    """B's release has read its row (coarse_lock_files NULL) when A's release
    hands X to B. B's leaving transition must not act on the stale snapshot."""
    pid = (await db_module.create_project(db, "handoff-before-transition"))["id"]
    sess = await _session(db, pid)
    a = await _item(db, pid, "Refactor the config parser", [f"file:{X}"])
    b = await _item(db, pid, "Add retry telemetry to uploads", [f"symbol:{X}::s"])
    await _claim(db, pid, a, sess)
    await _claim(db, pid, b, sess)

    b_paused = asyncio.Event()
    a_done = asyncio.Event()
    original = sprint_items_module._transition_status

    async def _paused_transition(db_, project_id, item_id, to_status, *args, **kwargs):
        if item_id == b and to_status == "pending" and not b_paused.is_set():
            b_paused.set()
            await a_done.wait()
        return await original(db_, project_id, item_id, to_status, *args, **kwargs)

    monkeypatch.setattr(sprint_items_module, "_transition_status", _paused_transition)

    async def _a_while_b_paused():
        await b_paused.wait()
        try:
            return await _release_db(db, pid, a, sess)
        finally:
            a_done.set()

    _, released_a = await asyncio.wait_for(
        asyncio.gather(_release_db(db, pid, b, sess), _a_while_b_paused()), timeout=60,
    )

    assert [k["sibling_item_ids"] for k in released_a["kept_for_sibling_items"]] == [[b]]
    assert await _status(db, a) == "pending" and await _status(db, b) == "pending"
    assert await _holder(db, X) is None, "B released on a stale snapshot and stranded X"


async def test_hand_off_landing_before_a_transfer_flip_moves_with_the_item(db, monkeypatch):
    """T=symbol:X::s is being transferred to another session when A=file:X's
    release hands X to T (still in_progress under the old session). The
    transfer must not overwrite that hand-off and leave X with the old session."""
    pid = (await db_module.create_project(db, "handoff-before-flip"))["id"]
    sess = await _session(db, pid)
    receiver = await _session(db, pid, "receiver")
    a = await _item(db, pid, "Refactor the config parser", [f"file:{X}"])
    t = await _item(db, pid, "Add retry telemetry to uploads", [f"symbol:{X}::s"])
    await _claim(db, pid, a, sess)
    await _claim(db, pid, t, sess)

    t_paused = asyncio.Event()
    a_done = asyncio.Event()
    original = sprint_items_module._transition_status

    async def _paused_transition(db_, project_id, item_id, to_status, *args, **kwargs):
        if item_id == t and to_status == "in_progress" and not t_paused.is_set():
            t_paused.set()
            await a_done.wait()
        return await original(db_, project_id, item_id, to_status, *args, **kwargs)

    monkeypatch.setattr(sprint_items_module, "_transition_status", _paused_transition)

    async def _a_while_t_paused():
        await t_paused.wait()
        try:
            return await _release_db(db, pid, a, sess)
        finally:
            a_done.set()

    moved, _ = await asyncio.wait_for(
        asyncio.gather(
            db_module.transfer_sprint_item_claim(db, pid, t, sess, "bob", to_session_id=receiver),
            _a_while_t_paused(),
        ),
        timeout=60,
    )
    assert not moved.get("blocked"), moved

    assert await _holder(db, X) != sess, "X stranded with the session T left"
    await _release_db(db, pid, t, receiver)
    assert await _holder(db, X) is None


async def test_transfer_reacquisition_is_not_recorded_as_a_pivot(db):
    """The transfer flips the item to the receiver BEFORE re-acquiring its
    locks there; re-acquiring the owned coarse lock must not look like a
    mid-execution pivot (claim_file's touches_resources amendment would add
    file:X to an item that declares only symbol:X::s, and flag
    resources_amended)."""
    resource = f"symbol:{X}::s"
    pid = (await db_module.create_project(db, "transfer-no-pivot"))["id"]
    sess = await _session(db, pid)
    receiver = await _session(db, pid, "receiver")
    item = await _item(db, pid, "Add retry telemetry to uploads", [resource])
    await _claim(db, pid, item, sess)
    before = await db_module.get_sprint_item(db, item)

    moved = await db_module.transfer_sprint_item_claim(
        db, pid, item, sess, "bob", to_session_id=receiver,
    )

    assert moved["transferred_resources"] == [resource]
    assert await _holder(db, X) == receiver
    after = await db_module.get_sprint_item(db, item)
    assert db_module.parse_touches_resources(after["touches_resources"]) == [resource]
    assert after.get("resources_amended") == before.get("resources_amended")


# ---------------------------------------------------------------------------
# F1 — ownership that no declaration covers any more is still released
# ---------------------------------------------------------------------------

async def test_owned_whole_file_lock_is_released_after_its_declaration_was_edited_away(db):
    pid = (await db_module.create_project(db, "owned-undeclared"))["id"]
    sess = await _session(db, pid)
    item = await _item(db, pid, "Add retry telemetry to uploads", [f"symbol:{X}::s"])
    claimed = await _claim(db, pid, item, sess)
    assert json.loads(claimed["coarse_lock_files"]) == [X]
    await db.execute(
        "UPDATE sprint_items SET touches_resources = ? WHERE id = ?",
        (json.dumps([f"file:{Y}"]), item),
    )
    await db.commit()

    await _release_db(db, pid, item, sess)

    assert await _holder(db, X) is None


# ---------------------------------------------------------------------------
# F2 — malformed coarse_lock_files
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("5", set(), id="int"),
        pytest.param("true", set(), id="bool"),
        pytest.param('"pkg/mod.py"', set(), id="json-string"),
        pytest.param("{}", set(), id="object"),
        pytest.param('{"pkg/mod.py": 1}', set(), id="object-with-path-key"),
        pytest.param('[1, null, "pkg/x.py"]', {"pkg/x.py"}, id="mixed-list"),
        pytest.param('["pkg/x.py", "", "  "]', {"pkg/x.py"}, id="empty-entries"),
        pytest.param("not json", set(), id="malformed-json"),
        pytest.param('["pkg/x.py"', set(), id="truncated-json"),
        pytest.param('[" ./pkg\\\\x.py "]', {"pkg/x.py"}, id="normalized-like-a-declaration"),
        pytest.param("[]", set(), id="empty-list"),
        pytest.param(None, set(), id="null"),
    ],
)
def test_coarse_lock_paths_accepts_only_a_list_of_path_strings(raw, expected):
    assert sprint_items_module._coarse_lock_paths(
        {"id": "item-1", "coarse_lock_files": raw},
    ) == expected


@pytest.mark.parametrize(
    "raw",
    ["5", "true", '"pkg/mod.py"', "{}", '[1, null, "pkg/x.py"]', "not json"],
)
async def test_malformed_coarse_lock_files_never_breaks_release_or_transfer(db, caplog, raw):
    pid = (await db_module.create_project(db, "malformed-coarse"))["id"]
    sess = await _session(db, pid)
    receiver = await _session(db, pid, "receiver")
    released_item = await _item(db, pid, "Refactor the config parser", ["symbol:pkg/mod.py::helper"])
    moved_item = await _item(db, pid, "Harden the upload path", ["symbol:pkg/other.py::helper"])
    await _claim(db, pid, released_item, sess)
    await _claim(db, pid, moved_item, sess)
    for item_id in (released_item, moved_item):
        await db.execute(
            "UPDATE sprint_items SET coarse_lock_files = ? WHERE id = ?", (raw, item_id),
        )
    await db.commit()

    with caplog.at_level(logging.WARNING, logger="meridian.db.sprint_items"):
        released = await _release_db(db, pid, released_item, sess)
        moved = await db_module.transfer_sprint_item_claim(
            db, pid, moved_item, sess, "bob", to_session_id=receiver,
        )

    assert released["item"]["status"] == "pending"
    assert not moved.get("blocked"), moved
    assert moved["item"]["actor"] == "bob"
    assert moved["item"]["lock_session_id"] == receiver
    events = {
        e["event_type"]
        for e in await db_module.get_action_audit_log(db, project_id=pid)
    }
    assert sprint_items_module.SPRINT_ITEM_CLAIM_RELEASED_AUDIT_EVENT in events
    assert sprint_items_module.SPRINT_ITEM_CLAIM_TRANSFERRED_AUDIT_EVENT in events
    warnings = [r.getMessage() for r in caplog.records if "coarse_lock_files" in r.getMessage()]
    assert warnings, "a malformed ownership record must be logged"
    assert not any("pkg/" in message for message in warnings), "the stored value was logged"
