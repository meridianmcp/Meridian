"""c0ddd5b3 — a claim made with an explicit ``actor`` different from its
``session_id`` must not leave its resource locks behind.

The MCP claim_sprint_item handler acquires every touches_resources lock under
``session_id`` (meridian.mcp.handler._sprint_item_resource_claim_gate), but
release_sprint_item_claim / _reset_stale_claim / transfer_sprint_item_claim
released under the item's ``actor`` (or ``from_session_id``). ``actor`` is
attribution — a human name, an orchestrator id — and legitimately differs, so
every lock stayed held until its 2h TTL, blocking every other session.

The fix records the lock-holding session on the item as ``lock_session_id``
(live ownership, kept apart from the historical ``actor``) and releases under
it, falling back to the old identity for rows that don't carry one.
"""
from __future__ import annotations

import meridian.server  # noqa: F401 — import first to avoid the handler/server import cycle
from meridian import db as db_module
from meridian import server as srv
from meridian.db import sprint_items as sprint_items_module
from meridian.mcp.handler import _sprint_item_resource_claim_gate

FILE_RES = "file:pkg/owned.py"
FILE_PATH = "pkg/owned.py"
SYMBOL_RES = "symbol:pkg/sym.py::foo"
SYMBOL_PATH = "pkg/sym.py"
SYMBOL_SRC = "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n"
STALE = {"classification": "stale", "reasons": ["test"], "signals": {}}


async def _setup(db, name: str, resources: list[str] | None = None):
    project = await db_module.create_project(db, name)
    pid = project["id"]
    item = await db_module.add_sprint_item(
        db, pid, "v1", name,
        touches_resources=[FILE_RES, SYMBOL_RES] if resources is None else resources,
        prospect_bypass=True,
    )
    sess = await db_module.register_session(db, pid, "claimer")
    return pid, item["id"], sess["id"]


async def _mcp_claim(db, pid: str, item_id: str, session_id: str, actor: str | None = "adam"):
    args = {
        "project_id": pid, "item_id": item_id, "session_id": session_id,
        "resource_contents": {SYMBOL_PATH: SYMBOL_SRC},
    }
    if actor is not None:
        args["actor"] = actor
    result = await srv._dispatch_mcp_tool("claim_sprint_item", args, db, "/tmp")
    assert "error" not in result and not result.get("blocked"), result
    return result


async def _file_holder(db, path: str = FILE_PATH):
    return ((await db_module.get_file_claims(db, path)).get("file_lock") or {}).get("session_id")


async def _symbol_holders(db, path: str = SYMBOL_PATH) -> set[str]:
    claims = await db_module.get_symbol_claims(db, path)
    return {c.get("session_id") for c in claims}


async def _holds_nothing(db, session_id: str) -> bool:
    return (
        await _file_holder(db) != session_id
        and session_id not in await _symbol_holders(db)
    )


# ---------------------------------------------------------------------------
# Claim side: the lock owner is recorded, separately from actor
# ---------------------------------------------------------------------------

async def test_mcp_claim_with_explicit_actor_records_the_lock_session(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-claim")

    result = await _mcp_claim(db, pid, item_id, sid)

    assert result["actor"] == "adam"
    assert result["lock_session_id"] == sid
    assert await _file_holder(db) == sid
    assert sid in await _symbol_holders(db)


async def test_claim_with_nothing_locked_records_no_lock_session(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-no-resources", resources=[])

    result = await _mcp_claim(db, pid, item_id, sid)

    assert result["actor"] == "adam"
    assert result["lock_session_id"] is None


async def test_db_level_claim_without_a_lock_session_writes_null(db):
    """A claim never inherits an earlier claim's lock owner."""
    pid, item_id, sid = await _setup(db, "c0ddd5b3-db-claim")
    await _mcp_claim(db, pid, item_id, sid)
    await db_module.release_sprint_item_claim(db, pid, item_id, sid)

    other = await db_module.register_session(db, pid, "other")
    item = await db_module.claim_sprint_item(db, pid, item_id, actor=other["id"])

    assert item["lock_session_id"] is None
    assert item["actor"] == other["id"]


# ---------------------------------------------------------------------------
# Release / stale-reset / transfer free the locks the session actually holds
# ---------------------------------------------------------------------------

async def test_release_frees_every_lock_held_by_the_claiming_session(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-release")
    await _mcp_claim(db, pid, item_id, sid)

    result = await srv._dispatch_mcp_tool(
        "release_sprint_item_claim",
        {"project_id": pid, "item_id": item_id, "session_id": sid, "reason": "test"},
        db, "/tmp",
    )

    assert not result.get("blocked"), result
    assert sorted(result["released_resources"]) == sorted([FILE_RES, SYMBOL_RES])
    assert await _holds_nothing(db, sid)
    assert result["item"]["status"] == "pending"
    assert result["item"]["lock_session_id"] is None
    assert result["item"]["actor"] is None


async def test_release_by_the_attribution_actor_still_frees_the_sessions_locks(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-release-by-actor")
    await _mcp_claim(db, pid, item_id, sid)

    result = await db_module.release_sprint_item_claim(db, pid, item_id, "adam")

    assert not result.get("blocked"), result
    assert await _holds_nothing(db, sid)


async def test_release_by_an_unrelated_session_is_still_refused(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-release-foreign")
    await _mcp_claim(db, pid, item_id, sid)
    stranger = await db_module.register_session(db, pid, "stranger")

    result = await db_module.release_sprint_item_claim(db, pid, item_id, stranger["id"])

    assert result["blocked"] is True
    assert result["error"] == "NOT_CLAIM_OWNER"
    assert result["lock_session_id"] == sid
    assert await _file_holder(db) == sid


async def test_forced_release_by_another_session_frees_the_owners_locks(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-release-force")
    await _mcp_claim(db, pid, item_id, sid)
    operator = await db_module.register_session(db, pid, "operator")

    result = await db_module.release_sprint_item_claim(
        db, pid, item_id, operator["id"], force=True,
    )

    assert not result.get("blocked"), result
    assert await _holds_nothing(db, sid)


async def test_stale_reset_frees_every_lock_held_by_the_claiming_session(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-stale")
    await _mcp_claim(db, pid, item_id, sid)

    reset = await sprint_items_module._reset_stale_claim(db, pid, item_id, STALE)

    assert reset is not None
    assert sorted(reset["released_resources"]) == sorted([FILE_RES, SYMBOL_RES])
    assert await _holds_nothing(db, sid)
    assert reset["item"]["lock_session_id"] is None


async def test_transfer_moves_lock_ownership_to_the_receiving_session(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-transfer", resources=[FILE_RES])
    await _mcp_claim(db, pid, item_id, sid)
    receiver = await db_module.register_session(db, pid, "receiver")

    result = await srv._dispatch_mcp_tool(
        "transfer_sprint_item_claim",
        {
            "project_id": pid, "item_id": item_id, "session_id": sid,
            "to_actor": "bob", "to_session_id": receiver["id"],
        },
        db, "/tmp",
    )

    assert not result.get("blocked"), result
    assert result["transferred_resources"] == [FILE_RES]
    assert await _file_holder(db) == receiver["id"]
    assert result["item"]["actor"] == "bob"
    assert result["item"]["lock_session_id"] == receiver["id"]

    # And the receiver's own release then frees what it now holds.
    released = await db_module.release_sprint_item_claim(db, pid, item_id, receiver["id"])
    assert not released.get("blocked"), released
    assert await _file_holder(db) is None


async def test_transfer_releases_symbol_locks_held_by_the_claiming_session(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-transfer-symbol")
    await _mcp_claim(db, pid, item_id, sid)
    receiver = await db_module.register_session(db, pid, "receiver")

    result = await db_module.transfer_sprint_item_claim(
        db, pid, item_id, sid, "bob", to_session_id=receiver["id"],
    )

    assert not result.get("blocked"), result
    assert SYMBOL_RES in result["released_only_resources"]
    assert sid not in await _symbol_holders(db)
    assert await _file_holder(db) == receiver["id"]


async def test_transfer_without_a_receiving_session_frees_the_locks_and_clears_ownership(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-transfer-human")
    await _mcp_claim(db, pid, item_id, sid)

    result = await db_module.transfer_sprint_item_claim(db, pid, item_id, sid, "bob")

    assert not result.get("blocked"), result
    assert await _holds_nothing(db, sid)
    assert result["item"]["status"] == "in_progress"
    assert result["item"]["actor"] == "bob"
    assert result["item"]["lock_session_id"] is None


async def test_same_actor_may_still_move_the_locks_to_a_new_session(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-transfer-same-actor", resources=[FILE_RES])
    await _mcp_claim(db, pid, item_id, sid)
    new_sess = await db_module.register_session(db, pid, "adam-new-session")

    result = await db_module.transfer_sprint_item_claim(
        db, pid, item_id, sid, "adam", to_session_id=new_sess["id"],
    )

    assert not result.get("blocked"), result
    assert await _file_holder(db) == new_sess["id"]
    assert result["item"]["lock_session_id"] == new_sess["id"]


async def test_same_actor_with_nothing_to_move_is_still_same_actor(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-transfer-noop", resources=[FILE_RES])
    await _mcp_claim(db, pid, item_id, sid)

    no_session = await db_module.transfer_sprint_item_claim(db, pid, item_id, sid, "adam")
    same_session = await db_module.transfer_sprint_item_claim(
        db, pid, item_id, sid, "adam", to_session_id=sid,
    )

    assert no_session["error"] == "SAME_ACTOR"
    assert same_session["error"] == "SAME_ACTOR"
    assert await _file_holder(db) == sid


# ---------------------------------------------------------------------------
# Every other way a claim ends clears live ownership; completion stays allowed
# ---------------------------------------------------------------------------

async def test_complete_by_the_claiming_session_is_allowed_and_clears_ownership(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-complete", resources=[FILE_RES])
    await _mcp_claim(db, pid, item_id, sid)

    done = await db_module.complete_sprint_item(db, pid, item_id, actor=sid)

    assert done["status"] == "done"
    assert done["lock_session_id"] is None


async def test_complete_by_an_unrelated_session_is_still_refused(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-complete-foreign", resources=[FILE_RES])
    await _mcp_claim(db, pid, item_id, sid)
    stranger = await db_module.register_session(db, pid, "stranger")

    try:
        await db_module.complete_sprint_item(db, pid, item_id, actor=stranger["id"])
    except sprint_items_module.SprintItemClaimMismatch:
        pass
    else:  # pragma: no cover — the assertion below is the real failure message
        raise AssertionError("a foreign session completed a live claim")


async def test_any_transition_away_from_in_progress_clears_the_lock_session(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-fail", resources=[FILE_RES])
    await _mcp_claim(db, pid, item_id, sid)

    failed = await db_module.fail_sprint_item(db, pid, item_id, reason="nope")

    assert failed["status"] == "failed"
    assert failed["lock_session_id"] is None


# ---------------------------------------------------------------------------
# Legacy rows (no lock_session_id) keep the old behavior
# ---------------------------------------------------------------------------

async def test_legacy_row_still_releases_under_actor(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-legacy", resources=[FILE_RES])
    await db_module.claim_sprint_item(db, pid, item_id, actor=sid)
    assert (await _sprint_item_resource_claim_gate(db, pid, item_id, sid))["ok"] is True
    assert (await db_module.get_sprint_item(db, item_id))["lock_session_id"] is None

    result = await db_module.release_sprint_item_claim(db, pid, item_id, sid)

    assert result["released_resources"] == [FILE_RES]
    assert await _file_holder(db) is None


async def test_legacy_row_stale_reset_still_releases_under_actor(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-legacy-stale", resources=[FILE_RES])
    await db_module.claim_sprint_item(db, pid, item_id, actor=sid)
    assert (await _sprint_item_resource_claim_gate(db, pid, item_id, sid))["ok"] is True

    reset = await sprint_items_module._reset_stale_claim(db, pid, item_id, STALE)

    assert reset["released_resources"] == [FILE_RES]
    assert await _file_holder(db) is None


async def test_legacy_row_transfer_still_releases_under_from_session(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-legacy-transfer", resources=[FILE_RES])
    receiver = await db_module.register_session(db, pid, "receiver")
    await db_module.claim_sprint_item(db, pid, item_id, actor=sid)
    assert (await _sprint_item_resource_claim_gate(db, pid, item_id, sid))["ok"] is True

    result = await db_module.transfer_sprint_item_claim(
        db, pid, item_id, sid, receiver["id"], to_session_id=receiver["id"],
    )

    assert result["transferred_resources"] == [FILE_RES]
    assert await _file_holder(db) == receiver["id"]
    assert result["item"]["lock_session_id"] == receiver["id"]


# ---------------------------------------------------------------------------
# Stale-claim classification judges the session that holds the claim
# ---------------------------------------------------------------------------

async def test_classifier_uses_the_lock_session_for_liveness(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-classify", resources=[FILE_RES])
    await _mcp_claim(db, pid, item_id, sid)
    await db.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (sid,))
    await db.commit()

    verdict = await db_module.classify_stale_claim(db, await db_module.get_sprint_item(db, item_id))

    # With actor="adam" alone (no session row) this was unverifiable; the
    # claiming session's explicit close now proves the claim abandoned.
    assert verdict["classification"] == "stale"
    assert verdict["signals"]["actor"] == "adam"
    assert verdict["signals"]["lock_session_id"] == sid


async def test_classifier_signals_are_unchanged_for_legacy_rows(db):
    pid, item_id, sid = await _setup(db, "c0ddd5b3-classify-legacy", resources=[FILE_RES])
    await db_module.claim_sprint_item(db, pid, item_id, actor=sid)

    verdict = await db_module.classify_stale_claim(db, await db_module.get_sprint_item(db, item_id))

    assert "lock_session_id" not in verdict["signals"]
    assert verdict["signals"]["actor"] == sid
