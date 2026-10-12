"""Regression tests for sprint item de7e3c2a (Phase 1 of decision automatic-mcp).

Wires the real availability inputs into every trusted capability-contract
call site, and adds a claim-time contract to claim_sprint_item:

* ``handoff.contract_availability_kwargs`` picks the probe. A project that
  declares no capability gets nothing (no tunnel probe, prior "unknown"
  output). A declared-capability project with a hosted tenant passes the
  tenant (real tunnel/plugin inventory). A declared-capability project with no
  tenant gets the explicit local-first floor
  (``capability_availability.local_first_live_inventory``) instead of the
  blanket ``"unknown"`` degrade.
* The four pre-existing call sites (start_session, the generate_handoff
  dispatch, and the two REST handoff routes) pass that kwargs dict.
* ``handoff.build_claim_capability_contract`` / the claim_sprint_item dispatch
  attach a contract scoped to the claimed item, but only when the item declares
  a tool requirement AND the project declares a capability. WARN-ONLY: it
  never blocks a claim.

Required cases from the item notes:
  (a) a project with NO declared manifest is completely unaffected: no tunnel
      probe is sent and the contract output is the prior "unknown" shape;
  (b) a hosted project with a declared required capability and a live tunnel
      reports executable truthfully, and a dead tunnel fails closed as
      ``missing`` (not a permanent ``unknown``);
  (c) a self-hosted/no-tenant session gets the local-first floor.

Honesty caveat (handoff_receipt precedent): every verdict here is
agent-cooperative, not transport-level. A test in this file never implies a
wire-level guarantee.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

import meridian.server  # noqa: F401 — import first to avoid cycle
from meridian import capability_availability as ca
from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian.mcp import handler as mcp_handler
from meridian.mcp.handlers import project_tools as pt_mod
from meridian.routes import handoff as routes_handoff


@pytest_asyncio.fixture
async def db():
    conn = await db_module.init_db(":memory:")
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def project(db):
    return await db_module.create_project(db, "sprint-35-de7e3c2a")


def _cap(**overrides):
    base = {
        "id": "alpha",
        "purpose": "regression capability",
        "required_tools": ["codebase__search_graph"],
        "availability_policy": "required",
    }
    base.update(overrides)
    return base


def _live_inventory(*, tunnel_reachable: bool) -> dict:
    """A hosted-shaped inventory: one enabled, invocable codebase plugin
    exposing search_graph. ``tunnel_reachable`` toggles whether it is live."""
    return {
        "tunnel_reachable": tunnel_reachable,
        "builtin_tools": {"paper_search"},
        "plugins": {
            "codebase": {
                "slot": "code-intel",
                "enabled": True,
                "invocable": True,
                "tools": {"search_graph"},
            },
        },
        "stdio_registry": {},
    }


def _search_requirement() -> list[dict]:
    return [{
        "name": "search_graph",
        "server_or_namespace": "codebase",
        "required_or_preferred": "required",
        "purpose": "locate symbols",
    }]


def _record_probe(monkeypatch) -> list:
    """Replace the tunnel-probing inventory builder with a recorder that serves
    a live hosted inventory. Returns the list of tenants it was called with."""
    calls: list = []

    async def _probe(tenant):
        calls.append(tenant)
        return _live_inventory(tunnel_reachable=True)

    monkeypatch.setattr(pt_mod, "_build_live_inventory", _probe)
    return calls


# ---------------------------------------------------------------------------
# contract_availability_kwargs — the single probe-selection decision.
# ---------------------------------------------------------------------------

async def test_kwargs_no_manifest_is_empty_so_nothing_is_probed(db, project):
    assert await handoff_module.contract_availability_kwargs(
        db, project["id"], {"id": "t1"},
    ) == {}
    assert await handoff_module.contract_availability_kwargs(
        db, project["id"], None,
    ) == {}


async def test_kwargs_declared_tenant_passes_tenant_through(db, project):
    await db_module.set_project_capability_manifest(db, project["id"], [_cap()])
    assert await handoff_module.contract_availability_kwargs(
        db, project["id"], {"id": "t1"},
    ) == {"tenant": {"id": "t1"}}


async def test_kwargs_declared_no_tenant_gets_local_first_floor(db, project):
    await db_module.set_project_capability_manifest(db, project["id"], [_cap()])
    kwargs = await handoff_module.contract_availability_kwargs(db, project["id"], None)
    assert set(kwargs) == {"live_inventory"}
    inv = kwargs["live_inventory"]
    # The honest floor: tunnel presumed unreachable, native tools only.
    assert inv["tunnel_reachable"] is False
    assert inv["plugins"] == {}
    assert "paper_search" in inv["builtin_tools"]


# ---------------------------------------------------------------------------
# (a) No declared manifest: completely unaffected.
# ---------------------------------------------------------------------------

def test_checker_with_no_capabilities_reports_empty_checked_summary():
    """Pinned MDE-1 behavior for an explicit checker run over an empty list:
    an honest, empty summary -- nothing declared, nothing missing."""
    checker = handoff_module._availability_checker_from_live_inventory(
        ca.local_first_live_inventory(),
    )
    assert checker([]) == {"available": [], "missing": [], "degraded": []}


async def test_no_manifest_contract_is_unaffected_and_never_probes(db, project, monkeypatch):
    calls = _record_probe(monkeypatch)
    contract = await handoff_module.build_effective_capability_contract(
        db, project["id"],
        **(await handoff_module.contract_availability_kwargs(db, project["id"], {"id": "t1"})),
    )
    assert calls == []  # no tunnel probe for a project that declares nothing
    assert contract is not None
    assert contract["availability"]["status"] == "unknown"
    assert contract["availability"]["available"] == "unknown"
    assert contract["availability"]["missing"] == "unknown"
    assert contract["availability"]["degraded"] == "unknown"
    assert contract["executable"] is True
    assert contract["executable_reasons"] == []


async def test_no_manifest_start_session_hosted_sends_no_probe(db, project, monkeypatch):
    calls = _record_probe(monkeypatch)
    result = await pt_mod.handle_start_session(
        {"project_id": project["id"], "session_name": "sprint-35-no-manifest"},
        db,
        "/tmp/meridian-test",
        tenant={"id": "tenant-live"},
        _mcp_tenant_id=None,
        executor_sessions=set(),
    )
    assert calls == []
    contract = result.get("capability_contract")
    assert contract is not None
    assert contract["availability"]["status"] == "unknown"
    assert contract["executable"] is True
    assert contract["executable_reasons"] == []


async def test_no_manifest_claim_gets_no_capability_contract(db, project):
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Needs a search tool",
        tool_requirements=_search_requirement(),
    )
    stored = await db_module.get_sprint_item(db, item["id"])
    assert await handoff_module.build_claim_capability_contract(
        db, project["id"], stored,
    ) is None


# ---------------------------------------------------------------------------
# (b) Hosted tenant + declared required capability: executable is truthful.
# ---------------------------------------------------------------------------

async def test_hosted_live_tunnel_required_capability_is_executable(db, project, monkeypatch):
    await db_module.set_project_capability_manifest(db, project["id"], [_cap()])

    async def _live(_tenant):
        return _live_inventory(tunnel_reachable=True)

    monkeypatch.setattr(pt_mod, "_build_live_inventory", _live)
    contract = await handoff_module.build_effective_capability_contract(
        db, project["id"],
        **(await handoff_module.contract_availability_kwargs(db, project["id"], {"id": "tenant-live"})),
    )
    assert contract["availability"]["available"] == ["alpha"]
    assert contract["executable"] is True
    assert contract["executable_reasons"] == []


async def test_hosted_dead_tunnel_required_capability_fails_closed_as_missing(db, project, monkeypatch):
    """Not 'unknown': a required capability whose tunnel is down is reported
    missing, so the contract says exactly why it is not executable."""
    await db_module.set_project_capability_manifest(db, project["id"], [_cap()])

    async def _dead(_tenant):
        return _live_inventory(tunnel_reachable=False)

    monkeypatch.setattr(pt_mod, "_build_live_inventory", _dead)
    contract = await handoff_module.build_effective_capability_contract(
        db, project["id"],
        **(await handoff_module.contract_availability_kwargs(db, project["id"], {"id": "tenant-dead"})),
    )
    assert contract["availability"]["status"] != "unknown"
    assert contract["availability"]["missing"] == ["alpha"]
    assert contract["executable"] is False
    assert "missing_required_capabilities:alpha" in contract["executable_reasons"]
    assert not any(r.startswith("required_capabilities_unverified") for r in contract["executable_reasons"])


async def test_start_session_hosted_tenant_reaches_contract(db, project, monkeypatch):
    """The start_session call site must pass the tenant through, so the hosted
    orientation reports the real live verdict instead of the blanket unknown."""
    await db_module.set_project_capability_manifest(db, project["id"], [_cap()])
    calls = _record_probe(monkeypatch)
    result = await pt_mod.handle_start_session(
        {"project_id": project["id"], "session_name": "sprint-35-hosted"},
        db,
        "/tmp/meridian-test",
        tenant={"id": "tenant-live"},
        _mcp_tenant_id=None,
        executor_sessions=set(),
    )
    assert calls == [{"id": "tenant-live"}]
    contract = result["capability_contract"]
    assert contract["availability"]["available"] == ["alpha"]
    assert contract["executable"] is True


# ---------------------------------------------------------------------------
# (c) Self-hosted / no tenant: the honest local-first floor, not blanket unknown.
# ---------------------------------------------------------------------------

async def test_self_hosted_session_gets_local_first_floor(db, project):
    await db_module.set_project_capability_manifest(db, project["id"], [
        _cap(id="native-only", required_tools=["paper_search"]),
        _cap(id="tunnel-only", required_tools=["codebase__search_graph"]),
    ])
    contract = await handoff_module.build_effective_capability_contract(
        db, project["id"],
        **(await handoff_module.contract_availability_kwargs(db, project["id"], None)),
    )
    assert contract["availability"]["status"] != "unknown"
    # Native tool resolves genuinely available with no tunnel at all.
    assert contract["availability"]["available"] == ["native-only"]
    # Tunnel-backed tool stays honestly missing (fail-closed), never faked ok.
    assert contract["availability"]["missing"] == ["tunnel-only"]
    assert contract["executable"] is False
    assert "missing_required_capabilities:tunnel-only" in contract["executable_reasons"]


async def test_rest_helper_resolves_tenant_or_falls_back_to_local_floor(db, project, monkeypatch):
    await db_module.set_project_capability_manifest(db, project["id"], [_cap()])

    async def _no_credential(_request):
        raise RuntimeError("no bearer or session")

    monkeypatch.setattr(routes_handoff, "_get_authenticated_tenant", _no_credential)
    kwargs = await routes_handoff._contract_availability_kwargs(object(), db, project["id"])
    assert kwargs["live_inventory"]["tunnel_reachable"] is False

    async def _hosted(_request):
        return {"id": "tenant-rest"}

    monkeypatch.setattr(routes_handoff, "_get_authenticated_tenant", _hosted)
    kwargs = await routes_handoff._contract_availability_kwargs(object(), db, project["id"])
    assert kwargs == {"tenant": {"id": "tenant-rest"}}


# ---------------------------------------------------------------------------
# Claim-time contract: scoped to the claimed item, warn-only, never blocks.
# ---------------------------------------------------------------------------

async def test_claim_contract_emitted_for_item_with_requirement_and_manifest(db, project):
    await db_module.set_project_capability_manifest(db, project["id"], [_cap()])
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Needs search_graph",
        tool_requirements=_search_requirement(),
    )
    stored = await db_module.get_sprint_item(db, item["id"])
    contract = await handoff_module.build_claim_capability_contract(
        db, project["id"], stored, tenant=None,
    )
    assert contract is not None
    # Scoped to the claimed item: only this item appears in the item sections.
    assert [e["item_id"] for e in contract["item_tool_requirements"]] == [item["id"]]
    # Self-hosted claim gets the local-first floor: tunnel-only alpha is missing.
    assert contract["executable"] is False
    assert "missing_required_capabilities:alpha" in contract["executable_reasons"]


async def test_claim_contract_omitted_when_item_declares_no_requirement(db, project):
    await db_module.set_project_capability_manifest(db, project["id"], [_cap()])
    item = await db_module.add_sprint_item(db, project["id"], "v1", "No tools needed")
    stored = await db_module.get_sprint_item(db, item["id"])
    assert await handoff_module.build_claim_capability_contract(
        db, project["id"], stored,
    ) is None


async def test_attach_claim_contract_only_annotates_successful_claims(db, project):
    await db_module.set_project_capability_manifest(db, project["id"], [_cap()])
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Needs search_graph",
        tool_requirements=_search_requirement(),
    )
    args = {"project_id": project["id"], "item_id": item["id"], "session_id": "s35"}

    refusal = {"error": "RESOURCE_LOCKED", "status": "in_progress", "project_id": project["id"]}
    assert await mcp_handler._attach_claim_capability_contract(refusal, args, db, None) is refusal

    pending = {"id": item["id"], "status": "pending", "project_id": project["id"]}
    assert await mcp_handler._attach_claim_capability_contract(pending, args, db, None) is pending

    # A real claim response carries the full stored item (incl. tool_requirements).
    stored = await db_module.get_sprint_item(db, item["id"])
    claimed = {**stored, "status": "in_progress"}
    annotated = await mcp_handler._attach_claim_capability_contract(claimed, args, db, None)
    assert annotated is not claimed
    assert "capability_contract" in annotated
    assert "capability_contract" not in claimed  # input never mutated


async def test_claim_sprint_item_dispatch_attaches_contract_end_to_end(db, project):
    """The real claim_sprint_item dispatch path: a successful claim of an item
    that declares a requirement, in a project with a manifest, carries the
    claim-time contract. Warn-only: the claim itself still succeeds."""
    await db_module.set_project_capability_manifest(db, project["id"], [_cap()])
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Needs search_graph",
        tool_requirements=_search_requirement(),
    )
    result = await mcp_handler._handle_sprint_tools(
        "claim_sprint_item",
        {"project_id": project["id"], "item_id": item["id"], "session_id": "s35-claim"},
        db, "/tmp/meridian-test", None, None,
    )
    assert result.get("status") == "in_progress", result
    assert "capability_contract" in result
    assert result["capability_contract"]["executable"] is False  # local floor, warn-only
