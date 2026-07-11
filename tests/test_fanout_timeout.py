"""6f9503a9 — parallel-fan-out barrier must be BOUNDED.

Root cause under test: ``server._idle_until_session_done`` is the "wait for X to
finish" primitive an orchestrator uses when it fans out a parallel batch of
subagents. It used to be an unbounded ``while True`` poll — if one watched
subagent got stuck at the network seam and never transitioned to
``closed``/``archived`` (crashed without closing, or hung mid-work with a stale
``last_seen`` but ``active`` status), the loop spun forever and hung the ENTIRE
batch on that single item.

These tests pin the fix: the wait is bounded by ``timeout_seconds`` and, on a
stuck session, returns ``done=False, timed_out=True`` FAST (does not hang) so the
caller fails that one item and lets the rest of the batch continue. The normal
"session closes" path, and the explicit ``timeout_seconds=None`` unbounded
opt-out, are also covered so the fix doesn't regress the happy path.
"""

from __future__ import annotations

import asyncio

import pytest

from meridian import db as db_module
from meridian import server


async def _make_active_session(db, *, name: str = "subagent") -> str:
    """Create a project + an ``active`` session; return the session id."""
    project = await db_module.create_project(db, f"fanout-{name}")
    session = await db_module.register_session(db, project["id"], name)
    return session["id"]


@pytest.mark.asyncio
async def test_stuck_session_times_out_fast_instead_of_hanging(db):
    """A never-closing subagent must NOT hang the barrier — it must time out.

    This is the core regression: wrap the call in asyncio.wait_for with a
    generous ceiling. Before the fix this call was an unbounded ``while True``
    and would blow the ceiling; after the fix it returns on its own bound.
    """
    sid = await _make_active_session(db)

    # Small real timeout + poll so the test is fast and deterministic. The outer
    # wait_for ceiling (5s) is FAR larger than the 0.3s barrier timeout, so if
    # the barrier were still unbounded this test would fail with TimeoutError.
    result = await asyncio.wait_for(
        server._idle_until_session_done(
            db, sid, poll_seconds=1, timeout_seconds=0.3
        ),
        timeout=5.0,
    )

    assert result["done"] is False
    assert result["timed_out"] is True
    assert result["status"] == "active"  # never closed — that's the whole point
    assert result["session_id"] == sid


@pytest.mark.asyncio
async def test_timed_out_result_is_distinguishable_so_batch_continues(db):
    """The fallback must be machine-distinguishable from a real completion.

    An orchestrator keys "fail this item fast, keep going" on the difference
    between a genuine done (``done=True``) and a bailout (``timed_out=True``).
    """
    sid = await _make_active_session(db)

    stuck = await asyncio.wait_for(
        server._idle_until_session_done(
            db, sid, poll_seconds=1, timeout_seconds=0.2
        ),
        timeout=5.0,
    )
    # Distinguishable: not done, explicitly timed out, carries the id to fail.
    assert stuck["done"] is False and stuck.get("timed_out") is True
    assert stuck.get("waited_seconds") == 0.2


@pytest.mark.asyncio
async def test_closed_session_returns_done_before_timeout(db):
    """The happy path is unaffected: a session that closes returns done=True.

    We close the watched session concurrently while the barrier polls, and the
    barrier must observe the transition and return a normal completion — with no
    ``timed_out`` flag.
    """
    sid = await _make_active_session(db)

    async def _close_soon():
        await asyncio.sleep(0.1)
        await db_module.close_session(db, sid)

    closer = asyncio.create_task(_close_soon())
    try:
        result = await asyncio.wait_for(
            server._idle_until_session_done(
                db, sid, poll_seconds=1, timeout_seconds=30.0
            ),
            timeout=5.0,
        )
    finally:
        await closer

    assert result["done"] is True
    assert result["status"] == "closed"
    assert "timed_out" not in result


@pytest.mark.asyncio
async def test_missing_session_returns_done_immediately(db):
    """A watched id that doesn't exist resolves immediately (done/missing)."""
    result = await asyncio.wait_for(
        server._idle_until_session_done(db, "does-not-exist", timeout_seconds=0.2),
        timeout=5.0,
    )
    assert result["done"] is True
    assert result["status"] == "missing"


@pytest.mark.asyncio
async def test_explicit_unbounded_opt_out_still_completes_on_close(db):
    """timeout_seconds=None restores the legacy unbounded wait — and must still
    return normally when the session eventually closes (no accidental hang for
    callers that opted out)."""
    sid = await _make_active_session(db)

    async def _close_soon():
        await asyncio.sleep(0.1)
        await db_module.close_session(db, sid)

    closer = asyncio.create_task(_close_soon())
    try:
        result = await asyncio.wait_for(
            server._idle_until_session_done(
                db, sid, poll_seconds=1, timeout_seconds=None
            ),
            timeout=5.0,
        )
    finally:
        await closer

    assert result["done"] is True
    assert result["status"] == "closed"


@pytest.mark.asyncio
async def test_default_timeout_is_bounded_not_infinite():
    """Guard the default: the module-level default must be a finite number so a
    caller that passes no timeout still can't hang forever."""
    assert isinstance(server._IDLE_UNTIL_DEFAULT_TIMEOUT_S, (int, float))
    assert server._IDLE_UNTIL_DEFAULT_TIMEOUT_S > 0
    assert server._IDLE_UNTIL_DEFAULT_TIMEOUT_S != float("inf")


@pytest.mark.asyncio
async def test_one_stuck_item_does_not_block_the_batch(db):
    """End-to-end fan-out semantics: a batch of barriers where one subagent is
    stuck must NOT block the batch — the stuck one times out fast while the
    others complete, and the whole gather finishes well under any hang.

    This mirrors the reported bug: a 7-item Wave-A batch where one item's
    subagent hung at the network seam. Here item 0 never closes; the rest do.
    """
    sids = [await _make_active_session(db, name=f"w{i}") for i in range(4)]
    stuck_sid = sids[0]

    async def _close_rest():
        await asyncio.sleep(0.1)
        for sid in sids[1:]:
            await db_module.close_session(db, sid)

    closer = asyncio.create_task(_close_rest())
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                *[
                    server._idle_until_session_done(
                        db, sid, poll_seconds=1, timeout_seconds=0.5
                    )
                    for sid in sids
                ]
            ),
            timeout=8.0,  # if the stuck item hung the batch, we'd never get here
        )
    finally:
        await closer

    by_sid = {r["session_id"]: r for r in results}
    # The stuck subagent bailed out fast; the batch was not blocked by it.
    assert by_sid[stuck_sid]["done"] is False
    assert by_sid[stuck_sid]["timed_out"] is True
    # Every other item completed normally.
    for sid in sids[1:]:
        assert by_sid[sid]["done"] is True
        assert by_sid[sid]["status"] == "closed"
