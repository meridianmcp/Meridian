"""Tests for sprint item 0de0599a — keep compact start_session bounded by
deferring heavy capability contracts.

Root cause: handle_start_session (meridian/mcp/handlers/project_tools.py)
unconditionally attached the FULL capability_contract (built via
build_effective_capability_contract with capability_contract.py's library
defaults: max_executor_contracts=25, max_contract_list_items=200) to BOTH
compact and full orientations. On a real board this produced a ~593KB
start_session(compact=true) response, ~382KB of it from this one field —
defeating the entire point of the compact orientation (a slim payload an
executor's context isn't blown by).

Fix: handle_start_session now passes max_executor_contracts=0 and
max_contract_list_items=0 to build_effective_capability_contract when
compact=True, so the per-item breakdown sections (item_tool_requirements,
item_sprint_item_pointers, item_artifact_pointer_findings,
item_executor_contracts) come back empty with an honest *_truncated marker,
while the small scalar fields (executable, executable_reasons, availability,
manifest_hash, requested, effective) stay intact — those are what a session
orientation actually needs to decide "can I start working". compact=False
is byte-for-byte unaffected, matching every generate_handoff call site
(mcp/handler.py, routes/handoff.py), none of which pass these new optional
overrides.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

import meridian.server  # noqa: F401 — import first to avoid cycle
from meridian.mcp.handlers import project_tools as pt_mod
from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import capability_contract as cc


@pytest_asyncio.fixture
async def db():
    conn = await db_module.init_db(":memory:")
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def project(db):
    return await db_module.create_project(db, "compact-start-session-0de0599a")


async def _add_heavy_pending_item(db, project_id: str):
    """A pending item with structured tool_requirements — enough to populate
    item_tool_requirements/item_executor_contracts when uncapped."""
    return await db_module.add_sprint_item(
        db, project_id, "v1", "Refactor the auth module",
        tool_requirements=[{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "purpose": "locate target",
        }],
    )


async def _call_start_session(db, project_id: str, *, compact: bool | None = None) -> dict:
    """Call handle_start_session for real (no monkeypatched composite) so the
    actual capability_contract attachment logic under test runs end to end."""
    args = {"project_id": project_id, "session_name": "test-session"}
    if compact is not None:
        args["compact"] = compact
    result = await pt_mod.handle_start_session(
        args,
        db,
        "/tmp/meridian-test",
        tenant=None,  # self-hosted path (no tenant)
        _mcp_tenant_id=None,
        executor_sessions=set(),
    )
    return result  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# End-to-end: handle_start_session compact vs full
# ---------------------------------------------------------------------------

async def test_compact_start_session_caps_capability_contract_item_lists(db, project):
    item = await _add_heavy_pending_item(db, project["id"])
    result = await _call_start_session(db, project["id"])  # compact defaults True
    contract = result.get("capability_contract")
    assert contract is not None, "compact start_session must still attach a capability_contract"

    # The heavy per-item sections must be capped to empty, not populated.
    assert contract["item_tool_requirements"] == []
    assert contract["item_sprint_item_pointers"] == []
    assert contract["item_artifact_pointer_findings"] == []
    assert contract["item_executor_contracts"] == []

    # Never a silent drop: each section's _truncated sibling must say what
    # was omitted, referencing the real pending item that exists.
    assert contract["item_executor_contracts_truncated"]["included"] == 0
    assert contract["item_executor_contracts_truncated"]["total_candidates"] >= 1
    assert contract["item_tool_requirements_truncated"]["included"] == 0
    assert contract["item_tool_requirements_truncated"]["total_candidates"] >= 1

    # The scalar fields a session orientation actually needs must still be present.
    assert "executable" in contract
    assert "executable_reasons" in contract
    assert "availability" in contract
    assert "manifest_hash" in contract
    assert "requested" in contract
    assert "effective" in contract
    assert contract["project_id"] == project["id"]
    del item  # only needed to exist on the board, not referenced further


async def test_full_start_session_keeps_uncapped_capability_contract(db, project):
    """compact=False must be unaffected by the new cap — full per-item detail
    still comes through, matching pre-fix behavior exactly (parity guard)."""
    item = await _add_heavy_pending_item(db, project["id"])
    result = await _call_start_session(db, project["id"], compact=False)
    contract = result.get("capability_contract")
    assert contract is not None

    by_id = {e["item_id"]: e for e in contract["item_executor_contracts"]}
    assert item["id"] in by_id, "full start_session must still embed the per-item executor_contract"
    assert contract["item_executor_contracts_truncated"]["included"] >= 1

    tool_req_ids = {e["item_id"] for e in contract["item_tool_requirements"]}
    assert item["id"] in tool_req_ids


# ---------------------------------------------------------------------------
# Unit-level: build_effective_capability_contract kwarg passthrough
# (isolates the wrapper's behavior from handle_start_session's own decision
# of when to pass the caps, and guards generate_handoff's callers directly)
# ---------------------------------------------------------------------------

async def test_build_effective_capability_contract_default_unaffected(db, project):
    """No caller passing zero args (i.e. every existing generate_handoff call
    site in mcp/handler.py and routes/handoff.py) must see zero behavior
    change: item_executor_contracts stays populated with the library default."""
    item = await _add_heavy_pending_item(db, project["id"])
    contract = await handoff_module.build_effective_capability_contract(db, project["id"])
    assert contract is not None
    by_id = {e["item_id"]: e for e in contract["item_executor_contracts"]}
    assert item["id"] in by_id


async def test_build_effective_capability_contract_explicit_zero_caps(db, project):
    await _add_heavy_pending_item(db, project["id"])
    contract = await handoff_module.build_effective_capability_contract(
        db, project["id"], max_executor_contracts=0, max_contract_list_items=0,
    )
    assert contract is not None
    assert contract["item_executor_contracts"] == []
    assert contract["item_tool_requirements"] == []
    assert contract["item_sprint_item_pointers"] == []
    assert contract["item_artifact_pointer_findings"] == []
    # Direct parity check against calling capability_contract.build_capability_contract
    # with the same caps — the wrapper must not alter the underlying shape.
    # (Compare the deterministic, capped fields directly rather than full
    # serialize_contract equality, since generated_at is wall-clock and each
    # call builds a fresh contract.)
    direct = await cc.build_capability_contract(
        db, project["id"], max_executor_contracts=0, max_contract_list_items=0,
    )
    assert contract["item_executor_contracts_truncated"] == direct["item_executor_contracts_truncated"]
    assert contract["item_tool_requirements_truncated"] == direct["item_tool_requirements_truncated"]
    assert contract["manifest_hash"] == direct["manifest_hash"]
