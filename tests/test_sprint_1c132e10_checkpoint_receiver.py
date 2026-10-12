"""1c132e10 -- checkpoint() adopts the pending_goal receiver argument.

Root cause (follow-up to 0527f636): ``projects.pending_goal`` is ONE read-once
slot per project. ``checkpoint()`` persists a delta continuation through that
slot WITHOUT an addressed receiver, so any sibling ``start_session`` in the
same project can pop it before the session that checkpointed.

Fix under test: ``handle_checkpoint`` addresses the persisted continuation to
the checkpointing session's own ``session_name``. A sibling registering under a
different name is withheld (the goal stays stored for the intended receiver),
while the checkpointing session still receives it. A session with no usable
name keeps the unaddressed behaviour, and an over-long name never blocks the
checkpoint itself.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import server as srv
from meridian.mcp.handlers.project_tools import handle_start_session


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _seed_project(db, name: str):
    p = await db_module.create_project(db, name)
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    return p


async def _checkpoint(db, pid: str, session_id: str, tmp_path):
    """Drive the real checkpoint MCP dispatch, as a tool call would."""
    result = await srv._dispatch_mcp_tool(
        "checkpoint", {"session_id": session_id, "project_id": pid}, db, str(tmp_path),
    )
    assert "error" not in result, result
    assert "summary" in result, result
    return result


async def _start(db, pid: str, tmp_path, name: str):
    """Drive the real start_session handler exactly as an MCP call would."""
    result = await handle_start_session(
        {"project_id": pid, "session_name": name},
        db=db,
        data_dir=str(tmp_path),
        tenant=None,
        _mcp_tenant_id=None,
        executor_sessions=set(),
    )
    assert "error" not in result, result
    return result


# ---------------------------------------------------------------------------
# the checkpointing session's own continuation is addressed to it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_addresses_continuation_to_its_own_session(db, tmp_path):
    p = await _seed_project(db, "1c132e10-own-session")
    pid = p["id"]
    owner = await db_module.register_session(db, pid, "ckpt-owner")

    await _checkpoint(db, pid, owner["id"], tmp_path)

    assert await db_module.get_pending_goal(db, pid), "checkpoint must persist a continuation"
    assert await db_module.get_pending_goal_receiver(db, pid) == {
        "session_name": "ckpt-owner",
    }


@pytest.mark.asyncio
async def test_sibling_start_session_cannot_consume_checkpoint_continuation(db, tmp_path):
    """The sibling starts FIRST (the exact steal order) -- it must be withheld,
    and the checkpointing session, starting second, still gets the goal."""
    p = await _seed_project(db, "1c132e10-sibling")
    pid = p["id"]
    owner = await db_module.register_session(db, pid, "ckpt-owner")
    await _checkpoint(db, pid, owner["id"], tmp_path)

    sibling = await _start(db, pid, tmp_path, "parallel-sibling")
    assert not sibling.get("pending_goal"), "a sibling must not consume the checkpoint goal"
    assert sibling.get("pending_goal_withheld"), "the withheld goal must be reported"
    assert await db_module.get_pending_goal(db, pid), "withheld goal stays stored"

    receiver = await _start(db, pid, tmp_path, "ckpt-owner")
    assert receiver.get("pending_goal"), "the checkpointing session still receives it"
    assert await db_module.get_pending_goal(db, pid) is None, "delivery clears the slot"


# ---------------------------------------------------------------------------
# no usable name: unchanged, unaddressed behaviour, never a blocked checkpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_without_usable_name_stays_unaddressed(db, tmp_path):
    p = await _seed_project(db, "1c132e10-blank-name")
    pid = p["id"]
    nameless = await db_module.register_session(db, pid, "   ")

    await _checkpoint(db, pid, nameless["id"], tmp_path)

    assert await db_module.get_pending_goal(db, pid), "checkpoint must persist a continuation"
    assert await db_module.get_pending_goal_receiver(db, pid) is None
    # Unaddressed: the first start_session still picks it up, as before.
    first = await _start(db, pid, tmp_path, "any-session")
    assert first.get("pending_goal")


@pytest.mark.asyncio
async def test_overlong_session_name_does_not_block_checkpoint(db, tmp_path):
    """A name over the receiver length limit makes normalisation raise; the
    checkpoint must still succeed and persist an unaddressed continuation."""
    p = await _seed_project(db, "1c132e10-long-name")
    pid = p["id"]
    owner = await db_module.register_session(db, pid, "x" * 600)

    await _checkpoint(db, pid, owner["id"], tmp_path)

    assert await db_module.get_pending_goal(db, pid), "checkpoint must persist a continuation"
    assert await db_module.get_pending_goal_receiver(db, pid) is None
