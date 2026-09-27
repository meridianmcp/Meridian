"""fd1eda7c — releasing one sprint item's claim must not drop a lock a
same-session sibling item still needs.

``file_locks`` holds ONE row per path and ``file_symbol_claims`` one row per
(session, path, symbol); neither carries a sprint item. When one session holds
two in_progress items that resolve (``meridian.db._resource_file_of``) to the
same real file, both claims share that single row, and every release path
(release_sprint_item_claim, _reset_stale_claim, transfer_sprint_item_claim)
released by path — so ending item A freed the file while item B was still
in_progress with no lock at all.

"Same session" is the lock-owning session c0ddd5b3 introduced
(``lock_session_id``, falling back to ``actor`` for legacy rows), never the
attribution actor alone.
"""
from __future__ import annotations

import json

import pytest

import meridian.server  # noqa: F401 — import first to avoid the handler/server import cycle
from meridian import db as db_module
from meridian import server as srv
from meridian.db import sprint_items as sprint_items_module
from meridian.mcp.handler import _sprint_item_resource_claim_gate

REAL = "pkg/mod.py"
PLAIN = f"file:{REAL}"
SHORTHAND_A = f"file:{REAL}:helper"
SHORTHAND_B = f"file:{REAL}:other"
SYMBOL_HELPER = f"symbol:{REAL}::helper"
SYMBOL_OTHER = f"symbol:{REAL}::other"
SOURCE = "def helper():\n    return 1\n\n\ndef other():\n    return 2\n"
STALE = {"classification": "stale", "reasons": ["test"], "signals": {}}


async def _project(db, name: str, resources_a: list[str], resources_b: list[str]):
    project = await db_module.create_project(db, name)
    pid = project["id"]
    a = await db_module.add_sprint_item(
        db, pid, "v1", "Refactor the config parser", touches_resources=resources_a,
        prospect_bypass=True,
    )
    b = await db_module.add_sprint_item(
        db, pid, "v1", "Add retry telemetry to uploads", touches_resources=resources_b,
        prospect_bypass=True,
    )
    assert "id" in a and "id" in b, (a, b)
    return pid, a["id"], b["id"]


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
    assert result["lock_session_id"] == session_id
    return result


async def _file_holder(db, path: str = REAL):
    return ((await db_module.get_file_claims(db, path)).get("file_lock") or {}).get("session_id")


async def _symbol_holders(db, symbol: str, path: str = REAL) -> set[str]:
    return {
        c.get("session_id")
        for c in await db_module.get_symbol_claims(db, path)
        if c.get("symbol_name") == symbol
    }


async def _release(db, pid: str, item_id: str, session_id: str):
    result = await srv._dispatch_mcp_tool(
        "release_sprint_item_claim",
        {"project_id": pid, "item_id": item_id, "session_id": session_id, "reason": "test"},
        db, "/tmp",
    )
    assert not result.get("blocked") and "error" not in result, result
    return result


def _kept(result: dict, resource: str) -> dict:
    matches = [k for k in result.get("kept_for_sibling_items") or [] if k["resource"] == resource]
    assert len(matches) == 1, result.get("kept_for_sibling_items")
    return matches[0]


# ---------------------------------------------------------------------------
# release_sprint_item_claim: one session, two items on one real file
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("res_a", "res_b"),
    [
        pytest.param(PLAIN, PLAIN, id="plain-plain"),
        pytest.param(SHORTHAND_A, SHORTHAND_B, id="shorthand-shorthand"),
        pytest.param(SHORTHAND_A, PLAIN, id="shorthand-plain"),
        pytest.param(PLAIN, SHORTHAND_A, id="plain-shorthand"),
    ],
)
async def test_releasing_one_item_keeps_the_file_its_sibling_still_needs(db, res_a, res_b):
    pid, a, b = await _project(db, f"fd1eda7c-{res_a}-{res_b}", [res_a], [res_b])
    sess = (await db_module.register_session(db, pid, "worker"))["id"]
    await _mcp_claim(db, pid, a, sess)
    await _mcp_claim(db, pid, b, sess)
    assert await _file_holder(db) == sess

    first = await _release(db, pid, a, sess)

    assert await _file_holder(db) == sess, "sibling B lost its lock"
    assert first["released_resources"] == []
    kept = _kept(first, res_a)
    assert kept == {
        "resource": res_a, "file_path": REAL, "scope": "file",
        "held_under_session_id": sess, "sibling_item_ids": [b],
    }
    assert first["item"]["status"] == "pending"

    second = await _release(db, pid, b, sess)

    assert await _file_holder(db) is None
    assert second["released_resources"] == [res_b]
    assert "kept_for_sibling_items" not in second


async def test_file_item_release_keeps_the_lock_for_a_symbol_sibling(db):
    """file/symbol: B's symbol claim (a real AST range) and the file lock both survive."""
    pid, a, b = await _project(db, "fd1eda7c-file-symbol", [PLAIN], [SYMBOL_HELPER])
    sess = (await db_module.register_session(db, pid, "worker"))["id"]
    await _mcp_claim(db, pid, a, sess)
    await _mcp_claim(db, pid, b, sess)
    assert sess in await _symbol_holders(db, "helper")

    first = await _release(db, pid, a, sess)

    # release_file would also have soft-released every symbol claim the
    # session holds on the file, B's included.
    assert await _file_holder(db) == sess
    assert sess in await _symbol_holders(db, "helper")
    assert _kept(first, PLAIN)["sibling_item_ids"] == [b]

    second = await _release(db, pid, b, sess)

    assert second["released_resources"] == [SYMBOL_HELPER]
    assert await _file_holder(db) is None, "the lock kept for B must go with B"
    assert await _symbol_holders(db, "helper") == set()


async def test_symbol_item_release_keeps_the_file_lock_of_a_file_sibling(db):
    pid, a, b = await _project(db, "fd1eda7c-symbol-first", [SYMBOL_HELPER], [PLAIN])
    sess = (await db_module.register_session(db, pid, "worker"))["id"]
    await _mcp_claim(db, pid, a, sess)
    await _mcp_claim(db, pid, b, sess)

    first = await _release(db, pid, a, sess)

    # A's own symbol claim is released; the whole-file lock B needs is not.
    assert first["released_resources"] == [SYMBOL_HELPER]
    assert await _symbol_holders(db, "helper") == set()
    assert await _file_holder(db) == sess
    assert _kept(first, SYMBOL_HELPER)["scope"] == "file"

    await _release(db, pid, b, sess)
    assert await _file_holder(db) is None


async def test_coarse_symbol_sibling_keeps_the_shared_file_lock(db):
    """No source supplied: B's symbol widened to the SAME whole-file lock row A holds."""
    pid, a, b = await _project(db, "fd1eda7c-coarse", [PLAIN], [SYMBOL_HELPER])
    sess = (await db_module.register_session(db, pid, "worker"))["id"]
    await _mcp_claim(db, pid, a, sess, source=False)
    await _mcp_claim(db, pid, b, sess, source=False)
    assert await _symbol_holders(db, "helper") == set()

    first = await _release(db, pid, a, sess)
    assert await _file_holder(db) == sess
    assert _kept(first, PLAIN)["sibling_item_ids"] == [b]

    second = await _release(db, pid, b, sess)
    assert second["released_resources"] == [SYMBOL_HELPER]
    assert await _file_holder(db) is None


async def test_same_symbol_on_two_items_keeps_the_single_symbol_claim(db):
    pid, a, b = await _project(db, "fd1eda7c-same-symbol", [SYMBOL_HELPER], [SYMBOL_HELPER])
    sess = (await db_module.register_session(db, pid, "worker"))["id"]
    await _mcp_claim(db, pid, a, sess)
    await _mcp_claim(db, pid, b, sess)

    first = await _release(db, pid, a, sess)

    assert sess in await _symbol_holders(db, "helper")
    kept = _kept(first, SYMBOL_HELPER)
    assert kept["scope"] == "symbol" and kept["symbol"] == "helper"
    assert kept["sibling_item_ids"] == [b]

    await _release(db, pid, b, sess)
    assert await _symbol_holders(db, "helper") == set()


async def test_different_symbols_on_one_file_were_already_independent(db):
    """file_symbol_claims is per (session, path, symbol): nothing to keep."""
    pid, a, b = await _project(db, "fd1eda7c-diff-symbols", [SYMBOL_HELPER], [SYMBOL_OTHER])
    sess = (await db_module.register_session(db, pid, "worker"))["id"]
    await _mcp_claim(db, pid, a, sess)
    await _mcp_claim(db, pid, b, sess)

    first = await _release(db, pid, a, sess)

    assert first["released_resources"] == [SYMBOL_HELPER]
    assert "kept_for_sibling_items" not in first
    assert await _symbol_holders(db, "helper") == set()
    assert sess in await _symbol_holders(db, "other")


async def test_coarse_symbol_lock_is_released_with_its_item(db):
    """The gate widens an unresolvable symbol to a whole-file lock; releasing
    the symbol row alone left that lock held until its TTL."""
    project = await db_module.create_project(db, "fd1eda7c-coarse-leak")
    pid = project["id"]
    item = await db_module.add_sprint_item(
        db, pid, "v1", "coarse", touches_resources=[SYMBOL_HELPER], prospect_bypass=True,
    )
    sess = (await db_module.register_session(db, pid, "worker"))["id"]
    await _mcp_claim(db, pid, item["id"], sess, source=False)
    # Widened: a whole-file lock, no symbol row.
    assert await _file_holder(db) == sess
    assert await _symbol_holders(db, "helper") == set()

    result = await _release(db, pid, item["id"], sess)

    assert result["released_resources"] == [SYMBOL_HELPER]
    assert await _file_holder(db) is None


# ---------------------------------------------------------------------------
# Only the SAME lock-owning session's items are siblings
# ---------------------------------------------------------------------------

async def test_an_item_held_by_a_different_session_is_not_a_sibling(db):
    """Same attribution actor, different lock-owning session: A's lock goes."""
    pid, a, b = await _project(db, "fd1eda7c-other-session", [PLAIN], [PLAIN])
    s1 = (await db_module.register_session(db, pid, "s1"))["id"]
    s2 = (await db_module.register_session(db, pid, "s2"))["id"]
    await _mcp_claim(db, pid, a, s1)
    await db_module.claim_sprint_item(db, pid, b, actor="adam", lock_session_id=s2)

    result = await _release(db, pid, a, s1)

    assert result["released_resources"] == [PLAIN]
    assert "kept_for_sibling_items" not in result
    assert await _file_holder(db) is None


async def test_two_sessions_each_keep_their_own_locks(db):
    project = await db_module.create_project(db, "fd1eda7c-two-sessions")
    pid = project["id"]
    titles = (
        "Refactor the config parser", "Add retry telemetry to uploads",
        "Document the billing webhook", "Speed up dashboard rendering",
    )
    items = [
        (await db_module.add_sprint_item(
            db, pid, "v1", title, touches_resources=[f"file:pkg/f{n}.py"], prospect_bypass=True,
        ))["id"]
        for n, title in enumerate(titles)
    ]
    s1 = (await db_module.register_session(db, pid, "s1"))["id"]
    s2 = (await db_module.register_session(db, pid, "s2"))["id"]
    for item_id, sess in zip(items, (s1, s1, s2, s2)):
        await _mcp_claim(db, pid, item_id, sess)

    result = await _release(db, pid, items[0], s1)

    assert result["released_resources"] == ["file:pkg/f0.py"]
    assert "kept_for_sibling_items" not in result
    assert await _file_holder(db, "pkg/f0.py") is None
    assert await _file_holder(db, "pkg/f1.py") == s1
    assert await _file_holder(db, "pkg/f2.py") == s2
    assert await _file_holder(db, "pkg/f3.py") == s2


async def test_legacy_rows_are_siblings_through_their_actor(db):
    """No lock_session_id on either row: the actor is the lock owner (c0ddd5b3)."""
    pid, a, b = await _project(db, "fd1eda7c-legacy", [PLAIN], [SHORTHAND_A])
    sess = (await db_module.register_session(db, pid, "worker"))["id"]
    for item_id in (a, b):
        await db_module.claim_sprint_item(db, pid, item_id, actor=sess)
        assert (await _sprint_item_resource_claim_gate(db, pid, item_id, sess))["ok"] is True
        assert (await db_module.get_sprint_item(db, item_id))["lock_session_id"] is None

    first = await db_module.release_sprint_item_claim(db, pid, a, sess)

    assert await _file_holder(db) == sess
    assert _kept(first, PLAIN)["sibling_item_ids"] == [b]

    await db_module.release_sprint_item_claim(db, pid, b, sess)
    assert await _file_holder(db) is None


async def test_a_sibling_that_is_no_longer_in_progress_does_not_keep_the_lock(db):
    pid, a, b = await _project(db, "fd1eda7c-sibling-done", [PLAIN], [PLAIN])
    sess = (await db_module.register_session(db, pid, "worker"))["id"]
    await _mcp_claim(db, pid, a, sess)
    await _mcp_claim(db, pid, b, sess)
    await db_module.complete_sprint_item(db, pid, b, actor=sess)

    result = await _release(db, pid, a, sess)

    assert result["released_resources"] == [PLAIN]
    assert await _file_holder(db) is None


# ---------------------------------------------------------------------------
# Stale reset
# ---------------------------------------------------------------------------

async def test_stale_reset_keeps_the_file_a_sibling_still_needs(db):
    pid, a, b = await _project(db, "fd1eda7c-stale", [SHORTHAND_A], [SHORTHAND_B])
    sess = (await db_module.register_session(db, pid, "worker"))["id"]
    await _mcp_claim(db, pid, a, sess)
    await _mcp_claim(db, pid, b, sess)

    first = await sprint_items_module._reset_stale_claim(db, pid, a, STALE)

    assert first["released_resources"] == []
    assert _kept(first, SHORTHAND_A)["sibling_item_ids"] == [b]
    assert await _file_holder(db) == sess

    second = await sprint_items_module._reset_stale_claim(db, pid, b, STALE)

    assert second["released_resources"] == [SHORTHAND_B]
    assert "kept_for_sibling_items" not in second
    assert await _file_holder(db) is None


async def test_stale_reset_audit_records_the_kept_locks(db):
    pid, a, b = await _project(db, "fd1eda7c-stale-audit", [PLAIN], [PLAIN])
    sess = (await db_module.register_session(db, pid, "worker"))["id"]
    await _mcp_claim(db, pid, a, sess)
    await _mcp_claim(db, pid, b, sess)

    await sprint_items_module._reset_stale_claim(db, pid, a, STALE)

    async with db.execute(
        "SELECT detail FROM action_audit_log WHERE event_type = ? AND project_id = ?",
        (sprint_items_module.RECONCILE_STALE_CLAIM_AUDIT_EVENT, pid),
    ) as cur:
        rows = await cur.fetchall()
    details = [json.loads(dict(r)["detail"]) for r in rows]
    assert details and details[-1]["kept_for_sibling_items"][0]["sibling_item_ids"] == [b]


# ---------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------

async def test_transfer_does_not_steal_a_lock_a_sibling_still_needs(db):
    pid, a, b = await _project(db, "fd1eda7c-transfer", [PLAIN], [PLAIN])
    sess = (await db_module.register_session(db, pid, "worker"))["id"]
    receiver = (await db_module.register_session(db, pid, "receiver"))["id"]
    await _mcp_claim(db, pid, a, sess)
    await _mcp_claim(db, pid, b, sess)

    result = await srv._dispatch_mcp_tool(
        "transfer_sprint_item_claim",
        {
            "project_id": pid, "item_id": a, "session_id": sess,
            "to_actor": "bob", "to_session_id": receiver,
        },
        db, "/tmp",
    )

    assert not result.get("blocked"), result
    assert await _file_holder(db) == sess, "the lock was moved out from under B"
    assert result["transferred_resources"] == []
    assert result["released_only_resources"] == []
    assert _kept(result, PLAIN) == {
        "resource": PLAIN, "file_path": REAL, "scope": "file",
        "held_under_session_id": sess, "sibling_item_ids": [b],
    }
    assert result["item"]["lock_session_id"] == receiver

    # Once B lets go the file is free, and A (now the receiver's) is no
    # longer a sibling of the original session.
    released_b = await _release(db, pid, b, sess)
    assert released_b["released_resources"] == [PLAIN]
    assert await _file_holder(db) is None


async def test_transfer_symbol_resource_does_not_release_a_shared_symbol_claim(db):
    pid, a, b = await _project(db, "fd1eda7c-transfer-symbol", [SYMBOL_HELPER], [SYMBOL_HELPER])
    sess = (await db_module.register_session(db, pid, "worker"))["id"]
    receiver = (await db_module.register_session(db, pid, "receiver"))["id"]
    await _mcp_claim(db, pid, a, sess)
    await _mcp_claim(db, pid, b, sess)

    result = await db_module.transfer_sprint_item_claim(
        db, pid, a, sess, "bob", to_session_id=receiver,
    )

    assert not result.get("blocked"), result
    assert sess in await _symbol_holders(db, "helper")
    assert result["released_only_resources"] == []
    assert _kept(result, SYMBOL_HELPER)["scope"] == "symbol"


async def test_transfer_to_the_lock_owning_session_counts_as_transferred(db):
    """Only the actor changes hands: the kept lock is already the receiver's."""
    pid, a, b = await _project(db, "fd1eda7c-transfer-self", [PLAIN], [PLAIN])
    sess = (await db_module.register_session(db, pid, "worker"))["id"]
    await _mcp_claim(db, pid, a, sess)
    await _mcp_claim(db, pid, b, sess)

    result = await db_module.transfer_sprint_item_claim(
        db, pid, a, sess, "bob", to_session_id=sess,
    )

    assert not result.get("blocked"), result
    assert result["transferred_resources"] == [PLAIN]
    assert "kept_for_sibling_items" not in result
    assert await _file_holder(db) == sess


async def test_transfer_without_siblings_still_moves_the_lock(db):
    pid, a, b = await _project(db, "fd1eda7c-transfer-alone", [PLAIN], ["file:pkg/elsewhere.py"])
    sess = (await db_module.register_session(db, pid, "worker"))["id"]
    receiver = (await db_module.register_session(db, pid, "receiver"))["id"]
    await _mcp_claim(db, pid, a, sess)
    await _mcp_claim(db, pid, b, sess)

    result = await db_module.transfer_sprint_item_claim(
        db, pid, a, sess, "bob", to_session_id=receiver,
    )

    assert result["transferred_resources"] == [PLAIN]
    assert "kept_for_sibling_items" not in result
    assert await _file_holder(db) == receiver
    assert await _file_holder(db, "pkg/elsewhere.py") == sess


async def test_transfer_releases_a_coarse_symbol_lock_instead_of_stranding_it(db):
    project = await db_module.create_project(db, "fd1eda7c-transfer-coarse")
    pid = project["id"]
    item = await db_module.add_sprint_item(
        db, pid, "v1", "coarse", touches_resources=[SYMBOL_HELPER], prospect_bypass=True,
    )
    sess = (await db_module.register_session(db, pid, "worker"))["id"]
    receiver = (await db_module.register_session(db, pid, "receiver"))["id"]
    await _mcp_claim(db, pid, item["id"], sess, source=False)

    result = await db_module.transfer_sprint_item_claim(
        db, pid, item["id"], sess, "bob", to_session_id=receiver,
    )

    assert result["released_only_resources"] == [SYMBOL_HELPER]
    assert await _file_holder(db) is None
