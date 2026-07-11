"""b74099b2 — a server-side DEPLOY must reach already-connected MCP sessions.

Bug: adding a new HOSTED MCP tool (e.g. search_outputs) via a server-side deploy
is invisible to sessions that are already connected. The tunnel
``notifications/tools/list_changed`` mechanism (54ddd609) only ever fired on a
slot-health RECOVERY (unhealthy->healthy), never on a deploy — a deploy lands as a
fresh process start, which no recovery path covers.

Fix: a SECOND trigger on server STARTUP. The lifespan enumerates the tenants whose
binary tunnel is still marked active (``tenants.tunnel_active = 1``, persisted across
the deploy because the old process died without clearing it) and calls
``notify_tools_list_changed(tid)`` for each, so the next ``tools/list`` from an
already-connected session re-aggregates and picks up the newly-deployed tool set.

This reuses the EXISTING pending mechanism (``_tools_list_changed_pending`` /
``notify_tools_list_changed`` / ``consume_tools_list_changed``); it does not build a
new one. These tests mirror the 54ddd609 pending-mechanism test pattern and add:

  * the DB accessor ``list_active_tunnel_tenant_ids`` returns only active tenants;
  * the startup trigger marks every active tenant pending (consume True once, then
    False — fire-once drain);
  * zero active tenants is a clean no-op;
  * the trigger is guarded — it never raises even if the tenant query blows up.

Pure in-process unit tests — no server, ports, network, or sleeps.
"""
from __future__ import annotations

import asyncio

import pytest

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian import db as db_module
from meridian.routes import tunnel as tn


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset the per-process pending registry around each test (mirrors 54ddd609)."""
    def _reset():
        tn._slot_health.clear()
        tn._slot_status_detail.clear()
        tn._tunnel_tool_routes.clear()
        tn._tools_list_changed_pending.clear()
    _reset()
    yield
    _reset()


async def _mark_tunnel_active(db, email: str, active: bool):
    """Create a tenant and set its tunnel_active flag; return its id."""
    t = await db_module.upsert_tenant(db, email)
    await db_module.update_tenant(db, t["id"], tunnel_active=1 if active else 0)
    return t["id"]


async def _startup_trigger(db) -> None:
    """Exactly what the server lifespan does on startup (b74099b2) — factored out so
    the trigger's semantics are unit-testable without booting the whole app. Kept in
    lockstep with the guarded block in meridian/server.py:lifespan."""
    try:
        for tid in await db_module.list_active_tunnel_tenant_ids(db):
            tn.notify_tools_list_changed(tid)
    except Exception:  # noqa: BLE001 — must never block startup
        pass


# ---------------------------------------------------------------------------
# The DB accessor — returns only tunnel_active tenants
# ---------------------------------------------------------------------------

def test_list_active_tunnel_tenant_ids_returns_only_active():
    async def _run():
        db = await db_module.init_db(":memory:")
        a = await _mark_tunnel_active(db, "active1@example.com", True)
        b = await _mark_tunnel_active(db, "active2@example.com", True)
        # An inactive tenant (tunnel_active default 0) must be excluded.
        await _mark_tunnel_active(db, "idle@example.com", False)
        ids = await db_module.list_active_tunnel_tenant_ids(db)
        return set(ids), {a, b}

    got, expected = asyncio.run(_run())
    assert got == expected


def test_list_active_tunnel_tenant_ids_empty_when_none_active():
    async def _run():
        db = await db_module.init_db(":memory:")
        await _mark_tunnel_active(db, "idle-only@example.com", False)
        return await db_module.list_active_tunnel_tenant_ids(db)

    assert asyncio.run(_run()) == []


# ---------------------------------------------------------------------------
# The startup trigger — marks every active tenant pending (fire-once drain)
# ---------------------------------------------------------------------------

def test_startup_trigger_marks_each_active_tenant_pending_once():
    async def _run():
        db = await db_module.init_db(":memory:")
        ids = [
            await _mark_tunnel_active(db, f"t{i}@example.com", True)
            for i in range(3)
        ]
        await _mark_tunnel_active(db, "not-connected@example.com", False)
        await _startup_trigger(db)
        return ids

    ids = asyncio.run(_run())

    # Every active tenant is now pending — consume returns True exactly once...
    for tid in ids:
        assert tn.consume_tools_list_changed(tid) is True
        # ...then False on a second consume (fire-once drain, no double-fire).
        assert tn.consume_tools_list_changed(tid) is False


def test_startup_trigger_skips_inactive_tenant():
    async def _run():
        db = await db_module.init_db(":memory:")
        active = await _mark_tunnel_active(db, "on@example.com", True)
        inactive = await _mark_tunnel_active(db, "off@example.com", False)
        await _startup_trigger(db)
        return active, inactive

    active, inactive = asyncio.run(_run())
    assert tn.consume_tools_list_changed(active) is True
    # The disconnected tenant was never marked — nothing to re-list for it.
    assert tn.consume_tools_list_changed(inactive) is False


def test_startup_trigger_zero_active_is_clean_noop():
    async def _run():
        db = await db_module.init_db(":memory:")
        await _mark_tunnel_active(db, "idle@example.com", False)
        await _startup_trigger(db)

    asyncio.run(_run())
    # Nothing was marked pending at all.
    assert tn._tools_list_changed_pending == set()


# ---------------------------------------------------------------------------
# Guarded — the trigger never raises even if the tenant query fails
# ---------------------------------------------------------------------------

def test_startup_trigger_never_raises_when_tenant_query_fails(monkeypatch):
    """If enumerating tenants blows up (bad DB state mid-deploy), startup must not
    die — the block is guarded. The real lifespan swallows + logs; _startup_trigger
    mirrors the swallow."""
    async def _boom(_db):
        raise RuntimeError("simulated control-plane DB failure")

    monkeypatch.setattr(db_module, "list_active_tunnel_tenant_ids", _boom)

    async def _run():
        db = await db_module.init_db(":memory:")
        # Must return cleanly despite the query raising.
        await _startup_trigger(db)

    asyncio.run(_run())
    # Nothing marked, no exception escaped.
    assert tn._tools_list_changed_pending == set()


def test_startup_trigger_is_idempotent_across_two_boots():
    """Two startups in a row (e.g. a redeploy) re-mark the same active tenant — the
    set-add is idempotent and a single drain still clears it exactly once."""
    async def _run():
        db = await db_module.init_db(":memory:")
        tid = await _mark_tunnel_active(db, "redeploy@example.com", True)
        await _startup_trigger(db)
        await _startup_trigger(db)  # second boot re-marks the same tenant
        return tid

    tid = asyncio.run(_run())
    assert tn.consume_tools_list_changed(tid) is True
    assert tn.consume_tools_list_changed(tid) is False
