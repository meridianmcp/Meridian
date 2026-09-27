"""Follow-ups to c0ddd5b3 (lock_session_id) and fd1eda7c (sibling-aware release)
from an independent verification pass.

1. Releasing a ``symbol:X::s`` item freed ANY whole-file lock its session held
   on X — including one the item never took (a manual ``claim_file``, or a lock
   that belongs to another item of the same session). The claim gate knew
   which symbol resources it widened to a coarse lock it newly acquired, but
   nothing persisted it. Now ``sprint_items.coarse_lock_files`` does.
2. An actor-only transfer (``to_session_id`` == the lock owner) released the
   item's coarse whole-file lock (and, pre-existing, its real symbol claim)
   while the item stayed ``in_progress`` under that same session.
3. Every status transition hard-depended on ``lock_session_id``; a DB whose
   migration was skipped (``_run_pg_migrations`` logs and continues) would
   refuse every claim/complete/release. Writes of the new columns now degrade
   to the pre-column behavior when a column is missing.
4. ``claim_file``'s touches_resources amendment matched the item on
   ``actor = session_id`` only, so a pivot lock taken by the lock-owning
   session of a claim made with an explicit actor was never declared, and so
   never released.
"""
from __future__ import annotations

import json

import pytest

import meridian.server  # noqa: F401 — import first to avoid the handler/server import cycle
from meridian import db as db_module
from meridian import server as srv
from meridian.db import sprint_items as sprint_items_module

REAL = "pkg/mod.py"
OTHER = "pkg/other.py"
PLAIN = f"file:{REAL}"
SYMBOL_HELPER = f"symbol:{REAL}::helper"
SOURCE = "def helper():\n    return 1\n\n\ndef other():\n    return 2\n"


async def _item(db, pid: str, title: str, resources: list[str]) -> str:
    item = await db_module.add_sprint_item(
        db, pid, "v1", title, touches_resources=resources, prospect_bypass=True,
    )
    assert "id" in item, item
    return item["id"]


async def _setup(db, name: str, resources: list[str]):
    project = await db_module.create_project(db, name)
    pid = project["id"]
    item_id = await _item(db, pid, "Refactor the config parser", resources)
    sess = (await db_module.register_session(db, pid, "worker"))["id"]
    return pid, item_id, sess


async def _mcp_claim(
    db, pid: str, item_id: str, session_id: str, *, actor: str = "adam", source: bool = True,
):
    args: dict = {
        "project_id": pid, "item_id": item_id, "session_id": session_id, "actor": actor,
    }
    if source:
        args["resource_contents"] = {REAL: SOURCE}
    result = await srv._dispatch_mcp_tool("claim_sprint_item", args, db, "/tmp")
    assert "error" not in result and not result.get("blocked"), result
    return result


async def _mcp(db, name: str, args: dict):
    return await srv._dispatch_mcp_tool(name, args, db, "/tmp")


async def _release(db, pid: str, item_id: str, session_id: str):
    result = await _mcp(db, "release_sprint_item_claim", {
        "project_id": pid, "item_id": item_id, "session_id": session_id, "reason": "test",
    })
    assert not result.get("blocked") and "error" not in result, result
    return result


async def _file_holder(db, path: str = REAL):
    return ((await db_module.get_file_claims(db, path)).get("file_lock") or {}).get("session_id")


async def _symbol_holders(db, symbol: str = "helper", path: str = REAL) -> set[str]:
    return {
        c.get("session_id")
        for c in await db_module.get_symbol_claims(db, path)
        if c.get("symbol_name") == symbol
    }


# ---------------------------------------------------------------------------
# 1. A symbol release never frees a whole-file lock the item did not take
# ---------------------------------------------------------------------------

async def test_symbol_release_keeps_a_manual_file_lock_the_item_never_took(db):
    pid, a, sess = await _setup(db, "own-manual-real-symbol", [SYMBOL_HELPER])
    manual = await _mcp(db, "claim_file", {"file_path": REAL, "session_id": sess})
    assert manual.get("claimed"), manual
    await _mcp_claim(db, pid, a, sess)  # source supplied: a real symbol claim
    assert sess in await _symbol_holders(db)

    result = await _release(db, pid, a, sess)

    assert result["released_resources"] == [SYMBOL_HELPER]
    assert await _symbol_holders(db) == set()
    assert await _file_holder(db) == sess, "the session's own manual lock was freed"


async def test_coarse_widening_onto_a_pre_held_lock_is_not_the_items_to_release(db):
    """No source: the gate widens to the file lock the session ALREADY held, so
    it is not newly acquired and not recorded as the item's coarse lock."""
    pid, a, sess = await _setup(db, "own-manual-coarse", [SYMBOL_HELPER])
    assert (await _mcp(db, "claim_file", {"file_path": REAL, "session_id": sess})).get("claimed")
    claimed = await _mcp_claim(db, pid, a, sess, source=False)
    assert not (claimed.get("coarse_lock_files") or [])

    await _release(db, pid, a, sess)

    assert await _file_holder(db) == sess


async def test_newly_acquired_coarse_lock_is_recorded_and_released(db):
    pid, a, sess = await _setup(db, "own-coarse-recorded", [SYMBOL_HELPER])

    claimed = await _mcp_claim(db, pid, a, sess, source=False)

    assert json.loads(claimed["coarse_lock_files"]) == [REAL]
    result = await _release(db, pid, a, sess)
    assert result["released_resources"] == [SYMBOL_HELPER]
    assert await _file_holder(db) is None
    assert result["item"]["coarse_lock_files"] is None


async def test_symbol_release_keeps_a_file_lock_held_for_another_projects_item(db):
    """The lock rows are global per path; a same-session item in another
    project that declares file:X shares the row."""
    p1 = (await db_module.create_project(db, "own-cross-p1"))["id"]
    p2 = (await db_module.create_project(db, "own-cross-p2"))["id"]
    c = await _item(db, p1, "Refactor the config parser", [PLAIN])
    b = await _item(db, p2, "Add retry telemetry to uploads", [SYMBOL_HELPER])
    sess = (await db_module.register_session(db, p1, "worker"))["id"]
    await _mcp_claim(db, p1, c, sess)
    await _mcp_claim(db, p2, b, sess)

    await _release(db, p2, b, sess)

    assert await _file_holder(db) == sess, "item C in the other project lost its lock"


async def test_coarse_lock_is_kept_for_a_file_item_in_another_project(db):
    """B's coarse lock came first (newly acquired, so B's to release) — but a
    same-session item of another project declared the same file since."""
    p1 = (await db_module.create_project(db, "own-cross-coarse-p1"))["id"]
    p2 = (await db_module.create_project(db, "own-cross-coarse-p2"))["id"]
    b = await _item(db, p2, "Add retry telemetry to uploads", [SYMBOL_HELPER])
    c = await _item(db, p1, "Refactor the config parser", [PLAIN])
    sess = (await db_module.register_session(db, p1, "worker"))["id"]
    await _mcp_claim(db, p2, b, sess, source=False)
    await _mcp_claim(db, p1, c, sess)

    first = await _release(db, p2, b, sess)

    assert await _file_holder(db) == sess
    assert [k["sibling_item_ids"] for k in first["kept_for_sibling_items"]] == [[c]]

    await _release(db, p1, c, sess)
    assert await _file_holder(db) is None


async def test_an_owned_coarse_lock_kept_for_a_same_symbol_sibling_is_handed_to_it(db):
    """A took the coarse lock (owned); B widened onto it (not owned). Releasing
    A keeps it for B — and makes it B's, so B's release frees it."""
    project = await db_module.create_project(db, "own-handoff-symbol")
    pid = project["id"]
    a = await _item(db, pid, "Refactor the config parser", [SYMBOL_HELPER])
    b = await _item(db, pid, "Add retry telemetry to uploads", [SYMBOL_HELPER])
    sess = (await db_module.register_session(db, pid, "worker"))["id"]
    await _mcp_claim(db, pid, a, sess, source=False)
    claimed_b = await _mcp_claim(db, pid, b, sess, source=False)
    assert claimed_b["coarse_lock_files"] is None

    await _release(db, pid, a, sess)

    assert await _file_holder(db) == sess
    assert json.loads((await db_module.get_sprint_item(db, b))["coarse_lock_files"]) == [REAL]
    await _release(db, pid, b, sess)
    assert await _file_holder(db) is None


# ---------------------------------------------------------------------------
# 2. Transfers keep (or move) what the item holds
# ---------------------------------------------------------------------------

async def test_actor_only_transfer_keeps_the_coarse_file_lock(db):
    pid, a, sess = await _setup(db, "own-transfer-self-coarse", [SYMBOL_HELPER])
    await _mcp_claim(db, pid, a, sess, source=False)
    assert await _file_holder(db) == sess

    result = await db_module.transfer_sprint_item_claim(
        db, pid, a, sess, "bob", to_session_id=sess,
    )

    assert not result.get("blocked"), result
    assert await _file_holder(db) == sess, "the item stays in_progress under sess with no lock"
    assert result["transferred_resources"] == [SYMBOL_HELPER]
    assert result["released_only_resources"] == []
    assert result["item"]["lock_session_id"] == sess
    assert json.loads(result["item"]["coarse_lock_files"]) == [REAL]

    # Still the item's to release afterwards.
    released = await _release(db, pid, a, sess)
    assert released["released_resources"] == [SYMBOL_HELPER]
    assert await _file_holder(db) is None


async def test_actor_only_transfer_keeps_the_real_symbol_claim(db):
    pid, a, sess = await _setup(db, "own-transfer-self-symbol", [SYMBOL_HELPER])
    await _mcp_claim(db, pid, a, sess)

    result = await db_module.transfer_sprint_item_claim(
        db, pid, a, sess, "bob", to_session_id=sess,
    )

    assert not result.get("blocked"), result
    assert sess in await _symbol_holders(db)
    assert result["transferred_resources"] == [SYMBOL_HELPER]


async def test_transfer_moves_a_coarse_file_lock_to_the_receiving_session(db):
    pid, a, sess = await _setup(db, "own-transfer-coarse", [SYMBOL_HELPER])
    receiver = (await db_module.register_session(db, pid, "receiver"))["id"]
    await _mcp_claim(db, pid, a, sess, source=False)

    result = await db_module.transfer_sprint_item_claim(
        db, pid, a, sess, "bob", to_session_id=receiver,
    )

    assert not result.get("blocked"), result
    assert await _file_holder(db) == receiver
    assert result["transferred_resources"] == [SYMBOL_HELPER]
    assert json.loads(result["item"]["coarse_lock_files"]) == [REAL]

    await _release(db, pid, a, receiver)
    assert await _file_holder(db) is None


async def test_transfer_without_receiver_releases_the_coarse_lock_and_forgets_it(db):
    pid, a, sess = await _setup(db, "own-transfer-coarse-human", [SYMBOL_HELPER])
    await _mcp_claim(db, pid, a, sess, source=False)

    result = await db_module.transfer_sprint_item_claim(db, pid, a, sess, "bob")

    assert result["released_only_resources"] == [SYMBOL_HELPER]
    assert await _file_holder(db) is None
    assert result["item"]["coarse_lock_files"] is None


# ---------------------------------------------------------------------------
# 3. A DB without the new columns still transitions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("column", ["lock_session_id", "coarse_lock_files"])
async def test_transitions_survive_a_missing_ownership_column(db, column):
    await db.execute(f"ALTER TABLE sprint_items DROP COLUMN {column}")
    await db.commit()
    sprint_items_module._forget_sprint_item_columns(db)
    pid, a, sess = await _setup(db, f"own-missing-{column}", [PLAIN, SYMBOL_HELPER])

    await _mcp_claim(db, pid, a, sess, actor=sess, source=False)
    assert await _file_holder(db) == sess
    released = await _release(db, pid, a, sess)
    assert await _file_holder(db) is None
    assert released["item"]["status"] == "pending"

    await _mcp_claim(db, pid, a, sess, actor=sess, source=False)
    moved = await db_module.transfer_sprint_item_claim(db, pid, a, sess, "bob")
    assert moved["item"]["actor"] == "bob"
    done = await db_module.complete_sprint_item(db, pid, a, actor="bob")
    assert done["status"] == "done"

    b = await _item(db, pid, "Add retry telemetry to uploads", ["file:pkg/b.py"])
    await _mcp_claim(db, pid, b, sess, actor=sess)
    failed = await db_module.fail_sprint_item(db, pid, b, reason="nope")
    assert failed["status"] == "failed"


async def test_column_probe_caches_only_a_present_column(db):
    sprint_items_module._forget_sprint_item_columns(db)
    assert await sprint_items_module._sprint_items_has_column(db, "lock_session_id") is True
    assert await sprint_items_module._sprint_items_has_column(db, "no_such_column") is False
    await db.execute("ALTER TABLE sprint_items ADD COLUMN no_such_column TEXT")
    await db.commit()
    # A missing column is re-probed, so a later migration is picked up live.
    assert await sprint_items_module._sprint_items_has_column(db, "no_such_column") is True


# ---------------------------------------------------------------------------
# 4. Pivot claims amend the item held under lock_session_id
# ---------------------------------------------------------------------------

async def test_pivot_claim_by_the_lock_session_is_declared_and_released(db):
    pid, a, sess = await _setup(db, "own-pivot", [PLAIN])
    await _mcp_claim(db, pid, a, sess)  # actor="adam" != sess

    pivot = await _mcp(db, "claim_file", {"file_path": OTHER, "session_id": sess, "item_id": a})
    assert pivot.get("claimed"), pivot

    item = await db_module.get_sprint_item(db, a)
    assert f"file:{OTHER}" in db_module.parse_touches_resources(item["touches_resources"])
    result = await _release(db, pid, a, sess)
    assert f"file:{OTHER}" in result["released_resources"]
    assert await _file_holder(db, OTHER) is None


async def test_pivot_claim_without_item_id_finds_the_lock_sessions_item(db):
    """No initial resources: the claim still records the claiming session, so
    the heuristic (no item_id) pivot lookup finds the item and release frees it."""
    pid, a, sess = await _setup(db, "own-pivot-heuristic", [])
    claimed = await _mcp_claim(db, pid, a, sess)
    assert claimed["lock_session_id"] == sess

    pivot = await _mcp(db, "claim_file", {"file_path": OTHER, "session_id": sess})
    assert pivot.get("claimed"), pivot

    await _release(db, pid, a, sess)
    assert await _file_holder(db, OTHER) is None
