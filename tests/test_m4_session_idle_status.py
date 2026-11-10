"""m4 -- an active session with a recent heartbeat must not be reported idle.

Root cause: ``expire_idle_sessions`` flips a session that has shown no liveness
for 30 minutes from status 'active' to 'idle'. Every later liveness signal
(heartbeat, MCP tool call, tunnel keepalive, passive touch) refreshed
``last_seen`` but left ``status`` at 'idle'. The dashboard's ``isLiveSession``
requires status 'active', so a session doing live work, including the current
one, kept rendering as idle.

These tests pin the fix: any liveness bump on an 'idle' session returns it to
'active', while 'closed' and 'archived' sessions are never reopened by a bump.
"""

import pytest

from meridian import db as db_module


async def _status_of(db, project_id, session_id):
    sessions = await db_module.get_sessions(db, project_id, active_only=False)
    return next(s for s in sessions if s["id"] == session_id)["status"]


async def _backdate(db, session_id):
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now', '-2 hours') WHERE id = ?",
        (session_id,),
    )
    await db.commit()


async def _sweep_to_idle(db, project_id, session_id):
    await _backdate(db, session_id)
    await db_module.expire_idle_sessions(db, max_age_minutes=30)
    assert await _status_of(db, project_id, session_id) == "idle"  # precondition


@pytest.mark.asyncio
async def test_session_with_recent_heartbeat_is_not_idle_after_sweep(db):
    p = await db_module.create_project(db, "m4-heartbeat-not-idle")
    s = await db_module.register_session(db, p["id"], "worker")
    await _backdate(db, s["id"])
    assert await db_module.heartbeat_session(db, s["id"]) is True
    await db_module.expire_idle_sessions(db, max_age_minutes=30)
    assert await _status_of(db, p["id"], s["id"]) == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bump", ["heartbeat", "tool_call", "keepalive", "passive_touch"]
)
async def test_liveness_bump_returns_idle_session_to_active(db, bump):
    p = await db_module.create_project(db, "m4-bump-revives")
    s = await db_module.register_session(db, p["id"], "worker")
    await _sweep_to_idle(db, p["id"], s["id"])

    if bump == "heartbeat":
        assert await db_module.heartbeat_session(db, s["id"]) is True
    elif bump == "tool_call":
        await db_module.update_session_seen(db, s["id"])
    elif bump == "keepalive":
        assert await db_module.keepalive_sessions(db, [s["id"]]) == 1
    else:
        assert s["id"] in await db_module.touch_latest_active_session(db, p["id"])

    assert await _status_of(db, p["id"], s["id"]) == "active"


@pytest.mark.asyncio
async def test_heartbeat_on_fresh_idle_session_reactivates_it(db):
    # last_seen is fresh (inside heartbeat's 5-minute no-op window) but the
    # status is 'idle': the early return must not skip the status repair.
    p = await db_module.create_project(db, "m4-fresh-idle")
    s = await db_module.register_session(db, p["id"], "worker")
    await db.execute("UPDATE sessions SET status = 'idle' WHERE id = ?", (s["id"],))
    await db.commit()
    assert await db_module.heartbeat_session(db, s["id"]) is True
    assert await _status_of(db, p["id"], s["id"]) == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["closed", "archived"])
async def test_liveness_bump_never_reopens_closed_or_archived(db, terminal):
    p = await db_module.create_project(db, "m4-terminal-stays")
    s = await db_module.register_session(db, p["id"], "worker")
    await db.execute("UPDATE sessions SET status = ? WHERE id = ?", (terminal, s["id"]))
    await db.commit()

    await db_module.update_session_seen(db, s["id"])
    await db_module.heartbeat_session(db, s["id"])
    await db_module.keepalive_sessions(db, [s["id"]])
    await db_module.touch_latest_active_session(db, p["id"])

    assert await _status_of(db, p["id"], s["id"]) == terminal
