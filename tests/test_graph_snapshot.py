"""c00b1ccf — cached codebase-graph snapshot: caps, persistence, offline searcher."""
from __future__ import annotations

import asyncio

from meridian import db as db_module
from meridian import graph_snapshot as gs
from meridian import handoff as handoff_module


def test_caps_and_enabled_flag():
    assert gs.resolve_entity_cap(None) == 500
    assert gs.resolve_entity_cap({}) == 500
    assert gs.resolve_entity_cap({"graph_snapshot_cap": 100}) == 100
    assert gs.resolve_entity_cap({"graph_snapshot_cap": 99999}) == 5000   # clamp to max
    assert gs.resolve_entity_cap({"graph_snapshot_cap": 0}) == 1          # clamp to min
    assert gs.resolve_entity_cap({"graph_snapshot_cap": "bad"}) == 500
    assert gs.graph_snapshot_enabled({"graph_snapshot_enabled": True}) is True
    assert gs.graph_snapshot_enabled({}) is False
    assert gs.graph_snapshot_enabled(None) is False


def test_upsert_enforces_cap_and_search_matches():
    async def _run():
        db = await db_module.init_db(":memory:")
        ents = [
            {"qualified_name": f"mod.func_{i}", "file": f"f{i}.py", "kind": "function"}
            for i in range(10)
        ]
        stored = await db_module.upsert_graph_entities(db, "p1", ents, cap=5)
        cnt = await db_module.count_graph_entities(db, "p1")
        # A second upsert REPLACES the snapshot; a blank-named entity is dropped.
        stored2 = await db_module.upsert_graph_entities(db, "p1", [
            {"qualified_name": "auth.login", "file": "auth.py"},
            {"qualified_name": "   ", "file": "blank.py"},
        ], cap=500)
        cnt2 = await db_module.count_graph_entities(db, "p1")
        hits = await db_module.search_graph_entities(db, "p1", "fix the login flow")
        short = await db_module.search_graph_entities(db, "p1", "zz")  # tokens < 3 chars
        return stored, cnt, stored2, cnt2, hits, short

    stored, cnt, stored2, cnt2, hits, short = asyncio.run(_run())
    assert stored == 5 and cnt == 5          # capped
    assert stored2 == 1 and cnt2 == 1        # replaced; blank dropped
    assert any(h["file"] == "auth.py" for h in hits)   # matched "login"
    assert short == []


def test_snapshot_searcher_finds_entities():
    async def _run():
        db = await db_module.init_db(":memory:")
        await db_module.upsert_graph_entities(db, "p1", [
            {"qualified_name": "billing.charge_customer", "file": "billing.py"},
        ])
        searcher = gs.make_snapshot_searcher(db, "p1")
        return await searcher("charge the customer")

    matches = asyncio.run(_run())
    assert any(m["file"] == "billing.py" for m in matches)


def test_resolver_registration_wires_dead_path():
    # Before c00b1ccf the resolver was never registered → _resolve_graph_searcher
    # always returned None. Registering a snapshot resolver makes it return a
    # searcher (this is what server startup now does).
    try:
        handoff_module.set_graph_searcher_resolver(None)  # deterministic start
        assert handoff_module._resolve_graph_searcher("p1") is None  # unregistered
        db_obj = object()
        handoff_module.set_graph_searcher_resolver(
            lambda pid: gs.make_snapshot_searcher(db_obj, pid)
        )
        assert callable(handoff_module._resolve_graph_searcher("p1"))
    finally:
        handoff_module.set_graph_searcher_resolver(None)
