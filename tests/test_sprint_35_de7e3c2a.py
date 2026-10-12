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

Review finding (de7e3c2a): the live-inventory probe is awaited on every one of
those paths, and a cold tunnel slot can stall it for ~60s while its verdict is
warn-only. The probe is therefore bounded by a short budget, and a probe that
never returns degrades to the unverified state instead of holding the call.
The bound tests at the bottom of this file pin that behaviour, and the wiring
test pins that generate_handoff and both REST handoff routes pass
``contract_availability_kwargs``.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import pytest
import pytest_asyncio

import meridian.server  # noqa: F401 — import first to avoid cycle
from meridian import capability_availability as ca
from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import handoff_receipt as receipt_module
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


# ---------------------------------------------------------------------------
# Review finding (de7e3c2a): the live-inventory probe must be bounded.
#
# start_session, generate_handoff, both REST handoff routes and the claim-time
# contract all await the tenant probe through build_effective_capability_contract.
# A cold tunnel slot can stall that probe for ~60s, and its verdict is only a
# warn-only field, so an unbounded await held a trusted response for nothing.
# ---------------------------------------------------------------------------

# Outer guard for every bound test. On unbounded code the awaited call would
# hang forever, so the guard turns that hang into a TimeoutError and the test
# fails instead of stalling the suite. It sits well above the ~5s budget.
_PROBE_GUARD_SECONDS = 15.0

# The production budget is about 5s. Allow scheduling slack on top of it.
_DEFAULT_BUDGET_CEILING_SECONDS = 7.0


# How long the modelled cold tunnel slot stalls. Deliberately longer than the
# guard: a bounded call cancels the probe long before this, and an unbounded
# call is cut off by the guard. The probe never silently succeeds (it fails if
# it ever runs to completion), so a leftover request cannot stall teardown.
_HANG_SECONDS = 20.0


def _hanging_probe(calls: list):
    """A live-inventory probe that stalls, modelling a cold tunnel slot."""
    async def _probe(tenant):
        calls.append(tenant)
        await asyncio.sleep(_HANG_SECONDS)
        raise AssertionError("probe ran to completion: the call was not bounded")
    return _probe


def _shrink_probe_budget(monkeypatch) -> None:
    """Keep path tests fast. The default budget is asserted by the canonical
    test below; the path tests only need the mechanism to fire quickly."""
    monkeypatch.setattr(
        handoff_module, "_LIVE_INVENTORY_PROBE_BUDGET_SECONDS", 0.2, raising=False,
    )


async def test_never_returning_probe_resolves_within_budget_in_unverified_state(
    db, project, monkeypatch,
):
    """Regression (de7e3c2a review): a probe that never returns must not hold
    build_effective_capability_contract past the ~5s budget. The contract comes
    back in the unverified state, never a faked verdict."""
    await db_module.set_project_capability_manifest(db, project["id"], [_cap()])
    calls: list = []
    monkeypatch.setattr(pt_mod, "_build_live_inventory", _hanging_probe(calls))

    started = time.monotonic()
    contract = await asyncio.wait_for(
        handoff_module.build_effective_capability_contract(
            db, project["id"], tenant={"id": "tenant-cold"},
        ),
        timeout=_PROBE_GUARD_SECONDS,
    )
    elapsed = time.monotonic() - started

    assert calls == [{"id": "tenant-cold"}]  # the probe really was awaited
    assert elapsed < _DEFAULT_BUDGET_CEILING_SECONDS, elapsed
    assert contract is not None
    assert contract["availability"]["status"] == "unknown"
    assert contract["availability"]["available"] == "unknown"
    # Unverified, not faked: a required capability cannot be claimed executable.
    assert contract["executable"] is False
    assert "required_capabilities_unverified:alpha" in contract["executable_reasons"]


async def test_start_session_hanging_probe_is_bounded(db, project, monkeypatch):
    await db_module.set_project_capability_manifest(db, project["id"], [_cap()])
    _shrink_probe_budget(monkeypatch)
    calls: list = []
    monkeypatch.setattr(pt_mod, "_build_live_inventory", _hanging_probe(calls))
    result = await asyncio.wait_for(
        pt_mod.handle_start_session(
            {"project_id": project["id"], "session_name": "sprint-35-cold-probe"},
            db,
            "/tmp/meridian-test",
            tenant={"id": "tenant-cold"},
            _mcp_tenant_id=None,
            executor_sessions=set(),
        ),
        timeout=_PROBE_GUARD_SECONDS,
    )
    assert calls == [{"id": "tenant-cold"}]
    contract = result["capability_contract"]
    assert contract["availability"]["status"] == "unknown"
    assert contract["executable"] is False


async def test_claim_contract_hanging_probe_is_bounded_and_never_blocks(db, project, monkeypatch):
    """claim_sprint_item's attach step: the claim is annotated, not refused."""
    await db_module.set_project_capability_manifest(db, project["id"], [_cap()])
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Needs search_graph",
        tool_requirements=_search_requirement(),
    )
    stored = await db_module.get_sprint_item(db, item["id"])
    _shrink_probe_budget(monkeypatch)
    calls: list = []
    monkeypatch.setattr(pt_mod, "_build_live_inventory", _hanging_probe(calls))
    args = {"project_id": project["id"], "item_id": item["id"], "session_id": "s35-cold"}
    claimed = {**stored, "status": "in_progress"}
    annotated = await asyncio.wait_for(
        mcp_handler._attach_claim_capability_contract(claimed, args, db, {"id": "tenant-cold"}),
        timeout=_PROBE_GUARD_SECONDS,
    )
    assert calls == [{"id": "tenant-cold"}]
    assert annotated["status"] == "in_progress"  # warn-only: the claim stands
    contract = annotated["capability_contract"]
    assert contract["availability"]["status"] == "unknown"
    assert contract["executable"] is False


async def test_generate_handoff_hanging_probe_is_bounded(db, project, monkeypatch, tmp_path):
    await db_module.set_project_capability_manifest(db, project["id"], [_cap()])
    _shrink_probe_budget(monkeypatch)
    calls: list = []
    monkeypatch.setattr(pt_mod, "_build_live_inventory", _hanging_probe(calls))
    result = await asyncio.wait_for(
        mcp_handler._handle_task_tools(
            "generate_handoff",
            {"project_id": project["id"], "mode": "goal"},
            db, str(tmp_path), {"id": "tenant-cold"}, None,
        ),
        timeout=_PROBE_GUARD_SECONDS,
    )
    assert calls == [{"id": "tenant-cold"}]
    contract = result["capability_contract"]
    assert contract["availability"]["status"] == "unknown"
    assert contract["executable"] is False


# --- REST: both handoff routes, driven through the real app (TestClient). ---

def _mcp_call(client, name: str, arguments: dict) -> dict:
    r = client.post("/mcp/sse", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert r.status_code == 200
    return r.json()


def _mcp_result(client, name: str, arguments: dict) -> Any:
    resp = _mcp_call(client, name, arguments)
    assert resp.get("result") is not None, resp
    return json.loads(resp["result"]["content"][0]["text"])


def _client_project(client, name: str, capabilities: list[dict]) -> str:
    pid = client.post("/projects", json={"name": name}).json()["id"]
    _mcp_result(client, "set_capability_manifest", {
        "project_id": pid, "capabilities": capabilities,
    })
    return pid


def _native_capability(cap_id: str = "native-only") -> dict:
    """Resolves genuinely available with no tunnel (native builtin tool)."""
    return {
        "id": cap_id,
        "purpose": "native capability",
        "required_tools": ["paper_search"],
        "availability_policy": "required",
    }


def _call_with_guard(fn):
    """Run a blocking TestClient call under a hard ceiling. A hung request in a
    daemon thread becomes an assertion failure instead of stalling the suite."""
    box: dict[str, Any] = {}

    def _run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            box["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(_PROBE_GUARD_SECONDS)
    if worker.is_alive():
        raise AssertionError("REST request still running after the guard: probe is unbounded")
    if "error" in box:
        raise box["error"]
    return box["value"]


@pytest.mark.parametrize(
    "method,path_suffix",
    [("GET", "/handoff/planner"), ("POST", "/handoff")],
)
def test_rest_handoff_route_hanging_probe_is_bounded(client, monkeypatch, method, path_suffix):
    pid = _client_project(
        client, f"sprint-35-rest-cold-{method.lower()}",
        [{"id": "alpha", "purpose": "regression capability",
          "required_tools": ["Serena: find_symbol"], "availability_policy": "required"}],
    )
    _shrink_probe_budget(monkeypatch)
    calls: list = []
    monkeypatch.setattr(pt_mod, "_build_live_inventory", _hanging_probe(calls))

    async def _hosted(_request):
        return {"id": "tenant-cold"}

    monkeypatch.setattr(routes_handoff, "_get_authenticated_tenant", _hosted)
    response = _call_with_guard(lambda: client.request(method, f"/projects/{pid}{path_suffix}"))
    assert response.status_code == 200, response.text
    assert calls == [{"id": "tenant-cold"}]
    contract = response.json()["capability_contract"]
    assert contract["availability"]["status"] == "unknown"
    assert contract["executable"] is False


# --- Wiring: generate_handoff and both REST routes pass contract_availability_kwargs. ---

def test_generate_handoff_and_rest_routes_pass_contract_availability_kwargs(client, monkeypatch):
    """Wiring (de7e3c2a review): the probe-selection decision must reach every
    trusted call site. Each of the three sites must call the helper for this
    project, and the resulting contract must carry the real verdict (the
    local-first floor resolves the native capability available), proving the
    returned kwargs are spliced into build_effective_capability_contract."""
    seen: list[tuple[str, Any]] = []
    real_kwargs = handoff_module.contract_availability_kwargs

    async def _spy(db, project_id, tenant):
        seen.append((project_id, tenant))
        return await real_kwargs(db, project_id, tenant)

    monkeypatch.setattr(handoff_module, "contract_availability_kwargs", _spy)
    pid = _client_project(client, "sprint-35-wiring", [_native_capability()])

    def _during(fn):
        before = len(seen)
        response = fn()
        return response, [p for p, _ in seen[before:]]

    post, post_calls = _during(lambda: client.post(f"/projects/{pid}/handoff"))
    assert post.status_code == 200, post.text
    assert post_calls and set(post_calls) == {pid}
    assert post.json()["capability_contract"]["availability"]["available"] == ["native-only"]

    planner, planner_calls = _during(lambda: client.get(f"/projects/{pid}/handoff/planner"))
    assert planner.status_code == 200, planner.text
    assert planner_calls and set(planner_calls) == {pid}
    assert planner.json()["capability_contract"]["availability"]["available"] == ["native-only"]

    mcp_result, mcp_calls = _during(
        lambda: _mcp_result(client, "generate_handoff", {"project_id": pid, "mode": "goal"}),
    )
    assert mcp_calls and set(mcp_calls) == {pid}
    assert mcp_result["capability_contract"]["availability"]["available"] == ["native-only"]


# ---------------------------------------------------------------------------
# Review finding (de7e3c2a), claim_sprint_item path: the handoff-provenance
# check awaited the same live-inventory probe with no bound.
#
# handle_claim_sprint_item calls handoff_receipt.verify_handoff_provenance after
# the claim has already committed. That check reaches check_capability_availability
# -> _build_live_inventory, which had no bound, so a cold tunnel slot could hold
# the claim response for ~60s while its verdict is warn-only. The probe is now
# bounded by the same shared budget, resolves in the unverified state, and the
# claim still commits.
# ---------------------------------------------------------------------------

def _provenance_cap() -> dict:
    return _cap(
        id=receipt_module.HANDOFF_PROVENANCE_CAPABILITY_ID,
        required_tools=["verify_handoff_token"],
    )


async def test_claim_sprint_item_hanging_provenance_probe_is_bounded_and_claim_commits(
    db, project, monkeypatch,
):
    """Regression (de7e3c2a review, claim path): a never-returning live-inventory
    probe must not hold claim_sprint_item past the budget. The claim still
    commits, and the provenance result stays warn-only, never a block."""
    await db_module.set_project_capability_manifest(db, project["id"], [_provenance_cap()])
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Claim on a cold tunnel")
    _shrink_probe_budget(monkeypatch)
    calls: list = []
    monkeypatch.setattr(pt_mod, "_build_live_inventory", _hanging_probe(calls))

    started = time.monotonic()
    result = await asyncio.wait_for(
        mcp_handler._handle_sprint_tools(
            "claim_sprint_item",
            {"project_id": project["id"], "item_id": item["id"], "session_id": "s35-provenance-cold"},
            db, "/tmp/meridian-test", {"id": "tenant-cold"}, None,
        ),
        timeout=_PROBE_GUARD_SECONDS,
    )
    elapsed = time.monotonic() - started

    assert calls == [{"id": "tenant-cold"}]  # the probe really was awaited
    assert elapsed < 3.0, elapsed  # 0.2s shrunk budget plus scheduling slack
    assert result.get("status") == "in_progress", result  # the claim stands
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["status"] == "in_progress"  # committed, not rolled back
    # Unverified, not faked: no receipt exists, so the warn-only field is set.
    assert result.get("handoff_provenance_warning")
    assert "handoff_provenance_receipt" not in result


async def test_provenance_check_hanging_probe_resolves_within_default_budget(
    db, project, monkeypatch,
):
    """Canonical budget pin for the claim path: at the production budget a
    never-returning probe resolves in the unverified state instead of being held."""
    await db_module.set_project_capability_manifest(db, project["id"], [_provenance_cap()])
    calls: list = []
    monkeypatch.setattr(pt_mod, "_build_live_inventory", _hanging_probe(calls))

    started = time.monotonic()
    check = await asyncio.wait_for(
        receipt_module.verify_handoff_provenance(
            db, {"id": "tenant-cold"}, project["id"], session_id="s35-cold",
        ),
        timeout=_PROBE_GUARD_SECONDS,
    )
    elapsed = time.monotonic() - started

    assert calls == [{"id": "tenant-cold"}]
    assert elapsed < _DEFAULT_BUDGET_CEILING_SECONDS, elapsed
    assert check["applicable"] is True  # the project declared the capability
    assert check["ok"] is True  # warn-only: never blocks
    assert check["degraded"] is True  # no receipt, so the warning path is taken
    assert check["capability"]["status"] == "unknown"  # unverified, not faked


async def test_provenance_timeout_still_honours_a_real_receipt(db, project, monkeypatch):
    """A timed-out probe must not discard the receipt lookup: the receipt comes
    from the DB, not the probe, so a genuine verification still resolves."""
    await db_module.set_project_capability_manifest(db, project["id"], [_provenance_cap()])
    await receipt_module.record_handoff_provenance_receipt(
        db, tenant_id="tenant-cold", project_id=project["id"],
        session_id="s35-cold", tool_name="verify_handoff_token", outcome="valid",
    )
    _shrink_probe_budget(monkeypatch)
    calls: list = []
    monkeypatch.setattr(pt_mod, "_build_live_inventory", _hanging_probe(calls))

    check = await asyncio.wait_for(
        receipt_module.verify_handoff_provenance(
            db, {"id": "tenant-cold"}, project["id"], session_id="s35-cold",
        ),
        timeout=_PROBE_GUARD_SECONDS,
    )

    assert calls == [{"id": "tenant-cold"}]
    assert check["applicable"] is True
    assert check["receipt"] is not None
    assert check["degraded"] is False
    assert check["warning"] is None
